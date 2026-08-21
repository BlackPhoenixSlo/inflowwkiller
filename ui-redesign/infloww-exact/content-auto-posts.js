// ==== live wiring: auto_posts automation rule (see service/automations/auto_posts.py) ====
// Every value on this page comes from the rule row or from OF:
//   pace       → payload.resend_after_hours / resend_count + trigger.every_seconds
//   queue      → payload.posts[] (texts / media_folder_id / media_count / price /
//                hours_to_live / delay_minutes / preview_media_*)
//   thumbnails → /api/of/v2/vault/lists[].medias[0].url, /vault/media?list_id=…,
//                /vault/media/{id}   (list_id — `list` is silently ignored by the route)
//   runs       → /admin/stats/automation-runs?kind=auto_posts
Fastt.ready(async function () {
  var $ = Fastt.$, esc = Fastt.esc;
  var CLOCK_SVG = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9"><circle cx="12" cy="12" r="9"/><path d="M12 7.5V12l3 2" stroke-linecap="round"/></svg>';
  var TRASH_SVG = '<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9"><path d="M4 7h16M10 4h4M7 7l1 13h8l1-13" stroke-linecap="round" stroke-linejoin="round"/></svg>';
  var KEEP_SVG = '<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9"><path d="M6 4h9a2 2 0 0 1 2 2v14l-6.5-4L4 20V6a2 2 0 0 1 2-2z" stroke-linejoin="round"/></svg>';

  var card = $('#card-autoposts'), sw = $('#sw-enabled');
  var rule = null, folders = [], folderNames = {};
  var mediaThumbs = {};   // vault media id → thumb url (memoised)
  var folderThumbs = {};  // vault folder id → thumb url (memoised)
  var runsCache = [];

  // folder id → name / cover thumb. vault/lists already inlines up to 5
  // 300x300 `medias[].url` per folder, so the queue covers cost no extra call.
  try {
    var vl = await Fastt.get('/api/of/v2/vault/lists', { limit: 50 });
    folders = vl.list || [];
    folders.forEach(function (f) {
      folderNames[f.id] = f.name;
      var m = (f.medias || [])[0];
      if (m && m.url) folderThumbs[f.id] = m.url;
    });
  } catch (e) { /* names fall back to raw ids, covers to the flat panel colour */ }

  rule = await Fastt.rule('auto_posts');

  function payloadOf() { return (rule && rule.payload) || {}; }
  function postsOf() { var p = payloadOf().posts; return Array.isArray(p) ? p : []; }
  function folderById(id) {
    for (var i = 0; i < folders.length; i++) if (String(folders[i].id) === String(id)) return folders[i];
    return null;
  }

  async function saveRule(body) {
    try {
      if (rule) {
        rule = await Fastt.patch('/admin/automation-rules/' + rule.id, body);
      } else {
        rule = await Fastt.post('/admin/automation-rules', Object.assign({
          account_id: Fastt.account(), kind: 'auto_posts', name: 'auto_posts',
          is_enabled: false, trigger: { every_seconds: 2592000 }, payload: { posts: [] }
        }, body));
      }
      Fastt.saved();
    } catch (e) { Fastt.oops(e); }
    renderAll();
  }
  function savePayload(next) { return saveRule({ payload: next }); }
  function savePosts(ps) { return savePayload(Object.assign({}, payloadOf(), { posts: ps })); }
  function postsCopy() { return postsOf().map(function (p) { return Object.assign({}, p); }); }

  function fmtWhen(s) {
    var d = Fastt.parseUtc(s);
    return d ? d.toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : '—';
  }
  function fmtHours(h) {
    h = Number(h) || 0;
    if (h <= 0) return '—';
    if (h >= 24 && h % 24 === 0) return (h / 24) + ' day' + (h === 24 ? '' : 's');
    return (h % 1 ? h.toFixed(1) : h) + ' h';
  }
  // The rule's own cadence is 2592000s — "every 720 h" is true and unreadable.
  function fmtEvery(sec) {
    sec = Number(sec) || 0;
    if (sec <= 0) return '—';
    if (sec >= 86400) { var d = sec / 86400; return (d % 1 ? d.toFixed(1) : d) + ' day' + (d === 1 ? '' : 's'); }
    if (sec >= 3600) { var h = sec / 3600; return (h % 1 ? h.toFixed(1) : h) + ' h'; }
    return Math.max(1, Math.round(sec / 60)) + ' min';
  }

  // ── status strip ───────────────────────────────────────────
  function renderStatus() {
    var on = !!(rule && rule.is_enabled), ps = postsOf();
    var pl = payloadOf();
    var st = $('#st-run');
    st.classList.toggle('ok', on);
    st.innerHTML = '<i></i>' + (on ? 'Running' : (rule ? 'Off' : 'Not set up'));
    $('#st-queue').innerHTML = '<i></i>' + (rule
      ? ps.length + ' post' + (ps.length === 1 ? '' : 's') + ' in the queue'
      : 'no auto_posts rule yet');
    var nx = 'no auto_posts rule yet';
    var rep = (Number(pl.resend_after_hours) > 0 && Number(pl.resend_count) > 0)
      ? ('every ' + fmtHours(pl.resend_after_hours)) : null;
    if (rule && rule.has_pending_job) nx = 'queue job pending';
    else if (rule && rule.next_due_at) nx = 'next cycle ' + fmtWhen(rule.next_due_at);
    else if (on) nx = rep ? ('repeats ' + rep) : ('fresh pass every ' + fmtEvery(rule.every_seconds));
    else if (rule) nx = 'paused' + (rep ? ' · would repeat ' + rep : ' · nothing scheduled');
    $('#st-next').innerHTML = '<i></i>' + nx;
    var lr = rule && rule.last_run;
    $('#st-last').innerHTML = '<i></i>' + (lr && lr.started_at
      ? 'last run ' + esc(lr.status || '?') + ' · ' + Fastt.fmtAgo(lr.started_at)
      : 'never run');
    card.classList.toggle('on', on);
    sw.classList.toggle('on', on);
    var state = card.querySelector('.fx-tc-state');
    if (state) state.textContent = on ? 'Running' : 'Off';
    // The queue below stays open and editable while paused — say why nothing fires.
    $('#pausebar-txt').innerHTML = rule
      ? '<b>Paused.</b> Nothing posts to the feed until you flip the switch above. Everything below is live rule state ' +
        '(rule #' + esc(rule.id) + ') and every edit saves straight into it — editing while paused is safe.'
      : '<b>Not set up.</b> This creator has no auto_posts rule yet. Adding a post or saving any knob below creates one, left disabled.';
  }

  // ── posting pace (real knobs, no invented randomiser) ──────
  function renderPace() {
    var pl = payloadOf(), ps = postsOf();
    var ra = Number(pl.resend_after_hours) || 0, rc = Number(pl.resend_count) || 0;
    $('#pace-rv').textContent = (ra > 0 && rc > 0)
      ? ('every ' + fmtHours(ra) + ' · ' + rc + ' more cycle' + (rc === 1 ? '' : 's'))
      : (rule ? 'one pass, no repeat' : '—');
    $('#pace-hours').value = ra > 0 ? ra : '';
    $('#pace-cycles').value = rc > 0 ? rc : '';

    var trig = (rule && rule.trigger) || {};
    var clock = Array.isArray(trig.daily_at) && trig.daily_at.length > 0;
    var ev = Number(rule && rule.every_seconds) || 0;
    $('#pace-every').value = (!clock && ev > 0) ? (Math.round(ev / 8640) / 10) : '';
    $('#pace-every').disabled = clock;

    var bits = [];
    if (!rule) bits.push('No auto_posts rule for this creator yet — saving any knob creates one, disabled.');
    else if (!ps.length) bits.push('Queue is empty — nothing to pace yet.');
    else if (ps.length === 1) bits.push('One post in the queue, so a pass is a single post — the repeat cycle above IS the cadence.');
    else {
      var gaps = ps.slice(1).map(function (p) { return Number(p.delay_minutes) || 0; });
      bits.push(ps.length + ' posts drip one at a time' +
        (gaps.every(function (g) { return g === 0; })
          ? ', back-to-back (no extra wait set — see the per-post overrides).'
          : ', with ' + gaps.join(' / ') + ' min extra waits between them.'));
    }
    if (clock) bits.push('Rule fires on a clock trigger (' + esc(trig.daily_at.join(', ')) + '), so the days field does not apply.');
    else if (ev > 0) bits.push('The rule itself only re-arms a fresh pass every ' + fmtEvery(ev) + ' — auto_posts is an action kind (catalogued recurring:false), it drips itself.');
    var measured = measuredCadence();
    if (measured) bits.push('Measured on the last ' + measured.n + ' runs: ~' + measured.h + ' h apart.');
    $('#pace-note').innerHTML = bits.join(' ');
  }
  /** Median gap between the recorded run starts — the pace that actually happened. */
  function measuredCadence() {
    var ts = runsCache.map(function (r) { var d = Fastt.parseUtc(r.started_at); return d ? d.getTime() : 0; })
      .filter(Boolean).sort(function (a, b) { return b - a; });
    if (ts.length < 3) return null;
    var gaps = [];
    for (var i = 1; i < ts.length; i++) gaps.push((ts[i - 1] - ts[i]) / 3600e3);
    gaps.sort(function (a, b) { return a - b; });
    var med = gaps[Math.floor(gaps.length / 2)];
    if (!isFinite(med) || med <= 0) return null;
    return { n: ts.length, h: Math.round(med * 10) / 10 };
  }

  // ── queue thumbnails (real vault media, assigned via el.style) ──
  async function folderThumbUrl(fid) {
    if (folderThumbs[fid] !== undefined) return folderThumbs[fid];
    folderThumbs[fid] = '';
    try {
      var out = await Fastt.get('/api/of/v2/vault/media', { list_id: fid, limit: 1 });
      var it = (out.list || [])[0], f = (it && it.files) || {};
      folderThumbs[fid] = (f.squarePreview && f.squarePreview.url) || (f.thumb && f.thumb.url) || '';
    } catch (e) { /* stays '' → flat panel, honest */ }
    return folderThumbs[fid];
  }
  async function mediaThumbUrl(id) {
    if (mediaThumbs[id] !== undefined) return mediaThumbs[id];
    mediaThumbs[id] = '';
    try {
      var m = await Fastt.get('/api/of/v2/vault/media/' + id);
      var f = m.files || {};
      mediaThumbs[id] = (f.squarePreview && f.squarePreview.url) || (f.thumb && f.thumb.url) || '';
    } catch (e) { /* stays '' */ }
    return mediaThumbs[id];
  }
  async function thumbUrlFor(p) {
    if (p.media_folder_id != null) {
      var u = await folderThumbUrl(p.media_folder_id);
      if (u) return u;
    }
    var ids = Array.isArray(p.media_files) ? p.media_files : [];
    if (ids.length) return await mediaThumbUrl(ids[0]);
    return '';
  }
  function paint(el, url) {
    if (!el) return;
    if (url) el.style.backgroundImage = 'url(' + JSON.stringify(url) + ')';
    else el.style.backgroundImage = 'none';
  }

  function poolLabel(p) {
    if (p.media_folder_id != null) {
      var name = folderNames[p.media_folder_id] || ('folder ' + p.media_folder_id);
      var n = Number(p.media_count) || 1;
      return name + ' · ' + n + ' pic' + (n === 1 ? '' : 's');
    }
    var k = Array.isArray(p.media_files) ? p.media_files.length : 0;
    return k ? (k + ' picked pic' + (k === 1 ? '' : 's')) : 'text only';
  }
  function textsOf(p) {
    var t = Array.isArray(p.texts) ? p.texts.filter(function (x) { return typeof x === 'string' && x.trim(); }) : [];
    if (t.length) return t;
    return (typeof p.text === 'string' && p.text.trim()) ? [p.text] : [];
  }
  function capOf(p) {
    var t = textsOf(p);
    if (!t.length) return '(no caption)';
    return t[0] + (t.length > 1 ? ' (+' + (t.length - 1) + ' variants)' : '');
  }

  function renderQueue() {
    var ps = postsOf();
    $('#q-count').textContent = 'Up next · ' + ps.length + ' in the queue';
    var strip = $('#qstrip');
    strip.querySelectorAll('.qcard, .q-empty').forEach(function (el) { el.remove(); });
    var addTile = strip.querySelector('#qadd');
    if (!ps.length) {
      var em = document.createElement('div');
      em.className = 'q-empty';
      em.style.cssText = 'display:flex;align-items:center;color:var(--muted);font-size:12.5px;padding:0 6px;max-width:280px;line-height:1.5';
      em.textContent = rule
        ? 'Queue is empty — “Add a post” writes straight into this rule’s payload.posts.'
        : 'No auto_posts rule yet — adding a post creates one (disabled).';
      strip.insertBefore(em, addTile);
      return;
    }
    ps.forEach(function (p, i) {
      var ttl = (typeof p.hours_to_live === 'number' && p.hours_to_live > 0) ? p.hours_to_live : null;
      var price = Number(p.price) || 0;
      var delay = (typeof p.delay_minutes === 'number' && p.delay_minutes > 0) ? p.delay_minutes : 0;
      var elCard = document.createElement('div');
      elCard.className = 'qcard';
      elCard.innerHTML =
        '<div class="qthumb qedit" data-edit="' + i + '">' +
          '<span class="qtools">' +
            (i > 0 ? '<button class="qtool" data-mv="' + i + '" data-dir="-1" title="Move earlier">&#8249;</button>' : '') +
            (i < ps.length - 1 ? '<button class="qtool" data-mv="' + i + '" data-dir="1" title="Move later">&#8250;</button>' : '') +
            '<button class="qtool" data-del="' + i + '" title="Remove from queue">&times;</button>' +
          '</span>' +
          (price > 0 ? '<span class="ptag">$' + esc(price) + '</span>' : '') +
          '<span class="ftag">' + esc(poolLabel(p)) + '</span></div>' +
        '<div class="qbody"><div class="qcap qedit" data-edit="' + i + '" title="Click to edit this post">' + esc(capOf(p)) + '</div>' +
          '<div class="qmeta"><span class="qchip">' + CLOCK_SVG +
            (i === 0 ? 'first in queue' : (delay ? '+' + delay + ' min' : 'no extra wait')) + '</span>' +
          '<span class="qdel">' + (ttl ? TRASH_SVG + fmtHours(ttl) : KEEP_SVG + 'keeps') + '</span></div></div>';
      strip.insertBefore(elCard, addTile);
      thumbUrlFor(p).then(function (u) { paint(elCard.querySelector('.qthumb'), u); });
    });
  }

  function renderOverrides() {
    var tbl = $('#ovr-table'), ps = postsOf();
    tbl.querySelectorAll('tr').forEach(function (tr, i) { if (i > 0) tr.remove(); });
    var body = tbl.querySelector('tr').parentNode;
    if (!ps.length) {
      var tr0 = document.createElement('tr');
      tr0.innerHTML = '<td class="cap" colspan="5" style="color:var(--muted)">No posts in the queue.</td>';
      body.appendChild(tr0);
      return;
    }
    ps.forEach(function (p, i) {
      var ttl = (typeof p.hours_to_live === 'number' && p.hours_to_live > 0) ? p.hours_to_live : '';
      var tr = document.createElement('tr');
      tr.innerHTML =
        '<td class="cap"><span class="dotk" data-dot="' + i + '"></span><span class="qedit" data-edit="' + i + '" title="Click to edit captions + pool">' + esc(capOf(p)) + '</span></td>' +
        '<td><input class="fx-input" data-i="' + i + '" data-k="delay_minutes" value="' + (Number(p.delay_minutes) || 0) + '"></td>' +
        '<td><input class="fx-input" data-i="' + i + '" data-k="hours_to_live" value="' + ttl + '" placeholder="keep"></td>' +
        '<td><input class="fx-input" data-i="' + i + '" data-k="media_count" value="' + (Number(p.media_count) || 1) + '"></td>' +
        '<td><div class="fx-unit" data-unit="$"><input class="fx-input" data-i="' + i + '" data-k="price" value="' + (Number(p.price) || 0) + '"></div></td>';
      body.appendChild(tr);
      thumbUrlFor(p).then(function (u) { paint(tr.querySelector('.dotk'), u); });
    });
  }

  function folderOptionsHtml(selectedId, noneLabel) {
    var out = '<option value="">' + esc(noneLabel) + '</option>';
    var seen = false;
    folders.forEach(function (f) {
      var on = (selectedId != null && String(f.id) === String(selectedId));
      if (on) seen = true;
      out += '<option value="' + esc(f.id) + '"' + (on ? ' selected' : '') + '>' +
        esc(f.name) + ' — ' + (f.photosCount || 0) + ' photos' +
        ((f.videosCount || 0) ? (' / ' + f.videosCount + ' videos') : '') + '</option>';
    });
    if (selectedId != null && selectedId !== '' && !seen) {
      out += '<option value="' + esc(selectedId) + '" selected>folder ' + esc(selectedId) + ' (not in this vault)</option>';
    }
    return out;
  }

  // ── paid feed posts: every knob here is a real payload key ──
  function renderPaid() {
    var tbl = $('#paid-table'), ps = postsOf();
    tbl.querySelectorAll('tr').forEach(function (tr, i) { if (i > 0) tr.remove(); });
    var body = tbl.querySelector('tr').parentNode;
    if (!ps.length) {
      var tr0 = document.createElement('tr');
      tr0.innerHTML = '<td class="cap" colspan="4" style="color:var(--muted)">No posts in the queue — nothing to price.</td>';
      body.appendChild(tr0);
      return;
    }
    ps.forEach(function (p, i) {
      var paid = (Number(p.price) || 0) > 0;
      var tr = document.createElement('tr');
      tr.innerHTML =
        '<td class="cap"><span class="dotk" data-dot="' + i + '"></span>' + esc(capOf(p)) + '</td>' +
        '<td><div class="fx-unit" data-unit="$"><input class="fx-input" data-i="' + i + '" data-k="price" value="' + (Number(p.price) || 0) + '"></div></td>' +
        '<td><input class="fx-input" data-i="' + i + '" data-k="preview_media_count" value="' +
          (Number(p.preview_media_count) || '') + '" placeholder="' + (paid ? 'none' : 'free post') + '"' + (paid ? '' : ' disabled') + '></td>' +
        '<td><select class="fx-select" style="min-width:220px" data-i="' + i + '" data-k="preview_media_folder_id"' + (paid ? '' : ' disabled') + '>' +
          folderOptionsHtml(p.preview_media_folder_id, paid ? '— same pool as the post —' : '— free post —') + '</select></td>';
      body.appendChild(tr);
      thumbUrlFor(p).then(function (u) { paint(tr.querySelector('.dotk'), u); });
    });
  }

  function renderChecks() {
    var pl = payloadOf(), ps = postsOf();
    var withTtl = ps.filter(function (p) { return typeof p.hours_to_live === 'number' && p.hours_to_live > 0; });
    var allTtl = ps.length > 0 && withTtl.length === ps.length;
    $('#ck-ttl').classList.toggle('on', allTtl);
    $('#ck-ttl-lbl').textContent = allTtl
      ? ('Auto-delete each post after ' + fmtHours(withTtl[0].hours_to_live))
      : (withTtl.length ? ('Auto-delete each post (' + withTtl.length + ' of ' + ps.length + ' set — turn on to apply to all)')
                        : 'Auto-delete each post after 48 h');
    $('#ck-repost').classList.toggle('on', (Number(pl.resend_count) || 0) > 0);
    $('#ck-dry').classList.toggle('on', !!pl.dry_run);
    $('#rp-wait').value = (pl.resend_after_hours != null && pl.resend_after_hours > 0) ? pl.resend_after_hours : '';
    $('#rp-cycles').value = (pl.resend_count != null) ? pl.resend_count : '';
  }

  // ── previous runs ──────────────────────────────────────────
  var parseStats = Fastt.ruleStats;
  function runBits(r) {
    var st = parseStats(r), bits = [];
    if (st) {
      if (st.dry_run) bits.push('dry run · ' + (st.posts || 0) + ' planned');
      if (typeof st.of_post_id === 'number') bits.push('post #' + st.of_post_id);
      var a = Fastt.parseUtc(r.started_at), b = Fastt.parseUtc(st.delete_at);
      if (a && b && b > a) bits.push('auto-deletes after ' + (Math.round((b - a) / 360e3) / 10) + ' h');
      if (typeof st.remaining === 'number' && st.remaining > 0) bits.push(st.remaining + ' left in the pass');
      if (typeof st.resends_left === 'number' && st.resends_left > 0) bits.push(st.resends_left + ' cycle' + (st.resends_left === 1 ? '' : 's') + ' left');
      if (st.status === 'skipped' && st.reason) bits.push('skipped (' + st.reason + ')');
    }
    if (r.error_text) bits.push(String(r.error_text).slice(0, 120));
    return bits.join(' · ');
  }
  function statusCls(s) {
    s = String(s || '').toLowerCase();
    if (s === 'ok' || s === 'done' || s === 'success') return 'ok';
    if (s === 'skipped') return 'skip';
    if (s === 'error' || s === 'failed') return 'err';
    return '';
  }
  async function renderRuns() {
    var box = $('#runs-list');
    try {
      var out = await Fastt.get('/admin/stats/automation-runs', { kind: 'auto_posts', limit: 8 });
      var all = out.runs || [];
      // Run rows carry no rule_id — scope to this account, fall back to the
      // global (account_id null) rows only when this account has none.
      var mine = all.filter(function (r) { return String(r.account_id) === String(Fastt.account()); });
      if (!mine.length) mine = all.filter(function (r) { return r.account_id == null; });
      runsCache = mine;
      if (!mine.length) {
        box.innerHTML = '<div class="runrow"><span style="color:var(--muted)">No runs recorded for this creator yet — the automation has never fired here.</span></div>';
        renderPace();
        return;
      }
      box.innerHTML = mine.slice(0, 8).map(function (r) {
        var bits = runBits(r);
        return '<div class="runrow"><span class="rstat ' + statusCls(r.status) + '">' + esc(r.status || '?') + '</span>' +
          '<span class="rwhen" title="' + esc(r.started_at || '') + '">' + Fastt.fmtAgo(r.started_at) + '</span>' +
          '<span class="rbits">' + (bits ? '· ' + esc(bits) : '') + '</span></div>';
      }).join('');
      renderPace();
    } catch (e) {
      box.innerHTML = '<div class="runrow"><span class="rstat err">error</span><span style="color:var(--muted)">Could not load run history.</span></div>';
    }
  }

  function renderAll() { renderStatus(); renderPace(); renderQueue(); renderOverrides(); renderPaid(); renderChecks(); }
  renderAll();
  renderRuns();
  Fastt.liveBadge(card.querySelector('.fx-tc-title'));
  Fastt.liveBadge($('#pace-row').querySelector('.fx-sl-head'));
  Fastt.liveBadge($('#runs-h'));
  Fastt.liveBadge($('#grp-ovr').querySelector('h4'));
  Fastt.liveBadge($('#grp-repost').querySelector('h4'));
  Fastt.liveBadge($('#grp-paid').querySelector('h4'));
  Fastt.liveBadge($('#grp-test').querySelector('h4'));

  // "Shuffle caption + photo each cycle" is not a knob — it is what the engine
  // always does (_pools.pick_text / pick_media resolve per fire). Say so.
  (function () {
    var ck = $('#ck-shuffle');
    if (!ck) return;
    ck.classList.add('on');
    ck.style.pointerEvents = 'none';
    ck.style.opacity = '.75';
    var sub = document.createElement('span');
    sub.className = 'sub';
    sub.textContent = 'always on — the engine re-picks text + photos on every single fire';
    ck.appendChild(sub);
  })();

  // ── enable / disable (explicit click; enabling posts to the live feed) ──
  sw.addEventListener('click', function (e) {
    var next = !(rule && rule.is_enabled);
    if (next && !confirm('Enable Auto Posts? On its cadence this WILL create real posts on the live OnlyFans feed.')) {
      e.stopPropagation(); // keep fx-js from flipping the visual
      return;
    }
    saveRule({ is_enabled: next });
  });

  // ── run now (explicit click + confirm — this posts for real) ──
  $('#btn-runnow').addEventListener('click', function () {
    if (!rule) { Fastt.toast('No auto_posts rule saved yet', 'err'); return; }
    var ps = postsOf();
    if (!ps.length) { Fastt.toast('Queue is empty — nothing to post', 'err'); return; }
    var dry = !!payloadOf().dry_run;
    if (!confirm(dry
      ? 'Run Auto Posts now? Plan-only test run is ON, so it will write the plan to the log and post nothing.'
      : 'Post the next queued item to the LIVE OnlyFans feed right now?\n\n"' +
        capOf(ps[0]).slice(0, 120) + '"\n' + poolLabel(ps[0]))) return;
    Fastt.post('/admin/automation-rules/' + rule.id + '/run-now', {})
      .then(function () { Fastt.saved('Run queued — refresh in ~30s to see it'); setTimeout(renderRuns, 4000); })
      .catch(Fastt.oops);
  });

  // ── the two fx-checks ──
  $('#ck-ttl').addEventListener('click', function () {
    var turningOn = !$('#ck-ttl').classList.contains('on');
    var ps = postsCopy().map(function (q) {
      if (turningOn) { if (!(typeof q.hours_to_live === 'number' && q.hours_to_live > 0)) q.hours_to_live = 48; }
      else delete q.hours_to_live;
      return q;
    });
    savePosts(ps);
  });
  $('#ck-repost').addEventListener('click', function () {
    var turningOn = !$('#ck-repost').classList.contains('on');
    var pl = Object.assign({}, payloadOf());
    if (turningOn) {
      pl.resend_count = (Number(pl.resend_count) || 0) > 0 ? pl.resend_count : 2;
      pl.resend_after_hours = (Number(pl.resend_after_hours) || 0) > 0 ? pl.resend_after_hours : 24;
    } else { pl.resend_count = 0; }
    savePayload(pl);
  });
  $('#ck-dry').addEventListener('click', function () {
    var turningOn = !$('#ck-dry').classList.contains('on');
    savePayload(Object.assign({}, payloadOf(), { dry_run: turningOn }));
  });

  // ── posting-pace inputs ──
  function savePace() {
    var pl = Object.assign({}, payloadOf());
    var h = parseFloat($('#pace-hours').value), c = parseInt($('#pace-cycles').value, 10);
    if (isFinite(h) && h >= 1) pl.resend_after_hours = h; else delete pl.resend_after_hours;
    pl.resend_count = (isFinite(c) && c > 0) ? c : 0;
    savePayload(pl);
  }
  $('#pace-hours').addEventListener('change', savePace);
  $('#pace-cycles').addEventListener('change', savePace);
  $('#pace-every').addEventListener('change', function () {
    var d = parseFloat($('#pace-every').value);
    if (!isFinite(d) || d <= 0) { Fastt.toast('Needs a positive number of days', 'err'); renderPace(); return; }
    saveRule({ trigger: Object.assign({}, (rule && rule.trigger) || {}, { every_seconds: Math.round(d * 86400) }) });
  });

  // ── per-post overrides table (change = save) ──
  $('#ovr-table').addEventListener('change', function (e) {
    var inp = e.target.closest('input[data-i]');
    if (!inp) return;
    var i = Number(inp.dataset.i), k = inp.dataset.k;
    var ps = postsCopy();
    if (!ps[i]) return;
    var v = parseFloat(inp.value);
    if (k === 'price') ps[i].price = isFinite(v) && v > 0 ? Math.round(v * 100) / 100 : 0;
    else if (k === 'media_count') ps[i].media_count = (isFinite(v) && v >= 1) ? Math.round(v) : 1;
    else if (k === 'hours_to_live') { if (isFinite(v) && v > 0) ps[i].hours_to_live = v; else delete ps[i].hours_to_live; }
    else if (k === 'delay_minutes') { if (isFinite(v) && v > 0) ps[i].delay_minutes = v; else delete ps[i].delay_minutes; }
    savePosts(ps);
  });

  // ── paid-posts table (price + free-preview pool) ──
  $('#paid-table').addEventListener('change', function (e) {
    var inp = e.target.closest('[data-i]');
    if (!inp) return;
    var i = Number(inp.dataset.i), k = inp.dataset.k;
    var ps = postsCopy();
    if (!ps[i]) return;
    if (k === 'price') {
      var v = parseFloat(inp.value);
      ps[i].price = isFinite(v) && v > 0 ? Math.round(v * 100) / 100 : 0;
    } else if (k === 'preview_media_count') {
      var n = parseInt(inp.value, 10);
      if (isFinite(n) && n > 0) ps[i].preview_media_count = n; else delete ps[i].preview_media_count;
    } else if (k === 'preview_media_folder_id') {
      if (inp.value) ps[i].preview_media_folder_id = Number(inp.value);
      else delete ps[i].preview_media_folder_id;
    }
    savePosts(ps);
  });

  // ── queue: add / edit / remove / reorder (writes payload.posts) ──
  $('#qstrip').addEventListener('click', function (e) {
    var mv = e.target.closest('[data-mv]');
    if (mv) {
      e.stopPropagation();
      var i = Number(mv.dataset.mv), dir = Number(mv.dataset.dir), j = i + dir;
      var ps = postsCopy();
      if (!ps[i] || !ps[j]) return;
      var tmp = ps[i]; ps[i] = ps[j]; ps[j] = tmp;
      savePosts(ps);
      return;
    }
    var del = e.target.closest('[data-del]');
    if (del) {
      e.stopPropagation();
      var di = Number(del.dataset.del);
      var ps2 = postsCopy();
      if (!ps2[di]) return;
      if (!confirm('Remove this post from the queue?\n\n"' + capOf(ps2[di]).slice(0, 120) + '"\n\nIt only leaves the automation — nothing on the live feed changes.')) return;
      ps2.splice(di, 1);
      savePosts(ps2);
      return;
    }
    var ed = e.target.closest('[data-edit]');
    if (ed) { openEditor(Number(ed.dataset.edit)); return; }
    if (e.target.closest('#qadd')) openEditor(null);
  });
  $('#ovr-table').addEventListener('click', function (e) {
    var ed = e.target.closest('[data-edit]');
    if (ed) openEditor(Number(ed.dataset.edit));
  });
  $('#btn-addpost').addEventListener('click', function () { openEditor(null); });

  // ── folder-organized vault picker (mirrors messages.html openVault) ──
  //   Folders first (GET /api/of/v2/vault/lists?limit=50), click one to load
  //   its media (GET /api/of/v2/vault/media?list_id=<id>&limit=48), with an
  //   "All media" flat fallback and a back-to-folders breadcrumb.
  function vpBackSvg() { return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M15 18l-6-6 6-6"/></svg>'; }
  function vpChev() { return '<span class="vf-chev"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M9 6l6 6-6 6"/></svg></span>'; }
  function vpGridSvg() { return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/></svg>'; }
  function vpFolderSvg() { return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>'; }
  function vpHonest(title, why, remedy) {
    return '<div class="vp-honest"><b>' + esc(title) + '</b><br>' + esc(why) +
      (remedy ? '<br><br>Remedy: <code>' + esc(remedy) + '</code>' : '') + '</div>';
  }

  // preselected: [{id, thumb}]; onAttach(picked) receives the full selection.
  function openMediaPicker(preselected, onAttach) {
    var back = document.createElement('div');
    back.className = 'vp-back';
    back.innerHTML =
      '<div class="vp"><div class="vp-h"><span>Add media from vault</span><span class="x">✕</span></div>' +
      '<div class="vp-b"><div class="vp-loading">Loading folders…</div></div>' +
      '<div class="vp-f"><span class="vp-sel">0 selected</span>' +
      '<button class="fx-btn" id="vp-add" style="margin-left:auto">Attach to post</button></div></div>';
    document.body.appendChild(back);
    var body = back.querySelector('.vp-b');
    var chosen = {};   // media id -> thumb url, persists across folder navigation
    (preselected || []).forEach(function (p) { chosen[String(p.id)] = p.thumb || ''; });
    function close() { back.remove(); }
    back.addEventListener('click', function (e) { if (e.target === back || e.target.classList.contains('x')) close(); });
    function updSel() {
      var n = Object.keys(chosen).length;
      var el = back.querySelector('.vp-sel'); if (el) el.textContent = n + ' selected';
    }
    function crumb(here) {
      return '<div class="vp-crumb"><span class="vp-back-btn" data-back="1">' + vpBackSvg() + 'Folders</span>' +
        (here ? '<span class="vp-sep">/</span><span class="vp-here">' + esc(here) + '</span>' : '') + '</div>';
    }
    function tileGrid(list) {
      return '<div class="vp-grid">' + list.map(function (m) {
        var f = (m.files && (m.files.thumb || m.files.squarePreview || m.files.preview || m.files.full)) || null;
        var u = f && f.url;
        var sel = chosen[String(m.id)] != null ? ' sel' : '';
        return '<div class="vp-tile' + sel + '" data-mid="' + esc(m.id) + '" data-thumb="' + esc(u || '') + '">' +
          (u ? '<img src="' + esc(u) + '" alt="" loading="lazy">' : '') +
          '<span class="vp-tag">' + esc(m.type || '') + '</span></div>';
      }).join('') + '</div>';
    }
    // MEDIA view — listId null ⇒ "All media" (flat), else that folder's media.
    async function showMedia(listId, label) {
      body.innerHTML = crumb(label) + '<div class="vp-loading">Loading media…</div>';
      try {
        var params = { limit: 48, offset: 0 };
        if (listId) params.list_id = listId;
        var out = await Fastt.get('/api/of/v2/vault/media', params);
        var list = out.list || [];
        if (!list.length) {
          body.innerHTML = crumb(label) + vpHonest('No media here',
            listId ? 'GET /api/of/v2/vault/media?list_id=' + listId + ' returned 0 items.'
                   : 'GET /api/of/v2/vault/media returned 0 items for this account.');
          return;
        }
        body.innerHTML = crumb(label) + tileGrid(list);
      } catch (e) {
        body.innerHTML = crumb(label) + vpHonest('Vault media unavailable',
          'GET /api/of/v2/vault/media returned ' + ((e && e.status) || 'an error') + ' for this account.');
      }
    }
    // FOLDER view — the creator's folders + an "All media" shortcut.
    async function showFolders() {
      body.innerHTML = '<div class="vp-loading">Loading folders…</div>';
      var flist = [];
      try {
        var out = await Fastt.get('/api/of/v2/vault/lists', { limit: 50 });
        flist = out.list || [];
      } catch (e) {
        body.innerHTML = vpHonest('Vault folders unavailable',
          'GET /api/of/v2/vault/lists returned ' + ((e && e.status) || 'an error') + ' for this account.');
        return;
      }
      var html = '<div class="vp-folders">';
      html += '<div class="vp-folder all" data-list="__all__"><div class="vf-ic">' + vpGridSvg() +
        '</div><div class="vf-meta"><div class="vf-name">All media</div>' +
        '<div class="vf-count">Everything in the vault</div></div>' + vpChev() + '</div>';
      if (!flist.length) {
        html += '</div>' + vpHonest('No custom folders',
          'GET /api/of/v2/vault/lists returned 0 folders. Use “All media” above to pick from the full vault.');
        body.innerHTML = html;
        return;
      }
      flist.forEach(function (fd) {
        var parts = [];
        if (fd.photosCount) parts.push(fd.photosCount + ' photo' + (fd.photosCount === 1 ? '' : 's'));
        if (fd.videosCount) parts.push(fd.videosCount + ' video' + (fd.videosCount === 1 ? '' : 's'));
        if (fd.gifsCount) parts.push(fd.gifsCount + ' gif' + (fd.gifsCount === 1 ? '' : 's'));
        if (fd.audiosCount) parts.push(fd.audiosCount + ' audio');
        var count = parts.length ? parts.join(' · ') : 'empty';
        html += '<div class="vp-folder" data-list="' + esc(fd.id) + '" data-name="' + esc(fd.name || 'Folder') + '">' +
          '<div class="vf-ic">' + vpFolderSvg() + '</div>' +
          '<div class="vf-meta"><div class="vf-name">' + esc(fd.name || 'Untitled folder') + '</div>' +
          '<div class="vf-count">' + esc(count) + '</div></div>' + vpChev() + '</div>';
      });
      html += '</div>';
      body.innerHTML = html;
    }
    // Delegated clicks: folder → open, back → folders, tile → (de)select.
    body.addEventListener('click', function (e) {
      if (e.target.closest('[data-back]')) { showFolders(); return; }
      var fd = e.target.closest('.vp-folder');
      if (fd) {
        var lid = fd.getAttribute('data-list');
        if (lid === '__all__') showMedia(null, 'All media');
        else showMedia(lid, fd.getAttribute('data-name'));
        return;
      }
      var t = e.target.closest('.vp-tile');
      if (t) {
        var id = t.getAttribute('data-mid');
        if (chosen[id] != null) { delete chosen[id]; t.classList.remove('sel'); }
        else { chosen[id] = t.getAttribute('data-thumb') || ''; t.classList.add('sel'); }
        updSel();
      }
    });
    back.querySelector('#vp-add').addEventListener('click', function () {
      var picked = Object.keys(chosen).map(function (id) { return { id: id, thumb: chosen[id] || '' }; });
      onAttach(picked);
      close();
    });
    updSel();
    showFolders();
  }

  function openEditor(idx) {
    var isNew = (idx == null);
    var src = isNew ? { media_count: 1 } : (postsOf()[idx] || {});
    var texts = textsOf(src);
    var back = document.createElement('div');
    back.className = 'pe-back';
    back.innerHTML =
      '<div class="pe">' +
        '<h3>' + (isNew ? 'Add a post to the queue' : 'Edit queued post ' + (idx + 1)) + '</h3>' +
        '<div class="fx-card-sub">This writes into the Auto Posts rule only — nothing goes to the live feed until the automation fires.</div>' +
        '<div class="fx-field"><label>Caption variants — one per line, the engine picks one at random per fire</label>' +
          '<textarea class="fx-input" id="pe-texts" placeholder="text me before I leave&#10;guess what I\'m filming later…"></textarea>' +
          '<div class="fx-kv" id="pe-tcount"></div></div>' +
        '<div class="fx-grid2">' +
          '<div class="fx-field"><label>Photo pool — vault folder</label>' +
            '<select class="fx-select" id="pe-folder">' + folderOptionsHtml(src.media_folder_id, '— no folder —') + '</select></div>' +
          '<div class="fx-field"><label>Photos attached per fire</label>' +
            '<input class="fx-input" id="pe-count" value="' + (Number(src.media_count) || 1) + '"></div>' +
        '</div>' +
        '<div class="fx-field"><label>Hand-picked media — added to the pool alongside the folder</label>' +
          '<div class="pe-media" id="pe-media"></div></div>' +
        '<div class="fx-grid3">' +
          '<div class="fx-field"><label>Price</label><div class="fx-unit" data-unit="$">' +
            '<input class="fx-input" id="pe-price" value="' + (Number(src.price) || 0) + '"></div></div>' +
          '<div class="fx-field"><label>Keep for (h)</label>' +
            '<input class="fx-input" id="pe-ttl" value="' + ((Number(src.hours_to_live) > 0) ? src.hours_to_live : '') + '" placeholder="keep forever"></div>' +
          '<div class="fx-field"><label>Extra wait before it (min)</label>' +
            '<input class="fx-input" id="pe-delay" value="' + (Number(src.delay_minutes) || 0) + '"></div>' +
        '</div>' +
        '<div class="fx-grid2">' +
          '<div class="fx-field"><label>Free preview pool (paid posts only)</label>' +
            '<select class="fx-select" id="pe-pfolder">' + folderOptionsHtml(src.preview_media_folder_id, '— same pool as the post —') + '</select></div>' +
          '<div class="fx-field"><label>Free preview photos</label>' +
            '<input class="fx-input" id="pe-pcount" value="' + (Number(src.preview_media_count) || '') + '" placeholder="none"></div>' +
        '</div>' +
        '<div class="pe-foot">' +
          (isNew ? '' : '<button class="fx-btn ghost" id="pe-del" style="color:#e5735b;border-color:#4a3330">Remove from queue</button>') +
          '<span class="sp"></span>' +
          '<button class="fx-btn ghost" id="pe-cancel">Cancel</button>' +
          '<button class="fx-btn" id="pe-save">' + (isNew ? 'Add to queue' : 'Save post') + '</button>' +
        '</div>' +
      '</div>';
    document.body.appendChild(back);
    var ta = $('#pe-texts', back);
    ta.value = texts.join('\n');
    var keepIds = Array.isArray(src.media_files) ? src.media_files.slice() : [];
    var keepThumbs = {};   // media id (string) -> thumb url, for the picked-strip preview
    function renderMediaBox() {
      var box = $('#pe-media', back); if (!box) return;
      var n = keepIds.length;
      var thumbs = keepIds.slice(0, 16).map(function (id) {
        return '<div class="pm-th" data-th="' + esc(keepThumbs[String(id)] || '') + '"></div>';
      }).join('');
      box.innerHTML =
        '<div class="pm-top"><span class="pm-lbl">' +
          (n ? '<b>' + n + '</b> hand-picked item' + (n === 1 ? '' : 's') + ' stay in the pool'
             : 'No hand-picked media yet') + '</span>' +
          '<button type="button" class="fx-btn ghost" id="pm-pick" style="height:32px;padding:0 14px;margin-left:auto">' +
            (n ? 'Add more' : 'Pick from vault') + '</button>' +
          (n ? '<span class="pm-clear" id="pm-clear">clear</span>' : '') +
        '</div>' +
        (n ? '<div class="pm-thumbs">' + thumbs + '</div>' : '');
      // paint thumbs via DOM property — avoids breaking the style attribute on quoted urls
      box.querySelectorAll('.pm-th').forEach(function (el) {
        var u = el.getAttribute('data-th');
        if (u) el.style.backgroundImage = 'url(' + JSON.stringify(u) + ')';
      });
    }
    function hydrateThumbs() {
      var missing = keepIds.filter(function (id) { return !keepThumbs[String(id)]; });
      if (!missing.length) return;
      Promise.all(missing.map(function (id) {
        return mediaThumbUrl(id).then(function (u) { keepThumbs[String(id)] = u || ''; }).catch(function () {});
      })).then(renderMediaBox);
    }
    function close() { back.remove(); }
    back.addEventListener('click', function (e) { if (e.target === back) close(); });
    function countTexts() {
      var n = ta.value.split('\n').filter(function (s) { return s.trim(); }).length;
      $('#pe-tcount', back).textContent = n + ' variant' + (n === 1 ? '' : 's') +
        (n > 1 ? ' — one is drawn at random every time this post fires' : '');
    }
    ta.addEventListener('input', countTexts); countTexts();
    var mediaBox = $('#pe-media', back);
    mediaBox.addEventListener('click', function (e) {
      if (e.target.closest('#pm-clear')) { keepIds = []; keepThumbs = {}; renderMediaBox(); return; }
      if (e.target.closest('#pm-pick')) {
        var pre = keepIds.map(function (id) { return { id: id, thumb: keepThumbs[String(id)] || '' }; });
        openMediaPicker(pre, function (picked) {
          // The picker seeds with the current selection, so `picked` is the full
          // intended set — rebuild keepIds from it (a deselect really removes).
          keepIds = picked.map(function (p) {
            var id = /^\d+$/.test(String(p.id)) ? Number(p.id) : p.id;
            keepThumbs[String(id)] = p.thumb || keepThumbs[String(id)] || '';
            return id;
          });
          renderMediaBox();
        });
      }
    });
    renderMediaBox();
    hydrateThumbs();
    $('#pe-cancel', back).addEventListener('click', close);
    var delBtn = $('#pe-del', back);
    if (delBtn) delBtn.addEventListener('click', function () {
      if (!confirm('Remove this post from the queue?')) return;
      var ps = postsCopy(); ps.splice(idx, 1); close(); savePosts(ps);
    });
    $('#pe-save', back).addEventListener('click', function () {
      var lines = ta.value.split('\n').map(function (s) { return s.trim(); }).filter(Boolean);
      var q = Object.assign({}, src);
      delete q.text; delete q.texts;
      if (lines.length > 1) q.texts = lines; else if (lines.length === 1) q.text = lines[0];
      var fid = $('#pe-folder', back).value;
      if (fid) q.media_folder_id = Number(fid); else delete q.media_folder_id;
      if (keepIds.length) q.media_files = keepIds; else delete q.media_files;
      var mc = parseInt($('#pe-count', back).value, 10);
      q.media_count = (isFinite(mc) && mc > 0) ? mc : 1;
      var pr = parseFloat($('#pe-price', back).value);
      if (isFinite(pr) && pr > 0) q.price = Math.round(pr * 100) / 100; else delete q.price;
      var ttl = parseFloat($('#pe-ttl', back).value);
      if (isFinite(ttl) && ttl > 0) q.hours_to_live = ttl; else delete q.hours_to_live;
      var dm = parseFloat($('#pe-delay', back).value);
      if (isFinite(dm) && dm > 0) q.delay_minutes = dm; else delete q.delay_minutes;
      var pf = $('#pe-pfolder', back).value;
      if (pf) q.preview_media_folder_id = Number(pf); else delete q.preview_media_folder_id;
      var pc = parseInt($('#pe-pcount', back).value, 10);
      if (isFinite(pc) && pc > 0) q.preview_media_count = pc; else delete q.preview_media_count;

      if (!lines.length && q.media_folder_id == null && !(q.media_files || []).length) {
        Fastt.toast('A post needs a caption or a photo pool — the engine skips empty items', 'err');
        return;
      }
      if ((Number(q.price) || 0) > 0 && q.media_folder_id == null && !(q.media_files || []).length) {
        Fastt.toast('A priced post must carry media — OF rejects paid text-only posts', 'err');
        return;
      }
      var ps = postsCopy();
      if (isNew) ps.push(q); else ps[idx] = q;
      close();
      savePosts(ps);
    });
    ta.focus();
    try { ta.setSelectionRange(0, 0); ta.scrollTop = 0; } catch (e) { /* older engines */ }
  }
});
