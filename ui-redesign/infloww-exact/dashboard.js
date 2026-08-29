// ==== LIVE WIRING (relay data via _shared/fastt.js) ====
//
// EVERY money number on this page is described by THREE axes, and every tile
// prints all three. The old header documented only the first two, and that
// omission WAS the bug: the KPI card was per-creator while the chart directly
// under it was roster-wide, both labelled only "relay ledger · gross", so
// nothing on screen revealed that they counted different creators.
//
//   1. BASIS — gross or net.  BOTH now come from the relay ledger:
//        gross = SUM(transactions.amount_cents) — what the fan paid.
//        net   = SUM(transactions.net_cents)    — OF's OWN post-fee figure,
//                read verbatim from its payload (net = gross − fee; VAT is
//                tracked separately and NOT deducted).
//      Net used to come from GET /api/of/v2/payouts/stats, which is structurally
//      ONE OF session and therefore cannot answer an agency question. Verified
//      2026-08-29 on a closed month: ledger and OF payouts agree to the cent
//      ($1795.36 gross / $1436.25 net), and OF reports no `stream` bucket at
//      all — so nothing is lost by leaving that API behind.
//      ⚠️ Do NOT "simplify" net to gross × 0.8. It is arithmetically identical
//      today, which is exactly the trap: it silently diverges the moment OF
//      applies a referral split or changes its cut.
//
//   2. POPULATION — which creators.  "agency" = every creator the SIGNED-IN
//      PRINCIPAL owns (Fastt.get(..., {scope:"agency"}), WIRING_GUIDE rule 4b);
//      "creator" = the one picked in the topbar. The roster VARIES by viewer —
//      no principal owns every account — so agency tiles say "N creators", never
//      a bare "Total".
//
//   3. STATUS — /admin/stats/revenue and /admin/stats/per-model both restrict to
//      ("cleared","pending"). `pending` IS revenue: the ledger writes rows
//      pending and clears them ~7 days later, so a cleared-only view renders the
//      current week $0.00.
//
// Cards that are deliberately CREATOR-scoped (Fans & subscriptions, the ops
// pill, the bell) say so on their face — they read OF per-session and cannot be
// aggregated without an N-account fan-out this page will not do.
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
  // Gated on a selected creator: with no X-Account-Id the OF proxy falls back to
  // whichever account is "active" server-side, so this used to show a real
  // unread count belonging to a creator the viewer never picked.
  if (Fastt.account()) {
    try {
      var _nc = await Fastt.get('/api/of/v2/users/notifications/count');
      var _bell = Fastt.$('#bell-count');
      var _unread = Number(_nc && _nc.all) || 0;
      if (_bell) {
        _bell.textContent = _unread > 99 ? '99+' : String(_unread);
        _bell.style.display = _unread ? '' : 'none';
        _bell.title = _unread + ' unread OnlyFans notification' + (_unread === 1 ? '' : 's')
          + ' for the selected creator';
      }
    } catch (e) { /* OF unreachable — leave the bell bare */ }
  }
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
  // How many creators the SIGNED-IN PRINCIPAL owns. Not a constant and not 7:
  // on the live box no principal owns the whole roster and three accounts are
  // co-owned, so four operators see four different legitimate agency totals.
  // Filled in by loadCreators()/loadIngestCaveat(); until then the label stays
  // vague rather than asserting a count we have not counted.
  var ROSTER_N = 0;
  function rosterLabel() {
    return ROSTER_N ? (ROSTER_N === 1 ? '1 creator' : ROSTER_N + ' creators') : 'your creators';
  }
  // The section heading sits above the earnings card AND the channel chart, and
  // BOTH are agency-wide now; only the Creators overview table further down is
  // per-creator. It used to read "Creator earnings overview", which described
  // the old single-creator card and would now be a label over the wrong
  // population — the exact failure this whole change exists to remove. Driven
  // off ROSTER_N so the heading counts the same creators the money does.
  function syncEarnHead() {
    var el = $('#earn-head-tx');
    if (!el) return;
    el.textContent =
      ROSTER_N > 1 ? 'Agency earnings · all ' + ROSTER_N + ' creators'
      : ROSTER_N === 1 ? 'Agency earnings · your 1 creator'
      : 'Agency earnings · all creators';
  }
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

  // ── the window's ledger, fetched ONCE and shared ─────────────────
  // group_by=day&by_kind=true returns each day with its kinds nested inside, so
  // ONE request feeds both the KPI card and the channel chart. That is not just
  // fewer round-trips: summing the nested kinds across days IS the kind total,
  // so the card and the chart are now incapable of disagreeing. Previously they
  // were two separate requests at two different SCOPES, which is how a
  // per-creator headline ended up sitting above a roster-wide chart.
  var _win = {};                    // cache key -> promise, one per (from,to)
  function loadWindow(from, to) {
    var key = from + '|' + to;
    if (!_win[key]) {
      _win[key] = Fastt.get('/admin/stats/revenue',
        { group_by: 'day', by_kind: true, include_net: true, from: from, to: to },
        { scope: 'agency' });
    }
    return _win[key];
  }

  // Ledger kind -> tile. ONE mapper for the KPI card and the channel chart;
  // they used to carry byte-identical private copies (kpiBucket / chanBucket)
  // that could drift apart silently.
  //
  // Covers the writer's FULL catalogue (service/transaction_ingest.py
  // KIND_CATALOG), not just the five kinds this DB happens to contain. The old
  // comment here claimed "there is no ledger kind for Streams" and cited
  // /admin/ingest/transactions/unknown-kinds as proof — that endpoint filters
  // kind == 'unknown', so it is empty BY CONSTRUCTION for every catalogued kind
  // and proved nothing. `tip_stream`/`ppv_stream` are real ledger kinds; this
  // account set has simply never produced one.
  function tileBucket(kind) {
    if (!kind) return null;
    if (kind === 'ppv_post' || kind === 'tip_post') return 'posts';
    if (kind === 'ppv_stream' || kind === 'tip_stream') return 'streams';
    if (kind === 'ppv' || kind === 'ppv_message') return 'messages';
    if (kind === 'tip') return 'tips';
    if (kind === 'subscription' || kind === 'rebill') return 'subscriptions';
    return null;                    // custom / unknown -> disclosed as "other"
  }

  /** Fold the shared window payload into per-tile totals for the active basis. */
  function foldWindow(rows) {
    var B = { subscriptions: 0, posts: 0, messages: 0, tips: 0, streams: 0 };
    var total = 0, other = 0, missingNet = 0;
    var field = BASIS === 'net' ? 'net_cents' : 'total_cents';
    (rows || []).forEach(function (day) {
      (day.by_kind || []).forEach(function (k) {
        var c = k[field] || 0;
        total += c;
        missingNet += k.net_missing_count || 0;
        var b = tileBucket(k.kind);
        if (b) B[b] += c; else other += c;
      });
    });
    return { buckets: B, total: total, other: other, missingNet: missingNet };
  }

  function basisLabel() {
    return 'relay ledger \u00b7 ' + (BASIS === 'net' ? 'net' : 'gross') + ' \u00b7 ' + rosterLabel();
  }

  // ── earnings KPI card (agency-wide, both bases from the ledger) ───
  async function loadEarnings() {
    var w = segWindow();
    var out = await loadWindow(w.from, w.to);
    var f = foldWindow(out.rows);
    var src = basisLabel();

    setTile('subscriptions', Fastt.fmtCents(f.buckets.subscriptions), src);
    setTile('messages', Fastt.fmtCents(f.buckets.messages), src);
    setTile('tips', Fastt.fmtCents(f.buckets.tips), src);
    setTile('posts', Fastt.fmtCents(f.buckets.posts), src);

    // Streams: a REAL agency-wide ledger figure. $0.00 here is a measured zero
    // (this roster has never produced a stream sale), not an absent source, and
    // it will populate itself the day one lands.
    setTile('streams', Fastt.fmtCents(f.buckets.streams), src,
      f.buckets.streams
        ? 'Live-stream sales from ledger kinds ppv_stream + tip_stream.'
        : 'No stream transactions in this window. ppv_stream/tip_stream ARE ledger kinds — this roster has never produced one.');

    // Referrals: no ledger kind exists, and OF's balance is LIFETIME and
    // single-creator — it does not move when the segment changes, so it was
    // already wrong at one creator, never mind the roster. Show the page's
    // "real absence" dash rather than a number that cannot mean what it says.
    var refEl = tile('referrals');
    if (refEl) {
      var rv = refEl.querySelector('.mv');
      if (rv) rv.textContent = '\u2014';
      var rs = refEl.querySelector('.msrc');
      if (rs) rs.textContent = 'no agency-wide source';
      refEl.title = 'OF reports referral earnings only as a LIFETIME, single-creator balance '
        + '(/api/of/v2/payments/referrals/balance). There is no referral kind in the ledger, so '
        + 'there is no windowed, agency-wide figure to show. A dash means "not measurable here", not zero.';
    }

    $('#kpi-total').textContent = Fastt.fmtCents(f.total);
    var note = src
      + (f.other ? ' (incl. other kinds)' : '')
      + (f.missingNet && BASIS === 'net' ? ' \u00b7 ' + f.missingNet + ' row(s) awaiting net backfill' : '')
      + ' \u00b7 cleared + pending';
    $('#kpi-total-src').textContent = note;
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
    var fh = $('#fans-head');
    if (fh && !fh.querySelector('.fscope')) {
      var sc = document.createElement('span');
      sc.className = 'fscope';
      sc.style.cssText = 'margin-left:8px;font-size:11px;color:#8a8a8a;font-weight:500';
      sc.textContent = 'selected creator only';
      fh.appendChild(sc);
    }
    if (note) {
      note.innerHTML = '<b>Scope:</b> the SELECTED creator, not the agency \u2014 OF exposes fan counts '
        + 'per session, and an agency figure would need one OF request per creator (unthrottled, no 429 '
        + 'handling) and would double-count fans subscribed to several creators. Every other card on this '
        + 'page is agency-wide; this one is not, and says so. '
        + '<b>Source:</b> OF <code>subscriptions/count/all</code> → <code>subscribers.*</code> '
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
    // AGENCY scope + basis-aware. This call used to omit the scope entirely,
    // so fastt.js injected the selected creator and the "daily earnings" chart
    // silently plotted ONE creator underneath a roster-wide headline.
    var out = await Fastt.get('/admin/stats/revenue',
      { group_by: 'day', include_net: true, from: iso(start), to: iso(today) },
      { scope: 'agency' });
    var _f = BASIS === 'net' ? 'net_cents' : 'total_cents';
    var byDay = {};
    (out.rows || []).forEach(function (r) { if (r.day) byDay[r.day] = r[_f] || 0; });

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
      empty.textContent = 'No ledger revenue across ' + rosterLabel() + ' in the last ' + TREND_DAYS + ' days';
      plot.appendChild(empty);
      if (yax) yax.querySelectorAll('span').forEach(function (s) { s.textContent = ''; });
      if (note) note.textContent = basisLabel() + ' \u00b7 bucketed by UTC day \u00b7 cleared + pending.';
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
        + ' \u2014 ' + basisLabel() + ', bucketed by UTC day.';
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
    // Agency scope: with an account_id this endpoint returns a single row.
    // Migrated from bare `noAccount:true` to the named option (WIRING_GUIDE 4b)
    // so the page states the POPULATION it wants rather than the mechanism.
    var out = await Fastt.get('/admin/stats/per-model',
      { from: w.from, to: w.to }, { scope: 'agency' });
    CRE_ROWS = (out.per_model || []).slice();
    ROSTER_N = CRE_ROWS.length;      // the principal's roster, not a hardcoded 7
    syncEarnHead();
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

  // ── sidebar unread badge: REMOVED, deliberately ──────────────────
  // This page had its own `loadUnread()` writing #msg-badge from
  // /admin/chats/recent?limit=25, while _shared/fastt.js's mountMsgBadge()
  // writes the SAME element from the same endpoint with limit=100. The limit is
  // applied BEFORE the unread filter, so the two produced different numbers by
  // construction and whichever resolved last won — a nondeterministic badge.
  // fastt.js owns #msg-badge for all 55 pages; the page-local copy is gone
  // rather than reconciled, because two writers was the bug.

  // ── boot + segment refresh ───────────────────────────────────────
  // NOTE: the #modelswap topbar switcher is mounted centrally by fastt.js —
  // do not re-bind it here.
  // ── earnings by channel (REAL per-day-per-kind, agency-wide) ─────
  // Each line is now the actual money booked to that channel on that day, read
  // from group_by=day&by_kind=true.
  //
  // It used to be `day_total x that_channel's share of the WHOLE window`, i.e.
  // six scaled copies of one curve. That was not an approximation, it was
  // invention: on 2026-08-24 and 2026-08-29 real tip revenue was exactly $0 and
  // the chart still drew a tips line, because tips were 3.3% of the window.
  //
  // Drawn as STACKED AREAS, not six independent lines. Real ledger data is
  // sparse — in a typical week ~43% of (day x channel) cells are empty and three
  // of the five channels are flat at zero most days — so overlapping lines at
  // the baseline read as breakage. Stacked, the same sparsity reads as
  // composition, and the silhouette is the day total.
  var CHAN_DEF = [
    { key: 'subscriptions', label: 'Subscriptions', color: '#3b82f6' },
    { key: 'tips',          label: 'Tips',          color: '#10b981' },
    { key: 'posts',         label: 'Posts',         color: '#ef4444' },
    { key: 'messages',      label: 'Messages',      color: '#f59e0b' },
    { key: 'streams',       label: 'Streams',       color: '#a855f7' },
  ];
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
  function drawChannel(pts, totals) {
    var svg = $('#chan-chart'), legend = $('#chan-legend'), note = $('#chan-note');
    if (!svg) return;
    var grand = CHAN_DEF.reduce(function (a, c) { return a + (totals[c.key] || 0); }, 0) || 0;
    if (legend) legend.innerHTML = CHAN_DEF.map(function (c) {
      var v = totals[c.key] || 0, pc = grand > 0 ? (100 * v / grand).toFixed(2) : '0.00';
      return '<div class="lg"><span class="dot" style="background:' + c.color + '"></span>' +
        '<span class="nm">' + c.label + '</span><span class="pc">' + pc + '%</span>' +
        '<span class="am">' + Fastt.fmtCents(v) + '</span></div>';
    }).join('');

    var L = 46, R = 996, T = 20, B = 254;
    // Only stack channels that actually earned in this window; a channel that is
    // zero everywhere adds an invisible band and a dead legend entry.
    var series = CHAN_DEF.filter(function (c) { return (totals[c.key] || 0) > 0; });
    var maxV = chanNiceMax(pts.reduce(function (m, p) {
      return Math.max(m, series.reduce(function (a, c) { return a + (p.v[c.key] || 0); }, 0));
    }, 0));
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

    // Cumulative baselines: band k is drawn between running totals k-1 and k.
    var below = pts.map(function () { return 0; });
    series.forEach(function (c) {
      var upper = pts.map(function (p, i) { return below[i] + (p.v[c.key] || 0); });
      var top = pts.map(function (p, i) { return X(i).toFixed(1) + ',' + Y(upper[i]).toFixed(1); });
      var bot = [];
      for (var i = pts.length - 1; i >= 0; i--) bot.push(X(i).toFixed(1) + ',' + Y(below[i]).toFixed(1));
      h += '<polygon points="' + top.concat(bot).join(' ') + '" fill="' + c.color + '" fill-opacity="0.55"/>';
      h += '<polyline points="' + top.join(' ') + '" fill="none" stroke="' + c.color +
           '" stroke-width="2" stroke-linejoin="round"/>';
      below = upper;
    });
    if (!series.length) {
      h += '<text x="520" y="140" text-anchor="middle" fill="#8a8a8a" font-family="Inter, sans-serif" font-size="14">'
        + 'No revenue in this window</text>';
    }
    svg.innerHTML = h;
    if (note) note.textContent = basisLabel()
      + ' \u00b7 real per-day-per-kind from the ledger (group_by=day&by_kind=true), stacked \u00b7 cleared + pending.';
  }

  async function loadChannel() {
    var w = segWindow(), svg = $('#chan-chart');
    if (!svg) return;
    var out;
    try {
      // Same promise the KPI card awaits — one request, so the legend amounts
      // and the tiles above are literally the same numbers.
      out = await loadWindow(w.from, w.to);
    } catch (e) {
      blankChannel('Earnings by channel unavailable');
      Fastt.setBadge($('#chan-head'), 'static', 'NO LIVE DATA');
      Fastt.oops(e); return;
    }
    var field = BASIS === 'net' ? 'net_cents' : 'total_cents';
    var byDay = {}, totals = { subscriptions: 0, tips: 0, posts: 0, messages: 0, streams: 0 };
    (out.rows || []).forEach(function (r) {
      var per = {};
      (r.by_kind || []).forEach(function (k) {
        var b = tileBucket(k.kind);
        if (!b || !(b in totals)) return;
        per[b] = (per[b] || 0) + (k[field] || 0);
        totals[b] += k[field] || 0;
      });
      byDay[r.day] = per;
    });
    // Fill every calendar day so a gap reads as a real zero, not a missing point.
    var DAY = 86400000, isoD = function (d) { return d.toISOString().slice(0, 10); };
    var start = new Date(w.from + 'T00:00:00Z'), end = new Date(w.to + 'T00:00:00Z');
    var pts = [];
    for (var t = start.getTime(); t <= end.getTime(); t += DAY) {
      var d = isoD(new Date(t));
      pts.push({ label: d.slice(5), v: byDay[d] || {} });
    }
    drawChannel(pts, totals);
    Fastt.setBadge($('#chan-head'), 'live');
  }

  // ── ingest health: what EARNS the word "total" ───────────────────
  // A roster sum is only honest if every creator in it is actually being
  // scanned. On the live box account ACCOUNT_ID is `paused` with 24 consecutive
  // 401s — it contributes $0.00 to every window, and before this line existed a
  // roster-wide headline would have absorbed that silently and looked
  // authoritative. Now the card says how many creators are reporting, and names
  // the ones that are not.
  async function loadIngestCaveat() {
    var srcEl = $('#kpi-total-src');
    if (!srcEl) return;
    var health;
    try { health = await Fastt.get('/admin/ingest/transactions/health', null, { scope: 'agency' }); }
    catch (e) { return; }                      // never let a caveat break the card
    var accts = (health && health.accounts) || [];
    if (!accts.length) return;
    var dark = accts.filter(function (a) {
      return a.tier === 'red' || a.current_status === 'paused'
        || a.current_status === 'never_scanned' || a.fully_backfilled === false;
    });
    ROSTER_N = accts.length;
    syncEarnHead();                            // before the early return below
    if (!dark.length) return;
    var names = dark.map(function (a) { return a.display_name || a.account_id; });
    srcEl.textContent += ' \u00b7 \u26a0 ' + (accts.length - dark.length) + ' of ' + accts.length + ' creators reporting';
    var card = $('.card.earn');
    if (card) {
      card.title = 'Not every creator is being ingested, so this total is a floor, not a complete figure.\n'
        + 'Not reporting: ' + names.join(', ') + '.\n'
        + 'A paused account is usually a dead OF session (401) — it needs re-authenticating, not a code fix.';
    }
  }

  async function refresh() {
    _win = {};                       // one shared fetch per refresh, never stale
    try { await loadEarnings(); } catch (e) { Fastt.oops(e); }
    try { await loadChannel(); } catch (e) { Fastt.oops(e); }
    try { await loadEmployeeSales(); } catch (e) { Fastt.oops(e); }
    try { await loadFans(); } catch (e) { Fastt.oops(e); }
    try { await loadCreators(); } catch (e) { Fastt.oops(e); }
    try { await loadTrend(); } catch (e) { Fastt.oops(e); }
    try { await loadIngestCaveat(); } catch (e) { /* caveat is best-effort */ }
  }

  // ── fail LOUD if the shared client is too old for agency scope ────
  // A fastt.js predating WIRING_GUIDE rule 4b would drop `{scope:"agency"}` on
  // the floor, inject the selected creator's account_id, and render a
  // PER-CREATOR number under an all-creators label — no console error, no 4xx,
  // no visual tell. That is indistinguishable from "the deploy worked", and it
  // is exactly how a stale artifact turns a fix into a week of confusion.
  // Blank the money rather than show a number that might be one creator's.
  if (!Fastt.supportsScope || !Fastt.supportsScope('agency')) {
    ['#chan-card', '#trend-card', '#es-card'].forEach(function (sel) {
      var c = $(sel);
      if (c) Fastt.setBadge(c.querySelector('.card-h'), 'static', 'CLIENT TOO OLD');
    });
    $$('.metric .mv').forEach(function (v) { v.textContent = '\u2014'; });
    $$('.metric .msrc').forEach(function (v) { v.textContent = 'stale client'; });
    var kt = $('#kpi-total'), kts = $('#kpi-total-src');
    if (kt) kt.textContent = '\u2014';
    if (kts) kts.textContent = '_shared/fastt.js is too old for agency scope \u2014 these numbers would be one creator\u2019s. Hard-reload; if it persists the deploy shipped dashboard.js without fastt.js.';
    Fastt.oops('Dashboard needs a newer _shared/fastt.js (agency scope missing)');
    return;
  }

  // Segment buttons drive every agency card, so they bind unconditionally now.
  // They used to sit below the no-creator guard on the reasoning that a click
  // "would otherwise pull cross-account totals into these tiles" — cross-account
  // totals are now the POINT of these tiles, so that reasoning inverted.
  document.querySelectorAll('#seg button').forEach(function (b) {
    b.addEventListener('click', function () { refresh(); });
  });

  // Cards that need a creator, and only those, degrade when none is selected.
  // Earnings / channel / trend / employee-sales / creators are agency-wide and
  // render fine without one.
  if (!Fastt.account()) {
    var badge0 = $('#msg-badge');
    if (badge0) { badge0.textContent = '0'; badge0.style.display = 'none'; }
    var opsTxt0 = $('#ops-status'), opsDot0 = $('#ops-dot');
    if (opsTxt0) opsTxt0.textContent = 'No creator selected';
    if (opsDot0) opsDot0.style.background = '#8a8a8a';
    Fastt.setBadge($('#ops-pill'), 'static', 'NO DATA');
    var fc = $('#fans-card');
    if (fc) Fastt.setBadge(fc.querySelector('.card-h'), 'static', 'NO CREATOR SELECTED');
    $$('.fanstile .fv').forEach(function (v) { v.textContent = 'n/a'; });
  }

  await refresh();

  // Badge AFTER the fetch, never before: these calls used to run ahead of
  // refresh(), and since refresh() swallows each failure into a toast, a card
  // whose loader threw kept a green LIVE pill over stale content.
  Fastt.setBadge($('.sec-title'), 'live');
  Fastt.setBadge($('#es-head'), 'live');
  Fastt.setBadge($('#trend-head'), 'live');
  Fastt.setBadge($('#cre-head'), 'live');
  if (Fastt.account()) {
    Fastt.setBadge($('#fans-head'), 'live');
    Fastt.setBadge($('#ops-pill'), 'live');
    try { await loadOps(); } catch (e) { Fastt.oops(e); }
  }
});

