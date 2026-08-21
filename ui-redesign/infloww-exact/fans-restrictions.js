/* ==== fastt wiring: automation-restrict + of-restricted lists (server.py) ==== */

/* Runs at parse time, BEFORE the first fetch can fail: strip the 15 mockup fans
   and every baked counter, so a dead relay leaves an honest empty page instead
   of invented names with Lift buttons next to them. */
(function () {
  var body = document.getElementById("rx-rows");
  if (body) {
    body.innerHTML = '<tr data-type="loading"><td colspan="4" style="text-align:center;'
      + 'color:var(--muted);padding:22px 10px;font-size:13px">Loading the skip list…</td></tr>';
  }
  // manual + of_restricted have endpoints → they resolve to a number below.
  // muted + unreachable have NONE (checked: openapi has no route that lists or
  // counts skip_list by those reasons) → their tiles keep a static em-dash with a
  // "Not tracked yet" caption + NO API YET badge, so they read as absent-by-design,
  // not as a failed/loading metric next to the real numbers.
  ["rx-n-manual", "rx-n-ofr"].forEach(function (id) {
    var el = document.getElementById(id);
    if (el) el.textContent = "—";
  });
  var LBL = { all: "All", manual: "Manual restrict", muted: "Muted creator",
              ofr: "OF restricted", unr: "Unreachable" };
  var NO_API = { muted: 1, unr: 1 };
  Array.prototype.forEach.call(document.querySelectorAll("#rx-filter .fx-chip"), function (el) {
    var k = el.getAttribute("data-f");
    el.textContent = (LBL[k] || k) + " · " + (NO_API[k] ? "n/a" : "…");
  });
  var liftAll = document.getElementById("rx-liftall");
  if (liftAll) liftAll.disabled = true;
  // Badge the two no-endpoint tiles NOW, not after a fetch — their honesty must
  // survive a dead relay (the loader below returns early when it can't reach it).
  if (window.Fastt) {
    Fastt.staticBadge(document.getElementById("rx-stat-muted"), "NO API YET");
    Fastt.staticBadge(document.getElementById("rx-stat-unr"), "NO API YET");
  }
})();

Fastt.ready(async function () {
  "use strict";
  var $ = Fastt.$, esc = Fastt.esc;
  var GRADS = ["g1", "g2", "g3", "g4", "g5"];

  // ── header-right creator scope (was a dead "Aria" mockup on a Ava page) ──
  // Same roster the topbar switcher uses: /admin/accounts, falling back to
  // /admin/stats/per-model when the caller is unauthed (see fastt.js).
  (function mountScope() {
    var box = $("#rx-acct"), lbl = $("#rx-acct-lbl");
    if (!box || !lbl) return;
    function nameOf(a) { return (a && (a.nickname || a.display_name)) || (a && String(a.id)) || ""; }
    var roster = Fastt.accounts() || [];
    var cur = Fastt.accountRow();
    lbl.textContent = nameOf(cur) || Fastt.account() || "No creator";
    box.title = "Scoped to " + (nameOf(cur) || Fastt.account() || "nothing")
      + " — everything on this page is that creator's skip list";
    box.addEventListener("click", function (e) {
      e.stopPropagation();
      Fastt.$$(".ft-acct-menu").forEach(function (m) { m.remove(); });
      var menu = document.createElement("div");
      menu.className = "ft-acct-menu";
      var r = box.getBoundingClientRect();
      menu.style.left = Math.max(8, r.right - 220) + "px";
      menu.style.top = (r.bottom + 6) + "px";
      menu.innerHTML = roster.map(function (a) {
        return '<div class="row ' + (String(a.id) === String(Fastt.account()) ? "on" : "")
          + '" data-id="' + esc(a.id) + '"><span class="dot"></span><span>' + esc(nameOf(a)) + "</span></div>";
      }).join("") + '<div class="row signin" data-act="signin">'
        + (roster.length ? "Switch user…" : "Sign in to list creators…") + "</div>";
      document.body.appendChild(menu);
      var close = function () { menu.remove(); document.removeEventListener("click", close); };
      setTimeout(function () { document.addEventListener("click", close); }, 0);
      menu.addEventListener("click", function (ev) {
        var row = ev.target.closest(".row");
        if (!row) return;
        close();
        if (row.dataset.act === "signin") {
          Fastt.signInModal().then(function (ok) { if (ok) location.reload(); });
          return;
        }
        if (row.dataset.id && String(row.dataset.id) !== String(Fastt.account())) {
          Fastt.setAccount(row.dataset.id); // fastt.js reloads the page on switch
        }
      });
    });
  })();

  function initials(r) {
    var s = (r.of_display_name || r.of_username || String(r.fan_id)).trim();
    var parts = s.split(/\s+/);
    var out = (parts[0] ? parts[0][0] : "") + (parts[1] ? parts[1][0] : "");
    return (out || s.slice(0, 2)).toUpperCase();
  }
  function since(iso) {
    var d = Fastt.parseUtc(iso);   // relay timestamps are UTC; don't read them as local
    return d ? d.toLocaleDateString("en-US", { month: "short", day: "numeric" }) : "—";
  }

  var state = { rows: [], manual: 0, ofr: 0, filter: "all" };

  function rowHtml(r) {
    var grad = GRADS[Math.abs(Number(r.fan_id)) % GRADS.length];
    var name = esc(r.of_display_name || r.of_username || "fan " + r.fan_id);
    var handle = (r.of_username ? "@" + esc(r.of_username) + " · " : "") + "id " + esc(r.fan_id);
    var badge, why, act;
    if (r.type === "manual") {
      badge = '<span class="rx-b manual"><i></i>Manual restrict</span>';
      why = "Restricted from the chat ⋯ menu";
      act = '<button class="fx-btn ghost rx-lift" data-fan="' + esc(r.fan_id) + '" data-type="manual" title="Resume automations for this fan">Lift</button>';
    } else {
      badge = '<span class="rx-b ofr"><i></i>OF restricted</span>';
      why = "Restricted on OnlyFans — messages not delivered, off all unread counts";
      act = '<button class="fx-btn ghost rx-lift" data-fan="' + esc(r.fan_id) + '" data-type="ofr">Lift</button>'
          + '<div class="rx-hint">Un-restricts on OnlyFans itself —<br>messages + counts resume</div>';
    }
    return '<tr data-type="' + r.type + '">'
      + '<td><div class="rx-fan"><span class="rx-av ' + grad + '">' + esc(initials(r)) + '</span>'
      + '<div><div class="rx-name">' + name + '</div><div class="rx-handle">' + handle + '</div></div></div></td>'
      + '<td>' + badge + '<div class="rx-why">' + why + '</div></td>'
      + '<td><span class="rx-since">' + esc(since(r.added_at)) + '</span></td>'
      + '<td class="rx-act">' + act + '</td></tr>';
  }

  function emptyHtml(msg) {
    return '<tr data-type="empty"><td colspan="4" style="text-align:center;color:var(--muted);padding:22px 10px;font-size:13px">'
      + esc(msg) + "</td></tr>";
  }

  function paint() {
    var f = state.filter;
    var visible = state.rows.filter(function (r) { return f === "all" || r.type === f; });
    var body;
    if (f === "muted") {
      body = emptyHtml("No relay endpoint lists muted-creator skips. They are real and still gate every send server-side "
        + "(should_skip_muted_creator) — this page just can't enumerate them. Un-mute the chat on OF to free one.");
    } else if (f === "unr") {
      body = emptyHtml("No relay endpoint lists unreachable skips. A bounced send writes skip_list('unreachable') + a 7-day "
        + "pause server-side — this page can't enumerate them, and there is no lift path for them.");
    } else if (!visible.length) {
      body = emptyHtml(f === "all" ? "No restricted fans — nobody is being skipped." : "None right now.");
    } else {
      body = visible.map(rowHtml).join("");
    }
    $("#rx-rows").innerHTML = body;
    var total = state.rows.length;
    $("#rx-count").textContent = total + (total === 1 ? " fan" : " fans") + " — every automation skips them";
    $("#rx-n-manual").textContent = state.manual;
    $("#rx-n-ofr").textContent = state.ofr;
    $("#rx-liftall-txt").textContent = "Lift all manual restricts (" + state.manual + ")";
    $("#rx-liftall").disabled = !state.manual;
    // chip counts — muted/unr have no endpoint, so they say n/a forever
    var chips = { all: total, manual: state.manual, ofr: state.ofr };
    Fastt.$$("#rx-filter .fx-chip").forEach(function (c) {
      var key = c.dataset.f;
      var label = { all: "All", manual: "Manual restrict", muted: "Muted creator", ofr: "OF restricted", unr: "Unreachable" }[key];
      c.textContent = label + " · " + (key in chips ? chips[key] : "n/a");
      if (!(key in chips)) c.title = "No relay endpoint exposes this reason — picking it shows why, not a fan list.";
    });
  }

  async function load() {
    var res = await Promise.all([
      Fastt.get("/api/of/v2/automation-restrict"),
      Fastt.get("/api/of/v2/of-restricted"),
    ]);
    var man = res[0], ofr = res[1];
    state.manual = man.count || 0;
    state.ofr = ofr.count || 0;
    state.rows = (man.list || []).map(function (r) { return Object.assign({ type: "manual" }, r); })
      .concat((ofr.list || []).map(function (r) { return Object.assign({ type: "ofr" }, r); }))
      .sort(function (a, b) { return String(b.added_at || "").localeCompare(String(a.added_at || "")); });
    paint();
  }
  try {
    await load();
  } catch (e) {
    // Bootstrap failed: say so where the fans would have been, keep every
    // counter at "—", and stop — no LIVE badges, no handlers on absent data.
    $("#rx-rows").innerHTML = emptyHtml(
      "Couldn't reach the relay — the skip list is unknown. Nothing is shown because nothing was loaded.");
    $("#rx-count").textContent = "unavailable";
    $("#rx-liftall-txt").textContent = "Lift all manual restricts";
    Fastt.staticBadge($("#rx-count").parentElement.querySelector(".fx-card-h"), "NO DATA — RELAY UNREACHABLE");
    Fastt.oops(e);
    return;
  }

  Fastt.liveBadge($("#rx-n-manual").closest(".rx-stat"));
  Fastt.liveBadge($("#rx-n-ofr").closest(".rx-stat"));
  Fastt.liveBadge($("#rx-count").parentElement.querySelector(".fx-card-h"));
  Fastt.staticBadge($("#rx-stat-muted"), "NO API YET");
  Fastt.staticBadge($("#rx-stat-unr"), "NO API YET");

  // filter chips (fx-kit paints the selection; we just re-filter)
  $("#rx-filter").addEventListener("click", function (e) {
    var c = e.target.closest(".fx-chip");
    if (!c) return;
    state.filter = c.dataset.f || "all";
    paint();
  });

  // lift one (explicit click + confirm — both mutate real state)
  $("#rx-rows").addEventListener("click", function (e) {
    var btn = e.target.closest(".rx-lift");
    if (!btn) return;
    var fan = btn.dataset.fan;
    if (btn.dataset.type === "manual") {
      if (!confirm("Lift the manual restriction for fan " + fan + "? Automations may message them again.")) return;
      btn.disabled = true;
      Fastt.del("/api/of/v2/automation-restrict/" + fan)
        .then(function () { Fastt.saved("Restriction lifted"); return load(); })
        .catch(function (err) { btn.disabled = false; Fastt.oops(err); });
    } else if (btn.dataset.type === "ofr") {
      if (!confirm("Un-restrict fan " + fan + " ON ONLYFANS? Their messages start delivering again and automations may target them.")) return;
      btn.disabled = true;
      Fastt.del("/api/of/v2/users/" + fan + "/restrict")
        .then(function () { Fastt.saved("OF restriction lifted"); return load(); })
        .catch(function (err) { btn.disabled = false; Fastt.oops(err); });
    }
  });

  // lift ALL manual restricts
  $("#rx-liftall").addEventListener("click", function () {
    if (!state.manual) return;
    if (!confirm("Lift ALL " + state.manual + " manual restrictions? Every one of these fans becomes automatable again.")) return;
    Fastt.del("/api/of/v2/automation-restrict")
      .then(function (out) { Fastt.saved("Cleared " + ((out && out.cleared) || 0) + " restrictions"); return load(); })
      .catch(function (err) { Fastt.oops(err); });
  });

  // Advanced quarantine knobs: the 7-day pause is fixed in code (skip_unreachable_fan).
  var quar = $("#rx-quar-group");
  Fastt.staticBadge(quar.querySelector("h4"), "FIXED IN CODE");
  Fastt.$$("input, select", quar).forEach(function (el) { el.disabled = true; });
  Fastt.$$(".fx-check", quar).forEach(function (el) { el.style.pointerEvents = "none"; });
});
