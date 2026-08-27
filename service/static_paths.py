"""static_paths.py — the two mounted static frontends, and the middleware
exemption that belongs with them.

Both hand-written UIs are served by `NoCacheStatic` mounts in server.py, and
both are `no-store` on purpose: they ship unhashed, so a cached copy serves
ancient JS after a deploy. That policy is correct and stays. Its cost is that
NOTHING is reused across navigations — every asset of every page is refetched
every time, and each refetch is an ordinary request through the whole global
middleware chain.

Two links in that chain open a SQLite session on any request carrying a cookie:
`auth.session_middleware` (a `User` select plus `_master_account_ids`, which is
explicitly "recomputed each request") and `chatters.chatter_session_middleware`
(a `Chatter` select plus a throttled `last_seen_at` UPDATE). A file mount can
use neither answer — it reads the filesystem and never looks at a principal.

That was the amplifier on 2026-08-21: an unbounded loop in transaction_ingest
starved the SQLite writer, at which point those middlewares' `last_seen_at`
UPDATE sat on the 30 s busy_timeout and surfaced as 500s on real API calls. Its
throttle cannot re-arm while the write keeps failing, so contention promoted
itself from intermittent to permanent. The loop was the cause; this was the
multiplier, and 129 uncacheable files across 54 pages is a large multiplier.

⚠️ THE SHARE-TOKEN GATE IS NOT IN SCOPE HERE and must never be. It is
registered LAST in server.py, which makes it OUTERMOST (Starlette runs
user-added middlewares in reverse registration order), so it has already
accepted or refused the request before any exempted middleware would have run.
Skipping work *inside* the gate changes nothing about who may fetch these files.

Verified 2026-08-22: `/ui` and `/infloww` are `app.mount(...)` targets and
NOTHING else — no route, no router prefix, is declared under either. If that
ever stops being true, this predicate stops being safe and the new route has to
move or be excluded explicitly.
"""
from __future__ import annotations

import functools
from typing import Awaitable, Callable

from starlette.requests import Request
from starlette.responses import Response

# Mount points, WITHOUT the trailing slash — see the predicate for why both
# forms have to be accepted.
_MOUNTS = ("/ui", "/infloww")

CallNext = Callable[[Request], Awaitable[Response]]
HttpMiddleware = Callable[[Request, CallNext], Awaitable[Response]]


def is_static_asset(path: str) -> bool:
    """True for the two static mounts and everything under them.

    The bare mount point counts: a request for `/infloww` (no trailing slash)
    is answered by Starlette's Mount with a redirect to `/infloww/`, which is
    just as unable to use a principal as the file it points at.

    Prefix matching is on `<mount>/` and not on `<mount>` so a future route
    named `/uix` or `/inflowwadmin` cannot be swallowed by the mount beside it.
    """
    return any(path == m or path.startswith(m + "/") for m in _MOUNTS)


def exempt_static(middleware: HttpMiddleware) -> HttpMiddleware:
    """Wrap a global http middleware so it does not run for the static mounts.

    Applied at the REGISTRATION SITE in server.py, which is also the file that
    declares the mounts — so the exemption sits next to the thing it describes,
    exists once rather than once per middleware, and neither auth.py nor
    chatters.py has to learn the frontend's URL layout to get the benefit.
    Adding a third exempt middleware is one more wrap, not another copy of a
    guard clause.

    Registration ORDER is untouched, which matters: server.py's ordering
    comments are load-bearing (Starlette runs user-added middlewares in reverse
    registration order, and the isolation gate depends on running after both
    session middlewares have set their principal).
    """
    @functools.wraps(middleware)
    async def _exempt(request: Request, call_next: CallNext) -> Response:
        if is_static_asset(request.url.path):
            return await call_next(request)
        return await middleware(request, call_next)

    return _exempt
