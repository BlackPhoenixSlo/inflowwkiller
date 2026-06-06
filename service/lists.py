"""
lists.py — DB-backed read endpoints for lists, list membership, and the
wall-media cache.

Per T-B5 these are cache-first; the wall-media reader here is a pure DB
read (returns whatever the cached `wall_media` + `wall_scan_state` rows
say), as distinct from /admin/vault/wall-media in server.py which
actively walks OF's /posts feed to extend coverage.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import func, select

from db.engine import get_session
from db.models import List as ListModel
from db.models import ListMember, WallMedia, WallScanState
from auth import assert_account_owned

log = logging.getLogger("of-relay.lists")
router = APIRouter()


def _list_to_dict(row: ListModel, member_count: int) -> dict[str, Any]:
    return {
        "id": row.id,
        "account_id": row.account_id,
        "of_list_id": row.of_list_id,
        "name": row.name,
        "kind": row.kind,
        "query_json": _safe_load_json(row.query_json),
        "is_system": bool(row.is_system),
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "member_count": int(member_count or 0),
    }


def _list_member_to_dict(m: ListMember) -> dict[str, Any]:
    return {
        "list_id": m.list_id,
        "fan_id": m.fan_id,
        "added_at": m.added_at.isoformat() if m.added_at else None,
        "added_by_employee_id": m.added_by_employee_id,
    }


def _safe_load_json(raw: str | None) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


@router.get("/admin/lists")
async def list_lists(account_id: str = Query(...)) -> dict[str, Any]:
    """All lists for an account, each annotated with its member count.

    Single SQL with a LEFT JOIN + COUNT so we don't fire N+1 member-count
    queries when an account has dozens of lists."""
    assert_account_owned(account_id)
    async with get_session() as s:
        q = (
            select(ListModel, func.count(ListMember.fan_id).label("member_count"))
            .outerjoin(ListMember, ListMember.list_id == ListModel.id)
            .where(ListModel.account_id == account_id)
            .group_by(ListModel.id)
            .order_by(ListModel.name)
        )
        rows = (await s.execute(q)).all()
        return {"lists": [_list_to_dict(r[0], r[1]) for r in rows]}


@router.get("/admin/lists/{list_id}/members")
async def list_list_members(
    list_id: int,
    limit: int = Query(500, ge=1, le=2000),
) -> dict[str, Any]:
    """All fan_ids in a list, newest-add first. 404 if the list doesn't
    exist (vs an empty list, which means the list exists but is empty).
    Ownership: the list's account_id must belong to the signed-in friend."""
    async with get_session() as s:
        list_row = (
            await s.execute(select(ListModel.id, ListModel.account_id).where(ListModel.id == list_id))
        ).first()
        if list_row is None:
            raise HTTPException(status_code=404, detail="list not found")
        # Reuse the same 404 message so we don't leak existence of other
        # users' lists. assert_account_owned would 403; we want 404 here.
        from auth import get_request_user
        _u = get_request_user()
        if _u is not None and list_row.account_id not in _u.account_ids:
            raise HTTPException(status_code=404, detail="list not found")
        rows = (
            await s.execute(
                select(ListMember)
                .where(ListMember.list_id == list_id)
                .order_by(ListMember.added_at.desc())
                .limit(limit)
            )
        ).scalars().all()
        return {
            "list_id": list_id,
            "members": [_list_member_to_dict(m) for m in rows],
        }


@router.get("/admin/wall-media")
async def list_wall_media(account_id: str = Query(...)) -> dict[str, Any]:
    """Cached wall-media ids for one account — pure DB read.

    For the network-walking variant that extends coverage by paginating
    OF's /posts feed, see GET /admin/vault/wall-media in server.py."""
    assert_account_owned(account_id)
    async with get_session() as s:
        media_ids = (
            await s.execute(
                select(WallMedia.media_id).where(WallMedia.account_id == account_id)
            )
        ).scalars().all()
        state = (
            await s.execute(
                select(WallScanState).where(WallScanState.account_id == account_id)
            )
        ).scalar_one_or_none()
        return {
            "account_id": account_id,
            "media_ids": sorted({int(x) for x in media_ids}),
            "scan_state": {
                "newest_post_published_at": (
                    state.newest_post_published_at.isoformat()
                    if state and state.newest_post_published_at else None
                ),
                "oldest_post_published_at": (
                    state.oldest_post_published_at.isoformat()
                    if state and state.oldest_post_published_at else None
                ),
                "fully_backfilled": bool(state.fully_backfilled) if state else False,
                "last_scan_at": (
                    state.last_scan_at.isoformat()
                    if state and state.last_scan_at else None
                ),
                "scanned_posts_total": int(state.scanned_posts_total) if state else 0,
            } if state else None,
        }
