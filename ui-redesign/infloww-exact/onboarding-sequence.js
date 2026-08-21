Fastt.ready(async () => {
  const $ = Fastt.$, $$ = Fastt.$$, esc = Fastt.esc;
  const KIND = "send_followup";
  const ACCT = Fastt.account();
  const DEF_STEPS = [26, 64, 256];
  const SLOT_LABEL = {
    morning_1: "Early morning", morning_2: "Morning", afternoon_1: "Early afternoon",
    afternoon_2: "Late afternoon", evening: "Evening", night: "Night",
  };
  // send_followup._IMAGE_FOLDER_NAMES — the folder fallback when a slot has no
  // configured time_images id. First of these WITH photos wins (not "Teasers").
  const AUTO_FOLDERS = ["bot", "welcome script", "welcome"];
  const thumbUrl = (id) => (ACCT && id)
    ? "/admin/vault-ai/thumb?account_id=" + encodeURIComponent(ACCT) + "&media_id=" + encodeURIComponent(id)
    : "";

  const [rulesRes, cfgRes, statsRes, listsRes] = await Promise.allSettled([
    Fastt.rulesByKind(),
    Fastt.get("/admin/account-config"),
    Fastt.get("/admin/stats/per-automation",
      { from: new Date(Date.now() - 7 * 864e5).toISOString().slice(0, 10) }),
    Fastt.get("/api/of/v2/vault/lists", { limit: 50 }),
  ]);
  if (rulesRes.status !== "fulfilled") { Fastt.oops(rulesRes.reason); return; }
  let rule = (rulesRes.value[KIND] || [])[0] || null;
  const welcomeRule = (rulesRes.value.send_welcome || [])[0] || null;
  const cfgOk = cfgRes.status === "fulfilled";
  const modelOptions = cfgOk ? (cfgRes.value.model_options || []) : [];
  const acctModel = cfgOk ? cfgRes.value.config.model : null;
  const timeImages = (cfgOk && cfgRes.value.config.time_images) || {};
  const vaultLists = listsRes.status === "fulfilled"
    ? (listsRes.value.list || listsRes.value.rows || []) : null;

  const setSt = (id, text, cls) => { const el = $(id); if (!el) return;
    el.className = "fx-st" + (cls ? " " + cls : ""); el.innerHTML = "<i></i>" + esc(text); };
  const setChip = (el, on) => { if (!el) return;
    el.classList.toggle("on", on); el.innerHTML = "<i></i>" + (on ? "On" : "Off"); };

  const everySec = () => (rule && rule.every_seconds) || 2700;
  const everyMin = () => Math.max(1, Math.round(everySec() / 60));

  function render() {
    const on = !!(rule && rule.is_enabled);
    const p = (rule && rule.payload) || {};
    const dry = !!p.dry_run;
    setSt("#sq-st-run", on ? (dry ? "Running · test mode (no sends)" : "Running") : "Off",
      on ? (dry ? "warn" : "ok") : "");
    const last = rule && rule.last_run && rule.last_run.completed_at;
    // next_due_at is tz-NAIVE UTC — parseUtc stamps the missing Z, else this is
    // always negative and the readout says "due now" forever.
    const ndAt = on && rule.next_due_at ? Fastt.parseUtc(rule.next_due_at) : null;
    const nd = ndAt ? ndAt.getTime() - Date.now() : null;
    setSt("#sq-st-sweep", "Sweeps every " + everyMin() + " min · "
      + (last ? "last " + Fastt.fmtAgo(last) : "never swept")
      + (nd !== null ? " · next " + (nd > 0 ? "in " + Math.max(1, Math.round(nd / 6e4)) + " min" : "due now") : ""));
    setSt("#sq-st-model", p.model || acctModel || "account default model");
    $("#sq-enable").classList.toggle("on", on);

    // schedule + test-mode knobs
    $("#sq-every").value = everyMin();
    $("#sq-dry").classList.toggle("on", dry);
    const qhStart = $("#sq-qh-start"), qhEnd = $("#sq-qh-end");
    if (!qhStart.options.length) {  // build the 0–23 hour options once
      const hrs = Array.from({ length: 24 }, (_, h) =>
        "<option value=\"" + h + "\">" + String(h).padStart(2, "0") + ":00</option>").join("");
      qhStart.innerHTML = hrs; qhEnd.innerHTML = hrs;
    }
    const qh = Array.isArray(rule && rule.quiet_hours) && rule.quiet_hours.length === 2
      ? rule.quiet_hours : null;
    const qhOn = !!(qh && !(qh[0] === 0 && qh[1] === 0));
    $("#sq-qh-on").classList.toggle("on", qhOn);
    qhStart.value = qh ? qh[0] : 22;
    qhEnd.value = qh ? qh[1] : 8;
    qhStart.disabled = qhEnd.disabled = !qhOn;
    $("#sq-qh-sub").textContent = qhOn
      ? "paused " + String(qh[0]).padStart(2, "0") + ":00–" + String(qh[1]).padStart(2, "0") + ":00 creator time"
      : "off — the sweep runs around the clock";
    setChip($("#sq-chip0"), !!(welcomeRule && welcomeRule.is_enabled));
    $$(".sq-chip").forEach((c) => setChip(c, on));

    const sh = Array.isArray(p.step_hours) && p.step_hours.length >= 3 ? p.step_hours : DEF_STEPS;
    [["#sq-t1", "#sq-h1"], ["#sq-t2", "#sq-h2"], ["#sq-t3", "#sq-h3"]].forEach(([t, i], n) => {
      $(t).textContent = "+" + sh[n] + " h"; $(i).value = sh[n];
    });
    $("#sq-g1").textContent = "no reply for " + sh[0] + " hours";
    $("#sq-g2").textContent = "still nothing at " + sh[1] + " hours";
    $("#sq-g3").textContent = "still nothing at " + sh[2] + " hours";
    $("#sq-guard").value = p.exclude_replied_hours ?? 12;
    $("#sq-limit").value = p.limit ?? 200;
    $("#sq-img").classList.toggle("on", p.with_image !== false);
    const sel = $("#sq-model");
    sel.innerHTML = "<option value=\"\">account default</option>" + modelOptions.map((m) =>
      "<option" + (m === p.model ? " selected" : "") + ">" + esc(m) + "</option>").join("");
    sel.value = p.model || "";
    paintMedia();
  }

  if (statsRes.status === "fulfilled") {
    const row = (statsRes.value.rows || []).find((r) => r.automation === "followup");
    setSt("#sq-st-sent", (row ? row.messages_sent : 0) + " nudges sent this week");
  } else setSt("#sq-st-sent", "nudge count unavailable");

  // ── the folder the auto image path will ACTUALLY use ─────────
  const autoFolder = (() => {
    if (!vaultLists) return undefined;                   // unknown
    const byName = {};
    for (const f of vaultLists) {
      const n = String(f.name || "").trim().toLowerCase();
      if (f && f.id != null && !(n in byName)) byName[n] = f;
    }
    return AUTO_FOLDERS.map((n) => byName[n]).find((f) => f && (f.photosCount | 0) > 0) || null;
  })();

  function folderHint() {
    const pinned = Object.keys(timeImages).length;
    const sub = $("#sq-img-sub"), hint = $("#sq-folder-hint");
    sub.textContent = pinned
      ? pinned + " of 6 time slots have a photo pinned in Brain & Persona"
      : "no slot photos pinned — the folder fallback picks one";
    let tail;
    if (autoFolder === undefined) {
      tail = "the first of your <b>bot</b> / <b>welcome script</b> / <b>welcome</b> folders that has photos (the vault list didn’t load, so we can’t name it right now).";
    } else if (autoFolder) {
      tail = "your <b>“" + esc(autoFolder.name) + "”</b> folder (" + (autoFolder.photosCount | 0)
        + " photos) — send_followup tries <b>bot</b>, then <b>welcome script</b>, then <b>welcome</b>, first with photos wins.";
    } else {
      tail = "…nothing: none of <b>bot</b> / <b>welcome script</b> / <b>welcome</b> exists with photos on this account, so an unpinned slot sends <b>text only</b>.";
    }
    hint.innerHTML = "There are <b>6</b> time slots (early morning, morning, early afternoon, late afternoon, "
      + "evening, night), not four. A slot with a photo pinned in "
      + "<a href=\"onboarding-welcome.html\" style=\"color:#7d97f8;text-decoration:none\">Welcome → Advanced</a> "
      + "wins outright; every other slot falls back to " + tail;
  }
  folderHint();

  // ── per-sweep counts on the gate cards ───────────────────────
  const lastStats = (rule && rule.last_run && rule.last_run.stats) || null;
  (function gateCounts() {
    const NA = {
      __none_quarantine: ["not counted", "The 7-day quarantine is applied inside the per-fan cooldown check — the sweep does not log it separately."],
      __none_prefilter: ["pre-filtered", "Cold chats never enter the eligible set, so the sweep has no counter for them."],
    };
    $$("[data-gate]").forEach((el) => {
      const key = el.dataset.gate;
      if (NA[key]) {
        el.className = "gn na"; el.textContent = NA[key][0]; el.title = NA[key][1]; return;
      }
      if (!lastStats) { el.className = "gn na"; el.textContent = "no sweep yet";
        el.title = "This automation has never run for this creator, so there are no per-sweep counts."; return; }
      const v = Number(lastStats[key] || 0);
      el.className = "gn" + (v ? "" : " zero");
      el.textContent = Fastt.fmtInt(v) + " last sweep";
      el.title = "send_followup run stats · " + key;
    });
    const elig = $("#sq-n-eligible"), skips = $("#sq-n-skips");
    if (!lastStats) {
      elig.className = "h4n zero"; elig.textContent = "never swept";
      skips.className = "h4n zero"; skips.textContent = "never swept";
      $("#sq-eligible-hint").innerHTML = "This creator has no recorded follow-up sweep yet, so there is no eligible count to show. "
        + "The numbers on these cards fill in from the run log the first time the drip runs.";
      return;
    }
    const n = Number(lastStats.eligible || 0);
    elig.className = "h4n" + (n ? "" : " zero");
    elig.textContent = Fastt.fmtInt(n) + " eligible";
    const skipped = ["skipped_unread", "skipped_locked", "skipped_cooldown", "skipped_guard"]
      .reduce((a, k) => a + Number(lastStats[k] || 0), 0);
    skips.className = "h4n" + (skipped ? "" : " zero");
    skips.textContent = Fastt.fmtInt(skipped) + " held last sweep";
    $("#sq-eligible-hint").innerHTML = "Last sweep "
      + (rule.last_run.completed_at ? "(" + esc(Fastt.fmtAgo(rule.last_run.completed_at)) + ") " : "")
      + "found <b>" + Fastt.fmtInt(n) + "</b> eligible fans, advanced <b>"
      + Fastt.fmtInt(lastStats.state_advanced || 0) + "</b> through the drip and sent <b>"
      + Fastt.fmtInt(lastStats.followups_sent || 0) + "</b> nudge"
      + ((lastStats.followups_sent || 0) === 1 ? "" : "s") + "."
      + (lastStats.errors ? " <b>" + Fastt.fmtInt(lastStats.errors) + "</b> error(s)." : "")
      + (lastStats.cap_hit ? " The daily AI cap was hit." : "");
  })();

  // ── step media: the real slot photo, on every step with_image covers ──
  let curSlot = null;
  function paintMedia() {
    const wOn = !welcomeRule || !welcomeRule.payload || welcomeRule.payload.with_image !== false;
    const fOn = !rule || !rule.payload || rule.payload.with_image !== false;
    const slotId = curSlot ? timeImages[curSlot] : null;
    for (let i = 0; i <= 3; i++) {
      const img = $('[data-media="' + i + '"]');
      const note = $("#sq-note" + i);
      if (!img) continue;
      const on = i === 0 ? wOn : fOn;
      const id = i === 0 ? (welcomeImage || slotId) : slotId;
      if (on && id) {
        img.src = thumbUrl(id); img.classList.add("on");
        img.onerror = () => { img.classList.remove("on"); };
        if (note) note.textContent = "photo #" + id + " · " + (SLOT_LABEL[curSlot] || curSlot || "current") + " slot, pinned for this creator";
      } else {
        img.classList.remove("on"); img.removeAttribute("src");
        if (note) note.textContent = !on
          ? "photo off for this step — text only"
          : (autoFolder === undefined
              ? "no photo pinned for this slot — one is picked from the fallback vault folder at send time"
              : autoFolder
                ? "no photo pinned for this slot — one is picked from your “" + autoFolder.name + "” folder at send time"
                : "no photo pinned and no fallback folder with photos — this step sends text only");
      }
    }
  }

  // ── T0 welcome: composed for REAL (verbatim, no LLM call) ────
  let welcomeImage = null;
  (async function composeWelcome() {
    const note = $("#sq-note0");
    try {
      const res = await Fastt.post("/admin/automation-preview",
        { account_id: ACCT, kind: "send_welcome" });
      const b = res.bubbles || [res.text || ""];
      curSlot = res.slot || null;
      welcomeImage = res.image || null;
      $('[data-txt="0a"]').textContent = b[0] || "";
      const second = $('[data-b2="0"]');
      if (b[1]) { second.style.display = ""; $('[data-txt="0b"]').textContent = b[1]; }
      else second.style.display = "none";
      Fastt.liveBadge($("#sq-pv0"));
      paintMedia();
    } catch (e) {
      $('[data-txt="0a"]').textContent = "couldn’t compose the welcome";
      $('[data-b2="0"]').style.display = "none";
      if (note) note.textContent = "The welcome composer returned an error ("
        + (((e.body && (e.body.detail || e.body.error)) || e.message || "")).toString().slice(0, 120)
        + ") — nothing here is invented, so the bubble is left empty.";
      Fastt.staticBadge($("#sq-pv0"), "COMPOSE FAILED");
    }
  })();

  render();
  Fastt.liveBadge($(".sec-title"));
  // Steps 1–3 are illustrative until someone spends an AI call on a live sample:
  // send_followup composes fresh per fan, and this relay's preview route has no
  // `step` field, so even a successful sample can only ever be step 1.
  ["#sq-pv1", "#sq-pv2", "#sq-pv3"].forEach((id) => Fastt.staticBadge($(id), "SAMPLE"));
  ["#sq-fixed-gap", "#sq-fixed-pause", "#sq-fixed-rest"].forEach((id) =>
    Fastt.staticBadge($(id), "FIXED IN CODE"));

  // ── explicit live sample of a follow-up (one LLM call) ───────
  $("#sq-sample").addEventListener("click", async () => {
    const btn = $("#sq-sample"), note = $("#sq-sample-note");
    btn.disabled = true; btn.textContent = "Composing…";
    note.style.display = "none";
    try {
      const res = await Fastt.post("/admin/automation-preview",
        { account_id: ACCT, kind: KIND });
      const txt = (res.text || "").trim();
      if (!txt) throw new Error("the composer returned an empty line");
      const holder = $('[data-txt="1"]');
      holder.textContent = txt;
      const badge = $("#sq-pv1").querySelector(".ft-static");
      if (badge) badge.remove();
      Fastt.liveBadge($("#sq-pv1"));
      curSlot = res.slot || curSlot;
      paintMedia();
      note.className = "fx-note";
      note.style.display = "";
      note.innerHTML = "<span>Live sample composed for step <b>1</b>. The preview route takes no <code>step</code> "
        + "parameter, so steps 2 and 3 cannot be sampled separately — they stay illustrative. "
        + "Every fan gets their own freshly-written line at send time; this is one draw, not the copy that ships.</span>";
    } catch (e) {
      const raw = e.body && (e.body.detail || e.body.error || (typeof e.body === "string" ? e.body : ""));
      const detail = (raw || (e.status ? "" : e.message) || "").toString().trim();
      note.className = "fx-note warn";
      note.style.display = "";
      note.innerHTML = "<span><b>Live sample unavailable.</b> <code>POST /admin/automation-preview "
        + "{kind:'send_followup'}</code> failed on this relay"
        + (e.status ? " (HTTP " + e.status + ")" : "") + (detail ? " — " + esc(detail.slice(0, 160)) : "")
        + ". The follow-up composer needs a working LLM call, so nothing live can be shown — steps 1–3 below "
        + "stay marked SAMPLE rather than pretending.</span>";
    } finally {
      btn.disabled = false; btn.textContent = "Compose a live sample (1 AI call)";
    }
  });

  async function saveRule(patch) {
    try {
      rule = await Fastt.upsertRule(KIND, patch, {
        name: "Follow up quiet fans", every_seconds: 3300, is_enabled: false,
        payload: { step_hours: DEF_STEPS, with_image: true },
      });
      Fastt.saved(); render();
    } catch (e) { Fastt.oops(e); render(); }
  }
  const patchPayload = (changes) => saveRule({ payload: { ...((rule && rule.payload) || {}), ...changes } });
  const num = (el, lo) => { const n = parseInt(el.value, 10); return isFinite(n) && n >= lo ? n : null; };

  document.addEventListener("click", (e) => {
    if (e.target.closest("#sq-enable")) {
      // The fx-kit handler already flipped the switch's .on class before this
      // runs. Turning the drip ON starts real fan sends on the next tick, so
      // gate that direction behind a confirm and revert the toggle on cancel.
      const wantOn = $("#sq-enable").classList.contains("on");
      if (wantOn && !confirm(
        "Turn the follow-up drip ON? Silent fans will start getting AI-written nudges on the next sweep. "
        + "(Tip: flip on Test mode in Advanced first to watch it run with no DMs sent.)")) {
        $("#sq-enable").classList.remove("on"); return;
      }
      saveRule({ is_enabled: wantOn });
    } else if (e.target.closest("#sq-img")) {
      patchPayload({ with_image: $("#sq-img").classList.contains("on") });
      folderHint();
    } else if (e.target.closest("#sq-dry")) {
      patchPayload({ dry_run: $("#sq-dry").classList.contains("on") });
    } else if (e.target.closest("#sq-qh-on")) {
      const qhOn = $("#sq-qh-on").classList.contains("on");
      $("#sq-qh-start").disabled = $("#sq-qh-end").disabled = !qhOn;
      const s = parseInt($("#sq-qh-start").value, 10) || 0;
      const en = parseInt($("#sq-qh-end").value, 10) || 0;
      saveRule({ quiet_hours: qhOn ? [s, en] : [0, 0] });  // [0,0] clears server-side
    }
  });
  document.addEventListener("change", (e) => {
    const t = e.target;
    if (["sq-h1", "sq-h2", "sq-h3"].includes(t.id)) {
      const hs = [num($("#sq-h1"), 1), num($("#sq-h2"), 1), num($("#sq-h3"), 1)];
      if (hs.every((h) => h !== null)) patchPayload({ step_hours: hs });
    } else if (t.id === "sq-guard" && num(t, 0) !== null)
      patchPayload({ exclude_replied_hours: num(t, 0) });
    else if (t.id === "sq-limit" && num(t, 1) !== null)
      patchPayload({ limit: num(t, 1) });
    else if (t.id === "sq-every" && num(t, 1) !== null)
      saveRule({ every_seconds: num(t, 1) * 60 });   // minutes → seconds; server clamps 30s..30d
    else if ((t.id === "sq-qh-start" || t.id === "sq-qh-end")
             && $("#sq-qh-on").classList.contains("on")) {
      const s = parseInt($("#sq-qh-start").value, 10) || 0;
      const en = parseInt($("#sq-qh-end").value, 10) || 0;
      saveRule({ quiet_hours: [s, en] });
    } else if (t.id === "sq-model") {
      const p = { ...((rule && rule.payload) || {}) };
      if (t.value) p.model = t.value; else delete p.model;
      saveRule({ payload: p });
    }
  });
});
