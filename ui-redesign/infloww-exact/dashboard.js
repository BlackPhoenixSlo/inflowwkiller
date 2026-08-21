// ==== LIVE WIRING (relay data via _shared/fastt.js) ====
//
// TWO money bases live on this page and every tile names the one it used:
//   • "Net earnings"   → GET /api/of/v2/payouts/stats — OF's own post-cut view.
//     list.total.{all,tips,subscribes,chat_messages,stream}.{total_net,total_gross}
//     and list.months[<monthEpoch>].{tips,subscribes,chat_messages,stream}[] whose
//     entries are PER-DAY {time,net,gross} — so any segment window is summable.
//     Values are DOLLAR floats (OF payments API), never cents.
//   • "Gross earnings" → GET /admin/stats/revenue?group_by=kind — the relay ledger
//     in CENTS, i.e. what the fan actually paid.
// The two will NOT agree (OF takes ~20%), so the basis is printed under every
// number rather than silently mixed.
Fastt.ready(async function () {
  var $ = Fastt.$;
  var $$ = Fastt.$$;

  // ── honest chrome: data below is bucketed by UTC day ─────────────
  var tzTop = $('#tz-top'), tzSec = $('#tz-sec');
  if (tzTop) tzTop.textContent = 'UTC+00:00';
  if (tzSec) tzSec.textContent = 'UTC+00:00';

  // ── genuinely backend-less: there is no shifts table and no clock-in
  //    table anywhere in the relay, so shifts + clock-in are collapsed into
  //    one honest "coming soon" strip below the live data. ──
  Fastt.staticBadge($('#team-head'), 'COMING SOON');

  // ── every money tile gets a provenance line under its label ──────
  $$('.metric').forEach(function (m) {
    var lbl = m.querySelector('.ml');
    if (lbl && !m.querySelector('.msrc')) {
      var s = document.createElement('div');
      s.className = 'msrc';
      lbl.insertAdjacentElement('afterend', s);
    }
  });
  function tile(kpi) { return $('.metric[data-kpi="' + kpi + '"]'); }
  function setTile(kpi, value, src, title) {
    var el = tile(kpi);
    if (!el) return;
    var v = el.querySelector('.mv'), s = el.querySelector('.msrc');
    if (v) v.textContent = value;
    if (s) s.textContent = src || '';
    if (title) el.title = title;
  }


  // ── topbar bell: real unread count from OF, not a decorative dot ──
  // GET /api/of/v2/users/notifications/count → per-type UNREAD ints; `all` is
  // the number the bell should wear. On failure the bell stays bare rather
  // than showing an invented indicator.
  try {
    var _nc = await Fastt.get('/api/of/v2/users/notifications/count');
    var _bell = Fastt.$('#bell-count');
    var _unread = Number(_nc && _nc.all) || 0;
    if (_bell) {
      _bell.textContent = _unread > 99 ? '99+' : String(_unread);
      _bell.style.display = _unread ? '' : 'none';
      _bell.title = _unread + ' unread OnlyFans notification' + (_unread === 1 ? '' : 's');
    }
  } catch (e) { /* OF unreachable — leave the bell bare */ }
  // ── segment → UTC date window (bare dates; server makes `to` inclusive) ──
  function segWindow() {
    var onBtn = $('#seg button.on');
    var lbl = onBtn ? onBtn.textContent.trim() : 'This week';
    var now = new Date();
    var DAY = 86400000;
    var today = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate()));
    var iso = function (d) { return d.toISOString().slice(0, 10); };
    if (lbl === 'Yesterday') {
      var y = new Date(today.getTime() - DAY);
      return { from: iso(y), to: iso(y), label: lbl };
    }
    if (lbl === 'Today') return { from: iso(today), to: iso(today), label: lbl };
    if (lbl === 'This month') {
      var first = new Date(Date.UTC(today.getUTCFullYear(), today.getUTCMonth(), 1));
      return { from: iso(first), to: iso(today), label: lbl };
    }
    var off = (today.getUTCDay() + 6) % 7; // Monday-start ISO week
    var mon = new Date(today.getTime() - off * DAY);
    return { from: iso(mon), to: iso(today), label: lbl };
  }

  // ── basis switch (the "Net earnings ▾" control is a real Gross/Net toggle) ──
  var BASIS = 'net';
  var dd = $('#dd-net'), ddLabel = $('#dd-net-label');
  if (dd) {
    dd.addEventListener('click', function (e) {
      var opt = e.target.closest('.ddopt');
      if (opt) {
        e.stopPropagation();
        BASIS = opt.dataset.basis;
        $$('#dd-net-menu .ddopt').forEach(function (o) { o.classList.toggle('on', o === opt); });
        if (ddLabel) ddLabel.textContent = BASIS === 'net' ? 'Net earnings' : 'Gross earnings';
        dd.classList.remove('open');
        refresh();
        return;
      }
      dd.classList.toggle('open');
      e.stopPropagation();
    });
    document.addEventListener('click', function () { dd.classList.remove('open'); });
  }

  // ── OF payouts (cached once per page: it is an all-history payload) ──
  var _payouts;   // undefined = not fetched, null = fetch failed
  async function payoutsStats() {
    if (_payouts !== undefined) return _payouts;
    try {
      var out = await Fastt.get('/api/of/v2/payouts/stats');
      _payouts = (out && out.list) ? out.list : null;
    } catch (e) { _payouts = null; }
    return _payouts;
  }
  var PAY_CATS = ['tips', 'subscribes', 'chat_messages', 'stream'];
  /** Sum every per-day payouts entry whose `time` falls inside [from, to]. */
  function payoutsWindow(list, from, to) {
    var fs = Date.parse(from + 'T00:00:00Z') / 1000;
    var ts = Date.parse(to + 'T00:00:00Z') / 1000 + 86400;
    var acc = {};
    PAY_CATS.forEach(function (c) { acc[c] = { net: 0, gross: 0 }; });
    var months = (list && list.months) || {};
    Object.keys(months).forEach(function (mk) {
      PAY_CATS.forEach(function (c) {
        (months[mk][c] || []).forEach(function (e) {
          if (e.time >= fs && e.time < ts) {
            acc[c].net += Number(e.net) || 0;
            acc[c].gross += Number(e.gross) || 0;
          }
        });
      });
    });
    return acc;
  }

  // ── earnings KPI card ────────────────────────────────────────────
  // Ledger kinds → tiles. The complete live kind set is ppv_message,
  // ppv_post, tip, rebill, subscription (verified against
  // /admin/ingest/transactions/unknown-kinds, which is empty), so there is
  // no ledger kind for Streams or Referrals — those two read OF directly.
  function kpiBucket(kind) {
    if (!kind) return null;
    if (kind === 'ppv_post') return 'posts';
    if (kind === 'ppv' || kind === 'ppv_message') return 'messages';
    if (kind === 'tip') return 'tips';
    if (kind === 'subscription' || kind === 'rebill') return 'subscriptions';
    return null;
  }
  async function loadEarnings() {
    var w = segWindow();
    var out = await Fastt.get('/admin/stats/revenue',
      { group_by: 'kind', from: w.from, to: w.to });
    var rows = out.rows || [];
    var L = { subscriptions: 0, posts: 0, messages: 0, tips: 0 };
    var lTotal = 0, lOther = 0;
    rows.forEach(function (r) {
      var c = r.total_cents || 0;
      lTotal += c;
      var b = kpiBucket(r.kind);
      if (b) L[b] += c; else lOther += c;
    });

    var list = await payoutsStats();
    var P = list ? payoutsWindow(list, w.from, w.to) : null;
    var K = BASIS === 'net' ? 'net' : 'gross';
    var payLbl = 'OF payouts · ' + K;
    var ledLbl = 'relay ledger · gross';

    if (BASIS === 'net' && P) {
      setTile('subscriptions', Fastt.fmtMoney(P.subscribes.net), payLbl);
      setTile('messages', Fastt.fmtMoney(P.chat_messages.net), payLbl);
      setTile('tips', Fastt.fmtMoney(P.tips.net), payLbl);
      setTile('streams', Fastt.fmtMoney(P.stream.net), payLbl);
      // OF payouts has no "posts" category at all — keep the tile on the
      // ledger and SAY so instead of inventing a net figure for it.
      setTile('posts', Fastt.fmtCents(L.posts), 'ledger gross · not in OF payouts',
        'OF payouts/stats has no posts bucket; this is the ledger gross figure.');
      var netTotal = P.subscribes.net + P.chat_messages.net + P.tips.net + P.stream.net;
      $('#kpi-total').textContent = Fastt.fmtMoney(netTotal);
      $('#kpi-total-src').textContent = 'OF payouts · net' + (L.posts ? ' (posts excluded)' : '');
    } else {
      setTile('subscriptions', Fastt.fmtCents(L.subscriptions), ledLbl);
      setTile('messages', Fastt.fmtCents(L.messages), ledLbl);
      setTile('tips', Fastt.fmtCents(L.tips), ledLbl);
      setTile('posts', Fastt.fmtCents(L.posts), ledLbl);
      if (P) setTile('streams', Fastt.fmtMoney(P.stream.gross), 'OF payouts · gross',
        'The relay ledger has no stream kind; live-stream money only exists in OF payouts.');
      else setTile('streams', 'n/a', 'OF payouts unavailable');
      $('#kpi-total').textContent = Fastt.fmtCents(lTotal);
      $('#kpi-total-src').textContent = 'relay ledger · gross' + (lOther ? ' (incl. other kinds)' : '');
    }
    if (BASIS === 'net' && !P) {
      // Never let a "Net" label sit over ledger gross numbers.
      setTile('subscriptions', Fastt.fmtCents(L.subscriptions), ledLbl + ' — payouts unavailable');
      setTile('messages', Fastt.fmtCents(L.messages), ledLbl + ' — payouts unavailable');
      setTile('tips', Fastt.fmtCents(L.tips), ledLbl + ' — payouts unavailable');
      setTile('posts', Fastt.fmtCents(L.posts), ledLbl);
      setTile('streams', 'n/a', 'OF payouts unavailable');
      $('#kpi-total').textContent = Fastt.fmtCents(lTotal);
      $('#kpi-total-src').textContent = 'relay ledger · gross — OF payouts unavailable';
    }

    // Referrals: OF's own referral balance. It is a LIFETIME balance, not a
    // windowed figure, and OF's payments API reports DOLLARS (a float) — do
    // not run it through fmtCents.
    try {
      var ref = await Fastt.get('/api/of/v2/payments/referrals/balance');
      var v = Number(ref && ref.referralEarnings);
      setTile('referrals', Fastt.fmtMoney(isFinite(v) ? v : 0), 'OF balance · all-time',
        'GET /api/of/v2/payments/referrals/balance → referralEarnings (dollars, lifetime — not scoped to the period above).');
    } catch (e) {
      setTile('referrals', 'n/a', 'OF referrals API unavailable');
    }
  }

  // ── fans & subscriptions strip ───────────────────────────────────
  // GOTCHA (app/hooks/useStats.ts): use `subscribers.*` — the fans OF this
  // creator — NOT `subscriptions.*`, which counts creators this account
  // follows (1201 here vs 42 real fans).
  function setFan(key, value, sub) {
    var t = $('.fanstile[data-fan="' + key + '"]');
    if (!t) return;
    t.querySelector('.fv').textContent = value;
    if (sub != null) t.querySelector('.fs').textContent = sub;
  }
  async function loadFans() {
    var w = segWindow();
    var note = $('#fans-note');
    var counts = null, chart = null;
    try { counts = await Fastt.get('/api/of/v2/subscriptions/count/all'); } catch (e) { counts = null; }
    var s = counts && counts.subscribers;
    if (s && typeof s === 'object') {
      setFan('active', Fastt.fmtInt(s.active));
      setFan('online', Fastt.fmtInt(s.activeOnline));
      setFan('expired', Fastt.fmtInt(s.expired));
      setFan('all', Fastt.fmtInt(s.all));
    } else {
      ['active', 'online', 'expired', 'all'].forEach(function (k) {
        setFan(k, 'n/a', 'OF returned no subscribers block');
      });
    }
    try {
      chart = await Fastt.get('/api/of/v2/subscriptions/subscribers/chart',
        { start: w.from, end: w.to });
    } catch (e) { chart = null; }
    // Use the `subscribes` series, NOT `earnings` — on free pages the
    // earnings series is all zeros and would report "0 new fans" forever.
    if (chart && Array.isArray(chart.subscribes)) {
      var n = 0;
      chart.subscribes.forEach(function (p) {
        var d = String(p.date || '').slice(0, 10);
        if (d >= w.from && d <= w.to) n += Number(p.count) || 0;
      });
      setFan('new', Fastt.fmtInt(n), w.label.toLowerCase() + ' · ' + w.from + ' → ' + w.to);
    } else {
      setFan('new', 'n/a', 'OF subscribers chart unavailable');
    }
    if (note) {
      note.innerHTML = '<b>Source:</b> OF <code>subscriptions/count/all</code> → <code>subscribers.*</code> '
        + '(the fans subscribed to this creator). The sibling <code>subscriptions.*</code> block counts '
        + 'creators <i>this account follows</i> and is deliberately not shown here. New fans come from '
        + 'OF <code>subscriptions/subscribers/chart</code>, <code>subscribes</code> series (free-inclusive).';
    }
  }

  // ── daily earnings trend (its own range, independent of the segment) ──
  var TREND_DAYS = 30;
  function niceCeil(cents) {
    if (cents <= 0) return 100;
    var p = Math.pow(10, Math.floor(Math.log10(cents)));
    var steps = [1, 2, 2.5, 5, 10];
    for (var i = 0; i < steps.length; i++) {
      if (steps[i] * p >= cents) return steps[i] * p;
    }
    return 10 * p;
  }
  function fmtAxis(cents) {
    var d = cents / 100;
    if (d >= 1000) return '$' + (d / 1000).toFixed(d >= 10000 ? 0 : 1) + 'k';
    return '$' + Math.round(d);
  }
  async function loadTrend() {
    var plot = $('#trend-plot'), yax = $('#trend-y'), note = $('#trend-note');
    if (!plot) return;
    var DAY = 86400000;
    var now = new Date();
    var today = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate()));
    var start = new Date(today.getTime() - (TREND_DAYS - 1) * DAY);
    var iso = function (d) { return d.toISOString().slice(0, 10); };
    var out = await Fastt.get('/admin/stats/revenue',
      { group_by: 'day', from: iso(start), to: iso(today) });
    var byDay = {};
    (out.rows || []).forEach(function (r) { if (r.day) byDay[r.day] = r.total_cents || 0; });

    // Fill every calendar day so gaps read as real zero-revenue days.
    var days = [];
    for (var i = 0; i < TREND_DAYS; i++) {
      var d = iso(new Date(start.getTime() + i * DAY));
      days.push({ day: d, cents: byDay[d] || 0 });
    }
    var peak = days.reduce(function (m, d) { return Math.max(m, d.cents); }, 0);
    var total = days.reduce(function (m, d) { return m + d.cents; }, 0);

    plot.querySelectorAll('.trend-bars, .es-empty').forEach(function (n) { n.remove(); });
    if (!peak) {
      var empty = document.createElement('div');
      empty.className = 'es-empty';
      empty.textContent = 'No ledger revenue for this creator in the last ' + TREND_DAYS + ' days';
      plot.appendChild(empty);
      if (yax) yax.querySelectorAll('span').forEach(function (s) { s.textContent = ''; });
      if (note) note.textContent = 'Relay ledger, gross cents, bucketed by UTC day (GET /admin/stats/revenue?group_by=day).';
      return;
    }
    var max = niceCeil(peak);
    if (yax) {
      var labels = [max, max * 0.75, max * 0.5, max * 0.25, 0];
      yax.querySelectorAll('span').forEach(function (s, i) { s.textContent = fmtAxis(labels[i]); });
    }
    var bars = document.createElement('div');
    bars.className = 'trend-bars';
    var every = TREND_DAYS <= 7 ? 1 : (TREND_DAYS <= 30 ? 5 : 15);
    days.forEach(function (d, i) {
      var col = document.createElement('div');
      col.className = 'tcol';
      col.title = d.day + ' — ' + Fastt.fmtCents(d.cents);
      var b = document.createElement('div');
      b.className = 'tb' + (d.cents ? '' : ' zero');
      // reserve the 28px x-label band so a peak bar can't overlap the labels
      b.style.height = d.cents
        ? 'calc((100% - 28px) * ' + (d.cents / max).toFixed(4) + ')'
        : '2px';
      col.appendChild(b);
      if (i % every === 0 || i === days.length - 1) {
        var x = document.createElement('div');
        x.className = 'tx';
        x.textContent = d.day.slice(5);
        col.appendChild(x);
      }
      bars.appendChild(col);
    });
    plot.appendChild(bars);
    if (note) {
      note.innerHTML = '<b>' + Fastt.fmtCents(total) + '</b> over ' + TREND_DAYS + ' days · peak '
        + Fastt.fmtCents(peak) + ' on ' + Fastt.esc(days.reduce(function (a, b) { return b.cents > a.cents ? b : a; }).day)
        + ' — relay ledger, gross cents, bucketed by UTC day.';
    }
  }
  $$('#trend-chips .chip').forEach(function (b) {
    b.addEventListener('click', function () {
      $$('#trend-chips .chip').forEach(function (x) { x.classList.remove('on'); });
      b.classList.add('on');
      TREND_DAYS = Number(b.dataset.days) || 30;
      loadTrend().catch(Fastt.oops);
    });
  });

  // ── creators overview (the WHOLE roster, not this creator) ───────
  var KIND_LABEL = { ppv: 'PPV', post: 'Post', tip: 'Tip', subscription: 'Sub', rebill: 'Rebill', custom: 'Custom' };
  var AV = ['#e0679b', '#5b8def', '#67d1ae', '#a78bfa', '#e5a35b', '#e07a5f'];
  function avColor(id) { // stable color per account so sorting doesn't reshuffle avatars
    var s = String(id), h = 0;
    for (var i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
    return AV[h % AV.length];
  }
  // Every metric the table + friendlier controls read straight off the loaded
  // rows; null (arpu/ltv with no denominator) sinks to the bottom on any sort.
  function sortVal(r, key) {
    switch (key) {
      case 'name':     return (r.display_name || ('Account ' + r.account_id)).toLowerCase();
      case 'revenue':  return r.total_revenue_cents || 0;
      case 'pending':  return r.pending_revenue_cents || 0;
      case 'fans':     return r.active_subs_count == null ? -1 : r.active_subs_count;
      case 'new_subs': return r.new_subs_count || 0;
      case 'arpu':     return r.arpu_cents == null ? null : r.arpu_cents;
      case 'ltv':      return r.ltv_cents == null ? null : r.ltv_cents;
      case 'msgs':     return r.messages_sent || 0;
      case 'ppvs':     return r.ppv_conversions || 0;
    }
    return 0;
  }

  var CRE_ROWS = [];              // full roster from the last fetch
  var CRE_META = {};              // { from, to, active_subs_source, unavailable }
  var CRE_SORT = { key: 'revenue', dir: -1 };   // default: revenue, descending
  var CRE_Q = '';                 // live search text

  async function loadCreators() {
    var w = segWindow();
    // NO account_id — with one the endpoint returns a single row. noAccount
    // also drops the X-Account-Id header, which this route ignores anyway.
    var out = await Fastt.get('/admin/stats/per-model',
      { from: w.from, to: w.to }, { noAccount: true });
    CRE_ROWS = (out.per_model || []).slice();
    CRE_META = {
      from: out.from || w.from, to: out.to || w.to,
      active_subs_source: out.active_subs_source || 'unknown',
      unavailable: out.active_subs_source === 'unavailable',
    };
    renderCreators();
  }

  function renderCreators() {
    var body = $('#cre-body'), note = $('#cre-note'), cnt = $('#cre-count');
    if (!body) return;
    var me = String(Fastt.account() || '');
    var unavailable = CRE_META.unavailable;

    // filter (search) → sort → render. Nulls always sink regardless of dir.
    var q = CRE_Q.trim().toLowerCase();
    var rows = CRE_ROWS.filter(function (r) {
      if (!q) return true;
      var name = (r.display_name || ('Account ' + r.account_id)).toLowerCase();
      return name.indexOf(q) !== -1 || String(r.account_id).indexOf(q) !== -1;
    });
    var sk = CRE_SORT.key, dir = CRE_SORT.dir;
    rows.sort(function (a, b) {
      var va = sortVal(a, sk), vb = sortVal(b, sk);
      if (va == null && vb == null) return 0;
      if (va == null) return 1;           // nulls last, both directions
      if (vb == null) return -1;
      if (typeof va === 'string') return dir * va.localeCompare(vb);
      return dir * (va - vb);
    });

    // reflect the active sort in the header carets
    $$('#cre-tbl th.sortable').forEach(function (th) {
      var on = th.dataset.sk === sk;
      th.classList.toggle('sorted', on);
      var arr = th.querySelector('.sarr');
      if (arr) arr.textContent = on ? (dir === -1 ? '▼' : '▲') : '';
    });

    body.innerHTML = '';
    if (!rows.length) {
      body.innerHTML = '<tr><td colspan="10" class="panel-empty">' +
        (CRE_ROWS.length
          ? 'No creators match “' + Fastt.esc(CRE_Q) + '”'
          : 'No creators returned for ' + Fastt.esc(CRE_META.from) + ' → ' + Fastt.esc(CRE_META.to)) +
        '</td></tr>';
    }
    var dash = '<span class="dash" title="no value for this creator in the window — a real absence, not a zero">—</span>';
    rows.forEach(function (r) {
      var tr = document.createElement('tr');
      if (String(r.account_id) === me) tr.className = 'me';
      var name = r.display_name || ('Account ' + r.account_id);
      var kinds = r.revenue_by_kind || {};
      var chips = Object.keys(KIND_LABEL)
        .filter(function (k) { return (kinds[k] || 0) > 0; })
        .map(function (k) {
          return '<span class="kchip" title="' + Fastt.esc(KIND_LABEL[k]) + ' ' + Fastt.fmtCents(kinds[k]) + '">'
            + Fastt.esc(KIND_LABEL[k]) + ' ' + Fastt.fmtCents(kinds[k]) + '</span>';
        }).join('');
      var pending = r.pending_revenue_cents || 0;
      var arpu = r.arpu_cents == null ? dash : Fastt.fmtCents(r.arpu_cents);
      var ltv = r.ltv_cents == null ? dash : Fastt.fmtCents(r.ltv_cents);
      var fans = unavailable
        ? '<span class="kchip warn" title="The backend could not read active subscription state, so active subs and ARPU are unavailable for every creator.">subs unavailable</span>'
        : Fastt.fmtInt(r.active_subs_count);
      tr.innerHTML =
        '<td><span class="cname"><span class="cav" style="background:' + avColor(r.account_id) + '">'
          + Fastt.esc(String(name).charAt(0).toUpperCase()) + '</span>' + Fastt.esc(name) + '</span></td>' +
        '<td class="rev">' + Fastt.fmtCents(r.total_revenue_cents || 0) + '</td>' +
        '<td><span class="kchips">' + (chips || '<span class="kchip">no revenue</span>') + '</span></td>' +
        '<td' + (pending ? ' title="clears by ' + Fastt.esc(r.pending_clears_by || 'unknown') + '"' : '') + '>'
          + Fastt.fmtCents(pending) + '</td>' +
        '<td>' + fans + '</td>' +
        '<td>' + Fastt.fmtInt(r.new_subs_count) + '</td>' +
        '<td>' + arpu + '</td>' +
        '<td>' + ltv + '</td>' +
        '<td>' + Fastt.fmtInt(r.messages_sent) + '</td>' +
        '<td>' + Fastt.fmtInt(r.ppv_conversions) + '</td>';
      body.appendChild(tr);
    });

    // ── at-a-glance stat tiles (over the FILTERED set, like the real app) ──
    var totRev = 0, totMsg = 0, totPpv = 0;
    rows.forEach(function (r) {
      totRev += r.total_revenue_cents || 0;
      totMsg += (r.revenue_by_kind && r.revenue_by_kind.ppv) || 0;
      totPpv += r.ppv_conversions || 0;
    });
    function setStat(k, v) { var e = $('.crestat .csv[data-cs="' + k + '"]'); if (e) e.textContent = v; }
    setStat('creators', Fastt.fmtInt(rows.length));
    setStat('revenue', Fastt.fmtCents(totRev));
    setStat('messages', Fastt.fmtCents(totMsg));
    setStat('ppvs', Fastt.fmtInt(totPpv));
    var csub = $('.crestat .css[data-css="creators"]');
    if (csub) csub.textContent = q ? 'of ' + CRE_ROWS.length + ' · filtered' : 'in the roster';

    if (cnt) cnt.textContent = (q && rows.length !== CRE_ROWS.length)
      ? rows.length + ' of ' + CRE_ROWS.length + ' creators'
      : CRE_ROWS.length + ' creators';
    if (note) {
      note.innerHTML =
        '<b>Window</b> ' + Fastt.esc(CRE_META.from) + ' → ' + Fastt.esc(CRE_META.to) + ' (UTC), gross ledger cents. '
        + '<b>Fans</b> = active subscriptions, source <code>' + Fastt.esc(CRE_META.active_subs_source) + '</code>'
        + (CRE_META.active_subs_source === 'transactions_31d'
            ? ' — derived from paid transactions in the last 31 days, so free pages correctly read 0.' : '.')
        + ' <b>ARPU / LTV</b> show <span class="dash">—</span> when the creator has no denominator in the window '
        + '(no fans / no new subs) — that is a real absence, not a zero. '
        + '<b>Click a column header</b> to sort; <b>Export CSV</b> saves exactly the rows shown.';
    }
  }

  // ── friendlier controls: search · sortable headers · CSV export ──
  var creQ = $('#cre-q');
  if (creQ) {
    creQ.addEventListener('input', function () { CRE_Q = creQ.value; renderCreators(); });
  }
  $$('#cre-tbl th.sortable').forEach(function (th) {
    th.addEventListener('click', function () {
      var k = th.dataset.sk;
      if (CRE_SORT.key === k) CRE_SORT.dir = -CRE_SORT.dir;
      // sensible first-click direction: names A→Z, numbers high→low
      else CRE_SORT = { key: k, dir: k === 'name' ? 1 : -1 };
      renderCreators();
    });
  });
  var creExport = $('#cre-export');
  if (creExport) {
    creExport.addEventListener('click', function () { exportCreatorsCsv(); });
  }
  function csvField(v) {
    var s = String(v == null ? '' : v);
    return /[",\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
  }
  function exportCreatorsCsv() {
    // Export the CURRENTLY shown rows (search + sort applied), matching the
    // real app's Export. Pure client-side Blob download — no network, no send.
    var q = CRE_Q.trim().toLowerCase();
    var rows = CRE_ROWS.filter(function (r) {
      if (!q) return true;
      var name = (r.display_name || ('Account ' + r.account_id)).toLowerCase();
      return name.indexOf(q) !== -1 || String(r.account_id).indexOf(q) !== -1;
    });
    var sk = CRE_SORT.key, dir = CRE_SORT.dir;
    rows.sort(function (a, b) {
      var va = sortVal(a, sk), vb = sortVal(b, sk);
      if (va == null && vb == null) return 0;
      if (va == null) return 1; if (vb == null) return -1;
      if (typeof va === 'string') return dir * va.localeCompare(vb);
      return dir * (va - vb);
    });
    if (!rows.length) { Fastt.oops('Nothing to export for this filter'); return; }
    var dollars = function (c) { return ((c || 0) / 100).toFixed(2); };
    var lines = [];
    lines.push('# Creators overview export');
    lines.push('# window,' + CRE_META.from + ' → ' + CRE_META.to + ' (UTC)');
    lines.push('# active_subs_source,' + CRE_META.active_subs_source);
    lines.push('# generated,' + new Date().toISOString());
    lines.push('');
    lines.push(['account_id', 'creator', 'revenue_usd', 'ppv_usd', 'post_usd', 'tip_usd',
      'sub_usd', 'rebill_usd', 'pending_usd', 'active_fans', 'new_subs',
      'arpu_usd', 'ltv_usd', 'messages_sent', 'ppvs_sold'].join(','));
    rows.forEach(function (r) {
      var k = r.revenue_by_kind || {};
      lines.push([
        r.account_id, csvField(r.display_name || ''),
        dollars(r.total_revenue_cents), dollars(k.ppv), dollars(k.post), dollars(k.tip),
        dollars(k.subscription), dollars(k.rebill), dollars(r.pending_revenue_cents),
        r.active_subs_count == null ? '' : r.active_subs_count,
        r.new_subs_count || 0,
        r.arpu_cents == null ? '' : dollars(r.arpu_cents),
        r.ltv_cents == null ? '' : dollars(r.ltv_cents),
        r.messages_sent || 0, r.ppv_conversions || 0,
      ].join(','));
    });
    var blob = new Blob([lines.join('\n')], { type: 'text/csv;charset=utf-8' });
    var url = URL.createObjectURL(blob);
    var a = document.createElement('a');
    a.href = url;
    a.download = 'creators-overview_' + CRE_META.from + '_' + CRE_META.to + '.csv';
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(function () { URL.revokeObjectURL(url); }, 0);
    Fastt.toast(rows.length + ' creator' + (rows.length === 1 ? '' : 's') + ' exported');
  }

  // ── employee sales: /admin/stats/per-employee (by_account, scoped
  // client-side — the endpoint has no account_id param) ─────────────
  async function loadEmployeeSales() {
    var w = segWindow();
    var out = await Fastt.get('/admin/stats/per-employee',
      { from: w.from, to: w.to, by_account: 'true' });
    var acct = String(Fastt.account() || '');
    var rows = (out.employees || []).map(function (e) {
      var sub = (e.per_account || []).find(function (p) {
        return String(p.account_id) === acct;
      });
      return {
        name: e.display_name || (e.employee_id != null ? 'Employee #' + e.employee_id : 'Unattributed'),
        cents: sub ? (sub.revenue_cents || 0) : 0,
      };
    }).filter(function (r) { return r.cents > 0; });
    rows.sort(function (a, b) { return b.cents - a.cents; });
    rows = rows.slice(0, 8);

    var plot = $('#es-plot');
    if (!plot) return;
    plot.querySelectorAll('.esbars, .es-empty').forEach(function (n) { n.remove(); });

    var yaxis = $('#es-yaxis');
    if (!rows.length) {
      if (yaxis) yaxis.querySelectorAll('span').forEach(function (s) { s.textContent = ''; });
      var empty = document.createElement('div');
      empty.className = 'es-empty';
      empty.textContent = 'No attributed sales in this period';
      plot.appendChild(empty);
      return;
    }

    var max = niceCeil(rows[0].cents);
    if (yaxis) {
      var labels = [max, max * 0.75, max * 0.5, max * 0.25, 0];
      yaxis.querySelectorAll('span').forEach(function (s, i) {
        s.textContent = labels[i] != null ? fmtAxis(labels[i]) : '';
      });
    }
    var wrap = document.createElement('div');
    wrap.className = 'esbars';
    rows.forEach(function (r) {
      var col = document.createElement('div');
      col.className = 'escol';
      col.title = r.name + ' — ' + Fastt.fmtCents(r.cents);
      var val = document.createElement('div');
      val.className = 'val';
      val.textContent = Fastt.fmtCents(r.cents);
      var bar = document.createElement('div');
      bar.className = 'bar';
      // column height also holds the value + name labels (~42px): scale
      // against the remaining band so tall bars can't push labels out.
      bar.style.height = 'calc((100% - 42px) * ' + (r.cents / max).toFixed(4) + ')';
      var nm = document.createElement('div');
      nm.className = 'nm';
      nm.textContent = r.name;
      col.appendChild(val); col.appendChild(bar); col.appendChild(nm);
      wrap.appendChild(col);
    });
    plot.appendChild(wrap);
  }

  // ── ops pill: rev probe + ingest health for the current account ──
  // The pill wears a LIVE badge, so it must never stay green on a failure:
  // a throw (network/500) flips it red exactly like an `error` payload does.
  async function loadOps() {
    var dot = $('#ops-dot'), txt = $('#ops-status');
    if (!dot || !txt) return;
    function fail(msg) { dot.style.background = '#ff5f57'; txt.textContent = msg; }
    var rev;
    try { rev = await Fastt.get('/admin/rev/live'); }
    catch (e) { fail('Relay unreachable'); throw e; }
    if (rev && rev.error) {
      fail('Relay error');
      return;
    }
    var health;
    try { health = await Fastt.get('/admin/ingest/transactions/health'); }
    catch (e) { fail('Health check failed'); throw e; }
    var acct = String(Fastt.account() || '');
    var row = (health.accounts || []).find(function (a) {
      return String(a.account_id) === acct;
    });
    if (!row) { txt.textContent = 'No ingest data'; dot.style.background = '#febc2e'; return; }
    if (row.tier === 'green') { txt.textContent = 'Operational'; dot.style.background = '#3ec46a'; }
    else if (row.tier === 'yellow') { txt.textContent = 'Ingest lagging'; dot.style.background = '#febc2e'; }
    else { txt.textContent = 'Ingest stale'; dot.style.background = '#ff5f57'; }
  }

  // ── sidebar unread badge: /admin/chats/recent (unread chats) ─────
  async function loadUnread() {
    var badge = $('#msg-badge');
    if (!badge) return;
    var out = await Fastt.get('/admin/chats/recent', { limit: 25 });
    var n = (out.list || []).filter(function (c) { return c.hasUnread; }).length;
    badge.textContent = String(n);
    badge.style.display = n ? '' : 'none';
  }

  // ── boot + segment refresh ───────────────────────────────────────
  // NOTE: the #modelswap topbar switcher is mounted centrally by fastt.js —
  // do not re-bind it here.
  // ── earnings by channel (multi-line, ALL models — no account_id) ──
  // Same approach as the reference dashboard: the ledger groups by day OR kind,
  // not both, so each channel line = the day total split by that channel's share
  // of the window. Referrals/Streams aren't ledger kinds → they read $0.
  var CHAN_DEF = [
    { key: 'subscriptions', label: 'Subscriptions', color: '#3b82f6' },
    { key: 'tips',          label: 'Tips',          color: '#10b981' },
    { key: 'posts',         label: 'Posts',         color: '#ef4444' },
    { key: 'messages',      label: 'Messages',      color: '#f59e0b' },
    { key: 'referrals',     label: 'Referrals',     color: '#6366f1' },
    { key: 'streams',       label: 'Streams',       color: '#a855f7' },
  ];
  function chanBucket(kind) {
    if (!kind) return null;
    if (kind === 'ppv_post') return 'posts';
    if (kind === 'ppv' || kind === 'ppv_message') return 'messages';
    if (kind === 'tip') return 'tips';
    if (kind === 'subscription' || kind === 'rebill') return 'subscriptions';
    return null;
  }
  function chanNiceMax(v) { if (v <= 0) return 400; var p = Math.pow(10, Math.floor(Math.log10(v))); var n = v / p; return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10) * p; }
  function chanMoney(c) { var d = c / 100; return d >= 1000 ? '$' + (d / 1000).toFixed(1) + 'k' : '$' + Math.round(d); }
  function blankChannel(msg) {
    var svg = $('#chan-chart'), legend = $('#chan-legend'), note = $('#chan-note');
    if (svg) svg.innerHTML = '<g stroke="#2c2c2c" stroke-dasharray="3 5">' +
      [20, 78, 137, 195].map(function (y) { return '<line x1="46" y1="' + y + '" x2="996" y2="' + y + '"/>'; }).join('') +
      '</g><line x1="46" y1="254" x2="996" y2="254" stroke="#3a3a3a"/>' +
      (msg ? '<text x="520" y="140" text-anchor="middle" fill="#8a8a8a" font-family="Inter, sans-serif" font-size="14">' + Fastt.esc(msg) + '</text>' : '');
    if (legend) legend.innerHTML = '';
    if (note) note.textContent = '';
  }
  function drawChannel(pts, shares) {
    var svg = $('#chan-chart'), legend = $('#chan-legend'), note = $('#chan-note');
    if (!svg) return;
    var grand = CHAN_DEF.reduce(function (a, c) { return a + (shares[c.key] || 0); }, 0) || 0;
    if (legend) legend.innerHTML = CHAN_DEF.map(function (c) {
      var v = shares[c.key] || 0, pc = grand > 0 ? (100 * v / grand).toFixed(2) : '0.00';
      return '<div class="lg"><span class="dot" style="background:' + c.color + '"></span>' +
        '<span class="nm">' + c.label + '</span><span class="pc">' + pc + '%</span>' +
        '<span class="am">' + Fastt.fmtCents(v) + '</span></div>';
    }).join('');
    var L = 46, R = 996, T = 20, B = 254;
    var series = CHAN_DEF.map(function (c) { return { c: c, share: grand > 0 ? shares[c.key] / grand : 0 }; }).filter(function (s) { return s.share > 0; });
    var vals = pts.map(function (p) { return p.v; });
    var maxLine = series.length ? Math.max.apply(null, series.map(function (s) { return Math.max.apply(null, vals.map(function (v) { return v * s.share; })); }).concat([0])) : 0;
    var maxV = chanNiceMax(maxLine);
    var X = function (i) { return pts.length === 1 ? (L + R) / 2 : L + i * (R - L) / (pts.length - 1); };
    var Y = function (v) { return B - (v / maxV) * (B - T); };
    var fr = [1, .75, .5, .25, 0], labY = [25, 83, 141, 199, 258];
    var h = '<g font-family="Inter, sans-serif" font-size="14" fill="#6a6a6a">';
    fr.forEach(function (f, i) { h += '<text x="8" y="' + labY[i] + '">' + chanMoney(maxV * f) + '</text>'; });
    h += '</g><g stroke="#2c2c2c" stroke-dasharray="3 5">';
    [20, 78, 137, 195].forEach(function (y) { h += '<line x1="46" y1="' + y + '" x2="996" y2="' + y + '"/>'; });
    h += '</g><line x1="46" y1="254" x2="996" y2="254" stroke="#3a3a3a"/>';
    var step = Math.max(1, Math.ceil(pts.length / 8));
    h += '<g font-family="Inter, sans-serif" font-size="14" fill="#8a8a8a">';
    pts.forEach(function (p, i) {
      if (i % step && i !== pts.length - 1) return;
      var anchor = i === 0 ? 'start' : (i === pts.length - 1 ? 'end' : 'middle');
      h += '<text x="' + X(i).toFixed(1) + '" y="284" text-anchor="' + anchor + '">' + Fastt.esc(p.label) + '</text>';
    });
    h += '</g>';
    series.forEach(function (s) {
      h += '<polyline points="' + pts.map(function (p, i) { return X(i).toFixed(1) + ',' + Y(p.v * s.share).toFixed(1); }).join(' ') +
        '" fill="none" stroke="' + s.c.color + '" stroke-width="2.5" stroke-linejoin="round"/>';
    });
    svg.innerHTML = h;
    if (note) note.textContent = 'All models combined · each channel line = the day total split by that channel’s share of the window (ledger groups by day OR kind). Gross cents.';
  }
  async function loadChannel() {
    var w = segWindow(), svg = $('#chan-chart');
    if (!svg) return;
    var dayRes, kindRes;
    try {
      dayRes = await Fastt.get('/admin/stats/revenue', { group_by: 'day', from: w.from, to: w.to }, { noAccount: true });
      kindRes = await Fastt.get('/admin/stats/revenue', { group_by: 'kind', from: w.from, to: w.to }, { noAccount: true });
    } catch (e) { blankChannel('Earnings by channel unavailable'); Fastt.staticBadge($('#chan-head'), 'NO LIVE DATA'); Fastt.oops(e); return; }
    var shares = { subscriptions: 0, tips: 0, posts: 0, messages: 0, referrals: 0, streams: 0 };
    (kindRes.rows || []).forEach(function (r) { var b = chanBucket(r.kind); if (b) shares[b] += (r.total_cents || 0); });
    var DAY = 86400000, isoD = function (d) { return d.toISOString().slice(0, 10); };
    var start = new Date(w.from + 'T00:00:00Z'), end = new Date(w.to + 'T00:00:00Z');
    var byDay = {}; (dayRes.rows || []).forEach(function (r) { if (r.day) byDay[r.day] = (byDay[r.day] || 0) + (r.total_cents || 0); });
    var pts = []; for (var t = start.getTime(); t <= end.getTime(); t += DAY) { var d = isoD(new Date(t)); pts.push({ label: d.slice(5), v: byDay[d] || 0 }); }
    if (!pts.length) { blankChannel('No days in the selected window'); Fastt.staticBadge($('#chan-head'), 'EMPTY WINDOW'); return; }
    drawChannel(pts, shares);
    Fastt.liveBadge($('#chan-head'));
  }

  async function refresh() {
    try { await loadEarnings(); } catch (e) { Fastt.oops(e); }
    try { await loadChannel(); } catch (e) { Fastt.oops(e); }
    try { await loadEmployeeSales(); } catch (e) { Fastt.oops(e); }
    try { await loadFans(); } catch (e) { Fastt.oops(e); }
    try { await loadCreators(); } catch (e) { Fastt.oops(e); }
  }

  // No creator selected → without an account_id the /admin stats queries
  // go global (all accounts). Leave the placeholders rather than show
  // numbers that belong to nobody; fastt.js already banners the fix.
  // The segment buttons stay unbound below this guard on purpose: one
  // click would otherwise pull cross-account totals into these tiles.
  if (!Fastt.account()) {
    // …and don't leave the baked mock numbers reading as this creator's.
    var badge0 = $('#msg-badge');
    if (badge0) { badge0.textContent = '0'; badge0.style.display = 'none'; }
    var opsTxt0 = $('#ops-status'), opsDot0 = $('#ops-dot');
    if (opsTxt0) opsTxt0.textContent = 'No creator selected';
    if (opsDot0) opsDot0.style.background = '#8a8a8a';
    Fastt.staticBadge($('#ops-pill'), 'NO DATA');
    // The roster table + its own chart are agency-wide, so they are still
    // real without a creator scope — render them and honestly blank the rest.
    ['#fans-card', '#trend-card'].forEach(function (sel) {
      var c = $(sel);
      if (!c) return;
      Fastt.staticBadge(c.querySelector('.card-h'), 'NO CREATOR SELECTED');
    });
    $$('.fanstile .fv').forEach(function (v) { v.textContent = 'n/a'; });
    var tplot = $('#trend-plot');
    if (tplot) {
      var te = document.createElement('div');
      te.className = 'es-empty';
      te.textContent = 'Pick a creator to load their daily ledger';
      tplot.appendChild(te);
    }
    Fastt.liveBadge($('#cre-head'));
    try { await loadCreators(); } catch (e) { Fastt.oops(e); }
    try { await loadChannel(); } catch (e) { Fastt.oops(e); }   // agency-wide, needs no creator scope
    return;
  }

  document.querySelectorAll('#seg button').forEach(function (b) {
    b.addEventListener('click', function () { refresh(); });
  });
  Fastt.liveBadge($('.sec-title'));
  Fastt.liveBadge($('#es-head'));
  Fastt.liveBadge($('#fans-head'));
  Fastt.liveBadge($('#trend-head'));
  Fastt.liveBadge($('#cre-head'));
  Fastt.liveBadge($('#ops-pill'));
  await refresh();
  try { await loadTrend(); } catch (e) { Fastt.oops(e); }
  try { await loadOps(); } catch (e) { Fastt.oops(e); }
  try { await loadUnread(); } catch (e) { Fastt.oops(e); }
});
