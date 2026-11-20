'use strict';
window.CL = window.CL || {};
var CL = window.CL;

CL.state = { mappingId: null, mappings: [], queueFor: null, inflight: 0, writes: 0, routeToken: 0, reviewer: '', queue: { q: '', status: '', review_status: '', confidence: '', sort: 'discrepancies', offset: 0, limit: 50 }, jobId: null, pick: { ids: new Set(), all: false, total: 0 }, deploy: { report: null, signature: null, stale: false, request: null, names: {} } };

// A filter is anything that hides rows; sort order is not a filter.
CL.queueDirty = () => { const f = CL.state.queue; return !!(f.q || f.status || f.review_status || f.confidence); };

CL.esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
CL.badge = (text, cls) => `<span class="badge ${cls || ''}">${CL.esc(text)}</span>`;
// The three review states a reviewer can reach by acting; the wire values live here once.
CL.reviewState = Object.freeze({ IN_PROGRESS: 'in_progress', REVIEWED: 'reviewed', APPROVED: 'approved_for_sync' });

CL.statusClass = (s) => ({ succeeded: 'ok', synchronized: 'ok', completed: 'ok', approved_for_sync: 'ok', ready: 'ok', reviewed: 'accent', failed: 'bad', conflict: 'bad', stale: 'warn', unsupported_target: 'warn', review_required: 'warn', in_progress: 'warn', suggested: 'warn', skipped: 'warn', skipped_already_applied: 'warn', skipped_prerequisite: 'warn', interrupted: 'warn', unmatched: 'bad', rejected: 'bad' }[s] || '');
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

// Every request shows the top bar at once; one that runs past SLOW_MS also says so in words, so a
// slow tool reads as "working", not as a frozen page. Neither blocks input.
CL.SLOW_MS = 400;
CL.busyStart = (write) => {
  CL.state.inflight = (CL.state.inflight || 0) + 1;
  if (write) CL.state.writes += 1;
  document.body.classList.add('busy');
  if (!CL.state.slowTimer) {
    CL.state.slowTimer = setTimeout(() => {
      document.getElementById('busy-note').textContent = CL.state.writes ? 'Saving…' : 'Loading…';
      document.body.classList.add('slow');
    }, CL.SLOW_MS);
  }
};
CL.busyEnd = (write) => {
  CL.state.inflight = Math.max(0, CL.state.inflight - 1);
  if (write) CL.state.writes = Math.max(0, CL.state.writes - 1);
  if (CL.state.inflight) return;
  clearTimeout(CL.state.slowTimer);
  CL.state.slowTimer = null;
  document.body.classList.remove('busy', 'slow');
  document.getElementById('busy-note').textContent = '';
};

CL.api = async (method, path, body) => {
  if (method !== 'GET' && CL.READ_POSTS.indexOf(path.split('?')[0]) === -1 && !CL.requireReviewer()) {
    throw CL.NEEDS_REVIEWER;
  }
  const write = method !== 'GET';
  CL.busyStart(write);
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
    CL.busyEnd(write);
  }
};

// File downloads need the raw body, not the JSON parse CL.api does.
CL.download = async (path, name) => {
  CL.busyStart(false);
  try {
    const res = await fetch(path, { headers: { 'X-Reviewer': CL.reviewer() } });
    if (!res.ok) {
      const data = await res.json().catch(() => ({ error: res.statusText }));
      throw new Error(data.error || `HTTP ${res.status}`);
    }
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = name;
    document.body.appendChild(a); a.click(); a.remove();
    URL.revokeObjectURL(url);
    CL.toast('report downloaded');
  } catch (err) {
    CL.toast(err.message, true);
  } finally {
    CL.busyEnd(false);
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
CL.FILTER_AT = 12;
CL.loadScanOptions = async (selectId, connectionId, path, noun) => {
  if (!connectionId) return;
  const el = document.getElementById(selectId);
  CL.selectLoading(el);
  try {
    const data = await CL.api('GET', `/api/connections/${connectionId}/${path}`);
    const items = data.items;
    const render = (list) => {
      el.innerHTML = list
        .map(o => `<option value="${CL.esc(o.id)}" data-name="${CL.esc(o.name)}">${CL.esc(o.name)}${o.date ? ' ' + CL.esc(o.date) : ''}</option>`).join('')
        || `<option value="">${items.length ? 'no match' : 'none'}</option>`;
    };
    render(items);
    // a filter box, only for a list too long to scan by eye: paste or type any part of a name
    let box = document.getElementById(selectId + '-filter');
    if (items.length < CL.FILTER_AT) { if (box) box.remove(); return; }
    if (!box) {
      box = document.createElement('input');
      box.id = selectId + '-filter';
      box.placeholder = `filter ${noun}s: paste or type a name`;
      el.parentNode.insertBefore(box, el);
    }
    box.value = '';
    box.oninput = () => {
      const q = box.value.trim().toLowerCase();
      render(q ? items.filter(o => String(o.name).toLowerCase().includes(q)) : items);
      const exact = items.find(o => String(o.name).toLowerCase() === q);
      if (exact) el.value = String(exact.id);
    };
  } catch (e) {
    el.innerHTML = '<option value="">unavailable</option>';
    CL.loadFailure(noun, e);
  }
};

// `html` is a trusted body built by the caller, which escapes everything it interpolates.
CL.dialog = ({ title, message, html = '', field, value = '', multiline = false, confirmText = 'Save', danger = false }) => new Promise(resolve => {
  const prev = document.activeElement;
  const back = document.createElement('div');
  back.className = 'modal-backdrop';
  back.innerHTML = `<div class="modal" role="dialog" aria-modal="true" aria-labelledby="modal-title">
      <h2 id="modal-title">${CL.esc(title)}</h2>
      ${message ? `<p>${CL.esc(message)}</p>` : ''}
      ${html}
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
  // A double-click must not queue two writes, so a new action waits while a write is in flight.
  // Reads never count (tool names, project lists, a slow view): with them counted, one slow tool
  // refused every click on the page.
  if (CL.state.writes) { CL.toast('still saving — one moment'); return undefined; }
  try { return await fn(); }
  // the inline error by the Reviewer box already says what to do; do not stack a toast on it
  catch (e) { if (e === CL.NEEDS_REVIEWER) return undefined; CL.toast(e.message, true); throw e; }
};

// A click is fire-and-forget: CL.run has already told the reviewer what failed, and nothing is
// waiting on the promise, so letting it reject only prints an unhandled rejection.
CL.click = (fn) => { CL.run(fn).catch(() => {}); };

const VIEW_TITLES = { overview: 'Project overview', queue: 'Review queue', workspace: 'Component review', history: 'Synchronization history', deploy: 'Source deployment', settings: 'Settings & health' };

CL.view = (name) => {
  document.querySelectorAll('.view').forEach(v => v.classList.add('hidden'));
  document.getElementById('view-' + name).classList.remove('hidden');
  const active = name === 'workspace' ? 'queue' : name;
  document.querySelectorAll('.navlinks a').forEach(a => a.classList.toggle('active', a.dataset.view === active));
  document.title = `${VIEW_TITLES[name] || 'Review'} · CL Review`;
  CL.state.view = name;
};

CL.mapping = () => CL.state.mappings.find(m => m.id === CL.state.mappingId);

// The stored mapping holds ids (a Black Duck project uuid, "folder:292"); the tools know the names.
// One live read per mapping, cached, shared by the sidebar and the overview. A failed read is
// cached as null so a tool that is down is not asked again on every navigation.
CL.fetchTargets = (m) => {
  CL.state.targets = CL.state.targets || {};
  if (!(m.id in CL.state.targets)) {
    CL.state.targets[m.id] = CL.api('GET', `/api/mappings/${m.id}/targets`).catch(() => null);
  }
  return CL.state.targets[m.id];
};

// Each line is truncated with the full text on hover, so a long id never widens the sidebar.
CL.sideStatus = () => {
  const m = CL.mapping();
  const el = document.getElementById('side-status');
  if (!m) { el.innerHTML = '<p>No mapping selected.</p>'; return; }
  const line = (side, tool, ids, t) => {
    const text = `${tool} · ${(t && t.label) || ids}`;
    return `<div class="side-line" title="${CL.esc(`${side}: ${text}`)}"><span class="muted">${side}</span> ${CL.esc(text)}</div>`;
  };
  const paint = (targets) => {
    if (CL.mapping() !== m) return;          // the reviewer switched mapping meanwhile
    el.innerHTML = line('PL', m.pl_connection.tool, `${m.pl_project} ${m.pl_version || ''}`.trim(), targets && targets.pl)
      + line('SL', m.sl_connection.tool, m.sl_scan, targets && targets.sl);
  };
  paint(null);
  CL.fetchTargets(m).then(paint);
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
  sel.title = (CL.mapping() || {}).name || '';
  CL.sideStatus();
};

CL.route = async () => {
  // Two navigations can be in flight at once; only the newest one may write to the document.
  const token = ++CL.state.routeToken;
  // the view stays usable while it loads; it only dims (after a short delay, in CSS) to say so
  const main = document.getElementById('main');
  main.setAttribute('aria-busy', 'true');
  try {
    await CL._render(token);
  } catch (e) {
    if (token !== CL.state.routeToken) return;
    const el = document.querySelector('.view:not(.hidden)') || document.getElementById('view-overview');
    el.innerHTML = `<h1>Could not load this view</h1>
      <div class="panel"><p class="badge bad">${CL.esc(e.message)}</p>
      <div class="row-actions mt-3"><button id="route-retry" class="primary">Retry</button></div></div>`;
    document.getElementById('route-retry').onclick = () => CL.click(CL._render);
  } finally {
    if (token === CL.state.routeToken) main.removeAttribute('aria-busy');
  }
};

// A select whose options come from a tool says so while they load, instead of looking empty.
CL.selectLoading = (el) => { if (el) el.innerHTML = '<option value="">loading…</option>'; };

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
  if (hash === 'settings') { CL.view('settings'); await CL.renderSettings(fresh); return; }
  CL.view('overview'); await CL.renderOverview(fresh);
};

// ---------- readouts ----------
// A wire value is shown as words; the raw value stays one hover away (title), because the
// evidence must stay inspectable.
CL.words = (s) => String(s == null ? '' : s).replace(/_/g, ' ');
CL.status = (s) => `<span class="badge ${CL.statusClass(s)}" title="${CL.esc(s)}">${CL.esc(CL.words(s))}</span>`;
CL.when = (iso, empty = 'never') => {
  if (!iso) return `<span class="muted">${CL.esc(empty)}</span>`;
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return CL.esc(iso);
  const s = Math.max(0, Math.round((Date.now() - t) / 1000));
  const rel = s < 60 ? 'just now' : s < 3600 ? `${Math.floor(s / 60)} min ago`
    : s < 86400 ? `${Math.floor(s / 3600)} h ago` : s < 30 * 86400 ? `${Math.floor(s / 86400)} d ago` : iso.slice(0, 10);
  return `<time datetime="${CL.esc(iso)}" title="${CL.esc(iso)}">${rel}</time>`;
};
CL.duration = (s) => {
  if (s == null) return 'unknown';
  if (s < 1) return 'under a second';
  if (s < 60) return `${Math.round(s)} s`;
  if (s < 3600) return `${Math.floor(s / 60)} min ${Math.round(s % 60)} s`;
  return `${Math.floor(s / 3600)} h ${Math.round((s % 3600) / 60)} min`;
};
// A job summary is a JSON object of counts ({"succeeded": 1}); it reads as "1 succeeded".
CL.jobResult = (summary) => {
  let o = summary;
  if (typeof o === 'string') { try { o = JSON.parse(o); } catch (e) { return CL.esc(CL.dash(o)); } }
  const parts = Object.entries(o || {}).filter(([, n]) => n).map(([k, n]) => `${n} ${CL.words(k)}`);
  return parts.length ? `<span title="${CL.esc(JSON.stringify(o))}">${CL.esc(parts.join(' · '))}</span>` : '—';
};
CL.connHealth = (c) => CL.badge(c.last_test_ok ? 'ok' : (c.last_test_at ? 'failed' : 'untested'),
  c.last_test_ok ? 'ok' : (c.last_test_at ? 'bad' : 'warn'));
// One jobs table for every view that lists jobs, so the columns never drift apart.
CL.jobsTable = (items, clickable) => `<table><thead><tr><th>job</th><th>mode</th><th>status</th><th>result</th><th>created by</th><th>finished</th></tr></thead><tbody>
  ${items.map(j => `<tr${clickable ? ` class="clickable" data-job="${j.id}"` : ''}><td><a href="#/history/job/${j.id}">#${j.id}</a></td><td>${j.dry_run ? 'dry run' : 'live'}</td><td>${CL.status(j.status)}</td><td class="small">${CL.jobResult(j.summary !== undefined ? j.summary : j.summary_json)}</td><td>${CL.esc(j.created_by || '—')}</td><td class="small">${CL.when(j.finished_at, '—')}</td></tr>`).join('')}
  </tbody></table>`;

// ---------- view zones ----------
// Every view is header -> attention -> work -> reference (.github/skills/ui-layout/SKILL.md).
// The header holds the h1, one context line, and the view's actions on the right.
CL.viewHead = (title, ctx, actions) => `<div class="view-head">
    <div><h1 tabindex="-1">${title}</h1>${ctx ? `<p class="view-ctx">${ctx}</p>` : ''}</div>
    ${actions ? `<div class="row-actions">${actions}</div>` : ''}
  </div>`;
// Attention renders only when something needs it; each item is trusted HTML built by the caller.
CL.attention = (items) => items.length
  ? `<div class="panel attention" role="region" aria-label="Needs attention"><h2>Needs attention</h2><ul class="warnlist">${items.map(t => `<li>${t}</li>`).join('')}</ul></div>` : '';
// One context line for every view: ids first, upgraded to names and links by resolveMappingTargets.
CL.mappingCtx = (m) => m ? `<span class="muted">PL</span> <span class="map-target" data-side="pl">${CL.esc(m.pl_project)} ${CL.esc(m.pl_version || '')}</span>
  <span class="muted">↔</span> <span class="muted">SL</span> <span class="map-target" data-side="sl">${CL.esc(m.sl_scan)}</span>` : '';

// ---------- overview ----------
// Job: see where this mapping stands and which queue slice to open next. Connection and
// mapping administration lives in Settings (see .github/skills/ui-layout/VIEWS.md).
CL.overviewBody = (m, ov, conns) => {
  const c = ov.counts;
  const sides = ov.sides;
  const conn = (s) => conns.find(k => k.id === m[`${s}_connection_id`]) || m[`${s}_connection`] || {};
  // A tile is a link only when the queue can answer with EXACTLY that set. "Pending review"
  // spans two review statuses, "stale approvals" is not a queue filter, and "matched" spans
  // several match statuses; the queue filters one value at a time, so a link there would
  // show a different number than the tile just showed.
  const tile = (n, label, cls, href) => {
    const inner = `<b>${n}</b><span>${label}</span>`;
    return href
      ? `<a class="stat stat-link ${cls}" href="${href}" title="Show these in the review queue">${inner}</a>`
      : `<div class="stat ${cls}">${inner}</div>`;
  };
  const attention = [];
  if (c.conflicts) attention.push(`${c.conflicts} stale approval${c.conflicts === 1 ? '' : 's'}: the source data changed after the decision, so re-review before synchronizing. <a href="#/queue">Open review queue</a>`);
  ['pl', 'sl'].forEach(s => {
    const x = sides[s], k = conn(s), S = s.toUpperCase();
    if (x.status === 'error') attention.push(`${S} retrieval failed${x.error ? `: ${CL.esc(x.error)}` : ''}. Refresh snapshots once the tool answers.`);
    else if (x.status === 'never') attention.push(`${S} snapshot has never been retrieved. Use Refresh snapshots in the sidebar.`);
    if (k.id && !k.last_test_ok) attention.push(`${S} connection “${CL.esc(k.name)}” ${k.last_test_at ? 'failed its last test' : 'has not been tested'}. <a href="#/settings">Test it in Settings</a>`);
  });
  const side = (s) => {
    const x = sides[s], k = conn(s);
    const cls = x.status === 'ok' ? 'ok' : (x.status === 'error' ? 'bad' : 'warn');
    return `<div><h3>${s.toUpperCase()} · ${CL.esc(k.name || '—')}</h3>
      <dl class="kv">
        <dt>tool</dt><dd>${CL.esc(x.tool || k.tool || '—')}</dd>
        <dt>connection</dt><dd>${k.id ? CL.connHealth(k) : '—'}</dd>
        <dt>snapshot</dt><dd>${CL.badge(CL.words(x.status), cls)}</dd>
        <dt>retrieved</dt><dd>${CL.when(x.retrieved_at)}</dd>
        <dt>components</dt><dd class="num">${x.count}</dd>
        ${s === 'pl' && x.out_of_scope != null ? `<dt>outside SL scope</dt><dd><span class="num">${x.out_of_scope}</span> of ${x.listed}
          <span class="small muted">listed, not read (no SL component can match them; link one by hand to read it)</span></dd>` : ''}
      </dl></div>`;
  };
  return `
    ${CL.viewHead(CL.esc(m.name), CL.mappingCtx(m),
      '<a class="btn primary" href="#/queue" title="Opens worst-first: most discrepancies, then least certain matches">Open review queue</a>')}
    ${CL.attention(attention)}
    <div class="stats mb-3">
      ${tile(c.pending_review, 'pending review', c.pending_review ? 'is-warn' : '', '')}
      ${tile(c.conflicts, 'stale approvals', c.conflicts ? 'is-bad' : '', '')}
      ${tile(c.suggested, 'match suggestions', '', '#/queue?status=suggested')}
      ${tile(c.unmatched, 'unmatched', '', '#/queue?status=unmatched')}
      ${tile(c.approved, 'approved for sync', c.approved ? 'is-ok' : '', '#/queue?review_status=approved_for_sync')}
      ${tile(c.matched, 'matched components', '', '')}
    </div>
    <div class="panel"><h2>Sources</h2>
      <div class="split">${side('pl')}${side('sl')}</div>
      <p class="small muted mt-3">${conns.length} connection${conns.length === 1 ? '' : 's'} configured · <a href="#/settings">Manage connections and mappings in Settings</a></p>
    </div>
    <div class="panel"><h2>Recent synchronization</h2>
      ${ov.recent_sync.length ? CL.jobsTable(ov.recent_sync, false) + '<p class="small mt-2"><a href="#/history">All jobs and the synchronization gate</a></p>'
        : '<p class="muted">No synchronization yet. Approved components appear here after a dry run.</p>'}
    </div>
    <div class="panel"><h2>Version delta</h2>
      <p class="small muted">Compare this mapping with the previous version of the same project. The comparison surfaces new or changed components and newly observed licenses/copyrights.</p>
      <div class="row-actions"><button id="delta-btn">Compare to previous version</button></div>
      <div id="delta-result" class="mt-2"></div>
    </div>
  `;
};

CL.renderOverview = async (fresh) => {
  const el = document.getElementById('view-overview');
  const m = CL.mapping();
  if (!m) {
    el.innerHTML = `${CL.viewHead('Project overview', '<span class="muted">No PL↔SL mapping yet.</span>',
        '<a class="btn primary" href="#/settings">Set up connections and a mapping</a>')}
      <div class="panel"><p class="muted">Add a PL and an SL connection, then create a mapping, in Settings &amp; health.</p></div>`;
    return;
  }
  // the overview and the connections are independent reads; one wave
  const [ov, catalog] = await Promise.all([CL.api('GET', `/api/mappings/${m.id}/overview`), CL.fetchCatalog()]);
  if (fresh && !fresh()) return;
  el.innerHTML = CL.overviewBody(m, ov, catalog.conns);
  CL.bindOverview();
  CL.resolveMappingTargets(m, fresh, el);
};

// Views render the ids first and upgrade them in place once the names arrive;
// when a tool cannot answer, the ids simply stay.
CL.resolveMappingTargets = async (m, fresh, root = document) => {
  const targets = await CL.fetchTargets(m);
  if (!targets || (fresh && !fresh())) return;
  // scoped to one view: hidden views stay in the DOM and may show another mapping
  root.querySelectorAll('.map-target').forEach(el => {
    const t = targets[el.dataset.side];
    if (!t || !t.label) return;
    el.innerHTML = /^https?:\/\//i.test(t.url || '')
      ? `<a href="${CL.esc(t.url)}" target="_blank" rel="noopener noreferrer">${CL.esc(t.label)}</a>`
      : CL.esc(t.label);
  });
};

// Connections and tools change far less often than the overview; both panels read them once.
CL.fetchCatalog = async () => {
  const [conns, tools] = await Promise.all([CL.api('GET', '/api/connections'), CL.api('GET', '/api/tools')]);
  CL.state.connections = conns.items;
  CL.state.tools = tools.items;
  return { conns: conns.items, tools: tools.items };
};

CL.bindOverview = () => {
  const deltaBtn = document.getElementById('delta-btn');
  if (deltaBtn) deltaBtn.onclick = () => CL.click(async () => {
    const d = await CL.api('GET', `/api/mappings/${CL.state.mappingId}/delta`);
    const box = document.getElementById('delta-result');
    if (!box) return;
    if (!d.previous) { box.innerHTML = '<p class="muted">No previous version mapping for the same project.</p>'; return; }
    const findings = d.new_findings.length ? `<table class="mt-2"><thead><tr><th>component</th><th>version</th><th>new licenses</th><th>new copyrights</th></tr></thead><tbody>${d.new_findings.map(f => `<tr><td>${CL.esc(f.component)}</td><td>${CL.esc(f.version || '')}</td><td class="small">${CL.esc((f.new_licenses || []).join(', ') || '—')}</td><td class="small">${CL.esc((f.new_copyrights || []).join('; ') || '—')}</td></tr>`).join('')}</tbody></table>` : '<p class="muted mt-2">No new findings against the previous version.</p>';
    box.innerHTML = `<div class="kv"><dt>previous</dt><dd>${CL.esc(d.previous.version)}</dd><dt>new components</dt><dd>${d.new_components.length}</dd><dt>changed components</dt><dd>${d.changed_components.length}</dd><dt>new findings</dt><dd>${d.new_findings.length}</dd></div>` + findings + `<div class="row-actions mt-2"><button id="delta-apply" class="primary-soft">Carry previous decisions</button></div>`;
    const apply = document.getElementById('delta-apply');
    if (apply) apply.onclick = () => CL.click(async () => {
      const r = await CL.api('POST', `/api/mappings/${CL.state.mappingId}/delta/apply`, { apply_decisions: true });
      CL.toast(`carried ${r.carried_decisions.length} decisions`);
      await CL.renderOverview();
    });
  });
};

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
        <option value="">any</option>${['not_reviewed', 'review_required', CL.reviewState.IN_PROGRESS, CL.reviewState.REVIEWED, CL.reviewState.APPROVED].map(s => `<option value="${s}" ${CL.state.queue.review_status === s ? 'selected' : ''}>${CL.words(s)}</option>`).join('')}
      </select></label>
      <label>match status<select id="q-status">
        <option value="">any</option>${['auto', 'suggested', 'confirmed', 'unmatched', 'rejected', 'manual'].map(s => `<option value="${s}" ${CL.state.queue.status === s ? 'selected' : ''}>${s}</option>`).join('')}
      </select></label>
      <label>confidence<select id="q-confidence"><option value="">any</option><option value="high" ${CL.state.queue.confidence === 'high' ? 'selected' : ''}>high (≥0.90)</option><option value="low" ${CL.state.queue.confidence === 'low' ? 'selected' : ''}>low (&lt;0.90)</option></select></label>
      <label>sort<select id="q-sort">${[['discrepancies', 'discrepancies (most first)'], ['confidence', 'confidence (least certain first)'], ['status', 'review status'], ['name', 'name A–Z']]
        .map(([v, l]) => `<option value="${v}" ${CL.state.queue.sort === v ? 'selected' : ''}>${l}</option>`).join('')}</select></label>
      <button id="q-apply" class="primary">Search</button>
      <button id="q-clear" ${CL.queueDirty() ? '' : 'disabled'}>Clear filters</button>
    </div>
    <details id="q-rules" class="panel"><summary>Rules: one decision applied wherever its condition holds</summary>
      <div id="q-rules-body" class="small muted">Opening counts what each rule would decide…</div></details>`;

// View-level actions sit in the header, apart from the filters they have nothing to do with.
CL.queueActions = () => `<button id="q-link" title="Pair any PL component with any SL component by hand">Link components…</button>
  <span class="act-divider" aria-hidden="true"></span>
  <button id="q-export" class="ghost" title="Download the full review report for this mapping as CSV">Export CSV</button>
  <button id="q-export-html" class="ghost" title="Download the full review report for this mapping as HTML">Export HTML</button>`;

// Selection is kept across pages; "all" means every row the current filters show, on any page.
CL.pickCount = () => (CL.state.pick.all ? CL.state.pick.total : CL.state.pick.ids.size);
CL.bulkBar = (data) => {
  const n = CL.pickCount(), p = CL.state.pick;
  const pageAll = data.items.length && data.items.every(r => p.ids.has(r.id));
  return `<div id="q-bulk" class="bulk-bar" role="region" aria-label="Selected components" ${n ? '' : 'hidden'}>
    <b class="num">${n}</b> selected
    ${!p.all && pageAll && data.total > data.items.length ? `<button class="tiny ghost" id="q-pick-all">Select all ${data.total} matching</button>` : ''}
    <button class="tiny ghost" id="q-pick-none">Clear selection</button>
    <span class="bulk-actions row-actions">
      <button class="tiny primary-soft" data-bulk="approve" title="Approve for synchronization. Components with nothing to change are approved as reviewed only, so the sync marks them reviewed in the PL tool">Approve for sync</button>
      <button class="tiny" data-bulk="confirm" title="Confirm the suggested or automatic match">Confirm matches</button>
      <button class="tiny ghost" id="q-export-pick">Export selection</button>
      <span class="act-divider" aria-hidden="true"></span>
      <button class="tiny danger-ghost" data-bulk="unlink">Unlink…</button>
    </span></div>`;
};

// The bulk bar follows the table and sticks to the viewport bottom: above it, a sticky bar
// would cover the header checkbox and the rows being picked.
CL.queueRows = (data) => `<div class="table-scroll"><table class="stack-rows"><thead><tr>
      <th class="pick"><input type="checkbox" id="q-pick-page" aria-label="Select every component on this page"
        ${data.items.length && data.items.every(r => CL.state.pick.all || CL.state.pick.ids.has(r.id)) ? 'checked' : ''}></th>
      <th>PL component</th><th>SL component</th><th>match</th><th>confidence</th><th>licenses</th><th>discrepancies</th><th>review</th><th>match actions</th></tr></thead><tbody>
      ${data.items.map(r => `<tr class="clickable${CL.state.pick.all || CL.state.pick.ids.has(r.id) ? ' selected' : ''}" data-id="${r.id}" tabindex="0">
        <td class="pick" data-label="select"><input type="checkbox" class="q-pick" data-pick="${r.id}"
          aria-label="Select ${CL.esc(r.pl_name || r.sl_name || `component ${r.id}`)}" ${CL.state.pick.all || CL.state.pick.ids.has(r.id) ? 'checked' : ''}></td>
        <td data-label="PL component"><a href="#/component/${r.id}"><b>${CL.esc(r.pl_name || '—')}</b></a> <span class="muted">${CL.esc(r.pl_version || '')}</span></td>
        <td data-label="SL component">${CL.esc(r.sl_name || '—')} <span class="muted">${CL.esc(r.sl_version || '')}</span></td>
        ${CL.queueMatchCell(r)}
        ${CL.queueConfidenceCell(r)}
        <td data-label="licenses" class="small">${CL.esc((r.licenses || []).join(', ') || '—')}</td>
        <td data-label="discrepancies" class="num">${r.discrepancy_count ? `<span class="badge bad">${r.discrepancy_count}</span>` : '<span class="muted">0</span>'}</td>
        <td data-label="review">${CL.status(r.review_status)}</td>
        <td data-label="match actions" class="row-actions"><button class="tiny" data-edit-match="${r.id}">Edit match</button>${r.pl_key && r.sl_key && r.status !== 'rejected' ? `<button class="tiny danger-ghost" data-unlink-match="${r.id}">Unlink</button>` : ''}</td></tr>`).join('') || '<tr><td colspan="9" class="muted">No components match these filters. Clear a filter or widen the search.</td></tr>'}
    </tbody></table></div>${CL.bulkBar(data)}
    <div class="pager"><button id="q-prev" ${data.offset === 0 ? 'disabled' : ''}>‹ prev</button>
      <span>${data.total ? data.offset + 1 : 0}–${Math.min(data.offset + data.limit, data.total)} of ${data.total}</span>
      <button id="q-next" ${data.offset + data.limit >= data.total ? 'disabled' : ''}>next ›</button></div>`;

// The method is worth reading when it differs from the status; an unmatched row whose method
// is also "unmatched" only says the same word twice across two columns.
CL.queueMatchCell = (r) => {
  const method = r.method && r.method !== r.status ? ` <span class="small muted">${CL.esc(r.method)}</span>` : '';
  return `<td data-label="match">${CL.status(r.status)}${method}</td>`;
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
  if (!CL.state.mappingId) {
    el.innerHTML = CL.viewHead('Review queue', '<span class="muted">No mapping yet.</span>', '<a class="btn primary" href="#/settings">Create a mapping in Settings</a>');
    CL.state.queueFor = null; return;
  }
  const fromHash = CL.queueFromHash;
  CL.queueFromHash = false;
  if (CL.state.queueFor !== CL.state.mappingId || !document.getElementById('queue-list')) {
    el.innerHTML = `${CL.viewHead('Review queue <span class="muted small" id="queue-count"></span>', CL.mappingCtx(CL.mapping()), CL.queueActions())}
      ${CL.queueFilters()}<div class="panel" id="queue-list"></div>`;
    CL.bindQueue();
    CL.resolveMappingTargets(CL.mapping(), fresh, el);
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
  const rules = document.getElementById('q-rules');
  if (rules) rules.ontoggle = () => { if (rules.open) CL.click(CL.renderRules); };
  document.getElementById('q-link').onclick = () => CL.click(async () => {
    const pick = await CL.matchEditor({
      mappingId: CL.state.mappingId, title: 'Link components',
      message: 'Pick any PL component and any SL component to pair them, whatever name or version they have.',
      confirmText: 'Link',
    });
    if (!pick) return;
    await CL.api('POST', `/api/mappings/${CL.state.mappingId}/matches`, pick);
    CL.toast('components linked');
    await CL.renderQueue();
  });
  document.getElementById('q-export').onclick = () =>
    CL.click(() => CL.download(`/api/mappings/${CL.state.mappingId}/export.csv`, 'review-report.csv'));
  document.getElementById('q-export-html').onclick = () =>
    CL.click(() => CL.download(`/api/mappings/${CL.state.mappingId}/export.html`, 'review-report.html'));
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
    CL.clearPick();
    paint();
    CL.renderQueue();
  };
  document.getElementById('q-apply').onclick = () => { clearTimeout(debounce); apply(); };
  document.getElementById('q-clear').onclick = () => {
    clearTimeout(debounce);
    Object.assign(CL.state.queue, { q: '', status: '', review_status: '', confidence: '', offset: 0 });
    CL.clearPick();
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
      CL.clearPick();
      paint();
      CL.renderQueue();
    }, DEBOUNCE_MS);
  };
  search.onkeydown = (e) => { if (e.key === 'Enter') { clearTimeout(debounce); apply(); } };
  ['q-review', 'q-status', 'q-confidence', 'q-sort'].forEach(i => { document.getElementById(i).onchange = apply; });
};

CL.clearPick = () => { CL.state.pick = { ids: new Set(), all: false, total: 0 }; };

// The filters the queue shows right now, as the bulk endpoint reads them.
CL.queueSelection = () => (CL.state.pick.all
  ? { all: true, filters: { q: CL.state.queue.q, status: CL.state.queue.status, review_status: CL.state.queue.review_status, confidence: CL.state.queue.confidence } }
  : { ids: [...CL.state.pick.ids] });

CL.bulkReport = async (label, r) => {
  const parts = [`${label}: ${r.done.length} of ${r.requested}`];
  if (r.review_only) parts.push(`${r.review_only} with nothing to change, approved as reviewed only`);
  if (!r.failed.length) { CL.toast(parts.join(' · ')); return; }
  await CL.dialog({
    title: `${r.failed.length} component${r.failed.length === 1 ? '' : 's'} not ${label.toLowerCase()}`,
    message: parts.join(' · ') + '. These were left as they were:',
    html: `<ul class="plan-list small">${r.failed.slice(0, 25).map(f => `<li><b>${CL.esc(f.name)}</b>: ${CL.esc(f.error)}</li>`).join('')}
      ${r.failed.length > 25 ? `<li>…and ${r.failed.length - 25} more</li>` : ''}</ul>`,
    confirmText: 'OK',
  });
};

CL.bindBulk = (data) => {
  const p = CL.state.pick;
  p.total = data.total;
  const repaint = () => CL.renderQueue();
  // the pick cell selects, it never opens the component the row links to
  document.querySelectorAll('#queue-list td.pick').forEach(td => {
    td.onclick = (e) => { e.stopPropagation(); if (e.target === td) td.querySelector('input').click(); };
  });
  document.querySelectorAll('#queue-list .q-pick').forEach(box => {
    box.onclick = (e) => e.stopPropagation();
    box.onchange = () => {
      const id = parseInt(box.dataset.pick, 10);
      if (p.all) { p.all = false; data.items.forEach(r => p.ids.add(r.id)); }
      if (box.checked) p.ids.add(id); else p.ids.delete(id);
      repaint();
    };
  });
  const page = document.getElementById('q-pick-page');
  if (page) page.onchange = () => {
    if (p.all && !page.checked) { CL.clearPick(); repaint(); return; }
    data.items.forEach(r => { if (page.checked) p.ids.add(r.id); else p.ids.delete(r.id); });
    repaint();
  };
  const all = document.getElementById('q-pick-all');
  if (all) all.onclick = () => { p.all = true; repaint(); };
  const none = document.getElementById('q-pick-none');
  if (none) none.onclick = () => { CL.clearPick(); repaint(); };
  const exp = document.getElementById('q-export-pick');
  if (exp) exp.onclick = () => CL.click(async () => {
    let ids = [...p.ids];
    if (p.all) {
      ids = [];
      for (let offset = 0; offset === 0 || offset < p.total; offset += 200) {
        const qs = new URLSearchParams({ ...CL.queueSelection().filters, limit: 200, offset });
        ids.push(...(await CL.api('GET', `/api/mappings/${CL.state.mappingId}/components?${qs}`)).items.map(r => r.id));
      }
    }
    await CL.download(`/api/mappings/${CL.state.mappingId}/export.csv?ids=${ids.join(',')}`, 'review-selection.csv');
  });
  document.querySelectorAll('#queue-list [data-bulk]').forEach(b => b.onclick = () => CL.click(async () => {
    const action = b.dataset.bulk, n = CL.pickCount();
    if (action === 'unlink' && !await CL.confirm(`Unlink ${n} pair${n === 1 ? '' : 's'}? Each side returns to the unmatched list and the pair is not proposed again after a refresh.`,
      { title: 'Unlink selected pairs', confirmText: `Unlink ${n}` })) return;
    const r = await CL.api('POST', `/api/mappings/${CL.state.mappingId}/bulk`, { action, ...CL.queueSelection() });
    CL.clearPick();
    await CL.renderQueue();
    await CL.bulkReport({ approve: 'Approved', confirm: 'Confirmed', unlink: 'Unlinked' }[action], r);
  }));
};

CL.renderRules = async () => {
  const box = document.getElementById('q-rules-body');
  if (!box) return;
  const { items } = await CL.api('GET', `/api/mappings/${CL.state.mappingId}/rules`);
  box.className = '';
  box.innerHTML = items.map(r => `<div class="rule">
      <div><b>${CL.esc(r.label)}</b><div class="small muted">${CL.esc(r.detail)}</div>
        <div class="small">${r.rows ? `${r.rows} undecided row${r.rows === 1 ? '' : 's'} in ${r.components} component${r.components === 1 ? '' : 's'}: ${CL.esc(r.sample.join(', '))}${r.components > r.sample.length ? '…' : ''}`
          : '<span class="muted">Nothing to decide right now.</span>'}</div></div>
      <button class="tiny primary-soft" data-rule="${CL.esc(r.name)}" data-rows="${r.rows}" ${r.rows ? '' : 'disabled'}>Apply to ${r.rows}…</button>
    </div>`).join('') || '<p class="small muted">No rules are defined.</p>';
  box.querySelectorAll('[data-rule]').forEach(b => b.onclick = () => CL.click(async () => {
    const rule = items.find(x => x.name === b.dataset.rule);
    if (!await CL.confirm(`${rule.label}: record ${rule.rows} decision${rule.rows === 1 ? '' : 's'} in ${rule.components} component${rule.components === 1 ? '' : 's'} under your name?`,
      { title: 'Apply rule', confirmText: `Apply to ${rule.rows}`, danger: false })) return;
    const r = await CL.api('POST', `/api/mappings/${CL.state.mappingId}/rules/${rule.name}/apply`);
    CL.toast(`rule applied: ${r.decided} decision${r.decided === 1 ? '' : 's'} recorded${r.failed.length ? `, ${r.failed.length} refused` : ''}`, r.failed.length > 0);
    await CL.renderQueue();
    await CL.renderRules();
  }));
};

CL.bindQueueRows = (data) => {
  const byId = new Map(data.items.map(r => [String(r.id), r]));
  document.querySelectorAll('[data-edit-match]').forEach(b => {
    b.onclick = (e) => e.stopPropagation();
    b.addEventListener('click', () => CL.click(async () => {
      const r = byId.get(b.dataset.editMatch);
      const pick = await CL.matchEditor({
        mappingId: CL.state.mappingId, title: 'Edit match',
        message: 'Choose which PL and SL component belong together. This replaces the automatic proposal.',
        plKey: r.pl_key, slKey: r.sl_key,
      });
      if (!pick) return;
      await CL.api('POST', `/api/components/${r.id}/reassign`, pick);
      CL.toast('match saved');
      await CL.renderQueue();
    }));
  });
  document.querySelectorAll('[data-unlink-match]').forEach(b => {
    b.onclick = (e) => e.stopPropagation();
    b.addEventListener('click', () => CL.click(async () => {
      if (!await CL.confirm('Unlink this pair? Both components return to the unmatched list and the pair is not proposed again after refresh.', { title: 'Unlink match', confirmText: 'Unlink' })) return;
      await CL.api('POST', `/api/components/${b.dataset.unlinkMatch}/reject`, { reason: 'reviewer unlinked' });
      CL.toast('pair unlinked');
      await CL.renderQueue();
    }));
  });
  CL.bindBulk(data);
  document.getElementById('q-prev').onclick = () => { CL.state.queue.offset = Math.max(0, CL.state.queue.offset - CL.state.queue.limit); CL.renderQueue(); };
  document.getElementById('q-next').onclick = () => { CL.state.queue.offset += CL.state.queue.limit; CL.renderQueue(); };
  document.querySelectorAll('#queue-list tr.clickable').forEach(tr => {
    const open = () => { location.hash = `#/component/${tr.dataset.id}`; };
    tr.onclick = open;
    tr.onkeydown = (e) => { if (e.key === 'Enter' && e.target === tr) { e.preventDefault(); open(); } };
  });
};

// ---------- history ----------
// Template first, bindings after: the panels are one string each, built from the responses.
CL.lastDryRun = (jobs) => (jobs ? jobs.items : []).find(j => j.dry_run);

// Work zone: what will be written, then the gate as numbered steps. Execute stays the danger
// variant on purpose: it is an irreversible write to the PL tool (asserted in the smoke test).
CL.historyPreview = (preview, jobs) => {
  if (!preview.components.length) return `<div class="panel"><h2>Pending synchronization</h2>
      <p class="muted">Nothing approved yet. Approve components in the review workspace.</p></div>`;
  const rows = preview.components.flatMap(c => c.items.map((i, k) => `<tr>
      <td data-label="component">${k ? '' : `<a href="#/component/${c.match.id}">${CL.esc(c.match.pl_name)}</a> <span class="muted">${CL.esc(c.match.pl_version || '')}</span>`}</td>
      <td data-label="field">${CL.esc(CL.words(i.field))}</td><td data-label="status">${CL.status(i.status)}</td>
      <td data-label="note" class="small">${CL.esc(i.message || '—')}</td></tr>`)).join('');
  const dry = CL.lastDryRun(jobs);
  return `<div class="panel"><h2>Pending synchronization</h2>
      <p class="small muted">Only components explicitly approved for synchronization appear here.</p>
      <table class="stack-rows"><thead><tr><th>component</th><th>field</th><th>status</th><th>note</th></tr></thead><tbody>${rows}</tbody></table>
      <ol class="gate" aria-label="Synchronization steps">
        <li id="step-dry" class="${dry ? 'is-done' : ''}"><div class="row-actions"><button id="sync-dry">Dry run</button>
            <span class="small" id="sync-dry-result">${dry ? `last dry run <a href="#/history/job/${dry.id}">#${dry.id}</a>${CL.jobResult(dry.summary) === '—' ? '' : ` · ${CL.jobResult(dry.summary)}`} · ${CL.when(dry.finished_at || dry.created_at, '—')}` : ''}</span></div>
          <p class="small muted step-hint">Shows what every field would do. A dry run writes nothing.</p></li>
        <li id="step-confirm"><div class="checkline"><label><input type="checkbox" id="sync-confirm"> I approve these writes</label></div>
          <p class="small muted step-hint">Your name is recorded against every value written.</p></li>
        <li><div class="row-actions"><button id="sync-live" class="danger" disabled aria-describedby="sync-hint" title="Tick &quot;I approve these writes&quot; first">Execute synchronization</button></div>
          <p class="small muted step-hint" id="sync-hint">Execute stays disabled until you tick the approval box. A dry run writes nothing.</p></li>
      </ol>
    </div>`;
};

CL.historyJobs = (jobs) => `
    <div class="panel"><h2>Jobs</h2>
      ${jobs.items.length ? CL.jobsTable(jobs.items, true) : '<p class="muted">No jobs yet.</p>'}
      <div id="job-detail"></div>
    </div>`;

CL.renderHistory = async (fresh) => {
  const el = document.getElementById('view-history');
  if (!CL.state.mappingId) {
    el.innerHTML = CL.viewHead('Synchronization history', '<span class="muted">No mapping yet.</span>', '<a class="btn primary" href="#/settings">Create a mapping in Settings</a>');
    return;
  }
  const [jobs, preview] = await Promise.all([
    CL.api('GET', `/api/sync/jobs?mapping_id=${CL.state.mappingId}`),
    CL.api('POST', '/api/sync/preview', { mapping_id: CL.state.mappingId, finalize: true }),
  ]);
  if (fresh && !fresh()) return;
  const m = CL.mapping();
  const n = preview.components.length;
  const ctx = `<span>${n} component${n === 1 ? '' : 's'} approved for synchronization</span><span class="muted">→</span>
    <span class="muted">PL</span> <span>${CL.esc(m ? `${m.pl_connection.name} · ${m.pl_project} ${m.pl_version || ''}` : '—')}</span>`;
  const attention = CL.state.readOnly ? ['Read-only mode: this instance refuses every write to Black Duck / FOSSology. Dry runs still work.'] : [];
  el.innerHTML = `${CL.viewHead('Synchronization history', ctx, '')}${CL.attention(attention)}${CL.historyPreview(preview, jobs)}${CL.historyJobs(jobs)}`;
  CL.bindHistory(jobs);
  if (CL.state.jobId) await CL.showJob(CL.state.jobId);
};

CL.bindHistory = (jobs) => {
  const confirmBox = document.getElementById('sync-confirm');
  if (confirmBox) {
    const live = document.getElementById('sync-live');
    const hint = document.getElementById('sync-hint');
    // Execute unlocks only once a dry run for this mapping has actually run; the jobs list
    // already on screen is the evidence, so no extra state is needed.
    const dryDone = () => (jobs ? jobs.items : []).some(j => j.dry_run);
    const paint = () => {
      const ready = confirmBox.checked && dryDone();
      document.getElementById('step-confirm').classList.toggle('is-done', confirmBox.checked);
      live.disabled = !ready;
      live.title = ready
        ? 'Writes the approved values to the PL project list'
        : (confirmBox.checked ? 'Run a dry run first' : 'Tick "I approve these writes" first');
      hint.textContent = ready
        ? 'Live writes go to the PL project list now, item by item; a failed item is reported per field.'
        : (confirmBox.checked
          ? 'Execute unlocks once a dry run for this mapping has run. A dry run writes nothing.'
          : 'Execute stays disabled until you tick the approval box. A dry run writes nothing.');
    };
    confirmBox.onchange = paint;
    paint();
    // a pressed sync button stays pressed until the job answers, so one click is one job
    const runSync = (fn) => CL.click(async () => {
      const btns = [document.getElementById('sync-dry'), live];
      btns.forEach(b => { b.disabled = true; });
      try { await fn(); } finally { btns.forEach(b => { if (b.isConnected) b.disabled = false; }); paint(); }
    });
    document.getElementById('sync-dry').onclick = () => runSync(async () => {
      const job = await CL.api('POST', '/api/sync/jobs', { mapping_id: CL.state.mappingId, dry_run: true, finalize: true });
      CL.toast(`dry-run job #${job.id}: ${CL.dash(JSON.stringify(job.summary))}`);
      await CL.renderHistory();
      await CL.showJob(job.id);
    });
    live.onclick = () => runSync(async () => {
      const job = await CL.api('POST', '/api/sync/jobs', { mapping_id: CL.state.mappingId, dry_run: false, confirm: true, finalize: true });
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
  box.innerHTML = `<h2>Job #${job.id} · ${job.dry_run ? 'dry run' : 'live'} · ${CL.status(job.status)}</h2>
    <p class="small muted">${CL.when(job.created_at)} by ${CL.esc(job.created_by)} · ${CL.jobResult(job.summary)}</p>
    ${job.components.map(c => `<h3 class="mt-3">${CL.esc(c.pl_name)} <span class="muted">${CL.esc(c.pl_version || '')}</span></h3>
      <table><thead><tr><th>field</th><th>status</th><th>before</th><th>approved</th><th>api</th><th>message</th></tr></thead><tbody>
      ${c.items.map(i => `<tr><td>${CL.esc(CL.words(i.field))}</td><td>${CL.status(i.status)}</td><td class="small"><code>${CL.esc(JSON.stringify(i.pl_before))}</code></td><td class="small">${CL.esc(String(i.approved_value || '').slice(0, 200))}</td><td>${CL.esc(i.api_status || '')}</td><td class="small">${CL.esc(i.message)}</td></tr>`).join('')}
      </tbody></table>`).join('')}
    ${job.items.some(i => i.status === 'failed' || i.status === 'interrupted') ? `<div class="row-actions mt-3"><button id="retry-job" class="primary-soft">Retry ${job.items.some(i => i.status === 'failed') ? 'failed' : 'unwritten'} items…</button></div>` : ''}
    ${!job.dry_run && job.items.some(i => i.status === 'succeeded' || i.status === 'skipped_already_applied') ? `<div class="row-actions mt-3">
      <button id="undo-preview">Preview undo</button>
      <span class="act-divider" aria-hidden="true"></span>
      <button id="undo-job" class="danger">Undo…</button>
    </div><div id="undo-detail" class="mt-2"></div>` : ''}`;
  const retry = document.getElementById('retry-job');
  if (retry) retry.onclick = () => CL.click(async () => {
    if (!await CL.confirm('Retry the items that did not land, against the PL tool?',
                         { title: 'Retry unwritten items', confirmText: 'Retry' })) return;
    const j = await CL.api('POST', `/api/sync/jobs/${id}/retry`, { confirm: true });
    CL.toast(`retry job #${j.id}: ${CL.dash(JSON.stringify(j.summary))}`, j.status === 'failed');
    await CL.renderHistory();
    await CL.showJob(j.id);
  });
  const undoPreview = document.getElementById('undo-preview');
  if (undoPreview) undoPreview.onclick = () => CL.click(async () => {
    const p = await CL.api('GET', `/api/sync/jobs/${id}/undo/preview`);
    const box = document.getElementById('undo-detail');
    if (!box) return;
    box.innerHTML = `<p class="small muted">Undo preview for job #${id}</p>
      <table><thead><tr><th>field</th><th>possible</th><th>reason</th><th>before</th></tr></thead><tbody>
        ${p.items.map(i => `<tr><td>${CL.esc(i.field)}</td><td>${i.possible ? CL.badge('yes', 'ok') : CL.badge('manual', 'warn')}</td><td class="small">${CL.esc(i.reason || '')}</td><td class="small"><code>${CL.esc(JSON.stringify(i.before))}</code></td></tr>`).join('')}
      </tbody></table>`;
  });
  const undoJob = document.getElementById('undo-job');
  if (undoJob) undoJob.onclick = () => CL.click(async () => {
    if (!await CL.confirm('Restore the before-values for every succeeded item in this job?',
                          { title: 'Undo synchronization', confirmText: 'Undo' })) return;
    const r = await CL.api('POST', `/api/sync/jobs/${id}/undo`, { confirm: true });
    CL.toast(`undo: ${r.undone.length} restored; ${r.manual.length} need manual action`, r.manual.length > 0);
    await CL.renderHistory();
    await CL.showJob(id);
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
  if (!d.report) blocked = 'Upload stays disabled until step 1 has previewed this exact target.';
  else if (d.stale) blocked = 'This target changed after the preview. Preview again before uploading.';
  else if (!approved) blocked = 'Upload stays disabled until you tick the approval box. A preview writes nothing.';
  run.disabled = !!blocked;
  document.getElementById('dp-step-preview').classList.toggle('is-done', !!d.report && !d.stale);
  document.getElementById('dp-step-confirm').classList.toggle('is-done', approved);
  run.title = blocked || 'Creates the folder and one upload per library in the scanner';
  document.getElementById('dp-hint').textContent = blocked
    || 'Uploads go to the scanner now. Nothing reaches the project list until you synchronize it.';
};

// Template first, bindings after - same shape as the overview and the queue.
CL.deployTarget = (pls, sls) => {
  const options = (list) => list.map(c => `<option value="${c.id}">${CL.esc(c.name)}</option>`).join('');
  return `${CL.viewHead('Source deployment', '<span class="muted">Resolve every library of a PL project version to its source archive and upload it into one scanner folder, which then opens here as a scan.</span>', '')}
    <div class="panel"><h2>Deployment target</h2>
      <div class="split">
        <fieldset><legend>From: project-level tool</legend>
          <div class="field-grid one">
            <label>project-level tool<select id="dp-pl">${options(pls)}</select></label>
            <label>project<select id="dp-project"></select></label>
            <label>version<select id="dp-version"></select></label>
          </div></fieldset>
        <fieldset><legend>To: source-level scanner</legend>
          <div class="field-grid one">
            <label>source-level tool<select id="dp-sl">${options(sls)}</select></label>
            <label>parent folder<select id="dp-parent"></select></label>
            <label>folder name<input id="dp-folder" placeholder="{project}__{version}"></label>
          </div>
          <span class="field-label mt-2" id="dp-agents-label">scanner agents per upload</span>
          <span class="checkline" role="group" aria-labelledby="dp-agents-label">
            ${DEPLOY_AGENTS.map(([v, l], i) => `<label><input type="checkbox" id="dp-agent-${v}" ${i < 2 ? 'checked' : ''}>${l}</label>`).join('')}
          </span></fieldset>
      </div>
      <ol class="gate" aria-label="Deployment steps">
        <li id="dp-step-preview"><div class="row-actions"><button id="dp-preview">Preview sources</button></div>
          <p class="small muted step-hint">Resolves every source URL and writes nothing. The report opens below.</p></li>
        <li id="dp-step-confirm"><div class="checkline"><label><input type="checkbox" id="dp-confirm"> I approve these uploads</label></div>
          <p class="small muted step-hint">Check the preview report first.</p></li>
        <li><div class="row-actions"><button id="dp-run" class="primary" disabled aria-describedby="dp-hint">Upload to scanner</button></div>
          <p class="small muted step-hint" id="dp-hint"></p></li>
      </ol>
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
    el.innerHTML = `${CL.viewHead('Source deployment', '<span class="muted">Needs a Black Duck connection and a FOSSology connection.</span>', '<a class="btn primary" href="#/settings">Add connections in Settings</a>')}
      <div class="panel"><p class="muted">Deployment needs a project-list connection that reports its libraries and a scanner connection that accepts uploads — Black Duck and FOSSology today. Add one of each first.</p></div>`;
    return;
  }
  el.innerHTML = CL.deployTarget(pls, sls);
  CL.bindDeploy();
  await Promise.all([CL.loadDeployProjects(), CL.loadDeployFolders()]);  // two tools, no reason to queue
  if (CL.state.deploy.report) CL.renderDeployReport();
  CL.updateDeployGate();
};

CL.loadDeployProjects = async () => {
  const id = document.getElementById('dp-pl').value;
  if (!id) return;
  CL.selectLoading(document.getElementById('dp-project'));
  CL.selectLoading(document.getElementById('dp-version'));
  try {
    const data = await CL.api('GET', `/api/connections/${id}/projects?versions=0`);
    const proj = document.getElementById('dp-project');
    proj.innerHTML = data.items.map(p => `<option value="${CL.esc(p.id)}">${CL.esc(p.name)}</option>`).join('')
      || '<option value="">no projects</option>';
    const fillVersions = async () => {
      const ver = document.getElementById('dp-version');
      let versions = [];
      CL.selectLoading(ver);
      if (proj.value) {
        try {
          versions = (await CL.api('GET', `/api/connections/${id}/projects/${encodeURIComponent(proj.value)}/versions`)).items;
        } catch (e) { CL.loadFailure('version', e); }
      }
      ver.innerHTML = versions.map(v => `<option value="${CL.esc(v)}">${CL.esc(v)}</option>`).join('')
        || '<option value="">no versions</option>';
      document.getElementById('dp-folder').placeholder = `${proj.value}__${ver.value}`;
      CL.deployMarkStale();
    };
    proj.onchange = fillVersions;
    await fillVersions();
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
      <button id="dp-mapping" class="primary-soft">Create mapping from this folder</button>
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
CL.setRefreshBusy = (busy, text) => {
  const btn = document.getElementById('refresh-btn');
  if (!btn) return;
  btn.classList.toggle('is-busy', busy);
  btn.disabled = busy;
  const label = btn.querySelector('.btn-label');
  if (label) label.textContent = busy ? (text || 'Refreshing…') : 'Refresh snapshots';
  document.getElementById('full-refresh-btn').disabled = busy;
  if (!busy) {
    document.getElementById('cancel-refresh-btn').classList.add('hidden');
    CL.paintRefreshProgress(null);
  }
};

// What the refresh will do, per side, and what the estimate is based on.
CL.planHtml = (plan) => {
  const side = (key) => {
    const x = plan.sides[key];
    const unit = CL.esc(x.unit || 'components');
    const what = x.error ? `could not be counted: ${CL.esc(x.error)}`
      : x.items == null ? `reads ${CL.esc(x.work)}`
      : `${x.items} ${unit}${x.cached ? `: ${x.cached} unchanged and reused, ${x.to_read} to read` : ' to read'}. Reads ${CL.esc(x.work)}.`;
    const last = x.last
      ? `Last read: ${CL.duration(x.last.seconds)} for ${x.last.read} ${unit}, ${CL.when(x.last.at)}.`
      : 'No previous refresh of this side to estimate from.';
    return `<li><b>${key.toUpperCase()}</b> · ${CL.esc(x.tool)} · ${CL.esc(x.target)}
      <div class="small">${what}</div>
      <div class="small muted">${last} Estimate: ${CL.duration(x.estimate_seconds)}.</div></li>`;
  };
  return `<ul class="plan-list">${side('pl')}${side('sl')}</ul>
    <p class="small">Then both snapshots are saved and re-matched in one transaction; review decisions are kept.
      You can cancel while it reads, and a cancelled refresh saves nothing.</p>
    <p class="plan-total">Estimated time: <b>${plan.estimate_seconds == null ? 'unknown' : `about ${CL.duration(plan.estimate_seconds)}`}</b></p>`;
};

// Live progress under the button: which side, how far, how long, how much is left.
CL.paintRefreshProgress = (job) => {
  const el = document.getElementById('refresh-progress');
  if (!el) return;
  if (!job) { el.hidden = true; el.innerHTML = ''; return; }
  const p = job.progress || {};
  const cancelBtn = document.getElementById('cancel-refresh-btn');
  let line, pct = null;
  if (job.status === 'cancelling') line = 'Cancelling: stopping at the next request…';
  else if (p.side === 'commit') line = 'Saving both snapshots and re-matching…';
  else if (!p.side) line = 'Starting…';
  else if (p.total) {
    pct = Math.round((p.done / p.total) * 100);
    const left = p.done ? (p.side_elapsed_seconds / p.done) * (p.total - p.done) : null;
    line = `${p.side.toUpperCase()} ${p.done}/${p.total}${left != null ? ` · about ${CL.duration(left)} left` : ''}`;
  } else line = `${p.side.toUpperCase()} · listing…`;
  const est = job.estimate_seconds != null ? ` of about ${CL.duration(job.estimate_seconds)}` : '';
  el.hidden = false;
  el.innerHTML = `<div class="side-line" title="${CL.esc(line)}">${CL.esc(line)}</div>
    ${pct != null ? `<span class="ws-meter" role="progressbar" aria-label="refresh progress" aria-valuenow="${pct}" aria-valuemin="0" aria-valuemax="100"><i style="width:${pct}%"></i></span>` : ''}
    <div class="muted">${CL.duration(job.elapsed_seconds)} elapsed${est}</div>`;
  const saving = p.side === 'commit';
  cancelBtn.classList.remove('hidden');
  cancelBtn.disabled = saving || job.status === 'cancelling';
  cancelBtn.textContent = job.status === 'cancelling' ? 'Cancelling…' : 'Cancel refresh';
  cancelBtn.title = saving ? 'Saving one transaction: the refresh can no longer be cancelled' : 'Stop reading; nothing from this refresh is saved';
};

// Runs the refresh as a background job and polls it, so a large folder neither times out the
// request nor leaves the page blind.
CL.refreshJob = async (full, estimate) => {
  let job = await CL.api('POST', `/api/mappings/${CL.state.mappingId}/refresh-jobs`, { full: !!full, estimate_seconds: estimate });
  const cancelBtn = document.getElementById('cancel-refresh-btn');
  cancelBtn.onclick = async () => {
    cancelBtn.disabled = true;
    cancelBtn.textContent = 'Cancelling…';
    try { job = await CL.api('POST', `/api/refresh-jobs/${job.id}/cancel`); CL.paintRefreshProgress(job); }
    catch (e) { if (e !== CL.NEEDS_REVIEWER) CL.toast(e.message, true); CL.paintRefreshProgress(job); }
  };
  while (job.status === 'running' || job.status === 'cancelling') {
    CL.paintRefreshProgress(job);
    await new Promise(r => setTimeout(r, 1000));
    job = await CL.api('GET', `/api/refresh-jobs/${job.id}`);
  }
  if (job.status === 'error') throw new Error(job.error);
  return job.status === 'cancelled' ? null : job;
};

// Plan first: a refresh that is long, or that cannot be estimated, says what it will do and
// how long it should take, and starts only when the reviewer says so.
CL.refresh = (full) => CL.run(async () => {
  if (!CL.state.mappingId) throw new Error('no mapping selected');
  full = full === true;
  CL.setRefreshBusy(true, 'Estimating…');
  try {
    const plan = await CL.api('GET', `/api/mappings/${CL.state.mappingId}/refresh-plan${full ? '?full=1' : ''}`);
    if (plan.needs_approval) {
      const ok = await CL.dialog({
        title: full ? 'Start a full refresh?' : 'Start the refresh?',
        message: plan.estimate_seconds == null
          ? 'This refresh cannot be estimated yet, so here is what it will do.'
          : `This refresh should take about ${CL.duration(plan.estimate_seconds)}.`,
        html: CL.planHtml(plan), confirmText: 'Start refresh',
      });
      if (!ok) { CL.toast('refresh not started'); return; }
    }
    CL.setRefreshBusy(true, 'Refreshing…');
    const job = await CL.refreshJob(full, plan.estimate_seconds);
    if (!job) { CL.toast('refresh cancelled; nothing was saved and the previous snapshots are unchanged'); return; }
    const r = job.result;
    const bad = Object.entries(r.sides).filter(([, v]) => v.status === 'error' || v.status === 'skipped');
    const took = job.started_at && job.finished_at ? ` in ${CL.duration((Date.parse(job.finished_at) - Date.parse(job.started_at)) / 1000)}` : '';
    const scope = r.timing && r.timing.pl && r.timing.pl.out_of_scope ? ` (${r.timing.pl.out_of_scope} PL components outside the SL scope were not read)` : '';
    CL.toast(bad.length ? 'refresh incomplete: ' + bad.map(([k, v]) => `${k.toUpperCase()}: ${v.error || v.reason}`).join('; ')
      : `refreshed${took}: SL ${r.sides.sl.count}, PL ${r.sides.pl.count}${scope}`, bad.length > 0);
    await CL.route();
  } finally {
    CL.setRefreshBusy(false);
  }
});

document.getElementById('refresh-btn').onclick = () => CL.refresh(false);
document.getElementById('full-refresh-btn').onclick = () => CL.refresh(true);
document.getElementById('mapping-select').onchange = (e) => {
  CL.state.mappingId = parseInt(e.target.value, 10) || null;
  localStorage.setItem('cl.mapping', String(CL.state.mappingId));
  CL.state.queue.offset = 0;
  e.target.title = (CL.mapping() || {}).name || '';
  CL.sideStatus();
  CL.route();
};
fetch('/api/health').then(r => r.json())
  .then(h => { CL.state.readOnly = !!h.read_only; document.getElementById('ro-banner').hidden = !h.read_only; })
  .catch(() => {}); // the banner is decoration; a dead health call must not break the app
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
    // rows inside a collapsed <details> have no box; the cursor only visits what is on screen
    return [...document.querySelectorAll('#view-workspace tbody tr')].filter(tr => tr.querySelector('[data-dec]') && tr.getClientRects().length);
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

// Act on the cursor row only: a decision key must never fire on a row the reviewer has not
// pointed at, so a/p/e/f do nothing until j/k has placed the cursor.
CL.rowAction = (action) => {
  const row = CL.kbdCursor();
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
    ['Enter', 'open the queue row; apply the component in the workspace'],
    ['o', 'open the row under the cursor'],
    ['a', 'Approve SL on the current row'],
    ['p', 'Keep PL on the current row'],
    ['e', 'Correct the current row'],
    ['f', 'Record a false positive (scope prompt)'],
    ['n', 'focus the review note box'],
    ['?', 'show this list'],
    ['Esc', 'close a dialog'],
  ];
  const back = document.createElement('div');
  back.className = 'modal-backdrop';
  back.innerHTML = `<div class="modal" role="dialog" aria-modal="true" aria-labelledby="sc-title">
      <h2 id="sc-title">Keyboard shortcuts</h2>
      <p>Reject stays mouse-only. False positive is bound to <kbd>f</kbd> with a scope prompt; <kbd>Enter</kbd> applies the workspace component.</p>
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
  const closeBtn = back.querySelector('[data-x=close]');
  const onKey = (e) => {
    if (e.key === 'Escape') { e.preventDefault(); close(); }
    // The only focusable control is Close; keep Tab there so focus cannot escape behind the scrim.
    else if (e.key === 'Tab') { e.preventDefault(); closeBtn.focus(); }
  };
  document.addEventListener('keydown', onKey, true);
  closeBtn.onclick = close;
  closeBtn.focus();
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
  if (e.key === 'Enter') {
    if (view === 'workspace' && CL.state.workspaceId && CL.state.workspaceApproved) {
      e.preventDefault(); CL.click(() => CL.applyComponent(CL.state.workspaceId)); return;
    }
    if (CL.openCursor()) e.preventDefault();
    return;
  }
  if (e.key === 'o') { if (CL.openCursor()) e.preventDefault(); return; }
  if (view !== 'workspace') return;
  if (e.key === 'a') { if (CL.rowAction('approve_sl')) e.preventDefault(); return; }
  if (e.key === 'p') { if (CL.rowAction('accept_pl')) e.preventDefault(); return; }
  if (e.key === 'e') { if (CL.rowAction('correct')) e.preventDefault(); return; }
  if (e.key === 'f') { if (CL.rowAction('mark_false_positive')) e.preventDefault(); return; }
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
