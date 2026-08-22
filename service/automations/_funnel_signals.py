"""service/automations/_funnel_signals.py — reply/purchase SIGNAL reads for
reply_mass_funnel.

The DB-first questions the walker asks about a fan mid-funnel — "when did we last
message them for this run?", "have they replied since?", "what did they say?",
"did they unlock a PPV?" — pulled out of the walker so the state machine reads as
orchestration and these stay a thin, testable query layer. Every read is
DB-first (the WS pump already wrote inbound) and takes its OWN AsyncSession.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select

from db.engine import get_session
from db.models import Message

from ._funnel_routing import strip_html


async def last_funnel_out_at(account_id: str, fan_id: int, mass_run_id: int) -> datetime | None:
    """Timestamp of the latest outbound message belonging to THIS funnel run
    (the broadcast row, then each step we sent — all tagged mass_run_id). Scoping
    to the run isolates the funnel from welcome_chatter_for_info/send_welcome
    noise."""
    async with get_session() as s:
        ts = (await s.execute(
            select(func.max(Message.created_at)).where(
                Message.account_id == str(account_id),
                Message.fan_id == int(fan_id),
                Message.mass_run_id == int(mass_run_id),
                Message.direction == "out",
            )
        )).scalar_one_or_none()
    return ts


async def replied_recipient_fans(account_id: str, mass_run_id: int) -> set[int]:
    """Every recipient of THIS run who has replied since our last funnel-out to
    them — the whole discovery question, in ONE query.

    This is the set-based twin of calling `last_funnel_out_at` + `has_fan_replied`
    once per recipient, and it exists because that pair was the walker's single
    most expensive line. Discovery re-asks it for EVERY recipient on EVERY tick:
    at ~1,600 recipients per broadcast, two awaited round-trips each, and a run
    list that was never bounded, one tick issued ~19,000 sequential aiosqlite
    queries and took 553 s (measured on prod 2026-08-22) to discover nobody. The
    loop is unchanged in meaning — a recipient always has an outbound row tagged
    with this run (that is what makes them a recipient), so `last_funnel_out_at`
    is never None for them and the `since is None ⇒ any inbound counts` fallback
    is unreachable here. `last_funnel_out_at` stays for the per-fan caller in the
    due-state walk, where the row count is small and the question is asked about
    one fan at a time.
    """
    async with get_session() as s:
        last_out = (
            select(
                Message.fan_id.label("fan_id"),
                func.max(Message.created_at).label("out_at"),
            )
            .where(
                Message.account_id == str(account_id),
                Message.mass_run_id == int(mass_run_id),
                Message.direction == "out",
            )
            .group_by(Message.fan_id)
            .subquery()
        )
        inbound_after = (
            select(Message.message_id)
            .where(
                Message.account_id == str(account_id),
                Message.fan_id == last_out.c.fan_id,
                Message.direction == "in",
                Message.is_unsent.is_(False),
                Message.created_at > last_out.c.out_at,
            )
            .exists()
        )
        rows = (await s.execute(
            select(last_out.c.fan_id).where(inbound_after)
        )).all()
    return {int(r[0]) for r in rows}


async def latest_reply(
    account_id: str, fan_id: int, since: datetime | None,
) -> tuple[bool, str]:
    """The fan's latest inbound strictly AFTER `since`, in ONE query → (replied,
    reply_text). `replied` is True iff any qualifying inbound exists (even an
    empty/reaction body); `reply_text` is that newest inbound's HTML-stripped body
    ('' when none, or the body is blank). Collapses the old has-replied + last-text
    pair, which ran the identical row-scan twice per due fan per tick."""
    async with get_session() as s:
        q = select(Message.body).where(
            Message.account_id == str(account_id),
            Message.fan_id == int(fan_id),
            Message.direction == "in",
            Message.is_unsent.is_(False),
        )
        if since is not None:
            q = q.where(Message.created_at > since)
        q = q.order_by(Message.created_at.desc(), Message.message_id.desc()).limit(1)
        row = (await s.execute(q)).first()
    if row is None:
        return False, ""
    return True, (strip_html(row[0]) if row[0] else "")


async def has_fan_paid_ppv(account_id: str, fan_id: int, since: datetime | None) -> bool:
    """True iff the fan UNLOCKED (paid) an outbound PPV since `since` — the
    "offer taken" signal (#30). Reads Message.is_paid, which transaction_ingest
    flips True on the `ppv_message` ledger row (and ai_chatter's isOpened
    fast-path). `since` None → any paid PPV counts. `purchased_at` is the unlock
    time; we coalesce to created_at for rows the ledger hasn't stamped yet."""
    async with get_session() as s:
        q = select(Message.message_id).where(
            Message.account_id == str(account_id),
            Message.fan_id == int(fan_id),
            Message.direction == "out",
            Message.is_paid.is_(True),
        )
        if since is not None:
            q = q.where(func.coalesce(Message.purchased_at, Message.created_at) > since)
        hit = (await s.execute(q.limit(1))).first()
    return hit is not None


async def last_inbound_at(
    account_id: str, fan_id: int, *, since: datetime | None = None,
) -> datetime | None:
    """Timestamp of the fan's latest inbound (direction='in', not unsent), or
    None. `since` bounds it to replies after our broadcast. Used to gate NEW
    funnel enrollment against a run's `discovery_closed_at` cutoff (#R4)."""
    async with get_session() as s:
        q = select(func.max(Message.created_at)).where(
            Message.account_id == str(account_id),
            Message.fan_id == int(fan_id),
            Message.direction == "in",
            Message.is_unsent.is_(False),
        )
        if since is not None:
            q = q.where(Message.created_at > since)
        return (await s.execute(q)).scalar_one_or_none()
