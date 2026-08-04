"""customs_api.py — the owed-customs queue.

  GET  /admin/customs                  → every fan currently owed a custom
  POST /admin/customs/clear            → mark one delivered (clears the marker)

WHY IT IS CROSS-ACCOUNT BY DEFAULT
----------------------------------
The queue exists so that nobody has to remember to go and look. A per-account
page reintroduces exactly the thing the nickname marker was chosen to avoid — a
surface you only see if you already suspect there is something on it. Omitting
`account_id` returns every owed fan across every account the principal can see,
newest tip first, so one glance covers the whole roster.

WHAT "OWED" MEANS
-----------------
`fans.custom_nickname` ends in the ` Custom` marker — see `automations/_customs`.
The marker is the ledger; this endpoint is a VIEW over it and a way to clear it.
It deliberately holds no state of its own, so the page, the OF app and the
automation can never disagree about who is owed what.

⚠️ The suffix test is `_customs.is_owed`, never a substring — prod contains
`Johny/Colombia/31/Customer Service` and matching "custom" loosely puts a fan in
this queue forever.

CLEARING PUSHES TO ONLYFANS
---------------------------
The operator works in the OF app as much as here, and a marker cleared in one
place but not the other is worse than no marker at all. So a clear writes the DB
AND pushes the shortened nickname to OF. The DB write is the source of truth; an
OF push failure is reported to the caller but does not roll it back, because
`customs_watch` re-pushes on its next sweep.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, select

from auth import assert_account_owned, clamp_account_filter
from automations import _customs
from db.engine import get_session
from db.models import Account, AutomationRule, Fan, Transaction

log = logging.getLogger("of-relay.customs_api")

router = APIRouter()


@router.get("/admin/customs")
async def list_customs(account_id: str | None = Query(None)) -> dict[str, Any]:
    """Every fan owed a custom. `account_id` optional — omit for the whole roster."""
    allowed = clamp_account_filter(account_id)

    async with get_session() as s:
        q = select(Fan.account_id, Fan.fan_id, Fan.custom_nickname,
                   Fan.of_display_name, Fan.of_username,
                   Fan.lifetime_spend_cents).where(
            Fan.custom_nickname.is_not(None))
        if allowed is not None:
            q = q.where(Fan.account_id.in_(allowed))
        rows = (await s.execute(q)).all()

        # Only the real markers — see the module docstring on Customer Service.
        owed = [r for r in rows if _customs.is_owed(r[2])]
        if not owed:
            # STILL report untracked accounts. An empty queue plus a silent
            # config gap looks exactly like a healthy empty queue, and that is
            # the state this whole feature exists to make impossible.
            return {"customs": [], "count": 0,
                    "untracked": await _untracked(allowed)}

        # The tip that most likely bought it: newest qualifying tip from that
        # fan. Shown so the operator can see WHAT was paid without leaving the
        # page — a $100 and a $200 order are different pieces of work.
        keys = {(r[0], int(r[1])) for r in owed}
        tips = (await s.execute(
            select(Transaction.account_id, Transaction.fan_id,
                   Transaction.amount_cents, Transaction.occurred_at)
            .where(Transaction.kind.in_(_customs.ORDER_KINDS),
                   Transaction.amount_cents >= _customs.MIN_CENTS,
                   Transaction.account_id.in_({k[0] for k in keys}))
            .order_by(Transaction.occurred_at.desc())
        )).all()
        newest: dict[tuple, tuple] = {}
        for acct, fid, cents, at in tips:
            k = (acct, int(fid or 0))
            if k in keys and k not in newest:
                newest[k] = (int(cents or 0), at)

        names = {a: (n or a) for a, n in (await s.execute(
            select(Account.id, Account.nickname)
            .where(Account.id.in_({k[0] for k in keys}))
        )).all()}

    out = []
    for acct, fid, nick, disp, uname, spend in owed:
        cents, at = newest.get((acct, int(fid)), (None, None))
        out.append({
            "account_id": acct,
            "account_name": names.get(acct, acct),
            "fan_id": int(fid),
            # What the operator sees in the OF inbox — the marker included, so
            # the row on screen and the name in the app are the same string.
            "nickname": nick,
            "display_name": disp or uname or f"fan #{fid}",
            "tip_cents": cents,
            "tipped_at": at.isoformat() if at else None,
            "lifetime_spend_cents": int(spend or 0),
            "chat_href": f"/chat/{acct}/{int(fid)}",
        })
    # Oldest debt first: the man who has been waiting longest is the one at risk.
    out.sort(key=lambda r: (r["tipped_at"] or "9999"))
    return {"customs": out, "count": len(out),
            "untracked": await _untracked(allowed)}


async def _untracked(allowed: list[str] | None) -> list[dict[str, Any]]:
    """Accounts taking qualifying tips with NO enabled `customs_watch` rule.

    THE ONE HOLE IN A PER-ACCOUNT SWITCH is that an eligible account never gets
    the rule and nobody notices — which is silently the same outcome as having no
    feature at all, because the money still arrives and nothing records it. So
    the queue reports it: a tip big enough to be an order, on an account that is
    not watching for them, is a configuration question shown next to the work it
    would otherwise have created.

    Deliberately NOT auto-enabling. What a $100 DM tip MEANS differs by account —
    it is an order on the male accounts and was 5x generosity on the female ones
    last month — so switching the watcher on for anyone who takes one would mark
    those fans owed and stop the bot selling to them."""
    since = datetime.utcnow() - timedelta(days=14)
    async with get_session() as s:
        q = (select(Transaction.account_id, func.count(),
                    func.max(Transaction.amount_cents))
             .where(Transaction.kind.in_(_customs.ORDER_KINDS),
                    Transaction.amount_cents >= _customs.MIN_CENTS,
                    Transaction.occurred_at >= since)
             .group_by(Transaction.account_id))
        if allowed is not None:
            q = q.where(Transaction.account_id.in_(allowed))
        tipped = (await s.execute(q)).all()
        if not tipped:
            return []

        watching = {a for (a,) in (await s.execute(
            select(AutomationRule.account_id)
            .where(AutomationRule.kind == "customs_watch",
                   AutomationRule.is_enabled.is_(True),
                   AutomationRule.account_id.in_({t[0] for t in tipped}))
        )).all()}

        names = {a: (n or a) for a, n in (await s.execute(
            select(Account.id, Account.nickname)
            .where(Account.id.in_({t[0] for t in tipped}))
        )).all()}

    return [{"account_id": a, "account_name": names.get(a, a),
             "qualifying_tips": int(n), "biggest_cents": int(mx or 0)}
            for a, n, mx in tipped if a not in watching]


class _ClearBody(BaseModel):
    account_id: str
    fan_id: int


@router.post("/admin/customs/clear")
async def clear_custom(body: _ClearBody = Body(...)) -> dict[str, Any]:
    """Mark one custom delivered — strips the marker and pushes it to OF."""
    assert_account_owned(body.account_id)

    async with get_session() as s:
        fan = await s.get(Fan, {"account_id": str(body.account_id),
                                "fan_id": int(body.fan_id)})
        if fan is None:
            raise HTTPException(404, "fan not found")
        if not _customs.is_owed(fan.custom_nickname):
            # Idempotent: two operators clicking the same row is not an error,
            # and neither is `customs_watch` having cleared it first.
            return {"ok": True, "already_clear": True,
                    "nickname": fan.custom_nickname}
        cleared = _customs.clear(fan.custom_nickname)
        fan.custom_nickname = cleared or None
        # THE thing that makes this button stick. `customs_watch` derives "owed"
        # from live conditions, so without a settled-at stamp the same tip is
        # still in its window on the next sweep and gets re-marked — this click
        # used to undo itself within 15 minutes.
        fan.customs_cleared_at = datetime.utcnow()
        await s.commit()

    pushed = False
    try:
        import automation_executor as ax
        client = await asyncio.to_thread(ax._make_client, str(body.account_id))
        await asyncio.to_thread(client.set_fan_custom_name,
                                int(body.fan_id), cleared)
        pushed = True
    except Exception:
        # Not fatal, and not rolled back — the DB is the source of truth and
        # customs_watch re-pushes on its next sweep.
        log.warning("customs clear: OF push failed account=%s fan=%s",
                    body.account_id, body.fan_id, exc_info=True)

    log.info("customs CLEARED account=%s fan=%s nick=%r pushed=%s",
             body.account_id, body.fan_id, cleared, pushed)
    return {"ok": True, "nickname": cleared or None, "pushed_to_of": pushed}
