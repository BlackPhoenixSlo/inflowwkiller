"""
service/automations/_run_pool.py — run one coroutine per item, several at once,
under a wall-time ADMISSION budget.

Built for `send_welcome`'s paced bursts (plans/welcome-pacing §A3): a paced
burst is ~85-99s of mostly sleeping, and a run holds one of the executor's four
GLOBAL slots for its WALL time, so serialising the bursts would price a 25-fan
backlog at about an hour of pinned slot. Nothing in here is welcome-specific —
the item, the worker and the per-item start jitter are the caller's.

THE CONTRACT:
  • at most `concurrency` workers in flight; each waits `jitter_for(item)`
    seconds before it starts, so N first sends do not fire in the same second.
  • the budget is checked BEFORE an item starts, never during: an over-budget
    run stops admitting and the in-flight workers finish, so the overshoot is
    bounded by one worker. Items never admitted are counted in `deferred` —
    the caller decides what a deferred item means (for a welcome: no
    `welcome_sent` claim, the next tick re-serves him).
  • one worker's escape never kills the others; its exception is collected in
    `errors` and the pool still returns. A CancelledError from any worker is
    re-raised, so the executor parks and requeues the run instead of
    finalising a half-executed batch as `ok`.
  • a cancel that lands while the pool is parked on admission takes every
    spawned worker down with it AND waits for them to unwind before re-raising
    — each worker's `finally` (the one that hands a fan lease back) runs on a
    later loop turn that a shutdown may never take otherwise.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TypeVar

log = logging.getLogger("of-relay.automation.run_pool")

T = TypeVar("T")

# The start jitter is a plain wait — nothing is typed, nothing is held — so it
# is a plain sleep. Module-level so the tests can make it instant.
_sleep = asyncio.sleep


@dataclass(slots=True)
class PoolResult:
    deferred: int = 0                                  # never admitted (budget)
    errors: list[BaseException] = field(default_factory=list)


async def run_paced(
    items: list[T], worker: Callable[[T], Awaitable[None]], *,
    concurrency: int, wall_budget_s: float, started_at: float,
    jitter_for: Callable[[T], float],
    clock: Callable[[], float] = time.monotonic,
) -> PoolResult:
    """Run `worker(item)` for every item, `concurrency` at a time, admitting
    nothing once `clock() - started_at` exceeds `wall_budget_s`.

    `clock` and `started_at` share one timebase (the caller's `time.monotonic`
    by default) so a run's own wall clock and the budget cannot disagree."""
    sem = asyncio.Semaphore(concurrency)
    tasks: list[asyncio.Task] = []
    result = PoolResult()

    async def _holding_the_slot(item: T) -> None:
        """THE CALLER HAS ALREADY ACQUIRED `sem`; this coroutine owns releasing
        it, on every path including cancellation. Acquiring is the ADMISSION
        point and happens in the loop below, where the budget is a real
        elapsed measurement rather than zero; releasing happens where the work
        ends."""
        try:
            await _sleep(jitter_for(item))
            await worker(item)
        finally:
            sem.release()

    try:
        for i, item in enumerate(items):
            # ADMISSION CONTROL, in wall time. The first `concurrency` items
            # start at once and the next one waits here.
            await sem.acquire()
            if clock() - started_at > wall_budget_s:
                # The one place the loop releases a slot it acquired: this item
                # never starts, so the task that would have owned the release
                # is never created.
                sem.release()
                result.deferred = len(items) - i
                break
            tasks.append(asyncio.create_task(_holding_the_slot(item)))
    except asyncio.CancelledError:
        # Cancelled while parked on sem.acquire(). The tasks already spawned
        # are detached and would otherwise run to completion AFTER this
        # function raises. A task not yet inside a blocking call STOPS at its
        # next await; one already inside `asyncio.to_thread` does not — the
        # thread is not interruptible — which is why the pool must still
        # RAISE: the executor requeues, and the caller's own dedup decides
        # what a re-run re-serves.
        for task in tasks:
            task.cancel()
        # …and WAIT for them to actually unwind: `task.cancel()` only schedules
        # the CancelledError. `return_exceptions=True` so one child's escape
        # cannot stop the others being collected; the second `except` covers
        # being cancelled AGAIN while collecting — the original cancel is what
        # is re-raised either way.
        if tasks:
            try:
                await asyncio.gather(*tasks, return_exceptions=True)
            except asyncio.CancelledError:
                pass
        raise
    if tasks:
        for res in await asyncio.gather(*tasks, return_exceptions=True):
            # CancelledError is NOT an Exception, so return_exceptions=True
            # hands it back as an ordinary result. Re-raising is what makes a
            # half-executed batch get retried instead of recorded as a clean
            # success.
            if isinstance(res, asyncio.CancelledError):
                raise res
            if isinstance(res, BaseException):
                result.errors.append(res)
    return result
