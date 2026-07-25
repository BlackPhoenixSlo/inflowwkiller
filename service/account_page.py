"""service/account_page.py — is this creator's OF page FREE or PAID?

A FREE page sells on the wall: a "paid post" is free preview + locked media,
and that is how `auto_posts` / `ppv_send`'s feed lane / the Vault-AI arc make
money outside DMs. A page with a SUBSCRIPTION PRICE cannot do that — the sub
IS the paywall, so a priced feed post is not an option OF offers her. Anything
that posts to the wall therefore has to know which kind of page it posts to,
and this module is the ONE place that answers.

GROUND TRUTH IS ONE FIELD: `subscribePrice` on GET /users/me (dollars, 0 on a
free page). Probed live across the whole roster 2026-07-24:

    paidA        subscribePrice 5      paidFeed True    ← paid page
    paidB        subscribePrice 19.99  paidFeed False   ← paid page
    freeA        subscribePrice 0      paidFeed False   ← free page
    freeB / freeC        subscribePrice 0               ← free pages

so `paidFeed` is NOT the flag (it disagrees with itself across two paid
pages), and postMinPrice/postMaxPrice/canChangeContentPrice read 3/100/True on
paid and free pages alike — OF advertises the restriction nowhere. The `posts`
table corroborates from the other side: every account that has ever recorded a
priced post is a subscribePrice==0 page, and a paid page's 820 posts contain zero.

UNKNOWN IS NOT PAID. A dead session or an OF blip yields None, and then every
caller keeps its CURRENT behavior (post as asked). Failing that way costs one
rejected OF call on a paid page; failing the other way would quietly stop a
FREE page from selling on its wall, which is real money and nothing in the
logs would explain it.

Cached in-process — no schema change, because the answer changes about never
(a creator flips her page maybe once). `prime()` fills the cache for free from
a /users/me dict a caller already holds; `invalidate()` drops it after a
profile PATCH.
"""
from __future__ import annotations

import asyncio
import logging
import time

log = logging.getLogger("of-relay.account-page")

# The skip reason every feed path reports when it declines to post. One string
# so the UI, the logs and the tests all match on the same token.
PAID_PAGE_SKIP = "paid_page"

# A known answer is good for hours (a page's tier is near-static); an UNKNOWN
# one expires fast so an account whose session was dead for one probe starts
# being gated again as soon as it recovers.
_TTL_KNOWN_S = 6 * 3600
_TTL_UNKNOWN_S = 300

# account_id -> (expires_at_monotonic, subscribe_price_cents | None)
_cache: dict[str, tuple[float, int | None]] = {}
_locks: dict[str, asyncio.Lock] = {}


def _store(account_id: str, cents: int | None) -> int | None:
    ttl = _TTL_KNOWN_S if cents is not None else _TTL_UNKNOWN_S
    _cache[str(account_id)] = (time.monotonic() + ttl, cents)
    return cents


def _price_cents(profile: object) -> int | None:
    """`subscribePrice` (dollars) out of a /users/me dict → cents, or None when
    the field is absent/non-numeric (a bool is not a price)."""
    if not isinstance(profile, dict):
        return None
    raw = profile.get("subscribePrice")
    if raw is None or isinstance(raw, bool):
        return None
    try:
        return int(round(float(raw) * 100))
    except (TypeError, ValueError):
        return None


def prime(account_id: str, profile: object) -> None:
    """Fill the cache from a /users/me dict the caller already has (the health
    probe, a profile route) — saves this module its own OF round-trip. A dict
    without a usable `subscribePrice` is ignored rather than cached as
    unknown: it would only shorten the TTL of a good answer."""
    cents = _price_cents(profile)
    if cents is not None:
        _store(account_id, cents)


def invalidate(account_id: str) -> None:
    """Drop the cached tier — call after anything that can change it (a
    PATCH /users/me, a fresh session capture)."""
    _cache.pop(str(account_id), None)


def cached_price_cents(account_id: str) -> int | None:
    """The cached price without touching OF; None when unknown OR unfetched.
    For sync callers that must not block (renderers, logs)."""
    hit = _cache.get(str(account_id))
    if hit is None or hit[0] <= time.monotonic():
        return None
    return hit[1]


async def subscribe_price_cents(account_id: str) -> int | None:
    """This page's monthly subscription price in cents; 0 = free page, None =
    couldn't tell (dead session, OF error). Never raises — an unanswerable
    question must not take down the send that asked it."""
    key = str(account_id)
    hit = _cache.get(key)
    now = time.monotonic()
    if hit is not None and hit[0] > now:
        return hit[1]

    lock = _locks.setdefault(key, asyncio.Lock())
    async with lock:
        # Another waiter may have filled it while we queued.
        hit = _cache.get(key)
        if hit is not None and hit[0] > time.monotonic():
            return hit[1]
        try:
            import automation_executor as ax  # lazy: keep this module import-light

            client = await asyncio.to_thread(ax._make_client, key)
            profile = await asyncio.to_thread(client.me)
        except Exception:
            log.debug("account_page: /users/me probe failed account=%s", key,
                      exc_info=True)
            return _store(key, None)
        cents = _price_cents(profile)
        if cents is None:
            log.warning("account_page: /users/me for account=%s carried no "
                        "subscribePrice — page tier unknown", key)
        return _store(key, cents)


async def is_paid_page(account_id: str) -> bool:
    """True only when OF says this page charges for a subscription — i.e. when
    a PRICED feed post is not something she can do. Unknown reads False so the
    caller behaves exactly as it does today (see the module docstring)."""
    cents = await subscribe_price_cents(account_id)
    return bool(cents and cents > 0)
