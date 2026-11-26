let snapshot;
let busy = false;
let dirty = false;
const $ = selector => document.querySelector(selector);
const modeButtons = [...document.querySelectorAll('[data-mode]')];

function showError(message) {
  $('#error').textContent = message;
  $('#error').hidden = !message;
}
async function post(path, input) {
  const response = await fetch(path, {
    method: 'POST', headers: {
      'Content-Type': 'application/json', 'X-CSRF-Token': snapshot.csrf
    }, body: JSON.stringify(input)
  });
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || 'Request failed');
  return body;
}
async function action(path, input) {
  busy = true;
  document.querySelectorAll('button').forEach(button => { button.disabled = true; });
  showError('');
  try {
    snapshot = await post(path, input);
    dirty = false;
    render();
  } catch (error) {
    showError(error.message);
  } finally {
    busy = false;
    // Re-render only after success; preserve edited text after a failed save.
    document.querySelectorAll('button').forEach(button => {
      button.disabled = button.dataset.blocked === 'true';
    });
  }
}
function statusLabel(status) {
  return ({ running: 'Ready', starting: 'Starting', disabled: 'Disabled',
    retrying: 'Retrying', 'inactive mode': 'Inactive' })[status] || status;
}
function row(player, route) {
  const fragment = $('#player-template').content.cloneNode(true);
  const item = fragment.querySelector('li');
  item.classList.toggle('is-excluded', !player.eligible);
  item.querySelector('h3').textContent = player.name;
  item.querySelector('.details').textContent = [
    player.provider, player.model, player.power ? 'Power control' : null
  ].filter(Boolean).join(' · ');
  item.querySelector('.reason').textContent = player.reason || '';
  const status = route?.status || (player.eligible ? 'Not exposed' : 'Excluded');
  const badge = item.querySelector('.badge');
  badge.textContent = statusLabel(status);
  badge.dataset.state = status;
  item.querySelector('.port').textContent = route ? `Port ${route.port}` : '';
  const form = item.querySelector('form');
  const name = form.elements.namedItem('name');
  const enabled = form.elements.namedItem('enabled');
  name.value = route?.name || player.name;
  name.setAttribute('aria-label', `Receiver name for ${player.name}`);
  name.disabled = !player.eligible;
  enabled.checked = route?.enabled || false;
  enabled.disabled = !player.eligible && !route?.enabled;
  enabled.setAttribute('aria-label', `Expose ${player.name} in YouTube Music`);
  const save = form.querySelector('button');
  save.dataset.blocked = String(!player.eligible && !route);
  save.disabled = save.dataset.blocked === 'true';
  form.addEventListener('input', () => { dirty = true; });
  form.addEventListener('submit', event => {
    event.preventDefault();
    if (busy) return;
    if (route?.enabled && (!enabled.checked || name.value.trim() !== route.name)
        && !confirm('This will end any active casting session on this receiver. Continue?')) return;
    void action('/api/routes', {
      player_id: player.player_id, name: name.value, enabled: enabled.checked
    });
  });
  return fragment;
}
function render() {
  const manual = snapshot.mode === 'manual';
  $('#refresh').disabled = false;
  modeButtons.forEach(button => {
    button.disabled = false;
    button.setAttribute('aria-pressed', String(button.dataset.mode === snapshot.mode));
  });
  $('#mode-description').textContent = manual
    ? 'One cast receiver, with speaker routing managed manually in MA.'
    : 'A separate receiver for each enabled speaker. Audio is routed automatically.';
  $('#automatic-section').hidden = manual;
  $('#manual-section').hidden = !manual;
  $('#refresh').hidden = manual;
  const routes = manual ? (snapshot.manual ? [snapshot.manual] : []) : snapshot.routes;
  $('#status').textContent = snapshot.discoveryError || [
    `${routes.filter(r => r.status === 'running').length} receiver(s) ready`,
    manual ? `Manual speaker routing${snapshot.manual ? ` · Port ${snapshot.manual.port}` : ''}`
      : `Last discovery: ${snapshot.updatedAt ? new Date(snapshot.updatedAt).toLocaleTimeString() : 'waiting'}`
  ].join(' · ');
  if (manual) {
    const route = snapshot.manual;
    const form = $('#manual-form');
    form.elements.namedItem('name').value = route?.name || 'YouTube Music';
    form.elements.namedItem('enabled').checked = route?.enabled ?? true;
    $('#manual-status').textContent = statusLabel(route?.status || 'Starting');
    $('#manual-status').dataset.state = route?.status || 'starting';
    $('#source-name').textContent = route?.name || 'YouTube Music';
    // No hidden discovered-player rows linger in the DOM in manual mode.
    $('#players').replaceChildren();
    return;
  }
  const players = [...snapshot.players];
  for (const route of snapshot.routes) {
    if (!players.some(p => p.player_id === route.player_id)) {
      players.push({ player_id: route.player_id, name: route.name, eligible: false,
        reason: 'Unavailable in MA. Receiver identity and settings are retained.' });
    }
  }
  const enabled = id => snapshot.routes.some(r => r.player_id === id && r.enabled);
  players.sort((a, b) => Number(b.eligible) - Number(a.eligible)
    || Number(enabled(b.player_id)) - Number(enabled(a.player_id))
    || a.name.localeCompare(b.name));
  $('#players').replaceChildren();
  for (const player of players) {
    $('#players').append(row(player, snapshot.routes.find(r => r.player_id === player.player_id)));
  }
  $('#speaker-count').textContent = `${players.filter(p => p.eligible).length} available · ${players.filter(p => !p.eligible).length} excluded / unavailable`;
  $('#empty').hidden = players.length > 0;
}
async function refresh() {
  if (busy || dirty || document.activeElement?.tagName === 'INPUT') return;
  try {
    const response = await fetch('/api/state');
    if (!response.ok) throw new Error('Management connection failed');
    snapshot = await response.json();
    render();
  } catch (error) { showError(error.message); }
}
modeButtons.forEach(button => button.addEventListener('click', () => {
  if (busy || !snapshot || snapshot.mode === button.dataset.mode) return;
  if (!confirm(`Switch routing mode? Active casting sessions will end. Saved speaker settings will be preserved.${dirty ? ' Unsaved edits will be discarded.' : ''}`)) return;
  void action('/api/mode', { mode: button.dataset.mode });
}));
$('#manual-form').addEventListener('input', () => { dirty = true; });
$('#manual-form').addEventListener('submit', event => {
  event.preventDefault();
  if (busy) return;
  const route = snapshot.manual;
  const form = event.currentTarget;
  const name = form.elements.namedItem('name').value;
  const enabled = form.elements.namedItem('enabled').checked;
  if (route?.enabled && (!enabled || name.trim() !== route.name)
      && !confirm('This will end any active casting session. Continue?')) return;
  void action('/api/manual', { name, enabled });
});
$('#refresh').addEventListener('click', () => {
  if (dirty && !confirm('Discard unsaved changes and refresh?')) return;
  void action('/api/refresh', {});
});
void refresh();
setInterval(refresh, 5000);
