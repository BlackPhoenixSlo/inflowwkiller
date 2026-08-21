/* ==== LIVE WIRING — employees-manage ============================================
 * Roster truth: GET /admin/employees?include_disabled=true (answers 200 unauthed;
 * this is exactly what the real app's components/settings/EmployeesTab.tsx renders).
 * Enrichment:
 *   • GET /admin/employees/{id}/access            → explicit creator grants
 *   • GET /admin/stats/per-employee?by_account=1  → creators actually worked + KPIs
 *   • GET /admin/chatters                          → owner-only link layer, probed
 *     ONLY when /auth/me proves a session exists (it 401s otherwise).
 * Mutations (all behind an explicit click + confirm):
 *   POST /admin/employees · PATCH /admin/employees/{id} · DELETE /admin/employees/{id}
 *   PUT  /admin/employees/{id}/access
 * Drill-down: GET /admin/employees/{id}/actions (keyset paged via next_from_at).
 */
Fastt.ready(async function () {
  var esc = Fastt.esc, fmtInt = Fastt.fmtInt, fmtCents = Fastt.fmtCents;
  var table = document.getElementById('em-table');
  var foot = document.getElementById('em-foot');
  var PALETTE = ['#8b5cf6', '#4166f6', '#67d1ae', '#ec4b9b', '#e5a35b', '#e5735b', '#06B6D4', '#A855F7'];

  var S = {
    emps: [],            // /admin/employees rows
    access: {},          // id -> {account_ids, wildcard}
    accessLoaded: false,
    perf: {},            // id -> {messages_sent, ppv_conversions, revenue_cents, per_account[]}
    creators: {},        // account_id -> nickname
    chatters: null,      // /admin/chatters rows when signed in
    signedIn: false,     // /auth/me resolved to a real owner session
    sel: {},             // id -> true
    view: 'all',
  };

  Fastt.liveBadge(document.getElementById('em-title'));
  Fastt.staticBadge(document.getElementById('em-groupnote').querySelector('.t'), 'NO BACKEND');

  // ── tiny modal helper ─────────────────────────────────────────
  function modal(title, bodyHtml, footHtml) {
    var back = document.createElement('div');
    back.className = 'emod-back';
    back.innerHTML = '<div class="emod"><div class="emod-h">' + title +
      '<span class="x" data-close="1">&times;</span></div>' +
      '<div class="emod-b">' + bodyHtml + '</div>' +
      '<div class="emod-f">' + (footHtml || '<button class="btn-ghost" data-close="1">Close</button>') + '</div></div>';
    document.body.appendChild(back);
    back.addEventListener('click', function (e) {
      if (e.target === back || e.target.closest('[data-close]')) back.remove();
    });
    return back;
  }

  // ── derived helpers ───────────────────────────────────────────
  function creatorName(id) {
    return S.creators[String(id)] || String(id);
  }
  function roleOf(e) {
    return e.chatter_id ? 'Chatter (linked login)' : 'Employee (roster)';
  }
  /** Creators this employee may touch. Empty grant list is NOT "all creators" —
   *  the relay stores an explicit NULL row for the wildcard, so [] means the
   *  employee has no explicit grant at all. Say that, don't guess. */
  function grantText(e) {
    var a = S.access[e.id];
    if (!S.accessLoaded) return '<span class="dim">checking grants…</span>';
    if (!a) return '<span class="dim">grant unreadable</span>';
    if (a.wildcard) return '<span style="color:#67d1ae">All creators (wildcard)</span>';
    var ids = (a.account_ids || []).filter(function (x) { return x; });
    if (!ids.length) return '<span class="dim">no explicit grant</span>';
    return ids.map(function (id) { return esc(creatorName(id)); }).join(', ');
  }
  function workedText(e) {
    var p = S.perf[e.id];
    var pa = (p && p.per_account) || [];
    if (!pa.length) return '';
    var names = pa.slice(0, 3).map(function (r) { return esc(r.account_nickname || r.account_id); });
    var extra = pa.length > 3 ? ' +' + (pa.length - 3) : '';
    return '<div class="sub">worked: ' + names.join(', ') + extra + '</div>';
  }
  function perfText(e) {
    var p = S.perf[e.id];
    if (!p) return '<span class="dim">no attributed activity</span>';
    return fmtInt(p.messages_sent) + ' msgs · ' + fmtInt(p.ppv_conversions) + ' PPV' +
      '<div class="sub" style="color:#67d1ae">' + fmtCents(p.revenue_cents) + ' attributed</div>';
  }
  function workedIds(e) {
    var p = S.perf[e.id];
    return ((p && p.per_account) || []).map(function (r) { return String(r.account_id); });
  }

  // ── filtering ─────────────────────────────────────────────────
  function passView(e) {
    if (S.view === 'active') return e.is_active;
    if (S.view === 'disabled') return !e.is_active;
    if (S.view === 'chatter') return !!e.chatter_id;
    if (S.view === 'roster') return !e.chatter_id;
    if (S.view === 'producing') return !!S.perf[e.id] && S.perf[e.id].revenue_cents > 0;
    return true;
  }
  function filtered() {
    var q = (document.getElementById('em-filter').value || '').trim().toLowerCase();
    var creator = document.getElementById('em-creator').value;
    var status = document.getElementById('em-status').value;
    var role = document.getElementById('em-role').value;
    return S.emps.filter(function (e) {
      if (!passView(e)) return false;
      if (q && String(e.display_name || '').toLowerCase().indexOf(q) === -1
            && String(e.id).indexOf(q.replace('#', '')) === -1) return false;
      if (status === 'active' && !e.is_active) return false;
      if (status === 'disabled' && e.is_active) return false;
      if (role === 'chatter' && !e.chatter_id) return false;
      if (role === 'roster' && e.chatter_id) return false;
      if (creator) {
        var a = S.access[e.id] || {};
        var granted = a.wildcard || (a.account_ids || []).map(String).indexOf(creator) !== -1;
        if (!granted && workedIds(e).indexOf(creator) === -1) return false;
      }
      return true;
    });
  }

  // ── render ────────────────────────────────────────────────────
  var VIEWS = [
    ['all', 'All employees'],
    ['active', 'Activated'],
    ['disabled', 'Deactivated'],
    ['chatter', 'Chatter-linked'],
    ['roster', 'Roster only'],
    ['producing', 'With attributed revenue'],
  ];
  function countFor(key) {
    var was = S.view; S.view = key;
    var n = S.emps.filter(passView).length;
    S.view = was; return n;
  }
  function renderViews() {
    document.getElementById('em-railcount').textContent = S.emps.length + ' total';
    document.getElementById('em-views').innerHTML = VIEWS.map(function (v) {
      return '<div class="vrow' + (S.view === v[0] ? ' on' : '') + '" data-view="' + v[0] + '">' +
        esc(v[1]) + '<span class="n">' + countFor(v[0]) + '</span></div>';
    }).join('');
  }

  function rowHtml(e) {
    var color = e.color || PALETTE[e.id % PALETTE.length];
    return '<div class="et2-row' + (e.is_active ? '' : ' disabled') + '" data-id="' + esc(e.id) + '">' +
      '<div><span class="cbx' + (S.sel[e.id] ? ' on' : '') + '" data-act="sel"></span></div>' +
      '<div><div class="empname"><span class="emp-dot" style="background:' + esc(color) + '"></span>' +
        '<span class="nm">' + esc(e.display_name || ('employee #' + e.id)) + '</span></div>' +
        '<div class="sub">#' + esc(e.id) + ' · added ' + esc(Fastt.fmtAgo(e.created_at)) + '</div></div>' +
      '<div>' + grantText(e) + workedText(e) + '</div>' +
      '<div>' + esc(roleOf(e)) +
        (e.chatter_id ? '<div class="sub">login ' + esc(String(e.chatter_id).slice(0, 8)) + '…</div>' : '') + '</div>' +
      '<div>' + perfText(e) + '</div>' +
      '<div class="' + (e.is_active ? 'ok' : 'off') + '">' + (e.is_active ? 'Activated' : 'Deactivated') + '</div>' +
      '<div class="acts">' +
        '<span data-act="edit">Edit</span>' +
        '<span data-act="access">Access</span>' +
        '<span data-act="activity">Activity</span>' +
        '<span data-act="toggle">' + (e.is_active ? 'Disable' : 'Enable') + '</span>' +
      '</div></div>';
  }

  function render() {
    var rows = filtered();
    table.querySelectorAll('.et2-row').forEach(function (r) { r.remove(); });
    if (!rows.length) {
      var d = document.createElement('div');
      d.className = 'et2-row';
      d.style.cssText = 'display:flex;align-items:center;justify-content:center;color:#8a8a8a';
      d.textContent = S.emps.length ? 'No employees match these filters'
                                    : 'No employees on this relay yet — use “+ Add employee”';
      table.appendChild(d);
    } else {
      table.insertAdjacentHTML('beforeend', rows.map(rowHtml).join(''));
    }
    var label = (VIEWS.find(function (v) { return v[0] === S.view; }) || VIEWS[0])[1];
    document.getElementById('em-title').firstChild.nodeValue = label + ' · ' + rows.length;
    var nSel = Object.keys(S.sel).length;
    document.getElementById('em-selinfo').textContent =
      nSel ? (nSel + ' row' + (nSel === 1 ? '' : 's') + ' selected') : 'No rows selected';
    var all = document.getElementById('em-all');
    all.classList.toggle('on', rows.length > 0 && rows.every(function (e) { return S.sel[e.id]; }));
    renderViews();
    var linked = S.emps.filter(function (e) { return e.chatter_id; }).length;
    var withPerf = S.emps.filter(function (e) { return S.perf[e.id]; }).length;
    foot.innerHTML =
      '<span>' + S.emps.length + ' employees · ' + linked + ' chatter-linked · ' +
        withPerf + ' with attributed activity</span>' +
      '<span class="dim">roster + grants + KPIs are live relay reads; refresh with any filter change</span>' +
      (S.chatters ? '<span style="color:#67d1ae">signed in — chatter logins merged</span>'
                  : '<span class="dim">not signed in — chatter login details hidden (GET /admin/chatters is owner-only)</span>');
  }

  // ── loaders ───────────────────────────────────────────────────
  async function loadRoster() {
    var out = await Fastt.get('/admin/employees', { include_disabled: 'true' });
    S.emps = (out.employees || []).slice().sort(function (a, b) {
      return String(a.display_name || '').localeCompare(String(b.display_name || ''));
    });
  }

  function seedCreators() {
    (Fastt.accounts() || []).forEach(function (a) {
      S.creators[String(a.id)] = a.nickname || String(a.id);
    });
  }

  async function loadPerf() {
    var out = await Fastt.get('/admin/stats/per-employee', { by_account: 'true' });
    (out.employees || []).forEach(function (r) {
      if (r.employee_id == null) return;   // synthetic "Unattributed" bucket
      S.perf[r.employee_id] = r;
      // The account picker's nicknames win; per_account fills in creators the
      // picker never saw (other owners' models that this employee worked).
      (r.per_account || []).forEach(function (pa) {
        var key = String(pa.account_id);
        if (!S.creators[key]) S.creators[key] = pa.account_nickname || key;
      });
    });
  }

  function fillCreatorFilter() {
    var sel = document.getElementById('em-creator');
    var cur = sel.value;
    var ids = Object.keys(S.creators).sort(function (a, b) {
      return S.creators[a].localeCompare(S.creators[b]);
    });
    sel.innerHTML = '<option value="">Any creator</option>' + ids.map(function (id) {
      return '<option value="' + esc(id) + '">' + esc(S.creators[id]) + '</option>';
    }).join('');
    sel.value = cur;
  }

  /** Explicit grants, 6 at a time so 33 employees don't open 33 sockets. */
  async function loadAccess() {
    var ids = S.emps.map(function (e) { return e.id; });
    for (var i = 0; i < ids.length; i += 6) {
      await Promise.all(ids.slice(i, i + 6).map(async function (id) {
        try { S.access[id] = await Fastt.get('/admin/employees/' + id + '/access'); }
        catch (e) { S.access[id] = null; }
      }));
    }
    S.accessLoaded = true;
  }

  /** /admin/chatters 401s for anyone but a signed-in owner — so ask /auth/me
   *  first (200 + null when unauthed) and skip the call entirely otherwise.
   *  No speculative 401 lands in the network log. */
  async function loadChattersIfSignedIn() {
    var me = null;
    try { me = await Fastt.get('/auth/me'); } catch (e) { S.signedIn = false; return; }
    if (!me) { S.signedIn = false; return; }
    S.signedIn = true;
    try { S.chatters = await Fastt.get('/admin/chatters'); } catch (e) { S.chatters = null; }
    if (!S.chatters) return;
    var byId = {};
    S.chatters.forEach(function (c) { byId[String(c.id)] = c; });
    S.emps.forEach(function (e) {
      var c = e.chatter_id && byId[String(e.chatter_id)];
      if (c) { e._login = c.username; e._lastSeen = c.last_seen_at; }
    });
  }

  // ── chatter-login layer (owner-only: invite / link / unlink) ──
  // Mirrors the real app's settings/ChattersTab. On this relay /auth/me is
  // null and GET /admin/chatters 401s, so this degrades to an honest, badged
  // "owner session required" state instead of faking a linked-chatter list.
  var chattersBadged = false;
  function renderChatters() {
    var body = document.getElementById('cl-body');
    var cnt = document.getElementById('cl-count');
    if (!body) return;
    if (!S.signedIn) {
      cnt.textContent = 'owner-only';
      if (!chattersBadged) {
        Fastt.staticBadge(document.querySelector('#cl-head .clt'), 'OWNER SESSION REQUIRED');
        chattersBadged = true;
      }
      body.innerHTML =
        '<div class="clnote"><span class="ic"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M12 9v4M12 17h.01" stroke-linecap="round"/><path d="M10.3 4.3 2.6 18a2 2 0 0 0 1.7 3h15.4a2 2 0 0 0 1.7-3L13.7 4.3a2 2 0 0 0-3.4 0z"/></svg></span>' +
        '<div class="d"><b style="color:#cfcfcf">Sign in as the owner to manage chatter logins.</b><br>' +
        'The invite / link / unlink actions are owner-scoped — <code>GET /admin/chatters</code> returns 401 and ' +
        '<code>/auth/me</code> resolved to null on this relay, so there is no real chatter list to act on and nothing is invented. ' +
        'Employees that already mirror a chatter login are still shown in the roster above (Role → “Chatter (linked login)”).</div></div>' +
        '<div style="display:flex;gap:10px;flex-wrap:wrap">' +
          '<button class="btn-blue" disabled>Mint invite URL</button>' +
          '<button class="btn-outline" disabled>Link existing chatter…</button></div>';
      return;
    }
    var rows = S.chatters || [];
    cnt.textContent = rows.length + ' linked';
    var list = rows.length ? rows.map(function (c) {
      return '<div class="clrow"><span class="emp-dot" style="background:' + esc(c.color || '#666') + '"></span>' +
        '<span class="un">@' + esc(c.username) + '</span>' +
        '<span>' + esc(c.display_name || '—') + '</span>' +
        '<span class="dim" style="font-size:12.5px">' +
          (c.employee_id_for_this_owner != null ? ('mirror #' + esc(c.employee_id_for_this_owner))
                                                : 'mirror auto-created on first action') + '</span>' +
        '<span class="clact danger" data-unlink="' + esc(c.id) + '">Unlink</span></div>';
    }).join('') : '<div class="dim" style="font-size:13px">No chatters linked yet — invite one below.</div>';
    body.innerHTML =
      '<div class="clcard"><h4>Linked chatters</h4>' + list + '</div>' +
      '<div class="clcard"><h4>Invite a new chatter</h4>' +
        '<p>Mint a one-shot signup URL. The chatter creates their account through it and is auto-linked to you on first login (token expires in 24h).</p>' +
        '<div class="clform"><button class="btn-blue" id="cl-mint">Mint invite URL</button>' +
        '<span id="cl-invite" style="flex:1"></span></div></div>' +
      '<div class="clcard"><h4>Link an existing chatter</h4>' +
        '<p>Use this when the chatter already has an account through another owner. They see your models in their picker on next sign-in.</p>' +
        '<form class="clform" id="cl-linkform"><input class="clminp" id="cl-linkun" placeholder="Username" autocomplete="off">' +
        '<button class="btn-blue" type="submit">Link</button></form>' +
        '<div id="cl-linkmsg" style="font-size:12.5px;margin-top:9px"></div></div>';

    var mint = document.getElementById('cl-mint');
    if (mint) mint.addEventListener('click', async function () {
      if (!confirm('Mint a one-shot chatter invite URL?')) return;
      try {
        var r = await Fastt.post('/admin/chatters/invite', {});
        var url = location.origin + (r.accept_path || ('/chatter/invite/' + r.token));
        document.getElementById('cl-invite').innerHTML =
          '<input class="clminp" readonly value="' + esc(url) + '" onfocus="this.select()">';
        Fastt.toast('Invite URL minted — expires ' + esc(Fastt.fmtAgo(r.expires_at) || '24h'), 'ok');
      } catch (e) { Fastt.oops(e); }
    });
    var lf = document.getElementById('cl-linkform');
    if (lf) lf.addEventListener('submit', async function (ev) {
      ev.preventDefault();
      var un = document.getElementById('cl-linkun').value.trim().toLowerCase();
      if (!un) return;
      if (!confirm('Link chatter @' + un + ' to this owner?')) return;
      var msg = document.getElementById('cl-linkmsg');
      try {
        var r = await Fastt.post('/admin/chatters/link', { username: un });
        msg.innerHTML = '<span style="color:#67d1ae">' +
          (r.already_linked ? '@' + esc(un) + ' was already linked.' : 'Linked @' + esc(un) + '.') + '</span>';
        await loadChattersIfSignedIn(); renderChatters(); render();
      } catch (e) {
        var t = (e.status === 404) ? ('No chatter named "' + un + '" — send them an invite link instead.')
                                   : ((e.body && e.body.detail) || e.message);
        msg.innerHTML = '<span style="color:#e05b5b">' + esc(t) + '</span>';
      }
    });
    body.querySelectorAll('[data-unlink]').forEach(function (el) {
      el.addEventListener('click', async function () {
        if (!confirm('Unlink this chatter? Their login stays, but they lose access to your models.')) return;
        try {
          await Fastt.del('/admin/chatters/' + el.dataset.unlink + '/link');
          Fastt.saved('Unlinked ✓');
          await loadChattersIfSignedIn(); renderChatters(); render();
        } catch (e) { Fastt.oops(e); }
      });
    });
  }
  document.getElementById('cl-head').addEventListener('click', function () {
    document.getElementById('cl-sec').classList.toggle('open');
  });

  // ── mutations ─────────────────────────────────────────────────
  async function refresh() {
    await loadRoster();
    S.accessLoaded = false;
    render();
    await loadAccess();
    render();
  }

  document.getElementById('em-new').addEventListener('click', async function () {
    var name = prompt('New employee display name (shown in the "who is chatting" picker):');
    if (name === null) return;
    name = name.trim();
    if (!name) { Fastt.toast('Name is required', 'err'); return; }
    var color = prompt('Row colour (hex, blank = relay picks one):', '') || '';
    if (!confirm('Create employee "' + name + '" on this relay?')) return;
    try {
      var body = { display_name: name };
      if (color.trim()) body.color = color.trim();
      await Fastt.post('/admin/employees', body);
      Fastt.saved('Employee created ✓');
      await refresh();
    } catch (e) { Fastt.oops(e); }
  });

  var bmenu = document.getElementById('em-batchmenu');
  document.getElementById('em-batch').addEventListener('click', function (e) {
    e.stopPropagation();
    bmenu.classList.toggle('open');
  });
  document.addEventListener('click', function () { bmenu.classList.remove('open'); });
  bmenu.addEventListener('click', async function (e) {
    var item = e.target.closest('[data-b]');
    if (!item) return;
    e.stopPropagation();
    bmenu.classList.remove('open');
    var ids = Object.keys(S.sel);
    var act = item.dataset.b;
    if (act === 'clear') { S.sel = {}; render(); return; }
    if (!ids.length) { Fastt.toast('Select at least one row first', 'err'); return; }
    if (act === 'access') { openAccess(ids.map(Number)); return; }
    var enable = act === 'enable';
    if (!confirm((enable ? 'Enable' : 'Disable') + ' ' + ids.length + ' employee(s)?' +
        (enable ? '' : '\n\nDisable is a soft delete — audit history is kept.'))) return;
    try {
      for (var i = 0; i < ids.length; i++) {
        if (enable) await Fastt.patch('/admin/employees/' + ids[i], { is_active: true });
        else await Fastt.del('/admin/employees/' + ids[i]);
      }
      Fastt.saved((enable ? 'Enabled ' : 'Disabled ') + ids.length + ' ✓');
      S.sel = {};
      await refresh();
    } catch (e2) { Fastt.oops(e2); }
  });

  function openEdit(e) {
    var color = e.color || PALETTE[e.id % PALETTE.length];
    var back = modal('Edit ' + esc(e.display_name || ('#' + e.id)),
      '<label class="flabel" style="font-size:13.5px;color:#cfcfcf">Display name</label>' +
      '<input class="modinput" id="em-ed-name" value="' + esc(e.display_name || '') + '">' +
      '<div style="margin-top:18px;font-size:13.5px;color:#cfcfcf">Row colour</div>' +
      '<div class="swatches" id="em-ed-sw">' + PALETTE.map(function (c) {
        return '<span class="swatch' + (c.toLowerCase() === String(color).toLowerCase() ? ' on' : '') +
          '" data-c="' + c + '" style="background:' + c + '"></span>';
      }).join('') + '</div>' +
      '<div style="margin-top:18px;color:#7a7a7a;font-size:12.5px">' +
        'PATCH /admin/employees/' + e.id + ' — rename / recolour only. ' +
        (e.chatter_id ? 'This row mirrors a chatter login; the login itself is managed under Chatters.' : '') +
      '</div>',
      '<button class="btn-ghost" data-close="1">Cancel</button>' +
      '<button class="btn-blue" id="em-ed-save">Save</button>');
    var picked = color;
    back.querySelector('#em-ed-sw').addEventListener('click', function (ev) {
      var sw = ev.target.closest('.swatch');
      if (!sw) return;
      picked = sw.dataset.c;
      back.querySelectorAll('.swatch').forEach(function (x) { x.classList.toggle('on', x === sw); });
    });
    back.querySelector('#em-ed-save').addEventListener('click', async function () {
      var name = back.querySelector('#em-ed-name').value.trim();
      if (!name) { Fastt.toast('Name is required', 'err'); return; }
      try {
        await Fastt.patch('/admin/employees/' + e.id, { display_name: name, color: picked });
        back.remove();
        Fastt.saved('Saved ✓');
        await refresh();
      } catch (err) { Fastt.oops(err); }
    });
  }

  function openAccess(ids) {
    var single = ids.length === 1 ? S.emps.find(function (e) { return e.id === ids[0]; }) : null;
    var cur = single ? (S.access[single.id] || { account_ids: [], wildcard: false }) : { account_ids: [], wildcard: false };
    var curIds = (cur.account_ids || []).filter(function (x) { return x; }).map(String);
    var creatorIds = Object.keys(S.creators).sort(function (a, b) {
      return S.creators[a].localeCompare(S.creators[b]);
    });
    var back = modal(single ? ('Creator access — ' + esc(single.display_name))
                            : ('Creator access — ' + ids.length + ' employees'),
      '<div style="color:#8a8a8a;font-size:13px;margin-bottom:12px">' +
        'Replaces the whole grant set (PUT /admin/employees/{id}/access). ' +
        'Wildcard = every creator, and it is exclusive of explicit ids.' +
        (single ? '' : ' The same set is written to all selected employees.') +
      '</div>' +
      '<div class="acheck" data-w="1"><span class="cbx' + (cur.wildcard ? ' on' : '') + '"></span>' +
        '<b>All creators (wildcard)</b></div>' +
      creatorIds.map(function (id) {
        return '<div class="acheck" data-a="' + esc(id) + '"><span class="cbx' +
          (curIds.indexOf(id) !== -1 ? ' on' : '') + '"></span>' + esc(S.creators[id]) +
          ' <span class="dim" style="font-size:12px">' + esc(id) + '</span></div>';
      }).join(''),
      '<span class="lft">' + (single ? (cur.wildcard ? 'currently: wildcard'
        : (curIds.length ? 'currently: ' + curIds.length + ' creator(s)' : 'currently: no explicit grant'))
        : 'bulk write') + '</span>' +
      '<button class="btn-ghost" data-close="1">Cancel</button>' +
      '<button class="btn-blue" id="em-ac-save">Save access</button>');
    back.querySelector('.emod-b').addEventListener('click', function (ev) {
      var row = ev.target.closest('.acheck');
      if (!row) return;
      var box = row.querySelector('.cbx');
      box.classList.toggle('on');
      if (row.dataset.w && box.classList.contains('on')) {
        back.querySelectorAll('.acheck[data-a] .cbx').forEach(function (b) { b.classList.remove('on'); });
      } else if (row.dataset.a && box.classList.contains('on')) {
        var w = back.querySelector('.acheck[data-w] .cbx');
        if (w) w.classList.remove('on');
      }
    });
    back.querySelector('#em-ac-save').addEventListener('click', async function () {
      var wildcard = back.querySelector('.acheck[data-w] .cbx').classList.contains('on');
      var picked = Array.prototype.map.call(
        back.querySelectorAll('.acheck[data-a] .cbx.on'),
        function (b) { return b.closest('.acheck').dataset.a; });
      if (!confirm('Write this access set to ' + ids.length + ' employee(s)?\n\n' +
          (wildcard ? 'All creators (wildcard)' : (picked.length ? picked.map(creatorName).join(', ') : 'no creators — revokes every grant')))) return;
      try {
        for (var i = 0; i < ids.length; i++) {
          await Fastt.put('/admin/employees/' + ids[i] + '/access',
            wildcard ? { account_ids: [], wildcard: true } : { account_ids: picked, wildcard: false });
        }
        back.remove();
        Fastt.saved('Access saved ✓');
        S.accessLoaded = false;
        await loadAccess();
        render();
      } catch (err) { Fastt.oops(err); }
    });
  }

  async function openActivity(e) {
    var back = modal('Activity — ' + esc(e.display_name || ('#' + e.id)),
      '<div id="em-ax-list" style="color:#8a8a8a">Loading audit rows…</div>',
      '<span class="lft">GET /admin/employees/' + e.id + '/actions</span>' +
      '<button class="btn-ghost" id="em-ax-more" style="display:none">Load older</button>' +
      '<button class="btn-ghost" data-close="1">Close</button>');
    var list = back.querySelector('#em-ax-list');
    var more = back.querySelector('#em-ax-more');
    var cursor = null, rendered = 0;
    function payloadLine(a) {
      if (!a.payload) return '';
      var p = a.payload;
      if (p.text) return '<div class="pl">“' + esc(String(p.text).slice(0, 140)) + '”' +
        (p.price ? ' · $' + esc(p.price) : '') + '</div>';
      var keys = Object.keys(p).slice(0, 4);
      if (!keys.length) return '';
      return '<div class="pl">' + esc(keys.map(function (k) {
        return k + '=' + JSON.stringify(p[k]).slice(0, 40);
      }).join(' · ')) + '</div>';
    }
    async function page() {
      var out = await Fastt.get('/admin/employees/' + e.id + '/actions',
        cursor ? { limit: 25, from_at: cursor } : { limit: 25 });
      var rows = out.actions || [];
      if (!rendered && !rows.length) {
        list.innerHTML = '<div style="padding:18px 0;color:#8a8a8a">No audit rows for this employee — '
          + 'nothing has been done through the relay under their id.</div>';
        return;
      }
      if (!rendered) list.innerHTML = '';
      list.insertAdjacentHTML('beforeend', rows.map(function (a) {
        return '<div class="arow"><div class="tm">' + esc(Fastt.fmtAgo(a.at)) +
          '<div style="font-size:11.5px;color:#6a6a6a">' + esc(Fastt.fmtDate(a.at)) + '</div></div>' +
          '<div class="ac">' + esc(a.action) +
          (a.account_id ? ' <span class="dim">· ' + esc(creatorName(a.account_id)) + '</span>' : '') +
          payloadLine(a) + '</div></div>';
      }).join(''));
      rendered += rows.length;
      cursor = out.next_from_at;
      more.style.display = cursor ? '' : 'none';
    }
    more.addEventListener('click', function () { page().catch(Fastt.oops); });
    try { await page(); } catch (err) {
      list.innerHTML = '<div style="color:#e05b5b">Couldn’t read this employee’s audit log — ' +
        esc((err.body && err.body.detail) || err.message) + '</div>';
    }
  }

  table.addEventListener('click', async function (ev) {
    if (ev.target.id === 'em-all') {
      var rows = filtered();
      var allOn = rows.length && rows.every(function (e) { return S.sel[e.id]; });
      rows.forEach(function (e) { if (allOn) delete S.sel[e.id]; else S.sel[e.id] = true; });
      render();
      return;
    }
    var act = ev.target.closest('[data-act]');
    if (!act) return;
    var rowEl = act.closest('.et2-row');
    if (!rowEl) return;
    var e = S.emps.find(function (x) { return String(x.id) === String(rowEl.dataset.id); });
    if (!e) return;
    var kind = act.dataset.act;
    if (kind === 'sel') {
      if (S.sel[e.id]) delete S.sel[e.id]; else S.sel[e.id] = true;
      render();
    } else if (kind === 'edit') {
      openEdit(e);
    } else if (kind === 'access') {
      openAccess([e.id]);
    } else if (kind === 'activity') {
      openActivity(e);
    } else if (kind === 'toggle') {
      var on = !e.is_active;
      if (!confirm((on ? 'Enable' : 'Disable') + ' "' + (e.display_name || e.id) + '"?' +
          (on ? '' : '\n\nSoft delete — the row stays so audit history keeps resolving a name.'))) return;
      try {
        if (on) await Fastt.patch('/admin/employees/' + e.id, { is_active: true });
        else await Fastt.del('/admin/employees/' + e.id);
        Fastt.saved(on ? 'Enabled ✓' : 'Disabled');
        await refresh();
      } catch (err) { Fastt.oops(err); }
    }
  });

  document.getElementById('em-views').addEventListener('click', function (ev) {
    var v = ev.target.closest('[data-view]');
    if (!v) return;
    S.view = v.dataset.view;
    render();
  });

  ['em-creator', 'em-status', 'em-role'].forEach(function (id) {
    document.getElementById(id).addEventListener('change', render);
  });
  document.getElementById('em-filter').addEventListener('input', Fastt.debounce(render, 150));
  document.getElementById('em-search').addEventListener('click', render);
  document.getElementById('em-reset').addEventListener('click', function () {
    document.getElementById('em-filter').value = '';
    document.getElementById('em-creator').value = '';
    document.getElementById('em-status').value = '';
    document.getElementById('em-role').value = '';
    S.view = 'all'; S.sel = {};
    render();
  });

  // ── boot ──────────────────────────────────────────────────────
  seedCreators();
  try {
    await loadRoster();
  } catch (err) {
    table.querySelectorAll('.et2-row').forEach(function (r) { r.remove(); });
    var d = document.createElement('div');
    d.className = 'et2-row';
    d.style.cssText = 'display:flex;align-items:center;justify-content:center;color:#e05b5b';
    d.textContent = 'Couldn’t load the roster — GET /admin/employees failed';
    table.appendChild(d);
    foot.textContent = 'Roster unavailable — relay error';
    throw err;
  }
  render();
  try { await loadPerf(); } catch (err) { console.warn('per-employee stats unavailable', err); }
  fillCreatorFilter();
  render();
  await loadAccess();
  render();
  await loadChattersIfSignedIn();
  render();
  renderChatters();
});
