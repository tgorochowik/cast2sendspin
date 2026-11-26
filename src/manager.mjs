import { createServer } from 'node:http';
import { spawn, execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { readFile, writeFile, rename, mkdir, appendFile } from 'node:fs/promises';
import { randomBytes, randomUUID } from 'node:crypto';
import { createInterface } from 'node:readline';
import { once } from 'node:events';
import {
  addRoute, cleanName, initialRegistry, validateRegistry, ensureManualRoute,
  setRoutingMode, desiredRoutes
} from './registry.mjs';

process.umask(0o077);
const exec = promisify(execFile);
const config = JSON.parse(await readFile('config.json', 'utf8'));
const host = config.management_host || '127.0.0.1';
const port = config.management_port || 8790;
if (!Number.isInteger(port) || port < 1024 || port > 65535) {
  throw new Error('Management port must be an integer between 1024 and 65535');
}
// No auth was requested for management; bind only to loopback. This also keeps
// other LAN users from taking over speakers through the management API.
if (!['127.0.0.1', '::1', 'localhost'].includes(host)) {
  throw new Error('Management must bind to loopback until authentication is implemented');
}
const csrf = randomBytes(32).toString('hex');
await mkdir('.state', { recursive: true, mode: 0o700 });
const registryPath = '.state/receivers.json';
let registry;
try {
  registry = JSON.parse(await readFile(registryPath, 'utf8'));
} catch (error) {
  if (error.code !== 'ENOENT') throw error;
  let uuid = randomUUID();
  try {
    // Preserve the currently advertised legacy UDN during migration.
    const response = await fetch(`http://127.0.0.1:${config.dial_port}/ytcr/ssdp/device-desc.xml`,
      { signal: AbortSignal.timeout(2000) });
    const match = (await response.text()).match(/<UDN>uuid:([a-f0-9-]{36})<\/UDN>/);
    if (match) uuid = match[1];
  } catch {}
  registry = initialRegistry(config, uuid);
}
validateRegistry(registry);
async function save() {
  validateRegistry(registry);
  await writeFile(`${registryPath}.tmp`, JSON.stringify(registry, null, 2) + '\n', { mode: 0o600 });
  await rename(`${registryPath}.tmp`, registryPath);
}
await save();
if (process.argv.includes('--init')) {
  console.log('Receiver registry initialized; legacy identity retained.');
  process.exit(0);
}

const children = new Map();
const retryAfter = new Map();
let players = [], updatedAt = null, discoveryError = null, failures = 0;
let stopping = false, serial = Promise.resolve();
function enqueue(action) {
  const next = serial.then(action);
  serial = next.catch(() => {});
  return next;
}
async function log(event, details = {}) {
  const line = JSON.stringify({ time: new Date().toISOString(), event, ...details });
  console.log(line);
  await appendFile(process.env.MANAGER_LOG_FILE || '.state/manager.log', line + '\n', { mode: 0o600 });
}

function effectiveConfig(route) {
  const manual = route.player_id === '__manual_source__';
  const p = players.find(p => p.player_id === route.player_id);
  return {
    ...config, routing_mode: manual ? 'manual' : 'automatic',
    target_player_id: manual ? null : route.player_id,
    target_player_name: manual ? 'Manual routing in Music Assistant' : (p?.name || route.name),
    receiver_name: route.name,
    dial_port: route.port, dial_uuid: route.dial_uuid, state_dir: route.state_dir,
    source_name: manual ? route.name : (
      route.id === 'legacy' ? 'YouTube Music Cast Input' : `YouTube Cast · ${route.name}`)
  };
}

async function stopChild(id) {
  const entry = children.get(id);
  if (!entry) return;
  entry.stopping = true;
  if (entry.child.exitCode !== null || entry.child.signalCode !== null) {
    children.delete(id);
    return;
  }
  const exited = once(entry.child, 'exit').catch(() => {});
  entry.child.kill('SIGTERM');
  const timeout = setTimeout(() => {
    // Kill only this child process group if graceful shutdown gets stuck.
    try { process.kill(-entry.child.pid, 'SIGKILL'); } catch {}
  }, 20000);
  await exited;
  clearTimeout(timeout);
  if (children.get(id) === entry) children.delete(id);
  await log('RECEIVER_STOPPED', { id });
}

async function startChild(route) {
  const childConfig = effectiveConfig(route);
  const fingerprint = JSON.stringify(childConfig);
  const path = `.state/routes/${route.id}/config.json`;
  await mkdir(`.state/routes/${route.id}`, { recursive: true, mode: 0o700 });
  await writeFile(path, JSON.stringify(childConfig, null, 2), { mode: 0o600 });
  const child = spawn(process.execPath, ['src/bridge.mjs'], {
    env: {
      ...process.env, BRIDGE_CONFIG: path,
      BRIDGE_STATE_DIR: route.state_dir,
      BRIDGE_LOG: route.id === 'legacy'
        ? (process.env.LEGACY_BRIDGE_LOG || '.state/bridge.log')
        : `${route.state_dir}/bridge.log`
    },
    stdio: ['ignore', 'pipe', 'pipe', 'ipc'], detached: true
  });
  const entry = { child, status: 'starting', fingerprint, stopping: false };
  children.set(route.id, entry);
  child.stderr.on('data', () => {}); // Never forward raw upstream output.
  child.on('error', () => { entry.status = 'error'; });
  createInterface({ input: child.stdout }).on('line', line => {
    let data;
    try { data = JSON.parse(line); } catch { return; }
    if (data.event === 'READY' && data.name) {
      entry.status = 'running';
      retryAfter.delete(route.id);
      void log('RECEIVER_READY', { id: route.id, name: route.name, port: route.port });
    } else if (data.event === 'STARTUP_FAILED' || data.event === 'FATAL') {
      entry.status = 'error';
    }
  });
  child.on('exit', code => {
    if (children.get(route.id) !== entry) return;
    children.delete(route.id);
    if (!entry.stopping && !stopping) {
      retryAfter.set(route.id, Date.now() + 30000);
      void log('RECEIVER_EXIT', { id: route.id, code });
    }
  });
}

async function reconcile() {
  const wanted = desiredRoutes(registry, players, failures);
  for (const id of [...children.keys()]) {
    if (!wanted.some(route => route.id === id)) await stopChild(id);
  }
  for (const route of wanted) {
    if (stopping) return;
    const entry = children.get(route.id);
    if (entry && entry.fingerprint !== JSON.stringify(effectiveConfig(route))) {
      await stopChild(route.id);
    }
    if (!children.has(route.id) && Date.now() >= (retryAfter.get(route.id) || 0)) {
      await startChild(route);
    }
  }
}

async function refresh() {
  try {
    const args = ['src/inventory.py'];
    if (registry.mode === 'manual') args.push('--connection-only');
    const { stdout } = await exec('.venv/bin/python', args, {
      timeout: 45000, maxBuffer: 1024 * 1024
    });
    const inventory = JSON.parse(stdout);
    if (!Array.isArray(inventory.players)) throw new Error('Invalid inventory');
    players = inventory.players;
    updatedAt = new Date().toISOString();
    discoveryError = null;
    failures = 0;
  } catch {
    failures++;
    discoveryError = registry.mode === 'manual'
      ? 'Music Assistant connection is unavailable.'
      : 'MA discovery is unavailable. New receivers cannot be enabled.';
  }
  await reconcile();
}

function state() {
  const manual = registry.mode === 'manual';
  const summary = route => {
    const p = players.find(p => p.player_id === route.player_id);
    const manualRoute = route.player_id === '__manual_source__';
    return {
      id: route.id, player_id: route.player_id, name: route.name,
      enabled: route.enabled, port: route.port,
      status: children.get(route.id)?.status || (
        !route.enabled ? 'disabled' : (manualRoute !== manual) ? 'inactive mode'
        : discoveryError ? 'discovery unavailable'
        : !manualRoute && !p?.eligible ? 'unavailable or excluded' : 'retrying'
      )
    };
  };
  return {
    csrf, mode: manual ? 'manual' : 'automatic',
    updatedAt, discoveryError, players: manual ? [] : players,
    routes: manual ? [] : registry.routes.map(summary),
    manual: registry.manual ? summary(registry.manual) : null,
  };
}

function json(response, status, body) {
  response.writeHead(status, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' });
  response.end(JSON.stringify(body));
}
const assets = new Map([
  ['/', ['src/web/index.html', 'text/html; charset=utf-8']],
  ['/app.js', ['src/web/app.js', 'text/javascript; charset=utf-8']],
  ['/style.css', ['src/web/style.css', 'text/css; charset=utf-8']],
  ['/icon.svg', ['src/web/icon.svg', 'image/svg+xml']]
]);
const allowedHosts = new Set([`localhost:${port}`, `127.0.0.1:${port}`, `[::1]:${port}`]);
const server = createServer(async (request, response) => {
  response.setHeader('X-Content-Type-Options', 'nosniff');
  response.setHeader('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'");
  if (!allowedHosts.has(request.headers.host)) return json(response, 403, { error: 'Invalid Host' });
  if (request.headers.origin && request.headers.origin !== `http://${request.headers.host}`) {
    return json(response, 403, { error: 'Cross-origin access denied' });
  }
  try {
    const path = new URL(request.url, `http://${request.headers.host}`).pathname;
    if (request.method === 'GET' && path === '/api/state') return json(response, 200, state());
    if (request.method === 'GET' && assets.has(path)) {
      const [file, type] = assets.get(path);
      response.writeHead(200, { 'Content-Type': type, 'Cache-Control': 'no-store' });
      return response.end(await readFile(file));
    }
    if (request.method !== 'POST') return json(response, 404, { error: 'Not found' });
    if (request.headers['x-csrf-token'] !== csrf
        || !request.headers['content-type']?.startsWith('application/json')) {
      return json(response, 403, { error: 'Invalid management request' });
    }
    let body = '';
    for await (const chunk of request) {
      body += chunk;
      if (body.length > 8192) return json(response, 413, { error: 'Request too large' });
    }
    const input = JSON.parse(body || '{}');
    if (path === '/api/refresh') {
      await enqueue(refresh);
      return json(response, 200, state());
    }
    if (path === '/api/mode') {
      await enqueue(async () => {
        setRoutingMode(registry, input.mode);
        retryAfter.clear();
        await save();
        await refresh();
      });
      return json(response, 200, state());
    }
    if (path === '/api/manual') {
      await enqueue(async () => {
        if (typeof input.enabled !== 'boolean') throw new Error('Enabled state is required');
        const name = cleanName(input.name);
        const route = ensureManualRoute(registry);
        route.name = name;
        route.enabled = input.enabled;
        retryAfter.delete(route.id);
        await save();
        await reconcile();
      });
      return json(response, 200, state());
    }
    if (path !== '/api/routes') return json(response, 404, { error: 'Not found' });
    await enqueue(async () => {
      if (registry.mode === 'manual') throw new Error('Switch to per-speaker mode to edit speaker receivers');
      if (typeof input.enabled !== 'boolean' || typeof input.player_id !== 'string') {
        throw new Error('Player and enabled state are required');
      }
      let route = registry.routes.find(r => r.player_id === input.player_id);
      const p = players.find(p => p.player_id === input.player_id);
      if (input.enabled && (discoveryError || !p?.eligible)) {
        throw new Error(p?.reason || 'This player is not currently eligible');
      }
      if (!route && !input.enabled) throw new Error('Receiver not found');
      const name = cleanName(input.name ?? route?.name ?? p?.name);
      if (registry.routes.some(r => r.player_id !== input.player_id && r.enabled && r.name === name)
          && input.enabled) throw new Error('Another enabled receiver already uses that name');
      if (!route) route = addRoute(registry, p, name);
      route.name = name;
      route.enabled = input.enabled;
      retryAfter.delete(route.id);
      await save();
      await reconcile();
    });
    json(response, 200, state());
  } catch (error) {
    // Only return known local validation messages, never exec/spawn/network errors.
    const safe = new Set([
      'Player and enabled state are required', 'Receiver not found',
      'This player is not currently eligible', 'Receiver name must be text',
      'Use a receiver name of 1–80 characters without control characters',
      'Another enabled receiver already uses that name', 'This player already has a receiver',
      'Invalid routing mode', 'Enabled state is required',
      'Switch to per-speaker mode to edit speaker receivers'
    ]);
    json(response, 400, { error: safe.has(error.message) ? error.message : 'Request failed; check discovery and receiver status' });
  }
});
server.listen(port, host, () => { void log('MANAGER_READY', { url: `http://localhost:${port}` }); });
server.on('error', () => { console.error('Management port unavailable'); process.exit(1); });
void enqueue(refresh).catch(() => log('DISCOVERY_FAILED'));
const interval = setInterval(() => {
  // Do not queue another refresh while a slow request is still pending.
  if (!refreshPending && !stopping) {
    refreshPending = true;
    void enqueue(refresh).finally(() => { refreshPending = false; }).catch(() => {});
  }
}, 15000);
let refreshPending = false;
async function shutdown() {
  if (stopping) return;
  stopping = true;
  clearInterval(interval);
  server.close();
  await enqueue(async () => {
    await Promise.all([...children.keys()].map(stopChild));
  });
  await log('MANAGER_STOPPED');
  process.exit(0);
}
process.on('SIGINT', () => { void shutdown(); });
process.on('SIGTERM', () => { void shutdown(); });
