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
  // Wrapped for the same reason `persistedFlag` wraps its own access: a browser
  // with site data hard-blocked THROWS on `localStorage`, and this read runs at
  // IIFE load — so an unwrapped one took the whole file down with it and no
  // page mounted at all. There, account scope simply stops persisting.
  const lsGet = (k) => { try { return localStorage.getItem(k); } catch (e) { return null; } };
  const lsSet = (k, v) => { try { localStorage.setItem(k, v); } catch (e) {} };
  const lsDel = (k) => { try { localStorage.removeItem(k); } catch (e) {} };
  const state = {
    accountId: lsGet(LS_KEY) || null,
    accounts: [],            // rows from /admin/accounts
    me: undefined,           // /auth/me result or null
  };

  function account() { return state.accountId; }
  function accountRow() {
    return state.accounts.find((a) => String(a.id) === String(state.accountId)) || null;
  }
  function setAccount(id) {
    state.accountId = id ? String(id) : null;
    if (state.accountId) lsSet(LS_KEY, state.accountId);
    else lsDel(LS_KEY);
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
      lsSet(LS_KEY, state.accountId);
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
    // The bar is fixed to the bottom at z-index 9996, so it sits ON TOP of
    // whatever the page ends with. On a desktop that is white space; at 390px
    // it was covering the composer's Send button and the last row of every
    // list. The flag lets the phone stylesheet reserve the height back.
    document.documentElement.classList.add("ft-has-noacct");
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
  /** PHONE LAYOUT — the whole responsive layer for the 52 canonical pages.
   *
   *  The skin is a 1440x900 desktop mock: `.app` is a fixed 1440px box, the
   *  sidebar is a 252px column that is always open, and 53 of the 56 pages ship
   *  `<meta viewport content="width=1440">`. On a phone that meta plus
   *  initial-scale=1 gives you the TOP-LEFT CORNER of a 1440px canvas at 1:1 —
   *  the sidebar and about 150px of content, and you pan sideways for the rest.
   *  That is the "sticks out of view" this fixes.
   *
   *  It lives here, not in the pages, for the reason BUILD_GUIDE gives: every
   *  page is dashboard.html with a different `<main>`, the `<aside class=
   *  "sidebar">` must stay byte-identical, and there is no shared stylesheet.
   *  One `styleOnce` here reaches all 52 without editing a single `<aside>`.
   *
   *  The sidebar becomes an off-canvas drawer on the SAME `.panel-btn` that
   *  collapses it to a rail on desktop — one control, the meaning of which
   *  follows the width. `ft-siderail` is force-removed under the breakpoint
   *  (without touching its localStorage value, so the desktop rail survives a
   *  rotation): a 68px icon rail inside a drawer is two ideas fighting. */
  function mountPhoneLayout() {
    const root = document.documentElement;
    if (root.__ftPhone) return;
    root.__ftPhone = 1;

    // 760px, not 768: `.main`'s content is authored against a ~1040px column,
    // so the drawer has to take over well before a tablet stops fitting it.
    const mq = window.matchMedia("(max-width:760px)");

    styleOnce("ft-phone-css", `
      /* OUTSIDE the breakpoint, deliberately. The scrim is appended to <body>
         on every page at every width, so its display is the only thing keeping
         it out of the desktop layout — and every other rule in this file is
         inside the media block below. Unstyled it is a 4x4 UA <button> and, in
         a <body> that is 'display:flex;justify-content:center', a flex sibling
         of '.app' — which shrank the shell by 4px on 54 of the 56 pages at
         1440. Hidden by default; the breakpoint turns it on. */
      .ft-scrim{display:none}
      @media (max-width:760px){
        .ft-scrim{display:block}
        body{display:block;min-height:0}
        .app{width:100%;max-width:100%;height:100dvh}
        /* The topbar keeps its 52px and scrolls; wrapping it would eat the
           shell's height on the one axis a phone cannot spare. */
        /* ── the topbar ──────────────────────────────────────────────────
           At 390 this bar holds 1430px of content. Letting it scroll was not a
           fix: a row that runs past the edge reads as broken whether or not it
           can be dragged. So the rule is by ROLE — anything that repeats what
           the page already says goes, anything that decides WHAT YOU ARE
           LOOKING AT stays, and the two conveniences keep their icon and drop
           their label. Result is ~340px of the 390 with nothing clipped. */
        .topbar{padding:0 8px;gap:6px;overflow-x:auto;overflow-y:hidden;scrollbar-width:none}
        .topbar::-webkit-scrollbar{display:none}
        .topbar>*{flex:0 0 auto}
        /* "You're on a free trial / 7 days left" — the in-page banner directly
           below it says the same thing, with room to say it properly. */
        .topbar .trial{display:none}
        /* "No creator selected · NO DATA" and "UTC+00:00" are repeated by the
           amber banner pinned to the bottom of every page; "Referrals" and
           "Leaderboard" are secondary nav that lives in the menu. One rule
           catches all four -- they are div.pill and a.pill in .top-right. */
        .topbar .top-right .pill{display:none}
        .topbar .top-right{gap:6px}
        /* Search and "Normal view" keep the icon, lose the words. */
        .topbar .ft-pill span{display:none}
        .topbar .ft-pill{padding:0 9px}
        /* There is no Cmd key on a phone, so the shortcut hint is dead weight
           -- and it is the widest thing left in the bar. */
        .topbar .ft-pill kbd,#ft-search-pill kbd{display:none}
        #ft-search-pill{padding:0 9px!important;gap:0!important}
        /* The creator name is the one label worth its width -- it is the answer
           to "whose numbers am I looking at" -- so it truncates, not hides. */
        .topbar .ms-name{max-width:70px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
        .main{padding:14px 14px 28px;overflow-y:auto;overscroll-behavior:contain}
        /* Wide content scrolls in its own box, never the body — BUILD_GUIDE §4. */
        .main table{display:block;overflow-x:auto;max-width:100%}

        .sidebar{position:fixed;top:52px;left:0;bottom:0;z-index:60;
          width:min(84vw,300px);flex-basis:auto;
          transform:translateX(-100%);transition:transform .18s ease;
          overflow-y:auto;overscroll-behavior:contain;
          padding-bottom:calc(16px + env(safe-area-inset-bottom))}
        html.ft-phonenav .sidebar{transform:translateX(0)}
        .ft-scrim{position:fixed;inset:52px 0 0 0;z-index:59;background:rgba(0,0,0,.6);
          opacity:0;pointer-events:none;transition:opacity .18s ease;border:0;padding:0;margin:0}
        html.ft-phonenav .ft-scrim{opacity:1;pointer-events:auto}
        .panel-btn{position:relative;z-index:61}

        /* ── the two families the 390px probe found ───────────────────────
           (1) right-aligned action clusters. Every one is 'margin-left:auto'
               + 'display:flex' with no wrap, so at 390 they run off the edge
               carrying the page's primary buttons with them. Dropping the
               auto margin lets them sit under the heading instead of beside
               it; 'flex-wrap' does the rest. */
        .head-right,.hact,.sh-right,.al-right,.hr,.c-act,.vm-hero-act,
        .fu-when,.rx-stat,.cre-tools,.vm-legend{
          margin-left:0;width:100%;flex-wrap:wrap;justify-content:flex-start}

        /* (2) the fx kit. These rules are byte-identical in all 52 pages
               (BUILD_GUIDE: append the block from _shared/components.html),
               so overriding them once here reaches every page that uses them. */
        .fx-card-h,.fx-tc-row,.fx-kv,.fx-check{flex-wrap:wrap;min-width:0}
        .fx-togglecard{padding:14px 15px}
        .fx-composer,.fx-card{max-width:100%}
        /* A segmented control is a row of fixed-width choices — it cannot wrap
           without reading as separate buttons, so it scrolls instead. */
        .fx-seg{display:flex;max-width:100%;overflow-x:auto;scrollbar-width:none}
        .fx-seg::-webkit-scrollbar{display:none}
        .fx-seg>*{flex:0 0 auto}

        /* The dashboard's KPI row is the only 3-up grid in the skin. */
        .metrics{grid-template-columns:1fr;grid-auto-rows:auto}

        /* The action clusters above only helped once their PARENT row could
           wrap -- a wrapping child inside a nowrap row is still pushed off the
           edge. These are the rows that hold a heading and its buttons. */
        .inbox-head,.fan-head,.ghead,.al-cols,.rx-hero,.sh-cols,.vm-hero,
        .card-head,.sec-row,.head-row{flex-wrap:wrap}
        /* Tab strips scroll instead of wrapping -- a wrapped tab row reads as
           two rows of unrelated buttons. Same shape as the settings strips. */
        .rv-tabs,.sbtabs,.thread-tabs,.rtabs{overflow-x:auto;overflow-y:hidden;
          scrollbar-width:none;flex-wrap:nowrap}
        .rv-tabs::-webkit-scrollbar,.sbtabs::-webkit-scrollbar{display:none}
        .rv-tabs>*,.sbtabs>*{flex:0 0 auto}

        /* ── (3) the grids ────────────────────────────────────────────
           .fx-grid2/.fx-grid3 are the kit's own Advanced-drawer grids (30
           pages each) and .sec-head is the canonical section header (43).
           Those three plus .metrics are most of the skin; the rest below is
           the per-page tail the probe turned up. */
        .sec-head,.card-head,.panel-head{flex-wrap:wrap}
        .fx-grid2,.fx-grid3,.metrics,.cards,.stats,.af-grid,.mm-grid,.bc-grid,
        .bc-poolgrid,.lad2,.mon-grid,.pxadv,.sum-grid,.esum,.grid-bottom,
        .kpigrid,.kc-kpis,.nudge-grid,.wk-stats,.pxform-grid,.logrow,.wk-slot{
          grid-template-columns:1fr!important;grid-auto-rows:auto}
        /* A grid or flex child is min-width:auto by default, so one wide
           descendant -- a textarea with a cols attribute, an unbroken URL --
           makes the TRACK wider than its container instead of shrinking. That
           is what was left after the templates were collapsed. */
        .fx-grid2>*,.fx-grid3>*,.mm-grid>*,.bc-grid>*,.af-grid>*,.mon-grid>*,
        .lad2>*,.pxadv>*,.nudge-grid>*,.grid-bottom>*,.kc-kpis>*,.aut-grid>*,
        .sh-cols>*,.al-cols>*,.card>*,.irow>*{min-width:0}
        .card,.irow,.sh-right,.al-right,.inbox-head,.c-act{flex-wrap:wrap;min-width:0}
        /* A form control sized in columns/characters ignores its container. */
        .main textarea,.main input,.main select{max-width:100%}

        /* The last of it: panes and controls pinned to a desktop pixel width.
           'flex:0 0 430px' cannot shrink by definition, so these need the
           basis released, not just min-width:0. */
        .sh-right,.al-right{width:auto;flex:1 1 auto}
        .vm-search{width:auto;flex:1 1 auto;min-width:0}
        /* A full-width primary button is the phone convention anyway. */
        .c-act .fx-btn,.vm-hero-act .fx-btn,.fx-composer .fx-btn{max-width:100%}
        .fu-msg,.act-note{min-width:0;max-width:100%;overflow-wrap:anywhere}

        /* ── what only appears once there is DATA ──────────────────────
           These were invisible on an empty mock: a creator row with no
           creators has no tools, a rule card with no rule has no cadence
           chip, a rewards page with no fans has no fan chips. Signed in on
           the live stack, each of them runs off the right. */
        .cre-tools{margin-left:0;width:100%;flex-wrap:wrap}
        .cre-search{flex:1 1 auto;min-width:0;width:auto}
        .cre-search input{min-width:0;width:100%}
        /* Cadence chips ("every 1 min", "every 3-7 h") sit under a rule name
           and are inline-flex, so they cannot shrink or wrap on their own. */
        .tc-cad{max-width:100%;white-space:normal;height:auto;min-height:22px;padding:2px 9px}
        /* Fan chips carry a display name of unbounded length. */
        .fchip{max-width:100%;min-width:0}
        .fnm,.fchip>*{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
        .vm-meta{max-width:calc(100% - 18px);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
        .rx-stat{min-width:0}
        /* The composer bar keeps Send on screen by wrapping, not by pushing. */
        .fx-comp-bar{flex-wrap:wrap;gap:6px}
        .fx-comp-bar>*{min-width:0}
        /* A card header that carries controls as well as a title. */
        .fx-card-h>*{min-width:0}
        .fx-card-h span{flex-wrap:wrap;min-width:0}
        /* The vault toolbar holds a search box and filters on one line. */
        .vm-toolbar{flex-wrap:wrap;gap:8px}
        .vm-toolbar>*{min-width:0}
        .vm-search{flex:1 1 100%}

        /* ── the last eleven, diagnosed signed-in on the live stack ───────
           Almost all of it is one shape: a flex row set to nowrap holding a
           cluster it cannot fit, or a child that cannot shrink because its
           min-width is auto. Listed by the row rather than the symptom so the
           next person can see the family. */
        .hometabs,.revrow,.revaxis,.fpick,.mgr-head,.kbd-hint,.fx-presets,
        .nc-top,.ov-body,.count-pill,.mgr-head .right{flex-wrap:wrap;min-width:0}
        .hometabs>*,.revrow>*,.revaxis>*,.fpick>*,.mgr-head>*,.fx-presets>*,
        .nc-top>*,.ov-body>*,.ghead>*,.fx-check>*{min-width:0}
        /* Single-column grids whose content still sets the track width. */
        .mon-cols>*,.mon-cols .body,.mon-h2,.mon-empty{min-width:0;max-width:100%}
        /* Preset tiles and funnel picks are fixed-content cards in a row. */
        .fx-preset,.fpk{min-width:0;max-width:100%}
        .fx-preset>*,.fpk>*,.fpk .r1,.fpk .nm,.fpk .meta{min-width:0;max-width:100%;
          overflow:hidden;text-overflow:ellipsis}
        .panelwrap{min-width:0;max-width:100%}
        /* The vault search input carries its own pixel width. */
        .vm-search input,.mgr-head input,.ov-body input{width:100%;min-width:0}
        /* group.html shares the chat top strip, so it gets the same sort: the
           status pills repeat what the page says, the rest can wrap. */
        .topstrip .top-right{flex-wrap:wrap;min-width:0}
        .topstrip .count-pill{max-width:100%}

        /* The final three, each a pixel width the page sets on itself and wins
           on, so these need the declaration force rather than a better rule.
           width:230px with flex:0 0 230px cannot shrink by any other means. */
        .c-when{width:auto!important;flex:1 1 100%!important;min-width:0}
        /* flex-basis:100% alone did not break the line here, so min-width is
           what actually forces it onto its own row. */
        .vm-search{width:100%!important;flex:1 1 100%!important;min-width:100%!important}
        /* An action cluster rendered as a span has no layout of its own. */
        /* flex:0 0 212px beats any width declaration, so the BASIS has to be
           replaced -- and its two rows have to be allowed to wrap first. */
        .shead,.srow{flex-wrap:wrap;min-width:0}
        .shead>*,.srow>*{min-width:0}
        .c-act,span.c-act{display:flex!important;flex:1 1 100%!important;
          min-width:100%!important;flex-wrap:wrap;margin-left:0}
        /* An auto left margin parks this icon at the far right of its line, so
           on a full line it lands past the edge. A fixed gap keeps it beside
           what it belongs to. */
        .fx-check .bx{margin-left:6px;flex:0 0 auto}
        .fx-check{width:100%;padding-right:2px}

        /* Card walls: one column. Two 181px columns turn a sentence into two
           words per line, which measures as "fits" and reads as unusable. */
        .aut-grid,.mon-cols,.mgrid,.tpl-grid,.rc-queue,.brain-grid,
        .al-pass-grid,.sh-grid,.fu-slots,.emgrid,.fgrid{
          grid-template-columns:1fr!important}
        /* Stat strips keep two columns — numbers stay scannable in pairs. */
        .vgrid,.gifgrid,.crestats,.kpirow,.rx-hero,.imp-stats,.fansgrid,
        .vm-grid{grid-template-columns:1fr 1fr!important}
        /* A 7-column month header only means anything at full width. */
        .mthead{min-width:640px}
        /* A 7-day calendar loses its meaning stacked, so it scrolls as a unit
           and keeps its columns. .emoji-pop is left alone: eight emoji fit. */
        .cal-wrap,.weekcal,.wc-body,.cal-grid{min-width:640px}
        .cal-scroll{overflow-x:auto;scrollbar-width:none}

                /* Toasts are bottom-right on desktop; at 390 that box is most of the
           screen, so they sit above the "no creator" banner instead. */
        /* This skin has no backend, so its two fetches fail on every page and
           five identical toasts stack up the screen. On a phone that is half
           the viewport spent saying one thing, so only the newest two show. */
        .ft-toast-wrap{left:10px;right:10px;width:auto;align-items:stretch;
          bottom:calc(56px + env(safe-area-inset-bottom));z-index:70}
        .ft-toast{max-width:100%}
        .ft-toast-wrap>.ft-toast:nth-last-child(n+3){display:none}

        /* The dashboard's earnings card is a flex row of a donut and a stat
           column; at 390 the column has nowhere to go, so it stacks. */
        .earn{flex-direction:column;padding:18px 12px;gap:14px}
        .earn>*{min-width:0;max-width:100%}
      }
      @media (max-width:760px) and (prefers-reduced-motion:reduce){
        .sidebar,.ft-scrim{transition:none}
      }`);

    const scrim = document.createElement("button");
    scrim.className = "ft-scrim";
    scrim.type = "button";
    scrim.setAttribute("aria-label", "Close the menu");
    scrim.tabIndex = -1;
    document.body.appendChild(scrim);

    const setNav = (open) => {
      root.classList.toggle("ft-phonenav", open);
      scrim.tabIndex = open ? 0 : -1;
      const btn = document.querySelector(".topbar .panel-btn");
      if (btn) btn.setAttribute("aria-expanded", open ? "true" : "false");
    };

    // Under the breakpoint the rail is suppressed but its stored value is left
    // alone, so a phone visit never costs the operator his desktop rail.
    const applyWidth = () => {
      if (mq.matches) {
        root.classList.remove("ft-siderail");
      } else {
        setNav(false);
        try {
          if (lsGet("fastt_rail") === "1") root.classList.add("ft-siderail");
        } catch (e) {}
      }
    };
    applyWidth();
    mq.addEventListener("change", applyWidth);

    // The panel button already collapses the rail on desktop (mountRailToggle).
    // On a phone it opens the drawer instead — capture phase so it lands before
    // that handler and the rail is never toggled at this width.
    document.addEventListener("click", (e) => {
      const btn = e.target.closest && e.target.closest(".topbar .panel-btn");
      if (!btn || !mq.matches) return;
      e.stopPropagation();
      e.preventDefault();
      setNav(!root.classList.contains("ft-phonenav"));
    }, true);

    scrim.addEventListener("click", () => setNav(false));
    // A tap on a real link navigates; closing first stops the drawer flashing
    // over the next page during the load.
    document.addEventListener("click", (e) => {
      if (!mq.matches || !root.classList.contains("ft-phonenav")) return;
      const a = e.target.closest && e.target.closest(".sidebar a[href]");
      if (a && !a.getAttribute("href").startsWith("#")) setNav(false);
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && root.classList.contains("ft-phonenav")) setNav(false);
    });
  }

  /** PHONE CHAT — list and thread are one screen each, not three columns.
   *
   *  messages.html and group.html are the two pages that do NOT use the
   *  canonical sidebar shell. They are three fixed panes: .list at 390px,
   *  .center (the thread) taking the rest, and .right at 280px. On a 390px
   *  phone only the list fits, so tapping a fan appeared to do nothing --
   *  selectFan() ran, the thread rendered, and it rendered off-screen.
   *
   *  So the panes become screens. The list is the phone default; a tap on a
   *  .conv row opens the thread full-width; the .back chevron the skin already
   *  ships in .thread-head (decoration until now, exactly like .panel-btn was)
   *  goes back. .right is left off the phone entirely -- fan insights are a
   *  reference panel, and the ask was to put the focus on the conversation.
   *
   *  This runs on the BUBBLE phase so messages.js's own row handler has already
   *  called selectFan() by the time the pane swaps: the thread is populated
   *  before it is shown, never after. */
  function mountPhoneChat() {
    const list = document.querySelector(".list");
    const center = document.querySelector(".center");
    if (!list || !center) return;              // not a chat page
    const root = document.documentElement;
    if (root.__ftPhoneChat) return;
    root.__ftPhoneChat = 1;

    styleOnce("ft-phonechat-css", `
      @media (max-width:760px){
        .list{width:100%;min-width:0;flex:1 1 auto;border-right:0}
        .center,.right{display:none}
        /* One screen at a time: showing the thread hides the list outright
           rather than sliding it, so no second scroll container survives. */
        html.ft-chat-open .list{display:none}
        html.ft-chat-open .center{display:flex;flex:1 1 auto;min-width:0}
        html.ft-chat-open .right{display:none}
        /* The chevron is 22px of SVG in the markup; give it a real 44px box
           without moving it on desktop, where this whole block never applies. */
        .thread-head .back{width:26px;height:26px;padding:8px;margin:-8px -2px -8px -8px;
          box-sizing:content-box;cursor:pointer;border-radius:9px}
        .thread-head .back:active{background:#222}
        .thread-head{gap:8px;padding:10px 10px}
        .thread-body{padding:14px 12px}

        /* ── the chat shell's own three rows ────────────────────────────
           messages.html does not use the canonical topbar, so it needs the
           same role sort: what names the conversation stays, what is a
           desktop idea goes, what is a tool strip scrolls. */
        .topstrip{overflow-x:auto;overflow-y:hidden;scrollbar-width:none;padding-right:8px}
        .topstrip::-webkit-scrollbar{display:none}
        .topstrip .ft-pill span{display:none}
        .topstrip .top-right{gap:6px}
        .topstrip .ft-pill kbd{display:none}
        /* Adding a creator means capturing a session, which is a desktop job in
           this product -- /setup says so itself. The scope row keeps Home and
           the model switcher, which are what you navigate with. */
        .topstrip .add-creator{display:none}
        .topstrip .htab,.topstrip .am-hero{padding-left:10px;padding-right:10px}
        /* The translate dropdown and the window/PiP square are Infloww desktop
           chrome we cloned for looks -- neither is wired to anything (see the
           two markAll() calls in messages.js). On a phone there is no second
           window and no browser translate bar to open, so they are pure width.
           .ft-pill / #ft-sfw-pill are injected separately and are unaffected. */
        .topstrip .top-right .tr-box,
        .topstrip .top-right .icon-btn{display:none}
        /* "Pop out" opens the thread in a second window -- there is no second
           window on a phone. */
        .thread-head .th-right{display:none}
        /* Six tools (star, mute, notes, pin, Gallery, Find) cannot fit beside a
           name at 390. The name is the thing that must not move, so it holds
           its ground and the tools scroll under the thumb. */
        .thread-head .th-id{min-width:0;flex:0 1 auto}
        .thread-head .th-tools{flex:1 1 auto;min-width:0;overflow-x:auto;
          overflow-y:hidden;scrollbar-width:none}
        .thread-head .th-tools::-webkit-scrollbar{display:none}
        .thread-head .th-tools>*{flex:0 0 auto}
        /* The composer is the other half of "focus on the chat view": both its
           rows wrap rather than push Send off the edge. */
        /* The composer: the box you type in was 29px tall and wedged between
           six buttons, while the button row under it took 79px. On a phone the
           message is the point, so the input takes its own full-width row at a
           real height and the controls wrap beneath it. */
        .composer{padding:10px 10px calc(10px + env(safe-area-inset-bottom))}
        .comp-r1,.comp-r2{flex-wrap:wrap;min-width:0;gap:8px}
        .comp-r1>*,.comp-r2>*{min-width:0}
        .comp-input{flex:1 1 100%;min-width:100%;order:-1}
        .comp-input input{
          width:100%;min-height:46px;padding:12px 14px;
          border-radius:11px;background:#1c1c1c;border:1px solid var(--border);
          /* 16px exactly: iOS zooms the whole page when a focused field is
             smaller than this, which is its own kind of "sticks out of view". */
          font-size:16px;line-height:1.35}
        .comp-input input::placeholder{font-size:15px}
        /* Giving the input its own row cost the thread 49px, so the two button
           rows give most of it back: they are icon buttons, not reading matter. */
        .comp-r1{gap:6px;margin-bottom:0}
        .comp-r2{gap:6px}
        .comp-r1 .pill,.comp-r2 .pill,.comp-r1 button,.comp-r2 button{
          height:34px;padding-top:0;padding-bottom:0}

        /* Room for the fixed "no creator selected" bar, which otherwise covers
           whatever each page ends with -- Send, here. */
        html.ft-has-noacct .composer{padding-bottom:calc(60px + env(safe-area-inset-bottom))}
        html.ft-has-noacct .main{padding-bottom:calc(68px + env(safe-area-inset-bottom))}
        html.ft-has-noacct .rows,html.ft-has-noacct .thread-body{padding-bottom:60px}
        #ft-noacct{font-size:11px!important;padding:7px 12px!important;line-height:1.3}
      }`);

    const mq = window.matchMedia("(max-width:760px)");
    const open = (on) => root.classList.toggle("ft-chat-open", !!on && mq.matches);

    // Bubble phase -- messages.js has already selected the fan.
    document.addEventListener("click", (e) => {
      if (!mq.matches || !e.target.closest) return;
      if (e.target.closest(".rows .conv")) open(true);
    });

    const back = document.querySelector(".thread-head .back");
    if (back) {
      back.setAttribute("role", "button");
      back.setAttribute("tabindex", "0");
      back.setAttribute("aria-label", "Back to conversations");
      const go = (e) => { if (!mq.matches) return; e.preventDefault(); e.stopPropagation(); open(false); };
      back.addEventListener("click", go);
      back.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") go(e); });
    }
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && root.classList.contains("ft-chat-open")) open(false);
    });
    // Leaving phone width puts all three panes back, so a rotation to landscape
    // never strands the operator inside a one-pane view.
    mq.addEventListener("change", () => { if (!mq.matches) root.classList.remove("ft-chat-open"); });
  }

  /** PHONE MODEL PICKER — the roster becomes a sheet instead of a train.
   *
   *  On desktop row 1 is a tab bar: Home, All Models, one .ctab per creator,
   *  Add a Creator. On a phone that row is wider than the screen, so it turned
   *  into a horizontal scroller -- every creator past the second was off the
   *  right edge, and the row below it (Group chat) made the whole header two
   *  wrapped lines of half-cut tabs.
   *
   *  So the creator tabs move out of the row and become a full-width dropdown
   *  under a single trigger that shows WHO you are currently talking as. The
   *  row that is left is one line: Home, Group chat, All Models, that trigger.
   *
   *  #ctabs is REPOSITIONED, never cloned. messages.js owns its markup and
   *  binds click/keyboard/drag on the container itself, so switching creators,
   *  closing a tab and the live unread badges all keep working untouched --
   *  the sheet is the same element wearing different CSS. */
  function mountPhoneModelPicker() {
    const strip = document.querySelector(".topstrip");
    const ctabs = strip && strip.querySelector("#ctabs");
    if (!strip || !ctabs) return;              // not the chat shell
    const root = document.documentElement;
    if (root.__ftPhoneModels) return;
    root.__ftPhoneModels = 1;

    styleOnce("ft-phonemodels-css", `
      /* Hidden by default, not "hidden above 760": a 760.5px viewport matches
         neither max-width:760 nor min-width:761, and that gap is a stray
         button on someone's tablet. The phone block below opts them back in. */
      #ft-models-btn,#ft-models-backdrop{display:none}
      @media (max-width:760px){
        .topstrip{gap:6px}
        /* Home and Group chat are one-tap destinations whose labels were
           costing ~130px of a 390px row. The icons already say it. */
        .topstrip .htab{font-size:0;gap:0;padding:6px 11px}
        .topstrip .htab svg{width:18px;height:18px;color:#e8e8e8}
        /* The scope tab keeps a word -- it names a mode, not a place, so an
           icon alone would not carry it. But "All Models" is 89px next to a
           button that already says which model, so on a phone it is "All".
           Swapped in CSS so the desktop markup and its href stay untouched. */
        .topstrip .am-hero{margin-left:0;padding:0 12px;font-size:0}
        .topstrip .am-hero svg{display:none}
        .topstrip .am-hero:after{content:"All";font-size:13px;font-weight:700}
        /* Row 2 exists only for popped-out fan tabs now; with none open it
           collapses instead of holding a 34px empty stripe. */
        .hometabs{height:auto;min-height:0;padding:0 8px;border-bottom:0}

        /* ── the trigger ─────────────────────────────────────────────── */
        #ft-models-btn{display:inline-flex;align-items:center;gap:7px;
          align-self:flex-end;margin-bottom:5px;height:30px;padding:0 9px;
          border-radius:8px;background:#2d2130;border:1px solid rgba(236,75,155,.55);
          color:#ffd8ec;font:700 13px Inter,system-ui,sans-serif;flex:0 1 auto;min-width:0}
        #ft-models-btn:active{background:#3a2a3e}
        #ft-models-btn .ft-mb-av{width:22px;height:22px;border-radius:6px;flex:none;
          overflow:hidden;display:grid;place-items:center;
          background:linear-gradient(135deg,#f3a26d,#d96b8f 55%,#7b5cc0)}
        #ft-models-btn .ft-mb-av img{width:100%;height:100%;object-fit:cover;display:block}
        #ft-models-btn .ft-mb-name{max-width:88px;overflow:hidden;
          text-overflow:ellipsis;white-space:nowrap}
        #ft-models-btn .ft-mb-n{font-size:10px;font-weight:700;color:#d8b6c8;
          background:rgba(236,75,155,.18);border-radius:7px;padding:1px 5px;flex:none}
        #ft-models-btn .ft-mb-n:empty{display:none}
        #ft-models-btn svg{width:13px;height:13px;flex:none;
          transition:transform .14s}
        html.ft-models-open #ft-models-btn svg{transform:rotate(180deg)}

        /* ── the sheet (same #ctabs element) ─────────────────────────── */
        .topstrip .ctabs{display:none}
        html.ft-models-open .topstrip .ctabs{
          display:flex;position:fixed;left:0;right:0;z-index:60;
          flex-direction:column;align-items:stretch;gap:4px;
          max-width:none;max-height:min(62vh,430px);overflow-y:auto;
          background:var(--topbar);border-bottom:1px solid var(--border);
          box-shadow:0 18px 40px rgba(0,0,0,.55);
          padding:8px 8px calc(8px + env(safe-area-inset-bottom))}
        html.ft-models-open .topstrip .ctab{
          top:0;width:auto;max-width:none;height:50px;padding:0 6px 0 8px;gap:10px;
          border-radius:11px;border-bottom-width:1px}
        html.ft-models-open .topstrip .ctab .cav{width:34px;height:34px;border-radius:9px}
        html.ft-models-open .topstrip .ctab .cname{font-size:15px;flex:1 1 auto;
          min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
        /* 16px of ✕ beside a name is a mis-tap waiting to happen when the row
           itself switches creator. */
        html.ft-models-open .topstrip .ctab .cclose{width:40px;height:40px;
          margin-left:0;font-size:15px;border-radius:9px}
        html.ft-models-open .topstrip .ctabs-empty{padding:14px 10px;font-size:13px}
        html.ft-models-open #ft-models-backdrop{display:block;position:fixed;
          inset:0;z-index:59;background:rgba(0,0,0,.45)}
      }`);

    const btn = document.createElement("button");
    btn.id = "ft-models-btn";
    btn.type = "button";
    btn.setAttribute("aria-haspopup", "true");
    btn.setAttribute("aria-expanded", "false");
    btn.innerHTML =
      '<span class="ft-mb-av"></span><span class="ft-mb-name">Models</span>' +
      '<span class="ft-mb-n"></span>' +
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" ' +
      'stroke-linecap="round"><path d="M6 9l6 6 6-6"/></svg>';
    ctabs.parentNode.insertBefore(btn, ctabs);

    const backdrop = document.createElement("div");
    backdrop.id = "ft-models-backdrop";
    document.body.appendChild(backdrop);

    const mq = window.matchMedia("(max-width:760px)");
    const isOpen = () => root.classList.contains("ft-models-open");
    const setOpen = (on) => {
      on = !!on && mq.matches;
      // Measured, not guessed: the strip's height changes with the safe area
      // and with whether row 2 has any popped-out tabs in it.
      if (on) ctabs.style.top = Math.max(0, Math.round(strip.getBoundingClientRect().bottom)) + "px";
      else ctabs.style.top = "";
      root.classList.toggle("ft-models-open", on);
      btn.setAttribute("aria-expanded", on ? "true" : "false");
    };
    btn.addEventListener("click", (e) => { e.preventDefault(); e.stopPropagation(); setOpen(!isOpen()); });
    backdrop.addEventListener("click", () => setOpen(false));
    // Picking a creator -- or closing her tab -- is the end of the sheet's job.
    // Deferred so messages.js's own delegated handler runs on a live element.
    ctabs.addEventListener("click", (e) => {
      if (isOpen() && e.target.closest(".ctab")) setTimeout(() => setOpen(false), 0);
    });
    document.addEventListener("keydown", (e) => { if (e.key === "Escape" && isOpen()) setOpen(false); });
    window.addEventListener("resize", () => { if (!mq.matches) setOpen(false); });

    // messages.js rewrites #ctabs wholesale on every switch, so the trigger
    // reads its label back off the DOM rather than tracking state of its own.
    const sync = () => {
      const tabs = ctabs.querySelectorAll(".ctab");
      const act = ctabs.querySelector(".ctab.active") || tabs[0];
      const nm = act && act.querySelector(".cname");
      const label = nm ? nm.textContent.trim() : "Models";
      btn.querySelector(".ft-mb-name").textContent = label;
      const av = btn.querySelector(".ft-mb-av");
      const img = act && act.querySelector(".cav img");
      av.textContent = "";
      if (img) av.appendChild(img.cloneNode(true));
      btn.querySelector(".ft-mb-n").textContent = tabs.length > 1 ? String(tabs.length) : "";
      btn.setAttribute("aria-label", "Switch creator — currently " + label);
    };
    new MutationObserver(sync).observe(ctabs, { childList: true, subtree: true });
    sync();

    // Home and Group chat are both "leave this screen" -- on a phone they sit
    // side by side in the one header row instead of on two stacked stripes.
    const home = strip.querySelector(".htab");
    const hometabs = document.querySelector(".hometabs");
    const group = hometabs && hometabs.querySelector(".htab");
    const place = () => {
      if (!group) return;
      if (mq.matches) {
        if (group.parentNode !== strip) strip.insertBefore(group, home ? home.nextSibling : strip.firstChild);
      } else if (group.parentNode === strip) {
        hometabs.insertBefore(group, hometabs.firstChild);
      }
    };
    place();
    mq.addEventListener("change", () => { place(); setOpen(false); });
  }

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
    mountPhoneLayout();
    mountPhoneChat();
    mountPhoneModelPicker();
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
