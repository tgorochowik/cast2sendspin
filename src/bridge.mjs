import { spawn } from 'node:child_process';
import { readFile } from 'node:fs/promises';
import { createInterface } from 'node:readline';
import { EventEmitter } from 'node:events';
import YouTubeCastReceiver, { Constants, PLAYER_STATUSES } from 'yt-cast-receiver';
import { BridgePlayer } from './bridge-player.mjs';
import { LocalStore, log, logger } from './runtime.mjs';
import { randomUUID } from 'node:crypto';
import { createReceiver } from './receiver.mjs';

process.umask(0o077);
const config = JSON.parse(await readFile(process.env.BRIDGE_CONFIG || 'config.json', 'utf8'));

class Worker extends EventEmitter {
  pending = new Map();
  sequence = 0;
  state = { position: 0, duration: 0, volume: { level: 0, muted: false } };
  async start() {
    this.process = spawn('.venv/bin/python', ['-u', 'src/worker.py'], {
      stdio: ['pipe', 'pipe', 'pipe']
    });
    // Do not expose raw library exceptions, HTTP headers, or signed media URLs.
    this.process.stderr.on('data', () => log('WORKER_DIAGNOSTIC_SUPPRESSED'));
    this.process.stdin.on('error', () => {});
    this.process.on('error', () => this.emit('failed'));
    this.process.on('exit', code => {
      for (const { reject, timer } of this.pending.values()) {
        clearTimeout(timer);
        reject(new Error('Media worker exited'));
      }
      this.pending.clear();
      log('WORKER_EXIT', { code });
      this.emit('failed');
    });
    const ready = new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('Media worker startup timed out')), 90000);
      this.once('ready', () => { clearTimeout(timer); resolve(); });
      this.once('failed', () => { clearTimeout(timer); reject(new Error('Media worker startup failed')); });
    });
    createInterface({ input: this.process.stdout }).on('line', line => {
      let message;
      try { message = JSON.parse(line); }
      catch { log('WORKER_INVALID_MESSAGE'); return; }
      if (message.event === 'STATE') this.state = message;
      else if (message.event === 'RESULT') {
        const pending = this.pending.get(message.id);
        if (!pending) return;
        this.pending.delete(message.id);
        clearTimeout(pending.timer);
        if (message.error) {
          log('COMMAND_FAILED', { method: pending.method, errorType: message.error });
          pending.reject(new Error('Media command failed'));
        } else {
          this.state = message.result;
          pending.resolve(message.result);
        }
      } else {
        log(message.event, Object.fromEntries(
          Object.entries(message).filter(([key]) =>
            ['video_id', 'title', 'reason', 'error_type', 'resolver', 'itag', 'bitrate'].includes(key))
        ));
        if (message.event === 'READY') {
          this.state = message.state;
          this.emit('ready');
        }
        if (message.event === 'FATAL') this.emit('failed');
        this.emit('event', message);
      }
    });
    await ready;
  }
  call(method, args = {}) {
    const id = ++this.sequence;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error('Media command timed out'));
      }, 90000);
      this.pending.set(id, { resolve, reject, timer, method });
      this.process.stdin.write(`${JSON.stringify({ id, method, args })}\n`);
    });
  }
}

const worker = new Worker();
let receiver;
let stopping = false;
async function stop() {
  if (stopping) return;
  stopping = true;
  log('SHUTDOWN');
  const deadline = setTimeout(() => {
    worker.process?.kill('SIGKILL');
    process.exit(1);
  }, 15000);
  try {
    // Stop the frontend first so no new load can race the final cleanup.
    if (receiver) await receiver.stop();
    if (worker.process?.exitCode === null) await worker.call('finish');
  } catch {
    log('CLEANUP_FAILED');
  } finally {
    worker.process?.stdin.end();
    await dataStore.pending.catch(() => {});
    // EOF gives the worker its own graceful teardown; exit when it finishes.
    if (worker.process?.exitCode === null) {
      await new Promise(resolve => worker.process.once('exit', resolve));
    }
    clearTimeout(deadline);
    process.exit(0);
  }
}
const dataStore = new LocalStore();
await dataStore.load();
const dialUuid = config.dial_uuid || await dataStore.get('dial.uuid') || randomUUID();
await dataStore.set('dial.uuid', dialUuid);
process.on('SIGINT', stop);
process.on('SIGTERM', stop);
// A managed receiver must not outlive its manager after an unexpected crash.
process.on('disconnect', stop);
process.on('unhandledRejection', () => { log('UNHANDLED_REJECTION'); void stop(); });

try {
  await worker.start();
  const player = new BridgePlayer(worker);
  player.setLogger(logger);
  receiver = createReceiver(player, {
    device: { name: config.receiver_name, screenName: config.receiver_name },
    dial: { port: config.dial_port, bindToInterfaces: [config.dial_interface] },
    app: {
      resetPlayerOnDisconnectPolicy:
        Constants.RESET_PLAYER_ON_DISCONNECT_POLICIES.ALL_EXPLICITLY_DISCONNECTED
    },
    dataStore, logger
  }, dialUuid);
  worker.on('event', message => {
    if (message.event === 'TRACK_ENDED') {
      // One queue authority: the YouTube receiver selects the next item.
      void player.next().catch(() => log('QUEUE_ADVANCE_FAILED'));
    } else if (message.event === 'TAKEN_OVER') {
      void (async () => {
        await player.yieldToMA();
        // Drop the upstream session too, rather than leaving the phone attached
        // to a destination that another MA user has taken over.
        await receiver.stop();
        if (!stopping) await receiver.start();
      })().catch(() => log('SESSION_RELEASE_FAILED'));
    } else if (message.event === 'VOLUME_CHANGED') {
      worker.state.volume = message.volume;
      void player.notifyExternalStateChange().catch(() => {});
    } else if (message.event === 'PLAYBACK_ERROR') {
      void player.stop().then(() => player.notifyExternalStateChange(PLAYER_STATUSES.STOPPED))
        .catch(() => log('PLAYBACK_CLEANUP_FAILED'));
    }
  });
  worker.on('failed', () => { if (!stopping) void stop(); });
  receiver.on('senderConnect', sender => log('SENDER_CONNECTED', { name: sender.name }));
  receiver.on('senderDisconnect', (_sender, implicit) => log('SENDER_DISCONNECTED', { implicit }));
  receiver.on('error', () => log('RECEIVER_ERROR'));
  receiver.on('terminate', () => { log('RECEIVER_TERMINATED'); void stop(); });
  await receiver.start();
  log('READY', { name: config.receiver_name, target: config.target_player_name, pid: process.pid });
} catch {
  log('STARTUP_FAILED');
  await stop();
}
