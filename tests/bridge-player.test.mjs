import test from 'node:test';
import assert from 'node:assert/strict';
import { BridgePlayer } from '../src/bridge-player.mjs';

function fixture() {
  const calls = [];
  const worker = {
    state: { position: 10, duration: 200, volume: { level: 24, muted: false } },
    async call(method) { calls.push(method); return this.state; }
  };
  const player = new BridgePlayer(worker);
  player.setLogger({ info() {}, debug() {}, warn() {}, error() {} });
  player.queue.setRequestHandler({ reset() {} });
  return { player, calls };
}

test('changing track does not release the MA input or cycle power', async () => {
  const { player, calls } = fixture();
  await player.play({ id: 'Kf5pXDhx5Vc' }, 0);
  calls.length = 0;
  await player.play({ id: 'enPg09IpUjU' }, 0);
  assert.deepEqual(calls, ['track_stop', 'play']);
});

test('explicit stop releases the casting session', async () => {
  const { player, calls } = fixture();
  await player.play({ id: 'Kf5pXDhx5Vc' }, 0);
  calls.length = 0;
  await player.stop();
  assert.deepEqual(calls, ['finish']);
});

test('setPlaylist stop before play keeps the MA input selected', async () => {
  const { player, calls } = fixture();
  await player.play({ id: 'Kf5pXDhx5Vc' }, 0);
  calls.length = 0;
  player.queue.setAsCurrent({ id: 'enPg09IpUjU' });
  await player.stop();
  assert.deepEqual(calls, ['track_stop']);
  await player.play({ id: 'enPg09IpUjU' }, 0);
  calls.length = 0;
  await player.stop();
  assert.deepEqual(calls, ['finish']);
});

test('a repeated video at a new queue index is still a track replacement', async () => {
  const { player, calls } = fixture();
  await player.play({ id: 'Kf5pXDhx5Vc', context: { index: 0 } }, 0);
  calls.length = 0;
  player.queue.setAsCurrent({ id: 'Kf5pXDhx5Vc', context: { index: 1 } });
  // Playlist.setAsCurrent deliberately ignores identical IDs; use its public
  // snapshot only in a stub here to exercise repeated-video queue selections.
  Object.defineProperty(player.queue, 'current', {
    get: () => ({ id: 'Kf5pXDhx5Vc', context: { index: 1 } })
  });
  await player.stop();
  assert.deepEqual(calls, ['track_stop']);
});

test('reset while idle still cancels pending worker loads', async () => {
  const { player, calls } = fixture();
  await player.reset();
  assert.deepEqual(calls, ['finish']);
});

test('yielding to MA does not stop the new owner', async () => {
  const { player, calls } = fixture();
  await player.play({ id: 'Kf5pXDhx5Vc' }, 0);
  calls.length = 0;
  await player.yieldToMA();
  assert.deepEqual(calls, []);
});

test('sender credentials and params reach the worker, without unrelated session fields', async () => {
  let request;
  const player = new BridgePlayer({
    async call(method, args) { request = { method, args }; }
  });
  assert.equal(await player.doPlay({
    id: 'ZFZM6jDTWd4', context: { ctt: 'synthetic-token', params: 'params', unrelated: 'omit' }
  }, 12), true);
  assert.deepEqual(request, {
    method: 'play', args: {
      video_id: 'ZFZM6jDTWd4', position: 12,
      context: { ctt: 'synthetic-token', params: 'params' }
    }
  });
});
