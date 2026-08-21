/* ==== Auto Convo wiring — GET/PUT /admin/autoreply-config (autoreply automation) ==== */
Fastt.ready(async () => {
  const $ = Fastt.$;
  // The strip ships #st-run as a GREEN "Loading…" pill. Drop the green before the
  // first fetch so a relay error can never leave a healthy-looking status chip.
  { const st0 = document.getElementById('st-run'); if (st0) st0.classList.remove('ok'); }
  const acct = Fastt.account();
  if (!acct) return;

  // ---- helpers -------------------------------------------------
  var chipText = Fastt.chipText;
  const centsToDollarStr = (c) => String((Number(c) || 0) / 100);
  const dollarsToCents = (s) => {
    const n = parseFloat(String(s).replace(/[,$\s]/g, ''));
    return isFinite(n) && n >= 0 ? Math.round(n * 100) : 0;
  };
  const intOf = (s, fb) => {
    const n = parseInt(String(s).replace(/[,\s]/g, ''), 10);
    return isFinite(n) && n >= 0 ? n : fb;
  };
  function setRange(el, v) {
    v = Number(v) || 0;
    if (v > Number(el.max)) el.max = v;
    if (v < Number(el.min)) el.min = v;
    el.value = v;
    el.dispatchEvent(new Event('input', { bubbles: true })); // fx kit updates the .fx-rv label
  }

  // ---- state ---------------------------------------------------
  let cfg = {};        // full validated config we PUT back (server allowlist keys only)
  // autoreply-config `enabled` is only half the truth: the sweep is SCHEDULED by the
  // automation_rules row (kind=autoreply). Config on + rule disabled = nothing ever runs.
  let ruleRow = null;      // /admin/automation-rules row, {is_enabled, every_seconds, last_run}
  let ruleErr = false;     // the rules fetch failed — say "unknown", never "Running"

  function populate() {
    const cfgOn = !!cfg.enabled;
    const scheduled = !!(ruleRow && ruleRow.is_enabled);
    const sw = $('#sw-convo'), tc = $('#tc-convo');
    sw.classList.toggle('on', cfgOn);
    tc.classList.toggle('on', cfgOn);
    const stateEl = tc.querySelector('.fx-tc-state');
    stateEl.textContent = !cfgOn ? 'Off'
      : (scheduled ? 'Running' : (ruleErr ? 'Sweep unknown' : 'Not scheduled'));
    stateEl.style.color = (cfgOn && !scheduled) ? '#febc2e' : '';
    const st = $('#st-run');
    st.classList.remove('ok', 'warn');
    if (cfgOn && scheduled) { st.classList.add('ok'); chipText(st, 'Running'); }
    else if (cfgOn) {
      st.classList.add('warn');
      chipText(st, ruleErr ? 'On — sweep state unknown' : 'On — no sweep scheduled');
    } else chipText(st, 'Off');

    $('#f-life').value = centsToDollarStr(cfg.max_lifetime_spend_cents);
    $('#f-recent').value = centsToDollarStr(cfg.max_recent_spend_cents);
    $('#f-recent-days').value = cfg.recent_spend_days;
    $('#f-days-purch').value = cfg.min_days_since_purchase;
    $('#f-days-first').value = cfg.min_days_since_first_chat;
    setRange($('#r-stepin'), cfg.silence_min_minutes);
    setRange($('#r-stale'), cfg.silence_max_minutes);
    $('#f-max-nudges').value = cfg.max_nudges;
    $('#f-min-gap').value = cfg.min_gap_minutes;
    $('#ck-noinfo').classList.toggle('on', !!cfg.info_not_required);
    setRange($('#r-history'), cfg.last_n_messages);

    // quiet hours select — add the stored window as an option if it's not a preset
    const sq = $('#s-quiet');
    const qh = cfg.quiet_hours_json;
    const want = Array.isArray(qh) && qh.length === 2 ? qh[0] + ',' + qh[1] : '';
    if (want && !Array.from(sq.options).some((o) => o.value === want)) {
      const o = document.createElement('option');
      o.value = want;
      o.textContent = String(qh[0]).padStart(2, '0') + ':00 – ' + String(qh[1]).padStart(2, '0') + ':00';
      sq.appendChild(o);
    }
    sq.value = want;

    $('#kv-steps').innerHTML = 'steps in after <b>' + Number(cfg.silence_min_minutes) +
      ' min</b> · max <b>' + Number(cfg.max_nudges) + '</b> nudge'
      + (Number(cfg.max_nudges) === 1 ? '' : 's') + ' per waiting message';

    // ── prose derived from the live config (it used to hardcode 30 min–2 h,
    //    $2,000 lifetime and "$500 in the last 1 day", all of which were wrong
    //    for this creator and would go stale the moment a knob moved) ──
    const win = mins(cfg.silence_min_minutes) + ' – ' + mins(cfg.silence_max_minutes);
    $('#tc-desc').innerHTML = 'Keeps quiet fans talking. One friendly reply when a fan goes silent <b>'
      + Fastt.esc(win) + '</b> — covers everyone under the whale cutoff. Never mentions prices.';
    $('#stale-hint').textContent = 'past this the follow-up drip owns the fan · window ' + win;
    const days = Number(cfg.recent_spend_days) || 0;
    $('#qual-note').innerHTML =
      'Only fans under <b>' + usd(cfg.max_lifetime_spend_cents) + ' lifetime</b> qualify — '
      + 'anyone above belongs to the AI Chatter closer. A tighter recent gate also applies: <b>'
      + usd(cfg.max_recent_spend_cents) + '</b> in the last <b>' + days + ' day'
      + (days === 1 ? '' : 's') + '</b>. '
      + (Number(cfg.min_days_since_purchase) || Number(cfg.min_days_since_first_chat)
          ? 'Day-gates: ' + Number(cfg.min_days_since_purchase) + 'd since a purchase, '
            + Number(cfg.min_days_since_first_chat) + 'd since the first chat.'
          : 'Both day-gates sit at 0: she does not wait after a purchase or a first chat.');
    $('#skip-whale').innerHTML = 'He is over the ' + usd(cfg.max_lifetime_spend_cents)
      + ' spend cutoff<span class="ss">true whales go to AI Chatter</span>';
  }
  // $200000 → "$2,000" (fmtCents would print "$2000.00" in running prose).
  function usd(cents) {
    const d = (Number(cents) || 0) / 100;
    return '$' + d.toLocaleString('en-US', { minimumFractionDigits: d % 1 ? 2 : 0,
                                             maximumFractionDigits: 2 });
  }
  // 780 min reads as "13 h", 25 stays "25 min" — the card used to claim "2 h" while
  // the kv line right beneath it printed the true 25.
  function mins(m) {
    m = Number(m) || 0;
    if (m < 90) return m + ' min';
    const h = m / 60;
    return (Math.round(h * 10) / 10) + ' h';
  }

  async function load() {
    const out = await Fastt.get('/admin/autoreply-config');
    cfg = Object.assign({}, out.defaults, out.config);
    try { ruleRow = await Fastt.rule('autoreply'); ruleErr = false; }
    catch (e) { ruleRow = null; ruleErr = true; console.error(e); }
    populate();
  }

  let saving = false, dirty = false;
  async function save(part) {
    Object.assign(cfg, part);
    if (saving) { dirty = true; return; }
    saving = true;
    try {
      do {
        dirty = false;
        const resp = await Fastt.put('/admin/autoreply-config', { account_id: acct, config: cfg });
        cfg = Object.assign({}, cfg, resp.config);
      } while (dirty);
      Fastt.saved();
      populate();
    } catch (e) {
      Fastt.oops(e);
      try { await load(); } catch (_) {}
    } finally { saving = false; dirty = false; }
  }

  // ---- initial load -------------------------------------------
  await load();

  // Skip-bucket labels for the last-sweep funnel card — declared before the
  // status block that calls paintSweep() so it is out of the TDZ at call time.
  const SKIP_LABELS = [
    ['skipped_raced', 'Team or AI got there first'],
    ['skipped_hot_lead', 'Closer owned the moment'],
    ['skipped_spend', 'Over the spend cutoff'],
    ['skipped_cap', 'AI budget reached'],
    ['skipped_cooldown', 'Already nudged / cooling down'],
    ['skipped_restricted', 'Restricted or muted'],
    ['skipped_unreachable', 'Unreachable fan'],
    ['skipped_spam', 'Promo-spam guard'],
    ['skipped_locked', 'Locked by another send'],
    ['errors', 'Errors'],
  ];

  // status strip — real data from the autoreply rule (already fetched in load())
  {
    const lr = ruleRow && ruleRow.last_run;
    if (ruleErr) {
      chipText($('#st-nudges'), 'sweep stats unavailable');
      $('#kv-sweep').textContent = 'sweep stats unavailable';
    } else if (lr && lr.stats) {
      chipText($('#st-nudges'), (lr.stats.sent ?? 0) + ' nudged last sweep (' + (lr.stats.candidates ?? 0)
        + ' candidate' + ((lr.stats.candidates ?? 0) === 1 ? '' : 's') + ')');
      $('#kv-sweep').innerHTML = '<b>' + (lr.stats.sent ?? 0) + '</b> nudged · <b>' +
        (lr.stats.candidates ?? 0) + '</b> candidate' + ((lr.stats.candidates ?? 0) === 1 ? '' : 's') + ' last sweep';
    } else {
      chipText($('#st-nudges'), 'no sweeps yet');
      $('#kv-sweep').textContent = 'no sweep stats yet';
    }
    chipText($('#st-sweep'), ruleErr ? 'Last sweep —'
      : 'Last sweep ' + Fastt.fmtAgo(lr && lr.started_at));
    paintSweep(lr, ruleErr);
  }

  // ---- Last-sweep funnel card (right column) -------------------
  // Every count below is a real field on the autoreply rule's last_run.stats.
  function paintSweep(lr, err) {
    const body = $('#sweep-body'), when = $('#sweep-when');
    const hEl = document.querySelector('#card-sweep .fx-card-h');
    if (err) {
      when.textContent = '';
      body.innerHTML = '<div class="sw-empty">Sweep stats are unavailable right now — the '
        + 'automation-rules service did not answer. Reload to try again.</div>';
      Fastt.staticBadge(hEl, 'UNAVAILABLE');
      return;
    }
    const s = lr && lr.stats;
    if (!s) {
      when.textContent = '';
      body.innerHTML = '<div class="sw-empty">No sweeps have run yet. Turn Auto Convo on and '
        + 'schedule the sweep on the Automations page — the first run\'s breakdown lands here.</div>';
      Fastt.staticBadge(hEl, 'NO SWEEPS');
      return;
    }
    when.textContent = Fastt.fmtAgo(lr.started_at);
    const cand = Number(s.candidates) || 0;
    const sent = Number(s.sent) || 0;
    const rows = SKIP_LABELS
      .map(([k, lbl]) => [lbl, Number(s[k]) || 0])
      .filter(([, v]) => v > 0);
    let html = '<div class="sw-tiles">'
      + '<div class="sw-tile"><div class="n g">' + sent + '</div><div class="l">Nudged</div></div>'
      + '<div class="sw-tile"><div class="n">' + cand + '</div><div class="l">Matched filters</div></div>'
      + '</div>';
    if (rows.length) {
      html += '<div class="sw-break"><div class="h">Stood down on</div>'
        + rows.map(([lbl, v]) =>
            '<div class="sw-row"><i></i>' + Fastt.esc(lbl) + '<span class="v">' + v + '</span></div>').join('')
        + '</div>';
    } else if (cand === 0) {
      html += '<div class="sw-break"><div class="sw-empty">No fan matched the filters this sweep — '
        + 'nobody was waiting long enough, or everyone was above the spend cutoff.</div></div>';
    } else {
      html += '<div class="sw-break"><div class="sw-empty">Every matched fan got a nudge — '
        + 'no stand-downs this sweep.</div></div>';
    }
    body.innerHTML = html;
    Fastt.liveBadge(hEl);
  }
  try {
    const brain = await Fastt.get('/admin/account-config');
    chipText($('#st-model'), brain.config.model || 'model not set');
    chipText($('#st-cost'), 'AI budget ' + Fastt.fmtCents(brain.config.daily_cost_cap_cents) + '/day');
  } catch (e) { console.error(e); }

  // ---- Sound human — GET/PUT /admin/style-config ----------------
  // Every key below ships in style-config's config+defaults today; the PUT is a
  // partial merge server-side, so a patch never has to resend the whole blob.
  let style = {};          // defaults ⊕ stored, the effective view the engines read
  const CAT = { 'f-cat-skip': 'cat_sticker_skip_pct', 'f-cat-solo': 'cat_sticker_solo_pct',
                'f-cat-gap': 'cat_sticker_gap_min' };
  function paintStyle() {
    Fastt.$$('.fx-check[data-style]').forEach((el) =>
      el.classList.toggle('on', !!style[el.dataset.style]));
    // The Auto Convo master mirrors ai-chatter.html: on when any of this page's
    // three Auto-Convo style knobs is on.
    $('#sh-master').classList.toggle('on',
      !!(style.autoreply || style.typos_autoreply || style.nonnative_autoreply));
    const catOn = style.cat_stickers !== false;
    $('#cat-knobs').classList.toggle('off', !catOn);
    Object.entries(CAT).forEach(([id, key]) => {
      const el = document.getElementById(id);
      if (!el) return;
      if (document.activeElement !== el) el.value = Number(style[key]) || 0;
      el.disabled = !catOn;
    });
  }
  async function saveStyle(patch) {
    Object.assign(style, patch);
    paintStyle();
    try {
      const resp = await Fastt.put('/admin/style-config', { account_id: acct, config: patch });
      style = Object.assign({}, style, (resp && resp.config) || {});
      Fastt.saved();
    } catch (e) { Fastt.oops(e); }
    paintStyle();
  }
  try {
    const sc = await Fastt.get('/admin/style-config');
    style = Object.assign({}, sc.defaults, sc.config);
    paintStyle();
    Fastt.liveBadge($('#card-human .fx-card-h'));
  } catch (e) {
    Fastt.staticBadge($('#card-human .fx-card-h'), 'STYLE CONFIG UNREACHABLE');
    console.error(e);
  }
  document.addEventListener('click', (e) => {
    const sk = e.target.closest('.fx-check[data-style]');   // fx kit already flipped it
    if (sk) { saveStyle({ [sk.dataset.style]: sk.classList.contains('on') }); return; }
    if (e.target.closest('#sh-master')) {
      const on = $('#sh-master').classList.contains('on');
      saveStyle({ autoreply: on, typos_autoreply: on, nonnative_autoreply: on });
    }
  });
  Object.entries(CAT).forEach(([id, key]) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.addEventListener('change', () => {
      const n = parseFloat(String(el.value).replace(/[^0-9.]/g, ''));
      if (!isFinite(n) || n < 0) { paintStyle(); return; }
      const max = key === 'cat_sticker_gap_min' ? 10080 : 100;
      saveStyle({ [key]: Math.min(n, max) });
    });
  });

  // ---- badges --------------------------------------------------
  Fastt.liveBadge($('#tc-convo .fx-tc-title'));
  Fastt.staticBadge($('#h-nudge-demo'), 'SAMPLE');

  // ---- persistence (listeners registered AFTER the fx kit so we read post-toggle state)
  document.addEventListener('click', (e) => {
    if (e.target.closest('#sw-convo')) {
      const on = $('#sw-convo').classList.contains('on');
      // Arming the engine is the one click here that can put real DMs in front of
      // fans (on any DB where the autoreply rule row is enabled). Confirm it, the
      // same way the Re-engage page confirms its master switch.
      if (on) {
        const scheduled = !!(ruleRow && ruleRow.is_enabled);
        const every = ruleRow && ruleRow.every_seconds
          ? ' every ' + Math.max(1, Math.round(ruleRow.every_seconds / 60)) + ' min' : '';
        const msg = scheduled
          ? 'Turn Auto Convo ON?\n\nThe autoreply sweep runs' + every + ' for this creator — '
            + 'it will send real DMs to fans who match the filters below.'
          : 'Turn Auto Convo ON?\n\nThis saves enabled:true. The autoreply automation rule is '
            + 'currently disabled for this creator, so no sweep will fire until you enable it '
            + 'on the Automations page — but it will start sending as soon as you do.';
        if (!confirm(msg)) { populate(); return; }   // snap the switch back
      }
      save({ enabled: on });
      return;
    }
    const ck = e.target.closest('#ck-noinfo');
    if (ck) save({ info_not_required: ck.classList.contains('on') });
  });

  $('#f-life').addEventListener('change', (e) => save({ max_lifetime_spend_cents: dollarsToCents(e.target.value) }));
  $('#f-recent').addEventListener('change', (e) => save({ max_recent_spend_cents: dollarsToCents(e.target.value) }));
  $('#f-recent-days').addEventListener('change', (e) => save({ recent_spend_days: intOf(e.target.value, 1) }));
  $('#f-days-purch').addEventListener('change', (e) => save({ min_days_since_purchase: intOf(e.target.value, 0) }));
  $('#f-days-first').addEventListener('change', (e) => save({ min_days_since_first_chat: intOf(e.target.value, 0) }));
  $('#f-max-nudges').addEventListener('change', (e) => save({ max_nudges: intOf(e.target.value, 1) }));
  $('#f-min-gap').addEventListener('change', (e) => save({ min_gap_minutes: intOf(e.target.value, 5) }));
  $('#r-stepin').addEventListener('change', (e) => save({ silence_min_minutes: intOf(e.target.value, 30) }));
  $('#r-stale').addEventListener('change', (e) => save({ silence_max_minutes: intOf(e.target.value, 120) }));
  $('#r-history').addEventListener('change', (e) => save({ last_n_messages: intOf(e.target.value, 16) }));
  $('#s-quiet').addEventListener('change', (e) => {
    const v = e.target.value;
    save({ quiet_hours_json: v ? v.split(',').map(Number) : null });
  });
});
