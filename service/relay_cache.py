"""
relay_cache.py — in-process TTL cache with explicit WS-driven invalidation.

Sister of `vault_cache.py`, but tuned for hot-path OF endpoint responses
(/api/of/v2/init, list_chats, notifications_count, ...). Differences from
vault_cache:

  * vault_cache persists to SQLite (cross-restart, cross-employee). This
    module is RAM-only — fastest possible hit (no row decode, no I/O) at
    the cost of losing state on process restart.
  * vault_cache is content-driven (TTL only). This module supports
    explicit invalidation on WS events: when the OF realtime channel
    tells us a chat got a new message, the transcoder calls
    `invalidate("list_chats", account_id)` and the next caller goes
    upstream.
  * Single-flight on misses via a per-key `asyncio.Lock`, so N concurrent
    callers on a cold key produce ONE upstream fetch (not N).

Namespacing
-----------
Cache keys are tuples: `(namespace, account_id, key_extras)`. The
`namespace` is the logical endpoint label (e.g. "list_chats"). The
`account_id` is always included so an invalidation for one OF account
can never touch another. `key_extras` is a tuple of the per-call
parameters that affect the response (limit, offset, filter, ...).

If a Stage C call-site caches a response that varies by employee
(anything that the upstream serves differently per X-Employee-Id), the
employee_id MUST be included in `key_extras` — this module is
account-scoped, not employee-scoped.

Single-flight vs multi-worker
-----------------------------
Coalescing is process-local. Running uvicorn with `--workers > 1` gives
each worker its own cache + lock dict; invalidation in one worker does
not propagate to the others. The relay runs single-worker by design
(see Dockerfile: SQLite write-lock + WS pump singleton), so this is not
a problem today. If multi-worker is ever introduced, swap this module
for a Redis-backed implementation — the public API stays the same.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable

log = logging.getLogger("of-relay.relay_cache")

# Cache key shape: (namespace, account_id, key_extras_tuple).
# Cache value shape: (value, expires_at_monotonic).
_cache: dict[tuple[str, str, tuple], tuple[Any, float]] = {}

# Invalidation generation per (namespace, account_id). A fetch snapshots the
# generation BEFORE going upstream and only writes its result back if no
# invalidation bumped it meanwhile — otherwise an in-flight fetch that read
# upstream BEFORE a state change (e.g. mark-as-read, a WS-signalled message)
# resolves after the invalidation and silently re-caches the pre-change
# snapshot for a full TTL ("zombie" entries the invalidation was meant to
# kill). Pair-level (not per-key) so a fetch on a key with no cached entry
# yet is covered too. Bounded by namespace x account count.
_pair_gen: dict[tuple[str, str], int] = {}


def _bump_gen(namespace: str, account_id: str) -> None:
    pair = (namespace, account_id)
    _pair_gen[pair] = _pair_gen.get(pair, 0) + 1

# Per-key locks, only used on the miss path. Guarded by `_registry_lock`
# so two coroutines can't both create + register a fresh lock for the
# same key.
_locks: dict[tuple[str, str, tuple], asyncio.Lock] = {}
_registry_lock = asyncio.Lock()

# Per-namespace hit/miss counters for /admin/_debug/relay-cache-stats.
_hits: dict[str, int] = {}
_misses: dict[str, int] = {}


def _record_hit(namespace: str) -> None:
    _hits[namespace] = _hits.get(namespace, 0) + 1


def _record_miss(namespace: str) -> None:
    _misses[namespace] = _misses.get(namespace, 0) + 1


async def _get_or_make_lock(key: tuple[str, str, tuple]) -> asyncio.Lock:
    async with _registry_lock:
        lock = _locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _locks[key] = lock
        return lock


async def get_or_fetch(
    namespace: str,
    account_id: str,
    key_extras: tuple,
    ttl_seconds: float,
    fetcher: Callable[[], Awaitable[Any]] | Callable[[], Any],
    *,
    bypass: bool = False,
) -> Any:
    """Return a cached value or fetch + cache.

    `fetcher` may be sync OR async. Sync fetchers are dispatched via
    `asyncio.to_thread` so they don't block the event loop — this lets
    call-sites pass `lambda: _proxy(...)` directly without wrapping.

    `bypass=True` (used by `?refresh=1` callers) skips the cache read
    but still writes the fresh result back, so the next non-bypass
    caller sees it.

    Exceptions from `fetcher` propagate; nothing is cached on failure.
    """
    key = (namespace, account_id, tuple(key_extras))

    # Fast path: cache hit without acquiring any lock.
    if not bypass:
        entry = _cache.get(key)
        if entry is not None:
            value, expires_at = entry
            if expires_at > time.monotonic():
                _record_hit(namespace)
                return value

    # Miss path: acquire per-key lock so N concurrent callers on the
    # same cold key produce ONE upstream fetch.
    lock = await _get_or_make_lock(key)
    async with lock:
        # Re-check the cache after acquiring the lock. A peer coroutine
        # may have already populated it while we were queued.
        if not bypass:
            entry = _cache.get(key)
            if entry is not None:
                value, expires_at = entry
                if expires_at > time.monotonic():
                    _record_hit(namespace)
                    return value

        _record_miss(namespace)
        gen0 = _pair_gen.get((namespace, account_id), 0)
        if asyncio.iscoroutinefunction(fetcher):
            value = await fetcher()
        else:
            value = await asyncio.to_thread(fetcher)

        # Latest-wins: skip the write-back if an invalidation landed while we
        # were upstream — our snapshot predates it. The caller still gets the
        # value it fetched; the NEXT reader misses and re-fetches post-change
        # state. (See _pair_gen.)
        if _pair_gen.get((namespace, account_id), 0) == gen0:
            _cache[key] = (value, time.monotonic() + float(ttl_seconds))
        return value


def invalidate(
    namespace: str,
    account_id: str,
    *,
    match: Callable[[tuple], bool] | None = None,
) -> int:
    """Drop cached entries for (namespace, account_id).

    With `match=None`, drops every entry for that pair. Otherwise drops
    only entries whose `key_extras` tuple satisfies `match(extras)`.

    Sync (no I/O) so the transcoder can call it right after
    `await s.commit()` without an extra await. Returns the count removed.

    Note on ordering — the transcoder MUST call this AFTER committing
    its DB writes. If it runs before the commit, a concurrent reader
    can miss-the-cache, re-fetch the pre-commit upstream state, and
    cache it.
    """
    # Bump the pair generation FIRST — even when nothing is currently cached —
    # so any in-flight fetch under this pair skips its write-back instead of
    # re-caching pre-invalidation state (see _pair_gen). Locks are deliberately
    # NOT popped: dropping a live lock would let a second coroutine mint a
    # fresh one and defeat single-flight; the dict is bounded by keyspace.
    _bump_gen(namespace, account_id)
    removed = 0
    # Two passes to avoid mutating during iteration.
    to_drop: list[tuple[str, str, tuple]] = []
    for key in _cache.keys():
        ns, aid, extras = key
        if ns != namespace or aid != account_id:
            continue
        if match is not None and not match(extras):
            continue
        to_drop.append(key)
    for key in to_drop:
        _cache.pop(key, None)
        removed += 1
    if removed:
        log.debug(
            "relay_cache.invalidate ns=%s account=%s removed=%d",
            namespace, account_id, removed,
        )
    return removed


# The namespaces whose bodies show MONEY — see `invalidate_money_feeds`.
_MONEY_FEED_NAMESPACES = ("notifications", "notifications_count", "init")


def invalidate_money_feeds(account_id: str) -> int:
    """Drop every cached view of an account's money.

    OF serves three facts off one upstream state — the notifications feed,
    its per-category badge counts, and the counts embedded in the /init
    bootstrap — so a tip, a subscribe or a PPV unlock changes all three at
    once. Callers are the money-event paths: an inbound tip and a `toasts`
    frame (`event_transcoder`), and a PPV purchase
    (`purchase_notifications._broadcast_purchase`). Each is the server-side
    twin of a client-side `["notif-list", aid]` invalidation in
    `NotificationToaster` — the browser refetches the moment it hears the
    event, so if the relay does not drop its own entry on the SAME event,
    that refetch just re-reads our cached body and the feed lags the toast
    by a whole TTL.

    One named set rather than three `invalidate()` calls per site, because
    the three are one fact. Re-deriving the set at each event path is how
    the purchase lane — the only one that fires in production — ended up
    busting the feed but neither the badge nor `init`, and `init` has a
    5-minute TTL. A named set has one place to be wrong.

    Sync (RAM-only), like `invalidate`. Returns the total count removed.
    """
    removed = 0
    for namespace in _MONEY_FEED_NAMESPACES:
        removed += invalidate(namespace, account_id)
    return removed


def invalidate_account(account_id: str) -> int:
    """Drop EVERY cached entry for an account across ALL namespaces.

    `invalidate()` is namespace-scoped; this is the account-wide variant
    used when an account is removed (or re-added) so no hot-path cache
    (list_chats, my_settings, init, notifications, ...) can serve a
    dropped account's data to a later caller — or leak a prior session's
    data if the same account_id is re-captured. Strictly scoped to the
    one account_id, so it never touches another account's entries.

    Sync (RAM-only), like `invalidate`. Returns the count removed.
    """
    to_drop = [key for key in _cache.keys() if key[1] == account_id]
    # Bump every namespace seen for this account (cached OR in flight via the
    # lock registry) so no in-flight fetch writes back after the wipe.
    for ns in {key[0] for key in to_drop} | {key[0] for key in _locks if key[1] == account_id}:
        _bump_gen(ns, account_id)
    for key in to_drop:
        _cache.pop(key, None)
    if to_drop:
        log.debug(
            "relay_cache.invalidate_account account=%s removed=%d",
            account_id, len(to_drop),
        )
    return len(to_drop)


def stats() -> dict[str, Any]:
    """Snapshot of hit/miss counters + current entry counts.

    Exposed via /admin/_debug/relay-cache-stats. Useful for tuning TTLs
    once Stage C wires real namespaces in — a namespace with 0 hits
    after a warm-up window is either keyed wrong (per-employee data
    cached account-wide?) or has TTL=0 / always-bypass callers.
    """
    namespaces = set(_hits) | set(_misses)
    per_ns: dict[str, dict[str, int]] = {}
    for ns in namespaces:
        per_ns[ns] = {
            "hits": _hits.get(ns, 0),
            "misses": _misses.get(ns, 0),
        }
    entries_per_ns: dict[str, int] = {}
    for (ns, _aid, _extras) in _cache.keys():
        entries_per_ns[ns] = entries_per_ns.get(ns, 0) + 1
    return {
        "namespaces": per_ns,
        "entries": entries_per_ns,
        "total_entries": len(_cache),
        "total_locks": len(_locks),
    }
