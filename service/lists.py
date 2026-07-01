"""
lists.py — DB-backed read endpoints for lists, list membership, and the
wall-media cache.

Per T-B5 these are cache-first; the wall-media reader here is a pure DB
read (returns whatever the cached `wall_media` + `wall_scan_state` rows
say), as distinct from /admin/vault/wall-media in server.py which
actively walks OF's /posts feed to extend coverage.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import delete, func, select, update

from db.engine import get_session
from db.models import List as ListModel
from db.models import ListMember, WallMedia, WallScanState
from auth import assert_account_owned
from audiences import MASSDMEXCLUDE_LIST, MASSPPVEXCLUDE_LIST

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


# ── MASSPPVEXCLUDE / MASSDMEXCLUDE system lists ──────────────────────
# Two per-account exclude lists (kind='exclude'), applied by SEND TYPE:
#   kind=ppv → MASSPPVEXCLUDE — skipped from mass PPV sends (priced broadcasts)
#   kind=dm  → MASSDMEXCLUDE  — skipped from mass DM sends (unpriced + online blast)
# audiences.exclude_list_fan_ids(account_id, name) reads them by name; each is
# mirrored to a same-named PINNED OnlyFans list. `kind` defaults to ppv so the
# original single-toggle callers keep hitting the PPV list. Lazily seeded.
_KIND_TO_NAME = {"ppv": MASSPPVEXCLUDE_LIST, "dm": MASSDMEXCLUDE_LIST}


def _list_name_for(kind: str) -> str:
    name = _KIND_TO_NAME.get((kind or "ppv").strip().lower())
    if name is None:
        raise HTTPException(status_code=400, detail="kind must be 'ppv' or 'dm'")
    return name


async def _system_exclude_list(s, account_id: str, name: str, *, create: bool):
    """The account's system exclude list ROW for `name` (kind='exclude').
    For MASSPPVEXCLUDE, a legacy single system exclude list (e.g. "Do Not Mass")
    is migrated into it so its members keep excluding. create=False → None when
    absent (read path never seeds); create=True → lazily seed it."""
    row = (await s.execute(
        select(ListModel).where(
            ListModel.account_id == account_id,
            ListModel.kind == "exclude",
            ListModel.name == name,
        ).order_by(ListModel.id).limit(1)
    )).scalar_one_or_none()
    if row is not None:
        return row
    if name == MASSPPVEXCLUDE_LIST:
        # Old single-list semantics were "exclude from all mass" — closest to PPV.
        legacy = (await s.execute(
            select(ListModel).where(
                ListModel.account_id == account_id,
                ListModel.kind == "exclude",
                ListModel.is_system.is_(True),
                ListModel.name != MASSDMEXCLUDE_LIST,
            ).order_by(ListModel.id).limit(1)
        )).scalar_one_or_none()
        if legacy is not None:
            legacy.name = MASSPPVEXCLUDE_LIST
            return legacy
    if not create:
        return None
    row = ListModel(
        account_id=account_id, name=name,
        kind="exclude", is_system=True, of_list_id=None,
    )
    s.add(row)
    await s.flush()
    return row


@router.get("/admin/do-not-mass")
async def get_do_not_mass(
    account_id: str = Query(...), fan_id: int = Query(...), kind: str = Query("ppv"),
) -> dict[str, Any]:
    """Is this fan on the account's MASSPPVEXCLUDE (kind=ppv) / MASSDMEXCLUDE
    (kind=dm) list?"""
    assert_account_owned(account_id)
    name = _list_name_for(kind)
    async with get_session() as s:
        row = await _system_exclude_list(s, account_id, name, create=False)
        if row is None:
            return {"do_not_mass": False}
        hit = (await s.execute(
            select(ListMember.fan_id).where(
                ListMember.list_id == row.id,
                ListMember.fan_id == int(fan_id),
            )
        )).first()
        return {"do_not_mass": hit is not None}


def _iter_of_lists(resp: Any) -> list[dict]:
    if isinstance(resp, list):
        return [x for x in resp if isinstance(x, dict)]
    if isinstance(resp, dict):
        inner = resp.get("list") or resp.get("data") or []
        return [x for x in inner if isinstance(x, dict)]
    return []


def _find_or_create_of_list(client, name: str) -> int | None:
    """OF list id named `name`, creating it if absent (blocking — run off-thread).
    Find-first so we never mint a duplicate on OF. None if OF returns no id."""
    try:
        existing = client.get_lists(limit=50, offset=0)
    except Exception:
        existing = None
    for lst in _iter_of_lists(existing):
        if str(lst.get("name", "")).strip().lower() == name.lower():
            lid = lst.get("id")
            return int(lid) if lid is not None else None
    created = client.create_list(name)
    lid = created.get("id") if isinstance(created, dict) else None
    return int(lid) if lid is not None else None


async def _sync_do_not_mass(account_id: str, fan_id: int, name: str, *, add: bool) -> dict[str, Any]:
    """Flip a fan's `name` (MASSPPVEXCLUDE / MASSDMEXCLUDE) membership on BOTH the
    local mirror (drives the DB-first mass exclusion) and a real PINNED OnlyFans
    list of the SAME NAME. OF is best-effort: the local change ALWAYS lands (the
    exclusion is what matters); `of_synced` is False when OF was unreachable."""
    # 1) Local mirror — the source of truth for the audience exclusion.
    async with get_session() as s:
        row = await _system_exclude_list(s, account_id, name, create=add)
        if row is None:                       # remove with no list → nothing to do
            return {"do_not_mass": False, "of_synced": True, "of_list_id": None}
        lid = int(row.id)
        of_list_id = row.of_list_id
        if add:
            exists = (await s.execute(select(ListMember.fan_id).where(
                ListMember.list_id == lid, ListMember.fan_id == int(fan_id),
            ))).first()
            if exists is None:
                s.add(ListMember(list_id=lid, fan_id=int(fan_id)))
        else:
            await s.execute(delete(ListMember).where(
                ListMember.list_id == lid, ListMember.fan_id == int(fan_id),
            ))
        members = [int(x) for x in (await s.execute(
            select(ListMember.fan_id).where(ListMember.list_id == lid),
        )).scalars().all()]

    # 2) OnlyFans mirror — best-effort, off-thread (blocking OF client).
    of_synced = True
    try:
        import automation_executor as ax
        client = await asyncio.to_thread(ax._make_client, account_id)
        newly_created = False
        if of_list_id is None and add:
            # First use on this account: find-or-create + pin the OF list, then
            # backfill every current local member so the two mirrors match.
            of_list_id = await asyncio.to_thread(
                _find_or_create_of_list, client, name)
            newly_created = of_list_id is not None
            if of_list_id is not None:
                try:
                    await asyncio.to_thread(client.set_list_pinned_to_chat, of_list_id, True)
                except Exception:
                    log.warning("massppvexclude: OF pin failed (non-fatal)", exc_info=True)
                async with get_session() as s:
                    await s.execute(update(ListModel).where(ListModel.id == lid)
                                    .values(of_list_id=of_list_id))
        if of_list_id is not None:
            if newly_created:
                for m in members:            # includes the just-added fan
                    try:
                        await asyncio.to_thread(client.add_user_to_list, of_list_id, m)
                    except Exception:
                        log.warning("massppvexclude: OF backfill add %s failed", m, exc_info=True)
            elif add:
                await asyncio.to_thread(client.add_user_to_list, of_list_id, int(fan_id))
            else:
                await asyncio.to_thread(client.remove_user_from_list, of_list_id, int(fan_id))
        elif add:
            of_synced = False               # wanted OF, couldn't create the list
    except Exception:
        of_synced = False
        log.warning("massppvexclude: OF sync failed (local change kept) account=%s fan=%s",
                    account_id, fan_id, exc_info=True)

    return {"do_not_mass": add, "of_synced": of_synced, "of_list_id": of_list_id}


@router.post("/admin/do-not-mass")
async def add_do_not_mass(
    account_id: str = Query(...), fan_id: int = Query(...), kind: str = Query("ppv"),
) -> dict[str, Any]:
    """Add a fan to MASSPPVEXCLUDE (kind=ppv) or MASSDMEXCLUDE (kind=dm) — seeds
    the local list AND a pinned OnlyFans list of the same name on first use,
    backfilling existing members."""
    assert_account_owned(account_id)
    return await _sync_do_not_mass(account_id, int(fan_id), _list_name_for(kind), add=True)


@router.delete("/admin/do-not-mass")
async def remove_do_not_mass(
    account_id: str = Query(...), fan_id: int = Query(...), kind: str = Query("ppv"),
) -> dict[str, Any]:
    """Take a fan off MASSPPVEXCLUDE (kind=ppv) or MASSDMEXCLUDE (kind=dm)."""
    assert_account_owned(account_id)
    return await _sync_do_not_mass(account_id, int(fan_id), _list_name_for(kind), add=False)


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
