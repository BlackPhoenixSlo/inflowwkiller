"""service/audiences.py — build mass-send audiences from the local DB.

These return plain `list[int]` fan-id lists meant to be handed to
`OFClient.send_mass_message(included_users=...)` (the `userIds` body field).
DB-first: no OF call needed — we already have every message in `messages`.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Iterable

from sqlalchemy import func, select

from db.engine import get_session
from db.models import Blacklist, Fan, Message, NudgeState

log = logging.getLogger("of-relay.audiences")


async def recent_chat_fan_ids(
    account_id: str,
    *,
    hours: float = 2.0,
    direction: str | None = None,
    limit: int | None = None,
    exclude_bots: bool = True,
    exclude_blacklisted: bool = True,
) -> list[int]:
    """Fan ids we exchanged a message with in the last `hours`, newest first.

    `direction`: None = either side (default), 'in' = fans who messaged us,
    'out' = fans we messaged. `limit` caps the list (the "N ids" cap).
    Bots and blacklisted fans are dropped by default.
    """
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    async with get_session() as s:
        # Newest activity per fan, so a `limit` keeps the MOST recent chatters.
        q = (
            select(Message.fan_id)
            .where(Message.account_id == account_id, Message.created_at >= since)
        )
        if direction in ("in", "out"):
            q = q.where(Message.direction == direction)
        q = (
            q.group_by(Message.fan_id)
            .order_by(func.max(Message.created_at).desc())
        )
        if limit:
            q = q.limit(int(limit))
        ids = [int(r) for r in (await s.execute(q)).scalars().all() if r]
        if not ids:
            return []

        drop: set[int] = set()
        if exclude_bots:
            bots = (await s.execute(
                select(Fan.fan_id).where(
                    Fan.account_id == account_id,
                    Fan.fan_id.in_(ids),
                    Fan.is_bot.is_(True),
                )
            )).scalars().all()
            drop.update(int(b) for b in bots)
        if exclude_blacklisted:
            # Blacklist is global (no account scoping) — just fan_id.
            bl = (await s.execute(
                select(Blacklist.fan_id).where(Blacklist.fan_id.in_(ids))
            )).scalars().all()
            drop.update(int(b) for b in bl)

        return [i for i in ids if i not in drop]


def resolve_window_hours(value, default: float) -> float:
    """The shared None→default / 0→off convention for contact-guard windows
    (pioneered by mass_nudge): key absent/None → `default` hours; an explicit
    0 (or negative) → 0.0 = guard off; unparseable → `default`."""
    if value is None:
        return float(default)
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return float(default)


async def contact_guard_excludes(
    account_id: str,
    *,
    outbound_hours: float | None = None,
    inbound_hours: float | None = None,
    extra_ids: Iterable[int] | None = None,
) -> set[int]:
    """Fan ids a PROACTIVE touch must skip — the cross-automation contact guard.

    Unions every "we already touched them / they're already engaged" ledger:
      • `messages` outbound within `outbound_hours` — 1:1 sends AND explicit-id
        mass sends (those write optimistic rows the moment they fire),
      • `NudgeState.last_nudged_at` within `outbound_hours` — mass_nudge /
        nudge_online stamp NudgeState INSTEAD of messages (deliberate: per-fan
        rows for every nudge would reshuffle inbox previews constantly),
      • `messages` inbound within `inbound_hours` — active repliers,
      • any `extra_ids` (explicit excludes, exclude-list members, …).

    Bots/blacklisted are NOT filtered out here — this builds an EXCLUDE set,
    not an audience; dropping a blacklisted fan from the excludes would let a
    broadcast reach them. (`recent_chat_fan_ids`' defaults are for includes.)
    A window that is None/0 is off. Returns a plain set[int].

    NOT covered (known gap): list/online broadcasts (`userLists` sends) write
    no per-fan rows until the WS/scrape reconciler backfills them — their
    recipients are invisible here for a few hours. Cadence is their guard.
    """
    out: set[int] = {int(x) for x in (extra_ids or [])}
    if outbound_hours and outbound_hours > 0:
        out |= set(await recent_chat_fan_ids(
            account_id, hours=float(outbound_hours), direction="out",
            exclude_bots=False, exclude_blacklisted=False,
        ))
        # Nudge ledger: naive-UTC cutoff to match the stored stamps.
        cutoff = datetime.utcnow() - timedelta(hours=float(outbound_hours))
        async with get_session() as s:
            nudged = (await s.execute(
                select(NudgeState.fan_id).where(
                    NudgeState.account_id == str(account_id),
                    NudgeState.last_nudged_at.is_not(None),
                    NudgeState.last_nudged_at >= cutoff,
                )
            )).scalars().all()
        out |= {int(n) for n in nudged}
    if inbound_hours and inbound_hours > 0:
        out |= set(await recent_chat_fan_ids(
            account_id, hours=float(inbound_hours), direction="in",
            exclude_bots=False, exclude_blacklisted=False,
        ))
    return out


async def resolve_mass_audience(
    account_id: str,
    *,
    included_users: list[int] | None = None,
    excluded_users: list[int] | None = None,
    recent_chat_hours: float | None = None,
    recent_chat_limit: int | None = None,
    exclude_replied_hours: float | None = None,
    exclude_inbound_hours: float | None = None,
    unread_limit: int | None = None,
    client=None,
) -> dict:
    """Merge the DB/OF-sourced audience knobs into explicit include/exclude
    fan-id lists — the SAME resolution the relay's `/messages/queue` handler
    does, lifted out so the automation path (mass_premade → send_mass_message)
    targets the identical audience as the Mass Online composer.

    ADD to the include set:
      • `recent_chat_hours` (capped to `recent_chat_limit`) — fans we exchanged
        a message with recently (newest first, from the local `messages` table),
      • `unread_limit` — fans with unread messages (the OF "Unread" inbox tab;
        needs an OF `client` — one is built lazily off-thread if not given).
    ADD to the exclude set (via `contact_guard_excludes` — any outbound TOUCH,
    so nudges stamped only in NudgeState count too, and bots/blacklisted are
    kept in the excludes):
      • `exclude_replied_hours` — fans we sent an OUTBOUND message to (or
        nudged) recently,
      • `exclude_inbound_hours` — fans who messaged us INBOUND recently.

    Bots + blacklisted fans are dropped from the INCLUDE side by
    `recent_chat_fan_ids`. Returns `{"included_users": [...],
    "excluded_users": [...]}` (deduped, order kept).
    """
    included = list(included_users or [])
    excluded = list(excluded_users or [])

    if recent_chat_hours:
        recent_ids = await recent_chat_fan_ids(
            account_id, hours=recent_chat_hours, limit=recent_chat_limit,
        )
        included = list(dict.fromkeys([*included, *recent_ids]))

    if unread_limit:
        c = client
        if c is None:
            # Off-thread: building the client + the unread lookup are blocking.
            import automation_executor as ax
            c = await asyncio.to_thread(ax._make_client, account_id)
        unread_ids = await asyncio.to_thread(
            lambda: c.unread_chat_fan_ids(max_fans=int(unread_limit))
        )
        included = list(dict.fromkeys([*included, *unread_ids]))

    if exclude_replied_hours or exclude_inbound_hours:
        guard = await contact_guard_excludes(
            account_id,
            outbound_hours=exclude_replied_hours,
            inbound_hours=exclude_inbound_hours,
        )
        excluded += sorted(guard)
    excluded = list(dict.fromkeys(excluded))

    return {"included_users": included, "excluded_users": excluded}
