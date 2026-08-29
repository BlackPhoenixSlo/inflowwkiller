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
                      "{MESSAGE_LINK}": "…/chats/chat/FAN_ID?firstId=10491720677794"}}

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

Idempotent by construction: the UPDATE only matches rows that are still
unpaid, and every ownership stamp is SELECT-first, so re-seeing the same
notification (every 30s, until it ages out of the feed) can never
double-apply anything. `_DONE_NOTIFS` below is NOT a correctness mechanism —
it only spares a re-sighting's repeat queries (and, for posts, repeat OF
fetches); it is process-local, and a restart simply reprocesses each
notification once.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Any, NamedTuple

from sqlalchemy import or_, select, update

import ownership
import relay_cache
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
    """One notification → {notif_id, fan_id, message_id, amount_cents, created_at,
    name}, or None when it isn't a chat-message purchase we can act on."""
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
        # OF names the payer right here; the live announce puts it on the
        # toast so a chatter sees WHO bought without a lookup.
        "name": (pairs or {}).get("{NAME}") if isinstance(pairs, dict) else None,
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


class _NotifKey(NamedTuple):
    """What the two memos below are keyed on. A plain tuple would do — this
    exists so the `notif_id is None` guard reads as itself everywhere it is
    asked, rather than as `key[2]`."""
    kind: str            # "msg" | "post" — namespaces the two lanes explicitly
    account_id: str
    notif_id: Any        # OF's own id; None when the payload carried none


# Notifications fully handled this process-lifetime — the feed re-serves the
# same events every poll until they age out, so without this every sighting
# re-runs the stamp queries (and, for posts, could re-hit OF). A relay restart
# just reprocesses each once (everything downstream is idempotent).
_DONE_NOTIFS: dict[_NotifKey, None] = {}


# Notifications already DELIVERED to at least one browser. Deliberately
# separate from _DONE_NOTIFS: "resolved" and "announced" are different
# questions. A purchase whose message row hasn't been scraped yet stays un-done
# and retries every poll — but it must not re-toast on each of those retries,
# and conversely a purchase somebody else flipped first is still news worth
# announcing. An entry lands here only once the ping REACHED a live subscriber:
# a broadcast into an empty room leaves the memo untouched, so the sale is
# offered again on the next poll instead of being remembered as told.
_ANNOUNCED_NOTIFS: dict[_NotifKey, None] = {}

# Shared bound for both memos above.
_NOTIF_MEMO_MAX = 4096

# Only announce purchases this fresh. Bounds the noise when the relay restarts
# and re-walks a feed page it already announced (same rationale as the ledger's
# 48h insert cap), without needing durable state. Generous next to the 30s
# poll, so an ordinary gap or a slow tick still notifies.
_ANNOUNCE_MAX_AGE_S = 2 * 60 * 60


def _remember(memo: dict[_NotifKey, None], key: _NotifKey) -> None:
    """Record `key` in one of the two memos, evicting oldest-first past
    _NOTIF_MEMO_MAX — an insertion-ordered dict as a cheap ring buffer."""
    while len(memo) >= _NOTIF_MEMO_MAX:
        memo.pop(next(iter(memo)))
    memo[key] = None


def _is_done(key: _NotifKey) -> bool:
    """Has this notification already been fully resolved this process-lifetime?
    An id-less one is never remembered (nothing to key on), so it can never be
    done either."""
    return key.notif_id is not None and key in _DONE_NOTIFS


def _is_fresh(created_at: Any) -> bool:
    """Is this notification recent enough to be worth a live ping? An
    unparseable timestamp counts as fresh: going silent on an OF format change
    is a worse failure than an occasional stale toast, and the browser dedupes
    on the notification id anyway."""
    raw = str(created_at or "").strip()
    if not raw:
        return True
    try:
        ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return True
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - ts).total_seconds() <= _ANNOUNCE_MAX_AGE_S


def _should_announce(key: _NotifKey, created_at: Any) -> bool:
    """Is this purchase worth a live ping we have not already landed? Says no
    to an id-less notification (nothing the browser could dedupe on) and to
    anything stale.

    Pure: the memo is written by the caller, and only on a real delivery. Two
    concurrent polls of one account can therefore both answer True and both
    broadcast — deliberate, because the ping carries OF's real notification id
    and the browser claims ids before rendering. That is the same dedupe this
    design already leans on to collapse the ping against the feed row, so a
    claim-and-rollback protocol here would buy nothing."""
    return (key.notif_id is not None
            and _is_fresh(created_at)
            and key not in _ANNOUNCED_NOTIFS)


async def _broadcast_purchase(account_id: str, p: dict[str, Any]) -> bool:
    """Tell every open browser a PPV was just unlocked. Returns whether the
    ping had anywhere to land.

    OF pushes no purchase event and its own `toasts` feed carries none either,
    so without this a sale is invisible until something refetches. The ledger
    can't do this job: it is the money record, but it lands late and by its own
    measurement sometimes hours late.

    `notif_id` is OF's REAL notification id — the same id the browser sees when
    it lists `users/notifications?type=purchases`. That's the point: the client
    can key on it, so the live ping and the eventual feed row dedupe against
    each other instead of showing one sale twice.

    **A broadcast with no subscriber is not a delivery.** SSE has no mailbox,
    so `events.broadcast` returns how many queues it actually handed the event
    to, and zero is reported as False rather than pretending. The caller then
    leaves the memo unwritten and the next 30s poll offers the sale again —
    the whole difference between "toasted the moment someone was looking" and
    "silently never toasted, because the tab was closed at 18:33".

    Never raises: a broadcast failure must not cost us the is_paid flip. It
    returns False, which only costs a retry.
    """
    # Unconditional, and NOT under the delivery question below: the bust is
    # server-side truth (this account's money changed), so it must happen even
    # with nobody watching — the tab that connects two seconds from now
    # refetches on its own and must not be served our pre-sale snapshot. This
    # is the ONLY money lane that fires in production: the WS carries no
    # purchase frame, so event_transcoder's unlock handlers never run and this
    # 30s poll is how a sale becomes visible at all.
    relay_cache.invalidate_money_feeds(str(account_id))
    try:
        from events import broadcast as _sse_broadcast
        reached = await _sse_broadcast({
            "purchase_notified": {
                "notif_id": str(p["notif_id"]),
                "fan_id": int(p["fan_id"]),
                "message_id": int(p["message_id"]),
                "amount_cents": int(p["amount_cents"] or 0),
                "created_at": p.get("created_at"),
                "name": (str(p["name"]) if p.get("name") else None),
            },
            "__account_id": str(account_id),
        })
    except Exception:  # noqa: BLE001
        log.debug("purchase_notified broadcast failed account=%s", account_id,
                  exc_info=True)
        return False
    return reached > 0


async def _message_paid(account_id: str, fan_id: int, message_id: int) -> bool:
    async with get_session() as s:
        return bool((await s.execute(
            select(Message.is_paid).where(
                Message.account_id == str(account_id),
                Message.fan_id == int(fan_id),
                Message.message_id == int(message_id))
        )).scalar_one_or_none())


async def _handle_items(account_id: str, client: Any, items: list,
                        *, announce: bool = False) -> dict[str, int]:
    """Shared per-notification dispatch for the 30s poll and the deep backfill.
    Chat purchase → flip is_paid + stamp the message's media as owned. Post
    purchase → stamp the post's media as owned. The STAMP legs are isolated
    (the try_stamp_* wrappers swallow and leave the notification retryable);
    an error in the is_paid flip or paid-check propagates to the caller — the
    30s poll swallows it there and unfinished notifications retry on the next
    sighting. Fully-handled events are remembered so re-sightings stop
    costing queries; an event whose message row hasn't landed yet (scrape
    lag) or whose post can't be resolved yet (wall scan lag) is NOT marked
    done and retries on the next poll."""
    out = {"seen": 0, "flipped": 0, "posts": 0, "stamped": 0}
    for n in (items or []):
        parsed = parse_purchase(n)
        if parsed:
            out["seen"] += 1
            key = _NotifKey("msg", str(account_id), parsed["notif_id"])
            # The ANNOUNCE lane, deliberately above the _is_done gate: a
            # purchase is news whether or not this call is the one that flips
            # is_paid, and on a live seller the ledger linker usually wins that
            # race — so the very first sighting is already marked done, and
            # gating on _is_done first would retire the announcement before it
            # was ever heard. Remembered only on a real delivery.
            if (announce and _should_announce(key, parsed["created_at"])
                    and await _broadcast_purchase(account_id, parsed)):
                _remember(_ANNOUNCED_NOTIFS, key)
            if _is_done(key):
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
            if key.notif_id is not None and (
                flipped or stamped
                or await _message_paid(account_id, parsed["fan_id"],
                                       parsed["message_id"])
            ):
                _remember(_DONE_NOTIFS, key)
            continue
        post = parse_post_purchase(n)
        if post:
            out["posts"] += 1
            key = _NotifKey("post", str(account_id), post["notif_id"])
            if _is_done(key):
                continue
            res = await ownership.try_stamp_post(
                account_id, post["fan_id"], post["post_id"],
                price_cents=post["amount_cents"], client=client,
                context="notif_post")
            out["stamped"] += res["stamped"]
            if key.notif_id is not None and res["resolved"]:
                _remember(_DONE_NOTIFS, key)
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
        # BOTH callers announce — the 30s fast tick AND the post-ownership sweep
        # (transaction_ingest); noise stays bounded by the _ANNOUNCED_NOTIFS dedupe
        # + the 2h freshness gate. The silent path is backfill_purchases below.
        return await _handle_items(account_id, client, items, announce=True)
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
        # Silent by construction: a deep repair walks months of purchases, and
        # announcing them would fire hundreds of toasts for sales long past.
        got = await _handle_items(account_id, client, fresh)
        for k, v in got.items():
            totals[k] += v
        totals["pages"] += 1
        if len(items) < int(page_size):
            break
    log.info("purchase_notif_backfill account=%s %s", account_id, totals)
    return totals
