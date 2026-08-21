/* ==== LIVE WIRING — notifications ============================================
 * Three real feeds, newest first, each with its own honest empty state:
 *
 *  1. OnlyFans notifications — GET /api/of/v2/users/notifications?limit&offset[&type]
 *     Returns a BARE ARRAY (not {list:[]}) of
 *     {id,type,subType,createdAt,isRead,text,replacePairs,canGoToProfile,user{…}}.
 *     `createdAt` is tz-AWARE (+00:00) so Fastt.parseUtc leaves it alone.
 *     `text` is a token template — replacePairs must be substituted BEFORE the
 *     html is stripped, exactly like app/components/NotificationBell.tsx.
 *     Tab counts: GET …/notifications/count. Tab ORDER: GET
 *     …/notifications/settings/tabs-order. Filters are SINGULAR
 *     (?type=subscribed, not "subscriptions"); ?type=tags 400s upstream, so
 *     the chip set below matches the production bell exactly (8 chips, no tags).
 *  2. Live activity — the relay /events SSE channel, ALLOW-LISTED. The raw
 *     channel is mostly plumbing (typing ×995, count_priority_chat, unread_tips,
 *     v, syncInProcess …) which would bury the real rows; only user-meaningful
 *     types render. `hasSystemNotifications` is a signal, not a row: it triggers
 *     a refetch of feed 1.
 *  3. Relay & automation errors — GET /admin/errors (7-day window).
 * ============================================================================ */
Fastt.ready(async function () {
  var $ = Fastt.$, esc = Fastt.esc;
  var acct = String(Fastt.account() || '');

  // ── honest chrome ───────────────────────────────────────────────
  var tz = $('#tz-top');
  if (tz) tz.textContent = 'UTC+00:00';   // every relay stamp is UTC

  // ── topbar health pill (ported from dashboard.html — the green dot
  //    was hardcoded decoration on this page) ──────────────────────
  async function loadOps() {
    var dot = $('#ops-dot'), txt = $('#ops-status');
    if (!dot || !txt) return;
    function fail(msg) { dot.style.background = '#ff5f57'; txt.textContent = msg; }
    var rev;
    try { rev = await Fastt.get('/admin/rev/live'); }
    catch (e) { fail('Relay unreachable'); throw e; }
    if (rev && rev.error) { fail('Relay error'); return; }
    var health;
    try { health = await Fastt.get('/admin/ingest/transactions/health'); }
    catch (e) { fail('Health check failed'); throw e; }
    var row = (health.accounts || []).find(function (a) { return String(a.account_id) === acct; });
    if (!row) { txt.textContent = 'No ingest data'; dot.style.background = '#febc2e'; return; }
    if (row.tier === 'green') { txt.textContent = 'Operational'; dot.style.background = '#3ec46a'; }
    else if (row.tier === 'yellow') { txt.textContent = 'Ingest lagging'; dot.style.background = '#febc2e'; }
    else { txt.textContent = 'Ingest stale'; dot.style.background = '#ff5f57'; }
  }
  Fastt.liveBadge($('#ops-pill'));
  try { await loadOps(); } catch (e) { Fastt.oops(e); }

  Fastt.liveBadge($('#nt-title'));

  // ── type chips (production bell's 8, ordered by OF's own tabs-order) ──
  var FILTERS = [
    { id: 'all',        label: 'All',           type: null,        count: 'all' },
    { id: 'subscribed', label: 'Subscriptions', type: 'subscribed', count: 'subscribed' },
    { id: 'purchases',  label: 'Purchases',     type: 'purchases',  count: 'purchases' },
    { id: 'tip',        label: 'Tips',          type: 'tip',        count: 'tip' },
    { id: 'commented',  label: 'Comments',      type: 'commented',  count: 'commented' },
    { id: 'mentioned',  label: 'Mentions',      type: 'mentioned',  count: 'mentioned' },
    { id: 'favorited',  label: 'Likes',         type: 'favorited',  count: 'favorited' },
    { id: 'message',    label: 'Messages',      type: 'message',    count: 'message' },
  ];
  // Restore the last-used chip — shared with the real app + the popout bell
  // through the SAME localStorage key, so a chatter who lives on Tips/Purchases
  // lands back there after a reload.
  var FILTER_KEY = 'chatterly:notif-filter:v1';
  var savedFilterId = null;
  try { savedFilterId = localStorage.getItem(FILTER_KEY); } catch (e) {}
  var active = FILTERS.find(function (f) { return f.id === savedFilterId; }) || FILTERS[0];
  var counts = {};
  var lastFeed = [];

  // ── local read-state watermark (per creator) ────────────────────
  // OF exposes no mark-read WRITE route through the relay, so read-state here
  // is a client-side high-water mark of notification ids the operator has
  // dismissed in THIS view. A row is unread when OF still flags it unread AND
  // its id sits above the mark. This is exactly the real app's philosophy
  // (its badge is a tab-local counter, not an OF mutation).
  var WM_KEY = 'ft_notif_read_wm_' + acct;
  var readWatermark = 0;
  try { readWatermark = Number(localStorage.getItem(WM_KEY)) || 0; } catch (e) {}
  function isUnread(n) {
    if (n && n.isRead) return false;
    var id = Number(n && n.id);
    return !(id && id <= readWatermark);
  }

  // ── OF→internal type map (ported from app/lib/notifSettings.ts) ──
  function mapKey(raw) {
    if (!raw) return null;
    var t = String(raw).toLowerCase();
    if (t === 'message' || t === 'messages' || t === 'chatmessage') return 'message';
    if (t === 'tip' || t === 'tips') return 'tip';
    if (t === 'purchase' || t === 'purchases' || t === 'paided_message' || t === 'paided_post') return 'purchases';
    if (t === 'subscribed' || t === 'subscriber' || t === 'subscription') return 'subscribed';
    if (t === 'commented' || t === 'comment') return 'commented';
    if (t === 'mentioned' || t === 'mention') return 'mentioned';
    if (t === 'favorited' || t === 'favorite' || t === 'like' || t === 'liked') return 'favorited';
    return null;
  }
  // The ecosystem's SSE mirror. The relay's own toaster/overlay (and the real
  // Next app) write captured arrivals — including the types OF's own feed
  // silently drops (likes especially: favorited count can read 51 while the
  // list endpoint returns []) — here. Reading it lets those types surface on
  // the Likes/Mentions/Comments tabs instead of a permanent empty box.
  function historyFor(type) {
    var map = null;
    try { map = JSON.parse(localStorage.getItem('chatterly:notif-history:v1')); } catch (e) { map = null; }
    if (!map || typeof map !== 'object') return [];
    var list = map[acct];
    if (!Array.isArray(list)) return [];
    var cutoff = Date.now() - 7 * 24 * 3600 * 1000;
    return list.filter(function (c) {
      if (!c || c.id == null) return false;
      var ts = Date.parse(c.createdAt);
      if (!(ts >= cutoff)) return false;
      if (!type) return true;
      return (c.typeKey || mapKey(c.type)) === type;
    }).map(function (c) {
      return { id: c.id, type: c.type, createdAt: c.createdAt, isRead: false,
        text: c.text, replacePairs: c.replacePairs, user: c.user || {}, __mirror: true };
    });
  }

  async function loadTabs() {
    var order = null;
    try { order = await Fastt.get('/api/of/v2/users/notifications/settings/tabs-order'); }
    catch (e) { order = null; }
    if (Array.isArray(order) && order.length) {
      var rank = {};
      order.forEach(function (t, i) { rank[t] = i; });
      FILTERS.sort(function (a, b) {
        var ra = rank[a.id] == null ? 99 : rank[a.id];
        var rb = rank[b.id] == null ? 99 : rank[b.id];
        return ra - rb;
      });
    }
    try { counts = await Fastt.get('/api/of/v2/users/notifications/count') || {}; }
    catch (e) { counts = {}; }
    renderTabs();
    // The topbar bell is a real unread counter, not a permanent red dot.
    var bell = $('#bell-count');
    var unread = Number(counts.all) || 0;
    if (bell) {
      bell.textContent = unread > 99 ? '99+' : String(unread);
      bell.style.display = unread ? '' : 'none';
    }
  }
  function renderTabs() {
    var wrap = $('#nt-tabs');
    if (!wrap) return;
    wrap.innerHTML = FILTERS.map(function (f) {
      var n = counts[f.count];
      var has = typeof n === 'number';
      // The /count payload is an UNREAD counter, not a total: OF happily
      // returns 20 read `purchases` rows while count.purchases is 2.
      return '<button type="button" class="ntab' + (f.id === active.id ? ' on' : '') + '" data-id="' + esc(f.id) + '"'
        + (has ? ' title="' + esc(String(n)) + ' unread"' : '') + '>'
        + esc(f.label)
        + (has ? '<span class="n' + (n ? '' : ' zero') + '">' + esc(String(n)) + '</span>' : '')
        + '</button>';
    }).join('');
    wrap.querySelectorAll('.ntab').forEach(function (b) {
      b.addEventListener('click', function () {
        var f = FILTERS.find(function (x) { return x.id === b.dataset.id; });
        if (!f || f.id === active.id) return;
        active = f;
        try { localStorage.setItem(FILTER_KEY, f.id); } catch (e) {}
        renderTabs();
        loadOfFeed().catch(Fastt.oops);
      });
    });
  }

  // ── OF feed ─────────────────────────────────────────────────────
  function renderText(raw, pairs) {
    var s = String(raw || '');
    if (pairs) Object.keys(pairs).forEach(function (k) { s = s.split(k).join(pairs[k]); });
    return s.replace(/<[^>]+>/g, '').replace(/\s+/g, ' ').trim();
  }
  function avatarUrl(u) {
    var raw = (u && u.avatar) || (u && u.avatarThumbs && (u.avatarThumbs.c50 || u.avatarThumbs.c144)) || '';
    if (!raw || !acct) return '';
    // relay image proxy (app/lib/relay.ts proxyImage) — keeps every asset
    // same-origin instead of hot-linking OF's signed CDN urls.
    return '/img?u=' + encodeURIComponent(raw) + '&account_id=' + encodeURIComponent(acct);
  }
  var TYPE_LABEL = {
    paided_message: 'purchase', paided_post: 'purchase', subscribed: 'subscription',
    tip: 'tip', message: 'message', commented: 'comment', mentioned: 'mention',
    favorited: 'like', stream: 'stream',
  };
  function ofRow(n) {
    var u = n.user || n.fromUser || {};
    var av = avatarUrl(u);
    var text = renderText(n.text || n.description || '', n.replacePairs);
    var amount = (n.replacePairs && n.replacePairs['{AMOUNT}']) || '';
    var name = u.name || u.username || '';
    var unread = isUnread(n);
    var isTip = mapKey(n.type) === 'tip';
    // OF's own deep link for the thread — the notification body already
    // carries this url; user.id is the fan's OF id.
    var href = u.id ? 'https://onlyfans.com/my/chats/chat/' + encodeURIComponent(u.id) : '';
    var inner =
      '<span class="av">' + (av
        ? '<img src="' + esc(av) + '" alt="" loading="lazy" decoding="async">'
        : esc((name || '?').slice(0, 1).toUpperCase())) + '</span>' +
      '<span class="bd">' +
        '<span class="t1">' + (name ? '<b>' + esc(name) + '</b> ' : '') + esc(text) + '</span>' +
        '<span class="t2">' +
          '<span>' + esc(Fastt.fmtAgo(n.createdAt)) + '</span>' +
          '<span class="tag">' + esc(TYPE_LABEL[n.type] || n.type || 'event') + '</span>' +
          (n.subType ? '<span class="tag">' + esc(n.subType) + '</span>' : '') +
          (n.__mirror ? '<span class="tag from" title="Captured live by the notification mirror — OnlyFans’ own feed drops this type">mirror</span>' : '') +
          (amount ? '<span class="amt' + (isTip ? ' tip' : '') + '">' + esc(amount) + '</span>' : '') +
          (unread ? '<span class="tag" style="color:#8fa6ff;border-color:#33406e">new</span>' : '') +
        '</span>' +
      '</span>';
    var cls = 'nrow' + (unread ? ' unread' : '');
    if (href) {
      return '<a class="' + cls + '" href="' + esc(href) + '" target="_blank" rel="noreferrer" '
        + 'title="Open ' + esc(name || 'this fan') + '’s thread on OnlyFans">' + inner + '</a>';
    }
    return '<div class="' + cls + '">' + inner + '</div>';
  }
  function emptyBox(msg) {
    return '<div class="nt-empty">'
      + '<svg width="26" height="28" viewBox="0 0 86 94" fill="none"><path d="M22 26h30l13 13v39a4 4 0 0 1-4 4H22a4 4 0 0 1-4-4V30a4 4 0 0 1 4-4z" fill="#3a3a3a"/><path d="M52 26v9a4 4 0 0 0 4 4h9z" fill="#4d4d4d"/></svg>'
      + '<span>' + esc(msg) + '</span></div>';
  }
  async function loadOfFeed() {
    var list = $('#of-list'), sub = $('#of-sub'), note = $('#of-note');
    if (!list) return;
    if (!acct) {
      list.innerHTML = emptyBox('No creator selected — pick one to load their OnlyFans notifications.');
      return;
    }
    var params = { limit: 20, offset: 0 };
    if (active.type) params.type = active.type;
    var raw;
    try {
      raw = await Fastt.get('/api/of/v2/users/notifications', params);
    } catch (e) {
      list.innerHTML = emptyBox('OnlyFans returned an error for the "' + active.label + '" tab (relay ' +
        (e && e.status ? e.status : 'error') + ').');
      if (sub) sub.textContent = '';
      return;
    }
    var ofItems = Array.isArray(raw) ? raw : (raw && Array.isArray(raw.list) ? raw.list : []);
    // Merge the SSE mirror so OF-dropped types (likes especially) surface.
    // Dedup on id — a mirrored arrival and OF's eventual feed row collapse
    // into one, exactly like the real app's history merge.
    var seen = {};
    ofItems.forEach(function (n) { if (n && n.id != null) seen[String(n.id)] = 1; });
    var mirror = historyFor(active.type).filter(function (m) { return !seen[String(m.id)]; });
    var items = ofItems.concat(mirror).sort(function (a, b) {
      return (Fastt.parseUtc(b.createdAt) || 0) - (Fastt.parseUtc(a.createdAt) || 0);
    });
    lastFeed = items;
    var unreadN = items.filter(isUnread).length;
    var mr = $('#nt-markread'), mrLbl = $('#nt-markread-lbl');
    if (mr) mr.disabled = !unreadN;
    if (mrLbl) mrLbl.textContent = unreadN ? 'Mark all read (' + unreadN + ')' : 'All read';
    if (sub) sub.textContent = active.label + ' · ' + items.length + (ofItems.length === 20 ? '+ shown' : ' shown')
      + (mirror.length ? ' · ' + mirror.length + ' from mirror' : '')
      + (unreadN ? ' · ' + unreadN + ' unread' : '');
    list.innerHTML = items.length
      ? items.map(ofRow).join('')
      : emptyBox(active.id === 'favorited' || active.id === 'mentioned' || active.id === 'commented'
        ? 'No "' + active.label + '" yet. OnlyFans drops these from its own feed, so they only appear once the live mirror captures one on this creator.'
        : 'No "' + active.label + '" notifications on this creator’s OnlyFans account.');
    if (note) {
      note.innerHTML = 'GET <code>/api/of/v2/users/notifications</code> (limit 20)'
        + (active.type ? ' with <code>type=' + esc(active.type) + '</code>' : '')
        + ' — OF’s own feed for this creator. Timestamps are tz-aware UTC; rows link out to the fan’s OnlyFans thread. '
        + 'The number on each chip is the <b>unread</b> count from <code>…/notifications/count</code> (not the row total, '
        + 'which is why a chip can read 0 while the tab lists read history); chip order comes from '
        + '<code>…/settings/tabs-order</code>. OF rejects <code>type=tags</code> with a 400, so — like the production '
        + 'bell — there is no Tags chip.';
    }
  }

  // ── relay errors ────────────────────────────────────────────────
  async function loadErrors() {
    var list = $('#err-list'), sub = $('#err-sub'), note = $('#err-note');
    if (!list) return;
    var out = await Fastt.get('/admin/errors', { since_hours: 168, limit: 100 });
    var rows = out.list || [];
    if (sub) sub.textContent = 'last 7 days · ' + rows.length + ' stored';
    list.innerHTML = rows.length ? rows.map(function (r) {
      return '<div class="nrow err">'
        + '<span class="av" style="color:#e05b5b">!</span>'
        + '<span class="bd">'
          + '<span class="t1"><b>' + esc(r.kind || 'error') + '</b> ' + esc(r.source || 'server') + '</span>'
          + '<span class="msg">' + esc(String(r.message || '(no message)').slice(0, 600)) + '</span>'
          + '<span class="t2"><span>' + esc(Fastt.fmtAgo(r.occurred_at)) + '</span>'
          + (r.account_id ? '<span class="tag">account ' + esc(r.account_id) + '</span>' : '') + '</span>'
        + '</span></div>';
    }).join('') : emptyBox('No relay errors stored in the last 7 days.');
    if (note) {
      note.innerHTML = 'GET <code>/admin/errors?since_hours=168</code> — unhandled server exceptions the relay '
        + 'persisted. This log is <b>relay-wide</b>, not per-creator.';
    }
  }

  // ── live SSE activity (allow-listed) ────────────────────────────
  // The raw /events channel is dominated by plumbing frames — /admin/events/stats
  // by_type shows typing 995, count_priority_chat 329, unread_tips 329, v 5,
  // syncInProcess 5 … Rendering "every key we saw" produced content-free rows
  // that buried the real ones, so this is an explicit allow-list.
  var LIVE_TYPES = {
    toasts: 'purchase / tip toast',
    purchase_notified: 'purchase',
    new_message: 'new message',
    api2_chat_message: 'chat message',
    chat_messages: 'chat message',
    subscribed: 'new subscriber',
    chat_queue_finish: 'mass send finished',
    chat_queue_update: 'mass send progress',
    stories: 'story',
    post_published: 'post published',
    stream_start: 'stream started',
    stream_stop: 'stream ended',
  };
  var liveItems = [];
  var LIVE_MAX = 40;
  function renderLive() {
    var list = $('#live-list'), sub = $('#live-sub'), note = $('#live-note');
    if (!list) return;
    if (sub) sub.textContent = liveItems.length ? liveItems.length + ' since this page opened' : 'streaming';
    list.innerHTML = liveItems.length ? liveItems.map(function (it) {
      return '<div class="nrow live">'
        + '<span class="av" style="color:#67d1ae">●</span>'
        + '<span class="bd">'
          + '<span class="t1"><b>' + esc(it.label) + '</b>' + (it.text ? ' ' + esc(it.text) : '') + '</span>'
          + '<span class="t2"><span>' + esc(it.when) + '</span><span class="tag">' + esc(it.type) + '</span>'
          + (it.scope ? '<span class="tag">' + esc(it.scope) + '</span>' : '') + '</span>'
        + '</span></div>';
    }).join('') : emptyBox('Connected to the relay event stream — nothing user-facing has come through yet.');
    if (note) {
      note.innerHTML = 'Relay <code>/events</code> SSE, filtered to user-meaningful types ('
        + esc(Object.keys(LIVE_TYPES).join(', ')) + '). Plumbing frames — <code>typing</code>, '
        + '<code>count_priority_chat</code>, <code>unread_tips</code>, <code>v</code>, <code>syncInProcess</code> — '
        + 'are dropped on purpose; <code>hasSystemNotifications</code> refetches the OnlyFans feed above '
        + 'instead of rendering as a row.';
    }
  }
  function pushLive(ev, type) {
    var inner = (ev && typeof ev === 'object' && ev[type] && typeof ev[type] === 'object') ? ev[type] : (ev || {});
    var text = inner.text || inner.message || (ev && (ev.text || ev.message)) || '';
    if (typeof text !== 'string') text = '';
    text = text.replace(/<[^>]*>/g, '').replace(/\s+/g, ' ').trim().slice(0, 220);
    liveItems.unshift({
      type: type,
      label: LIVE_TYPES[type] || type,
      text: text,
      scope: (ev && ev.__account_name) ? String(ev.__account_name)
        : ((ev && ev.__account_id) ? 'account ' + ev.__account_id : ''),
      when: new Date().toLocaleTimeString(),
    });
    if (liveItems.length > LIVE_MAX) liveItems.length = LIVE_MAX;
    renderLive();
  }

  // ── boot ────────────────────────────────────────────────────────
  await loadTabs();
  await loadOfFeed();
  try { await loadErrors(); } catch (e) { Fastt.oops(e); }
  renderLive();

  // hasSystemNotifications is OF telling us the feed changed — treat it as a
  // refresh signal (that is what it means), never as a notification row.
  var refetch = Fastt.debounce(function () {
    loadTabs().catch(function () {});
    loadOfFeed().catch(function () {});
  }, 1500);
  var es = Fastt.sse(function (ev, name) {
    if (name === 'ping' || name === 'connected') return;   // heartbeat, not a row
    if (name === 'hasSystemNotifications') { refetch(); return; }
    if (!LIVE_TYPES[name]) return;                          // plumbing — drop
    pushLive(ev, name);
  }, ['hasSystemNotifications', 'stream_start', 'stream_stop']);
  var liveBadgeEl = $('#nt-title').querySelector('.ft-live');
  es.addEventListener('ping', function () {
    if (liveBadgeEl) liveBadgeEl.title = 'stream alive — last ping ' + new Date().toLocaleTimeString();
  });

  // ── Mark all read (local view read-state) ───────────────────────
  $('#nt-markread').addEventListener('click', function () {
    var top = lastFeed.reduce(function (m, n) { var id = Number(n.id); return id > m ? id : m; }, readWatermark);
    readWatermark = top;
    try { localStorage.setItem(WM_KEY, String(top)); } catch (e) {}
    // Reset the ecosystem's tab-local badge (the real app's bell + the shared
    // overlay listen for this) — same event NotificationBell dispatches.
    try { window.dispatchEvent(new CustomEvent('chatterly:notif-cleared')); } catch (e) {}
    // Clear the topbar bell too — it means "unseen in this tab", which the
    // operator just acknowledged. A genuinely new system notification will
    // re-badge it via the hasSystemNotifications → loadTabs refetch.
    var bell = $('#bell-count');
    if (bell) { bell.textContent = ''; bell.style.display = 'none'; }
    loadOfFeed().catch(Fastt.oops);
    Fastt.toast('Marked read in this view', 'ok');
  });

  // ── Header gear → real, read-only OF delivery-channel read-out ──
  // GET /api/of/v2/users/settings/notifications/transports returns the
  // channels OnlyFans is delivering this creator's notifications through
  // (e.g. ["email","message","webpush","toast"]). There is NO write route
  // through the relay, so this is a faithful read-only reflection — badged
  // as such rather than a fake toggle set.
  var gear = $('#nt-gear');
  var CHAN_META = {
    email:   { label: 'Email',           sub: 'Sent to the account email address', ic: '✉' },
    message: { label: 'In-app message',  sub: 'OnlyFans inbox notification',        ic: '☰' },
    webpush: { label: 'Web push',        sub: 'Browser push notification',          ic: '◉' },
    toast:   { label: 'Toast pop',       sub: 'In-app toast on onlyfans.com',       ic: '◔' },
    push:    { label: 'Mobile push',     sub: 'OnlyFans mobile app push',           ic: '▣' },
    sms:     { label: 'SMS',             sub: 'Text message',                       ic: '✆' },
  };
  var gearPop = null, transportsCache = null;
  function closeGear() {
    if (gearPop) { gearPop.remove(); gearPop = null; document.removeEventListener('click', onGearOutside); }
  }
  function onGearOutside(e) {
    if (gearPop && !gearPop.contains(e.target) && e.target !== gear && !gear.contains(e.target)) closeGear();
  }
  async function openGear() {
    gearPop = document.createElement('div');
    gearPop.className = 'nt-pop';
    var r = gear.getBoundingClientRect();
    gearPop.style.top = (r.bottom + 8) + 'px';
    gearPop.style.right = Math.max(8, window.innerWidth - r.right) + 'px';
    gearPop.innerHTML = '<h4>Delivery channels</h4><div class="cap">Loading OnlyFans’ notification transports…</div>';
    document.body.appendChild(gearPop);
    setTimeout(function () { document.addEventListener('click', onGearOutside); }, 0);
    var arr = transportsCache;
    if (arr == null) {
      try { arr = await Fastt.get('/api/of/v2/users/settings/notifications/transports'); }
      catch (e) { arr = null; }
      transportsCache = arr;
    }
    if (!gearPop) return;
    if (!Array.isArray(arr)) {
      gearPop.innerHTML = '<h4>Delivery channels</h4>'
        + '<div class="cap">Couldn’t read OnlyFans’ notification transports for this creator '
        + '(no captured session, or the OF read failed).</div>';
      return;
    }
    var rows = arr.map(function (t) {
      var m = CHAN_META[t] || { label: t, sub: 'OnlyFans transport', ic: '•' };
      return '<div class="chan"><span class="ic">' + esc(m.ic) + '</span>'
        + '<span class="lbl"><b>' + esc(m.label) + '</b><span>' + esc(m.sub) + '</span></span>'
        + '<span class="on">ON</span></div>';
    }).join('');
    gearPop.innerHTML = '<h4>Delivery channels</h4>'
      + '<div class="cap">Channels OnlyFans is currently delivering this creator’s notifications through.</div>'
      + (rows || '<div class="cap">OnlyFans reports no active delivery channels.</div>')
      + '<div class="foot">Read-only — <code>GET …/settings/notifications/transports</code>. '
      + 'OnlyFans exposes no write route through the relay, so these can’t be toggled here; '
      + 'change them in OnlyFans’ own settings.</div>';
  }
  gear.style.cursor = 'pointer';
  gear.addEventListener('click', function (e) {
    e.stopPropagation();
    if (gearPop) { closeGear(); return; }
    openGear().catch(Fastt.oops);
  });
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape') closeGear(); });

  $('#nt-clear').addEventListener('click', async function () {
    // DELETE /admin/errors has NO account filter (server.py admin_errors_clear) —
    // it wipes stored errors for every creator in this relay. Say so, and scope the
    // delete to the same 7-day window the list above reads so nothing outside what
    // the operator can actually see is destroyed. OnlyFans notifications are NOT
    // touched: this button never writes to OF.
    if (!confirm('Dismiss stored relay error notifications from the last 7 days?\n\n' +
                 'This is relay-wide: stored errors are not per-creator, so every ' +
                 'creator’s rows in that window are deleted. Your OnlyFans ' +
                 'notifications are not affected.')) return;
    try {
      var out = await Fastt.del('/admin/errors', { params: { since_hours: 168 } });
      Fastt.toast('Cleared ' + (out.deleted || 0) + ' stored error' + (out.deleted === 1 ? '' : 's'), 'ok');
      await loadErrors();
    } catch (e) { Fastt.oops(e); }
  });
});
