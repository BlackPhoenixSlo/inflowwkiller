"""service/automations/customs_watch.py — Automation: customs_watch.

Turns a tip into a piece of visible owed work, and clears it when the work goes
out. The whole feature, per the operator's own framing:

    notice the money → make it unmissable → stop selling → let a human clear it

Delivery is MANUAL — a person records the voice note and sends it. Nothing here
sends anything to a fan. It only writes a nickname.

WHERE THE STATE LIVES
---------------------
`fans.customs_owed_at` — one nullable timestamp, holding the moment the
qualifying tip landed. NULL means nothing is owed. The operator sees the queue on
/customs and clears it there.

⚠️ THIS MODULE USED TO WRITE A " Custom" SUFFIX ONTO `fans.custom_nickname` AND
PUSH IT TO ONLYFANS, AND IT NEVER ONCE SURVIVED. `custom_nickname` has four other
writers; `welcome_chatter_for_info._maybe_push_nickname` — reached from ai_chatter on EVERY tick
— rebuilds the name from structured facts and drops anything that is not a
structured fact, then writes the shortened name to OF and back into our row.
ai_chatter runs every 60s and this runs every 900s, so the marker was erased about
a minute after it was written. Prod, after days live: 204 runs, zero marks that
outlived a tick. See `_customs`. NOTHING HERE MAY WRITE A NICKNAME AGAIN.

Because the column holds the TIP's timestamp rather than a bare flag, this module
also stopped needing its window fallback — see the clear loop.

WHY IT CLEARS ITSELF TOO
------------------------
The operator clearing the debt by hand is the authority and stays. But a
hand-clear that is FORGOTTEN leaves the account unable to sell to its single
best fan, forever — the brake has no timeout on purpose, since a timeout would
resume selling to a man who never got what he paid for. So a delivery detected
in the thread clears it as well. Whichever comes first wins; both are idempotent.

THREE THINGS THIS DELIBERATELY DOES NOT DO
------------------------------------------
1. It does not re-mark. `fans.customs_cleared_at` records the moment a fan's
   debt was settled and tips at or before it are ignored — otherwise clearing
   the debt (delivered!) makes the next sweep see the same old tip and mark it
   again, and the operator's click undoes itself on a 72-hour loop. That is not
   hypothetical: it is what this module did until 2026-08-04, and the delivery
   detector hid it in testing because a seeded voice note made the re-mark path
   unreachable.
2. It does not count quantity. $200 is not two customs; the live transcript that
   priced this feature shows the amount encoding LENGTH. Stacked tips inside one
   burst window are ONE order (`_customs.orders_per_fan` decides), not several.
3. It does not touch OnlyFans at all. It reads our transactions and writes our
   column; the operator's surface is /customs. That is what makes it impossible
   for a nickname pipeline to undo it.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import select

from automation_registry import register
from db.engine import get_session
from db.models import Fan, Message, MessageMedia, Transaction
from . import _customs
from ._common import coerce_ids

# ⚠️ NO `of_client` / `automation_executor` IMPORT, AND THAT IS THE POINT.
# This module reads our transactions and writes one of our columns. It has no
# reason to reach OnlyFans, and the moment it does, the marker it writes is back
# in the path of the nickname pipeline that erased it for days. If a future change
# needs an OF call here, re-read this module's docstring first.

log = logging.getLogger("of-relay.automation.customs_watch")

_PURPOSE = "customs_watch"
_DEFAULT_LOOKBACK_H = 72      # far enough back to catch a weekend, short enough
                              # that switching the feature on does not mark every
                              # historical $200 tip an account has ever taken
_DEFAULT_LIMIT = 200

# Transaction states that mean the money is real. Mirrors
# `ai_chatter._tip_sum_since` deliberately — a tip is either an order everywhere
# or nowhere, and these two were the pair that disagreed.
_SETTLED_STATUSES = ("cleared", "pending")


async def _delivered_since(account_id: str, fan_id: int,
                           since: datetime) -> bool:
    """Did the creator send this fan a voice note since `since`?

    The WHERE clause is the whole rule — outbound, not unsent, after the tip, and
    a `DELIVERY_TYPES` medium. It used to select price/is_tip as well and re-test
    each row through `_customs.is_delivery`, which mattered while a FREE video
    also counted; with an audio-only product that predicate could only ever agree
    with the query that fetched the row. Asking the database once beats asking it
    and then asking Python the same question."""
    async with get_session() as s:
        hit = (await s.execute(
            select(MessageMedia.media_id)
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
            .limit(1)
        )).first()
    return hit is not None


async def watch_flags(account_id: str) -> tuple[bool, dict]:
    """(enabled, rule_payload) for this account's `customs_watch` rule.

    The payload is the rule's `steps_json` — the same dict the scheduler hands
    `run`, so it carries the per-account `min_cents` floor and `dry_run`. Mirrors
    `tip_reward.reward_flags`: one read, for the live dispatcher below.

    (`trigger_json` holds `every_seconds` and is the scheduler's business. The
    floor rides `steps_json`; the comment in `run` calling it trigger_json is
    wrong and is corrected there.)"""
    from sqlalchemy import select as _select

    from db.models import AutomationRule
    try:
        async with get_session() as s:
            row = (await s.execute(
                _select(AutomationRule.is_enabled, AutomationRule.steps_json)
                .where(AutomationRule.account_id == str(account_id),
                       AutomationRule.kind == _PURPOSE)
                .limit(1)
            )).first()
    except Exception:
        log.warning("customs_watch flags read failed account=%s", account_id,
                    exc_info=True)
        return False, {}
    if row is None:
        return False, {}
    import json as _json
    try:
        payload = _json.loads(row[1] or "{}") or {}
    except Exception:
        payload = {}
    return bool(row[0]), payload


def order_floor(payload: dict | None) -> int:
    """The cents floor this account treats as an ORDER, off the rule payload.
    Shared by `run` and by the live dispatcher in
    `webhook_dispatch.on_inbound_tip` — the two paths that must never disagree
    about what counts. The floor RULE itself lives in `_customs.resolve_floor`."""
    return _customs.resolve_floor((payload or {}).get("min_cents"))


# ⚠️ THE LIVE TIP DISPATCHER IS NOT HERE, AND THAT IS ON PURPOSE.
# It would need `automation_executor` (to enqueue and wake), and this module's
# docstring bans that import — the ban is what keeps the marker out of the reach
# of the pipeline that erased it for days. Dispatch lives with the other
# `on_inbound_*` hooks in `webhook_dispatch`, which already owns that seam; this
# module exports `watch_flags` + `order_floor` and stays a reader and a writer of
# our own two columns.


@register("customs_watch")
async def run(account_id: str, payload: dict, *, run_id: int) -> dict:
    payload = payload or {}
    lookback_h = int(payload.get("lookback_hours") or _DEFAULT_LOOKBACK_H)
    limit = int(payload.get("limit") or _DEFAULT_LIMIT)
    dry_run = bool(payload.get("dry_run"))
    # Per-account floor, off the rule's steps_json (which the scheduler hands us
    # AS this payload — an older comment here said trigger_json, which is where
    # `every_seconds` lives, not the floor). One spelling: `_customs.resolve_floor`.
    floor = order_floor(payload)
    only = coerce_ids(payload.get("only_fan_ids"))
    since = datetime.utcnow() - timedelta(hours=lookback_h)

    # A floor above the quote band's own minimum means the chatter can name a
    # price this watch refuses to book — the fan pays it and is never marked
    # (the 2026-08-18 bug, reachable by editing one knob). Loud, every run,
    # because the misconfiguration is silent everywhere else.
    if floor > _customs.ASK_MIN_CENTS:
        log.warning(
            "customs_watch_floor_above_ask account=%s min_cents=%s ask_min=%s "
            "— a quoted $%d custom would go unmarked; lower min_cents or "
            "raise the ask band",
            account_id, floor, _customs.ASK_MIN_CENTS,
            _customs.ASK_MIN_CENTS // 100)

    stats = {"tips_scanned": 0, "marked": 0, "cleared": 0, "already_marked": 0,
             "skipped_delivered": 0, "already_settled": 0,
             "errors": 0, "dry_run": dry_run, "min_cents": floor}

    # ── 1. Settled order-kind tips in the window — EVERY size. The floor is a
    # question about the ORDER (a burst of tips), not each transaction, so the
    # sub-floor halves fans stack ($30+$30, $200+$50) must reach
    # `_customs.orders_per_fan` — the same definition the /customs queue
    # displays — instead of dying in a SQL prefilter.
    async with get_session() as s:
        q = (select(Transaction.fan_id, Transaction.amount_cents,
                    Transaction.occurred_at)
             .where(Transaction.account_id == str(account_id),
                    Transaction.kind.in_(_customs.ORDER_KINDS),
                    # MONEY THAT ACTUALLY ARRIVED. Without this a refunded or
                    # charged-back $200 marks the fan owed a voice note, and the
                    # only way back is a human noticing. `ai_chatter._tip_sum_since`
                    # has always filtered this way; this query did not, which made
                    # two engines disagree about whether the same tip was real.
                    # 'pending' counts: it is money in flight, and treating it as
                    # an order is the safe direction — the fan believes he ordered.
                    Transaction.status.in_(_SETTLED_STATUSES),
                    Transaction.occurred_at >= since)
             .order_by(Transaction.occurred_at.desc())
             .limit(limit))
        tips = [r for r in (await s.execute(q)).all() if r[0] is not None]

    rows = [(str(account_id), int(fan_id), cents, at, None)
            for fan_id, cents, at in tips
            if not (only and int(fan_id) not in only)]
    stats["tips_scanned"] = len(rows)
    # The anchor is the NEWEST tip of the fan's newest qualifying burst:
    # delivery is judged from it, so a second order placed before the first
    # shipped extends the wait rather than being cleared by the first delivery.
    by_fan: dict[int, datetime] = {
        fan_id: anchor_at
        for (_acct, fan_id), (_total, anchor_at, _msg)
        in _customs.orders_per_fan(rows, floor).items()}

    for fan_id, tipped_at in by_fan.items():
        try:
            async with get_session() as s:
                fan = await s.get(Fan, {"account_id": str(account_id),
                                        "fan_id": int(fan_id)})
                owed = _customs.is_owed(fan)
                settled = fan.customs_cleared_at if fan else None
            # ALREADY SETTLED. Without this the scanner re-marked a tip the
            # operator had just cleared, 15 minutes later, for as long as the tip
            # stayed in the window — their "Sent" click undid itself.
            if settled is not None and tipped_at <= settled:
                stats["already_settled"] += 1
                continue
            if owed:
                stats["already_marked"] += 1
                continue
            # He already got it — a tip that was fulfilled before this automation
            # ever ran must not resurrect as owed work.
            if await _delivered_since(account_id, fan_id, tipped_at):
                stats["skipped_delivered"] += 1
                continue
            if dry_run:
                stats["marked"] += 1
                continue
            # Re-read inside the write session and re-test: `is_owed` above was
            # judged on a snapshot from a session that is now closed, and the
            # operator may have cleared him in between. `mark` returning False is
            # that race losing quietly, which is the correct outcome.
            async with get_session() as s:
                fan = await s.get(Fan, {"account_id": str(account_id),
                                        "fan_id": int(fan_id)})
                if fan is None:
                    continue
                if not _customs.mark(fan, tipped_at):
                    stats["already_marked"] += 1
                    continue
                await s.commit()
            stats["marked"] += 1
            log.info("customs_watch OWED account=%s fan=%s tipped_at=%s",
                     account_id, fan_id, tipped_at)
        except Exception:
            stats["errors"] += 1
            log.warning("customs_watch mark failed account=%s fan=%s",
                        account_id, fan_id, exc_info=True)

    # ── 2. Clear anyone whose custom has since gone out
    #
    # The column IS the query now. It also carries the ORDER's timestamp, which
    # removes this loop's worst bug: it used to judge delivery from
    # `by_fan.get(fan_id, since)`, falling back to the 72h SWEEP WINDOW whenever
    # the originating tip had aged out of it. Any free outbound video in the last
    # three days — a mass send, a tip_reward freebie, a chatter being nice — then
    # settled a debt incurred a week earlier. `customs_owed_at` cannot age out, so
    # there is nothing to fall back to and the question is always the right one:
    # has a voice note gone out SINCE HE PAID?
    async with get_session() as s:
        owed_rows = (await s.execute(
            select(Fan.fan_id, Fan.customs_owed_at)
            .where(Fan.account_id == str(account_id),
                   Fan.customs_owed_at.is_not(None))
        )).all()

    for fan_id, owed_at in owed_rows:
        if only and int(fan_id) not in only:
            continue
        try:
            if not await _delivered_since(account_id, fan_id, owed_at):
                continue
            if dry_run:
                stats["cleared"] += 1
                continue
            async with get_session() as s:
                fan = await s.get(Fan, {"account_id": str(account_id),
                                        "fan_id": int(fan_id)})
                if fan is None:
                    continue
                if not _customs.clear(fan):
                    continue
                await s.commit()
            stats["cleared"] += 1
            log.info("customs_watch DELIVERED account=%s fan=%s owed_since=%s",
                     account_id, fan_id, owed_at)
        except Exception:
            stats["errors"] += 1
            log.warning("customs_watch clear failed account=%s fan=%s",
                        account_id, fan_id, exc_info=True)

    return stats
