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
  const total = cr.length + lic.length + (c.license_text ? 1 : 0);
  let decided = cr.filter(r => r.decision).length + lic.filter(r => r.decision).length;
  if (c.license_text && (d.decisions || []).some(x => x.field === 'license_text')) decided += 1;
  return { total, decided: Math.max(0, Math.min(total, decided)) };
};

// Diff highlighting is on by default and switchable per component. It lives on CL (not
// CL.state, which app.js creates after this file loads) so a re-render after a decision
// keeps the reviewer's choice instead of snapping back.
CL.diffOn = true;

// One control for the whole workspace, rendered in the sticky bar. It toggles a class on the
// workspace root rather than re-rendering: the marks stay in the DOM, only their paint changes.
CL.diffToggle = () => `<button id="diff-toggle" class="tiny" role="switch" aria-checked="${CL.diffOn}"
  title="Highlight the words that differ between the two sides">${CL.diffOn ? 'Highlight diff' : 'Plain text'}</button>`;

// A re-render replaces the panel, so remember which control the reviewer was on and hand focus back to its twin.
CL.focusKey = (el) => !el || el === document.body || el === document.documentElement ? null
  : el.id ? `#${el.id}`
  : el.dataset.dec ? `[data-dec="${el.dataset.dec}"][data-key="${CSS.escape(el.dataset.key)}"][data-action="${el.dataset.action}"]`
  : el.dataset.suggestion ? `[data-suggestion="${el.dataset.suggestion}"][data-sdec="${el.dataset.sdec}"]`
  : el.dataset.assign ? `[data-assign="${CSS.escape(el.dataset.assign)}"]` : null;

CL.renderWorkspace = async (id, fresh) => {
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
  const focusKey = CL.focusKey(document.activeElement);
  const m = d.match;
  const pl = d.pl && d.pl.component, sl = d.sl && d.sl.component;
  const cls = CL.statusClass(m.status);
  const side = (name, ver, missing) => name
    ? `${CL.esc(name)} <span class="muted num">${CL.esc(ver || '')}</span>`
    : `<span class="badge warn">${missing}</span>`;
  el.innerHTML = `
    <div class="panel">
      <h1 tabindex="-1">${side(m.pl_name, m.pl_version, 'no PL component')}
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
          <dt>PL purl</dt><dd class="small">${CL.esc(CL.dash(pl && pl.purl))}</dd>
          <dt>SL purl</dt><dd class="small">${CL.esc(CL.dash(sl && sl.purl))}</dd>
          <dt>PL snapshot</dt><dd class="small num">${CL.esc(CL.dash(pl && (pl.source_version || '').slice(0, 16)))}</dd>
          <dt>SL snapshot</dt><dd class="small num">${CL.esc(CL.dash(sl && (sl.source_version || '').slice(0, 16)))}</dd>
        </div>
      </div>
      <div class="row-actions mt-2">
        ${m.status === 'suggested' || m.status === 'unmatched' ? '<button id="w-confirm" class="primary">Confirm match</button>' : ''}
        <button id="w-reassign">Reassign…</button>
        <button id="w-reject" class="danger">Reject match</button>
        ${m.review_status === CL.reviewState.REVIEWED ? '<button id="w-approve" class="primary">Approve component for synchronization</button>' : ''}
        ${m.review_status === CL.reviewState.APPROVED ? '<button id="w-unapprove">Withdraw sync approval</button>' : ''}
      </div>
    </div>
    ${d.warnings.length ? `<div class="panel"><h2>⚠ Warnings</h2><ul class="warnlist">${d.warnings.map(w => `<li>${CL.esc(w)}</li>`).join('')}</ul></div>` : ''}
    ${d.stale.length ? `<div class="panel"><h2>Stale approvals</h2><table><thead><tr><th>field</th><th>entry</th><th>reason</th></tr></thead><tbody>${d.stale.map(s => `<tr><td>${CL.esc(s.field)}</td><td class="small">${CL.esc(s.entry_key.slice(0, 12))}</td><td>${CL.esc(s.reason)}</td></tr>`).join('')}</tbody></table>
      <p class="small muted">Re-review these fields before synchronizing; the source data changed after the decision.</p></div>` : ''}
    ${CL.copyrightPanel(d)}
    ${CL.licensePanel(d)}
    ${CL.textPanel(d)}
    ${CL.suggestionPanel(d)}
    ${CL.syncPanel(d)}
    ${CL.notesPanel(d)}
    ${d.candidates.length ? CL.candidatePanel(d) : ''}
    ${CL.wsStickyBar(d)}`;
  CL.bindWorkspace(d);
  CL.bindStickyBar(el);
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
    <span class="ws-badges">${CL.badge(m.status, CL.statusClass(m.status))}
      <span class="ws-conf small muted">${m.method} · ${m.confidence.toFixed(2)}</span></span>
    <span class="ws-badges">${CL.badge(m.review_status, CL.statusClass(m.review_status))}</span>
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

CL.copyrightPanel = (d) => {
  const rows = d.comparison.copyrights;
  return `<div class="panel"><h2>Copyright notices (PL vs SL)</h2>
    <p class="small muted">Missing or different data is not automatically wrong. Decide per notice; original observations are preserved.</p>
    ${rows.length ? `<table class="diff-table"><thead><tr><th>class</th><th>PL notice</th><th>SL notice</th><th>decision</th><th>actions</th></tr></thead><tbody>
      ${rows.map(r => {
        const df = CL.diffHtml(r.pl_text, r.sl_text);
        return `<tr class="${r.rejected ? 'selected' : ''}">
        <td>${CL.badge(r.kind, r.kind === 'matched' ? 'ok' : (r.kind === 'duplicate' ? 'warn' : 'accent'))}</td>
        <td>${r.pl_text ? `<div class="notice pl_only">${df.left}</div>` : '<span class="muted">—</span>'}</td>
        <td>${r.sl_text ? `<div class="notice sl_only">${df.right}</div>` : '<span class="muted">—</span>'}</td>
        <td class="dec small">${r.decision ? CL.badge(r.decision.action, CL.statusClass(r.decision.action === 'approve_sl' ? 'reviewed' : '')) + '<br><span class="muted">' + CL.esc(r.decision.reviewer || '') + '</span>' : '—'}</td>
        <td class="acts row-actions">${CL.decisionButtons('copyright', r.key, !!r.sl_text, !!r.pl_text)}
        </td></tr>`;
      }).join('')}
    </tbody></table>` : '<p class="muted">No copyright notices on either side.</p>'}
  </div>`;
};

CL.licensePanel = (d) => {
  const rows = d.comparison.licenses;
  return `<div class="panel"><h2>License names</h2>
    <p class="small muted">Names are compared exactly after normalization. Similar names are not treated as equivalent; SPDX differences are shown.</p>
    ${rows.length ? `<table class="diff-table"><thead><tr><th>class</th><th>PL license</th><th>SL license</th><th>SPDX</th><th>evidence</th><th>decision</th><th>actions</th></tr></thead><tbody>
      ${rows.map(r => {
        const df = CL.diffHtml(r.pl_name, r.sl_name);
        return `<tr class="${r.rejected ? 'selected' : ''}">
        <td>${CL.badge(r.kind, r.kind === 'matched' ? 'ok' : 'accent')}</td>
        <td>${r.pl_name ? df.left : '<span class="muted">—</span>'}</td>
        <td>${r.sl_name ? df.right : '<span class="muted">—</span>'}</td>
        <td class="small">${CL.esc(r.pl_spdx || '—')} / ${CL.esc(r.sl_spdx || '—')}</td>
        <td class="small">${CL.esc((r.evidence_paths || []).join(', ') || '—')}</td>
        <td class="dec small">${r.decision ? CL.badge(r.decision.action) : '—'}</td>
        <td class="acts row-actions">${CL.decisionButtons('license_name', r.key, !!r.sl_name, !!r.pl_name)}
        </td></tr>`;
      }).join('')}
    </tbody></table>` : '<p class="muted">No licenses on either side.</p>'}
  </div>`;
};

CL.textPanel = (d) => {
  const t = d.comparison.license_text;
  if (!t) return `<div class="panel"><h2>License text evidence</h2><p class="muted">No license text available on either side.</p></div>`;
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
  const textDiff = CL.diffHtml(t.pl_current ? t.pl_current.text : '', t.preferred ? t.preferred.text : '');
  return `<div class="panel"><h2>License text: evidence &amp; provenance</h2>
    <p>${CL.badge(rec, rec === 'already_matches' ? 'ok' : (rec === 'no_source_text' ? 'bad' : 'warn'))}
      <span class="small muted">${t.reasons.map(r => CL.esc(r)).join(' · ')}</span></p>
    <div class="split">
      <div><div class="side-label">PL current text (${CL.esc(t.pl_current ? t.pl_current.kind : 'none')})</div>
        <div class="textbox"><pre>${t.pl_current ? textDiff.left : '—'}</pre></div></div>
      <div><div class="side-label">Preferred source-file text (${CL.esc(t.preferred ? (t.preferred.file_path || t.preferred.file_name) : 'none')})</div>
        <div class="textbox"><pre>${t.preferred ? textDiff.right : '—'}</pre></div></div>
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

CL.syncPanel = (d) => `<div class="panel"><h2>Approved synchronization preview</h2>
  ${d.sync_preview.length ? `<table><thead><tr><th>field</th><th>status</th><th>PL before</th><th>approved value</th><th>note</th></tr></thead><tbody>
    ${d.sync_preview.map(i => `<tr><td>${CL.esc(i.field)}</td><td>${CL.badge(i.status, CL.statusClass(i.status))}</td>
      <td class="small">${CL.esc(JSON.stringify(i.pl_before))}</td><td class="small">${CL.esc(String(i.approved_value || '').slice(0, 160))}</td><td class="small">${CL.esc(i.message)}</td></tr>`).join('')}
    </tbody></table>${d.match.review_state !== CL.reviewState.APPROVED ? '<p class="small warn">Component is not approved for sync yet; nothing will be written.</p>' : ''}`
    : '<p class="muted">No approved SL-derived changes for this component.</p>'}
</div>`;

CL.notesPanel = (d) => `<div class="panel"><h2>Notes &amp; decision history</h2>
  ${d.notes.length ? d.notes.map(n => `<div class="notice"><b>${CL.esc(n.reviewer)}</b> <span class="small muted">${CL.esc(n.created_at)}</span><br>${CL.esc(n.body)}</div>`).join('') : '<p class="muted">No notes.</p>'}
  <label class="side-label mt-3" for="note-body">Review note</label>
  <textarea id="note-body" placeholder="What changed and why — kept with the component"></textarea>
  <div class="row-actions"><button id="add-note">Add note</button></div>
  ${d.decisions.length ? `<table class="mt-2"><thead><tr><th>field</th><th>entry</th><th>action</th><th>value</th><th>reviewer</th><th>decided</th></tr></thead><tbody>
    ${d.decisions.map(x => `<tr><td>${CL.esc(x.field)}</td><td class="small">${CL.esc(x.entry_key.slice(0, 10))}</td><td>${CL.badge(x.action)}</td><td class="small">${CL.esc(String(x.approved_value || '').slice(0, 80))}</td><td>${CL.esc(x.reviewer)}</td><td class="small">${CL.esc(x.created_at)}</td></tr>`).join('')}
  </tbody></table>` : ''}
</div>`;

CL.suggestionPanel = (d) => `<div class="panel"><h2>Machine suggestions (optional assistance)</h2>
  <p class="small muted">Suggestions are stored separately from human decisions. Accepting one creates a normal review decision; nothing is synchronized without component approval.</p>
  ${d.suggestions && d.suggestions.length ? `<table><thead><tr><th>field</th><th>entry</th><th>value</th><th>confidence</th><th>model</th><th>status</th><th>actions</th></tr></thead><tbody>
    ${d.suggestions.map(s => `<tr><td>${CL.esc(s.field)}</td><td class="small">${CL.esc(s.entry_key.slice(0, 10))}</td><td class="small">${CL.esc(String(s.value).slice(0, 120))}</td>
      <td class="num">${s.confidence.toFixed(2)}</td><td class="small">${CL.esc(s.model)}</td><td>${CL.badge(s.status, s.status === 'accepted' ? 'ok' : (s.status === 'rejected' ? 'bad' : 'warn'))}</td>
      <td class="acts row-actions">${s.status === 'open' ? `<button class="tiny" data-suggestion="${s.id}" data-sdec="accept">Accept</button><button class="tiny" data-suggestion="${s.id}" data-sdec="reject">Reject</button>` : '<span class="muted">—</span>'}</td></tr>`).join('')}
  </tbody></table>` : '<p class="muted">No suggestions recorded.</p>'}
</div>`;

CL.candidatePanel = (d) => `<div class="panel"><h2>Manual match candidates</h2>
  ${d.candidates.length ? `<table><thead><tr><th>SL component</th><th>version</th><th>similarity</th><th>action</th></tr></thead><tbody>
    ${d.candidates.map(c => `<tr><td>${CL.esc(c.sl_name)}</td><td class="num">${CL.esc(CL.dash(c.sl_version))}</td><td class="num">${c.similarity}</td><td><button class="tiny" data-assign="${CL.esc(c.sl_key)}">Assign</button></td></tr>`).join('')}
  </tbody></table>` : '<p class="muted">No unmatched SL components available.</p>'}
</div>`;

CL.bindWorkspace = (d) => {
  const id = d.match.id;
  const rerender = async () => { await CL.renderWorkspace(id); };
  const toggle = document.getElementById('diff-toggle');
  if (toggle) {
    toggle.onclick = () => {
      CL.diffOn = !CL.diffOn;
      // Paint is a class on the view root: the marks stay in the DOM, only their colour changes.
      document.getElementById('view-workspace').classList.toggle('diff-off', !CL.diffOn);
      toggle.setAttribute('aria-checked', String(CL.diffOn));
      toggle.textContent = CL.diffOn ? 'Highlight diff' : 'Plain text';
    };
  }
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
  if (confirmBtn) confirmBtn.onclick = () => CL.click(async () => { await CL.api('POST', `/api/components/${id}/confirm`); await rerender(); });
  document.getElementById('w-reassign').onclick = () => CL.click(async () => {
    const key = await CL.dialog({
      title: 'Reassign SL component',
      message: 'Paste the SL component key to match this PL component against (see manual match candidates below).',
      field: 'SL component key', value: d.match.sl_key || '', confirmText: 'Reassign',
    });
    if (key === null) return;
    await CL.api('POST', `/api/components/${id}/reassign`, { sl_key: key.trim() });
    await rerender();
  });
  document.getElementById('w-reject').onclick = () => CL.click(async () => {
    if (!await CL.confirm('Reject this match? It will not be presented as a new discovery after refresh.', { title: 'Reject match', confirmText: 'Reject match' })) return;
    await CL.api('POST', `/api/components/${id}/reject`, { reason: 'reviewer rejected' });
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
};
