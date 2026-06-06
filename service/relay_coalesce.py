"""In-flight request coalescer for idempotent OF GETs.

Collapses concurrent identical in-flight relay GETs onto a single
upstream OF call. This is **dedup, not caching** — each unique request
still hits OF once. The win is when multiple chatters (or multiple
tabs/popouts of the same chatter) request the same data within a
100-500ms window.

The coalescer sits OUTSIDE the per-account priority lane in
`server._proxy`, so waiters joining an in-flight request do NOT hold
a semaphore slot each — only the original fetcher does.

────────────────────────────────────────────────────────────────────────
DO NOT coalesce a handler if any of these hold:
  • body (or any helper it calls) reads `X-Employee-Id`
  • body calls `attribution.resolve()` (service/attribution.py:51)
  • body calls `resolve_employee_id` / `_employee_for_request`
    (service/employees.py:559+)
  • any POST/PUT/PATCH/DELETE
  • response carries per-caller context (e.g. /admin/errors,
    /employees/whoami, /admin/stats/*, /admin/automations/*)
  • send/queue endpoints — must run per-chatter for attribution

Known X-Employee-Id read sites (grep `service/`):
  server.py:1477, 2738, 2745, 2841, 2888, 2899
  messages.py:131, 143, 146
  employees.py:530, 542, 559, 782, 833, 860, 886, 985
  attribution.py:51
────────────────────────────────────────────────────────────────────────

Allowlist lives in `service/server.py` next to each wrapped endpoint —
this is an explicit per-endpoint list, NOT a `@coalesce` decorator that
auto-applies to GETs. A blanket decorator is a footgun for attribution.
"""

from __future__ import annotations

import asyncio
import copy
import threading
from typing import Any, Awaitable, Callable, TypeVar

T = TypeVar("T")

# Keyed by the full tuple supplied by the caller. The first element is
# the namespace ("list_chats", "list_users", ...) used for stats.
_inflight: dict[tuple, asyncio.Future] = {}

# Stats: number of waiters that joined an already-in-flight request,
# per namespace. The original (first) caller does NOT increment this —
# only joiners do. Read via `/admin/coalesce/stats`.
_coalesced_hits: dict[str, int] = {}
_first_calls: dict[str, int] = {}

# Guards `_coalesced_hits` and `_first_calls`. The `_inflight` dict is
# only mutated from inside the event loop, so it doesn't need a lock.
_stats_lock = threading.Lock()


def _bump_hit(namespace: str) -> None:
    with _stats_lock:
        _coalesced_hits[namespace] = _coalesced_hits.get(namespace, 0) + 1


def _bump_first(namespace: str) -> None:
    with _stats_lock:
        _first_calls[namespace] = _first_calls.get(namespace, 0) + 1


def stats() -> dict[str, Any]:
    """Snapshot of coalescer activity for `/admin/coalesce/stats`."""
    with _stats_lock:
        return {
            "coalesced_hits": dict(_coalesced_hits),
            "first_calls": dict(_first_calls),
            "inflight": len(_inflight),
            "inflight_keys": [k[0] for k in _inflight.keys()],
        }


async def coalesce(key: tuple, fetcher: Callable[[], Awaitable[T]]) -> T:
    """If a fetcher is already in flight for `key`, await its result.
    Otherwise start one. Each waiter gets a `copy.deepcopy` of the
    result so post-return mutations by one waiter can't leak into
    another waiter's response.

    Exceptions from the fetcher propagate to ALL waiters via
    `future.set_exception(e)`.

    The fetcher runs in a detached `asyncio.create_task` so that
    cancellation of the original awaiter does NOT abort the upstream
    call — other waiters still get a result.

    `key` must be a hashable tuple. By convention, key[0] is the
    namespace label (used for stats grouping) and the remaining slots
    fully identify the request, INCLUDING account_id — otherwise two
    chatters on different OF accounts would share a response."""
    assert isinstance(key, tuple) and len(key) >= 1, "key must be a non-empty tuple"
    namespace = str(key[0])

    existing = _inflight.get(key)
    if existing is not None:
        _bump_hit(namespace)
        # `shield` so a cancelled waiter doesn't propagate `.cancel()` to
        # the shared future (which would knock out every other waiter).
        result = await asyncio.shield(existing)
        return copy.deepcopy(result)

    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()
    _inflight[key] = fut
    _bump_first(namespace)

    async def _run() -> None:
        try:
            value = await fetcher()
            if not fut.done():
                fut.set_result(value)
        except BaseException as e:  # noqa: BLE001 — propagate ALL to waiters
            if not fut.done():
                fut.set_exception(e)
        finally:
            # Clear the in-flight entry only if it still points to us.
            # If a fast retry happened after set_result, _inflight may
            # already hold a fresh future — don't clobber it.
            if _inflight.get(key) is fut:
                _inflight.pop(key, None)

    asyncio.create_task(_run())
    # Leader uses shield too — if the request that kicked off the fetch
    # is cancelled, the detached fetcher task must keep running so the
    # joined waiters still get a result.
    result = await asyncio.shield(fut)
    return copy.deepcopy(result)


def reset_for_tests() -> None:
    """Wipe state. Only call from test scripts."""
    _inflight.clear()
    with _stats_lock:
        _coalesced_hits.clear()
        _first_calls.clear()
