// ==== LIVE WIRING (fastt relay) ====
// Reads
//   /admin/stats/per-model (no acct)  → all-models earnings summary, channel shares, KPI cards
//   /admin/stats/revenue?group_by=day → earnings trends
//   /api/of/v2/payouts/transactions   → Earnings breakdown (gross/fee/VAT/net) + the net-earnings mode
//   /api/of/v2/payouts/chargebacks/ratio → chargeback ratio
//   /api/of/v2/payments/referrals/balance → Referrals tile
//   /api/of/v2/streams/feed           → Streams tile (count only; OF exposes no stream $)
// The date field owns from/to for EVERY read on the page.
// Honest empty states: Refunds (OF has no refund line item), Creator tag (no endpoint),
//   Hour granularity (/admin/stats/revenue only groups by day|kind), Streams earnings.
Fastt.ready(async () => {
  const DAY = 86400000;
  const isoDay = (d) => d.toISOString().slice(0, 10);
  const dayStart = (s) => new Date(s + 'T00:00:00Z');
  const dayEnd = (s) => new Date(s + 'T23:59:59.999Z');
  const shortLbl = (s) => new Date(s.length <= 10 ? s + 'T00:00:00Z' : s)
    .toLocaleDateString('en-US', { month: 'short', day: 'numeric', timeZone: 'UTC' });
  const longLbl = (s) => dayStart(s)
    .toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric', timeZone: 'UTC' });

  // ── the reporting window (owned here, threaded into every read) ──
  const now = new Date();
  const range = { from: isoDay(new Date(now.getTime() - 6 * DAY)), to: isoDay(now) };
  const spanDays = () => Math.max(1,
    Math.round((dayStart(range.to) - dayStart(range.from)) / DAY) + 1);

  const dateField = document.querySelector('.fbar .field');
  function paintRange() {
    if (!dateField) return;
    const tn = Array.prototype.filter.call(dateField.childNodes,
      (n) => n.nodeType === 3 && n.textContent.trim());
    if (tn.length >= 2) { tn[0].textContent = ' ' + longLbl(range.from) + ' '; tn[1].textContent = ' ' + longLbl(range.to) + ' '; }
  }
  paintRange();

  /** Range popover: presets + two real date inputs. Applies to the whole page. */
  function mountRangePicker(field, onApply) {
    if (!field) return;
    field.title = 'Change the reporting window — every panel on this page re-queries';
    field.addEventListener('click', (ev) => {
      ev.stopPropagation();
      document.querySelectorAll('.ft-pop').forEach((n) => n.remove());
      const pop = document.createElement('div');
      pop.className = 'ft-pop';
      const r = field.getBoundingClientRect();
      pop.style.left = Math.max(8, Math.min(r.left, window.innerWidth - 290)) + 'px';
      pop.style.top = (r.bottom + 6) + 'px';
      pop.innerHTML =
        '<div class="pr">' +
        '<button data-d="7">Last 7 days</button><button data-d="14">Last 14 days</button>' +
        '<button data-d="30">Last 30 days</button><button data-d="90">Last 90 days</button></div>' +
        '<div class="rw"><span>From</span><input type="date" data-k="f" value="' + range.from + '"></div>' +
        '<div class="rw"><span>To</span><input type="date" data-k="t" value="' + range.to + '"></div>' +
        '<button class="go" type="button">Apply</button>';
      document.body.appendChild(pop);
      const ph = pop.getBoundingClientRect().height;
      if (r.bottom + 6 + ph > window.innerHeight - 8) pop.style.top = Math.max(8, r.top - ph - 6) + 'px';
      const close = () => { pop.remove(); document.removeEventListener('click', close); };
      setTimeout(() => document.addEventListener('click', close), 0);
      pop.addEventListener('click', (e2) => e2.stopPropagation());
      pop.querySelectorAll('.pr button').forEach((b) => b.addEventListener('click', () => {
        const n = Number(b.dataset.d);
        const to = new Date();
        close();
        onApply(isoDay(new Date(to.getTime() - (n - 1) * DAY)), isoDay(to));
      }));
      pop.querySelector('.go').addEventListener('click', () => {
        const f = pop.querySelector('input[data-k="f"]').value;
        const t = pop.querySelector('input[data-k="t"]').value;
        if (!f || !t) { Fastt.toast('Pick both dates'); return; }
        if (f > t) { Fastt.toast('“From” must be on or before “To”'); return; }
        close(); onApply(f, t);
      });
    });
  }

  // ═══════════════ all-models selection layer (default = every model, combined) ═══════════════
  // Declared BEFORE the filter bar because the Models picker paints its label
  // immediately (needs selectedIds / allModels in scope).
  const accById = {};
  (Fastt.accounts() || []).forEach((a) => { accById[String(a.id)] = a; });
  let allModels = [], allModelsPrev = [], activeSubsSrc = 'n/a';
  let selectedIds = null;                       // null = all models
  const isAllSel = () => selectedIds === null;
  const selModels = () => isAllSel() ? allModels : allModels.filter((m) => selectedIds.has(String(m.account_id)));
  const decorate = (r) => { const by = r.revenue_by_kind || {}; r._ppv = by.ppv || 0; r._post = by.post || 0; r._tip = by.tip || 0; r._sub = (by.subscription || 0) + (by.rebill || 0); return r; };
  function combineRows(rows) {
    const zk = () => ({ ppv: 0, post: 0, tip: 0, subscription: 0, rebill: 0, custom: 0 });
    const by = zk(), pby = zk();
    let total = 0, pend = 0, msgs = 0, ppvc = 0, newsubs = 0, active = 0, activeSeen = false, clears = null;
    rows.forEach((r) => {
      const k = r.revenue_by_kind || {}, p = r.pending_by_kind || {};
      Object.keys(by).forEach((kk) => { by[kk] += (k[kk] || 0); pby[kk] += (p[kk] || 0); });
      total += r.total_revenue_cents || 0; pend += r.pending_revenue_cents || 0;
      msgs += r.messages_sent || 0; ppvc += r.ppv_conversions || 0; newsubs += r.new_subs_count || 0;
      if (typeof r.active_subs_count === 'number') { active += r.active_subs_count; activeSeen = true; }
      if (r.pending_clears_by && (!clears || r.pending_clears_by < clears)) clears = r.pending_clears_by;
    });
    return { revenue_by_kind: by, pending_by_kind: pby, total_revenue_cents: total, pending_revenue_cents: pend,
      messages_sent: msgs, ppv_conversions: ppvc, new_subs_count: newsubs,
      active_subs_count: activeSeen ? active : null, pending_clears_by: clears };
  }
  const selPrevTotal = () => {
    const set = isAllSel() ? null : selectedIds;
    return allModelsPrev.reduce((a, r) => (set && !set.has(String(r.account_id))) ? a : a + (r.total_revenue_cents || 0), 0);
  };
  async function loadModels() {
    const span = spanDays();
    const prevTo = isoDay(new Date(dayStart(range.from).getTime() - DAY));
    const prevFrom = isoDay(new Date(dayStart(range.from).getTime() - span * DAY));
    const pm = await Fastt.get('/admin/stats/per-model', { from: range.from, to: range.to }, { noAccount: true });
    allModels = (pm.per_model || []).map(decorate);
    activeSubsSrc = pm.active_subs_source || 'n/a';
    const pmPrev = await Fastt.get('/admin/stats/per-model', { from: prevFrom, to: prevTo }, { noAccount: true }).catch(() => null);
    allModelsPrev = pmPrev ? (pmPrev.per_model || []) : [];
    if (selectedIds) {   // drop a stale selection down to ids that still exist in this window
      const live = new Set(allModels.map((m) => String(m.account_id)));
      selectedIds = new Set(Array.from(selectedIds).filter((id) => live.has(id)));
      if (!selectedIds.size) selectedIds = null;
    }
  }

  // ── filter bar: what is real, what is not ──
  const fbarFields = Array.prototype.slice.call(document.querySelectorAll('.fbar .field'));
  const tagField = fbarFields[1], creatorField = fbarFields[2];
  if (tagField) {
    Fastt.staticBadge(tagField, 'NO BACKEND');
    tagField.title = 'Creator tags have no relay endpoint — nothing to filter on';
  }
  if (creatorField) {
    creatorField.title = 'Pick which models the report combines — all selected by default';
    const lbl = creatorField.querySelector('.mut');
    const paintFilterLbl = () => {
      if (!lbl) return;
      if (isAllSel()) { lbl.textContent = 'All models'; lbl.style.color = '#dcdcdc'; }
      else { lbl.textContent = selectedIds.size + ' of ' + (allModels.length || selectedIds.size) + ' models'; lbl.style.color = '#fff'; }
    };
    window.__paintFilterLbl = paintFilterLbl;
    paintFilterLbl();
    creatorField.addEventListener('click', (ev) => {
      ev.stopPropagation();
      document.querySelectorAll('.ft-pop').forEach((n) => n.remove());
      const rowsM = allModels.slice().sort((a, b) => (b.total_revenue_cents || 0) - (a.total_revenue_cents || 0));
      const pop = document.createElement('div');
      pop.className = 'ft-pop';
      const r = creatorField.getBoundingClientRect();
      pop.style.left = Math.max(8, Math.min(r.left, window.innerWidth - 280)) + 'px';
      pop.style.top = (r.bottom + 6) + 'px';
      pop.style.maxHeight = '380px'; pop.style.overflowY = 'auto'; pop.style.minWidth = '260px';
      let ph = '<div class="cphead" style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px">' +
        '<span style="color:#8a8a8a;font-size:12px">Models · ' + (isAllSel() ? 'all' : selectedIds.size) + '/' + rowsM.length + '</span>' +
        '<span><span class="qa" data-a="all" style="color:#4166f6;cursor:pointer">All</span> · <span class="qa" data-a="none" style="color:#8a8a8a;cursor:pointer">None</span></span></div><div class="msdiv"></div>';
      ph += rowsM.length ? rowsM.map((m) => {
        const id = String(m.account_id), acc = accById[id] || {};
        const on = isAllSel() || selectedIds.has(id);
        return '<label class="msrow"><input type="checkbox" data-id="' + Fastt.esc(id) + '"' + (on ? ' checked' : '') + '>' +
          '<span class="dot" style="background:' + Fastt.esc(acc.color || '#666') + '"></span>' +
          '<span class="nm">' + Fastt.esc(acc.nickname || m.display_name || id) + '</span>' +
          '<span class="am">' + moneyLbl(m.total_revenue_cents || 0) + '</span></label>';
      }).join('') : '<div class="rw">No models in this window yet.</div>';
      pop.innerHTML = ph;
      document.body.appendChild(pop);
      const hh = pop.getBoundingClientRect().height;
      if (r.bottom + 6 + hh > window.innerHeight - 8) pop.style.top = Math.max(8, r.top - hh - 6) + 'px';
      const close = () => { pop.remove(); document.removeEventListener('click', close); };
      setTimeout(() => document.addEventListener('click', close), 0);
      pop.addEventListener('click', (e2) => {
        e2.stopPropagation();
        const qa = e2.target.closest('.qa');
        if (qa) { selectedIds = qa.dataset.a === 'all' ? null : new Set(); close(); applySelection(); return; }
        const cb = e2.target.closest('input[data-id]');
        if (cb) {
          const id = cb.dataset.id;
          const set = isAllSel() ? new Set(allModels.map((m) => String(m.account_id))) : new Set(selectedIds);
          if (cb.checked) set.add(id); else set.delete(id);
          selectedIds = (set.size === allModels.length) ? null : set;   // all ticked ⇒ collapse to the "all" sentinel
          applySelection();
        }
      });
    });
    Fastt.liveBadge(creatorField);
  }

  // section headers
  const sects = Array.prototype.slice.call(document.querySelectorAll('.sect'));
  const sect = (t) => sects.find((s) => s.textContent.trim().indexOf(t) === 0) || null;
  const summarySect = sect('Earnings summary');
  const trendSect = sect('Earnings trends');
  const chanSect = sect('Earnings by channel');
  const bdSect = sect('Earnings breakdown');
  const perfSect = sect('Creator performance');
  const setBadge = (el, live, txt) => {
    if (!el) return;
    el.querySelectorAll(':scope > .ft-live, :scope > .ft-static').forEach((n) => n.remove());
    if (live) Fastt.liveBadge(el); else Fastt.staticBadge(el, txt);
  };

  // tiles by label text
  const tileMap = { 'Total earnings': 'total', 'Subscriptions': 'subscription', 'Posts': 'post', 'Messages': 'ppv', 'Tips': 'tip' };
  const tiles = {};
  let refTile = null, streamTile = null;
  document.querySelectorAll('.esum .emx').forEach((t) => {
    const lab = t.querySelector('.lab');
    if (!lab) return;
    const name = lab.textContent.trim();
    if (tileMap[name]) tiles[tileMap[name]] = t;
    if (name === 'Referrals') refTile = t;
    if (name === 'Streams') streamTile = t;
  });
  // pending line under the headline number (real app: PerModelKpiGrid)
  const pendLine = document.createElement('div');
  pendLine.className = 'pend';
  pendLine.style.cssText = 'font-size:12.5px;margin:-2px 0 6px';
  if (tiles.total) tiles.total.insertBefore(pendLine, tiles.total.querySelector('.pct'));

  const sumSrc = document.getElementById('sumSrc');
  const chanChart = document.querySelector('.chan-chart');
  const chanLegend = document.getElementById('chanLegend');
  const chanNote = document.getElementById('chanNote');

  function zeroTiles(msg) {
    document.querySelectorAll('.esum .emx').forEach((t) => {
      if (t === refTile || t === streamTile) return;
      const v = t.querySelector('.val'), p = t.querySelector('.pct');
      if (v) v.textContent = '—';
      if (p) p.textContent = '';
    });
    pendLine.textContent = '';
    if (sumSrc) sumSrc.textContent = msg || '';
    blankChannel(msg || '');
  }

  // ═══════════════ earnings by channel — multi-line (period total × kind share) ═══════════════
  const CHAN = [
    { key: 'subscriptions', label: 'Subscriptions', color: '#3b82f6' },
    { key: 'tips',          label: 'Tips',          color: '#10b981' },
    { key: 'posts',         label: 'Posts',         color: '#ef4444' },
    { key: 'messages',      label: 'Messages',      color: '#f59e0b' },
    { key: 'referrals',     label: 'Referrals',     color: '#6366f1' },
    { key: 'streams',       label: 'Streams',       color: '#a855f7' },
  ];
  function channelShares() {
    const by = (pmRow && pmRow.revenue_by_kind) || {};
    // per-model ingest carries neither referrals nor stream $, so those legend
    // rows read $0 — same as the reference dashboard.
    return { subscriptions: (by.subscription || 0) + (by.rebill || 0), tips: by.tip || 0,
      posts: by.post || 0, messages: by.ppv || 0, referrals: 0, streams: 0 };
  }
  function blankChannel(msg) {
    if (chanChart) chanChart.innerHTML =
      '<g stroke="#2c2c2c" stroke-dasharray="3 5">' +
      [20, 78, 137, 195].map((y) => '<line x1="46" y1="' + y + '" x2="996" y2="' + y + '"/>').join('') +
      '</g><line x1="46" y1="254" x2="996" y2="254" stroke="#3a3a3a"/>' +
      (msg ? '<text x="520" y="140" text-anchor="middle" fill="#8a8a8a" font-family="Inter, sans-serif" font-size="14">' + Fastt.esc(msg) + '</text>' : '');
    if (chanLegend) chanLegend.innerHTML = '';
    if (chanNote) chanNote.textContent = '';
  }
  function drawChannel(pts) {
    if (!chanChart) return;
    const shares = channelShares();
    const grand = Object.keys(shares).reduce((a, k) => a + shares[k], 0) || 0;
    if (chanLegend) chanLegend.innerHTML = CHAN.map((c) => {
      const v = shares[c.key] || 0, pc = grand > 0 ? (100 * v / grand).toFixed(2) : '0.00';
      return '<div class="lg"><span class="dot" style="background:' + c.color + '"></span>' +
        '<span class="nm">' + c.label + '</span><span class="pc">' + pc + '%</span>' +
        '<span class="am">' + Fastt.fmtCents(v) + '</span></div>';
    }).join('');
    const L = 46, R = 996, T = 20, B = 254;
    const series = CHAN.map((c) => ({ c, share: grand > 0 ? shares[c.key] / grand : 0 })).filter((s) => s.share > 0);
    const vals = pts.map((p) => p.v);
    const maxLine = series.length
      ? Math.max.apply(null, series.map((s) => Math.max.apply(null, vals.map((v) => v * s.share))).concat([0])) : 0;
    const maxV = niceMax(maxLine);
    const X = (i) => pts.length === 1 ? (L + R) / 2 : L + i * (R - L) / (pts.length - 1);
    const Y = (v) => B - (v / maxV) * (B - T);
    const fr = [1, .75, .5, .25, 0], labY = [25, 83, 141, 199, 258];
    let h = '<g font-family="Inter, sans-serif" font-size="14" fill="#6a6a6a">';
    fr.forEach((f, i) => { h += '<text x="8" y="' + labY[i] + '">' + moneyLbl(maxV * f) + '</text>'; });
    h += '</g><g stroke="#2c2c2c" stroke-dasharray="3 5">';
    [20, 78, 137, 195].forEach((y) => { h += '<line x1="46" y1="' + y + '" x2="996" y2="' + y + '"/>'; });
    h += '</g><line x1="46" y1="254" x2="996" y2="254" stroke="#3a3a3a"/>';
    const step = Math.max(1, Math.ceil(pts.length / 8));
    h += '<g font-family="Inter, sans-serif" font-size="14" fill="#8a8a8a">';
    pts.forEach((p, i) => {
      if (i % step && i !== pts.length - 1) return;
      const anchor = i === 0 ? 'start' : (i === pts.length - 1 ? 'end' : 'middle');
      h += '<text x="' + X(i).toFixed(1) + '" y="284" text-anchor="' + anchor + '">' + Fastt.esc(p.label) + '</text>';
    });
    h += '</g>';
    series.forEach((s) => {
      h += '<polyline points="' + pts.map((p, i) => X(i).toFixed(1) + ',' + Y(p.v * s.share).toFixed(1)).join(' ') +
        '" fill="none" stroke="' + s.c.color + '" stroke-width="2.5" stroke-linejoin="round"/>';
    });
    if (series.length) series.forEach((s) => {
      pts.forEach((p, i) => { h += '<circle cx="' + X(i).toFixed(1) + '" cy="' + Y(p.v * s.share).toFixed(1) + '" r="2.6" fill="' + s.c.color + '"/>'; });
    });
    chanChart.innerHTML = h;
    if (chanNote) chanNote.textContent =
      'Each channel line = the period total split by that channel’s share of the window ' +
      '(fastt ingest groups by day OR kind, not both, so exact per-day-per-kind isn’t available). Gross cents.';
  }

  // ── chart helpers (earnings trends) ──
  const trendSvg = document.querySelector('.chartcard svg.chart');
  const niceMax = (v) => {
    if (v <= 0) return 400;
    const p = Math.pow(10, Math.floor(Math.log10(v)));
    const n = v / p;
    return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10) * p;
  };
  const moneyLbl = (c) => { const d = c / 100; return d >= 1000 ? '$' + (d / 1000).toFixed(1) + 'k' : '$' + Math.round(d); };
  function blankTrend(msg) {
    if (!trendSvg) return;
    trendSvg.innerHTML =
      '<g stroke="#2c2c2c" stroke-dasharray="3 5">' +
      [20, 78, 137, 195].map((y) => '<line x1="46" y1="' + y + '" x2="996" y2="' + y + '"/>').join('') +
      '</g><line x1="46" y1="254" x2="996" y2="254" stroke="#3a3a3a"/>' +
      '<text x="520" y="140" text-anchor="middle" fill="#8a8a8a" font-family="Inter, sans-serif" font-size="14">' +
      Fastt.esc(msg) + '</text>';
  }
  function drawTrend(pts) {
    const L = 46, R = 996, T = 20, B = 254;
    const maxV = niceMax(Math.max.apply(null, pts.map((p) => p.v).concat([0])));
    const X = (i) => pts.length === 1 ? (L + R) / 2 : L + i * (R - L) / (pts.length - 1);
    const Y = (v) => B - (v / maxV) * (B - T);
    const fr = [1, 0.75, 0.5, 0.25, 0];
    const labY = [25, 83, 141, 199, 258];
    let h = '<g font-family="Inter, sans-serif" font-size="14" fill="#6a6a6a">';
    fr.forEach((f, i) => { h += '<text x="8" y="' + labY[i] + '">' + moneyLbl(maxV * f) + '</text>'; });
    h += '</g><g stroke="#2c2c2c" stroke-dasharray="3 5">';
    [20, 78, 137, 195].forEach((y) => { h += '<line x1="46" y1="' + y + '" x2="996" y2="' + y + '"/>'; });
    h += '</g><line x1="46" y1="254" x2="996" y2="254" stroke="#3a3a3a"/>';
    const step = Math.max(1, Math.ceil(pts.length / 8));
    h += '<g font-family="Inter, sans-serif" font-size="14" fill="#8a8a8a">';
    pts.forEach((p, i) => {
      if (i % step && i !== pts.length - 1) return;
      // clamp the first/last label inside the viewBox instead of letting it clip
      const anchor = i === 0 ? 'start' : (i === pts.length - 1 ? 'end' : 'middle');
      h += '<text x="' + X(i).toFixed(1) + '" y="284" text-anchor="' + anchor + '">' + Fastt.esc(p.label) + '</text>';
    });
    h += '</g>';
    h += '<polyline points="' + pts.map((p, i) => X(i).toFixed(1) + ',' + Y(p.v).toFixed(1)).join(' ') +
      '" fill="none" stroke="#4166f6" stroke-width="2.5" stroke-linejoin="round"/>';
    h += '<g fill="#4166f6">' + pts.map((p, i) =>
      '<circle cx="' + X(i).toFixed(1) + '" cy="' + Y(p.v).toFixed(1) + '" r="4"><title>' +
      Fastt.esc(p.label + ' · ' + Fastt.fmtCents(p.v)) + '</title></circle>').join('') + '</g>';
    trendSvg.innerHTML = h;
  }

  const acct = Fastt.account();

  // ── Referrals + Streams tiles (window-independent OF balances) ──
  async function loadSideTiles() {
    if (refTile) {
      const v = refTile.querySelector('.val'), p = refTile.querySelector('.pct');
      const lab = refTile.querySelector('.lab');
      if (!acct) { v.textContent = '—'; p.textContent = 'no creator selected'; setBadge(lab, false, 'NO LIVE DATA'); }
      else {
        try {
          const r = await Fastt.get('/api/of/v2/payments/referrals/balance');
          // referralEarnings is DOLLARS, not cents. 0 here is a real zero.
          v.textContent = Fastt.fmtMoney(r && r.referralEarnings);
          p.textContent = 'OF referral balance (all-time)';
          setBadge(lab, true);
        } catch (e) { v.textContent = '—'; p.textContent = 'no OF session'; setBadge(lab, false, 'NO LIVE DATA'); }
      }
    }
    if (streamTile) {
      const v = streamTile.querySelector('.val'), p = streamTile.querySelector('.pct');
      const lab = streamTile.querySelector('.lab');
      if (!acct) { v.textContent = '—'; p.textContent = 'no creator selected'; setBadge(lab, false, 'NO LIVE DATA'); }
      else {
        try {
          const r = await Fastt.get('/api/of/v2/streams/feed');
          const n = ((r && r.list) || []).length;
          v.textContent = Fastt.fmtInt(n);
          v.style.fontSize = '34px';
          p.textContent = n === 1 ? 'stream · OF reports no stream $' : 'streams · OF reports no stream $';
          // the COUNT is live; the money column has no OF field at all
          setBadge(lab, true);
          streamTile.title = 'OF /streams/feed returns the stream list only — it carries no earnings figure, so this tile counts streams instead of faking a dollar amount.';
        } catch (e) { v.textContent = '—'; p.textContent = 'no OF session'; setBadge(lab, false, 'NO LIVE DATA'); }
      }
    }
  }

  // ── OF payout ledger (Earnings breakdown + the net-earnings mode) ──
  // /api/of/v2/payouts/transactions IGNORES start/end (verified by curl), so we
  // page with offset= until the page's oldest row falls out of the window, then
  // filter on createdAt client-side. OF stamps carry +00:00 — already tz-aware.
  const bd = {
    gross: document.querySelector('#bdGross .r'),
    fee: document.querySelector('#bdFee .r'),
    vat: document.querySelector('#bdVat .r'),
    cb: document.querySelector('#bdCb .r'),
    ref: document.querySelector('#bdRef .r'),
    net: document.querySelector('#bdNet .r'),
    note: document.getElementById('bdNote'),
  };
  let ledger = null;           // {rows, gross, fee, vat, net, byType, truncated}
  Fastt.staticBadge(document.getElementById('bdRef'), 'NO OF FEED');
  document.getElementById('bdRef').title =
    'OF exposes no refund line item — descriptionDetails.type is only message | subscription | subscription_prolong | tips.';

  function bdBlank(msg) {
    ['gross', 'fee', 'vat', 'cb', 'net'].forEach((k) => { if (bd[k]) bd[k].textContent = '—'; });
    if (bd.ref) bd.ref.textContent = '—';
    if (bd.note) bd.note.textContent = msg;
  }

  async function loadLedger() {
    if (!acct) { ledger = null; bdBlank('No creator selected — OF payout ledger unavailable.'); setBadge(bdSect, false, 'NO LIVE DATA'); return; }
    bdBlank('Reading the OF payout ledger…');
    const winStart = dayStart(range.from), winEnd = dayEnd(range.to);
    const rows = [];
    let truncated = false, page = 0, exhausted = false;
    try {
      while (page < 8) {
        const r = await Fastt.get('/api/of/v2/payouts/transactions', { limit: 50, offset: page * 50 });
        const list = (r && r.list) || [];
        if (!list.length) { exhausted = true; break; }
        list.forEach((x) => {
          const t = Fastt.parseUtc(x.createdAt);
          if (t && t >= winStart && t <= winEnd) rows.push(x);
        });
        const oldest = Fastt.parseUtc(list[list.length - 1].createdAt);
        if (oldest && oldest < winStart) { exhausted = true; break; }
        if (!r.hasMore) { exhausted = true; break; }
        page += 1;
      }
      if (!exhausted) truncated = true;
    } catch (e) {
      ledger = null;
      bdBlank('No OF session for this creator — payout ledger unavailable.');
      setBadge(bdSect, false, 'NO LIVE DATA');
      return;
    }
    const sum = (f) => rows.reduce((a, x) => a + (Number(x[f]) || 0), 0);
    const byType = {};
    rows.forEach((x) => {
      const t = ((x.descriptionDetails || {}).type) || 'other';
      byType[t] = byType[t] || { gross: 0, net: 0, n: 0 };
      byType[t].gross += Number(x.amount) || 0;
      byType[t].net += Number(x.net) || 0;
      byType[t].n += 1;
    });
    ledger = { rows, gross: sum('amount'), fee: sum('fee'), vat: sum('vatAmount'), net: sum('net'), byType, truncated };

    bd.gross.textContent = Fastt.fmtMoney(ledger.gross);
    bd.fee.textContent = '−' + Fastt.fmtMoney(ledger.fee);
    bd.vat.textContent = Fastt.fmtMoney(ledger.vat);
    bd.net.textContent = Fastt.fmtMoney(ledger.net);
    bd.ref.textContent = 'no OF line item';
    bd.ref.style.color = '#8a8a8a';
    bd.ref.style.fontWeight = '400';

    let cbTxt = '—';
    try {
      const cb = await Fastt.get('/api/of/v2/payouts/chargebacks/ratio', { start: range.from, end: range.to });
      const ratio = Number(cb && cb.chargebacksRatio);
      if (isFinite(ratio)) cbTxt = (ratio * 100).toFixed(2) + '%';
    } catch (e) { cbTxt = 'unavailable'; }
    bd.cb.textContent = cbTxt;
    document.getElementById('bdCb').title =
      'OF /payouts/chargebacks/ratio returns a RATIO, not a dollar figure — shown as a percentage rather than invented cents.';

    const cleared = rows.filter((x) => x.status === 'done').length;
    bd.note.innerHTML = 'OnlyFans payout ledger · ' + Fastt.fmtInt(rows.length) + ' transaction' +
      (rows.length === 1 ? '' : 's') + ' in ' + Fastt.esc(longLbl(range.from)) + ' → ' + Fastt.esc(longLbl(range.to)) +
      ' · ' + Fastt.fmtInt(cleared) + ' cleared, ' + Fastt.fmtInt(rows.length - cleared) + ' pending.' +
      (truncated ? '<br>Only the 400 most recent OF transactions were scanned — widen with a shorter window for an exact figure.' : '') +
      '<br>This panel is OF’s own money; the tiles above come from fastt’s ingest, so the two can differ while a sale is still settling.';
    setBadge(bdSect, true);
  }

  // ── earnings summary tiles (combined selection) ──
  let pmRow = null, pmPrevTotal = 0;
  const netToggle = document.getElementById('netToggle');
  const netOn = () => netToggle && netToggle.classList.contains('on');

  function renderTiles() {
    if (!pmRow) return;
    const by = pmRow.revenue_by_kind || {};
    const pby = pmRow.pending_by_kind || {};
    // Net = OF's per-account payout ledger, so it only applies when the selection
    // is exactly the signed-in creator; otherwise stay on gross (combined) figures.
    const useNet = netOn() && ledger && !isAllSel() && selectedIds.size === 1 && selectedIds.has(String(acct));
    let buckets, pending, total, srcTxt;
    if (useNet) {
      const t = (k) => (ledger.byType[k] ? ledger.byType[k].net : 0);
      buckets = {
        total: Math.round(ledger.net * 100),
        subscription: Math.round((t('subscription') + t('subscription_prolong')) * 100),
        post: null,                       // OF's ledger has no post line item
        ppv: Math.round(t('message') * 100),
        tip: Math.round(t('tips') * 100),
      };
      pending = null;
      total = buckets.total;
      srcTxt = 'Source: OnlyFans payout ledger — NET, after the platform fee. ' +
        'Posts have no OF ledger line item (types are message | subscription | subscription_prolong | tips), so that tile reads “—”. ' +
        'Turn “Show net earnings” off for fastt’s gross ingest figures.';
    } else {
      total = pmRow.total_revenue_cents || 0;
      buckets = {
        total: total,
        subscription: (by.subscription || 0) + (by.rebill || 0),
        post: by.post || 0,
        ppv: by.ppv || 0,
        tip: by.tip || 0,
      };
      pending = {
        total: pmRow.pending_revenue_cents || 0,
        subscription: (pby.subscription || 0) + (pby.rebill || 0),
        post: pby.post || 0,
        ppv: pby.ppv || 0,
        tip: pby.tip || 0,
      };
      srcTxt = 'Source: fastt ingest (/admin/stats/per-model) — GROSS, before OnlyFans’ platform fee. ' +
        'Turn “Show net earnings” on for OF’s own net payout ledger.';
    }
    Object.keys(tiles).forEach((k) => {
      const el = tiles[k];
      const v = el.querySelector('.val'), p = el.querySelector('.pct');
      if (buckets[k] === null) {
        v.textContent = 'n/a';
        v.style.color = '#6a6a6a';
        v.style.fontSize = '26px';
        p.textContent = 'no OF ledger line item';
        return;
      }
      v.style.color = ''; v.style.fontSize = '';
      v.textContent = Fastt.fmtCents(buckets[k]);
      if (k === 'total') {
        p.textContent = pmPrevTotal > 0
          ? ((total >= pmPrevTotal ? '+' : '') + (100 * (total - pmPrevTotal) / pmPrevTotal).toFixed(1) + '% vs prev ' + spanDays() + 'd')
          : 'no revenue in the previous ' + spanDays() + 'd';
      } else {
        p.textContent = (total > 0 ? (100 * buckets[k] / total).toFixed(2) : '0.00') + '% of total' +
          (pending && pending[k] > 0 ? ' · ' + Fastt.fmtCents(pending[k]) + ' pending' : '');
      }
    });
    if (pending && pending.total > 0) {
      pendLine.textContent = 'incl. ' + Fastt.fmtCents(pending.total) + ' pending' +
        (pmRow.pending_clears_by ? ' · clears by ' + longLbl(pmRow.pending_clears_by) : '');
    } else if (useNet) {
      // OF's own pending flag: status "loading" = not cleared yet, "done" = cleared
      const stillClearing = ledger.rows.filter((x) => x.status !== 'done')
        .reduce((a, x) => a + (Number(x.net) || 0), 0);
      pendLine.textContent = stillClearing > 0
        ? 'incl. ' + Fastt.fmtMoney(stillClearing) + ' still clearing (OF status “loading”)' : '';
    } else { pendLine.textContent = ''; }
    if (sumSrc) sumSrc.textContent = srcTxt;
  }

  // Earnings summary tiles from the COMBINED selection (all models by default).
  // The channel multi-line is drawn by renderTrend (it needs the period buckets).
  function renderSummary() {
    pmRow = combineRows(selModels());
    pmPrevTotal = selPrevTotal();
    if (!allModels.length || !selModels().length) {
      setBadge(summarySect, false, 'NO ROWS');
      setBadge(chanSect, false, 'NO ROWS');
      zeroTiles(allModels.length
        ? 'No model matches the current filter — pick at least one in the Models filter.'
        : 'No per-model rows in ' + longLbl(range.from) + ' → ' + longLbl(range.to) + '.');
      return false;
    }
    renderTiles();
    setBadge(summarySect, true);
    setBadge(chanSect, true);
    return true;
  }

  // ── earnings trends ──
  // Selection-aware day revenue. All models ⇒ one aggregate call (no account_id);
  // a subset ⇒ one call per picked model, summed (mirrors the reference dashboard —
  // /admin/stats/revenue can't group by day AND account at once).
  async function loadDays(fromIso, toIso) {
    const q = { from: fromIso, to: toIso, group_by: 'day' };
    const merge = (rows, m) => { (rows || []).forEach((x) => { if (x.day) m[x.day] = (m[x.day] || 0) + (x.total_cents || 0); }); return m; };
    if (isAllSel()) {
      const r = await Fastt.get('/admin/stats/revenue', q, { noAccount: true });
      return merge(r.rows, {});
    }
    const ids = Array.from(selectedIds);
    const parts = await Promise.all(ids.map((id) =>
      Fastt.get('/admin/stats/revenue', Object.assign({ account_id: id }, q), { noAccount: true }).catch(() => ({ rows: [] }))));
    const m = {}; parts.forEach((r) => merge(r.rows, m)); return m;
  }
  let trendMode = 'day';
  async function renderTrend(mode) {
    trendMode = mode;
    if (!allModels.length || !selModels().length) { blankTrend('No models in this selection'); blankChannel(''); setBadge(trendSect, false, 'NO LIVE DATA'); return; }
    const s = dayStart(range.from), e = dayStart(range.to);
    const nDays = spanDays();
    const pts = [];
    if (mode === 'day') {
      const m = await loadDays(range.from, range.to);
      for (let i = 0; i < nDays; i++) {
        const d = isoDay(new Date(s.getTime() + i * DAY));
        pts.push({ label: shortLbl(d), v: m[d] || 0 });
      }
    } else if (mode === 'week') {
      const m = await loadDays(range.from, range.to);
      const nW = Math.max(1, Math.ceil(nDays / 7));
      for (let w = 0; w < nW; w++) {
        const wStart = new Date(s.getTime() + w * 7 * DAY);
        let sum = 0;
        for (let i = 0; i < 7; i++) {
          const d = new Date(wStart.getTime() + i * DAY);
          if (d > e) break;
          sum += m[isoDay(d)] || 0;
        }
        pts.push({ label: shortLbl(isoDay(wStart)), v: sum });
      }
    } else { // month buckets inside the window
      const m = await loadDays(range.from, range.to);
      const byMonth = {};
      Object.keys(m).forEach((d) => { const k = d.slice(0, 7); byMonth[k] = (byMonth[k] || 0) + m[d]; });
      let cur = new Date(Date.UTC(s.getUTCFullYear(), s.getUTCMonth(), 1));
      while (cur <= e) {
        const k = isoDay(cur).slice(0, 7);
        pts.push({ label: cur.toLocaleDateString('en-US', { month: 'short', year: '2-digit', timeZone: 'UTC' }), v: byMonth[k] || 0 });
        cur = new Date(Date.UTC(cur.getUTCFullYear(), cur.getUTCMonth() + 1, 1));
      }
    }
    if (!pts.length) { blankTrend('No days in the selected window'); blankChannel(''); setBadge(trendSect, false, 'EMPTY WINDOW'); return; }
    drawTrend(pts);
    drawChannel(pts);          // channel lines ride the same period buckets
    setBadge(trendSect, true);
  }
  const segBtns = document.querySelectorAll('#trendSeg button');
  segBtns.forEach((b) => {
    const mode = b.textContent.trim().toLowerCase();
    if (mode === 'hour') {
      b.title = '/admin/stats/revenue groups by day or kind only — there is no hourly bucket to read';
      b.style.opacity = '.55';
    }
    b.addEventListener('click', () => {
      if (mode === 'hour') {
        Fastt.toast('Hourly granularity has no backing endpoint (revenue groups by day|kind) — showing Day');
        segBtns.forEach((x) => x.classList.toggle('on', x.textContent.trim() === 'Day'));
        renderTrend('day').catch((e) => { blankTrend('Earnings trend unavailable'); setBadge(trendSect, false, 'NO LIVE DATA'); Fastt.oops(e); });
        return;
      }
      renderTrend(mode).catch((e) => { blankTrend('Earnings trend unavailable'); setBadge(trendSect, false, 'NO LIVE DATA'); Fastt.oops(e); });
    });
  });

  // ── Creator performance tab ──
  // ── Creator performance = per-model KPI cards (infloww-style) ──
  const perfCards = document.getElementById('perfCards');
  const perfNote = document.getElementById('perfNote');
  const perfHideZero = document.getElementById('perfHideZero');
  const perfHidden = document.getElementById('perfHidden');
  const perfCount = document.getElementById('perfCount');
  const perfExportBtn = document.getElementById('perfExport');
  let perfShown = false;

  const paidOf = (r) => { const by = r.revenue_by_kind || {}, p = r.pending_by_kind || {};
    return ((by.subscription || 0) + (by.rebill || 0) + (p.subscription || 0) + (p.rebill || 0)) > 0; };
  const kcChip = (label, cents, pend) => (!cents && !pend) ? '' :
    '<span class="kc-chip"><span class="kl">' + label + '</span><span class="kv">' + Fastt.fmtCents(cents) + '</span>' +
    (pend > 0 ? '<span class="kp">·' + Fastt.fmtCents(pend) + 'p</span>' : '') + '</span>';
  const kcKpi = (label, val, mut) => '<div class="kc-kpi"><div class="kk">' + label + '</div><div class="kvv' + (mut ? ' mut' : '') + '">' + val + '</div></div>';
  function modelCardHtml(r) {
    const acc = accById[String(r.account_id)] || {};
    const name = acc.nickname || r.display_name || String(r.account_id);
    const av = acc.avatar
      ? '<img src="/img?u=' + encodeURIComponent(acc.avatar) + '" alt="" loading="lazy" onerror="this.remove()">'
      : Fastt.esc(String(name).trim().slice(0, 1).toUpperCase());
    const paid = paidOf(r);
    const by = r.revenue_by_kind || {}, p = r.pending_by_kind || {};
    const active = (typeof r.active_subs_count === 'number') ? r.active_subs_count : null;
    return '<div class="kpicard">' +
      '<div class="kc-head"><div class="kc-av"' + (acc.color ? ' style="background:' + Fastt.esc(acc.color) + '"' : '') + '>' + av + '</div>' +
        '<div class="kc-id"><div class="kc-name"><span class="nm">' + Fastt.esc(name) + '</span>' +
          '<span class="kc-badge ' + (paid ? 'paid' : 'free') + '">' + (paid ? 'paid' : 'free') + '</span></div>' +
          '<div class="kc-handle">' + Fastt.esc(String(r.account_id)) + '</div></div></div>' +
      '<div class="kc-total">' + Fastt.fmtCents(r.total_revenue_cents || 0) + '</div>' +
      ((r.pending_revenue_cents || 0) > 0
        ? '<div class="kc-pend">incl. <span class="pend">' + Fastt.fmtCents(r.pending_revenue_cents) + ' pending</span>' +
          (r.pending_clears_by ? ' · clears by ' + Fastt.esc(longLbl(r.pending_clears_by)) : '') + '</div>' : '') +
      '<div class="kc-revlbl">total revenue</div>' +
      '<div class="kc-chips">' +
        kcChip('PPV', by.ppv || 0, p.ppv || 0) + kcChip('Post', by.post || 0, p.post || 0) + kcChip('Tip', by.tip || 0, p.tip || 0) +
        kcChip('Sub', by.subscription || 0, p.subscription || 0) + kcChip('Rebill', by.rebill || 0, p.rebill || 0) + kcChip('Other', by.custom || 0, p.custom || 0) +
      '</div>' +
      '<div class="kc-kpis">' +
        kcKpi('Fans', active == null ? '—' : Fastt.fmtInt(active), active == null) +
        kcKpi('New subs', Fastt.fmtInt(r.new_subs_count || 0)) +
        kcKpi('LTV', r.ltv_cents == null ? '—' : Fastt.fmtCents(r.ltv_cents), r.ltv_cents == null) +
        kcKpi('ARPU', r.arpu_cents == null ? '—' : Fastt.fmtCents(r.arpu_cents), r.arpu_cents == null) +
        kcKpi('Messages', Fastt.fmtInt(r.messages_sent || 0)) +
        kcKpi('PPVs sold', Fastt.fmtInt(r.ppv_conversions || 0)) +
      '</div></div>';
  }
  function renderPerfCards() {
    if (!perfCards) return;
    if (!allModels.length) {
      perfCards.innerHTML = ''; perfHidden.textContent = ''; if (perfCount) perfCount.textContent = '';
      perfNote.textContent = 'No creators returned for this window.'; setBadge(perfSect, false, 'NO ROWS'); return;
    }
    let rows = selModels().slice();
    const before = rows.length;
    if (perfHideZero.checked) rows = rows.filter((r) => (r.total_revenue_cents || 0) > 0);
    rows.sort((a, b) => (b.total_revenue_cents || 0) - (a.total_revenue_cents || 0));
    perfHidden.textContent = (before - rows.length)
      ? (before - rows.length) + ' zero-revenue creator' + (before - rows.length === 1 ? '' : 's') + ' hidden' : '';
    if (perfCount) perfCount.textContent = rows.length + ' model' + (rows.length === 1 ? '' : 's') + (isAllSel() ? '' : ' selected');
    perfCards.innerHTML = rows.length
      ? rows.map(modelCardHtml).join('')
      : '<div style="grid-column:1/-1;text-align:center;color:#8a8a8a;padding:40px">' +
        (before ? 'No creator earned in this window — untick “Hide $0 creators” to see all ' + before + '.' : 'No creators returned.') + '</div>';
    perfNote.textContent = 'GET /admin/stats/per-model (no account_id) · ' + longLbl(range.from) + ' → ' + longLbl(range.to) +
      ' · active-subs source: ' + activeSubsSrc + '. Fans = OF active subscribers; New subs = new/renewed paid subs in the window. Gross cents.';
    setBadge(perfSect, true);
  }
  window.__loadPerf = () => { perfShown = true; renderPerfCards(); };
  perfHideZero.addEventListener('change', renderPerfCards);

  // ── Export CSV: the per-model rows in view, fixed metric set ──
  function perfCsv() {
    if (!allModels.length) { Fastt.toast('No model data loaded yet'); return; }
    let rows = selModels().slice();
    if (perfHideZero.checked) rows = rows.filter((r) => (r.total_revenue_cents || 0) > 0);
    rows.sort((a, b) => (b.total_revenue_cents || 0) - (a.total_revenue_cents || 0));
    if (!rows.length) { Fastt.toast('No rows in the current view to export'); return; }
    const d = (c) => (Number(c || 0) / 100).toFixed(2);
    const field = (v) => { const s = String(v); return /[",\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s; };
    const lines = [];
    lines.push('# Creator reports — per-model KPIs');
    lines.push('# generated,' + new Date().toISOString());
    lines.push('# window,' + range.from + ',' + range.to);
    lines.push('# source,GET /admin/stats/per-model (no account_id) — GROSS cents → dollars');
    lines.push('# selection,' + (isAllSel() ? 'all models' : Array.from(selectedIds).join(' ')));
    lines.push('');
    lines.push(['account_id', 'creator', 'total', 'ppv', 'post', 'tip', 'sub', 'rebill', 'other', 'pending', 'fans', 'new_subs', 'ltv', 'arpu', 'messages', 'ppvs_sold'].join(','));
    rows.forEach((r) => {
      const by = r.revenue_by_kind || {}, acc = accById[String(r.account_id)] || {};
      lines.push([r.account_id, acc.nickname || r.display_name || r.account_id, d(r.total_revenue_cents),
        d(by.ppv), d(by.post), d(by.tip), d(by.subscription), d(by.rebill), d(by.custom), d(r.pending_revenue_cents),
        (r.active_subs_count == null ? '' : r.active_subs_count), (r.new_subs_count || 0),
        (r.ltv_cents == null ? '' : d(r.ltv_cents)), (r.arpu_cents == null ? '' : d(r.arpu_cents)),
        (r.messages_sent || 0), (r.ppv_conversions || 0)].map(field).join(','));
    });
    const blob = new Blob([lines.join('\n')], { type: 'text/csv;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'creator-performance_' + range.from + '_' + range.to + '.csv';
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 0);
    Fastt.toast(rows.length + ' creator' + (rows.length === 1 ? '' : 's') + ' exported to CSV');
  }
  if (perfExportBtn) {
    perfExportBtn.title = 'Download the per-model rows in view as a CSV (client-side; nothing sent)';
    perfExportBtn.addEventListener('click', perfCsv);
  }

  // ── net-earnings toggle: real gross↔net switch ──
  if (netToggle) {
    netToggle.title = 'Gross = fastt ingest (/admin/stats/per-model). Net = OnlyFans payout ledger (/payouts/transactions), after the platform fee.';
    netToggle.addEventListener('click', () => {
      // the page's own visual handler already flipped .on before this runs
      const single = !isAllSel() && selectedIds.size === 1 && selectedIds.has(String(acct));
      if (netOn() && (!single || !ledger)) {
        netToggle.classList.remove('on');
        Fastt.toast(!single
          ? 'Net earnings is per-creator — filter to just ' + ((accById[String(acct)] || {}).nickname || acct) + ' to use it'
          : 'Net needs the OF payout ledger — it did not load');
        return;
      }
      renderTiles();
    });
  }

  // ── Reset ──
  const resetEl = document.querySelector('.fbar .reset');
  if (resetEl) {
    resetEl.title = 'Back to the last 7 days, gross';
    resetEl.addEventListener('click', () => {
      const to = new Date();
      if (netToggle) netToggle.classList.remove('on');
      applyRange(isoDay(new Date(to.getTime() - 6 * DAY)), isoDay(to));
    });
  }

  // ── orchestration ──
  // Re-render every panel for the CURRENT selection without re-fetching per-model
  // (allModels is already in hand); only the trend/channel refetch, since revenue
  // is per-account. Called on every Models-filter change.
  function applySelection() {
    if (window.__paintFilterLbl) window.__paintFilterLbl();
    renderSummary();
    renderTrend(trendMode).catch((e) => {
      blankTrend('Earnings trend unavailable'); blankChannel('');
      setBadge(trendSect, false, 'NO LIVE DATA'); Fastt.oops(e);
    });
    if (perfShown) renderPerfCards();
  }
  async function reloadAll() {
    zeroTiles('Loading…');
    blankTrend('Loading earnings trend…');
    blankChannel('');
    try {
      await loadModels();
    } catch (e) {
      zeroTiles('Earnings data unavailable — see the error toast.');
      blankTrend('Earnings trend unavailable');
      setBadge(summarySect, false, 'NO LIVE DATA');
      setBadge(chanSect, false, 'NO LIVE DATA');
      Fastt.oops(e);
      return;
    }
    if (window.__paintFilterLbl) window.__paintFilterLbl();
    const ok = renderSummary();
    await loadLedger();
    if (ok) renderTiles();               // re-render in case the net mode is on
    await renderTrend(trendMode).catch((e) => {
      blankTrend('Earnings trend unavailable — see the error toast');
      blankChannel('');
      setBadge(trendSect, false, 'NO LIVE DATA');
      Fastt.oops(e);
    });
    if (perfShown) renderPerfCards();
  }
  function applyRange(f, t) {
    range.from = f; range.to = t;
    paintRange();
    reloadAll().catch(Fastt.oops);
  }
  mountRangePicker(dateField, applyRange);
  if (dateField) Fastt.liveBadge(dateField);

  await loadSideTiles();
  await reloadAll();
});
