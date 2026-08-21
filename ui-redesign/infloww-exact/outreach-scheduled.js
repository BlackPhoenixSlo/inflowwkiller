/* Live wiring — GET /api/of/v2/scheduled-sends (+ DELETE /{job_id}) for the
   relay's own send-later jobs, and GET /api/of/v2/schedules (type:"chat") for
   OF's native queue (+ DELETE /messages/queue/{queue_id}, confirm-gated). */
Fastt.ready(async () => {
  "use strict";
  const $ = Fastt.$, esc = Fastt.esc;
  const row = Fastt.accountRow();
  const nick = row ? (row.nickname || String(row.id)) : (Fastt.account() || "—");
  const GRADS = ["g1", "g2", "g3", "g4", "g5"];

  Fastt.liveBadge($("#cardUpcoming .card-h"));
  Fastt.liveBadge($("#cardRoster .card-h"));
  Fastt.staticBadge($("#heroCard .sh-kicker"), "ILLUSTRATION");

  const scope = $("#qScope");
  if (scope) {
    scope.innerHTML = "Everything queued on <b style=\"color:#cfd8ff;font-weight:600\">" + esc(nick)
      + "</b> — the OF scheduled queue and this relay's own send-later jobs, both account-scoped. "
      + "Cancel a send any time before it fires. Use “Scan all creators” below for the whole roster.";
  }

  // ── fan identity: /admin/fans/{account}/by-ids gives name + avatar for the
  //    ids a queued 1:1 row carries (OF's own queue rows are list-targeted and
  //    have no single recipient, so they stay "Mass blast"). ────────────────
  const fanCache = new Map();
  async function resolveFans(ids) {
    const want = ids.filter((n) => n != null && !fanCache.has(String(n)));
    if (!want.length || !Fastt.account()) return;
    try {
      const out = await Fastt.get("/admin/fans/" + Fastt.account() + "/by-ids",
        { ids: want.join(",") });
      const fans = (out && out.fans) || {};
      for (const k of Object.keys(fans)) fanCache.set(String(k), fans[k] || {});
    } catch (e) { console.warn("fan name lookup failed", e); }
    // ids the DB has never seen must not be retried on every repaint
    for (const n of want) if (!fanCache.has(String(n))) fanCache.set(String(n), {});
  }
  function fanLabel(id) {
    const f = fanCache.get(String(id)) || {};
    const nm = f.customNickname || f.name || f.username;
    return nm ? String(nm) : "Fan " + (id == null ? "?" : id);
  }
  function fanAvatar(id, grad, init) {
    const f = fanCache.get(String(id)) || {};
    if (f.avatar) {
      return '<span class="sav ' + grad + '"><img src="' + esc(f.avatar) + '" alt="" '
        + 'onerror="this.remove()"></span>';
    }
    return '<span class="sav ' + grad + '">' + esc(init) + "</span>";
  }

  // Drop the mockup's fabricated queue rows BEFORE the first fetch — a failed
  // load must never leave fake sends sitting under live Cancel buttons.
  const rowsHost = $("#schedRows");
  if (rowsHost) rowsHost.innerHTML = "";

  const CLOCK = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><path d="M12 7.5V12l3 2" stroke-linecap="round"/></svg>';
  // Server stamps are parsed through Fastt.parseUtc: /scheduled-sends emits
  // "...isoformat()+Z", OF's queue emits its own offset — parseUtc handles both
  // and also rescues a bare tz-naive stamp instead of reading it as local time.
  const ts = (s) => { const d = Fastt.parseUtc(s); return d ? d.getTime() : NaN; };
  function fmtWhen(iso) {
    if (!iso) return "—";
    const d = Fastt.parseUtc(iso);
    if (!d) return "—";
    return d.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" })
      + " · " + d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
  }
  function rel(iso) {
    if (!iso) return "—";
    const ms = ts(iso) - Date.now();
    if (!isFinite(ms)) return "—";
    if (ms <= 0) return "Overdue — goes out on the next server pass";
    const m = Math.round(ms / 60000);
    if (m < 60) return "in " + m + " m";
    const h = Math.floor(m / 60);
    if (h < 48) return "in " + h + " h " + String(m % 60).padStart(2, "0") + " m";
    return "in " + Math.round(h / 24) + " days";
  }

  // OF entity text arrives as HTML (<br>, &nbsp;, entities). Flatten to plain
  // text the same way the real app (lib/ofHtml.stripOFHtml) does.
  function stripHtml(s) {
    return String(s || "")
      .replace(/<br\s*\/?>/gi, "\n").replace(/<\/p\s*>/gi, "\n")
      .replace(/<[^>]+>/g, "")
      .replace(/&nbsp;/g, " ").replace(/&amp;/g, "&").replace(/&lt;/g, "<")
      .replace(/&gt;/g, ">").replace(/&quot;/g, '"').replace(/&#39;/g, "'")
      .trim();
  }
  // The real app reads scheduled OF sends from /schedules (calendar), NOT the
  // older /messages/queue which returns stale/empty rows for sends made in the
  // new OF web flow. Mirror that: today → +1y, local tz, flatten type:"chat".
  function schedRange() {
    const fmt = (d) => d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0")
      + "-" + String(d.getDate()).padStart(2, "0");
    const now = new Date(), end = new Date(); end.setFullYear(now.getFullYear() + 1);
    const tz = (Intl.DateTimeFormat().resolvedOptions().timeZone) || "UTC";
    return { limit: 50, publish_date: fmt(now), publish_date_end: fmt(end), time_zone: tz };
  }
  // Direct (1 recipient, no lists/groups) vs mass audience — mirrors the real
  // app's massLabel so a 1-fan send never reads the same as a 200-fan blast.
  function audienceOf(e) {
    const fans = (e.userIds || []).length, lists = (e.userLists || []).length,
      groups = (e.groups || []).length;
    if (fans === 1 && !lists && !groups) return { direct: true, fanId: e.userIds[0] };
    const parts = [];
    if (lists) parts.push(lists + " list" + (lists === 1 ? "" : "s"));
    if (groups) parts.push(groups + " group" + (groups === 1 ? "" : "s"));
    if (fans) parts.push(fans + " fan" + (fans === 1 ? "" : "s"));
    return { direct: false, label: parts.join(" + ") || "everyone it targets" };
  }

  async function load() {
    const wrap = $("#schedRows"), empty = $("#schedEmpty"), head = $("#schedHead");
    let oneToOne = [], ofItems = [], ofErr = false, syncing = false;
    try {
      const out = await Fastt.get("/api/of/v2/scheduled-sends");
      oneToOne = (out && out.list) || [];
    } catch (e) { Fastt.oops(e); }
    try {
      const q = await Fastt.get("/api/of/v2/schedules", schedRange());
      syncing = !!(q && q.syncInProcess);
      ofItems = ((q && q.list) || [])
        .filter((it) => it && it.type === "chat" && it.entity && isFinite(Number(it.entity.id)))
        .map((it) => ({ when: it.publishDateTime || it.entity.scheduledDate || null, e: it.entity }));
    } catch (e) { ofErr = true; console.warn("OF /schedules unavailable", e); }

    const rows = [];
    // Resolve names for every single-recipient row up front (relay jobs +
    // OF direct sends) so the table paints once.
    const directOfIds = ofItems.map((x) => audienceOf(x.e)).filter((a) => a.direct).map((a) => a.fanId);
    await resolveFans(oneToOne.map((j) => j.fan_id).concat(directOfIds));
    for (const j of oneToOne) {
      rows.push({
        kind: "dm", id: j.job_id, when: j.run_at, status: j.status,
        text: stripHtml(j.text) || "(no text)", price: Number(j.price) || 0,
        media: Number(j.media_count) || 0, fanId: j.fan_id,
        who: fanLabel(j.fan_id), sub: "on " + nick + " · server send-later",
      });
    }
    for (const { when, e } of ofItems) {
      // entity.id is the OF queue id → DELETE /messages/queue/{id} cancels it
      // (same path the real app's cancel mutation uses).
      const aud = audienceOf(e);
      const price = Number(e.price) || 0, media = Number(e.mediaCount) || (e.media || []).length || 0;
      const text = stripHtml(e.text) || "(no text)";
      if (aud.direct) {
        rows.push({
          kind: "mass", id: e.id, when, status: "pending", text, price, media,
          fanId: aud.fanId, who: fanLabel(aud.fanId), sub: "on " + nick + " · OF scheduled",
        });
      } else {
        rows.push({
          kind: "mass", id: e.id, when, status: "pending", text, price, media,
          who: "Mass blast", sub: "on " + nick + " · " + aud.label,
        });
      }
    }
    rows.sort((a, b) => (a.when ? ts(a.when) : 8640000000000000) - (b.when ? ts(b.when) : 8640000000000000));

    const overdue = rows.filter((r) => r.when && ts(r.when) < Date.now()).length;
    const q1 = $("#stQ"); if (q1) q1.textContent = rows.length + " queued";
    const c1 = $("#qCount"); if (c1) c1.textContent = String(rows.length);
    const so = $("#stOverdue");
    if (so) {
      // JS owns the text — the markup ships no count to fall back to.
      so.hidden = overdue === 0;
      so.innerHTML = "<i></i>" + overdue + " overdue — goes out on the next server pass";
    }

    wrap.innerHTML = rows.map((r, i) => {
      const od = r.when && ts(r.when) < Date.now();
      const init = r.kind === "mass"
        ? "MM"
        : (/[A-Za-z]/.test(r.who) ? r.who.replace(/[^A-Za-z]/g, "").slice(0, 2).toUpperCase()
                                  : String(r.fanId || "").slice(-2) || "F");
      const band = [];
      if (r.media > 0) band.push('<span class="mbadge">' + r.media + " media</span>");
      if (r.price > 0) band.push('<span class="mbadge price">' + Fastt.fmtMoney(r.price) + " to unlock</span>");
      return '<div class="srow"' + (od ? ' data-overdue="1"' : "") + ">"
        + '<div class="c-fan">' + fanAvatar(r.fanId, GRADS[i % GRADS.length], init)
        + '<span><span class="fn">' + esc(r.who) + '</span><span class="fa">' + esc(r.sub)
        + (r.kind === "dm" && r.fanId != null ? " · #" + esc(r.fanId) : "") + "</span></span></div>"
        + '<div class="c-msg"><div class="mtx">' + esc(r.text) + "</div>"
        + (band.length ? '<div class="mband">' + band.join("") + "</div>" : "") + "</div>"
        + '<div class="c-when"><span class="tchip' + (od ? " warn" : "") + '">' + CLOCK + esc(fmtWhen(r.when)) + "</span>"
        + '<div class="trel">' + esc(rel(r.when)) + "</div></div>"
        + '<div class="c-act">'
        + '<button class="fx-btn ghost btn-sm btn-resched" title="No reschedule endpoint — cancel, then schedule again">Reschedule</button>'
        + (r.id == null
            ? '<button class="fx-btn ghost btn-sm" disabled style="opacity:.5" title="This queue item carries no id — cancel it from OF directly">Cancel</button>'
            : '<button class="fx-btn ghost btn-sm btn-cancel" data-kind="' + r.kind + '" data-id="' + esc(r.id) + '">Cancel</button>')
        + "</div></div>";
    }).join("");
    if (empty) empty.hidden = rows.length > 0;
    if (head) head.style.display = rows.length ? "" : "none";
    // When OF reports it's mid-sync an empty list means "not fetched yet", not
    // "nothing queued" — say so instead of a bare empty state.
    const eSub = empty && empty.querySelector(".sub");
    if (eSub) eSub.textContent = (rows.length === 0 && syncing)
      ? "OF is still syncing its queue — hit Refresh in a moment."
      : "Pick “Send later” in any composer and it will show up here.";
    if (ofErr) Fastt.toast("OF /schedules lookup failed — showing server-side send-later jobs only", "err");
  }
  await load();

  $("#btnRefresh").addEventListener("click", () => load());

  // ── roster scan (explicit click) ─────────────────────────────
  // Read-only. Fastt.get() would rewrite X-Account-Id to the CURRENT account,
  // so each per-creator read passes noAccount:true and sets the header itself.
  // Creators whose OF session isn't loaded on this relay answer 404 "Unknown
  // account_id" — that's reported as "no session", never folded into a 0.
  const rostRows = $("#rostRows"), rostCount = $("#rostCount");
  function paintRoster(list) {
    rostRows.innerHTML = list.map((r) => {
      const cls = r.state === "ok" ? (r.n > 0 ? "rv" : "rv zero")
        : (r.state === "nosession" ? "rv na" : "rv err");
      const txt = r.state === "ok"
        ? (r.n === 1 ? "1 queued" : r.n + " queued")
        : (r.state === "nosession" ? "no OF session on this relay"
          : (r.state === "pending" ? "…" : "lookup failed — " + r.msg));
      return '<div class="rrow"><span class="rn">' + esc(r.name) + "</span>"
        + '<span class="' + cls + '">' + esc(txt) + "</span>"
        + '<span class="rid">' + esc(r.id) + "</span></div>";
    }).join("");
  }
  $("#btnScanAll").addEventListener("click", async () => {
    const btn = $("#btnScanAll");
    const accts = Fastt.accounts();
    if (!accts.length) {
      rostRows.innerHTML = '<div class="rrow"><span class="rn">No roster</span>'
        + '<span class="rv na">/admin/accounts is empty for this caller and the '
        + 'per-model fallback returned nothing — sign in to list creators</span></div>';
      return;
    }
    btn.disabled = true;
    const list = accts.map((a) => ({
      id: String(a.id), name: a.nickname || String(a.id), state: "pending", n: 0, msg: "",
    }));
    paintRoster(list);
    rostCount.textContent = "scanning " + list.length + "…";
    for (const r of list) {
      try {
        const opts = { noAccount: true, headers: { "X-Account-Id": r.id } };
        const out = await Fastt.get("/api/of/v2/scheduled-sends", null, opts);
        let n = ((out && out.list) || []).length;
        // Add the OF-scheduled queue (same primary source as the table). Best
        // effort: if /schedules errors we still report the relay-job count.
        try {
          const q = await Fastt.get("/api/of/v2/schedules", schedRange(), opts);
          n += ((q && q.list) || []).filter((it) => it && it.type === "chat").length;
        } catch (e2) { /* keep relay-job count */ }
        r.state = "ok";
        r.n = n;
      } catch (e) {
        const detail = String((e && e.body && e.body.detail) || "");
        if (e && e.status === 404 && /Unknown account_id/i.test(detail)) r.state = "nosession";
        else { r.state = "err"; r.msg = (detail || (e && e.message) || "error").slice(0, 60); }
      }
      paintRoster(list);
    }
    const okRows = list.filter((r) => r.state === "ok");
    const total = okRows.reduce((s, r) => s + r.n, 0);
    rostCount.textContent = total + " queued across " + okRows.length + "/" + list.length + " creators";
    btn.disabled = false;
  });
  rostRows.innerHTML = '<div class="rrow"><span class="rn">'
    + esc(Fastt.accounts().length ? Fastt.accounts().length + " creators on the roster" : "Roster unavailable")
    + '</span><span class="rv na">not scanned yet — hit “Scan all creators”</span></div>';

  $("#schedRows").addEventListener("click", async (e) => {
    if (e.target.closest(".btn-resched")) {
      Fastt.toast("No reschedule endpoint yet — cancel it, then schedule a new send from the composer");
      return;
    }
    const btn = e.target.closest(".btn-cancel");
    if (!btn) return;
    const kind = btn.dataset.kind, id = btn.dataset.id;
    if (!id || id === "undefined" || id === "null") {
      Fastt.toast("That row carries no cancellable id — cancel it from OF directly", "err");
      return;
    }
    const q = kind === "mass"
      ? "Cancel this scheduled MASS blast? It won't go out."
      : "Cancel this scheduled send? It won't go out.";
    if (!confirm(q)) return;
    try {
      if (kind === "mass") {
        await Fastt.del("/api/of/v2/messages/queue/" + id);
      } else {
        const out = await Fastt.del("/api/of/v2/scheduled-sends/" + id);
        if (out && out.cancelled === false) {
          Fastt.toast("Too late — it's already going out", "err");
          await load();
          return;
        }
      }
      Fastt.saved("Cancelled ✓");
      await load();
    } catch (e2) { Fastt.oops(e2); }
  });
});
