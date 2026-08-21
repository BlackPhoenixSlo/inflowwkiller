Fastt.ready(async () => {
  const $ = Fastt.$, $$ = Fastt.$$, esc = Fastt.esc;
  const KIND = "process_old_fans";
  const ACCT = Fastt.account();

  let rule = null, chatCfg = {}, acctModel = null, runs = [], lastStats = null;
  let polling = null;

  const setSt = (id, text, cls) => { const el = $(id); if (!el) return;
    el.className = "fx-st" + (cls ? " " + cls : ""); el.innerHTML = "<i></i>" + esc(text); };
  const parseStats = (r) => { if (!r || !r.stats_json) return null;
    try { return JSON.parse(r.stats_json); } catch { return null; } };

  async function loadAll() {
    const [rulesRes, chatRes, cfgRes, runsRes] = await Promise.allSettled([
      Fastt.rulesByKind(),
      Fastt.get("/admin/ai-chatter-config"),
      Fastt.get("/admin/account-config"),
      Fastt.get("/admin/stats/automation-runs", { kind: KIND, limit: 5 }),
    ]);
    if (rulesRes.status !== "fulfilled") { Fastt.oops(rulesRes.reason); return false; }
    rule = (rulesRes.value[KIND] || [])[0] || null;
    chatCfg = chatRes.status === "fulfilled" ? (chatRes.value.config || chatRes.value) : {};
    acctModel = cfgRes.status === "fulfilled" ? cfgRes.value.config.model : null;
    runs = runsRes.status === "fulfilled" ? (runsRes.value.runs || []) : [];
    lastStats = parseStats(runs[0]);
    return true;
  }

  async function reloadRuns() {
    try {
      const out = await Fastt.get("/admin/stats/automation-runs", { kind: KIND, limit: 5 });
      runs = out.runs || [];
      lastStats = parseStats(runs[0]);
      return true;
    } catch { return false; }
  }

  // ── payload assembled from the Advanced knobs ────────────────
  // Split the targeting box into numeric fan_ids vs @handle usernames.
  function parseTargets() {
    const raw = ($("#im-targets").value || "").split(/[\s,]+/).map((s) => s.trim()).filter(Boolean);
    const ids = [], handles = [];
    raw.forEach((t) => { const c = t.replace(/^@+/, "");
      if (/^\d+$/.test(c)) ids.push(Number(c)); else if (c) handles.push(c); });
    return { ids, handles };
  }
  function livePayload() {
    const p = { ...((rule && rule.payload) || {}) };
    const { ids, handles } = parseTargets();
    if (ids.length) p.fan_ids = ids; else delete p.fan_ids;
    if (handles.length) p.usernames = handles; else delete p.usernames;
    const n = parseInt($("#im-batch").value, 10);
    if (isFinite(n) && n >= 1) p.recent_limit = n; else delete p.recent_limit;
    p.recent_by = $("#im-by").value === "messaged" ? "messaged" : "subscribed";
    const lim = parseInt($("#im-limit").value, 10);
    if (isFinite(lim) && lim >= 1) p.limit = lim; else delete p.limit;
    const mdl = ($("#im-model").value || "").trim();
    if (mdl) p.model = mdl; else delete p.model;
    const set = (k, on) => { if (on) p[k] = true; else delete p[k]; };
    set("flag_only", $("#im-flagonly").classList.contains("on"));
    set("push_to_of", $("#im-push").classList.contains("on"));
    set("reprocess", $("#im-reprocess").classList.contains("on"));
    return p;
  }

  function render() {
    const p = (rule && rule.payload) || {};
    const on = !!(rule && rule.is_enabled);
    const lastRun = runs[0] || null;
    setSt("#im-st-run",
      !rule ? "No import rule yet — the run button creates a one-shot job anyway"
        : lastRun ? "Last ran " + Fastt.fmtAgo(lastRun.started_at) + " · " + lastRun.status
        : (on ? "Scheduled — waiting for its slot" : "Never run"),
      lastRun && lastRun.status === "ok" ? "ok" : (lastRun && lastRun.status === "error" ? "err" : ""));
    const explicitN = (Array.isArray(p.fan_ids) ? p.fan_ids.length : 0)
      + (Array.isArray(p.usernames) ? p.usernames.length : 0);
    const batch = explicitN
      ? "Batch: " + explicitN + " specifically targeted fan" + (explicitN === 1 ? "" : "s")
      : p.recent_limit
        ? "Batch: " + p.recent_limit + " most recently " + (p.recent_by === "messaged" ? "messaged" : "subscribed") + " fans"
        : "Batch not configured — set it under Advanced";
    setSt("#im-st-batch", batch, explicitN || p.recent_limit ? "" : "warn");
    setSt("#im-st-model", "Profiles by " + (p.model || acctModel || "account default model"));

    $("#im-state").textContent = on ? "On (timer)" : "Off";
    $("#im-switch").classList.toggle("on", on);
    $("#im-card").classList.toggle("on", on);

    const s1 = $("#im-s1"), s2 = $("#im-s2"), s3 = $("#im-s3");
    if (lastStats && !(lastStats.old_fans ?? 0)) {
      // A run that matched no fans is NOT progress — say so instead of "Done · 0".
      s1.textContent = "Last run matched no fans"; s1.className = "imp-state warn";
      s2.textContent = "Nothing to profile"; s2.className = "imp-state";
    } else if (lastStats) {
      s1.textContent = "Done · " + Fastt.fmtInt(lastStats.flagged ?? 0) + " flagged";
      s1.className = "imp-state ok";
      if (lastStats.flag_only) { s2.textContent = "Skipped (dry run)"; s2.className = "imp-state warn"; }
      else if (lastStats.gen_info) { s2.textContent = "Done · " + Fastt.fmtInt(lastStats.processed ?? 0) + " profiled"; s2.className = "imp-state ok"; }
      else { s2.textContent = "Nothing to profile"; s2.className = "imp-state"; }
    } else {
      s1.textContent = "Waiting"; s1.className = "imp-state";
      s2.textContent = "Waiting"; s2.className = "imp-state";
    }
    s3.textContent = chatCfg.engage_old_fans ? "On — old-fan mode live" : "Off — AI still skips them";
    s3.className = "imp-state" + (chatCfg.engage_old_fans ? " ok" : " warn");

    const total = lastStats ? (lastStats.old_fans ?? 0) : 0;
    const done = lastStats ? (lastStats.processed ?? 0) : 0;
    const pct = total ? Math.round((done / total) * 100) : 0;
    $("#im-pl").textContent = !lastStats ? "0% · not started"
      : total ? pct + "% · " + Fastt.fmtInt(done) + " of " + Fastt.fmtInt(total) + " fans profiled"
      : "0% · last run matched no fans";
    $("#im-bar").style.width = pct + "%";

    const targetLines = [
      ...(Array.isArray(p.fan_ids) ? p.fan_ids.map(String) : []),
      ...(Array.isArray(p.usernames) ? p.usernames.map((u) => "@" + u) : []),
    ];
    $("#im-targets").value = targetLines.join("\n");
    $("#im-model").value = p.model ?? "";
    $("#im-batch").value = p.recent_limit ?? "";
    $("#im-limit").value = p.limit ?? "";
    $("#im-by").value = p.recent_by === "messaged" ? "messaged" : "subscribed";
    $("#im-flagonly").classList.toggle("on", !!p.flag_only);
    $("#im-push").classList.toggle("on", !!p.push_to_of);
    $("#im-reprocess").classList.toggle("on", !!p.reprocess);
    if (explicitN) {
      $("#im-batch-hint").innerHTML = "<b>" + Fastt.fmtInt(explicitN) + "</b> fan"
        + (explicitN === 1 ? " is" : "s are") + " explicitly targeted above. Explicit targeting "
        + "<b>wins</b> — this batch size and order are ignored for the run.";
    } else {
      $("#im-batch-hint").innerHTML = "With no <code>fan_ids</code> / <code>usernames</code> targeted, the run takes the "
        + "<b>" + (p.recent_limit ? Fastt.fmtInt(p.recent_limit) : "N") + "</b> most recently "
        + (p.recent_by === "messaged" ? "<b>messaged</b>" : "<b>subscribed</b>") + " fans. "
        + "Leave the batch size empty and the run has nothing to do — it exits with <code>old_fans: 0</code>.";
    }
    renderLastRun();
  }

  const LR_TILES = [
    ["old_fans", "old fans in batch"],
    ["flagged", "flagged pre-AI"],
    ["processed", "sent to profiling"],
    ["already_onboarded", "already onboarded"],
  ];
  function renderLastRun() {
    const meta = $("#im-lr-meta"), tiles = $("#im-lr-stats"), list = $("#im-lr-runs");
    if (!runs.length) {
      meta.textContent = "never run";
      tiles.innerHTML = '<div class="imp-empty" style="grid-column:1/-1">This creator has <b>never run the import</b>, '
        + "so the run log is empty. Set a batch size under Advanced, then press <b>Start import</b> — the breakdown lands here.</div>";
      list.innerHTML = "";
      return;
    }
    const last = runs[0], s = lastStats || {};
    const flags = [];
    if (s.flag_only) flags.push("dry run (flag only)");
    if (s.note) flags.push(String(s.note));
    if (s.unresolved_usernames) flags.push(s.unresolved_usernames + " unresolved @handles");
    if (s.duration_ms != null) flags.push((s.duration_ms / 1000).toFixed(1) + "s");
    meta.textContent = (last.status || "?") + " · " + Fastt.fmtAgo(last.started_at)
      + (flags.length ? " · " + flags.join(" · ") : "");
    tiles.innerHTML = LR_TILES.map(([k, lbl]) => {
      const v = Number(s[k] || 0);
      return '<div class="imp-stat"><b class="' + (v ? "" : "zero") + '">' + Fastt.fmtInt(v)
        + "</b><span>" + esc(lbl) + "</span></div>";
    }).join("");
    const sub = [];
    if (s.gen_info) sub.push("gen_info: " + esc(JSON.stringify(s.gen_info).slice(0, 160)));
    if (s.apply_profiles) sub.push("apply_profiles: " + esc(JSON.stringify(s.apply_profiles).slice(0, 160)));
    if (last.error_text) sub.push("error: " + esc(last.error_text.slice(0, 200)));
    list.innerHTML = (sub.length
        ? '<div class="imp-run" style="display:block;line-height:1.6">' + sub.join("<br>") + "</div>" : "")
      + runs.map((r) => {
        const st = parseStats(r) || {};
        const cls = r.status === "ok" ? "ok" : (r.status === "error" ? "err" : "");
        return '<div class="imp-run"><i class="' + cls + '"></i><span>' + esc(r.status || "?")
          + '</span><span class="ago">' + esc(Fastt.fmtAgo(r.started_at)) + "</span>"
          + '<span class="num">' + Fastt.fmtInt(st.old_fans || 0) + " found · "
          + Fastt.fmtInt(st.flagged || 0) + " flagged · " + Fastt.fmtInt(st.processed || 0) + " profiled</span></div>";
      }).join("");
  }

  if (!(await loadAll())) return;
  render();
  Fastt.liveBadge($("#im-title"));
  Fastt.liveBadge($("#im-lastrun > .fx-card-h"));
  // The old-fan question cadence is a CODE default (ai_chatter._DEFAULTS
  // ["old_fan_question_every"]); /admin/ai-chatter-config never returns it and its
  // validator drops unknown keys, so there is nothing to edit anywhere.
  Fastt.staticBadge($("#im-cadence").parentElement.querySelector("h4"), "CODE DEFAULT · NOT EDITABLE");

  // ── config saves (payload knobs only — never is_enabled) ─────
  // The rule payload IS the form: rebuild it from the knobs so clearing a field
  // removes the key instead of persisting a null the automation then ignores.
  async function savePayload() {
    try {
      rule = await Fastt.upsertRule(KIND, { payload: livePayload() },
        { name: "Onboard pre-AI fans", every_seconds: 2700, is_enabled: false, payload: {} });
      Fastt.saved(); render();
    } catch (e) { Fastt.oops(e); render(); }
  }

  document.addEventListener("change", (e) => {
    if (["im-batch", "im-limit", "im-by", "im-targets", "im-model"].includes(e.target.id)) savePayload();
  });
  document.addEventListener("click", (e) => {
    if (e.target.closest("#im-flagonly") || e.target.closest("#im-push")
        || e.target.closest("#im-reprocess")) savePayload();
  });

  // ── the master switch: schedules the rule on its 45-min timer ──
  $("#im-switch").addEventListener("click", async (e) => {
    e.stopPropagation();
    const wantOn = !(rule && rule.is_enabled);
    if (wantOn && !confirm(
      "Turn the import rule ON?\n\n"
      + "This is a ONE-SHOT job living on a repeating rule: while it is on, the executor re-runs "
      + "process_old_fans every " + Math.round(((rule && rule.every_seconds) || 2700) / 60) + " minutes, "
      + "spending LLM budget on profiling each time.\n\n"
      + "For a single pass, leave this off and press “Start import” instead.")) {
      render(); return;
    }
    if (!wantOn && !confirm("Turn the import rule off? No further scheduled passes will run.")) {
      render(); return;
    }
    try {
      rule = await Fastt.upsertRule(KIND, { is_enabled: wantOn },
        { name: "Onboard pre-AI fans", every_seconds: 2700, payload: {} });
      Fastt.saved(); render();
    } catch (err) { Fastt.oops(err); render(); }
  });

  // ── Start import: an explicit, confirmed one-shot ────────────
  $("#im-refresh").addEventListener("click", async () => {
    await reloadRuns(); render(); Fastt.toast("Run log refreshed");
  });

  $("#im-start").addEventListener("click", async () => {
    const p = livePayload();
    const explicitN = (Array.isArray(p.fan_ids) ? p.fan_ids.length : 0)
      + (Array.isArray(p.usernames) ? p.usernames.length : 0);
    const batchTxt = explicitN
      ? explicitN + " specifically targeted fan" + (explicitN === 1 ? "" : "s")
      : p.recent_limit
        ? p.recent_limit + " most recently " + (p.recent_by === "messaged" ? "messaged" : "subscribed") + " fans"
        : null;
    if (!batchTxt) {
      Fastt.toast("Target some fans or set a batch size under Advanced first — otherwise the run has nothing to do", "err");
      return;
    }
    const lines = [
      "Start the one-time import for this creator?",
      "",
      "Batch: " + batchTxt,
      p.flag_only
        ? "Dry run: flags only — no profiles are written, nothing is pushed to OnlyFans."
        : "It will flag those fans pre-AI (skip_list rows) and then run gen_info + apply_profiles over them, which SPENDS LLM BUDGET.",
      p.push_to_of ? "Nicknames and notes WILL be written onto OnlyFans." : "Profiles stay inside fastt.",
      "",
      "No fan is messaged by this job.",
    ];
    if (!confirm(lines.join("\n"))) return;

    const btn = $("#im-start");
    btn.disabled = true; btn.textContent = "Starting…";
    const startedAt = Date.now();
    try {
      const out = await Fastt.post("/admin/automation/enqueue",
        { account_id: ACCT, kind: KIND, payload: p });
      Fastt.saved("Import job #" + (out.enqueued_job_id ?? "?") + " queued");
      watchRun(startedAt);
    } catch (e) {
      Fastt.oops(e);
      btn.disabled = false;
      btn.innerHTML = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9"><path d="M7 5l12 7-12 7V5z" stroke-linejoin="round"/></svg>Start import';
    }
  });

  function watchRun(startedAt) {
    const btn = $("#im-start"), bar = $("#im-bar"), pl = $("#im-pl");
    const before = runs[0] ? runs[0].id : null;
    let ticks = 0;
    $("#im-bar").parentElement.classList.add("run");
    pl.textContent = "queued — waiting for the executor…";
    bar.style.width = "6%";
    clearInterval(polling);
    polling = setInterval(async () => {
      ticks += 1;
      await reloadRuns();
      const fresh = runs[0];
      if (fresh && String(fresh.id) !== String(before)) {
        if (fresh.completed_at) {
          clearInterval(polling); polling = null;
          $("#im-bar").parentElement.classList.remove("run");
          btn.disabled = false;
          btn.innerHTML = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9"><path d="M7 5l12 7-12 7V5z" stroke-linejoin="round"/></svg>Start import';
          render();
          Fastt.saved("Import finished · " + (fresh.status || "?"));
          return;
        }
        pl.textContent = "running… (started " + Fastt.fmtAgo(fresh.started_at) + ")";
        bar.style.width = "55%";
      } else {
        bar.style.width = Math.min(45, 6 + ticks * 4) + "%";
      }
      if (ticks > 100 || Date.now() - startedAt > 5 * 60000) {
        clearInterval(polling); polling = null;
        $("#im-bar").parentElement.classList.remove("run");
        btn.disabled = false;
        btn.innerHTML = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9"><path d="M7 5l12 7-12 7V5z" stroke-linejoin="round"/></svg>Start import';
        pl.textContent = "still running — press Refresh to re-read the run log";
      }
    }, 3000);
  }
});
