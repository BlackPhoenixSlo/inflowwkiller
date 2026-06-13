"""
mass_broadcast_cache.py — local SQLite cache for OF's mass-message history.

Backs the /settings → Mass messages tab. The feed
`GET /users/me/stats/messages/group` returns broadcasts already sent;
each row carries the queue id needed to cancel/unsend the broadcast
from every recipient (no edit window — `unsendSeconds=1000000`).

The tab fans out across every active account, so reading direct from
OF on every open is slow + flickers. We persist the listing to
`mass_broadcast_cache` and read off SQLite for the snappy first render;
the user-initiated Refresh button (and the cancel mutation) reuse the
upstream-fetch path to update the cache.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_ as sa_and, func, not_ as sa_not, or_ as sa_or, select, update as sa_update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from db.engine import get_session
from db.models import MassBroadcastCache

log = logging.getLogger("of-relay.mass_broadcast_cache")

# Safety-net TTL — the cache is normally kept fresh by the manual Refresh
# button + the cancel mutation. Callers can use `is_stale()` to decide
# whether to auto-refresh from upstream before serving cached rows so
# the UI doesn't quietly drift if no-one clicks Refresh for days.
TTL_SECONDS = 24 * 60 * 60


def _parse_of_date(s: str | None) -> datetime | None:
    """Parse OF's ISO-8601-with-offset (`2026-05-23T17:50:13+00:00`) into a
    UTC-naive datetime. Returns None on missing / unparseable input — we
    store as nullable since OF sometimes omits the field."""
    if not s:
        return None
    try:
        # Python ≥3.11 handles +HH:MM directly. Normalize to UTC-naive — both
        # readers assume UTC (`_row_to_dict` re-stamps `+00:00`; the unsend sweep
        # compares against `datetime.utcnow()`), so `astimezone(tz=None)` (host-LOCAL)
        # would skew `sent_at` by the server's offset on any non-UTC box.
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    except (ValueError, TypeError):
        return None


def _row_to_dict(row: MassBroadcastCache) -> dict[str, Any]:
    """Project a DB row back into the JSON shape the OF feed uses, so the
    frontend doesn't have to know about two payload shapes."""
    try:
        media = json.loads(row.media_json) if row.media_json else []
    except (ValueError, TypeError):
        media = []
    try:
        previews = json.loads(row.previews_json) if row.previews_json else []
    except (ValueError, TypeError):
        previews = []
    return {
        "id": row.queue_id,
        "date": row.sent_at.isoformat() + "+00:00" if row.sent_at else None,
        "text": row.body_text,
        "rawText": row.raw_text,
        "giphyId": row.giphy_id,
        "isFree": bool(row.is_free),
        "isTip": bool(row.is_tip),
        "mediaCount": row.media_count or 0,
        "media": media,
        "previews": previews,
        "sentCount": row.sent_count or 0,
        "viewedCount": row.viewed_count or 0,
        "isCanceled": bool(row.is_canceled),
        "canUnsend": bool(row.can_unsend),
        "unsendSeconds": row.unsend_seconds,
        "template": row.template,
        "fetched_at": row.fetched_at.isoformat() + "Z" if row.fetched_at else None,
    }


async def is_stale(account_id: str, ttl_seconds: int = TTL_SECONDS) -> bool:
    """True if the freshest cached row for this account is older than the
    TTL — or there are no rows at all. Callers (e.g. /admin/mass-messages)
    use this to decide whether to fall back to an upstream refresh before
    serving from cache."""
    cutoff = datetime.utcnow() - timedelta(seconds=ttl_seconds)
    async with get_session() as s:
        newest = (
            await s.execute(
                select(func.max(MassBroadcastCache.fetched_at)).where(
                    MassBroadcastCache.account_id == account_id
                )
            )
        ).scalar_one_or_none()
    if newest is None:
        return True
    return newest < cutoff


async def list_for_account(account_id: str, *, since: datetime | None = None,
                           limit: int = 100) -> list[dict[str, Any]]:
    """Read cached rows for one account, newest broadcast first. `since`
    filters by `sent_at >= cutoff` (typically utcnow - daysBack)."""
    async with get_session() as s:
        stmt = select(MassBroadcastCache).where(
            MassBroadcastCache.account_id == account_id
        )
        if since is not None:
            stmt = stmt.where(MassBroadcastCache.sent_at >= since)
        stmt = stmt.order_by(MassBroadcastCache.sent_at.desc().nullslast()).limit(limit)
        rows = (await s.execute(stmt)).scalars().all()

        # Attribution: join mass_runs on (account_id, queue_id) so the Mass
        # Messages tab can show WHICH automation (+ funnel) produced each cached
        # broadcast. NULL automation = a manual UI broadcast or a pre-0032 run.
        attribution: dict[int, dict[str, Any]] = {}
        queue_ids = [r.queue_id for r in rows if r.queue_id is not None]
        if queue_ids:
            from db.models import MassMessageFunnel, MassRun  # local: avoid import cycle
            run_rows = (await s.execute(
                select(
                    MassRun.queue_id,
                    MassRun.automation_kind,
                    MassMessageFunnel.name.label("funnel_name"),
                )
                .outerjoin(MassMessageFunnel, MassMessageFunnel.id == MassRun.funnel_id)
                .where(MassRun.account_id == account_id)
                .where(MassRun.queue_id.in_(queue_ids))
            )).all()
            for rr in run_rows:
                attribution[int(rr.queue_id)] = {
                    "automationKind": rr.automation_kind,
                    "funnelName": rr.funnel_name,
                }

    out: list[dict[str, Any]] = []
    for r in rows:
        d = _row_to_dict(r)
        attr = attribution.get(r.queue_id) if r.queue_id is not None else None
        d["automationKind"] = attr["automationKind"] if attr else None
        d["funnelName"] = attr["funnelName"] if attr else None
        out.append(d)
    return out


async def upsert_many(account_id: str, items: list[dict[str, Any]]) -> int:
    """Write through OF response items. Returns the count of rows
    touched. Empty `items` is a no-op (we don't wipe existing rows —
    OF only ever returns a slice of the timeline)."""
    if not items:
        return 0
    now = datetime.utcnow()
    written = 0
    async with get_session() as s:
        for it in items:
            try:
                queue_id = int(it.get("id"))
            except (TypeError, ValueError):
                continue
            try:
                media_blob = json.dumps(it.get("media") or [], ensure_ascii=False, default=str)
                previews_blob = json.dumps(it.get("previews") or [], ensure_ascii=False, default=str)
            except (TypeError, ValueError):
                media_blob = "[]"
                previews_blob = "[]"
            values = {
                "account_id": account_id,
                "queue_id": queue_id,
                "sent_at": _parse_of_date(it.get("date")),
                "body_text": it.get("text"),
                "raw_text": it.get("rawText"),
                "giphy_id": it.get("giphyId"),
                "is_free": bool(it.get("isFree", True)),
                "is_tip": bool(it.get("isTip", False)),
                "media_count": int(it.get("mediaCount") or 0),
                "media_json": media_blob,
                "previews_json": previews_blob,
                "sent_count": int(it.get("sentCount") or 0),
                "viewed_count": int(it.get("viewedCount") or 0),
                "is_canceled": bool(it.get("isCanceled", False)),
                "can_unsend": bool(it.get("canUnsend", True)),
                "unsend_seconds": (
                    int(it["unsendSeconds"])
                    if isinstance(it.get("unsendSeconds"), (int, float))
                    else None
                ),
                "template": it.get("template"),
                "fetched_at": now,
            }
            stmt = sqlite_insert(MassBroadcastCache).values(**values)
            # A cancel is terminal — a broadcast never un-cancels. Keep
            # `is_canceled` monotonic (existing OR incoming) and force
            # `can_unsend` off once canceled, so a lagging OF feed that still
            # reports `isCanceled:false` for a broadcast we already unsent can't
            # resurrect the cache row and make the unsend sweep re-cancel it.
            excl = stmt.excluded
            set_ = {k: v for k, v in values.items() if k not in ("account_id", "queue_id")}
            set_["is_canceled"] = sa_or(MassBroadcastCache.is_canceled, excl.is_canceled)
            set_["can_unsend"] = sa_and(
                excl.can_unsend,
                sa_not(sa_or(MassBroadcastCache.is_canceled, excl.is_canceled)),
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=["account_id", "queue_id"],
                set_=set_,
            )
            await s.execute(stmt)
            written += 1
    return written


async def mark_canceled(account_id: str, queue_id: int) -> bool:
    """Flip `is_canceled=True` + `can_unsend=False` for one broadcast.
    Called after the cancel mutation succeeds upstream so the UI's next
    read sees the new state without waiting for a refresh."""
    async with get_session() as s:
        result = await s.execute(
            sa_update(MassBroadcastCache)
            .where(
                MassBroadcastCache.account_id == account_id,
                MassBroadcastCache.queue_id == queue_id,
            )
            .values(is_canceled=True, can_unsend=False, unsend_seconds=0)
        )
        return (result.rowcount or 0) > 0
