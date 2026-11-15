/* Console logic */
const $ = (s) => document.querySelector(s);
const $$ = (s) => document.querySelectorAll(s);
let CONFIG = null;
let PG_IMAGE = '';   // data URL loaded from a local file

/* ---- tab switching ---- */
$$('.tabs button').forEach(b => b.onclick = () => {
  $$('.tabs button').forEach(x => x.classList.toggle('active', x === b));
  ['overview', 'playground', 'keys', 'settings', 'password'].forEach(t =>
    $('#tab-' + t).classList.toggle('hidden', t !== b.dataset.tab));
  if (b.dataset.tab === 'keys') loadKeys();
});

/* ---- overview ---- */
function renderStats(s, d) {
  const items = [
    ['Requests', s.requests],
    ['Videos done', s.tasks_completed],
    ['Errors', s.errors],
    ['Devices today', d.today_devices],
    ['Remaining today', d.remaining_today],
    ['Uptime', Math.round(s.uptime_s) + 's'],
  ];
  $('#stats').innerHTML = items.map(([l, v]) =>
    `<div class="stat"><div class="label">${l}</div><div class="value">${v}</div></div>`).join('');
}

function renderPool(p) {
  if (!p) return;
  $('#pool-badge').textContent = `idle ${p.idle}/${p.target} · in-flight ${p.inflight} · credits ${p.credits}`;
  const items = [
    ['Ready', p.idle],
    ['In flight', p.inflight],
    ['Exhausted', p.exhausted],
    ['Credits', p.credits],
    ['Pool size', p.size + '/' + p.target],
    ['Minted', p.minted],
  ];
  $('#pool-stats').innerHTML = items.map(([l, v]) =>
    `<div class="stat"><div class="label">${l}</div><div class="value">${v}</div></div>`).join('');
}

function stateBadge(state, remaining) {
  const s = state || (remaining > 0 ? 'available' : 'exhausted');
  if (s === 'in-flight') return '<span class="badge warn">in-flight</span>';
  if (s === 'exhausted') return '<span class="badge err">exhausted</span>';
  if (s === 'available') return '<span class="badge ok">available</span>';
  return '<span class="badge">' + esc(s) + '</span>';
}

function renderDevices(d) {
  $('#ledger-badge').textContent = `today ${d.today_devices} · used ${d.used_today}/${d.capacity_today} · left ${d.remaining_today}`;
  const rows = (d.devices || []).map(x => `<tr>
    <td class="mono">${esc(x.id)}</td>
    <td>${x.used}</td>
    <td>${x.remaining}</td>
    <td>${esc(x.reason)}</td>
    <td>${stateBadge(x.state, x.remaining)}</td>
  </tr>`).join('');
  $('#device-body').innerHTML = rows || '<tr><td colspan="5" class="muted">No devices yet — one is created on the first generation.</td></tr>';
}

async function loadOverview() {
  const j = await api('/admin/api/config');
  CONFIG = j.config;
  $('#ver').textContent = 'v' + j.version;
  renderStats(j.stats, j.devices);
  renderPool(j.pool);
  renderDevices(j.devices);
  if (CONFIG.must_change_password) toast('Please change the default password now', 'err');
}

$('#pool-refill').onclick = async () => {
  const btn = $('#pool-refill');
  btn.disabled = true;
  try { const j = await api('/admin/api/pool/refill', { method: 'POST' }); renderPool(j.pool); toast('Pool refilled', 'ok'); }
  catch (e) { toast(e.message, 'err'); }
  finally { btn.disabled = false; }
};

$('#newdevice').onclick = async () => {
  try { const j = await api('/admin/api/devices/new', { method: 'POST' }); toast('New device ' + (j.device || ''), 'ok'); loadOverview(); }
  catch (e) { toast(e.message, 'err'); }
};

/* ---- playground ---- */
$('#pg-file').onchange = (e) => {
  const f = e.target.files[0];
  if (!f) return;
  const r = new FileReader();
  r.onload = () => { PG_IMAGE = r.result; showPreview(PG_IMAGE); };
  r.readAsDataURL(f);
};
$('#pg-url').oninput = (e) => { if (e.target.value) showPreview(e.target.value); };
$('#pg-b64').oninput = (e) => { if (e.target.value) showPreview(e.target.value); };
$('#pg-clear').onclick = () => {
  PG_IMAGE = ''; $('#pg-file').value = ''; $('#pg-url').value = ''; $('#pg-b64').value = '';
  $('#pg-preview-row').style.display = 'none';
};
function showPreview(src) {
  const img = $('#pg-preview');
  img.src = src.startsWith('data:') || src.startsWith('http') ? src : 'data:image/png;base64,' + src;
  $('#pg-preview-row').style.display = 'flex';
}

function pgImage() {
  const url = $('#pg-url').value.trim();
  const b64 = $('#pg-b64').value.trim();
  return PG_IMAGE || url || b64 || '';
}

$('#pg-run').onclick = async () => {
  const prompt = $('#pg-prompt').value.trim();
  const image = pgImage();
  if (!image) return toast('Provide a source image first', 'err');
  if (!prompt) return toast('Enter a prompt first', 'err');
  const btn = $('#pg-run');
  btn.disabled = true;
  $('#pg-result').innerHTML = '';
  $('#pg-elapsed').textContent = '-';
  $('#pg-status').textContent = 'Submitting…';
  $('#pg-progress').classList.remove('hidden');
  const started = Date.now();
  const tick = setInterval(() => {
    $('#pg-status').textContent = `Generating… ${Math.round((Date.now() - started) / 1000)}s`;
  }, 500);
  try {
    let task = await api('/admin/api/generate', { method: 'POST', body: { image, prompt } });
    while (task && task.status && !['completed', 'success', 'failed', 'error'].includes(task.status)) {
      await new Promise(r => setTimeout(r, 4000));
      task = await api('/admin/api/task?id=' + encodeURIComponent(task.id));
      const pct = Math.max(0, Math.min(100, parseInt(task.progress, 10) || 0));
      $('#pg-progress').querySelector('span').style.width = pct + '%';
      $('#pg-status').textContent = `Generating… ${pct}% · ${Math.round((Date.now() - started) / 1000)}s`;
    }
    $('#pg-progress').classList.add('hidden');
    $('#pg-elapsed').textContent = Math.round((Date.now() - started) / 1000) + 's';
    if (task && (task.status === 'completed' || task.status === 'success') && task.video_url) {
      $('#pg-result').innerHTML = `<video controls autoplay playsinline src="${esc(task.video_url)}"></video>
        <div class="row mt"><a class="badge" href="${esc(task.video_url)}" target="_blank" rel="noopener">Open video</a>
        <button class="ghost small" id="pg-copy">Copy URL</button></div>`;
      $('#pg-copy').onclick = () => copyText(task.video_url);
      $('#pg-status').textContent = `Done · task ${esc(task.id)}`;
    } else {
      const err = (task && task.error) || 'Generation failed';
      $('#pg-status').textContent = '';
      toast(err, 'err');
    }
  } catch (e) {
    $('#pg-status').textContent = '';
    $('#pg-progress').classList.add('hidden');
    toast(e.message, 'err');
  } finally {
    clearInterval(tick);
    btn.disabled = false;
  }
};

/* ---- keys ---- */
async function loadKeys() {
  const j = await api('/admin/api/keys');
  $('#require-key').checked = !!j.require_api_key;
  const rows = (j.keys || []).map(k => `<tr>
    <td>${esc(k.name)}</td>
    <td class="mono">${esc(k.key || '')}</td>
    <td>${k.enabled ? '<span class="badge ok">enabled</span>' : '<span class="badge err">disabled</span>'}</td>
    <td class="row">
      <button class="ghost small" data-copy="${esc(k.key || '')}">Copy</button>
      <button class="ghost small" data-toggle="${esc(k.key || '')}" data-on="${k.enabled ? 0 : 1}">${k.enabled ? 'Disable' : 'Enable'}</button>
      <button class="danger small" data-del="${esc(k.key || '')}">Delete</button>
    </td>
  </tr>`).join('');
  $('#keys-body').innerHTML = rows || '<tr><td colspan="4" class="muted">No keys yet</td></tr>';
  $$('[data-toggle]').forEach(b => b.onclick = async () => {
    await api('/admin/api/keys/update', { method: 'POST', body: { key: b.dataset.toggle, enabled: b.dataset.on === '1' } });
    loadKeys();
  });
  $$('[data-del]').forEach(b => b.onclick = async () => {
    if (!confirm('Delete this key?')) return;
    await api('/admin/api/keys/delete', { method: 'POST', body: { key: b.dataset.del } });
    loadKeys();
  });
  $$('[data-copy]').forEach(b => b.onclick = () => copyText(b.dataset.copy));
}

$('#require-key').onchange = async () => {
  await api('/admin/api/config', { method: 'PUT', body: { require_api_key: $('#require-key').checked } });
  toast('Updated', 'ok');
};

$('#new-key').onclick = async () => {
  const name = prompt('Key name:', 'default');
  if (name === null) return;
  const j = await api('/admin/api/keys', { method: 'POST', body: { name } });
  await loadKeys();
  if (j.key && j.key.key) {
    if (confirm('Key created. Copy it to the clipboard?\n\n' + j.key.key)) copyText(j.key.key);
  }
};

/* ---- settings ---- */
const FIELDS = ['devices.daily_limit', 'devices.submit_retries',
  'pool.target', 'pool.min', 'pool.max', 'pool.reg_min_interval',
  'pool.refill_interval', 'pool.session_ttl', 'pool.acquire_timeout',
  'upstream.host', 'upstream.timeout', 'upstream.poll_timeout', 'upstream.user_agent'];

function fillSettings() {
  if (!CONFIG) return;
  FIELDS.forEach(f => {
    const [sec, leaf] = f.split('.');
    const v = CONFIG[sec] ? CONFIG[sec][leaf] : undefined;
    const el = document.getElementById(f);
    if (el && v !== undefined) el.value = v;
  });
  $('#docs_public').checked = !!CONFIG.docs_public;
}

$('#save-settings').onclick = async () => {
  const body = {};
  FIELDS.forEach(f => {
    const [sec, leaf] = f.split('.');
    const el = document.getElementById(f);
    if (!el) return;
    body[sec] = body[sec] || {};
    body[sec][leaf] = el.value;
  });
  body.docs_public = $('#docs_public').checked;
  try {
    const j = await api('/admin/api/config', { method: 'PUT', body });
    toast(j.errors && j.errors.length ? ('Partial failure: ' + j.errors.join('; ')) : 'Settings saved', j.errors && j.errors.length ? 'err' : 'ok');
    loadOverview();
  } catch (e) { toast(e.message, 'err'); }
};

/* ---- password ---- */
$('#save-pw').onclick = async () => {
  const cur = $('#pw-current').value, nw = $('#pw-new').value, nw2 = $('#pw-new2').value;
  if (nw !== nw2) return toast('The new passwords do not match', 'err');
  try {
    await api('/admin/api/password', { method: 'POST', body: { current: cur, new: nw } });
    toast('Password updated, please sign in again', 'ok');
    setTimeout(() => location.href = '/login', 1200);
  } catch (e) { toast(e.message, 'err'); }
};

/* ---- sign out ---- */
$('#logout').onclick = async () => {
  await api('/admin/api/logout', { method: 'POST' });
  location.href = '/login';
};

/* ---- init ---- */
loadOverview().then(fillSettings).catch(e => toast(e.message, 'err'));
setInterval(() => { loadOverview().catch(() => {}); }, 5000);
