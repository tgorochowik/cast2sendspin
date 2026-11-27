# Playback latency investigation

Recorded 2026-11-27. These are observations from a local debugging session, not
controlled benchmarks or guarantees for other devices/networks. The runtime was
Music Assistant 2.10.13 with a ReSpeaker Sendspin output.

## What we kept

- **Persistent sender-authorized YouTube resolver:** one Node resolver process per
  receiver, warmed asynchronously at startup. Reuses initialization and connections;
  serializes requests so credentials cannot overlap. Clears the per-request user
  context and credential-transfer token after use. Initialization expires after
  30 minutes; failures force reinitialization on the next request. Cancellation or
  an RPC timeout kills/reaps the process before another request can use it.
- **Audio access check and continuous streaming:** resolve the authorized media
  URL, request a small initial byte range to check access, then let FFmpeg decode
  the URL into PCM as it arrives. Playback never required downloading the whole
  song first. Keep Premium format preference and fail closed when sender
  authorization is unavailable; anonymous fallback remains explicitly opt-in.
- **Fast output interruption for pause/seek:** this MA live source supports Stop,
  not native Pause. Per-speaker pause stops MA output while retaining the cast
  session and remembered position; resume reselects the source. Seek interrupts
  output before restarting at the new position. MA can show idle while the phone
  is paused. Source-stop callbacks caused by these operations must not be mistaken
  for external takeovers; actual ownership changes must still relinquish control.
  Manual/single-source routing does not control arbitrary MA output players.
- **Decoder shutdown:** drain FFmpeg's stdout while waiting for termination.
  Otherwise a full PCM pipe or shutdown flush can stall until the three-second
  kill timeout. Serialize decoder pipe reads against the capture loop.
- **Track-replacement handling:** YouTube's `setPlaylist` handler can call Stop
  before Play, outside the bridge's `play()` wrapper. Distinguish this from a real
  Stop, so changing tracks does not unnecessarily release/reclaim the MA source
  or cycle its power. Compare selection ID/index; reset session bookkeeping on
  genuine Stop/reset.
- **Lightweight diagnostics:** command boundaries/durations, resolver stage
  timings, decoder startup/shutdown, and PCM transitions/stalls. Never log sender
  credentials, raw playback parameters, signed URLs, or raw upstream exceptions.

## Measurements

| Measurement | Observed result |
| --- | --- |
| Original per-track YouTube resolution | About 2.1–2.9 s |
| Persistent resolver, five successful warm resolutions | 268–386 ms |
| Those five play commands through first PCM playback confirmation | 443–574 ms |
| Initialization-only check, cold vs warm, same process | 2.230 s vs 0.002 s |
| Pause command completion / silence fed to Sendspin | About 3 ms / 14–22 ms |
| Resume command to audio fed to Sendspin | About 24 ms |
| Seek command to new-position audio before output interruption fix | About 507 ms |
| Track-switch shutdown gap before draining stdout | About 3.3 s |
| Gap after the shutdown fix, early observations | About 0.5 s |

The user heard roughly 1.5–2 s of pause/seek delay when the bridge merely changed
incoming PCM or sent silence. Those PCM changes reached Sendspin promptly, but
previously queued audio continued downstream. MA's own Stop was immediately
audible, motivating the output interruption approach. Its exact buffering
location was not instrumented; this is not a measured end-to-end network latency.

### Lookahead comparison

Measured from the start of `track_stop` to `PLAYING`, including stopping the old
decoder and starting the new track:

| Path | Individual samples (ms) | Mean |
| --- | --- | --- |
| No buffer reuse | 544, 528, 590, 693, 885 | 648 ms |
| Validated ten-second buffer reuse | 407, 629 | 518 ms |

Difference: about **130 ms / 20%** in these small, non-randomized samples. This
excludes the phone's command delivery, earlier receiver queue/navigation work, and
speaker-audible buffering. Do not treat it as a controlled A/B result.

For the two validated hits specifically:

- Fresh YouTube resolution/access validation: **226 ms and 303 ms**.
- After validation, buffered playback confirmation: **about 51 ms**.
- Play-command totals: **279 ms and 356 ms**.
- Separate preceding decoder-stop durations: **127 ms and 273 ms**.

## Next-track buffering experiment

This experiment worked after handling parameter differences, but was removed.
Its code was not committed separately before removal; these notes preserve the
design and evidence, not an archived implementation.

1. Observe `player.queue.getState()` in `yt-cast-receiver`, which exposes
   `current`, `next`, and `autoplay`. Observe queue/state changes; never call
   `next()` just to inspect the queue. YouTube remains the queue authority.
2. Transfer only the next video ID and its own `context.ctt` / `context.params`
   to the Python worker through private stdin.
3. Resolve speculatively in a **separate persistent resolver process**, so
   background work/cancellation cannot block foreground resolution or destroy
   its warm session.
4. Start FFmpeg at position zero and read at most **10 seconds of PCM** into
   memory: 48 kHz, stereo, signed 16-bit little-endian = 192,000 bytes/s, or
   **1,920,000 bytes** for ten seconds. This is audio-buffer memory only, not the
   total memory cost of the extra resolver/decoder.
5. Leave that decoder backpressured on its stdout pipe. On a valid hit, transfer
   the prepared PCM **and the same decoder** into playback: send the prefix
   first, then continue reading its stdout. This avoids reopening/seeking the
   media URL at the buffer boundary. Short tracks can use a shorter complete
   prefix. A real-pipe test checked byte-for-byte ordering through handoff.
6. Prepare only one next track; cancel/reap it on queue/authorization changes,
   genuine Stop, disconnect, takeover, or shutdown. Incomplete preparation must
   never delay foreground playback. Nonzero start positions do not use a prefix
   decoded from zero.
7. Expire prepared entries after **120 seconds**; queue observation could request
   a refresh. This bounded stale URLs/connections, but could repeat background
   work on long tracks or long pauses.
8. An optional one-shot recovery reopened a failed held decoder at the consumed
   PCM offset. It did not guarantee recovery from expired URLs or missing
   authorization. Do not mistake a ten-second prefix for protection against all
   later network failures.

The first background preparation took about 2.2–2.4 s while its resolver warmed.
Subsequent observed preparations took roughly 0.4–0.9 s. `PREFETCH_READY` reported
ten seconds prepared, but preparation alone did not prove playback used it.

## Authorization and playback parameters

The initial cache key required an exact video ID, sender token (`ctt`), and
opaque playback-context string (`params`). On actual Next transitions:

- The predicted video matched the selected video.
- The authorization token matched.
- The ten-second buffer was ready, unexpired, and its decoder was healthy.
- **The playback parameters differed.**

That produced cache misses even though prediction and buffering worked. The
parameter fields were not decoded; do not claim they are exclusively navigation
data or that they cannot affect audio. Their actual values were never logged.

### Validated reuse that worked

Do not simply remove `params` from the key. The successful conservative path was:

1. Keep exact video/token matching, zero start position, expiry, and decoder-health
   checks. A different or missing token never qualified for this exception.
2. When only parameters differed, resolve the actual selection using its actual
   parameters and the identical sender token, including the audio-access check.
3. Require a matching private representation fingerprint incorporating requested
   video ID, itag, MIME/codecs, content length, last-modified revision, approximate
   duration, sample rate, channels, audio-track ID, DRC flag, and variant tags.
   Required numeric metadata had to be present and valid; an audio-track object
   without an ID was not sufficient evidence.
4. Also incorporate a SHA-256 hash and length of the first up-to-4096 encoded
   audio bytes. Collect the same bounded prefix irrespective of HTTP chunk
   boundaries, then cancel the response body. Never download the whole song to
   calculate this evidence. Metadata plus a matching prefix is conservative
   representation evidence, **not a cryptographic proof of whole-file equality**.
5. Reuse only when these fingerprints match and both results are sender-authorized.
   Missing evidence, another format/language/version, or anonymous resolution
   falls back to normal streaming. Use the freshly checked URL for future
   seeks/recovery, not the speculative URL.

The live logs then showed `PREFETCH_VALIDATED` followed by `PREFETCH_HIT` for both
tested transitions. Validation still cost 226–303 ms, limiting the improvement.

Miss diagnostics used only fixed reason codes and comparison booleans
(`same_video`, `same_authorization`, `same_parameters`, readiness, expiry,
position-zero and decoder-health checks). Tokens, parameters, signed URLs, and
the private fingerprints themselves must never enter logs or persistent caches.

## Why lookahead was removed

The large improvement came from retaining resolver initialization, which removed
roughly two seconds of per-track overhead. Lookahead saved only about 130 ms in
the observed switches while adding:

- Another resolver process and held FFmpeg decoder per active lookahead.
- Buffer handoff, expiry/refresh, cancellation, and process cleanup.
- Parameter-aware revalidation and representation-fingerprint machinery.
- More queue/session races and potential stale-connection recovery paths.

The user chose the simpler implementation. The persistent resolver, prompt
pause/seek, decoder shutdown fix, track-replacement handling, and lightweight
timings remain. Removing lookahead is a complexity tradeoff, not a claim that
prefetching never helps.

## If we revisit this

- Start from a concrete goal, such as seamless automatic transitions, rather than
  optimizing a small command-time measurement alone.
- Run a larger controlled comparison: same output, tracks, network, and starting
  state. Measure receiver command receipt, decoder stop, YouTube stages, first
  PCM, and speaker-audible transition separately.
- First inspect remaining decoder-stop overhead; the observed 127–273 ms can be
  comparable to the entire lookahead gain. Avoid broad process kills or regressions
  in cleanup/ownership safety.
- Recheck the installed `yt-cast-receiver` queue events and source lifecycle; their
  public API/version may change. Guard the interval where the queue selection has
  changed but the worker is still playing the previous item, or a queue listener
  can evict the very buffer being promoted.
- Retain tests for exact token matching, params-only validation, missing metadata,
  language/version/quality differences, zero vs nonzero start positions, queue
  edits, repeated video IDs at different indices, explicit Stop, disconnect,
  takeover, cancellation, EOF, expiry, byte-order continuity, and process reaping.
- Keep buffers bounded, in memory, and session-scoped. Background work must never
  delay foreground Play, advance the queue, or silently substitute authorization.
- Do not promise gapless playback or instant resume from source-only buffering:
  MA and speaker output buffering are separate concerns.
