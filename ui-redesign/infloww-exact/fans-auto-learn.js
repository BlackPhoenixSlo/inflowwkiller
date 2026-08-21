/* ==== fastt wiring: gen_info + apply_profiles rules (see service/automations/) ==== */
Fastt.ready(async function () {
  "use strict";
  var $ = Fastt.$;
  // Server timestamps are tz-naive UTC — pin them before Date() parses as local.
  function utc(s) { return (s && !/[zZ]|[+-]\d{2}:?\d{2}$/.test(s)) ? s + "Z" : s; }
  function chip(el, text, cls) {
    if (!el) return;
    el.className = "fx-st" + (cls ? " " + cls : "");
    el.innerHTML = "<i></i>" + Fastt.esc(text);
  }

  var byKind = await Fastt.rulesByKind();
  var gen = (byKind.gen_info || [])[0] || null;
  var ap  = (byKind.apply_profiles || [])[0] || null;

  // ── status strip ─────────────────────────────────────────────
  var genOn = !!(gen && gen.is_enabled);
  chip($("#al-st-run"), genOn ? "Running" : "Off", genOn ? "ok" : "");
  var lastAt = gen && gen.last_run && (gen.last_run.completed_at || gen.last_run.started_at);
  chip($("#al-st-last"), "Last pass " + Fastt.fmtAgo(utc(lastAt)));
  // Model + cost come from the PER-PURPOSE LLM ledger, not from the whole
  // account's AI spend: /admin/stats/grok-cost sums ai_chatter + autoreply +
  // followup + everything, so showing it here labelled "today" made unrelated
  // chat spend read as profiling cost. grok-calls is grouped by purpose —
  // filter to gen_info, which is what this page is about.
  function genRow(rows) {
    return (rows || []).find(function (r) { return r.purpose === "gen_info"; }) || null;
  }
  var payloadModel = (gen && gen.payload && gen.payload.model) || "";
  var lifeGen = null, todayGen = null;
  try {
    var life = await Fastt.get("/admin/stats/grok-calls");
    lifeGen = genRow(life.rows);
  } catch (e) { /* chips fall back to the rule row below */ }
  try {
    var d = new Date(), iso = new Date(d.getTime() - d.getTimezoneOffset() * 60000)
      .toISOString().slice(0, 10);
    var day = await Fastt.get("/admin/stats/grok-calls", { from: iso, to: iso });
    todayGen = genRow(day.rows);
  } catch (e) { /* below */ }

  // Model chip: what the ledger says was ACTUALLY used, cross-checked against
  // the configured override instead of trusting payload.model blindly.
  var usedModel = (lifeGen && lifeGen.model) || "";
  if (usedModel) {
    chip($("#al-st-model"), payloadModel && payloadModel !== usedModel
      ? "set " + payloadModel + " · last used " + usedModel
      : usedModel + " · verified in the LLM ledger",
      payloadModel && payloadModel !== usedModel ? "warn" : "");
  } else {
    chip($("#al-st-model"), payloadModel
      ? payloadModel + " · never used yet"
      : (gen && gen.last_run && gen.last_run.stats && gen.last_run.stats.model)
        || "account default");
  }

  if (todayGen) {
    chip($("#al-st-cost"), Fastt.fmtCents(todayGen.cost_cents) + " profiling today · "
      + Fastt.fmtInt(todayGen.calls) + " calls", "ok");
  } else {
    chip($("#al-st-cost"), "$0.00 profiling today · no gen_info calls");
  }
  if (lifeGen) {
    chip($("#al-st-life"), "lifetime " + Fastt.fmtInt(lifeGen.calls) + " profiles · "
      + Fastt.fmtCents(lifeGen.cost_cents) + " · " + (lifeGen.avg_latency_ms || 0) + " ms avg · "
      + Math.round((lifeGen.cache_hit_ratio || 0) * 100) + "% cache");
  } else {
    chip($("#al-st-life"), "lifetime — no profiling calls on this creator yet");
  }

  // ── main toggle card = gen_info.is_enabled ───────────────────
  var card = $("#al-card"), sw = $("#al-switch"), state = $("#al-state");
  function paintCard(on) {
    card.classList.toggle("on", on);
    sw.classList.toggle("on", on);
    state.textContent = on ? "Running" : "Off";
  }
  paintCard(genOn);
  Fastt.liveBadge(card.querySelector(".fx-tc-title"));

  sw.addEventListener("click", function () {
    var want = !sw.classList.contains("on"); // read BEFORE the fx-kit visual toggle
    Fastt.upsertRule("gen_info", { is_enabled: want },
      { every_seconds: 2700, is_enabled: false, payload: { limit: 200 } })
      .then(function () {
        if (gen) gen.is_enabled = want;
        Fastt.saved(want ? "Auto-learn ON" : "Auto-learn OFF");
        chip($("#al-st-run"), want ? "Running" : "Off", want ? "ok" : "");
        setTimeout(function () { paintCard(want); }, 0);
      })
      .catch(function (e) { Fastt.oops(e); setTimeout(function () { paintCard(!want); }, 0); });
  });

  // ── "write nicknames & notes to OF" = apply_profiles push ────
  var pushCk = $("#al-push-check");
  var pushOn = !!(ap && ap.is_enabled && ap.payload && ap.payload.push_to_of !== false);
  pushCk.classList.toggle("on", pushOn);
  pushCk.addEventListener("click", function () {
    var want = !pushCk.classList.contains("on");
    var pl = Object.assign({}, (ap && ap.payload) || {}, { push_to_of: want });
    // ON needs the rule running too; OFF just stops the OF write (DB notes keep flowing).
    var body = want ? { is_enabled: true, payload: pl } : { payload: pl };
    Fastt.upsertRule("apply_profiles", body, { every_seconds: 86400, is_enabled: false })
      .then(function () {
        if (ap) { ap.payload = pl; if (want) ap.is_enabled = true; }
        Fastt.saved(want ? "Nicknames + notes will be written to OF" : "OF write off — DB only");
        setTimeout(function () { pushCk.classList.toggle("on", want); }, 0);
      })
      .catch(function (e) { Fastt.oops(e); setTimeout(function () { pushCk.classList.toggle("on", !want); }, 0); });
  });

  // ── kv line: honest numbers from the last apply sweep ────────
  var apStats = ap && ap.last_run && ap.last_run.stats;
  var profiled = apStats && apStats.profiles_seen != null ? Fastt.fmtInt(apStats.profiles_seen) : "—";
  var nextTxt = "paused";
  if (gen && gen.is_enabled && gen.next_due_at) {
    var due = Fastt.parseUtc(gen.next_due_at);
    var mins = Math.max(0, Math.round(((due ? due.getTime() : Date.now()) - Date.now()) / 60000));
    nextTxt = "in " + (mins >= 60 ? Math.round(mins / 60) + " h" : mins + " min");
  }
  $("#al-kv").innerHTML = "<b>" + profiled + "</b> fans profiled (last sweep) · spend-tiered refresh · next pass " + Fastt.esc(nextTxt);

  // ── what the OF write actually did last time (apply_profiles stats) ──
  (function paintPushEvidence() {
    var el = $("#al-push-evi");
    if (!apStats) {
      el.innerHTML = ap
        ? "last push <b>never run</b> — nothing has been written to OnlyFans yet"
        : "no apply-profiles rule for this creator yet — nothing writes to OnlyFans";
      return;
    }
    if (!apStats.pushed_to_of) {
      el.innerHTML = "last sweep wrote <b>0</b> to OnlyFans (OF write was off) · "
        + "<b>" + Fastt.fmtInt(apStats.applied || 0) + "</b> profiles applied in fastt"
        + " · " + Fastt.fmtAgo(utc(ap.last_run.completed_at || ap.last_run.started_at));
      return;
    }
    el.innerHTML = "last push <b>" + Fastt.fmtInt(apStats.pushed_nick || 0) + "</b> nicknames + <b>"
      + Fastt.fmtInt(apStats.pushed_note || 0) + "</b> notes written to OF · <b>"
      + Fastt.fmtInt(apStats.skipped_empty || 0) + "</b> profiles had nothing to write"
      + (apStats.errors ? " · <b style=\"color:#f2295b\">" + Fastt.fmtInt(apStats.errors) + " errors</b>" : " · 0 errors")
      + " · " + Fastt.fmtAgo(utc(ap.last_run.completed_at || ap.last_run.started_at));
  })();

  // ── gen_info's OWN last pass — was fetched and thrown away before ──
  (function paintLastPass() {
    var card = $("#al-pass-card");
    var lr = gen && gen.last_run, s = lr && lr.stats;
    if (!gen) {
      $("#al-pass-when").textContent = "";
      $("#al-pass-verdict").textContent = "No gen_info rule exists for this creator yet — flip Auto-learn on once to create it.";
      Fastt.staticBadge(card.querySelector(".fx-card-h"), "NO RULE YET");
      return;
    }
    if (!lr || !s) {
      $("#al-pass-when").textContent = "never run";
      $("#al-pass-verdict").textContent = "This rule has never completed a pass, so there is nothing to report yet.";
      Fastt.staticBadge(card.querySelector(".fx-card-h"), "NEVER RUN");
      return;
    }
    Fastt.liveBadge(card.querySelector(".fx-card-h"));
    $("#al-pass-when").textContent = Fastt.fmtAgo(utc(lr.completed_at || lr.started_at))
      + " · " + Fastt.fmtDate(utc(lr.started_at));
    var cand = s.candidates || 0, fresh = s.skipped_fresh || 0, tried = s.attempted || 0;
    var made = s.generated || 0, failed = s.failed || 0, capped = s.capped || 0;
    var verdict;
    if (lr.status !== "ok") {
      verdict = "The pass FAILED: " + (lr.error_text || lr.status);
    } else if (!cand) {
      verdict = "No fan qualified this pass — nobody had written a message worth profiling.";
    } else if (!tried) {
      verdict = "Nothing new to learn: all " + Fastt.fmtInt(cand) + " candidates were already fresh "
        + "(under the 8-new-message rebuild gate, or inside their tier's window), so no LLM call was made.";
    } else {
      verdict = "Rebuilt " + Fastt.fmtInt(made) + " of " + Fastt.fmtInt(tried) + " attempted profiles"
        + (failed ? ", " + Fastt.fmtInt(failed) + " failed" : ", none failed")
        + (capped ? ", " + Fastt.fmtInt(capped) + " hit the daily budget cap" : "")
        + " · model " + (s.model || "account default") + ".";
    }
    $("#al-pass-verdict").textContent = verdict;
    var cells = [
      ["Candidates", cand, ""],
      ["Already fresh", fresh, fresh && !tried ? "warn" : ""],
      ["Attempted", tried, tried ? "" : "zero"],
      ["Rebuilt", made, made ? "" : "zero"],
      ["Deferred for scrape", s.deferred_for_scrape || 0, ""],
      ["Capped by budget", capped, capped ? "warn" : "zero"],
      ["Failed", failed, failed ? "warn" : "zero"],
      ["Took", s.duration_ms != null ? (s.duration_ms / 1000).toFixed(1) + " s" : "n/a", "dim"],
    ];
    $("#al-pass-grid").innerHTML = cells.map(function (c) {
      var val = typeof c[1] === "number" ? Fastt.fmtInt(c[1]) : c[1];
      return '<div class="al-pv ' + (c[2] === "zero" ? "zero" : c[2] === "warn" ? "warn" : "")
        + '"><b>' + Fastt.esc(val) + "</b><span>" + Fastt.esc(c[0]) + "</span></div>";
    }).join("");
  })();

  // ── pass history (last 10) ───────────────────────────────────
  (async function loadPassHistory() {
    var host = $("#al-pass-hist");
    if (!gen) { host.innerHTML = ""; return; }
    var out;
    try {
      out = await Fastt.get("/admin/stats/automation-runs", { kind: "gen_info", limit: 20 });
    } catch (e) {
      host.innerHTML = '<div class="fx-kv" style="margin-top:12px">Run history unavailable — the relay didn\'t answer.</div>';
      return;
    }
    var runs = (out.runs || []).map(function (r) {
      var s = {};
      try { s = r.stats_json ? JSON.parse(r.stats_json) : (r.stats || {}); } catch (e) { s = {}; }
      return { at: r.started_at, status: r.status, err: r.error_text, s: s };
    });
    if (!runs.length) { host.innerHTML = '<div class="fx-kv" style="margin-top:12px">No recorded passes yet.</div>'; return; }
    host.innerHTML = '<table class="al-hist"><thead><tr><th>Pass</th><th class="num">Candidates</th>'
      + '<th class="num">Already fresh</th><th class="num">Rebuilt</th><th class="num">Failed</th><th class="num">Took</th></tr></thead><tbody>'
      + runs.slice(0, 10).map(function (r) {
          var s = r.s, gm = s.generated || 0;
          return "<tr><td>" + Fastt.esc(Fastt.fmtDate(utc(r.at)))
            + (r.status !== "ok" ? ' <span style="color:#f2295b">' + Fastt.esc(r.status) + "</span>" : "") + "</td>"
            + '<td class="num">' + Fastt.fmtInt(s.candidates || 0) + "</td>"
            + '<td class="num">' + Fastt.fmtInt(s.skipped_fresh || 0) + "</td>"
            + '<td class="num' + (gm ? "" : " dim") + '">' + Fastt.fmtInt(gm) + "</td>"
            + '<td class="num' + (s.failed ? "" : " dim") + '">' + Fastt.fmtInt(s.failed || 0) + "</td>"
            + '<td class="num dim">' + (s.duration_ms != null ? (s.duration_ms / 1000).toFixed(1) + " s" : "n/a") + "</td></tr>";
        }).join("")
      + "</tbody></table>"
      + (runs.length > 10 ? '<div class="fx-kv" style="margin-top:8px">showing the last 10 of ' + runs.length + " recorded passes</div>" : "");
  })();

  // ── advanced: static knobs (values live in code, not config) ─
  var cadGroup = $("#al-cadence-group");
  Fastt.staticBadge(cadGroup.querySelector("h4"), "FIXED IN CODE");
  // Read-outs of gen_info.py constants, not editable knobs — no endpoint exposes them.
  Fastt.$$("input, select", cadGroup).forEach(function (s) { s.disabled = true; });
  $("#al-min-msgs").disabled = true;
  Fastt.staticBadge($("#al-min-field").querySelector("label"), "FIXED IN CODE");
  var skipCk = $("#al-skip-check");
  skipCk.style.pointerEvents = "none";
  Fastt.staticBadge(skipCk, "FIXED IN CODE");
  var pushField = $("#al-push-field");
  pushField.querySelector("select").disabled = true;
  Fastt.staticBadge(pushField.querySelector("label"), "USE CHECKBOX ABOVE");
  Fastt.liveBadge($("#adv-learn").previousElementSibling); // the adv head row

  // ── advanced: live knobs → gen_info payload ──────────────────
  // Resolves TRUE only when the PUT landed — callers repaint/revert on false,
  // so a failed save can never leave an unsaved value sitting in the field.
  function saveGenPayload(mut, msg) {
    var pl = Object.assign({}, (gen && gen.payload) || {});
    mut(pl);
    return Fastt.upsertRule("gen_info", { payload: pl },
      { every_seconds: 2700, is_enabled: false })
      .then(function () { if (gen) gen.payload = pl; Fastt.saved(msg); return true; })
      .catch(function (e) { Fastt.oops(e); return false; });
  }
  function savedLimit() { return (gen && gen.payload && gen.payload.limit) || 200; }
  function savedModel() { return (gen && gen.payload && gen.payload.model) || ""; }

  var limitIn = $("#al-limit");
  limitIn.value = savedLimit();
  limitIn.addEventListener("change", function () {
    var n = parseInt(String(limitIn.value).replace(/[^\d]/g, ""), 10);
    if (!n || n < 1) { Fastt.toast("Enter a number ≥ 1", "err"); limitIn.value = savedLimit(); return; }
    limitIn.value = n;
    saveGenPayload(function (pl) { pl.limit = n; }, "Sweep limit saved")
      .then(function (ok) { if (!ok) limitIn.value = savedLimit(); });
  });

  var modelSel = $("#al-model");
  var curModel = savedModel();
  if (curModel && !Array.prototype.some.call(modelSel.options, function (o) { return o.value === curModel; })) {
    var opt = document.createElement("option");
    opt.value = curModel; opt.textContent = curModel;
    modelSel.appendChild(opt);
  }
  // No payload.model = no override saved: show "Account default", which is what
  // the status chip says, instead of presuming the code default is configured.
  modelSel.value = curModel;
  modelSel.addEventListener("change", function () {
    var want = modelSel.value;
    saveGenPayload(function (pl) { if (want) pl.model = want; else delete pl.model; },
                   want ? "Profiling model saved" : "Cleared — using the account default")
      .then(function (ok) { if (!ok) modelSel.value = savedModel(); });
  });

  // ── force-refresh one fan (explicit click; enqueues force_ids gen_info) ─
  $("#al-force-btn").addEventListener("click", function () {
    var raw = $("#al-force-id").value.trim();
    if (!/^\d{3,}$/.test(raw)) { Fastt.toast("Enter the fan's numeric OF id (name lookup isn't wired here)", "err"); return; }
    // gen_info only rebuilds the stored profile. The OF nickname/note write is a
    // separate rule (apply_profiles) and only happens on its next sweep.
    if (!confirm("Rebuild this fan's profile now? Burns one LLM call. Nothing is written to OnlyFans by this — the nickname/note push happens later, on the next apply-profiles sweep.")) return;
    Fastt.post("/admin/fans/" + Fastt.account() + "/" + raw + "/lines/generate")
      .then(function () { Fastt.saved("Queued — profile rebuilds within ~30 s"); })
      .catch(function (e) { Fastt.oops(e); });
  });

  // ══ REAL profile card ═══════════════════════════════════════════════
  // Every field below exists per fan on GET /admin/fans/{acct}/{fan_id}; the
  // roster comes from the same per-fan-data build the Sheets export uses.
  var profCard = $("#al-prof-card"), pick = $("#al-fan-pick"), body = $("#al-prof-body");
  var LANG = { en: "English", es: "Español", sl: "Slovenščina", pt: "Português",
               fr: "Français", de: "Deutsch", it: "Italiano" };
  var QI = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M20 12a8 8 0 0 1-11.3 7.3L4 20l.7-4.7A8 8 0 1 1 20 12z"/></svg>';
  var TI = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M12 20s-7-4.5-7-9.5A3.8 3.8 0 0 1 12 8a3.8 3.8 0 0 1 7 2.5C19 15.5 12 20 12 20z" stroke-linejoin="round"/></svg>';

  function initials(name, id) {
    var s = String(name || id || "?").trim();
    var p = s.split(/\s+/);
    // one word → first TWO characters (a single "9" from a numeric id reads broken)
    var out = p.length > 1 ? (p[0][0] || "") + (p[1][0] || "") : s.slice(0, 2);
    return (out || "?").toUpperCase();
  }
  function splitTags(v) {
    return String(v || "").split(/[,;]+/).map(function (t) { return t.trim(); })
      .filter(Boolean).slice(0, 6);
  }
  function profBody(fan, av) {
    var p = fan.profile || {};
    var realName = fan.of_display_name || (av && av.name) || fan.real_name || "";
    var name = realName || ("fan " + fan.fan_id);
    // OF leaves of_display_name blank on most fans — fall back to the handle, then
    // the id, rather than stamping a made-up label on the avatar.
    var avaSeed = realName || fan.of_username || (av && av.username) || String(fan.fan_id);
    var metaBits = [];
    if (fan.his_age) metaBits.push(fan.his_age);
    var loc = [fan.home_city, fan.home_country].filter(Boolean).join(", ");
    if (loc) metaBits.push(loc);
    metaBits.push(Fastt.fmtCents(fan.lifetime_spend_cents) + " lifetime");
    if (p.last_generated_at) metaBits.push("profiled " + Fastt.fmtAgo(p.last_generated_at));
    else metaBits.push("never profiled");

    var tags = splitTags(fan.occupation).concat(splitTags(fan.hobbies)).concat(splitTags(fan.fetishes))
      .concat(Array.isArray(fan.tags) ? fan.tags : []);
    tags = tags.filter(function (t, i) { return tags.indexOf(t) === i; }).slice(0, 8);

    var av_html = av && av.avatar
      ? '<img src="' + Fastt.esc(av.avatar) + '" alt="">'
      : Fastt.esc(initials(avaSeed, fan.fan_id));

    var h = '<div class="alp-head">'
      + '<div class="alp-ava">' + av_html + "</div>"
      + "<div><div class=\"alp-name\">" + Fastt.esc(name) + "</div>"
      + '<div class="alp-meta">' + Fastt.esc(metaBits.join(" · ")) + "</div></div>"
      + (fan.language ? '<span class="alp-lang">🌐 ' + Fastt.esc(LANG[fan.language] || fan.language)
          + (fan.language_source === "manual" ? " · pinned" : "") + "</span>" : "")
      + "</div>";

    h += '<div class="alp-row"><span class="alp-lbl">Nickname → OF</span><b>'
      + Fastt.esc(p.nickname || fan.custom_nickname || fan.generated_nickname || "— none written yet")
      + "</b></div>";

    h += tags.length
      ? '<div class="alp-tags">' + tags.map(function (t) {
          return '<span class="alp-tag">' + Fastt.esc(t) + "</span>"; }).join("") + "</div>"
      : '<div class="alp-empty">No job / hobbies / kinks extracted yet — he hasn\'t said enough.</div>';

    h += '<div class="alp-sec">Note pushed to OnlyFans</div>';
    h += fan.applied_notes
      ? '<div class="alp-note">' + Fastt.esc(fan.applied_notes).replace(/\n/g, "<br>") + "</div>"
        + '<div class="alp-foot" style="border:0;padding:6px 0 0;margin:0">written to OF '
        + Fastt.esc(fan.applied_notes_at ? Fastt.fmtAgo(fan.applied_notes_at) : "at an unknown time") + "</div>"
      : '<div class="alp-empty">Nothing pushed to OnlyFans for him — the apply-profiles sweep either hasn\'t run or had nothing to write.</div>';

    if (p.short_bio) {
      h += '<div class="alp-sec">Short bio</div><div class="alp-note">' + Fastt.esc(p.short_bio) + "</div>";
    }

    h += '<div class="alp-sec">Questions it will ask</div>';
    h += (p.q && p.q.length)
      ? p.q.map(function (q) { return '<div class="alp-q">' + QI + Fastt.esc(q) + "</div>"; }).join("")
      : '<div class="alp-empty">No questions written yet.</div>';

    h += '<div class="alp-sec">Tease lines</div>';
    h += (p.tease && p.tease.length)
      ? p.tease.map(function (t) { return '<div class="alp-q pink">' + TI + Fastt.esc(t) + "</div>"; }).join("")
      : '<div class="alp-empty">No tease lines written yet.</div>';

    if (p.rich_note) {
      h += '<div class="alp-sec">Full note the AI reads</div><div class="alp-note">'
        + Fastt.esc(p.rich_note).replace(/\n/g, "<br>") + "</div>";
    }

    h += '<div class="alp-foot">Rebuilt automatically once ' + Fastt.esc(realName ? realName.split(/\s+/)[0] : "he")
      + " sends 8 more messages and his tier's window is up.</div>";
    return h;
  }

  async function showFan(fanId) {
    body.innerHTML = '<div style="font-size:13px;color:var(--muted);padding:10px 0">Loading profile…</div>';
    try {
      var fan = await Fastt.get("/admin/fans/" + Fastt.account() + "/" + fanId);
      var av = null;
      try {
        var byIds = await Fastt.get("/admin/fans/" + Fastt.account() + "/by-ids", { ids: fanId });
        av = (byIds.fans || {})[String(fanId)] || null;
      } catch (e) { /* avatar is a nicety — initials cover it */ }
      body.innerHTML = profBody(fan, av);
      // Friendliness: the Advanced "Refresh now" box targets the fan you're
      // looking at, so a stale profile is one click to rebuild — without
      // retyping the id. Only fill when empty, never clobber a typed value.
      var fid = $("#al-force-id");
      fid.placeholder = "Fan ID — e.g. " + fanId;
      if (!fid.value.trim()) fid.value = fanId;
    } catch (e) {
      body.innerHTML = '<div class="alp-empty">Couldn\'t load fan ' + Fastt.esc(fanId)
        + ' — the relay returned an error. Nothing is shown rather than something invented.</div>';
      Fastt.oops(e);
    }
  }

  (async function mountProfile() {
    var data;
    try {
      data = await Fastt.get("/admin/stats/per-fan-data");
    } catch (e) {
      $("#al-prof-badge").textContent = "unavailable";
      pick.innerHTML = "<option>—</option>"; pick.disabled = true;
      body.innerHTML = '<div class="alp-empty">Couldn\'t reach the relay, so no fan profile is shown. '
        + "This card only ever renders a real profiled fan.</div>";
      Fastt.staticBadge(profCard.querySelector(".fx-card-h"), "NO DATA — RELAY UNREACHABLE");
      return;
    }
    var rows = data.rows || [];
    if (!rows.length) {
      $("#al-prof-badge").textContent = "none yet";
      pick.innerHTML = "<option>No profiled fans</option>"; pick.disabled = true;
      body.innerHTML = '<div class="alp-empty">No fan on this creator has a profile yet — turn Auto-learn on '
        + "and the first sweep fills this card.</div>";
      Fastt.staticBadge(profCard.querySelector(".fx-card-h"), "NO PROFILED FANS YET");
      return;
    }
    $("#al-prof-badge").textContent = Fastt.fmtInt(rows.length) + " profiled";
    Fastt.liveBadge(profCard.querySelector(".fx-card-h"));
    pick.innerHTML = rows.map(function (r) {
      var label = (r.fan_name || ("fan " + r.fan_id))
        + (r.total_spend ? " · " + Fastt.fmtMoney(r.total_spend) : "")
        + " · " + Fastt.fmtInt(r.message_count || 0) + " msgs";
      return '<option value="' + Fastt.esc(r.fan_id) + '">' + Fastt.esc(label) + "</option>";
    }).join("");
    pick.addEventListener("change", function () { showFan(pick.value); });
    await showFan(rows[0].fan_id);
  })();
});
