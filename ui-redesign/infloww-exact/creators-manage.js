/* ==== LIVE WIRING — creators-manage (accounts · proxy binding · session · employees) ====
 *  GET /admin/proxies                          → creators + proxy registry + bindings
 *  GET /admin/rev/drift                        → per-creator session freshness
 *  GET /admin/stats/per-employee?by_account=1  → which employees actually worked each creator
 *  PATCH/DELETE /admin/accounts/{id}           → rename / remove (explicit click + confirm)
 * Filters and paging are real client-side work over the live rows — nothing here is skin.
 */
Fastt.ready(async function () {
  var esc = Fastt.esc, fmtInt = Fastt.fmtInt, fmtCents = Fastt.fmtCents;
  var table = document.getElementById('cm-table');
  var totalEl = document.getElementById('cm-total');
  var state = { accounts: [], proxies: [], drift: {}, health: {}, session: {}, byAccount: {},
                page: 1, healthLoaded: false, sessionLoaded: false };
  var CURRENT = String(Fastt.account() || '');

  Fastt.liveBadge(document.getElementById('cm-title'));

  function proxyFor(id) {
    for (var i = 0; i < state.proxies.length; i++) {
      var ids = (state.proxies[i].assigned_accounts || []).map(function (a) { return String(a.id); });
      if (ids.indexOf(String(id)) !== -1) return state.proxies[i];
    }
    return null;
  }

  function sessionState(a) {
    var d = state.drift[String(a.id)];
    if (!d) return { key: a.has_session ? 'current' : 'none',
                     color: a.has_session ? '#8a8a8a' : '#f0483e',
                     label: a.has_session ? 'session captured (freshness unknown)' : 'no session' };
    if (!d.has_session) return { key: 'none', color: '#f0483e', label: 'no session' };
    if (d.stale) return { key: 'stale', color: '#f0aa5a', label: 'stale — captured rev ' + (d.session_rev || '?') };
    return { key: 'current', color: '#3ec46a', label: 'current — rev ' + (d.session_rev || '?') };
  }

  // Combined session-connection status (the badge asked for):
  //   GET /admin/session/status → is a session captured on disk (connected/none)
  //   GET /health               → does that session still authenticate (live → connected, fail → dead)
  // connected / dead / none, exactly. 'checking…' while the live probe is in flight.
  function connState(a) {
    var sess = state.session[String(a.id)];
    var hasFile = sess ? !!sess.loaded : !!a.has_session;   // session/status is authoritative; fall back to registry flag
    if (!hasFile) return { key: 'none', cls: 'none', label: 'no session', ofName: '' };
    var h = state.health[String(a.id)];
    if (!state.healthLoaded || !h) return { key: 'checking', cls: 'probe', label: 'checking…', ofName: '' };
    if (h.ok) return { key: 'connected', cls: 'conn', label: 'connected', ofName: h.name || '' };
    return { key: 'dead', cls: 'dead', label: 'session dead', ofName: h.name || '',
             err: h.error ? String(h.error).slice(0, 60) : '' };
  }

  // How the session was captured — read from session/status.profile_id.
  function methodLabel(pid) {
    var p = String(pid || '');
    if (!p) return '';
    if (p.indexOf('paste-curl') === 0) return 'cURL';
    if (p.indexOf('playwright-proxy') === 0) { var m = p.split(':')[1]; return m ? 'Proxy ' + m : 'Proxy'; }
    if (p.indexOf('incogniton') === 0) return 'Incogniton';
    return p.slice(0, 18);
  }

  // OF timestamps come back compact (YYYYMMDDTHHMMSSZ); reflow to ISO for fmtAgo.
  function capAgo(s) {
    var m = /^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z$/.exec(String(s || ''));
    var iso = m ? (m[1] + '-' + m[2] + '-' + m[3] + 'T' + m[4] + ':' + m[5] + ':' + m[6] + 'Z') : s;
    return Fastt.fmtAgo(iso);
  }

  function renderSummary() {
    var host = document.getElementById('cm-summary');
    var n = state.accounts.length;
    var live = 0, down = 0, stale = 0, noSess = 0;
    state.accounts.forEach(function (a) {
      var cs = connState(a), ss = sessionState(a);
      if (cs.key === 'none') { noSess++; return; }
      if (cs.key === 'connected') live++;
      else if (cs.key === 'dead') down++;
      if (ss.key === 'stale') stale++;
    });
    function chip(bg, col, ic, v, l) {
      return '<div class="sm-chip"><div class="sm-ic" style="background:' + bg + ';color:' + col + '">' + ic +
        '</div><div><div class="sm-v">' + v + '</div><div class="sm-l">' + l + '</div></div></div>';
    }
    var icUsers = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="9" cy="8" r="3"/><path d="M3.5 18c.6-3 2.6-4.5 5.5-4.5s4.9 1.5 5.5 4.5" stroke-linecap="round"/><path d="M16 5.5a2.6 2.6 0 0 1 0 5M19 18c-.3-1.9-1-3.3-2.2-4.2" stroke-linecap="round"/></svg>';
    var icLive = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M20 6L9 17l-5-5" stroke-linecap="round" stroke-linejoin="round"/></svg>';
    var icWarn = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 3l9 16H3z" stroke-linejoin="round"/><path d="M12 10v4M12 17v.01" stroke-linecap="round"/></svg>';
    var icDown = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><path d="M9 9l6 6M15 9l-6 6" stroke-linecap="round"/></svg>';
    var probing = !state.healthLoaded;
    host.innerHTML =
      chip('#20264a', '#8aa3ff', icUsers, n, 'Creators') +
      chip('rgba(62,196,106,.14)', '#5fd08a', icLive, probing ? '…' : live, 'Live on OnlyFans') +
      chip('rgba(240,170,90,.14)', '#f0aa5a', icWarn, stale, 'Stale session' + (stale === 1 ? '' : 's')) +
      ((down > 0 || noSess > 0)
        ? chip('rgba(240,72,62,.14)', '#f0685e', icDown, down + noSess, 'Need attention')
        : '');
  }

  function empsFor(id) {
    var list = state.byAccount[String(id)] || [];
    if (!list.length) return '<span style="color:#6a6a6a">no attributed sender</span>';
    var rev = list.reduce(function (s, r) { return s + r.revenue_cents; }, 0);
    var top = list[0];
    return esc(top.name) + ' <span style="color:#7a7a7a">' + fmtInt(top.messages_sent) + ' msgs</span>' +
      '<div style="font-size:12.5px;color:#8a8a8a;margin-top:3px">' +
        (list.length > 1 ? '+' + (list.length - 1) + ' more sender' + (list.length > 2 ? 's' : '') + ' · ' : '') +
        '<span style="color:#67d1ae">' + fmtCents(rev) + ' attributed</span></div>';
  }

  // Deterministic pleasant hue so a creator with no saved color still gets a
  // colored initials chip — never a flat grey block (was the #3a3550 default).
  var AV_HUES = ['#5b8def', '#e0679b', '#67d1ae', '#a78bfa', '#e5a35b', '#4fb0c6', '#e06b6b', '#7c8cf8'];
  function hueFor(seed) {
    var s = String(seed), h = 0;
    for (var i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
    return AV_HUES[h % AV_HUES.length];
  }

  function rowHtml(a) {
    var p = proxyFor(a.id);
    var s = sessionState(a);        // rev-drift freshness (drives the "stale rev" chip)
    var cs = connState(a);          // connected / dead / none / checking
    var sess = state.session[String(a.id)];
    var initial = (a.nickname || String(a.id)).slice(0, 1).toUpperCase();
    var isCur = String(a.id) === CURRENT;
    var col = a.color || hueFor(a.nickname || a.id);
    var dot = cs.cls === 'conn' ? '<span class="d"></span>' : '';
    // combined connected/dead/none pill + underneath: OF name · captured ago · cookies · capture method
    var statusCell =
      '<div class="cellof" title="' + esc(cs.err || cs.label) + '"><svg width="17" height="17" viewBox="0 0 28 28" fill="none"><circle cx="15" cy="14" r="7" stroke="#cfcfcf" stroke-width="3"/><circle cx="15" cy="14" r="2.3" fill="#cfcfcf"/><path d="M7.5 14H3" stroke="#cfcfcf" stroke-width="3" stroke-linecap="round"/></svg>' +
        '<span class="hz ' + cs.cls + '">' + dot + esc(cs.label) + '</span></div>';
    var bits = [];
    if (cs.ofName) bits.push(esc(cs.ofName));
    if (sess && sess.loaded) {
      if (sess.captured_at) bits.push('captured ' + esc(capAgo(sess.captured_at)));
      if (sess.cookies_count != null) bits.push(esc(sess.cookies_count) + ' cookies');
    }
    var methodTag = (sess && sess.loaded && sess.profile_id)
      ? '<span class="cm-method">' + esc(methodLabel(sess.profile_id)) + '</span>' : '';
    var staleTag = (s.key === 'stale')
      ? ' <span class="hz down" style="font-size:11px">stale rev</span>' : '';
    var sub = bits.join(' · ') + (methodTag ? (bits.length ? ' · ' : '') + methodTag : '') + staleTag;
    if (!sub) sub = '<span style="color:#6a6a6a">no captured session — Connect to add one</span>';
    var hasFile = sess ? !!sess.loaded : !!a.has_session;
    return '<div class="dt-row" data-id="' + esc(a.id) + '">' +
      '<div class="cellcreator"><span class="av" style="background:' + esc(col) +
        ';display:flex;align-items:center;justify-content:center;font-weight:600;color:#fff">' + esc(initial) + '</span>' +
        '<span>' + esc(a.nickname || a.id) + (isCur ? '<span class="curbadge">CURRENT</span>' : '') + '</span></div>' +
      '<div class="cm-nick">' + esc(a.nickname || 'unnamed') + '</div>' +
      '<div>' + (p ? esc(p.label) : '<span style="color:#6a6a6a">direct egress</span>') + '</div>' +
      '<div style="min-width:0">' + statusCell + '<div class="ofname">' + sub + '</div></div>' +
      '<div>' + esc(a.id) + '</div>' +
      '<div>' + empsFor(a.id) + '</div>' +
      '<div class="cellact adv">' +
        '<span class="ib' + (isCur ? ' cur' : '') + '" data-act="switch" title="' + (isCur ? 'This is the active creator' : 'Switch the whole app to this creator') + '"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M8 7h11l-3-3M16 17H5l3 3" stroke-linecap="round" stroke-linejoin="round"/></svg></span>' +
        (hasFile ? '<span class="ib reload" data-act="reload" title="Reload session from disk (hot-swap the OF client)"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9"><path d="M20 11a8 8 0 1 0-.5 4" stroke-linecap="round"/><path d="M20 5v5h-5" stroke-linecap="round" stroke-linejoin="round"/></svg></span>' : '') +
        '<span class="ib recap" data-act="recapture" title="Re-capture this creator’s OnlyFans session (paste a fresh cURL)"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M15 7a5 5 0 0 1 0 10h-1M9 17a5 5 0 0 1 0-10h1" stroke-linecap="round"/><path d="M8 12h8" stroke-linecap="round"/></svg></span>' +
        '<span class="swatch" data-act="recolor" title="Recolor creator"><input type="color" value="' + esc(/^#[0-9a-fA-F]{6}$/.test(col) ? col : '#3a3550') + '"><span style="width:16px;height:16px;border-radius:4px;background:' + esc(col) + ';pointer-events:none"></span></span>' +
        '<span class="ib" data-act="rename" title="Rename creator"><svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M4 20l1-4L16 5l3 3L8 19l-4 1z"/><path d="M14 7l3 3"/></svg></span>' +
        '<span class="ib" data-act="remove" title="Remove creator (permanent)"><svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M5 7h14M9 7V5h6v2M7 7l1 12h8l1-12" stroke-linecap="round" stroke-linejoin="round"/></svg></span>' +
      '</div></div>';
  }

  function val(id) { var e = document.getElementById(id); return e ? e.value : ''; }

  function filtered() {
    var q = (val('cm-filter') || '').trim().toLowerCase();
    var plat = val('cm-platform'), prox = val('cm-proxy'), conn = val('cm-conn');
    var emp = val('cm-emp'), status = val('cm-status');
    return state.accounts.filter(function (a) {
      if (q && String(a.nickname || '').toLowerCase().indexOf(q) === -1 && String(a.id).indexOf(q) === -1) return false;
      if (plat && plat !== 'onlyfans') return false;    // one platform exists; kept honest, not hidden
      if (prox) {
        var p = proxyFor(a.id);
        if (prox === '__none') { if (p) return false; }
        else if (!p || p.label !== prox) return false;
      }
      if (conn === 'live') { if (connState(a).key !== 'connected') return false; }
      else if (conn === 'down') { if (connState(a).key !== 'dead') return false; }
      else if (conn === 'none') { if (connState(a).key !== 'none') return false; }
      else if (conn && sessionState(a).key !== conn) return false;
      if (status === 'session' && !a.has_session) return false;
      if (status === 'nosession' && a.has_session) return false;
      if (emp) {
        var list = state.byAccount[String(a.id)] || [];
        if (!list.some(function (r) { return String(r.id) === emp; })) return false;
      }
      return true;
    });
  }

  function renderPager(total, per) {
    var pages = Math.max(1, Math.ceil(total / per));
    if (state.page > pages) state.page = pages;
    var host = document.getElementById('cm-pager');
    var nums = [];
    for (var i = 1; i <= pages; i++) {
      if (pages > 7 && i > 2 && i < pages - 1 && Math.abs(i - state.page) > 1) {
        if (nums[nums.length - 1] !== '…') nums.push('…');
        continue;
      }
      nums.push(i);
    }
    host.innerHTML =
      '<span class="pb" data-pg="prev" style="opacity:' + (state.page > 1 ? 1 : .35) + '"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M15 6l-6 6 6 6" stroke-linecap="round" stroke-linejoin="round"/></svg></span>' +
      nums.map(function (n) {
        if (n === '…') return '<span style="color:#6a6a6a;padding:0 2px">…</span>';
        return '<span class="pnum" data-pg="' + n + '" style="cursor:pointer;' +
          (n === state.page ? 'border-color:#4166f6;color:#fff' : 'color:#8a8a8a') + '">' + n + '</span>';
      }).join('') +
      '<span class="pb" data-pg="next" style="opacity:' + (state.page < pages ? 1 : .35) + '"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 6l6 6-6 6" stroke-linecap="round" stroke-linejoin="round"/></svg></span>';
  }

  function render() {
    var rows = filtered();
    var per = parseInt(val('cm-per') || '10', 10);
    var pages = Math.max(1, Math.ceil(rows.length / per));
    if (state.page > pages) state.page = pages;
    var start = (state.page - 1) * per;
    var slice = rows.slice(start, start + per);

    table.querySelectorAll('.dt-row').forEach(function (r) { r.remove(); });
    if (!slice.length) {
      var empty = document.createElement('div');
      empty.className = 'dt-row';
      empty.style.cssText = 'display:flex;align-items:center;justify-content:center;color:#8a8a8a';
      empty.textContent = state.accounts.length
        ? 'No creators match these filters'
        : 'No creators yet — a creator appears once its OnlyFans session is captured';
      table.appendChild(empty);
    } else {
      table.insertAdjacentHTML('beforeend', slice.map(rowHtml).join(''));
    }
    totalEl.textContent = 'Total: ' + state.accounts.length + ' creators' +
      (rows.length !== state.accounts.length ? ' · ' + rows.length + ' match' : '');
    document.getElementById('cm-range').textContent = rows.length
      ? ('showing ' + (start + 1) + '–' + Math.min(start + per, rows.length) + ' of ' + rows.length)
      : '';
    renderPager(rows.length, per);
    renderSummary();
  }

  function fillFilters() {
    var prox = document.getElementById('cm-proxy');
    prox.innerHTML = '<option value="">Any proxy</option>' +
      state.proxies.map(function (p) { return '<option value="' + esc(p.label) + '">' + esc(p.label) + '</option>'; }).join('') +
      '<option value="__none">Direct egress (no proxy)</option>';
    var seen = {}, opts = [];
    Object.keys(state.byAccount).forEach(function (aid) {
      state.byAccount[aid].forEach(function (r) {
        if (seen[r.id]) return; seen[r.id] = 1; opts.push(r);
      });
    });
    opts.sort(function (a, b) { return a.name.localeCompare(b.name); });
    document.getElementById('cm-emp').innerHTML = '<option value="">Any employee</option>' +
      opts.map(function (r) { return '<option value="' + esc(r.id) + '">' + esc(r.name) + '</option>'; }).join('');
  }

  // Per-creator captured-session snapshot: GET /admin/session/status?account_id=<id>.
  // Fills captured_at / cookies_count / profile_id (capture method) for each row.
  async function fetchSessions() {
    try {
      await Promise.all(state.accounts.map(async function (a) {
        try {
          var st = await Fastt.get('/admin/session/status', { account_id: a.id });
          state.session[String(a.id)] = st || { loaded: false };
        } catch (e) { state.session[String(a.id)] = { loaded: false }; }
      }));
    } finally { state.sessionLoaded = true; render(); }
  }

  // Live per-account OnlyFans probe (2–5s each — kept off the critical path).
  async function probeHealth() {
    try {
      var hp = await Fastt.get('/health', { all_accounts: '1' });
      state.health = {};
      (hp.accounts || []).forEach(function (r) { state.health[String(r.account_id)] = r; });
      state.healthLoaded = true;
      render();
    } catch (e) { console.warn('health probe unavailable', e); state.healthLoaded = true; render(); }
  }

  async function load() {
    var px = await Fastt.get('/admin/proxies');
    state.accounts = px.accounts || [];
    state.proxies = px.proxies || [];
    render();

    fetchSessions();        // fire-and-forget: per-creator captured-session snapshots
    probeHealth();          // fire-and-forget: rows show "checking…" until it lands

    // Session freshness (probes OF's live rev — can take a beat).
    try {
      var drift = await Fastt.get('/admin/rev/drift');
      state.drift = {};
      (drift.accounts || []).forEach(function (r) { state.drift[String(r.account_id)] = r; });
      render();
    } catch (e) { console.warn('rev/drift unavailable', e); }

    // Who actually worked each creator — the Employees column.
    try {
      var st = await Fastt.get('/admin/stats/per-employee', { by_account: 'true' });
      state.byAccount = {};
      (st.employees || []).forEach(function (r) {
        if (r.employee_id == null) return;          // synthetic Unattributed bucket
        (r.per_account || []).forEach(function (pa) {
          var key = String(pa.account_id);
          (state.byAccount[key] = state.byAccount[key] || []).push({
            id: r.employee_id, name: r.display_name,
            messages_sent: pa.messages_sent, revenue_cents: pa.revenue_cents,
          });
        });
      });
      Object.keys(state.byAccount).forEach(function (k) {
        state.byAccount[k].sort(function (a, b) { return b.messages_sent - a.messages_sent; });
      });
      fillFilters();
      render();
    } catch (e) { console.warn('per-employee unavailable', e); fillFilters(); }
  }

  ['cm-platform', 'cm-proxy', 'cm-conn', 'cm-emp', 'cm-status', 'cm-per'].forEach(function (id) {
    document.getElementById(id).addEventListener('change', function () { state.page = 1; render(); });
  });
  document.getElementById('cm-filter').addEventListener('input',
    Fastt.debounce(function () { state.page = 1; render(); }, 150));
  document.getElementById('cm-reset').addEventListener('click', function () {
    ['cm-platform', 'cm-proxy', 'cm-conn', 'cm-emp', 'cm-status'].forEach(function (id) {
      document.getElementById(id).value = '';
    });
    document.getElementById('cm-filter').value = '';
    state.page = 1; render();
  });
  // Primary action: re-probe every creator's live OnlyFans connection.
  document.getElementById('cm-recheck').addEventListener('click', async function () {
    var b = this;
    if (b.classList.contains('spin')) return;
    b.classList.add('spin');
    state.healthLoaded = false; render();
    await probeHealth();
    setTimeout(function () { b.classList.remove('spin'); }, 600);
    Fastt.toast('Health re-checked', 'ok');
  });

  // Advanced: reveal expert controls (recolor swatch).
  document.getElementById('cm-adv').addEventListener('click', function () {
    this.classList.toggle('on');
    table.classList.toggle('cm-table-adv', this.classList.contains('on'));
  });

  document.getElementById('cm-pager').addEventListener('click', function (e) {
    var b = e.target.closest('[data-pg]');
    if (!b) return;
    var v = b.dataset.pg;
    if (v === 'prev') state.page = Math.max(1, state.page - 1);
    else if (v === 'next') state.page = state.page + 1;
    else state.page = parseInt(v, 10);
    render();
  });

  // Recolor (Advanced): native color input → PATCH /admin/accounts/{id} {color}.
  table.addEventListener('change', async function (e) {
    var inp = e.target.closest('.swatch input[type=color]');
    if (!inp) return;
    var row = inp.closest('.dt-row'); var id = row && row.dataset.id;
    var acct = state.accounts.find(function (a) { return String(a.id) === String(id); });
    if (!acct || inp.value === acct.color) return;
    try {
      await Fastt.patch('/admin/accounts/' + encodeURIComponent(id), { color: inp.value });
      acct.color = inp.value;
      Fastt.saved('Recolored ✓');
      render();
    } catch (err) { Fastt.oops(err); }
  });

  table.addEventListener('click', async function (e) {
    var btn = e.target.closest('[data-act]');
    if (!btn) return;
    var row = btn.closest('.dt-row');
    var id = row && row.dataset.id;
    if (!id) return;
    var acct = state.accounts.find(function (a) { return String(a.id) === String(id); });
    if (btn.dataset.act === 'switch') {
      if (String(id) === CURRENT) { Fastt.toast('Already the active creator', 'ok'); return; }
      if (!confirm('Switch the whole app to "' + (acct.nickname || id) + '"?\nEvery screen will load this creator\'s data.')) return;
      Fastt.setAccount(String(id));
      Fastt.saved('Switched to ' + (acct.nickname || id));
      setTimeout(function () { location.reload(); }, 500);
      return;
    }
    if (btn.dataset.act === 'reload') {
      // Operator action — re-reads the latest captured session from disk and
      // hot-swaps the OF client. Not fan-facing; sends nothing to any fan.
      if (btn.classList.contains('spin')) return;
      if (!confirm('Reload "' + (acct.nickname || id) + '" session from disk?\n\n'
        + 'Hot-swaps the OF client to the latest captured cookies. Does not message any fan.')) return;
      btn.classList.add('spin');
      try {
        await Fastt.post('/admin/reload-session', undefined, { params: { account_id: id } });
        Fastt.saved('Session reloaded ✓');
        await fetchSessions();
        probeHealth();
      } catch (err) { Fastt.oops(err); }
      finally { setTimeout(function () { btn.classList.remove('spin'); }, 600); }
      return;
    }
    if (btn.dataset.act === 'recapture') { openCapture(acct); return; }
    if (btn.dataset.act === 'rename') {
      var next = prompt('New nickname for ' + (acct.nickname || id) + ':', acct.nickname || '');
      if (next === null || !next.trim() || next.trim() === acct.nickname) return;
      try {
        await Fastt.patch('/admin/accounts/' + encodeURIComponent(id), { nickname: next.trim() });
        Fastt.saved('Renamed ✓');
        await load();
      } catch (err) { Fastt.oops(err); }
    } else if (btn.dataset.act === 'remove') {
      if (!confirm('Permanently remove creator "' + (acct.nickname || id) + '" (' + id + ')?\n\nThis deletes its captured sessions and stops its automations. Cannot be undone.')) return;
      try {
        await Fastt.del('/admin/accounts/' + encodeURIComponent(id));
        Fastt.saved('Removed');
        await load();
      } catch (err) { Fastt.oops(err); }
    }
  });

  // ── Capture-session modal (POST /admin/session/bootstrap, mode=paste-curl) ──
  var capBack = document.getElementById('cm-cap-back');
  var capCurl = document.getElementById('cm-curl');
  var capNick = document.getElementById('cm-nick');
  var capActive = document.getElementById('cm-active');
  var capTitle = document.getElementById('cm-cap-title');
  var capNote = document.getElementById('cm-recap-note');
  var capWho = document.getElementById('cm-recap-who');
  var capBtn = document.getElementById('cm-capture');
  var CAP_LABEL = capBtn.innerHTML;
  var capTarget = '';   // account_id hint when re-capturing an existing creator

  function syncCapBtn() {
    var v = (capCurl.value || '').trim();
    capBtn.disabled = !(v.length > 4 && /curl/i.test(v));
  }
  function openCapture(acct) {
    capTarget = acct ? String(acct.id) : '';
    if (acct) {
      capTitle.textContent = 'Re-capture session';
      capWho.textContent = (acct.nickname || acct.id) + ' (' + acct.id + ')';
      capNote.classList.add('on');
      capNick.value = acct.nickname || '';
      capActive.checked = String(acct.id) === CURRENT;
    } else {
      capTitle.textContent = 'Connect a creator';
      capNote.classList.remove('on');
      capNick.value = '';
      capActive.checked = true;
    }
    capCurl.value = '';
    syncCapBtn();
    capBack.classList.add('open');
    setTimeout(function () { capCurl.focus(); }, 30);
  }
  function closeCapture() { capBack.classList.remove('open'); }

  document.getElementById('cm-connect').addEventListener('click', function () { openCapture(null); });
  document.getElementById('cm-cap-x').addEventListener('click', closeCapture);
  document.getElementById('cm-cap-cancel').addEventListener('click', closeCapture);
  capBack.addEventListener('click', function (e) { if (e.target === capBack) closeCapture(); });
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape' && capBack.classList.contains('open')) closeCapture(); });
  capCurl.addEventListener('input', syncCapBtn);

  // Only fires on an explicit click with an operator-pasted cURL — never during load.
  capBtn.addEventListener('click', async function () {
    var curl = (capCurl.value || '').trim();
    if (!(curl.length > 4 && /curl/i.test(curl))) { Fastt.toast('That doesn’t look like a cURL command', 'err'); return; }
    var body = { mode: 'paste-curl', curl: curl, make_active: !!capActive.checked };
    var nk = (capNick.value || '').trim(); if (nk) body.nickname = nk;
    if (capTarget) body.account_id = capTarget;
    capBtn.disabled = true; capBtn.textContent = 'Capturing…';
    try {
      var r = await Fastt.post('/admin/session/bootstrap', body);
      closeCapture();
      Fastt.saved('Session captured — creator ' + (r.user_id || r.account_id || ''));
      await load();
    } catch (err) {
      Fastt.oops(err);
    } finally {
      capBtn.innerHTML = CAP_LABEL; syncCapBtn();
    }
  });

  try {
    await load();
  } catch (e) {
    table.querySelectorAll('.dt-row').forEach(function (r) { r.remove(); });
    var errRow = document.createElement('div');
    errRow.className = 'dt-row';
    errRow.style.cssText = 'display:flex;align-items:center;justify-content:center;color:#e05b5b';
    errRow.textContent = 'Couldn’t load creators — relay error';
    table.appendChild(errRow);
    totalEl.textContent = 'Total: unavailable';
    throw e;
  }
});
