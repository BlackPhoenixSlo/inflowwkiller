// ==== LIVE WIRING (fastt relay) ====
// Reads
//   /admin/stats/per-model                        → creator column (all creators)
//   /api/of/v2/subscriptions/subscribers/chart    → New-fans per-day series (subscribes[])
//   /api/of/v2/users/me/stats/overview            → New / Renew window totals + deltas
//   /api/of/v2/subscriptions/count/all            → live fan + subscription counts
//   /admin/stats/per-fan-data                     → the Fan-data table (gen_info profiles)
// The date field owns from/to; "Shown by" buckets the plotted series.
// Honest empty states: there is NO per-day renew series and NO total-fans history on OF —
//   those render as window totals / live point-in-time counts, never as an invented line.
Fastt.ready(async () => {
  const DAY = 86400000;
  const isoDay = (d) => d.toISOString().slice(0, 10);
  const dayStart = (s) => new Date(s + 'T00:00:00Z');
  const shortLbl = (s) => {
    const d = Fastt.parseUtc(s);
    return d ? d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', timeZone: 'UTC' }) : '—';
  };
  const longLbl = (s) => dayStart(s).toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric', timeZone: 'UTC' });

  const now = new Date();
  const range = { from: isoDay(new Date(now.getTime() - 6 * DAY)), to: isoDay(now) };
  let grain = 'day';

  const hrFields = Array.prototype.slice.call(document.querySelectorAll('.hr .field'));
  const dateField = hrFields[0], grainField = hrFields[1], netField = hrFields[2];

  function paintRange() {
    if (!dateField) return;
    const tn = Array.prototype.filter.call(dateField.childNodes,
      (n) => n.nodeType === 3 && n.textContent.trim());
    if (tn.length >= 2) { tn[0].textContent = ' ' + longLbl(range.from) + ' '; tn[1].textContent = ' ' + longLbl(range.to) + ' '; }
  }
  function paintGrain() {
    if (!grainField) return;
    const tn = Array.prototype.filter.call(grainField.childNodes, (n) => n.nodeType === 3 && n.textContent.trim());
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
    const ph = pop.getBoundingClientRect().height;
    if (r.bottom + 6 + ph > window.innerHeight - 8) pop.style.top = Math.max(8, r.top - ph - 6) + 'px';
    const close = () => { pop.remove(); document.removeEventListener('click', close); };
    setTimeout(() => document.addEventListener('click', close), 0);
    pop.addEventListener('click', (e) => { e.stopPropagation(); if (onPick) onPick(e, close, pop); });
    return pop;
  }

  if (dateField) {
    dateField.title = 'Change the reporting window — both fan charts re-query';
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
            range.from = isoDay(new Date(to.getTime() - (Number(b.dataset.d) - 1) * DAY));
            range.to = isoDay(to);
            paintRange(); loadFanCharts().catch(Fastt.oops);
            return;
          }
          if (e.target.closest('.go')) {
            const f = pop.querySelector('input[data-k="f"]').value;
            const t = pop.querySelector('input[data-k="t"]').value;
            if (!f || !t) { Fastt.toast('Pick both dates'); return; }
            if (f > t) { Fastt.toast('“From” must be on or before “To”'); return; }
            close(); range.from = f; range.to = t; paintRange(); loadFanCharts().catch(Fastt.oops);
          }
        });
    });
    Fastt.liveBadge(dateField);
  }
  if (grainField) {
    grainField.title = 'Bucket size for the New-fans series (client-side fold of OF’s daily buckets)';
    grainField.addEventListener('click', (ev) => {
      ev.stopPropagation();
      popAt(grainField, ['day', 'week', 'month'].map((g) =>
        '<div class="rw pick" data-g="' + g + '" style="color:' + (g === grain ? '#fff' : '#cfcfcf') + '"><span>Shown by ' + g +
        '</span><span>' + (g === grain ? '●' : '') + '</span></div>').join(''),
        (e, close) => {
          const row = e.target.closest('[data-g]');
          if (!row) return;
          close(); grain = row.dataset.g; paintGrain(); renderNewFans();
        });
    });
  }
  if (netField) {
    Fastt.staticBadge(netField, 'NO BACKEND');
    netField.title = 'OF’s subscriber chart is a head-count, not money — there is no gross/net split to switch between here. ' +
      'Gross vs net lives on Creator reports, which reads the payout ledger.';
  }

  const titles = Array.prototype.slice.call(document.querySelectorAll('.chart-title'));
  // order in DOM: 0 Fan value & LTV · 1 New fans · 2 Total fans · 3 Fan cohorts · 4 Spend distribution · 5 Fan data
  const valTitle = titles[0], newTitle = titles[1], totalTitle = titles[2],
        cohTitle = titles[3], spendTitle = titles[4], fanTitle = titles[5];
  const setBadge = (el, live, txt) => {
    if (!el) return;
    el.querySelectorAll(':scope > .ft-live, :scope > .ft-static').forEach((n) => n.remove());
    if (live) Fastt.liveBadge(el); else Fastt.staticBadge(el, txt);
  };
  const kbox = (label, value, sub, cls) =>
    '<div class="kbox"><div class="kl">' + Fastt.esc(label) + '</div><div class="kv">' + value + '</div>' +
    (sub ? '<div class="kd ' + (cls || '') + '">' + sub + '</div>' : '') + '</div>';
  const deltaTxt = (d) => (d === null || d === undefined || !isFinite(d)) ? ''
    : (d > 0 ? '▲ ' : d < 0 ? '▼ ' : '') + Math.abs(d).toFixed(1) + '% vs prev.';
  const deltaCls = (d) => (d > 0 ? 'up' : d < 0 ? 'dn' : '');

  // ── creator column ──
  const col = document.querySelector('.creatorcol');
  const tpl = col.querySelector('.creator-card');
  const allCre = document.querySelector('.allcreators');
  const searchBox = document.querySelector('.searchbox');
  const refreshBtn = document.querySelector('.refreshbtn');
  let creatorCards = [];
  const creatorFail = (msg) => {
    if (tpl && tpl.parentNode) tpl.remove();
    col.querySelectorAll('.ft-crefail').forEach((n) => n.remove());
    const d = document.createElement('div');
    d.className = 'ft-crefail';
    d.style.cssText = 'padding:12px 6px;color:#8a8a8a;font-size:13.5px';
    d.textContent = msg;
    col.appendChild(d);
    setBadge(allCre, false, 'NO LIVE DATA');
  };
  async function loadCreators() {
    try {
      // noAccount: list EVERY account (the client would otherwise scope /admin/* to the current one)
      const pm = await Fastt.get('/admin/stats/per-model', null, { noAccount: true });
      const rows = pm.per_model || [];
      if (!rows.length || !tpl) { creatorFail('No creators returned — sign in to list accounts'); return; }
      const frag = document.createDocumentFragment();
      creatorCards = [];
      rows.forEach((r) => {
        const card = tpl.cloneNode(true);
        const active = String(r.account_id) === String(Fastt.account());
        if (!active) card.style.cssText = 'background:#1b1b1d;border-color:#2c2c2c';
        const nm = r.display_name || String(r.account_id);
        card.querySelector('.cc-name').textContent = nm;
        // avatar: initials fallback (no image URL from per-model) on a name-seeded tint
        const av = card.querySelector('.cc-av');
        const ini = av && av.querySelector('.ini');
        if (ini) {
          const parts = nm.trim().split(/\s+/).filter(Boolean);
          const letters = parts.length > 1
            ? (parts[0][0] + parts[parts.length - 1][0])
            : nm.replace(/[^A-Za-z0-9]/g, '').slice(0, 2);
          ini.textContent = (letters || nm.slice(0, 1) || '?');
          let hseed = 0;
          for (let i = 0; i < nm.length; i++) hseed = (hseed * 31 + nm.charCodeAt(i)) & 0xffffff;
          const hue = hseed % 360;
          av.style.background = 'linear-gradient(150deg,hsl(' + hue + ',42%,42%),hsl(' + ((hue + 34) % 360) + ',46%,26%))';
        }
        const bell = card.querySelector('.bell');
        const bellSvg = bell.querySelector('svg');
        bell.textContent = '';
        if (bellSvg) bell.appendChild(bellSvg);
        bell.appendChild(document.createTextNode(Fastt.fmtInt(r.new_subs_count || 0)));
        bell.title = 'New paid subs (last 30d)';
        const mb = card.querySelector('.msgbadge');
        const mbSvg = mb.querySelector('svg');
        mb.textContent = '';
        if (mbSvg) mb.appendChild(mbSvg);
        mb.appendChild(document.createTextNode(Fastt.fmtInt(r.messages_sent || 0)));
        mb.title = 'Messages sent (last 30d)';
        card.title = 'Revenue (30d): ' + Fastt.fmtCents(r.total_revenue_cents || 0) +
          ' · active subs ' + Fastt.fmtInt(r.active_subs_count || 0);
        card.dataset.name = (r.display_name || String(r.account_id)).toLowerCase();
        card.style.marginBottom = '8px';
        if (!active) card.addEventListener('click', () => Fastt.setAccount(r.account_id));
        creatorCards.push(card);
        frag.appendChild(card);
      });
      tpl.remove();
      col.appendChild(frag);
      setBadge(allCre, true);
    } catch (e) {
      creatorFail('Creator list unavailable');
      Fastt.oops(e);
    }
  }
  // the search box filters the creator list it sits in
  if (searchBox) {
    searchBox.innerHTML = '';
    const inp = document.createElement('input');
    inp.type = 'search'; inp.placeholder = 'Search creators';
    inp.style.cssText = 'flex:1;min-width:0;background:transparent;border:0;outline:none;color:#e6e6e6;font:14px Inter,sans-serif';
    searchBox.appendChild(inp);
    const mg = document.createElement('span');
    mg.className = 'mg';
    mg.innerHTML = '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="11" cy="11" r="7"/><path d="M20 20l-3.5-3.5" stroke-linecap="round"/></svg>';
    searchBox.appendChild(mg);
    inp.addEventListener('input', () => {
      const q = inp.value.trim().toLowerCase();
      creatorCards.forEach((c) => { c.style.display = (!q || (c.dataset.name || '').indexOf(q) >= 0) ? '' : 'none'; });
    });
  }
  if (refreshBtn) {
    refreshBtn.title = 'Reload the creator list and both fan charts';
    refreshBtn.addEventListener('click', () => {
      col.querySelectorAll('.creator-card, .ft-crefail').forEach((n) => n.remove());
      const again = document.createElement('div');
      again.className = 'ft-crefail';
      again.style.cssText = 'padding:12px 6px;color:#8a8a8a;font-size:13.5px';
      again.textContent = 'Reloading…';
      col.appendChild(again);
      loadCreators().then(() => { col.querySelectorAll('.ft-crefail').forEach((n) => n.remove()); }).catch(Fastt.oops);
      loadFanCharts().catch(Fastt.oops);
      loadFans().then(renderCohorts).catch(Fastt.oops);
    });
  }

  // ── New-fans chart ──
  const svg = document.querySelectorAll('.fancharts svg.chart')[0];
  const totalSvg = document.querySelectorAll('.fancharts svg.chart')[1];
  const legendBs = document.querySelectorAll('.legend b');
  const newStrip = document.getElementById('newStrip');
  const newNote = document.getElementById('newNote');
  let subSeries = null;                       // [{date, count}] straight from OF

  function drawNewFans(pts) {
    const L = 64, R = 968, T = 24, B = 250;
    let maxV = Math.max.apply(null, pts.map((p) => p.v).concat([0]));
    maxV = maxV <= 0 ? 2 : Math.max(2, Math.ceil(maxV / 2) * 2);
    const X = (i) => pts.length === 1 ? (L + R) / 2 : L + i * (R - L) / (pts.length - 1);
    const Y = (v) => B - (v / maxV) * (B - T);
    const fr = [1, 0.75, 0.5, 0.25, 0];
    const labY = [29, 85, 142, 198, 255];
    const numLbl = (v) => (Math.round(v * 10) / 10).toString();
    let h = '<g font-family="Inter, sans-serif" font-size="14" fill="#6a6a6a" text-anchor="end">';
    fr.forEach((f, i) => { h += '<text x="40" y="' + labY[i] + '">' + numLbl(maxV * f) + '</text>'; });
    h += '</g><g stroke="#2a2a2a" stroke-dasharray="3 5">';
    [24, 80.5, 137, 193.5].forEach((y) => { h += '<line x1="64" y1="' + y + '" x2="968" y2="' + y + '"/>'; });
    h += '</g><line x1="64" y1="250" x2="968" y2="250" stroke="#3a3a3a"/>';
    const step = Math.max(1, Math.ceil(pts.length / 8));
    h += '<g font-family="Inter, sans-serif" font-size="14" fill="#8a8a8a">';
    pts.forEach((p, i) => {
      if (i % step && i !== pts.length - 1) return;
      const anchor = i === 0 ? 'start' : (i === pts.length - 1 ? 'end' : 'middle');
      h += '<text x="' + X(i).toFixed(1) + '" y="280" text-anchor="' + anchor + '">' + Fastt.esc(p.label) + '</text>';
    });
    h += '</g>';
    h += '<polyline points="' + pts.map((p, i) => X(i).toFixed(1) + ',' + Y(p.v).toFixed(1)).join(' ') +
      '" fill="none" stroke="#f4776e" stroke-width="2.5" stroke-linejoin="round"/>';
    h += '<g fill="#f4776e">' + pts.map((p, i) =>
      '<circle cx="' + X(i).toFixed(1) + '" cy="' + Y(p.v).toFixed(1) + '" r="4.5"><title>' +
      Fastt.esc(p.label + ' · ' + p.v + ' new') + '</title></circle>').join('') + '</g>';
    svg.innerHTML = h;
  }
  function noSeries(msg) {
    svg.innerHTML =
      '<g stroke="#2a2a2a" stroke-dasharray="3 5">' +
      [24, 80.5, 137, 193.5].map((y) => '<line x1="64" y1="' + y + '" x2="968" y2="' + y + '"/>').join('') +
      '</g><line x1="64" y1="250" x2="968" y2="250" stroke="#3a3a3a"/>' +
      '<text x="500" y="140" text-anchor="middle" fill="#8a8a8a" font-family="Inter, sans-serif" font-size="14">' +
      Fastt.esc(msg) + '</text>';
    setBadge(newTitle, false, 'NO LIVE DATA');
  }
  /** Fold OF's daily buckets into the chosen grain. */
  function foldSeries(rows) {
    if (grain === 'day') {
      return rows.map((x) => ({ label: shortLbl(x.date), v: Number(x.count) || 0 }));
    }
    const out = [];
    if (grain === 'week') {
      for (let i = 0; i < rows.length; i += 7) {
        const chunk = rows.slice(i, i + 7);
        out.push({ label: shortLbl(chunk[0].date), v: chunk.reduce((a, x) => a + (Number(x.count) || 0), 0) });
      }
      return out;
    }
    const byMonth = {};
    rows.forEach((x) => {
      const d = Fastt.parseUtc(x.date);
      if (!d) return;
      const k = d.toISOString().slice(0, 7);
      byMonth[k] = (byMonth[k] || 0) + (Number(x.count) || 0);
    });
    Object.keys(byMonth).sort().forEach((k) => {
      out.push({
        label: new Date(k + '-01T00:00:00Z').toLocaleDateString('en-US', { month: 'short', year: '2-digit', timeZone: 'UTC' }),
        v: byMonth[k],
      });
    });
    return out;
  }
  function renderNewFans() {
    if (!subSeries || !subSeries.length) return;
    let pts = foldSeries(subSeries);
    // A window shorter than one bucket collapses to a single dot, which is not a
    // trend — fall back to the finest grain and say so rather than drawing it.
    if (pts.length < 2 && grain !== 'day') {
      Fastt.toast('This window is shorter than one ' + grain + ' — showing by day');
      grain = 'day'; paintGrain();
      pts = foldSeries(subSeries);
    }
    drawNewFans(pts);
    setBadge(newTitle, true);
  }

  // ── Total-fans panel (live counts; OF has no per-day history) ──
  const totalStrip = document.getElementById('totalStrip');
  const totalNote = document.getElementById('totalNote');
  if (totalSvg) totalSvg.style.display = 'none';   // the baked flat line is mockup, not data

  // ── Fan value & LTV strip (per-model, window-scoped to the date field) ──
  const valStrip = document.getElementById('valStrip');
  const valNote = document.getElementById('valNote');
  async function loadValue() {
    if (!Fastt.account()) {
      valStrip.innerHTML = '';
      valNote.textContent = 'Pick a creator to load its revenue, LTV and ARPU for the window.';
      setBadge(valTitle, false, 'NO LIVE DATA');
      return;
    }
    valStrip.innerHTML = '<div class="kbox"><div class="kl">Loading window value…</div><div class="kv">—</div></div>';
    try {
      // per-model is scoped by start/end — same window the date field drives.
      const pm = await Fastt.get('/admin/stats/per-model', { start: range.from, end: range.to });
      const row = (pm.per_model || []).find((r) => String(r.account_id) === String(Fastt.account()));
      if (!row) {
        valStrip.innerHTML = '';
        valNote.textContent = 'No revenue rows for this creator in the window.';
        setBadge(valTitle, false, 'NO WINDOW DATA');
        return;
      }
      const rev = row.total_revenue_cents || 0;
      const pend = row.pending_revenue_cents || 0;
      const ltv = row.ltv_cents, arpu = row.arpu_cents;
      valStrip.innerHTML =
        kbox('Revenue (window)', Fastt.fmtCents(rev),
          pend > 0 ? 'incl. ' + Fastt.fmtCents(pend) + ' pending' : 'cleared', pend > 0 ? '' : 'up') +
        kbox('LTV', ltv == null ? '—' : Fastt.fmtCents(ltv),
          'revenue ÷ ' + Fastt.fmtInt(row.new_subs_count || 0) + ' new subs') +
        kbox('ARPU', arpu == null ? '—' : Fastt.fmtCents(arpu),
          'revenue ÷ ' + Fastt.fmtInt(row.active_subs_count || 0) + ' active subs') +
        kbox('New subs', Fastt.fmtInt(row.new_subs_count || 0), 'new + renewed, window') +
        kbox('PPVs sold', Fastt.fmtInt(row.ppv_conversions || 0), 'unlocks attributed, window') +
        kbox('Messages sent', Fastt.fmtInt(row.messages_sent || 0), 'outbound, window');
      valNote.innerHTML =
        'From <span style="font-family:ui-monospace,monospace">/admin/stats/per-model</span>, scoped to the date window above. ' +
        '<b>LTV</b> = window revenue ÷ new subscriptions; <b>ARPU</b> = window revenue ÷ current active subs. ' +
        'These are the money lens on the fan base; the cohorts and distribution below are the population lens (lifetime spend per fan).';
      setBadge(valTitle, true);
    } catch (e) {
      valStrip.innerHTML = '';
      valNote.textContent = 'Window value unavailable — see the error toast.';
      setBadge(valTitle, false, 'NO LIVE DATA');
      Fastt.oops(e);
    }
  }

  async function loadFanCharts() {
    loadValue().catch(Fastt.oops);
    if (!Fastt.account()) {
      noSeries('No creator selected — new-fan series unavailable');
      newStrip.innerHTML = '';
      newNote.textContent = 'Pick a creator in the sidebar to load OF’s subscriber stats.';
      totalStrip.innerHTML = '';
      totalNote.textContent = '';
      setBadge(totalTitle, false, 'NO LIVE DATA');
      return;
    }
    // ---- New fans ----
    try {
      const r = await Fastt.get('/api/of/v2/subscriptions/subscribers/chart', { start: range.from, end: range.to });
      subSeries = (r && r.subscribes) || [];
      if (subSeries.length) renderNewFans();
      else { subSeries = null; noSeries('OF returned no new-fan buckets for this window'); }
    } catch (e) {
      subSeries = null;
      noSeries('No OF session for this creator — new-fan series unavailable');
    }
    // ---- New / Renew window totals (there is NO per-day renew series on OF) ----
    try {
      const ov = await Fastt.get('/api/of/v2/users/me/stats/overview', { start: range.from, end: range.to });
      const s = ((ov || {}).visitors || {}).subscriptions || {};
      const vis = ((ov || {}).visitors || {}).visitors || {};
      const nw = (s.new || {}), rn = (s.renew || {});
      const all = (Number(nw.total) || 0) + (Number(rn.total) || 0);
      newStrip.innerHTML =
        kbox('New subscribers', Fastt.fmtInt(nw.total || 0), deltaTxt(nw.delta), deltaCls(nw.delta)) +
        kbox('Renews', Fastt.fmtInt(rn.total || 0), deltaTxt(rn.delta), deltaCls(rn.delta)) +
        kbox('All (new + renews)', Fastt.fmtInt(all), 'window total', '') +
        kbox('Profile visitors', Fastt.fmtInt(vis.total || 0), deltaTxt(vis.delta), deltaCls(vis.delta));
      newNote.innerHTML =
        'Only <b style="color:#f4776e">New subscribers</b> has a per-day feed on OF (subscribers/chart → <span style="font-family:ui-monospace,monospace">subscribes[]</span>), which is the plotted line. ' +
        'OF exposes <b>no per-day renew series</b>, so <b style="color:#34d399">Renews</b> and <b style="color:#4166f6">All</b> are shown as window totals above (users/me/stats/overview) rather than invented lines.' +
        '<br>The two counters are not the same population: the plotted series is OF’s free-inclusive subscriber chart, while New/Renew above are OF’s subscription counters — a free-trial fan lands in the line but not in “New subscribers”.';
      legendBs.forEach((b) => {
        const t = b.textContent.trim();
        b.style.opacity = t === 'New subscribers' ? '1' : '.6';
        b.title = t === 'New subscribers'
          ? 'Plotted per day from OF subscribers/chart'
          : 'No per-day feed on OF — shown as a window total in the strip below';
        if (t !== 'New subscribers' && b.dataset.ftNote !== '1') {
          b.dataset.ftNote = '1';
          const s2 = document.createElement('span');
          s2.style.cssText = 'color:#6a6a6a;font-size:11.5px';
          s2.textContent = '(total only)';
          b.appendChild(s2);
        }
      });
    } catch (e) {
      newStrip.innerHTML = '';
      newNote.textContent = 'OF subscription totals unavailable for this creator (no live session).';
    }
    // ---- Total fans: live counts ----
    try {
      const c = await Fastt.get('/api/of/v2/subscriptions/count/all');
      const sb = (c && c.subscribers) || {}, sn = (c && c.subscriptions) || {};
      totalStrip.innerHTML =
        kbox('Fans (active)', Fastt.fmtInt(sb.active || 0), 'OF subscribers.active — who a mass message reaches') +
        kbox('Online now', Fastt.fmtInt(sb.activeOnline || 0), 'subscribers.activeOnline') +
        kbox('Expired fans', Fastt.fmtInt(sb.expired || 0), 'lapsed, still in the audience list') +
        kbox('Restricted / blocked', Fastt.fmtInt((sb.restricted || 0) + (sb.blocked || 0)), 'excluded from sends') +
        kbox('All-time fans', Fastt.fmtInt(sb.all || 0), 'subscribers.all') +
        kbox('Creators followed', Fastt.fmtInt(sn.active || 0), 'active of ' + Fastt.fmtInt(sn.all || 0) + ' subscriptions');
      totalNote.innerHTML =
        'These are OF’s own live counts (<span style="font-family:ui-monospace,monospace">/subscriptions/count/all</span>), point-in-time — <b>not</b> our chat DB, which only holds fans we have messaged. ' +
        'OF exposes no total-fans history series, so there is nothing to plot over time here; the New-fans chart above is the growth signal.';
      // the counts ARE live; the *history* is what OF does not expose — say both
      setBadge(totalTitle, true);
      const extra = document.createElement('span');
      extra.className = 'ft-static';
      extra.textContent = 'NO HISTORY FEED';
      totalTitle.appendChild(extra);
    } catch (e) {
      totalStrip.innerHTML = '';
      totalNote.textContent = 'No OF session for this creator — live fan counts unavailable.';
      setBadge(totalTitle, false, 'NO LIVE DATA');
    }
  }

  // ── Fan data table (gen_info profiles — the push_to_sheets dataset) ──
  const fanTable = document.getElementById('fanTable');
  const fanNote = document.getElementById('fanNote');
  const fanQ = document.querySelector('.fanq');
  const fanCount = document.getElementById('fanCount');
  const fanCsv = document.getElementById('fanCsv');
  let fanRows = null, fanFiltered = [];
  let cohortFilter = null;   // {key,label,lo,hi} set by clicking a cohort tile
  const fanSort = { key: 'total_spend', dir: -1 };
  // Lifetime-spend cohorts (dollars). hi is exclusive; null hi = open top.
  const COHORTS = [
    { key: 'none',  label: 'Non-payers',   lo: -1,  hi: 0.005, color: '#5a5a5a' },
    { key: 'micro', label: '$0–20',        lo: 0.005, hi: 20,  color: '#5b8def' },
    { key: 'mid',   label: '$20–100',      lo: 20,  hi: 100,   color: '#67d1ae' },
    { key: 'high',  label: '$100–500',     lo: 100, hi: 500,   color: '#e5a35b' },
    { key: 'whale', label: 'Whales $500+', lo: 500, hi: null,  color: '#ec4b9b' },
  ];
  const inCohort = (spend, c) => {
    const s = Number(spend) || 0;
    return s >= c.lo && (c.hi == null || s < c.hi);
  };
  const FAN_COLS = [
    { k: 'fan_name', l: 'Fan' },
    { k: 'nickname', l: 'Nickname' },
    { k: 'language', l: 'Lang' },
    { k: 'total_spend', l: 'Spend', num: true },
    { k: 'message_count', l: 'Msgs', num: true },
    { k: 'short_bio', l: 'Short bio' },
    { k: 'bullet_points', l: 'Rich note' },
    { k: '_q', l: 'Q1 / Q2 / Q3' },
    { k: '_t', l: 'Tease 1 / 2 / 3' },
    { k: 'note_on_of', l: 'Note on OF' },
    { k: 'last_updated', l: 'Updated' },
  ];
  // An optional profile field that gen_info never filled renders as an EMPTY cell.
  // A dash here would read as "we looked and there is nothing", which is the same
  // thing but noisier across 11 columns × 249 fans.
  const stack = (items) => {
    const real = items.filter((x) => x && String(x).trim());
    if (!real.length) return '';
    return '<ul class="stk clamp">' + real.map((t) => '<li>' + Fastt.esc(t) + '</li>').join('') + '</ul>';
  };
  const dOnly = (iso) => { const d = Fastt.parseUtc(iso); return d ? d.toISOString().slice(0, 10) : ''; };
  function renderFans() {
    if (!fanRows) return;
    const needle = (fanQ.value || '').trim().toLowerCase();
    let rows = fanRows.slice();
    if (cohortFilter) rows = rows.filter((r) => inCohort(r.total_spend, cohortFilter));
    if (needle) {
      rows = rows.filter((r) => [r.fan_name, r.nickname, r.short_bio, r.bullet_points, r.q1, r.q2, r.q3, String(r.fan_id)]
        .some((v) => (v || '').toString().toLowerCase().indexOf(needle) >= 0));
    }
    rows.sort((a, b) => {
      const av = a[fanSort.key], bv = b[fanSort.key];
      if (fanSort.key === 'total_spend' || fanSort.key === 'message_count') {
        return ((Number(av) || 0) - (Number(bv) || 0)) * fanSort.dir;
      }
      return String(av || '').localeCompare(String(bv || '')) * fanSort.dir;
    });
    fanFiltered = rows;
    let h = '<table><thead><tr>';
    FAN_COLS.forEach((c) => {
      h += '<th data-k="' + c.k + '" class="' + (c.num ? 'r ' : '') + (fanSort.key === c.k ? 'on' : '') + '">' +
        Fastt.esc(c.l) + (fanSort.key === c.k ? (fanSort.dir < 0 ? ' ↓' : ' ↑') : '') + '</th>';
    });
    h += '</tr></thead><tbody>';
    if (!rows.length) {
      h += '<tr><td colspan="' + FAN_COLS.length + '" style="text-align:center;color:#8a8a8a;padding:34px">' +
        (fanRows.length ? 'No fan matches “' + Fastt.esc(needle) + '”.' : 'No profiled fans yet for this creator — gen_info builds these after a fan has messaged.') +
        '</td></tr>';
    }
    rows.forEach((r) => {
      h += '<tr>' +
        '<td class="nw"><span class="fname">' + Fastt.esc(r.fan_name || r.fan_id) + '</span><br><span class="fid">' + Fastt.esc(r.fan_id) + '</span></td>' +
        '<td class="nw">' + Fastt.esc(r.nickname || '') + '</td>' +
        '<td class="nw" style="text-transform:uppercase;font-family:ui-monospace,monospace">' + Fastt.esc(r.language || '') + '</td>' +
        '<td class="r" style="color:' + (Number(r.total_spend) > 0 ? '#fff' : '#5a5a5a') + ';font-weight:600">' +
          Fastt.fmtMoney(r.total_spend) + '</td>' +
        '<td class="r" style="color:' + (r.message_count ? '#9a9a9a' : '#5a5a5a') + '">' + Fastt.fmtInt(r.message_count || 0) + '</td>' +
        '<td class="w" title="' + Fastt.esc(r.short_bio || '') + '"><div class="clamp">' + Fastt.esc(r.short_bio || '') + '</div></td>' +
        '<td class="w" title="' + Fastt.esc(r.bullet_points || '') + '"><div class="clamp">' + Fastt.esc(r.bullet_points || '') + '</div></td>' +
        '<td class="w" title="' + Fastt.esc([r.q1, r.q2, r.q3].filter(Boolean).join('\n')) + '">' + stack([r.q1, r.q2, r.q3]) + '</td>' +
        '<td class="w" title="' + Fastt.esc([r.tease1, r.tease2, r.tease3].filter(Boolean).join('\n')) + '">' + stack([r.tease1, r.tease2, r.tease3]) + '</td>' +
        '<td class="nw" style="color:' + (r.note_on_of === 'added' ? '#67d1ae' : '#8a8a8a') + '">' + Fastt.esc(r.note_on_of || '') + '</td>' +
        '<td class="nw">' + Fastt.esc(dOnly(r.last_updated)) + '</td></tr>';
    });
    h += '</tbody></table>';
    fanTable.innerHTML = h;
    fanTable.querySelectorAll('th[data-k]').forEach((th) => th.addEventListener('click', () => {
      const k = th.dataset.k;
      if (k === '_q' || k === '_t') return;
      if (fanSort.key === k) fanSort.dir = -fanSort.dir;
      else { fanSort.key = k; fanSort.dir = (k === 'total_spend' || k === 'message_count') ? -1 : 1; }
      renderFans();
    }));
    fanCount.textContent = rows.length === fanRows.length
      ? Fastt.fmtInt(rows.length) + ' fans'
      : Fastt.fmtInt(rows.length) + ' of ' + Fastt.fmtInt(fanRows.length) + ' fans';
  }
  async function loadFans() {
    if (!Fastt.account()) {
      fanTable.innerHTML = '<div style="padding:34px;text-align:center;color:#8a8a8a">No creator selected — fan profiles are per creator.</div>';
      setBadge(fanTitle, false, 'NO LIVE DATA');
      return;
    }
    fanTable.innerHTML = '<div style="padding:34px;text-align:center;color:#8a8a8a">Loading fan profiles…</div>';
    try {
      const r = await Fastt.get('/admin/stats/per-fan-data', { account_id: Fastt.account(), limit: 2000 });
      fanRows = r.rows || [];
      renderFans();
      fanNote.innerHTML = 'GET /admin/stats/per-fan-data — the per-fan profiles <span style="font-family:ui-monospace,monospace">gen_info</span> builds, ' +
        'i.e. exactly what <span style="font-family:ui-monospace,monospace">push_to_sheets</span> exports, read live from our DB. ' +
        'Spend and message counts come from our own ingest, so a fan OF knows but we have never messaged will not appear here.' +
        '<br>Scroll the table sideways for short bio, rich note, Q1–Q3, teases, note-on-OF and last-updated; hover a clamped cell for the full text.';
      setBadge(fanTitle, true);
    } catch (e) {
      fanTable.innerHTML = '<div style="padding:34px;text-align:center;color:#8a8a8a">Fan profiles unavailable — see the error toast.</div>';
      setBadge(fanTitle, false, 'NO LIVE DATA');
      Fastt.oops(e);
    }
  }
  fanQ.addEventListener('input', Fastt.debounce(renderFans, 180));
  fanCsv.addEventListener('click', () => {
    if (!fanFiltered.length) { Fastt.toast('Nothing to download yet'); return; }
    const q = (v) => '"' + String(v == null ? '' : v).replace(/"/g, '""') + '"';
    const head = ['fan_id', 'fan_name', 'nickname', 'language', 'total_spend', 'message_count',
      'short_bio', 'bullet_points', 'q1', 'q2', 'q3', 'tease1', 'tease2', 'tease3', 'note_on_of', 'last_updated'];
    const lines = [head.join(',')].concat(fanFiltered.map((r) => head.map((k) => q(r[k])).join(',')));
    const url = URL.createObjectURL(new Blob([lines.join('\n')], { type: 'text/csv;charset=utf-8' }));
    const a = document.createElement('a');
    a.href = url;
    a.download = 'fan-data-' + Fastt.account() + '-' + new Date().toISOString().slice(0, 10) + '.csv';
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 4000);
  });

  // ── Fan cohorts + spend distribution (lifetime spend, from the roster) ──
  const cohBar = document.getElementById('cohBar');
  const cohGrid = document.getElementById('cohGrid');
  const cohClearHolder = document.getElementById('cohClearHolder');
  const cohNote = document.getElementById('cohNote');
  const spendSvg = document.getElementById('spendSvg');
  const spendLeg = document.getElementById('spendLeg');
  const SPEND_BUCKETS = [
    { label: '$0', lo: -1, hi: 0.005 }, { label: '1–10', lo: 0.005, hi: 10 },
    { label: '10–25', lo: 10, hi: 25 }, { label: '25–50', lo: 25, hi: 50 },
    { label: '50–100', lo: 50, hi: 100 }, { label: '100–250', lo: 100, hi: 250 },
    { label: '250–500', lo: 250, hi: 500 }, { label: '500–1k', lo: 500, hi: 1000 },
    { label: '1k+', lo: 1000, hi: null },
  ];
  const colorForSpend = (s) => {
    const c = COHORTS.find((x) => inCohort(s, x));
    return c ? c.color : '#5a5a5a';
  };
  // Loading skeleton — the spend SVG would otherwise render fully blank until
  // the (creator → charts → fans) await chain resolves and renderCohorts() draws.
  function spendSkeleton() {
    const L = 64, R = 968, B = 250, n = SPEND_BUCKETS.length, bandW = (R - L) / n, barW = bandW * 0.6;
    const heights = [46, 92, 210, 150, 74, 118, 60, 96, 40];
    let h = '<defs><linearGradient id="skShim" x1="0" y1="0" x2="1" y2="0">' +
      '<stop offset="0" stop-color="#1e1e20"/><stop offset="0.5" stop-color="#28282b"/><stop offset="1" stop-color="#1e1e20"/>' +
      '<animate attributeName="x1" values="-1;1" dur="1.15s" repeatCount="indefinite"/>' +
      '<animate attributeName="x2" values="0;2" dur="1.15s" repeatCount="indefinite"/></linearGradient></defs>';
    h += '<g stroke="#2a2a2a" stroke-dasharray="3 5">' +
      [24, 80.5, 137, 193.5].map((y) => '<line x1="64" y1="' + y + '" x2="968" y2="' + y + '"/>').join('') +
      '</g><line x1="64" y1="250" x2="968" y2="250" stroke="#3a3a3a"/>';
    for (let i = 0; i < n; i++) {
      const bh = heights[i % heights.length];
      const cx = L + bandW * i + bandW / 2, x = cx - barW / 2, y = B - bh;
      h += '<rect x="' + x.toFixed(1) + '" y="' + y.toFixed(1) + '" width="' + barW.toFixed(1) +
        '" height="' + bh + '" rx="3" fill="url(#skShim)"/>';
    }
    h += '<text x="500" y="140" text-anchor="middle" fill="#6a6a6a" font-family="Inter, sans-serif" font-size="14">Loading spend distribution…</text>';
    spendSvg.innerHTML = h;
    spendLeg.innerHTML = '';
  }
  spendSkeleton();
  function cohEmpty(msg) {
    cohBar.innerHTML = ''; cohBar.style.display = 'none';
    cohGrid.innerHTML = '<div style="color:#8a8a8a;font-size:13.5px;padding:6px 2px">' + Fastt.esc(msg) + '</div>';
    cohClearHolder.innerHTML = '';
    spendSvg.innerHTML =
      '<g stroke="#2a2a2a" stroke-dasharray="3 5">' +
      [24, 80.5, 137, 193.5].map((y) => '<line x1="64" y1="' + y + '" x2="968" y2="' + y + '"/>').join('') +
      '</g><line x1="64" y1="250" x2="968" y2="250" stroke="#3a3a3a"/>' +
      '<text x="500" y="140" text-anchor="middle" fill="#8a8a8a" font-family="Inter, sans-serif" font-size="14">' +
      Fastt.esc(msg) + '</text>';
    spendLeg.innerHTML = '';
    cohNote.textContent = '';
    setBadge(cohTitle, false, 'NO LIVE DATA');
    setBadge(spendTitle, false, 'NO LIVE DATA');
  }
  function drawSpend() {
    const counts = SPEND_BUCKETS.map((b) => fanRows.filter((r) => {
      const s = Number(r.total_spend) || 0;
      return s >= b.lo && (b.hi == null || s < b.hi);
    }).length);
    const L = 64, R = 968, T = 24, B = 250, n = counts.length;
    let maxV = Math.max.apply(null, counts.concat([0]));
    maxV = maxV <= 0 ? 4 : Math.max(4, Math.ceil(maxV / 4) * 4);
    const bandW = (R - L) / n, barW = bandW * 0.6;
    const Y = (v) => B - (v / maxV) * (B - T);
    const fr = [1, 0.75, 0.5, 0.25, 0], labY = [29, 85, 142, 198, 255];
    let h = '<g font-family="Inter, sans-serif" font-size="14" fill="#6a6a6a" text-anchor="end">';
    fr.forEach((f, i) => { h += '<text x="40" y="' + labY[i] + '">' + Math.round(maxV * f) + '</text>'; });
    h += '</g><g stroke="#2a2a2a" stroke-dasharray="3 5">';
    [24, 80.5, 137, 193.5].forEach((y) => { h += '<line x1="64" y1="' + y + '" x2="968" y2="' + y + '"/>'; });
    h += '</g><line x1="64" y1="250" x2="968" y2="250" stroke="#3a3a3a"/>';
    counts.forEach((v, i) => {
      const cx = L + bandW * i + bandW / 2, x = cx - barW / 2, y = Y(v), bh = B - y;
      const col = colorForSpend(SPEND_BUCKETS[i].lo < 0 ? -1 : SPEND_BUCKETS[i].lo + 0.001);
      h += '<rect x="' + x.toFixed(1) + '" y="' + y.toFixed(1) + '" width="' + barW.toFixed(1) +
        '" height="' + Math.max(0, bh).toFixed(1) + '" rx="3" fill="' + col + '"><title>' +
        Fastt.esc(SPEND_BUCKETS[i].label + ' · ' + v + ' fans') + '</title></rect>';
      if (v > 0) h += '<text x="' + cx.toFixed(1) + '" y="' + (y - 6).toFixed(1) +
        '" text-anchor="middle" font-family="Inter, sans-serif" font-size="13" fill="#cfcfcf">' + v + '</text>';
      h += '<text x="' + cx.toFixed(1) + '" y="280" text-anchor="middle" font-family="Inter, sans-serif" font-size="13.5" fill="#8a8a8a">' +
        Fastt.esc(SPEND_BUCKETS[i].label) + '</text>';
    });
    spendSvg.innerHTML = h;
    spendLeg.innerHTML = 'Each bar = number of fans whose <b style="color:#cfcfcf;font-weight:600">lifetime spend</b> falls in that range · bar color marks its cohort';
    setBadge(spendTitle, true);
  }
  function renderCohorts() {
    if (!fanRows || !fanRows.length) {
      cohEmpty(!Fastt.account() ? 'No creator selected — cohorts are per creator'
        : 'No profiled fans yet for this creator');
      return;
    }
    const totalFans = fanRows.length;
    const totalSpend = fanRows.reduce((a, r) => a + (Number(r.total_spend) || 0), 0);
    const stats = COHORTS.map((c) => {
      const rs = fanRows.filter((r) => inCohort(r.total_spend, c));
      const spend = rs.reduce((a, r) => a + (Number(r.total_spend) || 0), 0);
      return { c, n: rs.length, spend };
    });
    // population bar (segments by count share)
    cohBar.style.display = 'flex';
    cohBar.innerHTML = stats.map((s) => s.n
      ? '<span title="' + Fastt.esc(s.c.label + ' · ' + s.n + ' fans') + '" style="width:' +
        (s.n / totalFans * 100).toFixed(2) + '%;background:' + s.c.color + '"></span>' : '').join('');
    // tiles
    cohGrid.innerHTML = stats.map((s) => {
      const pctFans = totalFans ? (s.n / totalFans * 100) : 0;
      const pctRev = totalSpend ? (s.spend / totalSpend * 100) : 0;
      return '<div class="cohtile' + (cohortFilter && cohortFilter.key === s.c.key ? ' on' : '') +
        '" data-key="' + s.c.key + '" title="Click to filter the Fan data table to this cohort">' +
        '<div class="ch-top"><span class="ch-sw" style="background:' + s.c.color + '"></span>' +
        '<span class="ch-lbl">' + Fastt.esc(s.c.label) + '</span></div>' +
        '<div class="ch-n">' + Fastt.fmtInt(s.n) + '</div>' +
        '<div class="ch-sub">' + pctFans.toFixed(1) + '% of fans</div>' +
        '<div class="ch-rev">' + Fastt.fmtMoney(s.spend) + ' · ' + pctRev.toFixed(1) + '% of spend</div></div>';
    }).join('');
    cohGrid.querySelectorAll('.cohtile').forEach((t) => t.addEventListener('click', () => {
      const key = t.dataset.key;
      cohortFilter = (cohortFilter && cohortFilter.key === key) ? null : COHORTS.find((x) => x.key === key);
      renderCohorts();   // repaint .on state + clear button
      renderFans();      // apply filter to the table
      if (cohortFilter) fanTable.scrollIntoView({ block: 'nearest' });
    }));
    // clear chip
    cohClearHolder.innerHTML = cohortFilter
      ? '<button class="cohclear" type="button">Filtering table: ' + Fastt.esc(cohortFilter.label) + ' ✕</button>' : '';
    const clr = cohClearHolder.querySelector('.cohclear');
    if (clr) clr.addEventListener('click', () => { cohortFilter = null; renderCohorts(); renderFans(); });
    // headline: the whale share is the number worth surfacing
    const whale = stats.find((s) => s.c.key === 'whale');
    const payers = stats.filter((s) => s.c.key !== 'none').reduce((a, s) => a + s.n, 0);
    cohNote.innerHTML =
      'Lifetime spend per fan, from <span style="font-family:ui-monospace,monospace">/admin/stats/per-fan-data</span> (our ingest) — the same roster in the table below. ' +
      '<b>' + Fastt.fmtInt(payers) + '</b> of ' + Fastt.fmtInt(totalFans) + ' fans have spent; ' +
      (whale && whale.n
        ? '<b style="color:#ec4b9b">' + Fastt.fmtInt(whale.n) + ' whale' + (whale.n === 1 ? '' : 's') + '</b> drive ' +
          (totalSpend ? (whale.spend / totalSpend * 100).toFixed(0) : 0) + '% of all spend. '
        : 'no whales over $500 yet. ') +
      'Click any cohort to filter the Fan data table. This is lifetime population value — the strip up top is the money lens for the selected window.';
    setBadge(cohTitle, true);
    drawSpend();
  }

  await loadCreators();
  await loadFanCharts();
  await loadFans();
  renderCohorts();
});
