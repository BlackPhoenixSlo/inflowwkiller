Fastt.ready(async function () {
  'use strict';
  var acct = Fastt.account();
  var esc = Fastt.esc, fmtInt = Fastt.fmtInt;
  var PAGE = 40;

  function qs(o) {
    var p = [];
    for (var k in o) if (o[k] !== undefined && o[k] !== null && o[k] !== '')
      p.push(encodeURIComponent(k) + '=' + encodeURIComponent(o[k]));
    return p.join('&');
  }

  // Header creator chip: show the REAL selected account and open the real
  // picker (the same menu fastt.js mounts on the sidebar .creator block) —
  // not the hardcoded "Aria" demo name.
  var csel = document.querySelector('.ghead .csel');
  if (csel) {
    var arow = Fastt.accountRow();
    for (var ci = 0; ci < csel.childNodes.length; ci++) {
      var cn = csel.childNodes[ci];
      if (cn.nodeType === 3 && cn.textContent.trim()) {
        cn.textContent = arow ? String(arow.nickname || arow.id) : (acct || 'Pick creator');
        break;
      }
    }
    csel.style.cursor = 'pointer';
    csel.addEventListener('click', function () {
      var c = document.querySelector('.creator');
      if (c) c.click();
    });
  }

  // Chrome with no backend gets badged on BOTH paths — the no-account early
  // return below must not leave the demo toolbar looking operable.
  var hact = document.querySelector('.hact');
  if (hact) Fastt.staticBadge(hact, 'STATIC DEMO');
  var topRow = document.querySelector('.vtoprow');
  var capSel = topRow && topRow.querySelector('.capsel');
  if (capSel) Fastt.staticBadge(capSel, 'STATIC');
  var recentEl = topRow && topRow.querySelector('.recent');
  // The grid/list view switch has no handler at all — it doesn't even flip its
  // own .on class. Badge it so it doesn't read as a working view toggle.
  var viewTog = document.querySelector('.viewtog');
  if (viewTog) Fastt.staticBadge(viewTog, 'STATIC');

  if (!acct) {
    if (recentEl) Fastt.staticBadge(recentEl, 'STATIC');
    // No creator selected — replace the demo rail/grid with an honest state.
    var r0 = document.getElementById('vp-rail');
    if (r0) {
      var c0 = r0.querySelector('.vcustom');
      r0.innerHTML = (c0 ? c0.outerHTML : '') +
        '<div class="vcat" style="cursor:default"><div class="vn" style="color:#6a6a6a">No creator selected</div>' +
        '<div class="vc"><span class="mut" style="color:#6a6a6a">pick one to load the vault</span></div></div>';
      var ic0 = r0.querySelector('.vcustom .r');
      if (ic0) Fastt.staticBadge(ic0, 'STATIC');
    }
    var g0 = document.getElementById('vp-grid');
    if (g0) g0.innerHTML = '<div class="vtile" style="grid-column:1/-1;min-height:120px;display:flex;align-items:center;justify-content:center;color:#6a6a6a;font-size:13px">No creator selected — click the creator name (top-left) to sign in and pick one.</div>';
    return;
  }

  var PHO = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="9" cy="10" r="1.5"/><path d="M4 18l5-4 3 2 4-4 4 4" stroke-linejoin="round"/></svg>';
  var VID = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><rect x="3" y="6" width="13" height="12" rx="2"/><path d="M16 10l5-3v10l-5-3z" stroke-linejoin="round"/></svg>';
  var AUD = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><rect x="9" y="3" width="6" height="11" rx="3"/><path d="M6 11a6 6 0 0 0 12 0M12 17v4" stroke-linecap="round"/></svg>';
  var DOT = '<span style="color:#4a4a4a">&middot;</span>';

  function countsHtml(p, v, g, a, plus) {
    var parts = [];
    var suf = plus ? '+' : '';
    if (p) parts.push('<span class="m">' + PHO + ' ' + fmtInt(p) + suf + '</span>');
    if (v) parts.push('<span class="m">' + VID + ' ' + fmtInt(v) + suf + '</span>');
    if (g) parts.push('<span class="m"><span class="gif">GIF</span> ' + fmtInt(g) + suf + '</span>');
    if (a) parts.push('<span class="m">' + AUD + ' ' + fmtInt(a) + suf + '</span>');
    if (!parts.length) return '<span class="mut" style="color:#6a6a6a">empty</span>';
    return parts.join(DOT);
  }

  // ══════════════════════════════════════════════════════════════
  // source: the local mirror when it has rows, else OnlyFans LIVE
  // ══════════════════════════════════════════════════════════════
  var sum = await Fastt.get('/admin/vault-ai/cache/summary');
  var SRC = (sum.count > 0) ? 'mirror' : 'live';
  var ofFolders = [], allCounts = { photo: 0, video: 0, gif: 0, audio: 0 }, allPlus = false;

  if (SRC === 'mirror') {
    var res = await Promise.all([
      Fastt.get('/admin/vault-ai/of-folders'),
      Fastt.get('/admin/vault-ai/items', { limit: 1000 }),
    ]);
    ofFolders = (res[0] && res[0].list) || [];
    var allOut = res[1] || { list: [] };
    (allOut.list || []).forEach(function (m) { if (allCounts[m.type] !== undefined) allCounts[m.type]++; });
    allPlus = !!allOut.hasMore;
  } else {
    // The live OF read is the ONLY source in this branch, and it is the branch
    // every creator takes while the mirror is empty. Letting it reject would
    // abort the whole ready() callback and leave the baked mockup ("All media
    // 837") on screen as if it were this creator's real vault — so fail loudly
    // and visibly instead.
    try {
      // `limit` is capped at 50 by the OF proxy — 100 comes back 422.
      var ls = await Fastt.get('/api/of/v2/vault/lists', { limit: 50 });
      var all = ls.all || {};
      allCounts = {
        photo: all.photosCount || 0, video: all.videosCount || 0,
        gif: all.gifsCount || 0, audio: all.audiosCount || 0,
      };
      ofFolders = (ls.list || []).map(function (f) {
        return {
          id: f.id, name: f.name, type: f.type,
          photosCount: f.photosCount || 0, videosCount: f.videosCount || 0,
          gifsCount: f.gifsCount || 0, audiosCount: f.audiosCount || 0,
        };
      });
    } catch (e) {
      var why = (e && e.status === 503) ? 'no captured OnlyFans session for this creator'
              : 'the OnlyFans vault read failed (' + ((e && e.status) || 'network') + ')';
      var railErr = document.getElementById('vp-rail');
      if (railErr) {
        railErr.querySelectorAll('.vcat').forEach(function (n) { n.remove(); });
        railErr.insertAdjacentHTML('beforeend',
          '<div class="vcat" style="cursor:default"><div class="vn" style="color:#6a6a6a">Vault unavailable</div></div>');
      }
      var gridErr = document.getElementById('vp-grid');
      if (gridErr) {
        gridErr.innerHTML = '<div class="vtile" style="grid-column:1/-1;min-height:120px;display:flex;'
          + 'align-items:center;justify-content:center;text-align:center;color:#6a6a6a;font-size:13px;padding:18px">'
          + 'Could not load this creator\'s vault — ' + Fastt.esc(why) + '.<br>'
          + 'Nothing is shown rather than stale numbers. Retry after re-capturing the session.</div>';
      }
      Fastt.oops(e);
      return;
    }
  }
  function folderTotal(f) {
    return (f.photosCount || 0) + (f.videosCount || 0) + (f.gifsCount || 0) + (f.audiosCount || 0);
  }

  // ── folder rail ──────────────────────────────────────────────
  var rail = document.getElementById('vp-rail');
  if (rail) {
    var custom = rail.querySelector('.vcustom');
    var h = custom ? custom.outerHTML : '';
    h += '<div class="vcat sel" data-all="1"><div class="vn">All media</div><div class="vc">' +
      countsHtml(allCounts.photo, allCounts.video, allCounts.gif, allCounts.audio, allPlus) + '</div></div>';
    var shown = ofFolders.slice().sort(function (a, b) { return folderTotal(b) - folderTotal(a); });
    shown.forEach(function (f) {
      h += '<div class="vcat" data-of-id="' + esc(f.id) + '"><div class="vn">' + esc(f.name || ('#' + f.id)) + '</div><div class="vc">' +
        countsHtml(f.photosCount, f.videosCount, f.gifsCount, f.audiosCount, false) + '</div></div>';
    });
    if (!shown.length)
      h += '<div class="vcat" style="cursor:default"><div class="vn" style="color:#6a6a6a">No folders</div><div class="vc"><span class="mut" style="color:#6a6a6a">OnlyFans returned no vault lists</span></div></div>';
    rail.innerHTML = h;
    var vcHead = rail.querySelector('.vcustom');
    if (SRC === 'live') Fastt.staticBadge(vcHead || rail, 'LIVE (OF)');
    else Fastt.liveBadge(vcHead || rail);
    // The rail-header SEARCH icon has no folder-search backend — badge just it.
    // (The "+" beside it now creates a real OF folder — wired below.)
    var vcSearch = vcHead && vcHead.querySelector('.r svg:first-child');
    if (vcSearch) Fastt.staticBadge(vcSearch, 'STATIC');
  }

  // ── grid (paged, either source) ──────────────────────────────
  var grid = document.getElementById('vp-grid');
  var state = { type: 'all', query: '', of_folder_id: null, sort: 'desc' };
  var page = { items: [], offset: 0, hasMore: false, loading: false, seq: 0 };
  var thumbMode = 'direct', probed = false;
  var selectMode = false, selectSet = new Set();
  function itemById(id) { for (var i = 0; i < page.items.length; i++) if (String(page.items[i].id) === String(id)) return page.items[i]; return null; }

  function normLive(m) {
    var f = m.files || {};
    var th = (f.thumb && f.thumb.url) || (f.squarePreview && f.squarePreview.url) || '';
    var folders = (m.listStates || []).filter(function (s) { return s.hasMedia; })
      .map(function (s) { return s.name; });
    return { id: m.id, type: m.type, ofThumb: th, duration: m.duration || 0, folders: folders, live: true,
             priced: false, described: false, raw: m };
  }
  function normMirror(m) {
    var folders = (m.listStates || []).filter(function (s) { return s && s.hasMedia; })
      .map(function (s) { return s.name; });
    return { id: m.id, type: m.type, ofThumb: m._thumb, duration: m.duration || 0,
             folders: folders, live: false,
             priced: !!(m._ai && m._ai.suggested_price_cents),
             described: !!(m._ai && m._ai.describe_status === 'described'),
             raw: m, ai: (m._ai || {}) };
  }
  function thumbOf(it) {
    if (!it.live) return it.ofThumb;
    if (!it.ofThumb) return '';
    // OF CDN urls are IP-signed; if this browser can't read them, route the
    // image through the relay's own OF-CDN proxy rather than show empty tiles.
    return thumbMode === 'direct' ? it.ofThumb
      : ('/admin/vault-ai/poster?' + qs({ account_id: acct, media_id: it.id, i: 0, u: it.ofThumb }));
  }
  function fmtDur(s) {
    s = Math.round(s || 0); if (s <= 0) return '';
    var m = Math.floor(s / 60), r = s % 60; return m + ':' + (r < 10 ? '0' : '') + r;
  }
  // ── video preview helpers ── the OF payload contract lives in
  //    _shared/vault-media.js, so the browser vault and the chat picker can
  //    never disagree about what is playable. The hover session below is NOT
  //    shared: it paints the tile's backgroundImage, the pickers paint img.src.
  var imgProxy            = Fastt.imgProxy,
      progressiveVideoSrc = FasttVault.progressiveVideoSrc,
      isDrmVideo          = FasttVault.isDrmVideo,
      videoPosterFrames   = FasttVault.videoPosterFrames;
  var PLAY_SVG = '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg>';
  function openVaultVideo(m) {
    var src = progressiveVideoSrc(m), drm = isDrmVideo(m), posters = videoPosterFrames(m);
    var back = document.createElement('div'); back.className = 'vid-back';
    var body;
    if (src) {
      var pf = (m.files && (m.files.squarePreview || m.files.preview || m.files.thumb)) || null;
      var poster = pf && pf.url;
      // Route through /img (Range-capable proxy) — OF's raw CDN video URLs are
      // IP-signed to the relay and 403 if the browser fetches them directly.
      body = '<video src="' + esc(imgProxy(src)) + '"' + (poster ? ' poster="' + esc(imgProxy(poster)) + '"' : '') +
        ' controls autoplay playsinline loop preload="metadata"></video>' +
        '<div class="vid-note">Streaming the ' + (m.videoSources && m.videoSources['240'] ? '240p' : (m.videoSources && m.videoSources['720'] ? '720p' : 'full')) +
        ' source straight from OnlyFans. If it stalls, the signed URL may have expired — press Refresh.</div>';
    } else if (drm && posters.length) {
      body = '<img id="vpFrame" src="' + esc(imgProxy(posters[0])) + '" alt="">' +
        '<div class="vid-note">OnlyFans protects this video with DRM (FairPlay) — it can’t be streamed here, ' +
        'so these are its ' + posters.length + ' preview frame' + (posters.length === 1 ? '' : 's') + '.</div>' +
        '<div class="vid-frames">' + posters.map(function (_, i) { return '<b class="' + (i === 0 ? 'on' : '') + '" data-i="' + i + '"></b>'; }).join('') + '</div>';
    } else {
      body = '<div class="vid-note">No playable preview for this video (no progressive source and no DRM poster frames).</div>';
    }
    back.innerHTML = '<div class="vid-wrap"><button class="vid-x" title="Close">&times;</button>' + body + '</div>';
    document.body.appendChild(back);
    function close() { var v = back.querySelector('video'); if (v) { try { v.pause(); } catch (e) {} } back.remove(); document.removeEventListener('keydown', onKey); }
    function onKey(e) { if (e.key === 'Escape') close(); }
    document.addEventListener('keydown', onKey);
    back.addEventListener('click', function (e) { if (e.target === back) close(); });
    back.querySelector('.vid-x').addEventListener('click', close);
    if (!src && drm && posters.length > 1) {
      var frame = back.querySelector('#vpFrame'), dots = back.querySelectorAll('.vid-frames b'), i = 0;
      function showFrame(n) { i = (n + posters.length) % posters.length; frame.src = imgProxy(posters[i]);
        Array.prototype.forEach.call(dots, function (d, j) { d.classList.toggle('on', j === i); }); }
      var tick = setInterval(function () { if (!back.isConnected) { clearInterval(tick); return; } showFrame(i + 1); }, 1100);
      back.querySelector('.vid-frames').addEventListener('click', function (e) {
        var b = e.target.closest('b'); if (b) { clearInterval(tick); showFrame(Number(b.getAttribute('data-i'))); } });
    }
  }
  // Ensure we have the LIVE playable shape (mirror rows are trimmed) then play.
  async function playVault(it) {
    if (!it) return;
    var raw = it.raw || {};
    var ok = raw.videoSources || (raw.files && (raw.files.drm || (raw.files.preview && raw.files.preview.options)));
    if (ok) { openVaultVideo(raw); return; }
    try { var m = await Fastt.get('/api/of/v2/vault/media/' + it.id); openVaultVideo(m); }
    catch (e) { Fastt.toast('Could not load this video for preview (' + ((e && e.status) || 'error') + ')'); }
  }

  function tileHtml(it) {
    var th = thumbOf(it);
    var title = '#' + it.id + ' · ' + (it.type || '') +
      (it.folders && it.folders.length ? ' · ' + it.folders.join(', ') : '');
    var sel = selectSet.has(it.id);
    var badge = '';
    if (it.type === 'video') badge = '<span class="vt-badge">▶ ' + esc(fmtDur(it.duration)) + '</span>';
    else if (it.type === 'gif') badge = '<span class="vt-badge">GIF</span>';
    else if (it.type === 'audio') badge = '<span class="vt-badge">' + AUD + '</span>';
    // Clickable play overlay (+ DRM chip) — same as the chat picker. Hidden in
    // select mode so a tile click toggles selection instead of playing.
    var overlay = '';
    if (it.type === 'video' && !selectMode) {
      var drm = isDrmVideo(it.raw);
      overlay = '<span class="vplay" data-play="' + esc(it.id) + '" title="' + (drm ? 'DRM — preview frames' : 'Play preview') + '">' + PLAY_SVG + '</span>' +
        (drm ? '<span class="vlock" title="OnlyFans DRM">🔒</span>' : '');
    }
    return '<div class="vtile' + (sel ? ' sel' : '') + '" data-id="' + esc(it.id) + '" title="' + esc(title) + '" style="' +
      (th ? 'background-image:url(\'' + esc(th) + '\');background-size:cover;background-position:center' : '') + '">' +
      (it.described ? '<span class="vt-dot" title="described"></span>' : '') +
      overlay +
      badge +
      (selectMode ? '<span class="vt-check">✓</span>' : '') +
      '</div>';
  }
  function renderGrid() {
    if (!grid) return;
    if (typeof stopHover === 'function') stopHover();   // drop any hover preview pointing at a tile we're replacing
    if (hoverDwell){ clearTimeout(hoverDwell); hoverDwell = null; }
    if (!page.items.length) {
      grid.innerHTML = '<div class="vtile" style="grid-column:1/-1;min-height:120px;display:flex;align-items:center;justify-content:center;color:#6a6a6a;font-size:13px">' +
        ((state.query || state.type !== 'all' || state.of_folder_id != null)
          ? 'Nothing matches this filter.'
          : 'OnlyFans returned no vault media for this creator.') + '</div>';
      return;
    }
    var html = page.items.map(tileHtml).join('');
    html += '<div style="grid-column:1/-1;display:flex;align-items:center;justify-content:center;gap:14px;' +
      'padding:6px 0 2px;color:#7a7a7a;font-size:12.5px">' +
      '<span>Showing ' + fmtInt(page.items.length) + ' · ' +
      (SRC === 'live' ? 'live from OnlyFans' : 'local mirror') + '</span>' +
      (page.hasMore ? '<button class="btn-blue" id="vp-more" style="height:34px">Load ' + PAGE + ' more</button>' : '') +
      '</div>';
    grid.innerHTML = html;
    var more = document.getElementById('vp-more');
    if (more) more.addEventListener('click', function () { loadGrid(true); });
  }
  async function fetchPage(offset) {
    if (SRC === 'live') {
      var p = { limit: PAGE, offset: offset, field: 'recent', sort: state.sort };
      if (state.type !== 'all') p.type = state.type;
      if (state.query) p.query = state.query;
      if (state.of_folder_id != null) p.list_id = state.of_folder_id;
      var out = await Fastt.get('/api/of/v2/vault/media', p);
      return { list: (out.list || []).map(normLive), hasMore: !!out.hasMore };
    }
    var p2 = { limit: PAGE, offset: offset, sort: state.sort === 'asc' ? 'oldest' : 'newest' };
    if (state.type !== 'all') p2.type = state.type;
    if (state.query) p2.query = state.query;
    if (state.of_folder_id != null) p2.of_folder_id = state.of_folder_id;
    var out2 = await Fastt.get('/admin/vault-ai/items', p2);
    return { list: (out2.list || []).map(normMirror), hasMore: !!out2.hasMore };
  }
  async function loadGrid(append) {
    if (!grid || page.loading) return;
    page.loading = true;
    var seq = ++page.seq;
    if (!append) { page.items = []; page.offset = 0; }
    try {
      var out = await fetchPage(page.offset);
      if (seq !== page.seq) return;
      page.items = page.items.concat(out.list);
      page.offset += out.list.length;
      page.hasMore = out.hasMore && out.list.length > 0;
      renderGrid();
      if (SRC === 'live' && !probed && page.items.length) {
        probed = true;
        var first = null;
        for (var i = 0; i < page.items.length; i++) if (page.items[i].ofThumb) { first = page.items[i]; break; }
        if (first) {
          var img = new Image();
          img.onerror = function () { thumbMode = 'proxy'; renderGrid(); };
          img.src = first.ofThumb;
        }
      }
    } catch (e) { Fastt.oops(e); } finally { page.loading = false; }
  }
  await loadGrid(false);
  if (topRow) Fastt.liveBadge(topRow);

  // "Recent" is the sort control in the mockup — make it real (newest ⇄ oldest)
  // instead of a decorative label.
  if (recentEl) {
    recentEl.style.cursor = 'pointer';
    recentEl.addEventListener('click', function () {
      state.sort = state.sort === 'desc' ? 'asc' : 'desc';
      var tn = null;
      for (var i = 0; i < recentEl.childNodes.length; i++) {
        var n = recentEl.childNodes[i];
        if (n.nodeType === 3 && n.textContent.trim()) { tn = n; break; }
      }
      if (tn) tn.textContent = state.sort === 'desc' ? 'Recent ' : 'Oldest ';
      loadGrid(false);
    });
  }

  if (rail) rail.addEventListener('click', function (ev) {
    var row = ev.target.closest('.vcat');
    if (!row || (!row.dataset.all && !row.dataset.ofId)) return;
    rail.querySelectorAll('.vcat').forEach(function (r) { r.classList.remove('sel'); });
    row.classList.add('sel');
    state.of_folder_id = row.dataset.ofId ? Number(row.dataset.ofId) : null;
    loadGrid(false);
  });
  var q = document.getElementById('vp-q');
  if (q) q.addEventListener('input', Fastt.debounce(function () {
    state.query = q.value.trim(); loadGrid(false);
  }, 350));
  var mrow = document.querySelector('.mrow');
  if (mrow) mrow.addEventListener('click', function (ev) {
    var pill = ev.target.closest('.seg-pill');
    if (!pill) return;
    var t = pill.textContent.trim().toLowerCase();
    state.type = (t === 'all') ? 'all' : t;
    loadGrid(false);
  });

  // ══════════════════════════════════════════════════════════════
  // FRIENDLIER LAYER — one primary action (Collect), a live state
  // chip, a click-to-manage drawer, folder writes, batch Tools, and
  // multi-select. Everything below writes to the REAL relay.
  // ══════════════════════════════════════════════════════════════
  var VIS_STATES = ['bare', 'covered', 'not_in_frame'];
  var TIERS = ['safe', 'suggestive', 'explicit', 'graphic', 'unknown'];
  function money(c) { return (c == null || c === '') ? '' : (Number(c) / 100).toFixed(2); }

  // ── collection state chip + primary Collect action + Tools ──────
  var ghead = document.querySelector('.ghead');
  var hactEl = document.querySelector('.hact');
  var stateChip = null, collectBtn = null;
  if (ghead && hactEl) {
    var wrap = document.createElement('div');
    wrap.style.cssText = 'display:flex;align-items:center;gap:10px;margin-left:16px';
    stateChip = document.createElement('div');
    stateChip.className = 'vp-state';
    stateChip.innerHTML = '<span class="dot"></span><span class="txt">…</span>';
    collectBtn = document.createElement('button');
    collectBtn.className = 'btn-out';
    collectBtn.type = 'button';
    collectBtn.innerHTML = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9"><path d="M12 4v10m0 0l-4-4m4 4l4-4M5 20h14" stroke-linecap="round" stroke-linejoin="round"/></svg><span>Collect all</span>';
    var selBtn = document.createElement('button');
    selBtn.className = 'btn-out'; selBtn.type = 'button';
    selBtn.innerHTML = '<span>Select</span>';
    var toolsBtn = document.createElement('button');
    toolsBtn.className = 'btn-tools'; toolsBtn.type = 'button';
    toolsBtn.innerHTML = '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M12 15a3 3 0 100-6 3 3 0 000 6z"/><path d="M19 12a7 7 0 00-.1-1l2-1.5-2-3.4-2.3.9a7 7 0 00-1.7-1l-.3-2.5h-4l-.3 2.5a7 7 0 00-1.7 1L4 5.6l-2 3.4L4 10.5a7 7 0 000 3L2 15l2 3.4 2.3-.9a7 7 0 001.7 1l.3 2.5h4l.3-2.5a7 7 0 001.7-1l2.3.9 2-3.4-2-1.5a7 7 0 00.1-1z" stroke-linejoin="round"/></svg><span>Tools</span>';
    wrap.appendChild(stateChip); wrap.appendChild(collectBtn); wrap.appendChild(selBtn); wrap.appendChild(toolsBtn);
    ghead.insertBefore(wrap, hactEl);
    Fastt.liveBadge(stateChip);
  }

  var collecting = false;
  function paintState(s) {
    if (!stateChip) return;
    var dot = stateChip.querySelector('.dot'), txt = stateChip.querySelector('.txt');
    var run = s && s.run;
    if (run && run.running) {
      dot.className = 'dot run';
      txt.textContent = 'Collecting… ' + fmtInt(run.total_seen || 0);
      if (collectBtn) { collectBtn.disabled = true; collectBtn.querySelector('span').textContent = 'Collecting…'; }
    } else if ((s && s.count) > 0) {
      dot.className = 'dot on';
      txt.textContent = fmtInt(s.count) + ' cached · ' + (SRC === 'mirror' ? 'local' : 'live');
      if (collectBtn) { collectBtn.disabled = false; collectBtn.querySelector('span').textContent = 'Re-collect'; }
    } else {
      dot.className = 'dot';
      txt.textContent = 'Live · not collected';
      if (collectBtn) { collectBtn.disabled = false; collectBtn.querySelector('span').textContent = 'Collect all'; }
    }
  }
  paintState(sum);
  async function pollCollect() {
    for (var i = 0; i < 240 && collecting; i++) {
      await new Promise(function (r) { setTimeout(r, 2500); });
      var st;
      try { st = await Fastt.get('/admin/vault-ai/collect/status'); } catch (e) { continue; }
      var run = st && st.run;
      paintState({ count: (run && run.upserted) || 0, run: run });
      if (run && !run.running) {
        collecting = false;
        Fastt.toast('Collect finished — ' + fmtInt(run.upserted || 0) + ' items mirrored. Reloading…');
        setTimeout(function () { location.reload(); }, 1200);
        return;
      }
    }
  }
  if (collectBtn) collectBtn.addEventListener('click', async function () {
    if (collecting) return;
    var re = (sum && sum.count > 0);
    if (!confirm((re ? 'Re-scan' : 'Scan') + ' this creator\'s ENTIRE OnlyFans vault into the local mirror?\n\n' +
      'This reads every media page from OnlyFans (no messages are sent). It runs in the background and unlocks describe, tag search and per-item editing.')) return;
    try {
      collecting = true;
      await Fastt.post('/admin/vault-ai/collect', {});
      paintState({ count: 0, run: { running: true, total_seen: 0 } });
      pollCollect();
    } catch (e) { collecting = false; Fastt.oops(e); }
  });

  // ── slide-over drawer scaffold ──────────────────────────────────
  var appRoot = document.querySelector('.app');
  var scrim = document.createElement('div'); scrim.className = 'vp-scrim';
  var draw = document.createElement('aside'); draw.className = 'vp-draw';
  appRoot.appendChild(scrim); appRoot.appendChild(draw);
  function closeDraw() { scrim.classList.remove('show'); draw.classList.remove('show'); }
  scrim.addEventListener('click', closeDraw);
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape') closeDraw(); });
  function openDraw(html) { draw.innerHTML = html; scrim.classList.add('show'); draw.classList.add('show'); }
  // The drawer's media block plays the video (same lightbox as the grid + chat).
  var currentDrawerItem = null;
  draw.addEventListener('click', function (e) {
    if (e.target.closest('[data-vplay]') && currentDrawerItem) playVault(currentDrawerItem);
  });

  function bestImg(it) {
    if (!it.live) return '/admin/vault-ai/image?' + qs({ account_id: acct, media_id: it.id });
    var f = (it.raw && it.raw.files) || {};
    return (f.full && f.full.url) || (f.preview && f.preview.url) ||
           (f.squarePreview && f.squarePreview.url) || it.ofThumb || '';
  }

  // ── per-item detail drawer ──────────────────────────────────────
  function openItem(it) {
    currentDrawerItem = it;
    var ai = it.ai || {};
    var flds = ai.fields || {};
    var img = bestImg(it);
    var isVid = it.type === 'video';
    var meta = ['<span class="vpd-chip">#' + esc(it.id) + '</span>',
                '<span class="vpd-chip">' + esc(it.type || '') + '</span>'];
    if (it.duration) meta.push('<span class="vpd-chip">' + esc(fmtDur(it.duration)) + '</span>');
    (it.folders || []).forEach(function (nm) { meta.push('<span class="vpd-chip">📁 ' + esc(nm) + '</span>'); });

    var mediaBlock = '<div class="vpd-media">' +
      (img ? '<img src="' + esc(img) + '" alt="" onerror="this.style.display=\'none\'">' : '') +
      (isVid ? '<div class="vpd-play" data-vplay="1" title="Play preview"><span>▶</span></div>' : '') + '</div>';

    var editable = (SRC === 'mirror');
    var aiFieldRows = '';
    if (editable) {
      var beats = Array.isArray(flds.beats) ? flds.beats : [];
      var shown = Object.keys(flds).filter(function (k) {
        return ['beats', 'description', 'tags', '_prompt_version', '_frames'].indexOf(k) < 0 &&
               flds[k] != null && flds[k] !== '' && flds[k] !== 'none' &&
               !(Array.isArray(flds[k]) && !flds[k].length);
      });
      if (beats.length) {
        aiFieldRows += '<div class="vpd-lbl">What happens (' + beats.length + ')</div><ol style="margin:0;padding-left:18px;font-size:12.5px;color:#e8e8e8">' +
          beats.map(function (b) { return '<li>' + esc(String(b)) + '</li>'; }).join('') + '</ol>';
      }
      if (shown.length) {
        aiFieldRows += '<div class="vpd-lbl">AI read-out</div><dl class="vpd-aifld">' +
          shown.map(function (k) {
            var v = Array.isArray(flds[k]) ? flds[k].join(', ') : (flds[k] === true ? 'yes' : flds[k] === false ? 'no' : String(flds[k]));
            return '<dt>' + esc(k.replace(/_/g, ' ')) + '</dt><dd>' + esc(v) + '</dd>';
          }).join('') + '</dl>';
      }
    }

    var editor = editable ? (
      '<div class="vpd-lbl">Description</div>' +
      '<textarea class="vpd-in" data-f="description" rows="3" placeholder="Full description…">' + esc(ai.description || ai.video_description || '') + '</textarea>' +
      aiFieldRows +
      '<div class="vpd-lbl">Tags</div>' +
      '<input class="vpd-in" data-f="tags" placeholder="comma, separated, tags" value="' + esc((ai.tags || []).join(', ')) + '">' +
      '<div class="vpd-lbl">On show — decides folder, price &amp; mass-send</div>' +
      '<div class="vpd-row">' +
        visSel('breasts', flds.breasts_vis) + visSel('vulva', flds.vulva_vis) + visSel('anus', flds.anus_vis) +
      '</div>' +
      '<div class="vpd-row" style="margin-top:6px">' +
        '<div><div class="vpd-lbl">Tier</div>' + tierSel(ai.explicitness_tier) + '</div>' +
        '<div><div class="vpd-lbl">Price ($)</div><input class="vpd-in" data-f="suggested_price_cents" inputmode="decimal" placeholder="—" value="' + esc(money(ai.suggested_price_cents)) + '"></div>' +
      '</div>' +
      '<div class="vpd-lbl">Suggested caption</div>' +
      '<input class="vpd-in" data-f="suggested_caption" placeholder="One-line PPV/DM caption…" value="' + esc(ai.suggested_caption || '') + '">' +
      '<div class="vpd-lbl">Suggested script</div>' +
      '<textarea class="vpd-in" data-f="suggested_script" rows="2" placeholder="Longer sell copy…">' + esc(ai.suggested_script || '') + '</textarea>'
    ) : (
      '<div class="vpd-note"><b>Live from OnlyFans.</b> Describe, tag search and field editing write to the local mirror — ' +
      'click <b>Collect all</b> (top) to scan this vault first. You can still remove this item from the OnlyFans vault below.</div>'
    );

    openDraw(
      '<div class="vpd-head"><span class="t">#' + esc(it.id) + ' · ' + esc(it.type) + (it.duration ? ' · ' + esc(fmtDur(it.duration)) : '') + '</span>' +
        '<button class="vpd-x" title="Close (Esc)">✕</button></div>' +
      '<div class="vpd-body">' + mediaBlock + '<div class="vpd-meta">' + meta.join('') + '</div>' + editor + '</div>' +
      '<div class="vpd-bar">' +
        (editable ? '<button class="btn-blue" data-act="save" style="height:38px" disabled>Save &amp; lock</button>' +
                    '<button class="btn-out" data-act="describe" style="height:38px">' + (it.described ? 'Re-describe' : 'Describe') + '</button>' : '') +
        '<button class="btn-out" data-act="remove" style="height:38px;margin-left:auto;border-color:#5a2b28;color:#f0715f">Remove from vault</button>' +
      '</div>'
    );
    draw.querySelector('.vpd-x').addEventListener('click', closeDraw);
    wireDrawer(it);
  }
  function visSel(name, val) {
    var opts = '<option value="">—</option>' + VIS_STATES.map(function (v) {
      return '<option value="' + v + '"' + (v === val ? ' selected' : '') + '>' + (v === 'not_in_frame' ? 'not in frame' : v) + '</option>';
    }).join('');
    return '<div><div class="vpd-lbl" style="margin-top:0">' + name + '</div><select class="vpd-in" data-f="' + name + '_vis">' + opts + '</select></div>';
  }
  function tierSel(val) {
    return '<select class="vpd-in" data-f="explicitness_tier"><option value="">—</option>' +
      TIERS.map(function (t) { return '<option value="' + t + '"' + (t === val ? ' selected' : '') + '>' + t + '</option>'; }).join('') + '</select>';
  }
  function wireDrawer(it) {
    var saveBtn = draw.querySelector('[data-act="save"]');
    var inputs = Array.prototype.slice.call(draw.querySelectorAll('.vpd-in'));
    var init = {};
    inputs.forEach(function (el) { init[el.dataset.f] = el.value; });
    function dirty() { return inputs.some(function (el) { return el.value !== init[el.dataset.f]; }); }
    inputs.forEach(function (el) {
      var ev = (el.tagName === 'SELECT') ? 'change' : 'input';
      el.addEventListener(ev, function () { if (saveBtn) saveBtn.disabled = !dirty(); });
    });
    if (saveBtn) saveBtn.addEventListener('click', async function () {
      var fields = {};
      inputs.forEach(function (el) {
        if (el.value === init[el.dataset.f]) return;
        var f = el.dataset.f;
        if (f === 'tags') fields.tags = el.value;
        else if (f === 'suggested_price_cents') {
          var t = el.value.trim();
          fields.suggested_price_cents = t === '' ? null : Math.round(Number(t.replace(/[^0-9.]/g, '')) * 100);
        } else fields[f] = el.value;
      });
      if (!Object.keys(fields).length) return;
      saveBtn.disabled = true; saveBtn.textContent = 'Saving…';
      try {
        var res = await Fastt.patch('/admin/vault-ai/items/' + it.id, { account_id: acct, fields: fields });
        // merge back so re-opening shows the override
        var v = res.item || {};
        it.ai = Object.assign(it.ai || {}, {
          description: v.description, tags: v.tags, explicitness_tier: v.explicitness_tier,
          suggested_caption: v.suggested_caption, suggested_script: v.suggested_script,
          suggested_price_cents: v.suggested_price_cents,
        });
        Fastt.saved(); saveBtn.textContent = 'Saved · locked ' + (res.locked || []).length;
        inputs.forEach(function (el) { init[el.dataset.f] = el.value; });
      } catch (e) { Fastt.oops(e); saveBtn.textContent = 'Save & lock'; saveBtn.disabled = false; }
    });
    var descBtn = draw.querySelector('[data-act="describe"]');
    if (descBtn) descBtn.addEventListener('click', async function () {
      if (!confirm('Run AI vision on #' + it.id + '? This spends a small amount of LLM budget (a few hundredths of a cent).')) return;
      descBtn.disabled = true; descBtn.textContent = 'Describing…';
      try {
        var r = await Fastt.post('/admin/vault-ai/describe', { account_id: acct, media_id: Number(it.id) });
        if (r && r.ok) {
          it.described = true; it.ai = it.ai || {}; it.ai.description = r.description; it.ai.tags = r.tags || [];
          Fastt.toast('Described.'); openItem(it);
          loadGrid(false);
        } else { Fastt.toast('Not described: ' + ((r && r.status) || 'unknown')); descBtn.disabled = false; descBtn.textContent = 'Describe'; }
      } catch (e) { Fastt.oops(e); descBtn.disabled = false; descBtn.textContent = 'Describe'; }
    });
    var rmBtn = draw.querySelector('[data-act="remove"]');
    if (rmBtn) rmBtn.addEventListener('click', async function () {
      if (!confirm('Remove #' + it.id + ' from the OnlyFans vault?\n\nThis uses OnlyFans’ own "Remove from vault" — the media is HIDDEN, not destroyed (already-sent PPVs keep working). OnlyFans offers NO undo.')) return;
      rmBtn.disabled = true; rmBtn.textContent = 'Removing…';
      try {
        await Fastt.post('/admin/vault-ai/items/' + it.id + '/hide', { account_id: acct });
        Fastt.toast('Removed from vault.'); closeDraw(); loadGrid(false);
      } catch (e) { Fastt.oops(e); rmBtn.disabled = false; rmBtn.textContent = 'Remove from vault'; }
    });
  }

  // ── grid clicks: open item OR toggle selection ──────────────────
  if (grid) grid.addEventListener('click', function (ev) {
    if (ev.target.closest('#vp-more')) return;
    var playEl = ev.target.closest('[data-play]');
    if (playEl) { ev.stopPropagation(); playVault(itemById(playEl.getAttribute('data-play'))); return; }
    var tile = ev.target.closest('.vtile[data-id]');
    if (!tile) return;
    var it = itemById(tile.dataset.id);
    if (!it) return;
    if (selectMode) {
      if (selectSet.has(it.id)) selectSet.delete(it.id); else selectSet.add(it.id);
      tile.classList.toggle('sel');
      paintSelBar();
    } else {
      grid.querySelectorAll('.vtile.pick').forEach(function (t) { t.classList.remove('pick'); });
      tile.classList.add('pick');
      openItem(it);
    }
  });

  // ── hover-scrub video previews (same behaviour as the chat picker) ──
  // Dwell on a video tile → the relay's 12-frame storyboard cycles in the
  // tile background; a countdown covers the cold build; leaving cancels it.
  // Operates on background-image (these tiles have no <img>). Self-cleans the
  // instant the tile detaches (grid re-render) so no scrub fetches leak.
  var HOVER = null, hoverDwell = null;
  var scrubUrl = FasttVault.scrubUrl;
  function setBg(tile, url){ tile.style.backgroundImage = "url('" + String(url).replace(/'/g, '%27') + "')"; }
  function stopHover(){
    if (!HOVER) return;
    var h = HOVER; HOVER = null;
    h.timers.forEach(function(t){ clearInterval(t); clearTimeout(t); });
    if (h.tile && h.tile.isConnected){ h.tile.style.backgroundImage = h.origBg; h.tile.classList.remove('vhovering'); var cd = h.tile.querySelector('.vhover-cd'); if (cd) cd.remove(); }
    if (h.url){ try{ fetch('/img/scrub/cancel?u=' + encodeURIComponent(h.url) + '&sid=' + encodeURIComponent(h.sid), { method: 'POST', keepalive: true }); }catch(e){} }
  }
  function slideTick(sid){ if (!HOVER || HOVER.sid !== sid || !HOVER.tile.isConnected){ stopHover(); return; } }
  function startHover(tile){
    var it = itemById(tile.dataset.id); if (!it || it.type !== 'video') return;
    var raw = it.raw || it, url = progressiveVideoSrc(raw), posters = videoPosterFrames(raw);
    if (!url && !posters.length) return;
    stopHover();
    var sid = Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
    HOVER = { sid: sid, url: url, dur: raw.duration || 0, tile: tile, origBg: tile.style.backgroundImage, frame: 0, timers: [] };
    tile.classList.add('vhovering');
    if (posters.length) setBg(tile, imgProxy(posters[0]));   // instant fallback while the storyboard builds
    if (url){
      var est = Math.max(3, Math.round((raw.duration || 30) / 8));
      var cd = document.createElement('span'); cd.className = 'vhover-cd'; cd.textContent = est + 's'; tile.appendChild(cd);
      var started = Date.now();
      var cdTimer = setInterval(function(){
        slideTick(sid); if (!HOVER || HOVER.sid !== sid) return;
        var left = Math.max(0, est - (Date.now() - started) / 1000);
        cd.textContent = left > 0 ? (left.toFixed(1) + 's') : '…';
      }, 100);
      HOVER.timers.push(cdTimer);
      var probe = new Image();
      probe.onload = function(){
        if (!HOVER || HOVER.sid !== sid) return;
        clearInterval(cdTimer);
        var el = HOVER.tile.querySelector('.vhover-cd'); if (el) el.remove();
        for (var i = 1; i < 12; i++){ var pi = new Image(); pi.src = scrubUrl(url, i, raw.duration, sid); }
        HOVER.timers.push(setInterval(function(){
          slideTick(sid); if (!HOVER || HOVER.sid !== sid) return;
          HOVER.frame = (HOVER.frame + 1) % 12;
          setBg(HOVER.tile, scrubUrl(url, HOVER.frame, raw.duration, sid));
        }, 600));
      };
      probe.onerror = function(){ if (HOVER && HOVER.sid === sid) stopHover(); };
      probe.src = scrubUrl(url, 0, raw.duration, sid);
    } else if (posters.length > 1){
      var pi2 = 0;
      HOVER.timers.push(setInterval(function(){
        slideTick(sid); if (!HOVER || HOVER.sid !== sid) return;
        pi2 = (pi2 + 1) % posters.length; setBg(HOVER.tile, imgProxy(posters[pi2]));
      }, 700));
    }
  }
  if (grid){
    grid.addEventListener('mouseover', function(ev){
      if (selectMode) return;   // in select mode a tile is for picking, not previewing
      var tile = ev.target.closest('.vtile[data-id]'); if (!tile) return;
      if (HOVER && HOVER.tile === tile) return;
      var it = itemById(tile.dataset.id); if (!it || it.type !== 'video') return;
      if (hoverDwell) clearTimeout(hoverDwell);
      hoverDwell = setTimeout(function(){ startHover(tile); }, 400);
    });
    grid.addEventListener('mouseout', function(ev){
      var tile = ev.target.closest('.vtile[data-id]'); if (!tile) return;
      if (ev.relatedTarget && tile.contains(ev.relatedTarget)) return;
      if (hoverDwell){ clearTimeout(hoverDwell); hoverDwell = null; }
      if (HOVER && HOVER.tile === tile) stopHover();
    });
  }

  // ── select mode + bulk action bar (real OF folder writes) ───────
  var selBarEl = null;
  var rpanel = document.querySelector('.rpanel');
  var mrowEl = document.querySelector('.mrow');
  function ensureSelBar() {
    if (selBarEl) return selBarEl;
    selBarEl = document.createElement('div'); selBarEl.className = 'vp-selbar'; selBarEl.style.display = 'none';
    if (rpanel && mrowEl && mrowEl.nextSibling) rpanel.insertBefore(selBarEl, mrowEl.nextSibling);
    else if (rpanel) rpanel.appendChild(selBarEl);
    return selBarEl;
  }
  function paintSelBar() {
    var bar = ensureSelBar();
    if (!selectMode) { bar.style.display = 'none'; return; }
    var ids = Array.from(selectSet);
    var folderOpts = liveFolders.map(function (f) {
      return '<option value="' + esc(f.id) + '">' + esc(f.name || ('#' + f.id)) + '</option>';
    }).join('');
    bar.style.display = 'flex';
    bar.innerHTML = '<span class="n">' + ids.length + ' selected</span>' +
      '<button class="btn-out" data-a="newf" style="height:34px"' + (ids.length ? '' : ' disabled') + '>+ New folder with these</button>' +
      (folderOpts ? '<select data-a="addf"' + (ids.length ? '' : ' disabled') + '><option value="">Add to folder…</option>' + folderOpts + '</select>' : '') +
      (SRC === 'mirror' ? '<button class="btn-out" data-a="descsel" style="height:34px"' + (ids.length ? '' : ' disabled') + '>Describe selected</button>' : '') +
      '<button class="lnk" data-a="clear">Clear</button>';
  }
  if (selBtn) selBtn.addEventListener('click', function () {
    selectMode = !selectMode; selectSet = new Set();
    selBtn.classList.toggle('btn-blue', selectMode);
    selBtn.classList.toggle('btn-out', !selectMode);
    selBtn.querySelector('span').textContent = selectMode ? 'Selecting…' : 'Select';
    renderGrid(); paintSelBar();
  });
  document.addEventListener('click', async function (ev) {
    var el = ev.target.closest('.vp-selbar [data-a]'); if (!el) return;
    var a = el.dataset.a, ids = Array.from(selectSet).map(Number);
    if (a === 'clear') { selectSet = new Set(); renderGrid(); paintSelBar(); return; }
    if (a === 'newf') {
      var nm = prompt('New OnlyFans folder name:'); if (!nm || !nm.trim()) return;
      if (!confirm('Create OnlyFans folder “' + nm.trim() + '” with ' + ids.length + ' item(s)?')) return;
      try { await Fastt.post('/admin/vault-ai/of-folders', { account_id: acct, name: nm.trim(), media_ids: ids });
        Fastt.toast('Folder created on OnlyFans.'); setTimeout(function () { location.reload(); }, 900); } catch (e) { Fastt.oops(e); }
    } else if (a === 'descsel') {
      if (!confirm('Run AI vision on ' + ids.length + ' selected item(s)? Spends LLM budget.')) return;
      el.disabled = true; var done = 0;
      for (var i = 0; i < ids.length; i++) { try { await Fastt.post('/admin/vault-ai/describe', { account_id: acct, media_id: ids[i] }); } catch (e) {} el.textContent = 'Describing ' + (++done) + '/' + ids.length; }
      Fastt.toast('Described ' + done + ' item(s).'); selectSet = new Set(); loadGrid(false); paintSelBar();
    }
  });
  document.addEventListener('change', async function (ev) {
    var sel = ev.target.closest('.vp-selbar select[data-a="addf"]'); if (!sel || !sel.value) return;
    var ids = Array.from(selectSet).map(Number), fid = sel.value, nm = sel.options[sel.selectedIndex].text;
    if (!confirm('Add ' + ids.length + ' item(s) to “' + nm + '” on OnlyFans?')) { sel.value = ''; return; }
    try { await Fastt.post('/admin/vault-ai/of-folders/' + fid + '/add', { account_id: acct, media_ids: ids });
      Fastt.toast('Added to folder.'); selectSet = new Set(); renderGrid(); paintSelBar(); } catch (e) { Fastt.oops(e); }
    sel.value = '';
  });

  // ── folder rail writes: create (+), rename, delete ──────────────
  var liveFolders = ofFolders.slice();
  var railEl = document.getElementById('vp-rail');
  if (railEl) {
    // decorate each real folder row with a hover menu (rename / delete)
    railEl.querySelectorAll('.vcat[data-of-id]').forEach(function (row) {
      var m = document.createElement('div'); m.className = 'fmenu';
      m.innerHTML = '<button data-fa="rename" title="Rename">✎</button><button data-fa="delete" title="Delete folder">🗑</button>';
      row.appendChild(m);
    });
    // "+" in the rail header creates a real OF folder
    var plusIcon = railEl.querySelector('.vcustom .r svg:last-child');
    if (plusIcon) {
      plusIcon.style.cursor = 'pointer'; plusIcon.style.pointerEvents = 'auto';
      plusIcon.addEventListener('click', async function (ev) {
        ev.stopPropagation();
        var nm = prompt('New OnlyFans folder name:'); if (!nm || !nm.trim()) return;
        if (!confirm('Create OnlyFans folder “' + nm.trim() + '”?')) return;
        try { await Fastt.post('/admin/vault-ai/of-folders', { account_id: acct, name: nm.trim(), media_ids: [] });
          Fastt.toast('Folder created.'); setTimeout(function () { location.reload(); }, 900); } catch (e) { Fastt.oops(e); }
      });
      // this icon can DO something now → drop the STATIC badge on its group by
      // re-badging only the search icon.
    }
    railEl.addEventListener('click', async function (ev) {
      var b = ev.target.closest('.fmenu button'); if (!b) return;
      ev.stopPropagation();
      var row = b.closest('.vcat'); var fid = row.dataset.ofId;
      var nameEl = row.querySelector('.vn'); var cur = nameEl ? nameEl.textContent : '';
      if (b.dataset.fa === 'rename') {
        var nm = prompt('Rename folder:', cur); if (!nm || !nm.trim() || nm.trim() === cur) return;
        try { await Fastt.post('/admin/vault-ai/of-folders/' + fid + '/rename', { account_id: acct, name: nm.trim() });
          if (nameEl) nameEl.textContent = nm.trim(); Fastt.saved(); } catch (e) { Fastt.oops(e); }
      } else if (b.dataset.fa === 'delete') {
        if (!confirm('Delete folder “' + cur + '” on OnlyFans? (the media stays in your vault)')) return;
        try { await Fastt.del('/admin/vault-ai/of-folders/' + fid + '?' + qs({ account_id: acct, clear_media: false }));
          Fastt.toast('Folder deleted.'); row.remove(); } catch (e) { Fastt.oops(e); }
      }
    });
  }

  // ── Tools drawer (Advanced): batch jobs + honest review signals ─
  var jobs = {};
  function toolsHtml() {
    var collected = (sum && sum.count > 0);
    var gate = collected ? '' : ' disabled title="Collect the vault first"';
    return '<div class="vpd-head"><span class="t">Vault tools</span><button class="vpd-x" title="Close (Esc)">✕</button></div>' +
      '<div class="vpd-body">' +
      '<div class="tl-sec"><h4>Describe all</h4><p>Run AI vision over every un-described item in the background. Writes description, tags and the sell-taxonomy that folders + pricing read.</p>' +
        '<div style="display:flex;align-items:center;gap:8px"><button class="tl-run" data-job="describe"' + gate + '>Describe all</button><span class="tl-cnt" id="tl-describe"></span></div></div>' +
      '<div class="tl-sec"><h4>Harvest OnlyFans search</h4><p>Runs OnlyFans’ own search across ~50 selling keywords and folds the hits into local tag search.</p>' +
        '<div style="display:flex;align-items:center;gap:8px"><button class="tl-run" data-job="harvest"' + gate + '>Harvest (50)</button><span class="tl-cnt" id="tl-harvest"></span></div></div>' +
      '<div class="tl-sec"><h4>Review queues</h4><p>The counts below are live from the mirror. The full side-by-side review UI lives in the main app — this panel surfaces the signal so you know when to open it.</p>' +
        '<div id="tl-review" style="display:flex;flex-direction:column;gap:8px"><div class="tl-cnt" style="margin:0">' + (collected ? 'Loading…' : 'Collect the vault first.') + '</div></div></div>' +
      '</div>';
  }
  async function loadReviewCounts() {
    var box = draw.querySelector('#tl-review'); if (!box) return;
    try {
      var r = await Promise.all([
        Fastt.get('/admin/vault-ai/duplicates').catch(function () { return {}; }),
        Fastt.get('/admin/vault-ai/disputes').catch(function () { return {}; }),
        Fastt.get('/admin/vault-ai/flags-review').catch(function () { return {}; }),
      ]);
      function n(o) { if (!o) return 0; if (Array.isArray(o.list)) return o.list.length; if (Array.isArray(o.clusters)) return o.clusters.length; if (typeof o.count === 'number') return o.count; return 0; }
      var rows = [
        ['Duplicates', n(r[0]), 'reupload clusters to prune'],
        ['Disagreements', n(r[1]), 'items the two vision passes read differently'],
        ['Flags to review', n(r[2]), 'exposure flags worth a human eye'],
      ];
      box.innerHTML = rows.map(function (x) {
        return '<div style="display:flex;align-items:center;gap:10px;padding:9px 11px;border:1px solid #262626;border-radius:8px;background:#171717">' +
          '<b style="color:#fff;font-size:13.5px">' + esc(x[0]) + '</b>' +
          '<span style="font-size:16px;font-weight:700;color:' + (x[1] ? '#e2a94e' : '#67d1ae') + '">' + fmtInt(x[1]) + '</span>' +
          '<span style="font-size:12px;color:#8a8a8a;margin-left:auto;text-align:right">' + esc(x[2]) + '</span></div>';
      }).join('');
      Fastt.staticBadge(box, 'REVIEW IN MAIN APP');
    } catch (e) { box.innerHTML = '<div class="tl-cnt" style="margin:0;color:#f0715f">Could not load review counts.</div>'; }
  }
  function jobLine(id) { return draw.querySelector('#tl-' + id); }
  async function runJob(kind) {
    if (jobs[kind]) return;
    var btn = draw.querySelector('[data-job="' + kind + '"]'), line = jobLine(kind);
    var cost = kind === 'describe' ? '\n\nThis spends LLM budget against today’s cap.' : '';
    if (!confirm('Start "' + (kind === 'describe' ? 'Describe all' : 'Harvest') + '" in the background?' + cost)) return;
    jobs[kind] = true; if (btn) { btn.disabled = true; btn.classList.add('busy'); }
    try {
      if (kind === 'describe') await Fastt.post('/admin/vault-ai/describe-all', { account_id: acct, prompt_version: 'v2' });
      else await Fastt.post('/admin/vault-ai/harvest-keywords', { account_id: acct });
    } catch (e) { jobs[kind] = false; if (btn) { btn.disabled = false; btn.classList.remove('busy'); } Fastt.oops(e); return; }
    var url = kind === 'describe' ? '/admin/vault-ai/describe-all/status' : '/admin/vault-ai/harvest-keywords/status';
    for (var i = 0; i < 400 && jobs[kind]; i++) {
      await new Promise(function (r) { setTimeout(r, 2500); });
      var st; try { st = await Fastt.get(url); } catch (e) { continue; }
      var p = st && st.progress;
      if (p && line) line.textContent = kind === 'describe'
        ? (p.done + '/' + p.total + (p.capped ? ' (cap hit)' : ''))
        : (p.done + '/' + p.total + ' · ' + (p.matches || 0) + ' hits');
      if (st && !st.running) break;
    }
    jobs[kind] = false; if (btn) { btn.disabled = false; btn.classList.remove('busy'); }
    if (line) line.textContent += ' · done';
    loadGrid(false);
  }
  if (toolsBtn) toolsBtn.addEventListener('click', function () {
    openDraw(toolsHtml());
    draw.querySelector('.vpd-x').addEventListener('click', closeDraw);
    draw.querySelectorAll('[data-job]').forEach(function (b) {
      b.addEventListener('click', function () { runJob(b.dataset.job); });
    });
    if (sum && sum.count > 0) loadReviewCounts();
  });
});
