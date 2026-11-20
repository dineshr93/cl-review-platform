'use strict';
window.CL = window.CL || {};
var CL = window.CL;

// One empty-state token everywhere: an em dash, not "" / "no purl" / "{}".
CL.dash = (v) => { const s = String(v == null ? '' : v).trim(); return (!s || s === '{}' || s === '[]' || s === 'no purl') ? '—' : s; };

// ---------- word-level diff ----------
// A reviewer reads two strings looking for the difference, not for equality. Showing which
// WORDS changed is what makes a copyright variant decidable at a glance.
//
// Whitespace is kept as its own token so a license text keeps its line breaks: the stream is
// [word, gap, word, gap, ...] and only the word tokens take part in the diff.
//
// SECURITY ORDER: every token is escaped with CL.esc BEFORE it is wrapped in <mark>. The
// reverse would put raw scanner text inside an element and turn the diff into an injection
// point. _paintDiff is the only place a <mark> is produced, and it escapes first.
const DIFF_MAX_TOKENS = 1500;   // per side after trimming; bounds the DP table near 9 MB

const meaningful = (toks) => {
  const idx = [];
  for (let i = 0; i < toks.length; i++) if (toks[i].trim()) idx.push(i);
  return idx;
};

// Longest-common-subsequence membership for two token arrays, as parallel boolean lists.
const lcsKeep = (A, B) => {
  const n = A.length, m = B.length;
  const dp = [];
  for (let i = 0; i <= n; i++) dp.push(new Int32Array(m + 1));
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      dp[i][j] = A[i] === B[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
    }
  }
  const a = new Array(n).fill(false), b = new Array(m).fill(false);
  let i = 0, j = 0;
  while (i < n && j < m) {
    if (A[i] === B[j]) { a[i] = true; b[j] = true; i++; j++; }
    else if (dp[i + 1][j] >= dp[i][j + 1]) i++;
    else j++;
  }
  return { a, b };
};

// Indices of the tokens that are NOT part of the common subsequence, per side.
// null means "do not highlight": identical texts, an empty side, or too many tokens to
// diff honestly inside the browser's budget.
CL._diffIndices = (leftText, rightText) => {
  const L = String(leftText == null ? '' : leftText).split(/(\s+)/);
  const R = String(rightText == null ? '' : rightText).split(/(\s+)/);
  const li = meaningful(L), ri = meaningful(R);
  if (!li.length || !ri.length) return { left: null, right: null };
  if (li.length === ri.length && li.every((t, k) => L[t] === R[ri[k]])) return { left: null, right: null };
  // Trim the shared prefix/suffix of the word stream, so a one-word edit in a long text
  // costs a small table instead of a big one.
  let p = 0;
  while (p < li.length && p < ri.length && L[li[p]] === R[ri[p]]) p++;
  let ea = li.length, eb = ri.length;
  while (ea > p && eb > p && L[li[ea - 1]] === R[ri[eb - 1]]) { ea--; eb--; }
  const midA = li.slice(p, ea), midB = ri.slice(p, eb);
  if (!midA.length || !midB.length) return { left: null, right: null };
  if (midA.length > DIFF_MAX_TOKENS || midB.length > DIFF_MAX_TOKENS) return { left: null, right: null };
  const keep = lcsKeep(midA.map(k => L[k]), midB.map(k => R[k]));
  const left = new Set(), right = new Set();
  midA.forEach((tokIdx, k) => { if (!keep.a[k]) left.add(tokIdx); });
  midB.forEach((tokIdx, k) => { if (!keep.b[k]) right.add(tokIdx); });
  return { left, right };
};

// Escape first, then wrap. A token that is only whitespace is never marked.
CL._paintDiff = (text, changed, cls) => {
  if (!changed) return CL.esc(text);
  const toks = String(text == null ? '' : text).split(/(\s+)/);
  return toks.map((t, i) => {
    const safe = CL.esc(t);
    return changed.has(i) && t.trim() ? `<mark class="${cls}">${safe}</mark>` : safe;
  }).join('');
};

// Both sides at once, already escaped; safe to interpolate into innerHTML.
CL.diffHtml = (leftText, rightText) => {
  const flags = CL._diffIndices(leftText, rightText);
  return {
    left: CL._paintDiff(leftText, flags.left, 'd-del'),
    right: CL._paintDiff(rightText, flags.right, 'd-add'),
    diffed: !!(flags.left || flags.right),
  };
};

// ---------- decision progress ----------
// The reviewer's question is "how much of THIS component is still mine to decide".
// One decision point per compared entry: every copyright notice, every license pair, and
// the license text as a single point. A row carrying a decision is a point that is closed.
CL.decisionProgress = (d) => {
  const c = (d && d.comparison) || {};
  const cr = c.copyrights || [], lic = c.licenses || [];
  const texts = c.license_texts || (c.license_text ? [c.license_text] : []);
  const total = cr.length + lic.length + texts.length;   // one point per license's text
  const decided = cr.filter(r => r.decision).length + lic.filter(r => r.decision).length
    + texts.filter(t => t.decision).length;
  return { total, decided: Math.max(0, Math.min(total, decided)) };
};

// Diff highlighting is on by default and switchable per component. It lives on CL (not
// CL.state, which app.js creates after this file loads) so a re-render after a decision
// keeps the reviewer's choice instead of snapping back.
CL.diffOn = true;

// One control for the whole workspace, rendered in the sticky bar. It toggles a class on the
// workspace root rather than re-rendering: the marks stay in the DOM, only their paint changes.
CL.diffToggle = (id) => `<button ${id ? `id="${id}" ` : ''}class="${id ? '' : 'tiny '}diff-toggle" role="switch" aria-checked="${CL.diffOn}"
  title="Highlight the words that differ between the two sides">${CL.diffOn ? 'Highlight diff' : 'Plain text'}</button>`;

// A re-render replaces the panel, so remember which control the reviewer was on and hand focus back to its twin.
CL.focusKey = (el) => !el || el === document.body || el === document.documentElement ? null
  : el.id ? `#${el.id}`
  : el.dataset.dec ? `[data-dec="${el.dataset.dec}"][data-key="${CSS.escape(el.dataset.key)}"][data-action="${el.dataset.action}"]`
  : el.dataset.suggestion ? `[data-suggestion="${el.dataset.suggestion}"][data-sdec="${el.dataset.sdec}"]`
  : el.dataset.assign ? `[data-assign="${CSS.escape(el.dataset.assign)}"]` : null;

CL.renderWorkspace = async (id, fresh) => {
  CL.state.workspaceId = id;
  const el = document.getElementById('view-workspace');
  if (!id) { el.innerHTML = '<p class="muted">No component selected.</p>'; return; }
  let d;
  try { d = await CL.api('GET', `/api/components/${id}`); }
  catch (e) {
    if (fresh && !fresh()) return;
    el.innerHTML = `<h1>Component #${CL.esc(id)}</h1>
      <div class="panel"><p>A read of this component failed: ${CL.badge(e.message, 'bad')}</p>
      <div class="row-actions mt-3"><button id="w-reload" class="primary">Retry</button></div></div>`;
    document.getElementById('w-reload').onclick = () => CL.click(() => CL.renderWorkspace(id));
    return;
  }
  if (fresh && !fresh()) return;          // a newer navigation won while this was in flight
  CL.state.workspaceApproved = d.match.review_state === CL.reviewState.APPROVED;
  const focusKey = CL.focusKey(document.activeElement);
  const m = d.match;
  const pl = d.pl && d.pl.component, sl = d.sl && d.sl.component;
  const cls = CL.statusClass(m.status);
  const side = (name, ver, missing) => name
    ? `${CL.esc(name)} <span class="muted num">${CL.esc(ver || '')}</span>`
    : `<span class="badge warn">${missing}</span>`;
  const prog = CL.decisionProgress(d);
  const fo = CL.slLink(d);
  // the mapping this component belongs to (a link may open one that is not the active mapping)
  const mapping = (CL.state.mappings || []).find(x => x.id === m.mapping_id) || CL.mapping();
  const ctx = `${mapping ? `<span class="view-ctx-line">${CL.mappingCtx(mapping)}</span>` : ''}${CL.status(m.status)} <span class="small muted">${CL.esc(m.method)} · confidence <b class="num">${m.confidence.toFixed(2)}</b></span>
    ${CL.status(m.review_status)}
    <span class="small"><span class="num">${m.discrepancy_count}</span> discrepanc${m.discrepancy_count === 1 ? 'y' : 'ies'} · <span class="num">${prog.decided}/${prog.total}</span> decided</span>
    ${fo ? `<span class="small">${fo}</span>` : ''}`;
  const actions = `${m.status === 'suggested' || m.status === 'unmatched' ? '<button id="w-confirm" class="primary-soft">Confirm match</button>' : ''}
    <button id="w-reassign">Edit match…</button>
    ${CL.diffToggle('diff-toggle')}
    ${m.pl_key && m.sl_key && m.status !== 'rejected' ? '<span class="act-divider" aria-hidden="true"></span><button id="w-reject" class="danger-ghost">Unlink</button>' : ''}`;
  const attention = d.warnings.map(w => CL.esc(w));
  if (d.stale.length) attention.push(`${d.stale.length} stale approval${d.stale.length === 1 ? '' : 's'}: the source data changed after the decision. Re-decide ${d.stale.map(x => `<b>${CL.esc(CL.words(x.field))}</b> (${CL.esc(x.reason)})`).join(', ')} before synchronizing.`);
  el.innerHTML = `
    ${CL.viewHead(`${side(m.pl_name, m.pl_version, 'no PL component')} <span class="muted">↔</span> ${side(m.sl_name, m.sl_version, 'no SL component')}`, ctx, actions)}
    ${CL.attention(attention)}
    ${d.candidates.length ? CL.candidatePanel(d) : ''}
    ${CL.copyrightPanel(d)}
    ${CL.licensePanel(d)}
    ${CL.textPanel(d)}
    ${CL.syncPanel(d)}
    ${CL.evidencePanel(d, pl, sl)}
    ${CL.notesPanel(d)}
    ${CL.suggestionPanel(d)}
    ${CL.wsStickyBar(d)}`;
  CL.bindWorkspace(d);
  CL.bindStickyBar(el);
  if (mapping) CL.resolveMappingTargets(mapping, fresh, el);
  const target = (focusKey && el.querySelector(focusKey)) || el.querySelector('h1');
  if (target) target.focus({ preventScroll: true });
};

// The sticky context bar: the four things a reviewer re-checks every few rows (which
// component, how sure the match is, where the review stands, how much is left) held at the
// top of the viewport while the panels scroll.
//
// It is rendered LAST in the DOM and positioned fixed, so it never sits over the h1: an
// IntersectionObserver on the h1 hides it while the title is still on screen. The bar holds
// <span>s only — the h1 stays the single h1 of the view.
CL.wsStickyBar = (d) => {
  const m = d.match;
  const prog = CL.decisionProgress(d);
  const pct = prog.total ? Math.round((prog.decided / prog.total) * 100) : 0;
  const done = prog.total > 0 && prog.decided >= prog.total;
  return `<div class="ws-sticky hidden" id="ws-sticky" role="group" aria-label="Component review context">
    <span class="ws-name">${CL.esc(CL.dash(m.pl_name || m.sl_name || `component #${m.id}`))}</span>
    <span class="ws-badges">${CL.status(m.status)}
      <span class="ws-conf small muted">${m.method} · ${m.confidence.toFixed(2)}</span></span>
    <span class="ws-badges">${CL.status(m.review_status)}</span>
    <span class="ws-prog" role="img" aria-label="${prog.decided} of ${prog.total} decision points decided">
      <span class="ws-meter" aria-hidden="true"><i style="width:${pct}%"></i></span>
      <span class="ws-count num${done ? ' done' : ''}">${prog.decided}/${prog.total}</span>
    </span>
    ${CL.diffToggle()}
  </div>`;
};

// Reveal the bar only once the h1 has scrolled out of view, so the title is never covered.
CL.bindStickyBar = (root) => {
  const bar = root.querySelector('#ws-sticky');
  const title = root.querySelector('h1');
  if (!bar || !title) return;
  if (!('IntersectionObserver' in window)) { bar.classList.add('hidden'); return; }
  const observer = new IntersectionObserver((entries) => {
    // Below the layout breakpoint the bar is display:none anyway; keep it hidden there.
    const narrow = window.matchMedia('(max-width: 880px)').matches;
    const visible = entries.length ? entries[0].isIntersecting : false;
    bar.classList.toggle('hidden', narrow || visible);
  }, { threshold: 0 });
  observer.observe(title);
  window.addEventListener('resize', () => {
    const narrow = window.matchMedia('(max-width: 880px)').matches;
    if (narrow) bar.classList.add('hidden');
  }, { passive: true });
};

// The six decision actions are the same for every compared field; only the field name, the
// entry key and whether each side holds a value change. Written once so the copyright and
// license panels cannot drift apart.
//
// The six are NOT styled alike. A reviewer decides the same way for most rows — the SL
// evidence is right — so that one button carries the row's emphasis; the edits are quiet;
// and the two destructive actions are pushed to the far end behind a divider, because a
// mis-click on six identical buttons used to be a coin flip.
CL.decisionButtons = (field, key, hasSl, hasPl) => {
  const k = `data-dec="${field}" data-key="${CL.esc(key)}"`;
  const parts = [];
  if (hasSl) parts.push(`<button class="tiny primary-soft" ${k} data-action="approve_sl" title="Take the SL value; this is what a sync will write">Approve SL</button>`);
  if (hasPl) parts.push(`<button class="tiny" ${k} data-action="accept_pl" title="Leave the PL value as it is">Keep PL</button>`);
  parts.push(`<button class="tiny ghost" ${k} data-action="correct">Correct…</button>`);
  parts.push(`<button class="tiny ghost" ${k} data-action="note_only">Note…</button>`);
  parts.push('<span class="act-divider" aria-hidden="true"></span>');
  parts.push('<span class="danger-cluster">'
    + `<button class="tiny danger-ghost" ${k} data-action="mark_false_positive" title="The scanner finding is noise; nothing is written">False positive</button>`
    + `<button class="tiny danger-ghost" ${k} data-action="reject" title="Discard this entry from the comparison">Reject</button>`
    + '</span>');
  return parts.join(' ');
};

// Bulk decisions: a pick column and a bar per table. The bar offers only the actions that make
// sense for a set; correct and note stay single-row because each needs its own value.
CL.rowPick = (field, key, label) => `<td class="pick"><input type="checkbox" class="row-pick" data-pick-field="${field}"
  data-pick-key="${CL.esc(key)}" aria-label="Select ${CL.esc(label)}"></td>`;
CL.tableBulkBar = (field, falsePositive) => `<div class="bulk-bar" data-bulk-field="${field}" role="region" aria-label="Selected rows" hidden>
  <b class="num" data-bulk-count>0</b> selected
  <span class="bulk-actions row-actions">
    <button class="tiny primary-soft" data-bulk-dec="approve_sl">Approve SL</button>
    <button class="tiny" data-bulk-dec="accept_pl">Keep PL</button>
    <span class="act-divider" aria-hidden="true"></span>
    ${falsePositive ? '<button class="tiny danger-ghost" data-bulk-dec="mark_false_positive" title="Remembered for this component">False positive</button>' : ''}
    <button class="tiny danger-ghost" data-bulk-dec="reject">Reject</button>
  </span></div>`;
CL.pickHead = (field) => `<th class="pick"><input type="checkbox" class="row-pick-all" data-pick-field="${field}"
  aria-label="Select every row that needs a decision"></th>`;

// Link to the source-level upload for file-level detail; only http(s) URLs are rendered as links.
CL.slLink = (d) => {
  let meta = {};
  try { meta = JSON.parse((d.sl && d.sl.component && d.sl.component.metadata_json) || '{}'); } catch (e) { /* no metadata */ }
  if (!/^https?:\/\//.test(meta.fossology_url || '')) return '';
  return `<a href="${CL.esc(meta.fossology_url)}" target="_blank" rel="noopener noreferrer">FOSSology upload ${CL.esc(String(meta.upload_id || ''))}</a>`;
};

// A decision reads as words with who made it; the wire value stays on hover.
CL.decisionCell = (dec) => dec
  ? `<span class="badge ${dec.action === 'approve_sl' ? 'accent' : ''}" title="${CL.esc(dec.action)}">${CL.esc(CL.words(dec.action))}</span><br><span class="muted">${CL.esc(dec.reviewer || '')}</span>`
  : '—';

// Machine suggestions sit in the row they are about, so the reviewer sees them while deciding.
CL.openSuggestions = (d, field, key) => (d.suggestions || []).filter(x => x.status === 'open' && x.field === field && x.entry_key === key);
CL.suggestionChips = (list) => list.map(x => `<div class="sugg"><span class="badge" title="${CL.esc(x.model)}">suggestion ${x.confidence.toFixed(2)}</span>
    <span class="small">${CL.esc(String(x.value).slice(0, 120))}</span>
    <button class="tiny ghost" data-suggestion="${x.id}" data-sdec="accept">Accept</button><button class="tiny danger-ghost" data-suggestion="${x.id}" data-sdec="reject">Reject</button></div>`).join('');

CL.copyrightPanel = (d) => {
  const rows = d.comparison.copyrights;
  return `<div class="panel"><h2>Copyright notices</h2>
    <p class="small muted">Missing or different data is not automatically wrong. Decide per notice; original observations are preserved.</p>
    ${rows.length ? `${CL.tableBulkBar('copyright', false)}<table class="diff-table"><thead><tr>${CL.pickHead('copyright')}<th>PL notice</th><th>SL notice</th><th>class</th><th>decision</th><th>actions</th></tr></thead><tbody>
      ${rows.map(r => {
        const df = CL.diffHtml(r.pl_text, r.sl_text);
        return `<tr class="${r.rejected ? 'selected' : ''}" data-open="${r.kind !== 'matched' && !r.decision ? 1 : 0}">
        ${CL.rowPick('copyright', r.key, r.sl_text || r.pl_text || 'notice')}
        <td>${r.pl_text ? `<div class="notice pl_only">${df.left}</div>` : '<span class="muted">—</span>'}</td>
        <td>${r.sl_text ? `<div class="notice sl_only">${df.right}</div>` : '<span class="muted">—</span>'}</td>
        <td>${CL.badge(CL.words(r.kind), r.kind === 'matched' ? 'ok' : (r.kind === 'duplicate' ? 'warn' : ''))}</td>
        <td class="dec small">${CL.decisionCell(r.decision)}</td>
        <td class="acts"><div class="row-actions">${CL.decisionButtons('copyright', r.key, !!r.sl_text, !!r.pl_text)}</div>
          ${CL.suggestionChips(CL.openSuggestions(d, 'copyright', r.key))}</td></tr>`;
      }).join('')}
    </tbody></table>` : '<p class="muted">No copyright notices on either side.</p>'}
  </div>`;
};

CL.licensePanel = (d) => {
  const rows = d.comparison.licenses;
  return `<div class="panel"><h2>License names</h2>
    <p class="small muted">Names are compared exactly after normalization. Similar names are not treated as equivalent; SPDX differences are shown.</p>
    ${rows.length ? `${CL.tableBulkBar('license_name', true)}<table class="diff-table"><thead><tr>${CL.pickHead('license_name')}<th>PL license</th><th>SL license</th><th>SPDX (PL / SL)</th><th>class</th><th>decision</th><th>actions</th></tr></thead><tbody>
      ${rows.map(r => {
        const df = CL.diffHtml(r.pl_name, r.sl_name);
        return `<tr class="${r.rejected ? 'selected' : ''}" data-open="${r.kind !== 'matched' && !r.decision && !r.auto_false_positive ? 1 : 0}">
        ${CL.rowPick('license_name', r.key, r.sl_name || r.pl_name || 'license')}
        <td>${r.pl_name ? df.left : '<span class="muted">—</span>'}</td>
        <td>${r.sl_name ? df.right : '<span class="muted">—</span>'}</td>
        <td class="small">${CL.esc(r.pl_spdx || '—')} / ${CL.esc(r.sl_spdx || '—')}</td>
        <td>${CL.badge(CL.words(r.kind), r.kind === 'matched' ? 'ok' : '')}${r.auto_false_positive ? ' ' + CL.badge('auto false positive', 'ok') : ''}</td>
        <td class="dec small">${CL.decisionCell(r.decision)}</td>
        <td class="acts"><div class="row-actions">${CL.decisionButtons('license_name', r.key, !!r.sl_name, !!r.pl_name)}</div>
          ${CL.suggestionChips(CL.openSuggestions(d, 'license_name', r.key))}</td></tr>`;
      }).join('')}
    </tbody></table>` : '<p class="muted">No licenses on either side.</p>'}
  </div>`;
};

// One compact row per PL license: the summary says where it stands, the body (diff, actions,
// candidates) opens on demand. A single-license component shows its one row open.
CL.textRow = (d, t, open) => {
  const sfx = t.suffix || '';
  const key = t.discrepancy_key || ('no_source_text' + sfx);
  const name = t.license || 'License text';
  const rec = t.recommendation;
  const textDiff = CL.diffHtml(t.pl_current ? t.pl_current.text : '', t.preferred ? t.preferred.text : '');
  const cand = (list, assigned) => list.map(c => `<tr class="${c.preferred ? 'selected' : ''}">
      <td>${CL.badge(c.side.toUpperCase(), c.side === 'sl' ? 'accent' : '')}</td>
      <td>${CL.badge(CL.words(c.kind), c.kind === 'source_file' ? 'ok' : 'warn')}</td>
      <td class="small wrap">${CL.esc(c.file_path || c.file_name || '—')}</td>
      <td class="small">${CL.esc(c.provenance)}</td>
      <td class="num"><code>${CL.esc((c.content_hash || '').slice(0, 12))}</code></td>
      <td>${c.side === 'sl' && c.kind === 'source_file' && !c.preferred
        ? `<button class="tiny" data-dec="license_text" data-key="${CL.esc(c.content_hash + sfx)}" data-action="approve_sl">${assigned ? 'Use this text' : `Use for ${CL.esc(name)}`}</button>`
        : (c.preferred ? CL.badge('preferred', 'ok') : '<span class="muted">—</span>')}</td></tr>`).join('');
  const rows = cand(t.candidates || [], true) + cand(t.unassigned || [], false);
  return `<details class="lt-row"${open ? ' open' : ''}>
    <summary><b>${CL.esc(name)}</b>
      <span class="small muted">PL: ${CL.esc(CL.words(t.pl_current ? t.pl_current.kind : 'none'))}</span>
      ${CL.badge(CL.words(rec), rec === 'already_matches' ? 'ok' : (rec === 'no_source_text' ? 'bad' : 'warn'))}
      ${t.decision ? `<span class="small">decision: ${CL.decisionCell(t.decision).replace('<br>', ' ')}</span>` : ''}</summary>
    <p class="small muted">${t.reasons.map(r => CL.esc(r)).join(' · ')}</p>
    <div class="split mt-2">
      <div><div class="side-label">PL current text · ${CL.esc(CL.words(t.pl_current ? t.pl_current.kind : 'none'))}</div>
        <div class="textbox"><pre>${t.pl_current ? textDiff.left : '—'}</pre></div></div>
      <div><div class="side-label">Preferred source-file text · ${CL.esc(t.preferred ? (t.preferred.file_path || t.preferred.file_name) : 'none')}</div>
        <div class="textbox"><pre>${t.preferred ? textDiff.right : '—'}</pre></div></div>
    </div>
    <div class="row-actions mt-2">
      ${t.preferred ? `<button class="tiny primary-soft" data-dec="license_text" data-key="${CL.esc(t.preferred.content_hash + sfx)}" data-action="approve_sl">Approve preferred text</button>` : ''}
      ${t.pl_current ? `<button class="tiny" data-dec="license_text" data-key="${CL.esc(key)}" data-action="accept_pl">Keep PL text</button>` : ''}
      <button class="tiny ghost" data-write-text="${CL.esc(sfx)}" data-license="${CL.esc(name)}" title="Paste the text, for example from the file in FOSSology, and say where it comes from">Write text…</button>
      <span class="act-divider" aria-hidden="true"></span>
      <button class="tiny danger-ghost" data-dec="license_text" data-key="${CL.esc(key)}" data-action="reject">Mark irrelevant</button>
    </div>
    ${CL.suggestionChips(CL.openSuggestions(d, 'license_text', t.preferred ? t.preferred.content_hash + sfx : key))}
    ${rows ? `<details class="mt-2"><summary>Text candidates and their provenance (${(t.candidates || []).length + (t.unassigned || []).length})</summary>
      <table><thead><tr><th>side</th><th>kind</th><th>file</th><th>provenance</th><th>hash</th><th>action</th></tr></thead><tbody>${rows}</tbody></table>
    </details>` : ''}
  </details>`;
};

CL.textPanel = (d) => {
  const rows = d.comparison.license_texts || [];
  const loaded = d.comparison.loaded_files || [];
  const last = loaded.length ? loaded[loaded.length - 1] : null;
  const tools = `<div class="row-actions">
      ${d.match.sl_key ? '<button class="tiny" id="w-load-files" title="Read this component\'s license files from the SL tool now (one upload, a few seconds)">Load license files from the SL tool</button>' : ''}
      ${CL.slLink(d) ? `<span class="small">${CL.slLink(d)}</span>` : ''}
      ${last ? `<span class="small muted">${loaded.length} license file${loaded.length === 1 ? '' : 's'} loaded by ${CL.esc(last.loaded_by)}, ${CL.when(last.loaded_at)}</span>` : ''}
    </div>`;
  if (!rows.length) return `<div class="panel"><h2>License text</h2>${tools}<p class="muted mt-2">No license text available on either side.</p></div>`;
  const multi = rows.length > 1;
  return `<div class="panel"><h2>License text${multi ? ` · ${rows.length} licenses` : ''}</h2>
    ${tools}
    <div class="lt-rows mt-2">${rows.map(t => CL.textRow(d, t, !multi)).join('')}</div>
    <p class="small muted mt-2">Synchronized text is an identified source-file license or header (LICENSE, LICENCE, COPYING, NOTICE, or a file header with license evidence), or text a reviewer wrote with a stated source. Generic metadata text is never sent.</p>
  </div>`;
};

// The end of the work zone: what this component would write, then approval -> dry run -> apply,
// as numbered steps. Approving is the view's one primary action.
CL.syncPanel = (d) => {
  const m = d.match;
  const approved = m.review_status === CL.reviewState.APPROVED;
  // Both sides agree: nothing to write except the review itself, which the sync records in the PL tool.
  const agrees = m.discrepancy_count === 0 && m.pl_key && m.sl_key && ['auto', 'confirmed'].includes(m.status);
  const ready = m.review_status === CL.reviewState.REVIEWED || agrees;
  const prog = CL.decisionProgress(d);
  const step1 = approved
    ? `${CL.badge('approved for sync', 'ok')}<button id="w-unapprove" class="ghost">Withdraw sync approval</button>`
    : `<button id="w-approve" class="primary" ${ready ? '' : `disabled title="Decide every discrepant field first"`} aria-describedby="w-approve-hint">${agrees && m.review_status !== CL.reviewState.REVIEWED ? 'Approve as reviewed' : 'Approve component for synchronization'}</button>`;
  return `<div class="panel"><h2>Synchronization for this component</h2>
  ${d.sync_preview.length ? `<table><thead><tr><th>field</th><th>status</th><th>PL before</th><th>approved value</th><th>note</th></tr></thead><tbody>
    ${d.sync_preview.map(i => `<tr><td>${CL.esc(CL.words(i.field))}</td><td>${CL.status(i.status)}</td>
      <td class="small"><code>${CL.esc(JSON.stringify(i.pl_before))}</code></td><td class="small">${CL.esc(String(i.approved_value || '').slice(0, 160))}</td><td class="small">${CL.esc(i.message)}</td></tr>`).join('')}
    </tbody></table>` : '<p class="muted">No approved SL-derived changes for this component.</p>'}
  <ol class="gate" aria-label="Component synchronization steps">
    <li class="${approved ? 'is-done' : ''}"><div class="row-actions">${step1}</div>
      <p class="small muted step-hint" id="w-approve-hint">${approved ? 'Approved: it is included in the next synchronization.'
        : (m.review_status === CL.reviewState.REVIEWED ? 'Every field is decided. Approving includes this component in the next synchronization.'
          : agrees ? 'Both sides agree, so nothing is corrected: the synchronization only marks this component reviewed in the PL tool.'
          : `Decide every discrepant field first (${prog.decided}/${prog.total} decided).`)}</p></li>
    <li><div class="row-actions"><button id="w-preview">Preview changes</button>
        <button id="w-dry" ${approved ? '' : 'disabled title="Approve the component first"'}>Dry run</button></div>
      <p class="small muted step-hint">A preview and a dry run write nothing.</p></li>
    <li><div class="row-actions"><button id="w-apply" class="danger" ${approved ? '' : 'disabled title="Approve the component first"'}>Apply…</button></div>
      <p class="small muted step-hint">Writes approved licenses, license text and copyrights, marks Black Duck reviewed, and closes the FOSSology upload with the configured comment.</p></li>
  </ol>
  <div id="w-sync-result" class="mt-2"></div>
</div>`;
};

// Reference: the match evidence and both identities, PL then SL with the same fields.
CL.evidencePanel = (d, pl, sl) => {
  const m = d.match;
  const sideKv = (label, c) => `<div><h3>${label}</h3><dl class="kv">
      <dt>component</dt><dd>${CL.esc(c ? `${c.name} ${c.version || ''}` : '—')}</dd>
      <dt>tool</dt><dd>${CL.esc(c ? c.tool : '—')}</dd>
      <dt>purl</dt><dd class="small"><code>${CL.esc(CL.dash(c && c.purl))}</code></dd>
      <dt>snapshot</dt><dd class="small num">${CL.esc(CL.dash(c && (c.source_version || '').slice(0, 16)))}</dd>
    </dl></div>`;
  return `<details class="panel"><summary>Match evidence and identity</summary>
    <div class="split">${sideKv('PL', pl)}${sideKv('SL', sl)}</div>
    <dl class="kv mt-3">
      <dt>rationale</dt><dd class="small">${m.rationale.map(r => CL.esc(r)).join('<br>') || '—'}</dd>
      <dt>mismatch</dt><dd class="small">${m.mismatch_reasons.map(r => CL.esc(r)).join('<br>') || '—'}</dd>
    </dl>
  </details>`;
};

CL.notesPanel = (d) => `<div class="panel"><h2>Notes &amp; decision history</h2>
  ${d.notes.length ? d.notes.map(n => `<div class="notice"><b>${CL.esc(n.reviewer)}</b> <span class="small muted">${CL.when(n.created_at)}</span><br>${CL.esc(n.body)}</div>`).join('') : ''}
  <label class="side-label mt-3" for="note-body">Review note</label>
  <textarea id="note-body" placeholder="What changed and why — kept with the component"></textarea>
  <div class="row-actions"><button id="add-note">Add note</button></div>
  ${d.decisions.length ? `<details class="mt-3"><summary>Decision history (${d.decisions.length})</summary><table><thead><tr><th>field</th><th>entry</th><th>action</th><th>value</th><th>reviewer</th><th>decided</th></tr></thead><tbody>
    ${d.decisions.map(x => `<tr><td>${CL.esc(CL.words(x.field))}</td><td class="small"><code>${CL.esc(x.entry_key.slice(0, 10))}</code></td><td>${CL.badge(CL.words(x.action))}</td><td class="small">${CL.esc(String(x.approved_value || '').slice(0, 80))}</td><td>${CL.esc(x.reviewer)}</td><td class="small">${CL.when(x.created_at)}</td></tr>`).join('')}
  </tbody></table></details>` : ''}
</div>`;

// Open suggestions render inline in their row; this panel holds only the rest (decided ones,
// or ones whose entry is no longer in the comparison), and is absent when there are none.
CL.suggestionPanel = (d) => {
  const rowKeys = new Set([
    ...d.comparison.copyrights.map(r => `copyright|${r.key}`),
    ...d.comparison.licenses.map(r => `license_name|${r.key}`),
  ]);
  (d.comparison.license_texts || []).forEach(t =>
    rowKeys.add(`license_text|${t.preferred ? t.preferred.content_hash + (t.suffix || '') : t.discrepancy_key}`));
  const rest = (d.suggestions || []).filter(x => x.status !== 'open' || !rowKeys.has(`${x.field}|${x.entry_key}`));
  if (!rest.length) return '';
  return `<details class="panel"><summary>Other machine suggestions (${rest.length})</summary>
  <p class="small muted">Suggestions are stored separately from human decisions. Accepting one creates a normal review decision; nothing is synchronized without component approval.</p>
  <table><thead><tr><th>field</th><th>entry</th><th>value</th><th>confidence</th><th>model</th><th>status</th><th>actions</th></tr></thead><tbody>
    ${rest.map(x => `<tr><td>${CL.esc(CL.words(x.field))}</td><td class="small"><code>${CL.esc(x.entry_key.slice(0, 10))}</code></td><td class="small">${CL.esc(String(x.value).slice(0, 120))}</td>
      <td class="num">${x.confidence.toFixed(2)}</td><td class="small">${CL.esc(x.model)}</td><td>${CL.badge(x.status, x.status === 'accepted' ? 'ok' : (x.status === 'rejected' ? 'bad' : 'warn'))}</td>
      <td class="acts row-actions">${x.status === 'open' ? `<button class="tiny ghost" data-suggestion="${x.id}" data-sdec="accept">Accept</button><button class="tiny danger-ghost" data-suggestion="${x.id}" data-sdec="reject">Reject</button>` : '<span class="muted">—</span>'}</td></tr>`).join('')}
  </tbody></table></details>`;
};

// Mix and match: choose any PL and any SL component of the mapping, whatever the matcher proposed.
// Resolves {pl_key, sl_key}, or null when cancelled.
CL.matchEditor = ({ mappingId, title, message, plKey = '', slKey = '', confirmText = 'Save match' }) => new Promise((resolve) => {
  const prev = document.activeElement;
  const sel = { pl: plKey, sl: slKey };
  const known = { pl: {}, sl: {} };
  const back = document.createElement('div');
  back.className = 'modal-backdrop';
  back.innerHTML = `<div class="modal wide" role="dialog" aria-modal="true" aria-labelledby="modal-title">
      <h2 id="modal-title">${CL.esc(title)}</h2>
      ${message ? `<p>${CL.esc(message)}</p>` : ''}
      <div class="pick-grid">${['pl', 'sl'].map(s => `<div>
        <label for="pick-${s}-q">${s.toUpperCase()} component</label>
        <input id="pick-${s}-q" type="search" placeholder="filter by name, version or key" autocomplete="off">
        <select id="pick-${s}" size="9" aria-label="${s.toUpperCase()} component"></select></div>`).join('')}
      </div>
      <p class="small" id="pick-hint" aria-live="polite"></p>
      <div class="modal-actions">
        <button type="button" data-x="cancel">Cancel</button>
        <button type="button" class="primary" data-x="ok">${CL.esc(confirmText)}</button>
      </div></div>`;
  document.body.appendChild(back);
  const label = (i) => `${i.name} ${i.version || ''}`.trim() + (i.partner ? ` — paired with ${i.partner}` : '')
    + (i.not_read ? ' — outside the SL scope, read on the next refresh once linked' : '');
  const hint = () => {
    const taken = ['pl', 'sl'].map(s => known[s][sel[s]]).filter(i => i && i.partner);
    back.querySelector('#pick-hint').textContent = taken.length
      ? 'Selecting a component that is already paired unpairs it from its current partner.' : '';
  };
  const draw = (s, items) => {
    const list = items.slice();
    if (sel[s] && known[s][sel[s]] && !list.some(i => i.key === sel[s])) list.unshift(known[s][sel[s]]);
    back.querySelector(`#pick-${s}`).innerHTML = list.map(i =>
      `<option value="${CL.esc(i.key)}"${i.key === sel[s] ? ' selected' : ''}>${CL.esc(label(i))}</option>`).join('');
  };
  const load = async (s) => {
    try {
      const q = back.querySelector(`#pick-${s}-q`).value.trim();
      const pool = await CL.api('GET', `/api/mappings/${mappingId}/pool?limit=500&q=${encodeURIComponent(q)}`);
      pool[s].items.forEach(i => { known[s][i.key] = i; });
      draw(s, pool[s].items);
      hint();
    } catch (e) { CL.toast(e.message, true); }
  };
  const done = (val) => {
    document.removeEventListener('keydown', onKey, true);
    back.remove();
    if (prev && prev.focus) prev.focus();
    resolve(val);
  };
  const ok = () => {
    if (!sel.pl || !sel.sl) { CL.toast('pick both a PL and an SL component', true); return; }
    done({ pl_key: sel.pl, sl_key: sel.sl });
  };
  const onKey = (e) => {
    if (e.key === 'Escape') { e.preventDefault(); done(null); }
    else if (e.key === 'Tab') {
      const f = [...back.querySelectorAll('button, input, select')];
      const first = f[0], last = f[f.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    }
  };
  document.addEventListener('keydown', onKey, true);
  ['pl', 'sl'].forEach((s) => {
    let timer = null;
    back.querySelector(`#pick-${s}`).onchange = (e) => { sel[s] = e.target.value; hint(); };
    back.querySelector(`#pick-${s}-q`).oninput = () => { clearTimeout(timer); timer = setTimeout(() => load(s), 250); };
    load(s);
  });
  back.querySelector('[data-x=cancel]').onclick = () => done(null);
  back.querySelector('[data-x=ok]').onclick = ok;
  back.onclick = (e) => { if (e.target === back) done(null); };
  back.querySelector('#pick-pl-q').focus();
});

// Candidates fix the match, and the fields depend on the match, so they sit above the fields.
CL.candidatePanel = (d) => `<div class="panel"><h2>Manual match candidates</h2>
  <p class="small muted">This component has no confirmed partner. Assign one before deciding fields.</p>
  <table><thead><tr><th>SL component</th><th>version</th><th>similarity</th><th>action</th></tr></thead><tbody>
    ${d.candidates.map(c => `<tr><td>${CL.esc(c.sl_name)}</td><td class="num">${CL.esc(CL.dash(c.sl_version))}</td><td class="num">${c.similarity}</td><td><button class="tiny primary-soft" data-assign="${CL.esc(c.sl_key)}">Assign</button></td></tr>`).join('')}
  </tbody></table>
</div>`;

CL.falsePositiveDialog = async () => {
  const scope = await CL.dialog({
    title: 'False-positive scope',
    message: 'Remember this ignored finding so it does not ask for a decision again.',
    field: 'Scope (component, version, or global)',
    value: 'component',
    confirmText: 'Continue',
  });
  if (scope === null) return null;
  if (!['component', 'version', 'global'].includes(scope)) {
    CL.toast('scope must be component, version, or global', true);
    return null;
  }
  const pattern = await CL.dialog({
    title: 'Evidence path pattern',
    message: 'Use * to match every evidence path; otherwise a fnmatch pattern such as src/LICENSE.',
    field: 'Path pattern',
    value: '*',
    confirmText: 'Record false positive',
  });
  if (pattern === null) return null;
  return { scope, path_pattern: pattern || '*' };
};

CL.applyComponent = async (id) => {
  const mid = id || CL.state.workspaceId;
  if (!mid) { CL.toast('no component selected', true); return false; }
  if (!await CL.confirm('Apply the approved changes to the PL tool and close the matching FOSSology upload?',
                        { title: 'Apply component', confirmText: 'Apply' })) return false;
  const r = await CL.api('POST', `/api/components/${mid}/apply`, { confirm: true });
  CL.toast(r.reviewed
    ? 'component applied: PL reviewed and FOSSology upload closed'
    : 'component apply incomplete; inspect the job report', !r.reviewed);
  await CL.renderWorkspace(mid);
  return true;
};

CL.bindTableBulk = (id, rerender) => {
  document.querySelectorAll('#view-workspace .bulk-bar[data-bulk-field]').forEach(bar => {
    const field = bar.dataset.bulkField;
    const boxes = [...document.querySelectorAll(`#view-workspace .row-pick[data-pick-field="${field}"]`)];
    const head = document.querySelector(`#view-workspace .row-pick-all[data-pick-field="${field}"]`);
    const picked = () => boxes.filter(b => b.checked);
    const paint = () => {
      const n = picked().length;
      bar.hidden = !n;
      bar.querySelector('[data-bulk-count]').textContent = n;
      boxes.forEach(b => b.closest('tr').classList.toggle('picked', b.checked));
    };
    boxes.forEach(b => { b.onchange = paint; });
    if (head) head.onchange = () => {
      // "all" means every row that still needs a decision, not rows already settled
      boxes.forEach(b => { b.checked = head.checked && b.closest('tr').dataset.open === '1'; });
      paint();
    };
    bar.querySelectorAll('[data-bulk-dec]').forEach(btn => btn.onclick = () => CL.click(async () => {
      const keys = picked().map(b => b.dataset.pickKey);
      const action = btn.dataset.bulkDec;
      if (['reject', 'mark_false_positive'].includes(action) && !await CL.confirm(
        `${btn.textContent.trim()} for ${keys.length} row${keys.length === 1 ? '' : 's'}?`,
        { title: btn.textContent.trim(), confirmText: `${btn.textContent.trim()} ${keys.length}` })) return;
      const r = await CL.api('POST', `/api/components/${id}/decisions/bulk`, { field, entry_keys: keys, action });
      CL.toast(`${r.done.length} decision${r.done.length === 1 ? '' : 's'} recorded${r.failed.length ? `; ${r.failed.length} refused: ${r.failed[0].error}` : ''}`, r.failed.length > 0);
      await rerender();
    }));
  });
};

CL.bindWorkspace = (d) => {
  const id = d.match.id;
  const rerender = async () => { await CL.renderWorkspace(id); };
  const toggles = document.querySelectorAll('#view-workspace .diff-toggle');
  toggles.forEach(toggle => {
    toggle.onclick = () => {
      CL.diffOn = !CL.diffOn;
      // Paint is a class on the view root: the marks stay in the DOM, only their colour changes.
      document.getElementById('view-workspace').classList.toggle('diff-off', !CL.diffOn);
      toggles.forEach(t => {
        t.setAttribute('aria-checked', String(CL.diffOn));
        t.textContent = CL.diffOn ? 'Highlight diff' : 'Plain text';
      });
    };
  });
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
    if (action === 'mark_false_positive') {
      const rule = await CL.falsePositiveDialog();
      if (!rule) return;
      body.scope = rule.scope;
      body.path_pattern = rule.path_pattern;
    }
    await CL.api('POST', `/api/components/${id}/decisions`, body);
    CL.toast('decision recorded');
    await rerender();
  });
  document.querySelectorAll('[data-dec]').forEach(b => b.onclick = () => decide(b.dataset.dec, b.dataset.key, b.dataset.action));
  CL.bindTableBulk(id, rerender);
  const load = document.getElementById('w-load-files');
  if (load) load.onclick = () => CL.click(async () => {
    const r = await CL.api('POST', `/api/components/${id}/sl-license-files`);
    CL.toast(r.files ? `${r.files} license file${r.files === 1 ? '' : 's'} loaded from the SL tool` : 'the SL tool reports no license files for this component', !r.files);
    await rerender();
  });
  document.querySelectorAll('[data-write-text]').forEach(b => b.onclick = () => CL.click(async () => {
    const text = await CL.dialog({ title: `Write the license text for ${b.dataset.license}`,
      message: 'This text is what a synchronization writes to the PL license. Paste it exactly, for example from the license file in FOSSology.',
      field: 'License text', multiline: true, confirmText: 'Continue' });
    if (!text) return;
    const source = await CL.dialog({ title: 'Where does this text come from?',
      message: 'Recorded with the decision, because this text is not an identified file the tool read.',
      field: 'Source (file path, URL or reason)', confirmText: 'Record decision' });
    if (!source) return;
    await CL.api('POST', `/api/components/${id}/decisions`, { field: 'license_text', entry_key: `reviewer_supplied${b.dataset.writeText}`,
      action: 'correct', approved_value: text, note: `reviewer-supplied text; source: ${source}` });
    CL.toast('license text decision recorded');
    await rerender();
  }));
  const confirmBtn = document.getElementById('w-confirm');
  if (confirmBtn) confirmBtn.onclick = () => CL.click(async () => { await CL.api('POST', `/api/components/${id}/confirm`); await rerender(); });
  document.getElementById('w-reassign').onclick = () => CL.click(async () => {
    const pick = await CL.matchEditor({
      mappingId: d.match.mapping_id, title: 'Edit match',
      message: 'Choose which PL and SL component belong together. This replaces the automatic proposal.',
      plKey: d.match.pl_key, slKey: d.match.sl_key, confirmText: 'Save match',
    });
    if (!pick) return;
    await CL.api('POST', `/api/components/${id}/reassign`, pick);
    CL.toast('match saved');
    await rerender();
  });
  const unlink = document.getElementById('w-reject');
  if (unlink) unlink.onclick = () => CL.click(async () => {
    if (!await CL.confirm('Unlink this pair? Both components return to the unmatched list and the pair is not proposed again after refresh.', { title: 'Unlink match', confirmText: 'Unlink' })) return;
    await CL.api('POST', `/api/components/${id}/reject`, { reason: 'reviewer unlinked' });
    await rerender();
  });
  const approve = document.getElementById('w-approve');
  if (approve) approve.onclick = () => CL.click(async () => { await CL.api('POST', `/api/components/${id}/review`, { state: CL.reviewState.APPROVED }); CL.toast('approved for synchronization'); await rerender(); });
  const unapprove = document.getElementById('w-unapprove');
  if (unapprove) unapprove.onclick = () => CL.click(async () => { await CL.api('POST', `/api/components/${id}/review`, { state: CL.reviewState.REVIEWED }); await rerender(); });
  document.getElementById('add-note').onclick = () => CL.click(async () => {
    const body = document.getElementById('note-body').value.trim();
    if (!body) return;
    await CL.api('POST', `/api/components/${id}/notes`, { body });
    await rerender();
  });
  document.querySelectorAll('[data-suggestion]').forEach(b => b.onclick = () => CL.click(async () => {
    await CL.api('POST', `/api/suggestions/${b.dataset.suggestion}/decision`, { action: b.dataset.sdec });
    CL.toast('suggestion ' + b.dataset.sdec + 'ed');
    await rerender();
  }));
  document.querySelectorAll('[data-assign]').forEach(b => b.onclick = () => CL.click(async () => {
    await CL.api('POST', `/api/components/${id}/reassign`, { sl_key: b.dataset.assign });
    await rerender();
  }));
  const box = document.getElementById('w-sync-result');
  const showItems = (items) => {
    if (!box) return;
    box.innerHTML = `<table><thead><tr><th>field</th><th>status</th><th>before</th><th>approved</th><th>message</th></tr></thead><tbody>${items.map(i => `<tr><td>${CL.esc(i.field)}</td><td>${CL.badge(i.status, CL.statusClass(i.status))}</td><td class="small">${CL.esc(JSON.stringify(i.pl_before))}</td><td class="small">${CL.esc(String(i.approved_value || '').slice(0, 160))}</td><td class="small">${CL.esc(i.message)}</td></tr>`).join('')}</tbody></table>`;
  };
  document.getElementById('w-preview').onclick = () => CL.click(async () => {
    const r = await CL.api('POST', '/api/sync/preview', { mapping_id: d.match.mapping_id, component_ids: [id], finalize: true });
    const comps = r.components || [];
    const items = comps.length ? comps[0].items : [];
    if (box) box.innerHTML = items.length ? `<p class="small muted">Preview for this component</p>` : `<p class="muted">No approved changes.</p>`;
    showItems(items);
  });
  document.getElementById('w-dry').onclick = () => CL.click(async () => {
    const job = await CL.api('POST', '/api/sync/jobs', { mapping_id: d.match.mapping_id, dry_run: true, component_ids: [id], finalize: true });
    if (box) box.innerHTML = `<p class="small muted">Dry-run job #${job.id} · ${CL.badge(job.status, CL.statusClass(job.status))}</p>`;
    showItems(job.items || []);
  });
  document.getElementById('w-apply').onclick = () => CL.click(() => CL.applyComponent(id));
};
