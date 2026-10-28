// Browser smoke test for the CL Review UI. Run: node tests/web_smoke.mjs
// Needs: node >= 22 (global WebSocket), a chromium binary, and the app's python.
// Env: CL_PYTHON (default: this interpreter's sibling venv), CL_CHROME (default: chromium)
import { spawn, execFileSync } from 'node:child_process';
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
// throwaway dirs: a chromium already running on the default profile would swallow this launch,
// and the app's own database keeps the demo data identical from run to run
const dirs = [];
const mktmp = (prefix) => { const d = fs.mkdtempSync(path.join(os.tmpdir(), prefix)); dirs.push(d); return d; };
const appDir = mktmp('cl-smoke-');
const server = spawn(PY, ['-m', 'clreview.server', '--demo', '--port', String(port),
  '--db', path.join(appDir, 'smoke.db')], { stdio: 'ignore' });

// Every flag below is a switch the chromium binary really has. Check the binary you launch (for a
// snap that is /snap/chromium/current/usr/lib/chromium-browser/chrome, not the wrapper on PATH);
// switch names are stored without the leading dashes:
//   grep -ac disable-dev-shm-usage /snap/chromium/current/usr/lib/chromium-browser/chrome
const chromeFlags = (cdp, profile) => [
  '--headless=new',
  `--user-data-dir=${profile}`,
  `--remote-debugging-port=${cdp}`,
  '--no-sandbox',                    // CI runners are unprivileged containers; the sandbox needs user namespaces
  '--disable-gpu',                   // no GPU: without this chrome spends startup failing to initialise one
  '--no-first-run',
  '--no-default-browser-check',      // a first-run prompt opens a second page target this test would then ignore
  '--disable-dev-shm-usage',         // ubuntu-latest mounts /dev/shm at 64 MB; a renderer that fills it dies before CDP answers
  '--disable-breakpad',              // no session bus on a runner (the dbus line in the log); keeps the crash handler off the startup path
  '--disable-background-networking', // first-run update/component probes race the CDP endpoint on a restricted runner
  'about:blank',
];

const procs = [server];
const shutdown = () => {
  for (const p of procs) { try { p.kill(); } catch {} }
  for (const d of dirs) { try { fs.rmSync(d, { recursive: true, force: true }); } catch {} }
};
process.on('exit', shutdown);

const cdpCall = (cdp, p, init) => fetch(`http://127.0.0.1:${cdp}${p}`, init);
const oneLine = (s, n) => s.replace(/\s+/g, ' ').slice(0, n);

// One launch attempt, with a fresh port and a fresh profile: a chrome that died can leave a
// SingletonLock in its profile dir, and a fresh dir sidesteps the stale-lock path entirely.
const launchChrome = async (attempt) => {
  const cdpPort = await freePort();
  const profile = mktmp('cl-smoke-chrome-');
  const log = path.join(profile, 'chrome.log');
  const flags = chromeFlags(cdpPort, profile);
  const proc = spawn(CHROME, flags, { stdio: ['ignore', 'ignore', fs.openSync(log, 'w')] });
  procs.push(proc);
  const st = { attempt, cdpPort, profile, log, flags, proc, exit: null, probes: [] };
  proc.on('exit', (code, sig) => { st.exit = code === null ? `killed by ${sig}` : `exited ${code}`; });
  proc.on('error', (e) => { st.exit = `spawn failed: ${e.code || e.message}`; });
  return st;
};

const probe = (st, line) => { if (st.probes.length < 40 && !st.probes.includes(line)) st.probes.push(line); };

// Proof chrome is up is the endpoint answering /json/version — not a fixed sleep budget, which is
// what made this flake between runners. A process that already died is reported at once.
const waitForCdp = async (st, ms) => {
  const started = Date.now();
  for (;;) {
    if (st.exit) { probe(st, `/json/version -> chrome ${st.exit} before answering`); return false; }
    try {
      const res = await cdpCall(st.cdpPort, '/json/version');
      probe(st, `/json/version -> ${res.status} ${oneLine(await res.text(), 160)}`);
      if (res.ok) return true;
    } catch (e) { probe(st, `/json/version -> ${e.name}: ${e.message}`); }
    if (Date.now() - started > ms) return false;
    await sleep(250);
  }
};

// CDP can answer before any page target exists (the launch URL is opened after the browser is
// serving), so ask for a page rather than wait for chrome to make one.
const pageTarget = async (st, ms) => {
  const started = Date.now();
  let asked = false;
  for (;;) {
    try {
      const list = await (await cdpCall(st.cdpPort, '/json/list')).json();
      const page = list.find((x) => x.type === 'page' && x.webSocketDebuggerUrl);
      if (page) { probe(st, `/json/list -> ${list.length} targets, using page ${page.id}`); return page; }
      probe(st, `/json/list -> ${list.length} targets, none a page`);
      if (!asked) {
        asked = true;
        for (const method of ['PUT', 'GET']) {   // /json/new moved from GET to PUT; take whichever answers
          try {
            const res = await cdpCall(st.cdpPort, '/json/new?about:blank', { method });
            probe(st, `${method} /json/new -> ${res.status} ${oneLine(await res.text(), 120)}`);
            if (res.ok) break;
          } catch (e) { probe(st, `${method} /json/new -> ${e.message}`); }
        }
      }
    } catch (e) { probe(st, `/json/list -> ${e.name}: ${e.message}`); }
    if (Date.now() - started > ms) return null;
    await sleep(250);
  }
};

// The runner log only showed a dbus line. On failure, dump everything that could name the cause.
const reportChrome = (attempts, why) => {
  console.log(`\n--- chromium diagnostics (${why}) ---`);
  console.log(`binary: ${CHROME}`);
  try { console.log(`version: ${execFileSync(CHROME, ['--version'], { encoding: 'utf8', timeout: 10000 }).trim()}`); }
  catch (e) { console.log(`version: --version failed: ${oneLine(e.message, 160)}`); }
  for (const st of attempts) {
    console.log(`\nattempt ${st.attempt}: cdp port ${st.cdpPort}, profile ${st.profile}, process ${st.exit || 'still running'}`);
    console.log(`  flags: ${st.flags.join(' ')}`);
    console.log('  cdp probes:');
    for (const p of st.probes) console.log(`    ${p}`);
    let log = '';
    try { log = fs.readFileSync(st.log, 'utf8'); } catch (e) { log = `(${st.log} unreadable: ${e.message})`; }
    console.log(`  ${st.log}:\n${(log.trim() || '(empty)').split('\n').map((l) => '    ' + l).join('\n')}`);
  }
  console.log('--- end chromium diagnostics ---\n');
};

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
  // A fresh demo has nothing reviewed: decide every open discrepancy through the API first, or the
  // approval below is refused and the sync/deploy sections have nothing to show.
  const approveMe = comps.items.find((c) => c.status !== 'unmatched' && c.discrepancy_count > 0) || comps.items[0];
  const det = await api('GET', `/api/components/${approveMe.id}`);
  const decide = (field, entry_key) => api('POST', `/api/components/${approveMe.id}/decisions`,
    { field, entry_key, action: 'approve_sl' });
  const COPYRIGHT_KINDS = ['variant', 'sl_only', 'pl_only'];
  const LICENSE_KINDS = ['spdx_diff', 'sl_only', 'pl_only'];
  for (const r of det.comparison.copyrights) if (COPYRIGHT_KINDS.includes(r.kind)) await decide('copyright', r.key);
  for (const r of det.comparison.licenses) if (LICENSE_KINDS.includes(r.kind)) await decide('license_name', r.key);
  const ltx = det.comparison.license_text;
  if (ltx && ltx.recommendation === 'replace_with_source' && ltx.preferred) {
    await decide('license_text', ltx.preferred.content_hash);
  }
  const approved = await api('POST', `/api/components/${approveMe.id}/review`, { state: 'approved_for_sync' });
  ok('setup: a demo component reaches approved-for-sync', approved.review_status === 'approved_for_sync',
    approved.error || `${approveMe.pl_name || approveMe.sl_name}`);

  // Two attempts: a runner that loses the first launch (shm pressure, a slow bus) gets a clean
  // second one instead of a red suite.
  let target = null;
  const attempts = [];
  for (let attempt = 1; attempt <= 2 && !target; attempt++) {
    const st = await launchChrome(attempt);
    attempts.push(st);
    if (await waitForCdp(st, 30000)) target = await pageTarget(st, 10000);
    if (!target && attempt < 2) {
      console.log(`chromium attempt ${attempt} gave no page target (${st.exit || 'process alive'}); relaunching`);
      try { st.proc.kill(); } catch {}
    }
  }
  if (!target) {
    reportChrome(attempts, 'no page target after 2 attempts');
    shutdown();
    throw new Error('chromium gave no CDP page target after 2 attempts — see the diagnostics above');
  }
  if (attempts.length > 1) reportChrome(attempts.slice(0, 1), 'attempt 1 needed a retry');

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
  // Poll a page condition instead of sleeping a fixed time: a slow runner waits, it does not flake.
  const until = async (label, predicate, ms = 10000) => {
    const started = Date.now();
    for (;;) {
      if (await ev(`(() => { try { return !!(${predicate}); } catch { return false; } })()`)) return true;
      if (Date.now() - started > ms) { ok(`wait: ${label}`, false, `timed out after ${ms}ms`); return false; }
      await sleep(100);
    }
  };
  // quiet: no request in flight for three polls running, so the render the last response triggered has run
  const quiet = async (label = 'network', ms = 10000) => {
    const started = Date.now();
    let still = 0;
    for (;;) {
      still = (await ev(`window.__pending === 0 ? 1 : 0`)) ? still + 1 : 0;
      if (still >= 3) return true;
      if (Date.now() - started > ms) { ok(`wait: ${label} settled`, false, `still busy after ${ms}ms`); return false; }
      await sleep(100);
    }
  };
  const untilApi = async (label, test, ms = 20000) => {
    const started = Date.now();
    for (;;) {
      if (await test()) return true;
      if (Date.now() - started > ms) { ok(`wait: ${label}`, false, `timed out after ${ms}ms`); return false; }
      await sleep(200);
    }
  };
  const go = async (hash, ready = `document.querySelector('.view:not(.hidden) h1')`) => {
    await send('Page.navigate', { url: base + hash });
    await until(`rendered ${hash}`, `location.hash === ${JSON.stringify(hash.slice(1))} && ${ready}`);
    await quiet(`view ${hash}`);
  };

  await send('Page.enable'); await send('Runtime.enable');
  // Count in-flight requests from the first document on: the app re-renders a view when its data
  // arrives, so a click that lands before the last render is a click on a node about to be replaced.
  await send('Page.addScriptToEvaluateOnNewDocument', { source: `
    window.__pending = 0;
    window.__rej = [];
    addEventListener('unhandledrejection', (e) => window.__rej.push(String((e.reason && e.reason.message) || e.reason)));
    const realFetch = window.fetch;
    window.fetch = (...a) => { window.__pending++; return realFetch(...a).finally(() => { window.__pending--; }); };
  ` });
  await size(1440, 1000);

  // --- reviewer gate: a write with no name is refused at the field ---
  await go('/#/history');
  await ev(`localStorage.removeItem('cl.reviewer'); document.getElementById('reviewer').value = ''; 1`);
  const NOTES = `document.querySelectorAll('.notice:not(.pl_only):not(.sl_only)').length`;
  await go('/#/component/7', `document.getElementById('note-body')`);
  const n0 = await ev(NOTES);
  await ev(`(() => { const t = document.getElementById('note-body'); t.value = 'smoke note'; t.focus(); document.getElementById('add-note').click(); })()`);
  await quiet('reviewer gate');
  await until('inline reviewer error', `document.getElementById('reviewer-error').hidden === false`);
  ok('unnamed write is blocked inline', await ev(`document.getElementById('reviewer-error').hidden === false
    && document.getElementById('reviewer').getAttribute('aria-invalid') === 'true'
    && document.activeElement.id === 'reviewer'
    && document.getElementById('toast').classList.contains('hidden')`));
  ok('unnamed write created nothing', (await ev(NOTES)) === n0);

  await ev(`(() => { const i = document.getElementById('reviewer'); i.value = 'Smoke Tester'; i.dispatchEvent(new Event('input', { bubbles: true })); })()`);
  await quiet('reviewer name');
  await until('reviewer error cleared', `document.getElementById('reviewer-error').hidden === true && !document.getElementById('reviewer').hasAttribute('aria-invalid')`);
  ok('typing a name clears the error', await ev(`document.getElementById('reviewer-error').hidden === true
    && !document.getElementById('reviewer').hasAttribute('aria-invalid')`));

  // --- workspace: a decision records, and focus survives the re-render ---
  const before = await ev(NOTES);
  await ev(`(() => { const t = document.getElementById('note-body'); t.value = 'smoke note 2'; t.focus(); document.getElementById('add-note').click(); })()`);
  await quiet('note write');
  await until('note written', `${NOTES} > ${before}`);
  ok('named write lands', (await ev(NOTES)) === before + 1);
  ok('focus survives the re-render', await ev(`document.activeElement.id === 'note-body'`), await ev(`document.activeElement.tagName + '#' + document.activeElement.id`));

  // --- queue: triage order, discrepancy styling, clear-filters ---
  await go('/#/queue', `document.querySelectorAll('td[data-label=discrepancies]').length > 0`);
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
  await quiet('queue search');
  await until('search applied', `document.getElementById('q-clear').disabled === false`);
  ok('clear filters enables with a filter', await ev(`document.getElementById('q-clear').disabled === false`));
  await ev(`document.getElementById('q-clear').click(); 1`);
  await quiet('queue clear');
  await until('filters reset', `document.getElementById('q-search').value === '' && document.getElementById('q-clear').disabled === true`);
  ok('clear filters resets the search', await ev(`document.getElementById('q-search').value === '' && document.getElementById('q-clear').disabled`));

  // --- a re-render replaces the rows under the reviewer; focus and the search value stay ---
  await ev(`(() => { const i = document.getElementById('q-search'); i.focus(); i.value = 'lib';
    i.dispatchEvent(new Event('input', { bubbles: true }));
    i.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true })); })()`);
  await quiet('search re-render');
  ok('focus and the search value survive a re-render',
     await ev(`document.activeElement.id === 'q-search' && document.getElementById('q-search').value === 'lib'`),
     await ev(`document.activeElement.tagName + '#' + document.activeElement.id + '=' + document.getElementById('q-search').value`));
  await ev(`document.getElementById('q-clear').click(); 1`);
  await quiet('second clear');

  // --- a click that arrives mid-request is explained, not swallowed ---
  const drop = JSON.parse(await ev(`(() => {
    CL.state.inflight = 1;
    CL.run(() => Promise.resolve('ran'));
    const text = document.getElementById('toast').textContent;
    CL.state.inflight = 0;
    return JSON.stringify({ text });
  })()`));
  ok('a dropped click says why', /still/i.test(drop.text), drop.text);

  // --- two navigations at once: only the newest may paint ---
  await send('Page.navigate', { url: base + '/#/queue' });
  await send('Page.navigate', { url: base + '/#/overview' });
  await until('newest view won', `location.hash === '#/overview' && (document.querySelector('.view:not(.hidden) h1') || {}).textContent`);
  const race = JSON.parse(await ev(`JSON.stringify({ hash: location.hash, h1: (document.querySelector('.view:not(.hidden) h1') || {}).textContent || '' })`));
  ok('the newest navigation wins the render', race.hash === '#/overview' && !/Review queue/.test(race.h1), `${race.hash} / ${race.h1}`);

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
    await quiet('sync approval');
    await until('execute enabled', `document.getElementById('sync-live').disabled === false`);
    ok('execute enables once approved', await ev(`document.getElementById('sync-live').disabled === false`));
    // leave a job behind so the overview job link has something to open
    await ev(`document.getElementById('sync-dry').click(); 1`);
    await quiet('dry run');
    await until('dry-run job detail', `((document.querySelector('#job-detail h2') || {}).textContent || '').includes('Job #')`, 20000);
    ok('dry run records a job and opens its detail', /Job #\d+/.test(await ev(`(document.querySelector('#job-detail h2') || {}).textContent || ''`)));
  }

  // --- a job row is reachable without a mouse ---
  await go('/#/history');
  const href = await ev(`(() => { const a = document.querySelector('#view-history tbody a[href^="#/history/job/"]'); return a ? a.getAttribute('href') : ''; })()`);
  ok('history job rows are links', href === '' || /^#\/history\/job\/\d+$/.test(href), href || 'no job rows');

  // --- overview job link opens that job ---
  await go('/#/overview');
  const jobId = await ev(`(document.querySelector('#view-overview a[href^="#/history/job/"]') || {}).textContent || ''`);
  if (jobId) {
    await ev(`document.querySelector('#view-overview a[href^="#/history/job/"]').click(); 1`);
    await quiet('job link');
    await until('job detail from overview', `((document.querySelector('#job-detail h2') || {}).textContent || '').includes(${JSON.stringify(jobId)})`, 20000);
    ok('overview job link opens the job', (await ev(`(document.querySelector('#job-detail h2') || {}).textContent || ''`)).includes(jobId), `want ${jobId}, detail: ${await ev(`(document.querySelector('#job-detail') || {}).innerText?.replace(/\s+/g, ' ').slice(0, 80)`)}`);
  } else ok('overview job link opens the job', false, 'overview shows: ' + await ev(`(document.querySelector('#view-overview') || {}).innerText?.replace(/\s+/g, ' ').slice(0, 140)`));

  // --- deploy: preview first, then an explicit approval, then the write ---
  await go('/#/deploy', `document.getElementById('dp-run')`);
  const dg = JSON.parse(await ev(`JSON.stringify({ disabled: document.getElementById('dp-run').disabled,
    hint: document.getElementById('dp-hint').textContent.length > 10, described: document.getElementById('dp-run').getAttribute('aria-describedby') === 'dp-hint' })`));
  ok('upload starts disabled with a reason', dg.disabled && dg.hint && dg.described);
  const folderMaps = async () => (await api('GET', '/api/mappings')).items.filter((m) => String(m.sl_scan).startsWith('folder:')).length;
  const maps0 = await folderMaps();
  await ev(`document.getElementById('dp-preview').click(); 1`);
  await quiet('deploy preview');
  await until('deploy preview rows', `document.querySelectorAll('#dp-report tbody tr').length > 0`, 20000);
  const pv = JSON.parse(await ev(`JSON.stringify({ rows: document.querySelectorAll('#dp-report tbody tr').length,
    disabled: document.getElementById('dp-run').disabled, hint: document.getElementById('dp-hint').textContent })`));
  ok('preview lists every library', pv.rows > 0, `${pv.rows} rows`);
  ok('preview alone still cannot write', pv.disabled === true && /approval/i.test(pv.hint), pv.hint);
  ok('preview writes nothing', (await folderMaps()) === maps0);
  await ev(`(() => { const c = document.getElementById('dp-confirm'); c.checked = true; c.dispatchEvent(new Event('change')); })()`);
  await quiet('deploy approval');
  await until('upload enabled after approval', `document.getElementById('dp-run').disabled === false`);
  ok('upload enables once approved', await ev(`document.getElementById('dp-run').disabled === false`));
  await ev(`(() => { const i = document.getElementById('dp-folder'); i.value = 'smoke-changed'; i.dispatchEvent(new Event('input', { bubbles: true })); })()`);
  await quiet('target change');
  await until('target change invalidates preview', `document.getElementById('dp-run').disabled === true`);
  ok('changing the target invalidates the preview', await ev(`document.getElementById('dp-run').disabled === true`));
  await ev(`(() => { const i = document.getElementById('dp-folder'); i.value = ''; i.dispatchEvent(new Event('input', { bubbles: true })); })()`);
  await quiet('target cleared');
  await until('target cleared', `document.getElementById('dp-folder').value === ''`);
  await ev(`document.getElementById('dp-run').click(); 1`);
  await quiet('live upload', 30000);
  await until('live upload report', `[...document.querySelectorAll('#dp-report .stat')].some((s) => /uploaded/.test(s.textContent))`, 30000);
  const lv = JSON.parse(await ev(`JSON.stringify({ uploaded: [...document.querySelectorAll('#dp-report .stat')].some(s => /uploaded/.test(s.textContent)),
    cta: !!document.getElementById('dp-mapping') })`));
  ok('live run reports uploads', lv.uploaded === true);
  ok('a deployed folder offers a mapping', lv.cta === true);
  await ev(`document.getElementById('dp-mapping').click(); 1`);
  await untilApi('mapping for the deployed folder', async () => (await folderMaps()) === maps0 + 1);
  ok('the mapping points at the deployed folder', (await folderMaps()) === maps0 + 1);

  // --- shell: no dead controls, one h1 per view, mobile fits ---
  ok('sidebar has no duplicate synchronize button', await ev(`!document.getElementById('sync-btn') && !document.getElementById('authnote')`));
  for (const [hash, name] of [['/#/overview', 'overview'], ['/#/queue', 'queue'], ['/#/history', 'history'], ['/#/deploy', 'deploy']]) {
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
  const rej = await ev(`(window.__rej || []).join(' | ')`);
  ok('no unhandled promise rejections', !rej, rej);
  ws.close();
} finally {
  shutdown();
}

console.log(fails.length ? `\n${fails.length} FAILED: ${fails.join(', ')}` : '\nall smoke checks passed');
process.exit(fails.length ? 1 : 0);
