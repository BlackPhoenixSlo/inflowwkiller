/* fastt wiring — the automations switchboard.
   Cards map to automation_rules rows via data-kind. Three cards are NOT rules:
   /admin/automation-kinds marks tip_reward and make_right surface:"internal", so
   their real switch is a config blob (tip-reward-config / make-right-config), and
   Reply Timing is rhythm_enabled inside ai-chatter-config. Card copy that quotes a
   number (whale gate, PPV band, tip ladder, run caps) is rewritten from the live
   config so it can never quote a house default the account does not run.
   Sources: /admin/automation-rules · /admin/automation-kinds · /admin/ai-chatter-config
            /admin/ppv-library-config · /admin/tip-reward-config · /admin/make-right-config
            /admin/stats/automation-runs · /admin/session/status */
Fastt.ready(async function () {
  var esc = Fastt.esc;
  var CLOCK = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><path d="M12 7.5V12l3 2" stroke-linecap="round" stroke-linejoin="round"/></svg>';

  var acct = Fastt.account();
  var out = await Fastt.get('/admin/automation-rules');
  var rules = (out.rules || []).filter(function (r) { return String(r.account_id) === String(acct); });
  var byKind = {};
  rules.forEach(function (r) { (byKind[r.kind] = byKind[r.kind] || []).push(r); });

  function tryGet(path, params) {
    return Fastt.get(path, params).then(function (v) { return v; }, function (e) {
      console.warn(path + ' unavailable', e); return null;
    });
  }
  var loaded = await Promise.all([
    tryGet('/admin/ai-chatter-config'), tryGet('/admin/session/status'),
    tryGet('/admin/automation-kinds'), tryGet('/admin/ppv-library-config'),
    tryGet('/admin/tip-reward-config'), tryGet('/admin/make-right-config')
  ]);
  var chatter = loaded[0], session = loaded[1], kindsOut = loaded[2];
  var ppvCfg = loaded[3], tipCfg = loaded[4], mrCfg = loaded[5];

  var KINDS = {};
  ((kindsOut && kindsOut.kinds) || []).forEach(function (k) { KINDS[k.kind] = k; });

  /** merged view of a {config, defaults} blob — the shape every /admin/*-config uses */
  function merged(blob) {
    if (!blob) return null;
    var m = {};
    Object.assign(m, blob.defaults || {}, blob.config || {});
    return m;
  }
  var TIP = merged(tipCfg), MR = merged(mrCfg), PPV = merged(ppvCfg), CH = merged(chatter);

  function fmtCad(r) {
    var t = r.trigger || {};
    if (t.daily_at) return 'daily at ' + t.daily_at + (t.max_runs ? ' · max ' + t.max_runs + '/day' : '');
    var s = Number(t.every_seconds || r.every_seconds || 0);
    return cadWords(s);
  }
  function cadWords(s) {
    s = Number(s || 0);
    if (!s) return 'manual';
    if (s < 120) return 'every ' + s + ' s';
    if (s < 7200) return 'every ' + Math.round(s / 60) + ' min';
    if (s < 172800) return 'every ' + Math.round(s / 360) / 10 + ' h';
    return 'every ' + Math.round(s / 8640) / 10 + ' d';
  }
  /** one chip for a kind that owns several rows (ppv_send, unsend_messages):
   *  a single row's cadence would silently stand in for all of them. */
  function cadSummary(rows) {
    if (rows.length === 1) return fmtCad(rows[0]);
    var secs = rows.map(function (r) { return Number((r.trigger || {}).every_seconds || r.every_seconds || 0); })
      .filter(function (s) { return s > 0; });
    if (!secs.length) return fmtCad(rows[0]);
    var lo = Math.min.apply(null, secs), hi = Math.max.apply(null, secs);
    return lo === hi ? cadWords(lo) : cadWords(lo) + ' – ' + cadWords(hi).replace('every ', '');
  }
  function trgRaw(r) {
    var t = r.trigger || {};
    if (t.daily_at) return 'daily_at ' + t.daily_at + (t.max_runs ? ' · max_runs ' + t.max_runs : '');
    if (t.every_seconds) return 'every_seconds ' + t.every_seconds;
    return JSON.stringify(t);
  }
  function setCad(card, txt) {
    var chip = card.querySelector('.tc-cad');
    if (!chip) {
      chip = document.createElement('span');
      chip.className = 'tc-cad';
      card.querySelector('.fx-tc-body').appendChild(chip);
    }
    chip.innerHTML = CLOCK + esc(txt);
  }
  function setState(card, on, label) {
    card.classList.toggle('on', on);
    var sw = card.querySelector('.fx-switch');
    if (sw) sw.classList.toggle('on', on);
    var st = card.querySelector('.fx-tc-state');
    if (st) st.textContent = label || (on ? 'Running' : 'Off');
  }
  function setDesc(card, txt) {
    var d = card.querySelector('.fx-tc-desc');
    if (d && txt) d.textContent = txt;
  }
  /** the per-card last-run strip the real app shows on every row */
  function setRun(card, rows) {
    var host = card.querySelector('.tc-run');
    if (!host) {
      host = document.createElement('div');
      host.className = 'tc-run';
      card.querySelector('.fx-tc-body').appendChild(host);
    }
    if (!rows || !rows.length) { host.innerHTML = ''; return; }
    // several rows of one kind (ppv_send, unsend_messages): show the newest run
    var best = null, pend = false, next = null;
    rows.forEach(function (r) {
      if (r.has_pending_job) pend = true;
      if (r.next_due_at && (!next || r.next_due_at < next)) next = r.next_due_at;
      var lr = r.last_run;
      if (!lr) return;
      var ts = lr.completed_at || lr.started_at || '';
      if (!best || ts > (best.last_run.completed_at || best.last_run.started_at || '')) best = r;
    });
    var bits = '';
    if (best) {
      var lr = best.last_run;
      var status = lr.status || 'unknown';
      bits += '<span class="st ' + esc(status) + '">' + esc(status === 'running' ? 'running…' : status) + '</span>'
        + '<span>' + esc(Fastt.fmtAgo(lr.completed_at || lr.started_at)) + '</span>';
      var st = lr.stats || {};
      var nums = Object.keys(st).filter(function (k) { return typeof st[k] === 'number' && st[k] && k !== 'duration_ms'; })
        .slice(0, 3).map(function (k) { return k.replace(/_/g, ' ') + ' ' + st[k]; });
      if (nums.length) bits += '<span>· ' + esc(nums.join(' · ')) + '</span>';
      if (typeof st.duration_ms === 'number') bits += '<span>· ' + esc(fmtDur(st.duration_ms)) + '</span>';
    } else {
      bits += '<span class="st">never run</span>';
    }
    if (pend) bits += '<span class="st queued">queued</span>';
    if (next) bits += '<span>· next ' + esc(Fastt.fmtDate(next)) + '</span>';
    if (best && best.last_run && best.last_run.error_text) {
      bits += '<span class="etxt">' + esc(best.last_run.error_text) + '</span>';
    }
    host.innerHTML = bits;
  }
  function fmtDur(ms) {
    if (!isFinite(ms) || ms < 0) return '';
    if (ms < 1000) return ms + 'ms';
    var s = ms / 1000;
    if (s < 60) return (s < 10 ? s.toFixed(1) : Math.round(s)) + 's';
    return Math.floor(s / 60) + 'm ' + Math.round(s % 60) + 's';
  }

  /* run-now is wired but fires ONLY on an explicit click + confirm —
     it bypasses the cadence and can message fans immediately. */
  function addRunNow(card, rule) {
    var right = card.querySelector('.tc-right');
    if (!right || right.querySelector('[data-runnow]')) return;
    var a = document.createElement('a');
    a.className = 'cfg';
    a.setAttribute('data-runnow', rule.id);
    a.href = 'javascript:void(0)';
    a.textContent = 'Run now';
    a.addEventListener('click', async function () {
      if (!confirm('Run "' + rule.name + '" immediately? This bypasses the schedule and can message fans right now.')) return;
      try {
        var res = await Fastt.post('/admin/automation-rules/' + rule.id + '/run-now');
        Fastt.saved('Job #' + res.enqueued_job_id + ' enqueued');
      } catch (err) { Fastt.oops(err); }
    });
    right.appendChild(a);
  }

  // the Broadcast card carried demo copy in its hint — drop it
  document.querySelectorAll('.tc-hint').forEach(function (h) { h.remove(); });

  // ---- descriptions that quote a number: rewrite from the live config ----
  function num(v, fallback) { return (typeof v === 'number' && isFinite(v)) ? v : fallback; }
  /** prose money: always two decimals so partial-dollar gates read true ($7.99, $10.00) */
  function retuneCopy() {
    var c;
    if (CH) {
      c = document.querySelector('.fx-togglecard[data-kind="ai_chatter"]');
      var gate = num(CH.max_lifetime_spend_cents, null);
      if (c) setDesc(c, 'Chats and sells in DMs when a fan leans in.'
        + (gate === null ? '' : ' Fans past ' + Fastt.fmtCents(gate) + ' lifetime always go to your team.'));
    }
    if (PPV) {
      c = document.querySelector('.fx-togglecard[data-kind="ppv_send"]');
      var lo = num(PPV.price_min_cents, null), hi = num(PPV.price_max_cents, null);
      if (c) setDesc(c, 'Sends your ready-made paid drops to the right fans at the right time.'
        + (lo !== null && hi !== null ? ' Prices ' + Fastt.fmtCents(lo) + '–' + Fastt.fmtCents(hi) + '.' : ''));
    }
    if (TIP) {
      c = document.querySelector('.fx-togglecard[data-cfg="tip_reward"]');
      if (c) setDesc(c, 'A tip triggers a thank-you gift — one free pic per $' + num(TIP.dollars_per_image, 10)
        + ', up to ' + num(TIP.max_images, 5) + ', within ' + num(TIP.window_hours, 72) + ' hours.');
    }
    if (MR) {
      c = document.querySelector('.fx-togglecard[data-cfg="make_right"]');
      if (c) setDesc(c, 'Spots a double-charged fan and makes it right — apology plus '
        + num(MR.gift_min_count, 2) + '–' + num(MR.gift_max_count, 4) + ' free pieces, looking back '
        + num(MR.lookback_days, 30) + ' days.'
        + (MR.on_hard_decline ? ' Also apologises after a hard card decline.' : ''));
    }
    // payload-derived copy
    var re = (byKind.reengage_buyers || [])[0];
    c = document.querySelector('.fx-togglecard[data-kind="reengage_buyers"]');
    if (c && re && re.payload) {
      var p = re.payload;
      setDesc(c, 'One warm hello to buyers quiet for ' + num(p.cold_hours, 24) + ' h, then AI Chatter takes over. '
        + 'Max ' + num(p.max_per_run, 25) + ' a run.');
    } else if (c) {
      setDesc(c, 'One warm hello to buyers who went quiet, then AI Chatter takes over. Set it up on its own page.');
    }
    var fu = (byKind.send_followup || [])[0];
    c = document.querySelector('.fx-togglecard[data-kind="send_followup"]');
    if (c && fu && fu.payload) {
      var f = fu.payload;
      var hrs = Array.isArray(f.step_hours) ? f.step_hours.filter(function (h) { return typeof h === 'number'; }) : [];
      var steps = hrs.length || (Array.isArray(f.steps) ? f.steps.length : num(f.max_followups, 0));
      var days = hrs.length ? Math.round(Math.max.apply(null, hrs) / 24) : null;
      setDesc(c, 'Nudges fans who went silent'
        + (steps ? ' — up to ' + steps + ' gentle note' + (steps === 1 ? '' : 's') : '')
        + (days ? ' spread over ' + days + ' days' : '')
        + (f.with_image ? ', with a picture' : '') + '.');
    }
    // Reply Timing's own copy promises an overnight sleep the engine skips when
    // rhythm_no_sleep is on (service/automations/rhythm.py bails on ctx.no_sleep).
    c = document.querySelector('.fx-togglecard[data-cfg="rhythm"]');
    if (c && CH) {
      setDesc(c, CH.rhythm_no_sleep
        ? 'Human pacing — wait, type, pause. No overnight sleep on this creator: she replies around the clock.'
        : 'Human pacing — wait, type, pause, sleep at night. Off means replies arrive instantly.');
    }
    var un = byKind.unsend_messages || [];
    c = document.querySelector('.fx-togglecard[data-kind="unsend_messages"]');
    if (c && un.length) {
      var hrs = null;
      un.forEach(function (r) {
        var pol = (r.payload && r.payload.policy) || {};
        var h = num(pol.mass_text_hours, num(pol.mass_hours, null));
        if (h !== null && hrs === null) hrs = h;
      });
      setDesc(c, 'Quietly removes unsold mass messages'
        + (hrs === null ? ' on its policy' : ' after ' + hrs + ' hour' + (hrs === 1 ? '' : 's'))
        + ' so inboxes stay clean.');
    }
    // catalog summary as the tooltip for every kind card
    document.querySelectorAll('.fx-togglecard[data-kind]').forEach(function (card) {
      var k = KINDS[card.dataset.kind];
      if (k && k.summary) card.title = k.label + ' — ' + k.summary;
    });
  }

  // ---- paint cards from the live rows (re-callable after create/edit/delete) ----
  function paintKindCard(card) {
    var kind = card.dataset.kind;
    var rows = byKind[kind] || [];
    var title = card.querySelector('.fx-tc-title');
    var oldBadge = title.querySelector('.ft-live, .ft-static');
    if (oldBadge) oldBadge.remove();
    var cfg = card.querySelector('.cfg');
    if (!rows.length) {
      setState(card, false, 'Not set up');
      var k = KINDS[kind];
      setCad(card, 'no rule on this creator'
        + (k && k.cadence_default_s ? ' · default would be ' + cadWords(k.cadence_default_s) : ''));
      setRun(card, null);
      Fastt.staticBadge(title, 'NO RULE');
      if (cfg && cfg.firstChild) cfg.firstChild.nodeValue = 'Set it up ';
    } else {
      setState(card, rows.some(function (r) { return r.is_enabled; }));
      setCad(card, (rows.length > 1 ? rows.length + ' rules · ' : '') + cadSummary(rows));
      setRun(card, rows);
      addRunNow(card, rows[0]);
      Fastt.liveBadge(title);
      if (cfg && cfg.firstChild && cfg.firstChild.nodeValue.trim() === 'Set it up') cfg.firstChild.nodeValue = 'Configure ';
    }
  }
  function paintAllKindCards() {
    document.querySelectorAll('.fx-togglecard[data-kind]').forEach(paintKindCard);
  }
  paintAllKindCards();

  // ---- config-blob cards (not automation_rules rows) ----
  // Reply Timing = rhythm_enabled; Tip Rewards = tip-reward-config.enabled;
  // Make It Right = make-right-config.enabled. All three PUT {account_id, config}.
  var CFG_CARDS = {
    rhythm: {
      blob: chatter, path: '/admin/ai-chatter-config', key: 'rhythm_enabled',
      chip: function (m) {
        if (!m.rhythm_enabled) return 'humanizer off · replies land instantly';
        // rhythm.py skips the overnight sleep entirely when no_sleep is on, so
        // printing the window then would describe a rule that never fires.
        if (m.rhythm_no_sleep) return 'humanizer · no sleep window · 24/7';
        var w = (chatter && chatter.effective_sleep_window) || [];
        return 'humanizer · sleep ' + (w.length ? w.join('–') : 'window not set');
      }
    },
    tip_reward: {
      blob: tipCfg, path: '/admin/tip-reward-config', key: 'enabled',
      chip: function (m) {
        var bits = [];
        bits.push(m.always_reward ? 'every tip' : 'qualifying tips');
        if (m.ask_enabled) bits.push('tip-ask on');
        if (m.hot_teaser_enabled) bits.push('hot teaser on');
        if (m.teaser_convo_enabled) bits.push('teaser convo on');
        if (m.image_reply_enabled) bits.push('image reply on');
        return bits.join(' · ');
      }
    },
    make_right: {
      blob: mrCfg, path: '/admin/make-right-config', key: 'enabled',
      chip: function (m) {
        var bits = [];
        bits.push(m.auto_send ? 'auto-send on' : 'suggest only');
        bits.push('look back ' + num(m.lookback_days, 30) + ' d');
        bits.push('cap ' + num(m.per_fan_cap, 2) + '/fan');
        if (m.on_hard_decline) bits.push('hard-decline apology on');
        return bits.join(' · ');
      }
    }
  };
  var cfgState = {};
  Object.keys(CFG_CARDS).forEach(function (name) {
    var spec = CFG_CARDS[name];
    var card = document.querySelector('.fx-togglecard[data-cfg="' + name + '"]');
    if (!card) return;
    var title = card.querySelector('.fx-tc-title');
    if (!spec.blob) {
      setState(card, false, 'Unavailable');
      setCad(card, spec.path + ' did not load');
      Fastt.staticBadge(title, 'CONFIG UNAVAILABLE');
      return;
    }
    cfgState[name] = { stored: Object.assign({}, spec.blob.config || {}), defaults: spec.blob.defaults || {} };
    paintCfg(name);
    Fastt.liveBadge(title);
  });
  function cfgMerged(name) {
    var s = cfgState[name];
    return Object.assign({}, s.defaults, s.stored);
  }
  function paintCfg(name) {
    var spec = CFG_CARDS[name];
    var card = document.querySelector('.fx-togglecard[data-cfg="' + name + '"]');
    if (!card || !cfgState[name]) return;
    var m = cfgMerged(name);
    setState(card, !!m[spec.key]);
    setCad(card, spec.chip(m));
  }

  // ---- toggles: the fx kit flips the visuals first, we persist after ----
  document.addEventListener('click', async function (e) {
    var sw = e.target.closest('.fx-switch');
    if (!sw) return;
    var card = sw.closest('.fx-togglecard');
    if (!card) return;                              // expert-view drawer switch
    var want = sw.classList.contains('on');         // state AFTER the visual flip
    function revert() { setState(card, !want); }

    var cfgName = card.dataset.cfg;
    if (cfgName) {
      var spec = CFG_CARDS[cfgName];
      if (!spec || !cfgState[cfgName]) { revert(); Fastt.toast(cfgName + ' config not loaded'); return; }
      var next = Object.assign({}, cfgState[cfgName].stored);
      next[spec.key] = want;
      try {
        var res = await Fastt.put(spec.path, { account_id: acct, config: next });
        cfgState[cfgName].stored = (res && res.config) ? res.config : next;
        Fastt.saved(card.querySelector('.fx-tc-title').firstChild.nodeValue.trim() + ' ' + (want ? 'on' : 'off'));
      } catch (err) { revert(); Fastt.oops(err); }
      paintCfg(cfgName);
      return;
    }

    var kind = card.dataset.kind;
    if (!kind) return;
    var rows = byKind[kind] || [];
    if (!rows.length) {
      // no row to flip — offer to create it DISABLED so the hub isn't a dead end
      setState(card, false, 'Not set up');
      if (!want) return;
      var k = KINDS[kind] || {};
      if (!confirm('This creator has no "' + kind + '" automation yet.\n\n'
        + 'Create it now? It is created DISABLED — nothing starts running until you switch it on.')) return;
      try {
        var created = await Fastt.post('/admin/automation-rules', {
          account_id: acct, kind: kind, name: (k.label || kind),
          every_seconds: k.cadence_default_s || 3600, payload: {}, is_enabled: false
        });
        byKind[kind] = [created];
        rules.push(created);
        setState(card, false);
        setCad(card, fmtCad(created));
        setRun(card, [created]);
        addRunNow(card, created);
        var t = card.querySelector('.fx-tc-title');
        var b = t.querySelector('.ft-static'); if (b) b.remove();
        Fastt.liveBadge(t);
        Fastt.saved('Created "' + (created.name || kind) + '" (disabled)');
        refreshCounts();
      } catch (err) { Fastt.oops(err); }
      return;
    }
    try {
      for (var i = 0; i < rows.length; i++) {
        var upd = await Fastt.patch('/admin/automation-rules/' + rows[i].id, { is_enabled: want });
        rows[i].is_enabled = !!upd.is_enabled;
      }
      Fastt.saved((want ? 'Enabled ' : 'Disabled ') + (rows.length > 1 ? rows.length + ' rules' : '"' + rows[0].name + '"'));
      refreshCounts();
    } catch (err) { revert(); Fastt.oops(err); }
  });

  // ---- hero, section counters, expert table ----
  function refreshCounts() {
    var enabled = rules.filter(function (r) { return r.is_enabled; });
    // errors anywhere in the run log of this account's rules — the hero used to
    // count only ENABLED rules, so 4 error runs on paused rules read as "healthy".
    var errs = rules.filter(function (r) { return r.last_run && r.last_run.status === 'error'; });
    var hero = document.querySelector('.hero-line');
    if (hero) hero.innerHTML = enabled.length + ' automation' + (enabled.length === 1 ? '' : 's') + ' running<span class="sep">·</span>' +
      (errs.length ? '<span style="color:#e05b5b">' + errs.length + ' rule' + (errs.length === 1 ? '' : 's') + ' ended in error</span>'
        : enabled.length === 0 ? '<span style="color:#8a8a8a">all paused</span>'
          : '<span class="ok">all healthy</span>');
    var sub = document.querySelector('.hero-sub');
    var row = Fastt.accountRow();
    if (sub) sub.textContent = (row ? (row.nickname || row.id) : (acct || 'no account selected')) + ': ' +
      rules.length + ' rules across ' + Object.keys(byKind).length + ' kinds · ' +
      document.querySelectorAll('.fx-togglecard').length + ' cards on this page';
    document.querySelectorAll('.aut-sec').forEach(function (sec) {
      var c = sec.querySelector('.aut-sec-h .c');
      if (c) c.textContent = sec.querySelectorAll('.fx-togglecard.on').length +
        ' of ' + sec.querySelectorAll('.fx-togglecard').length + ' running';
    });
    var tb = document.querySelector('.xt tbody');
    if (tb) tb.innerHTML = rules.map(function (r) {
      return '<tr class="' + (r.is_enabled ? '' : 'dis') + '"><td class="rname">' + esc(r.name) +
        '</td><td><span class="kind">' + esc(r.kind) + '</span></td><td class="trg">' + esc(trgRaw(r)) +
        '</td><td><span class="en"><i></i>' + (r.is_enabled ? 'enabled' : 'disabled') + '</span></td>' +
        '<td class="rx-actcell">' +
        '<button class="rx-act" data-edit="' + r.id + '" type="button">Edit</button>' +
        '<button class="rx-act run" data-run="' + r.id + '" type="button">Run</button>' +
        '<button class="rx-act del" data-del="' + r.id + '" type="button">Delete</button>' +
        '</td></tr>';
    }).join('') || '<tr><td colspan="5" style="color:#8a8a8a">No rules for this account</td></tr>';
    var foot = document.querySelector('.xt-foot');
    if (foot) foot.textContent = rules.length + ' rules · ' + enabled.length +
      ' enabled · schedules run via the background executor — the UI never blocks on them.';
    var tcount = document.getElementById('rx-toolcount');
    if (tcount) tcount.textContent = rules.length + ' rule' + (rules.length === 1 ? '' : 's') + ' · ' + enabled.length + ' enabled';
  }

  // ---- status chips: honest live numbers only ----
  function refreshChips() {
    var chips = document.querySelector('.fx-status');
    if (!chips) return;
    var lastTs = null;
    rules.forEach(function (r) {
      var c = r.last_run && r.last_run.completed_at;
      if (c && (!lastTs || c > lastTs)) lastTs = c;
    });
    var pending = rules.filter(function (r) { return r.has_pending_job; }).length;
    var enabledN = rules.filter(function (r) { return r.is_enabled; }).length;
    var errN = rules.filter(function (r) { return r.last_run && r.last_run.status === 'error'; }).length;
    chips.innerHTML =
      '<span class="fx-st ' + (session && session.loaded ? 'ok' : 'err') + '"><i></i>OF session ' + (session && session.loaded ? 'loaded' : 'missing') + '</span>' +
      '<span class="fx-st"><i></i>' + enabledN + ' of ' + rules.length + ' rules enabled</span>' +
      '<span class="fx-st"><i></i>Last run ' + esc(Fastt.fmtAgo(lastTs)) + '</span>' +
      '<span class="fx-st' + (errN ? ' err' : '') + '"><i></i>' + errN + ' last-run error' + (errN === 1 ? '' : 's') + '</span>' +
      '<span class="fx-st' + (pending ? ' warn' : '') + '"><i></i>' + pending + ' job' + (pending === 1 ? '' : 's') + ' pending</span>';
  }

  // ---- run history (automation_runs audit log, 60s poll) ----
  var rhOpen = {};
  function statusCls(s) { return s === 'ok' || s === 'error' || s === 'running' ? s : ''; }
  async function loadRuns() {
    var list = document.getElementById('rh-list');
    var cnt = document.getElementById('rh-count');
    if (!list) return;
    var res;
    try { res = await Fastt.get('/admin/stats/automation-runs', { limit: 200 }); }
    catch (e) {
      list.innerHTML = '<div class="rh-empty">Run log unavailable — ' + esc((e.body && e.body.detail) || e.message) + '</div>';
      if (cnt) cnt.textContent = 'unavailable';
      return;
    }
    var runs = (res.runs || []).filter(function (r) { return !r.account_id || String(r.account_id) === String(acct); });
    var groups = [], seen = {};
    runs.forEach(function (r) {
      if (!seen[r.kind]) { seen[r.kind] = { kind: r.kind, latest: r, runs: [] }; groups.push(seen[r.kind]); }
      seen[r.kind].runs.push(r);
    });
    groups.sort(function (a, b) { return String(b.latest.started_at || '').localeCompare(String(a.latest.started_at || '')); });
    // A run can exist in the audit log without being stamped on the rule row
    // (rule.last_run stays null). Rather than let a card say "never run" while
    // the log below it shows a run 1 min ago, borrow the log's newest run.
    var latestByKind = {};
    groups.forEach(function (g) { latestByKind[g.kind] = g.latest; });
    document.querySelectorAll('.fx-togglecard[data-kind]').forEach(function (card) {
      var rows = byKind[card.dataset.kind] || [];
      if (!rows.length) return;
      if (rows.some(function (r) { return r.last_run; })) return;
      var lg = latestByKind[card.dataset.kind];
      if (!lg) return;
      var host = card.querySelector('.tc-run');
      if (!host) return;
      var s0 = Fastt.parseUtc(lg.started_at), s1 = Fastt.parseUtc(lg.completed_at);
      var d = (s0 && s1) ? fmtDur(s1.getTime() - s0.getTime()) : '';
      host.innerHTML = '<span class="st ' + esc(lg.status || '') + '">' + esc(lg.status || '—') + '</span>'
        + '<span>' + esc(Fastt.fmtAgo(lg.started_at)) + '</span>'
        + (d ? '<span>· ' + esc(d) + '</span>' : '')
        + '<span>· from the run log</span>'
        + (lg.error_text ? '<span class="etxt">' + esc(lg.error_text) + '</span>' : '');
    });
    var errN = runs.filter(function (r) { return r.status === 'error'; }).length;
    if (cnt) cnt.textContent = runs.length + ' runs · ' + groups.length + ' automations · '
      + (errN ? errN + ' errors' : 'no errors');
    if (!groups.length) {
      list.innerHTML = '<div class="rh-empty">The executor has not logged a run for this creator yet.</div>';
      return;
    }
    list.innerHTML = groups.map(function (g) {
      var k = KINDS[g.kind];
      var last = g.latest;
      var body = g.runs.slice(0, 12).map(function (r) {
        var s0 = Fastt.parseUtc(r.started_at), s1 = Fastt.parseUtc(r.completed_at);
        var d = (s0 && s1) ? fmtDur(s1.getTime() - s0.getTime()) : '';
        return '<div class="rh-d"><span class="st ' + statusCls(r.status) + '">'
          + esc(r.status) + '</span><span>' + esc(Fastt.fmtDate(r.started_at)) + '</span>'
          + (d ? '<span>· ' + esc(d) + '</span>' : '')
          + (r.error_text ? '<span style="color:#e05b5b">· ' + esc(r.error_text) + '</span>' : '')
          + (r.stats_json && r.stats_json !== '{}' ? '<span class="js">' + esc(r.stats_json) + '</span>' : '')
          + '</div>';
      }).join('');
      return '<div class="rh-row" data-kind="' + esc(g.kind) + '">'
        + '<span class="cv">' + (rhOpen[g.kind] ? '▾' : '▸') + '</span>'
        + '<span class="kd">' + esc(g.kind) + '</span>'
        + '<span class="lb">' + esc(k ? k.label : '') + '</span>'
        + '<span class="st ' + statusCls(last.status) + '">'
        + esc(last.status === 'running' ? 'running…' : last.status) + '</span>'
        + '<span class="ag">' + esc(Fastt.fmtAgo(last.started_at)) + '</span>'
        + '<span class="nn">' + g.runs.length + ' run' + (g.runs.length === 1 ? '' : 's') + '</span>'
        + '</div>'
        + (last.error_text ? '<div class="rh-err">' + esc(last.error_text) + '</div>' : '')
        + '<div class="rh-detail' + (rhOpen[g.kind] ? ' open' : '') + '" data-detail="' + esc(g.kind) + '">' + body + '</div>';
    }).join('');
  }
  var rhList = document.getElementById('rh-list');
  if (rhList) {
    rhList.addEventListener('click', function (e) {
      var row = e.target.closest('.rh-row'); if (!row) return;
      var k = row.dataset.kind;
      rhOpen[k] = !rhOpen[k];
      var d = rhList.querySelector('[data-detail="' + k + '"]');
      if (d) d.classList.toggle('open', !!rhOpen[k]);
      row.querySelector('.cv').textContent = rhOpen[k] ? '▾' : '▸';
    });
  }

  // ===================================================================
  // Rule manager — create / edit / delete one automation_rules row.
  // Housed in the Expert view so the friendly card grid stays uncluttered
  // (progressive disclosure). Mirrors the real app's RuleEditor: kind
  // (immutable on edit), name, cadence, enabled, quiet hours, and TYPED
  // per-run knobs from /admin/automation-kinds (+ a raw-JSON escape hatch).
  // New rules are created PAUSED — the operator flips them on from the card.
  // ===================================================================
  var UNIT_S = { s: 1, min: 60, h: 3600 };
  function splitEvery(secs) {
    secs = Number(secs) || 0;
    if (secs > 0 && secs % 3600 === 0) return { v: secs / 3600, u: 'h' };
    if (secs > 0 && secs % 60 === 0) return { v: secs / 60, u: 'min' };
    return { v: secs || 300, u: 's' };
  }
  function ruleById(id) { return rules.filter(function (r) { return String(r.id) === String(id); })[0]; }
  function rulesSurfaceKinds() {
    return Object.keys(KINDS).filter(function (k) { return KINDS[k].surface === 'rules'; })
      .sort(function (a, b) { return String(KINDS[a].label || a).localeCompare(String(KINDS[b].label || b)); });
  }

  var ED = null;                                    // editor state
  var modal = document.getElementById('rx-modal');
  var edBody = document.getElementById('rx-body');
  function getEl(id) { return document.getElementById(id); }
  function showErr(msg) { var e = getEl('rx-err'); if (e) { e.textContent = msg || ''; e.style.display = msg ? '' : 'none'; } }

  function seedVal(kn, payload) {
    var v = payload ? payload[kn.key] : undefined;
    if (kn.type === 'bool') return v === undefined ? Boolean(kn.default) : Boolean(v);
    if (kn.type === 'ids') return Array.isArray(v) ? v.join(', ') : '';
    if (kn.type === 'json') return v === undefined ? '' : JSON.stringify(v, null, 2);
    return v == null ? '' : String(v);
  }
  function knobField(kn, val) {
    var lbl = '<span class="rx-lbl"><code>' + esc(kn.key) + '</code> — ' + esc(kn.hint || '') + '</span>';
    var w = kn.widget || kn.type;
    if (kn.type === 'bool' || w === 'switch') {
      return '<label class="fx-check full' + (val ? ' on' : '') + '" data-knob="' + esc(kn.key) + '" data-type="bool">'
        + '<span class="bx"></span><span>' + esc(kn.key) + '</span><span class="sub">' + esc(kn.hint || '') + '</span></label>';
    }
    if (kn.type === 'json') {
      return '<div class="fx-field full">' + lbl + '<textarea class="rx-json" data-knob="' + esc(kn.key)
        + '" data-type="json" spellcheck="false" placeholder="[ ] or { }">' + esc(val == null ? '' : String(val)) + '</textarea></div>';
    }
    if (kn.type === 'str' && Array.isArray(kn.enum) && kn.enum.length) {
      var opts = kn.enum.map(function (o) {
        return '<option value="' + esc(o) + '"' + (String(val) === String(o) ? ' selected' : '') + '>' + esc(o) + '</option>';
      }).join('');
      return '<div class="fx-field">' + lbl + '<select class="fx-select" data-knob="' + esc(kn.key) + '" data-type="str">'
        + '<option value="">— default —</option>' + opts + '</select></div>';
    }
    var isNum = kn.type === 'int';
    var ph = kn.type === 'ids' ? '123, 456' : (kn.default !== undefined ? 'default ' + kn.default : '');
    return '<div class="fx-field">' + lbl + '<input class="fx-input" data-knob="' + esc(kn.key) + '" data-type="' + esc(kn.type) + '"'
      + (isNum ? ' type="number"' + (kn.min != null ? ' min="' + kn.min + '"' : '') + (kn.max != null ? ' max="' + kn.max + '"' : '') : ' type="text"')
      + ' value="' + esc(val == null ? '' : String(val)) + '" placeholder="' + esc(ph) + '"></div>';
  }

  function buildEditorBody() {
    var isEdit = ED.mode === 'edit';
    var kind = ED.kind;
    var meta = KINDS[kind] || {};
    var rule = ED.rule;
    var payload = (rule && rule.payload) || {};
    var knobs = meta.knobs || [];
    var leftover = Object.assign({}, payload);
    knobs.forEach(function (kn) { delete leftover[kn.key]; });
    ED.leftover = leftover;

    var ev = splitEvery(rule ? rule.every_seconds : (meta.cadence_default_s || 300));
    var qh = (rule && Array.isArray(rule.quiet_hours)) ? rule.quiet_hours : [];
    var enabled = rule ? !!rule.is_enabled : false;

    var html = '';
    if (!isEdit) {
      var core = rulesSurfaceKinds().filter(function (k) { return KINDS[k].group !== 'advanced'; });
      var adv = rulesSurfaceKinds().filter(function (k) { return KINDS[k].group === 'advanced'; });
      function opts(list) {
        return list.map(function (k) {
          return '<option value="' + esc(k) + '"' + (k === kind ? ' selected' : '') + '>' + esc(KINDS[k].label || k) + ' (' + esc(k) + ')</option>';
        }).join('');
      }
      html += '<div class="fx-field"><span class="rx-lbl">Automation</span><select class="fx-select" id="rx-kind">'
        + '<optgroup label="Core">' + opts(core) + '</optgroup>'
        + (adv.length ? '<optgroup label="Advanced">' + opts(adv) + '</optgroup>' : '') + '</select></div>';
    }

    html += '<div class="rx-row2">'
      + '<div class="fx-field"><span class="rx-lbl">Name</span><input class="fx-input" id="rx-name" value="'
      + esc(rule ? (rule.name || '') : (meta.label || kind)) + '" placeholder="' + esc(meta.label || kind) + '"></div>'
      + '<div class="fx-field"><span class="rx-lbl">Run every</span><div class="rx-cad">'
      + '<input class="fx-input" id="rx-cadv" type="number" min="1" value="' + ev.v + '">'
      + '<select class="fx-select" id="rx-cadu">'
      + '<option value="s"' + (ev.u === 's' ? ' selected' : '') + '>seconds</option>'
      + '<option value="min"' + (ev.u === 'min' ? ' selected' : '') + '>minutes</option>'
      + '<option value="h"' + (ev.u === 'h' ? ' selected' : '') + '>hours</option>'
      + '</select></div></div></div>';

    if (isEdit) {
      html += '<label class="fx-check' + (enabled ? ' on' : '') + ' rx-enrow" id="rx-enabled"><span class="bx"></span>'
        + '<span>Enabled — runs on this schedule</span></label>';
    } else {
      html += '<div class="rx-enrow"><span class="rx-hint">Created <b style="color:#cfcfcf">paused</b> — turn it on from its card once you’ve checked the settings.</span></div>';
    }

    if (meta.summary || meta.example) {
      html += '<div class="rx-sum">' + (meta.summary ? esc(meta.summary) : '')
        + (meta.example ? '<br><span class="ex">Example:</span> ' + esc(meta.example) : '') + '</div>';
    }
    if (meta.recurring === false) {
      html += '<div class="rx-warn">⚠ ' + esc(kind) + ' is an action-style automation — on a bare timer it needs settings to do anything useful.</div>';
    }

    html += '<div class="rx-adv-t open" data-adv="rx-settings"><svg class="cv" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4"><path d="M9 6l6 6-6 6" stroke-linecap="round" stroke-linejoin="round"/></svg>Settings'
      + (knobs.length ? '<span class="rx-jsonlink" id="rx-jsontoggle">' + (ED.rawMode ? '← Typed fields' : 'Edit raw JSON →') + '</span>' : '') + '</div>';
    html += '<div class="rx-adv-b open" id="rx-settings">';
    if (!knobs.length || ED.rawMode) {
      var seedJson = Object.keys(payload).length ? JSON.stringify(payload, null, 2) : '';
      html += '<textarea class="rx-json" id="rx-rawjson" spellcheck="false" placeholder="{ }">' + esc(seedJson) + '</textarea>';
      if (!knobs.length) html += '<div class="rx-hint" style="margin-top:6px">No catalog knobs for this automation — edit its payload as JSON.</div>';
    } else {
      html += '<div class="rx-knobs">' + knobs.map(function (kn) { return knobField(kn, seedVal(kn, payload)); }).join('') + '</div>';
    }
    html += '</div>';

    html += '<div class="rx-adv-t" data-adv="rx-adv"><svg class="cv" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4"><path d="M9 6l6 6-6 6" stroke-linecap="round" stroke-linejoin="round"/></svg>Advanced</div>';
    html += '<div class="rx-adv-b" id="rx-adv"><span class="rx-lbl">Quiet hours (creator-local, optional)</span>'
      + '<div class="rx-cad"><input class="fx-input" id="rx-qs" type="number" min="0" max="23" placeholder="—" value="' + (qh.length ? qh[0] : '') + '">'
      + '<span class="rx-hint">to</span>'
      + '<input class="fx-input" id="rx-qe" type="number" min="0" max="23" placeholder="—" value="' + (qh.length ? qh[1] : '') + '">'
      + '<span class="rx-hint">blank = 24/7 · wraps midnight if start &gt; end</span></div></div>';

    html += '<div class="rx-err" id="rx-err" style="display:none"></div>';
    edBody.innerHTML = html;

    getEl('rx-title').textContent = isEdit ? 'Edit rule' : 'New rule';
    var tag = getEl('rx-kindtag');
    tag.textContent = kind; tag.style.display = isEdit ? '' : 'none';
    updateEq();
  }

  function edEvery() {
    var v = Number((getEl('rx-cadv') || {}).value) || 0;
    var u = (getEl('rx-cadu') || {}).value || 's';
    return Math.round(v * (UNIT_S[u] || 1));
  }
  function updateEq() { var eq = getEl('rx-eq'); if (eq) eq.textContent = '= every ' + edEvery() + 's'; }
  function edName() { var e = getEl('rx-name'); return e ? e.value.trim() : ''; }
  function edEnabled() { var e = getEl('rx-enabled'); return e ? e.classList.contains('on') : false; }
  function edQuiet() {
    var a = (getEl('rx-qs') || {}).value, b = (getEl('rx-qe') || {}).value;
    a = (a == null ? '' : String(a)).trim(); b = (b == null ? '' : String(b)).trim();
    if (!a && !b) return null;
    if (!a || !b) throw new Error('Quiet hours need BOTH a start and an end (or leave both blank).');
    var s = Number(a), e = Number(b);
    if ([s, e].some(function (h) { return !Number.isInteger(h) || h < 0 || h > 23; })) throw new Error('Quiet hours must be whole hours 0–23.');
    return [s, e];
  }
  function readPayload() {
    var raw = getEl('rx-rawjson');
    if (raw) {
      var t = raw.value.trim();
      if (!t) return {};
      var p;
      try { p = JSON.parse(t); } catch (e) { throw new Error('Payload is not valid JSON: ' + e.message); }
      if (typeof p !== 'object' || p === null || Array.isArray(p)) throw new Error('Payload must be a JSON object, e.g. { "limit": 40 }');
      return p;
    }
    var out = Object.assign({}, ED.leftover || {});
    var els = edBody.querySelectorAll('[data-knob]');
    for (var i = 0; i < els.length; i++) {
      var el = els[i], key = el.getAttribute('data-knob'), type = el.getAttribute('data-type');
      if (type === 'bool') { out[key] = el.classList.contains('on'); continue; }
      var v = (el.value == null ? '' : String(el.value)).trim();
      if (v === '') continue;                        // unset → the executor's own default applies
      if (type === 'int') {
        var n = Number(v);
        if (!Number.isFinite(n) || !Number.isInteger(n)) throw new Error(key + ' must be a whole number');
        out[key] = n;
      } else if (type === 'ids') {
        var ids = v.split(/[\s,]+/).filter(Boolean).map(Number);
        if (ids.some(function (x) { return !Number.isInteger(x); })) throw new Error(key + ' must be a list of whole-number ids');
        out[key] = ids;
      } else if (type === 'json') {
        var pj; try { pj = JSON.parse(v); } catch (e) { throw new Error(key + ' is not valid JSON: ' + e.message); }
        out[key] = pj;
      } else { out[key] = v; }
    }
    return out;
  }
  function readForm() { return { name: edName(), every_seconds: edEvery(), quiet_hours: edQuiet(), is_enabled: edEnabled(), payload: readPayload() }; }

  function openEditor(rule) {
    var kind = rule ? rule.kind : (rulesSurfaceKinds()[0] || '');
    if (!kind) { Fastt.toast('Automation catalog unavailable'); return; }
    ED = { mode: rule ? 'edit' : 'new', rule: rule || null, kind: kind, rawMode: false };
    buildEditorBody();
    modal.classList.add('open');
  }
  function closeEditor() { modal.classList.remove('open'); ED = null; }

  function toggleRaw() {
    var p;
    try { p = readPayload(); } catch (e) { showErr(e.message); return; }
    showErr('');
    var name = edName(), ev = edEvery(), en = edEnabled(), qh;
    try { qh = edQuiet(); } catch (e) { qh = (ED.rule && ED.rule.quiet_hours) || null; }
    ED.rule = Object.assign({}, ED.rule || {}, {
      name: name, every_seconds: ev, is_enabled: en, quiet_hours: qh, payload: p, kind: ED.kind
    });
    ED.rawMode = !ED.rawMode;
    buildEditorBody();
  }

  async function saveEditor() {
    showErr('');
    var f;
    try { f = readForm(); } catch (e) { showErr(e.message); return; }
    if (f.every_seconds < 30) { showErr('Interval must be at least 30 seconds (the executor ticks every 30 s).'); return; }
    var btn = getEl('rx-save'); btn.disabled = true; var old = btn.textContent; btn.textContent = 'Saving…';
    try {
      if (ED.mode === 'edit') {
        await Fastt.patch('/admin/automation-rules/' + ED.rule.id, {
          name: f.name || ED.rule.kind,
          every_seconds: f.every_seconds,
          payload: f.payload,
          quiet_hours: f.quiet_hours || [0, 0],       // [0,0] clears server-side
          is_enabled: f.is_enabled
        });
        Fastt.saved('Saved "' + (f.name || ED.rule.kind) + '"');
      } else {
        // New rules are always created PAUSED — the operator turns them on from the card.
        var body = { account_id: acct, kind: ED.kind, every_seconds: f.every_seconds, payload: f.payload, is_enabled: false };
        if (f.name) body.name = f.name;
        if (f.quiet_hours) body.quiet_hours = f.quiet_hours;
        await Fastt.post('/admin/automation-rules', body);
        Fastt.saved('Created "' + (f.name || ED.kind) + '" (paused)');
      }
      closeEditor();
      await reloadRules();
      await loadRuns();
    } catch (e) {
      showErr((e && e.body && e.body.detail) || (e && e.message) || 'Save failed');
      btn.disabled = false; btn.textContent = old;
    }
  }

  async function deleteRule(id) {
    var r = ruleById(id); if (!r) return;
    if (!confirm('Delete automation rule "' + r.name + '" (' + r.kind + ')?\n\nThis stops its schedule and cannot be undone.')) return;
    try {
      await Fastt.del('/admin/automation-rules/' + id);
      Fastt.saved('Deleted "' + r.name + '"');
      await reloadRules();
      await loadRuns();
    } catch (e) { Fastt.oops(e); }
  }

  async function runRuleById(id) {
    var r = ruleById(id); if (!r) return;
    if (!confirm('Run "' + r.name + '" immediately? This bypasses the schedule and can message fans right now.')) return;
    try {
      var res = await Fastt.post('/admin/automation-rules/' + id + '/run-now');
      Fastt.saved('Job #' + res.enqueued_job_id + ' enqueued');
    } catch (e) { Fastt.oops(e); }
  }

  // rebuild the local rule model + repaint everything that reads it
  async function reloadRules() {
    var o = await Fastt.get('/admin/automation-rules');
    rules = (o.rules || []).filter(function (r) { return String(r.account_id) === String(acct); });
    byKind = {};
    rules.forEach(function (r) { (byKind[r.kind] = byKind[r.kind] || []).push(r); });
    paintAllKindCards();
    retuneCopy();
    refreshCounts();
    refreshChips();
  }

  // ---- editor + expert-view + refresh wiring ----
  getEl('rx-close').addEventListener('click', closeEditor);
  getEl('rx-cancel').addEventListener('click', closeEditor);
  getEl('rx-save').addEventListener('click', saveEditor);
  modal.addEventListener('click', function (e) { if (e.target === modal) closeEditor(); });
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape' && modal.classList.contains('open')) closeEditor(); });
  edBody.addEventListener('input', function (e) { if (e.target.id === 'rx-cadv') updateEq(); });
  edBody.addEventListener('change', function (e) {
    if (e.target.id === 'rx-kind') { ED.kind = e.target.value; ED.rawMode = false; ED.rule = null; buildEditorBody(); }
    else if (e.target.id === 'rx-cadu') updateEq();
  });
  edBody.addEventListener('click', function (e) {
    if (e.target.closest('#rx-jsontoggle')) { toggleRaw(); return; }
    var at = e.target.closest('.rx-adv-t');
    if (at) { var b = getEl(at.getAttribute('data-adv')); var open = at.classList.toggle('open'); if (b) b.classList.toggle('open', open); }
  });

  var xtb = document.querySelector('.xt tbody');
  if (xtb) xtb.addEventListener('click', function (e) {
    var ed = e.target.closest('[data-edit]'); if (ed) { var r = ruleById(ed.getAttribute('data-edit')); if (r) openEditor(r); return; }
    var rn = e.target.closest('[data-run]'); if (rn) { runRuleById(rn.getAttribute('data-run')); return; }
    var dl = e.target.closest('[data-del]'); if (dl) { deleteRule(dl.getAttribute('data-del')); return; }
  });
  var rxNew = getEl('rx-new');
  if (rxNew) rxNew.addEventListener('click', function () { openEditor(null); });

  var pgRefresh = getEl('pg-refresh');
  if (pgRefresh) pgRefresh.addEventListener('click', async function () {
    pgRefresh.disabled = true; var svg = pgRefresh.querySelector('svg'); if (svg) svg.style.opacity = '.4';
    try { await reloadRules(); await loadRuns(); Fastt.toast('Refreshed'); }
    catch (e) { Fastt.oops(e); }
    finally { pgRefresh.disabled = false; if (svg) svg.style.opacity = ''; }
  });

  retuneCopy();
  refreshCounts();
  refreshChips();
  await loadRuns();
  setInterval(loadRuns, 60000);
});
