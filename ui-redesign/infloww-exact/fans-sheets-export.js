/* ==== fastt wiring: push_to_sheets rule (service/automations/push_to_sheets.py) ==== */
Fastt.ready(async function () {
  "use strict";
  var $ = Fastt.$;
  function utc(s) { return (s && !/[zZ]|[+-]\d{2}:?\d{2}$/.test(s)) ? s + "Z" : s; }
  function chip(el, text, cls) {
    if (!el) return;
    el.className = "fx-st" + (cls ? " " + cls : "");
    el.innerHTML = "<i></i>" + Fastt.esc(text);
  }

  var byKind = await Fastt.rulesByKind();
  var rule = (byKind.push_to_sheets || [])[0] || null;
  var lr = rule && rule.last_run;
  var st = (lr && lr.stats) || null;
  var on = !!(rule && rule.is_enabled);
  var pl0 = (rule && rule.payload) || {};

  // ── status strip (honest: we can only see the last run, not the token) ──
  chip($("#gs-st-status"),
    lr ? (lr.status === "ok" ? "Last export clean" : "Last export failed") : "Never exported",
    lr ? (lr.status === "ok" ? "ok" : "err") : "");
  chip($("#gs-st-last"), "Last export " + Fastt.fmtAgo(utc(lr && (lr.completed_at || lr.started_at))));
  chip($("#gs-st-rows"), (st && st.rows_written != null ? Fastt.fmtInt(st.rows_written) : "0") + " rows written");
  var nextTxt = "Auto-export off";
  if (on && rule.next_due_at) {
    var due = Fastt.parseUtc(rule.next_due_at);
    var mins = Math.max(0, Math.round(((due ? due.getTime() : Date.now()) - Date.now()) / 60000));
    nextTxt = "Next export in " + (mins >= 60 ? Math.round(mins / 60) + " h" : mins + " min");
  } else if (on) {
    nextTxt = "Next export on the next tick";
  }
  chip($("#gs-st-next"), nextTxt);

  // ── cadence + effective tab, interpolated into every sentence that used to
  //    hardcode "Once an hour" / "the Main tab". Both drift with the rule row. ──
  function everyTxt(sec) {
    if (!sec) return "On demand";
    if (sec % 86400 === 0) { var d = sec / 86400; return d === 1 ? "Once a day" : "Every " + d + " days"; }
    if (sec % 3600 === 0) { var h = sec / 3600; return h === 1 ? "Once an hour" : "Every " + h + " hours"; }
    var m = Math.round(sec / 60);
    return m === 1 ? "Once a minute" : "Every " + m + " minutes";
  }
  function repaintCopy() {
    var secs = (rule && rule.every_seconds) || 0;
    var tab = (rule && rule.payload && rule.payload.sheet_tab)
      || (st && st.sheet_tab) || "Main";
    $("#gs-tc-desc").textContent = everyTxt(secs)
      + ", one row per profiled fan lands in your sheet — kept fresh automatically, "
      + "so chatters can skim who's who without opening fastt.";
    $("#gs-warn").innerHTML = "Each export <b>clears and rewrites</b> the <b>"
      + Fastt.esc(tab) + "</b> tab — keep your own columns in a different tab.";
    $("#gs-sub-cadence").innerHTML = everyTxt(secs) + " into tab <b style=\"color:#cfcfcf\">"
      + Fastt.esc(tab) + "</b>"
      + (rule && rule.is_enabled ? "." : ", when auto-export is on.");
    return tab;
  }
  repaintCopy();

  // ── toggle card = push_to_sheets.is_enabled ─────────────────
  var card = $("#gs-card"), sw = $("#gs-switch"), state = $("#gs-state");
  function paintCard(o) {
    card.classList.toggle("on", o);
    sw.classList.toggle("on", o);
    state.textContent = o ? "Running" : "Off";
  }
  paintCard(on);
  Fastt.liveBadge(card.querySelector(".fx-tc-title"));

  sw.addEventListener("click", function () {
    var want = !sw.classList.contains("on");
    Fastt.upsertRule("push_to_sheets", { is_enabled: want },
      { every_seconds: 86400, is_enabled: false })
      .then(function () {
        if (rule) rule.is_enabled = want;
        Fastt.saved(want ? "Sheet export ON" : "Sheet export OFF");
        setTimeout(function () { paintCard(want); }, 0);
      })
      .catch(function (e) { Fastt.oops(e); setTimeout(function () { paintCard(!want); }, 0); });
  });

  // ── destination line + kv from the observed config/run ──────
  // Newest run that actually recorded a destination. The LAST run may have died
  // before writing (no stats blob at all), and losing the sheet id then is
  // exactly when an operator needs it most — so keep the last known-good one.
  var histDest = { id: "", tab: "" };
  function destId() { return pl0.spreadsheet_id || (st && st.spreadsheet_id) || histDest.id || ""; }
  function destTab() { return pl0.sheet_tab || (st && st.sheet_tab) || histDest.tab || "Main"; }

  // Never surface a raw backend error verbatim — it can leak absolute host
  // paths / env-var names. Map the known auth failure to a friendly line and
  // strip any filesystem path from anything else.
  function cleanErr(raw) {
    var s = String(raw || "failed");
    if (/token|GOOGLE_SHEETS|auth|credential/i.test(s)) return "Google Sheets not connected — connect it in Advanced";
    return s.replace(/\/[\w./~-]+/g, "…").replace(/\s+/g, " ").trim().slice(0, 80);
  }

  function repaintKv() {
    var id = destId();
    var tab = destTab();
    $("#gs-dest-name").textContent = id ? (id.length > 14 ? id.slice(0, 14) + "…" : id) : "default sheet";
    $("#gs-dest-name").title = id
      ? id + (pl0.spreadsheet_id ? " (configured)" : " (from the last run that wrote)")
      : "No spreadsheet id configured — push_to_sheets falls back to its default sheet";
    $("#gs-dest-tab").textContent = tab;
    $("#gs-kv").innerHTML = lr
      ? "last export <b>" + Fastt.esc(Fastt.fmtAgo(utc(lr.completed_at || lr.started_at))) + "</b> · <b>"
        + (st && st.rows_written != null ? Fastt.fmtInt(st.rows_written) : "0") + "</b> rows · "
        + (lr.status === "ok"
            ? (st && st.updated_cells != null ? Fastt.fmtInt(st.updated_cells) + " cells · " : "") + "finished clean"
            : "FAILED: " + Fastt.esc(cleanErr(lr.error_text || lr.status)))
      : "no export has run yet";
  }
  repaintKv();

  $("#gs-open").addEventListener("click", function () {
    var id = destId();
    if (!id) { Fastt.toast("No spreadsheet id known yet — run one export or set one in Advanced", "err"); return; }
    window.open("https://docs.google.com/spreadsheets/d/" + encodeURIComponent(id), "_blank");
  });

  // ── Export now (explicit click + confirm) ───────────────────
  // Safe by construction: push_to_sheets READS our DB and writes ONE Google
  // Sheet. It makes no OnlyFans call and messages nobody — see
  // service/automations/push_to_sheets.py. Rule id comes from the loaded row.
  var runBtn = $("#gs-runnow"), runTxt = $("#gs-runnow-txt");
  if (!rule) {
    runBtn.disabled = true;
    runBtn.title = "No push_to_sheets rule exists for this creator yet — flip the toggle once to create it.";
  }
  runBtn.addEventListener("click", function () {
    if (!rule) return;
    if (!confirm("Run the sheet export now?\n\nIt rewrites the \"" + repaintCopy()
      + "\" tab of the destination spreadsheet with every profiled fan.\n"
      + "It reads our database only — no OnlyFans call, no message to any fan.")) return;
    runBtn.disabled = true; runTxt.textContent = "Exporting…";
    Fastt.post("/admin/automation-rules/" + rule.id + "/run-now")
      .then(function () {
        Fastt.saved("Export queued — repainting when it lands");
        return pollAfterRun(rule.last_run && rule.last_run.started_at);
      })
      .catch(function (e) { Fastt.oops(e); })
      .then(function () { runBtn.disabled = false; runTxt.textContent = "Export now"; });
  });

  /** Re-poll the rule until last_run moves on (or we give up), then repaint the
   *  status strip, the kv line and the history — never a fake optimistic row. */
  function pollAfterRun(prevStarted) {
    var tries = 0;
    return new Promise(function (resolve) {
      (function tick() {
        tries++;
        Fastt.rulesByKind().then(function (m) {
          var fresh = (m.push_to_sheets || [])[0] || null;
          var moved = fresh && fresh.last_run
            && String(fresh.last_run.started_at) !== String(prevStarted);
          if (moved || tries >= 12) {
            if (fresh) { rule = fresh; lr = fresh.last_run; st = (lr && lr.stats) || null; pl0 = fresh.payload || {}; }
            repaintStrip(); repaintKv(); repaintCopy();
            loadHistory();
            if (!moved) Fastt.toast("Still running — the history refreshes on reload", "");
            resolve();
          } else { setTimeout(tick, 2500); }
        }).catch(function () { resolve(); });
      })();
    });
  }

  function repaintStrip() {
    chip($("#gs-st-status"),
      lr ? (lr.status === "ok" ? "Last export clean" : "Last export failed") : "Never exported",
      lr ? (lr.status === "ok" ? "ok" : "err") : "");
    chip($("#gs-st-last"), "Last export " + Fastt.fmtAgo(utc(lr && (lr.completed_at || lr.started_at))));
    chip($("#gs-st-rows"), (st && st.rows_written != null ? Fastt.fmtInt(st.rows_written) : "0") + " rows written");
  }

  // ── advanced knobs → rule payload / cadence ──────────────────
  Fastt.liveBadge($("#adv-sheets").previousElementSibling);
  // Resolves TRUE only when the PATCH landed. It used to swallow the error and
  // resolve regardless, so callers repainted to the NEW value after a FAILED
  // save. Every caller below now reverts to the last known-saved value on false.
  function savePayload(mut, msg) {
    var pl = Object.assign({}, (rule && rule.payload) || {});
    mut(pl);
    return Fastt.upsertRule("push_to_sheets", { payload: pl },
      { every_seconds: 86400, is_enabled: false })
      .then(function () { if (rule) rule.payload = pl; pl0 = pl; Fastt.saved(msg); return true; })
      .catch(function (e) { Fastt.oops(e); return false; });
  }

  var sheetIn = $("#gs-spreadsheet");
  sheetIn.value = pl0.spreadsheet_id || "";
  sheetIn.addEventListener("change", function () {
    var v = sheetIn.value.trim();
    var m = v.match(/\/d\/([A-Za-z0-9_-]{20,})/); // accept a pasted full URL
    if (m) { v = m[1]; sheetIn.value = v; }
    savePayload(function (pl) {
      if (v) pl.spreadsheet_id = v; else delete pl.spreadsheet_id;
    }, v ? "Spreadsheet saved" : "Cleared — using the default shared sheet")
      .then(function (ok) {
        if (!ok) { sheetIn.value = pl0.spreadsheet_id || ""; return; }
        repaintKv();
      });
  });

  var tabIn = $("#gs-tab");
  tabIn.value = pl0.sheet_tab || "";
  if (!tabIn.value) tabIn.placeholder = "Main (default)";
  tabIn.addEventListener("change", function () {
    var v = tabIn.value.trim();
    savePayload(function (pl) {
      if (v) pl.sheet_tab = v; else delete pl.sheet_tab;
    }, v ? "Tab saved" : "Cleared — default tab 'Main'")
      .then(function (ok) {
        if (!ok) { tabIn.value = pl0.sheet_tab || ""; return; }
        repaintCopy(); repaintKv();   // the warning names the tab — keep it true
      });
  });

  var limitIn = $("#gs-limit");
  limitIn.value = pl0.limit || 5000;
  limitIn.addEventListener("change", function () {
    var n = parseInt(String(limitIn.value).replace(/[^\d]/g, ""), 10);
    if (!n || n < 1) { Fastt.toast("Enter a number ≥ 1", "err"); limitIn.value = pl0.limit || 5000; return; }
    limitIn.value = n;
    savePayload(function (pl) { pl.limit = n; }, "Row limit saved")
      .then(function (ok) { if (!ok) limitIn.value = pl0.limit || 5000; });
  });

  var createCk = $("#gs-create");
  createCk.classList.toggle("on", pl0.create_tab !== false);
  createCk.addEventListener("click", function () {
    var want = !createCk.classList.contains("on");
    savePayload(function (pl) { pl.create_tab = want; }, want ? "Will create a missing tab" : "Won't create missing tabs")
      // fx-kit already flipped the box on click — re-affirm on success, undo on failure.
      .then(function (ok) { setTimeout(function () { createCk.classList.toggle("on", ok ? want : !want); }, 0); });
  });

  var everySel = $("#gs-every");
  var cur = rule && rule.every_seconds;
  if (cur && Array.prototype.some.call(everySel.options, function (o) { return +o.value === cur; })) {
    everySel.value = String(cur);
  } else if (cur) {
    var o = document.createElement("option");
    o.value = String(cur); o.textContent = "Every " + Math.round(cur / 3600) + " h (current)";
    everySel.appendChild(o); everySel.value = String(cur);
  }
  everySel.addEventListener("change", function () {
    var n = parseInt(everySel.value, 10);
    Fastt.upsertRule("push_to_sheets", { every_seconds: n },
      { every_seconds: n, is_enabled: false })
      .then(function () { if (rule) rule.every_seconds = n; Fastt.saved("Schedule saved"); repaintCopy(); })
      .catch(function (e) { Fastt.oops(e); });
  });

  // ── EXPORT HISTORY: the full automation_runs trail, not just the last run ──
  var histCard = $("#gs-hist-card");
  // A run that died before writing has NO stats blob. Say "n/a" (it never
  // recorded one), not "—", which reads as a value still loading.
  var NA = '<span style="color:var(--muted2)">n/a</span>';
  function fmtDur(ms) {
    if (ms == null) return NA;
    return ms < 1000 ? ms + " ms" : (ms / 1000).toFixed(1) + " s";
  }
  function histEmpty(msg) {
    $("#gs-hist-body").innerHTML =
      '<div style="font-size:13px;color:var(--muted);padding:20px 0 4px">' + Fastt.esc(msg) + "</div>";
  }
  async function loadHistory() {
    var out;
    try {
      out = await Fastt.get("/admin/stats/automation-runs", { kind: "push_to_sheets", limit: 20 });
    } catch (e) {
      $("#gs-hist-count").textContent = "unavailable";
      histEmpty("Couldn't reach the relay — the run log is unknown.");
      Fastt.staticBadge(histCard.querySelector(".fx-card-h"), "NO DATA — RELAY UNREACHABLE");
      return;
    }
    var runs = (out.runs || []).map(function (r) {
      var s = {};
      try { s = r.stats_json ? JSON.parse(r.stats_json) : (r.stats || {}); } catch (e) { s = {}; }
      return {
        id: r.id, started: r.started_at, done: r.completed_at, status: r.status,
        err: r.error_text, rows: s.rows_written, seen: s.profiles_seen,
        cells: s.updated_cells, urows: s.updated_rows, tab: s.sheet_tab,
        created: s.created_tab, ms: s.duration_ms, sid: s.spreadsheet_id,
      };
    });
    var lastWrote = runs.find(function (r) { return r.sid; });
    if (lastWrote) {
      histDest = { id: lastWrote.sid, tab: lastWrote.tab || "" };
      repaintKv();   // the destination survives a run that died before writing
    }
    if (!runs.length) {
      $("#gs-hist-count").textContent = "no runs yet";
      histEmpty("push_to_sheets has never run for this creator — nothing to chart yet.");
      Fastt.staticBadge(histCard.querySelector(".fx-card-h"), "NO RUNS YET");
      return;
    }
    Fastt.liveBadge(histCard.querySelector(".fx-card-h"));
    $("#gs-hist-count").textContent = runs.length + (runs.length === 1 ? " run" : " runs")
      + " · newest " + Fastt.fmtAgo(utc(runs[0].done || runs[0].started));

    // Bar chart: rows_written per run, oldest → newest, scaled off the data.
    var chron = runs.slice().reverse();
    var max = chron.reduce(function (m, r) { return Math.max(m, Number(r.rows) || 0); }, 0);
    var chart, xaxis;
    if (max > 0) {
      chart = '<div class="gsh-y">peak ' + Fastt.fmtInt(max) + ' rows</div><div class="gsh-chart">'
        + chron.map(function (r) {
            var v = Number(r.rows) || 0;
            var pct = max ? Math.max(3, Math.round((v / max) * 100)) : 3;
            var bad = r.status !== "ok";
            return '<div class="gsh-col" title="' + Fastt.esc(Fastt.fmtDate(utc(r.started)))
              + " · " + Fastt.fmtInt(v) + ' rows' + (bad ? " · FAILED" : "") + '">'
              + '<span class="gsh-bv">' + (v ? Fastt.fmtInt(v) : "0") + "</span>"
              + '<div class="gsh-bar' + (bad ? " err" : "") + '" style="height:' + pct + '%"></div>'
              + "</div>";
          }).join("")
        + "</div>";
      // Label per DAY: first column of each date shows "Jul 24"; further runs
      // that share that day show the run time instead, so the axis never repeats
      // an identical date back-to-back.
      var prevDay = null;
      xaxis = '<div class="gsh-x">' + chron.map(function (r) {
        var d = Fastt.parseUtc(utc(r.started));
        if (!d) { prevDay = null; return '<div class="gsh-xl">—</div>'; }
        var dayKey = d.toLocaleDateString("en-US", { month: "short", day: "numeric" });
        var label = dayKey;
        if (dayKey === prevDay) {
          label = d.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" });
        }
        prevDay = dayKey;
        return '<div class="gsh-xl">' + Fastt.esc(label) + "</div>";
      }).join("") + "</div>";
    } else {
      chart = '<div class="fx-note warn" style="margin-top:14px"><span>Every recorded run wrote <b>0</b> rows — '
        + "nothing to plot. That usually means no fan profiles existed yet, or the export failed before writing.</span></div>";
      xaxis = "";
    }

    var table = '<table class="gsh-table"><thead><tr>'
      + "<th>When</th><th>Status</th><th class=\"num\">Profiles seen</th><th class=\"num\">Rows written</th>"
      + "<th class=\"num\">Cells</th><th>Tab</th><th class=\"num\">Took</th></tr></thead><tbody>"
      + runs.slice(0, 10).map(function (r) {
          var ok = r.status === "ok";
          return "<tr><td>" + Fastt.esc(Fastt.fmtDate(utc(r.started))) + "</td>"
            + '<td style="color:' + (ok ? "var(--green)" : "#f2295b") + '">'
            + Fastt.esc(ok ? "ok" : (r.err ? String(r.err).slice(0, 40) : r.status)) + "</td>"
            + '<td class="num">' + (r.seen != null ? Fastt.fmtInt(r.seen) : NA) + "</td>"
            + '<td class="num">' + (r.rows != null ? Fastt.fmtInt(r.rows) : NA) + "</td>"
            + '<td class="num">' + (r.cells != null ? Fastt.fmtInt(r.cells) : NA) + "</td>"
            + "<td>" + (r.tab ? Fastt.esc(r.tab) : NA) + (r.created ? " <span class=\"pill-note ok\">created</span>" : "") + "</td>"
            + '<td class="num">' + fmtDur(r.ms) + "</td></tr>";
        }).join("")
      + "</tbody></table>";

    $("#gs-hist-body").innerHTML = chart + xaxis + table
      + (runs.length > 10 ? '<div class="gs-more">showing the last 10 of ' + runs.length + " runs</div>" : "");
  }

  // ── WHAT LANDS IN THE SHEET: the real 249 rows, real header ─────────────
  // Same endpoint the export builder feeds from, so the columns can never drift
  // from push_to_sheets._HEADER. total_spend is DOLLARS (float) → fmtMoney;
  // last_updated is tz-naive UTC → parseUtc/fmtAgo.
  var prevCard = $(".sheet-prev").closest(".fx-card");
  var prevTitle = $("#gs-prev-title");
  var PAGE = 10, shown = PAGE, fanRows = [], fanCols = [], gsQuery = "";
  // Same fields the real app's User-data search covers (name/nickname/bio/Q1-3),
  // plus the ids so an operator can paste a fan_id straight in.
  function matchRow(r) {
    if (!gsQuery) return true;
    var hay = [r.fan_name, r.nickname, r.short_bio, r.bullet_points, r.q1, r.q2, r.q3, r.fan_id, r.chat_id];
    for (var i = 0; i < hay.length; i++) {
      if (hay[i] != null && String(hay[i]).toLowerCase().indexOf(gsQuery) >= 0) return true;
    }
    return false;
  }
  function viewRows() { return gsQuery ? fanRows.filter(matchRow) : fanRows; }
  var WRAP = { short_bio: 1, bullet_points: 1, q1: 1, q2: 1, q3: 1, tease1: 1, tease2: 1, tease3: 1, nickname: 1 };
  var NUMC = { total_spend: 1, message_count: 1, fan_id: 1, chat_id: 1 };

  function cellHtml(col, r) {
    var key = String(col).toLowerCase();      // header "Q1" → row key "q1"
    var v = r[key];
    if (key === "total_spend") return '<td class="num">' + Fastt.fmtMoney(v) + "</td>";
    if (key === "message_count") return '<td class="num">' + Fastt.fmtInt(v) + "</td>";
    if (key === "fan_id" || key === "chat_id") return '<td class="num">' + Fastt.esc(v == null ? "—" : v) + "</td>";
    if (key === "last_updated") {
      return "<td>" + (v ? '<span title="' + Fastt.esc(Fastt.fmtDate(v)) + '">' + Fastt.esc(Fastt.fmtAgo(v)) + "</span>" : "") + "</td>";
    }
    if (key === "note_on_of") {
      var added = String(v || "").toLowerCase() === "added";
      return '<td><span class="pill-note ' + (added ? "ok" : "no") + '">' + Fastt.esc(v || "not added") + "</span></td>";
    }
    // A missing value is written to the sheet as an EMPTY cell — render it empty
    // here too, rather than an em-dash that would read as "still loading".
    var txt = v == null ? "" : String(v);
    var html = Fastt.esc(txt).replace(/\n/g, "<br>");
    if (WRAP[key]) {
      return '<td class="wrapcell"><div class="clamp" title="' + Fastt.esc(txt) + '">' + html + "</div></td>";
    }
    return "<td>" + html + "</td>";
  }
  function paintPreview() {
    $("#gs-prev-head").innerHTML = fanCols.map(function (c) {
      return '<th class="' + (NUMC[String(c).toLowerCase()] ? "num" : "") + '">' + Fastt.esc(c) + "</th>";
    }).join("");
    var view = viewRows();
    if (!view.length) {   // a search that matched nobody — honest empty, not a blank table
      $("#gs-prev-rows").innerHTML = '<tr><td class="gs-nomatch" colspan="' + fanCols.length + '">'
        + "No profiled fan matches “" + Fastt.esc(gsQuery) + "”. "
        + Fastt.fmtInt(fanRows.length) + " fans go into the sheet in total.</td></tr>";
      $("#gs-prev-count").textContent = "0 of " + Fastt.fmtInt(fanRows.length) + " rows match";
      $("#gs-prev-more").style.display = "none";
      return;
    }
    $("#gs-prev-rows").innerHTML = view.slice(0, shown).map(function (r) {
      return "<tr>" + fanCols.map(function (c) { return cellHtml(c, r); }).join("") + "</tr>";
    }).join("");
    $("#gs-prev-count").textContent = gsQuery
      ? "Showing " + Math.min(shown, view.length) + " of " + Fastt.fmtInt(view.length)
        + " matching · " + Fastt.fmtInt(fanRows.length) + " fans total"
      : "Showing " + Math.min(shown, fanRows.length) + " of " + Fastt.fmtInt(fanRows.length)
        + " rows · " + fanCols.length + " columns, exactly as the sheet gets them";
    $("#gs-prev-more").style.display = shown < view.length ? "" : "none";
  }
  async function loadPreview() {
    var data;
    try {
      data = await Fastt.get("/admin/stats/per-fan-data");
    } catch (e) {
      $("#gs-prev-head").innerHTML = "<th>Fan data</th>";
      $("#gs-prev-rows").innerHTML = '<tr><td style="color:var(--muted);padding:18px 12px">'
        + "Couldn't reach the relay — no rows to show. Nothing here is invented.</td></tr>";
      $("#gs-prev-count").textContent = "unavailable";
      Fastt.staticBadge(prevTitle, "NO DATA — RELAY UNREACHABLE");
      return;
    }
    fanCols = data.columns || [];
    fanRows = data.rows || [];
    if (!fanCols.length || !fanRows.length) {
      $("#gs-prev-head").innerHTML = "<th>Fan data</th>";
      $("#gs-prev-rows").innerHTML = '<tr><td style="color:var(--muted);padding:18px 12px">'
        + "No profiled fans for this creator yet — the export would write only the header row. "
        + "Turn on Auto-learn Fans to fill this.</td></tr>";
      $("#gs-prev-count").textContent = "0 rows";
      Fastt.staticBadge(prevTitle, "NO PROFILED FANS YET");
      return;
    }
    Fastt.liveBadge(prevTitle);
    // Rows exist → the search is real. Enable + reveal it.
    var si = $("#gs-search");
    si.disabled = false;
    si.placeholder = "Search " + Fastt.fmtInt(fanRows.length) + " fans by name / nickname / bio…";
    $("#gs-search-wrap").classList.add("show");
    $("#gs-prev-sub").innerHTML = "One row per profiled fan — these are the <b style=\"color:#cfcfcf\">live rows</b> "
      + "this export writes, read from the same builder (<code>build_fan_data</code>). "
      + "The header is driven by the response's <code>columns</code>, so it can never drift from the exporter. "
      + "Scroll the table sideways for all " + fanCols.length + " columns — a blank cell here is blank in the sheet too.";
    paintPreview();
  }
  $("#gs-prev-more").addEventListener("click", function () {
    shown = Math.min(shown + 25, viewRows().length);
    paintPreview();
  });

  // ── live search over the loaded rows (client-side; no fan is messaged) ──
  var searchIn = $("#gs-search"), searchWrap = $("#gs-search-wrap"), searchClr = $("#gs-search-clr");
  searchIn.addEventListener("input", function () {
    gsQuery = searchIn.value.trim().toLowerCase();
    searchWrap.classList.toggle("has-q", !!gsQuery);
    shown = PAGE;               // a fresh query starts from the top of its result set
    paintPreview();
  });
  searchIn.addEventListener("keydown", function (e) {
    if (e.key === "Escape") { searchIn.value = ""; searchIn.dispatchEvent(new Event("input")); }
  });
  searchClr.addEventListener("click", function () {
    searchIn.value = ""; gsQuery = ""; searchWrap.classList.remove("has-q");
    shown = PAGE; paintPreview(); searchIn.focus();
  });

  await Promise.all([loadHistory(), loadPreview()]);
});
