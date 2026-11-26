import { randomUUID } from 'node:crypto';

export function initialRegistry(config, uuid = randomUUID()) {
  return {
    version: 1,
    mode: 'automatic',
    routes: config.target_player_id && config.receiver_name ? [{
      id: 'legacy', player_id: config.target_player_id, name: config.receiver_name,
      enabled: true, port: config.dial_port || 8099, dial_uuid: uuid, state_dir: '.state'
    }] : []
  };
}

export function addRoute(registry, player, name) {
  if (registry.routes.some(r => r.player_id === player.player_id)) {
    throw new Error('This player already has a receiver');
  }
  const used = new Set([...registry.routes, ...(registry.manual ? [registry.manual] : [])].map(r => r.port));
  let port = 8100;
  while (used.has(port)) port++;
  if (port > 8999) throw new Error('No receiver ports available');
  const id = randomUUID();
  const route = {
    id, player_id: player.player_id, name, enabled: true,
    port, dial_uuid: randomUUID(), state_dir: `.state/routes/${id}`
  };
  registry.routes.push(route);
  return route;
}

export function cleanName(name) {
  if (typeof name !== 'string') throw new Error('Receiver name must be text');
  const value = name.trim();
  if (!value || value.length > 80 || /[\u0000-\u001f\u007f]/u.test(value)) {
    throw new Error('Use a receiver name of 1–80 characters without control characters');
  }
  return value;
}

export function validateRegistry(registry) {
  if (registry.version !== 1 || !Array.isArray(registry.routes)) throw new Error('Invalid receiver registry');
  if (!['automatic', 'manual'].includes(registry.mode || 'automatic')) throw new Error('Invalid routing mode');
  if (registry.manual && registry.manual.player_id !== '__manual_source__') throw new Error('Invalid manual source');
  const ids = new Set(), ports = new Set(), players = new Set(), uuids = new Set();
  for (const r of [...registry.routes, ...(registry.manual ? [registry.manual] : [])]) {
    if (!/^(legacy|[a-f0-9-]{36})$/.test(r.id)
        || typeof r.player_id !== 'string' || !r.player_id
        || !Number.isInteger(r.port) || r.port < 1024 || r.port > 65535
        || typeof r.enabled !== 'boolean'
        || !/^[a-f0-9-]{36}$/.test(r.dial_uuid)
        || r.state_dir !== (r.id === 'legacy' ? '.state' : `.state/routes/${r.id}`)
        || ids.has(r.id) || ports.has(r.port) || players.has(r.player_id) || uuids.has(r.dial_uuid)) {
      throw new Error('Invalid or duplicate receiver definition');
    }
    cleanName(r.name);
    ids.add(r.id); ports.add(r.port); players.add(r.player_id); uuids.add(r.dial_uuid);
  }
}

export function ensureManualRoute(registry) {
  if (registry.manual) return registry.manual;
  // Reuse the allocator, but keep manual configuration outside player mappings.
  const route = addRoute(registry, { player_id: '__manual_source__' }, 'YouTube Music');
  registry.routes.pop();
  registry.manual = route;
  return route;
}

export function setRoutingMode(registry, mode) {
  if (!['automatic', 'manual'].includes(mode)) throw new Error('Invalid routing mode');
  if (mode === 'manual') ensureManualRoute(registry);
  registry.mode = mode;
}

export function desiredRoutes(registry, players, failures = 0) {
  if (failures >= 3) return [];
  if (registry.mode === 'manual') return registry.manual?.enabled ? [registry.manual] : [];
  return registry.routes.filter(route => route.enabled &&
    players.some(player => player.player_id === route.player_id && player.eligible));
}
