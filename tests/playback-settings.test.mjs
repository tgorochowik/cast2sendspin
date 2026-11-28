import test from 'node:test';
import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import {
  playbackSettings, setPlaybackSettings, configFingerprint,
  requestPlaybackSettings, bindPlaybackSettings
} from '../src/playback-settings.mjs';
import { validateRegistry } from '../src/registry.mjs';

test('UI preference overrides the config default and legacy inverse migrates', () => {
  assert.deepEqual(playbackSettings({}, { seek_without_output_stop: true }),
    { stop_receiver_on_seek: false });
  assert.deepEqual(playbackSettings({}, { stop_receiver_on_seek: true, seek_without_output_stop: true }),
    { stop_receiver_on_seek: true });
  const registry = { version: 1, routes: [] };
  setPlaybackSettings(registry, { stop_receiver_on_seek: false });
  assert.deepEqual(playbackSettings(registry, { stop_receiver_on_seek: true }),
    { stop_receiver_on_seek: false });
  validateRegistry(registry);
});

test('invalid values neither mutate preferences nor pass registry validation', () => {
  const registry = { version: 1, routes: [], playback: { stop_receiver_on_seek: false } };
  for (const value of [undefined, null, 'true', 1, {}]) {
    assert.throws(() => setPlaybackSettings(registry, { stop_receiver_on_seek: value }), /boolean/);
    assert.equal(registry.playback.stop_receiver_on_seek, false);
    assert.throws(() => validateRegistry({ ...registry, playback: { stop_receiver_on_seek: value } }));
  }
});

test('only live seek preferences are excluded from the receiver restart fingerprint', () => {
  const config = { receiver_name: 'Speaker', stop_receiver_on_seek: true, seek_without_output_stop: false };
  assert.equal(configFingerprint(config), configFingerprint({
    ...config, stop_receiver_on_seek: false, seek_without_output_stop: true
  }));
  assert.notEqual(configFingerprint(config), configFingerprint({ ...config, receiver_name: 'Renamed' }));
});

test('manager IPC applies the preference to the worker and waits for acknowledgment', async () => {
  const parent = new EventEmitter();
  const child = new EventEmitter();
  parent.send = (message, callback) => { queueMicrotask(() => child.emit('message', message)); callback(); };
  child.send = (message, callback) => { parent.emit('message', message); callback(); };
  const calls = [];
  const unbind = bindPlaybackSettings(child, {
    async call(method, args) { calls.push({ method, args }); }
  });
  await requestPlaybackSettings(parent, { stop_receiver_on_seek: true });
  assert.deepEqual(calls, [{ method: 'configure_seek', args: { stop_receiver_on_seek: true } }]);
  assert.equal(parent.listenerCount('message'), 0);
  assert.equal(parent.listenerCount('exit'), 0);
  unbind();
});

test('failed worker update and missing acknowledgment are bounded and sanitized', async () => {
  const parent = new EventEmitter();
  const child = new EventEmitter();
  parent.send = (message, callback) => { queueMicrotask(() => child.emit('message', message)); callback(); };
  child.send = (message, callback) => { parent.emit('message', message); callback(); };
  const unbind = bindPlaybackSettings(child, {
    async call() { throw new Error('private upstream details'); }
  });
  await assert.rejects(requestPlaybackSettings(parent, { stop_receiver_on_seek: false }),
    /^Error: Receiver settings not acknowledged$/);
  unbind();
  await assert.rejects(requestPlaybackSettings(parent, { stop_receiver_on_seek: false }, 10),
    /not acknowledged/);
  assert.equal(parent.listenerCount('message'), 0);
  assert.equal(parent.listenerCount('exit'), 0);
});
