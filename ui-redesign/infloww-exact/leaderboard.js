// ==== LIVE WIRING (fastt relay) ====
// This page ships TWO real rankings over one period control:
//   • Creators  — GET /admin/stats/per-model  with NO account_id (an account_id
//     collapses the payload to a single row). Ranks the whole roster by
//     total_revenue_cents, with messages_sent / ppv_conversions / active subs
//     on the sub-line. arpu_cents and ltv_cents are NULL for creators with no
//     denominator in the window and render as "—", never 0.
//   • Employees — GET /admin/stats/per-employee (org-wide, no account param).
// The Infloww lock/CTA block above is a static mock end to end — including its
// "Last updated" line, which is deliberately NOT re-stamped with the clock:
// a fresh timestamp would make the mock read as freshly-computed truth.
Fastt.ready(async () => {
  const $ = Fastt.$, $$ = Fastt.$$, esc = Fastt.esc;
  const acct = String(Fastt.account() || '');

  // ── honest chrome ─────────────────────────────────────────────
  const tz = $('#tz-top');
  if (tz) tz.textContent = 'UTC+00:00';   // every relay stamp is UTC

  // ── topbar health pill (ported from dashboard.html — a hardcoded
  //    green "Operational" light on every screen is a lie) ────────
  async function loadOps() {
    const dot = $('#ops-dot'), txt = $('#ops-status');
    if (!dot || !txt) return;
    const fail = (m) => { dot.style.background = '#ff5f57'; txt.textContent = m; };
    let rev;
    try { rev = await Fastt.get('/admin/rev/live'); }
    catch (e) { fail('Relay unreachable'); throw e; }
    if (rev && rev.error) { fail('Relay error'); return; }
    let health;
    try { health = await Fastt.get('/admin/ingest/transactions/health'); }
    catch (e) { fail('Health check failed'); throw e; }
    const row = (health.accounts || []).find((a) => String(a.account_id) === acct);
    if (!row) { txt.textContent = 'No ingest data'; dot.style.background = '#febc2e'; return; }
    if (row.tier === 'green') { txt.textContent = 'Operational'; dot.style.background = '#3ec46a'; }
    else if (row.tier === 'yellow') { txt.textContent = 'Ingest lagging'; dot.style.background = '#febc2e'; }
    else { txt.textContent = 'Ingest stale'; dot.style.background = '#ff5f57'; }
  }
  Fastt.liveBadge($('#ops-pill'));
  try { await loadOps(); } catch (e) { Fastt.oops(e); }


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
  // These two badges belong ONLY to the Infloww lock/CTA promo mock at the top
  // (the "not on the leaderboard yet" hero + its frozen timestamp). Labelled
  // "INFLOWW SAMPLE" — not the generic "STATIC DEMO" — so they read as scoped to
  // that promo block and can never be mistaken for a verdict on the LIVE agency
  // ranking table below, which carries its own LIVE badge on #lb-h1.
  Fastt.staticBadge($('.lb-title'), 'INFLOWW SAMPLE');
  Fastt.staticBadge($('.lb-upd'), 'INFLOWW SAMPLE');
  Fastt.liveBadge($('#lb-h1'));

  // ── period control (same windows as the dashboard segment, plus the
  //    30-day window this page used to hardcode) ──────────────────
  function segWindow() {
    const on = $('#lb-seg button.on');
    const lbl = on ? on.textContent.trim() : 'Last 30 days';
    const DAY = 86400000;
    const now = new Date();
    const today = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate()));
    const iso = (d) => d.toISOString().slice(0, 10);
    if (lbl === 'Yesterday') { const y = new Date(today.getTime() - DAY); return { from: iso(y), to: iso(y), label: lbl }; }
    if (lbl === 'Today') return { from: iso(today), to: iso(today), label: lbl };
    if (lbl === 'This month') {
      const first = new Date(Date.UTC(today.getUTCFullYear(), today.getUTCMonth(), 1));
      return { from: iso(first), to: iso(today), label: lbl };
    }
    if (lbl === 'This week') {
      const off = (today.getUTCDay() + 6) % 7;   // Monday-start ISO week
      return { from: iso(new Date(today.getTime() - off * DAY)), to: iso(today), label: lbl };
    }
    return { from: iso(new Date(today.getTime() - 29 * DAY)), to: iso(today), label: 'Last 30 days' };
  }

  const AV = ['#e0679b', '#5b8def', '#67d1ae', '#a78bfa', '#e5a35b', '#e07a5f'];
  let TAB = 'creators';
  let METRIC = 'revenue';
  const expanded = new Set();          // rowkeys currently open
  let CUR = { scoring: [], max: 0, metric: 'revenue', empty: '' };

  // The rankable metrics — the real PerEmployeeTable is sortable by
  // Messages / PPVs / Revenue; here that becomes a friendlier "Rank by".
  const METRICS = {
    revenue: { key: 'cents', label: 'Revenue',  stacked: true,  unit: '' },
    ppv:     { key: 'ppv',   label: 'PPVs sold', stacked: false, unit: 'PPVs' },
    msgs:    { key: 'msgs',  label: 'Messages',  stacked: false, unit: 'msgs' },
  };
  const mfmt = (m, v) => METRICS[m].stacked ? Fastt.fmtCents(v)
    : Fastt.fmtInt(v) + '<span class="lbunit"> ' + METRICS[m].unit + '</span>';

  function barHtml(r, m, max) {
    const M = METRICS[m], val = r[M.key] || 0;
    const pct = max > 0 ? Math.max(0.5, (val / max) * 100) : 0;
    if (!M.stacked || val <= 0) {
      return '<span class="lbtrack"><span class="lbfill solid" style="width:' + pct.toFixed(1) + '%"></span></span>';
    }
    // stacked: PPV / Tip / other, proportional inside the filled portion
    const ppv = Math.max(0, r.kppv || 0), tip = Math.max(0, r.ktip || 0),
          oth = Math.max(0, val - ppv - tip);
    const seg = (c, cls) => c > 0 ? '<span class="lbseg ' + cls + '" style="flex:' + c + '"></span>' : '';
    return '<span class="lbtrack"><span class="lbfill" style="width:' + pct.toFixed(1) + '%">'
      + seg(ppv, 's-ppv') + seg(tip, 's-tip') + seg(oth, 's-oth') + '</span></span>';
  }

  function rowHtml(r, i, max, m) {
    const M = METRICS[m], val = r[M.key] || 0;
    const medal = i === 0 ? ' g1' : (i === 1 ? ' g2' : (i === 2 ? ' g3' : ''));
    const canExp = !!r.detail;
    const open = expanded.has(r.rowkey);
    const chev = canExp
      ? '<span class="lbchev' + (open ? ' o' : '') + '">›</span>'
      : '<span class="lbchev ghost"></span>';
    // when NOT ranking by revenue, keep the money visible on the sub-line
    const subLead = m !== 'revenue'
      ? '<span class="lbsubrev">' + Fastt.fmtCents(r.cents || 0) + '</span> · ' : '';
    const tags = (r.unattributed ? '<span class="lbtag">unattributed</span>' : '')
      + (r.me ? '<span class="lbtag you">you</span>' : '');
    const row = '<div class="lbrow' + (r.me ? ' me' : '') + (canExp ? ' exp' : '') + (open ? ' open' : '')
        + '" data-key="' + esc(r.rowkey) + '">'
      + '<span class="lbrank' + medal + '">' + (i + 1) + '</span>'
      + '<span class="lbav" style="background:' + AV[i % AV.length] + '">'
        + esc(String(r.name || '?').charAt(0).toUpperCase()) + '</span>'
      + '<span class="lbmain"><span class="lbnm">' + esc(r.name) + tags + '</span>'
        + '<span class="lbsub">' + subLead + r.sub + '</span></span>'
      + barHtml(r, m, max)
      + '<span class="lbval">' + mfmt(m, val) + '</span>'
      + chev
      + '</div>';
    return row + (open && canExp ? r.detail : '');
  }

  async function loadCreators(w) {
    // NO account_id: with one, per-model returns a single row.
    const out = await Fastt.get('/admin/stats/per-model',
      { from: w.from, to: w.to }, { noAccount: true });
    const unavailable = out.active_subs_source === 'unavailable';
    const KINDS = [['ppv','PPV'],['tip','Tips'],['post','Posts'],
                   ['subscription','Subs'],['rebill','Rebills'],['custom','Custom']];
    const rows = (out.per_model || []).map((r) => {
      const rbk = r.revenue_by_kind || {};
      const cents = r.total_revenue_cents || 0;
      const dash = '<span style="color:#5f5f5f">—</span>';
      const fans = unavailable
        ? '<span style="color:#f0c98a">subs n/a</span>'
        : Fastt.fmtInt(r.active_subs_count) + ' fans';
      const arpu = r.arpu_cents == null ? dash : Fastt.fmtCents(r.arpu_cents);
      const chips = KINDS.filter(([k]) => (rbk[k] || 0) > 0)
        .map(([k, l]) => '<span class="kchip"><i class="kdot ' + k + '"></i>' + l + ' '
          + Fastt.fmtCents(rbk[k]) + '</span>').join('');
      const pend = r.pending_revenue_cents || 0;
      const pendLine = pend > 0
        ? '<div class="kpend">Pending ' + Fastt.fmtCents(pend)
          + (r.pending_clears_by ? ' · clears by ' + esc(r.pending_clears_by) : '') + '</div>' : '';
      const detail = '<div class="lbdetail"><div class="kchips">'
        + (chips || '<span class="kmuted">No cleared revenue in this window</span>')
        + '</div>' + pendLine + '</div>';
      return {
        rowkey: 'c:' + r.account_id,
        name: r.display_name || ('Account ' + r.account_id),
        cents, ppv: r.ppv_conversions || 0, msgs: r.messages_sent || 0,
        kppv: rbk.ppv || 0, ktip: rbk.tip || 0, pending: pend,
        me: String(r.account_id) === acct, unattributed: false,
        sub: fans + ' · ' + Fastt.fmtInt(r.new_subs_count) + ' new · '
           + Fastt.fmtInt(r.messages_sent) + ' msgs · ' + Fastt.fmtInt(r.ppv_conversions)
           + ' PPVs · ARPU ' + arpu,
        detail,
      };
    });
    return {
      rows, tab: 'creators',
      empty: 'No creator revenue in ' + w.label.toLowerCase() + ' (' + w.from + ' → ' + w.to + ')',
      note: 'GET <code>/admin/stats/per-model</code> (no <code>account_id</code>) — the whole roster, '
        + 'gross ledger cents, UTC days. Active fans come from <code>'
        + esc(out.active_subs_source || 'unknown') + '</code>'
        + (out.active_subs_source === 'transactions_31d'
            ? ' — derived from paid transactions in the last 31 days, so free pages correctly read 0 fans.' : '.')
        + ' Tap a creator to see the revenue split by kind and pending (uncleared) cents.',
    };
  }

  async function loadEmployees(w) {
    // by_account=true expands each chatter into per-account subtotals.
    const out = await Fastt.get('/admin/stats/per-employee',
      { from: w.from, to: w.to, by_account: true });
    const rows = (out.employees || []).map((e) => {
      const rbk = e.revenue_by_kind || {};
      const kppv = rbk.ppv || 0, ktip = rbk.tip || 0;
      const unatt = e.employee_id == null;
      const pa = (e.per_account || []).slice()
        .sort((a, b) => (b.revenue_cents || 0) - (a.revenue_cents || 0));
      const detail = pa.length
        ? '<div class="lbdetail">' + pa.map((s) => {
            const srk = s.revenue_by_kind || {};
            return '<div class="lbsubrow">'
              + '<span class="lbsubnm">' + esc(s.account_nickname || ('#' + s.account_id)) + '</span>'
              + '<span class="lbsubmeta">' + Fastt.fmtInt(s.messages_sent) + ' msgs · '
                + Fastt.fmtInt(s.ppv_conversions) + ' PPVs · PPV ' + Fastt.fmtCents(srk.ppv || 0)
                + ((srk.tip || 0) > 0 ? ' · Tip ' + Fastt.fmtCents(srk.tip) : '') + '</span>'
              + '<span class="lbsubval">' + Fastt.fmtCents(s.revenue_cents || 0) + '</span>'
              + '</div>';
          }).join('') + '</div>'
        : '';
      return {
        rowkey: 'e:' + (unatt ? 'unatt' : e.employee_id),
        name: e.display_name || (unatt ? 'Unattributed' : 'Employee #' + e.employee_id),
        cents: e.revenue_cents || 0, ppv: e.ppv_conversions || 0, msgs: e.messages_sent || 0,
        kppv, ktip, pending: 0, me: false, unattributed: unatt,
        sub: Fastt.fmtInt(e.messages_sent) + ' msgs · ' + Fastt.fmtInt(e.ppv_conversions)
           + ' PPVs · PPV ' + Fastt.fmtCents(kppv)
           + (ktip > 0 ? ' · Tip ' + Fastt.fmtCents(ktip) : '')
           + (unatt ? ' · automation / direct OF' : ''),
        detail,
      };
    });
    return {
      rows, tab: 'employees',
      empty: 'No employee-attributed sales in ' + w.label.toLowerCase() + ' (' + w.from + ' → ' + w.to + ')',
      note: 'GET <code>/admin/stats/per-employee</code> (<code>by_account=true</code>) — org-wide '
        + '(no creator scope), gross ledger cents, UTC days. Tap a chatter to see their per-account split. '
        + 'The <b>Unattributed</b> row is real: revenue the linker could not tie to a chatter (automation '
        + 'sends and sales made outside the app).',
    };
  }

  // Tiles that lead the panel: total money, the leader, and a tab-specific stat.
  function tile(lbl, val, sub) {
    return '<div class="lbtile"><div class="ltlbl">' + esc(lbl) + '</div>'
      + '<div class="ltval">' + val + '</div>'
      + (sub ? '<div class="ltsub">' + sub + '</div>' : '') + '</div>';
  }
  function summaryHtml(sc, m, w) {
    if (!sc.length) return '';
    const totalC = sc.reduce((s, r) => s + (r.cents || 0), 0);
    const leader = sc[0];
    let t3;
    if (TAB === 'employees') {
      const ppvC = sc.reduce((s, r) => s + (r.kppv || 0), 0);
      const tipC = sc.reduce((s, r) => s + (r.ktip || 0), 0);
      t3 = tile('PPV vs Tip revenue', Fastt.fmtCents(ppvC) + ' PPV', Fastt.fmtCents(tipC) + ' in tips');
    } else {
      const pend = sc.reduce((s, r) => s + (r.pending || 0), 0);
      t3 = pend > 0
        ? tile('Pending (uncleared)', Fastt.fmtCents(pend), 'across ' + sc.length + ' creators')
        : tile('Creators ranked', String(sc.length), 'earning in window');
    }
    const leadVal = METRICS[m].stacked ? Fastt.fmtCents(leader[METRICS[m].key])
      : Fastt.fmtInt(leader[METRICS[m].key]) + ' ' + METRICS[m].unit;
    return tile(esc(w.label) + ' revenue', Fastt.fmtCents(totalC), sc.length + ' ranked')
      + tile('Leader · ' + METRICS[m].label, esc(leader.name), leadVal)
      + t3;
  }
  function footHtml(sc) {
    if (!sc.length) return '';
    const c = sc.reduce((s, r) => s + (r.cents || 0), 0);
    const msgs = sc.reduce((s, r) => s + (r.msgs || 0), 0);
    const ppv = sc.reduce((s, r) => s + (r.ppv || 0), 0);
    return '<div class="lbfootrow"><span>Total (' + sc.length + ')</span>'
      + '<span>' + Fastt.fmtInt(msgs) + ' msgs</span>'
      + '<span>' + Fastt.fmtInt(ppv) + ' PPVs</span>'
      + '<span class="lbfootc">' + Fastt.fmtCents(c) + '</span></div>';
  }

  // Repaint ONLY the list from cached data (used on expand/collapse — no refetch).
  function paintList() {
    const list = $('#lb-list');
    if (!list) return;
    if (!CUR.scoring.length) {
      list.innerHTML = '<div class="lb-empty">' + esc(CUR.empty) + '</div>';
      return;
    }
    list.innerHTML = CUR.scoring.map((r, i) => rowHtml(r, i, CUR.max, CUR.metric)).join('');
  }

  // A fast tab/period/metric click must not let a slow earlier response repaint
  // the panel after a newer one already landed.
  let seq = 0;
  async function render() {
    const mine = ++seq;
    const w = segWindow(), m = METRIC;
    const list = $('#lb-list'), note = $('#lb-note'),
          sum = $('#lb-summary'), foot = $('#lb-foot');
    if (!list) return;
    expanded.clear();
    list.innerHTML = '<div class="lb-empty">Loading ' + esc(w.label.toLowerCase()) + '…</div>';
    if (sum) sum.innerHTML = '';
    if (foot) foot.innerHTML = '';
    let res;
    try {
      res = TAB === 'creators' ? await loadCreators(w) : await loadEmployees(w);
    } catch (e) {
      if (mine !== seq) return;
      list.innerHTML = '<div class="lb-empty">Ranking unavailable — relay '
        + esc(String((e && e.status) || 'error')) + '</div>';
      if (note) note.textContent = '';
      Fastt.oops(e);
      return;
    }
    if (mine !== seq) return;
    const key = METRICS[m].key;
    const scoring = res.rows.filter((r) => (r[key] || 0) > 0)
      .sort((a, b) => (b[key] || 0) - (a[key] || 0));
    const idle = res.rows.length - scoring.length;
    CUR = { scoring, max: scoring.length ? scoring[0][key] : 0, metric: m, empty: res.empty };
    paintList();
    if (sum) sum.innerHTML = summaryHtml(scoring, m, w);
    if (foot) foot.innerHTML = footHtml(scoring);
    if (note) {
      note.innerHTML = '<b>' + esc(w.label) + '</b> · ' + esc(w.from) + ' → ' + esc(w.to) + ' (UTC) · '
        + 'ranked by ' + esc(METRICS[m].label) + ' · ' + scoring.length + ' shown'
        + (idle > 0 ? ', ' + idle + ' with no ' + esc(METRICS[m].label.toLowerCase())
            + ' in this window (hidden)' : '')
        + '. ' + res.note;
    }
  }

  // Expand / collapse a row without refetching.
  $('#lb-list').addEventListener('click', (ev) => {
    const row = ev.target.closest('.lbrow.exp');
    if (!row) return;
    const k = row.getAttribute('data-key');
    if (!k) return;
    if (expanded.has(k)) expanded.delete(k); else expanded.add(k);
    paintList();
  });

  $$('#lb-tabs .lbtab').forEach((b) => b.addEventListener('click', () => {
    if (b.classList.contains('on')) return;
    $$('#lb-tabs .lbtab').forEach((x) => x.classList.remove('on'));
    b.classList.add('on');
    TAB = b.dataset.tab;
    $('#lb-h1').firstChild.textContent = TAB === 'creators' ? 'Your agency ranking' : 'Your chatters ranking';
    render();
  }));
  $$('#lb-seg button').forEach((b) => b.addEventListener('click', () => {
    if (b.classList.contains('on')) return;
    $$('#lb-seg button').forEach((x) => x.classList.remove('on'));
    b.classList.add('on');
    render();
  }));
  $$('#lb-metric button').forEach((b) => b.addEventListener('click', () => {
    if (b.classList.contains('on')) return;
    $$('#lb-metric button').forEach((x) => x.classList.remove('on'));
    b.classList.add('on');
    METRIC = b.dataset.metric;
    render();
  }));

  await render();
});
