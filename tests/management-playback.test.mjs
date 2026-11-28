import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:net';
import { spawn } from 'node:child_process';
import { createInterface } from 'node:readline';
import { once } from 'node:events';
import { mkdtemp, symlink, writeFile, readFile, chmod, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { runInNewContext } from 'node:vm';

test('management preference API is CSRF-protected, strict, persistent and does not write config', {
  timeout: 15000
}, async () => {
  const server = createServer();
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  const port = server.address().port;
  await new Promise(resolve => server.close(resolve));
  const dir = await mkdtemp(join(tmpdir(), 'cast2sendspin-playback-'));
  const config = { management_port: port, stop_receiver_on_seek: false };
  await writeFile(join(dir, 'config.json'), JSON.stringify(config));
  await chmod(join(dir, 'config.json'), 0o444);
  await symlink(fileURLToPath(new URL('../src/', import.meta.url)), join(dir, 'src'));
  let child;
  async function start() {
    child = spawn(process.execPath, [fileURLToPath(new URL('../src/manager.mjs', import.meta.url))], {
      cwd: dir, stdio: ['ignore', 'pipe', 'pipe']
    });
    child.stderr.resume();
    const lines = createInterface({ input: child.stdout });
    await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('Manager startup timed out')), 5000);
      child.once('error', reject);
      lines.on('line', line => {
        let event;
        try { event = JSON.parse(line); } catch { return; }
        if (event.event === 'MANAGER_READY') {
          clearTimeout(timer);
          resolve();
        }
      });
    });
  }
  async function stop() {
    if (child && child.exitCode === null && child.signalCode === null) {
      const exited = once(child, 'exit', { signal: AbortSignal.timeout(5000) });
      child.kill('SIGTERM');
      try { await exited; }
      catch { child.kill('SIGKILL'); throw new Error('Manager shutdown timed out'); }
    }
  }
  const base = `http://127.0.0.1:${port}`;
  try {
    // No virtualenv or routes in this private fixture: discovery fails locally
    // before any network/MA/token access. Only management runs.
    await start();
    let state = await (await fetch(`${base}/api/state`)).json();
    assert.equal(state.playback.stop_receiver_on_seek, false);
    const post = (input, csrf = state.csrf, origin) => fetch(`${base}/api/playback`, {
      method: 'POST', headers: {
        'Content-Type': 'application/json', 'X-CSRF-Token': csrf,
        ...(origin ? { Origin: origin } : {})
      }, body: JSON.stringify(input)
    });
    assert.equal((await post({ stop_receiver_on_seek: true }, 'wrong')).status, 403);
    assert.equal((await post({ stop_receiver_on_seek: true }, state.csrf, 'http://attacker.test')).status, 403);
    assert.equal((await post({ stop_receiver_on_seek: 'true' })).status, 400);
    const response = await post({ stop_receiver_on_seek: true });
    assert.equal(response.status, 200);
    state = await response.json();
    assert.equal(state.playback.stop_receiver_on_seek, true);
    assert.equal(state.playback.pending, false);
    const saved = JSON.parse(await readFile(join(dir, '.state/receivers.json'), 'utf8'));
    assert.equal(saved.playback.stop_receiver_on_seek, true);
    assert.deepEqual(JSON.parse(await readFile(join(dir, 'config.json'), 'utf8')), config);
    const html = await (await fetch(base)).text();
    assert.match(html, /Stop Sendspin output while seeking/);
    assert.match(html, /old audio continues to play/);
    await stop();
    await start();
    state = await (await fetch(`${base}/api/state`)).json();
    assert.equal(state.playback.stop_receiver_on_seek, true);
  } finally {
    await stop();
    await rm(dir, { recursive: true, force: true });
  }
});

test('web UI switch renders the saved value and submits a positive boolean with CSRF', async () => {
  const nodes = new Map();
  function node(selector) {
    if (!nodes.has(selector)) nodes.set(selector, {
      dataset: {}, listeners: {}, checked: false, disabled: true,
      addEventListener(event, callback) { this.listeners[event] = callback; },
      setAttribute() {}, replaceChildren() {},
      querySelector() { return node(`${selector}-save`); },
      elements: { namedItem(name) { return node(`${selector}-${name}`); } }
    });
    return nodes.get(selector);
  }
  let state = {
    csrf: 'synthetic-csrf', mode: 'automatic', routes: [], players: [], manual: null,
    playback: { stop_receiver_on_seek: false, pending: false }
  };
  const calls = [];
  runInNewContext(await readFile(new URL('../src/web/app.js', import.meta.url), 'utf8'), {
    document: {
      querySelector: node, activeElement: null,
      querySelectorAll: selector => selector === 'button'
        ? [node('#refresh'), node('#playback-form-save')] : []
    },
    fetch: async (url, input) => {
      calls.push({ url, input });
      if (input?.method === 'POST') state = { ...state, playback: JSON.parse(input.body) };
      return { ok: true, json: async () => state };
    },
    setInterval() {}, confirm() { return true; }
  });
  await new Promise(setImmediate);
  const field = node('#playback-form-stop_receiver_on_seek');
  assert.equal(field.checked, false);
  assert.equal(field.disabled, false);
  field.checked = true;
  node('#playback-form').listeners.submit({
    preventDefault() {}, currentTarget: node('#playback-form')
  });
  await new Promise(setImmediate);
  const submitted = calls.find(call => call.url === '/api/playback');
  assert.deepEqual(JSON.parse(submitted.input.body), { stop_receiver_on_seek: true });
  assert.equal(submitted.input.headers['X-CSRF-Token'], 'synthetic-csrf');
  assert.equal(field.checked, true);
});
