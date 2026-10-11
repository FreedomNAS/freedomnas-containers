'use strict';

let _status = null;
let _poll = null;
let _lastJob = null;
let _logsFor = null;

document.addEventListener('DOMContentLoaded', () => {
  if (!initShared()) return;
  document.getElementById('btn-install').addEventListener('click', installRuntime);
  document.getElementById('btn-setup').addEventListener('click', setupRuntime);
  document.getElementById('btn-refresh').addEventListener('click', loadLists);
  document.getElementById('containers-body').addEventListener('click', onContainerAction);
  document.getElementById('images-body').addEventListener('click', onImageAction);
  document.getElementById('form-pull').addEventListener('submit', pullImage);
  document.getElementById('btn-prune').addEventListener('click', pruneImages);
  document.getElementById('btn-run').addEventListener('click', () => openModal('modal-run'));
  document.getElementById('form-run').addEventListener('submit', runContainer);
  document.getElementById('btn-logs-refresh').addEventListener('click', () => _logsFor && showLogs(_logsFor));
  bindModalClose('modal-logs');
  bindModalClose('modal-run');
  loadStatus();
});

async function loadStatus() {
  try {
    _status = await apiJSON('GET', '/api/containers/status');
  } catch (e) {
    document.getElementById('runtime-body').innerHTML =
      `<tr><td colspan="2" class="text-muted">Could not read the runtime: ${esc(e.message)}</td></tr>`;
    return;
  }
  render();
  const job = _status.job || {};
  const ready = _status.installed && _status.running && !!_status.version;
  document.getElementById('containers-section').hidden = !ready;
  document.getElementById('images-section').hidden = !ready;
  const jobKey = `${job.kind}:${job.state}:${job.finished}`;
  if (ready && (_lastJob === null || (jobKey !== _lastJob && job.state !== 'running'))) loadLists();
  _lastJob = jobKey;
  const busy = job.state === 'running';
  if (busy && !_poll) _poll = setInterval(loadStatus, 3000);
  if (!busy && _poll) { clearInterval(_poll); _poll = null; }
}

function row(label, value) {
  return `<tr><td class="text-muted" style="padding:5px 0;width:140px">${label}</td><td>${value}</td></tr>`;
}

function render() {
  const s = _status;
  let state;
  if (!s.installed) state = '<span class="badge badge-muted">Not installed</span>';
  else if (!s.configured) state = '<span class="badge badge-warn">Installed, not set up</span>';
  else if (s.running && s.version && (s.datasets || []).every(d => d.mounted))
    state = `<span class="badge badge-ok">Running on ${esc(s.pool)}</span>`;
  else state = '<span class="badge badge-err">Not running</span>';

  const rows = [row('Docker', state)];
  if (s.version) rows.push(row('Version', esc(s.version)));
  for (const d of s.datasets || []) {
    const mounted = d.mounted ? '' : ' <span class="badge badge-err">Not mounted</span>';
    rows.push(row(esc(d.path || d.name),
      `<span class="font-mono">${esc(d.dataset)}</span>${mounted}`));
  }
  if (s.driver) rows.push(row('Storage driver', esc(s.driver)));
  if (s.containers !== null && s.containers !== undefined)
    rows.push(row('Containers', `${s.containers} · ${s.images} images`));
  document.getElementById('runtime-body').innerHTML = rows.join('');

  const job = s.job || {};
  const line = document.getElementById('job-line');
  const running = {install: 'Installing Docker…', setup: 'Setting up the datasets and starting Docker…',
                   pull: 'Downloading the image…', run: 'Starting the container (downloading its image if needed)…'};
  const done = {install: 'Docker installed.', setup: `Docker now keeps its data on ${s.pool}.`,
                pull: 'Image downloaded.', run: 'Container started.'};
  if (job.state === 'running') line.textContent = running[job.kind] || 'Working…';
  else if (job.state === 'failed') line.textContent = `The ${job.kind} failed: ${job.error}`;
  else if (job.state === 'done') line.textContent = done[job.kind] || 'Done.';
  line.hidden = !job.state || job.state === 'idle';

  const busy = job.state === 'running';
  document.getElementById('install-section').hidden = s.installed;
  document.getElementById('setup-section').hidden = !s.installed || s.configured;
  document.getElementById('btn-install').disabled = busy;
  document.getElementById('btn-setup').disabled = busy || !s.pools.length;

  const select = document.getElementById('f-pool');
  const chosen = select.value || s.pool || '';
  select.innerHTML = s.pools.length
    ? s.pools.map(p => `<option value="${esc(p)}"${p === chosen ? ' selected' : ''}>${esc(p)}</option>`).join('')
    : '<option value="">No pool imported</option>';
}

async function installRuntime() {
  const btn = document.getElementById('btn-install');
  setLoading(btn, true, 'Starting…');
  try {
    await apiJSON('POST', '/api/containers/install');
  } catch (e) {
    toast(`Error: ${e.message}`, 'err');
  }
  setLoading(btn, false);
  loadStatus();
}

async function setupRuntime() {
  const pool = document.getElementById('f-pool').value;
  if (!pool) return;
  if (!await confirm('Set up containers',
      `Docker stops while its data is put on ${pool}/containers, then starts again. Running containers stop for that time.`,
      'Continue')) return;
  const btn = document.getElementById('btn-setup');
  setLoading(btn, true, 'Starting…');
  try {
    await apiJSON('POST', '/api/containers/setup', { pool });
  } catch (e) {
    toast(`Error: ${e.message}`, 'err');
  }
  setLoading(btn, false);
  loadStatus();
}

async function loadLists() {
  await Promise.all([loadContainers(), loadImages()]);
}

let _containers = [];

async function loadContainers() {
  const body = document.getElementById('containers-body');
  try {
    _containers = (await apiJSON('GET', '/api/containers/containers')).containers || [];
  } catch (e) {
    body.innerHTML = `<tr><td colspan="5" class="text-muted">Could not list containers: ${esc(e.message)}</td></tr>`;
    return;
  }
  if (!_containers.length) {
    body.innerHTML = '<tr><td colspan="5" class="text-muted">No containers yet.</td></tr>';
    return;
  }
  body.innerHTML = _containers.map(c => {
    const n = esc(c.name);
    const up = c.state === 'running';
    const badge = up ? 'badge-ok' : (c.state === 'exited' || c.state === 'created' ? 'badge-muted' : 'badge-warn');
    return `
    <tr>
      <td><span class="font-mono">${n}</span>${c.project ? ` <span class="badge badge-info">${esc(c.project)}</span>` : ''}</td>
      <td class="font-mono text-sm">${esc(c.image)}</td>
      <td><span class="badge ${badge}" title="${esc(c.status)}">${esc(c.state)}</span></td>
      <td class="font-mono text-sm">${esc(c.ports)}</td>
      <td class="advanced-only" style="white-space:nowrap;text-align:right">
        <button class="btn btn-secondary btn-sm" data-name="${n}" data-action="logs">Logs</button>
        ${up
          ? `<button class="btn btn-secondary btn-sm" data-name="${n}" data-action="restart">Restart</button>
             <button class="btn btn-secondary btn-sm" data-name="${n}" data-action="stop">Stop</button>`
          : `<button class="btn btn-secondary btn-sm" data-name="${n}" data-action="start">Start</button>`}
        <button class="btn btn-secondary btn-sm" data-name="${n}" data-action="remove">Remove</button>
      </td>
    </tr>`;
  }).join('');
}

async function onContainerAction(e) {
  const btn = e.target.closest('button[data-action]');
  if (!btn) return;
  const name = btn.dataset.name;
  const action = btn.dataset.action;
  if (action === 'logs') return showLogs(name);
  const c = _containers.find(x => x.name === name) || {};
  if (action === 'stop' && !await confirm('Stop container', `Stop ${name}? What it serves stops until it is started again.`, 'Stop')) return;
  if (action === 'remove' && !await confirm('Remove container',
      `Remove ${name}?${c.state === 'running' ? ' It is running and will be stopped.' : ''} Its named volumes are kept.`, 'Remove')) return;
  setLoading(btn, true, '…');
  try {
    if (action === 'remove') {
      await apiJSON('DELETE', `/api/containers/containers/${encodeURIComponent(name)}?force=${c.state === 'running'}`);
    } else {
      await apiJSON('POST', `/api/containers/containers/${encodeURIComponent(name)}/${action}`);
    }
    toast(`${name}: ${action} done.`, 'ok');
  } catch (err) {
    toast(`Error: ${err.message}`, 'err');
    setLoading(btn, false);
  }
  loadLists();
}

async function showLogs(name) {
  _logsFor = name;
  document.getElementById('logs-title').textContent = `Logs of ${name}`;
  const pre = document.getElementById('logs-text');
  pre.textContent = 'Loading…';
  openModal('modal-logs');
  try {
    const d = await apiJSON('GET', `/api/containers/containers/${encodeURIComponent(name)}/logs?lines=200`);
    pre.textContent = d.logs || '(no output)';
    pre.parentElement.scrollTop = pre.parentElement.scrollHeight;
  } catch (e) {
    pre.textContent = `Could not read the logs: ${e.message}`;
  }
}

async function loadImages() {
  const body = document.getElementById('images-body');
  let list;
  try {
    list = (await apiJSON('GET', '/api/containers/images')).images || [];
  } catch (e) {
    body.innerHTML = `<tr><td colspan="4" class="text-muted">Could not list images: ${esc(e.message)}</td></tr>`;
    return;
  }
  body.innerHTML = list.length ? list.map(i => `
    <tr>
      <td class="font-mono text-sm">${esc(i.reference)}</td>
      <td>${esc(i.size)}</td>
      <td class="text-muted">${esc(i.created)}</td>
      <td style="text-align:right"><button class="btn btn-secondary btn-sm" data-image="${esc(i.reference)}">Remove</button></td>
    </tr>`).join('') : '<tr><td colspan="4" class="text-muted">No images.</td></tr>';
}

async function onImageAction(e) {
  const btn = e.target.closest('button[data-image]');
  if (!btn) return;
  const image = btn.dataset.image;
  if (!await confirm('Remove image', `Remove ${image}? It is downloaded again when a container needs it.`, 'Remove')) return;
  setLoading(btn, true, '…');
  try {
    await apiJSON('DELETE', `/api/containers/images/${encodeURIComponent(image)}`);
    toast(`${image} removed.`, 'ok');
  } catch (err) {
    toast(`Error: ${err.message}`, 'err');
    setLoading(btn, false);
  }
  loadImages();
}

async function pullImage(e) {
  e.preventDefault();
  const input = document.getElementById('f-pull-image');
  const image = input.value.trim();
  if (!image) return;
  try {
    await apiJSON('POST', '/api/containers/images/pull', { image });
    input.value = '';
  } catch (err) {
    toast(`Error: ${err.message}`, 'err');
  }
  loadStatus();
}

async function pruneImages() {
  if (!await confirm('Remove unused images', 'Remove every image no container uses and no tag names?', 'Remove')) return;
  try {
    await apiJSON('POST', '/api/containers/images/prune');
    toast('Unused images removed.', 'ok');
  } catch (err) {
    toast(`Error: ${err.message}`, 'err');
  }
  loadImages();
}

function lines(id) {
  return document.getElementById(id).value.split('\n').map(l => l.trim()).filter(Boolean);
}

async function runContainer(e) {
  e.preventDefault();
  const spec = {
    image: document.getElementById('f-run-image').value.trim(),
    name: document.getElementById('f-run-name').value.trim() || null,
    ports: lines('f-run-ports'),
    volumes: lines('f-run-volumes'),
    env: lines('f-run-env'),
    restart: document.getElementById('f-run-restart').value,
  };
  const btn = document.getElementById('btn-run-submit');
  setLoading(btn, true, 'Starting…');
  try {
    await apiJSON('POST', '/api/containers/containers/run', spec);
    closeModal('modal-run');
    document.getElementById('form-run').reset();
  } catch (err) {
    toast(`Error: ${err.message}`, 'err');
  }
  setLoading(btn, false);
  loadStatus();
}

function esc(s) {
  return String(s ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}
