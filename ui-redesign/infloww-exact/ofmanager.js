Fastt.ready(async function () {
  'use strict';
  /* OF Manager — the relay mirrors this creator's OWN OnlyFans account across the
     /api/of/v2/* proxy (header X-Account-Id). Twelve sidebar sub-items all point at
     ofmanager.html, so this page routes them as in-page hash tabs, each lazily
     fetching only what it shows. READ-ONLY: nothing here sends, posts or queues. */
  var $ = Fastt.$, esc = Fastt.esc, fmtInt = Fastt.fmtInt, fmtMoney = Fastt.fmtMoney;
  var acct = Fastt.account();
  var bodyEl = $('#ofm-body'), tabsEl = $('#ofm-tabs');

  var TABS = [
    ['home', 'Home'], ['post', 'New post'], ['notifications', 'Notifications'],
    ['messages', 'Messages Basic'], ['vault', 'Vault'], ['queue', 'Queue'],
    ['collections', 'Collections'], ['statements', 'Statements'],
    ['statistics', 'Statistics'], ['bank', 'Bank'], ['profile', 'My profile'],
    ['settings', 'OF settings']
  ];

  // ── tiny helpers ────────────────────────────────────────────────────
  function img(u) { return u ? '/img?u=' + encodeURIComponent(u) : ''; }
  function strip(s) {
    return String(s == null ? '' : s).replace(/<br\s*\/?>/gi, ' ').replace(/<[^>]*>/g, '').trim();
  }
  function dt(s) { return Fastt.fmtDate(s); }
  function day(s) { var d = Fastt.parseUtc(s); return d ? d.toISOString().slice(0, 10) : '—'; }
  function ago(s) { return Fastt.fmtAgo(s); }
  function safe(p) { return p.then(function (v) { return { ok: true, v: v }; }, function (e) { return { ok: false, e: e }; }); }
  function errBox(what, r) {
    var d = r && r.e && r.e.body && (r.e.body.detail || r.e.body.error);
    return '<div class="ofm-empty ofm-err"><b>' + esc(what) + ' could not be read.</b><br>'
      + esc(typeof d === 'string' ? d : (r && r.e && r.e.message) || 'request failed') + '</div>';
  }
  function stat(k, v, d, cls) {
    return '<div class="ofm-stat' + (cls ? ' ' + cls : '') + '"><div class="k">' + esc(k) + '</div>'
      + '<div class="v">' + v + '</div>' + (d ? '<div class="d">' + d + '</div>' : '') + '</div>';
  }
  function delta(n) {
    if (typeof n !== 'number' || !isFinite(n)) return '';
    var s = (n > 0 ? '+' : '') + (Math.round(n * 10) / 10) + '%';
    return '<span class="' + (n > 0 ? 'up' : (n < 0 ? 'dn' : '')) + '">' + s + ' vs prev</span>';
  }
  function initials(n) { return String(n || '?').trim().slice(0, 1).toUpperCase() || '?'; }

  // ── charts: every pixel comes from the response ─────────────────────
  var CW = 640, CH = 168, PL = 40, PR = 10, PT = 12, PB = 22;
  function niceMax(v) {
    if (v <= 0) return 1;
    var mag = Math.pow(10, Math.floor(Math.log(v) / Math.LN10));
    var n = v / mag;
    var step = n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10;
    return step * mag;
  }
  function axes(max, labels, fmtY) {
    var out = '';
    [0, 0.5, 1].forEach(function (f) {
      var y = PT + (CH - PT - PB) * (1 - f);
      out += '<line class="gl" x1="' + PL + '" y1="' + y.toFixed(1) + '" x2="' + (CW - PR) + '" y2="' + y.toFixed(1) + '"/>'
        + '<text class="ax" x="' + (PL - 6) + '" y="' + (y + 3).toFixed(1) + '" text-anchor="end">'
        + esc(fmtY(max * f)) + '</text>';
    });
    labels.forEach(function (l) {
      out += '<text class="ax" x="' + l.x.toFixed(1) + '" y="' + (CH - 6) + '" text-anchor="' + (l.a || 'middle') + '">'
        + esc(l.t) + '</text>';
    });
    return out;
  }
  function xLabels(pts, keyFn) {
    if (!pts.length) return [];
    var n = pts.length;
    var idx = n <= 2 ? [0, n - 1] : [0, Math.floor((n - 1) / 2), n - 1];
    var seen = {};
    return idx.filter(function (i) { if (seen[i]) return false; seen[i] = 1; return true; }).map(function (i) {
      return { x: PL + (n === 1 ? 0 : i * (CW - PL - PR) / (n - 1)), t: keyFn(pts[i]),
               a: i === 0 ? 'start' : (i === n - 1 ? 'end' : 'middle') };
    });
  }
  /** points: [{label, value}] */
  function lineChart(points, fmtY) {
    if (!points.length) return '';
    fmtY = fmtY || function (v) { return String(Math.round(v)); };
    var max = niceMax(Math.max.apply(null, points.map(function (p) { return p.value; })));
    var n = points.length;
    var X = function (i) { return PL + (n === 1 ? (CW - PL - PR) / 2 : i * (CW - PL - PR) / (n - 1)); };
    var Y = function (v) { return PT + (CH - PT - PB) * (1 - v / max); };
    var line = points.map(function (p, i) { return X(i).toFixed(1) + ',' + Y(p.value).toFixed(1); }).join(' ');
    var area = 'M' + X(0).toFixed(1) + ',' + (CH - PB) + ' L' + line.split(' ').join(' L') + ' L' + X(n - 1).toFixed(1) + ',' + (CH - PB) + ' Z';
    var dots = points.length <= 40 ? points.map(function (p, i) {
      return '<circle class="dt" cx="' + X(i).toFixed(1) + '" cy="' + Y(p.value).toFixed(1) + '" r="2"/>';
    }).join('') : '';
    return '<svg class="ofm-chart" viewBox="0 0 ' + CW + ' ' + CH + '" role="img">'
      + axes(max, xLabels(points, function (p) { return p.label; }), fmtY)
      + '<path class="ar" d="' + area + '"/><polyline class="ln" points="' + line + '"/>' + dots + '</svg>';
  }
  function barChart(points, fmtY, green) {
    if (!points.length) return '';
    fmtY = fmtY || function (v) { return String(Math.round(v)); };
    var max = niceMax(Math.max.apply(null, points.map(function (p) { return p.value; })));
    var n = points.length;
    var slot = (CW - PL - PR) / n;
    var bw = Math.max(2, Math.min(26, slot * 0.66));
    var bars = points.map(function (p, i) {
      var h = (CH - PT - PB) * (p.value / max);
      var x = PL + i * slot + (slot - bw) / 2;
      return '<rect class="br' + (green ? ' g' : '') + '" x="' + x.toFixed(1) + '" y="' + (CH - PB - h).toFixed(1)
        + '" width="' + bw.toFixed(1) + '" height="' + Math.max(0, h).toFixed(1) + '" rx="1.5"/>';
    }).join('');
    return '<svg class="ofm-chart" viewBox="0 0 ' + CW + ' ' + CH + '" role="img">'
      + axes(max, xLabels(points, function (p) { return p.label; }), fmtY) + bars + '</svg>';
  }
  function chartOrEmpty(points, kind, fmtY, why, green) {
    if (!points || !points.length) return '<div class="ofm-empty">' + esc(why) + '</div>';
    var all0 = points.every(function (p) { return !p.value; });
    var svg = kind === 'bar' ? barChart(points, fmtY, green) : lineChart(points, fmtY);
    return svg + (all0 ? '<div class="ofm-chartnote">Every point in this window is zero — the series is real, the activity is not there.</div>' : '');
  }
  function mmdd(iso) { var s = String(iso || ''); return s.length >= 10 ? s.slice(5, 10) : s; }

  // ── tab plumbing ────────────────────────────────────────────────────
  var cache = {}, current = null;
  function paintTabs(counts) {
    tabsEl.innerHTML = TABS.map(function (t) {
      var c = counts && counts[t[0]];
      return '<button class="ofm-tab' + (t[0] === current ? ' on' : '') + '" type="button" data-tab="' + t[0] + '">'
        + esc(t[1]) + (c ? '<span class="cnt">' + esc(c) + '</span>' : '') + '</button>';
    }).join('');
  }
  var tabCounts = {};
  tabsEl.addEventListener('click', function (ev) {
    var b = ev.target.closest('[data-tab]'); if (!b) return;
    location.hash = '#' + b.dataset.tab;
  });
  // the shared sidebar's 12 OF Manager sub-items all point at this page with no
  // hash — rewrite them here so they land on the tab they name.
  (function () {
    var subs = document.querySelectorAll('#sub-ofm .sub-item');
    var order = TABS.map(function (t) { return t[0]; });
    subs.forEach(function (a, i) { if (order[i]) a.setAttribute('href', 'ofmanager.html#' + order[i]); });
  })();
  function markSidebar(id) {
    document.querySelectorAll('#sub-ofm .sub-item').forEach(function (a) {
      a.classList.toggle('active', a.getAttribute('href') === 'ofmanager.html#' + id);
    });
  }

  function tabName(id) {
    var t = TABS.filter(function (x) { return x[0] === id; })[0];
    return t ? t[1] : id;
  }
  async function show(id, force) {
    if (!TABS.some(function (t) { return t[0] === id; })) id = 'home';
    current = id;
    paintTabs(tabCounts);
    markSidebar(id);
    $('#ofm-title').textContent = tabName(id);
    Fastt.liveBadge($('#ofm-title'));
    if (force) delete cache[id];
    if (cache[id]) { bodyEl.innerHTML = cache[id]; afterPaint(id); return; }
    bodyEl.innerHTML = '<div class="ofm-load">Loading ' + esc(tabName(id)) + ' from OnlyFans…</div>';
    var html;
    try { html = await RENDER[id](); }
    catch (e) {
      console.warn('tab ' + id + ' failed', e);
      html = errBox(tabName(id), { e: e });
    }
    if (current !== id) return;      // user switched while we fetched
    cache[id] = html;
    bodyEl.innerHTML = html;
    afterPaint(id);
  }
  function afterPaint(id) {
    if (WIRE[id]) { try { WIRE[id](); } catch (e) { console.warn(e); } }
  }
  window.addEventListener('hashchange', function () { show((location.hash || '').replace('#', '') || 'home'); });
  $('#ofm-reload').addEventListener('click', function () { show(current, true); });

  // ── keyboard nav (friendlier than the real OF embed, which is a bare iframe) ──
  // ← / → step through the tab row; R reloads the current tab. Ignored while the
  // user is typing in the vault search / notification filter, so it never eats input.
  $('#ofm-reload').setAttribute('title', 'Reload this tab (R)');
  document.addEventListener('keydown', function (e) {
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    var t = e.target, tag = t && t.tagName;
    if (tag === 'INPUT' || tag === 'SELECT' || tag === 'TEXTAREA' || (t && t.isContentEditable)) return;
    var order = TABS.map(function (x) { return x[0]; });
    var i = order.indexOf(current);
    if (i < 0) return;
    if (e.key === 'ArrowRight') { location.hash = '#' + order[(i + 1) % order.length]; e.preventDefault(); }
    else if (e.key === 'ArrowLeft') { location.hash = '#' + order[(i - 1 + order.length) % order.length]; e.preventDefault(); }
    else if (e.key === 'r' || e.key === 'R') { show(current, true); e.preventDefault(); }
  });

  // ── no creator scope ────────────────────────────────────────────────
  if (!acct) {
    paintTabs({});
    bodyEl.innerHTML = '<div class="ofm-empty"><b>No creator selected.</b><br>'
      + 'OF Manager mirrors one creator\'s OnlyFans account. Pick a creator in the top bar to load it.</div>';
    $('#ofm-sub').textContent = 'no creator selected';
    return;
  }

  // ── shared "me" (several tabs want it) ──────────────────────────────
  var ME = null;
  async function me() { if (!ME) ME = await Fastt.get('/api/of/v2/users/me'); return ME; }

  var RENDER = {}, WIRE = {};

  // ============================ HOME =================================
  RENDER.home = async function () {
    var r = await Promise.all([
      safe(me()),
      safe(Fastt.get('/api/of/v2/subscriptions/count')),
      safe(Fastt.get('/api/of/v2/users/notifications/count')),
      safe(Fastt.get('/api/of/v2/payouts/balances'))
    ]);
    var m = r[0].ok ? r[0].v : null, sc = r[1].ok ? (r[1].v.subscriptions || {}) : null;
    var nc = r[2].ok ? r[2].v : null, bal = r[3].ok ? r[3].v : null;
    if (nc) { tabCounts.notifications = nc.all || 0; paintTabs(tabCounts); }
    if (!m) return errBox('OnlyFans profile', r[0]);

    var out = '';
    out += '<div class="ofm-hero">'
      + (m.header ? '<img class="hdr" src="' + esc(img(m.headerThumbs && m.headerThumbs.w760 || m.header)) + '" alt="">'
        : '<div class="hdr"></div>')
      + '<div class="who">'
      + (m.avatar ? '<img class="av" src="' + esc(img(m.avatarThumbs && m.avatarThumbs.c144 || m.avatar)) + '" alt="">'
        : '<div class="av"></div>')
      + '<div><div class="nm">' + esc(m.name || '—')
      + (m.isVerified ? '<span class="ofm-vfy">✓</span>' : '') + '</div>'
      + '<div class="un">@' + esc(m.username || '—') + ' · id ' + esc(m.id) + '</div></div>'
      + '<div class="rr">joined <b>' + esc(day(m.joinDate)) + '</b><br>last seen <b>' + esc(ago(m.lastSeen)) + '</b></div>'
      + '</div></div>';

    out += '<div class="ofm-card"><h3>Audience<span class="r">GET /api/of/v2/subscriptions/count</span></h3>'
      + (sc ? '<div class="ofm-stats">'
        + stat('Active subscribers', fmtInt(sc.active), '', 'accent')
        + stat('Expired', fmtInt(sc.expired))
        + stat('Muted', fmtInt(sc.muted))
        + stat('Blocked', fmtInt(sc.blocked))
        + stat('Following', fmtInt(m.subscribesCount), 'creators this account follows')
        + '</div>' : errBox('Subscriber counts', r[1]))
      + '</div>';

    out += '<div class="ofm-card"><h3>Content<span class="r">GET /api/of/v2/users/me</span></h3>'
      + '<div class="ofm-stats">'
      + stat('Posts', fmtInt(m.postsCount), fmtInt(m.archivedPostsCount) + ' archived')
      + stat('Photos', fmtInt(m.photosCount))
      + stat('Videos', fmtInt(m.videosCount))
      + stat('Media items', fmtInt(m.mediasCount))
      + stat('Likes received', fmtInt(m.favoritedCount), '', 'pink')
      + stat('Sub price', fmtMoney(m.subscribePrice || 0), (m.subscriptionBundles || []).length + ' bundle(s)')
      + '</div></div>';

    out += '<div class="ofm-row2">'
      + '<div class="ofm-card"><h3>Balance<span class="r">GET /api/of/v2/payouts/balances</span></h3>'
      + (bal ? '<div class="ofm-stats">'
        + stat('Available now', fmtMoney(bal.payoutAvailable), esc(bal.currency || ''), 'accent')
        + stat('Pending', fmtMoney(bal.payoutPending), bal.manualPayoutPendingDays ? bal.manualPayoutPendingDays + '-day hold' : '')
        + stat('Min payout', fmtMoney(bal.minPayoutSumm), 'period: ' + esc(bal.withdrawalPeriod || '—'))
        + '</div>' : errBox('Balances', r[3]))
      + '</div>'
      + '<div class="ofm-card"><h3>Unread on OnlyFans<span class="r">GET /api/of/v2/users/notifications/count</span></h3>'
      + (nc ? (function () {
        var keys = Object.keys(nc).filter(function (k) { return k !== 'all' && nc[k]; });
        return '<div class="ofm-stats">' + stat('All unread', fmtInt(nc.all)) + '</div>'
          + '<div class="ofm-flags">' + (keys.length
            ? keys.map(function (k) { return '<span class="ofm-pill blue">' + esc(k) + ' · ' + fmtInt(nc[k]) + '</span>'; }).join('')
            : '<span class="ofm-pill">nothing else unread</span>') + '</div>'
          + '<div class="sub" style="margin-top:10px">Open the <b>Notifications</b> tab for the feed itself.</div>';
      })() : errBox('Notification counts', r[2]))
      + '</div></div>';
    return out;
  };

  // ========================== NEW POST ===============================
  RENDER.post = async function () {
    var r = await Promise.all([
      safe(Fastt.get('/api/of/v2/users/' + acct + '/posts', { limit: 12 })),
      safe(Fastt.get('/api/of/v2/users/settings/post')),
      safe(Fastt.get('/api/of/v2/schedules'))
    ]);
    var posts = r[0].ok ? (Array.isArray(r[0].v) ? r[0].v : (r[0].v.list || [])) : null;
    var ps = r[1].ok ? r[1].v : null;
    var sched = r[2].ok ? (r[2].v.list || []) : null;

    var out = '<div class="ofm-card"><h3>Publish a post</h3>'
      + '<div class="sub">OF Manager is a <b>read-only mirror</b> — it never posts publicly. Composing, media '
      + 'attachment and scheduling live on the Create Post page, which owns the write path.</div>'
      + '<div class="ofm-bar"><a class="ofm-btn pri" href="content-create-post.html">Open Create Post →</a>'
      + '<a class="ofm-btn" href="content-auto-posts.html">Auto Posts (drip) →</a>'
      + '<a class="ofm-btn" href="auto-stories.html">Auto Stories →</a></div></div>';

    out += '<div class="ofm-card"><h3>Post settings<span class="r">GET /api/of/v2/users/settings/post</span></h3>'
      + (ps ? '<div class="ofm-flags">'
        + '<span class="ofm-pill ' + (ps.isEnabled ? 'ok' : '') + '">auto-delete posts: ' + (ps.isEnabled ? 'on' : 'off') + '</span>'
        + '<span class="ofm-pill">after ' + esc(ps.currentCode) + ' h</span>'
        + '<span class="ofm-pill">options: ' + esc((ps.options || []).join(' / ')) + ' h</span>'
        + '</div>' : errBox('Post settings', r[1]))
      + '</div>';

    out += '<div class="ofm-card"><h3>Scheduled posts<span class="r">GET /api/of/v2/schedules</span></h3>'
      + (sched === null ? errBox('Scheduled posts', r[2])
        : sched.length ? '<div class="ofm-wrap"><table class="ofm-tbl"><thead><tr><th>When</th><th>Text</th><th class="num">Media</th></tr></thead><tbody>'
          + sched.map(function (s) {
            return '<tr><td class="mut">' + esc(dt(s.scheduledDate || s.postedAt)) + '</td><td>'
              + esc(strip(s.text).slice(0, 90) || '—') + '</td><td class="num">' + esc((s.media || []).length) + '</td></tr>';
          }).join('') + '</tbody></table></div>'
          : '<div class="ofm-empty"><b>Nothing scheduled.</b><br>OnlyFans returned an empty schedule list for this creator right now — this is a live read, not a stub.</div>')
      + '</div>';

    out += '<div class="ofm-card"><h3>Recently published<span class="r">GET /api/of/v2/users/' + esc(acct) + '/posts</span></h3>'
      + (posts === null ? errBox('Posts', r[0])
        : posts.length ? '<div class="ofm-grid">' + posts.map(function (p) {
          var m0 = (p.media || [])[0] || {}, f = m0.files || {};
          var u = (f.thumb && f.thumb.url) || (f.preview && f.preview.url) || (f.full && f.full.url) || '';
          return '<div class="ofm-mi" title="' + esc(strip(p.text)) + '">'
            + (u ? '<img src="' + esc(img(u)) + '" alt="" loading="lazy">' : '')
            + '<span class="tg">' + esc(day(p.postedAt)) + '</span>'
            + '<span class="mt"><span>♥ ' + esc(p.favoritesCount || 0) + '</span><span>' + esc(p.mediaCount || 0) + ' media</span></span>'
            + '</div>';
        }).join('') + '</div>'
        : '<div class="ofm-empty">This account has no posts on the mirror.</div>')
      + '</div>';
    return out;
  };

  // ======================== NOTIFICATIONS ============================
  var NOTIF_TYPES = [['', 'All'], ['purchases', 'Purchases'], ['tip', 'Tips'], ['subscribed', 'Subscribed'],
    ['message', 'Messages'], ['favorited', 'Likes'], ['commented', 'Comments'], ['mentioned', 'Mentions']];
  var notifState = { type: '', rows: [], offset: 0, more: true };
  function notifRow(n) {
    var u = n.user || {};
    var nm = u.name || (n.replacePairs && n.replacePairs['{NAME}']) || 'OnlyFans';
    var av = (u.avatarThumbs && u.avatarThumbs.c50) || u.avatar || '';
    return '<div class="it' + (n.isRead ? '' : ' unread') + '">'
      + (av ? '<img class="av2" src="' + esc(img(av)) + '" alt="">' : '<div class="av2">' + esc(initials(nm)) + '</div>')
      + '<div class="bd"><div class="tt"><b>' + esc(nm) + '</b> ' + esc(strip(n.text)) + '</div>'
      + '<div class="mm"><span>' + esc(dt(n.createdAt)) + '</span><span>' + esc(ago(n.createdAt)) + '</span>'
      + '<span class="ofm-pill">' + esc(n.type || '?') + '</span>'
      + (n.subType ? '<span class="ofm-pill">' + esc(n.subType) + '</span>' : '')
      + (n.isRead ? '' : '<span class="ofm-pill blue">unread</span>') + '</div></div></div>';
  }
  RENDER.notifications = async function () {
    notifState = { type: '', rows: [], offset: 0, more: true };
    var r = await Promise.all([
      safe(Fastt.get('/api/of/v2/users/notifications', { limit: 25, offset: 0 })),
      safe(Fastt.get('/api/of/v2/users/notifications/count'))
    ]);
    var rows = r[0].ok ? (Array.isArray(r[0].v) ? r[0].v : (r[0].v.list || [])) : null;
    var nc = r[1].ok ? r[1].v : null;
    if (nc) { tabCounts.notifications = nc.all || 0; paintTabs(tabCounts); }
    if (rows === null) return errBox('OnlyFans notifications', r[0]);
    notifState.rows = rows; notifState.offset = rows.length; notifState.more = rows.length >= 25;

    return '<div class="ofm-card"><h3>OnlyFans notifications'
      + '<span class="r">GET /api/of/v2/users/notifications</span></h3>'
      + '<div class="sub">The creator\'s own OnlyFans notification feed — purchases, tips, subs, likes. '
      + '(The sidebar\'s Notifications page shows <i>relay</i> events instead; this is OF\'s.)</div>'
      + (nc ? '<div class="ofm-flags">' + Object.keys(nc).map(function (k) {
        return '<span class="ofm-pill' + (nc[k] ? ' blue' : '') + '">' + esc(k) + ' ' + fmtInt(nc[k]) + '</span>';
      }).join('') + '</div>' : '')
      + '<div class="ofm-bar"><select class="ofm-sel" id="ofm-ntype">'
      + NOTIF_TYPES.map(function (t) { return '<option value="' + esc(t[0]) + '">' + esc(t[1]) + '</option>'; }).join('')
      + '</select><span class="sub" id="ofm-ncount">' + rows.length + ' loaded</span></div>'
      + '<div class="ofm-feed" id="ofm-nfeed">'
      + (rows.length ? rows.map(notifRow).join('') : '<div class="ofm-empty">No notifications in this feed.</div>')
      + '</div>'
      + '<div class="ofm-bar"><button class="ofm-btn" id="ofm-nmore">Load more</button></div>'
      + '</div>';
  };
  WIRE.notifications = function () {
    var sel = $('#ofm-ntype'), feed = $('#ofm-nfeed'), more = $('#ofm-nmore'), cnt = $('#ofm-ncount');
    if (!sel) return;
    sel.value = notifState.type;
    async function load(reset) {
      if (reset) { notifState.rows = []; notifState.offset = 0; notifState.more = true; }
      more.disabled = true; more.textContent = 'Loading…';
      try {
        var p = { limit: 25, offset: notifState.offset };
        if (notifState.type) p.type = notifState.type;
        var out = await Fastt.get('/api/of/v2/users/notifications', p);
        var rows = Array.isArray(out) ? out : (out.list || []);
        notifState.rows = notifState.rows.concat(rows);
        notifState.offset += rows.length;
        notifState.more = rows.length >= 25;
      } catch (e) { Fastt.oops(e); }
      feed.innerHTML = notifState.rows.length
        ? notifState.rows.map(notifRow).join('')
        : '<div class="ofm-empty">No notifications of this type.</div>';
      cnt.textContent = notifState.rows.length + ' loaded';
      more.disabled = !notifState.more;
      more.textContent = notifState.more ? 'Load more' : 'No more';
      cache.notifications = bodyEl.innerHTML;
    }
    sel.addEventListener('change', function () { notifState.type = sel.value; load(true); });
    more.addEventListener('click', function () { load(false); });
    more.disabled = !notifState.more;
    if (!notifState.more) more.textContent = 'No more';
  };

  // ========================= MESSAGES BASIC ==========================
  RENDER.messages = async function () {
    var out = await safe(Fastt.get('/api/of/v2/chats', { limit: 25, order: 'recent' }));
    if (!out.ok) return errBox('Chat list', out);
    var list = out.v.list || [];
    var ids = list.map(function (c) { return (c.withUser || {}).id; }).filter(Boolean);
    var fans = {};
    if (ids.length) {
      var fr = await safe(Fastt.get('/admin/fans/' + acct + '/by-ids', { ids: ids.join(',') }));
      if (fr.ok) fans = fr.v.fans || {};
    }
    var unread = list.filter(function (c) { return (c.unreadMessagesCount || 0) > 0; }).length;
    if (unread) { tabCounts.messages = unread; paintTabs(tabCounts); }
    return '<div class="ofm-card"><h3>Chats<span class="r">GET /api/of/v2/chats · names from /admin/fans/…/by-ids</span></h3>'
      + '<div class="sub">The plain OnlyFans inbox. The full two-pane chat with spend, PPV history and sending '
      + 'lives in <b>Messages Pro</b>.</div>'
      + '<div class="ofm-bar"><a class="ofm-btn pri" href="messages.html">Open Messages Pro →</a>'
      + '<span class="sub">' + list.length + ' recent chats · ' + unread + ' unread</span></div>'
      + (list.length ? '<div class="ofm-feed">' + list.map(function (c) {
        var uid = (c.withUser || {}).id, f = fans[String(uid)] || {};
        var nm = f.name || ('fan ' + uid);
        var lm = c.lastMessage || {};
        var mine = (lm.fromUser || {}).id && String((lm.fromUser || {}).id) === String(acct);
        return '<div class="it' + (c.unreadMessagesCount ? ' unread' : '') + '">'
          + (f.avatar ? '<img class="av2" src="' + esc(img(f.avatar)) + '" alt="">' : '<div class="av2">' + esc(initials(nm)) + '</div>')
          + '<div class="bd"><div class="tt"><b>' + esc(nm) + '</b>' + (f.username ? ' <span style="color:#6a6a6a">@' + esc(f.username) + '</span>' : '')
          + '<br>' + (mine ? '<span style="color:#6a6a6a">you: </span>' : '') + esc(strip(lm.text).slice(0, 130) || '(media)') + '</div>'
          + '<div class="mm"><span>' + esc(ago(lm.createdAt)) + '</span>'
          + (lm.mediaCount ? '<span class="ofm-pill">' + esc(lm.mediaCount) + ' media</span>' : '')
          + (lm.isTip ? '<span class="ofm-pill ok">tip</span>' : '')
          + (c.unreadMessagesCount ? '<span class="ofm-pill err">' + esc(c.unreadMessagesCount) + ' unread</span>' : '')
          + (c.isMutedNotifications ? '<span class="ofm-pill warn">muted</span>' : '')
          + (c.canSendMessage ? '' : '<span class="ofm-pill err">cannot send</span>')
          + '</div></div></div>';
      }).join('') + '</div>'
        : '<div class="ofm-empty">OnlyFans returned no chats for this creator.</div>')
      + '</div>';
  };

  // ============================= VAULT ===============================
  var vaultState = { type: 'all', list_id: '', query: '', items: [], offset: 0, more: true, lists: [] };
  function vaultTile(m) {
    var f = m.files || {};
    var u = (f.thumb && f.thumb.url) || (f.squarePreview && f.squarePreview.url) || (f.preview && f.preview.url) || '';
    var c = m.counters || {};
    return '<div class="ofm-mi" title="' + esc(m.id) + ' · ' + esc(dt(m.createdAt)) + '">'
      + (u ? '<img src="' + esc(img(u)) + '" alt="" loading="lazy">' : '')
      + '<span class="tg">' + esc(m.type || '?') + '</span>'
      + '<span class="mt"><span>♥ ' + esc(c.likesCount || 0) + '</span>'
      + '<span>' + esc(c.buyersCount || 0) + ' buyers</span>'
      + (c.tipsSumm ? '<span>' + fmtMoney(c.tipsSumm) + '</span>' : '') + '</span></div>';
  }
  RENDER.vault = async function () {
    vaultState = { type: 'all', list_id: '', query: '', items: [], offset: 0, more: true, lists: vaultState.lists };
    var r = await Promise.all([
      safe(Fastt.get('/api/of/v2/vault/media', { limit: 48, offset: 0, type: 'all' })),
      safe(Fastt.get('/api/of/v2/vault/lists', { limit: 50 }))
    ]);
    if (!r[0].ok) return errBox('Vault media', r[0]);
    var items = r[0].v.list || [];
    vaultState.items = items; vaultState.offset = items.length; vaultState.more = items.length >= 48;
    vaultState.lists = r[1].ok ? (r[1].v.list || []) : [];
    return '<div class="ofm-card"><h3>Vault<span class="r">GET /api/of/v2/vault/media</span></h3>'
      + '<div class="sub">Server-side filters: type, folder, text search. Thumbnails ride the relay\'s image proxy '
      + '(OnlyFans CDN urls are signed and expire).</div>'
      + '<div class="ofm-bar">'
      + '<select class="ofm-sel" id="ofm-vtype">'
      + ['all', 'photo', 'video', 'gif', 'audio'].map(function (t) { return '<option value="' + t + '">' + t + '</option>'; }).join('')
      + '</select>'
      + '<select class="ofm-sel" id="ofm-vlist"><option value="">All folders</option>'
      + vaultState.lists.map(function (l) {
        return '<option value="' + esc(l.id) + '">' + esc(l.name) + ' (' + ((l.photosCount || 0) + (l.videosCount || 0) + (l.gifsCount || 0) + (l.audiosCount || 0)) + ')</option>';
      }).join('') + '</select>'
      + '<input class="ofm-inp" id="ofm-vq" placeholder="search captions…" style="width:200px">'
      + '<button class="ofm-btn" id="ofm-vgo">Search</button>'
      + '<span class="sub" id="ofm-vcount">' + items.length + ' shown</span></div>'
      + '<div class="ofm-grid" id="ofm-vgrid">'
      + (items.length ? items.map(vaultTile).join('') : '') + '</div>'
      + (items.length ? '' : '<div class="ofm-empty">No media matched.</div>')
      + '<div class="ofm-bar"><button class="ofm-btn" id="ofm-vmore">Load more</button></div>'
      + '</div>';
  };
  WIRE.vault = function () {
    var grid = $('#ofm-vgrid'), more = $('#ofm-vmore'), cnt = $('#ofm-vcount');
    if (!grid) return;
    $('#ofm-vtype').value = vaultState.type;
    $('#ofm-vlist').value = vaultState.list_id;
    $('#ofm-vq').value = vaultState.query;
    async function load(reset) {
      if (reset) { vaultState.items = []; vaultState.offset = 0; vaultState.more = true; }
      more.disabled = true; more.textContent = 'Loading…';
      try {
        var p = { limit: 48, offset: vaultState.offset, type: vaultState.type || 'all' };
        if (vaultState.list_id) p.list_id = vaultState.list_id;
        if (vaultState.query) p.query = vaultState.query;
        var out = await Fastt.get('/api/of/v2/vault/media', p);
        var list = out.list || [];
        vaultState.items = vaultState.items.concat(list);
        vaultState.offset += list.length;
        vaultState.more = list.length >= 48;
      } catch (e) { Fastt.oops(e); vaultState.more = false; }
      grid.innerHTML = vaultState.items.length ? vaultState.items.map(vaultTile).join('')
        : '<div class="ofm-empty" style="grid-column:1/-1">Nothing in the vault matches this filter.</div>';
      cnt.textContent = vaultState.items.length + ' shown';
      more.disabled = !vaultState.more;
      more.textContent = vaultState.more ? 'Load more' : 'No more';
      cache.vault = bodyEl.innerHTML;
    }
    function refilter() {
      vaultState.type = $('#ofm-vtype').value;
      vaultState.list_id = $('#ofm-vlist').value;
      vaultState.query = $('#ofm-vq').value.trim();
      load(true);
    }
    $('#ofm-vtype').addEventListener('change', refilter);
    $('#ofm-vlist').addEventListener('change', refilter);
    $('#ofm-vgo').addEventListener('click', refilter);
    $('#ofm-vq').addEventListener('keydown', function (e) { if (e.key === 'Enter') refilter(); });
    more.addEventListener('click', function () { load(false); });
    more.disabled = !vaultState.more;
    if (!vaultState.more) more.textContent = 'No more';
  };

  // =========================== COLLECTIONS ===========================
  RENDER.collections = async function () {
    var r = await safe(Fastt.get('/api/of/v2/vault/lists', { limit: 50 }));
    if (!r.ok) return errBox('Vault collections', r);
    var lists = r.v.list || [];
    return '<div class="ofm-card"><h3>Collections<span class="r">GET /api/of/v2/vault/lists</span></h3>'
      + '<div class="sub">The creator\'s vault folders. Automations address these by <b>name</b> '
      + '(tip-reward tiers, teaser rungs, auto-stories folders).</div>'
      + (lists.length ? '<div class="ofm-lists">' + lists.map(function (l) {
        var n = (l.photosCount || 0) + (l.videosCount || 0) + (l.gifsCount || 0) + (l.audiosCount || 0);
        return '<div class="ofm-list"><div class="nm">' + esc(l.name)
          + '<span class="ofm-pill">' + esc(l.type || 'custom') + '</span></div>'
          + '<div class="ct">' + n + ' item' + (n === 1 ? '' : 's') + ' · '
          + (l.photosCount || 0) + ' photo · ' + (l.videosCount || 0) + ' video · '
          + (l.gifsCount || 0) + ' gif · ' + (l.audiosCount || 0) + ' audio</div>'
          + ((l.medias || []).length
            ? '<div class="cv">' + l.medias.slice(0, 4).map(function (m) {
              return m.url ? '<img src="' + esc(img(m.url)) + '" alt="" loading="lazy">' : '<div class="ph"></div>';
            }).join('') + '</div>'
            : '<div class="ct" style="margin-top:9px;color:#6a6a6a">empty folder — nothing to preview</div>')
          + '</div>';
      }).join('') + '</div>'
        : '<div class="ofm-empty">This vault has no folders.</div>')
      + '</div>';
  };

  // ============================= QUEUE ===============================
  RENDER.queue = async function () {
    var r = await Promise.all([
      safe(Fastt.get('/api/of/v2/messages/queue', { limit: 25 })),
      safe(Fastt.get('/api/of/v2/schedules/later/chat', { limit: 25 })),
      safe(Fastt.get('/api/of/v2/scheduled-sends'))
    ]);
    function asList(x) { return Array.isArray(x) ? x : ((x && x.list) || []); }
    var mq = r[0].ok ? asList(r[0].v) : null;
    var lc = r[1].ok ? asList(r[1].v) : null;
    var ss = r[2].ok ? asList(r[2].v) : null;

    function section(title, route, rows, res, cols, rowFn, emptyWhy) {
      return '<div class="ofm-card"><h3>' + esc(title) + '<span class="r">' + esc(route) + '</span></h3>'
        + (rows === null ? errBox(title, res)
          : rows.length ? '<div class="ofm-wrap"><table class="ofm-tbl"><thead><tr>'
            + cols.map(function (c) { return '<th>' + esc(c) + '</th>'; }).join('') + '</tr></thead><tbody>'
            + rows.map(rowFn).join('') + '</tbody></table></div>'
            : '<div class="ofm-empty"><b>Empty right now.</b><br>' + esc(emptyWhy) + '</div>')
        + '</div>';
    }
    var out = '<div class="ofm-card"><h3>Queue</h3><div class="sub">Everything OnlyFans (and the relay) is holding to '
      + 'send later for this creator. Read-only: this page never enqueues, cancels or sends.</div></div>';
    out += section('Mass-message queue', 'GET /api/of/v2/messages/queue', mq, r[0],
      ['Id', 'State', 'Text', 'When'], function (q) {
        return '<tr><td class="mut">' + esc(q.id != null ? q.id : '—') + '</td><td>'
          + '<span class="ofm-pill">' + esc(q.state || q.status || '—') + '</span></td><td>'
          + esc(strip(q.text).slice(0, 90) || '—') + '</td><td class="mut">' + esc(dt(q.createdAt || q.scheduledDate)) + '</td></tr>';
      }, 'OnlyFans reports no mass message in flight or queued for this account.');
    out += section('Scheduled chats', 'GET /api/of/v2/schedules/later/chat', lc, r[1],
      ['When', 'Text', 'Media'], function (s) {
        return '<tr><td class="mut">' + esc(dt(s.scheduledDate || s.createdAt)) + '</td><td>'
          + esc(strip(s.text).slice(0, 90) || '—') + '</td><td class="num">' + esc((s.media || []).length) + '</td></tr>';
      }, 'OnlyFans holds no scheduled chat message for this account.');
    out += section('Relay scheduled sends', 'GET /api/of/v2/scheduled-sends', ss, r[2],
      ['Job', 'Fan', 'Run at', 'State'], function (s) {
        return '<tr><td class="mut">' + esc(s.id != null ? s.id : '—') + '</td><td>' + esc(s.fan_id || s.user_id || '—')
          + '</td><td class="mut">' + esc(dt(s.run_at || s.scheduled_at)) + '</td><td>'
          + '<span class="ofm-pill">' + esc(s.state || s.status || '—') + '</span></td></tr>';
      }, 'The relay is holding no scheduled 1:1 send. Create one from a chat in Messages Pro.');
    return out;
  };

  // =========================== STATEMENTS ============================
  RENDER.statements = async function () {
    var r = await Promise.all([
      safe(Fastt.get('/api/of/v2/payouts/transactions', { limit: 40 })),
      safe(Fastt.get('/api/of/v2/payouts/stats')),
      safe(Fastt.get('/api/of/v2/payouts/chargebacks/ratio')),
      safe(Fastt.get('/api/of/v2/payouts/balances'))
    ]);
    var tx = r[0].ok ? (r[0].v.list || []) : null;
    var months = r[1].ok ? ((r[1].v.list || {}).months || {}) : null;
    var cb = r[2].ok ? r[2].v : null;
    var bal = r[3].ok ? r[3].v : null;

    var out = '';
    var grossNet = null;
    if (months) {
      var keys = Object.keys(months).sort(function (a, b) { return Number(a) - Number(b); });
      var last = keys.slice(-12);
      grossNet = last.map(function (k) {
        var m = months[k] || {};
        return { label: new Date(Number(k) * 1000).toISOString().slice(0, 7), value: Number(m.total_gross) || 0,
                 net: Number(m.total_net) || 0,
                 tips: (m.tips || []).length, subs: (m.subscribes || []).length,
                 chat: (m.chat_messages || []).length };
      });
    }
    out += '<div class="ofm-card"><h3>Money in<span class="r">GET /api/of/v2/payouts/balances · /chargebacks/ratio</span></h3>'
      + '<div class="ofm-stats">'
      + (bal ? stat('Available', fmtMoney(bal.payoutAvailable), esc(bal.currency || ''), 'accent')
        + stat('Pending', fmtMoney(bal.payoutPending), (bal.manualPayoutPendingDays || 0) + '-day hold') : '')
      + (grossNet && grossNet.length ? stat('This month gross', fmtMoney(grossNet[grossNet.length - 1].value),
        'net ' + fmtMoney(grossNet[grossNet.length - 1].net)) : '')
      + (cb ? stat('Chargeback ratio', (Number(cb.chargebacksRatio) || 0).toFixed(2) + '%') : '')
      + '</div></div>';

    out += '<div class="ofm-card"><h3>Monthly earnings<span class="r">GET /api/of/v2/payouts/stats</span></h3>'
      + (months === null ? errBox('Monthly earnings', r[1])
        : chartOrEmpty(grossNet, 'bar', function (v) { return '$' + Math.round(v); },
          'OnlyFans returned no monthly earnings buckets.', true)
        + (grossNet && grossNet.length ? '<div class="ofm-chartnote">Gross per month, last '
          + grossNet.length + ' months with data.</div>'
          + '<div class="ofm-wrap"><table class="ofm-tbl"><thead><tr><th>Month</th><th class="num">Gross</th>'
          + '<th class="num">Net</th><th class="num">Tips</th><th class="num">Subs</th><th class="num">Chat</th></tr></thead><tbody>'
          + grossNet.slice().reverse().map(function (m) {
            return '<tr><td>' + esc(m.label) + '</td><td class="num">' + fmtMoney(m.value) + '</td>'
              + '<td class="num">' + fmtMoney(m.net) + '</td><td class="num mut">' + m.tips + '</td>'
              + '<td class="num mut">' + m.subs + '</td><td class="num mut">' + m.chat + '</td></tr>';
          }).join('') + '</tbody></table></div>' : ''))
      + '</div>';

    out += '<div class="ofm-card"><h3>Transactions<span class="r">GET /api/of/v2/payouts/transactions</span></h3>'
      + (tx === null ? errBox('Transactions', r[0])
        : tx.length ? '<div class="ofm-wrap"><table class="ofm-tbl"><thead><tr><th>When</th><th>Kind</th><th>From</th>'
          + '<th class="num">Gross</th><th class="num">Fee</th><th class="num">Net</th><th>Status</th></tr></thead><tbody>'
          + tx.map(function (t) {
            var u = t.user || {}, dd = t.descriptionDetails || {};
            return '<tr><td class="mut">' + esc(dt(t.createdAt)) + '</td>'
              + '<td><span class="ofm-pill">' + esc(dd.type || '—') + '</span></td>'
              + '<td>' + esc(u.name || strip(t.description).slice(0, 40) || '—') + '</td>'
              + '<td class="num">' + fmtMoney(t.amount) + '</td>'
              + '<td class="num mut">' + fmtMoney(t.fee) + '</td>'
              + '<td class="num">' + fmtMoney(t.net) + '</td>'
              + '<td><span class="ofm-pill' + (t.status === 'done' ? ' ok' : '') + '">' + esc(t.status || '—') + '</span></td></tr>';
          }).join('') + '</tbody></table></div>'
          : '<div class="ofm-empty">No transactions in this window.</div>')
      + '</div>';
    return out;
  };

  // ============================== BANK ===============================
  RENDER.bank = async function () {
    var r = await Promise.all([
      safe(Fastt.get('/api/of/v2/payouts/balances')),
      safe(Fastt.get('/api/of/v2/payouts/requests')),
      safe(Fastt.get('/api/of/v2/payouts/chargebacks/ratio')),
      safe(Fastt.get('/api/of/v2/payments/referrals/balance'))
    ]);
    var bal = r[0].ok ? r[0].v : null;
    var reqs = r[1].ok ? (r[1].v.list || []) : null;
    var cb = r[2].ok ? r[2].v : null;
    var ref = r[3].ok ? r[3].v : null;

    var out = '<div class="ofm-card"><h3>Payout account<span class="r">GET /api/of/v2/payouts/balances</span></h3>'
      + (bal ? '<div class="ofm-stats">'
        + stat('Available', fmtMoney(bal.payoutAvailable), esc(bal.currency || ''), 'accent')
        + stat('Pending', fmtMoney(bal.payoutPending), (bal.manualPayoutPendingDays || 0) + '-day hold')
        + stat('Min payout', fmtMoney(bal.minPayoutSumm))
        + stat('Withdrawal period', esc(bal.withdrawalPeriod || '—'))
        + (cb ? stat('Chargeback ratio', (Number(cb.chargebacksRatio) || 0).toFixed(2) + '%') : '')
        + (ref ? stat('Referral earnings', fmtMoney(ref.referralEarnings)) : '')
        + '</div>'
        + '<div class="ofm-flags">' + (bal.withdrawalPeriodOptions || []).map(function (o) {
          return '<span class="ofm-pill' + (o.code === bal.withdrawalPeriod ? ' ok' : '') + '">' + esc(o.name) + '</span>';
        }).join('') + '</div>'
        + '<div class="sub" style="margin-top:10px">Read-only mirror — changing the payout period or requesting a '
        + 'withdrawal is done on OnlyFans itself.</div>'
        : errBox('Balances', r[0]))
      + '</div>';

    var approved = (reqs || []).filter(function (q) { return q.state === 'approved'; });
    var total = approved.reduce(function (a, q) { return a + (Number(q.amount) || 0); }, 0);
    out += '<div class="ofm-card"><h3>Payout requests<span class="r">GET /api/of/v2/payouts/requests</span></h3>'
      + (reqs === null ? errBox('Payout requests', r[1])
        : reqs.length ? '<div class="ofm-stats">'
          + stat('Requests', fmtInt(reqs.length))
          + stat('Approved', fmtInt(approved.length))
          + stat('Approved total', fmtMoney(total), '', 'accent')
          + stat('Latest', fmtMoney(reqs[0].amount), esc(day(reqs[0].createdAt)))
          + '</div>'
          + chartOrEmpty(reqs.slice(0, 14).reverse().map(function (q) {
            return { label: mmdd(q.createdAt), value: Number(q.amount) || 0 };
          }), 'bar', function (v) { return '$' + Math.round(v); }, 'No payout history.', true)
          + '<div class="ofm-wrap"><table class="ofm-tbl"><thead><tr><th>Invoice</th><th>Requested</th>'
          + '<th class="num">Amount</th><th>State</th><th>Reason</th></tr></thead><tbody>'
          + reqs.map(function (q) {
            return '<tr><td class="mut">' + esc(q.invoiceId) + '</td><td class="mut">' + esc(dt(q.createdAt)) + '</td>'
              + '<td class="num">' + fmtMoney(q.amount) + '</td>'
              + '<td><span class="ofm-pill' + (q.state === 'approved' ? ' ok' : (q.state === 'rejected' ? ' err' : ' warn')) + '">'
              + esc(q.state) + '</span></td><td class="mut">' + esc(q.rejectReason || '—') + '</td></tr>';
          }).join('') + '</tbody></table></div>'
          : '<div class="ofm-empty">No payout has ever been requested on this account.</div>')
      + '</div>';
    return out;
  };

  // =========================== STATISTICS ============================
  RENDER.statistics = async function () {
    var r = await Promise.all([
      safe(Fastt.get('/api/of/v2/users/me/stats/overview')),
      safe(Fastt.get('/api/of/v2/users/me/profile/stats')),
      safe(Fastt.get('/api/of/v2/subscriptions/subscribers/chart')),
      safe(Fastt.get('/api/of/v2/posts/chart')),
      safe(Fastt.get('/admin/stats/revenue')),
      safe(Fastt.get('/api/of/v2/users/me/stats/top/post', { limit: 1 }))
    ]);
    var ov = r[0].ok ? (r[0].v.visitors || {}) : null;
    var pf = r[1].ok ? r[1].v : null;
    var subs = r[2].ok ? (r[2].v.earnings || []) : null;
    var posts = r[3].ok ? ((r[3].v.posts || {}).chart || []) : null;
    var rev = r[4].ok ? (r[4].v.rows || []) : null;
    var top = r[5].ok ? (r[5].v.views || null) : null;

    var out = '';
    if (ov) {
      var v = ov.visitors || {}, e = ov.earnings || {}, s = ov.subscriptions || {};
      out += '<div class="ofm-card"><h3>Last 30 days<span class="r">GET /api/of/v2/users/me/stats/overview</span></h3>'
        + '<div class="ofm-stats">'
        + stat('Profile visitors', fmtInt(v.total), delta(v.delta))
        + stat('Earnings (net)', fmtMoney(e.total), delta(e.delta) + ' · gross ' + fmtMoney(e.gross), 'accent')
        + stat('New subs', fmtInt((s.new || {}).total), delta((s.new || {}).delta))
        + stat('Renewals', fmtInt((s.renew || {}).total), delta((s.renew || {}).delta))
        + '</div></div>';
    } else { out += errBox('Overview stats', r[0]); }

    var vis = pf && pf.chart && pf.chart.visitors ? pf.chart.visitors : null;
    out += '<div class="ofm-card"><h3>Profile visitors<span class="r">GET /api/of/v2/users/me/profile/stats</span></h3>'
      + (vis === null ? errBox('Profile stats', r[1])
        : chartOrEmpty(vis.map(function (p) { return { label: mmdd(p.date), value: Number(p.count) || 0 }; }),
          'line', null, 'OnlyFans returned no visitor series.'))
      + (vis && vis.length ? '<div class="ofm-chartnote">' + vis.length + ' days · '
        + fmtInt(vis.reduce(function (a, p) { return a + (Number(p.count) || 0); }, 0)) + ' visits in window</div>' : '')
      + '</div>';

    out += '<div class="ofm-row2">'
      + '<div class="ofm-card"><h3>New subscribers / day<span class="r">GET /api/of/v2/subscriptions/subscribers/chart</span></h3>'
      + (subs === null ? errBox('Subscriber chart', r[2])
        : chartOrEmpty(subs.map(function (p) { return { label: mmdd(p.date), value: Number(p.count) || 0 }; }),
          'bar', null, 'OnlyFans returned no subscriber series.'))
      + '<div class="ofm-chartnote">The response key is literally <code>earnings</code> — it carries subscriber counts.</div>'
      + '</div>'
      + '<div class="ofm-card"><h3>Posts published / day<span class="r">GET /api/of/v2/posts/chart</span></h3>'
      + (posts === null ? errBox('Post chart', r[3])
        : chartOrEmpty(posts.map(function (p) { return { label: mmdd(p.date), value: Number(p.count) || 0 }; }),
          'bar', null, 'OnlyFans returned no post series.'))
      + '</div></div>';

    // revenue: OF's own /earnings/chart returns [] even with a range, so use the
    // relay's ingested transaction ledger instead of drawing an empty box.
    var byDay = {};
    (rev || []).forEach(function (row) { byDay[row.day] = (byDay[row.day] || 0) + (Number(row.total_cents) || 0); });
    var days = Object.keys(byDay).sort().slice(-30);
    out += '<div class="ofm-card"><h3>Revenue, last 30 days with data<span class="r">GET /admin/stats/revenue</span></h3>'
      + '<div class="sub">From the relay\'s own ingested transaction ledger. OnlyFans\' '
      + '<code>/api/of/v2/earnings/chart</code> returns an empty array on this account even with a date range, '
      + 'so nothing is hung on it.</div>'
      + (rev === null ? errBox('Relay revenue', r[4])
        : chartOrEmpty(days.map(function (d) { return { label: mmdd(d), value: byDay[d] / 100 }; }),
          'bar', function (v) { return '$' + Math.round(v); }, 'The relay has ingested no transactions for this creator.', true))
      + (days.length ? '<div class="ofm-chartnote">' + days.length + ' days · '
        + fmtMoney(days.reduce(function (a, d) { return a + byDay[d]; }, 0) / 100) + ' total</div>' : '')
      + '</div>';

    var tc = pf && pf.topCountries ? pf.topCountries : null;
    out += '<div class="ofm-row2">'
      + '<div class="ofm-card"><h3>Top countries<span class="r">profile/stats.topCountries</span></h3>'
      + (tc && (tc.rows || []).length ? '<div class="ofm-wrap"><table class="ofm-tbl"><thead><tr><th>#</th><th>Country</th>'
        + '<th class="num">Views</th><th class="num">Guests</th><th class="num">Users</th></tr></thead><tbody>'
        + tc.rows.map(function (row) {
          var vc = row.viewsCount || {};
          return '<tr><td class="mut">' + esc(row.rank) + '</td><td>' + esc(row.countryName || '—')
            + (row.countryCode ? ' <span class="mut">' + esc(row.countryCode) + '</span>' : '') + '</td>'
            + '<td class="num">' + fmtInt(vc.total) + '</td><td class="num mut">' + fmtInt(vc.guests) + '</td>'
            + '<td class="num mut">' + fmtInt(vc.users) + '</td></tr>';
        }).join('') + '</tbody></table></div>'
        + '<div class="ofm-chartnote">' + fmtInt((tc.totals || {}).total) + ' views · '
        + fmtInt((tc.totals || {}).guests) + ' guests · ' + fmtInt((tc.totals || {}).users) + ' logged-in</div>'
        : '<div class="ofm-empty">OnlyFans returned no country breakdown.</div>')
      + '</div>'
      + '<div class="ofm-card"><h3>Top post<span class="r">GET /api/of/v2/users/me/stats/top/post</span></h3>'
      + (top ? (function () {
        var st = top.stats || {}, m0 = (top.media || [])[0] || {}, f = m0.files || {};
        var u = (f.thumb && f.thumb.url) || (f.preview && f.preview.url) || '';
        return '<div class="ofm-feed"><div class="it">'
          + (u ? '<img class="thumb" src="' + esc(img(u)) + '" alt="">' : '<div class="thumb"></div>')
          + '<div class="bd"><div class="tt"><b>' + esc(strip(top.text) || '(no caption)') + '</b></div>'
          + '<div class="mm"><span>' + esc(dt(top.postedAt)) + '</span>'
          + '<span class="ofm-pill">' + fmtInt(st.lookCount) + ' views</span>'
          + '<span class="ofm-pill">' + fmtInt(st.uniqueLookCount) + ' unique</span>'
          + '<span class="ofm-pill">♥ ' + fmtInt(st.likeCount) + '</span>'
          + '<span class="ofm-pill">' + fmtInt(st.commentCount) + ' comments</span>'
          + '<span class="ofm-pill">avg ' + esc(st.lookDurationAverage || 0) + 's</span></div></div></div></div>';
      })() : '<div class="ofm-empty">No top post for this window.</div>')
      + '</div></div>';
    return out;
  };

  // ============================ PROFILE ==============================
  RENDER.profile = async function () {
    var r = await safe(me());
    if (!r.ok) return errBox('Profile', r);
    var m = r.v;
    var bundles = m.subscriptionBundles || [];
    var out = '<div class="ofm-hero">'
      + (m.header ? '<img class="hdr" src="' + esc(img(m.headerThumbs && m.headerThumbs.w760 || m.header)) + '" alt="">' : '<div class="hdr"></div>')
      + '<div class="who">'
      + (m.avatar ? '<img class="av" src="' + esc(img(m.avatarThumbs && m.avatarThumbs.c144 || m.avatar)) + '" alt="">' : '<div class="av"></div>')
      + '<div><div class="nm">' + esc(m.name || '—') + (m.isVerified ? '<span class="ofm-vfy">✓</span>' : '') + '</div>'
      + '<div class="un">onlyfans.com/' + esc(m.username || '') + '</div></div>'
      + '<div class="rr">' + fmtInt(m.subscribersCount) + ' subs · ' + fmtInt(m.favoritedCount) + ' likes<br>'
      + fmtInt(m.postsCount) + ' posts · ' + fmtInt(m.mediasCount) + ' media</div></div></div>';

    out += '<div class="ofm-row2">'
      + '<div class="ofm-card"><h3>Bio<span class="r">users/me.about</span></h3>'
      + '<div class="ofm-about">' + (m.about ? m.about : '<p style="color:#8a8a8a">No bio set on OnlyFans.</p>') + '</div>'
      + '<div class="ofm-kv">'
      + '<div class="k">Location</div><div class="v">' + esc(m.location || '—') + '</div>'
      + '<div class="k">Website</div><div class="v">' + esc(m.website || '—') + '</div>'
      + '<div class="k">Wishlist</div><div class="v">' + esc(m.wishlist || '—') + '</div>'
      + '<div class="k">Email</div><div class="v">' + esc(m.email || '—') + '</div>'
      + '<div class="k">Joined</div><div class="v">' + esc(day(m.joinDate)) + '</div>'
      + '<div class="k">Last seen</div><div class="v">' + esc(dt(m.lastSeen)) + ' (' + esc(ago(m.lastSeen)) + ')</div>'
      + '</div></div>'
      + '<div class="ofm-card"><h3>Pricing<span class="r">users/me</span></h3>'
      + '<div class="ofm-stats">'
      + stat('Subscription', fmtMoney(m.subscribePrice || 0), m.subscribePrice ? 'per month' : 'free page')
      + stat('Tips', fmtMoney(m.tipsMin) + '–' + fmtMoney(m.tipsMax), m.tipsEnabled ? 'enabled' : 'disabled')
      + stat('PPV range', fmtMoney(m.creditsMin) + '–' + fmtMoney(m.creditsMax))
      + '</div>'
      + (bundles.length ? '<div class="ofm-wrap"><table class="ofm-tbl"><thead><tr><th>Bundle</th>'
        + '<th class="num">Months</th><th class="num">Discount</th><th class="num">Price</th><th>Buyable</th></tr></thead><tbody>'
        + bundles.map(function (b) {
          return '<tr><td class="mut">#' + esc(b.id) + '</td><td class="num">' + esc(b.duration) + '</td>'
            + '<td class="num">' + esc(b.discount) + '%</td><td class="num">' + fmtMoney(b.price) + '</td>'
            + '<td>' + (b.canBuy ? '<span class="ofm-pill ok">yes</span>' : '<span class="ofm-pill">no</span>') + '</td></tr>';
        }).join('') + '</tbody></table></div>'
        : '<div class="ofm-empty" style="margin-top:12px">No subscription bundles configured.</div>')
      + '<div class="ofm-flags">'
      + '<span class="ofm-pill' + (m.canEarn ? ' ok' : ' err') + '">can earn: ' + (m.canEarn ? 'yes' : 'no') + '</span>'
      + '<span class="ofm-pill' + (m.tipsEnabled ? ' ok' : '') + '">tips ' + (m.tipsEnabled ? 'on' : 'off') + '</span>'
      + '<span class="ofm-pill' + (m.canSendChatToAll ? ' ok' : '') + '">mass DM ' + (m.canSendChatToAll ? 'allowed' : 'blocked') + '</span>'
      + '<span class="ofm-pill' + (m.hasStories ? ' ok' : '') + '">stories ' + (m.hasStories ? 'live' : 'none') + '</span>'
      + '</div></div></div>';
    return out;
  };

  // =========================== OF SETTINGS ===========================
  var FLAGS = [
    ['isPrivate', 'Private profile'], ['hideAfterMassMessages', 'Hide after mass messages'],
    ['showPostsTips', 'Show post tips'], ['showSubscribesOffers', 'Show subscribe offers'],
    ['disableSubscribesOffers', 'Subscribe offers disabled'], ['isEmailNotificationsEnabled', 'Email notifications'],
    ['canAcceptMessageOnlyFromFriends', 'DMs from friends only'], ['isAutoFollowBack', 'Auto follow-back'],
    ['commentsOnlyForPayers', 'Comments payers-only'], ['hasPaidPosts', 'Has paid posts'],
    ['isDrmEnabled', 'DRM enabled'], ['showFriendsToSubscribers', 'Show friends to subs'],
    ['isSuggestionsOptOut', 'Opted out of suggestions'], ['muteTagsInChats', 'Mute tags in chats'],
    ['shouldReceiveLessNotifications', 'Fewer notifications'], ['replyOnSubscribe', 'Auto welcome (replyOnSubscribe)']
  ];
  RENDER.settings = async function () {
    var r = await Promise.all([
      safe(Fastt.get('/api/of/v2/users/me/settings')),
      safe(Fastt.get('/api/of/v2/users/settings/chat')),
      safe(Fastt.get('/api/of/v2/users/settings/post')),
      safe(Fastt.get('/api/of/v2/users/settings/notifications/transports'))
    ]);
    var s = r[0].ok ? r[0].v : null;
    var ch = r[1].ok ? r[1].v : null, po = r[2].ok ? r[2].v : null;
    var tr = r[3].ok ? (Array.isArray(r[3].v) ? r[3].v : (r[3].v.list || [])) : null;

    var out = '<div class="ofm-card"><h3>Account settings<span class="r">GET /api/of/v2/users/me/settings</span></h3>'
      + '<div class="sub">Read-only mirror of the switches on OnlyFans itself. '
      + '<b>replyOnSubscribe</b> is the one the Welcome automation reads (an active welcome template is not the same flag).</div>'
      + (s ? '<div class="ofm-flags">' + FLAGS.map(function (f) {
        var on = !!s[f[0]];
        return '<span class="ofm-pill' + (on ? ' ok' : '') + '">' + esc(f[1]) + ': ' + (on ? 'on' : 'off') + '</span>';
      }).join('') + '</div>'
        + '<div class="ofm-kv">'
        + '<div class="k">Blocked countries</div><div class="v">' + esc((s.blockedCountries || []).join(', ') || 'none') + '</div>'
        + '<div class="k">Blocked states</div><div class="v">' + esc((s.blockedStates || []).join(', ') || 'none') + '</div>'
        + '<div class="k">Blocked IPs</div><div class="v">' + esc((s.blockedIps || []).join(', ') || 'none') + '</div>'
        + '<div class="k">Referral reward</div><div class="v">' + esc(s.recommenderReward || '—') + '</div>'
        + '<div class="k">Co-streaming requests</div><div class="v">' + esc(s.coStreamingRequestFrom || '—') + '</div>'
        + '<div class="k">Bundle max price</div><div class="v">' + esc(s.bundleMaxPrice != null ? fmtMoney(s.bundleMaxPrice) : '—') + '</div>'
        + '<div class="k">2FA</div><div class="v">'
        + esc([s.appOtp ? 'app' : '', s.phoneOtp ? 'phone' : '', s.passkeyOtp ? 'passkey' : '', s.faceOtp ? 'face' : ''].filter(Boolean).join(', ') || 'none') + '</div>'
        + '</div>'
        : errBox('Account settings', r[0]))
      + '</div>';

    out += '<div class="ofm-row3">'
      + '<div class="ofm-card"><h3>Chat auto-delete</h3>'
      + (ch ? '<div class="ofm-stats">' + stat('State', ch.isEnabled ? 'On' : 'Off', 'after ' + esc(ch.currentCode) + ' h') + '</div>'
        + '<div class="ofm-flags">' + (ch.options || []).map(function (o) {
          return '<span class="ofm-pill' + (o === ch.currentCode ? ' ok' : '') + '">' + esc(o) + ' h</span>';
        }).join('') + '</div>' : errBox('Chat settings', r[1]))
      + '</div>'
      + '<div class="ofm-card"><h3>Post auto-delete</h3>'
      + (po ? '<div class="ofm-stats">' + stat('State', po.isEnabled ? 'On' : 'Off', 'after ' + esc(po.currentCode) + ' h') + '</div>'
        + '<div class="ofm-flags">' + (po.options || []).map(function (o) {
          return '<span class="ofm-pill' + (o === po.currentCode ? ' ok' : '') + '">' + esc(o) + ' h</span>';
        }).join('') + '</div>' : errBox('Post settings', r[2]))
      + '</div>'
      + '<div class="ofm-card"><h3>Notification transports</h3>'
      + (tr ? '<div class="ofm-flags">' + (tr.length
        ? tr.map(function (t) { return '<span class="ofm-pill ok">' + esc(t) + '</span>'; }).join('')
        : '<span class="ofm-pill">none enabled</span>') + '</div>' : errBox('Transports', r[3]))
      + '</div></div>';
    return out;
  };

  // ── boot ────────────────────────────────────────────────────────────
  var row = Fastt.accountRow();
  $('#ofm-sub').innerHTML = 'live OnlyFans mirror · ' + esc(row ? (row.nickname || row.id) : acct)
    + ' <span class="info"><svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><circle cx="12" cy="12" r="9"/><path d="M12 11v5" stroke-linecap="round"/><circle cx="12" cy="8" r=".9" fill="currentColor" stroke="none"/></svg></span>';
  Fastt.liveBadge($('#ofm-title'));
  // unread badge on the Notifications tab, whichever tab you land on
  safe(Fastt.get('/api/of/v2/users/notifications/count')).then(function (n) {
    if (n.ok && n.v && n.v.all) { tabCounts.notifications = n.v.all; paintTabs(tabCounts); }
  });
  await show((location.hash || '').replace('#', '') || 'home');
});
