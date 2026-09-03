"""
The relay's one OFClient pool.

Why this module exists: `OFClient.__init__` opens a curl_cffi Session and the
class has no usable `close()` — curl_cffi 0.15.0's `Session.close()` closes the
*calling thread's* handle (and creates one if that thread has none), so an
evicting thread cannot release handles the worker threads opened. A client per
call was therefore an fd per call. Under a proxy swap every IP-signed vault url
died at once, every tile took the re-sign path, and the relay hit EMFILE.

So clients are pinned per account. Steady-state handle count is bounded by
(accounts x threadpool size) instead of growing with request count.

It lives in its own module rather than in `server.py` or
`automation_executor.py` because both of those need it and importing either
from the other is a cycle. It previously lived in both — `server._clients` for
HTTP routes and nothing at all for the automation lane — which meant an
invalidation had to remember two homes. There is one home now.

Eviction is the price of pinning: a pinned client holds the session cookies,
`x_of_rev` and `x_hash` it was built with, so anything that changes what's on
disk (re-bootstrap, proxy edit, re-capture) must evict. See `evict()`.
"""

from __future__ import annotations

import logging
import threading

from of_client import OFClient

log = logging.getLogger("of-relay.client_pool")

_POOL: dict[str, OFClient] = {}
# Guards the miss branch only. 50-odd call sites reach `get()` through
# `asyncio.to_thread`, and `from_account()` does disk I/O — so without this,
# a cold pool under a stampede (exactly the post-proxy-swap case) lets N
# threads each build a client and orphan N-1 of them, unclosed.
_LOCK = threading.Lock()


def get(account_id: str) -> OFClient:
    """The pinned OFClient for `account_id`, built on first use.

    Raises whatever `OFClient.from_account` raises (FileNotFoundError for a
    missing session, ValueError for incomplete signing rules) — callers that
    face a user translate those; the pool has no opinion. A failed build is
    not cached.
    """
    c = _POOL.get(account_id)
    if c is not None:
        return c
    with _LOCK:
        # Re-check: another thread may have built it while we waited.
        c = _POOL.get(account_id)
        if c is None:
            c = _POOL[account_id] = OFClient.from_account(account_id)
            log.info("OFClient ready (account=%s user_id=%s rev=%s)",
                     account_id, c.user_id, c.x_of_rev)
    return c


def evict(account_id: str | None = None) -> None:
    """Drop pooled client(s) so the next `get()` re-reads session + proxy from
    disk. `None` evicts every account — a proxy edit can affect any of them.

    Explicitly `is None` and not a truthiness test: the failure mode of a
    falsy-but-not-None argument slipping through is silently dumping every
    account's client, which is the worst possible reading of a typo.
    """
    if account_id is None:
        _POOL.clear()
    else:
        _POOL.pop(account_id, None)
    # Fansly accounts are pooled in fansly_backend, not here (their client is
    # the shim, not an OFClient). Invalidation still has ONE home — this
    # function — so a re-capture or proxy edit can't leave a stale shim behind
    # for the lane that happens to call the other module. Imported lazily and
    # guarded: a deployment without the sibling `fansly/` package has no Fansly
    # pool to drop, and eviction must not fail because of it.
    try:
        import fansly_backend
    except Exception:
        return
    fansly_backend.drop(account_id)
