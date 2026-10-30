// Manual behavioural check of the workspace sticky bar, the diff toggle, the keyboard shortcuts
// and the queue hash filters, driven against the running demo. It prints observations and
// asserts nothing: the CI gate is tests/web_smoke.mjs, this is the tool a human reads after a
// UI change to see what the page actually did.
// Run: CL_CHROME=/usr/bin/chromium-browser node tools/ui_check.mjs
import { spawn } from 'node:child_process';
import net from 'node:net'; import os from 'node:os'; import fs from 'node:fs'; import path from 'node:path';
import { setTimeout as sleep } from 'node:timers/promises';
const CHROME = process.env.CL_CHROME || 'chromium';
const PY = process.env.CL_PYTHON || 'python3';
const probePort = () => new Promise(r => { const s = net.createServer().listen(0, '127.0.0.1', () => { const p = s.address().port; s.close(() => r(p)); }); });
const dirs = []; const mk = (p) => { const d = fs.mkdtempSync(path.join(os.tmpdir(), p)); dirs.push(d); return d; };
const port = await probePort(); const base = `http://127.0.0.1:${port}`;
const server = spawn(PY, ['-m', 'clreview.server', '--demo', '--port', String(port), '--db', path.join(mk('chk-'), 'd.db')], { stdio: 'ignore' });
const flags = (cdp, prof) => ['--headless=new', `--user-data-dir=${prof}`, `--remote-debugging-port=${cdp}`, '--no-sandbox', '--disable-gpu', '--no-first-run', '--no-default-browser-check', '--disable-dev-shm-usage', 'about:blank'];
const cdp = await probePort(); const prof = mk('chk-chrome-');
const chrome = spawn(CHROME, flags(cdp, prof), { stdio: 'ignore' });
const shutdown = () => { [server, chrome].forEach(p => { try { p.kill() } catch { } }); dirs.forEach(d => { try { fs.rmSync(d, { recursive: true, force: true }) } catch { } }) };
process.on('exit', shutdown);
let up = false; for (let i = 0; i < 80 && !up; i++) { try { up = (await fetch(base + '/api/health')).ok } catch { await sleep(250) } }
if (!up) throw new Error('server down');
const api = async (m, p, b) => (await fetch(base + p, { method: m, headers: { 'Content-Type': 'application/json', 'X-Reviewer': 'Checker' }, body: b ? JSON.stringify(b) : undefined })).json();
const map = (await api('GET', '/api/mappings')).items[0].id;
const comps = (await api('GET', `/api/mappings/${map}/components?limit=200`)).items;
let target = null;
for (const c of comps) {
  if (c.status === 'unmatched') continue;
  const d = await api('GET', `/api/components/${c.id}`);
  const lt = d.comparison.license_text;
  if (lt && lt.pl_current && lt.preferred && lt.pl_current.text !== lt.preferred.text) { target = c; break; }
}
console.log('target component:', target ? `${target.pl_name}/${target.sl_name} #${target.id}` : 'NONE');
const list = (await (await fetch(`http://127.0.0.1:${cdp}/json/list`)).json()).find(x => x.type === 'page' && x.webSocketDebuggerUrl);
if (!list) throw new Error('no page target');
const ws = new WebSocket(list.webSocketDebuggerUrl);
await new Promise((r, j) => { ws.onopen = r; ws.onerror = j });
let mid = 0; const w = {}; const errs = [];
ws.onmessage = e => { const o = JSON.parse(e.data); if (o.id && w[o.id]) { w[o.id](o); delete w[o.id] } if (o.method === 'Runtime.exceptionThrown') errs.push('exc: ' + JSON.stringify(o.params.exceptionDetails).slice(0, 200)); if (o.method === 'Runtime.consoleAPICalled' && o.params.type === 'error') errs.push('console: ' + (o.params.args || []).map(a => a.value || a.description || '').join(' ').slice(0, 160)) };
const send = (m, p = {}) => new Promise(r => { const i = ++mid; w[i] = r; ws.send(JSON.stringify({ id: i, method: m, params: p })) });
const ev = async x => (await send('Runtime.evaluate', { expression: x, awaitPromise: true, returnByValue: true })).result?.result?.value;
const until = async (lbl, pred, ms = 12000) => { const s = Date.now(); for (; ;) { if (await ev(`(()=>{try{return !!(${pred})}catch(e){return false}})()`)) return true; if (Date.now() - s > ms) { console.log('TIMEOUT', lbl); return false } await sleep(100) } };
const quiet = async () => { let st = 0; for (; ;) { st = (await ev('window.__pending===0?1:0')) ? st + 1 : 0; if (st >= 3) return; await sleep(100) } };
await send('Page.enable'); await send('Runtime.enable');
await send('Page.addScriptToEvaluateOnNewDocument', { source: 'window.__pending=0;const f=window.fetch;window.fetch=(...a)=>{window.__pending++;return f(...a).finally(()=>{window.__pending--})}' });
await send('Emulation.setDeviceMetricsOverride', { width: 1440, height: 1000, deviceScaleFactor: 1, mobile: false });
const go = async (h, ready) => { await send('Page.navigate', { url: base + h }); await until('nav ' + h, `location.hash===${JSON.stringify(h.slice(1))} && ${ready || "document.querySelector('.view:not(.hidden) h1')"}`); await quiet(); };
const key = async (k, code, text) => { await send('Input.dispatchKeyEvent', { type: 'keyDown', key: k, code, text }); await send('Input.dispatchKeyEvent', { type: 'keyUp', key: k, code }); await sleep(160); };

console.log('\n=== workspace diff + sticky ===');
await go(`/#/component/${target.id}`, `document.getElementById('ws-sticky')`);
console.log('marks d-del:', await ev(`document.querySelectorAll('mark.d-del').length`));
console.log('marks d-add:', await ev(`document.querySelectorAll('mark.d-add').length`));
console.log('sticky hidden at top (h1 visible):', await ev(`document.getElementById('ws-sticky').classList.contains('hidden')`));
console.log('no h1 inside sticky:', await ev(`document.getElementById('ws-sticky').querySelectorAll('h1').length`));
console.log('one h1 in view:', await ev(`document.querySelectorAll('.view:not(.hidden) h1').length`));
console.log('progress text:', await ev(`(document.querySelector('.ws-count')||{}).textContent`));
console.log('diff toggle label:', await ev(`(document.getElementById('diff-toggle')||{}).textContent`));
await ev(`window.scrollTo(0, 800); 1`); await sleep(450);
console.log('sticky revealed after scroll:', await ev(`!document.getElementById('ws-sticky').classList.contains('hidden')`));
console.log('sticky top in viewport:', await ev(`Math.round(document.getElementById('ws-sticky').getBoundingClientRect().top)`));
await ev(`document.getElementById('diff-toggle').click(); 1`); await sleep(160);
console.log('toggle aria-checked now:', await ev(`document.getElementById('diff-toggle').getAttribute('aria-checked')`));
console.log('view diff-off:', await ev(`document.getElementById('view-workspace').classList.contains('diff-off')`));

console.log('\n=== keyboard ===');
await ev(`document.activeElement.blur(); 1`);
await key('n', 'KeyN');
console.log('n -> activeElement:', await ev(`document.activeElement.id || document.activeElement.tagName`));
await ev(`document.activeElement.blur(); 1`);
await key('j', 'KeyJ');
console.log('j -> kbd-cursor count:', await ev(`document.querySelectorAll('.kbd-cursor').length`));
console.log('cursor row has decision buttons:', await ev(`!!(document.querySelector('.kbd-cursor')||{querySelector:()=>null}).querySelector('[data-dec]')`));
await ev(`document.activeElement.blur(); 1`);
await key('?', 'Slash', '?');
console.log('? -> shortcuts modal:', await ev(`!!document.getElementById('sc-title')`));
await key('Escape', 'Escape');
console.log('Esc closes shortcuts:', await ev(`!document.getElementById('sc-title')`));
await ev(`document.getElementById('note-body').focus(); 1`);
await key('j', 'KeyJ', 'j');
console.log('typing in note does not move cursor:', await ev(`document.querySelectorAll('.kbd-cursor').length === 1 && document.activeElement.id === 'note-body'`));

console.log('\n=== queue hash filters + polish ===');
await go('/#/queue?status=suggested', `document.getElementById('q-search')`);
console.log('q-status reflects hash:', await ev(`document.getElementById('q-status').value`));
console.log('clear enabled:', await ev(`!document.getElementById('q-clear').disabled`));
console.log('all rows suggested:', await ev(`[...document.querySelectorAll('#queue-list tbody tr')].every(r=>r.textContent.includes('suggested'))`));
await go('/#/queue?review_status=approved_for_sync');
console.log('q-review reflects hash:', await ev(`document.getElementById('q-review').value`));
await go('/#/queue?bogus=1&status=nope', `document.getElementById('q-search')`);
console.log('unknown/bogus ignored (status empty):', await ev(`document.getElementById('q-status').value === ''`));
await go('/#/queue?status=unmatched');
console.log('no "unmatched unmatched":', await ev(`![...document.querySelectorAll('td[data-label=match]')].some(td=>/unmatched\\s+unmatched/i.test(td.textContent))`));
console.log('unmatched rows show em-dash confidence:', await ev(`[...document.querySelectorAll('td[data-label=confidence]')].every(td=>td.textContent.trim()==='—')`));
await go('/#/queue');
await ev(`document.getElementById('q-clear').click(); 1`); await sleep(400); await quiet();
console.log('after clear: filters empty:', await ev(`CL.state.queue.status === '' && CL.state.queue.review_status === ''`));
console.log('confidence meters on full queue:', await ev(`document.querySelectorAll('.conf-meter').length`));
console.log('meter classes:', await ev(`[...document.querySelectorAll('.conf-meter i')].map(i=>i.className).join(',')`));
console.log('rows on full queue:', await ev(`document.querySelectorAll('#queue-list tbody tr').length`));
await ev(`(()=>{const i=document.getElementById('q-search'); i.value='libpng'; i.dispatchEvent(new Event('input',{bubbles:true}));})()`);
await sleep(700); await quiet();
console.log('live debounce applied:', await ev(`!document.getElementById('q-clear').disabled`));
console.log('live search rows:', await ev(`document.querySelectorAll('#queue-list tbody tr').length`));

console.log('\n=== overview + chrome ===');
await go('/#/overview');
console.log('stat links:', await ev(`[...document.querySelectorAll('a.stat')].map(a=>a.getAttribute('href')).join(' | ')`));
console.log('plain stat divs:', await ev(`document.querySelectorAll('div.stat').length`));
console.log('nav icons:', await ev(`document.querySelectorAll('.nav-ico').length`));
console.log('theme toggle svgs:', await ev(`document.querySelectorAll('#theme-toggle svg').length`));
console.log('refresh spinner node:', await ev(`!!document.querySelector('#refresh-btn .btn-spin')`));
console.log('\nERRORS:', errs.length ? errs.slice(0, 6) : 'none');
ws.close(); shutdown();
