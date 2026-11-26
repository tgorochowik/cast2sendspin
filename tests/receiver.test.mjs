import test from 'node:test';
import assert from 'node:assert/strict';
import dial from '@patrickkfkan/peer-dial';
import { createReceiver } from '../src/receiver.mjs';

test('persistent UUID is passed to DIAL and constructor is restored', () => {
  const original = dial.Server;
  class FakeServer { constructor(options) { this.uuid = options.uuid; } }
  dial.Server = FakeServer;
  try {
    class FakeReceiver {
      constructor() { this.server = new dial.Server({ uuid: 'ignored' }); }
    }
    const receiver = createReceiver({}, {}, 'persistent-id', FakeReceiver);
    assert.equal(receiver.server.uuid, 'persistent-id');
    assert.equal(dial.Server, FakeServer);
  } finally { dial.Server = original; }
});

test('DIAL constructor is restored if receiver construction throws', () => {
  const original = dial.Server;
  class Broken { constructor() { throw new Error('broken'); } }
  assert.throws(() => createReceiver({}, {}, 'id', Broken));
  assert.equal(dial.Server, original);
});
