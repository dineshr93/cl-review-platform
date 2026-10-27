// Browser smoke test for the CL Review UI. Run: node tests/web_smoke.mjs
// Needs: node >= 22 (global WebSocket), a chromium binary, and the app's python.
// Env: CL_PYTHON (default: this interpreter's sibling venv), CL_CHROME (default: chromium)
import { spawn } from 'node:child_process';
import net from 'node:net';
import os from 'node:os';
import fs from 'node:fs';
import path from 'node:path';
import { setTimeout as sleep } from 'node:timers/promises';

const CHROME = process.env.CL_CHROME || 'chromium';
const PY = process.env.CL_PYTHON || 'python3';
const taken = new Set();
const probePort = () => new Promise((res) => { const s = net.createServer().listen(0, '127.0.0.1', () => { const p = s.address().port; s.close(() => res(p)); }); });
const freePort = async () => { let p; do { p = await probePort(); } while (taken.has(p)); taken.add(p); return p; };

const fails = [];
const ok = (name, pass, detail = '') => { console.log(`${pass ? 'PASS' : 'FAIL'}  ${name}${detail ? ' — ' + detail : ''}`); if (!pass) fails.push(name); };

const port = await freePort();
const base = `http://127.0.0.1:${port}`;
const server = spawn(PY, ['-m', 'clreview.server', '--demo', '--port', String(port)], { stdio: 'ignore' });
const cdpPort = await freePort();
// a throwaway profile: a chromium already running on the default one would swallow this launch
const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'cl-smoke-'));
const chromeLog = path.join(profile, 'chrome.log');
const chrome = spawn(CHROME, ['--headless=new', '--no-sandbox', '--disable-gpu', '--no-first-run',
  `--user-data-dir=${profile}`, `--remote-debugging-port=${cdpPort}`, 'about:blank'],
  { stdio: ['ignore', 'ignore', fs.openSync(chromeLog, 'w')] });

const shutdown = () => {
  try { server.kill(); chrome.kill(); } catch {}
  try { fs.rmSync(profile, { recursive: true, force: true }); } catch {}
};
process.on('exit', shutdown);

try {
  // --- wait for the app, then seed through the API so the sync path has something to do ---
  let up = false;
  for (let i = 0; i < 80 && !up; i++) { try { up = (await fetch(base + '/api/health')).ok; } catch { await sleep(250); } }
  if (!up) throw new Error('demo server did not answer on ' + base);
  const api = async (method, p, body) => (await fetch(base + p, {
    method,
    headers: { 'Content-Type': 'application/json', 'X-Reviewer': 'Smoke Tester' },
    body: body ? JSON.stringify(body) : undefined,
  })).json();
  const mappings = await api('GET', '/api/mappings');
  const mapId = mappings.items[0].id;
  const comps = await api('GET', `/api/mappings/${mapId}/components?limit=200`);
  const toApprove = comps.items.find((c) => c.review_status === 'reviewed') || comps.items[0];
  await api('POST', `/api/components/${toApprove.id}/review`, { state: 'approved_for_sync' });

  let target;
  for (let i = 0; i < 80 && !target; i++) {
    await sleep(250);
    try {
      const list = await (await fetch(`http://127.0.0.1:${cdpPort}/json/list`)).json();
      target = list.find((x) => x.type === 'page');
    } catch { /* chrome still starting */ }
  }
  if (!target) {
    let err = '';
    try { err = fs.readFileSync(chromeLog, 'utf8').replace(/\s+/g, ' ').slice(0, 300); } catch {}
    shutdown();
    throw new Error('chromium did not start on cdp port ' + cdpPort + (err ? ': ' + err : ''));
  }

  const ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
  let mid = 0; const waiting = {}; const consoleErrors = [];
  ws.onmessage = (e) => {
    const o = JSON.parse(e.data);
    if (o.id && waiting[o.id]) { waiting[o.id](o); delete waiting[o.id]; }
    if (o.method === 'Runtime.exceptionThrown') consoleErrors.push((o.params.exceptionDetails.exception?.description || o.params.exceptionDetails.text || '').split('\n')[0]);
    if (o.method === 'Runtime.consoleAPICalled' && o.params.type === 'error') consoleErrors.push(o.params.args.map((a) => a.value || a.description || '').join(' ').slice(0, 140));
  };
  const send = (method, params = {}) => new Promise((res) => { const i = ++mid; waiting[i] = res; ws.send(JSON.stringify({ id: i, method, params })); });
  const ev = async (expression) => (await send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true })).result?.result?.value;
  const size = (w, h) => send('Emulation.setDeviceMetricsOverride', { width: w, height: h, deviceScaleFactor: 1, mobile: w < 500 });
  const go = async (hash) => { await send('Page.navigate', { url: base + hash }); await sleep(1500); };

  await send('Page.enable'); await send('Runtime.enable');
  await size(1440, 1000);

  // --- reviewer gate: a write with no name is refused at the field ---
  await go('/#/history');
  await ev(`localStorage.removeItem('cl.reviewer'); document.getElementById('reviewer').value = ''; 1`);
  const NOTES = `document.querySelectorAll('.notice:not(.pl_only):not(.sl_only)').length`;
  await go('/#/component/7');
  const n0 = await ev(NOTES);
  await ev(`(() => { const t = document.getElementById('note-body'); t.value = 'smoke note'; t.focus(); document.getElementById('add-note').click(); })()`);
  await sleep(400);
  ok('unnamed write is blocked inline', await ev(`document.getElementById('reviewer-error').hidden === false
    && document.getElementById('reviewer').getAttribute('aria-invalid') === 'true'
    && document.activeElement.id === 'reviewer'
    && document.getElementById('toast').classList.contains('hidden')`));
  ok('unnamed write created nothing', (await ev(NOTES)) === n0);

  await ev(`(() => { const i = document.getElementById('reviewer'); i.value = 'Smoke Tester'; i.dispatchEvent(new Event('input', { bubbles: true })); })()`);
  await sleep(150);
  ok('typing a name clears the error', await ev(`document.getElementById('reviewer-error').hidden === true
    && !document.getElementById('reviewer').hasAttribute('aria-invalid')`));

  // --- workspace: a decision records, and focus survives the re-render ---
  const before = await ev(NOTES);
  await ev(`(() => { const t = document.getElementById('note-body'); t.value = 'smoke note 2'; t.focus(); document.getElementById('add-note').click(); })()`); await sleep(1600);
  ok('named write lands', (await ev(NOTES)) === before + 1);
  ok('focus survives the re-render', await ev(`document.activeElement.id === 'note-body'`), await ev(`document.activeElement.tagName + '#' + document.activeElement.id`));

  // --- queue: triage order, discrepancy styling, clear-filters ---
  await go('/#/queue');
  const q = await ev(`(() => {
    const nums = [...document.querySelectorAll('td[data-label=discrepancies]')].map(td => +td.textContent.trim());
    return JSON.stringify({ nums, badged: document.querySelectorAll('td[data-label=discrepancies] .badge').length,
      clearDisabled: document.getElementById('q-clear').disabled, sort: document.getElementById('q-sort').value,
      searchLabel: document.getElementById('q-apply').textContent.trim() });
  })()`);
  const qq = JSON.parse(q);
  ok('queue defaults to triage order', qq.sort === 'discrepancies' && qq.nums.join() === [...qq.nums].sort((a, b) => b - a).join(), qq.nums.join());
  ok('discrepancies are painted only when non-zero', qq.badged === qq.nums.filter((n) => n > 0).length, `${qq.badged}/${qq.nums.length}`);
  ok('clear filters starts disabled', qq.clearDisabled === true);
  ok('search button says what it does', qq.searchLabel === 'Search');
  await ev(`(() => { const i = document.getElementById('q-search'); i.value = 'zlib'; i.dispatchEvent(new Event('input', { bubbles: true })); document.getElementById('q-apply').click(); })()`);
  await sleep(1200);
  ok('clear filters enables with a filter', await ev(`document.getElementById('q-clear').disabled === false`));
  await ev(`document.getElementById('q-clear').click(); 1`); await sleep(1200);
  ok('clear filters resets the search', await ev(`document.getElementById('q-search').value === '' && document.getElementById('q-clear').disabled`));

  // --- history: destructive action is marked, disabled, and says why ---
  await go('/#/history');
  const h = await ev(`(() => { const b = document.getElementById('sync-live'); return b ? JSON.stringify({
      disabled: b.disabled, danger: b.classList.contains('danger'), hint: document.getElementById('sync-hint').textContent.length > 10,
      described: b.getAttribute('aria-describedby') === 'sync-hint' }) : 'no pending writes'; })()`);
  if (h === 'no pending writes') ok('history shows pending writes', false, 'demo has nothing approved');
  else {
    const hh = JSON.parse(h);
    ok('execute is destructive and disabled with a reason', hh.disabled && hh.danger && hh.hint && hh.described);
    await ev(`(() => { const c = document.getElementById('sync-confirm'); c.checked = true; c.dispatchEvent(new Event('change')); })()`);
    await sleep(200);
    ok('execute enables once approved', await ev(`document.getElementById('sync-live').disabled === false`));
    // leave a job behind so the overview job link has something to open
    await ev(`document.getElementById('sync-dry').click(); 1`); await sleep(2500);
    ok('dry run records a job and opens its detail', /Job #\d+/.test(await ev(`(document.querySelector('#job-detail h2') || {}).textContent || ''`)));
  }

  // --- overview job link opens that job ---
  await go('/#/overview');
  const jobId = await ev(`(document.querySelector('#view-overview a[href^="#/history/job/"]') || {}).textContent || ''`);
  if (jobId) {
    await ev(`document.querySelector('#view-overview a[href^="#/history/job/"]').click(); 1`); await sleep(1500);
    ok('overview job link opens the job', (await ev(`(document.querySelector('#job-detail h2') || {}).textContent || ''`)).includes(jobId), `want ${jobId}, detail: ${await ev(`(document.querySelector('#job-detail') || {}).innerText?.replace(/\s+/g, ' ').slice(0, 80)`)}`);
  } else ok('overview job link opens the job', false, 'overview shows: ' + await ev(`(document.querySelector('#view-overview') || {}).innerText?.replace(/\s+/g, ' ').slice(0, 140)`));

  // --- shell: no dead controls, one h1 per view, mobile fits ---
  ok('sidebar has no duplicate synchronize button', await ev(`!document.getElementById('sync-btn') && !document.getElementById('authnote')`));
  for (const [hash, name] of [['/#/overview', 'overview'], ['/#/queue', 'queue'], ['/#/history', 'history']]) {
    await go(hash);
    ok(`one h1 in ${name}`, (await ev(`document.querySelectorAll('.view:not(.hidden) h1').length`)) === 1);
  }
  for (const w of [390, 320]) {
    await size(w, 800); await go('/#/overview');
    const m = JSON.parse(await ev(`JSON.stringify({ scrollW: document.scrollingElement.scrollWidth, innerW: innerWidth,
      toggleRight: Math.round(document.getElementById('theme-toggle').getBoundingClientRect().right) })`));
    ok(`no horizontal overflow at ${w}px`, m.scrollW <= m.innerW, `${m.scrollW}/${m.innerW}`);
    ok(`theme toggle inside viewport at ${w}px`, m.toggleRight <= m.innerW, `${m.toggleRight}/${m.innerW}`);
  }

  ok('no console errors or unhandled rejections', consoleErrors.length === 0, consoleErrors.slice(0, 3).join(' | '));
  ws.close();
} finally {
  shutdown();
}

console.log(fails.length ? `\n${fails.length} FAILED: ${fails.join(', ')}` : '\nall smoke checks passed');
process.exit(fails.length ? 1 : 0);
