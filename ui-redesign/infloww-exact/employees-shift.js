/* ==== LIVE WIRING — employees-shift ==========================================
 * There is NO shift-scheduling backend on this relay: no shift table, no route,
 * nothing to create or save. So the page does not draw an invented roster on an
 * invented week. It draws the ONE thing that is real and shift-shaped —
 * when each operator actually worked — reconstructed from the audit trail:
 *
 *   GET /admin/audit?limit=500&offset=…   walked back until the shown week is
 *                                         covered (500 is the route's cap)
 *   GET /admin/employees?include_disabled=true   → operator names
 *   GET /admin/stats/per-employee                → names for system ids the
 *                                                  roster hides (Automation)
 *   GET /admin/proxies                           → creator nicknames
 *
 * Every timestamp is parsed with Fastt.parseUtc (relay stamps are tz-naive UTC)
 * and rendered in the viewer's own zone.
 */
Fastt.ready(async function () {
  var esc = Fastt.esc, fmtInt = Fastt.fmtInt;
  var MAX_PAGES = 12, PAGE = 500;

  var S = {
    rows: [], pages: 0, exhausted: false,
    names: {}, creators: {}, autoIds: {},
    weekStart: null, tab: 'week',
  };

  // ── week helpers (local time, Sunday-start like the mock-up) ──
  function startOfWeek(d) {
    var x = new Date(d.getFullYear(), d.getMonth(), d.getDate());
    x.setDate(x.getDate() - x.getDay());
    return x;
  }
  function addDays(d, n) {
    var x = new Date(d.getTime());
    x.setDate(x.getDate() + n);
    return x;
  }
  function dayKey(d) {
    return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
  }
  function hhmm(d) {
    return d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
  }
  S.weekStart = startOfWeek(new Date());

  try {
    document.getElementById('sh-tz').textContent =
      Intl.DateTimeFormat().resolvedOptions().timeZone || 'local time';
  } catch (e) { /* keep the fallback label */ }

  Fastt.staticBadge(document.getElementById('sh-note'), 'NO SHIFT BACKEND');
  var createBtn = document.getElementById('sh-create');
  Fastt.staticBadge(createBtn, 'NO BACKEND');
  createBtn.title = 'No shift route exists on this relay — there is nothing to POST.';
  createBtn.addEventListener('click', function () {
    Fastt.toast('No shift-scheduling endpoint exists on this relay — nothing to create.', 'err');
  });
  // The grid itself IS live data (audit rows), even though scheduling is not.
  Fastt.liveBadge(document.getElementById('sh-live'));

  // ── loaders ───────────────────────────────────────────────────
  async function loadNames() {
    try {
      var out = await Fastt.get('/admin/employees', { include_disabled: 'true' });
      (out.employees || []).forEach(function (e) { S.names[e.id] = e.display_name; });
    } catch (e) { console.warn('employees unavailable', e); }
    try {
      var px = await Fastt.get('/admin/proxies');
      (px.accounts || []).forEach(function (a) { S.creators[String(a.id)] = a.nickname || String(a.id); });
    } catch (e) { console.warn('proxies unavailable', e); }
    // by_account also carries nicknames for creators the picker never sees
    // (other owners' models an employee worked), so audit rows never render a
    // bare numeric account id.
    try {
      var st = await Fastt.get('/admin/stats/per-employee', { by_account: 'true' });
      (st.employees || []).forEach(function (r) {
        if (r.employee_id != null) {
          if (!S.names[r.employee_id]) S.names[r.employee_id] = r.display_name;
          if (/^automation$/i.test(r.display_name || '')) S.autoIds[r.employee_id] = 1;
        }
        (r.per_account || []).forEach(function (pa) {
          var k = String(pa.account_id);
          if (!S.creators[k]) S.creators[k] = pa.account_nickname || k;
        });
      });
    } catch (e) { console.warn('per-employee unavailable', e); }
  }

  function oldestLoaded() {
    if (!S.rows.length) return null;
    return Fastt.parseUtc(S.rows[S.rows.length - 1].at);
  }

  /** Walk /admin/audit back until the requested day is covered (or we hit the
   *  page cap). The route has no date filter, so paging is the only way. */
  async function ensureCovered(untilDate, onProgress) {
    while (!S.exhausted && S.pages < MAX_PAGES) {
      var oldest = oldestLoaded();
      if (oldest && oldest <= untilDate) return;
      var out = await Fastt.api('/admin/audit',
        { params: { limit: PAGE, offset: S.pages * PAGE }, noAccount: true });
      var got = out.actions || [];
      S.rows = S.rows.concat(got);
      S.pages++;
      // Paint what we have after each page so the grid appears immediately
      // instead of holding on "Loading…" until the whole week is covered.
      if (onProgress) { try { onProgress(); } catch (e) { /* keep walking */ } }
      if (got.length < PAGE) { S.exhausted = true; return; }
    }
  }

  // ── filtering ─────────────────────────────────────────────────
  function opKey(a) { return a.employee_id == null ? 'sys' : String(a.employee_id); }
  function opName(k) {
    if (k === 'sys') return 'unattributed / system';
    return S.names[k] || ('employee #' + k);
  }
  function isAuto(k) { return k !== 'sys' && !!S.autoIds[k]; }

  function passes(a) {
    var kind = document.getElementById('sh-kind').value;
    var emp = document.getElementById('sh-emp').value;
    var acct = document.getElementById('sh-acct').value;
    var k = opKey(a);
    if (emp && k !== emp) return false;
    if (acct && String(a.account_id || '') !== acct) return false;
    if (kind === 'auto' && !isAuto(k)) return false;
    if (kind === 'human' && (isAuto(k) || k === 'sys')) return false;
    return true;
  }

  function fillFilters() {
    var seen = {};
    S.rows.forEach(function (a) { seen[opKey(a)] = 1; });
    var emp = document.getElementById('sh-emp');
    var cur = emp.value;
    emp.innerHTML = '<option value="">Any employee</option>' +
      Object.keys(seen).sort(function (a, b) { return opName(a).localeCompare(opName(b)); })
        .map(function (k) { return '<option value="' + esc(k) + '">' + esc(opName(k)) + '</option>'; }).join('');
    emp.value = cur;
    var acctSel = document.getElementById('sh-acct');
    var cur2 = acctSel.value;
    var aseen = {};
    S.rows.forEach(function (a) { if (a.account_id) aseen[String(a.account_id)] = 1; });
    acctSel.innerHTML = '<option value="">Any creator</option>' +
      Object.keys(aseen).sort(function (a, b) {
        return (S.creators[a] || a).localeCompare(S.creators[b] || b);
      }).map(function (id) {
        return '<option value="' + esc(id) + '">' + esc(S.creators[id] || id) + '</option>';
      }).join('');
    acctSel.value = cur2;
  }

  // ── render: week grid ─────────────────────────────────────────
  function renderWeek() {
    var days = [];
    for (var i = 0; i < 7; i++) days.push(addDays(S.weekStart, i));
    var todayKey = dayKey(new Date());
    var weekEnd = addDays(S.weekStart, 7);

    document.getElementById('sh-weeklbl').textContent =
      days[0].toLocaleDateString([], { month: 'short', day: 'numeric' }) + ' – ' +
      days[6].toLocaleDateString([], { month: 'short', day: 'numeric', year: 'numeric' });

    // bucket: op -> dayKey -> {n, first, last, accts}
    var buckets = {}, inWeek = 0;
    S.rows.forEach(function (a) {
      if (!passes(a)) return;
      var d = Fastt.parseUtc(a.at);
      if (!d || d < S.weekStart || d >= weekEnd) return;
      inWeek++;
      var k = opKey(a), dk = dayKey(d);
      var op = buckets[k] || (buckets[k] = { total: 0, days: {} });
      op.total++;
      var cell = op.days[dk] || (op.days[dk] = { n: 0, first: d, last: d, accts: {} });
      cell.n++;
      if (d < cell.first) cell.first = d;
      if (d > cell.last) cell.last = d;
      if (a.account_id) cell.accts[String(a.account_id)] = 1;
    });
    var ops = Object.keys(buckets).sort(function (a, b) { return buckets[b].total - buckets[a].total; });

    document.getElementById('sh-weekhead').innerHTML =
      '<div class="wc-nav" data-wk="prev"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M15 6l-6 6 6 6" stroke-linecap="round" stroke-linejoin="round"/></svg></div>' +
      days.map(function (d) {
        var on = dayKey(d) === todayKey;
        return '<div class="wc-dh' + (on ? ' today' : '') + '"><div class="dow">' +
          d.toLocaleDateString([], { weekday: 'short' }) + '</div><div class="dnum">' + d.getDate() + '</div></div>';
      }).join('') +
      '<div class="wc-nav" data-wk="next"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 6l6 6-6 6" stroke-linecap="round" stroke-linejoin="round"/></svg></div>';

    var bodyEl = document.getElementById('sh-weekbody');
    if (!ops.length) {
      bodyEl.style.display = 'none';
      document.getElementById('sh-empty') && document.getElementById('sh-empty').remove();
      var d2 = document.createElement('div');
      d2.id = 'sh-empty';
      d2.className = 'sh-empty';
      d2.innerHTML = '<b>Nobody acted through the relay in this week.</b>' +
        (S.exhausted
          ? ' The audit log has been read to its end, so this really is silence.'
          : ' Only the newest ' + fmtInt(S.rows.length) + ' audit rows are loaded (back to ' +
            esc(Fastt.fmtDate(S.rows.length ? S.rows[S.rows.length - 1].at : null)) +
            ') — older weeks are beyond the scan window this page performs.');
      bodyEl.parentNode.appendChild(d2);
      return;
    }
    var old = document.getElementById('sh-empty');
    if (old) old.remove();
    bodyEl.style.display = '';

    bodyEl.innerHTML = ops.map(function (k) {
      var op = buckets[k];
      var initial = opName(k).slice(0, 1).toUpperCase();
      return '<div class="wc-emp"><div class="wc-av">' + esc(initial) + '</div>' +
          '<div class="wc-emplbl" title="' + esc(opName(k)) + '">' + esc(opName(k)) + '</div>' +
          '<div class="wc-emptot">' + fmtInt(op.total) + ' actions</div></div>' +
        days.map(function (d) {
          var c = op.days[dayKey(d)];
          if (!c) return '<div class="wc-cell"></div>';
          var accts = Object.keys(c.accts).map(function (i) { return S.creators[i] || i; });
          return '<div class="wc-cell"><div class="shift" title="' + esc(accts.join(', ')) + '">' +
            '<div class="hrs">' + esc(hhmm(c.first)) + ' – ' + esc(hhmm(c.last)) + '</div>' +
            '<div class="cnt">' + fmtInt(c.n) + ' action' + (c.n === 1 ? '' : 's') + '</div>' +
            (accts.length ? '<div class="acs">' + esc(accts.slice(0, 2).join(', ')) +
              (accts.length > 2 ? ' +' + (accts.length - 2) : '') + '</div>' : '') +
            '</div></div>';
        }).join('') +
        '<div class="wc-cell" style="min-height:0"></div>';
    }).join('');

    var busiest = ops.length
      ? ' Busiest: <b style="color:#cdd8ff">' + esc(opName(ops[0])) + '</b> (' + fmtInt(buckets[ops[0]].total) + ' actions).'
      : '';
    document.getElementById('sh-warntxt').innerHTML =
      fmtInt(inWeek) + ' audit rows fall inside this week across ' + ops.length + ' operator' +
      (ops.length === 1 ? '' : 's') + '.' + busiest +
      ' A “shift” here is first→last action on a day, not a booked slot — gaps inside it are invisible.';
  }

  // ── render: activity log ──────────────────────────────────────
  function renderLog() {
    var wrap = document.getElementById('sh-logwrap');
    var weekEnd = addDays(S.weekStart, 7);
    var rows = S.rows.filter(function (a) {
      if (!passes(a)) return false;
      var d = Fastt.parseUtc(a.at);
      return d && d >= S.weekStart && d < weekEnd;
    });
    S.logRows = rows;
    if (!rows.length) {
      wrap.innerHTML = '<div class="sh-empty"><b>No activity in this week for these filters.</b></div>';
      return;
    }
    wrap.innerHTML = '<div style="display:flex;align-items:center;margin-bottom:12px">' +
        '<div class="sc-cap" style="margin:0">' + fmtInt(rows.length) + ' action' +
          (rows.length === 1 ? '' : 's') + ' logged this week for these filters.</div>' +
        '<button class="btn-ghost" data-export style="margin-left:auto;height:38px;padding:0 16px;display:inline-flex;align-items:center;gap:8px">' +
          '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M12 4v10M8 11l4 3 4-3" stroke-linecap="round" stroke-linejoin="round"/><path d="M5 19h14" stroke-linecap="round"/></svg>' +
          'Export CSV</button></div>' +
      '<div class="loglist"><div class="logrow loghead">' +
      '<div class="lt">When</div><div class="lo">Operator</div><div class="la">Action</div>' +
      '<div class="lc">Creator</div></div>' + rows.slice(0, 250).map(function (a) {
      var d = Fastt.parseUtc(a.at);
      return '<div class="logrow"><div class="lt">' + esc(d ? d.toLocaleString() : '—') + '</div>' +
        '<div class="lo">' + esc(opName(opKey(a))) + '</div>' +
        '<div class="la">' + esc(a.action) + '</div>' +
        '<div class="lc">' + esc(a.account_id ? (S.creators[String(a.account_id)] || a.account_id) : '—') + '</div></div>';
    }).join('') + '</div>' +
    (rows.length > 250 ? '<div class="sh-scan" style="margin:12px 0">showing the newest 250 of ' +
      fmtInt(rows.length) + ' rows in this week</div>' : '');
  }

  // ── export: activity log → CSV (client-side, from loaded rows) ─
  function exportCsv() {
    var rows = S.logRows || [];
    if (!rows.length) { Fastt.toast('Nothing to export for this week and filters.', 'err'); return; }
    var out = [['When (local)', 'Operator', 'Action', 'Creator']];
    rows.forEach(function (a) {
      var d = Fastt.parseUtc(a.at);
      out.push([
        d ? d.toLocaleString() : '',
        opName(opKey(a)),
        a.action || '',
        a.account_id ? (S.creators[String(a.account_id)] || a.account_id) : ''
      ]);
    });
    var csv = out.map(function (r) {
      return r.map(function (c) {
        return '"' + String(c == null ? '' : c).replace(/"/g, '""') + '"';
      }).join(',');
    }).join('\r\n');
    var blob = new Blob([csv], { type: 'text/csv;charset=utf-8' });
    var url = URL.createObjectURL(blob);
    var a = document.createElement('a');
    a.href = url;
    a.download = 'shift-activity-' + dayKey(S.weekStart) + '.csv';
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
    Fastt.toast('Exported ' + fmtInt(rows.length) + ' rows', 'ok');
  }

  function render() {
    document.getElementById('sh-weekwrap').style.display = S.tab === 'week' ? '' : 'none';
    document.getElementById('sh-logwrap').style.display = S.tab === 'log' ? '' : 'none';
    document.getElementById('sh-warn').style.display = S.tab === 'week' ? '' : 'none';
    if (S.tab === 'week') renderWeek(); else renderLog();
    document.getElementById('sh-scan').textContent =
      fmtInt(S.rows.length) + ' audit rows loaded' + (S.exhausted ? ' (whole log)' : ' — ' + S.pages + ' page(s)');
  }

  async function gotoWeek(start) {
    S.weekStart = start;
    document.getElementById('sh-scan').textContent = 'scanning audit log…';
    try {
      await ensureCovered(start, function () { fillFilters(); render(); });
    } catch (e) { console.warn('audit page failed', e); }
    fillFilters();
    render();
  }

  // ── events ────────────────────────────────────────────────────
  document.getElementById('sh-tabs').addEventListener('click', function (e) {
    var t = e.target.closest('[data-shtab]');
    if (!t) return;
    S.tab = t.dataset.shtab;
    render();
  });
  document.getElementById('sh-prev').addEventListener('click', function () { gotoWeek(addDays(S.weekStart, -7)); });
  document.getElementById('sh-next').addEventListener('click', function () { gotoWeek(addDays(S.weekStart, 7)); });
  document.getElementById('sh-today').addEventListener('click', function () { gotoWeek(startOfWeek(new Date())); });
  document.getElementById('sh-weekhead').addEventListener('click', function (e) {
    var n = e.target.closest('[data-wk]');
    if (!n) return;
    gotoWeek(addDays(S.weekStart, n.dataset.wk === 'prev' ? -7 : 7));
  });
  ['sh-kind', 'sh-emp', 'sh-acct'].forEach(function (id) {
    document.getElementById(id).addEventListener('change', render);
  });
  document.getElementById('sh-reset').addEventListener('click', function () {
    ['sh-kind', 'sh-emp', 'sh-acct'].forEach(function (id) { document.getElementById(id).value = ''; });
    render();
  });
  document.getElementById('sh-refresh').addEventListener('click', async function () {
    S.rows = []; S.pages = 0; S.exhausted = false;
    await gotoWeek(S.weekStart);
    Fastt.toast('Audit window reloaded', 'ok');
  });
  document.getElementById('sh-logwrap').addEventListener('click', function (e) {
    if (e.target.closest('[data-export]')) exportCsv();
  });
  // keyboard week nav (ignored while a form control is focused)
  document.addEventListener('keydown', function (e) {
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    var tag = (document.activeElement && document.activeElement.tagName) || '';
    if (tag === 'INPUT' || tag === 'SELECT' || tag === 'TEXTAREA') return;
    if (e.key === 'ArrowLeft') { e.preventDefault(); gotoWeek(addDays(S.weekStart, -7)); }
    else if (e.key === 'ArrowRight') { e.preventDefault(); gotoWeek(addDays(S.weekStart, 7)); }
  });
  document.getElementById('sh-weeklbl').title = 'Use ← / → to step weeks';

  // ── boot ──────────────────────────────────────────────────────
  await loadNames();
  await gotoWeek(S.weekStart);
});
