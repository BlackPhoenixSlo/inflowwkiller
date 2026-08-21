/* Live wiring — every panel on this page is one of:
 *   GET  /admin/mass-messages?days_back&limit        (history cache of OF's group feed)
 *   POST /admin/mass-messages/refresh                (pull fresh rows from OF)
 *   GET  /api/of/v2/lists?limit=50                   (the include/exclude universe)
 *   GET  /api/of/v2/vault/media?limit=                (media tray + previews)
 *   GET  /admin/smart-lists (+ /{id}/resolve)         (spend tiers → user_ids)
 *   GET  /admin/funnels                              (funnel_id on the body)
 *   GET  /admin/automation-rules                     (mass_premade + unsend_messages)
 *   GET  /admin/stats/per-automation?from&to         (attributed revenue)
 *   POST /api/of/v2/messages/queue                   (explicit click + confirm ONLY)
 *   DELETE /api/of/v2/messages/queue/{queue_id}      (unsend, confirm-gated)
 */
Fastt.ready(async () => {
  "use strict";
  const $ = Fastt.$, $$ = Fastt.$$, esc = Fastt.esc;
  const acct = Fastt.account();
  const row = Fastt.accountRow();
  const nick = row ? (row.nickname || String(row.id)) : (acct || "—");
  const setTxt = (id, txt) => { const el = document.getElementById(id); if (el) el.textContent = txt; };
  const setChip = (id, html) => { const el = document.getElementById(id); if (el) el.innerHTML = html; };
  // Thumbnails: prefer the signed CDN url OF just handed us in the vault list
  // (always fresh); fall back to the relay's cached-thumb route, which 404s for
  // items the vault-AI cache has never seen — hence the onerror guard below.
  const vaultUrl = new Map();
  const thumb = (id) => vaultUrl.get(String(id))
    || ("/admin/vault-ai/thumb?account_id=" + encodeURIComponent(acct)
        + "&media_id=" + encodeURIComponent(id));
  const IMG_FALLBACK = ' onerror="this.style.display=\'none\';this.parentElement.classList.add(\'nothumb\')"';

  // identity
  setTxt("hdrModel", nick);
  setTxt("sendsFrom", nick);

  const state = {
    lists: [], listsErr: null,
    smart: [],
    funnels: [],
    vault: [], vaultErr: null,
    media: [],            // vault ids attached to the composer
    gif: null,            // {id,url} picked from giphy → giphy_id
    templates: [],        // saved OF message templates
    premade: null,        // mass_premade rule
    unsendRules: [],      // unsend_messages rules
    repTexts: [], repMedia: [],
    hist: [],
  };

  // ── loads (each panel degrades on its own; one 4xx must not blank the page)
  async function loadLists() {
    try {
      const L = await Fastt.get("/api/of/v2/lists", { limit: 50 });
      state.lists = (L && L.list) || [];
    } catch (e) { state.listsErr = e; console.warn("lists load failed", e); }
  }
  async function loadRest() {
    const jobs = [
      Fastt.get("/admin/smart-lists").then((r) => { state.smart = ((r && r.lists) || []).filter(isRealTier); })
        .catch((e) => console.warn("smart-lists", e)),
      Fastt.get("/admin/funnels").then((r) => { state.funnels = (r && r.funnels) || []; })
        .catch((e) => console.warn("funnels", e)),
      Fastt.rulesByKind().then((m) => {
        state.premade = (m.mass_premade || [])[0] || null;
        state.unsendRules = m.unsend_messages || [];
      }).catch((e) => console.warn("rules", e)),
    ];
    await Promise.all(jobs);
  }
  await Promise.all([loadLists(), loadRest()]);

  // ── badges: what's live vs what has no backend ──
  Fastt.liveBadge($("#cardNewBlast .fx-card-h"));
  Fastt.liveBadge($("#cardRecent .fx-card-h"));
  Fastt.liveBadge($("#cardRev .fx-card-h"));
  Fastt.liveBadge($("#cardUnsend .fx-card-h"));

  // ════════════════════════════════════════════════════ audience: OF lists
  const LIST_ORDER = ["fans", "following", "rebill_on", "rebill_off", "friends",
    "tagged", "muted", "recent", "close_friends"];
  const listKey = (l) => (l.type && l.type !== "custom") ? String(l.type) : String(l.id);
  function sortedLists() {
    return state.lists.slice().sort((a, b) => {
      let ia = LIST_ORDER.indexOf(a.type), ib = LIST_ORDER.indexOf(b.type);
      if (ia < 0) ia = 99; if (ib < 0) ib = 99;
      if (ia !== ib) return ia - ib;
      return (b.usersCount || 0) - (a.usersCount || 0);
    });
  }
  function chipHtml(l, side, on) {
    return '<span class="fx-chip' + (on ? " on" : "") + '" data-key="' + esc(listKey(l))
      + '" data-n="' + (l.usersCount || 0) + '" title="' + esc(l.type) + " · " + esc(listKey(l))
      + '">' + esc(l.name) + ' <b style="color:var(--muted2);font-weight:500">'
      + (l.usersCount || 0) + "</b></span>";
  }
  function renderLists() {
    const inc = $("#incLists"), exc = $("#excLists");
    if (!state.lists.length) {
      const why = state.listsErr
        ? "GET /api/of/v2/lists failed (" + esc(String((state.listsErr.body && state.listsErr.body.detail)
            || state.listsErr.message || "error").slice(0, 60)) + ")"
        : "OF returned no lists for this creator";
      inc.innerHTML = '<span style="font-size:12.5px;color:var(--muted2)">' + why
        + " — a blast can't be aimed without them, so sending is disabled.</span>";
      exc.innerHTML = '<span style="font-size:12.5px;color:var(--muted2)">—</span>';
      return;
    }
    const ls = sortedLists();
    inc.innerHTML = ls.map((l) => chipHtml(l, "inc", l.type === "fans")).join("");
    exc.innerHTML = ls.map((l) => chipHtml(l, "exc",
      l.name === "MASSdmEXCLUDE" || l.name === "Auto_Exclude")).join("");
  }
  const picked = (id) => $$("#" + id + " .fx-chip.on").map((c) => c.dataset.key);
  const pickedN = (id) => $$("#" + id + " .fx-chip.on")
    .reduce((s, c) => s + Number(c.dataset.n || 0), 0);

  // ════════════════════════════════════════════════════ spend tier = smart list
  // The queue body has no spend field. Smart Lists express spend as a rule and
  // resolve to explicit fan ids, which the body DOES take as user_ids.
  const spendResolved = { key: null, ids: null };
  // A smart list only earns a Spend-tier chip if it's a real, named tier. Keyboard-mash
  // test lists (an all-lowercase, vowel-less name like "fghj") are junk and stay hidden —
  // otherwise seller-side test artifacts show up as clickable audience filters.
  function isRealTier(l) {
    const name = ((l && l.name) || "").trim();
    if (!name) return false;
    if (/^[a-z]{4,}$/.test(name) && !/[aeiou]/.test(name)) return false; // consonant mash
    return true;
  }
  function renderSpend() {
    const box = $("#spendChips"), note = $("#spendNote");
    let html = '<span class="fx-chip on" data-sl="">Any spend</span>';
    html += state.smart.map((l) => {
      const r = (l.rules && l.rules.rules) || [];
      const hint = r.map((x) => x.field + " " + x.op + " " + x.value).join(", ");
      return '<span class="fx-chip" data-sl="' + esc(l.id) + '" title="' + esc(hint) + '">'
        + esc(l.name) + "</span>";
    }).join("");
    box.innerHTML = html;
    note.innerHTML = state.smart.length
      ? "Smart lists resolve to explicit fan ids and ride along as <code style=\"color:#9db1fb\">user_ids</code>. "
        + "Build more in <a href=\"growth-smart-lists.html\" style=\"color:#9db1fb;text-decoration:none\">Smart lists</a>."
      : "No smart lists on this creator yet — the queue body has no spend field of its own, so "
        + "<a href=\"growth-smart-lists.html\" style=\"color:#9db1fb;text-decoration:none\">build one</a> to narrow a blast by spend.";
  }
  async function onSpendPick(el) {
    const id = el.dataset.sl;
    if (!id) { spendResolved.key = null; spendResolved.ids = null; updReach(); return; }
    try {
      const out = await Fastt.get("/admin/smart-lists/" + id + "/resolve");
      spendResolved.key = id;
      spendResolved.ids = out.fan_ids || [];
      Fastt.toast(esc(el.textContent) + " → " + spendResolved.ids.length + " fans");
    } catch (e) { Fastt.oops(e); spendResolved.key = null; spendResolved.ids = null; }
    updReach();
  }

  // ════════════════════════════════════════════════════ funnels
  function renderFunnels() {
    const sel = $("#selFunnel");
    sel.innerHTML = '<option value="">No funnel — one-shot blast</option>'
      + state.funnels.map((f) => '<option value="' + esc(f.id) + '">' + esc(f.name)
        + " · " + f.step_count + " step" + (f.step_count === 1 ? "" : "s") + "</option>").join("");
    if (!state.funnels.length) {
      sel.innerHTML = '<option value="">No funnels on this account</option>';
      sel.disabled = true;
    }
  }

  // ════════════════════════════════════════════════════ media tray + picker
  function renderTray() {
    const tray = $("#trayDemo");
    const add = $("#tileAdd");
    $$("#trayDemo .tile:not(.addt)").forEach((t) => t.remove());
    const prev = Number($("#selPreviews") ? $("#selPreviews").value : 1) || 0;
    state.media.forEach((id, i) => {
      const free = i < prev;
      const el = document.createElement("div");
      el.className = "tile";
      el.innerHTML = '<img src="' + thumb(id) + '" alt="" loading="lazy"' + IMG_FALLBACK + ">"
        + '<span class="rm" data-rm="' + esc(id) + '" title="remove">×</span>'
        + (free ? '' : '<span class="lk"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="5" y="10" width="14" height="10" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3"/></svg></span>')
        + '<span class="ttag' + (free ? " free" : "") + '">' + (free ? "FREE preview" : "#" + esc(id)) + "</span>";
      tray.insertBefore(el, add);
    });
    const kv = $("#trayKv");
    const price = Number($("#priceInput").value) || 0;
    if (!state.media.length) {
      kv.textContent = price > 0
        ? "No media attached — OF rejects a priced blast with nothing to unlock. Add vault items or clear the price."
        : "No media attached — this goes out as a plain text blast.";
      kv.style.color = price > 0 ? "#e0b05e" : "";
    } else {
      kv.style.color = "";
      kv.textContent = state.media.length + " vault item" + (state.media.length === 1 ? "" : "s")
        + (price > 0 ? " · first " + Math.min(prev, state.media.length) + " sent as a free preview" : " · free blast");
    }
    const pk = $("#prevKv");
    if (pk) {
      pk.textContent = price > 0
        ? "previews = the first " + prev + " of the " + state.media.length + " attached ids; the rest stay locked until they pay $" + price + "."
        : "Free blast — previews only matter once the price is above $0.";
    }
  }
  // Folder-organized vault picker: FOLDERS first (GET …/vault/lists), drill into
  // a folder's media (GET …/vault/media?list_id=…), plus an "All media" flat
  // shortcut + a back-to-folders breadcrumb. Selection persists across nav.
  // async so existing openPicker().catch(Fastt.oops) callers keep working.
  async function openPicker() {
    const back = document.createElement("div");
    back.className = "vp-back";
    back.innerHTML = '<div class="vp"><div class="vp-h">Vault<span class="cap" id="vpCap">loading…</span>'
      + '<button class="fx-btn ghost" id="vpClose" style="height:30px;margin-left:auto">Close</button></div>'
      + '<div class="vp-body" id="vpBody"></div>'
      + '<div class="vp-f"><span style="font-size:12.5px;color:var(--muted)" id="vpSel">0 selected</span>'
      + '<button class="fx-btn" id="vpUse" style="margin-left:auto">Attach</button></div></div>';
    document.body.appendChild(back);
    const close = () => back.remove();
    back.addEventListener("click", (e) => { if (e.target === back || e.target.id === "vpClose") close(); });
    const body = back.querySelector("#vpBody");
    const cap = back.querySelector("#vpCap");
    const selCap = back.querySelector("#vpSel");
    // chosen persists across folder navigation — you can pick from many folders
    const chosen = new Set(state.media.map(String));
    const updSel = () => { selCap.textContent = chosen.size + " selected"; };
    updSel();

    const backSvg = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M15 18l-6-6 6-6"/></svg>';
    const crumb = (label) => '<div class="vp-crumb"><span class="vp-back-btn" data-back="1">' + backSvg
      + 'Folders</span>' + (label ? '<span class="vp-sep">/</span><span class="vp-here">' + esc(label) + '</span>' : '') + '</div>';

    function tileGrid(list) {
      // stash each item's signed CDN thumb so the tray never falls back to the
      // cached-thumb route (which 404s for items the vault AI never saw)
      list.forEach((m) => {
        const f = m.files || {};
        const u = (f.squarePreview && f.squarePreview.url) || (f.thumb && f.thumb.url);
        if (u) vaultUrl.set(String(m.id), u);
      });
      return '<div class="vp-grid">' + list.map((m) => {
        const f = m.files || {};
        const url = (f.squarePreview && f.squarePreview.url) || (f.thumb && f.thumb.url) || (f.preview && f.preview.url);
        return '<div class="vp-i' + (chosen.has(String(m.id)) ? " on" : "") + '" data-id="' + esc(m.id) + '">'
          + (url ? '<img src="' + esc(url) + '" alt="" loading="lazy">' : "")
          + (m.type && m.type !== "photo" ? '<span class="vid">' + esc(m.type) + "</span>" : "")
          + '<span class="id">#' + esc(m.id) + "</span></div>";
      }).join("") + '</div>';
    }
    const honest = (title, detail) => '<div class="vp-honest"><div class="vp-honest-t">' + esc(title)
      + '</div><div class="vp-honest-d">' + esc(detail) + '</div></div>';

    // MEDIA view — listId null ⇒ "All media" (flat), else that folder's media.
    async function showMedia(listId, label) {
      cap.textContent = label;
      body.innerHTML = crumb(label) + '<div class="vp-load">Loading media…</div>';
      try {
        const params = { limit: 48, offset: 0 };
        if (listId) params.list_id = listId;
        const out = await Fastt.get("/api/of/v2/vault/media", params);
        const list = (out && out.list) || [];
        if (!list.length) {
          body.innerHTML = crumb(label) + honest("No media here",
            listId ? "GET /api/of/v2/vault/media?list_id=" + listId + " returned 0 items."
                   : "GET /api/of/v2/vault/media returned 0 items for this creator.");
          return;
        }
        body.innerHTML = crumb(label) + tileGrid(list);
      } catch (e) {
        body.innerHTML = crumb(label) + honest("Vault media unavailable",
          "GET /api/of/v2/vault/media returned " + ((e && e.status) || "an error")
          + " — paste ids manually with the “ids” button instead.");
      }
    }

    // FOLDER view — the creator's folders + an "All media" shortcut.
    async function showFolders() {
      cap.textContent = "loading…";
      body.innerHTML = '<div class="vp-load">Loading folders…</div>';
      let folders = [];
      try {
        const out = await Fastt.get("/api/of/v2/vault/lists", { limit: 50 });
        folders = (out && out.list) || [];
      } catch (e) {
        cap.textContent = "unavailable";
        body.innerHTML = honest("Vault folders unavailable",
          "GET /api/of/v2/vault/lists returned " + ((e && e.status) || "an error")
          + " — paste ids manually with the “ids” button instead.");
        return;
      }
      cap.textContent = folders.length + " folder" + (folders.length === 1 ? "" : "s") + " · click to open";
      const gridSvg = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/></svg>';
      const folderSvg = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>';
      const chev = '<span class="vf-chev"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M9 6l6 6-6 6"/></svg></span>';
      let html = '<div class="vp-folders">';
      html += '<div class="vfolder all" data-list="__all__"><div class="vf-ic">' + gridSvg
        + '</div><div class="vf-meta"><div class="vf-name">All media</div>'
        + '<div class="vf-count">Everything in the vault</div></div>' + chev + '</div>';
      folders.forEach((fd) => {
        const parts = [];
        if (fd.photosCount) parts.push(fd.photosCount + " photo" + (fd.photosCount === 1 ? "" : "s"));
        if (fd.videosCount) parts.push(fd.videosCount + " video" + (fd.videosCount === 1 ? "" : "s"));
        if (fd.gifsCount) parts.push(fd.gifsCount + " gif" + (fd.gifsCount === 1 ? "" : "s"));
        if (fd.audiosCount) parts.push(fd.audiosCount + " audio");
        const count = parts.length ? parts.join(" · ") : "empty";
        html += '<div class="vfolder" data-list="' + esc(fd.id) + '" data-name="' + esc(fd.name || "Folder") + '">'
          + '<div class="vf-ic">' + folderSvg + '</div>'
          + '<div class="vf-meta"><div class="vf-name">' + esc(fd.name || "Untitled folder") + '</div>'
          + '<div class="vf-count">' + esc(count) + '</div></div>' + chev + '</div>';
      });
      html += '</div>';
      if (!folders.length) {
        html += honest("No custom folders", "Use “All media” above to pick from the full vault.");
      }
      body.innerHTML = html;
    }

    // Delegated clicks: folder → open, back → folders, tile → (de)select.
    body.addEventListener("click", (e) => {
      if (e.target.closest("[data-back]")) { showFolders(); return; }
      const fd = e.target.closest(".vfolder");
      if (fd) {
        const lid = fd.getAttribute("data-list");
        if (lid === "__all__") showMedia(null, "All media");
        else showMedia(lid, fd.getAttribute("data-name"));
        return;
      }
      const it = e.target.closest(".vp-i");
      if (it) {
        const id = it.dataset.id;
        if (chosen.has(id)) { chosen.delete(id); it.classList.remove("on"); }
        else { chosen.add(id); it.classList.add("on"); }
        updSel();
      }
    });
    back.querySelector("#vpUse").addEventListener("click", () => {
      state.media = Array.from(chosen).map(Number);
      close(); renderTray(); updReach();
    });

    showFolders();
  }
  $("#trayDemo").addEventListener("click", (e) => {
    const rm = e.target.closest("[data-rm]");
    if (rm) {
      state.media = state.media.filter((x) => String(x) !== rm.dataset.rm);
      renderTray(); return;
    }
    if (e.target.closest("#tileAdd")) openPicker().catch(Fastt.oops);
  });
  // ── composer-bar icons — all four now wired to a real backend / client action
  $("#ciImg").addEventListener("click", () => openPicker().catch(Fastt.oops));

  // one open panel at a time; a second click on the same icon closes it
  function togglePanel(id) {
    ["emojiPanel", "gifPanel", "tplPanel"].forEach((p) => {
      const el = document.getElementById(p);
      if (el) el.classList.toggle("open", p === id && !el.classList.contains("open"));
    });
  }
  function insertAtCursor(str) {
    const ta = $("#composerText");
    const s = ta.selectionStart ?? ta.value.length, e = ta.selectionEnd ?? ta.value.length;
    ta.value = ta.value.slice(0, s) + str + ta.value.slice(e);
    const pos = s + str.length; ta.focus(); ta.setSelectionRange(pos, pos);
  }

  // emoji: pure client-side insert-at-cursor (no backend needed)
  const EMOJI = ["😍","😘","🥰","😏","😈","🔥","💦","🍑","🍆","💕","💋","👅","🥵","😜","🤤","😉","👀","💯","✨","🎉","😊","😅","🙈","💞","😳","🤭","💗","💖","💘","💝","🥴","😻","💅","👑","🎀","🩷","🫦","🤍","🖤","🌸"];
  (function buildEmoji() {
    $("#emojiGrid").innerHTML = EMOJI.map((e) => '<button type="button" data-em="' + e + '">' + e + "</button>").join("");
  })();
  $("#ciEmoji").addEventListener("click", () => togglePanel("emojiPanel"));
  $("#emojiGrid").addEventListener("click", (e) => {
    const b = e.target.closest("[data-em]"); if (!b) return; insertAtCursor(b.dataset.em);
  });

  // gif: GET /api/of/v2/giphy/proxy/gifs/{trending,search}; picked id rides as giphy_id
  let gifLoaded = false;
  function gifThumb(g) {
    const im = g.images || {};
    return (im.fixed_width_small && im.fixed_width_small.url) || (im.preview_gif && im.preview_gif.url)
      || (im.fixed_width && im.fixed_width.url) || (im.downsized && im.downsized.url) || "";
  }
  async function loadGifs(q) {
    const grid = $("#gifGrid");
    grid.innerHTML = '<span style="font-size:12px;color:var(--muted2)">searching…</span>';
    try {
      const path = q ? "/api/of/v2/giphy/proxy/gifs/search" : "/api/of/v2/giphy/proxy/gifs/trending";
      const out = await Fastt.get(path, q ? { q: q, limit: 24 } : { limit: 24 });
      const data = (out && out.data) || [];
      if (!data.length) { grid.innerHTML = '<span style="font-size:12px;color:var(--muted2)">no GIFs</span>'; return; }
      grid.innerHTML = data.map((g) =>
        '<div class="gi" data-gid="' + esc(g.id) + '" data-url="' + esc(gifThumb(g))
        + '" title="' + esc(g.title || "") + '"><img src="' + esc(gifThumb(g)) + '" alt="" loading="lazy"></div>').join("");
    } catch (e) {
      grid.innerHTML = '<span style="font-size:12px;color:var(--muted)">giphy proxy unavailable</span>';
      console.warn("giphy", e);
    }
  }
  function renderGifChip() {
    const host = $("#gifChip");
    if (!state.gif) { host.style.display = "none"; host.innerHTML = ""; return; }
    host.style.display = "";
    host.innerHTML = '<span class="gifchip-in"><img src="' + esc(state.gif.url) + '" alt="">'
      + '<span style="font-size:12px;color:#ddd">1 GIF attached · rides this blast as <code style="color:#9db1fb">giphy_id</code></span>'
      + '<button class="gx" id="gifClear" title="remove">×</button></span>';
  }
  $("#ciGif").addEventListener("click", async () => {
    togglePanel("gifPanel");
    if ($("#gifPanel").classList.contains("open") && !gifLoaded) { gifLoaded = true; await loadGifs(""); }
  });
  $("#gifGo").addEventListener("click", () => loadGifs($("#gifSearch").value.trim()));
  $("#gifSearch").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); loadGifs($("#gifSearch").value.trim()); } });
  $("#gifGrid").addEventListener("click", (e) => {
    const it = e.target.closest(".gi"); if (!it) return;
    state.gif = { id: it.dataset.gid, url: it.dataset.url };
    renderGifChip(); togglePanel(null);
  });
  $("#gifChip").addEventListener("click", (e) => {
    if (e.target.closest("#gifClear")) { state.gif = null; renderGifChip(); }
  });

  // templates: GET /api/of/v2/messages/templates — pick loads text/price/media into the composer
  let tplLoaded = false;
  const stripHtml = (h) => { const d = document.createElement("div"); d.innerHTML = String(h || ""); return (d.textContent || "").trim(); };
  async function loadTemplates() {
    const host = $("#tplList");
    host.innerHTML = '<span style="font-size:12px;color:var(--muted2)">reading templates…</span>';
    try {
      const out = await Fastt.get("/api/of/v2/messages/templates", { limit: 40 });
      const rows = Array.isArray(out) ? out : ((out && out.list) || []);
      state.templates = rows;
      if (!rows.length) {
        host.innerHTML = '<span style="font-size:12px;color:var(--muted2)">No saved templates on this creator — build them in <a href="onboarding-templates.html" style="color:#9db1fb;text-decoration:none">Templates</a>.</span>';
        return;
      }
      host.innerHTML = rows.map((t, i) => {
        const txt = stripHtml(t.displayText || t.text) || "(no text)";
        const marks = [];
        if (t.mediaCount) marks.push(t.mediaCount + " media");
        if ((t.previews || []).length) marks.push(t.previews.length + " free");
        if (t.giphyId) marks.push("GIF");
        if (t.template) marks.push(esc(t.template));
        return '<button type="button" class="tpl-i" data-ti="' + i + '"><span class="tt">'
          + esc(txt.slice(0, 140)) + (txt.length > 140 ? "…" : "")
          + (marks.length ? '<span class="tm">' + marks.join(" · ") + "</span>" : "")
          + "</span>" + (t.price > 0 ? '<span class="tp">$' + esc(t.price) + "</span>" : '<span class="tp" style="color:#8a8a8a;background:#1c1c1c">free</span>') + "</button>";
      }).join("");
    } catch (e) {
      host.innerHTML = '<span style="font-size:12px;color:var(--muted)">Couldn’t read templates.</span>';
      console.warn("templates", e);
    }
  }
  $("#ciTemplate").addEventListener("click", async () => {
    togglePanel("tplPanel");
    if ($("#tplPanel").classList.contains("open") && !tplLoaded) { tplLoaded = true; await loadTemplates(); }
  });
  $("#tplList").addEventListener("click", (e) => {
    const b = e.target.closest(".tpl-i"); if (!b) return;
    const t = (state.templates || [])[Number(b.dataset.ti)]; if (!t) return;
    $("#composerText").value = stripHtml(t.displayText || t.text);
    if (t.price > 0) $("#priceInput").value = t.price;
    // template media ids (numeric) → tray; ignore fresh-claim stubs without ids
    const ids = (t.media || []).map((m) => m && m.id).filter((x) => x != null).map(Number).filter(Boolean);
    if (ids.length) state.media = ids;
    if (t.giphyId) { state.gif = { id: String(t.giphyId), url: "" }; }
    togglePanel(null);
    renderTray(); renderGifChip(); updReach();
    Fastt.toast("Template loaded");
  });
  // click-away closes any open composer panel
  document.addEventListener("click", (e) => {
    if (e.target.closest("#emojiPanel, #gifPanel, #tplPanel, #ciEmoji, #ciGif, #ciTemplate")) return;
    ["emojiPanel", "gifPanel", "tplPanel"].forEach((p) => { const el = document.getElementById(p); if (el) el.classList.remove("open"); });
  });

  // ════════════════════════════════════════════════════ reach + status chips
  function audienceSummary() {
    const inc = picked("incLists"), exc = picked("excLists");
    const online = $("#ckOnline").classList.contains("on");
    const repliers = $("#ckRepliers").classList.contains("on");
    if (spendResolved.ids) return { text: "the " + spendResolved.ids.length + " fans on that smart list", n: spendResolved.ids.length };
    if (repliers) return { text: "fans chatted with in the last 24 h (local DB)", n: null };
    if (!inc.length) return { text: "nobody — pick a list", n: 0 };
    const n = pickedN("incLists"), off = pickedN("excLists");
    return {
      text: inc.length + " list" + (inc.length === 1 ? "" : "s") + (online ? ", online only" : "")
        + (exc.length ? ", minus " + exc.length + " exclude list" + (exc.length === 1 ? "" : "s") : ""),
      n: n, off: off, online: online,
    };
  }
  function updReach() {
    const el = $("#audReach");
    if (!el) return;
    const a = audienceSummary();
    if (spendResolved.ids) {
      el.innerHTML = "Resolved audience: <b>" + Fastt.fmtInt(spendResolved.ids.length)
        + " fan ids</b> from the smart list — sent as <code style=\"color:#9db1fb\">user_ids</code>";
      return;
    }
    if ($("#ckRepliers").classList.contains("on")) {
      el.innerHTML = "Audience: <b>fans chatted with in the last 24 h</b> — resolved from the local DB at send time";
      return;
    }
    if (!a.n) { el.innerHTML = 'Estimated reach: <b style="color:#e0b05e">pick at least one list</b>'; return; }
    if (a.online) {
      el.innerHTML = "Estimated reach: <b>whoever of those " + Fastt.fmtInt(a.n) + "</b> is online at send time";
      return;
    }
    el.innerHTML = "Estimated reach: <b>≤ " + Fastt.fmtInt(a.n) + "</b>"
      + (a.off ? " minus up to " + Fastt.fmtInt(a.off) + " excluded" : "")
      + ' <span style="color:var(--muted2)">(list sizes, before OF de-dupes)</span>';
  }

  // ════════════════════════════════════════════════════ auto-unsend sweep rule
  function unsendRule() {
    // the policy row is the one carrying a policy; a dry_run row is a rehearsal
    return state.unsendRules.find((r) => r.payload && r.payload.policy)
      || state.unsendRules.find((r) => !(r.payload && r.payload.dry_run))
      || state.unsendRules[0] || null;
  }
  function renderUnsend() {
    const r = unsendRule();
    const st = $("#unsRuleState");
    if (!r) {
      st.innerHTML = "<i></i>no unsend_messages rule on this account";
      $$("#cardUnsend input, #cardUnsend button").forEach((e) => { e.disabled = true; });
      $("#unsMeta").textContent = "Nothing sweeps old broadcasts on this creator — every blast stays up until someone unsends it by hand.";
      Fastt.staticBadge($("#cardUnsend .fx-card-h"), "NO RULE ON THIS ACCOUNT");
      setChip("stUnsend", "<i></i>No auto-unsend rule");
      return;
    }
    const p = (r.payload && r.payload.policy) || {};
    st.innerHTML = "<i></i>" + (r.is_enabled
      ? "Sweeping every " + Math.round((r.every_seconds || 3600) / 60) + " min"
      : "Rule #" + r.id + " is OFF — nothing sweeps");
    st.classList.toggle("ok", !!r.is_enabled);
    $("#unsText").value = p.mass_text_hours != null ? p.mass_text_hours : "";
    $("#unsMedia").value = p.mass_media_hours != null ? p.mass_media_hours : "";
    $("#unsPrice").value = p.mass_price_hours != null ? p.mass_price_hours : "";
    const stats = (r.last_run && r.last_run.stats) || {};
    const others = state.unsendRules.filter((x) => x.id !== r.id);
    $("#unsMeta").innerHTML = 'Rule <b>#' + esc(r.id) + "</b> “" + esc(r.name || "unsend_messages")
      + "” · last run " + Fastt.fmtAgo(r.last_run && r.last_run.started_at)
      + (stats.unsent != null ? " · " + stats.unsent + " unsent of " + (stats.targets || 0) + " targets" : "")
      + (others.length ? " · " + others.length + " other unsend_messages row"
          + (others.length === 1 ? "" : "s") + " on this account ("
          + others.map((x) => "#" + x.id + (x.payload && x.payload.dry_run ? " dry-run" : "")).join(", ") + ")"
        : "");
    setChip("stUnsend", "<i></i>" + (r.is_enabled
      ? "Sweep armed · text " + (p.mass_text_hours != null ? p.mass_text_hours + " h" : "off")
        + " / media " + (p.mass_media_hours != null ? p.mass_media_hours + " h" : "off")
        + " / PPV " + (p.mass_price_hours != null ? p.mass_price_hours + " h" : "off")
      : "Sweep rule #" + r.id + " is OFF"));
  }
  $("#unsSave").addEventListener("click", async () => {
    const r = unsendRule(); if (!r) return;
    const btn = $("#unsSave"); btn.disabled = true;
    try {
      const num = (id) => { const n = parseFloat($("#" + id).value); return isFinite(n) && n > 0 ? n : null; };
      const policy = {};
      const t = num("unsText"), m = num("unsMedia"), pz = num("unsPrice");
      if (t != null) policy.mass_text_hours = t;
      if (m != null) policy.mass_media_hours = m;
      if (pz != null) policy.mass_price_hours = pz;
      const payload = Object.assign({}, r.payload || {}, { policy: policy });
      await Fastt.patch("/admin/automation-rules/" + r.id, { payload: payload });
      const map = await Fastt.rulesByKind();
      state.unsendRules = map.unsend_messages || [];
      renderUnsend();
      Fastt.saved("Unsend policy saved");
    } catch (e) { Fastt.oops(e); }
    btn.disabled = false;
  });

  // ════════════════════════════════════════════════════ repeat from pool
  function repMsg() {
    const p = (state.premade && state.premade.payload) || {};
    return (p.messages && p.messages[0]) || {};
  }
  function renderRepeat() {
    const card = $("#cardRepeat"), r = state.premade;
    if (!r) {
      $(".fx-tc-state", card).textContent = "No rule";
      $("#repLast").textContent = "This creator has no mass_premade rule, so nothing repeats. Create one in Automations — a rule born empty has no pool and would send nothing.";
      $("#repSw").style.pointerEvents = "none";
      $("#repSw").style.opacity = ".4";
      card.classList.remove("on");
      $$("#repKnobs input, #repKnobs button, #repKnobs .addln").forEach((e) => { e.style.pointerEvents = "none"; e.disabled = true; });
      Fastt.staticBadge($("#cardRepeat .fx-tc-title"), "NO RULE ON THIS ACCOUNT");
      return;
    }
    Fastt.liveBadge($("#cardRepeat .fx-tc-title"));
    const m = repMsg();
    const on = !!r.is_enabled;
    card.classList.toggle("on", on);
    $("#repSw").classList.toggle("on", on);
    const stEl = $(".fx-tc-state", card);
    stEl.textContent = on ? "Running" : "Off";
    stEl.style.color = on ? "" : "var(--muted)";
    const lr = r.last_run, s = (lr && lr.stats) || {};
    $("#repLast").innerHTML = lr
      ? ("Rule <b>#" + esc(r.id) + "</b> · last fired " + esc(Fastt.fmtAgo(lr.started_at))
        + " · " + esc(s.sent != null ? s.sent + " sent" : (lr.status || "no stats"))
        + (s.mass_run_id ? " · mass run " + esc(s.mass_run_id) : "")
        + (s.queue_id ? " · queue " + esc(s.queue_id) : "")
        + (s.unsend_at ? " · unsends " + esc(Fastt.fmtDate(s.unsend_at)) : "")
        + (s.resends_left != null ? " · " + esc(s.resends_left) + " resends left" : ""))
      : ("Rule <b>#" + esc(r.id) + "</b> · never fired");
    $("#repEvery").value = Math.round((r.every_seconds || 7200) / 60);
    $("#repUnsend").value = m.unsend_after_hours != null ? m.unsend_after_hours : "";
    $("#repLists").value = (m.user_lists || []).join(", ");
    $("#repRepl").value = m.exclude_replied_hours != null ? m.exclude_replied_hours : "";
    $("#repInb").value = m.exclude_inbound_hours != null ? m.exclude_inbound_hours : "";
    $("#repOnline").classList.toggle("on", m.online_only === true);
    state.repTexts = (m.texts || (m.text ? [m.text] : [])).slice();
    state.repMedia = (m.media_files || []).slice();
    renderRepPool();
  }
  function renderRepPool() {
    const host = $("#repTexts");
    host.innerHTML = "";
    (state.repTexts.length ? state.repTexts : [""]).forEach((t) => {
      const i = document.createElement("input");
      i.className = "fx-input";
      i.value = t;
      i.placeholder = "a line for the pool…";
      host.appendChild(i);
    });
    setTxt("repTextN", state.repTexts.length + " line" + (state.repTexts.length === 1 ? "" : "s") + " · one picked at random per fire");
    const mh = $("#repMedia");
    mh.innerHTML = state.repMedia.slice(0, 11).map((id) =>
      '<span class="rt" title="vault #' + esc(id) + '"><img src="' + thumb(id)
      + '" alt="" loading="lazy"' + IMG_FALLBACK + "></span>").join("")
      + (state.repMedia.length > 11 ? '<span class="more">+' + (state.repMedia.length - 11) + "</span>" : "")
      + (state.repMedia.length ? "" : '<span style="font-size:12.5px;color:var(--muted2)">no media — this pool sends text only</span>');
    setTxt("repMediaN", state.repMedia.length + " vault item" + (state.repMedia.length === 1 ? "" : "s"));
  }
  $("#repAddLine").addEventListener("click", () => {
    const i = document.createElement("input");
    i.className = "fx-input"; i.placeholder = "new line…";
    $("#repTexts").appendChild(i); i.focus();
  });
  $("#repEditMedia").addEventListener("click", () => {
    const raw = prompt("Vault media ids for the pool (comma-separated, blank = text-only):",
      state.repMedia.join(", "));
    if (raw === null) return;
    state.repMedia = raw.split(/[,\s]+/).filter(Boolean)
      .filter((x) => /^\d+$/.test(x)).map(Number);
    renderRepPool();
    Fastt.toast("Media pool staged — hit “Save pool” to write it");
  });
  $("#repSave").addEventListener("click", async () => {
    const r = state.premade; if (!r) return;
    const btn = $("#repSave"); btn.disabled = true;
    try {
      const texts = $$("#repTexts .fx-input").map((i) => i.value.trim()).filter(Boolean);
      const num = (id) => { const n = parseFloat($("#" + id).value); return isFinite(n) && n > 0 ? n : null; };
      const msg = Object.assign({}, repMsg());
      msg.texts = texts;
      delete msg.text;
      msg.media_files = state.repMedia;
      msg.online_only = $("#repOnline").classList.contains("on");
      const lists = $("#repLists").value.split(/[,\s]+/).filter(Boolean);
      if (lists.length) msg.user_lists = lists; else delete msg.user_lists;
      const setOrDrop = (k, v) => { if (v == null) delete msg[k]; else msg[k] = v; };
      setOrDrop("unsend_after_hours", num("repUnsend"));
      setOrDrop("exclude_replied_hours", num("repRepl"));
      setOrDrop("exclude_inbound_hours", num("repInb"));
      const payload = Object.assign({}, r.payload || {}, { messages: [msg] });
      const every = parseInt($("#repEvery").value, 10);
      await Fastt.patch("/admin/automation-rules/" + r.id, {
        payload: payload,
        every_seconds: (isFinite(every) && every > 0) ? every * 60 : r.every_seconds,
      });
      const map = await Fastt.rulesByKind();
      state.premade = (map.mass_premade || [])[0] || null;
      renderRepeat();
      Fastt.saved("Pool saved");
    } catch (e) { Fastt.oops(e); }
    btn.disabled = false;
  });
  // toggling the rule is an explicit, confirmed click (it messages real fans)
  document.addEventListener("click", async (e) => {
    if (!e.target.closest("#repSw")) return;
    const r = state.premade;
    const wantOn = $("#repSw").classList.contains("on");
    if (!r) { renderRepeat(); return; }
    if (wantOn && !confirm("Turn the repeating pool blast ON?\n\nIt will fire every "
        + Math.round((r.every_seconds || 7200) / 60) + " min and send a REAL mass message to "
        + ((repMsg().user_lists || []).join(", ") || "its configured audience") + " with no further prompt.")) {
      renderRepeat(); return;
    }
    try {
      await Fastt.patch("/admin/automation-rules/" + r.id, { is_enabled: wantOn });
      const map = await Fastt.rulesByKind();
      state.premade = (map.mass_premade || [])[0] || null;
      renderRepeat();
      Fastt.saved(wantOn ? "Repeat enabled" : "Repeat disabled");
    } catch (e2) { Fastt.oops(e2); renderRepeat(); }
  });

  // ════════════════════════════════════════════════════ revenue by automation
  async function loadRevenue() {
    const host = $("#revBars");
    const days = Number($("#revRange").value) || 30;
    const iso = (d) => d.toISOString().slice(0, 10);
    host.innerHTML = '<div style="color:var(--muted2);font-size:12.5px">reading…</div>';
    try {
      const out = await Fastt.get("/admin/stats/per-automation", {
        from: iso(new Date(Date.now() - days * 864e5)), to: iso(new Date()),
      });
      const rows = (out && out.rows) || [];
      if (!rows.length) {
        host.innerHTML = '<div style="color:var(--muted)">No automation activity in this window.</div>';
        return;
      }
      // scale from the data: the top earner is 100%, everything else in ratio
      const max = Math.max(1, ...rows.map((r) => r.revenue_cents || 0));
      host.innerHTML = rows.slice(0, 10).map((r) => {
        const cents = r.revenue_cents || 0;
        const pct = cents > 0 ? Math.max(2, Math.round((cents / max) * 100)) : 0;
        return '<div class="revrow"><span class="k" title="' + esc(r.automation) + '">' + esc(r.automation) + "</span>"
          + '<span class="track"><span class="revbar' + (cents ? "" : " zero") + '" style="width:' + pct + '%"></span></span>'
          + '<span class="v' + (cents ? "" : " zero") + '">' + Fastt.fmtCents(cents) + "</span>"
          + '<span class="n">' + Fastt.fmtInt(r.messages_sent || 0) + " sent</span></div>";
      }).join("")
        // axis labels come from the real scale, not a fixed ladder
        + '<div class="revaxis"><span class="k"></span><span class="sc"><span>$0</span><span>'
        + esc(Fastt.fmtCents(max / 2)) + "</span><span>" + esc(Fastt.fmtCents(max))
        + '</span></span><span class="pad"></span></div>';
    } catch (e) {
      host.innerHTML = '<div style="color:var(--muted)">Couldn’t read /admin/stats/per-automation.</div>';
      console.warn(e);
    }
  }
  $("#revRange").addEventListener("change", () => loadRevenue());

  // ════════════════════════════════════════════════════ history
  const NO_THUMB = '<span class="mthumb none"><svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M20 12a8 8 0 0 1-11.3 7.3L4 20l.7-4.7A8 8 0 1 1 20 12z"/></svg></span>';
  function whenCell(iso) {
    const d = Fastt.parseUtc(iso);
    if (!d) return '<td class="when">—</td>';
    return '<td class="when">' + esc(d.toLocaleDateString(undefined, { month: "short", day: "numeric" }))
      + " " + esc(d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" }))
      + "<small>" + esc(Fastt.fmtAgo(iso)) + "</small></td>";
  }
  function renderHistory() {
    const body = $("#histBody");
    const hide = $("#ckHideUnsent").classList.contains("on");
    const items = hide ? state.hist.filter((i) => !i.isCanceled) : state.hist;
    setTxt("histCount", state.hist.length
      ? (items.length + " of " + state.hist.length + " shown")
      : "");
    if (!items.length) {
      body.innerHTML = '<tr><td colspan="7" style="color:var(--muted)">'
        + (state.hist.length ? "Every blast in this window is unsent — untick “Hide unsent”."
                             : "No mass messages in this window.") + "</td></tr>";
      return;
    }
    body.innerHTML = items.map((it) => {
      const turl = it.media && it.media[0] && it.media[0].files && it.media[0].files.thumb
        && it.media[0].files.thumb.url;
      const th = turl ? '<span class="mthumb" style="background:url(\'' + esc(turl) + '\') center/cover"></span>' : NO_THUMB;
      const marks = [];
      if (it.mediaCount) marks.push('<span class="mmark">' + it.mediaCount + " media</span>");
      if ((it.previews || []).length) marks.push('<span class="mmark">' + it.previews.length + " free</span>");
      if (it.giphyId) marks.push('<span class="mmark">GIF</span>');
      if (it.isTip) marks.push('<span class="mmark">tip</span>');
      const badge = it.isFree ? '<span class="pbadge free">free</span>' : '<span class="pbadge">PPV</span>';
      const aud = it.funnelName ? esc(it.funnelName) + " (funnel)"
        : (it.automationKind ? esc(it.automationKind)
          : (it.template ? '<span style="color:var(--muted)">' + esc(it.template) + "</span>" : "Mass blast"));
      const state_ = it.isCanceled
        ? '<span class="stchip un">Unsent</span>'
        : '<span class="stchip ok">Delivered</span>';
      const act = (it.canUnsend && !it.isCanceled)
        ? '<button class="fx-btn ghost btn-uns" data-qid="' + esc(it.id) + '">Unsend</button>'
        : '<span style="color:var(--muted2);font-size:11px">'
          + (it.isCanceled ? "already unsent" : "unsend window closed") + "</span>";
      return "<tr>" + whenCell(it.date)
        + '<td><div class="msgcell">' + th + '<span class="mtext" title="' + esc(it.text || "") + '">'
        + esc(it.text || "(no text)") + "</span>" + badge + marks.join("") + "</div></td>"
        + "<td>" + aud + "</td>"
        + '<td style="text-align:right">' + Fastt.fmtInt(it.sentCount) + "</td>"
        + '<td style="text-align:right;color:' + (it.viewedCount ? "#cfcfcf" : "var(--muted2)") + '">'
        + Fastt.fmtInt(it.viewedCount) + "</td>"
        + "<td>" + state_ + "</td>"
        + '<td style="text-align:right">' + act + "</td></tr>";
    }).join("");
  }
  async function loadHistory() {
    const body = $("#histBody");
    const days = Number($("#histDays").value) || 30;
    body.innerHTML = '<tr><td colspan="7" style="color:var(--muted2)">reading…</td></tr>';
    try {
      const out = await Fastt.get("/admin/mass-messages", { days_back: days, limit: 60 });
      state.hist = (out && out.items) || [];
      setChip("stLast", "<i></i>" + (state.hist.length
        ? "Last blast " + Fastt.fmtAgo(state.hist[0].date) + " · " + Fastt.fmtInt(state.hist[0].sentCount) + " fans"
        : "No blasts in the last " + days + " days"));
      renderHistory();
    } catch (e) {
      state.hist = [];
      body.innerHTML = '<tr><td colspan="7" style="color:var(--muted)">Couldn’t load history.</td></tr>';
      Fastt.oops(e);
    }
  }
  $("#histDays").addEventListener("change", () => loadHistory());
  $("#histReload").addEventListener("click", () => loadHistory());
  $("#histRefresh").addEventListener("click", async () => {
    const btn = $("#histRefresh"); btn.disabled = true;
    const days = Number($("#histDays").value) || 30;
    try {
      // read-only against OF: refills the relay's cache, sends nothing
      const out = await Fastt.post("/admin/mass-messages/refresh?account_id="
        + encodeURIComponent(acct) + "&days_back=" + days + "&limit=60", undefined);
      Fastt.saved("Pulled " + ((out && out.written) != null ? out.written : "?") + " rows from OF");
      await loadHistory();
    } catch (e) { Fastt.oops(e); }
    btn.disabled = false;
  });
  document.addEventListener("click", (e) => {
    if (e.target.closest("#ckHideUnsent")) setTimeout(renderHistory, 0);
  });
  $("#histBody").addEventListener("click", async (e) => {
    const btn = e.target.closest(".btn-uns");
    if (!btn) return;
    const qid = btn.dataset.qid;
    const it = state.hist.find((x) => String(x.id) === String(qid)) || {};
    if (!confirm("UNSEND this blast?\n\n“" + String(it.text || "").slice(0, 120) + "”\n\n"
      + "It disappears from every one of the " + (it.sentCount || 0)
      + " fans it reached — including anyone who already paid. This cannot be undone.")) return;
    btn.disabled = true;
    try {
      await Fastt.del("/api/of/v2/messages/queue/" + qid);
      Fastt.saved("Unsent ✓");
      await loadHistory();
    } catch (e2) { Fastt.oops(e2); btn.disabled = false; }
  });

  // ════════════════════════════════════════════════════ exclude-list counts
  (function fillExcludeCounts() {
    const byName = (n) => state.lists.find((x) => x.name === n);
    const ppvN = (byName("MASSppvEXCLUDE") || {}).usersCount ?? 0;
    const dmN = (byName("MASSdmEXCLUDE") || {}).usersCount ?? 0;
    const autoN = (byName("Auto_Exclude") || {}).usersCount ?? 0;
    setTxt("chipPpvEx", "PPV exclude list · " + ppvN);
    setTxt("chipDmEx", "DM exclude list · " + dmN);
    setTxt("exlPpvChip", "MASSppvEXCLUDE · " + ppvN);
    setTxt("exlDmChip", "MASSdmEXCLUDE · " + dmN);
    setTxt("exlAutoChip", "Auto_Exclude · " + autoN);
    setTxt("exlPpvCount", ppvN + " fans");
    setTxt("exlDmCount", dmN + " fans");
  })();

  // ════════════════════════════════════════════════════ status strip
  function updUnsendChip() {
    const on = $("#ckUnsend").classList.contains("on");
    const hrs = Number(($("#inUnsendHrs") || {}).value) || 4;
    setTxt("unsendHrsLbl", String(hrs));
    $("#ckUnsend").style.opacity = on ? "" : ".65";
  }
  setChip("stReady", "<i></i>Ready — " + esc(nick) + " connected");
  try {
    const q = await Fastt.get("/api/of/v2/messages/queue", { limit: 50 });
    const qn = Array.isArray(q) ? q.length : ((q && q.list) || []).length;
    setChip("stSched", "<i></i>" + qn + " scheduled on OF");
  } catch (e) { setChip("stSched", "<i></i>scheduled queue unavailable"); }

  // ════════════════════════════════════════════════════ send / schedule
  function buildBody(scheduledIso) {
    const text = ($("#composerText").value || "").trim();
    const price = Math.round(Math.max(0, Number($("#priceInput").value) || 0) * 100) / 100;
    const body = { text: text, price: price };
    if (spendResolved.ids && spendResolved.ids.length) {
      body.user_ids = spendResolved.ids;
    } else if ($("#ckRepliers").classList.contains("on")) {
      body.recent_chat_hours = 24;
    } else {
      const inc = picked("incLists");
      if (inc.length) body.user_lists = inc;
    }
    const exc = picked("excLists");
    if (exc.length) body.excluded_user_lists = exc;
    if ($("#ckOnline").classList.contains("on")) body.online_only = true;
    if ($("#chipInbound").classList.contains("on")) body.exclude_inbound_hours = 1;
    if ($("#chipReplied").classList.contains("on")) body.exclude_replied_hours = 12;
    if (!scheduledIso && $("#ckUnsend").classList.contains("on")) {
      body.unsend_after_hours = Number(($("#inUnsendHrs") || {}).value) || 4;
    }
    if (state.media.length) {
      body.media_files = state.media.slice();
      const prev = Number($("#selPreviews").value) || 0;
      if (price > 0 && prev > 0) body.previews = state.media.slice(0, prev);
    }
    if (price > 0 && $("#ckLockedText").classList.contains("on")) body.locked_text = true;
    if (state.gif && state.gif.id) body.giphy_id = state.gif.id;
    // advanced online-targeting extras — only attach positive values the wire accepts
    const advNum = (id) => { const n = parseFloat(($("#" + id) || {}).value); return isFinite(n) && n > 0 ? n : null; };
    const ur = advNum("advUnread"); if (ur != null) body.unread_limit = Math.floor(ur);
    const rcl = advNum("advRecentCap"); if (rcl != null) body.recent_chat_limit = Math.floor(rcl);
    const fid = parseInt($("#selFunnel").value, 10);
    if (isFinite(fid) && fid > 0) body.funnel_id = fid;
    if (scheduledIso) body.scheduled_date = scheduledIso;
    return body;
  }
  async function fire(scheduledIso) {
    const body = buildBody(scheduledIso);
    if (!body.text && !state.media.length && !body.giphy_id) {
      Fastt.toast("Add text, media, or a GIF first", "err"); return;
    }
    if (!body.user_lists && !body.user_ids && !body.recent_chat_hours) {
      Fastt.toast("Pick an audience — at least one list, a smart list, or the repliers box", "err"); return;
    }
    if (body.price > 0 && !state.media.length) {
      Fastt.toast("A priced blast needs vault media — OF has nothing to lock otherwise", "err"); return;
    }
    const gifTag = body.giphy_id ? " + GIF" : "";
    const what = body.price > 0
      ? "$" + body.price + " PPV blast (" + state.media.length + " media, "
        + ((body.previews || []).length) + " free preview" + gifTag + ")"
      : "FREE blast" + (state.media.length ? " (" + state.media.length + " media" + gifTag + ")"
          : (body.giphy_id ? " (GIF" + (body.text ? " + text" : "") + ")" : " (text only)"));
    const who = audienceSummary().text;
    const when = scheduledIso ? "at " + new Date(scheduledIso).toLocaleString() : "NOW";
    const fn = body.funnel_id
      ? "\nRepliers will walk funnel #" + body.funnel_id + "."
      : "";
    if (!confirm("Send " + what + "\nto " + who + " " + when + " from " + nick + "?"
      + fn + "\n\nThis messages real fans.")) return;
    try {
      const res = await Fastt.post("/api/of/v2/messages/queue", body);
      Fastt.saved(scheduledIso ? "Scheduled ✓" : "Blast sent ✓"
        + (res && res.id ? " (queue " + res.id + ")" : ""));
      await loadHistory();
    } catch (e) { Fastt.oops(e); }
  }

  // ════════════════════════════════════════════════════ wiring + first paint
  renderLists();
  renderSpend();
  renderFunnels();
  renderTray();
  renderUnsend();
  renderRepeat();
  updUnsendChip();
  updReach();
  await Promise.all([loadHistory(), loadRevenue()]);

  document.addEventListener("click", (e) => {
    if (e.target.closest("#incLists, #excLists, #ckOnline, #ckRepliers")) setTimeout(updReach, 0);
    if (e.target.closest("#ckUnsend")) setTimeout(updUnsendChip, 0);
    const sp = e.target.closest("#spendChips .fx-chip");
    if (sp) setTimeout(() => onSpendPick(sp), 0);
  });
  $("#priceInput").addEventListener("input", () => renderTray());
  $("#selPreviews").addEventListener("change", () => renderTray());
  $("#inUnsendHrs").addEventListener("input", updUnsendChip);
  const lnk = $("#lnkRepeat");
  if (lnk) lnk.addEventListener("click", (e) => {
    e.preventDefault();
    $("#cardRepeat").scrollIntoView({ behavior: "smooth", block: "center" });
  });

  // No creator resolved (unauthed /admin/accounts) → the relay would fall back
  // to whatever account it considers current. Never let a blast leave from an
  // account this page can't name. Same for an audience it couldn't read.
  const blockedWhy = !Fastt.hasAccount()
    ? "Pick a creator first — this page can't name the sending account"
    : (!state.lists.length ? "OF list lookup failed — a blast can't be aimed" : null);
  if (blockedWhy) {
    ["#btnSend", "#btnSchedule"].forEach((sel) => {
      const b = $(sel);
      if (!b) return;
      b.disabled = true;
      b.style.opacity = ".5";
      b.title = blockedWhy;
    });
    setChip("stReady", "<i></i>" + esc(blockedWhy));
    return;
  }
  $("#btnSend").addEventListener("click", () => fire(null));
  $("#btnSchedule").addEventListener("click", () => {
    const raw = prompt("Send at (YYYY-MM-DD HH:MM, your local time):");
    if (!raw) return;
    const d = new Date(raw.trim().replace(" ", "T"));
    if (isNaN(d.getTime())) { Fastt.toast("Couldn’t parse that date", "err"); return; }
    if (d.getTime() < Date.now() + 60 * 1000) { Fastt.toast("Pick a time in the future", "err"); return; }
    fire(d.toISOString());
  });
});
