/* fastt wiring — the same automation_rules rows as the Automations hub, in the
 * Infloww table skin, now with the numbers and controls the hub has.
 *
 *  • "Purchased" was a hardcoded '—' on every row. /admin/stats/per-automation
 *    carries revenue_cents per engine token; the rule KIND is mapped to that
 *    token (send_welcome→welcome, send_followup→followup, …).
 *  • "Sent" printed the first numeric key it found in last_run.stats, so
 *    scrape_chats showed messages_inserted (rows INGESTED) and push_to_sheets
 *    showed rows_written (spreadsheet rows) under a column labelled "Sent".
 *    Sent is now messages_sent from per-automation; the last-run counter moved
 *    to its own line and is named for what it actually counts.
 *  • A rule in ERROR rendered as "Off / every 60 s / — / —". last_run.status,
 *    error_text, run age, has_pending_job and next_due_at all show now.
 *  • Slugs got human labels + summaries from /admin/automation-kinds.
 *  • Status / Trigger / Date-created filters are real; row actions are real.
 */
Fastt.ready(async function () {
  var esc = Fastt.esc, $ = Fastt.$;
  var acct = Fastt.account();
  if (!acct) return;

  var out = await Fastt.get('/admin/automation-rules');
  var rules = (out.rules || []).filter(function (r) { return String(r.account_id) === String(acct); });

  // Named catalogue: label + summary + typed knobs for the editor.
  var kinds = {}, kindList = [];
  try {
    kindList = (await Fastt.get('/admin/automation-kinds')).kinds || [];
    kindList.forEach(function (k) { kinds[k.kind] = k; });
  } catch (e) { console.error(e); }

  // Per-automation money + sends. The engine TOKEN is not always the rule KIND.
  var perAuto = {};
  try {
    ((await Fastt.get('/admin/stats/per-automation')).rows || []).forEach(function (r) { perAuto[r.automation] = r; });
  } catch (e) { console.error(e); }

  // rule kind → engine token in /admin/stats/per-automation.
  var TOKEN = {
    send_welcome: 'welcome', send_followup: 'followup', ai_chatter: 'ai_chatter',
    autoreply: 'autoreply', welcome_chatter_for_info: 'welcome_chatter_for_info',
    of_ai_chat: 'of_ai_chat', deep_convo: 'deep_convo',   // both retired/renamed 2026-08-19
    reply_mass_funnel: 'reply_mass_funnel', ppv_send: 'ppv_send', nudge_online: 'nudge_online',
    gen_info: 'gen_info', tip_reward: 'tip_reward', reengage_buyers: 'reengage_buyers',
  };
  // Kinds that never send a fan message and have no money of their own — an
  // honest "not tracked", never a fabricated 0.
  var NOT_A_SENDER = {
    apply_profiles: 'writes nicknames/notes to OnlyFans — sends nothing',
    push_to_sheets: 'writes spreadsheet rows — sends nothing',
    scrape_chats: 'ingests chat history — sends nothing',
    auto_posts: 'publishes a feed post — not a DM',
    auto_stories: 'posts stories — not a DM',
    unsend_messages: 'removes messages — sends nothing',
    process_old_fans: 'moves fans into the AI funnel — sends nothing itself',
    mass_nudge: 'broadcast sends are booked under nudge_online / ppv_send, not this kind',
    mass_premade: 'broadcast sends are booked on the mass run, not this kind',
    send_mass_message: 'broadcast sends are booked on the mass run, not this kind',
    online_blast: 'broadcast sends are booked under nudge_online, not this kind',
  };
  // last_run counter that actually matters, per kind, named for what it counts.
  var LAST_RUN = {
    scrape_chats: ['messages_inserted', 'messages ingested'],
    push_to_sheets: ['rows_written', 'rows written to the sheet'],
    apply_profiles: ['applied', 'profiles applied'],
    auto_posts: ['posted', 'feed posts published'],
    auto_stories: ['posted', 'stories posted'],
    send_welcome: ['welcomes_sent', 'welcomes sent'],
    send_followup: ['followups_sent', 'follow-ups sent'],
    autoreply: ['sent', 'replies sent'],
    deep_convo: ['sends', 'sends'],
    gen_info: ['generated', 'profiles generated'],
    nudge_online: ['enqueued', 'nudges enqueued'],
    mass_premade: ['sent', 'sent'],
    unsend_messages: ['unsent', 'messages unsent'],
    reply_mass_funnel: ['advanced', 'threads advanced'],
    ppv_send: ['cells_sent', 'PPV cells sent'],
    welcome_chatter_for_info: [null, null],
    of_ai_chat: [null, null],        // renamed 2026-08-19 — legacy rows still render
  };
  // How many rules on this account share a token (ppv_send has 3) — so the
  // shared aggregate is never read as this one rule's own number.
  var tokenRules = {};
  rules.forEach(function (r) { var t = TOKEN[r.kind]; if (t) tokenRules[t] = (tokenRules[t] || 0) + 1; });

  var MASS_KINDS = { mass_nudge: 1, mass_premade: 1, send_mass_message: 1, online_blast: 1, nudge_online: 1, unsend_messages: 1 };

  function labelOf(r) { return (kinds[r.kind] && kinds[r.kind].label) || r.kind; }
  function fmtCad(r) {
    var t = r.trigger || {};
    if (t.daily_at) return 'daily at ' + t.daily_at + (t.max_runs ? ' · max ' + t.max_runs + '/day' : '');
    var s = Number(t.every_seconds || r.every_seconds || 0);
    if (!s) return 'manual';
    if (s < 120) return 'every ' + s + ' s';
    if (s < 7200) return 'every ' + Math.round(s / 60) + ' min';
    if (s < 172800) return 'every ' + Math.round(s / 360) / 10 + ' h';
    return 'every ' + Math.round(s / 8640) / 10 + ' d';
  }
  function lastRunStat(r) {
    var st = r.last_run && r.last_run.stats;
    if (!st) return '';
    var pair = LAST_RUN[r.kind];
    if (pair && pair[0] && typeof st[pair[0]] === 'number') return Fastt.fmtInt(st[pair[0]]) + ' ' + pair[1] + ' last run';
    if (pair && pair[0] === null) return '';
    // No curated key: name the counter by its own key rather than mislabel it.
    for (var k in st) if (typeof st[k] === 'number' && k !== 'duration_ms') return Fastt.fmtInt(st[k]) + ' ' + k + ' last run';
    return '';
  }
  function sentCell(r) {
    var tok = TOKEN[r.kind], row = tok && perAuto[tok];
    var head;
    if (row) {
      head = '<span style="font-weight:500">' + Fastt.fmtInt(row.messages_sent) + '</span>'
        + (tokenRules[tok] > 1 ? '<span class="sm-sub">all ' + tokenRules[tok] + ' ' + esc(r.kind) + ' rules</span>' : '');
    } else if (NOT_A_SENDER[r.kind]) {
      head = '<span class="sm-tag mut" title="' + esc(NOT_A_SENDER[r.kind]) + '">not a sender</span>';
    } else {
      head = '<span class="sm-tag mut" title="no row for &quot;' + esc(r.kind) + '&quot; in /admin/stats/per-automation">not tracked</span>';
    }
    var lr = lastRunStat(r);
    return head + (lr ? '<div class="sm-sub">' + esc(lr) + '</div>' : '');
  }
  function moneyCell(r) {
    var tok = TOKEN[r.kind], row = tok && perAuto[tok];
    if (!row) {
      return '<span class="sm-tag mut" title="' + esc(NOT_A_SENDER[r.kind]
        || 'no revenue row for "' + r.kind + '" in /admin/stats/per-automation') + '">not tracked</span>';
    }
    var cents = row.revenue_cents || 0;
    return '<span style="color:' + (cents ? '#67d1ae' : '#8a8a8a') + ';font-weight:' + (cents ? 600 : 400) + '">'
      + Fastt.fmtCents(cents) + '</span>'
      + (cents && tokenRules[tok] > 1 ? '<span class="sm-sub">all ' + tokenRules[tok] + ' rules</span>' : '');
  }
  function statusCell(r) {
    var lr = r.last_run || null;
    var bits = ['<span class="' + (r.is_enabled ? 'pill-in' : 'pill-ex') + '">' + (r.is_enabled ? 'Running' : 'Off') + '</span>'];
    if (!lr || !lr.started_at) bits.push('<span class="sm-tag mut">never run</span>');
    else bits.push('<span class="sm-tag ' + (lr.status === 'ok' ? 'ok' : 'err') + '">' + esc(lr.status || '?') + '</span>');
    if (r.has_pending_job) bits.push('<span class="sm-tag blue">queued</span>');
    var html = '<div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center">' + bits.join('') + '</div>';
    if (lr && lr.started_at) html += '<div class="sm-sub">ran ' + esc(Fastt.fmtAgo(lr.started_at)) + '</div>';
    if (lr && lr.error_text) {
      var full = String(lr.error_text);
      html += '<div class="sm-sub" style="color:#e05b5b" title="' + esc(full) + '">'
        + esc(full.length > 96 ? full.slice(0, 96) + '…' : full) + '</div>';
    }
    return html;
  }
  function triggerCell(r) {
    var html = '<div>' + esc(fmtCad(r)) + '</div>';
    if (r.quiet_hours) html += '<div class="sm-sub">quiet ' + esc(String(r.quiet_hours[0])) + ':00–' + esc(String(r.quiet_hours[1])) + ':00</div>';
    if (r.next_due_at) html += '<div class="sm-sub">next ' + esc(Fastt.fmtDate(r.next_due_at)) + '</div>';
    else if (r.is_enabled) html += '<div class="sm-sub">due on the next executor tick</div>';
    return html;
  }

  var gbody = document.querySelector('.gbody');
  var fbar = gbody.querySelector('.fbar');
  var thead = gbody.querySelector('.thead');
  var empty = gbody.querySelector('.empty');
  var wrap = document.createElement('div');
  wrap.id = 'sm-rows';
  thead.insertAdjacentElement('afterend', wrap);

  // Live summary strip — leads the page with the state of THIS tab at a glance
  // (the real app shows only a bare "X/Y on"). Computed from data already loaded.
  var summary = document.createElement('div');
  summary.id = 'sm-summary';
  gbody.insertBefore(summary, fbar);

  function tabSet() {
    return rules.filter(function (r) { var m = !!MASS_KINDS[r.kind]; return mode === 'auto' ? !m : m; });
  }
  function renderSummary() {
    var set = tabSet();
    var running = 0, errored = 0, never = 0, queued = 0;
    var seen = {}, sent = 0, rev = 0, tracked = false;
    set.forEach(function (r) {
      if (r.is_enabled) running++;
      var lr = r.last_run;
      if (!lr || !lr.started_at) never++;
      else if (lr.status && lr.status !== 'ok') errored++;
      if (r.has_pending_job) queued++;
      var tok = TOKEN[r.kind];
      if (tok && perAuto[tok] && !seen[tok]) {          // dedupe shared tokens (ppv_send ×3)
        seen[tok] = 1; tracked = true;
        sent += Number(perAuto[tok].messages_sent || 0);
        rev += Number(perAuto[tok].revenue_cents || 0);
      }
    });
    function tile(v, l, color, title) {
      return '<div class="sm-stat"' + (title ? ' title="' + esc(title) + '"' : '') + '>'
        + '<div class="sm-stat-v"' + (color ? ' style="color:' + color + '"' : '') + '>' + v + '</div>'
        + '<div class="sm-stat-l">' + esc(l) + '</div></div>';
    }
    var noun = mode === 'auto' ? 'automations' : 'broadcast rules';
    summary.innerHTML =
      tile('<span style="color:#67d1ae">' + running + '</span><span style="color:#5a5a5a">/' + set.length + '</span>',
           'Running · ' + noun, null) +
      tile(String(errored), 'Last run errored', errored ? '#e05b5b' : '#8a8a8a',
           errored ? 'Rules whose most recent run failed — open History on the row for the error.' : '') +
      tile(String(never), 'Never run', '#8a8a8a') +
      tile(String(queued), 'Job queued', queued ? '#8fa6ff' : '#8a8a8a') +
      (tracked
        ? tile(Fastt.fmtInt(sent), 'Sent (attributed)', '#fff',
               'Total messages attributed to these rules in /admin/stats/per-automation.')
          + tile(Fastt.fmtCents(rev), 'Revenue (attributed)', rev ? '#67d1ae' : '#8a8a8a',
               'Total revenue attributed to these rules. Shared tokens counted once.')
        : '<div class="sm-stat"><div class="sm-stat-v" style="color:#8a8a8a;font-size:15px">not a sender</div>'
          + '<div class="sm-stat-l">no attributed sends on this tab</div></div>');
  }

  var mode = 'auto';
  var query = '';

  function passesFilters(r) {
    var st = $('#sm-fstatus').value, tr = $('#sm-ftrigger').value;
    var lr = r.last_run || null;
    if (st === 'on' && !r.is_enabled) return false;
    if (st === 'off' && r.is_enabled) return false;
    if (st === 'ok' && !(lr && lr.status === 'ok')) return false;
    if (st === 'error' && !(lr && lr.status && lr.status !== 'ok')) return false;
    if (st === 'never' && lr && lr.started_at) return false;
    if (st === 'queued' && !r.has_pending_job) return false;
    if (tr) {
      if (tr === '@daily') { if (!(r.trigger || {}).daily_at) return false; }
      else if (tr === '@fast') { if (!((r.every_seconds || 0) > 0 && r.every_seconds <= 300)) return false; }
      else if (tr === '@slow') { if (!(r.every_seconds > 300)) return false; }
      else if (r.kind !== tr) return false;
    }
    var from = $('[data-sm="cfrom"]').value, to = $('[data-sm="cto"]').value;
    if (from || to) {
      var d = Fastt.parseUtc(r.created_at);           // tz-naive UTC on the wire
      if (!d) return false;
      if (from && d < new Date(from + 'T00:00:00')) return false;
      if (to && d > new Date(to + 'T23:59:59')) return false;
    }
    return true;
  }

  function render() {
    renderSummary();
    var list = rules.filter(function (r) {
      var isMass = !!MASS_KINDS[r.kind];
      if (mode === 'auto' ? isMass : !isMass) return false;
      if (query && (r.name + ' ' + r.kind + ' ' + labelOf(r)).toLowerCase().indexOf(query) === -1) return false;
      return passesFilters(r);
    });
    if (!list.length) { wrap.innerHTML = ''; empty.style.display = ''; return; }
    empty.style.display = 'none';
    wrap.innerHTML = list.map(function (r) {
      var k = kinds[r.kind] || {};
      return '<div class="trow" data-rule="' + r.id + '">' +
        '<div class="td" style="flex:1;min-width:0;display:flex;flex-direction:column;align-items:flex-start;gap:4px;padding-top:14px;padding-bottom:14px">' +
          '<span style="font-weight:500">' + esc(labelOf(r)) + '</span>' +
          '<span style="font-size:12.5px;color:#8a8a8a">' + esc(r.name) +
            ' · <span style="font-family:ui-monospace,Menlo,monospace">' + esc(r.kind) + '</span>' +
            (k.surface ? ' · ' + esc(k.surface) : '') + '</span>' +
          (k.summary ? '<span class="sm-sub" style="max-width:100%">' + esc(String(k.summary).slice(0, 150)) + (String(k.summary).length > 150 ? '…' : '') + '</span>' : '') +
          '<span class="sm-sub">created ' + esc(Fastt.fmtDate(r.created_at)) + '</span></div>' +
        '<div class="td" style="width:200px;flex:0 0 auto;padding-top:14px;padding-bottom:14px">' + statusCell(r) + '</div>' +
        '<div class="td" style="width:200px;flex:0 0 auto;padding-top:14px;padding-bottom:14px">' + triggerCell(r) + '</div>' +
        '<div class="td" style="width:130px;flex:0 0 auto;padding-top:14px;padding-bottom:14px">' + sentCell(r) + '</div>' +
        '<div class="td" style="width:120px;flex:0 0 auto;padding-top:14px;padding-bottom:14px">' + moneyCell(r) + '</div>' +
        '<div class="td" style="width:160px;flex:0 0 auto;margin-left:auto;display:flex;flex-wrap:wrap;gap:6px;padding-top:14px;padding-bottom:14px">' +
          '<button class="sm-act" data-act="toggle">' + (r.is_enabled ? 'Disable' : 'Enable') + '</button>' +
          '<button class="sm-act" data-act="run">Run now</button>' +
          '<button class="sm-act" data-act="history">History</button>' +
          '<button class="sm-act" data-act="edit">Edit</button>' +
          '<button class="sm-act danger" data-act="del">Delete</button>' +
        '</div>' +
      '</div>';
    }).join('');
  }

  /* ── row actions ─────────────────────────────────────────────────── */
  async function reload() {
    var o = await Fastt.get('/admin/automation-rules');
    rules = (o.rules || []).filter(function (r) { return String(r.account_id) === String(acct); });
    render();
  }
  wrap.addEventListener('click', async function (e) {
    var btn = e.target.closest('[data-act]');
    if (!btn) return;
    var id = Number(btn.closest('.trow').dataset.rule);
    var r = rules.find(function (x) { return x.id === id; });
    if (!r) return;
    var act = btn.dataset.act;
    try {
      if (act === 'toggle') {
        var on = !r.is_enabled;
        if (on && !confirm('Enable “' + labelOf(r) + '” (' + r.kind + ')?\n\nThis automation will start running ' +
            fmtCad(r) + '. Sender kinds message real fans.')) return;
        if (!on && !confirm('Disable “' + labelOf(r) + '”?')) return;
        await Fastt.patch('/admin/automation-rules/' + id, { is_enabled: on });
        Fastt.saved(on ? 'Enabled' : 'Disabled');
        await reload();
      } else if (act === 'run') {
        if (!confirm('Run “' + labelOf(r) + '” (' + r.kind + ') RIGHT NOW?\n\n' +
            'It runs with its SAVED payload. If this is a sender kind it will message real fans on OnlyFans.')) return;
        var res = await Fastt.post('/admin/automation-rules/' + id + '/run-now', {});
        Fastt.saved('Queued job #' + (res && res.enqueued_job_id));
        setTimeout(reload, 2500);
      } else if (act === 'del') {
        if (!confirm('Delete the rule “' + r.name + '” (' + r.kind + ')? This removes the schedule, not the tab config.')) return;
        await Fastt.del('/admin/automation-rules/' + id);
        Fastt.saved('Deleted');
        await reload();
      } else if (act === 'edit') {
        openEditor(r);
      } else if (act === 'history') {
        openHistory(r);
      }
    } catch (err) { Fastt.oops(err); }
  });

  /* ── run history (read-only), matches the app's AutomationRunsCard ──── */
  async function openHistory(r) {
    var inner = '<div data-hist style="min-height:80px;display:flex;align-items:center;'
      + 'justify-content:center;color:#8a8a8a;font-size:13px">Loading run history…</div>';
    var back = document.createElement('div');
    back.className = 'ft-modal-back';
    back.innerHTML = '<div class="ft-modal" style="width:560px;max-height:82vh;overflow:auto">' +
      '<h3>Run history — ' + esc(labelOf(r)) + ' <span style="font-family:ui-monospace,Menlo,monospace;'
        + 'font-weight:400;color:#8a8a8a;font-size:12px">' + esc(r.kind) + '</span></h3>' +
      inner + '<button data-close style="background:#333;margin-top:6px">Close</button></div>';
    document.body.appendChild(back);
    back.addEventListener('click', function (e) { if (e.target === back) back.remove(); });
    Fastt.$('[data-close]', back).addEventListener('click', function () { back.remove(); });
    var holder = Fastt.$('[data-hist]', back);
    try {
      var runs = ((await Fastt.get('/admin/stats/automation-runs', { limit: 200 })).runs || [])
        .filter(function (x) { return x.kind === r.kind && String(x.account_id) === String(acct); })
        .slice(0, 40);
      if (!runs.length) {
        holder.innerHTML = '<div style="text-align:center;color:#8a8a8a;font-size:13px;padding:26px 0">'
          + 'No recorded runs for this automation yet.</div>';
        return;
      }
      holder.style.cssText = '';
      holder.innerHTML = runs.map(function (x) {
        var dur = null;
        if (x.started_at && x.completed_at) {
          var ms = Fastt.parseUtc(x.completed_at) - Fastt.parseUtc(x.started_at);
          if (isFinite(ms) && ms >= 0) dur = ms < 1000 ? ms + 'ms' : (ms / 1000).toFixed(ms < 10000 ? 1 : 0) + 's';
        }
        var stats = '';
        if (x.stats_json) {
          try {
            var o = JSON.parse(x.stats_json);
            stats = Object.keys(o).filter(function (k) { return k !== 'duration_ms' && o[k] !== null && o[k] !== ''; })
              .slice(0, 6).map(function (k) {
                return '<span style="color:#8a8a8a">' + esc(k) + '</span> ' + esc(String(o[k]));
              }).join(' · ');
          } catch (e) {}
        }
        var ok = x.status === 'ok';
        return '<div style="padding:11px 0;border-bottom:1px solid #1c1c1c">'
          + '<div style="display:flex;align-items:center;gap:9px">'
          + '<span class="sm-tag ' + (ok ? 'ok' : (x.status === 'running' ? 'blue' : 'err')) + '">' + esc(x.status || '?') + '</span>'
          + '<span style="font-size:12.5px;color:#cfcfcf">' + esc(Fastt.fmtDate(x.started_at)) + '</span>'
          + '<span style="font-size:12px;color:#8a8a8a">' + esc(Fastt.fmtAgo(x.started_at)) + '</span>'
          + (dur ? '<span style="margin-left:auto;font-size:12px;color:#8a8a8a">' + esc(dur) + '</span>' : '')
          + '</div>'
          + (stats ? '<div style="font-size:12px;color:#e0e0e0;margin-top:6px;line-height:1.5">' + stats + '</div>' : '')
          + (x.error_text ? '<div style="font-size:12px;color:#e05b5b;margin-top:6px;white-space:pre-wrap;'
              + 'word-break:break-word">' + esc(String(x.error_text).slice(0, 400)) + '</div>' : '')
          + '</div>';
      }).join('');
    } catch (e) {
      holder.style.cssText = '';
      holder.innerHTML = '<div style="color:#e05b5b;font-size:13px;padding:20px 0">'
        + esc((e && e.message) || 'Failed to load run history') + '</div>';
    }
  }

  /* ── rule editor / creator, driven by the kind catalogue's knobs ──── */
  function knobField(kn, val) {
    var key = esc(kn.key), hint = esc(kn.hint || '');
    if (kn.type === 'bool') {
      return '<label class="sm-chk"><input type="checkbox" data-k="' + key + '" data-t="bool"' + (val ? ' checked' : '') + '>' +
        key + (hint ? ' <span class="sm-hint">— ' + hint + '</span>' : '') + '</label>';
    }
    var input;
    if (kn.enum) {
      input = '<select data-k="' + key + '" data-t="str"><option value="">— default —</option>' +
        kn.enum.map(function (o) { return '<option value="' + esc(o) + '"' + (String(val) === String(o) ? ' selected' : '') + '>' + esc(o) + '</option>'; }).join('') + '</select>';
    } else if (kn.type === 'int') {
      input = '<input type="number" data-k="' + key + '" data-t="int"' + (kn.min != null ? ' min="' + kn.min + '"' : '') +
        (kn.max != null ? ' max="' + kn.max + '"' : '') + ' value="' + (val == null ? '' : esc(val)) + '">';
    } else if (kn.type === 'ids') {
      input = '<input type="text" data-k="' + key + '" data-t="ids" value="' + esc(Array.isArray(val) ? val.join(', ') : (val == null ? '' : val)) + '" placeholder="123, 456">';
    } else if (kn.type === 'json') {
      input = '<textarea rows="3" data-k="' + key + '" data-t="json">' + esc(val == null ? '' : JSON.stringify(val)) + '</textarea>';
    } else {
      input = '<input type="text" data-k="' + key + '" data-t="str" value="' + (val == null ? '' : esc(val)) + '">';
    }
    return '<label class="sm-f"><span>' + key + '</span>' + input + (hint ? '<span class="sm-hint">' + hint + '</span>' : '') + '</label>';
  }
  function readKnobs(root, base) {
    var payload = {};
    Object.keys(base || {}).forEach(function (k) { payload[k] = base[k]; });   // keep unknown keys
    Fastt.$$('[data-k]', root).forEach(function (el) {
      var k = el.dataset.k, t = el.dataset.t, v;
      if (t === 'bool') { if (!el.checked) { delete payload[k]; return; } v = true; }
      else if (el.value === '') { delete payload[k]; return; }
      else if (t === 'int') v = Number(el.value);
      else if (t === 'ids') v = el.value.split(/[,\s]+/).map(Number).filter(function (n) { return n > 0; });
      else if (t === 'json') { try { v = JSON.parse(el.value); } catch (e) { throw new Error('bad JSON in ' + k); } }
      else v = el.value;
      payload[k] = v;
    });
    return payload;
  }
  function modal(title, inner, onSave, saveLabel) {
    var back = document.createElement('div');
    back.className = 'ft-modal-back';
    back.innerHTML = '<div class="ft-modal" style="width:460px;max-height:82vh;overflow:auto">' +
      '<h3>' + title + '</h3>' + inner +
      '<div class="ft-err" data-err></div>' +
      '<button data-save>' + (saveLabel || 'Save') + '</button></div>';
    document.body.appendChild(back);
    back.addEventListener('click', function (e) { if (e.target === back) back.remove(); });
    Fastt.$('[data-save]', back).addEventListener('click', async function () {
      var errEl = Fastt.$('[data-err]', back);
      try { await onSave(back); back.remove(); }
      catch (e) {
        errEl.style.display = 'block';
        errEl.textContent = (e && e.body && e.body.detail) ? String(e.body.detail) : String((e && e.message) || e);
      }
    });
    return back;
  }
  function openEditor(r) {
    var k = kinds[r.kind] || {};
    var knobs = k.knobs || [];
    var known = {}; knobs.forEach(function (x) { known[x.key] = 1; });
    var extra = {};
    Object.keys(r.payload || {}).forEach(function (kk) { if (!known[kk]) extra[kk] = r.payload[kk]; });
    var inner =
      '<label class="sm-f"><span>Name</span><input type="text" data-name value="' + esc(r.name) + '"></label>' +
      '<label class="sm-f"><span>Run every (seconds)</span><input type="number" min="30" max="2592000" data-every value="' + (r.every_seconds || 0) + '"></label>' +
      (knobs.length ? '<div class="sm-hint" style="margin:10px 0 6px">Payload knobs for <b>' + esc(r.kind) + '</b> — leave blank for the engine default.</div>' : '') +
      knobs.map(function (kn) { return knobField(kn, (r.payload || {})[kn.key]); }).join('') +
      '<label class="sm-f"><span>Other payload keys (raw JSON)</span><textarea rows="3" data-extra>' + esc(JSON.stringify(extra)) + '</textarea>' +
      '<span class="sm-hint">Anything not covered by a knob above. Sent verbatim.</span></label>' +
      '<div class="sm-hint" style="margin-bottom:10px">Enabling is a separate, confirmed action on the row — saving here never flips it.</div>';
    modal('Edit rule #' + r.id, inner, async function (back) {
      var extraObj = {};
      var raw = Fastt.$('[data-extra]', back).value.trim();
      if (raw) extraObj = JSON.parse(raw);
      var payload = readKnobs(back, extraObj);
      await Fastt.patch('/admin/automation-rules/' + r.id, {
        name: Fastt.$('[data-name]', back).value.trim() || r.name,
        every_seconds: Number(Fastt.$('[data-every]', back).value) || r.every_seconds,
        payload: payload,
      });
      Fastt.saved();
      await reload();
    }, 'Save changes');
  }
  function openCreator() {
    var opts = kindList.slice().sort(function (a, b) { return (a.label || a.kind).localeCompare(b.label || b.kind); });
    var inner =
      '<label class="sm-f"><span>Automation</span><select data-kind>' +
        opts.map(function (k) { return '<option value="' + esc(k.kind) + '">' + esc(k.label || k.kind) + ' — ' + esc(k.kind) + '</option>'; }).join('') +
      '</select></label>' +
      '<label class="sm-f"><span>Name</span><input type="text" data-name placeholder="(defaults to the kind)"></label>' +
      '<label class="sm-f"><span>Run every (seconds)</span><input type="number" min="30" max="2592000" data-every value="300"></label>' +
      '<div data-knobs></div>' +
      '<div class="sm-hint" style="margin-bottom:10px">Created <b>disabled</b>. Turn it on from the row when you are ready — that is a separate, confirmed action.</div>';
    var back = modal('New automation', inner, async function (b) {
      var kind = Fastt.$('[data-kind]', b).value;
      var payload = readKnobs(Fastt.$('[data-knobs]', b), {});
      await Fastt.post('/admin/automation-rules', {
        account_id: acct, kind: kind,
        name: Fastt.$('[data-name]', b).value.trim() || kind,
        every_seconds: Number(Fastt.$('[data-every]', b).value) || 300,
        payload: payload, is_enabled: false,
      });
      Fastt.saved('Created (disabled)');
      await reload();
    }, 'Create automation');
    var kindSel = Fastt.$('[data-kind]', back), holder = Fastt.$('[data-knobs]', back), everyEl = Fastt.$('[data-every]', back);
    function paintKnobs() {
      var k = kinds[kindSel.value] || {};
      if (k.cadence_default_s) everyEl.value = k.cadence_default_s;
      holder.innerHTML = (k.summary ? '<div class="sm-hint" style="margin:0 0 10px">' + esc(k.summary) + '</div>' : '')
        + (k.knobs || []).map(function (kn) { return knobField(kn, undefined); }).join('');
    }
    kindSel.addEventListener('change', paintKnobs);
    paintKnobs();
  }

  // tabs are a real split: recurring per-fan automations vs mass/blast kinds
  var tabs = document.querySelectorAll('.gtabs .gtab');
  if (tabs[0]) tabs[0].addEventListener('click', function () { mode = 'auto'; render(); });
  if (tabs[1]) tabs[1].addEventListener('click', function () { mode = 'mass'; render(); });

  // Trigger filter options: the real kind taxonomy plus the cadence shapes.
  var present = {};
  rules.forEach(function (r) { present[r.kind] = 1; });
  $('#sm-ftrigger').innerHTML = '<option value="">All</option>'
    + '<option value="@fast">≤ 5 min loop</option><option value="@slow">slower than 5 min</option>'
    + '<option value="@daily">daily at a fixed time</option>'
    + Object.keys(present).sort().map(function (k) {
        return '<option value="' + esc(k) + '">' + esc((kinds[k] && kinds[k].label) || k) + '</option>';
      }).join('');

  // search filters the live rows client-side
  var input = document.querySelector('.fbar .inp input');
  if (input) input.addEventListener('input', Fastt.debounce(function () {
    query = input.value.trim().toLowerCase(); render();
  }, 150));
  ['#sm-fstatus', '#sm-ftrigger', '[data-sm="cfrom"]', '[data-sm="cto"]'].forEach(function (s) {
    $(s).addEventListener('change', render);
  });
  $('#sm-search').addEventListener('click', function () {
    if (input) query = input.value.trim().toLowerCase();
    render();
  });
  $('#sm-reset').addEventListener('click', function () {
    if (input) input.value = '';
    query = '';
    $('#sm-fstatus').value = ''; $('#sm-ftrigger').value = '';
    $('[data-sm="cfrom"]').value = ''; $('[data-sm="cto"]').value = '';
    render();
  });

  Fastt.liveBadge(document.querySelector('.gtitle'));
  var newBtn = document.querySelector('.hact .btn-blue');
  if (newBtn) newBtn.addEventListener('click', openCreator);

  // Refresh — re-pull rules AND per-automation stats (the app's Refresh button).
  var hact = document.querySelector('.hact');
  if (hact && newBtn) {
    var refreshBtn = document.createElement('button');
    refreshBtn.className = 'sm-refresh';
    refreshBtn.innerHTML = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
      + 'stroke-width="2"><path d="M20 11a8 8 0 1 0-2.3 5.6M20 5v6h-6" stroke-linecap="round" '
      + 'stroke-linejoin="round"/></svg>Refresh';
    hact.insertBefore(refreshBtn, newBtn);
    refreshBtn.addEventListener('click', async function () {
      if (refreshBtn.classList.contains('spin')) return;
      refreshBtn.classList.add('spin');
      try {
        var o = await Fastt.get('/admin/automation-rules');
        rules = (o.rules || []).filter(function (r) { return String(r.account_id) === String(acct); });
        perAuto = {};
        ((await Fastt.get('/admin/stats/per-automation')).rows || []).forEach(function (r) { perAuto[r.automation] = r; });
        tokenRules = {};
        rules.forEach(function (r) { var t = TOKEN[r.kind]; if (t) tokenRules[t] = (tokenRules[t] || 0) + 1; });
        render();
        Fastt.saved('Refreshed');
      } catch (e) { Fastt.oops(e); }
      finally { refreshBtn.classList.remove('spin'); }
    });
  }
  // "Sent to" (audience) and "Created by" (rule authorship) have no field on
  // automation_rules and nothing on the wire carries them — badge, don't fake.
  Fastt.$$('.fbar .fld').forEach(function (f) {
    var lbl = (f.querySelector('label') || {}).textContent || '';
    if (/Sent to|Created by|Start date/.test(lbl)) {
      Fastt.staticBadge(f, 'NO BACKEND');
      f.title = 'automation_rules stores no audience, no author and no schedule start — '
        + 'nothing on the relay can fill these three. "Date created" next to them is real.';
    }
  });
  var csel = document.querySelector('.csel');
  var row = Fastt.accountRow();
  if (csel && (row || acct)) csel.childNodes.forEach(function (n) {
    if (n.nodeType === 3 && n.textContent.trim()) n.textContent = row ? (row.nickname || row.id) : acct;
  });

  render();
});
