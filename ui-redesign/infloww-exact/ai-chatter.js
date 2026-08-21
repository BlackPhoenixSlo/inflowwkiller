Fastt.ready(async () => {
  // Section-header persona chip: cosmetic chrome with no handler that shipped
  // hardcoded "Aria". Name the creator the page is actually reading/writing and
  // badge it so nobody mistakes it for a second (working) creator switcher.
  (function fixPersonaChip() {
    const lbl = document.getElementById('hdr-persona');
    const drop = document.getElementById('hdr-persona-drop');
    if (!lbl) return;
    const row = Fastt.accountRow();
    lbl.textContent = row ? (row.nickname || String(row.id)) : (Fastt.account() || '—');
    if (drop) {
      // This chip only DISPLAYS the active creator — it's not the switcher.
      // The real one is the creator card in the sidebar (Fastt.mountAccountPicker).
      Fastt.staticBadge(drop, 'NOT A SWITCHER');
      drop.style.cursor = 'default';
      drop.title = 'Shows the active creator. To switch creators, click the creator card at the top of the sidebar.';
    }
  })();

  if (!Fastt.account()) return;              // no creator picked — placeholders stay

  const $ = Fastt.$, $$ = Fastt.$$;
  const [cfgResp, styleResp, acctResp, costResp] = await Promise.all([
    Fastt.get('/admin/ai-chatter-config'),
    Fastt.get('/admin/style-config'),
    Fastt.get('/admin/account-config'),
    Fastt.get('/admin/stats/grok-cost').catch(() => null),
  ]);
  let stored = cfgResp.config || {};         // the SPARSE stored blob (PUT replaces it whole)
  const defs = cfgResp.defaults || {};
  let style = styleResp.config || {};
  let acct = acctResp.config || {};
  const eff = () => ({ ...defs, ...stored });

  const MODEL_LABELS = {
    'deepseek-v4-flash': 'DeepSeek V4 Flash', 'deepseek-v4-pro': 'DeepSeek V4 Pro',
    'grok-4-1-fast-non-reasoning': 'Grok 4.1 Fast',
    'qwen3-vl-30b': 'Qwen3-VL 30B', 'qwen3-vl-235b': 'Qwen3-VL 235B',
  };
  const modelLabel = (m) => MODEL_LABELS[m] || m || '—';

  async function saveCfg(patch) {
    try {
      const resp = await Fastt.put('/admin/ai-chatter-config',
        { account_id: Fastt.account(), config: { ...stored, ...patch } });
      stored = resp.config || {};
      Fastt.saved();
    } catch (e) { Fastt.oops(e); }
    render();
  }
  async function saveStyle(patch) {
    try {
      const resp = await Fastt.put('/admin/style-config',
        { account_id: Fastt.account(), config: patch });   // server merges partials
      style = resp.config || style;
      Fastt.saved();
    } catch (e) { Fastt.oops(e); }
    render();
  }
  async function saveAcct(patch) {
    try {
      const resp = await Fastt.put('/admin/account-config',
        { account_id: Fastt.account(), config: { ...acct, ...patch } });  // whole-row PUT
      acct = (resp && resp.config) || { ...acct, ...patch };
      Fastt.saved();
    } catch (e) { Fastt.oops(e); }
    render();
  }

  // The two spend-floor windows the server ships (defaults.msg_limits_by_spend =
  // [{days:30,min_cents:1000,cap:10},{days:7,min_cents:10000,cap:15}]). Ids match
  // the inputs; the fallback is the server default for that window.
  const SPEND_RULES = [{ days: 30, id: 'adv-sp30' }, { days: 7, id: 'adv-sp7' }];
  const spendList = () => {
    const c = eff();
    const l = Array.isArray(c.msg_limits_by_spend) ? c.msg_limits_by_spend
            : (defs.msg_limits_by_spend || []);
    return l.map(r => ({ days: Number(r.days) || 0, min_cents: Number(r.min_cents) || 0,
                         cap: Number(r.cap) || 0 }));
  };
  const spendRule = (days) => spendList().find(r => r.days === days)
    || (defs.msg_limits_by_spend || []).find(r => Number(r.days) === days)
    || { days, min_cents: 0, cap: 0 };
  const setSpend = (days, patch) => {
    const rest = spendList().filter(r => r.days !== days);
    const next = { ...spendRule(days), ...patch, days };
    return { msg_limits_by_spend: rest.concat([next]).sort((a, b) => b.days - a.days) };
  };

  const setOn = (el, on) => { if (el) el.classList.toggle('on', !!on); };
  const setTxt = (id, t) => { const el = document.getElementById(id); if (el) el.textContent = t; };
  const setVal = (id, v) => { const el = document.getElementById(id);
    if (el && document.activeElement !== el) el.value = v; };
  const setSeg = (seg, val) => { if (!seg) return;
    $$('button', seg).forEach(b => b.classList.toggle('on', b.dataset.val === val)); };
  const setSlider = (id, v) => { const el = document.getElementById(id); if (!el) return;
    el.value = v;
    const rv = el.previousElementSibling && el.previousElementSibling.querySelector('.fx-rv');
    if (rv) { const f = el.getAttribute('data-fmt') || '%v';
      rv.textContent = f.replace('%v', Number(el.value).toLocaleString()); } };

  // max_offers_per_fan_per_day floors at 0 in the validator (scripts_api _INT_KNOBS)
  // and the engine reads `if max_day > 0` — so a stored 0 means UNLIMITED, not "1".
  // The slider now allows 0; relabel it so the readout does not lie.
  function offersLabel() {
    const el = document.getElementById('sl-offers');
    if (!el) return;
    const head = el.previousElementSibling;
    const rv = head && head.querySelector('.fx-rv');
    if (rv && Number(el.value) === 0) rv.textContent = 'Unlimited';
  }
  document.addEventListener('input', (e) => {
    if (e.target && e.target.id === 'sl-offers') offersLabel();   // after fx-js repaints
  });

  function render() {
    const c = eff();
    setOn($('#hdr-enabled'), c.enabled);
    setTxt('st-run-t', c.enabled ? 'Running' : 'Off');
    const runPill = $('#st-run'); if (runPill) runPill.className = 'fx-st ' + (c.enabled ? 'ok' : '');
    setTxt('st-mode-t', c.mode === 'backup' ? 'Backup — missed chats only' : 'Always-on');
    setTxt('st-seller-t', 'Full seller');
    setTxt('st-model-t', modelLabel(acct.model));
    let cost = null;
    if (costResp && Array.isArray(costResp.rows)) {
      const today = new Date().toISOString().slice(0, 10);
      const row = costResp.rows.find(r =>
        String(r.account_id) === String(Fastt.account()) && r.day === today);
      cost = row ? Number(row.cost_cents) || 0 : 0;
    }
    setTxt('st-cost-t', cost === null ? '$— today' : Fastt.fmtCents(cost) + ' today');

    const preset = 'aggressive';   // the only preset with a backend behind it
    $$('#presets .fx-preset').forEach(p =>
      p.classList.toggle('sel', p.dataset.preset === preset));
    // The card shipped "your current setup (the graded vault · Ava)" — a hardcoded
    // cross-account claim. Name the creator actually on screen, or say nothing.
    const row = Fastt.accountRow();
    setTxt('preset-aggr-t', 'Creates the intent itself.'
      + (preset === 'aggressive'
          ? ' Currently selected for ' + (row ? (row.nickname || row.id) : Fastt.account()) + '.'
          : ''));

    setSlider('sl-whale', Math.round((c.max_lifetime_spend_cents || 0) / 100));
    setSlider('sl-offers', c.max_offers_per_fan_per_day);
    offersLabel();                            // 0 is a real, storable state: unlimited
    setSlider('sl-patience', c.min_fan_msgs_between_offers);
    setSlider('sl-sla', c.sla_minutes);

    setOn($('[data-style="ai_chatter"]'), style.ai_chatter);
    setOn($('[data-style="typos_ai_chatter"]'), style.typos_ai_chatter);
    setOn($('[data-style="nonnative_ai_chatter"]'), style.nonnative_ai_chatter);
    setOn($('[data-style="strip_emojis"]'), style.strip_emojis);
    // cat_stickers ships DEFAULT ON (style_config_api: absent → True), so an
    // untouched creator reads it as on, matching the engine.
    setOn($('[data-style="cat_stickers"]'), style.cat_stickers !== false);
    setOn($('#sh-master'),
      style.ai_chatter || style.typos_ai_chatter || style.nonnative_ai_chatter);

    setSeg($('[data-seg="mode"]'), c.mode || 'always');
    $$('.fx-check[data-cfg]').forEach(el => setOn(el, c[el.dataset.cfg]));

    // Old-fan get-to-know cadence — only meaningful when engage_old_fans is on;
    // grey the field out (not hide) so its purpose stays legible.
    setVal('adv-oldfan-every', c.old_fan_question_every != null ? c.old_fan_question_every : 10);
    const ofg = $('#grp-oldfan'); if (ofg) ofg.classList.toggle('grp-off', !c.engage_old_fans);
    setVal('adv-ask-after', c.ask_after_fan_msgs != null ? c.ask_after_fan_msgs : 0);
    setVal('adv-resume', c.resume_after_manual_hours != null ? c.resume_after_manual_hours : 0);
    setVal('adv-expiry-h', Math.round(((c.offer_expiry_minutes || 0) / 60) * 10) / 10);
    setVal('adv-max-open', c.max_open_offers);
    setVal('adv-psf-mult', c.proven_spend_floor_mult);
    const ml = { ...(defs.msg_limits_by_signal || {}), ...(c.msg_limits_by_signal || {}) };
    setVal('adv-ml-baseline', ml.baseline);
    setVal('adv-ml-buying', ml.buying_signal);
    setVal('adv-ml-nosig', ml.no_signal);
    setVal('adv-ml-pic', ml.pic_sent);

    // Proven-spender cap FLOOR — msg_limits_by_spend is a LIST the validator takes
    // whole (scripts_api _MAX_SPEND_RULES), so every save resends both rules; the
    // server shallow-merges the config and a partial list would drop the other one.
    SPEND_RULES.forEach(({ days, id }) => {
      const r = spendRule(days);
      setVal(id + '-min', Math.round((r.min_cents || 0) / 100));
      setVal(id + '-cap', r.cap);
    });
    // Master switch: the group only takes effect when its flag is on, so say so
    // instead of letting an operator tune four inert inputs.
    const gc = $('#grp-cadence');
    if (gc) gc.classList.toggle('grp-off', !c.cadence_enabled);
    setVal('adv-session-gap', c.session_gap_minutes);
    setVal('adv-post-buy', c.post_purchase_minutes);
    setVal('adv-nudge-after', c.nudge_after_minutes);
    setVal('adv-velocity', (c.spend_velocity_cap_7d_cents || 0) / 100);
    setVal('adv-stop-unpaid', c.stop_after_unpaid_rungs);
    setVal('adv-haggle', Math.round((c.haggle_discount_pct || 0) * 100));
    setVal('adv-teaser', Math.round((c.teaser_discount_pct || 0) * 100));

    const sel = $('#adv-model');
    if (sel && Array.isArray(acctResp.model_options)) {
      sel.innerHTML = acctResp.model_options.map(m =>
        '<option value="' + Fastt.esc(m) + '"' + (m === acct.model ? ' selected' : '') + '>'
        + Fastt.esc(modelLabel(m)) + '</option>').join('');
    }
    setVal('adv-cost-cap',
      ((acct.daily_cost_cap_cents != null ? acct.daily_cost_cap_cents : 100) / 100).toFixed(2));
  }
  render();

  Fastt.liveBadge($('#card-how .fx-card-h'));
  Fastt.liveBadge($('#sh-card .fx-card-h'));
  Fastt.liveBadge($('.fx-adv-head'));
  // "Chat only" has no backend flag (AI Chatter always sells) — the two live
  // presets write real config, this one only routes you to Auto Convo. Badge it
  // as a signpost, not a "static demo" (which wrongly implied a fake mockup).
  Fastt.staticBadge($('#presets [data-preset="chat"] .pn'), 'IN AUTO CONVO');

  // ── Live monitor — GET /admin/ai-chatter/sessions ─────────────────────────
  // {progress:[{fan_id,fan_name,script,position,status}], offers:[{id,fan_id,fan_name,
  //  item_label,mode,status,price_cents,tip_unlock_cents,tips_accum_cents,resolved_by,
  //  offered_at}]}. offered_at is a tz-NAIVE UTC stamp → parseUtc, never new Date().
  const MON_MAX = 25;
  const fanLabel = (r) => String(r.fan_name || '').split('/')[0].trim() || ('fan ' + r.fan_id);
  async function loadMonitor() {
    const pins = $('#mon-pins'), offs = $('#mon-offers');
    try {
      const out = await Fastt.get('/admin/ai-chatter/sessions');
      const progress = out.progress || [], offers = out.offers || [];
      const open = offers.filter(o => o.status === 'open');
      const chip = $('#mon-open');
      chip.textContent = open.length
        ? open.length + ' open offer' + (open.length === 1 ? '' : 's')
        : 'no open offers';
      chip.className = 'mon-chip' + (open.length ? ' warn' : '');
      $('#mon-count').textContent = offers.length
        ? '· ' + Fastt.fmtInt(offers.length) + ' total' : '';

      pins.innerHTML = progress.length ? progress.map(p =>
        '<div class="mon-row"><span class="mon-fan">' + Fastt.esc(fanLabel(p)) + '</span>'
        + '<span class="mon-item">🎬 ' + Fastt.esc(p.script || '—')
        + ' · item ' + Fastt.esc(String(p.position != null ? p.position : '—')) + '</span>'
        + '<span class="mon-badge' + (p.status === 'active' ? ' ok' : '') + '">'
        + Fastt.esc(String(p.status || '—')) + '</span></div>').join('')
        : '<div class="mon-empty">No script pins yet — a pin only exists while she is walking a '
          + 'fan through a multi-part scene. Single-rung offers never create one.</div>';

      offs.innerHTML = offers.length ? offers.slice(0, MON_MAX).map(o => {
        const px = o.mode === 'tip' ? o.tip_unlock_cents : o.price_cents;
        const cls = o.status === 'open' ? ' open' : (o.status === 'delivered' ? ' ok' : '');
        return '<div class="mon-row" data-offer="' + Fastt.esc(String(o.id)) + '">'
          + '<span class="mon-fan">' + Fastt.esc(fanLabel(o)) + '</span>'
          + '<span class="mon-item">' + Fastt.esc(o.item_label || ('item #' + o.id)) + '</span>'
          + '<span class="mon-px">' + Fastt.fmtCents(px) + '</span>'
          + '<span class="mon-badge' + cls + '">' + Fastt.esc(String(o.status || '—'))
          + (o.resolved_by ? ' · ' + Fastt.esc(String(o.resolved_by)) : '') + '</span>'
          + '<span class="mon-when">' + Fastt.esc(Fastt.fmtAgo(o.offered_at)) + '</span>'
          + (o.status === 'open'
              ? '<button class="mon-x" data-cancel="' + Fastt.esc(String(o.id))
                + '" title="Cancel this offer — hands the fan to a human. Sends the fan nothing.">✕</button>'
              : '<span style="width:20px;flex:0 0 auto"></span>')
          + '</div>';
      }).join('') + (offers.length > MON_MAX
        ? '<div class="mon-more">showing the newest ' + MON_MAX + ' of '
          + Fastt.fmtInt(offers.length) + '</div>' : '')
        : '<div class="mon-empty">She has not offered anything on this creator yet.</div>';
    } catch (e) {
      $('#mon-open').textContent = 'unavailable';
      pins.innerHTML = '';
      offs.innerHTML = '<div class="mon-empty">The relay refused /admin/ai-chatter/sessions ('
        + Fastt.esc(String((e && e.body && (e.body.detail || e.body.error)) || (e && e.message) || 'error'))
        + ') — no offer data to show.</div>';
      console.error(e);
    }
  }
  Fastt.liveBadge($('#mon-h'));
  loadMonitor();
  $('#mon-refresh').addEventListener('click', () => loadMonitor());
  // Cancelling closes the offer row and hands the fan back to a human. It sends the
  // fan NOTHING — but it is still a state write, so it stays behind an explicit confirm.
  $('#mon-offers').addEventListener('click', async (e) => {
    const btn = e.target.closest('[data-cancel]');
    if (!btn) return;
    const id = btn.dataset.cancel;
    if (!confirm('Cancel offer #' + id + '?\n\nThe open offer is closed and the fan is handed to '
      + 'a human. Nothing is sent to the fan.')) return;
    btn.disabled = true;
    try {
      await Fastt.post('/admin/ai-chatter/offers/' + encodeURIComponent(id) + '/cancel',
        { account_id: String(Fastt.account()) });
      Fastt.saved('Offer #' + id + ' cancelled');
    } catch (err) { Fastt.oops(err); }
    loadMonitor();
  });

  // ── Test her — POST /admin/scripts/simulate (dry run, sends nothing) ──────────
  // Returns {bubbles:[...], offer:null|{label,price_cents,tip_unlock_cents,is_free_teaser},
  //  offerable_count, manifest_present}. It DOES fire one real model call (fan_id=0),
  // so it lives behind an explicit click — but it never touches a fan thread.
  Fastt.liveBadge($('#sim-h'));
  const simIn = $('#sim-in'), simBtn = $('#sim-run'), simOut = $('#sim-out');
  let simBusy = false;
  async function runSim() {
    if (simBusy) return;
    const fanSays = (simIn.value || simIn.placeholder || '').trim();
    if (!fanSays) { simIn.focus(); return; }
    simBusy = true; simBtn.disabled = true; simBtn.textContent = 'Running…';
    simOut.innerHTML = '<div class="sim-loading"><span class="sim-spin"></span>'
      + 'Thinking like ' + Fastt.esc((Fastt.accountRow() && (Fastt.accountRow().nickname))
        || String(Fastt.account())) + '…</div>';
    try {
      const r = await Fastt.post('/admin/scripts/simulate',
        { account_id: String(Fastt.account()), fan_says: fanSays });
      const bubbles = Array.isArray(r.bubbles) ? r.bubbles : [];
      let html = '';
      if (!r.manifest_present) {
        html += '<div class="fx-note warn" style="margin-top:12px">'
          + '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" style="flex:0 0 auto;margin-top:1px"><path d="M12 3l9 16H3z" stroke-linejoin="round"/><path d="M12 10v4" stroke-linecap="round"/><circle cx="12" cy="17" r=".9" fill="currentColor" stroke="none"/></svg>'
          + 'No sellable items reached the prompt — she can chat but can’t offer anything. '
          + 'Add catalog items on <b>Scripts &amp; Pricing</b> (check they have media + a price).</div>';
      }
      html += bubbles.length
        ? '<div class="sim-bubbles">' + bubbles.map(b =>
            '<div class="sim-bubble">' + Fastt.esc(String(b)) + '</div>').join('') + '</div>'
        : '<div class="mon-empty" style="margin-top:12px">She returned no text for that message.</div>';
      if (r.offer) {
        const teaser = r.offer.is_free_teaser;
        const px = teaser ? 'free teaser'
          : '$' + ((Number(r.offer.tip_unlock_cents) || Number(r.offer.price_cents) || 0) / 100).toFixed(2);
        html += '<div class="sim-offer">💰 Would offer: '
          + Fastt.esc(r.offer.label || ('item #' + r.offer.item_id)) + ' · ' + Fastt.esc(px) + '</div>';
      } else if (r.manifest_present) {
        html += '<div class="fx-kv2" style="margin-top:10px">No offer this turn — '
          + Fastt.fmtInt(r.offerable_count || 0) + ' item'
          + ((r.offerable_count === 1) ? '' : 's') + ' were on the table; she chose to keep chatting.</div>';
      }
      simOut.innerHTML = html;
    } catch (e) {
      const detail = (e && e.body && (e.body.detail || e.body.error))
        || (e && e.message) || 'error';
      // A 500 here is a server-side condition (usually the account's resolved chat
      // model or content library not being ready) — surface it, don't fake success.
      const hint = (e && e.status === 500)
        ? ' This is a server-side error for this creator — often the chat model or content '
          + 'library isn’t ready. It never reaches a fan.'
        : ' Nothing was sent.';
      simOut.innerHTML = '<div class="fx-note warn" style="margin-top:12px">'
        + '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" style="flex:0 0 auto;margin-top:1px"><circle cx="12" cy="12" r="9"/><path d="M12 8v5" stroke-linecap="round"/><circle cx="12" cy="16" r=".9" fill="currentColor" stroke="none"/></svg>'
        + 'The dry run couldn’t complete: ' + Fastt.esc(String(detail).slice(0, 160))
        + '.' + hint + '</div>';
      console.error(e);
    } finally {
      simBusy = false; simBtn.disabled = false; simBtn.textContent = 'Run test';
    }
  }
  simBtn.addEventListener('click', runSim);
  simIn.addEventListener('keydown', (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') { e.preventDefault(); runSim(); }
  });

  const num = (el) => {
    const v = parseFloat(String(el.value).replace(/[^0-9.eE+-]/g, ''));
    return isFinite(v) ? v : null;
  };
  const mlPatch = (tier, v) => {
    const c = eff();
    const ml = { ...(defs.msg_limits_by_signal || {}), ...(c.msg_limits_by_signal || {}) };
    ml[tier] = Math.max(0, Math.round(v));
    return { msg_limits_by_signal: ml };
  };

  // fx-js (registered earlier) has already flipped the visual state when these run.
  document.addEventListener('click', (e) => {
    const sw = e.target.closest('.fx-switch');
    if (sw) {
      const on = sw.classList.contains('on');
      if (sw.id === 'hdr-enabled') { saveCfg({ enabled: on }); return; }
      if (sw.id === 'sh-master') {
        saveStyle({ ai_chatter: on, typos_ai_chatter: on, nonnative_ai_chatter: on });
        return;
      }
      return;
    }
    const pr = e.target.closest('#presets .fx-preset');
    if (pr) {
      const p = pr.dataset.preset;
      if (p === 'chat') {
        Fastt.toast('Chat-only has no backend knob yet — AI Chatter always sells. Use Auto Convo for pure chat.');
        render();                                    // snap the selection back
      } else if (p === 'aggressive') {
        saveCfg({ force_ask: true, nudge_enabled: true });
      }
      return;
    }
    const sg = e.target.closest('.fx-seg[data-seg] button');
    if (sg) { saveCfg({ [sg.closest('.fx-seg').dataset.seg]: sg.dataset.val }); return; }
    const ck = e.target.closest('.fx-check[data-cfg]');
    if (ck) { saveCfg({ [ck.dataset.cfg]: ck.classList.contains('on') }); return; }
    const sk = e.target.closest('.fx-check[data-style]');
    if (sk) { saveStyle({ [sk.dataset.style]: sk.classList.contains('on') }); return; }
  });

  const H = {
    'adv-oldfan-every': v => ({ old_fan_question_every: Math.min(100, Math.max(1, Math.round(v))) }),
    'adv-ask-after': v => ({ ask_after_fan_msgs: Math.round(v) }),
    'adv-resume': v => ({ resume_after_manual_hours: Math.round(v) }),
    'adv-expiry-h': v => ({ offer_expiry_minutes: Math.round(v * 60) }),
    'adv-max-open': v => ({ max_open_offers: Math.round(v) }),
    'adv-psf-mult': v => ({ proven_spend_floor_mult: v }),
    'adv-ml-baseline': v => mlPatch('baseline', v),
    'adv-ml-buying': v => mlPatch('buying_signal', v),
    'adv-ml-nosig': v => mlPatch('no_signal', v),
    'adv-ml-pic': v => mlPatch('pic_sent', v),
    'adv-sp30-min': v => setSpend(30, { min_cents: Math.max(0, Math.round(v * 100)) }),
    'adv-sp30-cap': v => setSpend(30, { cap: Math.max(0, Math.round(v)) }),
    'adv-sp7-min': v => setSpend(7, { min_cents: Math.max(0, Math.round(v * 100)) }),
    'adv-sp7-cap': v => setSpend(7, { cap: Math.max(0, Math.round(v)) }),
    'adv-session-gap': v => ({ session_gap_minutes: Math.round(v) }),
    'adv-post-buy': v => ({ post_purchase_minutes: Math.round(v) }),
    'adv-nudge-after': v => ({ nudge_after_minutes: Math.round(v) }),
    'adv-velocity': v => ({ spend_velocity_cap_7d_cents: Math.round(v * 100) }),
    'adv-stop-unpaid': v => ({ stop_after_unpaid_rungs: Math.round(v) }),
    'adv-haggle': v => ({ haggle_discount_pct: v / 100 }),
    'adv-teaser': v => ({ teaser_discount_pct: v / 100 }),
  };
  document.addEventListener('change', (e) => {
    const t = e.target;
    if (t.classList && t.classList.contains('fx-range')) {
      const v = Number(t.value);
      if (t.id === 'sl-whale') saveCfg({ max_lifetime_spend_cents: Math.round(v * 100) });
      else if (t.id === 'sl-offers') saveCfg({ max_offers_per_fan_per_day: v });
      else if (t.id === 'sl-patience') saveCfg({ min_fan_msgs_between_offers: v });
      else if (t.id === 'sl-sla') saveCfg({ sla_minutes: v });
      return;
    }
    if (t.id === 'adv-model') { saveAcct({ model: t.value }); return; }
    if (t.id === 'adv-cost-cap') {
      const v = num(t);
      if (v === null) render(); else saveAcct({ daily_cost_cap_cents: Math.round(v * 100) });
      return;
    }
    if (H[t.id]) {
      const v = num(t);
      if (v === null) render(); else saveCfg(H[t.id](v));
    }
  });
});
