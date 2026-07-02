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
from audiences import (
    MASSDMEXCLUDE_LIST,
    MASSPPVEXCLUDE_LIST,
    _LIST_PAGE,
    _page_all,
)

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


def _resolve_of_list_id(all_lists: list[dict], name: str) -> int | None:
    """First OF list id whose name case-insensitively matches `name`, from an
    ALREADY-PAGED list snapshot. None if absent. Name is the canonical key
    (survives a stale/deleted stored of_list_id and a sibling relay that already
    minted the list)."""
    want = name.strip().lower()
    for lst in all_lists:
        if str(lst.get("name", "")).strip().lower() == want:
            lid = lst.get("id")
            try:
                return int(lid) if lid is not None else None
            except (TypeError, ValueError):
                return None
    return None


def _reconcile_of_pair_blocking(client, targets: list[dict]) -> dict[str, dict]:
    """Ensure EVERY (name → local members) target in `targets` exists as a pinned
    OF list and additively contains its local members. Blocking (sync OF client) —
    call via asyncio.to_thread.

    ONE exhaustive `/lists` page-read up front (limit=100, trust hasMore — the
    single-page-50 read this replaces silently missed lists past offset 50 and,
    worse, swallowed a read error into 'no list exists' → duplicate mint). Resolve
    each name in that snapshot; create only when genuinely absent. Additive backfill
    only — never removes OF members, since these lists are also hand-editable and the
    local DB is authoritative only for send-time exclusion, not for OF list contents.

    Returns {name: {of_list_id, created, pinned, added, add_failed, ok}}. `ok` is
    True iff the list now has a real id and every local member add landed (an empty
    list is trivially ok). Pin failure does NOT sink `ok`: OF's list/folder pickers
    render unpinned lists too (Auto_Exclude shows unpinned), so pinning is cosmetic
    chat-sidebar sugar, not a visibility gate."""
    all_lists = _page_all(lambda off: client.get_lists(limit=_LIST_PAGE, offset=off))
    out: dict[str, dict] = {}
    for t in targets:
        name = t["name"]
        members = t.get("members") or []
        res = {"of_list_id": None, "created": False, "pinned": False,
               "added": 0, "add_failed": 0, "ok": False}
        lid = _resolve_of_list_id(all_lists, name)
        if lid is None:
            created = client.create_list(name)
            lid = created.get("id") if isinstance(created, dict) else None
            try:
                lid = int(lid) if lid is not None else None
            except (TypeError, ValueError):
                lid = None
            res["created"] = lid is not None
        res["of_list_id"] = lid
        if lid is None:                       # create failed → leave ok False, move on
            out[name] = res
            continue
        try:
            client.set_list_pinned_to_chat(lid, True)
            res["pinned"] = True
        except Exception:
            log.warning("mass_exclude[%s]: OF pin failed (non-fatal) list=%s",
                        name, lid, exc_info=True)
        for m in members:                     # additive backfill; tolerate per-fan 403s
            try:
                client.add_user_to_list(lid, int(m))
                res["added"] += 1
            except Exception:
                res["add_failed"] += 1
                log.warning("mass_exclude[%s]: OF backfill add %s failed", name, m,
                            exc_info=True)
        res["ok"] = res["add_failed"] == 0
        out[name] = res
    return out


async def _ensure_pair_rows(s, account_id: str, *, create: bool) -> dict[str, ListModel | None]:
    """Both system exclude ROWS for the account, keyed by name. PPV first so its
    legacy 'Do Not Mass' migration runs before the DM lookup. create=True seeds any
    missing row (empty) so the OF reconcile can persist BOTH of_list_ids."""
    ppv = await _system_exclude_list(s, account_id, MASSPPVEXCLUDE_LIST, create=create)
    dm = await _system_exclude_list(s, account_id, MASSDMEXCLUDE_LIST, create=create)
    return {MASSPPVEXCLUDE_LIST: ppv, MASSDMEXCLUDE_LIST: dm}


async def _snapshot_pair(s, rows: dict[str, ListModel | None]) -> dict[str, dict]:
    """{name: {local_id, of_list_id, members[]}} for every present row — read
    inside the local transaction so the OF step works off a consistent snapshot."""
    snap: dict[str, dict] = {}
    for name, row in rows.items():
        if row is None:
            continue
        members = [int(x) for x in (await s.execute(
            select(ListMember.fan_id).where(ListMember.list_id == int(row.id)),
        )).scalars().all()]
        snap[name] = {"local_id": int(row.id), "of_list_id": row.of_list_id,
                      "members": members}
    return snap


async def _persist_of_list_ids(updates: dict[int, int]) -> None:
    """Write reconcile's newly-resolved/created of_list_ids back to their rows."""
    if not updates:
        return
    async with get_session() as s:
        for local_id, of_list_id in updates.items():
            await s.execute(update(ListModel).where(ListModel.id == int(local_id))
                            .values(of_list_id=int(of_list_id)))


async def _sync_do_not_mass(account_id: str, fan_id: int, name: str, *, add: bool) -> dict[str, Any]:
    """Flip a fan's `name` (MASSPPVEXCLUDE / MASSDMEXCLUDE) membership on BOTH the
    local mirror (drives the DB-first mass exclusion) and a real PINNED OnlyFans list
    of the SAME NAME. OF is best-effort: the local change ALWAYS lands (the exclusion
    is what matters); `of_synced` is False when the fan's intended OF state couldn't
    land.

    Two OF paths:
      • STEADY STATE — every system list already has an of_list_id → a single
        incremental add/remove of this fan on its list. Cheap; no paging.
      • FIRST USE / HEAL — any of_list_id is still NULL on an ADD → reconcile the
        WHOLE PAIR (page once, find-or-create + pin BOTH names, backfill each). This
        is why toggling PPV also mints MASSdmEXCLUDE: both lists appear together, and
        a stranded account (local member, NULL of_list_id, no OF list) self-heals on
        the next add. A remove never creates an OF list."""
    # 1) Local mirror — the source of truth for the audience exclusion.
    async with get_session() as s:
        rows = await _ensure_pair_rows(s, account_id, create=add)
        target = rows.get(name)
        if target is None:                    # remove with no list → nothing to do
            return {"do_not_mass": False, "of_synced": True, "of_list_id": None}
        target_local_id = int(target.id)
        if add:
            exists = (await s.execute(select(ListMember.fan_id).where(
                ListMember.list_id == target_local_id, ListMember.fan_id == int(fan_id),
            ))).first()
            if exists is None:
                s.add(ListMember(list_id=target_local_id, fan_id=int(fan_id)))
        else:
            await s.execute(delete(ListMember).where(
                ListMember.list_id == target_local_id, ListMember.fan_id == int(fan_id),
            ))
        await s.flush()
        snap = await _snapshot_pair(s, rows)

    target_of_list_id = snap[name]["of_list_id"]
    both_synced = all(v["of_list_id"] is not None for v in snap.values())

    # 2) OnlyFans mirror — best-effort, off-thread (blocking OF client).
    of_synced = True
    try:
        import automation_executor as ax
        client = await asyncio.to_thread(ax._make_client, account_id)

        if not add:
            # A remove only depends on the TARGET list, never the sibling: if the
            # target is mirrored, drop the fan from it; otherwise there is nothing on
            # OF to remove. (Never reconcile/create on a remove.)
            if target_of_list_id is not None:
                await asyncio.to_thread(client.remove_user_from_list, target_of_list_id, int(fan_id))
        elif both_synced:
            # Steady state — one incremental add on the target list.
            await asyncio.to_thread(client.add_user_to_list, target_of_list_id, int(fan_id))
        else:
            # First use / heal — reconcile the whole pair so BOTH lists exist.
            targets = [{"name": n, "members": snap[n]["members"]} for n in snap]
            result = await asyncio.to_thread(_reconcile_of_pair_blocking, client, targets)
            updates = {snap[n]["local_id"]: result[n]["of_list_id"]
                       for n in snap if result.get(n, {}).get("of_list_id") is not None}
            await _persist_of_list_ids(updates)
            target_res = result.get(name, {})
            target_of_list_id = target_res.get("of_list_id")
            of_synced = bool(target_res.get("ok"))    # target fan landed on target list
    except Exception:
        of_synced = False
        log.warning("mass_exclude[%s]: OF sync failed (local change kept) account=%s fan=%s",
                    name, account_id, fan_id, exc_info=True)

    return {"do_not_mass": add, "of_synced": of_synced, "of_list_id": target_of_list_id}


@router.post("/admin/do-not-mass")
async def add_do_not_mass(
    account_id: str = Query(...), fan_id: int = Query(...), kind: str = Query("ppv"),
) -> dict[str, Any]:
    """Add a fan to MASSPPVEXCLUDE (kind=ppv) or MASSDMEXCLUDE (kind=dm) — seeds the
    local list AND, on first use, BOTH pinned OnlyFans lists of the same names,
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


@router.post("/admin/mass-exclude/sync")
async def sync_mass_exclude(account_id: str = Query(...)) -> dict[str, Any]:
    """Idempotent repair: ensure BOTH MASSppvEXCLUDE and MASSdmEXCLUDE exist as
    pinned OnlyFans lists for the account and additively backfill each from its local
    members, persisting any newly-resolved of_list_id. Heals accounts stranded by a
    failed/never-run first sync (local member, of_list_id NULL, no OF list) WITHOUT
    an operator having to re-toggle a fan. Safe to run for every account on deploy —
    the local rows are seeded empty if absent, but no fan is ever added to a list the
    operator didn't populate. Returns per-list status."""
    assert_account_owned(account_id)
    async with get_session() as s:
        rows = await _ensure_pair_rows(s, account_id, create=True)
        snap = await _snapshot_pair(s, rows)

    try:
        import automation_executor as ax
        client = await asyncio.to_thread(ax._make_client, account_id)
        targets = [{"name": n, "members": snap[n]["members"]} for n in snap]
        result = await asyncio.to_thread(_reconcile_of_pair_blocking, client, targets)
    except Exception:
        log.warning("mass_exclude sync: OF unreachable account=%s", account_id, exc_info=True)
        return {"account_id": account_id, "of_synced": False, "lists": {}}

    updates = {snap[n]["local_id"]: result[n]["of_list_id"]
               for n in snap if result.get(n, {}).get("of_list_id") is not None}
    await _persist_of_list_ids(updates)
    return {
        "account_id": account_id,
        "of_synced": all(r.get("ok") for r in result.values()),
        "lists": result,
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
