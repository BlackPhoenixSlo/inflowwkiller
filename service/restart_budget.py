"""service/restart_budget.py — how many times the relay may restart itself.

WHY THIS EXISTS. The Concurrency panel's saved values apply at restart, so a
panel that cannot restart the relay always ends at "now go find a terminal".
The button that closes that loop ends the process so docker's
`restart: unless-stopped` cycles it — which drops in-flight OF sends and the
websocket pumps. This module is the bound on that button.

🚨 THE LEDGER IS A FILE, AND THAT IS THE WHOLE POINT. The event being limited
is THIS PROCESS ENDING. An in-memory counter would reset on exactly the thing it
counts, so it would bound nothing at all while looking like it did.

WHY ITS OWN MODULE and not part of `lane_config`. It wants *a mutex and a
durable directory*, not that module's state — it touches no knob, no precedence
chain, no registry. Sharing `lane_config`'s lock would also mean spending a
restart blocks a config save and vice versa, for no reason anyone chose. It
takes the directory from `lane_config.path()` because that is already the
answer to "which directory here actually survives" (a docker volume, not a
`/private/tmp` mount that macOS clears on reboot).

TWO CLOCK RULES, and they are not symmetric — this is the subtle part:

  • A stamp in the FUTURE is not COUNTED. A clock that jumped forward and back
    would otherwise hold the button shut for the length of the skew, and being
    unable to restart is the worse failure of the two.
  • A stamp in the future is still KEPT IN THE FILE. Counting and pruning are
    different questions, and conflating them is a real bug that was caught in
    review: a write that persisted only the counted stamps would DELETE the
    other ones, so a backward clock correction did not restore the budget, it
    erased it permanently. Never write back a list narrower than the one read,
    except for stamps that are genuinely older than the window.

The same rule is what makes concurrent spends safe. `now` is sampled INSIDE the
lock, so a thread can never prune a stamp that another thread committed a
millisecond earlier just because that stamp looks "future" to a clock read
before the lock was taken.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

import lane_config
from atomic_json import write_atomic

log = logging.getLogger("of-relay.restart-budget")

LIMIT = 2
WINDOW_S = 3600

# A pathological clock (repeated forward jumps) could otherwise accumulate
# future stamps forever, since those are deliberately never pruned.
#
# 🚨 THE CAP IS PER-BUCKET, NOT ON THE WHOLE LIST, and that is a bug fix rather
# than a style choice. Truncating the merged list to its newest N discards the
# LARGEST stamps last — and future stamps are always the largest, so once N of
# them accumulated, the truncation evicted every real in-window stamp AND the
# one just written. Measured on the first version: 17 seeded stamps, then 41
# consecutive restarts granted against a limit of 2, with the endpoint happily
# reporting `remaining: 2` throughout. A narrowing write that is not age-based
# is exactly what the docstring above forbids; capping the two buckets
# separately means future stamps can never evict the stamps that count.
_MAX_STAMPS = LIMIT * 8

_PATH = Path(lane_config.path()).with_name("lane_restarts.json")
_lock = threading.Lock()


def path() -> str:
    """Where the ledger is, for the panel's footer and for operators who want a
    clean slate."""
    return str(_PATH)


def _stamps() -> list[float]:
    """Epoch seconds of past operator-requested restarts. Never raises: a
    corrupt ledger must not be able to wedge the button permanently shut."""
    try:
        raw = json.loads(_PATH.read_text("utf-8"))
    except FileNotFoundError:
        return []
    except Exception:
        log.warning("restart ledger unreadable at %s — treating as empty",
                    _PATH, exc_info=True)
        return []
    if not isinstance(raw, list):
        # A dict would otherwise iterate its KEYS, which is a different kind of
        # wrong than "empty" and reads as a silently-reset budget.
        return []
    out: list[float] = []
    for value in raw:
        try:
            out.append(float(value))
        except (TypeError, ValueError):
            continue        # skip the bad element, keep the good ones
    return out


def _counted(stamps: list[float], now: float) -> list[float]:
    """The stamps that SPEND the budget: inside the window and not in the
    future. See the module docstring on why future stamps are excluded here and
    still survive `_kept`."""
    return [t for t in stamps if 0 <= now - t < WINDOW_S]


def _kept(stamps: list[float], now: float) -> list[float]:
    """The stamps that survive a write: everything not genuinely older than the
    window. Strictly wider than `_counted` — deliberately."""
    return [t for t in stamps if now - t < WINDOW_S]


def _budget(stamps: list[float], now: float) -> dict[str, Any]:
    counted = _counted(stamps, now)
    used = len(counted)
    retry_after = 0
    if used >= LIMIT and counted:
        # The budget frees when the OLDEST counted stamp leaves the window.
        retry_after = max(0, int(WINDOW_S - (now - min(counted))) + 1)
    return {
        "used": used,
        "limit": LIMIT,
        "remaining": max(0, LIMIT - used),
        "window_s": WINDOW_S,
        "retry_after_s": retry_after,
    }


def budget() -> dict[str, Any]:
    """How many restarts are left in the window, and when the next one frees."""
    now = time.time()
    return _budget(_stamps(), now)


def spend() -> dict[str, Any] | None:
    """Take one restart from the budget, atomically. Returns the budget AFTER
    spending, or None when there was nothing left to spend.

    Check-and-spend is ONE locked step, and `now` is sampled inside that lock.
    Two panels clicking together must not both read `remaining=1` and both
    restart; on a two-per-hour budget that is the whole budget gone to a
    double-click.

    🚨 The caller MUST spend BEFORE exiting. This ledger is the only thing that
    outlives the event it counts.
    """
    with _lock:
        now = time.time()
        stamps = _stamps()
        if len(_counted(stamps, now)) >= LIMIT:
            return None
        # Merge into what was read; never replace it with something narrower.
        # Bucketed so the backstop cannot evict the stamps that count — see
        # `_MAX_STAMPS`.
        merged = _kept(stamps, now) + [now]
        counted = sorted(t for t in merged if t <= now)
        ahead = sorted(t for t in merged if t > now)
        # `ahead[:N]`, not `ahead[-N:]`: within the future bucket the stamps
        # worth keeping are the NEAREST ones — they are the next to start
        # counting. Keeping the furthest instead would still be a narrowing
        # write that is not age-based, just a far smaller one than the bug this
        # bucketing replaced.
        keep = counted[-_MAX_STAMPS:] + ahead[:_MAX_STAMPS]
        try:
            write_atomic(_PATH, keep)
        except OSError:
            # Fail CLOSED, the opposite of how `lane_config` treats its store —
            # and deliberately so. An unrecordable restart is invisible to the
            # next boot, and a limit that forgets is not a limit. Refusing is
            # the safe direction for a button that drops in-flight sends.
            log.warning("restart ledger unwritable at %s — refusing the restart",
                        _PATH, exc_info=True)
            return None
        # Computed inside the lock from what was just written: re-reading the
        # file after releasing would report a number another thread had already
        # moved. `restart_relay` returns THIS, rather than calling `budget()`
        # again — a guarantee with no consumer is a guarantee that rots.
        return _budget(keep, now)
