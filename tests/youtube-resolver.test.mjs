import test from 'node:test';
import assert from 'node:assert/strict';
import { Readable } from 'node:stream';
import {
  audioCandidates, credentialContext, createSenderResolver,
  resolveSender, serve, validateMediaUrl
} from '../src/youtube-resolver.mjs';
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

function mockAudioAccess(t) {
  t.mock.method(globalThis, 'fetch', async () => ({
    ok: true, body: { getReader: () => ({
      async read() { return { value: new Uint8Array(4096) }; },
      async cancel() {}
    }) }
  }));
}

function fakeTube(onPlayer = () => {}) {
  const tube = {
    session: { context: {}, player: {
      signature_timestamp: 123,
      async decipher() { return 'https://r1.googlevideo.com/audio'; }
    } },
    actions: { async execute() {
      await onPlayer(tube.session.context);
      return { data: {
        playabilityStatus: { status: 'OK' },
        streamingData: { adaptiveFormats: [
          { itag: 774, mimeType: 'audio/webm', url: 'https://r1.googlevideo.com/audio' }
        ] }
      } };
    } }
  };
  return tube;
}

test('warmup and multiple songs reuse initialization but not sender credentials', async t => {
  mockAudioAccess(t);
  let created = 0;
  const senders = [];
  const tube = fakeTube(context => senders.push(context.user.credentialTransferTokens[0].token));
  const resolver = createSenderResolver({ create: async () => { created++; return tube; } });
  await resolver.warmup();
  const first = await resolver.resolve({ id: 'ZFZM6jDTWd4', context: { ctt: 'sender-one' } });
  const second = await resolver.resolve({ id: 'nwWVxPaOdN4', context: { ctt: 'sender-two' } });
  assert.equal(created, 1);
  assert.deepEqual(senders, ['sender-one', 'sender-two']);
  assert.equal(tube.session.context.user, undefined);
  assert.equal(first.timings.cache_hit, true);
  assert.equal(second.timings.cache_hit, true);
  assert.ok(Number.isInteger(second.timings.player_ms));
  assert.ok(!JSON.stringify(second).includes('sender-'));
});

test('concurrent requests serialize credential-bearing player calls', async t => {
  mockAudioAccess(t);
  let release;
  const gate = new Promise(resolve => { release = resolve; });
  const seen = [];
  const tube = fakeTube(async context => {
    const token = context.user.credentialTransferTokens[0].token;
    seen.push(token);
    if (token === 'one') {
      await gate;
      assert.equal(context.user.credentialTransferTokens[0].token, 'one');
    }
  });
  const resolver = createSenderResolver({ create: async () => tube });
  const first = resolver.resolve({ id: 'ZFZM6jDTWd4', context: { ctt: 'one' } });
  const second = resolver.resolve({ id: 'nwWVxPaOdN4', context: { ctt: 'two' } });
  release();
  await Promise.all([first, second]);
  assert.deepEqual(seen, ['one', 'two']);
  assert.equal(tube.session.context.user, undefined);
});

test('failed requests clear credentials and reinitialize for the next request', async t => {
  mockAudioAccess(t);
  const failed = fakeTube(() => { throw new Error('upstream-token-secret'); });
  const good = fakeTube();
  const tubes = [failed, good];
  let created = 0;
  const resolver = createSenderResolver({ create: async () => tubes[created++] });
  await assert.rejects(resolver.resolve({ id: 'ZFZM6jDTWd4', context: { ctt: 'one' } }));
  assert.equal(failed.session.context.user, undefined);
  await resolver.resolve({ id: 'nwWVxPaOdN4', context: { ctt: 'two' } });
  assert.equal(created, 2);
  assert.equal(good.session.context.user, undefined);
});

test('warm session expires and failed initialization can be retried', async () => {
  let time = 0, created = 0;
  const resolver = createSenderResolver({
    now: () => time, maxAgeMs: 100,
    create: async () => {
      created++;
      if (created === 1) throw new Error('initialization failed');
      return fakeTube();
    }
  });
  await assert.rejects(resolver.warmup());
  await resolver.warmup();
  await resolver.warmup();
  assert.equal(created, 2);
  time = 101;
  await resolver.warmup();
  assert.equal(created, 3);
});

test('persistent protocol handles multiple and malformed lines without leaking errors', async () => {
  const writes = [], requests = [];
  const resolver = {
    async warmup() {},
    async resolve(request) {
      requests.push(request);
      if (request.id === 'fail') throw new Error('synthetic-secret signed-url');
      return { info: { resolver: 'sender' } };
    }
  };
  await serve(Readable.from([
    '{"op":"warmup","request_id":1}\ninvalid\nnull\n',
    '{"op":"resolve","id":"fail","request_id":2,"context":{"ctt":"secret"}}\n',
    '{"op":"resolve","id":"ok","request_id":3,"context":{"ctt":"another"}}\n'
  ]), { write: line => writes.push(line) }, resolver);
  const responses = writes.map(line => JSON.parse(line));
  assert.equal(responses[0].ready, true);
  assert.equal(responses[3].request_id, 2);
  assert.equal(responses[3].error, 'SENDER_RESOLUTION_FAILED');
  assert.equal(responses[4].request_id, 3);
  assert.equal(responses[4].info.resolver, 'sender');
  assert.ok(!writes.join('').includes('secret'));
  assert.ok(requests.every(request => request.context.ctt === undefined));
});

test('persistent protocol bounds unterminated and oversized requests', async () => {
  for (const input of ['x'.repeat(49153), 'x'.repeat(49153) + '\n']) {
    await assert.rejects(serve(Readable.from([input]), { write() {} }), /REQUEST_TOO_LARGE/);
  }
  await assert.rejects(serve(Readable.from(['{"op":']), { write() {} }), /INCOMPLETE_REQUEST/);
});
