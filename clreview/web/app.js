'use strict';
window.CL = window.CL || {};
var CL = window.CL;

CL.state = { mappingId: null, mappings: [], queueFor: null, inflight: 0, routeToken: 0, reviewer: '', queue: { q: '', status: '', review_status: '', confidence: '', sort: 'discrepancies', offset: 0, limit: 50 }, jobId: null, deploy: { report: null, signature: null, stale: false, request: null, names: {} } };

// A filter is anything that hides rows; sort order is not a filter.
CL.queueDirty = () => { const f = CL.state.queue; return !!(f.q || f.status || f.review_status || f.confidence); };

CL.esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
CL.badge = (text, cls) => `<span class="badge ${cls || ''}">${CL.esc(text)}</span>`;
// The three review states a reviewer can reach by acting; the wire values live here once.
CL.reviewState = Object.freeze({ IN_PROGRESS: 'in_progress', REVIEWED: 'reviewed', APPROVED: 'approved_for_sync' });

CL.statusClass = (s) => ({ succeeded: 'ok', synchronized: 'ok', completed: 'ok', approved_for_sync: 'ok', ready: 'ok', reviewed: 'accent', failed: 'bad', conflict: 'bad', stale: 'warn', unsupported_target: 'warn', review_required: 'warn', in_progress: 'warn', suggested: 'warn', skipped: 'warn', skipped_already_applied: 'warn', unmatched: 'bad', rejected: 'bad' }[s] || '');
// Open identity by design: anyone may enter a name, but nothing is recorded without one.
CL.reviewer = () => document.getElementById('reviewer').value.trim();

// POSTs that only compute; they must work before a reviewer has named themselves.
CL.READ_POSTS = ['/api/sync/preview'];
CL.NEEDS_REVIEWER = { name: 'NeedsReviewer' };

CL.clearReviewerError = () => {
  document.getElementById('reviewer').removeAttribute('aria-invalid');
  document.getElementById('reviewer-error').hidden = true;
};

CL.requireReviewer = () => {
  if (CL.reviewer()) { CL.clearReviewerError(); return true; }
  const input = document.getElementById('reviewer');
  input.setAttribute('aria-invalid', 'true');
  document.getElementById('reviewer-error').hidden = false;
  input.focus();
  return false;
};

CL.api = async (method, path, body) => {
  if (method !== 'GET' && CL.READ_POSTS.indexOf(path.split('?')[0]) === -1 && !CL.requireReviewer()) {
    throw CL.NEEDS_REVIEWER;
  }
  CL.state.inflight = (CL.state.inflight || 0) + 1;
  document.body.classList.add('busy');
  try {
    const res = await fetch(path, {
      method,
      headers: { 'Content-Type': 'application/json', 'X-Reviewer': CL.reviewer() },
      body: body ? JSON.stringify(body) : undefined,
    });
    const data = await res.json().catch(() => ({ error: res.statusText }));
    if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
    return data;
  } finally {
    CL.state.inflight = Math.max(0, CL.state.inflight - 1);
    if (!CL.state.inflight) document.body.classList.remove('busy');
  }
};

CL.toast = (msg, isError) => {
  const el = document.getElementById('toast');
  el.className = 'toast' + (isError ? ' error' : '');
  el.setAttribute('role', isError ? 'alert' : 'status');
  el.textContent = msg;
  const x = document.createElement('button');
  x.type = 'button';
  x.textContent = '✕';
  x.setAttribute('aria-label', 'Dismiss notification');
  x.onclick = () => el.classList.add('hidden');
  el.appendChild(x);
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.add('hidden'), isError ? 8000 : 3500);
};

// Discovery is best-effort: a tool that is down must not take the whole form down with it.
CL.loadFailure = (what, e) => CL.toast(`${what} discovery failed: ${e.message}`, true);

// One loader for "list this connection's options into a <select>": scans for a mapping,
// folders for a deployment. Both answer [{id, name}] (scans add a date), and both must say
// "none" when the list is empty and "unavailable" when the tool did not answer - a blank
// select cannot be told apart from a tool that is down.
CL.loadScanOptions = async (selectId, connectionId, path, noun) => {
  if (!connectionId) return;
  const el = document.getElementById(selectId);
  try {
    const data = await CL.api('GET', `/api/connections/${connectionId}/${path}`);
    el.innerHTML = data.items
      .map(o => `<option value="${CL.esc(o.id)}">${CL.esc(o.name)}${o.date ? ' ' + CL.esc(o.date) : ''}</option>`).join('')
      || '<option value="">none</option>';
  } catch (e) {
    el.innerHTML = '<option value="">unavailable</option>';
    CL.loadFailure(noun, e);
  }
};

CL.dialog = ({ title, message, field, value = '', multiline = false, confirmText = 'Save', danger = false }) => new Promise(resolve => {
  const prev = document.activeElement;
  const back = document.createElement('div');
  back.className = 'modal-backdrop';
  back.innerHTML = `<div class="modal" role="dialog" aria-modal="true" aria-labelledby="modal-title">
      <h2 id="modal-title">${CL.esc(title)}</h2>
      ${message ? `<p>${CL.esc(message)}</p>` : ''}
      ${field ? `<label for="modal-input">${CL.esc(field)}</label>` +
        (multiline ? '<textarea id="modal-input"></textarea>' : '<input id="modal-input" type="text">') : ''}
      <div class="modal-actions">
        <button type="button" data-x="cancel">Cancel</button>
        <button type="button" class="${danger ? 'danger' : 'primary'}" data-x="ok">${CL.esc(confirmText)}</button>
      </div></div>`;
  document.body.appendChild(back);
  const input = back.querySelector('#modal-input');
  const okBtn = back.querySelector('[data-x=ok]');
  if (input) input.value = value;
  const done = (val) => {
    document.removeEventListener('keydown', onKey, true);
    back.remove();
    if (prev && prev.focus) prev.focus();
    resolve(val);
  };
  const ok = () => done(input ? (input.value.trim() || null) : true);
  const onKey = (e) => {
    if (e.key === 'Escape') { e.preventDefault(); done(null); }
    else if (e.key === 'Enter' && (!input || !multiline || e.metaKey || e.ctrlKey)) { e.preventDefault(); ok(); }
    else if (e.key === 'Tab') {
      const f = [...back.querySelectorAll('button, input, textarea')];
      const first = f[0], last = f[f.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    }
  };
  document.addEventListener('keydown', onKey, true);
  back.querySelector('[data-x=cancel]').onclick = () => done(null);
  okBtn.onclick = ok;
  back.onclick = (e) => { if (e.target === back) done(null); };
  if (input) { input.focus(); if (input.select) input.select(); } else okBtn.focus();
});

CL.confirm = (message, opts) => CL.dialog(Object.assign({ title: 'Are you sure?', message, confirmText: 'Confirm', danger: true }, opts || {}));

CL.run = async (fn) => {
  // one request at a time: a double-click must not queue two writes
  if (CL.state.inflight) { CL.toast('still working — one moment'); return undefined; }
  try { return await fn(); }
  // the inline error by the Reviewer box already says what to do; do not stack a toast on it
  catch (e) { if (e === CL.NEEDS_REVIEWER) return undefined; CL.toast(e.message, true); throw e; }
};

// A click is fire-and-forget: CL.run has already told the reviewer what failed, and nothing is
// waiting on the promise, so letting it reject only prints an unhandled rejection.
CL.click = (fn) => { CL.run(fn).catch(() => {}); };

const VIEW_TITLES = { overview: 'Project overview', queue: 'Review queue', workspace: 'Component review', history: 'Synchronization history', deploy: 'Source deployment' };

CL.view = (name) => {
  document.querySelectorAll('.view').forEach(v => v.classList.add('hidden'));
  document.getElementById('view-' + name).classList.remove('hidden');
  const active = name === 'workspace' ? 'queue' : name;
  document.querySelectorAll('.navlinks a').forEach(a => a.classList.toggle('active', a.dataset.view === active));
  document.title = `${VIEW_TITLES[name] || 'Review'} · CL Review`;
  CL.state.view = name;
};

CL.mapping = () => CL.state.mappings.find(m => m.id === CL.state.mappingId);

CL.sideStatus = () => {
  const m = CL.mapping();
  const el = document.getElementById('side-status');
  if (!m) { el.innerHTML = '<p>No mapping selected.</p>'; return; }
  el.innerHTML = `
    <div>PL: ${CL.esc(m.pl_connection.tool)} · ${CL.esc(m.pl_project)} ${CL.esc(m.pl_version || '')}</div>
    <div>SL: ${CL.esc(m.sl_connection.tool)} · ${CL.esc(m.sl_scan)}</div>`;
};

CL.loadMappings = async () => {
  const data = await CL.api('GET', '/api/mappings');
  CL.state.mappings = data.items;
  const sel = document.getElementById('mapping-select');
  if (!data.items.length) {
    sel.innerHTML = '<option value="">no mapping configured</option>';
    CL.state.mappingId = null;
    CL.sideStatus();
    return;
  }
  const saved = localStorage.getItem('cl.mapping');
  CL.state.mappingId = CL.state.mappingId || (data.items.find(m => String(m.id) === saved) || data.items[0]).id;
  sel.innerHTML = data.items.map(m => `<option value="${m.id}" ${m.id === CL.state.mappingId ? 'selected' : ''}>${CL.esc(m.name)}</option>`).join('');
  CL.sideStatus();
};

CL.route = async () => {
  // Two navigations can be in flight at once; only the newest one may write to the document.
  const token = ++CL.state.routeToken;
  try {
    await CL._render(token);
  } catch (e) {
    if (token !== CL.state.routeToken) return;
    const el = document.querySelector('.view:not(.hidden)') || document.getElementById('view-overview');
    el.innerHTML = `<h1>Could not load this view</h1>
      <div class="panel"><p class="badge bad">${CL.esc(e.message)}</p>
      <div class="row-actions mt-3"><button id="route-retry" class="primary">Retry</button></div></div>`;
    document.getElementById('route-retry').onclick = () => CL.click(CL._render);
  }
};

CL._render = async (token) => {
  const fresh = () => token === undefined || token === CL.state.routeToken;
  const hash = location.hash.replace(/^#\/?/, '');
  if (!CL.state.mappingId) {
    await CL.loadMappings();
    if (!fresh()) return;                 // a newer navigation won; this view never happened
  }
  if (hash.startsWith('component/')) {
    CL.view('workspace');
    await CL.renderWorkspace(parseInt(hash.split('/')[1], 10), fresh);
    return;
  }
  if (hash === 'queue' || hash.startsWith('queue?')) {
    // A stat tile on the overview links here with the filter already chosen
    // (#/queue?review_status=review_required). Unknown params are ignored, so a stale link
    // degrades to the plain queue rather than to a broken filter.
    CL.queueFromHash = hash.length > 5 && CL.applyQueueHash(hash.slice(6));
    CL.view('queue'); await CL.renderQueue(fresh); return;
  }
  if (hash.startsWith('history')) {
    const job = parseInt(hash.split('/')[2], 10);   // history/job/5
    CL.state.jobId = Number.isFinite(job) ? job : null;
    CL.view('history'); await CL.renderHistory(fresh); return;
  }
  if (hash === 'deploy') { CL.view('deploy'); await CL.renderDeploy(fresh); return; }
  CL.view('overview'); await CL.renderOverview(fresh);
};

// ---------- overview ----------
// Template and bind are separate: the body is one string built from the response, the bindings
// are wired after it is in the document. Same shape as connectionsPanel() + bindOverview().
CL.overviewBody = (m, ov) => {
  const c = ov.counts;
  const sides = ov.sides;
  // A tile is a link only when the queue can answer with EXACTLY that set. Three of the six
  // can (suggested / unmatched / approved-for-sync); "matched" spans several match statuses
  // and "pending review" spans two review statuses, and the queue filters one value at a
  // time — a link there would show a different number than the tile just showed.
  const tile = (n, label, cls, href) => {
    const inner = `<b>${n}</b><span>${label}</span>`;
    return href
      ? `<a class="stat stat-link ${cls}" href="${href}" title="Show these in the review queue">${inner}</a>`
      : `<div class="stat ${cls}">${inner}</div>`;
  };
  return `
    <h1>${CL.esc(m.name)}</h1>
    <div class="stats">
      ${tile(c.matched, 'matched components', '', '')}
      ${tile(c.suggested, 'match suggestions', '', '#/queue?status=suggested')}
      ${tile(c.unmatched, 'unmatched', '', '#/queue?status=unmatched')}
      ${tile(c.pending_review, 'pending review', c.pending_review ? 'is-warn' : '', '')}
      ${tile(c.approved, 'approved for sync', c.approved ? 'is-ok' : '', '#/queue?review_status=approved_for_sync')}
      ${tile(c.conflicts, 'stale approvals', c.conflicts ? 'is-bad' : '', '')}
    </div>
    <div class="grid2 mt-3">
      <div class="panel"><h2>Retrieval status</h2>
        ${['pl', 'sl'].map(s => {
          const x = sides[s];
          const cls = x.status === 'ok' ? 'ok' : (x.status === 'error' ? 'bad' : 'warn');
          return `<div class="kv"><dt>${s.toUpperCase()} tool</dt><dd>${CL.esc(x.tool || '—')} ${CL.badge(x.status, cls)}</dd>
            <dt>retrieved</dt><dd>${CL.esc(x.retrieved_at || 'never')}</dd>
            <dt>components</dt><dd>${x.count}</dd>
            ${x.error ? `<dt>error</dt><dd class="badge bad">${CL.esc(x.error)}</dd>` : ''}</div>`;
        }).join('')}
      </div>
      <div class="panel"><h2>Mapping</h2>
        <div class="kv">
          <dt>PL connection</dt><dd>${CL.esc(m.pl_connection.name)} · ${CL.esc(m.pl_connection.tool)}</dd>
          <dt>PL project</dt><dd>${CL.esc(m.pl_project)} ${CL.esc(m.pl_version || '')}</dd>
          <dt>SL connection</dt><dd>${CL.esc(m.sl_connection.name)} · ${CL.esc(m.sl_connection.tool)}</dd>
          <dt>SL scan</dt><dd>${CL.esc(m.sl_scan)}</dd>
        </div>
      </div>
    </div>
    <div class="panel"><h2>Recent synchronization</h2>
      ${ov.recent_sync.length ? `<table><thead><tr><th>job</th><th>mode</th><th>status</th><th>summary</th><th>created</th></tr></thead><tbody>
        ${ov.recent_sync.map(j => `<tr><td><a href="#/history/job/${j.id}">#${j.id}</a></td><td>${j.dry_run ? 'dry run' : 'live'}</td><td>${CL.badge(j.status, CL.statusClass(j.status))}</td><td class="small">${CL.esc(CL.dash(j.summary_json))}</td><td class="small num">${CL.esc(j.created_at)}</td></tr>`).join('')}
      </tbody></table>` : '<p class="muted">No synchronization yet. Approved components appear here after a dry run.</p>'}
    </div>
  `;
};

CL.renderOverview = async (fresh) => {
  const el = document.getElementById('view-overview');
  const m = CL.mapping();
  if (!m) {
    const catalog = await CL.fetchCatalog();
    el.innerHTML = `<h1>Project overview</h1><div class="panel"><p class="muted">No PL↔SL mapping yet. Configure both connections below, then create a mapping.</p></div>` + CL.connectionsPanel(catalog) + CL.mappingForm();
    CL.bindOverview();
    return;
  }
  // the overview, the connections and the tool catalog are independent reads; one wave
  const [ov, catalog] = await Promise.all([CL.api('GET', `/api/mappings/${m.id}/overview`), CL.fetchCatalog()]);
  if (fresh && !fresh()) return;
  el.innerHTML = CL.overviewBody(m, ov) + CL.connectionsPanel(catalog) + CL.mappingForm();
  CL.bindOverview();
};

// Connections and tools change far less often than the overview; both panels read them once.
CL.fetchCatalog = async () => {
  const [conns, tools] = await Promise.all([CL.api('GET', '/api/connections'), CL.api('GET', '/api/tools')]);
  CL.state.connections = conns.items;
  CL.state.tools = tools.items;
  return { conns: conns.items, tools: tools.items };
};

CL.connectionsPanel = (catalog) => {
  const { conns, tools } = catalog;
  const toolOptions = (role) => tools.filter(t => t.roles.includes(role)).map(t => `<option value="${CL.esc(t.tool)}">${CL.esc(t.tool)}</option>`).join('');
  const rows = conns.map(c => `<tr><td class="num">${c.id}</td><td>${CL.badge(c.role, 'accent')}</td><td>${CL.esc(c.tool)}</td><td>${CL.esc(c.name)}</td><td>${CL.esc(c.base_url || '—')}</td>
    <td class="mid"><div class="stack">${CL.badge(c.last_test_ok ? 'ok' : (c.last_test_at ? 'failed' : 'untested'), c.last_test_ok ? 'ok' : (c.last_test_at ? 'bad' : 'warn'))}
      ${c.last_test_error ? `<span class="small muted">${CL.esc(c.last_test_error)}</span>` : ''}
      <button class="tiny" data-test="${c.id}">Test</button></div></td></tr>`).join('');
  return `<div class="panel"><h2>Connections</h2>
    <table><thead><tr><th>id</th><th>role</th><th>tool</th><th>name</th><th>base url</th><th>connectivity</th></tr></thead><tbody>${rows || '<tr><td colspan="6" class="muted">No connections configured. Add one below.</td></tr>'}</tbody></table>
  </div>
  <div class="panel"><h2>Add connection</h2>
    <div class="form-row">
      <label>role<select id="c-role"><option>pl</option><option>sl</option></select></label>
      <label>tool<select id="c-tool"></select></label>
      <label>name<input id="c-name" placeholder="Prod Black Duck"></label>
      <label>base url<input id="c-url" placeholder="https://blackduck.example"></label>
      <label>token env<input id="c-token-env" placeholder="MY_TOOL_TOKEN"></label>
      <label>user env<input id="c-user-env" placeholder="MY_TOOL_USER"></label>
      <label>password env<input id="c-pass-env" placeholder="MY_TOOL_PASS"></label>
      <label>fixture path<input id="c-fixtures" placeholder="fixtures/demo/pl.json"></label>
      <button id="add-connection" class="primary">Add connection</button>
    </div>
    <p class="small muted">Credentials live only in environment variables named here. Tokens are never returned by the API.</p>
  </div>`;
};

CL.bindOverview = () => {
  const roleSel = document.getElementById('c-role');
  const toolSel = document.getElementById('c-tool');
  if (roleSel && toolSel) {
    const fill = () => {
      toolSel.innerHTML = (CL.state.tools || []).filter(t => t.roles.includes(roleSel.value)).map(t => `<option value="${CL.esc(t.tool)}">${CL.esc(t.tool)}</option>`).join('');
    };
    fill();
    roleSel.onchange = fill;
    document.getElementById('add-connection').onclick = () => CL.click(async () => {
      const config = {};
      const fx = document.getElementById('c-fixtures').value.trim();
      if (fx) config.fixtures = fx;
      const auth = {};
      const tokenEnv = document.getElementById('c-token-env').value.trim();
      const userEnv = document.getElementById('c-user-env').value.trim();
      const passEnv = document.getElementById('c-pass-env').value.trim();
      if (tokenEnv) auth.token = tokenEnv;
      if (userEnv) auth.username = userEnv;
      if (passEnv) auth.password = passEnv;
      await CL.api('POST', '/api/connections', {
        role: roleSel.value, tool: toolSel.value,
        name: document.getElementById('c-name').value.trim() || toolSel.value,
        base_url: document.getElementById('c-url').value.trim(), config, auth_env: auth,
      });
      CL.toast('connection added');
      await CL.loadMappings();
      await CL.renderOverview();
    });
  }
  document.querySelectorAll('[data-test]').forEach(b => b.onclick = () => CL.click(async () => {
    const r = await CL.api('POST', `/api/connections/${b.dataset.test}/test`);
    CL.toast(r.ok ? 'connection ok' : 'test failed: ' + (r.error || ''), !r.ok);
    await CL.renderOverview();
  }));
  const form = document.getElementById('mapping-form');
  if (form) {
    const fill = () => {
      const pls = (CL.state.connections || []).filter(c => c.role === 'pl');
      const sls = (CL.state.connections || []).filter(c => c.role === 'sl');
      document.getElementById('m-pl').innerHTML = pls.map(c => `<option value="${c.id}">${CL.esc(c.name)}</option>`).join('');
      document.getElementById('m-sl').innerHTML = sls.map(c => `<option value="${c.id}">${CL.esc(c.name)}</option>`).join('');
    };
    fill();
    document.getElementById('m-pl').onchange = () => CL.loadProjects();
    document.getElementById('m-sl').onchange = () => CL.loadScans();
    CL.loadProjects(); CL.loadScans();
    document.getElementById('create-mapping').onclick = () => CL.click(async () => {
      const plId = parseInt(document.getElementById('m-pl').value, 10);
      const slId = parseInt(document.getElementById('m-sl').value, 10);
      const project = document.getElementById('m-project').value;
      const version = document.getElementById('m-version').value;
      const scan = document.getElementById('m-scan').value;
      const name = document.getElementById('m-name').value.trim() || `${project} ${version}`;
      const created = await CL.api('POST', '/api/mappings', { name, pl_connection_id: plId, pl_project: project, pl_version: version, sl_connection_id: slId, sl_scan: scan });
      CL.state.mappingId = created.id;
      localStorage.setItem('cl.mapping', String(created.id));
      await CL.loadMappings();
      await CL.refresh();
      await CL.renderOverview();
    });
  }
};

CL.loadProjects = async () => {
  const id = document.getElementById('m-pl').value;
  if (!id) return;
  try {
    const data = await CL.api('GET', `/api/connections/${id}/projects`);
    const proj = document.getElementById('m-project');
    proj.innerHTML = data.items.map(p => `<option value="${p.id}">${CL.esc(p.name)}</option>`).join('');
    const fillVersions = () => {
      const p = data.items.find(x => x.id === proj.value);
      document.getElementById('m-version').innerHTML = ((p && p.versions) || []).map(v => `<option value="${CL.esc(v)}">${CL.esc(v)}</option>`).join('');
    };
    proj.onchange = fillVersions;
    fillVersions();
  } catch (e) { CL.loadFailure('project', e); }
};

CL.loadScans = () => CL.loadScanOptions('m-scan', document.getElementById('m-sl').value, 'scans', 'scan');

CL.mappingForm = () => `<div class="panel"><h2>New mapping</h2>
  <div class="form-row" id="mapping-form">
    <label>PL connection<select id="m-pl"></select></label>
    <label>PL project<select id="m-project"></select></label>
    <label>PL version<select id="m-version"></select></label>
    <label>SL connection<select id="m-sl"></select></label>
    <label>SL scan<select id="m-scan"></select></label>
    <label>name<input id="m-name" placeholder="optional label"></label>
    <button id="create-mapping" class="primary">Create mapping</button>
  </div>
</div>`;

// ---------- queue ----------
// The hash may carry the filter: #/queue?review_status=review_required&sort=discrepancies.
// Only the keys the queue actually understands are read, and only with a value the queue can
// render; anything else is dropped so a mistyped link cannot put a bogus value in a <select>.
const QUEUE_HASH_KEYS = {
  q: (v) => v,
  review_status: (v) => (['not_reviewed', 'review_required', CL.reviewState.IN_PROGRESS,
    CL.reviewState.REVIEWED, CL.reviewState.APPROVED].includes(v) ? v : null),
  status: (v) => (['auto', 'suggested', 'confirmed', 'unmatched', 'rejected', 'manual'].includes(v) ? v : null),
  confidence: (v) => (['high', 'low'].includes(v) ? v : null),
  sort: (v) => (['discrepancies', 'confidence', 'status', 'name'].includes(v) ? v : null),
};

// A hash that names any known filter replaces the whole filter set. That is what makes a
// shared link (#/queue?status=unmatched) show exactly what it says, whatever the reviewer
// had filtered before; a hash naming nothing recognised leaves the current filters alone.
CL.applyQueueHash = (query) => {
  const params = new URLSearchParams(query);
  const picked = [];
  Object.keys(QUEUE_HASH_KEYS).forEach((key) => {
    if (!params.has(key)) return;
    const value = QUEUE_HASH_KEYS[key](params.get(key) || '');
    if (value !== null) picked.push([key, value]);
  });
  if (!picked.length) return false;
  Object.assign(CL.state.queue, { q: '', status: '', review_status: '', confidence: '', sort: 'discrepancies', offset: 0 });
  picked.forEach(([key, value]) => { CL.state.queue[key] = value; });
  return true;
};

// The filter block is built once per mapping, so a filter that arrives from the hash has to
// be written back into the controls — otherwise the selects keep showing the previous
// filter while the rows below are filtered by something else.
CL.syncQueueControls = () => {
  const fields = { q: 'q-search', review_status: 'q-review', status: 'q-status', confidence: 'q-confidence', sort: 'q-sort' };
  Object.keys(fields).forEach((key) => {
    const el = document.getElementById(fields[key]);
    if (el) el.value = CL.state.queue[key];
  });
  const clear = document.getElementById('q-clear');
  if (clear) clear.disabled = !CL.queueDirty();
};

// The filter block is built once per mapping; a search, a page turn or a clear rewrites only
// the rows and the pager. That is what keeps the search box's value and lets focus be restored.
CL.queueFilters = () => `
    <div class="filters">
      <label>search name / version / license<input id="q-search" value="${CL.esc(CL.state.queue.q)}" placeholder="name, version, or license"></label>
      <label>review status<select id="q-review">
        <option value="">any</option>${['not_reviewed', 'review_required', CL.reviewState.IN_PROGRESS, CL.reviewState.REVIEWED, CL.reviewState.APPROVED].map(s => `<option ${CL.state.queue.review_status === s ? 'selected' : ''}>${s}</option>`).join('')}
      </select></label>
      <label>match status<select id="q-status">
        <option value="">any</option>${['auto', 'suggested', 'confirmed', 'unmatched', 'rejected', 'manual'].map(s => `<option ${CL.state.queue.status === s ? 'selected' : ''}>${s}</option>`).join('')}
      </select></label>
      <label>confidence<select id="q-confidence"><option value="">any</option><option value="high" ${CL.state.queue.confidence === 'high' ? 'selected' : ''}>high (≥0.90)</option><option value="low" ${CL.state.queue.confidence === 'low' ? 'selected' : ''}>low (&lt;0.90)</option></select></label>
      <label>sort<select id="q-sort">${[['discrepancies', 'discrepancies (most first)'], ['confidence', 'confidence (least certain first)'], ['status', 'review status'], ['name', 'name A–Z']]
        .map(([v, l]) => `<option value="${v}" ${CL.state.queue.sort === v ? 'selected' : ''}>${l}</option>`).join('')}</select></label>
      <button id="q-apply" class="primary">Search</button>
      <button id="q-clear" ${CL.queueDirty() ? '' : 'disabled'}>Clear filters</button>
    </div>`;

CL.queueRows = (data) => `<table class="stack-rows"><thead><tr><th>PL component</th><th>SL component</th><th>match</th><th>confidence</th><th>licenses</th><th>discrepancies</th><th>review</th></tr></thead><tbody>
      ${data.items.map(r => `<tr class="clickable" data-id="${r.id}">
        <td data-label="PL component"><a href="#/component/${r.id}"><b>${CL.esc(r.pl_name || '—')}</b></a> <span class="muted">${CL.esc(r.pl_version || '')}</span></td>
        <td data-label="SL component">${CL.esc(r.sl_name || '—')} <span class="muted">${CL.esc(r.sl_version || '')}</span></td>
        ${CL.queueMatchCell(r)}
        ${CL.queueConfidenceCell(r)}
        <td data-label="licenses" class="small">${CL.esc((r.licenses || []).join(', ') || '—')}</td>
        <td data-label="discrepancies" class="num">${r.discrepancy_count ? `<span class="badge bad">${r.discrepancy_count}</span>` : '<span class="muted">0</span>'}</td>
        <td data-label="review">${CL.badge(r.review_status, CL.statusClass(r.review_status))}</td></tr>`).join('') || '<tr><td colspan="7" class="muted">No components match these filters. Clear a filter or widen the search.</td></tr>'}
    </tbody></table>
    <div class="pager"><button id="q-prev" ${data.offset === 0 ? 'disabled' : ''}>‹ prev</button>
      <span>${data.total ? data.offset + 1 : 0}–${Math.min(data.offset + data.limit, data.total)} of ${data.total}</span>
      <button id="q-next" ${data.offset + data.limit >= data.total ? 'disabled' : ''}>next ›</button></div>`;

// The method is worth reading when it differs from the status; an unmatched row whose method
// is also "unmatched" only says the same word twice across two columns.
CL.queueMatchCell = (r) => {
  const method = r.method && r.method !== r.status ? ` <span class="small muted">${CL.esc(r.method)}</span>` : '';
  return `<td data-label="match">${CL.badge(r.status, CL.statusClass(r.status))}${method}</td>`;
};

// A bare 0.99 makes 0.99 and 0.72 look equally serious. The meter makes the gap visible at
// a glance; the number stays for the reviewer who wants the exact figure.
CL.queueConfidenceCell = (r) => {
  const pct = Math.round((r.confidence || 0) * 100);
  const cls = r.confidence >= 0.9 ? 'hi' : (r.confidence > 0 ? 'mid' : 'none');
  if (!r.confidence) return '<td data-label="confidence" class="num"><span class="muted">—</span></td>';
  return `<td data-label="confidence" class="num">
    <span class="conf-meter" role="img" aria-label="confidence ${pct} percent"><i class="${cls}" style="width:${pct}%"></i></span>
    <span class="conf-val">${r.confidence.toFixed(2)}</span></td>`;
};

CL.renderQueue = async (fresh) => {
  const el = document.getElementById('view-queue');
  if (!CL.state.mappingId) { el.innerHTML = '<h1>Review queue</h1><p class="muted">Create a mapping first.</p>'; CL.state.queueFor = null; return; }
  const fromHash = CL.queueFromHash;
  CL.queueFromHash = false;
  if (CL.state.queueFor !== CL.state.mappingId || !document.getElementById('queue-list')) {
    el.innerHTML = `<h1>Review queue <span class="muted small" id="queue-count"></span></h1>
      ${CL.queueFilters()}<div class="panel" id="queue-list"></div>`;
    CL.bindQueue();
    CL.state.queueFor = CL.state.mappingId;
  } else if (fromHash) {
    // The controls were built for an earlier filter; show the one the link asked for.
    CL.syncQueueControls();
  }
  const qs = new URLSearchParams({ q: CL.state.queue.q, review_status: CL.state.queue.review_status, status: CL.state.queue.status, confidence: CL.state.queue.confidence, sort: CL.state.queue.sort, offset: CL.state.queue.offset, limit: CL.state.queue.limit });
  const focusKey = CL.focusKey(document.activeElement);
  const data = await CL.api('GET', `/api/mappings/${CL.state.mappingId}/components?${qs}`);
  if (fresh && !fresh()) return;
  document.getElementById('queue-count').textContent = `${data.total} components`;
  document.getElementById('queue-list').innerHTML = CL.queueRows(data);
  CL.bindQueueRows(data);
  // the rows were replaced under the reviewer; hand focus back to the same control
  const back = focusKey && document.querySelector(focusKey);
  if (back) back.focus({ preventScroll: true });
};

CL.bindQueue = () => {
  const fields = { q: 'q-search', review_status: 'q-review', status: 'q-status', confidence: 'q-confidence', sort: 'q-sort' };
  // Live search fires on a pause in typing; Enter and the Search button cancel the pending
  // timer so one intent never turns into two requests.
  const DEBOUNCE_MS = 250;
  let debounce = null;
  // The filter block is not re-rendered on a search or a page turn, so whatever the state change
  // implies for the controls is applied here instead.
  const paint = () => { document.getElementById('q-clear').disabled = !CL.queueDirty(); };
  const sync = (skipSort) => {
    Object.keys(fields).forEach(k => {
      if (skipSort && k === 'sort') return;
      document.getElementById(fields[k]).value = CL.state.queue[k];
    });
  };
  const apply = () => {
    Object.keys(fields).forEach(k => { CL.state.queue[k] = document.getElementById(fields[k]).value; });
    CL.state.queue.offset = 0;
    paint();
    CL.renderQueue();
  };
  document.getElementById('q-apply').onclick = () => { clearTimeout(debounce); apply(); };
  document.getElementById('q-clear').onclick = () => {
    clearTimeout(debounce);
    Object.assign(CL.state.queue, { q: '', status: '', review_status: '', confidence: '', offset: 0 });
    sync(true);
    paint();
    CL.renderQueue();
  };
  // Live search: a keystroke searches after a short pause, so the reviewer stops typing and
  // the list is already there.
  const search = document.getElementById('q-search');
  search.oninput = () => {
    clearTimeout(debounce);
    debounce = setTimeout(() => {
      CL.state.queue.q = search.value;
      CL.state.queue.offset = 0;
      paint();
      CL.renderQueue();
    }, DEBOUNCE_MS);
  };
  search.onkeydown = (e) => { if (e.key === 'Enter') { clearTimeout(debounce); apply(); } };
  ['q-review', 'q-status', 'q-confidence', 'q-sort'].forEach(i => { document.getElementById(i).onchange = apply; });
};

CL.bindQueueRows = (data) => {
  document.getElementById('q-prev').onclick = () => { CL.state.queue.offset = Math.max(0, CL.state.queue.offset - CL.state.queue.limit); CL.renderQueue(); };
  document.getElementById('q-next').onclick = () => { CL.state.queue.offset += CL.state.queue.limit; CL.renderQueue(); };
  document.querySelectorAll('#queue-list tr.clickable').forEach(tr => tr.onclick = () => { location.hash = `#/component/${tr.dataset.id}`; });
};

// ---------- history ----------
// Template first, bindings after: the panels are one string each, built from the responses.
CL.historyPreview = (preview) => `
    <div class="panel"><h2>Pending synchronization preview</h2>
      <p class="small muted">Only components explicitly approved for synchronization appear here. Dry-run first; live writes require the confirmation checkbox and a verified PL connection.</p>
      ${preview.components.length ? `<table><thead><tr><th>component</th><th>approved changes</th></tr></thead><tbody>${preview.components.map(c => `<tr><td>${CL.esc(c.match.pl_name)} ${CL.esc(c.match.pl_version || '')}</td><td>${c.items.map(i => `<div>${CL.esc(i.field)}: ${CL.badge(i.status, CL.statusClass(i.status))} <span class="small">${CL.esc(i.message || '')}</span></div>`).join('')}</td></tr>`).join('')}</tbody></table>
      <div class="checkline mt-2"><button id="sync-dry">Dry run</button><label><input type="checkbox" id="sync-confirm"> I approve these writes</label><button id="sync-live" class="danger" disabled aria-describedby="sync-hint" title="Tick &quot;I approve these writes&quot; first">Execute synchronization</button></div>
      <p class="small muted mt-2" id="sync-hint">Execute stays disabled until you tick the approval box. A dry run writes nothing.</p>`
        : '<p class="muted">Nothing approved yet. Approve components in the review workspace.</p>'}
    </div>`;

CL.historyJobs = (jobs) => `
    <div class="panel"><h2>Jobs</h2>
      ${jobs.items.length ? `<table><thead><tr><th>job</th><th>mode</th><th>status</th><th>summary</th><th>created by</th><th>finished</th></tr></thead><tbody>
        ${jobs.items.map(j => `<tr class="clickable" data-job="${j.id}"><td><a href="#/history/job/${j.id}">#${j.id}</a></td><td>${j.dry_run ? 'dry run' : 'live'}</td><td>${CL.badge(j.status, CL.statusClass(j.status))}</td><td class="small">${CL.esc(CL.dash(JSON.stringify(j.summary)))}</td><td>${CL.esc(j.created_by)}</td><td class="small">${CL.esc(j.finished_at || '—')}</td></tr>`).join('')}
      </tbody></table>` : '<p class="muted">No jobs yet.</p>'}
      <div id="job-detail"></div>
    </div>`;

CL.renderHistory = async (fresh) => {
  const el = document.getElementById('view-history');
  if (!CL.state.mappingId) { el.innerHTML = '<h1>Synchronization history</h1><p class="muted">Create a mapping first.</p>'; return; }
  const [jobs, preview] = await Promise.all([
    CL.api('GET', `/api/sync/jobs?mapping_id=${CL.state.mappingId}`),
    CL.api('POST', '/api/sync/preview', { mapping_id: CL.state.mappingId }),
  ]);
  if (fresh && !fresh()) return;
  el.innerHTML = `<h1>Synchronization history</h1>${CL.historyPreview(preview)}${CL.historyJobs(jobs)}`;
  CL.bindHistory();
  if (CL.state.jobId) await CL.showJob(CL.state.jobId);
};

CL.bindHistory = () => {
  const confirmBox = document.getElementById('sync-confirm');
  if (confirmBox) {
    const live = document.getElementById('sync-live');
    const hint = document.getElementById('sync-hint');
    confirmBox.onchange = () => {
      live.disabled = !confirmBox.checked;
      live.title = confirmBox.checked ? 'Writes the approved values to the PL project list' : 'Tick "I approve these writes" first';
      hint.textContent = confirmBox.checked
        ? 'Live writes go to the PL project list now, item by item; a failed item is reported per field.'
        : 'Execute stays disabled until you tick the approval box. A dry run writes nothing.';
    };
    // a pressed sync button stays pressed until the job answers, so one click is one job
    const runSync = (fn) => CL.click(async () => {
      const btns = [document.getElementById('sync-dry'), live];
      btns.forEach(b => { b.disabled = true; });
      try { await fn(); } finally { btns.forEach(b => { if (b.isConnected) b.disabled = false; }); }
    });
    document.getElementById('sync-dry').onclick = () => runSync(async () => {
      const job = await CL.api('POST', '/api/sync/jobs', { mapping_id: CL.state.mappingId, dry_run: true });
      CL.toast(`dry-run job #${job.id}: ${CL.dash(JSON.stringify(job.summary))}`);
      await CL.renderHistory();
      await CL.showJob(job.id);
    });
    live.onclick = () => runSync(async () => {
      const job = await CL.api('POST', '/api/sync/jobs', { mapping_id: CL.state.mappingId, dry_run: false, confirm: true });
      CL.toast(`synchronization job #${job.id}: ${CL.dash(JSON.stringify(job.summary))}`, job.status === 'failed');
      await CL.renderHistory();
      await CL.showJob(job.id);
    });
  }
  document.querySelectorAll('#view-history tr.clickable[data-job]').forEach(tr => tr.onclick = () => CL.click(() => CL.showJob(parseInt(tr.dataset.job, 10))));
};

CL.showJob = async (id) => {
  CL.state.jobId = id;
  const job = await CL.api('GET', `/api/sync/jobs/${id}`);
  const box = document.getElementById('job-detail');
  if (!box) return;
  box.innerHTML = `<h2>Job #${job.id} · ${job.dry_run ? 'dry run' : 'live'} · ${CL.badge(job.status, CL.statusClass(job.status))}</h2>
    <p class="small muted">${CL.esc(job.created_at)} by ${CL.esc(job.created_by)} · summary ${CL.esc(CL.dash(JSON.stringify(job.summary)))}</p>
    ${job.components.map(c => `<div class="panel"><b>${CL.esc(c.pl_name)} ${CL.esc(c.pl_version || '')}</b>
      <table><thead><tr><th>field</th><th>status</th><th>before</th><th>approved</th><th>api</th><th>message</th></tr></thead><tbody>
      ${c.items.map(i => `<tr><td>${CL.esc(i.field)}</td><td>${CL.badge(i.status, CL.statusClass(i.status))}</td><td class="small">${CL.esc(JSON.stringify(i.pl_before))}</td><td class="small">${CL.esc(String(i.approved_value || '').slice(0, 200))}</td><td>${CL.esc(i.api_status || '')}</td><td class="small">${CL.esc(i.message)}</td></tr>`).join('')}
      </tbody></table></div>`).join('')}
    ${job.items.some(i => i.status === 'failed') ? '<button id="retry-job" class="primary">Retry failed items…</button>' : ''}`;
  const retry = document.getElementById('retry-job');
  if (retry) retry.onclick = () => CL.click(async () => {
    if (!await CL.confirm('Retry failed items against the PL tool?', { title: 'Retry failed items', confirmText: 'Retry' })) return;
    const j = await CL.api('POST', `/api/sync/jobs/${id}/retry`, { confirm: true });
    CL.toast(`retry job #${j.id}: ${CL.dash(JSON.stringify(j.summary))}`, j.status === 'failed');
    await CL.renderHistory();
    await CL.showJob(j.id);
  });
};

// ---------- source deployment ----------
const DEPLOY_AGENTS = [['nomos', 'nomos'], ['copyright_email_author', 'copyright / author'], ['monk', 'monk']];
const DEPLOY_STATS = [['uploaded', 'uploaded', 'is-ok'], ['would_upload', 'to upload', ''], ['already_present', 'already present', ''],
                      ['no_source_url', 'no source URL', 'is-warn'], ['unsupported_registry', 'unknown registry', 'is-warn'],
                      ['upload_failed', 'upload failed', 'is-bad']];
const DEPLOY_LABEL = { uploaded: 'uploaded', already_present: 'already present', no_source_url: 'no source URL',
                       unsupported_registry: 'unknown registry', upload_failed: 'upload failed', would_upload: 'would upload' };
const AGENT_NAME = {};
DEPLOY_AGENTS.forEach(([id, label]) => { AGENT_NAME[id] = label; });
// green only for bytes that reached the scanner; a preview row is not a verdict, so it stays neutral
const DEPLOY_CLASS = { uploaded: 'ok', upload_failed: 'bad', no_source_url: 'warn', unsupported_registry: 'warn',
                       would_upload: '', already_present: '' };

// a timestamp inside a sentence reads as prose, not as a database column
CL.stamp = (iso) => {
  const m = /^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})/.exec(String(iso || ''));
  return m ? `${m[1]} ${m[2]}` : String(iso || '');
};
// the report opens on what the operator has to act on, not on what went right
const DEPLOY_RANK = { upload_failed: 0, no_source_url: 1, unsupported_registry: 2, uploaded: 3, would_upload: 3, already_present: 4 };

CL.deployForm = () => ({
  pl_connection_id: parseInt(document.getElementById('dp-pl').value, 10),
  sl_connection_id: parseInt(document.getElementById('dp-sl').value, 10),
  project_id: document.getElementById('dp-project').value,
  version_id: document.getElementById('dp-version').value,
  folder: document.getElementById('dp-folder').value.trim() || null,
  parent_folder: document.getElementById('dp-parent').value ? parseInt(document.getElementById('dp-parent').value, 10) : null,
  agents: DEPLOY_AGENTS.filter(([v]) => document.getElementById('dp-agent-' + v).checked).map(([v]) => v),
});

CL.deployMarkStale = () => {
  const d = CL.state.deploy;
  d.stale = !!d.report && JSON.stringify(CL.deployForm()) !== d.signature;
  CL.updateDeployGate();
};

CL.deploySummary = (r) => {
  const parts = DEPLOY_STATS.filter(([k]) => r.counts[k]).map(([k, label]) => `${r.counts[k]} ${label}`);
  return parts.length ? parts.join(', ') : 'nothing to deploy';
};

CL.updateDeployGate = () => {
  const run = document.getElementById('dp-run');
  if (!run) return;
  const d = CL.state.deploy;
  const approved = document.getElementById('dp-confirm').checked;
  let blocked = '';
  if (!d.report) blocked = 'Preview resolves every source URL and writes nothing. Upload stays disabled until a preview matches this target.';
  else if (d.stale) blocked = 'This target changed after the preview. Preview again before uploading.';
  else if (!approved) blocked = 'Upload stays disabled until you tick the approval box. A preview writes nothing.';
  run.disabled = !!blocked;
  run.title = blocked || 'Creates the folder and one upload per library in the scanner';
  document.getElementById('dp-hint').textContent = blocked
    || 'Uploads go to the scanner now. Nothing reaches the project list until you synchronize it.';
};

// Template first, bindings after - same shape as the overview and the queue.
CL.deployTarget = (pls, sls) => {
  const options = (list) => list.map(c => `<option value="${c.id}">${CL.esc(c.name)}</option>`).join('');
  return `<h1>Source deployment</h1>
    <p class="note">Every library of a project version is resolved to its source archive and uploaded into one scanner folder. That folder opens here as a scan, so the reviewed values can be synchronized back.</p>
    <div class="panel"><h2>Deployment target</h2>
      <div class="field-grid">
        <label>project-level tool<select id="dp-pl">${options(pls)}</select></label>
        <label>project<select id="dp-project"></select></label>
        <label>version<select id="dp-version"></select></label>
        <label>source-level tool<select id="dp-sl">${options(sls)}</select></label>
        <label>parent folder<select id="dp-parent"></select></label>
        <label>folder name<input id="dp-folder" placeholder="{project}__{version}"></label>
      </div>
      <div class="field-grid mt-3">
        <div>
          <span class="field-label" id="dp-agents-label">scanner agents per upload</span>
          <span class="checkline" role="group" aria-labelledby="dp-agents-label">
            ${DEPLOY_AGENTS.map(([v, l], i) => `<label><input type="checkbox" id="dp-agent-${v}" ${i < 2 ? 'checked' : ''}>${l}</label>`).join('')}
          </span>
        </div>
      </div>
      <div class="checkline mt-3">
        <label><input type="checkbox" id="dp-confirm"> I approve these uploads</label>
        <button id="dp-preview">Preview sources</button>
        <button id="dp-run" class="primary" disabled aria-describedby="dp-hint">Upload to scanner</button>
      </div>
      <p class="small muted mt-2" id="dp-hint"></p>
    </div>
    <div id="dp-report"></div>`;
};

CL.bindDeploy = () => {
  document.getElementById('dp-pl').onchange = () => { CL.loadDeployProjects(); CL.deployMarkStale(); };
  document.getElementById('dp-sl').onchange = () => { CL.loadDeployFolders(); CL.deployMarkStale(); };
  ['dp-project', 'dp-version', 'dp-parent'].forEach(id => { document.getElementById(id).onchange = CL.deployMarkStale; });
  const nameInput = document.getElementById('dp-folder');
  nameInput.onchange = CL.deployMarkStale;
  nameInput.oninput = CL.deployMarkStale;
  DEPLOY_AGENTS.forEach(([v]) => { document.getElementById('dp-agent-' + v).onchange = CL.deployMarkStale; });
  document.getElementById('dp-confirm').onchange = CL.updateDeployGate;
  document.getElementById('dp-preview').onclick = () => CL.runDeploy(true);
  document.getElementById('dp-run').onclick = () => CL.runDeploy(false);
};

CL.renderDeploy = async (fresh) => {
  const el = document.getElementById('view-deploy');
  const [conns, tools] = await Promise.all([CL.api('GET', '/api/connections'), CL.api('GET', '/api/tools')]);
  if (fresh && !fresh()) return;
  const canDeploy = {};
  tools.items.forEach(t => { canDeploy[t.tool] = t.deploy; });
  const pls = conns.items.filter(c => c.role === 'pl' && canDeploy[c.tool]);
  const sls = conns.items.filter(c => c.role === 'sl' && canDeploy[c.tool]);
  if (!pls.length || !sls.length) {
    el.innerHTML = `<h1>Source deployment</h1><div class="panel">
      <p class="muted">Deployment needs a project-list connection that reports its libraries and a scanner connection that accepts uploads — Black Duck and FOSSology today. Add one of each first.</p>
      <p class="mt-2"><a href="#/overview">Open the project overview</a></p></div>`;
    return;
  }
  el.innerHTML = CL.deployTarget(pls, sls);
  CL.bindDeploy();
  await CL.loadDeployProjects();
  await CL.loadDeployFolders();
  if (CL.state.deploy.report) CL.renderDeployReport();
  CL.updateDeployGate();
};

CL.loadDeployProjects = async () => {
  const id = document.getElementById('dp-pl').value;
  if (!id) return;
  try {
    const data = await CL.api('GET', `/api/connections/${id}/projects`);
    const proj = document.getElementById('dp-project');
    proj.innerHTML = data.items.map(p => `<option value="${CL.esc(p.id)}">${CL.esc(p.name)}</option>`).join('')
      || '<option value="">no projects</option>';
    const fillVersions = () => {
      const p = data.items.find(x => x.id === proj.value);
      const ver = document.getElementById('dp-version');
      ver.innerHTML = ((p && p.versions) || []).map(v => `<option value="${CL.esc(v)}">${CL.esc(v)}</option>`).join('')
        || '<option value="">no versions</option>';
      document.getElementById('dp-folder').placeholder = `${proj.value}__${ver.value}`;
      CL.deployMarkStale();
    };
    proj.onchange = fillVersions;
    fillVersions();
  } catch (e) { CL.loadFailure('project', e); }
};

CL.loadDeployFolders = async () => {
  await CL.loadScanOptions('dp-parent', document.getElementById('dp-sl').value, 'folders', 'folder');
  CL.deployMarkStale();
};

CL.runDeploy = (dryRun) => CL.click(async () => {
  const buttons = [document.getElementById('dp-preview'), document.getElementById('dp-run')];
  buttons.forEach(b => { b.disabled = true; });
  try {
    const form = CL.deployForm();
    const signature = JSON.stringify(form);
    const report = await CL.api('POST', '/api/deploy', Object.assign({}, form, { dry_run: dryRun }));
    const d = CL.state.deploy;
    d.report = report;
    d.signature = signature;
    d.stale = false;
    d.request = form;
    d.names = {
      project: document.getElementById('dp-project').selectedOptions[0]?.text || form.project_id,
      version: form.version_id,
    };
    CL.toast(`${dryRun ? 'preview' : 'deployment'}: ${CL.deploySummary(report)}`, report.counts.upload_failed > 0);
    CL.renderDeployReport();
  } finally {
    buttons.forEach(b => { if (b.isConnected) b.disabled = false; });
    CL.updateDeployGate();
  }
});

CL.renderDeployReport = () => {
  const box = document.getElementById('dp-report');
  if (!box) return;
  const r = CL.state.deploy.report;
  const rows = r.components.slice().sort((a, b) =>
    (DEPLOY_RANK[a.status] ?? 5) - (DEPLOY_RANK[b.status] ?? 5) || String(a.name).localeCompare(String(b.name)));
  const stats = DEPLOY_STATS.filter(([k]) => r.counts[k])
    .map(([k, label, cls]) => `<div class="stat ${cls}"><b>${r.counts[k]}</b><span>${label}</span></div>`).join('');
  const cta = !r.dry_run && r.folder.id ? `<div class="row-actions mt-3">
      <button id="dp-mapping">Create mapping from this folder</button>
      <span class="small muted">Reviews the whole folder against ${CL.esc(CL.state.deploy.names.project || r.project)} in one mapping.</span>
    </div>` : '';
  box.innerHTML = `<div class="panel">
    <h2>${r.dry_run ? 'Preview' : 'Deployment'} · <code>${CL.esc(r.folder.name)}</code></h2>
    <p class="small muted">${r.folder.id
      ? `scanner folder id ${r.folder.id} · ${r.folder.created ? 'created by this run' : 'already existed'} · `
      : 'no folder is created before a live run · '}
      agents ${CL.esc(r.agents.map(a => AGENT_NAME[a] || a).join(', '))} · ${CL.esc(CL.stamp(r.generated_at))}</p>
    ${stats ? `<div class="stats mt-3">${stats}</div>` : '<p class="muted mt-3">No components on this project version.</p>'}
    ${(r.warnings || []).map(w => `<p class="small mt-2">${CL.badge('warning', 'warn')} ${CL.esc(w)}</p>`).join('')}
    ${rows.length ? `<table class="stack-rows deploy-table mt-3"><thead><tr><th>component</th><th>registry</th><th>source</th><th>status</th><th>detail</th></tr></thead><tbody>
      ${rows.map(x => `<tr>
        <td data-label="component"><b>${CL.esc(x.name)}</b> <span class="muted">${CL.esc(x.version || '')}</span></td>
        <td data-label="registry">${CL.esc(x.registry || '—')}</td>
        <td data-label="source">${x.url ? `<code>${CL.esc(x.url)}</code>` : '<span class="muted">—</span>'}</td>
        <td data-label="status">${CL.badge(DEPLOY_LABEL[x.status] || x.status, DEPLOY_CLASS[x.status] || '')}</td>
        <td data-label="detail" class="small muted">${CL.esc(x.message)}</td></tr>`).join('')}
      </tbody></table>` : ''}
    ${cta}</div>`;
  const ctaBtn = document.getElementById('dp-mapping');
  if (ctaBtn) ctaBtn.onclick = () => CL.click(async () => {
    const d = CL.state.deploy;
    const created = await CL.api('POST', '/api/mappings', {
      name: `${d.names.project} ${d.names.version}`.trim(),
      pl_connection_id: d.request.pl_connection_id, pl_project: d.request.project_id,
      pl_version: d.request.version_id, sl_connection_id: d.request.sl_connection_id,
      sl_scan: `folder:${d.report.folder.id}`,
    });
    CL.state.mappingId = created.id;
    localStorage.setItem('cl.mapping', String(created.id));
    await CL.loadMappings();
    location.hash = '#/overview';
    await CL.refresh();
  });
};

// ---------- shell ----------
// The refresh takes a while and touches both tools, so the button that started it says so:
// the global bar alone does not tell you which control is busy.
CL.setRefreshBusy = (busy) => {
  const btn = document.getElementById('refresh-btn');
  if (!btn) return;
  btn.classList.toggle('is-busy', busy);
  btn.disabled = busy;
  const label = btn.querySelector('.btn-label');
  if (label) label.textContent = busy ? 'Refreshing…' : 'Refresh snapshots';
};

CL.refresh = () => CL.run(async () => {
  if (!CL.state.mappingId) throw new Error('no mapping selected');
  CL.setRefreshBusy(true);
  try {
    const r = await CL.api('POST', `/api/mappings/${CL.state.mappingId}/refresh`);
    const bad = Object.entries(r.sides).filter(([, v]) => v.status === 'error');
    CL.toast(bad.length ? 'refresh incomplete: ' + bad.map(([k, v]) => `${k}: ${v.error}`).join('; ') : `refreshed: PL ${r.sides.pl.count}, SL ${r.sides.sl.count}`, bad.length > 0);
    await CL.route();
  } finally {
    CL.setRefreshBusy(false);
  }
});

document.getElementById('refresh-btn').onclick = CL.refresh;
document.getElementById('mapping-select').onchange = (e) => {
  CL.state.mappingId = parseInt(e.target.value, 10) || null;
  localStorage.setItem('cl.mapping', String(CL.state.mappingId));
  CL.state.queue.offset = 0;
  CL.sideStatus();
  CL.route();
};
document.getElementById('reviewer').value = localStorage.getItem('cl.reviewer') || '';
document.getElementById('reviewer').onchange = (e) => localStorage.setItem('cl.reviewer', e.target.value.trim());
document.getElementById('reviewer').oninput = CL.clearReviewerError;

const applyTheme = (t) => {
  document.documentElement.dataset.theme = t;
  localStorage.setItem('cl.theme', t);
  const btn = document.getElementById('theme-toggle');
  btn.setAttribute('aria-pressed', String(t === 'dark'));
  btn.setAttribute('aria-label', t === 'dark' ? 'Switch to light theme' : 'Switch to dark theme');
};
applyTheme(localStorage.getItem('cl.theme') || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'));
document.getElementById('theme-toggle').onclick = () =>
  applyTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark');

// Screen-reader table semantics for every table rendered later.
new MutationObserver(() => {
  document.querySelectorAll('#main th:not([scope])').forEach(th => { th.scope = 'col'; });
}).observe(document.getElementById('main'), { childList: true, subtree: true });

// ---------- keyboard ----------
// A reviewer triages hundreds of rows with the same three motions, so the motions get keys.
// Everything here is deliberately suppressed while the reviewer is typing or a dialog is
// open, and the two destructive actions (reject / false positive) get NO key at all: the
// product is human-gated on purpose, and a shortcut is not worth a wrong keypress.
CL.modalOpen = () => !!document.querySelector('.modal-backdrop');
CL.typingHere = () => {
  const el = document.activeElement;
  return !!el && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.tagName === 'SELECT' || el.isContentEditable);
};

// The rows a cursor can sit on: queue rows, and workspace rows that carry decision buttons.
CL.kbdRows = () => {
  if (CL.state.view === 'queue') return [...document.querySelectorAll('#queue-list tr.clickable')];
  if (CL.state.view === 'workspace') {
    return [...document.querySelectorAll('#view-workspace tbody tr')].filter(tr => tr.querySelector('[data-dec]'));
  }
  return [];
};

CL.kbdCursor = () => document.querySelector('.kbd-cursor');

CL.moveCursor = (delta) => {
  const rows = CL.kbdRows();
  if (!rows.length) return false;
  const current = rows.indexOf(CL.kbdCursor());
  rows.forEach(r => r.classList.remove('kbd-cursor'));
  const next = current === -1
    ? (delta > 0 ? 0 : rows.length - 1)
    : Math.max(0, Math.min(rows.length - 1, current + delta));
  const row = rows[next];
  row.classList.add('kbd-cursor');
  row.scrollIntoView({ block: 'nearest' });
  return true;
};

// Act on the cursor row, falling back to the first row that offers the action so a single
// key still works before the reviewer has moved anywhere.
CL.rowAction = (action) => {
  const row = CL.kbdCursor() || CL.kbdRows()[0];
  if (!row) return false;
  const btn = row.querySelector(`[data-action="${action}"]`);
  if (!btn || btn.disabled) return false;
  btn.click();
  return true;
};

CL.openCursor = () => {
  const row = CL.kbdCursor();
  if (!row) return false;
  if (CL.state.view === 'queue' && row.dataset.id) { location.hash = `#/component/${row.dataset.id}`; return true; }
  const link = row.querySelector('a[href^="#/"]');
  if (link) { link.click(); return true; }
  return false;
};

CL.showShortcuts = () => {
  if (CL.modalOpen()) return;
  const rows = [
    ['/', 'focus the queue search'],
    ['j / k', 'move the row cursor down / up'],
    ['Enter or o', 'open the row under the cursor'],
    ['a', 'Approve SL on the current row'],
    ['p', 'Keep PL on the current row'],
    ['e', 'Correct the current row'],
    ['n', 'focus the review note box'],
    ['?', 'show this list'],
    ['Esc', 'close a dialog'],
  ];
  const back = document.createElement('div');
  back.className = 'modal-backdrop';
  back.innerHTML = `<div class="modal" role="dialog" aria-modal="true" aria-labelledby="sc-title">
      <h2 id="sc-title">Keyboard shortcuts</h2>
      <p>Nothing destructive is bound to a key. Reject and False positive stay mouse-only.</p>
      <table class="sc-table"><tbody>
        ${rows.map(([k, v]) => `<tr><td><kbd>${CL.esc(k)}</kbd></td><td>${CL.esc(v)}</td></tr>`).join('')}
      </tbody></table>
      <div class="modal-actions"><button type="button" class="primary" data-x="close">Close</button></div>
    </div>`;
  document.body.appendChild(back);
  const close = () => {
    document.removeEventListener('keydown', onKey, true);
    back.remove();
  };
  const onKey = (e) => { if (e.key === 'Escape') { e.preventDefault(); close(); } };
  document.addEventListener('keydown', onKey, true);
  back.querySelector('[data-x=close]').onclick = close;
  back.onclick = (e) => { if (e.target === back) close(); };
};

document.addEventListener('keydown', (e) => {
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  if (CL.modalOpen()) return;
  // A key pressed while the reviewer is typing belongs to the text, not to the app.
  if (CL.typingHere()) {
    if (e.key === 'Escape' && document.activeElement && document.activeElement.blur) document.activeElement.blur();
    return;
  }
  const view = CL.state.view;
  if (e.key === '/') {
    const search = document.getElementById('q-search');
    if (search) { e.preventDefault(); search.focus(); search.select(); }
    return;
  }
  if (e.key === '?') { e.preventDefault(); CL.showShortcuts(); return; }
  if (e.key === 'j' || e.key === 'k') {
    if (CL.moveCursor(e.key === 'j' ? 1 : -1)) e.preventDefault();
    return;
  }
  if (e.key === 'Enter' || e.key === 'o') {
    if (CL.openCursor()) e.preventDefault();
    return;
  }
  if (view !== 'workspace') return;
  if (e.key === 'a') { if (CL.rowAction('approve_sl')) e.preventDefault(); return; }
  if (e.key === 'p') { if (CL.rowAction('accept_pl')) e.preventDefault(); return; }
  if (e.key === 'e') { if (CL.rowAction('correct')) e.preventDefault(); return; }
  if (e.key === 'n') {
    const note = document.getElementById('note-body');
    if (note) { e.preventDefault(); note.focus(); }
  }
});

window.addEventListener('hashchange', () => CL.route());

(async () => {
  if (location.hash.startsWith('#/component/')) {
    await CL.loadMappings();
    await CL.route();
    return;
  }
  await CL.loadMappings();
  await CL.route();
})();
