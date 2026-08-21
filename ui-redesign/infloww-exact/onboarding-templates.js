Fastt.ready(async () => {
  const $ = Fastt.$, $$ = Fastt.$$, esc = Fastt.esc;
  const ACCT = Fastt.account();
  let saved = [];        // /admin/saved-replies rows (fastt-side, "Manual")
  let ofWelcome = [];    // OF reply_on_subscribe templates (read-only mirror)
  let replyOnSub = null; // /api/of/v2/users/me/settings.replyOnSubscribe (tri-state)
  let filter = "all";

  const stripHtml = (s) => { const d = document.createElement("div"); d.innerHTML = s || ""; return d.textContent || ""; };
  const varify = (s) => esc(s).replace(/\{name\}/g, '<span class="var">{name}</span>');
  const COPY_SVG = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15h-.5A1.5 1.5 0 0 1 3 13.5v-9A1.5 1.5 0 0 1 4.5 3h9A1.5 1.5 0 0 1 15 4.5V5" stroke-linecap="round"/></svg>';

  async function load() {
    const [sr, of, st] = await Promise.allSettled([
      Fastt.get("/admin/saved-replies"),
      Fastt.get("/api/of/v2/messages/templates", { template: "reply_on_subscribe" }),
      // The template's own isActive is DECOUPLED from the master switch — whether
      // OF actually auto-replies to a new sub is settings.replyOnSubscribe.
      Fastt.get("/api/of/v2/users/me/settings"),
    ]);
    saved = sr.status === "fulfilled" ? (sr.value.list || []) : [];
    ofWelcome = of.status === "fulfilled" && Array.isArray(of.value) ? of.value : [];
    replyOnSub = st.status === "fulfilled" && st.value && typeof st.value.replyOnSubscribe === "boolean"
      ? st.value.replyOnSubscribe : null;
    if (sr.status !== "fulfilled") Fastt.oops(sr.reason);
  }

  const setSt = (id, text, cls) => { const el = $(id); if (!el) return;
    el.className = "fx-st" + (cls ? " " + cls : ""); el.innerHTML = "<i></i>" + esc(text); };

  function ofAutoReplyLabel() {
    if (replyOnSub === null) return ["OF auto-reply state unavailable", ""];
    return replyOnSub
      ? ["OF auto-replies to new subs · on", "ok"]
      : ["OF auto-reply to new subs · off", "warn"];
  }

  function render() {
    setSt("#tp-count", saved.length + " saved repl" + (saved.length === 1 ? "y" : "ies") + " in fastt",
      saved.length ? "ok" : "");
    const w = ofWelcome[0];
    setSt("#tp-oftpl", w ? "OF welcome template stored" : "No OF welcome template", w ? "ok" : "");
    const [lbl, cls] = ofAutoReplyLabel();
    setSt("#tp-welcome", lbl, cls);
    const newest = saved.map((r) => r.updated_at).filter(Boolean).sort().pop();
    setSt("#tp-edited", newest ? "Last edited " + Fastt.fmtAgo(newest) : "Nothing edited yet");

    const counts = { all: saved.length + ofWelcome.length, welcome: ofWelcome.length, followups: 0, manual: saved.length };
    $$("#tp-filter .fx-chip").forEach((c) => {
      const cat = c.dataset.cat;
      c.textContent = { all: "All", welcome: "Welcome", followups: "Follow-ups", manual: "Manual" }[cat] + " · " + counts[cat];
    });

    const grid = $("#tp-grid");
    const newCard = $("#tp-new");
    grid.querySelectorAll(".tpl-card:not(.new)").forEach((el) => el.remove());
    grid.querySelectorAll(".tp-empty").forEach((el) => el.remove());
    const frag = document.createDocumentFragment();

    if (filter === "all" || filter === "welcome") for (const t of ofWelcome) {
      const el = document.createElement("div");
      el.className = "tpl-card"; el.dataset.of = "1";
      const facts = ["stored on OF"];
      // When we know the auto-reply state, the toggle button below carries it —
      // don't also spell it out here. Only narrate it when it's unknowable.
      if (replyOnSub === null) facts.push("auto-reply unknown");
      if (t.mediaCount) facts.push(t.mediaCount + " media");
      el.innerHTML = '<div class="tpl-top"><div class="tpl-name">OF welcome message</div>'
        + '<a class="tpl-tag wel" href="onboarding-welcome.html" title="The reply_on_subscribe slot stored on OnlyFans itself">Welcome</a></div>'
        + '<div class="tpl-text">' + varify(stripHtml(t.text)) + "</div>"
        + '<div class="tpl-foot"><span class="tpl-meta" title="The template’s own isActive flag is '
        + (t.isActive ? "true" : "false")
        + ' — it does NOT control whether OF fires the welcome; settings.replyOnSubscribe does.">'
        + esc(facts.join(" · ")) + "</span>"
        + (replyOnSub === null ? ""
            : '<button class="tpl-ar' + (replyOnSub ? " on" : "") + '" data-ar="' + (replyOnSub ? "1" : "0")
              + '" title="' + (replyOnSub
                  ? "OnlyFans is auto-replying to new subscribers — click to turn off"
                  : "OnlyFans is NOT auto-replying to new subscribers — click to turn on") + '"><i></i>'
              + (replyOnSub ? "Auto-reply on" : "Auto-reply off") + "</button>")
        + '<button class="tpl-copy">' + COPY_SVG + "Copy</button></div>";
      frag.appendChild(el);
    }
    if (filter === "all" || filter === "manual") for (const r of saved) {
      const el = document.createElement("div");
      el.className = "tpl-card"; el.dataset.id = r.id;
      const meta = [];
      if (r.price > 0) meta.push(Fastt.fmtMoney(r.price) + " PPV");
      if ((r.media || []).length) meta.push(r.media.length + " media");
      if (r.script_id) meta.push(r.script_id + " · step " + (r.script_step ?? "?"));
      if (!meta.length) meta.push("one-tap in chat");
      el.innerHTML = '<div class="tpl-top"><div class="tpl-name">' + esc(r.title || "Untitled") + '</div>'
        + '<a class="tpl-tag man" title="Saved reply stored in fastt">Manual</a></div>'
        + '<div class="tpl-text">' + varify(r.text) + "</div>"
        + '<div class="tpl-foot"><span class="tpl-meta">' + esc(meta.join(" · ")) + "</span>"
        + '<button class="tpl-copy">' + COPY_SVG + "Copy</button></div>";
      frag.appendChild(el);
    }
    if (!frag.childNodes.length) {
      const empty = document.createElement("div");
      empty.className = "tp-empty";
      empty.style.cssText = "grid-column:1/-1;padding:26px;text-align:center;color:var(--muted);font-size:13.5px";
      empty.textContent = filter === "followups"
        ? "Follow-up nudges are written fresh by the AI for each fan — there are no fixed follow-up templates."
        : "No templates yet for this account — click “New template” to add the first one.";
      frag.appendChild(empty);
    }
    grid.insertBefore(frag, newCard);
    renderSequences();
    $("#tp-copyhint").textContent = saved.length
      ? "Copies all " + saved.length + " saved repl" + (saved.length === 1 ? "y" : "ies") + " into another creator’s set."
      : "Nothing to copy — this creator has no saved replies yet.";
  }

  // ── Sequences: real script_id / script_step groups ───────────
  function renderSequences() {
    const body = $("#tp-seq-body"), n = $("#tp-seq-n");
    const withScript = saved.filter((r) => r.script_id);
    n.textContent = withScript.length + " of " + saved.length + " templates carry a script name";
    if (!withScript.length) {
      body.innerHTML = '<div class="tp-empty-box">No sequences yet — <b>0 of '
        + saved.length + '</b> saved template' + (saved.length === 1 ? " carries" : "s carry")
        + ' a script name.<br>Open a template above, give it a <b>script name</b> and a <b>step number</b>, '
        + 'and its flow shows up here.</div>';
      return;
    }
    const groups = {};
    for (const r of withScript) (groups[r.script_id] = groups[r.script_id] || []).push(r);
    body.innerHTML = '<div class="tp-scripts">' + Object.keys(groups).sort().map((sid) => {
      const rows = groups[sid].slice().sort((a, b) => (a.script_step ?? 999) - (b.script_step ?? 999));
      return '<div class="tp-script"><div class="sh">' + esc(sid)
        + '<span class="cnt">' + rows.length + " step" + (rows.length === 1 ? "" : "s") + "</span></div>"
        + '<div class="tp-steps">' + rows.map((r, i) => {
            const nxt = rows[i + 1];
            return '<div class="tp-step"><span class="k">' + esc(r.script_step ?? "?") + "</span>"
              + '<span class="t">' + esc(r.title || r.text.slice(0, 70)) + "</span>"
              + '<span class="nx">' + (nxt ? "next → " + esc(nxt.title || ("step " + (nxt.script_step ?? "?"))) : "last step")
              + "</span></div>";
          }).join("") + "</div></div>";
    }).join("") + "</div>";
  }

  // ── Per-account overrides: the REAL roster + REAL counts ─────
  async function renderRoster() {
    const list = $("#tp-acct-list"), hint = $("#tp-acct-hint");
    const accts = Fastt.accounts();
    if (!accts.length) {
      list.innerHTML = '<span class="tp-a" style="cursor:default">only this creator (' + esc(ACCT || "none") + ")</span>";
      hint.innerHTML = "The creator roster couldn’t be listed (an unauthed caller gets an empty <code>/admin/accounts</code>), "
        + "so only the selected creator is shown. Sign in from the creator switcher to see the rest.";
      return;
    }
    list.innerHTML = accts.map((a) =>
      '<span class="tp-a' + (String(a.id) === String(ACCT) ? " on" : "") + '" data-acct="' + esc(a.id) + '">'
      + esc(a.nickname || a.id) + ' <span class="n pending" data-n="' + esc(a.id) + '">…</span></span>').join("");
    hint.innerHTML = "Every creator keeps her own set of saved replies. Counts below are live from "
      + "<code>/admin/saved-replies</code> per creator — click one to switch scope.";
    const results = await Promise.allSettled(accts.map((a) =>
      Fastt.get("/admin/saved-replies", { account_id: a.id }, { noAccount: true })));
    let total = 0, unknown = 0;
    results.forEach((r, i) => {
      const el = $('[data-n="' + CSS.escape(String(accts[i].id)) + '"]');
      if (!el) return;
      el.classList.remove("pending");
      if (r.status === "fulfilled") {
        const c = (r.value.list || []).length; total += c; el.textContent = "· " + c;
      } else { unknown += 1; el.textContent = "· ?"; el.title = "count unavailable"; }
    });
    hint.innerHTML = "Every creator keeps her own set. <b>" + Fastt.fmtInt(total) + "</b> saved repl"
      + (total === 1 ? "y" : "ies") + " across <b>" + accts.length + "</b> creator"
      + (accts.length === 1 ? "" : "s")
      + (unknown ? " (" + unknown + " couldn’t be read)" : "")
      + " — live from <code>/admin/saved-replies</code>. Click a creator to switch scope.";
  }

  // ── copy this set to another creator (real writes, confirmed) ─
  function copySetModal() {
    if (!saved.length) { Fastt.toast("This creator has no saved replies to copy", "err"); return; }
    const others = Fastt.accounts().filter((a) => String(a.id) !== String(ACCT));
    const back = document.createElement("div");
    back.className = "ft-modal-back";
    back.innerHTML = '<div class="ft-modal" style="width:380px"><h3>Copy ' + saved.length
      + " template" + (saved.length === 1 ? "" : "s") + " to…</h3>"
      + (others.length
          ? '<select id="tp-copy-to" style="width:100%;box-sizing:border-box;background:#1c1c1c;border:1px solid #333;'
            + 'color:#fff;border-radius:8px;padding:9px 11px;margin-bottom:10px;font:13px Inter,sans-serif">'
            + others.map((a) => '<option value="' + esc(a.id) + '">' + esc(a.nickname || a.id) + "</option>").join("")
            + "</select>"
            + '<div style="font-size:12px;color:#8a8a8a;margin-bottom:10px;line-height:1.5">'
            + "Creates copies in the target creator’s set. Nothing is overwritten and nothing is sent to any fan.</div>"
            + '<div class="ft-err" id="tp-copy-err"></div><button id="tp-copy-go">Copy</button>'
          : '<div style="font-size:12.5px;color:#8a8a8a;line-height:1.5">No other creator is listed for this login, '
            + "so there is nowhere to copy to. Sign in from the creator switcher first.</div>")
      + "</div>";
    document.body.appendChild(back);
    back.addEventListener("click", (e) => { if (e.target === back) back.remove(); });
    const go = $("#tp-copy-go", back);
    if (go) go.addEventListener("click", async () => {
      const to = $("#tp-copy-to", back).value;
      const name = (others.find((a) => String(a.id) === String(to)) || {}).nickname || to;
      if (!confirm("Copy " + saved.length + " template(s) into " + name + "? This writes new saved replies on that creator."))
        return;
      go.disabled = true; go.textContent = "Copying…";
      try {
        for (const r of saved) await Fastt.post("/admin/saved-replies", {
          title: r.title || null, text: r.text, price: r.price || 0,
          locked_text: !!r.locked_text, media: r.media || [],
          tagged_users: r.tagged_users || [], previews: r.previews || [],
          gif_id: r.gif_id || null, gif_url: r.gif_url || null,
          script_id: r.script_id || null, script_step: r.script_step ?? null,
        }, { params: { account_id: to }, noAccount: true });
        back.remove(); Fastt.saved("Copied " + saved.length + " → " + name);
        renderRoster();
      } catch (e) {
        go.disabled = false; go.textContent = "Copy";
        const err = $("#tp-copy-err", back);
        err.style.display = "block";
        err.textContent = (e.body && (e.body.detail || e.body.error)) || e.message;
      }
    });
  }

  // ── editor modal (saved replies CRUD — all click-driven) ─────
  function editModal(row) {
    const back = document.createElement("div");
    back.className = "ft-modal-back";
    back.innerHTML = '<div class="ft-modal" style="width:420px">'
      + "<h3>" + (row ? "Edit template" : "New template") + "</h3>"
      + '<input type="text" id="tp-m-title" placeholder="Title" value="' + esc(row ? row.title || "" : "") + '">'
      + '<textarea id="tp-m-text" placeholder="Text — use {name} for the fan’s name" style="width:100%;box-sizing:border-box;background:#1c1c1c;border:1px solid #333;color:#fff;border-radius:8px;padding:9px 11px;margin-bottom:10px;font:13px Inter,sans-serif;min-height:96px;resize:vertical">'
      + esc(row ? row.text : "") + "</textarea>"
      + '<input type="text" id="tp-m-price" placeholder="PPV price in $ (0 = free)" value="' + (row && row.price ? row.price : "") + '">'
      + '<label id="tp-m-lockwrap" style="display:none;align-items:center;gap:8px;font-size:12.5px;color:#b9b9b9;margin:-3px 0 10px;cursor:pointer;user-select:none">'
      + '<input type="checkbox" id="tp-m-lock" style="accent-color:#4166f6;width:auto;margin:0">Lock the message text behind the price (fan pays to read it)</label>'
      + '<input type="text" id="tp-m-script" placeholder="Script name (optional) — groups templates into a flow" value="' + esc(row ? row.script_id || "" : "") + '">'
      + '<input type="text" id="tp-m-step" placeholder="Script step (1, 2, …)" value="' + (row && row.script_step ? row.script_step : "") + '">'
      + '<div class="ft-err" id="tp-m-err"></div>'
      + '<button id="tp-m-save">Save</button>'
      + (row ? '<button id="tp-m-del" style="margin-top:8px;background:transparent;border:1px solid #4a2a2a;color:#e05b5b">Delete</button>' : "")
      + "</div>";
    document.body.appendChild(back);
    back.addEventListener("click", (e) => { if (e.target === back) back.remove(); });
    // Lock-text control only makes sense at a price > 0 — reveal it live as
    // the price is typed, seed it from the row's saved state.
    const priceEl = $("#tp-m-price", back), lockWrap = $("#tp-m-lockwrap", back), lockEl = $("#tp-m-lock", back);
    if (row && row.locked_text) lockEl.checked = true;
    const syncLock = () => { lockWrap.style.display = (parseFloat(priceEl.value) || 0) > 0 ? "flex" : "none"; };
    syncLock(); priceEl.addEventListener("input", syncLock);
    $("#tp-m-save", back).addEventListener("click", async () => {
      const text = $("#tp-m-text", back).value.trim();
      const err = $("#tp-m-err", back);
      if (!text) { err.style.display = "block"; err.textContent = "Text is required"; return; }
      const cents = Math.round((parseFloat($("#tp-m-price", back).value) || 0) * 100);
      const stepRaw = parseInt($("#tp-m-step", back).value, 10);
      const body = {
        title: $("#tp-m-title", back).value.trim() || null,
        text,
        price: cents / 100,
        locked_text: cents > 0 ? $("#tp-m-lock", back).checked : false,
        media: row ? row.media || [] : [],
        tagged_users: row ? row.tagged_users || [] : [],
        previews: row ? row.previews || [] : [],
        gif_id: row ? row.gif_id : null,
        gif_url: row ? row.gif_url : null,
        script_id: $("#tp-m-script", back).value.trim() || null,
        script_step: isFinite(stepRaw) ? stepRaw : null,
      };
      try {
        if (row) await Fastt.put("/admin/saved-replies/" + row.id, body);
        else await Fastt.post("/admin/saved-replies", body);
        back.remove(); Fastt.saved(); await load(); render(); renderRoster();
      } catch (e) { err.style.display = "block"; err.textContent = (e.body && e.body.detail) || e.message; }
    });
    const del = $("#tp-m-del", back);
    if (del) del.addEventListener("click", async () => {
      if (!confirm("Delete this template? This cannot be undone.")) return;
      try { await Fastt.del("/admin/saved-replies/" + row.id);
        back.remove(); Fastt.saved("Deleted"); await load(); render(); renderRoster();
      } catch (e) { Fastt.oops(e); }
    });
  }

  await load(); render();
  Fastt.liveBadge($(".sec-title"));
  Fastt.liveBadge($("#tp-peracct").parentElement.querySelector("h4"));
  renderRoster();

  document.addEventListener("click", async (e) => {
    const chip = e.target.closest("#tp-filter .fx-chip");
    if (chip) { filter = chip.dataset.cat; render(); return; }
    const acct = e.target.closest("#tp-acct-list [data-acct]");
    if (acct) {
      if (String(acct.dataset.acct) !== String(ACCT)) Fastt.setAccount(acct.dataset.acct);
      return;
    }
    if (e.target.closest("#tp-copyset")) { copySetModal(); return; }
    const copy = e.target.closest(".tpl-copy");
    if (copy) {
      const card = copy.closest(".tpl-card");
      const txt = card.querySelector(".tpl-text").textContent;
      try { await navigator.clipboard.writeText(txt); } catch {}
      copy.classList.add("done"); copy.lastChild.textContent = "Copied";
      setTimeout(() => { copy.classList.remove("done"); copy.lastChild.textContent = "Copy"; }, 1200);
      return;
    }
    const ar = e.target.closest(".tpl-ar");
    if (ar) {
      // Toggle OF's master reply-on-subscribe flag. This is a real OF config
      // change (not a fan message), so it stays behind an explicit click +
      // confirm — never fired automatically.
      const next = ar.dataset.ar !== "1";
      if (!confirm(next
        ? "Turn ON the OnlyFans welcome auto-reply?\n\nEvery new subscriber will automatically receive your welcome message."
        : "Turn OFF the OnlyFans welcome auto-reply?\n\nNew subscribers will stop receiving the welcome message automatically.")) return;
      ar.disabled = true;
      try {
        await Fastt.patch("/api/of/v2/users/me/reply-on-subscribe", { enabled: next });
        replyOnSub = next;
        Fastt.saved(next ? "Auto-reply turned on ✓" : "Auto-reply turned off");
        render();
      } catch (err) { ar.disabled = false; Fastt.oops(err); }
      return;
    }
    if (e.target.closest("#tp-new")) { editModal(null); return; }
    const card = e.target.closest("#tp-grid .tpl-card");
    if (card && card.dataset.id) {
      const row = saved.find((r) => String(r.id) === card.dataset.id);
      if (row) editModal(row);
      return;
    }
    if (card && card.dataset.of) {
      Fastt.toast("The OF welcome message lives on OnlyFans — edit it on the Welcome page / OF settings.");
      return;
    }
    if (e.target.closest("#tp-export")) {
      const blob = new Blob([JSON.stringify(saved, null, 2)], { type: "application/json" });
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = "fastt-templates-" + ACCT + ".json";
      a.click(); URL.revokeObjectURL(a.href);
      return;
    }
    if (e.target.closest("#tp-import")) {
      const inp = document.createElement("input");
      inp.type = "file"; inp.accept = ".json,application/json";
      inp.addEventListener("change", async () => {
        const f = inp.files && inp.files[0]; if (!f) return;
        let rows;
        try { rows = JSON.parse(await f.text()); } catch { Fastt.toast("Not valid JSON", "err"); return; }
        if (!Array.isArray(rows)) { Fastt.toast("Expected a JSON array of templates", "err"); return; }
        const ok = rows.filter((r) => r && typeof r.text === "string" && r.text.trim());
        if (!ok.length) { Fastt.toast("No rows with a text field found", "err"); return; }
        if (!confirm("Import " + ok.length + " template(s) into this account?")) return;
        try {
          for (const r of ok) await Fastt.post("/admin/saved-replies", {
            title: r.title || null, text: r.text,
            price: Math.round((Number(r.price) || 0) * 100) / 100,
            locked_text: !!r.locked_text, media: Array.isArray(r.media) ? r.media : [],
            tagged_users: Array.isArray(r.tagged_users) ? r.tagged_users : [],
            previews: Array.isArray(r.previews) ? r.previews : [],
            gif_id: r.gif_id || null, gif_url: r.gif_url || null,
            script_id: r.script_id || null,
            script_step: Number.isFinite(r.script_step) ? r.script_step : null,
          });
          Fastt.saved("Imported " + ok.length); await load(); render(); renderRoster();
        } catch (err2) { Fastt.oops(err2); }
      });
      inp.click();
      return;
    }
  });
});
