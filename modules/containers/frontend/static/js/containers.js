'use strict';

let _status = null;
let _poll = null;

document.addEventListener('DOMContentLoaded', () => {
  if (!initShared()) return;
  document.getElementById('btn-install').addEventListener('click', installRuntime);
  document.getElementById('btn-setup').addEventListener('click', setupRuntime);
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
  const busy = _status.job && _status.job.state === 'running';
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
  if (job.state === 'running') {
    line.textContent = job.kind === 'install' ? 'Installing Docker…' : 'Setting up the datasets and starting Docker…';
  } else if (job.state === 'failed') {
    line.textContent = `The ${job.kind} failed: ${job.error}`;
  } else if (job.state === 'done') {
    line.textContent = job.kind === 'install' ? 'Docker installed.' : `Docker now keeps its data on ${s.pool}.`;
  }
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

function esc(s) {
  return String(s ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}
