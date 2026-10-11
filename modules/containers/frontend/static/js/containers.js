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
  document.getElementById('btn-logs-refresh').addEventListener('click', () => _logsFor && showLogs(..._logsFor));
  document.getElementById('btn-new-app').addEventListener('click', () => openAppForm(null));
  document.getElementById('form-app').addEventListener('submit', saveApp);
  document.getElementById('apps-body').addEventListener('click', onAppAction);
  document.getElementById('btn-app-remove').addEventListener('click', removeApp);
  bindModalClose('modal-logs');
  bindModalClose('modal-run');
  bindModalClose('modal-app');
  bindModalClose('modal-app-remove');
  bindModalClose('modal-install');
  bindModalClose('modal-installed');
  document.getElementById('catalogue-grid').addEventListener('click', onCatalogueClick);
  document.getElementById('form-install').addEventListener('submit', installFromCatalogue);
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
  document.getElementById('apps-section').hidden = !ready;
  document.getElementById('catalogue-section').hidden = !ready;
  if (ready && !_catalogue) loadCatalogue();
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
                   pull: 'Downloading the image…', run: 'Starting the container (downloading its image if needed)…',
                   app: 'Working on the app (downloading its images if needed)…'};
  const done = {install: 'Docker installed.', setup: `Docker now keeps its data on ${s.pool}.`,
                pull: 'Image downloaded.', run: 'Container started.', app: 'App done.'};
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
  await Promise.all([loadApps(), loadContainers(), loadImages()]);
}

let _apps = [];

async function loadApps() {
  const body = document.getElementById('apps-body');
  try {
    _apps = (await apiJSON('GET', '/api/containers/apps')).apps || [];
  } catch (e) {
    body.innerHTML = `<tr><td colspan="4" class="text-muted">Could not list apps: ${esc(e.message)}</td></tr>`;
    return;
  }
  if (!_apps.length) {
    body.innerHTML = '<tr><td colspan="4" class="text-muted">No apps yet.</td></tr>';
    return;
  }
  body.innerHTML = _apps.map(a => {
    const n = esc(a.name);
    let badge;
    if (a.state === 'down') badge = '<span class="badge badge-muted">Stopped</span>';
    else if (a.containers && a.running === a.containers) badge = `<span class="badge badge-ok">Running (${a.running})</span>`;
    else badge = `<span class="badge badge-warn">${a.running} of ${a.containers} running</span>`;
    return `
    <tr>
      <td><span class="font-mono">${n}</span><div class="text-muted text-sm font-mono">${esc(a.dataset)}</div></td>
      <td>${badge}${a.web_port && a.state === 'up'
        ? ` <a class="btn btn-secondary btn-sm" target="_blank" rel="noopener"
             href="http://${esc(location.hostname)}:${esc(a.web_port)}/">Open</a>` : ''}</td>
      <td class="font-mono text-sm">${esc(a.ports)}</td>
      <td class="advanced-only" style="white-space:nowrap;text-align:right">
        <button class="btn btn-secondary btn-sm" data-app="${n}" data-action="logs">Logs</button>
        ${a.state === 'up'
          ? `<button class="btn btn-secondary btn-sm" data-app="${n}" data-action="down">Stop</button>`
          : `<button class="btn btn-secondary btn-sm" data-app="${n}" data-action="up">Start</button>`}
        <button class="btn btn-secondary btn-sm" data-app="${n}" data-action="update">Update</button>
        <button class="btn btn-secondary btn-sm" data-app="${n}" data-action="edit">Edit</button>
        <button class="btn btn-secondary btn-sm" data-app="${n}" data-action="remove">Remove</button>
      </td>
    </tr>`;
  }).join('');
}

let _catalogue = null;
let _installing = null;

async function loadCatalogue() {
  const grid = document.getElementById('catalogue-grid');
  try {
    _catalogue = (await apiJSON('GET', '/api/containers/catalogue')).entries || [];
  } catch (e) {
    grid.innerHTML = `<p class="text-muted">Could not read the catalogue: ${esc(e.message)}</p>`;
    return;
  }
  grid.innerHTML = _catalogue.map(c => `
    <div class="card" style="display:flex;flex-direction:column;gap:8px">
      <div class="font-bold">${esc(c.name)}</div>
      <div class="text-muted text-sm" style="flex:1">${esc(c.description)}</div>
      <div style="display:flex;gap:8px;align-items:center">
        <button class="btn btn-primary btn-sm" data-entry="${esc(c.id)}">Install</button>
        <a class="text-sm" href="${esc(c.website)}" target="_blank" rel="noopener">Website</a>
      </div>
    </div>`).join('');
}

function settingField(s, value, users) {
  const id = `f-set-${esc(s.key)}`;
  const hint = s.hint ? `<span class="form-hint">${esc(s.hint)}</span>` : '';
  let input;
  if (s.type === 'user') {
    input = `<select id="${id}" class="input" data-key="${esc(s.key)}">${users.map(u =>
      `<option${u === value ? ' selected' : ''}>${esc(u)}</option>`).join('')}</select>`;
  } else if (s.type === 'choice') {
    input = `<select id="${id}" class="input" data-key="${esc(s.key)}">${s.choices.map(c =>
      `<option${c === value ? ' selected' : ''}>${esc(c)}</option>`).join('')}</select>`;
  } else {
    input = `<input id="${id}" class="input${s.type === 'password' || s.type === 'folder' ? ' font-mono' : ''}"
      data-key="${esc(s.key)}" value="${esc(value)}" ${s.type === 'port' ? 'inputmode="numeric"' : ''}>`;
  }
  return `<div class="form-group"><label for="${id}">${esc(s.label || s.key)}</label>${input}${hint}</div>`;
}

async function onCatalogueClick(e) {
  const btn = e.target.closest('button[data-entry]');
  if (!btn) return;
  setLoading(btn, true, '…');
  try {
    _installing = await apiJSON('GET', `/api/containers/catalogue/${encodeURIComponent(btn.dataset.entry)}`);
  } catch (err) {
    toast(`Error: ${err.message}`, 'err');
    setLoading(btn, false);
    return;
  }
  setLoading(btn, false);
  const c = _installing;
  document.getElementById('install-title').textContent = `Install ${c.name}`;
  document.getElementById('install-description').textContent = c.description;
  document.getElementById('install-note').textContent = c.note || '';
  document.getElementById('install-note').hidden = !c.note;
  document.getElementById('f-install-name').value = c.app_name;
  document.getElementById('install-settings').innerHTML =
    c.settings.map(s => settingField(s, c.defaults[s.key] || '', c.users || [])).join('');
  document.getElementById('install-assisted').hidden = UILevel.current() !== 'assisted';
  openModal('modal-install');
}

async function installFromCatalogue(e) {
  e.preventDefault();
  const c = _installing;
  const values = { ...c.defaults };
  if (UILevel.current() !== 'assisted') {
    document.querySelectorAll('#install-settings [data-key]').forEach(el => { values[el.dataset.key] = el.value.trim(); });
  }
  const name = UILevel.current() === 'assisted' ? c.app_name
                                                : document.getElementById('f-install-name').value.trim();
  const btn = document.getElementById('btn-install-submit');
  setLoading(btn, true, 'Installing…');
  let result;
  try {
    result = await apiJSON('POST', `/api/containers/catalogue/${encodeURIComponent(c.id)}/install`, { name, values });
  } catch (err) {
    toast(`Error: ${err.message}`, 'err', 8000);
    setLoading(btn, false);
    return;
  }
  setLoading(btn, false);
  closeModal('modal-install');
  document.getElementById('installed-title').textContent = `${c.name} installed as ${result.name}`;
  const rows = [];
  if (result.web_port) {
    const url = `http://${location.hostname}:${result.web_port}/`;
    rows.push(['Address', `<a href="${esc(url)}" target="_blank" rel="noopener">${esc(url)}</a>`]);
  }
  for (const [label, value] of Object.entries(result.shown || {}))
    rows.push([esc(label), `<span class="font-mono">${esc(value)}</span>`]);
  document.getElementById('installed-body').innerHTML = rows.map(([l, v]) =>
    `<tr><td class="text-muted" style="padding:5px 8px 5px 0">${l}</td><td>${v}</td></tr>`).join('');
  openModal('modal-installed');
  loadStatus();
  loadApps();
}

let _editing = null;

async function openAppForm(name) {
  _editing = name;
  const form = document.getElementById('form-app');
  form.reset();
  const nameInput = document.getElementById('f-app-name');
  nameInput.readOnly = !!name;
  document.getElementById('app-start-row').hidden = !!name;
  document.getElementById('app-title').textContent = name ? `Edit ${name}` : 'New app';
  document.getElementById('btn-app-submit').textContent = name ? 'Save' : 'Create';
  if (name) {
    try {
      const app = await apiJSON('GET', `/api/containers/apps/${encodeURIComponent(name)}`);
      nameInput.value = app.name;
      document.getElementById('f-app-compose').value = app.compose;
    } catch (e) {
      toast(`Error: ${e.message}`, 'err');
      return;
    }
  }
  openModal('modal-app');
}

async function saveApp(e) {
  e.preventDefault();
  const name = document.getElementById('f-app-name').value.trim();
  const compose = document.getElementById('f-app-compose').value;
  const btn = document.getElementById('btn-app-submit');
  setLoading(btn, true, 'Checking…');
  try {
    if (_editing) {
      await apiJSON('PUT', `/api/containers/apps/${encodeURIComponent(name)}`, { compose });
    } else {
      await apiJSON('POST', '/api/containers/apps',
                    { name, compose, start: document.getElementById('f-app-start').checked });
    }
    closeModal('modal-app');
    toast(_editing ? `${name} saved.` : `${name} created.`, 'ok');
  } catch (err) {
    toast(`Error: ${err.message}`, 'err', 8000);
  }
  setLoading(btn, false);
  loadStatus();
  loadApps();
}

let _removing = null;

async function onAppAction(e) {
  const btn = e.target.closest('button[data-app]');
  if (!btn) return;
  const name = btn.dataset.app;
  const action = btn.dataset.action;
  if (action === 'logs') return showLogs('app', name);
  if (action === 'edit') return openAppForm(name);
  if (action === 'remove') {
    _removing = name;
    const app = _apps.find(a => a.name === name) || {};
    document.getElementById('app-remove-title').textContent = `Remove ${name}`;
    document.getElementById('app-remove-dataset').textContent = app.dataset || '';
    document.getElementById('f-app-destroy').checked = false;
    return openModal('modal-app-remove');
  }
  if (action === 'down' && !await confirm('Stop app', `Stop ${name}? Its containers are removed; its data stays.`, 'Stop')) return;
  setLoading(btn, true, '…');
  try {
    await apiJSON('POST', `/api/containers/apps/${encodeURIComponent(name)}/${action}`);
  } catch (err) {
    toast(`Error: ${err.message}`, 'err');
    setLoading(btn, false);
  }
  loadStatus();
}

async function removeApp() {
  const name = _removing;
  const destroy = document.getElementById('f-app-destroy').checked;
  if (destroy && !await confirm('Destroy data', `Destroy the dataset of ${name} with everything in it? This cannot be undone.`, 'Destroy')) return;
  const btn = document.getElementById('btn-app-remove');
  setLoading(btn, true, 'Removing…');
  try {
    await apiJSON('DELETE', `/api/containers/apps/${encodeURIComponent(name)}?destroy_data=${destroy}`);
    closeModal('modal-app-remove');
    toast(`${name} removed.`, 'ok');
  } catch (err) {
    toast(`Error: ${err.message}`, 'err');
  }
  setLoading(btn, false);
  loadLists();
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
      <td class="font-mono text-sm" title="${esc(c.image)}">${esc(String(c.image || '').split('@')[0])}</td>
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
  if (action === 'logs') return showLogs('container', name);
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

async function showLogs(kind, name) {
  _logsFor = [kind, name];
  document.getElementById('logs-title').textContent = `Logs of ${name}`;
  const pre = document.getElementById('logs-text');
  pre.textContent = 'Loading…';
  openModal('modal-logs');
  try {
    const base = kind === 'app' ? 'apps' : 'containers';
    const d = await apiJSON('GET', `/api/containers/${base}/${encodeURIComponent(name)}/logs?lines=200`);
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
