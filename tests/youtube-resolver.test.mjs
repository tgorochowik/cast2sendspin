import test from 'node:test';
import assert from 'node:assert/strict';
import { audioCandidates, credentialContext, resolveSender, validateMediaUrl } from '../src/youtube-resolver.mjs';
import { sandboxEvaluate } from '../src/youtube-sandbox.mjs';

test('missing sender token fails closed before making requests', async () => {
  await assert.rejects(resolveSender({ id: 'ZFZM6jDTWd4' }, () => {
    throw new Error('Should not create a session');
  }), /SENDER_AUTH_REQUIRED/);
  assert.deepEqual(credentialContext('synthetic').credentialTransferTokens,
    [{ scope: 'VIDEO', token: 'synthetic' }]);
});

test('Premium audio wins over shared formats; video and URL-less formats are excluded', () => {
  const formats = audioCandidates({
    playabilityStatus: { status: 'OK' },
    streamingData: { adaptiveFormats: [
      { itag: 140, mimeType: 'audio/mp4', bitrate: 130000, url: 'x' },
      { itag: 141, mimeType: 'audio/mp4', bitrate: 258000, audioQuality: 'AUDIO_QUALITY_HIGH', signatureCipher: 'x' },
      { itag: 774, mimeType: 'audio/webm', averageBitrate: 301000, audioQuality: 'AUDIO_QUALITY_HIGH', signatureCipher: 'x' },
      { itag: 1, mimeType: 'video/mp4', bitrate: 9000000, url: 'x' },
      { itag: 2, mimeType: 'audio/mp4', bitrate: 9000000 }
    ] }
  });
  assert.deepEqual(formats.map(f => f.itag), [774, 141, 140]);
});

test('media URL validation blocks unexpected protocols, hosts and credentials', () => {
  assert.equal(validateMediaUrl('https://r1.googlevideo.com/audio'),
    'https://r1.googlevideo.com/audio');
  for (const url of ['http://r1.googlevideo.com/audio', 'https://googlevideo.com.attacker.test/',
    'https://user:pass@r1.googlevideo.com/', 'https://r1.googlevideo.com:9999/']) {
    assert.throws(() => validateMediaUrl(url));
  }
});

test('resolution uses sender authorization, checks access, and clears credentials', async t => {
  const context = {};
  let request;
  let cancelled = false;
  t.mock.method(globalThis, 'fetch', async () => ({
    ok: true, body: { getReader: () => ({
      async read() { return { value: new Uint8Array([1]) }; },
      async cancel() { cancelled = true; }
    }) }
  }));
  const info = await resolveSender({
    id: 'ZFZM6jDTWd4', context: { ctt: 'synthetic', params: 'context-params' }
  }, async () => ({
    session: { context, player: {
      signature_timestamp: 123,
      async decipher() { return 'https://r1.googlevideo.com/audio'; }
    } },
    actions: { async execute(endpoint, payload) {
      request = payload;
      assert.equal(context.user.credentialTransferTokens[0].token, 'synthetic');
      return { data: {
        playabilityStatus: { status: 'OK' },
        videoDetails: { title: 'Title', lengthSeconds: '100' },
        streamingData: { adaptiveFormats: [
          { itag: 141, mimeType: 'audio/mp4', audioQuality: 'AUDIO_QUALITY_HIGH', signatureCipher: 'cipher' }
        ] }
      } };
    } }
  }));
  assert.equal(info.resolver, 'sender');
  assert.equal(info.itag, 141);
  assert.equal(info.duration, 100);
  assert.equal(request.params, 'context-params');
  assert.equal(request.playbackContext.contentPlaybackContext.signatureTimestamp, 123);
  assert.equal(context.user, undefined);
  assert.equal(cancelled, true);
  assert.ok(!JSON.stringify(info).includes('synthetic'));
});

test('unavailable content clears credentials rather than retrying anonymously', async () => {
  const context = {};
  await assert.rejects(resolveSender({
    id: 'ZFZM6jDTWd4', context: { ctt: 'synthetic' }
  }, async () => ({
    session: { context, player: {} },
    actions: { async execute() { return { data: { playabilityStatus: { status: 'UNPLAYABLE' } } }; } }
  })), /SENDER_CONTENT_UNAVAILABLE/);
  assert.equal(context.user, undefined);
});

test('decipher sandbox exposes no process, network or filesystem APIs', async () => {
  assert.deepEqual(await sandboxEvaluate({
    output: 'return {process: typeof process, fetch: typeof fetch, require: typeof require};'
  }), { process: 'undefined', fetch: 'undefined', require: 'undefined' });
});
