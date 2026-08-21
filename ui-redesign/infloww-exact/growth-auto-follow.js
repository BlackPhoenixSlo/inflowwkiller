/* ── live wiring: automation rules kind=auto_follow, ACROSS THE WHOLE ROSTER ──
 *
 * What changed and why:
 *  • The table used to render ONE row (the selected account) under a column
 *    headed "Creator name". The roster is 15 and the only creator that HAS
 *    auto-follow data (Jaka, rule 169) was invisible on every other scope, so
 *    the page read as "nothing ever ran". Both list endpoints answer unscoped,
 *    so one GET each now backs all 15 rows and the Creator filter is real.
 *  • runFollows() summed only followed+refollowed+pinged. service/automations/
 *    auto_follow.py emits `liked` for like_messages / like_posts — the most
 *    common action reported 0 in every numeric column. `liked` is counted now
 *    and the header word comes from the configured action.
 *  • A dry run returned 0 and threw the PLAN away. rule.last_run.stats carries
 *    candidates + would_follow/would_ping/would_like_fans + a note; that is
 *    real work and is rendered as a PLAN (badged), never as a send.
 *  • There was no configuration surface at all — one switch that hardcoded
 *    {action:follow, source:expired, cap 5, dry_run} forever. The editor below
 *    the table mirrors app/components/growth/AutoFollowTab.tsx.
 */

Fastt.ready(async () => {
  const $ = Fastt.$, esc = Fastt.esc;

  const rowsEl = $('#af-rows');
  const editorEl = $('#af-editor');
  const acct = Fastt.account();
  if (!acct) {
    rowsEl.innerHTML = '<div class="empty"><div>No creator selected — click the creator name (top-left) to pick one.</div></div>';
    return;
  }
  Fastt.liveBadge($('#af-title'));

  const roster = Fastt.accounts().slice();
  const nameOf = (id) => {
    const a = roster.find((r) => String(r.id) === String(id));
    return (a && (a.nickname || a.id)) || String(id);
  };

  let rules = {};      // account_id -> auto_follow rule (or absent)
  let runsBy = {};     // account_id -> [run rows, newest first]
  let smartLists = []; // segments for the account being edited
  let selected = acct; // which creator the editor is bound to
  let form = null;

  /* ── numbers ──────────────────────────────────────────────────────── */
  const statsOf = (r) => { try { return JSON.parse(r.stats_json || '{}') || {}; } catch (e) { return {}; } };
  // Actions a run actually FIRED. `liked` is the like_messages / like_posts
  // counter (auto_follow.py:452,:488); followed/refollowed/pinged cover
  // follow/ping (:386-414). A dry run fires nothing — it is counted as a plan.
  function fired(st) {
    if (st.dry_run) return 0;
    return (st.followed || 0) + (st.refollowed || 0) + (st.pinged || 0) + (st.liked || 0);
  }
  const planned = (st) => (st.would_follow || st.would_ping || st.would_like || st.would_like_fans || []).length;
  // automation_runs.started_at is tz-NAIVE UTC ("2026-07-24T15:25:35.910398");
  // a bare new Date() reads it as local and slides runs into the wrong day.
  const startedMs = (r) => { const d = Fastt.parseUtc(r.started_at); return d ? d.getTime() : 0; };
  function sumSince(id, days) {
    const cut = Date.now() - days * 86400e3;
    return (runsBy[id] || []).reduce((n, r) => (startedMs(r) >= cut) ? n + fired(statsOf(r)) : n, 0);
  }
  function sumDay(id, offset) {  // 0 = today, 1 = yesterday (local days)
    const d = new Date(); d.setHours(0, 0, 0, 0);
    const start = d.getTime() - offset * 86400e3, end = start + 86400e3;
    return (runsBy[id] || []).reduce((n, r) => {
      const t = startedMs(r);
      return (t >= start && t < end) ? n + fired(statsOf(r)) : n;
    }, 0);
  }

  const ACTION_WORD = { follow: 'follows', like_messages: 'likes', like_posts: 'likes', ping: 'pings' };
  const ACTION_LABEL = {
    like_messages: 'Like latest message (re-engage)',
    follow: 'Follow fans back (win-back)',
    ping: 'Re-follow ping (quiet fans)',
    like_posts: 'Like posts by id (API-only action)',
  };
  const SOURCE_LABEL = {
    expired: 'Recently-expired fans (win-back)',
    recent_active: 'Recently-active fans',
    smart_list: 'A Smart List',
  };
  const SOURCES_BY_ACTION = {
    follow: ['expired', 'recent_active', 'smart_list'],
    like_messages: ['recent_active', 'smart_list'],
    like_posts: [], ping: [],
  };
  const actionOf = (r) => (r && r.payload && r.payload.action) || 'like_messages';
  const isDry = (r) => !r || !r.payload || r.payload.dry_run !== false;

  /* ── friendly presets (lead master-card) ──────────────────────────────
   * Each preset only sets the two pace dials (cap/run + cadence). It never
   * flips dry-run or enable — those stay explicit, gated actions so the
   * master card can never silently start acting on real fans. */
  const PRESETS = {
    gentle:     { cap: 10, every: 360, name: 'Gentle',     desc: 'Slow & safe',
                  bullets: ['~10 actions per run', 'Runs every 6 hours', 'Best while you learn the tool'] },
    normal:     { cap: 25, every: 240, name: 'Normal',     desc: 'Balanced pace',
                  bullets: ['~25 actions per run', 'Runs every 4 hours', 'The house default'] },
    aggressive: { cap: 50, every: 120, name: 'Aggressive', desc: 'Maximum reach',
                  bullets: ['~50 actions per run', 'Runs every 2 hours', 'Fastest win-backs'] },
  };
  const matchPreset = (f) => Object.keys(PRESETS).find(
    (k) => PRESETS[k].cap === f.dailyCap && PRESETS[k].every === f.everyMinutes) || '';
  function openAdvanced() {
    const adv = $('#af-adv');
    if (adv && !adv.classList.contains('open')) adv.classList.add('open');
  }

  /* ── table ────────────────────────────────────────────────────────── */
  function lastCell(id) {
    const r = rules[id];
    const lr = r && r.last_run;
    if (!lr || !lr.started_at) {
      return r ? '<span style="color:#8a8a8a">rule saved, never run</span>'
               : '<span style="color:#6f6f6f">no auto-follow rule on this creator</span>';
    }
    const st = lr.stats || {};
    const bits = [];
    bits.push('<span class="af-tag ' + (lr.status === 'ok' ? 'ok' : 'err') + '">' + esc(lr.status || '?') + '</span>');
    if (st.dry_run) {
      bits.push('<span class="af-tag plan" title="' + esc(st.note || 'dry run — planned only, nothing was sent') + '">PLAN '
        + planned(st) + ' of ' + (st.candidates || 0) + '</span>');
    } else if (fired(st)) {
      bits.push('<span class="af-tag blue">' + fired(st) + ' ' + (ACTION_WORD[actionOf(r)] || 'actions') + '</span>');
    } else {
      bits.push('<span class="af-tag mut">0 sent</span>');
    }
    if (r.has_pending_job) bits.push('<span class="af-tag blue">queued</span>');
    let html = '<div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center">' + bits.join('') + '</div>'
      + '<div style="color:#8a8a8a;font-size:12.5px;margin-top:5px">' + esc(Fastt.fmtAgo(lr.started_at))
      + ' · ' + esc(Fastt.fmtDate(lr.started_at)) + '</div>';
    if (lr.error_text) html += '<div style="color:#e05b5b;font-size:12.5px;margin-top:3px">' + esc(lr.error_text) + '</div>';
    return html;
  }

  /* ── live summary strip (roster-wide, filter-independent) ─────────────
   * Every number here is folded from the two lists already loaded — no extra
   * calls — so the operator sees the whole roster's posture before scanning
   * rows: how many creators are wired, how many act live, recent volume, and
   * the freshest run. Recomputed on every render so toggles reflect instantly. */
  function updateSummary() {
    const el = $('#af-summary');
    if (!el) return;
    const ids = Object.keys(rules);
    const configured = ids.length;
    const enabled = ids.filter((id) => rules[id].is_enabled);
    const liveActs = enabled.filter((id) => !isDry(rules[id]));   // enabled AND dry-run OFF
    let actions7 = 0;
    roster.forEach((a) => { actions7 += sumSince(a.id, 7); });
    let lastMs = 0, lastStamp = null;
    Object.keys(runsBy).forEach((id) => runsBy[id].forEach((r) => {
      const t = startedMs(r); if (t > lastMs) { lastMs = t; lastStamp = r.started_at; }
    }));
    const enabledLive = liveActs.length;
    el.innerHTML =
      '<div class="af-stat"><div class="k"><span class="d"></span>Configured</div>'
        + '<div class="v">' + configured + ' <span style="font-size:16px;color:#8a8a8a;font-weight:600">of ' + roster.length + '</span></div>'
        + '<div class="s">' + (configured ? 'creators have an auto-follow rule' : 'no creator is set up yet — click a row') + '</div></div>'
      + '<div class="af-stat' + (enabled.length ? ' live' : '') + '"><div class="k"><span class="d"></span>Enabled</div>'
        + '<div class="v">' + enabled.length + '</div>'
        + '<div class="s">' + (enabledLive
            ? enabledLive + ' act live · ' + (enabled.length - enabledLive) + ' plan-only'
            : enabled.length ? 'all in dry-run (plan only)' : 'none running') + '</div></div>'
      + '<div class="af-stat"><div class="k"><span class="d"></span>Actions · 7 days</div>'
        + '<div class="v">' + Fastt.fmtInt(actions7) + '</div>'
        + '<div class="s">real likes / follows / pings fired</div></div>'
      + '<div class="af-stat"><div class="k"><span class="d"></span>Last activity</div>'
        + '<div class="v" style="font-size:20px">' + (lastStamp ? esc(Fastt.fmtAgo(lastStamp)) : 'never') + '</div>'
        + '<div class="s">' + (lastStamp ? esc(Fastt.fmtDate(lastStamp)) : 'no run recorded across the roster') + '</div></div>';
  }

  /* ── lead master-card: one on/off toggle + a pace preset + Save ───────
   * Binds to the SELECTED creator (same `form`/`save()` the editor uses).
   * The toggle only stages `form.enabled`; Save persists it behind the same
   * confirm gate, so nothing here can enable a rule without a click+confirm. */
  function renderMaster() {
    const el = $('#af-master');
    if (!el) return;
    const r = rules[String(selected)];
    if (!form) form = ruleToForm(r);
    const cur = matchPreset(form);
    const savedOn = !!(r && r.is_enabled);
    const pendingOn = !!form.enabled;
    const chips = Object.keys(PRESETS).map((k) => {
      const p = PRESETS[k];
      return '<div class="af-preset' + (cur === k ? ' sel' : '') + '" data-preset="' + k + '">'
        + '<div class="pn">' + esc(p.name) + '</div><div class="pt">' + esc(p.desc) + '</div>'
        + '<ul>' + p.bullets.map((b) => '<li>' + esc(b) + '</li>').join('') + '</ul></div>';
    }).join('');
    const saveLabel = r ? (pendingOn && !savedOn ? 'Save &amp; turn on' : 'Save changes') : 'Create automation';
    const dryNote = form.dryRun
      ? 'Dry-run is <b style="color:#e2a94e">ON</b> — plans only, nothing touches OnlyFans.'
      : 'Dry-run is <b style="color:#e05b5b">OFF</b> — runs act on real fans on OnlyFans.';
    el.innerHTML =
      '<div class="af-m-head">'
        + '<div class="af-m-title">Auto-follow · <b>' + esc(nameOf(selected)) + '</b></div>'
        + '<div class="af-m-tgl-wrap"><span class="af-m-state' + (pendingOn ? ' on' : '') + '">' + (pendingOn ? 'On' : 'Off') + '</span>'
        + '<span class="tgl blue' + (pendingOn ? ' on' : '') + '" id="af-m-tgl" title="Turn this creator’s auto-follow on or off (applied when you Save)"></span></div>'
      + '</div>'
      + '<div class="af-hint" style="margin-top:3px">Re-engage fans with a like, a follow-back or a re-follow ping — never a DM. Pick a pace, then Save.</div>'
      + '<div class="af-presets">' + chips + '</div>'
      + '<div class="af-m-acts">'
        + '<button class="btn-blue" id="af-m-save">' + saveLabel + '</button>'
        + '<span class="af-hint">' + dryNote + ' <a class="af-adv-link" id="af-m-adv">Advanced settings ›</a></span>'
      + '</div>';
    el.querySelectorAll('.af-preset').forEach((p) => p.addEventListener('click', () => {
      const k = p.dataset.preset;
      form.dailyCap = PRESETS[k].cap; form.everyMinutes = PRESETS[k].every;
      renderMaster(); renderEditor();
    }));
    $('#af-m-tgl', el).addEventListener('click', () => {
      form.enabled = !form.enabled;
      renderMaster();
      const en = $('[data-af="enabled"]', editorEl); if (en) en.checked = form.enabled;
    });
    $('#af-m-save', el).addEventListener('click', save);
    $('#af-m-adv', el).addEventListener('click', () => {
      openAdvanced();
      if (editorEl.scrollIntoView) editorEl.scrollIntoView({ block: 'center' });
    });
  }

  function render() {
    updateSummary();
    renderMaster();
    const fc = $('#af-fcreator').value, fs = $('#af-fstate').value;
    const vis = roster.filter((a) => {
      if (fc && String(a.id) !== String(fc)) return false;
      const r = rules[a.id];
      if (fs === 'on') return !!(r && r.is_enabled);
      if (fs === 'off') return !!(r && !r.is_enabled);
      if (fs === 'none') return !r;
      return true;
    });

    // Header word follows the configured action instead of the fixed "follows".
    const acts = new Set(roster.filter((a) => rules[a.id]).map((a) => ACTION_WORD[actionOf(rules[a.id])] || 'actions'));
    const word = acts.size === 1 ? [...acts][0] : 'actions';
    $('#af-thead').innerHTML =
      '<div class="th" style="width:230px">Creator name</div>'
      + '<div class="th" style="width:120px" title="Fired across every stored run">Total ' + word + '</div>'
      + '<div class="th" style="width:90px">Today</div><div class="th" style="width:105px">Yesterday</div>'
      + '<div class="th" style="width:90px">7 days</div><div class="th" style="width:90px">30 days</div>'
      + '<div class="th" style="flex:1;min-width:0">Last run</div>'
      + '<div class="th" style="width:110px;justify-content:flex-end">Action</div>';

    if (!vis.length) {
      rowsEl.innerHTML = '<div class="empty"><div>No creator matches this filter.</div></div>';
      return;
    }
    rowsEl.innerHTML = vis.map((a) => {
      const id = String(a.id), r = rules[id];
      const p = (r && r.payload) || {};
      const total = (runsBy[id] || []).reduce((n, x) => n + fired(statsOf(x)), 0);
      const meta = r
        ? esc((ACTION_LABEL[actionOf(r)] || actionOf(r)).replace(/ \(.*\)$/, '')
            + ' · cap ' + (p.daily_cap != null ? p.daily_cap : 50) + '/run'
            + ' · every ' + Math.round((r.every_seconds || 14400) / 60) + ' min'
            + (isDry(r) ? ' · dry-run' : ''))
        : '<span style="color:#6f6f6f">not set up — click to configure</span>';
      const due = r && r.next_due_at
        ? '<div style="color:#6f6f6f;font-size:12px;margin-top:4px">next due ' + esc(Fastt.fmtDate(r.next_due_at)) + '</div>' : '';
      return `<div class="trow${id === String(selected) ? ' af-on' : ''}" data-acct="${esc(id)}">
        <div class="td" style="width:230px;display:flex;align-items:center;gap:12px;min-width:0">
          <span class="avsm"></span>
          <span style="min-width:0"><span style="display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(a.nickname || id)}</span>
          <span style="color:#9a9a9a;font-size:12.5px">${meta}</span></span></div>
        <div class="td" style="width:120px" title="across ${(runsBy[id] || []).length} stored run(s)">${Fastt.fmtInt(total)}</div>
        <div class="td" style="width:90px">${Fastt.fmtInt(sumDay(id, 0))}</div>
        <div class="td" style="width:105px">${Fastt.fmtInt(sumDay(id, 1))}</div>
        <div class="td" style="width:90px">${Fastt.fmtInt(sumSince(id, 7))}</div>
        <div class="td" style="width:90px">${Fastt.fmtInt(sumSince(id, 30))}</div>
        <div class="td" style="flex:1;min-width:0;padding-top:12px;padding-bottom:12px">${lastCell(id)}${due}</div>
        <div class="td" style="width:110px;display:flex;justify-content:flex-end">
          <span class="tgl${r && r.is_enabled ? ' on' : ''}" data-tgl="${esc(id)}"
            title="${r ? 'Enable / disable this creator’s auto-follow rule' : 'Configure it below first'}"></span></div>
      </div>`;
    }).join('');
  }

  /* ── editor (mirrors app/components/growth/AutoFollowTab.tsx) ──────── */
  function ruleToForm(r) {
    const p = (r && r.payload) || {}, t = p.targets || {};
    const action = p.action || 'like_messages';
    const valid = SOURCES_BY_ACTION[action] || [];
    return {
      enabled: !!(r && r.is_enabled),
      action,
      source: valid.indexOf(t.source) >= 0 ? t.source : (valid[0] || 'recent_active'),
      days: typeof t.days === 'number' ? t.days : 7,
      smartListId: typeof t.smart_list_id === 'number' ? t.smart_list_id : '',
      postIds: Array.isArray(t.post_ids) ? t.post_ids.join(', ') : '',
      quietDays: typeof p.quiet_days === 'number' ? p.quiet_days : 7,
      pingGapDays: typeof p.min_days_between_pings === 'number' ? p.min_days_between_pings : 14,
      dailyCap: typeof p.daily_cap === 'number' ? p.daily_cap : 50,
      dryRun: p.dry_run !== false,               // default TRUE, like the engine
      everyMinutes: r && r.every_seconds ? Math.max(1, Math.round(r.every_seconds / 60)) : 240,
    };
  }
  function formPayload() {
    const p = { action: form.action, daily_cap: form.dailyCap, dry_run: form.dryRun };
    if (form.action === 'ping') {
      p.quiet_days = form.quietDays; p.min_days_between_pings = form.pingGapDays;
    } else if (form.action === 'like_posts') {
      p.targets = { post_ids: String(form.postIds).split(/[,\s]+/).map(Number).filter((n) => n > 0) };
    } else if (form.source === 'smart_list') {
      p.targets = { source: 'smart_list', smart_list_id: form.smartListId === '' ? null : Number(form.smartListId) };
    } else if (form.source === 'expired') {
      p.targets = { source: 'expired' };
    } else {
      p.targets = { source: 'recent_active', days: form.days };
    }
    return p;
  }

  function runPanel(r) {
    if (!r) return '<div class="af-run">No rule stored for this creator yet — nothing has ever run.</div>';
    const lr = r.last_run;
    if (!lr || !lr.started_at) return '<div class="af-run">Rule <b>#' + r.id + '</b> exists but has never run.</div>';
    const st = lr.stats || {};
    let out = '<div class="af-run">Last run <span class="af-tag ' + (lr.status === 'ok' ? 'ok' : 'err') + '">' + esc(lr.status || '?')
      + '</span> · <b>' + esc(Fastt.fmtDate(lr.started_at)) + '</b> (' + esc(Fastt.fmtAgo(lr.started_at)) + ')'
      + (typeof st.duration_ms === 'number' ? ' · ' + st.duration_ms + ' ms' : '');
    if (lr.error_text) out += '<div style="color:#e05b5b;margin-top:6px">' + esc(lr.error_text) + '</div>';
    if (st.dry_run) {
      // The plan IS the work a dry run did — show it, badged as a plan.
      const ids = st.would_follow || st.would_ping || st.would_like || st.would_like_fans || [];
      out += '<div style="margin-top:8px"><span class="af-tag plan">DRY RUN — PLANNED, NOT SENT</span></div>'
        + '<div style="margin-top:6px">Pool <b>' + Fastt.fmtInt(st.candidates || 0) + '</b> candidate(s)'
        + ' → planned <b>' + ids.length + '</b> ' + (ACTION_WORD[st.action || actionOf(r)] || 'actions')
        + (st.source ? ' from <b>' + esc(st.source) + '</b>' : '') + '</div>'
        + (ids.length ? '<div class="af-ids" style="margin-top:4px">' + esc(ids.join(', ')) + '</div>' : '')
        + (st.note ? '<div style="margin-top:4px;color:#8a8a8a">' + esc(st.note) + '</div>' : '');
    } else {
      const keys = Object.keys(st).filter((k) => typeof st[k] === 'number' && k !== 'duration_ms');
      out += '<div style="margin-top:6px">' + (keys.length
        ? keys.map((k) => esc(k) + ' <b>' + Fastt.fmtInt(st[k]) + '</b>').join(' · ')
        : 'the run reported no counters') + '</div>';
    }
    if (r.next_due_at) out += '<div style="margin-top:6px;color:#6f6f6f">Next due ' + esc(Fastt.fmtDate(r.next_due_at)) + '</div>';
    if (r.has_pending_job) out += '<div style="margin-top:4px"><span class="af-tag blue">a job is queued right now</span></div>';
    out += '</div>';
    return out;
  }

  function renderEditor() {
    const r = rules[String(selected)];
    if (!form) form = ruleToForm(r);
    const valid = SOURCES_BY_ACTION[form.action] || [];
    const opt = (v, label, cur) => '<option value="' + esc(v) + '"' + (String(cur) === String(v) ? ' selected' : '') + '>' + esc(label) + '</option>';

    let targetBlock = '';
    if (form.action === 'ping') {
      targetBlock =
        '<label class="af-f"><span>Quiet after (days)</span><input class="af-num" type="number" min="1" data-af="quiet" value="' + form.quietDays + '"></label>'
        + '<label class="af-f"><span>Min days between pings</span><input class="af-num" type="number" min="1" data-af="gap" value="' + form.pingGapDays + '"></label>'
        + '<div class="af-f"><span>Pool</span><div class="af-hint">Fans who chatted before and have gone silent — an unfollow + instant re-follow so OnlyFans pings them. Free profiles only.</div></div>';
    } else if (form.action === 'like_posts') {
      targetBlock = '<label class="af-f" style="grid-column:span 2"><span>Post ids (comma-separated)</span>'
        + '<input class="af-num" data-af="postids" value="' + esc(form.postIds) + '" placeholder="123456, 234567"></label>';
    } else {
      targetBlock = '<label class="af-f"><span>Target</span><select class="af-sel" data-af="source">'
        + valid.map((s) => opt(s, SOURCE_LABEL[s], form.source)).join('') + '</select></label>';
      if (form.source === 'recent_active') {
        targetBlock += '<label class="af-f"><span>Active within (days)</span><input class="af-num" type="number" min="1" data-af="days" value="' + form.days + '"></label>';
      } else if (form.source === 'smart_list') {
        targetBlock += '<label class="af-f"><span>Smart List</span><select class="af-sel" data-af="list">'
          + '<option value="">— pick a segment —</option>'
          + smartLists.map((l) => opt(l.id, l.name, form.smartListId)).join('') + '</select>'
          + (smartLists.length ? '' : '<div class="af-hint">This creator has no smart lists — create one on Growth → Smart lists.</div>') + '</label>';
      } else {
        targetBlock += '<div class="af-f"><span>Pool</span><div class="af-hint">OnlyFans’ own lapsed-subscriber list — the fans worth winning back.</div></div>';
      }
    }

    editorEl.style.display = '';
    editorEl.innerHTML =
      '<h3>Configure — ' + esc(nameOf(selected))
      + (r ? '<span class="af-tag mut">rule #' + r.id + '</span>' : '<span class="af-tag mut">no rule yet</span>')
      + (r && r.is_enabled ? '<span class="af-tag ok">enabled</span>' : '<span class="af-tag mut">off</span>')
      + (isDry(r) ? '<span class="af-tag plan">dry-run</span>' : '<span class="af-tag err">LIVE — acts on OnlyFans</span>')
      + '</h3>'
      + '<div class="af-hint" style="margin-top:8px">Re-engagement nudges only — a like, a follow-back or a re-follow ping. Never a DM. '
      + 'Follow / ping price-check every fan and skip paid pages, so they cannot spend.</div>'
      + '<div class="af-grid">'
        + '<label class="af-f"><span>Action</span><select class="af-sel" data-af="action">'
          + ['like_messages', 'follow', 'ping'].map((a) => opt(a, ACTION_LABEL[a], form.action)).join('')
          + (form.action === 'like_posts' ? opt('like_posts', ACTION_LABEL.like_posts, form.action) : '')
        + '</select></label>'
        + '<label class="af-f"><span>Max actions / run</span><input class="af-num" type="number" min="0" data-af="cap" value="' + form.dailyCap + '"></label>'
        + '<label class="af-f"><span>Run every (minutes)</span><input class="af-num" type="number" min="1" data-af="every" value="' + form.everyMinutes + '"></label>'
        + targetBlock
      + '</div>'
      + '<div class="af-chks">'
        + '<label class="af-chk"><input type="checkbox" data-af="dry"' + (form.dryRun ? ' checked' : '') + '>Dry run <span class="af-hint">(plan only — never acts)</span></label>'
        + '<label class="af-chk"><input type="checkbox" data-af="enabled"' + (form.enabled ? ' checked' : '') + '>Enabled</label>'
      + '</div>'
      + '<div class="af-acts">'
        + '<button class="btn-blue" data-af="save">' + (r ? 'Save changes' : 'Create automation') + '</button>'
        + (r ? '<button class="btn-out" data-af="runnow">Run now</button>' : '')
        + '<span class="af-hint">' + (r ? 'Run now uses the LAST SAVED config, not unsaved edits.' : 'Nothing is stored for this creator until you save.') + '</span>'
      + '</div>'
      + runPanel(r);

    // ---- field bindings
    const bindNum = (k, key, min) => {
      const el = $('[data-af="' + k + '"]', editorEl); if (!el) return;
      el.addEventListener('input', () => { form[key] = Math.max(min, Number(el.value) || min); });
    };
    bindNum('cap', 'dailyCap', 0);
    bindNum('every', 'everyMinutes', 1);
    bindNum('days', 'days', 1);
    bindNum('quiet', 'quietDays', 1);
    bindNum('gap', 'pingGapDays', 1);
    const pi = $('[data-af="postids"]', editorEl); if (pi) pi.addEventListener('input', () => { form.postIds = pi.value; });
    $('[data-af="action"]', editorEl).addEventListener('change', (e) => {
      form.action = e.target.value;
      const v = SOURCES_BY_ACTION[form.action] || [];
      if (v.indexOf(form.source) < 0) form.source = v[0] || 'recent_active';
      renderEditor();
    });
    const src = $('[data-af="source"]', editorEl); if (src) src.addEventListener('change', (e) => { form.source = e.target.value; renderEditor(); });
    const lst = $('[data-af="list"]', editorEl); if (lst) lst.addEventListener('change', (e) => { form.smartListId = e.target.value; });
    $('[data-af="dry"]', editorEl).addEventListener('change', (e) => { form.dryRun = e.target.checked; renderEditor(); });
    $('[data-af="enabled"]', editorEl).addEventListener('change', (e) => { form.enabled = e.target.checked; });
    $('[data-af="save"]', editorEl).addEventListener('click', save);
    const rn = $('[data-af="runnow"]', editorEl); if (rn) rn.addEventListener('click', runNow);
  }

  async function loadSmartLists() {
    try {
      smartLists = (await Fastt.get('/admin/smart-lists', { account_id: selected }, { noAccount: true })).lists || [];
    } catch (e) { smartLists = []; }
  }

  async function save() {
    const r = rules[String(selected)];
    const payload = formPayload();
    const every_seconds = Math.max(60, Math.round(form.everyMinutes) * 60);
    // Turning the switch ON is the one action that can make this touch OnlyFans.
    if (form.enabled && !(r && r.is_enabled)) {
      if (!confirm('Enable auto-follow for ' + nameOf(selected) + '?\n\n'
        + (ACTION_LABEL[form.action] || form.action) + ' · cap ' + form.dailyCap + '/run · every '
        + form.everyMinutes + ' min\nDry-run is ' + (form.dryRun
          ? 'ON — runs only PLAN, nothing touches OnlyFans.'
          : 'OFF — runs WILL act on real fans on OnlyFans.'))) return;
    }
    if (!form.enabled && r && r.is_enabled && !confirm('Disable auto-follow for ' + nameOf(selected) + '?')) return;
    try {
      if (r) {
        rules[String(selected)] = await Fastt.patch('/admin/automation-rules/' + r.id,
          { name: r.name || 'Auto-follow / Auto-like', every_seconds, payload, is_enabled: form.enabled });
      } else {
        rules[String(selected)] = await Fastt.post('/admin/automation-rules', {
          account_id: selected, kind: 'auto_follow', name: 'Auto-follow / Auto-like',
          every_seconds, payload, is_enabled: form.enabled,
        });
      }
      Fastt.saved();
      form = ruleToForm(rules[String(selected)]);
      render(); renderEditor();
    } catch (e) { Fastt.oops(e); }
  }

  async function runNow() {
    const r = rules[String(selected)];
    if (!r) return;
    const dry = isDry(r);            // the SAVED payload decides, not the form
    const verb = actionOf(r) === 'ping' ? 'UNFOLLOW + RE-FOLLOW real fans'
      : actionOf(r) === 'follow' ? 'FOLLOW real fans' : 'LIKE real messages';
    if (!confirm(dry
      ? 'Run the LAST SAVED config now for ' + nameOf(selected) + '?\n\nDry-run is ON — it will only plan, nothing is sent.'
      : 'Run the LAST SAVED config now for ' + nameOf(selected) + '?\n\nDry-run is OFF — this will ' + verb + ' on OnlyFans.')) return;
    try {
      await Fastt.post('/admin/automation-rules/' + r.id + '/run-now', {});
      Fastt.saved(dry ? 'Dry run queued — plans only' : 'Running now — actions land within ~30s');
    } catch (e) { Fastt.oops(e); }
  }

  async function setEnabled(id, on) {
    const r = rules[String(id)];
    if (!r) { selected = String(id); form = null; await loadSmartLists(); render(); renderEditor(); openAdvanced();
      Fastt.toast('Configure it below first, then save with Enabled ticked.'); return; }
    if (on && !confirm('Enable auto-follow for ' + nameOf(id) + '?\n\n'
      + (ACTION_LABEL[actionOf(r)] || actionOf(r)) + ' · every ' + Math.round((r.every_seconds || 14400) / 60) + ' min\n'
      + 'Dry-run is ' + (isDry(r) ? 'ON — runs only PLAN.' : 'OFF — runs WILL act on OnlyFans.'))) return;
    if (!on && !confirm('Disable auto-follow for ' + nameOf(id) + '?')) return;
    try {
      rules[String(id)] = await Fastt.patch('/admin/automation-rules/' + r.id, { is_enabled: on });
      Fastt.saved(on ? 'Automation enabled' : 'Automation disabled');
      if (String(id) === String(selected)) form = ruleToForm(rules[String(id)]);
      render(); renderEditor();
    } catch (e) { Fastt.oops(e); }
  }

  /* ── load ─────────────────────────────────────────────────────────── */
  async function refresh() {
    // Both endpoints answer unscoped, so the whole roster costs 2 calls.
    const all = (await Fastt.get('/admin/automation-rules', null, { noAccount: true })).rules || [];
    rules = {};
    for (const r of all) if (r.kind === 'auto_follow') rules[String(r.account_id)] = r;
    const runs = (await Fastt.get('/admin/stats/automation-runs',
      { kind: 'auto_follow', limit: 500 }, { noAccount: true })).runs || [];
    runsBy = {};
    for (const r of runs) (runsBy[String(r.account_id)] = runsBy[String(r.account_id)] || []).push(r);
    // A creator that only exists as a rule owner still deserves a row.
    for (const id of Object.keys(rules)) {
      if (!roster.some((a) => String(a.id) === id)) roster.push({ id, nickname: id });
    }
    $('#af-fcreator').innerHTML = '<option value="">All creators (' + roster.length + ')</option>'
      + roster.map((a) => '<option value="' + esc(a.id) + '">' + esc(a.nickname || a.id) + '</option>').join('');
    await loadSmartLists();
    render(); renderEditor();
  }

  const advToggle = $('#af-adv-toggle');
  if (advToggle) advToggle.addEventListener('click', () => $('#af-adv').classList.toggle('open'));
  $('#af-fcreator').addEventListener('change', render);
  $('#af-fstate').addEventListener('change', render);
  $('#af-reset').addEventListener('click', () => {
    $('#af-fcreator').value = ''; $('#af-fstate').value = ''; render();
  });
  rowsEl.addEventListener('click', async (e) => {
    const t = e.target.closest('[data-tgl]');
    if (t) { await setEnabled(t.dataset.tgl, !t.classList.contains('on')); return; }
    const row = e.target.closest('[data-acct]');
    if (!row || String(row.dataset.acct) === String(selected)) return;
    selected = String(row.dataset.acct);
    form = null;
    await loadSmartLists();
    render(); renderEditor(); openAdvanced();
  });

  try {
    await refresh();
  } catch (e) {
    // Never fall back to the mockup row: show the failure where the data goes.
    rowsEl.innerHTML = '<div class="trow"><div class="td" style="flex:1;color:#e2a94e">'
      + 'Could not load the auto-follow state from the relay — no counts are shown because none were loaded.</div></div>';
    Fastt.oops(e);
  }
});
