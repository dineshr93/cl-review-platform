// Capture docs/screenshots/*.png from the running demo, then review the PNGs before committing.
// Run: CL_CHROME=/usr/bin/chromium-browser node tools/capture_screenshots.mjs
import { spawn } from 'node:child_process';
import net from 'node:net';
import os from 'node:os';
import fs from 'node:fs';
import path from 'node:path';
import { setTimeout as sleep } from 'node:timers/promises';

const CHROME = process.env.CL_CHROME || 'chromium';
const PY = process.env.CL_PYTHON || 'python3';
const OUT = path.resolve('docs/screenshots');
const REVIEW = '/tmp/cl-shots';
fs.mkdirSync(OUT, { recursive: true });
fs.mkdirSync(REVIEW, { recursive: true });

const probePort = () => new Promise((res) => { const s = net.createServer().listen(0, '127.0.0.1', () => { const p = s.address().port; s.close(() => res(p)); }); });
const dirs = [];
const mktmp = (p) => { const d = fs.mkdtempSync(path.join(os.tmpdir(), p)); dirs.push(d); return d; };

const port = await probePort();
const base = `http://127.0.0.1:${port}`;
const appDir = mktmp('cl-shots-');
const server = spawn(PY, ['-m', 'clreview.server', '--demo', '--port', String(port), '--db', path.join(appDir, 'shots.db')], { stdio: 'ignore' });
const shutdown = () => { try { server.kill(); } catch {} for (const d of dirs) { try { fs.rmSync(d, { recursive: true, force: true }); } catch {} } };
process.on('exit', shutdown);

let up = false;
for (let i = 0; i < 80 && !up; i++) { try { up = (await fetch(base + '/api/health')).ok; } catch { await sleep(250); } }
if (!up) throw new Error('demo server did not answer on ' + base);

const api = async (method, p, body) => (await fetch(base + p, {
  method, headers: { 'Content-Type': 'application/json', 'X-Reviewer': 'Dinesh R' },
  body: body ? JSON.stringify(body) : undefined,
})).json();

const mapId = (await api('GET', '/api/mappings')).items[0].id;
const comps = await api('GET', `/api/mappings/${mapId}/components?limit=200`);
// Decide the fuzzy libpng match so the workspace has diff + progress, then approve it.
const target = comps.items.find((c) => c.status === 'suggested') || comps.items[0];
const det = await api('GET', `/api/components/${target.id}`);
const decide = (field, entry_key) => api('POST', `/api/components/${target.id}/decisions`, { field, entry_key, action: 'approve_sl' });
for (const r of det.comparison.copyrights) if (['variant', 'sl_only', 'pl_only'].includes(r.kind)) await decide('copyright', r.key);
for (const r of det.comparison.licenses) if (['spdx_diff', 'sl_only', 'pl_only'].includes(r.kind)) await decide('license_name', r.key);
const ltx = det.comparison.license_text;
if (ltx && ltx.recommendation === 'replace_with_source' && ltx.preferred) await decide('license_text', ltx.preferred.content_hash);
await api('POST', `/api/components/${target.id}/review`, { state: 'approved_for_sync' });
// A dry run and a live job so the history view has real rows to show.
await api('POST', '/api/sync/jobs', { mapping_id: mapId, dry_run: true });
await api('POST', '/api/sync/jobs', { mapping_id: mapId, dry_run: false, confirm: true });
console.log('seeded: component', target.id, target.pl_name || target.sl_name);

const cdpPort = await probePort();
const profile = mktmp('cl-shots-chrome-');
const chrome = spawn(CHROME, [
  '--headless=new', `--user-data-dir=${profile}`, `--remote-debugging-port=${cdpPort}`,
  '--no-sandbox', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
  '--disable-dev-shm-usage', '--disable-breakpad', '--disable-background-networking', 'about:blank',
], { stdio: 'ignore' });
dirs.push(profile);

const cdp = (p, init) => fetch(`http://127.0.0.1:${cdpPort}${p}`, init);
let page = null;
for (let i = 0; i < 120 && !page; i++) {
  try {
    const list = await (await cdp('/json/list')).json();
    page = list.find((x) => x.type === 'page' && x.webSocketDebuggerUrl) || null;
  } catch { /* not up yet */ }
  if (!page) await sleep(250);
}
if (!page) throw new Error('no CDP page target');

const ws = new WebSocket(page.webSocketDebuggerUrl);
await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
let mid = 0; const waiting = {};
ws.onmessage = (e) => { const o = JSON.parse(e.data); if (o.id && waiting[o.id]) { waiting[o.id](o); delete waiting[o.id]; } };
const send = (method, params = {}) => new Promise((res) => { const i = ++mid; waiting[i] = res; ws.send(JSON.stringify({ id: i, method, params })); });
const ev = async (expression) => (await send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true })).result?.result?.value;
const until = async (label, pred, ms = 15000) => {
  const t0 = Date.now();
  for (;;) {
    if (await ev(`(() => { try { return !!(${pred}); } catch { return false; } })()`)) return;
    if (Date.now() - t0 > ms) throw new Error(`wait ${label} timed out`);
    await sleep(100);
  }
};
const quiet = async (ms = 12000) => {
  const t0 = Date.now(); let still = 0;
  for (;;) {
    still = (await ev('window.__pending === 0 ? 1 : 0')) ? still + 1 : 0;
    if (still >= 3) return;
    if (Date.now() - t0 > ms) return;
    await sleep(100);
  }
};

await send('Page.enable'); await send('Runtime.enable');
await send('Page.addScriptToEvaluateOnNewDocument', { source: `
  window.__pending = 0;
  const realFetch = window.fetch;
  window.fetch = (...a) => { window.__pending++; return realFetch(...a).finally(() => { window.__pending--; }); };
` });

const shot = async (name, { w = 1440, h = 900, theme = 'light', scroll = 0, hash = '#/overview' } = {}) => {
  await send('Emulation.setDeviceMetricsOverride', { width: w, height: h, deviceScaleFactor: 2, mobile: w < 500 });
  await send('Page.navigate', { url: base + '/#/overview' });
  await until('boot', `document.querySelector('#view-overview h1')`);
  await ev(`localStorage.setItem('cl.theme', ${JSON.stringify(theme)}); applyTheme(${JSON.stringify(theme)}); 'ok'`);
  await send('Page.navigate', { url: base + hash });
  await until('view ' + hash, `location.hash === ${JSON.stringify(hash)} && document.querySelector('.view:not(.hidden) h1')`);
  await quiet();
  if (scroll) { await ev(`window.scrollTo(0, ${scroll}); 'ok'`); await sleep(350); }
  const img = await send('Page.captureScreenshot', { format: 'png' });
  const buf = Buffer.from(img.result.data, 'base64');
  fs.writeFileSync(path.join(OUT, name), buf);
  fs.writeFileSync(path.join(REVIEW, `${w}-${theme}-${name}`), buf);
  console.log('wrote', name, (buf.length / 1024).toFixed(0) + 'KB', `${w}x${h}`, theme, `sticky=${await ev(`(() => { const s = document.querySelector('#ws-sticky'); return s ? (s.classList.contains('hidden') ? 'hidden' : 'shown') : 'none'; })()`)}`);
};

await shot('project-overview.png', { hash: '#/overview' });
await shot('review-workspace.png', { hash: `#/component/${target.id}`, scroll: 585 });
await shot('synchronization-history.png', { hash: '#/history' });

// extra review copies (not committed) — small + dark variants for eyeballing
await shot('_review-workspace-dark.png', { hash: `#/component/${target.id}`, scroll: 585, theme: 'dark' });
await shot('_review-queue.png', { hash: '#/queue' });
await shot('_review-overview-390.png', { w: 390, h: 844, hash: '#/overview' });
await shot('_review-workspace-390.png', { w: 390, h: 844, hash: `#/component/${target.id}`, theme: 'dark', scroll: 300 });

ws.close();
chrome.kill();
shutdown();
console.log('done. committed ->', OUT, '\nreview copies ->', REVIEW);
