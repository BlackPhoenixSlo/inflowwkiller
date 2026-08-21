/* ── live wiring: profile promotions ────────────────────────────────────────
 *
 *  campaigns   GET/POST /admin/promotions · PATCH/DELETE /admin/promotions/{id}
 *  dates       GET /api/of/v2/promotions — promotions_api._from_live() hardcodes
 *              created_at=None, so 310/313 rows had no date at all. OF returns
 *              createdAt + finishedAt on its own route; join on of_promo_id.
 *              ⚠️ tz-AWARE stamps — plain new Date(), no Z-stamping fixup.
 *              ⚠️ the relay's OF proxy ignores limit/offset here: it always
 *              returns the newest 100 with hasMore:true. That still covers the
 *              MAX_ROWS window; anything older stays honestly dateless.
 *  engine      GET/POST /admin/automation-rules (kind promo_reactivate),
 *              PATCH /admin/automation-rules/{id}, POST …/{id}/run-now.
 *              The per-campaign "Keep running" ticks are the opt-in; that rule
 *              is the thing that acts on them. Without it, arming does nothing —
 *              so the card says so instead of pretending.
 *  roster      Fastt.accounts() — the left panel used to be two hardcoded
 *              creators ("Aria", "Mia") with invented badge counts.
 *
 *  SAFETY: the automation is created DISABLED with dry_run TRUE. Arming it and
 *  "Check now" are each an explicit click behind confirm().
 * ───────────────────────────────────────────────────────────────────────── */
Fastt.ready(async () => {
  const $ = Fastt.$, esc = Fastt.esc;

  const rowsEl = $('#pm-rows');
  const acct = Fastt.account();

  // ── left creator panel: the real roster, current account selected ────────
  const AV = ['#e0679b', '#5b8def', '#67d1ae', '#a78bfa', '#e5a35b', '#e07a5f'];
  function renderCreators() {
    const all = Fastt.accounts() || [];
    const q = ($('#pm-csearch').value || '').toLowerCase();
    const list = all.filter((a) => !q || String(a.nickname || a.id).toLowerCase().includes(q));
    $('#pm-callcount').textContent = all.length
      ? all.length + ' creator' + (all.length === 1 ? '' : 's')
        + (list.length !== all.length ? ' · ' + list.length + ' shown' : '')
      : 'No creators';
    if (!all.length) {
      $('#pm-creators').innerHTML = '<div style="color:#8a8a8a;font-size:13.5px;padding:10px 8px;line-height:1.6">'
        + 'No creator roster — /admin/accounts is empty for an unauthed caller and the per-model fallback returned nothing. Sign in from the creator selector to list accounts.</div>';
      return;
    }
    if (!list.length) {
      $('#pm-creators').innerHTML = '<div style="color:#8a8a8a;font-size:13.5px;padding:10px 8px">No creator matches “' + esc(q) + '”.</div>';
      return;
    }
    $('#pm-creators').innerHTML = list.map((a) => {
      const name = a.nickname || String(a.id);
      const i = all.findIndex((x) => String(x.id) === String(a.id));
      const on = String(a.id) === String(acct);
      return `<div class="ccard${on ? ' sel' : ''}" data-acct="${esc(a.id)}" title="${on ? 'Currently selected' : 'Switch to ' + esc(name)}">
        <div class="cav" style="background:${esc(a.color || AV[(i < 0 ? 0 : i) % AV.length])};display:flex;align-items:center;justify-content:center;font:700 19px Inter,sans-serif;color:#fff">${esc(name[0].toUpperCase())}</div>
        <div class="cinfo"><div class="cname">${esc(name)}</div>
          <div style="font-size:12px;color:#7a7a7a">${esc(a.id)}${on ? ' · selected' : ''}</div></div>
      </div>`;
    }).join('');
  }
  renderCreators();
  $('#pm-csearch').addEventListener('input', Fastt.debounce(renderCreators, 120));
  $('#pm-creators').addEventListener('click', (e) => {
    const card = e.target.closest('[data-acct]');
    if (!card || String(card.dataset.acct) === String(acct)) return;
    Fastt.setAccount(card.dataset.acct);   // reloads the page scoped to that creator
  });

  if (!acct) {
    rowsEl.innerHTML = '<div class="empty" style="flex:none;padding:70px 0"><div>No creator selected — pick one on the left.</div></div>';
    $('#pm-autoact-sub').textContent = 'No creator selected.';
    $('#pm-addlist-sub').textContent = 'No creator selected.';
    $('#pm-react-blurb').textContent = 'No creator selected.';
    return;
  }
  Fastt.liveBadge($('#pm-head'));
  Fastt.liveBadge($('#pm-insights-head'));
  // Friendlier than a flashed "No data": show a real loading state until the
  // first refresh() resolves, so an empty table never lies during the fetch.
  rowsEl.innerHTML = '<div class="empty" style="flex:none;padding:70px 0"><div>Loading campaigns…</div></div>';
  $('#pm-totals').innerHTML = '<div class="t"><span class="tl">Loading campaign insights…</span><span class="tv" style="font-size:15px;color:#8a8a8a">…</span></div>';
  Fastt.staticBadge($('#pm-rev-note'), 'REVENUE — NOT ATTRIBUTED');
  // Duration is per-campaign; this row only seeds the create modal, it saves nothing.
  Fastt.staticBadge($('#pm-expiry-badge'), 'CREATE-MODAL DEFAULT — NOTHING SAVED');

  const TRASH = '<svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M5 7h14M10 7V5h4v2M6 7l1 13h10l1-13" stroke-linecap="round" stroke-linejoin="round"/></svg>';
  const MAX_ROWS = 80;

  let campaigns = [];
  let ofAvailable = true;
  let showExpired = false;
  let ofDates = null;      // Map of_promo_id → {createdAt, finishedAt}
  let ofDateWindow = 0;    // how many promos OF actually handed back

  const ofDate = (s) => { const d = s ? new Date(s) : null; return d && !isNaN(d.getTime()) ? d : null; };
  const createdOf = (c) => {
    if (c.created_at) return c.created_at;                       // fastt mirror rows
    const j = ofDates && ofDates.get(c.of_promo_id);
    return (j && j.createdAt) || null;
  };
  const finishedOf = (c) => {
    const j = ofDates && ofDates.get(c.of_promo_id);
    return (j && j.finishedAt) || null;
  };
  const fmtWhen = (s) => {
    const d = ofDate(s);
    return d ? d.toLocaleDateString() + ' ' + d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : null;
  };

  const statusPill = (st) =>
    st === 'active'   ? '<span class="pill-in">Active</span>' :
    st === 'finished' ? '<span class="pill-ex">Finished</span>' :
                        '<span style="color:#9a9a9a;font-size:13px">' + esc(st || 'unknown') + '</span>';

  function visible() {
    return campaigns.filter((c) => showExpired || c.status === 'active' || c.auto_reactivate);
  }

  function renderTotals() {
    const active = campaigns.filter((c) => c.status === 'active').length;
    const armed = campaigns.filter((c) => c.auto_reactivate).length;
    const claims = campaigns.reduce((a, c) => a + (c.subscriber_count || 0), 0);
    const dated = campaigns.filter((c) => createdOf(c)).length;
    const t = (label, value, sub) =>
      `<div class="t"><span class="tl">${esc(label)}</span><span class="tv">${value}</span>${sub ? '<span class="tl">' + esc(sub) + '</span>' : ''}</div>`;
    $('#pm-totals').innerHTML =
      t('Campaigns', Fastt.fmtInt(campaigns.length), campaigns.filter((c) => c.id != null).length + ' managed here · ' + campaigns.filter((c) => c.id == null).length + ' read-only')
      + t('Live now', Fastt.fmtInt(active), armed + ' set to keep running')
      + t('Claims', Fastt.fmtInt(claims), 'subscriptions taken on a promo')
      + t('Showing', Fastt.fmtInt(Math.min(visible().length, MAX_ROWS)), showExpired ? 'incl. expired' : 'live + armed only')
      + t('Dated', Fastt.fmtInt(dated), dated < campaigns.length ? 'OF returns its newest ' + (ofDateWindow || 100) : 'all');
  }

  function render() {
    const vis = visible();
    const warn = ofAvailable ? '' :
      '<div class="trow" style="min-height:44px;color:#e2a94e;font-size:13.5px"><div class="td">OnlyFans not reachable right now — showing mirrored campaigns only.</div></div>';
    renderTotals();
    if (!vis.length) {
      rowsEl.innerHTML = warn + '<div class="empty" style="flex:none;padding:70px 0"><div>'
        + (campaigns.length ? 'No live campaigns — flip “Show expired” to see ' + campaigns.length + ' past ones' : 'No promotion campaigns yet')
        + '</div></div>';
      return;
    }
    const shown = vis.slice(0, MAX_ROWS);
    rowsEl.innerHTML = warn + shown.map((c, i) => {
      const cap = (c.subscribe_counts === 0 || c.subscribe_counts == null) ? '∞' : c.subscribe_counts;
      const bits = [];
      if (c.discount_percent != null) bits.push(c.discount_percent + '% off');
      if (c.price_cents != null) bits.push('sub @ ' + Fastt.fmtCents(c.price_cents));
      if (c.subscribe_days) bits.push(c.subscribe_days + 'd access');
      if (c.reactivate_count) bits.push('re-run ×' + c.reactivate_count);
      const when = [];
      when.push(esc(c.source));
      const cd = fmtWhen(createdOf(c));
      if (cd) when.push('created ' + esc(cd));
      const fd = fmtWhen(finishedOf(c));
      if (fd && c.status !== 'active') when.push('ended ' + esc(fd));
      if (!cd) when.push('<span class="nodata" title="OnlyFans only returns its newest 100 promotions, and this one falls outside that window">no date from OF</span>');
      const canManage = c.id != null;
      return `
      <div class="trow" data-i="${i}">
        <div class="td" style="flex:1;min-width:0">
          <div style="display:flex;align-items:center;gap:10px">${esc(c.name || ('Promo #' + c.of_promo_id))} ${statusPill(c.status)}</div>
          <div style="color:#8a8a8a;font-size:12.5px;margin-top:5px">${esc(bits.join(' · ') || '—')}${c.message ? ' · “' + esc(c.message) + '”' : ''}</div>
          <div style="color:#6f6f6f;font-size:12px;margin-top:3px">${when.join(' · ')}</div>
        </div>
        <div class="td" style="width:160px">${Fastt.fmtInt(c.subscriber_count || 0)} / ${cap}</div>
        <div class="td" style="width:230px"><span class="nodata" title="No transaction carries a campaign id, so no revenue can be tied to this promo">not attributed</span></div>
        <div class="td" style="width:200px;display:flex;justify-content:flex-end;align-items:center;gap:12px">
          ${canManage
            ? `<span style="display:flex;align-items:center;gap:8px;color:#9a9a9a;font-size:12.5px">Keep running <span class="tgl blue${c.auto_reactivate ? ' on' : ''}" data-act="keep" title="Re-create this promo on OnlyFans each time it finishes"></span></span>
               <span class="iconbtn2" data-act="del" title="Delete on OnlyFans">${TRASH}</span>`
            : `<span style="color:#6f6f6f;font-size:12px" title="Created outside fastt — re-create it here to manage it">read-only</span>`}
        </div>
      </div>`;
    }).join('') + (vis.length > MAX_ROWS
      ? '<div class="trow" style="min-height:44px;color:#8a8a8a;font-size:13.5px"><div class="td">+ ' + (vis.length - MAX_ROWS) + ' more (older) campaigns not shown</div></div>'
      : '');
  }

  /** OF's own /promotions route carries createdAt + finishedAt. */
  async function loadOfDates() {
    try {
      const out = await Fastt.get('/api/of/v2/promotions');
      const items = (out && out.items) || (Array.isArray(out) ? out : []);
      ofDateWindow = items.length;
      ofDates = new Map(items.filter((p) => p && p.id != null)
        .map((p) => [p.id, { createdAt: p.createdAt || null, finishedAt: p.finishedAt || null }]));
    } catch (e) { ofDates = new Map(); ofDateWindow = 0; }
  }

  async function refresh() {
    const [out] = await Promise.all([Fastt.get('/admin/promotions'), loadOfDates()]);
    campaigns = out.campaigns || [];
    ofAvailable = out.of_available !== false;
    // Live first, then armed, then genuinely newest-first once dates are joined.
    campaigns.sort((a, b) => {
      const s = ((b.status === 'active') - (a.status === 'active'))
        || (b.auto_reactivate - a.auto_reactivate);
      if (s) return s;
      const da = ofDate(createdOf(a)), db = ofDate(createdOf(b));
      if (da && db) return db - da;
      return (db ? 1 : 0) - (da ? 1 : 0);
    });
    render();
    paintReact();
  }

  // ── “Add fans to list”: real list names, honestly inert action ───────────
  async function loadLists() {
    const sel = $('#pm-list-sel');
    try {
      const out = await Fastt.get('/admin/lists');
      const lists = out.lists || [];
      sel.innerHTML = lists.length
        ? lists.map((l) => `<option value="${esc(l.id)}">${esc(l.name)}${l.is_system ? ' (system)' : ''} — ${Fastt.fmtInt(l.member_count || 0)} fans</option>`).join('')
        : '<option>No lists on this account</option>';
      $('#pm-addlist-sub').innerHTML = lists.length
        ? 'These are this account’s real OnlyFans lists. <b style="color:#b5b5b5">Nothing auto-adds promo claimers to them</b> — no producer for that exists anywhere in the relay, so this row saves nothing.'
        : 'This account has no lists yet, and nothing auto-adds promo claimers to a list — that producer does not exist in the relay.';
    } catch (e) {
      sel.innerHTML = '<option>Lists unavailable</option>';
      $('#pm-addlist-sub').textContent = 'Could not read this account’s lists.';
    }
    Fastt.staticBadge($('#pm-addlist'), 'NO AUTO-ADD BACKEND');
  }

  // ── promo_reactivate rule (the engine behind “Keep running”) ────────────
  let rule = null;
  const isDry = (r) => !r || r.payload == null || r.payload.dry_run !== false;   // default TRUE

  function ruleState(r) {
    if (!r) return { label: 'NOT SET UP', color: '#8a8a8a', bg: 'rgba(138,138,138,.12)', bd: '#333' };
    if (!r.is_enabled) return { label: 'OFF', color: '#8a8a8a', bg: 'rgba(138,138,138,.12)', bd: '#333' };
    return isDry(r)
      ? { label: 'DRY RUN', color: '#e2a94e', bg: 'rgba(226,169,78,.12)', bd: 'rgba(226,169,78,.35)' }
      : { label: 'LIVE', color: '#67d1ae', bg: 'rgba(103,209,174,.12)', bd: 'rgba(103,209,174,.35)' };
  }
  const chip = (s) => `<span style="font:600 10px Inter,sans-serif;letter-spacing:.04em;color:${s.color};`
    + `background:${s.bg};border:1px solid ${s.bd};border-radius:99px;padding:2px 8px;vertical-align:middle">${s.label}</span>`;

  function paintReact() {
    const s = ruleState(rule);
    $('#pm-react-state').innerHTML = chip(s);
    $('#pm-autoact-state').innerHTML = chip(s);
    $('#pm-autoact-tgl').classList.toggle('on', !!(rule && rule.is_enabled));
    $('#pm-react-en').classList.toggle('on', !!(rule && rule.is_enabled));
    $('#pm-react-dry').classList.toggle('on', isDry(rule));
    $('#pm-react-mins').value = rule && rule.every_seconds ? Math.max(1, Math.round(rule.every_seconds / 60)) : 60;
    $('#pm-react-max').value = rule && rule.payload && typeof rule.payload.max_reactivations === 'number'
      ? rule.payload.max_reactivations : 0;
    $('#pm-react-save').textContent = rule ? 'Save changes' : 'Create automation';
    $('#pm-react-run').style.display = rule ? 'inline-flex' : 'none';

    const armed = campaigns.filter((c) => c.auto_reactivate).length;
    $('#pm-autoact-sub').innerHTML = rule
      ? 'Master switch for the <code>promo_reactivate</code> automation — the engine that re-creates finished campaigns you ticked “Keep running”. '
        + '<b style="color:#b5b5b5">' + armed + '</b> campaign' + (armed === 1 ? '' : 's') + ' armed'
        + (isDry(rule) ? ' · dry run is ON, so it reports and creates nothing.' : ' · dry run is OFF — it creates real promos.')
      : 'No <code>promo_reactivate</code> automation exists on this account, so “Keep running” below currently does <b style="color:#e2a94e">nothing</b>. '
        + 'Create it in the card under the table first — this switch has no rule to flip.';

    const last = rule && rule.last_run;
    if (!rule) {
      $('#pm-react-last').innerHTML = '<span style="color:#e2a94e">Not created yet.</span> '
        + armed + ' campaign' + (armed === 1 ? '' : 's') + ' ticked “Keep running” — nothing will act on '
        + (armed === 1 ? 'it' : 'them') + ' until this automation exists.';
    } else if (!last) {
      $('#pm-react-last').innerHTML = 'Never run yet · checks every '
        + Math.round((rule.every_seconds || 3600) / 60) + ' min'
        + (rule.next_due_at ? ' · next ' + esc(Fastt.fmtDate(rule.next_due_at)) : '')
        + ' · ' + armed + ' campaign' + (armed === 1 ? '' : 's') + ' armed';
    } else {
      const st = (last.stats && Object.keys(last.stats).length)
        ? ' · ' + Object.entries(last.stats).map(([k, v]) => esc(k) + ' ' + esc(String(v))).join(' · ') : '';
      $('#pm-react-last').innerHTML = 'Last check: <b style="color:#e8e8e8">' + esc(last.status || '?') + '</b>' + st
        + (last.started_at ? ' · ' + esc(Fastt.fmtDate(last.started_at)) : '')
        + (last.error_text ? ' · <span style="color:#f0715f">' + esc(last.error_text) + '</span>' : '')
        + ' · ' + armed + ' campaign' + (armed === 1 ? '' : 's') + ' armed';
    }
  }

  async function loadRule() {
    try { rule = await Fastt.rule('promo_reactivate'); } catch (e) { rule = null; }
    paintReact();
  }

  const formEnabled = () => $('#pm-react-en').classList.contains('on');
  const formDry = () => $('#pm-react-dry').classList.contains('on');

  $('#pm-react-en').addEventListener('click', function () {
    const arming = !this.classList.contains('on');
    if (arming && !confirm('Arm the promo_reactivate automation?\n\nIt will run on a schedule and act on every campaign ticked “Keep running”.'
      + (formDry() ? '\n\nDry run is ON, so it will only report what it would re-create.'
                   : '\n\n⚠️ Dry run is OFF — it will create REAL promotions on OnlyFans.'))) return;
    this.classList.toggle('on', arming);
    $('#pm-react-save').textContent = rule ? 'Save changes *' : 'Create automation *';
  });
  $('#pm-react-dry').addEventListener('click', function () {
    const off = this.classList.contains('on');
    if (off && !confirm('Turn dry run OFF?\n\nThe automation will then create REAL promotions on OnlyFans for every armed campaign that has finished.')) return;
    this.classList.toggle('on', !off);
    $('#pm-react-save').textContent = rule ? 'Save changes *' : 'Create automation *';
  });

  $('#pm-react-save').addEventListener('click', async () => {
    const every_seconds = Math.max(60, (parseInt($('#pm-react-mins').value, 10) || 60) * 60);
    const payload = {
      max_reactivations: parseInt($('#pm-react-max').value, 10) || 0,
      dry_run: formDry(),
    };
    const enabled = formEnabled();
    if (enabled && !payload.dry_run
        && !confirm('Save ENABLED with dry run OFF?\n\nEvery armed campaign that finishes will be re-created on OnlyFans automatically.')) return;
    try {
      if (rule) {
        await Fastt.patch('/admin/automation-rules/' + rule.id, { every_seconds, payload, is_enabled: enabled });
      } else {
        await Fastt.post('/admin/automation-rules', {
          account_id: acct, kind: 'promo_reactivate', name: 'Keep promos running',
          every_seconds, payload, is_enabled: enabled, trigger: { every_seconds },
        });
      }
      Fastt.saved(rule ? 'Automation saved' : 'Automation created');
      await loadRule();
    } catch (e) { Fastt.oops(e); }
  });

  $('#pm-react-run').addEventListener('click', async () => {
    if (!rule) return;
    const savedDry = isDry(rule);           // run-now executes the SAVED payload
    if (!confirm(savedDry
      ? 'Run a dry check now?\n\nIt reports what it would re-create and changes nothing on OnlyFans.'
      : '⚠️ The SAVED config has dry run OFF.\n\nRunning now will create REAL promotions on OnlyFans for every armed campaign that has finished. Continue?')) return;
    try {
      await Fastt.post('/admin/automation-rules/' + rule.id + '/run-now', {});
      Fastt.saved(savedDry ? 'Dry run queued' : 'Check queued');
      setTimeout(loadRule, 3000);
    } catch (e) { Fastt.oops(e); }
  });

  // The settings-row switch is the same rule's is_enabled — flip it directly.
  $('#pm-autoact-tgl').addEventListener('click', async () => {
    if (!rule) {
      Fastt.toast('No promo_reactivate automation yet — create it in the card below the table first.', 'err');
      return;
    }
    const arming = !rule.is_enabled;
    if (arming && !confirm('Auto-activate finished campaigns?\n\nThe promo_reactivate automation starts running every '
      + Math.round((rule.every_seconds || 3600) / 60) + ' min and acts on every campaign ticked “Keep running”.'
      + (isDry(rule) ? '\n\nDry run is ON — it will only report.' : '\n\n⚠️ Dry run is OFF — it will create REAL promotions.'))) return;
    try {
      await Fastt.patch('/admin/automation-rules/' + rule.id, { is_enabled: arming });
      Fastt.saved(arming ? 'Auto-activate on' : 'Auto-activate off');
      await loadRule();
    } catch (e) { Fastt.oops(e); }
  });

  // ── create promotion ────────────────────────────────────────────────────
  function openCreate() {
    const defDays = $('#pm-expiry-days').value || '30';
    const back = document.createElement('div');
    back.className = 'ft-modal-back';
    back.innerHTML = `
      <div class="ft-modal" style="width:380px">
        <h3>Create promotion</h3>
        <input type="text" id="pmm-name" placeholder="Campaign name (internal label)">
        <input type="number" id="pmm-discount" placeholder="Discount % (1-100)" value="50" min="1" max="100">
        <input type="number" id="pmm-counts" placeholder="Max claims (0 = unlimited)" value="10" min="0">
        <input type="number" id="pmm-days" placeholder="Duration days (100% off only)" value="${esc(defDays)}" min="0" max="3650">
        <input type="text" id="pmm-message" placeholder="Public offer message (optional)">
        <div style="font-size:11px;color:#8a8a8a;margin-bottom:10px">Starts a REAL subscription promo on this OnlyFans profile. 100% off = free trial (duration honoured); a partial discount runs until the claim cap fills.</div>
        <div class="ft-err" id="pmm-err"></div>
        <button id="pmm-go">Start on OnlyFans</button>
      </div>`;
    document.body.appendChild(back);
    back.addEventListener('click', (e) => { if (e.target === back) back.remove(); });
    $('#pmm-go', back).addEventListener('click', async () => {
      const errEl = $('#pmm-err', back);
      const discount = parseInt($('#pmm-discount', back).value, 10);
      if (!confirm('Start this promotion on OnlyFans now? Fans will see the discounted price immediately.')) return;
      try {
        await Fastt.post('/admin/promotions', {
          account_id: acct,
          name: $('#pmm-name', back).value.trim(),
          discount: isNaN(discount) ? 50 : discount,
          subscribe_counts: parseInt($('#pmm-counts', back).value, 10) || 0,
          subscribe_days: parseInt($('#pmm-days', back).value, 10) || 0,
          message: $('#pmm-message', back).value.trim(),
          promo_type: 'all',
        });
        back.remove();
        Fastt.saved('Promotion started');
        await refresh();
      } catch (e) {
        errEl.style.display = 'block';
        errEl.textContent = (e.body && e.body.detail) || 'Create failed';
      }
    });
  }

  // ── events ──────────────────────────────────────────────────────────────
  $('#pm-create').addEventListener('click', openCreate);
  $('#pm-crefresh').addEventListener('click', async () => {
    try { await refresh(); await loadRule(); Fastt.toast('Refreshed', 'ok'); } catch (e) { Fastt.oops(e); }
  });
  $('#pm-showexp').addEventListener('click', function () {
    showExpired = !showExpired;
    this.classList.toggle('on', showExpired);
    render();
  });
  rowsEl.addEventListener('click', async (e) => {
    const btn = e.target.closest('[data-act]');
    if (!btn) return;
    const c = visible().slice(0, MAX_ROWS)[Number(btn.closest('.trow').dataset.i)];
    if (!c || c.id == null) return;
    if (btn.dataset.act === 'keep') {
      const arming = !c.auto_reactivate;
      if (arming && !confirm('Keep “' + (c.name || c.of_promo_id) + '” running?\n\nEach time it finishes, the promo_reactivate automation re-creates it on OnlyFans (a REAL new promo, ' + (c.discount_percent || '?') + '% off).'
        + (rule ? (rule.is_enabled ? '' : '\n\nThat automation exists but is OFF — nothing happens until you enable it.')
                : '\n\nThat automation does not exist yet — create it in the card below the table, or this tick does nothing.'))) return;
      try {
        await Fastt.patch('/admin/promotions/' + c.id, { auto_reactivate: arming });
        Fastt.saved(arming ? 'Will keep running' : 'Auto-reactivate off');
        await refresh();
      } catch (err) { Fastt.oops(err); }
      return;
    }
    if (btn.dataset.act === 'del') {
      if (!confirm('Delete campaign "' + (c.name || c.of_promo_id) + '"? If it is live on OnlyFans it is ended there too.')) return;
      try { await Fastt.del('/admin/promotions/' + c.id); Fastt.saved('Deleted'); await refresh(); }
      catch (err) { Fastt.oops(err); }
    }
  });

  try {
    await Promise.all([refresh(), loadRule(), loadLists()]);
  } catch (e) {
    rowsEl.innerHTML = '<div class="empty" style="flex:none;padding:70px 0"><div style="color:#f0715f">Could not load campaigns — ' + esc((e && e.message) || 'request failed') + '</div><button class="btn-sm" id="pm-retry" style="margin-top:6px">Retry</button></div>';
    $('#pm-totals').innerHTML = '';
    const rb = $('#pm-retry');
    if (rb) rb.addEventListener('click', () => location.reload());
  }
  paintReact();
});
