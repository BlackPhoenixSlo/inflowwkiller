"""attribution_backfill — assign `transactions.attributed_employee_id` for
confirmed sales that currently resolve to NO employee (the per-employee stats
"Unattributed" bucket).

Rule ("whoever chatted the fan 1:1 at the time of the sale", chosen by ops):
credit the sender of the most recent NON-MASS outbound message — `mass_run_id
IS NULL` (a mass broadcast is not "chatting"), `sent_by_employee_id` NOT NULL
(a human chatter OR the `Automation` sentinel — an AI 1:1 close counts) — to the
same `(account, fan)` at or before the sale, within `lookback_days` (7, matching
the view's own tip window). No such 1:1 toucher → leave the sale Unattributed;
we never force-credit. (The ~$20k of tips on fans with no 1:1 chatter — mostly
mass/scraped — stay Unattributed by design.)

The attribution view `per_employee_revenue_with_attribution` already credits:
  • message-linked ppv/tip/custom rows via the linked message's own sender;
  • standalone tips via a 7-day last-toucher lookback (`standalone_tips_ranked`).
This module fills ONLY the rows those two paths leave NULL, writing to
`transactions.attributed_employee_id` — which the view's COALESCE prefers over
the live lookup, so a backfilled row wins.

Deliberately does NOT touch rows the view already attributes (message sender
present, or any 7-day toucher). NOTE: a tip whose only 7-day touch is a MASS
blast is ALREADY credited by the view (to the mass sender) and is therefore NOT
an orphan here — reclassifying those to a 1:1 chatter would require a VIEW change
(add `mass_run_id IS NULL` to `standalone_tips_ranked`) and is intentionally out
of scope. The mass exclusion below matters for message-linked orphans, where it
moves credit from a mass blast to the real 1:1 chatter.

Used by:
  • db/backfills/backfill_orphan_attribution.py — one-shot over all history
  • transaction_ingest.run_one_tick             — auto-assign after each scan
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, select, update

from db.engine import get_session
from db.models import Message, Transaction


log = logging.getLogger("attribution_backfill")

# Revenue kinds the attribution view splits per employee (mirrors the
# `message_linked` CTE in migration 0038). Subscriptions/rebills are excluded —
# they aren't chatter-driven and never enter the per-employee view.
REVENUE_KINDS: tuple[str, ...] = (
    "ppv", "ppv_message", "ppv_post", "ppv_stream",
    "tip", "tip_post", "tip_stream", "custom",
)
TIP_KINDS: tuple[str, ...] = ("tip", "tip_post", "tip_stream")
# Same statuses stats counts (see stats._TRACKED_STATUSES): pending is real
# revenue that clears in ~7d, so we attribute it now rather than waiting.
TRACKED_STATUSES: tuple[str, ...] = ("cleared", "pending")

# Last-toucher window. 7 days == the live view's own tip window, so a sale is
# credited to whoever chatted the fan "at the time of the sale", not to a stale
# touch from weeks ago. No unbounded fallback: if nobody chatted 1:1 in the
# window the sale stays Unattributed (ops' explicit choice).
DEFAULT_LOOKBACK_DAYS = 7
# MUST match the live view's `standalone_tips_ranked` window (any-sender, incl.
# mass). We treat a standalone tip as an orphan only when the view found NO
# toucher in this window — otherwise the view already credits it and we must not
# create a write/view mismatch. (This intentionally stays any-sender: mass-touch
# tips are the view's job, not this backfill's — see the module docstring.)
_VIEW_TIP_LOOKBACK_DAYS = 7


async def resolve_last_toucher(
    s: Any,
    account_id: str,
    fan_id: int | None,
    occurred_at: datetime,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> int | None:
    """Sender of the most recent NON-MASS 1:1 outbound message to `(account,
    fan)` at/before `occurred_at`, within `lookback_days`. `mass_run_id IS NULL`
    excludes broadcasts (a blast is not "chatting"); the Automation sentinel is a
    valid sender (an AI 1:1 close counts). Returns the employee id, or None when
    nobody chatted the fan 1:1 in the window — in which case the sale is left
    Unattributed (no unbounded fallback, no invented credit)."""
    if fan_id is None:
        return None

    cutoff = occurred_at - timedelta(days=lookback_days)
    row = (await s.execute(
        select(Message.sent_by_employee_id)
        .where(
            Message.account_id == account_id,
            Message.fan_id == fan_id,
            Message.direction == "out",
            Message.mass_run_id.is_(None),           # a mass blast is not chatting
            Message.sent_by_employee_id.is_not(None),
            Message.created_at <= occurred_at,
            Message.created_at > cutoff,
        )
        .order_by(Message.created_at.desc())
        .limit(1)
    )).first()
    return int(row[0]) if row is not None else None


async def _orphan_message_linked(
    s: Any, *, account_id: str | None, since: datetime | None
) -> list[Any]:
    """Revenue txns linked to a message whose own sender is NULL — the view's
    `message_linked` path leaves these in the Unattributed bucket."""
    q = (
        select(
            Transaction.id, Transaction.account_id,
            Transaction.fan_id, Transaction.occurred_at,
        )
        .join(
            Message,
            and_(
                Message.account_id == Transaction.account_id,
                Message.fan_id == Transaction.fan_id,
                Message.message_id == Transaction.message_id,
            ),
        )
        .where(
            Transaction.message_id.is_not(None),
            Transaction.kind.in_(REVENUE_KINDS),
            Transaction.status.in_(TRACKED_STATUSES),
            Transaction.attributed_employee_id.is_(None),
            Message.sent_by_employee_id.is_(None),
        )
    )
    if account_id is not None:
        q = q.where(Transaction.account_id == account_id)
    if since is not None:
        q = q.where(Transaction.occurred_at >= since)
    return list((await s.execute(q)).all())


async def _orphan_standalone_tips(
    s: Any, *, account_id: str | None, since: datetime | None
) -> list[Any]:
    """Standalone tips (no message_id) the view's 7-day lookback couldn't
    attribute — byte-for-byte the same predicate as the
    /admin/ingest/transactions/unattributed endpoint (widened to pending)."""
    toucher = (
        select(Message.message_id)
        .where(
            Message.account_id == Transaction.account_id,
            Message.fan_id == Transaction.fan_id,
            Message.direction == "out",
            Message.sent_by_employee_id.is_not(None),
            Message.created_at <= Transaction.occurred_at,
            Message.created_at
            > func.datetime(Transaction.occurred_at, f"-{_VIEW_TIP_LOOKBACK_DAYS} days"),
        )
        .correlate(Transaction)
        .exists()
    )
    q = (
        select(
            Transaction.id, Transaction.account_id,
            Transaction.fan_id, Transaction.occurred_at,
        )
        .where(
            Transaction.kind.in_(TIP_KINDS),
            Transaction.message_id.is_(None),
            Transaction.status.in_(TRACKED_STATUSES),
            Transaction.attributed_employee_id.is_(None),
            Transaction.fan_id.is_not(None),
            ~toucher,
        )
    )
    if account_id is not None:
        q = q.where(Transaction.account_id == account_id)
    if since is not None:
        q = q.where(Transaction.occurred_at >= since)
    return list((await s.execute(q)).all())


async def attribute_orphans(
    *,
    account_id: str | None = None,
    since: datetime | None = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Attribute every currently-unattributed revenue transaction to its
    last-toucher. Idempotent: only touches rows with
    `attributed_employee_id IS NULL` whose effective attribution is NULL, so
    re-running is a no-op. Returns a stats dict.

    `account_id` / `since` scope the sweep (the ingest hook passes both to keep
    each tick cheap); the one-shot backfill passes neither."""
    stats: dict[str, Any] = {
        "orphans_message_linked": 0,
        "orphans_standalone_tip": 0,
        "attributed": 0,
        "unresolved": 0,
        "by_employee": {},
        "dry_run": dry_run,
    }

    async with get_session() as s:
        linked = await _orphan_message_linked(s, account_id=account_id, since=since)
        tips = await _orphan_standalone_tips(s, account_id=account_id, since=since)
        stats["orphans_message_linked"] = len(linked)
        stats["orphans_standalone_tip"] = len(tips)

        for tx_id, aid, fid, occ in [*linked, *tips]:
            emp_id = await resolve_last_toucher(s, aid, fid, occ, lookback_days)
            if emp_id is None:
                stats["unresolved"] += 1
                continue
            if not dry_run:
                await s.execute(
                    update(Transaction)
                    .where(Transaction.id == tx_id)
                    .values(attributed_employee_id=emp_id)
                )
            stats["attributed"] += 1
            stats["by_employee"][emp_id] = stats["by_employee"].get(emp_id, 0) + 1
        # get_session commits on context exit; a dry run issued no UPDATEs so
        # the commit is a harmless no-op.

    log.info(
        "attribute_orphans account=%s since=%s lookback=%dd dry_run=%s "
        "orphans=%d attributed=%d unresolved=%d",
        account_id or "ALL",
        since.isoformat() if since else "-",
        lookback_days, dry_run,
        stats["orphans_message_linked"] + stats["orphans_standalone_tip"],
        stats["attributed"], stats["unresolved"],
    )
    return stats
