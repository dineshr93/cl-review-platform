'use strict';
window.CL = window.CL || {};
var CL = window.CL;

// Settings & health: connections and mappings (the admin the overview links to), service
// health, the editable license map, and the remembered false-positive rules.
// Six yes/no flags read as "4 of 6" with the list one click away; the full string squeezed the row.
CL.capabilities = (x) => {
  if (x.error) return CL.esc(x.error);
  const keys = ['write_license_names', 'write_license_text', 'write_copyrights',
                'write_review_status', 'write_upload_status', 'verify_writes'];
  const yes = keys.filter(k => x[k]).length;
  return `<details><summary>${yes} of ${keys.length}</summary>
    <ul class="small">${keys.map(k => `<li>${CL.esc(CL.words(k))}: ${x[k] ? 'yes' : '<span class="muted">no</span>'}</li>`).join('')}</ul></details>`;
};

CL.settingsBody = (health, maps, rules, catalog) => {
  const attention = [];
  if (health.db !== 'ok') attention.push(`Database: ${CL.esc(health.db)}.`);
  if (health.read_only) attention.push('Read-only mode: writes to Black Duck / FOSSology are refused.');
  catalog.conns.filter(c => !c.last_test_ok).forEach(c => attention.push(
    `Connection “${CL.esc(c.name)}” ${c.last_test_at ? `failed its last test${c.last_test_error ? `: ${CL.esc(c.last_test_error)}` : ''}` : 'has not been tested'}. Use Test in its row.`));
  return `${CL.viewHead('Settings &amp; health', '<span class="muted">Connections, mappings, license map and service health</span>', '')}
    ${CL.attention(attention)}
    ${CL.connectionsPanel(catalog, health)}
    ${CL.mappingsPanel()}
    ${CL.mappingForm()}
    <div class="panel"><h2>Service health</h2>
      <dl class="kv">
        <dt>app version</dt><dd>${CL.esc(health.version)}</dd>
        <dt>database</dt><dd>${CL.esc(health.db)}</dd>
        <dt>read-only</dt><dd>${health.read_only ? 'yes' : 'no'}</dd>
        <dt>time</dt><dd class="small">${CL.when(health.time)}</dd>
      </dl>
    </div>
    <div class="panel"><h2>License map (FO short name → SPDX → BD license)</h2>
      <p class="small muted">Editable local mapping. Approval of an SL license writes the mapped BD license identifier; source-file text is still taken from SL evidence.</p>
      <div class="form-actions"><button id="lm-suggest" class="primary-soft" title="Nothing is saved until you apply">Suggest entries from PL, SL and SPDX</button>
        <span class="small muted">Matches the SL license names in your snapshots to the PL license catalogue through the official SPDX list.</span></div>
      <div id="lm-proposals"></div>
      <table><thead><tr><th>FO short name</th><th>SPDX</th><th>BD license</th><th>source</th><th>updated</th><th>action</th></tr></thead><tbody>
        ${maps.items.map(r => `<tr><td>${CL.esc(r.short_name)}</td><td class="small">${CL.esc(r.spdx || '—')}</td>
          <td>${CL.esc(r.bd_license)}</td><td class="small">${CL.esc(r.source)}</td><td class="small">${CL.when(r.updated_at, '—')}</td>
          <td><button class="tiny danger-ghost" data-lm-delete="${r.id}">Delete</button></td></tr>`).join('') || '<tr><td colspan="6" class="muted">No mappings.</td></tr>'}
      </tbody></table>
      <div class="field-grid mt-2" id="license-map-form">
        <label>FO short name<input id="lm-short" placeholder="MIT"></label>
        <label>SPDX<input id="lm-spdx" placeholder="MIT"></label>
        <label>BD license<input id="lm-bd" placeholder="MIT"></label>
      </div>
      <div class="form-actions"><button id="lm-save" class="primary-soft">Save license mapping</button></div>
    </div>
    <div class="panel"><h2>Remembered false positives</h2>
      <p class="small muted">A rule ignores a license finding for this component, this version, or globally. Revoke a rule to re-surface the finding.</p>
      <table><thead><tr><th>license</th><th>scope</th><th>path pattern</th><th>created by</th><th>created</th><th>action</th></tr></thead><tbody>
        ${rules.items.map(r => `<tr><td>${CL.esc(r.license_name)}</td><td>${CL.badge(CL.words(r.scope))}</td>
          <td class="small">${CL.esc(r.path_pattern || '*')}</td><td>${CL.esc(r.created_by)}</td>
          <td class="small">${CL.when(r.created_at, '—')}</td>
          <td><button class="tiny ghost" data-rule-revoke="${r.id}">Revoke</button></td></tr>`).join('') || '<tr><td colspan="6" class="muted">No rules yet.</td></tr>'}
      </tbody></table>
    </div>`;
};

CL.renderSettings = async (fresh) => {
  const el = document.getElementById('view-settings');
  const [health, maps, rules, catalog] = await Promise.all([
    CL.api('GET', '/api/health'),
    CL.api('GET', '/api/license-map'),
    CL.api('GET', '/api/false-positive-rules'),
    CL.fetchCatalog(),
  ]);
  if (fresh && !fresh()) return;
  el.innerHTML = CL.settingsBody(health, maps, rules, catalog);
  CL.bindSettings();
  CL.bindConnections();
};

// Every mapping, with the one destructive action that removes it and all of its data.
CL.mappingsPanel = () => {
  const items = CL.state.mappings || [];
  const rows = items.map(m => `<tr>
      <td class="wrap">${CL.esc(m.name)}${m.id === CL.state.mappingId ? ` ${CL.badge('active', 'accent')}` : ''}</td>
      <td class="wrap small"><span class="muted">PL</span> ${CL.esc(m.pl_connection.name)} · ${CL.esc(`${m.pl_project} ${m.pl_version || ''}`.trim())}</td>
      <td class="wrap small"><span class="muted">SL</span> ${CL.esc(m.sl_connection.name)} · ${CL.esc(m.sl_scan)}</td>
      <td class="small">${CL.when(m.created_at, '—')}</td>
      <td><button class="tiny danger-ghost" data-delete-mapping="${m.id}">Delete…</button></td></tr>`).join('');
  return `<div class="panel"><h2>Mappings</h2>
    ${items.length ? `<table><thead><tr><th>name</th><th>PL</th><th>SL</th><th>created</th><th>action</th></tr></thead><tbody>${rows}</tbody></table>`
      : '<p class="muted">No mappings yet. Create one below.</p>'}
  </div>`;
};

// What goes is listed before anything is removed, and the name must be typed back: this deletes
// reviewer decisions, which nothing else in the console can do.
CL.deleteMapping = async (id) => {
  const u = await CL.api('GET', `/api/mappings/${id}/usage`);
  const c = u.counts;
  const lines = [[c.decisions, 'review decisions'], [c.notes, 'notes'], [c.suggestions, 'machine suggestions'],
    [c.matches, 'component matches'], [c.sync_jobs, 'synchronization jobs'], [c.snapshots, 'snapshots'],
    [c.components, 'stored components'], [c.false_positive_rules, 'false-positive rules scoped to it']];
  const typed = await CL.dialog({
    title: 'Delete mapping',
    message: `This permanently removes “${u.mapping.name}” and everything recorded under it:`,
    html: `<ul class="plan-list">${lines.map(([n, label]) => `<li><b class="num">${n}</b> ${label}</li>`).join('')}</ul>
      <p class="small">The audit trail is kept, and nothing changes in Black Duck or FOSSology. This cannot be undone.</p>`,
    field: 'Type the mapping name to confirm', confirmText: 'Delete mapping', danger: true,
  });
  if (typed === null) return;
  if (typed !== u.mapping.name) { CL.toast('the name does not match; nothing was deleted', true); return; }
  const r = await CL.api('DELETE', `/api/mappings/${id}?confirm=${encodeURIComponent(typed)}`);
  if (CL.state.mappingId === id) { CL.state.mappingId = null; localStorage.removeItem('cl.mapping'); }
  if (CL.state.targets) delete CL.state.targets[id];
  CL.state.queueFor = null;
  await CL.loadMappings();
  const freed = r.reclaimed_bytes >= 1048576 ? ` · ${(r.reclaimed_bytes / 1048576).toFixed(1)} MB freed` : '';
  CL.toast(`mapping “${r.name}” deleted${freed}`);
  await CL.renderSettings();
};

CL.bindSettings = () => {
  document.querySelectorAll('[data-delete-mapping]').forEach(b => b.onclick = () =>
    CL.click(() => CL.deleteMapping(parseInt(b.dataset.deleteMapping, 10))));
  document.getElementById('lm-save').onclick = () => CL.click(async () => {
    const short = document.getElementById('lm-short').value.trim();
    const spdx = document.getElementById('lm-spdx').value.trim();
    const bd = document.getElementById('lm-bd').value.trim();
    if (!short || !bd) { CL.toast('FO short name and BD license are required', true); return; }
    await CL.api('POST', '/api/license-map', { short_name: short, spdx, bd_license: bd });
    CL.toast('license mapping saved');
    await CL.renderSettings();
  });
  document.getElementById('lm-suggest').onclick = () => CL.click(CL.suggestLicenseMap);
  document.querySelectorAll('[data-lm-delete]').forEach(b => b.onclick = () => CL.click(async () => {
    if (!await CL.confirm('Delete this license mapping?', { title: 'Delete mapping', confirmText: 'Delete' })) return;
    await CL.api('DELETE', `/api/license-map/${b.dataset.lmDelete}`);
    CL.toast('license mapping deleted');
    await CL.renderSettings();
  }));
  document.querySelectorAll('[data-rule-revoke]').forEach(b => b.onclick = () => CL.click(async () => {
    if (!await CL.confirm('Revoke this false-positive rule? The finding re-surfaces on the next review.', { title: 'Revoke rule', confirmText: 'Revoke' })) return;
    await CL.api('POST', `/api/false-positive-rules/${b.dataset.ruleRevoke}/revoke`, {});
    CL.toast('false-positive rule revoked');
    await CL.renderSettings();
  }));
};

// ---------- connections & mappings (admin; moved from the overview) ----------
CL.connectionsPanel = (catalog, health) => {
  const { conns } = catalog;
  const live = {};
  ((health && health.connections) || []).forEach(h => { live[h.id] = h; });
  const rows = conns.map(c => { const h = live[c.id] || {}; return `<tr><td class="num">${c.id}</td><td>${CL.esc(c.name)}</td><td>${CL.badge(c.role, c.role === 'sl' ? 'accent' : '')}</td><td>${CL.esc(c.tool)}</td>
    <td class="small">${CL.esc(c.base_url || '—')}</td><td class="small">${CL.esc(h.api_version || '—')}</td>
    <td>${CL.connHealth(c)}${c.last_test_error ? `<div class="small muted">${CL.esc(c.last_test_error)}</div>` : ''}</td>
    <td class="small">${h.capabilities ? CL.capabilities(h.capabilities) : '—'}</td>
    <td><div class="row-actions"><button class="tiny" data-test="${c.id}">Test</button><button class="tiny ghost" data-edit="${c.id}">Edit</button>
      <span class="act-divider" aria-hidden="true"></span><button class="tiny danger-ghost" data-delete="${c.id}">Delete</button></div></td></tr>`; }).join('');
  return `<div class="panel"><h2>Connections</h2>
    <table><thead><tr><th>id</th><th>name</th><th>role</th><th>tool</th><th>base url</th><th>API version</th><th>test</th><th>permissions</th><th>actions</th></tr></thead><tbody>${rows || '<tr><td colspan="9" class="muted">No connections configured. Add one below.</td></tr>'}</tbody></table>
  </div>
  <div class="panel"><h2 id="c-title">Add connection</h2>
    <fieldset><legend>Identity</legend><div class="field-grid four">
      <label>role<select id="c-role"><option value="pl">pl (project list)</option><option value="sl">sl (scanner)</option></select></label>
      <label>tool<select id="c-tool"></select></label>
      <label>name<input id="c-name" placeholder="Prod Black Duck"></label>
      <label>base url<input id="c-url" placeholder="https://blackduck.example"></label>
    </div></fieldset>
    <fieldset><legend>Credentials (environment variable names)</legend>
      <p class="small muted">Credentials live only in the environment variables named here; tokens are never stored or returned by the API. With a token env given, user and password env can stay empty.</p>
      <div class="field-grid">
      <label>token env<input id="c-token-env" placeholder="MY_TOOL_TOKEN"></label>
      <label><span>user env <span class="muted">(optional)</span></span><input id="c-user-env" placeholder="MY_TOOL_USER"></label>
      <label><span>password env <span class="muted">(optional)</span></span><input id="c-pass-env" placeholder="MY_TOOL_PASS"></label>
    </div></fieldset>
    <details id="c-advanced"><summary>Advanced (fixture path, timeout, FOSSology workers and evidence)</summary><div class="field-grid">
      <label>fixture path<input id="c-fixtures" placeholder="fixtures/demo/pl.json"></label>
      <label>timeout s<input id="c-timeout" type="number" min="1" placeholder="default"></label>
      <label><span>workers <span class="muted">(FOSSology)</span></span><input id="c-workers" type="number" min="1" placeholder="8"></label>
      <label><span>evidence <span class="muted">(FOSSology)</span></span><select id="c-evidence"><option value="">default (summary)</option><option value="summary">summary</option><option value="files">files (slow)</option></select></label>
    </div></details>
    <div class="form-actions">
      <button id="add-connection" class="primary-soft">Add connection</button>
      <button id="cancel-edit" class="ghost hidden">Cancel edit</button>
    </div>
  </div>`;
};

CL.bindConnections = () => {
  const roleSel = document.getElementById('c-role');
  const toolSel = document.getElementById('c-tool');
  if (roleSel && toolSel) {
    const fill = () => {
      toolSel.innerHTML = (CL.state.tools || []).filter(t => t.roles.includes(roleSel.value)).map(t => `<option value="${CL.esc(t.tool)}">${CL.esc(t.tool)}</option>`).join('');
    };
    fill();
    roleSel.onchange = fill;
    let editing = null;
    const addBtn = document.getElementById('add-connection');
    const cancelBtn = document.getElementById('cancel-edit');
    const val = (id) => document.getElementById(id).value.trim();
    const resetForm = () => {
      editing = null;
      document.getElementById('c-title').textContent = 'Add connection';
      addBtn.textContent = 'Add connection';
      cancelBtn.classList.add('hidden');
      roleSel.disabled = toolSel.disabled = false;
      ['c-name', 'c-url', 'c-token-env', 'c-user-env', 'c-pass-env', 'c-fixtures', 'c-timeout', 'c-workers'].forEach(i => { document.getElementById(i).value = ''; });
      document.getElementById('c-evidence').value = '';
    };
    cancelBtn.onclick = resetForm;
    document.querySelectorAll('[data-edit]').forEach(b => b.onclick = () => {
      const c = (CL.state.connections || []).find(x => String(x.id) === b.dataset.edit);
      if (!c) return;
      editing = c.id;
      roleSel.value = c.role; fill(); toolSel.value = c.tool;
      roleSel.disabled = toolSel.disabled = true; // role and tool are fixed once mappings depend on them
      const cfg = c.config || {}, env = c.auth_env || {};
      document.getElementById('c-name').value = c.name;
      document.getElementById('c-url').value = c.base_url || '';
      document.getElementById('c-token-env').value = env.token || '';
      document.getElementById('c-user-env').value = env.username || '';
      document.getElementById('c-pass-env').value = env.password || '';
      document.getElementById('c-fixtures').value = cfg.fixtures || '';
      document.getElementById('c-timeout').value = cfg.timeout || '';
      document.getElementById('c-workers').value = cfg.workers || '';
      document.getElementById('c-evidence').value = cfg.evidence || '';
      document.getElementById('c-advanced').open = !!(cfg.fixtures || cfg.timeout || cfg.workers || cfg.evidence);
      document.getElementById('c-title').textContent = `Edit connection ${c.id}`;
      addBtn.textContent = 'Save changes';
      cancelBtn.classList.remove('hidden');
      document.getElementById('c-title').scrollIntoView({ block: 'center' });
    });
    document.querySelectorAll('[data-delete]').forEach(b => b.onclick = () => CL.click(async () => {
      if (!await CL.confirm('Delete this connection? Connections used by a mapping cannot be deleted.', { confirmText: 'Delete' })) return;
      await CL.api('DELETE', `/api/connections/${b.dataset.delete}`);
      CL.toast('connection deleted');
      await CL.loadMappings();
      await CL.renderSettings();
    }));
    addBtn.onclick = () => CL.click(async () => {
      const config = {};
      // keep config keys this form does not own (e.g. agents, folders_as_scans) when editing
      if (editing) {
        const old = (CL.state.connections || []).find(x => x.id === editing);
        Object.assign(config, old && old.config);
      }
      const set = (k, v) => { if (v === '' || v === undefined || Number.isNaN(v)) delete config[k]; else config[k] = v; };
      set('fixtures', val('c-fixtures'));
      set('timeout', val('c-timeout') === '' ? '' : Number(val('c-timeout')));
      set('workers', val('c-workers') === '' ? '' : parseInt(val('c-workers'), 10));
      set('evidence', val('c-evidence'));
      const auth = {};
      if (val('c-token-env')) auth.token = val('c-token-env');
      if (val('c-user-env')) auth.username = val('c-user-env');
      if (val('c-pass-env')) auth.password = val('c-pass-env');
      const body = { name: val('c-name') || toolSel.value, base_url: val('c-url'), config, auth_env: auth };
      if (editing) {
        await CL.api('POST', `/api/connections/${editing}`, body);
        CL.toast('connection updated');
      } else {
        await CL.api('POST', '/api/connections', Object.assign({ role: roleSel.value, tool: toolSel.value }, body));
        CL.toast('connection added');
      }
      await CL.loadMappings();
      await CL.renderSettings();
    });
  }
  document.querySelectorAll('[data-test]').forEach(b => b.onclick = () => CL.click(async () => {
    const r = await CL.api('POST', `/api/connections/${b.dataset.test}/test`);
    CL.toast(r.ok ? 'connection ok' : 'test failed: ' + (r.error || ''), !r.ok);
    await CL.renderSettings();
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
      const optionName = (id) => { const o = document.getElementById(id).selectedOptions[0]; return o ? (o.dataset.name || o.textContent) : ''; };
      const name = document.getElementById('m-name').value.trim()
        || CL.defaultMappingName(optionName('m-project'), version, optionName('m-scan'), project);
      const created = await CL.api('POST', '/api/mappings', { name, pl_connection_id: plId, pl_project: project, pl_version: version, sl_connection_id: slId, sl_scan: scan });
      CL.state.mappingId = created.id;
      localStorage.setItem('cl.mapping', String(created.id));
      await CL.loadMappings();
      await CL.refresh();
      location.hash = '#/overview';
    });
  }
};

CL.loadProjects = async () => {
  const id = document.getElementById('m-pl').value;
  if (!id) return;
  CL.selectLoading(document.getElementById('m-project'));
  CL.selectLoading(document.getElementById('m-version'));
  try {
    const data = await CL.api('GET', `/api/connections/${id}/projects?versions=0`);
    const proj = document.getElementById('m-project');
    proj.innerHTML = data.items.map(p => `<option value="${p.id}">${CL.esc(p.name)}</option>`).join('');
    const fillVersions = async () => {
      const sel = document.getElementById('m-version');
      sel.innerHTML = '';
      if (!proj.value) return;
      CL.selectLoading(sel);
      try {
        const v = await CL.api('GET', `/api/connections/${id}/projects/${encodeURIComponent(proj.value)}/versions`);
        sel.innerHTML = v.items.map(x => `<option value="${CL.esc(x)}">${CL.esc(x)}</option>`).join('');
      } catch (e) { CL.loadFailure('version', e); }
    };
    proj.onchange = fillVersions;
    await fillVersions();
  } catch (e) { CL.loadFailure('project', e); }
};

// Default mapping name: normalized "<PL project>--<PL version>--<SL folder>--<first 3 of the PL project id>".
// The id suffix keeps two projects with the same name apart without putting the whole hash in the name.
CL.defaultMappingName = (project, version, scan, projectId) => {
  const norm = (s) => String(s || '').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '');
  return [norm(project), norm(version), norm(String(scan || '').replace(/\s*\(folder\)\s*$/i, '')),
    norm(projectId).replace(/-/g, '').slice(0, 3)].join('--');
};

CL.loadScans = () => CL.loadScanOptions('m-scan', document.getElementById('m-sl').value, 'scans?folders_only=1', 'scan');

CL.mappingForm = () => `<div class="panel"><h2>New mapping</h2>
  <div id="mapping-form">
    <div class="split">
      <fieldset><legend>PL: project list</legend><div class="field-grid one">
        <label>PL connection<select id="m-pl"></select></label>
        <label>PL project<select id="m-project"></select></label>
        <label>PL version<select id="m-version"></select></label>
      </div></fieldset>
      <fieldset><legend>SL: scan</legend><div class="field-grid one">
        <label>SL connection<select id="m-sl"></select></label>
        <label>SL scan<select id="m-scan"></select></label>
      </div></fieldset>
    </div>
    <div class="field-grid"><label><span>name <span class="muted">(optional)</span></span><input id="m-name" placeholder="defaults to project--version--folder"></label></div>
    <div class="form-actions"><button id="create-mapping" class="primary-soft">Create mapping and refresh</button></div>
  </div>
</div>`;

// Proposals are only proposals: the confident ones are ticked, the rest wait for a choice, and
// nothing reaches the map until Apply. A row with several equal candidates offers them as a pick.
CL.LM_METHODS = { spdx_id: 'same SPDX id', exact_name: 'same name', name_same: 'same words and numbers', name_subset: 'SL words inside the PL name', none: 'no match' };
CL.suggestLicenseMap = async () => {
  const box = document.getElementById('lm-proposals');
  box.innerHTML = '<p class="small muted">Reading the PL license catalogue, SL license details and the SPDX list…</p>';
  const r = await CL.api('GET', '/api/license-map/suggestions');
  const src = r.sources, sk = r.skipped;
  const kept = [[sk.unchanged, 'already mapped the same way'], [sk.manual, 'set by a reviewer (never proposed over)'],
    [sk.direct, 'already a PL license name'], [sk.not_a_license, 'scanner verdicts, not licenses']]
    .filter(([n]) => n).map(([n, t]) => `${n} ${t}`).join(' · ');
  const warn = (r.warnings || []).map(w => `<li>${CL.esc(w)}</li>`).join('');
  const rows = r.items.map((p, i) => {
    const pick = p.candidates.length > 1
      ? `<select class="lm-bd" data-i="${i}" aria-label="PL license for ${CL.esc(p.short_name)}">${p.candidates.map(c =>
          `<option value="${CL.esc(c.bd_license)}" ${c.bd_license === p.bd_license ? 'selected' : ''}>${CL.esc(c.bd_license)} (${c.confidence.toFixed(2)})</option>`).join('')}</select>`
      : (p.bd_license ? CL.esc(p.bd_license) : '<span class="muted">—</span>');
    return `<tr${p.recommended ? ' class="picked"' : ''}>
      <td class="pick">${p.bd_license ? `<input type="checkbox" class="lm-pick" data-i="${i}" aria-label="Apply ${CL.esc(p.short_name)}" ${p.recommended ? 'checked' : ''}>` : ''}</td>
      <td>${CL.esc(p.short_name)}</td><td class="small">${CL.esc(p.spdx || '—')}</td><td class="wrap">${pick}</td>
      <td class="small">${CL.status(p.action)} ${CL.esc(CL.LM_METHODS[p.method] || p.method)}${p.confidence ? ` <span class="num">${p.confidence.toFixed(2)}</span>` : ''}${p.note ? `<div class="muted">${CL.esc(p.note)}</div>` : ''}</td>
      <td class="small">${CL.esc(p.current || '—')}</td></tr>`;
  }).join('');
  box.innerHTML = `<section class="mt-2"><h3>Proposals</h3>
      <p class="small"><b>${r.items.length}</b> proposal${r.items.length === 1 ? '' : 's'} from <b class="num">${src.sl_names}</b> SL license names,
        <b class="num">${src.pl_licenses}</b> PL licenses and <b class="num">${src.spdx_licenses}</b> SPDX licenses.
        ${kept ? `<span class="muted">Not proposed: ${kept}.</span>` : ''}</p>
      ${warn ? `<ul class="small warnlist">${warn}</ul>` : ''}
      ${r.items.length ? `<table><thead><tr><th class="pick"><input type="checkbox" id="lm-pick-all" aria-label="Select every proposal"></th>
        <th>SL name</th><th>SPDX</th><th>proposed BD license</th><th>why</th><th>current</th></tr></thead><tbody>${rows}</tbody></table>
      <div class="form-actions"><button id="lm-apply" class="primary">Apply selected</button><button id="lm-dismiss" class="ghost">Dismiss</button></div>`
        : '<p class="small muted">Nothing to propose: every SL license name is already settled.</p>'}
    </section>`;
  const picks = () => [...box.querySelectorAll('.lm-pick')];
  const count = () => {
    const n = picks().filter(c => c.checked).length;
    const btn = document.getElementById('lm-apply');
    if (btn) { btn.textContent = `Apply ${n} selected`; btn.disabled = !n; }
    picks().forEach(c => c.closest('tr').classList.toggle('picked', c.checked));
  };
  picks().forEach(c => c.onchange = count);
  const all = document.getElementById('lm-pick-all');
  if (all) all.onchange = () => { picks().forEach(c => { c.checked = all.checked; }); count(); };
  box.querySelectorAll('.lm-bd').forEach(sel => sel.onchange = () => {
    r.items[sel.dataset.i].bd_license = sel.value;
    const c = box.querySelector(`.lm-pick[data-i="${sel.dataset.i}"]`);
    if (c) { c.checked = true; count(); }
  });
  const dismiss = document.getElementById('lm-dismiss');
  if (dismiss) dismiss.onclick = () => { box.innerHTML = ''; };
  const apply = document.getElementById('lm-apply');
  if (apply) apply.onclick = () => CL.click(async () => {
    const items = picks().filter(c => c.checked).map(c => r.items[c.dataset.i])
      .map(p => ({ short_name: p.short_name, spdx: p.spdx, bd_license: p.bd_license }));
    const res = await CL.api('POST', '/api/license-map/apply', { items });
    CL.toast(`${res.applied} license mappings saved; queue counts follow on the next refresh`);
    await CL.renderSettings();
  });
  count();
};
