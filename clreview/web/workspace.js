'use strict';
window.CL = window.CL || {};
var CL = window.CL;

// One empty-state token everywhere: an em dash, not "" / "no purl" / "{}".
CL.dash = (v) => { const s = String(v == null ? '' : v).trim(); return (!s || s === '{}' || s === '[]' || s === 'no purl') ? '—' : s; };

CL.renderWorkspace = async (id) => {
  const el = document.getElementById('view-workspace');
  if (!id) { el.innerHTML = '<p class="muted">No component selected.</p>'; return; }
  let d;
  try { d = await CL.api('GET', `/api/components/${id}`); }
  catch (e) { el.innerHTML = `<h1>Component #${CL.esc(id)}</h1><p class="badge bad">${CL.esc(e.message)}</p>`; return; }
  CL.state.detail = d;
  const m = d.match;
  const pl = d.pl && d.pl.component, sl = d.sl && d.sl.component;
  const cls = CL.statusClass(m.status);
  const side = (name, ver, missing) => name
    ? `${CL.esc(name)} <span class="muted num">${CL.esc(ver || '')}</span>`
    : `<span class="badge warn">${missing}</span>`;
  el.innerHTML = `
    <div class="panel">
      <h1>${side(m.pl_name, m.pl_version, 'no PL component')}
        <span class="muted">↔</span> ${side(m.sl_name, m.sl_version, 'no SL component')}</h1>
      <div class="grid2">
        <div class="kv">
          <dt>match</dt><dd>${CL.badge(m.status, cls)} <span class="small muted">${CL.esc(m.method)} · confidence <b class="num">${m.confidence.toFixed(2)}</b></span></dd>
          <dt>rationale</dt><dd class="small">${m.rationale.map(r => CL.esc(r)).join('<br>') || '—'}</dd>
          <dt>mismatch</dt><dd class="small">${m.mismatch_reasons.map(r => CL.esc(r)).join('<br>') || '—'}</dd>
          <dt>review</dt><dd>${CL.badge(m.review_status, CL.statusClass(m.review_status))} · <span class="num">${m.discrepancy_count}</span> discrepanc${m.discrepancy_count === 1 ? 'y' : 'ies'}</dd>
        </div>
        <div class="kv">
          <dt>PL component</dt><dd>${CL.esc(pl ? `${pl.name} ${pl.version || ''} · ${pl.tool}` : '—')}</dd>
          <dt>SL component</dt><dd>${CL.esc(sl ? `${sl.name} ${sl.version || ''} · ${sl.tool}` : '—')}</dd>
          <dt>PL purl</dt><dd class="small">${CL.dash(pl && pl.purl)}</dd>
          <dt>SL purl</dt><dd class="small">${CL.dash(sl && sl.purl)}</dd>
          <dt>PL snapshot</dt><dd class="small num">${CL.dash(pl && (pl.source_version || '').slice(0, 16))}</dd>
          <dt>SL snapshot</dt><dd class="small num">${CL.dash(sl && (sl.source_version || '').slice(0, 16))}</dd>
        </div>
      </div>
      <div class="row-actions mt-2">
        ${m.status === 'suggested' || m.status === 'unmatched' ? '<button id="w-confirm" class="primary">Confirm match</button>' : ''}
        <button id="w-reassign">Reassign…</button>
        <button id="w-reject" class="danger">Reject match</button>
        ${m.review_status === 'reviewed' ? '<button id="w-approve" class="primary">Approve component for synchronization</button>' : ''}
        ${m.review_status === 'approved_for_sync' ? '<button id="w-unapprove">Withdraw sync approval</button>' : ''}
      </div>
    </div>
    ${d.warnings.length ? `<div class="panel"><h2>⚠ Warnings</h3><ul class="warnlist">${d.warnings.map(w => `<li>${CL.esc(w)}</li>`).join('')}</ul></div>` : ''}
    ${d.stale.length ? `<div class="panel"><h2>Stale approvals</h3><table><thead><tr><th>field</th><th>entry</th><th>reason</th></tr></thead><tbody>${d.stale.map(s => `<tr><td>${CL.esc(s.field)}</td><td class="small">${CL.esc(s.entry_key.slice(0, 12))}</td><td>${CL.esc(s.reason)}</td></tr>`).join('')}</tbody></table>
      <p class="small muted">Re-review these fields before synchronizing; the source data changed after the decision.</p></div>` : ''}
    ${CL.copyrightPanel(d)}
    ${CL.licensePanel(d)}
    ${CL.textPanel(d)}
    ${CL.suggestionPanel(d)}
    ${CL.syncPanel(d)}
    ${CL.notesPanel(d)}
    ${d.candidates.length ? CL.candidatePanel(d) : ''}`;
  CL.bindWorkspace(d);
};

CL.copyrightPanel = (d) => {
  const rows = d.comparison.copyrights;
  return `<div class="panel"><h2>Copyright notices (PL vs SL)</h3>
    <p class="small muted">Missing or different data is not automatically wrong. Decide per notice; original observations are preserved.</p>
    ${rows.length ? `<table><thead><tr><th>class</th><th>PL notice</th><th>SL notice</th><th>decision</th><th>actions</th></tr></thead><tbody>
      ${rows.map(r => `<tr class="${r.rejected ? 'selected' : ''}">
        <td>${CL.badge(r.kind, r.kind === 'matched' ? 'ok' : (r.kind === 'duplicate' ? 'warn' : 'accent'))}</td>
        <td>${r.pl_text ? `<div class="notice pl_only">${CL.esc(r.pl_text)}</div>` : '<span class="muted">—</span>'}</td>
        <td>${r.sl_text ? `<div class="notice sl_only">${CL.esc(r.sl_text)}</div>` : '<span class="muted">—</span>'}</td>
        <td class="dec small">${r.decision ? CL.badge(r.decision.action, CL.statusClass(r.decision.action === 'approve_sl' ? 'reviewed' : '')) + '<br><span class="muted">' + CL.esc(r.decision.reviewer || '') + '</span>' : '—'}</td>
        <td class="acts row-actions">
          ${r.sl_text ? `<button class="tiny" data-dec="copyright" data-key="${CL.esc(r.key)}" data-action="approve_sl">Approve SL</button>` : ''}
          ${r.pl_text ? `<button class="tiny" data-dec="copyright" data-key="${CL.esc(r.key)}" data-action="accept_pl">Keep PL</button>` : ''}
          <button class="tiny" data-dec="copyright" data-key="${CL.esc(r.key)}" data-action="correct">Correct…</button>
          <button class="tiny" data-dec="copyright" data-key="${CL.esc(r.key)}" data-action="mark_false_positive">False positive</button>
          <button class="tiny" data-dec="copyright" data-key="${CL.esc(r.key)}" data-action="reject">Reject</button>
          <button class="tiny" data-dec="copyright" data-key="${CL.esc(r.key)}" data-action="note_only">Note…</button>
        </td></tr>`).join('')}
    </tbody></table>` : '<p class="muted">No copyright notices on either side.</p>'}
  </div>`;
};

CL.licensePanel = (d) => {
  const rows = d.comparison.licenses;
  return `<div class="panel"><h2>License names</h3>
    <p class="small muted">Names are compared exactly after normalization. Similar names are not treated as equivalent; SPDX differences are shown.</p>
    ${rows.length ? `<table><thead><tr><th>class</th><th>PL license</th><th>SL license</th><th>SPDX</th><th>evidence</th><th>decision</th><th>actions</th></tr></thead><tbody>
      ${rows.map(r => `<tr class="${r.rejected ? 'selected' : ''}">
        <td>${CL.badge(r.kind, r.kind === 'matched' ? 'ok' : 'accent')}</td>
        <td>${CL.esc(r.pl_name || '—')}</td>
        <td>${CL.esc(r.sl_name || '—')}</td>
        <td class="small">${CL.esc(r.pl_spdx || '—')} / ${CL.esc(r.sl_spdx || '—')}</td>
        <td class="small">${CL.esc((r.evidence_paths || []).join(', ') || '—')}</td>
        <td class="dec small">${r.decision ? CL.badge(r.decision.action) : '—'}</td>
        <td class="acts row-actions">
          ${r.sl_name ? `<button class="tiny" data-dec="license_name" data-key="${CL.esc(r.key)}" data-action="approve_sl">Approve SL</button>` : ''}
          ${r.pl_name ? `<button class="tiny" data-dec="license_name" data-key="${CL.esc(r.key)}" data-action="accept_pl">Keep PL</button>` : ''}
          <button class="tiny" data-dec="license_name" data-key="${CL.esc(r.key)}" data-action="correct">Correct…</button>
          <button class="tiny" data-dec="license_name" data-key="${CL.esc(r.key)}" data-action="mark_false_positive">False positive</button>
          <button class="tiny" data-dec="license_name" data-key="${CL.esc(r.key)}" data-action="reject">Reject</button>
          <button class="tiny" data-dec="license_name" data-key="${CL.esc(r.key)}" data-action="note_only">Note…</button>
        </td></tr>`).join('')}
    </tbody></table>` : '<p class="muted">No licenses on either side.</p>'}
  </div>`;
};

CL.textPanel = (d) => {
  const t = d.comparison.license_text;
  if (!t) return `<div class="panel"><h2>License text evidence</h3><p class="muted">No license text available on either side.</p></div>`;
  const cand = t.candidates.map(c => `<tr class="${c.preferred ? 'selected' : ''}">
      <td>${CL.badge(c.side.toUpperCase(), 'accent')}</td>
      <td>${CL.badge(c.kind, c.kind === 'source_file' ? 'ok' : 'warn')}</td>
      <td class="small">${CL.esc(c.file_path || c.file_name || '—')}</td>
      <td class="small">${CL.esc(c.provenance)}</td>
      <td class="num">${CL.esc((c.content_hash || '').slice(0, 12))}</td>
      <td>${c.preferred ? CL.badge('preferred', 'ok') : '<span class="muted">—</span>'}</td>
      <td>${c.side === 'sl' && c.kind === 'source_file' ? `<button class="tiny" data-dec="license_text" data-key="${CL.esc(c.content_hash)}" data-action="approve_sl">Use this text</button>` : '<span class="muted">—</span>'}</td>
    </tr>`).join('');
  const rec = t.recommendation;
  return `<div class="panel"><h2>License text: evidence &amp; provenance</h3>
    <p>${CL.badge(rec, rec === 'already_matches' ? 'ok' : (rec === 'no_source_text' ? 'bad' : 'warn'))}
      <span class="small muted">${t.reasons.map(r => CL.esc(r)).join(' · ')}</span></p>
    <div class="split">
      <div><div class="side-label">PL current text (${CL.esc(t.pl_current ? t.pl_current.kind : 'none')})</div>
        <div class="textbox"><pre>${CL.esc(t.pl_current ? t.pl_current.text : '—')}</pre></div></div>
      <div><div class="side-label">Preferred source-file text (${CL.esc(t.preferred ? (t.preferred.file_path || t.preferred.file_name) : 'none')})</div>
        <div class="textbox"><pre>${CL.esc(t.preferred ? t.preferred.text : '—')}</pre></div></div>
    </div>
    <table class="mt-2"><thead><tr><th>side</th><th>kind</th><th>file</th><th>provenance</th><th>hash</th><th>status</th><th>action</th></tr></thead><tbody>${cand}</tbody></table>
    <div class="row-actions mt-2">
      ${t.preferred ? `<button class="tiny" data-dec="license_text" data-key="${CL.esc(t.preferred.content_hash)}" data-action="approve_sl">Approve preferred text</button>` : ''}
      ${t.pl_current ? `<button class="tiny" data-dec="license_text" data-key="${CL.esc(t.preferred ? t.preferred.content_hash : 'no_source_text')}" data-action="accept_pl">Keep PL text</button>` : ''}
      <button class="tiny" data-dec="license_text" data-key="${CL.esc(t.preferred ? t.preferred.content_hash : 'no_source_text')}" data-action="reject">Mark irrelevant</button>
    </div>
    <p class="small muted">Synchronization only sends text from an identified source-file license (LICENSE, LICENCE, COPYING, NOTICE or equivalent). Generic metadata text is never sent.</p>
  </div>`;
};

CL.syncPanel = (d) => `<div class="panel"><h2>Approved synchronization preview</h3>
  ${d.sync_preview.length ? `<table><thead><tr><th>field</th><th>status</th><th>PL before</th><th>approved value</th><th>note</th></tr></thead><tbody>
    ${d.sync_preview.map(i => `<tr><td>${CL.esc(i.field)}</td><td>${CL.badge(i.status, CL.statusClass(i.status))}</td>
      <td class="small">${CL.esc(JSON.stringify(i.pl_before))}</td><td class="small">${CL.esc(String(i.approved_value || '').slice(0, 160))}</td><td class="small">${CL.esc(i.message)}</td></tr>`).join('')}
    </tbody></table>${d.match.review_state !== 'approved_for_sync' ? '<p class="small warn">Component is not approved for sync yet; nothing will be written.</p>' : ''}`
    : '<p class="muted">No approved SL-derived changes for this component.</p>'}
</div>`;

CL.notesPanel = (d) => `<div class="panel"><h2>Notes &amp; decision history</h3>
  ${d.notes.length ? d.notes.map(n => `<div class="notice"><b>${CL.esc(n.reviewer)}</b> <span class="small muted">${CL.esc(n.created_at)}</span><br>${CL.esc(n.body)}</div>`).join('') : '<p class="muted">No notes.</p>'}
  <label class="side-label mt-3" for="note-body">Review note</label>
  <textarea id="note-body" placeholder="What changed and why — kept with the component"></textarea>
  <div class="row-actions"><button id="add-note">Add note</button></div>
  ${d.decisions.length ? `<table class="mt-2"><thead><tr><th>field</th><th>entry</th><th>action</th><th>value</th><th>reviewer</th><th>decided</th></tr></thead><tbody>
    ${d.decisions.map(x => `<tr><td>${CL.esc(x.field)}</td><td class="small">${CL.esc(x.entry_key.slice(0, 10))}</td><td>${CL.badge(x.action)}</td><td class="small">${CL.esc(String(x.approved_value || '').slice(0, 80))}</td><td>${CL.esc(x.reviewer)}</td><td class="small">${CL.esc(x.created_at)}</td></tr>`).join('')}
  </tbody></table>` : ''}
</div>`;

CL.suggestionPanel = (d) => `<div class="panel"><h2>Machine suggestions (optional assistance)</h3>
  <p class="small muted">Suggestions are stored separately from human decisions. Accepting one creates a normal review decision; nothing is synchronized without component approval.</p>
  ${d.suggestions && d.suggestions.length ? `<table><thead><tr><th>field</th><th>entry</th><th>value</th><th>confidence</th><th>model</th><th>status</th><th>actions</th></tr></thead><tbody>
    ${d.suggestions.map(s => `<tr><td>${CL.esc(s.field)}</td><td class="small">${CL.esc(s.entry_key.slice(0, 10))}</td><td class="small">${CL.esc(String(s.value).slice(0, 120))}</td>
      <td class="num">${s.confidence.toFixed(2)}</td><td class="small">${CL.esc(s.model)}</td><td>${CL.badge(s.status, s.status === 'accepted' ? 'ok' : (s.status === 'rejected' ? 'bad' : 'warn'))}</td>
      <td class="acts row-actions">${s.status === 'open' ? `<button class="tiny" data-suggestion="${s.id}" data-sdec="accept">Accept</button><button class="tiny" data-suggestion="${s.id}" data-sdec="reject">Reject</button>` : '<span class="muted">—</span>'}</td></tr>`).join('')}
  </tbody></table>` : '<p class="muted">No suggestions recorded.</p>'}
</div>`;

CL.candidatePanel = (d) => `<div class="panel"><h2>Manual match candidates</h3>
  ${d.candidates.length ? `<table><thead><tr><th>SL component</th><th>version</th><th>similarity</th><th>action</th></tr></thead><tbody>
    ${d.candidates.map(c => `<tr><td>${CL.esc(c.sl_name)}</td><td class="num">${CL.dash(c.sl_version)}</td><td class="num">${c.similarity}</td><td><button class="tiny" data-assign="${CL.esc(c.sl_key)}">Assign</button></td></tr>`).join('')}
  </tbody></table>` : '<p class="muted">No unmatched SL components available.</p>'}
</div>`;

CL.bindWorkspace = (d) => {
  const id = d.match.id;
  const rerender = async () => { await CL.renderWorkspace(id); };
  const decide = (field, key, action) => CL.run(async () => {
    const body = { field, entry_key: key, action };
    if (action === 'correct') {
      const row = (field === 'copyright' ? d.comparison.copyrights : d.comparison.licenses).find(r => r.key === key);
      const current = row ? (row.sl_text || row.pl_text || row.sl_name || row.pl_name || '') : '';
      const value = await CL.dialog({
        title: 'Reviewed correction',
        message: 'This value is what will be written to the PL tool after component approval.',
        field: 'Corrected value', value: current, multiline: true, confirmText: 'Record decision',
      });
      if (value === null) return;
      body.approved_value = value;
    }
    if (action === 'note_only') {
      const note = await CL.dialog({ title: 'Note for this entry', field: 'Note', multiline: true, confirmText: 'Record note' });
      if (!note) return;
      body.note = note;
    }
    await CL.api('POST', `/api/components/${id}/decisions`, body);
    CL.toast('decision recorded');
    await rerender();
  });
  document.querySelectorAll('[data-dec]').forEach(b => b.onclick = () => decide(b.dataset.dec, b.dataset.key, b.dataset.action));
  const confirmBtn = document.getElementById('w-confirm');
  if (confirmBtn) confirmBtn.onclick = () => CL.run(async () => { await CL.api('POST', `/api/components/${id}/confirm`); await rerender(); });
  document.getElementById('w-reassign').onclick = () => CL.run(async () => {
    const key = await CL.dialog({
      title: 'Reassign SL component',
      message: 'Paste the SL component key to match this PL component against (see manual match candidates below).',
      field: 'SL component key', value: d.match.sl_key || '', confirmText: 'Reassign',
    });
    if (key === null) return;
    await CL.api('POST', `/api/components/${id}/reassign`, { sl_key: key.trim() });
    await rerender();
  });
  document.getElementById('w-reject').onclick = () => CL.run(async () => {
    if (!await CL.confirm('Reject this match? It will not be presented as a new discovery after refresh.', { title: 'Reject match', confirmText: 'Reject match' })) return;
    await CL.api('POST', `/api/components/${id}/reject`, { reason: 'reviewer rejected' });
    await rerender();
  });
  const approve = document.getElementById('w-approve');
  if (approve) approve.onclick = () => CL.run(async () => { await CL.api('POST', `/api/components/${id}/review`, { state: 'approved_for_sync' }); CL.toast('approved for synchronization'); await rerender(); });
  const unapprove = document.getElementById('w-unapprove');
  if (unapprove) unapprove.onclick = () => CL.run(async () => { await CL.api('POST', `/api/components/${id}/review`, { state: 'reviewed' }); await rerender(); });
  document.getElementById('add-note').onclick = () => CL.run(async () => {
    const body = document.getElementById('note-body').value.trim();
    if (!body) return;
    await CL.api('POST', `/api/components/${id}/notes`, { body });
    await rerender();
  });
  document.querySelectorAll('[data-suggestion]').forEach(b => b.onclick = () => CL.run(async () => {
    await CL.api('POST', `/api/suggestions/${b.dataset.suggestion}/decision`, { action: b.dataset.sdec });
    CL.toast('suggestion ' + b.dataset.sdec + 'ed');
    await rerender();
  }));
  document.querySelectorAll('[data-assign]').forEach(b => b.onclick = () => CL.run(async () => {
    await CL.api('POST', `/api/components/${id}/reassign`, { sl_key: b.dataset.assign });
    await rerender();
  }));
};
