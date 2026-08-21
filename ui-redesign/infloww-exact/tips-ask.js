Fastt.ready(async function () {
  var $ = Fastt.$, esc = Fastt.esc;
  // Mark the strip's numbers as mock-up via the shared badge helper (consistent
  // vocabulary with the rest of the skin). Injected before the account check so
  // the no-creator placeholder state keeps the badge.
  Fastt.staticBadge($('#ta-sample-badge'), 'SAMPLE NUMBERS — mock-up, not real activity');
  // No creator picked: fastt.js shows the global placeholder banner and the
  // strip keeps its "SAMPLE NUMBERS" static badge.
  if (!Fastt.account()) return;

  // Real creator in scope — drop the mock-up counters synchronously, before the
  // first await, so fabricated numbers never show alongside live config.
  var sampleBadge = $('#ta-sample-badge'); if (sampleBadge) sampleBadge.remove();
  ['ta-st-ask', 'ta-st-nudge', 'ta-st-amt', 'ta-st-cool', 'ta-st-cfg'].forEach(function (id) {
    var el = $('#' + id); if (el) { el.innerHTML = '<i></i>Loading…'; el.classList.remove('ok'); }
  });
  $('#ta-act-n').textContent = '…';
  $('#ta-act-body').innerHTML = '<div class="fx-kv" style="padding:10px 0">Loading recent tips…</div>';

  var out = await Fastt.get('/admin/tip-reward-config');
  var stored = out.config || {};
  var defaults = out.defaults || {};
  // tip_request defaults mirror service/automations/tip_request.py _DEFAULTS
  var TR_DEF = { enabled: false, media_id: null,
                 caption: 'hope you loved that 🥺 wanna send me a lil tip so i keep going?',
                 min_wait_hours: 2,
                 max_age_hours: 48, cooldown_hours: 168, guard_hours: 6, limit: 200 };
  function M() { var m = {}; Object.assign(m, defaults, stored); return m; }
  function TR() { var t = {}; Object.assign(t, TR_DEF, stored.tip_request || {}); return t; }
  function setTR(k, v) {
    var t = {}; Object.assign(t, stored.tip_request || {}); t[k] = v; stored.tip_request = t;
  }

  var saving = false;
  async function save() {
    if (saving) return; saving = true;
    try {
      var r = await Fastt.put('/admin/tip-reward-config',
        { account_id: Fastt.account(), config: stored });
      if (r && r.config) stored = r.config;
      Fastt.saved();
    } catch (e) { Fastt.oops(e); }
    finally { saving = false; }
    await refreshMediaPreview();
    render();
  }
  var saveDeb = Fastt.debounce(save, 700);

  // ── real outcome data: tips received in the last 30 days ──
  var tips = [], tipsOk = false;
  try {
    var to = new Date(Date.now() + 86400e3).toISOString().slice(0, 10); // inclusive-midnight bound → +1d keeps today
    var from = new Date(Date.now() - 30 * 86400e3).toISOString().slice(0, 10);
    tips = (await Fastt.get('/admin/tips-list', { from: from, to: to, limit: 50 })).rows || [];
    tipsOk = true;
  } catch (e) { /* card says the ledger could not be read */ }

  // ── the cold-buyer nudge's own run log (its automation kind is tip_request) ──
  var nudgeRuns = [], nudgeOk = false;
  try {
    nudgeRuns = (await Fastt.get('/admin/stats/automation-runs',
      { kind: 'tip_request', limit: 50 })).runs || [];
    nudgeOk = true;
  } catch (e) { /* the note says the log is unreadable */ }

  // ── the nudge only fires if a scheduled tip_request rule exists AND is on;
  //    without it the enabled flag below is inert. Read the true firing state. ──
  var trRule = null, rulesOk = false;
  try {
    var rr = await Fastt.get('/admin/automation-rules');
    trRule = (rr.rules || []).filter(function (x) { return x.kind === 'tip_request'; })[0] || null;
    rulesOk = true;
  } catch (e) { /* readiness note falls back to config-only wording */ }

  // ── vault folders, for the teaser-image picker ──
  var vaultFolders = [];
  try {
    var vl = await Fastt.get('/api/of/v2/vault/lists', { limit: 50 }); // route caps at le=50 — 100 422s
    vaultFolders = (vl.list || []).filter(function (x) {
      return x && x.name && ((x.photosCount || 0) + (x.videosCount || 0) + (x.gifsCount || 0)) > 0;
    }).map(function (x) {
      return { id: x.id, name: x.name, photos: x.photosCount || 0, videos: x.videosCount || 0, gifs: x.gifsCount || 0 };
    });
  } catch (e) { /* picker falls back to a typed media id */ }

  // ── preview of the chosen media (files.thumb from the OF mirror) ──
  var mediaPreview = { id: null, url: '', err: '' };
  async function refreshMediaPreview() {
    var id = TR().media_id;
    if (!id) { mediaPreview = { id: null, url: '', err: '' }; return; }
    if (mediaPreview.id === id && (mediaPreview.url || mediaPreview.err)) return;
    mediaPreview = { id: id, url: '', err: '' };
    try {
      var md = await Fastt.get('/api/of/v2/vault/media/' + id);
      var f = md.files || {};
      mediaPreview.url = (f.thumb && f.thumb.url) || (f.squarePreview && f.squarePreview.url)
        || (f.preview && f.preview.url) || (f.full && f.full.url) || '';
      if (!mediaPreview.url) mediaPreview.err = 'this item has no preview file';
    } catch (e) {
      mediaPreview.err = (e && e.status === 404)
        ? 'this media id is no longer in the vault'
        : 'could not read this media from OF';
    }
  }

  function setSwitch(sw, on, onWord) {
    if (!sw) return;
    sw.classList.toggle('on', on);
    var tc = sw.closest('.fx-togglecard');
    if (tc) {
      tc.classList.toggle('on', on);
      var st = tc.querySelector('.fx-tc-state'); if (st) st.textContent = on ? (onWord || 'Running') : 'Off';
    }
  }
  function chip(id, text, ok) {
    var el = $('#' + id); if (!el) return;
    el.innerHTML = '<i></i>' + esc(text);
    if (ok !== undefined) el.classList.toggle('ok', !!ok);
  }
  var IMG_SVG = '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><rect x="3" y="5" width="18" height="14" rx="2.5"/><circle cx="8.5" cy="10" r="1.6"/><path d="M21 16l-5-5-9 8" stroke-linejoin="round"/></svg>';

  function render() {
    var m = M(), t = TR();
    setSwitch($('.fx-switch[data-k="ask_enabled"]'), !!m.ask_enabled);
    setSwitch($('.fx-switch[data-tr="enabled"]'), !!t.enabled);
    chip('ta-st-ask', m.ask_enabled ? 'Ask running' : 'Ask off', !!m.ask_enabled);
    chip('ta-st-nudge', t.enabled ? 'Cold-buyer nudge on' : 'Cold-buyer nudge off', !!t.enabled);

    // REAL: what actually landed, not a config echo
    var sum = 0;
    tips.forEach(function (x) { sum += x.amount_cents || 0; });
    if (!tipsOk) chip('ta-st-amt', 'Tip ledger unreadable');
    else if (!tips.length) chip('ta-st-amt', 'No tips in 30 days');
    else chip('ta-st-amt', tips.length + (tips.length === 1 ? ' tip' : ' tips')
      + ' · ' + Fastt.fmtCents(sum) + ' in 30 days', true);
    if (!tipsOk) chip('ta-st-cool', 'Last tip unknown');
    else chip('ta-st-cool', tips.length ? 'Last tip ' + Fastt.fmtAgo(tips[0].occurred_at) : 'No recent tips');
    chip('ta-st-cfg', m.ask_amount_dollars
      ? 'Asks for $' + m.ask_amount_dollars + ' · nudge every ' + t.cooldown_hours + ' h'
      : 'No set amount — nudge every ' + t.cooldown_hours + ' h, max ' + t.limit + '/sweep');

    $('#ta-amt').value = m.ask_amount_dollars == null ? '' : m.ask_amount_dollars;
    $('#ta-tpl').value = m.ask_template || '';
    $('#ta-nudge-cap').value = t.caption || '';

    // teaser image: a real preview, not a gradient placeholder
    var tile = $('#ta-media-tile');
    if (!t.media_id) {
      tile.innerHTML = IMG_SVG;
      $('#ta-media-sub').innerHTML = '<span style="color:var(--muted)">No image chosen yet — the nudge has nothing to send until you pick one</span>';
      $('#ta-media-clear').style.display = 'none';
      $('#ta-media-btn').textContent = 'Choose from vault';
    } else if (mediaPreview.url) {
      tile.innerHTML = '<img src="' + esc(mediaPreview.url) + '" alt="">';
      $('#ta-media-sub').innerHTML = 'Vault media <b>#' + esc(t.media_id)
        + '</b> · sent free — the same one image on every nudge';
      $('#ta-media-clear').style.display = '';
      $('#ta-media-btn').textContent = 'Change';
    } else {
      tile.innerHTML = IMG_SVG;
      $('#ta-media-sub').innerHTML = 'Vault media <b>#' + esc(t.media_id) + '</b> — <span style="color:#e8cf9a">'
        + esc(mediaPreview.err || 'preview unavailable') + '</span>';
      $('#ta-media-clear').style.display = '';
      $('#ta-media-btn').textContent = 'Change';
    }

    // ── honest firing-state banner: the enabled flag alone does NOT send ──
    var warn = $('#ta-nudge-warn'), warnTxt = $('#ta-nudge-warn-txt');
    var blockers = [];
    if (t.enabled) {
      if (!t.media_id) blockers.push('no teaser image is set');
      if (rulesOk && !trRule)
        blockers.push('no scheduled sweep runs it — add a <b>tip_request</b> automation rule with a cadence trigger in <a href="automations.html" style="color:#7d97f8">AI Selling → Overview</a>');
      else if (rulesOk && trRule && !trRule.is_enabled)
        blockers.push('its scheduled sweep rule is switched off');
    }
    if (t.enabled && blockers.length) {
      warn.style.display = '';
      warnTxt.innerHTML = 'The nudge is switched on but <b>won’t send yet</b> — ' + blockers.join(', and ') + '.';
    } else {
      warn.style.display = 'none';
    }

    var sel = $('#ta-cool');
    var have = Array.prototype.some.call(sel.options, function (o) { return +o.value === +t.cooldown_hours; });
    if (!have) {
      var o = document.createElement('option');
      o.value = t.cooldown_hours; o.textContent = 'Every ' + t.cooldown_hours + ' h';
      sel.appendChild(o);
    }
    sel.value = String(t.cooldown_hours);
    $('#ta-wait').value = t.min_wait_hours;
    $('#ta-age').value = t.max_age_hours;
    $('#ta-cool2').value = t.cooldown_hours;
    $('#ta-guard').value = t.guard_hours;
    $('#ta-limit').value = t.limit;

    // ── outcome card ──
    var body = $('#ta-act-body');
    $('#ta-act-n').textContent = tipsOk ? (tips.length + (tips.length === 1 ? ' tip' : ' tips')) : '—';
    if (!tipsOk) {
      $('#ta-act-sub').textContent = 'The tip ledger could not be read for this creator.';
      body.innerHTML = '<div class="fx-kv" style="padding:10px 0">GET /admin/tips-list failed — this page cannot say what landed.</div>';
    } else if (!tips.length) {
      $('#ta-act-sub').textContent = 'The outcome side of the ask — every tip this creator received in the last 30 days.';
      body.innerHTML = '<div class="fx-kv" style="padding:10px 0;color:var(--muted);display:block;line-height:1.5">'
        + 'No tip landed in the last 30 days. The ask is '
        + (m.ask_enabled ? '<b>on</b>' : '<b>off</b>')
        + ' — it only fires inside an AI Chatter / Auto Convo reply, so it needs those lanes running too.</div>';
    } else {
      $('#ta-act-sub').textContent = 'The outcome side of the ask — every tip this creator received in the last 30 days ('
        + Fastt.fmtCents(sum) + ' total).';
      body.innerHTML = tips.slice(0, 8).map(function (x) {
        var fan = x.fan || {}; // tips-list nests names under row.fan (service/transactions.py)
        var name = fan.display_name || fan.username || ('fan #' + x.fan_id);
        return '<div class="fx-kv" style="padding:8px 0;border-top:1px solid var(--border-soft)">'
          + '<b style="color:#fff">' + esc(name) + '</b> tipped '
          + '<b style="color:var(--green)">' + esc(Fastt.fmtCents(x.amount_cents)) + '</b>'
          + '<span style="margin-left:auto;color:var(--muted2)">' + esc(Fastt.fmtAgo(x.occurred_at)) + '</span></div>';
      }).join('');
    }

    // ── the nudge's own run log (kind=tip_request) ──
    var nt = $('#ta-nudge-runs-txt');
    if (!nudgeOk) {
      nt.innerHTML = 'The cold-buyer nudge run log could not be read — GET /admin/stats/automation-runs failed.';
    } else if (!nudgeRuns.length) {
      var stateWord;
      if (!t.enabled) stateWord = 'off';
      else if (rulesOk && !trRule) stateWord = 'on, but no scheduled sweep exists yet to run it';
      else if (rulesOk && trRule && !trRule.is_enabled) stateWord = 'on, but its scheduled sweep is switched off';
      else stateWord = 'on, waiting for its next sweep';
      nt.innerHTML = 'The cold-buyer nudge has <b>never run</b> for this creator — <b>0</b> entries in the automation run log. '
        + 'It is currently <b>' + stateWord + '</b>.';
    } else {
      var last = nudgeRuns[0], ls = {};
      try { ls = JSON.parse(last.stats_json || '{}') || {}; } catch (e) { ls = {}; }
      nt.innerHTML = 'Cold-buyer nudge: <b>' + nudgeRuns.length + '</b> run'
        + (nudgeRuns.length === 1 ? '' : 's') + ' in the log · last one '
        + esc(Fastt.fmtAgo(last.started_at))
        + (ls.sent != null ? ' (sent ' + esc(ls.sent) + ')' : '') + '.';
    }
  }

  // inputs
  $('#ta-amt').addEventListener('change', function () {
    var v = this.value.trim();
    if (v === '') { delete stored.ask_amount_dollars; saveDeb(); return; }
    var n = parseInt(v, 10);
    if (isNaN(n) || n < 1) { render(); return; }
    stored.ask_amount_dollars = n; saveDeb();
  });
  $('#ta-tpl').addEventListener('change', function () { stored.ask_template = this.value; saveDeb(); });
  $('#ta-nudge-cap').addEventListener('change', function () { setTR('caption', this.value); saveDeb(); });
  $('#ta-cool').addEventListener('change', function () {
    var n = parseInt(this.value, 10); if (isNaN(n)) return;
    setTR('cooldown_hours', n); saveDeb();
  });
  function bindTRInt(id, key, lo) {
    $('#' + id).addEventListener('change', function () {
      var n = parseInt(this.value, 10);
      if (isNaN(n) || n < lo) { render(); return; }
      setTR(key, n); saveDeb();
    });
  }
  bindTRInt('ta-wait', 'min_wait_hours', 0);
  bindTRInt('ta-age', 'max_age_hours', 1);
  bindTRInt('ta-cool2', 'cooldown_hours', 0);
  bindTRInt('ta-guard', 'guard_hours', 0);
  bindTRInt('ta-limit', 'limit', 1);

  // ── vault media picker: pick the ONE teaser image by eye, not by id ──
  function openMediaPicker(currentId) {
    if (!vaultFolders.length) {   // OF mirror unreachable → keep it usable, typed
      var v = prompt('Vault media id for the nudge teaser image (a number; blank = clear).\n'
                     + 'The vault folder list could not be read, so there is nothing to browse.',
                     currentId == null ? '' : currentId);
      if (v === null) return;
      v = v.trim();
      if (v === '') { setTR('media_id', null); save(); return; }
      var n0 = parseInt(v, 10);
      if (isNaN(n0)) { Fastt.toast('Media id must be a number', 'err'); return; }
      setTR('media_id', n0); save();
      return;
    }
    var picked = currentId || null;
    var back = document.createElement('div');
    back.className = 'mp-back';
    back.innerHTML = '<div class="mp-panel">'
      + '<div class="mp-head">Pick the nudge teaser image'
      + '<select class="fx-select" data-mp="folder">' + vaultFolders.map(function (f) {
          return '<option value="' + esc(f.id) + '">' + esc(f.name) + ' (' + f.photos + ' photos'
            + (f.videos ? ' · ' + f.videos + ' videos' : '') + ')</option>';
        }).join('') + '</select>'
      + '<span class="sub">One image, sent free with the caption below it. Videos are shown but a still photo reads best.</span>'
      + '</div><div class="mp-grid"></div>'
      + '<div class="mp-foot"><span class="pickid" data-mp="pickid"></span>'
      + '<button class="fx-btn ghost" data-mp="cancel">Cancel</button>'
      + '<button class="fx-btn" data-mp="ok">Use this image</button></div></div>';
    var grid = back.querySelector('.mp-grid');
    var folderSel = back.querySelector('[data-mp="folder"]');
    var pickIdEl = back.querySelector('[data-mp="pickid"]');
    function setPickLabel() {
      pickIdEl.textContent = picked ? 'Selected: media #' + picked : 'Nothing selected yet';
    }
    async function loadFolder() {
      grid.innerHTML = '<div class="mp-msg">Loading…</div>';
      try {
        var r = await Fastt.get('/api/of/v2/vault/media',
          { list_id: folderSel.value, limit: 30 });
        var items = (r.list || []).filter(function (it) {
          return it && it.canView && !it.hasError && it.files && (it.files.thumb || it.files.squarePreview);
        });
        if (!items.length) { grid.innerHTML = '<div class="mp-msg">Nothing viewable in this folder.</div>'; return; }
        grid.innerHTML = items.map(function (it) {
          var u = (it.files.thumb && it.files.thumb.url) || (it.files.squarePreview && it.files.squarePreview.url);
          return '<div class="mp-cell' + (String(it.id) === String(picked) ? ' on' : '') + '" data-mid="' + esc(it.id) + '">'
            + '<img src="' + esc(u) + '" alt="" loading="lazy">'
            + (it.type !== 'photo' ? '<span class="vid">' + esc(it.type) + '</span>' : '') + '</div>';
        }).join('');
      } catch (e) {
        grid.innerHTML = '<div class="mp-msg">Could not load this folder from OF — '
          + esc((e && e.message) || 'request failed') + '</div>';
      }
    }
    folderSel.addEventListener('change', loadFolder);
    back.addEventListener('click', function (e) {
      if (e.target === back) { back.remove(); return; }
      var act = e.target.closest('[data-mp]');
      if (act && act.dataset.mp === 'cancel') { back.remove(); return; }
      if (act && act.dataset.mp === 'ok') {
        back.remove();
        if (picked) { setTR('media_id', parseInt(picked, 10)); save(); }
        return;
      }
      var cell = e.target.closest('.mp-cell');
      if (!cell) return;
      picked = cell.dataset.mid;
      grid.querySelectorAll('.mp-cell').forEach(function (c) { c.classList.remove('on'); });
      cell.classList.add('on');
      setPickLabel();
    });
    function onKey(e) {
      if (!document.body.contains(back)) { document.removeEventListener('keydown', onKey); return; }
      if (e.key === 'Escape') { document.removeEventListener('keydown', onKey); back.remove(); }
    }
    document.addEventListener('keydown', onKey);
    document.body.appendChild(back);
    setPickLabel();
    loadFolder();
  }

  $('#ta-media-btn').addEventListener('click', function () { openMediaPicker(TR().media_id); });
  $('#ta-media-clear').addEventListener('click', function () {
    if (!confirm('Clear the nudge teaser image? The nudge sends nothing until one is set.')) return;
    setTR('media_id', null); save();
  });

  document.addEventListener('click', function (e) {
    var a = e.target.closest('.fx-switch[data-k="ask_enabled"]');
    if (a) { stored.ask_enabled = a.classList.contains('on'); save(); return; }
    var b = e.target.closest('.fx-switch[data-tr="enabled"]');
    if (b) { setTR('enabled', b.classList.contains('on')); save(); return; }
  });

  Fastt.liveBadge($('.fx-togglecard .fx-tc-title'));
  Fastt.liveBadge($('#ta-nudge-card .fx-tc-title'));
  Fastt.liveBadge($('#ta-activity .fx-card-h'));
  await refreshMediaPreview();
  render();
});
