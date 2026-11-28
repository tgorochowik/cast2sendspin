# Current-track buffer: manual seek evaluation

These are historical A/B evaluation notes. Following the trials, the user chose
automatic current-track fetching and a configurable stop-on-seek preference.
The old `media_buffer_enabled` experiment toggle below is no longer used.
Current configuration is documented in README.md and at the end of this file.
This is not next-track prefetch or gapless playback. Speaker buffering still
affects audible seeking even when all media bytes are local.

## Setup and instrumentation

- Use the same receiver, output volume, network and track in all rounds. Prefer
  an 8–12 minute track under the 32 MiB encoded-audio limit, so missing ranges are
  still available for the cold-cache trials. “Shallow” can also test the full-cache
  case, but may download too quickly for reliable cold-cache testing.
- Change only these root `config.json` settings between rounds and restart the
  manager. Reconnect the phone and select the track from the beginning after each
  restart; caches are intentionally discarded on Stop/track replacement.
- Leave other receivers idle to reduce network/load variation.
- `SEEK_STARTED` records mode and cache completeness; `BUFFER_READ` distinguishes
  cached reads from range misses; `SEEK_AUDIO` records time from worker seek
  handling to the first new-position PCM frame fed to Sendspin. The report also
  adds the estimated wait between command receipt and seek handling, so queued
  rapid seeks do not appear artificially fast. Command completion alone does not
  establish that audio was ready. Listen for the actual gap too.
- Logs never include sender context, signed URLs, or local capability URLs.

## A — Direct-streaming baseline

```json
{"media_buffer_enabled": false}
```

After five seconds of playback, seek to approximately 75%, 25%, 90%, 50%, 10%,
allowing about five seconds of playback after each seek. Repeat once.
Also do a rapid forward/backward pair (75% then 25% within about a second).
Report audible gaps, wrong-position playback, stalls or unexpected skips.

## B — Best case: completely fetched track

```json
{"media_buffer_enabled": true, "media_buffer_prefetch_delay_ms": 0}
```

Start the same track fresh. Wait for `BUFFER_COMPLETE` in its receiver log
(the agent can confirm this), then repeat A's seeks and rapid pair.
Confirm the initial mode is `buffered`, not a `BUFFER_UNAVAILABLE` fallback.
All seeks should use local bytes; full-cache seeks should not contact YouTube.

## C — Worst case: forward miss followed by backward miss

```json
{"media_buffer_enabled": true, "media_buffer_prefetch_delay_ms": 5000}
```

This diagnostic-only setting delays background chunk filling, **not** foreground
range requests. It makes incomplete-cache testing reproducible without throttling
the internet or degrading other services. It is not a production default.

Start the track fresh; as soon as audio starts, seek to 75%, then back to 25%
within about a second. Try 90% then 50% too. Repeat with a fresh track selection
three times (otherwise prior trials warm the same ranges).

Check the logs: the target range must actually be missing (`BUFFER_READ` with
`cache_hit: false`). If all reads hit, the trial did not exercise the worst case;
use a longer track or a different, uncached destination. Percent time is not
percent encoded bytes. Both target ranges should be fetched directly, without
waiting for background downloading to traverse the intervening file. Already
fetched ranges must survive forward/backward changes. A superseded seek need not
produce audio, but must not later replace the final selection.

## Comparison and decision

Generate a per-seek table and median/min/max for direct, full-cache, and miss
paths (including background-delay profile), e.g.:

```sh
.venv/bin/python scripts/seek_report.py .state/routes/RECEIVER_ID/bridge.log --since 2026-11-28T13:00:00
```

Use the actual trial start time and receiver ID from `.state/receivers.json`.
Keep superseded/no-audio/error trials visible; do not count them as latency wins.
Repeat the same seek sequence rather than comparing one unusually fast sample.
The timing does not include phone command delivery or speaker-audible buffering.

Keep the feature only if full-cache seeking improves consistently or audibly,
uncached/rapid seeks do not materially regress versus direct streaming, and
playback remains correct through Stop, replacement, takeover, and shutdown.
If MA output interruption dominates and the improvement is negligible, leave it
off or remove it rather than accepting complexity on the strength of one sample.
After testing, reset `media_buffer_prefetch_delay_ms` to `0`; enabling buffering
by default is a separate decision.

## Initial manual results — 2026-11-28

ReSpeaker, same “Again” selection (video `jPWNcfrZzBE`, itag 774,
978.641-second audio, 31,972,638 encoded bytes). The entire buffered track
downloaded in 1,488 ms. All five regular buffered seeks began with a complete
cache. Targets were approximate, not identical; order was direct then buffered,
not randomized.

| Regular seeks (n=5 per mode) | First PCM median | Range |
| --- | ---: | ---: |
| Direct | 751 ms | 606–919 ms |
| Fully cached | 662 ms | 436–786 ms |

The median difference was 89 ms (~12%), with overlapping ranges. The user
reported no noticeable improvement and possibly worse perceived behavior.
Median command-to-first-PCM remainder fell from 159 to 84 ms, but substantial
  output interruption/decoder shutdown overhead remained. These measurements do
not capture speaker-audible latency and do not establish a reliable speedup.

The rapid pairs each superseded the forward seek before its first PCM; final
backward seek took 186 ms direct and 109 ms buffered. Do not mix those unusually
short, already-interrupted-output cases into the regular-seek medians.
### Cold-cache rapid pairs

Background fill was delayed 5 seconds per chunk; foreground fetches were not
throttled. Both backward target reads were actual misses (byte offsets 10,488,685
and 13,689,194), fetched in about 6 ms apiece. Only about 13% and 22% of the file
was cached when the final target audio began.

The first forward range fetch was still completing as the backward seek arrived.
Both forward seeks were superseded before first PCM; the second was superseded
before a target-range fetch was observed. Final backward seeks took 226 and
125 ms from handling to first PCM. The latter also waited 299 ms behind its
predecessor, so it took about 424 ms including worker queue wait. These rapid
results are not comparable to ordinary seeks from stable playback.

There were no observed unexpected queue advances, playback failures, or forced
decoder kills in these trials. Decoder truncation/I/O diagnostics during
intentional seek cancellation are not new upstream connection failures.
No additional long-network-stall behavior was established by the manual trials;
that case is covered by synthetic tests, not a claim about all real networks.

Conclusion: the prototype handled the exercised misses, but the full-cache
listening trial did not demonstrate a worthwhile seeking benefit. Leave buffering
off for normal use, retain the committed HTTP reconnect fix, and do not promote
this experiment on the strength of its fast rapid-pair samples. Investigating a
source-scoped flush or no-output-stop seek is a separate output-latency experiment.

## Follow-up: seeking without stopping MA output

`"seek_without_output_stop": true` is a separate opt-in experiment, only for
automatic/per-speaker routing. While actively playing, a seek checks source
ownership, invalidates the old decoder, and replaces it without stopping MA,
reselecting its source, or ending the capture. The capture sends silence at its
normal cadence while the decoder is unavailable. It does not replay old decoder
frames. Pause, Stop, seek-while-paused, and ownership loss keep their safe behavior.
There is no claim that already-queued downstream audio has been flushed.

Compare these two profiles against the interrupted-output rounds above:

| Profile | `seek_without_output_stop` | `media_buffer_enabled` | Background delay |
| --- | --- | --- | --- |
| Direct, continuous | true | false | 0 |
| Full cache, continuous | true | true | 0 |

Use the same track and keep it playing during the trial. For direct mode, allow
five seconds after playback starts; for the cache round, first confirm
`BUFFER_COMPLETE`. Seek approximately 70% → 25% → 90% → 50% → 10%, about five
seconds apart; then 75% → 30%, about a second apart.

Report **both**:

1. Silence/dropout duration, or whether playback sounded continuous.
2. How long old-position audio continued before the new position became audible.

Do not infer audible latency from `SEEK_AUDIO`: output buffers may hold old audio
even if new-position PCM is fed quickly. `seek_report.py` separates continuous
and interrupted output, regular and rapid trials, and includes estimated command
queue wait. Confirm there is no new `SOURCE_STREAM_STARTED` event during an
ordinary continuous seek; intentional pause/resume still restarts it.
### Direct continuous-output result

Six regular seeks took 206–529 ms to first PCM including estimated worker queue
wait (median 444 ms), versus 752 ms for the five original interrupted-output
samples with queue wait included. The original five-target sequence alone had
a 380 ms median; an extra regular seek after the rapid pair took 529 ms.
The rapid pair's forward seek was superseded; its final backward seek took
635 ms including queue wait. There were no unexpected queue advances or forced
decoder kills, and no new source-stream start was observed during the seeks.

The user reported that seeking definitely felt worse. Faster PCM delivery is
therefore not an audible-latency win; the observation is consistent with queued
downstream audio being retained, though its exact buffering delay was not
instrumented.

### Fully cached continuous-output result and conclusion

The complete 31,972,638-byte track fetched in 1,551 ms. Five regular seeks began
with a full cache and took 119–393 ms to first PCM including estimated worker
queue wait (median 165 ms). Both targets of the rapid pair produced PCM, in
134 and 304 ms respectively; unlike the earlier trials, the forward target was
not superseded before producing audio. This can leave intermediate-position
audio queued downstream; its actual audibility was not recorded.

The user reported that it still did not feel very smooth and was unsure which
mode was better. The trials were small, unblinded, ordered rather than
randomized, and used approximate targets with variable spacing. None measured
speaker-audible latency. Faster first PCM alone is not sufficient evidence for
retaining either feature as an improvement.

| Configuration | Regular samples | Median first PCM, including worker queue wait |
| --- | ---: | ---: |
| Direct / interrupted output | 5 | 752 ms |
| Full cache / interrupted output | 5 | 662 ms |
| Direct / continuous output | 6 | 444 ms |
| Full cache / continuous output | 5 | 165 ms |

Restore `media_buffer_enabled: false`, `seek_without_output_stop: false`, and
`media_buffer_prefetch_delay_ms: 0` after evaluation. Retain the committed HTTP
reconnect fix, which was independently verified to recover the actual reset.
That was the initial recommendation, not the final product decision below.
Further audible-latency improvement should investigate output-buffer flushing
and end-to-end measurement, not assume that faster downloading alone solves it.

## User decision after evaluation

Keep fetching the complete current track ahead as normal behavior, subject to
the existing 32 MiB per-receiver memory limit and safe initialization fallback.
This is no longer opt-in; `media_buffer_enabled` is obsolete.

Expose **Stop Sendspin output while seeking** in the web UI and
`stop_receiver_on_seek` in the configuration. Off keeps the stream running
(faster PCM delivery, but potentially sluggish-feeling old queued audio); on
interrupts the output before seeking. Preserve pause, Stop and manual-routing
safety. Persist UI preferences in the writable receiver registry rather than the
read-only config mount. Apply changes to the next seek over manager→bridge→worker
IPC, without restarting receivers or discarding an active track's cache.

The installed configuration retains the last tested preference: **off**, with
current-track fetching enabled and no diagnostic download slowdown. The trials
do not establish that this has better audible latency; the switch explicitly
lets the user choose the tradeoff.
