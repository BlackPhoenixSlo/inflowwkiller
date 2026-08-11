"""
vault_sweep.py — the one way a vault sweep walks a list of media ids.

`vault_ai_api` grew four of these: a `_X_running: set[str]`, a `_X_progress:
dict[str, dict]`, a 409 on double-start, and `GET /admin/vault-ai/<kind>/status`
returning `{running, progress}`. The CLIENT noticed first and collapsed its half
into one hook — `app/hooks/useVaultSweep.ts` opens by saying "The server runs
several of these and three are literally the same object". The server never
followed, so `_run_describe_all` and `_run_flags_all` carried the same
semaphore → per-item → chunk → gather skeleton twice.

That cost real money on 2026-08-11: the per-item call was unguarded in BOTH, and
one item that RAISED (rather than returning a status) propagated out of the
gather and ended the whole pass — an operator re-pressing "Describe all" every
few minutes for an hour, ~47% of it stopped. Fixing it in two places would have
left the next fault class to be fixed in one and not the other, which is how the
two drifted in the first place (they disagreed about what `done` counts).

WHAT A `visit` MUST RETURN. A dict — the shape `_describe_one` / `_flags_one`
already return, so nothing had to change to adopt this:

    ok               truthy ⇒ the item was processed. Anything else is a failure.
    status           "capped" ⇒ the day's budget is gone; the sweep STOPS. Every
                     other value is informational.
    detail           why it failed, for the operator. The FIRST one is kept.
    cost_millicents  optional; summed across the run.

A `visit` that raises is a failure like any other — it does not end the sweep.
The reason it is counted rather than propagated is that the alternative was
measured and it was terrible: the item that raises writes no row, so it reads as
never-attempted forever, and the 49 other items in its chunk are abandoned
mid-flight. Pinned by `test_vault_ai_describe.case_one_exploding_item_does_not_
end_the_sweep`.

`done` COUNTS ITEMS VISITED, NOT ITEMS THAT SUCCEEDED. The two sweeps used to
disagree about this — describe counted visits, flags counted successes — while
the shared frontend rendered both as "done/total", so a flags pass that finished
with 13 failures parked at 487/500 forever. Visits is the answer that makes
"done == total" mean "this pass is over", which is the only question the counter
is ever asked. `failed` carries the rest.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any, NamedTuple

log = logging.getLogger("of-relay.vault_sweep")

# Items dispatched per `gather`. Bounded so a 2,000-item vault doesn't spawn
# 2,000 tasks at once; the semaphore bounds how many actually RUN.
CHUNK = 50


class SweepResult(NamedTuple):
    """What a finished sweep amounts to. The same fields the progress dict
    publishes, so a caller logging the outcome and a UI polling mid-run are
    reading one set of definitions."""

    total: int
    done: int          # VISITED — see the module docstring
    failed: int
    capped: bool
    cost_millicents: int
    error: str         # the first `detail` a failure carried


async def run(
    ids: list[int],
    visit: Callable[[int], Awaitable[dict[str, Any]]],
    *,
    concurrency: int,
    gate: Callable[[], asyncio.Semaphore],
    into: dict[str, dict[str, Any]],
    key: str,
    label: str,
) -> SweepResult:
    """Visit every id, `concurrency` at a time, publishing progress as it goes.

    `gate` is called (not passed) so a process-wide semaphore can bind lazily to
    the running loop. `into[key]` is rewritten after every item — that dict is
    what the status endpoint serves, and it deliberately outlives the sweep so a
    UI arriving late still learns how the last pass ended.
    """
    total = len(ids)
    done = failed = cost = 0
    capped = False
    first_error = ""

    def _publish() -> None:
        into[key] = {"total": total, "done": done, "failed": failed,
                     "capped": capped, "cost_millicents": cost,
                     "error": first_error}

    _publish()
    sem = asyncio.Semaphore(concurrency)

    async def _one(mid: int) -> None:
        nonlocal done, failed, cost, capped, first_error
        async with sem, gate():  # per-sweep bound + process-wide gate
            if capped:
                return
            try:
                res = await visit(mid)
            except Exception as e:  # noqa: BLE001 — one bad item costs ONE item
                log.exception("%s: item raised media=%s", label, mid)
                res = {"ok": False, "status": "error",
                       "detail": f"{type(e).__name__}: {e}"[:200]}
            if res.get("status") == "capped":
                capped = True
            if not res.get("ok"):
                failed += 1
                if not first_error and res.get("detail"):
                    first_error = str(res["detail"])[:200]
            cost += int(res.get("cost_millicents") or 0)
            done += 1
            _publish()

    for i in range(0, total, CHUNK):
        if capped:
            break
        # `return_exceptions` is the backstop behind `_one`'s own guard: a bare
        # gather re-raises the first failure the instant it happens and abandons
        # the rest of the chunk as orphans. Nothing about one item should be able
        # to reach the caller.
        await asyncio.gather(*(_one(m) for m in ids[i:i + CHUNK]),
                             return_exceptions=True)
    return SweepResult(total, done, failed, capped, cost, first_error)
