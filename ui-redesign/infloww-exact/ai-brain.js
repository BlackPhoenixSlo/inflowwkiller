/* ==== Brain & Persona wiring — GET/PUT /admin/account-config ==== */
Fastt.ready(async () => {
  const $ = Fastt.$, $$ = Fastt.$$;
  const acct = Fastt.account();
  if (!acct) return;

  const MODEL_LABEL = {
    'deepseek-v4-flash': 'DeepSeek V4 Flash — fast & cheap (recommended)',
    'deepseek-v4-pro': 'DeepSeek V4 Pro',
    'grok-4-1-fast-non-reasoning': 'Grok 4.1 Fast',
    'qwen3-vl-30b': 'Qwen3-VL 30B',
    'qwen3-vl-235b': 'Qwen3-VL 235B',
    'glm-5.3-flash': 'GLM 5.3 Flash — cheapest GLM, ~1.9s',
    'glm-4.5-air': 'GLM 4.5 Air — fastest (~0.6s), no thinking, ~18% dearer',
  };
  const label = (id) => MODEL_LABEL[id] || id;
  const shortLabel = (id) => (MODEL_LABEL[id] || id).split(' — ')[0];

  var chipText = Fastt.chipText;
  function setCapSlider(cents) {
    const el = $('#r-cap');
    const dollars = (Number(cents) || 0) / 100;
    if (dollars > Number(el.max)) el.max = Math.ceil(dollars);
    if (dollars < Number(el.min)) el.min = Math.floor(dollars);
    el.value = dollars;
    const rv = el.previousElementSibling.querySelector('.fx-rv');
    if (rv) rv.textContent = Fastt.fmtCents(cents);
  }

  const out = await Fastt.get('/admin/account-config');
  let stored = out.config;               // last server-confirmed brain
  const purposes = out.purposes || [];   // the server's key list — load-bearing, see pSelects

  // ---- language: the shipped es/sl packs had no control at all -------------
  // GET already returns languages:[{code,label}] alongside config.language, and
  // the language drives BOTH the voice and which safety-word list runs.
  const sLang = $('#s-lang');
  sLang.innerHTML = (out.languages || [{ code: 'en', label: 'English' }]).map((l) =>
    '<option value="' + Fastt.esc(l.code) + '">' + Fastt.esc(l.label) + '</option>').join('');
  sLang.title = 'The language she writes in AND which safety-word list runs. Changing it '
    + "re-generates each fan's saved lines on their next profile pass.";

  // ---- day-rhythm slot images (config.time_images) -------------------------
  // Stored as {slot: vault_media_id}. The thumbs come from the relay's own
  // permanent on-disk cache (/admin/vault-ai/thumb) — signature-free, so a tile
  // never breaks on an expired OF url. An id with no cached thumb says so rather
  // than showing a broken image or a decorative gradient standing in for a photo.
  let imgs = Object.assign({}, stored.time_images || {});
  const thumbUrl = (id) => '/admin/vault-ai/thumb?account_id='
    + encodeURIComponent(Fastt.account()) + '&media_id=' + encodeURIComponent(id);
  function paintSlots() {
    $$('.rhy-slot').forEach((holder) => {
      const slot = holder.dataset.imgslot;
      const id = imgs[slot];
      holder.innerHTML = id
        ? '<span class="rhy-thumb" data-pick="' + slot + '" title="Media #' + Fastt.esc(String(id))
          + ' — click to change" style="background:#1c1c1c url(' + thumbUrl(id)
          + ') center/cover"><span class="rx" data-clear="' + slot + '" title="Remove this picture">✕</span></span>'
        : '<button class="rhy-add" data-pick="' + slot + '">+ Picture</button>';
    });
  }

  // Picker source: every vault media this account already uses in its catalog
  // (GET /admin/scripts singles[].media_ids / preview_media_ids) plus whatever is
  // already pinned to a slot. Those are exactly the ids whose thumbs the relay has
  // cached, so every tile renders. The full vault browser lives on the Vault AI page.
  let pickerIds = null;
  async function loadPickerIds() {
    if (pickerIds) return pickerIds;
    const seen = new Set(Object.values(imgs).map(Number).filter(Boolean));
    try {
      const cat = await Fastt.get('/admin/scripts');
      (cat.singles || []).concat((cat.scripts || []).flatMap((sc) => sc.items || []))
        .forEach((it) => {
          (it.preview_media_ids || []).forEach((m) => seen.add(Number(m)));
          (it.media_ids || []).forEach((m) => seen.add(Number(m)));
        });
    } catch (e) { console.error(e); }
    pickerIds = Array.from(seen).filter((n) => n > 0);
    return pickerIds;
  }
  async function openPicker(slot) {
    const ids = await loadPickerIds();
    const back = document.createElement('div');
    back.className = 'vp-back';
    back.innerHTML = '<div class="vp"><div class="vp-h">Pick a picture for this slot'
      + '<span class="sub">vault media already used by this creator</span>'
      + '<button class="x" data-vpclose>✕</button></div>'
      + (ids.length
          ? '<div class="vp-grid">' + ids.map((id) =>
              '<div class="vp-tile' + (Number(imgs[slot]) === id ? ' sel' : '') + '" data-mid="' + id
              + '" style="background-image:url(' + thumbUrl(id) + ')"><b>#' + id + '</b></div>').join('')
            + '</div>'
          : '<div class="vp-empty">This creator has no vault media bound to a catalog rung yet, so '
            + 'there is nothing here to offer. Bind media on <a class="plink" href="ai-scripts-pricing.html">'
            + 'Scripts &amp; Pricing</a> (or in Vault AI) and it will show up here.</div>')
      + '</div>';
    document.body.appendChild(back);
    back.addEventListener('click', (ev) => {
      if (ev.target === back || ev.target.closest('[data-vpclose]')) { back.remove(); return; }
      const tile = ev.target.closest('.vp-tile');
      if (!tile) return;
      imgs[slot] = Number(tile.dataset.mid);
      back.remove();
      paintSlots();
      Fastt.toast('Picture set — press "Save brain" to keep it');
    });
  }
  document.addEventListener('click', (e) => {
    const clear = e.target.closest('[data-clear]');
    if (clear) {
      e.stopPropagation();
      delete imgs[clear.dataset.clear];
      paintSlots();
      Fastt.toast('Picture removed — press "Save brain" to keep it');
      return;
    }
    const pick = e.target.closest('[data-pick]');
    if (pick) openPicker(pick.dataset.pick);
  });

  // ---- build the model dropdowns from the live model list ------
  const sModel = $('#s-model');
  sModel.innerHTML = '<option value="">— no model set —</option>' +
    (out.model_options || []).map((m) =>
      '<option value="' + Fastt.esc(m) + '">' + Fastt.esc(label(m)) + '</option>').join('');
  // ---- thinking effort: options follow the MODEL, not the provider ------
  // The legal values differ between providers ("medium" is good DeepSeek and a
  // hard 400 on z.ai), and most models have no reasoning control at all — so
  // the row HIDES rather than offering a box that cannot do anything.
  //
  // The list is weakest-first and the fallback is opts[0], so picking a new
  // model lands on the CHEAPEST setting it has rather than inheriting whatever
  // suited the last one. There is no blank "use the default" entry on purpose:
  // an effort that travels is always a real value, and this editor is where it
  // gets chosen. The saved value still wins on load, so the box shows what the
  // account is actually running rather than resetting it on sight.
  const sEffort = $('#s-effort'), effortRow = $('#fx-effort');
  const EFFORTS = out.effort_options || {};
  function syncEffort(preferred) {
    const opts = EFFORTS[sModel.value] || [];
    effortRow.style.display = opts.length ? '' : 'none';
    sEffort.innerHTML = opts.map(
      (v) => '<option value="' + Fastt.esc(v) + '">' + Fastt.esc(v) + '</option>').join('');
    sEffort.value = opts.indexOf(preferred) >= 0 ? preferred : (opts[0] || '');
  }
  sModel.addEventListener('change', () => syncEffort(''));

  // A hand-typed `data-purpose` is the copy that drifts. The server owns the key
  // list (MODEL_PURPOSES) and the lanes read those exact strings, so a field
  // naming a key the server no longer offers would go on silently writing a dead
  // override — which is precisely how the "send_welcome"/"send_followup" fields
  // sat dead for months. Hide those instead of letting them lie.
  const pSelects = $$('select[data-purpose]').filter((sel) => {
    if (purposes.indexOf(sel.dataset.purpose) >= 0) return true;
    const field = sel.closest('.fx-field');
    if (field) field.style.display = 'none';
    return false;
  });
  pSelects.forEach((sel) => {
    sel.innerHTML = '<option value="">Inherit default</option>' +
      (out.model_options || []).map((m) =>
        '<option value="' + Fastt.esc(m) + '">' + Fastt.esc(shortLabel(m)) + '</option>').join('');
  });

  // ---- fill the form from a config object ----------------------
  function fill(cfg) {
    $('#t-persona').value = cfg.persona || '';
    $('#t-welcome').value = cfg.welcome_rules || '';
    $('#f-location').value = cfg.location || '';
    sModel.value = cfg.model || '';
    syncEffort(cfg.reasoning_effort || '');
    const tz = $('#s-tz');
    if (cfg.timezone && !Array.from(tz.options).some((o) => o.value === cfg.timezone)) {
      const o = document.createElement('option');
      o.value = cfg.timezone; o.textContent = cfg.timezone;
      tz.appendChild(o);
    }
    if (!Array.from(tz.options).some((o) => o.value === '')) {
      const o = document.createElement('option');
      o.value = '';
      const off = Number(cfg.utc_offset) || 0;
      o.textContent = 'No city set — UTC' + (off >= 0 ? '+' : '−') + String(Math.abs(off)).padStart(2, '0') + ':00';
      tz.insertBefore(o, tz.firstChild);
    }
    tz.value = cfg.timezone || '';
    setCapSlider(cfg.daily_cost_cap_cents);
    const mbp = cfg.model_by_purpose || {};
    pSelects.forEach((sel) => { sel.value = mbp[sel.dataset.purpose] || ''; });
    const acts = cfg.time_activities || {};
    $$('input[data-slot]').forEach((inp) => { inp.value = acts[inp.dataset.slot] || ''; });
    sLang.value = cfg.language || 'en';
    if (!sLang.value) sLang.value = 'en';
    imgs = Object.assign({}, cfg.time_images || {});
    paintSlots();
  }

  function paintStatus(cfg) {
    const st = $('#st-brain');
    st.classList.toggle('ok', !!cfg.persona);
    chipText(st, cfg.persona ? 'Brain active' : 'No persona set');
    chipText($('#st-model'), cfg.model ? shortLabel(cfg.model) : 'model not set');
    chipText($('#st-cap'), 'AI budget ' + Fastt.fmtCents(cfg.daily_cost_cap_cents) + '/day');
  }

  fill(stored);
  paintStatus(stored);

  Fastt.liveBadge($('.fx-card > .fx-card-h'));
  Fastt.staticBadge($('#fld-vault-vision'), 'STATIC DEMO');       // vault model lives on another surface
  Fastt.liveBadge($('#h-rhythm'));                                // real thumbs + picker + remove

  // ---- gather + save ------------------------------------------
  function gather() {
    const mbp = Object.assign({}, stored.model_by_purpose);       // keeps deep_convo etc.
    pSelects.forEach((sel) => {
      const p = sel.dataset.purpose;
      if (sel.value) mbp[p] = sel.value; else delete mbp[p];
    });
    const acts = {};
    $$('input[data-slot]').forEach((inp) => {
      const v = inp.value.trim();
      if (v) acts[inp.dataset.slot] = v;
    });
    return {
      persona: $('#t-persona').value,
      welcome_rules: $('#t-welcome').value,
      location: $('#f-location').value,
      language: sLang.value || 'en',
      timezone: $('#s-tz').value || null,
      utc_offset: stored.utc_offset,                               // preserved; IANA timezone wins server-side
      daily_cost_cap_cents: Math.round(Number($('#r-cap').value) * 100),
      model: sModel.value || null,
      // Empty for a model with no reasoning control — the server refuses an
      // effort on one of those, and the row is hidden in that case anyway.
      reasoning_effort: sEffort.value || null,
      model_by_purpose: mbp,
      time_activities: acts,
      time_images: Object.assign({}, imgs),                        // slot picker + ✕ remove write here
    };
  }

  $('#btn-save').addEventListener('click', async () => {
    const btn = $('#btn-save');
    btn.disabled = true;
    try {
      const resp = await Fastt.put('/admin/account-config', { account_id: acct, config: gather() });
      stored = resp.config;
      fill(stored);
      paintStatus(stored);
      Fastt.saved('Brain saved ✓');
    } catch (e) { Fastt.oops(e); }
    finally { btn.disabled = false; }
  });

  $('#btn-reset').addEventListener('click', () => {
    if (!confirm('Refill every field from the house example brain?\n\nNothing is saved until you press "Save brain". Your slot pictures are kept.')) return;
    const d = Object.assign({}, out.defaults, {
      time_images: imgs,                    // the pictures survive a reset, saved or not
      utc_offset: stored.utc_offset,
      language: sLang.value || stored.language,
    });
    fill(d);
    Fastt.toast('Form refilled from defaults — press "Save brain" to keep it');
  });

  // live label while dragging the budget slider (kit shows whole dollars)
  $('#r-cap').addEventListener('input', (e) => {
    const rv = e.target.previousElementSibling.querySelector('.fx-rv');
    if (rv) rv.textContent = Fastt.fmtCents(Math.round(Number(e.target.value) * 100));
  });

  // ---- timezone live clock: the operator's glance-check that her clock is right
  // (a bot claiming "good morning" at midnight is the failure this guards). Computed
  // by Intl from the picked IANA zone, never stored — the saved utc_offset is only a
  // DST-blind fallback for zone-less accounts.
  function tzClock() {
    const el = $('#tz-clock'); if (!el) return;
    const tz = $('#s-tz').value;
    if (!tz) {
      const off = Number(stored.utc_offset) || 0;
      el.textContent = off
        ? 'legacy UTC' + (off > 0 ? '+' : '−') + Math.abs(off) + ' offset in use — pick her real city to stay DST-correct'
        : 'no city set — she has no clock in chat';
      el.style.color = off ? '#e8cf9a' : 'var(--muted2)';
      return;
    }
    try {
      const off = (new Intl.DateTimeFormat('en-US', { timeZone: tz, timeZoneName: 'shortOffset' })
        .formatToParts(new Date()).find((p) => p.type === 'timeZoneName') || {}).value;
      const clock = new Date().toLocaleTimeString('en-US', { timeZone: tz, hour: 'numeric', minute: '2-digit' });
      el.textContent = (off ? off.replace('GMT', 'UTC') : '') + ' · her clock right now: ' + clock;
      el.style.color = 'var(--muted2)';
    } catch (e) { el.textContent = ''; }
  }
  $('#s-tz').addEventListener('change', tzClock);
  tzClock();
  setInterval(tzClock, 30000);

  // ============================================================================
  //  WELCOME + FOLLOW-UP automation rules — two backend objects distinct from
  //  the brain config (the send_welcome / send_followup automation_rules). Each
  //  card owns its own Save. Turning either ON starts REAL outbound messaging to
  //  real fans, so enabling is confirm()-gated on every save that flips it on.
  // ============================================================================
  const SLOT_LABEL = {
    morning_1: 'Morning (early)', morning_2: 'Morning (late)',
    afternoon_1: 'Afternoon (early)', afternoon_2: 'Afternoon (late)',
    evening: 'Evening', night: 'Night',
  };
  const RULES = {};
  async function loadRules() {
    const m = await Fastt.rulesByKind();
    RULES.send_welcome = (m.send_welcome || [])[0] || null;
    RULES.send_followup = (m.send_followup || [])[0] || null;
  }
  const everyMin = (s, def) => Math.max(1, Math.round((Number(s) || def) / 60));

  function lastRunText(r) {
    if (!r) return 'not set up yet';
    const lr = r.last_run;
    const ran = lr && lr.started_at ? Fastt.fmtAgo(lr.completed_at || lr.started_at) : null;
    if (lr && lr.error_text) return (r.is_enabled ? 'on' : 'paused') + ' · last run errored ' + (ran || '');
    if (!r.is_enabled) return ran ? 'paused · last ran ' + ran : 'paused';
    return ran ? 'on · last ran ' + ran : 'on · not run yet';
  }
  function setStatChip(el, r) {
    const i = el.querySelector('i');
    el.textContent = ''; if (i) el.appendChild(i);
    el.appendChild(document.createTextNode(lastRunText(r)));
    const err = !!(r && r.last_run && r.last_run.error_text);
    el.classList.toggle('ok', !!(r && r.is_enabled && !err));
    el.classList.toggle('err', err);
  }

  // shared preview renderer (welcome + follow-up return the same shape)
  function renderPreview(box, res, kind) {
    if (!res) { box.innerHTML = ''; return; }
    const img = res.image
      ? '<div class="prev-thumb" title="Media #' + Fastt.esc(String(res.image))
        + '" style="background:#1c1c1c url(' + thumbUrl(res.image) + ') center/cover"></div>' : '';
    const meta = 'To “' + Fastt.esc(res.name || 'babe') + '”'
      + (res.slot ? ' · ' + (SLOT_LABEL[res.slot] || res.slot) : '')
      + (kind === 'send_followup' && res.step ? ' · step ' + res.step : '')
      + (res.image ? '' : ' · no image')
      + (res.pinned ? ' · 📌 pinned' : (res.restyled ? ' · AI restyled' : ''));
    const bubbles = (res.bubbles && res.bubbles.length)
      ? res.bubbles.map((b, i) =>
          '<div class="prev-bubble">'
          + (i === 1 && (res.pinned || res.restyled)
              ? '<span class="prev-tag">' + (res.pinned ? '📌 pinned' : 'AI') + '</span>' : '')
          + Fastt.esc(b) + '</div>').join('')
      : '<div class="prev-bubble">' + Fastt.esc(res.text || '') + '</div>';
    const cap = res.cap_hit
      ? '<div class="prev-cap">Daily AI cap reached — showing the plain template line.</div>' : '';
    box.innerHTML = '<div class="prev-wrap">' + img
      + '<div class="prev-body"><div class="prev-meta">' + meta + '</div>' + cap
      + '<div class="prev-bubbles">' + bubbles + '</div></div></div>';
  }
  function previewErr(box, e, hint) {
    const msg = (e && e.body && (e.body.detail || e.body.error)) || (e && e.message) || 'Preview failed';
    box.innerHTML = '<div class="prev-err">' + Fastt.esc(String(msg))
      + (hint ? '<span class="sub">' + hint + '</span>' : '') + '</div>';
  }

  // enable switches: own their toggle (stopPropagation keeps the fx-js kit from
  // double-toggling), and repaint the Off/Enabled label live.
  [['#wc-enable', '#wc-state-lbl'], ['#fu-enable', '#fu-state-lbl']].forEach(([sw, lbl]) => {
    $(sw).addEventListener('click', (e) => {
      e.stopPropagation();
      const on = $(sw).classList.toggle('on');
      $(lbl).textContent = on ? 'Enabled' : 'Off';
    });
  });

  // ---- Welcome ----
  const wcSlot = $('#wc-slot');
  wcSlot.innerHTML = '<option value="">Current time</option>' +
    (out.slots || []).map((s) => '<option value="' + Fastt.esc(s) + '">'
      + Fastt.esc(SLOT_LABEL[s] || s) + '</option>').join('');
  let wcLast = null;
  function paintWelcome() {
    const r = RULES.send_welcome;
    const on = !!(r && r.is_enabled);
    $('#wc-enable').classList.toggle('on', on);
    $('#wc-state-lbl').textContent = on ? 'Enabled' : 'Off';
    if (document.activeElement !== $('#wc-every')) $('#wc-every').value = everyMin(r && r.every_seconds, 300);
    setStatChip($('#wc-lastrun'), r);
  }
  async function wcRunPreview(ignorePin) {
    const box = $('#wc-preview-box');
    box.innerHTML = '<div class="prev-loading">Composing…</div>';
    try {
      const draft = gather();  // preview the UNSAVED on-screen brain → WYSIWYG
      const res = await Fastt.post('/admin/automation-preview', {
        account_id: acct, kind: 'send_welcome',
        fan_id: $('#wc-fan').value.trim() ? Number($('#wc-fan').value.trim()) : null,
        slot: wcSlot.value || null,
        restyle: $('#wc-restyle').classList.contains('on'),
        config: {
          persona: draft.persona, welcome_rules: draft.welcome_rules, location: draft.location,
          utc_offset: draft.utc_offset, time_activities: draft.time_activities,
          time_images: draft.time_images, model: draft.model,
        },
        ignore_pin: !!ignorePin,
      });
      wcLast = res;
      renderPreview(box, res, 'send_welcome');
      $('#wc-regen').style.display = '';
      const canPin = res.bubbles && res.bubbles.length > 1 && !res.pinned;
      $('#wc-keep').style.display = canPin ? '' : 'none';
      $('#wc-unpin').style.display = res.pinned ? '' : 'none';
    } catch (e) { previewErr(box, e); }
  }
  async function wcKeep() {
    if (!wcLast || !wcLast.slot || !(wcLast.bubbles && wcLast.bubbles[1])) return;
    try {
      await Fastt.post('/admin/welcome-pin', { account_id: acct, slot: wcLast.slot, line: wcLast.bubbles[1] });
      wcSlot.value = wcLast.slot;
      Fastt.saved('Pinned — this line will send for that slot');
      await wcRunPreview(false);
    } catch (e) { Fastt.oops(e); }
  }
  async function wcUnpin() {
    if (!wcLast || !wcLast.slot) return;
    try {
      await Fastt.post('/admin/welcome-pin', { account_id: acct, slot: wcLast.slot, line: null });
      wcSlot.value = wcLast.slot;
      Fastt.saved('Unpinned — back to auto AI lines');
      await wcRunPreview(false);
    } catch (e) { Fastt.oops(e); }
  }
  async function wcSave() {
    const enable = $('#wc-enable').classList.contains('on');
    const was = !!(RULES.send_welcome && RULES.send_welcome.is_enabled);
    if (enable && !was && !confirm(
      'Turn ON Welcome?\n\nEvery new subscriber will be auto-messaged in her voice on the cadence below. '
      + 'This sends real messages to real fans.')) return;
    const every_seconds = Math.max(60, Math.round((Number($('#wc-every').value) || 5) * 60));
    const btn = $('#wc-save'); btn.disabled = true;
    try {
      RULES.send_welcome = await Fastt.upsertRule('send_welcome',
        { every_seconds, is_enabled: enable },
        { name: 'Welcome new subscribers' });
      paintWelcome();
      Fastt.saved(enable ? 'Welcome saved ✓' : 'Welcome saved (off) ✓');
    } catch (e) { Fastt.oops(e); } finally { btn.disabled = false; }
  }
  $('#wc-preview').addEventListener('click', () => wcRunPreview(false));
  $('#wc-regen').addEventListener('click', () => wcRunPreview(true));
  $('#wc-keep').addEventListener('click', wcKeep);
  $('#wc-unpin').addEventListener('click', wcUnpin);
  $('#wc-save').addEventListener('click', wcSave);

  // ---- Follow-ups ----
  const FU_DEF_HOURS = [26, 64, 256];
  function paintFollowup() {
    const r = RULES.send_followup;
    const on = !!(r && r.is_enabled);
    $('#fu-enable').classList.toggle('on', on);
    $('#fu-state-lbl').textContent = on ? 'Enabled' : 'Off';
    if (document.activeElement !== $('#fu-every')) $('#fu-every').value = everyMin(r && r.every_seconds, 2700);
    const p = (r && r.payload) || {};
    const sh = Array.isArray(p.step_hours) && p.step_hours.length === 3 ? p.step_hours : FU_DEF_HOURS;
    ['#fu-h1', '#fu-h2', '#fu-h3'].forEach((id, i) => { if (document.activeElement !== $(id)) $(id).value = sh[i]; });
    $('#fu-image').classList.toggle('on', p.with_image !== false);
    setStatChip($('#fu-lastrun'), r);
  }
  async function fuRunPreview() {
    const box = $('#fu-preview-box');
    box.innerHTML = '<div class="prev-loading">Composing…</div>';
    try {
      const res = await Fastt.post('/admin/automation-preview', {
        account_id: acct, kind: 'send_followup',
        fan_id: $('#fu-fan').value.trim() ? Number($('#fu-fan').value.trim()) : null,
      });
      renderPreview(box, res, 'send_followup');
    } catch (e) {
      previewErr(box, e, 'The follow-up preview always makes a live AI call (there is no free '
        + 'template). If this workspace\'s model isn\'t reachable it fails here — the saved '
        + 'automation still runs on the server.');
    }
  }
  async function fuSave() {
    const enable = $('#fu-enable').classList.contains('on');
    const was = !!(RULES.send_followup && RULES.send_followup.is_enabled);
    if (enable && !was && !confirm(
      'Turn ON Follow-ups?\n\nQuiet fans will be auto-nudged up to three times. '
      + 'This sends real messages to real fans.')) return;
    const every_seconds = Math.max(60, Math.round((Number($('#fu-every').value) || 45) * 60));
    const step_hours = ['#fu-h1', '#fu-h2', '#fu-h3'].map((id) => Math.max(1, Math.round(Number($(id).value) || 1)));
    const with_image = $('#fu-image').classList.contains('on');
    const existing = (RULES.send_followup && RULES.send_followup.payload) || {};
    const btn = $('#fu-save'); btn.disabled = true;
    try {
      RULES.send_followup = await Fastt.upsertRule('send_followup',
        { every_seconds, is_enabled: enable, payload: Object.assign({}, existing, { step_hours, with_image }) },
        { name: 'Follow up quiet fans' });
      paintFollowup();
      Fastt.saved(enable ? 'Follow-ups saved ✓' : 'Follow-ups saved (off) ✓');
    } catch (e) { Fastt.oops(e); } finally { btn.disabled = false; }
  }
  $('#fu-preview').addEventListener('click', fuRunPreview);
  $('#fu-save').addEventListener('click', fuSave);

  await loadRules();
  paintWelcome();
  paintFollowup();
  Fastt.liveBadge($('#card-welcome .ttl'));
  Fastt.liveBadge($('#card-followup .ttl'));
});
