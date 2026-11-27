import { pathToFileURL } from 'node:url';
import Innertube from 'youtubei.js';
import './youtube-sandbox.mjs';

export function credentialContext(token) {
  if (typeof token !== 'string' || !token || token.length > 24000) {
    throw new Error('SENDER_AUTH_REQUIRED');
  }
  return {
    enableSafetyMode: false, lockedSafetyMode: false,
    credentialTransferTokens: [{ scope: 'VIDEO', token }]
  };
}

export function audioCandidates(data) {
  if (data.playabilityStatus?.status !== 'OK') throw new Error('SENDER_CONTENT_UNAVAILABLE');
  const formats = [
    ...(data.streamingData?.adaptiveFormats || []),
    ...(data.streamingData?.formats || [])
  ].filter(f => f.mimeType?.startsWith('audio/') && (f.url || f.signatureCipher || f.cipher));
  // Prefer Premium High formats; then the highest average audio bitrate.
  return formats.sort((a, b) =>
    Number(b.audioQuality === 'AUDIO_QUALITY_HIGH') - Number(a.audioQuality === 'AUDIO_QUALITY_HIGH')
    || (b.averageBitrate || b.bitrate || 0) - (a.averageBitrate || a.bitrate || 0)
  );
}

export function validateMediaUrl(value) {
  const url = new URL(value);
  if (url.protocol !== 'https:' || !url.hostname.endsWith('.googlevideo.com')
      || url.username || url.password || (url.port && url.port !== '443')) {
    throw new Error('INVALID_MEDIA_URL');
  }
  return url.toString();
}

export async function resolveSender(
  video, create = options => Innertube.create(options), onTiming = () => {}
) {
  const started = performance.now();
  const timing = { init_ms: 0, player_ms: 0, decipher_ms: 0, access_ms: 0 };
  const user = credentialContext(video.context?.ctt);
  if (typeof video.id !== 'string' || !/^[A-Za-z0-9_-]{11}$/.test(video.id)) {
    throw new Error('INVALID_VIDEO_ID');
  }
  let tube;
  try {
    tube = await create({
      retrieve_player: true, enable_session_cache: false,
      fetch: (input, init) => fetch(input, { ...init, signal: AbortSignal.timeout(15000) })
    });
    timing.init_ms = Math.round(performance.now() - started);
    tube.session.context.user = user;
    const playerStarted = performance.now();
    const response = await tube.actions.execute('/player', {
      videoId: video.id, client: 'YTMUSIC',
      ...(typeof video.context?.params === 'string' ? { params: video.context.params } : {}),
      contentCheckOk: true, racyCheckOk: true,
      playbackContext: {
        contentPlaybackContext: { signatureTimestamp: tube.session.player?.signature_timestamp }
      }
    });
    timing.player_ms = Math.round(performance.now() - playerStarted);
    const data = response.data;
    const candidates = audioCandidates(data);
    for (const format of candidates) {
      try {
        const decipherStarted = performance.now();
        const url = validateMediaUrl(await tube.session.player.decipher(
          format.url, format.signatureCipher, format.cipher
        ));
        timing.decipher_ms += Math.round(performance.now() - decipherStarted);
        // Check access before claiming an MA source. No credential substitution.
        const accessStarted = performance.now();
        let reader;
        try {
          const check = await fetch(url, {
            headers: { Range: 'bytes=0-4095' },
            signal: AbortSignal.timeout(10000), redirect: 'error'
          });
          reader = check.body?.getReader();
          if (!check.ok) continue;
          const chunk = await reader?.read();
          if (!chunk?.value?.length) continue;
        } finally {
          try { await reader?.cancel(); }
          finally { timing.access_ms += Math.round(performance.now() - accessStarted); }
        }
        return {
          url, title: data.videoDetails?.title,
          duration: Number(data.videoDetails?.lengthSeconds || 0),
          resolver: 'sender', itag: format.itag,
          bitrate: format.averageBitrate || format.bitrate || 0
        };
      } catch { /* Try another audio format under the SAME sender credential. */ }
    }
    throw new Error('SENDER_AUDIO_UNAVAILABLE');
  } finally {
    if (tube) delete tube.session.context.user;
    delete user.credentialTransferTokens;
    onTiming({ ...timing, total_ms: Math.round(performance.now() - started) });
  }
}

export function createSenderResolver({
  create = options => Innertube.create(options),
  maxAgeMs = 30 * 60 * 1000,
  now = () => performance.now()
} = {}) {
  let tube, initializedAt = 0;
  let serial = Promise.resolve();
  const options = {
    retrieve_player: true, enable_session_cache: false,
    fetch: (input, init) => fetch(input, { ...init, signal: AbortSignal.timeout(15000) })
  };
  async function session() {
    if (!tube || now() - initializedAt >= maxAgeMs) {
      tube = undefined;
      tube = await create(options);
      initializedAt = now();
    }
    return tube;
  }
  function enqueue(action) {
    const result = serial.then(action);
    // The queue tail must not retain the last signed media URL/result.
    serial = result.then(() => {}, () => {});
    return result;
  }
  return {
    warmup: () => enqueue(async () => { await session(); }),
    resolve: video => enqueue(async () => {
      const cacheHit = Boolean(tube && now() - initializedAt < maxAgeMs);
      let timings;
      try {
        const info = await resolveSender(video, session, value => { timings = value; });
        return { info, timings: { ...timings, cache_hit: cacheHit } };
      } catch (error) {
        // Reinitialize on the next request after failure (including stale player
        // scripts), but never replay requests or substitute another credential.
        tube = undefined;
        throw error;
      }
    })
  };
}

export async function serve(input, output, resolver = createSenderResolver()) {
  let pending = Buffer.alloc(0);
  for await (const chunk of input) {
    pending = Buffer.concat([pending, Buffer.from(chunk)]);
    let newline;
    while ((newline = pending.indexOf(10)) !== -1) {
      if (newline > 49152) throw new Error('REQUEST_TOO_LARGE');
      let request;
      let response;
      const line = pending.subarray(0, newline).toString('utf8');
      pending = Buffer.from(pending.subarray(newline + 1));
      try {
        request = JSON.parse(line);
        if (request.op === 'warmup') {
          await resolver.warmup();
          response = { ready: true };
        } else if (request.op === 'resolve') {
          response = await resolver.resolve(request);
        } else {
          throw new Error('INVALID_REQUEST');
        }
      } catch {
        // No raw exceptions: upstream errors can contain signed URLs or tokens.
        response = { error: 'SENDER_RESOLUTION_FAILED' };
      } finally {
        if (request?.context) delete request.context.ctt;
      }
      output.write(JSON.stringify({ request_id: request?.request_id, ...response }) + '\n');
    }
    if (pending.length > 49152) throw new Error('REQUEST_TOO_LARGE');
  }
  if (pending.length) throw new Error('INCOMPLETE_REQUEST');
}

async function main() {
  process.umask(0o077);
  if (process.argv.includes('--serve')) {
    await serve(process.stdin, process.stdout);
    return;
  }
  let input = '';
  for await (const chunk of process.stdin) {
    input += chunk;
    if (Buffer.byteLength(input) > 49152) throw new Error('REQUEST_TOO_LARGE');
  }
  const video = JSON.parse(input);
  input = '';
  try {
    const info = await resolveSender(video);
    process.stdout.write(JSON.stringify({ info }));
  } finally {
    if (video.context) delete video.context.ctt;
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch(() => {
    // Never print raw upstream exception messages, URLs, or request bodies.
    process.stdout.write(JSON.stringify({ error: 'SENDER_RESOLUTION_FAILED' }));
    process.exitCode = 1;
  });
}
