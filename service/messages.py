"""
messages.py — server-side endpoints for the per-chat message timeline.

Four endpoints, all keyed on (account_id, fan_id):

  GET    /admin/messages/{account_id}/{fan_id}?limit=&before_id=
                                          → paginated history (newest first)
  GET    /admin/messages/{account_id}/{fan_id}/{message_id}
                                          → single message
  POST   /admin/messages/{account_id}/{fan_id}/mark-read
                                          → zero out chats.unread_count
                                            (LOCAL only — the OF call lives
                                            in the existing relay endpoint)
  PATCH  /admin/messages/{account_id}/{fan_id}/{message_id}/flag
                                          → upsert message_flags row keyed
                                            on (…, flagged_by_employee_id
                                            from X-Employee-Id header)

This file owns NONE of the OF-network logic — every row served comes from
the local `messages` / `message_flags` / `chats` tables that the WS
transcoder populates. Outbound sends + OF-side read receipts live in
of_client.py and the relay endpoints in server.py.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
from datetime import datetime, timedelta
from typing import Any, AsyncIterator

from fastapi import APIRouter, Body, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from db.engine import get_session
from db.models import (
    Account, Chat, Employee, Fan, MassBroadcastCache, MassMessageFunnel,
    MassRun, Message, MessageFlag, MessageMedia,
)
from stats import _parse_date
from auth import assert_account_owned, assert_employee_filter_owned, clamp_account_filter


# CSV server-side cap. Streamed so memory stays bounded; the file size
# (~50-200 MB at full cap) is the real ceiling, not RAM. Beyond this we
# emit a TRUNCATED sentinel row + X-Row-Count header so the UI can warn.
_CSV_MAX_ROWS = 50_000
_CSV_HEADER = [
    "message_id",
    "account_id",
    "account_nickname",
    "fan_id",
    "fan_username",
    "fan_display_name",
    "direction",
    "sent_at_iso",
    "body",
    "media_count",
    "price_cents",
    "is_paid",
    "is_tip",
    "purchased_at_iso",
    "sent_by_employee_id",
    "employee_name",
    "automation_kind",
]


log = logging.getLogger("of-relay.messages")
router = APIRouter()


def _row_to_dict(
    m: Message, media: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "account_id": m.account_id,
        "fan_id": m.fan_id,
        "message_id": m.message_id,
        "direction": m.direction,
        "sender_name": m.sender_name,
        "body": m.body,
        "media_ids": _safe_load_list(m.media_ids),
        # Per-item media with NULLABLE width/height — the render-stability
        # cache (18_chat_render_stability §1.1). `media_ids` (id-only, legacy)
        # is kept for back-compat; `media` carries the dims the skeleton/grid
        # need. Empty until a live OF fetch / client PATCH fills message_media.
        "media": media or [],
        "media_count": m.media_count,
        "price_cents": m.price_cents,
        "is_paid": m.is_paid,
        "is_tip": m.is_tip,
        "is_unsent": m.is_unsent,
        "unsent_by_employee_id": m.unsent_by_employee_id,
        "unsent_reason": m.unsent_reason,
        "unsent_at": m.unsent_at.isoformat() if m.unsent_at else None,
        "is_promise": m.is_promise,
        "promise_due_date": (
            m.promise_due_date.isoformat() if m.promise_due_date else None
        ),
        "purchased_at": m.purchased_at.isoformat() if m.purchased_at else None,
        "sent_by_employee_id": m.sent_by_employee_id,
        "temp_id": m.temp_id,
        "mass_run_id": m.mass_run_id,
        "funnel_step": m.funnel_step,
        "automation_kind": m.automation_kind,
        "created_at": m.created_at.isoformat() if m.created_at else None,
        "ingested_at": m.ingested_at.isoformat() if m.ingested_at else None,
    }


def _safe_load_list(raw: str | None) -> list[Any]:
    if not raw:
        return []
    try:
        v = json.loads(raw)
        return v if isinstance(v, list) else []
    except (ValueError, TypeError):
        return []


# ── media dimensions (18_chat_render_stability §1.1) ────────────────────
#
# message_media is the render-stability cache: one row per media item, with
# NULLABLE width/height. Dims are NEVER read from a Message.raw_json — the WS
# event carries only the media id (DA bug 1). They come from two lazy sources:
#   1. the live OF REST /chats/{id}/messages media objects, whose
#      `files`/`info` blocks carry source/full/preview width+height
#      (verified against real OF payloads — see reconcile_rest_media);
#   2. a client onload→PATCH for items OF never sized.
# Every consumer tolerates null dims (neutral skeleton box).

# OF sub-blocks that may carry REAL (non-square) pixel dims, best first.
# `thumb` is deliberately EXCLUDED: OF thumbs are 300x300 squares that would
# distort the aspect ratio the skeleton/grid reserve.
_DIM_KEYS = ("full", "source", "preview")


def _as_pos_int(v: Any) -> int | None:
    """Coerce an OF width/height to a positive int, else None."""
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def extract_media_dims(media: dict) -> tuple[int | None, int | None]:
    """Pull natural (width, height) from one OF REST media object.

    Reads files.{full,source,preview}, then the same under `info`, then a
    top-level width/height. Returns (None, None) for a WS-shaped media item
    (id only) — which is exactly why raw_json can't be the dims source."""
    if not isinstance(media, dict):
        return (None, None)
    for container_key in ("files", "info"):
        container = media.get(container_key)
        if not isinstance(container, dict):
            continue
        for k in _DIM_KEYS:
            sub = container.get(k)
            if isinstance(sub, dict):
                w = _as_pos_int(sub.get("width"))
                h = _as_pos_int(sub.get("height"))
                if w and h:
                    return (w, h)
    # Some payloads put width/height directly on the media object.
    return (_as_pos_int(media.get("width")), _as_pos_int(media.get("height")))


def _media_urls(media: dict) -> tuple[str | None, str | None]:
    """(thumb_url, source_url) from an OF media object, tolerating shape drift."""
    thumb_url = source_url = None
    files = media.get("files") if isinstance(media, dict) else None
    if isinstance(files, dict):
        thumb = files.get("thumb")
        if isinstance(thumb, dict):
            thumb_url = thumb.get("url")
        for k in _DIM_KEYS:
            sub = files.get(k)
            if isinstance(sub, dict) and sub.get("url"):
                source_url = sub.get("url")
                break
    if not source_url and isinstance(media, dict):
        source_url = media.get("url")
    return (thumb_url, source_url)


def _media_duration_ms(media: dict) -> int | None:
    """Video/audio length in ms if OF provided it (seconds upstream)."""
    if not isinstance(media, dict):
        return None
    dur = media.get("duration")
    if dur is None:
        files = media.get("files")
        if isinstance(files, dict) and isinstance(files.get("full"), dict):
            dur = files["full"].get("duration")
    try:
        secs = float(dur)
    except (TypeError, ValueError):
        return None
    return int(secs * 1000) if secs > 0 else None


def _media_to_dict(mm: MessageMedia) -> dict[str, Any]:
    """message_media row → the OFMedia-ish shape the seed payload returns.
    width/height stay nullable; the UI tolerates null (neutral skeleton box)."""
    return {
        "id": mm.media_id,
        "type": mm.type,
        "width": mm.width,
        "height": mm.height,
        "thumb_url": mm.thumb_url,
        "source_url": mm.source_url,
        "duration_ms": mm.duration_ms,
    }


async def _load_media_for_messages(
    s: AsyncSession, account_id: str, fan_id: int, message_ids: list[int],
) -> dict[int, list[dict[str, Any]]]:
    """message_id → its message_media rows (as dicts), for a page of messages."""
    if not message_ids:
        return {}
    rows = (
        await s.execute(
            select(MessageMedia).where(
                MessageMedia.account_id == account_id,
                MessageMedia.fan_id == fan_id,
                MessageMedia.message_id.in_(message_ids),
            )
        )
    ).scalars().all()
    out: dict[int, list[dict[str, Any]]] = {}
    for mm in rows:
        out.setdefault(mm.message_id, []).append(_media_to_dict(mm))
    return out


async def reconcile_rest_media(
    s: AsyncSession, account_id: str, fan_id: int, of_messages: list[dict],
) -> None:
    """Upsert message_media rows from OF REST messages — the PRIMARY dims
    source. One row per media item, dims from files/info when present.

    FK-safe: message_media is a CASCADE child of `messages`, so we only write
    rows whose parent message already exists (the WS pump / outbound
    attribution own message persistence — T-API does not take that over;
    cold-start rows still get dims inline on the live payload). Existing dims
    survive on conflict via COALESCE, so a client onload→PATCH is never
    clobbered by a later REST refresh that happens to lack dims."""
    by_msg: dict[int, list[dict]] = {}
    for m in of_messages or []:
        if not isinstance(m, dict):
            continue
        mid = m.get("id")
        media = m.get("media")
        if isinstance(mid, int) and isinstance(media, list) and media:
            by_msg[mid] = media
    if not by_msg:
        return
    existing = set(
        (
            await s.execute(
                select(Message.message_id).where(
                    Message.account_id == account_id,
                    Message.fan_id == fan_id,
                    Message.message_id.in_(list(by_msg.keys())),
                )
            )
        ).scalars()
    )
    if not existing:
        return
    now = datetime.utcnow()
    wrote = False
    for mid, media in by_msg.items():
        if mid not in existing:
            continue
        for item in media:
            if not isinstance(item, dict) or not isinstance(item.get("id"), int):
                continue
            w, h = extract_media_dims(item)
            thumb_url, source_url = _media_urls(item)
            ins = sqlite_insert(MessageMedia).values(
                account_id=account_id,
                fan_id=fan_id,
                message_id=mid,
                media_id=item["id"],
                type=item.get("type"),
                width=w,
                height=h,
                thumb_url=thumb_url,
                source_url=source_url,
                duration_ms=_media_duration_ms(item),
                updated_at=now,
            )
            stmt = ins.on_conflict_do_update(
                index_elements=["account_id", "fan_id", "message_id", "media_id"],
                set_={
                    # COALESCE(excluded, existing): a fresh REST refresh updates
                    # fields it actually has, but never overwrites a stored value
                    # (e.g. a client onload→PATCHed dim) with a null.
                    "type": func.coalesce(ins.excluded.type, MessageMedia.type),
                    "width": func.coalesce(ins.excluded.width, MessageMedia.width),
                    "height": func.coalesce(ins.excluded.height, MessageMedia.height),
                    "thumb_url": func.coalesce(
                        ins.excluded.thumb_url, MessageMedia.thumb_url
                    ),
                    "source_url": func.coalesce(
                        ins.excluded.source_url, MessageMedia.source_url
                    ),
                    "duration_ms": func.coalesce(
                        ins.excluded.duration_ms, MessageMedia.duration_ms
                    ),
                    "updated_at": now,
                },
            )
            await s.execute(stmt)
            wrote = True
    if wrote:
        await s.commit()


async def sync_rest_media_dims(
    account_id: str, fan_id: int, of_messages: list[dict],
) -> None:
    """Live-route entry point: persist OF REST dims into message_media AND
    mutate each media item in `of_messages` to carry top-level width/height
    (so the client gets dims without parsing OF's nested files/info).

    Source order per item: OF inline dims first; then a stored message_media
    row (e.g. a prior client onload→PATCH) for items OF never sized. Opens its
    own session; best-effort — callers wrap in try/except so a media hiccup
    never breaks chat load. Leaves dims null when neither source has them."""
    if not of_messages:
        return
    need_db: dict[int, list[dict]] = {}
    for m in of_messages:
        if not isinstance(m, dict):
            continue
        media = m.get("media")
        if not isinstance(media, list):
            continue
        for item in media:
            if not isinstance(item, dict):
                continue
            w, h = extract_media_dims(item)
            if w and h:
                item["width"], item["height"] = w, h
            else:
                item.setdefault("width", None)
                item.setdefault("height", None)
                mid = item.get("id")
                if isinstance(mid, int):
                    need_db.setdefault(mid, []).append(item)
    async with get_session() as s:
        await reconcile_rest_media(s, account_id, fan_id, of_messages)
        if need_db:
            rows = (
                await s.execute(
                    select(
                        MessageMedia.media_id,
                        MessageMedia.width,
                        MessageMedia.height,
                    ).where(
                        MessageMedia.account_id == account_id,
                        MessageMedia.media_id.in_(list(need_db.keys())),
                    )
                )
            ).all()
            for media_id, w, h in rows:
                if w and h:
                    for item in need_db.get(media_id, []):
                        item["width"], item["height"] = w, h


def _flag_to_dict(f: MessageFlag) -> dict[str, Any]:
    return {
        "account_id": f.account_id,
        "fan_id": f.fan_id,
        "message_id": f.message_id,
        "flagged_by_employee_id": f.flagged_by_employee_id,
        "flagged_unread": f.flagged_unread,
        "starred": f.starred,
        "updated_at": f.updated_at.isoformat() if f.updated_at else None,
    }


async def _require_employee_id(request: Request, account_id: str | None = None) -> int:
    """Resolve the actor's Employee.id for flag/unsend writes.

    Three sources, in priority order:
      1. `X-Employee-Id` header (owner UI picks from the roster).
      2. Chatter session — auto-resolves the per-(chatter, account_owner)
         mirror Employee via the same path the audit middleware uses,
         so a chatter's stars/flags attribute to their mirror row (not
         the system Automation sentinel).
      3. None → 400. Mirrors the old contract for unauthed scripts.
    """
    from employees import resolve_outbound_employee_id
    eid = await resolve_outbound_employee_id(request, account_id)
    if eid is None:
        raise HTTPException(
            status_code=400,
            detail="X-Employee-Id header or chatter session required",
        )
    if eid <= 0:
        raise HTTPException(status_code=400, detail="X-Employee-Id must be positive")
    return eid


class FlagBody(BaseModel):
    # Both optional — omitted = leave field alone, present (incl. null falls
    # back to False) = set the new value. Mirrors fans.py PATCH semantics.
    flagged_unread: bool | None = None
    starred: bool | None = None


# ── routes ─────────────────────────────────────────────────────────────
#
# NOTE: route order matters in FastAPI. The literal-third-segment route
# (`mark-read`) is declared BEFORE the int-parameter route `/{message_id}`
# so "mark-read" doesn't try to parse as an int and 422. The PPV-list
# route at /admin/paid-messages lives at a different prefix, but is
# declared first here as a defensive measure — if anyone ever moves it
# under /admin/messages/* the parametric `{account_id}/{fan_id}` below
# would shadow it.


def _build_messages_query(
    *,
    start: datetime | None,
    end: datetime | None,
    account_ids: list[str] | None,
    employee_id: int | None,
    status: str,
    type_: str,
    direction: str,
    fan_query: str | None,
    fan_id: int | None,
    for_csv: bool,
):
    """Shared SELECT builder for both JSON paginated + CSV streaming paths.

    `for_csv=True` returns full `Message` ORM rows with `Fan`/`Employee`
    eager-loaded via `selectinload` (separate IN-query, not a join) so
    the row stream stays a tight Message-only scan. The JSON path keeps
    its narrow column list to avoid pulling `raw_json`/`media_ids` text.
    """
    if for_csv:
        # No ORM relationships are declared on Message → Fan/Employee
        # (see db/models.py Message class), so we can't use selectinload.
        # Instead we explicit-join here and let `s.stream()` iterate row
        # tuples; memory stays bounded because the consumer flushes each
        # row's CSV bytes before pulling the next.
        q = (
            select(
                Message.account_id,
                Message.fan_id,
                Message.message_id,
                Message.body,
                Message.media_count,
                Message.price_cents,
                Message.is_paid,
                Message.direction,
                Message.is_tip,
                Message.created_at,
                Message.purchased_at,
                Message.sent_by_employee_id,
                Message.automation_kind,
                Message.mass_run_id,
                Fan.of_username,
                Fan.of_display_name,
                Employee.display_name.label("employee_name"),
            )
            .select_from(Message)
            .outerjoin(
                Fan,
                and_(Fan.account_id == Message.account_id, Fan.fan_id == Message.fan_id),
            )
            .outerjoin(Employee, Employee.id == Message.sent_by_employee_id)
        )
    else:
        q = (
            select(
                Message.account_id,
                Message.fan_id,
                Message.message_id,
                Message.body,
                Message.media_count,
                Message.price_cents,
                Message.is_paid,
                Message.direction,
                Message.is_tip,
                Message.created_at,
                Message.purchased_at,
                Message.sent_by_employee_id,
                Message.automation_kind,
                Message.mass_run_id,
                Fan.of_username,
                Fan.of_display_name,
                Fan.avatar_url,
                Employee.display_name.label("employee_name"),
            )
            .select_from(Message)
            .outerjoin(
                Fan,
                and_(Fan.account_id == Message.account_id, Fan.fan_id == Message.fan_id),
            )
            .outerjoin(Employee, Employee.id == Message.sent_by_employee_id)
        )

    if direction in ("in", "out"):
        q = q.where(Message.direction == direction)
    if type_ == "paid":
        q = q.where(Message.price_cents > 0)
    elif type_ == "free":
        q = q.where(Message.price_cents == 0)
    if start is not None:
        q = q.where(Message.created_at >= start)
    if end is not None:
        q = q.where(Message.created_at <= end)
    if account_ids is not None:
        q = q.where(Message.account_id.in_(account_ids))
    if employee_id is not None:
        q = q.where(Message.sent_by_employee_id == employee_id)
    if fan_id is not None:
        q = q.where(Message.fan_id == fan_id)
    if status == "paid" and type_ != "free":
        q = q.where(Message.is_paid.is_(True))
    elif status == "unpaid" and type_ != "free":
        # NULL is_paid coexists with price_cents>0 on legacy rows
        # (OF didn't always echo the flag) — treat NULL as unpaid.
        q = q.where(or_(Message.is_paid.is_(False), Message.is_paid.is_(None)))
    if fan_query and not for_csv:
        like = f"%{fan_query}%"
        q = q.where(or_(Fan.of_username.like(like), Fan.of_display_name.like(like)))
    elif fan_query and for_csv:
        # CSV path didn't outerjoin Fan in the base select — apply via
        # a subquery on fans to keep filter semantics identical.
        like = f"%{fan_query}%"
        fan_ids = (
            select(Fan.fan_id)
            .where(Fan.account_id == Message.account_id)
            .where(or_(Fan.of_username.like(like), Fan.of_display_name.like(like)))
            .scalar_subquery()
        )
        q = q.where(Message.fan_id.in_(fan_ids))

    return q


async def _stream_csv(
    *,
    s: AsyncSession,
    q,
    account_nicknames: dict[str, str],
) -> tuple[AsyncIterator[bytes], dict[str, int]]:
    """Yield CSV bytes chunk-by-chunk. Caps at `_CSV_MAX_ROWS`; on
    overflow emits a TRUNCATED sentinel and stops.

    Counters are returned by reference (a single-key dict) so the caller
    can stash the final count in an X-Row-Count header *after* the stream
    drains — Starlette flushes headers before the body, so the value must
    be the cap when we hit it (the truncation case). We pre-compute that
    bound here rather than waiting.
    """
    counters = {"rows": 0, "truncated": 0}

    async def gen() -> AsyncIterator[bytes]:
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(_CSV_HEADER)
        yield buf.getvalue().encode("utf-8")
        buf.seek(0)
        buf.truncate(0)

        result = await s.stream(q.execution_options(yield_per=500))
        async for row in result:
            if counters["rows"] >= _CSV_MAX_ROWS:
                counters["truncated"] = 1
                writer.writerow([
                    "# TRUNCATED — narrow date range or filters",
                    "", "", "", "", "", "", "", "", "", "", "", "", "", "", "",
                ])
                yield buf.getvalue().encode("utf-8")
                buf.seek(0)
                buf.truncate(0)
                break

            writer.writerow([
                row.message_id,
                row.account_id,
                account_nicknames.get(row.account_id, ""),
                row.fan_id,
                row.of_username or "",
                row.of_display_name or "",
                row.direction,
                row.created_at.isoformat() if row.created_at else "",
                (row.body or "")[:2000],
                row.media_count,
                row.price_cents,
                "" if row.is_paid is None else ("true" if row.is_paid else "false"),
                "true" if row.is_tip else "false",
                row.purchased_at.isoformat() if row.purchased_at else "",
                row.sent_by_employee_id or "",
                row.employee_name or "",
                row.automation_kind or "",
            ])
            counters["rows"] += 1

            # Flush every row's bytes; StringIO is reset so it never grows.
            yield buf.getvalue().encode("utf-8")
            buf.seek(0)
            buf.truncate(0)

    return gen(), counters


async def _collapsed_mass_rows(
    *,
    s: AsyncSession,
    start: datetime | None,
    end: datetime | None,
    account_ids: list[str] | None,
    employee_id: int | None,
    type_: str,
    before: tuple[datetime, int] | None,
    limit: int,
) -> list[dict[str, Any]]:
    """One summary dict per mass broadcast (mass_run) that produced outbound
    rows in the window — the collapse that replaces N per-fan rows with a
    single "sent to N fans" row in the All-Messages tab.

    Synthetic `message_id = mass_placeholder_message_id(run_id)` (5e15 + run_id)
    is the row's stable, collision-free React key / sort tiebreaker — NOT the
    sort key. Rows interleave with normal messages by send TIME (`created_at`),
    so a 6d-old broadcast sits in its true chronological slot, not pinned to the
    top. The caller paginates on a `(sent_at, message_id)` keyset; `before` is
    that composite cursor (rows must sort strictly below it).

    `recipient_count` (and `viewed_count`) come from `mass_broadcast_cache`
    (OF's `/users/me/stats/messages/group` — "126 sent · 21 viewed"), joined by
    `MassRun.queue_id`. That cache may lag a brand-new broadcast, so the count
    falls back: `cache.sent_count` → `MassRun.recipient_count` →
    COUNT(DISTINCT fan_id) placeholder. The placeholder is usually 1 for an
    explicit-userIds send (one optimistic row persisted), so the cache is the
    real fan-out.

    Direction is always "out" (a broadcast). `status` is intentionally NOT
    applied — paid/unpaid is per-recipient on a broadcast, not a property of
    the summary. `type=free|paid` filters on the broadcast's own price.
    """
    from attribution import mass_placeholder_message_id

    q = (
        select(
            Message.account_id.label("account_id"),
            Message.mass_run_id.label("mass_run_id"),
            func.count(func.distinct(Message.fan_id)).label("recipient_count"),
            func.max(Message.body).label("body"),
            func.max(Message.price_cents).label("price_cents"),
            func.max(Message.media_count).label("media_count"),
            func.max(Message.created_at).label("created_at"),
            func.max(Message.sent_by_employee_id).label("sent_by_employee_id"),
            func.max(Message.automation_kind).label("automation_kind"),
            func.max(Message.sender_name).label("sender_name"),
            func.max(Employee.display_name).label("employee_name"),
            func.max(MassMessageFunnel.name).label("funnel_name"),
            func.max(MassRun.recipient_count).label("run_recipient_count"),
            # Source of truth for "N sent · M viewed" — keyed on the run's OF
            # queue_id (one cache row per run, so max() just unwraps it).
            func.max(MassBroadcastCache.sent_count).label("cache_sent"),
            func.max(MassBroadcastCache.viewed_count).label("cache_viewed"),
        )
        .select_from(Message)
        .outerjoin(Employee, Employee.id == Message.sent_by_employee_id)
        .outerjoin(MassRun, MassRun.id == Message.mass_run_id)
        .outerjoin(MassMessageFunnel, MassMessageFunnel.id == MassRun.funnel_id)
        .outerjoin(
            MassBroadcastCache,
            and_(
                MassBroadcastCache.account_id == MassRun.account_id,
                MassBroadcastCache.queue_id == MassRun.queue_id,
            ),
        )
        .where(Message.mass_run_id.is_not(None))
        .where(Message.direction == "out")
        .group_by(Message.account_id, Message.mass_run_id)
    )
    if start is not None:
        q = q.where(Message.created_at >= start)
    if end is not None:
        q = q.where(Message.created_at <= end)
    if account_ids is not None:
        q = q.where(Message.account_id.in_(account_ids))
    if employee_id is not None:
        q = q.where(Message.sent_by_employee_id == employee_id)
    if type_ == "paid":
        q = q.where(Message.price_cents > 0)
    elif type_ == "free":
        q = q.where(Message.price_cents == 0)

    grouped = (await s.execute(q)).all()

    out: list[dict[str, Any]] = []
    for g in grouped:
        synth_id = mass_placeholder_message_id(int(g.mass_run_id))
        created_at = g.created_at
        # Keyset on (created_at, synth_id): only runs strictly below the
        # cursor belong on this page. Mirrors the normal-rows WHERE clause.
        if before is not None and created_at is not None:
            b_at, b_id = before
            if not (created_at < b_at or (created_at == b_at and synth_id < b_id)):
                continue
        body = g.body or ""
        out.append({
            "account_id": g.account_id,
            "fan_id": 0,  # no single fan — distinct (account, synth_id) keeps the React key unique
            "message_id": synth_id,
            "fan": {"username": None, "display_name": None, "avatar_url": None},
            "body": body[:2000],
            "body_truncated": len(body) > 2000,
            "media_count": int(g.media_count or 0),
            "price_cents": int(g.price_cents or 0),
            "is_paid": False,
            "direction": "out",
            "is_tip": False,
            "sent_at": created_at.isoformat() if created_at else None,
            "purchased_at": None,
            "sent_by_employee_id": g.sent_by_employee_id,
            "employee_name": g.employee_name,
            # Denormalized "mass-<who>" tag stamped on the rows at send time
            # (attribution.mass_sender_tag); the summary row prefers it over the
            # Employee join so it survives an employee delete.
            "sender_name": g.sender_name,
            "automation_kind": g.automation_kind,
            # Mass-summary extras — the row renderer switches on is_mass_summary.
            "is_mass_summary": True,
            "mass_run_id": int(g.mass_run_id),
            # Cache is the truth; fall back when the stats poll hasn't run yet.
            "recipient_count": int(
                g.cache_sent or g.run_recipient_count or g.recipient_count or 0
            ),
            "viewed_count": int(g.cache_viewed or 0),
            "funnel_name": g.funnel_name,
        })
    # Newest broadcast first by send TIME (synth_id tiebreaker); cap to the
    # page size (caller merges with the non-mass page and re-cuts, so an
    # over-fetch here is harmless).
    out.sort(key=lambda r: (r["sent_at"] or "", r["message_id"]), reverse=True)
    return out[:limit]


@router.get("/admin/paid-messages")
async def list_paid_messages(
    request: Request,
    from_: str = Query(..., alias="from", description="ISO date or datetime; required"),
    to: str = Query(..., description="ISO date or datetime; required"),
    account_id: str | None = Query(None),
    employee_id: int | None = Query(None, description="Filters on sent_by_employee_id"),
    status: str = Query("all", pattern="^(paid|unpaid|all)$"),
    type_: str = Query("paid", alias="type", pattern="^(paid|free|all)$"),
    direction: str = Query("out", pattern="^(out|in|any)$"),
    fan_id: int | None = Query(None, description="Exact filter on Message.fan_id"),
    fan_query: str | None = Query(None, description="Substring match on fan username/display name; ≥2 chars"),
    collapse_mass: bool = Query(
        True,
        description="Collapse each mass broadcast's per-fan rows into one "
        "'sent to N fans' summary row (JSON only). Auto-disabled when a single "
        "fan or a fan_query is in scope.",
    ),
    format: str = Query("json", pattern="^(json|csv)$"),
    limit: int = Query(50, ge=1, le=100),
    before_sent_at: str | None = Query(
        None,
        description="Keyset cursor (primary): ISO send-time of the last row on "
        "the previous page. Paired with before_id as the tiebreaker.",
    ),
    before_id: int | None = Query(
        None,
        description="Keyset cursor (tiebreaker): message_id of the last row on "
        "the previous page. With before_sent_at, paginates by (sent_at, "
        "message_id) DESC; alone, falls back to legacy message_id-only paging.",
    ),
) -> Any:
    """Paginated message list — newest first by send TIME.

    Ordered/paginated on a `(sent_at, message_id)` DESC keyset (message_id
    is the tiebreaker), so collapsed mass-broadcast summaries — which carry a
    synthetic 5e15-band message_id — interleave with normal rows by their real
    send time instead of pinning to page 1.

    Defaults preserve the original PPV-tab semantics: `type=paid`,
    `direction=out`. The All-Messages tab drops both filters by passing
    `type=all&direction=any`; the per-fan deep link adds `fan_id=<id>`.

    Reads `messages` directly. JSON path joins `fans` for display
    name/avatar and `employees` for the sender label. No `transactions`
    join — this endpoint is messages-only by design (see Phase E
    synthesis §3, T-PAIDMSG).

    `purchased_at` may be NULL even when `is_paid=true` — the WS pump
    sometimes lags writing the timestamp. We forward the NULL as-is so
    the UI can render "just paid" without lying with a fabricated time.

    `format=csv` returns a streaming `text/csv` download (50k row cap,
    truncation sentinel + `X-Row-Count` header). `limit`/`before_id` are
    ignored in CSV mode — the export reflects the filter set, not the
    current page.
    """
    if fan_query is not None and len(fan_query) < 2:
        raise HTTPException(status_code=400, detail="fan_query must be 2+ chars")
    account_ids = clamp_account_filter(account_id)
    # Gate the second axis. Without this, a chatter with access to
    # account_X could ask `?employee_id=<arbitrary id>` and read every
    # message attributed to that random employee on account_X — see the
    # audit notes for the bypass this closed.
    await assert_employee_filter_owned(employee_id)
    start = _parse_date(from_, field="from", request=request)
    end = _parse_date(to, field="to", request=request)

    if format == "csv":
        # We open the session inside the StreamingResponse generator so
        # it stays alive for the duration of the download. Pulling the
        # account-nickname map up front keeps the row loop join-free.
        nick_map: dict[str, str] = {}
        async with get_session() as s:
            nick_rows = (await s.execute(select(Account.id, Account.nickname))).all()
            for ar in nick_rows:
                nick_map[ar.id] = ar.nickname or ""

        async def _csv_response() -> AsyncIterator[bytes]:
            async with get_session() as s2:
                q = _build_messages_query(
                    start=start, end=end,
                    account_ids=account_ids, employee_id=employee_id,
                    status=status, type_=type_, direction=direction,
                    fan_query=fan_query, fan_id=fan_id,
                    for_csv=True,
                ).order_by(Message.created_at.desc(), Message.message_id.desc())
                stream, counters = await _stream_csv(
                    s=s2, q=q, account_nicknames=nick_map,
                )
                async for chunk in stream:
                    yield chunk

        # Filename uses raw inputs so the user sees the date range they
        # typed (not the parsed naive-UTC version we computed for SQL).
        safe_from = from_.replace(":", "").replace("/", "-")[:32]
        safe_to = to.replace(":", "").replace("/", "-")[:32]
        filename = f"messages_{safe_from}_to_{safe_to}.csv"
        return StreamingResponse(
            _csv_response(),
            media_type="text/csv",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "X-Row-Count": str(_CSV_MAX_ROWS),  # upper bound; UI toasts on >= cap
                "Cache-Control": "no-store",
            },
        )

    # Collapse mass broadcasts into one summary row per run, EXCEPT when the
    # view is already narrow (a single fan or a fan search) — there the raw
    # per-fan rows are what the operator wants. Inbound has no mass rows.
    do_collapse = collapse_mass and fan_id is None and not fan_query and direction != "in"

    # Composite keyset cursor: (sent_at, message_id) DESC. before_sent_at is
    # the previous page's last send time; before_id breaks ties within the
    # same timestamp. before_dt is naive UTC to match the column convention
    # (these isoformat strings always carry a "T", so no end-of-day shift).
    before_dt: datetime | None = None
    if before_sent_at:
        try:
            before_dt = datetime.fromisoformat(before_sent_at)
        except ValueError as e:
            raise HTTPException(
                status_code=400, detail=f"invalid before_sent_at: {e}"
            ) from e
    before: tuple[datetime, int] | None = (
        (before_dt, before_id) if before_dt is not None and before_id is not None else None
    )

    async with get_session() as s:
        q = _build_messages_query(
            start=start, end=end,
            account_ids=account_ids, employee_id=employee_id,
            status=status, type_=type_, direction=direction,
            fan_query=fan_query, fan_id=fan_id,
            for_csv=False,
        )
        if do_collapse:
            # Per-fan mass rows are replaced by the summary rows below.
            q = q.where(Message.mass_run_id.is_(None))
        if before is not None:
            b_at, b_id = before
            q = q.where(
                or_(
                    Message.created_at < b_at,
                    and_(Message.created_at == b_at, Message.message_id < b_id),
                )
            )
        elif before_id is not None:
            # Legacy single-key cursor (old client that omitted before_sent_at).
            q = q.where(Message.message_id < before_id)
        q = q.order_by(Message.created_at.desc(), Message.message_id.desc()).limit(limit)

        rows = (await s.execute(q)).all()

        mass_rows: list[dict[str, Any]] = []
        if do_collapse:
            mass_rows = await _collapsed_mass_rows(
                s=s, start=start, end=end, account_ids=account_ids,
                employee_id=employee_id, type_=type_,
                before=before, limit=limit,
            )

    # 200-char body truncation kept for paid-only (default) so PPV-tab
    # payloads stay small; expanded to 2000 once the All-Messages tab
    # asks for inbound or free bodies (rendered with line-clamp client-side).
    body_cap = 200 if type_ == "paid" else 2000

    items: list[dict[str, Any]] = []
    for r in rows:
        body = r.body or ""
        items.append({
            "account_id": r.account_id,
            "fan_id": r.fan_id,
            "message_id": r.message_id,
            "fan": {
                "username": r.of_username,
                "display_name": r.of_display_name,
                "avatar_url": r.avatar_url,
            },
            "body": body[:body_cap],
            "body_truncated": len(body) > body_cap,
            "media_count": r.media_count,
            "price_cents": r.price_cents,
            "is_paid": bool(r.is_paid) if r.is_paid is not None else False,
            "direction": r.direction,
            "is_tip": bool(r.is_tip),
            "sent_at": r.created_at.isoformat() if r.created_at else None,
            "purchased_at": r.purchased_at.isoformat() if r.purchased_at else None,
            "sent_by_employee_id": r.sent_by_employee_id,
            "employee_name": r.employee_name,
            "automation_kind": r.automation_kind,
            "is_mass_summary": False,
        })

    # Merge the two (sent_at, message_id)-keyset streams (both fetched with the
    # same `< (before_sent_at, before_id)` cursor, both DESC) and re-cut to the
    # page size. The smallest kept (sent_at, id) becomes the next cursor; the
    # rows we drop here are all below it and reappear on the next page's fetch —
    # no gaps, no dupes. sent_at is an ISO string (uniform naive-UTC isoformat),
    # so a lexical compare is the same as a datetime compare.
    if mass_rows:
        items.extend(mass_rows)
        items.sort(key=lambda r: (r["sent_at"] or "", r["message_id"]), reverse=True)
        items = items[:limit]

    if len(items) == limit:
        last = items[-1]
        next_before_id = last["message_id"]
        next_before_sent_at = last["sent_at"]
    else:
        next_before_id = None
        next_before_sent_at = None
    return {
        "rows": items,
        "next_before_id": next_before_id,
        "next_before_sent_at": next_before_sent_at,
    }


@router.get("/admin/messages/{account_id}/{fan_id}")
async def list_messages(
    account_id: str,
    fan_id: int,
    limit: int = Query(50, ge=1, le=200),
    before_id: int | None = Query(None, description="Return messages with message_id < this"),
) -> dict[str, Any]:
    """Paginated timeline — newest first.

    Cursor is the last seen `message_id`. Ordering by message_id DESC
    means the PK (account_id, fan_id, message_id) covers both the filter
    and the sort, no separate sort step needed.

    Mass-send optimistic placeholders (`attribution._MASS_PLACEHOLDER_BASE`
    band) are served only while fresh (≤1h): they exist to bridge the
    send→delivery window, but a placeholder that was never reconciled — or
    whose broadcast was auto-unsent on OF — would otherwise seed the chat
    pane forever, since its synthetic id sits above every real OF id and the
    client's fresh-page merge can't retire it. The chat UI is this endpoint's
    only consumer, and the OF fetch is the truth for anything older."""
    assert_account_owned(account_id)
    from attribution import _MASS_PLACEHOLDER_BASE  # local: matches _collapsed_mass_rows
    from transaction_ingest import _TIP_MSG_ID_BASE  # band END — tips start here
    placeholder_floor = datetime.utcnow() - timedelta(hours=1)
    async with get_session() as s:
        q = (
            select(Message)
            .where(Message.account_id == account_id, Message.fan_id == fan_id)
            # The age cutoff applies ONLY to the mass-placeholder band
            # [5e15, 6e15). At 6e15 the ledger-synthesized TIP rows begin
            # (transaction_ingest._TIP_MSG_ID_BASE) — those are permanent
            # synthetic history the WS pump never saw, and this seed is their
            # ONLY render path, so they must never age out.
            .where(or_(
                Message.message_id < _MASS_PLACEHOLDER_BASE,
                Message.message_id >= _TIP_MSG_ID_BASE,
                Message.created_at >= placeholder_floor,
            ))
            .order_by(Message.message_id.desc())
            .limit(limit)
        )
        if before_id is not None:
            q = q.where(Message.message_id < before_id)
        rows = (await s.execute(q)).scalars().all()
        media_by_msg = await _load_media_for_messages(
            s, account_id, fan_id, [m.message_id for m in rows]
        )
        items = [_row_to_dict(m, media_by_msg.get(m.message_id, [])) for m in rows]
        # next_before_id: cursor the client passes next call. Null means
        # this page was shorter than `limit`, so there's nothing more.
        next_before = items[-1]["message_id"] if len(items) == limit else None
        return {
            "messages": items,
            "limit": limit,
            "next_before_id": next_before,
        }


@router.get("/admin/messages/{account_id}/{fan_id}/attribution")
async def list_message_attribution(
    account_id: str,
    fan_id: int,
    since_id: int | None = Query(None, description="Only return rows with message_id >= this"),
    limit: int = Query(200, ge=1, le=200),
) -> dict[str, Any]:
    """Map message_id → sender employee for outbound bubbles.

    LEFT JOIN messages.sent_by_employee_id → employees.id. Deleted
    employees (ON DELETE SET NULL on sent_by_employee_id, or rows that
    never had an employee captured) produce NULL display_name; we emit
    `display_name=null` so the UI's "render only if truthy" guard skips
    them — never the literal string "null"."""
    assert_account_owned(account_id)
    async with get_session() as s:
        q = (
            select(
                Message.message_id,
                Message.sent_by_employee_id,
                Message.automation_kind,
                Message.sender_name,
                Employee.display_name,
                Employee.color,
            )
            .select_from(Message)
            .outerjoin(Employee, Employee.id == Message.sent_by_employee_id)
            .where(
                Message.account_id == account_id,
                Message.fan_id == fan_id,
                Message.direction == "out",
            )
        )
        if since_id is not None:
            q = q.where(Message.message_id >= since_id)
        q = q.order_by(Message.message_id.desc()).limit(limit)
        rows = (await s.execute(q)).all()

    by_msg_id: dict[str, dict[str, Any]] = {}
    for r in rows:
        by_msg_id[str(r.message_id)] = {
            "employee_id": r.sent_by_employee_id,
            "display_name": r.display_name,
            # Which automation sent it ("of_ai_chat", "autoreply", …). When set,
            # the bubble shows the specific automation name instead of the flat
            # "Automation" sentinel that every bot send's display_name resolves to.
            "automation_kind": r.automation_kind,
            # Denormalized mass sender tag ("mass-kingsley1") for HUMAN broadcast
            # rows; "" for 1:1 sends and automation-fired mass (the Automation
            # sentinel is excluded in attribution.mass_sender_tag). The bubble
            # prefers this over the automation label / employee name.
            "sender_name": r.sender_name,
            "color": r.color,
        }
    return {"by_msg_id": by_msg_id}


def _iso(v: Any) -> datetime | None:
    """Parse an OF ISO timestamp ('...Z' or '+00:00'); None on anything odd."""
    if not isinstance(v, str) or not v:
        return None
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


async def reconcile_chats_from_of(account_id: str, of_chats: list[dict]) -> int:
    """Reconcile local `chats` from a fresh OF /chats payload — read state +
    last-message direction. Returns the number of chats touched.

    Why this exists: OF's `unreadMessagesCount` is the SOURCE OF TRUTH for read
    state (cleared when a chat is read on OF web OR via our mark-as-read), but
    nothing local mirrored it. `chats.unread_count` is WS-incremented and only
    zeroed by an app-side outbound send, so it drifted toward "every chat that
    ever got a DM" — which is why the roster blue badge read as a *total* instead
    of the real unread backlog. We piggy-back on the inbox's existing /chats fetch
    (NO new OF calls) to pull OF's truth into the tables the roster aggregate reads.

    Per chat we:
      • set `unread_count` = OF's count (unconditional — OF is authoritative), and
      • advance the last message + persist its direction MONOTONICALLY, so an
        OF-web reply (which the WS pump skips) flips the chat off the owe-reply
        badge without waiting for the next scrape.

    Monotonic-on-message_id makes the last-message advance safe against a
    concurrent WS write landing a newer inbound mid-reconcile; unread_count is set
    every time because read state isn't monotonic. Best-effort: own session,
    swallows errors — a sync hiccup must never break the inbox response it rides on.
    """
    own = str(account_id)
    touched = 0
    try:
        async with get_session() as s:
            for c in of_chats:
                wu = c.get("withUser") or {}
                fan_id = wu.get("id")
                if fan_id is None:
                    continue
                unread = int(c.get("unreadMessagesCount") or 0)
                lm = c.get("lastMessage") or {}
                lm_id = lm.get("id")
                from_id = (lm.get("fromUser") or {}).get("id")

                # (a) Ensure the last message exists locally WITH its direction so
                #     the roster's last_message_id→direction join can classify it.
                #     do_nothing: never clobber a fuller row the WS/scrape wrote.
                if lm_id is not None and from_id is not None:
                    direction = "out" if str(from_id) == own else "in"
                    await s.execute(
                        sqlite_insert(Message)
                        .values(account_id=own, fan_id=int(fan_id),
                                message_id=int(lm_id), direction=direction,
                                body=(lm.get("text") or ""),
                                created_at=_iso(lm.get("createdAt")) or datetime.utcnow())
                        .on_conflict_do_nothing(
                            index_elements=["account_id", "fan_id", "message_id"])
                    )

                # (b) unread_count = OF truth, unconditionally (create row if new).
                await s.execute(
                    sqlite_insert(Chat)
                    .values(account_id=own, fan_id=int(fan_id), unread_count=unread)
                    .on_conflict_do_update(
                        index_elements=["account_id", "fan_id"],
                        set_={"unread_count": unread})
                )

                # (c) Advance the chat's last message forward-only.
                if lm_id is not None:
                    preview = (lm.get("text") or "")[:120]
                    await s.execute(
                        sqlite_insert(Chat)
                        .values(account_id=own, fan_id=int(fan_id),
                                last_message_id=int(lm_id),
                                last_message_at=_iso(lm.get("createdAt")),
                                last_message_preview=preview)
                        .on_conflict_do_update(
                            index_elements=["account_id", "fan_id"],
                            set_={"last_message_id": int(lm_id),
                                  "last_message_at": _iso(lm.get("createdAt")),
                                  "last_message_preview": preview},
                            where=(Chat.last_message_id.is_(None))
                                  | (Chat.last_message_id < int(lm_id)),
                        )
                    )
                touched += 1
            await s.commit()
    except Exception:
        log.warning("reconcile_chats_from_of(%s) failed — best-effort", account_id,
                    exc_info=True)
    return touched


@router.post("/admin/messages/{account_id}/{fan_id}/mark-read")
async def mark_chat_read(account_id: str, fan_id: int) -> dict[str, Any]:
    """LOCAL read-marker — zeroes `chats.unread_count` for this pair.

    Does NOT call OF. The relay endpoint that POSTs to OF's chat read
    endpoint is responsible for the upstream side; this just keeps our
    inbox badge from lying after the user has clearly seen the messages.
    The inbox calls this on EVERY chat open (no OF round-trip) so a chat the
    chatter reads but doesn't reply to flips from the roster's blue (unread)
    count to orange (owe reply) immediately, even when OF already considered
    it read (so our `reconcile_chats_from_of` left nothing to clear).

    The WHERE guards on `unread_count > 0` so the rowcount tells the caller
    whether anything ACTUALLY changed — a chat opened with nothing locally
    unread returns `cleared=false`, letting the UI skip a needless roster
    refetch."""
    assert_account_owned(account_id)
    async with get_session() as s:
        stmt = (
            update(Chat)
            .where(Chat.account_id == account_id, Chat.fan_id == fan_id,
                   Chat.unread_count > 0)
            .values(unread_count=0)
        )
        result = await s.execute(stmt)
        await s.commit()
        cleared = (result.rowcount or 0) > 0
        # rowcount=0 → no chats row yet (WS hasn't transcoded one) OR it was
        # already read. Not an error — 200 with cleared=false.
        if cleared:
            # Reading a chat moves it off the roster's blue (unread) count. That
            # badge is served from a 5-min OF cache that ONLY the WS pump busts
            # (on messages) — a read isn't a message, so without this the poll
            # would keep serving the pre-read count for up to the full TTL. Bust
            # it so the next roster poll re-reads OF and the badge drop sticks.
            import relay_cache  # noqa: PLC0415 — defer; avoids an import cycle at module load
            relay_cache.invalidate("roster_count", account_id)
        return {"account_id": account_id, "fan_id": fan_id, "cleared": cleared}


@router.post("/admin/messages/{account_id}/{fan_id}/rescrape-now")
async def rescrape_chat_now(account_id: str, fan_id: int) -> dict[str, Any]:
    """Synchronously re-scrape ONE fan's chat from OF into the local DB and
    return when done — the chat ↻ refresh's "instant re-cache".

    Runs INLINE, NOT via the automation queue, on purpose. The queued
    scrape_chats path dispatches per (account, kind) behind a per-(account, kind)
    in-flight guard, so an interactive single-fan refresh would otherwise wait
    for a whole multi-minute account-wide scrape sweep that shares the same
    (account, 'scrape_chats') pair to finish first. Calling _scrape_one_chat
    directly sidesteps the queue, both bulk-lane slots, and that guard, so the
    re-cache lands in ~1-2s regardless of background sweeps. Recent-end fast-skip
    makes repeat clicks cheap. Best-effort: returns ok=false on failure — the UI
    already shows live OF data (messages/profile/PPV) regardless of this call."""
    assert_account_owned(account_id)
    try:
        # Local import: the executor pulls in the whole automation graph; defer
        # it to request time (it's already loaded in-process) to avoid a cycle.
        from automation_executor import (  # noqa: PLC0415
            _make_client, _scrape_one_chat,
            _SCRAPE_PAGE_SIZE, _SCRAPE_MAX_PAGES,
        )
        client = await asyncio.to_thread(_make_client, account_id)
        inserted, seen = await _scrape_one_chat(
            account_id, client.user_id, client, int(fan_id), None,
            page_size=_SCRAPE_PAGE_SIZE, max_pages=_SCRAPE_MAX_PAGES,
        )
        return {"account_id": account_id, "fan_id": fan_id,
                "ok": True, "inserted": inserted, "seen": seen}
    except Exception as exc:  # best-effort — never 500 a UI refresh
        log.warning("rescrape_chat_now(%s,%s) failed — best-effort",
                    account_id, fan_id, exc_info=True)
        return {"account_id": account_id, "fan_id": fan_id, "ok": False, "error": str(exc)}


@router.post("/admin/messages/detect-mass")
async def detect_mass(
    account_id: str | None = Query(
        None, description="Scope to one account; omit for every account the principal owns"
    ),
    gap_seconds: int = Query(180, ge=10, le=3600, description="Max gap between consecutive sends in one burst"),
    min_recipients: int = Query(5, ge=2, le=10000, description="Min distinct fans for a burst to count as mass"),
    dry_run: bool = Query(False, description="Report what would be tagged without writing"),
) -> dict[str, Any]:
    """Retro-tag OF-native mass blasts the WS pump stored as per-fan rows.

    Sessionizes untagged outbound rows (`mass_run_id IS NULL` AND
    `automation_kind IS NULL`) into bursts and stamps a synthetic MassRun on
    each, so the Messages list collapses them into one "MASS · sent to N" row.
    Idempotent — only touches rows not already tied to a run. See
    mass_detect.py for the burst rule."""
    from mass_detect import detect_mass_bursts

    account_ids = clamp_account_filter(account_id)
    return await detect_mass_bursts(
        account_ids=account_ids,
        gap_seconds=gap_seconds,
        min_recipients=min_recipients,
        dry_run=dry_run,
    )


class MediaDimsBody(BaseModel):
    # Positive ints only — a 0/negative measured dim is meaningless and
    # would distort the skeleton/grid. Pydantic 422s on non-positive.
    width: int = Field(..., gt=0)
    height: int = Field(..., gt=0)


@router.patch("/admin/messages/{account_id}/{fan_id}/media/{media_id}/dims")
async def patch_media_dims(
    account_id: str,
    fan_id: int,
    media_id: int,
    body: MediaDimsBody = Body(...),
) -> dict[str, Any]:
    """Client onload→PATCH for media OF never sized (18_chat_render_stability
    §1.1). The browser measures `naturalWidth/Height` on the <img> and reports
    it here so the next DB-seed render reserves the right box.

    UPDATE keyed on (account_id, media_id) — the ix_message_media_media index
    — NOT the full PK: the same asset can appear on multiple messages and they
    all share its true dims. `fan_id` stays in the path for REST symmetry with
    the sibling routes; it is not part of the WHERE. No row yet (OF unsized AND
    the parent message not persisted) → updated=false, not an error."""
    assert_account_owned(account_id)
    async with get_session() as s:
        stmt = (
            update(MessageMedia)
            .where(
                MessageMedia.account_id == account_id,
                MessageMedia.media_id == media_id,
            )
            .values(width=body.width, height=body.height, updated_at=datetime.utcnow())
        )
        result = await s.execute(stmt)
        await s.commit()
        return {
            "account_id": account_id,
            "media_id": media_id,
            "updated": (result.rowcount or 0) > 0,
        }


@router.get("/admin/messages/{account_id}/{fan_id}/{message_id}")
async def get_message(
    account_id: str, fan_id: int, message_id: int,
) -> dict[str, Any]:
    """Single message by PK. 404 if not in our local store yet."""
    assert_account_owned(account_id)
    async with get_session() as s:
        m = await s.get(Message, (account_id, fan_id, message_id))
        if m is None:
            raise HTTPException(status_code=404, detail="message not found")
        media_by_msg = await _load_media_for_messages(
            s, account_id, fan_id, [message_id]
        )
        return _row_to_dict(m, media_by_msg.get(message_id, []))


@router.patch("/admin/messages/{account_id}/{fan_id}/{message_id}/flag")
async def flag_message(
    request: Request,
    account_id: str,
    fan_id: int,
    message_id: int,
    body: FlagBody = Body(...),
) -> dict[str, Any]:
    """Upsert the (account, fan, message, employee) flag row.

    Only fields explicitly present in the body are written. Both fields
    default to False on a brand-new row. The composite PK is
    (account_id, fan_id, message_id, flagged_by_employee_id) — each
    employee owns their own flag state; we never coalesce across team."""
    assert_account_owned(account_id)
    employee_id = await _require_employee_id(request, account_id)
    payload = body.model_dump(exclude_unset=True)

    async with get_session() as s:
        key = (account_id, fan_id, message_id, employee_id)
        f = await s.get(MessageFlag, key)
        if f is None:
            f = MessageFlag(
                account_id=account_id,
                fan_id=fan_id,
                message_id=message_id,
                flagged_by_employee_id=employee_id,
                flagged_unread=bool(payload.get("flagged_unread", False)),
                starred=bool(payload.get("starred", False)),
            )
            s.add(f)
        else:
            if "flagged_unread" in payload:
                f.flagged_unread = bool(payload["flagged_unread"])
            if "starred" in payload:
                f.starred = bool(payload["starred"])
            f.updated_at = datetime.utcnow()
        await s.commit()
        await s.refresh(f)
        return _flag_to_dict(f)
