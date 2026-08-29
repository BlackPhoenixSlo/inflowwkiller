/* fastt.js — shared relay client for the Infloww-skin fastt UI.
 *
 * Served same-origin by the relay (see server.py's /infloww mount), so every
 * call below is a plain relative fetch — no CORS, and the relay's share-token
 * cookie (set once via ?t=...) rides along automatically.
 *
 * Conventions (mirrors web/app.js + app/lib/relay.ts):
 *   • account scope = localStorage "of_account_id" (shared with the legacy /ui)
 *   • X-Account-Id header on every relay call; account_id query param is ALSO
 *     appended to /admin/* GETs because config endpoints declare it as a
 *     required Query(...) — FastAPI ignores it where unused.
 *   • /admin/accounts populates the sidebar creator selector. Unauthed callers
 *     get an empty list (by design) → the selector offers a sign-in modal
 *     (POST /auth/login). Config endpoints themselves work unauthed
 *     (assert_account_owned no-ops without a principal).
 */
(function () {
  "use strict";

  // ── tiny DOM + format helpers ────────────────────────────────
  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));
  /** Add a <style> to <head> exactly once, keyed by id. Every mount below ships
   *  its own CSS, and each was hand-rolling this identical four-line block. */
  const styleOnce = (id, css) => {
    if (document.getElementById(id)) return;
    const el = document.createElement("style");
    el.id = id; el.textContent = css;
    document.head.appendChild(el);
  };

  /** A boolean that persists in localStorage AND shows as a class on <html> —
   *  the shape every house-wide toggle here needs (SFW blur, the icon rail).
   *
   *  The CLASS is the single source of truth. A closure `let on` beside it would
   *  be a second copy of one bit, and every handler that flipped it had to
   *  remember to write all three places; reading the DOM instead means a handler
   *  can never act on a stale value. localStorage is wrapped because it throws
   *  outright in a hard-blocked-cookies browser — there, the flag simply stops
   *  persisting rather than taking the page down with it. */
  const persistedFlag = (key, cls) => {
    const root = document.documentElement;
    let init = false;
    try { init = localStorage.getItem(key) === "1"; } catch (e) {}
    root.classList.toggle(cls, init);
    const flag = {
      get on() { return root.classList.contains(cls); },
      set(v) {
        root.classList.toggle(cls, !!v);
        try { localStorage.setItem(key, v ? "1" : "0"); } catch (e) {}
      },
      toggle() { flag.set(!flag.on); return flag.on; },
    };
    return flag;
  };

  const esc = (s) => String(s ?? "")
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
  const fmtMoney = (n) => "$" + (Number(n) || 0).toFixed(2);
  const fmtCents = (c) => fmtMoney((Number(c) || 0) / 100);
  const fmtInt = (n) => (Number(n) || 0).toLocaleString("en-US");
  /** Parse a server timestamp. The relay stores tz-NAIVE UTC (no trailing Z),
   *  which `new Date()` would read as local time — a run 31 minutes old then
   *  renders as "2h ago" on a UTC+2 machine. Stamp the Z when it's missing. */
  const parseUtc = (s) => {
    if (!s) return null;
    if (s instanceof Date) return s;
    let str = String(s);
    if (/^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}/.test(str) && !/(Z|[+-]\d{2}:?\d{2})$/.test(str)) {
      str = str.replace(" ", "T") + "Z";
    }
    const d = new Date(str);
    return isNaN(d.getTime()) ? null : d;
  };
  const fmtDate = (s) => { const d = parseUtc(s); return d ? d.toLocaleString() : "—"; };
  const fmtAgo = (s) => {
    const dt = parseUtc(s);
    if (!dt) return "—";
    const d = (Date.now() - dt.getTime()) / 1000;
    if (!isFinite(d)) return "—";
    if (d < 0) return "just now";
    if (d < 60) return "just now";
    if (d < 3600) return Math.floor(d / 60) + "m ago";
    if (d < 86400) return Math.floor(d / 3600) + "h ago";
    return Math.floor(d / 86400) + "d ago";
  };
  const debounce = (fn, ms) => {
    let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
  };
  /** Route an OF-hosted image/video through the relay's proxy. OF CDN urls are
   *  IP-signed to the relay and 403 straight from a browser, so this is not a
   *  nicety — it is the only way the asset renders. Lives here because it is a
   *  RELAY url rule (this file already owns api()'s param building) and because
   *  the SFW blur below keys on the `/img?u=` shape it produces; four pages had
   *  their own identical copy. */
  const imgProxy = (url) => "/img?u=" + encodeURIComponent(url);
  /** Retitle a chip/pill while keeping its leading <i> icon. Pages did this by
   *  hand three times over; it is pure DOM with no page state, so it lives here. */
  const chipText = (el, txt) => {
    const i = el.querySelector("i");
    el.textContent = "";
    if (i) el.appendChild(i);
    el.appendChild(document.createTextNode(txt));
  };

  // ── account scope ────────────────────────────────────────────
  const LS_KEY = "of_account_id";
  const state = {
    accountId: localStorage.getItem(LS_KEY) || null,
    accounts: [],            // rows from /admin/accounts
    me: undefined,           // /auth/me result or null
  };

  function account() { return state.accountId; }
  function accountRow() {
    return state.accounts.find((a) => String(a.id) === String(state.accountId)) || null;
  }
  function setAccount(id) {
    state.accountId = id ? String(id) : null;
    if (state.accountId) localStorage.setItem(LS_KEY, state.accountId);
    else localStorage.removeItem(LS_KEY);
    location.reload(); // simplest correct thing: every page re-scopes on load
  }

  // ── fetch wrapper ────────────────────────────────────────────
  class ApiError extends Error {
    constructor(status, body, url) {
      super("relay " + status + " on " + url);
      this.status = status; this.body = body; this.url = url;
    }
  }

  // `acct` overrides the globally-selected account for THIS call only — the
  // multi-creator pages (combined inbox, group chat) render several creators at
  // once, so "the account" is per-request, not per-page. Everything else is
  // unchanged: /admin/* still gets the account_id param, every call still gets
  // the X-Account-Id header. Without this, those pages hand-roll noAccount:true
  // plus their own header, which is this same rule copied into the page.
  // `scope` names the POPULATION a read is about, where `noAccount` only named a
  // mechanism ("don't send the id"). Three values:
  //   "creator" (default) — the globally-selected creator. Unchanged behaviour.
  //   "agency"            — every creator the SIGNED-IN PRINCIPAL owns. Sends
  //                         neither the account_id param nor the X-Account-Id
  //                         header, so the server's clamp_account_filter(None)
  //                         resolves the roster from the session.
  //   an unknown string   — THROWS. See the note below; this is load-bearing.
  //
  // ⚠️ SECURITY: "agency" is a NAME, not a boundary. The real gate is the
  // relay's _account_isolation_middleware, which 401s anonymous /admin/*. Under
  // ALLOW_ANONYMOUS_ADMIN=1 clamp_account_filter returns None — no WHERE clause,
  // every tenant in the DB. Never exercise agency reads under that flag against
  // a multi-tenant database.
  //
  // ⚠️ WHY UNKNOWN SCOPES THROW: a client that predates this option would drop
  // `scope` on the floor (JS ignores unknown keys), inject the selected
  // account_id anyway, and render a per-creator number under an "all creators"
  // label — with no console error and no visual tell. Pages that depend on
  // agency scope must therefore ALSO check `Fastt.supportsScope("agency")` at
  // boot and refuse to render money if it is missing. Failing loud is the whole
  // point; a silent wrong number is worse than a blank card.
  const SCOPES = ["creator", "agency"];
  function supportsScope(name) { return SCOPES.indexOf(name) !== -1; }

  async function api(path, opts = {}) {
    const { method = "GET", body, params, headers = {}, raw = false,
            noAccount = false, acct, scope, priority } = opts;
    if (scope !== undefined && !supportsScope(scope)) {
      throw new Error(
        'Fastt.api: unknown scope "' + scope + '" (expected ' + SCOPES.join(" | ") + ")",
      );
    }
    // "agency" suppresses the param AND the header, exactly as noAccount does.
    const anon = noAccount || scope === "agency";
    const aid = (acct === undefined || acct === null || acct === "")
      ? state.accountId : String(acct);
    const url = new URL(path, location.origin);
    if (params) for (const [k, v] of Object.entries(params)) {
      if (v !== undefined && v !== null && v !== "") url.searchParams.set(k, v);
    }
    if (!anon && aid && url.pathname.startsWith("/admin/")
        && !url.searchParams.has("account_id")) {
      url.searchParams.set("account_id", aid);
    }
    const h = { ...headers };
    if (!anon && aid) h["X-Account-Id"] = aid;
    // ⚠️ PRIORITY IS A DEMOTION, NOT A BOOST. The relay runs a per-account OF
    // lane (service/server.py:1362): `total` = 5 concurrent calls, of which a
    // `background` caller must ALSO hold one of 2 sub-cap slots. So user work
    // always keeps >= 3 reserved slots and jumps the queue ahead of anything
    // tagged background. The server default for a missing/any-other header is
    // "user" (server.py:_current_priority), so ONLY the demotion travels — and
    // tagging a call the operator is waiting on is the one way to make this
    // header hurt. Reserve it for bulk enrichment and decoration.
    if (priority === "background") h["X-Priority"] = "background";
    let payload;
    if (body !== undefined) {
      h["Content-Type"] = "application/json";
      payload = JSON.stringify(body);
    }
    const resp = await fetch(url.pathname + url.search, { method, headers: h, body: payload });
    if (!resp.ok) {
      let b = null;
      try { b = await resp.json(); } catch { try { b = await resp.text(); } catch {} }
      throw new ApiError(resp.status, b, url.pathname);
    }
    if (raw) return resp;
    if (resp.status === 204) return null;
    const ct = resp.headers.get("content-type") || "";
    return ct.includes("json") ? resp.json() : resp.text();
  }
  const get = (p, params, o) => api(p, { ...o, params });
  const post = (p, body, o) => api(p, { ...o, method: "POST", body });
  const put = (p, body, o) => api(p, { ...o, method: "PUT", body });
  const patch = (p, body, o) => api(p, { ...o, method: "PATCH", body });
  const del = (p, o) => api(p, { ...o, method: "DELETE" });

  // ── automation_rules helpers (many pages are one rule by kind) ─
  async function rulesByKind() {
    const out = await get("/admin/automation-rules");
    const map = {};
    for (const r of out.rules || []) {
      if (String(r.account_id) !== String(state.accountId)) continue;
      (map[r.kind] = map[r.kind] || []).push(r);
    }
    return map;
  }
  async function rule(kind) {
    const map = await rulesByKind();
    return (map[kind] || [])[0] || null;
  }
  /** Flip / create the singleton rule for a kind. patchBody applies to an
   *  existing row; createBody seeds a new one (payload/trigger defaults). */
  async function upsertRule(kind, patchBody, createBody) {
    const existing = await rule(kind);
    if (existing) return patch("/admin/automation-rules/" + existing.id, patchBody);
    return post("/admin/automation-rules", {
      account_id: state.accountId, kind, ...createBody, ...patchBody,
    });
  }
  /** Last-run stats off a rule row, whichever shape the serializer used: an
   *  already-parsed `stats` object, a `stats_json` object, or a `stats_json`
   *  string. null when there is nothing to show (including unparseable JSON —
   *  a run summary is never worth throwing over). The rule row shape is this
   *  file's contract, so the reader belongs here and not in each page. */
  const ruleStats = (r) => {
    if (r && r.stats && typeof r.stats === "object") return r.stats;
    if (!r || !r.stats_json) return null;
    if (typeof r.stats_json === "object") return r.stats_json;
    try { return JSON.parse(r.stats_json); } catch (e) { return null; }
  };

  // ── injected chrome: toasts, modal, badges ───────────────────
  const CSS = `
  .ft-toast-wrap{position:fixed;right:18px;bottom:18px;z-index:9999;display:flex;flex-direction:column;gap:8px}
  .ft-toast{background:#232323;border:1px solid #333;border-left:3px solid #4166f6;color:#fff;
    font:13px/1.45 Inter,sans-serif;border-radius:8px;padding:10px 14px;min-width:220px;max-width:360px;
    box-shadow:0 6px 24px rgba(0,0,0,.45);opacity:0;transform:translateY(6px);transition:all .18s}
  .ft-toast.on{opacity:1;transform:none}
  .ft-toast.ok{border-left-color:#67d1ae}.ft-toast.err{border-left-color:#e05b5b}
  .ft-modal-back{position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:9998;display:flex;
    align-items:center;justify-content:center}
  .ft-modal{background:#262626;border:1px solid #333;border-radius:12px;padding:22px;width:340px;
    font:13px Inter,sans-serif;color:#fff}
  .ft-modal h3{margin:0 0 14px;font-size:15px}
  .ft-modal input{width:100%;box-sizing:border-box;background:#1c1c1c;border:1px solid #333;color:#fff;
    border-radius:8px;padding:9px 11px;margin-bottom:10px;font:13px Inter,sans-serif}
  .ft-modal button{width:100%;background:#4166f6;border:0;color:#fff;border-radius:8px;padding:10px;
    font:600 13px Inter,sans-serif;cursor:pointer}
  .ft-modal .ft-err{color:#e05b5b;font-size:12px;margin:2px 0 8px;display:none}
  /* The topbar's pills — Search, Normal view, SFW — are one shape. Only their
     metrics and type differ, so each id below carries just its own deltas. */
  .ft-pill{display:inline-flex;align-items:center;border:1px solid #333;border-radius:99px;
    background:#1c1c1c;color:#9a9a9a;cursor:pointer}
  .ft-pill:hover{border-color:#4166f6;color:#fff}
  .ft-live{display:inline-flex;align-items:center;gap:5px;font:600 10px Inter,sans-serif;color:#67d1ae;
    background:rgba(103,209,174,.12);border:1px solid rgba(103,209,174,.35);border-radius:99px;padding:2px 8px}
  .ft-static{display:inline-flex;align-items:center;gap:5px;font:600 10px Inter,sans-serif;color:#8a8a8a;
    background:rgba(138,138,138,.1);border:1px solid #333;border-radius:99px;padding:2px 8px}
  .ft-acct-menu{position:fixed;z-index:9997;background:#1c1c1c;border:1px solid #333;border-radius:10px;
    padding:6px;min-width:210px;box-shadow:0 10px 30px rgba(0,0,0,.5);font:13px Inter,sans-serif}
  .ft-acct-menu .row{display:flex;align-items:center;gap:8px;padding:8px 10px;border-radius:7px;
    color:#fff;cursor:pointer}
  .ft-acct-menu .row:hover{background:#262626}
  .ft-acct-menu .dot{width:8px;height:8px;border-radius:50%;background:#4166f6;flex:none}
  .ft-acct-menu .row.on{background:#232323;outline:1px solid #4166f6}
  .ft-acct-menu .signin{color:#8a8a8a;border-top:1px solid #333;margin-top:4px;padding-top:9px}`;

  function injectCss() { styleOnce("ft-css", CSS); }

  let toastWrap;
  function toast(msg, kind) {
    injectCss();
    if (!toastWrap) {
      toastWrap = document.createElement("div");
      toastWrap.className = "ft-toast-wrap";
      document.body.appendChild(toastWrap);
    }
    const t = document.createElement("div");
    t.className = "ft-toast " + (kind || "");
    t.textContent = msg;
    toastWrap.appendChild(t);
    requestAnimationFrame(() => t.classList.add("on"));
    setTimeout(() => { t.classList.remove("on"); setTimeout(() => t.remove(), 250); }, 3400);
  }
  const saved = (msg) => toast(msg || "Saved ✓", "ok");
  const oops = (e) => {
    const detail = e && e.body && (e.body.detail || e.body.error);
    toast((detail ? String(detail).slice(0, 140) : (e && e.message) || "Request failed"), "err");
    console.error(e);
  };

  /** Mark a widget as live-wired or an intentionally-static mock. */
  function liveBadge(el) { injectCss(); _badge(el, "ft-live", "LIVE"); }
  function staticBadge(el, note) { injectCss(); _badge(el, "ft-static", note || "STATIC DEMO"); }
  function _badge(el, cls, label) {
    if (!el || el.querySelector(":scope > .ft-live, :scope > .ft-static")) return;
    const b = document.createElement("span");
    b.className = cls; b.textContent = label;
    el.appendChild(b);
  }

  /** Badge that REPLACES whatever badge the element already wears.
   *
   *  `liveBadge`/`staticBadge` above deliberately no-op when any badge is
   *  already present, which makes a card's badge permanently wrong in both
   *  directions once state changes: succeed-then-fail leaves a green LIVE over
   *  an "unavailable" card, and fail-then-succeed leaves a grey failure note
   *  over live data.
   *
   *  This is a SEPARATE function rather than a fix to `_badge` on purpose.
   *  Eight pages call both badge kinds on the same element from mutually
   *  exclusive branches; they would all be improved by replace semantics, but
   *  they are not covered by tests and `outreach-broadcast.js` badges once at
   *  boot and again at the end of a load. Changing the shared helper would put
   *  55 untested pages in the blast radius of one page's bug. Opt in per page;
   *  migrating the rest is its own change.
   */
  function setBadge(el, kind, label) {
    if (!el) return;
    injectCss();
    const old = el.querySelector(":scope > .ft-live, :scope > .ft-static");
    if (old) old.remove();
    _badge(el, kind === "live" ? "ft-live" : "ft-static",
           label || (kind === "live" ? "LIVE" : "STATIC DEMO"));
  }

  // ── sign-in modal (friend auth) ──────────────────────────────
  function signInModal() {
    return new Promise((resolve) => {
      injectCss();
      const back = document.createElement("div");
      back.className = "ft-modal-back";
      back.innerHTML = `
        <div class="ft-modal">
          <h3>Sign in to fastt</h3>
          <input type="text" placeholder="Username" id="ft-user" autocomplete="username">
          <input type="password" placeholder="Password" id="ft-pass" autocomplete="current-password">
          <div class="ft-err" id="ft-login-err"></div>
          <button id="ft-login-go">Sign in</button>
        </div>`;
      document.body.appendChild(back);
      back.addEventListener("click", (e) => { if (e.target === back) { back.remove(); resolve(false); } });
      const go = async () => {
        const errEl = $("#ft-login-err", back);
        try {
          await post("/auth/login", {
            username: $("#ft-user", back).value.trim(),
            password: $("#ft-pass", back).value,
          }, { noAccount: true });
          back.remove(); resolve(true);
        } catch (e) {
          errEl.style.display = "block";
          errEl.textContent = (e.body && e.body.detail) || "Sign-in failed";
        }
      };
      $("#ft-login-go", back).addEventListener("click", go);
      back.addEventListener("keydown", (e) => { if (e.key === "Enter") go(); });
      $("#ft-user", back).focus();
    });
  }

  // ── creator selector (sidebar ".creator" block) ──────────────
  async function loadAccounts() {
    try {
      const out = await get("/admin/accounts", null, { noAccount: true });
      state.accounts = out.accounts || out.rows || (Array.isArray(out) ? out : []);
    } catch (e) { state.accounts = []; }
    // /admin/accounts is PER-PRINCIPAL and returns [] to an unauthed caller by
    // design. Without a fallback every creator renders as a bare numeric id and
    // the switcher looks empty. per-model is unauthed-readable and carries the
    // display names, so use it to name the roster when the owned list is empty.
    if (!state.accounts.length) {
      try {
        const end = new Date(), start = new Date(Date.now() - 30 * 864e5);
        const iso = (d) => d.toISOString().slice(0, 10);
        const pm = await get("/admin/stats/per-model",
          { start: iso(start), end: iso(end) }, { noAccount: true });
        state.accounts = (pm.per_model || [])
          .map((r) => ({ id: String(r.account_id), nickname: r.display_name || String(r.account_id) }))
          .filter((r) => !/^Account \d+$/.test(r.nickname) || String(r.id) === String(state.accountId));
        state.accountsAreDerived = true;
      } catch (e) { /* leave empty; callers degrade to the id */ }
    }
    // sane default: first account if none picked or picked one vanished
    if (state.accounts.length && !state.accounts.some((a) => String(a.id) === String(state.accountId))) {
      state.accountId = String(state.accounts[0].id);
      localStorage.setItem(LS_KEY, state.accountId);
    }
    return state.accounts;
  }

  function mountAccountPicker() {
    const holder = $(".creator");
    if (!holder) return;
    const nameEl = $(".creator .name");
    const row = accountRow();
    if (nameEl && row) nameEl.textContent = row.nickname || row.id;
    else if (nameEl && state.accountId) nameEl.textContent = state.accountId;
    holder.style.cursor = "pointer";
    holder.addEventListener("click", async (e) => {
      e.stopPropagation();
      $$(".ft-acct-menu").forEach((m) => m.remove());
      injectCss();
      const menu = document.createElement("div");
      menu.className = "ft-acct-menu";
      const r = holder.getBoundingClientRect();
      menu.style.left = r.left + "px"; menu.style.top = (r.bottom + 6) + "px";
      const rows = state.accounts.map((a) => `
        <div class="row ${String(a.id) === String(state.accountId) ? "on" : ""}" data-id="${esc(a.id)}">
          <span class="dot" style="${a.color ? "background:" + esc(a.color) : ""}"></span>
          <span>${esc(a.nickname || a.id)}</span>
        </div>`).join("");
      menu.innerHTML = rows +
        `<div class="row signin" data-act="signin">${state.accounts.length ? "Switch user…" : "Sign in to list accounts…"}</div>`;
      document.body.appendChild(menu);
      const close = () => { menu.remove(); document.removeEventListener("click", close); };
      setTimeout(() => document.addEventListener("click", close), 0);
      menu.addEventListener("click", async (ev) => {
        const rowEl = ev.target.closest(".row");
        if (!rowEl) return;
        if (rowEl.dataset.act === "signin") {
          close();
          if (await signInModal()) { await loadAccounts(); location.reload(); }
          return;
        }
        close(); setAccount(rowEl.dataset.id);
      });
    });
  }

  // ── topbar creator switcher (#modelswap, on 52 pages) ────────
  // The mockup ships hardcoded creators and a cosmetic ms-js handler that
  // only repaints the label. Repopulate it from the real account list and
  // take the click in the CAPTURE phase so the scope actually changes.
  const AV_COLORS = ["#e0679b", "#5b8def", "#67d1ae", "#a78bfa", "#e5a35b", "#e07a5f"];
  function mountModelSwap() {
    const sw = document.getElementById("modelswap");
    if (!sw) return;
    const menu = sw.querySelector(".ms-menu");
    const head = menu && menu.querySelector(".ms-head");
    if (!menu) return;

    if (!state.accounts.length) {
      // Unauthed: /admin/accounts is empty by design. Don't leave fake
      // creators sitting there looking switchable — offer sign-in instead.
      menu.querySelectorAll(".ms-item").forEach((n) => n.remove());
      if (head) head.textContent = "Sign in to switch creator";
      const item = document.createElement("a");
      item.className = "ms-item"; item.dataset.ftSignin = "1";
      item.innerHTML = '<span class="ms-dot" style="background:#4166f6">→</span>'
                     + '<span class="ms-nm">Sign in…</span><span class="ms-on"></span>';
      menu.appendChild(item);
      return;
    }

    if (head) head.textContent = "Switch creator";
    menu.querySelectorAll(".ms-item").forEach((n) => n.remove());
    state.accounts.forEach((a, i) => {
      const name = a.nickname || String(a.id);
      const color = a.color || AV_COLORS[i % AV_COLORS.length];
      const on = String(a.id) === String(state.accountId);
      const item = document.createElement("a");
      item.className = "ms-item" + (on ? " active" : "");
      item.dataset.acctId = String(a.id);
      item.innerHTML = `<span class="ms-dot" style="background:${esc(color)}">${esc(name[0].toUpperCase())}</span>`
                     + `<span class="ms-nm">${esc(name)}</span><span class="ms-on"></span>`;
      menu.appendChild(item);
    });

    const row = accountRow();
    if (row) {
      const nm = sw.querySelector(".ms-name"), av = sw.querySelector(".ms-av");
      const name = row.nickname || String(row.id);
      const idx = state.accounts.findIndex((a) => String(a.id) === String(row.id));
      const color = row.color || AV_COLORS[(idx < 0 ? 0 : idx) % AV_COLORS.length];
      if (nm) nm.textContent = name;
      if (av) { av.textContent = name[0].toUpperCase(); av.style.background = color; }
    }

    document.addEventListener("click", async (e) => {
      const item = e.target.closest && e.target.closest(".ms-item");
      if (!item || !sw.contains(item)) return;
      if (item.dataset.ftSignin) {
        e.stopPropagation(); sw.classList.remove("open");
        if (await signInModal()) location.reload();
        return;
      }
      if (!item.dataset.acctId) return;
      e.stopPropagation();
      setAccount(item.dataset.acctId);
    }, true); // capture: run before the mockup's cosmetic ms-js handler
  }

  // ── no-account honesty banner ────────────────────────────────
  // With no creator selected the pages still show their baked mockup
  // numbers. Say so, loudly, rather than letting them read as real.
  function noAccountBanner() {
    if ($("#ft-noacct")) return;
    const bar = document.createElement("div");
    bar.id = "ft-noacct";
    bar.style.cssText = "position:fixed;left:0;right:0;bottom:0;z-index:9996;background:#3a2a12;"
      + "border-top:1px solid #7a5a20;color:#f0c98a;font:600 12px Inter,sans-serif;"
      + "padding:9px 16px;text-align:center;cursor:pointer";
    bar.textContent = "No creator selected — every number on this page is placeholder demo data. "
      + "Click to sign in and load real data.";
    bar.addEventListener("click", async () => { if (await signInModal()) location.reload(); });
    document.body.appendChild(bar);
  }
  const hasAccount = () => !!state.accountId;

  // ── SSE (relay /events channel) ──────────────────────────────
  // The relay sets the SSE `event:` line to the OF event type on EVERY frame
  // (service/events.py sse_stream) — there are no unnamed frames, so a bare
  // `es.onmessage` is deaf. EventSource has no wildcard, so we register a
  // named listener per type. This list is the observed set in event_inbox;
  // callers can pass extra names.
  const SSE_TYPES = [
    "message", "ping", "connected", "toasts", "purchase_notified",
    "chat_messages", "api2_chat_message", "new_message", "messages",
    "chat_message_delete", "chat_message_like", "chat_queue_update",
    "chat_queue_finish", "typing", "stories", "subscribed",
    "post_published", "post_updated", "post_expire", "post_fundraising_updated",
    "syncInProcess", "newTagsCount",
  ];
  /** Fastt.sse(handler, extraTypes?) — handler(payload, eventName). */
  function sse(onEvent, extraTypes) {
    const es = new EventSource("/events");
    const types = SSE_TYPES.concat(extraTypes || []);
    for (const name of types) {
      es.addEventListener(name, (ev) => {
        let data = ev.data;
        try { data = JSON.parse(ev.data); } catch { /* ping sends a bare ts */ }
        try { onEvent(data, name); } catch (err) { console.error(err); }
      });
    }
    return es;
  }

  // ── boot ─────────────────────────────────────────────────────
  const readyFns = [];
  let booted = false;
  /** Pages call Fastt.ready(async () => {...}) — runs after the account
   *  picker is populated so Fastt.account() is always usable inside. If boot
   *  has already finished (e.g. a late-loaded _shared script like overlays.js
   *  registers after the fact), the fn runs immediately instead of never. */
  function ready(fn) {
    if (booted) { (async () => { try { await fn(); } catch (e) { oops(e); } })(); return; }
    readyFns.push(fn);
  }

  /** Load the always-on shared chrome (MoneyRail/toasts/bell overlays, and the
   *  ⌘K command palette) once, from every page, without a per-page include. */
  function loadOverlays() {
    const base = (document.currentScript && document.currentScript.src)
      ? document.currentScript.src.replace(/fastt\.js.*$/, "")
      : "_shared/";
    [["ft-overlays-js", "overlays.js"], ["ft-palette-js", "palette.js"]].forEach(([id, file]) => {
      if (document.getElementById(id)) return;
      const s = document.createElement("script");
      s.id = id; s.src = base + file; s.async = true;
      document.head.appendChild(s);
    });
  }

  /** Wire the "Messages Pro" sidebar unread badge on every page that carries
   *  the canonical sidebar (43 of them baked a mock "36"). One home, here. */
  /** Inject a "Search ⌘K" pill into the topbar so the command palette is
   *  discoverable (palette.js binds ⌘K globally; this is the visible handle). */
  function mountSearchPill() {
    if (document.getElementById("ft-search-pill")) return;
    const anchor = document.querySelector('.topbar a[href="referrals.html"]')
      || document.querySelector('.topbar .pill');
    if (!anchor || !anchor.parentElement) return;
    styleOnce("ft-search-css", `#ft-search-pill{gap:8px;height:36px;padding:0 12px;
        font:13px Inter,sans-serif;margin-right:2px}
        #ft-search-pill kbd{background:#2a2a2a;border:1px solid #3a3a3a;border-radius:5px;padding:1px 6px;
        font:600 11px Inter;color:#bbb}`);
    const btn = document.createElement("button");
    btn.id = "ft-search-pill"; btn.type = "button"; btn.className = "ft-pill";
    const isMac = /Mac|iPhone|iPad/.test(navigator.platform);
    btn.innerHTML = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
      + 'stroke-width="1.8"><circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3" stroke-linecap="round"/></svg>'
      + '<span>Search</span><kbd>' + (isMac ? "⌘" : "Ctrl") + "K</kbd>";
    btn.addEventListener("click", () => { if (window.FasttPalette) window.FasttPalette.open(); });
    anchor.parentElement.insertBefore(btn, anchor);
  }

  /** Wire the topbar's panel icon into a REAL sidebar collapse — the skin
   *  shipped it as decoration (nothing anywhere listened to it).
   *
   *  Collapsed, the sidebar is an icon rail: labels, chevrons, badges, the
   *  pop-out arrows, the version stamp and every open submenu go away and the
   *  252px column becomes 68px. The state is per-BROWSER, not per-page — each
   *  page here is its own document, so localStorage is the only thing that can
   *  carry a collapsed rail across a navigation.
   *
   *  Clicking an icon that owns a submenu re-opens the rail first: a submenu is
   *  a list of words, so it is unusable at icon width. That runs on the CAPTURE
   *  phase, ahead of the page's own canonical-sidebar handler, so by the time
   *  the submenu is toggled the labels are already back. Plain links just
   *  navigate and the rail stays collapsed on the next page. */
  function mountRailToggle() {
    const bar = document.querySelector(".topbar");
    const btn = bar && bar.querySelector(".panel-btn");
    if (!btn || btn.__ftRail) return;
    btn.__ftRail = 1;

    styleOnce("ft-siderail-css", `
        .sidebar{transition:width .14s ease,flex-basis .14s ease}
        .panel-btn{cursor:pointer;border-radius:9px}
        .panel-btn:hover{background:#1c1c1c;color:#fff}
        html.ft-siderail .sidebar{width:68px;flex:0 0 68px;padding-left:10px;padding-right:10px;overflow:hidden}
        html.ft-siderail .sidebar .nav-sec,
        html.ft-siderail .sidebar .lbl,
        html.ft-siderail .sidebar .cv,
        html.ft-siderail .sidebar .chev,
        html.ft-siderail .sidebar .badge,
        html.ft-siderail .sidebar .popout,
        html.ft-siderail .sidebar .version,
        html.ft-siderail .sidebar .subnav,
        html.ft-siderail .creator .name,
        html.ft-siderail .creator .pink{display:none}
        html.ft-siderail .nav-item{justify-content:center;padding-left:0;padding-right:0}
        html.ft-siderail .creator{justify-content:center;padding-left:0;padding-right:0;gap:0}`);

    const rail = persistedFlag("fastt_rail", "ft-siderail");

    // At icon width the label is gone, so hang it on the tooltip instead.
    document.querySelectorAll(".sidebar .nav-item").forEach((it) => {
      const lbl = it.querySelector(".lbl");
      if (lbl && !it.getAttribute("title")) it.setAttribute("title", lbl.textContent.trim());
    });

    btn.setAttribute("title", "Collapse the sidebar to icons");
    btn.addEventListener("click", () => rail.toggle());

    // Capture phase — must beat the page's own has-sub handler.
    document.addEventListener("click", (e) => {
      if (!rail.on) return;
      if (e.target.closest && e.target.closest(".sidebar .nav-item.has-sub")) rail.set(false);
    }, true);
  }

  /** Inject a "Normal view" switch into the topbar's LEFT edge — the twin of
   *  the ⧉ button in the Next app's TopNav, so the two front-ends are a round
   *  trip rather than a one-way door.
   *
   *  Both skins are the SAME relay and the same data; this one is the easy
   *  way around, /inbox is the worked-in one. The link is a real navigation
   *  (full page load) because the two UIs are separate documents — there is
   *  no shared router to hand off to.
   *
   *  Anchored after `.panel-btn` (the sidebar icon) so it reads as chrome and
   *  not as page content. Messages and Group — the two surfaces someone is most
   *  likely to be ON when they want the other view — ship a `.topstrip` with no
   *  panel button instead of the canonical `.topbar`, so there it falls in at
   *  the head of `.top-right`, the same landing spot their SFW pill uses. Miss
   *  that and the round trip has a hole exactly where it matters most. */
  function mountViewSwitch() {
    if (document.getElementById("ft-view-switch")) return;
    const bar = document.querySelector(".topbar");
    const panel = bar && bar.querySelector(".panel-btn");
    const right = document.querySelector(".top-right");
    if (!bar && !right) return;
    styleOnce("ft-view-switch-css", `#ft-view-switch{gap:7px;height:32px;padding:0 11px;
        font:600 12px Inter,sans-serif;letter-spacing:.2px;text-decoration:none;
        margin-left:6px;white-space:nowrap;flex:none}
        #ft-view-switch svg{flex:none}`);
    const a = document.createElement("a");
    a.id = "ft-view-switch"; a.className = "ft-pill";
    a.href = "/inbox";
    a.title = "Switch to the normal view (/inbox) — same data, same relay";
    a.innerHTML = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
      + 'stroke-width="1.9"><path d="M9 7L4 12l5 5" stroke-linecap="round" stroke-linejoin="round"/>'
      + '<path d="M4 12h10a6 6 0 0 1 6 6v1" stroke-linecap="round"/></svg><span>Normal view</span>';
    if (panel && panel.parentElement === bar) bar.insertBefore(a, panel.nextSibling);
    else if (bar) bar.insertBefore(a, bar.firstChild);
    else right.insertBefore(a, right.firstChild);
  }

  /** SFW blur — a real, house-wide privacy toggle (top-right on every page).
   *  OnlyFans content AND fan avatars all load through the relay's /img?u=
   *  proxy, so one CSS rule keyed on <html>.ft-sfw blurs exactly the fan
   *  imagery everywhere — hover any tile/thumb to peek. Persisted in
   *  localStorage so it survives navigation and pop-outs. The skin shipped a
   *  dead switch on Messages; this wires it and clones it onto every page. */
  function mountSfwToggle() {
    styleOnce("ft-sfw-css", `
        html.ft-sfw img[src*="/img?u="],
        html.ft-sfw .mgrid img,
        html.ft-sfw .att .a img,
        html.ft-sfw video{filter:blur(16px);transition:filter .12s}
        html.ft-sfw img[src*="/img?u="]:hover,
        html.ft-sfw .mgrid .mt:hover img,
        html.ft-sfw .att .a:hover img,
        html.ft-sfw video:hover{filter:blur(0)}
        html.ft-sfw .sfw .switch,html.ft-sfw .sfw .toggle{background:#4166f6}
        html.ft-sfw .sfw .switch:before,html.ft-sfw .sfw .toggle:after{left:14px}
        .sfw{cursor:pointer}
        #ft-sfw-pill{gap:7px;height:36px;padding:0 11px;
          font:600 12px Inter,sans-serif;letter-spacing:.3px;margin-right:2px}
        #ft-sfw-pill .dot{width:26px;height:14px;border-radius:8px;background:#3a3a3a;position:relative;
          flex:none;transition:background .12s}
        #ft-sfw-pill .dot:before{content:"";position:absolute;top:2px;left:2px;width:10px;height:10px;
          border-radius:50%;background:#fff;transition:left .12s}
        html.ft-sfw #ft-sfw-pill{color:#fff;border-color:#4166f6}
        html.ft-sfw #ft-sfw-pill .dot{background:#4166f6}
        html.ft-sfw #ft-sfw-pill .dot:before{left:14px}`);
    const sfw = persistedFlag("fastt_sfw", "ft-sfw");
    const TITLE = "SFW blur — hide fan imagery for screen-sharing (persists across pages, hover to peek)";
    function toggle() {
      toast(sfw.toggle() ? "SFW blur ON — fan imagery hidden (hover to peek)" : "SFW blur off");
    }
    // 1) Wire the page's OWN SFW control in place — every canonical page and
    //    Messages ship one (varying markup: .switch or .toggle). No duplicate.
    const existing = document.querySelector(".sfw");
    if (existing) {
      if (!existing.__ftSfw) {
        existing.__ftSfw = 1;
        existing.style.cursor = "pointer";
        existing.setAttribute("title", TITLE);
        existing.addEventListener("click", toggle);
      }
      return;
    }
    // 2) No native control (e.g. group.html) — drop a pill into the topbar.
    if (document.getElementById("ft-sfw-pill")) return;
    const anchor = document.getElementById("ft-search-pill")
      || document.querySelector('.topbar a[href="referrals.html"]')
      || document.querySelector(".topbar .pill")
      || document.querySelector(".top-right");
    if (!anchor) return;
    const btn = document.createElement("button");
    btn.id = "ft-sfw-pill"; btn.type = "button"; btn.className = "ft-pill";
    btn.setAttribute("title", TITLE);
    btn.innerHTML = '<span>SFW</span><span class="dot"></span>';
    btn.addEventListener("click", toggle);
    if (anchor.classList && anchor.classList.contains("top-right")) anchor.insertBefore(btn, anchor.firstChild);
    else anchor.parentElement.insertBefore(btn, anchor);
  }

  async function mountMsgBadge() {
    const badge = document.querySelector('a[href="messages.html"] .badge');
    if (!badge || !state.accountId) return;
    try {
      const out = await get("/admin/chats/recent", { limit: 100 });
      const rows = (out.list || []).filter((r) => String(r.__accountId) === String(state.accountId));
      const n = rows.filter((r) => r.hasUnread || (r.unreadMessagesCount || 0) > 0).length;
      if (n > 0) { badge.textContent = n > 99 ? "99+" : String(n); badge.style.display = ""; }
      else badge.style.display = "none";
    } catch (e) { /* leave the baked value rather than error */ }
  }

  /** Help center — the skin ships this sidebar link DEAD (`href="#"`, all 52
   *  pages that carry it), so wire it to the help that exists.
   *
   *  NOT a port of the "?" dock. The bot is closed-book over a 1,900-line
   *  manual whose 113 click paths are written in the NORMAL view's nav words —
   *  42 say "Automations →", 10 "Setup →", 8 "Stuff →", none of which
   *  exist here; the 27 under "Settings"/"Growth" resolve to pages with
   *  different tabs, which is the worse failure because it looks right. A bot
   *  mounted here would answer in coordinates this view does not have. So the
   *  honest wiring is a HANDOFF, the same one `mountViewSwitch` already offers.
   *
   *  The click also sets the dock's own persisted open bit (one origin serves
   *  both views) so the panel is open on arrival — landing on /inbox next to a
   *  collapsed 9px bubble is a link that technically worked and practically
   *  did not. Key mirrors `OPEN_KEY` in app/components/assistant/AssistantWidget.tsx. */
  const ASSISTANT_OPEN_KEY = "chatterly:assistant_open";
  function mountHelpLink() {
    for (const a of $$(".sbfoot a.nav-item")) {
      const lbl = $(".lbl", a);
      if (!lbl || lbl.textContent.trim() !== "Help center") continue;
      // Only ever adopt the dead one — a real href here is someone's later work.
      if (a.getAttribute("href") !== "#") continue;
      a.setAttribute("href", "/inbox");
      a.title = "Ask the help bot — opens the ? panel in the normal view";
      a.addEventListener("click", () => {
        try { localStorage.setItem(ASSISTANT_OPEN_KEY, "1"); } catch (e) { /* private mode */ }
      });
    }
  }

  async function boot() {
    injectCss();
    await loadAccounts();
    mountAccountPicker();
    mountModelSwap();
    mountMsgBadge();
    mountSearchPill();
    mountViewSwitch();
    mountRailToggle();
    mountSfwToggle();
    mountHelpLink();
    if (!state.accountId) noAccountBanner();
    for (const fn of readyFns) {
      try { await fn(); } catch (e) { oops(e); }
    }
    booted = true;
    loadOverlays();
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => { boot(); });
  } else { boot(); }

  // ── exports ──────────────────────────────────────────────────
  window.Fastt = {
    $, $$, esc, fmtMoney, fmtCents, fmtInt, fmtDate, fmtAgo, parseUtc, debounce,
    chipText, imgProxy,
    api, get, post, put, patch, del, ApiError,
    account, accountRow, setAccount, accounts: () => state.accounts, hasAccount,
    rule, rulesByKind, upsertRule, ruleStats,
    toast, saved, oops, liveBadge, staticBadge, setBadge, supportsScope,
    signInModal, sse, ready,
  };
})();
