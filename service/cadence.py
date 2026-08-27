"""
cadence.py — a supervisor loop's sleep floor, made structural.

On 2026-08-21 `transaction_ingest`'s cycle computed its own delay as
`interval - elapsed`. When a cycle overran its interval that expression went
NEGATIVE, `asyncio.sleep()` returned immediately, and the loop spun at full
tilt. It held the box at 100% CPU for ~4 hours, at which point Hostinger
applied a CPU cap — and that cap outlived the bug by ~18 hours, because
throttles do not lift when you fix the cause.

The bug was one missing `max()`. That is the point: the failure was not
subtle, it was unguarded. This module makes the guard the default so the
next loop cannot be written without one.

    from cadence import cadence

    while True:
        async with cadence(interval_s=30, floor_s=5, name="my-supervisor"):
            await do_one_cycle()

Two properties worth stating, because both are load-bearing:

1. **The floor holds even when the body raises.** The nastiest spin is not a
   slow cycle, it is an exception thrown immediately on every pass — a retry
   loop with no backoff is a busy-wait with extra steps. The sleep lives in a
   `finally`, so a body that fails in 2ms still yields for `floor_s`.

2. **Cancellation is never delayed.** Awaiting inside `finally` during
   shutdown would hold the loop open for a full interval and make container
   stops hit the SIGKILL grace instead of exiting cleanly, so `CancelledError`
   skips the sleep and propagates immediately.

`service/tests/test_loop_cadence_guard.py` fails the build if anyone
reintroduces a bare `sleep(interval - elapsed)` anywhere under service/.
"""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager

log = logging.getLogger("of-relay.cadence")

# Nothing in this codebase legitimately wants a supervisor cycle tighter than
# this. A loop asking for less is either a bug or wants an event, not a poll.
DEFAULT_FLOOR_S = 1.0


def next_delay(cycle_started: float, interval_s: float,
               floor_s: float = DEFAULT_FLOOR_S) -> float:
    """Seconds to sleep after a cycle that began at `cycle_started`
    (a `time.monotonic()` stamp), never less than `floor_s`.

    Pure and synchronous so it can be unit-tested without a loop."""
    elapsed = time.monotonic() - cycle_started
    return max(floor_s, interval_s - elapsed)


@asynccontextmanager
async def cadence(interval_s: float, *, floor_s: float = DEFAULT_FLOOR_S,
                  name: str = ""):
    """Hold a loop to `interval_s` per pass, never faster than `floor_s`."""
    if floor_s <= 0:
        raise ValueError("floor_s must be > 0 — a zero floor is the bug this "
                         "module exists to prevent")
    started = time.monotonic()
    cancelled = False
    try:
        yield
    except asyncio.CancelledError:
        cancelled = True
        raise
    finally:
        if not cancelled:
            elapsed = time.monotonic() - started
            delay = max(floor_s, interval_s - elapsed)
            if elapsed > interval_s and name:
                # An overrun is not itself an error, but a loop that overruns
                # EVERY pass is running flat out at its floor and is worth
                # seeing before it becomes an incident.
                log.warning("cadence_overrun loop=%s elapsed=%.1fs interval=%.1fs "
                            "— holding the %.1fs floor", name, elapsed, interval_s, floor_s)
            await asyncio.sleep(delay)


# A poll loop's first cycle is its most expensive one and it lands at the worst
# possible moment. See `boot_grace`.
DEFAULT_BOOT_GRACE_S = 0.0


async def boot_grace(delay_s: float, *, name: str = "") -> None:
    """Hold a poll loop's FIRST cycle for `delay_s` seconds after process start.

    `cadence` bounds the gap BETWEEN cycles. This bounds the one cycle it
    cannot see: the one that starts before anything else has settled. At t=0
    the relay is spawning N WS pumps, the pump supervisor, the automation
    executor's immediate first drain and a fleet-wide scrape_chats seed — all
    onto the same two cores. A poll loop that fires a full-fleet walk into
    that contends with its own startup.

    That is not hypothetical. On 2026-08-21 three restarts inside ~64 min each
    handed the 30s ingest fast poll a fleet walk it could not finish inside its
    budget; the box went to 100% CPU and Hostinger applied a cap that outlived
    the fix by ~18 hours. The floor in `cadence` stops a loop that overruns
    from spinning; it does nothing about starting into a thundering herd.

    Yields immediately when `delay_s <= 0`, so a caller can disable the grace
    by config without branching. `CancelledError` propagates — a container stop
    during the grace exits at once instead of holding for the full delay."""
    if delay_s <= 0:
        return
    if name:
        log.info("boot_grace loop=%s holding %.0fs before first cycle", name, delay_s)
    await asyncio.sleep(delay_s)
