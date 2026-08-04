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
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, select

from auth import assert_account_owned, clamp_account_filter
from automations import _customs
from db.engine import get_session
from db.models import Account, AutomationRule, Fan, Message, Transaction

# How far back the REVIEW list reaches. A backlog to clear once, not a
# permanent second queue.
_REVIEW_DAYS = 60

log = logging.getLogger("of-relay.customs_api")

router = APIRouter()


# Fans STACK tips to reach a price. Live example (Amia / 142006425): $200 and
# $100 twenty-five seconds apart, then $150 twenty minutes later — one $450
# order, three rows. Showing only the newest told the operator "$150" for work
# worth three times that, which is exactly the number they decide on. Same
# pattern on 80511815 ($200+$100, 26s apart) and 21843747 ($200+$100, 13s).
_BURST_MINUTES = 30


def _orders_per_fan(rows, min_cents: int | None = None) -> dict[tuple, tuple]:
    """(account, fan) → (total_cents, anchor_at, anchor_msg_id) for the fan's most
    recent ORDER, from rows ordered occurred_at DESC as
    (account_id, fan_id, amount_cents, occurred_at, message_id).

    An ORDER is a BURST of tips, not one tip. Fans stack to reach a price, and
    they do it constantly — Amia/142006425 sent $200 and $100 twenty-five seconds
    apart then $150 twenty minutes later, one $450 order across three rows.

    ⚠️ THE FLOOR IS TESTED ON THE TOTAL, AND THAT IS THE WHOLE POINT. The first
    version filtered each transaction against the floor in SQL and summed what
    survived, which reintroduced the same understatement one layer down: prod is
    full of $200+$80, $200+$50, $150+$80, $100+$54 — the sub-floor half was
    dropped before the sum, so a $280 order displayed as $200. It also made
    $60+$60 invisible entirely, and $120 is a real order.

    So the caller passes EVERY tip in the window and this decides. The floor is a
    question about the ORDER; asking it of each transaction is asking the wrong
    thing about the wrong noun."""
    floor = _customs.MIN_CENTS if min_cents is None else max(1, int(min_cents))
    bursts: dict[tuple, tuple] = {}
    for acct, fid, cents, at, msg_id in rows:
        if fid is None or at is None:
            continue
        key = (acct, int(fid))
        cents = int(cents or 0)
        if key not in bursts:         # first wins — the rows arrive newest-first
            bursts[key] = (cents, at, msg_id)
            continue
        total, anchor_at, anchor_msg = bursts[key]
        # Still inside the burst the anchor belongs to? Add it on. Anything older
        # is a PREVIOUS order and is not this row's business.
        if (anchor_at - at) <= timedelta(minutes=_BURST_MINUTES):
            bursts[key] = (total + cents, anchor_at, anchor_msg)
    return {k: v for k, v in bursts.items() if v[0] >= floor}


def _chat_href(account_id: str, fan_id: int, msg_id) -> str:
    """The thread, anchored on the tip when we have its message id — landing on
    the message that has to be READ to decide, not merely on the fan."""
    base = f"/chat/{account_id}/{int(fan_id)}"
    return f"{base}?msg={int(msg_id)}" if msg_id else base


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
                   Transaction.amount_cents, Transaction.occurred_at,
                   Transaction.message_id)
            .where(Transaction.kind.in_(_customs.ORDER_KINDS),
                   # NO floor here — see _orders_per_fan: a sub-floor tip
                   # stacked onto a qualifying one is part of the same order.
                   Transaction.account_id.in_({k[0] for k in keys}))
            .order_by(Transaction.occurred_at.desc())
        )).all()
        newest = {k: v for k, v in _orders_per_fan(tips).items() if k in keys}

        names = {a: (n or a) for a, n in (await s.execute(
            select(Account.id, Account.nickname)
            .where(Account.id.in_({k[0] for k in keys}))
        )).all()}

    out = []
    for acct, fid, nick, disp, uname, spend in owed:
        cents, at, msg_id = newest.get((acct, int(fid)), (None, None, None))
        out.append({
            "account_id": acct,
            "account_name": names.get(acct, acct),
            "fan_id": int(fid),
            "marked": True,
            # What the operator sees in the OF inbox — the marker included, so
            # the row on screen and the name in the app are the same string.
            "nickname": nick,
            "display_name": disp or uname or f"fan #{fid}",
            "tip_cents": cents,
            "tipped_at": at.isoformat() if at else None,
            "lifetime_spend_cents": int(spend or 0),
            "chat_href": _chat_href(acct, fid, msg_id),
        })
    out.extend(await _unmarked(allowed, {(r["account_id"], r["fan_id"]) for r in out}))
    # Oldest debt first: the man who has been waiting longest is the one at risk.
    out.sort(key=lambda r: (r["tipped_at"] or "9999"))
    return {"customs": out, "count": len(out),
            "untracked": await _untracked(allowed)}


async def _unmarked(allowed: list[str] | None,
                    already: set[tuple]) -> list[dict[str, Any]]:
    """Qualifying tips carrying NO marker — the ones nothing was watching for.

    Operator ruling 2026-08-04: *"show them in the customs field and we will
    manually take a look and delete if needed"*. Before this they were a COUNT in
    a banner, which tells you a problem exists without letting you resolve it;
    every one had to be hunted down in OnlyFans by hand. As rows they get the
    same Sent button, and clearing one stamps `customs_cleared_at` so it settles
    and stops reappearing.

    These are a REVIEW list, not a debt list — a $100 DM tip is an order on the
    male accounts and was generosity five times on the female ones last month,
    and nothing here can tell which. `marked: False` says so on the row, and the
    settled ones are excluded so the list drains as it is worked.

    Bounded to `_REVIEW_DAYS`: this is a backlog to clear once, not a permanent
    second queue."""
    since = datetime.utcnow() - timedelta(days=_REVIEW_DAYS)
    async with get_session() as s:
        q = (select(Transaction.account_id, Transaction.fan_id,
                    Transaction.amount_cents, Transaction.occurred_at,
                    Transaction.message_id)
             .where(Transaction.kind.in_(_customs.ORDER_KINDS),
                    # NO floor — _orders_per_fan tests the burst TOTAL.
                    Transaction.occurred_at >= since,
                    Transaction.fan_id.is_not(None))
             .order_by(Transaction.occurred_at.desc()))
        if allowed is not None:
            q = q.where(Transaction.account_id.in_(allowed))
        tips = (await s.execute(q)).all()
        if not tips:
            return []

        keys = {(a, int(f)) for a, f, _, _, _ in tips} - already
        if not keys:
            return []
        fans = {(f.account_id, int(f.fan_id)): f for f in (await s.execute(
            select(Fan).where(Fan.account_id.in_({k[0] for k in keys})))
        ).scalars().all()}
        names = {a: (n or a) for a, n in (await s.execute(
            select(Account.id, Account.nickname)
            .where(Account.id.in_({k[0] for k in keys}))
        )).all()}

    out = []
    for key, (cents, at, msg_id) in _orders_per_fan(tips).items():
        if key not in keys:
            continue
        acct, fid = key
        fan = fans.get(key)
        # Already settled — delivered, or an operator cleared it. Not a question.
        if fan is not None and fan.customs_cleared_at and at <= fan.customs_cleared_at:
            continue
        out.append({
            "account_id": acct,
            "account_name": names.get(acct, acct),
            "fan_id": int(fid),
            "marked": False,
            "nickname": (fan.custom_nickname if fan else None),
            "display_name": ((fan.of_display_name or fan.of_username) if fan else None)
                            or f"fan #{fid}",
            "tip_cents": int(cents or 0),
            "tipped_at": at.isoformat() if at else None,
            "lifetime_spend_cents": int((fan.lifetime_spend_cents if fan else 0) or 0),
            # Anchor the chat on the tip itself — see the page for why the id
            # matters more than the fan link alone.
            "chat_href": _chat_href(acct, fid, msg_id),
        })
    return out


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


@router.get("/admin/customs/context")
async def customs_context(
    account_id: str = Query(...),
    fan_id: int = Query(...),
    at: str | None = Query(None, description="ISO anchor; default = his newest qualifying tip"),
    before: int = Query(18, ge=1, le=60),
    after: int = Query(6, ge=0, le=60),
) -> dict[str, Any]:
    """The conversation AROUND the tip — what was actually ordered.

    This is the answer to "how do we know what to do with this row". A tip on its
    own says a man paid; the twenty messages around it say what he paid FOR. From
    a real thread (Amia / 142006425): "I can start doing it babe immediately
    after you tip for it" / "Make sure you talk dirty to daddy Dennis while
    you're riding me" / $200 + $100 / "Wanna see me in a sexy lingerie" — a
    complete work order, reconstructed from messages we already store.

    COUNT-BASED, not a time window. A time window is empty on a slow thread and
    unbounded on a fast one; N-before/M-after always returns something to read.

    It also makes the chat deep-link's page bound stop mattering for the common
    case: the context arrives WITH the row, so nobody has to reach a 700-hour-old
    message in the OnlyFans-backed thread pane to make the call."""
    assert_account_owned(account_id)

    async with get_session() as s:
        anchor_at = None
        if at:
            try:
                parsed = datetime.fromisoformat(at.replace("Z", "+00:00"))
            except ValueError:
                raise HTTPException(422, "bad `at` timestamp")
            # CONVERT to UTC before dropping the offset. `.replace(tzinfo=None)`
            # alone DISCARDS it, so "03:11:48+02:00" anchored at 03:11 instead of
            # 01:11 and read two hours of the wrong conversation. The column is
            # tz-naive UTC; anything arriving with an offset has to be moved, not
            # stripped. Naive input is already UTC by that same convention.
            anchor_at = (parsed.astimezone(timezone.utc).replace(tzinfo=None)
                         if parsed.tzinfo is not None else parsed)
        if anchor_at is None:
            # His newest tip, floor-free. The floor is a question about an
            # ORDER TOTAL now (see _orders_per_fan), so a $60+$60 order has a row
            # in the queue but no single tip that clears it — asking for one here
            # would return no anchor for a fan the queue is actively showing.
            # Picking the anchor and judging significance are different jobs.
            anchor_at = (await s.execute(
                select(func.max(Transaction.occurred_at))
                .where(Transaction.account_id == str(account_id),
                       Transaction.fan_id == int(fan_id),
                       Transaction.kind.in_(_customs.ORDER_KINDS))
            )).scalar_one_or_none()
        if anchor_at is None:
            return {"anchor_at": None, "messages": []}

        cols = (Message.message_id, Message.direction, Message.body,
                Message.created_at, Message.is_tip, Message.price_cents)
        older = (await s.execute(
            select(*cols).where(Message.account_id == str(account_id),
                                Message.fan_id == int(fan_id),
                                Message.is_unsent.is_(False),
                                Message.created_at <= anchor_at)
            .order_by(Message.created_at.desc()).limit(before)
        )).all()
        newer = (await s.execute(
            select(*cols).where(Message.account_id == str(account_id),
                                Message.fan_id == int(fan_id),
                                Message.is_unsent.is_(False),
                                Message.created_at > anchor_at)
            .order_by(Message.created_at.asc()).limit(after)
        )).all()

    # Reuse the canonical stripper rather than adding a FOURTH copy — there are
    # already three in the tree (automation_executor, event_transcoder,
    # transaction_ingest). Imported locally, like the OF client below, so this
    # module stays importable without dragging the executor in at load time.
    from automation_executor import _strip_html

    def _row(r) -> dict[str, Any]:
        mid, direction, body, created, is_tip, price = r
        return {
            "message_id": int(mid),
            "from_fan": direction == "in",
            # Bodies are stored as OF's HTML. The page renders text, so strip
            # here rather than shipping markup to a React tree.
            "text": _strip_html(body or ""),
            "at": created.isoformat() if created else None,
            "is_tip": bool(is_tip),
            "price_cents": int(price or 0),
        }

    return {"anchor_at": anchor_at.isoformat(),
            "messages": [_row(r) for r in reversed(older)] + [_row(r) for r in newer]}


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
            # No marker — either two operators clicked the same row, or this is
            # a REVIEW row (a qualifying tip nothing was watching for). Either
            # way it must SETTLE: without the stamp a review row reappears on
            # every load and the backlog can never be worked down.
            fan.customs_cleared_at = datetime.utcnow()
            await s.commit()
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
