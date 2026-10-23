'use strict';
window.CL = window.CL || {};
const CL = window.CL;

CL.state = { mappingId: null, mappings: [], reviewer: '', queue: { q: '', status: '', review_status: '', confidence: '', sort: 'name', offset: 0, limit: 50 }, jobId: null };

CL.esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
CL.badge = (text, cls) => `<span class="badge ${cls || ''}">${CL.esc(text)}</span>`;
CL.statusClass = (s) => ({ succeeded: 'ok', synchronized: 'ok', completed: 'ok', approved_for_sync: 'ok', reviewed: 'accent', failed: 'bad', conflict: 'bad', stale: 'warn', unsupported_target: 'warn', review_required: 'warn', in_progress: 'warn', suggested: 'warn', unmatched: 'bad', rejected: 'bad' }[s] || '');
CL.reviewer = () => document.getElementById('reviewer').value.trim() || 'reviewer';

CL.api = async (method, path, body) => {
  const res = await fetch(path, {
    method,
    headers: { 'Content-Type': 'application/json', 'X-Reviewer': CL.reviewer() },
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({ error: res.statusText }));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
};

CL.toast = (msg, isError) => {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.className = 'toast' + (isError ? ' error' : '');
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.add('hidden'), isError ? 7000 : 3500);
};

CL.run = async (fn) => {
  try { return await fn(); } catch (e) { CL.toast(e.message, true); throw e; }
};

CL.view = (name) => {
  document.querySelectorAll('.view').forEach(v => v.classList.add('hidden'));
  document.getElementById('view-' + name).classList.remove('hidden');
  document.querySelectorAll('.navlinks a').forEach(a => a.classList.toggle('active', a.dataset.view === name));
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
  const hash = location.hash.replace(/^#\/?/, '');
  if (!CL.state.mappingId) {
    await CL.loadMappings();
  }
  if (hash.startsWith('component/')) {
    CL.view('workspace');
    await CL.renderWorkspace(parseInt(hash.split('/')[1], 10));
    return;
  }
  if (hash === 'queue') { CL.view('queue'); await CL.renderQueue(); return; }
  if (hash === 'history') { CL.view('history'); await CL.renderHistory(); return; }
  CL.view('overview'); await CL.renderOverview();
};

// ---------- overview ----------
CL.renderOverview = async () => {
  const el = document.getElementById('view-overview');
  const mappings = CL.state.mappings;
  const m = CL.mapping();
  if (!m) {
    el.innerHTML = `<h1>Project overview</h1><div class="panel"><p class="muted">No PL↔SL mapping yet. Configure both connections below, then create a mapping.</p></div>` + await CL.connectionsPanel() + await CL.mappingForm();
    CL.bindOverview();
    return;
  }
  const ov = await CL.api('GET', `/api/mappings/${m.id}/overview`);
  const c = ov.counts;
  const sides = ov.sides;
  el.innerHTML = `
    <h1>${CL.esc(m.name)}</h1>
    <div class="stats">
      <div class="stat"><b>${c.matched}</b><span>matched components</span></div>
      <div class="stat"><b>${c.suggested}</b><span>match suggestions</span></div>
      <div class="stat"><b>${c.unmatched}</b><span>unmatched</span></div>
      <div class="stat"><b>${c.pending_review}</b><span>pending review</span></div>
      <div class="stat"><b>${c.approved}</b><span>approved for sync</span></div>
      <div class="stat"><b>${c.conflicts}</b><span>stale approvals</span></div>
    </div>
    <div class="grid2" style="margin-top:12px">
      <div class="panel"><h3>Retrieval status</h3>
        ${['pl', 'sl'].map(s => {
          const x = sides[s];
          const cls = x.status === 'ok' ? 'ok' : (x.status === 'error' ? 'bad' : 'warn');
          return `<div class="kv"><dt>${s.toUpperCase()} tool</dt><dd>${CL.esc(x.tool || '—')} ${CL.badge(x.status, cls)}</dd>
            <dt>retrieved</dt><dd>${CL.esc(x.retrieved_at || 'never')}</dd>
            <dt>components</dt><dd>${x.count}</dd>
            ${x.error ? `<dt>error</dt><dd class="badge bad">${CL.esc(x.error)}</dd>` : ''}</div>`;
        }).join('')}
      </div>
      <div class="panel"><h3>Mapping</h3>
        <div class="kv">
          <dt>PL connection</dt><dd>${CL.esc(m.pl_connection.name)} · ${CL.esc(m.pl_connection.tool)}</dd>
          <dt>PL project</dt><dd>${CL.esc(m.pl_project)} ${CL.esc(m.pl_version || '')}</dd>
          <dt>SL connection</dt><dd>${CL.esc(m.sl_connection.name)} · ${CL.esc(m.sl_connection.tool)}</dd>
          <dt>SL scan</dt><dd>${CL.esc(m.sl_scan)}</dd>
        </div>
      </div>
    </div>
    <div class="panel"><h3>Recent synchronization</h3>
      ${ov.recent_sync.length ? `<table><thead><tr><th>job</th><th>mode</th><th>status</th><th>summary</th><th>created</th></tr></thead><tbody>
        ${ov.recent_sync.map(j => `<tr><td><a href="#/history" data-job="${j.id}">#${j.id}</a></td><td>${j.dry_run ? 'dry run' : 'live'}</td><td>${CL.badge(j.status, CL.statusClass(j.status))}</td><td>${CL.esc(j.summary_json)}</td><td>${CL.esc(j.created_at)}</td></tr>`).join('')}
      </tbody></table>` : '<p class="muted">No synchronization yet.</p>'}
    </div>
    ${await CL.connectionsPanel()}
    ${await CL.mappingForm()}`;
  CL.bindOverview();
};

CL.connectionsPanel = async () => {
  const [conns, tools] = await Promise.all([CL.api('GET', '/api/connections'), CL.api('GET', '/api/tools')]);
  CL.state.connections = conns.items;
  CL.state.tools = tools.items;
  const toolOptions = (role) => tools.items.filter(t => t.roles.includes(role)).map(t => `<option value="${CL.esc(t.tool)}">${CL.esc(t.tool)}</option>`).join('');
  const rows = conns.items.map(c => `<tr><td>${c.id}</td><td>${CL.badge(c.role, 'accent')}</td><td>${CL.esc(c.tool)}</td><td>${CL.esc(c.name)}</td><td>${CL.esc(c.base_url || '—')}</td>
    <td>${c.last_test_ok ? CL.badge('ok', 'ok') : CL.badge(c.last_test_at ? 'failed' : 'untested', c.last_test_at ? 'bad' : 'warn')}</td>
    <td class="small">${CL.esc(c.last_test_error || '')}</td>
    <td><button class="tiny" data-test="${c.id}">Test</button></td></tr>`).join('');
  return `<div class="panel"><h3>Connections</h3>
    <table><thead><tr><th>id</th><th>role</th><th>tool</th><th>name</th><th>base url</th><th>test</th><th>error</th><th></th></tr></thead><tbody>${rows || '<tr><td colspan="8" class="muted">none configured</td></tr>'}</tbody></table>
    <div class="form-row" style="margin-top:10px">
      <label>role<select id="c-role"><option>pl</option><option>sl</option></select></label>
      <label>tool<select id="c-tool"></select></label>
      <label>name<input id="c-name" placeholder="Prod Black Duck"></label>
      <label>base url<input id="c-url" placeholder="https://blackduck.example"></label>
      <label>token env<input id="c-token-env" placeholder="BLACKDUCK_TOKEN"></label>
      <label>user env<input id="c-user-env" placeholder="FOSSOLOGY_USER"></label>
      <label>password env<input id="c-pass-env" placeholder="FOSSOLOGY_PASS"></label>
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
    document.getElementById('add-connection').onclick = () => CL.run(async () => {
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
  document.querySelectorAll('[data-test]').forEach(b => b.onclick = () => CL.run(async () => {
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
    document.getElementById('create-mapping').onclick = () => CL.run(async () => {
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
  } catch (e) { CL.toast('project discovery failed: ' + e.message, true); }
};

CL.loadScans = async () => {
  const id = document.getElementById('m-sl').value;
  if (!id) return;
  try {
    const data = await CL.api('GET', `/api/connections/${id}/scans`);
    document.getElementById('m-scan').innerHTML = data.items.map(s => `<option value="${CL.esc(s.id)}">${CL.esc(s.name)} ${CL.esc(s.date || '')}</option>`).join('');
  } catch (e) { CL.toast('scan discovery failed: ' + e.message, true); }
};

CL.mappingForm = () => `<div class="panel"><h3>New mapping</h3>
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
CL.renderQueue = async () => {
  const el = document.getElementById('view-queue');
  if (!CL.state.mappingId) { el.innerHTML = '<h1>Review queue</h1><p class="muted">Create a mapping first.</p>'; return; }
  const qs = new URLSearchParams({ q: CL.state.queue.q, review_status: CL.state.queue.review_status, status: CL.state.queue.status, confidence: CL.state.queue.confidence, sort: CL.state.queue.sort, offset: CL.state.queue.offset, limit: CL.state.queue.limit });
  const data = await CL.api('GET', `/api/mappings/${CL.state.mappingId}/components?${qs}`);
  el.innerHTML = `<h1>Review queue <span class="muted small">${data.total} components</span></h1>
    <div class="filters">
      <label>search name / version / license<input id="q-search" value="${CL.esc(CL.state.queue.q)}" placeholder="zlib"></label>
      <label>review status<select id="q-review">
        <option value="">any</option>${['not_reviewed', 'review_required', 'in_progress', 'reviewed', 'approved_for_sync'].map(s => `<option ${CL.state.queue.review_status === s ? 'selected' : ''}>${s}</option>`).join('')}
      </select></label>
      <label>match status<select id="q-status">
        <option value="">any</option>${['auto', 'suggested', 'confirmed', 'unmatched', 'rejected', 'manual'].map(s => `<option ${CL.state.queue.status === s ? 'selected' : ''}>${s}</option>`).join('')}
      </select></label>
      <label>confidence<select id="q-confidence"><option value="">any</option><option value="high" ${CL.state.queue.confidence === 'high' ? 'selected' : ''}>high (≥0.90)</option><option value="low" ${CL.state.queue.confidence === 'low' ? 'selected' : ''}>low (&lt;0.90)</option></select></label>
      <label>sort<select id="q-sort"><option value="name" ${CL.state.queue.sort === 'name' ? 'selected' : ''}>name</option><option value="confidence" ${CL.state.queue.sort === 'confidence' ? 'selected' : ''}>confidence</option><option value="discrepancies" ${CL.state.queue.sort === 'discrepancies' ? 'selected' : ''}>discrepancies</option><option value="status" ${CL.state.queue.sort === 'status' ? 'selected' : ''}>status</option></select></label>
      <button id="q-apply" class="primary">Apply</button>
    </div>
    <div class="panel"><table><thead><tr><th>PL component</th><th>SL component</th><th>match</th><th>confidence</th><th>licenses</th><th>discrepancies</th><th>review</th></tr></thead><tbody>
      ${data.items.map(r => `<tr class="clickable" data-id="${r.id}">
        <td><b>${CL.esc(r.pl_name || '—')}</b> <span class="muted">${CL.esc(r.pl_version || '')}</span></td>
        <td>${CL.esc(r.sl_name || '—')} <span class="muted">${CL.esc(r.sl_version || '')}</span></td>
        <td>${CL.badge(r.status, CL.statusClass(r.status))} <span class="small muted">${CL.esc(r.method)}</span></td>
        <td>${r.confidence.toFixed(2)}</td>
        <td class="small">${CL.esc((r.licenses || []).join(', '))}</td>
        <td>${r.discrepancy_count}</td>
        <td>${CL.badge(r.review_status, CL.statusClass(r.review_status))}</td></tr>`).join('') || '<tr><td colspan="7" class="muted">no components match the filters</td></tr>'}
    </tbody></table>
    <div class="pager"><button id="q-prev" ${data.offset === 0 ? 'disabled' : ''}>‹ prev</button>
      <span>${data.offset + 1}–${Math.min(data.offset + data.limit, data.total)} of ${data.total}</span>
      <button id="q-next" ${data.offset + data.limit >= data.total ? 'disabled' : ''}>next ›</button></div>
    </div>`;
  const apply = () => {
    CL.state.queue.q = document.getElementById('q-search').value;
    CL.state.queue.review_status = document.getElementById('q-review').value;
    CL.state.queue.status = document.getElementById('q-status').value;
    CL.state.queue.confidence = document.getElementById('q-confidence').value;
    CL.state.queue.sort = document.getElementById('q-sort').value;
    CL.state.queue.offset = 0;
    CL.renderQueue();
  };
  document.getElementById('q-apply').onclick = apply;
  document.getElementById('q-search').onkeydown = (e) => { if (e.key === 'Enter') apply(); };
  document.getElementById('q-prev').onclick = () => { CL.state.queue.offset = Math.max(0, CL.state.queue.offset - CL.state.queue.limit); CL.renderQueue(); };
  document.getElementById('q-next').onclick = () => { CL.state.queue.offset += CL.state.queue.limit; CL.renderQueue(); };
  el.querySelectorAll('tr.clickable').forEach(tr => tr.onclick = () => { location.hash = `#/component/${tr.dataset.id}`; });
};

// ---------- history ----------
CL.renderHistory = async () => {
  const el = document.getElementById('view-history');
  if (!CL.state.mappingId) { el.innerHTML = '<h1>Synchronization history</h1><p class="muted">Create a mapping first.</p>'; return; }
  const jobs = await CL.api('GET', `/api/sync/jobs?mapping_id=${CL.state.mappingId}`);
  const preview = await CL.api('POST', '/api/sync/preview', { mapping_id: CL.state.mappingId });
  const rows = preview.components.map(c => `<tr><td>${CL.esc(c.match.pl_name)} ${CL.esc(c.match.pl_version || '')}</td><td>${c.items.map(i => `<div>${CL.esc(i.field)}: ${CL.badge(i.status, CL.statusClass(i.status))} <span class="small">${CL.esc(i.message || '')}</span></div>`).join('')}</td></tr>`).join('');
  el.innerHTML = `<h1>Synchronization history</h1>
    <div class="panel"><h3>Pending synchronization preview</h3>
      <p class="small muted">Only components explicitly approved for synchronization appear here. Dry-run first; live writes require the confirmation checkbox and a verified PL connection.</p>
      ${preview.components.length ? `<table><thead><tr><th>component</th><th>approved changes</th></tr></thead><tbody>${rows}</tbody></table>
      <div class="checkline" style="margin-top:8px"><button id="sync-dry">Dry run</button><label><input type="checkbox" id="sync-confirm"> I approve these writes</label><button id="sync-live" class="primary" disabled>Execute synchronization</button></div>`
        : '<p class="muted">Nothing approved yet. Approve components in the review workspace.</p>'}
    </div>
    <div class="panel"><h3>Jobs</h3>
      ${jobs.items.length ? `<table><thead><tr><th>job</th><th>mode</th><th>status</th><th>summary</th><th>created by</th><th>finished</th></tr></thead><tbody>
        ${jobs.items.map(j => `<tr class="clickable" data-job="${j.id}"><td>#${j.id}</td><td>${j.dry_run ? 'dry run' : 'live'}</td><td>${CL.badge(j.status, CL.statusClass(j.status))}</td><td class="small">${CL.esc(JSON.stringify(j.summary))}</td><td>${CL.esc(j.created_by)}</td><td>${CL.esc(j.finished_at || '—')}</td></tr>`).join('')}
      </tbody></table>` : '<p class="muted">No jobs yet.</p>'}
      <div id="job-detail"></div>
    </div>`;
  const confirmBox = document.getElementById('sync-confirm');
  if (confirmBox) {
    confirmBox.onchange = () => { document.getElementById('sync-live').disabled = !confirmBox.checked; };
    document.getElementById('sync-dry').onclick = () => CL.run(async () => {
      const job = await CL.api('POST', '/api/sync/jobs', { mapping_id: CL.state.mappingId, dry_run: true });
      CL.toast(`dry-run job #${job.id}: ${JSON.stringify(job.summary)}`);
      await CL.renderHistory();
      await CL.showJob(job.id);
    });
    document.getElementById('sync-live').onclick = () => CL.run(async () => {
      const job = await CL.api('POST', '/api/sync/jobs', { mapping_id: CL.state.mappingId, dry_run: false, confirm: true });
      CL.toast(`synchronization job #${job.id}: ${JSON.stringify(job.summary)}`, job.status === 'failed');
      await CL.renderHistory();
      await CL.showJob(job.id);
    });
  }
  el.querySelectorAll('tr.clickable[data-job]').forEach(tr => tr.onclick = () => CL.showJob(parseInt(tr.dataset.job, 10)));
  if (CL.state.jobId) await CL.showJob(CL.state.jobId);
};

CL.showJob = async (id) => {
  CL.state.jobId = id;
  const job = await CL.api('GET', `/api/sync/jobs/${id}`);
  const box = document.getElementById('job-detail');
  if (!box) return;
  box.innerHTML = `<h3>Job #${job.id} · ${job.dry_run ? 'dry run' : 'live'} · ${CL.badge(job.status, CL.statusClass(job.status))}</h3>
    <p class="small muted">${CL.esc(job.created_at)} by ${CL.esc(job.created_by)} · summary ${CL.esc(JSON.stringify(job.summary))}</p>
    ${job.components.map(c => `<div class="panel"><b>${CL.esc(c.pl_name)} ${CL.esc(c.pl_version || '')}</b>
      <table><thead><tr><th>field</th><th>status</th><th>before</th><th>approved</th><th>api</th><th>message</th></tr></thead><tbody>
      ${c.items.map(i => `<tr><td>${CL.esc(i.field)}</td><td>${CL.badge(i.status, CL.statusClass(i.status))}</td><td class="small">${CL.esc(JSON.stringify(i.pl_before))}</td><td class="small">${CL.esc(String(i.approved_value || '').slice(0, 200))}</td><td>${i.api_status || ''}</td><td class="small">${CL.esc(i.message)}</td></tr>`).join('')}
      </tbody></table></div>`).join('')}
    ${job.items.some(i => i.status === 'failed') ? '<button id="retry-job" class="primary">Retry failed items…</button>' : ''}`;
  const retry = document.getElementById('retry-job');
  if (retry) retry.onclick = () => CL.run(async () => {
    if (!confirm('Retry failed items against the PL?')) return;
    const j = await CL.api('POST', `/api/sync/jobs/${id}/retry`, { confirm: true });
    CL.toast(`retry job #${j.id}: ${JSON.stringify(j.summary)}`, j.status === 'failed');
    await CL.renderHistory();
    await CL.showJob(j.id);
  });
};

// ---------- shell ----------
CL.refresh = () => CL.run(async () => {
  if (!CL.state.mappingId) throw new Error('no mapping selected');
  const r = await CL.api('POST', `/api/mappings/${CL.state.mappingId}/refresh`);
  const bad = Object.entries(r.sides).filter(([, v]) => v.status === 'error');
  CL.toast(bad.length ? 'refresh incomplete: ' + bad.map(([k, v]) => `${k}: ${v.error}`).join('; ') : `refreshed: PL ${r.sides.pl.count}, SL ${r.sides.sl.count}`, bad.length > 0);
  await CL.route();
});

document.getElementById('refresh-btn').onclick = CL.refresh;
document.getElementById('sync-btn').onclick = () => {
  if (location.hash === '#/history') CL.renderHistory();
  else location.hash = '#/history';
};
document.getElementById('mapping-select').onchange = (e) => {
  CL.state.mappingId = parseInt(e.target.value, 10) || null;
  localStorage.setItem('cl.mapping', String(CL.state.mappingId));
  CL.state.queue.offset = 0;
  CL.sideStatus();
  CL.route();
};
document.getElementById('reviewer').value = localStorage.getItem('cl.reviewer') || '';
document.getElementById('reviewer').onchange = (e) => localStorage.setItem('cl.reviewer', e.target.value);

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
