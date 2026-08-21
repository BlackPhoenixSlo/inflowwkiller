Fastt.ready(async () => {
  const $ = Fastt.$, $$ = Fastt.$$, esc = Fastt.esc;
  const KIND = "send_welcome";
  const ACCT = Fastt.account();
  const SLOT_LABEL = {
    morning_1: "Early morning", morning_2: "Morning", afternoon_1: "Early afternoon",
    afternoon_2: "Late afternoon", evening: "Evening", night: "Night",
  };
  const CFG_KEYS = ["persona","welcome_rules","location","language","timezone","utc_offset",
                    "daily_cost_cap_cents","model","model_by_purpose","time_activities","time_images"];
  // send_welcome._IMAGE_FOLDER_NAMES — first of these folders that HAS photos wins
  // when a slot is left on auto. A per-slot time_images id beats the folder entirely.
  const AUTO_FOLDERS = ["bot", "welcome script", "welcome"];
  const thumbUrl = (id) => (ACCT && id)
    ? "/admin/vault-ai/thumb?account_id=" + encodeURIComponent(ACCT) + "&media_id=" + encodeURIComponent(id)
    : "";

  // ── load: brain config + rule + 7-day welcome stats + run log + vault folders ──
  const [cfgRes, ruleRes, statsRes, runsRes, listsRes, ofTplRes, ofSetRes] = await Promise.allSettled([
    Fastt.get("/admin/account-config"),
    Fastt.rule(KIND),
    Fastt.get("/admin/stats/per-automation",
      { from: new Date(Date.now() - 7 * 864e5).toISOString().slice(0, 10) }),
    Fastt.get("/admin/stats/automation-runs", { kind: KIND, limit: 5 }),
    Fastt.get("/api/of/v2/vault/lists", { limit: 50 }),
    // OnlyFans' OWN welcome (reply_on_subscribe slot) + the master toggle that
    // decides whether OF actually fires it — read so we can flag a double-welcome.
    Fastt.get("/api/of/v2/messages/templates"),
    Fastt.get("/api/of/v2/users/me/settings"),
  ]);
  if (cfgRes.status !== "fulfilled") { Fastt.oops(cfgRes.reason); return; }
  const cfg = {};
  for (const k of CFG_KEYS) cfg[k] = cfgRes.value.config[k];
  cfg.time_activities = cfg.time_activities || {};
  cfg.time_images = cfg.time_images || {};
  let rule = ruleRes.status === "fulfilled" ? ruleRes.value : null;
  const runs = runsRes.status === "fulfilled" ? (runsRes.value.runs || []) : [];
  const vaultLists = listsRes.status === "fulfilled"
    ? ((listsRes.value.list || listsRes.value.rows || [])) : null;

  // ── OnlyFans' OWN welcome (reply_on_subscribe) + master flag ──
  const ofTplOk = ofTplRes.status === "fulfilled";
  const ofSetOk = ofSetRes.status === "fulfilled";
  const ofTpl = ofTplOk
    ? (Array.isArray(ofTplRes.value) ? ofTplRes.value : (ofTplRes.value.list || ofTplRes.value.rows || []))
        .find((t) => t && t.template === "reply_on_subscribe") || null
    : null;
  // OF's template.isActive is decoupled from whether OF actually fires it — the
  // truth is /users/me/settings.replyOnSubscribe (see of_welcome_isactive note).
  let ofActiveNow = ofSetOk ? !!ofSetRes.value.replyOnSubscribe : null;
  const stripHtml = (s) => String(s || "")
    .replace(/<br\s*\/?>/gi, "\n").replace(/<\/p\s*>/gi, "\n").replace(/<[^>]+>/g, "")
    .replace(/&nbsp;/g, " ").replace(/&amp;/g, "&").replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">").replace(/&quot;/g, '"').replace(/&#39;/g, "'").trim();

  const setSt = (id, text, cls) => { const el = $(id); if (!el) return;
    el.className = "fx-st" + (cls ? " " + cls : ""); el.innerHTML = "<i></i>" + esc(text); };
  const runStats = (r) => { if (!r || !r.stats_json) return null;
    try { return JSON.parse(r.stats_json); } catch { return null; } };

  // ── week counter ─────────────────────────────────────────────
  let week = null;
  if (statsRes.status === "fulfilled") {
    const row = (statsRes.value.rows || []).find((r) => r.automation === "welcome");
    week = row ? row.messages_sent : 0;
    setSt("#st-week", week + " welcomed this week");
  } else setSt("#st-week", "welcome count unavailable");

  // ── rule / status strip ──────────────────────────────────────
  function effectiveModel() {
    const p = (rule && rule.payload) || {};
    return p.model || cfg.model || null;
  }
  function renderRule() {
    const on = !!(rule && rule.is_enabled);
    const p = (rule && rule.payload) || {};
    setSt("#st-run", on ? "Running" : "Off", on ? "ok" : "");
    setSt("#st-every", rule && rule.every_seconds
      ? "Checks the sub feed every " + Math.round(rule.every_seconds / 60) + " min"
      : "No welcome rule yet — flip the switch to create one");
    setSt("#st-last", rule && rule.last_run && rule.last_run.completed_at
      ? "Last run " + Fastt.fmtAgo(rule.last_run.completed_at) : "Never run");
    const em = effectiveModel();
    setSt("#st-model", em
      ? (p.model ? em + " (welcome override)" : em + " (account default)")
      : "no model set — account default");
    $("#wk-state").textContent = on ? "Running" : "Off";
    $("#wk-enable").classList.toggle("on", on);
    $("#wk-card").classList.toggle("on", on);
    $("#wk-photo-check").classList.toggle("on", p.with_image !== false);
    $("#wk-restyle").classList.toggle("on", p.restyle !== false);
    $("#wk-maxw").value = p.max_welcomes ?? 25;
    $("#wk-limit").value = p.limit ?? 50;
    $("#wk-guard").value = p.guard_hours ?? 12;
    $("#wk-newout").value = p.new_max_outbound ?? 2;
    $("#wk-newin").value = p.new_max_inbound ?? 8;
    $("#wk-every").value = rule && rule.every_seconds ? Math.round(rule.every_seconds / 60) : 5;
    // next_due_at is tz-NAIVE UTC — parseUtc stamps the missing Z, else this is
    // always negative and the readout says "due now" forever.
    const ndAt = rule && rule.next_due_at ? Fastt.parseUtc(rule.next_due_at) : null;
    const nd = ndAt ? ndAt.getTime() - Date.now() : null;
    $("#wk-kv").innerHTML = "<b>" + esc(week === null ? "—" : String(week)) + "</b> welcomed this week · next feed check "
      + (on && nd !== null ? "<b>" + (nd > 0 ? "in " + Math.max(1, Math.round(nd / 6e4)) + " min" : "due now") + "</b>"
                           : "<b>—</b> (" + (on ? "unscheduled" : "off") + ")");
    renderModelSel();
    if (typeof renderOfWelcome === "function") renderOfWelcome();
  }

  // ── model: the RULE payload override, not the global brain model ──
  function renderModelSel() {
    const p = (rule && rule.payload) || {};
    const opts = (cfgRes.value.model_options || []);
    const sel = $("#wk-model");
    sel.innerHTML = '<option value="">account default'
      + (cfg.model ? " (" + esc(cfg.model) + ")" : "") + "</option>"
      + opts.map((m) => '<option value="' + esc(m) + '"'
          + (m === p.model ? " selected" : "") + ">" + esc(m) + "</option>").join("");
    sel.value = p.model || "";
    $("#wk-model-hint").innerHTML = p.model
      ? "Welcome runs on <b>" + esc(p.model) + "</b> — stored on this automation's rule, so the rest of the brain is untouched."
      : "Falls back to the account model" + (cfg.model ? " (<b>" + esc(cfg.model) + "</b>)" : "")
        + ", set in <a href=\"ai-brain.html\" style=\"color:#7d97f8;text-decoration:none\">Brain &amp; Persona</a>.";
  }

  renderRule();
  Fastt.liveBadge($("#wk-card .fx-tc-title"));

  // ── OnlyFans' own welcome cross-check (double-welcome guard) ──
  function renderOfWelcome() {
    const stateEl = $("#ofw-state"), metaEl = $("#ofw-meta"), body = $("#ofw-body");
    if (!stateEl || !body) return;
    const aiOn = !!(rule && rule.is_enabled);
    if (!ofSetOk || !ofTplOk) {
      stateEl.className = "fx-st"; stateEl.style.height = "24px";
      stateEl.innerHTML = "<i></i>unknown";
      metaEl.textContent = "couldn’t read OnlyFans";
      body.innerHTML = '<div class="wk-empty">Couldn’t read OnlyFans’ own welcome — the '
        + '<code>messages/templates</code> or account-settings call failed, so we can’t say whether OF is also '
        + 'greeting new subs. Check the creator’s OF session and reload.</div>';
      Fastt.staticBadge($("#ofw-card > .fx-card-h"), "NO DATA");
      return;
    }
    const on = !!ofActiveNow;
    const text = ofTpl ? stripHtml(ofTpl.text || ofTpl.displayText) : "";
    stateEl.className = "fx-st " + (on ? "warn" : "ok"); stateEl.style.height = "24px";
    stateEl.innerHTML = "<i></i>" + (on ? "OF welcome ON" : "OF welcome off");
    metaEl.textContent = ofTpl
      ? ((ofTpl.mediaCount ? ofTpl.mediaCount + " photo" + (ofTpl.mediaCount === 1 ? "" : "s") : "text only")
         + (ofTpl.price ? " · $" + Number(ofTpl.price).toFixed(2) : ""))
      : "no OF welcome saved";

    let html = "";
    if (ofTpl && text) {
      html += '<div class="ofw-quote"><span class="qmark">“</span><div class="ofw-qcol">'
        + '<div class="ofw-qtext">' + esc(text) + "</div>"
        + '<div class="ofw-qmeta">OnlyFans’ fixed reply · the same line to every new sub · edit it in '
        + '<a href="onboarding-templates.html" style="color:#7d97f8;text-decoration:none">Templates</a></div>'
        + "</div></div>";
    }
    let note;
    if (on && aiOn) {
      note = '<div class="fx-note warn"><span><b>Both welcomes are on.</b> Every new subscriber gets OnlyFans’ '
        + 'fixed reply above <b>and</b> the AI welcome — two hellos back to back. Turn one off so a new fan hears a '
        + 'single greeting. The AI welcome is name- and time-aware; OF’s is the same line for everyone.</span></div>';
    } else if (on && !aiOn) {
      note = '<div class="fx-note"><span>OnlyFans is sending its fixed reply to new subs; the AI welcome above is '
        + '<b>off</b>, so there’s no clash. Flip the AI welcome on <b>and</b> switch OF’s off to greet each fan by name instead.</span></div>';
    } else if (!on && aiOn) {
      note = '<div class="fx-note"><span><b>Clean.</b> OnlyFans’ own welcome is off, so the AI welcome above is the '
        + '<b>only</b> hello a new sub gets.' + (ofTpl ? ' A saved OF reply still exists (shown above) but won’t fire until switched on.' : '')
        + "</span></div>";
    } else {
      note = '<div class="fx-note warn"><span><b>No welcome is running.</b> OnlyFans’ own welcome is off and the AI '
        + 'welcome above is off too — a brand-new subscriber currently gets nothing. Turn the AI welcome on above '
        + '(recommended), or switch OF’s fixed reply on.</span></div>';
    }
    html += note;

    const canOn = on || !!ofTpl; // can't switch ON a welcome that doesn't exist
    html += '<div class="ofw-actions">'
      + '<button class="fx-btn ' + (on ? "ghost" : "green") + '" id="ofw-toggle"'
      + (canOn ? "" : ' disabled title="No OF welcome saved to switch on — set one in Templates first"') + ">"
      + (on ? "Turn OnlyFans’ welcome off" : "Turn OnlyFans’ welcome on") + "</button>"
      + '<a class="fx-btn ghost" href="onboarding-templates.html" style="text-decoration:none">Edit OF welcome text</a>'
      + "</div>";
    body.innerHTML = html;
    Fastt.liveBadge($("#ofw-card > .fx-card-h"));
  }
  renderOfWelcome();

  // Toggle OF's master replyOnSubscribe flag. This is a settings change, not a
  // send — but it decides whether OF greets future subs, so gate it on confirm.
  document.addEventListener("click", async (e) => {
    if (!e.target.closest("#ofw-toggle")) return;
    const btn = e.target.closest("#ofw-toggle");
    if (btn.disabled) return;
    const turningOn = !ofActiveNow;
    const ok = confirm(turningOn
      ? "Turn ON OnlyFans’ own welcome?\n\nOnlyFans will auto-send its fixed reply to every NEW subscriber from now on. If the AI welcome is also on, new subs get two hellos."
      : "Turn OFF OnlyFans’ own welcome?\n\nOnlyFans will stop sending its fixed auto-reply to new subscribers. The AI welcome (if on) is unaffected.");
    if (!ok) return;
    btn.disabled = true; btn.textContent = "…";
    try {
      await Fastt.patch("/api/of/v2/users/me/reply-on-subscribe", { enabled: turningOn });
      ofActiveNow = turningOn;
      Fastt.saved(turningOn ? "OnlyFans’ welcome on" : "OnlyFans’ welcome off");
      renderOfWelcome();
    } catch (err) { Fastt.oops(err); renderOfWelcome(); }
  });

  // ── brain fields (slot lines, images, rules text) ────────────
  $$("#wk-slots input[data-slot]").forEach((inp) => {
    inp.value = cfg.time_activities[inp.dataset.slot] || "";
  });
  function renderSlotImages() {
    $$("#wk-slots [data-pick]").forEach((box) => {
      const slot = box.dataset.pick;
      const id = cfg.time_images[slot];
      const img = box.querySelector("[data-thumb]");
      const lbl = box.querySelector("[data-img]");
      box.classList.toggle("has", !!id);
      if (id) {
        img.src = thumbUrl(id); img.style.display = "";
        img.onerror = () => { img.style.display = "none"; };
        lbl.textContent = "#" + id;
        box.title = "Photo #" + id + " — click to change";
      } else {
        img.removeAttribute("src"); img.style.display = "none";
        lbl.textContent = "auto · pick…";
        box.title = "No photo pinned — the folder picker chooses one. Click to pin a photo.";
      }
    });
  }
  renderSlotImages();

  // Name the folder the auto path will ACTUALLY use (send_welcome tries bot →
  // welcome script → welcome and takes the first that has photos).
  (function folderHint() {
    const el = $("#wk-folder-hint");
    const head = "The slot photo rides on the greeting bubble. Click a thumbnail to pick from the vault, ✕ to put the slot back on auto. ";
    if (!vaultLists) {
      el.innerHTML = head + "Slots on <b>auto</b> take the first of your <b>bot</b> / <b>welcome script</b> / <b>welcome</b> folders that has photos — the vault list didn’t load, so we can’t say which one that is right now.";
      return;
    }
    const byName = {};
    for (const f of vaultLists) {
      const n = String(f.name || "").trim().toLowerCase();
      if (f && f.id != null && !(n in byName)) byName[n] = f;
    }
    const hit = AUTO_FOLDERS.map((n) => byName[n]).find((f) => f && (f.photosCount | 0) > 0);
    el.innerHTML = head + (hit
      ? "Slots on <b>auto</b> pick by time of day from your <b>“" + esc(hit.name) + "”</b> folder ("
        + (hit.photosCount | 0) + " photos) — send_welcome tries <b>bot</b>, then <b>welcome script</b>, then <b>welcome</b>, first with photos wins."
      : "None of the auto folders (<b>bot</b> / <b>welcome script</b> / <b>welcome</b>) exists with photos on this account, so a slot left on auto sends <b>text only</b>.");
  })();

  $("#wk-rules").value = cfg.welcome_rules || "";

  const saveCfg = Fastt.debounce(async () => { await saveCfgNow(); }, 600);
  async function saveCfgNow() {
    try {
      const out = await Fastt.put("/admin/account-config", { account_id: ACCT, config: cfg });
      for (const k of CFG_KEYS) cfg[k] = out.config[k];
      cfg.time_activities = cfg.time_activities || {}; cfg.time_images = cfg.time_images || {};
      Fastt.saved(); renderSlotImages(); renderModelSel();
    } catch (e) { Fastt.oops(e); }
  }
  document.addEventListener("change", (e) => {
    const t = e.target;
    if (t.matches("#wk-slots input[data-slot]")) {
      const v = t.value.trim();
      if (v) cfg.time_activities[t.dataset.slot] = v; else delete cfg.time_activities[t.dataset.slot];
      saveCfg();
    } else if (t.id === "wk-rules") { cfg.welcome_rules = t.value; saveCfg(); }
  });

  // ── rule saves (enable, cadence, payload knobs) ──────────────
  async function saveRule(patch) {
    try {
      rule = await Fastt.upsertRule(KIND, patch,
        { name: "Welcome new subscribers", every_seconds: 420, is_enabled: false, payload: {} });
      Fastt.saved(); renderRule();
    } catch (e) { Fastt.oops(e); renderRule(); }
  }
  const patchPayload = (changes) => saveRule({ payload: { ...((rule && rule.payload) || {}), ...changes } });

  document.addEventListener("click", (e) => {
    if (e.target.closest("#wk-enable"))
      saveRule({ is_enabled: $("#wk-enable").classList.contains("on") });
    else if (e.target.closest("#wk-photo-check")) {
      patchPayload({ with_image: $("#wk-photo-check").classList.contains("on") });
      applyPreview();
    } else if (e.target.closest("#wk-restyle"))
      patchPayload({ restyle: $("#wk-restyle").classList.contains("on") });
  });
  const num = (el, lo) => { const n = parseInt(el.value, 10); return isFinite(n) && n >= lo ? n : null; };
  document.addEventListener("change", (e) => {
    const t = e.target;
    if (t.id === "wk-maxw" && num(t, 1) !== null) patchPayload({ max_welcomes: num(t, 1) });
    else if (t.id === "wk-limit" && num(t, 1) !== null) patchPayload({ limit: num(t, 1) });
    else if (t.id === "wk-guard" && num(t, 0) !== null) patchPayload({ guard_hours: num(t, 0) });
    else if (t.id === "wk-newout" && num(t, 0) !== null) patchPayload({ new_max_outbound: num(t, 0) });
    else if (t.id === "wk-newin" && num(t, 0) !== null) patchPayload({ new_max_inbound: num(t, 0) });
    else if (t.id === "wk-every") {
      const m = num(t, 1);
      if (m === null) { renderRule(); return; }
      saveRule({ every_seconds: m * 60 });
    } else if (t.id === "wk-model") {
      const p = { ...((rule && rule.payload) || {}) };
      if (t.value) p.model = t.value; else delete p.model;
      saveRule({ payload: p });
    }
  });

  // ── last sweep ───────────────────────────────────────────────
  const LR_TILES = [
    ["welcomes_sent", "welcomes sent"],
    ["new_subscribers", "new subs found"],
    ["subscribers_seen", "feed items scanned"],
    ["image_attached", "photo attached"],
    ["skipped_existing", "already chatting"],
    ["skipped_cooldown", "welcomed before"],
    ["skipped_guard", "contact guard held"],
    ["skipped_restricted", "restricted on OF"],
    ["restyled", "AI-restyled lines"],
    ["pinned_used", "pinned line used"],
    ["skipped_locked", "locked / blacklisted"],
    ["errors", "errors"],
  ];
  (function renderLastRun() {
    const meta = $("#wk-lr-meta"), tiles = $("#wk-lr-stats"), list = $("#wk-lr-runs");
    if (runsRes.status !== "fulfilled") {
      meta.textContent = "run log unavailable";
      tiles.innerHTML = '<div class="wk-empty" style="grid-column:1/-1">The run log didn’t load — /admin/stats/automation-runs returned an error, so there is nothing honest to show here.</div>';
      Fastt.staticBadge($("#wk-lastrun > .fx-card-h"), "NO DATA");
      return;
    }
    if (!runs.length) {
      meta.textContent = "never run";
      tiles.innerHTML = '<div class="wk-empty" style="grid-column:1/-1">This account has never run a welcome sweep, so there is no breakdown yet. Turn the switch on (or wait for the next feed check) and the counts land here.</div>';
      return;
    }
    const last = runs[0], s = runStats(last) || {};
    const flags = [];
    if (s.cap_hit) flags.push("daily AI cap hit");
    if (s.batch_capped) flags.push("batch cap hit");
    if (s.dry_run) flags.push("dry run");
    meta.textContent = (last.status || "?") + " · " + Fastt.fmtAgo(last.started_at)
      + (s.duration_ms ? " · " + (s.duration_ms / 1000).toFixed(1) + "s" : "")
      + (flags.length ? " · " + flags.join(" · ") : "");
    tiles.innerHTML = LR_TILES.map(([k, lbl]) => {
      const v = Number(s[k] || 0);
      return '<div class="wk-stat"><b class="' + (v ? "" : "zero") + '">' + Fastt.fmtInt(v)
        + "</b><span>" + esc(lbl) + "</span></div>";
    }).join("");
    if (last.error_text) {
      const e = document.createElement("div");
      e.className = "fx-note warn"; e.style.marginTop = "12px";
      e.textContent = "Last error: " + last.error_text;
      tiles.after(e);
    }
    list.innerHTML = runs.map((r) => {
      const st = runStats(r) || {};
      const cls = r.status === "ok" ? "ok" : (r.status === "error" ? "err" : "");
      return '<div class="wk-run"><i class="' + cls + '"></i><span>' + esc(r.status || "?")
        + '</span><span class="ago">' + esc(Fastt.fmtAgo(r.started_at)) + "</span>"
        + '<span class="num">' + Fastt.fmtInt(st.welcomes_sent || 0) + " sent · "
        + Fastt.fmtInt(st.new_subscribers || 0) + " new · "
        + (st.duration_ms ? (st.duration_ms / 1000).toFixed(1) + "s" : "—") + "</span></div>";
    }).join("");
  })();
  Fastt.liveBadge($("#wk-lastrun > .fx-card-h"));

  // ── live preview (compose-only; restyle only on Regenerate) ──
  const nick = (Fastt.accountRow() && Fastt.accountRow().nickname) || "She";
  $("#pv-av").textContent = nick[0].toUpperCase();
  let curSlot = "morning_1", lastPv = null, target = {};

  function setPhoto(id, slot) {
    const box = $("#pv-photo"), lab = $("#pv-photo-label"), withImg =
      !rule || !rule.payload || rule.payload.with_image !== false;
    box.querySelectorAll("img").forEach((n) => n.remove());
    if (!withImg) {
      box.style.display = "none"; lab.textContent = ""; return;
    }
    box.style.display = "";
    if (!id) {
      box.classList.add("ph");
      $("#pv-photo-ph").style.display = "";
      $("#pv-photo-ph").textContent = "no photo resolved for this slot — text only";
      lab.textContent = "";
      return;
    }
    box.classList.remove("ph");
    $("#pv-photo-ph").style.display = "none";
    const img = document.createElement("img");
    img.alt = ""; img.src = thumbUrl(id);
    img.onerror = () => {
      box.classList.add("ph");
      $("#pv-photo-ph").style.display = "";
      $("#pv-photo-ph").textContent = "photo #" + id + " — thumbnail unavailable";
      img.remove();
    };
    box.appendChild(img);
    const pinned = cfg.time_images[slot] ? "pinned to this slot" : "picked by the folder rule";
    lab.textContent = (SLOT_LABEL[slot] || slot) + " · photo #" + id + " · " + pinned;
  }

  async function preview(slot, opts) {
    opts = opts || {};
    curSlot = slot;
    $("#pv-name").textContent = nick + " · to a new sub · composing…";
    const warn = $("#pv-restyle-warn");
    warn.style.display = "none";
    try {
      const body = {
        account_id: ACCT, kind: KIND, slot,
        restyle: !!opts.restyle, ignore_pin: !!opts.ignore_pin,
      };
      if (target.fan_id) body.fan_id = target.fan_id;
      if (target.test_name) body.test_name = target.test_name;
      const res = await Fastt.post("/admin/automation-preview", body);
      lastPv = res;
      const b = res.bubbles || [res.text];
      const who = target.fan_id ? "fan #" + target.fan_id
        : target.test_name ? "“" + target.test_name + "”" : "a new sub";
      $("#pv-name").textContent = nick + " · to " + who + " · just now";
      $("#pv-b1-text").textContent = b[0] || "";
      $("#pv-b2").textContent = b[1] || ""; $("#pv-b2").style.display = b[1] ? "" : "none";
      $("#pv-b3").textContent = b[2] || ""; $("#pv-b3").style.display = b[2] ? "" : "none";
      setPhoto(res.image, res.slot);
      $("#pv-meta").textContent = (SLOT_LABEL[res.slot] || res.slot) + " slot · "
        + (res.pinned ? "pinned line" : res.restyled ? "AI-restyled" : "verbatim template")
        + (res.cap_hit ? " · daily AI cap hit" : "");
      $("#pv-pin").textContent = res.pinned ? "📌 Unpin this slot" : "📌 Pin this line";
      const chip = $("#pv-pinned");
      if (res.pinned) { chip.style.display = "";
        chip.textContent = "📌 " + (SLOT_LABEL[res.slot] || res.slot) + " pinned: “" + (b[1] || "") + "”"; }
      else chip.style.display = "none";
      // Honesty: preview_compose swallows a failed restyle and returns 200 with
      // restyled:false — without this the button just repaints the same line.
      if (opts.restyle) {
        if (res.cap_hit) {
          warn.style.display = "";
          warn.innerHTML = "<span><b>Daily AI cap reached.</b> The restyle was skipped and you are looking at the plain "
            + "template. Raise the cap in <a href=\"ai-brain.html\" style=\"color:inherit;text-decoration:underline\">Brain &amp; Persona</a> "
            + "(daily cost cap) or try again tomorrow — the live welcome falls back to the same verbatim line.</span>";
        } else if (res.pinned) {
          warn.style.display = "";
          warn.innerHTML = "<span>This slot is <b>pinned</b>, so the AI never rerolls it. Unpin it first to sample a fresh line.</span>";
        } else if (!res.restyled) {
          warn.style.display = "";
          warn.innerHTML = "<span><b>AI restyle unavailable — showing the plain template.</b> The composer asked the model "
            + "for a restyled activity line and got nothing back, so it fell back to the verbatim template (exactly what a "
            + "live welcome would send). Model in play: <b>" + esc(effectiveModel() || "account default") + "</b>.</span>";
        }
      }
    } catch (e) {
      $("#pv-name").textContent = nick + " · preview failed";
      const raw = e && e.body && (e.body.detail || e.body.error
        || (typeof e.body === "string" ? e.body : ""));
      const detail = (raw || (e && e.status ? "" : (e && e.message)) || "").toString().trim();
      warn.style.display = "";
      warn.innerHTML = "<span><b>Couldn’t compose a preview.</b> <code>POST /admin/automation-preview</code> failed"
        + (e && e.status ? " (HTTP " + e.status + ")" : "")
        + (detail ? " — " + esc(detail.slice(0, 160)) : "")
        + (target.fan_id ? ". Check that fan id belongs to this creator." : ".") + "</span>";
    }
  }
  const applyPreview = () => preview(curSlot);
  Fastt.liveBadge($("#pv-card > .fx-card-h"));
  preview(curSlot);

  document.addEventListener("click", (e) => {
    const chip = e.target.closest("#pv-chips .fx-chip");
    if (chip) { preview(chip.dataset.slot); return; }
    if (e.target.closest("#pv-regen")) { preview(curSlot, { restyle: true, ignore_pin: true }); return; }
    if (e.target.closest("#pv-apply")) {
      const nm = $("#pv-name-in").value.trim();
      const fid = parseInt($("#pv-fan-in").value.trim(), 10);
      target = {};
      if (isFinite(fid) && fid > 0) target.fan_id = fid;
      else if (nm) target.test_name = nm;
      $("#pv-clear-target").style.display = (target.fan_id || target.test_name) ? "" : "none";
      preview(curSlot);
      return;
    }
    if (e.target.closest("#pv-clear-target")) {
      target = {}; $("#pv-name-in").value = ""; $("#pv-fan-in").value = "";
      $("#pv-clear-target").style.display = "none";
      preview(curSlot);
      return;
    }
    if (e.target.closest("#pv-pin")) {
      if (!lastPv) return;
      const pinned = lastPv.pinned;
      const line = (lastPv.bubbles || [])[1] || "";
      if (!pinned && !line) { Fastt.toast("Nothing to pin — regenerate first", "err"); return; }
      Fastt.post("/admin/welcome-pin", {
        account_id: ACCT, slot: lastPv.slot, line: pinned ? null : line,
      }).then(() => { Fastt.saved(pinned ? "Unpinned" : "Pinned ✓"); preview(lastPv.slot); })
        .catch(Fastt.oops);
    }
  });

  // ── vault picker (per-slot time_images) ──────────────────────
  let pickerFolders = null;   // lazily normalised folder list
  function foldersForPicker() {
    if (pickerFolders) return pickerFolders;
    const rows = (vaultLists || []).filter((f) => f && f.id != null && (f.photosCount | 0) > 0);
    const rank = (f) => {
      const i = AUTO_FOLDERS.indexOf(String(f.name || "").trim().toLowerCase());
      return i < 0 ? 99 : i;
    };
    rows.sort((a, b) => rank(a) - rank(b) || (b.photosCount | 0) - (a.photosCount | 0));
    pickerFolders = rows;
    return rows;
  }

  function openPicker(slot) {
    const folders = foldersForPicker();
    const back = document.createElement("div");
    back.className = "vp-back";
    back.innerHTML = '<div class="vp">'
      + '<div class="vp-h">Photo for ' + esc(SLOT_LABEL[slot] || slot)
      + '<span class="sub">saved to this creator’s time_images — the welcome attaches it on the greeting bubble</span>'
      + '<span class="x" data-close="1">✕</span></div>'
      + '<div class="vp-body"><div class="vp-folders" id="vp-folders"></div>'
      + '<div class="vp-grid" id="vp-grid"><div class="vp-msg">Loading…</div></div></div>'
      + '<div class="vp-foot"><span id="vp-foot-txt">Photos only — the welcome attaches a single image.</span>'
      + '<span style="color:var(--muted2)">' + (cfg.time_images[slot]
          ? "· pinned now: #" + esc(cfg.time_images[slot]) : "· on auto") + "</span>"
      + '<button class="fx-btn ghost" style="height:30px;font-size:12.5px;margin-left:auto" data-clear="1">Put this slot back on auto</button></div>'
      + "</div>";
    document.body.appendChild(back);
    const gridEl = back.querySelector("#vp-grid");
    const foldEl = back.querySelector("#vp-folders");
    const close = () => back.remove();

    if (!vaultLists) {
      foldEl.innerHTML = '<div class="vp-msg" style="padding:14px;font-size:12.5px">Vault folders unavailable</div>';
      gridEl.innerHTML = '<div class="vp-msg">The vault list didn’t load from OnlyFans, so there is nothing to pick from. '
        + 'Check the creator’s OF session, then reload this page.</div>';
    } else {
      foldEl.innerHTML = '<div class="vp-f" data-list="">'
        + '<span class="n">All photos</span><span class="c">vault</span></div>'
        + folders.map((f) => {
            const auto = AUTO_FOLDERS.indexOf(String(f.name || "").trim().toLowerCase()) >= 0;
            return '<div class="vp-f" data-list="' + esc(f.id) + '">'
              + (auto ? '<span class="star" title="one of the auto-pick folders">★</span>' : "")
              + '<span class="n">' + esc(f.name || ("#" + f.id)) + '</span>'
              + '<span class="c">' + (f.photosCount | 0) + "</span></div>";
          }).join("");
      const first = folders[0];
      selectFolder(first ? String(first.id) : "");
    }

    async function selectFolder(listId) {
      foldEl.querySelectorAll(".vp-f").forEach((n) =>
        n.classList.toggle("on", (n.dataset.list || "") === (listId || "")));
      gridEl.innerHTML = '<div class="vp-msg">Loading…</div>';
      try {
        const params = { limit: 48, type: "photo" };
        if (listId) params.list_id = listId;
        const out = await Fastt.get("/api/of/v2/vault/media", params);
        const items = (out.list || out.rows || []).filter((m) => m && m.id);
        if (!items.length) {
          gridEl.innerHTML = '<div class="vp-msg">No photos in this folder.</div>';
          return;
        }
        const cur = cfg.time_images[slot];
        gridEl.innerHTML = items.map((m) =>
          '<div class="vp-cell' + (String(m.id) === String(cur) ? " sel" : "") + '" data-id="' + esc(m.id) + '">'
          + '<img loading="lazy" alt="" src="' + esc(thumbUrl(m.id)) + '">'
          + '<span class="id">#' + esc(m.id) + "</span></div>").join("");
        back.querySelector("#vp-foot-txt").textContent =
          items.length + " photo" + (items.length === 1 ? "" : "s") + " · click one to pin it to this slot";
      } catch (err) {
        gridEl.innerHTML = '<div class="vp-msg">Couldn’t read this folder from OnlyFans ('
          + esc(((err.body && (err.body.detail || err.body.error)) || err.message || "").toString().slice(0, 120))
          + ").</div>";
      }
    }

    back.addEventListener("click", async (ev) => {
      if (ev.target === back || ev.target.closest("[data-close]")) { close(); return; }
      const f = ev.target.closest(".vp-f");
      if (f) { selectFolder(f.dataset.list || ""); return; }
      const cell = ev.target.closest(".vp-cell");
      if (cell) {
        cfg.time_images[slot] = parseInt(cell.dataset.id, 10);
        close(); await saveCfgNow(); applyPreview();
        return;
      }
      if (ev.target.closest("[data-clear]")) {
        delete cfg.time_images[slot];
        close(); await saveCfgNow(); applyPreview();
      }
    });
  }

  document.addEventListener("click", async (e) => {
    const x = e.target.closest("#wk-slots [data-clear]");
    if (x) {
      e.stopPropagation();
      delete cfg.time_images[x.dataset.clear];
      await saveCfgNow(); applyPreview();
      return;
    }
    const box = e.target.closest("#wk-slots [data-pick]");
    if (box) openPicker(box.dataset.pick);
  });
});
