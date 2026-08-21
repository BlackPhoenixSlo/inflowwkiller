Fastt.ready(async function () {
  'use strict';
  var acct = Fastt.account();
  var esc = Fastt.esc;

  function stripSet(id, txt, cls) {
    var el = document.getElementById(id);
    if (!el) return;
    el.innerHTML = '<i></i>' + esc(txt);
    if (cls !== undefined) el.className = 'fx-st' + (cls ? ' ' + cls : '');
  }

  if (!acct) {
    // No creator selected — the static demo cards must not sit there looking
    // clickable. Honest empty state instead; badge what stays decorative.
    ['rm-st-state', 'rm-st-wait', 'rm-st-time', 'rm-st-folder'].forEach(function (id) {
      stripSet(id, 'No creator selected', '');
    });
    var q0 = document.getElementById('rm-queue');
    if (q0) q0.innerHTML = '<div class="rc-card" style="grid-column:1/-1;justify-content:center;align-items:center;display:flex;min-height:120px;color:#8a8a8a;font-size:13px">No creator selected — click the creator name (top-left) to sign in and pick one.</div>';
    var pl0 = document.getElementById('rm-planner');
    if (pl0) Fastt.staticBadge(pl0.querySelector('.rc-phead .t') || pl0, 'STATIC DEMO');
    var hf0 = document.getElementById('rm-handsfree');
    if (hf0) Fastt.staticBadge(hf0.querySelector('h4') || hf0, 'NOT WIRED');
    // The master card's switch still flips visually via the fx kit but saves
    // nothing without a creator — say so instead of letting it read as state.
    var c0 = document.getElementById('rm-card');
    if (c0) Fastt.staticBadge(c0.querySelector('.fx-tc-title'), 'NO CREATOR — INERT');
    var kv0 = document.getElementById('rm-kv');
    if (kv0) kv0.textContent = 'Pick a creator (top-left) to load the real reminder settings.';
    return;
  }
  function thumbUrl(mid) {
    return '/admin/vault-ai/thumb?account_id=' + encodeURIComponent(acct) + '&media_id=' + encodeURIComponent(mid);
  }

  var results = await Promise.all([
    Fastt.get('/admin/account-ai-config/vault-ai'),
    Fastt.get('/admin/vault-ai/review'),
    Fastt.rule('vault_daily_reminder'),
    Fastt.get('/admin/vault-ai/folders'),
    Fastt.get('/admin/vault-ai/of-folders'),
  ]);
  var cfg = (results[0] && results[0].config) || {};
  var review = results[1] || {};
  var rule = results[2];
  var aiFolders = (results[3] && results[3].folders) || [];
  var ofFolders = (results[4] && results[4].list) || [];
  var dr = cfg.daily_reminder || {};
  var cards = review.reminder || [];

  // Times come from the RULE trigger (trigger.daily_at — the executor's
  // clock-time mode), NOT from config daily_reminder.daily_at: nothing in
  // service/ reads that config key, so it would be a decorative lie.
  function ruleTimes() {
    var t = rule && rule.trigger;
    return (t && t.daily_at && t.daily_at.length) ? t.daily_at.slice() : [];
  }
  function timesLabel() {
    var ts = ruleTimes();
    if (ts.length) return 'Daily card at ' + ts.join(' · ');
    return rule ? 'Cards tick hourly — no clock time set' : 'No schedule row yet';
  }

  // ── status strip (LIVE) ──────────────────────────────────────
  var on = !!dr.enabled;
  var ruleOn = !!(rule && rule.is_enabled);
  stripSet('rm-st-state', on ? (ruleOn ? 'On — cards are produced daily' : 'On (no schedule row yet)') : 'Off — nothing sends', on ? 'ok' : '');
  stripSet('rm-st-wait', cards.length ? cards.length + ' card' + (cards.length > 1 ? 's' : '') + ' waiting for you' : 'No cards waiting', cards.length ? 'warn' : '');
  stripSet('rm-st-time', timesLabel());
  stripSet('rm-st-folder', dr.folder_name ? 'Folder: ' + dr.folder_name : 'Folder: not set', dr.folder_name ? '' : 'warn');
  Fastt.liveBadge(document.getElementById('rm-status'));

  // ── master toggle (LIVE — config daily_reminder.enabled + rule) ─
  var card = document.getElementById('rm-card');
  var sw = document.getElementById('rm-switch');
  function paintMaster(o) {
    if (!card || !sw) return;
    card.classList.toggle('on', o);
    sw.classList.toggle('on', o);
    var st = card.querySelector('.fx-tc-state');
    if (st) st.textContent = o ? 'Running' : 'Off';
  }
  paintMaster(on);
  var desc = document.getElementById('rm-desc');
  if (desc) desc.innerHTML = 'Every day fastt picks <b style="color:#ddd">' + esc(dr.images_per_day || 2) +
    ' photo' + ((dr.images_per_day || 2) > 1 ? 's' : '') + ' your fans haven’t seen</b> from your <b style="color:#ddd">' +
    esc(dr.folder_name || '(no folder set)') + '</b> folder, pairs them with one of your lines, and drops a card below. You press Approve — only then does it go out.';
  var kv = document.getElementById('rm-kv');
  if (kv) kv.innerHTML = 'Next card at <b>' + esc(ruleTimes()[0] || (rule ? 'next hourly tick' : 'no schedule yet')) +
    '</b> · goes out via the mass-broadcast path minus your exclude list · per-fan cooldown <b>' + esc(dr.per_fan_cooldown_hours || 48) + ' h</b>';
  if (card) Fastt.liveBadge(card.querySelector('.fx-tc-title'));

  async function patchDr(partial, msg) {
    try {
      var out = await Fastt.patch('/admin/account-ai-config/vault-ai', { account_id: acct, config: { daily_reminder: partial } });
      cfg = (out && out.config) || cfg;
      dr = cfg.daily_reminder || dr;
      Fastt.saved(msg || 'Saved ✓');
    } catch (e) { Fastt.oops(e); }
  }

  if (sw) sw.addEventListener('click', async function () {
    var o = !sw.classList.contains('on'); // fx kit paints after this handler
    await patchDr({ enabled: o }, o ? 'Daily reminder cards ON (suggest-only)' : 'Daily reminder cards OFF');
    try {
      await Fastt.upsertRule('vault_daily_reminder', { is_enabled: o }, {
        name: 'Vault daily reminder', every_seconds: 3600, payload: {}, is_enabled: o,
      });
    } catch (e) { Fastt.oops(e); }
  });

  // ── approval queue (LIVE; approve/skip fire only on click) ───
  var queue = document.getElementById('rm-queue');
  var FOLD_ICO = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M4 7a2 2 0 0 1 2-2h4l2 2h6a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V7z"/></svg>';
  var CLOCK_ICO = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="12" cy="12" r="9"/><path d="M12 7.5V12l3 2" stroke-linecap="round"/></svg>';
  var OK_ICO = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><path d="M20 6L9 17l-5-5" stroke-linecap="round" stroke-linejoin="round"/></svg>';
  function cardHtml(it, idx) {
    var p = it.payload || {};
    var mids = p.media_ids || [];
    // created_at is tz-NAIVE UTC — parse it through Fastt.parseUtc or a card
    // drafted near midnight UTC shows the wrong weekday.
    var dt = Fastt.parseUtc(it.created_at);
    var day = dt ? dt.toLocaleDateString(undefined, { weekday: 'short' }) : '';
    var thumbs = mids.slice(0, 2).map(function (m) {
      return '<div class="rc-thumb" style="background-image:url(\'' + esc(thumbUrl(m)) + '\');background-size:cover;background-position:center"></div>';
    }).join('');
    return '<div class="rc-card' + (idx === 0 ? ' today' : '') + '" data-rid="' + esc(it.id) + '">' +
      '<div class="rc-top"><span class="rc-day">' + (idx === 0 ? 'Today <small>· ' + esc(day) + '</small>' : esc(day)) + '</span>' +
      '<span class="rc-folder">' + FOLD_ICO + esc(p.folder_name || '—') + '</span></div>' +
      '<div class="rc-thumbs"><span class="rc-unseen">Unseen</span>' + thumbs + '</div>' +
      '<div class="rc-line">' + esc(p.line || '(no line)') + '</div>' +
      '<div class="rc-meta">' + CLOCK_ICO + 'goes out ' + esc(ruleTimes()[0] || 'on the next enabled run') + ' · ' + esc(mids.length) + ' image' + (mids.length === 1 ? '' : 's') + '</div>' +
      '<div class="rc-actions">' +
      '<button class="fx-btn green" data-act="approve">' + OK_ICO + 'Approve</button>' +
      '<button class="fx-btn ghost" data-act="skip">Skip</button></div></div>';
  }
  function renderQueue() {
    if (!queue) return;
    if (!cards.length) {
      queue.innerHTML = '<div class="rc-card" style="grid-column:1/-1;justify-content:center;align-items:center;display:flex;min-height:120px;color:#8a8a8a;font-size:13px">' +
        'No reminder cards waiting. ' + (on ? 'The producer drops one each day it runs.' : 'Turn the automation on and the producer will draft one per day.') + '</div>';
      return;
    }
    queue.innerHTML = cards.map(cardHtml).join('');
  }
  renderQueue();
  var qhead = document.getElementById('rm-qhead');
  if (qhead) Fastt.liveBadge(qhead);

  if (queue) queue.addEventListener('click', async function (ev) {
    var btn = ev.target.closest('button[data-act]');
    if (!btn) return;
    var cardEl = btn.closest('.rc-card');
    var rid = cardEl && Number(cardEl.dataset.rid);
    if (!rid) return;
    var approve = btn.dataset.act === 'approve';
    var q = approve
      ? 'Approve this reminder card? A consumer automation will send it via the mass-broadcast path (with exclude list + cooldowns).'
      : 'Skip this card? The photos go straight back into the pool.';
    if (!confirm(q)) return;
    try {
      var out = await Fastt.post('/admin/vault-ai/review/' + (approve ? 'approve' : 'reject'),
        { account_id: acct, ids: [rid] });
      if (approve && out.stale && out.stale.length) {
        Fastt.toast('Card is stale — the vault moved since it was drafted. It stays pending.', 'err');
        return;
      }
      Fastt.saved(approve ? 'Card approved' : 'Card skipped');
      cards = cards.filter(function (c) { return c.id !== rid; });
      renderQueue();
      stripSet('rm-st-wait', cards.length ? cards.length + ' cards waiting for you' : 'No cards waiting', cards.length ? 'warn' : '');
    } catch (e) { Fastt.oops(e); }
  });

  // ══════════════════════════════════════════════════════════════
  // PPV arc — read-only preview + the ARMING panel.
  //
  // The mockup's 7-slot Mon…Sun strip has no JSON source: service/
  // vault_ppv_week.py builds days[]/arc[] but scripts_api.py serializes only
  // {summary, coverage, html}. So the strip stays hidden and the region shows
  // the endpoint's OWN generated plan (real data, self-contained) instead of a
  // sentence — plus the arming controls, which had no consumer anywhere.
  // ══════════════════════════════════════════════════════════════
  var reloadPreview = null; // set by the planner IIFE; band edits below re-price the arc
  (function () {
    var planner = document.getElementById('rm-planner');
    var frame = document.querySelector('#rm-frame-wrap .rc-if');
    var frameWrap = document.getElementById('rm-frame-wrap');
    var statsEl = document.getElementById('rm-plan-stats');
    var subEl = document.getElementById('rm-plan-sub');
    var weeksSel = document.getElementById('rm-weeks');
    var voiceChk = document.getElementById('rm-voice');
    var combChk = document.getElementById('rm-combine');
    var armState = document.getElementById('rm-armstate');
    var armBtn = document.getElementById('rm-armbtn');
    var cancelBtn = document.getElementById('rm-cancelbtn');
    var rebuildBtn = document.getElementById('rm-rebuild');

    var pw = cfg.ppv_week || {};
    var arcWeeks = Math.max(1, Math.min(4, Number(pw.weeks) || 1));
    var voiceOn = pw.in_voice_copy !== false;
    var combOn = pw.combine_feed_and_dm !== false;
    if (weeksSel) weeksSel.value = String(arcWeeks);
    if (voiceChk) voiceChk.classList.toggle('on', voiceOn);
    if (combChk) combChk.classList.toggle('on', combOn);

    async function patchVai(partial, msg) {
      try {
        var out = await Fastt.patch('/admin/account-ai-config/vault-ai',
          { account_id: acct, config: { ppv_week: partial } });
        cfg = (out && out.config) || cfg;
        pw = cfg.ppv_week || pw;
        Fastt.saved(msg || 'Saved ✓');
      } catch (e) { Fastt.oops(e); }
    }

    function chip(txt, cls) { return '<span class="fx-st' + (cls ? ' ' + cls : '') + '"><i></i>' + esc(txt) + '</span>'; }

    var lastPlan = null;
    async function loadPreview(useLlm) {
      if (statsEl) statsEl.innerHTML = chip(useLlm ? 'Writing in-voice copy…' : 'Building the arc…');
      if (rebuildBtn) rebuildBtn.disabled = true;
      try {
        var p = await Fastt.get('/admin/vault-ai/ppv-week',
          { weeks: arcWeeks, use_llm: !!useLlm, combine: combOn });
        lastPlan = p;
        var sm = p.summary || {}, cov = p.coverage || {};
        // A WEEK plan and a MONTH plan carry DIFFERENT summary keys
        // (plan_week has selling_days/preview_unsafe_days/recap_reused;
        // plan_month has weeks/dm_sends/feed_posts instead). Render only the
        // keys the response actually has — never a 0 for a missing field.
        var shape = (sm.selling_days != null)
          ? (sm.days || 0) + ' days · ' + sm.selling_days + ' selling'
          : (sm.days || 0) + ' days · ' + (sm.weeks || arcWeeks) + ' waves';
        if (subEl) subEl.textContent = shape + ' · ' +
          Fastt.fmtCents(sm.price_low_cents) + '–' + Fastt.fmtCents(sm.price_high_cents);
        if (statsEl) statsEl.innerHTML =
          chip(shape) +
          chip((sm.media_bound || 0) + ' media-bound', (sm.media_bound || 0) ? 'ok' : 'warn') +
          chip((sm.thin_days || 0) + ' thin day' + ((sm.thin_days || 0) === 1 ? '' : 's'), (sm.thin_days || 0) ? 'warn' : '') +
          (sm.preview_unsafe_days ? chip(sm.preview_unsafe_days + ' preview-unsafe', 'err') : '') +
          (sm.dm_sends != null ? chip(sm.dm_sends + ' DM sends · ' + (sm.feed_posts || 0) + ' feed posts') : '') +
          (sm.priced_sends != null ? chip(sm.priced_sends + ' priced · ' + (sm.free_sends || 0) + ' free') : '') +
          chip((cov.placeable || 0) + ' of ' + (cov.items_total || 0) + ' items placeable', (cov.placeable || 0) ? '' : 'warn') +
          chip((cov.with_flags || 0) + ' flag-read · ' + (cov.rich_taxonomy || 0) + ' rich taxonomy') +
          chip('price band ' + Fastt.fmtCents(sm.price_low_cents) + '–' + Fastt.fmtCents(sm.price_high_cents)) +
          chip(useLlm ? 'copy: in-voice (LLM)' : 'copy: template (free)') +
          // `ppv_week.enabled` is stored config with no server-side consumer —
          // the Arm button is what actually books drops. Say so, don't fake a switch.
          chip('arc flag: ' + (pw.enabled ? 'on' : 'off') + ' (stored only)');
        if (frame) frame.srcdoc = p.html || '<p>No preview.</p>';
        if (frameWrap) frameWrap.style.display = '';
        if (planner) Fastt.liveBadge(planner.querySelector('.rc-phead .t') || planner);
      } catch (e) {
        if (statsEl) statsEl.innerHTML = chip('Preview unavailable: ' +
          ((e.body && (e.body.detail || e.body.error)) || e.message), 'err');
        if (frameWrap) frameWrap.style.display = 'none';
        if (planner) Fastt.staticBadge(planner.querySelector('.rc-phead .t') || planner, 'UNAVAILABLE');
      } finally { if (rebuildBtn) rebuildBtn.disabled = false; }
    }

    function paintArm(st) {
      if (!armState) return;
      if (st && st.active) {
        armState.classList.add('on');
        var nextAt = st.next_at ? Fastt.fmtDate(st.next_at) : '—';
        armState.innerHTML = 'Armed &middot; <b>' + Fastt.fmtInt((st.drops || []).length) + ' drops</b> booked &middot; ' +
          Fastt.fmtInt(st.fired || 0) + ' fired, ' + Fastt.fmtInt(st.pending || 0) + ' pending<br>next ' + esc(nextAt);
        if (armBtn) armBtn.textContent = 'Re-arm this arc…';
        if (cancelBtn) cancelBtn.style.display = '';
      } else {
        armState.classList.remove('on');
        armState.innerHTML = 'Not armed &mdash; <b>nothing is booked</b>' +
          (cfg.enabled ? '' : '<br>Vault AI is OFF: arming returns <b>vault_ai_off</b>');
        if (armBtn) armBtn.textContent = 'Arm this arc…';
        if (cancelBtn) cancelBtn.style.display = 'none';
      }
    }
    async function loadArmState() {
      try { paintArm(await Fastt.get('/admin/vault-ai/ppv-week/status')); }
      catch (e) { if (armState) armState.textContent = 'Arc state unavailable'; }
    }

    // The first preview is deliberately the FREE path (use_llm=false): the
    // in-voice pass makes ~7 model calls per week and must never fire just
    // because a page was opened.
    loadPreview(false);
    loadArmState();
    reloadPreview = loadPreview; // band editor (below) calls this to re-price

    if (weeksSel) weeksSel.addEventListener('change', function () {
      arcWeeks = Math.max(1, Math.min(4, Number(weeksSel.value) || 1));
      patchVai({ weeks: arcWeeks }, 'Arc length: ' + arcWeeks + ' week' + (arcWeeks === 1 ? '' : 's'));
      loadPreview(false);
    });
    if (voiceChk) voiceChk.addEventListener('click', function () {
      voiceOn = !voiceChk.classList.contains('on'); // fx kit paints after us
      patchVai({ in_voice_copy: voiceOn },
        voiceOn ? 'In-voice copy ON — press “Rebuild preview” to see it' : 'In-voice copy OFF (template captions)');
    });
    if (combChk) combChk.addEventListener('click', function () {
      combOn = !combChk.classList.contains('on');
      patchVai({ combine_feed_and_dm: combOn },
        combOn ? 'Each drop = paid post + mass PPV' : 'DM only — no feed post');
      loadPreview(false);
    });
    if (rebuildBtn) rebuildBtn.addEventListener('click', function () {
      if (voiceOn && !confirm('Rebuild the preview with in-voice copy?\n\nThat writes every caption through the model — about ' +
        (7 * arcWeeks) + ' calls. Still read-only: nothing is armed or sent.')) return;
      loadPreview(voiceOn);
    });

    if (armBtn) armBtn.addEventListener('click', async function () {
      if (!confirm('Arm this ' + arcWeeks + '-week arc?\n\n' +
        'Every day of the arc is BOOKED as a future-dated drop — real paid posts and/or mass PPVs to your fans, ' +
        'on the schedule you just previewed. Nothing sends right now, but the week then runs itself.\n\n' +
        'You can stand it down at any time; drops that already fired stay sent.')) return;
      armBtn.disabled = true;
      try {
        var r = await Fastt.post('/admin/vault-ai/ppv-week/approve',
          { account_id: acct, weeks: arcWeeks, use_llm: voiceOn, combine: combOn });
        Fastt.saved('Arc armed · ' + Fastt.fmtInt((r.drops || []).length) + ' drops booked');
        await loadArmState();
      } catch (e) {
        var detail = String((e.body && (e.body.detail || e.body.error)) || '');
        var reasons = {
          vault_ai_off: 'Vault AI is off for this creator — turn it on under Auto-describe, then arm the arc.',
          ppv_library_master_off: 'The PPV Library master switch is off — an armed arc would send nothing. Turn on PPV Library first, then arm.',
          nothing_to_schedule: 'Nothing to schedule — the arc came back empty (no placeable, described vault items). Describe more of the vault, then rebuild.',
        };
        if (e.status === 409 && reasons[detail]) Fastt.toast(reasons[detail], 'err');
        else Fastt.oops(e);
      } finally { armBtn.disabled = false; }
    });
    if (cancelBtn) cancelBtn.addEventListener('click', async function () {
      if (!confirm('Stand the arc down?\n\nEvery drop that has not fired yet is cancelled. Drops already sent stay sent.')) return;
      cancelBtn.disabled = true;
      try {
        var r = await Fastt.post('/admin/vault-ai/ppv-week/cancel', { account_id: acct });
        Fastt.saved('Arc stood down' + (r && r.cancelled != null ? ' · ' + Fastt.fmtInt(r.cancelled) + ' drops cancelled' : ''));
        await loadArmState();
      } catch (e) { Fastt.oops(e); } finally { cancelBtn.disabled = false; }
    });
  })();

  // ── advanced knobs (LIVE → daily_reminder.*) ─────────────────
  var fold = document.getElementById('rm-fold');
  if (fold) {
    var names = [];
    aiFolders.forEach(function (f) { if (f.name) names.push(f.name); });
    ofFolders.forEach(function (f) { if (f.name && names.indexOf(f.name) < 0) names.push(f.name); });
    var opts = '<option value="">— not set —</option>';
    if (dr.folder_name && names.indexOf(dr.folder_name) < 0) names.unshift(dr.folder_name);
    names.forEach(function (n) { opts += '<option value="' + esc(n) + '">' + esc(n) + '</option>'; });
    fold.innerHTML = opts;
    fold.value = dr.folder_name || '';
    fold.addEventListener('change', function () {
      patchDr({ folder_name: fold.value }, fold.value ? 'Reminder folder: ' + fold.value : 'Reminder folder cleared');
    });
  }
  var imgs = document.getElementById('rm-imgs');
  if (imgs) {
    imgs.value = String(dr.images_per_day || 2);
    imgs.addEventListener('change', function () {
      var n = Math.max(1, Math.round(Number(imgs.value) || 2));
      imgs.value = String(n);
      patchDr({ images_per_day: n }, n + ' images per day');
    });
  }
  // Time chips drive the RULE trigger (trigger.daily_at → one fire per slot
  // per day; automation_rules_api._validate_trigger). Empty selection falls
  // back to the hourly tick. A rule created here is ALWAYS disabled — only
  // the master switch above flips is_enabled.
  var timesEl = document.getElementById('rm-times');
  if (timesEl) {
    var chipTimes = ruleTimes();
    function paintChips() {
      timesEl.querySelectorAll('.fx-chip').forEach(function (ch) {
        ch.classList.toggle('on', chipTimes.indexOf(ch.dataset.time) >= 0);
      });
    }
    paintChips();
    timesEl.addEventListener('click', async function (ev) {
      var ch = ev.target.closest('.fx-chip');
      if (!ch) return;
      ev.stopPropagation(); // we repaint from saved state; keep the fx kit out
      var t = ch.dataset.time;
      var i = chipTimes.indexOf(t);
      if (i >= 0) chipTimes.splice(i, 1); else { chipTimes.push(t); chipTimes.sort(); }
      var trig = chipTimes.length
        ? { daily_at: chipTimes.slice(), tz_offset_minutes: -new Date().getTimezoneOffset() }
        : { every_seconds: 3600 };
      try {
        rule = await Fastt.upsertRule('vault_daily_reminder', { trigger: trig }, {
          name: 'Vault daily reminder', payload: {}, is_enabled: false,
        });
        paintChips();
        stripSet('rm-st-time', timesLabel());
        Fastt.saved(chipTimes.length ? 'Card drops at ' + chipTimes.join(', ') : 'No clock time — hourly tick');
      } catch (e) { chipTimes = ruleTimes(); paintChips(); Fastt.oops(e); }
    });
  }
  var lines = document.getElementById('rm-lines');
  if (lines) {
    lines.value = (dr.lines || []).join('\n');
    lines.addEventListener('change', function () {
      var ls = lines.value.split('\n').map(function (s) { return s.trim(); }).filter(Boolean);
      patchDr({ lines: ls }, ls.length + ' line' + (ls.length === 1 ? '' : 's') + ' saved');
    });
  }
  var rep = document.getElementById('rm-rep');
  if (rep) {
    rep.value = String(dr.repeat_after_days || 30);
    rep.addEventListener('change', function () {
      var n = Math.max(1, Math.round(Number(rep.value) || 30));
      rep.value = String(n);
      patchDr({ repeat_after_days: n }, 'Repeat window: ' + n + ' days');
    });
  }
  var cool = document.getElementById('rm-cool');
  if (cool) {
    cool.value = String(dr.per_fan_cooldown_hours || 48);
    cool.addEventListener('change', function () {
      var n = Math.max(0, Math.round(Number(cool.value) || 48));
      cool.value = String(n);
      patchDr({ per_fan_cooldown_hours: n }, 'Per-fan cooldown: ' + n + ' h');
      var rot = document.getElementById('rm-rot-kv');
      if (rot) rot.innerHTML = 'A photo is never shown to the same fans twice inside the window · a fan who got any reminder in the last <b>' + n + ' h</b> is skipped automatically.';
    });
    var rot0 = document.getElementById('rm-rot-kv');
    if (rot0) rot0.innerHTML = 'A photo is never shown to the same fans twice inside the window · a fan who got any reminder in the last <b>' + esc(dr.per_fan_cooldown_hours || 48) + ' h</b> is skipped automatically.';
  }
  var low = document.getElementById('rm-low');
  if (low) {
    low.value = dr.on_under_min_unseen || 'use_fewer';
    low.addEventListener('change', function () {
      patchDr({ on_under_min_unseen: low.value }, 'Low-photos behaviour saved');
    });
  }

  // ── PPV price bands per explicitness tier (LIVE → pricing.bands_by_tier + tier_labels) ──
  // These bands are what the week planner draws every PPV price from, so this is
  // where the arc's "$5.99–$14.99" summary is actually set. Lives in Advanced
  // (rare/expert knob) and re-prices the preview on save.
  (function () {
    var bandBody = document.getElementById('rm-bands-body');
    if (!bandBody) return;
    var pricing = cfg.pricing || {};
    var bands = pricing.bands_by_tier || {};
    var labels = cfg.tier_labels || {};
    var ORDER = ['safe', 'suggestive', 'explicit', 'graphic', 'unknown'];
    var tiers = ORDER.filter(function (t) { return bands[t]; });
    Object.keys(bands).forEach(function (t) { if (tiers.indexOf(t) < 0) tiers.push(t); });
    if (!tiers.length) {
      bandBody.innerHTML = '<div class="fx-kv">No price bands configured for this creator.</div>';
      Fastt.staticBadge(document.getElementById('rm-bands').querySelector('h4'), 'NO BANDS');
      return;
    }
    var dollars = function (c) { return (Math.max(0, Number(c) || 0) / 100).toFixed(2); };
    function rowHtml(t) {
      var b = bands[t] || [0, 0];
      var lbl = labels[t] != null ? labels[t] : t;
      return '<div class="fx-rung" data-tier="' + esc(t) + '">' +
        '<span class="n">' + esc((t[0] || '?').toUpperCase()) + '</span>' +
        '<input class="fx-input" data-lbl style="flex:1;height:34px" value="' + esc(lbl) + '" aria-label="' + esc(t) + ' tier label">' +
        '<div class="fx-unit" data-unit="$"><input class="fx-input" data-lo value="' + esc(dollars(b[0])) + '" inputmode="decimal" aria-label="' + esc(t) + ' floor"></div>' +
        '<span style="color:#6a6a6a">–</span>' +
        '<div class="fx-unit" data-unit="$"><input class="fx-input" data-hi value="' + esc(dollars(b[1])) + '" inputmode="decimal" aria-label="' + esc(t) + ' ceiling"></div>' +
        '</div>';
    }
    bandBody.innerHTML = tiers.map(rowHtml).join('');
    Fastt.liveBadge(document.getElementById('rm-bands').querySelector('h4'));

    async function saveTier(rung) {
      var t = rung.dataset.tier;
      var loEl = rung.querySelector('[data-lo]'), hiEl = rung.querySelector('[data-hi]'), lblEl = rung.querySelector('[data-lbl]');
      var lo = Math.max(0, Math.round((parseFloat(loEl.value) || 0) * 100));
      var hi = Math.max(0, Math.round((parseFloat(hiEl.value) || 0) * 100));
      if (hi < lo) hi = lo; // ceiling never below floor
      loEl.value = dollars(lo); hiEl.value = dollars(hi);
      var lbl = (lblEl.value || '').trim() || t;
      lblEl.value = lbl;
      var bandPatch = {}; bandPatch[t] = [lo, hi];
      var labelPatch = {}; labelPatch[t] = lbl;
      try {
        var out = await Fastt.patch('/admin/account-ai-config/vault-ai',
          { account_id: acct, config: { pricing: { bands_by_tier: bandPatch }, tier_labels: labelPatch } });
        cfg = (out && out.config) || cfg;
        bands = (cfg.pricing || {}).bands_by_tier || bands;
        labels = cfg.tier_labels || labels;
        Fastt.saved(lbl + ' band: ' + Fastt.fmtCents(lo) + '–' + Fastt.fmtCents(hi));
        if (reloadPreview) reloadPreview(false); // re-price the arc preview (free path)
      } catch (e) { Fastt.oops(e); }
    }
    bandBody.addEventListener('change', function (ev) {
      var inp = ev.target.closest('input'); if (!inp) return;
      var rung = inp.closest('.fx-rung'); if (rung) saveTier(rung);
    });
  })();

  // Hands-free switches: NOT wired. consume_reminders never reads
  // `auto_post` / `auto_mass_message` — the consumer mass-DMs EVERY approved
  // card on the rule's next enabled run (there is no second-click path), and
  // no feed-post consumer exists at all. Painting an OFF switch that claims
  // "cards wait for you" would be a lie, so: reality painted, saves refused.
  var hfGroup = document.getElementById('rm-handsfree');
  if (hfGroup) Fastt.staticBadge(hfGroup.querySelector('h4') || hfGroup, 'NOT WIRED');
  var apSw = document.getElementById('rm-autopost');
  if (apSw) {
    apSw.classList.remove('on'); // reality: nothing ever posts to the feed
    apSw.addEventListener('click', function (ev) {
      ev.stopPropagation(); // no visual flip — there is nothing to flip
      Fastt.toast('Not wired: no consumer auto-posts approved cards to the feed.');
    });
  }
  var amSw = document.getElementById('rm-automass');
  if (amSw) {
    amSw.classList.add('on'); // reality: every approved card is mass-DM'd
    amSw.addEventListener('click', function (ev) {
      ev.stopPropagation();
      Fastt.toast('Not a real gate: the consumer sends every approved card once the rule is enabled. Approve IS the send decision.');
    });
  }
});
