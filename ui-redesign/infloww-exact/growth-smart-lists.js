/* ── live wiring: /admin/smart-lists (list/create/patch/delete + preview + resolve) ── */

/* Parse-time, before any fetch can fail: drop the four mockup list rows (invented
   names, criteria and fan counts, with pencil/trash icons that look clickable but
   carry no data-act). A failed GET must not leave them standing as real lists. */
(function () {
  var el = document.getElementById('sl-rows');
  if (el) el.innerHTML = '<div class="trow"><div class="td" style="flex:1;color:#9a9a9a">Loading…</div></div>';
})();

Fastt.ready(async () => {
  const $ = Fastt.$, esc = Fastt.esc;

  const rowsEl = $('#sl-rows');
  const acct = Fastt.account();
  if (!acct) {
    rowsEl.innerHTML = '<div class="empty"><div>No creator selected — click the creator name (top-left) to pick one.</div></div>';
    $('#sl-creators').innerHTML = '<div class="cempty">Sign in (or pick a creator) to load the roster.</div>';
    return;
  }
  Fastt.liveBadge($('#sl-head'));

  /* ── LEFT PANEL: the real creator roster ─────────────────────────────
     Was two baked mockup cards ("Aria" 4/2, "Mia" 99+/35) that switched
     nothing. Rebuilt from Fastt.accounts() (the full roster) with the same
     per-model rows that name it carrying the numbers, so every figure on a
     card is this creator's own 30-day total and a click re-scopes the page. */
  const CAV = [
    'linear-gradient(150deg,#d98aa2,#6f57b8)', 'linear-gradient(150deg,#4a5a3a,#2e3a52)',
    'linear-gradient(150deg,#5b8def,#2b3f8f)', 'linear-gradient(150deg,#67d1ae,#2f7d68)',
    'linear-gradient(150deg,#e5a35b,#8f5a2b)', 'linear-gradient(150deg,#a78bfa,#5b3fa8)',
  ];
  const creatorsEl = $('#sl-creators');
  const roster = Fastt.accounts().slice();
  const stats = {};             // account_id -> per-model row
  const listCount = {};         // account_id -> smart-list count (all-creators mode)
  let allMode = false;
  let creatorQuery = '';

  try {
    const end = new Date(), start = new Date(Date.now() - 30 * 864e5);
    const iso = (d) => d.toISOString().slice(0, 10);
    const pm = await Fastt.get('/admin/stats/per-model',
      { start: iso(start), end: iso(end) }, { noAccount: true });
    for (const r of (pm.per_model || [])) stats[String(r.account_id)] = r;
  } catch (e) { console.error(e); }

  function creatorCard(a, i) {
    const st = stats[String(a.id)] || null;
    const on = String(a.id) === String(acct);
    const rev = st ? Fastt.fmtCents(st.total_revenue_cents || 0) : null;
    const msgs = st ? Fastt.fmtInt(st.messages_sent || 0) : null;
    const subs = st ? (st.active_subs_count || 0) : 0;
    const nLists = listCount[String(a.id)];
    return `<div class="ccard${on && !allMode ? ' sel' : ''}" data-id="${esc(a.id)}" title="Switch this page to ${esc(a.nickname || a.id)}">
        <div class="cav" style="background:${CAV[i % CAV.length]}"></div>
        <div class="cinfo">
          <div class="cname">${esc(a.nickname || a.id)}</div>
          <div style="display:flex;gap:6px;flex-wrap:wrap">
            <span class="cbadge ${st && st.total_revenue_cents ? 'green' : 'grey'}" title="Revenue, last 30 days">${rev == null ? 'no stats' : rev}</span>
            <span class="cbadge ${msgs && msgs !== '0' ? 'amber' : 'grey'}" title="Messages sent, last 30 days">${msgs == null ? '—' : msgs} msg</span>
            ${subs ? `<span class="cbadge blue" title="Active subscribers">${Fastt.fmtInt(subs)} subs</span>` : ''}
            ${nLists == null ? '' : `<span class="cbadge ${nLists ? 'blue' : 'grey'}" title="Smart lists on this creator">${nLists} list${nLists === 1 ? '' : 's'}</span>`}
          </div>
        </div>
      </div>`;
  }

  function renderCreators() {
    const q = creatorQuery;
    const vis = roster.filter((a) => !q || String(a.nickname || a.id).toLowerCase().includes(q));
    $('#sl-all').classList.toggle('on', allMode);
    if (!roster.length) {
      creatorsEl.innerHTML = '<div class="cempty">The relay returned no creator roster '
        + '(<code>/admin/accounts</code> is per-principal and empty when unauthed, and the '
        + 'per-model fallback named nobody). Nothing is shown rather than a placeholder name.</div>';
      return;
    }
    if (!vis.length) { creatorsEl.innerHTML = '<div class="cempty">No creator matches “' + esc(q) + '”.</div>'; return; }
    creatorsEl.innerHTML = '<div class="clegend">' + roster.length + ' creators · revenue &amp; messages, last 30 days</div>'
      + vis.map((a) => creatorCard(a, roster.indexOf(a))).join('');
  }
  renderCreators();

  creatorsEl.addEventListener('click', (e) => {
    const card = e.target.closest('.ccard');
    if (!card || String(card.dataset.id) === String(acct)) return;
    Fastt.setAccount(card.dataset.id);   // reloads the page scoped to that creator
  });
  const cSearch = $('.cp-search input');
  if (cSearch) {
    cSearch.placeholder = 'Search creator';
    cSearch.addEventListener('input', Fastt.debounce(() => {
      creatorQuery = cSearch.value.trim().toLowerCase(); renderCreators();
    }, 120));
  }

  const PENCIL = '<svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M4 20l1-4L16 5l3 3L8 19l-4 1z"/><path d="M14 7l3 3"/></svg>';
  const TRASH  = '<svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M5 7h14M10 7V5h4v2M6 7l1 13h10l1-13" stroke-linecap="round" stroke-linejoin="round"/></svg>';

  let lists = [];
  const counts = {};   // list_id -> resolved fan count (loaded lazily)

  function humanRule(r) {
    if (r.field === 'lifetime_spend')  return 'Total spend ' + r.op + ' ' + Fastt.fmtCents(r.value);
    if (r.field === 'recent_spend')    return 'Spend (' + (r.days || 30) + 'd) ' + r.op + ' ' + Fastt.fmtCents(r.value);
    if (r.field === 'last_active_days')return 'Last active ' + r.op + ' ' + r.value + 'd';
    if (r.field === 'status')          return 'Status ' + r.op + ' ' + r.value;
    if (r.field === 'tag')             return 'Tag ' + (r.op === 'has' ? 'has' : 'not') + ' "' + r.value + '"';
    return r.field || '?';
  }
  function criteria(rules) {
    const rr = (rules && rules.rules) || [];
    if (!rr.length) return 'Everyone (no rules)';
    const glue = (rules.match === 'any') ? ' OR ' : ' · ';
    return rr.map(humanRule).join(glue);
  }
  // "Incl. expired": a list pinned to status == active excludes expired subs.
  function inclExpired(rules) {
    const rr = (rules && rules.rules) || [];
    const excl = rr.some(r => r.field === 'status' && r.op === '==' && String(r.value).toLowerCase() === 'active');
    return excl ? '<span class="pill-ex">Excludes</span>' : '<span class="pill-in">Includes</span>';
  }

  const nameOf = (id) => {
    const a = roster.find((r) => String(r.id) === String(id));
    return (a && (a.nickname || a.id)) || String(id);
  };

  // Header summary: total lists + total reachable fans (a live roll-up the real
  // app never shows). Reachable = sum of the resolved counts as they stream in.
  function updateHeaderSummary() {
    const el = $('#sl-summary'); if (!el) return;
    const n = lists.length;
    if (!n) { el.textContent = ''; return; }
    const nums = lists.map(l => counts[l.id]).filter(c => typeof c === 'number');
    const total = nums.reduce((a, b) => a + b, 0);
    const scope = allMode ? (n + ' list' + (n === 1 ? '' : 's') + ' across ' + roster.length + ' creators')
                          : (n + ' list' + (n === 1 ? '' : 's'));
    el.textContent = scope + (nums.length
      ? ' · ' + Fastt.fmtInt(total) + ' fans reachable' + (nums.length === n ? '' : ' so far…')
      : '');
  }

  function render() {
    updateHeaderSummary();
    const q = ($('#sl-search').value || '').toLowerCase();
    const vis = lists.filter(l => !q || (l.name || '').toLowerCase().includes(q));
    // Column widths tighten in all-creators mode so the extra Creator column
    // doesn't squeeze Criteria into a one-word-per-line column.
    const W = allMode ? { name: 150, incl: 130, fans: 80, act: 110 }
                      : { name: 200, incl: 180, fans: 110, act: 150 };
    $('#sl-thead').innerHTML =
      (allMode ? '<div class="th" style="width:150px">Creator</div>' : '')
      + '<div class="th" style="width:' + W.name + 'px">List name</div>'
      + '<div class="th" style="flex:1;min-width:0">Criteria</div>'
      + '<div class="th" style="width:' + W.incl + 'px">Incl. expired</div>'
      + '<div class="th" style="width:' + W.fans + 'px">Fans</div>'
      + '<div class="th" style="width:' + W.act + 'px">Actions</div>';
    if (!vis.length) {
      rowsEl.innerHTML = '<div class="empty"><div>' + (lists.length
        ? 'No list matches the search'
        : (allMode ? 'No smart lists on any of the ' + roster.length + ' creators yet'
                   : 'No smart lists on ' + esc(nameOf(acct)) + ' yet — create one')) + '</div></div>';
      return;
    }
    rowsEl.innerHTML = vis.map(l => `
      <div class="trow" data-id="${l.id}">
        ${allMode ? `<div class="td" style="width:150px;color:#cfcfcf;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(nameOf(l.account_id))}</div>` : ''}
        <div class="td" style="width:${W.name}px">${esc(l.name)}</div>
        <div class="td" style="flex:1;min-width:0">${esc(criteria(l.rules))}</div>
        <div class="td" style="width:${W.incl}px">${inclExpired(l.rules)}</div>
        <div class="td" style="width:${W.fans}px">${counts[l.id] == null ? '<span style="color:#6f6f6f">counting…</span>'
          : counts[l.id] === 'ERR' ? '<span style="color:#e2a94e" title="the resolve call failed — no count was loaded">n/a</span>'
          : Fastt.fmtInt(counts[l.id])}</div>
        <div class="td" style="width:${W.act}px;display:flex;gap:8px">
          <span class="iconbtn2" data-act="edit" title="Edit list">${PENCIL}</span>
          <span class="iconbtn2" data-act="del" title="Delete list">${TRASH}</span>
        </div>
      </div>`).join('');
  }

  async function refresh() {
    if (allMode) {
      // One GET per creator — /admin/smart-lists is account-scoped by query param,
      // so a roster-wide view is genuinely N calls (they are local + ~5ms each).
      const out = [];
      for (const a of roster) {
        try {
          const got = (await Fastt.get('/admin/smart-lists', { account_id: a.id }, { noAccount: true })).lists || [];
          listCount[String(a.id)] = got.length;
          out.push(...got);
        } catch (e) { console.error(e); listCount[String(a.id)] = 0; }
      }
      lists = out;
      renderCreators();
    } else {
      lists = (await Fastt.get('/admin/smart-lists')).lists || [];
    }
    render();
    for (const l of lists) {
      if (counts[l.id] != null) continue;
      try { counts[l.id] = (await Fastt.get('/admin/smart-lists/' + l.id + '/resolve', null, { noAccount: true })).count; }
      catch (e) { console.error(e); counts[l.id] = 'ERR'; }
    }
    render();
  }

  // "All creators" is a REAL scope switch, not a decoration: it folds every
  // creator's segments into one table (with a Creator column) and stamps each
  // roster card with its own list count.
  $('#sl-all').addEventListener('click', async () => {
    allMode = !allMode;
    $('#sl-create').style.display = allMode ? 'none' : '';   // create is per-creator
    rowsEl.innerHTML = '<div class="trow"><div class="td" style="flex:1;color:#9a9a9a">Loading…</div></div>';
    renderCreators();
    try { await refresh(); } catch (e) { Fastt.oops(e); }
  });
  const refreshBtn = $('.cp-refresh');
  if (refreshBtn) {
    refreshBtn.title = 'Reload lists from the relay';
    refreshBtn.addEventListener('click', async () => {
      for (const k of Object.keys(counts)) delete counts[k];
      try { await refresh(); Fastt.saved('Reloaded'); } catch (e) { Fastt.oops(e); }
    });
  }

  /* ── Structured segment builder ──────────────────────────────────────
     Grounds exactly on smart_lists_api._validate_rules: five fields, each with
     its legal operator set + value coercion. Money is shown/typed in DOLLARS
     and sent in CENTS; recent_spend carries a days window. Friendlier than the
     real app: quick-start presets, a plain-English summary, and a LIVE count
     that auto-previews on every edit (no "Preview" button to remember). */
  const FIELDS = [
    { field: 'lifetime_spend',   label: 'Lifetime spend',    kind: 'money' },
    { field: 'recent_spend',     label: 'Recent spend',      kind: 'money', window: true },
    { field: 'last_active_days', label: 'Days since active', kind: 'days' },
    { field: 'status',           label: 'Subscription',      kind: 'status' },
    { field: 'tag',              label: 'Tag',               kind: 'tag' },
  ];
  const OPS = {
    money:  ['>=', '<=', '>', '<', '==', '!='],
    days:   ['<=', '>=', '>', '<', '==', '!='],
    status: ['==', '!='],
    tag:    ['has', 'not_has'],
  };
  const OP_LABEL = { has: 'has', not_has: 'does not have' };
  const kindOf = (f) => (FIELDS.find(x => x.field === f) || FIELDS[0]).kind;

  // Quick-start presets — each a valid rules object, grounded in the real fields.
  const PRESETS = [
    { label: '💰 Whales ($100+)',    rules: [{ field: 'lifetime_spend', op: '>=', value: 10000 }] },
    { label: '💵 Spenders ($40+)',   rules: [{ field: 'lifetime_spend', op: '>=', value: 4000 }] },
    { label: '🔥 Active this week',  rules: [{ field: 'last_active_days', op: '<=', value: 7 }] },
    { label: '😴 Lapsed (30d+)',     rules: [{ field: 'last_active_days', op: '>=', value: 30 }] },
    { label: '✅ Active subs',       rules: [{ field: 'status', op: '==', value: 'active' }], match: 'all' },
    { label: '🌐 Everyone',          rules: [] },
  ];

  function toUI(r) {
    const k = kindOf(r.field);
    const value = k === 'money' ? String((Number(r.value) || 0) / 100) : String(r.value == null ? '' : r.value);
    return { field: r.field, op: r.op, value, days: String(r.days || 30) };
  }
  function fromUI(r) {
    const k = kindOf(r.field);
    if (k === 'money') {
      const out = { field: r.field, op: r.op, value: Math.round((Number(r.value) || 0) * 100) };
      if (r.field === 'recent_spend') out.days = Math.max(1, Math.round(Number(r.days) || 30));
      return out;
    }
    if (k === 'days') return { field: r.field, op: r.op, value: Number(r.value) || 0 };
    return { field: r.field, op: r.op, value: r.value };
  }
  function summarize(rules) {
    if (!rules.rules.length) return 'Everyone — no filters, every fan on this creator.';
    const glue = rules.match === 'any' ? ' OR ' : ' AND ';
    return rules.rules.map(humanRule).join(glue);
  }

  function openModal(list) {
    const isEdit = !!list;
    const acctId = isEdit ? list.account_id : acct;
    let match = (isEdit && list.rules && list.rules.match === 'any') ? 'any' : 'all';
    let rows = (isEdit && list.rules && (list.rules.rules || []).length)
      ? list.rules.rules.map(toUI)
      : [{ field: 'lifetime_spend', op: '>=', value: '100', days: '30' }];

    const back = document.createElement('div');
    back.className = 'ft-modal-back';
    back.innerHTML = `
      <div class="ft-modal slb" style="width:540px;max-height:90vh;overflow:auto">
        <h3>${isEdit ? 'Edit smart list' : 'Create smart list'}</h3>
        <input type="text" id="slm-name" placeholder="Segment name — e.g. Active whales" value="${isEdit ? esc(list.name) : ''}">
        ${isEdit ? '' : '<span class="slb-lbl">Quick start</span><div class="slb-presets" id="slm-presets"></div>'}
        <span class="slb-lbl">Match</span>
        <div class="slb-match" id="slm-match">
          <button type="button" class="slb-mbtn" data-m="all">ALL rules · AND</button>
          <button type="button" class="slb-mbtn" data-m="any">ANY rule · OR</button>
        </div>
        <span class="slb-lbl">Rules</span>
        <div id="slm-rules"></div>
        <button type="button" class="slb-add" id="slm-add">+ Add rule</button>
        <div class="slb-sum">
          <div class="slb-desc" id="slm-desc"></div>
          <div class="slb-cnt" id="slm-cnt"></div>
          <div class="slb-sample" id="slm-sample"></div>
        </div>
        <div class="ft-err" id="slm-err"></div>
        <div class="slb-foot">
          <button type="button" class="slb-cancel" id="slm-cancel">Cancel</button>
          <button type="button" id="slm-save">${isEdit ? 'Save changes' : 'Create list'}</button>
        </div>
      </div>`;
    document.body.appendChild(back);

    const close = () => back.remove();
    back.addEventListener('click', (e) => { if (e.target === back) close(); });
    back.addEventListener('keydown', (e) => { if (e.key === 'Escape') close(); });
    const errEl = $('#slm-err', back);
    const showErr = (txt) => { errEl.style.display = 'block'; errEl.textContent = txt; };
    const clearErr = () => { errEl.style.display = 'none'; errEl.textContent = ''; };
    const detail = (e) => (e && e.body && e.body.detail) ? String(e.body.detail) : String((e && e.message) || e);

    // preset chips
    if (!isEdit) {
      $('#slm-presets', back).innerHTML = PRESETS.map((p, i) =>
        `<button type="button" class="slb-preset" data-p="${i}">${esc(p.label)}</button>`).join('');
      $('#slm-presets', back).addEventListener('click', (e) => {
        const b = e.target.closest('.slb-preset'); if (!b) return;
        const p = PRESETS[+b.dataset.p];
        match = p.match || 'all';
        rows = p.rules.length ? p.rules.map(toUI) : [];
        paintMatch(); renderRules(); onChange();
      });
    }

    function paintMatch() {
      $('#slm-match', back).querySelectorAll('.slb-mbtn')
        .forEach(b => b.classList.toggle('on', b.dataset.m === match));
    }
    $('#slm-match', back).addEventListener('click', (e) => {
      const b = e.target.closest('.slb-mbtn'); if (!b) return;
      match = b.dataset.m; paintMatch(); onChange();
    });

    function rowHTML(r, i) {
      const k = kindOf(r.field);
      const fieldSel = '<select class="fld" data-i="' + i + '" data-k="field">'
        + FIELDS.map(f => '<option value="' + f.field + '"' + (f.field === r.field ? ' selected' : '') + '>' + f.label + '</option>').join('')
        + '</select>';
      const opSel = '<select data-i="' + i + '" data-k="op">'
        + OPS[k].map(o => '<option value="' + o + '"' + (o === r.op ? ' selected' : '') + '>' + (OP_LABEL[o] || o) + '</option>').join('')
        + '</select>';
      let valPart;
      if (k === 'status') {
        valPart = '<select data-i="' + i + '" data-k="value">'
          + ['active', 'expired'].map(s => '<option value="' + s + '"' + (s === r.value ? ' selected' : '') + '>' + s + '</option>').join('')
          + '</select>';
      } else if (k === 'money') {
        valPart = '<span class="slb-money"><input class="slb-val" type="number" min="0" step="1" data-i="' + i + '" data-k="value" value="' + esc(r.value) + '" placeholder="0"></span>'
          + (r.field === 'recent_spend'
            ? '<span class="unit">in last</span><input class="slb-days" type="number" min="1" data-i="' + i + '" data-k="days" value="' + esc(r.days) + '"><span class="unit">days</span>'
            : '');
      } else if (k === 'days') {
        valPart = '<input class="slb-val" type="number" min="0" data-i="' + i + '" data-k="value" value="' + esc(r.value) + '" placeholder="0"><span class="unit">days</span>';
      } else { // tag
        valPart = '<input class="slb-val" style="width:120px" data-i="' + i + '" data-k="value" value="' + esc(r.value) + '" placeholder="tag name">';
      }
      const rm = rows.length > 1 ? '<span class="slb-x" data-i="' + i + '" data-k="remove" title="Remove rule">✕</span>' : '<span style="width:22px"></span>';
      return '<div class="slb-rule">' + fieldSel + opSel + valPart + rm + '</div>';
    }
    function renderRules() {
      $('#slm-rules', back).innerHTML = rows.map(rowHTML).join('');
    }
    $('#slm-rules', back).addEventListener('input', (e) => {
      const t = e.target; if (t.dataset.k == null) return;
      rows[+t.dataset.i][t.dataset.k] = t.value; onChange();
    });
    $('#slm-rules', back).addEventListener('change', (e) => {
      const t = e.target; if (t.dataset.k !== 'field') return;
      const i = +t.dataset.i, k = kindOf(t.value);
      rows[i].field = t.value;
      rows[i].op = OPS[k][0];                       // reset op to a legal one
      if (k === 'status' && !['active', 'expired'].includes(rows[i].value)) rows[i].value = 'active';
      renderRules(); onChange();
    });
    $('#slm-rules', back).addEventListener('click', (e) => {
      const x = e.target.closest('[data-k="remove"]'); if (!x) return;
      rows.splice(+x.dataset.i, 1); renderRules(); onChange();
    });
    $('#slm-add', back).addEventListener('click', () => {
      rows.push({ field: 'lifetime_spend', op: '>=', value: '', days: '30' }); renderRules(); onChange();
    });

    // Live preview — auto-runs (debounced) whenever the rules change.
    let seq = 0;
    const runPreview = Fastt.debounce(async () => {
      const my = ++seq;
      const rules = { match, rules: rows.map(fromUI) };
      try {
        const out = await Fastt.post('/admin/smart-lists/preview', { account_id: acctId, rules });
        if (my !== seq) return; // a newer edit already superseded this call
        $('#slm-cnt', back).innerHTML = '<b>' + Fastt.fmtInt(out.count) + '</b> <span class="t">/ '
          + Fastt.fmtInt(out.total_fans) + ' fans match</span>';
        const s = (out.sample || []).slice(0, 8);
        $('#slm-sample', back).innerHTML = s.length
          ? s.map(f => '<span class="slb-chip">' + esc(f.name) + ' <span class="s">'
              + Fastt.fmtCents(f.lifetime_spend_cents || 0) + '</span></span>').join('')
            + (out.count > s.length ? '<span style="color:#6a6a6a">+' + Fastt.fmtInt(out.count - s.length) + ' more</span>' : '')
          : '';
      } catch (e) {
        if (my !== seq) return;
        $('#slm-cnt', back).innerHTML = '<span class="warn">' + esc(detail(e)) + '</span>';
        $('#slm-sample', back).innerHTML = '';
      }
    }, 350);

    function onChange() {
      clearErr();
      $('#slm-desc', back).textContent = summarize({ match, rules: rows.map(fromUI) });
      $('#slm-cnt', back).innerHTML = '<span class="t">counting…</span>';
      runPreview();
    }

    $('#slm-cancel', back).addEventListener('click', close);
    $('#slm-save', back).addEventListener('click', async () => {
      const name = $('#slm-name', back).value.trim();
      if (!name) { showErr('Give the segment a name first.'); $('#slm-name', back).focus(); return; }
      const rules = { match, rules: rows.map(fromUI) };
      try {
        if (isEdit) await Fastt.patch('/admin/smart-lists/' + list.id, { name, rules });
        else await Fastt.post('/admin/smart-lists', { account_id: acctId, name, rules });
        close();
        Fastt.saved(isEdit ? 'Saved' : 'List created');
        if (isEdit) delete counts[list.id];
        await refresh();
      } catch (e) { showErr(detail(e)); }
    });

    paintMatch(); renderRules(); onChange();
    setTimeout(() => { const n = $('#slm-name', back); if (n) n.focus(); }, 0);
  }

  $('#sl-create').addEventListener('click', () => openModal(null));
  $('#sl-search').addEventListener('input', Fastt.debounce(render, 150));
  rowsEl.addEventListener('click', async (e) => {
    const btn = e.target.closest('[data-act]');
    if (!btn) return;
    const id = Number(btn.closest('.trow').dataset.id);
    const list = lists.find(l => l.id === id);
    if (!list) return;
    if (btn.dataset.act === 'edit') { openModal(list); return; }
    if (btn.dataset.act === 'del') {
      if (!confirm('Delete smart list "' + list.name + '"? Audiences built on it lose the segment.')) return;
      try {
        await Fastt.del('/admin/smart-lists/' + id);
        Fastt.saved('Deleted');
        delete counts[id];
        await refresh();
      } catch (err) { Fastt.oops(err); }
    }
  });

  try {
    await refresh();
  } catch (e) {
    rowsEl.innerHTML = '<div class="empty"><div>Could not load smart lists from the relay — '
      + 'nothing is shown because nothing was loaded.</div></div>';
    Fastt.oops(e);
  }
});
