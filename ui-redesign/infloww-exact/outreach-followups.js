Fastt.ready(async function () {
  'use strict';
  var $ = Fastt.$, $$ = Fastt.$$, esc = Fastt.esc;
  var acct = Fastt.account();
  if (!acct) {
    Fastt.staticBadge($('#fu-status'), 'NO ACCOUNT SELECTED');
    return;
  }

  var state = { rule: null, acfg: null };
  async function loadRule() {
    state.rule = await Fastt.rule('send_followup');
  }
  async function loadAcfg() {
    // Full blob — PUT /admin/account-config REPLACES every column it reads, so
    // a slot edit has to hand back persona/model/timezone/… verbatim.
    var res = await Fastt.get('/admin/account-config');
    state.acfg = (res && res.config) || {};
  }
  await Promise.all([loadRule(), loadAcfg().catch(function (e) {
    console.warn('account-config load failed', e); state.acfg = null;
  })]);

  function pl() { return (state.rule && state.rule.payload) || {}; }
  function stepHours() {
    var sh = pl().step_hours;
    return Array.isArray(sh) && sh.length ? [sh[0] || 26, sh[1] || 64, sh[2] || 256] : [26, 64, 256];
  }
  function setSt(el, txt, ok) { el.innerHTML = '<i></i>' + esc(txt); el.classList.toggle('ok', !!ok); }
  function approx(h) {
    var d = h / 24;
    return '≈ ' + (d >= 1 ? (Math.round(d * 2) / 2 + ' day' + (d >= 1.5 ? 's' : '')) : Math.round(h) + ' h');
  }
  function ckSet(id, on) { var el = $('#' + id); if (el) el.classList.toggle('on', !!on); }
  function ckGet(id) { var el = $('#' + id); return !!(el && el.classList.contains('on')); }

  // ── time-of-day photo slots ─────────────────────────────────────────
  // send_followup.py::_slot_image_id reads cfg['time_images'][_slot_key(hour)],
  // with the SAME six buckets as _photo_index. _compose_system feeds
  // cfg['time_activities'][slot] to the prompt as "You are currently …".
  var SLOTS = [
    { key: 'morning_1', nm: 'Morning (early)', hrs: '05–09' },
    { key: 'morning_2', nm: 'Morning (late)', hrs: '09–12' },
    { key: 'afternoon_1', nm: 'Afternoon (early)', hrs: '12–15' },
    { key: 'afternoon_2', nm: 'Afternoon (late)', hrs: '15–18' },
    { key: 'evening', nm: 'Evening', hrs: '18–21' },
    { key: 'night', nm: 'Night', hrs: '21–05' },
  ];
  function slotIdxForHour(h) {
    if (h >= 5 && h < 9) return 0;
    if (h >= 9 && h < 12) return 1;
    if (h >= 12 && h < 15) return 2;
    if (h >= 15 && h < 18) return 3;
    if (h >= 18 && h < 21) return 4;
    return 5;
  }
  function modelHour() {
    var c = state.acfg || {};
    if (c.timezone) {
      try {
        return parseInt(new Intl.DateTimeFormat('en-US',
          { hour: 'numeric', hour12: false, timeZone: c.timezone }).format(new Date()), 10) % 24;
      } catch (e) { /* fall through to the offset */ }
    }
    var off = Number(c.utc_offset);
    if (!isFinite(off)) off = 0;
    return ((new Date().getUTCHours() + off) % 24 + 24) % 24;
  }
  function renderSlots() {
    var host = $('#fu-slots'), hint = $('#fu-slothint');
    if (!host) return;
    if (!state.acfg) {
      host.innerHTML = '';
      hint.textContent = 'Couldn’t read /admin/account-config for this creator — the six slot photos live there, '
        + 'so nothing can be shown or edited here right now.';
      return;
    }
    var imgs = state.acfg.time_images || {};
    var acts = state.acfg.time_activities || {};
    var nowIdx = slotIdxForHour(modelHour());
    var mapped = 0;
    host.innerHTML = SLOTS.map(function (s, i) {
      var id = imgs[s.key];
      if (id) mapped++;
      var isNow = i === nowIdx;
      var tile = id
        ? '<div class="tile live' + (isNow ? ' now' : '') + '" data-slot="' + s.key + '" '
          + 'title="vault media ' + esc(id) + ' — click to change">'
          + '<img src="/admin/vault-ai/thumb?account_id=' + encodeURIComponent(acct)
          + '&media_id=' + encodeURIComponent(id) + '" alt="" loading="lazy">'
          + '<span class="mid">#' + esc(id) + '</span></div>'
        : '<div class="tile empty' + (isNow ? ' now' : '') + '" data-slot="' + s.key + '">+ Pick</div>';
      return '<div class="fu-slot">' + tile
        + '<div class="nm">' + esc(s.nm) + '<span class="hrs">' + esc(s.hrs) + '</span>'
        + (isNow ? '<span class="fu-nowchip">NOW</span>' : '') + '</div>'
        + '<div class="act' + (acts[s.key] ? '' : ' miss') + '">'
        + esc(acts[s.key] || 'no activity line — the prompt just gets the clock')
        + '</div></div>';
    }).join('');
    hint.innerHTML = '<b style="color:#cfd8ff;font-weight:600">' + mapped + ' of 6</b> slots have a vault photo '
      + '(<code style="color:#9db1fb">config.time_images</code>) and the line under each is that slot’s '
      + '<code style="color:#9db1fb">time_activities</code> entry — what she says she’s doing right now. '
      + 'Click a tile to point the slot at a different vault id. '
      + (mapped === 6 ? 'Every slot is mapped, so a nudge always has a photo when the box above is ticked.'
                      : 'An unmapped slot sends text-only at that hour — there is no folder fallback.');
  }
  async function saveSlot(slotKey, mediaId) {
    var cfg = JSON.parse(JSON.stringify(state.acfg || {}));
    cfg.time_images = Object.assign({}, cfg.time_images || {});
    if (mediaId == null) delete cfg.time_images[slotKey];
    else cfg.time_images[slotKey] = mediaId;
    var res = await Fastt.put('/admin/account-config', { account_id: acct, config: cfg });
    state.acfg = (res && res.config) || cfg;
    renderSlots();
  }
  $('#fu-slots').addEventListener('click', async function (e) {
    var tile = e.target.closest('[data-slot]');
    if (!tile) return;
    var key = tile.dataset.slot;
    var cur = ((state.acfg || {}).time_images || {})[key];
    var raw = prompt('Vault media id for the "' + key + '" slot (blank = unmap, text-only at that hour):',
      cur != null ? String(cur) : '');
    if (raw === null) return;
    raw = raw.trim();
    if (raw && !/^\d+$/.test(raw)) { Fastt.toast('That is not a vault media id', 'err'); return; }
    try {
      await saveSlot(key, raw ? Number(raw) : null);
      Fastt.saved(raw ? 'Slot ' + key + ' → #' + raw : 'Slot ' + key + ' cleared');
    } catch (e2) { Fastt.oops(e2); }
  });

  function render() {
    var r = state.rule, p = pl();
    var on = !!(r && r.is_enabled);
    var stats = (r && r.last_run && r.last_run.stats) || {};
    var everyMin = r ? Math.round((r.every_seconds || 2700) / 60) : 45;

    setSt($('#fu-st-run'), on ? 'Running' : (r ? 'Paused — rule disabled' : 'No rule yet on this account'), on);
    setSt($('#fu-st-last'), 'Last check ' + Fastt.fmtAgo(r && r.last_run && r.last_run.started_at), false);
    setSt($('#fu-st-next'), on ? ('Checks every ' + everyMin + ' min') : 'paused', false);
    setSt($('#fu-st-mid'), (stats.eligible != null ? stats.eligible : '—') + ' fans eligible last pass', false);
    setSt($('#fu-st-model'), p.model || 'account default model', false);

    var sw = $('#fu-master-sw'), card = $('#fu-master');
    sw.classList.toggle('on', on);
    card.classList.toggle('on', on);
    $('.fx-tc-state', card).textContent = on ? 'Running' : 'Off';
    ckSet('fu-ck-image-top', p.with_image !== false);
    ckSet('fu-ck-image', p.with_image !== false);
    $('#fu-kv-stats').innerHTML = '<b>' + esc(stats.eligible != null ? stats.eligible : '—') +
      '</b> eligible · <b>' + esc(stats.followups_sent != null ? stats.followups_sent : '—') +
      '</b> sent last pass · <b>' + esc(stats.state_advanced != null ? stats.state_advanced : '—') + '</b> advanced';

    // Why the eligible pool didn't all convert — grounded in last_run.stats.
    // The real app dumps raw numbers; this names each skip reason so a "111
    // eligible, 0 sent" pass explains itself at a glance.
    var lp = $('#fu-lastpass');
    if (r && r.last_run && r.last_run.stats) {
      var chip = function (n, label, cls) {
        if (!n) return '';
        return '<span class="fu-lp ' + (cls || '') + '"><b>' + esc(n) + '</b> ' + esc(label) + '</span>';
      };
      var parts = [
        chip(stats.image_attached, 'with photo'),
        chip(stats.skipped_unread, 'skipped · unread reply', 'skip'),
        chip(stats.skipped_guard, 'skipped · messaged recently', 'skip'),
        chip(stats.skipped_cooldown, 'skipped · resting', 'skip'),
        chip(stats.skipped_locked, 'skipped · in another flow', 'skip'),
        (stats.errors ? '<span class="fu-lp skip"><b>' + esc(stats.errors) + '</b> errors</span>' : ''),
        (stats.cap_hit ? '<span class="fu-lp cap">daily AI budget hit — pass cut short</span>' : ''),
        (stats.duration_ms != null ? '<span class="fu-lp">ran in <b>' + esc(stats.duration_ms) + '</b> ms</span>' : ''),
      ].filter(Boolean);
      // When nothing sent but fans were eligible, lead with the plain reason.
      if (!stats.followups_sent && stats.eligible && !parts.some(function (p) { return p.indexOf('skipped') > -1; })) {
        parts.unshift('<span class="fu-lp">all eligible fans were still inside a timer — none were due yet</span>');
      }
      lp.innerHTML = parts.join('');
    } else {
      lp.innerHTML = '<span class="fu-lp">no pass has run on this account yet</span>';
    }

    var sh = stepHours();
    [1, 2, 3].forEach(function (i) {
      $('#fu-h' + i).innerHTML = esc(sh[i - 1]) + '&nbsp;h';
      $('#fu-ap' + i).textContent = approx(sh[i - 1]);
      $('#fu-t' + i).value = sh[i - 1];
    });
    $('#fu-every').value = everyMin;
    $('#fu-guard').value = p.exclude_replied_hours != null ? p.exclude_replied_hours : 12;
    $('#fu-limit').value = p.limit != null ? p.limit : 200;
    var modelSel = $('#fu-model');
    var want = p.model || 'deepseek-v4-flash';
    var found = false;
    $$('option', modelSel).forEach(function (o) { if (o.value === want) { o.selected = true; found = true; } });
    if (!found) {
      var o = document.createElement('option');
      o.value = want; o.textContent = want + ' (from rule)'; o.selected = true;
      modelSel.appendChild(o);
    }
    $('#fu-savecap').textContent = 'Changes apply on the next check — within ' + everyMin + ' min.';
  }

  function collect() {
    var num = function (id, fb) { var n = parseInt($('#' + id).value, 10); return isFinite(n) && n > 0 ? n : fb; };
    var payload = Object.assign({}, pl(), {
      step_hours: [num('fu-t1', 26), num('fu-t2', 64), num('fu-t3', 256)],
      with_image: ckGet('fu-ck-image'),
      limit: num('fu-limit', 200),
      exclude_replied_hours: num('fu-guard', 12),
      model: $('#fu-model').value,
    });
    return { payload: payload, every_seconds: Math.max(60, num('fu-every', 45) * 60) };
  }

  // keep the two "attach a photo" checks mirrored (both write payload.with_image on Save)
  document.addEventListener('click', function (e) {
    if (e.target.closest('#fu-ck-image')) ckSet('fu-ck-image-top', ckGet('fu-ck-image'));
    else if (e.target.closest('#fu-ck-image-top')) ckSet('fu-ck-image', ckGet('fu-ck-image-top'));
  });

  $('#fu-save').addEventListener('click', async function () {
    var btn = $('#fu-save');
    btn.disabled = true;
    try {
      var body = collect();
      if (state.rule) {
        await Fastt.patch('/admin/automation-rules/' + state.rule.id,
          { payload: body.payload, every_seconds: body.every_seconds });
      } else {
        // create DISABLED — enabling is only ever the explicit toggle
        await Fastt.post('/admin/automation-rules', {
          account_id: acct, kind: 'send_followup', name: 'send_followup',
          every_seconds: body.every_seconds, payload: body.payload, is_enabled: false,
        });
      }
      await loadRule(); render();
      Fastt.saved();
    } catch (e) { Fastt.oops(e); }
    btn.disabled = false;
  });

  // document-level so it runs AFTER fx-js has flipped the visual state
  document.addEventListener('click', async function (e) {
    if (!e.target.closest('#fu-master-sw')) return;
    var sw = $('#fu-master-sw');
    var wantOn = sw.classList.contains('on');
    var revert = function () { render(); };
    if (wantOn && !confirm('Enable the follow-up drip for this account?\nOnce running it WILL message quiet fans on its own.')) { revert(); return; }
    try {
      if (state.rule) {
        await Fastt.patch('/admin/automation-rules/' + state.rule.id, { is_enabled: wantOn });
      } else {
        var body = collect();
        await Fastt.post('/admin/automation-rules', {
          account_id: acct, kind: 'send_followup', name: 'send_followup',
          every_seconds: body.every_seconds, payload: body.payload, is_enabled: wantOn,
        });
      }
      await loadRule(); render();
      Fastt.saved(wantOn ? 'Follow-ups enabled' : 'Follow-ups disabled');
    } catch (e) { Fastt.oops(e); revert(); }
  });

  var previewPanel = $('#fu-preview-panel'), ppBody = $('#fu-pp-body'), ppMeta = $('#fu-pp-meta');
  $('#fu-pp-x').addEventListener('click', function () { previewPanel.hidden = true; });
  $('#fu-preview').addEventListener('click', async function () {
    var btn = $('#fu-preview');
    btn.disabled = true;
    previewPanel.hidden = false;
    ppMeta.textContent = '';
    ppBody.innerHTML = '<div class="fu-pp-loading"><span class="fu-pp-spin"></span>'
      + 'Writing a fresh nudge with the AI — this makes one cap-governed model call…</div>';
    try {
      // compose-only (LLM call, cap-governed) — nothing is sent, no state written
      var res = await Fastt.post('/admin/automation-preview', { account_id: acct, kind: 'send_followup' });
      ppMeta.textContent = '· nudge ' + (res.step || 1) + ' · ' + (res.slot || 'slot') + ' tone';
      var photo = '';
      if (res.image) {
        photo = '<div class="fu-pp-photo"><img src="/admin/vault-ai/thumb?account_id='
          + encodeURIComponent(acct) + '&media_id=' + encodeURIComponent(res.image)
          + '" alt="">attaches this hour’s slot photo (#' + esc(res.image) + ')</div>';
      } else {
        photo = '<div class="fu-pp-photo">text-only — no photo mapped for this hour</div>';
      }
      ppBody.innerHTML = '<div class="fu-pp-msg"><span class="fu-pp-av"></span>'
        + '<div class="fu-pp-bubble">' + esc(res.text || '(the model returned no text)') + '</div></div>' + photo;
    } catch (e) {
      // Surface the real reason. A 500 here is almost always the account's
      // resolved follow-up model being one the live API rejects — actionable.
      var detail = (e && e.body && (e.body.detail || e.body.error)) || (e && e.message) || 'Preview failed';
      var hint = (e && e.status === 500)
        ? 'The compose call failed on the server — most often the follow-up model set for this creator isn’t one the live API accepts. Check the model in Advanced → AI model, save, and retry.'
        : esc(String(detail).slice(0, 200));
      ppBody.innerHTML = '<div class="fu-pp-err"><b>Couldn’t compose a sample.</b> ' + hint + '</div>';
    }
    btn.disabled = false;
  });

  // step-card checkboxes have no backend (steps 1-3 are fixed) — leave them inert
  $$('.fu-step .fx-check').forEach(function (ck) { ck.style.pointerEvents = 'none'; });

  Fastt.liveBadge($('#fu-status'));
  Fastt.liveBadge($('#fu-master .fx-tc-title'));
  Fastt.liveBadge($('#fu-savebar'));
  Fastt.staticBadge($('#fu-maxseq-field > label'), 'FIXED');
  Fastt.staticBadge($('#fu-creativity-field > label'), 'FIXED');
  var advGroups = $$('.fx-adv-group > h4');
  advGroups.forEach(function (h) {
    var t = h.textContent.trim();
    if (t === 'Who qualifies' || t === 'After a reply') Fastt.staticBadge(h, 'FIXED IN CODE');
    else Fastt.liveBadge(h);
  });
  render();
  renderSlots();
  // The three step cards say "+ a photo picked for the time of day" — show the
  // slot that is actually current, not a gradient.
  (function paintStepThumbs() {
    var cfg = state.acfg || {};
    var id = (cfg.time_images || {})[SLOTS[slotIdxForHour(modelHour())].key];
    $$('.fu-attach').forEach(function (row) {
      var th = row.querySelector('.th');
      if (!th) return;
      if (id) {
        th.className = 'th';
        th.style.cssText = 'background:#161616;overflow:hidden';
        th.innerHTML = '<img src="/admin/vault-ai/thumb?account_id=' + encodeURIComponent(acct)
          + '&media_id=' + encodeURIComponent(id) + '" alt="" '
          + 'style="width:100%;height:100%;object-fit:cover;display:block">';
        row.lastChild.textContent = ' + this hour’s slot photo (#' + id + ')';
      } else {
        th.className = 'th';
        th.style.cssText = 'background:#181818;border-style:dashed';
        row.lastChild.textContent = ' + no photo for this hour — text-only';
      }
    });
  })();
});
