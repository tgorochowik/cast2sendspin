import { readFile } from 'node:fs/promises';

try {
  const config = JSON.parse(await readFile('config.json', 'utf8'));
  const host = config.management_host === '::1' ? '[::1]' : (config.management_host || '127.0.0.1');
  const response = await fetch(`http://${host}:${config.management_port || 8790}/api/state`, {
    signal: AbortSignal.timeout(4000)
  });
  if (!response.ok) process.exit(1);
  const result = await response.json();
  process.exit(Array.isArray(result.routes) && Array.isArray(result.players) ? 0 : 1);
} catch {
  process.exit(1);
}
