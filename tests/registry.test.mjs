import test from 'node:test';
import assert from 'node:assert/strict';
import {
  addRoute, cleanName, initialRegistry, validateRegistry, ensureManualRoute,
  setRoutingMode, desiredRoutes
} from '../src/registry.mjs';

test('switching modes preserves speaker settings and selects exactly one manual route', () => {
  const registry = initialRegistry({});
  const route = addRoute(registry, { player_id: 'speaker' }, 'Speaker');
  const saved = structuredClone(route);
  setRoutingMode(registry, 'manual');
  const manual = registry.manual;
  assert.deepEqual(desiredRoutes(registry, []), [manual]);
  assert.deepEqual(registry.routes[0], saved);
  setRoutingMode(registry, 'automatic');
  assert.deepEqual(desiredRoutes(registry, [{ player_id: 'speaker', eligible: true }]), [route]);
  setRoutingMode(registry, 'manual');
  assert.equal(registry.manual, manual);
  validateRegistry(registry);
});

test('manual port is reserved and disabled manual mode advertises nothing', () => {
  const registry = initialRegistry({});
  const manual = ensureManualRoute(registry);
  const route = addRoute(registry, { player_id: 'speaker' }, 'Speaker');
  assert.notEqual(manual.port, route.port);
  setRoutingMode(registry, 'manual');
  manual.enabled = false;
  assert.deepEqual(desiredRoutes(registry, []), []);
  manual.enabled = true;
  assert.deepEqual(desiredRoutes(registry, [], 3), []);
  validateRegistry(registry);
});

test('invalid modes do not change the registry', () => {
  const registry = initialRegistry({});
  const before = structuredClone(registry);
  assert.throws(() => setRoutingMode(registry, 'invalid'));
  assert.deepEqual(registry, before);
});

test('a fresh server starts with no enabled receivers', () => {
  const registry = initialRegistry({ ma_url: 'http://ma:8095', dial_interface: 'eth0' });
  assert.deepEqual(registry.routes, []);
  validateRegistry(registry);
});

test('existing single-receiver configuration migrates without losing identity', () => {
  const id = '12345678-1234-1234-1234-123456789abc';
  const registry = initialRegistry({
    target_player_id: 'speaker', receiver_name: 'Existing', dial_port: 8099
  }, id);
  validateRegistry(registry);
  assert.equal(registry.routes[0].dial_uuid, id);
  assert.equal(registry.routes[0].state_dir, '.state');
  assert.equal(registry.routes[0].enabled, true);
});

test('new routes have isolated persistent identities, state and ports', () => {
  const registry = { version: 1, routes: [] };
  const first = addRoute(registry, { player_id: 'one' }, 'One');
  const second = addRoute(registry, { player_id: 'two' }, 'Two');
  assert.notEqual(first.dial_uuid, second.dial_uuid);
  assert.notEqual(first.state_dir, second.state_dir);
  assert.notEqual(first.port, second.port);
  validateRegistry(registry);
  const original = structuredClone(first);
  first.name = 'Renamed';
  first.enabled = false;
  validateRegistry(registry);
  assert.equal(first.dial_uuid, original.dial_uuid);
  assert.equal(first.port, original.port);
  assert.equal(first.state_dir, original.state_dir);
});

test('disabled receiver ports stay reserved', () => {
  const registry = { version: 1, routes: [] };
  const first = addRoute(registry, { player_id: 'one' }, 'One');
  first.enabled = false;
  const second = addRoute(registry, { player_id: 'two' }, 'Two');
  assert.notEqual(first.port, second.port);
});

test('duplicate players cannot get competing receivers', () => {
  const registry = { version: 1, routes: [] };
  addRoute(registry, { player_id: 'one' }, 'One');
  assert.throws(() => addRoute(registry, { player_id: 'one' }, 'Other'));
});

test('registry rejects state path traversal and duplicate ports', () => {
  const registry = { version: 1, routes: [] };
  const one = addRoute(registry, { player_id: 'one' }, 'One');
  const two = addRoute(registry, { player_id: 'two' }, 'Two');
  two.port = one.port;
  assert.throws(() => validateRegistry(registry));
  two.port = one.port + 1;
  two.state_dir = '../../escape';
  assert.throws(() => validateRegistry(registry));
});

test('receiver names are nonempty and bounded', () => {
  assert.equal(cleanName(' Kitchen '), 'Kitchen');
  for (const value of ['', ' ', 'x'.repeat(81), 'bad\nname', null]) {
    assert.throws(() => cleanName(value));
  }
});
