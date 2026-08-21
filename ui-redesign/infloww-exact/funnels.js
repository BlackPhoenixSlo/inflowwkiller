Fastt.ready(async function () {
  'use strict';
  var $ = Fastt.$, $$ = Fastt.$$, esc = Fastt.esc;
  var acct = Fastt.account();

  var state = {
    funnels: [],       // summaries from /admin/funnels
    details: {},       // id -> full funnel
    stats: {},         // id -> {steps:[...]}
    media: {},         // id -> per-account media map (current account)
    sel: null,         // selected funnel id
    walker: null,      // reply_mass_funnel rule (current account)
    lists: [],         // GET /api/of/v2/lists → the real include/exclude universe
    listsErr: null,
    roster: [],        // Fastt.accounts() — every creator the matrix must cover
    xmedia: {},        // "fid:acct" -> media map for the cross-model matrix
    xstate: 'idle',    // idle | loading | done | error
  };

  function setSt(el, txt, ok) { el.innerHTML = '<i></i>' + esc(txt); el.classList.toggle('ok', !!ok); }
  function isPpv(st) { return st && st.type === 'paid_ppv'; }
  function stepMsg(st) {
    var msgs = (st.messages || []).filter(function (m) { return String(m).trim(); });
    if (msgs.length) return msgs;
    if (st.prompt || st.generate) return null; // AI-generated
    return [];
  }
  function statsBy(fid) {
    var out = {};
    ((state.stats[fid] || {}).steps || []).forEach(function (s) { out[s.step] = s; });
    return out;
  }
  function totals(fid) {
    var sent1 = null, bought = 0, rev = 0;
    ((state.stats[fid] || {}).steps || []).forEach(function (s, i) {
      if (i === 0) sent1 = s.sent;
      bought += s.bought; rev += s.revenue_cents;
    });
    return { entered: sent1, bought: bought, revenue_cents: rev };
  }

  // ---- loads ----
  async function loadAll() {
    var list = await Fastt.get('/admin/funnels');
    state.funnels = list.funnels || [];
    await Promise.all(state.funnels.map(async function (f) {
      state.details[f.id] = await Fastt.get('/admin/funnels/' + f.id);
    }));
    if (state.sel === null && state.funnels.length) state.sel = state.funnels[0].id;
  }
  async function loadSel() {
    var id = state.sel;
    if (id == null) return;
    var jobs = [
      Fastt.get('/admin/funnels/' + id).then(function (d) { state.details[id] = d; }),
      Fastt.get('/admin/funnel-stats', { funnel_id: id }).then(function (s) { state.stats[id] = s; }),
    ];
    if (acct) jobs.push(
      Fastt.get('/admin/funnels/' + id + '/media/' + acct).then(function (m) { state.media[id] = m; })
    );
    await Promise.all(jobs);
  }
  async function loadLists() {
    try {
      var L = await Fastt.get('/api/of/v2/lists', { limit: 50 });
      state.lists = (L && L.list) || [];
    } catch (e) { state.listsErr = e; state.lists = []; }
  }

  // ---- renders ----
  function renderStatus() {
    var r = state.walker;
    var on = !!(r && r.is_enabled);
    var stats = (r && r.last_run && r.last_run.stats) || {};
    var wk = $('#fn-st-walker');
    var label = r
      ? (on ? ('Walker running · every ' + Math.round((r.every_seconds || 120) / 60) + ' min')
            : 'Walker paused — funnels won’t advance')
      : 'No walker rule yet';
    wk.innerHTML = '<i></i>' + esc(label) +
      (acct ? '<button class="wk-btn' + (on ? '' : ' go') + '" id="fn-walker-toggle" title="' +
        (on ? 'Pause the reply-walker' : 'Enable the reply-walker (needed for funnels to advance past the opener)') +
        '">' + (on ? 'Pause' : 'Enable reply-walking') + '</button>' : '');
    wk.classList.toggle('ok', on);
    setSt($('#fn-st-fans'), stats.runs != null
      ? (stats.runs + ' active runs walked last pass · ' + (stats.advanced || 0) + ' fans advanced')
      : 'no walk data yet', false);
    var t = state.sel != null ? totals(state.sel) : { bought: 0, revenue_cents: 0 };
    setSt($('#fn-st-unlocks'), t.bought + ' unlocks · ' + Fastt.fmtCents(t.revenue_cents) + ' all-time (this funnel)', false);
    setSt($('#fn-st-last'), 'Last pass ' + Fastt.fmtAgo(r && r.last_run && r.last_run.completed_at), false);
  }
  function renderPicker() {
    var pick = $('#fn-pick');
    var html = state.funnels.map(function (f) {
      var d = state.details[f.id] || {};
      var prices = (d.steps || []).filter(isPpv).map(function (s) { return s.price || 0; });
      var range = prices.length
        ? ('$0 → $' + Math.max.apply(null, prices))
        : 'free';
      return '<div class="fpk' + (f.id === state.sel ? ' sel' : '') + '" data-fid="' + f.id + '" title="' + esc(f.description || '') + '">' +
        '<div class="r1"><span class="nm">' + esc(f.name) + '</span></div>' +
        '<div class="meta">' + f.step_count + ' step' + (f.step_count === 1 ? '' : 's') + ' · ' + esc(range) + '</div></div>';
    }).join('');
    html += '<div class="fpk new" id="fn-new2">' +
      '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><path d="M12 5v14M5 12h14" stroke-linecap="round"/></svg>New funnel</div>';
    pick.innerHTML = html;
  }
  var CLOCK_SVG = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="12" cy="12" r="8.5"/><path d="M12 8v4l3 2" stroke-linecap="round" stroke-linejoin="round"/></svg>';
  var LOCK_SVG = '<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="5" y="11" width="14" height="9" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3" stroke-linecap="round"/></svg>';
  /** Up to three real vault thumbnails for a mapped step. A relay thumb 404s
   *  for an id the vault-AI cache never saw, so a failed image collapses to a
   *  plain tile rather than a broken-image glyph. */
  function mtiles(ids) {
    var list = (ids || []).slice(0, 3);
    if (!list.length || !acct) return '<span class="mtile"></span>';
    return list.map(function (id) {
      return '<span class="mtile"><img src="/admin/vault-ai/thumb?account_id=' +
        encodeURIComponent(acct) + '&media_id=' + encodeURIComponent(id) +
        '" alt="" loading="lazy" title="vault #' + esc(id) +
        '" onerror="this.remove()"></span>';
    }).join('');
  }
  function twait(st) {
    var iv = (st && st.check_intervals_min) || [2, 4, 10];
    return '<div class="twait"><span>' + CLOCK_SVG + 'Checks for the reply at <b>' +
      esc(iv.join(', ')) + ' min</b></span></div>';
  }
  function renderTimeline() {
    var tl = $('#fn-tl');
    var d = state.details[state.sel];
    if (!d) {
      tl.innerHTML = '<div class="titem"><div class="tdot trig"></div><div class="tcard trigc">' +
        '<div class="tc-top"><span class="tc-title">No funnels yet — create one</span></div></div></div>';
      return;
    }
    var sb = statsBy(state.sel);
    var t = totals(state.sel);
    var media = (state.media[state.sel] || {});
    var stepsMedia = media.steps_media || {};

    var html = '';
    // trigger card
    html += '<div class="titem"><div class="tdot trig">' +
      '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M3 11l16-6-5 16-3-6.5L3 11z" stroke-linejoin="round"/></svg>' +
      '</div><div class="tcard trigc"><div class="tc-top">' +
      '<span class="tc-title">Fan replies to the mass tease</span>' +
      '<span class="chip">trigger · any reply</span>' +
      '<span class="tc-status">' + (t.entered != null ? t.entered + ' entered' : 'no sends yet') + '</span>' +
      '<span class="tc-tools"><button class="tc-tool" data-edit-opener title="Edit name, description &amp; opener">' + PENCIL + '</button></span>' +
      '</div><div class="tc-msg">"' + esc(d.opening_message) + '"' +
      (media.opening_media_ids && media.opening_media_ids.length
        ? '<span class="by">Opener media: ' + media.opening_media_ids.length + ' vault item(s) mapped for this model</span>'
        : '<span class="by">No opener media mapped for this model — opener text only</span>') +
      '</div><div class="tc-sub">Only fans who write back enter the funnel. Quiet fans are never paywalled — they just never hear from it again.</div>' +
      '</div></div>';

    var lastIdx = (d.steps || []).length - 1;
    (d.steps || []).forEach(function (st, idx) {
      var s = sb[st.step] || { sent: 0, bought: 0, conv_pct: 0, revenue_cents: 0 };
      var ppv = isPpv(st);
      var msgs = stepMsg(st);
      html += twait(ppv ? null : st);
      html += '<div class="titem"><div class="tdot' + (ppv ? ' paid' : '') + '">' + st.step + '</div>' +
        '<div class="tcard"><div class="tc-top">' +
        '<span class="tc-title">' + (ppv ? 'PPV unlock — step ' + st.step : 'Reply step ' + st.step) + '</span>' +
        '<span class="chip">on any reply</span>' +
        (ppv ? '<span class="chip price">$' + esc(st.price != null ? st.price : '?') + '</span>'
             : '<span class="chip free">$0 · free</span>') +
        (st.locked_text ? '<span class="chip lockc">' + LOCK_SVG + 'text locked too</span>' : '') +
        (st.next ? '<span class="chip">branches</span>' : '') +
        '<span class="tc-status">' + (s.sent ? ('Sent ' + s.sent + (ppv ? ' · ' + s.bought + ' unlocked (' + s.conv_pct + '%)' : '')) : 'no sends yet') + '</span>' +
        '<span class="tc-tools">' +
          '<button class="tc-tool" data-step-edit="' + idx + '" title="Edit step">' + PENCIL + '</button>' +
          '<button class="tc-tool" data-step-up="' + idx + '" title="Move earlier"' + (idx === 0 ? ' disabled' : '') + '>' + UP_SVG + '</button>' +
          '<button class="tc-tool" data-step-down="' + idx + '" title="Move later"' + (idx === lastIdx ? ' disabled' : '') + '>' + DOWN_SVG + '</button>' +
          '<button class="tc-tool del" data-step-del="' + idx + '" title="Remove step">' + TRASH + '</button>' +
        '</span>' +
        '</div>' +
        '<div class="tc-msg">' + (msgs === null
          ? '<i>AI-generated from a prompt at send time</i>'
          : '"' + esc(msgs[0] || '') + '"' + (msgs.length > 1 ? '<span class="by">+ ' + (msgs.length - 1) + ' more line(s)</span>' : '')) +
        '</div>';
      if (ppv) {
        var sm = stepsMedia[String(st.step)] || {};
        var n = (sm.media_files || []).length;
        var pv = (sm.previews || []).length;
        html += '<div class="tc-meta">' + (n
          ? '<span class="mchip"><span class="mtiles">' + mtiles(sm.media_files) + '</span>' +
            n + ' vault item' + (n === 1 ? '' : 's') + ' mapped (this model)</span>' +
            (pv ? '<span class="chip free">' + pv + ' free preview' + (pv === 1 ? '' : 's') + '</span>' : '')
          : (Array.isArray(st.media_files) && st.media_files.length
            ? '<span class="mchip"><span class="mtiles">' + mtiles(st.media_files) + '</span>' + st.media_files.length + ' legacy item(s) on the step itself</span>'
            : '<span class="mchip">no media mapped for this model — step is skipped</span>')) +
          '</div>';
      }
      html += '</div></div>';
    });

    // end node
    html += '<div class="titem"><div class="tdot end">' +
      '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><path d="M5 12.5l4.5 4.5L19 7.5" stroke-linecap="round" stroke-linejoin="round"/></svg>' +
      '</div><div class="tcard endc"><div class="tc-top">' +
      '<span class="tc-title">A buy ends the climb</span>' +
      '<span class="tc-status">' + t.bought + ' bought · ' + Fastt.fmtCents(t.revenue_cents) + '</span>' +
      '</div><div class="tc-sub">Any unlocked PPV — this funnel’s or another automation’s — halts the walk right there and hands the thread to a live chatter to keep the momentum.</div>' +
      '</div></div>';

    html += '<button class="addstep" id="fn-addstep">' +
      '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><path d="M12 5v14M5 12h14" stroke-linecap="round"/></svg>Add step</button>';
    tl.innerHTML = html;
  }
  // ============ LAUNCH PANEL ============
  // POST /api/of/v2/messages/queue (_MassScheduleBody) is the same call the
  // real composer makes; funnel_id is the documented field that walks every
  // replier through this funnel. Nothing here fires without a click + confirm.
  var LIST_ORDER = ['fans', 'following', 'rebill_on', 'rebill_off', 'friends',
    'tagged', 'muted', 'recent', 'close_friends'];
  function listKey(l) {
    // OF accepts built-in names ("fans") and numeric custom-list ids alike;
    // send the built-in name when there is one so the body reads like the app's.
    return (l.type && l.type !== 'custom') ? String(l.type) : String(l.id);
  }
  function sortedLists() {
    return state.lists.slice().sort(function (a, b) {
      var ia = LIST_ORDER.indexOf(a.type), ib = LIST_ORDER.indexOf(b.type);
      if (ia < 0) ia = 99; if (ib < 0) ib = 99;
      if (ia !== ib) return ia - ib;
      return (b.usersCount || 0) - (a.usersCount || 0);
    });
  }
  function renderLaunchAudience() {
    var inc = $('#fn-inc'), exc = $('#fn-exc');
    if (!state.lists.length) {
      var why = state.listsErr
        ? 'GET /api/of/v2/lists failed for this creator (' +
          esc(String((state.listsErr.body && state.listsErr.body.detail) || state.listsErr.message || 'error').slice(0, 70)) + ')'
        : 'OF returned no lists for this creator';
      inc.innerHTML = '<span style="font-size:12.5px;color:var(--muted2)">' + why +
        ' — pick an audience on the Mass Message page instead.</span>';
      exc.innerHTML = '';
      return;
    }
    var mk = function (l, side) {
      var k = listKey(l);
      return '<span class="fx-chip' + (side === 'inc' && l.type === 'fans' ? ' on' : '') +
        '" data-side="' + side + '" data-key="' + esc(k) + '" data-n="' + (l.usersCount || 0) + '" ' +
        'title="' + esc(l.type) + ' · ' + esc(k) + '">' + esc(l.name) +
        ' <b style="color:var(--muted2);font-weight:500">' + (l.usersCount || 0) + '</b></span>';
    };
    var ls = sortedLists();
    inc.innerHTML = ls.map(function (l) { return mk(l, 'inc'); }).join('');
    // the two standing exclude lists this house keeps are pre-ticked
    exc.innerHTML = ls.map(function (l) {
      var h = mk(l, 'exc');
      if (l.name === 'MASSdmEXCLUDE' || l.name === 'Auto_Exclude') h = h.replace('class="fx-chip"', 'class="fx-chip on"');
      return h;
    }).join('');
    renderReach();
  }
  function pickedLists(side) {
    return Fastt.$$('#fn-' + side + ' .fx-chip.on').map(function (c) { return c.dataset.key; });
  }
  function renderReach() {
    var el = $('#fn-reach');
    if (!el) return;
    var incEls = Fastt.$$('#fn-inc .fx-chip.on'), excEls = Fastt.$$('#fn-exc .fx-chip.on');
    if (!incEls.length) { el.innerHTML = 'Estimated reach: <b style="color:#e0b05e">pick at least one list</b>'; return; }
    // OF de-duplicates across lists server-side; the sum of usersCount is an
    // upper bound, so it is labelled as one — never as a resolved audience.
    var top = incEls.reduce(function (s, c) { return s + Number(c.dataset.n || 0); }, 0);
    var off = excEls.reduce(function (s, c) { return s + Number(c.dataset.n || 0); }, 0);
    if ($('#fn-ck-online') && $('#fn-ck-online').classList.contains('on')) {
      el.innerHTML = 'Estimated reach: <b>whoever of those ' + Fastt.fmtInt(top) +
        '</b> is online at send time';
      return;
    }
    el.innerHTML = 'Estimated reach: <b>≤ ' + Fastt.fmtInt(top) + '</b>' +
      (off ? ' minus up to ' + Fastt.fmtInt(off) + ' excluded' : '') +
      ' <span style="color:var(--muted2)">(list sizes, before OF de-dupes)</span>';
  }
  function launchBody() {
    var d = state.details[state.sel];
    if (!d) return null;
    var body = {
      text: d.opening_message || '',
      funnel_id: d.id,
      price: 0,
      user_lists: pickedLists('inc'),
    };
    var ex = pickedLists('exc');
    if (ex.length) body.excluded_user_lists = ex;
    if ($('#fn-ck-online').classList.contains('on')) body.online_only = true;
    var rep = parseFloat($('#fn-l-replied').value);
    if (isFinite(rep) && rep > 0) body.exclude_replied_hours = rep;
    var uns = parseFloat($('#fn-l-unsend').value);
    if (isFinite(uns) && uns > 0) body.unsend_after_hours = uns;
    return body;
  }
  $('#fn-inc').addEventListener('click', function () { setTimeout(renderReach, 0); });
  $('#fn-exc').addEventListener('click', function () { setTimeout(renderReach, 0); });
  $('#fn-ck-online').addEventListener('click', function () { setTimeout(renderReach, 0); });
  $('#fn-send-btn').addEventListener('click', async function () {
    var d = state.details[state.sel];
    if (!d) { Fastt.toast('Pick a funnel first', 'err'); return; }
    if (!acct) { Fastt.toast('No creator selected — this page can’t name the sending account', 'err'); return; }
    var body = launchBody();
    if (!body.text.trim()) { Fastt.toast('This funnel has no opening message to send', 'err'); return; }
    if (!body.user_lists.length) { Fastt.toast('Pick at least one “Send to” list', 'err'); return; }
    var nm = (Fastt.accountRow() && Fastt.accountRow().nickname) || acct;
    if (!confirm('LAUNCH the funnel "' + d.name + '" from ' + nm + ' NOW?\n\n'
        + '“' + body.text.slice(0, 120) + '”\n\n'
        + 'to lists: ' + body.user_lists.join(', ')
        + (body.excluded_user_lists ? '\nexcluding: ' + body.excluded_user_lists.join(', ') : '')
        + (body.online_only ? '\nonline fans only' : '')
        + (body.unsend_after_hours ? '\nauto-unsend after ' + body.unsend_after_hours + ' h' : '')
        + '\n\nThis is a FREE mass message to REAL fans, and every replier starts walking the paywall ladder.')) return;
    var btn = $('#fn-send-btn'); btn.disabled = true;
    try {
      var res = await Fastt.post('/api/of/v2/messages/queue', body);
      Fastt.saved('Tease launched ✓' + (res && res.id ? ' (queue ' + res.id + ')' : ''));
      await loadSel(); renderAll();
    } catch (e) { Fastt.oops(e); }
    btn.disabled = false;
  });

  // ============ CROSS-MODEL MEDIA MATRIX ============
  // Media is per (funnel, account); the roster comes from Fastt.accounts(),
  // which falls back to /admin/stats/per-model for an unauthed caller.
  async function loadMatrix() {
    var d = state.details[state.sel];
    if (!d || !state.roster.length) { state.xstate = 'idle'; return; }
    state.xstate = 'loading';
    renderMatrix();
    var jobs = state.roster.map(async function (a) {
      var key = d.id + ':' + a.id;
      try {
        state.xmedia[key] = await Fastt.get('/admin/funnels/' + d.id + '/media/' + a.id);
      } catch (e) { state.xmedia[key] = { _err: String((e.body && e.body.detail) || e.message || 'error') }; }
    });
    await Promise.all(jobs);
    state.xstate = 'done';
    renderMatrix();
  }
  function renderMatrix() {
    var d = state.details[state.sel] || {};
    var tbl = $('#fn-mmx'), cap = $('#fn-mmx-cap');
    var ppvSteps = (d.steps || []).filter(isPpv);
    var head = '<tr><th>Model</th><th>Tease</th>' + ppvSteps.map(function (s) {
      return '<th>Step ' + s.step + ' · $' + esc(s.price != null ? s.price : '?') + '</th>';
    }).join('') + '<th></th></tr>';
    var roster = state.roster.length
      ? state.roster
      : [{ id: acct, nickname: (Fastt.accountRow() && Fastt.accountRow().nickname) || acct || '(no account)' }];
    var colspan = 2 + ppvSteps.length;
    var mappedModels = 0;
    var rows = roster.map(function (a) {
      var mine = String(a.id) === String(acct);
      var m = state.xmedia[d.id + ':' + a.id];
      var cells = '<td class="model">' + esc(a.nickname || a.id) + '</td>';
      if (!m) {
        cells += '<td class="load" colspan="' + colspan + '">' +
          (state.xstate === 'loading' ? 'reading…' : 'not read') + '</td>';
      } else if (m._err) {
        cells += '<td class="cell-warn" colspan="' + colspan + '">' + esc(m._err.slice(0, 60)) + '</td>';
      } else {
        var any = (m.opening_media_ids || []).length > 0;
        cells += '<td>' + ((m.opening_media_ids || []).length
          ? '<span class="cell-ok">✓ ' + m.opening_media_ids.length + ' item(s)</span>'
          : '<span class="cell-none">text-only opener</span>') + '</td>';
        ppvSteps.forEach(function (s) {
          var sm = (m.steps_media || {})[String(s.step)] || {};
          var n = (sm.media_files || []).length;
          if (n) any = true;
          cells += '<td>' + (n
            ? '<span class="cell-ok">✓ ' + n + ' mapped</span>'
            : '<span class="cell-warn">needs mapping</span>') + '</td>';
        });
        if (any) mappedModels++;
      }
      cells += '<td>' + (mine && d.id != null
        ? '<button class="fx-btn ghost" id="fn-media-edit" style="height:30px;padding:0 12px;font-size:12px">Map…</button>'
        : '<span style="font-size:11px;color:var(--muted2)" title="Media is written with the creator’s own scope — switch to ' +
          esc(a.nickname || a.id) + ' to map hers">switch to map</span>') + '</td>';
      return '<tr' + (mine ? ' class="me"' : '') + '>' + cells + '</tr>';
    }).join('');
    tbl.innerHTML = head + rows;
    if (cap) {
      cap.innerHTML = state.roster.length
        ? (state.xstate === 'done'
          ? '<b style="color:#cfd8ff">' + mappedModels + ' of ' + roster.length + '</b> creators have any media mapped for “' +
            esc(d.name || '—') + '”. Every other row would send that step text-only — the walker skips an unmapped paywall and logs it.'
          : 'Reading ' + roster.length + ' creators…')
        : 'Roster unavailable (/admin/accounts is empty for this caller and the per-model fallback returned nothing) — only the selected creator is shown.';
    }
  }

  function renderAdvanced() {
    var d = state.details[state.sel] || {};
    var replySteps = (d.steps || []).filter(function (s) { return !isPpv(s); });
    var iv = (replySteps[0] && replySteps[0].check_intervals_min) || [2, 4, 10];
    $('#fn-iv').value = iv.join(', ');
    // derived, not a second stored knob: the walker repeats the last interval
    $('#fn-iv2').value = iv.length ? iv[iv.length - 1] : '';
    var p = (state.walker && state.walker.payload) || {};
    $('#fn-max').value = p.max_chats != null ? p.max_chats : '';
    $('#fn-raw').textContent = d.id != null
      ? JSON.stringify({ name: d.name, description: d.description,
          opening_message: d.opening_message, steps: d.steps }, null, 2)
      : 'no funnel selected';
    // the selected creator's own map also feeds the cross-model matrix
    if (acct && d.id != null && state.media[state.sel]) {
      state.xmedia[d.id + ':' + acct] = state.media[state.sel];
    }
    renderMatrix();
    // launch panel
    var note = $('#fn-launch-note');
    if (note) {
      note.innerHTML = d.id != null
        ? '<span>Sends this funnel’s own opener — <b>“' + esc((d.opening_message || '').slice(0, 90)) +
          '”</b> — free, with <code style="color:#9db1fb">funnel_id ' + esc(d.id) +
          '</code> attached so every replier starts walking. The paywalls live in the steps above.</span>'
        : '<span>No funnel selected.</span>';
    }
    if (!state.lists.length) renderLaunchAudience(); else renderReach();
  }
  // ============ FRIENDLY EDITORS (modal-based; replace raw-JSON prompts) ============
  var X_SVG = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M6 6l12 12M18 6L6 18" stroke-linecap="round"/></svg>';
  var GEAR_SVG = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="12" cy="12" r="3"/><path d="M12 3v2M12 19v2M5.6 5.6l1.4 1.4M17 17l1.4 1.4M3 12h2M19 12h2M5.6 18.4L7 17M17 7l1.4-1.4" stroke-linecap="round"/></svg>';
  var STEP_SVG = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M4 20h5v-5H4zM10 15h5v-5h-5zM16 10h4V5h-4z"/></svg>';
  var IMG_SVG = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><rect x="4" y="5" width="16" height="14" rx="2"/><circle cx="9" cy="10" r="1.6"/><path d="M5 17l4.5-4 3 2.5L16 11l3 3.5"/></svg>';
  var PENCIL = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9"><path d="M4 20l1-4L15 6l3 3L8 19l-4 1z"/><path d="M13 8l3 3" stroke-linecap="round"/></svg>';
  var UP_SVG = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><path d="M12 19V6M6 11l6-6 6 6" stroke-linecap="round" stroke-linejoin="round"/></svg>';
  var DOWN_SVG = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><path d="M12 5v13M6 13l6 6 6-6" stroke-linecap="round" stroke-linejoin="round"/></svg>';
  var TRASH = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9"><path d="M5 7h14M10 7V5h4v2M6 7l1 12h10l1-12" stroke-linecap="round" stroke-linejoin="round"/></svg>';

  var $ov = $('#fn-ov');
  function closeModal() { $ov.classList.remove('open'); $ov.innerHTML = ''; }
  function openModal(o) {
    $ov.innerHTML =
      '<div class="fn-modal" role="dialog" aria-modal="true">' +
        '<div class="fn-mh"><span class="t">' + (o.icon || '') + esc(o.title) + '</span>' +
          '<span class="x" data-mclose aria-label="Close">' + X_SVG + '</span></div>' +
        '<div class="fn-mb">' + o.bodyHTML + '</div>' +
        '<div class="fn-mf">' + (o.footerHTML || '') + '</div>' +
      '</div>';
    $ov.classList.add('open');
    if (o.onMount) o.onMount();
  }
  $ov.addEventListener('click', function (e) {
    if (e.target === $ov || e.target.closest('[data-mclose]')) closeModal();
  });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && $ov.classList.contains('open')) closeModal();
  });
  function acctName() { return (Fastt.accountRow() && Fastt.accountRow().nickname) || acct || 'this model'; }

  // ---- funnel settings (create / edit name+desc+opener / delete) ----
  function openFunnelModal(mode) {
    var editing = mode === 'edit';
    var d = editing ? (state.details[state.sel] || {}) : {};
    if (editing && d.id == null) { Fastt.toast('Pick a funnel to edit first', 'err'); return; }
    var body =
      '<div class="fx-field"><label>Name</label><input class="fx-input" id="fm-name" placeholder="e.g. strokes"></div>' +
      '<div class="fx-field"><label>Description <span style="color:var(--muted2);font-weight:400">(optional — only you see it)</span></label><input class="fx-input" id="fm-desc" placeholder="what this funnel is for"></div>' +
      '<div class="fx-field"><label>Opening message <span style="color:var(--muted2);font-weight:400">(the free mass tease every replier walks from)</span></label><textarea class="fx-input" id="fm-open" rows="3" placeholder="The first message every fan sees…"></textarea></div>';
    var footer =
      (editing ? '<button class="fx-btn ghost" id="fm-del" style="border-color:rgba(242,41,91,.4);color:#f2a3b6">' + TRASH + 'Delete</button>' : '') +
      '<div class="sp"></div><span class="err" id="fm-msg"></span>' +
      '<button class="fx-btn ghost" data-mclose>Cancel</button>' +
      '<button class="fx-btn" id="fm-save">' + (editing ? 'Save changes' : 'Create funnel') + '</button>';
    openModal({ title: editing ? 'Edit funnel' : 'New funnel', icon: GEAR_SVG, bodyHTML: body, footerHTML: footer, onMount: function () {
      $('#fm-name').value = d.name || ''; $('#fm-desc').value = d.description || ''; $('#fm-open').value = d.opening_message || '';
      $('#fm-name').focus();
      $('#fm-save').addEventListener('click', async function () {
        var name = $('#fm-name').value.trim(), open = $('#fm-open').value.trim(), desc = $('#fm-desc').value.trim();
        if (!name) { $('#fm-msg').textContent = 'Name is required'; return; }
        if (!open) { $('#fm-msg').textContent = 'Opening message is required'; return; }
        var payload = { name: name, opening_message: open, description: desc || null };
        var btn = $('#fm-save'); btn.disabled = true; $('#fm-msg').textContent = '';
        try {
          if (editing) {
            state.details[d.id] = await Fastt.put('/admin/funnels/' + d.id, payload);
            await loadAll(); closeModal(); await selectFunnel(d.id); Fastt.saved('Funnel saved');
          } else {
            var created = await Fastt.post('/admin/funnels', payload);
            await loadAll(); closeModal(); await selectFunnel(created.id); Fastt.saved('Funnel “' + created.name + '” created');
          }
        } catch (e) { $('#fm-msg').textContent = (e.body && e.body.detail) || e.message || 'Save failed'; btn.disabled = false; }
      });
      var del = $('#fm-del');
      if (del) del.addEventListener('click', async function () {
        if (!confirm('Delete funnel “' + d.name + '”?\n\nIts steps and every model’s media mapping go with it. This cannot be undone.')) return;
        del.disabled = true; $('#fm-msg').textContent = '';
        try {
          await Fastt.del('/admin/funnels/' + d.id);
          delete state.details[d.id]; state.sel = null;
          await loadAll();
          state.sel = state.funnels.length ? state.funnels[0].id : null;
          closeModal();
          if (state.sel != null) { await selectFunnel(state.sel); } else { renderAll(); }
          Fastt.saved('Funnel deleted');
        } catch (e) { $('#fm-msg').textContent = (e.body && e.body.detail) || e.message || 'Delete failed'; del.disabled = false; }
      });
    } });
  }

  // ---- step editor (reply / PPV, variants, AI-generate, pacing) ----
  var KNOWN_STEP = { type: 1, step: 1, messages: 1, generate: 1, prompt: 1, price: 1, locked_text: 1, check_intervals_min: 1 };
  function openStepModal(stepIndex) {
    var d = state.details[state.sel];
    if (!d) { Fastt.toast('Pick a funnel first', 'err'); return; }
    var steps = (d.steps || []).slice();
    var isNew = stepIndex == null;
    var src = isNew ? {} : (steps[stepIndex] || {});
    var extra = {}; Object.keys(src).forEach(function (k) { if (!KNOWN_STEP[k]) extra[k] = src[k]; });
    var stepNo = isNew ? (steps.reduce(function (m, s) { return Math.max(m, s.step || 0); }, 0) + 1) : (src.step || stepIndex + 1);
    var draft = {
      kind: isPpv(src) ? 'ppv' : 'reply',
      intervals: Array.isArray(src.check_intervals_min) ? src.check_intervals_min.join(', ') : '',
      price: (src.price != null) ? String(src.price) : '0',
      locked: !!src.locked_text,
      prompt: src.prompt ? String(src.prompt) : '',
      messages: Array.isArray(src.messages) ? src.messages.map(String) : [],
    };
    var hasStatic = draft.messages.some(function (m) { return m.trim(); });
    draft.generate = !hasStatic && !!(src.generate || (src.prompt && String(src.prompt).trim()));
    if (!draft.messages.length) draft.messages = [''];

    openModal({ title: isNew ? 'Add step' : 'Edit step ' + stepNo, icon: STEP_SVG,
      bodyHTML: '<div id="se-body"></div>',
      footerHTML: '<div class="sp"></div><span class="err" id="se-msg"></span>' +
        '<button class="fx-btn ghost" data-mclose>Cancel</button>' +
        '<button class="fx-btn" id="se-save">' + (isNew ? 'Add step' : 'Save step') + '</button>',
      onMount: function () { drawBody(); $('#se-save').addEventListener('click', saveStep); } });

    function drawBody() {
      var b = $('#se-body'); if (!b) return;
      var h = '';
      h += '<div class="fx-field"><label>Step type</label><div class="fx-seg" id="se-kind">' +
        '<button type="button" data-k="reply" class="' + (draft.kind === 'reply' ? 'on' : '') + '">Reply · free</button>' +
        '<button type="button" data-k="ppv" class="' + (draft.kind === 'ppv' ? 'on' : '') + '">Paid PPV</button></div></div>';
      if (draft.kind === 'reply') {
        h += '<div class="fx-field"><label>Check for a reply at (minutes, comma-separated — blank = 2, 4, 10)</label>' +
          '<input class="fx-input" id="se-iv" value="' + esc(draft.intervals) + '" placeholder="2, 4, 10"></div>';
      } else {
        h += '<div style="display:flex;gap:16px;align-items:flex-start">' +
          '<div class="fx-field" style="width:150px"><label>Price (USD, whole)</label><div class="fx-unit" data-unit="$"><input class="fx-input" id="se-price" inputmode="numeric" value="' + esc(draft.price) + '"></div></div>' +
          '<label class="fx-check ' + (draft.locked ? 'on' : '') + '" id="se-lock" style="margin-top:26px"><span class="bx"></span>Lock the text too<span class="sub">pay to read</span></label></div>';
      }
      h += '<label class="fx-check ' + (draft.generate ? 'on' : '') + '" id="se-gen"><span class="bx"></span>Generate the ' + (draft.kind === 'ppv' ? 'sales copy' : 'reply') + ' with AI</label>';
      if (draft.generate) {
        h += '<div class="fx-field"><label>Prompt <span style="color:var(--muted2);font-weight:400">(optional — guides the AI; blank uses the persona)</span></label>' +
          '<textarea class="fx-input" id="se-prompt" rows="3" placeholder="e.g. tease the PPV and build urgency…">' + esc(draft.prompt) + '</textarea></div>';
      } else {
        h += '<div class="fx-field"><label>' + (draft.kind === 'ppv' ? 'Sales copy' : 'Reply') + ' variants <span style="color:var(--muted2);font-weight:400">(one sent at random; first ≤2 used)</span></label>' +
          '<div class="fn-vars" id="se-vars"></div><button type="button" class="fn-addvar" id="se-addvar">+ add variant</button></div>';
      }
      b.innerHTML = h;
      if (!draft.generate) drawVars();
      bindBody();
    }
    function drawVars() {
      var v = $('#se-vars'); if (!v) return;
      v.innerHTML = draft.messages.map(function (m, i) {
        return '<div class="fn-var"><textarea class="fx-input" data-vi="' + i + '" rows="2" placeholder="Variant ' + (i + 1) + '">' + esc(m) + '</textarea>' +
          (draft.messages.length > 1 ? '<button type="button" class="rm" data-rm="' + i + '">×</button>' : '') + '</div>';
      }).join('');
    }
    function bindBody() {
      $$('#se-kind button').forEach(function (btn) { btn.addEventListener('click', function () { sync(); draft.kind = btn.dataset.k; drawBody(); }); });
      var gen = $('#se-gen'); if (gen) gen.addEventListener('click', function () { sync(); draft.generate = !draft.generate; drawBody(); });
      var lock = $('#se-lock'); if (lock) lock.addEventListener('click', function () { draft.locked = !draft.locked; lock.classList.toggle('on', draft.locked); });
      var addv = $('#se-addvar'); if (addv) addv.addEventListener('click', function () { sync(); draft.messages.push(''); drawVars(); bindVars(); });
      bindVars();
    }
    function bindVars() {
      var v = $('#se-vars'); if (!v) return;
      $$('[data-rm]', v).forEach(function (btn) { btn.addEventListener('click', function () { sync(); if (draft.messages.length > 1) draft.messages.splice(+btn.dataset.rm, 1); drawVars(); bindVars(); }); });
    }
    function sync() {
      var iv = $('#se-iv'); if (iv) draft.intervals = iv.value;
      var pr = $('#se-price'); if (pr) draft.price = pr.value;
      var pm = $('#se-prompt'); if (pm) draft.prompt = pm.value;
      var v = $('#se-vars'); if (v) $$('[data-vi]', v).forEach(function (t) { draft.messages[+t.dataset.vi] = t.value; });
    }
    function toWire() {
      sync();
      var out = Object.assign({}, extra, { step: stepNo });
      var msgs = draft.messages.map(function (m) { return m.trim(); }).filter(Boolean);
      if (draft.kind === 'ppv') {
        out.type = 'paid_ppv';
        if (String(draft.price).trim() !== '') {
          var p = Number(draft.price);
          if (!Number.isInteger(p) || p < 0) throw new Error('Price must be a whole number ≥ 0');
          out.price = p;
        }
        if (draft.locked) out.locked_text = true;
      } else {
        var parts = String(draft.intervals).split(/[\s,]+/).map(function (s) { return s.trim(); }).filter(Boolean);
        var ivs = [];
        for (var i = 0; i < parts.length; i++) { var n = Number(parts[i]); if (!Number.isInteger(n) || n <= 0) throw new Error('Interval “' + parts[i] + '” must be a positive whole number'); ivs.push(n); }
        if (ivs.length) out.check_intervals_min = ivs;
      }
      if (draft.generate) { out.generate = true; if (draft.prompt.trim()) out.prompt = draft.prompt.trim(); }
      else { if (!msgs.length) throw new Error('Add at least one message, or switch it to AI-generated'); out.messages = msgs; }
      return out;
    }
    async function saveStep() {
      var wire; try { wire = toWire(); } catch (e) { $('#se-msg').textContent = e.message; return; }
      var next = steps.slice();
      if (isNew) next.push(wire); else next[stepIndex] = wire;
      next = next.map(function (s, i) { return Object.assign({}, s, { step: i + 1 }); });
      var btn = $('#se-save'); btn.disabled = true; $('#se-msg').textContent = '';
      try {
        state.details[d.id] = await Fastt.put('/admin/funnels/' + d.id, { steps: next });
        await loadSel(); closeModal(); renderAll();
        Fastt.saved(isNew ? 'Step added' : 'Step saved');
      } catch (e) { $('#se-msg').textContent = (e.body && e.body.detail) || e.message || 'Save failed'; btn.disabled = false; }
    }
  }
  async function reorderStep(stepIndex, dir) {
    var d = state.details[state.sel]; if (!d) return;
    var steps = (d.steps || []).slice(); var j = stepIndex + dir;
    if (j < 0 || j >= steps.length) return;
    var t = steps[stepIndex]; steps[stepIndex] = steps[j]; steps[j] = t;
    steps = steps.map(function (s, i) { return Object.assign({}, s, { step: i + 1 }); });
    try { state.details[d.id] = await Fastt.put('/admin/funnels/' + d.id, { steps: steps }); await loadSel(); renderAll(); Fastt.saved('Step order saved'); }
    catch (e) { Fastt.oops(e); }
  }
  async function removeStep(stepIndex) {
    var d = state.details[state.sel]; if (!d) return;
    var steps = (d.steps || []); var s = steps[stepIndex]; if (!s) return;
    if (!confirm('Remove step ' + (s.step || stepIndex + 1) + '?\n\nIts copy' + (isPpv(s) ? ' and every model’s media mapping for it' : '') + ' will be dropped.')) return;
    var next = steps.filter(function (_, i) { return i !== stepIndex; }).map(function (x, i) { return Object.assign({}, x, { step: i + 1 }); });
    try { state.details[d.id] = await Fastt.put('/admin/funnels/' + d.id, { steps: next }); await loadSel(); renderAll(); Fastt.saved('Step removed'); }
    catch (e) { Fastt.oops(e); }
  }

  // ---- per-model media mapper (opener ids + per-PPV ids + free preview count) ----
  function openMediaModal() {
    var d = state.details[state.sel];
    if (!d || !acct) { Fastt.toast('Pick a creator first', 'err'); return; }
    var media = state.media[state.sel] || { opening_media_ids: [], steps_media: {} };
    var ppv = (d.steps || []).filter(isPpv);
    var nm = acctName();
    var draft = { opener: (media.opening_media_ids || []).slice(), steps: {} };
    ppv.forEach(function (s) {
      var m = (media.steps_media || {})[String(s.step)] || {};
      draft.steps[s.step] = { ids: (m.media_files || []).slice(), preview: (m.previews || []).length };
    });
    var body =
      '<div class="fx-note"><span>Vault ids come from <b>' + esc(nm) + '</b>’s vault — an id from one model is meaningless in another’s. The funnel text is shared; the media is per model.</span></div>' +
      '<div class="fn-msec"><div class="sh">Opening message media</div>' +
        '<div class="fx-field"><label>Vault ids (comma-separated — blank = text-only opener)</label><input class="fx-input" id="mm-open" value="' + esc(draft.opener.join(', ')) + '"></div>' +
        '<div class="fn-mstrip" id="mm-open-strip"></div></div>' +
      (ppv.length ? ppv.map(function (s) {
        var st = draft.steps[s.step];
        return '<div class="fn-msec"><div class="sh">Step ' + s.step + ' · PPV<span class="pr">$' + esc(s.price != null ? s.price : '?') + '</span></div>' +
          '<div class="fx-field"><label>Vault ids (comma-separated — blank = unmapped, step is skipped)</label><input class="fx-input mm-ids" data-step="' + s.step + '" value="' + esc(st.ids.join(', ')) + '"></div>' +
          '<div class="fx-field" style="max-width:240px"><label>Free preview — first N sent as a teaser</label><input class="fx-input mm-prev" data-step="' + s.step + '" inputmode="numeric" value="' + st.preview + '"></div>' +
          '<div class="fn-mstrip" data-strip="' + s.step + '"></div></div>';
      }).join('') : '<div class="fx-note warn"><span>This funnel has no PPV steps yet — add one, save, then map its media here.</span></div>');
    openModal({ title: 'Map media · ' + nm, icon: IMG_SVG, bodyHTML: body,
      footerHTML: '<div class="sp"></div><span class="err" id="mm-msg"></span>' +
        '<button class="fx-btn ghost" data-mclose>Cancel</button><button class="fx-btn" id="mm-save">Save media</button>',
      onMount: function () {
        drawStrips();
        $('#mm-open').addEventListener('input', drawStrips);
        $$('.mm-ids').forEach(function (i) { i.addEventListener('input', drawStrips); });
        $$('.mm-prev').forEach(function (i) { i.addEventListener('input', drawStrips); });
        $('#mm-save').addEventListener('click', saveMedia);
      } });
    function parseIds(raw) { return String(raw || '').split(/[\s,]+/).map(function (x) { return x.trim(); }).filter(function (x) { return /^\d+$/.test(x); }).map(Number); }
    function tile(id, free) {
      return '<div class="fn-mt' + (free ? ' free' : '') + '"><div class="im">#' + esc(id) +
        '<img src="/admin/vault-ai/thumb?account_id=' + encodeURIComponent(acct) + '&media_id=' + encodeURIComponent(id) +
        '" alt="" loading="lazy" style="position:absolute;inset:0" onerror="this.remove()"></div>' +
        (free ? '<span class="flag">🔓</span>' : '') + '</div>';
    }
    function drawStrips() {
      var os = $('#mm-open-strip');
      if (os) os.innerHTML = parseIds($('#mm-open').value).map(function (id) { return tile(id, false); }).join('');
      $$('.fn-mstrip[data-strip]').forEach(function (strip) {
        var stp = strip.getAttribute('data-strip');
        var ids = parseIds($('.mm-ids[data-step="' + stp + '"]').value);
        var prev = Math.max(0, Math.min(parseInt($('.mm-prev[data-step="' + stp + '"]').value, 10) || 0, ids.length));
        strip.innerHTML = ids.map(function (id, i) { return tile(id, i < prev); }).join('');
      });
    }
    async function saveMedia() {
      var opener = parseIds($('#mm-open').value);
      var steps_media = {};
      $$('.mm-ids').forEach(function (inp) {
        var stp = inp.getAttribute('data-step'); var ids = parseIds(inp.value);
        if (!ids.length) return;
        var prev = Math.max(0, Math.min(parseInt($('.mm-prev[data-step="' + stp + '"]').value, 10) || 0, ids.length));
        steps_media[String(stp)] = { media_files: ids, previews: ids.slice(0, prev) };
      });
      var btn = $('#mm-save'); btn.disabled = true; $('#mm-msg').textContent = '';
      try {
        state.media[state.sel] = await Fastt.put('/admin/funnels/' + d.id + '/media/' + acct, { opening_media_ids: opener, steps_media: steps_media });
        state.xmedia[d.id + ':' + acct] = state.media[state.sel];
        closeModal(); renderTimeline(); renderAdvanced(); Fastt.saved('Media mapping saved for ' + nm);
      } catch (e) { $('#mm-msg').textContent = (e.body && e.body.detail) || e.message || 'Save failed'; btn.disabled = false; }
    }
  }

  // ---- reply-walker enable/disable (click + confirm; never auto-fired) ----
  async function toggleWalker() {
    if (!acct) { Fastt.toast('Pick a creator first', 'err'); return; }
    var r = state.walker;
    var turnOn = !(r && r.is_enabled);
    var msg = turnOn
      ? 'Enable reply-walking on ' + acctName() + '?\n\nEvery ~2 min the walker checks each funnel replier and sends the next reply / PPV step. This messages REAL fans.'
      : 'Pause reply-walking on ' + acctName() + '?\n\nFunnels still send the opener, but fans already inside stop advancing through the steps.';
    if (!confirm(msg)) return;
    try {
      if (r) await Fastt.patch('/admin/automation-rules/' + r.id, { is_enabled: turnOn });
      else if (turnOn) await Fastt.post('/admin/automation-rules', { account_id: acct, kind: 'reply_mass_funnel', name: 'Reply-walking', every_seconds: 120, payload: {}, is_enabled: true });
      state.walker = await Fastt.rule('reply_mass_funnel');
      renderStatus();
      Fastt.saved(turnOn ? 'Reply-walking enabled' : 'Reply-walking paused');
    } catch (e) { Fastt.oops(e); }
  }

  function renderAll() { renderStatus(); renderPicker(); renderTimeline(); renderAdvanced(); }

  // ---- actions ----
  async function selectFunnel(id) {
    state.sel = id;
    await loadSel();
    renderAll();
    loadMatrix().catch(function (e) { console.warn('matrix load failed', e); });
  }
  function newFunnel() { openFunnelModal('new'); }
  $('#fn-new').addEventListener('click', newFunnel);
  $('#fn-settings').addEventListener('click', function () {
    if (state.sel == null) { openFunnelModal('new'); return; }
    openFunnelModal('edit');
  });
  $('#fn-pick').addEventListener('click', function (e) {
    if (e.target.closest('#fn-new2')) { newFunnel(); return; }
    var card = e.target.closest('.fpk[data-fid]');
    if (card) selectFunnel(parseInt(card.dataset.fid, 10)).catch(Fastt.oops);
  });
  $('#fn-tl').addEventListener('click', function (e) {
    if (e.target.closest('#fn-addstep')) { openStepModal(null); return; }
    if (e.target.closest('[data-edit-opener]')) { openFunnelModal('edit'); return; }
    var ed = e.target.closest('[data-step-edit]'); if (ed) { openStepModal(+ed.getAttribute('data-step-edit')); return; }
    var up = e.target.closest('[data-step-up]'); if (up) { reorderStep(+up.getAttribute('data-step-up'), -1); return; }
    var dn = e.target.closest('[data-step-down]'); if (dn) { reorderStep(+dn.getAttribute('data-step-down'), 1); return; }
    var rm = e.target.closest('[data-step-del]'); if (rm) { removeStep(+rm.getAttribute('data-step-del')); return; }
  });
  document.addEventListener('click', function (e) {
    if (e.target.closest('#fn-walker-toggle')) { toggleWalker(); }
  });
  $('#fn-iv').addEventListener('change', async function () {
    var d = state.details[state.sel];
    if (!d) return;
    var iv = $('#fn-iv').value.split(/[,\s]+/).map(function (x) { return parseInt(x, 10); })
      .filter(function (n) { return isFinite(n) && n > 0; });
    if (!iv.length) { Fastt.toast('Give at least one positive minute value', 'err'); renderAdvanced(); return; }
    var steps = (d.steps || []).map(function (s) {
      return isPpv(s) ? s : Object.assign({}, s, { check_intervals_min: iv });
    });
    try {
      state.details[d.id] = await Fastt.put('/admin/funnels/' + d.id, { steps: steps });
      renderTimeline(); renderAdvanced();
      Fastt.saved('Reply pacing saved on every reply step');
    } catch (e) { Fastt.oops(e); }
  });
  $('#fn-max').addEventListener('change', async function () {
    if (!state.walker) { Fastt.toast('No reply_mass_funnel rule on this account yet'); return; }
    var n = parseInt($('#fn-max').value, 10);
    var payload = Object.assign({}, state.walker.payload || {});
    if (isFinite(n) && n > 0) payload.max_chats = n; else delete payload.max_chats;
    try {
      await Fastt.patch('/admin/automation-rules/' + state.walker.id, { payload: payload });
      state.walker = await Fastt.rule('reply_mass_funnel');
      Fastt.saved();
    } catch (e) { Fastt.oops(e); }
  });
  document.addEventListener('click', function (e) {
    if (e.target.closest('#fn-media-edit')) openMediaModal();
  });

  // ---- badges ----
  Fastt.liveBadge($('#fn-status'));
  Fastt.liveBadge($('#fn-pick-lbl'));
  var advH4 = $$('.fx-adv-group > h4');
  advH4.forEach(function (h) {
    var t = h.textContent.trim();
    if (t === 'Reply pacing' || t === 'Per-model media' || t === 'Raw steps'
        || t === 'Send the tease (launch)') Fastt.liveBadge(h);
  });

  // ---- boot ----
  state.roster = Fastt.accounts() || [];
  await loadAll();
  state.walker = acct ? await Fastt.rule('reply_mass_funnel') : null;
  if (acct) await loadLists();
  await loadSel();
  renderLaunchAudience();
  renderAll();
  if (!acct) {
    ['#fn-send-btn'].forEach(function (s) {
      var b = $(s); if (!b) return;
      b.disabled = true; b.style.opacity = '.5';
      b.title = 'Pick a creator first — a launch has to name the sending account';
    });
    Fastt.toast('No creator selected — stats/media are account-scoped; pick a creator top-left');
  } else {
    await loadMatrix();
  }
});
