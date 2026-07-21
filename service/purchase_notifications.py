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

**Scope: resolution only. This module never writes money.** It flips
`messages.is_paid` and nothing else. The `transactions` ledger stays the single
writer of revenue, so there is no path by which a notification and its later
ledger row could both be counted. Offer resolution then happens on its own:
`ai_chatter._resolve_open_offers` already checks `_message_is_paid()` FIRST
(`paid_by="ppv_ledger"`), so a flipped message resolves on the next tick through
the existing, tested path rather than a second copy of that logic here.

Idempotent by construction: the UPDATE only matches rows that are still unpaid,
so re-seeing the same notification (every 30s, until it ages out of the feed) is
a no-op and needs no seen-id bookkeeping.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any

from sqlalchemy import or_, update

from db.engine import get_session
from db.models import Message

log = logging.getLogger("of-relay.purchase_notif")

# The notification kinds that mean "a fan paid for a chat message". `type` is the
# coarse bucket; `subType` distinguishes chat purchases from post/stream ones,
# which have no message to flip.
_PAID_TYPES = {"paided_message"}
_PAID_SUBTYPES = {"subscriber_pay_for_chat_message"}

# `…/chats/chat/<fan_id>?firstId=<message_id>` — the purchased message.
_MSG_LINK_RE = re.compile(r"/chats/chat/(\d+)\?firstId=(\d+)")
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


async def poll_purchases(account_id: str, client: Any, *, limit: int = 10) -> dict[str, int]:
    """Fetch the newest purchase notifications and flip is_paid for each.

    Never raises: this rides on the freshness tick, and a notifications hiccup
    must not take down transaction ingest. Returns counts for the caller's log.
    """
    out = {"seen": 0, "flipped": 0}
    try:
        r = client.get(
            f"/api2/v2/users/notifications?limit={int(limit)}&offset=0&type=purchases")
        raw = r.json() if hasattr(r, "json") else r
        items = raw.get("list") if isinstance(raw, dict) else raw
        for n in (items or []):
            parsed = parse_purchase(n)
            if not parsed:
                continue
            out["seen"] += 1
            if await _flip_paid(account_id, parsed["fan_id"], parsed["message_id"]):
                out["flipped"] += 1
                log.info(
                    "purchase_notif_flip account=%s fan=%s msg=%s amount_cents=%s at=%s",
                    account_id, parsed["fan_id"], parsed["message_id"],
                    parsed["amount_cents"], parsed["created_at"])
    except Exception:  # noqa: BLE001
        log.debug("purchase_notif_poll_failed account=%s", account_id, exc_info=True)
    return out
