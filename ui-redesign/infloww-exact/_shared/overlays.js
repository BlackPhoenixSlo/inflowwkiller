/* overlays.js — the always-on chrome ported from the real fastt app:
 *   • MoneyRail   — sticky, draggable bottom-right dock of live buys + tips
 *   • money toast — a pop when a buy/tip lands over SSE
 *   • bell menu   — the topbar 🔔 becomes a live notifications dropdown
 *
 * Loaded automatically by _shared/fastt.js (no per-page include needed). Uses
 * only Fastt.* helpers and the same relay endpoints the real MoneyRail/
 * NotificationBell use:
 *   GET /api/of/v2/users/notifications?type=purchases   (buys)
 *   GET /api/of/v2/users/notifications?type=tip         (tips)
 *   SSE /events  (named frames: purchase_notified, toasts, new_message, tip…)
 * All scoped to the current creator via the X-Account-Id header Fastt injects.
 */
(function () {
  "use strict";
  if (!window.Fastt) return;
  const F = window.Fastt;
  const esc = F.esc;

  // ── shared css ───────────────────────────────────────────────
  const CSS = `
  .ft-rail{position:fixed;right:18px;bottom:18px;z-index:9990;width:296px;background:#1c1c1c;
    border:1px solid #333;border-radius:12px;box-shadow:0 12px 40px rgba(0,0,0,.55);
    font:13px Inter,sans-serif;color:#fff;overflow:hidden;user-select:none}
  .ft-rail-head{display:flex;align-items:center;gap:8px;padding:9px 11px;background:#232323;
    border-bottom:1px solid #333;cursor:grab}
  .ft-rail-head:active{cursor:grabbing}
  .ft-rail-title{font-weight:700;font-size:12.5px;display:flex;align-items:center;gap:6px}
  .ft-rail-new{background:#67d1ae;color:#06281f;border-radius:99px;font-size:10px;font-weight:800;
    padding:1px 6px;display:none}
  .ft-rail-sp{flex:1}
  .ft-rail-filters{display:flex;gap:3px}
  .ft-rail-filters button{background:transparent;border:1px solid #383838;color:#9a9a9a;font:600 10.5px Inter;
    border-radius:99px;padding:2px 8px;cursor:pointer}
  .ft-rail-filters button.on{background:#4166f6;border-color:#4166f6;color:#fff}
  .ft-rail-x{background:transparent;border:0;color:#9a9a9a;cursor:pointer;font-size:15px;line-height:1;padding:2px}
  .ft-rail-scope{display:flex;gap:4px;padding:7px 9px;border-bottom:1px solid #2a2a2a;background:#191919}
  .ft-rail-scope button{flex:1;background:transparent;border:1px solid #333;color:#9a9a9a;
    font:600 11px Inter;border-radius:7px;padding:4px 6px;cursor:pointer}
  .ft-rail-scope button.on{background:#4166f6;border-color:#4166f6;color:#fff}
  .ft-rail-body{max-height:320px;overflow:auto}
  /* Collapsed = a compact pill (just the title + expand), so the dock barely
     overlaps page content. Filters/scope/body reappear on expand. */
  .ft-rail.collapsed{width:auto}
  .ft-rail.collapsed .ft-rail-body,.ft-rail.collapsed .ft-rail-scope,
  .ft-rail.collapsed .ft-rail-filters,.ft-rail.collapsed .ft-rail-sp{display:none}
  .ft-rail-row{display:flex;align-items:center;gap:9px;padding:9px 11px;border-bottom:1px solid #262626;
    text-decoration:none;color:#fff}
  .ft-rail-row:hover{background:#242424}
  .ft-rail-row .av{width:32px;height:32px;border-radius:50%;object-fit:cover;background:#333;flex:none}
  .ft-rail-row .mid{flex:1;min-width:0}
  .ft-rail-row .nm{font-weight:600;font-size:12.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .ft-rail-row .sub{color:#8a8a8a;font-size:11px}
  .ft-rail-row .amt{font-weight:800;font-size:13px}
  .ft-rail-row .amt.buy{color:#67d1ae}.ft-rail-row .amt.tip{color:#ec4b9b}
  .ft-rail-empty{padding:20px 14px;text-align:center;color:#8a8a8a;font-size:12px}
  .ft-money-toasts{position:fixed;right:18px;bottom:0;z-index:9992;display:flex;flex-direction:column;gap:8px;
    padding-bottom:8px}
  .ft-mt{display:flex;align-items:center;gap:10px;background:#232323;border:1px solid #333;border-left:3px solid #67d1ae;
    border-radius:10px;padding:10px 12px;min-width:250px;box-shadow:0 8px 28px rgba(0,0,0,.5);
    color:#fff;font:13px Inter;opacity:0;transform:translateY(8px);transition:all .2s;cursor:pointer;text-decoration:none}
  .ft-mt.tip{border-left-color:#ec4b9b}
  .ft-mt.on{opacity:1;transform:none}
  .ft-mt .av{width:34px;height:34px;border-radius:50%;object-fit:cover;background:#333;flex:none}
  .ft-mt .amt{font-weight:800;margin-left:auto}
  .ft-bell-menu{position:fixed;z-index:9993;width:340px;max-height:440px;overflow:auto;background:#1c1c1c;
    border:1px solid #333;border-radius:12px;box-shadow:0 14px 44px rgba(0,0,0,.6);font:13px Inter;color:#fff}
  .ft-bell-menu .bh{display:flex;align-items:center;padding:11px 13px;border-bottom:1px solid #333;font-weight:700}
  .ft-bell-menu .bh a{margin-left:auto;color:#4166f6;font-size:12px;font-weight:600;text-decoration:none}
  .ft-bell-row{display:flex;gap:9px;padding:10px 13px;border-bottom:1px solid #262626;text-decoration:none;color:#fff}
  .ft-bell-row:hover{background:#242424}
  .ft-bell-row.unread{background:rgba(65,102,246,.06)}
  .ft-bell-row .av{width:30px;height:30px;border-radius:50%;object-fit:cover;background:#333;flex:none}
  .ft-bell-row .tx{flex:1;min-width:0;font-size:12.5px;line-height:1.4}
  .ft-bell-row .tx b{font-weight:700}
  .ft-bell-row .ago{color:#8a8a8a;font-size:11px;margin-top:2px}
  .ft-bell-empty{padding:22px;text-align:center;color:#8a8a8a}`;

  function css() {
    if (document.getElementById("ft-overlays-css")) return;
    const s = document.createElement("style");
    s.id = "ft-overlays-css"; s.textContent = CSS;
    document.head.appendChild(s);
  }

  // ── data ─────────────────────────────────────────────────────
  const amountOf = (n) => (n.replacePairs && n.replacePairs["{AMOUNT}"]) ||
    ((n.text || "").match(/\$[\d,.]+/) || [""])[0];
  const nameOf = (n) => (n.user && n.user.name) ||
    (n.replacePairs && n.replacePairs["{NAME}"]) || "Fan";
  const kindOf = (n) => (n.type === "tip" ? "tip" : "buy");
  const whatOf = (n) => {
    if (n.type === "tip") return "tip";
    const st = n.subType || "";
    if (st.indexOf("chat") >= 0 || st.indexOf("message") >= 0) return "message";
    if (st.indexOf("post") >= 0) return "post";
    if (st.indexOf("stream") >= 0) return "stream";
    return "purchase";
  };

  const creNameOf = (aid) =>
    ((F.accounts() || []).find((a) => String(a.id) === String(aid)) || {}).nickname || String(aid);

  async function fetchMoney(type, aid) {
    // aid omitted => current creator (Fastt injects its header). aid given =>
    // that creator, via a per-request X-Account-Id override (for the all-creators
    // fan-out). Every row is tagged with its source account so rows from
    // different creators can be merged, labeled, and deep-linked correctly.
    const opts = aid ? { noAccount: true, headers: { "X-Account-Id": String(aid) } } : undefined;
    const who = aid || F.account();
    try {
      const rows = await F.get("/api/of/v2/users/notifications", { type, limit: 20 }, opts);
      const list = Array.isArray(rows) ? rows : (rows.list || []);
      list.forEach((r) => { r.__aid = String(who); r.__creator = creNameOf(who); });
      return list;
    } catch (e) { return null; }
  }

  // ── MoneyRail ────────────────────────────────────────────────
  // NOT messages.html: the inbox already shows per-fan spend + the bell, and a
  // fixed dock there covered the Fan Insights column. Home/leaderboard only.
  // NOT leaderboard.html: its ranking table is full-bleed to the bottom-right,
  // so even a collapsed dock overlaps the lowest rows.
  const RAIL_SURFACES = ["dashboard.html", ""];
  function railWanted() {
    const page = (location.pathname.split("/").pop() || "");
    if (document.body && document.body.hasAttribute("data-no-money-rail")) return false;
    if (document.body && document.body.hasAttribute("data-money-rail")) return true;
    return RAIL_SURFACES.indexOf(page) >= 0;
  }

  const LS = "ft_rail";
  const railState = (() => {
    try { return JSON.parse(localStorage.getItem(LS)) || {}; }
    catch (e) { return {}; }
  })();
  const saveRail = () => { try { localStorage.setItem(LS, JSON.stringify(railState)); } catch (e) {} };

  let railEl, railBody, railData = { purchases: [], tip: [] }, railFilter = railState.filter || "all";
  let lastSeenId = railState.lastSeen || 0;

  function railRows() {
    let rows = [];
    if (railFilter === "all") rows = railData.purchases.concat(railData.tip);
    else rows = railData[railFilter] || [];
    return rows.sort((a, b) => (F.parseUtc(b.createdAt) || 0) - (F.parseUtc(a.createdAt) || 0));
  }

  function renderRail() {
    if (!railBody) return;
    const rows = railRows();
    const newN = rows.filter((r) => Number(r.id) > lastSeenId).length;
    const nb = railEl.querySelector(".ft-rail-new");
    if (nb) { nb.style.display = newN ? "" : "none"; nb.textContent = "+" + newN; }
    if (!rows.length) {
      railBody.innerHTML = '<div class="ft-rail-empty">No recent buys or tips for this creator.</div>';
      return;
    }
    const allScope = railState.scope === "all";
    railBody.innerHTML = rows.slice(0, 40).map((n) => {
      const k = kindOf(n), fan = (n.user && n.user.id) || "";
      const aid = n.__aid || F.account();
      const href = "messages.html?account=" + encodeURIComponent(aid) + "&fan=" + encodeURIComponent(fan);
      const av = (n.user && n.user.avatar)
        ? '<img class="av" src="' + esc(n.user.avatar) + '" loading="lazy">'
        : '<span class="av"></span>';
      // In all-creators mode, name the creator the money landed on.
      const sub = (allScope ? esc(n.__creator) + " · " : "") + esc(whatOf(n)) + " · " + esc(F.fmtAgo(n.createdAt));
      return '<a class="ft-rail-row" href="' + href + '">' + av +
        '<div class="mid"><div class="nm">' + esc(nameOf(n)) + '</div>' +
        '<div class="sub">' + sub + '</div></div>' +
        '<div class="amt ' + k + '">' + esc(amountOf(n)) + '</div></a>';
    }).join("");
  }

  function mountRail() {
    if (!railWanted() || !F.account()) return;
    css();
    // Default COLLAPSED (just the header bar) so the dock never covers page
    // content on first load — the user expands it when they want the feed.
    const startCollapsed = ("collapsed" in railState) ? railState.collapsed : true;
    railState.collapsed = startCollapsed;
    railEl = document.createElement("div");
    railEl.className = "ft-rail" + (startCollapsed ? " collapsed" : "");
    railEl.innerHTML =
      '<div class="ft-rail-head">' +
        '<span class="ft-rail-title">💸 Money <span class="ft-rail-new"></span></span>' +
        '<span class="ft-rail-sp"></span>' +
        '<div class="ft-rail-filters">' +
          '<button data-f="all">All</button>' +
          '<button data-f="purchases">Buys</button>' +
          '<button data-f="tip">Tips</button>' +
        '</div>' +
        '<button class="ft-rail-x" title="expand/collapse">' + (startCollapsed ? "▴" : "▾") + '</button>' +
      '</div>' +
      '<div class="ft-rail-scope">' +
        '<button data-s="one">This creator</button>' +
        '<button data-s="all">All creators</button>' +
      '</div>' +
      '<div class="ft-rail-body"></div>';
    document.body.appendChild(railEl);
    railBody = railEl.querySelector(".ft-rail-body");
    if (railState.pos) {
      railEl.style.right = "auto"; railEl.style.bottom = "auto";
      railEl.style.left = railState.pos.left + "px"; railEl.style.top = railState.pos.top + "px";
    }
    railEl.querySelectorAll(".ft-rail-filters button").forEach((b) => {
      if (b.dataset.f === railFilter) b.classList.add("on");
      b.addEventListener("click", (e) => {
        e.stopPropagation();
        railFilter = b.dataset.f; railState.filter = railFilter; saveRail();
        railEl.querySelectorAll(".ft-rail-filters button").forEach((x) => x.classList.toggle("on", x === b));
        renderRail();
      });
    });
    railEl.querySelectorAll(".ft-rail-scope button").forEach((b) => {
      if ((railState.scope || "one") === b.dataset.s) b.classList.add("on");
      b.addEventListener("click", (e) => {
        e.stopPropagation();
        if ((railState.scope || "one") === b.dataset.s) return;
        railState.scope = b.dataset.s; saveRail();
        railEl.querySelectorAll(".ft-rail-scope button").forEach((x) => x.classList.toggle("on", x === b));
        railData.purchases = []; railData.tip = [];
        loadRail();
      });
    });
    const collapseBtn = railEl.querySelector(".ft-rail-x");
    collapseBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      const c = railEl.classList.toggle("collapsed");
      railState.collapsed = c; saveRail();
      collapseBtn.textContent = c ? "▴" : "▾";
      if (!c) { lastSeenId = Math.max(lastSeenId, topId()); railState.lastSeen = lastSeenId; saveRail(); renderRail(); }
    });
    dragify(railEl, railEl.querySelector(".ft-rail-head"));
    loadRail();
  }

  function topId() {
    return railRows().reduce((m, r) => Math.max(m, Number(r.id) || 0), 0);
  }

  async function loadRail() {
    if (railState.scope === "all") {
      // Fan out across creators (capped) so money on a creator you're NOT
      // looking at still surfaces — the real MoneyRail's whole point. Lazy:
      // only runs while All-creators is active.
      const accts = (F.accounts() || []).slice(0, 12);
      if (railBody && !railData.purchases.length && !railData.tip.length) {
        railBody.innerHTML = '<div class="ft-rail-empty">Loading money across creators…</div>';
      }
      // Only query creators with a captured session — the notifications endpoint
      // 404s for session-less ones, and those 404s would pollute the console.
      // session/status returns {loaded:false} cleanly (no 404) for the rest.
      const live = [];
      await Promise.all(accts.map(async (a) => {
        try {
          const st = await F.get("/admin/session/status", { account_id: a.id }, { noAccount: true });
          if (st && st.loaded) live.push(a);
        } catch (e) { /* skip */ }
      }));
      const buys = [], tips = [];
      await Promise.all(live.map(async (a) => {
        const [b, t] = await Promise.all([fetchMoney("purchases", a.id), fetchMoney("tip", a.id)]);
        if (b) buys.push(...b);
        if (t) tips.push(...t);
      }));
      railData.purchases = buys; railData.tip = tips;
    } else {
      const [buys, tips] = await Promise.all([fetchMoney("purchases"), fetchMoney("tip")]);
      if (buys) railData.purchases = buys;
      if (tips) railData.tip = tips;
      if (buys === null && tips === null && railBody) {
        railBody.innerHTML = '<div class="ft-rail-empty">Couldn\'t load money for this creator — no captured session, or the OF read failed.</div>';
        return;
      }
    }
    if (!railState.collapsed) { lastSeenId = Math.max(lastSeenId, topId()); railState.lastSeen = lastSeenId; saveRail(); }
    renderRail();
  }

  function dragify(box, handle) {
    let sx, sy, ox, oy, on = false;
    handle.addEventListener("mousedown", (e) => {
      if (e.target.closest("button")) return;
      on = true; sx = e.clientX; sy = e.clientY;
      const r = box.getBoundingClientRect(); ox = r.left; oy = r.top;
      box.style.right = "auto"; box.style.bottom = "auto";
      e.preventDefault();
    });
    document.addEventListener("mousemove", (e) => {
      if (!on) return;
      const left = Math.max(4, Math.min(window.innerWidth - box.offsetWidth - 4, ox + e.clientX - sx));
      const top = Math.max(4, Math.min(window.innerHeight - 40, oy + e.clientY - sy));
      box.style.left = left + "px"; box.style.top = top + "px";
    });
    document.addEventListener("mouseup", () => {
      if (!on) return; on = false;
      railState.pos = { left: parseInt(box.style.left, 10), top: parseInt(box.style.top, 10) }; saveRail();
    });
  }

  // ── money toaster (SSE) ──────────────────────────────────────
  let toastWrap;
  function moneyToast(n) {
    css();
    if (!toastWrap) {
      toastWrap = document.createElement("div");
      toastWrap.className = "ft-money-toasts";
      document.body.appendChild(toastWrap);
    }
    const k = kindOf(n), fan = (n.user && n.user.id) || "", aid = F.account();
    const av = (n.user && n.user.avatar)
      ? '<img class="av" src="' + esc(n.user.avatar) + '">' : '<span class="av"></span>';
    const el = document.createElement("a");
    el.className = "ft-mt " + (k === "tip" ? "tip" : "");
    el.href = "messages.html?account=" + encodeURIComponent(aid) + "&fan=" + encodeURIComponent(fan);
    el.innerHTML = av + '<div><div style="font-weight:600">' + esc(nameOf(n)) + '</div>' +
      '<div style="color:#8a8a8a;font-size:11px">' + (k === "tip" ? "sent a tip" : "just bought " + esc(whatOf(n))) + '</div></div>' +
      '<div class="amt">' + esc(amountOf(n)) + '</div>';
    toastWrap.appendChild(el);
    requestAnimationFrame(() => el.classList.add("on"));
    setTimeout(() => { el.classList.remove("on"); setTimeout(() => el.remove(), 300); }, 6000);
  }

  // ── topbar bell → live dropdown ──────────────────────────────
  function bellEl() {
    return document.querySelector('a.iconbtn[href="notifications.html"]');
  }
  async function fetchBell() {
    // "All" notifications = the union we can read cheaply: buys + tips + likes/msgs.
    const [buys, tips] = await Promise.all([fetchMoney("purchases"), fetchMoney("tip")]);
    return (buys || []).concat(tips || [])
      .sort((a, b) => (F.parseUtc(b.createdAt) || 0) - (F.parseUtc(a.createdAt) || 0));
  }
  function mountBell() {
    const bell = bellEl();
    if (!bell || !F.account()) return;
    css();
    let menu = null;
    bell.addEventListener("click", async (e) => {
      e.preventDefault(); e.stopPropagation();
      if (menu) { menu.remove(); menu = null; return; }
      menu = document.createElement("div");
      menu.className = "ft-bell-menu";
      const r = bell.getBoundingClientRect();
      menu.style.top = (r.bottom + 8) + "px";
      menu.style.right = Math.max(8, window.innerWidth - r.right) + "px";
      menu.innerHTML = '<div class="bh">Notifications<a href="notifications.html">See all</a></div>' +
        '<div class="ft-bell-empty">Loading…</div>';
      document.body.appendChild(menu);
      const close = (ev) => { if (menu && !menu.contains(ev.target) && ev.target !== bell) { menu.remove(); menu = null; document.removeEventListener("click", close); } };
      setTimeout(() => document.addEventListener("click", close), 0);
      const rows = await fetchBell();
      if (!menu) return;
      const aid = F.account();
      const body = rows.length ? rows.slice(0, 20).map((n) => {
        const fan = (n.user && n.user.id) || "";
        const av = (n.user && n.user.avatar) ? '<img class="av" src="' + esc(n.user.avatar) + '">' : '<span class="av"></span>';
        const txt = (n.text || "").replace(/<a [^>]*>/g, "").replace(/<\/a>/g, "");
        return '<a class="ft-bell-row ' + (n.isRead ? "" : "unread") + '" href="messages.html?account=' +
          encodeURIComponent(aid) + '&fan=' + encodeURIComponent(fan) + '">' + av +
          '<div class="tx"><b>' + esc(nameOf(n)) + '</b> ' + esc(txt) +
          '<div class="ago">' + esc(F.fmtAgo(n.createdAt)) + '</div></div></a>';
      }).join("") : '<div class="ft-bell-empty">No recent notifications.</div>';
      menu.innerHTML = '<div class="bh">Notifications<a href="notifications.html">See all</a></div>' + body;
    });
  }

  // ── SSE wiring: refresh + toast on money frames ──────────────
  function wireSse() {
    if (!F.account()) return;
    let last = 0;
    F.sse((payload, name) => {
      const moneyish = name === "purchase_notified" || name === "tip" ||
        (name === "toasts") || (name === "new_message" && payload && /pay|tip|purchas/i.test(JSON.stringify(payload).slice(0, 400)));
      if (!moneyish) return;
      // throttle rail reloads
      const now = Date.now();
      if (now - last > 4000) { last = now; if (railEl) loadRail(); }
      // toast if the frame carries an amount + a user
      const n = (payload && (payload.notification || payload.new_message || payload)) || {};
      if (n && (n.user || n.replacePairs)) moneyToast(n);
    }, ["purchase_notified", "tip"]);
  }

  // ── boot ─────────────────────────────────────────────────────
  F.ready(() => {
    mountBell();
    mountRail();
    try { wireSse(); } catch (e) { /* SSE optional */ }
  });

  window.FasttOverlays = { moneyToast, loadRail: () => railEl && loadRail() };
})();
