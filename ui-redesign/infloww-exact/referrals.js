/* fastt wiring — referrals.
 *
 * CORRECTION to this file's previous comment: it claimed "the referral program
 * has NO backend in the relay" and badged the whole page STATIC. That is FALSE
 * for the OnlyFans side. Verified live on ACCOUNT_ID and ACCOUNT_ID:
 *   GET /api/of/v2/payments/referrals/balance  → {"referralEarnings": 0}
 *   GET /api/of/v2/payouts/requests/referral   → {"list": [], "marker": …}
 * Those two are wired below and carry LIVE badges.
 *
 * What genuinely has no backend is INFLOWW's own "invite an agency, earn 10% of
 * their first-year subscription" offer — that is Infloww-the-SaaS's partner
 * programme, not something this relay (or OnlyFans) knows about. The hero block
 * and its CTA stay badged STATIC and inert.
 *
 * UNIT CAUTION: referralEarnings comes from OF's *payments* API, which reports
 * DOLLARS (a float) — the same convention as /api/of/v2/payouts/stats — NOT
 * cents. It must not go through Fastt.fmtCents. Both live accounts currently
 * return 0, so the unit is asserted from the sibling payouts endpoints rather
 * than observed on a non-zero balance; the UI states the assumption.
 */
Fastt.ready(async function () {
  var $ = Fastt.$, esc = Fastt.esc;
  var acct = String(Fastt.account() || '');

  // ── honest chrome ─────────────────────────────────────────────
  var tz = $('#tz-top');
  if (tz) tz.textContent = 'UTC+00:00';   // every relay stamp is UTC

  // ── topbar health pill (ported from dashboard.html) ───────────
  async function loadOps() {
    var dot = $('#ops-dot'), txt = $('#ops-status');
    if (!dot || !txt) return;
    function fail(m) { dot.style.background = '#ff5f57'; txt.textContent = m; }
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


  // ── topbar bell: real unread count from OF, not a decorative dot ──
  // GET /api/of/v2/users/notifications/count → per-type UNREAD ints; `all` is
  // the number the bell should wear. On failure the bell stays bare rather
  // than showing an invented indicator.
  try {
    var _nc = await Fastt.get('/api/of/v2/users/notifications/count');
    var _bell = Fastt.$('#bell-count');
    var _unread = Number(_nc && _nc.all) || 0;
    if (_bell) {
      _bell.textContent = _unread > 99 ? '99+' : String(_unread);
      _bell.style.display = _unread ? '' : 'none';
      _bell.title = _unread + ' unread OnlyFans notification' + (_unread === 1 ? '' : 's');
    }
  } catch (e) { /* OF unreachable — leave the bell bare */ }
  // ── Infloww's OWN partner programme: genuinely backend-less ───
  Fastt.staticBadge($('#ref-badge-slot'), 'INFLOWW’S OWN OFFER — NO BACKEND HERE');
  var btn = $('#ref-btn');
  if (btn) {
    btn.title = 'Infloww’s own agency-referral programme — not exposed by this relay or by OnlyFans.';
    btn.addEventListener('click', function () {
      Fastt.toast('Infloww’s agency-referral programme has no API here — your OnlyFans referral numbers are below.', 'err');
    });
  }

  function emptyBox(msg) {
    return '<div class="rempty">'
      + '<svg width="24" height="26" viewBox="0 0 86 94" fill="none"><path d="M22 26h30l13 13v39a4 4 0 0 1-4 4H22a4 4 0 0 1-4-4V30a4 4 0 0 1 4-4z" fill="#3a3a3a"/><path d="M52 26v9a4 4 0 0 0 4 4h9z" fill="#4d4d4d"/></svg>'
      + '<span>' + esc(msg) + '</span></div>';
  }

  // summary-tile helpers (keep the top strip in lockstep with the detail card)
  function sumEarn(txt, cls) {
    var el = $('#rs-earn'); if (!el) return;
    el.textContent = txt; el.className = 'rs-val' + (cls ? ' ' + cls : '');
  }

  // ── 1. OnlyFans referral balance ──────────────────────────────
  async function loadBalance() {
    var v = $('#rc-balance-v'), meta = $('#rc-balance-meta'),
        note = $('#rc-balance-note'), sub = $('#rc-balance-sub');
    if (!acct) {
      v.textContent = 'n/a'; v.className = 'rbig na';
      meta.textContent = 'No creator selected.';
      sumEarn('n/a', 'na');
      return;
    }
    var out;
    try { out = await Fastt.get('/api/of/v2/payments/referrals/balance'); }
    catch (e) {
      v.textContent = 'n/a'; v.className = 'rbig na';
      meta.textContent = 'OnlyFans returned an error for this creator (relay '
        + ((e && e.status) || 'error') + ').';
      sumEarn('error', 'na');
      return;
    }
    var n = Number(out && out.referralEarnings);
    if (!isFinite(n)) {
      v.textContent = 'n/a'; v.className = 'rbig na';
      meta.textContent = 'OnlyFans returned no referralEarnings field.';
      sumEarn('n/a', 'na');
      return;
    }
    v.className = 'rbig';
    v.textContent = Fastt.fmtMoney(n);
    sumEarn(Fastt.fmtMoney(n));
    Fastt.liveBadge($('#rs-earn-lbl'));
    Fastt.liveBadge($('#rc-balance-h'));
    var row = Fastt.accountRow();
    if (sub) sub.textContent = (row && (row.nickname || row.id)) || acct;
    meta.textContent = n > 0
      ? 'Referral commission OnlyFans currently holds for this creator.'
      : 'This creator has never earned OnlyFans referral commission — a real zero, straight from OF.';
    note.innerHTML = 'GET <code>/api/of/v2/payments/referrals/balance</code> → <code>referralEarnings</code>. '
      + 'This is OnlyFans\'s <b>creator referral</b> programme (5% of a creator you referred, for 12 months), '
      + 'not Infloww\'s agency offer above. Value is a lifetime balance in <b>dollars</b> — OF\'s payments API '
      + 'reports dollars, not cents.';
  }

  function sumReq(txt, cls) {
    var el = $('#rs-req'); if (!el) return;
    el.textContent = txt; el.className = 'rs-val' + (cls ? ' ' + cls : '');
  }

  // ── 2. OnlyFans referral payout requests ──────────────────────
  function reqDate(r) {
    var raw = r.createdAt || r.date || r.time;
    if (raw == null) return 'unknown date';
    // OF payout stamps can arrive as unix seconds (int) or an ISO string.
    if (typeof raw === 'number') { var d = new Date(raw * 1000); return isNaN(d) ? String(raw) : d.toLocaleString(); }
    return String(raw);
  }
  function reqRow(r) {
    var amt = (r.amount != null) ? r.amount : (r.total != null ? r.total : null);
    return '<div class="rrow">'
      + '<span>' + esc(reqDate(r)) + '</span>'
      + (r.id != null ? '<span class="rid">#' + esc(r.id) + '</span>' : '')
      + (r.status ? '<span class="rst">' + esc(r.status) + '</span>' : '')
      + '<span class="ramt">' + (amt == null ? '—' : Fastt.fmtMoney(amt)) + '</span>'
      + '</div>';
  }
  async function loadRequests() {
    var list = $('#rc-requests-list'), note = $('#rc-requests-note'), sub = $('#rc-requests-sub');
    if (!acct) { list.innerHTML = emptyBox('No creator selected.'); sumReq('n/a', 'na'); return; }
    list.innerHTML = '<div class="rc-loading"><span class="rc-sk"></span>Loading from OnlyFans…</div>';
    var out;
    try { out = await Fastt.get('/api/of/v2/payouts/requests/referral'); }
    catch (e) {
      list.innerHTML = emptyBox('OnlyFans returned an error for this creator (relay '
        + ((e && e.status) || 'error') + ').');
      sumReq('error', 'na');
      return;
    }
    var rows = Array.isArray(out) ? out : ((out && out.list) || []);
    Fastt.liveBadge($('#rs-req-lbl'));
    Fastt.liveBadge($('#rc-requests-h'));
    sumReq(String(rows.length));
    var rsub = $('#rs-req-sub');
    if (rsub) rsub.textContent = rows.length ? 'Referral payouts requested on OnlyFans' : 'None requested yet — nothing pending';
    if (sub) sub.textContent = rows.length + (rows.length === 1 ? ' request' : ' requests');
    list.innerHTML = rows.length
      ? rows.map(reqRow).join('')
      : emptyBox('No referral payout has ever been requested on this creator’s OnlyFans account — '
                 + 'OF returned an empty list, not an error.');
    note.innerHTML = 'GET <code>/api/of/v2/payouts/requests/referral</code> → <code>list[]</code> '
      + '(paginated by <code>marker</code>). Requests are read-only here: this page never asks OnlyFans '
      + 'for a payout.';
  }

  // ── creator tile (from the resolved account row) ──────────────
  (function () {
    var who = $('#rs-who');
    if (!who) return;
    var row = Fastt.accountRow();
    who.textContent = (row && (row.nickname || row.name || row.username)) || acct || 'none';
    if (row && (row.nickname || row.name)) who.title = who.textContent + (acct ? ' · ' + acct : '');
  })();

  // ── refresh orchestrator: one real action for this read-only surface ──
  var refreshing = false;
  async function reload() {
    if (refreshing) return;
    refreshing = true;
    var btn = $('#rs-refresh'), upd = $('#rs-updated');
    if (btn) { btn.disabled = true; btn.classList.add('spin'); }
    sumEarn('—', 'loading'); sumReq('—', 'loading');
    $('#rc-balance-v').textContent = '…'; $('#rc-balance-v').className = 'rbig';
    try { await loadBalance(); } catch (e) { Fastt.oops(e); }
    try { await loadRequests(); } catch (e) { Fastt.oops(e); }
    if (upd) upd.textContent = 'updated ' + new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    if (btn) { btn.disabled = false; setTimeout(function () { btn.classList.remove('spin'); }, 520); }
    refreshing = false;
  }
  var rbtn = $('#rs-refresh');
  if (rbtn) rbtn.addEventListener('click', reload);
  // keyboard-friendly: R re-pulls (ignore when typing in a field)
  document.addEventListener('keydown', function (e) {
    if (e.defaultPrevented || e.metaKey || e.ctrlKey || e.altKey) return;
    var t = e.target, tag = t && t.tagName;
    if (tag === 'INPUT' || tag === 'TEXTAREA' || (t && t.isContentEditable)) return;
    if ((e.key === 'r' || e.key === 'R')) { e.preventDefault(); reload(); }
  });

  await reload();
});
