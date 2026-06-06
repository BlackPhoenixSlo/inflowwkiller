console.info('[app.js] build chats-scroll-cap-v6 (rt-row-bubble) loaded');
// OF Relay test UI.
//
// One file, no framework. Each "view" (chats, fans, posts, vault, lists,
// queue, promo, money) is a self-contained section in index.html and a
// matching `loaders[name]` here. The view router shows one at a time and
// runs the loader lazily the first time each view is opened.

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);
const escapeHtml = (s) => String(s ?? '')
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
const stripHtml = (s) => String(s ?? '').replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim();
const fmtMoney = (n) => '$' + (Number(n) || 0).toFixed(2);
const fmtDate = (s) => s ? new Date(s).toLocaleString() : '—';

let state = {
  myUserId: null,
  hydrate: true,
  unreadOnly: false,
  selectedChatId: null,
  streamAbort: null,
  userCache: new Map(),
  oldestId: null,
  totalLoaded: 0,
  hasMore: false,
  loaded: new Set(['chats']),  // which views have been initialized
  // Multi-account: which account this tab is currently viewing. Persisted in
  // localStorage so refreshes stay on the same account. null = use server default.
  account: { id: localStorage.getItem('of_account_id') || null,
             nickname: null, color: null },
  accounts: [],   // list from /admin/accounts
};

// ─── Account-aware fetch wrapper ───────────────────────────────
// All relay calls go through this so the X-Account-Id header is added
// automatically. Non-relay calls (full URLs, third-party hosts) pass through
// untouched. Monkey-patches window.fetch in-place because there are ~60 raw
// fetch() call sites across this file — we'd rather inject the header in one
// place than rename every call site.
const _origFetch = window.fetch.bind(window);
window.fetch = (input, init = {}) => {
  try {
    let url = typeof input === 'string' ? input
            : (input && input.url) || '';
    const isRelative = url.startsWith('/');
    const isApi = url.startsWith('/api/of/v2/') || url.startsWith('/admin/')
                || url === '/health' || url.startsWith('/health?')
                || url.startsWith('/openapi.json');
    if (isRelative && isApi && state.account && state.account.id) {
      const headers = new Headers((init && init.headers) || {});
      if (!headers.has('X-Account-Id')) headers.set('X-Account-Id', state.account.id);
      init = { ...init, headers };
    }
  } catch (e) { /* fall through with original args */ }
  return _origFetch(input, init);
};

// ─── Account switcher ─────────────────────────────────────────
async function refreshAccounts({ initial = false } = {}) {
  let data;
  try {
    const r = await _origFetch('/admin/accounts');  // no X-Account-Id needed for /admin/accounts
    if (!r.ok) throw new Error(r.status);
    data = await r.json();
  } catch (e) {
    return;
  }
  state.accounts = data.accounts || [];
  const activeServer = data.active_account_id;
  // On first load, prefer the localStorage selection. Otherwise fall back to
  // the server's notion of active. If even that is null, pick the newest with
  // a session, which mirrors the server-side fallback.
  if (initial) {
    const persisted = state.account.id;
    const valid = persisted && state.accounts.some(a => a.id === persisted);
    if (!valid) {
      state.account.id = activeServer
        || (state.accounts.find(a => a.has_session) || {}).id
        || null;
      if (state.account.id) localStorage.setItem('of_account_id', state.account.id);
    }
  }
  const cur = state.accounts.find(a => a.id === state.account.id) || null;
  if (cur) {
    state.account.nickname = cur.nickname;
    state.account.color = cur.color;
  }
  renderAccountSwitcher(activeServer);
}

function renderAccountSwitcher(activeServerId) {
  const dot = document.getElementById('account-dot');
  const label = document.getElementById('account-label');
  if (!dot || !label) return;
  if (!state.accounts.length) {
    dot.style.background = '#888';
    label.textContent = 'no accounts';
    return;
  }
  const cur = state.accounts.find(a => a.id === state.account.id) || state.accounts[0];
  dot.style.background = cur.color || '#888';
  label.textContent = cur.nickname || cur.id;
  const list = document.getElementById('account-menu-list');
  if (!list) return;
  list.innerHTML = state.accounts.map(a => `
    <div class="acc-row ${a.id === state.account.id ? 'active' : ''}" data-account-pick="${a.id}">
      <span class="dot" style="background:${a.color || '#888'}"></span>
      <span class="nick">${escapeHtml(a.nickname || a.id)}</span>
      <span class="uid">#${a.id}${a.has_session ? '' : ' · ⚠'}${a.id === activeServerId ? ' · default' : ''}</span>
    </div>
  `).join('');
  list.querySelectorAll('[data-account-pick]').forEach(row => {
    row.onclick = async (e) => {
      e.stopPropagation();
      const id = row.dataset.accountPick;
      if (id === state.account.id) { closeAccountMenu(); return; }
      await switchAccount(id);
    };
  });
}

function openAccountMenu() {
  const menu = document.getElementById('account-menu');
  if (menu) menu.style.display = 'block';
}
function closeAccountMenu() {
  const menu = document.getElementById('account-menu');
  if (menu) menu.style.display = 'none';
}

async function switchAccount(account_id) {
  state.account.id = account_id;
  localStorage.setItem('of_account_id', account_id);
  closeAccountMenu();
  // Hard reload: easiest correct way to flush every per-account-cached piece
  // of state (selected chat, message stream, vault page, user cache, etc.).
  // We could surgically reset state, but a reload also re-mounts the event
  // bus WebSocket with the new account_id filter, which is the right default.
  location.reload();
}

// Hook switcher click handlers (deferred until DOM ready below)
document.addEventListener('DOMContentLoaded', () => {
  const pill = document.getElementById('account-switcher');
  if (!pill) return;
  pill.addEventListener('click', (e) => {
    if (e.target && e.target.closest('[data-account-action="manage"]')) {
      e.preventDefault();
      closeAccountMenu();
      // Programmatically open the Setup view
      const nav = document.querySelector('#nav-strip a[data-view="setup"]');
      if (nav) nav.click();
      return;
    }
    if (e.target && e.target.closest('[data-account-pick]')) return; // row handler
    const menu = document.getElementById('account-menu');
    if (!menu) return;
    menu.style.display = menu.style.display === 'block' ? 'none' : 'block';
  });
  document.addEventListener('click', (e) => {
    if (!e.target.closest('#account-switcher')) closeAccountMenu();
  });
});

// ─── Header pills ──────────────────────────────────────────────
async function refreshHealth() {
  const pill = $('#health-pill');
  try {
    const r = await fetch('/health');
    const j = await r.json();
    if (j.ok) {
      state.myUserId = j.user_id;
      pill.className = 'pill ok';
      pill.textContent = `ok · ${j.name} (#${j.user_id})`;
    } else {
      pill.className = 'pill err';
      pill.textContent = `${j.error || 'down'}`;
    }
  } catch (e) {
    pill.className = 'pill err';
    pill.textContent = 'unreachable';
  }
}

async function refreshStats() {
  const set = (id, text, hide = false) => {
    const el = $(id);
    el.textContent = text;
    el.style.display = hide ? 'none' : '';
  };
  await Promise.allSettled([
    (async () => {
      const r = await fetch('/api/of/v2/subscriptions/count');
      if (!r.ok) return set('#stats-subs', '', true);
      const d = await r.json();
      const subs = d.subscriptions || {};
      set('#stats-subs', `${subs.active ?? 0} active · ${subs.expired ?? 0} expired`);
    })(),
    (async () => {
      const r = await fetch('/api/of/v2/chats?filter=unread&limit=50');
      if (!r.ok) return set('#stats-unread', '', true);
      const d = await r.json();
      const chats = d.list || [];
      set('#stats-unread', `${chats.length}${d.hasMore ? '+' : ''} unread`);
    })(),
    (async () => {
      const r = await fetch('/api/of/v2/messages/queue?limit=50');
      if (!r.ok) return set('#stats-scheduled', '', true);
      const d = await r.json();
      const items = Array.isArray(d) ? d : (d.list || []);
      set('#stats-scheduled', `${items.length} scheduled`);
    })(),
  ]);
}

// ─── Realtime event stream ────────────────────────────────────
// One global WebSocket to /ws/events. Every OF push event arrives here.
// We hand them off to per-handler callbacks (chat list refresh, badge
// flash, notification toast, …) instead of touching the DOM directly.
state.eventBus = (() => {
  const handlers = new Set();
  let ws = null;
  let backoff = 1000;
  let livePill = null;
  const setLive = (on, text) => {
    if (!livePill) {
      livePill = document.createElement('span');
      livePill.className = 'pill';
      livePill.style.cursor = 'default';
      livePill.title = 'OF realtime event stream — auto-reconnect';
      const header = document.querySelector('header');
      if (header) header.insertBefore(livePill, header.querySelector('.grow'));
    }
    livePill.className = `pill ${on ? 'ok' : 'warn'}`;
    livePill.textContent = text;
  };
  const connect = () => {
    setLive(false, 'live: connecting…');
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    ws = new WebSocket(`${proto}//${location.host}/ws/events`);
    ws.onopen = () => { backoff = 1000; setLive(true, 'live ●'); };
    ws.onmessage = (e) => {
      let event;
      try { event = JSON.parse(e.data); } catch { return; }
      if (event && event.__ready) return;          // handshake ack
      for (const h of handlers) {
        try { h(event); } catch (err) { console.error('event handler', err); }
      }
    };
    ws.onclose = () => {
      setLive(false, `live: reconnecting in ${Math.round(backoff/1000)}s`);
      setTimeout(connect, backoff);
      backoff = Math.min(backoff * 2, 30000);
    };
    ws.onerror = () => { /* onclose will handle reconnect */ };
  };
  connect();
  return {
    on: (fn) => { handlers.add(fn); return () => handlers.delete(fn); },
    off: (fn) => handlers.delete(fn),
  };
})();

// Realtime sidebar update: when a new chat message lands, patch the existing
// row in place (preview text + unread badge + move-to-top) instead of
// refetching. Refetching would reset pagination to offset=0 and walk up to
// the cap on every burst of inbound messages.
//
// If the fan isn't in the currently-loaded slice, we deliberately no-op —
// the row will appear next time the user reloads or pages further.
function _patchChatRowFromEvent(fromId, m, { bumpUnread } = {}) {
  const root = $('#chats');
  const row = root ? root.querySelector(`.chat-row[data-chat-id="${fromId}"]`) : null;
  if (!row) return;
  const preview = row.querySelector('.last');
  if (preview) {
    const txt = stripHtml(m.text || '').slice(0, 60);
    preview.innerHTML = txt ? escapeHtml(txt) : '<em>(no text)</em>';
  }
  if (bumpUnread) {
    let badge = row.querySelector('.badge');
    if (badge) {
      const n = parseInt(badge.textContent, 10) || 0;
      badge.textContent = String(n + 1);
    } else {
      badge = document.createElement('span');
      badge.className = 'badge';
      badge.textContent = '1';
      row.appendChild(badge);
    }
  }
  if (row.parentNode === root && root.firstChild !== row) {
    root.insertBefore(row, root.firstChild);
  }
}

// Wire default handlers: counter updates + chat list refresh on new messages
state.eventBus.on((event) => {
  if (!event || typeof event !== 'object') return;
  // OF pushes counter snapshots in their own envelope
  if ('chat_messages' in event) {
    const el = $('#stats-unread');
    if (el) {
      el.textContent = `${event.chat_messages} unread`;
      el.style.background = 'rgba(255,28,247,.20)';
      setTimeout(() => { el.style.background = ''; }, 1500);
    }
  }
  // Push notifications on new chat messages — flash the row in the sidebar.
  if (event.api2_chat_message) {
    const m = event.api2_chat_message;
    const fromId = m.fromUser?.id;
    if (fromId) {
      const row = document.querySelector(`[data-chat-id="${fromId}"]`);
      if (row) {
        row.style.transition = 'background .25s';
        row.style.background = 'rgba(255,28,247,.18)';
        setTimeout(() => { row.style.background = ''; }, 2000);
      }
      const isOpenChat = state.selectedChatId && Number(state.selectedChatId) === Number(fromId);
      // If the currently-open chat is this fan, prepend the message live
      if (isOpenChat && typeof renderMessage === 'function') {
        try { renderMessage(m, { live: true }); } catch (e) { console.warn(e); }
      }
      // Always update the sidebar row in place — no refetch. Bump unread
      // only when the user isn't already looking at this chat.
      _patchChatRowFromEvent(fromId, m, { bumpUnread: !isOpenChat });
    }
  }
  // Scheduled badge live
  if (event.api2_post_publish || event.post_expire) {
    refreshStats();
  }
});

// ─── View router ───────────────────────────────────────────────
async function showView(name) {
  $$('#nav-strip a').forEach(a => a.classList.toggle('active', a.dataset.view === name));
  $$('.view').forEach(v => { v.hidden = v.dataset.view !== name; });
  if (!state.loaded.has(name) && loaders[name]) {
    state.loaded.add(name);
    try { await loaders[name](); } catch (e) {
      console.error(`loader ${name} failed:`, e);
    }
  }
}
$$('#nav-strip a').forEach(a => a.addEventListener('click', (e) => {
  e.preventDefault();
  showView(a.dataset.view);
}));

// Helpers for the data views
async function fetchJson(path, opts) {
  const r = await fetch(path, opts);
  const ok = r.ok;
  const ct = r.headers.get('content-type') || '';
  const body = ct.includes('application/json') ? await r.json().catch(() => null) : await r.text();
  return { ok, status: r.status, body };
}

function renderEmpty(host, message) {
  host.innerHTML = `<div class="empty">${escapeHtml(message)}</div>`;
}

function renderError(host, err) {
  host.innerHTML = `<div class="empty" style="color:var(--err)">${escapeHtml(typeof err === 'string' ? err : JSON.stringify(err))}</div>`;
}

// ─── Loaders for each non-chat view ─────────────────────────────
const loaders = {};

// ── HOME ──
loaders.home = async () => {
  const reload = async () => {
    const host = $('#home-body');
    host.innerHTML = '<div class="empty">loading…</div>';
    const [me, counts, onthisday, recent, notifs] = await Promise.all([
      fetchJson('/api/of/v2/users/me'),
      fetchJson('/api/of/v2/subscriptions/count/all'),
      fetchJson('/api/of/v2/users/posts/on-this-day'),
      fetchJson('/api/of/v2/posts?limit=3'),
      fetchJson('/api/of/v2/users/notifications/count'),
    ]);
    const u = me.ok ? me.body : {};
    const cnt = counts.ok ? counts.body : {};
    const subs = cnt.subscriptions || {};
    const ot = onthisday.ok ? (onthisday.body || []) : [];
    const recentList = recent.ok ? (Array.isArray(recent.body) ? recent.body : (recent.body.list || [])) : [];
    const nc = notifs.ok ? notifs.body : {};
    host.innerHTML = `
      <div class="stat-cards">
        <div class="stat-card"><div class="label">My posts</div><div class="value">${u.postsCount ?? '?'}</div></div>
        <div class="stat-card"><div class="label">Followers</div><div class="value">${u.favoritedCount ?? 0}</div></div>
        <div class="stat-card"><div class="label">Active fans</div><div class="value">${subs.active ?? 0}</div></div>
        <div class="stat-card"><div class="label">Expired fans</div><div class="value">${subs.expired ?? 0}</div></div>
        <div class="stat-card"><div class="label">Unread notifs</div><div class="value">${nc.all ?? 0}</div></div>
        <div class="stat-card"><div class="label">Chat messages</div><div class="value">${u.chatMessagesCount ?? 0}</div></div>
      </div>
      <h3>On this day</h3>
      ${ot.length ? `<table class="data"><thead><tr><th>posted</th><th>text</th></tr></thead><tbody>${ot.map(p=>`<tr><td>${fmtDate(p.postedAt)}</td><td>${escapeHtml(stripHtml(p.text).slice(0,120))}</td></tr>`).join('')}</tbody></table>` : '<div class="empty">no past posts on this date</div>'}
      <h3 style="margin-top:24px">Recent posts</h3>
      ${recentList.length ? recentList.map(p=>`<div class="post-row"><div style="flex:1"><div>${escapeHtml(stripHtml(p.text).slice(0,200))}</div><div class="meta-line">id ${p.id} · ${fmtDate(p.postedAt)} · ❤ ${p.favoritesCount||0} · 💬 ${p.commentsCount||0}</div></div></div>`).join('') : '<div class="empty">no posts</div>'}
    `;
  };
  $('#home-reload').onclick = reload;
  await reload();
};

// ── NOTIFICATIONS ──
loaders.notifs = async () => {
  const TYPES = [
    { id: 'all',         label: 'All' },
    { id: 'subscribed',  label: 'Subscriptions' },
    { id: 'tip',         label: 'Tips' },
    { id: 'purchases',   label: 'Purchases' },
    { id: 'comments',    label: 'Comments' },
    { id: 'mentions',    label: 'Mentions' },
  ];
  let activeTab = 'all';
  const tabsEl = $('#notifs-tabs');
  tabsEl.innerHTML = TYPES.map(t => `<button data-tab="${t.id}" ${t.id===activeTab?'class="on"':''}>${t.label}</button>`).join('');
  tabsEl.querySelectorAll('button').forEach(b => b.onclick = () => { activeTab = b.dataset.tab; tabsEl.querySelectorAll('button').forEach(x=>x.classList.toggle('on', x===b)); reload(); });

  const reload = async () => {
    const host = $('#notifs-body');
    host.innerHTML = '<div class="empty">loading…</div>';
    const [items, counts] = await Promise.all([
      fetchJson(`/api/of/v2/users/notifications?limit=30${activeTab==='all'?'':'&type='+activeTab}`),
      fetchJson('/api/of/v2/users/notifications/count'),
    ]);
    if (!items.ok) { renderError(host, items.body); return; }
    const list = Array.isArray(items.body) ? items.body : (items.body.list || []);
    $('#notifs-meta').textContent = counts.ok ? `${counts.body.all || 0} unread` : '';
    // Update the nav badge
    if (counts.ok && counts.body.all) {
      const b = $('#nav-notif-count');
      b.textContent = counts.body.all > 99 ? '99+' : counts.body.all;
      b.style.display = '';
    }
    if (!list.length) { renderEmpty(host, `no ${activeTab} notifications`); return; }
    host.innerHTML = list.map(n => {
      const u = n.user || {};
      const avatar = u.avatar || u.avatarThumbs?.c50;
      const initial = (u.name || u.username || '?')[0].toUpperCase();
      return `<div class="post-row">
        <div class="avatar" style="width:40px;height:40px${avatar?`;background-image:url('${avatar}')`:''}">${avatar?'':escapeHtml(initial)}</div>
        <div style="flex:1;min-width:0">
          <div><strong>${escapeHtml(u.name || u.username || `#${u.id||'?'}`)}</strong> <span style="color:var(--fg-dim)">${escapeHtml(n.type || '')}</span></div>
          <div class="meta-line">${escapeHtml(stripHtml(n.text||'').slice(0,200))}</div>
          <div class="meta-line">${fmtDate(n.createdAt)} · id ${n.id}</div>
        </div>
      </div>`;
    }).join('');
  };
  $('#notifs-reload').onclick = reload;
  await reload();
};

// ── COLLECTIONS (4 tabs: User lists / Fans / Bookmarks / Labels) ──
loaders.collections = async () => {
  let activeTab = 'lists';
  const tabsEl = $('#coll-tabs');
  tabsEl.querySelectorAll('button').forEach(b => b.onclick = () => {
    activeTab = b.dataset.tab;
    tabsEl.querySelectorAll('button').forEach(x => x.classList.toggle('on', x === b));
    reload();
  });

  const renderLists = async (host) => {
    const r = await fetchJson('/api/of/v2/lists?limit=50');
    if (!r.ok) return renderError(host, r.body);
    const lists = Array.isArray(r.body) ? r.body : (r.body.list || []);
    $('#coll-meta').textContent = `${lists.length} lists`;
    host.innerHTML = `
      <div style="display:flex;gap:8px;margin-bottom:12px;align-items:center">
        <input id="new-list-name" placeholder="new list name…" style="flex:1;background:var(--bg);color:var(--fg);border:1px solid var(--border);border-radius:8px;padding:6px 10px;font:inherit">
        <button id="new-list-fire" class="toolbar-btn" style="background:var(--accent);color:#000;font-weight:700;border-color:var(--accent)">+ Create list</button>
      </div>
      <table class="data">
        <thead><tr><th>name</th><th>type</th><th class="num">users</th><th class="num">posts</th><th style="min-width:240px"></th></tr></thead>
        <tbody>${lists.map(l => `<tr data-list-row="${l.id}">
          <td><span class="list-name" data-id="${escapeHtml(String(l.id))}"><strong>${escapeHtml(l.name)}</strong></span> <small style="color:var(--fg-dim)">${escapeHtml(String(l.id))}</small></td>
          <td>${escapeHtml(l.type || '—')}</td>
          <td class="num">${l.usersCount ?? 0}</td>
          <td class="num">${l.postsCount ?? 0}</td>
          <td style="text-align:right">
            <button class="toolbar-btn" data-act="view" data-id="${escapeHtml(String(l.id))}" data-name="${escapeHtml(l.name)}">👥 users</button>
            <button class="toolbar-btn" data-act="addfan" data-id="${escapeHtml(String(l.id))}" data-name="${escapeHtml(l.name)}">+ fan</button>
            ${l.canPinToChat !== false ? `
              <button class="toolbar-btn" data-act="pin-chat" data-id="${escapeHtml(String(l.id))}" data-pinned="${l.isPinnedToChat ? '1' : '0'}" title="${l.isPinnedToChat ? 'Unpin from chat sidebar' : 'Pin as chat-sidebar folder'}">${l.isPinnedToChat ? '📌 pinned' : '📌 pin chat'}</button>
            ` : ''}
            ${l.type === 'custom' ? `
              <button class="toolbar-btn" data-act="rename" data-id="${escapeHtml(String(l.id))}" data-name="${escapeHtml(l.name)}">✎</button>
              <button class="toolbar-btn" data-act="delete" data-id="${escapeHtml(String(l.id))}" data-name="${escapeHtml(l.name)}" style="color:var(--err);border-color:rgba(248,81,73,.3)">🗑</button>
            ` : `<small style="color:var(--fg-dim)">built-in</small>`}
          </td>
        </tr>`).join('')}</tbody>
      </table>
      <div id="list-users-pane" style="margin-top:18px"></div>`;

    // Create
    $('#new-list-fire').onclick = async () => {
      const name = $('#new-list-name').value.trim();
      if (!name) { alert('name required'); return; }
      const r = await fetch('/api/of/v2/lists', {
        method: 'POST', headers: {'content-type':'application/json'},
        body: JSON.stringify({ name }),
      });
      if (!r.ok) { alert(`create failed: ${r.status} ${(await r.text()).slice(0,200)}`); return; }
      $('#new-list-name').value = '';
      await renderLists(host);   // refresh
    };

    const renderListUsers = async (id, name) => {
      const pane = $('#list-users-pane');
      pane.innerHTML = `<h3>👥 ${escapeHtml(name)} (${escapeHtml(id)}) — users</h3><div class="empty">loading…</div>`;
      const r = await fetchJson(`/api/of/v2/lists/${encodeURIComponent(id)}/users?limit=50`);
      if (!r.ok) { renderError(pane, r.body); return; }
      const users = Array.isArray(r.body) ? r.body : (r.body.list || []);
      pane.innerHTML = `
        <h3 style="margin-bottom:8px">👥 ${escapeHtml(name)} <small style="color:var(--fg-dim);font-weight:400">(${users.length} users)</small></h3>
        ${users.length ? `<table class="data">
          <thead><tr><th></th><th>name</th><th>last seen</th><th></th></tr></thead>
          <tbody>${users.map(u => {
            const avatar = u.avatar || u.avatarThumbs?.c50;
            const uname = u.name || u.username || `#${u.id}`;
            return `<tr>
              <td><div class="avatar" style="width:32px;height:32px${avatar?`;background-image:url('${avatar}')`:''}">${avatar?'':escapeHtml(uname[0]||'?').toUpperCase()}</div></td>
              <td><strong>${escapeHtml(uname)}</strong><br><small style="color:var(--fg-dim)">@${escapeHtml(u.username||'')} · #${u.id}</small></td>
              <td>${fmtDate(u.lastSeen)}</td>
              <td><button class="toolbar-btn" data-rm-uid="${u.id}" style="color:var(--err);border-color:rgba(248,81,73,.3)">remove</button></td>
            </tr>`;
          }).join('')}</tbody>
        </table>` : '<div class="empty">no users in this list</div>'}`;

      pane.querySelectorAll('button[data-rm-uid]').forEach(rb => {
        rb.onclick = async () => {
          const uid = rb.dataset.rmUid;
          if (!confirm(`Remove user #${uid} from "${name}"?`)) return;
          const dr = await fetch(`/api/of/v2/lists/${encodeURIComponent(id)}/users/${uid}`, { method: 'DELETE' });
          if (!dr.ok) { alert(`remove failed: ${dr.status} ${(await dr.text()).slice(0,200)}`); return; }
          renderListUsers(id, name);
        };
      });
    };

    // All actions (view / addfan / rename / delete)
    host.querySelectorAll('button[data-act]').forEach(b => {
      b.onclick = async () => {
        const id = b.dataset.id;
        const name = b.dataset.name;
        if (b.dataset.act === 'view') {
          await renderListUsers(id, name);
        } else if (b.dataset.act === 'addfan') {
          const uid = prompt(`Add fan to "${name}".\nEnter the fan's user id (e.g. 117183):`);
          if (!uid || !/^\d+$/.test(uid.trim())) return;
          const r = await fetch(`/api/of/v2/lists/${encodeURIComponent(id)}/users/${uid.trim()}`, { method: 'POST' });
          if (!r.ok) { alert(`add failed: ${r.status} ${(await r.text()).slice(0,200)}`); return; }
          await renderLists(host);
          await renderListUsers(id, name);
        } else if (b.dataset.act === 'rename') {
          const newName = prompt(`Rename list "${name}" to:`, name);
          if (!newName || newName === name) return;
          const r = await fetch(`/api/of/v2/lists/${encodeURIComponent(id)}`, {
            method: 'PATCH', headers: {'content-type':'application/json'},
            body: JSON.stringify({ name: newName }),
          });
          if (!r.ok) { alert(`rename failed: ${r.status} ${(await r.text()).slice(0,200)}`); return; }
          await renderLists(host);
        } else if (b.dataset.act === 'delete') {
          if (!confirm(`DELETE list "${name}"?\nThis removes the list entirely (fans are not deleted).`)) return;
          const r = await fetch(`/api/of/v2/lists/${encodeURIComponent(id)}`, { method: 'DELETE' });
          if (!r.ok) { alert(`delete failed: ${r.status} ${(await r.text()).slice(0,200)}`); return; }
          await renderLists(host);
        } else if (b.dataset.act === 'pin-chat') {
          const wasPinned = b.dataset.pinned === '1';
          const r = await fetch(`/api/of/v2/lists/${encodeURIComponent(id)}/pin-chat`, {
            method: 'PATCH', headers: {'content-type':'application/json'},
            body: JSON.stringify({ pinned: !wasPinned }),
          });
          if (!r.ok) { alert(`pin-chat failed: ${r.status} ${(await r.text()).slice(0,200)}`); return; }
          // Refresh both the lists table AND the chat-sidebar chips so the
          // newly-pinned folder appears immediately.
          await renderLists(host);
          if (typeof refreshChatFolders === 'function') refreshChatFolders();
        }
      };
    });
  };

  const renderFans = async (host) => {
    const types = ['active','expired','all','muted','recent'];
    const sel = host.querySelector('#fan-type-sel')?.value || 'expired';
    host.innerHTML = `<select id="fan-type-sel" class="toolbar-btn">${types.map(t=>`<option ${t===sel?'selected':''}>${t}</option>`).join('')}</select>
      <div id="fan-body" style="margin-top:12px"><div class="empty">loading…</div></div>`;
    host.querySelector('#fan-type-sel').onchange = () => renderFans(host);
    const r = await fetchJson(`/api/of/v2/subscribers?type=${sel}&limit=30`);
    const body = host.querySelector('#fan-body');
    if (!r.ok) return renderError(body, r.body);
    const fans = Array.isArray(r.body) ? r.body : (r.body.list || []);
    $('#coll-meta').textContent = `${fans.length} fans · ${sel}`;
    if (!fans.length) { renderEmpty(body, `no ${sel} fans`); return; }
    body.innerHTML = `<table class="data">
      <thead><tr><th></th><th>name</th><th>last seen</th><th>expires</th></tr></thead>
      <tbody>${fans.map(u=>{
        const avatar = u.avatar || u.avatarThumbs?.c50;
        const name = u.name || u.username || `#${u.id}`;
        return `<tr>
          <td><div class="avatar" style="width:32px;height:32px${avatar?`;background-image:url('${avatar}')`:''}">${avatar?'':escapeHtml(name[0]||'?').toUpperCase()}</div></td>
          <td><strong>${escapeHtml(name)}</strong><br><small style="color:var(--fg-dim)">@${escapeHtml(u.username||'')} · #${u.id}</small></td>
          <td>${fmtDate(u.lastSeen)}</td>
          <td>${u.subscribedByData?.expiredAt ? fmtDate(u.subscribedByData.expiredAt) : '—'}</td>
        </tr>`;
      }).join('')}</tbody></table>`;
  };

  const renderBookmarks = async (host) => {
    // OF's bookmarks page has TWO sources:
    //   /posts/bookmarks/all?format=infinite&skip_users=all  → the master post list
    //   /posts/bookmarks/categories                          → user-defined folders
    // The legacy /posts/bookmarks endpoint also exists but returns the same
    // post list (no categories). We render both so an "empty bookmarks but
    // I have categories" account is still informative.
    const [postsR, catsR] = await Promise.all([
      fetchJson('/api/of/v2/posts/bookmarks/all?limit=30'),
      fetchJson('/api/of/v2/posts/bookmarks/categories?limit=30'),
    ]);
    if (!postsR.ok) return renderError(host, postsR.body);
    const posts = Array.isArray(postsR.body) ? postsR.body : (postsR.body.list || []);
    const cats  = catsR.ok ? (catsR.body.list || []) : [];
    $('#coll-meta').textContent = `${posts.length} bookmarked · ${cats.length} folder${cats.length===1?'':'s'}`;

    let html = '';
    // Folders strip first
    if (cats.length) {
      html += `<h3 style="margin:0 0 8px">📁 Folders</h3>
        <table class="data" style="margin-bottom:18px">
          <thead><tr><th>name</th><th class="num">posts</th><th>last activity</th></tr></thead>
          <tbody>${cats.map(c => `<tr>
            <td><strong>${escapeHtml(c.name || '?')}</strong> <small style="color:var(--fg-dim)">id ${c.id}</small></td>
            <td class="num">${c.postCount ?? c.posts?.length ?? 0}</td>
            <td>${fmtDate(c.lastActivity)}</td>
          </tr>`).join('')}</tbody>
        </table>`;
    }

    // All-bookmarks list
    if (posts.length) {
      html += `<h3 style="margin:0 0 8px">📑 All bookmarked posts</h3>` +
        posts.map(p => `<div class="post-row"><div style="flex:1">
          <div>${escapeHtml(stripHtml(p.text || '').slice(0, 200))}</div>
          <div class="meta-line">id ${p.id} · ${fmtDate(p.postedAt)} · ❤ ${p.favoritesCount || 0} · 💬 ${p.commentsCount || 0}${p.price ? ' · $' + p.price : ''}</div>
        </div></div>`).join('');
    } else {
      html += `<div class="empty" style="padding:24px;background:var(--bg-elev);border:1px solid var(--border);border-radius:8px">
        <strong>No bookmarked posts.</strong>
        <div style="color:var(--fg-dim);margin-top:6px;font-size:12px">
          ${cats.length ? `You have ${cats.length} bookmark folder${cats.length===1?'':'s'} but ${cats.every(c=>!c.postCount)?'they are all empty':'none of them appear in the "all" list'}.` : 'Visit any OF post and tap the bookmark icon to add some.'}
        </div>
      </div>`;
    }
    host.innerHTML = html;
  };

  const renderLabels = async (host) => {
    const r = await fetchJson('/api/of/v2/labels');
    if (!r.ok) return renderError(host, r.body);
    const labels = r.body.list || [];
    $('#coll-meta').textContent = `${labels.length} labels`;
    host.innerHTML = `<table class="data"><thead><tr><th>name</th><th>type</th><th class="num">posts</th></tr></thead><tbody>${
      labels.map(l=>`<tr><td><strong>${escapeHtml(l.name)}</strong> <small style="color:var(--fg-dim)">${escapeHtml(String(l.id))}</small></td><td>${escapeHtml(l.type||'—')}</td><td class="num">${l.postsCount??0}</td></tr>`).join('')
    }</tbody></table>`;
  };

  const reload = async () => {
    const host = $('#coll-body');
    host.innerHTML = '<div class="empty">loading…</div>';
    if (activeTab === 'lists') return renderLists(host);
    if (activeTab === 'fans') return renderFans(host);
    if (activeTab === 'bookmarks') return renderBookmarks(host);
    if (activeTab === 'labels') return renderLabels(host);
  };
  $('#coll-reload').onclick = reload;
  await reload();
};

// ── STATEMENTS (Earnings / Payouts / Stats / Promos tabs) ──
loaders.statements = async () => {
  let activeTab = 'earnings';
  const tabsEl = $('#stmts-tabs');
  tabsEl.querySelectorAll('button').forEach(b => b.onclick = () => {
    activeTab = b.dataset.tab;
    tabsEl.querySelectorAll('button').forEach(x => x.classList.toggle('on', x === b));
    reload();
  });

  // Balance pill at the top
  const refreshBalance = async () => {
    const r = await fetchJson('/api/of/v2/payouts/balances');
    if (r.ok) {
      const b = r.body;
      $('#stmts-balance').textContent = `bal ${fmtMoney(b.payoutAvailable)} · pending ${fmtMoney(b.payoutPending)}`;
    }
  };

  const renderEarnings = async (host) => {
    const r = await fetchJson('/api/of/v2/payouts/transactions?limit=30');
    if (!r.ok) return renderError(host, r.body);
    const list = (r.body.list || []);
    host.innerHTML = `<table class="data">
      <thead><tr><th>date</th><th>type</th><th class="num">gross</th><th class="num">fee</th><th class="num">net</th><th>description</th></tr></thead>
      <tbody>${list.map(t=>`<tr>
        <td>${fmtDate(t.date || t.createdAt)}</td>
        <td>${escapeHtml(t.type || '—')}</td>
        <td class="num">${fmtMoney(t.amount)}</td>
        <td class="num">${fmtMoney((t.amount||0)-(t.net||0))}</td>
        <td class="num">${fmtMoney(t.net)}</td>
        <td>${escapeHtml((t.description||'').slice(0,80))}</td>
      </tr>`).join('')}</tbody></table>`;
  };

  const renderPayoutRequests = async (host) => {
    const r = await fetchJson('/api/of/v2/payouts/requests?limit=30');
    if (!r.ok) return renderError(host, r.body);
    const list = r.body.list || [];
    if (!list.length) return renderEmpty(host, 'no payout requests');
    host.innerHTML = `<table class="data">
      <thead><tr><th>date</th><th class="num">amount</th><th>status</th><th>method</th></tr></thead>
      <tbody>${list.map(p=>`<tr>
        <td>${fmtDate(p.date || p.createdAt)}</td>
        <td class="num">${fmtMoney(p.amount)}</td>
        <td>${escapeHtml(p.status || '—')}</td>
        <td>${escapeHtml(p.payoutType || '—')}</td>
      </tr>`).join('')}</tbody></table>`;
  };

  const renderStats = async (host) => {
    const r = await fetchJson('/api/of/v2/payouts/stats?by=day');
    if (!r.ok) return renderError(host, r.body);
    const months = r.body.list?.months || {};
    if (!Object.keys(months).length) return renderEmpty(host, 'no stats');
    // Summarize per type across all months
    const byType = {};
    let totalDays = 0;
    for (const [mts, data] of Object.entries(months)) {
      for (const [type, days] of Object.entries(data)) {
        for (const d of days) {
          byType[type] = (byType[type] || 0) + (d.net || 0);
          totalDays++;
        }
      }
    }
    host.innerHTML = `<div class="stat-cards">
      ${Object.entries(byType).sort((a,b)=>b[1]-a[1]).map(([t,v])=>`<div class="stat-card"><div class="label">${escapeHtml(t)}</div><div class="value" style="font-size:18px">${fmtMoney(v)}</div></div>`).join('')}
    </div><div class="empty">${totalDays} data points across ${Object.keys(months).length} month(s)</div>`;
  };

  const renderPromos = async (host) => {
    const [promos, trials] = await Promise.all([
      fetchJson('/api/of/v2/promotions'),
      fetchJson('/api/of/v2/promotions/trial'),
    ]);
    const section = (title, resp) => {
      if (!resp.ok) return `<h3>${title}</h3>${renderError({innerHTML:''}, resp.body) || ''}<div class="empty" style="color:var(--err)">error</div>`;
      const items = resp.body.items || resp.body.list || (Array.isArray(resp.body) ? resp.body : []);
      if (!items.length) return `<h3>${title}</h3><div class="empty">none</div>`;
      return `<h3>${title}</h3><table class="data">
        <thead><tr><th>id</th><th>type</th><th class="num">price</th><th class="num">claims</th><th>created</th><th>finishes</th><th>status</th></tr></thead>
        <tbody>${items.map(p=>`<tr>
          <td>${p.id}</td><td>${escapeHtml(p.type || '—')}</td>
          <td class="num">${p.price !== undefined ? fmtMoney(p.price) : '—'}</td>
          <td class="num">${p.claimsCount ?? 0}/${p.subscribeCounts ?? '?'}</td>
          <td>${fmtDate(p.createdAt)}</td><td>${fmtDate(p.finishedAt)}</td>
          <td>${p.isFinished ? '✓ done' : '⏳ active'}</td>
        </tr>`).join('')}</tbody></table>`;
    };
    host.innerHTML = section('Promotions', promos) + '<br>' + section('Trials', trials);
  };

  const reload = async () => {
    const host = $('#stmts-body');
    host.innerHTML = '<div class="empty">loading…</div>';
    await refreshBalance();
    if (activeTab === 'earnings')   return renderEarnings(host);
    if (activeTab === 'payouts')    return renderPayoutRequests(host);
    if (activeTab === 'stats')      return renderStats(host);
    if (activeTab === 'promotions') return renderPromos(host);
  };
  $('#stmts-reload').onclick = reload;
  await reload();
};

// ── SANDBOX ─────────────────────────────────────────────────────
// Lists every /api/of/v2 endpoint. Per row:
//   - GETs with no path params: instant [Run]
//   - GETs with path params:    expandable inputs + [Send]
//   - Writes:                   expandable path inputs + JSON body editor + [Send]
//                               (only when the writes-lock is open)
//
// Smart defaults pre-fill path-param inputs. Body templates are derived from
// the OpenAPI requestBody schema so writes have a starter JSON they can edit.
loaders.sandbox = (() => {
  const SMART_DEFAULTS = {
    chat_id: '117183', message_id: '9667600999356', post_id: '2476090757',
    user_id: '117183', user_id_or_username: 'johnnysins', list_id: 'fans',
    label_id: '5920737', queue_id: '1',
    ids: '117183', q: 'scarlet', type: 'all', limit: '5', offset: '0',
    publish_date: new Date().toISOString().slice(0, 10),
    publish_date_end: new Date().toISOString().slice(0, 10),
    time_zone: 'Europe/Ljubljana', view: 'main', by: 'day',
  };

  // OpenAPI body-schema → starter JSON literal
  const sampleFor = (schema, schemas) => {
    if (!schema) return '';
    if (schema.$ref) {
      const name = schema.$ref.split('/').pop();
      return sampleFor(schemas?.[name], schemas);
    }
    if (schema.type === 'object' && schema.properties) {
      const obj = {};
      for (const [k, v] of Object.entries(schema.properties)) {
        if (v.default !== undefined) obj[k] = v.default;
        else if (v.example !== undefined) obj[k] = v.example;
        else if (v.type === 'string') obj[k] = '';
        else if (v.type === 'integer') obj[k] = 0;
        else if (v.type === 'number') obj[k] = 0;
        else if (v.type === 'boolean') obj[k] = false;
        else if (v.type === 'array') obj[k] = [];
      }
      return obj;
    }
    return {};
  };

  let allEndpoints = [];
  let writesUnlocked = false;
  let openIdx = null;        // currently-expanded row idx
  let schemas = {};          // OpenAPI components.schemas

  const fireRequest = async (ep, pathOverride, bodyText) => {
    const result = $('#sandbox-result');
    let path = pathOverride;
    // Append default query string for known params (only when they have a default)
    const qParams = ep.params.filter(p => p.in === 'query');
    const qsBits = [];
    for (const p of qParams) {
      const input = document.getElementById(`sb-q-${allEndpoints.indexOf(ep)}-${p.name}`);
      const v = input ? input.value : SMART_DEFAULTS[p.name];
      if (v !== undefined && v !== '') qsBits.push(`${p.name}=${encodeURIComponent(v)}`);
    }
    if (qsBits.length) path += (path.includes('?') ? '&' : '?') + qsBits.join('&');

    const init = { method: ep.method };
    if (bodyText && bodyText.trim()) {
      init.headers = { 'content-type': 'application/json' };
      init.body = bodyText;
    }
    result.innerHTML = `<div style="color:var(--fg-dim)">→ ${ep.method} ${escapeHtml(path)}</div><div class="empty">loading…</div>`;
    const t0 = performance.now();
    try {
      const r = await fetch(path, init);
      const ms = Math.round(performance.now() - t0);
      const text = await r.text();
      let bodyHtml;
      try {
        const j = JSON.parse(text);
        bodyHtml = `<pre style="margin:0;white-space:pre-wrap">${escapeHtml(JSON.stringify(j, null, 2))}</pre>`;
      } catch {
        bodyHtml = `<pre style="margin:0;white-space:pre-wrap">${escapeHtml(text)}</pre>`;
      }
      const color = r.ok ? 'var(--ok)' : 'var(--err)';
      result.innerHTML = `
        <div style="margin-bottom:8px;color:${color}"><strong>${r.status}</strong> ${ep.method} ${escapeHtml(path)} <span style="color:var(--fg-dim)">· ${ms}ms · ${text.length}b</span></div>
        ${bodyHtml}`;
    } catch (e) {
      result.innerHTML = `<div style="color:var(--err)">error: ${escapeHtml(e.message)}</div>`;
    }
  };

  const pathPlaceholderInputs = (ep) => {
    const idx = allEndpoints.indexOf(ep);
    const names = [...ep.path.matchAll(/\{(\w+)\}/g)].map(m => m[1]);
    return names.map(n => `
      <label style="display:flex;gap:6px;align-items:center;margin-bottom:4px;font-size:11px">
        <span style="min-width:120px;color:var(--fg-dim)">${escapeHtml(n)}</span>
        <input id="sb-p-${idx}-${n}" value="${escapeHtml(SMART_DEFAULTS[n] || '')}" style="flex:1;background:var(--bg);color:var(--fg);border:1px solid var(--border);border-radius:6px;padding:3px 8px;font-family:ui-monospace,monospace">
      </label>`).join('');
  };

  const queryInputs = (ep) => {
    const idx = allEndpoints.indexOf(ep);
    const qs = ep.params.filter(p => p.in === 'query');
    if (!qs.length) return '';
    return `<div style="margin-top:8px;color:var(--fg-dim);font-size:10px">QUERY</div>` + qs.map(p => `
      <label style="display:flex;gap:6px;align-items:center;margin-bottom:4px;font-size:11px">
        <span style="min-width:120px;color:var(--fg-dim)">${escapeHtml(p.name)}${p.required ? ' *' : ''}</span>
        <input id="sb-q-${idx}-${p.name}" value="${escapeHtml(SMART_DEFAULTS[p.name] || '')}" placeholder="${escapeHtml(p.schema?.default ?? p.schema?.pattern ?? '')}" style="flex:1;background:var(--bg);color:var(--fg);border:1px solid var(--border);border-radius:6px;padding:3px 8px;font-family:ui-monospace,monospace">
      </label>`).join('');
  };

  const bodyEditor = (ep) => {
    if (!ep.body) return '';
    const idx = allEndpoints.indexOf(ep);
    const sample = sampleFor(ep.body, schemas);
    const json = typeof sample === 'object' ? JSON.stringify(sample, null, 2) : '';
    return `
      <div style="margin-top:8px;color:var(--fg-dim);font-size:10px">JSON BODY</div>
      <textarea id="sb-b-${idx}" rows="6" style="width:100%;background:var(--bg);color:var(--fg);border:1px solid var(--border);border-radius:6px;padding:6px;font-family:ui-monospace,monospace;font-size:11px">${escapeHtml(json)}</textarea>`;
  };

  const buildPathFromInputs = (ep) => {
    const idx = allEndpoints.indexOf(ep);
    return ep.path.replace(/\{(\w+)\}/g, (_, n) => {
      const el = document.getElementById(`sb-p-${idx}-${n}`);
      return encodeURIComponent(el?.value || SMART_DEFAULTS[n] || `{${n}}`);
    });
  };

  const render = (filter = '') => {
    const list = $('#sandbox-list');
    const lc = filter.toLowerCase();
    const groups = {};
    for (const ep of allEndpoints) {
      if (lc && !`${ep.method} ${ep.path}`.toLowerCase().includes(lc)) continue;
      const seg = ep.path.replace('/api/of/v2/', '').split('/')[0] || '/';
      (groups[seg] = groups[seg] || []).push(ep);
    }
    const totalShown = Object.values(groups).reduce((s, a) => s + a.length, 0);
    $('#sandbox-count').textContent = `${totalShown} / ${allEndpoints.length}`;
    list.innerHTML = Object.entries(groups).sort().map(([g, eps]) => `
      <h3 style="margin:14px 0 6px;color:var(--fg-dim);text-transform:uppercase;font-size:11px">${escapeHtml(g)}</h3>
      ${eps.map(ep => {
        const idx = allEndpoints.indexOf(ep);
        const isWrite = ep.method !== 'GET';
        const isOpen = openIdx === idx;
        const blocked = isWrite && !writesUnlocked;
        const hasParams = ep.path.includes('{') || ep.params.length > 0 || ep.body;
        const color = ep.method === 'GET' ? 'var(--accent-2)'
                   : ep.method === 'DELETE' ? 'var(--err)'
                   : 'var(--accent)';
        return `<div data-row="${idx}" style="border-bottom:1px solid var(--border);padding:4px 0">
          <div style="display:flex;align-items:center;gap:8px;font-size:12px">
            <span style="display:inline-block;min-width:54px;font-weight:700;color:${color}">${ep.method}</span>
            <code style="flex:1;font-size:11px;color:var(--fg)">${escapeHtml(ep.path)}</code>
            ${hasParams
              ? `<button class="toolbar-btn" data-toggle-idx="${idx}" ${blocked ? 'disabled' : ''}>${isOpen ? '▾ Close' : '⚙ Edit'}</button>`
              : `<button class="toolbar-btn" data-fire-idx="${idx}" ${blocked ? 'disabled' : ''}>${isWrite ? '⚠ Run' : 'Run'}</button>`}
          </div>
          ${isOpen ? `<div style="margin:8px 0 8px 62px;padding:10px;background:var(--bg-elev-2);border:1px solid var(--border);border-radius:6px">
            ${pathPlaceholderInputs(ep)}
            ${queryInputs(ep)}
            ${bodyEditor(ep)}
            <button class="toolbar-btn" data-send-idx="${idx}" style="margin-top:8px;background:var(--accent);color:#000;border-color:var(--accent);font-weight:700">▶ Send</button>
          </div>` : ''}
        </div>`;
      }).join('')}
    `).join('');

    list.querySelectorAll('button[data-toggle-idx]').forEach(b => {
      b.onclick = () => { const i = +b.dataset.toggleIdx; openIdx = openIdx === i ? null : i; render($('#sandbox-filter').value); };
    });
    list.querySelectorAll('button[data-fire-idx]').forEach(b => {
      b.onclick = () => { const ep = allEndpoints[+b.dataset.fireIdx]; fireRequest(ep, ep.path); };
    });
    list.querySelectorAll('button[data-send-idx]').forEach(b => {
      b.onclick = () => {
        const idx = +b.dataset.sendIdx;
        const ep = allEndpoints[idx];
        const path = buildPathFromInputs(ep);
        const bodyText = document.getElementById(`sb-b-${idx}`)?.value;
        fireRequest(ep, path, bodyText);
      };
    });
  };

  return async () => {
    if (!allEndpoints.length) {
      const oas = await (await fetch('/openapi.json')).json();
      schemas = oas.components?.schemas || {};
      for (const [path, ops] of Object.entries(oas.paths)) {
        if (!path.startsWith('/api/of/v2')) continue;
        for (const [method, op] of Object.entries(ops)) {
          let body = null;
          const ref = op.requestBody?.content?.['application/json']?.schema;
          if (ref) body = ref;
          allEndpoints.push({
            method: method.toUpperCase(), path,
            params: op.parameters || [],
            body,
          });
        }
      }
      allEndpoints.sort((a, b) => a.path.localeCompare(b.path) || a.method.localeCompare(b.method));
    }

    const header = $('#sandbox-body').previousElementSibling;
    if (header && !header.dataset.lockBound) {
      const btn = document.createElement('button');
      btn.className = 'toolbar-btn';
      btn.textContent = '🔒 writes locked';
      btn.style.marginLeft = 'auto';
      btn.onclick = () => {
        writesUnlocked = !writesUnlocked;
        btn.textContent = writesUnlocked ? '🔓 writes UNLOCKED' : '🔒 writes locked';
        btn.classList.toggle('on', writesUnlocked);
        render($('#sandbox-filter').value);
      };
      header.insertBefore(btn, header.querySelector('.grow'));
      header.dataset.lockBound = '1';
    }

    $('#sandbox-filter').oninput = (e) => render(e.target.value);
    render('');
  };
})();

// ── PROFILE ──
// Shows: avatar + about + stat cards + 3 ACTION FORMS (Create post,
// Send / schedule message, Mass message) + settings dump.
// Each action form has a confirm-before-fire safeguard since these are writes.
// ── REALTIME — live event log + webhook CRUD + event catalog ──
// All events come through state.eventBus (one WS to /ws/events). This view
// just adds another subscriber that appends to a scrolling log; it does
// NOT open its own connection. Closing the tab leaves the bus running.
state.eventLog = [];        // ring buffer of last N events for the log
state.eventLogMax = 200;
state.eventLogPaused = false;
state.eventLogFilter = '';

// Subscribe once — populate the buffer even if the Events tab is never opened
state.eventBus.on((event) => {
  if (state.eventLogPaused) return;
  state.eventLog.unshift({ ts: Date.now(), event });
  if (state.eventLog.length > state.eventLogMax) state.eventLog.length = state.eventLogMax;
  // If the Events tab is currently visible, re-render its log live
  if (typeof state.rtRender === 'function') state.rtRender();
});

// Catalog of every OF realtime event key we know about, with what each means.
// Combines: keys observed live in our own WS pump + the 3rd-party
// "translation" docs (OFAuth realtime + OnlyFansAPI webhooks) so the user
// can correlate raw OF keys with the friendlier names those services use.
const RT_CATALOG = [
  // Connection lifecycle (handshake, OF native)
  { key: 'connected', kind: 'meta', desc: 'WS handshake ack', ofauth: '—', ofapi: '—' },
  { key: 'v',         kind: 'meta', desc: 'WS protocol version', ofauth: '—', ofapi: '—' },

  // Chats / DMs
  { key: 'api2_chat_message', kind: 'chat', desc: 'New direct message arrived',
    ofauth: 'realtime.chat_message.received', ofapi: 'messages.received' },
  { key: 'new_message', kind: 'chat', desc: 'Notification envelope for a new chat message',
    ofauth: 'realtime.chat_message.received', ofapi: 'messages.received' },
  { key: 'chat_message_delete', kind: 'chat', desc: 'A chat message was unsent',
    ofauth: 'chat_message_delete', ofapi: '—' },
  { key: 'chat_message_liked', kind: 'chat', desc: 'Someone liked a chat message',
    ofauth: 'realtime.chat_message.liked', ofapi: '—' },
  { key: 'chat_message_unliked', kind: 'chat', desc: 'Someone unliked a chat message',
    ofauth: 'realtime.chat_message.unliked', ofapi: '—' },
  { key: 'chat_typing', kind: 'chat', desc: 'A fan is typing in a chat',
    ofauth: 'realtime.chat_typing.updated', ofapi: 'users.typing' },

  // Counter snapshots (inbox + tabs)
  { key: 'chat_messages', kind: 'counter', desc: 'Unread DM count (snapshot)',
    ofauth: 'realtime.inbox_counts.updated', ofapi: '—' },
  { key: 'count_priority_chat', kind: 'counter', desc: 'Priority inbox unread count',
    ofauth: 'realtime.inbox_counts.updated', ofapi: '—' },
  { key: 'unread_tips', kind: 'counter', desc: 'Unread tip notifications',
    ofauth: 'realtime.inbox_counts.updated', ofapi: '—' },
  { key: 'messages', kind: 'counter', desc: 'Generic unread message counter',
    ofauth: 'realtime.inbox_counts.updated', ofapi: '—' },

  // Posts / feed (creators I follow)
  { key: 'post_published', kind: 'post', desc: 'A creator I follow published a post',
    ofauth: 'realtime.post.published', ofapi: '—' },
  { key: 'post_updated', kind: 'post', desc: 'A post was edited',
    ofauth: '—', ofapi: '—' },
  { key: 'post_expire', kind: 'post', desc: 'A timed post auto-expired',
    ofauth: '—', ofapi: '—' },
  { key: 'post_fundraising', kind: 'post', desc: 'Fundraising progress on a post',
    ofauth: 'realtime.post_fundraising.updated', ofapi: '—' },

  // Stories
  { key: 'stories_updated', kind: 'story', desc: 'A story was added/changed',
    ofauth: 'realtime.stories.updated', ofapi: '—' },

  // System / toasts
  { key: 'system_message', kind: 'system', desc: 'New system message received',
    ofauth: 'realtime.system_message.received', ofapi: '—' },
  { key: 'system_messages_updated', kind: 'system', desc: 'System inbox counters',
    ofauth: 'realtime.system_messages.updated', ofapi: '—' },
  { key: 'toast', kind: 'system', desc: 'In-app toast notification',
    ofauth: 'realtime.toasts.received', ofapi: '—' },

  // Money — likely keys (not yet observed live; documented by 3rd-party APIs)
  { key: 'tip', kind: 'money', desc: 'Tip received (LIKELY KEY — verify on first hit)',
    ofauth: '—', ofapi: 'transactions.new' },
  { key: 'new_subscriber', kind: 'money', desc: 'New paying subscriber',
    ofauth: '—', ofapi: 'subscriptions.new' },
  { key: 'subscription_renewed', kind: 'money', desc: 'Subscription auto-renewed',
    ofauth: '—', ofapi: 'subscriptions.renewed' },
  { key: 'subscription_expired', kind: 'money', desc: 'Subscription expired',
    ofauth: '—', ofapi: 'subscriptions.expired' },
  { key: 'rebill_on', kind: 'money', desc: 'Fan enabled auto-renew',
    ofauth: '—', ofapi: 'subscriptions.rebill_on' },
  { key: 'rebill_off', kind: 'money', desc: 'Fan disabled auto-renew',
    ofauth: '—', ofapi: 'subscriptions.rebill_off' },

  // Account/session (these are NOT OF native — they're OnlyFansAPI's own)
  // We surface them when we detect them locally (e.g. session expiry).
  { key: 'accounts_session_expired', kind: 'account', desc: 'Our captured cookies stopped working',
    ofauth: '—', ofapi: 'accounts.session_expired' },
];

const RT_KIND_COLOR = {
  meta:    'var(--fg-dim)',
  chat:    'var(--accent)',
  counter: 'var(--accent-2)',
  post:    '#7dd3fc',
  story:   '#facc15',
  system:  '#a78bfa',
  money:   'var(--ok)',
  account: 'var(--warn)',
};

loaders.realtime = async () => {
  const host = $('#rt-body');

  // ── render: top stats, controls, log table, webhook config, event catalog ──
  host.innerHTML = `
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:16px">
      <div class="card" style="padding:14px">
        <strong>📡 Pump stats</strong>
        <pre id="rt-stats" style="background:var(--bg);border:1px solid var(--border);border-radius:6px;padding:8px;font:11px/1.4 ui-monospace,monospace;margin-top:8px;min-height:60px">loading…</pre>
        <button id="rt-refresh-stats" class="toolbar-btn" style="margin-top:6px">↻ Refresh stats</button>
      </div>
      <div class="card" style="padding:14px">
        <strong>🪝 Webhooks</strong>
        <div style="color:var(--fg-dim);font-size:11px;margin:6px 0">External URLs we POST events to (one per event type or '*' = all).</div>
        <div id="rt-webhooks" style="font:11px/1.5 ui-monospace,monospace;background:var(--bg);border:1px solid var(--border);border-radius:6px;padding:8px;max-height:140px;overflow:auto;margin-top:6px">loading…</div>
        <div style="display:flex;gap:6px;margin-top:8px;flex-wrap:wrap">
          <input id="rt-wh-url"   placeholder="https://your-server/hook" style="flex:2;min-width:160px;background:var(--bg);color:var(--fg);border:1px solid var(--border);border-radius:6px;padding:5px 8px;font:11px ui-monospace,monospace">
          <input id="rt-wh-types" placeholder="event_types comma-sep ('*' = all)" value="*" style="flex:1;min-width:120px;background:var(--bg);color:var(--fg);border:1px solid var(--border);border-radius:6px;padding:5px 8px;font:11px ui-monospace,monospace">
          <button id="rt-wh-add" class="toolbar-btn">+ Add webhook</button>
        </div>
      </div>
    </div>

    <div style="display:flex;gap:8px;align-items:center;margin-bottom:8px;flex-wrap:wrap">
      <strong>📜 Live event log</strong>
      <span class="grow" style="flex:1"></span>
      <input id="rt-log-filter" placeholder="filter key (e.g. chat, post)" style="background:var(--bg);color:var(--fg);border:1px solid var(--border);border-radius:6px;padding:4px 8px;font:11px ui-monospace,monospace;min-width:180px">
      <button id="rt-log-pause" class="toolbar-btn">⏸ Pause</button>
      <button id="rt-log-clear" class="toolbar-btn">🗑 Clear</button>
    </div>
    <div id="rt-log" style="background:var(--bg-elev);border:1px solid var(--border);border-radius:8px;max-height:340px;overflow:auto;padding:6px;font:11px/1.4 ui-monospace,monospace">— waiting for events —</div>

    <h3 style="margin-top:22px">📚 Event catalog</h3>
    <div style="color:var(--fg-dim);font-size:12px;margin-bottom:8px">
      Raw OF event keys we know about, mapped to 3rd-party translations (<a href="https://docs.ofauth.com/api-reference/realtime/beta/chat-message" target="_blank">OFAuth</a> / <a href="https://docs.onlyfansapi.com" target="_blank">OnlyFansAPI</a>). Keys we've actually seen are tagged ✓.
    </div>
    <div id="rt-catalog"></div>
  `;

  // ── pump stats ──
  const loadStats = async () => {
    try {
      const r = await fetch('/admin/events/stats');
      const d = await r.json();
      const status = $('#rt-status');
      const anyRunning = (d.pumps_running || 0) > 0;
      if (status) {
        if (anyRunning) { status.className = 'pill ok'; status.textContent = `${d.pumps_running}/${Object.keys(d.pumps||{}).length} pumps · ${d.subscribers} client${d.subscribers===1?'':'s'}`; }
        else            { status.className = 'pill err'; status.textContent = 'pumps down'; }
      }
      const countEl = $('#rt-count');
      if (countEl) countEl.textContent = `${d.received} events`;
      const bytes = Object.entries(d.by_type || {})
        .sort(([,a],[,b]) => b - a)
        .map(([k, n]) => `  ${String(n).padStart(5)}  ${k}`).join('\n') || '  (none yet)';
      const accLines = Object.entries(d.by_account || {})
        .sort(([,a],[,b]) => b - a)
        .map(([aid, n]) => {
          const a = (state.accounts || []).find(x => x.id === aid);
          const name = a ? (a.nickname || aid) : aid;
          return `  ${String(n).padStart(5)}  ${name} (#${aid})`;
        }).join('\n') || '  (none yet)';
      const pumpLines = Object.entries(d.pumps || {})
        .map(([aid, p]) => {
          const a = (state.accounts || []).find(x => x.id === aid);
          const name = a ? (a.nickname || aid) : aid;
          return `  ${p.running ? '●' : '○'} ${name} (#${aid})`;
        }).join('\n') || '  (no pumps running)';
      $('#rt-stats').textContent =
        `received:     ${d.received}\n` +
        `subscribers:  ${d.subscribers}\n` +
        `pumps:        ${d.pumps_running}/${Object.keys(d.pumps||{}).length}\n` +
        `last event:   ${d.last_event_ts ? new Date(d.last_event_ts * 1000).toLocaleTimeString() : '—'}\n` +
        `started at:   ${d.started_at ? new Date(d.started_at * 1000).toLocaleTimeString() : '—'}\n\n` +
        `per account:\n${pumpLines}\n\n` +
        `by account:\n${accLines}\n\n` +
        `by type:\n${bytes}`;
    } catch (e) {
      $('#rt-stats').textContent = `error: ${e.message}`;
    }
  };
  $('#rt-refresh-stats').onclick = loadStats;
  loadStats();
  // Auto-refresh every 5s while the tab is mounted (cleanup on view change is fine — no leak risk)
  const statsTimer = setInterval(() => {
    if (!document.querySelector('.view[data-view="realtime"]')?.hidden) loadStats();
  }, 5000);
  state.rtStatsTimer && clearInterval(state.rtStatsTimer);
  state.rtStatsTimer = statsTimer;

  // ── webhooks ──
  const loadWebhooks = async () => {
    try {
      const r = await fetch('/admin/webhooks');
      const cfg = await r.json();
      const host = $('#rt-webhooks');
      const entries = Object.entries(cfg);
      if (!entries.length) { host.innerHTML = '<em style="color:var(--fg-dim)">no webhooks configured</em>'; return; }
      host.innerHTML = entries.map(([kind, urls]) =>
        urls.map(u => `<div style="display:flex;gap:6px;align-items:center;padding:2px 0">
          <span style="color:var(--accent);min-width:120px">${escapeHtml(kind)}</span>
          <span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${escapeHtml(u)}">${escapeHtml(u)}</span>
          <a data-rm-url="${escapeHtml(u)}" data-rm-kind="${escapeHtml(kind)}" style="cursor:pointer;color:var(--err)">×</a>
        </div>`).join('')
      ).join('');
      host.querySelectorAll('a[data-rm-url]').forEach(a => a.onclick = async () => {
        if (!confirm(`Delete webhook ${a.dataset.rmUrl} for ${a.dataset.rmKind}?`)) return;
        await fetch(`/admin/webhooks?url=${encodeURIComponent(a.dataset.rmUrl)}&event_type=${encodeURIComponent(a.dataset.rmKind)}`, { method: 'DELETE' });
        loadWebhooks();
      });
    } catch (e) {
      $('#rt-webhooks').textContent = `error: ${e.message}`;
    }
  };
  $('#rt-wh-add').onclick = async () => {
    const url = $('#rt-wh-url').value.trim();
    const types = $('#rt-wh-types').value.trim().split(',').map(s=>s.trim()).filter(Boolean);
    if (!url || !types.length) { alert('url + at least one event_type required'); return; }
    const r = await fetch('/admin/webhooks', {
      method: 'POST', headers: {'content-type':'application/json'},
      body: JSON.stringify({ url, event_types: types }),
    });
    if (!r.ok) { alert(`failed: ${r.status} ${(await r.text()).slice(0,200)}`); return; }
    $('#rt-wh-url').value = '';
    loadWebhooks();
  };
  loadWebhooks();

  // ── event log ──
  const seenKeys = new Set();
  const logEl = $('#rt-log');
  state.rtRender = () => {
    const filt = state.eventLogFilter.toLowerCase().trim();
    const rows = state.eventLog.filter(({ event }) => {
      if (!filt) return true;
      return Object.keys(event || {}).some(k => k.toLowerCase().includes(filt));
    });
    if (!rows.length) { logEl.innerHTML = '<div style="color:var(--fg-dim);padding:8px">— no events match —</div>'; return; }
    logEl.innerHTML = rows.slice(0, 80).map(({ ts, event }) => {
      // Strip our meta-keys before showing them as "the event type"
      const keys = Object.keys(event || {}).filter(k => !k.startsWith('__'));
      keys.forEach(k => seenKeys.add(k));
      const primary = keys[0] || '?';
      const kind = (RT_CATALOG.find(c => c.key === primary) || {}).kind || 'unknown';
      const color = RT_KIND_COLOR[kind] || 'var(--fg-dim)';
      const time = new Date(ts).toLocaleTimeString();
      const acctName = event && event.__account_name;
      const acctColor = (event && event.__account_color) || '#888';
      // Hide our private keys from the preview as well
      const preview = JSON.stringify(Object.fromEntries(
        Object.entries(event || {}).filter(([k]) => !k.startsWith('__'))
      )).slice(0, 180);
      return `<div style="padding:3px 6px;border-bottom:1px solid var(--border)">
        <span style="color:var(--fg-dim)">${time}</span>
        ${acctName ? `<span class="pill" style="margin-left:6px;padding:1px 6px;font-size:10px;background:${acctColor}22;color:${acctColor};border:1px solid ${acctColor}44">${escapeHtml(acctName)}</span>` : ''}
        <strong style="color:${color};margin-left:8px">${escapeHtml(primary)}</strong>
        ${keys.length > 1 ? `<span style="color:var(--fg-dim);margin-left:4px">+${keys.length-1}</span>` : ''}
        <span style="color:var(--fg);margin-left:8px">${escapeHtml(preview)}${preview.length >= 180 ? '…' : ''}</span>
      </div>`;
    }).join('');
    // After rendering, refresh the catalog ✓ marks
    renderCatalog();
  };
  $('#rt-log-filter').oninput = (e) => { state.eventLogFilter = e.target.value; state.rtRender(); };
  $('#rt-log-pause').onclick = () => {
    state.eventLogPaused = !state.eventLogPaused;
    $('#rt-log-pause').textContent = state.eventLogPaused ? '▶ Resume' : '⏸ Pause';
    $('#rt-log-pause').classList.toggle('on', state.eventLogPaused);
  };
  $('#rt-log-clear').onclick = () => { state.eventLog.length = 0; state.rtRender(); };

  // ── catalog ──
  const renderCatalog = () => {
    const groups = {};
    for (const e of RT_CATALOG) { (groups[e.kind] ||= []).push(e); }
    const kindOrder = ['chat','counter','post','story','system','money','account','meta'];
    $('#rt-catalog').innerHTML = kindOrder.filter(k => groups[k]).map(kind => `
      <h4 style="color:${RT_KIND_COLOR[kind]};margin:14px 0 6px;text-transform:uppercase;font-size:11px;letter-spacing:.5px">${kind}</h4>
      <table class="data" style="margin-bottom:4px">
        <thead><tr>
          <th></th><th>OF key</th><th>description</th>
          <th>OFAuth name</th><th>OnlyFansAPI name</th>
        </tr></thead>
        <tbody>${groups[kind].map(e => `<tr>
          <td>${seenKeys.has(e.key) ? '<span title="seen live" style="color:var(--ok)">✓</span>' : '<span style="color:var(--fg-dim)">·</span>'}</td>
          <td><code style="color:${RT_KIND_COLOR[kind]}">${escapeHtml(e.key)}</code></td>
          <td>${escapeHtml(e.desc)}</td>
          <td style="color:var(--fg-dim);font-family:ui-monospace,monospace;font-size:10px">${escapeHtml(e.ofauth)}</td>
          <td style="color:var(--fg-dim);font-family:ui-monospace,monospace;font-size:10px">${escapeHtml(e.ofapi)}</td>
        </tr>`).join('')}</tbody>
      </table>`).join('');
  };
  // seed seenKeys from current buffer
  for (const r of state.eventLog)
    for (const k of Object.keys(r.event || {}))
      if (!k.startsWith('__')) seenKeys.add(k);
  state.rtRender();
};

// ── SETUP — session bootstrap (3 modes, no Incogniton at runtime) ──
loaders.setup = async () => {
  const host = $('#setup-body');
  const meta = $('#setup-meta');

  const status = async () => {
    try {
      const r = await fetch('/admin/session/status');
      const d = await r.json();
      if (d.loaded) {
        meta.textContent = `${d.user_id || '?'} · rev ${d.x_of_rev || '?'} · captured ${d.captured_at || '?'} · profile ${d.profile_id || '?'}`;
        meta.className = 'pill ok';
      } else {
        meta.textContent = 'no session loaded';
        meta.className = 'pill warn';
      }
    } catch (e) {
      meta.textContent = 'status error';
      meta.className = 'pill err';
    }
  };

  const post = async (body) => {
    const out = $('#setup-result');
    out.textContent = `working… ${body.mode}`;
    out.style.color = 'var(--fg-dim)';
    try {
      const r = await fetch('/admin/session/bootstrap', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(body),
      });
      const text = await r.text();
      let parsed = null;
      try { parsed = JSON.parse(text); } catch {}
      const pretty = parsed ? JSON.stringify(parsed, null, 2) : text;
      out.textContent = `${r.status} ${r.ok ? 'OK' : 'FAILED'}\n\n${pretty}`;
      out.style.color = r.ok ? 'var(--ok)' : 'var(--err)';
      if (r.ok) {
        await status();
        // Refresh health pill in header
        try { fetch('/health').then(r => r.json()).then(d => {
          const pill = $('#health-pill');
          if (pill) { pill.textContent = d.ok ? `ok · ${d.name}` : 'no session'; pill.className = `pill ${d.ok ? 'ok' : 'err'}`; }
        }); } catch {}
      }
      // Caller may want the account_id (e.g. to attach a proxy as a follow-up).
      return r.ok ? parsed : null;
    } catch (e) {
      out.textContent = `error: ${e.message}`;
      out.style.color = 'var(--err)';
      return null;
    }
  };

  host.innerHTML = `
    <div style="max-width:900px">
      <div style="background:var(--bg-elev);border:1px solid var(--border);border-radius:10px;padding:14px 16px;margin-bottom:16px">
        <strong>How sessions work</strong>
        <p style="color:var(--fg-dim);font-size:12px;line-height:1.5;margin:6px 0 0">
          Once per OnlyFans revision (~monthly) you bootstrap a session: cookies, a few
          headers, and the signing rules used to sign every API call. After that,
          everything (reads, writes, uploads) runs from this server with no browser
          needed. Each captured session is filed under the OF account it belongs to —
          one server can serve many models. Use the dropdown below to add, rename or
          re-capture an account.
        </p>
      </div>

      <h3 style="margin:18px 0 8px;display:flex;align-items:center;gap:10px">
        Accounts
        <span class="pill" id="setup-active-pill">—</span>
      </h3>
      <div id="accounts-host"><div class="empty">loading accounts…</div></div>

      <h3 style="margin:24px 0 8px">Add or re-capture an account</h3>
      <div class="card" style="padding:10px 12px;margin-bottom:10px">
        <label style="display:flex;align-items:center;gap:10px;font-size:12px">
          <span style="min-width:120px;color:var(--fg-dim)">Nickname (optional)</span>
          <input id="setup-nickname" placeholder="e.g. Bella, Mia2, Test-burner" style="flex:1;background:var(--bg);color:var(--fg);border:1px solid var(--border);border-radius:6px;padding:6px 8px;font:inherit;font-size:12px">
        </label>
        <label style="display:flex;align-items:center;gap:10px;font-size:12px;margin-top:8px">
          <span style="min-width:120px;color:var(--fg-dim)">Make active after</span>
          <input id="setup-make-active" type="checkbox" checked> <span style="color:var(--fg-dim);font-size:11px">(flip the relay's default to this account once captured)</span>
        </label>
      </div>

      <div style="display:grid;grid-template-columns:1fr 1fr;gap:14px">

        <div class="card" style="padding:14px">
          <strong>① Default Incogniton profile</strong>
          <div style="color:var(--fg-dim);font-size:11px;margin:6px 0">Uses the profile id from <code>INCOGNITON_PROFILE_ID</code> env var (or the hard-coded fallback). Plays back to a normal Incogniton browser tab; you log in once if needed.</div>
          <button id="setup-mode-default" class="toolbar-btn" style="margin-top:6px">Run capture</button>
        </div>

        <div class="card" style="padding:14px">
          <strong>② Custom Incogniton profile</strong>
          <div style="color:var(--fg-dim);font-size:11px;margin:6px 0">Each model needs its own Incogniton profile (= its own fingerprint + cookies). Paste the UUID of the profile already logged into that account.</div>
          <input id="setup-profile-id" placeholder="profile uuid e.g. 2a4924fb-29c7-…" style="width:100%;background:var(--bg);color:var(--fg);border:1px solid var(--border);border-radius:6px;padding:6px 8px;font:inherit;font-size:12px;margin-top:4px">
          <button id="setup-mode-custom" class="toolbar-btn" style="margin-top:8px">Run capture</button>
        </div>

        <div class="card" style="padding:14px;grid-column:span 2;border:1px solid rgba(124,92,255,.45);background:rgba(124,92,255,.04)">
          <strong>⭐ Browser through proxy (recommended — login + runtime share the same IP)</strong>
          <div style="color:var(--fg-dim);font-size:11px;margin:6px 0;line-height:1.5">
            Spins up a fresh Chromium routed through one of your saved proxies. You log in once
            through that proxy IP, so OF's cookies are minted from the same egress the relay
            uses later. No "teleport" risk like there is with Incogniton-on-host-IP.
            <br><strong style="color:var(--accent)">HOST-ONLY</strong> — requires Playwright on the machine running uvicorn (not the Docker container).
          </div>
          <div style="display:flex;gap:8px;align-items:center;margin-top:8px;flex-wrap:wrap">
            <label style="color:var(--fg-dim);font-size:12px;min-width:80px">Proxy</label>
            <select id="setup-pw-proxy" style="flex:1;min-width:200px;background:var(--bg);color:var(--fg);border:1px solid var(--border);border-radius:6px;padding:6px 8px;font:inherit;font-size:12px">
              <option value="">— loading proxies… —</option>
            </select>
          </div>
          <div style="display:flex;gap:8px;align-items:center;margin-top:10px;flex-wrap:wrap">
            <button id="setup-mode-pw-proxy" class="toolbar-btn" style="background:var(--accent);color:#000;font-weight:700;border-color:var(--accent)">Launch browser via proxy</button>
            <button id="setup-wipe-fresh-buckets" class="toolbar-btn" title="Delete every fresh-* browser profile dir so the next launch is guaranteed-clean and disk is reclaimed">Wipe previous sessions</button>
          </div>
        </div>

        <div class="card" style="padding:14px;grid-column:span 2">
          <strong>③ Paste cURL — preferred (use the Chatterly Login extension)</strong>
          <div style="color:var(--fg-dim);font-size:11px;margin:6px 0;line-height:1.5">
            Install <code>loginExtension/</code> as an unpacked Chrome extension, click
            <strong>Log in to OnlyFans</strong>, sign in, then <strong>Copy curl</strong>.
            Paste it below. The extension injects <code>x-relay-static-param</code> so
            this works on any new OF revision without a separate Incogniton bootstrap.
            <br><br>
            Or, for the manual fallback: in any browser logged into OnlyFans →
            DevTools (F12) → Network tab → click any <code>/api2/v2/*</code> request →
            right-click → <strong>Copy → Copy as cURL</strong>. (Manual curls need the
            <em>advanced</em> static_param override on first use at a new OF revision.)
          </div>
          <textarea id="setup-curl" rows="6" placeholder="curl 'https://onlyfans.com/api2/v2/...' -H 'sign: …' -H 'time: …' -H 'user-id: …' -H 'x-bc: …' -H 'x-of-rev: …' -H 'user-agent: …' -b 'auth_id=…; sess=…; …'" style="width:100%;background:var(--bg);color:var(--fg);border:1px solid var(--border);border-radius:6px;padding:8px;font:11px/1.4 ui-monospace,monospace;resize:vertical"></textarea>
          <div style="display:flex;gap:8px;align-items:center;margin-top:8px;flex-wrap:wrap">
            <label style="color:var(--fg-dim);font-size:12px;min-width:80px">Proxy</label>
            <select id="setup-curl-proxy" style="flex:1;min-width:200px;background:var(--bg);color:var(--fg);border:1px solid var(--border);border-radius:6px;padding:6px 8px;font:inherit;font-size:12px">
              <option value="">— none (relay egresses on this server's WAN IP) —</option>
            </select>
          </div>
          <div style="color:var(--warn,#f5c34a);font-size:11px;margin-top:6px;line-height:1.5">
            ⚠ <strong>Teleport warning:</strong> the curl's cookies were minted from
            the IP <em>you</em> logged in on. If you attach a proxy here, every signed
            call the relay sends afterwards will egress from the proxy IP — OF sees
            the same account suddenly hop continents. Only safe when the proxy IP
            is close to your login IP (same country / similar ASN).
          </div>
          <details style="margin-top:6px">
            <summary style="color:var(--fg-dim);font-size:11px;cursor:pointer">advanced: override static_param (only if you're on an OF revision we haven't seen)</summary>
            <input id="setup-staticparam" placeholder="static_param (32 chars; usually leave blank)" style="width:100%;background:var(--bg);color:var(--fg);border:1px solid var(--border);border-radius:6px;padding:6px 8px;font:inherit;font-size:11px;margin-top:6px">
          </details>
          <button id="setup-mode-curl" class="toolbar-btn" style="margin-top:10px;background:var(--accent);color:#000;font-weight:700;border-color:var(--accent)">Bootstrap from curl</button>
        </div>
      </div>

      <h3 style="margin-top:24px">Result</h3>
      <pre id="setup-result" style="background:var(--bg-elev);border:1px solid var(--border);border-radius:8px;padding:12px;font:11px/1.4 ui-monospace,monospace;color:var(--fg-dim);min-height:80px;white-space:pre-wrap;overflow:auto;max-height:400px">— pick a mode above —</pre>

      <h3 style="margin-top:28px;display:flex;align-items:center;gap:10px">
        Proxies
        <span class="pill warn" style="font-weight:400">Phase 1 · DC testing · creds plaintext</span>
      </h3>
      <div style="color:var(--fg-dim);font-size:11px;line-height:1.5;margin:6px 0 12px;max-width:780px">
        Each captured session can be bound to one proxy. OFClient routes every signed
        request through that proxy. <strong>Assign before sharing the relay</strong> —
        otherwise every model egresses from this server's WAN IP and OF links the accounts.
      </div>
      <div id="proxies-host"><div class="empty">loading…</div></div>
    </div>`;

  // Common extras pulled from the optional inputs above the mode cards
  const extras = () => {
    const out = {};
    const nick = $('#setup-nickname').value.trim();
    if (nick) out.nickname = nick;
    out.make_active = !!$('#setup-make-active').checked;
    return out;
  };

  // Populate the playwright-proxy mode's proxy dropdown from /admin/proxies.
  // Done inline (not awaited) so the rest of the form is interactive immediately.
  (async () => {
    try {
      const r = await _origFetch('/admin/proxies');
      const d = await r.json();
      const sel = $('#setup-pw-proxy');
      if (!sel) return;
      const proxies = d.proxies || [];
      if (!proxies.length) {
        sel.innerHTML = '<option value="">— no proxies registered (add one below) —</option>';
        return;
      }
      sel.innerHTML = '<option value="">— pick a proxy —</option>' +
        proxies.map(p => {
          const acct = p.assigned_account ? ` · → ${p.assigned_account.nickname || p.assigned_account.id}` : ' · unassigned';
          return `<option value="${escapeHtml(p.label)}">${escapeHtml(p.label)} (${escapeHtml(p.host)}:${p.port})${acct}</option>`;
        }).join('');
    } catch (e) { console.warn('proxy list for playwright-proxy failed:', e); }
  })();

  $('#setup-mode-pw-proxy').onclick = () => {
    const label = $('#setup-pw-proxy').value.trim();
    if (!label) { alert('pick a proxy first'); return; }
    if (!confirm(`Launch a fresh Chromium through proxy "${label}"?\n\nA browser window will open on the host running uvicorn. Log into OF normally; cookies will be captured automatically once auth_id is set.\n\nThis mode is host-only — it does NOT work inside the Docker container.`)) return;
    post({ mode: 'playwright-proxy', proxy_label: label, ...extras() });
  };

  $('#setup-wipe-fresh-buckets').onclick = async () => {
    if (!confirm('Wipe every fresh-* browser profile dir?\n\nDoes NOT touch your account dirs or captured sessions — only the disposable per-launch Chromium profiles. Use this if a previous launch left you logged in as an old account.')) return;
    const btn = $('#setup-wipe-fresh-buckets');
    const orig = btn.textContent;
    btn.textContent = 'Wiping…';
    btn.disabled = true;
    const resultEl = $('#setup-result');
    try {
      const r = await _origFetch('/admin/session/wipe-fresh-browser-buckets', { method: 'POST' });
      const d = await r.json();
      if (resultEl) resultEl.textContent = JSON.stringify(d, null, 2);
      btn.textContent = `Wiped ${d.wiped?.length ?? 0}`;
      setTimeout(() => { btn.textContent = orig; btn.disabled = false; }, 2000);
    } catch (e) {
      if (resultEl) resultEl.textContent = String(e);
      btn.textContent = orig;
      btn.disabled = false;
    }
  };

  $('#setup-mode-default').onclick = () => {
    if (!confirm('Run Incogniton capture with the default profile? This opens an Incogniton browser window if not already open.')) return;
    post({ mode: 'incogniton-default', ...extras() });
  };
  $('#setup-mode-custom').onclick = () => {
    const pid = $('#setup-profile-id').value.trim();
    if (!pid) { alert('profile_id required'); return; }
    if (!confirm(`Run Incogniton capture with profile ${pid}?`)) return;
    post({ mode: 'incogniton-custom', profile_id: pid, ...extras() });
  };
  // Populate the proxy dropdown on the paste-curl card. Same options as the
  // playwright-proxy card — including the "none" leading option so the user
  // can intentionally bootstrap with no proxy attached.
  (async () => {
    try {
      const r = await _origFetch('/admin/proxies');
      const d = await r.json();
      const sel = $('#setup-curl-proxy');
      if (!sel) return;
      const proxies = d.proxies || [];
      const opts = ['<option value="">— none (relay egresses on this server\'s WAN IP) —</option>'];
      for (const p of proxies) {
        const acct = p.assigned_account ? ` · → ${p.assigned_account.nickname || p.assigned_account.id}` : ' · unassigned';
        opts.push(`<option value="${escapeHtml(p.label)}">${escapeHtml(p.label)} (${escapeHtml(p.host)}:${p.port})${acct}</option>`);
      }
      sel.innerHTML = opts.join('');
    } catch (e) { console.warn('proxy list for paste-curl failed:', e); }
  })();

  $('#setup-mode-curl').onclick = async () => {
    const curl = $('#setup-curl').value.trim();
    if (!curl) { alert('paste a curl command first'); return; }
    if (!curl.startsWith('curl')) { alert('must start with `curl`'); return; }
    const sp = $('#setup-staticparam').value.trim();
    const proxyLabel = $('#setup-curl-proxy').value.trim();
    if (proxyLabel) {
      if (!confirm(
        `Attach proxy "${proxyLabel}" to this account?\n\n` +
        `OnlyFans will see every signed call egress from the proxy IP. The ` +
        `cookies you're pasting were minted from your own browsing IP, so OF ` +
        `effectively sees the account "teleport" to the proxy. Only safe when ` +
        `the proxy IP is geographically/ASN-close to where you logged in.\n\n` +
        `OK to attach the proxy. Cancel to bootstrap with no proxy.`
      )) return;
    }
    const body = { mode: 'paste-curl', curl, ...extras() };
    if (sp) body.static_param_override = sp;
    const result = await post(body);
    // post() returned the parsed response. If we have an account_id and the
    // user picked a proxy, bind them now — the curl-paste flow doesn't
    // accept proxy_label upstream, so we attach as a second step.
    if (proxyLabel && result && result.account_id) {
      try {
        const r = await fetch('/admin/proxies/assign', {
          method: 'POST',
          headers: {'content-type': 'application/json'},
          body: JSON.stringify({ label: proxyLabel, account_id: result.account_id }),
        });
        if (!r.ok) {
          const t = await r.text();
          alert(`Session captured, but proxy attach failed:\n\n${t}\n\nAttach manually under Setup → Proxies.`);
        } else {
          // Reload the client so the next signed call routes via the new proxy.
          try { await fetch(`/admin/reload-session?account_id=${encodeURIComponent(result.account_id)}`, { method: 'POST' }); } catch {}
        }
      } catch (e) {
        alert(`Proxy attach failed: ${e.message}\nAttach manually under Setup → Proxies.`);
      }
    }
  };

  if ($('#setup-reload')) $('#setup-reload').onclick = status;
  await status();
  await renderAccountsPanel();
  await renderProxies();
};

// Re-capture is now done out-of-band via the Chatterly Login extension —
// the user signs in there, copies the fresh curl, and pastes it into the
// Setup → Paste cURL card. No in-UI browser modal needed.

// ── Accounts panel inside Setup tab ─────────────────────────
async function renderAccountsPanel() {
  const host = document.querySelector('#accounts-host');
  const activePill = document.querySelector('#setup-active-pill');
  if (!host) return;
  host.innerHTML = '<div class="empty">loading accounts…</div>';
  let data;
  try {
    const r = await _origFetch('/admin/accounts');
    data = await r.json();
  } catch (e) {
    host.innerHTML = `<div class="empty" style="color:var(--err)">error loading: ${e.message}</div>`;
    return;
  }
  const accounts = data.accounts || [];
  const activeId = data.active_account_id;
  if (activePill) {
    const a = accounts.find(x => x.id === activeId);
    activePill.textContent = a ? `default: ${a.nickname || a.id}` : 'no active account';
    activePill.className = 'pill ' + (a ? 'ok' : 'warn');
  }
  // Update the global switcher too so the rest of the UI is consistent.
  await refreshAccounts();
  // Probe each account's health in parallel so we can flag expired sessions
  let healthByAid = {};
  try {
    const r = await _origFetch('/health?all_accounts=1');
    const j = await r.json();
    for (const row of (j.accounts || [])) healthByAid[row.account_id] = row;
  } catch (e) { /* leave empty */ }

  if (!accounts.length) {
    host.innerHTML = `<div class="empty">no accounts yet — bootstrap one below</div>`;
    return;
  }
  host.innerHTML = `
    <table style="width:100%;border-collapse:collapse;font-size:12px">
      <thead><tr style="text-align:left;color:var(--fg-dim);font-size:11px;border-bottom:1px solid var(--border)">
        <th style="padding:6px 8px">Account</th>
        <th style="padding:6px 8px">user_id</th>
        <th style="padding:6px 8px">Status</th>
        <th style="padding:6px 8px">Incogniton</th>
        <th style="padding:6px 8px">Last used</th>
        <th style="padding:6px 8px">Actions</th>
      </tr></thead>
      <tbody>
      ${accounts.map(a => {
        const h = healthByAid[a.id] || {};
        const statusPill = !a.has_session
          ? '<span class="pill warn">no session</span>'
          : h.ok ? `<span class="pill ok">live · ${escapeHtml(h.name || '')}</span>`
                 : `<span class="pill err" title="${escapeHtml(h.upstream_body || h.error || '')}">${escapeHtml(h.error || 'down')}</span>`;
        return `
        <tr data-aid="${a.id}" style="border-bottom:1px solid var(--border)">
          <td style="padding:6px 8px">
            <span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${a.color || '#888'};margin-right:6px;vertical-align:middle"></span>
            <input data-field="nickname" value="${escapeHtml(a.nickname || a.id)}" style="background:transparent;border:1px solid transparent;color:var(--fg);padding:3px 6px;border-radius:4px;font-size:12px;width:180px">
          </td>
          <td style="padding:6px 8px;color:var(--fg-dim);font-family:ui-monospace,monospace;font-size:11px">${a.id}</td>
          <td style="padding:6px 8px">${statusPill} ${a.id === activeId ? '<span class="pill" style="margin-left:4px">default</span>' : ''}</td>
          <td style="padding:6px 8px;font-family:ui-monospace,monospace;font-size:10px;color:var(--fg-dim)" title="${escapeHtml(a.incogniton_profile_id || '')}">${a.incogniton_profile_id ? a.incogniton_profile_id.slice(0,8)+'…' : '—'}</td>
          <td style="padding:6px 8px;color:var(--fg-dim);font-size:11px">${a.last_used_at || '—'}</td>
          <td style="padding:6px 8px;white-space:nowrap">
            <button class="toolbar-btn" data-act="view">View</button>
            <button class="toolbar-btn" data-act="activate" ${a.id===activeId?'disabled':''}>Default</button>
            <button class="toolbar-btn" data-act="recapture" title="re-run Incogniton capture for this account">Re-capture</button>
            <button class="toolbar-btn" data-act="save" style="display:none">Save</button>
            <button class="toolbar-btn" data-act="delete" style="color:var(--err)">Delete</button>
          </td>
        </tr>`;
      }).join('')}
      </tbody>
    </table>`;

  host.querySelectorAll('tr[data-aid]').forEach(tr => {
    const aid = tr.dataset.aid;
    const nickInput = tr.querySelector('[data-field="nickname"]');
    const saveBtn = tr.querySelector('[data-act="save"]');
    nickInput.addEventListener('input', () => { saveBtn.style.display = 'inline-block'; });
    saveBtn.onclick = async () => {
      const nickname = nickInput.value.trim();
      const r = await fetch(`/admin/accounts/${encodeURIComponent(aid)}`, {
        method: 'PATCH',
        headers: {'content-type': 'application/json'},
        body: JSON.stringify({ nickname }),
      });
      if (r.ok) await renderAccountsPanel();
    };
    tr.querySelector('[data-act="view"]').onclick = () => switchAccount(aid);
    tr.querySelector('[data-act="activate"]').onclick = async () => {
      const r = await fetch('/admin/accounts/active', {
        method: 'POST', headers: {'content-type':'application/json'},
        body: JSON.stringify({ account_id: aid }),
      });
      if (r.ok) await renderAccountsPanel();
    };
    tr.querySelector('[data-act="recapture"]').onclick = async () => {
      const acctRow = (state.accounts || []).find(x => x.id === aid) || {};

      // PATH A — account has an Incogniton profile id stored → re-capture
      // via Incogniton (cleanest match: same fingerprint that minted the
      // original cookies). Requires uvicorn running on host (not Docker)
      // AND Incogniton running on that host. We try; if the server reports
      // "No module named 'capture_session'" we offer the proxy fallback.
      if (acctRow.incogniton_profile_id) {
        if (!confirm(
          `Re-capture "${acctRow.nickname || aid}" via Incogniton?\n\n` +
          `Profile: ${acctRow.incogniton_profile_id}\n\n` +
          `Requirements:\n` +
          `  • uvicorn running locally on the host (not Docker)\n` +
          `  • Incogniton desktop app running\n\n` +
          `Click OK to proceed. If you're in Docker, click Cancel and we'll ` +
          `offer the proxy-based remote browser instead.`
        )) {
          // User cancelled Incogniton — fall through to proxy path below.
        } else {
          const r = await fetch('/admin/session/bootstrap', {
            method:'POST', headers:{'content-type':'application/json'},
            body: JSON.stringify({
              mode: 'incogniton-custom',
              profile_id: acctRow.incogniton_profile_id,
              account_id: aid, make_active: false,
            }),
          });
          const txt = await r.text();
          if (r.ok) {
            alert(`re-captured via Incogniton\n\n${txt}`);
            await renderAccountsPanel();
            return;
          }
          // Common failure: capture_session module isn't in the runtime
          // (Docker image) — surface that + offer the proxy fallback.
          if (/capture_session/i.test(txt)) {
            if (!confirm(
              `Incogniton mode failed — capture_session module isn't available ` +
              `in this runtime (you're probably in Docker, where Incogniton ` +
              `can't run anyway).\n\n` +
              `Switch to the proxy-based remote-browser capture instead?`
            )) return;
            // fall through to proxy path
          } else {
            alert(`Incogniton capture failed:\n\n${txt}`);
            return;
          }
        }
      }

      // PATH B — proxy-based capture. Look up the proxy bound to this
      // account so login egress = runtime egress (no teleport).
      let proxyLabel = null;
      try {
        const pr = await _origFetch('/admin/proxies');
        const pd = await pr.json();
        const p = (pd.proxies || []).find(x => x.assigned_account && x.assigned_account.id === aid);
        if (p) proxyLabel = p.label;
      } catch {}

      if (!proxyLabel) {
        alert(
          `Account "${acctRow.nickname || aid}" has no proxy assigned and no ` +
          `Incogniton profile saved.\n\n` +
          `Assign a proxy first under Setup → Proxies, then click Re-capture again.\n\n` +
          `(Re-capturing without a proxy would mint cookies from this server's WAN IP — ` +
          `OF would flag it as a "teleport" the next time the relay sent a signed request.)`
        );
        return;
      }

      // Preferred re-capture path now: install the Chatterly Login extension,
      // sign in on your own machine, copy the curl, paste into the Setup card.
      // Host-Playwright is still available for users who run uvicorn locally.
      if (!confirm(
        `Re-capture "${acctRow.nickname || aid}" — pick a path:\n\n` +
        `• OK  → launch host Chromium via proxy "${proxyLabel}" (requires uvicorn ` +
        `running locally with venv, NOT Docker).\n` +
        `• Cancel → close this dialog. Recommended path: use the Chatterly Login ` +
        `extension (loginExtension/) on the device you normally browse OF from, ` +
        `then paste the fresh curl into Setup → Paste cURL.`
      )) return;

      const r = await fetch('/admin/session/bootstrap', {
        method:'POST', headers:{'content-type':'application/json'},
        body: JSON.stringify({
          mode: 'playwright-proxy', proxy_label: proxyLabel,
          account_id: aid, make_active: false,
        }),
      });
      const txt = await r.text();
      alert(r.ok ? `re-captured\n\n${txt}` : `failed:\n${txt}`);
      await renderAccountsPanel();
    };
    tr.querySelector('[data-act="delete"]').onclick = async () => {
      if (!confirm(`Delete account ${aid}? This removes its session(s) and cannot be undone.`)) return;
      const r = await fetch(`/admin/accounts/${encodeURIComponent(aid)}`, { method: 'DELETE' });
      if (r.ok) {
        if (state.account.id === aid) {
          localStorage.removeItem('of_account_id');
          state.account.id = null;
        }
        await renderAccountsPanel();
      }
    };
  });
}

async function renderProxies() {
  const host = document.querySelector('#proxies-host');
  if (!host) return;
  host.innerHTML = '<div class="empty">loading proxies…</div>';
  let data;
  try {
    const r = await fetch('/admin/proxies');
    data = await r.json();
  } catch (e) {
    host.innerHTML = `<div class="empty" style="color:var(--err)">error loading proxies: ${e.message}</div>`;
    return;
  }
  const proxies = data.proxies || [];
  const accounts = data.accounts || [];
  const acctOptions = ['<option value="">— unassigned —</option>']
    .concat(accounts.map(a =>
      `<option value="${a.id}">${escapeHtml(a.nickname || a.id)} (#${a.id})${a.has_session ? '' : ' · ⚠ no session'}</option>`
    )).join('');

  const escAttr = s => String(s ?? '').replace(/"/g, '&quot;').replace(/</g, '&lt;');

  const rows = proxies.map(p => {
    const credLine = (p.username || p.password)
      ? `${p.host}:${p.port}:${p.username || ''}:${p.password || ''}`
      : `${p.host}:${p.port}`;
    const verified = p.verified_ip
      ? `<span class="pill ok" title="${p.verified_at || ''}">${p.verified_ip} · ${p.verified_geo || '?'}</span>`
      : '<span class="pill warn">unverified</span>';
    // Show the bound ACCOUNT (preferred). Fall back to the legacy session-file
    // binding if no account is set — keeps any leftover assignments visible.
    const acct = p.assigned_account;
    const assignedLabel = acct
      ? `<span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${acct.color || '#888'};margin-right:6px;vertical-align:middle"></span>` +
        `<strong>${escapeHtml(acct.nickname || acct.id)}</strong> ` +
        `<span style="color:var(--fg-dim)">(#${acct.id})</span>`
      : p.assigned_session
        ? `<code>${escapeHtml(p.assigned_session)}</code> <span style="color:var(--fg-dim)">(legacy session-file binding — will auto-migrate)</span>`
        : '<span style="color:var(--fg-dim)">— not assigned —</span>';
    return `
      <div class="card" data-label="${escAttr(p.label)}" style="padding:12px;margin-bottom:10px">
        <div style="display:flex;align-items:center;gap:10px;margin-bottom:8px">
          <strong style="font-size:14px">${p.label}</strong>
          ${verified}
          <span class="grow"></span>
          <button class="toolbar-btn" data-act="test">Test</button>
          <button class="toolbar-btn" data-act="remove" style="color:var(--err)">Remove</button>
        </div>
        <div style="font:12px/1.4 ui-monospace,monospace;color:var(--fg-dim);margin:4px 0">
          <span style="color:var(--fg)">creds:</span> <code>${escAttr(credLine)}</code>
        </div>
        ${p.notes ? `<div style="font-size:11px;color:var(--fg-dim);margin:4px 0">${escAttr(p.notes)}</div>` : ''}
        <div style="display:flex;align-items:center;gap:8px;margin-top:8px;flex-wrap:wrap">
          <span style="font-size:11px;color:var(--fg-dim)">assigned to:</span>
          ${assignedLabel}
        </div>
        <div style="display:flex;align-items:center;gap:8px;margin-top:8px">
          <select data-act="assign-select" style="flex:1;background:var(--bg);color:var(--fg);border:1px solid var(--border);border-radius:6px;padding:6px 8px;font:inherit;font-size:11px">
            ${acctOptions}
          </select>
          <button class="toolbar-btn" data-act="assign">Assign</button>
        </div>
        <div data-act="result" style="font:11px/1.4 ui-monospace,monospace;color:var(--fg-dim);margin-top:8px;white-space:pre-wrap"></div>
      </div>`;
  }).join('') || '<div class="empty">no proxies yet — add one below</div>';

  host.innerHTML = `
    ${rows}
    <details style="margin-top:14px">
      <summary style="cursor:pointer;color:var(--fg-dim);font-size:12px">+ add new proxy</summary>
      <div class="card" style="padding:12px;margin-top:8px;display:grid;grid-template-columns:repeat(4,1fr);gap:8px">
        <input id="np-label" placeholder="label (eg eu-2)" style="background:var(--bg);color:var(--fg);border:1px solid var(--border);border-radius:6px;padding:6px 8px;font:inherit;font-size:12px">
        <input id="np-host" placeholder="host" style="background:var(--bg);color:var(--fg);border:1px solid var(--border);border-radius:6px;padding:6px 8px;font:inherit;font-size:12px">
        <input id="np-port" placeholder="port" type="number" style="background:var(--bg);color:var(--fg);border:1px solid var(--border);border-radius:6px;padding:6px 8px;font:inherit;font-size:12px">
        <select id="np-scheme" style="background:var(--bg);color:var(--fg);border:1px solid var(--border);border-radius:6px;padding:6px 8px;font:inherit;font-size:12px">
          <option value="http">http</option>
          <option value="https">https</option>
          <option value="socks5">socks5</option>
        </select>
        <input id="np-user" placeholder="username (optional)" style="background:var(--bg);color:var(--fg);border:1px solid var(--border);border-radius:6px;padding:6px 8px;font:inherit;font-size:12px;grid-column:span 2">
        <input id="np-pass" placeholder="password (optional)" style="background:var(--bg);color:var(--fg);border:1px solid var(--border);border-radius:6px;padding:6px 8px;font:inherit;font-size:12px;grid-column:span 2">
        <input id="np-notes" placeholder="notes (provider, city, ASN…)" style="background:var(--bg);color:var(--fg);border:1px solid var(--border);border-radius:6px;padding:6px 8px;font:inherit;font-size:12px;grid-column:span 3">
        <button id="np-save" class="toolbar-btn" style="background:var(--accent);color:#000;font-weight:700;border-color:var(--accent)">Add proxy</button>
      </div>
    </details>`;

  // Pre-select the currently-assigned account in each row's dropdown.
  host.querySelectorAll('[data-label]').forEach(card => {
    const label = card.getAttribute('data-label');
    const p = proxies.find(x => x.label === label);
    const sel = card.querySelector('[data-act="assign-select"]');
    if (sel && p && p.assigned_account) sel.value = p.assigned_account.id;
  });

  host.addEventListener('click', async (e) => {
    const btn = e.target.closest('button[data-act]');
    if (!btn) return;
    const card = btn.closest('[data-label]');
    if (!card) return;
    const label = card.getAttribute('data-label');
    const result = card.querySelector('[data-act="result"]');
    const setMsg = (txt, color) => { if (result) { result.textContent = txt; result.style.color = color || 'var(--fg-dim)'; } };
    const act = btn.getAttribute('data-act');
    try {
      if (act === 'test') {
        setMsg('probing…');
        const r = await fetch(`/admin/proxies/${encodeURIComponent(label)}/test`, { method: 'POST' });
        const d = await r.json();
        if (d.ok) {
          setMsg(`OK · egress ${d.ip} · ${d.geo || ''}`, 'var(--ok)');
          await renderProxies();
        } else {
          setMsg(`FAIL: ${d.error || 'unknown'}`, 'var(--err)');
        }
      } else if (act === 'assign') {
        const sel = card.querySelector('[data-act="assign-select"]');
        const account_id = sel ? (sel.value || null) : null;
        setMsg('assigning…');
        const r = await fetch('/admin/proxies/assign', {
          method: 'POST',
          headers: { 'content-type': 'application/json' },
          // Send account_id (the new scheme). The server clears any legacy
          // session-file binding on the same proxy when this lands.
          body: JSON.stringify({ label, account_id }),
        });
        const d = await r.json();
        if (r.ok) {
          const nick = account_id
            ? (((data.accounts || []).find(a => a.id === account_id) || {}).nickname || account_id)
            : '(unassigned)';
          setMsg(`assigned → ${nick} · OFClient pool flushed`, 'var(--ok)');
          await renderProxies();
        } else {
          setMsg(`FAIL: ${d.detail || r.status}`, 'var(--err)');
        }
      } else if (act === 'remove') {
        if (!confirm(`Remove proxy "${label}"?`)) return;
        const r = await fetch(`/admin/proxies/${encodeURIComponent(label)}`, { method: 'DELETE' });
        if (r.ok) await renderProxies();
        else setMsg(`FAIL: ${(await r.json()).detail || r.status}`, 'var(--err)');
      }
    } catch (err) {
      setMsg(`error: ${err.message}`, 'var(--err)');
    }
  });

  const saveBtn = host.querySelector('#np-save');
  if (saveBtn) {
    saveBtn.onclick = async () => {
      const body = {
        label:    host.querySelector('#np-label').value.trim(),
        host:     host.querySelector('#np-host').value.trim(),
        port:     parseInt(host.querySelector('#np-port').value, 10),
        scheme:   host.querySelector('#np-scheme').value,
        username: host.querySelector('#np-user').value.trim() || null,
        password: host.querySelector('#np-pass').value.trim() || null,
        notes:    host.querySelector('#np-notes').value.trim(),
      };
      if (!body.label || !body.host || !body.port) {
        alert('label, host, port required'); return;
      }
      const r = await fetch('/admin/proxies', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (r.ok) await renderProxies();
      else alert(`FAIL: ${(await r.json()).detail || r.status}`);
    };
  }
}

loaders.profile = async () => {
  const reload = async () => {
    const host = $('#profile-body');
    host.innerHTML = '<div class="empty">loading…</div>';
    const todayIso = new Date().toISOString().slice(0, 10);
    const nextYearIso = (() => { const d = new Date(); d.setFullYear(d.getFullYear()+1); return d.toISOString().slice(0,10); })();
    const tz = Intl.DateTimeFormat().resolvedOptions().timeZone || 'Europe/Ljubljana';
    const [me, settings, sched, lists] = await Promise.all([
      fetchJson('/api/of/v2/users/me'),
      fetchJson('/api/of/v2/users/me/settings'),
      fetchJson(`/api/of/v2/schedules?limit=50&publish_date=${todayIso}&publish_date_end=${nextYearIso}&time_zone=${encodeURIComponent(tz)}`),
      fetchJson('/api/of/v2/lists?limit=50'),
    ]);
    if (!me.ok) return renderError(host, me.body);
    const u = me.body;
    const scheduledList = sched.ok ? (sched.body.list || []) : [];
    const userLists = lists.ok ? (Array.isArray(lists.body) ? lists.body : (lists.body.list || [])) : [];

    host.innerHTML = `
      <div style="display:flex;gap:24px;align-items:center;margin-bottom:24px">
        <div class="avatar" style="width:120px;height:120px;font-size:48px${u.avatar?`;background-image:url('${u.avatar}')`:''}">${u.avatar?'':escapeHtml((u.name||u.username||'?')[0]).toUpperCase()}</div>
        <div style="flex:1">
          <h2 style="margin:0">${escapeHtml(u.name || u.username || '')}</h2>
          <div style="color:var(--fg-dim)">@${escapeHtml(u.username||'')} · #${u.id} · ${escapeHtml(u.location || '')}</div>
          <div style="margin-top:6px;font-size:12px">${escapeHtml(stripHtml(u.about||'').slice(0,300))}</div>
        </div>
      </div>

      <div class="stat-cards">
        <div class="stat-card"><div class="label">Posts</div><div class="value">${u.postsCount ?? 0}</div></div>
        <div class="stat-card"><div class="label">Photos</div><div class="value">${u.photosCount ?? 0}</div></div>
        <div class="stat-card"><div class="label">Videos</div><div class="value">${u.videosCount ?? 0}</div></div>
        <div class="stat-card"><div class="label">Followers</div><div class="value">${u.favoritedCount ?? 0}</div></div>
        <div class="stat-card"><div class="label">Subscribe price</div><div class="value">${fmtMoney(u.subscribePrice||0)}</div></div>
        <div class="stat-card"><div class="label">Tips $</div><div class="value" style="font-size:14px">${u.tipsMin}–${u.tipsMax}</div></div>
      </div>

      <h3 style="margin-top:24px">Quick actions</h3>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-top:8px">
        <div class="card" style="padding:14px">
          <strong>📝 Create post</strong>
          <div style="color:var(--fg-dim);font-size:11px;margin-bottom:8px">POST /posts · creates a public feed post</div>
          <textarea id="act-post-text" rows="3" placeholder="Post text…" style="width:100%;background:var(--bg);color:var(--fg);border:1px solid var(--border);border-radius:6px;padding:6px;font:inherit"></textarea>
          <div style="display:flex;gap:8px;margin-top:8px">
            <input id="act-post-price" type="number" min="0" step="0.01" placeholder="price ($, 0=free)" style="flex:1;background:var(--bg);color:var(--fg);border:1px solid var(--border);border-radius:6px;padding:4px 8px">
            <button id="act-post-fire" class="toolbar-btn" style="background:var(--accent);color:#000;font-weight:700;border-color:var(--accent)">Create post</button>
          </div>
        </div>

        <div class="card" style="padding:14px">
          <strong>💬 Send / schedule message</strong>
          <div style="color:var(--fg-dim);font-size:11px;margin-bottom:8px">POST /chats/{id}/messages · single fan</div>
          <input id="act-msg-chat" placeholder="chat_id (fan user id)" value="117183" style="width:100%;background:var(--bg);color:var(--fg);border:1px solid var(--border);border-radius:6px;padding:4px 8px;margin-bottom:6px">
          <textarea id="act-msg-text" rows="2" placeholder="Message text…" style="width:100%;background:var(--bg);color:var(--fg);border:1px solid var(--border);border-radius:6px;padding:6px;font:inherit"></textarea>
          <div style="display:flex;gap:8px;margin-top:8px;align-items:center">
            <input id="act-msg-price" type="number" min="0" step="0.01" placeholder="price" style="flex:1;background:var(--bg);color:var(--fg);border:1px solid var(--border);border-radius:6px;padding:4px 8px">
            <input id="act-msg-when" type="datetime-local" style="flex:2;background:var(--bg);color:var(--fg);border:1px solid var(--border);border-radius:6px;padding:4px 8px" title="leave empty for immediate">
            <button id="act-msg-fire" class="toolbar-btn">Send</button>
          </div>
        </div>

        <div class="card" style="padding:14px;grid-column:span 2">
          <strong>📣 Mass message</strong>
          <div style="color:var(--fg-dim);font-size:11px;margin-bottom:8px">POST /messages/queue · broadcast / schedule for many fans (named lists, not 0/1/2)</div>
          <textarea id="act-mass-text" rows="2" placeholder="Mass text…" style="width:100%;background:var(--bg);color:var(--fg);border:1px solid var(--border);border-radius:6px;padding:6px;font:inherit"></textarea>
          <div style="margin-top:10px;font-size:11px;color:var(--fg-dim)">PICK LISTS (audience):</div>
          <div id="act-mass-lists-pick" style="display:flex;flex-wrap:wrap;gap:6px;margin-top:6px">
            ${userLists.length ? userLists.map(l => `
              <label style="display:inline-flex;align-items:center;gap:6px;background:var(--bg-elev-2);border:1px solid var(--border);border-radius:8px;padding:4px 8px;font-size:12px;cursor:pointer">
                <input type="checkbox" data-list-id="${escapeHtml(String(l.id))}" data-list-name="${escapeHtml(l.name)}">
                <strong>${escapeHtml(l.name)}</strong>
                <small style="color:var(--fg-dim)">${l.type === 'custom' ? 'custom' : 'built-in'} · ${l.usersCount ?? 0}</small>
              </label>`).join('') : '<em style="color:var(--fg-dim)">no lists loaded</em>'}
          </div>
          <div style="display:flex;gap:8px;margin-top:10px;align-items:center;flex-wrap:wrap">
            <input id="act-mass-userids" placeholder="extra user ids comma-sep (optional)" style="flex:2;min-width:160px;background:var(--bg);color:var(--fg);border:1px solid var(--border);border-radius:6px;padding:4px 8px">
            <input id="act-mass-price" type="number" min="0" step="0.01" placeholder="price ($)" style="flex:1;min-width:80px;background:var(--bg);color:var(--fg);border:1px solid var(--border);border-radius:6px;padding:4px 8px">
            <input id="act-mass-when" type="datetime-local" style="flex:1;min-width:180px;background:var(--bg);color:var(--fg);border:1px solid var(--border);border-radius:6px;padding:4px 8px" title="leave empty to send NOW; pick to schedule">
            <button id="act-mass-fire" class="toolbar-btn">Broadcast / Schedule</button>
          </div>
        </div>

        <div class="card" style="padding:14px;grid-column:span 2">
          <strong>📅 Scheduled items <small style="color:var(--fg-dim);font-weight:400">/schedules · ${scheduledList.length}</small></strong>
          <div style="color:var(--fg-dim);font-size:11px;margin-bottom:8px">live calendar (open the Queue tab for a full table + cancel)</div>
          ${scheduledList.length ? scheduledList.slice(0, 10).map(s => {
            const e = s.entity || {};
            const text = stripHtml(e.text || e.rawText || '');
            return `<div style="font-size:11px;padding:6px 0;border-bottom:1px solid var(--border);display:flex;gap:10px;align-items:center">
              <strong style="min-width:140px">${fmtDate(s.publishDateTime || e.scheduledAt || e.postedAt)}</strong>
              <span class="pill" style="font-size:10px">${s.type === 'post' ? '📝 post' : '💬 chat'}</span>
              <span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${escapeHtml(text.slice(0,100)) || '<em style="color:var(--fg-dim)">(no text)</em>'}</span>
            </div>`;
          }).join('') : '<div class="empty">nothing scheduled</div>'}
        </div>
      </div>

      <h3 style="margin-top:24px">Settings (excerpt)</h3>
      ${settings.ok ? `<pre style="background:var(--bg-elev);border:1px solid var(--border);border-radius:8px;padding:12px;overflow:auto;max-height:300px;font-size:11px">${escapeHtml(JSON.stringify(settings.body, null, 2).slice(0, 3500))}…</pre>` : '<div class="empty">settings failed</div>'}
    `;

    // Wire actions — every write goes through confirm() first
    $('#act-post-fire').onclick = async () => {
      const text = $('#act-post-text').value.trim();
      const price = Number($('#act-post-price').value || 0);
      if (!text) { alert('post text empty'); return; }
      if (!confirm(`Create a public post on your feed?\n\n${text.slice(0,200)}\n\nprice: $${price}`)) return;
      const r = await fetch('/api/of/v2/posts', {
        method: 'POST', headers: {'content-type':'application/json'},
        body: JSON.stringify({ text, price }),
      });
      alert(`${r.status} ${r.ok?'OK':'failed'}: ${(await r.text()).slice(0,300)}`);
    };

    $('#act-msg-fire').onclick = async () => {
      const chatId = $('#act-msg-chat').value.trim();
      const text = $('#act-msg-text').value.trim();
      const price = Number($('#act-msg-price').value || 0);
      const whenLocal = $('#act-msg-when').value;
      if (!chatId || !text) { alert('chat_id + text required'); return; }
      const scheduled = whenLocal ? new Date(whenLocal).toISOString() : null;
      const label = scheduled ? `schedule for ${whenLocal}` : 'send NOW';
      if (!confirm(`${label} to chat ${chatId}?\n\n${text.slice(0,200)}\n\nprice: $${price}`)) return;
      const path = scheduled
        ? `/api/of/v2/chats/${encodeURIComponent(chatId)}/messages/scheduled`
        : `/api/of/v2/chats/${encodeURIComponent(chatId)}/messages`;
      const body = scheduled
        ? { text, price, scheduled_date: scheduled }
        : { text, price };
      const r = await fetch(path, { method: 'POST', headers: {'content-type':'application/json'}, body: JSON.stringify(body) });
      alert(`${r.status} ${r.ok?'OK':'failed'}: ${(await r.text()).slice(0,300)}`);
    };

    $('#act-mass-fire').onclick = async () => {
      const text = $('#act-mass-text').value.trim();
      const price = Number($('#act-mass-price').value || 0);
      const picked = [...host.querySelectorAll('#act-mass-lists-pick input:checked')];
      const lists = picked.map(c => c.dataset.listId);
      const listNames = picked.map(c => c.dataset.listName);
      const extraIds = $('#act-mass-userids').value.split(',').map(s=>s.trim()).filter(Boolean).map(Number).filter(n=>Number.isFinite(n));
      const whenLocal = $('#act-mass-when').value;
      if (!text) { alert('text required'); return; }
      if (!lists.length && !extraIds.length) { alert('pick at least one list OR provide extra user ids'); return; }
      const scheduled = whenLocal ? new Date(whenLocal).toISOString() : null;
      const audience = [listNames.join(', ') || null, extraIds.length ? `${extraIds.length} extra users` : null].filter(Boolean).join(' + ');
      const verb = scheduled ? `SCHEDULE for ${whenLocal}` : 'BROADCAST NOW';
      if (!confirm(`${verb} to [${audience}]?\n\n${text.slice(0,200)}\n\nprice: $${price}\n\nThis sends to potentially MANY fans.`)) return;
      // Schedule + immediate broadcast both go through POST /messages/queue;
      // the only difference is whether scheduled_date is set.
      const body = {
        text, price,
        user_lists: lists,
        user_ids: extraIds,
      };
      if (scheduled) body.scheduled_date = scheduled;
      // Route: scheduled uses the strict /messages/queue with Pydantic body;
      // immediate broadcast goes through the wrapper /chats/messages which
      // accepts our extras + maps to the same OF endpoint server-side.
      const route = scheduled ? '/api/of/v2/messages/queue' : '/api/of/v2/chats/messages';
      const r = await fetch(route, {
        method: 'POST', headers: {'content-type':'application/json'},
        body: JSON.stringify(scheduled
          ? { text, price, user_lists: lists, user_ids: extraIds, scheduled_date: scheduled }
          : { text, price, user_lists: lists, included_users: extraIds }),
      });
      alert(`${r.status} ${r.ok?'OK':'failed'}: ${(await r.text()).slice(0,300)}`);
      if (r.ok) await reload();
    };
  };
  $('#profile-reload').onclick = reload;
  await reload();
};

// ── POSTS ──
loaders.posts = async () => {
  const reload = async () => {
    const host = $('#posts-body');
    host.innerHTML = '<div class="empty">loading…</div>';
    const { ok, body } = await fetchJson('/api/of/v2/posts?limit=20');
    if (!ok) { renderError(host, body); return; }
    const posts = Array.isArray(body) ? body : (body.list || []);
    $('#posts-meta').textContent = `${posts.length} posts`;
    if (!posts.length) { renderEmpty(host, 'no posts'); return; }
    host.innerHTML = posts.map(p => {
      const thumb = p.preview?.[0] || p.media?.[0]?.preview || p.media?.[0]?.thumb || p.media?.[0]?.src;
      const text = stripHtml(p.text) || '(no text)';
      return `<div class="post-row">
        <div class="thumb-sm" ${thumb ? `style="background-image:url('${thumb}')"` : ''}></div>
        <div style="flex:1;min-width:0">
          <div>${escapeHtml(text.slice(0, 200))}</div>
          <div class="meta-line">id ${p.id} · ${fmtDate(p.postedAt)} · ${p.mediaCount || 0} media · ❤ ${p.favoritesCount || 0} · 💬 ${p.commentsCount || 0}${p.price ? ' · $' + p.price : ''}</div>
        </div>
      </div>`;
    }).join('');
  };
  $('#posts-reload').onclick = reload;
  await reload();
};

// ── VAULT ──
// Two fixes from last round of feedback:
//   1. Filter: OF needs `type=`, not `filter=` (latter is silently ignored)
//   2. Pagination: scroll-near-bottom loads the next 48 items
loaders.vault = (() => {
  let cursor = 0;
  let activeType = 'all';
  let loadingMore = false;
  let hasMore = true;

  const renderItem = (m) => {
    const src =
      m.files?.squarePreview?.url || m.files?.preview?.url ||
      m.files?.thumb?.url || m.preview || m.thumb || m.url;
    const kind = m.type || 'media';
    return `<div class="card">
      <div class="thumb" ${src ? `style="background-image:url('${src}')"` : ''}>
        <div class="thumb-overlay">${escapeHtml(kind)}${kind==='video' && m.duration ? ` · ${m.duration}s` : ''}</div>
      </div>
      <div class="card-body">id ${m.id}${m.hasUsedInCount ? ` · used ${m.hasUsedInCount}×` : ''}</div>
    </div>`;
  };

  const loadMore = async () => {
    if (loadingMore || !hasMore) return;
    loadingMore = true;
    const sent = $('#vault-sentinel');
    if (sent) sent.textContent = 'loading more…';
    try {
      const url = `/api/of/v2/vault/media?type=${encodeURIComponent(activeType)}&limit=48&offset=${cursor}`;
      const { ok, body } = await fetchJson(url);
      if (!ok) { if (sent) sent.textContent = 'error'; return; }
      const items = body.list || [];
      hasMore = !!body.hasMore;
      cursor += items.length;
      $('#vault-grid').insertAdjacentHTML('beforeend', items.map(renderItem).join(''));
      $('#vault-meta').textContent = `${cursor}${hasMore ? '+' : ''} loaded · type=${activeType}`;
      if (sent) sent.textContent = hasMore ? '↓ scroll for more' : '— end —';
    } finally { loadingMore = false; }
  };

  const fullReload = async () => {
    cursor = 0; hasMore = true;
    activeType = $('#vault-filter').value;
    $('#vault-grid').innerHTML = '';
    if (!$('#vault-sentinel')) {
      const s = document.createElement('div');
      s.id = 'vault-sentinel'; s.className = 'empty';
      s.style.gridColumn = '1/-1';
      $('#vault-grid').after(s);
    }
    await loadMore();
    // Bind scroll on the page-body container once
    const scroller = $('#vault-grid').closest('.page-body');
    if (scroller && !scroller.dataset.vaultBound) {
      scroller.addEventListener('scroll', () => {
        const nearBottom = scroller.scrollTop + scroller.clientHeight >= scroller.scrollHeight - 200;
        if (nearBottom) loadMore();
      });
      scroller.dataset.vaultBound = '1';
    }
  };

  return async () => {
    $('#vault-reload').onclick = fullReload;
    $('#vault-filter').onchange = fullReload;
    await fullReload();
  };
})();

// ── QUEUE (scheduled chats + posts) ──
// Primary source is /schedules (the calendar view OF's own "Queue" page uses).
// /schedules/later/{chat,post} are filtered "send-later" views that turned up
// empty on this account; we keep them as a secondary section only if non-empty.
// Items returned by /schedules wrap their real payload in `.entity`, so we
// flatten that for the table. Cancel uses DELETE /messages/queue/{entity.id}
// for chat items and DELETE /posts/{entity.id} for posts (OF's two paths).
loaders.queue = async () => {
  const tz = Intl.DateTimeFormat().resolvedOptions().timeZone || 'Europe/Ljubljana';
  // Default range: from today to +1 year (matches what OF's /my/queue does
  // when no date is selected — uses limit=4 + date range).
  const todayIso = () => new Date().toISOString().slice(0, 10);
  const inOneYear = () => {
    const d = new Date(); d.setFullYear(d.getFullYear() + 1);
    return d.toISOString().slice(0, 10);
  };
  let rangeStart = todayIso();
  let rangeEnd = inOneYear();

  const reload = async () => {
    const host = $('#queue-body');
    host.innerHTML = '<div class="empty">loading…</div>';

    const q = (path) => `${path}&time_zone=${encodeURIComponent(tz)}`;
    const [schedules, counters, laterChat, laterPost] = await Promise.all([
      fetchJson(q(`/api/of/v2/schedules?limit=50&publish_date=${rangeStart}&publish_date_end=${rangeEnd}`)),
      fetchJson(q(`/api/of/v2/schedules/counters?publish_date=${rangeStart}&publish_date_end=${rangeEnd}`)),
      fetchJson('/api/of/v2/schedules/later/chat?limit=10'),
      fetchJson('/api/of/v2/schedules/later/post?limit=10'),
    ]);

    if (!schedules.ok) { renderError(host, schedules.body); return; }
    const items = schedules.body.list || [];
    const chats = items.filter(i => i.type === 'chat');
    const posts = items.filter(i => i.type === 'post');
    const laterC = laterChat.ok ? (laterChat.body.list || []) : [];
    const laterP = laterPost.ok ? (laterPost.body.list || []) : [];

    $('#queue-meta').textContent =
      `${items.length} scheduled · ${chats.length} chats · ${posts.length} posts`;

    const countersList = counters.ok ? (counters.body.list || {}) : {};
    const counterDays = Object.entries(countersList)
      .sort(([a],[b]) => a.localeCompare(b))
      .map(([d, c]) => `<button class="toolbar-btn" data-day="${d}" title="filter to this day">
          ${d} <small style="color:var(--fg-dim)">
          ${c.chat ? `💬${c.chat}` : ''}${c.post ? ` 📝${c.post}` : ''}
        </small></button>`).join('');

    const audienceFor = (e) => {
      if (e.userIds?.length) return `${e.userIds.length} user${e.userIds.length>1?'s':''} (${e.userIds.slice(0,3).join(', ')}${e.userIds.length>3?'…':''})`;
      if (e.userLists?.length) return `lists: ${e.userLists.join(', ')}`;
      if (e.sentRulesExtra) return e.sentRulesExtra;
      return '—';
    };

    const row = (it) => {
      const e = it.entity || {};
      const when = it.publishDateTime || e.scheduledAt || e.postedAt;
      const text = stripHtml(e.text || e.rawText || '');
      const isChat = it.type === 'chat';
      return `<tr data-sched-id="${it.id}" data-entity-id="${e.id}" data-type="${it.type}">
        <td>${fmtDate(when)}</td>
        <td><span class="pill">${isChat ? '💬 chat' : '📝 post'}</span></td>
        <td>${escapeHtml(text.slice(0, 150)) || '<em style="color:var(--fg-dim)">(no text)</em>'}</td>
        <td class="num">${e.price ? fmtMoney(e.price) : '—'}</td>
        <td>${isChat ? audienceFor(e) : '<em style="color:var(--fg-dim)">feed post</em>'}</td>
        <td><small style="color:var(--fg-dim)">${e.mediaCount ? `📎${e.mediaCount}` : ''} sched#${it.id} · ent#${e.id}</small></td>
        <td><button class="toolbar-btn" data-cancel-type="${it.type}" data-entity-id="${e.id}" data-sched-id="${it.id}" style="color:var(--err);border-color:rgba(248,81,73,.3)">🗑</button></td>
      </tr>`;
    };

    host.innerHTML = `
      <div style="display:flex;gap:10px;align-items:center;margin-bottom:12px;flex-wrap:wrap">
        <label style="font-size:11px;color:var(--fg-dim)">from</label>
        <input id="q-start" type="date" value="${rangeStart}" class="toolbar-btn"/>
        <label style="font-size:11px;color:var(--fg-dim)">to</label>
        <input id="q-end" type="date" value="${rangeEnd}" class="toolbar-btn"/>
        <button id="q-apply" class="toolbar-btn">Apply</button>
        <span class="grow" style="flex:1"></span>
        <small style="color:var(--fg-dim)">tz: ${tz}</small>
      </div>

      ${counterDays ? `<div style="margin-bottom:14px">
        <div style="color:var(--fg-dim);font-size:11px;margin-bottom:6px">📅 Days with scheduled items (from /schedules/counters)</div>
        <div style="display:flex;flex-wrap:wrap;gap:6px">${counterDays}</div>
      </div>` : ''}

      <h3 style="margin-top:18px">📅 Scheduled items <small style="color:var(--fg-dim);font-weight:400">/schedules · ${items.length}</small></h3>
      ${items.length ? `<table class="data">
        <thead><tr><th>when</th><th>type</th><th>text</th><th class="num">price</th><th>audience / target</th><th>refs</th><th></th></tr></thead>
        <tbody>${items.map(row).join('')}</tbody></table>` : '<div class="empty">nothing scheduled in this range</div>'}

      ${laterC.length || laterP.length ? `<h3 style="margin-top:24px">⏭ "Send later" buckets <small style="color:var(--fg-dim);font-weight:400">/schedules/later/{chat,post}</small></h3>
      <div style="font-size:12px;color:var(--fg-dim);margin-bottom:8px">${laterC.length} chats · ${laterP.length} posts</div>` : ''}
    `;

    // Day-filter quick buttons
    host.querySelectorAll('button[data-day]').forEach(b => b.onclick = () => {
      rangeStart = rangeEnd = b.dataset.day;
      reload();
    });

    // Apply range button
    $('#q-apply').onclick = () => {
      rangeStart = $('#q-start').value || rangeStart;
      rangeEnd = $('#q-end').value || rangeEnd;
      reload();
    };

    // Cancel: chats → DELETE /messages/queue/{entity.id} (the message id),
    // posts → DELETE /posts/{entity.id} (the post id). Confirmed by OF UI XHRs.
    host.querySelectorAll('button[data-cancel-type]').forEach(b => {
      b.onclick = async () => {
        const t = b.dataset.cancelType;
        const eid = b.dataset.entityId;
        if (!confirm(`Cancel scheduled ${t} (entity #${eid})?`)) return;
        const path = t === 'post'
          ? `/api/of/v2/posts/${eid}`
          : `/api/of/v2/messages/queue/${eid}`;
        const r = await fetch(path, { method: 'DELETE' });
        if (!r.ok) { alert(`cancel failed: ${r.status} ${(await r.text()).slice(0,200)}`); return; }
        await reload();
      };
    });
  };
  $('#queue-reload').onclick = reload;
  await reload();
};

// ─── Chat view ─────────────────────────────────────────────────
// Chat list state lives here so the filter chips / search box / infinite
// scroll all share a single source of truth. Each chip / search keystroke
// produces a "query" object; the next page picks up from `nextOffset`.
state.chatList = {
  pageSize: 20,
  filter: null,        // 'unread' | 'pinned' | 'priority' | 'with_tips' | null
  listId: null,        // custom folder (list_id) when a pinned folder is active
  search: '',          // free-text search by fan
  offset: 0,
  hasMore: true,
  loading: false,
  reqSeq: 0,           // race-protection: each new query bumps this
  autofillBurst: 0,    // pages auto-fetched since last manual action; capped to stop runaway pagination
  capOffset: 200,      // hard scroll cap; "load more" click bumps it by SCROLL_CAP_STEP. Search bypasses.
};
state.chatFolders = [];   // populated from /chats/folders
state.chatChipCounts = {}; // {filter|listId → unread count} fed by realtime events
state.unreadOnly = false;  // kept for back-compat: the old toggle now maps to filter='unread'

// Built-in OF chat filters. Verified live: pinned / priority / unread all
// return valid responses. "With Tips" is in the OF mobile UI but its server
// filter name is unknown — every guess (with_tips, withTips, tipped, tips)
// returns "Invalid filter" 400, so the chip is omitted until we capture the
// real curl from an account that has tipped fans.
const CHAT_BUILTIN_CHIPS = [
  { key: 'all',       label: 'All',       filter: null,         icon: '' },
  { key: 'pinned',    label: 'Pinned',    filter: 'pinned',     icon: '📌' },
  { key: 'priority',  label: 'Priority',  filter: 'priority',   icon: '⚡' },
  { key: 'unread',    label: 'Unread',    filter: 'unread',     icon: '●' },
];

async function loadChats() {
  // Top-level reset entry-point — wires search/chips/scroll once, then loads page 1.
  if (!loadChats._wired) {
    _wireChatListEvents();
    loadChats._wired = true;
    // Fetch the user's pinned folders so they appear as extra chips
    refreshChatFolders().catch(() => {});
  }
  state.chatList.offset = 0;
  state.chatList.hasMore = true;
  state.chatList.reqSeq += 1;
  state.chatList.autofillBurst = 0;
  state.chatList.capOffset = SCROLL_CAP_STEP;
  $('#chats').innerHTML = '<div class="empty">loading…</div>';
  // Reset the sentinel text (might still say "scanned X chats" from a prior search)
  const sentinel = $('#chats-sentinel');
  if (sentinel) sentinel.textContent = 'loading more…';
  await _fetchChatsPage({ replace: true, seq: state.chatList.reqSeq });
  _renderChatChips();
}

async function refreshChatFolders() {
  try {
    const r = await fetch('/api/of/v2/chats/folders?limit=20');
    if (!r.ok) return;
    const d = await r.json();
    state.chatFolders = (d.list || []).filter(f => f.isPinnedToChat);
    _renderChatChips();
  } catch (e) {
    console.warn('chat folders fetch failed:', e);
  }
}

function _renderChatChips() {
  const host = $('#chat-chips');
  if (!host) return;
  const cl = state.chatList;
  const isActive = (chip) => {
    if (chip.listId !== undefined) return cl.listId === chip.listId;
    return cl.listId === null && cl.filter === (chip.filter ?? null);
  };
  const builtins = CHAT_BUILTIN_CHIPS.map(c => ({
    ...c, listId: undefined,
  }));
  const folders = state.chatFolders.map(f => ({
    key: 'folder:' + f.id, label: f.name || `List #${f.id}`,
    listId: String(f.id), icon: '📂', folder: f, filter: null,
  }));
  const chips = builtins.concat(folders);
  const chipsHtml = chips.map(c => {
    const cnt = (c.folder && c.folder.subscribersCount)
      ? `<span class="chip-count">${c.folder.subscribersCount}</span>` : '';
    const unpin = c.folder
      ? `<span class="chip-x" data-unpin="${c.folder.id}" title="Unpin folder from chat sidebar">×</span>`
      : '';
    return `<button class="chat-chip${isActive(c) ? ' active' : ''}" data-chip="${c.key}" data-list-id="${c.listId ?? ''}" data-filter="${c.filter ?? ''}">
      ${c.icon ? `<span>${c.icon}</span>` : ''}${escapeHtml(c.label)}${cnt}${unpin}
    </button>`;
  }).join('');
  // Trailing edit chip — opens the "pick folders to pin" picker.
  const editChip = `<button class="chat-chip" data-act="edit-folders" title="Pick folders to pin as chips" style="padding:4px 10px">✎</button>`;
  host.innerHTML = chipsHtml + editChip;
  host.querySelectorAll('.chat-chip').forEach(btn => {
    if (btn.dataset.act === 'edit-folders') {
      btn.onclick = (e) => { e.stopPropagation(); _openFolderPicker(btn); };
      return;
    }
    btn.onclick = (e) => {
      if (e.target.closest('[data-unpin]')) return; // unpin handler below catches it
      const lid = btn.dataset.listId || null;
      const flt = btn.dataset.filter || null;
      state.chatList.listId = lid;
      state.chatList.filter = flt;
      // Keep the legacy boolean in sync so any other code that reads it stays consistent
      state.unreadOnly = (flt === 'unread');
      loadChats();
    };
  });
  host.querySelectorAll('[data-unpin]').forEach(x => {
    x.onclick = async (e) => {
      e.stopPropagation();
      const lid = x.dataset.unpin;
      if (!confirm('Unpin this folder from the chat sidebar?')) return;
      try {
        const r = await fetch(`/api/of/v2/lists/${encodeURIComponent(lid)}/pin-chat`, {
          method: 'PATCH',
          headers: {'content-type': 'application/json'},
          body: JSON.stringify({ pinned: false }),
        });
        if (r.ok) {
          // If the active chip is the one we just unpinned, drop back to All
          if (state.chatList.listId === lid) {
            state.chatList.listId = null;
            state.chatList.filter = null;
          }
          await refreshChatFolders();
          loadChats();
        }
      } catch (err) { console.warn('unpin failed:', err); }
    };
  });
}

async function _openFolderPicker(anchorBtn) {
  // Close any existing picker first
  document.querySelectorAll('.folder-picker').forEach(el => el.remove());
  let lists = [];
  try {
    // The chat-folders endpoint returns ALL `can_pin_chat` lists (pinned + not).
    // We sort pinned-first so the user sees what's already chosen at the top.
    const r = await fetch('/api/of/v2/chats/folders?limit=50');
    const d = await r.json();
    lists = (d.list || []).slice().sort((a, b) => {
      const ap = a.isPinnedToChat ? 1 : 0;
      const bp = b.isPinnedToChat ? 1 : 0;
      if (ap !== bp) return bp - ap;
      return (a.name || '').localeCompare(b.name || '');
    });
  } catch (e) {
    alert(`Couldn't load folders: ${e.message}`);
    return;
  }

  const pop = document.createElement('div');
  pop.className = 'folder-picker';
  pop.innerHTML = `
    <div class="folder-picker-header">Pin folders to chat sidebar</div>
    <div class="folder-picker-body">
      ${lists.length ? lists.map(l => `
        <label class="folder-picker-row">
          <input type="checkbox" data-list-id="${l.id}" ${l.isPinnedToChat ? 'checked' : ''}>
          <span class="folder-picker-name">${escapeHtml(l.name || `List #${l.id}`)}</span>
          <span class="folder-picker-count">${l.usersCount ?? l.subscribersCount ?? 0} users</span>
        </label>
      `).join('') : '<div class="empty" style="padding:14px">No pinnable lists yet — create one in the Lists tab first.</div>'}
    </div>
    <div class="folder-picker-footer">
      <button data-act="close" class="toolbar-btn">Done</button>
    </div>
  `;
  // Position below the edit button
  const rect = anchorBtn.getBoundingClientRect();
  pop.style.position = 'fixed';
  pop.style.top = (rect.bottom + 6) + 'px';
  pop.style.left = Math.max(8, Math.min(rect.left, window.innerWidth - 320)) + 'px';
  document.body.appendChild(pop);

  pop.querySelectorAll('input[data-list-id]').forEach(cb => {
    cb.onchange = async () => {
      const lid = cb.dataset.listId;
      const pinned = cb.checked;
      cb.disabled = true;
      try {
        const r = await fetch(`/api/of/v2/lists/${encodeURIComponent(lid)}/pin-chat`, {
          method: 'PATCH', headers: {'content-type': 'application/json'},
          body: JSON.stringify({ pinned }),
        });
        if (!r.ok) {
          cb.checked = !pinned;
          alert(`pin failed: ${r.status}`);
        } else {
          await refreshChatFolders();   // re-render chip strip
        }
      } finally {
        cb.disabled = false;
      }
    };
  });
  pop.querySelector('[data-act="close"]').onclick = () => pop.remove();

  // Click-away closes
  const dismiss = (e) => {
    if (!pop.contains(e.target) && e.target !== anchorBtn) {
      pop.remove();
      document.removeEventListener('mousedown', dismiss);
    }
  };
  setTimeout(() => document.addEventListener('mousedown', dismiss), 0);
}

async function _fetchChatsPage({ replace = false, seq } = {}) {
  const cl = state.chatList;
  if (cl.loading || (!cl.hasMore && !replace)) return;
  // Defense-in-depth: even if a future code path forgets to gate, the cap
  // is enforced here. `replace=true` (a fresh loadChats) is always allowed
  // because it has already reset the cap to its baseline.
  if (!replace && _scrollCapped()) { _renderLoadMoreSentinel(); return; }
  cl.loading = true;
  const sentinel = $('#chats-sentinel');
  if (sentinel) sentinel.style.display = replace ? 'none' : 'block';
  const params = new URLSearchParams({
    limit: String(cl.pageSize), offset: String(cl.offset), order: 'recent',
  });
  if (cl.filter) params.set('filter', cl.filter);
  if (cl.listId) params.set('list_id', cl.listId);
  // OF's chat search param is `query=` (captured live from /my/chats/?q=…).
  // We also still filter client-side after fetch — that lets us match the
  // last-message text in addition to the fan's name/username.
  if (cl.search) params.set('query', cl.search);

  let chats = [], hasMore = false;
  try {
    const r = await fetch(`/api/of/v2/chats?${params.toString()}`);
    const d = await r.json();
    chats = d.list || [];
    // OF returns `hasMore` on the chats list payload — fall back to "page was full" if missing.
    hasMore = (typeof d.hasMore === 'boolean') ? d.hasMore : (chats.length >= cl.pageSize);
  } catch (e) {
    if (replace) $('#chats').innerHTML = `<div class="empty">error: ${escapeHtml(e.message)}</div>`;
    cl.loading = false;
    if (sentinel) sentinel.style.display = 'none';
    return;
  }

  // Race protection: if a newer query started while this one was in flight, drop the result.
  if (seq !== undefined && seq !== cl.reqSeq) {
    cl.loading = false;
    return;
  }

  // Hydrate fan names/avatars one batch per page (matches the existing single-page behavior)
  if (state.hydrate && chats.length) {
    const ids = [...new Set(chats.map(c => c.withUser?.id).filter(Boolean))]
      .filter(id => !state.userCache.has(id));
    if (ids.length) {
      const qs = ids.map(id => `ids=${id}`).join('&');
      try {
        const ur = await fetch(`/api/of/v2/users/list?${qs}&view=m`);
        const dict = await ur.json();
        for (const [k, u] of Object.entries(dict)) state.userCache.set(Number(k), u);
      } catch (e) { console.warn('users/list failed:', e); }
    }
  }

  // Client-side search filter, applied AFTER hydration so we can match names
  // and usernames OF returned in /users/list (chats themselves don't carry them).
  let visible = chats;
  if (cl.search) {
    const q = cl.search.toLowerCase();
    visible = chats.filter(c => {
      const uid = c.withUser?.id;
      const u = state.userCache.get(uid);
      const name = (u?.name || '').toLowerCase();
      const uname = (u?.username || '').toLowerCase();
      const last = stripHtml(c.lastMessage?.text || '').toLowerCase();
      return name.includes(q) || uname.includes(q) || last.includes(q);
    });
  }

  const root = $('#chats');
  if (replace) root.innerHTML = '';
  cl.offset += chats.length;            // advance regardless of client filter
  cl.hasMore = hasMore && chats.length > 0;
  for (const c of visible) _appendChatRow(root, c);

  if (replace && !root.children.length) {
    root.innerHTML = cl.search
      ? `<div class="empty">no matches yet — <a href="#" data-act="search-more" style="color:var(--accent)">load more pages</a> to keep searching</div>`
      : '<div class="empty">no chats</div>';
    const a = root.querySelector('[data-act="search-more"]');
    if (a) a.onclick = (e) => { e.preventDefault(); _autoFillUntilScrollable(); };
  }
  if (sentinel) sentinel.style.display = cl.hasMore ? 'block' : 'none';
  cl.loading = false;

  // If the rendered list isn't tall enough to scroll, the scroll-based
  // infinite-loader will never fire. Keep fetching until either the viewport
  // overflows (scroll handler can take over) or there are no more pages.
  // Also drives the "search through more pages" UX when client-side filter
  // hides everything from the current page.
  _autoFillUntilScrollable();
}

// Hard cap on how far the chats sidebar will paginate without a manual click.
// Search bypasses the cap (otherwise typo'd queries dead-end at 200 chats).
// "Load more" click extends the cap by another SCROLL_CAP_STEP.
const SCROLL_CAP_STEP = 200;
// Per-burst safety net so a CSS bug that keeps scrollHeight≤clientHeight can't
// chain hundreds of fetches before offset reaches the hard cap.
const AUTOFILL_BURST_LIMIT = 10;

function _renderLoadMoreSentinel() {
  const cl = state.chatList;
  const sentinel = $('#chats-sentinel');
  if (!sentinel) return;
  sentinel.style.display = 'block';
  sentinel.innerHTML = `loaded ${cl.offset} chats · <a href="#" data-act="load-more" style="color:var(--accent)">load more</a>`;
  const a = sentinel.querySelector('[data-act="load-more"]');
  if (a) a.onclick = (e) => {
    e.preventDefault();
    cl.capOffset = cl.offset + SCROLL_CAP_STEP;
    cl.autofillBurst = 0;
    sentinel.textContent = 'loading more…';
    _fetchChatsPage({ seq: cl.reqSeq });
  };
}

function _scrollCapped() {
  const cl = state.chatList;
  return !cl.search && cl.offset >= cl.capOffset;
}

function _autoFillUntilScrollable() {
  const cl = state.chatList;
  const aside = document.querySelector('aside.chats-side');
  if (!aside || !cl.hasMore || cl.loading) return;
  // If the chats sidebar isn't in the layout (user is on another view), its
  // clientHeight is 0, so the "tall enough" check below would never satisfy
  // and we'd page through the entire backlog in the background.
  if (aside.offsetParent === null || aside.clientHeight === 0) return;
  if (_scrollCapped()) { _renderLoadMoreSentinel(); return; }
  // Microtask delay so the DOM commits the just-appended rows before we measure.
  setTimeout(() => {
    const tallEnough = aside.scrollHeight > aside.clientHeight + 80;
    if (tallEnough || !cl.hasMore || cl.loading) return;
    if (_scrollCapped()) { _renderLoadMoreSentinel(); return; }

    cl.autofillBurst = (cl.autofillBurst || 0) + 1;
    if (cl.autofillBurst > AUTOFILL_BURST_LIMIT) {
      _renderLoadMoreSentinel();
      return;
    }

    if (cl.search) {
      const sentinel = $('#chats-sentinel');
      if (sentinel) {
        sentinel.style.display = 'block';
        sentinel.textContent = `searching… scanned ${cl.offset} chats`;
      }
    }
    _fetchChatsPage({ seq: cl.reqSeq });
  }, 0);
}

function _appendChatRow(root, c) {
  // If the search "no matches yet" placeholder is still showing from an
  // earlier empty page, clear it before appending the first real row that
  // arrived from a later page.
  const empty = root.querySelector('.empty');
  if (empty) empty.remove();

  const uid = c.withUser?.id;
  const u = state.userCache.get(uid);
  const display = u?.name || u?.username || `#${uid}`;
  const avatar = u?.avatar || u?.avatarThumbs?.c50;
  const initial = (display[0] || '?').toUpperCase();
  const last = stripHtml(c.lastMessage?.text).slice(0, 60);
  const unread = c.unreadMessagesCount || 0;

  const div = document.createElement('div');
  div.className = 'chat-row' + (uid === state.selectedChatId ? ' active' : '');
  div.dataset.chatId = uid;
  div.innerHTML = `
    <div class="avatar" ${avatar ? `style="background-image:url('${avatar}')"` : ''}>
      ${avatar ? '' : escapeHtml(initial)}
    </div>
    <div class="chat-meta">
      <div class="name">${escapeHtml(display)}</div>
      <div class="last">${escapeHtml(last) || '<em>(no text)</em>'}</div>
    </div>
    ${unread > 0 ? `<span class="badge">${unread}</span>` : ''}
  `;
  div.onclick = () => selectChat(uid, display);
  root.appendChild(div);
}

function _wireChatListEvents() {
  // Infinite scroll: when the sidebar scrolls within 200px of bottom, fetch next page.
  const aside = document.querySelector('aside.chats-side');
  if (aside) {
    aside.addEventListener('scroll', () => {
      const cl = state.chatList;
      if (cl.loading || !cl.hasMore) return;
      const nearBottom = aside.scrollTop + aside.clientHeight >= aside.scrollHeight - 200;
      if (!nearBottom) return;
      if (_scrollCapped()) { _renderLoadMoreSentinel(); return; }
      cl.autofillBurst = 0;
      _fetchChatsPage({ seq: cl.reqSeq });
    }, { passive: true });
  }
  // Debounced search — 250ms after the last keystroke, re-run the query.
  const searchEl = $('#chat-search');
  if (searchEl) {
    let t;
    searchEl.addEventListener('input', () => {
      clearTimeout(t);
      t = setTimeout(() => {
        const q = searchEl.value.trim();
        if (q === state.chatList.search) return;
        state.chatList.search = q;
        loadChats();
      }, 250);
    });
  }
}

async function selectChat(chatId, display) {
  if (state.streamAbort) state.streamAbort.abort();
  state.selectedChatId = chatId;
  state.oldestId = null;
  state.totalLoaded = 0;
  state.hasMore = false;

  for (const el of $$('.chat-row')) {
    el.classList.toggle('active', Number(el.dataset.chatId) === chatId);
  }

  $('#convo-title').textContent = display + ' · ' + chatId;
  $('#send-text').disabled = false;
  $('#send-text').placeholder = `Reply to ${display}…`;
  $('#send-btn').disabled = false;
  $('#msg-count').textContent = '0 msgs';
  // Enable chat-action buttons + wire them to the just-selected chatId
  for (const btn of [
    $('#chat-act-markread'), $('#chat-act-markunrd'), $('#chat-act-mute'), $('#chat-act-hide'),
    $('#chat-act-addlist'), $('#chat-act-notenick'),
    $('#send-attach-btn'), $('#send-price'),
  ]) {
    if (btn) btn.disabled = false;
  }
  // Upload label uses pointer-events to gate clicks (can't .disabled a <label>)
  const ulabel = $('#send-upload-label');
  if (ulabel) { ulabel.style.opacity = '1'; ulabel.style.pointerEvents = 'auto'; }

  $('#msg-list').innerHTML = '';
  setStreamStatus('opening stream…');
  await streamMessages(chatId, { fromId: null, initial: true });
}

async function streamMessages(chatId, { fromId = null, initial = false } = {}) {
  state.streamAbort = new AbortController();
  let pagesThisCall = 0;
  let buffer = '';
  const params = new URLSearchParams({ delay_ms: '200', max_pages: '5' });
  if (fromId != null) params.set('from_id', String(fromId));
  const url = `/api/of/v2/chats/${chatId}/messages/stream?${params}`;
  try {
    const resp = await fetch(url, { signal: state.streamAbort.signal });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += dec.decode(value, { stream: true });
      let nl;
      while ((nl = buffer.indexOf('\n')) >= 0) {
        const line = buffer.slice(0, nl).trim();
        buffer = buffer.slice(nl + 1);
        if (!line) continue;
        const page = JSON.parse(line);
        if (page.error) { setStreamStatus(`upstream error: ${page.upstream_status}`, true); continue; }
        const wasAtBottom = isScrolledToBottom();
        for (const m of page.messages) {
          prependMessage(m);
          if (state.oldestId == null || m.id < state.oldestId) state.oldestId = m.id;
        }
        state.totalLoaded += page.count;
        state.hasMore = page.hasMore;
        pagesThisCall++;
        $('#msg-count').textContent = `${state.totalLoaded} msgs${state.hasMore ? ' (more)' : ''}`;
        renderTopAffordance();
        if ((initial && pagesThisCall === 1) || wasAtBottom) scrollToBottom();
      }
    }
    if (state.totalLoaded === 0) setStreamStatus('no messages');
  } catch (e) {
    if (e.name === 'AbortError') return;
    setStreamStatus(`stream error: ${e.message}`, true);
  }
}

function renderTopAffordance() {
  const s = $('#stream-status');
  s.innerHTML = '';
  s.style.color = '';
  if (state.hasMore) {
    const btn = document.createElement('button');
    btn.className = 'load-older';
    btn.textContent = '↑ Load older messages';
    btn.onclick = async () => {
      btn.disabled = true;
      btn.textContent = 'loading older…';
      await streamMessages(state.selectedChatId, { fromId: state.oldestId, initial: false });
    };
    s.appendChild(btn);
  } else {
    s.textContent = '— top of history —';
  }
}

// Build a single message bubble. Pin and like buttons exercise the verified
// /messages/{id}/pin and /messages/{id}/like writes. We use POST/DELETE on
// each toggle; the buttons reflect the optimistic state and revert on error.
function buildMessageNode(m, { isMine }) {
  const ts = new Date(m.createdAt).toLocaleString();
  const div = document.createElement('div');
  div.className = 'msg' + (isMine ? ' mine' : '');
  // OF returns isLiked + isPinned on the message object after fetch.
  const liked = !!m.isLiked;
  const pinned = !!m.isPinned;
  div.innerHTML = `
    <div class="text">${m.text || '<em>(no text)</em>'}</div>
    <div class="meta">
      ${escapeHtml(ts)} · id ${m.id}${m.price > 0 ? ` · $${m.price}` : ''}
      <span class="actions">
        ${!isMine ? `<button data-act="like"  class="${liked ? 'on' : ''}" title="like">${liked ? '♥' : '♡'}</button>` : ''}
        <button data-act="pin"  class="${pinned ? 'on' : ''}" title="pin to chat">📌</button>
        ${isMine ? `<button data-act="unsend" title="unsend (within edit window)">🗑</button>` : ''}
      </span>
    </div>
  `;
  div.querySelectorAll('.actions button').forEach(btn => {
    btn.addEventListener('click', (e) => onMessageAction(e, m, div));
  });
  return div;
}

async function onMessageAction(e, m, host) {
  const btn = e.currentTarget;
  const act = btn.dataset.act;
  const wasOn = btn.classList.contains('on');
  btn.disabled = true;
  let url, method;
  if (act === 'like')   { url = `/api/of/v2/messages/${m.id}/like`; method = wasOn ? 'DELETE' : 'POST'; }
  if (act === 'pin')    { url = `/api/of/v2/messages/${m.id}/pin`;  method = wasOn ? 'DELETE' : 'POST'; }
  if (act === 'unsend') {
    if (!confirm(`Unsend message #${m.id}? This deletes it from OF.`)) { btn.disabled = false; return; }
    url = `/api/of/v2/messages/${m.id}`; method = 'DELETE';
  }
  try {
    const r = await fetch(url, { method });
    if (!r.ok) throw new Error(`HTTP ${r.status}: ${(await r.text()).slice(0, 200)}`);
    if (act === 'unsend') { host.remove(); return; }
    btn.classList.toggle('on', !wasOn);
  } catch (err) {
    alert(`${act} failed: ${err.message}`);
  } finally {
    btn.disabled = false;
  }
}

function prependMessage(m) {
  const list = $('#msg-list');
  const div = buildMessageNode(m, { isMine: m.fromUser?.id === state.myUserId });
  list.insertBefore(div, list.firstChild);
}

function appendOwnMessage(m) {
  const list = $('#msg-list');
  list.appendChild(buildMessageNode(m, { isMine: true }));
  scrollToBottom();
}

function setStreamStatus(text, isError = false) {
  const s = $('#stream-status');
  s.textContent = text;
  s.style.color = isError ? 'var(--err)' : 'var(--fg-dim)';
}

function isScrolledToBottom() {
  const box = $('#messages');
  return box.scrollTop + box.clientHeight >= box.scrollHeight - 40;
}

function scrollToBottom() {
  const box = $('#messages');
  requestAnimationFrame(() => { box.scrollTop = box.scrollHeight; });
}

// Compose state for vault attachments
state.attachedMediaIds = [];

// Attachments can be either:
//   - a numeric vault id  (existing vault item, or dedupe hit on upload)
//   - an object {processId, host, name, ...}  (fresh upload claim)
// Both forms are valid for OF's `mediaFiles` array.
function attachmentLabel(a) {
  if (typeof a === 'number' || typeof a === 'string') return `vault#${a}`;
  if (a && a.processId) return `📎 ${escapeHtml(a.name || a.processId)}`;
  return '?';
}
function renderAttachments() {
  const host = $('#send-attachments');
  if (!state.attachedMediaIds.length) { host.style.display = 'none'; return; }
  host.style.display = '';
  host.innerHTML = `<strong>attached:</strong> ${state.attachedMediaIds.map((a, i) =>
    `<span style="background:var(--bg-elev-2);border:1px solid var(--border);border-radius:6px;padding:2px 8px;margin-right:4px">${attachmentLabel(a)} <a data-rm-idx="${i}" style="cursor:pointer;color:var(--err)">×</a></span>`
  ).join('')}`;
  host.querySelectorAll('a[data-rm-idx]').forEach(a => a.onclick = () => {
    const idx = Number(a.dataset.rmIdx);
    state.attachedMediaIds = state.attachedMediaIds.filter((_, i) => i !== idx);
    renderAttachments();
  });
}

// Vault picker — sidebar of folders + main grid of media, multi-select toggling
async function openVaultPicker() {
  const existing = document.getElementById('vault-picker-backdrop');
  if (existing) existing.remove();
  const backdrop = document.createElement('div');
  backdrop.id = 'vault-picker-backdrop';
  backdrop.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.7);z-index:9999;display:flex;align-items:center;justify-content:center;padding:40px';
  backdrop.innerHTML = `
    <div style="background:var(--bg-elev);border:1px solid var(--border);border-radius:12px;width:min(1100px,100%);max-height:80vh;overflow:hidden;display:grid;grid-template-rows:auto 1fr;grid-template-columns:240px 1fr;grid-template-areas:'header header' 'side main'">
      <div style="grid-area:header;padding:12px 16px;border-bottom:1px solid var(--border);display:flex;gap:10px;align-items:center">
        <strong>📎 Vault picker</strong>
        <select id="vp-type" class="toolbar-btn">
          <option value="all">all types</option>
          <option value="photo">photo</option>
          <option value="video">video</option>
          <option value="audio">audio</option>
          <option value="gif">gif</option>
        </select>
        <span class="grow" style="flex:1"></span>
        <span id="vp-meta" class="pill"></span>
        <button id="vp-done" class="toolbar-btn" style="background:var(--accent);color:#000;font-weight:700;border-color:var(--accent)">Done</button>
        <button id="vp-cancel" class="toolbar-btn">Cancel</button>
      </div>
      <div id="vp-side" style="grid-area:side;border-right:1px solid var(--border);overflow-y:auto;padding:8px"></div>
      <div id="vp-grid" style="grid-area:main;overflow-y:auto;padding:16px;display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:10px;align-content:flex-start"></div>
    </div>`;
  document.body.appendChild(backdrop);

  const tentative = new Set(state.attachedMediaIds.map(String));
  let activeListId = null;   // null = all items, otherwise filter by list_id

  const loadFolders = async () => {
    const side = $('#vp-side');
    side.innerHTML = '<div class="empty">loading…</div>';
    const r = await fetch('/api/of/v2/vault/lists?view=main&limit=50');
    if (!r.ok) { side.innerHTML = `<div class="empty">${r.status}</div>`; return; }
    const lists = (await r.json()).list || [];
    side.innerHTML = `
      <button data-list="" class="toolbar-btn ${activeListId === null ? 'on' : ''}" style="width:100%;text-align:left;margin-bottom:4px">📂 All vault</button>
      ${lists.map(l => `
        <button data-list="${l.id}" class="toolbar-btn ${activeListId === l.id ? 'on' : ''}" style="width:100%;text-align:left;margin-bottom:4px">
          📁 ${escapeHtml(l.name)} <small style="color:var(--fg-dim)">${l.photosCount + l.videosCount + l.gifsCount + l.audiosCount} items</small>
        </button>`).join('')}`;
    side.querySelectorAll('button[data-list]').forEach(b => {
      b.onclick = () => {
        activeListId = b.dataset.list ? Number(b.dataset.list) : null;
        loadFolders();   // refresh active styling
        reload();
      };
    });
  };

  const reload = async () => {
    const t = $('#vp-type').value;
    const grid = $('#vp-grid');
    grid.innerHTML = '<div class="empty" style="grid-column:1/-1">loading…</div>';
    const params = new URLSearchParams({ type: t, limit: 48 });
    if (activeListId !== null) params.set('list_id', String(activeListId));
    const r = await fetch(`/api/of/v2/vault/media?${params}`);
    if (!r.ok) { grid.innerHTML = `<div class="empty">${r.status}</div>`; return; }
    const items = (await r.json()).list || [];
    $('#vp-meta').textContent = `${items.length} items${activeListId !== null ? ' · in folder' : ''}`;
    if (!items.length) { grid.innerHTML = '<div class="empty" style="grid-column:1/-1">empty</div>'; return; }
    grid.innerHTML = items.map(m => {
      const src = m.files?.squarePreview?.url || m.files?.preview?.url || m.files?.thumb?.url;
      const on = tentative.has(String(m.id));
      return `<div data-vid="${m.id}" style="cursor:pointer;outline:${on ? '3px solid var(--accent)' : 'none'};border-radius:8px;overflow:hidden;background:var(--bg-elev-2)">
        <div class="thumb" ${src ? `style="background-image:url('${src}')"` : ''}>
          <div class="thumb-overlay">${escapeHtml(m.type || 'media')}</div>
        </div>
        <div style="padding:6px 8px;font-size:11px;color:var(--fg-dim)">#${m.id} ${on ? '· selected' : ''}</div>
      </div>`;
    }).join('');
    grid.querySelectorAll('[data-vid]').forEach(c => {
      c.onclick = () => {
        const id = c.dataset.vid;
        if (tentative.has(id)) tentative.delete(id); else tentative.add(id);
        reload();
      };
    });
  };

  $('#vp-type').onchange = reload;
  $('#vp-done').onclick = () => {
    state.attachedMediaIds = [...tentative].map(Number);
    renderAttachments();
    backdrop.remove();
  };
  $('#vp-cancel').onclick = () => backdrop.remove();
  backdrop.onclick = (e) => { if (e.target === backdrop) backdrop.remove(); };

  await Promise.all([loadFolders(), reload()]);
}

$('#send-attach-btn').onclick = openVaultPicker;

// Upload-from-disk in chat compose. Uploads bytes to S3, dedupe-checks, and
// if OF returns ready=true, auto-attaches the vault id to the outgoing
// message. If ready=false (first-time-claim — fresh file OF has never seen),
// shows the helpful hint instead of attaching, since send would fail.
{
  const inp = $('#send-upload-input');
  if (inp && !inp.dataset.bound) {
    inp.addEventListener('change', async (e) => {
      const f = e.target.files?.[0];
      e.target.value = '';   // allow re-selecting the same file later
      if (!f || !state.selectedChatId) return;
      const host = $('#send-attachments');
      host.style.display = '';
      host.style.color = 'var(--accent-2)';
      host.innerHTML = `uploading ${escapeHtml(f.name)} (${(f.size/1024).toFixed(0)}KB)…`;
      try {
        const fd = new FormData();
        fd.append('file', f);
        const r = await fetch('/api/of/v2/upload', { method: 'POST', body: fd });
        const text = await r.text();
        if (!r.ok) {
          host.style.color = 'var(--err)';
          host.innerHTML = `upload failed: ${r.status} ${escapeHtml(text.slice(0,180))}`;
          return;
        }
        const d = JSON.parse(text);
        if (d.ready && Array.isArray(d.send_with)) {
          // Push each entry (numeric id on dedupe, object on fresh claim)
          for (const entry of d.send_with) state.attachedMediaIds.push(entry);
          renderAttachments();
          host.style.color = 'var(--ok)';
          const label = d.deduped
            ? `dedupe match → vault_id ${d.vault_id}`
            : `fresh claim → processId ${(d.send_with[0] || {}).processId}`;
          host.innerHTML = `✓ attached: ${label} · ${(d.size/1024).toFixed(0)}KB. ${escapeHtml(d.note)}`;
        } else {
          host.style.color = 'var(--warn)';
          host.innerHTML = `⚠ ${escapeHtml(d.note || 'upload incomplete')}`;
        }
      } catch (err) {
        host.style.color = 'var(--err)';
        host.innerHTML = `upload error: ${escapeHtml(err.message)}`;
      }
    });
    inp.dataset.bound = '1';
  }
}

// Send
$('#send-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const text = $('#send-text').value.trim();
  const price = Number($('#send-price').value || 0);
  if (!text && !state.attachedMediaIds.length) { return; }
  if (!state.selectedChatId) return;
  $('#send-btn').disabled = true;
  const original = $('#send-btn').textContent;
  $('#send-btn').textContent = 'sending…';
  try {
    const r = await fetch(`/api/of/v2/chats/${state.selectedChatId}/messages`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({
        text,
        price,
        media_files: state.attachedMediaIds,
        locked_text: price > 0,
      }),
    });
    if (!r.ok) {
      const body = await r.text();
      throw new Error(`HTTP ${r.status}: ${body.slice(0, 200)}`);
    }
    const msg = await r.json();
    appendOwnMessage(msg);
    $('#send-text').value = '';
    $('#send-price').value = '';
    state.attachedMediaIds = [];
    renderAttachments();
  } catch (err) {
    alert('Send failed: ' + err.message);
  } finally {
    $('#send-btn').disabled = false;
    $('#send-btn').textContent = original;
    $('#send-text').focus();
  }
});

// Add-to-list shortcut in chat header
$('#chat-act-addlist').onclick = async () => {
  if (!state.selectedChatId) return;
  let lists;
  try {
    const r = await fetch('/api/of/v2/lists?limit=50');
    if (!r.ok) { alert(`failed to load lists: ${r.status}`); return; }
    const data = await r.json();   // ← was reading twice before; now read ONCE
    lists = Array.isArray(data) ? data : (data.list || []);
  } catch (err) {
    alert('failed to load lists: ' + err.message);
    return;
  }
  if (!lists.length) { alert('no lists found'); return; }
  const backdrop = document.createElement('div');
  backdrop.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.7);z-index:9999;display:flex;align-items:center;justify-content:center;padding:40px';
  backdrop.innerHTML = `
    <div style="background:var(--bg-elev);border:1px solid var(--border);border-radius:12px;width:min(480px,100%);max-height:80vh;overflow:auto;padding:16px">
      <h3 style="margin:0 0 12px 0">📋 Add fan #${state.selectedChatId} to which list?</h3>
      <div style="display:flex;flex-direction:column;gap:6px">
        ${lists.map(l => `
          <button data-list-id="${escapeHtml(String(l.id))}" class="toolbar-btn" style="text-align:left;padding:8px 12px">
            <strong>${escapeHtml(l.name)}</strong> <small style="color:var(--fg-dim)">· ${l.type||'?'} · ${l.usersCount ?? 0} users</small>
          </button>`).join('')}
      </div>
      <div style="display:flex;gap:8px;margin-top:14px">
        <button id="addlist-cancel" class="toolbar-btn">Cancel</button>
      </div>
    </div>`;
  document.body.appendChild(backdrop);
  backdrop.querySelectorAll('button[data-list-id]').forEach(b => {
    b.onclick = async () => {
      const lid = b.dataset.listId;
      const ar = await fetch(`/api/of/v2/lists/${encodeURIComponent(lid)}/users/${state.selectedChatId}`, { method: 'POST' });
      if (!ar.ok) { alert(`add failed: ${ar.status} ${(await ar.text()).slice(0,200)}`); return; }
      backdrop.remove();
      alert(`Added fan #${state.selectedChatId} to "${b.textContent.trim().split('·')[0].trim()}"`);
    };
  });
  $('#addlist-cancel').onclick = () => backdrop.remove();
  backdrop.onclick = (e) => { if (e.target === backdrop) backdrop.remove(); };
};
$('#send-text').addEventListener('keydown', (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
    e.preventDefault();
    $('#send-form').requestSubmit();
  }
});

$('#reload-chats').onclick = loadChats;
$('#hydrate-toggle').onclick = () => {
  state.hydrate = !state.hydrate;
  $('#hydrate-toggle').textContent = `names: ${state.hydrate ? 'on' : 'off'}`;
  loadChats();
};
// (The legacy `#filter-unread` button was replaced by the Unread chip in the
// chat sidebar — the chip path through state.chatList.filter is the single
// source of truth now.)
$('#mark-all-read').onclick = async () => {
  if (!confirm('Mark EVERY chat as read?')) return;
  const r = await fetch('/api/of/v2/chats/mark-as-read', { method: 'POST' });
  if (!r.ok) { alert(`mark-all-read failed: ${r.status} ${await r.text()}`); return; }
  loadChats();
};

// Wire chat-action buttons. Each acts on state.selectedChatId. Toggling
// pairs reload the chat list so badges/state are reflected.
async function chatAction(method, suffix, label, confirmMsg) {
  if (!state.selectedChatId) return;
  if (confirmMsg && !confirm(confirmMsg)) return;
  const r = await fetch(`/api/of/v2/chats/${state.selectedChatId}${suffix}`, { method });
  if (!r.ok) { alert(`${label} failed: ${r.status} ${await r.text()}`); return; }
  await loadChats();   // refresh unread badges
}
// Note + nickname modal — both fields use the same endpoint PUT /subscriptions/{uid}
$('#chat-act-notenick').onclick = async () => {
  if (!state.selectedChatId) return;
  // Get current values from /users/list (x view returns notice + displayName)
  let current = {};
  try {
    const r = await fetch(`/api/of/v2/users/list?ids=${state.selectedChatId}&view=x`);
    if (r.ok) {
      const d = await r.json();
      current = d[String(state.selectedChatId)] || {};
    }
  } catch {}
  const backdrop = document.createElement('div');
  backdrop.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.7);z-index:9999;display:flex;align-items:center;justify-content:center;padding:40px';
  backdrop.innerHTML = `
    <div style="background:var(--bg-elev);border:1px solid var(--border);border-radius:12px;width:min(480px,100%);padding:20px">
      <h3 style="margin:0 0 16px 0">📝 Note + nickname · fan #${state.selectedChatId}</h3>
      <label style="display:block;margin-bottom:8px;font-size:11px;color:var(--fg-dim)">CUSTOM NICKNAME (shows next to username in OF)</label>
      <input id="nn-name" type="text" value="${escapeHtml(current.displayName || '')}" placeholder="e.g. 🐳 whale" style="width:100%;background:var(--bg);color:var(--fg);border:1px solid var(--border);border-radius:8px;padding:8px 12px;margin-bottom:14px;font:inherit">
      <label style="display:block;margin-bottom:8px;font-size:11px;color:var(--fg-dim)">PRIVATE NOTE (creator-side only)</label>
      <textarea id="nn-note" rows="4" placeholder="Things to remember about this fan…" style="width:100%;background:var(--bg);color:var(--fg);border:1px solid var(--border);border-radius:8px;padding:8px 12px;font:inherit;resize:vertical">${escapeHtml(current.notice || '')}</textarea>
      <div style="display:flex;gap:8px;margin-top:14px">
        <button id="nn-cancel" class="toolbar-btn">Cancel</button>
        <span style="flex:1"></span>
        <button id="nn-clear" class="toolbar-btn">Clear both</button>
        <button id="nn-save" class="toolbar-btn" style="background:var(--accent);color:#000;font-weight:700;border-color:var(--accent)">Save</button>
      </div>
    </div>`;
  document.body.appendChild(backdrop);
  $('#nn-cancel').onclick = () => backdrop.remove();
  backdrop.onclick = (e) => { if (e.target === backdrop) backdrop.remove(); };

  const submit = async (body) => {
    const r = await fetch(`/api/of/v2/subscriptions/${state.selectedChatId}`, {
      method: 'PUT', headers: {'content-type': 'application/json'},
      body: JSON.stringify(body),
    });
    if (!r.ok) { alert(`save failed: ${r.status} ${(await r.text()).slice(0,200)}`); return; }
    backdrop.remove();
  };
  $('#nn-save').onclick = () => submit({
    notice: $('#nn-note').value,
    displayName: $('#nn-name').value,
  });
  $('#nn-clear').onclick = () => {
    if (!confirm('Clear note AND nickname for this fan?')) return;
    submit({ notice: '', displayName: '' });
  };
};

$('#chat-act-markread').onclick = () => chatAction('POST',   '/mark-as-read', 'mark-read');
$('#chat-act-markunrd').onclick = () => chatAction('DELETE', '/mark-as-read', 'mark-unread');
$('#chat-act-mute').onclick     = () => chatAction('POST',   '/mute',         'mute');
$('#chat-act-hide').onclick     = () => chatAction('POST',   '/hide',         'hide',
                                       'Hide this chat from your inbox? (Toggle off in OF UI to restore — or DELETE /chats/{id}/hide.)');

// ─── x-of-rev drift banner ─────────────────────────────────────
// Polls /admin/rev/drift and shows a banner when any account's stored
// x-of-rev no longer matches the live OF build hash. The "dismiss" state
// is keyed by the live rev — so dismissing on rev A doesn't suppress the
// warning when rev B ships and a new drift appears.
async function refreshRevDrift() {
  const banner = document.getElementById('drift-banner');
  const msgEl  = document.getElementById('drift-banner-msg');
  if (!banner || !msgEl) return;
  let drift;
  try {
    const r = await fetch('/admin/rev/drift');
    if (!r.ok) return;
    drift = await r.json();
  } catch (_) { return; }
  if (!drift || !drift.any_drift) { banner.style.display = 'none'; return; }
  const dismissedFor = localStorage.getItem('drift_dismissed_for');
  if (dismissedFor && drift.live_rev && dismissedFor === drift.live_rev) {
    banner.style.display = 'none';
    return;
  }
  const stale = (drift.accounts || []).filter(a => a.drift);
  const labels = stale.map(a => a.nickname || a.account_id).join(', ');
  msgEl.textContent =
    ` OnlyFans shipped a new frontend (rev ${drift.live_rev}). Stale: ` +
    `${labels}. Sign in again with the Chatterly Login extension and ` +
    `paste the fresh curl into Setup.`;
  banner.style.display = 'block';

  const cta = document.getElementById('drift-banner-cta');
  if (cta && !cta.__bound) {
    cta.__bound = true;
    cta.addEventListener('click', (e) => {
      e.preventDefault();
      const nav = document.querySelector('#nav-strip a[data-view="setup"]');
      if (nav) nav.click();
    });
  }
  const dismiss = document.getElementById('drift-banner-dismiss');
  if (dismiss && !dismiss.__bound) {
    dismiss.__bound = true;
    dismiss.addEventListener('click', (e) => {
      e.preventDefault();
      if (drift.live_rev) localStorage.setItem('drift_dismissed_for', drift.live_rev);
      banner.style.display = 'none';
    });
  }
}

// ─── Boot ──────────────────────────────────────────────────────
(async () => {
  // Resolve which account this tab is viewing BEFORE the first /health probe
  // (otherwise refreshHealth() races against the X-Account-Id wiring and
  // reports the active account, not the one persisted in localStorage).
  await refreshAccounts({ initial: true });
  await refreshHealth();
  // Drift check is independent of health (rev can drift even while signed
  // calls still work for a few hours), so run it in parallel.
  refreshRevDrift();
  // Re-probe every 5 minutes — covers OF rev bumps during a long session.
  setInterval(refreshRevDrift, 5 * 60 * 1000);
  if (state.myUserId) {
    await Promise.all([loadChats(), refreshStats()]);
  }
})();
