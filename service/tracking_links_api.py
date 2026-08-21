"""service/tracking_links_api.py — Tracking / UTM links + click analytics.

Fully internal (no OnlyFans dependency). An operator mints a named link with a
short `slug`; the PUBLIC endpoint `GET /t/{slug}` records one click and 302s to
the `target_url`. Per-link analytics = clicks over time (subscriber / revenue
attribution is a deliberate TODO — there's no reliable click→sub join yet).

Routes:
  Public (share-token exempt in server.py):
    GET  /t/{slug}                                record click → 302 target_url

  Owner-gated (/admin/*):
    GET    /admin/tracking-links?account_id=      list links (+ click_count)
    POST   /admin/tracking-links                  create (auto or custom slug)
    DELETE /admin/tracking-links/{link_id}        delete (cascades clicks)
    GET    /admin/tracking-links/{link_id}/analytics?account_id=
                                                  total + recent + by-day series
"""
from __future__ import annotations

import logging
import re
import secrets
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from auth import assert_account_owned
from db.engine import get_session
from db.models import TrackingClick, TrackingLink

log = logging.getLogger("of-relay.tracking_links_api")

router = APIRouter()

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_MAX_SLUG = 48


def _slugify(text: str) -> str:
    base = _SLUG_RE.sub("-", (text or "").strip().lower()).strip("-")
    return base[:_MAX_SLUG] or "link"


def _valid_target(url: str) -> bool:
    return isinstance(url, str) and url.startswith(("http://", "https://")) and len(url) <= 2000


def _serialize(row: TrackingLink) -> dict:
    return {
        "id": row.id,
        "account_id": row.account_id,
        "name": row.name,
        "slug": row.slug,
        "target_url": row.target_url,
        "click_count": row.click_count,
        "path": f"/t/{row.slug}",
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


# ── Public redirect (NO auth — a fan clicks this) ─────────────────────

@router.get("/t/{slug}")
async def follow_tracking_link(slug: str, request: Request):
    """Record a click and 302 to the target. An unknown slug 404s; a known slug
    ALWAYS redirects.

    The click write is deliberately kept OUT of the lookup's transaction. It
    used to share it, and the try/except around it could not deliver on
    "best-effort": `s.execute(update(...))` triggers autoflush, so the INSERT is
    what raises, the swallow leaves the transaction poisoned, and the auto-commit
    in get_session.__aexit__ — which is OUTSIDE the try — then raises
    PendingRollbackError. The fan got `Internal Server Error` instead of their
    redirect, and the click wasn't recorded either. Any write failure did it:
    disk-full, corruption, a constraint, or simply another writer holding
    SQLite's write lock past busy_timeout.

    This is the one URL a real fan touches — it goes in the creator's bio — so
    resolve the target, leave the session, and only then attempt the write.
    """
    async with get_session() as s:
        row = (await s.execute(
            select(TrackingLink).where(TrackingLink.slug == slug)
        )).scalar_one_or_none()
        if row is None:
            raise HTTPException(404, "unknown link")
        link_id, target = row.id, row.target_url

    # Separate session, and the try WRAPS the commit — a failed click costs us
    # the stat, never the fan's redirect.
    try:
        async with get_session() as s2:
            s2.add(TrackingClick(
                tracking_link_id=link_id,
                referer=(request.headers.get("referer") or None),
                user_agent=(request.headers.get("user-agent") or None),
                ip=(request.client.host if request.client else None),
            ))
            await s2.execute(
                update(TrackingLink)
                .where(TrackingLink.id == link_id)
                .values(click_count=TrackingLink.click_count + 1)
            )
    except Exception:
        log.warning("tracking click record failed slug=%s", slug, exc_info=True)

    # 302 so the browser doesn't cache the hop (each click must re-record).
    return RedirectResponse(url=target, status_code=302)


# ── Owner-gated CRUD ──────────────────────────────────────────────────

@router.get("/admin/tracking-links")
async def list_tracking_links(account_id: str = Query(...)) -> dict[str, Any]:
    assert_account_owned(account_id)
    async with get_session() as s:
        rows = (await s.execute(
            select(TrackingLink).where(TrackingLink.account_id == account_id)
            .order_by(TrackingLink.created_at.desc())
        )).scalars().all()
    return {"links": [_serialize(r) for r in rows]}


class _CreateBody(BaseModel):
    account_id: str
    name: str
    target_url: str
    slug: str | None = None


@router.post("/admin/tracking-links")
async def create_tracking_link(body: _CreateBody = Body(...)) -> dict[str, Any]:
    assert_account_owned(body.account_id)
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(422, "name is required")
    if not _valid_target(body.target_url):
        raise HTTPException(422, "target_url must be an http(s) URL")
    base = _slugify(body.slug) if body.slug else _slugify(name)
    now = datetime.utcnow()

    # slug is globally unique → retry a few times with a random suffix on clash.
    # The try/except MUST sit OUTSIDE the `async with`: on a UNIQUE collision the
    # flush raises IntegrityError, get_session.__aexit__ rolls back + re-raises,
    # and we catch it here. Catching it INSIDE the block instead would let the
    # context manager's success-path auto-commit run on a dead transaction →
    # PendingRollbackError → 500 (and the retry would never fire).
    last_err: Exception | None = None
    for attempt in range(6):
        slug = base if (attempt == 0 and body.slug) else f"{base}-{secrets.token_hex(3)}"
        try:
            async with get_session() as s:
                row = TrackingLink(account_id=body.account_id, name=name, slug=slug,
                                   target_url=body.target_url, click_count=0, created_at=now)
                s.add(row)
                await s.flush()
                out = _serialize(row)
            log.info("tracking_link_created id=%s account=%s slug=%s",
                     out["id"], body.account_id, slug)
            return out
        except IntegrityError as e:  # UNIQUE(slug) collision → retry with a new suffix
            last_err = e
            continue
    raise HTTPException(409, f"could not allocate a unique slug: {repr(last_err)[:120]}")


@router.delete("/admin/tracking-links/{link_id}")
async def delete_tracking_link(link_id: int) -> dict[str, Any]:
    async with get_session() as s:
        row = await s.get(TrackingLink, link_id)
        if row is None:
            raise HTTPException(404, "tracking link not found")
        assert_account_owned(row.account_id)
        await s.delete(row)  # clicks cascade (FK ondelete=CASCADE)
    log.info("tracking_link_deleted id=%s", link_id)
    return {"deleted": link_id}


@router.get("/admin/tracking-links/{link_id}/analytics")
async def tracking_link_analytics(link_id: int, account_id: str = Query(...)) -> dict[str, Any]:
    assert_account_owned(account_id)
    async with get_session() as s:
        row = await s.get(TrackingLink, link_id)
        if row is None or row.account_id != account_id:
            raise HTTPException(404, "tracking link not found")
        total = (await s.execute(
            select(func.count()).select_from(TrackingClick)
            .where(TrackingClick.tracking_link_id == link_id)
        )).scalar_one()
        recent = (await s.execute(
            select(TrackingClick.clicked_at, TrackingClick.referer)
            .where(TrackingClick.tracking_link_id == link_id)
            .order_by(TrackingClick.clicked_at.desc())
            .limit(25)
        )).all()
        by_day = (await s.execute(
            select(func.date(TrackingClick.clicked_at), func.count())
            .where(TrackingClick.tracking_link_id == link_id)
            .group_by(func.date(TrackingClick.clicked_at))
            .order_by(func.date(TrackingClick.clicked_at))
        )).all()
    return {
        "link": _serialize(row),
        "total_clicks": int(total or 0),
        "recent": [{"clicked_at": c.isoformat() if c else None, "referer": r}
                   for c, r in recent],
        "by_day": [{"day": str(d), "clicks": int(n)} for d, n in by_day],
        # subscribers / revenue attribution is intentionally not computed — no
        # reliable click→subscriber join exists yet.
        "subscribers": None,
        "revenue_cents": None,
    }
