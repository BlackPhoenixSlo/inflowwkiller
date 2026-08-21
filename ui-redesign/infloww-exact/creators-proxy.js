/* ==== LIVE WIRING — creators-proxy (proxy registry + creator egress map) ====
 *  GET  /admin/proxies                     → registry + every creator, with bindings
 *  GET  /admin/rev/drift                   → session freshness per creator
 *  POST /admin/proxies                     → register
 *  POST /admin/proxies/{label}/test        → live egress-IP probe
 *  POST /admin/proxies/assign  ·  …/unbind → bind / unbind a creator
 *  DELETE /admin/proxies/{label}           → remove
 * The registry is legitimately empty on this relay; the page says so and then
 * shows what IS true — every creator's actual egress path.
 */
Fastt.ready(async function () {
  var esc = Fastt.esc;
  var body = document.getElementById('px-body');
  var pxTable = document.getElementById('px-table');
  var head = pxTable.querySelector('.et-head');
  var GRID = head.getAttribute('style') || '';
  var state = { proxies: [], accounts: [], drift: {} };

  Fastt.liveBadge(document.getElementById('px-title'));
  Fastt.liveBadge(document.getElementById('px-egress-t'));

  function proxyFor(id) {
    for (var i = 0; i < state.proxies.length; i++) {
      var ids = (state.proxies[i].assigned_accounts || []).map(function (a) { return String(a.id); });
      if (ids.indexOf(String(id)) !== -1) return state.proxies[i];
    }
    return null;
  }

  function sessionCell(a) {
    var d = state.drift[String(a.id)];
    if (!a.has_session) return '<span style="color:#f0483e">no session captured</span>';
    if (!d) return '<span style="color:#8a8a8a">captured · freshness unknown</span>';
    if (d.stale) return '<span style="color:#f0aa5a">stale — rev ' + esc(d.session_rev || '?') + '</span>';
    return '<span style="color:#3ec46a">current — rev ' + esc(d.session_rev || '?') + '</span>';
  }

  function renderStats() {
    var bound = state.accounts.filter(function (a) { return proxyFor(a.id); }).length;
    var shared = state.proxies.filter(function (p) { return (p.assigned_accounts || []).length > 1; }).length;
    var verified = state.proxies.filter(function (p) { return p.verified_ip; }).length;
    document.getElementById('px-stats').innerHTML =
      '<div class="pxstat"><div class="v">' + state.proxies.length + '</div><div class="l">Proxies registered</div></div>' +
      '<div class="pxstat"><div class="v">' + verified + '</div><div class="l">Verified egress IP</div></div>' +
      '<div class="pxstat"><div class="v">' + bound + '</div><div class="l">Creators behind a proxy</div></div>' +
      '<div class="pxstat"><div class="v' + (state.accounts.length - bound ? ' warn' : '') + '">' +
        (state.accounts.length - bound) + '</div><div class="l">Creators on direct egress</div></div>' +
      '<div class="pxstat"><div class="v' + (shared ? ' warn' : '') + '">' + shared + '</div>' +
        '<div class="l">Proxies shared by 2+ creators</div></div>';
  }

  function proxyRowHtml(p) {
    var boundList = p.assigned_accounts || [];
    var boundIds = {};
    boundList.forEach(function (a) { boundIds[String(a.id)] = 1; });
    var chips = boundList.map(function (a) {
      var meta = state.accounts.find(function (x) { return String(x.id) === String(a.id); }) || a;
      var color = meta.color || a.color || '#666';
      return '<span class="pxchip"><span style="width:8px;height:8px;border-radius:50%;background:' + esc(color) + '"></span>' +
        esc(a.nickname || a.id) +
        '<span class="x" data-unbind="' + esc(a.id) + '" title="Unbind ' + esc(a.nickname || a.id) + '">×</span></span>';
    }).join('');
    var unbound = state.accounts.filter(function (a) { return !boundIds[String(a.id)]; });
    var dd = unbound.length
      ? '<select class="pxf-inline-dd" data-addcre><option value="">' +
          (boundList.length ? '+ add creator' : '+ assign creator') + '</option>' +
          unbound.map(function (a) { return '<option value="' + esc(a.id) + '">' + esc(a.nickname || a.id) + '</option>'; }).join('') +
        '</select>'
      : '';
    var warn = boundList.length > 1
      ? '<span class="pxwarn" title="OF can correlate accounts that egress from the same IP — only share a proxy for accounts you intend to link.">⚠ shared</span>'
      : '';
    var creatorCell = '<div style="display:flex;flex-wrap:wrap;gap:6px;align-items:center">' +
      chips + warn + dd +
      (boundList.length || dd ? '' : '<span style="color:#6a6a6a">unbound</span>') + '</div>';
    var status = p.verified_ip
      ? 'IP ' + esc(p.verified_ip) + (p.verified_at ? ' · ' + esc(Fastt.fmtAgo(p.verified_at)) : '')
      : '<span style="color:#8a8a8a">unverified — run Test</span>';
    return '<div class="et-row" data-label="' + esc(p.label) + '" style="' + GRID +
        ';display:grid;align-items:center;padding:0 30px;min-height:64px;border-top:1px solid #232323;font-size:14.5px;color:#d0d0d0;background:#161616">' +
      '<div style="color:#fff;font-weight:600">' + esc(p.label) + '</div>' +
      '<div>' + esc(p.host) + '</div>' +
      '<div>' + esc(p.port) + '</div>' +
      '<div>' + esc((p.scheme || 'http').toUpperCase()) + '</div>' +
      '<div>' + creatorCell + '</div>' +
      '<div>' + status + '</div>' +
      '<div style="display:flex;gap:14px;color:#6d8bfb;font-weight:500">' +
        '<span data-act="test" style="cursor:pointer">Test</span>' +
        '<span data-act="del" style="cursor:pointer;color:#8a8a8a">Delete</span>' +
      '</div></div>';
  }

  function egressRowHtml(a) {
    var p = proxyFor(a.id);
    return '<div class="et-row" data-acct="' + esc(a.id) + '" style="grid-template-columns:1.4fr 1.1fr 1.3fr 1.5fr 1fr' +
        ';display:grid;align-items:center;padding:0 30px;min-height:62px;border-top:1px solid #232323;font-size:14.5px;color:#d0d0d0;background:#161616">' +
      '<div style="display:flex;align-items:center;gap:10px;color:#fff">' +
        '<span style="width:26px;height:26px;border-radius:50%;display:flex;align-items:center;justify-content:center;' +
        'font-size:12px;font-weight:700;background:' + esc(a.color || '#3a3550') + '">' +
        esc(String(a.nickname || a.id).slice(0, 1).toUpperCase()) + '</span>' + esc(a.nickname || a.id) + '</div>' +
      '<div>' + esc(a.id) + '</div>' +
      '<div>' + (p
        ? '<span style="color:#67d1ae">' + esc(p.label) + '</span> <span style="color:#7a7a7a">' + esc(p.host) + ':' + esc(p.port) + '</span>'
        : '<span style="color:#8a8a8a">direct — relay’s own IP</span>') + '</div>' +
      '<div>' + sessionCell(a) + '</div>' +
      '<div style="color:#6d8bfb;font-weight:500">' + (
        p ? '<span data-eact="unbind" style="cursor:pointer">Unbind</span>'
          : state.proxies.length
            ? '<select class="pxf-inline-dd" data-bindpx><option value="">Bind proxy…</option>' +
                state.proxies.map(function (px) {
                  return '<option value="' + esc(px.label) + '">' + esc(px.label) + ' — ' + esc(px.host) + ':' + esc(px.port) + '</option>';
                }).join('') + '</select>'
            : '<span style="color:#6a6a6a" title="Register a proxy above first">no proxies yet</span>'
      ) + '</div></div>';
  }

  function renderAll() {
    pxTable.querySelectorAll('.et-row').forEach(function (r) { r.remove(); });
    if (!state.proxies.length) {
      body.style.display = '';
      document.getElementById('px-cap').textContent = 'No custom proxies registered';
      document.getElementById('px-cap2').innerHTML =
        'Every creator below therefore egresses straight from this relay’s own IP. ' +
        'That is a real configuration, not a missing read — add one above to route a creator through it.';
    } else {
      body.style.display = 'none';
      pxTable.insertAdjacentHTML('beforeend', state.proxies.map(proxyRowHtml).join(''));
    }
    var eg = document.getElementById('px-egress-body');
    eg.innerHTML = state.accounts.length
      ? state.accounts.map(egressRowHtml).join('')
      : '<div style="background:#161616;padding:40px;text-align:center;color:#8a8a8a">' +
        'No creators on this relay yet — a creator appears once its session is captured.</div>';
    renderStats();
  }

  async function load() {
    var out = await Fastt.get('/admin/proxies');
    state.proxies = out.proxies || [];
    state.accounts = out.accounts || [];
    renderAll();
    try {
      var drift = await Fastt.get('/admin/rev/drift');
      state.drift = {};
      (drift.accounts || []).forEach(function (r) { state.drift[String(r.account_id)] = r; });
      renderAll();
    } catch (e) { console.warn('rev/drift unavailable', e); }
  }

  // ── inline add-proxy form (replaces the old prompt() chain) ──
  var form = document.getElementById('px-form');
  var addBtn = document.getElementById('px-add');
  function setFormErr(m) {
    var el = document.getElementById('pf-err');
    el.textContent = m || '';
  }
  function openForm(open) {
    form.classList.toggle('open', open);
    addBtn.textContent = open ? 'Close form' : '+ Add new proxy';
    if (open) { setFormErr(''); setTimeout(function () { document.getElementById('pf-label').focus(); }, 0); }
  }
  addBtn.addEventListener('click', function () { openForm(!form.classList.contains('open')); });
  document.getElementById('pf-cancel').addEventListener('click', function () { openForm(false); });
  document.getElementById('pf-adv-t').addEventListener('click', function () {
    this.classList.toggle('open');
    document.getElementById('pf-adv').classList.toggle('open', this.classList.contains('open'));
  });
  document.getElementById('pf-port').addEventListener('input', function () {
    this.value = this.value.replace(/[^0-9]/g, '');
  });

  async function saveProxy() {
    setFormErr('');
    var label = document.getElementById('pf-label').value.trim();
    var host = document.getElementById('pf-host').value.trim();
    var scheme = document.getElementById('pf-scheme').value;
    var port = parseInt(document.getElementById('pf-port').value, 10);
    if (!label) { setFormErr('Label is required'); document.getElementById('pf-label').focus(); return; }
    if (!host) { setFormErr('Host is required'); document.getElementById('pf-host').focus(); return; }
    if (!port || port < 1 || port > 65535) { setFormErr('Port must be 1–65535'); document.getElementById('pf-port').focus(); return; }
    if (state.proxies.some(function (p) { return p.label === label; })) {
      setFormErr('A proxy named "' + label + '" already exists'); return;
    }
    var username = document.getElementById('pf-user').value.trim() || null;
    var password = username ? (document.getElementById('pf-pass').value || null) : null;
    var notes = document.getElementById('pf-notes').value.trim();
    if (!confirm('Register proxy "' + label + '" → ' + host + ':' + port + ' (' + scheme + ')?')) return;
    var btn = document.getElementById('pf-save');
    btn.disabled = true; btn.textContent = 'Saving…';
    try {
      await Fastt.post('/admin/proxies', {
        label: label, host: host, port: port,
        scheme: scheme, username: username, password: password, notes: notes,
      });
      Fastt.saved('Proxy saved ✓');
      ['pf-label', 'pf-host', 'pf-port', 'pf-user', 'pf-pass', 'pf-notes'].forEach(function (id) {
        document.getElementById(id).value = '';
      });
      openForm(false);
      await load();
    } catch (e) { setFormErr((e && e.body && (e.body.detail || e.body.error)) || 'Save failed'); Fastt.oops(e); }
    finally { btn.disabled = false; btn.textContent = 'Save proxy'; }
  }
  document.getElementById('pf-save').addEventListener('click', saveProxy);
  form.addEventListener('keydown', function (e) {
    if (e.key === 'Enter' && e.target.tagName === 'INPUT') { e.preventDefault(); saveProxy(); }
  });

  pxTable.addEventListener('click', async function (e) {
    // chip × — unbind one account from this proxy
    var un = e.target.closest('[data-unbind]');
    if (un) {
      var uLabel = un.closest('.et-row').dataset.label;
      var uId = un.dataset.unbind;
      var uA = state.accounts.find(function (x) { return String(x.id) === String(uId); });
      var uName = uA ? (uA.nickname || uA.id) : uId;
      if (!confirm('Unbind ' + uName + ' from "' + uLabel + '"?\n\nIt falls back to the relay’s own IP.')) return;
      try {
        await Fastt.post('/admin/proxies/' + encodeURIComponent(uLabel) + '/unbind', { account_id: String(uId) });
        Fastt.saved('Unbound ✓');
        await load();
      } catch (err) { Fastt.oops(err); }
      return;
    }
    var act = e.target.closest('[data-act]');
    if (!act) return;
    var label = act.closest('.et-row').dataset.label;
    if (act.dataset.act === 'test') {
      act.textContent = 'Testing…';
      try {
        var r = await Fastt.post('/admin/proxies/' + encodeURIComponent(label) + '/test');
        Fastt.toast(r.ok ? ('Egress IP ' + r.ip + (r.geo ? ' · ' + r.geo : '')) : ('Probe failed: ' + (r.error || 'unknown')), r.ok ? 'ok' : 'err');
      } catch (err) { Fastt.oops(err); }
      act.textContent = 'Test';
      await load();
    } else if (act.dataset.act === 'del') {
      var delP = state.proxies.find(function (p) { return p.label === label; });
      var n = delP ? (delP.assigned_accounts || []).length : 0;
      var ext = n ? ' ' + n + ' creator' + (n === 1 ? '' : 's') + ' fall back to direct egress until reassigned.' : '';
      if (!confirm('Delete proxy "' + label + '"?' + ext)) return;
      try {
        await Fastt.del('/admin/proxies/' + encodeURIComponent(label));
        Fastt.saved('Deleted');
        await load();
      } catch (err) { Fastt.oops(err); }
    }
  });

  // proxy-row inline "+ add creator" dropdown → bind that account
  pxTable.addEventListener('change', async function (e) {
    var dd = e.target.closest('[data-addcre]');
    if (!dd) return;
    var label = dd.closest('.et-row').dataset.label;
    var aid = dd.value;
    if (!aid) return;
    var a2 = state.accounts.find(function (x) { return String(x.id) === String(aid); });
    var name = a2 ? (a2.nickname || a2.id) : aid;
    if (!confirm('Route ' + name + ' through proxy "' + label + '"?')) { dd.value = ''; return; }
    try {
      await Fastt.post('/admin/proxies/assign', { label: label, account_id: String(aid) });
      Fastt.saved('Assigned ✓');
      await load();
    } catch (err) { dd.value = ''; Fastt.oops(err); }
  });

  var egressEl = document.getElementById('px-egress');
  egressEl.addEventListener('click', async function (e) {
    var act = e.target.closest('[data-eact]');
    if (!act || act.dataset.eact !== 'unbind') return;
    var id = act.closest('.et-row').dataset.acct;
    var a = state.accounts.find(function (x) { return String(x.id) === String(id); });
    if (!a) return;
    var cur = proxyFor(a.id);
    if (!cur) return;
    if (!confirm('Unbind ' + (a.nickname || a.id) + ' from "' + cur.label + '"?\n\nIt falls back to the relay’s own IP.')) return;
    try {
      await Fastt.post('/admin/proxies/' + encodeURIComponent(cur.label) + '/unbind', { account_id: String(a.id) });
      Fastt.saved('Unbound ✓');
      await load();
    } catch (err) { Fastt.oops(err); }
  });
  // egress-row inline "Bind proxy…" dropdown → route this creator through it
  egressEl.addEventListener('change', async function (e) {
    var dd = e.target.closest('[data-bindpx]');
    if (!dd) return;
    var id = dd.closest('.et-row').dataset.acct;
    var label = dd.value;
    if (!label) return;
    var a = state.accounts.find(function (x) { return String(x.id) === String(id); });
    var name = a ? (a.nickname || a.id) : id;
    if (!confirm('Route ' + name + ' through proxy "' + label + '"?')) { dd.value = ''; return; }
    try {
      await Fastt.post('/admin/proxies/assign', { label: label, account_id: String(id) });
      Fastt.saved('Bound ✓');
      await load();
    } catch (err) { dd.value = ''; Fastt.oops(err); }
  });

  try {
    await load();
  } catch (e) {
    document.getElementById('px-cap').textContent = 'Couldn’t load proxies — relay error';
    document.getElementById('px-cap2').textContent = 'This is a failed read, not an empty registry.';
    document.getElementById('px-egress-body').innerHTML =
      '<div style="background:#161616;padding:40px;text-align:center;color:#e05b5b">Creator egress unavailable</div>';
    throw e;
  }
});
