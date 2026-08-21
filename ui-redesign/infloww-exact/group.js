Fastt.ready(async function(){
  var esc = Fastt.esc;
  var $id = function(id){ return document.getElementById(id); };
  var gridEl = $id('grid');

  var SLOT_CAP = 8;
  var LS_PANES = 'fastt_group_panes';   // [{aid, fanId}]
  var LS_INIT  = 'fastt_group_init';    // "1" once the house default has run

  // ── per-account request helpers (mirror messages.html) ──────
  // Each pane is a different creator, so the account is per-REQUEST.
  // `Fastt.api({acct})` owns that rule; these two only shorten the call sites.
  function aget(path, params, aid){ return Fastt.get(path, params, {acct: aid}); }
  function apost(path, body, aid){ return Fastt.post(path, body, {acct: aid}); }

  // ── helpers ─────────────────────────────────────────────────
  var imgProxy = Fastt.imgProxy;
  function initials(name){ return esc(String(name || '?').trim().slice(0,2).toUpperCase()); }
  // Fan display names arrive as a slash-delimited nickname blob
  // (e.g. "Justin/Aldergrove,Canada/30/works at a hardware store").
  // Show only the first segment; fall back to @username, then Fan <id>.
  function cleanName(raw, username, fanId){
    var first = String(raw == null ? '' : raw).split('/')[0].trim();
    if(first) return first;
    var un = String(username == null ? '' : username).trim();
    if(un) return '@' + un;
    return 'Fan ' + fanId;
  }
  function stripHtml(html){
    if(!html) return '';
    try{ return (new DOMParser().parseFromString(String(html), 'text/html').body.textContent || '').trim(); }
    catch(e){ return String(html).replace(/<[^>]*>/g,'').trim(); }
  }
  function fmtTime(iso){
    var d = Fastt.parseUtc(iso);
    if(!d) return '';
    if(d.toDateString() === new Date().toDateString())
      return d.toLocaleTimeString([], {hour:'numeric', minute:'2-digit'}).toLowerCase();
    return d.toLocaleDateString([], {month:'short', day:'numeric'});
  }

  // ── roster (from the derived account list) ──────────────────
  var ROSTER = (Fastt.accounts() || []).map(function(a){
    return {id: String(a.id), name: a.nickname || String(a.id), color: a.color || null};
  });
  var byAid = {}; ROSTER.forEach(function(c){ byAid[c.id] = c; });
  function creatorName(aid){ var c = byAid[String(aid)]; return c ? c.name : String(aid); }

  // ── panes model + persistence ───────────────────────────────
  var panes = [];   // [{aid, fanId}]
  function loadPanes(){
    try{
      var raw = JSON.parse(localStorage.getItem(LS_PANES));
      if(!Array.isArray(raw)) return [];
      var out = [], seen = {};
      raw.forEach(function(s){
        if(!s || typeof s !== 'object') return;
        var aid = String(s.aid != null ? s.aid : s.accountId || '');
        var fan = Number(s.fanId);
        if(!aid || !isFinite(fan) || fan <= 0) return;
        var k = aid + ':' + fan; if(seen[k]) return; seen[k] = 1;
        out.push({aid: aid, fanId: fan});
      });
      return out.slice(0, SLOT_CAP);
    }catch(e){ return []; }
  }
  function savePanes(){ try{ localStorage.setItem(LS_PANES, JSON.stringify(panes)); }catch(e){} }
  function hasPane(aid, fanId){
    return panes.some(function(s){ return s.aid === String(aid) && String(s.fanId) === String(fanId); });
  }
  function addPane(aid, fanId){
    aid = String(aid); fanId = Number(fanId);
    if(!aid || !isFinite(fanId) || fanId <= 0) return false;
    if(hasPane(aid, fanId)) return false;
    if(panes.length >= SLOT_CAP){ Fastt.toast('Group is full — close a chat to add another', 'err'); return false; }
    panes.push({aid: aid, fanId: fanId}); savePanes(); renderGrid(); return true;
  }
  function removePane(aid, fanId){
    panes = panes.filter(function(s){ return !(s.aid === String(aid) && String(s.fanId) === String(fanId)); });
    savePanes(); renderGrid();
  }

  function updateCount(){
    $id('countN').textContent = String(panes.length);
    var pill = $id('countPill');
    pill.classList.toggle('full', panes.length >= SLOT_CAP);
    $id('btnAdd').disabled = panes.length >= SLOT_CAP;
    var sub = $id('brandSub');
    sub.textContent = panes.length ? (panes.length + (panes.length === 1 ? ' chat' : ' chats') + ' open · fast multi-chat')
                                   : 'Multi-chat across creators';
  }

  // ── message bubble (text-only, media collapses to a chip) ───
  function gMsgHtml(m, ownerId){
    var out = String((m.fromUser && m.fromUser.id) || '') === String(ownerId);
    var text = stripHtml(m.text);
    if(text.length > 600) text = text.slice(0, 599) + '…';
    var price = Number(m.price) || 0;
    var mediaCount = m.mediaCount != null ? m.mediaCount : (m.media ? m.media.length : 0);
    var locked = price > 0 && !m.isFree;
    var t = fmtTime(m.createdAt);
    var chips = '';
    if(locked) chips += '<span title="PPV ' + Fastt.fmtMoney(price) + '">🔒 ' + Fastt.fmtMoney(price) + '</span>';
    if(m.isTip) chips += '<span>💸 ' + Fastt.fmtMoney(Number(m.tipAmount) || 0) + '</span>';
    if(mediaCount > 0) chips += '<span title="' + mediaCount + ' media attached">📎 ' + mediaCount + '</span>';
    return '<div class="gb ' + (out ? 'out' : 'in') + '">' +
      (text ? esc(text) : (chips ? '' : '<span style="opacity:.6">(media)</span>')) +
      (chips ? '<div class="chips">' + chips + '</div>' : '') +
      '<span class="btime">' + esc(t) + '</span></div>';
  }

  // ── a single pane element (self-loading) ────────────────────
  var meCache = {};   // creator me is not needed for direction (fromUser.id === aid),
                      // but users/list gives the fan avatar/name — cached per fan.
  function buildPane(slot){
    var aid = slot.aid, fanId = slot.fanId;
    var el = document.createElement('div');
    el.className = 'pane';
    el.setAttribute('data-aid', aid);
    el.setAttribute('data-fan', String(fanId));
    el.innerHTML =
      '<div class="pane-head">' +
        '<span class="pane-av">' + initials('') + '</span>' +
        '<div class="pane-id">' +
          '<div class="pane-name">fan ' + esc(String(fanId)) + '</div>' +
          '<div class="pane-cre"><i></i>' + esc(creatorName(aid)) + '</div>' +
        '</div>' +
        '<a class="pane-open" title="Open full chat in Messages" target="_blank" rel="noreferrer" ' +
          'href="messages.html?account=' + encodeURIComponent(aid) + '&fan=' + encodeURIComponent(fanId) + '&popout=1">' +
          '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M14 4h6v6M20 4l-8 8M18 13v6a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h6"/></svg>' +
        '</a>' +
        '<button class="pane-x" title="Remove from group" aria-label="Remove from group">&times;</button>' +
      '</div>' +
      '<div class="pane-body"><div class="pane-state">Loading…</div></div>' +
      '<div class="pane-att"></div>' +
      '<div class="pane-tools">' +
        '<button class="ptool" data-tool="vault" title="Attach from vault">' +
          '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="8" cy="9" r="1.6"/><path d="M4 18l5-4 4 3 3-2 4 3"/></svg>' +
          '<span class="cnt" style="display:none">0</span></button>' +
        '<button class="ptool" data-tool="gif" title="Attach a GIF">' +
          '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.4"><rect x="3" y="6" width="18" height="12" rx="2"/><text x="12" y="15" font-size="7" font-weight="700" text-anchor="middle" fill="currentColor" stroke="none">GIF</text></svg></button>' +
        '<button class="ptool" data-tool="emoji" title="Emoji">' +
          '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><circle cx="12" cy="12" r="9"/><path d="M8.5 14.5a4 4 0 0 0 7 0" stroke-linecap="round"/><circle cx="9" cy="10" r="1" fill="currentColor" stroke="none"/><circle cx="15" cy="10" r="1" fill="currentColor" stroke="none"/></svg></button>' +
      '</div>' +
      '<div class="pane-comp">' +
        '<textarea rows="1" placeholder="Message…"></textarea>' +
        '<button class="pane-send" disabled>Send</button>' +
      '</div>' +
      '<div class="pane-foot"><span>fast-chat · last 20</span><span class="pane-cnt">—</span></div>';

    var bodyEl = el.querySelector('.pane-body');
    var nameEl = el.querySelector('.pane-name');
    var avEl = el.querySelector('.pane-av');
    var taEl = el.querySelector('textarea');
    var sendEl = el.querySelector('.pane-send');
    var cntEl = el.querySelector('.pane-cnt');
    var attEl = el.querySelector('.pane-att');
    var toolsEl = el.querySelector('.pane-tools');
    var vaultCntEl = toolsEl.querySelector('[data-tool="vault"] .cnt');
    var fanLabel = 'fan ' + fanId;
    var attach = {};   // media id -> thumb url (vault attachments for the next send)
    var gifId = null;  // giphy id for the next send

    el.querySelector('.pane-x').addEventListener('click', function(){ removePane(aid, fanId); });

    // ── load fan profile (name + avatar) ──
    (async function(){
      try{
        var resp = await Fastt.get('/api/of/v2/users/list?ids=' + fanId + '&view=m', null, {acct: aid});
        var u = resp && resp[String(fanId)];
        if(u && el.isConnected){
          var th = u.avatarThumbs || {};
          var raw = u.displayName || u.customNickname || u.name || u.username || fanLabel;
          var nm = cleanName(raw, u.username, fanId);
          fanLabel = nm;
          nameEl.textContent = nm;
          nameEl.setAttribute('title', String(raw || nm));
          taEl.setAttribute('placeholder', 'Message ' + nm + '…');
          var av = th.c50 || th.c144 || u.avatar || null;
          if(av){
            avEl.innerHTML = initials(nm) +
              '<img src="' + esc(imgProxy(av)) + '" alt="" loading="lazy" onerror="this.remove()">';
          }else{
            avEl.textContent = String(nm).trim().slice(0,2).toUpperCase();
          }
        }
      }catch(e){ /* keep the fan-id label */ }
    })();

    // ── load thread (last 20, text-only) ──
    async function loadThread(){
      try{
        var resp = await aget('/api/of/v2/chats/' + fanId + '/messages', {limit: 20, order: 'desc'}, aid);
        if(!el.isConnected) return;
        var list = (resp.list || []).slice().reverse();
        if(!list.length){
          bodyEl.innerHTML = '<div class="pane-state">No messages yet.</div>';
          cntEl.textContent = '0';
          return;
        }
        bodyEl.innerHTML = list.map(function(m){ return gMsgHtml(m, aid); }).join('');
        bodyEl.scrollTop = bodyEl.scrollHeight;
        cntEl.textContent = String(list.length);
      }catch(e){
        if(!el.isConnected) return;
        bodyEl.innerHTML = '<div class="pane-state err">Could not load (' +
          esc(String((e && e.status) || 'error')) + ').</div>';
      }
    }
    loadThread();

    // ── composer (click + confirm gated — NEVER auto-fires) ──
    function refreshSend(){
      sendEl.disabled = !(taEl.value.trim().length || Object.keys(attach).length || gifId);
    }
    function renderAtt(){
      var ids = Object.keys(attach);
      var html = ids.map(function(id){
        var th = attach[id];
        return '<span class="a" data-rm="' + esc(id) + '" title="Remove">' +
          (th ? '<img src="' + esc(th.charAt(0) === '/' ? th : imgProxy(th)) + '" alt="" onerror="this.remove()">' : '📎') + '<b>✕</b></span>';
      }).join('');
      if(gifId) html += '<span class="a gif" data-rmgif="1" title="Remove GIF">GIF<b>✕</b></span>';
      attEl.innerHTML = html;
      if(vaultCntEl){ vaultCntEl.textContent = ids.length; vaultCntEl.style.display = ids.length ? '' : 'none'; }
      refreshSend();
    }
    attEl.addEventListener('click', function(e){
      var rm = e.target.closest('[data-rm]');
      if(rm){ delete attach[rm.getAttribute('data-rm')]; renderAtt(); return; }
      if(e.target.closest('[data-rmgif]')){ gifId = null; renderAtt(); }
    });
    toolsEl.addEventListener('click', function(e){
      var b = e.target.closest('.ptool'); if(!b) return;
      var tool = b.getAttribute('data-tool');
      if(tool === 'vault'){
        openGroupVault(aid, fanId, function(picked){ Object.keys(picked).forEach(function(id){ attach[id] = picked[id]; }); renderAtt(); });
      }else if(tool === 'gif'){
        openGroupGif(aid, function(gid){ gifId = gid; renderAtt(); });
      }else if(tool === 'emoji'){
        openEmoji(b, function(ch){ insertAtCursor(taEl, ch); refreshSend(); });
      }
    });
    taEl.addEventListener('input', refreshSend);
    taEl.addEventListener('keydown', function(e){
      if(e.key === 'Enter' && !e.shiftKey){ e.preventDefault(); doSend(); }
    });
    sendEl.addEventListener('click', doSend);

    var sending = false;
    async function doSend(){
      var text = taEl.value.trim();
      var mids = Object.keys(attach);
      if((!text && !mids.length && !gifId) || sending) return;
      var who = fanLabel, cre = creatorName(aid);
      var extra = (mids.length ? ' + ' + mids.length + ' vault item(s)' : '') + (gifId ? ' + GIF' : '');
      // SAFETY: a real OnlyFans DM. Explicit confirm on every send, always.
      if(!confirm('SEND this message to ' + who + ' on OnlyFans as ' + cre + '?' + extra +
                  (text ? '\n\n"' + text + '"' : '\n\n(media only)'))) return;
      sending = true; sendEl.disabled = true; taEl.disabled = true;
      try{
        var body = {text: text};
        if(mids.length) body.media_files = mids.map(Number);
        if(gifId) body.giphy_id = gifId;
        var r = await apost('/api/of/v2/chats/' + fanId + '/messages', body, aid);
        if(r && !r.fromUser) r.fromUser = {id: Number(aid)};
        var state = bodyEl.querySelector('.pane-state');
        if(state) bodyEl.innerHTML = '';
        bodyEl.insertAdjacentHTML('beforeend', gMsgHtml(r || {
          text: text, createdAt: new Date().toISOString(), fromUser: {id: Number(aid)},
          mediaCount: mids.length}, aid));
        bodyEl.scrollTop = bodyEl.scrollHeight;
        taEl.value = ''; attach = {}; gifId = null; renderAtt();
        Fastt.saved('Sent ✓');
      }catch(e){ Fastt.oops(e); }
      finally{ sending = false; taEl.disabled = false; refreshSend(); taEl.focus(); }
    }

    return el;
  }

  // ═══════════════════════════════════════════════════════════
  // PANE PICKERS — vault (folders→media, cached, video previews) / gif / emoji
  // Per-account, click-gated. Attachments ride the next Send (media_files/giphy_id).
  // ═══════════════════════════════════════════════════════════
  // ── vault video helpers ── the OF payload contract lives in _shared/vault-media.js
  var progressiveVideoSrc = FasttVault.progressiveVideoSrc,
      isDrmVideo          = FasttVault.isDrmVideo,
      videoPosterFrames   = FasttVault.videoPosterFrames;
  function fmtDur(sec){
    sec = Math.max(0, Math.round(Number(sec) || 0));
    var mm = Math.floor(sec/60), ss = sec%60; return mm + ':' + (ss<10?'0':'') + ss;
  }
  var GPLAY = '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg>';

  function openGroupVideo(m){
    var src = progressiveVideoSrc(m), drm = isDrmVideo(m), posters = videoPosterFrames(m);
    var back = document.createElement('div'); back.className = 'gvid-back';
    var body;
    if(src){
      var pf = (m.files && (m.files.squarePreview || m.files.preview || m.files.thumb)) || null;
      body = '<video src="' + esc(src) + '"' + (pf && pf.url ? ' poster="' + esc(imgProxy(pf.url)) + '"' : '') +
        ' controls autoplay playsinline loop preload="metadata"></video>';
    }else if(drm && posters.length){
      body = '<img id="gvpFrame" src="' + esc(imgProxy(posters[0])) + '" alt="">' +
        '<div class="gvid-note">OnlyFans protects this video with DRM — preview frames only (' + posters.length + ').</div>';
    }else{
      body = '<div class="gvid-note">No playable preview for this video.</div>';
    }
    back.innerHTML = '<button class="gvid-x" title="Close">&times;</button>' + body;
    document.body.appendChild(back);
    function close(){ var v = back.querySelector('video'); if(v){ try{ v.pause(); }catch(e){} } back.remove(); document.removeEventListener('keydown', onKey); }
    function onKey(e){ if(e.key === 'Escape') close(); }
    document.addEventListener('keydown', onKey);
    back.addEventListener('click', function(e){ if(e.target === back) close(); });
    back.querySelector('.gvid-x').addEventListener('click', close);
    if(!src && drm && posters.length > 1){
      var frame = back.querySelector('#gvpFrame'), i = 0;
      var tick = setInterval(function(){ if(!back.isConnected){ clearInterval(tick); return; } i = (i+1)%posters.length; frame.src = imgProxy(posters[i]); }, 1100);
    }
  }

  var GVAULT_CACHE = { folders:{}, summary:{}, hist:{} };   // per-account (+ per-fan hist), session-lived

  // Per-fan folder MRU + per-account usage counts (folders reordered most-used
  // first), per-fan picker position, and the AI caption — same behaviour as the
  // single-model chat picker so the group picker is at parity.
  function gvMruKey(aid, fid){ return 'ft_vault_mru:' + aid + ':' + (fid == null ? '_' : fid); }
  function gvCountKey(aid){ return 'ft_vault_counts:' + aid; }
  function gvStateKey(aid, fid){ return 'ft_vault_state:' + aid + ':' + (fid == null ? '_' : fid); }
  function gvLoadMru(aid, fid){ try{ var a = JSON.parse(localStorage.getItem(gvMruKey(aid, fid)) || '[]'); return Array.isArray(a) ? a.filter(function(n){ return typeof n === 'number'; }).slice(0, 3) : []; }catch(e){ return []; } }
  function gvSaveMru(aid, fid, mru){ try{ localStorage.setItem(gvMruKey(aid, fid), JSON.stringify(mru.slice(0, 3))); }catch(e){} }
  function gvLoadCounts(aid){ try{ var o = JSON.parse(localStorage.getItem(gvCountKey(aid)) || '{}'); return (o && typeof o === 'object') ? o : {}; }catch(e){ return {}; } }
  function gvBumpCount(aid, lid){ try{ var o = gvLoadCounts(aid); o[String(lid)] = (o[String(lid)] || 0) + 1; localStorage.setItem(gvCountKey(aid), JSON.stringify(o)); }catch(e){} }
  function gvLoadState(aid, fid){ try{ var s = JSON.parse(localStorage.getItem(gvStateKey(aid, fid)) || 'null'); return (s && typeof s === 'object') ? s : null; }catch(e){ return null; } }
  function gvSaveState(aid, fid, st){ try{ localStorage.setItem(gvStateKey(aid, fid), JSON.stringify(st)); }catch(e){} }
  function gvAiCaption(m){
    var ai = m && m._ai; if(!ai) return null;
    var primary = m.type === 'video' ? ai.video_description : ai.description;
    var text = String(primary || ai.description || ai.video_description || '').trim();
    var tags = (Array.isArray(ai.tags) ? ai.tags : []).map(function(t){ return String(t).trim(); }).filter(Boolean).slice(0, 4);
    if(!text && !tags.length) return null;
    return { text: text, tags: tags };
  }
  // Unified vault picker — parity with the single-model chat: cache-first source,
  // type/folder/sort/search, MRU folder quick-chips (reorder), AI captions,
  // owned/sent rings for this pane's fan, and hover-scrub video previews.
  async function openGroupVault(aid, fid, onConfirm){
    var sh = sheet(), opts = {acct: aid}, PAGE = 48;
    sh.H.innerHTML = '<span>Vault · ' + esc(creatorName(aid)) + '</span>' +
      '<span class="step" id="gvSel">0 selected</span><span class="x" title="Close">&times;</span>';
    sh.H.querySelector('.x').addEventListener('click', function(){ hov.stop(); sh.close(); });
    sh.B.innerHTML = '<div class="gvtb" id="gvTb"></div><div class="gvchips" id="gvChips" style="display:none"></div>' +
      '<div id="gvHint"></div><div id="gvBody"><div class="gnote">Loading vault…</div></div>';
    var foot = document.createElement('div');
    foot.className = 'sheet-search'; foot.style.borderTop = '1px solid var(--border)';
    foot.innerHTML = '<button class="btn-add" id="gvAdd" style="width:100%;justify-content:center">Attach selected</button>';
    sh.querySelector('.sheet').appendChild(foot);

    var chosen = {}, mediaMeta = {}, histMap = {};
    var hov = null;   // hover-scrub session (FasttVault.hoverScrub), armed below
    var savedSt = gvLoadState(aid, fid);
    var state = { type: (savedSt && savedSt.type) || 'all', listId: (savedSt && savedSt.listId != null) ? savedSt.listId : null,
                  sort: (savedSt && savedSt.sort) || 'newest', query: '',
                  captions: localStorage.getItem('ft_vault_captions') === '1' };
    function persistState(){ gvSaveState(aid, fid, { type: state.type, listId: state.listId, sort: state.sort }); }
    var page = { items: [], offset: 0, hasMore: false, loading: false, seq: 0 };
    var SRC = 'live', folders = [], mru = gvLoadMru(aid, fid), counts = gvLoadCounts(aid);
    var tbEl = sh.B.querySelector('#gvTb'), chipsEl = sh.B.querySelector('#gvChips'),
        hintEl = sh.B.querySelector('#gvHint'), bodyEl = sh.B.querySelector('#gvBody');
    // Hover-to-preview over the tile grid — same session the single-model
    // picker runs; it owns its timers and its own delegated listeners.
    hov = FasttVault.hoverScrub(bodyEl, function(mid){ return mediaMeta[mid]; });
    function updSel(){ var n = Object.keys(chosen).length; var el = sh.querySelector('#gvSel'); if(el) el.textContent = n + ' selected'; }

    try{
      var sum = GVAULT_CACHE.summary[aid];
      if(sum === undefined){ sum = await Fastt.get('/admin/vault-ai/cache/summary', {account_id: aid}, opts); GVAULT_CACHE.summary[aid] = sum; }
      SRC = (sum && sum.count > 0) ? 'mirror' : 'live';
    }catch(e){ SRC = 'live'; }
    try{
      folders = GVAULT_CACHE.folders[aid];
      if(!folders){ var lo = await Fastt.get('/api/of/v2/vault/lists', {limit: 50}, opts); folders = lo.list || []; GVAULT_CACHE.folders[aid] = folders; }
    }catch(e){ folders = []; }
    if(state.listId != null && !folderById(state.listId)) state.listId = null;

    function folderById(id){ for(var i = 0; i < folders.length; i++){ if(String(folders[i].id) === String(id)) return folders[i]; } return null; }
    function folderCounts(f){ var parts = [], ph = (f.photosCount || 0) + (f.gifsCount || 0);
      if(ph) parts.push('🖼 ' + ph); if(f.videosCount) parts.push('🎬 ' + f.videosCount); if(f.audiosCount) parts.push('🎵 ' + f.audiosCount); return parts.join(' · '); }
    function rankedFolderIds(){
      var out = [], seen = {};
      function push(id){ if(seen[id]) return; if(!folderById(id)) return; seen[id] = 1; out.push(Number(id)); }
      mru.forEach(function(id){ push(id); });
      Object.keys(counts).map(function(id){ return { id: Number(id), c: Number(counts[id]) || 0 }; })
        .filter(function(e){ return isFinite(e.id) && e.c > 0; }).sort(function(a, b){ return b.c - a.c; }).forEach(function(e){ push(e.id); });
      folders.forEach(function(f){ push(f.id); });
      return out;
    }
    function renderToolbar(){
      var types = [['all','All'],['photo','Photos'],['video','Videos'],['gif','GIFs']];
      var seg = '<div class="vseg">' + types.map(function(t){ return '<button data-type="' + t[0] + '"' + (state.type === t[0] ? ' class="on"' : '') + '>' + t[1] + '</button>'; }).join('') + '</div>';
      var ranked = rankedFolderIds();
      var optHtml = '<option value=""' + (state.listId == null ? ' selected' : '') + '>All folders</option>' +
        ranked.map(function(id){ var f = folderById(id); if(!f) return ''; var c = folderCounts(f);
          return '<option value="' + esc(f.id) + '"' + (String(state.listId) === String(f.id) ? ' selected' : '') + '>' + esc((f.name || ('Folder ' + f.id)) + (c ? ' — ' + c : '')) + '</option>'; }).join('');
      var sel = folders.length ? '<select id="gvFolder" title="Filter by folder (most-used first)">' + optHtml + '</select>' : '';
      var sort = '<select id="gvSort"><option value="newest"' + (state.sort === 'newest' ? ' selected' : '') + '>Newest</option><option value="oldest"' + (state.sort === 'oldest' ? ' selected' : '') + '>Oldest</option></select>';
      var search = '<input type="search" id="gvQ" placeholder="Search vault…" value="' + esc(state.query) + '">';
      var capTog = '<button class="vcap-tog' + (state.captions ? ' on' : '') + '" id="gvCapTog" title="Show AI descriptions on thumbnails">📝 captions</button>';
      var cached = SRC === 'mirror' ? '<span class="vcached" title="Served instantly from the local cache">⚡ cached</span>' : '';
      tbEl.innerHTML = seg + sel + sort + search + capTog + cached;
    }
    function renderChips(){
      if(!folders.length){ chipsEl.style.display = 'none'; return; }
      chipsEl.style.display = '';
      var ranked = rankedFolderIds().slice(0, 3), mruSet = {}; mru.forEach(function(id){ mruSet[id] = 1; });
      chipsEl.innerHTML = '<span class="vlbl">Folders:</span><button class="vchip' + (state.listId == null ? ' on' : '') + '" data-lid="">All</button>' +
        ranked.map(function(id){ var f = folderById(id); if(!f) return '';
          return '<button class="vchip' + (String(state.listId) === String(id) ? ' on' : '') + '" data-lid="' + esc(id) + '" title="' + esc(f.name || '') + '">' + (mruSet[id] ? '<span class="st">★</span>' : '') + esc(f.name || ('Folder ' + id)) + '</button>'; }).join('');
    }
    function renderHint(){ hintEl.innerHTML = (state.captions && SRC !== 'mirror') ? '<div class="gvcap-hint">📝 AI captions light up once this account’s vault is <b>Collected</b> (Vault manager → Collect).</div>' : ''; }

    function tileThumb(m){ if(SRC === 'mirror') return m._thumb || ''; var f = m.files && (m.files.thumb || m.files.squarePreview || m.files.preview || m.files.full); return (f && f.url) ? imgProxy(f.url) : ''; }
    function tileThumbRaw(m){ if(SRC === 'mirror') return m._thumb || ''; var f = m.files && (m.files.thumb || m.files.squarePreview || m.files.preview || m.files.full); return (f && f.url) ? f.url : ''; }
    function tileHtml(m){
      mediaMeta[m.id] = m;
      var thumb = tileThumb(m), thumbRaw = tileThumbRaw(m), sel = chosen[m.id] != null ? ' sel' : '';
      var isVid = m.type === 'video', isGif = m.type === 'gif', extra = '';
      if(isVid){ var drm = isDrmVideo(m);
        extra += '<span class="vplay" data-play="' + esc(m.id) + '" title="' + (drm ? 'DRM — preview frames' : 'Play preview') + '">' + GPLAY + '</span>';
        if(m.duration) extra += '<span class="vdur">' + fmtDur(m.duration) + '</span>';
        if(drm) extra += '<span class="vlock" title="OnlyFans DRM">🔒</span>'; }
      var h = histMap[m.id], histBadge = '';
      if(h){ if(h.was_purchased) histBadge = '<span class="vt-own" title="This fan already OWNS this">✓ owned</span>';
        else if(h.send_count > 0) histBadge = '<span class="vt-sent" title="Sent, not bought">sent</span>'; }
      var described = !!(m._ai && m._ai.describe_status === 'described');
      var cap = state.captions ? gvAiCaption(m) : null;
      var capHtml = cap ? ('<div class="vcap">' + (cap.text ? '<p>' + esc(cap.text) + '</p>' : '') + (cap.tags.length ? '<div class="vcap-tags">' + cap.tags.map(function(t){ return '<span>' + esc(t) + '</span>'; }).join('') + '</div>' : '') + '</div>') : '';
      return '<div class="mt' + sel + '" data-mid="' + esc(m.id) + '" data-thumb="' + esc(thumbRaw) + '">' +
        (thumb ? '<img src="' + esc(thumb) + '" alt="" loading="lazy" decoding="async" onerror="this.remove()">' : '') +
        extra + capHtml + histBadge + (described && !histBadge ? '<span class="vt-dot" title="AI-described"></span>' : '') +
        '<span class="tag">' + esc(isGif ? 'GIF' : (m.type || '')) + '</span></div>';
    }
    async function fetchPage(offset){
      if(SRC === 'mirror'){
        var p = { account_id: aid, limit: PAGE, offset: offset, sort: state.sort };
        if(state.type !== 'all') p.type = state.type; if(state.query) p.query = state.query; if(state.listId != null) p.of_folder_id = state.listId;
        var out = await Fastt.get('/admin/vault-ai/items', p, opts); return { list: out.list || [], hasMore: !!out.hasMore };
      }
      var q = { limit: PAGE, offset: offset, field: 'recent', sort: state.sort === 'newest' ? 'desc' : 'asc' };
      if(state.type !== 'all') q.type = state.type; if(state.query) q.query = state.query; if(state.listId != null) q.list_id = state.listId;
      var out2 = await Fastt.get('/api/of/v2/vault/media', q, opts); return { list: out2.list || [], hasMore: !!out2.hasMore };
    }
    function renderGrid(){
      hov.stop();   // the tiles this preview points at are about to be replaced
      if(!page.items.length){ bodyEl.innerHTML = '<div class="gnote">' + ((state.query || state.type !== 'all' || state.listId != null) ? 'Nothing matches this filter.' : 'The vault returned 0 items.') + '</div>'; return; }
      bodyEl.innerHTML = '<div class="gmgrid">' + page.items.map(tileHtml).join('') + '</div>' +
        '<div class="gvmore-row"><span>Showing ' + page.items.length + ' · ' + (SRC === 'mirror' ? '⚡ local cache' : 'live from OnlyFans') + '</span>' + (page.hasMore ? '<button class="btn-add" id="gvMore" style="height:30px;padding:0 12px">Load ' + PAGE + ' more</button>' : '') + '</div>';
    }
    async function loadGrid(append){
      if(append && page.loading) return;
      page.loading = true; var seq = ++page.seq;
      if(!append){ page.items = []; page.offset = 0; bodyEl.innerHTML = '<div class="gnote">Loading media…</div>'; }
      try{
        var out = await fetchPage(page.offset);
        if(seq !== page.seq) return;
        page.items = page.items.concat(out.list); page.offset += out.list.length; page.hasMore = out.hasMore && out.list.length > 0;
        renderGrid();
      }catch(e){ if(seq === page.seq) bodyEl.innerHTML = '<div class="gnote">Vault unavailable (' + esc(String((e && e.status) || 'error')) + ').</div>'; }
      finally{ if(seq === page.seq) page.loading = false; }
    }
    async function playItem(m){
      if(!m) return;
      if(progressiveVideoSrc(m) || videoPosterFrames(m).length){ openGroupVideo(m); return; }
      try{ var full = await Fastt.get('/api/of/v2/vault/media/' + m.id, null, opts); openGroupVideo(full || m); }catch(e){ openGroupVideo(m); }
    }
    function chooseFolder(lid){
      state.listId = (lid === '' || lid == null) ? null : Number(lid);
      if(state.listId != null){ gvBumpCount(aid, state.listId); mru = [state.listId].concat(mru.filter(function(x){ return x !== state.listId; })).slice(0, 3); gvSaveMru(aid, fid, mru); counts = gvLoadCounts(aid); }
      persistState(); renderToolbar(); renderChips(); loadGrid(false);
    }
    bodyEl.addEventListener('click', function(e){
      var play = e.target.closest('[data-play]');
      if(play){ e.stopPropagation(); playItem(mediaMeta[play.getAttribute('data-play')]); return; }
      if(e.target.closest('#gvMore')){ loadGrid(true); return; }
      var t = e.target.closest('.mt');
      if(t){ var id = t.getAttribute('data-mid'); if(chosen[id] != null){ delete chosen[id]; t.classList.remove('sel'); } else { chosen[id] = t.getAttribute('data-thumb') || ''; t.classList.add('sel'); } updSel(); }
    });
    tbEl.addEventListener('click', function(e){
      var b = e.target.closest('[data-type]');
      if(b){ state.type = b.getAttribute('data-type'); persistState(); renderToolbar(); loadGrid(false); return; }
      if(e.target.closest('#gvCapTog')){ state.captions = !state.captions; try{ localStorage.setItem('ft_vault_captions', state.captions ? '1' : '0'); }catch(_e){} renderToolbar(); renderHint(); if(!page.loading) renderGrid(); return; }
    });
    tbEl.addEventListener('change', function(e){
      if(e.target.id === 'gvFolder'){ chooseFolder(e.target.value); return; }
      if(e.target.id === 'gvSort'){ state.sort = e.target.value; persistState(); loadGrid(false); return; }
    });
    tbEl.addEventListener('input', Fastt.debounce(function(){ var q = sh.querySelector('#gvQ'); if(!q) return; var v = q.value.trim(); if(v === state.query) return; state.query = v; loadGrid(false); }, 300));
    chipsEl.addEventListener('click', function(e){ var c = e.target.closest('[data-lid]'); if(!c) return; chooseFolder(c.getAttribute('data-lid')); });
    foot.querySelector('#gvAdd').addEventListener('click', function(){
      if(!Object.keys(chosen).length){ Fastt.toast('Pick at least one item'); return; }
      hov.stop(); onConfirm(chosen); sh.close();
    });

    renderToolbar(); renderChips(); renderHint(); loadGrid(false);
    if(fid != null){
      var hk = aid + ':' + fid, cachedH = GVAULT_CACHE.hist[hk];
      if(cachedH){ histMap = cachedH; }
      else{ Fastt.get('/admin/vault/fan-history', {account_id: aid, fan_id: fid}, opts)
        .then(function(fh){ histMap = (fh && fh.by_media) || {}; GVAULT_CACHE.hist[hk] = histMap; if(!page.loading && page.items.length) renderGrid(); }).catch(function(){}); }
    }
  }

  function openGroupGif(aid, onConfirm){
    var sh = sheet();
    sh.H.innerHTML = '<span>GIF · ' + esc(creatorName(aid)) + '</span><span class="x" title="Close">&times;</span>';
    sh.H.querySelector('.x').addEventListener('click', sh.close);
    sh.B.innerHTML = '<div class="sheet-search"><input id="ggq" placeholder="Search Giphy…" autocomplete="off"></div><div id="ggrid"><div class="gnote">Loading trending…</div></div>';
    async function run(q){
      var grid = sh.B.querySelector('#ggrid'); grid.innerHTML = '<div class="gnote">Loading…</div>';
      try{
        var out = q ? await Fastt.get('/api/of/v2/giphy/proxy/gifs/search', {q:q, limit:24}, {acct: aid})
                    : await Fastt.get('/api/of/v2/giphy/proxy/gifs/trending', {limit:24}, {acct: aid});
        var data = out.data || [];
        grid.innerHTML = data.length ? '<div class="gmgrid">' + data.map(function(g){
          var im = (g.images && (g.images.fixed_width_small || g.images.preview_gif || g.images.original)) || {};
          return '<div class="mt" data-gid="' + esc(g.id) + '">' + (im.url ? '<img src="' + esc(im.url) + '" alt="" loading="lazy">' : '') + '</div>';
        }).join('') + '</div>' : '<div class="gnote">No GIFs found.</div>';
      }catch(e){ grid.innerHTML = '<div class="gnote">Giphy proxy unavailable (' + esc(String((e&&e.status)||'error')) + ').</div>'; }
    }
    sh.B.addEventListener('click', function(e){ var t = e.target.closest('[data-gid]'); if(!t) return; onConfirm(t.getAttribute('data-gid')); sh.close(); });
    sh.B.querySelector('#ggq').addEventListener('keydown', function(e){ if(e.key === 'Enter') run(this.value.trim()); });
    run(null);
  }

  var EMOJIS = ['😊','😉','😍','🥰','😘','😜','😏','🥵','🔥','💦','🍑','🍆','💋','❤️','💕','👀','😈','🙈','🤤','💯','🎉','🙏','👑','✨','😅','😂','🥺','😳','💖','🤗','🫦','🌶️'];
  function insertAtCursor(ta, ch){
    var s = ta.selectionStart, e = ta.selectionEnd, v = ta.value;
    ta.value = v.slice(0, s) + ch + v.slice(e);
    ta.selectionStart = ta.selectionEnd = s + ch.length; ta.focus();
  }
  function openEmoji(anchor, onPick){
    Array.prototype.forEach.call(document.querySelectorAll('.emoji-pop'), function(p){ p.remove(); });
    var pop = document.createElement('div'); pop.className = 'emoji-pop';
    pop.innerHTML = EMOJIS.map(function(e){ return '<button type="button">' + e + '</button>'; }).join('');
    document.body.appendChild(pop);
    var r = anchor.getBoundingClientRect(), w = pop.getBoundingClientRect();
    pop.style.left = Math.min(Math.max(8, r.left), window.innerWidth - w.width - 8) + 'px';
    pop.style.top = Math.max(8, r.top - w.height - 6) + 'px';
    pop.addEventListener('click', function(e){ var b = e.target.closest('button'); if(b) onPick(b.textContent); });
    setTimeout(function(){
      document.addEventListener('click', function h(ev){ if(!pop.contains(ev.target) && ev.target !== anchor){ pop.remove(); document.removeEventListener('click', h); } });
    }, 0);
  }

  // ── render ──────────────────────────────────────────────────
  function renderEmpty(){
    gridEl.classList.add('is-empty');
    gridEl.innerHTML =
      '<div class="empty">' +
        '<div class="eicon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><circle cx="9" cy="8" r="3.2"/><path d="M3 20c0-3.3 2.7-6 6-6s6 2.7 6 6"/><circle cx="17.5" cy="9" r="2.4"/><path d="M15.5 20c0-2.8 1.8-5 4.5-5"/></svg></div>' +
        '<h2>No chats in this group yet</h2>' +
        '<p>Add up to 8 fan conversations side-by-side to answer many chats fast — even across different creators. Refresh keeps them open.</p>' +
        '<button class="btn-add" id="emptyAdd"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M12 5v14M5 12h14"/></svg> Add a chat</button>' +
      '</div>';
    var b = $id('emptyAdd'); if(b) b.addEventListener('click', openCreatorPicker);
  }
  function renderGrid(){
    updateCount();
    if(!panes.length){ renderEmpty(); return; }
    gridEl.classList.remove('is-empty');
    gridEl.innerHTML = '';
    panes.forEach(function(slot){ gridEl.appendChild(buildPane(slot)); });
  }

  // ═══════════════════════════════════════════════════════════
  // ADD-A-PANE  (step 1: creator → step 2: fan)
  // ═══════════════════════════════════════════════════════════
  function sheet(){
    var back = document.createElement('div');
    back.className = 'sheet-back';
    back.innerHTML = '<div class="sheet"><div class="sheet-h" id="shH"></div><div class="sheet-b" id="shB"></div></div>';
    document.body.appendChild(back);
    back.close = function(){ back.remove(); };
    back.addEventListener('click', function(e){ if(e.target === back) back.close(); });
    back.H = back.querySelector('#shH');
    back.B = back.querySelector('#shB');
    return back;
  }

  function openCreatorPicker(){
    var sh = sheet();
    sh.H.innerHTML = '<span>Add a chat</span><span class="step">1 / 2 · pick a creator</span>' +
      '<span class="x" title="Close">&times;</span>';
    sh.H.querySelector('.x').addEventListener('click', sh.close);
    if(!ROSTER.length){
      sh.B.innerHTML = '<div class="sheet-note"><b>No creators available.</b><br>' +
        'Sign in (top-right of Messages) or capture a session so the roster loads.</div>';
      return;
    }
    sh.B.innerHTML = ROSTER.map(function(c, i){
      var col = c.color || ['#e0679b','#5b8def','#67d1ae','#a78bfa','#e5a35b','#e07a5f'][i % 6];
      return '<div class="pick" data-aid="' + esc(c.id) + '">' +
        '<span class="pav" style="background:' + esc(col) + '">' + initials(c.name) + '</span>' +
        '<div class="pmain"><div class="pnm">' + esc(c.name) + '</div>' +
        '<div class="psub">creator ' + esc(c.id) + '</div></div>' +
        '<span class="pmeta">&rsaquo;</span></div>';
    }).join('');
    sh.B.addEventListener('click', function(e){
      var row = e.target.closest('.pick'); if(!row) return;
      var aid = row.getAttribute('data-aid');
      sh.close();
      openFanPicker(aid);
    });
  }

  async function openFanPicker(aid){
    var sh = sheet();
    var cname = creatorName(aid);
    sh.H.innerHTML = '<span class="bk">&lsaquo; Creators</span><span>' + esc(cname) + '</span>' +
      '<span class="step">2 / 2 · pick a fan</span><span class="x" title="Close">&times;</span>';
    sh.H.querySelector('.x').addEventListener('click', sh.close);
    sh.H.querySelector('.bk').addEventListener('click', function(){ sh.close(); openCreatorPicker(); });
    sh.B.innerHTML = '<div class="pane-state">Loading recent chats…</div>';

    var rows = [];
    try{
      // Local-DB recent feed, filtered to this creator (cheap, names ride along).
      var out = await Fastt.get('/admin/chats/recent', {limit: 100}, {noAccount:true});
      rows = (out.list || []).filter(function(r){
        return String(r.__accountId) === String(aid) && r.withUser && r.withUser.id;
      });
    }catch(e){ /* fall through to OF below */ }
    if(!rows.length){
      // fall back to a live OF pull for creators with no local rows yet
      try{
        var resp = await aget('/api/of/v2/chats', {limit: 30, offset: 0, order: 'recent'}, aid);
        rows = (resp.list || []).filter(function(r){ return r.withUser && r.withUser.id; });
      }catch(e){ /* leave empty */ }
    }
    if(!rows.length){
      sh.B.innerHTML = '<div class="sheet-note"><b>No recent chats for ' + esc(cname) + '.</b><br>' +
        'This creator has no captured conversations in this lane yet.</div>';
      return;
    }

    var norm = rows.map(function(r){
      var wu = r.withUser || {}, lm = r.lastMessage || null;
      return {fanId: wu.id, name: wu.name || wu.username || ('fan ' + wu.id),
              username: wu.username || '', avatar: wu.avatar || null,
              prev: lm ? stripHtml(lm.text).slice(0, 48) : '', at: lm && lm.createdAt};
    });

    function paint(filterTxt){
      var q = (filterTxt || '').toLowerCase();
      var shown = norm.filter(function(n){
        if(!q) return true;
        return (n.name + ' ' + n.username + ' ' + n.fanId).toLowerCase().indexOf(q) >= 0;
      });
      var list = shown.map(function(n){
        var here = hasPane(aid, n.fanId);
        return '<div class="pick' + (here ? ' dim' : '') + '" data-fan="' + esc(String(n.fanId)) + '">' +
          '<span class="pav rnd">' + initials(n.name) +
            (n.avatar ? '<img src="' + esc(imgProxy(n.avatar)) + '" alt="" loading="lazy" onerror="this.remove()">' : '') +
          '</span>' +
          '<div class="pmain"><div class="pnm">' + esc(n.name) + '</div>' +
          '<div class="psub">' + (n.prev ? esc(n.prev) : ('@' + esc(n.username || n.fanId))) + '</div></div>' +
          '<span class="pmeta">' + (here ? 'added' : (n.at ? esc(fmtTime(n.at)) : '')) + '</span></div>';
      }).join('');
      listWrap.innerHTML = list || '<div class="sheet-note">No matches.</div>';
    }

    sh.B.innerHTML = '<div class="sheet-search"><input id="fanSearch" placeholder="Search this creator’s fans…" autocomplete="off"></div><div id="fanList"></div>';
    var listWrap = sh.B.querySelector('#fanList');
    var search = sh.B.querySelector('#fanSearch');
    paint('');
    search.addEventListener('input', function(){ paint(search.value); });
    listWrap.addEventListener('click', function(e){
      var row = e.target.closest('.pick'); if(!row || row.classList.contains('dim')) return;
      var fan = row.getAttribute('data-fan');
      if(addPane(aid, fan)){ sh.close(); }
    });
  }

  $id('btnAdd').addEventListener('click', openCreatorPicker);

  // ═══════════════════════════════════════════════════════════
  // LIVE (SSE) — append incoming DMs to any matching open pane
  // ═══════════════════════════════════════════════════════════
  function paneFor(aid, fanId){
    return gridEl.querySelector('.pane[data-aid="' + aid + '"][data-fan="' + fanId + '"]');
  }
  function onChatMessage(env){
    var aid = env.__account_id;
    var msg = env.api2_chat_message || env.new_message || null;
    if(!aid || !msg || !msg.fromUser) return;
    var fromId = Number(msg.fromUser.id);
    if(!isFinite(fromId)) return;
    var outbound = String(fromId) === String(aid);
    var fanId = outbound ? Number(env.__fan_id != null ? env.__fan_id : (msg.toUser && msg.toUser.id)) : fromId;
    if(!isFinite(fanId)) return;
    var el = paneFor(String(aid), String(fanId));
    if(!el) return;
    var bodyEl = el.querySelector('.pane-body');
    var state = bodyEl.querySelector('.pane-state');
    if(state) bodyEl.innerHTML = '';
    bodyEl.insertAdjacentHTML('beforeend', gMsgHtml(msg, aid));
    bodyEl.scrollTop = bodyEl.scrollHeight;
    // keep only the last ~20 rendered so long-lived panes stay light
    var kids = bodyEl.querySelectorAll('.gb');
    if(kids.length > 20){ for(var i = 0; i < kids.length - 20; i++) kids[i].remove(); }
    el.querySelector('.pane-cnt').textContent = String(bodyEl.querySelectorAll('.gb').length);
  }
  function startLive(){
    Fastt.sse(function(payload, name){
      if(!payload || typeof payload !== 'object') return;
      if(name === 'api2_chat_message' || name === 'new_message') onChatMessage(payload);
    });
  }

  // ═══════════════════════════════════════════════════════════
  // SEED + HOUSE DEFAULT + BOOT
  // ═══════════════════════════════════════════════════════════
  // Deep-link: ?seed=<aid>:<fanId>,<aid>:<fanId> — other surfaces open the
  // group pre-filled. Consumed once, then stripped from the URL so a refresh
  // restores from localStorage instead of re-seeding.
  function takeSeed(){
    var url = new URL(location.href);
    var raw = url.searchParams.get('seed');
    if(!raw) return null;
    url.searchParams.delete('seed');
    history.replaceState({}, '', url.pathname + (url.search || '') + url.hash);
    var out = [], seen = {};
    raw.split(',').forEach(function(tok){
      var idx = tok.lastIndexOf(':'); if(idx <= 0) return;
      var aid = tok.slice(0, idx).trim(); var fan = Number(tok.slice(idx + 1));
      if(!aid || !isFinite(fan) || fan <= 0) return;
      var k = aid + ':' + fan; if(seen[k]) return; seen[k] = 1;
      out.push({aid: aid, fanId: fan});
    });
    return out.length ? out.slice(0, SLOT_CAP) : null;
  }

  // House default: on a first-ever visit (no stored panes, no seed), pre-open a
  // few recent chats for the current creator so the surface is immediately
  // useful. Honest empty state still shows if that creator has none.
  async function houseDefault(){
    var aid = Fastt.account();
    if(!aid) return [];
    try{
      var out = await Fastt.get('/admin/chats/recent', {limit: 100}, {noAccount:true});
      var rows = (out.list || []).filter(function(r){
        return String(r.__accountId) === String(aid) && r.withUser && r.withUser.id;
      });
      var seen = {}, picked = [];
      for(var i = 0; i < rows.length && picked.length < 3; i++){
        var fid = rows[i].withUser.id; if(seen[fid]) continue; seen[fid] = 1;
        picked.push({aid: String(aid), fanId: Number(fid)});
      }
      return picked;
    }catch(e){ return []; }
  }

  // Deep-link APPEND: ?add=<aid>:<fanId> — from the Messages kebab "Open in Group".
  // Unlike ?seed (which replaces), this adds one chat to whatever is already open.
  function takeAdd(){
    var url = new URL(location.href);
    var raw = url.searchParams.get('add'); if(!raw) return null;
    url.searchParams.delete('add');
    history.replaceState({}, '', url.pathname + (url.search || '') + url.hash);
    var idx = raw.lastIndexOf(':'); if(idx <= 0) return null;
    var aid = raw.slice(0, idx).trim(); var fan = Number(raw.slice(idx + 1));
    if(!aid || !isFinite(fan) || fan <= 0) return null;
    return {aid: aid, fanId: fan};
  }

  var hasAdd = new URL(location.href).searchParams.has('add');
  var seed = takeSeed();
  if(seed){
    panes = seed; savePanes();
    try{ localStorage.setItem(LS_INIT, '1'); }catch(e){}
  }else{
    panes = loadPanes();
    // ?add on a first visit → open ONLY the intended chat, skip the house default
    if(!panes.length && !hasAdd && localStorage.getItem(LS_INIT) !== '1'){
      panes = await houseDefault();
      savePanes();
      try{ localStorage.setItem(LS_INIT, '1'); }catch(e){}
    }
  }

  renderGrid();
  var toAdd = takeAdd();
  if(toAdd){ localStorage.setItem(LS_INIT, '1'); addPane(toAdd.aid, toAdd.fanId); }   // append + persist + re-render
  setTimeout(startLive, 2500);
});
