Fastt.ready(async function () {
  'use strict';
  var $ = Fastt.$, $$ = Fastt.$$, esc = Fastt.esc;
  var acct = Fastt.account();
  if (!acct) {
    Fastt.staticBadge($('#bc-status'), 'NO ACCOUNT SELECTED');
    return;
  }

  // ---- load: nudge-config blob + this account's automation rules ----
  var cfgRes = await Fastt.get('/admin/nudge-config');
  var rules = await Fastt.rulesByKind();
  var stored = cfgRes.config || {};
  var defaults = cfgRes.defaults || {};
  function eff(k) { return stored[k] !== undefined ? stored[k] : defaults[k]; }

  var state = {
    stored: stored,
    slots: JSON.parse(JSON.stringify(stored.slots || defaults.slots || {})),
    bucket: 'default',
    rules: rules,
    mnSlots: {},        // mass_nudge rule payload.slots (its own pools)
    mnBucket: 'default',
  };

  // header identity — the mockup shipped a hardcoded creator name here
  var hdr = $('#bc-hdr-model');
  var hdrRow = Fastt.accountRow();
  if (hdr) hdr.textContent = hdrRow ? (hdrRow.nickname || String(hdrRow.id)) : acct;

  // vault thumbnails: /admin/vault-ai/thumb?account_id=&media_id= → image/jpeg
  function thumbUrl(id) {
    return '/admin/vault-ai/thumb?account_id=' + encodeURIComponent(acct)
      + '&media_id=' + encodeURIComponent(id);
  }
  function thumbImg(id) {
    return '<img src="' + thumbUrl(id) + '" alt="" loading="lazy" title="vault #' + esc(id) + '">';
  }

  var POOL_SLOTS = {
    morning: ['morning_1', 'morning_2'],
    afternoon: ['afternoon_1', 'afternoon_2'],
    evening: ['evening'],
    night: ['night'],
  };
  var SLOT_LABEL = { morning_1: 'Morning', morning_2: 'Morning', afternoon_1: 'Afternoon',
    afternoon_2: 'Afternoon', evening: 'Evening', night: 'Night' };
  // The four UI pools each cover one or two SERVER sub-slots. Every rendered
  // line remembers the sub-slot it came from (input.dataset.slot) so a save
  // writes each line back where it was — merging them would turn Ava's
  // 6 + 6 distinct morning lines into 12 + 12 duplicates.
  var SLOT_HOURS = { morning_1: '05–09', morning_2: '09–12', afternoon_1: '12–15',
    afternoon_2: '15–18', evening: '18–21', night: '21–05' };
  function slotKey(h) {
    if (h >= 5 && h < 9) return 'morning_1';
    if (h >= 9 && h < 12) return 'morning_2';
    if (h >= 12 && h < 15) return 'afternoon_1';
    if (h >= 15 && h < 18) return 'afternoon_2';
    if (h >= 18 && h < 21) return 'evening';
    return 'night';
  }
  function ruleOf(kind) { return (state.rules[kind] || [])[0] || null; }
  function setSt(el, txt, ok) {
    el.innerHTML = '<i></i>' + esc(txt);
    el.classList.toggle('ok', !!ok);
  }
  function intOr(el, fb) { var n = parseInt(el.value, 10); return isFinite(n) ? Math.max(0, n) : fb; }
  function centsOf(el) {
    var raw = String(el.value || '').replace(/[^0-9.]/g, '');
    if (!raw) return null;
    var n = parseFloat(raw);
    return isFinite(n) ? Math.round(n * 100) : null;
  }
  function segSet(segEl, idx) {
    $$('button', segEl).forEach(function (b, i) { b.classList.toggle('on', i === idx); });
  }
  function segIdx(segEl) {
    var i = 0; $$('button', segEl).forEach(function (b, j) { if (b.classList.contains('on')) i = j; });
    return i;
  }
  function ckSet(id, on) { var el = $('#' + id); if (el) el.classList.toggle('on', !!on); }
  function ckGet(id) { var el = $('#' + id); return !!(el && el.classList.contains('on')); }

  // ---- pools: render / commit (bucket-aware) ----
  function poolUnion(bucket, pool, field) {
    var b = state.slots[bucket] || {};
    var out = [], seen = {};
    POOL_SLOTS[pool].forEach(function (k) {
      var arr = (b[k] && b[k][field]) || [];
      arr.forEach(function (v) { var s = String(v); if (!seen[s]) { seen[s] = 1; out.push(v); } });
    });
    return out;
  }
  function mkLine(slot, text) {
    var inp = document.createElement('input');
    inp.className = 'fx-input';
    inp.value = text || '';
    inp.dataset.slot = slot;
    inp.title = 'server slot ' + slot + ' · ' + (SLOT_HOURS[slot] || '');
    inp.placeholder = state.bucket === 'weekend' ? 'weekend line (blank = use every-day pool)' : 'add a line…';
    return inp;
  }
  function renderPools() {
    var b = state.slots[state.bucket] || {};
    $$('#bc-poolgrid .bc-pool').forEach(function (poolEl) {
      var pool = poolEl.dataset.pool;
      $$('.fx-input', poolEl).forEach(function (i) { i.remove(); });
      var add = $('.bc-addline', poolEl);
      var n = 0;
      POOL_SLOTS[pool].forEach(function (k) {
        ((b[k] && b[k].text) || []).forEach(function (t) {
          poolEl.insertBefore(mkLine(k, t), add);
          n++;
        });
      });
      if (!n) poolEl.insertBefore(mkLine(POOL_SLOTS[pool][0], ''), add);
      paintPoolMedia(poolEl, poolUnion(state.bucket, pool, 'image'), 'nudge', pool);
    });
  }
  /** Repaint one pool's photo strip from the REAL vault ids in the config.
   *  `owner` picks which editor a click writes back to (nudge config vs the
   *  mass_nudge rule payload) — both store {slot:{text:[],image:[]}}. */
  function paintPoolMedia(poolEl, imgs, owner, pool) {
    var media = $('.bc-pool-media', poolEl);
    if (!media) return;
    var capTxt = imgs.length
      ? imgs.length + ' vault photo' + (imgs.length === 1 ? '' : 's') + ' · rotates'
      : 'no pool photo — this slot sends text-only';
    media.innerHTML = imgs.slice(0, 4).map(function (id) {
      return '<span class="bc-mini pick" data-owner="' + owner + '" data-pool="' + esc(pool)
        + '" data-id="' + esc(id) + '">' + thumbImg(id) + '</span>';
    }).join('')
      + (imgs.length > 4 ? '<span class="bc-mini none">+' + (imgs.length - 4) + '</span>' : '')
      + '<span class="bc-mini add pick" data-owner="' + owner + '" data-pool="' + esc(pool)
      + '" data-id="" title="edit this pool\'s vault ids">+</span>'
      + '<span style="font-size:11px;color:var(--muted2)" data-role="imgcap"></span>';
    $('[data-role="imgcap"]', media).textContent = capTxt;
  }
  /** Edit the image[] array of every sub-slot in a UI pool. */
  function editPoolImages(owner, pool) {
    var slots = owner === 'nudge' ? state.slots : state.mnSlots;
    var bucket = owner === 'nudge' ? state.bucket : state.mnBucket;
    var b = slots[bucket] || (slots[bucket] = {});
    var keys = POOL_SLOTS[pool];
    for (var i = 0; i < keys.length; i++) {
      var k = keys[i];
      var cur = ((b[k] || {}).image || []).join(', ');
      var raw = prompt('Vault media ids for slot "' + k + '" (' + (SLOT_HOURS[k] || '') +
        ') — comma-separated, blank = text-only:', cur);
      if (raw === null) return false;               // cancel aborts the whole pool
      var ids = raw.split(/[,\s]+/).filter(Boolean)
        .filter(function (x) { return /^\d+$/.test(x); }).map(Number);
      b[k] = Object.assign({}, b[k] || {}, { image: ids });
    }
    return true;
  }
  function commitBucketFromDom() {
    var bucket = state.bucket;
    var b = Object.assign({}, state.slots[bucket] || {});
    Object.keys(POOL_SLOTS).forEach(function (pool) {
      var poolEl = $('#bc-poolgrid .bc-pool[data-pool="' + pool + '"]');
      if (!poolEl) return;
      var first = POOL_SLOTS[pool][0];
      var bySlot = {};
      POOL_SLOTS[pool].forEach(function (k) { bySlot[k] = []; });
      $$('.fx-input', poolEl).forEach(function (i) {
        var v = i.value.trim();
        if (!v) return;
        var k = i.dataset.slot;
        // a line typed into "+ Add line" has no origin — it lands in the
        // pool's EARLIEST sub-slot rather than being copied into all of them
        if (!bySlot[k]) k = first;
        bySlot[k].push(v);
      });
      POOL_SLOTS[pool].forEach(function (k) {
        var prev = b[k] || {};
        if (!bySlot[k].length && prev.text === undefined && prev.image === undefined) return;
        b[k] = Object.assign({}, prev, { text: bySlot[k] });
      });
    });
    // A bucket is empty only when it has neither text NOR images: an
    // image-only weekend slot must not be deleted just because its text is blank.
    var any = Object.keys(b).some(function (k) {
      var s = b[k] || {};
      return ((s.text || []).length > 0) || ((s.image || []).length > 0);
    });
    if (bucket !== 'default' && !any) delete state.slots[bucket];
    else state.slots[bucket] = b;
  }

  // ---- config collect + debounced PUT ----
  function collectConfig() {
    commitBucketFromDom();
    var cfg = JSON.parse(JSON.stringify(state.stored));
    cfg.slots = state.slots;
    var nums = {
      'bc-delay': 'delay_minutes', 'bc-jitter': 'jitter_minutes', 'bc-gap': 'gap_minutes',
      'bc-maxtick': 'max_per_tick', 'bc-scancap': 'max_online_scan', 'bc-onlinerecent': 'online_recent_minutes',
      'bc-cooldown': 'min_hours_between_nudges', 'bc-maxnoreply': 'max_no_reply', 'bc-activeconvo': 'active_convo_hours',
    };
    Object.keys(nums).forEach(function (id) {
      var el = $('#' + id), n = parseInt(el.value, 10);
      if (isFinite(n)) cfg[nums[id]] = Math.max(0, n);
    });
    cfg.min_lifetime_spend_cents = centsOf($('#bc-spend-min'));
    cfg.max_lifetime_spend_cents = centsOf($('#bc-spend-max'));
    cfg.max_recent_spend_cents = centsOf($('#bc-spend-recent'));
    cfg.min_recent_spend_cents = centsOf($('#bc-spend-recent-min'));
    var _rd = parseInt($('#bc-spend-days').value, 10); if (isFinite(_rd) && _rd > 0) cfg.recent_spend_days = _rd;
    var _wg = parseInt($('#bc-welcomegrace').value, 10); if (isFinite(_wg) && _wg >= 0) cfg.welcome_grace_hours = _wg;
    var from = $('#bc-qh-from').value, until = $('#bc-qh-until').value;
    if (/^Off/.test(from)) cfg.quiet_hours = [0, 0];
    else {
      var fh = parseInt(from, 10) % 24;
      var uh = /^\d/.test(until) ? parseInt(until, 10) % 24 : 7;
      cfg.quiet_hours = [fh, uh];
    }
    cfg.info_line_mode = ['tease', 'qa', 'mix'][segIdx($('#bc-flavor'))];
    cfg.repeat_mode = ['new', 'same'][segIdx($('#bc-repeat'))];
    cfg.send_info_line = ckGet('bc-ck-info');
    cfg.info_line_image = ckGet('bc-ck-infoimg');
    cfg.send_nudge_line = ckGet('bc-ck-nudgeline');
    cfg.nudge_line_text = ckGet('bc-ck-nudgetext');
    cfg.nudge_line_image = ckGet('bc-ck-nudgeimg');
    // the kill-switch nudge_online.py reads before anything else
    cfg.enabled = ckGet('bc-ck-enabled');
    return cfg;
  }
  var save = Fastt.debounce(async function () {
    try {
      var cfg = collectConfig();
      var res = await Fastt.put('/admin/nudge-config', { account_id: acct, config: cfg });
      state.stored = res.config || cfg;
      state.slots = JSON.parse(JSON.stringify(state.stored.slots || state.slots));
      stored = state.stored;
      renderStatus(); renderPreviewCard();
      Fastt.saved();
    } catch (e) { Fastt.oops(e); }
  }, 700);

  // ---- renders ----
  // nudge_online.py returns early when the config blob's own `enabled` key is
  // false, so the rule switch alone does NOT prove sends happen.
  function cfgEnabled() { return eff('enabled') !== false; }
  function renderStatus() {
    var r = ruleOf('nudge_online');
    var on = !!(r && r.is_enabled);
    var cfgOn = cfgEnabled();
    var every = (r && r.every_seconds) || 60;
    var stats = (r && r.last_run && r.last_run.stats) || {};
    setSt($('#bc-st-run'),
      !cfgOn ? (on ? 'Rule on, but config kill-switch is OFF (enabled: false) — nothing sends'
                   : 'Off — rule disabled AND config kill-switch off')
             : (on ? ('Running — scans every ' + every + ' s') : 'Off — rule disabled, not scanning'),
      on && cfgOn);
    setSt($('#bc-st-online'), stats.online != null ? (stats.online + ' fans online at last scan') : 'no scan data yet', on && cfgOn);
    var lineCount = 0, bucketCount = 0;
    Object.keys(state.slots).forEach(function (bk) {
      bucketCount++;
      Object.keys(state.slots[bk] || {}).forEach(function (k) {
        lineCount += ((state.slots[bk][k] || {}).text || []).length;
      });
    });
    setSt($('#bc-st-pools'), 'Pools · ' + lineCount + ' lines · ' + bucketCount + ' day bucket' + (bucketCount === 1 ? '' : 's'), false);
    var qh = eff('quiet_hours') || [0, 0];
    setSt($('#bc-st-quiet'), (+qh[0] === +qh[1]) ? 'Quiet hours off — sends 24/7'
      : ('Quiet ' + qh[0] + ':00–' + qh[1] + ':00'), false);
    var kvOn = $('#bc-kv-online');
    kvOn.innerHTML = '<b>' + esc(stats.online != null ? stats.online : '—') + '</b> fans online · scans every <b>' + esc(every) + ' s</b>';
    $('#bc-kv-cooldown').innerHTML = 'cooldown: <b>' + esc(eff('min_hours_between_nudges')) + ' h per fan</b>';
    var reach = $('#bc-reach');
    reach.innerHTML = stats.online != null
      ? ('Last scan: <b>' + esc(stats.online) + ' fans</b> online · ' + esc(stats.handled || 0) + ' handled · ' + esc(stats.skipped || 0) + ' skipped')
      : 'No scan has run yet on this account.';
  }
  function renderMaster() {
    var r = ruleOf('nudge_online');
    var on = !!(r && r.is_enabled);
    var cfgOn = cfgEnabled();
    var sw = $('#bc-master-sw'), card = $('#bc-master');
    sw.classList.toggle('on', on);
    card.classList.toggle('on', on);
    var stEl = $('.fx-tc-state', card);
    stEl.textContent = on ? (cfgOn ? 'Running' : 'Config off') : 'Off';
    stEl.title = cfgOn ? '' : 'The nudge config blob has enabled:false — the sender returns early even with the rule on';
    var pill = $('#bc-rulestates');
    var kinds = [['nudge_online', '1:1 nudge'], ['mass_nudge', 'mass'], ['online_blast', 'blast']];
    pill.innerHTML = '<i></i>' + kinds.map(function (kv) {
      var rr = ruleOf(kv[0]);
      var stx = rr ? (rr.is_enabled ? 'on' : 'off') : '—';
      return '<span data-kind="' + kv[0] + '" style="cursor:pointer" title="click to toggle the ' +
        kv[0] + ' rule">' + esc(kv[1]) + ': ' + stx + '</span>';
    }).join(' · ');
    pill.classList.toggle('ok', on);
  }
  function selPreviewHour() {
    var el = $('#bc-preview-hour'), v = el && el.value;
    if (v === '' || v == null) return new Date().getHours();
    var n = parseInt(v, 10); return isFinite(n) ? ((n % 24) + 24) % 24 : new Date().getHours();
  }
  // Shared: render a live-composed message set (name + text + photo) into a box.
  function renderComposed(box, title, name, messages) {
    var rows = (messages || []).map(function (m) {
      var media = (m.media || []);
      var thumb = media.length ? '<div class="bc-tile" style="width:40px;height:40px">' + thumbImg(media[0]) + '</div>' : '';
      return '<div class="bc-line" style="margin-bottom:6px">' + thumb +
        '<span>' + (m.kind ? '<b style="color:#9db1fb;font-weight:600">' + esc(m.kind) + '</b> ' : '') +
        esc(m.text || '(image only)') + (media.length ? ' <span style="color:var(--muted2)">[+#' + esc(media[0]) + ']</span>' : '') + '</span></div>';
    }).join('');
    box.style.display = 'block';
    box.innerHTML = '<div class="fx-note" style="flex-direction:column;align-items:stretch;gap:8px">' +
      '<div style="font-size:12px;color:var(--muted)">' + esc(title) +
      (name ? ' · resolves to <b style="color:#ddd">' + esc(name) + '</b>' : '') + '</div>' +
      (rows || '<div class="bc-line" style="margin:0">nothing composes for this hour — the pool is empty</div>') +
      '</div>';
  }
  function renderPreviewCard() {
    var h = selPreviewHour();
    var key = slotKey(h);
    var wd = ['sunday', 'monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday'][new Date().getDay()];
    var isWknd = wd === 'saturday' || wd === 'sunday';
    var pool = null, buckets = [wd, isWknd ? 'weekend' : 'weekday', 'default'];
    for (var i = 0; i < buckets.length; i++) {
      var b = state.slots[buckets[i]];
      if (b && b[key] && ((b[key].text || []).length || (b[key].image || []).length)) { pool = b[key]; break; }
    }
    pool = pool || {};
    var lines = pool.text || [];
    $('#bc-cur-slot').textContent = SLOT_LABEL[key] + ' (' + key + ')';
    $('#bc-cur-slot2').textContent = SLOT_LABEL[key];
    $('#bc-rot-note').textContent = 'rotates ' + lines.length + ' line' + (lines.length === 1 ? '' : 's');
    var card = $('#bc-pool-card');
    $$('.bc-line', card).forEach(function (el) { el.remove(); });
    var before = $('.bc-media', card);
    (lines.length ? lines.slice(0, 4) : ['(this slot’s pool is empty — nothing would send)']).forEach(function (t) {
      var d = document.createElement('div');
      d.className = 'bc-line';
      d.innerHTML = '<span class="who">Aa</span>' + esc(t);
      card.insertBefore(d, before);
    });
    var imgs = pool.image || [];
    var tile = $('#bc-media-tile');
    if (tile) {
      if (imgs.length) {
        tile.className = 'bc-tile';
        tile.innerHTML = thumbImg(imgs[0]);
      } else {
        tile.className = 'bc-tile none';
        tile.textContent = 'no photo';
      }
    }
    $('#bc-media-cap').innerHTML = imgs.length
      ? '<b>Slot pool</b> · showing <b>#' + esc(imgs[0]) + '</b> — 1 of ' + imgs.length + ' vault photo' + (imgs.length === 1 ? '' : 's') + ' rides along, a different one each send'
      : '<b>No slot photo</b> · falls back to the account’s time-of-day image (if set)';
  }
  function fillForm() {
    $('#bc-delay').value = eff('delay_minutes');
    $('#bc-jitter').value = eff('jitter_minutes');
    $('#bc-gap').value = eff('gap_minutes');
    $('#bc-maxtick').value = eff('max_per_tick');
    $('#bc-scancap').value = eff('max_online_scan');
    $('#bc-onlinerecent').value = eff('online_recent_minutes');
    $('#bc-cooldown').value = eff('min_hours_between_nudges');
    $('#bc-maxnoreply').value = eff('max_no_reply');
    $('#bc-activeconvo').value = eff('active_convo_hours');
    $('#bc-welcomegrace').value = eff('welcome_grace_hours');
    var c;
    c = eff('min_lifetime_spend_cents'); $('#bc-spend-min').value = c != null ? (c / 100) : '';
    c = eff('max_lifetime_spend_cents'); $('#bc-spend-max').value = c != null ? (c / 100) : '';
    c = eff('max_recent_spend_cents'); $('#bc-spend-recent').value = c != null ? (c / 100) : '';
    c = eff('min_recent_spend_cents'); $('#bc-spend-recent-min').value = c != null ? (c / 100) : '';
    var rd = eff('recent_spend_days'); $('#bc-spend-days').value = rd != null ? rd : '';
    var qh = eff('quiet_hours') || [0, 0];
    var fromSel = $('#bc-qh-from'), untilSel = $('#bc-qh-until');
    if (+qh[0] === +qh[1]) { fromSel.selectedIndex = 0; untilSel.selectedIndex = 0; }
    else {
      var fTxt = (qh[0] < 10 ? '0' : '') + qh[0] + ':00', uTxt = (qh[1] < 10 ? '0' : '') + qh[1] + ':00';
      $$('option', fromSel).forEach(function (o, i) { if (o.textContent === fTxt) fromSel.selectedIndex = i; });
      $$('option', untilSel).forEach(function (o, i) { if (o.textContent === uTxt) untilSel.selectedIndex = i; });
    }
    segSet($('#bc-flavor'), Math.max(0, ['tease', 'qa', 'mix'].indexOf(eff('info_line_mode') || 'mix')));
    segSet($('#bc-repeat'), Math.max(0, ['new', 'same'].indexOf(eff('repeat_mode') || 'new')));
    ckSet('bc-ck-info', eff('send_info_line'));
    ckSet('bc-ck-infoimg', eff('info_line_image'));
    ckSet('bc-ck-nudgeline', eff('send_nudge_line'));
    ckSet('bc-ck-nudgetext', eff('nudge_line_text') !== false);
    ckSet('bc-ck-nudgeimg', eff('nudge_line_image') !== false);
    ckSet('bc-ck-welcomed', eff('require_welcomed'));
    ckSet('bc-ck-enabled', cfgEnabled());
    renderKillHint();
  }
  function renderKillHint() {
    var on = ckGet('bc-ck-enabled');
    var r = ruleOf('nudge_online');
    var ruleOn = !!(r && r.is_enabled);
    $('#bc-killhint').innerHTML = on
      ? ('Sends are allowed. Whether anything actually goes out still depends on the <b>' +
         (ruleOn ? 'running' : 'disabled') + '</b> <code>nudge_online</code> rule above.')
      : 'Hard off. <b>nudge_online.py returns before it scans</b>, so no 1:1 nudge can leave this account no matter what the rule, the pools or the pacing say.';
  }

  // ---- rule toggling (explicit click + confirm before enabling) ----
  var KIND_META = {
    nudge_online: { label: '1:1 online nudge (nudge_online)', name: 'Nudge online', every: 60 },
    mass_nudge: { label: 'Mass Nudge broadcast (mass_nudge)', name: 'Mass Nudge', every: 1200 },
    online_blast: { label: 'Online Blast (online_blast)', name: 'Online Blast', every: 3600 },
  };
  async function toggleKind(kind, wantOn, revert) {
    var meta = KIND_META[kind];
    var existing = ruleOf(kind);
    // Never mint a rule from this pill. A brand-new rule would be born with
    // payload {} and, for online_blast, an empty payload falls back to the
    // built-in _DEFAULT_SLOTS lines — i.e. a generic blast to everyone online.
    if (!existing) {
      Fastt.toast(wantOn
        ? ('No ' + kind + ' rule on this account — create and configure it in Automations first. '
           + 'An unconfigured ' + kind + ' would send built-in default lines.')
        : ('No ' + kind + ' rule exists yet — nothing to turn off'), wantOn ? 'err' : '');
      if (revert) revert(); return;
    }
    var emptyPayload = !existing.payload || !Object.keys(existing.payload).length;
    if (wantOn && !confirm('Enable ' + meta.label + ' for this account?'
        + '\nOnce running it WILL message fans on its own.'
        + (emptyPayload ? '\n\nThis rule has NO payload configured — it will fall back to built-in default content.' : '')
        + (cfgEnabled() ? '' : '\n\n(The nudge config kill-switch is enabled:false — the 1:1 nudge sender would still return early.)'))) {
      if (revert) revert(); return;
    }
    try {
      await Fastt.patch('/admin/automation-rules/' + existing.id, { is_enabled: wantOn });
      state.rules = await Fastt.rulesByKind();
      renderMaster(); renderStatus();
      Fastt.saved(meta.name + (wantOn ? ' enabled' : ' disabled'));
    } catch (e) { Fastt.oops(e); if (revert) revert(); }
  }
  // document-level so it runs AFTER fx-js has flipped the visual state
  document.addEventListener('click', function (e) {
    if (!e.target.closest('#bc-master-sw')) return;
    var sw = $('#bc-master-sw');
    var wantOn = sw.classList.contains('on');
    // revert = repaint from the rule row (also restores the "Config off" state text)
    toggleKind('nudge_online', wantOn, renderMaster);
  });
  $('#bc-rulestates').addEventListener('click', function (e) {
    var s = e.target.closest('[data-kind]');
    if (!s) return;
    var kind = s.dataset.kind, r = ruleOf(kind);
    toggleKind(kind, !(r && r.is_enabled), renderMaster);
  });

  // ---- day-bucket segment (read the CLICKED button — class flip happens later) ----
  $('#bc-dayseg').addEventListener('click', function (e) {
    var btn = e.target.closest('button');
    if (!btn) return;
    var idx = $$('button', $('#bc-dayseg')).indexOf(btn);
    commitBucketFromDom();
    state.bucket = idx === 1 ? 'weekend' : 'default';
    renderPools();
  });

  // ---- pool add-line ----
  $('#bc-poolgrid').addEventListener('click', function (e) {
    var add = e.target.closest('.bc-addline');
    if (!add) return;
    var poolEl = add.closest('.bc-pool');
    var inp = mkLine(POOL_SLOTS[poolEl.dataset.pool][0], '');
    inp.placeholder = 'new line…';
    poolEl.insertBefore(inp, add);
    inp.focus();
  });

  // ---- save triggers (config only; rule toggles handled above) ----
  $('#adv-bc').addEventListener('change', function (e) {
    if (e.target.closest('#bc-tz-field')) return; // timezone select: no backend key here
    // the Mass Nudge / Online Blast groups edit an automation ROW, not this
    // config blob — they have their own explicit Save button
    if (e.target.closest('#bc-mn, #bc-ob, #bc-rollout')) return;
    save();
  });
  $('#adv-bc').addEventListener('click', function (e) {
    if (e.target.closest('#bc-ck-enabled')) {
      // fx-js flips the class after this bubbles — read it on the next tick
      setTimeout(function () { renderKillHint(); save(); }, 0);
      return;
    }
    if (e.target.closest('#bc-flavor button, #bc-repeat button, #bc-ck-info, #bc-ck-infoimg, #bc-ck-nudgeline, #bc-ck-nudgetext, #bc-ck-nudgeimg')) save();
    if (e.target.closest('#bc-ck-welcomed')) {
      // fx-js flips the class AFTER this bubbles to document — revert on the next tick
      setTimeout(function () { ckSet('bc-ck-welcomed', eff('require_welcomed')); }, 0);
      Fastt.toast('"Require welcomed" is fixed server-side — not saveable from here');
    }
  });

  // ---- preview: change the previewed hour repaints the client-side pool card ----
  $('#bc-preview-hour').addEventListener('change', renderPreviewCard);

  // ---- preview ("Compose a real sample" = live compose-only, nothing sends) ----
  $('#bc-test-btn').addEventListener('click', async function () {
    var btn = $('#bc-test-btn'), box = $('#bc-test-result');
    var fanRaw = ($('#bc-test-fan').value || '').trim();
    var fanId = /^\d+$/.test(fanRaw) ? Number(fanRaw) : null;
    if (fanRaw && fanId == null) { Fastt.toast('Fan id must be numeric', 'err'); return; }
    btn.disabled = true;
    box.style.display = 'block';
    box.innerHTML = '<div class="bc-hint">composing…</div>';
    try {
      var res = await Fastt.post('/admin/nudge-config/preview', {
        account_id: acct, config: collectConfig(),
        hour: selPreviewHour(), fan_id: fanId,
      });
      renderComposed(box, 'Live composer · slot ' + (res.slot || '—'), res.name, res.messages);
    } catch (e) { box.innerHTML = '<div class="fx-note warn">Preview failed: ' + esc(e.message || e) + '</div>'; }
    btn.disabled = false;
  });

  // ================= MASS NUDGE (rule payload, not the config blob) =========
  // Knob names come straight from GET /admin/automation-kinds → mass_nudge:
  // with_image · exclude_replied_hours · exclude_inbound_hours · excluded_users
  // · max_online · unsend_after_hours · slots · dry_run  (+ online_only, which
  // the live payload on this account carries).
  var MN_POOLS = ['morning', 'afternoon', 'evening', 'night'];
  function mnRule() { return ruleOf('mass_nudge'); }
  function mnPayload() { return (mnRule() && mnRule().payload) || {}; }

  function mnRenderPools() {
    var grid = $('#bc-mn-poolgrid');
    var b = state.mnSlots[state.mnBucket] || {};
    grid.innerHTML = MN_POOLS.map(function (pool) {
      var label = pool[0].toUpperCase() + pool.slice(1);
      var hrs = POOL_SLOTS[pool].map(function (k) { return SLOT_HOURS[k]; }).join(' / ');
      return '<div class="bc-pool" data-pool="' + pool + '"><h5>' + label +
        ' <span class="hrs">' + esc(hrs) + '</span></h5>' +
        '<span class="bc-addline">+ Add line</span>' +
        '<div class="bc-pool-media"><span style="font-size:11px;color:var(--muted2)" data-role="imgcap"></span></div></div>';
    }).join('');
    $$('#bc-mn-poolgrid .bc-pool').forEach(function (poolEl) {
      var pool = poolEl.dataset.pool, add = $('.bc-addline', poolEl), n = 0;
      POOL_SLOTS[pool].forEach(function (k) {
        (((b[k] || {}).text) || []).forEach(function (t) {
          var inp = mkLine(k, t);
          inp.placeholder = 'mass line (no {name} — same text for everyone)';
          poolEl.insertBefore(inp, add); n++;
        });
      });
      if (!n) {
        var e0 = mkLine(POOL_SLOTS[pool][0], '');
        e0.placeholder = 'empty — this slot broadcasts nothing';
        poolEl.insertBefore(e0, add);
      }
      var imgs = [], seen = {};
      POOL_SLOTS[pool].forEach(function (k) {
        (((b[k] || {}).image) || []).forEach(function (v) {
          if (!seen[v]) { seen[v] = 1; imgs.push(v); }
        });
      });
      paintPoolMedia(poolEl, imgs, 'mass', pool);
    });
  }
  function mnCommitBucket() {
    var b = Object.assign({}, state.mnSlots[state.mnBucket] || {});
    MN_POOLS.forEach(function (pool) {
      var poolEl = $('#bc-mn-poolgrid .bc-pool[data-pool="' + pool + '"]');
      if (!poolEl) return;
      var first = POOL_SLOTS[pool][0], bySlot = {};
      POOL_SLOTS[pool].forEach(function (k) { bySlot[k] = []; });
      $$('.fx-input', poolEl).forEach(function (i) {
        var v = i.value.trim(); if (!v) return;
        var k = bySlot[i.dataset.slot] ? i.dataset.slot : first;
        bySlot[k].push(v);
      });
      POOL_SLOTS[pool].forEach(function (k) {
        var prev = b[k] || {};
        if (!bySlot[k].length && prev.text === undefined && prev.image === undefined) return;
        b[k] = Object.assign({}, prev, { text: bySlot[k] });
      });
    });
    var any = Object.keys(b).some(function (k) {
      var s = b[k] || {};
      return ((s.text || []).length > 0) || ((s.image || []).length > 0);
    });
    if (state.mnBucket !== 'default' && !any) delete state.mnSlots[state.mnBucket];
    else state.mnSlots[state.mnBucket] = b;
  }
  function mnCollect() {
    mnCommitBucket();
    var p = JSON.parse(JSON.stringify(mnPayload()));
    var num = function (id) { var n = parseInt($('#' + id).value, 10); return isFinite(n) && n >= 0 ? n : null; };
    p.slots = state.mnSlots;
    p.with_image = ckGet('bc-mn-ck-img');
    p.online_only = ckGet('bc-mn-ck-online');
    if (ckGet('bc-mn-ck-dry')) p.dry_run = true; else delete p.dry_run;
    var v;
    v = num('bc-mn-repl'); if (v == null) delete p.exclude_replied_hours; else p.exclude_replied_hours = v;
    v = num('bc-mn-inb'); if (v == null) delete p.exclude_inbound_hours; else p.exclude_inbound_hours = v;
    v = num('bc-mn-unsend'); if (v == null) delete p.unsend_after_hours; else p.unsend_after_hours = v;
    v = num('bc-mn-max'); if (v == null) delete p.max_online; else p.max_online = v;
    var every = parseInt($('#bc-mn-every').value, 10);
    return { payload: p, every_seconds: (isFinite(every) && every > 0) ? every * 60 : (mnRule() || {}).every_seconds || 1200 };
  }
  function mnRender() {
    var r = mnRule(), p = mnPayload();
    var grp = $('#bc-mn');
    if (!r) {
      $('#bc-mn-state').innerHTML = '<i></i>no mass_nudge rule on this account';
      $('#bc-mn-last').innerHTML = '<i></i>—';
      $$('#bc-mn input, #bc-mn button').forEach(function (el) { el.disabled = true; });
      $('#bc-mn-poolgrid').innerHTML =
        '<div class="bc-hint" style="grid-column:1/-1">This creator has no <code>mass_nudge</code> rule row, so there is nothing to edit. ' +
        'Create it in Automations first — a rule born with an empty payload would broadcast built-in default lines.</div>';
      Fastt.staticBadge($('#bc-mn > h4'), 'NO RULE ON THIS ACCOUNT');
      return;
    }
    Fastt.liveBadge($('#bc-mn > h4'));
    var stats = (r.last_run && r.last_run.stats) || {};
    $('#bc-mn-state').innerHTML = '<i></i>' + (r.is_enabled
      ? 'Running · fires every ' + Math.round((r.every_seconds || 1200) / 60) + ' min'
      : 'Off — rule disabled (id ' + r.id + ')');
    $('#bc-mn-state').classList.toggle('ok', !!r.is_enabled);
    $('#bc-mn-last').innerHTML = '<i></i>' + (r.last_run
      ? ('last run ' + Fastt.fmtAgo(r.last_run.started_at) + ' · ' +
         (stats.sent != null ? stats.sent + ' sent' : (r.last_run.status || 'no stats')))
      : 'never run');
    $('#bc-mn-every').value = Math.round((r.every_seconds || 1200) / 60);
    $('#bc-mn-repl').value = p.exclude_replied_hours != null ? p.exclude_replied_hours : '';
    $('#bc-mn-inb').value = p.exclude_inbound_hours != null ? p.exclude_inbound_hours : '';
    $('#bc-mn-unsend').value = p.unsend_after_hours != null ? p.unsend_after_hours : '';
    $('#bc-mn-max').value = p.max_online != null ? p.max_online : '';
    ckSet('bc-mn-ck-img', p.with_image === true);
    ckSet('bc-mn-ck-online', p.online_only !== false);
    ckSet('bc-mn-ck-dry', p.dry_run === true);
    state.mnSlots = JSON.parse(JSON.stringify(p.slots || {}));
    mnRenderPools();
  }
  $('#bc-mn-dayseg').addEventListener('click', function (e) {
    var btn = e.target.closest('button'); if (!btn) return;
    var idx = $$('button', $('#bc-mn-dayseg')).indexOf(btn);
    mnCommitBucket();
    state.mnBucket = idx === 1 ? 'weekend' : 'default';
    mnRenderPools();
  });
  $('#bc-mn-poolgrid').addEventListener('click', function (e) {
    var add = e.target.closest('.bc-addline');
    if (!add) return;
    var poolEl = add.closest('.bc-pool');
    var inp = mkLine(POOL_SLOTS[poolEl.dataset.pool][0], '');
    inp.placeholder = 'new mass line…';
    poolEl.insertBefore(inp, add);
    inp.focus();
  });
  $('#bc-mn-save').addEventListener('click', async function () {
    var r = mnRule(); if (!r) return;
    var btn = $('#bc-mn-save'); btn.disabled = true;
    try {
      var body = mnCollect();
      await Fastt.patch('/admin/automation-rules/' + r.id,
        { payload: body.payload, every_seconds: body.every_seconds });
      state.rules = await Fastt.rulesByKind();
      mnRender(); renderMaster();
      Fastt.saved('Mass Nudge saved');
    } catch (e) { Fastt.oops(e); }
    btn.disabled = false;
  });
  function mnSelHour() {
    var v = $('#bc-mn-hour').value;
    if (v === '' || v == null) return new Date().getHours();
    var n = parseInt(v, 10); return isFinite(n) ? ((n % 24) + 24) % 24 : new Date().getHours();
  }
  $('#bc-mn-preview').addEventListener('click', async function () {
    var r = mnRule(); if (!r) return;
    var btn = $('#bc-mn-preview'), box = $('#bc-mn-result'); btn.disabled = true;
    box.style.display = 'block'; box.innerHTML = '<div class="bc-hint">composing…</div>';
    try {
      // compose-only: resolves one slot line + photo, sends nothing
      var res = await Fastt.post('/admin/mass-nudge/preview', {
        account_id: acct, payload: mnCollect().payload, hour: mnSelHour(),
      });
      renderComposed(box, 'Broadcast · slot ' + (res.slot || '—') + ' · rotates ' + (res.lines || 0) + ' line' + (res.lines === 1 ? '' : 's'),
        null, [{ kind: '', text: res.text, media: res.media }]);
    } catch (e) { box.innerHTML = '<div class="fx-note warn">Preview failed: ' + esc(e.message || e) + '</div>'; }
    btn.disabled = false;
  });

  // ---- run-now: 1:1 nudge detector pass (outbound — explicit click + confirm) ----
  $('#bc-runnow').addEventListener('click', async function () {
    var r = ruleOf('nudge_online');
    if (!r) { Fastt.toast('No nudge_online rule on this account to run', 'err'); return; }
    if (!confirm('Run ONE nudge_online detector pass now?\n\n' +
      'It scans who is online right now and queues personalised nudges — those WILL message fans after their delay.' +
      (cfgEnabled() ? '' : '\n\n(The config kill-switch is enabled:false — nudge_online.py returns early, so nothing would actually send.)'))) return;
    var btn = $('#bc-runnow'); btn.disabled = true;
    try {
      await Fastt.post('/admin/automation-rules/' + r.id + '/run-now', {});
      state.rules = await Fastt.rulesByKind();
      renderStatus(); renderMaster();
      Fastt.saved('Detector pass queued — nudges fire after their delay');
    } catch (e) { Fastt.oops(e); }
    btn.disabled = false;
  });

  // ---- send-now: mass_nudge broadcast (outbound — explicit click + confirm) ----
  $('#bc-mn-sendnow').addEventListener('click', async function () {
    var r = mnRule(); if (!r) return;
    if (!confirm('Broadcast ONE Mass Nudge to every fan online right now?\n\n' +
      'This sends immediately — it does not wait for the timer. Appears in stats within ~30s.')) return;
    var btn = $('#bc-mn-sendnow'); btn.disabled = true;
    try {
      await Fastt.post('/admin/automation-rules/' + r.id + '/run-now', {});
      state.rules = await Fastt.rulesByKind();
      mnRender();
      Fastt.saved('Broadcasting to online fans now');
    } catch (e) { Fastt.oops(e); }
    btn.disabled = false;
  });

  // ---- roll out the 1:1 nudge config to other models (paused — enable:false) ----
  var roSel = {};
  function roRoster() {
    return (Fastt.accounts() || []).filter(function (a) { return String(a.id) !== String(acct); });
  }
  function roRenderCount() {
    var n = Object.keys(roSel).filter(function (k) { return roSel[k]; }).length;
    setSt($('#bc-ro-count'), n + ' selected', n > 0);
  }
  function roRender() {
    var grid = $('#bc-ro-grid'), roster = roRoster();
    if (!roster.length) {
      grid.innerHTML = '<span class="bc-hint" style="grid-column:1/-1">Only one creator on this login — nothing to roll out to.</span>';
      $('#bc-ro-apply').disabled = true; $('#bc-ro-all').disabled = true; $('#bc-ro-none').disabled = true;
      return;
    }
    grid.innerHTML = roster.map(function (a) {
      var on = !!roSel[a.id];
      return '<label class="fx-check' + (on ? ' on' : '') + '" data-ro="' + esc(a.id) + '" style="padding:4px 0">' +
        '<span class="bx"></span>' + esc(a.nickname || a.id) + '</label>';
    }).join('');
    roRenderCount();
  }
  $('#bc-ro-grid').addEventListener('click', function (e) {
    var lbl = e.target.closest('[data-ro]'); if (!lbl) return;
    var id = lbl.dataset.ro; roSel[id] = !roSel[id];
    setTimeout(roRenderCount, 0);
  });
  $('#bc-ro-all').addEventListener('click', function () { roRoster().forEach(function (a) { roSel[a.id] = true; }); roRender(); });
  $('#bc-ro-none').addEventListener('click', function () { roSel = {}; roRender(); });
  $('#bc-ro-apply').addEventListener('click', async function () {
    var ids = Object.keys(roSel).filter(function (k) { return roSel[k]; });
    if (!ids.length) { Fastt.toast('Pick at least one model', 'err'); return; }
    if (!confirm('Copy this 1:1 nudge config (text-only) to ' + ids.length + ' model' + (ids.length === 1 ? '' : 's') +
      '?\n\nEach is written PAUSED — no rule is enabled, nothing starts sending.')) return;
    var btn = $('#bc-ro-apply'); btn.disabled = true;
    try {
      var cfg = collectConfig();
      var res = await Fastt.put('/admin/nudge-config/bulk', { account_ids: ids, config: cfg, enable: false });
      Fastt.saved('Copied to ' + (res.count != null ? res.count : ids.length) + ' model' + ((res.count === 1) ? '' : 's') + ' (paused)');
    } catch (e) { Fastt.oops(e); }
    btn.disabled = false;
  });

  // pool photo strips (both editors)
  document.addEventListener('click', function (e) {
    var mini = e.target.closest('.bc-mini.pick');
    if (!mini) return;
    var owner = mini.dataset.owner, pool = mini.dataset.pool;
    if (!editPoolImages(owner, pool)) return;
    if (owner === 'nudge') { renderPools(); renderPreviewCard(); save(); }
    else { mnRenderPools(); Fastt.toast('Photo pool staged — hit “Save Mass Nudge” to write it'); }
  });

  // ---- online_blast: honest state (this account has no such rule) ----
  function renderOnlineBlast() {
    var r = ruleOf('online_blast');
    var body = $('#bc-ob-body');
    if (!r) {
      body.innerHTML = 'No <code>online_blast</code> rule exists on this creator, so there is nothing to show or edit — ' +
        'the earlier status pill printed a bare dash where that fact belongs. ' +
        'A blast rule created empty falls back to the sender’s built-in default lines, which is why this page will not mint one.';
      Fastt.staticBadge($('#bc-ob > h4'), 'NO RULE ON THIS ACCOUNT');
      return;
    }
    var stats = (r.last_run && r.last_run.stats) || {};
    body.innerHTML = 'Rule <b>#' + esc(r.id) + '</b> · ' + (r.is_enabled ? 'running' : 'disabled') +
      ' · fires every ' + Math.round((r.every_seconds || 3600) / 60) + ' min · last run ' +
      Fastt.fmtAgo(r.last_run && r.last_run.started_at) +
      (stats.sent != null ? ' · ' + stats.sent + ' sent' : '') +
      '. Toggle it from the pill at the top of this page.';
    Fastt.liveBadge($('#bc-ob > h4'));
  }

  // ---- static bits ----
  Fastt.staticBadge($('#bc-audience .fx-card-h'), 'CONTROLS STATIC');
  Fastt.staticBadge($('#bc-tz-field > label'), 'STATIC');
  Fastt.staticBadge($('#bc-ck-welcomed'), 'FIXED');
  Fastt.liveBadge($('#bc-status'));
  Fastt.liveBadge($('#bc-master .fx-tc-title'));
  Fastt.liveBadge($('#bc-pool-card .fx-card-h'));
  Fastt.liveBadge($('.fx-adv-head'));

  Fastt.liveBadge($('#bc-killgrp > h4'));
  Fastt.liveBadge($('#bc-rollout > h4'));

  // ---- initial paint ----
  fillForm();
  renderPools();
  renderStatus();
  renderMaster();
  renderPreviewCard();
  mnRender();
  renderOnlineBlast();
  roRender();
});
