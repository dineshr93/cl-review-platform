'use strict';
window.CL = window.CL || {};
var CL = window.CL;

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
  el.innerHTML = `
    <div class="panel">
      <h1>${CL.esc(m.pl_name || '(no PL component)')} ${CL.esc(m.pl_version || '')}
        <span class="muted">↔</span> ${CL.esc(m.sl_name || '(no SL component)')} ${CL.esc(m.sl_version || '')}</h1>
      <div class="grid2">
        <div class="kv">
          <dt>match</dt><dd>${CL.badge(m.status, cls)} <span class="small">${CL.esc(m.method)} · confidence ${m.confidence.toFixed(2)}</span></dd>
          <dt>rationale</dt><dd class="small">${m.rationale.map(r => CL.esc(r)).join('<br>') || '—'}</dd>
          <dt>mismatch</dt><dd class="small">${m.mismatch_reasons.map(r => CL.esc(r)).join('<br>') || '—'}</dd>
          <dt>review</dt><dd>${CL.badge(m.review_status, CL.statusClass(m.review_status))} · ${m.discrepancy_count} discrepanc${m.discrepancy_count === 1 ? 'y' : 'ies'}</dd>
        </div>
        <div class="kv">
          <dt>PL component</dt><dd>${CL.esc(pl ? `${pl.name} ${pl.version || ''} · ${pl.tool}` : '—')}<br><span class="small muted">${CL.esc(pl ? (pl.purl || 'no purl') : '')}</span></dd>
          <dt>SL component</dt><dd>${CL.esc(sl ? `${sl.name} ${sl.version || ''} · ${sl.tool}` : '—')}<br><span class="small muted">${CL.esc(sl ? (sl.purl || 'no purl') : '')}</span></dd>
          <dt>PL identifiers</dt><dd class="small">${CL.esc(pl ? (pl.purl || 'no purl') : '')} ${CL.esc(pl ? (pl.metadata_json || '') : '')}</dd>
          <dt>SL identifiers</dt><dd class="small">${CL.esc(sl ? (sl.purl || 'no purl') : '')} ${CL.esc(sl ? (sl.metadata_json || '') : '')}</dd>
          <dt>PL hash</dt><dd class="small">${CL.esc((pl && pl.source_version || '').slice(0, 16))}</dd>
          <dt>SL hash</dt><dd class="small">${CL.esc((sl && sl.source_version || '').slice(0, 16))}</dd>
        </div>
      </div>
      <div class="row-actions" style="margin-top:8px">
        ${m.status === 'suggested' || m.status === 'unmatched' ? '<button id="w-confirm" class="primary">Confirm match</button>' : ''}
        <button id="w-reassign">Reassign…</button>
        <button id="w-reject" class="danger">Reject match</button>
        ${m.review_status === 'reviewed' ? '<button id="w-approve" class="primary">Approve component for synchronization</button>' : ''}
        ${m.review_status === 'approved_for_sync' ? '<button id="w-unapprove">Withdraw sync approval</button>' : ''}
      </div>
    </div>
    ${d.warnings.length ? `<div class="panel"><h3>Warnings</h3><ul class="warnlist">${d.warnings.map(w => `<li>${CL.esc(w)}</li>`).join('')}</ul></div>` : ''}
    ${d.stale.length ? `<div class="panel"><h3>Stale approvals</h3><table><thead><tr><th>field</th><th>entry</th><th>reason</th></tr></thead><tbody>${d.stale.map(s => `<tr><td>${CL.esc(s.field)}</td><td class="small">${CL.esc(s.entry_key.slice(0, 12))}</td><td>${CL.esc(s.reason)}</td></tr>`).join('')}</tbody></table>
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
  return `<div class="panel"><h3>Copyright notices (PL vs SL)</h3>
    <p class="small muted">Missing or different data is not automatically wrong. Decide per notice; original observations are preserved.</p>
    ${rows.length ? `<table><thead><tr><th>class</th><th>PL notice</th><th>SL notice</th><th>decision</th><th></th></tr></thead><tbody>
      ${rows.map(r => `<tr class="${r.rejected ? 'selected' : ''}">
        <td>${CL.badge(r.kind, r.kind === 'matched' ? 'ok' : (r.kind === 'duplicate' ? 'warn' : 'accent'))}</td>
        <td><div class="notice pl_only">${CL.esc(r.pl_text || '—')}</div></td>
        <td><div class="notice sl_only">${CL.esc(r.sl_text || '—')}</div></td>
        <td class="small">${r.decision ? CL.badge(r.decision.action, CL.statusClass(r.decision.action === 'approve_sl' ? 'reviewed' : '')) + '<br>' + CL.esc(r.decision.reviewer || '') : '—'}</td>
        <td class="row-actions">
          ${r.sl_text ? `<button class="tiny" data-dec="copyright" data-key="${CL.esc(r.key)}" data-action="approve_sl">Approve SL</button>` : ''}
          ${r.pl_text ? `<button class="tiny" data-dec="copyright" data-key="${CL.esc(r.key)}" data-action="accept_pl">Keep PL</button>` : ''}
          <button class="tiny" data-dec="copyright" data-key="${CL.esc(r.key)}" data-action="correct">Correct…</button>
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
  return `<div class="panel"><h3>License names</h3>
    <p class="small muted">Names are compared exactly after normalization. Similar names are not treated as equivalent; SPDX differences are shown.</p>
    ${rows.length ? `<table><thead><tr><th>class</th><th>PL license</th><th>SL license</th><th>SPDX</th><th>evidence</th><th>decision</th><th></th></tr></thead><tbody>
      ${rows.map(r => `<tr class="${r.rejected ? 'selected' : ''}">
        <td>${CL.badge(r.kind, r.kind === 'matched' ? 'ok' : 'accent')}</td>
        <td>${CL.esc(r.pl_name || '—')}</td>
        <td>${CL.esc(r.sl_name || '—')}</td>
        <td class="small">${CL.esc(r.pl_spdx || '—')} / ${CL.esc(r.sl_spdx || '—')}</td>
        <td class="small">${CL.esc((r.evidence_paths || []).join(', ') || '—')}</td>
        <td class="small">${r.decision ? CL.badge(r.decision.action) : '—'}</td>
        <td class="row-actions">
          ${r.sl_name ? `<button class="tiny" data-dec="license_name" data-key="${CL.esc(r.key)}" data-action="approve_sl">Approve SL</button>` : ''}
          ${r.pl_name ? `<button class="tiny" data-dec="license_name" data-key="${CL.esc(r.key)}" data-action="accept_pl">Keep PL</button>` : ''}
          <button class="tiny" data-dec="license_name" data-key="${CL.esc(r.key)}" data-action="correct">Correct…</button>
          <button class="tiny" data-dec="license_name" data-key="${CL.esc(r.key)}" data-action="mark_false_positive">False positive</button>
          <button class="tiny" data-dec="license_name" data-key="${CL.esc(r.key)}" data-action="note_only">Note…</button>
        </td></tr>`).join('')}
    </tbody></table>` : '<p class="muted">No licenses on either side.</p>'}
  </div>`;
};

CL.textPanel = (d) => {
  const t = d.comparison.license_text;
  if (!t) return `<div class="panel"><h3>License text evidence</h3><p class="muted">No license text available on either side.</p></div>`;
  const cand = t.candidates.map(c => `<tr class="${c.preferred ? 'selected' : ''}">
      <td>${CL.badge(c.side.toUpperCase(), 'accent')}</td>
      <td>${CL.badge(c.kind, c.kind === 'source_file' ? 'ok' : 'warn')}</td>
      <td class="small">${CL.esc(c.file_path || c.file_name || '—')}</td>
      <td class="small">${CL.esc(c.provenance)}</td>
      <td class="small">${CL.esc((c.content_hash || '').slice(0, 12))}</td>
      <td>${c.preferred ? CL.badge('preferred', 'ok') : ''}</td>
      <td>${c.side === 'sl' && c.kind === 'source_file' ? `<button class="tiny" data-dec="license_text" data-key="${CL.esc(c.content_hash)}" data-action="approve_sl">Use this text</button>` : ''}</td>
    </tr>`).join('');
  const rec = t.recommendation;
  return `<div class="panel"><h3>License text: evidence &amp; provenance</h3>
    <p>${CL.badge(rec, rec === 'already_matches' ? 'ok' : (rec === 'no_source_text' ? 'bad' : 'warn'))}
      <span class="small muted">${t.reasons.map(r => CL.esc(r)).join(' · ')}</span></p>
    <div class="split">
      <div><div class="side-label">PL current text (${CL.esc(t.pl_current ? t.pl_current.kind : 'none')})</div>
        <div class="textbox"><pre>${CL.esc(t.pl_current ? t.pl_current.text : '—')}</pre></div></div>
      <div><div class="side-label">Preferred source-file text (${CL.esc(t.preferred ? (t.preferred.file_path || t.preferred.file_name) : 'none')})</div>
        <div class="textbox"><pre>${CL.esc(t.preferred ? t.preferred.text : '—')}</pre></div></div>
    </div>
    <table style="margin-top:8px"><thead><tr><th>side</th><th>kind</th><th>file</th><th>provenance</th><th>hash</th><th></th><th></th></tr></thead><tbody>${cand}</tbody></table>
    <div class="row-actions" style="margin-top:8px">
      ${t.preferred ? `<button class="tiny" data-dec="license_text" data-key="${CL.esc(t.preferred.content_hash)}" data-action="approve_sl">Approve preferred text</button>` : ''}
      <button class="tiny" data-dec="license_text" data-key="${CL.esc(t.preferred ? t.preferred.content_hash : 'no_source_text')}" data-action="accept_pl">Keep PL text</button>
      <button class="tiny" data-dec="license_text" data-key="${CL.esc(t.preferred ? t.preferred.content_hash : 'no_source_text')}" data-action="reject">Mark irrelevant</button>
    </div>
    <p class="small muted">Synchronization only sends text from an identified source-file license (LICENSE, LICENCE, COPYING, NOTICE or equivalent). Generic metadata text is never sent.</p>
  </div>`;
};

CL.syncPanel = (d) => `<div class="panel"><h3>Approved synchronization preview</h3>
  ${d.sync_preview.length ? `<table><thead><tr><th>field</th><th>status</th><th>PL before</th><th>approved value</th><th>note</th></tr></thead><tbody>
    ${d.sync_preview.map(i => `<tr><td>${CL.esc(i.field)}</td><td>${CL.badge(i.status, CL.statusClass(i.status))}</td>
      <td class="small">${CL.esc(JSON.stringify(i.pl_before))}</td><td class="small">${CL.esc(String(i.approved_value || '').slice(0, 160))}</td><td class="small">${CL.esc(i.message)}</td></tr>`).join('')}
    </tbody></table>${d.match.review_state !== 'approved_for_sync' ? '<p class="small warn">Component is not approved for sync yet; nothing will be written.</p>' : ''}`
    : '<p class="muted">No approved SL-derived changes for this component.</p>'}
</div>`;

CL.notesPanel = (d) => `<div class="panel"><h3>Notes &amp; decision history</h3>
  ${d.notes.length ? d.notes.map(n => `<div class="notice"><b>${CL.esc(n.reviewer)}</b> <span class="small muted">${CL.esc(n.created_at)}</span><br>${CL.esc(n.body)}</div>`).join('') : '<p class="muted">No notes.</p>'}
  <textarea id="note-body" placeholder="Review note (kept with the component)"></textarea>
  <div class="row-actions"><button id="add-note">Add note</button></div>
  ${d.decisions.length ? `<table style="margin-top:8px"><thead><tr><th>field</th><th>entry</th><th>action</th><th>value</th><th>reviewer</th><th>decided</th></tr></thead><tbody>
    ${d.decisions.map(x => `<tr><td>${CL.esc(x.field)}</td><td class="small">${CL.esc(x.entry_key.slice(0, 10))}</td><td>${CL.badge(x.action)}</td><td class="small">${CL.esc(String(x.approved_value || '').slice(0, 80))}</td><td>${CL.esc(x.reviewer)}</td><td class="small">${CL.esc(x.created_at)}</td></tr>`).join('')}
  </tbody></table>` : ''}
</div>`;

CL.suggestionPanel = (d) => `<div class="panel"><h3>Machine suggestions (optional assistance)</h3>
  <p class="small muted">Suggestions are stored separately from human decisions. Accepting one creates a normal review decision; nothing is synchronized without component approval.</p>
  ${d.suggestions && d.suggestions.length ? `<table><thead><tr><th>field</th><th>entry</th><th>value</th><th>confidence</th><th>model</th><th>status</th><th></th></tr></thead><tbody>
    ${d.suggestions.map(s => `<tr><td>${CL.esc(s.field)}</td><td class="small">${CL.esc(s.entry_key.slice(0, 10))}</td><td class="small">${CL.esc(String(s.value).slice(0, 120))}</td>
      <td>${s.confidence.toFixed(2)}</td><td class="small">${CL.esc(s.model)}</td><td>${CL.badge(s.status, s.status === 'accepted' ? 'ok' : (s.status === 'rejected' ? 'bad' : 'warn'))}</td>
      <td class="row-actions">${s.status === 'open' ? `<button class="tiny" data-suggestion="${s.id}" data-sdec="accept">Accept</button><button class="tiny" data-suggestion="${s.id}" data-sdec="reject">Reject</button>` : ''}</td></tr>`).join('')}
  </tbody></table>` : '<p class="muted">No suggestions recorded.</p>'}
</div>`;

CL.candidatePanel = (d) => `<div class="panel"><h3>Manual match candidates</h3>
  ${d.candidates.length ? `<table><thead><tr><th>SL component</th><th>version</th><th>similarity</th><th></th></tr></thead><tbody>
    ${d.candidates.map(c => `<tr><td>${CL.esc(c.sl_name)}</td><td>${CL.esc(c.sl_version || '')}</td><td>${c.similarity}</td><td><button class="tiny" data-assign="${CL.esc(c.sl_key)}">Assign</button></td></tr>`).join('')}
  </tbody></table>` : '<p class="muted">No unmatched SL components available.</p>'}
</div>`;

CL.bindWorkspace = (d) => {
  const id = d.match.id;
  const rerender = async () => { await CL.renderWorkspace(id); };
  const decide = (field, key, action) => CL.run(async () => {
    let body = { field, entry_key: key, action };
    if (action === 'correct') {
      const row = (field === 'copyright' ? d.comparison.copyrights : d.comparison.licenses).find(r => r.key === key);
      const current = row ? (row.sl_text || row.pl_text || row.sl_name || row.pl_name || '') : '';
      const value = prompt('Reviewed correction value:', current);
      if (value === null) return;
      body.approved_value = value;
    }
    if (action === 'note_only') {
      const note = prompt('Note for this entry:');
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
    const key = prompt('SL component key to assign (see manual match candidates):', d.match.sl_key || '');
    if (key === null) return;
    await CL.api('POST', `/api/components/${id}/reassign`, { sl_key: key.trim() });
    await rerender();
  });
  document.getElementById('w-reject').onclick = () => CL.run(async () => {
    if (!confirm('Reject this match? It will not be presented as a new discovery after refresh.')) return;
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
