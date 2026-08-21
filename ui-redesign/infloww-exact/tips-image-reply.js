Fastt.ready(async function () {
  var $ = Fastt.$, esc = Fastt.esc;
  // No creator picked: fastt.js shows the global placeholder banner; mark the
  // strip's mock-up counters with the shared static badge helper.
  if (!Fastt.account()) {
    Fastt.staticBadge($('#ir-strip'), 'SAMPLE NUMBERS — mock-up, not real activity');
    var mb = $('#ir-strip > .ft-static'); if (mb) mb.style.alignSelf = 'center';
    return;
  }

  // Real creator in scope — drop the mock-up counters synchronously, before the
  // first await, so fabricated numbers never show alongside live config.
  var sampleBadge = $('#ir-strip > .ft-static'); if (sampleBadge) sampleBadge.remove();
  ['ir-st-1', 'ir-st-2', 'ir-st-3', 'ir-st-4', 'ir-st-5'].forEach(function (id) {
    var el = $('#' + id); if (el) { el.innerHTML = '<i></i>Loading…'; el.classList.remove('ok'); }
  });
  $('#ir-act-n').textContent = '…';
  $('#ir-act-body').innerHTML =
    '<div class="pb-row first"><div class="pb-txt" style="color:var(--muted)">Reading the automation run log…</div></div>';

  var out = await Fastt.get('/admin/tip-reward-config');
  var stored = out.config || {};
  var defaults = out.defaults || {};
  function M() { var m = {}; Object.assign(m, defaults, stored); return m; }

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
    render();
  }
  var saveDeb = Fastt.debounce(save, 700);

  // ── vault folders: names + real counts + a signed first thumbnail per folder ──
  var vaultFolders = [], vaultByName = {};
  try {
    var vl = await Fastt.get('/api/of/v2/vault/lists', { limit: 50 }); // route caps at le=50 — 100 422s
    vaultFolders = (vl.list || []).filter(function (x) { return x && x.name; }).map(function (x) {
      var ms = x.medias || [];
      return { name: x.name, photos: x.photosCount || 0, videos: x.videosCount || 0,
               gifs: x.gifsCount || 0, audios: x.audiosCount || 0, thumb: (ms[0] && ms[0].url) || '' };
    });
    vaultFolders.forEach(function (f) { vaultByName[f.name] = f; });
  } catch (e) { /* selects fall back to the stored folder name only */ }
  var vaultNames = vaultFolders.map(function (f) { return f.name; });
  function folderTotal(n) { var f = vaultByName[n]; return f ? (f.photos + f.videos + f.gifs + f.audios) : -1; }
  function folderNote(n) {
    if (!n) return '';
    var t = folderTotal(n);
    if (t < 0) return ' (not in this vault)';
    if (t === 0) return ' (empty — nothing will send)';
    return ' (' + t + ' items)';
  }

  // ── what this lane actually did: tip_reward runs carrying image_reply:true ──
  var picRuns = [], runsOk = false, runWindowDays = 0, lifeImageReply = null;
  try {
    var rr = await Fastt.get('/admin/stats/automation-runs', { kind: 'tip_reward', limit: 200 });
    var all = (rr.runs || []).map(function (r) {
      var s = {};
      try { s = JSON.parse(r.stats_json || '{}') || {}; } catch (e2) { s = {}; }
      return { run: r, s: s };
    });
    runsOk = true;
    picRuns = all.filter(function (x) { return !!x.s.image_reply; });
    if (all.length) {
      var oldest = Fastt.parseUtc(all[all.length - 1].run.started_at);
      if (oldest) runWindowDays = Math.max(1, Math.round((Date.now() - oldest.getTime()) / 86400e3));
    }
  } catch (e) { /* runsOk stays false → the card says the log is unreadable */ }
  try {
    var pa = await Fastt.get('/admin/stats/per-automation', { from: '2020-01-01' });
    (pa.rows || []).forEach(function (r) {
      if (r.automation === 'image_reply') lifeImageReply = r.messages_sent || 0;
    });
  } catch (e) { /* the cross-check line is simply omitted */ }

  var sentRuns = picRuns.filter(function (x) { return x.s.status === 'ok'; });
  var throttled = picRuns.filter(function (x) { return x.s.reason === 'throttled'; }).length;
  var blocked = picRuns.filter(function (x) { return x.s.reason === 'restricted'; }).length;
  var otherSkip = picRuns.length - sentRuns.length - throttled - blocked;

  // OF display names are empty/synthetic for most fans and the operator nickname
  // is "Name/City/age/job" — but only when a name was ever learned. Prefer a real
  // display name, then a nickname head that isn't a "City,Country" chunk.
  function initials(nm) {
    if (/^fan #/.test(nm)) return '#';
    return String(nm).split(/\s+/).map(function (w) { return w[0] || ''; }).join('').slice(0, 2).toUpperCase();
  }
  function fanLabel(f, id) {
    var synth = function (s) { return !s || /^u\d+$/i.test(String(s).trim()); };
    var nm = String((f && f.name) || '').trim();
    var un = String((f && f.username) || '').trim();
    var nick = String((f && f.customNickname) || '').split('/')[0].trim();
    if (!synth(nm)) return nm;
    if (nick && nick.indexOf(',') < 0) return nick;
    if (!synth(un)) return un;
    return 'fan #' + id;
  }
  // fan names for the feed (run stats only carry fan_id)
  var fanNames = {}, want = {};
  picRuns.slice(0, 24).forEach(function (x) { if (x.s.fan_id) want[x.s.fan_id] = 1; });
  var ids = Object.keys(want);
  if (ids.length) {
    try {
      var fo = await Fastt.get('/admin/fans/' + Fastt.account() + '/by-ids', { ids: ids.join(',') });
      Object.keys(fo.fans || {}).forEach(function (k) { fanNames[k] = fanLabel(fo.fans[k], k); });
    } catch (e) { /* rows fall back to "fan #id" */ }
  }

  function setSwitch(sw, on) {
    if (!sw) return;
    sw.classList.toggle('on', on);
    var tc = sw.closest('.fx-togglecard');
    if (tc) {
      tc.classList.toggle('on', on);
      var st = tc.querySelector('.fx-tc-state'); if (st) st.textContent = on ? 'Running' : 'Off';
    }
  }
  function chip(id, html, ok) {
    var el = $('#' + id); if (!el) return;
    el.innerHTML = '<i></i>' + html;
    if (ok !== undefined) el.classList.toggle('ok', !!ok);
  }
  function folderOptions(current) {
    var names = vaultNames.slice();
    if (current && names.indexOf(current) < 0) names.unshift(current);
    return '<option value="">— no folder —</option>' + names.map(function (n) {
      return '<option value="' + esc(n) + '"' + (n === current ? ' selected' : '') + '>'
        + esc(n) + esc(folderNote(n)) + '</option>';
    }).join('');
  }
  function fillFolderSelect(sel, current) {
    sel.innerHTML = folderOptions(current);
    sel.value = current || '';
  }
  var LOCK_SVG = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="#c9d3ff" stroke-width="1.9"><rect x="5" y="10" width="14" height="10" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3"/></svg>';
  var ARROW = '<span class="tz-arrow"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#6a6a6a" stroke-width="1.9"><path d="M4 12h14M14 7l5 5-5 5" stroke-linecap="round" stroke-linejoin="round"/></svg></span>';
  var AVBG = ['linear-gradient(135deg,#7d97f8,#4166f6)', 'linear-gradient(135deg,#67d1ae,#2f8f6c)',
              'linear-gradient(135deg,#a78bfa,#6d4bd6)', 'linear-gradient(135deg,#e5735b,#b4432c)'];
  function art(folder) {
    var f = vaultByName[folder];
    return (f && f.thumb) ? '<img src="' + esc(f.thumb) + '" alt="" loading="lazy">' : '';
  }

  function render() {
    var m = M();
    setSwitch($('#ir-card .fx-switch[data-k]'), !!m.image_reply_enabled);
    setSwitch($('#ht-card .fx-switch[data-k]'), !!m.hot_teaser_enabled);
    setSwitch($('#tc-card .fx-switch[data-k]'), !!m.teaser_convo_enabled);
    $('#ir-closer').classList.toggle('on', !!m.image_closer_enabled);

    var onCount = [m.image_reply_enabled, m.hot_teaser_enabled, m.teaser_convo_enabled]
      .filter(Boolean).length;
    chip('ir-st-1', onCount ? onCount + ' of 3 plays on' : 'All three plays off', onCount > 0);
    // REAL activity, not a config echo
    chip('ir-st-2', !runsOk
      ? 'Run log unreadable'
      : (sentRuns.length + ' pic-backs · ' + throttled + ' throttled · ' + blocked
         + ' blocked (' + (runWindowDays || 30) + ' d)'),
      runsOk && sentRuns.length > 0);
    chip('ir-st-3', 'Hot teaser: ' + esc(m.hot_teaser_count) + ' pics · ' + esc(Fastt.fmtCents(m.hot_teaser_price_cents)) + ' paid');
    var rungs = m.teaser_convo_rungs || [];
    chip('ir-st-4', 'Ladder: ' + rungs.length + ' rungs, every ' + esc(m.teaser_convo_after_fan_msgs) + ' fan msgs');
    if (!runsOk) chip('ir-st-5', 'No run log');
    else if (!sentRuns.length) chip('ir-st-5', 'No pic-back has fired yet');
    else {
      var last = sentRuns[0];
      chip('ir-st-5', 'Last pic-back: ' + esc(fanNames[last.s.fan_id] || ('fan #' + last.s.fan_id))
        + ' · ' + esc(Fastt.fmtAgo(last.run.started_at)));
    }

    $('#ir-kv').innerHTML = '<b>' + esc(m.image_reply_count) + '</b> pic back · once per fan every <b>'
      + esc(m.image_reply_cooldown_hours) + ' h</b>' + (m.image_reply_caption ? ' · with caption' : ' · media-only');
    $('#ht-kv').innerHTML = 'New fans: <b>' + esc(m.hot_teaser_count) + ' free</b> from <b>'
      + esc(m.hot_teaser_free_folder || 'no folder set') + '</b>' + esc(folderNote(m.hot_teaser_free_folder))
      + ' (max ' + esc(m.hot_teaser_free_max)
      + ' ever) · buyers: <b>' + esc(Fastt.fmtCents(m.hot_teaser_price_cents)) + '</b> unlock from <b>'
      + esc(m.hot_teaser_paid_folder || 'no folder set') + '</b>' + esc(folderNote(m.hot_teaser_paid_folder));
    $('#tc-kv').innerHTML = 'Starts after <b>~' + esc(m.teaser_convo_after_fan_msgs) + '</b> fan messages · '
      + (m.teaser_convo_adaptive !== false
          ? 'adaptive: <b>climbs</b> a rung when he buys, <b>softens</b> when he passes'
          : 'legacy: <b>climbs</b> a rung every send');

    // ── pic-back diagram: the tile shows the folder the freebie is actually drawn from ──
    // (tip_reward resolves image_reply_basis_cents against the tier ladder)
    var basisNow = m.image_reply_basis_cents == null ? 999 : m.image_reply_basis_cents;
    var tierSorted = (m.tiers || []).slice().sort(function (a, b) {
      return (a.min_basis_cents || 0) - (b.min_basis_cents || 0);
    });
    var hitTier = null;
    tierSorted.forEach(function (t) { if (basisNow >= (t.min_basis_cents || 0)) hitTier = t; });
    var backFolder = (hitTier && (hitTier.folders || [])[0]) || '';
    $('#ir-back-tile').innerHTML = art(backFolder);
    $('#ir-back-lbl').innerHTML = 'she sends back · <b>free</b>'
      + (backFolder ? ' · <b>' + esc(backFolder) + '</b>' : ' · <b style="color:#e8cf9a">no folder set</b>');

    // ── "Pull the freebie from" select — built live from the real tier ladder ──
    // (image_reply resolves image_reply_basis_cents against the same tiers as the
    // pic-back diagram, so the option that's selected IS the folder in the tile).
    var tsel = $('#ir-tier');
    if (tsel) {
      if (!tierSorted.length) {
        tsel.innerHTML = '<option value="">— no tiers configured (set them on Tip Rewards) —</option>';
        tsel.disabled = true;
      } else {
        tsel.disabled = false;
        var hitBasis = hitTier ? (hitTier.min_basis_cents || 0) : (tierSorted[0].min_basis_cents || 0);
        tsel.innerHTML = tierSorted.map(function (t) {
          var mb = t.min_basis_cents || 0;
          var fld = (t.folders || []).filter(Boolean)[0] || 'no folder';
          var thr = mb > 0 ? '$' + (mb / 100) + '+ basis' : 'default';
          return '<option value="' + mb + '"' + (mb === hitBasis ? ' selected' : '') + '>'
            + esc((t.name || 'tier') + ' · ' + thr + ' — ' + fld) + '</option>';
        }).join('');
        tsel.value = String(hitBasis);
      }
    }

    // ── hot-teaser diagram: N free tiles from the FREE folder, then the priced unlock ──
    var freeWanted = Math.max(0, parseInt(m.hot_teaser_count, 10) || 0);
    var freeShown = Math.min(freeWanted, 6);
    var freeArt = art(m.hot_teaser_free_folder);
    var bits = [];
    if (!freeShown) {
      bits.push('<div class="tz-item"><div class="tz-tile fan"></div><span>no free pics</span></div>');
    } else {
      for (var i = 0; i < freeShown; i++) {
        bits.push('<div class="tz-item"><div class="tz-tile g' + ((i % 3) + 2) + '">' + freeArt + '</div>'
          + '<span>' + (i === 0
              ? 'free · <b>' + esc(m.hot_teaser_free_folder || 'no folder') + '</b>'
              : 'free') + '</span></div>');
      }
      if (freeWanted > freeShown) {
        bits.push('<div class="tz-item"><div class="tz-tile fan" style="font-size:12px;color:var(--muted)">+'
          + (freeWanted - freeShown) + '</div><span>more</span></div>');
      }
    }
    bits.push(ARROW);
    bits.push('<div class="tz-item"><div class="tz-tile lock">' + art(m.hot_teaser_paid_folder) + LOCK_SVG
      + '<b>' + esc(Fastt.fmtCents(m.hot_teaser_price_cents).replace(/\.00$/, '')) + '</b></div>'
      + '<span>then priced · <b>' + esc(m.hot_teaser_paid_folder || 'no folder') + '</b></span></div>');
    $('#ht-demo').innerHTML = bits.join('');

    // ladder demo strip — the real rungs
    var demo = $('#tc-demo');
    if (!rungs.length) {
      demo.innerHTML = '<div class="tz-item"><span style="color:var(--muted);font-size:12.5px">No rungs configured — the ladder is inert until folders are set.</span></div>';
    } else {
      demo.innerHTML = rungs.map(function (r, i) {
        var price = r.price_cents ? Fastt.fmtCents(r.price_cents).replace(/\.00$/, '') : 'free';
        var count = r.count || m.teaser_convo_count || 1;
        return '<div class="tz-item"><div class="tz-tile lock">' + art(r.folder) + LOCK_SVG + '<b>' + esc(price) + '</b></div><span><b>'
          + esc(r.folder || 'no folder') + '</b> · ' + esc(count) + (count === 1 ? ' pic' : ' pics') + '</span></div>';
      }).join(ARROW);
    }

    // advanced knobs
    $('#ir-count').value = m.image_reply_count;
    $('#ir-cool').value = m.image_reply_cooldown_hours;
    $('#ir-cap').value = m.image_reply_caption || '';
    $('#ht-count').value = m.hot_teaser_count;
    $('#ht-free-max').value = m.hot_teaser_free_max;
    $('#ht-price').value = (m.hot_teaser_price_cents || 0) / 100;
    $('#ht-cool').value = m.hot_teaser_cooldown_hours;
    fillFolderSelect($('#ht-free-folder'), m.hot_teaser_free_folder || '');
    fillFolderSelect($('#ht-paid-folder'), m.hot_teaser_paid_folder || '');

    // conversation-ladder editor
    $('#tc-after').value = m.teaser_convo_after_fan_msgs;
    $('#tc-count').value = m.teaser_convo_count;
    $('#tc-adaptive').value = m.teaser_convo_adaptive !== false
      ? 'Climbs only when he buys' : 'Climbs on every send (legacy)';
    $('#tc-rungs').innerHTML = rungs.map(function (r, i) {
      return '<div class="cv-rung"><span class="n">' + (i + 1) + '</span>'
        + '<select class="fx-select" data-cvf="' + i + '">' + folderOptions(r.folder || '') + '</select>'
        + '<div class="fx-unit" data-unit="$"><input class="fx-input" data-cvp="' + i + '" value="'
        + esc(((r.price_cents || 0) / 100)) + '"></div>'
        + '<span class="tag">' + (r.price_cents ? 'priced PPV' : 'free tease') + '</span>'
        + '<span class="cv-del" data-cvd="' + i + '" title="Remove rung">✕</span></div>';
    }).join('') || '<div style="font-size:12.5px;color:#e8cf9a;margin-bottom:8px">No rungs — the ladder is inert until you add one.</div>';
    $('#tc-rung-add').style.display = rungs.length >= 10 ? 'none' : '';  // server caps at _MAX_TEASER_RUNGS=10

    // ── activity card ──
    $('#ir-act-n').textContent = runsOk ? (sentRuns.length + (sentRuns.length === 1 ? ' sent' : ' sent')) : '—';
    var strip = $('#ir-act-strip'), body = $('#ir-act-body');
    if (!runsOk) {
      strip.innerHTML = '<span class="fx-st err"><i></i>Run log unreadable</span>';
      $('#ir-act-sub').textContent = 'The automation run log could not be read for this creator.';
      body.innerHTML = '<div class="pb-row first"><div class="pb-txt" style="color:var(--muted)">'
        + 'GET /admin/stats/automation-runs failed — this page cannot say what the pic-back lane did.</div></div>';
    } else {
      $('#ir-act-sub').textContent = 'Every inbound-photo fire from the automation run log'
        + (runWindowDays ? ' — the log goes back ' + runWindowDays + ' days.' : '.');
      strip.innerHTML =
        '<span class="fx-st' + (sentRuns.length ? ' ok' : '') + '"><i></i>' + sentRuns.length + ' pic-backs sent</span>'
        + '<span class="fx-st' + (throttled ? ' warn' : '') + '"><i></i>' + throttled + ' throttled (inside the cooldown)</span>'
        + '<span class="fx-st' + (blocked ? ' err' : '') + '"><i></i>' + blocked + ' blocked (fan restricted)</span>'
        + (otherSkip > 0 ? '<span class="fx-st"><i></i>' + otherSkip + ' skipped for other reasons</span>' : '')
        + (lifeImageReply == null ? ''
            : '<span class="fx-st"><i></i>' + lifeImageReply + ' messages all-time (ledger)</span>');
      if (!sentRuns.length) {
        body.innerHTML = '<div class="pb-row first"><div class="pb-txt" style="color:var(--muted);line-height:1.5">'
          + 'No pic-back has actually been delivered in this window'
          + (picRuns.length ? ' — all ' + picRuns.length + ' fires were skipped (see the chips above).' : '.')
          + (m.image_reply_enabled ? '' : ' The play is currently <b>off</b>.') + '</div></div>';
      } else {
        body.innerHTML = sentRuns.slice(0, 10).map(function (x, idx) {
          var s = x.s;
          var nm = fanNames[s.fan_id] || ('fan #' + s.fan_id);
          var ini = initials(nm);
          var n = s.images_sent || (s.media_ids || []).length || 0;
          return '<div class="pb-row' + (idx === 0 ? ' first' : '') + '">'
            + '<div class="pb-av" style="background:' + AVBG[idx % 4] + '">' + esc(ini) + '</div>'
            + '<div class="pb-txt"><b>' + esc(nm) + '</b> got <b>' + esc(n) + '</b> '
            + (n === 1 ? 'pic' : 'pics') + ' back</div>'
            + '<div class="pb-out">' + esc(s.tier ? s.tier + ' tier' : 'no tier logged')
            + (s.basis_cents ? ' · ' + Fastt.fmtCents(s.basis_cents) + ' basis' : '') + '</div>'
            + '<div class="pb-time">' + esc(Fastt.fmtAgo(x.run.started_at)) + '</div>'
            + '</div>';
        }).join('');
      }
    }
  }

  function bindInt(id, key, lo) {
    $('#' + id).addEventListener('change', function () {
      var n = parseInt(this.value, 10);
      if (isNaN(n) || n < lo) { render(); return; }
      stored[key] = n; saveDeb();
    });
  }
  bindInt('ir-count', 'image_reply_count', 1);
  bindInt('ir-cool', 'image_reply_cooldown_hours', 0);
  bindInt('ht-count', 'hot_teaser_count', 1);
  bindInt('ht-free-max', 'hot_teaser_free_max', 0);
  bindInt('ht-cool', 'hot_teaser_cooldown_hours', 0);
  bindInt('tc-after', 'teaser_convo_after_fan_msgs', 1);  // server clamps 1..1000
  bindInt('tc-count', 'teaser_convo_count', 1);           // server clamps 1..50
  $('#ir-cap').addEventListener('change', function () { stored.image_reply_caption = this.value; saveDeb(); });
  $('#ht-price').addEventListener('change', function () {
    var d = parseFloat(this.value);
    if (isNaN(d) || d < 0) { render(); return; }
    stored.hot_teaser_price_cents = Math.round(d * 100); saveDeb();
  });
  $('#ht-free-folder').addEventListener('change', function () { stored.hot_teaser_free_folder = this.value; saveDeb(); });
  $('#ht-paid-folder').addEventListener('change', function () { stored.hot_teaser_paid_folder = this.value; saveDeb(); });
  $('#ir-tier').addEventListener('change', function () {
    var n = parseInt(this.value, 10);
    if (isNaN(n)) return;
    stored.image_reply_basis_cents = n; saveDeb();
  });

  // ── conversation-ladder rung editing ──
  function rungCopy() {
    return (M().teaser_convo_rungs || []).map(function (r) {
      return { folder: r.folder || '', price_cents: r.price_cents || 0 };
    });
  }
  $('#tc-rungs').addEventListener('change', function (e) {
    var f = e.target.closest('select[data-cvf]');
    if (f) {
      var rs = rungCopy(), i = parseInt(f.dataset.cvf, 10);
      if (!rs[i]) return;
      rs[i].folder = f.value; stored.teaser_convo_rungs = rs; save(); return;
    }
    var pin = e.target.closest('input[data-cvp]');
    if (pin) {
      var rs2 = rungCopy(), j = parseInt(pin.dataset.cvp, 10);
      if (!rs2[j]) return;
      var d = parseFloat(pin.value);
      if (isNaN(d) || d < 0) { render(); return; }
      rs2[j].price_cents = Math.min(Math.round(d * 100), 100000);  // server clamps 0..100000
      stored.teaser_convo_rungs = rs2; save();
    }
  });

  document.addEventListener('click', function (e) {
    var sw = e.target.closest('.fx-switch[data-k]');
    if (sw) { stored[sw.dataset.k] = sw.classList.contains('on'); save(); return; }
    var ck = e.target.closest('#ir-closer');
    if (ck) { stored.image_closer_enabled = ck.classList.contains('on'); save(); return; }
    var addR = e.target.closest('#tc-rung-add');
    if (addR) {
      var rs = rungCopy();
      if (rs.length >= 10) { Fastt.toast('10 rungs is the server maximum', 'err'); return; }
      var last = rs.length ? rs[rs.length - 1].price_cents : 0;
      rs.push({ folder: '', price_cents: last ? Math.min(last * 2, 100000) : 1000 });
      stored.teaser_convo_rungs = rs; save(); return;
    }
    var delR = e.target.closest('.cv-del[data-cvd]');
    if (delR) {
      var rs3 = rungCopy(), di = parseInt(delR.dataset.cvd, 10);
      if (!rs3[di]) return;
      if (!confirm('Remove rung ' + (di + 1) + ' from the ladder?')) return;
      rs3.splice(di, 1); stored.teaser_convo_rungs = rs3; save(); return;
    }
  });

  Fastt.liveBadge($('#ir-card .fx-tc-title'));
  Fastt.liveBadge($('#ht-card .fx-tc-title'));
  Fastt.liveBadge($('#tc-card .fx-tc-title'));
  Fastt.liveBadge($('#ir-activity .fx-card-h'));
  Fastt.staticBadge($('#ir-bundle > h4'), 'STATIC DEMO — not saveable yet'); // no-op: badge is in the markup
  // teaser_convo_adaptive is in the automation defaults but NOT in
  // tip_reward_config_api._validate — a PUT would silently drop it, so show it
  // read-only rather than pretending it saves.
  Fastt.staticBadge($('#tc-adaptive').closest('.fx-field').querySelector('label'), 'READ-ONLY — no save key');
  $('#ir-bundle').querySelectorAll('input').forEach(function (i) { i.disabled = true; i.style.opacity = .55; });
  $('#ir-bundle').querySelectorAll('.fx-switch').forEach(function (s) { s.style.pointerEvents = 'none'; s.style.opacity = .55; });

  render();
});
