// ==== LIVE WIRING (fastt relay) ====
// Reads
//   /admin/stats/per-employee[?by_account=true] → sales chart, ranking, comparison,
//        Employee-performance tab. Org-wide by design: employees chat across creators.
//   /api/of/v2/payouts/transactions             → Sales-records tab (the real earning ledger)
// The date field owns from/to for every read; the granularity field picks the bucket size.
// The endpoint has no group_by, so each chart bucket costs one call — they are issued
// STRICTLY SEQUENTIALLY (Promise.all measured 13.9s vs 6.0s and pins to_thread slots,
// the known relay-starvation surface), and the bucket count is capped at 12.
// The synthetic "Unattributed" bucket (employee_id=null) is INCLUDED everywhere, muted
// amber — dropping it makes the totals disagree with per-model (real app: PerEmployeeTable).
Fastt.ready(async () => {
  const DAY = 86400000;
  const isoDay = (d) => d.toISOString().slice(0, 10);
  const dayStart = (s) => new Date(s + 'T00:00:00Z');
  const dayEnd = (s) => new Date(s + 'T23:59:59.999Z');
  const shortLbl = (s) => dayStart(s).toLocaleDateString('en-US', { month: 'short', day: 'numeric', timeZone: 'UTC' });
  const longLbl = (s) => dayStart(s).toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric', timeZone: 'UTC' });

  const now = new Date();
  const range = { from: isoDay(new Date(now.getTime() - 6 * DAY)), to: isoDay(now) };
  const spanDays = () => Math.max(1, Math.round((dayStart(range.to) - dayStart(range.from)) / DAY) + 1);
  let grain = 'day';                                   // day | week | month
  const opts = { includeOrphan: true, hideZero: true };

  const fields = Array.prototype.slice.call(document.querySelectorAll('.utab-right .field'));
  const dateField = fields[0], grainField = fields[1];

  function paintRange() {
    if (!dateField) return;
    const tn = Array.prototype.filter.call(dateField.childNodes,
      (n) => n.nodeType === 3 && n.textContent.trim());
    if (tn.length >= 2) { tn[0].textContent = ' ' + longLbl(range.from) + ' '; tn[1].textContent = ' ' + longLbl(range.to) + ' '; }
  }
  function paintGrain() {
    if (!grainField) return;
    const tn = Array.prototype.filter.call(grainField.childNodes,
      (n) => n.nodeType === 3 && n.textContent.trim());
    if (tn.length) tn[0].textContent = 'Shown by ' + grain;
  }
  paintRange(); paintGrain();

  function popAt(el, html, onPick) {
    document.querySelectorAll('.ft-pop').forEach((n) => n.remove());
    const pop = document.createElement('div');
    pop.className = 'ft-pop';
    const r = el.getBoundingClientRect();
    pop.style.left = Math.max(8, Math.min(r.left, window.innerWidth - 300)) + 'px';
    pop.style.top = (r.bottom + 6) + 'px';
    pop.innerHTML = html;
    document.body.appendChild(pop);
    // keep it on screen — a popover anchored low in the viewport flips above
    const ph = pop.getBoundingClientRect().height;
    if (r.bottom + 6 + ph > window.innerHeight - 8) {
      pop.style.top = Math.max(8, r.top - ph - 6) + 'px';
      if (ph > window.innerHeight - 16) { pop.style.top = '8px'; pop.style.maxHeight = (window.innerHeight - 16) + 'px'; pop.style.overflowY = 'auto'; }
    }
    const close = () => { pop.remove(); document.removeEventListener('click', close); };
    setTimeout(() => document.addEventListener('click', close), 0);
    pop.addEventListener('click', (e) => { e.stopPropagation(); if (onPick) onPick(e, close, pop); });
    return pop;
  }

  if (dateField) {
    dateField.title = 'Change the reporting window — chart, ranking, comparison and both tabs re-query';
    dateField.addEventListener('click', (ev) => {
      ev.stopPropagation();
      popAt(dateField,
        '<div class="pr"><button data-d="7">Last 7 days</button><button data-d="14">Last 14 days</button>' +
        '<button data-d="30">Last 30 days</button><button data-d="90">Last 90 days</button></div>' +
        '<div class="rw"><span>From</span><input type="date" data-k="f" value="' + range.from + '"></div>' +
        '<div class="rw"><span>To</span><input type="date" data-k="t" value="' + range.to + '"></div>' +
        '<button class="go" type="button">Apply</button>',
        (e, close, pop) => {
          const b = e.target.closest('.pr button');
          if (b) {
            const to = new Date();
            close();
            applyRange(isoDay(new Date(to.getTime() - (Number(b.dataset.d) - 1) * DAY)), isoDay(to));
            return;
          }
          if (e.target.closest('.go')) {
            const f = pop.querySelector('input[data-k="f"]').value;
            const t = pop.querySelector('input[data-k="t"]').value;
            if (!f || !t) { Fastt.toast('Pick both dates'); return; }
            if (f > t) { Fastt.toast('“From” must be on or before “To”'); return; }
            close(); applyRange(f, t);
          }
        });
    });
    Fastt.liveBadge(dateField);
  }
  if (grainField) {
    grainField.title = 'Bucket size for the Employee-sales chart. Each bucket is one /admin/stats/per-employee call, so the count is capped at 12.';
    grainField.addEventListener('click', (ev) => {
      ev.stopPropagation();
      popAt(grainField,
        ['day', 'week', 'month'].map((g) =>
          '<div class="rw pick" data-g="' + g + '" style="color:' + (g === grain ? '#fff' : '#cfcfcf') + '"><span>Shown by ' + g +
          '</span><span>' + (g === grain ? '●' : '') + '</span></div>').join(''),
        (e, close) => {
          const row = e.target.closest('[data-g]');
          if (!row) return;
          close(); grain = row.dataset.g; paintGrain();
          note('Loading employee sales…');
          loadChart().catch(Fastt.oops);
        });
    });
    grainField.style.cursor = 'pointer';
  }

  // ── "Filters" → what actually filters this page ──
  const filterBtn = document.querySelector('.btn-filter');
  if (filterBtn) {
    filterBtn.title = 'Ranking / chart options';
    filterBtn.addEventListener('click', (ev) => {
      ev.stopPropagation();
      popAt(filterBtn,
        '<div class="rw pick" data-o="includeOrphan"><span>Include “Unattributed”</span><span>' + (opts.includeOrphan ? '☑' : '☐') + '</span></div>' +
        '<div class="rw pick" data-o="hideZero"><span>Hide $0 employees</span><span>' + (opts.hideZero ? '☑' : '☐') + '</span></div>' +
        '<div class="rw" style="font-size:11.5px;line-height:1.45;color:#6a6a6a;display:block">Unattributed = orphan PPV unlocks and tips with no sending employee. It is real revenue; hiding it makes these totals disagree with Creator reports.</div>',
        (e, close) => {
          const row = e.target.closest('[data-o]');
          if (!row) return;
          opts[row.dataset.o] = !opts[row.dataset.o];
          close();
          renderRanking(); loadChart().catch(Fastt.oops); renderPerf();
        });
    });
  }

  const sects = Array.prototype.slice.call(document.querySelectorAll('.sect'));
  const sectBy = (t) => sects.find((s) => s.textContent.trim().indexOf(t) === 0) || null;
  const salesSect = sectBy('Employee sales');
  const compSect = sectBy('Employee comparison');
  const recSect = sectBy('Sales records');
  const perfSect = sectBy('Employee performance');
  const autoSect = sectBy('Automation performance');
  const setBadge = (el, live, txt) => {
    if (!el) return;
    el.querySelectorAll(':scope > .ft-live, :scope > .ft-static').forEach((n) => n.remove());
    if (live) Fastt.liveBadge(el); else Fastt.staticBadge(el, txt);
  };

  // ── ranking column ──
  const rankHead = document.querySelector('.rank-head .t');
  const rankSortEl = document.getElementById('rankSort');
  const rcol = document.querySelector('.rcol');
  let rankDir = -1;
  const rankNote = (txt) => {
    rcol.querySelectorAll('.rank-item').forEach((n) => n.remove());
    const d = document.createElement('div');
    d.className = 'rank-item';
    d.innerHTML = '<span class="rank-name" style="color:#8a8a8a">' + Fastt.esc(txt) + '</span>';
    rcol.appendChild(d);
  };
  rankNote('Loading…');

  // ── chart plumbing ──
  const svg = document.querySelector('svg.chart');
  const niceMax = (v) => {
    if (v <= 0) return 100;
    const p = Math.pow(10, Math.floor(Math.log10(v)));
    const n = v / p;
    return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10) * p;
  };
  const moneyLbl = (c) => { const d = c / 100; return d >= 1000 ? '$' + (d / 1000).toFixed(1) + 'k' : '$' + Math.round(d); };
  const L = 46, R = 994, T = 16, B = 290;
  const grid = () => {
    let g = '<g stroke="#242424" stroke-dasharray="3 5">';
    [16, 71, 126, 181, 235].forEach((y) => { g += '<line x1="46" y1="' + y + '" x2="994" y2="' + y + '"/>'; });
    return g + '</g><line x1="46" y1="290" x2="994" y2="290" stroke="#3a3a3a"/>';
  };
  function note(msg) {
    svg.innerHTML = grid() +
      '<text x="520" y="150" text-anchor="middle" fill="#8a8a8a" font-family="Inter, sans-serif" font-size="14">' +
      Fastt.esc(msg) + '</text>';
  }
  /** pts: [{label, total, orphan}] — `total` counts every bucket, `orphan` is the
   *  Unattributed slice drawn as a second amber line so it stays visible. */
  function drawChart(pts, expected) {
    const maxV = niceMax(Math.max.apply(null, pts.map((p) => p.total).concat([0])));
    const n = Math.max(1, expected || pts.length);
    const X = (i) => n <= 1 ? (L + R) / 2 : L + i * (R - L) / (n - 1);
    const Y = (v) => B - (v / maxV) * (B - T);
    const fr = [1, 0.8, 0.6, 0.4, 0.2, 0];
    const labY = [21, 76, 131, 186, 241, 295];
    let h = '<g font-family="Inter, sans-serif" font-size="14" fill="#6a6a6a" text-anchor="end">';
    fr.forEach((f, i) => { h += '<text x="40" y="' + labY[i] + '">' + moneyLbl(maxV * f) + '</text>'; });
    h += '</g>' + grid();
    const step = Math.max(1, Math.ceil(pts.length / 9));
    h += '<g font-family="Inter, sans-serif" font-size="14" fill="#8a8a8a">';
    pts.forEach((p, i) => {
      if (i % step && i !== pts.length - 1) return;
      const anchor = i === 0 ? 'start' : (i === pts.length - 1 && pts.length === n ? 'end' : 'middle');
      h += '<text x="' + X(i).toFixed(1) + '" y="320" text-anchor="' + anchor + '">' + Fastt.esc(p.label) + '</text>';
    });
    h += '</g>';
    if (opts.includeOrphan && pts.some((p) => p.orphan > 0)) {
      h += '<polyline points="' + pts.map((p, i) => X(i).toFixed(1) + ',' + Y(p.orphan).toFixed(1)).join(' ') +
        '" fill="none" stroke="#e0b24a" stroke-width="2" stroke-dasharray="5 4" stroke-linejoin="round"/>';
    }
    h += '<polyline points="' + pts.map((p, i) => X(i).toFixed(1) + ',' + Y(p.total).toFixed(1)).join(' ') +
      '" fill="none" stroke="#4166f6" stroke-width="2.5" stroke-linejoin="round"/>';
    h += '<g fill="#4166f6">' + pts.map((p, i) =>
      '<circle cx="' + X(i).toFixed(1) + '" cy="' + Y(p.total).toFixed(1) + '" r="4"><title>' +
      Fastt.esc(p.label + ' · ' + Fastt.fmtCents(p.total) +
        (p.orphan ? ' (incl. ' + Fastt.fmtCents(p.orphan) + ' unattributed)' : '')) + '</title></circle>').join('') + '</g>';
    // legend
    h += '<g font-family="Inter, sans-serif" font-size="12.5" fill="#8a8a8a">' +
      '<rect x="52" y="2" width="14" height="3" fill="#4166f6"/><text x="72" y="8">All employee sales</text>';
    if (opts.includeOrphan) {
      h += '<rect x="208" y="2" width="14" height="3" fill="#e0b24a"/><text x="228" y="8">of which unattributed</text>';
    }
    h += '</g>';
    svg.innerHTML = h;
  }
  note('Loading employee sales…');

  // ── bucket maths ──
  function buckets() {
    const s = dayStart(range.from), e = dayStart(range.to), n = spanDays();
    let list = [];
    if (grain === 'day') {
      for (let i = 0; i < n; i++) {
        const d = isoDay(new Date(s.getTime() + i * DAY));
        list.push({ from: d, to: d, label: shortLbl(d) });
      }
    } else if (grain === 'week') {
      for (let i = 0; i < n; i += 7) {
        const a = new Date(s.getTime() + i * DAY);
        const bEnd = new Date(Math.min(a.getTime() + 6 * DAY, e.getTime()));
        list.push({ from: isoDay(a), to: isoDay(bEnd), label: shortLbl(isoDay(a)) });
      }
    } else {
      let cur = new Date(Date.UTC(s.getUTCFullYear(), s.getUTCMonth(), 1));
      while (cur <= e) {
        const mEnd = new Date(Date.UTC(cur.getUTCFullYear(), cur.getUTCMonth() + 1, 0));
        list.push({
          from: isoDay(new Date(Math.max(cur.getTime(), s.getTime()))),
          to: isoDay(new Date(Math.min(mEnd.getTime(), e.getTime()))),
          label: cur.toLocaleDateString('en-US', { month: 'short', year: '2-digit', timeZone: 'UTC' }),
        });
        cur = new Date(Date.UTC(cur.getUTCFullYear(), cur.getUTCMonth() + 1, 1));
      }
    }
    return list;
  }

  async function loadChart() {
    let list = buckets();
    if (list.length > 12) {
      const next = grain === 'day' ? 'week' : 'month';
      Fastt.toast(list.length + ' ' + grain + ' buckets is one relay call each — switched to ' + next);
      grain = next; paintGrain();
      list = buckets();
      if (list.length > 12) list = list.slice(-12);
    }
    const pts = [];
    for (let i = 0; i < list.length; i++) {
      let r;
      try {
        r = await Fastt.get('/admin/stats/per-employee', { from: list[i].from, to: list[i].to });
      } catch (err) {
        if (!pts.length) { note('Employee sales unavailable — see the error toast'); setBadge(salesSect, false, 'NO LIVE DATA'); Fastt.oops(err); return; }
        break;
      }
      const emps = (r.employees || []);
      const orphan = emps.filter((e) => e.employee_id === null)
        .reduce((a, e) => a + (e.revenue_cents || 0), 0);
      const named = emps.filter((e) => e.employee_id !== null)
        .reduce((a, e) => a + (e.revenue_cents || 0), 0);
      pts.push({ label: list[i].label, total: named + (opts.includeOrphan ? orphan : 0), orphan: orphan });
      drawChart(pts, list.length);
    }
    if (!pts.length) { note('No buckets in the selected window'); setBadge(salesSect, false, 'EMPTY WINDOW'); return; }
    setBadge(salesSect, true);
  }

  // ── whole-window rollup (ranking + comparison + performance tab) ──
  let winRows = null;          // per-employee rows for the window, with per_account[]
  function visibleRows() {
    let rows = (winRows || []).slice();
    if (!opts.includeOrphan) rows = rows.filter((r) => r.employee_id !== null);
    if (opts.hideZero) rows = rows.filter((r) => (r.revenue_cents || 0) > 0);
    return rows;
  }
  function renderRanking() {
    if (!winRows) return;
    const rows = visibleRows().sort((a, b) => ((b.revenue_cents || 0) - (a.revenue_cents || 0)) * (rankDir < 0 ? 1 : -1));
    rcol.querySelectorAll('.rank-item').forEach((n) => n.remove());
    if (!rows.length) { rankNote('No employee sales in this window'); setBadge(rankHead, true); return; }
    rows.slice(0, 10).forEach((e, i) => {
      const orphan = e.employee_id === null;
      const d = document.createElement('div');
      d.className = 'rank-item';
      d.title = Fastt.fmtInt(e.messages_sent) + ' messages · ' + Fastt.fmtInt(e.ppv_conversions) + ' PPV unlocks · ' +
        'PPV ' + Fastt.fmtCents((e.revenue_by_kind || {}).ppv || 0) + ' · Tips ' + Fastt.fmtCents((e.revenue_by_kind || {}).tip || 0);
      d.innerHTML = '<span class="rank-num"' + (orphan ? ' style="background:#e0b24a;color:#2a1d00"' : '') + '>' + (i + 1) + '</span>' +
        '<span class="rank-name"' + (orphan ? ' style="color:#e0b24a"' : '') + '>' +
        Fastt.esc(e.display_name || ('Employee #' + e.employee_id)) +
        (orphan ? '<span class="warnpill">unattributed</span>' : '') + '</span>' +
        '<span class="rank-val">' + Fastt.fmtCents(e.revenue_cents || 0) + '</span>';
      rcol.appendChild(d);
    });
    const tot = rows.reduce((a, r) => a + (r.revenue_cents || 0), 0);
    const foot = document.createElement('div');
    foot.className = 'rank-item';
    foot.style.borderTop = '1px solid #2a2a2a';
    foot.innerHTML = '<span class="rank-name" style="color:#8a8a8a">Total (' + rows.length + ' shown)</span>' +
      '<span class="rank-val" style="color:#fff">' + Fastt.fmtCents(tot) + '</span>';
    rcol.appendChild(foot);
    setBadge(rankHead, true);
  }
  if (rankSortEl) {
    rankSortEl.title = 'Flip the ranking order';
    rankSortEl.addEventListener('click', () => {
      rankDir = -rankDir;
      const tn = Array.prototype.filter.call(rankSortEl.childNodes, (n) => n.nodeType === 3 && n.textContent.trim());
      if (tn.length) tn[0].textContent = rankDir < 0 ? 'Descending' : 'Ascending';
      renderRanking();
    });
  }

  // ── Employee comparison: the three pickers, wired ──
  const cmpPick = [null, null, null];
  const cmpOut = document.getElementById('cmpOut');
  const cmpEmpty = document.getElementById('cmpEmpty');
  const CMP_ROWS = [
    { l: 'Total revenue', f: (e) => Fastt.fmtCents(e.revenue_cents || 0), hi: true },
    { l: 'PPV revenue', f: (e) => Fastt.fmtCents((e.revenue_by_kind || {}).ppv || 0) },
    { l: 'Tip revenue', f: (e) => Fastt.fmtCents((e.revenue_by_kind || {}).tip || 0) },
    { l: 'Messages sent', f: (e) => Fastt.fmtInt(e.messages_sent || 0) },
    { l: 'PPV unlocks', f: (e) => Fastt.fmtInt(e.ppv_conversions || 0) },
    { l: '$ / message', f: (e) => (e.messages_sent ? Fastt.fmtCents((e.revenue_cents || 0) / e.messages_sent) : '—') },
    { l: 'Creators worked', f: (e) => Fastt.fmtInt((e.per_account || []).length) },
  ];
  const rowKey = (r) => (r.employee_id === null ? '__orphan__' : String(r.employee_id));
  function renderCmp() {
    const chosen = cmpPick.map((k) => (winRows || []).find((r) => rowKey(r) === k)).filter(Boolean);
    if (!chosen.length) { cmpOut.style.display = 'none'; cmpEmpty.style.display = ''; return; }
    cmpEmpty.style.display = 'none';
    cmpOut.style.display = '';
    let h = '<div class="dtable"><table><thead><tr><th>Metric</th>';
    chosen.forEach((e) => {
      h += '<th class="r"' + (e.employee_id === null ? ' style="color:#e0b24a"' : '') + '>' +
        Fastt.esc(e.display_name || ('#' + e.employee_id)) + '</th>';
    });
    h += '</tr></thead><tbody>';
    CMP_ROWS.forEach((row) => {
      h += '<tr><td>' + Fastt.esc(row.l) + '</td>';
      chosen.forEach((e) => {
        h += '<td class="r"' + (row.hi ? ' style="color:#fff;font-weight:600"' : '') + '>' + Fastt.esc(row.f(e)) + '</td>';
      });
      h += '</tr>';
    });
    h += '</tbody></table></div>';
    cmpOut.innerHTML = h;
  }
  document.querySelectorAll('.selbox').forEach((box) => {
    box.style.cursor = 'pointer';
    box.title = 'Pick an employee to compare';
    box.addEventListener('click', (ev) => {
      ev.stopPropagation();
      if (!winRows) { Fastt.toast('Employee rollup has not loaded yet'); return; }
      const slot = Number(box.dataset.slot);
      const html = '<div class="rw pick" data-k=""><span style="color:#8a8a8a">— none —</span><span></span></div>' +
        winRows.map((r) => '<div class="rw pick" data-k="' + Fastt.esc(rowKey(r)) + '" style="color:' +
          (r.employee_id === null ? '#e0b24a' : '#cfcfcf') + '"><span>' +
          Fastt.esc(r.display_name || ('#' + r.employee_id)) + '</span><span>' +
          Fastt.fmtCents(r.revenue_cents || 0) + '</span></div>').join('');
      popAt(box, html, (e, close) => {
        const row = e.target.closest('[data-k]');
        if (!row) return;
        close();
        cmpPick[slot] = row.dataset.k || null;
        const r = (winRows || []).find((x) => rowKey(x) === cmpPick[slot]);
        const tn = Array.prototype.filter.call(box.childNodes, (n) => n.nodeType === 3 && n.textContent.trim());
        if (tn.length) tn[0].textContent = r ? (r.display_name || ('#' + r.employee_id)) : 'Select';
        box.style.color = r ? '#e6e6e6' : '';
        renderCmp();
      });
    });
  });

  // ── Employee performance tab ──
  const perfTable = document.getElementById('perfTable');
  const perfNote = document.getElementById('perfNote');
  const perfHideZero = document.getElementById('perfHideZero');
  const perfTotalsOnly = document.getElementById('perfTotalsOnly');
  const perfHidden = document.getElementById('perfHidden');
  const perfExpanded = {};
  const perfSort = { key: 'revenue_cents', dir: -1 };
  const PERF_COLS = [
    { k: 'display_name', l: 'Employee' },
    { k: 'messages_sent', l: 'Messages', num: true },
    { k: 'ppv_conversions', l: 'PPVs', num: true },
    { k: '_ppv', l: 'PPV Rev', num: true, money: true },
    { k: '_tip', l: 'Tip Rev', num: true, money: true },
    { k: 'revenue_cents', l: 'Total', num: true, money: true },
  ];
  const kindOf = (r, k) => (r.revenue_by_kind || {})[k] || 0;
  function renderPerf() {
    if (!winRows) return;
    perfHideZero.checked = opts.hideZero;
    let rows = (winRows || []).slice();
    if (!opts.includeOrphan) rows = rows.filter((r) => r.employee_id !== null);
    const before = rows.length;
    if (opts.hideZero) rows = rows.filter((r) => (r.revenue_cents || 0) > 0);
    perfHidden.textContent = (before - rows.length)
      ? (before - rows.length) + ' zero-revenue row' + (before - rows.length === 1 ? '' : 's') + ' hidden' : '';
    rows.forEach((r) => { r._ppv = kindOf(r, 'ppv'); r._tip = kindOf(r, 'tip'); });
    rows.sort((a, b) => {
      const av = a[perfSort.key], bv = b[perfSort.key];
      if (perfSort.key === 'display_name') return String(av || '').localeCompare(String(bv || '')) * perfSort.dir;
      return ((Number(av) || 0) - (Number(bv) || 0)) * perfSort.dir;
    });
    let h = '<table><thead><tr>';
    PERF_COLS.forEach((c) => {
      h += '<th data-k="' + c.k + '" class="' + (c.num ? 'r ' : '') + (perfSort.key === c.k ? 'on' : '') + '">' +
        Fastt.esc(c.l) + (perfSort.key === c.k ? (perfSort.dir < 0 ? ' ↓' : ' ↑') : '') + '</th>';
    });
    h += '</tr></thead><tbody>';
    if (!rows.length) {
      h += '<tr><td colspan="6" style="text-align:center;color:#8a8a8a;padding:34px">' +
        (before ? 'No employee earned in this window — untick “Hide $0 rows” to see all ' + before + '.'
                : 'No employee activity in this window.') + '</td></tr>';
    }
    rows.forEach((r) => {
      const key = rowKey(r);
      const orphan = r.employee_id === null;
      const sub = r.per_account || [];
      const expandable = !perfTotalsOnly.checked && sub.length > 0;
      const open = expandable && perfExpanded[key];
      h += '<tr data-k="' + Fastt.esc(key) + '" class="' + (orphan ? 'orphan' : '') + '"' +
        (expandable ? ' style="cursor:pointer"' : '') + '>';
      h += '<td>' + (expandable ? '<span style="color:#6a6a6a;display:inline-block;width:14px">' + (open ? '▾' : '▸') + '</span>' : '<span style="display:inline-block;width:14px"></span>') +
        '<span style="font-weight:600;color:' + (orphan ? '#e0b24a' : '#fff') + '">' +
        Fastt.esc(r.display_name || ('Employee #' + r.employee_id)) + '</span>' +
        (orphan ? '<span class="warnpill">unattributed</span>' : '') + '</td>';
      h += '<td class="r">' + Fastt.fmtInt(r.messages_sent || 0) + '</td>';
      h += '<td class="r">' + Fastt.fmtInt(r.ppv_conversions || 0) + '</td>';
      h += '<td class="r">' + (r._ppv ? Fastt.fmtCents(r._ppv) : '<span style="color:#6a6a6a">—</span>') + '</td>';
      h += '<td class="r' + (!orphan && r._tip > 0 ? ' okmoney' : '') + '">' + (r._tip ? Fastt.fmtCents(r._tip) : '<span style="color:#6a6a6a">—</span>') + '</td>';
      h += '<td class="r" style="font-weight:600;color:#fff">' + Fastt.fmtCents(r.revenue_cents || 0) + '</td></tr>';
      if (open) {
        sub.slice().sort((a, b) => (b.revenue_cents || 0) - (a.revenue_cents || 0)).forEach((s) => {
          h += '<tr class="sub"><td style="padding-left:36px">' +
            Fastt.esc(s.account_nickname || s.account_id || '—') + '</td>' +
            '<td class="r">' + Fastt.fmtInt(s.messages_sent || 0) + '</td>' +
            '<td class="r">' + Fastt.fmtInt(s.ppv_conversions || 0) + '</td>' +
            '<td class="r">' + Fastt.fmtCents((s.revenue_by_kind || {}).ppv || 0) + '</td>' +
            '<td class="r">' + Fastt.fmtCents((s.revenue_by_kind || {}).tip || 0) + '</td>' +
            '<td class="r">' + Fastt.fmtCents(s.revenue_cents || 0) + '</td></tr>';
        });
      }
    });
    h += '</tbody><tfoot><tr><td>Total (' + rows.length + ')</td>' +
      '<td class="r">' + Fastt.fmtInt(rows.reduce((a, r) => a + (r.messages_sent || 0), 0)) + '</td>' +
      '<td class="r">' + Fastt.fmtInt(rows.reduce((a, r) => a + (r.ppv_conversions || 0), 0)) + '</td>' +
      '<td class="r">' + Fastt.fmtCents(rows.reduce((a, r) => a + r._ppv, 0)) + '</td>' +
      '<td class="r">' + Fastt.fmtCents(rows.reduce((a, r) => a + r._tip, 0)) + '</td>' +
      '<td class="r">' + Fastt.fmtCents(rows.reduce((a, r) => a + (r.revenue_cents || 0), 0)) + '</td></tr></tfoot></table>';
    perfTable.innerHTML = h;
    perfTable.querySelectorAll('th[data-k]').forEach((th) => th.addEventListener('click', () => {
      const k = th.dataset.k;
      if (perfSort.key === k) perfSort.dir = -perfSort.dir;
      else { perfSort.key = k; perfSort.dir = k === 'display_name' ? 1 : -1; }
      renderPerf();
    }));
    perfTable.querySelectorAll('tbody tr[data-k]').forEach((tr) => tr.addEventListener('click', () => {
      perfExpanded[tr.dataset.k] = !perfExpanded[tr.dataset.k];
      renderPerf();
    }));
    perfNote.innerHTML = 'GET /admin/stats/per-employee?by_account=true · ' + Fastt.esc(longLbl(range.from)) + ' → ' +
      Fastt.esc(longLbl(range.to)) + ' · org-wide (employees chat across creators). Click a row to break it down per creator.' +
      '<br>“Unattributed” is orphan PPV/tip revenue with no sending employee — kept in so these totals still reconcile with Creator reports.';
    setBadge(perfSect, true);
  }
  perfHideZero.addEventListener('change', () => { opts.hideZero = perfHideZero.checked; renderPerf(); renderRanking(); });
  perfTotalsOnly.addEventListener('change', renderPerf);
  window.__loadPerf = () => {
    if (winRows) renderPerf();
    else perfTable.innerHTML = '<div style="padding:34px;text-align:center;color:#8a8a8a">Loading…</div>';
  };

  async function loadWindow() {
    rankNote('Loading…');
    try {
      const win = await Fastt.get('/admin/stats/per-employee', { from: range.from, to: range.to, by_account: true });
      winRows = (win.employees || []).slice();
      renderRanking();
      renderCmp();
      if (document.getElementById('tabPerf').style.display !== 'none') renderPerf();
    } catch (e) {
      winRows = null;
      rankNote('Sales ranking unavailable');
      setBadge(rankHead, false, 'NO LIVE DATA');
      perfTable.innerHTML = '<div style="padding:34px;text-align:center;color:#8a8a8a">Employee rollup unavailable — see the error toast.</div>';
      setBadge(perfSect, false, 'NO LIVE DATA');
      Fastt.oops(e);
    }
  }

  // ── Sales records tab: the real OF earning ledger ──
  const salesTable = document.getElementById('salesTable');
  const salesMore = document.getElementById('salesMore');
  const salesNote = document.getElementById('salesNote');
  const salesCount = document.getElementById('salesCount');
  const salesKinds = document.getElementById('salesKinds');
  const salesWindowOnly = document.getElementById('salesWindow');
  const KIND_LBL = { message: 'PPV / message', subscription: 'New subscription', subscription_prolong: 'Rebill', tips: 'Tip' };
  let salesRows = [], salesOffset = 0, salesDone = false, salesLoading = false;

  function renderSales() {
    const winStart = dayStart(range.from), winEnd = dayEnd(range.to);
    const rows = salesWindowOnly.checked
      ? salesRows.filter((x) => { const t = Fastt.parseUtc(x.createdAt); return t && t >= winStart && t <= winEnd; })
      : salesRows;
    let h = '<table><thead><tr><th>When</th><th>Buyer</th><th>Kind</th>' +
      '<th class="r">Gross</th><th class="r">VAT</th><th class="r">Fee</th><th class="r">Net</th><th>Status</th></tr></thead><tbody>';
    if (!rows.length) {
      h += '<tr><td colspan="8" style="text-align:center;color:#8a8a8a;padding:34px">' +
        (salesRows.length
          ? 'No OF transactions inside ' + Fastt.esc(longLbl(range.from)) + ' → ' + Fastt.esc(longLbl(range.to)) +
            ' — untick “Only the selected window”, or load more.'
          : 'No transactions returned by OnlyFans.') + '</td></tr>';
    }
    rows.forEach((x) => {
      const u = x.user || {};
      const kind = ((x.descriptionDetails || {}).type) || 'other';
      const cleared = x.status === 'done';
      h += '<tr>' +
        '<td>' + Fastt.esc(Fastt.fmtDate(x.createdAt)) + '</td>' +
        '<td><span style="color:#fff">' + Fastt.esc(u.name || u.username || '—') + '</span>' +
          (u.username ? '<br><span style="color:#6a6a6a;font-size:11px">@' + Fastt.esc(u.username) + '</span>' : '') + '</td>' +
        '<td>' + Fastt.esc(KIND_LBL[kind] || kind) + '</td>' +
        '<td class="r">' + Fastt.fmtMoney(x.amount) + '</td>' +
        '<td class="r" style="color:#8a8a8a">' + Fastt.fmtMoney(x.vatAmount) + '</td>' +
        '<td class="r" style="color:#8a8a8a">−' + Fastt.fmtMoney(x.fee) + '</td>' +
        '<td class="r" style="color:#fff;font-weight:600">' + Fastt.fmtMoney(x.net) + '</td>' +
        '<td><span style="color:' + (cleared ? '#67d1ae' : '#e0b24a') + '">' +
          (cleared ? 'cleared' : 'pending') + '</span>' +
          (x.payoutPendingDays && !cleared ? '<span style="color:#6a6a6a"> · ' + x.payoutPendingDays + 'd</span>' : '') + '</td></tr>';
    });
    const sum = (f) => rows.reduce((a, x) => a + (Number(x[f]) || 0), 0);
    h += '</tbody><tfoot><tr><td colspan="3">Total (' + rows.length + ')</td>' +
      '<td class="r">' + Fastt.fmtMoney(sum('amount')) + '</td>' +
      '<td class="r">' + Fastt.fmtMoney(sum('vatAmount')) + '</td>' +
      '<td class="r">−' + Fastt.fmtMoney(sum('fee')) + '</td>' +
      '<td class="r">' + Fastt.fmtMoney(sum('net')) + '</td><td></td></tr></tfoot></table>';
    salesTable.innerHTML = h;
    salesCount.textContent = rows.length + ' shown of ' + salesRows.length + ' loaded';
    const kinds = {};
    rows.forEach((x) => { const k = ((x.descriptionDetails || {}).type) || 'other'; kinds[k] = (kinds[k] || 0) + 1; });
    salesKinds.textContent = Object.keys(kinds).map((k) => (KIND_LBL[k] || k) + ' ' + kinds[k]).join(' · ');
    salesMore.style.display = salesDone ? 'none' : '';
  }

  async function loadSalesPage() {
    if (salesLoading || salesDone) return;
    salesLoading = true;
    salesMore.textContent = 'Loading…';
    try {
      const r = await Fastt.get('/api/of/v2/payouts/transactions', { limit: 50, offset: salesOffset });
      const list = (r && r.list) || [];
      salesRows = salesRows.concat(list);
      salesOffset += list.length;
      if (!list.length || !r.hasMore) salesDone = true;
      renderSales();
      setBadge(recSect, true);
    } catch (e) {
      salesDone = true;
      salesTable.innerHTML = '<div style="padding:34px;text-align:center;color:#8a8a8a">' +
        'No OF session for this creator — the payout ledger is unavailable.</div>';
      setBadge(recSect, false, 'NO LIVE DATA');
      salesMore.style.display = 'none';
    } finally {
      salesLoading = false;
      salesMore.textContent = 'Load 50 more';
    }
  }
  salesMore.addEventListener('click', () => loadSalesPage());
  salesWindowOnly.addEventListener('change', renderSales);
  salesNote.innerHTML =
    'GET /api/of/v2/payouts/transactions — OnlyFans’ own earning ledger for the creator selected in the sidebar, ' +
    'newest first, 50 rows per page. OF <b>ignores</b> start/end on this route, so the window filter is applied here on <span style="font-family:ui-monospace,monospace">createdAt</span>. ' +
    'Status <span style="font-family:ui-monospace,monospace">done</span> = cleared, <span style="font-family:ui-monospace,monospace">loading</span> = still in the payout hold.';
  window.__loadSales = () => {
    if (!salesRows.length && !salesDone) {
      salesTable.innerHTML = '<div style="padding:34px;text-align:center;color:#8a8a8a">Loading the OF payout ledger…</div>';
      if (!Fastt.account()) {
        salesTable.innerHTML = '<div style="padding:34px;text-align:center;color:#8a8a8a">No creator selected — the OF payout ledger is per creator.</div>';
        setBadge(recSect, false, 'NO LIVE DATA');
        salesDone = true;
        salesMore.style.display = 'none';
        return;
      }
      loadSalesPage();
    }
  };

  // ── Automation performance tab: which automations earn vs. cost LLM spend ──
  // GET /admin/stats/per-automation — scoped to the creator selected in the sidebar
  // (the LLM spend is per creator, unlike per-employee which is org-wide). One row
  // per automation_kind: mass broadcasters show $0 spend; the profiler shows spend
  // with zero sends. `cost_cents` is a float in cents; `revenue_cents` an int in cents.
  const autoTable = document.getElementById('autoTable');
  const autoNote = document.getElementById('autoNote');
  const autoSummary = document.getElementById('autoSummary');
  const autoCount = document.getElementById('autoCount');
  const autoHideIdle = document.getElementById('autoHideIdle');
  const AUTO_LBL = {
    ppv_send: 'PPV send', ai_upseller: 'AI Upseller', ai_chatter: 'AI Chatter',
    autoreply: 'Auto-reply', nudge_online: 'Online nudge', online_blast: 'Online blast',
    mass_nudge: 'Mass nudge', image_reply: 'Image reply', followup: 'Follow-up',
    gen_info: 'Profiler', welcome: 'Welcome',
    welcome_chatter_for_info: 'Info-gather',
    of_ai_chat: 'Info-gather',        // renamed 2026-08-19 — legacy rows still render
    deep_convo: 'Deep Convo', reply_mass_funnel: 'Funnel reply', send_mass_message: 'Mass message',
  };
  const autoLabel = (k) => AUTO_LBL[k] || k;
  const fmtTokens = (n) => { n = Number(n) || 0; return n >= 1e6 ? (n / 1e6).toFixed(1) + 'M' : n >= 1e3 ? (n / 1e3).toFixed(1) + 'k' : String(n); };
  let autoRows = null, autoTotals = null, autoLoaded = false, autoLoading = false;
  const autoSort = { key: 'revenue_cents', dir: -1 };
  const AUTO_COLS = [
    { k: 'automation', l: 'Automation' },
    { k: 'messages_sent', l: 'Messages', num: true },
    { k: 'revenue_cents', l: 'Earnings', num: true },
    { k: 'llm_calls', l: 'LLM calls', num: true },
    { k: 'tokens', l: 'Tokens in/out', num: true },
    { k: 'cost_cents', l: 'Spend', num: true },
  ];
  function renderAuto() {
    if (!autoRows) return;
    let rows = autoRows.slice();
    const before = rows.length;
    if (autoHideIdle.checked) rows = rows.filter((r) => (r.messages_sent || 0) > 0 || (r.revenue_cents || 0) > 0 || (r.cost_cents || 0) > 0);
    const hidden = before - rows.length;
    const val = (r, k) => k === 'automation' ? autoLabel(r.automation)
      : k === 'tokens' ? ((r.tokens_in || 0) + (r.tokens_out || 0)) : (Number(r[k]) || 0);
    rows.sort((a, b) => {
      const av = val(a, autoSort.key), bv = val(b, autoSort.key);
      if (autoSort.key === 'automation') return String(av).localeCompare(String(bv)) * autoSort.dir;
      return (av - bv) * autoSort.dir;
    });
    const maxRev = Math.max.apply(null, rows.map((r) => r.revenue_cents || 0).concat([1]));
    const maxSpend = Math.max.apply(null, rows.map((r) => r.cost_cents || 0).concat([1]));
    let h = '<table><thead><tr>';
    AUTO_COLS.forEach((c) => {
      h += '<th data-k="' + c.k + '" class="' + (c.num ? 'r ' : '') + (autoSort.key === c.k ? 'on' : '') + '">' +
        Fastt.esc(c.l) + (autoSort.key === c.k ? (autoSort.dir < 0 ? ' ↓' : ' ↑') : '') + '</th>';
    });
    h += '</tr></thead><tbody>';
    if (!rows.length) {
      h += '<tr><td colspan="6" style="text-align:center;color:#8a8a8a;padding:34px">' +
        (before ? 'Every automation was idle in this window — untick “Hide idle” to see all ' + before + '.'
                : 'No automation activity in this window.') + '</td></tr>';
    }
    rows.forEach((r) => {
      const rev = r.revenue_cents || 0, spend = r.cost_cents || 0, calls = r.llm_calls || 0;
      h += '<tr title="' + Fastt.esc(autoLabel(r.automation) +
        (calls ? ' · ' + Fastt.fmtInt(calls) + ' LLM calls · ' + Fastt.fmtInt(r.tokens_in) + ' in / ' + Fastt.fmtInt(r.tokens_out) + ' out tokens' : ' · no LLM calls (send-only)')) + '">';
      h += '<td><span style="font-weight:600;color:#fff">' + Fastt.esc(autoLabel(r.automation)) + '</span></td>';
      h += '<td class="r">' + Fastt.fmtInt(r.messages_sent || 0) + '</td>';
      h += '<td class="r">' + (rev ? '<span style="color:#67d1ae;font-weight:600">' + Fastt.fmtCents(rev) + '</span><span class="cellbar" style="width:' + Math.round(rev / maxRev * 46) + 'px"></span>' : '<span style="color:#6a6a6a">—</span>') + '</td>';
      h += '<td class="r" style="color:#9a9a9a">' + (calls ? Fastt.fmtInt(calls) : '<span style="color:#6a6a6a">—</span>') + '</td>';
      h += '<td class="r" style="color:#9a9a9a">' + (calls ? fmtTokens(r.tokens_in) + ' / ' + fmtTokens(r.tokens_out) : '<span style="color:#6a6a6a">—</span>') + '</td>';
      h += '<td class="r">' + (spend ? '<span style="color:#e0b24a">' + Fastt.fmtCents(spend) + '</span><span class="cellbar spend" style="width:' + Math.round(spend / maxSpend * 46) + 'px"></span>' : '<span style="color:#6a6a6a">—</span>') + '</td></tr>';
    });
    const t = autoTotals || {};
    h += '</tbody><tfoot><tr><td>Total (' + rows.length + ')</td>' +
      '<td class="r">' + Fastt.fmtInt(rows.reduce((a, r) => a + (r.messages_sent || 0), 0)) + '</td>' +
      '<td class="r">' + Fastt.fmtCents(rows.reduce((a, r) => a + (r.revenue_cents || 0), 0)) + '</td>' +
      '<td class="r">' + Fastt.fmtInt(rows.reduce((a, r) => a + (r.llm_calls || 0), 0)) + '</td>' +
      '<td class="r" style="color:#8a8a8a">—</td>' +
      '<td class="r">' + Fastt.fmtCents(rows.reduce((a, r) => a + (r.cost_cents || 0), 0)) + '</td></tr></tfoot></table>';
    autoTable.innerHTML = h;
    autoTable.querySelectorAll('th[data-k]').forEach((th) => th.addEventListener('click', () => {
      const k = th.dataset.k;
      if (autoSort.key === k) autoSort.dir = -autoSort.dir;
      else { autoSort.key = k; autoSort.dir = k === 'automation' ? 1 : -1; }
      renderAuto();
    }));
    autoCount.textContent = hidden ? hidden + ' idle automation' + (hidden === 1 ? '' : 's') + ' hidden' : '';

    // friendly summary strip — earned vs. spent, net, and $ earned per $1 of AI spend
    const earned = Number(t.revenue_cents) || 0;
    const spent = Number(t.cost_cents) || 0;
    const net = earned - spent;
    const roi = spent > 0 ? (earned / spent) : null;
    autoSummary.innerHTML =
      '<div class="kpi good"><div class="lab">Earnings attributed</div><div class="val">' + Fastt.fmtCents(earned) + '</div><div class="sub">across ' + Fastt.fmtInt(t.messages_sent || 0) + ' messages sent</div></div>' +
      '<div class="kpi spend"><div class="lab">AI spend</div><div class="val">' + Fastt.fmtCents(spent) + '</div><div class="sub">' + Fastt.fmtInt(t.llm_calls || 0) + ' LLM calls</div></div>' +
      '<div class="kpi"><div class="lab">Net after AI cost</div><div class="val" style="color:' + (net >= 0 ? '#67d1ae' : '#ec4b9b') + '">' + Fastt.fmtCents(net) + '</div><div class="sub">earnings minus spend</div></div>' +
      '<div class="kpi roi"><div class="lab">Return on AI spend</div><div class="val">' + (roi === null ? 'n/a' : (roi >= 100 ? '>100' : roi.toFixed(0)) + '×') + '</div><div class="sub">' + (roi === null ? 'no AI spend yet' : 'earned per $1 spent') + '</div></div>';

    autoNote.innerHTML = 'GET /admin/stats/per-automation · ' + Fastt.esc(longLbl(range.from)) + ' → ' + Fastt.esc(longLbl(range.to)) +
      ' · scoped to <b>' + Fastt.esc((Fastt.accountRow() && Fastt.accountRow().nickname) || ('#' + Fastt.account())) + '</b> — AI spend is per creator. ' +
      'Send-only automations (PPV send, blasts) show no spend; the profiler shows spend with no sends. Sort by any column.';
    setBadge(autoSect, true);
  }
  autoHideIdle.addEventListener('change', renderAuto);
  async function loadAuto() {
    if (autoLoading) return;
    autoLoading = true;
    if (!autoRows) autoTable.innerHTML = '<div style="padding:34px;text-align:center;color:#8a8a8a">Loading automation performance…</div>';
    try {
      if (!Fastt.account()) {
        autoTable.innerHTML = '<div style="padding:34px;text-align:center;color:#8a8a8a">No creator selected — automation spend is per creator.</div>';
        setBadge(autoSect, false, 'NO LIVE DATA'); autoLoaded = true; return;
      }
      const r = await Fastt.get('/admin/stats/per-automation', { from: range.from, to: range.to });
      autoRows = (r.rows || []).slice();
      autoTotals = r.totals || null;
      autoLoaded = true;
      renderAuto();
    } catch (e) {
      autoRows = null; autoTotals = null; autoLoaded = false;
      autoTable.innerHTML = '<div style="padding:34px;text-align:center;color:#8a8a8a">Automation performance unavailable — see the error toast.</div>';
      autoSummary.innerHTML = '';
      setBadge(autoSect, false, 'NO LIVE DATA');
      Fastt.oops(e);
    } finally { autoLoading = false; }
  }
  window.__loadAuto = () => { if (!autoLoaded) loadAuto(); };

  // ── orchestration ──
  async function reloadAll() {
    await loadWindow();
    await loadChart();
    if (salesRows.length) renderSales();
    autoLoaded = false; autoRows = null;
    if (document.getElementById('tabAuto').style.display !== 'none') loadAuto();
  }
  function applyRange(f, t) {
    range.from = f; range.to = t;
    paintRange();
    note('Loading employee sales…');
    reloadAll().catch(Fastt.oops);
  }

  // refresh icon re-runs every read (no writes)
  const refreshBox = document.querySelector('.utab-right .iconbox');
  if (refreshBox) {
    refreshBox.title = 'Reload employee sales, ranking and comparison';
    refreshBox.addEventListener('click', () => {
      note('Loading employee sales…');
      reloadAll().catch(Fastt.oops);
    });
  }

  // the "View sales settings" pill in the info banner has no relay route
  const salesBtn = document.querySelector('.btn-sales');
  if (salesBtn) {
    salesBtn.style.display = 'inline-flex';
    salesBtn.style.alignItems = 'center';
    salesBtn.style.gap = '9px';
    Fastt.staticBadge(salesBtn, 'NO BACKEND');
    salesBtn.title = 'There is no organisation sales-settings endpoint on the relay';
  }

  await reloadAll();
});
