Fastt.ready(async function () {
  var $ = Fastt.$, esc = Fastt.esc;
  // No creator picked (unauthed /admin/accounts is empty): fastt.js shows the
  // global placeholder banner and the baked mock-up keeps its in-markup
  // "SAMPLE …" ft-static badges, so nothing fabricated reads as real.
  if (!Fastt.account()) return;

  // A real creator IS in scope: tear the mock-up down synchronously, BEFORE the
  // first await, so fabricated counters/rows never sit next to live data during
  // the (multi-second) config + vault + tips fetches.
  ['tr-sample-badge', 'tr-tier-sample', 'tr-week-sample'].forEach(function (id) {
    var b = $('#' + id); if (b) b.remove();
  });
  ['tr-st-run', 'tr-st-count', 'tr-st-size', 'tr-st-big', 'tr-st-last', 'tr-st-fired'].forEach(function (id) {
    var el = $('#' + id); if (el) { el.innerHTML = '<i></i>Loading…'; el.classList.remove('ok'); }
  });
  $('#tr-tiers').innerHTML = '<div class="tier-fmeta">Loading tiers…</div>';
  $('#tr-week-title').textContent = 'Tips this week';
  $('#tr-week-n').textContent = '…';
  $('#tr-week').querySelectorAll('.rw-row').forEach(function (r) { r.remove(); });
  $('#tr-week').insertAdjacentHTML('beforeend',
    '<div class="rw-row first"><div class="rw-txt" style="color:var(--muted)">Loading recent tips…</div></div>');
  $('#tr-fires-body').innerHTML =
    '<div class="ev-row first"><div class="ev-txt" style="color:var(--muted)">Reading the automation run log…</div></div>';

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

  // ── vault folders: names AND real per-folder counts + a real first thumbnail ──
  // /api/of/v2/vault/lists returns photosCount / videosCount / gifsCount /
  // audiosCount and up to 3 signed 300x300 previews per list (medias[].url).
  var vaultFolders = [], vaultByName = {};
  try {
    var vl = await Fastt.get('/api/of/v2/vault/lists', { limit: 50 }); // route caps at le=50 — 100 422s
    vaultFolders = (vl.list || []).filter(function (x) { return x && x.name; }).map(function (x) {
      var ms = x.medias || [];
      return {
        name: x.name,
        photos: x.photosCount || 0, videos: x.videosCount || 0,
        gifs: x.gifsCount || 0, audios: x.audiosCount || 0,
        thumb: (ms[0] && ms[0].url) || ''
      };
    });
    vaultFolders.forEach(function (f) { vaultByName[f.name] = f; });
  } catch (e) { /* picker degrades to a typed prompt; meta lines say "not in this vault" */ }
  var vaultNames = vaultFolders.map(function (f) { return f.name; });

  function folderTotal(f) { return f ? (f.photos + f.videos + f.gifs + f.audios) : -1; }
  function folderMeta(f) {
    if (!f) return 'not in this vault — nothing will send';
    var bits = [];
    if (f.photos) bits.push(f.photos + (f.photos === 1 ? ' photo' : ' photos'));
    if (f.videos) bits.push(f.videos + (f.videos === 1 ? ' video' : ' videos'));
    if (f.gifs) bits.push(f.gifs + (f.gifs === 1 ? ' gif' : ' gifs'));
    if (f.audios) bits.push(f.audios + (f.audios === 1 ? ' audio' : ' audios'));
    return bits.length ? bits.join(' · ') : 'empty — nothing will send';
  }
  var GRADS = ['g-teasers', 'g-beach', 'g-vip'];
  function folderChip(name, idx) {
    var f = vaultByName[name];
    var empty = folderTotal(f) <= 0;
    var art = f && f.thumb
      ? '<img class="fthumb" src="' + esc(f.thumb) + '" alt="" loading="lazy">'
      : '<span class="ftile ' + GRADS[idx % 3] + '"></span>';
    return '<span class="fchip' + (empty ? ' empty' : '') + '">' + art
      + '<span style="min-width:0"><span class="fnm">' + esc(name) + '</span>'
      + '<span class="fmt">' + esc(folderMeta(f)) + '</span></span></span>';
  }

  // ── recent tips (live feed + strip stats) ──
  var tips = [];
  try {
    var to = new Date(Date.now() + 86400e3).toISOString().slice(0, 10); // inclusive-midnight bound → +1d keeps today
    var from = new Date(Date.now() - 7 * 86400e3).toISOString().slice(0, 10);
    tips = (await Fastt.get('/admin/tips-list', { from: from, to: to, limit: 50 })).rows || [];
  } catch (e) { /* feed shows empty state */ }

  // ── did the reward lane ever actually fire? (run log + lifetime ledger) ──
  // tip_reward runs carry stats_json.image_reply=true when the fire came from the
  // INBOUND-PHOTO lane (that one belongs to Image Reply & Teasers). Filter it out
  // so this page never claims the other lane's numbers.
  var rewardRuns = [], picRuns = [], runWindowDays = 0, runsOk = false;
  var lifeReward = null, lifeImageReply = null;
  try {
    var rr = await Fastt.get('/admin/stats/automation-runs', { kind: 'tip_reward', limit: 200 });
    var all = (rr.runs || []).map(function (r) {
      var s = {};
      try { s = JSON.parse(r.stats_json || '{}') || {}; } catch (e2) { s = {}; }
      return { run: r, s: s };
    });
    runsOk = true;
    rewardRuns = all.filter(function (x) { return !x.s.image_reply; });
    picRuns = all.filter(function (x) { return !!x.s.image_reply; });
    if (all.length) {
      var oldest = Fastt.parseUtc(all[all.length - 1].run.started_at);
      if (oldest) runWindowDays = Math.max(1, Math.round((Date.now() - oldest.getTime()) / 86400e3));
    }
  } catch (e) { /* runsOk stays false → the card says the log is unreadable */ }
  try {
    var pa = await Fastt.get('/admin/stats/per-automation', { from: '2020-01-01' });
    (pa.rows || []).forEach(function (r) {
      if (r.automation === 'tip_reward') lifeReward = r.messages_sent || 0;
      if (r.automation === 'image_reply') lifeImageReply = r.messages_sent || 0;
    });
  } catch (e) { /* lifetime line is simply omitted */ }

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
  // fan names for the run log rows (run stats only carry fan_id)
  var fanNames = {};
  var wantIds = {};
  rewardRuns.slice(0, 10).forEach(function (x) { if (x.s.fan_id) wantIds[x.s.fan_id] = 1; });
  var idList = Object.keys(wantIds);
  if (idList.length) {
    try {
      var fo = await Fastt.get('/admin/fans/' + Fastt.account() + '/by-ids', { ids: idList.join(',') });
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
  function setCheck(el, on) { if (el) el.classList.toggle('on', on); }
  function chip(id, text, ok) {
    var el = $('#' + id); if (!el) return;
    el.innerHTML = '<i></i>' + text;
    if (ok !== undefined) el.classList.toggle('ok', !!ok);
  }
  var AVBG = ['linear-gradient(135deg,#7d97f8,#4166f6)', 'linear-gradient(135deg,#67d1ae,#2f8f6c)',
              'linear-gradient(135deg,#a78bfa,#6d4bd6)', 'linear-gradient(135deg,#e5735b,#b4432c)'];
  var BADGE = ['basic', 'mid', 'prem'];

  function render() {
    var m = M();
    setSwitch($('#tr-card .fx-switch[data-k]'), !!m.enabled);
    setCheck($('#tr-always'), !!m.always_reward);
    setCheck($('#tr-ctx-on'), m.context_pick_enabled !== false);
    chip('tr-st-run', m.enabled ? 'Running' : 'Off — nothing sends', !!m.enabled);
    chip('tr-st-count', tips.length + (tips.length === 1 ? ' tip' : ' tips') + ' this week');
    chip('tr-st-size', '$' + m.dollars_per_image + '/pic · ' + m.min_images + '–' + m.max_images + ' pics');
    var biggest = 0, i;
    for (i = 0; i < tips.length; i++) biggest = Math.max(biggest, tips[i].amount_cents || 0);
    chip('tr-st-big', biggest ? 'Biggest tip this week: ' + Fastt.fmtCents(biggest) : 'No tips this week');
    chip('tr-st-last', tips.length ? 'Last tip ' + Fastt.fmtAgo(tips[0].occurred_at) : 'No recent tips');
    chip('tr-st-fired', !runsOk
      ? 'Run log unreadable'
      : (rewardRuns.length
          ? rewardRuns.length + ' rewards fired (' + runWindowDays + ' d)'
          : '0 rewards fired (' + (runWindowDays || 30) + ' d)'),
      runsOk && rewardRuns.length > 0);
    $('#tr-desc').innerHTML = 'Fan tips → send unseen vault pics automatically. <b style="color:#ddd">$'
      + esc(m.dollars_per_image) + ' per image</b>, ' + esc(m.min_images) + '–' + esc(m.max_images)
      + ' images per tip, tips counted over ' + esc(m.window_hours) + ' h. He never gets a pic he’s already seen.';
    $('#tr-tiers-note').textContent = '— everything he tipped in the last ' + m.window_hours + ' h counts';

    // knobs
    $('#tr-dpi').value = m.dollars_per_image;
    $('#tr-window').value = m.window_hours;
    $('#tr-min').value = m.min_images;
    $('#tr-max').value = m.max_images;
    $('#tr-caption').value = m.caption || '';
    $('#tr-ctx-max').value = m.context_pick_max == null ? 3 : m.context_pick_max;
    $('#tr-ctx-msgs').value = m.context_pick_messages == null ? 20 : m.context_pick_messages;

    // tiers — name / $ threshold / folders / remove, all persisted through PUT tiers[]
    var tiers = m.tiers || [];
    var host = $('#tr-tiers');
    host.innerHTML = tiers.map(function (t, idx) {
      var minDollars = Math.round((t.min_basis_cents || 0) / 100);
      var folders = t.folders || [];
      var chips = folders.length
        ? folders.map(function (n, k) { return folderChip(n, idx + k); }).join('')
        : '<span style="font-size:12.5px;color:#e8cf9a">No folder set — nothing sends from this tier</span>';
      return '<div class="tier-row">'
        + '<span class="tier-badge ' + BADGE[Math.min(idx, 2)] + '" style="width:auto;flex:0 0 auto;padding:0 9px">'
        + esc(t.name || ('tier ' + (idx + 1))) + '</span>'
        + '<div class="tier-cond" style="width:auto;flex:0 0 auto"><div class="cndrow">'
        + '<input class="fx-input tier-name" data-tname="' + idx + '" value="' + esc(t.name || '') + '" placeholder="tier name" title="Tier name">'
        + '<span style="font-size:12px;color:var(--muted2)">at</span>'
        + '<div class="fx-unit" data-unit="$"><input class="fx-input tier-min" data-tmin="' + idx + '" value="' + esc(minDollars) + '" title="Dollar threshold"></div>'
        + '<span style="font-size:12px;color:var(--muted2)">+</span></div></div>'
        + '<span class="tier-arrow"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9"><path d="M5 12h14M13 6l6 6-6 6" stroke-linecap="round" stroke-linejoin="round"/></svg></span>'
        + '<div class="tier-folder">' + chips + '</div>'
        + '<span class="tier-swap" data-tier="' + idx + '">Change folders</span>'
        + '<span class="tier-del" data-tdel="' + idx + '" title="Remove this tier">✕</span>'
        + '</div>';
    }).join('') || '<div class="tier-fmeta">No tiers configured — add one below, or every tipper falls through with nothing to send.</div>';
    $('#tr-tier-add').style.display = tiers.length >= 10 ? 'none' : '';  // server caps at _MAX_TIERS=10

    // ON but nothing to give: matches the real app's "no folders → nothing sends"
    // guard. A tier with an empty folders[] is dropped on save, so it delivers
    // nothing — warn while the switch is on.
    var noFolders = !tiers.length || tiers.every(function (t) { return !((t.folders || []).length); });
    $('#tr-nofolders').style.display = (m.enabled && noFolders) ? 'flex' : 'none';

    // ── reward deliveries (run log) ──
    var body = $('#tr-fires-body');
    $('#tr-fires-n').textContent = runsOk ? (rewardRuns.length + (rewardRuns.length === 1 ? ' fire' : ' fires')) : '—';
    if (!runsOk) {
      $('#tr-fires-sub').textContent = 'The automation run log could not be read for this creator.';
      body.innerHTML = '<div class="ev-row first"><div class="ev-txt" style="color:var(--muted)">'
        + 'No run log available — GET /admin/stats/automation-runs failed, so this page cannot say whether a reward ever fired.</div></div>';
    } else if (!rewardRuns.length) {
      $('#tr-fires-sub').textContent = 'Straight off the automation run log — tip-triggered fires only.';
      var why = (m.enabled ? 'Reward delivery is <b>ON</b>' : 'Reward delivery is <b>OFF</b>')
        + ' — <b>0</b> tip-triggered rewards in the '
        + (runWindowDays ? 'last ' + runWindowDays + ' days' : 'retained window') + ' of the run log.';
      var side = picRuns.length
        ? ' The ' + picRuns.length + ' tip_reward runs in that window are all <b>inbound-photo pic-backs</b> — those belong to '
          + '<a href="tips-image-reply.html" style="color:#7d97f8">Image Reply &amp; Teasers</a>, not this page.'
        : '';
      var life = (lifeReward == null) ? ''
        : ' All-time the reward lane has sent <b>' + lifeReward + '</b> message'
          + (lifeReward === 1 ? '' : 's') + ' (per-automation ledger)'
          + (lifeImageReply == null ? '' : ', vs <b>' + lifeImageReply + '</b> for the image-reply lane') + '.';
      body.innerHTML = '<div class="ev-row first"><div class="ev-txt" style="color:var(--muted);line-height:1.5">'
        + why + side + life + '</div></div>';
    } else {
      $('#tr-fires-sub').textContent = 'Tip-triggered fires only — inbound-photo pic-backs are excluded (they live on Image Reply & Teasers).';
      body.innerHTML = rewardRuns.slice(0, 8).map(function (x, idx) {
        var s = x.s;
        var nm = fanNames[s.fan_id] || ('fan #' + s.fan_id);
        var ini = initials(nm);
        var okFire = s.status === 'ok';
        var what = okFire
          ? '<b>' + esc(s.images_sent || 0) + '</b> ' + ((s.images_sent === 1) ? 'pic' : 'pics') + ' sent'
          : 'skipped — ' + esc(s.reason || 'no reason logged');
        var basis = s.basis_cents ? ' · basis ' + Fastt.fmtCents(s.basis_cents) : '';
        return '<div class="ev-row' + (idx === 0 ? ' first' : '') + '">'
          + '<div class="rw-av" style="background:' + AVBG[idx % 4] + '">' + esc(ini) + '</div>'
          + '<div class="ev-txt"><b>' + esc(nm) + '</b> — ' + what
          + '<span style="color:var(--muted2);font-size:12px">' + esc(basis) + '</span></div>'
          + '<div class="ev-out">' + esc(s.tier ? s.tier + ' tier' : (okFire ? 'no tier logged' : 'no send')) + '</div>'
          + '<div class="ev-time">' + esc(Fastt.fmtAgo(x.run.started_at)) + '</div>'
          + '</div>';
      }).join('');
    }

    // this-week feed
    var week = $('#tr-week');
    week.querySelectorAll('.rw-row').forEach(function (r) { r.remove(); });
    $('#tr-week-title').textContent = 'Tips this week';
    $('#tr-week-n').textContent = tips.length + (tips.length === 1 ? ' tip' : ' tips');
    var frag = '';
    if (!tips.length) {
      frag = '<div class="rw-row first"><div class="rw-txt" style="color:var(--muted)">No tips in the last 7 days — the moment one lands, the reward fires' + (m.enabled ? '' : ' (once this page is switched on)') + '.</div></div>';
    } else {
      frag = tips.slice(0, 8).map(function (t, idx) {
        var fan = t.fan || {}; // tips-list nests names under row.fan (service/transactions.py)
        var name = fan.display_name || fan.username || ('fan #' + t.fan_id);
        var initials = String(name).split(/\s+/).map(function (w) { return w[0] || ''; }).join('').slice(0, 2).toUpperCase();
        return '<div class="rw-row' + (idx === 0 ? ' first' : '') + '">'
          + '<div class="rw-av" style="background:' + AVBG[idx % 4] + '">' + esc(initials) + '</div>'
          + '<div class="rw-txt"><b>' + esc(name) + '</b> tipped <span class="amt">' + esc(Fastt.fmtCents(t.amount_cents)) + '</span></div>'
          + '<div class="rw-out">' + esc(fan.username ? '@' + fan.username : 'fan #' + t.fan_id) + '</div>'
          + '<div class="rw-time">' + esc(Fastt.fmtAgo(t.occurred_at)) + '</div>'
          + '</div>';
      }).join('');
    }
    week.insertAdjacentHTML('beforeend', frag);
  }

  // number/text inputs → stored key
  function bindInt(id, key, lo) {
    $('#' + id).addEventListener('change', function () {
      var n = parseInt(this.value, 10);
      if (isNaN(n) || n < lo) { render(); return; }
      stored[key] = n; saveDeb();
    });
  }
  bindInt('tr-dpi', 'dollars_per_image', 1);
  bindInt('tr-window', 'window_hours', 1);
  // min/max images are coupled — the server 422s when max < min. Instead of
  // surfacing that raw error, keep the pair valid client-side: raising the floor
  // above the cap pushes the cap up with it, and vice-versa.
  $('#tr-min').addEventListener('change', function () {
    var n = parseInt(this.value, 10);
    if (isNaN(n) || n < 0) { render(); return; }
    stored.min_images = n;
    var mx = M().max_images;
    if (mx != null && n > mx) stored.max_images = n;
    saveDeb();
  });
  $('#tr-max').addEventListener('change', function () {
    var n = parseInt(this.value, 10);
    if (isNaN(n) || n < 1) { render(); return; }
    stored.max_images = n;
    var mn = M().min_images;
    if (mn != null && n < mn) stored.min_images = n;
    saveDeb();
  });
  bindInt('tr-ctx-max', 'context_pick_max', 0);      // server clamps 0..10
  bindInt('tr-ctx-msgs', 'context_pick_messages', 1); // server clamps 1..100
  $('#tr-caption').addEventListener('change', function () {
    stored.caption = this.value; saveDeb();
  });

  // ── tier list editing (name / threshold / folders / add / remove) ──
  function tierCopy() {
    return (M().tiers || []).map(function (t) {
      return { name: t.name || '', min_basis_cents: t.min_basis_cents || 0, folders: (t.folders || []).slice() };
    });
  }
  function commitTiers(tiers) {
    tiers.sort(function (a, b) { return (a.min_basis_cents || 0) - (b.min_basis_cents || 0); });
    stored.tiers = tiers; save();
  }
  // change (not input) so a half-typed name never round-trips through the server
  $('#tr-tiers').addEventListener('change', function (e) {
    var nameEl = e.target.closest('input[data-tname]');
    if (nameEl) {
      var tiers = tierCopy(), i = parseInt(nameEl.dataset.tname, 10);
      if (!tiers[i]) return;
      tiers[i].name = String(nameEl.value || '').trim().slice(0, 40); // server caps at 40
      commitTiers(tiers); return;
    }
    var minEl = e.target.closest('input[data-tmin]');
    if (minEl) {
      var t2 = tierCopy(), j = parseInt(minEl.dataset.tmin, 10);
      if (!t2[j]) return;
      var d = parseFloat(minEl.value);
      if (isNaN(d) || d < 0) { render(); return; }
      t2[j].min_basis_cents = Math.round(d * 100);
      commitTiers(t2); return;
    }
  });

  // ── folder picker (real vault list, counts + thumbnails, multi-select) ──
  function openFolderPicker(title, sub, selected, multi, onDone) {
    if (!vaultFolders.length) {   // OF mirror unreachable → keep it usable, typed
      var next = prompt(title + '\n(comma-separated folder names; the vault list could not be read)',
                        selected.join(', '));
      if (next === null) return;
      onDone(next.split(',').map(function (s) { return s.trim(); }).filter(Boolean));
      return;
    }
    var picked = selected.slice();
    var back = document.createElement('div');
    back.className = 'fp-back';
    back.innerHTML = '<div class="fp-panel"><div class="fp-head">' + esc(title)
      + '<span class="sub">' + esc(sub) + '</span></div><div class="fp-list"></div>'
      + '<div class="fp-foot"><button class="fx-btn ghost" data-fp="cancel">Cancel</button>'
      + '<button class="fx-btn" data-fp="ok">Use these folders</button></div></div>';
    var list = back.querySelector('.fp-list');
    list.innerHTML = vaultFolders.map(function (f, i) {
      var empty = folderTotal(f) <= 0;
      var art = f.thumb ? '<img src="' + esc(f.thumb) + '" alt="" loading="lazy">'
                        : '<span class="ftile ' + GRADS[i % 3] + '"></span>';
      return '<div class="fp-row' + (picked.indexOf(f.name) >= 0 ? ' on' : '') + '" data-fn="' + esc(f.name) + '">'
        + art + '<div style="min-width:0"><div class="nm">' + esc(f.name) + '</div>'
        + '<div class="mt' + (empty ? ' warnc' : '') + '">' + esc(folderMeta(f)) + '</div></div>'
        + '<span class="tick">✓</span></div>';
    }).join('');
    function close() { back.remove(); }
    back.addEventListener('click', function (e) {
      if (e.target === back) { close(); return; }
      var act = e.target.closest('[data-fp]');
      if (act) {
        if (act.dataset.fp === 'ok') { close(); onDone(picked); } else close();
        return;
      }
      var row = e.target.closest('.fp-row');
      if (!row) return;
      var nm = row.dataset.fn, at = picked.indexOf(nm);
      if (!multi) { picked = [nm]; list.querySelectorAll('.fp-row').forEach(function (r) { r.classList.remove('on'); }); row.classList.add('on'); return; }
      if (at >= 0) { picked.splice(at, 1); row.classList.remove('on'); }
      else { picked.push(nm); row.classList.add('on'); }
    });
    document.body.appendChild(back);
  }

  // switch / check / tier-swap clicks (kit toggles classes first — we read the result)
  document.addEventListener('click', function (e) {
    var sw = e.target.closest('#tr-card .fx-switch[data-k]');
    if (sw) { stored.enabled = sw.classList.contains('on'); save(); return; }
    var ck = e.target.closest('#tr-always');
    if (ck) { stored.always_reward = ck.classList.contains('on'); save(); return; }
    var ctx = e.target.closest('#tr-ctx-on');
    if (ctx) { stored.context_pick_enabled = ctx.classList.contains('on'); save(); return; }
    var add = e.target.closest('#tr-tier-add');
    if (add) {
      var t3 = tierCopy();
      if (t3.length >= 10) { Fastt.toast('10 tiers is the server maximum', 'err'); return; }
      var top = t3.length ? Math.max.apply(null, t3.map(function (t) { return t.min_basis_cents || 0; })) : 0;
      t3.push({ name: 'tier ' + (t3.length + 1), min_basis_cents: top ? top * 2 : 1000, folders: [] });
      commitTiers(t3); return;
    }
    var del = e.target.closest('.tier-del[data-tdel]');
    if (del) {
      var t4 = tierCopy(), di = parseInt(del.dataset.tdel, 10);
      if (!t4[di]) return;
      if (!confirm('Remove the "' + (t4[di].name || di) + '" tier? Tippers in that band fall to the tier below.')) return;
      t4.splice(di, 1); commitTiers(t4); return;
    }
    var swap = e.target.closest('.tier-swap[data-tier]');
    if (swap) {
      var idx = parseInt(swap.dataset.tier, 10);
      var t5 = tierCopy();
      if (!t5[idx]) return;
      openFolderPicker('Folders for the "' + (t5[idx].name || idx) + '" tier',
        'Only unseen items from these folders are sent. An empty folder sends nothing.',
        t5[idx].folders, true, function (picked) {
          t5[idx].folders = picked.slice(0, 25);   // server caps at _MAX_FOLDERS_PER_TIER=25
          commitTiers(t5);
        });
    }
  });

  Fastt.liveBadge($('#tr-card .fx-tc-title'));
  Fastt.liveBadge($('#tr-week .fx-card-h'));
  Fastt.liveBadge($('#tr-fires .fx-card-h'));
  Fastt.staticBadge($('#tr-bundle > h4'), 'STATIC DEMO — not saveable yet'); // no-op: badge is in the markup
  // bundle-scaling controls have no persisting backend key (service/
  // tip_reward_config_api.py _validate drops bundle_*) — leave them inert
  $('#tr-bundle').querySelectorAll('input').forEach(function (i) { i.disabled = true; i.style.opacity = .55; });
  $('#tr-bundle').querySelectorAll('.fx-check').forEach(function (c) { c.style.pointerEvents = 'none'; c.style.opacity = .55; });

  render();
});
