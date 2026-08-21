/* ==== LIVE WIRING — settings ====================================================
 * Every tab reads the relay. Nothing on this page prints a number it did not fetch.
 *
 *  Your account      GET /api/of/v2/users/me · GET /auth/me · GET /chatter/me
 *                    avatar through the relay image proxy /img?u=<encoded>
 *                    "Rename creator" → PATCH /admin/accounts/{id}
 *  Preferences       GET/PUT /admin/account-config  (utc_offset · timezone · language)
 *                    PUT is a FULL REPLACE — we re-GET, patch one key, send it all back.
 *  Security (org)    GET/PUT /admin/secrets · GET /admin/audit (paged)
 *  Security (pers.)  GET /admin/session/status · GET /admin/rev/drift
 *                    GET /api/of/v2/users/me/settings  (read-only mirror)
 *  Billing           GET /admin/stats/grok-cost (chart drawn from the rows)
 *  Your Balance      GET /api/of/v2/users/me (creditBalance / limits)
 *                    GET /api/of/v2/payments/referrals/balance · grok-cost vs cap
 *  Role settings     GET /admin/users · GET/PUT /admin/employees/{id}/access
 *  Sales settings    GET /admin/stats/attribution-coverage · …/attribution-orphans
 *  Time tracking     GET /admin/audit (observed activity — there is no clock-in API)
 *  Messaging         /admin/translate probe · /admin/banned-words · /admin/webhooks
 *
 * Panels build lazily on first tab activation so opening Settings costs one page's
 * worth of requests, not eleven.
 */
Fastt.ready(async function () {
  var esc = Fastt.esc, fmtCents = Fastt.fmtCents, fmtInt = Fastt.fmtInt;
  var accountId = Fastt.account();
  var acctName = accountId;

  function panel(key) { return document.querySelector('.panel[data-panel="' + key + '"]'); }
  function warn(e, what) { console.warn(what || 'settings', e); }
  function detail(e) {
    return (e && e.body && (e.body.detail || e.body.error)) || (e && e.message) || 'request failed';
  }
  function errBox(msg) {
    return '<div class="card2"><div class="t" style="color:#e05b5b">Couldn’t load</div>' +
      '<div class="d">' + esc(msg) + '</div></div>';
  }
  function proxied(url) { return '/img?u=' + encodeURIComponent(url); }

  // ── creator roster (names for ids) ────────────────────────────
  var creators = {};
  (Fastt.accounts() || []).forEach(function (a) { creators[String(a.id)] = a.nickname || String(a.id); });
  if (accountId && creators[accountId]) acctName = creators[accountId];

  // ── shared modal ──────────────────────────────────────────────
  function modal(title, bodyHtml, footHtml) {
    var back = document.createElement('div');
    back.className = 'smod-back';
    back.innerHTML = '<div class="smod"><div class="smod-h">' + title +
      '<span class="x" data-close="1">&times;</span></div>' +
      '<div class="smod-b">' + bodyHtml + '</div>' +
      '<div class="smod-f">' + (footHtml || '<button class="btn2" data-close="1">Close</button>') + '</div></div>';
    document.body.appendChild(back);
    function close() {
      back.remove();
      document.removeEventListener('keydown', onKey);
    }
    function onKey(e) { if (e.key === 'Escape') close(); }
    document.addEventListener('keydown', onKey);
    back.addEventListener('click', function (e) {
      if (e.target === back || e.target.closest('[data-close]')) close();
    });
    return back;
  }

  // ── account-config: full-replace-safe patch ───────────────────
  /** PUT /admin/account-config replaces persona / welcome_rules / location /
   *  time_activities / time_images wholesale. Always re-GET first and send the
   *  WHOLE config back with one key changed — patching a subset blanks Ava. */
  async function patchConfig(changes) {
    var fresh = await Fastt.get('/admin/account-config');
    var cfg = Object.assign({}, fresh.config, changes);
    await Fastt.put('/admin/account-config', { account_id: accountId, config: cfg });
    return await Fastt.get('/admin/account-config');
  }

  // ══════════════════════════════════════════════════════════════
  // 1. YOUR ACCOUNT
  // ══════════════════════════════════════════════════════════════
  var ofMe = null;                     // cached across account/balance tabs
  async function loadOfMe() {
    if (ofMe !== null) return ofMe;
    try { ofMe = await Fastt.get('/api/of/v2/users/me'); }
    catch (e) { ofMe = { _error: detail(e) }; }
    return ofMe;
  }

  async function buildAccount() {
    var body = document.getElementById('st-acct-body');
    var me = await loadOfMe();
    var authMe = null, chatterMe = null;
    try { authMe = await Fastt.get('/auth/me'); } catch (e) { warn(e, 'auth/me'); }
    try { chatterMe = await Fastt.get('/chatter/me'); } catch (e) { warn(e, 'chatter/me'); }

    var html = '';
    if (me._error) {
      html += errBox('GET /api/of/v2/users/me — ' + me._error +
        '. The captured OnlyFans session for this creator can’t answer; re-capture it from the relay setup page.');
    } else {
      var avatar = (me.avatarThumbs && (me.avatarThumbs.c144 || me.avatarThumbs.c50)) || me.avatar;
      html +=
        '<div class="card2"><div class="t" id="st-of-id">OnlyFans identity</div>' +
        '<div class="d">Live from <code>GET /api/of/v2/users/me</code> with <code>X-Account-Id: ' + esc(accountId) + '</code>. ' +
          'The picture is fetched through the relay’s image proxy (<code>/img?u=</code>) because OnlyFans CDN URLs are signed.</div>' +
        '<div class="idcard" style="margin-top:18px">' +
          '<div class="pic">' + (avatar
            ? '<img src="' + esc(proxied(avatar)) + '" alt="' + esc(me.name || '') + ' avatar">'
            : esc(String(me.name || '?').slice(0, 1).toUpperCase())) + '</div>' +
          '<div class="who">' +
            '<div style="font-size:22px;font-weight:600;color:#fff;display:flex;align-items:center;gap:10px;flex-wrap:wrap">' +
              esc(me.name || '—') +
              (me.isVerified ? '<span class="vchip">VERIFIED</span>' : '') + '</div>' +
            '<div class="kv">' +
              '<div class="k">Username</div><div class="v">@' + esc(me.username || '') + '</div>' +
              '<div class="k">Email on file</div><div class="v">' + esc(me.email || 'not exposed') + '</div>' +
              '<div class="k">OnlyFans id</div><div class="v">' + esc(me.id) + '</div>' +
              '<div class="k">Joined</div><div class="v">' + esc(me.joinDate ? new Date(me.joinDate).toLocaleDateString() : '—') + '</div>' +
              '<div class="k">fastt nickname</div><div class="v">' + esc(acctName) + '</div>' +
            '</div>' +
            '<div class="statgrid">' +
              '<div class="stat"><div class="sv">' + fmtInt(me.subscribersCount) + '</div><div class="sl">Subscribers</div></div>' +
              '<div class="stat"><div class="sv">' + fmtInt(me.postsCount) + '</div><div class="sl">Posts</div></div>' +
              '<div class="stat"><div class="sv">' + fmtInt(me.photosCount) + '</div><div class="sl">Photos</div></div>' +
              '<div class="stat"><div class="sv">' + fmtInt(me.videosCount) + '</div><div class="sl">Videos</div></div>' +
              '<div class="stat"><div class="sv">' + fmtInt(me.favoritedCount) + '</div><div class="sl">Likes received</div></div>' +
            '</div>' +
          '</div></div></div>';
    }

    var who = authMe ? ('owner “' + (authMe.username || authMe.id) + '”')
            : (chatterMe ? ('chatter “' + (chatterMe.username || chatterMe.id) + '”') : null);
    html +=
      '<div class="card2"><div class="t" id="st-op-id">Who is signed in to fastt</div>' +
      '<div class="d">Separate from the creator above: this is the operator identity the relay sees on ' +
        'your requests (<code>GET /auth/me</code> for owners, <code>GET /chatter/me</code> for chatter logins).</div>' +
      '<div class="kv">' +
        '<div class="k">Session</div><div class="v ' + (who ? 'good' : 'warn') + '">' +
          (who ? esc(who) : 'not signed in — reads work unauthed, owner-only routes (chatters, invites) do not') + '</div>' +
        '<div class="k">Creator scope</div><div class="v">' + esc(acctName) + ' <span style="color:#7a7a7a">(' + esc(accountId) + ')</span></div>' +
      '</div></div>';

    html +=
      '<div class="card2"><div class="t" id="st-tg">Telegram username</div>' +
      '<div class="d">The relay stores no Telegram handle — there is no column and no route for it, so this ' +
        'field is left empty rather than pretending to save.</div>' +
      '<input class="finput" style="margin-top:14px;max-width:420px" placeholder="no backend — not saved" disabled></div>';

    body.className = '';
    body.innerHTML = html;
    if (!me._error) Fastt.liveBadge(document.getElementById('st-of-id'));
    Fastt.liveBadge(document.getElementById('st-op-id'));
    Fastt.staticBadge(document.getElementById('st-tg'), 'NO BACKEND');

    var signin = document.getElementById('st-acct-signin');
    signin.textContent = who ? 'Signed in' : 'Sign in to fastt';
    signin.addEventListener('click', async function () {
      if (who) { Fastt.toast('Already signed in as ' + who, 'ok'); return; }
      if (await Fastt.signInModal()) location.reload();
    });
    document.getElementById('st-acct-rename').addEventListener('click', async function () {
      var next = prompt('fastt nickname for this creator (does not touch the OnlyFans profile):', acctName);
      if (next === null || !next.trim() || next.trim() === acctName) return;
      if (!confirm('Rename creator ' + accountId + ' to “' + next.trim() + '”?')) return;
      try {
        await Fastt.patch('/admin/accounts/' + encodeURIComponent(accountId), { nickname: next.trim() });
        Fastt.saved('Renamed ✓');
        location.reload();
      } catch (e) { Fastt.oops(e); }
    });
  }

  // ══════════════════════════════════════════════════════════════
  // 2. YOUR PREFERENCES
  // ══════════════════════════════════════════════════════════════
  var ZONES = ['America/Los_Angeles', 'America/Denver', 'America/Chicago', 'America/New_York',
    'America/Vancouver', 'America/Mexico_City', 'America/Sao_Paulo', 'Europe/London',
    'Europe/Lisbon', 'Europe/Madrid', 'Europe/Paris', 'Europe/Berlin', 'Europe/Ljubljana',
    'Europe/Warsaw', 'Europe/Athens', 'Europe/Moscow', 'Asia/Dubai', 'Asia/Karachi',
    'Asia/Kolkata', 'Asia/Bangkok', 'Asia/Manila', 'Asia/Tokyo', 'Australia/Sydney',
    'Pacific/Auckland', 'Pacific/Honolulu', 'UTC'];

  function clockNow(cfg) {
    try {
      if (cfg.timezone) {
        return new Date().toLocaleString('en-US', { timeZone: cfg.timezone, hour12: true });
      }
    } catch (e) { warn(e, 'tz'); }
    var off = Number(cfg.utc_offset || 0);
    var d = new Date(Date.now() + off * 3600000);
    return d.toUTCString().replace(' GMT', '') + '  (UTC' + (off >= 0 ? '+' : '') + off + ')';
  }

  async function buildPreferences() {
    var body = document.getElementById('st-pref-body');
    var resp;
    try { resp = await Fastt.get('/admin/account-config'); }
    catch (e) { body.className = ''; body.innerHTML = errBox('GET /admin/account-config — ' + detail(e)); return; }
    var cfg = resp.config || {};
    var langs = resp.languages || [];

    var offs = [];
    for (var i = -12; i <= 14; i++) offs.push(i);

    body.className = '';
    body.innerHTML =
      '<div class="card2"><div class="t" id="st-clock">Creator clock</div>' +
      '<div class="d">Every AI chat engine stamps the prompt with this clock, so “good morning” lands at the ' +
        'creator’s morning. An IANA zone wins over the raw offset because it is DST-correct. ' +
        'Saved through <code>PUT /admin/account-config</code> (full-config replace, handled safely).</div>' +
      '<div class="field" style="margin-top:18px"><label class="flabel">Time zone (IANA — preferred)</label>' +
        '<div class="fselect wired"><select id="st-tz">' +
          '<option value="">— none: use the UTC offset below —</option>' +
          ZONES.map(function (z) {
            return '<option value="' + esc(z) + '"' + (cfg.timezone === z ? ' selected' : '') + '>' + esc(z) + '</option>';
          }).join('') + '</select></div></div>' +
      '<div class="field"><label class="flabel">UTC offset (whole hours — legacy fallback)</label>' +
        '<div class="fselect wired"><select id="st-utc">' + offs.map(function (o) {
          return '<option value="' + o + '"' + (Number(cfg.utc_offset || 0) === o ? ' selected' : '') + '>UTC' +
            (o >= 0 ? '+' : '') + o + ':00</option>';
        }).join('') + '</select></div></div>' +
      '<div class="kv"><div class="k">Creator local time right now</div>' +
        '<div class="v good" id="st-now">' + esc(clockNow(cfg)) + '</div></div>' +
      '</div>' +

      '<div class="card2"><div class="t" id="st-lang">Language</div>' +
      '<div class="d">Drives the outbound language pack for this creator. The list below is shipped by the ' +
        'relay itself (<code>response.languages[]</code>) — it is not hardcoded here.</div>' +
      '<div class="field" style="margin-top:18px;margin-bottom:0"><div class="fselect wired"><select id="st-langsel">' +
        langs.map(function (l) {
          return '<option value="' + esc(l.code) + '"' + ((cfg.language || 'en') === l.code ? ' selected' : '') +
            '>' + esc(l.label) + ' (' + esc(l.code) + ')</option>';
        }).join('') + '</select></div></div></div>' +

      '<div class="card2"><div class="t" id="st-prefnb">Weekly-report start day · display mode · platform sync</div>' +
      '<div class="d">No relay route stores any of these — there is no reports-week setting, no theme column ' +
        '(these pages are dark-only by design), and no OnlyFans display-sync endpoint. Left inert instead of ' +
        'faking a save.</div>' +
      '<div class="taglist"><span class="tag">weekly report start — no backend</span>' +
        '<span class="tag">light / dark — skin only</span>' +
        '<span class="tag">sync to OnlyFans — no backend</span></div></div>';

    Fastt.liveBadge(document.getElementById('st-clock'));
    Fastt.liveBadge(document.getElementById('st-lang'));
    Fastt.staticBadge(document.getElementById('st-prefnb'), 'NO BACKEND');

    setInterval(function () {
      var el = document.getElementById('st-now');
      if (el) el.textContent = clockNow(cfg);
    }, 30000);

    async function save(changes, label) {
      try {
        var out = await patchConfig(changes);
        cfg = out.config || cfg;
        document.getElementById('st-now').textContent = clockNow(cfg);
        Fastt.saved(label + ' saved ✓');
      } catch (e) { Fastt.oops(e); }
    }
    document.getElementById('st-tz').addEventListener('change', function (e) {
      save({ timezone: e.target.value || null }, 'Time zone');
    });
    document.getElementById('st-utc').addEventListener('change', function (e) {
      save({ utc_offset: parseInt(e.target.value, 10) }, 'UTC offset');
    });
    document.getElementById('st-langsel').addEventListener('change', function (e) {
      save({ language: e.target.value }, 'Language');
    });
  }

  // ══════════════════════════════════════════════════════════════
  // 3. SECURITY (ORGANIZATION) — secrets + audit log
  // ══════════════════════════════════════════════════════════════
  function renderSecrets(sec) {
    var host = document.getElementById('st-secrets');
    var groups = {};
    Object.keys(sec.keys || {}).forEach(function (name) {
      var k = sec.keys[name];
      (groups[k.group || 'Other'] = groups[k.group || 'Other'] || []).push([name, k]);
    });
    var nSet = Object.keys(sec.keys || {}).filter(function (n) { return sec.keys[n].set; }).length;
    var rows = Object.keys(groups).map(function (g) {
      return '<div style="font-size:12.5px;font-weight:600;color:#8a8a8a;text-transform:uppercase;' +
        'letter-spacing:.06em;margin:18px 0 4px">' + esc(g) + '</div>' +
        groups[g].map(function (pair) {
          var name = pair[0], k = pair[1];
          var val = k.set
            ? '<span style="color:#67d1ae;font-weight:600">' + (k.hint ? esc(k.hint) : 'set') + '</span>' +
              '<span style="color:#6a6a6a;margin-left:8px">from ' + esc(k.source) + '</span>'
            : '<span style="color:#8a8a8a">not set</span>';
          return '<div class="syncrow" title="' + esc(k.help || '') + '" data-key="' + esc(name) + '">' +
            '<span class="nm">' + esc(k.label) + ' <span style="color:#6a6a6a;font-size:12.5px">(' + esc(name) + ')</span></span>' +
            '<span style="margin-left:auto;display:flex;align-items:center;gap:14px;font-size:14px">' + val +
              '<a class="act" data-sec="set" style="color:#6d8bfb;cursor:pointer">' + (k.set ? 'Replace' : 'Set') + '</a>' +
              (k.set && k.source !== 'env'
                ? '<a class="act" data-sec="clear" style="color:#8a8a8a;cursor:pointer">Clear</a>' : '') +
            '</span></div>';
        }).join('');
    }).join('');
    host.innerHTML =
      '<div class="card2"><div class="t" id="st-keys">API keys &amp; secrets</div>' +
      '<div class="d">Masked inventory from the relay’s secret store — raw values never reach the browser. ' +
        nSet + ' of ' + Object.keys(sec.keys || {}).length + ' keys set. Editing writes through ' +
        '<code>PUT /admin/secrets</code>; an empty value clears the key. Keys sourced from <code>env</code> ' +
        'are process environment and can be overridden but not cleared from here.</div>' + rows + '</div>';
    Fastt.liveBadge(document.getElementById('st-keys'));
    host.querySelectorAll('[data-sec]').forEach(function (a) {
      a.addEventListener('click', async function () {
        var name = a.closest('.syncrow').dataset.key;
        var k = sec.keys[name];
        if (a.dataset.sec === 'clear') {
          if (!confirm('Clear ' + name + '? Anything depending on it stops working immediately.')) return;
          try { renderSecrets(await Fastt.put('/admin/secrets', JSON.parse('{"' + name + '":""}'))); Fastt.saved('Cleared'); }
          catch (e) { Fastt.oops(e); }
          return;
        }
        var v = prompt('New value for ' + name + (k.multiline ? ' (paste the whole JSON):' : ':'), '');
        if (v === null) return;
        if (!v.trim()) { Fastt.toast('Empty — use Clear to unset a key', 'err'); return; }
        if (!confirm('Write a new ' + name + ' into the relay secret store?')) return;
        var b = {}; b[name] = v.trim();
        try { renderSecrets(await Fastt.put('/admin/secrets', b)); Fastt.saved('Key saved ✓'); }
        catch (e) { Fastt.oops(e); }
      });
    });
  }

  function auditRowHtml(a, empName) {
    var pl = '';
    if (a.payload && typeof a.payload === 'object') {
      var p = a.payload;
      if (p.text) pl = '<div class="sub">“' + esc(String(p.text).slice(0, 120)) + '”</div>';
      else {
        var ks = Object.keys(p).slice(0, 4);
        if (ks.length) pl = '<div class="sub">' + esc(ks.map(function (k) {
          return k + '=' + JSON.stringify(p[k]).slice(0, 36);
        }).join(' · ')) + '</div>';
      }
    }
    return '<tr><td style="white-space:nowrap">' + esc(Fastt.fmtAgo(a.at)) +
      '<div class="sub">' + esc(Fastt.fmtDate(a.at)) + '</div></td>' +
      '<td>' + esc(empName || (a.employee_id != null ? '#' + a.employee_id : 'system')) + '</td>' +
      '<td>' + esc(a.action) + pl + '</td>' +
      '<td style="white-space:nowrap">' + esc(a.account_id ? (creators[String(a.account_id)] || a.account_id) : '—') + '</td></tr>';
  }

  var roster = null;   // /admin/employees rows, shared by audit + role + time
  async function loadRoster() {
    if (roster) return roster;
    try {
      var out = await Fastt.get('/admin/employees', { include_disabled: 'true' });
      roster = out.employees || [];
    } catch (e) { warn(e, 'employees'); roster = []; }
    return roster;
  }
  /** /admin/employees deliberately hides the system "Automation" sentinel
   *  (user_id IS NULL), but its id DOES appear on audit rows. per-employee
   *  stats carry the display_name for every id the ledger knows, so use it as
   *  the fallback rather than printing a bare "#2". */
  var empNames = null;
  async function loadEmpNames() {
    if (empNames) return empNames;
    empNames = {};
    (await loadRoster()).forEach(function (e) { empNames[e.id] = e.display_name; });
    try {
      var st = await Fastt.get('/admin/stats/per-employee');
      (st.employees || []).forEach(function (r) {
        if (r.employee_id != null && !empNames[r.employee_id]) {
          empNames[r.employee_id] = r.display_name + ' (system)';
        }
      });
    } catch (e) { warn(e, 'per-employee'); }
    return empNames;
  }

  async function buildSecOrg() {
    // 2FA row: say what is actually true about this relay's own sign-in.
    var d2 = document.getElementById('st-2fa-d');
    var authMe = null;
    try { authMe = await Fastt.get('/auth/me'); } catch (e) { warn(e, 'auth/me'); }
    d2.textContent = 'This relay authenticates with a share-token cookie plus username/password '
      + '(POST /auth/login). It ships no second factor and no route to require one — so this toggle '
      + 'has nothing to write. Current session: ' + (authMe ? ('owner “' + (authMe.username || authMe.id) + '”') : 'not signed in') + '.';
    Fastt.staticBadge(document.getElementById('st-2fa').querySelector('.t'), 'NO BACKEND');
    document.getElementById('st-2fa-sw').style.cursor = 'default';

    try { renderSecrets(await Fastt.get('/admin/secrets')); }
    catch (e) { document.getElementById('st-secrets').innerHTML = errBox('GET /admin/secrets — ' + detail(e)); }

    // ---- audit log ----
    var host = document.getElementById('st-audit');
    var rst = await loadRoster();
    var byId = await loadEmpNames();
    host.innerHTML =
      '<div class="card2"><div class="t" id="st-audith">Audit log</div>' +
      '<div class="d">Every mutating call the relay middleware recorded, newest first ' +
        '(<code>GET /admin/audit</code>). Read-only — this table is the record, not a control.</div>' +
      '<div class="rowline">' +
        '<select class="sel2" id="st-au-emp"><option value="">All employees</option>' +
          rst.map(function (e) { return '<option value="' + e.id + '">' + esc(e.display_name) + '</option>'; }).join('') +
        '</select>' +
        '<select class="sel2" id="st-au-acct"><option value="">All creators</option>' +
          Object.keys(creators).map(function (id) {
            return '<option value="' + esc(id) + '">' + esc(creators[id]) + '</option>';
          }).join('') + '</select>' +
        '<span class="sp"></span>' +
        '<button class="btn2" id="st-au-more">Load older</button>' +
      '</div>' +
      '<div class="scroll240"><table class="minitbl"><thead><tr><th>When</th><th>Who</th><th>Action</th><th>Creator</th></tr></thead>' +
      '<tbody id="st-au-body"><tr><td colspan="4" style="color:#8a8a8a">Loading…</td></tr></tbody></table></div></div>';
    Fastt.liveBadge(document.getElementById('st-audith'));

    var offset = 0, PAGE = 25;
    async function loadAudit(reset) {
      if (reset) { offset = 0; document.getElementById('st-au-body').innerHTML = ''; }
      var params = { limit: PAGE, offset: offset };
      var emp = document.getElementById('st-au-emp').value;
      var acct = document.getElementById('st-au-acct').value;
      if (emp) params.employee_id = emp;
      if (acct) params.account_id = acct;
      try {
        var out = await Fastt.api('/admin/audit', { params: params, noAccount: true });
        var rows = out.actions || [];
        var tb = document.getElementById('st-au-body');
        if (reset) tb.innerHTML = '';
        if (!rows.length && !offset) {
          tb.innerHTML = '<tr><td colspan="4" style="color:#8a8a8a">No audit rows for this filter.</td></tr>';
        } else {
          tb.insertAdjacentHTML('beforeend', rows.map(function (a) {
            return auditRowHtml(a, byId[a.employee_id]);
          }).join(''));
        }
        offset += rows.length;
        document.getElementById('st-au-more').style.display = rows.length === PAGE ? '' : 'none';
      } catch (e) {
        document.getElementById('st-au-body').innerHTML =
          '<tr><td colspan="4" style="color:#e05b5b">' + esc(detail(e)) + '</td></tr>';
      }
    }
    document.getElementById('st-au-emp').addEventListener('change', function () { loadAudit(true); });
    document.getElementById('st-au-acct').addEventListener('change', function () { loadAudit(true); });
    document.getElementById('st-au-more').addEventListener('click', function () { loadAudit(false); });
    await loadAudit(true);
  }

  // ══════════════════════════════════════════════════════════════
  // 4. SECURITY (PERSONAL)
  // ══════════════════════════════════════════════════════════════
  async function buildSecPers() {
    var body = document.getElementById('st-secp-body');
    var st = null, drift = null, ofs = null;
    try { st = await Fastt.get('/admin/session/status'); } catch (e) { warn(e, 'session/status'); }
    try { drift = await Fastt.get('/admin/rev/drift'); } catch (e) { warn(e, 'rev/drift'); }
    try { ofs = await Fastt.get('/api/of/v2/users/me/settings'); } catch (e) { warn(e, 'of settings'); }

    var mine = drift && (drift.accounts || []).find(function (r) { return String(r.account_id) === String(accountId); });
    var stale = drift ? (drift.accounts || []).filter(function (r) { return r.stale; }).length : 0;

    var html = '';
    html += '<div class="card2"><div class="t" id="st-sess">Captured OnlyFans session for ' + esc(acctName) + '</div>' +
      '<div class="d">The credential this relay actually uses. Anyone holding this file can act as the creator ' +
        '— that is what “security” means here. <code>GET /admin/session/status</code>.</div>' +
      (st && st.loaded ? '<div class="kv">' +
        '<div class="k">Session file</div><div class="v">' + esc(st.session_file || '—') + '</div>' +
        '<div class="k">Captured</div><div class="v">' + esc(st.captured_at || '—') + '</div>' +
        '<div class="k">Cookies held</div><div class="v">' + esc(st.cookies_count) + '</div>' +
        '<div class="k">Browser profile</div><div class="v">' + esc(st.profile_id || 'direct (no proxy profile)') + '</div>' +
        '<div class="k">Signed app rev</div><div class="v">' + esc(st.x_of_rev || '—') + '</div>' +
        '</div>'
        : '<div class="kv"><div class="k">Session</div><div class="v bad">no session captured for this creator</div></div>') +
      '</div>';

    html += '<div class="card2"><div class="t" id="st-drift">Session freshness</div>' +
      '<div class="d">OnlyFans signs requests with a build revision. When the live build moves past the one your ' +
        'session captured, calls start failing — that is the drift check.</div>' +
      (drift ? '<div class="kv">' +
        '<div class="k">Live OnlyFans rev</div><div class="v">' + esc(drift.live_rev || 'unknown') + '</div>' +
        '<div class="k">This creator’s rev</div><div class="v ' + (mine && mine.stale ? 'warn' : 'good') + '">' +
          esc((mine && mine.session_rev) || 'unknown') + (mine && mine.stale ? ' — STALE, re-capture' : ' — current') + '</div>' +
        '<div class="k">Creators with stale sessions</div><div class="v ' + (stale ? 'warn' : 'good') + '">' +
          esc(stale) + ' of ' + esc((drift.accounts || []).length) + '</div>' +
        '</div>' : '<div class="d" style="color:#e05b5b">drift probe unavailable</div>') +
      '</div>';

    if (ofs) {
      var otp = ['strongOtp', 'phoneOtp', 'appOtp', 'passkeyOtp', 'faceOtp'].filter(function (k) { return ofs[k]; });
      html += '<div class="card2"><div class="t" id="st-ofsec">OnlyFans account security (read-only mirror)</div>' +
        '<div class="d">Straight from <code>GET /api/of/v2/users/me/settings</code>. The relay has no write route ' +
          'for these — change them on onlyfans.com and this panel follows.</div>' +
        '<div class="kv">' +
          '<div class="k">Two-factor on OnlyFans</div><div class="v ' + (otp.length ? 'good' : 'warn') + '">' +
            (otp.length ? esc(otp.join(', ')) : 'none enabled') + '</div>' +
          '<div class="k">Password set</div><div class="v ' + (ofs.hasPassword ? 'good' : 'warn') + '">' +
            (ofs.hasPassword ? 'yes' : 'no') + '</div>' +
          '<div class="k">Private profile</div><div class="v">' + (ofs.isPrivate ? 'yes' : 'no') + '</div>' +
          '<div class="k">DMs from friends only</div><div class="v">' + (ofs.canAcceptMessageOnlyFromFriends ? 'yes' : 'no') + '</div>' +
          '<div class="k">Email notifications</div><div class="v">' + (ofs.isEmailNotificationsEnabled ? 'on' : 'off') + '</div>' +
          '<div class="k">Blocked IPs</div><div class="v">' +
            ((ofs.blockedIps || []).length ? esc((ofs.blockedIps || []).join(', ')) : 'none') + '</div>' +
          '<div class="k">Blocked countries</div><div class="v">' +
            ((ofs.blockedCountries || []).length ? esc((ofs.blockedCountries || []).join(', ')) : 'none') + '</div>' +
          '<div class="k">Blocked states</div><div class="v">' +
            ((ofs.blockedStates || []).length ? esc((ofs.blockedStates || []).join(', ')) : 'none') + '</div>' +
        '</div></div>';
    } else {
      html += errBox('GET /api/of/v2/users/me/settings did not answer — OnlyFans security mirror unavailable.');
    }

    body.className = '';
    body.innerHTML = html;
    if (st) Fastt.liveBadge(document.getElementById('st-sess'));
    if (drift) Fastt.liveBadge(document.getElementById('st-drift'));
    if (ofs) Fastt.liveBadge(document.getElementById('st-ofsec'));
  }

  // ══════════════════════════════════════════════════════════════
  // 5. BILLING → AI Copilot (spend chart drawn from the rows)
  // ══════════════════════════════════════════════════════════════
  var grokCache = null;
  async function loadGrok() {
    if (grokCache) return grokCache;
    var rows = [], cap = null;
    try {
      var out = await Fastt.get('/admin/stats/grok-cost', { account_id: accountId });
      rows = (out.rows || []).filter(function (r) { return String(r.account_id) === String(accountId); });
      rows.sort(function (a, b) { return a.day < b.day ? -1 : 1; });
    } catch (e) { warn(e, 'grok-cost'); }
    try { cap = (await Fastt.get('/admin/account-config')).config.daily_cost_cap_cents; }
    catch (e) { warn(e, 'cap'); }
    grokCache = { rows: rows, cap: cap };
    return grokCache;
  }

  /** Bar chart built entirely from the response: scale, ticks and labels all
   *  come from the data, plus the configured daily cap as an overlay rule. */
  function spendChartSvg(rows, capCents) {
    var W = 960, H = 250, PL = 56, PR = 16, PT = 16, PB = 40;
    var iw = W - PL - PR, ih = H - PT - PB;
    var maxVal = 0;
    rows.forEach(function (r) { if (r.cost_cents > maxVal) maxVal = r.cost_cents; });
    if (capCents && capCents > maxVal) maxVal = capCents;
    if (maxVal <= 0) maxVal = 1;
    var top = Math.ceil(maxVal * 1.12);
    var y = function (v) { return PT + ih - (v / top) * ih; };
    var bw = Math.max(4, (iw / Math.max(rows.length, 1)) * 0.62);
    var step = iw / Math.max(rows.length, 1);

    var grid = '';
    [0, 0.25, 0.5, 0.75, 1].forEach(function (f) {
      var v = top * f, yy = y(v);
      grid += '<line x1="' + PL + '" y1="' + yy.toFixed(1) + '" x2="' + (W - PR) + '" y2="' + yy.toFixed(1) +
        '" stroke="#2c2c2c" stroke-width="1"' + (f ? ' stroke-dasharray="3 4"' : '') + '/>' +
        '<text x="' + (PL - 10) + '" y="' + (yy + 4).toFixed(1) + '" text-anchor="end" fill="#6a6a6a" ' +
        'font-size="11" font-family="Inter,sans-serif">$' + (v / 100).toFixed(2) + '</text>';
    });

    var bars = rows.map(function (r, i) {
      var h = Math.max(1, (r.cost_cents / top) * ih);
      var x = PL + i * step + (step - bw) / 2;
      var fill = r.is_capped ? '#ec4b9b' : '#4166f6';
      return '<rect x="' + x.toFixed(1) + '" y="' + (PT + ih - h).toFixed(1) + '" width="' + bw.toFixed(1) +
        '" height="' + h.toFixed(1) + '" rx="2" fill="' + fill + '"><title>' +
        esc(r.day) + ' — ' + fmtCents(r.cost_cents) + ' · ' + fmtInt(r.call_count) + ' calls' +
        (r.is_capped ? ' · CAPPED' : '') + '</title></rect>';
    }).join('');

    var labels = rows.map(function (r, i) {
      var everyN = Math.ceil(rows.length / 10);
      if (i % everyN !== 0 && i !== rows.length - 1) return '';
      var x = PL + i * step + step / 2;
      return '<text x="' + x.toFixed(1) + '" y="' + (H - PB + 20) + '" text-anchor="middle" fill="#6a6a6a" ' +
        'font-size="11" font-family="Inter,sans-serif">' + esc(r.day.slice(5)) + '</text>';
    }).join('');

    // The cap rule is drawn, but its label lives in the legend under the chart —
    // on a creator whose peak day is 60x the cap the in-chart label would sit on
    // top of the axis ticks.
    var capLine = '';
    if (capCents != null) {
      var cy = y(capCents);
      capLine = '<line x1="' + PL + '" y1="' + cy.toFixed(1) + '" x2="' + (W - PR) + '" y2="' + cy.toFixed(1) +
        '" stroke="#e0b25a" stroke-width="1.5" stroke-dasharray="6 4"/>';
    }
    return '<svg viewBox="0 0 ' + W + ' ' + H + '" role="img" aria-label="Daily LLM spend">' +
      grid + bars + capLine + labels + '</svg>';
  }

  async function buildBillingAi() {
    var host = document.getElementById('st-bill-ai');
    var g = await loadGrok();
    if (!g.rows.length) {
      host.className = '';
      host.innerHTML = '<div class="card2"><div class="t" id="st-noai">No LLM spend recorded for ' + esc(acctName) + '</div>' +
        '<div class="d">The <code>grok_daily_cost</code> rollup has no rows for this creator, so there is nothing ' +
          'to chart. It fills in as soon as an AI automation makes its first call.</div></div>';
      Fastt.staticBadge(document.getElementById('st-noai'), 'EMPTY — NO ROWS');
      return;
    }
    var total = 0, calls = 0;
    g.rows.forEach(function (r) { total += r.cost_cents; calls += r.call_count; });
    var today = g.rows[g.rows.length - 1];
    var capped = g.rows.filter(function (r) { return r.is_capped; }).length;

    host.className = '';
    host.innerHTML =
      '<div class="card2"><div class="t" id="st-aispend">LLM spend — ' + esc(acctName) + '</div>' +
      '<div class="d">The only metered cost in fastt. Every bar is one row of ' +
        '<code>GET /admin/stats/grok-cost</code>; the dashed rule is <code>config.daily_cost_cap_cents</code>. ' +
        'Pink bars are days the cap actually stopped calls.</div>' +
      '<div class="statgrid">' +
        '<div class="stat"><div class="sv accent">' + fmtCents(total) + '</div><div class="sl">Total across ' + g.rows.length + ' recorded days</div></div>' +
        '<div class="stat"><div class="sv">' + fmtInt(calls) + '</div><div class="sl">LLM calls</div></div>' +
        '<div class="stat"><div class="sv">' + fmtCents(Math.round(total / g.rows.length)) + '</div><div class="sl">Average per active day</div></div>' +
        '<div class="stat"><div class="sv ' + (capped ? '' : 'green') + '">' + capped + '</div><div class="sl">Days the cap bit</div></div>' +
        '<div class="stat"><div class="sv">' + fmtCents(today.cost_cents) + '</div><div class="sl">Latest day (' + esc(today.day) + ')</div></div>' +
      '</div>' +
      '<div class="chartbox">' + spendChartSvg(g.rows, g.cap) +
        '<div class="chartlegend">' +
          '<span><i class="lg" style="background:#4166f6"></i>daily spend</span>' +
          '<span><i class="lg" style="background:#ec4b9b"></i>cap stopped calls that day</span>' +
          (g.cap != null ? '<span><i class="dashrule"></i>configured daily cap <b>' + fmtCents(g.cap) + '</b></span>' : '') +
          '<span>peak day <b>' + fmtCents(Math.max.apply(null, g.rows.map(function (r) { return r.cost_cents; }))) + '</b></span>' +
        '</div></div>' +
      '<table class="minitbl"><thead><tr><th>Day</th><th>Spend</th><th>Calls</th><th>Cost / call</th><th>Capped</th></tr></thead><tbody>' +
      g.rows.slice().reverse().slice(0, 12).map(function (r) {
        return '<tr><td>' + esc(r.day) + '</td><td>' + fmtCents(r.cost_cents) + '</td><td>' + fmtInt(r.call_count) +
          '</td><td>' + (r.call_count ? fmtCents(r.cost_cents / r.call_count) : '—') + '</td><td>' +
          (r.is_capped ? '<span style="color:#ec4b9b">yes</span>' : '<span style="color:#67d1ae">no</span>') + '</td></tr>';
      }).join('') + '</tbody></table></div>';
    Fastt.liveBadge(document.getElementById('st-aispend'));
  }

  function wireBilling() {
    Fastt.staticBadge(document.getElementById('st-bill-actions'), 'NO BILLING BACKEND');
    document.getElementById('st-bill-actions').querySelectorAll('.btn').forEach(function (b) {
      b.disabled = true; b.style.opacity = '.5'; b.style.cursor = 'not-allowed';
      b.title = 'fastt stores no plan, card or invoice — nothing to open';
    });
    var tabs = document.getElementById('st-billtabs');
    tabs.addEventListener('click', async function (e) {
      var t = e.target.closest('.subtab');
      if (!t) return;
      tabs.querySelectorAll('.subtab').forEach(function (x) { x.classList.toggle('on', x === t); });
      var ai = t.dataset.sub === 'ai';
      document.getElementById('st-bill-sub').style.display = ai ? 'none' : '';
      document.getElementById('st-bill-ai').style.display = ai ? '' : 'none';
      if (ai) await ensure('billing-ai', buildBillingAi);
    });
  }

  // ══════════════════════════════════════════════════════════════
  // 6. YOUR BALANCE
  // ══════════════════════════════════════════════════════════════
  async function buildBalance() {
    var body = document.getElementById('st-bal-body');
    var me = await loadOfMe();
    var ref = null;
    try { ref = await Fastt.get('/api/of/v2/payments/referrals/balance'); } catch (e) { warn(e, 'referrals'); }
    var g = await loadGrok();
    var total30 = 0; g.rows.forEach(function (r) { total30 += r.cost_cents; });
    var today = g.rows.length ? g.rows[g.rows.length - 1] : null;
    var todayCents = today && today.day === new Date().toISOString().slice(0, 10) ? today.cost_cents : 0;

    var html = '';
    if (me._error) {
      html += errBox('GET /api/of/v2/users/me — ' + me._error);
    } else {
      html +=
        '<div class="card2"><div class="t" id="st-ofbal">OnlyFans balances</div>' +
        '<div class="d">Read live from the creator’s own account — <code>creditBalance</code> and the ' +
          'send/receive limits OnlyFans enforces. fastt holds no wallet of its own.</div>' +
        '<div class="statgrid">' +
          '<div class="stat"><div class="sv ' + (me.creditBalance ? 'green' : '') + '">' + fmtMoneySafe(me.creditBalance) + '</div><div class="sl">Credit balance</div></div>' +
          '<div class="stat"><div class="sv">' + fmtMoneySafe(ref && ref.referralEarnings) + '</div><div class="sl">Referral earnings</div></div>' +
          '<div class="stat"><div class="sv">$' + esc(me.tipsMin) + ' – $' + esc(me.tipsMax) + '</div><div class="sl">Tip range fans may send</div></div>' +
          '<div class="stat"><div class="sv">$' + esc(me.creditsMin) + ' – $' + esc(me.creditsMax) + '</div><div class="sl">Credit top-up range</div></div>' +
        '</div></div>';
    }

    html +=
      '<div class="card2"><div class="t" id="st-aibudget">fastt AI budget</div>' +
      '<div class="d">The only balance this relay itself controls: today’s LLM spend against ' +
        '<code>daily_cost_cap_cents</code> from the creator config.</div>' +
      '<div class="statgrid">' +
        '<div class="stat"><div class="sv ' + (g.cap != null && todayCents >= g.cap ? '' : 'green') + '">' + fmtCents(todayCents) + '</div><div class="sl">Spent today</div></div>' +
        '<div class="stat"><div class="sv">' + (g.cap == null ? 'not set' : fmtCents(g.cap)) + '</div><div class="sl">Daily cap</div></div>' +
        '<div class="stat"><div class="sv accent">' + fmtCents(total30) + '</div><div class="sl">Recorded lifetime spend (' + g.rows.length + ' days)</div></div>' +
      '</div></div>';

    html +=
      '<div class="card2"><div class="t" id="st-txh">Transaction history</div>' +
      '<div class="d">fastt is not a payment processor: it issues no invoices and holds no ledger of its own, ' +
        'so there is no transaction list to show. What it <i>does</i> record is the per-day LLM spend below ' +
        '(<code>GET /admin/stats/grok-cost</code>). Fan payments live in Analytics, not here.</div>' +
      (g.rows.length
        ? '<div class="scroll240"><table class="minitbl"><thead><tr><th>Day</th><th>LLM spend</th><th>Calls</th></tr></thead><tbody>' +
          g.rows.slice().reverse().map(function (r) {
            return '<tr><td>' + esc(r.day) + '</td><td>' + fmtCents(r.cost_cents) + '</td><td>' + fmtInt(r.call_count) + '</td></tr>';
          }).join('') + '</tbody></table></div>'
        : '<div class="d">No spend rows recorded yet.</div>') +
      '</div>';

    body.className = '';
    body.innerHTML = html;
    if (!me._error) Fastt.liveBadge(document.getElementById('st-ofbal'));
    Fastt.liveBadge(document.getElementById('st-aibudget'));
    Fastt.liveBadge(document.getElementById('st-txh'));
  }
  function fmtMoneySafe(v) {
    return (v === null || v === undefined) ? '—' : Fastt.fmtMoney(v);
  }

  // ══════════════════════════════════════════════════════════════
  // 7. ROLE SETTINGS — real principals + real permission writes
  // ══════════════════════════════════════════════════════════════
  var accessMap = {};
  function grantLabel(a) {
    if (!a) return '<span style="color:#8a8a8a">unreadable</span>';
    if (a.wildcard) return '<span style="color:#67d1ae">All creators (wildcard)</span>';
    var ids = (a.account_ids || []).filter(function (x) { return x; });
    if (!ids.length) return '<span style="color:#8a8a8a">no explicit grant</span>';
    return esc(ids.map(function (id) { return creators[String(id)] || id; }).join(', '));
  }

  function openAccessModal(emp, after) {
    var cur = accessMap[emp.id] || { account_ids: [], wildcard: false };
    var curIds = (cur.account_ids || []).filter(function (x) { return x; }).map(String);
    var ids = Object.keys(creators).sort(function (a, b) { return creators[a].localeCompare(creators[b]); });
    var back = modal('Creator access — ' + esc(emp.display_name),
      '<div style="color:#8a8a8a;font-size:13px;margin-bottom:12px">Replaces the whole grant set ' +
        '(<code>PUT /admin/employees/' + emp.id + '/access</code>). Wildcard means every creator and is ' +
        'exclusive of explicit ids.</div>' +
      '<div class="acheck" data-w="1"><span class="cbx2' + (cur.wildcard ? ' on' : '') + '"></span><b>All creators (wildcard)</b></div>' +
      ids.map(function (id) {
        return '<div class="acheck" data-a="' + esc(id) + '"><span class="cbx2' +
          (curIds.indexOf(id) !== -1 ? ' on' : '') + '"></span>' + esc(creators[id]) +
          ' <span style="color:#6a6a6a;font-size:12px">' + esc(id) + '</span></div>';
      }).join(''),
      '<span class="lft">' + (cur.wildcard ? 'currently: wildcard'
        : (curIds.length ? 'currently: ' + curIds.length + ' creator(s)' : 'currently: no explicit grant')) + '</span>' +
      '<button class="btn2" data-close="1">Cancel</button><button class="btn2 pri" id="st-ac-save">Save access</button>');
    back.querySelector('.smod-b').addEventListener('click', function (ev) {
      var row = ev.target.closest('.acheck'); if (!row) return;
      var box = row.querySelector('.cbx2');
      box.classList.toggle('on');
      if (row.dataset.w && box.classList.contains('on')) {
        back.querySelectorAll('.acheck[data-a] .cbx2').forEach(function (b) { b.classList.remove('on'); });
      } else if (row.dataset.a && box.classList.contains('on')) {
        var w = back.querySelector('.acheck[data-w] .cbx2'); if (w) w.classList.remove('on');
      }
    });
    back.querySelector('#st-ac-save').addEventListener('click', async function () {
      var wildcard = back.querySelector('.acheck[data-w] .cbx2').classList.contains('on');
      var picked = Array.prototype.map.call(back.querySelectorAll('.acheck[data-a] .cbx2.on'),
        function (b) { return b.closest('.acheck').dataset.a; });
      if (!confirm('Write this access set for ' + emp.display_name + '?\n\n' +
        (wildcard ? 'All creators (wildcard)' : (picked.length ? picked.map(function (i) { return creators[i] || i; }).join(', ')
          : 'no creators — revokes every grant')))) return;
      try {
        accessMap[emp.id] = await Fastt.put('/admin/employees/' + emp.id + '/access',
          wildcard ? { account_ids: [], wildcard: true } : { account_ids: picked, wildcard: false });
        back.remove();
        Fastt.saved('Access saved ✓');
        after();
      } catch (e) { Fastt.oops(e); }
    });
  }

  async function buildRole() {
    var body = document.getElementById('st-role-body');
    var users = [], rst = await loadRoster();
    try { users = await Fastt.get('/admin/users'); } catch (e) { warn(e, 'users'); }
    var active = rst.filter(function (e) { return e.is_active; });
    await (async function () {
      for (var i = 0; i < active.length; i += 6) {
        await Promise.all(active.slice(i, i + 6).map(async function (e) {
          try { accessMap[e.id] = await Fastt.get('/admin/employees/' + e.id + '/access'); }
          catch (err) { accessMap[e.id] = null; }
        }));
      }
    })();

    function usersTable() {
      var q = (document.getElementById('st-ru-q') ? document.getElementById('st-ru-q').value : '').trim().toLowerCase();
      var st = document.getElementById('st-ru-st') ? document.getElementById('st-ru-st').value : '';
      var cutoff = Date.now() - 7 * 864e5;
      var rows = users.filter(function (u) {
        if (q && String(u.username || '').toLowerCase().indexOf(q) === -1) return false;
        var seen = Fastt.parseUtc(u.last_seen_at);
        var recent = seen && seen.getTime() >= cutoff;
        if (st === 'recent' && !recent) return false;
        if (st === 'dormant' && recent) return false;
        return true;
      });
      if (!rows.length) return '<tr><td colspan="4" style="color:#8a8a8a">No principals match.</td></tr>';
      return rows.map(function (u) {
        var seen = Fastt.parseUtc(u.last_seen_at);
        var recent = seen && seen.getTime() >= cutoff;
        return '<tr><td>' + esc(u.username) + '<div class="sub">' + esc(String(u.id).slice(0, 12)) + '…</div></td>' +
          '<td>' + esc(u.account_count) + ' creator' + (u.account_count === 1 ? '' : 's') + '</td>' +
          '<td>' + esc(Fastt.fmtAgo(u.last_seen_at)) + '<div class="sub">joined ' + esc(Fastt.fmtAgo(u.created_at)) + '</div></td>' +
          '<td style="color:' + (recent ? '#67d1ae' : '#8a8a8a') + '">' + (recent ? 'active this week' : 'dormant') + '</td></tr>';
      }).join('');
    }

    function grantsTable() {
      return active.map(function (e) {
        return '<tr data-emp="' + e.id + '"><td>' + esc(e.display_name) + '<div class="sub">#' + e.id + '</div></td>' +
          '<td>' + (e.chatter_id ? 'Chatter (linked login)' : 'Employee (roster)') + '</td>' +
          '<td>' + grantLabel(accessMap[e.id]) + '</td>' +
          '<td><a class="act" data-role="edit">Set permissions</a></td></tr>';
      }).join('');
    }

    body.className = '';
    body.innerHTML =
      '<div class="card2"><div class="t" id="st-rolewhat">fastt has no role table</div>' +
      '<div class="d">There is no <code>roles</code> table and no role route on this relay, so no role list is ' +
        'invented here. Authorisation is exactly three real things: <b>sign-in principals</b> ' +
        '(<code>GET /admin/users</code>), <b>per-employee creator grants</b> ' +
        '(<code>GET/PUT /admin/employees/{id}/access</code>), and per-chatter account/folder access for ' +
        'signed-in chatter logins. Both live sets are below.</div></div>' +

      '<div class="card2"><div class="t" id="st-users">Sign-in principals</div>' +
      '<div class="d">Every account that can log into this relay, with how many creators it owns.</div>' +
      '<div class="rowline"><input class="inp2" id="st-ru-q" placeholder="username"><select class="sel2" id="st-ru-st">' +
        '<option value="">Any activity</option><option value="recent">Active this week</option>' +
        '<option value="dormant">Dormant</option></select>' +
        '<span class="sp"></span><button class="btn2 dim" id="st-ru-reset">Reset</button></div>' +
      '<div class="scroll240"><table class="minitbl"><thead><tr><th>Principal</th><th>Owns</th><th>Last seen</th><th>Status</th></tr></thead>' +
      '<tbody id="st-ru-body">' + usersTable() + '</tbody></table></div></div>' +

      '<div class="card2"><div class="t" id="st-grants">Creator access grants</div>' +
      '<div class="d">The permission model that actually gates data: which creators each active employee may ' +
        'touch. Editing writes straight through to the relay.</div>' +
      '<table class="minitbl"><thead><tr><th>Employee</th><th>Kind</th><th>Granted creators</th><th></th></tr></thead>' +
      '<tbody id="st-gr-body">' + grantsTable() + '</tbody></table></div>';

    Fastt.staticBadge(document.getElementById('st-rolewhat'), 'NO BACKEND');
    Fastt.liveBadge(document.getElementById('st-users'));
    Fastt.liveBadge(document.getElementById('st-grants'));
    Fastt.staticBadge(document.getElementById('st-role-add'), 'NO BACKEND');
    var addBtn = document.getElementById('st-role-add');
    addBtn.disabled = true; addBtn.style.opacity = '.5'; addBtn.style.cursor = 'not-allowed';

    function refreshUsers() { document.getElementById('st-ru-body').innerHTML = usersTable(); }
    document.getElementById('st-ru-q').addEventListener('input', Fastt.debounce(refreshUsers, 150));
    document.getElementById('st-ru-st').addEventListener('change', refreshUsers);
    document.getElementById('st-ru-reset').addEventListener('click', function () {
      document.getElementById('st-ru-q').value = '';
      document.getElementById('st-ru-st').value = '';
      refreshUsers();
    });
    document.getElementById('st-gr-body').addEventListener('click', function (ev) {
      var a = ev.target.closest('[data-role="edit"]'); if (!a) return;
      var id = Number(a.closest('tr').dataset.emp);
      var emp = active.find(function (e) { return e.id === id; });
      if (emp) openAccessModal(emp, function () {
        document.getElementById('st-gr-body').innerHTML = grantsTable();
      });
    });
  }

  // ══════════════════════════════════════════════════════════════
  // 8. SALES SETTINGS — the attribution the engine actually runs
  // ══════════════════════════════════════════════════════════════
  var RULES = [
    ['1:1 message', 'The employee id stamped on the outbound message (<code>messages.sent_by_employee_id</code>) when the fan pays against it.'],
    ['PPV unlock', 'The message that sold it — the unlock is linked to its PPV message, so the sender takes the sale.'],
    ['Tip replying to a message', 'Same link: the tip carries the message it answered, so it lands on that sender.'],
    ['Standalone tip (no message link)', 'Last outbound message to that fan inside a <b>7-day lookback</b>. This is what the shipped view does — not the “1 hour” the mock-up printed.'],
    ['Automation sends', 'Attributed to the system <b>Automation</b> employee, which is why it dominates the split below.'],
    ['Nothing links', 'Falls into the synthetic <b>Unattributed</b> bucket so the revenue stays visible instead of vanishing.'],
    ['Posts · subscriptions · streams · referrals', 'Ingested for revenue totals but never attributed to a person — no sender exists on those rows.'],
  ];

  async function buildSales() {
    var body = document.getElementById('st-sales-body');
    var cov = null, orph = null;
    try { cov = await Fastt.get('/admin/stats/attribution-coverage'); } catch (e) { warn(e, 'coverage'); }
    try { orph = await Fastt.get('/admin/stats/attribution-orphans'); } catch (e) { warn(e, 'orphans'); }

    var html = '';
    if (cov) {
      document.getElementById('st-sales-window').textContent =
        'window ' + cov.from + ' → ' + cov.to + ' (relay-computed, not a stored stamp)';
      var t = cov.totals || {};
      var per = cov.per_account || [];
      var mine = per.find(function (r) { return String(r.account_id) === String(accountId); }) || per[0] || {};
      var tot = mine.outbound_total || 1;
      var pctH = ((mine.attributed_to_human || 0) / tot) * 100;
      var pctA = ((mine.attributed_to_automation || 0) / tot) * 100;
      var pctU = ((mine.unattributed_count || 0) / tot) * 100;
      html +=
        '<div class="card2"><div class="t" id="st-cov">Attribution coverage — ' + esc(mine.display_name || acctName) + '</div>' +
        '<div class="d">Share of outbound messages that carry a sender. Everything the reports attribute is ' +
          'downstream of this number.</div>' +
        '<div class="statgrid">' +
          '<div class="stat"><div class="sv accent">' + esc(mine.coverage_pct != null ? mine.coverage_pct : t.coverage_pct) + '%</div><div class="sl">Coverage</div></div>' +
          '<div class="stat"><div class="sv">' + fmtInt(mine.outbound_total) + '</div><div class="sl">Outbound messages</div></div>' +
          '<div class="stat"><div class="sv green">' + fmtInt(mine.attributed_to_human) + '</div><div class="sl">To a human</div></div>' +
          '<div class="stat"><div class="sv">' + fmtInt(mine.attributed_to_automation) + '</div><div class="sl">To Automation</div></div>' +
          '<div class="stat"><div class="sv">' + fmtInt(mine.unattributed_count) + '</div><div class="sl">Unattributed</div></div>' +
        '</div>' +
        '<div class="covbar">' +
          '<i style="width:' + pctH.toFixed(2) + '%;background:#67d1ae"></i>' +
          '<i style="width:' + pctA.toFixed(2) + '%;background:#4166f6"></i>' +
          '<i style="width:' + pctU.toFixed(2) + '%;background:#3a3a3a"></i>' +
        '</div>' +
        '<div class="covlegend">' +
          '<span><i class="lg" style="background:#67d1ae"></i>human <b>' + pctH.toFixed(1) + '%</b></span>' +
          '<span><i class="lg" style="background:#4166f6"></i>automation <b>' + pctA.toFixed(1) + '%</b></span>' +
          '<span><i class="lg" style="background:#3a3a3a"></i>unattributed <b>' + pctU.toFixed(1) + '%</b></span>' +
        '</div>' +
        (per.length > 1 ? '<table class="minitbl"><thead><tr><th>Creator</th><th>Outbound</th><th>Coverage</th><th>Unattributed</th></tr></thead><tbody>' +
          per.map(function (r) {
            return '<tr><td>' + esc(r.display_name || r.account_id) + '</td><td>' + fmtInt(r.outbound_total) +
              '</td><td>' + esc(r.coverage_pct) + '%</td><td>' + fmtInt(r.unattributed_count) + '</td></tr>';
          }).join('') + '</tbody></table>' : '') +
        '</div>';
    } else {
      document.getElementById('st-sales-window').textContent = 'coverage window unavailable';
      html += errBox('GET /admin/stats/attribution-coverage did not answer.');
    }

    html +=
      '<div class="card2"><div class="t" id="st-rules">How a sale is attributed</div>' +
      '<div class="d">This is what <code>per_employee_revenue_with_attribution</code> actually does in ' +
        'this build — read off the shipped engine, not a configurable rulebook. There is no route to change it, ' +
        'so nothing here pretends to be editable.</div>' +
      '<table class="minitbl"><thead><tr><th style="width:230px">Sale type</th><th>Attributed to</th></tr></thead><tbody>' +
      RULES.map(function (r) { return '<tr><td style="color:#fff">' + r[0] + '</td><td>' + r[1] + '</td></tr>'; }).join('') +
      '</tbody></table></div>';

    if (orph) {
      var tips = orph.tips || [];
      var sum = 0; tips.forEach(function (t2) { sum += t2.amount_cents; });
      html +=
        '<div class="card2"><div class="t" id="st-orph">Unattributed tips</div>' +
        '<div class="d">Tips whose 7-day lookback found no outbound message. These are the rows the coverage ' +
          'number is missing — real money with no owner.</div>' +
        '<div class="statgrid">' +
          '<div class="stat"><div class="sv">' + fmtInt(tips.length) + '</div><div class="sl">Orphan tips</div></div>' +
          '<div class="stat"><div class="sv accent">' + fmtCents(sum) + '</div><div class="sl">Value with no owner</div></div>' +
        '</div>' +
        (tips.length ? '<div class="scroll240"><table class="minitbl"><thead><tr><th>When</th><th>Fan</th><th>Amount</th></tr></thead><tbody>' +
          tips.slice(0, 40).map(function (t2) {
            return '<tr><td>' + esc(Fastt.fmtDate(t2.occurred_at)) + '<div class="sub">' + esc(Fastt.fmtAgo(t2.occurred_at)) + '</div></td>' +
              '<td>fan ' + esc(t2.fan_id) + '</td><td>' + fmtCents(t2.amount_cents) + '</td></tr>';
          }).join('') + '</tbody></table></div>'
          : '<div class="d">None — every tip in range resolved to a sender.</div>') +
        '</div>';
    }

    html +=
      '<div class="card2"><div class="t" id="st-salesnb">Exclude welcome messages · exclude the 20% OnlyFans fee</div>' +
      '<div class="d">Neither switch exists in the relay: revenue rows are stored gross and welcome sends are ' +
        'attributed like any other message. Shown as inert rather than as toggles that quietly do nothing.</div>' +
      '<div class="taglist"><span class="tag">exclude welcome messages — no backend</span>' +
        '<span class="tag">exclude OF service fee — no backend</span></div></div>';

    body.className = '';
    body.innerHTML = html;
    if (cov) Fastt.liveBadge(document.getElementById('st-cov'));
    Fastt.staticBadge(document.getElementById('st-rules'), 'ENGINE BEHAVIOUR — READ-ONLY');
    if (orph) Fastt.liveBadge(document.getElementById('st-orph'));
    Fastt.staticBadge(document.getElementById('st-salesnb'), 'NO BACKEND');
  }

  // ══════════════════════════════════════════════════════════════
  // 9. TIME TRACKING — observed activity (no clock-in API exists)
  // ══════════════════════════════════════════════════════════════
  async function buildTime() {
    var body = document.getElementById('st-time-body');
    var rst = await loadRoster();
    var names = await loadEmpNames();
    var disabled = {}; rst.forEach(function (e) { if (!e.is_active) disabled[e.id] = 1; });
    // 500 is the route's hard limit, so walk three pages to cover a useful window.
    var rows = [];
    try {
      for (var pg = 0; pg < 3; pg++) {
        var out = await Fastt.api('/admin/audit',
          { params: { limit: 500, offset: pg * 500 }, noAccount: true });
        var got = out.actions || [];
        rows = rows.concat(got);
        if (got.length < 500) break;
      }
    } catch (e) { warn(e, 'audit'); }

    var agg = {};
    rows.forEach(function (a) {
      var key = a.employee_id == null ? 'sys' : a.employee_id;
      var g = agg[key] || (agg[key] = { n: 0, first: null, last: null, days: {}, accts: {} });
      g.n++;
      var d = Fastt.parseUtc(a.at);
      if (d) {
        if (!g.last || d > g.last) g.last = d;
        if (!g.first || d < g.first) g.first = d;
        g.days[d.toISOString().slice(0, 10)] = 1;
      }
      if (a.account_id) g.accts[String(a.account_id)] = 1;
    });
    var keys = Object.keys(agg).sort(function (a, b) { return agg[b].n - agg[a].n; });
    var window = rows.length
      ? (Fastt.fmtDate(rows[rows.length - 1].at) + ' → ' + Fastt.fmtDate(rows[0].at))
      : 'no rows';

    body.className = '';
    body.innerHTML =
      '<div class="card2"><div class="t" id="st-timenb">Clock in / clock out</div>' +
      '<div class="d">There is no shift or time-clock table on this relay and no route to start or stop one, ' +
        'so this toggle would save nothing — it stays off and inert. What the relay <i>does</i> know is when ' +
        'each operator actually acted, straight out of the audit log. That is the table below.</div></div>' +

      '<div class="card2"><div class="t" id="st-obs">Observed activity</div>' +
      '<div class="d">Derived from the newest ' + fmtInt(rows.length) + ' audit rows ' +
        '(<code>GET /admin/audit?limit=500</code>, walked ' + Math.ceil(rows.length / 500) + ' page(s) — 500 is the ' +
        'route’s hard limit), covering ' + esc(window) + '. Not a timesheet — a record of real calls.</div>' +
      (keys.length ? '<div class="scroll240"><table class="minitbl"><thead><tr><th>Operator</th><th>Actions</th>' +
        '<th>Days seen</th><th>Creators touched</th><th>Last action</th></tr></thead><tbody>' +
        keys.map(function (k) {
          var g = agg[k];
          var name = k === 'sys' ? 'unattributed / system' : (names[k] || ('employee #' + k));
          return '<tr><td>' + esc(name) + (disabled[k] ? '<div class="sub">deactivated</div>' : '') + '</td>' +
            '<td>' + fmtInt(g.n) + '</td>' +
            '<td>' + Object.keys(g.days).length + '</td>' +
            '<td>' + (Object.keys(g.accts).length
              ? esc(Object.keys(g.accts).map(function (i) { return creators[i] || i; }).slice(0, 3).join(', ')) : '—') + '</td>' +
            '<td>' + esc(Fastt.fmtAgo(g.last)) + '</td></tr>';
        }).join('') + '</tbody></table></div>'
        : '<div class="d">The audit log is empty — nothing to observe yet.</div>') +
      '</div>';
    Fastt.staticBadge(document.getElementById('st-timenb'), 'NO BACKEND');
    if (keys.length) Fastt.liveBadge(document.getElementById('st-obs'));
    var sw = panel('time').querySelector('.sw');
    if (sw) sw.style.cursor = 'default';
  }

  // ══════════════════════════════════════════════════════════════
  // 10. ABOUT
  // ══════════════════════════════════════════════════════════════
  async function buildAbout() {
    var body = document.getElementById('st-about-body');
    var st = null, drift = null;
    try { st = await Fastt.get('/admin/session/status'); } catch (e) { warn(e, 'session'); }
    try { drift = await Fastt.get('/admin/rev/drift'); } catch (e) { warn(e, 'drift'); }
    body.className = '';
    body.innerHTML =
      '<div class="cli" style="margin-bottom:14px">These pages are an Infloww-styled front end served by the ' +
        'fastt relay itself — “Infloww 5.7.14” is the mock-up’s version string, not this software’s, so it is ' +
        'not printed as if it were.</div>' +
      '<div class="card2" style="max-width:560px;text-align:left"><div class="t" id="st-build">Build identifiers</div>' +
      '<div class="kv">' +
        '<div class="k">Relay origin</div><div class="v">' + esc(location.origin) + '</div>' +
        '<div class="k">Creator in scope</div><div class="v">' + esc(acctName) + ' (' + esc(accountId) + ')</div>' +
        '<div class="k">Session app rev</div><div class="v">' + esc((st && st.x_of_rev) || 'no session') + '</div>' +
        '<div class="k">Live OnlyFans rev</div><div class="v">' + esc((drift && drift.live_rev) || 'unknown') + '</div>' +
        '<div class="k">Browser time</div><div class="v">' + esc(new Date().toLocaleString()) + '</div>' +
      '</div></div>';
    Fastt.liveBadge(document.getElementById('st-build'));
    var btn = panel('about').querySelector('.btn-primary');
    if (btn) btn.remove();
  }

  // ══════════════════════════════════════════════════════════════
  // 11. MESSAGING (kept from the previous pass, still live)
  // ══════════════════════════════════════════════════════════════
  async function buildMessaging() {
    var TR_KEY = 'chatterly:translate-en';
    var sw = document.getElementById('st-translate');
    function reflect() { sw.classList.toggle('on', localStorage.getItem(TR_KEY) === '1'); }
    reflect();
    var trTxt = sw.parentNode.querySelector('.txt');
    Fastt.staticBadge(trTxt.querySelector('.t'), 'BROWSER-LOCAL');
    var trDesc = trTxt.querySelector('.d');
    if (trDesc) {
      trDesc.textContent = 'Turns AI translation on for the fastt pages served by this relay. '
        + 'Stored in this browser only (localStorage “' + TR_KEY + '”) — it does not reach '
        + 'employees or the Messages Pro app on another origin. The translate endpoint itself is live.';
    }
    sw.addEventListener('click', async function () {
      var next = localStorage.getItem(TR_KEY) !== '1';
      if (next) localStorage.setItem(TR_KEY, '1'); else localStorage.removeItem(TR_KEY);
      reflect();
      if (!next) { Fastt.toast('Translate off — bubbles show original text', 'ok'); return; }
      try {
        var probe = await Fastt.post('/admin/translate', { texts: ['hola amigo'], target: 'en' });
        var r = probe.results && probe.results[0];
        Fastt.toast(r ? ('Translate is live: “hola amigo” → “' + r.text + '” (' + r.lang + ')')
                      : 'Translate enabled (probe returned no result — originals will show)', 'ok');
      } catch (e) { Fastt.oops(e); }
    });

    var host = panel('messaging');

    // ---- banned words ----
    var bwCard = document.createElement('div');
    bwCard.className = 'synccard';
    bwCard.style.maxWidth = 'none';
    bwCard.innerHTML =
      '<div class="t">Banned words</div>' +
      '<div class="d">Outbound compliance filter for <b>' + esc(acctName || 'creator') + '</b> — scanned at the ' +
        'send chokepoint (AI seller + mass sends). “Block” drops the message; “Mask” stars the word out.</div>' +
      '<div style="display:flex;gap:12px;align-items:center;flex-wrap:wrap">' +
        '<input class="finput" id="st-bw-words" style="flex:1;min-width:280px;height:44px" placeholder="comma-separated words…">' +
        '<select class="finput" id="st-bw-mode" style="width:130px;height:44px"></select>' +
        '<button class="btn btn-primary" id="st-bw-save" style="height:44px">Save</button>' +
      '</div>';
    host.appendChild(bwCard);
    Fastt.liveBadge(bwCard.querySelector('.t'));
    try {
      var bw = await Fastt.get('/admin/banned-words', { account_id: accountId });
      document.getElementById('st-bw-words').value = (bw.config.words || []).join(', ');
      var modeSel = document.getElementById('st-bw-mode');
      (bw.modes || ['block', 'mask']).forEach(function (m) {
        var o = document.createElement('option');
        o.value = m; o.textContent = m.charAt(0).toUpperCase() + m.slice(1);
        if (m === bw.config.mode) o.selected = true;
        modeSel.appendChild(o);
      });
    } catch (e) { warn(e, 'banned-words'); bwCard.querySelector('.d').innerHTML += ' <span style="color:#e05b5b">— read failed: ' + esc(detail(e)) + '</span>'; }
    document.getElementById('st-bw-save').addEventListener('click', async function () {
      try {
        var words = document.getElementById('st-bw-words').value
          .split(',').map(function (w) { return w.trim(); }).filter(Boolean);
        var out = await Fastt.put('/admin/banned-words', {
          account_id: accountId,
          config: { words: words, mode: document.getElementById('st-bw-mode').value },
        });
        document.getElementById('st-bw-words').value = (out.config.words || []).join(', ');
        Fastt.saved();
      } catch (e) { Fastt.oops(e); }
    });

    // ---- webhooks ----
    var whCard = document.createElement('div');
    whCard.className = 'synccard';
    whCard.style.maxWidth = 'none';
    whCard.innerHTML =
      '<div class="t">Webhooks</div>' +
      '<div class="d">Every matched relay event is POSTed as JSON to these URLs (event type “*” = all events).</div>' +
      '<div id="st-wh-list"></div>' +
      '<button class="btn btn-outline" id="st-wh-add" style="margin-top:14px">Add webhook</button>';
    host.appendChild(whCard);
    Fastt.liveBadge(whCard.querySelector('.t'));

    function renderWebhooks(cfg) {
      var list = document.getElementById('st-wh-list');
      var byUrl = {};
      Object.keys(cfg || {}).forEach(function (type) {
        (cfg[type] || []).forEach(function (url) { (byUrl[url] = byUrl[url] || []).push(type); });
      });
      var urls = Object.keys(byUrl);
      if (!urls.length) {
        list.innerHTML = '<div class="syncrow"><span class="nm" style="color:#8a8a8a">No webhooks registered</span></div>';
        return;
      }
      list.innerHTML = urls.map(function (url) {
        return '<div class="syncrow" data-url="' + esc(url) + '">' +
          '<span class="nm">' + esc(url) + ' <span style="color:#6a6a6a;font-size:12.5px">(' + esc(byUrl[url].join(', ')) + ')</span></span>' +
          '<span data-whact="del" style="margin-left:auto;color:#8a8a8a;cursor:pointer;font-size:14px">Remove</span></div>';
      }).join('');
    }
    try { renderWebhooks(await Fastt.get('/admin/webhooks')); }
    catch (e) { warn(e, 'webhooks'); document.getElementById('st-wh-list').innerHTML =
      '<div class="syncrow"><span class="nm" style="color:#e05b5b">webhook list failed: ' + esc(detail(e)) + '</span></div>'; }

    document.getElementById('st-wh-add').addEventListener('click', async function () {
      var url = prompt('Webhook URL (every matched event is POSTed there as JSON):');
      if (!url || !/^https?:\/\//.test(url.trim())) {
        if (url !== null) Fastt.toast('URL must start with http(s)://', 'err');
        return;
      }
      var types = prompt('Event types, comma-separated (* = all events):', '*');
      if (types === null) return;
      var list = types.split(',').map(function (s) { return s.trim(); }).filter(Boolean);
      try {
        var out = await Fastt.post('/admin/webhooks', { url: url.trim(), event_types: list.length ? list : ['*'] });
        renderWebhooks(out.config);
        Fastt.saved('Webhook added ✓');
      } catch (e) { Fastt.oops(e); }
    });
    whCard.addEventListener('click', async function (e) {
      var act = e.target.closest('[data-whact="del"]');
      if (!act) return;
      var url = act.closest('.syncrow').dataset.url;
      if (!confirm('Remove webhook ' + url + ' from all event types?')) return;
      try {
        var out = await Fastt.del('/admin/webhooks?url=' + encodeURIComponent(url) + '&event_type=*');
        renderWebhooks(out.config);
        Fastt.saved('Webhook removed');
      } catch (err) { Fastt.oops(err); }
    });
  }

  // ══════════════════════════════════════════════════════════════
  // lazy tab builder
  // ══════════════════════════════════════════════════════════════
  var built = {};
  async function ensure(key, fn) {
    if (built[key]) return;
    built[key] = true;
    try { await fn(); }
    catch (e) {
      warn(e, 'panel ' + key);
      Fastt.toast('Couldn’t build “' + key + '”: ' + detail(e), 'err');
    }
  }
  var BUILDERS = {
    account: buildAccount,
    preferences: buildPreferences,
    secorg: buildSecOrg,
    secpers: buildSecPers,
    billing: function () { wireBilling(); },
    balance: buildBalance,
    role: buildRole,
    sales: buildSales,
    time: buildTime,
    messaging: buildMessaging,
    about: buildAbout,
  };
  document.querySelectorAll('#tablist .tab').forEach(function (t) {
    t.addEventListener('click', function () {
      var key = t.getAttribute('data-tab');
      if (BUILDERS[key]) ensure(key, BUILDERS[key]);
    });
  });

  if (!accountId) {
    document.getElementById('st-acct-body').textContent =
      'No creator selected — pick one from the switcher in the top bar to load real data.';
    return;
  }
  await ensure('account', buildAccount);
});
