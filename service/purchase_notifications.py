"""service/purchase_notifications.py — learn about a PPV purchase in ~30s
instead of hours, from OF's own purchases notification feed.

OF pushes NO purchase event over the WebSocket. Verified against 25,672 raw
events across the full `event_inbox` retention window: the wire carries
`typing`, `chat_messages`, `toasts`, `stories`, … and nothing else — no
`paidMessage`, no `messageUnlock`, no `purchase`. The three handlers
`event_transcoder` has always had for those names have fired zero times, and
toasts decode to only `favorited`/`subscribed`/`commented`/`release_forms`.

The ledger DOES record every purchase, but late: measured
`ingested_at - occurred_at` lags of 209, 560 and 628 minutes. Polling it harder
does not help — a 30s poll of an hours-stale source is still hours stale.

`GET /api2/v2/users/notifications?type=purchases` is the fresh source, and it is
richer than the ledger for this job:

    {"id": 117301756206,
     "type": "paided_message",
     "subType": "subscriber_pay_for_chat_message",
     "createdAt": "2026-07-21T15:26:00+00:00",
     "replacePairs": {"{NAME}": "Daniel",
                      "{AMOUNT}": "$6.00",
                      "{MESSAGE_LINK}": "…/chats/chat/580481431?firstId=10491720677794"}}

`firstId` is **the exact message that was bought**. That is what makes this worth
having: the global payment→message linker bails `ambiguous` whenever a fan holds
more than one unpaid same-priced PPV, so `is_paid` never flips and the seller
keeps saying "waiting on your tip" over content he already owns. Here OF names
the message outright — no inference, no price heuristic, no ambiguity.

The same feed also names WALL-POST purchases — the ledger's `ppv_post` row
carries NO post id anywhere (verified live 07-23: `descriptionDetails.params`
is just the fan's profile URL + name), but the notification does:

    {"type": "paided_post",
     "subType": "your_post_purchased",
     "replacePairs": {"{POST_URL}": "https://onlyfans.com/<post_id>/<handle>",
                      "{PRICE}": "$10.00"},
     "user": {"id": <fan_id>, …}}

That post id is what lets ownership stamping work for wall buys (Phase 2 of
plan/PREVENTION_FIX_NO_DUPLICATE_PPV.md — the 07-23 incident: a wall post
re-sold inside a PPV eight minutes later, because both dedup readers were
blind to post purchases).

**Scope: resolution + ownership only. This module never writes money.** It
flips `messages.is_paid` and stamps `vault_sends.was_purchased` (via
service/ownership.py) and nothing else. The `transactions` ledger stays the
single writer of revenue, so there is no path by which a notification and its
later ledger row could both be counted. Offer resolution then happens on its
own: `ai_chatter._resolve_open_offers` already checks `_message_is_paid()`
FIRST (`paid_by="ppv_ledger"`), so a flipped message resolves on the next tick
through the existing, tested path rather than a second copy of that logic
here.

Idempotent by construction: the UPDATE only matches rows that are still unpaid,
so re-seeing the same notification (every 30s, until it ages out of the feed) is
a no-op and needs no seen-id bookkeeping.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime
from typing import Any

from sqlalchemy import or_, select, update

import ownership
from db.engine import get_session
from db.models import Message

log = logging.getLogger("of-relay.purchase_notif")

# The notification kinds that mean "a fan paid for a chat message". `type` is the
# coarse bucket; `subType` distinguishes chat purchases from post/stream ones,
# which have no message to flip.
_PAID_TYPES = {"paided_message"}
_PAID_SUBTYPES = {"subscriber_pay_for_chat_message"}

# A fan bought a WALL POST (captured live 2026-07-23). No message to flip —
# these feed ownership stamping only.
_POST_TYPES = {"paided_post"}
_POST_SUBTYPES = {"your_post_purchased"}

# `…/chats/chat/<fan_id>?firstId=<message_id>` — the purchased message.
_MSG_LINK_RE = re.compile(r"/chats/chat/(\d+)\?firstId=(\d+)")
# `https://onlyfans.com/<post_id>/<username>` — the purchased wall post.
_POST_LINK_RE = re.compile(r"onlyfans\.com/(\d+)(?:/|$|[\"'?])")
_AMOUNT_RE = re.compile(r"([0-9]+(?:\.[0-9]{1,2})?)")


def _parse_amount_cents(raw: Any) -> int:
    """"$6.00" → 600. 0 when absent/unparseable — the amount is informational
    here (we resolve by message id, not by price), so a odd currency format
    must not drop an otherwise-valid purchase."""
    m = _AMOUNT_RE.search(str(raw or ""))
    if not m:
        return 0
    try:
        return int(round(float(m.group(1)) * 100))
    except (TypeError, ValueError):
        return 0


def parse_purchase(n: Any) -> dict[str, Any] | None:
    """One notification → {notif_id, fan_id, message_id, amount_cents, created_at},
    or None when it isn't a chat-message purchase we can act on."""
    if not isinstance(n, dict):
        return None
    if str(n.get("type") or "") not in _PAID_TYPES:
        return None
    sub = str(n.get("subType") or "")
    # Tolerate a missing subType (older payloads) but reject a KNOWN non-chat one:
    # a post/stream purchase has no chat message and must not flip anything.
    if sub and sub not in _PAID_SUBTYPES:
        return None
    pairs = n.get("replacePairs")
    link = ""
    if isinstance(pairs, dict):
        link = str(pairs.get("{MESSAGE_LINK}") or "")
    if not link:
        link = str(n.get("text") or "")
    m = _MSG_LINK_RE.search(link)
    if not m:
        return None
    fan_id, message_id = int(m.group(1)), int(m.group(2))
    # Prefer the link's fan id; fall back to the user blob only if the link
    # somehow lacked one (it always carries it in practice).
    if not fan_id:
        fan_id = int(((n.get("user") or {}) if isinstance(n.get("user"), dict) else {}).get("id") or 0)
    if not fan_id or not message_id:
        return None
    amount = _parse_amount_cents(
        (pairs or {}).get("{AMOUNT}") if isinstance(pairs, dict) else None)
    return {
        "notif_id": n.get("id"),
        "fan_id": fan_id,
        "message_id": message_id,
        "amount_cents": amount,
        "created_at": n.get("createdAt"),
    }


def parse_post_purchase(n: Any) -> dict[str, Any] | None:
    """One notification → {notif_id, fan_id, post_id, amount_cents, created_at},
    or None when it isn't a wall-post purchase. Chat purchases stay with
    `parse_purchase` — the two vocabularies never overlap (`paided_message` vs
    `paided_post`)."""
    if not isinstance(n, dict):
        return None
    if str(n.get("type") or "") not in _POST_TYPES:
        return None
    sub = str(n.get("subType") or "")
    # Same tolerance rule as parse_purchase: missing subType passes, a KNOWN
    # foreign one (a stream, say) does not.
    if sub and sub not in _POST_SUBTYPES:
        return None
    pairs = n.get("replacePairs")
    link = ""
    if isinstance(pairs, dict):
        link = str(pairs.get("{POST_URL}") or "")
    if not link:
        link = str(n.get("text") or "")
    m = _POST_LINK_RE.search(link)
    if not m:
        return None
    post_id = int(m.group(1))
    user = n.get("user") if isinstance(n.get("user"), dict) else {}
    fan_id = int(user.get("id") or 0)
    if not fan_id or not post_id:
        return None
    amount = _parse_amount_cents(
        pairs.get("{PRICE}") if isinstance(pairs, dict) else None)
    return {
        "notif_id": n.get("id"),
        "fan_id": fan_id,
        "post_id": post_id,
        "amount_cents": amount,
        "created_at": n.get("createdAt"),
    }


async def _flip_paid(account_id: str, fan_id: int, message_id: int) -> bool:
    """Mark exactly this outbound priced message paid. Returns True when THIS
    call did the flip (so callers only log real transitions).

    Deliberately narrow: scoped to the (account, fan, message) triple, requires
    `direction='out'` and `price_cents > 0`, and only matches rows still unpaid.
    A malformed parse therefore cannot mark an inbound or free message as
    bought, and a repeat of the same notification updates zero rows."""
    async with get_session() as s:
        res = await s.execute(
            update(Message)
            .where(Message.account_id == str(account_id),
                   Message.fan_id == int(fan_id),
                   Message.message_id == int(message_id),
                   Message.direction == "out",
                   Message.price_cents > 0,
                   or_(Message.is_paid.is_(False), Message.is_paid.is_(None)))
            .values(is_paid=True, purchased_at=datetime.utcnow())
        )
        await s.commit()
    return bool(res.rowcount)


# Notifications fully handled this process-lifetime — the feed re-serves the
# same events every poll until they age out, so without this every sighting
# re-runs the stamp queries (and, for posts, could re-hit OF). Keys are
# ("msg"|"post", account_id, notif_id) — the kind tag namespaces the two
# lanes explicitly. Insertion-ordered dict as a cheap FIFO; a relay restart
# just reprocesses once (everything downstream is idempotent).
_DONE_NOTIFS: dict[tuple[str, str, Any], None] = {}
_DONE_NOTIFS_MAX = 4096


def _mark_done(key: tuple[str, str, Any]) -> None:
    while len(_DONE_NOTIFS) >= _DONE_NOTIFS_MAX:
        _DONE_NOTIFS.pop(next(iter(_DONE_NOTIFS)))
    _DONE_NOTIFS[key] = None


async def _message_paid(account_id: str, fan_id: int, message_id: int) -> bool:
    async with get_session() as s:
        return bool((await s.execute(
            select(Message.is_paid).where(
                Message.account_id == str(account_id),
                Message.fan_id == int(fan_id),
                Message.message_id == int(message_id))
        )).scalar_one_or_none())


async def _handle_items(account_id: str, client: Any, items: list) -> dict[str, int]:
    """Shared per-notification dispatch for the 30s poll and the deep backfill.
    Chat purchase → flip is_paid + stamp the message's media as owned. Post
    purchase → stamp the post's media as owned. Each notification is isolated
    (one bad stamp must not abort the rest of the batch) and remembered once
    fully handled, so re-sightings stop costing queries; an event whose
    message row hasn't landed yet (scrape lag) or whose post can't be resolved
    yet (wall scan lag) is NOT marked done and retries on the next poll."""
    out = {"seen": 0, "flipped": 0, "posts": 0, "stamped": 0}
    for n in (items or []):
        parsed = parse_purchase(n)
        if parsed:
            out["seen"] += 1
            key = ("msg", str(account_id), parsed["notif_id"])
            if parsed["notif_id"] is not None and key in _DONE_NOTIFS:
                continue
            flipped = await _flip_paid(
                account_id, parsed["fan_id"], parsed["message_id"])
            if flipped:
                out["flipped"] += 1
                log.info(
                    "purchase_notif_flip account=%s fan=%s msg=%s amount_cents=%s at=%s",
                    account_id, parsed["fan_id"], parsed["message_id"],
                    parsed["amount_cents"], parsed["created_at"])
            stamped = await ownership.try_stamp_message(
                account_id, parsed["fan_id"], parsed["message_id"],
                price_cents=parsed["amount_cents"], context="notif_chat")
            out["stamped"] += stamped
            # Done once flipped/stamped/already-paid; a message row that
            # hasn't landed yet (scrape lag) stays retryable.
            if parsed["notif_id"] is not None and (
                flipped or stamped
                or await _message_paid(account_id, parsed["fan_id"],
                                       parsed["message_id"])
            ):
                _mark_done(key)
            continue
        post = parse_post_purchase(n)
        if post:
            out["posts"] += 1
            key = ("post", str(account_id), post["notif_id"])
            if post["notif_id"] is not None and key in _DONE_NOTIFS:
                continue
            res = await ownership.try_stamp_post(
                account_id, post["fan_id"], post["post_id"],
                price_cents=post["amount_cents"], client=client,
                context="notif_post")
            out["stamped"] += res["stamped"]
            if post["notif_id"] is not None and res["resolved"]:
                _mark_done(key)
    return out


def _fetch_feed(client: Any, limit: int, offset: int) -> list:
    """Blocking OF GET + JSON decode — always call via asyncio.to_thread; the
    supervisor holds the per-account tick lock while polling, and a
    synchronous socket read on the event loop would stall every account."""
    r = client.get(
        f"/api2/v2/users/notifications?limit={int(limit)}"
        f"&offset={int(offset)}&type=purchases")
    raw = r.json() if hasattr(r, "json") else r
    items = raw.get("list") if isinstance(raw, dict) else raw
    return items if isinstance(items, list) else []


async def poll_purchases(account_id: str, client: Any, *, limit: int = 10) -> dict[str, int]:
    """Fetch the newest purchase notifications; flip is_paid for chat buys and
    stamp media ownership for chat AND wall-post buys.

    Never raises: this rides on the freshness tick, and a notifications hiccup
    must not take down transaction ingest. Returns counts for the caller's log.
    """
    try:
        items = await asyncio.to_thread(_fetch_feed, client, limit, 0)
        return await _handle_items(account_id, client, items)
    except Exception:  # noqa: BLE001
        log.debug("purchase_notif_poll_failed account=%s", account_id, exc_info=True)
        return {"seen": 0, "flipped": 0, "posts": 0, "stamped": 0}


async def backfill_purchases(
    account_id: str, client: Any, *, page_size: int = 50, max_pages: int = 20
) -> dict[str, int]:
    """Walk the purchases feed as deep as OF retains it and stamp everything —
    the historical repair for wall-post buys that pre-date the paided_post
    parser (and a belt for any chat buy the 30s poll missed while down).
    Idempotent: every stamp is SELECT-first. Stops on an empty page, a repeat
    page (OF ignores an offset past the end and re-serves the tail — verified
    live 07-23), or max_pages. Run in-container:

        docker exec -w /app/service fastt-relay python -c "
        import asyncio, purchase_notifications as pn
        from of_client import OFClient
        aid='<ACCOUNT_ID>'
        print(asyncio.run(pn.backfill_purchases(aid, OFClient.from_account(aid))))"
    """
    totals = {"seen": 0, "flipped": 0, "posts": 0, "stamped": 0, "pages": 0}
    seen_ids: set = set()
    for page in range(int(max_pages)):
        try:
            items = await asyncio.to_thread(
                _fetch_feed, client, page_size, page * int(page_size))
        except Exception:  # noqa: BLE001
            log.warning("purchase_notif_backfill_fetch_failed account=%s page=%d",
                        account_id, page, exc_info=True)
            break
        fresh = [n for n in items
                 if isinstance(n, dict) and n.get("id") not in seen_ids]
        if not fresh:
            break
        seen_ids.update(n.get("id") for n in fresh)
        got = await _handle_items(account_id, client, fresh)
        for k, v in got.items():
            totals[k] += v
        totals["pages"] += 1
        if len(items) < int(page_size):
            break
    log.info("purchase_notif_backfill account=%s %s", account_id, totals)
    return totals
