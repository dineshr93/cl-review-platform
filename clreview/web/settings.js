'use strict';
window.CL = window.CL || {};
var CL = window.CL;

// Settings & health: connection versions/permissions, the editable license map,
// and the remembered false-positive rules.
CL.settingsBody = (health, maps, rules) => {
  const caps = (c) => {
    const x = c.capabilities || {};
    if (x.error) return CL.esc(x.error);
    return ['write_license_names', 'write_license_text', 'write_copyrights',
            'write_review_status', 'write_upload_status', 'verify_writes']
      .map(k => `${k}: ${x[k] ? 'yes' : 'no'}`).join(' · ');
  };
  return `<h1>Settings &amp; health</h1>
    <div class="panel"><h2>Service health</h2>
      <div class="kv">
        <dt>app version</dt><dd>${CL.esc(health.version)}</dd>
        <dt>database</dt><dd>${CL.esc(health.db)}</dd>
        <dt>read-only</dt><dd>${health.read_only ? 'yes' : 'no'}</dd>
        <dt>time</dt><dd class="small num">${CL.esc(health.time)}</dd>
      </div>
      <table class="mt-2"><thead><tr><th>id</th><th>role</th><th>tool</th><th>server</th><th>API version</th><th>test</th><th>permissions</th></tr></thead><tbody>
        ${health.connections.length ? health.connections.map(c => `<tr>
          <td class="num">${c.id}</td><td>${CL.badge(c.role, 'accent')}</td><td>${CL.esc(c.tool)}</td>
          <td class="small">${CL.esc(c.base_url || '—')}</td><td class="small">${CL.esc(c.api_version || '—')}</td>
          <td class="small">${CL.badge(c.last_test_ok ? 'ok' : (c.last_test_at ? 'failed' : 'untested'), c.last_test_ok ? 'ok' : (c.last_test_at ? 'bad' : 'warn'))}</td>
          <td class="small">${caps(c)}</td></tr>`).join('') : '<tr><td colspan="7" class="muted">No connections configured.</td></tr>'}
      </tbody></table>
    </div>
    <div class="panel"><h2>License map (FO short name → SPDX → BD license)</h2>
      <p class="small muted">Editable local mapping. Approval of an SL license writes the mapped BD license identifier; source-file text is still taken from SL evidence.</p>
      <table><thead><tr><th>FO short name</th><th>SPDX</th><th>BD license</th><th>source</th><th>updated</th><th>action</th></tr></thead><tbody>
        ${maps.items.map(r => `<tr><td>${CL.esc(r.short_name)}</td><td class="small">${CL.esc(r.spdx || '—')}</td>
          <td>${CL.esc(r.bd_license)}</td><td class="small">${CL.esc(r.source)}</td><td class="small num">${CL.esc(r.updated_at)}</td>
          <td><button class="tiny danger-ghost" data-lm-delete="${r.id}">Delete</button></td></tr>`).join('') || '<tr><td colspan="6" class="muted">No mappings.</td></tr>'}
      </tbody></table>
      <div class="field-grid mt-2" id="license-map-form">
        <label>FO short name<input id="lm-short" placeholder="MIT"></label>
        <label>SPDX<input id="lm-spdx" placeholder="MIT"></label>
        <label>BD license<input id="lm-bd" placeholder="MIT"></label>
        <button id="lm-save" class="primary">Save mapping</button>
      </div>
    </div>
    <div class="panel"><h2>Remembered false positives</h2>
      <p class="small muted">A rule ignores a license finding for this component, this version, or globally. Revoke a rule to re-surface the finding.</p>
      <table><thead><tr><th>license</th><th>scope</th><th>path pattern</th><th>created by</th><th>created</th><th>action</th></tr></thead><tbody>
        ${rules.items.map(r => `<tr><td>${CL.esc(r.license_name)}</td><td>${CL.badge(r.scope, 'accent')}</td>
          <td class="small">${CL.esc(r.path_pattern || '*')}</td><td>${CL.esc(r.created_by)}</td>
          <td class="small num">${CL.esc(r.created_at)}</td>
          <td><button class="tiny" data-rule-revoke="${r.id}">Revoke</button></td></tr>`).join('') || '<tr><td colspan="6" class="muted">No rules yet.</td></tr>'}
      </tbody></table>
    </div>`;
};

CL.renderSettings = async (fresh) => {
  const el = document.getElementById('view-settings');
  const [health, maps, rules] = await Promise.all([
    CL.api('GET', '/api/health'),
    CL.api('GET', '/api/license-map'),
    CL.api('GET', '/api/false-positive-rules'),
  ]);
  if (fresh && !fresh()) return;
  el.innerHTML = CL.settingsBody(health, maps, rules);
  CL.bindSettings();
};

CL.bindSettings = () => {
  document.getElementById('lm-save').onclick = () => CL.click(async () => {
    const short = document.getElementById('lm-short').value.trim();
    const spdx = document.getElementById('lm-spdx').value.trim();
    const bd = document.getElementById('lm-bd').value.trim();
    if (!short || !bd) { CL.toast('FO short name and BD license are required', true); return; }
    await CL.api('POST', '/api/license-map', { short_name: short, spdx, bd_license: bd });
    CL.toast('license mapping saved');
    await CL.renderSettings();
  });
  document.querySelectorAll('[data-lm-delete]').forEach(b => b.onclick = () => CL.click(async () => {
    if (!await CL.confirm('Delete this license mapping?', { title: 'Delete mapping', confirmText: 'Delete' })) return;
    await CL.api('DELETE', `/api/license-map/${b.dataset['lm-delete']}`);
    CL.toast('license mapping deleted');
    await CL.renderSettings();
  }));
  document.querySelectorAll('[data-rule-revoke]').forEach(b => b.onclick = () => CL.click(async () => {
    await CL.api('POST', `/api/false-positive-rules/${b.dataset['rule-revoke']}/revoke`, {});
    CL.toast('false-positive rule revoked');
    await CL.renderSettings();
  }));
};
