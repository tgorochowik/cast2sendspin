import { randomUUID } from 'node:crypto';

export function playbackSettings(registry, config = {}) {
  const value = registry.playback?.stop_receiver_on_seek
    ?? config.stop_receiver_on_seek
    ?? (config.seek_without_output_stop === true ? false : true);
  if (typeof value !== 'boolean') throw new Error('Seek stop setting must be boolean');
  return { stop_receiver_on_seek: value };
}

export function setPlaybackSettings(registry, input) {
  if (typeof input?.stop_receiver_on_seek !== 'boolean') {
    throw new Error('Seek stop setting must be boolean');
  }
  registry.playback = { stop_receiver_on_seek: input.stop_receiver_on_seek };
}

// This one setting is delivered live. Other config changes still restart a
// receiver; a seek-policy change must not tear down an active cast session.
export function configFingerprint(config) {
  const { stop_receiver_on_seek, seek_without_output_stop, ...rest } = config;
  return JSON.stringify(rest);
}

export function requestPlaybackSettings(child, settings, timeout = 5000) {
  return new Promise((resolve, reject) => {
    const id = randomUUID();
    let settled = false;
    const finish = error => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      child.off('message', message);
      child.off('exit', exited);
      if (error) reject(new Error('Receiver settings not acknowledged'));
      else resolve();
    };
    const message = data => {
      if (data?.type !== 'playback-settings-result' || data.request_id !== id) return;
      finish(data.ok !== true);
    };
    const exited = () => finish(true);
    const timer = setTimeout(() => finish(true), timeout);
    child.on('message', message);
    child.once('exit', exited);
    try {
      child.send({ type: 'playback-settings', request_id: id, ...settings },
        error => { if (error) finish(true); });
    } catch { finish(true); }
  });
}

export function bindPlaybackSettings(channel, worker) {
  const listener = async data => {
    if (data?.type !== 'playback-settings'
        || typeof data.request_id !== 'string' || data.request_id.length > 64
        || typeof data.stop_receiver_on_seek !== 'boolean') return;
    let ok = true;
    try {
      await worker.call('configure_seek', { stop_receiver_on_seek: data.stop_receiver_on_seek });
    } catch { ok = false; }
    try {
      channel.send({
        type: 'playback-settings-result', request_id: data.request_id, ok
      }, () => {});
    } catch { /* The manager may already have exited. */ }
  };
  channel.on('message', listener);
  return () => channel.off('message', listener);
}
