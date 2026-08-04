"""service/automations/customs_watch.py — Automation: customs_watch.

Turns a tip into a piece of visible owed work, and clears it when the work goes
out. The whole feature, per the operator's own framing:

    notice the money → make it unmissable → stop selling → let a human clear it

Delivery is MANUAL — a person records the voice note and sends it. Nothing here
sends anything to a fan. It only writes a nickname.

WHERE THE STATE LIVES
---------------------
`fans.custom_nickname`, with " Custom" appended, pushed to OnlyFans as
`displayName`. See `_customs` for why a nickname beats a table: the operator
works in the OF app, not in our admin, and the marker renders next to the fan's
name in the inbox without anyone deciding to go and look.

`custom_nickname` is the right column of the three:
  • `generated_nickname` is gen_info's, and gen_info rewrites it wholesale on
    every profile refresh — a marker there is erased by the next sweep.
  • `fan_chosen_nickname` is the FAN's, and is not ours to touch.
  • `custom_nickname` is the OPERATOR's, and `apply_profiles` reads
    `fan_profiles.nickname` rather than this column, so the two never fight.

WHY IT CLEARS ITSELF TOO
------------------------
The operator clearing the marker by hand is the authority and stays. But a
hand-clear that is FORGOTTEN leaves the account unable to sell to its single
best fan, forever — the brake has no timeout on purpose, since a timeout would
resume selling to a man who never got what he paid for. So a delivery detected
in the thread clears it as well. Whichever comes first wins; both are idempotent.

TWO THINGS THIS DELIBERATELY DOES NOT DO
----------------------------------------
1. It does not re-mark. `fans.customs_cleared_at` records the moment a fan's
   debt was settled and tips at or before it are ignored — otherwise clearing
   the marker (delivered!) makes the next sweep see the same old tip and mark it
   again, and the operator's click undoes itself on a 72-hour loop. That is not
   hypothetical: it is what this module did until 2026-08-04, and the delivery
   detector hid it in testing because a seeded voice note made the re-mark path
   unreachable.
2. It does not count quantity. $400 is not two customs; the live transcript that
   priced this feature shows the amount encoding LENGTH, and two customs means
   two separate tips.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from sqlalchemy import select

import automation_executor as ax
from automation_registry import register
from db.engine import get_session
from db.models import Fan, Message, MessageMedia, Transaction
from of_client import OFClient
from . import _customs
from ._common import coerce_ids

log = logging.getLogger("of-relay.automation.customs_watch")

_PURPOSE = "customs_watch"
_DEFAULT_LOOKBACK_H = 72      # far enough back to catch a weekend, short enough
                              # that switching the feature on does not mark every
                              # historical $200 tip an account has ever taken
_DEFAULT_LIMIT = 200


async def _mark_of(account_id: str, fan_id: int, nick: str) -> bool:
    """Push the nickname to OnlyFans. Failure is logged, never fatal — the DB
    write is the source of truth and the next sweep retries the push."""
    try:
        client = await asyncio.to_thread(ax._make_client, account_id)
        await asyncio.to_thread(client.set_fan_custom_name, int(fan_id), nick)
        return True
    except Exception:
        log.warning("customs_watch: OF nickname push failed account=%s fan=%s",
                    account_id, fan_id, exc_info=True)
        return False


async def _delivered_since(account_id: str, fan_id: int,
                           since: datetime) -> bool:
    """Did the creator send this fan a voice note (or a free video) since `since`?"""
    async with get_session() as s:
        rows = (await s.execute(
            select(MessageMedia.type, Message.price_cents, Message.is_tip)
            .join(Message,
                  (Message.account_id == MessageMedia.account_id)
                  & (Message.fan_id == MessageMedia.fan_id)
                  & (Message.message_id == MessageMedia.message_id))
            .where(MessageMedia.account_id == str(account_id),
                   MessageMedia.fan_id == int(fan_id),
                   Message.direction == "out",
                   Message.is_unsent.is_(False),
                   Message.created_at >= since,
                   MessageMedia.type.in_(_customs.DELIVERY_TYPES))
        )).all()
    return any(_customs.is_delivery(t, p, bool(tip)) for t, p, tip in rows)


@register("customs_watch")
async def run(account_id: str, payload: dict, *, run_id: int) -> dict:
    payload = payload or {}
    lookback_h = int(payload.get("lookback_hours") or _DEFAULT_LOOKBACK_H)
    limit = int(payload.get("limit") or _DEFAULT_LIMIT)
    dry_run = bool(payload.get("dry_run"))
    # Per-account floor, off the rule's trigger_json. See _customs.qualifies.
    min_cents = payload.get("min_cents")
    min_cents = None if min_cents is None else max(1, int(min_cents))
    floor = _customs.MIN_CENTS if min_cents is None else min_cents
    only = coerce_ids(payload.get("only_fan_ids"))
    since = datetime.utcnow() - timedelta(hours=lookback_h)

    stats = {"tips_seen": 0, "marked": 0, "cleared": 0, "already_marked": 0,
             "skipped_delivered": 0, "pushed": 0, "push_failed": 0,
             "already_settled": 0,
             "errors": 0, "dry_run": dry_run, "min_cents": floor}

    # ── 1. Qualifying tips in the window
    async with get_session() as s:
        q = (select(Transaction.fan_id, Transaction.amount_cents,
                    Transaction.occurred_at, Transaction.kind)
             .where(Transaction.account_id == str(account_id),
                    Transaction.kind.in_(_customs.ORDER_KINDS),
                    Transaction.amount_cents >= floor,
                    Transaction.occurred_at >= since)
             .order_by(Transaction.occurred_at.desc())
             .limit(limit))
        tips = [r for r in (await s.execute(q)).all() if r[0] is not None]

    by_fan: dict[int, datetime] = {}
    for fan_id, cents, at, kind in tips:
        if only and int(fan_id) not in only:
            continue
        if not _customs.qualifies(kind, cents, min_cents):
            continue
        stats["tips_seen"] += 1
        # Keep the NEWEST qualifying tip per fan: delivery is judged from it, so
        # a second order placed before the first shipped extends the wait rather
        # than being cleared by the first delivery.
        prev = by_fan.get(int(fan_id))
        if prev is None or at > prev:
            by_fan[int(fan_id)] = at

    for fan_id, tipped_at in by_fan.items():
        try:
            async with get_session() as s:
                fan = await s.get(Fan, {"account_id": str(account_id),
                                        "fan_id": int(fan_id)})
                nick = (fan.custom_nickname or "") if fan else ""
                settled = fan.customs_cleared_at if fan else None
            # ALREADY SETTLED. Without this the scanner re-marked a tip the
            # operator had just cleared, 15 minutes later, for as long as the tip
            # stayed in the window — their "Sent" click undid itself.
            if settled is not None and tipped_at <= settled:
                stats["already_settled"] += 1
                continue
            if _customs.is_owed(nick):
                stats["already_marked"] += 1
                continue
            # He already got it — a tip that was fulfilled before this automation
            # ever ran must not resurrect as owed work.
            if await _delivered_since(account_id, fan_id, tipped_at):
                stats["skipped_delivered"] += 1
                continue
            marked = _customs.mark(nick)
            if dry_run:
                stats["marked"] += 1
                continue
            async with get_session() as s:
                fan = await s.get(Fan, {"account_id": str(account_id),
                                        "fan_id": int(fan_id)})
                if fan is None:
                    continue
                fan.custom_nickname = marked
                await s.commit()
            stats["marked"] += 1
            if await _mark_of(account_id, fan_id, marked):
                stats["pushed"] += 1
            else:
                stats["push_failed"] += 1
            log.info("customs_watch OWED account=%s fan=%s nick=%r",
                     account_id, fan_id, marked)
        except Exception:
            stats["errors"] += 1
            log.warning("customs_watch mark failed account=%s fan=%s",
                        account_id, fan_id, exc_info=True)

    # ── 2. Clear anyone whose custom has since gone out
    async with get_session() as s:
        owed_rows = (await s.execute(
            select(Fan.fan_id, Fan.custom_nickname)
            .where(Fan.account_id == str(account_id),
                   Fan.custom_nickname.is_not(None))
        )).all()

    for fan_id, nick in owed_rows:
        if not _customs.is_owed(nick):
            continue
        if only and int(fan_id) not in only:
            continue
        try:
            # Judge delivery from the tip that put the marker there when we can
            # still see it; otherwise from the window, which is the conservative
            # direction (a delivery older than the window leaves it owed, and a
            # human can always clear it by hand).
            mark_from = by_fan.get(int(fan_id), since)
            if not await _delivered_since(account_id, fan_id, mark_from):
                continue
            cleared = _customs.clear(nick)
            if dry_run:
                stats["cleared"] += 1
                continue
            async with get_session() as s:
                fan = await s.get(Fan, {"account_id": str(account_id),
                                        "fan_id": int(fan_id)})
                if fan is None:
                    continue
                fan.custom_nickname = cleared or None
                fan.customs_cleared_at = datetime.utcnow()
                await s.commit()
            stats["cleared"] += 1
            await _mark_of(account_id, fan_id, cleared)
            log.info("customs_watch DELIVERED account=%s fan=%s nick=%r",
                     account_id, fan_id, cleared)
        except Exception:
            stats["errors"] += 1
            log.warning("customs_watch clear failed account=%s fan=%s",
                        account_id, fan_id, exc_info=True)

    return stats
