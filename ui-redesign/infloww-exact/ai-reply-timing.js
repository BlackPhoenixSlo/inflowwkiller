Fastt.ready(async () => {
  // #rt-st-2 shipped "Live on 2 of 10 accounts" — an invented CROSS-ACCOUNT claim
  // that only became truthful once render() ran. Blank it before the first fetch so
  // a relay error (or no creator picked) can never leave fabricated fleet numbers up.
  (function neutralizeFleetPill() {
    const t = document.getElementById('rt-st-2');
    if (t) t.textContent = '—';
    const pill = document.getElementById('rt-p2');
    if (pill) pill.className = 'fx-st';
  })();

  // Section-header persona chip: inert chrome that shipped a hardcoded "Aria".
  // Name the creator this page actually reads/writes, and badge it so it is not
  // mistaken for a working creator switcher (the sidebar .creator block is that).
  (function fixPersonaChip() {
    const lbl = document.getElementById('hdr-persona');
    const drop = document.getElementById('hdr-persona-drop');
    if (!lbl) return;
    const row = Fastt.accountRow();
    lbl.textContent = row ? (row.nickname || String(row.id)) : (Fastt.account() || '—');
    if (drop) Fastt.staticBadge(drop, 'DISPLAY ONLY');
  })();

  if (!Fastt.account()) return;

  const $ = Fastt.$, $$ = Fastt.$$;
  const cfgResp = await Fastt.get('/admin/ai-chatter-config');
  let stored = cfgResp.config || {};       // sparse blob — PUT replaces it whole
  const defs = cfgResp.defaults || {};
  let view = cfgResp;                       // rhythm view: tz + derived/effective windows
  const eff = () => ({ ...defs, ...stored });

  async function saveCfg(patch, tz) {
    try {
      const body = { account_id: Fastt.account(), config: { ...stored, ...patch } };
      if (tz !== undefined) body.timezone = tz;   // "" clears the column back to NULL
      const resp = await Fastt.put('/admin/ai-chatter-config', body);
      stored = resp.config || {};
      view = resp;                          // PUT returns a fresh rhythm view
      Fastt.saved();
    } catch (e) { Fastt.oops(e); }
    render();
  }

  const setTxt = (id, t) => { const el = document.getElementById(id); if (el) el.textContent = t; };
  const winTxt = (w) => (Array.isArray(w) && w.length === 2) ? w[0] + '–' + w[1] : '—';

  function render() {
    const c = eff();
    const derived = view.derived_sleep_window || [];
    const effective = view.effective_sleep_window || derived;
    const override = Array.isArray(c.sleep_window) && c.sleep_window.length === 2
      ? c.sleep_window : null;

    // status strip
    setTxt('rt-st-1', c.rhythm_enabled ? 'Running on this account' : 'Off on this account');
    const p1 = $('#rt-p1'); if (p1) p1.className = 'fx-st ' + (c.rhythm_enabled ? 'ok' : '');
    setTxt('rt-st-2', view.timezone ? 'Timezone ' + view.timezone
      : (view.tz_offset_minutes != null
          ? 'UTC offset ' + (view.tz_offset_minutes / 60) + 'h'
          : 'No timezone set — sleep stays off'));
    const p2 = $('#rt-p2'); if (p2) p2.className = 'fx-st ' + (view.timezone || view.tz_offset_minutes != null ? 'ok' : 'warn');
    setTxt('rt-st-3', !c.rhythm_enabled ? 'Rhythm off — replies land in seconds'
      : (c.rhythm_no_sleep ? 'Sleep: disabled — delays only'
                           : 'Sleeps ' + winTxt(effective) + (override ? ' (manual)' : ' (auto)')));

    // master + presets
    const master = $('#rt-master');
    if (master) master.classList.toggle('on', !!c.rhythm_enabled);
    const tc = master && master.closest('.fx-togglecard');
    if (tc) { tc.classList.toggle('on', !!c.rhythm_enabled);
      const st = tc.querySelector('.fx-tc-state'); if (st) st.textContent = c.rhythm_enabled ? 'Running' : 'Off'; }
    const preset = !c.rhythm_enabled ? 'instant' : (c.rhythm_no_sleep ? 'nosleep' : 'realistic');
    $$('#rt-presets .fx-preset').forEach(p =>
      p.classList.toggle('sel', p.dataset.preset === preset));

    // derived-window texts + the chart band (the band position is real; the bars are not)
    setTxt('rt-derived-1', winTxt(derived));
    setTxt('rt-derived-2', winTxt(derived));
    setTxt('rt-derived-3', winTxt(derived));
    // The preset used to promise the window was "read off her own sends". It is
    // only derived when her histogram HAS a 3h+ quiet block — otherwise the engine
    // falls back to DEFAULT_SLEEP, and saying "read off her own sends" would be a lie.
    const dflt = view.default_sleep_window || [];
    const derivedIsDefault = derived.length === 2 && dflt.length === 2
      && derived[0] === dflt[0] && derived[1] === dflt[1];
    setTxt('rt-derived-src', derivedIsDefault
      ? '— the house default (her sends show no 3 h+ quiet block)'
      : '— read off her own sends');
    // "Your current setup (the graded vault · Ava)" was a hardcoded cross-account claim.
    const row = Fastt.accountRow();
    setTxt('rt-nosleep-cur', preset === 'nosleep'
      ? 'Current setup for ' + (row ? (row.nickname || row.id) : Fastt.account()) + '.' : '');
    setTxt('rt-sleep-state', c.rhythm_no_sleep ? 'Sleep is currently disabled'
                                               : 'Sleep is currently enabled');
    const band = $('.rt-sleepband');
    if (band && effective.length === 2) {
      const h = (s) => { const m = /^(\d{1,2}):(\d{2})$/.exec(s || '');
        return m ? (Number(m[1]) + Number(m[2]) / 60) : null; };
      const a = h(effective[0]), b = h(effective[1]);
      if (a != null && b != null) {
        band.style.left = (a / 24 * 100) + '%';
        band.style.width = (((b - a + 24) % 24) / 24 * 100) + '%';
        const lbl = band.querySelector('i');
        if (lbl) lbl.textContent = (c.rhythm_no_sleep ? 'would sleep ' : 'sleeps ') + winTxt(effective);
      }
    }

    // advanced: sleep window override + timezone
    const from = $('#rt-sleep-from'), until = $('#rt-sleep-until');
    if (from && document.activeElement !== from) {
      from.value = override ? override[0] : '';
      from.placeholder = 'auto — ' + (derived[0] || '');
    }
    if (until && document.activeElement !== until) {
      until.value = override ? override[1] : '';
      until.placeholder = 'auto — ' + (derived[1] || '');
    }
    const tz = $('#rt-tz');
    if (tz && document.activeElement !== tz) {
      const cur = view.timezone || '';
      if (cur && !Array.from(tz.options).some(o => o.value === cur)) {
        const o = document.createElement('option');
        o.value = cur; o.textContent = cur;
        tz.appendChild(o);
      }
      tz.value = cur;
    }
    const stall = $('[data-cfg="filming_stall_enabled"]');
    if (stall) stall.classList.toggle('on', !!c.filming_stall_enabled);
    renderCad();
  }

  // ── Cadence — reply caps per burst, chosen by the fan's live signal ──────────
  // All keys are validated + persisted by scripts_api (_validate_cfg): cadence_enabled,
  // msg_limits_by_signal (nested, complete dict), msg_limits_by_spend ([{days,min_cents,cap}]),
  // and the minute knobs in _INT_KNOBS. 0 on a tier cap = unlimited (see _cadence_gate).
  const CAD_TIERS = ['pic_sent', 'buying_signal', 'baseline', 'no_signal'];
  const clampInt = (v, lo, hi, d) => {
    let n = parseInt(v, 10); if (isNaN(n)) n = d;
    return Math.max(lo, Math.min(hi, n));
  };
  function collectSignal() {
    const o = {};
    CAD_TIERS.forEach(t => {
      const el = document.getElementById('cad-cap-' + t);
      o[t] = clampInt(el && el.value, 0, 500, 0);
    });
    return o;
  }
  function collectSpendRules() {
    return $$('#cad-spend-rows .cad-srow').map(row => ({
      days: clampInt(row.querySelector('[data-f="days"]').value, 0, 365, 0),
      min_cents: Math.max(0, Math.round((parseFloat(row.querySelector('[data-f="min"]').value) || 0) * 100)),
      cap: clampInt(row.querySelector('[data-f="cap"]').value, 0, 500, 0),
    }));
  }
  function spendRowHtml(r) {
    const d = r.days || 0, m = ((r.min_cents || 0) / 100), cap = r.cap || 0;
    return '<div class="cad-srow">'
      + '<div class="fx-field"><label>Paid in last (days)</label><input class="fx-input" data-f="days" inputmode="numeric" value="' + d + '"></div>'
      + '<div class="fx-field"><label>At least ($)</label><input class="fx-input" data-f="min" inputmode="decimal" value="' + m + '"></div>'
      + '<div class="fx-field"><label>Lift cap to (replies)</label><input class="fx-input" data-f="cap" inputmode="numeric" value="' + cap + '"></div>'
      + '<button class="cad-del" type="button" data-act="del-spend" title="Remove rule">✕</button></div>';
  }
  function renderSpendRules(rules) {
    const host = $('#cad-spend-rows'); if (!host) return;
    if (host.contains(document.activeElement)) return;   // don't clobber a field mid-edit
    host.innerHTML = rules.length
      ? rules.map(spendRowHtml).join('')
      : '<div class="cad-srow-empty">No spend rules — the signal caps apply exactly.</div>';
  }
  function renderCad() {
    const c = eff();
    const on = c.cadence_enabled !== false;             // defaults ON
    const master = $('#cad-master');
    if (master) master.classList.toggle('on', on);
    const card = $('#cad-card');
    if (card) {
      card.classList.toggle('on', on);
      const st = card.querySelector('.fx-tc-state'); if (st) st.textContent = on ? 'Running' : 'Off';
    }
    const sig = c.msg_limits_by_signal || {};
    CAD_TIERS.forEach(t => {
      const el = document.getElementById('cad-cap-' + t);
      if (el && document.activeElement !== el) el.value = (sig[t] != null ? sig[t] : 0);
    });
    const rules = Array.isArray(c.msg_limits_by_spend) ? c.msg_limits_by_spend : [];
    const floor = rules.reduce((m, r) => Math.max(m, Number(r.cap) || 0), 0);
    setTxt('cad-eff-floor', floor
      ? 'A proven spender never drops below ' + floor + ' — the real leash is the higher of his signal and his spend.'
      : '');
    [['cad-gap', 'session_gap_minutes'], ['cad-postbuy', 'post_purchase_minutes'],
     ['cad-expiry', 'offer_expiry_minutes']].forEach(([id, k]) => {
      const el = document.getElementById(id);
      if (el && document.activeElement !== el) el.value = (c[k] != null ? c[k] : '');
    });
    setTxt('rt-st-5', on ? 'Cadence on — she stops when he goes quiet'
                         : 'Cadence off — replies never stop');
    const p5 = $('#rt-p5'); if (p5) p5.className = 'fx-st ' + (on ? 'ok' : 'warn');
    const ladder = $('#cad-ladder'); if (ladder) ladder.style.opacity = on ? '' : '.45';
    renderSpendRules(rules);
  }

  render();

  Fastt.liveBadge($('.fx-tc-title'));
  Fastt.staticBadge($('#rt-delay-h'), 'STATIC');
  Fastt.staticBadge($('#rt-gap-explain'), 'STATIC');
  Fastt.staticBadge($('#rt-applies-h'), 'STATIC');
  const advHead = $('.fx-adv-head'); if (advHead) Fastt.liveBadge(advHead);
  { const cadTitle = $('#cad-card .fx-tc-title'); if (cadTitle) Fastt.liveBadge(cadTitle); }

  // Cadence writes: master toggle, per-tier caps, spend-rule add/remove, minute knobs.
  document.addEventListener('click', (e) => {
    const m = e.target.closest('#cad-master');
    if (m) { saveCfg({ cadence_enabled: m.classList.contains('on') }); return; }
    const add = e.target.closest('[data-act="add-spend"]');
    if (add) {
      const rules = collectSpendRules();
      rules.push({ days: 30, min_cents: 1000, cap: 10 });
      saveCfg({ msg_limits_by_spend: rules });
      return;
    }
    const del = e.target.closest('[data-act="del-spend"]');
    if (del) {
      const row = del.closest('.cad-srow'); if (row) row.remove();
      saveCfg({ msg_limits_by_spend: collectSpendRules() });
      return;
    }
  });
  document.addEventListener('change', (e) => {
    const t = e.target;
    if (t.id && t.id.indexOf('cad-cap-') === 0) { saveCfg({ msg_limits_by_signal: collectSignal() }); return; }
    if (t.id === 'cad-gap') { saveCfg({ session_gap_minutes: clampInt(t.value, 5, 1440, 60) }); return; }
    if (t.id === 'cad-postbuy') { saveCfg({ post_purchase_minutes: clampInt(t.value, 0, 1440, 25) }); return; }
    if (t.id === 'cad-expiry') { saveCfg({ offer_expiry_minutes: clampInt(t.value, 0, 10080, 120) }); return; }
    if (t.closest && t.closest('#cad-spend-rows .cad-srow')) { saveCfg({ msg_limits_by_spend: collectSpendRules() }); return; }
  });

  // fx-js has already flipped the visual state when these run.
  document.addEventListener('click', (e) => {
    const sw = e.target.closest('#rt-master');
    if (sw) { saveCfg({ rhythm_enabled: sw.classList.contains('on') }); return; }
    const pr = e.target.closest('#rt-presets .fx-preset');
    if (pr) {
      const p = pr.dataset.preset;
      if (p === 'instant') saveCfg({ rhythm_enabled: false });
      else if (p === 'realistic') saveCfg({ rhythm_enabled: true, rhythm_no_sleep: false });
      else saveCfg({ rhythm_enabled: true, rhythm_no_sleep: true });
      return;
    }
    const ck = e.target.closest('.fx-check[data-cfg]');
    if (ck) { saveCfg({ [ck.dataset.cfg]: ck.classList.contains('on') }); return; }
  });

  // ── the two charts, built from her real message log ────────────────────────
  // GET /admin/paid-messages?type=all&direction=out|in — with type=all it returns
  // EVERY message, not only paid ones. Keyset pagination is
  // (next_before_sent_at, next_before_id) → (before_sent_at, before_id); limit caps
  // at 100, so a wide window needs many pages. We walk up to PAGE_CAP pages per
  // direction and then LABEL THE WINDOW WE ACTUALLY COVERED rather than claiming 30d.
  // sent_at is a tz-NAIVE UTC stamp → Fastt.parseUtc, and the hour-of-day chart is
  // rendered in CREATOR-LOCAL hours (local = utc + tz_offset_minutes, the exact
  // conversion automations/rhythm.local_now uses) so the sleep band lines up.
  const PAGE_CAP = 30;                    // 3,000 rows/direction ≈ a week of traffic
  const DAYS_ASKED = 30;
  const BUCKETS = [
    { s: 30, label: '≤30s' }, { s: 60, label: '1m' }, { s: 120, label: '2m' },
    { s: 300, label: '5m' }, { s: 900, label: '15m' }, { s: 3600, label: '1h' },
    { s: 14400, label: '4h' }, { s: 28800, label: '8h' }, { s: Infinity, label: '8h+' },
  ];
  const isoDay = (d) => d.toISOString().slice(0, 10);
  const fmtDur = (s) => {
    s = Math.round(s);
    if (s < 60) return s + ' s';
    if (s < 3600) return Math.floor(s / 60) + ' m ' + (s % 60) + ' s';
    if (s < 86400) return (Math.round(s / 360) / 10) + ' h';
    return (Math.round(s / 8640) / 10) + ' d';
  };
  async function fetchDirection(dir, from, to) {
    const rows = [];
    let before = null;
    for (let page = 0; page < PAGE_CAP; page++) {
      const params = { type: 'all', direction: dir, from, to, limit: 100 };
      if (before) { params.before_id = before.id; params.before_sent_at = before.at; }
      const out = await Fastt.get('/admin/paid-messages', params);
      const batch = out.rows || [];
      rows.push.apply(rows, batch);
      if (batch.length < 100 || !out.next_before_id) break;
      before = { id: out.next_before_id, at: out.next_before_sent_at };
    }
    return rows;
  }
  function emptyChart(el, msg) {
    const band = el.querySelector('.rt-sleepband');
    el.classList.add('empty');
    el.innerHTML = '<div class="msg">' + Fastt.esc(msg) + '</div>';
    if (band) el.appendChild(band);
  }
  function drawBars(el, counts, titles) {
    const band = el.querySelector('.rt-sleepband');
    const peak = Math.max.apply(null, counts.concat([0]));
    el.classList.remove('empty');
    el.innerHTML = counts.map((n, i) =>
      '<div class="b" style="height:' + (peak ? Math.max(2, Math.round(n / peak * 100)) : 0)
      + '%" title="' + Fastt.esc(titles[i]) + '"></div>').join('');
    if (band) el.appendChild(band);
  }
  async function buildCharts() {
    const to = isoDay(new Date(Date.now() + 864e5));
    const from = isoDay(new Date(Date.now() - DAYS_ASKED * 864e5));
    let outRows, inRows;
    try {
      [outRows, inRows] = await Promise.all([
        fetchDirection('out', from, to), fetchDirection('in', from, to),
      ]);
    } catch (e) {
      console.error(e);
      Fastt.staticBadge($('#rt-chart-h'), 'MESSAGE LOG UNREACHABLE');
      setTxt('rt-sample', 'message log unavailable');
      emptyChart($('#rt-chart-a'), 'The relay refused /admin/paid-messages, so there is nothing real to plot.');
      emptyChart($('#rt-chart-b'), 'The relay refused /admin/paid-messages, so there is nothing real to plot.');
      return;
    }
    // Mass-broadcast summary rows are one synthetic row for N fans — never a 1:1 reply.
    const outs = outRows.filter(r => !r.is_mass_summary && r.sent_at);
    const ins = inRows.filter(r => r.sent_at);
    if (!outs.length) {
      Fastt.staticBadge($('#rt-chart-h'), 'NO MESSAGES YET');
      setTxt('rt-sample', 'no outbound messages in the last ' + DAYS_ASKED + ' days');
      emptyChart($('#rt-chart-a'), 'No outbound messages on this creator in the last '
        + DAYS_ASKED + ' days — nothing to measure yet.');
      emptyChart($('#rt-chart-b'), 'No outbound messages on this creator in the last '
        + DAYS_ASKED + ' days — nothing to measure yet.');
      return;
    }
    const ms = (r) => { const d = Fastt.parseUtc(r.sent_at); return d ? d.getTime() : null; };
    const oldestOut = Math.min.apply(null, outs.map(ms).filter(Boolean));
    const oldestIn = ins.length ? Math.min.apply(null, ins.map(ms).filter(Boolean)) : oldestOut;
    // Pair only inside the window BOTH directions cover, or a fan's inbound would
    // look unanswered simply because his reply fell off the outbound page walk.
    const pairFrom = Math.max(oldestOut, oldestIn);
    const covered = Math.max(1, Math.round((Date.now() - oldestOut) / 864e5));
    const capped = outs.length >= PAGE_CAP * 100 || ins.length >= PAGE_CAP * 100;

    // ── Chart A — inbound → her next outbound, per fan ──
    const ev = new Map();
    const push = (fid, t, kind) => {
      if (t == null) return;
      if (!ev.has(fid)) ev.set(fid, []);
      ev.get(fid).push([t, kind]);
    };
    ins.forEach(r => { const t = ms(r); if (t >= pairFrom) push(r.fan_id, t, 0); });
    outs.forEach(r => { const t = ms(r); if (t >= pairFrom) push(r.fan_id, t, 1); });
    const lat = [];
    ev.forEach((list) => {
      list.sort((a, b) => a[0] - b[0] || a[1] - b[1]);
      let pending = null;
      for (const [t, kind] of list) {
        if (kind === 0) { if (pending === null) pending = t; }   // his FIRST unanswered msg
        else if (pending !== null) { lat.push((t - pending) / 1000); pending = null; }
      }
    });
    const bins = BUCKETS.map(() => 0);
    lat.forEach((s) => {
      for (let i = 0; i < BUCKETS.length; i++) {
        if (s <= BUCKETS[i].s) { bins[i]++; break; }
      }
    });
    if (!lat.length) {
      emptyChart($('#rt-chart-a'), 'No fan message in this window got a reply, so there is no '
        + 'latency curve to draw yet.');
      setTxt('rt-median', 'no pairs');
      $('#rt-x-a').innerHTML = '';
    } else {
      drawBars($('#rt-chart-a'), bins,
        bins.map((n, i) => BUCKETS[i].label + ' — ' + Fastt.fmtInt(n) + ' repl'
          + (n === 1 ? 'y' : 'ies') + ' (' + Math.round(n / lat.length * 100) + '%)'));
      $('#rt-x-a').innerHTML = BUCKETS.map(b => '<span>' + b.label + '</span>').join('');
      const sorted = lat.slice().sort((a, b) => a - b);
      const median = sorted[Math.floor(sorted.length / 2)];
      setTxt('rt-median', 'median ' + fmtDur(median));
      // Mark the bucket the median falls in — the same "read the shape at a glance"
      // job the sleep band does on the hour chart.
      let mi = BUCKETS.findIndex(b => median <= b.s);
      if (mi < 0) mi = BUCKETS.length - 1;
      const mark = document.createElement('div');
      mark.className = 'rt-medmark';
      mark.style.left = (mi / BUCKETS.length * 100) + '%';
      mark.style.width = (1 / BUCKETS.length * 100) + '%';
      mark.innerHTML = '<i>median ' + Fastt.esc(fmtDur(median)) + '</i>';
      $('#rt-chart-a').appendChild(mark);
      const fast = Math.round(bins[0] / lat.length * 1000) / 10;
      $('#rt-note-a').innerHTML = Fastt.fmtInt(lat.length) + ' fan message'
        + (lat.length === 1 ? '' : 's') + ' answered · <b>' + fast + '%</b> inside 30 s'
        + ' · slowest bucket holds ' + Fastt.fmtInt(bins[8]) + '.';
    }

    // ── Chart B — her outbound by hour of day, creator-local ──
    const offMin = view.tz_offset_minutes;
    const hours = new Array(24).fill(0);
    outs.forEach((r) => {
      const t = ms(r); if (t == null) return;
      const d = new Date(t + (offMin || 0) * 60000);
      hours[d.getUTCHours()]++;
    });
    drawBars($('#rt-chart-b'), hours, hours.map((n, h) =>
      String(h).padStart(2, '0') + ':00 — ' + Fastt.fmtInt(n) + ' outbound'));
    $('#rt-x-b').innerHTML = hours.map((_, h) =>
      '<span' + (h === 0 ? ' style="text-align:left"' : (h === 23 ? ' style="text-align:right"' : ''))
      + '>' + (h % 6 === 0 || h === 23 ? String(h).padStart(2, '0') + ':00' : '') + '</span>').join('');
    const peakH = hours.indexOf(Math.max.apply(null, hours));
    setTxt('rt-online-v', offMin == null ? 'UTC hours — no timezone set'
      : 'creator-local hours (UTC' + (offMin < 0 ? '−' : '+')
        + String(Math.abs(offMin / 60)).padStart(2, '0') + ':00)');
    const derived = view.derived_sleep_window || [];
    const def = view.default_sleep_window || [];
    const isDefault = derived.length === 2 && def.length === 2
      && derived[0] === def[0] && derived[1] === def[1];
    $('#rt-note-b').innerHTML = 'Busiest hour <b>' + String(peakH).padStart(2, '0') + ':00</b>'
      + ' · ' + Fastt.fmtInt(outs.length) + ' outbound message' + (outs.length === 1 ? '' : 's')
      + (offMin == null
          ? '. No timezone on the account, so these are UTC hours and auto-sleep stays off.'
          : '.')
      + (isDefault
          ? ' The shaded window is the <b>house default</b> — her own sends show no 3 h+ quiet '
            + 'block, so the engine falls back rather than inventing one.'
          : ' The shaded window is derived from her own sends.');

    setTxt('rt-sample', 'last ' + covered + ' day' + (covered === 1 ? '' : 's') + ' · '
      + Fastt.fmtInt(lat.length) + ' real 1:1 replies'
      + (capped ? ' (newest ' + Fastt.fmtInt(outs.length + ins.length) + ' messages sampled)' : ''));
    Fastt.liveBadge($('#rt-chart-h'));
  }
  buildCharts();

  const HHMM = /^\s*(\d{1,2}):(\d{2})\s*$/;
  function saveSleepWindow() {
    const a = $('#rt-sleep-from').value.trim();
    const b = $('#rt-sleep-until').value.trim();
    if (!a && !b) { saveCfg({ sleep_window: null }); return; }   // back to auto
    if (!HHMM.test(a) || !HHMM.test(b)) {
      Fastt.toast('Enter both times as HH:MM (or clear both for auto)', 'err');
      return;
    }
    saveCfg({ sleep_window: [a, b] });
  }
  document.addEventListener('change', (e) => {
    if (e.target.id === 'rt-sleep-from' || e.target.id === 'rt-sleep-until') { saveSleepWindow(); return; }
    if (e.target.id === 'rt-tz') { saveCfg({}, e.target.value); return; }
  });
});
