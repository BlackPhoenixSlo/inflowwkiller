Fastt.ready(async function(){
  var esc = Fastt.esc;
  var $id = function(id){return document.getElementById(id);};
  var threadHead = $id('threadHead');
  var threadBody = $id('threadBody');
  var composer   = $id('composer');
  var emptyState = $id('emptyState');
  var rightPanel = $id('rightPanel');
  var rowsEl     = $id('rows');
  var input      = $id('composerInput');
  var sendBtn    = $id('sendBtn');
  var aiStrip    = $id('aiStrip');
  var typingLine = $id('typingLine');
  var ctabsEl    = $id('ctabs');

  // ─────────────────────────────────────────────────────────────
  // PER-ACCOUNT REQUESTS.
  // This page shows several creators at once, so "the account" is per-REQUEST,
  // not the globally selected one. `Fastt.api({acct})` owns that rule (header +
  // /admin/ account_id); these five just default it to the active tab so call
  // sites stay short. No request logic lives here.
  // ─────────────────────────────────────────────────────────────
  function aget(path, params, aid){ return Fastt.get(path, params, {acct: aid || activeAid}); }
  function apost(path, body, aid){ return Fastt.post(path, body, {acct: aid || activeAid}); }
  function aput(path, body, aid){ return Fastt.put(path, body, {acct: aid || activeAid}); }
  function apatch(path, body, aid){ return Fastt.patch(path, body, {acct: aid || activeAid}); }
  function adel(path, aid){ return Fastt.del(path, {acct: aid || activeAid}); }

  // ── persisted UI state ────────────────────────────────────
  var LS_ORDER = 'fastt_msg_tab_order', LS_CLOSED = 'fastt_msg_tab_closed';
  var LS_ACTIVE = 'fastt_msg_active',   LS_INBOX  = 'fastt_msg_inbox',   LS_TABS = 'fastt_msg_tabs';
  var OPEN_TABS = [];    // [{aid, fanId, name}] pinned chat tabs (in-page, persisted)
  function lsGet(k, dflt){
    try{ var v = JSON.parse(localStorage.getItem(k)); return v == null ? dflt : v; }
    catch(e){ return dflt; }
  }
  function lsSet(k, v){ try{ localStorage.setItem(k, JSON.stringify(v)); }catch(e){} }

  // ── state ─────────────────────────────────────────────────
  var CREATORS = [];     // [{id,name,nickname,avatar,color,hasSession,unread}]
  var byAid    = {};
  var activeAid = null;  // creator whose inbox/thread is on screen
  // Default to the UNIFIED cross-creator list on a first visit so "all the girls
  // in one place" is the thing you land on. A stored choice wins; a deep-link /
  // pop-out forces single-creator scope below.
  var inboxMode = lsGet(LS_INBOX, 'all');  // 'creator' | 'all'
  var chats = [];        // normalised rows: {aid, fanId, name, username, avatar, text, at, unread, muted, hasUnreadTips}
  var CHATS_CACHE = {};  // listCacheKey() -> {rows, offset, hasMore, scroll, ts} — instant model/scope swap
  var LIST_TTL = 45000;  // only revalidate a cached roster if it's older than this
  var fanInfo = {};      // "aid:fanId" -> profile bits
  var spendMap = {};     // "aid:fanId" -> {spend_cents, last_purchase_at}
  var currentFan = null, currentAid = null;
  var currentChatMeta = null;    // /api/of/v2/chats/{id} for the open thread
  var attrMap = {};              // by_msg_id → who sent it (employee/automation) for the open thread
  var threadMsgs = [];           // loaded messages for the open thread
  var threadHasOlder = false;    // does the open thread have older history to page back to
  var THREAD_CACHE = {};         // key(aid,fid) -> {msgs, oldestMsgId, hasOlder} — instant re-swap (SWR)
  var THREAD_PREFETCHING = {};   // key(aid,fid) -> true while a hover-prefetch is in flight
  var THREAD_SEEDING = {};       // key(aid,fid) -> true while a local-DB seed is in flight
  var filter = null;             // null | 'unread' | 'pinned' | 'priority'
  var searchQ = null, onlineOnly = false, withTips = false, oweOnly = false;
  var chatOffset = 0, chatHasMore = false;   // single-creator inbox pagination
  var folderId = null, folderList = [];       // active OF chat-folder filter (list_id)
  // Default = the real app's "spend-unread" structuring: unread → owe-reply →
  // idle tiers, biggest spender first within each tier. That surfaces the
  // "must reply" queue at the top without hiding anything.
  var sortMode = 0;              // 0 needs-reply(tiered) · 1 newest · 2 unread first · 3 top spender · 4 hybrid
  var SORTS = ['Needs reply', 'Newest', 'Unread first', 'Top spender', 'Hybrid'];
  var attachments = [];          // [{id, thumb}] vault media ids for the next send
  var giphyId = null, scheduleOn = false;
  var freePreviews = 0;          // # of leading attachments sent UNLOCKED on a PPV
  var replyTo = null;            // {id, text} for OF native quote-reply
  var oldestMsgId = null, loadingOlder = false;  // thread history pagination
  var AV_COLORS = ['#ec4b9b','#5b8def','#67d1ae','#a78bfa','#e5a35b','#e07a5f',
                   '#4fbfda','#f0a06a','#9bd167','#d16767','#8f7fe6','#e6c14f',
                   '#57c2a6','#c76a92','#6da8f3','#b0a05b'];

  function key(aid, fid){ return String(aid) + ':' + String(fid); }

  // ── helpers ───────────────────────────────────────────────
  function stripHtml(html){
    if(!html) return '';
    try{ return (new DOMParser().parseFromString(String(html), 'text/html').body.textContent || '').trim(); }
    catch(e){ return String(html).replace(/<[^>]*>/g,'').trim(); }
  }
  function fmtTime(iso){
    var d = Fastt.parseUtc(iso);
    if(!d) return '';
    if(d.toDateString() === new Date().toDateString()){
      return d.toLocaleTimeString([], {hour:'numeric', minute:'2-digit'}).toLowerCase();
    }
    return d.toLocaleDateString([], {month:'short', day:'numeric'});
  }
  // Delegate to the shared formatter so whole dollars keep their .00 (consistency across pages).
  function money(cents){ return Fastt.fmtCents(cents); }
  var imgProxy = Fastt.imgProxy;
  function initials(name){ return esc(String(name||'?').trim().slice(0,2).toUpperCase()); }
  function isOnline(aid, fid){
    var u = fanInfo[key(aid,fid)];
    var d = u && u.lastSeen ? Fastt.parseUtc(u.lastSeen) : null;
    return !!(d && (Date.now() - d.getTime()) < 5*60*1000);
  }
  var VF_SVG   = '<svg class="vf" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9" fill="#4fbfda"/><path d="M8 12.5l2.5 2.5 5-5.5" fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>';
  var CAM_SVG  = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="8" cy="9" r="1.6"/><path d="M4 18l5-4 4 3 3-2 4 3"/></svg>';
  var MUTE_SVG = '<svg class="mute-i" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M18 8a6 6 0 0 0-9-5M6 8c0 7-3 9-3 9h13M13.7 21a2 2 0 0 1-3.4 0M3 3l18 18"/></svg>';

  // ── popover + sheet chrome ────────────────────────────────
  function closePops(){ Array.prototype.forEach.call(document.querySelectorAll('.pop'), function(p){ p.remove(); }); }
  function popover(anchor, html, onClick){
    closePops();
    var p = document.createElement('div');
    p.className = 'pop';
    p.innerHTML = html;
    document.body.appendChild(p);
    var r = anchor.getBoundingClientRect();
    var w = p.getBoundingClientRect();
    var left = Math.min(Math.max(8, r.left), window.innerWidth - w.width - 8);
    var top = r.bottom + 6;
    if(top + w.height > window.innerHeight - 8) top = Math.max(8, r.top - w.height - 6);
    p.style.left = left + 'px'; p.style.top = top + 'px';
    p.addEventListener('click', function(ev){
      var row = ev.target.closest('.prow');
      if(!row || row.classList.contains('dim')) return;
      if(onClick) onClick(row, p);
    });
    setTimeout(function(){
      var away = function(ev){
        if(p.contains(ev.target)) return;
        p.remove(); document.removeEventListener('mousedown', away);
      };
      document.addEventListener('mousedown', away);
    }, 0);
    return p;
  }
  function sheet(title, bodyHtml, footHtml){
    var back = document.createElement('div');
    back.className = 'sheet-back';
    back.innerHTML = '<div class="sheet"><div class="sheet-h"><span>' + title +
      '</span><span class="x">✕</span></div><div class="sheet-b">' + bodyHtml + '</div>' +
      (footHtml ? '<div class="sheet-f">' + footHtml + '</div>' : '') + '</div>';
    document.body.appendChild(back);
    var close = function(){ back.remove(); };
    back.addEventListener('click', function(e){ if(e.target === back || e.target.classList.contains('x')) close(); });
    back.close = close;
    back.body = back.querySelector('.sheet-b');
    back.foot = back.querySelector('.sheet-f');
    return back;
  }
  function honest(title, why, remedy){
    return '<div class="honest"><b>' + esc(title) + '</b><br>' + esc(why) +
      (remedy ? '<br><br>Remedy: <code>' + esc(remedy) + '</code>' : '') + '</div>';
  }

  // ═══════════════════════════════════════════════════════════
  // CREATOR ROSTER + TAB STRIP
  // ═══════════════════════════════════════════════════════════
  async function buildRoster(){
    var roster = Fastt.accounts() || [];
    if(!roster.length && Fastt.account()) roster = [{id: Fastt.account(), nickname: Fastt.account()}];
    CREATORS = roster.map(function(a, i){
      return {id: String(a.id), name: a.nickname || String(a.id), nickname: a.nickname || String(a.id),
              avatar: null, color: a.color || AV_COLORS[i % AV_COLORS.length],
              hasSession: null, unread: 0, owed: 0};
    });
    byAid = {};
    CREATORS.forEach(function(c){ byAid[c.id] = c; });

    // Which creators can actually talk to OnlyFans in THIS lane? A local,
    // sub-100ms check — no OF round-trip, no 404 toast storm.
    await Promise.all(CREATORS.map(async function(c){
      try{
        var s = await Fastt.get('/admin/session/status', {account_id: c.id}, {noAccount:true});
        c.hasSession = !!(s && s.loaded);
        c.remedy = (s && s.remedy) || null;
      }catch(e){ c.hasSession = false; }
    }));

    // Real display name + avatar straight from OF, per creator.
    //
    // DELIBERATELY NOT AWAITED. This is tab decoration, and awaiting it put N
    // live OF round-trips (~0.55s each) in front of the first chat row on EVERY
    // page load — the skin navigates by full page load, so that cost was paid
    // 56 pages over. The strip paints from the local /admin/accounts nicknames
    // immediately; real names and avatars swap in when OF answers. Background
    // priority so decoration can never hold a lane slot the list or the open
    // thread is queued behind.
    Promise.all(CREATORS.filter(function(c){ return c.hasSession; }).map(async function(c){
      try{
        var me = await Fastt.get('/api/of/v2/users/me', null, {acct: c.id, priority: 'background'});
        if(me && me.name) c.name = me.name;
        if(me && me.username) c.username = me.username;
        var th = me && me.avatarThumbs;
        c.avatar = (th && (th.c50 || th.c144)) || (me && me.avatar) || null;
      }catch(e){ /* keep the roster name */ }
    })).then(function(){
      // Names/avatars landed — repaint the strip, and the rows too: rowHtml
      // reads byAid for the per-creator chip in cross-creator mode.
      renderTabs();
      if(chats.length) renderRows();
    });
  }

  // The one place that maps a /admin/chats/recent row into a list row. Shared by
  // the cross-creator load and the first-paint seed so the two can never drift.
  function mapRecentRow(r){
    var wu = r.withUser || {}, lm = r.lastMessage || null;
    var lastFromFan = !!(lm && lm.fromUser && lm.fromUser.id != null &&
                         String(lm.fromUser.id) !== String(r.__accountId));
    var restricted = !!wu.isRestricted;
    return {aid: String(r.__accountId), fanId: wu.id, name: wu.name, username: wu.username,
            avatar: wu.avatar, text: lm ? stripHtml(lm.text) : '', at: lm && lm.createdAt,
            mediaCount: 0, unread: (r.unreadMessagesCount || 0) > 0 || !!r.hasUnread,
            muted: false, hasUnreadTips: false, unresponded: lastFromFan && !restricted, restricted: restricted};
  }

  // Returns the fetched rows so boot can hand the SAME payload to the list
  // seed — one local GET, two consumers. The 60s ticker ignores the return.
  async function loadUnreadCounts(){
    // ONE local-DB call covers every creator. /admin/accounts/roster-counts (the
    // real app's source) returns {"counts":{}} to an unauthed caller, so the
    // cross-account recent feed is the honest source here.
    try{
      var out = await Fastt.get('/admin/chats/recent', {limit: 100}, {noAccount:true});
      var fold = {};
      (out.list || []).forEach(function(r){
        var a = String(r.__accountId || '');
        if(!a) return;
        if(!fold[a]) fold[a] = {unread: 0, rows: 0};
        fold[a].rows++;
        if((r.unreadMessagesCount || 0) > 0 || r.hasUnread) fold[a].unread++;
        // NOTE: no owe-reply badge on tabs — /admin/chats/recent carries no
        // message direction (lastMessage = {id,text,createdAt}) and
        // roster-counts is empty for this lane, so owe is only knowable per
        // creator via /api/of/v2/chats (chip + row dots in single-creator view).
      });
      CREATORS.forEach(function(c){ c.unread = (fold[c.id] || {}).unread || 0; });
      return out.list || [];
    }catch(e){ /* badge simply stays at 0 rather than showing an invented number */ }
    return [];
  }

  function tabOrder(){
    var saved = lsGet(LS_ORDER, null);
    // default order: creators that can actually load a chat come first, so the
    // strip opens on something usable instead of eleven session-less tabs.
    // Within each group the roster order is kept — a default that reshuffles
    // itself as unread counts move would make the strip unusable.
    var ids = CREATORS.map(function(c, i){ return {c: c, i: i}; }).sort(function(a, b){
      if(!!a.c.hasSession !== !!b.c.hasSession) return a.c.hasSession ? -1 : 1;
      return a.i - b.i;
    }).map(function(x){ return x.c.id; });
    if(!Array.isArray(saved)) return ids;
    var out = saved.filter(function(id){ return byAid[id]; });
    ids.forEach(function(id){ if(out.indexOf(id) < 0) out.push(id); });
    return out;
  }
  function closedSet(){
    var c = lsGet(LS_CLOSED, []);
    return Array.isArray(c) ? c : [];
  }
  function openTabs(){
    var closed = closedSet();
    return tabOrder().filter(function(id){ return closed.indexOf(id) < 0; });
  }

  function tabHtml(c, isActive){
    var av = esc(String(c.name || c.id).slice(0,2).toUpperCase()) +
      (c.avatar ? '<img src="' + esc(imgProxy(c.avatar)) + '" alt="" onerror="this.remove()">' : '');
    var title = c.hasSession
      ? c.name + (c.username ? ' (@' + c.username + ')' : '') + ' — ' + c.unread + ' unread'
      : c.name + ' — no OnlyFans session captured for this creator in this lane';
    return '<div class="ctab' + (isActive ? ' active' : '') + (c.hasSession ? '' : ' nosess') +
      '" data-aid="' + esc(c.id) + '" draggable="true" tabindex="0" title="' + esc(title) + '">' +
      '<div class="cav" style="background:' + esc(c.color) + '">' + av + '</div>' +
      '<span class="cname">' + esc(c.name) + '</span>' +
      (c.unread ? '<span class="mini-badge pink" title="unread">' + c.unread + '</span>' : '') +
      (c.hasSession ? '' : '<span class="lock" title="No OnlyFans session captured for this creator in this lane">🔒</span>') +
      '<span class="cclose" title="Close this tab (reopen from + Add a Creator)">✕</span></div>';
  }

  function renderTabs(){
    var ids = openTabs();
    if(!ids.length){
      ctabsEl.innerHTML = '<span class="ctabs-empty">All creator tabs closed — use “+ Add a Creator”</span>';
      return;
    }
    ctabsEl.innerHTML = ids.map(function(id){
      return tabHtml(byAid[id], String(id) === String(activeAid));
    }).join('');
    // in aggregated mode the tab shows WHO you're talking as; the pink inbox
    // box is what tells you the LIST is cross-creator
    var ib = $id('inboxBox');
    ib.style.borderColor = inboxMode === 'all' ? 'var(--pink)' : '#3a3a3a';
    ib.style.background = inboxMode === 'all' ? 'var(--pink-soft)' : '';
  }

  // drag-to-reorder + ←/→ keyboard move, persisted to localStorage
  var dragId = null;
  ctabsEl.addEventListener('dragstart', function(e){
    var t = e.target.closest('.ctab'); if(!t) return;
    dragId = t.getAttribute('data-aid');
    t.classList.add('dragging');
    try{ e.dataTransfer.setData('text/plain', dragId); e.dataTransfer.effectAllowed = 'move'; }catch(err){}
  });
  ctabsEl.addEventListener('dragend', function(){
    dragId = null;
    Array.prototype.forEach.call(ctabsEl.querySelectorAll('.ctab'), function(t){
      t.classList.remove('dragging'); t.classList.remove('dropmark');
    });
  });
  ctabsEl.addEventListener('dragover', function(e){
    if(!dragId) return;
    e.preventDefault();
    var t = e.target.closest('.ctab');
    Array.prototype.forEach.call(ctabsEl.querySelectorAll('.ctab'), function(x){ x.classList.remove('dropmark'); });
    if(t) t.classList.add('dropmark');
  });
  ctabsEl.addEventListener('drop', function(e){
    if(!dragId) return;
    e.preventDefault();
    var t = e.target.closest('.ctab');
    var over = t && t.getAttribute('data-aid');
    var order = tabOrder().slice();
    var from = order.indexOf(dragId);
    if(from < 0) return;
    order.splice(from, 1);
    var to = over ? order.indexOf(over) : order.length;
    if(to < 0) to = order.length;
    order.splice(to, 0, dragId);
    lsSet(LS_ORDER, order);
    dragId = null;
    renderTabs();
  });
  function moveTab(aid, delta){
    var order = tabOrder().slice();
    var i = order.indexOf(String(aid));
    var j = i + delta;
    if(i < 0 || j < 0 || j >= order.length) return;
    var tmp = order[i]; order[i] = order[j]; order[j] = tmp;
    lsSet(LS_ORDER, order);
    renderTabs();
    var el = ctabsEl.querySelector('.ctab[data-aid="' + aid + '"]');
    if(el) el.focus();
  }
  ctabsEl.addEventListener('keydown', function(e){
    var t = e.target.closest('.ctab'); if(!t) return;
    var aid = t.getAttribute('data-aid');
    if(e.key === 'ArrowLeft'){ e.preventDefault(); moveTab(aid, -1); }
    else if(e.key === 'ArrowRight'){ e.preventDefault(); moveTab(aid, 1); }
    else if(e.key === 'Enter' || e.key === ' '){ e.preventDefault(); switchCreator(aid); }
  });
  ctabsEl.addEventListener('click', function(e){
    var t = e.target.closest('.ctab'); if(!t) return;
    var aid = t.getAttribute('data-aid');
    if(e.target.closest('.cclose')){
      var closed = closedSet().slice();
      if(closed.indexOf(aid) < 0) closed.push(aid);
      lsSet(LS_CLOSED, closed);
      var left = openTabs();
      if(String(aid) === String(activeAid) && left.length) { switchCreator(left[0]); }
      else renderTabs();
      Fastt.toast('Tab closed — reopen it from “+ Add a Creator”');
      return;
    }
    switchCreator(aid);
  });

  $id('addCreator').addEventListener('click', function(e){
    e.stopPropagation();
    var closed = closedSet();
    var rows = CREATORS.map(function(c){
      var isOpen = closed.indexOf(c.id) < 0;
      return '<div class="prow" data-aid="' + esc(c.id) + '">' +
        '<span class="pav" style="background:' + esc(c.color) + '">' +
          (c.avatar ? '<img src="' + esc(imgProxy(c.avatar)) + '" alt="">' : esc(String(c.name).slice(0,2).toUpperCase())) +
        '</span><span class="ptxt">' + esc(c.name) + '</span>' +
        '<span class="pmeta">' + (isOpen ? 'open' : 'add') + (c.hasSession ? '' : ' · no session') + '</span></div>';
    }).join('');
    popover(this,
      '<div class="phead">Creators on this relay (' + CREATORS.length + ')</div>' + rows +
      '<div class="pdiv"></div>' +
      '<div class="prow dim">Creators are captured on the relay setup page — this list is the live roster.</div>',
      function(row){
        var aid = row.getAttribute('data-aid');
        var c2 = closedSet().filter(function(x){ return x !== aid; });
        lsSet(LS_CLOSED, c2);
        closePops();
        switchCreator(aid);
      });
  });

  function switchCreator(aid){
    if(!byAid[aid]) return;
    cacheList();   // snapshot the OUTGOING creator's roster + scroll before switching
    activeAid = String(aid);
    lsSet(LS_ACTIVE, activeAid);
    inboxMode = 'creator';
    // reset the folder filter for the new creator + clear stale chips synchronously
    folderId = null; folderList = []; filter = null; oweOnly = false;
    var fcb = $id('folderChips'); if(fcb) fcb.style.display = 'none';
    setPrimaryChip('all');
    lsSet(LS_INBOX, inboxMode);
    var closed = closedSet();
    if(closed.indexOf(activeAid) >= 0) lsSet(LS_CLOSED, closed.filter(function(x){ return x !== activeAid; }));
    renderTabs();
    paintScopeChrome();
    clearSelection();
    reloadList();
    loadNotifCount();
  }

  function paintScopeChrome(){
    var c = byAid[activeAid] || {};
    $id('listTitle').textContent = inboxMode === 'all' ? 'All inboxes' : (c.name || activeAid || '—');
    $id('inboxLabel').textContent = inboxMode === 'all'
      ? 'All · ' + CREATORS.filter(function(x){ return x.hasSession; }).length + ' creators'
      : (c.name || activeAid || '—');
    $id('inboxBox').setAttribute('title', inboxMode === 'all'
      ? 'Aggregated inbox across every creator (GET /admin/chats/recent — local DB, each row labelled with its creator). Click to scope to one creator.'
      : 'Scoped to ' + (c.name || activeAid) + ' (GET /api/of/v2/chats live from OnlyFans). Click to switch creator or go cross-creator.');
    // Pinned / Priority are OnlyFans per-creator filters — they mean nothing
    // over an aggregated list, so their counts come down in "all" mode.
    if(inboxMode === 'all'){
      ['pinned','priority'].forEach(function(r){
        var c = document.querySelector('.chips .chip[data-role="' + r + '"]');
        var cb = c && c.querySelector('.cbadge');
        if(cb) cb.style.display = 'none';
      });
    }
    // Light up the "All Models" top-nav tab whenever the list is unified, and
    // drop it the moment the operator scopes to one creator (via the inbox-scope
    // popover). Keeps the nav honest about what the list is actually showing.
    var amTab = document.getElementById('tabAllModels');
    if(amTab) amTab.classList.toggle('active', inboxMode === 'all');
  }

  // ═══════════════════════════════════════════════════════════
  // INBOX SELECTOR ("All inboxes" — now real)
  // ═══════════════════════════════════════════════════════════
  $id('inboxBox').addEventListener('click', function(e){
    e.stopPropagation();
    var rows = CREATORS.map(function(c){
      return '<div class="prow' + (inboxMode === 'creator' && String(c.id) === String(activeAid) ? ' on' : '') +
        '" data-aid="' + esc(c.id) + '">' +
        '<span class="pdot" style="background:' + esc(c.color) + '"></span>' +
        '<span class="ptxt">' + esc(c.name) + '</span>' +
        '<span class="pmeta">' + (c.hasSession ? (c.unread ? c.unread + ' unread' : 'live') : 'no session') + '</span></div>';
    }).join('');
    popover(this,
      '<div class="phead">Inbox scope</div>' +
      '<div class="prow' + (inboxMode === 'all' ? ' on' : '') + '" data-aid="__all">' +
        '<span class="pdot" style="background:var(--pink)"></span><span class="ptxt">All inboxes (every creator)</span>' +
        '<span class="pmeta">local DB</span></div>' +
      '<div class="pdiv"></div><div class="phead">One creator</div>' + rows,
      function(row){
        var aid = row.getAttribute('data-aid');
        closePops();
        if(aid === '__all'){
          cacheList();   // save the outgoing creator's roster + scroll before switching to All
          inboxMode = 'all'; lsSet(LS_INBOX, inboxMode);
          folderId = null; filter = null; oweOnly = false; setPrimaryChip('all');   // back to a clean All view
          renderTabs(); paintScopeChrome(); clearSelection(); reloadList();
        }else{
          switchCreator(aid);
        }
      });
  });

  // ═══════════════════════════════════════════════════════════
  // CHAT LIST
  // ═══════════════════════════════════════════════════════════
  var SKEL_ROWS = new Array(6).join('|').split('|').map(function(){
    return '<div class="skel-row"><div class="skel-av"></div><div class="skel-lines">' +
      '<div class="skel-bar w1"></div><div class="skel-bar w2"></div></div></div>';
  }).join('');
  function setRowsLoading(msg){
    // No message => cold-load/reload: show shimmer skeleton so it reads as
    // loading (rows can take ~10-30s). Explicit msg (e.g. "No conversations
    // found") stays a plain honest line.
    if(!msg){ rowsEl.innerHTML = SKEL_ROWS; return; }
    rowsEl.innerHTML = '<div style="padding:24px 16px;color:#8a8a8a;font-size:13px">' +
      esc(msg) + '</div>';
  }
  function setRowsHonest(html){ rowsEl.innerHTML = '<div style="padding:16px">' + html + '</div>'; }

  function rowHtml(c){
    var k = key(c.aid, c.fanId);
    var u = fanInfo[k] || {};
    var name = u.name || c.name || ('Fan ' + c.fanId);
    var handle = (u.username || c.username) ? '@' + (u.username || c.username) : '';
    var sp = spendMap[k];
    var spendD = sp ? Math.round((sp.spend_cents||0)/100) : 0;
    var avUrl = u.avatar || c.avatar;
    // Initials render underneath; the photo overlays and is dropped on load-error (see CSS).
    var av = initials(name) +
      (avUrl ? '<img src="' + esc(imgProxy(avUrl)) + '" alt="" loading="lazy" decoding="async" onerror="this.remove()">' : '');
    var cre = byAid[c.aid] || {};
    var chip = inboxMode === 'all'
      ? '<span class="creator-chip"><i style="background:' + esc(cre.color || '#666') + '"></i>' +
        esc(cre.name || c.aid) + '</span>' : '';
    return '<div class="conv' + (String(c.fanId)===String(currentFan) && String(c.aid)===String(currentAid) ? ' selected' : '') +
        '" data-fan="' + esc(c.fanId) + '" data-acct="' + esc(c.aid) + '">' +
      '<div class="av-wrap"><div class="av grey">' + av + '</div>' +
        (isOnline(c.aid, c.fanId) ? '<span class="dot"></span>' : '') +
        '<span class="spend">$' + spendD + '</span></div>' +
      '<div class="conv-main">' +
        '<div class="conv-top">' + chip + '<span class="cname">' + esc(name) + '</span>' +
          (u.isVerified ? VF_SVG : '') +
          '<span class="chandle">' + esc(handle) + '</span><span class="cx">×</span></div>' +
        '<div class="conv-bot">' +
          '<span class="cprev' + (c.unread ? ' bold' : '') + '">' + (c.mediaCount ? CAM_SVG : '') + esc(c.text || '') + '</span>' +
          '<span class="ctime">' + esc(fmtTime(c.at)) + '</span>' +
          (c.unread ? '<span class="ud" title="Unread"></span>'
            : c.unresponded ? '<span class="owe-dot" title="Read · awaiting your reply"></span>'
            : (c.muted ? MUTE_SVG : '')) +
        '</div></div></div>';
  }

  function spendOf(c){ return (spendMap[key(c.aid,c.fanId)]||{}).spend_cents || 0; }
  // Tier per the real inbox: 2 = unread (blue), 1 = owe-reply (fan sent last,
  // amber), 0 = idle (we sent last / no messages).
  function tierOf(c){ return c.unread ? 2 : (c.unresponded ? 1 : 0); }
  function byNewest(a,b){ return String(b.at||'') < String(a.at||'') ? -1 : 1; }
  function sortChats(list){
    var out = list.slice();
    if(sortMode === 0){
      // "Needs reply" — tier desc, then spend desc, then newest (spend-unread).
      out.sort(function(a,b){
        var ta = tierOf(a), tb = tierOf(b);
        if(ta !== tb) return tb - ta;
        var s = spendOf(b) - spendOf(a);
        if(s) return s;
        return byNewest(a,b);
      });
    }else if(sortMode === 2){
      out.sort(function(a,b){
        if(!!b.unread !== !!a.unread) return b.unread ? 1 : -1;
        return byNewest(a,b);
      });
    }else if(sortMode === 3){
      out.sort(function(a,b){ return spendOf(b) - spendOf(a); });
    }else if(sortMode === 4){
      // "Hybrid" — spend decayed by idle time; longer half-life for chats that
      // still need attention (unread 3h → owe 2h → idle 1h). Matches the real
      // app's hybrid mode; below $3 effective, unread wins the tiebreak.
      out.sort(function(a,b){
        var as = hybridScore(a), bs = hybridScore(b);
        if(as < 300 && bs < 300){
          if(!!b.unread !== !!a.unread) return b.unread ? 1 : -1;
          var s = spendOf(b) - spendOf(a); if(s) return s;
        }
        return bs - as;
      });
    }else{
      out.sort(byNewest);
    }
    return out;
  }
  function hybridScore(c){
    var spend = spendOf(c); if(!spend) return 0;
    var d = Fastt.parseUtc(c.at); if(!d) return spend;
    var half = c.unread ? 3 : (c.unresponded ? 2 : 1);   // hours
    var hours = Math.max(0, (Date.now() - d.getTime()) / 3600000);
    return spend * Math.pow(0.5, hours / half);
  }

  function renderRows(){
    var visible = sortChats(chats.filter(function(c){
      if(onlineOnly && !isOnline(c.aid, c.fanId)) return false;
      if(withTips && !c.hasUnreadTips) return false;
      if(oweOnly && !c.unresponded) return false;   // "Owe reply" queue
      return true;
    }));
    if(!visible.length){
      setRowsLoading('No conversations found');
      return;
    }
    rowsEl.innerHTML = visible.map(rowHtml).join('');
    // Server-paginated "Load more" — single-creator scope, no client-side filter active.
    if(inboxMode === 'creator' && chatHasMore && !oweOnly && !withTips && !onlineOnly){
      rowsEl.insertAdjacentHTML('beforeend', '<div class="load-more" id="loadMore">Load more conversations</div>');
    }
  }

  function updateChipCounts(){
    var nUnread = chats.filter(function(c){ return c.unread; }).length;
    var nOwe = chats.filter(function(c){ return c.unresponded; }).length;
    function setCount(role, n, show){
      var c = document.querySelector('.chips .chip[data-role="' + role + '"]');
      var cb = c && c.querySelector('.cbadge');
      if(cb){ cb.textContent = n; cb.style.display = (n && show) ? '' : 'none'; }
    }
    setCount('unread', nUnread, !filter && !oweOnly);
    setCount('owe', nOwe, !filter);   // must-reply queue size over the loaded window
  }

  async function enrich(aid, ids){
    if(!ids.length) return;
    var csv = ids.join(',');
    var jobs = await Promise.allSettled([
      aget('/admin/fans/' + aid + '/by-ids', {ids: csv}, aid),
      aget('/admin/fans/' + aid + '/spend-batch', {ids: csv}, aid),
      Fastt.get('/api/of/v2/users/list?' + ids.map(function(i){ return 'ids=' + i; }).join('&') + '&view=m',
                null, {acct: aid, priority: 'background'}),
    ]);
    if(jobs[0].status === 'fulfilled'){
      var fans = jobs[0].value.fans || {};
      Object.keys(fans).forEach(function(fid){
        var f = fans[fid], k = key(aid, fid), prev = fanInfo[k] || {};
        fanInfo[k] = Object.assign(prev, {
          name: f.name || prev.name, username: f.username || prev.username,
          avatar: f.avatar || prev.avatar, nickname: f.customNickname || prev.nickname,
        });
      });
    }
    if(jobs[1].status === 'fulfilled'){
      var sp = jobs[1].value.spend || {};
      Object.keys(sp).forEach(function(fid){ spendMap[key(aid, fid)] = sp[fid]; });
    }
    if(jobs[2].status === 'fulfilled' && jobs[2].value && !Array.isArray(jobs[2].value)){
      var users = jobs[2].value;
      Object.keys(users).forEach(function(fid){
        var uu = users[fid] || {}, th = uu.avatarThumbs || {}, k = key(aid, fid), prev = fanInfo[k] || {};
        fanInfo[k] = Object.assign(prev, {
          name: uu.name || prev.name, username: uu.username || prev.username,
          avatar: th.c50 || th.c144 || uu.avatar || prev.avatar,
          nickname: uu.customNickname || prev.nickname,
          isVerified: !!uu.isVerified, lastSeen: uu.lastSeen || null,
        });
      });
    }
  }

  async function loadChatsOne(append){
    var c = byAid[activeAid];
    if(c && c.hasSession === false){
      chatHasMore = false;
      chats = [];
      setRowsHonest(honest(
        'No OnlyFans session for ' + (c.name || activeAid),
        'This lane has no captured OF session for account ' + activeAid + ', so /api/of/v2/* returns ' +
        '404 "Unknown account_id". Nothing is loaded for this creator — this is an empty state, not a zero.',
        'POST /admin/session/bootstrap (capture a session for ' + activeAid + ')'));
      var b = rowsEl.querySelector('.honest');
      if(b) Fastt.staticBadge(b, 'NO SESSION');
      updateChipCounts();
      return;
    }
    if(!append){ chatOffset = 0; }
    var params = {limit: 25, offset: chatOffset, order: 'recent'};
    if(filter) params.filter = filter;
    if(folderId) params.list_id = folderId;   // scope to an OF chat-folder
    if(searchQ) params.query = searchQ;
    var _scope = scopeSig();
    var resp = await aget('/api/of/v2/chats', params, activeAid);
    if(scopeSig() !== _scope) return;   // scope moved mid-flight → drop this stale load
    var batch = (resp.list || []).map(function(r){
      var wu = r.withUser || {};
      var lm = r.lastMessage || null;
      var t = lm ? stripHtml(lm.text) : '';
      // "Owe reply": the fan sent the last message and we haven't answered.
      var lastFromFan = !!(lm && lm.fromUser && lm.fromUser.id != null &&
                           String(lm.fromUser.id) !== String(activeAid));
      var restricted = !!wu.isRestricted;
      return {aid: String(activeAid), fanId: wu.id, name: wu.name, username: wu.username,
              avatar: wu.avatar, text: t, at: lm && lm.createdAt, mediaCount: lm && lm.mediaCount,
              unread: (r.unreadMessagesCount || 0) > 0, muted: !!r.isMutedNotifications,
              hasUnreadTips: !!r.hasUnreadTips, unresponded: lastFromFan && !restricted, restricted: restricted};
    });
    chats = append ? chats.concat(batch) : batch;
    chatOffset += batch.length;
    chatHasMore = !!resp.hasMore && batch.length > 0;
    var seen = {}, ids = [];
    batch.forEach(function(r){ if(r.fanId && !seen[r.fanId]){ seen[r.fanId] = 1; ids.push(r.fanId); } });
    await enrich(activeAid, ids);
    if(scopeSig() !== _scope) return;   // scope moved during enrich → don't paint/cache the wrong creator
    renderRows(); updateChipCounts();
    cacheList();
    loadFlagSets(activeAid);
    if(!append) loadFolderChips(activeAid);
  }
  var chatLoadingMore = false;
  async function loadMoreChats(){
    if(chatLoadingMore) return;   // re-entrancy guard: a 2nd click would dupe rows + skip a page
    chatLoadingMore = true;
    var btn = $id('loadMore'); if(btn){ btn.textContent = 'Loading…'; btn.classList.add('busy'); }
    try{ await loadChatsOne(true); }catch(e){ Fastt.oops(e); }
    finally{ chatLoadingMore = false; }
  }

  // OnlyFans' own per-chat flags. `filter=pinned` / `filter=priority` are the
  // only sources for these — the relay exposes no route to SET either, so the
  // star is an indicator, not a toggle.
  var pinnedSet = {}, prioritySet = {};
  async function loadFlagSets(aid){
    var jobs = await Promise.allSettled([
      aget('/api/of/v2/chats', {limit: 50, offset: 0, filter: 'pinned'}, aid),
      aget('/api/of/v2/chats', {limit: 50, offset: 0, filter: 'priority'}, aid),
    ]);
    if(String(aid) !== String(activeAid)) return;
    pinnedSet = {}; prioritySet = {};
    if(jobs[0].status === 'fulfilled'){
      (jobs[0].value.list || []).forEach(function(r){ if(r.withUser) pinnedSet[r.withUser.id] = 1; });
    }
    if(jobs[1].status === 'fulfilled'){
      (jobs[1].value.list || []).forEach(function(r){ if(r.withUser) prioritySet[r.withUser.id] = 1; });
    }
    [['pinned', Object.keys(pinnedSet).length], ['priority', Object.keys(prioritySet).length]].forEach(function(p){
      var c = document.querySelector('.chips .chip[data-role="' + p[0] + '"]');
      var cb = c && c.querySelector('.cbadge');
      if(cb){ cb.textContent = p[1]; cb.style.display = (p[1] && inboxMode === 'creator') ? '' : 'none'; }
    });
    paintStar();
  }
  function paintStar(){
    var b = $id('tbStar');
    if(!b) return;
    var pinned = !!(currentFan && pinnedSet[currentFan]);
    var prio = !!(currentFan && prioritySet[currentFan]);
    var col = pinned ? 'var(--pink)' : prio ? 'var(--blue)' : '#4a4a4a';
    b.style.color = col;
    var svg = b.querySelector('svg');
    if(svg){ svg.setAttribute('fill', col); svg.setAttribute('stroke', col); }
    b.setAttribute('title',
      (pinned ? 'PINNED on OnlyFans' : prio ? 'PRIORITY on OnlyFans' : 'Not pinned or priority on OnlyFans') +
      ' — read-only: this relay exposes no route that pins or prioritises a chat, only the ' +
      'filters that read the state (/api/of/v2/chats?filter=pinned|priority).');
  }

  async function loadChatsAll(){
    chatHasMore = false;   // aggregate feed isn't server-paginated here
    folderId = null; $id('folderChips').style.display = 'none';   // folders are per-creator
    var _scope = scopeSig();
    var out = await Fastt.get('/admin/chats/recent', {limit: 100}, {noAccount:true});
    if(scopeSig() !== _scope) return;   // scope moved mid-flight → drop this stale load
    var rows = (out.list || []).filter(function(r){ return r.withUser && r.withUser.id; });
    chats = rows.map(mapRecentRow);
    renderRows(); updateChipCounts();
    // spend chips per creator present (local DB, cheap) — names already ride the rows
    var byA = {};
    chats.forEach(function(r){ (byA[r.aid] = byA[r.aid] || []).push(r.fanId); });
    await Promise.all(Object.keys(byA).map(async function(aid){
      try{
        var sp = await aget('/admin/fans/' + aid + '/spend-batch', {ids: byA[aid].join(',')}, aid);
        Object.keys(sp.spend || {}).forEach(function(fid){ spendMap[key(aid, fid)] = sp.spend[fid]; });
      }catch(e){}
    }));
    if(scopeSig() !== _scope) return;   // scope moved during spend fetch → don't paint/cache the wrong scope
    renderRows();
    cacheList();
  }

  // Only the DEFAULT view (no filter/folder/search) is cached — that's the
  // model/scope swap the operator does constantly. Filtered/searched views
  // always load fresh (they're transient).
  function listCacheKey(){
    if(filter || folderId || searchQ) return null;
    return inboxMode === 'all' ? '__all__' : ('a:' + activeAid);
  }
  // Full scope signature — used to bail a roster load whose scope moved while it
  // was in flight (a chat-list GET can take seconds; the operator may swap again
  // before it resolves). Mirrors loadThread's fan+account guard.
  function scopeSig(){ return inboxMode + '|' + activeAid + '|' + (filter || '') + '|' + (folderId || '') + '|' + (searchQ || ''); }
  function cacheList(){
    var ck = listCacheKey(); if(!ck) return;
    CHATS_CACHE[ck] = { rows: chats.slice(), offset: chatOffset, hasMore: chatHasMore,
                        scroll: rowsEl.scrollTop, ts: Date.now() };
  }
  // First-paint seed for the boot list: pre-warm CHATS_CACHE with the local-DB
  // rows loadUnreadCounts already fetched, stamped ts:0 so it reads as STALE.
  // reloadList's own instant-swap branch then does everything — paints the rows
  // with no skeleton, and (because the entry is stale) always revalidates
  // against OF. No new render path, no mode flag: the seed is just a cache
  // entry that arrived before its first read.
  //
  // Honesty: rows and their unread state come from the same mirror the creator
  // tabs and the whole cross-creator list already trust. What the mirror can't
  // answer stays absent — no owe dots (no message direction in this feed, so
  // unresponded=false and the owe chip stays hidden), no pin/priority
  // (loadFlagSets owns those), and hasMore:false so "Load more" waits for OF's
  // cursor.
  function seedListFromRecent(rows){
    var ck = listCacheKey();
    if(!ck || CHATS_CACHE[ck] || chats.length) return;   // scoped view, or a real load already won
    var mapped = (rows || []).filter(function(r){ return r.withUser && r.withUser.id; }).map(mapRecentRow);
    if(inboxMode !== 'all'){
      // A session-less creator is about to get loadChatsOne's honest "no OF
      // session for this lane" panel. Seeding local rows in front of it would
      // contradict the very next paint, so this scope keeps its skeleton.
      var cre = byAid[activeAid];
      if(cre && cre.hasSession === false) return;
      mapped = mapped.filter(function(r){ return String(r.aid) === String(activeAid); });
    }
    if(!mapped.length) return;                           // nothing local → leave the skeleton up
    CHATS_CACHE[ck] = { rows: mapped, offset: 0, hasMore: false, scroll: 0, ts: 0 };
  }

  // force=true → an explicit refresh / post-mutation reload (Reload button, Hide,
  // Mark-unread, a new-inbound SSE): still paint the cache for instant feel, but
  // ALWAYS revalidate (never short-circuit on the TTL). force=false is the plain
  // scope swap, which may skip the refetch while the cache is fresh.
  async function reloadList(force){
    // Instant swap: paint the cached roster + restore scroll immediately, then
    // revalidate in the background (unless it's still fresh). No blank skeleton.
    var ck = listCacheKey(), cached = ck && CHATS_CACHE[ck];
    if(cached && cached.rows.length){
      chats = cached.rows.slice(); chatOffset = cached.offset; chatHasMore = cached.hasMore;
      renderRows(); updateChipCounts();
      rowsEl.scrollTop = cached.scroll || 0;
      // Refresh the creator-scoped chrome (pin/priority stars, folder chips) in
      // the background — switchCreator reset those globals, so a cached paint
      // would otherwise drop them. Cheap + non-blocking; the roster stays instant.
      if(inboxMode !== 'all'){ loadFlagSets(activeAid); loadFolderChips(activeAid); }
      if(!force && Date.now() - cached.ts < LIST_TTL) return;   // fresh + not forced → no refetch
      try{
        if(inboxMode === 'all') await loadChatsAll();
        else await loadChatsOne();
        rowsEl.scrollTop = cached.scroll || 0;   // keep the operator where they were
      }catch(e){ /* keep the cached view on a failed revalidate */ }
      return;
    }
    setRowsLoading();
    try{
      if(inboxMode === 'all') await loadChatsAll();
      else await loadChatsOne();
    }catch(e){
      chats = [];
      setRowsHonest(honest('Could not load conversations',
        'The relay returned ' + ((e && e.status) || 'an error') + ' for this scope.',
        'check the relay at ' + location.origin));
      console.warn('chat list', e);
    }
  }

  // ═══════════════════════════════════════════════════════════
  // THREAD
  // ═══════════════════════════════════════════════════════════
  function setPresence(lastSeen){
    var st = $id('thStatus');
    var d = lastSeen ? Fastt.parseUtc(lastSeen) : null;
    var online = d && (Date.now() - d.getTime()) < 5*60*1000;
    if(online){
      st.classList.remove('off');
      st.innerHTML = '<span class="sdot"></span>Available now';
    }else{
      st.classList.add('off');
      st.innerHTML = '<span class="sdot"></span>' + (lastSeen ? 'Last seen ' + esc(Fastt.fmtAgo(lastSeen)) : 'Offline');
    }
  }

  function msgHtml(m){
    var out = String((m.fromUser && m.fromUser.id) || '') === String(currentAid);
    var text = stripHtml(m.text);
    var t = fmtTime(m.createdAt);
    var price = Number(m.price) || 0;
    var media = m.media || [];
    var thumb = '';
    var first = media[0];
    if(first && first.canView && first.files){
      var f = first.files.thumb || first.files.squarePreview || first.files.preview;
      if(f && f.url){
        // For a video the "full" file is the raw video (would break an <img>) —
        // use a large poster frame. Photos use the full-res image.
        var full = (first.type === 'video')
          ? (first.files.squarePreview || first.files.preview || f)
          : (first.files.full || first.files.squarePreview || first.files.preview || f);
        thumb = '<img class="msg-media" data-full="' + esc((full && full.url) || f.url) + '" src="' + esc(imgProxy(f.url)) +
          '" style="max-width:100%;border-radius:8px;display:block;cursor:zoom-in" alt="" loading="lazy" decoding="async">';
      }
    }
    var mid = m.id ? ' data-mid="' + esc(m.id) + '"' : '';
    var pin = m.isPinned ? ' msg-pinned' : '';
    // Hover actions: unsend our own messages, like the fan's. Both are gated
    // (unsend confirms; like is a deliberate click) in the delegated handler.
    var acts = '';
    if(m.id){
      acts = '<span class="msg-acts">' +
        '<button class="ma" data-act="reply" title="Reply to this message">↩</button>' +
        (out ? '<button class="ma" data-act="unsend" title="Unsend for everyone">⟲</button>'
             : '<button class="ma' + (m.isLiked ? ' on' : '') + '" data-act="like" title="Like this message">♥</button>') +
        '</span>';
    }
    if(price > 0){
      var paid = !!m.isOpened;
      var title = text || ('📦 ' + (m.mediaCount || media.length || 0) + ' media');
      return '<div class="ppv-mini' + pin + '"' + mid + (out ? '' : ' style="align-self:flex-start"') + '>' + acts + thumb +
        '<div class="pm-title">' + esc(title) + '</div>' +
        '<div class="paid-tag"' + (paid ? '' : ' style="color:var(--muted)"') + '>' +
          Fastt.fmtMoney(price) + ' · ' + (paid ? 'paid' : 'not paid') + ' · ' + esc(t) +
        '</div></div>';
    }
    var tip = m.isTip ? '<div class="paid-tag">💸 ' + Fastt.fmtMoney(Number(m.tipAmount)||0) + ' tip' +
      (!out && m.id ? ' <button class="tip-reward" data-tipreward="' + esc(m.id) + '" data-tipcents="' +
        Math.round((Number(m.tipAmount)||0)*100) + '" title="Send the tip-reward for this tip">🎁 reward</button>' : '') +
      '</div>' : '';
    var seenTick = (out && m.id) ? '<span class="seen-tick">✓</span>' : '';
    return '<div class="bubble ' + (out ? 'out' : 'in') + pin + '"' + mid + '>' + acts + thumb + tip + esc(text) +
      '<span class="btime">' + esc(t) + seenTick + '</span></div>';
  }
  function paintSeenReceipts(){
    var lr = currentChatMeta && currentChatMeta.lastReadMessageId;
    var lrn = lr ? Number(lr) : null;   // no meta yet → leave every tick at ✓ (sent)
    Array.prototype.forEach.call(threadBody.querySelectorAll('.bubble.out[data-mid]'), function(el){
      var mid = Number(el.getAttribute('data-mid'));
      var tick = el.querySelector('.seen-tick'); if(!tick || !isFinite(mid)) return;
      // idempotent: re-marks AND un-marks so a stale paint can't leave a false ✓✓
      if(lrn != null && mid <= lrn){ tick.textContent = '✓✓'; tick.classList.add('seen'); }
      else { tick.textContent = '✓'; tick.classList.remove('seen'); }
    });
  }

  // "Sent by" attribution — who (which chatter / which automation) sent each
  // outbound message. From /admin/messages/{aid}/{fid}/attribution (by_msg_id);
  // only system-sent messages carry it, so manual sends simply show no chip.
  // Idempotent paint over the existing bubbles, same shape as paintSeenReceipts.
  function paintAttribution(){
    Array.prototype.forEach.call(threadBody.querySelectorAll('.bubble.out[data-mid]'), function(el){
      var a = attrMap[el.getAttribute('data-mid')];
      if(!a || el.querySelector('.sent-by')) return;   // no attribution, or already painted
      var name = a.display_name || a.sender_name || (a.employee_id != null ? ('#' + a.employee_id) : 'system');
      var chip = document.createElement('span');
      chip.className = 'sent-by';
      if(a.color) chip.style.color = a.color;
      chip.title = 'Sent by ' + name + (a.automation_kind ? ' · ' + a.automation_kind : '');
      chip.textContent = (a.automation_kind ? '🤖 ' : '👤 ') + name;
      var bt = el.querySelector('.btime');
      if(bt) bt.appendChild(chip); else el.appendChild(chip);
    });
  }
  async function loadAttribution(fid, aid){
    try{
      var out = await aget('/admin/messages/' + aid + '/' + fid + '/attribution', null, aid);
      if(String(currentFan) !== String(fid) || String(currentAid) !== String(aid)) return;
      attrMap = (out && out.by_msg_id) || {};
      paintAttribution();
    }catch(e){ /* attribution is decoration — leave bubbles unlabelled */ }
  }

  // Signature of the rendered thread — length + first/last id + the per-message
  // render-affecting state (paid / liked / pinned). Lets a background revalidate
  // skip the repaint when NOTHING that shows changed (no flicker, no scroll yank
  // on swap-back), while still catching a state flip that keeps the same count +
  // edge ids — e.g. the core sell→buy loop flips isOpened on the last bubble
  // without adding a message.
  function threadSig(msgs){
    if(!msgs.length) return '0';
    var s = msgs.length + ':' + msgs[0].id + '-' + msgs[msgs.length - 1].id + '#';
    for(var i = 0; i < msgs.length; i++){ var m = msgs[i];
      if(m.isOpened) s += 'o' + i + ';';
      if(m.isLiked)  s += 'l' + i + ';';
      if(m.isPinned) s += 'p' + i + ';';
    }
    return s;
  }
  // Snapshot the open thread into the per-fan cache so re-selecting it paints
  // instantly. Stores a COPY so a later push()/filter() on threadMsgs can't
  // mutate the cached view.
  function cacheThread(fid, aid){
    if(fid == null) return;
    THREAD_CACHE[key(aid, fid)] = { msgs: threadMsgs.slice(), oldestMsgId: oldestMsgId, hasOlder: threadHasOlder };
  }
  function paintThread(msgs, hasOlder){
    threadMsgs = msgs;
    oldestMsgId = msgs.length ? msgs[0].id : null;
    threadHasOlder = !!hasOlder;
    var html = msgs.length ? msgs.map(msgHtml).join('') : '<div class="msg-meta">No messages yet</div>';
    // older history exists ⇒ offer to page back (gate on hasMore, since OF
    // ignores the limit param and can return fewer than 30 rows).
    if(hasOlder) html = '<div class="load-older" id="loadOlder">↑ Load older messages</div>' + html;
    threadBody.innerHTML = html;
    threadBody._sig = threadSig(msgs);
    threadBody.scrollTop = threadBody.scrollHeight;
    renderPinBar();
    paintSeenReceipts();   // meta may already be loaded (loadChatMeta races ahead)
    paintAttribution();    // attribution may already be loaded (revalidate / cached paint)
  }

  async function loadThread(fid, aid, refresh){
    try{
      var resp = await aget('/api/of/v2/chats/' + fid + '/messages',
                            {limit: 30, order: 'desc', refresh: refresh ? 'true' : undefined}, aid);
      // Guard BOTH fan and account — the same OF fan can be open under two
      // managed creators in 'all' mode, and a stale response must not paint the
      // wrong creator's thread.
      if(String(currentFan) !== String(fid) || String(currentAid) !== String(aid)) return;
      var msgs = (resp.list || []).slice().reverse();
      // Monotonicity guard: never let an OLDER server snapshot clobber a live
      // view that already advanced. If an SSE inbound or an optimistic send
      // landed while this GET was in flight, the on-screen last message is
      // newer than (or absent from) the server snapshot — keep the live view,
      // just refresh hasOlder + cache. (curLast.id == null ⇒ an optimistic
      // bubble with no server id yet, which we must never drop.)
      var curLast = threadMsgs.length ? threadMsgs[threadMsgs.length - 1] : null;
      var srvLastId = msgs.length ? msgs[msgs.length - 1].id : null;
      var viewAhead = !!curLast && (curLast.id == null || srvLastId == null || Number(curLast.id) > Number(srvLastId));
      if(viewAhead){
        threadHasOlder = !!resp.hasMore;
        // If a forced refresh replaced the DOM with a placeholder, restore the
        // (ahead) live view over it; otherwise leave the DOM untouched.
        if(threadBody._sig == null) paintThread(threadMsgs.slice(), threadHasOlder);
        cacheThread(fid, aid);
        return;
      }
      // Background revalidate over an identical cached paint: refresh the model
      // + cache but leave the DOM untouched (the "instant swap" path). A forced
      // refresh always repaints so it can never get stuck on a placeholder.
      if(!refresh && threadBody._sig === threadSig(msgs)){
        threadMsgs = msgs;
        oldestMsgId = msgs.length ? msgs[0].id : null;
        threadHasOlder = !!resp.hasMore;
        cacheThread(fid, aid);
        return;
      }
      paintThread(msgs, resp.hasMore);
      cacheThread(fid, aid);
    }catch(e){
      // Only surface an error when nothing is on screen yet (cold load). A
      // failed background revalidate must never wipe a good cached view.
      if(String(currentFan) === String(fid) && String(currentAid) === String(aid) && threadBody._sig == null){
        threadBody.innerHTML = '<div class="msg-meta">Could not load messages (' + esc(String((e&&e.status)||'error')) + ')</div>';
      }
      console.warn('thread', e);
    }
  }

  // ── local-DB thread seed ──────────────────────────────────
  // /admin/messages/{aid}/{fid} is the WS-transcoder's SQLite mirror of this
  // thread: ~0.1ms to query, not lane-bound, and the exact source the real
  // app's chat pane paints from (app/hooks/useChatMessagesLocal.ts). The live
  // OF head page is deliberately uncached at the relay (new DMs land there), so
  // a cold open sat on "Loading messages…" for the full OF round-trip — p50
  // 1.5s, p95 5.3s, measured. This paints real history in the meantime.
  //
  // THE SEED IS DOM-ONLY. It writes bubbles into threadBody and nothing else:
  // threadMsgs / oldestMsgId / threadHasOlder / THREAD_CACHE only ever hold
  // OF-derived data. That one rule is what keeps every existing guard correct
  // without knowing seeds exist — loadThread's monotonicity check sees an empty
  // model (never "ahead"), loadOlder has no cursor to page from, cacheThread
  // can only snapshot real windows, and an SSE inbound appends over the seed
  // exactly as it appends over the loading placeholder.
  //
  // The one mark it leaves is threadBody._sig = SEED_SIG — non-null so a failed
  // OF load doesn't wipe real history with an error line, yet impossible as
  // threadSig() output ('0' or 'len:first-last#…'), so the identical-paint
  // shortcut can never mistake the media-less seed for the real window and
  // skip the repaint that brings the media in.
  //
  // Not a badge case: these are the same messages, from the same mirror the
  // real app trusts. What IS withheld is anything the mirror cannot answer
  // honestly — media, the load-older cursor, and the "no messages" verdict.
  var SEED_SIG = 'seed';
  var SEED_MASS_MIN = 5e15;   // optimistic mass-send placeholder band starts here
  var SEED_MASS_END = 6e15;   // ledger-synthesized TIP rows start here
  var SEED_MASS_MAX_AGE_MS = 60 * 60 * 1000;
  function seedRowToMsg(r, aid, fid){
    var isOut = r.direction === 'out';
    var cents = Number(r.price_cents) || 0;
    // A tip carries its amount in tipAmount, not price: msgHtml tests `price > 0`
    // FIRST, so a tip that arrived as a price would render as an unpaid PPV box.
    // Ledger-synthesized tips are already written with price_cents = 0 and say
    // the amount in their body, so a zero-cent tip stays a plain bubble rather
    // than growing a "$0.00 tip" chip it cannot substantiate.
    var isTip = !!r.is_tip && cents > 0;
    return {
      id: r.message_id,
      // msgHtml picks the bubble side by comparing fromUser.id against the open
      // creator; carry the authoritative `direction` column through that shape.
      fromUser: {id: isOut ? Number(aid) : Number(fid), name: r.sender_name || ''},
      text: r.body || '',
      createdAt: r.created_at,
      media: [],                                   // no OF signed URLs in the mirror — OF fills these in
      mediaCount: Number(r.media_count) || 0,
      price: isTip ? 0 : cents / 100,
      isTip: isTip,
      tipAmount: isTip ? cents / 100 : 0,
      // The renderer reads OF's `isOpened`; `is_paid` is the ledger's own
      // confirmation, which lands minutes before OF echoes the unlock.
      isOpened: !!r.is_paid
    };
  }
  // Mirror rows (newest-first) → bubbles (oldest-first), with the band rules
  // applied in ONE place for both seeds.
  function seedRowsToMsgs(rows, aid, fid){
    var msgs = [];
    for(var i = rows.length - 1; i >= 0; i--){
      var r = rows[i], id = Number(r.message_id);
      if(!isFinite(id) || id <= 0) continue;
      if(r.is_unsent) continue;                  // gone from OF; would repaint as an empty locked shell
      // Ids at or above SEED_MASS_END are ledger-synthesized tips: permanent
      // local history that the OF window does NOT contain, and whose synthetic
      // ids sit above every real OF id. Seeding them would show a tip that
      // vanishes seconds later when OF's window lands, and would hand the
      // monotonicity guard an id no OF response can ever match. The real app
      // renders them because its cache is the merge point; this pane's merge
      // point is OF, so they stay out.
      if(id >= SEED_MASS_END) continue;
      // Mass-send placeholders only bridge the send→delivery window. Past an
      // hour they are a dead reconcile or an unsent broadcast.
      if(id >= SEED_MASS_MIN){
        var at = Fastt.parseUtc(r.created_at);
        if(!at || (Date.now() - at.getTime()) > SEED_MASS_MAX_AGE_MS) continue;
      }
      msgs.push(seedRowToMsg(r, aid, fid));
    }
    return msgs;
  }

  async function seedThread(fid, aid){
    var k = key(aid, fid);
    if(THREAD_SEEDING[k]) return;
    THREAD_SEEDING[k] = true;
    try{
      var out = await aget('/admin/messages/' + aid + '/' + fid, {limit: 30}, aid);
      // The operator moved on, or something authoritative already painted —
      // an OF response, a cached window, or an SSE inbound. All of those
      // outrank the seed; _sig is null only while the pane is still empty.
      if(String(currentFan) !== String(fid) || String(currentAid) !== String(aid)) return;
      if(threadBody._sig != null) return;
      var msgs = seedRowsToMsgs((out && out.messages) || [], aid, fid);
      // Nothing local → leave "Loading messages…" standing. "No messages yet"
      // is a verdict only a real OF response gets to deliver.
      if(!msgs.length) return;
      threadBody.innerHTML = msgs.map(msgHtml).join('');   // DOM only — no model writes
      threadBody._sig = SEED_SIG;
      threadBody.scrollTop = threadBody.scrollHeight;
      paintSeenReceipts();   // meta may already be loaded (loadChatMeta races ahead)
      paintAttribution();    // attribution is DOM-keyed, so seed bubbles take chips too
    }catch(e){ /* best-effort bridge — loadThread is the real load */ }
    finally{ delete THREAD_SEEDING[k]; }
  }

  // The older-page twin of seedThread, and the same contract: DOM ONLY. Every
  // scroll-up used to sit on a live OF round-trip (p50 1.5s) with nothing new
  // on screen while the very same history sat in the mirror, answerable in ~1ms.
  //
  // What it must never do is speak for OF. `oldestMsgId` and `threadHasOlder`
  // stay exactly where loadOlder's response puts them, so a mirror that runs
  // deeper than OF's window cannot page the cursor into a gap or promise
  // history OF won't serve. The bands need no age rule here: the cursor is an
  // id OF itself gave us, and the mirror filters on `message_id < cursor`,
  // while every synthetic id (5e15 placeholders, 6e15 ledger tips) sits three
  // orders of magnitude above the largest real OF id — so they cannot come
  // back through this door.
  async function seedOlder(fid, aid, cursor){
    try{
      var out = await aget('/admin/messages/' + aid + '/' + fid,
                           {limit: 30, before_id: cursor}, aid);
      if(String(currentFan) !== String(fid) || String(currentAid) !== String(aid)) return;
      // OF's page already landed (it repaints the pane and lowers the cursor)
      // or the request settled some other way — either way this is stale.
      if(!loadingOlder || oldestMsgId !== cursor) return;
      var msgs = seedRowsToMsgs((out && out.messages) || [], aid, fid);
      // The DOM is the record of what is already on screen. An errored OF page
      // leaves seeded bubbles standing and the user can click "Load older"
      // again, so dedupe against what is painted rather than trusting the
      // cursor — that is what keeps a retry from doubling the history.
      msgs = msgs.filter(function(m){
        return !threadBody.querySelector('[data-mid="' + m.id + '"]');
      });
      if(!msgs.length) return;
      var prevH = threadBody.scrollHeight, prevTop = threadBody.scrollTop;
      var btn = $id('loadOlder');
      var html = msgs.map(msgHtml).join('');
      // Insert BELOW the affordance so "↑ Load older messages" stays the top
      // row — OF is still fetching, and the button is still the live control.
      if(btn) btn.insertAdjacentHTML('afterend', html);
      else threadBody.insertAdjacentHTML('afterbegin', html);
      threadBody.scrollTop = threadBody.scrollHeight - prevH + prevTop;   // hold the anchor row
      paintSeenReceipts();
      paintAttribution();
    }catch(e){ /* best-effort bridge — loadOlder is the real load */ }
  }

  // Hover-prefetch: warm the per-fan cache after a short dwell so the FIRST
  // click paints instantly. Best-effort, read-only (never marks read / sends),
  // deduped against the cache and in-flight set so a mouse sweep costs at most
  // one GET per row you actually pause on.
  async function prefetchThread(fid, aid){
    if(fid == null) return;
    var k = key(aid, fid);
    if(THREAD_CACHE[k] || THREAD_PREFETCHING[k]) return;
    THREAD_PREFETCHING[k] = true;
    try{
      var resp = await Fastt.get('/api/of/v2/chats/' + fid + '/messages',
                                 {limit: 30, order: 'desc'},
                                 {acct: aid || activeAid, priority: 'background'});
      var msgs = (resp.list || []).slice().reverse();
      THREAD_CACHE[k] = { msgs: msgs, oldestMsgId: msgs.length ? msgs[0].id : null, hasOlder: !!resp.hasMore };
    }catch(e){ /* prefetch is best-effort */ }
    finally{ delete THREAD_PREFETCHING[k]; }
  }

  // Page back through older messages (OF cursor = before_id). Prepends and
  // preserves the scroll anchor so the view doesn't jump.
  async function loadOlder(){
    if(loadingOlder || !oldestMsgId || !currentFan) return;
    var fid = currentFan, aid = currentAid, cursor = oldestMsgId;   // pin the target at call time
    loadingOlder = true;
    var btn = $id('loadOlder'); if(btn){ btn.textContent = 'Loading…'; btn.classList.add('busy'); }
    seedOlder(fid, aid, cursor);   // parallel, DOM-only; OF below stays the truth
    try{
      var resp = await aget('/api/of/v2/chats/' + fid + '/messages',
                            {limit: 30, order: 'desc', before_id: cursor}, aid);
      // fan switched mid-flight → drop this result, don't corrupt the open thread
      if(String(currentFan) !== String(fid) || String(currentAid) !== String(aid)) return;
      var older = (resp.list || []).slice().reverse();
      if(!older.length){
        if(btn){ btn.textContent = '— beginning of conversation —'; btn.removeAttribute('id'); btn.classList.remove('busy'); }
        oldestMsgId = null;   // terminal: no more history, don't re-fetch
        threadHasOlder = false; cacheThread(fid, aid);
        return;
      }
      var prevH = threadBody.scrollHeight, prevTop = threadBody.scrollTop;
      threadMsgs = older.concat(threadMsgs);
      oldestMsgId = older[0].id;
      // gate on the payload's hasMore (OF ignores our limit and may return < 30)
      var html = (resp.hasMore ? '<div class="load-older" id="loadOlder">↑ Load older messages</div>' : '') +
                 threadMsgs.map(msgHtml).join('');
      threadBody.innerHTML = html;
      threadBody._sig = threadSig(threadMsgs);
      threadBody.scrollTop = threadBody.scrollHeight - prevH + prevTop;   // keep the anchor row in place
      renderPinBar();
      paintSeenReceipts();
      paintAttribution();   // label the newly-revealed older bubbles
      threadHasOlder = !!resp.hasMore; cacheThread(fid, aid);
    }catch(e){ Fastt.oops(e); }
    finally{ loadingOlder = false; }
  }

  async function loadChatMeta(fid, aid){
    try{
      currentChatMeta = await aget('/api/of/v2/chats/' + fid, null, aid);
      if(String(currentFan) !== String(fid)) return;
      paintMuteBtn();
      renderPinBar();
      paintSeenReceipts();
    }catch(e){ currentChatMeta = null; }
  }
  function paintMuteBtn(){
    var b = $id('tbMute');
    var muted = !!(currentChatMeta && currentChatMeta.isMutedNotifications);
    b.style.color = muted ? 'var(--pink)' : '';
    b.setAttribute('title', muted ? 'Notifications MUTED for this chat — click to unmute'
                                  : 'Notifications on — click to mute this chat');
  }

  // ── Pinned bar: the real app's PinnedPanel, sourced from the SAME loaded
  // window (message.isPinned) — no extra OF fetch, exactly like the original. A
  // fan can have pins that live above the loaded window; the bar says so.
  function jumpToMsg(mid){
    var el = threadBody.querySelector('[data-mid="' + mid + '"]');
    if(el){
      el.scrollIntoView({block:'center'});
      el.style.outline = '2px solid var(--pink)'; el.style.outlineOffset = '2px';
      setTimeout(function(){ el.style.outline = ''; }, 2200);
    }else{
      Fastt.toast('That pinned message is older than the loaded window — press ↻ to refresh');
    }
  }
  function renderPinBar(){
    var bar = $id('pinBar');
    var pins = threadMsgs.filter(function(m){ return m.isPinned; });
    var totalOnOf = (currentChatMeta && currentChatMeta.countPinnedMessages) || 0;
    if(!pins.length && !totalOnOf){ bar.classList.add('hidden'); return; }
    bar.classList.remove('hidden');
    if(!pins.length){
      // OF reports pins exist but none are inside the loaded window.
      bar.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M12 17v5M9 3h6l-1 7 3 2H7l3-2z"/></svg>' +
        '<span class="pintxt">' + totalOnOf + ' pinned message' + (totalOnOf===1?'':'s') +
        ' on OnlyFans — older than the loaded window</span>' +
        '<span class="pincount">↻</span>';
      return;
    }
    var top = pins[pins.length-1];
    bar.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M12 17v5M9 3h6l-1 7 3 2H7l3-2z"/></svg>' +
      '<span class="pintxt">' + esc(stripHtml(top.text) || (top.price ? Fastt.fmtMoney(top.price)+' PPV' : '(media)')) + '</span>' +
      '<span class="pincount">' + pins.length + ' pinned ▾</span>';
  }
  $id('pinBar').addEventListener('click', function(e){
    e.stopPropagation();
    var pins = threadMsgs.filter(function(m){ return m.isPinned; });
    if(!pins.length){ $id('tbRefresh').click(); return; }
    popover(this, '<div class="phead">Pinned in this chat (' + pins.length + ')</div>' +
      pins.slice().reverse().map(function(m){
        return '<div class="prow" data-mid="' + esc(m.id) + '"><span class="ptxt">' +
          esc(stripHtml(m.text) || (m.price ? Fastt.fmtMoney(m.price)+' PPV' : '(media)')).slice(0,90) +
          '</span><span class="pmeta">' + esc(fmtTime(m.createdAt)) + '</span></div>';
      }).join('') +
      '<div class="pdiv"></div><div class="prow" data-unpin="' + esc(pins[pins.length-1].id) +
        '"><span class="ptxt">Unpin the latest</span></div>',
      function(row){
        var un = row.getAttribute('data-unpin');
        closePops();
        if(un){
          if(!confirm('Unpin this message in the OnlyFans chat?')) return;
          adel('/api/of/v2/messages/' + un + '/pin/user/' + currentFan, currentAid)
            .then(function(){ Fastt.saved('Unpinned'); loadThread(currentFan, currentAid, true); loadChatMeta(currentFan, currentAid); })
            .catch(Fastt.oops);
          return;
        }
        jumpToMsg(row.getAttribute('data-mid'));
      });
  });

  // ── AI status strip ───────────────────────────────────────
  var AI_TONE = {active:'st-active', paused:'st-paused', blocked:'st-blocked', companion:'st-companion', off:'st-off'};
  var AI_DOT  = {active:'●', paused:'💤', blocked:'🚫', companion:'💬', off:'⭘'};
  var GATE_WHY = {
    low_information:"His last message was too thin to price against — she keeps talking, she just won't price noise.",
    we_spoke_last:"We spoke last. The offer is PARKED and fires on his next real message.",
    stale:"His last message is too old for a live ask. Parked, not deleted.",
    declined:"He turned an offer down — no new price follows a 'no'.",
    tapped:"He ignored the last ask, so the ladder tapped out. It decays.",
    hard_stop:"He threatened a chargeback or told her to stop. Operator-only unblock.",
    offers_paused:"He said he's broke — no cheaper counter-offer. A buy he starts is still honoured.",
    no_inbound:"He hasn't said anything yet.",
    rung_gap:"The last ask was seconds ago — she won't stack a second price on it.",
    near_bedtime:"Too close to her sleep window to open a ladder.",
    fan_daily_asks:"He's hit his daily limit on number of asks.",
    fan_daily_cents:"He's hit his daily limit on how much he's been asked for.",
    account_hourly:"The account hit its offers-per-hour ceiling.",
    account_daily:"The account hit its offers-per-day ceiling."
  };
  async function loadAiStatus(fid, aid){
    aiStrip.style.display = '';
    aiStrip.innerHTML = '<span>🤖 AI status…</span>';
    var d;
    try{ d = await aget('/admin/fans/' + aid + '/' + fid + '/ai-status', null, aid); }
    catch(e){
      if(String(currentFan) !== String(fid)) return;
      aiStrip.innerHTML = '<span class="st-blocked">⚠ AI status unavailable (' + esc(String((e&&e.status)||'error')) + ')</span>';
      return;
    }
    if(String(currentFan) !== String(fid)) return;
    var engine = d.engine === 'ai_upseller' ? 'AI Upseller' : d.engine === 'ai_chatter' ? 'AI Chatter' : 'AI off';
    var parts = [];
    parts.push('<span>' + (d.engine === 'none' ? '⭘' : '🤖') + ' ' + esc(engine) + '</span>');
    parts.push('<span class="' + (AI_TONE[d.state] || 'st-off') + '" title="' + esc(d.detail || '') + '">' +
      (AI_DOT[d.state] || '⭘') + ' ' + esc(d.label || d.state || '—') +
      (d.until ? ' → ' + esc(new Date(d.until).toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'})) : '') + '</span>');
    var cad = d.cadence || {};
    if(cad.enabled){
      var cap = cad.cap, used = cad.used || 0;
      var pct = cap ? Math.min(100, Math.round(used*100/cap)) : 0;
      parts.push('<span title="Replies used in this burst (tier ' + esc(cad.tier || '?') + ')">' +
        'cadence ' + used + (cap ? '/' + cap : '/∞') +
        ' <span class="meter"><i style="width:' + pct + '%"></i></span></span>');
    }
    if(d.gate && d.gate.enabled && !d.gate.ok){
      parts.push('<span title="' + esc(GATE_WHY[d.gate.why] || d.gate.why || '') + '">🔒 offer gated: ' +
        esc(String(d.gate.why || '').replace(/_/g,' ')) + '</span>');
    }
    if(d.open_ask){
      parts.push('<span title="' + (d.open_ask.by === 'human' ? 'Sent by a chatter' : 'Sent by the AI') + '">🔓 ' +
        money(d.open_ask.price_cents) + ' ask open</span>');
    }
    if(d.graduated === 'spent') parts.push('<span title="Handed to the upseller once he paid">🤝 payer</span>');
    var s7 = d.spend_7d || {};
    if(s7.cap_cents){
      parts.push('<span title="Spend in the last 7 days vs the account cap">7d ' + money(s7.paid_cents||0) +
        ' / ' + money(s7.cap_cents) + (s7.capped ? ' · CAPPED' : '') + '</span>');
    }
    if(d.language && d.language !== 'en'){
      parts.push('<span title="' + esc(d.language_source || '') + '">🌐 ' + esc(d.language) + '</span>');
    }
    var llm = d.llm || {};
    if(llm.model) parts.push('<span title="Last LLM call ' + esc(llm.last_at || '') + '">' + esc(llm.model) +
      ' · ' + (llm.calls_24h || 0) + ' calls/24h</span>');
    aiStrip.innerHTML = parts.join('');
  }

  // ═══════════════════════════════════════════════════════════
  // RIGHT RAIL — Insights / Sales / Notes
  // ═══════════════════════════════════════════════════════════
  var INS_IDS = ['insTotal','insLast','insTiny','insPpvTotal','insPpvAvg','insTipTotal','insTipAvg',
                 'insHighest','subStatus','subCost','subDuration','subRenewal','profNick','profLoc','profTags','profLang'];
  function resetInsights(){ INS_IDS.forEach(function(i){ var e = $id(i); if(e) e.textContent = '…'; });
    var w = $id('insSparkWrap'); if(w) w.style.display = 'none'; }

  var lastPpv = null, lastFan = null, lastOfUser = null, lastVaultHist = null;

  async function loadInsights(fid, aid){
    resetInsights();
    var jobs = await Promise.allSettled([
      Fastt.get('/api/of/v2/users/' + fid, null, {acct: aid, priority: 'background'}),
      aget('/admin/fans/' + aid + '/' + fid + '/ppv-history', {limit: 200}, aid),
      aget('/admin/fans/' + aid + '/' + fid, null, aid),
      aget('/admin/vault/fan-history', {account_id: aid, fan_id: fid}, aid),
    ]);
    if(String(currentFan) !== String(fid)) return;
    var ofu = jobs[0].status === 'fulfilled' ? jobs[0].value : null;
    var ppv = jobs[1].status === 'fulfilled' ? jobs[1].value : null;
    var fan = jobs[2].status === 'fulfilled' ? jobs[2].value : null;
    lastVaultHist = jobs[3].status === 'fulfilled' ? jobs[3].value : null;
    lastPpv = ppv; lastFan = fan; lastOfUser = ofu;
    renderInsightsSpark();   // surface the sales chart inline on the insights pane
    var sub = (ofu && ofu.subscribedOnData) || {};
    var sum = (ppv && ppv.summary) || {};
    var items = (ppv && ppv.items) || [];
    var sp = spendMap[key(aid, fid)] || {};

    // ONE BASIS: everything on this card is GROSS (list price, before OF's cut).
    // Mixing OF's net messagesSumm with our gross lifetime_spend is what made the
    // old card irreconcilable ($12.99 total next to a $6.39 "PPV" that was net).
    var ppvGross = items.reduce(function(a,i){ return a + (i.price_cents || 0); }, 0);
    var ppvNet   = items.reduce(function(a,i){ return a + (i.net_cents || 0); }, 0);
    var tipGross = sum.tips_cents || 0;
    var lifeCents = Math.max(
      (fan && fan.lifetime_spend_cents) || 0,
      sp.spend_cents || 0,
      ppvGross + tipGross
    );
    var totalEl = $id('insTotal');
    totalEl.textContent = money(lifeCents);
    totalEl.setAttribute('title', 'GROSS lifetime spend (list price, before OnlyFans\' cut). ' +
      'Net of fees on the tracked PPV rows: ' + money(ppvNet) + '.');
    var lastAt = sp.last_purchase_at || (items[0] && items[0].purchased_at) || null;
    $id('insLast').textContent = lastAt ? Fastt.fmtAgo(lastAt) : '—';

    // the "- | -" line the skin shipped: purchases + last-30d, both from items[]
    var cutoff = Date.now() - 30*864e5;
    var last30 = items.filter(function(i){
      var d = Fastt.parseUtc(i.purchased_at); return d && d.getTime() >= cutoff;
    });
    var l30c = last30.reduce(function(a,i){ return a + (i.price_cents||0); }, 0);
    $id('insTiny').textContent = items.length
      ? (items.length + ' purchase' + (items.length === 1 ? '' : 's') + ' | ' + money(l30c) + ' last 30d (gross)')
      : 'no tracked purchases | $0 last 30d';

    $id('insPpvTotal').textContent = money(ppvGross);
    $id('insPpvTotal').setAttribute('title', 'Gross ' + money(ppvGross) + ' · net after fees ' + money(ppvNet));
    $id('insPpvAvg').textContent = sum.avg_cents ? money(sum.avg_cents) : '–';
    $id('insTipTotal').textContent = money(tipGross);
    $id('insTipAvg').textContent = '–';
    $id('insTipAvg').setAttribute('title', 'Our ledger stores tip totals, not per-tip rows, so there is no honest average to show.');
    $id('insHighest').textContent = sum.max_cents ? money(sum.max_cents) : '–';

    var exp = sub.expiredAt ? new Date(sub.expiredAt) : null;
    var expired = !!(exp && !isNaN(exp.getTime()) && exp.getTime() < Date.now());
    $id('subStatus').textContent = expired ? 'Expired' : (ofu && ofu.subscribedOn ? 'Active' : '—');
    $id('subCost').textContent = Number(sub.price) > 0 ? '$' + Number(sub.price).toFixed(2) : 'free';
    $id('subDuration').textContent = sub.duration || (ofu && ofu.subscribedOnDuration) || '-';
    $id('subRenewal').textContent = (sub.renewedAt && new Date(sub.renewedAt).getTime() > Date.now()) ? 'On' : 'Off';

    var k = key(aid, fid);
    var info = fanInfo[k] || {};
    var nick = info.nickname || (ofu && ofu.customNickname) || (fan && fan.custom_nickname) || '';
    $id('profNick').textContent = nick || '—';
    var loc = fan ? [fan.home_city, fan.home_country].filter(Boolean).join(', ') : '';
    $id('profLoc').textContent = loc || 'Unknown';
    var tags = (fan && Array.isArray(fan.tags)) ? fan.tags : [];
    $id('profTags').textContent = tags.length ? tags.join(', ') : '—';
    $id('profLang').textContent = (fan && fan.language) ? fan.language : '—';

    if(ofu){
      fanInfo[k] = Object.assign(info, {
        lastSeen: ofu.lastSeen || info.lastSeen,
        isVerified: !!ofu.isVerified,
        nickname: ofu.customNickname || info.nickname,
      });
      setPresence(ofu.lastSeen);
      $id('thVf').style.display = ofu.isVerified ? '' : 'none';
    }
    if(rightTab === 'sales') renderSales();
    if(rightTab === 'notes') renderNotes();
  }

  // 12 GROSS-cent buckets across THIS fan's real buying window (first purchase
  // → now). Shared by the Sales tab and the inline insights spark so both draw
  // the same chart. (A fixed 12-week window drew an all-zero chart for any fan
  // whose last buy was older than that — a chart that never draws is a lie.)
  function salesBars(items){
    var now = Date.now(), N = 12;
    var times = items.map(function(i){ var d = Fastt.parseUtc(i.purchased_at); return d ? d.getTime() : null; })
                     .filter(function(t){ return t; });
    var first = times.length ? Math.min.apply(null, times) : now - 7*864e5;
    var span = Math.max(now - first, 7*864e5);
    var step = span / N;
    var buckets = new Array(N).fill(0);
    items.forEach(function(i){
      var d = Fastt.parseUtc(i.purchased_at); if(!d) return;
      var idx = Math.min(N-1, Math.max(0, Math.floor((d.getTime() - first)/step)));
      buckets[idx] += (i.price_cents||0);
    });
    var max = Math.max.apply(null, buckets);
    var barsHtml = buckets.map(function(v, bi){
      var h = max > 0 ? Math.max(2, Math.round(v*100/max)) : 2;
      var a = new Date(first + bi*step), b = new Date(first + (bi+1)*step);
      return '<i class="' + (v ? '' : 'z') + '" style="height:' + h + '%" title="' +
        esc(a.toLocaleDateString() + ' – ' + b.toLocaleDateString()) + ': ' + money(v) + '"></i>';
    }).join('');
    return { barsHtml: barsHtml, max: max,
             spanLabel: new Date(first).toLocaleDateString([], {month:'short', day:'numeric', year:'2-digit'}) };
  }
  // Compact sales chart surfaced on the DEFAULT insights pane (no tab switch) —
  // the operator asked for the sales graph to be prominent, not buried.
  function renderInsightsSpark(){
    var wrap = $id('insSparkWrap'); if(!wrap) return;
    var items = (lastPpv && lastPpv.items) || [];
    if(!items.length){ wrap.style.display = 'none'; return; }
    var sb = salesBars(items);
    $id('insSparkBars').innerHTML = sb.barsHtml;
    $id('insSparkAxis').innerHTML = '<span>' + esc(sb.spanLabel) + '</span><span>peak ' + money(sb.max) + '</span><span>now</span>';
    wrap.style.display = '';
  }

  // ── Sales pane: every tracked sale + a data-driven chart ──
  function renderSales(){
    var el = $id('paneSales');
    if(!currentFan){ el.innerHTML = honest('No conversation open', 'Pick a fan to see their purchase history.'); return; }
    var items = (lastPpv && lastPpv.items) || [];
    var sum = (lastPpv && lastPpv.summary) || {};
    if(!items.length){
      el.innerHTML = honest('No tracked sales for this fan',
        'GET /admin/fans/' + currentAid + '/' + currentFan + '/ppv-history returned 0 rows — the ledger has no ' +
        'PPV unlock, tip or post purchase attributed to this fan yet. This is an empty ledger, not a failed read.');
      Fastt.liveBadge(el.querySelector('.honest'));
      return;
    }
    var sb = salesBars(items);
    var bars = sb.barsHtml, max = sb.max, spanLabel = sb.spanLabel;
    var rows = items.slice(0, 60).map(function(i){
      var d = Fastt.parseUtc(i.purchased_at);
      var who = i.employee_name ? esc(i.employee_name) : '<span style="color:#6a6a6a">bot/unknown</span>';
      return '<tr><td>' + (d ? esc(d.toLocaleDateString([], {month:'short', day:'numeric'})) : '—') + '</td>' +
        '<td class="r">' + money(i.price_cents) + '</td>' +
        '<td class="r" style="color:#8a8a8a">' + money(i.net_cents) + '</td>' +
        '<td>' + who + '</td></tr>';
    }).join('');
    var vh = lastVaultHist && lastVaultHist.by_media ? Object.keys(lastVaultHist.by_media) : [];
    var bought = vh.filter(function(m){ return lastVaultHist.by_media[m].was_purchased; }).length;
    el.innerHTML =
      '<div class="card"><div class="card-head"><span class="ct">Purchases over time</span></div>' +
        '<div class="bars">' + bars + '</div>' +
        '<div class="axis"><span>' + esc(spanLabel) + '</span><span>peak ' + money(max) + '</span><span>now</span></div>' +
      '</div>' +
      '<div class="card" style="margin-top:12px"><div class="card-head"><span class="ct">Sales (' + items.length + ')</span></div>' +
        '<div class="subtle" style="margin:-4px 0 8px">gross ' + money(items.reduce(function(a,i){return a+(i.price_cents||0);},0)) +
        ' · net ' + money(items.reduce(function(a,i){return a+(i.net_cents||0);},0)) +
        ' · avg ' + money(sum.avg_cents||0) + ' · max ' + money(sum.max_cents||0) + '</div>' +
        '<table class="stab"><thead><tr><th>Date</th><th class="r">Gross</th><th class="r">Net</th><th>Sent by</th></tr></thead>' +
        '<tbody>' + rows + '</tbody></table>' +
      '</div>' +
      '<div class="card" style="margin-top:12px"><div class="card-head"><span class="ct">Vault history</span></div>' +
        '<div class="subtle">' + vh.length + ' vault item' + (vh.length===1?'':'s') + ' sent to this fan · ' +
        bought + ' unlocked</div></div>';
    Fastt.liveBadge(el.querySelector('.card-head'));
  }

  // ── Notes pane: OF-native notice + our local note ─────────
  function renderNotes(){
    var el = $id('paneNotes');
    if(!currentFan){ el.innerHTML = honest('No conversation open', 'Pick a fan to read or write their note.'); return; }
    var ofNote = (lastOfUser && lastOfUser.notice) || '';
    var localNote = (lastFan && lastFan.notes) || '';
    var rich = (lastFan && lastFan.profile && lastFan.profile.rich_note) || '';
    // FanDrawer.tsx prefers OF's own notice and falls back to our copy.
    var current = ofNote || localNote || '';
    el.innerHTML =
      '<div class="card"><div class="card-head"><span class="ct">Fan note</span></div>' +
        '<div class="subtle" style="margin:-4px 0 8px">' +
          (ofNote ? 'Showing the OnlyFans-native note (users/' + currentFan + '.notice).'
                  : (localNote ? 'OnlyFans has no note for this fan — showing our local copy.'
                               : 'No note on OnlyFans and none locally.')) +
        '</div>' +
        '<textarea class="note-ta" id="noteTa">' + esc(current) + '</textarea>' +
        '<div style="display:flex;gap:8px;margin-top:8px">' +
          '<button class="btn-sm pri" id="noteSave">Save to OnlyFans + local</button>' +
          '<button class="btn-sm" id="noteLocal">Save local only</button>' +
        '</div>' +
      '</div>' +
      (rich ? '<div class="card" style="margin-top:12px"><div class="card-head"><span class="ct">Generated profile note</span></div>' +
        '<div class="subtle" style="white-space:pre-wrap">' + esc(rich) + '</div></div>' : '') +
      '<div class="card" style="margin-top:12px"><div class="card-head"><span class="ct">Known facts</span></div>' +
        '<div class="subtle" style="white-space:pre-wrap">' + esc(factsLine()) + '</div></div>';
    Fastt.liveBadge(el.querySelector('.card-head'));
    $id('noteSave').addEventListener('click', function(){ saveNote(true); });
    $id('noteLocal').addEventListener('click', function(){ saveNote(false); });
  }
  function factsLine(){
    if(!lastFan) return '—';
    var bits = [];
    [['Name','real_name'],['Age','his_age'],['City','home_city'],['Country','home_country'],
     ['Job','occupation'],['Employer','employer'],['Hobbies','hobbies'],['Fetishes','fetishes'],
     ['Relationship','relationship_status'],['Pets','pets'],['Stage','relationship_stage'],
     ['Mood','mood_at_last_message'],['Language','language']].forEach(function(p){
      var v = lastFan[p[1]];
      if(Array.isArray(v)) v = v.join(', ');
      if(v !== null && v !== undefined && String(v).trim() !== '') bits.push(p[0] + ': ' + v);
    });
    return bits.length ? bits.join('\n') : 'gen_info has not profiled this fan yet.';
  }
  async function saveNote(toOF){
    var txt = $id('noteTa').value;
    if(toOF && !confirm('Write this note to the fan\'s OnlyFans profile?\n\n(Only you can see it — it is not sent to the fan.)')) return;
    try{
      if(toOF) await aput('/api/of/v2/subscriptions/' + currentFan + '/note', {note: txt}, currentAid);
      await apatch('/admin/fans/' + currentAid + '/' + currentFan, {notes: txt}, currentAid);
      if(lastFan) lastFan.notes = txt;
      if(lastOfUser && toOF) lastOfUser.notice = txt;
      Fastt.saved('Note saved');
    }catch(e){ Fastt.oops(e); }
  }

  var rightTab = 'insights';
  function setRightTab(t){
    rightTab = t;
    $id('paneInsights').classList.toggle('hidden', t !== 'insights');
    $id('paneSales').classList.toggle('hidden', t !== 'sales');
    $id('paneNotes').classList.toggle('hidden', t !== 'notes');
    $id('rtInsights').classList.toggle('active', t === 'insights');
    $id('rtSales').classList.toggle('active', t === 'sales');
    $id('rtNotes').classList.toggle('active', t === 'notes');
    $id('rightHead').textContent = t === 'sales' ? 'Sales' : t === 'notes' ? 'Notes' : 'Fan insights';
    if(t === 'sales') renderSales();
    if(t === 'notes') renderNotes();
  }
  $id('rtInsights').addEventListener('click', function(){ setRightTab('insights'); });
  $id('rtSales').addEventListener('click', function(){ setRightTab('sales'); });
  $id('insSparkOpen').addEventListener('click', function(){ setRightTab('sales'); });   // inline chart → full sales history

  // ── "+ New" compose menu (mass message / message online / broadcast / post) ──
  $id('newBtn').addEventListener('click', function(e){
    e.stopPropagation();
    var acct = activeAid ? ('?account=' + encodeURIComponent(activeAid)) : '';
    popover(this,
      '<div class="phead">Create new…</div>' +
      '<div class="prow" data-go="outreach-mass-message.html' + acct + '"><span class="ptxt">📣 Mass message</span><span class="pmeta">many fans</span></div>' +
      '<div class="prow" data-go="mass-nudge.html' + acct + '"><span class="ptxt">🟢 Message online fans</span><span class="pmeta">online now</span></div>' +
      '<div class="prow" data-go="outreach-broadcast.html' + acct + '"><span class="ptxt">📡 Broadcast</span><span class="pmeta">announce</span></div>' +
      '<div class="prow" data-go="content-create-post.html' + acct + '"><span class="ptxt">🖼 New post</span><span class="pmeta">to your wall</span></div>',
      function(row){ var go = row.getAttribute('data-go'); if(go){ closePops(); location.href = go; } });
  });
  $id('rtNotes').addEventListener('click', function(){ setRightTab('notes'); });

  // ═══════════════════════════════════════════════════════════
  // SELECTION
  // ═══════════════════════════════════════════════════════════
  function markSelected(){
    Array.prototype.forEach.call(document.querySelectorAll('.conv'), function(c){
      c.classList.toggle('selected',
        String(c.getAttribute('data-fan')) === String(currentFan) &&
        String(c.getAttribute('data-acct')) === String(currentAid));
    });
  }
  function clearSelection(){
    currentFan = null; currentAid = null; currentChatMeta = null; threadMsgs = []; attrMap = {};
    if(typeof renderTabStrip === 'function') renderTabStrip();   // drop the active-tab highlight
    lastPpv = lastFan = lastOfUser = lastVaultHist = null;
    markSelected();
    threadHead.style.display = 'none';
    aiStrip.style.display = 'none';
    typingLine.classList.add('hidden');
    $id('pinBar').classList.add('hidden');
    $id('schedChip').style.display = 'none';
    threadBody.style.display = 'none';
    composer.style.display = 'none';
    emptyState.style.display = 'flex';
    rightPanel.style.display = 'none';
  }
  function selectFan(fid, aid, userOpen){
    if(String(fid) === String(currentFan) && String(aid) === String(currentAid)){ clearSelection(); return; }
    currentFan = fid; currentAid = String(aid);
    currentChatMeta = null;   // drop the prior chat's meta so seen-receipts never paint stale
    attrMap = {};             // drop prior chat's attribution so "sent by" never paints stale
    if(inboxMode === 'all' && String(aid) !== String(activeAid)){
      activeAid = String(aid); lsSet(LS_ACTIVE, activeAid); renderTabs(); loadNotifCount();
    }
    markSelected();
    clearAttachments();
    cancelReply();
    var u = fanInfo[key(aid, fid)] || {};
    var row = chats.filter(function(c){ return String(c.fanId) === String(fid) && String(c.aid) === String(aid); })[0] || {};
    $id('thName').textContent = u.name || row.name || u.username || ('Fan ' + fid);
    $id('thVf').style.display = u.isVerified ? '' : 'none';
    setPresence(u.lastSeen);
    threadHead.style.display = '';
    threadBody.style.display = '';
    composer.style.display = '';
    emptyState.style.display = 'none';
    rightPanel.style.display = '';
    paintStar();
    // Instant swap: if we've opened this thread before (or hover-prefetched it),
    // paint the cached view immediately, then revalidate in the background.
    var _cached = THREAD_CACHE[key(aid, fid)];
    threadBody._sig = null;
    // Empty the thread model on EVERY switch, before either branch paints.
    // Without this, a cold open still holds the PREVIOUS fan's messages, and
    // loadThread's monotonicity guard compares that fan's last id against the
    // new fan's window — OF ids are global and time-ordered, so opening a
    // less-recent conversation read as "view ahead", painted the previous
    // fan's thread into this pane, and cached it under the new fan's key.
    threadMsgs = []; oldestMsgId = null; threadHasOlder = false;
    renderPinBar();   // pin bar reads threadMsgs — drop the prior fan's pins now
    if(_cached && _cached.msgs.length){
      paintThread(_cached.msgs.slice(), _cached.hasOlder);   // instant from cache
      loadThread(fid, aid);                                  // revalidate (no DOM churn if unchanged)
    }else{
      threadBody.innerHTML = '<div class="msg-meta">Loading messages…</div>';
      // Cold open: race the local mirror against OF. The seed is ~0.1ms of
      // SQLite and bails the moment anything authoritative has painted, so
      // whichever answers first is what the operator sees.
      seedThread(fid, aid);
      loadThread(fid, aid);
    }
    loadChatMeta(fid, aid);
    loadAiStatus(fid, aid);
    loadSchedChip(fid, aid);
    loadInsights(fid, aid).catch(function(e){ console.warn('insights', e); });
    loadAttribution(fid, aid);   // who sent each outbound message (non-blocking)
    // Only mark read on an explicit user open — never on startup auto-select or
    // a deep-link/pop-out, so OF read receipts never fire without operator intent.
    if(userOpen) markReadOnOpen(fid, aid, row);
    renderTabStrip();   // reflect the active tab if this chat is pinned
  }

  // Opening an unread chat clears it on OnlyFans (blue → gone), and optimistically
  // on the row + creator-tab badge. Read-state only; never a fan-facing send.
  async function markReadOnOpen(fid, aid, row){
    if(!row || !row.unread) return;
    row.unread = false;
    var el = rowsEl.querySelector('.conv[data-fan="' + fid + '"][data-acct="' + aid + '"]');
    if(el){
      var d = el.querySelector('.ud'); if(d) d.remove();
      var pv = el.querySelector('.cprev'); if(pv) pv.classList.remove('bold');
      // read but fan-sent-last → the amber owe-reply dot takes the unread dot's place
      if(row.unresponded && el.querySelector('.conv-bot') && !el.querySelector('.owe-dot')){
        el.querySelector('.conv-bot').insertAdjacentHTML('beforeend',
          '<span class="owe-dot" title="Read · awaiting your reply"></span>');
      }
    }
    var c = byAid[String(aid)]; if(c && c.unread > 0){ c.unread--; renderTabs(); }
    updateChipCounts();
    try{ await apost('/api/of/v2/chats/' + fid + '/mark-as-read', {}, aid); }
    catch(e){ /* OF may 404 if already read — harmless */ }
  }
  rowsEl.addEventListener('click', function(e){
    if(e.target.closest && e.target.closest('#loadMore')){ loadMoreChats(); return; }
    var rowEl = e.target.closest ? e.target.closest('.conv') : null;
    if(!rowEl) return;
    selectFan(rowEl.getAttribute('data-fan'), rowEl.getAttribute('data-acct'), true);  // user click → mark read
  });
  // Warm the thread cache on a short hover-dwell so the next click is instant.
  var _pfTimer = null;
  rowsEl.addEventListener('mouseover', function(e){
    var rowEl = e.target.closest ? e.target.closest('.conv') : null;
    if(!rowEl) return;
    var fid = rowEl.getAttribute('data-fan'), aid = rowEl.getAttribute('data-acct');
    if(_pfTimer) clearTimeout(_pfTimer);
    _pfTimer = setTimeout(function(){ prefetchThread(fid, aid); }, 120);   // matches the real app's ChatList dwell
  });

  // Bubble actions — unsend our own message, like the fan's. Delegated once;
  // covers bubbles added later by SSE. Every outbound-affecting action confirms.
  // Full-size media lightbox (reuses the vault .vid-back overlay chrome).
  function openImageLightbox(url){
    if(!url) return;
    var back = document.createElement('div'); back.className = 'vid-back';
    back.innerHTML = '<div class="vid-wrap"><button class="vid-x" title="Close">&times;</button>' +
      '<img src="' + esc(imgProxy(url)) + '" alt="" style="max-width:88vw;max-height:85vh;border-radius:12px;display:block"></div>';
    document.body.appendChild(back);
    function close(){ back.remove(); document.removeEventListener('keydown', onKey); }
    function onKey(ev){ if(ev.key === 'Escape') close(); }
    document.addEventListener('keydown', onKey);
    back.addEventListener('click', function(ev){ if(ev.target === back) close(); });
    back.querySelector('.vid-x').addEventListener('click', close);
  }
  threadBody.addEventListener('click', async function(e){
    if(e.target.closest('#loadOlder')){ loadOlder(); return; }
    var mediaEl = e.target.closest('.msg-media');
    if(mediaEl){ openImageLightbox(mediaEl.getAttribute('data-full')); return; }
    var reward = e.target.closest('[data-tipreward]');
    if(reward){
      e.stopPropagation();
      if(!currentFan) return;
      var tipCents = Number(reward.getAttribute('data-tipcents')) || 0;
      if(!confirm('Fire the tip-reward automation for this ' + Fastt.fmtCents(tipCents) + ' tip?\n\n' +
                  'It sends the configured reward (bundle/message) to this fan on OnlyFans.')) return;
      reward.disabled = true; reward.textContent = '…';
      try{
        await Fastt.post('/admin/tip-reward/send',
          {account_id: String(currentAid), fan_id: Number(currentFan), tip_cents: tipCents,
           tip_message_id: Number(reward.getAttribute('data-tipreward'))},
          {acct: currentAid});
        reward.textContent = '🎁 sent'; Fastt.saved('Tip reward sent');
      }catch(err){ reward.disabled = false; reward.textContent = '🎁 reward'; Fastt.oops(err); }
      return;
    }
    var btn = e.target.closest('[data-act]'); if(!btn) return;
    var bubble = btn.closest('[data-mid]'); if(!bubble || !currentFan) return;
    var mid = bubble.getAttribute('data-mid'), act = btn.getAttribute('data-act');
    if(act === 'reply'){
      var rm = threadMsgs.filter(function(x){ return String(x.id) === String(mid); })[0];
      if(rm) setReply(rm);
      return;
    }
    if(act === 'unsend'){
      if(!confirm('UNSEND this message for everyone on OnlyFans?\n\nThis retracts it from the fan and cannot be undone.')) return;
      try{
        await Fastt.api('/api/of/v2/messages/' + mid, {method:'DELETE', body:{withUserId: Number(currentFan)},
                        acct: currentAid});
        bubble.remove();
        threadMsgs = threadMsgs.filter(function(m){ return String(m.id) !== String(mid); });
        threadBody._sig = threadSig(threadMsgs); cacheThread(currentFan, currentAid);
        Fastt.saved('Unsent');
      }catch(err){ Fastt.oops(err); }
      return;
    }
    if(act === 'like'){
      var on = btn.classList.contains('on');
      btn.classList.toggle('on', !on);                    // optimistic
      try{
        await Fastt.api('/api/of/v2/messages/' + mid + '/like',
                        {method: on ? 'DELETE' : 'POST', acct: currentAid});
      }catch(err){ btn.classList.toggle('on', on); Fastt.oops(err); }
      return;
    }
  });

  // ═══════════════════════════════════════════════════════════
  // COMPOSER
  // ═══════════════════════════════════════════════════════════
  function insertText(t){
    var v = input.value;
    input.value = (v && !/\s$/.test(v) ? v + ' ' : v) + t;
    input.dispatchEvent(new Event('input'));
    input.focus();
  }
  input.addEventListener('input', function(){
    sendBtn.classList.toggle('active', !!input.value.trim().length || attachments.length > 0);
  });
  function renderAttachments(){
    var w = $id('attWrap');
    w.innerHTML = attachments.map(function(a){
      return '<span class="a" data-mid="' + esc(a.id) + '">' +
        (a.thumb ? '<img src="' + esc(a.thumb.charAt(0) === '/' ? a.thumb : imgProxy(a.thumb)) + '" alt="">' : '') + '<b>✕</b></span>';
    }).join('') + (giphyId ? '<span class="a" data-gif="1" style="display:grid;place-items:center;font-size:10px;font-weight:800">GIF<b>✕</b></span>' : '');
    var on = attachments.length || giphyId || scheduleOn || Number($id('ppvPrice').value) > 0;
    $id('compX').classList.toggle('on', !!on);
    $id('schedWrap').style.display = scheduleOn ? 'flex' : 'none';
    // free-preview stepper — only meaningful for a priced PPV that has attachments.
    // Ceiling is attachments.length - 1 so a priced PPV always keeps ≥1 locked item.
    var canPrev = Number($id('ppvPrice').value) > 0 && attachments.length > 0;
    if(freePreviews > attachments.length - 1) freePreviews = Math.max(0, attachments.length - 1);
    var ps = $id('prevStep'); if(ps){ ps.style.display = canPrev ? 'inline-flex' : 'none'; }
    var pn = $id('prevN'); if(pn) pn.textContent = String(freePreviews);
    sendBtn.classList.toggle('active', !!input.value.trim().length || attachments.length > 0 || !!giphyId);
  }
  $id('prevMinus').addEventListener('click', function(){ if(freePreviews > 0){ freePreviews--; renderAttachments(); } });
  $id('prevPlus').addEventListener('click', function(){ if(freePreviews < attachments.length - 1){ freePreviews++; renderAttachments(); } });
  $id('attWrap').addEventListener('click', function(e){
    var a = e.target.closest('.a'); if(!a) return;
    if(a.getAttribute('data-gif')) giphyId = null;
    else attachments = attachments.filter(function(x){ return String(x.id) !== String(a.getAttribute('data-mid')); });
    renderAttachments();
  });
  function clearAttachments(){
    attachments = []; giphyId = null; scheduleOn = false; freePreviews = 0;
    $id('ppvPrice').value = ''; $id('ppvLock').checked = false; $id('schedAt').value = '';
    renderAttachments();
  }
  $id('compXClear').addEventListener('click', clearAttachments);
  $id('ppvPrice').addEventListener('input', renderAttachments);

  var sending = false;
  async function sendCurrent(){
    var text = input.value.trim();
    var price = Number($id('ppvPrice').value) || 0;
    if((!text && !attachments.length && !giphyId) || !currentFan || sending) return;
    var u = fanInfo[key(currentAid, currentFan)] || {};
    var who = u.name || u.username || ('fan ' + currentFan);
    var cre = (byAid[currentAid] || {}).name || currentAid;
    var when = scheduleOn ? $id('schedAt').value : null;
    if(scheduleOn && !when){ Fastt.toast('Pick a date/time first', 'err'); return; }
    var nFree = Math.min(freePreviews, attachments.length);
    var what = (price > 0 ? 'a $' + price.toFixed(2) + ' PPV' : 'this message') +
      (attachments.length ? ' with ' + attachments.length + ' vault item(s)' : '') +
      (price > 0 && nFree ? ' (' + nFree + ' free preview' + (nFree === 1 ? '' : 's') + ')' : '') +
      (giphyId ? ' with a GIF' : '') +
      (replyTo ? ' — as a reply' : '');
    if(!confirm((scheduleOn ? 'SCHEDULE ' : 'SEND ') + what + ' to ' + who +
                ' on OnlyFans as ' + cre + '?\n\n"' + (text || '(no text)') + '"')) return;
    sending = true; sendBtn.disabled = true;
    var body = {text: text};
    if(price > 0){ body.price = price; body.locked_text = !!$id('ppvLock').checked; }
    if(attachments.length){
      body.media_files = attachments.map(function(a){ return Number(a.id); });
      // free previews: the first N vault ids ride UNLOCKED as teasers on the PPV
      if(price > 0) body.previews = attachments.slice(0, nFree).map(function(a){ return Number(a.id); });
    }
    if(giphyId) body.giphy_id = giphyId;
    if(replyTo) body.reply_to_message_id = Number(replyTo.id);   // OF native quote-reply
    try{
      var r;
      if(scheduleOn){
        body.scheduled_date = new Date(when).toISOString();
        r = await apost('/api/of/v2/chats/' + currentFan + '/messages/scheduled', body, currentAid);
        Fastt.saved('Scheduled ✓');
      }else{
        r = await apost('/api/of/v2/chats/' + currentFan + '/messages', body, currentAid);
        if(r && !r.fromUser) r.fromUser = {id: Number(currentAid)};
        var sent = r || {text: text, createdAt: new Date().toISOString(), fromUser: {id: Number(currentAid)}, price: price};
        threadMsgs.push(sent);   // keep in the model so load-older re-render + reply/unsend can resolve it
        threadBody.insertAdjacentHTML('beforeend', msgHtml(sent));
        threadBody._sig = threadSig(threadMsgs); cacheThread(currentFan, currentAid);
        threadBody.scrollTop = threadBody.scrollHeight;
        Fastt.saved('Sent ✓');
      }
      input.value = ''; sendBtn.classList.remove('active');
      clearAttachments();
      cancelReply();
    }catch(e){ Fastt.oops(e); }
    finally{ sending = false; sendBtn.disabled = false; }
  }
  // ── quote-reply strip ──
  function setReply(m){
    if(!m || !m.id) return;
    replyTo = {id: m.id, text: stripHtml(m.text) || (Number(m.price) > 0 ? Fastt.fmtMoney(m.price) + ' PPV' : '(media)')};
    $id('replyStripTxt').textContent = 'Replying to: ' + replyTo.text.slice(0, 90);
    $id('replyStrip').classList.remove('hidden');
    input.focus();
  }
  function cancelReply(){ replyTo = null; $id('replyStrip').classList.add('hidden'); }
  $id('replyCancel').addEventListener('click', cancelReply);
  sendBtn.addEventListener('click', sendCurrent);
  input.addEventListener('keydown', function(e){ if(e.key === 'Enter') sendCurrent(); });

  // ── emoji (client-side, nothing faked) ────────────────────
  var EMOJI = '😏 😘 🥰 😍 🔥 💦 😈 🥵 😉 😅 🤭 💕 💋 🍑 🍆 👀 🙈 ✨ 😭 🤤 💖 😇 🥺 😩 💫 🫦 🖤 😻'.split(' ');
  $id('cbEmoji').addEventListener('click', function(e){
    e.stopPropagation();
    popover(this, '<div class="phead">Emoji</div><div style="display:flex;flex-wrap:wrap;gap:2px;padding:4px">' +
      EMOJI.map(function(x){ return '<span class="prow" data-e="' + x + '" style="padding:6px 7px;font-size:18px">' + x + '</span>'; }).join('') +
      '</div>', function(row){ insertText(row.getAttribute('data-e')); });
  });

  // ── AI pill → this fan's generated lines ──────────────────
  $id('cbAI').addEventListener('click', async function(e){
    e.stopPropagation();
    if(!currentFan) return Fastt.toast('Open a conversation first');
    var btn = this;
    var d;
    try{ d = await aget('/admin/fans/' + currentAid + '/' + currentFan + '/lines', null, currentAid); }
    catch(err){ return Fastt.oops(err); }
    var qs = (d.questions || []), ts = (d.teases || []);
    var html = '<div class="phead">Questions grounded in his profile</div>' +
      (qs.length ? qs.map(function(x){ return '<div class="prow" data-t="' + esc(x.text) + '"><span class="ptxt">' + esc(x.text) + '</span></div>'; }).join('')
                 : '<div class="prow dim">none generated yet</div>') +
      '<div class="pdiv"></div><div class="phead">Teases</div>' +
      (ts.length ? ts.map(function(x){ return '<div class="prow" data-t="' + esc(x.text) + '"><span class="ptxt">' + esc(x.text) + '</span></div>'; }).join('')
                 : '<div class="prow dim">none generated yet</div>') +
      '<div class="pdiv"></div><div class="prow" data-gen="1">↻ Regenerate lines (costs one LLM call)</div>';
    popover(btn, html, async function(row){
      if(row.getAttribute('data-gen')){
        if(!confirm('Regenerate this fan\'s lines? This makes one LLM call. Nothing is sent to the fan.')) return;
        closePops();
        try{ await apost('/admin/fans/' + currentAid + '/' + currentFan + '/lines/generate', {}, currentAid);
             Fastt.saved('Lines regenerated'); }
        catch(err){ Fastt.oops(err); }
        return;
      }
      insertText(row.getAttribute('data-t'));
      closePops();
    });
  });

  // ── Scripts pill → OF templates + saved replies + PPV sets ─
  $id('cbScripts').addEventListener('click', async function(e){
    e.stopPropagation();
    var btn = this;
    var jobs = await Promise.allSettled([
      Fastt.get('/api/of/v2/messages/templates', null, {acct: activeAid}),
      aget('/admin/saved-replies', null, activeAid),
      aget('/admin/scripts', null, activeAid),
    ]);
    var tpl = jobs[0].status === 'fulfilled' ? (jobs[0].value || []) : [];
    var sr  = jobs[1].status === 'fulfilled' ? ((jobs[1].value || {}).list || []) : [];
    var sc  = jobs[2].status === 'fulfilled' ? (jobs[2].value || {}) : {};
    var singles = (sc.singles || []).filter(function(s){ return s.enabled !== false; });
    var html = '<div class="phead">OnlyFans templates (' + tpl.length + ')</div>' +
      (tpl.length ? tpl.map(function(t){
        return '<div class="prow" data-t="' + esc(stripHtml(t.text)) + '"><span class="ptxt">' + esc(stripHtml(t.text)).slice(0,90) + '</span></div>';
      }).join('') : '<div class="prow dim">none saved on OnlyFans</div>') +
      '<div class="pdiv"></div><div class="phead">Saved replies (' + sr.length + ')</div>' +
      (sr.length ? sr.map(function(t){
        return '<div class="prow" data-t="' + esc(t.text || t.body || '') + '"><span class="ptxt">' + esc(t.title || t.text || '') + '</span></div>';
      }).join('') : '<div class="prow dim">none saved in fastt for this creator</div>') +
      '<div class="pdiv"></div><div class="phead">PPV sets (' + singles.length + ')</div>' +
      (singles.length ? singles.map(function(s){
        return '<div class="prow" data-single="' + esc(s.id) + '"><span class="ptxt">' + esc(s.label || ('set ' + s.id)) +
          '</span><span class="pmeta">' + money(s.price_cents||0) + ' · ' + (s.media_ids||[]).length + ' media</span></div>';
      }).join('') : '<div class="prow dim">no vault sets configured</div>');
    popover(btn, html, function(row){
      var sid = row.getAttribute('data-single');
      if(sid){
        var s = singles.filter(function(x){ return String(x.id) === String(sid); })[0];
        if(s){
          attachments = (s.media_ids || []).map(function(m){ return {id: m, thumb: null}; });
          if(s.price_cents) $id('ppvPrice').value = (s.price_cents/100).toFixed(2);
          renderAttachments();
          Fastt.toast('Attached "' + (s.label || sid) + '" — review, then press Send');
        }
        closePops();
        return;
      }
      insertText(row.getAttribute('data-t'));
      closePops();
    });
  });

  // ── vault video helpers ── the OF payload contract lives in _shared/vault-media.js
  var progressiveVideoSrc = FasttVault.progressiveVideoSrc,
      isDrmVideo          = FasttVault.isDrmVideo,
      videoPosterFrames   = FasttVault.videoPosterFrames;
  function fmtDur(sec){
    sec = Math.max(0, Math.round(Number(sec) || 0));
    var mm = Math.floor(sec / 60), ss = sec % 60;
    return mm + ':' + (ss < 10 ? '0' : '') + ss;
  }
  var PLAY_SVG = '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg>';

  // ── vault video lightbox (play a real preview; DRM → poster frames) ──
  function openVaultVideo(m){
    var src = progressiveVideoSrc(m), drm = isDrmVideo(m), posters = videoPosterFrames(m);
    var back = document.createElement('div');
    back.className = 'vid-back';
    var body;
    if(src){
      var pf = (m.files && (m.files.squarePreview || m.files.preview || m.files.thumb)) || null;
      var poster = pf && pf.url;
      // Route through the relay's /img proxy — it supports Range requests for
      // browser <video> streaming, and OF's raw CDN URLs are IP-signed to the
      // relay (they 403 if the browser fetches them directly).
      body = '<video src="' + esc(imgProxy(src)) + '"' + (poster ? ' poster="' + esc(imgProxy(poster)) + '"' : '') +
        ' controls autoplay playsinline loop preload="metadata"></video>' +
        '<div class="vid-note">Streaming the ' + (m.videoSources && m.videoSources['240'] ? '240p' : (m.videoSources && m.videoSources['720'] ? '720p' : 'full')) +
        ' source straight from OnlyFans. If it stalls, the signed URL may have expired — reopen the vault.</div>';
    }else if(drm && posters.length){
      body = '<img id="vpFrame" src="' + esc(imgProxy(posters[0])) + '" alt="">' +
        '<div class="vid-note">OnlyFans protects this video with DRM (FairPlay) — it can’t be streamed here, ' +
        'so these are its ' + posters.length + ' preview frame' + (posters.length===1?'':'s') + '.</div>' +
        '<div class="vid-frames">' + posters.map(function(_,i){ return '<b class="' + (i===0?'on':'') + '" data-i="' + i + '"></b>'; }).join('') + '</div>';
    }else{
      body = '<div class="vid-note">No playable preview for this video (no progressive source and no DRM poster frames).</div>';
    }
    back.innerHTML = '<div class="vid-wrap"><button class="vid-x" title="Close">&times;</button>' + body + '</div>';
    document.body.appendChild(back);
    function close(){ var v = back.querySelector('video'); if(v){ try{ v.pause(); }catch(e){} } back.remove(); document.removeEventListener('keydown', onKey); }
    function onKey(e){ if(e.key === 'Escape') close(); }
    document.addEventListener('keydown', onKey);
    back.addEventListener('click', function(e){ if(e.target === back) close(); });
    back.querySelector('.vid-x').addEventListener('click', close);
    // DRM poster slideshow: auto-advance + dot clicks
    if(!src && drm && posters.length > 1){
      var frame = back.querySelector('#vpFrame'), dots = back.querySelectorAll('.vid-frames b'), i = 0;
      function showFrame(n){ i = (n + posters.length) % posters.length;
        frame.src = imgProxy(posters[i]);
        Array.prototype.forEach.call(dots, function(d, j){ d.classList.toggle('on', j === i); }); }
      var tick = setInterval(function(){ if(!back.isConnected){ clearInterval(tick); return; } showFrame(i + 1); }, 1100);
      back.querySelector('.vid-frames').addEventListener('click', function(e){
        var b = e.target.closest('b'); if(b){ clearInterval(tick); showFrame(Number(b.getAttribute('data-i'))); } });
    }
  }

  // ── vault picker (unified grid — parity with app/components/chat/VaultPicker:
  //    cache-first source, type/folder/sort/search, MRU folder quick-chips that
  //    surface the most-used folders first, AI caption overlays on thumbs) ──
  var VAULT_CACHE = { folders: {}, summary: {}, hist: {} };   // per-account (+ per-fan hist), session-lived

  // Per-fan folder MRU + per-account usage counts drive the "reorder folders"
  // ranking (most-used first, not OF's raw list order). localStorage so it
  // survives reloads + pop-outs. Mirrors the app VaultPicker.
  function vMruKey(aid, fid){ return 'ft_vault_mru:' + aid + ':' + (fid == null ? '_' : fid); }
  function vCountKey(aid){ return 'ft_vault_counts:' + aid; }
  function vLoadMru(aid, fid){
    try{ var a = JSON.parse(localStorage.getItem(vMruKey(aid, fid)) || '[]');
      return Array.isArray(a) ? a.filter(function(n){ return typeof n === 'number'; }).slice(0, 3) : []; }
    catch(e){ return []; }
  }
  function vSaveMru(aid, fid, mru){ try{ localStorage.setItem(vMruKey(aid, fid), JSON.stringify(mru.slice(0, 3))); }catch(e){} }
  function vLoadCounts(aid){
    try{ var o = JSON.parse(localStorage.getItem(vCountKey(aid)) || '{}'); return (o && typeof o === 'object') ? o : {}; }
    catch(e){ return {}; }
  }
  function vBumpCount(aid, lid){
    try{ var o = vLoadCounts(aid); o[String(lid)] = (o[String(lid)] || 0) + 1; localStorage.setItem(vCountKey(aid), JSON.stringify(o)); }catch(e){}
  }
  // Per-fan picker position: the exact folder/sort/type the chatter left it on,
  // so reopening the picker for this fan lands byte-identical (no forced reset).
  function vStateKey(aid, fid){ return 'ft_vault_state:' + aid + ':' + (fid == null ? '_' : fid); }
  function vLoadState(aid, fid){
    try{ var s = JSON.parse(localStorage.getItem(vStateKey(aid, fid)) || 'null'); return (s && typeof s === 'object') ? s : null; }
    catch(e){ return null; }
  }
  function vSaveState(aid, fid, st){ try{ localStorage.setItem(vStateKey(aid, fid), JSON.stringify(st)); }catch(e){} }
  // AI caption (S9 parity): the mirror `_ai` overlay → a one-glance description
  // + up to 4 tags. Videos prefer video_description. null when the item has
  // neither. Only mirror (Collect-ed) rows carry `_ai`.
  function vAiCaption(m){
    var ai = m && m._ai; if(!ai) return null;
    var primary = m.type === 'video' ? ai.video_description : ai.description;
    var text = String(primary || ai.description || ai.video_description || '').trim();
    var tags = (Array.isArray(ai.tags) ? ai.tags : []).map(function(t){ return String(t).trim(); }).filter(Boolean).slice(0, 4);
    if(!text && !tags.length) return null;
    return { text: text, tags: tags };
  }

  async function openVault(){
    var aid = activeAid, opts = {acct: aid}, fid = currentFan;
    var PAGE = 48;
    var sh = sheet('Vault',
      '<div class="vtb" id="vTb"></div>' +
      '<div class="vchips" id="vChips" style="display:none"></div>' +
      '<div id="vHint"></div>' +
      '<div id="vBody"><div class="subtle" style="padding:8px 2px">Loading vault…</div></div>',
      '<span class="subtle" id="vaultSel">0 selected</span>' +
      '<button class="btn-sm" id="vaultRefresh" style="margin-left:auto">↻ Refresh</button>' +
      '<button class="btn-sm pri" id="vaultAdd">Attach to message</button>');

    var chosen = {}, mediaMeta = {}, histMap = {};   // histMap: media_id -> this fan's send/purchase history
    var hov = null;   // hover-scrub session (FasttVault.hoverScrub), armed below
    var savedSt = vLoadState(aid, fid);   // restore this fan's last folder/sort/type
    var state = { type: (savedSt && savedSt.type) || 'all',
                  listId: (savedSt && savedSt.listId != null) ? savedSt.listId : null,
                  sort: (savedSt && savedSt.sort) || 'newest', query: '',
                  captions: localStorage.getItem('ft_vault_captions') === '1' };
    function persistState(){ vSaveState(aid, fid, { type: state.type, listId: state.listId, sort: state.sort }); }
    var page = { items: [], offset: 0, hasMore: false, loading: false, seq: 0 };
    var SRC = 'live', folders = [];
    var mru = vLoadMru(aid, fid), counts = vLoadCounts(aid);
    var tbEl = sh.querySelector('#vTb'), chipsEl = sh.querySelector('#vChips'),
        hintEl = sh.querySelector('#vHint'), bodyEl = sh.querySelector('#vBody');
    // Hover-to-preview over the tile grid. The session owns its own timers and
    // attaches its own delegated listeners; renderGrid only has to stop it
    // before replacing the tiles it points at.
    hov = FasttVault.hoverScrub(bodyEl, function(mid){ return mediaMeta[mid]; });

    function updSel(){ var n = Object.keys(chosen).length; var el = sh.querySelector('#vaultSel'); if(el) el.textContent = n + ' selected'; }

    // ── data source: local mirror once the account is Collect-ed, else live ──
    try{
      var sum = VAULT_CACHE.summary[aid];
      if(sum === undefined){ sum = await Fastt.get('/admin/vault-ai/cache/summary', {account_id: aid}, opts); VAULT_CACHE.summary[aid] = sum; }
      SRC = (sum && sum.count > 0) ? 'mirror' : 'live';
    }catch(e){ SRC = 'live'; }
    // ── folders (dropdown + quick-chips) ──
    try{
      folders = VAULT_CACHE.folders[aid];
      if(!folders){ var lo = await Fastt.get('/api/of/v2/vault/lists', {limit: 50}, opts); folders = lo.list || []; VAULT_CACHE.folders[aid] = folders; }
    }catch(e){ folders = []; }
    // A restored folder that no longer exists (deleted upstream) → fall back to
    // All rather than fetching an empty/soft-denied folder.
    if(state.listId != null && !folderById(state.listId)) state.listId = null;

    function folderById(id){ for(var i = 0; i < folders.length; i++){ if(String(folders[i].id) === String(id)) return folders[i]; } return null; }
    function folderCounts(f){
      var parts = [], ph = (f.photosCount || 0) + (f.gifsCount || 0);
      if(ph) parts.push('🖼 ' + ph);
      if(f.videosCount) parts.push('🎬 ' + f.videosCount);
      if(f.audiosCount) parts.push('🎵 ' + f.audiosCount);
      return parts.join(' · ');
    }
    // Folders reordered by usage: per-fan MRU first, then account-wide most-used,
    // then OF's order for the rest — the folders you actually use float to the
    // top (this is what "reorder folders" means in the picker). Frontend-only.
    function rankedFolderIds(){
      var out = [], seen = {};
      function push(id){ if(seen[id]) return; if(!folderById(id)) return; seen[id] = 1; out.push(Number(id)); }
      mru.forEach(function(id){ push(id); });
      Object.keys(counts).map(function(id){ return { id: Number(id), c: Number(counts[id]) || 0 }; })
        .filter(function(e){ return isFinite(e.id) && e.c > 0; })
        .sort(function(a, b){ return b.c - a.c; })
        .forEach(function(e){ push(e.id); });
      folders.forEach(function(f){ push(f.id); });
      return out;
    }

    function renderToolbar(){
      var types = [['all', 'All'], ['photo', 'Photos'], ['video', 'Videos'], ['gif', 'GIFs']];
      var seg = '<div class="vseg">' + types.map(function(t){
        return '<button data-type="' + t[0] + '"' + (state.type === t[0] ? ' class="on"' : '') + '>' + t[1] + '</button>';
      }).join('') + '</div>';
      var ranked = rankedFolderIds();
      var optHtml = '<option value=""' + (state.listId == null ? ' selected' : '') + '>All folders</option>' +
        ranked.map(function(id){ var f = folderById(id); if(!f) return '';
          var c = folderCounts(f);
          return '<option value="' + esc(f.id) + '"' + (String(state.listId) === String(f.id) ? ' selected' : '') + '>' +
            esc((f.name || ('Folder ' + f.id)) + (c ? ' — ' + c : '')) + '</option>'; }).join('');
      var sel = folders.length ? '<select id="vFolder" title="Filter by folder (most-used first)">' + optHtml + '</select>' : '';
      var sort = '<select id="vSort" title="Sort order">' +
        '<option value="newest"' + (state.sort === 'newest' ? ' selected' : '') + '>Newest</option>' +
        '<option value="oldest"' + (state.sort === 'oldest' ? ' selected' : '') + '>Oldest</option></select>';
      var search = '<input type="search" id="vQ" placeholder="Search vault…" value="' + esc(state.query) + '">';
      var capTog = '<button class="vcap-tog' + (state.captions ? ' on' : '') + '" id="vCapTog" title="Show AI descriptions on thumbnails">📝 captions</button>';
      var cached = SRC === 'mirror' ? '<span class="vcached" title="Served instantly from the local cache">⚡ cached</span>' : '';
      tbEl.innerHTML = seg + sel + sort + search + capTog + cached;
    }
    function renderChips(){
      if(!folders.length){ chipsEl.style.display = 'none'; return; }
      chipsEl.style.display = '';
      var ranked = rankedFolderIds().slice(0, 3);
      var mruSet = {}; mru.forEach(function(id){ mruSet[id] = 1; });
      chipsEl.innerHTML = '<span class="vlbl">Folders:</span>' +
        '<button class="vchip' + (state.listId == null ? ' on' : '') + '" data-lid="">All</button>' +
        ranked.map(function(id){ var f = folderById(id); if(!f) return '';
          return '<button class="vchip' + (String(state.listId) === String(id) ? ' on' : '') + '" data-lid="' + esc(id) + '" title="' + esc(f.name || '') + '">' +
            (mruSet[id] ? '<span class="st">★</span>' : '') + esc(f.name || ('Folder ' + id)) + '</button>'; }).join('');
    }
    function renderHint(){
      hintEl.innerHTML = (state.captions && SRC !== 'mirror')
        ? '<div class="vcap-hint">📝 AI captions read your vault’s descriptions — they light up once this account’s vault is <b>Collected</b> (Vault manager → Collect). Live OnlyFans media carries no descriptions to show.</div>'
        : '';
    }

    // Display-ready thumb for the grid <img>: mirror rows carry a relay path in
    // _thumb; live OF urls are IP-signed so they must go through /img.
    function tileThumb(m){
      if(SRC === 'mirror') return m._thumb || '';
      var f = m.files && (m.files.thumb || m.files.squarePreview || m.files.preview || m.files.full);
      return (f && f.url) ? imgProxy(f.url) : '';
    }
    // RAW thumb stored on data-thumb for the composer preview: renderAttachments
    // proxies exactly once, so we must hand it the UN-proxied source (the raw OF
    // url for live; the already-relay-resolvable _thumb path for mirror, which
    // renderAttachments leaves untouched). Storing the display value here is what
    // double-proxied the live preview into a broken image.
    function tileThumbRaw(m){
      if(SRC === 'mirror') return m._thumb || '';
      var f = m.files && (m.files.thumb || m.files.squarePreview || m.files.preview || m.files.full);
      return (f && f.url) ? f.url : '';
    }
    function tileHtml(m){
      mediaMeta[m.id] = m;
      var thumb = tileThumb(m), thumbRaw = tileThumbRaw(m);
      var sel = chosen[m.id] != null ? ' sel' : '';
      var isVid = m.type === 'video', isGif = m.type === 'gif';
      var extra = '';
      if(isVid){
        var drm = isDrmVideo(m);
        extra += '<span class="vplay" data-play="' + esc(m.id) + '" title="' + (drm ? 'DRM — preview frames' : 'Play preview') + '">' + PLAY_SVG + '</span>';
        if(m.duration) extra += '<span class="vdur">' + PLAY_SVG + fmtDur(m.duration) + '</span>';
        if(drm) extra += '<span class="vlock" title="OnlyFans DRM">🔒</span>';
      }
      // Per-fan history ring: don't re-sell content this fan already bought,
      // and see at a glance what's been sent-but-unbought. Fills in a beat after
      // the grid (fan-history fetch is non-blocking).
      var h = histMap[m.id], histBadge = '';
      if(h){
        if(h.was_purchased) histBadge = '<span class="vt-own" title="This fan already OWNS this — bought ' +
          esc(Fastt.fmtCents(h.total_paid_cents || h.last_price_cents || 0)) + '">✓ owned</span>';
        else if(h.send_count > 0) histBadge = '<span class="vt-sent" title="Sent to this fan ' + esc(h.send_count) +
          '× · not bought">sent</span>';
      }
      var described = !!(m._ai && m._ai.describe_status === 'described');
      var cap = state.captions ? vAiCaption(m) : null;
      var capHtml = cap ? ('<div class="vcap">' +
          (cap.text ? '<p>' + esc(cap.text) + '</p>' : '') +
          (cap.tags.length ? '<div class="vcap-tags">' + cap.tags.map(function(t){ return '<span>' + esc(t) + '</span>'; }).join('') + '</div>' : '') +
        '</div>') : '';
      return '<div class="mt' + sel + '" data-mid="' + esc(m.id) + '" data-thumb="' + esc(thumbRaw) + '">' +
        (thumb ? '<img src="' + esc(thumb) + '" alt="" loading="lazy" decoding="async" onerror="this.remove()">' : '') +
        extra + capHtml + histBadge +
        (described && !histBadge ? '<span class="vt-dot" title="AI-described"></span>' : '') +
        '<span class="tag">' + esc(isGif ? 'GIF' : (m.type || '')) + '</span></div>';
    }

    async function fetchPage(offset){
      if(SRC === 'mirror'){
        var p = { account_id: aid, limit: PAGE, offset: offset, sort: state.sort };
        if(state.type !== 'all') p.type = state.type;
        if(state.query) p.query = state.query;
        if(state.listId != null) p.of_folder_id = state.listId;
        var out = await Fastt.get('/admin/vault-ai/items', p, opts);
        return { list: out.list || [], hasMore: !!out.hasMore };
      }
      var q = { limit: PAGE, offset: offset, field: 'recent', sort: state.sort === 'newest' ? 'desc' : 'asc' };
      if(state.type !== 'all') q.type = state.type;
      if(state.query) q.query = state.query;
      if(state.listId != null) q.list_id = state.listId;
      var out2 = await Fastt.get('/api/of/v2/vault/media', q, opts);
      return { list: out2.list || [], hasMore: !!out2.hasMore };
    }
    function renderGrid(){
      hov.stop();   // the tiles this preview points at are about to be replaced
      if(!page.items.length){
        bodyEl.innerHTML = honest('No media here',
          (state.query || state.type !== 'all' || state.listId != null)
            ? 'Nothing in the vault matches this filter.'
            : 'The vault returned 0 items for account ' + aid + '.');
        return;
      }
      bodyEl.innerHTML = '<div class="mgrid">' + page.items.map(tileHtml).join('') + '</div>' +
        '<div class="vmore-row"><span>Showing ' + page.items.length + ' · ' + (SRC === 'mirror' ? '⚡ local cache' : 'live from OnlyFans') + '</span>' +
        (page.hasMore ? '<button class="btn-sm" id="vMore">Load ' + PAGE + ' more</button>' : '') + '</div>';
    }
    async function loadGrid(append){
      if(append && page.loading) return;
      page.loading = true; var seq = ++page.seq;
      if(!append){ page.items = []; page.offset = 0; bodyEl.innerHTML = '<div class="subtle" style="padding:8px 2px">Loading media…</div>'; }
      try{
        var out = await fetchPage(page.offset);
        if(seq !== page.seq) return;   // a newer filter change superseded this fetch
        page.items = page.items.concat(out.list);
        page.offset += out.list.length;
        page.hasMore = out.hasMore && out.list.length > 0;
        renderGrid();
      }catch(e){
        if(seq === page.seq) bodyEl.innerHTML = honest('Vault unavailable',
          (SRC === 'mirror' ? 'GET /admin/vault-ai/items' : 'GET /api/of/v2/vault/media') + ' returned ' + ((e && e.status) || 'an error') + ' for account ' + aid + '.');
      }finally{ if(seq === page.seq) page.loading = false; }
    }

    // mirror rows are trimmed (no videoSources / DRM manifest) — fetch the full
    // playable shape on demand, exactly like the browser vault manager.
    async function playItem(m){
      if(!m) return;
      if(progressiveVideoSrc(m) || videoPosterFrames(m).length){ openVaultVideo(m); return; }
      try{ var full = await Fastt.get('/api/of/v2/vault/media/' + m.id, null, opts); openVaultVideo(full || m); }
      catch(e){ openVaultVideo(m); }
    }


    function chooseFolder(lid){
      state.listId = (lid === '' || lid == null) ? null : Number(lid);
      if(state.listId != null){
        vBumpCount(aid, state.listId);
        mru = [state.listId].concat(mru.filter(function(x){ return x !== state.listId; })).slice(0, 3);
        vSaveMru(aid, fid, mru); counts = vLoadCounts(aid);
      }
      persistState();
      renderToolbar(); renderChips(); loadGrid(false);
    }

    // ── delegated clicks ──
    bodyEl.addEventListener('click', function(e){
      var play = e.target.closest('[data-play]');
      if(play){ e.stopPropagation(); playItem(mediaMeta[play.getAttribute('data-play')]); return; }
      if(e.target.closest('#vMore')){ loadGrid(true); return; }
      var t = e.target.closest('.mt');
      if(t){
        var id = t.getAttribute('data-mid');
        if(chosen[id] != null){ delete chosen[id]; t.classList.remove('sel'); }
        else { chosen[id] = t.getAttribute('data-thumb') || ''; t.classList.add('sel'); }
        updSel();
      }
    });
    tbEl.addEventListener('click', function(e){
      var b = e.target.closest('[data-type]');
      if(b){ state.type = b.getAttribute('data-type'); persistState(); renderToolbar(); loadGrid(false); return; }
      if(e.target.closest('#vCapTog')){
        state.captions = !state.captions;
        try{ localStorage.setItem('ft_vault_captions', state.captions ? '1' : '0'); }catch(_e){}
        renderToolbar(); renderHint();
        // Don't repaint a mid-flight load (page.items is reset to [] → would flash
        // a false "nothing matches"); the in-flight loadGrid renders with captions
        // already applied when it resolves.
        if(!page.loading) renderGrid();
        return;
      }
    });
    tbEl.addEventListener('change', function(e){
      if(e.target.id === 'vFolder'){ chooseFolder(e.target.value); return; }
      if(e.target.id === 'vSort'){ state.sort = e.target.value; persistState(); loadGrid(false); return; }
    });
    tbEl.addEventListener('input', Fastt.debounce(function(){
      var q = sh.querySelector('#vQ'); if(!q) return;
      var v = q.value.trim(); if(v === state.query) return;
      state.query = v; loadGrid(false);
    }, 300));
    chipsEl.addEventListener('click', function(e){
      var c = e.target.closest('[data-lid]'); if(!c) return;
      chooseFolder(c.getAttribute('data-lid'));
    });

    sh.querySelector('#vaultAdd').addEventListener('click', function(){
      Object.keys(chosen).forEach(function(id){
        if(!attachments.some(function(a){ return String(a.id) === String(id); }))
          attachments.push({id: id, thumb: chosen[id] || null});
      });
      renderAttachments(); sh.close();
      Fastt.toast(Object.keys(chosen).length + ' item(s) attached — review, then press Send');
    });
    // Refresh — bust the session + server caches for this creator, re-detect the
    // source (a fresh Collect may have populated the mirror), then reload.
    sh.querySelector('#vaultRefresh').addEventListener('click', async function(){
      var btn = this; btn.disabled = true; btn.textContent = '↻ Refreshing…';
      VAULT_CACHE.folders[aid] = null; VAULT_CACHE.summary[aid] = undefined;
      try{ await Fastt.post('/admin/vault/cache/invalidate', {}, {acct: aid}); }catch(e){}
      try{ var s2 = await Fastt.get('/admin/vault-ai/cache/summary', {account_id: aid}, opts); VAULT_CACHE.summary[aid] = s2; SRC = (s2 && s2.count > 0) ? 'mirror' : 'live'; }catch(e){}
      try{ var l2 = await Fastt.get('/api/of/v2/vault/lists', {limit: 50}, opts); folders = l2.list || []; VAULT_CACHE.folders[aid] = folders; }catch(e){}
      renderToolbar(); renderChips(); renderHint(); await loadGrid(false);
      btn.disabled = false; btn.textContent = '↻ Refresh';
    });

    renderToolbar(); renderChips(); renderHint(); loadGrid(false);

    // Per-fan send/purchase history for the owned/sent rings — non-blocking, so
    // it never delays the grid. Cached per (account, fan); repaints rings in when
    // it lands (guarded so it can't overwrite an in-flight load with an empty grid).
    if(fid != null){
      var hk = aid + ':' + fid, cachedH = VAULT_CACHE.hist[hk];
      if(cachedH){ histMap = cachedH; }
      else{
        Fastt.get('/admin/vault/fan-history', {account_id: aid, fan_id: fid}, opts)
          .then(function(fh){ histMap = (fh && fh.by_media) || {}; VAULT_CACHE.hist[hk] = histMap;
            // Only repaint rings onto an already-rendered non-empty grid — never
            // over an in-flight load OR a "Vault unavailable"/empty honest state
            // (there are no tiles to ring, and it would clobber the real message).
            if(!page.loading && page.items.length) renderGrid(); })
          .catch(function(){});
      }
    }
  }
  $id('cbVault').addEventListener('click', openVault);
  $id('cbUpload').addEventListener('click', openVault);

  // ── GIF picker (relay's giphy proxy) ──────────────────────
  $id('cbGif').addEventListener('click', async function(){
    var sh = sheet('GIF', '<input type="text" id="gifQ" placeholder="Search Giphy…" ' +
      'style="width:100%;background:#151515;border:1px solid #333;border-radius:8px;color:#eee;padding:8px 10px;font:13px Inter,sans-serif">' +
      '<div id="gifGrid" style="margin-top:10px"><div class="subtle">Loading trending…</div></div>');
    async function run(q){
      var grid = sh.querySelector('#gifGrid');
      grid.innerHTML = '<div class="subtle">Loading…</div>';
      try{
        var out = q
          ? await Fastt.get('/api/of/v2/giphy/proxy/gifs/search', {q: q, limit: 24}, {acct: activeAid})
          : await Fastt.get('/api/of/v2/giphy/proxy/gifs/trending', {limit: 24}, {acct: activeAid});
        var data = out.data || [];
        if(!data.length){ grid.innerHTML = '<div class="subtle">No GIFs found.</div>'; return; }
        grid.innerHTML = '<div class="mgrid">' + data.map(function(g){
          var im = (g.images && (g.images.fixed_width_small || g.images.preview_gif || g.images.original)) || {};
          return '<div class="mt" data-gid="' + esc(g.id) + '">' +
            (im.url ? '<img src="' + esc(im.url) + '" alt="" loading="lazy">' : '') + '</div>';
        }).join('') + '</div>';
      }catch(e){
        grid.innerHTML = honest('Giphy proxy unavailable',
          'GET /api/of/v2/giphy/proxy/gifs/* returned ' + ((e && e.status) || 'an error') + '.');
      }
    }
    sh.body.addEventListener('click', function(e){
      var t = e.target.closest('.mt'); if(!t) return;
      giphyId = t.getAttribute('data-gid');
      renderAttachments(); sh.close();
      Fastt.toast('GIF attached — review, then press Send');
    });
    sh.querySelector('#gifQ').addEventListener('keydown', function(e){
      if(e.key === 'Enter') run(this.value.trim());
    });
    run(null);
  });

  // ── schedule ──────────────────────────────────────────────
  async function openSchedule(){
    scheduleOn = true; renderAttachments();
    var sh = sheet('Scheduled messages', '<div class="subtle">Loading…</div>');
    try{
      var out = await Fastt.get('/api/of/v2/schedules/later/chat', null, {acct: activeAid});
      var list = out.list || [];
      sh.body.innerHTML = (list.length
        ? '<table class="stab"><thead><tr><th>When</th><th>Text</th><th class="r">Price</th></tr></thead><tbody>' +
          list.map(function(s){
            return '<tr><td>' + esc(Fastt.fmtDate(s.scheduledDate || s.scheduled_date)) + '</td>' +
              '<td style="white-space:normal">' + esc(stripHtml(s.text || '')).slice(0,120) + '</td>' +
              '<td class="r">' + Fastt.fmtMoney(Number(s.price)||0) + '</td></tr>';
          }).join('') + '</tbody></table>'
        : honest('Nothing scheduled', 'GET /api/of/v2/schedules/later/chat returned an empty list for this creator.')) +
        '<div class="subtle" style="margin-top:12px">The composer is now in <b>schedule</b> mode — pick a ' +
        'date/time above the send row, then press Send to queue it (you will be asked to confirm).</div>';
    }catch(e){
      sh.body.innerHTML = honest('Schedule list unavailable',
        'GET /api/of/v2/schedules/later/chat returned ' + ((e && e.status) || 'an error') + '.');
    }
  }
  $id('cbSched').addEventListener('click', openSchedule);
  $id('cbCal').addEventListener('click', openSchedule);

  // ── tag creators / mention ────────────────────────────────
  $id('cbTag').addEventListener('click', async function(e){
    e.stopPropagation();
    var btn = this;
    try{
      var out = await Fastt.get('/api/of/v2/posts/tagged-friend-users', {limit: 30, offset: 0}, {acct: activeAid});
      var items = out.items || out.list || [];
      popover(btn, '<div class="phead">Tag a creator (' + items.length + ')</div>' +
        (items.length ? items.map(function(i){
          var u = i.user || i;
          return '<div class="prow" data-u="' + esc(u.username || '') + '">' +
            '<span class="pav">' + esc(initials(u.name)) + (u.avatar ? '<img src="' + esc(imgProxy(u.avatar)) + '" onerror="this.remove()">' : '') + '</span>' +
            '<span class="ptxt">' + esc(u.name || u.username) + '</span></div>';
        }).join('') : '<div class="prow dim">no tagged-friend creators available</div>'),
        function(row){ insertText('@' + row.getAttribute('data-u')); closePops(); });
    }catch(err){ Fastt.oops(err); }
  });
  $id('cbMention').addEventListener('click', function(){
    if(!currentFan) return Fastt.toast('Open a conversation first');
    var u = fanInfo[key(currentAid, currentFan)] || {};
    if(u.username) insertText('@' + u.username);
    else Fastt.toast('No username known for this fan');
  });

  // ── translate the draft ───────────────────────────────────
  $id('cbTranslate').addEventListener('click', async function(e){
    e.stopPropagation();
    var txt = input.value.trim();
    if(!txt) return Fastt.toast('Type something to translate first');
    var btn = this;
    var LANGS = [['en','English'],['es','Spanish'],['sl','Slovenian'],['de','German'],['fr','French'],
                 ['it','Italian'],['pt','Portuguese'],['nl','Dutch'],['tr','Turkish']];
    popover(btn, '<div class="phead">Translate the draft to…</div>' +
      LANGS.map(function(l){ return '<div class="prow" data-l="' + l[0] + '">' + l[1] + '</div>'; }).join(''),
      async function(row){
        closePops();
        try{
          var out = await Fastt.post('/admin/translate', {texts: [txt], target: row.getAttribute('data-l')},
                                     {acct: activeAid});
          // /admin/translate → {results:[{text, lang}]}
          var t = (out.results || [])[0];
          if(t && typeof t === 'object') t = t.text;
          if(t){ input.value = t; input.dispatchEvent(new Event('input')); Fastt.toast('Translated'); }
          else Fastt.toast('Translator returned nothing', 'err');
        }catch(err){ Fastt.oops(err); }
      });
  });

  // ═══════════════════════════════════════════════════════════
  // THREAD TOOLBAR
  // ═══════════════════════════════════════════════════════════
  $id('tbMute').addEventListener('click', async function(){
    if(!currentFan) return;
    var muted = !!(currentChatMeta && currentChatMeta.isMutedNotifications);
    if(!confirm((muted ? 'Unmute' : 'Mute') + ' OnlyFans notifications for this chat?')) return;
    try{
      if(muted) await adel('/api/of/v2/chats/' + currentFan + '/mute', currentAid);
      else await apost('/api/of/v2/chats/' + currentFan + '/mute', {}, currentAid);
      if(currentChatMeta) currentChatMeta.isMutedNotifications = !muted;
      paintMuteBtn();
      Fastt.saved(muted ? 'Unmuted' : 'Muted');
    }catch(e){ Fastt.oops(e); }
  });
  $id('tbNotes').addEventListener('click', function(){ setRightTab('notes'); });
  // ── in-page chat tabs ─────────────────────────────────────
  // "Pop out" pins the open chat as a tab in a strip above the thread, so the
  // operator can juggle several conversations without losing any — WITHOUT
  // spawning a floating OS window (which is what the old window.open did, and
  // what the operator asked us to stop doing). Tabs persist across reloads.
  function tabKey(aid, fid){ return String(aid) + ':' + String(fid); }
  function renderTabStrip(){
    var strip = $id('threadTabs');
    if(!OPEN_TABS.length){ strip.style.display = 'none'; strip.innerHTML = ''; return; }
    strip.style.display = '';
    strip.innerHTML = OPEN_TABS.map(function(t){
      var active = String(t.aid) === String(currentAid) && String(t.fanId) === String(currentFan);
      // Per-model cue: colour dot from the tab's creator, so a strip of popped-out
      // fans across several models stays readable at a glance.
      var cre = byAid[String(t.aid)] || {};
      var dot = '<span class="ttab-dot" style="background:' + esc(cre.color || '#666') + '" title="' +
        esc(cre.name || t.aid) + '"></span>';
      return '<div class="ttab' + (active ? ' active' : '') + '" data-aid="' + esc(t.aid) + '" data-fan="' + esc(t.fanId) +
        '" title="' + esc((t.name || '') + (cre.name ? ' · ' + cre.name : '')) + '">' + dot +
        '<span class="ttab-name">' + esc(t.name || ('Fan ' + t.fanId)) +
        '</span><span class="ttab-x" data-close="1" title="Close tab">✕</span></div>';
    }).join('');
  }
  function addOpenTab(aid, fid, name){
    if(!aid || fid == null) return;
    if(!OPEN_TABS.some(function(t){ return tabKey(t.aid, t.fanId) === tabKey(aid, fid); })){
      OPEN_TABS.push({ aid: String(aid), fanId: String(fid), name: name || '' });
      lsSet(LS_TABS, OPEN_TABS);
    }
    renderTabStrip();
  }
  function removeOpenTab(aid, fid){
    var wasActive = String(aid) === String(currentAid) && String(fid) === String(currentFan);
    OPEN_TABS = OPEN_TABS.filter(function(t){ return tabKey(t.aid, t.fanId) !== tabKey(aid, fid); });
    lsSet(LS_TABS, OPEN_TABS);
    if(wasActive && OPEN_TABS.length){ var n = OPEN_TABS[OPEN_TABS.length - 1]; selectFan(n.fanId, n.aid, false); }
    else if(wasActive){ clearSelection(); }
    renderTabStrip();
  }
  $id('threadTabs').addEventListener('click', function(e){
    var tab = e.target.closest('.ttab'); if(!tab) return;
    var aid = tab.getAttribute('data-aid'), fid = tab.getAttribute('data-fan');
    if(e.target.closest('[data-close]')){ removeOpenTab(aid, fid); return; }
    if(String(aid) === String(currentAid) && String(fid) === String(currentFan)) return;   // already open
    selectFan(fid, aid, true);
  });
  $id('tbOpen').addEventListener('click', function(){
    if(!currentFan) return Fastt.toast('Open a conversation first');
    var u = fanInfo[key(currentAid, currentFan)] || {};
    var row = chats.filter(function(c){ return String(c.fanId) === String(currentFan) && String(c.aid) === String(currentAid); })[0] || {};
    var name = u.name || row.name || u.username || ('Fan ' + currentFan);
    var already = OPEN_TABS.some(function(t){ return tabKey(t.aid, t.fanId) === tabKey(currentAid, currentFan); });
    addOpenTab(currentAid, currentFan, name);
    Fastt.toast(already ? 'Already pinned as a tab' : 'Pinned as a tab — switch chats without losing this one');
  });
  $id('tbRefresh').addEventListener('click', function(){
    if(!currentFan) return;
    threadBody.innerHTML = '<div class="msg-meta">Refreshing from OnlyFans…</div>';
    threadBody._sig = null;   // placeholder isn't a real paint — let the forced revalidate always repaint
    loadThread(currentFan, currentAid, true);
    loadAiStatus(currentFan, currentAid);
  });
  // Session cache so re-opening a fan's gallery is instant (non-blocking); a
  // Refresh busts it. Same treatment as the vault picker.
  var GALLERY_CACHE = {};   // "aid:fanId" -> tiles[]
  $id('tbGallery').addEventListener('click', async function(){
    if(!currentFan) return;
    var aid = currentAid, fid = currentFan, ck = key(aid, fid);
    var sh = sheet('Chat gallery', '<div class="subtle">Loading media…</div>',
      '<button class="btn-sm" id="galRefresh" style="margin-left:auto">↻ Refresh</button>');
    function paint(tiles){
      sh.body.innerHTML = tiles.length
        ? '<div class="mgrid">' + tiles.map(function(t){
            return '<div class="mt"><img src="' + esc(imgProxy(t.url)) + '" alt="" loading="lazy" decoding="async">' +
              (t.price ? '<span class="tag">' + Fastt.fmtMoney(t.price) + '</span>' : '') + '</div>';
          }).join('') + '</div>'
        : honest('No media in this chat',
            'GET /api/of/v2/chats/' + fid + '/media returned no viewable files.');
    }
    async function load(force){
      if(GALLERY_CACHE[ck] && !force){ paint(GALLERY_CACHE[ck]); return; }
      sh.body.innerHTML = '<div class="subtle">Loading media…</div>';
      try{
        var out = await aget('/api/of/v2/chats/' + fid + '/media', {limit: 30, offset: 0}, aid);
        var tiles = [];
        (out.list || []).forEach(function(m){
          (m.media || []).forEach(function(md){
            var f = md.files && (md.files.thumb || md.files.squarePreview || md.files.preview || md.files.full);
            if(f && f.url) tiles.push({url: f.url, price: m.price, type: md.type, canView: md.canView});
          });
        });
        GALLERY_CACHE[ck] = tiles;
        paint(tiles);
      }catch(e){
        sh.body.innerHTML = honest('Gallery unavailable',
          'GET /api/of/v2/chats/{id}/media returned ' + ((e && e.status) || 'an error') + '.');
      }
    }
    sh.querySelector('#galRefresh').addEventListener('click', function(){ load(true); });
    load(false);
  });
  $id('tbFind').addEventListener('click', function(e){
    e.stopPropagation();
    if(!currentFan) return;
    var p = popover(this, '<div class="phead">Search this conversation</div>' +
      '<input type="text" id="findQ" placeholder="word or phrase…">' +
      '<div class="prow" data-go="1">Search on OnlyFans</div><div id="findOut"></div>');
    var qi = p.querySelector('#findQ');
    qi.focus();
    async function go(){
      var q = qi.value.trim(); if(!q) return;
      var out = p.querySelector('#findOut');
      out.innerHTML = '<div class="prow dim">searching…</div>';
      try{
        var ids = await aget('/api/of/v2/chats/' + currentFan + '/messages/search', {query: q}, currentAid);
        ids = Array.isArray(ids) ? ids : (ids.list || []);
        if(!ids.length){ out.innerHTML = '<div class="prow dim">no matches</div>'; return; }
        var rows = await Promise.allSettled(ids.slice(0, 12).map(function(mid){
          return aget('/admin/messages/' + currentAid + '/' + currentFan + '/' + mid, null, currentAid);
        }));
        out.innerHTML = '<div class="pdiv"></div><div class="phead">' + ids.length + ' match' +
          (ids.length===1?'':'es') + '</div>' + rows.map(function(r, i){
            var m = r.status === 'fulfilled' ? r.value : null;
            var body = m ? stripHtml(m.body) : ('message ' + ids[i] + ' (not in our local mirror)');
            return '<div class="prow" data-mid="' + esc(ids[i]) + '"><span class="ptxt">' +
              esc(body).slice(0,110) + '</span></div>';
          }).join('');
      }catch(err){ out.innerHTML = '<div class="prow dim">search failed: ' + esc(String((err&&err.status)||err)) + '</div>'; }
    }
    qi.addEventListener('keydown', function(ev){ if(ev.key === 'Enter') go(); });
    p.addEventListener('click', function(ev){
      var row = ev.target.closest('.prow'); if(!row) return;
      if(row.getAttribute('data-go')) return go();
      var mid = row.getAttribute('data-mid');
      if(!mid) return;
      var el = threadBody.querySelector('[data-mid="' + mid + '"]');
      closePops();
      if(el){ el.scrollIntoView({block:'center'}); el.style.outline = '2px solid var(--pink)';
              setTimeout(function(){ el.style.outline = ''; }, 2500); }
      else Fastt.toast('That message is older than the loaded window');
    });
  });
  $id('tbPin').addEventListener('click', function(e){
    e.stopPropagation();
    if(!currentFan) return;
    var pinned = (currentChatMeta && currentChatMeta.countPinnedMessages) || 0;
    var recent = threadMsgs.slice(-15).reverse();
    popover(this, '<div class="phead">Pin a message · ' + pinned + ' pinned on OnlyFans</div>' +
      (recent.length ? recent.map(function(m){
        return '<div class="prow" data-mid="' + esc(m.id) + '"><span class="ptxt">' +
          esc(stripHtml(m.text) || '(media)').slice(0,80) + '</span>' +
          '<span class="pmeta">' + esc(fmtTime(m.createdAt)) + '</span></div>';
      }).join('') : '<div class="prow dim">no messages loaded</div>'),
      async function(row){
        var mid = row.getAttribute('data-mid');
        closePops();
        if(!confirm('Pin this message in the OnlyFans chat?')) return;
        try{
          await apost('/api/of/v2/messages/' + mid + '/pin/user/' + currentFan, {}, currentAid);
          Fastt.saved('Pinned');
          loadChatMeta(currentFan, currentAid);
        }catch(err){ Fastt.oops(err); }
      });
  });

  // ── kebab: the real app's ChatActionsMenu ─────────────────
  $id('tbKebab').addEventListener('click', async function(e){
    e.stopPropagation();
    if(!currentFan) return;
    var btn = this;
    var muted = !!(currentChatMeta && currentChatMeta.isMutedNotifications);
    var restricted = !!(lastOfUser && lastOfUser.isRestricted);
    var autoR = null;
    try{ autoR = await aget('/api/of/v2/automation-restrict/' + currentFan, null, currentAid); }catch(err){}
    var folders = [];
    try{
      var fo = await Fastt.get('/api/of/v2/chats/folders', null, {acct: currentAid});
      folders = (fo.list || []).filter(function(f){ return f.type === 'custom'; });
    }catch(err){}
    var inList = {};
    folders.forEach(function(f){
      inList[f.id] = (f.users || []).some(function(u){ return String(u.id) === String(currentFan); });
    });
    // Mass-message opt-out state (MASSppvEXCLUDE / MASSdmEXCLUDE)
    var dnm = {ppv:false, dm:false};
    var jm = await Promise.allSettled([
      Fastt.get('/admin/do-not-mass', {account_id: currentAid, fan_id: currentFan, kind:'ppv'}, {acct: currentAid}),
      Fastt.get('/admin/do-not-mass', {account_id: currentAid, fan_id: currentFan, kind:'dm'}, {acct: currentAid})
    ]);
    if(jm[0].status === 'fulfilled') dnm.ppv = !!jm[0].value.do_not_mass;
    if(jm[1].status === 'fulfilled') dnm.dm = !!jm[1].value.do_not_mass;
    var html =
      '<div class="phead">Chat actions</div>' +
      '<div class="prow" data-a="unread">Mark as unread</div>' +
      '<div class="prow" data-a="mute">' + (muted ? 'Unmute notifications' : 'Mute notifications') + '</div>' +
      '<div class="prow" data-a="hide">Hide chat (returns on his next message)</div>' +
      '<div class="prow" data-a="group">Open this chat in Group view</div>' +
      '<div class="prow" data-a="rescrape">↻ Re-scrape from OnlyFans</div>' +
      '<div class="pdiv"></div><div class="phead">Mass-message opt-out</div>' +
      '<div class="prow' + (dnm.ppv ? ' on' : '') + '" data-dnm="ppv">' + (dnm.ppv ? '✓ ' : '') + 'Exclude from PPV blasts</div>' +
      '<div class="prow' + (dnm.dm ? ' on' : '') + '" data-dnm="dm">' + (dnm.dm ? '✓ ' : '') + 'Exclude from DM blasts</div>' +
      '<div class="pdiv"></div><div class="phead">Restrictions</div>' +
      '<div class="prow' + (restricted ? ' danger' : '') + '" data-a="restrict">' +
        (restricted ? 'Lift OnlyFans restrict' : 'Restrict on OnlyFans') + '</div>' +
      '<div class="prow" data-a="autorestrict">' +
        (autoR && autoR.restricted ? 'Allow automations again' : 'Restrict automations for this fan') +
        '<span class="pmeta">' + esc((autoR && autoR.reason) || '') + '</span></div>' +
      '<div class="pdiv"></div><div class="phead">Lists (' + folders.length + ')</div>' +
      (folders.length ? folders.map(function(f){
        return '<div class="prow" data-list="' + esc(f.id) + '" data-in="' + (inList[f.id] ? '1' : '') + '">' +
          '<span class="ptxt">' + esc(f.name) + '</span><span class="pmeta">' +
          (inList[f.id] ? 'remove' : 'add') + '</span></div>';
      }).join('') : '<div class="prow dim">no custom lists on this account</div>');
    popover(btn, html, async function(row){
      var a = row.getAttribute('data-a');
      var listId = row.getAttribute('data-list');
      var dnmKind = row.getAttribute('data-dnm');
      closePops();
      try{
        if(dnmKind){
          var was = dnm[dnmKind];
          if(!confirm((was ? 'Allow this fan back into' : 'Exclude this fan from') + ' ' +
                      dnmKind.toUpperCase() + ' blasts on OnlyFans?')) return;
          await Fastt.api('/admin/do-not-mass', {method: was ? 'DELETE' : 'POST',
            params:{account_id: currentAid, fan_id: currentFan, kind: dnmKind},
            acct: currentAid});
          Fastt.saved(was ? ('Back in ' + dnmKind.toUpperCase() + ' blasts')
                          : ('Excluded from ' + dnmKind.toUpperCase() + ' blasts'));
          return;
        }
        if(listId){
          var isIn = !!row.getAttribute('data-in');
          if(!confirm((isIn ? 'Remove' : 'Add') + ' this fan ' + (isIn ? 'from' : 'to') + ' that OnlyFans list?')) return;
          if(isIn) await adel('/api/of/v2/lists/' + listId + '/users/' + currentFan, currentAid);
          else await apost('/api/of/v2/lists/' + listId + '/users/' + currentFan, {}, currentAid);
          Fastt.saved(isIn ? 'Removed from list' : 'Added to list');
          return;
        }
        if(a === 'unread'){
          if(!confirm('Mark this conversation as unread on OnlyFans?')) return;
          await adel('/api/of/v2/chats/' + currentFan + '/mark-as-read', currentAid);
          Fastt.saved('Marked unread'); reloadList(true); return;
        }
        if(a === 'mute'){ $id('tbMute').click(); return; }
        if(a === 'group'){
          // Open (or focus) the Group view with this chat appended.
          window.open('group.html?add=' + encodeURIComponent(currentAid + ':' + currentFan), 'ft-group');
          return;
        }
        if(a === 'rescrape'){
          await Fastt.post('/admin/messages/' + currentAid + '/' + currentFan + '/rescrape-now', {},
                           {acct: currentAid});
          Fastt.saved('Re-scrape queued — refreshing…');
          setTimeout(function(){ if(currentFan) loadThread(currentFan, currentAid, true); }, 1500);
          return;
        }
        if(a === 'hide'){
          if(!confirm('Hide this chat? It comes back on his next message.')) return;
          await apost('/api/of/v2/chats/' + currentFan + '/hide', {}, currentAid);
          Fastt.saved('Chat hidden'); clearSelection(); reloadList(true); return;
        }
        if(a === 'restrict'){
          if(!confirm((restricted ? 'LIFT the OnlyFans restrict on' : 'RESTRICT') + ' this fan on OnlyFans?')) return;
          if(restricted) await adel('/api/of/v2/users/' + currentFan + '/restrict', currentAid);
          else await apost('/api/of/v2/users/' + currentFan + '/restrict', {}, currentAid);
          if(lastOfUser) lastOfUser.isRestricted = !restricted;
          Fastt.saved(restricted ? 'Restrict lifted' : 'Fan restricted'); return;
        }
        if(a === 'autorestrict'){
          var on = !!(autoR && autoR.restricted);
          if(!confirm(on ? 'Let automations message this fan again?'
                         : 'Stop ALL automations from messaging this fan?')) return;
          if(on) await adel('/api/of/v2/automation-restrict/' + currentFan, currentAid);
          else await apost('/api/of/v2/automation-restrict/' + currentFan, {}, currentAid);
          Fastt.saved(on ? 'Automations allowed' : 'Automations restricted'); return;
        }
      }catch(err){ Fastt.oops(err); }
    });
  });

  // ── Model persona "right now" (the real app's ModelInfoButton) ─────
  // Leads with what she is *doing right now* (local clock + the matching
  // time_activity line the send-welcome engine itself would pick), so the
  // chatter stays in character — then bio + location, then a muted model/cap
  // footer. Source: the same owner-gated /admin/account-config the Brain uses.
  var cfgCache = {};
  var SLOT_KEYS = ['morning_1','morning_2','afternoon_1','afternoon_2','evening','night'];
  var SLOT_LABELS = {morning_1:'Early morning',morning_2:'Late morning',afternoon_1:'Early afternoon',
                     afternoon_2:'Late afternoon',evening:'Evening',night:'Night'};
  function slotForHour(h){
    if(h>=5&&h<9) return 0; if(h>=9&&h<12) return 1; if(h>=12&&h<15) return 2;
    if(h>=15&&h<18) return 3; if(h>=18&&h<21) return 4; return 5;
  }
  function localClock(off){
    off = isFinite(off) ? off : 0;
    var now = new Date();
    var total = now.getUTCHours()*60 + now.getUTCMinutes() + Math.round(off*60);
    var w = ((total % 1440) + 1440) % 1440, h = Math.floor(w/60), m = w % 60;
    var ap = h>=12?'PM':'AM', h12 = (h%12===0)?12:h%12;
    return {label: h12 + ':' + String(m).padStart(2,'0') + ' ' + ap, hour: ((Math.trunc(off)+now.getUTCHours())%24+24)%24};
  }
  $id('tbModel').addEventListener('click', async function(e){
    e.stopPropagation();
    var aid = currentAid || activeAid;
    var btn = this;
    var got = cfgCache[aid];
    if(!got){
      try{ var r = await aget('/admin/account-config', null, aid); got = cfgCache[aid] = (r && r.config) || {}; }
      catch(err){ return Fastt.oops(err); }
    }
    var c = got, cl = localClock(c.utc_offset);
    var acts = c.time_activities || {};
    var nowAct = (acts[SLOT_KEYS[slotForHour(cl.hour)]] || '').trim();
    var cre = (byAid[aid] || {});
    var bio = String(c.persona || '').trim();
    var cap = Number(c.daily_cost_cap_cents) > 0 ? '$' + (c.daily_cost_cap_cents/100).toFixed(2) + '/day' : '—';
    var haveDay = SLOT_KEYS.some(function(k){ return (acts[k]||'').trim(); });
    var html = '<div class="phead">' + esc(cre.name || aid) + ' · persona</div>' +
      '<div class="mi-now"><div class="mi-t">Right now · ' + esc(cl.label) + ' her time</div>' +
        esc(nowAct || 'No activity line set for this time of day.') + '</div>' +
      (bio ? '<div class="mi-row">' + esc(bio.length>420 ? bio.slice(0,420)+'…' : bio) + '</div>' : '') +
      (c.location ? '<div class="mi-row">📍 <b>' + esc(c.location) + '</b>' +
        (c.utc_offset!=null ? ' · UTC' + (c.utc_offset>=0?'+':'−') + Math.abs(c.utc_offset) : '') + '</div>' : '') +
      (haveDay ? '<div class="pdiv"></div><div class="phead">Her day</div>' +
        SLOT_KEYS.filter(function(k){ return (acts[k]||'').trim(); }).map(function(k){
          var on = k === SLOT_KEYS[slotForHour(cl.hour)];
          return '<div class="mi-row"' + (on?' style="color:#ffd8ec"':'') + '><b>' + esc(SLOT_LABELS[k]) + '</b> — ' +
            esc(acts[k]) + '</div>';
        }).join('') : '') +
      '<div class="mi-foot">Model ' + esc(c.model || '—') + ' · cost cap ' + esc(cap) +
        ' · read-only (edit in Brain / account config)</div>';
    popover(btn, html);
  });

  // ── Per-chat scheduled sends (the real app's ScheduledForChat) ─────
  // Only this fan's own queued chat sends (userIds === [fanId]); mass sends
  // that merely include him are the Broadcast tab's job. Cancel is DELETE
  // /api/of/v2/messages/queue/{id}, click+confirm-gated.
  var schedForFan = [];
  async function loadSchedChip(fid, aid){
    var chip = $id('schedChip');
    chip.style.display = 'none'; schedForFan = [];
    try{
      var out = await Fastt.get('/api/of/v2/schedules', null, {acct: aid, priority: 'background'});
      if(String(currentFan) !== String(fid)) return;
      var list = (out && out.list) || [];
      schedForFan = list.filter(function(it){
        var e = it.entity || {};
        var uids = e.userIds || [];
        return it.type === 'chat' && uids.length === 1 && String(uids[0]) === String(fid) && isFinite(Number(e.id));
      }).map(function(it){
        var e = it.entity || {};
        return {id: Number(e.id), text: e.text || '', price: Number(e.price)||0,
                at: it.publishDateTime || e.scheduledDate || null};
      }).sort(function(a,b){ return String(a.at||'~') < String(b.at||'~') ? -1 : 1; });
      if(schedForFan.length){
        $id('schedChipN').textContent = schedForFan.length;
        chip.style.display = '';
        chip.title = schedForFan.length + ' send' + (schedForFan.length===1?'':'s') + ' queued for this fan — click to view or cancel';
      }
    }catch(e){ /* stays hidden — an empty queue is not an error */ }
  }
  $id('schedChip').addEventListener('click', function(e){
    e.stopPropagation();
    if(!schedForFan.length) return;
    var pop = popover(this, '<div class="phead">Scheduled for this fan (' + schedForFan.length + ')</div>' +
      schedForFan.map(function(s){
        return '<div class="prow" style="align-items:flex-start"><span class="ptxt">' +
          '<div>' + esc(stripHtml(s.text) || (s.price ? Fastt.fmtMoney(s.price)+' PPV' : '(media)')).slice(0,90) + '</div>' +
          '<div style="color:#8a8a8a;font-size:11px">' + esc(s.at ? Fastt.fmtDate(s.at) : 'no date') +
            (s.price ? ' · ' + Fastt.fmtMoney(s.price) : '') + '</div></span>' +
          '<span class="pmeta" data-cancel="' + esc(s.id) + '" style="color:#f08a8a;cursor:pointer">Cancel</span></div>';
      }).join(''));
    pop.addEventListener('click', function(ev){
      var c = ev.target.closest('[data-cancel]'); if(!c) return;
      ev.stopPropagation();
      var id = c.getAttribute('data-cancel');
      if(!confirm('Cancel this queued send? It will NOT go out to the fan.')) return;
      closePops();
      adel('/api/of/v2/messages/queue/' + id, currentAid)
        .then(function(){ Fastt.saved('Scheduled send cancelled'); loadSchedChip(currentFan, currentAid); })
        .catch(Fastt.oops);
    });
  });

  // ═══════════════════════════════════════════════════════════
  // NOTIFICATIONS
  // ═══════════════════════════════════════════════════════════
  var notifBadge = document.querySelector('.notif-badge');
  async function loadNotifCount(){
    var c = byAid[activeAid];
    if(!c || !c.hasSession){ notifBadge.style.display = 'none'; return; }
    try{
      var n = await Fastt.get('/api/of/v2/users/notifications/count', null,
                              {acct: activeAid, priority: 'background'});
      var all = Number(n && n.all) || 0;
      notifBadge.textContent = all;
      notifBadge.style.display = all ? '' : 'none';
      document.querySelector('.btn-notif').setAttribute('title',
        'OnlyFans notifications for ' + (c.name || activeAid) + ': ' +
        Object.keys(n).filter(function(k){ return n[k]; }).map(function(k){ return k + ' ' + n[k]; }).join(' · ') || 'none');
    }catch(e){ notifBadge.style.display = 'none'; }
  }
  document.querySelector('.btn-notif').addEventListener('click', async function(){
    var sh = sheet('Notifications · ' + esc((byAid[activeAid]||{}).name || activeAid),
      '<div class="subtle">Loading…</div>');
    var counts = {};
    try{ counts = await Fastt.get('/api/of/v2/users/notifications/count', null, {acct: activeAid}); }catch(e){}
    var TYPES = ['all','purchases','tip','subscribed','message','mentioned','commented','favorited','tags'];
    async function run(type){
      sh.body.innerHTML = '<div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:10px">' +
        TYPES.map(function(t){
          return '<button class="btn-sm' + (t === type ? ' pnk' : '') + '" data-t="' + t + '">' + t +
            (counts[t] ? ' (' + counts[t] + ')' : '') + '</button>';
        }).join('') + '</div><div id="nOut" class="nlist"><div class="subtle">Loading…</div></div>';
      var out = sh.querySelector('#nOut');
      try{
        var params = {limit: 30, offset: 0};
        if(type !== 'all') params.type = type;   // plural type values 400 — these are the singular count keys
        var list = await Fastt.get('/api/of/v2/users/notifications', params, {acct: activeAid});
        list = Array.isArray(list) ? list : (list.list || []);
        if(!list.length){
          out.innerHTML = honest('Nothing here', 'OnlyFans returned no "' + type + '" notifications for this creator.');
          return;
        }
        out.innerHTML = list.map(function(n){
          var u = n.user || {};
          var txt = stripHtml(n.text || '');
          var rp = n.replacePairs || {};
          Object.keys(rp).forEach(function(k){ txt = txt.split(k).join(stripHtml(rp[k])); });
          var av = (u.avatarThumbs && u.avatarThumbs.c50) || u.avatar;
          return '<div class="n' + (n.isRead ? '' : ' unread') + '">' +
            '<div class="nav">' + esc(initials(u.name)) + (av ? '<img src="' + esc(imgProxy(av)) + '" onerror="this.remove()">' : '') + '</div>' +
            '<div class="nt"><b>' + esc(u.name || u.username || 'someone') + '</b> ' + esc(txt) +
            '<div class="na">' + esc(Fastt.fmtAgo(n.createdAt)) + ' · ' + esc(n.type || '') + '</div></div></div>';
        }).join('');
      }catch(e){
        out.innerHTML = honest('Notifications unavailable',
          'GET /api/of/v2/users/notifications?type=' + type + ' returned ' + ((e && e.status) || 'an error') + '.');
      }
    }
    sh.body.addEventListener('click', function(e){
      var b = e.target.closest('button[data-t]'); if(!b) return;
      run(b.getAttribute('data-t'));
    });
    run('all');
  });

  // ═══════════════════════════════════════════════════════════
  // FILTERS / SORT / SEARCH
  // ═══════════════════════════════════════════════════════════
  var chipEls = document.querySelectorAll('.chips .chip');
  function chipByRole(role){ return document.querySelector('.chips .chip[data-role="' + role + '"]'); }
  var SERVER_ROLES = {pinned:1, priority:1, unread:1};   // OnlyFans-side filters
  ['pinned','priority'].forEach(function(r){
    var c = chipByRole(r), cb = c && c.querySelector('.cbadge'); if(cb) cb.style.display = 'none';
  });
  $id('chipTips').setAttribute('title', 'Rows where OnlyFans reports hasUnreadTips = true right now (not lifetime tippers)');
  chipByRole('owe').setAttribute('title',
    'Must-reply queue — chats where the fan sent the last message and we have not replied yet (client-side over the loaded window).');
  // Highlight one "primary" chip (all/unread/owe/pinned/priority). Tips is an
  // independent toggle and keeps its own active state.
  function setPrimaryChip(role){
    Array.prototype.forEach.call(chipEls, function(c){
      if(c.getAttribute('data-role') === 'tips') return;
      c.classList.toggle('active', c.getAttribute('data-role') === role);
    });
  }
  Array.prototype.forEach.call(chipEls, function(chip){
    chip.addEventListener('click', function(){
      var role = chip.getAttribute('data-role');
      if(role === 'tips'){
        withTips = !withTips; chip.classList.toggle('active', withTips); renderRows(); return;
      }
      if(role === 'owe'){
        // The "must reply" queue: load the full (unfiltered) window, then keep
        // only chats the fan sent last — no OF-side filter exists for this.
        // Only single-creator: the aggregated feed (/admin/chats/recent) carries
        // no message direction, so owe-reply is unknowable there.
        if(inboxMode === 'all'){
          Fastt.toast('Owe reply needs a single creator — the aggregated inbox has no message-direction data');
          return;
        }
        oweOnly = true; filter = null; folderId = null; setPrimaryChip('owe'); renderFolderChips(); reloadList(); return;
      }
      if(inboxMode === 'all' && SERVER_ROLES[role]){
        Fastt.toast('Pinned / Priority / Unread are OnlyFans-side filters — switch to a single creator to use them');
        return;
      }
      oweOnly = false;
      filter = SERVER_ROLES[role] ? role : null;   // 'all' → no filter
      folderId = null;                              // leaving a folder view
      setPrimaryChip(role);
      renderFolderChips();
      reloadList();
    });
  });

  // ── OF chat-folder filter chips (pinned fan-lists) ──────────
  var folderCache = {};   // aid -> filtered custom folders (session cache; avoids re-fetch each reload)
  async function loadFolderChips(aid){
    if(inboxMode !== 'creator'){ $id('folderChips').style.display = 'none'; return; }
    if(folderCache[aid]){ folderList = folderCache[aid]; renderFolderChips(); return; }
    var list;
    try{
      var out = await Fastt.get('/api/of/v2/chats/folders', {limit: 50}, {acct: aid});
      // drop the internal mass-exclude system lists — they're pinned-to-chat but
      // are plumbing, not operator-facing fan folders.
      list = (out.list || []).filter(function(f){
        return f.type === 'custom' && f.name !== 'MASSppvEXCLUDE' && f.name !== 'MASSdmEXCLUDE';
      });
    }catch(e){ list = []; }
    if(String(aid) !== String(activeAid)) return;   // stale response — leave shared state untouched
    folderCache[aid] = list; folderList = list;
    renderFolderChips();
  }
  function renderFolderChips(){
    var box = $id('folderChips');
    if(inboxMode !== 'creator'){ box.style.display = 'none'; return; }
    var folderSvg = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>';
    var pinned = folderList.filter(function(f){ return f.isPinnedToChat; });
    var html = pinned.map(function(f){
      return '<div class="fchip' + (String(folderId) === String(f.id) ? ' active' : '') + '" data-folder="' + esc(f.id) + '">' +
        folderSvg + esc(f.name || 'Folder') + '</div>';
    }).join('');
    if(folderList.length) html += '<div class="fchip edit" data-folder-edit="1" title="Pin/unpin folders as chips">✎ Lists</div>';
    box.innerHTML = html;
    box.style.display = html ? 'flex' : 'none';
  }
  $id('folderChips').addEventListener('click', function(e){
    if(e.target.closest('[data-folder-edit]')){ openFolderPicker(e.target.closest('[data-folder-edit]')); return; }
    var chip = e.target.closest('[data-folder]'); if(!chip) return;
    var id = chip.getAttribute('data-folder');
    folderId = (String(folderId) === String(id)) ? null : id;   // re-click clears
    filter = null; oweOnly = false;
    setPrimaryChip(folderId ? '__none' : 'all');
    renderFolderChips();
    reloadList();
  });
  function openFolderPicker(anchor){
    var html = '<div class="phead">Show folders as chips</div>' +
      (folderList.length ? folderList.map(function(f){
        return '<div class="prow' + (f.isPinnedToChat ? ' on' : '') + '" data-pin="' + esc(f.id) + '">' +
          '<span class="ptxt">' + esc(f.name || 'Folder') + '</span>' +
          '<span class="pmeta">' + (f.isPinnedToChat ? 'pinned' : 'pin') + '</span></div>';
      }).join('') : '<div class="prow dim">no custom lists on this creator</div>');
    popover(anchor, html, async function(row){
      var pid = row.getAttribute('data-pin'); if(!pid) return;
      var f = folderList.filter(function(x){ return String(x.id) === String(pid); })[0]; if(!f) return;
      var was = !!f.isPinnedToChat;
      closePops();
      try{
        await apatch('/api/of/v2/lists/' + pid + '/pin-chat', {pinned: !was}, activeAid);
        f.isPinnedToChat = !was;
        if(!f.isPinnedToChat && String(folderId) === String(pid)){ folderId = null; reloadList(); }
        renderFolderChips();
        Fastt.saved(was ? 'Folder unpinned' : 'Folder pinned as a chip');
      }catch(e){ Fastt.oops(e); }
    });
  }

  document.querySelector('.btn-online').addEventListener('click', function(){
    onlineOnly = !onlineOnly;
    this.style.outline = onlineOnly ? '2px solid #fff' : '';
    renderRows();
  });

  // list settings gear — the view options that actually exist on this list
  document.querySelector('.filter-row .gear').addEventListener('click', function(e){
    e.stopPropagation();
    popover(this,
      '<div class="phead">Sort</div>' +
      SORTS.map(function(s, i){
        return '<div class="prow' + (i === sortMode ? ' on' : '') + '" data-sort="' + i + '">' + s + '</div>';
      }).join('') +
      '<div class="pdiv"></div><div class="phead">Show only</div>' +
      '<div class="prow' + (onlineOnly ? ' on' : '') + '" data-t="online">Fans online now</div>' +
      '<div class="prow' + (withTips ? ' on' : '') + '" data-t="tips">Chats with unread tips</div>' +
      '<div class="pdiv"></div><div class="phead">Actions</div>' +
      '<div class="prow" data-t="markall">✓ Mark all as read</div>' +
      '<div class="prow" data-t="reload">↻ Reload this inbox</div>',
      function(row){
        var s = row.getAttribute('data-sort');
        if(s !== null && s !== undefined){
          sortMode = Number(s); sortTxt.textContent = SORTS[sortMode]; renderRows(); closePops(); return;
        }
        var t = row.getAttribute('data-t');
        if(t === 'online'){ onlineOnly = !onlineOnly;
          document.querySelector('.btn-online').style.outline = onlineOnly ? '2px solid #fff' : ''; renderRows(); }
        else if(t === 'tips'){ withTips = !withTips;
          (document.getElementById('chipTips') || {classList:{toggle:function(){}}}).classList.toggle('active', withTips); renderRows(); }
        else if(t === 'markall'){ markAllRead(); }
        else if(t === 'reload'){ reloadList(true); }
        closePops();
      });
  });

  // Mark every unread chat in the current scope read (sequential — OF rate-limits
  // parallel mark-reads). Optimistic clear + tab-badge refresh.
  async function markAllRead(){
    var unread = chats.filter(function(c){ return c.unread; });
    if(!unread.length){ Fastt.toast('No unread chats in this view'); return; }
    if(!confirm('Mark all ' + unread.length + ' unread chat' + (unread.length===1?'':'s') + ' as read on OnlyFans?')) return;
    Fastt.toast('Marking ' + unread.length + ' read…');
    var done = 0;
    for(var i = 0; i < unread.length; i++){
      var c = unread[i];
      try{ await apost('/api/of/v2/chats/' + c.fanId + '/mark-as-read', {}, c.aid); done++; }catch(e){}
      c.unread = false;
      var cr = byAid[String(c.aid)]; if(cr && cr.unread > 0) cr.unread--;
    }
    renderRows(); updateChipCounts(); renderTabs();
    Fastt.saved('Marked ' + done + ' read');
  }

  // composer gear — the composer's own view options
  $id('cbGear').addEventListener('click', function(e){
    e.stopPropagation();
    var on = $id('compX').classList.contains('on');
    popover(this,
      '<div class="phead">Composer</div>' +
      '<div class="prow' + (on ? ' on' : '') + '" data-t="ppv">' + (on ? 'Hide' : 'Show') + ' PPV / attachment row</div>' +
      '<div class="prow" data-t="clear">Clear draft + attachments</div>' +
      '<div class="prow" data-t="sched">' + (scheduleOn ? 'Send now (leave schedule mode)' : 'Schedule this message') + '</div>',
      function(row){
        var t = row.getAttribute('data-t');
        if(t === 'ppv') $id('compX').classList.toggle('on');
        else if(t === 'clear'){ input.value = ''; clearAttachments(); sendBtn.classList.remove('active'); }
        else if(t === 'sched'){ scheduleOn = !scheduleOn; renderAttachments(); }
        closePops();
      });
  });

  var sortEl = document.querySelector('.filter-row .sortline');
  var sortTxt = sortEl.querySelector('.txt');
  sortTxt.textContent = SORTS[sortMode];
  sortEl.style.cursor = 'pointer';
  sortEl.setAttribute('title', 'Click to cycle: needs-reply (unread → owe → spender) → newest → unread first → top spender → hybrid (spend × idle-decay) — client-side, over the loaded rows');
  sortEl.addEventListener('click', function(){
    sortMode = (sortMode + 1) % SORTS.length;
    sortTxt.textContent = SORTS[sortMode];
    renderRows();
  });

  var headBtns = document.querySelectorAll('.list-head .icon-btn');
  headBtns[1].addEventListener('click', function(){
    var q = prompt('Search fans by name / username:', searchQ || '');
    if(q === null) return;
    searchQ = q.trim() || null;
    if(inboxMode === 'all'){
      renderRows();
      if(searchQ){
        var needle = searchQ.toLowerCase();
        var hit = chats.filter(function(c){
          return String(c.name||'').toLowerCase().indexOf(needle) >= 0 ||
                 String(c.username||'').toLowerCase().indexOf(needle) >= 0;
        });
        rowsEl.innerHTML = hit.length ? sortChats(hit).map(rowHtml).join('')
          : '<div style="padding:24px 16px;color:#8a8a8a;font-size:13px">No match in the aggregated inbox</div>';
      }
      return;
    }
    reloadList();
  });
  headBtns[2].addEventListener('click', function(){
    Fastt.toast('New chats start from Growth → the fan list; this inbox shows existing conversations');
  });
  headBtns[0].addEventListener('click', function(){
    if(currentFan) $id('tbGallery').click();
    else Fastt.toast('Open a conversation to browse its media');
  });

  // ── location edit (PATCH /admin/fans/{aid}/{fid}) ─────────
  document.querySelector('.loc-box').style.cursor = 'pointer';
  document.querySelector('.loc-box').addEventListener('click', async function(){
    if(!currentFan) return;
    var cur = (lastFan && [lastFan.home_city, lastFan.home_country].filter(Boolean).join(', ')) || '';
    var next = prompt('Location for this fan — "City, Country" (saved to fastt, not to OnlyFans):', cur);
    if(next === null) return;
    var parts = next.split(',');
    var city = (parts[0] || '').trim();
    var country = (parts.slice(1).join(',') || '').trim();
    try{
      await apatch('/admin/fans/' + currentAid + '/' + currentFan,
                   {home_city: city || null, home_country: country || null}, currentAid);
      if(lastFan){ lastFan.home_city = city; lastFan.home_country = country; }
      $id('profLoc').textContent = [city, country].filter(Boolean).join(', ') || 'Unknown';
      Fastt.saved('Location saved');
    }catch(e){ Fastt.oops(e); }
  });

  // ── nickname edit (OF native custom-name) ─────────────────
  document.querySelector('.nick-edit').addEventListener('click', async function(){
    if(!currentFan) return;
    var cur = $id('profNick').textContent;
    var next = prompt('Custom nickname for this fan (saved to OnlyFans; empty clears):',
                      (cur === '—' || cur === '…') ? '' : cur);
    if(next === null) return;
    try{
      await aput('/api/of/v2/subscriptions/' + currentFan + '/custom-name', {name: next.trim()}, currentAid);
      $id('profNick').textContent = next.trim() || '—';
      var k = key(currentAid, currentFan);
      if(fanInfo[k]) fanInfo[k].nickname = next.trim();
      Fastt.saved('Nickname saved');
    }catch(e){ Fastt.oops(e); }
  });

  // ── fan tags editor (VIP / whale markers) — PATCH /admin/fans ──
  $id('tagsEdit').addEventListener('click', async function(){
    if(!currentFan) return;
    var cur = $id('profTags').textContent;
    var next = prompt('Tags for this fan (comma-separated — e.g. VIP, whale, roleplay). Empty clears:',
                      (cur === '—' || cur === '…') ? '' : cur);
    if(next === null) return;
    var tags = next.split(',').map(function(t){ return t.trim(); }).filter(Boolean);
    try{
      await apatch('/admin/fans/' + currentAid + '/' + currentFan, {tags: tags}, currentAid);
      $id('profTags').textContent = tags.length ? tags.join(', ') : '—';
      Fastt.saved('Tags saved');
    }catch(e){ Fastt.oops(e); }
  });
  // ── per-fan language override (ISO 639-1) — steers the AI's language ──
  $id('langEdit').addEventListener('click', async function(){
    if(!currentFan) return;
    var cur = $id('profLang').textContent;
    var next = prompt('Language override for this fan (ISO 639-1: en, es, sl, de, fr, it, pt, nl, tr…). Empty = auto:',
                      (cur === '—' || cur === '…') ? '' : cur);
    if(next === null) return;
    var lang = next.trim().toLowerCase();
    try{
      await apatch('/admin/fans/' + currentAid + '/' + currentFan, {language: lang || null}, currentAid);
      $id('profLang').textContent = lang || '—';
      Fastt.saved(lang ? ('Language set to ' + lang) : 'Language override cleared');
    }catch(e){ Fastt.oops(e); }
  });

  // ═══════════════════════════════════════════════════════════
  // LIVE (SSE) — one /events stream covers every creator
  // ═══════════════════════════════════════════════════════════
  var typingTimer = null;
  var listRefresh = Fastt.debounce(function(){ reloadList(true); }, 8000);

  function bumpRow(aid, fanId, text, at, inbound){
    var found = null;
    for(var i = 0; i < chats.length; i++){
      if(String(chats[i].fanId) === String(fanId) && String(chats[i].aid) === String(aid)){ found = chats[i]; break; }
    }
    var openHere = String(currentFan) === String(fanId) && String(currentAid) === String(aid);
    if(found){
      found.text = text; found.at = at || new Date().toISOString();
      if(inbound && !openHere) found.unread = true;
      renderRows(); updateChipCounts();
    }else if(inboxMode === 'all' || String(aid) === String(activeAid)){
      listRefresh();
    }
    if(inbound && !openHere){
      var c = byAid[aid];
      if(c){ c.unread = (c.unread || 0) + 1; renderTabs(); }
    }
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
    var text = stripHtml(msg.text) || (msg.mediaCount ? msg.mediaCount + ' media' : '');
    if(String(currentFan) === String(fanId) && String(currentAid) === String(aid)){
      threadMsgs.push(msg);
      var empty = threadBody.querySelector('.msg-meta');
      if(empty) threadBody.innerHTML = '';
      threadBody.insertAdjacentHTML('beforeend', msgHtml(msg));
      threadBody._sig = threadSig(threadMsgs); cacheThread(currentFan, currentAid);
      threadBody.scrollTop = threadBody.scrollHeight;
      typingLine.classList.add('hidden');
    }
    bumpRow(String(aid), fanId, text, msg.createdAt, !outbound);
  }

  // The stream is opened AFTER the first paint settles (see boot) so the initial
  // burst of roster/chat/insight fetches isn't competing with a connection that
  // never closes.
  function startLive(){
  Fastt.sse(function(payload, name){
    if(!payload || typeof payload !== 'object') return;
    if(name === 'api2_chat_message' || name === 'new_message'){
      onChatMessage(payload);
    }else if(name === 'chat_messages'){
      // OF's unread-counter push: {chat_messages: <unread chat count>,
      // count_priority_chat, unread_tips} — NOT a message body.
      var c = byAid[String(payload.__account_id)];
      if(c && typeof payload.chat_messages === 'number'){
        c.unread = payload.chat_messages;
        renderTabs();
      }
      listRefresh();
    }else if(name === 'typing'){
      var t = payload.typing || {};
      if(String(payload.__account_id) === String(currentAid) && String(t.id) === String(currentFan)){
        typingLine.textContent = 'typing…';
        typingLine.classList.remove('hidden');
        clearTimeout(typingTimer);
        typingTimer = setTimeout(function(){ typingLine.classList.add('hidden'); }, 4000);
      }
    }else if(name === 'purchase_notified' || name === 'toasts' || name === 'transaction_recorded'){
      loadNotifCount();
      if(currentFan){
        loadInsights(currentFan, currentAid).catch(function(){});
        // a payment landed — if it's for the open fan, re-pull the thread so any
        // now-paid PPV bubbles flip from "not paid" → "paid".
        var pf = (payload.transaction_recorded && payload.transaction_recorded.fan_id) ||
                 payload.__fan_id || (payload.user && payload.user.id) || (payload.fan && payload.fan.id);
        var pa = payload.__account_id;
        if(String(pf) === String(currentFan) && (!pa || String(pa) === String(currentAid))){
          loadThread(currentFan, currentAid, true);
        }
      }
    }
  }, ['transaction_recorded']);
  }

  // ═══════════════════════════════════════════════════════════
  // BADGES — what is live, what is honestly inert
  // ═══════════════════════════════════════════════════════════
  Fastt.liveBadge(document.querySelector('.list-head'));
  Fastt.liveBadge($id('addCreator'));
  Fastt.liveBadge($id('inboxBox'));
  var cardHeads = document.querySelectorAll('#paneInsights .card .card-head');
  Fastt.liveBadge(cardHeads[0]);
  Fastt.staticBadge(cardHeads[1], 'NO BACKEND');   // Organization metrics — no classifier exists
  Fastt.liveBadge(cardHeads[2]);
  Fastt.liveBadge(cardHeads[3]);
  Fastt.liveBadge(document.querySelector('.btn-notif'));
  Fastt.liveBadge(document.querySelector('.comp-r2'));
  Fastt.liveBadge(document.querySelector('#threadHead .th-right'));
  Fastt.liveBadge(document.querySelector('.comp-r1'));

  var STATIC_TIP = 'Static demo — no backend behind this control';
  function markStatic(el, tip){ if(el) el.setAttribute('title', tip || STATIC_TIP); }
  function markAll(sel, tip){ Array.prototype.forEach.call(document.querySelectorAll(sel), function(el){ markStatic(el, tip); }); }
  markStatic($id('cbAa'), 'Text formatting — OnlyFans strips markup from DM text, so there is nothing to save');
  // .sfw is wired live by fastt.js mountSfwToggle() — do NOT mark it static.
  markAll('.topstrip .top-right .tr-box', 'Screen-record/translate chrome — no backend');
  markAll('.topstrip .top-right .icon-btn', 'Window chrome — no backend');
  // (no container-level STATIC badge: the SFW toggle in here is now live)
  Array.prototype.forEach.call(document.querySelectorAll('#paneInsights .card .mrow'), function(r){
    var k = r.querySelector('.k');
    if(k && /Birthday/i.test(k.textContent)){
      var v = r.querySelector('.v');
      markStatic(v, 'Birthday — the fan record has no birthday field (FanUpdateBody has none), so there is nothing to save');
      Fastt.staticBadge(v, 'NO FIELD');
    }
  });
  markStatic($id('insPower'), 'No spending-power classifier exists on this relay — this is a placeholder label, not a score');
  markStatic($id('insCharge'), 'No chargeback-risk signal exists on this relay — this is a placeholder label, not a score');

  // ═══════════════════════════════════════════════════════════
  // BOOT
  // ═══════════════════════════════════════════════════════════
  // give the list header an id we can repaint on creator switch
  document.querySelector('.list-head h1').id = 'listTitle';

  await buildRoster();
  if(!CREATORS.length){
    ctabsEl.innerHTML = '<span class="ctabs-empty">No creators on this relay</span>';
    setRowsHonest(honest('No creators available',
      'Neither /admin/accounts (empty for an unauthed caller by design) nor /admin/stats/per-model returned a roster.',
      'sign in, or capture a session with POST /admin/session/bootstrap'));
    return;
  }
  // Deep-link: MoneyRail rows, the bell dropdown, and the "Open in new tab"
  // pop-out all navigate here as messages.html?account=<aid>&fan=<id>[&popout=1].
  var DL = new URLSearchParams(location.search);
  var dlAccount = DL.get('account'), dlFan = DL.get('fan'), dlPopout = DL.get('popout') === '1';
  var dlScope = DL.get('scope');
  if(dlPopout) document.body.classList.add('ft-popout');

  var want;
  if(dlAccount && byAid[dlAccount]){
    // honor the linked creator even if its tab was previously closed
    var closed = closedSet();
    if(closed.indexOf(String(dlAccount)) >= 0){
      lsSet(LS_CLOSED, closed.filter(function(id){ return id !== String(dlAccount); }));
    }
    want = String(dlAccount);
    inboxMode = 'creator';   // a deep-link / pop-out is scoped to one creator (do not persist)
  } else {
    want = lsGet(LS_ACTIVE, null) || Fastt.account();
    var opens = openTabs();
    if(!byAid[want] || opens.indexOf(String(want)) < 0){
      var live = opens.filter(function(id){ return byAid[id] && byAid[id].hasSession; });
      want = live[0] || opens[0] || CREATORS[0].id;
    }
  }
  // Top-nav "All Models" deep-link (messages.html?scope=all) — force the unified
  // cross-creator list. inboxMode already defaults to 'all', but a stale creator
  // scope / filter / folder from a prior session would otherwise stick, so reset
  // to a clean combined inbox every time the tab is clicked.
  if(dlScope === 'all'){
    inboxMode = 'all'; lsSet(LS_INBOX, inboxMode);
    folderId = null; filter = null; oweOnly = false; setPrimaryChip('all');
  }
  activeAid = String(want);
  lsSet(LS_ACTIVE, activeAid);
  // The unread tallies and the first list paint share this ONE local read —
  // loadUnreadCounts returns the rows it fetched, seedListFromRecent below
  // turns them into a pre-warmed (stale) CHATS_CACHE entry. Awaiting is fine:
  // it is an indexed local read, ~1ms measured.
  var recentRows = await loadUnreadCounts();
  renderTabs();
  paintScopeChrome();
  clearSelection();
  setRightTab('insights');
  var _savedTabs = lsGet(LS_TABS, []);
  OPEN_TABS = (Array.isArray(_savedTabs) ? _savedTabs : []).filter(function(t){ return t && t.aid && t.fanId != null; });
  renderTabStrip();   // restore pinned chat tabs from a prior session
  loadNotifCount();
  seedListFromRecent(recentRows);
  // Open the thread as soon as there IS a target rather than after the list
  // settles: a deep-link names its fan outright, and the seed above already
  // supplied a first row. Awaiting the live /api/of/v2/chats here was the last
  // thing holding the first message bubble behind a second OF round-trip.
  // Neither call marks anything read — that needs an explicit user click
  // (selectFan's `userOpen`), which no boot path passes.
  var listLoaded = reloadList();
  if(dlFan){ selectFan(String(dlFan), activeAid); }
  else if(chats.length) selectFan(chats[0].fanId, chats[0].aid);
  await listLoaded;
  // Nothing local to select from (a cold mirror) → fall back to the OF list.
  if(currentFan == null && chats.length) selectFan(chats[0].fanId, chats[0].aid);
  setInterval(function(){ loadUnreadCounts().then(renderTabs); }, 60000);
  // The 3s this used to wait was sized for a boot that blocked on OF; the
  // seed now paints in well under a second, which turned the delay into a
  // window where the pane is live-looking but deaf to new messages. The
  // normal app opens its EventSource immediately; one frame is enough here
  // to let the first paint finish before the stream starts patching it.
  setTimeout(startLive, 250);
});
