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

export async function resolveSender(video, create = options => Innertube.create(options)) {
  const user = credentialContext(video.context?.ctt);
  if (typeof video.id !== 'string' || !/^[A-Za-z0-9_-]{11}$/.test(video.id)) {
    throw new Error('INVALID_VIDEO_ID');
  }
  const tube = await create({
    retrieve_player: true, enable_session_cache: false,
    fetch: (input, init) => fetch(input, { ...init, signal: AbortSignal.timeout(15000) })
  });
  try {
    tube.session.context.user = user;
    const response = await tube.actions.execute('/player', {
      videoId: video.id, client: 'YTMUSIC',
      ...(typeof video.context?.params === 'string' ? { params: video.context.params } : {}),
      contentCheckOk: true, racyCheckOk: true,
      playbackContext: {
        contentPlaybackContext: { signatureTimestamp: tube.session.player?.signature_timestamp }
      }
    });
    const data = response.data;
    const candidates = audioCandidates(data);
    for (const format of candidates) {
      try {
        const url = validateMediaUrl(await tube.session.player.decipher(
          format.url, format.signatureCipher, format.cipher
        ));
        // Check access before claiming an MA source. No credential substitution.
        const check = await fetch(url, {
          headers: { Range: 'bytes=0-4095' },
          signal: AbortSignal.timeout(10000), redirect: 'error'
        });
        const reader = check.body?.getReader();
        try {
          if (!check.ok) continue;
          const chunk = await reader?.read();
          if (!chunk?.value?.length) continue;
        } finally {
          await reader?.cancel();
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
    delete tube.session.context.user;
    delete user.credentialTransferTokens;
  }
}

async function main() {
  process.umask(0o077);
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
