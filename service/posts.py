"""
posts.py — DB-backed read endpoints for the feed-post timeline.

Per T-B5: posts (draft/scheduled/posted/failed) are cache-first, served
from the local `posts` table populated by the WS transcoder + import
paths. The OF passthrough at /api/of/v2/posts/* is unchanged.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Query
from sqlalchemy import select

from db.engine import get_session
from db.models import Post
from auth import assert_account_owned

log = logging.getLogger("of-relay.posts")
router = APIRouter()


def _post_to_dict(p: Post) -> dict[str, Any]:
    return {
        "id": p.id,
        "account_id": p.account_id,
        "of_post_id": p.of_post_id,
        "temp_id": p.temp_id,
        "status": p.status,
        "text": p.text,
        "price_cents": p.price_cents,
        "media_ids": _safe_load_list(p.media_ids),
        "label_ids": _safe_load_list(p.label_ids),
        "scheduled_for": p.scheduled_for.isoformat() if p.scheduled_for else None,
        "posted_at": p.posted_at.isoformat() if p.posted_at else None,
        "failed_reason": p.failed_reason,
        "excluded_list_ids": _safe_load_list(p.excluded_list_ids),
        "created_by_employee_id": p.created_by_employee_id,
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "updated_at": p.updated_at.isoformat() if p.updated_at else None,
    }


def _safe_load_list(raw: str | None) -> list[Any]:
    if not raw:
        return []
    try:
        v = json.loads(raw)
        return v if isinstance(v, list) else []
    except (ValueError, TypeError):
        return []


@router.get("/admin/posts")
async def list_posts(
    account_id: str = Query(...),
    status: str | None = Query(
        None,
        description="Optional filter: draft / scheduled / posted / failed",
    ),
    limit: int = Query(200, ge=1, le=500),
) -> dict[str, Any]:
    """Posts for one account.

    Sort order depends on status:
      • scheduled → ascending by `scheduled_for` so the next-to-go is first
      • everything else → newest first by `updated_at`

    The `ix_posts_account_status` composite index covers the filtered case;
    the unfiltered case still walks the (account_id, updated_at) tail but
    is bounded by `limit`."""
    assert_account_owned(account_id)
    async with get_session() as s:
        q = select(Post).where(Post.account_id == account_id)
        if status:
            q = q.where(Post.status == status)
            if status == "scheduled":
                q = q.order_by(Post.scheduled_for.asc().nullslast())
            else:
                q = q.order_by(Post.updated_at.desc())
        else:
            q = q.order_by(Post.updated_at.desc())
        q = q.limit(limit)
        rows = (await s.execute(q)).scalars().all()
        return {"posts": [_post_to_dict(r) for r in rows]}
