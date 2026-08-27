"""service/lane_config_api.py — the Concurrency panel's read/write endpoints.

🚨 THESE ROUTES ARE UNAUTHENTICATED, BY EXPLICIT CHOICE, AND THEY WRITE.

They set the process-wide concurrency ceilings, and anyone who can reach the port
can change them. That is a decision the operator made knowingly for a
friends-only deployment — they asked for a panel with no password so retuning a
cap does not require signing in as admin — not an oversight. It is stated here,
and again at each gate it pierces in `server.py`, so nobody removes it as a bug.

🚨 AND ONE OF THEM RESTARTS THE RELAY. `RESTART_PATH` ends this process so
docker cycles it, because every value here applies at restart and a panel that
cannot restart is a panel that always ends at "now go find a terminal". It is
the destructive verb in this module and carries bounds the others do not need —
two per hour counted in a FILE that outlives the restart, and pid-1-only so it
can never stop a relay that nothing would bring back. `restart_relay()` has the
full reasoning; what it costs is in-flight work, and the panel says so too.

What is exposed, and what is not. The payload is cap NUMBERS only: no account
ids, no fan data, no session or key material. `/admin/lanes` is deliberately NOT
opened alongside it, because that route's body is keyed BY account id and
opening it would publish the account roster to anyone who asked for it.

The risk being accepted. These ceilings are shared by every account on the box,
so one caller's change lands on all of them, and a large value spends threads
and descriptors the whole process shares — 2026-07-04 (thread starvation) and
2026-08-08 (descriptor exhaustion) were both that failure. Two things bound it:
saved values apply at RESTART, so a bad number costs a restart rather than an
outage; and an env var beats a saved value, so recovery never requires reaching
into the container for a file. The audit middleware records every write.

Why a module and not two handlers in `server.py`: this repo keeps endpoint
families in `*_api.py` routers — there are two dozen — and `server.py` is
already ~9.8k lines. A new config API belongs in the shape the codebase already
has for config APIs.
"""
from __future__ import annotations

import logging
import os
import signal
import threading
import time
from typing import Any

from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

import container_limits
import lane_config
import restart_budget

log = logging.getLogger("of-relay.lane-config-api")

# The human panel. Deliberately NOT linked from anywhere in the app — the
# operator's choice is that this lives as a direct link documented in the
# README, so using it never requires signing in as anyone.
PAGE_PATH = "/lanes-panel"
# 🚨 EVERY ROUTE HERE IS A CHILD OF THE PAGE'S OWN PATH, AND THAT IS LOAD-BEARING.
#
# These were `/admin/lane-config` and `/admin/lane-restart` until review caught
# what the prefix bought: `app/next.config.ts` rewrites `/admin/:path*` to the
# relay wholesale, and traefik puts the Next app on the public host. So an
# `/admin/` path on the no-auth allowlist is reachable from the open internet
# with no cookie — which for a route that ENDS THE PROCESS is a remote kill
# switch, published with an achievable cadence rather than protected by one.
#
# `/lanes-panel` is in no rewrite, so the page has only ever been reachable on
# the relay port. Filing the API under the page's own path gives it exactly the
# reachability of the page whose "no password" ruling it inherits — which is the
# ruling the operator actually made. It also drops both routes out of
# `server.py`'s `startswith("/admin/")` pre-filter entirely.
#
# Do not move these back under `/admin/` to "match the other admin routes".
PATH = "/lanes-panel/config"
# The button that makes the rest of the panel finish. Every value here applies
# at restart, so without this the panel always ended at "now go find a
# terminal". Bounded by `restart_budget.LIMIT` per hour, and see
# `restart_relay()` for the guards that make self-exit safe.
RESTART_PATH = "/lanes-panel/restart"
# The SAME config resource, served a second time under `/admin/` — and the
# difference between the two is the whole point.
#
# `app/components/settings/LaneConfigCard.tsx` has always read and written these
# caps from inside Settings, and it reaches the relay through Next's
# `/admin/:path*` rewrite. Moving the route wholesale broke that card (404 on
# load and on save) with nothing to catch it: the path is a string literal, so
# `tsc` stays green. Review found it; this is the repair.
#
# 🚨 THIS ONE IS **NOT** ON `server.py`'s no-auth allowlist, and must never be
# added to it. It is publicly reachable via the rewrite, so it is gated like
# every other `/admin/` route — which is correct for the consumer it serves, a
# card that only renders for someone already signed in. The ungated door is
# `PATH`, which no rewrite covers.
#
# There is deliberately NO `/admin/` twin of `RESTART_PATH`. A resource that
# ends the process gets one door, and it is the one the open internet cannot
# reach.
ADMIN_PATH = "/admin/lane-config"

router = APIRouter(tags=["lane-config"])

# Long enough for the 200 to reach the browser before the process goes away —
# the panel needs the budget in that response to render "1 left".
_RESTART_DELAY_S = 0.5
# uvicorn's graceful shutdown waits for in-flight requests, and `/health?all`
# alone budgets 20s. Past this the operator asked for a restart and did not get
# one, which is worse than dropping the straggler, so we stop asking.
_RESTART_GRACE_S = 25.0
# Identifies THIS process, so the panel can tell a relay that came back from one
# that never went down. Polling for "does the port answer" is not enough: the
# first poll can land in the half-second before the SIGTERM, and the page would
# report a restart that had not happened yet.
_BOOT_ID = f"{os.getpid()}-{time.time():.3f}"


class LaneConfigBody(BaseModel):
    """`{"values": {"IMG_FETCH_CONCURRENCY": 12, "HEALTH_ALL_CONCURRENCY": null}}`

    `null` deletes an override and falls the knob back to its code default —
    "reset" without a second endpoint to keep in sync with this one.
    """
    values: dict[str, int | None]


def _payload() -> dict[str, Any]:
    """One shape for both verbs, so the panel never has to reconcile two views
    of the same state."""
    knobs = lane_config.describe()
    return {
        "knobs": knobs,
        "restart_required": any(k["pending"] for k in knobs),
        "path": lane_config.path(),
        "history": lane_config.history(50),
        "container": container_limits.describe(),
        "restart": _restart_state(),
    }


def _restart_state() -> dict[str, Any]:
    """The restart resource: cheap enough to poll. `_payload()` costs a jsonl
    read, four cgroup reads and a registry walk — polling THAT every 2s during a
    boot, forty times, to compare one string was ~280 file reads per restart
    against a relay that is busy starting up."""
    return {
        "budget": restart_budget.budget(),
        # Surfaced because `restart_budget.path()` otherwise documents a reader
        # it does not have — and a 429 with no way to learn the filename is a
        # dead end on a page whose whole point is not needing a terminal.
        "path": restart_budget.path(),
        "boot_id": _BOOT_ID,      # see `_BOOT_ID` for why polling needs it
        # Whether THIS process may end itself — see `restart_relay()`. The panel
        # disables the button on it rather than letting a click discover the
        # 409, because the answer never changes for the life of the process.
        "can_restart": _can_self_restart(),
    }


def _can_self_restart() -> bool:
    """PID 1 is the structural fact that makes self-exit safe, so it is the
    thing checked — not a list of hosts or an opt-in flag.

    As PID 1 this process IS the container: ending it ends the container, and
    docker's `restart: unless-stopped` (on every relay definition in this repo)
    starts a fresh one. At any other pid, exiting stops the relay and nothing
    brings it back — which is exactly what a developer running uvicorn in a venv
    would get. Fail closed there: the cost of refusing is a manual restart, the
    cost of allowing is an outage nobody is watching for.
    """
    return os.getpid() == 1


@router.get(PATH)
@router.get(ADMIN_PATH)
def get_lane_config() -> dict[str, Any]:
    """Every tunable ceiling, what it is set to, and where that came from.

    🚨 Unauthenticated — see the module docstring."""
    return _payload()


@router.post(PATH)
@router.post(ADMIN_PATH)
def set_lane_config(body: LaneConfigBody = Body(...)) -> dict[str, Any]:
    """Persist new ceilings. Applies at the next restart, never live.

    🚨 Unauthenticated WRITE — see the module docstring.

    The only rejections are an unknown knob and a non-integer, which is what the
    env vars these replaced already accepted.
    """
    try:
        lane_config.set_many(body.values)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except OSError as e:
        # The store lives on a mount. If it is gone, say so — a 200 here would
        # mean "saved" for a value that will not survive the restart it needs.
        log.exception("lane_config write failed")
        raise HTTPException(status_code=500, detail=f"could not write store: {e}")
    return _payload()


def _exit_soon() -> None:
    """SIGTERM ourselves, then hard-exit if that does not finish.

    SIGTERM first, not `os._exit`, because a graceful uvicorn shutdown runs the
    lifespan teardown — SQLite sessions close, the WS pumps stop — and this
    process is holding a 1.7 GB database open. The hard exit exists because
    graceful shutdown WAITS on in-flight requests with no timeout of its own: a
    single stuck OF call would otherwise turn "restart" into "hang", and the
    operator would be left with a button that silently did nothing.
    """
    time.sleep(_RESTART_DELAY_S)
    log.warning("lane-restart: operator-requested restart — SIGTERM to self (pid %s)",
                os.getpid())
    try:
        os.kill(os.getpid(), signal.SIGTERM)
    except OSError:
        log.exception("lane-restart: SIGTERM to self failed — exiting hard")
        os._exit(0)
    time.sleep(_RESTART_GRACE_S)
    # Still here, so the graceful path is stuck behind something that will not
    # finish. `_exit` rather than `sys.exit`: this is a daemon thread, and
    # SystemExit raised here would be swallowed and change nothing.
    log.warning("lane-restart: graceful shutdown unfinished after %ss — exiting hard",
                _RESTART_GRACE_S)
    os._exit(0)


def _schedule_exit() -> None:
    """The one line that makes this module destructive, given its own name.

    A test can rebind THIS and the exit becomes structurally unreachable; with
    the thread spawned inline at the call site, the same test has to neutralise
    `_exit_soon`'s body and hope nothing else ever calls it. `restart_relay`
    returns before this thread does anything, so the seam is the whole safety
    story for testing the endpoint at all.
    """
    threading.Thread(target=_exit_soon, name="lane-restart", daemon=True).start()


@router.get(RESTART_PATH)
def restart_status() -> dict[str, Any]:
    """Just enough to answer "is it back yet" — see `_restart_state`."""
    return _restart_state()


class RestartBody(BaseModel):
    """`{"confirm": true}` — and the BODY IS THE POINT, not the field.

    🚨 THIS IS THE CSRF GUARD, AND IT IS THE ONLY ONE. A bodyless POST is a CORS
    "simple request": any page the operator happens to have open can fire
    `fetch("http://127.0.0.1:8797/lanes-panel/restart", {method:"POST",
    mode:"no-cors"})` with NO preflight, get an opaque response it cannot read,
    and land the side effect anyway. Moving off `/admin/` closed the public
    internet; it does not close the operator's own browser, because localhost is
    reachable from every page in it.

    Requiring `application/json` forces a preflight, and there is no CORS
    middleware in this service to answer one — so the cross-origin call dies
    before the handler runs. `POST PATH` was incidentally protected by this all
    along (it needs a values body); the restart route needed nothing, which is
    what made it the sharper edge of the two.

    Do not make this field optional to "simplify" the endpoint. The requirement
    that a body exists at all is the whole mechanism.
    """
    confirm: bool


@router.post(RESTART_PATH)
def restart_relay(body: RestartBody = Body(...)) -> dict[str, Any]:
    """Restart the relay so saved caps take effect. Bounded to
    `restart_budget.LIMIT` per hour.

    🚨 Unauthenticated, like the rest of this module — and this one is the
    destructive verb, so it carries three guards the others do not need:

    • **A JSON body** (`RestartBody`), which is what stops a cross-origin page
      in the operator's browser from firing it preflight-free.
    • **PID 1 only** (`_can_self_restart`), checked BEFORE the budget is spent
      so a relay that cannot restart never burns a restart discovering it.
    • **A budget on disk**, spent BEFORE the exit. The event being limited is
      this process ending, so an in-memory counter would reset on the very
      thing it counts and bound nothing.

    What it costs, stated plainly because the panel says it too: in-flight work
    is dropped. Automation runs mid-send, open WS pumps and anything queued
    behind them do not survive. That is the trade for not needing a terminal,
    and it is why the number is two an hour and not twenty.
    """
    if not body.confirm:
        raise HTTPException(status_code=400, detail="confirm must be true")
    if not _can_self_restart():
        raise HTTPException(status_code=409, detail=(
            "this relay is not pid 1, so exiting would stop it for good rather "
            "than cycle it — restart it the way it was started"
        ))
    spent = restart_budget.spend()
    if spent is None:
        budget = restart_budget.budget()
        raise HTTPException(status_code=429, detail=(
            f"{budget['limit']} restarts per hour is the limit — "
            f"{budget['retry_after_s']}s until the next one frees"
        ))
    # The story the history exists to tell is "the cap changed, and then it was
    # applied". Without this the operator sees the first half and never the
    # second. Best-effort, like every other append: a lost row must not fail a
    # restart that is already paid for.
    try:
        lane_config.note_restart()
    except Exception:      # noqa: BLE001 — the restart is already paid for
        # `_append_history` swallows OSError, but anything else escaping here
        # would burn a restart from the budget and then never schedule one.
        log.warning("lane-restart: history note failed", exc_info=True)
    _schedule_exit()
    # `boot_id` identifies the process that ACCEPTED this, which is what the
    # page compares against while polling. Reusing the id it happened to hold
    # from an earlier load would settle the moment any other restart had
    # happened since the page was opened.
    return {"restarting": True, **_restart_state()}


# ── The panel page ──────────────────────────────────────────────────
#
# Self-contained HTML served by the relay itself: no app, no login, no build
# step, no external assets. It talks only to PATH above, so everything it can
# show or change is exactly what that endpoint already exposes.
# Raw string: the JS below carries backslash escapes (a \n inside a confirm()
# message), and in a cooked string Python would turn those into real newlines —
# which lands in the browser as an unterminated JS string literal.
_PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>Relay concurrency</title>
<style>
  :root { color-scheme: dark; }
  body { margin: 0 auto; max-width: 880px; padding: 24px 16px 48px;
         background: #101418; color: #dde3ea;
         font: 14px/1.5 -apple-system, "Segoe UI", Roboto, sans-serif; }
  h1 { font-size: 20px; margin: 0 0 4px; }
  .sub { color: #8b96a5; margin: 0 0 20px; }
  .banner { background: #3a2f10; border: 1px solid #8a6d1a; color: #ffd970;
            border-radius: 8px; padding: 10px 14px; margin: 0 0 16px; display: none; }
  table { width: 100%; border-collapse: collapse; }
  th { text-align: left; color: #8b96a5; font-weight: 500; font-size: 12px;
       text-transform: uppercase; letter-spacing: .04em; padding: 6px 8px; }
  td { padding: 8px; border-top: 1px solid #232a33; vertical-align: middle; }
  td.name { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 13px; }
  .badge { display: inline-block; border-radius: 999px; padding: 1px 8px;
           font-size: 11px; margin-left: 6px; }
  .b-env { background: #3d1f24; color: #ff9daa; }
  .b-pending { background: #163a2b; color: #6fe0ae; }
  input[type=number] { width: 76px; background: #0b0e12; color: #dde3ea;
        border: 1px solid #2c3540; border-radius: 6px; padding: 5px 8px; }
  input:disabled { opacity: .4; }
  button { background: #1d2733; color: #dde3ea; border: 1px solid #2c3540;
           border-radius: 6px; padding: 5px 12px; cursor: pointer; }
  button:hover { background: #26313f; }
  button.reset { background: transparent; border-color: transparent; color: #8b96a5; }
  button.reset:hover { color: #dde3ea; }
  .muted { color: #8b96a5; }
  h2 { font-size: 15px; margin: 28px 0 8px; }
  .hist td { font-size: 13px; }
  .foot { margin-top: 24px; color: #5d6875; font-size: 12px; }
  .err { color: #ff9daa; margin: 8px 0; display: none; }
  code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
         font-size: 12px; background: #0b0e12; border: 1px solid #232a33;
         border-radius: 5px; padding: 2px 6px; }
  code.cmd { display: block; padding: 10px 12px; overflow-x: auto;
             white-space: pre; margin: 8px 0 0; color: #9be7c0; }
  .note { background: #141a21; border: 1px solid #232a33; border-radius: 8px;
          padding: 10px 14px; color: #8b96a5; margin: 0 0 14px; }
  .pick { display: inline-flex; gap: 6px; }
  .pick button.on { background: #1e3a2c; border-color: #2f6a4c; color: #9be7c0; }
  button.danger { background: #3a1f24; border-color: #6a2f38; color: #ffb3bd; }
  button.danger:hover { background: #4a262c; }
  button.danger:disabled { opacity: .4; cursor: not-allowed; }
  button.tiny { padding: 2px 8px; font-size: 12px; }
  .why { color: #8b96a5; font-size: 12px; }
</style>
</head>
<body>
<h1>Relay concurrency</h1>
<p class="sub">Anyone with this link can read and change these caps. Saved
values apply on the next relay <b>restart</b>, never live.</p>
<div class="banner" id="banner">Saved values differ from what is running —
restart the relay to apply them.</div>
<div class="err" id="err"></div>
<table>
  <thead><tr><th>knob</th><th>running now</th><th>next boot</th>
  <th>default</th><th>set to</th><th></th></tr></thead>
  <tbody id="rows"></tbody>
</table>
<h2>Container limits</h2>
<p class="note">CPU and memory belong to the <b>container</b>, not to this
process. <code>/sys/fs/cgroup</code> is mounted read-only and there is no docker
socket in this image, so the panel can show you what is enforced and write the
command &mdash; it cannot apply it. Pick a value and run the line it gives you
on the host; <code>docker&nbsp;update</code> takes effect immediately, with no
restart.<br><br>⚠️ <b>It holds until the container is next recreated</b>, and
then it is gone: <code>docker&nbsp;update</code> writes to the container, not to
the compose file. Any <code>compose&nbsp;up&nbsp;-d</code> that sees changed
config &mdash; and <code>scripts/prod-cpu-limits.sh&nbsp;apply</code> itself
&mdash; replaces the container and takes the limit with it. For a value that
survives, put it in the gitignored <code>docker-compose.override.yml</code>:
that is the repo's one durable mechanism, and the reason this panel only writes
you a command.</p>
<table>
  <thead><tr><th>limit</th><th>enforced now</th><th>set to</th></tr></thead>
  <tbody id="climits"></tbody>
</table>
<div id="cmdwrap" style="display:none">
  <code class="cmd" id="cpucmd"></code>
  <button class="tiny" style="margin-top:8px" onclick="copyCmd()">copy</button>
</div>

<h2>Restart</h2>
<p class="note" id="restartnote">Checking whether this relay can restart
itself&hellip;</p>
<button class="danger" id="restartbtn" onclick="restart()" disabled>Restart the relay</button>
<span class="muted" id="restartbudget"></span>

<h2>History</h2>
<table><tbody id="hist"><tr><td class="muted">no changes yet</td></tr></tbody></table>

<h2>Suggested settings &mdash; Hostinger</h2>
<p class="note">The <b>default</b> column is each ceiling's built-in value,
which is what the 2 vCPU Hostinger box was sized against &mdash; it is read from
the running process, not restated here, so it cannot drift from the table above.
<b>4 core</b> is <code>SCALING.md</code>'s target for up to 50 accounts: an
<b>untested hypothesis</b>, sized against measurements that were read off a
different machine. Neither column is a recommendation for whatever box you are
looking at &mdash; check <i>running now</i> above first. Click a number to save
it.</p>
<table>
  <thead><tr><th>knob</th><th>default</th><th>4 core</th><th>why</th></tr></thead>
  <tbody id="suggest"></tbody>
</table>
<p class="foot" id="foot"></p>
<script>
const API = "/lanes-panel/config";
const RESTART_API = "/lanes-panel/restart";
const esc = s => String(s).replace(/[&<>"']/g,
  c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

// ONLY THE DELTAS from each knob's code default, and that is deliberate.
//
// The baseline is already in the payload — `describe()` ships `default` for
// every knob and the table at the top renders it — so restating it here would
// be exactly the declared-table failure `lane_config`'s docstring exists to
// forbid: "changing one makes the panel report the other". The first draft of
// this table restated seven defaults verbatim and got the eighth wrong,
// rendering 4 in one section and 8 in another, on the same page.
//
// It is also why the rows are driven by `j.knobs` rather than by this object: a
// knob nothing here mentions still gets a row, so the three that were missing
// from the hand-written version cannot go missing again.
const TARGET_4CORE = {
  HEALTH_ALL_CONCURRENCY:       [16, "must exceed how many accounts can be slow AT ONCE, not how fast a healthy probe is"],
  VAULT_MEDIA_CONCURRENCY:      [10, "async-parked and cheap; scales mildly with accounts"],
  STORYBOARD_BUILD_CONCURRENCY: [3,  "the one CPU-bound lane (ffmpeg) - leave a core to schedule everything else"],
  RELAY_EXECUTOR_THREADS:       [192, "every OF call is sync and shares this one pool"],
};
// Not knobs — the container's own ceilings, shown alongside so the two columns
// describe a whole machine rather than half of one.
const SUGGEST_CONTAINER = [
  ["cpus",      "1.2", "3.0", "a limit above the host's core count is not a raise"],
  ["mem_limit", "2g",  "4g",  "exceeding this is an OOM-kill, not a slowdown - a backstop, not a reservation"],
];

let last = null;
let cpuPick = null;
// `keepErr` is not a convenience flag — it is the difference between an
// operator seeing why a restart was refused and seeing nothing at all.
//
// `load()` is async, so a bare clear at the top runs SYNCHRONOUSLY on call. Two
// call sites invoke it immediately after showing an error (a 429 refusal, and
// the 80s "it never came back"), and both were silently wiped in the same turn
// — the 429 case leaving a destructive button that refuses without a word.
// Clearing after the fetch would not fix it either: refreshing the budget is
// the whole reason those sites call load() at all.
async function load(keepErr) {
  if (!keepErr) document.getElementById("err").style.display = "none";
  let j;
  try { j = await (await fetch(API)).json(); }
  catch (e) {
    // `renderRestart` before returning: this is the state the restart timeout
    // hands us, and without it the button stays disabled from `restart()` and
    // the label stays frozen on "restarting… 78s" — the exact dead end the
    // timeout's `load()` was added to prevent.
    showErr("could not reach the relay: " + e);
    renderRestart();
    return;
  }
  document.getElementById("banner").style.display =
    j.restart_required ? "block" : "none";
  // A per-row Save makes "edit several rows, save one" the natural flow —
  // carry unsaved edits across the re-render instead of silently wiping them.
  const dirty = {};
  document.querySelectorAll("#rows input").forEach(i => {
    // Numeric compare, not just lexical: a number input keeps "07"/"1e2"/"7.0"
    // verbatim, and a saved value whose canonical render is "7" must not read
    // as an unsaved edit — that phantom would be restored on every re-render
    // and could mask (then silently revert) another session's later change.
    if (i.value !== i.dataset.server && Number(i.value) !== Number(i.dataset.server))
      dirty[i.id] = i.value;
  });
  const rows = j.knobs.map(k => {
    const badges =
      (k.env_locked ? '<span class="badge b-env" title="an environment variable pins this — the panel cannot change it">env-pinned</span>' : "") +
      (k.pending ? '<span class="badge b-pending">on restart</span>' : "");
    const nid = "in-" + k.name;
    return `<tr>
      <td class="name">${esc(k.name)}${badges}</td>
      <td>${k.live ?? '<span class="muted">–</span>'}</td>
      <td>${esc(k.value)}</td>
      <td class="muted">${esc(k.default)}</td>
      <td><input type="number" min="1" id="${nid}" value="${esc(k.value)}"
           data-server="${esc(k.value)}" ${k.env_locked ? "disabled" : ""}></td>
      <td><button onclick="save('${esc(k.name)}')" ${k.env_locked ? "disabled" : ""}>Save</button>
          ${k.stored !== null && !k.env_locked
            ? `<button class="reset" onclick="reset('${esc(k.name)}')"
                 title="back to the default (${esc(k.default)})">reset</button>` : ""}</td>
    </tr>`;
  });
  document.getElementById("rows").innerHTML = rows.join("");
  Object.entries(dirty).forEach(([id, v]) => {
    const el = document.getElementById(id);
    if (el && !el.disabled) el.value = v;
  });
  const hist = (j.history || []).map(h => `<tr class="hist">
    <td class="muted">${esc((h.ts || "").replace("T", " ").replace("+00:00", " UTC"))}</td>
    ${h.event
      ? `<td colspan="2" class="why">${esc(h.event)}</td>`
      : `<td class="name">${esc(h.key)}</td>
         <td>${h.old ?? '<span class="muted">default</span>'} &rarr;
             ${h.new ?? '<span class="muted">default</span>'}</td>`}
  </tr>`);
  document.getElementById("hist").innerHTML = hist.length ? hist.join("")
    : '<tr><td class="muted">no changes yet</td></tr>';
  // ONE variable holds the whole payload. Three renderers each keeping their
  // own slice is how one of them ends up reading a field another already
  // replaced — and `pickCpu` needs to re-render outside a load, so at least one
  // of them has to read module state anyway.
  last = j;
  renderContainer();
  renderRestart();
  renderSuggest();
  document.getElementById("foot").textContent =
    "store: " + j.path + "   ·   restarts: " + ((j.restart || {}).path || "?");
}

// ── Container limits ────────────────────────────────────────────────
// Read-only by necessity, not by choice: the cgroup is mounted ro and there is
// no docker socket in this image. The picker's whole job is to write a correct
// command — it never claims to have applied one.
function renderContainer() {
  const c = (last && last.container) || {};
  const cpuNow = !c.cpus_readable ? '<span class="muted">unknown &mdash; no cgroup here</span>'
    : (c.cpus === null ? '<span class="muted">no limit</span>' : esc(c.cpus));
  // Gated on ITS OWN readable flag. A container can have a perfectly readable
  // cpu.max and an unparseable memory.max, and rendering that as "no limit"
  // is the worse of the two wrong answers — exceeding a memory ceiling is an
  // OOM-kill, not a slowdown.
  const memNow = !c.mem_readable ? '<span class="muted">unknown</span>'
    : (c.mem_bytes === null ? '<span class="muted">no limit</span>'
       : (Math.round(c.mem_bytes / 1073741824 * 10) / 10) + " GiB");
  const host = c.host_cpus;
  const pick = (n, label) => {
    // A pick above the host's core count cannot be a raise; say so on the
    // button rather than letting someone paste it and wonder.
    const tooBig = host && n > host;
    return `<button class="tiny ${cpuPick === n ? "on" : ""}"
      ${tooBig ? `title="this box has only ${esc(host)} cores"` : ""}
      onclick="pickCpu(${n})">${label}${tooBig ? " ⚠" : ""}</button>`;
  };
  document.getElementById("climits").innerHTML = `
    <tr><td class="name">cpus</td><td>${cpuNow}</td>
        <td><span class="pick">${pick(1, "1")}${pick(2, "2")}${pick(3, "3")}${pick(0, "no limit")}</span></td></tr>
    <tr><td class="name">memory</td><td>${memNow}</td>
        <td class="why">not offered here on purpose &mdash; a memory ceiling
        OOM-kills where a CPU one only throttles</td></tr>
    <tr><td class="name">host cores</td><td>${esc(host ?? "?")}</td>
        <td class="why">the ceiling any cpus value sits under</td></tr>`;
}
function pickCpu(n) {
  cpuPick = n;
  renderContainer();
  const c = (last && last.container) || {};
  // Gate on cpus_readable, NOT on container_id being truthy: outside a cgroup
  // /etc/hostname is just the machine name, and we would print a confident
  // `docker update --cpus=2 somedevbox` that targets nothing.
  const id = c.cpus_readable && c.container_id ? c.container_id : "<container>";
  document.getElementById("cpucmd").textContent =
    "docker update --cpus=" + n + " " + id;   // --cpus=0 removes the limit
  document.getElementById("cmdwrap").style.display = "block";
}
function copyCmd() {
  const text = document.getElementById("cpucmd").textContent;
  if (!navigator.clipboard) { showErr("no clipboard here — select the line and copy it"); return; }
  navigator.clipboard.writeText(text)
    .catch(() => showErr("could not copy — select the line and copy it by hand"));
}

// ── Suggested settings ──────────────────────────────────────────────
// Driven by j.knobs, not by TARGET_4CORE — see the comment on that object.
function renderSuggest() {
  const knobs = (last && last.knobs) || [];
  const rows = knobs.map(k => {
    const t = TARGET_4CORE[k.name];
    const target = t
      ? `<button class="tiny" onclick="saveValue('${esc(k.name)}', ${t[0]})"
           title="save ${t[0]} to ${esc(k.name)}">${t[0]}</button>`
      : '<span class="muted">leave at default</span>';
    return `<tr><td class="name">${esc(k.name)}</td>
      <td class="muted">${esc(k.default)}</td><td>${target}</td>
      <td class="why">${esc(t ? t[1] : "")}</td></tr>`;
  });
  // A target naming a knob this process does not resolve is stale. Show it —
  // silently dropping it is how a renamed knob leaves a suggestion that quietly
  // stops existing instead of one that visibly needs fixing.
  const known = new Set(knobs.map(k => k.name));
  const stale = Object.keys(TARGET_4CORE).filter(n => !known.has(n)).map(n =>
    `<tr><td class="name">${esc(n)}</td><td colspan="3" class="why">
     stale suggestion &mdash; no ceiling in this process resolves that name</td></tr>`);
  const container = SUGGEST_CONTAINER.map(([name, two, four, why]) =>
    `<tr><td class="name">${esc(name)}</td>
     <td class="muted">${esc(two)}</td><td class="muted">${esc(four)}</td>
     <td class="why">${esc(why)} &mdash; <b>not a knob</b>: set on the
     container, so both cells are written down here rather than read from the
     process like the rows above</td></tr>`);
  document.getElementById("suggest").innerHTML =
    rows.concat(stale, container).join("") ||
    '<tr><td class="muted">no ceilings resolved yet</td></tr>';
}
function saveValue(name, value) { post({ [name]: value }); }

// ── Restart ─────────────────────────────────────────────────────────
function renderRestart() {
  const btn = document.getElementById("restartbtn");
  // NO PAYLOAD IS NOT A DIAGNOSIS. Falling through with `{}` rendered the
  // bold, invented claim "this relay is not pid 1" and a budget of "0 of 0"
  // whenever the first load simply failed — the same "unlimited vs could not
  // tell" sin `container_limits` exists to avoid, one file over. Adding
  // renderRestart() to load()'s error path is what exposed this; leave the
  // static "Checking…" note in place instead.
  if (!last || !last.restart) {
    btn.disabled = true;
    document.getElementById("restartbudget").textContent = "";
    return;
  }
  const r = last.restart;
  const b = r.budget || {};
  const note = document.getElementById("restartnote");
  if (!r.can_restart) {
    note.innerHTML = "This relay is <b>not pid 1</b>, so ending it would stop " +
      "it for good rather than cycle it. Restart it however it was started.";
  } else {
    note.innerHTML = "Applies every saved value above. <b>In-flight work is " +
      "dropped</b> &mdash; automation runs mid-send and the open OF websocket " +
      "pumps do not survive it. The relay comes back on its own, usually within " +
      "a few seconds.";
  }
  btn.disabled = !r.can_restart || !(b.remaining > 0);
  let text = (b.remaining ?? 0) + " of " + (b.limit ?? 0) + " left this hour";
  if (!(b.remaining > 0) && b.retry_after_s)
    text += " — next in " + Math.ceil(b.retry_after_s / 60) + " min";
  document.getElementById("restartbudget").textContent = text;
}
async function restart() {
  const b = ((last && last.restart) || {}).budget || {};
  if (!confirm("Restart the relay now?\n\nIn-flight sends and the OF websocket " +
               "pumps are dropped. Limited to " + (b.limit ?? "?") +
               " restarts an hour.")) return;
  document.getElementById("err").style.display = "none";
  document.getElementById("restartbtn").disabled = true;
  // Never poll with a null baseline. `waitForBoot` settles on the first 200 it
  // sees when it has nothing to compare against, and uvicorn keeps answering
  // for the whole graceful-shutdown window (25s) while it drains — so a blind
  // poll reports "restarted" against the very process it asked to leave. The
  // last id this page was told is always a better baseline than none.
  const known = ((last && last.restart) || {}).boot_id || null;
  let r;
  try {
    // The body is the CSRF guard, not a formality — see `RestartBody`.
    r = await fetch(RESTART_API, { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ confirm: true }) });
  } catch (e) {
    // A POST that dies in flight is INDISTINGUISHABLE from success, because
    // success is the process going away. Poll rather than calling it a failure.
    waitForBoot(known);
    return;
  }
  if (!r.ok) { await showApiErr("restart refused", r); load(true); return; }
  // Prefer the id of the process that ACCEPTED it; fall back to the last known
  // one rather than to null.
  let before = known;
  try { before = (await r.json()).boot_id || known; } catch (e) { /* keep known */ }
  waitForBoot(before);
}
async function waitForBoot(before) {
  const label = document.getElementById("restartbudget");
  const started = Date.now();
  const deadlineMs = 80000;
  while (Date.now() - started < deadlineMs) {
    const secs = Math.round((Date.now() - started) / 1000);
    label.textContent = "restarting… " + secs + "s";
    await new Promise(done => setTimeout(done, 2000));
    try {
      // Timed out per-poll: during a restart docker's port proxy can ACCEPT and
      // then hold, so an untimed fetch hangs for the browser default and the
      // elapsed time silently runs far past the deadline.
      const opts = { cache: "no-store" };
      // Guarded: on an engine without AbortSignal.timeout this throws, and the
      // catch below would read it as "still down" — a guaranteed false 80s
      // failure on the page you open when things are already wrong.
      if (typeof AbortSignal !== "undefined" && AbortSignal.timeout)
        opts.signal = AbortSignal.timeout(2500);
      const res = await fetch(RESTART_API, opts);
      if (res.ok) {
        const fresh = await res.json();
        // `before === null` means we could not learn the old id, so any answer
        // is the best evidence available.
        if (!before || (fresh.boot_id && fresh.boot_id !== before)) {
          await load();
          return;
        }
      }
    } catch (e) { /* down, or the poll timed out — both mean keep waiting */ }
  }
  showErr("the relay has not come back after " +
          Math.round((Date.now() - started) / 1000) + "s — check the container");
  // Re-read so the button never stays dead — but KEEP the message that says
  // why, which is the only thing telling the operator to go look at the
  // container.
  load(true);
}
function showErr(m) {
  const e = document.getElementById("err");
  e.textContent = m; e.style.display = "block";
}
// FastAPI answers with {"detail": "..."} — showing the raw JSON puts braces and
// quotes in front of an operator who just wants the sentence.
async function showApiErr(prefix, res) {
  let detail = "";
  try { detail = (await res.json()).detail; } catch (e) { /* not JSON */ }
  showErr(prefix + ": " + (detail || res.status));
}
async function post(values) {
  document.getElementById("err").style.display = "none";
  let r;
  try {
    r = await fetch(API, { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ values }) });
  } catch (e) { showErr("save failed — could not reach the relay: " + e); return; }
  // On failure keep the page as it is: re-rendering would wipe the value the
  // operator just typed, right when they need it to retry.
  if (!r.ok) { await showApiErr("save failed", r); return; }
  load();
}
function save(name) {
  // Number(), not parseInt(): a number input legally holds "1e2" and "12.5",
  // and parseInt would silently save 1 and 12 instead of erroring.
  const v = Number(document.getElementById("in-" + name).value);
  if (!Number.isInteger(v) || v < 1) { showErr(name + " must be an integer >= 1"); return; }
  post({ [name]: v });
}
function reset(name) { post({ [name]: null }); }
load();
</script>
</body>
</html>
"""


@router.get(PAGE_PATH, include_in_schema=False)
def lanes_panel() -> HTMLResponse:
    """The Concurrency panel, as a page the relay serves itself.

    🚨 Unauthenticated, like the API it fronts — see the module docstring.
    Linked from the README and from nowhere in the app, on purpose."""
    return HTMLResponse(_PAGE)
