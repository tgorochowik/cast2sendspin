import { appendFileSync, mkdirSync } from 'node:fs';
import { readFile, writeFile, rename } from 'node:fs/promises';
import { DataStore } from 'yt-cast-receiver';
import { join } from 'node:path';

const stateDir = process.env.BRIDGE_STATE_DIR || '.state';
mkdirSync(stateDir, { recursive: true, mode: 0o700 });
export function log(event, details = {}) {
  const line = JSON.stringify({ time: new Date().toISOString(), event, ...details });
  appendFileSync(process.env.BRIDGE_LOG || join(stateDir, 'bridge.log'), `${line}\n`, { mode: 0o600 });
  console.log(line);
}
export const logger = {
  setLevel() {}, debug() {},
  info(message) { if (typeof message === 'string') log('LIB_INFO', { message }); },
  warn(message) { if (typeof message === 'string') log('LIB_WARN', { message }); },
  error(message) { if (typeof message === 'string') log('LIB_ERROR', { message }); }
};

export class LocalStore extends DataStore {
  values = {};
  pending = Promise.resolve();
  async load() {
    try {
      this.values = JSON.parse(await readFile(join(stateDir, 'identity.json'), 'utf8'));
    } catch (error) {
      if (error.code !== 'ENOENT') throw error;
    }
  }
  async get(key) { return this.values[key] ?? null; }
  async set(key, value) {
    this.values[key] = value;
    const snapshot = JSON.stringify(this.values);
    this.pending = this.pending.then(async () => {
      await writeFile(join(stateDir, 'identity.json.tmp'), snapshot, { mode: 0o600 });
      await rename(join(stateDir, 'identity.json.tmp'), join(stateDir, 'identity.json'));
    });
    await this.pending;
  }
}
