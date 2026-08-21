Fastt.ready(async function () {
  'use strict';
  var acct = Fastt.account();
  var esc = Fastt.esc, fmtInt = Fastt.fmtInt;
  var PAGE = 40;

  function stripSet(id, txt, cls) {
    var el = document.getElementById(id);
    if (!el) return;
    el.innerHTML = '<i></i>' + esc(txt);
    if (cls !== undefined) el.className = 'fx-st' + (cls ? ' ' + cls : '');
  }
  function qs(o) {
    var p = [];
    for (var k in o) if (o[k] !== undefined && o[k] !== null && o[k] !== '')
      p.push(encodeURIComponent(k) + '=' + encodeURIComponent(o[k]));
    return p.join('&');
  }
  function dur(s) {
    s = Math.round(Number(s) || 0);
    if (!s) return '';
    return Math.floor(s / 60) + ':' + ('0' + (s % 60)).slice(-2);
  }
  // `kinds` / `tiers` come back as COUNT MAPS ({"photo":3,"video":1}) from
  // service/vault_scripts.py `_folder()` — not arrays. Render either shape.
  function countLabel(o) {
    if (!o) return '';
    if (Array.isArray(o)) return o.filter(Boolean).join('/');
    if (typeof o === 'object') {
      return Object.keys(o).filter(function (k) { return o[k]; })
        .sort(function (a, b) { return o[b] - o[a]; })
        .map(function (k) { return fmtInt(o[k]) + ' ' + k; }).join(', ');
    }
    return String(o);
  }
  // Paint an honest failure over a mock region instead of leaving the baked
  // demo markup on screen when a load rejects.
  function bail(el, msg, tall) {
    if (!el) return;
    el.innerHTML = '<div class="vm-tile new" style="grid-column:1/-1;' +
      (tall ? 'height:130px;' : 'min-height:74px;') + 'border-color:#5a3a3a">' +
      '<div style="color:#c98f8f">' + esc(msg) + '</div></div>';
    Fastt.staticBadge(el, 'UNAVAILABLE');
  }
  function why(e) {
    var m = (e && (e.message || e.detail || e.error)) || 'request failed';
    return String(m).slice(0, 160);
  }

  // ── no creator: strip the baked mockup rather than let it read as data ──
  if (!acct) {
    ['vm-st-sync', 'vm-st-items', 'vm-st-desc', 'vm-st-flag', 'vm-st-model']
      .forEach(function (id) { stripSet(id, 'No creator selected', ''); });
    var rail0 = document.getElementById('vm-rail');
    if (rail0) rail0.innerHTML = '<div class="vm-frow" style="cursor:default;color:#6a6a6a">No creator selected</div>';
    var grid0 = document.getElementById('vm-grid');
    if (grid0) grid0.innerHTML = '<div class="vm-tile new" style="grid-column:1/-1;height:130px">' +
      '<div>No creator selected — click the creator name (top-left) to sign in and pick one.</div></div>';
    var hero0 = document.querySelector('.vm-hero');
    if (hero0) Fastt.staticBadge(hero0, 'NO CREATOR — INERT');
    var org0 = document.getElementById('vm-organize');
    if (org0) org0.disabled = true;
    ['vm-run-sync', 'vm-run-desc', 'vm-run-flags', 'vm-run-dupes', 'vm-run-harv', 'vm-run-disp', 'vm-select',
     'vm-sc-scan', 'vm-sc-apply', 'vm-sc-folders', 'vm-sc-create']
      .forEach(function (id) { var b = document.getElementById(id); if (b) b.disabled = true; });
    var scst0 = document.getElementById('vm-sc-status');
    if (scst0) scst0.innerHTML = '<span class="fx-st"><i></i>No creator selected</span>';
    return;
  }

  // ══════════════════════════════════════════════════════════════
  // load: mirror summary + config + folders (+ OF-live fallback)
  // ══════════════════════════════════════════════════════════════
  var results = await Promise.all([
    Fastt.get('/admin/vault-ai/cache/summary'),
    Fastt.get('/admin/vault-ai/describe-all/plan'),
    Fastt.get('/admin/vault-ai/flags-all/status'),
    Fastt.get('/admin/account-ai-config/vault-ai'),
    Fastt.get('/admin/vault-ai/folders'),
    Fastt.get('/admin/vault-ai/of-folders'),
  ]);
  var sum = results[0], plan = results[1], flagsSt = results[2];
  var cfg = (results[3] && results[3].config) || {};
  var aiFolders = (results[4] && results[4].folders) || [];
  var ofFolders = (results[5] && results[5].list) || [];

  // The mirror is the fast path. When it is COLD (0 rows) the whole page used
  // to render "run a vault sync first" while OF itself has thousands of items
  // — so fall back to the live OF vault and say so, exactly like the real app.
  var SRC = (sum.count > 0) ? 'mirror' : 'live';
  var liveAll = null;   // /vault/lists `all` counter block
  if (SRC === 'live') {
    try {
      var ls = await Fastt.get('/api/of/v2/vault/lists', { limit: 50 }); // 50 is OF's max; 100 → 422
      liveAll = ls.all || null;
      ofFolders = (ls.list || []).map(function (f) {
        return {
          id: f.id, name: f.name, type: f.type,
          photosCount: f.photosCount || 0, videosCount: f.videosCount || 0,
          gifsCount: f.gifsCount || 0, audiosCount: f.audiosCount || 0,
          mediaCount: (f.photosCount || 0) + (f.videosCount || 0) + (f.gifsCount || 0) + (f.audiosCount || 0),
        };
      });
    } catch (e) { Fastt.oops(e); }
  }
  function liveTotal() {
    if (!liveAll) return 0;
    return (liveAll.photosCount || 0) + (liveAll.videosCount || 0) +
           (liveAll.gifsCount || 0) + (liveAll.audiosCount || 0);
  }
  var totalItems = SRC === 'live' ? liveTotal() : (sum.count || 0);

  // ── status strip (LIVE) ──────────────────────────────────────
  if (sum.running) stripSet('vm-st-sync', 'Mirror sync running…', 'ok');
  else if (sum.last_run && sum.last_run.finished_at)
    stripSet('vm-st-sync', 'Mirror synced ' + Fastt.fmtAgo(sum.last_run.finished_at), 'ok');
  else stripSet('vm-st-sync', 'Mirror never synced — reading OnlyFans live', 'warn');
  stripSet('vm-st-items', fmtInt(totalItems) + ' items' + (SRC === 'live' ? ' (OF live)' : ''));
  if (SRC === 'live') stripSet('vm-st-desc', 'described: needs the mirror', 'warn');
  else stripSet('vm-st-desc', fmtInt(Math.max(0, (plan.total || 0) - (plan.undescribed || 0))) +
    ' / ' + fmtInt(plan.total || 0) + ' described');
  // `coverage.flagged` is COVERAGE (stills that HAVE the exposure flags), not a
  // problem count — flags_coverage() in service/vault_scripts.py. Reading it as
  // "N flagged" glows amber exactly when coverage is healthy, so show the real
  // pair instead and warn on `!ready`.
  var cov = (flagsSt && flagsSt.coverage) || {};
  if (!cov.stills) stripSet('vm-st-flag', 'flags: no stills in the mirror yet', 'warn');
  else stripSet('vm-st-flag',
    'flags: ' + fmtInt(cov.flagged || 0) + '/' + fmtInt(cov.stills || 0) +
    ' · ' + fmtInt(cov.missing || 0) + ' missing', cov.ready ? 'ok' : 'warn');
  stripSet('vm-st-model', String((cfg.models && cfg.models.describe) || 'qwen3-vl-30b'));
  Fastt.liveBadge(document.getElementById('vm-status'));

  // ══════════════════════════════════════════════════════════════
  // folder rail
  // ══════════════════════════════════════════════════════════════
  var rail = document.getElementById('vm-rail');
  var FICO = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M4 7a2 2 0 0 1 2-2h4l2 2h6a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V7z"/></svg>';
  var AICO = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M12 3.5l1.7 4 4 1.7-4 1.7L12 15l-1.7-4L6.3 9.2l4-1.7z" stroke-linejoin="round"/></svg>';
  var GRID_ICO = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9"><rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/></svg>';
  var FA = '<span class="fa"><button data-act="ren" title="Rename folder">✎</button>' +
    '<button data-act="del" title="Delete folder">🗑</button></span>';
  function railHtml() {
    var h = '<div class="vm-frow on" data-all="1"><span class="ic">' + GRID_ICO + '</span>All<span class="c">' + fmtInt(totalItems) + '</span></div>';
    h += '<div class="vm-rail-sec frow-sec">OnlyFans folders' +
      '<button class="vm-rail-new" data-new-folder="1" title="New OnlyFans folder">+</button></div>';
    var shown = ofFolders.filter(function (f) { return (f.mediaCount || 0) > 0; });
    shown.sort(function (a, b) { return (b.mediaCount || 0) - (a.mediaCount || 0); });
    shown.forEach(function (f) {
      h += '<div class="vm-frow mgmt" data-of-id="' + esc(f.id) + '" data-name="' + esc(f.name || ('#' + f.id)) + '"><span class="ic">' + FICO + '</span>' +
        esc(f.name || ('#' + f.id)) + '<span class="c">' + fmtInt(f.mediaCount) + '</span>' + FA + '</div>';
    });
    if (!shown.length) h += '<div class="vm-frow" style="cursor:default;color:#6a6a6a">No OF folders with media — “+” to create one</div>';
    h += '<div class="vm-rail-sec">AI folders <span class="vm-sugg">SUGGESTED</span></div>';
    aiFolders.forEach(function (f) {
      h += '<div class="vm-frow" data-int-id="' + esc(f.id) + '"><span class="ic">' + AICO + '</span>' +
        esc(f.name) + '<span class="c">' + fmtInt(f.count) + '</span></div>';
    });
    if (!aiFolders.length) h += '<div class="vm-frow" style="cursor:default;color:#6a6a6a">' +
      (SRC === 'live' ? 'Need the mirror — sync first' : 'None yet — run “Organize with AI”') + '</div>';
    return h;
  }
  if (rail) {
    rail.innerHTML = railHtml();
    if (SRC === 'live') Fastt.staticBadge(rail, 'LIVE (OF)'); else Fastt.liveBadge(rail);
  }

  // ══════════════════════════════════════════════════════════════
  // item grid — one normalized shape from either source, paged
  // ══════════════════════════════════════════════════════════════
  var grid = document.getElementById('vm-grid');
  var gridState = { query: '', sort: 'newest', type: 'all', of_folder_id: null, internal_folder_id: null };
  var selectMode = false, selected = new Set();
  var page = { items: [], offset: 0, hasMore: false, loading: false, seq: 0, err: null };
  // OF's CDN urls are IP-signed. Same egress as the relay ⇒ they load straight
  // from the browser (fast). If not, everything re-renders through the relay's
  // own OF-CDN image proxy instead of showing broken tiles.
  var thumbMode = 'direct';
  var probed = false;

  function normLive(m) {
    var f = m.files || {};
    var th = (f.thumb && f.thumb.url) || (f.squarePreview && f.squarePreview.url) || '';
    var fu = (f.preview && f.preview.url) || (f.full && f.full.url) || th;
    var folders = (m.listStates || []).filter(function (s) { return s.hasMedia; })
      .map(function (s) { return s.name; });
    return { id: m.id, type: m.type, ofThumb: th, ofFull: fu, duration: m.duration || 0,
             createdAt: m.createdAt, folders: folders, ai: null, live: true };
  }
  function normMirror(m) {
    return { id: m.id, type: m.type, ofThumb: m._thumb, ofFull: null, duration: m.duration || 0,
             createdAt: m.createdAt, folders: [], ai: m._ai || {}, live: false };
  }
  function proxied(u, mid) {
    return '/admin/vault-ai/poster?' + qs({ account_id: acct, media_id: mid, i: 0, u: u });
  }
  function thumbOf(it) {
    if (!it.live) return it.ofThumb;
    if (!it.ofThumb) return '';
    return thumbMode === 'direct' ? it.ofThumb : proxied(it.ofThumb, it.id);
  }
  function fullOf(it) {
    if (!it.live) return '/admin/vault-ai/image?' + qs({ account_id: acct, media_id: it.id });
    if (!it.ofFull) return '';
    return thumbMode === 'direct' ? it.ofFull : proxied(it.ofFull, it.id);
  }

  // The exposure-flag regions the flags pass fills in (service/vault_ai_brief.py).
  var VIS_REGIONS = ['vulva_vis', 'breasts_vis', 'anus_vis'];
  var VIS_STATES = ['not_in_frame', 'covered', 'bare'];
  var OVER_KEYS = { breasts_vis: 'over_breasts', vulva_vis: 'over_vulva', anus_vis: 'over_anus' };
  function hasFlags(ai) {
    var f = (ai && ai.fields) || {};
    return VIS_REGIONS.every(function (r) { return VIS_STATES.indexOf(f[r]) >= 0; });
  }

  function tileHtml(it, idx) {
    var ai = it.ai || {};
    var badges = '';
    if (ai.describe_status === 'described') badges += '<span class="vm-b ok">✓</span>';
    if (ai.suggested_price_cents) badges += '<span class="vm-b pr">$</span>';
    if (hasFlags(ai)) badges += '<span class="vm-b fl">⚑</span>';
    var tag = '';
    if (it.type === 'video') tag = '<span class="vm-dur">▶ ' + (dur(it.duration) || 'video') + '</span>';
    else if (it.type === 'gif') tag = '<span class="vm-dur">GIF</span>';
    else if (it.type === 'audio') tag = '<span class="vm-dur">♪</span>';
    var th = thumbOf(it);
    var sub = (it.type || 'item') + (it.live && it.folders.length ? ' · ' + it.folders[0] : '');
    var isSel = selected.has(it.id);
    return '<div class="vm-tile' + (isSel ? ' sel' : '') + '" data-i="' + idx + '" data-id="' + esc(it.id) + '" style="' +
      (th ? 'background-image:url(\'' + esc(th) + '\');background-size:cover;background-position:center' : 'background:#1a1a1a') + '">' +
      '<div class="sh"></div>' +
      (selectMode ? '<span class="vm-check">' + (isSel ? '✓' : '') + '</span>' : '') +
      (badges ? '<span class="vm-bdg">' + badges + '</span>' : '') +
      '<span class="vm-meta">#' + esc(it.id) + ' <span class="mut">· ' + esc(sub) + '</span></span>' + tag + '</div>';
  }

  function renderGrid() {
    if (!grid) return;
    // The error state must WIN over both the baked mockup and the "no media"
    // copy: a failed read is not the same claim as an empty vault.
    if (page.err && !page.items.length) {
      bail(grid, (SRC === 'live'
        ? 'Could not read the OnlyFans vault for this creator — ' + page.err +
          '. A dead or missing OF session is the usual cause; re-capture it, or run “Sync vault (Collect all)” in Advanced to build the local mirror.'
        : 'Could not read the local mirror — ' + page.err + '.'), true);
      return;
    }
    if (!page.items.length) {
      var why = (gridState.query || gridState.of_folder_id != null || gridState.internal_folder_id != null || gridState.type !== 'all')
        ? 'Nothing matches this filter.'
        : (SRC === 'live'
            ? 'OnlyFans returned no vault media for this creator.'
            : 'No items in the local mirror yet — run “Sync vault (Collect all)” in Advanced.');
      grid.innerHTML = '<div class="vm-tile new" style="grid-column:1/-1;height:130px"><div>' + esc(why) + '</div></div>';
      return;
    }
    var html = page.items.map(tileHtml).join('');
    html += '<div class="vm-more" style="grid-column:1/-1">' +
      '<span>Showing ' + fmtInt(page.items.length) + (page.hasMore ? '' : ' of ' + fmtInt(page.items.length)) + ' items' +
      ' <span class="vm-src">· ' + (SRC === 'live' ? 'live from OnlyFans' : 'local mirror') + '</span></span>' +
      (page.hasMore ? '<button class="fx-btn ghost" id="vm-more">Load ' + PAGE + ' more</button>' : '') + '</div>';
    grid.innerHTML = html;
    var more = document.getElementById('vm-more');
    if (more) more.addEventListener('click', function () { loadGrid(true); });
  }

  async function fetchPage(offset) {
    if (SRC === 'live') {
      var p = { limit: PAGE, offset: offset, field: 'recent',
                sort: gridState.sort === 'oldest' ? 'asc' : 'desc' };
      if (gridState.type !== 'all') p.type = gridState.type;
      if (gridState.query) p.query = gridState.query;
      if (gridState.of_folder_id != null) p.list_id = gridState.of_folder_id;
      var out = await Fastt.get('/api/of/v2/vault/media', p);
      return { list: (out.list || []).map(normLive), hasMore: !!out.hasMore };
    }
    var p2 = { limit: PAGE, offset: offset, sort: gridState.sort };
    if (gridState.type !== 'all') p2.type = gridState.type;
    if (gridState.query) p2.query = gridState.query;
    if (gridState.of_folder_id != null) p2.of_folder_id = gridState.of_folder_id;
    if (gridState.internal_folder_id != null) p2.internal_folder_id = gridState.internal_folder_id;
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
      if (SRC === 'live' && !probed && page.items.length) probeThumb();
    } catch (e) { Fastt.oops(e); } finally { page.loading = false; }
  }

  function probeThumb() {
    var first = null;
    for (var i = 0; i < page.items.length; i++) if (page.items[i].ofThumb) { first = page.items[i]; break; }
    if (!first) return;
    probed = true;
    var img = new Image();
    img.onerror = function () { thumbMode = 'proxy'; renderGrid(); };
    img.src = first.ofThumb;
  }

  await loadGrid(false);
  var toolbar = document.querySelector('.vm-toolbar');
  if (toolbar) { if (SRC === 'live') Fastt.staticBadge(toolbar, 'LIVE (OF)'); else Fastt.liveBadge(toolbar); }

  if (rail) rail.addEventListener('click', function (ev) {
    var newBtn = ev.target.closest('[data-new-folder]');
    if (newBtn) { ev.stopPropagation(); createFolder(); return; }
    var act = ev.target.closest('.fa button');
    if (act) {
      ev.stopPropagation();
      var frow = act.closest('.vm-frow');
      var fid = Number(frow.dataset.ofId), fname = frow.dataset.name || ('#' + fid);
      if (act.dataset.act === 'ren') renameFolder(fid, fname); else deleteFolder(fid, fname);
      return;
    }
    var row = ev.target.closest('.vm-frow');
    if (!row || (!row.dataset.all && !row.dataset.ofId && !row.dataset.intId)) return;
    rail.querySelectorAll('.vm-frow').forEach(function (r) { r.classList.remove('on'); });
    row.classList.add('on');
    gridState.of_folder_id = row.dataset.ofId ? Number(row.dataset.ofId) : null;
    gridState.internal_folder_id = row.dataset.intId ? Number(row.dataset.intId) : null;
    loadGrid(false);
  });

  // ── OF folder management (real OF vault writes — click + confirm gated) ──
  function mapLiveFolder(f) {
    return { id: f.id, name: f.name, type: f.type,
      photosCount: f.photosCount || 0, videosCount: f.videosCount || 0,
      gifsCount: f.gifsCount || 0, audiosCount: f.audiosCount || 0,
      mediaCount: (f.photosCount || 0) + (f.videosCount || 0) + (f.gifsCount || 0) + (f.audiosCount || 0) };
  }
  async function reloadFolders() {
    try {
      if (SRC === 'live') {
        var ls = await Fastt.get('/api/of/v2/vault/lists', { limit: 50 });
        ofFolders = (ls.list || []).map(mapLiveFolder);
      } else {
        var f = await Fastt.get('/admin/vault-ai/of-folders');
        ofFolders = (f.list || []).map(mapLiveFolder);
      }
      var f2 = await Fastt.get('/admin/vault-ai/folders');
      aiFolders = (f2 && f2.folders) || [];
    } catch (e) { Fastt.oops(e); }
    if (rail) rail.innerHTML = railHtml();
  }
  async function createFolder(ids) {
    var name = prompt('New OnlyFans folder name:'); if (!name || !name.trim()) return;
    ids = (ids || []).filter(function (x) { return x != null; });
    try {
      var out = await Fastt.post('/admin/vault-ai/of-folders', { account_id: acct, name: name.trim(), media_ids: ids });
      Fastt.saved('Folder “' + (out.name || name.trim()) + '” created' + (out.added ? ' · ' + out.added + ' added' : ''));
      await reloadFolders();
    } catch (e) { Fastt.oops(e); }
  }
  async function renameFolder(fid, cur) {
    var name = prompt('Rename folder:', cur); if (!name || !name.trim() || name.trim() === cur) return;
    try {
      await Fastt.post('/admin/vault-ai/of-folders/' + fid + '/rename', { account_id: acct, name: name.trim() });
      Fastt.saved('Renamed to “' + name.trim() + '”');
      await reloadFolders();
    } catch (e) { Fastt.oops(e); }
  }
  async function deleteFolder(fid, name) {
    if (!confirm('Delete the OnlyFans folder “' + name + '”?\n\nThe media stays in your vault — only the folder itself is removed.')) return;
    try {
      await Fastt.del('/admin/vault-ai/of-folders/' + fid);
      Fastt.saved('Folder “' + name + '” deleted');
      if (gridState.of_folder_id === fid) { gridState.of_folder_id = null; loadGrid(false); }
      await reloadFolders();
    } catch (e) { Fastt.oops(e); }
  }

  // ── type filter tabs (works for both live + mirror sources) ──
  var typeSeg = document.getElementById('vm-type');
  if (typeSeg) typeSeg.addEventListener('click', function (ev) {
    var b = ev.target.closest('button[data-t]'); if (!b) return;
    gridState.type = b.dataset.t; loadGrid(false);
  });

  // ── select mode + bulk actions ──
  var selbar = document.getElementById('vm-selbar');
  var selectBtn = document.getElementById('vm-select');
  function updateSelbar() {
    if (!selbar) return;
    if (!selectMode || !selected.size) { selbar.style.display = 'none'; selbar.innerHTML = ''; return; }
    selbar.style.display = '';
    selbar.innerHTML =
      '<span class="n">' + fmtInt(selected.size) + ' selected</span>' +
      '<button class="fx-btn" id="vm-sel-new">+ New folder with these</button>' +
      '<select class="fx-select" id="vm-sel-add"><option value="">Add to folder…</option>' +
        ofFolders.slice().sort(function (a, b) { return (b.mediaCount || 0) - (a.mediaCount || 0); })
          .map(function (f) { return '<option value="' + esc(f.id) + '">' + esc(f.name || ('#' + f.id)) + '</option>'; }).join('') +
      '</select>' +
      '<button class="fx-btn ghost" id="vm-sel-desc">Describe selected</button>' +
      '<button class="fx-btn ghost" id="vm-sel-clear">Clear</button>';
    document.getElementById('vm-sel-new').addEventListener('click', function () { createFolder(Array.from(selected)); });
    document.getElementById('vm-sel-clear').addEventListener('click', function () {
      selected.clear(); renderGrid(); updateSelbar();
    });
    document.getElementById('vm-sel-add').addEventListener('change', async function () {
      var fid = Number(this.value); if (!fid) return;
      var ids = Array.from(selected);
      try {
        var out = await Fastt.post('/admin/vault-ai/of-folders/' + fid + '/add', { account_id: acct, media_ids: ids });
        Fastt.saved('Added ' + fmtInt(out.added || ids.length) + ' to the folder');
        selected.clear(); renderGrid(); updateSelbar(); reloadFolders();
      } catch (e) { Fastt.oops(e); this.value = ''; }
    });
    document.getElementById('vm-sel-desc').addEventListener('click', async function () {
      var ids = Array.from(selected);
      if (!confirm('Describe ' + ids.length + ' selected item' + (ids.length === 1 ? '' : 's') + ' now?\n\nThis spends vision-model budget (no fan is messaged). Each item is described and flagged, then becomes editable.')) return;
      var btn = this; btn.disabled = true;
      var ok = 0;
      for (var i = 0; i < ids.length; i++) {
        btn.textContent = 'Describing ' + (i + 1) + '/' + ids.length + '…';
        try { var r = await Fastt.post('/admin/vault-ai/describe', { account_id: acct, media_id: ids[i] }); if (r && r.ok !== false) ok++; }
        catch (e) { /* keep going */ }
      }
      Fastt.saved('Described ' + ok + '/' + ids.length + ' item' + (ids.length === 1 ? '' : 's'));
      selected.clear(); btn.disabled = false; btn.textContent = 'Describe selected';
      updateSelbar(); loadGrid(false);
    });
  }
  if (selectBtn) selectBtn.addEventListener('click', function () {
    selectMode = !selectMode; selected.clear();
    selectBtn.classList.toggle('on', selectMode);
    selectBtn.textContent = selectMode ? 'Done' : 'Select';
    renderGrid(); updateSelbar();
  });
  var q = document.getElementById('vm-q');
  if (q) q.addEventListener('input', Fastt.debounce(function () {
    gridState.query = q.value.trim(); loadGrid(false);
  }, 350));
  var sortSel = document.getElementById('vm-sort');
  if (sortSel) sortSel.addEventListener('change', function () {
    gridState.sort = /old/i.test(sortSel.value) ? 'oldest' : 'newest'; loadGrid(false);
  });

  // ══════════════════════════════════════════════════════════════
  // item drawer — the detail card the "Good to know" note promises
  // ══════════════════════════════════════════════════════════════
  var TIERS = ['safe', 'suggestive', 'explicit', 'graphic', 'unknown'];
  var back = null;

  function closeDrawer() { if (back) { back.remove(); back = null; } }

  function taxHtml(fields) {
    if (!fields || typeof fields !== 'object') return '';
    var rows = [];
    Object.keys(fields).forEach(function (k) {
      var v = fields[k];
      if (v === null || v === undefined || v === '' ) return;
      if (Array.isArray(v)) { if (!v.length) return; v = v.join(', '); }
      else if (typeof v === 'object') return;
      rows.push('<span><b>' + esc(k.replace(/_/g, ' ')) + '</b> ' + esc(String(v)) + '</span>');
    });
    if (!rows.length) return '';
    return '<div><div style="font-size:11px;font-weight:700;letter-spacing:.07em;text-transform:uppercase;color:#6a6a6a;margin-bottom:7px">What the AI read</div>' +
      '<div class="vm-tax">' + rows.join('') + '</div></div>';
  }

  function flagsHtml(ai, editable) {
    var f = (ai && ai.fields) || {};
    var locked = (ai && ai.locked_fields) || [];
    var rows = VIS_REGIONS.map(function (r) {
      var ok = OVER_KEYS[r];
      return '<div class="fx-field"><label>' + esc(r.replace('_vis', '')) +
        (locked.indexOf(r) >= 0 ? ' <span class="vm-lock">LOCKED</span>' : '') + '</label>' +
        '<select class="fx-select" id="vm-f-' + r + '" ' + (editable ? '' : 'disabled') + '>' +
          '<option value="">— not read —</option>' +
          VIS_STATES.map(function (st) {
            return '<option value="' + st + '"' + (f[r] === st ? ' selected' : '') + '>' + st.replace(/_/g, ' ') + '</option>';
          }).join('') + '</select>' +
        '<input class="fx-input" id="vm-f-' + ok + '" ' + (editable ? '' : 'disabled') +
          ' value="' + esc(f[ok] || '') + '" placeholder="what covers it (e.g. white tank top)" style="margin-top:6px;height:32px">' +
        '</div>';
    }).join('');
    return '<div><div style="font-size:11px;font-weight:700;letter-spacing:.07em;text-transform:uppercase;color:#6a6a6a;margin-bottom:7px">' +
      'Exposure flags <span style="text-transform:none;letter-spacing:0;font-weight:500">— these pick the folder, the price band and whether it may be mass-sent</span></div>' +
      '<div class="fx-grid3">' + rows + '</div></div>';
  }

  function openDrawer(idx) {
    var it = page.items[idx];
    if (!it) return;
    closeDrawer();
    var ai = it.ai || {};
    var locked = ai.locked_fields || [];
    var editable = !it.live;
    var full = fullOf(it);
    back = document.createElement('div');
    back.className = 'vm-back';
    back.innerHTML =
      '<div class="vm-dr">' +
        '<div class="vm-dr-l">' +
          '<div class="vm-dr-img" id="vm-dr-img" style="' + (full ? 'background-image:url(\'' + esc(full) + '\')' : '') + '"></div>' +
          '<div class="vm-dr-scrub" id="vm-dr-scrub">' +
            (it.type === 'video'
              ? (editable ? 'hover the frame to scrub · <b id="vm-dr-fr">frame 1</b>' : 'frame scrub needs the local mirror')
              : (it.type || 'photo')) + '</div>' +
        '</div>' +
        '<div class="vm-dr-r">' +
          '<div class="vm-dr-h">' +
            '<div><div class="t">#' + esc(it.id) + ' <span class="m">· ' + esc(it.type || 'item') +
              (it.duration ? ' · ' + esc(dur(it.duration)) : '') + '</span></div>' +
            '<div class="m">' + esc(it.folders && it.folders.length ? it.folders.join(' · ') : (it.createdAt ? Fastt.fmtDate(it.createdAt) : '')) + '</div></div>' +
            '<button class="vm-dr-x" id="vm-dr-x">✕</button>' +
          '</div>' +
          '<div class="vm-dr-b">' +
            (editable ? '' :
              '<div class="fx-note warn"><div>This item is live from OnlyFans and has no row in the local mirror yet, so there is nothing to describe, price or lock. Run <b>Sync vault (Collect all)</b> in Advanced, then the AI fields below become editable.</div></div>') +
            '<div class="fx-field"><label>Description ' + (locked.indexOf('description') >= 0 ? '<span class="vm-lock">LOCKED</span>' : '') + '</label>' +
              '<textarea class="fx-input" id="vm-f-desc" ' + (editable ? '' : 'disabled') + ' placeholder="' +
              (editable ? 'not described yet — run Describe all' : 'no local row') + '">' + esc(ai.description || '') + '</textarea></div>' +
            (ai.video_description ? '<div class="fx-field"><label>Video read</label><textarea class="fx-input" disabled>' + esc(ai.video_description) + '</textarea></div>' : '') +
            '<div class="fx-grid2">' +
              '<div class="fx-field"><label>Tags ' + (locked.indexOf('tags') >= 0 ? '<span class="vm-lock">LOCKED</span>' : '') + '</label>' +
                '<input class="fx-input" id="vm-f-tags" ' + (editable ? '' : 'disabled') + ' value="' + esc((ai.tags || []).join(', ')) + '" placeholder="comma separated"></div>' +
              '<div class="fx-field"><label>Explicitness tier ' + (locked.indexOf('explicitness_tier') >= 0 ? '<span class="vm-lock">LOCKED</span>' : '') + '</label>' +
                '<select class="fx-select" id="vm-f-tier" ' + (editable ? '' : 'disabled') + '>' +
                  '<option value="">— unset —</option>' +
                  TIERS.map(function (t) {
                    return '<option value="' + t + '"' + (ai.explicitness_tier === t ? ' selected' : '') + '>' +
                      ((cfg.tier_labels && cfg.tier_labels[t]) || t) + '</option>';
                  }).join('') + '</select></div>' +
            '</div>' +
            '<div class="fx-grid2">' +
              '<div class="fx-field"><label>PPV price ' + (locked.indexOf('suggested_price_cents') >= 0 ? '<span class="vm-lock">LOCKED</span>' : '') + '</label>' +
                '<div class="fx-unit" data-unit="$"><input class="fx-input" id="vm-f-price" ' + (editable ? '' : 'disabled') +
                ' value="' + (ai.suggested_price_cents ? (ai.suggested_price_cents / 100).toFixed(2) : '') + '" placeholder="blank = no price"></div></div>' +
              '<div class="fx-field"><label>Story / tip flags</label>' +
                '<input class="fx-input" disabled value="' +
                esc([ai.story_suitable ? 'story-safe' : '', ai.tip_vault_flag ? 'tip-vault' : '', ai.review_state ? ('review: ' + ai.review_state) : '']
                  .filter(Boolean).join(' · ') || '—') + '"></div>' +
            '</div>' +
            '<div class="fx-field"><label>Suggested caption ' + (locked.indexOf('suggested_caption') >= 0 ? '<span class="vm-lock">LOCKED</span>' : '') + '</label>' +
              '<textarea class="fx-input" id="vm-f-cap" ' + (editable ? '' : 'disabled') + ' style="min-height:56px">' + esc(ai.suggested_caption || '') + '</textarea></div>' +
            '<div class="fx-field"><label>Selling script ' + (locked.indexOf('suggested_script') >= 0 ? '<span class="vm-lock">LOCKED</span>' : '') + '</label>' +
              '<textarea class="fx-input" id="vm-f-script" ' + (editable ? '' : 'disabled') + ' style="min-height:56px">' + esc(ai.suggested_script || '') + '</textarea></div>' +
            flagsHtml(ai, editable) +
            taxHtml(ai.fields) +
            '<div class="fx-note"><div>Anything you save here is written as an operator override <b>and locked</b> — a later describe or flags re-run skips those fields, so your correction is never overwritten.</div></div>' +
          '</div>' +
          '<div class="vm-dr-f">' +
            (full ? '<a class="fx-btn ghost" href="' + esc(full) + '" target="_blank" rel="noopener">Open full frame</a>' : '') +
            '<button class="fx-btn ghost" id="vm-dr-hide" style="color:#f0715f;border-color:rgba(240,113,95,.4)">Remove from vault…</button>' +
            '<button class="fx-btn" id="vm-dr-save" style="margin-left:auto"' + (editable ? '' : ' disabled') + '>Save &amp; lock</button>' +
          '</div>' +
        '</div>' +
      '</div>';
    document.body.appendChild(back);
    back.addEventListener('click', function (e) { if (e.target === back) closeDrawer(); });
    document.getElementById('vm-dr-x').addEventListener('click', closeDrawer);

    // video hover-scrub — only where cached poster frames can exist (mirror)
    if (it.type === 'video' && editable) {
      var imgEl = document.getElementById('vm-dr-img');
      var frEl = document.getElementById('vm-dr-fr');
      var probe = new Image();
      probe.onload = function () {
        var last = -1;
        imgEl.addEventListener('mousemove', function (ev) {
          var r = imgEl.getBoundingClientRect();
          var i = Math.max(0, Math.min(5, Math.floor(((ev.clientX - r.left) / r.width) * 6)));
          if (i === last) return;
          last = i;
          imgEl.style.backgroundImage = "url('" + ('/admin/vault-ai/poster?' + qs({ account_id: acct, media_id: it.id, i: i })) + "')";
          if (frEl) frEl.textContent = 'frame ' + (i + 1);
        });
        imgEl.addEventListener('mouseleave', function () {
          last = -1;
          imgEl.style.backgroundImage = full ? "url('" + full + "')" : '';
          if (frEl) frEl.textContent = 'frame 1';
        });
      };
      probe.onerror = function () {
        var s = document.getElementById('vm-dr-scrub');
        if (s) s.textContent = 'no cached poster frames for this clip';
      };
      probe.src = '/admin/vault-ai/poster?' + qs({ account_id: acct, media_id: it.id, i: 0 });
    }

    var saveBtn = document.getElementById('vm-dr-save');
    if (saveBtn && editable) saveBtn.addEventListener('click', async function () {
      var fields = {};
      var v;
      v = document.getElementById('vm-f-desc').value.trim();
      if (v !== (ai.description || '')) fields.description = v;
      v = document.getElementById('vm-f-tags').value.trim();
      if (v !== (ai.tags || []).join(', ')) fields.tags = v;
      v = document.getElementById('vm-f-tier').value;
      if (v !== (ai.explicitness_tier || '')) fields.explicitness_tier = v || null;
      v = document.getElementById('vm-f-price').value.trim();
      var cents = v === '' ? null : Math.max(0, Math.round(parseFloat(v) * 100));
      if (cents !== (ai.suggested_price_cents == null ? null : ai.suggested_price_cents)) fields.suggested_price_cents = cents;
      v = document.getElementById('vm-f-cap').value.trim();
      if (v !== (ai.suggested_caption || '')) fields.suggested_caption = v;
      v = document.getElementById('vm-f-script').value.trim();
      if (v !== (ai.suggested_script || '')) fields.suggested_script = v;
      var af = ai.fields || {};
      VIS_REGIONS.forEach(function (r) {
        var sel = document.getElementById('vm-f-' + r);
        // The API only accepts a real state here — "— not read —" means leave it alone.
        if (sel && sel.value && sel.value !== (af[r] || '')) fields[r] = sel.value;
        var ok = OVER_KEYS[r];
        var inp = document.getElementById('vm-f-' + ok);
        if (inp && inp.value.trim() !== (af[ok] || '')) fields[ok] = inp.value.trim();
      });
      if (!Object.keys(fields).length) { Fastt.toast('Nothing changed.'); return; }
      saveBtn.disabled = true;
      try {
        var out = await Fastt.patch('/admin/vault-ai/items/' + it.id, { account_id: acct, fields: fields });
        var row = (out && out.item) || {};
        it.ai = Object.assign({}, ai, {
          description: row.description, tags: row.tags || [], explicitness_tier: row.explicitness_tier,
          suggested_caption: row.suggested_caption, suggested_script: row.suggested_script,
          suggested_price_cents: row.suggested_price_cents, describe_status: row.describe_status,
          locked_fields: out.locked || [],
        });
        var nf = Object.assign({}, ai.fields || {});
        VIS_REGIONS.forEach(function (r) {
          if (fields[r] !== undefined) nf[r] = fields[r];
          var ok = OVER_KEYS[r];
          if (fields[ok] !== undefined) nf[ok] = fields[ok];
        });
        it.ai.fields = nf;
        Fastt.saved('Saved + locked: ' + Object.keys(fields).join(', '));
        renderGrid();
        closeDrawer();
      } catch (e) { Fastt.oops(e); saveBtn.disabled = false; }
    });

    var hideBtn = document.getElementById('vm-dr-hide');
    if (hideBtn) hideBtn.addEventListener('click', async function () {
      if (!confirm('Remove #' + it.id + ' from the REAL OnlyFans vault?\n\nOF has no unhide — this cannot be reversed from here. ' +
        'Anything already attached to a sent PPV or a live post keeps working.')) return;
      hideBtn.disabled = true;
      try {
        await Fastt.post('/admin/vault-ai/items/' + it.id + '/hide', { account_id: acct });
        Fastt.saved('#' + it.id + ' removed from the vault');
        page.items = page.items.filter(function (x) { return x.id !== it.id; });
        renderGrid();
        closeDrawer();
      } catch (e) { Fastt.oops(e); hideBtn.disabled = false; }
    });
  }

  if (grid) grid.addEventListener('click', function (ev) {
    if (ev.target.closest('#vm-more')) return;
    var tile = ev.target.closest('.vm-tile');
    if (!tile || tile.dataset.i === undefined) return;
    if (selectMode) {
      var id = Number(tile.dataset.id);
      if (selected.has(id)) selected.delete(id); else selected.add(id);
      var on = selected.has(id);
      tile.classList.toggle('sel', on);
      var ck = tile.querySelector('.vm-check'); if (ck) ck.textContent = on ? '✓' : '';
      updateSelbar();
      return;
    }
    openDrawer(Number(tile.dataset.i));
  });
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape') closeDrawer(); });

  // ══════════════════════════════════════════════════════════════
  // hero: AI folder plan — free preview, then an explicit confirm
  // ══════════════════════════════════════════════════════════════
  var hero = document.querySelector('.vm-hero');
  if (hero) Fastt.liveBadge(hero);
  var organize = document.getElementById('vm-organize');
  if (organize) organize.addEventListener('click', async function () {
    organize.disabled = true;
    var fp;
    try { fp = await Fastt.get('/admin/vault-ai/folder-plan', { keep: 2 }); }
    catch (e) { Fastt.oops(e); organize.disabled = false; return; }
    organize.disabled = false;
    var folders = fp.folders || [], s = fp.summary || {}, fl = fp.flags || {};
    var body;
    if (!folders.length) {
      body = '<div class="fx-note warn"><div>Nothing to plan yet — ' +
        (totalItems === 0 ? 'the vault looks empty.' :
         (sum.count === 0 ? 'the local mirror is empty. Run <b>Sync vault (Collect all)</b> in Advanced first, then <b>Describe all</b>.' :
          'no shoots were found in ' + fmtInt(sum.count) + ' mirrored items (' + fmtInt(fp.shoots_found || 0) + ' shoots, ' +
          fmtInt(fl.missing || 0) + ' stills still missing exposure flags). Run <b>Describe all</b> and the <b>Flags sweep</b>, then try again.')) +
        '</div></div>';
    } else {
      body = folders.map(function (f) {
        var lane = (f.lane || f.source || '').toString();
        return '<div class="vm-plan-row">' +
          '<div style="min-width:0;flex:1"><div class="pn">' + esc(f.name) + '</div>' +
          '<div class="pm">' + esc([f.purpose, countLabel(f.kinds), countLabel(f.tiers), f.outfit,
            f.closes_on_own ? fmtInt(f.closes_on_own) + ' close on their own' : '',
            f.mixed ? 'mixed set' : ''].filter(Boolean).join(' · ')) + '</div></div>' +
          (lane ? '<span class="vm-lane">' + esc(lane) + '</span>' : '') +
          '<span class="pc">' + fmtInt(f.size || (f.items || []).length) + '</span></div>';
      }).join('');
    }
    var modal = document.createElement('div');
    modal.className = 'vm-back';
    modal.innerHTML = '<div class="vm-dr" style="width:720px;flex-direction:column">' +
      '<div class="vm-dr-h"><div><div class="t">Build AI folders</div>' +
        '<div class="m">' + fmtInt(s.folders || 0) + ' folders · ' + fmtInt(s.unique_media || 0) + ' unique media · ' +
        fmtInt(s.memberships || 0) + ' memberships · ' + fmtInt(s.scripts || 0) + ' scripts</div></div>' +
        '<button class="vm-dr-x" id="vm-pl-x">✕</button></div>' +
      '<div class="vm-dr-b">' + body +
        '<div class="fx-note"><div>Preview only — nothing has been created. Applying writes <b>internal</b> folders in fastt; your real OnlyFans folders are not touched (<code>mirror_to_of</code> stays off).</div></div>' +
      '</div>' +
      '<div class="vm-dr-f"><span class="vm-src">keep ' + esc(fp.keep) + ' per shoot · flags ready: ' + (fl.ready ? 'yes' : 'no') + '</span>' +
        '<button class="fx-btn" id="vm-pl-go" style="margin-left:auto"' + (folders.length ? '' : ' disabled') + '>Create ' + fmtInt(folders.length) + ' folders</button></div>' +
      '</div>';
    document.body.appendChild(modal);
    modal.addEventListener('click', function (e) { if (e.target === modal) modal.remove(); });
    modal.querySelector('#vm-pl-x').addEventListener('click', function () { modal.remove(); });
    var go = modal.querySelector('#vm-pl-go');
    if (go && folders.length) go.addEventListener('click', async function () {
      if (!confirm('Create ' + folders.length + ' internal AI folders from this plan?\n\nOnly fastt-side folders are written — your OnlyFans vault is not modified.')) return;
      go.disabled = true;
      try {
        var out = await Fastt.post('/admin/vault-ai/folder-plan/apply',
          { account_id: acct, keep: fp.keep, confirm: true, mirror_to_of: false });
        Fastt.saved('Created ' + fmtInt(out.new || 0) + ' new · reused ' + fmtInt(out.reused || 0) + ' · ' + fmtInt(out.items || 0) + ' memberships');
        modal.remove();
        var f2 = await Fastt.get('/admin/vault-ai/folders');
        aiFolders = (f2 && f2.folders) || [];
        if (rail) rail.innerHTML = railHtml();
      } catch (e) { Fastt.oops(e); go.disabled = false; }
    });
  });

  // ══════════════════════════════════════════════════════════════
  // advanced jobs (LIVE, explicit click + confirm) with polling
  // ══════════════════════════════════════════════════════════════
  function setProg(id, frac) {
    var el = document.getElementById(id);
    if (!el) return;
    if (frac == null) { el.style.display = 'none'; return; }
    el.style.display = '';
    el.querySelector('i').style.width = Math.max(0, Math.min(100, frac * 100)) + '%';
    el.classList.toggle('ok', frac >= 1);
  }

  // ---- collect / sync ----
  var syncD = document.getElementById('vm-job-sync-d');
  var SYNC_BASE = 'Mirrors your whole OnlyFans vault locally so browsing, search and every AI job are instant. Read-only sweep of OF — nothing is moved, sent or posted.';
  function syncText(run) {
    if (!run) return SYNC_BASE + ' Never run for this creator.';
    var bits = [];
    if (run.phase) bits.push('phase ' + run.phase);
    if (run.pages_done) bits.push(fmtInt(run.pages_done) + ' pages');
    if (run.total_seen) bits.push(fmtInt(run.total_seen) + ' seen');
    if (run.upserted) bits.push(fmtInt(run.upserted) + ' mirrored');
    if (run.warm_total) bits.push(fmtInt(run.warmed || 0) + '/' + fmtInt(run.warm_total) + ' thumbs warmed');
    if (run.running) return 'Sync running — ' + (bits.join(' · ') || 'starting…');
    if (run.error) return 'Last sync FAILED: ' + String(run.error).slice(0, 160);
    return SYNC_BASE + ' Last run ' + Fastt.fmtAgo(run.finished_at || run.started_at) +
      (bits.length ? ' — ' + bits.join(' · ') : '');
  }
  var syncTimer = null;
  async function pollCollect() {
    try {
      var st = await Fastt.get('/admin/vault-ai/collect/status');
      var run = st.run;
      if (syncD) syncD.textContent = syncText(run);
      if (run && run.warm_total) setProg('vm-sync-prog', (run.warmed || 0) / run.warm_total);
      else setProg('vm-sync-prog', null);
      if (run && run.running) return true;
      if (syncTimer) { clearInterval(syncTimer); syncTimer = null; }
      var btn = document.getElementById('vm-run-sync');
      if (btn) { btn.disabled = false; btn.textContent = 'Sync'; }
      return false;
    } catch (e) { if (syncTimer) { clearInterval(syncTimer); syncTimer = null; } return false; }
  }
  function watchCollect() {
    if (syncTimer) return;
    var btn = document.getElementById('vm-run-sync');
    if (btn) { btn.disabled = true; btn.textContent = 'Syncing…'; }
    syncTimer = setInterval(pollCollect, 2500);
  }
  await pollCollect();
  if (sum.running) watchCollect();
  var runSync = document.getElementById('vm-run-sync');
  if (runSync) runSync.addEventListener('click', async function () {
    if (!confirm('Sync the whole OnlyFans vault into the local mirror now?\n\n' +
      'This is a READ-ONLY sweep of OnlyFans — no fan is messaged, nothing is posted, nothing is moved. ' +
      'It can take a few minutes on a large vault.')) return;
    try {
      await Fastt.api('/admin/vault-ai/collect', { method: 'POST' });
      Fastt.saved('Vault sync started');
      watchCollect();
    } catch (e) { Fastt.oops(e); }
  });

  // ---- describe-all ----
  var descD = document.getElementById('vm-job-desc-d');
  var DESC_BASE = 'Vision reads every new or changed item and writes description, tags and tier. ';
  if (descD) descD.textContent = DESC_BASE + fmtInt(plan.undescribed || 0) + ' undescribed · ' +
    fmtInt(plan.restage || 0) + ' stale for prompt ' + (plan.prompt_version || 'v2') + '.';
  var descTimer = null;
  async function pollDescribe() {
    try {
      var st = await Fastt.get('/admin/vault-ai/describe-all/status');
      var pr = st.progress || null;
      if (pr && descD) descD.textContent = 'Describe sweep ' + (st.running ? 'running' : 'finished') + ' — ' +
        fmtInt(pr.done || 0) + '/' + fmtInt(pr.total || 0) + ' done' +
        (pr.needs_review ? ' · ' + fmtInt(pr.needs_review) + ' need review' : '') +
        (pr.capped ? ' · budget-capped' : '');
      if (pr && pr.total) setProg('vm-desc-prog', (pr.done || 0) / pr.total); else setProg('vm-desc-prog', null);
      if (st.running) return true;
      if (descTimer) { clearInterval(descTimer); descTimer = null; }
      return false;
    } catch (e) { if (descTimer) { clearInterval(descTimer); descTimer = null; } return false; }
  }
  function watchDescribe() { if (!descTimer) descTimer = setInterval(pollDescribe, 3000); }
  var runDesc = document.getElementById('vm-run-desc');
  if (runDesc) runDesc.addEventListener('click', async function () {
    if (!confirm('Run a describe-all sweep now? This spends vision-model budget (no fan is messaged).')) return;
    try {
      var r = await Fastt.post('/admin/vault-ai/describe-all', { account_id: acct });
      Fastt.saved('Describe sweep ' + (r.status || 'started'));
      watchDescribe();
    } catch (e) { Fastt.oops(e); }
  });

  // ---- flags sweep ----
  var flagsD = document.getElementById('vm-job-flags-d');
  var FLAGS_BASE = 'Re-checks flagged items and names the garment instead of judging it. Runs in the background — the page stays usable while it works.';
  function flagsText(st) {
    var pr = st && st.progress;
    if (!pr) return FLAGS_BASE;
    var bits = [fmtInt(pr.done || 0) + '/' + fmtInt(pr.total || 0) + ' done'];
    if (pr.failed) bits.push(fmtInt(pr.failed) + ' failed');
    if (pr.capped) bits.push('budget-capped');
    if (pr.cost_millicents) bits.push('$' + (pr.cost_millicents / 100000).toFixed(4));
    if (pr.error) bits.push('error: ' + String(pr.error).slice(0, 90));
    return 'Flags sweep ' + (st.running ? 'running' : 'finished') + ' — ' + bits.join(' · ');
  }
  var flagsTimer = null;
  async function pollFlags() {
    try {
      var st = await Fastt.get('/admin/vault-ai/flags-all/status');
      if (flagsD) flagsD.textContent = flagsText(st);
      var pr = st.progress;
      if (pr && pr.total) setProg('vm-flags-prog', (pr.done || 0) / pr.total); else setProg('vm-flags-prog', null);
      var c = st.coverage || {};
      if (c.stills) stripSet('vm-st-flag', 'flags: ' + fmtInt(c.flagged || 0) + '/' + fmtInt(c.stills) +
        ' · ' + fmtInt(c.missing || 0) + ' missing', c.ready ? 'ok' : 'warn');
      if (st.running) return true;
      if (flagsTimer) { clearInterval(flagsTimer); flagsTimer = null; }
      return false;
    } catch (e) { if (flagsTimer) { clearInterval(flagsTimer); flagsTimer = null; } return false; }
  }
  function watchFlags() { if (!flagsTimer) flagsTimer = setInterval(pollFlags, 3000); }
  if (flagsSt && flagsSt.progress) { if (flagsD) flagsD.textContent = flagsText(flagsSt); }
  if (flagsSt && flagsSt.running) watchFlags();
  var runFlags = document.getElementById('vm-run-flags');
  if (runFlags) runFlags.addEventListener('click', async function () {
    if (!confirm('Run the flags sweep now? This spends vision-model budget (no fan is messaged).')) return;
    try {
      var r = await Fastt.post('/admin/vault-ai/flags-all', { account_id: acct });
      Fastt.saved('Flags sweep ' + (r.status || 'started') + ' · ' + fmtInt(r.candidates || 0) + ' candidates');
      watchFlags();
    } catch (e) { Fastt.oops(e); }
  });

  // ---- duplicates (read-only scan) ----
  var dupesD = document.getElementById('vm-job-dupes-d');
  var runDupes = document.getElementById('vm-run-dupes');
  if (runDupes) runDupes.addEventListener('click', async function () {
    try {
      var r = await Fastt.get('/admin/vault-ai/duplicates');
      var pct = r.scanned ? Math.round(1000 * (r.removable || 0) / r.scanned) / 10 : 0;
      if (dupesD) dupesD.textContent = 'Spots re-uploads of media already in the vault (' + pct + '% here — ' +
        fmtInt(r.removable || 0) + ' removable in ' + fmtInt(r.sets || 0) + ' sets). Review them on the Review page.';
      Fastt.toast(fmtInt(r.sets || 0) + ' duplicate sets · ' + fmtInt(r.removable || 0) + ' removable copies', 'ok');
    } catch (e) { Fastt.oops(e); }
  });

  // ---- harvest OF keyword search (background) ----
  var harvD = document.getElementById('vm-job-harv-d');
  var harvTimer = null;
  async function pollHarvest() {
    try {
      var st = await Fastt.get('/admin/vault-ai/harvest-keywords/status');
      var pr = st.progress;
      if (pr && harvD) harvD.textContent = 'Harvest ' + (st.running ? 'running' : 'finished') + ' — ' +
        fmtInt(pr.done || 0) + '/' + fmtInt(pr.total || 0) + ' keywords · ' + fmtInt(pr.matches || 0) + ' hits folded into search';
      if (pr && pr.total) setProg('vm-harv-prog', (pr.done || 0) / pr.total); else setProg('vm-harv-prog', null);
      if (st.running) return true;
      if (harvTimer) { clearInterval(harvTimer); harvTimer = null; }
      var b = document.getElementById('vm-run-harv'); if (b) { b.disabled = false; b.textContent = 'Run'; }
      return false;
    } catch (e) { if (harvTimer) { clearInterval(harvTimer); harvTimer = null; } return false; }
  }
  function watchHarvest() {
    if (harvTimer) return;
    var b = document.getElementById('vm-run-harv'); if (b) { b.disabled = true; b.textContent = 'Harvesting…'; }
    harvTimer = setInterval(pollHarvest, 2500);
  }
  await pollHarvest();
  var runHarv = document.getElementById('vm-run-harv');
  if (runHarv) runHarv.addEventListener('click', async function () {
    if (!confirm('Harvest OnlyFans search across ~50 selling keywords now?\n\nRead-only sweep of OnlyFans — no fan is messaged, nothing is posted. It folds the hits into local search.')) return;
    try {
      var r = await Fastt.post('/admin/vault-ai/harvest-keywords', { account_id: acct });
      Fastt.saved('Harvest started · ' + fmtInt(r.keywords || 0) + ' keywords');
      watchHarvest();
    } catch (e) { Fastt.oops(e); }
  });

  // ---- fix disagreements (disputes) ----
  var dispD = document.getElementById('vm-job-disp-d');
  var DISP_BASE = dispD ? dispD.textContent : '';
  var dispData = null;
  async function loadDisputes() {
    try { dispData = await Fastt.get('/admin/vault-ai/disputes'); }
    catch (e) { dispData = null; return; }
    var n = dispData.count || 0, checked = dispData.checked || 0;
    if (dispD) dispD.textContent = DISP_BASE + ' ' + (checked === 0
      ? 'Needs the mirror — run Sync + Describe first.'
      : (n === 0 ? 'Checked ' + fmtInt(checked) + ' described items — no disagreements.'
                 : fmtInt(n) + ' of ' + fmtInt(checked) + ' checked items disagree.'));
    var b = document.getElementById('vm-run-disp');
    if (b) b.textContent = n > 0 ? ('Review ' + fmtInt(n) + '…') : 'Review…';
  }
  function fieldSummary(obj) {
    if (!obj || !Object.keys(obj).length) return 'confirm only (no field change)';
    return Object.keys(obj).map(function (k) {
      return k.replace(/_/g, ' ') + ' → ' + (obj[k] === null ? '—' : obj[k]);
    }).join(', ');
  }
  function openDisputes() {
    var rows = (dispData && dispData.disputes) || [];
    var body;
    if (!rows.length) {
      body = '<div class="fx-note"><div>' + ((dispData && dispData.checked)
        ? 'No disagreements in ' + fmtInt(dispData.checked) + ' described items — the describe pass and the flags pass agree.'
        : 'Nothing to check yet — the local mirror has no described items. Run <b>Sync vault (Collect all)</b>, then <b>Describe all</b> first.') + '</div></div>';
    } else {
      body = rows.map(function (d) {
        var pf = (d.propose && d.propose.flags) || {}, pd = (d.propose && d.propose.describe) || {};
        return '<div class="vm-plan-row" style="align-items:flex-start;flex-direction:column;gap:8px" data-mid="' + d.media_id + '">' +
          '<div style="display:flex;align-items:center;gap:8px;width:100%"><div class="pn">#' + esc(d.media_id) +
            ' <span style="color:#8a8a8a;font-weight:500">· ' + esc(d.kind) + '</span></div>' +
            '<span class="vm-lane rev" style="margin-left:auto">' + esc((d.reasons || []).join(', ') || 'contradiction') + '</span></div>' +
          (d.description ? '<div class="pm">' + esc(d.description) + '</div>' : '') +
          '<div style="display:flex;gap:8px;flex-wrap:wrap;width:100%">' +
            '<button class="fx-btn ghost dsp-fix" data-mid="' + d.media_id + '" data-branch="flags">Trust flags: ' + esc(fieldSummary(pf)) + '</button>' +
            '<button class="fx-btn ghost dsp-fix" data-mid="' + d.media_id + '" data-branch="describe">Trust describe: ' + esc(fieldSummary(pd)) + '</button>' +
          '</div></div>';
      }).join('');
    }
    var modal = document.createElement('div');
    modal.className = 'vm-back';
    modal.innerHTML = '<div class="vm-dr" style="width:720px;flex-direction:column">' +
      '<div class="vm-dr-h"><div><div class="t">Fix disagreements</div>' +
        '<div class="m">' + fmtInt(rows.length) + ' to settle · ' + fmtInt((dispData && dispData.checked) || 0) + ' described items checked</div></div>' +
        '<button class="vm-dr-x" id="vm-dsp-x">✕</button></div>' +
      '<div class="vm-dr-b">' + body +
        '<div class="fx-note"><div>Each item shows the two ways the two AI readings could be reconciled. Pick the one that matches the picture — your choice is written to the item and <b>locked</b>, so a re-run never overwrites it.</div></div>' +
      '</div></div>';
    document.body.appendChild(modal);
    modal.addEventListener('click', function (e) { if (e.target === modal) modal.remove(); });
    modal.querySelector('#vm-dsp-x').addEventListener('click', function () { modal.remove(); });
    modal.querySelectorAll('.dsp-fix').forEach(function (btn) {
      btn.addEventListener('click', async function () {
        var mid = Number(btn.dataset.mid), branch = btn.dataset.branch;
        var d = rows.find(function (x) { return x.media_id === mid; });
        var vals = (d && d.propose && d.propose[branch]) || {};
        if (!Object.keys(vals).length && !confirm('This branch has no field change — mark #' + mid + ' resolved as-is?')) return;
        btn.disabled = true;
        try {
          await Fastt.post('/admin/vault-ai/disputes/resolve', { account_id: acct, media_id: mid, values: vals });
          Fastt.saved('#' + mid + ' resolved + locked');
          var rowEl = modal.querySelector('.vm-plan-row[data-mid="' + mid + '"]'); if (rowEl) rowEl.remove();
          rows = rows.filter(function (x) { return x.media_id !== mid; });
          loadDisputes();
        } catch (e) { Fastt.oops(e); btn.disabled = false; }
      });
    });
  }
  await loadDisputes();
  var runDisp = document.getElementById('vm-run-disp');
  if (runDisp) runDisp.addEventListener('click', async function () { await loadDisputes(); openDisputes(); });

  var jobsGroup = document.querySelector('#vm-adv .fx-adv-group > h4');
  if (jobsGroup) Fastt.liveBadge(jobsGroup);

  // ══════════════════════════════════════════════════════════════
  // recovered scripts (batch detection · direction scoring)
  // ══════════════════════════════════════════════════════════════
  var scGap = document.getElementById('vm-sc-gap');
  var scVids = document.getElementById('vm-sc-vids');
  var scList = document.getElementById('vm-sc-list');
  var scStatus = document.getElementById('vm-sc-status');
  function scParams() {
    return { gap_seconds: Number(scGap && scGap.value) || 900,
             videos_only: !!(scVids && scVids.classList.contains('on')) };
  }
  function laneCls(dir) {
    if (dir === 'reversed') return 'vm-lane rev';
    if (dir === 'forward') return 'vm-lane';
    return 'vm-lane flat';
  }
  async function loadScripts() {
    if (!scList) return;
    scStatus.innerHTML = '<span class="fx-st"><i></i>Scanning batches…</span>';
    try {
      var r = await Fastt.get('/admin/vault-ai/scripts', scParams());
      var s = r.summary || {};
      scStatus.innerHTML =
        '<span class="fx-st"><i></i>' + fmtInt(r.items || 0) + ' items scanned</span>' +
        '<span class="fx-st"><i></i>' + fmtInt(s.batches || 0) + ' batches</span>' +
        '<span class="fx-st ok"><i></i>' + fmtInt(s.forward || 0) + ' forward</span>' +
        '<span class="fx-st warn"><i></i>' + fmtInt(s.reversed || 0) + ' reversed</span>' +
        '<span class="fx-st"><i></i>' + fmtInt(s.flat || 0) + ' flat</span>' +
        (s.unscoreable_items ? '<span class="fx-st err"><i></i>' + fmtInt(s.unscoreable_items) + ' unscoreable</span>' : '');
      var scripts = r.scripts || [];
      if (!scripts.length) {
        scList.innerHTML = '<div class="fx-note warn"><div>No upload batches to score — ' +
          (sum.count === 0
            ? 'the local mirror is empty. Run <b>Sync vault (Collect all)</b>, then <b>Describe all</b>: scoring reads the describe fields.'
            : 'nothing in ' + fmtInt(r.items || 0) + ' mirrored items groups into a shoot at a ' +
              (scParams().gap_seconds / 60) + '-minute gap. Widen the gap or describe more items.') + '</div></div>';
        return;
      }
      scList.innerHTML = scripts.map(function (sc) {
        return '<div class="vm-plan-row">' +
          '<div style="min-width:0;flex:1"><div class="pn">Batch ' + esc(sc.script_id) + '</div>' +
          '<div class="pm">' + esc(sc.started_at ? Fastt.fmtDate(sc.started_at) : '') +
          ' · ' + fmtInt(sc.scoreable || 0) + ' of ' + fmtInt(sc.size || 0) + ' scoreable · τ ' +
          (sc.tau == null ? '—' : Math.round(sc.tau * 100) / 100) + '</div></div>' +
          '<span class="' + laneCls(sc.direction) + '">' + esc(sc.direction || 'flat') + '</span>' +
          '<span class="pc">' + fmtInt(sc.size || 0) + '</span></div>';
      }).join('');
    } catch (e) {
      scStatus.innerHTML = '<span class="fx-st err"><i></i>Script scan failed</span>';
      scList.innerHTML = '<div class="fx-note warn"><div>' + esc((e.body && (e.body.detail || e.body.error)) || e.message) + '</div></div>';
    }
  }
  await loadScripts();
  Fastt.liveBadge(document.querySelector('#vm-scripts-group > h4'));
  if (scGap) scGap.addEventListener('change', loadScripts);
  if (scVids) scVids.addEventListener('click', function () { setTimeout(loadScripts, 0); });
  var scScan = document.getElementById('vm-sc-scan');
  if (scScan) scScan.addEventListener('click', loadScripts);
  var scApply = document.getElementById('vm-sc-apply');
  if (scApply) scApply.addEventListener('click', async function () {
    if (!confirm('Write the recovered script order onto the local mirror?\n\nOnly fastt-side columns (script id / seq / score) are written — no media is moved, hidden or sent.')) return;
    try {
      var out = await Fastt.post('/admin/vault-ai/scripts/apply',
        Object.assign({ account_id: acct }, scParams()));
      Fastt.saved('Script order written to ' + fmtInt(out.items || out.written || 0) + ' items');
      loadScripts();
    } catch (e) { Fastt.oops(e); }
  });
  var scFolders = document.getElementById('vm-sc-folders');
  if (scFolders) scFolders.addEventListener('click', async function () {
    if (!confirm('Queue every detected shoot as a PROPOSED folder?\n\nThey land in Review as pending rows — no folder is created until you approve them there.')) return;
    try {
      var out = await Fastt.post('/admin/vault-ai/scripts/collect',
        Object.assign({ account_id: acct, queue: true }, scParams()));
      Fastt.saved(fmtInt(out.count || 0) + ' shoots proposed — approve them on the Review page');
    } catch (e) { Fastt.oops(e); }
  });
  var scCreate = document.getElementById('vm-sc-create');
  if (scCreate) scCreate.addEventListener('click', async function () {
    if (!confirm('Create internal folders for every script proposal you already APPROVED in Review?\n\nInternal folders only — your OnlyFans vault is not touched.')) return;
    try {
      var out = await Fastt.post('/admin/vault-ai/scripts/folders/apply', { account_id: acct });
      Fastt.saved('Created ' + fmtInt(out.folders || out.created || 0) + ' folders from approved proposals');
      var f3 = await Fastt.get('/admin/vault-ai/folders');
      aiFolders = (f3 && f3.folders) || [];
      if (rail) rail.innerHTML = railHtml();
    } catch (e) { Fastt.oops(e); }
  });
});
