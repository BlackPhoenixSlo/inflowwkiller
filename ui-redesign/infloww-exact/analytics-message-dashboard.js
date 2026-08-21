// ==== LIVE WIRING (fastt relay) ====
// Reads  GET /admin/paid-messages?from&to&account_id&employee_id&status&type&direction
//                                &fan_query&collapse_mass&limit  → {rows[], next_before_id,
//                                next_before_sent_at}  (keyset paging, newest first)
//        GET /admin/employees                                    → the Sender picker
//        Fastt.accounts()                                        → the Creator picker
// Export GET /admin/paid-messages?format=csv                     → server-streamed download
//
// Every control in the filter grid maps to a REAL query param except two, which the
// endpoint has no parameter for and are therefore applied to the loaded rows client-side
// and labelled as such: the price min/max (filters price_cents) and the message-text
// search box. `sent_at` from this route is tz-NAIVE UTC → Fastt.parseUtc.
Fastt.ready(async () => {
  const DAY = 86400000;
  const PAGE = 50;
  const isoDay = (d) => d.toISOString().slice(0, 10);
  const dayStart = (s) => new Date(s + 'T00:00:00Z');
  const longLbl = (s) => dayStart(s).toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric', timeZone: 'UTC' });

  const now = new Date();
  const range = { from: isoDay(new Date(now.getTime() - 29 * DAY)), to: isoDay(now) };

  const el = {
    range: document.getElementById('mdRange'),
    emp: document.getElementById('fEmp'),
    acct: document.getElementById('fAcct'),
    fan: document.getElementById('fFan'),
    min: document.getElementById('fMin'),
    max: document.getElementById('fMax'),
    status: document.getElementById('fStatus'),
    type: document.getElementById('fType'),
    dir: document.getElementById('fDir'),
    collapse: document.getElementById('fCollapse'),
    qText: document.getElementById('qText'),
    reset: document.querySelector('.btn-reset'),
    search: document.querySelector('.btn-search'),
    nn: document.querySelector('.result-head .nn'),
    lu: document.querySelector('.result-head .lu'),
    headTxt: document.querySelector('.result-head .txt'),
    table: document.querySelector('.mtable'),
    empty: document.querySelector('.mtable .empty-state'),
    more: document.getElementById('mdMore'),
    strip: document.getElementById('mdStrip'),
    note: document.getElementById('mdNote'),
    banner: document.getElementById('mdBanner'),
  };

  function paintRange() {
    const tn = Array.prototype.filter.call(el.range.childNodes,
      (n) => n.nodeType === 3 && n.textContent.trim());
    if (tn.length >= 2) { tn[0].textContent = ' ' + longLbl(range.from) + ' '; tn[1].textContent = ' ' + longLbl(range.to) + ' '; }
  }
  paintRange();
  el.range.title = 'Change the reporting window — from/to are REQUIRED by /admin/paid-messages';
  el.range.addEventListener('click', (ev) => {
    ev.stopPropagation();
    document.querySelectorAll('.ft-pop').forEach((n) => n.remove());
    const pop = document.createElement('div');
    pop.className = 'ft-pop';
    const r = el.range.getBoundingClientRect();
    pop.style.left = Math.max(8, Math.min(r.left, window.innerWidth - 290)) + 'px';
    pop.style.top = (r.bottom + 6) + 'px';
    pop.innerHTML =
      '<div class="pr"><button data-d="1">Today</button><button data-d="7">Last 7 days</button>' +
      '<button data-d="30">Last 30 days</button><button data-d="90">Last 90 days</button></div>' +
      '<div class="rw"><span>From</span><input type="date" data-k="f" value="' + range.from + '"></div>' +
      '<div class="rw"><span>To</span><input type="date" data-k="t" value="' + range.to + '"></div>' +
      '<button class="go" type="button">Apply</button>';
    document.body.appendChild(pop);
    const ph = pop.getBoundingClientRect().height;
    if (r.bottom + 6 + ph > window.innerHeight - 8) pop.style.top = Math.max(8, r.top - ph - 6) + 'px';
    const close = () => { pop.remove(); document.removeEventListener('click', close); };
    setTimeout(() => document.addEventListener('click', close), 0);
    pop.addEventListener('click', (e) => {
      e.stopPropagation();
      const b = e.target.closest('.pr button');
      if (b) {
        const to = new Date();
        close();
        range.from = isoDay(new Date(to.getTime() - (Number(b.dataset.d) - 1) * DAY));
        range.to = isoDay(to);
        paintRange(); reload();
        return;
      }
      if (e.target.closest('.go')) {
        const f = pop.querySelector('input[data-k="f"]').value;
        const t = pop.querySelector('input[data-k="t"]').value;
        if (!f || !t) { Fastt.toast('Pick both dates'); return; }
        if (f > t) { Fastt.toast('“From” must be on or before “To”'); return; }
        close(); range.from = f; range.to = t; paintRange(); reload();
      }
    });
  });
  Fastt.liveBadge(el.range);

  // ── pickers ──
  const accounts = Fastt.accounts();
  const acctName = {};
  accounts.forEach((a) => {
    acctName[String(a.id)] = a.nickname || String(a.id);
    const o = document.createElement('option');
    o.value = String(a.id); o.textContent = a.nickname || String(a.id);
    el.acct.appendChild(o);
  });
  if (Fastt.account()) el.acct.value = String(Fastt.account());
  const empName = {};
  try {
    const e = await Fastt.get('/admin/employees', { include_disabled: false }, { noAccount: true });
    (e.employees || []).forEach((x) => {
      empName[String(x.id)] = x.display_name;
      const o = document.createElement('option');
      o.value = String(x.id); o.textContent = x.display_name;
      el.emp.appendChild(o);
    });
  } catch (err) {
    el.emp.disabled = true;
    el.emp.title = '/admin/employees unavailable — sender filter disabled';
  }

  // ── column semantics that the endpoint genuinely does not serve ──
  const noParam = 'No query parameter on /admin/paid-messages — applied to the rows already loaded, ' +
    'so widen the window or load more pages for a complete answer.';
  [el.min, el.max].forEach((n) => { n.title = 'Client-side filter on price_cents. ' + noParam; });
  el.qText.title = 'Client-side filter on the message body. ' + noParam;
  el.fan.title = 'Server-side: fan_query (substring on username / display name, 2+ chars). ' +
    'A fan search also un-collapses mass rows so you see the real per-fan sends.';
  el.emp.title = 'Server-side: employee_id (sent_by_employee_id). Only meaningful on outbound messages.';
  el.acct.title = 'Server-side: account_id. Leave on “All creators” for the whole roster.';
  el.status.title = 'Server-side: status=paid|unpaid|all — did the fan actually unlock it.';
  el.type.title = 'Server-side: type=paid|free|all — did the message carry a price at all.';
  el.dir.title = 'Server-side: direction=out|in|any.';
  el.collapse.title = 'Server-side: collapse_mass. On, each mass broadcast is one “sent to N fans” summary row.';
  Fastt.staticBadge(document.querySelector('.md-toprow .searchbig'), 'CLIENT-SIDE');
  Fastt.staticBadge(document.querySelector('.mcell .pricewrap'), 'CLIENT-SIDE');

  // ── state ──
  let rows = [], cursor = null, done = false, loading = false;

  const params = () => {
    const p = {
      from: range.from, to: range.to,
      status: el.status.value, type: el.type.value, direction: el.dir.value,
      collapse_mass: el.collapse.checked ? 'true' : 'false',
      limit: PAGE,
    };
    if (el.acct.value) p.account_id = el.acct.value;
    if (el.emp.value) p.employee_id = el.emp.value;
    const fq = (el.fan.value || '').trim();
    if (fq.length >= 2) p.fan_query = fq;
    return p;
  };

  const strip = (html) => String(html || '').replace(/<[^>]*>/g, '').replace(/&nbsp;/g, ' ').trim();
  const kbox = (label, value, sub) =>
    '<div class="kbox"><div class="kl">' + Fastt.esc(label) + '</div><div class="kv">' + value + '</div>' +
    (sub ? '<div class="kd">' + Fastt.esc(sub) + '</div>' : '') + '</div>';
  const money = (c) => (c === null || c === undefined) ? '' : Fastt.fmtCents(c);

  function clientFilter(list) {
    const min = el.min.value === '' ? null : Math.round(Number(el.min.value) * 100);
    const max = el.max.value === '' ? null : Math.round(Number(el.max.value) * 100);
    const q = (el.qText.value || '').trim().toLowerCase();
    return list.filter((r) => {
      const price = Number(r.price_cents) || 0;
      if (min !== null && price < min) return false;
      if (max !== null && price > max) return false;
      if (q && strip(r.body).toLowerCase().indexOf(q) < 0) return false;
      return true;
    });
  }

  // automation_kind tokens as the real app labels them (MessageRowGeneric.tsx).
  // A specific automation beats the flat "Automation" employee sentinel.
  const AUTOMATION_LABELS = {
    welcome_chatter_for_info: 'Info-gather',
    of_ai_chat: 'Info-gather',        // renamed 2026-08-19 — legacy rows still render
    welcome: 'Welcome', deep_convo: 'Deep Convo', followup: 'Follow-up',   // deep_convo: retired engine
    autoreply: 'Auto-reply', reply_mass_funnel: 'Funnel reply', send_mass_message: 'Mass message',
    mass_nudge: 'Mass nudge', online_blast: 'Online blast', nudge_online: 'Online nudge',
    gen_info: 'Profiler', ai_chatter: 'AI Chatter', ai_upseller: 'AI Upseller',
  };
  function senderLabel(r) {
    if (r.direction === 'in') return 'Fan';
    if (r.automation_kind) return AUTOMATION_LABELS[r.automation_kind] || r.automation_kind;
    if (r.employee_name) return r.employee_name;
    if (r.sent_by_employee_id) return empName[String(r.sent_by_employee_id)] || ('Employee #' + r.sent_by_employee_id);
    return 'Unattributed';   // the log genuinely carries no sender for this row
  }
  // compact stamp — the mockup's Sent-time column is too narrow for a full locale string
  const stamp = (s) => {
    const d = Fastt.parseUtc(s);
    if (!d) return '';
    return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' }) + ', ' +
           d.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', hour12: false });
  };

  function render() {
    const shown = clientFilter(rows);
    el.table.querySelectorAll('.mtrow').forEach((n) => n.remove());
    if (!shown.length) {
      el.empty.style.display = '';
      const cap = el.empty.querySelector('.cap');
      if (cap) {
        cap.textContent = rows.length
          ? 'No message on this page matches the price / text filter'
          : 'No messages in ' + longLbl(range.from) + ' → ' + longLbl(range.to) + ' for these filters';
      }
    } else {
      el.empty.style.display = 'none';
      const frag = document.createDocumentFragment();
      shown.forEach((r) => {
        const d = document.createElement('div');
        d.className = 'mtrow';
        const fan = r.fan || {};
        const body = strip(r.body) || (r.media_count ? '(' + r.media_count + ' media, no text)' : '(no text)');
        const price = Number(r.price_cents) || 0;
        let pill;
        if (r.direction === 'in') pill = '<span class="pill-paid i">inbound</span>';
        else if (!price) pill = '<span class="pill-paid f">free</span>';
        else if (r.is_paid) pill = '<span class="pill-paid y">purchased</span>';
        else pill = '<span class="pill-paid n">unpaid</span>';
        const extras = [];
        if (r.media_count) extras.push(r.media_count + ' media');
        if (r.is_tip) extras.push('tip');
        if (r.is_mass_summary) extras.push('mass broadcast');
        const sentTo = r.is_mass_summary
          ? '<span class="who mut">mass audience</span>'
          : '<span class="who">' + Fastt.esc(fan.display_name || fan.username || r.fan_id || '—') + '</span>' +
            (fan.username ? '<span class="sub">@' + Fastt.esc(fan.username) + '</span>' : '');
        d.innerHTML =
          '<div><span class="who"' + (senderLabel(r) === 'Unattributed'
              ? ' style="color:#7a7a7a" title="This row carries no sent_by_employee_id and no automation_kind in the message log — nothing to attribute it to."'
              : '') + '>' + Fastt.esc(senderLabel(r)) + '</span></div>' +
          '<div><span class="who mut">' + Fastt.esc(acctName[String(r.account_id)] || r.account_id) + '</span></div>' +
          '<div style="flex-direction:column;align-items:flex-start;justify-content:center">' + sentTo + '</div>' +
          '<div><span class="msg" title="' + Fastt.esc(body) + '">' + Fastt.esc(body) +
            (extras.length ? ' <span class="mut">· ' + Fastt.esc(extras.join(' · ')) + '</span>' : '') + '</span></div>' +
          '<div><span class="who" title="' + Fastt.esc(Fastt.fmtDate(r.sent_at)) + '">' + Fastt.esc(stamp(r.sent_at)) + '</span></div>' +
          // a free / inbound message has no price at all — the Purchased column already
          // says so, and a dash here just reads as an unresolved placeholder
          '<div>' + (price ? money(price) : '') + '</div>' +
          '<div>' + pill +
            (r.is_paid && r.purchased_at ? '<span class="sub" style="margin-left:6px">' + Fastt.esc(Fastt.fmtAgo(r.purchased_at)) + '</span>' : '') +
          '</div>';
        frag.appendChild(d);
      });
      el.table.appendChild(frag);
    }
    // header + KPI strip, computed from what is actually on screen
    el.nn.textContent = shown.length
      ? Fastt.fmtInt(shown.length) + ' message' + (shown.length === 1 ? '' : 's') +
        (shown.length !== rows.length ? ' (of ' + Fastt.fmtInt(rows.length) + ' loaded)' : '') +
        (done ? '' : ' · more available')
      : 'No messages';
    el.lu.textContent = longLbl(range.from) + ' → ' + longLbl(range.to) +
      ' · ' + (el.acct.value ? (acctName[el.acct.value] || el.acct.value) : 'all creators');
    const priced = shown.filter((r) => (Number(r.price_cents) || 0) > 0 && r.direction !== 'in');
    const paid = priced.filter((r) => r.is_paid);
    const rev = paid.reduce((a, r) => a + (Number(r.price_cents) || 0), 0);
    const inbound = shown.filter((r) => r.direction === 'in').length;
    el.strip.innerHTML =
      kbox('Rows on screen', Fastt.fmtInt(shown.length), inbound ? Fastt.fmtInt(inbound) + ' inbound' : 'all outbound') +
      kbox('Priced (PPV)', Fastt.fmtInt(priced.length), priced.length ? Fastt.fmtInt(shown.length - priced.length - inbound) + ' free outbound' : 'none priced') +
      kbox('Purchased', Fastt.fmtInt(paid.length),
        priced.length ? (100 * paid.length / priced.length).toFixed(1) + '% unlock rate' : 'no priced rows') +
      kbox('Revenue on screen', Fastt.fmtCents(rev),
        paid.length ? 'avg ' + Fastt.fmtCents(rev / paid.length) + ' per unlock' : 'nothing unlocked yet');
    el.more.style.display = done ? 'none' : 'block';
    el.more.textContent = 'Load ' + PAGE + ' more';
  }
  const setBadge = (n, live, txt) => {
    if (!n) return;
    n.querySelectorAll(':scope > .ft-live, :scope > .ft-static').forEach((x) => x.remove());
    if (live) Fastt.liveBadge(n); else Fastt.staticBadge(n, txt);
  };

  async function loadPage(first) {
    if (loading) return;
    loading = true;
    if (first) { rows = []; cursor = null; done = false; el.nn.textContent = 'Loading…'; el.lu.textContent = ''; }
    else { el.more.textContent = 'Loading…'; }
    try {
      const p = params();
      if (cursor) { p.before_sent_at = cursor.at; p.before_id = cursor.id; }
      const r = await Fastt.get('/admin/paid-messages', p, { noAccount: true });
      const got = r.rows || [];
      rows = rows.concat(got);
      if (got.length < PAGE || !r.next_before_id) done = true;
      else cursor = { at: r.next_before_sent_at, id: r.next_before_id };
      render();
      setBadge(el.headTxt, true);
    } catch (e) {
      done = true;
      el.nn.textContent = 'Message log unavailable';
      el.lu.textContent = '';
      el.table.querySelectorAll('.mtrow').forEach((n) => n.remove());
      el.empty.style.display = '';
      const cap = el.empty.querySelector('.cap');
      if (cap) cap.textContent = 'Could not read /admin/paid-messages — see the error toast';
      el.strip.innerHTML = '';
      setBadge(el.headTxt, false, 'NO LIVE DATA');
      Fastt.oops(e);
    } finally {
      loading = false;
      el.more.textContent = 'Load ' + PAGE + ' more';
      el.more.style.display = done ? 'none' : 'block';
    }
  }
  // ── ledger analytics band (real money, whole-window totals) ──────────────
  // /admin/stats/revenue is the transactions ledger, NOT the message log — its
  // totals cover the full date-range regardless of the message-log filters
  // below, so it only refetches when the window or creator changes.
  const anBox = document.getElementById('mdAnalytics');
  let anScope = null;
  const KIND_TINT = { total: '#67d1ae', ppv: '#4166f6', tip: '#ec4b9b', sub: '#a78bfa' };
  const short$ = (c) => {
    const d = (Number(c) || 0) / 100;
    if (Math.abs(d) >= 1000) return '$' + (d / 1000).toFixed(d >= 10000 || d <= -10000 ? 0 : 1) + 'k';
    return '$' + Math.round(d);
  };
  const dayLbl = (iso) => dayStart(iso).toLocaleDateString('en-US', { month: 'short', day: 'numeric', timeZone: 'UTC' });

  function fillDays(from, to, byDay) {
    const map = {};
    (byDay || []).forEach((r) => { if (r.day) map[r.day] = (map[r.day] || 0) + (Number(r.total_cents) || 0); });
    const out = [];
    let d = dayStart(from); const end = dayStart(to); let guard = 0;
    while (d <= end && guard++ < 400) {
      const k = d.toISOString().slice(0, 10);
      out.push({ day: k, cents: map[k] || 0 });
      d = new Date(d.getTime() + DAY);
    }
    return out;
  }

  // bar chart redrawn from data: scales + axis ticks computed from the response
  function revChart(days) {
    const W = 560, H = 156, padL = 46, padR = 8, padT = 10, padB = 24;
    const iw = W - padL - padR, ih = H - padT - padB;
    const max = Math.max(1, ...days.map((d) => d.cents));
    const n = days.length || 1;
    const gap = iw / n, bw = Math.max(1.5, Math.min(26, gap * 0.7));
    const yTicks = [0, 0.5, 1].map((f) => {
      const v = max * f, yy = padT + ih - ih * f;
      return '<line x1="' + padL + '" y1="' + yy.toFixed(1) + '" x2="' + (W - padR) + '" y2="' + yy.toFixed(1) +
        '" stroke="#222" stroke-width="1"/>' +
        '<text x="' + (padL - 8) + '" y="' + (yy + 3).toFixed(1) + '" text-anchor="end" fill="#6a6a6a" font-size="9.5">' + short$(v) + '</text>';
    }).join('');
    let bars = '';
    days.forEach((d, i) => {
      const h = ih * d.cents / max;
      const x = padL + i * gap + (gap - bw) / 2;
      const y = padT + ih - h;
      bars += '<rect x="' + x.toFixed(1) + '" y="' + y.toFixed(1) + '" width="' + bw.toFixed(1) +
        '" height="' + Math.max(0, h).toFixed(1) + '" rx="1.5" fill="' + (d.cents ? '#4166f6' : '#282828') + '">' +
        '<title>' + Fastt.esc(dayLbl(d.day)) + ': ' + Fastt.esc(Fastt.fmtCents(d.cents)) + '</title></rect>';
    });
    const idx = n <= 1 ? [0] : [...new Set([0, Math.floor((n - 1) / 2), n - 1])];
    const xTicks = idx.map((i) => {
      const x = padL + i * gap + gap / 2;
      return '<text x="' + x.toFixed(1) + '" y="' + (H - 7) + '" text-anchor="middle" fill="#6a6a6a" font-size="9.5">' +
        Fastt.esc(dayLbl(days[i].day)) + '</text>';
    }).join('');
    return '<svg viewBox="0 0 ' + W + ' ' + H + '" role="img" aria-label="Revenue by day">' +
      yTicks + bars + xTicks + '</svg>';
  }

  const akbox = (label, tint, cents, sub) =>
    '<div class="akbox"><div class="kl"><i style="background:' + tint + '"></i>' + Fastt.esc(label) + '</div>' +
    '<div class="kv">' + Fastt.esc(Fastt.fmtCents(cents)) + '</div>' +
    '<div class="kd">' + Fastt.esc(sub) + '</div></div>';

  function renderAnalytics(byKind, byDay) {
    const K = {};
    (byKind || []).forEach((r) => {
      const k = r.kind || 'other';
      K[k] = K[k] || { c: 0, n: 0 };
      K[k].c += Number(r.total_cents) || 0; K[k].n += Number(r.count) || 0;
    });
    const grp = (...keys) => keys.reduce((a, k) => ({ c: a.c + (K[k] ? K[k].c : 0), n: a.n + (K[k] ? K[k].n : 0) }), { c: 0, n: 0 });
    const ppv = grp('ppv_message', 'ppv_post');
    const tip = grp('tip');
    const sub = grp('subscription', 'rebill');
    const total = (byKind || []).reduce((a, r) => ({ c: a.c + (Number(r.total_cents) || 0), n: a.n + (Number(r.count) || 0) }), { c: 0, n: 0 });
    const days = fillDays(range.from, range.to, byDay);
    const chartTotal = days.reduce((a, d) => a + d.cents, 0);
    const who = el.acct.value ? (acctName[el.acct.value] || el.acct.value) : 'all creators';
    anBox.innerHTML =
      '<div class="an-l"><div class="an-title">' +
        '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M12 1v22M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6" stroke-linecap="round"/></svg>' +
        'Revenue in window · <span class="mut">' + Fastt.esc(who) + '</span></div>' +
        '<div class="an-kpis">' +
          akbox('Total earned', KIND_TINT.total, total.c, Fastt.fmtInt(total.n) + ' payment' + (total.n === 1 ? '' : 's')) +
          akbox('PPV unlocks', KIND_TINT.ppv, ppv.c, Fastt.fmtInt(ppv.n) + ' sale' + (ppv.n === 1 ? '' : 's')) +
          akbox('Tips', KIND_TINT.tip, tip.c, Fastt.fmtInt(tip.n) + ' tip' + (tip.n === 1 ? '' : 's')) +
          akbox('Subs & rebills', KIND_TINT.sub, sub.c, Fastt.fmtInt(sub.n) + ' payment' + (sub.n === 1 ? '' : 's')) +
        '</div></div>' +
      '<div class="an-r"><div class="an-title">Revenue by day <span class="mut">· ' + Fastt.esc(Fastt.fmtCents(chartTotal)) + ' total</span></div>' +
        '<div class="an-chartwrap">' + (total.c ? revChart(days) : '<div class="an-err" style="color:#6a6a6a">No ledger revenue in this window for ' + Fastt.esc(who) + '.</div>') + '</div></div>' +
      '<div class="an-cap">Money from the transactions ledger (<b>/admin/stats/revenue</b>) — the true window total, unaffected by the message-log filters below. The KPI tiles under the table count only the message rows currently loaded.</div>';
    setBadge(anBox.querySelector('.an-l .an-title'), true);
  }

  async function loadAnalytics() {
    anBox.innerHTML = '<div class="an-skel"><span class="sp"></span>Loading revenue for this window…</div>';
    const base = { from: range.from, to: range.to };
    if (el.acct.value) base.account_id = el.acct.value;
    try {
      const [byKind, byDay] = await Promise.all([
        Fastt.get('/admin/stats/revenue', Object.assign({ group_by: 'kind' }, base), { noAccount: true }),
        Fastt.get('/admin/stats/revenue', Object.assign({ group_by: 'day' }, base), { noAccount: true }),
      ]);
      renderAnalytics(byKind.rows || [], byDay.rows || []);
    } catch (e) {
      anBox.innerHTML = '<div class="an-err">Could not load ledger revenue (/admin/stats/revenue) — see the error toast.</div>';
      Fastt.staticBadge(anBox, 'NO LIVE DATA');
      console.error(e);
    }
  }
  function maybeAnalytics() {
    const key = range.from + '|' + range.to + '|' + (el.acct.value || '');
    if (key === anScope) return;
    anScope = key;
    loadAnalytics();
  }

  const reload = () => { maybeAnalytics(); loadPage(true); };

  // ── quick-view presets → real server params (one-click, friendlier lead) ──
  const PRESETS = {
    all:       { type: 'all',  dir: 'any', status: 'all' },
    ppv:       { type: 'paid', dir: 'out', status: 'all' },
    purchased: { type: 'paid', dir: 'out', status: 'paid' },
    unpaid:    { type: 'paid', dir: 'out', status: 'unpaid' },
    free:      { type: 'free', dir: 'out', status: 'all' },
    inbound:   { type: 'all',  dir: 'in',  status: 'all' },
  };
  function activePreset() {
    const t = el.type.value, d = el.dir.value, s = el.status.value;
    if (d === 'in') return 'inbound';
    for (const k of ['all', 'ppv', 'purchased', 'unpaid', 'free']) {
      const p = PRESETS[k];
      if (p.type === t && p.dir === d && p.status === s) return k;
    }
    return null;
  }
  function syncPresetChips() {
    const a = activePreset();
    document.querySelectorAll('#mdPresets .chip').forEach((c) => c.classList.toggle('on', c.dataset.preset === a));
  }
  document.querySelectorAll('#mdPresets .chip').forEach((chip) => {
    chip.addEventListener('click', () => {
      const p = PRESETS[chip.dataset.preset];
      if (!p) return;
      el.type.value = p.type; el.dir.value = p.dir; el.status.value = p.status;
      syncPresetChips();
      reload();
    });
  });

  el.more.addEventListener('click', () => loadPage(false));
  [el.status, el.type, el.dir, el.acct, el.emp, el.collapse].forEach((n) => n.addEventListener('change', () => { syncPresetChips(); reload(); }));
  el.fan.addEventListener('input', Fastt.debounce(() => {
    const v = (el.fan.value || '').trim();
    if (v.length === 1) return;                     // backend rejects <2 chars
    reload();
  }, 300));
  [el.min, el.max, el.qText].forEach((n) => n.addEventListener('input', Fastt.debounce(render, 200)));
  el.search.addEventListener('click', reload);
  el.reset.addEventListener('click', () => {
    const to = new Date();
    range.from = isoDay(new Date(to.getTime() - 29 * DAY));
    range.to = isoDay(to);
    paintRange();
    el.emp.value = ''; el.acct.value = Fastt.account() ? String(Fastt.account()) : '';
    el.fan.value = ''; el.min.value = ''; el.max.value = '';
    el.status.value = 'all'; el.type.value = 'all'; el.dir.value = 'any';
    el.collapse.checked = true; el.qText.value = '';
    syncPresetChips();
    reload();
  });

  // first icon = refresh (read-only), second = server CSV export of the FILTER SET
  const ibs = document.querySelectorAll('.result-head .ib');
  if (ibs[0]) {
    ibs[0].title = 'Reload the message log (read-only — messages no one)';
    ibs[0].addEventListener('click', reload);
  }
  if (ibs[1]) {
    ibs[1].title = 'Download every row matching these filters (server-streamed CSV)';
    ibs[1].addEventListener('click', () => {
      const p = params();
      delete p.limit;
      p.format = 'csv';
      const qs = Object.keys(p).filter((k) => p[k] !== undefined && p[k] !== null && p[k] !== '')
        .map((k) => encodeURIComponent(k) + '=' + encodeURIComponent(p[k])).join('&');
      const a = document.createElement('a');
      a.href = '/admin/paid-messages?' + qs;
      a.download = '';
      document.body.appendChild(a); a.click(); a.remove();
      Fastt.toast('CSV export requested — it reflects the filters, not just this page');
    });
  }

  el.banner.textContent =
    'Direct messages from the fastt message log (/admin/paid-messages) — outbound and inbound, priced and free. ' +
    'Mass broadcasts collapse into one “sent to N fans” summary row unless you untick the box or search a fan.';
  el.note.innerHTML =
    'GET /admin/paid-messages — keyset-paged on (sent_at, message_id) DESC, newest first, ' + PAGE + ' rows a page. ' +
    'Sender / Creator / Sent to / Purchased / Status / Direction / Collapse are real query params; ' +
    '<b>Price</b> and the <b>message-text search</b> have no server parameter, so they filter the rows already loaded ' +
    '(both are badged CLIENT-SIDE). The KPI tiles count exactly what is on screen — they are not a window total. ' +
    'The download button asks the relay for a CSV of the whole filter set, not just this page. ' +
    'The revenue band up top is the transactions ledger — a true window total, unaffected by these row filters.';

  syncPresetChips();
  await reload();
});
