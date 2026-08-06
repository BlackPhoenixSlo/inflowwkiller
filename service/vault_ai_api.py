"""service/vault_ai_api.py — whole-vault local mirror ("Collect all") + instant local read.

The "Collect all" button walks the entire OF vault ONCE and upserts every item
into the `vault_items` mirror, so afterward the grid + search are served LOCALLY
(instant, including never-before-typed queries) instead of round-tripping OF.

  POST /admin/vault-ai/collect?account_id=            → start a sweep, returns {run_id}
  GET  /admin/vault-ai/collect/status?account_id=     → latest run progress
  GET  /admin/vault-ai/cache/summary?account_id=      → {count, last_run}
  GET  /admin/vault-ai/items?account_id=&...           → read from the mirror (local search)

The sweep pages with sort=asc (stable order) and preserves every AI / operator
field on conflict — only the mirror bookkeeping (raw_json, urls, last_seen_run_id)
is refreshed. Describe (Qwen3-VL) and folder-membership passes layer on top later.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query
from sqlalchemy import case, delete, func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

import automation_executor as ax  # _make_client seam (same one automations use)
# The permanent, signature-free picture stores (thumbs / full previews / DRM poster
# frames), including the whole route body behind the three serve endpoints below.
# This module keeps the SWEEP that fills them.
import vault_frames
import vault_mirror
import vault_stills
# NOTE: the OF payload-shape readers (`still_url`, `giphy_dm_id`) are NOT imported
# here — `vault_frames` owns "which url in a payload is the picture" now, and this
# module reaches them through it.
from auth import assert_account_owned
from db.engine import get_session
from db.models import (
    VaultCacheRun, VaultFolder, VaultFolderItem, VaultItem, VaultOfQueryLog,
)

log = logging.getLogger("of-relay.vault_ai_api")

router = APIRouter()

_PAGE = 100                # OF vault/media caps limit at 100
_MAX_PAGES = 200          # 20k-item backstop so a stuck hasMore can't loop forever
_PAGE_SLEEP_S = 0.15      # pacing: automations bypass the relay's priority lane
# Guard against two concurrent sweeps for the same account clobbering each other.
_running: set[str] = set()
# In-memory storyboard-warming progress per account (surfaced in /collect/status).
_warm_progress: dict[str, dict[str, Any]] = {}

async def _warm_store(client: Any, account_id: str, phase: str,
                      targets: list[tuple[Path, str]]) -> None:
    """Download every missing still in `targets` into its permanent path.

    One loop for all three stores — they differed only in which path a media id
    maps to, which is the caller's business and nothing else's.

    Progress uses `warmed`/`warm_total`, the keys /collect/status already reads —
    previously only the storyboard phase spoke them, so the three still phases ran
    silently behind a progress bar stuck on null."""
    total = len(targets)
    _warm_progress[account_id] = {"phase": phase, "warmed": 0, "warm_total": total}
    for n, (path, url) in enumerate(targets, 1):
        if not path.is_file():
            data = await asyncio.to_thread(vault_stills.fetch_bytes_sync, client, url)
            if data:
                vault_stills.write(path, data)
        if n % 10 == 0 or n == total:
            _warm_progress[account_id] = {"phase": phase, "warmed": n,
                                          "warm_total": total}


async def _run_collect(account_id: str, run_id: int) -> None:
    """Background sweep: page the whole vault, upsert into the mirror."""
    try:
        client = await asyncio.to_thread(ax._make_client, account_id)
        offset = 0
        pages = 0
        total = 0
        upserted = 0
        videos: list[tuple[str, float]] = []   # (signed url, duration) to warm after
        thumbs: list[tuple[int, str]] = []     # (media_id, thumb url) to cache after
        stills: list[tuple[int, str]] = []     # (media_id, full-frame url) to cache after
        drm_posters: list[tuple[int, list[str]]] = []  # DRM (media_id, poster urls)
        while pages < _MAX_PAGES:
            resp = await asyncio.to_thread(
                client.vault_media, limit=_PAGE, offset=offset, type="all", sort="asc",
            )
            items = (resp or {}).get("list") or []
            if not items:
                break
            async with get_session() as s:
                for m in items:
                    vals = vault_mirror.row_values(account_id, m, run_id)
                    if vals is None:
                        continue
                    total += 1
                    if vals.get("thumb_url"):
                        thumbs.append((vals["media_id"], vals["thumb_url"]))
                    # The full-FRAME preview, not just the 300px square. Warmed
                    # here for the same reason as the thumb: after OF's signature
                    # expires we can no longer fetch it, and a lazily-filled
                    # store means whatever nobody happened to open is lost.
                    still = vault_frames.describe_image_of(
                        m.get("files") if isinstance(m.get("files"), dict) else {}
                    )
                    if still:
                        stills.append((vals["media_id"], still))
                    if (m.get("type") == "video"):
                        vurl = vault_frames.video_url(m)
                        if vurl:
                            videos.append((vurl, float(m.get("duration") or 0)))
                        else:
                            # DRM: no sliceable mp4 → cache OF poster frames so
                            # the hover preview serves from disk, not a cold fetch.
                            pf = vault_frames.poster_frames(m)
                            if pf:
                                drm_posters.append((vals["media_id"], pf))
                    # Preserve AI / operator / describe fields on conflict —
                    # only refresh the mirror bookkeeping. `last_seen_run_id` is
                    # the sweep's own bookkeeping, so it rides on top of the
                    # shared list rather than inside it.
                    refresh = {k: vals[k]
                               for k in vault_mirror.MIRROR_REFRESH_FIELDS + ("last_seen_run_id",)}
                    stmt = (
                        sqlite_insert(VaultItem)
                        .values(**vals)
                        .on_conflict_do_update(
                            index_elements=["account_id", "media_id"],
                            set_=refresh,
                        )
                    )
                    await s.execute(stmt)
                    upserted += 1
                await s.commit()
            pages += 1
            offset += _PAGE
            async with get_session() as s:
                run = await s.get(VaultCacheRun, run_id)
                if run:
                    run.pages_done = pages
                    run.total_seen = total
                    run.upserted = upserted
                    run.phase = "media"
                    await s.commit()
            if not (resp or {}).get("hasMore"):
                break
            await asyncio.sleep(_PAGE_SLEEP_S)

        # ── Storyboard-warming phase (incremental) ──────────────────
        # Extract + cache 12 frames for every video whose storyboard isn't
        # already on disk. Metadata is already committed above, so search +
        # the grid are usable while this (slower) phase runs. Re-collect only
        # warms NEW videos — _warm_one_sync short-circuits cached ones.
        async with get_session() as s:
            run = await s.get(VaultCacheRun, run_id)
            if run:
                run.phase = "thumbs"
                await s.commit()
        # Fill the three permanent stores. All incremental — an already-cached file
        # is skipped, so a re-collect only pays for what is new.
        await _warm_store(client, account_id, "thumbs",
                          [(vault_stills.thumb_path(account_id, mid), u)
                           for mid, u in thumbs])
        # The full frames too, not just the squares (~960x1280). This is the copy
        # the review UI reads, and it is equally unrecoverable once the signature
        # it came from has aged out.
        await _warm_store(client, account_id, "images",
                          [(vault_stills.image_path(account_id, mid), u)
                           for mid, u in stills])
        # DRM clips can't be ffmpeg-storyboarded, so OF's own poster frames ARE the
        # hover preview — cached, a DRM hover is instant instead of a cold ~0.7s fetch.
        await _warm_store(client, account_id, "posters",
                          [(vault_stills.poster_path(account_id, mid, idx), u)
                           for mid, frames in drm_posters
                           for idx, u in enumerate(frames)])
        async with get_session() as s:
            run = await s.get(VaultCacheRun, run_id)
            if run:
                run.phase = "warming"
                await s.commit()
        warmed = 0
        _warm_progress[account_id] = {"phase": "warming", "warmed": 0, "warm_total": len(videos)}
        for vurl, vdur in videos:
            try:
                await asyncio.to_thread(vault_frames.warm_one_sync, client, vurl, vdur)
            except Exception:  # noqa: BLE001
                log.warning("warm dispatch failed", exc_info=True)
            warmed += 1
            _warm_progress[account_id] = {
                "phase": "warming", "warmed": warmed, "warm_total": len(videos),
            }
            await asyncio.sleep(0.02)   # light pacing between mp4 downloads
        _warm_progress.pop(account_id, None)

        async with get_session() as s:
            run = await s.get(VaultCacheRun, run_id)
            if run:
                run.status = "done"
                run.phase = "done"
                run.pages_done = pages
                run.total_seen = total
                run.upserted = upserted
                run.finished_at = datetime.utcnow()
                await s.commit()
        log.info("vault_collect done account=%s run=%s items=%s", account_id, run_id, total)
    except Exception as e:  # noqa: BLE001
        log.exception("vault_collect failed account=%s run=%s", account_id, run_id)
        try:
            async with get_session() as s:
                run = await s.get(VaultCacheRun, run_id)
                if run:
                    run.status = "error"
                    run.error = str(e)[:500]
                    run.finished_at = datetime.utcnow()
                    await s.commit()
        except Exception:  # noqa: BLE001
            pass
    finally:
        _running.discard(account_id)
        _warm_progress.pop(account_id, None)


@router.post("/admin/vault-ai/collect")
async def start_collect(account_id: str = Query(...)) -> dict[str, Any]:
    """Kick off a full-vault mirror sweep in the background."""
    assert_account_owned(account_id)
    if account_id in _running:
        raise HTTPException(status_code=409, detail={"error": "collect_already_running"})
    _running.add(account_id)
    # Drop the OF listing cache first. It holds whole response bodies whose
    # urls are time-signed, so serving one back after the signature aged out
    # hands the picker links that 403 — the same stale-url the sweep is about
    # to replace. Cheap, and it makes Re-collect mean "everything fresh".
    try:
        await vault_cache.invalidate(account_id)
    except Exception:  # noqa: BLE001
        log.warning("vault_cache invalidate failed for %s", account_id, exc_info=True)
    async with get_session() as s:
        run = VaultCacheRun(account_id=account_id, status="running", phase="media")
        s.add(run)
        await s.commit()
        run_id = run.id
    asyncio.create_task(_run_collect(account_id, run_id))
    return {"account_id": account_id, "run_id": run_id, "status": "running"}


def _run_status(row: VaultCacheRun, account_id: str) -> str:
    """What this run's status actually IS, not what it last managed to write.

    A collect is a background task and its rows are finalised by the task
    itself, so a relay restart mid-sweep leaves `running` behind with nothing
    left to ever clear it. One prod row said `running / warming` for three days;
    it is also why 1,046 clips were never warmed, and the status endpoint
    reported the sweep as still in progress the whole time.

    `_running` is the only place a live sweep is tracked and it is process-local,
    so a row claiming to run for an account nobody is running IS orphaned — the
    process that owned it is gone. Read, never written: the stored row stays the
    honest record of what the task last wrote, and a reconciliation pass that
    could itself be interrupted is machinery this does not need."""
    if row.status == "running" and account_id not in _running:
        return "interrupted"
    return row.status


# Which store(s) a `scope` names. One map, so stats and purge can't disagree about
# what "all" covers.
def _stores() -> dict[str, Path]:
    return {"thumbs": vault_stills.THUMB_DIR, "images": vault_stills.IMAGE_DIR,
            "posters": vault_stills.POSTER_DIR}


@router.get("/admin/vault-ai/stills/stats")
async def stills_stats(account_id: str = Query(...)) -> dict[str, Any]:
    """How much of this account's vault actually has PERMANENT local bytes.

    `mirror` is how many items we know about; the per-store counts are how many
    of them we can still render once OF's signed urls expire. A big gap means
    a re-collect hasn't run since the store was last lost."""
    assert_account_owned(account_id)
    async with get_session() as s:
        mirror = (
            await s.execute(
                select(func.count())
                .select_from(VaultItem)
                .where(VaultItem.account_id == account_id, VaultItem.removed_at.is_(None))
            )
        ).scalar_one()
    stores = _stores()
    out: dict[str, Any] = {"account_id": account_id, "mirror": int(mirror or 0),
                           "dirs": {k: str(d) for k, d in stores.items()}}
    for name, d in stores.items():
        out[name] = vault_stills.dir_stats(d / account_id)
    return out


@router.post("/admin/vault-ai/stills/purge")
async def stills_purge(
    account_id: str = Query(...),
    scope: str = Query("thumbs", pattern="^(thumbs|images|posters|all)$"),
) -> dict[str, Any]:
    """Delete this account's cached stills so the next Collect re-fetches them.

    Use this for a store that went BAD (truncated or wrong pictures), not as
    routine housekeeping: these bytes are the only copy that outlives OF's
    signature, so purging without a re-collect behind it is how tiles go
    blank. Re-collect is what refills them."""
    assert_account_owned(account_id)
    stores = _stores()
    targets = list(stores.values()) if scope == "all" else [stores[scope]]
    removed = vault_stills.purge(account_id, targets)
    await vault_cache.invalidate(account_id)
    return {"ok": True, "account_id": account_id, "scope": scope, "removed": removed}


@router.get("/admin/vault-ai/collect/status")
async def collect_status(account_id: str = Query(...)) -> dict[str, Any]:
    assert_account_owned(account_id)
    async with get_session() as s:
        row = (
            await s.execute(
                select(VaultCacheRun)
                .where(VaultCacheRun.account_id == account_id)
                .order_by(VaultCacheRun.started_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
    if row is None:
        return {"account_id": account_id, "run": None}
    warm = _warm_progress.get(account_id)
    return {
        "account_id": account_id,
        "run": {
            "id": row.id,
            "status": _run_status(row, account_id),
            "phase": row.phase,
            "total_seen": row.total_seen,
            "upserted": row.upserted,
            "pages_done": row.pages_done,
            "error": row.error,
            "started_at": row.started_at.isoformat() if row.started_at else None,
            "finished_at": row.finished_at.isoformat() if row.finished_at else None,
            "running": account_id in _running,
            "warmed": (warm or {}).get("warmed"),
            "warm_total": (warm or {}).get("warm_total"),
        },
    }


@router.get("/admin/vault-ai/cache/summary")
async def cache_summary(account_id: str = Query(...)) -> dict[str, Any]:
    assert_account_owned(account_id)
    async with get_session() as s:
        count = (
            await s.execute(
                select(func.count())
                .select_from(VaultItem)
                .where(VaultItem.account_id == account_id, VaultItem.removed_at.is_(None))
            )
        ).scalar_one()
        last = (
            await s.execute(
                select(VaultCacheRun)
                .where(VaultCacheRun.account_id == account_id)
                .order_by(VaultCacheRun.started_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
    return {
        "account_id": account_id,
        "count": int(count or 0),
        "running": account_id in _running,
        "last_run": None if last is None else {
            "id": last.id, "status": _run_status(last, account_id),
            "total_seen": last.total_seen,
            "finished_at": last.finished_at.isoformat() if last.finished_at else None,
        },
    }


# ── The three permanent stills ────────────────────────────────────────
#
# One route body (`vault_stills.serve`) three times over: disk, else fetch and
# keep, re-signing the url through OF if the stored one has expired. Each endpoint
# supplies only the store it reads and the rule for picking ITS url out of a media
# dict — and that rule is written once, used for both the stored payload and the
# re-signed one, so the two can't disagree.

@router.get("/admin/vault-ai/thumb")
async def vault_thumb(account_id: str = Query(...), media_id: int = Query(...)):
    """The 300px square — the grid + picker tile."""
    assert_account_owned(account_id)
    return await vault_stills.serve(
        account_id, media_id, vault_stills.thumb_path(account_id, media_id),
        vault_frames.pick_thumb)


@router.get("/admin/vault-ai/image")
async def vault_image(account_id: str = Query(...), media_id: int = Query(...)):
    """The FULL-FRAME image (~960x1280) — the review UI shows THIS, never the
    /thumb crop.

    /thumb is a 300x300 centre-crop of a 3:4 portrait: its top and bottom are
    gone, so a waistband or genitalia at the frame edge is invisible in it. That
    crop is exactly what made the vision model over-report `fully_nude`, and an
    operator judging the square would make the same mistake — they'd be
    correcting a flag against less of the picture than the model that set it saw.
    `_describe_image_of` picks the same variant the model was given, so the
    reviewer sees precisely the frame the reading was made on."""
    assert_account_owned(account_id)
    return await vault_stills.serve(
        account_id, media_id, vault_stills.image_path(account_id, media_id),
        vault_frames.pick_image)


@router.get("/admin/vault-ai/poster")
async def vault_poster(
    account_id: str = Query(...), media_id: int = Query(...), i: int = Query(0, ge=0),
):
    """Frame `i` of a DRM video's hover slideshow. The cache fills as you browse;
    Collect just pre-warms the whole vault.

    Takes no caller-supplied url. It used to accept `u=` so a hover over an
    UN-collected clip could still fill the store, gated by a host check that
    accepted any hostname containing both "cdn" and "onlyfans" — so
    `cdn.onlyfans.attacker.com` passed and the route fetched it with the
    account's own authenticated client. The parameter earned nothing: `serve`
    already resolves an unknown media by id straight from OF, which is both the
    safe source and the one that survives an expired signature."""
    assert_account_owned(account_id)
    return await vault_stills.serve(
        account_id, media_id, vault_stills.poster_path(account_id, media_id, i),
        vault_frames.pick_poster(i))


def _overlay(item: VaultItem, manual_order: int | None = None) -> dict[str, Any]:
    try:
        base = json.loads(item.raw_json) if item.raw_json else {"id": item.media_id, "type": item.kind}
    except Exception:  # noqa: BLE001
        base = {"id": item.media_id, "type": item.kind}
    base["_ai"] = {
        "describe_status": item.describe_status,
        "description": item.description,
        "video_description": item.video_description,
        "tags": load_json(item.tags, []),
        "explicitness_tier": item.explicitness_tier,
        "story_suitable": item.story_suitable,
        "tip_vault_flag": item.tip_vault_flag,
        "suggested_caption": item.suggested_caption,
        "suggested_price_cents": item.suggested_price_cents,
        "manual_order": item.manual_order if manual_order is None else manual_order,
        "review_state": item.review_state,
        "suggested_script": item.suggested_script,
    }
    # The V2 describe taxonomy (acts / clothing_state / beats / primary_folder /
    # …) lives ONLY in ai_fields_json — there are no columns for it. Without
    # this projection the browser receives just `description` + `tags` and the
    # other 21 fields the model produced are invisible, which is exactly what
    # made the vault look like it had lost them. Projected read-only: edits
    # still go through the override+lock path on the real columns.
    ai = load_json(item.ai_fields_json, None)
    if isinstance(ai, dict):
        base["_ai"]["fields"] = {
            k: v for k, v in ai.items()
            # `description`/`tags` are already served from their columns (which
            # carry any operator override); don't shadow them with the raw AI value.
            if k not in ("description", "tags")
        }
    # Stable, signature-free cached-thumb URL (self-heals on miss). Frontend
    # prefers this over the expiring OF url so tiles never break.
    base["_thumb"] = f"/admin/vault-ai/thumb?account_id={item.account_id}&media_id={item.media_id}"
    return base


@router.get("/admin/vault-ai/items")
async def local_items(
    account_id: str = Query(...),
    type: str = Query("all"),
    query: str | None = Query(None),
    sort: str = Query("newest"),
    internal_folder_id: int | None = Query(None),
    of_folder_id: int | None = Query(None),
    limit: int = Query(500, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    """Read the LOCAL mirror. Same {list, hasMore} shape as /api/of/v2/vault/media
    so the frontend swaps data source transparently. Search is a local LIKE-scan
    (instant). `internal_folder_id` filters to an internal folder and orders by
    its per-folder manual_order: 0 first, 1,2,3… from front, then unpinned by
    date, then …-3,-2,-1 (so -1 is dead last)."""
    assert_account_owned(account_id)
    q = (query or "").strip().lower()

    def _apply_common(st):
        if type and type != "all":
            st = st.where(VaultItem.kind == type)
        if q:
            like = f"%{q}%"
            st = st.where(
                func.lower(func.coalesce(VaultItem.search_text, "")).like(like)
                | func.lower(func.coalesce(VaultItem.description, "")).like(like)
                | func.lower(func.coalesce(VaultItem.tags, "")).like(like)
                | func.lower(func.coalesce(VaultItem.video_description, "")).like(like)
                # of_terms = OF's own vault-search matches (harvested by /search-of),
                # folded per item as a JSON array e.g. ["ass","boobs"]. OF indexes
                # hidden caption/content text the media dict never exposes, so this
                # is the ONLY way our local search becomes a superset of OF's.
                | func.lower(func.coalesce(VaultItem.of_terms, "")).like(like)
            )
        return st

    out: list[dict[str, Any]] = []
    async with get_session() as s:
        if internal_folder_id is not None:
            mo = VaultFolderItem.manual_order
            bucket = case((mo.is_(None), 1), (mo >= 0, 0), else_=2)
            stmt = (
                select(VaultItem, mo)
                .join(
                    VaultFolderItem,
                    (VaultFolderItem.account_id == VaultItem.account_id)
                    & (VaultFolderItem.media_id == VaultItem.media_id),
                )
                .where(
                    VaultItem.account_id == account_id,
                    VaultItem.removed_at.is_(None),
                    VaultFolderItem.folder_id == internal_folder_id,
                )
            )
            stmt = _apply_common(stmt)
            stmt = stmt.order_by(bucket, mo, VaultItem.created_at.desc()).limit(limit + 1).offset(offset)
            pairs = (await s.execute(stmt)).all()
            has_more = len(pairs) > limit
            for item, m_ord in pairs[:limit]:
                out.append(_overlay(item, m_ord))
        elif of_folder_id is not None:
            # An OF folder, served from the mirror. Membership = of_folder_ids
            # (parsed from listStates); manual_order via a LEFT join on
            # vault_folder_items keyed by the OF list id.
            mo = VaultFolderItem.manual_order
            bucket = case((mo.is_(None), 1), (mo >= 0, 0), else_=2)
            stmt = (
                select(VaultItem, mo)
                .outerjoin(
                    VaultFolderItem,
                    (VaultFolderItem.account_id == VaultItem.account_id)
                    & (VaultFolderItem.media_id == VaultItem.media_id)
                    & (VaultFolderItem.folder_id == of_folder_id),
                )
                .where(
                    VaultItem.account_id == account_id,
                    VaultItem.removed_at.is_(None),
                    func.coalesce(VaultItem.of_folder_ids, "[]").like(f"%{of_folder_id}%"),
                )
            )
            stmt = _apply_common(stmt)
            stmt = stmt.order_by(bucket, mo, VaultItem.created_at.desc()).limit(limit + 1).offset(offset)
            pairs = (await s.execute(stmt)).all()
            has_more = len(pairs) > limit
            for item, m_ord in pairs[:limit]:
                out.append(_overlay(item, m_ord))
        else:
            stmt = select(VaultItem).where(
                VaultItem.account_id == account_id, VaultItem.removed_at.is_(None)
            )
            stmt = _apply_common(stmt)
            stmt = stmt.order_by(
                VaultItem.created_at.asc() if sort == "oldest" else VaultItem.created_at.desc()
            ).limit(limit + 1).offset(offset)
            rows = (await s.execute(stmt)).scalars().all()
            has_more = len(rows) > limit
            for item in rows[:limit]:
                out.append(_overlay(item))
    return {"list": out, "hasMore": has_more, "source": "mirror"}


# ── OF folders, served from the MIRROR (accurate + proxy-proof) ─────
#
# OF's live /vault/lists?view=main is unreliable: its per-folder `hasMedia`
# flag lies (reports False for folders that plainly have media, e.g. "photos
# cherry" / "categort") and it can omit freshly-touched folders entirely
# (e.g. "nowfolder"). The mirror instead derives membership from each media
# item's own listStates[], which match the OF UI exactly. This lists every
# NON-EMPTY OF folder with correct per-kind counts, instantly, zero OF round
# trip. Empty folders have no media referencing them, so the frontend unions
# this with the live list to recover those.
_BUILTIN_FOLDER_NAMES = {
    "posts", "stories", "streams", "messages", "uploads",
    "tips", "archived", "purchased",
}


@router.get("/admin/vault-ai/of-folders")
async def list_of_folders_from_mirror(account_id: str = Query(...)) -> dict[str, Any]:
    """OF folders derived from the mirror's per-item listStates — accurate
    counts (incl. folders the live vault/lists endpoint drops) with no OF call."""
    assert_account_owned(account_id)
    kind_key = {
        "photo": "photosCount", "video": "videosCount",
        "gif": "gifsCount", "audio": "audiosCount",
    }
    folders: dict[Any, dict[str, Any]] = {}
    async with get_session() as s:
        rows = (
            await s.execute(
                select(VaultItem.raw_json, VaultItem.kind).where(
                    VaultItem.account_id == account_id,
                    VaultItem.removed_at.is_(None),
                )
            )
        ).all()
    for raw, kind in rows:
        m = load_json(raw, {}) or {}
        for ls in (m.get("listStates") or []):
            if not ls.get("hasMedia"):
                continue
            fid = ls.get("id")
            if fid is None:
                continue
            f = folders.get(fid)
            if f is None:
                f = folders[fid] = {
                    "id": fid, "name": ls.get("name") or "", "type": "custom",
                    "photosCount": 0, "videosCount": 0, "gifsCount": 0,
                    "audiosCount": 0, "hasMedia": True,
                }
            if not f["name"] and ls.get("name"):
                f["name"] = ls["name"]
            k = kind_key.get(kind)
            if k:
                f[k] += 1
    out: list[dict[str, Any]] = []
    for f in folders.values():
        f["mediaCount"] = (
            f["photosCount"] + f["videosCount"] + f["gifsCount"] + f["audiosCount"]
        )
        f["builtin"] = (f["name"] or "").strip().lower() in _BUILTIN_FOLDER_NAMES
        out.append(f)
    out.sort(key=lambda x: (x["name"] or "").lower())
    return {"list": out, "source": "mirror"}


# ── Internal folders (our own, zero OF writes) ──────────────────────

@router.get("/admin/vault-ai/folders")
async def list_folders(account_id: str = Query(...)) -> dict[str, Any]:
    """Internal folders + their media counts."""
    assert_account_owned(account_id)
    async with get_session() as s:
        folders = (
            await s.execute(
                select(VaultFolder)
                .where(VaultFolder.account_id == account_id, VaultFolder.deleted_at.is_(None))
                .order_by(VaultFolder.created_at.desc())
            )
        ).scalars().all()
        counts = dict(
            (
                await s.execute(
                    select(VaultFolderItem.folder_id, func.count())
                    .where(VaultFolderItem.account_id == account_id)
                    .group_by(VaultFolderItem.folder_id)
                )
            ).all()
        )
    return {
        "account_id": account_id,
        "folders": [
            {"id": f.id, "name": f.name, "of_list_id": f.of_list_id,
             "count": int(counts.get(f.id, 0))}
            for f in folders
        ],
    }


@router.post("/admin/vault-ai/folders")
async def create_folder(payload: dict = Body(...)) -> dict[str, Any]:
    account_id = str(payload.get("account_id") or "")
    name = str(payload.get("name") or "").strip()
    assert_account_owned(account_id)
    if not name:
        raise HTTPException(status_code=400, detail={"error": "name_required"})
    async with get_session() as s:
        folder = VaultFolder(account_id=account_id, name=name[:120], created_by="operator")
        s.add(folder)
        await s.commit()
        fid = folder.id
    return {"account_id": account_id, "id": fid, "name": name}


@router.delete("/admin/vault-ai/folders/{folder_id}")
async def delete_folder(folder_id: int, account_id: str = Query(...)) -> dict[str, Any]:
    assert_account_owned(account_id)
    async with get_session() as s:
        await s.execute(
            update(VaultFolder)
            .where(VaultFolder.id == folder_id, VaultFolder.account_id == account_id)
            .values(deleted_at=datetime.utcnow())
        )
        await s.commit()
    return {"account_id": account_id, "id": folder_id, "deleted": True}


@router.post("/admin/vault-ai/folders/{folder_id}/add")
async def add_to_folder(folder_id: int, payload: dict = Body(...)) -> dict[str, Any]:
    """Add selected media ids to an internal folder (idempotent)."""
    account_id = str(payload.get("account_id") or "")
    assert_account_owned(account_id)
    media_ids = [int(x) for x in (payload.get("media_ids") or []) if str(x).lstrip("-").isdigit()]
    if not media_ids:
        raise HTTPException(status_code=400, detail={"error": "media_ids_required"})
    async with get_session() as s:
        for mid in media_ids:
            stmt = (
                sqlite_insert(VaultFolderItem)
                .values(account_id=account_id, folder_id=folder_id, media_id=mid)
                .on_conflict_do_nothing(index_elements=["account_id", "folder_id", "media_id"])
            )
            await s.execute(stmt)
        await s.commit()
    return {"account_id": account_id, "folder_id": folder_id, "added": len(media_ids)}


@router.post("/admin/vault-ai/folders/{folder_id}/remove")
async def remove_from_folder(folder_id: int, payload: dict = Body(...)) -> dict[str, Any]:
    account_id = str(payload.get("account_id") or "")
    assert_account_owned(account_id)
    media_ids = [int(x) for x in (payload.get("media_ids") or []) if str(x).lstrip("-").isdigit()]
    async with get_session() as s:
        await s.execute(
            delete(VaultFolderItem).where(
                VaultFolderItem.account_id == account_id,
                VaultFolderItem.folder_id == folder_id,
                VaultFolderItem.media_id.in_(media_ids),
            )
        )
        await s.commit()
    return {"account_id": account_id, "folder_id": folder_id, "removed": len(media_ids)}


@router.post("/admin/vault-ai/reorder")
async def reorder(payload: dict = Body(...)) -> dict[str, Any]:
    """Set manual_order for media. If `folder_id` is given, order is per-folder
    (on vault_folder_items); otherwise it's the global order (on vault_items).
    `order` = [{media_id, manual_order}]. manual_order: 0 first, 1,2,3… front,
    -1 last, -2,-3… back, null = normal."""
    account_id = str(payload.get("account_id") or "")
    assert_account_owned(account_id)
    folder_id = payload.get("folder_id")
    order = payload.get("order") or []
    n = 0
    async with get_session() as s:
        for row in order:
            try:
                mid = int(row["media_id"])
            except Exception:  # noqa: BLE001
                continue
            mo = row.get("manual_order")
            mo = int(mo) if mo is not None and str(mo).lstrip("-").isdigit() else None
            if folder_id is not None:
                # Upsert: an OF-folder item has no membership row (membership is
                # from of_folder_ids), so INSERT the order row if it's missing.
                await s.execute(
                    sqlite_insert(VaultFolderItem)
                    .values(account_id=account_id, folder_id=int(folder_id),
                            media_id=mid, manual_order=mo)
                    .on_conflict_do_update(
                        index_elements=["account_id", "folder_id", "media_id"],
                        set_={"manual_order": mo},
                    )
                )
            else:
                await s.execute(
                    update(VaultItem)
                    .where(VaultItem.account_id == account_id, VaultItem.media_id == mid)
                    .values(manual_order=mo)
                )
            n += 1
        await s.commit()
    return {"account_id": account_id, "folder_id": folder_id, "updated": n}


# ── OF folders (REAL writes to OnlyFans — confirmed wire shapes) ────
import vault_cache  # noqa: E402


import vault_dupes  # noqa: E402
import vault_ai_brief  # noqa: E402  (canonical FLAG_KEYS + flags_known)
import vault_scripts  # noqa: E402
import vault_ai_fix  # noqa: E402  (operator resolution of describe/flags disputes)

# Blast-radius guard: one HTTP request can only ever remove this many. Bigger
# selections are batched by the client (a 2.3k-item vault yields ~700 copies),
# which also gives the operator a progress readout instead of one long stall.
_MAX_HIDE_PER_CALL = 500
# Ids per OF PUT — keeps a single /vault/media/hidden body bounded, and makes a
# failure cost one chunk instead of the whole batch.
_OF_HIDE_CHUNK = 100
# Ids per SQL IN(...) — stays well clear of SQLite's bound-parameter ceiling.
_DB_IN_CHUNK = 200


@router.get("/admin/vault-ai/duplicates")
async def list_duplicates(
    account_id: str = Query(...),
    threshold: int = Query(vault_dupes.DEFAULT_THRESHOLD),
    limit: int = Query(200),
) -> dict[str, Any]:
    """Duplicate sets for the review UI. Each set is {original, dupes[]} where
    `original` is the EARLIEST-uploaded copy (kept) and `dupes` are removal
    candidates. Read-only — nothing is hidden until /duplicates/hide is called.

    Thumbs come from the existing /admin/vault-ai/thumb route, so the client
    renders both sides of every pair before the operator confirms.
    """
    assert_account_owned(account_id)
    res = await vault_dupes.find_duplicates(account_id, threshold)

    def _slim(r: dict, **extra: Any) -> dict[str, Any]:
        return {
            "media_id": r["media_id"], "kind": r["kind"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            "duration_seconds": r["duration"], "send_count": r["send_count"],
            **extra,
        }

    clusters = [
        {
            "original": _slim(c["original"]),
            "dupes": [_slim(d, dhash_dist=d["dhash_dist"],
                            ahash_dist=d["ahash_dist"], exact=d["exact"],
                            band=vault_dupes.band_of(
                                max(d["dhash_dist"], d["ahash_dist"])))
                      for d in c["dupes"]],
            "band": c["band"], "worst": c["worst"], "all_exact": c["all_exact"],
            "sent_dupes": sum(1 for d in c["dupes"] if d["send_count"] > 0),
        }
        for c in res["clusters"][:max(1, int(limit))]
    ]
    return {**{k: v for k, v in res.items() if k != "clusters"},
            "account_id": account_id,
            "returned": len(clusters), "clusters": clusters}


@router.get("/admin/vault-ai/scripts")
async def list_scripts(
    account_id: str = Query(...),
    gap_seconds: int = Query(vault_scripts.DEFAULT_GAP_SECONDS),
    videos_only: bool = Query(False),
) -> dict[str, Any]:
    """The script proposal for an account: upload batches, each one's detected
    direction, and the canonical escalating order.

    Read-only and free — no LLM, no OF traffic. Scoring runs over the V2
    describe fields already on the rows, so the operator can re-run this with a
    different `gap_seconds` as often as they like before committing. Nothing is
    persisted until /scripts/apply.
    """
    assert_account_owned(account_id)
    plan = await vault_scripts.plan_scripts(
        account_id, gap_seconds=gap_seconds, videos_only=videos_only)
    return {
        **{k: v for k, v in plan.items() if k != "scripts"},
        "scripts": [
            {
                "script_id": s["script_id"], "direction": s["direction"],
                "tau": s["tau"], "size": s["size"], "scoreable": s["scoreable"],
                "started_at": s["started_at"].isoformat() if s["started_at"] else None,
                "items": [
                    {"media_id": i["media_id"], "kind": i["kind"],
                     "script_seq": i["script_seq"], "score": i["score"],
                     "duration_seconds": i["duration_seconds"],
                     "describe_status": i["describe_status"]}
                    for i in s["items"]
                ],
            }
            for s in plan["scripts"]
        ],
    }


@router.post("/admin/vault-ai/scripts/apply")
async def apply_scripts(payload: dict = Body(...)) -> dict[str, Any]:
    """Persist the script order onto our own mirror columns so the picker and
    folder views can ORDER BY script_id, script_seq.

    Writes ONLY `script_id` / `script_seq` / `script_score` / `script_reversed`
    — the OF vault is never touched, no media is moved or hidden, and a re-plan
    simply overwrites. Re-derives the plan server-side rather than trusting a
    posted ordering, so a stale UI cannot write an order the current scores no
    longer support.
    """
    account_id = str(payload.get("account_id") or "")
    assert_account_owned(account_id)
    gap_seconds = int(payload.get("gap_seconds") or vault_scripts.DEFAULT_GAP_SECONDS)
    plan = await vault_scripts.plan_scripts(
        account_id, gap_seconds=gap_seconds,
        videos_only=bool(payload.get("videos_only")))
    written = await vault_scripts.apply_scripts(account_id, plan)
    return {"account_id": account_id, **written, "summary": plan["summary"]}


@router.get("/admin/vault-ai/folder-plan")
async def get_folder_plan(
    account_id: str = Query(...),
    keep: int = Query(2, ge=0, le=20),
    solo: bool = Query(False),
) -> dict[str, Any]:
    """PREVIEW the folders the pipeline would create. Read-only, free, instant —
    no LLM, no OF traffic, nothing written. This is what the vault button shows
    before the operator confirms.

    `solo` adds a `-solo` cut of each lane, keeping only the items nobody else
    is in."""
    assert_account_owned(account_id)
    return await vault_scripts.plan_ai_folders(account_id, keep=keep, solo=solo)


async def _read_of_list_members(client, of_list_id: int) -> set[int]:
    """Every media id OF currently reports in ONE list.

    Membership is taken from OF rather than from the plan we just sent, because
    `add_media_to_vault_list` only ADDS: a list carries the union of every
    generation of the rules that has ever run. OF is the only thing that knows
    what is actually in there, so the mirror is made to agree with OF, not with
    our intent.
    """
    live: set[int] = set()
    offset = 0
    for _ in range(_MAX_PAGES):
        resp = await asyncio.to_thread(
            client.vault_media, limit=_PAGE, offset=offset,
            type="all", list_id=int(of_list_id),
        )
        rows = (resp or {}).get("list") or []
        if not rows:
            break
        for m in rows:
            if m.get("id") is not None:
                live.add(int(m["id"]))
        if len(rows) < _PAGE:
            break
        offset += _PAGE
        await asyncio.sleep(_PAGE_SLEEP_S)
    return live


async def _apply_list_membership(
    account_id: str, live_by_list: dict[int, set[int]],
) -> int:
    """Rewrite the mirror's per-item `of_folder_ids` for EVERY list we pushed,
    in ONE pass over the vault.

    Without this, mirroring is invisible in the UI. Item→folder membership is
    written ONLY by a full collect (it is parsed from `listStates` on each media
    dict), so a folder we just pushed to OF exists there but no cached item
    claims to be in it — the picker asks "who is in list X", gets nothing, and
    honestly renders "No media in this filter" while the dropdown, which counts
    from OF, says 4. Real on OF, invisible locally.

    This ran PER FOLDER once, and each run re-loaded every VaultItem for the
    account: 12 folders on a 1600-item vault meant ~20k ORM hydrations and 12
    commits inside the request the operator is sitting in front of. The item set
    is identical every time — only the list id differs — so the loads collapse
    into one. Both directions are still written, ids gained AND ids dropped, or
    a folder that shrinks would keep its stale members in the picker forever.
    """
    if not live_by_list:
        return 0
    touched = 0
    async with get_session() as s:
        items = (await s.execute(
            select(VaultItem).where(VaultItem.account_id == account_id)
        )).scalars().all()
        for item in items:
            try:
                ids = json.loads(item.of_folder_ids or "[]")
                ids = [int(x) for x in ids if x is not None]
            except (ValueError, TypeError):
                ids = []
            before = list(ids)
            for lid, live in live_by_list.items():
                has, should = lid in ids, int(item.media_id) in live
                if has == should:
                    continue
                if should:
                    ids.append(lid)
                else:
                    ids = [x for x in ids if x != lid]
            if ids != before:
                item.of_folder_ids = json.dumps(ids)
                touched += 1
        await s.commit()
    return touched


async def _mirror_ai_folders_to_of(
    account_id: str, created: list[dict[str, Any]], plan: dict[str, Any],
) -> list[dict[str, Any]]:
    """Push the just-built internal folders out as REAL OF vault lists.

    This is the only part of the pipeline that writes to her OnlyFans account,
    which is why it is opt-in per request rather than implied by "apply".

    Binds `VaultFolder.of_list_id` on success so a re-run ADDS to the same OF
    list instead of creating a second one with the same name. A folder that
    already carries an `of_list_id` is reused. Failures are reported per folder
    and never roll back the internal folder — a folder that exists locally but
    not on OF is recoverable; losing the grouping is not.

    Runs in two phases, and the ORDER is the point. Phase 1 is every write that
    changes her OnlyFans account; phase 2 only re-READS what we just wrote so
    the local picker agrees with it. They used to be interleaved per folder,
    which made the cheap cosmetic read-back a barrier in front of the next
    folder's create: on a 12-folder plan the request ran for minutes, the proxy
    in front of the relay gave up, and the browser showed a bare "Internal
    Server Error" while the relay had logged nothing at all (a client
    disconnect cancels the handler with `CancelledError`, which is a
    BaseException and so is invisible to the `except Exception` that records
    server errors). The folders that hadn't been reached yet simply never got
    made — the 2026-07-28 symptom of two `AI-` folders sitting with a NULL
    `of_list_id`, and of "sometimes it works": a small plan finished inside the
    timeout, a big one didn't.

    Split, a request cut short still leaves every folder REAL on OF with its
    media in it. Only the read-back is lost, and a re-run (or any collect)
    restores that.
    """
    by_name = {f["name"]: f for f in plan.get("folders") or []}
    client = await asyncio.to_thread(ax._make_client, account_id)
    out: list[dict[str, Any]] = []

    # ── Phase 1: the writes ──────────────────────────────────────────
    for made in created:
        spec = by_name.get(made["name"])
        media_ids = [int(i["media_id"]) for i in (spec or {}).get("items") or []]
        async with get_session() as s:
            folder = await s.get(VaultFolder, made["folder_id"])
            of_list_id = folder.of_list_id if folder else None

        try:
            if not of_list_id:
                res = await asyncio.to_thread(client.create_vault_list, made["name"][:120])
                of_list_id = res.get("id")
                if not of_list_id:
                    raise RuntimeError(f"OF returned no list id: {str(res)[:120]}")
                async with get_session() as s:
                    await s.execute(
                        update(VaultFolder)
                        .where(VaultFolder.id == made["folder_id"])
                        .values(of_list_id=int(of_list_id))
                    )
                    await s.commit()
            if media_ids:
                await asyncio.to_thread(client.add_media_to_vault_list, int(of_list_id), media_ids)
            out.append({**made, "of_list_id": int(of_list_id),
                        "of_added": len(media_ids)})
        except Exception as e:  # noqa: BLE001
            log.warning("OF mirror failed folder=%s name=%s",
                        made["folder_id"], made["name"], exc_info=True)
            out.append({**made, "of_error": str(e)[:200]})

    # ── Phase 2: the read-back, off the request ──────────────────────
    # Detached for the same reason Collect and the flags sweep are: it is one
    # OF page per 100 items across every list, and holding the operator's
    # request open for it is what made a 12-folder plan 500. Her account is
    # already correct when this starts, so nothing here can fail in a way that
    # costs a folder.
    list_ids = [int(r["of_list_id"]) for r in out if r.get("of_list_id")]
    if list_ids:
        asyncio.create_task(_readback_membership(client, account_id, list_ids))
    return out


async def _readback_membership(client, account_id: str, list_ids: list[int]) -> int:
    """Make the mirror agree with OF about who is in the lists we just pushed.

    Runs detached, so it owns its own error handling — an exception escaping a
    bare `create_task` is only ever seen as an "exception was never retrieved"
    warning at GC time.
    """
    live_by_list: dict[int, set[int]] = {}
    for lid in list_ids:
        try:
            live_by_list[lid] = await _read_of_list_members(client, lid)
        except Exception:  # noqa: BLE001
            log.warning("OF membership read-back failed list=%s", lid, exc_info=True)
    try:
        synced = await _apply_list_membership(account_id, live_by_list)
        log.info("mirror membership synced account=%s items=%s lists=%s",
                 account_id, synced, len(live_by_list))
        return synced
    except Exception:  # noqa: BLE001
        log.warning("mirror membership write failed account=%s", account_id,
                    exc_info=True)
        return 0


@router.post("/admin/vault-ai/folder-plan/apply")
async def apply_folder_plan(payload: dict = Body(...)) -> dict[str, Any]:
    """Create the previewed folders and fill them, in send order.

    Requires `confirm: true` — there is no path that creates folders from a
    single click. The plan is RE-DERIVED here rather than taken from the body,
    so a preview the operator left open for ten minutes cannot write a stale
    grouping; the worst case is that they get the current answer.

    Folders are internal and prefixed `AI-`; re-running refreshes the same ones
    instead of duplicating them, and a folder the operator made by hand is never
    touched.

    `mirror_to_of` additionally creates them as REAL OF vault lists. That is the
    ONLY write to her OnlyFans account in this pipeline, so it is opt-in per
    request and never implied by `confirm` alone. Nothing is ever SENT either
    way.

    `solo` must match what the preview was showing. It is part of what the plan
    IS, and `apply_ai_folders` retires generated folders the current plan no
    longer makes — so applying without it after previewing with it would create
    the lanes and retire every `-solo` folder in the same call.
    """
    account_id = str(payload.get("account_id") or "")
    assert_account_owned(account_id)
    if not payload.get("confirm"):
        raise HTTPException(status_code=400, detail={"error": "confirm_required"})
    keep = int(payload.get("keep") or 2)
    solo = bool(payload.get("solo"))
    plan = await vault_scripts.plan_ai_folders(account_id, keep=keep, solo=solo)
    result = await vault_scripts.apply_ai_folders(account_id, plan)

    if payload.get("mirror_to_of"):
        result["created"] = await _mirror_ai_folders_to_of(
            account_id, result["created"], plan)
        result["of_mirrored"] = sum(1 for c in result["created"] if c.get("of_list_id"))
        result["of_failed"] = sum(1 for c in result["created"] if c.get("of_error"))

    try:
        await vault_cache.invalidate(account_id)
    except Exception:  # noqa: BLE001
        pass
    return {"account_id": account_id, **result, "summary": plan["summary"]}


# Frames sampled from a CLIP for the flags pass.
#
# A clip changes state along its length — she can be dressed at 0:05 and nude at
# 1:30 — so a single frame answers the wrong question half the time. Four frames
# spread across the clip, each asked independently, then folded in code.
_FLAGS_VIDEO_FRAMES = 4


# What the model writes in an `over_` field when it means "nothing there". It
# is told to use null; it also says these.
_NOTHING_OVER = frozenset({"", "null", "none", "nothing", "n/a", "na", "nil",
                           "not_in_frame", "not in frame", "nude", "naked",
                           "no clothing", "no garment", "uncovered", "exposed"})

# The model is told to answer null and mostly does, but it also narrates the
# absence — "nothing, bare skin", "no clothing - she is bare". Any answer whose
# words are only about there being nothing there is nothing there. Matched on
# words rather than on the whole string so the phrasings do not have to be
# enumerated: a real garment always contributes a word that is not in this set.
_ABSENCE_WORDS = frozenset({"bare", "skin", "nothing", "no", "not", "none",
                            "null", "n/a", "visible", "is", "she", "her",
                            "exposed", "uncovered", "nude", "naked", "at",
                            "all", "and", "the", "a", "-", "—", ","})


def _garment_over(answer: dict[str, Any], key: str) -> str:
    """The named garment/object over a region, or "" for nothing.

    Errs toward "nothing": a false garment silently marks an exposed region
    covered, which is the direction that puts explicit material in a mass send.
    A false absence only costs the override, and the state field still stands.
    """
    v = answer.get(key)
    if not isinstance(v, str):
        return ""
    s = v.strip()
    if s.lower().strip(".") in _NOTHING_OVER:
        return ""
    words = [w for w in re.split(r"[\s,./-]+", s.lower()) if w]
    return "" if words and all(w in _ABSENCE_WORDS for w in words) else s


def _ground_in_named_garment(answer: dict[str, Any]) -> dict[str, Any]:
    """Let what the model NAMED overrule what it CLASSIFIED.

    Asking for a state directly ("is her chest bare?") measured badly in both
    polarities: `covered` pulled toward false on anything racy, and `bare` came
    back on a white tank top and a black halter top, because a one-word verdict
    invites the model to answer the vibe of the picture. Naming the garment is
    a much easier question and the describe pass already answers it correctly —
    the same model wrote "wearing a white tank top" in prose about the same
    photo it then called bare.

    So the naming is made LOAD-BEARING rather than advisory: name something over
    a region and the region is covered, whatever the state field said. Only
    `bare` is overridden — `not_in_frame` is left alone, because a garment can
    be named on a body part that is genuinely out of shot.
    """
    out = dict(answer)
    for region, over in vault_ai_brief.OVER_KEYS.items():
        if out.get(region) == "bare" and _garment_over(out, over):
            out[region] = "covered"
        elif out.get(region) == "not_in_frame":
            # Nothing can be over a part of her that is not in the picture.
            # The model still names the one garment it can see for EVERY
            # region — a woman face down in a bodystocking came back with
            # "black lace bodysuit" over her anus. Left in place that name
            # reads downstream as evidence she is dressed there.
            out.pop(over, None)
    return out


# Acts that cannot happen through fabric. Insertion only — `rubbing_clit` and
# `groping_own_breasts` are both perfectly possible over underwear, so they are
# deliberately absent: this list has to be airtight or it manufactures exposure
# rather than detecting it.
_INSERTION_ACTS = frozenset({
    "fingering", "toy_insertion", "riding_toy",
    "sex_missionary", "sex_doggy", "sex_riding",
})
_REAL_PENETRATION = frozenset({"fingers", "toy", "penis"})


def _reconcile_with_acts(fields: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
    """Let what is HAPPENING overrule what the flags pass thought it saw.

    The flags pass looks at one frame and names the garment nearest the action,
    then calls the region covered. On this vault that produced, verbatim: "A
    hand is inserting two fingers into the vagina" with `vulva_vis: covered,
    over_vulva: 'pink lace thong'`. Fingers do not go through a thong. Four of
    the operator's fourteen round-2 corrections were this exact shape, and all
    four were on the most explicit — which is to say most valuable — items in
    the vault.

    Deliberately narrow. Only INSERTION counts, because insertion is physically
    incompatible with a covered region; rubbing and groping happen over
    underwear all the time and would turn this into a machine for inventing
    exposure. A rule that gates money and mass sends has to be right in the
    direction it fires, not merely often right.
    """
    acts = {str(a).strip().lower() for a in (fields.get("acts") or [])}
    pen = str(fields.get("penetration") or "").strip().lower()
    if not (acts & _INSERTION_ACTS or pen in _REAL_PENETRATION):
        return data
    focus = {str(x).strip().lower() for x in (fields.get("body_focus") or [])}
    out = dict(data)
    # Anal only when the row actually says so; otherwise insertion means vulva.
    region = "anus_vis" if (focus & {"anus"}) and "pussy" not in focus else "vulva_vis"
    if out.get(region) == "covered":
        out[region] = "bare"
        out.pop(vault_ai_brief.OVER_KEYS[region], None)
    return out


def _fold_clip_flags(per_frame: list[dict[str, Any]]) -> dict[str, Any]:
    """Fold per-frame answers into one verdict for the whole clip.

    Regions fold by MAX exposure (`vault_ai_brief.fold_vis`) and
    `underwear_visible` by `any()` — both conservative, which is the right
    default for flags that gate what may be shown publicly and what is priced
    as a full reveal.

    The v1 fold was `all()` over "covered" booleans, and it collapsed: a frame
    that could not see the region answered `covered=False`, indistinguishable
    from a frame where she was bare, so ANY frame that simply missed the region
    marked the whole clip uncovered. 20 of 21 the graded vault clips folded to
    uncovered, which is no signal at all. With `not_in_frame` as its own state
    those frames fold away instead of voting for exposure.

    Deliberately folded HERE rather than asked of the model. The reason this
    pass exists at all is that the model got `fully_nude` wrong; making it also
    reason across frames would add a second thing it can get wrong, on top of a
    question it has already been measured good at one image at a time.
    """
    out: dict[str, Any] = {}
    seen = [bool(f["underwear_visible"]) for f in per_frame
            if isinstance(f.get("underwear_visible"), bool)]
    if seen:
        out["underwear_visible"] = any(seen)
    for region in vault_ai_brief.VIS_REGIONS:
        folded = vault_ai_brief.fold_vis([str(f.get(region) or "") for f in per_frame])
        if folded:
            out[region] = folded
    return out


def _clip_arc(per_frame: list[dict[str, Any]]) -> str | None:
    """Which way the clip escalates, from the frames already paid for.

    Compares how much is on show in the first frame against the last. A clip
    that opens covered and ends bare is a build-up; the reverse is a payoff
    that was uploaded (or shot) back to front. `vault_scripts` currently infers
    this direction with a rank correlation across SEPARATE items, which is a
    far weaker signal than watching one clip change — so this is recorded now
    even though nothing reads it yet.
    """
    if len(per_frame) < 2:
        return None
    def heat(f: dict[str, Any]) -> int:
        ranks = [vault_ai_brief.VIS_STATES.index(f[r]) for r in vault_ai_brief.VIS_REGIONS
                 if f.get(r) in vault_ai_brief.VIS_STATES]
        return max(ranks) if ranks else -1
    first, last = heat(per_frame[0]), heat(per_frame[-1])
    if first < 0 or last < 0 or first == last:
        return "flat"
    return "escalates" if last > first else "reverses"


async def _stamp_flags_failure(account_id: str, media_id: int, status: str,
                               detail: str = "") -> None:
    """Record WHY an item has no flags, in the item's own row.

    Flags have no status column and do not need one: `_flags_todo` decides from
    `flags_known()`, so a failed item is already retried by the next sweep. What
    was missing is any way to ASK. The pass returned `no_image` to a caller that
    logged it and moved on, so an operator looking at a described-but-unflagged
    row had nothing to read — the failure existed only in a log line that had
    long since rotated.

    Deliberately does NOT write `_flags_v`: that stamp is what marks flags
    CURRENT, and a failure must leave the item in the todo set."""
    async with get_session() as s:
        item = await s.get(VaultItem, (account_id, media_id))
        if item is None:
            return
        fields = load_json(item.ai_fields_json, {}) or {}
        fields["_flags_status"] = status
        fields["_flags_failed_at"] = datetime.utcnow().isoformat()
        if detail:
            fields["_flags_error"] = detail[:200]
        else:
            fields.pop("_flags_error", None)
        item.ai_fields_json = json.dumps(fields, ensure_ascii=False, default=str)
        await s.commit()


# Every describe/flags call goes to this provider, and the daily cap is held
# per-(account, provider) — so this one row IS the budget a vision sweep spends.
_VISION_PROVIDER = "deepinfra"


async def _vision_budget(account_id: str) -> dict[str, Any]:
    """Today's vision budget, plus the ONE sentence that explains a refusal.

    The sentence lives here and only here. It is product copy — it names a
    screen — so it does not belong in `llm_client`, which counts money; and it
    must not be re-composed in the browser, because then one sentence has two
    authors in two languages and they drift. Everything that has to tell an
    operator why vision work is unavailable — both sweeps, the single-item
    button, and the plan the buttons are drawn from — reads `blocked_reason`,
    which is empty exactly when nothing is blocking.
    """
    state = await llm_client.cap_state(account_id, _VISION_PROVIDER)
    reason = ""
    if state["capped"]:
        reason = (f"Today's AI budget for this account is spent — "
                  f"${state['spent_millicents'] / 10000:.2f} of "
                  f"${state['cap_millicents'] / 10000:.2f}. It resets at "
                  f"midnight UTC — or raise 'Daily cap' on the account's "
                  f"Brain (Automations → Brain).")
    return {**state, "blocked_reason": reason}


async def _flags_one(account_id: str, media_id: int,
                     model: str = "qwen3-vl-30b") -> dict[str, Any]:
    """Ask the three booleans for one item and MERGE them into its existing
    `ai_fields_json`. Never rewrites the description, tags or taxonomy — this is
    an enrichment pass, so a cheap call can't damage an expensive one.

    Photos ask once. Clips ask `_FLAGS_VIDEO_FRAMES` times, one frame per call,
    and fold with `_fold_clip_flags`.
    """
    async with get_session() as s:
        item = await s.get(VaultItem, (account_id, media_id))
    if item is None:
        return {"media_id": media_id, "ok": False, "status": "not_in_mirror"}
    if item.kind not in ("photo", "video"):
        return {"media_id": media_id, "ok": False, "status": "not_flaggable"}

    is_video = item.kind == "video"
    raw = load_json(item.raw_json, {}) or {}
    want = _FLAGS_VIDEO_FRAMES if is_video else 1
    got = await vault_frames.collect(account_id, item, raw, want=want)
    if not got.images:
        status = _NO_IMAGE_STATUS.get(got.reason, "fetch_failed")
        await _stamp_flags_failure(account_id, media_id, status)
        return {"media_id": media_id, "ok": False, "status": status}

    per_frame: list[dict[str, Any]] = []
    cost = 0
    result = None
    for img in got.images:
        content = [{"type": "text", "text": _FLAGS_PROMPT},
                   {"type": "image_url", "image_url": {"url": vision.shrink_data_url(img)}}]
        try:
            result = await llm_client.chat(
                model=model, messages=[{"role": "user", "content": content}],
                purpose="describe_flags", account_id=account_id, temperature=0.1,
            )
        except LLMCapExceeded as e:
            if not per_frame:
                # Same sentence describe's refusals carry — both sweeps spend
                # the same budget, and an operator reading two different
                # explanations of one cap has to work out they are one thing.
                budget = await _vision_budget(account_id)
                return {"media_id": media_id, "ok": False, "status": "capped",
                        "detail": budget["blocked_reason"] or str(e)}
            break  # keep what we already read rather than losing the whole clip
        except LLMError as e:
            if not per_frame:
                await _stamp_flags_failure(account_id, media_id, "error", str(e))
                return {"media_id": media_id, "ok": False, "status": "error",
                        "detail": str(e)[:300]}
            break
        cost += int(result.cost_cents or 0)
        parsed = _parse_describe(result.content)
        if any(k in parsed for k in _FLAG_KEYS):
            per_frame.append(_ground_in_named_garment(parsed))

    if not per_frame:
        raw_head = (result.content if result else "") or ""
        await _stamp_flags_failure(account_id, media_id, "unparsed", raw_head)
        return {"media_id": media_id, "ok": False, "status": "unparsed",
                "raw": raw_head[:200]}

    fields = load_json(item.ai_fields_json, {}) or {}
    data = _reconcile_with_acts(fields, _fold_clip_flags(per_frame))
    # An operator correction outranks the model, and must survive a forced
    # re-run — otherwise the next `flags-all --force` silently reverts it and
    # the operator has no reason to look at the item again. Same override+lock
    # contract `vault_ai_effective` defines for `description`; `vault_ai_fix`
    # writes the lock.
    locked = set(load_json(item.locked_fields_json, []) or [])
    for key in _FLAG_KEYS:
        if key in data and key not in locked:
            fields[key] = data[key]
    # Persisted, not merely used and discarded. Two jobs: when a region says
    # `covered` this is WHY in the model's own words (a wrong flag is far
    # cheaper to diagnose from "white tank top" than from "covered"), and
    # `vault_scripts._is_nude` reads it as the direct evidence that she is
    # dressed. Taken from the FIRST frame, which is where a clip starts out.
    for over in vault_ai_brief.OVER_KEYS.values():
        if over in locked:
            continue
        named = _garment_over(per_frame[0], over)
        if named:
            fields[over] = named[:60]
        else:
            fields.pop(over, None)
    if is_video:
        fields["_flags_frames"] = len(per_frame)
        arc = _clip_arc(per_frame)
        if arc:
            fields["_flags_arc"] = arc
    # Superseded shapes, dropped rather than left to rot: `genitals_covered`
    # conflated vulva and breasts, and the `*_covered` pair asked two opposite
    # questions under one name. Leaving either behind would let a caller that
    # was never updated keep reading a stale answer that looks current.
    for dead in ("genitals_covered", "pussy_covered", "breasts_covered"):
        fields.pop(dead, None)
    fields["_flags_model"] = model
    fields["_flags_v"] = _FLAGS_VERSION
    # A recovered item must stop reporting the failure it recovered from.
    for stale in ("_flags_status", "_flags_failed_at", "_flags_error"):
        fields.pop(stale, None)

    async with get_session() as s:
        await s.execute(
            update(VaultItem)
            .where(VaultItem.account_id == account_id, VaultItem.media_id == media_id)
            .values(ai_fields_json=json.dumps(fields, ensure_ascii=False, default=str))
        )
        await s.commit()
    return {"media_id": media_id, "ok": True, "status": "flagged",
            **{k: fields.get(k) for k in _FLAG_KEYS},
            "frames": len(per_frame), "cost_millicents": cost}


# ── Flags sweep (background, same shape as describe-all) ────────────
# A whole-vault sweep CANNOT run inside its own request. 327 items × ~3.4s is
# ~18 minutes; the app's proxy drops the socket long before that and the browser
# is shown "Internal Server Error" — while the sweep carries on server-side,
# invisible, so every retry click starts ANOTHER concurrent sweep over the same
# rows. `describe-all` has always been a background task with a status endpoint;
# this was the odd one out.
_flags_running: set[str] = set()
_flags_progress: dict[str, dict[str, Any]] = {}
# Same bound as describe. The pass is IO-bound on one small VLM call per item,
# so 4 at a time turns ~18 minutes into ~5 without changing what is asked.
_FLAGS_CONCURRENCY = 4

# What a vision model can be shown. An audio file has no frame, so sweeping it is
# not thoroughness — it is a row of guaranteed failure per pass (591 of them on
# two accounts), and the 45 whose signatures were still valid had their MP3 bytes
# posted to a vision endpoint as an image, which is what `describe_status =
# 'failed'` recorded. The flags sweep has always filtered this way; the describe
# sweep and its cost estimate never did, so the estimate quoted a total the sweep
# could not meet.
_DESCRIBABLE_KINDS = ("photo", "video")


async def _flags_todo(account_id: str, *, force: bool = False,
                      only: set[int] | None = None, limit: int = 0) -> list[int]:
    """Which items still need flags, oldest first."""
    async with get_session() as s:
        q = (select(VaultItem.media_id, VaultItem.ai_fields_json)
             .where(VaultItem.account_id == account_id,
                    VaultItem.removed_at.is_(None),
                    VaultItem.kind.in_(_DESCRIBABLE_KINDS)))
        if only:
            q = q.where(VaultItem.media_id.in_(only))
        rows = (await s.execute(q.order_by(VaultItem.created_at.asc()))).all()

    todo = []
    for mid, fj in rows:
        f = load_json(fj, {}) or {}
        # `flags_known`, not a key-presence test: a row carrying the superseded
        # boolean shape, or a region the model answered with something outside
        # the enum, has not really been flagged and must re-run. A row produced
        # by an OLDER prompt is stale for the same reason — its answers were
        # given to a different question.
        stale = int(f.get("_flags_v") or 0) < _FLAGS_VERSION
        if only or force or stale or not vault_ai_brief.flags_known(f):
            todo.append(int(mid))
    return todo[:limit] if limit else todo


async def _run_flags_all(account_id: str, todo: list[int]) -> None:
    """Background flags sweep. Resumable by construction: it only ever visits
    rows that are missing or stale, so a relay restart mid-sweep costs the items
    in flight and nothing else — pressing the button again picks up the rest."""
    try:
        total = len(todo)
        done = failed = cost = 0
        capped = False
        first_error = ""
        _flags_progress[account_id] = {"total": total, "done": 0, "failed": 0,
                                       "capped": False, "cost_millicents": 0}
        sem = asyncio.Semaphore(_FLAGS_CONCURRENCY)

        async def _one(mid: int) -> None:
            nonlocal done, failed, cost, capped, first_error
            async with sem, _vision_gate():  # per-sweep bound + process-wide gate
                if capped:
                    return
                res = await _flags_one(account_id, mid)
                if res.get("ok"):
                    done += 1
                    cost += int(res.get("cost_millicents") or 0)
                else:
                    failed += 1
                    if res.get("status") == "capped":
                        capped = True
                    if not first_error and res.get("detail"):
                        first_error = str(res["detail"])[:200]
                _flags_progress[account_id] = {
                    "total": total, "done": done, "failed": failed,
                    "capped": capped, "cost_millicents": cost,
                    "error": first_error,
                }

        for i in range(0, len(todo), 50):
            if capped:
                break
            await asyncio.gather(*(_one(m) for m in todo[i:i + 50]))

        if failed:
            log.warning("flags_all account=%s flagged=%s/%s FAILED=%s capped=%s first_error=%s",
                        account_id, done, total, failed, capped, first_error)
        else:
            log.info("flags_all done account=%s flagged=%s/%s cost_mc=%s",
                     account_id, done, total, cost)
    except Exception:  # noqa: BLE001
        log.exception("flags_all failed account=%s", account_id)
    finally:
        _flags_running.discard(account_id)


@router.post("/admin/vault-ai/flags-all")
async def flags_all(payload: dict = Body(...)) -> dict[str, Any]:
    """Run the cheap flags pass over an account's photos AND clips.

    Enrichment only: merges the three booleans into each row's existing
    `ai_fields_json` and touches nothing else. Skips items that already carry
    all of them unless `force`.

    Clips are included because the flags gate `AI-safe explicit`, the one folder
    that claims something is safe to mass-send. While clips went unflagged they
    fell into it by default — on the graded vault that meant a penetration clip and a
    dildo clip in the mass-safe folder.

    A whole-vault sweep returns `{"status": "running"}` immediately and reports
    through `/flags-all/status`. A NAMED `media_ids` set stays inline: it is a
    handful of items, and the caller (the grading probe) wants the answers back
    in the response.
    """
    account_id = str(payload.get("account_id") or "")
    assert_account_owned(account_id)
    force = bool(payload.get("force"))
    limit = int(payload.get("limit") or 0)
    # Re-check a NAMED set instead of the whole vault. A prompt change is
    # cheap to evaluate on the couple of dozen items that have ever been wrong
    # and expensive to evaluate on all of them — and the ones that have been
    # wrong before are where a regression shows up first. Implies `force`,
    # since the caller is asking for these specifically.
    only = payload.get("media_ids")
    only = {int(m) for m in only} if isinstance(only, (list, tuple, set)) else None

    todo = await _flags_todo(account_id, force=force, only=only, limit=limit)

    if only:
        done, failed, cost = 0, 0, 0
        for mid in todo:
            res = await _flags_one(account_id, mid)
            if res.get("ok"):
                done += 1
                cost += int(res.get("cost_millicents") or 0)
            else:
                failed += 1
                if res.get("status") == "capped":
                    break
        return {"account_id": account_id, "candidates": len(todo),
                "flagged": done, "failed": failed, "cost_millicents": cost,
                "prompt_version": _FLAGS_VERSION,
                # A sweep that stopped early leaves the vault at MIXED versions,
                # which is the state that has repeatedly been mistaken for a
                # finished one. Say so in the result rather than leaving a caller
                # to infer it from `failed`.
                "complete": failed == 0 and done == len(todo)}

    # One sweep per account. Without this, a click on a button that appears to
    # have failed re-runs the whole vault alongside the run already going.
    if account_id in _flags_running:
        return {"account_id": account_id, "status": "running",
                "candidates": len(todo), "already": True,
                "prompt_version": _FLAGS_VERSION}
    _flags_running.add(account_id)
    asyncio.create_task(_run_flags_all(account_id, todo))
    return {"account_id": account_id, "status": "running",
            "candidates": len(todo), "already": False,
            "prompt_version": _FLAGS_VERSION}


@router.get("/admin/vault-ai/flags-all/status")
async def flags_all_status(account_id: str = Query(...)) -> dict[str, Any]:
    """Progress of the background sweep, plus coverage read from the DB.

    Two numbers on purpose. `progress` is this process's live counter and dies
    with a restart; `coverage` is the durable truth, so a UI that reconnects
    after a redeploy still shows where the vault actually stands instead of
    starting over at nothing.
    """
    assert_account_owned(account_id)
    return {
        "account_id": account_id,
        "running": account_id in _flags_running,
        "progress": _flags_progress.get(account_id),
        "coverage": await vault_scripts.flags_coverage(account_id),
    }


@router.get("/admin/vault-ai/disputes")
async def list_disputes(account_id: str = "") -> dict[str, Any]:
    """Items where the describe pass and the flags pass contradict each other.

    Read-only. Each row carries BOTH readings and the two field-writes that
    would settle it, because which pass is wrong differs item by item and
    picking automatically is the mistake this whole review exists to avoid.
    """
    assert_account_owned(account_id)
    return await vault_ai_fix.list_disputes(account_id)


@router.post("/admin/vault-ai/disputes/resolve")
async def resolve_dispute(payload: dict = Body(...)) -> dict[str, Any]:
    """Apply ONE operator correction, locked against future re-runs.

    Body: `{account_id, media_id, values:{field: value}}` — normally one of the
    two branches `list_disputes` proposed, optionally hand-edited. Writes to
    `ai_fields_json` (so every existing reader sees it), to
    `operator_overrides_json`, and adds the field to `locked_fields_json`.
    """
    account_id = str(payload.get("account_id") or "")
    assert_account_owned(account_id)
    try:
        media_id = int(payload.get("media_id"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail={"error": "media_id_required"})
    res = await vault_ai_fix.apply_fix(account_id, media_id,
                                       payload.get("values") or {})
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res)
    return res


@router.get("/admin/vault-ai/flags-review")
async def flags_review_queue(account_id: str = "", limit: int = 120,
                             only_iffy: bool = False) -> dict[str, Any]:
    """Items for the operator to check, most valuable first.

    The flags pass has been wrong in a different direction after every prompt
    change, and each reversal was invisible to every automatic check and
    obvious to a human in seconds. So looking is part of the product.

    Returns EVERYTHING by default, suspects first. `only_iffy` narrows to the
    nominated ones, and is off by default because the nomination was measured
    and is not good enough to hide things behind: on the graded vault it flags 40 items
    to catch 11 of the 24 that are actually wrong — 28% precision, 46% recall.
    Filtering on that would quietly hide thirteen real errors, which is worse
    than a longer list. The reasons are a SORT ORDER and a set of badges; they
    are not a substitute for looking.
    """
    assert_account_owned(account_id)
    return await vault_ai_fix.review_queue(account_id, limit=limit,
                                           version=_FLAGS_VERSION,
                                           only_iffy=only_iffy)


@router.post("/admin/vault-ai/flags-review/grade")
async def flags_review_grade(payload: dict = Body(...)) -> dict[str, Any]:
    """Record one verdict: corrections locked, everything else CONFIRMED.

    Body: `{account_id, media_id, corrections?:{region:state}, note?}`.
    Confirming is not a no-op — it is the evidence `flags-accuracy` runs on,
    and without it "the operator agreed" and "nobody looked" are the same
    record.
    """
    account_id = str(payload.get("account_id") or "")
    assert_account_owned(account_id)
    try:
        media_id = int(payload.get("media_id"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail={"error": "media_id_required"})
    res = await vault_ai_fix.record_grade(
        account_id, media_id,
        corrections=payload.get("corrections") or {},
        version=_FLAGS_VERSION, note=str(payload.get("note") or ""))
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res)
    return res


@router.get("/admin/vault-ai/flags-accuracy")
async def flags_accuracy(account_id: str = "") -> dict[str, Any]:
    """How often the model agrees with the operator, on THIS vault.

    Counted only over items graded at the CURRENT prompt version. Mixing
    versions measures nothing, and counting ungraded items as correct is how a
    100% gets reported on a vault nobody has checked.
    """
    assert_account_owned(account_id)
    return await vault_ai_fix.flags_accuracy(account_id, version=_FLAGS_VERSION)


@router.post("/admin/vault-ai/scripts/collect")
async def collect_scripts(payload: dict = Body(...)) -> dict[str, Any]:
    """Every upload burst big enough to be a shoot, as a PROPOSED folder.

    Body: `{account_id, gap_seconds?, min_items?, queue?}`. Read-only by
    default. With `queue: true` each proposal is written to the review queue as
    a **pending** row — which still creates no folder. A folder only exists
    after the operator approves the row (/review/approve, which re-checks the
    baseline) and /scripts/folders/apply runs.
    """
    account_id = str(payload.get("account_id") or "")
    assert_account_owned(account_id)
    proposals = await vault_scripts.collect_scripts(
        account_id,
        gap_seconds=int(payload.get("gap_seconds") or vault_scripts.DEFAULT_GAP_SECONDS),
        min_items=int(payload.get("min_items") or vault_scripts.MIN_SCRIPT_ITEMS),
    )
    queued: dict[str, Any] = {}
    if payload.get("queue"):
        queued = await vault_scripts.queue_script_folders(account_id, proposals)
    return {
        "account_id": account_id, "count": len(proposals), **queued,
        "scripts": [
            {"script_id": p["script_id"], "name": p["name"],
             "direction": p["direction"], "reason": p["reason"], "tau": p["tau"],
             "size": p["size"], "kinds": p["kinds"],
             "closes_on_own": p["closes_on_own"],
             "started_at": p["started_at"].isoformat() if p["started_at"] else None,
             "items": [{"media_id": r["media_id"], "kind": r["kind"],
                        "manual_order": r["manual_order"], "score": r["score"],
                        "tier": r["why"]["tier"], "closes": r["why"]["closes"]}
                       for r in p["items"]]}
            for p in proposals
        ],
    }


@router.post("/admin/vault-ai/scripts/folders/apply")
async def apply_script_folders(payload: dict = Body(...)) -> dict[str, Any]:
    """Create folders for script proposals the operator has APPROVED.

    Only acts on review rows already flipped to `approved` — this endpoint can
    never approve anything itself, so a mis-click here cannot create a folder
    that was not confirmed. Internal folders only; no OF writes.
    """
    account_id = str(payload.get("account_id") or "")
    assert_account_owned(account_id)
    return {"account_id": account_id,
            **(await vault_scripts.apply_approved_script_folders(account_id))}


@router.post("/admin/vault-ai/scripts/order-folder")
async def order_folder_by_script(payload: dict = Body(...)) -> dict[str, Any]:
    """Order ONE internal folder into send order — tease first, payoff last.

    Body: `{account_id, folder_id, apply?}`. Writes `manual_order` 1..n on that
    folder's membership rows using the schema's existing convention, so the same
    media keeps an independent position in every other folder it belongs to.
    `apply` defaults to false: the caller gets the proposed order to render
    before anything is persisted.

    Requires the script columns to be populated — run /scripts/apply first.
    No OF writes; nothing is moved, hidden, or sent.
    """
    account_id = str(payload.get("account_id") or "")
    assert_account_owned(account_id)
    try:
        folder_id = int(payload.get("folder_id"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail={"error": "folder_id_required"})

    order = await vault_scripts.plan_folder_order(account_id, folder_id)
    applied = 0
    if payload.get("apply"):
        applied = (await vault_scripts.apply_folder_order(
            account_id, folder_id, order))["items"]
    return {
        "account_id": account_id, "folder_id": folder_id,
        "applied": applied, "items": len(order),
        "unscored": sum(1 for r in order if r["score"] is None),
        "order": [{"media_id": r["media_id"], "manual_order": r["manual_order"],
                   "score": r["score"], "kind": r["kind"],
                   "script_id": r["script_id"], "script_seq": r["script_seq"]}
                  for r in order],
    }


@router.post("/admin/vault-ai/duplicates/hide")
async def hide_duplicates(payload: dict = Body(...)) -> dict[str, Any]:
    """Remove confirmed duplicate copies from the REAL OF vault.

    This calls OF's own "Remove selected items from vault" path
    (`PUT /vault/media/hidden`) — the media is hidden, not destroyed, so
    anything already attached to a sent PPV or a live post keeps working. OF
    exposes no unhide, so the UI must confirm before calling this.

    Guards, all enforced server-side because the client's set can be stale:
      · every id is re-clustered NOW and must still be a DUPE — an id that is
        the ORIGINAL of its set is refused, so the keeper can never be hidden;
      · ids not in any duplicate set at the current threshold are refused;
      · copies with send_count > 0 are skipped unless `allow_sent` is set;
      · at most `_MAX_HIDE_PER_CALL` ids per request.
    """
    account_id = str(payload.get("account_id") or "")
    assert_account_owned(account_id)
    media_ids = [int(x) for x in (payload.get("media_ids") or [])
                 if str(x).lstrip("-").isdigit()]
    if not media_ids:
        raise HTTPException(status_code=400,
                            detail={"error": "media_ids_required"})
    if len(media_ids) > _MAX_HIDE_PER_CALL:
        raise HTTPException(status_code=400, detail={
            "error": "too_many", "max": _MAX_HIDE_PER_CALL,
            "got": len(media_ids)})
    allow_sent = bool(payload.get("allow_sent"))
    threshold = int(payload.get("threshold") or vault_dupes.DEFAULT_THRESHOLD)

    # Re-derive the truth rather than trusting the payload's classification.
    res = await vault_dupes.find_duplicates(account_id, threshold, rehash=False)
    originals = {c["original"]["media_id"] for c in res["clusters"]}
    dupes = {d["media_id"]: d for c in res["clusters"] for d in c["dupes"]}

    approved: list[int] = []
    refused: dict[str, list[int]] = {"is_original": [], "not_a_duplicate": [],
                                     "has_sends": []}
    for mid in media_ids:
        if mid in originals:
            refused["is_original"].append(mid)
        elif mid not in dupes:
            refused["not_a_duplicate"].append(mid)
        elif dupes[mid]["send_count"] > 0 and not allow_sent:
            refused["has_sends"].append(mid)
        else:
            approved.append(mid)

    # Send OF a bounded body per call rather than one giant mediaIds array, and
    # treat each chunk as independently durable: if a later chunk fails, the
    # ones already hidden ARE hidden on OF, so they must still be recorded
    # locally and reported — silently 502-ing the whole request would leave the
    # mirror claiming media that is actually gone.
    done: list[int] = []
    of_error: str | None = None
    if approved:
        client = await asyncio.to_thread(ax._make_client, account_id)
        for i in range(0, len(approved), _OF_HIDE_CHUNK):
            chunk = approved[i:i + _OF_HIDE_CHUNK]
            try:
                await asyncio.to_thread(client.hide_vault_media, chunk)
            except Exception as e:  # noqa: BLE001
                of_error = str(e)[:300]
                log.warning("vault dupes hide chunk failed account=%s at=%s",
                            account_id, i, exc_info=True)
                break
            done.extend(chunk)
        if not done:
            raise HTTPException(status_code=502, detail={
                "error": "of_hide_failed", "detail": of_error,
                "attempted": len(approved)})
        # Mirror the removal locally so the grid, the picker, the describe
        # sweep and every automation stop seeing the copies immediately —
        # without waiting for the next collect to notice they're gone.
        now = datetime.utcnow()
        async with get_session() as s:
            for i in range(0, len(done), _DB_IN_CHUNK):
                await s.execute(
                    update(VaultItem)
                    .where(VaultItem.account_id == account_id,
                           VaultItem.media_id.in_(done[i:i + _DB_IN_CHUNK]))
                    .values(removed_at=now)
                )
            await s.commit()
        try:
            await vault_cache.invalidate(account_id)
        except Exception:  # noqa: BLE001
            pass
        log.info("vault dupes hidden account=%s n=%s", account_id, len(done))
    hidden = len(done)

    out: dict[str, Any] = {
        "account_id": account_id, "hidden": hidden,
        "hidden_ids": done,
        "refused": {k: v for k, v in refused.items() if v},
    }
    if of_error:
        # Partial success: report what OF actually took and what it didn't, so
        # the UI can re-offer the remainder instead of double-counting.
        out["of_error"] = of_error
        out["not_hidden"] = approved[len(done):]
    return out


@router.post("/admin/vault-ai/of-folders")
async def create_of_folder(payload: dict = Body(...)) -> dict[str, Any]:
    """Create a REAL OF vault folder (POST /vault/lists) and optionally add the
    selected media. Invalidates the vault cache so the new folder shows at once."""
    account_id = str(payload.get("account_id") or "")
    assert_account_owned(account_id)
    name = str(payload.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail={"error": "name_required"})
    media_ids = [int(x) for x in (payload.get("media_ids") or []) if str(x).lstrip("-").isdigit()]
    client = await asyncio.to_thread(ax._make_client, account_id)
    try:
        folder = await asyncio.to_thread(client.create_vault_list, name[:120])
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail={"error": "of_create_failed", "detail": str(e)[:300]})
    fid = folder.get("id")
    added = 0
    if media_ids and fid:
        try:
            await asyncio.to_thread(client.add_media_to_vault_list, fid, media_ids)
            added = len(media_ids)
        except Exception:  # noqa: BLE001
            log.warning("of add-media after create failed folder=%s", fid, exc_info=True)
    try:
        await vault_cache.invalidate(account_id)
    except Exception:  # noqa: BLE001
        pass
    return {"account_id": account_id, "id": fid, "name": folder.get("name"), "added": added}


@router.post("/admin/vault-ai/of-folders/{list_id}/add")
async def add_to_of_folder(list_id: int, payload: dict = Body(...)) -> dict[str, Any]:
    """Add selected media to a REAL OF folder (POST /vault/lists/{id}/media)."""
    account_id = str(payload.get("account_id") or "")
    assert_account_owned(account_id)
    media_ids = [int(x) for x in (payload.get("media_ids") or []) if str(x).lstrip("-").isdigit()]
    if not media_ids:
        raise HTTPException(status_code=400, detail={"error": "media_ids_required"})
    client = await asyncio.to_thread(ax._make_client, account_id)
    try:
        await asyncio.to_thread(client.add_media_to_vault_list, list_id, media_ids)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail={"error": "of_add_failed", "detail": str(e)[:300]})
    try:
        await vault_cache.invalidate(account_id)
    except Exception:  # noqa: BLE001
        pass
    return {"account_id": account_id, "list_id": list_id, "added": len(media_ids)}


@router.post("/admin/vault-ai/of-folders/{list_id}/rename")
async def rename_of_folder(list_id: int, payload: dict = Body(...)) -> dict[str, Any]:
    """Rename a REAL OF folder (PATCH /vault/lists/{id})."""
    account_id = str(payload.get("account_id") or "")
    assert_account_owned(account_id)
    name = str(payload.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail={"error": "name_required"})
    client = await asyncio.to_thread(ax._make_client, account_id)
    try:
        await asyncio.to_thread(client.rename_vault_list, list_id, name[:120])
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail={"error": "of_rename_failed", "detail": str(e)[:300]})
    try:
        await vault_cache.invalidate(account_id)
    except Exception:  # noqa: BLE001
        pass
    return {"account_id": account_id, "list_id": list_id, "name": name}


@router.delete("/admin/vault-ai/of-folders/{list_id}")
async def delete_of_folder(
    list_id: int, account_id: str = Query(...), clear_media: bool = Query(False),
) -> dict[str, Any]:
    """Delete a REAL OF folder (DELETE /vault/lists/{id}). `clear_media=False`
    keeps the media in the vault (only removes the folder)."""
    assert_account_owned(account_id)
    client = await asyncio.to_thread(ax._make_client, account_id)
    try:
        await asyncio.to_thread(client.delete_vault_list, list_id, clear_media)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail={"error": "of_delete_failed", "detail": str(e)[:300]})
    # Drop our local per-folder order rows for this folder too.
    async with get_session() as s:
        await s.execute(delete(VaultFolderItem).where(
            VaultFolderItem.account_id == account_id, VaultFolderItem.folder_id == list_id))
        await s.commit()
    try:
        await vault_cache.invalidate(account_id)
    except Exception:  # noqa: BLE001
        pass
    return {"account_id": account_id, "list_id": list_id, "deleted": True}


@router.post("/admin/vault-ai/of-folders/sort")
async def sort_of_folders(payload: dict = Body(...)) -> dict[str, Any]:
    """Set the OF folder-list ordering (POST /vault/lists/sort). Either
    {sort: name|recent|media|custom|default, order: asc|desc} or a manual
    {custom_order: [list_id,...]} drag order."""
    account_id = str(payload.get("account_id") or "")
    assert_account_owned(account_id)
    client = await asyncio.to_thread(ax._make_client, account_id)
    custom_order = payload.get("custom_order")
    try:
        if custom_order:
            ids = [int(x) for x in custom_order if str(x).isdigit()]
            await asyncio.to_thread(client.set_vault_lists_custom_order, ids)
        else:
            sort = str(payload.get("sort") or "recent")
            order = str(payload.get("order") or "desc")
            await asyncio.to_thread(client.sort_vault_lists, sort, order)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail={"error": "of_sort_failed", "detail": str(e)[:300]})
    try:
        await vault_cache.invalidate(account_id)
    except Exception:  # noqa: BLE001
        pass
    return {"account_id": account_id, "sorted": True}


# ── Describe (Qwen3-VL vision) ──────────────────────────────────────

import re as _re  # noqa: E402

import llm_client  # noqa: E402
import vision  # noqa: E402  (shared frame download / shrink / refusal test)
from jsonsafe import load_json  # noqa: E402
from llm_client import LLMCapExceeded, LLMError  # noqa: E402

# Proven prompt (service/_probe_vault_describe.py — 8/8 described, 0 refused, correct NSFW tags).
_DESCRIBE_PROMPT = (
    "You are tagging an OnlyFans creator's vault media so it can be searched and "
    "sold. Look at the image and return STRICT JSON only, no prose:\n"
    '{"description": "<one literal sentence>", '
    '"tags": ["<lowercase tags>"], '
    '"explicitness": "sfw|suggestive|explicit|hardcore", '
    '"nsfw": true|false, "sellable": true|false}\n'
    "Choose tags from (and beyond) this vocabulary where they apply: face, portrait, "
    "nude, topless, lingerie, bikini, outfit, ass, booty, boobs, pussy, toy, dildo, "
    "masturbation, fingering, sex, blowjob, cum, tease, shower, bath, feet, pov, solo, "
    "selfie, mirror, bed, closeup. Be literal and specific about what is actually shown. "
    "For a video the images are sampled frames — describe the overall clip."
)


# ── V2 describe prompt (bake-off variant B, chosen 2026-07-21) ──────
#
# Measured over 12 real items x 3 variants (service/_probe_describe_v2.py):
# V2@9 frames on qwen3-vl-30b beat the V1 prompt decisively and cost $0.88 for a
# 2,309-item vault, with 235B kept in the harness for later. V1 above is left in
# place as the cheap baseline and as what the harness diffs against.
#
# The four changes over V1, each aimed at a measured failure:
#  1. Ownership/consent framing — V1 gave the model no reason not to sanitise,
#     which is how a clip with fingers inside became "touching her genitals".
#  2. An explicit anti-euphemism rule naming that exact failure mode.
#  3. `beats` — an ordered walk across the sampled frames, so a 2-minute clip
#     stops collapsing into one static sentence.
#  4. A CLOSED taxonomy instead of free tag soup. V1's free vocabulary let it
#     emit `nude` + `lingerie` + `topless` on the SAME item — 78% of the graded vault's rows
#     carry a contradiction like that, which is why nothing downstream could
#     order a script. `clothing_state` and `acts` are single-valued rungs, and
#     they are what service/vault_scripts.py scores.
_V2_SCHEMA = """{
  "description":      "<2-4 literal sentences — see RULES>",
  "beats":            ["<what happens first>", "<then>", "..."],
  "acts":             ["<from ACTS>"],
  "penetration":      "none|fingers|toy|penis|unclear",
  "insertion_depth":  "none|shallow|deep|unclear",
  "clothing_state":   "dressed|lingerie_on|pulled_aside|pulled_down|partially_off|fully_nude|unclear",
  "clothing_items":   ["<literal garments, with colour/material>"],
  "genitals_covered": true|false,
  "underwear_visible": true|false,
  "body_focus":       ["<from BODY>"],
  "position":         "<from POSITION>",
  "setting":          "<bedroom|bathroom|shower|kitchen|car|outdoor|studio|other>",
  "camera":           "<handheld_selfie|mirror|tripod_wide|pov_overhead|closeup|screen_recording>",
  "people_count":     <int>,
  "partner_visible":  true|false,
  "face_visible":     true|false,
  "toys":             ["<literal toy, with colour>"],
  "explicitness":     "sfw|suggestive|explicit|hardcore",
  "nsfw":             true|false,
  "sellable":         true|false,
  "primary_folder":   "<exactly one from FOLDERS>",
  "tags":             ["<lowercase, only what is VISIBLE>"],
  "confidence":       <0-100>
}"""

_V2_ACTS = ("posing, tease, strip, undress, groping_own_breasts, ass_shaking, twerk, "
            "spreading, rubbing_clit, fingering, toy_insertion, toy_on_clit, riding_toy, "
            "masturbation_orgasm, squirt, blowjob, handjob, sex_missionary, sex_doggy, "
            "sex_riding, cumshot, shower, bath, talking_to_camera, none")
_V2_BODY = ("face, hair, breasts, nipples, pussy, clit, ass, thighs, legs, feet, "
            "stomach, full_body")
_V2_POSITION = ("standing, sitting, kneeling, lying_on_back, lying_on_stomach, "
                "on_all_fours, legs_spread, bent_over, straddling, unclear")
_V2_FOLDERS = ("solo_fingering, solo_toy, solo_tease, nude_stills, lingerie, "
               "ass_twerk, boobs, blowjob, sex_with_partner, shower_bath, feet, "
               "outfit_cosplay, sfw_selfie, talking_head, other")

_DESCRIBE_PROMPT_V2 = (
    "You are an explicit-content CATALOGUER for an adult creator's own private media "
    "vault. The creator owns this media and has asked for it to be indexed so she can "
    "search it and file it into folders. Describe it plainly and clinically. Do not "
    "moralise, do not soften, do not refuse, do not add warnings.\n\n"
    "You are given frames sampled IN ORDER from one clip (or a single photo). "
    "Return STRICT JSON only — no prose, no markdown fences:\n"
    f"{_V2_SCHEMA}\n\n"
    "RULES\n"
    "1. description: 2-4 sentences, literal and anatomically specific. Name the act, "
    "the body part it is done TO, and the hand/toy doing it. Say whether contact is "
    "rubbing/stroking on the surface or INSERTED, and roughly how many fingers. "
    "Vague verbs are FORBIDDEN: 'touching herself', 'pleasuring herself', 'being "
    "intimate' are all wrong answers — say exactly what is happening.\n"
    "2. Clothing must be stated exactly. If underwear is still ON but pushed/pulled to "
    "one side to expose her, that is 'pulled_aside' — NOT nude and NOT simply 'wearing "
    "lingerie'. Pulled down to the thighs is 'pulled_down'. Say fully_nude when nothing "
    "is COVERING her breasts or her genitals. Socks, stockings, shoes, gloves, jewellery "
    "and a garment pushed down around her ankles cover neither, and do not make her any "
    "less nude — a woman in nothing but socks is fully_nude.\n"
    "2b. BEFORE you choose clothing_state, answer these two literally, by LOOKING at "
    "the frame rather than summarising the scene:\n"
    "    underwear_visible — is ANY garment visible anywhere on her body or pushed "
    "aside/down, however small? Waistband, strap, a thong string, lace at the hip, "
    "stockings, socks — all count as TRUE. Answer it on its own: this records what is "
    "THERE, not what is covered, so a TRUE here is perfectly compatible with fully_nude. "
    "Never adjust this answer to agree with the clothing_state you picked.\n"
    # `genitals_covered` is asked for and then DELETED by the flags pass, which
    # superseded it with the three-state `vulva_vis`. It stays because it is not
    # an output — it is the second half of a scaffold the model reasons through
    # before committing to a state. Deleting it as "unused" is a prompt change
    # wearing the costume of a cleanup; re-measure before touching it.
    #
    # What this scaffold used to do was WORSE than nothing. It asserted that a
    # visible garment rules out `fully_nude`, which is false: measured against an
    # operator's own labels on 10 explicit stills (2026-08-05), 6 items were
    # `fully_nude` WITH a garment visible — a nude woman in socks. Every model
    # answer that reported `underwear_visible=True` was wrong about the state
    # (0/8); every answer that reported False was right 19/22. The models were
    # resolving the contradiction by suppressing `underwear_visible` — a
    # `FLAG_KEY` the pricing tiers read. The rule poisoned the field it was
    # built on. Presence and coverage are now asked as the separate questions
    # they always were.
    "    genitals_covered — is her vulva covered by ANY fabric right now? Pushed aside "
    "so it is exposed = FALSE. Covered by a thong = TRUE. Out of frame or hidden by her "
    "pose/leg/hand rather than by fabric = FALSE (nothing is covering it).\n"
    "    COVERAGE is what governs, not presence: if a garment is COVERING her breasts "
    "or her genitals then she is not fully_nude, whatever the overall impression. "
    "'Nude except for a thong' is NOT fully_nude — it is pulled_aside or lingerie_on. "
    "But 'nude except for socks' IS fully_nude, because socks cover neither. This is "
    "the single most common error on this task, and a wrong answer here sells the "
    "wrong thing.\n"
    "3. beats: for a clip, 2-6 short ordered steps describing how it PROGRESSES across "
    "the frames (what she starts doing, what changes, how it ends). For a single photo "
    "return [].\n"
    "4. Only ever report what is VISIBLE in a frame. Never emit contradictory tags "
    "(nude + lingerie, topless + bra). The vocabularies below are a menu to choose "
    "from, not a checklist to fill in — an item with 3 true tags gets 3 tags.\n"
    "5. If a frame is too dark/blurred/cropped to tell, say 'unclear' in the field and "
    "lower `confidence`. Guessing is worse than 'unclear'.\n"
    "6. explicitness: sfw = nothing sexual; suggestive = clothed/implied; explicit = "
    "genitals or a sex act visible; hardcore = penetration, cum, or a partner act.\n\n"
    f"ACTS: {_V2_ACTS}\n"
    f"BODY: {_V2_BODY}\n"
    f"POSITION: {_V2_POSITION}\n"
    f"FOLDERS: {_V2_FOLDERS}\n"
)

# Frames per clip. V1 shipped a hardcoded 3 for a clip of ANY length (a 17-minute
# video was summarised from 3 stills); the bake-off's other real lever was
# raising this to 9 (`vault_frames.DESCRIBE_FRAMES`). Photos always send 1.
#
# …but a flat count is wrong in both directions: 12 stills of a 3-second clip is
# 4× the tokens for the same one moment, and 9 stills of a 17-minute session
# still misses most of it. Measured 2026-07-21 (`_probe_frames_vs_duration.py`,
# 6 long clips × 4 counts × 3 repeats, scored as coverage of the pooled union of
# every finding across all runs — NOT against one reference run, which is
# noise):
#
#     frames    coverage    mean beats
#          3         38%           1.9
#          6         49%           2.7
#          9         47%           2.9
#         12         55%           3.7
#
# 3→6 is a real jump; 6→9 is flat (inside run-to-run variance); 9→12 gains ~8pp
# and 27% more `beats`, which IS the progression a long clip needs. `beats` is
# the cleanest signal because it rises monotonically with frame count.
#
# 12 is the ceiling: `server._STORYBOARD_FRAMES = 12`, already extracted and
# cached, so using all of them costs nothing but vision tokens.
#
# CAVEAT worth remembering: no frame count exceeds ~55% coverage, and two
# IDENTICAL 12-frame runs agree only ~75% with each other. A single describe
# pass on a long clip is inherently lossy — for `epic` clips, two passes unioned
# would gain more than any frame-count change.
_FRAME_LADDER: tuple[tuple[int, int], ...] = (
    (30, 6),      # < 30s   — one moment; more stills just re-see it
    (180, 9),     # < 3min
)
_FRAME_LADDER_MAX = 12  # >= 3min, and the storyboard ceiling


def _frames_for_duration(seconds: int | None) -> int:
    """How many stills a clip of this length gets (see `_FRAME_LADDER`)."""
    s = int(seconds or 0)
    for limit, n in _FRAME_LADDER:
        if s < limit:
            return n
    return _FRAME_LADDER_MAX


# ── Cheap flags pass ────────────────────────────────────────────────
#
# `clothing_state` was wrong on ~1 in 3 of the items the $50 tier is built from:
# it reported `fully_nude` for shots where she still had panties on, and the
# error was invisible to every stored-data check because the enum, the garment
# list and the prose all agreed and all three were wrong.
#
# Two fixes, and this is the cheap one. Rather than re-running a full describe
# (2,788 tokens, ~16s/item) to recover two booleans the tier actually reads, ask
# ONLY for those booleans: ~350 tokens, ~3s/item — measured 5x faster and ~40x
# cheaper over the graded vault, about $0.001 for a 103-item vault.
#
# The image still has to keep its ASPECT RATIO. `files.thumb` is 300x300 and
# OF's stills are 3:4, so the thumb is a centre-CROP with the bottom of the
# frame missing — which is precisely where a waistband sits, and precisely why
# describe kept saying `fully_nude`. So we start from `preview` and downscale it
# ourselves: fewer pixels, whole picture. 448px on the long edge is the measured
# floor — 320px flips an item that 448 and 640 agree on, and 640 buys nothing.
# (the flag-run edge now lives with the resizer — vision.MAX_EDGE_DEFAULT)

# The prompt VERSION stamped onto every answer it produces.
#
# Bumped whenever the wording changes in a way that could move an answer. It
# exists because three separate times in one session a score was taken against
# a half-finished re-run and read as a result: v2 rows and v3 rows are
# indistinguishable once stored, so a partial sweep looks exactly like a
# finished one. With a stamp, `flags_all` can re-run only what is stale, and a
# scorer can refuse to report on a mixed vault.
#
#   1  two booleans, `pussy_covered` / `breasts_covered`
#   2  three-state enum per region, garment named first
#   3  in-frame/skin decision tree + sheer-fabric rule + insertion reconcile
_FLAGS_VERSION = 3

# Canonical in `vault_ai_brief` — `vault_scripts` gates its mass-safe lane on
# the same tuple, and two copies would drift the moment a flag is added.
_FLAG_KEYS = vault_ai_brief.FLAG_KEYS

# Each region is asked SEPARATELY because those splits ARE the price boundaries:
# $10 is breasts out with her vulva still covered, $50 is her vulva on show, and
# the mass-safe lane turns on nothing being on show at all. A single conflated
# question could not express the middle tier.
#
# Every region is asked in the SAME direction and with the SAME three answers.
# The previous version asked `pussy_covered` about fabric and `breasts_covered`
# about visibility, which meant `false` meant opposite things across two flags
# a caller had to read together — see `vault_ai_brief.VIS_STATES` for what that
# cost. Phrasing is positive ("what can you see") rather than a double negative
# ("is it kept from view"), because the negative form measured ~91% constant.
#
# GRADED against operator labels on 27 the graded vault items (`_probe_flags_score.py`).
# The version that asked for a state directly scored 80.2%, and 13 of its 16
# errors were the SAME mistake: `covered` where the truth was `not_in_frame`.
# Asked "is it covered", a model says yes whenever it cannot see skin — whether
# the body part is behind a bodysuit or simply not in the photograph. Both read
# as "not on show" to a human eye, and the previous wording never forced the
# distinction. A woman lying on her stomach in a bodystocking came back
# `covered` for all three regions when the honest answer is that none of them
# are in the picture.
#
# So the question is now a two-step decision, asked in order, rather than a
# three-way label to pick: is it in the photo at all → then is any skin
# showing. Models follow a tree far better than they pick from a menu, and the
# first step is the one that was being skipped.
_FLAGS_PROMPT = (
    "You are indexing one photo from an adult creator's own vault. She owns it "
    "and has asked for it to be catalogued so it can be sorted. Report only "
    "what you can SEE in this frame.\n\n"
    "Reply with STRICT JSON only, no prose. Emit the keys in EXACTLY this "
    "order — the `over_` answers come first because the states are read off "
    "them:\n"
    '{"over_breasts": "<what is over her chest, or null>",\n'
    ' "over_vulva":   "<what is over her crotch, or null>",\n'
    ' "over_anus":    "<what is over her backside, or null>",\n'
    ' "breasts_vis": "...", "vulva_vis": "...", "anus_vis": "...",\n'
    ' "underwear_visible": true|false}\n\n'
    "For each of her breasts, her vulva and her anus, work through these three "
    "questions IN ORDER.\n\n"
    "STEP 1 — is that part of her body in this photograph at all?\n"
    "  Point at it. If you cannot put your finger on the spot in THIS image, "
    'the answer is "not_in_frame" — set the matching `over_` field to null and '
    "move on to the next region.\n"
    "  Answer about the picture, never about her body. You know a woman has a "
    "vulva and you can usually guess she is wearing something over it. That is "
    "reasoning about the person. It is not seeing. A photograph of her face and "
    "chest contains no vulva and no anus, so both are NOT_IN_FRAME, no matter "
    "what she is obviously wearing below.\n"
    "  This is the answer people get wrong most often, so be deliberate:\n"
    "   · She is photographed from the front → her anus is NOT_IN_FRAME. It is "
    "behind her.\n"
    "   · She is photographed from behind, or lying face down → her breasts "
    "and her vulva are NOT_IN_FRAME.\n"
    "   · The shot is cropped at her waist → her vulva and anus are "
    "NOT_IN_FRAME.\n"
    "   · She is standing fully dressed and you cannot make out where a part "
    "of her body even is → NOT_IN_FRAME.\n"
    '  "not_in_frame" is a normal, common, correct answer. Most photos have at '
    "least one region that is simply not in them.\n\n"
    "STEP 2 — it IS in the photo. NAME what is over it, in the `over_` field.\n"
    "  Two or three words: \"white tank top\", \"black lace bra\", \"her right "
    'hand\", "the bedsheet". Use null only when you are looking at skin with '
    "nothing on it.\n"
    "  Name it BEFORE you judge how exposed she is. Saying what a woman is "
    "wearing is a far easier question than rating a photo, and the second "
    "answer falls out of the first.\n\n"
    "STEP 3 — now read the state off what you just named.\n"
    '  "covered" — you named something, and it hides that skin completely.\n'
    '  "bare"    — you can see skin, even a little. ANY of it counts: one '
    "nipple showing between her fingers, part of a breast beside her hand, a "
    "sliver of vulva beside a thong. Partly showing is showing.\n\n"
    "THE MISTAKE TO AVOID at step 2: clothing that shows the SHAPE of her body "
    'is still clothing. A tank top, t-shirt, bra, bodysuit or swimsuit is '
    '"covered" no matter how tight, how much cleavage, or how sexy the photo '
    "is.\n"
    "  Sheer fabric is fabric. Lace, mesh, fishnet and a bodystocking are "
    'CLOTHING, so skin seen THROUGH them is "covered" — the garment is on her. '
    '"bare" means there is no garment over that skin at all: pulled down, '
    "pulled aside, or never put on.\n\n"
    "IF SHE IS TOUCHING HERSELF, the skin she is touching is BARE. Underwear "
    "gets pulled aside for that. A hand between her legs, fingers or a toy "
    "inside her, her legs spread and held open, a clitoris being stroked — in "
    "every one of those her vulva is \"bare\", even if you can also see a thong "
    "somewhere in the frame. Do not name that thong as covering her: it has "
    "been moved out of the way, which is the entire reason you can see what you "
    "are looking at.\n\n"
    "Judge each region on its own. She is very often bare in one place and "
    "covered in another, and that difference is the whole point of this check. "
    "Never copy one garment into every `over_` field — a bra does not cover her "
    "vulva, a thong does not cover her breasts, and if you find yourself "
    "writing the same words three times, at least two of them are wrong. Do not "
    "let how explicit the picture FEELS decide anything: a picture can be very "
    "explicit with nothing actually on show, and a plain one can have "
    "everything on show.\n\n"
    "underwear_visible — is ANY garment visible anywhere on her body, worn or "
    "pushed aside or pulled down, however small? A waistband, a strap, a thong "
    "string, a band of lace at the hip or at the very bottom edge of the "
    "picture, stockings — all TRUE. Look at the edges of the frame before you "
    "answer FALSE.\n"
)



def _parse_describe(text: str) -> dict[str, Any]:
    m = _re.search(r"\{.*\}", text or "", _re.DOTALL)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except Exception:  # noqa: BLE001
        return {}


# Reading what a FAN sent lives in `inbound_describe` — it is a CHAT feature and
# only ever sat here because the vision primitives did. Those are now `vision`.
# This module is back to one job: her VAULT.


async def _describe_one(account_id: str, media_id: int, model: str = "qwen3-vl-30b",
                        prompt_version: str = "v2",
                        frames: int | None = None) -> dict[str, Any]:
    """Describe one item.

    `prompt_version` picks the bake-off variant: "v2" (default — rich structured
    schema, the one in production) or "v1" (the original one-sentence prompt,
    ~4× cheaper, kept for bulk passes where only a rough tag is wanted).
    `frames` overrides the duration ladder; None = `_frames_for_duration`.
    """
    async with get_session() as s:
        item = await s.get(VaultItem, (account_id, media_id))
    if item is None:
        raise HTTPException(status_code=404, detail={"error": "not_in_mirror", "media_id": media_id})
    raw = load_json(item.raw_json, {}) or {}
    want = int(frames) if frames else _frames_for_duration(item.duration_seconds)
    got = await vault_frames.collect(account_id, item, raw, want=want)
    if not got.images:
        status = _NO_IMAGE_STATUS.get(got.reason, "fetch_failed")
        async with get_session() as s:
            await s.execute(
                update(VaultItem)
                .where(VaultItem.account_id == account_id, VaultItem.media_id == media_id)
                .values(describe_status=status, describe_generated_at=datetime.utcnow())
            )
            await s.commit()
        return {"media_id": media_id, "ok": False, "status": status}

    prompt = _DESCRIBE_PROMPT if str(prompt_version) == "v1" else _DESCRIBE_PROMPT_V2
    content = [{"type": "text", "text": prompt}]
    content += [{"type": "image_url", "image_url": {"url": u}} for u in got.images]

    # Try 30b; on refusal/blank escalate ONCE to 235b (never overwrite a good
    # existing description with a blank).
    result = None
    used_model = model
    data: dict[str, Any] = {}
    for attempt_model in (model, "qwen3-vl-235b"):
        try:
            result = await llm_client.chat(
                model=attempt_model,
                messages=[{"role": "user", "content": content}],
                purpose="describe_media",
                account_id=account_id,
                temperature=0.1,
            )
        except LLMCapExceeded as e:
            # Deliberately leaves describe_status alone: "capped" means try again
            # later, and stamping a status would make the retry sweep skip it.
            # `detail` is what an operator reads, both here and in the sweep's
            # first_error, so it carries the same sentence the pre-flight
            # refusal does rather than the exception's own developer text.
            budget = await _vision_budget(account_id)
            return {"media_id": media_id, "ok": False, "status": "capped",
                    "detail": budget["blocked_reason"] or str(e)}
        except LLMError as e:
            # RECORD the failure. Leaving the status NULL made a failed item
            # indistinguishable from one never attempted — a missing
            # DEEPINFRA_API_KEY failed all 327 items of a vault while the UI
            # reported "done 327/327" and the row stayed blank.
            async with get_session() as s:
                await s.execute(
                    update(VaultItem)
                    .where(VaultItem.account_id == account_id, VaultItem.media_id == media_id)
                    .values(describe_status="failed", describe_model=attempt_model,
                            describe_generated_at=datetime.utcnow())
                )
                await s.commit()
            return {"media_id": media_id, "ok": False, "status": "error", "detail": str(e)[:300]}
        used_model = attempt_model
        if not vision.is_refusal(result.content):
            parsed = _parse_describe(result.content)
            if parsed.get("description") or parsed.get("tags"):
                data = parsed
                break
        if attempt_model == "qwen3-vl-235b":
            break  # escalation also failed

    if not (data.get("description") or data.get("tags")):
        async with get_session() as s:
            await s.execute(
                update(VaultItem)
                .where(VaultItem.account_id == account_id, VaultItem.media_id == media_id)
                .values(describe_status="refused", describe_model=used_model,
                        describe_call_id=(result.call_id if result else None),
                        describe_generated_at=datetime.utcnow())
            )
            await s.commit()
        return {"media_id": media_id, "ok": False, "status": "refused",
                "raw": (result.content if result else "" or "")[:200]}

    tags = [str(t).lower().strip() for t in (data.get("tags") or []) if str(t).strip()][:12]
    desc = str(data.get("description") or "")
    tier = data.get("explicitness")  # sfw|suggestive|explicit|hardcore
    nsfw = bool(data.get("nsfw")) if data.get("nsfw") is not None else None
    sellable = bool(data.get("sellable")) if data.get("sellable") is not None else None
    is_video = item.kind == "video"
    # Fold the V2 taxonomy into the local search blob too, so a search for
    # "pulled_aside" or "solo_toy" hits even though those never appear in the
    # prose. `beats` carries the clip's progression, which is often the only
    # place a mid-clip act is named at all.
    v2_terms: list[str] = []
    for key in ("acts", "body_focus", "toys", "clothing_items", "beats"):
        v2_terms += [str(x) for x in (data.get(key) or []) if str(x).strip()]
    for key in ("clothing_state", "penetration", "position", "setting",
                "camera", "primary_folder"):
        if data.get(key):
            v2_terms.append(str(data[key]))
    search_text = " ".join([desc, *tags, *v2_terms]).lower()

    # Human edits win: never overwrite an effective field the operator locked.
    locked = set(load_json(item.locked_fields_json, []))
    # Stamp WHICH variant produced this row: a v1 row is a candidate for a later
    # v2 re-scan, and without the stamp "already described" is ambiguous.
    data = {**data, "_prompt_version": ("v1" if str(prompt_version) == "v1" else "v2"),
            "_frames": len(got.images)}

    # `ai_fields_json` is written whole from `data`, which holds ONLY what the
    # describe prompt answers — so re-describing an item used to silently drop
    # everything the OTHER passes had put in the same column:
    #
    #   * the exposure flags. A described item came back with no `_flags_v` at
    #     all, the gated lanes emptied, and the flags pass had to be run again.
    #   * the dispute corrections. There are TWO override mechanisms and they
    #     differ: `edit_item` leaves this blob as the model's raw answer and
    #     wins at READ time through `effective_value` (which is why the fresh
    #     description below is expected to land here), while `vault_ai_fix`
    #     writes INTO the blob — its readers, the lanes and `contradictions`,
    #     go straight to `ai_fields_json` and never call `effective_value`. So
    #     only the `FIXABLE` half is restored here; carrying the rest would
    #     overwrite the model's answer with the operator's and erase the
    #     evidence that a re-run happened at all.
    #
    # The flags are a different reading of the SAME image, so they stay valid
    # across a re-describe. A locked dispute fix outranks model and carried alike.
    prev = load_json(item.ai_fields_json, {}) or {}
    for key in (*vault_ai_brief.FLAG_KEYS, *vault_ai_brief.OVER_KEYS.values(),
                "_flags_v", "_flags_model", "_flags_frames", "_flags_arc"):
        if key in prev:
            data[key] = prev[key]
    for key, val in (load_json(item.operator_overrides_json, {}) or {}).items():
        if key not in locked or key not in vault_ai_fix.FIXABLE:
            continue
        if val is None:
            data.pop(key, None)
        else:
            data[key] = val
    vals: dict[str, Any] = {
        "describe_status": "described",
        "describe_model": used_model,
        "describe_call_id": result.call_id if result else None,
        "describe_generated_at": datetime.utcnow(),
        "ai_fields_json": json.dumps(data, ensure_ascii=False, default=str),
        "frames_sampled": len(got.images) if is_video else None,
    }
    if "tip_vault_flag" not in locked:
        vals["tip_vault_flag"] = sellable
    if "tags" not in locked:
        vals["tags"] = json.dumps(tags)
    if "explicitness_tier" not in locked:
        vals["explicitness_tier"] = str(tier) if tier else None
    if "description" not in locked:
        if is_video:
            vals["video_description"] = desc
        else:
            vals["description"] = desc
    if "search_text" not in locked:
        vals["search_text"] = search_text

    async with get_session() as s:
        await s.execute(
            update(VaultItem)
            .where(VaultItem.account_id == account_id, VaultItem.media_id == media_id)
            .values(**vals)
        )
        await s.commit()
    return {
        "media_id": media_id, "ok": True, "status": "described",
        "description": desc, "tags": tags,
        "explicitness_tier": str(tier) if tier else None,
        "nsfw": nsfw, "sellable": sellable,
        "describe_model": used_model,
        "cost_millicents": result.cost_cents if result else 0,
        "images_sent": len(got.images),
    }


@router.post("/admin/vault-ai/describe")
async def describe_one(payload: dict = Body(...)) -> dict[str, Any]:
    account_id = str(payload.get("account_id") or "")
    assert_account_owned(account_id)
    try:
        media_id = int(payload.get("media_id"))
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=400, detail={"error": "media_id_required"})
    # Over the cap the LLM call is refused anyway, but only AFTER the frames
    # have been pulled off OF and (for a clip) cut with ffmpeg. Answering here
    # keeps a hopeless click cheap, and keeps "Describe selected" from grinding
    # through fifty items' worth of fetches to write nothing. Same shape the
    # in-flight refusal returns, so a caller has one case to handle.
    budget = await _vision_budget(account_id)
    if budget["capped"]:
        return {"media_id": media_id, "ok": False, "status": "capped",
                "detail": budget["blocked_reason"]}
    model = str(payload.get("model") or "qwen3-vl-30b")
    res = await _describe_one(
        account_id, media_id, model=model,
        prompt_version=str(payload.get("prompt_version") or "v2"),
        frames=payload.get("frames"),
    )
    # Flag it in the same breath, exactly as the sweep does. Describing ONE item
    # from the UI otherwise left it as the only row in the vault the gated lanes
    # could not read, and nothing on screen said so.
    if res.get("ok"):
        try:
            await _flags_one(account_id, media_id)
        except Exception:  # noqa: BLE001
            log.exception("flags after describe failed media=%s", media_id)
    return res


# ── Describe ALL (background sweep, gen_info-style) ─────────────────
_describe_running: set[str] = set()
_describe_progress: dict[str, dict[str, Any]] = {}
_DESCRIBE_CONCURRENCY = 4

# Process-wide ceiling on concurrent vision work across ALL accounts and BOTH
# sweeps (describe + flags). The per-sweep _DESCRIBE_CONCURRENCY/_FLAGS_CONCURRENCY
# bound ONE sweep; without a shared gate, kicking "Describe all" on 16 accounts
# fans out to 16×4 = 64 concurrent DeepInfra calls plus their ffmpeg/OF frame
# fetches and starves the relay threadpool (cost-audit finding: ReadTimeouts +
# 500s while sweeps run). Lazily created so it binds to the running event loop,
# not import time.
_VISION_GLOBAL_CONCURRENCY = 8
_vision_sema: "asyncio.Semaphore | None" = None


def _vision_gate() -> "asyncio.Semaphore":
    """The process-wide vision-concurrency gate (see _VISION_GLOBAL_CONCURRENCY).
    Every describe/flags sweep acquires this IN ADDITION to its per-sweep bound, so
    total concurrent vision work is capped no matter how many accounts sweep at once."""
    global _vision_sema
    if _vision_sema is None:  # single-threaded asyncio: check→set is atomic (no await)
        _vision_sema = asyncio.Semaphore(_VISION_GLOBAL_CONCURRENCY)
    return _vision_sema

# Why a frameless item got no frames, as a `describe_status`.
#
# The two outcomes want OPPOSITE retry policies, and collapsing them is what made
# the 2026-08-04 incident invisible: every failure was stamped `blocked_drm`, so
# 1,777 items whose signatures had merely expired inherited the 7-day cool-off
# meant for clips OF genuinely cannot render, and the next sweep skipped them.
#   no_variant   → OF answered with a payload carrying nothing renderable.
#                  Asking again changes nothing. Cool off.
#   fetch_failed → we could not read bytes we have every reason to think exist.
#                  Retry on the very next sweep, no cool-off.
#   gone         → OF has deleted it. The still store already stamped
#                  `removed_at`, which every candidate query filters on, so this
#                  status is only ever read by whoever asked for THIS item.
# Unknown reasons fall to the retryable side: over-retrying costs a fetch,
# under-retrying costs a description nobody notices is missing.
_NO_IMAGE_STATUS = {"no_variant": "blocked_drm", "fetch_failed": "fetch_failed",
                    "gone": "gone"}

# A blocked_drm item IS retried by a normal sweep (OF poster-frame fallback may
# start working) — but not on EVERY sweep. Once it re-settles to blocked_drm we
# cool it off this long before the next attempt, so a truly un-renderable clip
# stops costing an OF poster-fetch every pass (cost-audit finding). A force or
# restage sweep ignores the cooldown and revisits it regardless.
_DRM_RETRY_AFTER = timedelta(days=7)


async def _run_describe_all(account_id: str, force: bool,
                            prompt_version: str = "v2",
                            model: str = "qwen3-vl-30b",
                            restage: bool = False) -> None:
    """Background describe sweep.

    Three selections:
      force=False (default) — only items not yet successfully described.
      restage=True          — items described by a DIFFERENT prompt version, i.e.
                              the "we have a better prompt now, re-scan the old
                              rows" pass. Cheaper than force: rows already on
                              this version are left alone, so an interrupted
                              re-scan resumes instead of starting over.
      force=True            — everything, regardless.
    """
    try:
        async with get_session() as s:
            stmt = select(VaultItem.media_id).where(
                VaultItem.account_id == account_id, VaultItem.removed_at.is_(None),
                VaultItem.kind.in_(_DESCRIBABLE_KINDS),
            )
            if force:
                pass
            elif restage:
                # `_prompt_version` is stamped into ai_fields_json by _describe_one.
                # A row that predates the stamp has no marker at all, so it counts
                # as stale and gets re-scanned.
                stmt = stmt.where(
                    (VaultItem.describe_status != "described")
                    | (VaultItem.describe_status.is_(None))
                    | (VaultItem.ai_fields_json.is_(None))
                    | (~VaultItem.ai_fields_json.like(
                        f'%"_prompt_version": "{prompt_version}"%'))
                )
            else:
                # Everything not already described — INCLUDING blocked_drm, with
                # no cool-off. `_DRM_RETRY_AFTER` exists so the RECURRING 6h
                # `describe_media` sweep stops re-paying a poster-fetch on a clip
                # OF cannot render; it still applies there. It has no business
                # here. This is a button a person pressed, having just read the
                # count on it, and the one thing it must do is attempt what that
                # count promised. Honouring the cooldown made it silently skip
                # 1,777 of the 1,777 items it offered to describe — the operator
                # clicks, watches nothing happen, and has no way to find out why.
                # A wasted fetch is cheaper than a control nobody can trust.
                stmt = stmt.where(VaultItem.describe_status.isnot("described"))
            ids = [r[0] for r in (await s.execute(stmt)).all()]
        total = len(ids)
        done = 0
        capped = False
        _describe_progress[account_id] = {"total": total, "done": 0, "capped": False}
        sem = asyncio.Semaphore(_DESCRIBE_CONCURRENCY)

        failed = 0
        first_error = ""

        async def _one(mid: int) -> None:
            nonlocal done, capped, failed, first_error
            async with sem, _vision_gate():  # per-sweep bound + process-wide gate
                if capped:
                    return
                res = await _describe_one(account_id, mid, model=model,
                                          prompt_version=prompt_version)
                if res.get("status") == "capped":
                    capped = True
                # Flag it in the same breath. "Described" used to mean prose
                # plus `clothing_state` and nothing about what is actually on
                # show, so a freshly-described vault had no flags at all and
                # every gated lane was empty until someone remembered a second
                # button. The flags pass is ~3s and ~$0.00002 against describe's
                # ~16s and ~$0.0086 — a fifth of a percent — so making it
                # conditional bought nothing but a way to forget it.
                if res.get("ok"):
                    try:
                        await _flags_one(account_id, mid)
                    except Exception:  # noqa: BLE001
                        # Never let the cheap pass take down the expensive one:
                        # the description is already written and worth keeping.
                        log.exception("flags after describe failed media=%s", mid)
                # `done` means "visited", not "described". Track failures
                # separately: a whole vault can fail (bad/missing provider key)
                # and still report done=N/N, which reads as success.
                if not res.get("ok"):
                    failed += 1
                    if not first_error and res.get("detail"):
                        first_error = str(res["detail"])[:200]
                done += 1
                _describe_progress[account_id] = {
                    "total": total, "done": done, "capped": capped,
                    "failed": failed, "error": first_error,
                }

        # Run in bounded chunks so a giant vault doesn't spawn thousands of tasks.
        for i in range(0, len(ids), 50):
            if capped:
                break
            await asyncio.gather(*(_one(m) for m in ids[i:i + 50]))
        try:
            q = await vault_ai_fix.review_queue(account_id, limit=1,
                                                version=_FLAGS_VERSION,
                                                only_iffy=True)
            _describe_progress[account_id] = {
                **_describe_progress.get(account_id, {}), "needs_review": q["iffy"]}
        except Exception:  # noqa: BLE001
            log.exception("review count after describe failed account=%s", account_id)
        if failed:
            log.warning("describe_all account=%s done=%s/%s FAILED=%s capped=%s first_error=%s",
                        account_id, done, total, failed, capped, first_error)
        else:
            log.info("describe_all done account=%s done=%s/%s capped=%s", account_id, done, total, capped)
    except Exception as e:  # noqa: BLE001
        # Into the progress snapshot, not just the log. `running` goes False in
        # `finally` either way, so a sweep that DIED and a sweep that finished
        # were previously distinguishable only by counting — and a caller that
        # sees done<total has no way to know whether to wait or to press the
        # button again.
        log.exception("describe_all failed account=%s", account_id)
        _describe_progress[account_id] = {
            **_describe_progress.get(account_id, {}),
            "aborted": True, "error": f"{type(e).__name__}: {e}"[:200],
        }
    finally:
        _describe_running.discard(account_id)
        # keep last progress snapshot for the status endpoint to report "done"


@router.post("/admin/vault-ai/describe-all")
async def describe_all(payload: dict = Body(...)) -> dict[str, Any]:
    account_id = str(payload.get("account_id") or "")
    assert_account_owned(account_id)
    if account_id in _describe_running:
        raise HTTPException(status_code=409, detail={"error": "describe_already_running"})
    # REFUSE to start over the daily cap.
    #
    # Nothing here was ever unsafe: `llm_client._reserve` refuses every call
    # over the cap and the sweep stops on the first one. It was DISHONEST. The
    # sweep ended, the button went straight back to "Describe all (1777)", and a
    # second press started another one — which pulled frames off OF and cut them
    # with ffmpeg for a batch of items, had every LLM call refused, and reported
    # the same 1777 still to do. Nothing on screen ever said "you are out of
    # budget today", so the only reading available was that describe is broken,
    # and the natural response to that is to press it again.
    budget = await _vision_budget(account_id)
    if budget["capped"]:
        raise HTTPException(status_code=429,
                            detail={"error": "daily_cap_reached", **budget})
    force = bool(payload.get("force"))
    restage = bool(payload.get("restage"))
    prompt_version = "v1" if str(payload.get("prompt_version")) == "v1" else "v2"
    model = str(payload.get("model") or "qwen3-vl-30b")
    if model not in llm_client.MODELS:
        raise HTTPException(status_code=400, detail={"error": "unknown_model",
                                                    "model": model})
    _describe_running.add(account_id)
    asyncio.create_task(_run_describe_all(account_id, force,
                                          prompt_version=prompt_version,
                                          model=model, restage=restage))
    return {"account_id": account_id, "status": "running",
            "prompt_version": prompt_version, "model": model,
            "force": force, "restage": restage}


@router.get("/admin/vault-ai/describe-all/plan")
async def describe_all_plan(
    account_id: str = Query(...),
    prompt_version: str = Query("v2"),
) -> dict[str, Any]:
    """What each sweep mode WOULD do, so the UI can show real counts (and a cost
    estimate) before the operator commits to a re-scan of the whole vault."""
    assert_account_owned(account_id)
    pv = "v1" if str(prompt_version) == "v1" else "v2"
    async with get_session() as s:
        live = select(VaultItem).where(VaultItem.account_id == account_id,
                                       VaultItem.removed_at.is_(None),
                                       VaultItem.kind.in_(_DESCRIBABLE_KINDS))
        total = await s.scalar(
            select(func.count()).select_from(live.subquery()))
        undescribed = await s.scalar(select(func.count()).select_from(
            live.where(VaultItem.describe_status.isnot("described")).subquery()))
        stale = await s.scalar(select(func.count()).select_from(
            live.where(
                (VaultItem.describe_status != "described")
                | (VaultItem.describe_status.is_(None))
                | (VaultItem.ai_fields_json.is_(None))
                | (~VaultItem.ai_fields_json.like(f'%"_prompt_version": "{pv}"%'))
            ).subquery()))
    # Carried so the button can go grey BEFORE it is pressed, with the reason
    # already on it. The POST refuses over the cap either way, but a disabled
    # button that says why is the difference between "out of budget until
    # midnight" and "broken".
    budget = await _vision_budget(account_id)
    return {"account_id": account_id, "prompt_version": pv,
            "total": int(total or 0),
            "undescribed": int(undescribed or 0),
            "restage": int(stale or 0),
            "cap": budget}


@router.get("/admin/vault-ai/describe-all/status")
async def describe_all_status(account_id: str = Query(...)) -> dict[str, Any]:
    assert_account_owned(account_id)
    prog = _describe_progress.get(account_id)
    return {
        "account_id": account_id,
        "running": account_id in _describe_running,
        "progress": prog,
    }


# ── OF-search harvest (make local search ⊇ OF search) ──────────────
# OF's vault search matches server-side on hidden data (post captions, its own
# index) and returns NO tags — just the filtered id list (confirmed from the
# live response: 14 keys, zero text). So we run OF's own search in the
# background, fold the matched terms into each item's search_text/of_terms, and
# cache the query — permanently, so next time it's instant AND local search
# returns everything OF would PLUS our AI tags.
_OF_SEARCH_TTL_S = 7 * 24 * 3600
_OF_SEARCH_MAX_PAGES = 6

# "What sells on OnlyFans" seed set — categories + attributes we harvest OF's own
# search for, so local search covers everything OF would match across the selling
# taxonomy (folded in next to the AI vision tags). Explicit acts/body, clothing,
# settings, style, and common attributes.
_SELLING_KEYWORDS = [
    # explicit / body
    "nude", "naked", "topless", "porn", "masturbation", "fingering", "sex", "blowjob",
    "cum", "pussy", "ass", "booty", "boobs", "tits", "feet", "toy", "dildo", "vibrator",
    "anal", "squirt", "creampie", "riding",
    # clothing / lingerie
    "lingerie", "bra", "panties", "thong", "bikini", "dress", "outfit", "heels",
    "stockings", "cosplay", "uniform",
    # settings / themes
    "shower", "bath", "bed", "pool", "beach", "car", "travel", "gym", "mirror", "outdoor",
    # style / framing
    "pov", "solo", "selfie", "tease", "closeup", "face", "flowers",
    # attributes / colors
    "tattoo", "blonde", "red", "black", "white", "blue", "pink",
]


async def _harvest_query(account_id: str, q: str, client: Any = None) -> dict[str, Any]:
    """Harvest OF's own vault search for `q` and fold the term into each matched
    mirror item's of_terms/search_text. TTL-cached via vault_of_query_log."""
    q = (q or "").strip().lower()
    if not q:
        return {"query": q, "count": 0, "source": "empty", "ids": []}
    now = datetime.utcnow()
    async with get_session() as s:
        logrow = await s.get(VaultOfQueryLog, (account_id, q))
    if logrow and (now - logrow.fetched_at).total_seconds() < _OF_SEARCH_TTL_S:
        async with get_session() as s:
            ids = [
                r[0] for r in (
                    await s.execute(
                        select(VaultItem.media_id).where(
                            VaultItem.account_id == account_id,
                            func.lower(func.coalesce(VaultItem.of_terms, "")).like(f"%{q}%"),
                        )
                    )
                ).all()
            ]
        return {"query": q, "ids": ids, "count": len(ids), "source": "cache"}

    if client is None:
        try:
            client = await asyncio.to_thread(ax._make_client, account_id)
        except Exception:  # noqa: BLE001
            return {"query": q, "ids": [], "count": 0, "source": "no_client"}
    matched: list[int] = []
    offset = 0
    for _ in range(_OF_SEARCH_MAX_PAGES):
        try:
            resp = await asyncio.to_thread(
                client.vault_media, limit=40, offset=offset, type="all", sort="desc", query=q,
            )
        except Exception:  # noqa: BLE001
            break
        items = (resp or {}).get("list") or []
        if not items:
            break
        matched += [int(it["id"]) for it in items if it.get("id") is not None]
        if not (resp or {}).get("hasMore"):
            break
        offset += 40

    async with get_session() as s:
        for mid in matched:
            item = await s.get(VaultItem, (account_id, mid))
            if item is None:
                continue
            terms = set(load_json(item.of_terms, []))
            if q in terms:
                continue
            terms.add(q)
            new_search = ((item.search_text or "") + " " + q).strip().lower()
            await s.execute(
                update(VaultItem)
                .where(VaultItem.account_id == account_id, VaultItem.media_id == mid)
                .values(of_terms=json.dumps(sorted(terms)), search_text=new_search)
            )
        await s.execute(
            sqlite_insert(VaultOfQueryLog)
            .values(account_id=account_id, query=q, match_count=len(matched), fetched_at=now)
            .on_conflict_do_update(
                index_elements=["account_id", "query"],
                set_={"match_count": len(matched), "fetched_at": now},
            )
        )
        await s.commit()
    return {"query": q, "ids": matched, "count": len(matched), "source": "of"}


@router.post("/admin/vault-ai/search-of")
async def search_of(payload: dict = Body(...)) -> dict[str, Any]:
    account_id = str(payload.get("account_id") or "")
    assert_account_owned(account_id)
    r = await _harvest_query(account_id, str(payload.get("query") or ""))
    return {"account_id": account_id, **r}


# ── Harvest the whole "what sells" keyword set (background) ─────────
_kw_running: set[str] = set()
_kw_progress: dict[str, dict[str, Any]] = {}


async def _run_harvest_keywords(account_id: str) -> None:
    try:
        client = await asyncio.to_thread(ax._make_client, account_id)
        total = len(_SELLING_KEYWORDS)
        done = 0
        matches = 0
        _kw_progress[account_id] = {"total": total, "done": 0, "matches": 0}
        for kw in _SELLING_KEYWORDS:
            r = await _harvest_query(account_id, kw, client=client)
            matches += int(r.get("count") or 0)
            done += 1
            _kw_progress[account_id] = {"total": total, "done": done, "matches": matches}
            await asyncio.sleep(0.1)
        log.info("harvest_keywords done account=%s matches=%s", account_id, matches)
    except Exception:  # noqa: BLE001
        log.exception("harvest_keywords failed account=%s", account_id)
    finally:
        _kw_running.discard(account_id)


@router.post("/admin/vault-ai/harvest-keywords")
async def harvest_keywords(payload: dict = Body(...)) -> dict[str, Any]:
    account_id = str(payload.get("account_id") or "")
    assert_account_owned(account_id)
    if account_id in _kw_running:
        raise HTTPException(status_code=409, detail={"error": "harvest_already_running"})
    _kw_running.add(account_id)
    asyncio.create_task(_run_harvest_keywords(account_id))
    return {"account_id": account_id, "status": "running", "keywords": len(_SELLING_KEYWORDS)}


@router.get("/admin/vault-ai/harvest-keywords/status")
async def harvest_keywords_status(account_id: str = Query(...)) -> dict[str, Any]:
    assert_account_owned(account_id)
    return {
        "account_id": account_id,
        "running": account_id in _kw_running,
        "progress": _kw_progress.get(account_id),
    }


# ── Review queue (S2) ──────────────────────────────────────────────
# Pending AI-proposed ACTIONS (folder / ppv / reminder) awaiting operator
# approval. Approval flips status pending→approved ONLY — no OF write, no send,
# no vault mutation (contract §2 correction #2: approved ≠ applied ≠ armed). A
# separate consumer later does the OF work and flips approved→applied.
#
# On approve, we recompute the world-view captured in baseline_json (media
# hashes / folder membership / config hash) for the keys the producer stored.
# A mismatch ⇒ the proposal is `stale`, left `pending`, and the id is returned
# under "stale" instead of "approved". No global approve — approve is scoped
# per-kind or per-ids (contract §2).


from db.models import VaultAiReviewItem  # noqa: E402
import vault_ai_baseline as vab  # noqa: E402

_REVIEW_KINDS = ("folder", "ppv", "reminder")


def _review_item_view(row: VaultAiReviewItem) -> dict[str, Any]:
    """Wire shape for a review row (contract §2 `item`)."""
    return {
        "id": row.id,
        "kind": row.kind,
        "status": row.status,
        "payload": load_json(row.payload_json, {}),
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def _normalize_for_compare(value: Any) -> Any:
    """Order-insensitive normalize so stored vs recomputed compare as sets/maps.
    Lists become sorted, dicts key-sorted with values recursed; leaves are
    coerced to str so an int '12' matches a str '12' (JSON round-trip drift)."""
    if isinstance(value, dict):
        return {str(k): _normalize_for_compare(value[k]) for k in sorted(value, key=str)}
    if isinstance(value, (list, tuple, set)):
        return sorted(
            (_normalize_for_compare(v) for v in value),
            key=lambda v: json.dumps(v, sort_keys=True, default=str),
        )
    return value


async def _current_baseline_view(
    session, account_id: str, stored: dict[str, Any],
) -> dict[str, Any]:
    """Recompute the world-view keys present in `stored`. Delegates to the shared
    `vault_ai_baseline` so this checker and the producer (`vault_ai_service`)
    compute baselines from ONE implementation — see that module's header for the
    drift bug this prevents (producer/checker hashing the config two ways ⇒ every
    approval came back `stale`)."""
    return await vab.current_baseline_view(session, account_id, stored)


def _is_stale(stored_baseline_json: str | None, current: dict[str, Any]) -> bool:
    """True if the stored baseline no longer matches the current world for any
    key the producer captured. A NULL/empty baseline means the producer opted
    out of staleness detection — always fresh."""
    if not stored_baseline_json:
        return False
    stored = load_json(stored_baseline_json, None)
    if not isinstance(stored, dict) or not stored:
        return False
    trimmed_stored = {k: stored[k] for k in current.keys() if k in stored}
    return _normalize_for_compare(trimmed_stored) != _normalize_for_compare(current)


@router.get("/admin/vault-ai/review")
async def list_review_items(account_id: str = Query(...)) -> dict[str, Any]:
    """Pending review items for the account, grouped by kind."""
    assert_account_owned(account_id)
    grouped: dict[str, list[dict[str, Any]]] = {k: [] for k in _REVIEW_KINDS}
    async with get_session() as s:
        rows = (
            await s.execute(
                select(VaultAiReviewItem)
                .where(
                    VaultAiReviewItem.account_id == account_id,
                    VaultAiReviewItem.status == "pending",
                )
                .order_by(VaultAiReviewItem.created_at.asc(), VaultAiReviewItem.id.asc())
            )
        ).scalars().all()
    for row in rows:
        bucket = grouped.get(row.kind)
        if bucket is None:
            continue
        bucket.append(_review_item_view(row))
    return grouped


async def _load_target_rows(
    session, account_id: str, kind: str | None, ids: list[int] | None,
) -> list[VaultAiReviewItem]:
    """Fetch pending rows for a section approve (kind) OR an explicit ids list.
    Rows scoped to `account_id` and `status='pending'` in both cases so nothing
    outside the operator's scope can be touched."""
    stmt = select(VaultAiReviewItem).where(
        VaultAiReviewItem.account_id == account_id,
        VaultAiReviewItem.status == "pending",
    )
    if ids is not None:
        stmt = stmt.where(VaultAiReviewItem.id.in_(ids))
    if kind is not None:
        stmt = stmt.where(VaultAiReviewItem.kind == kind)
    stmt = stmt.order_by(VaultAiReviewItem.id.asc())
    return list((await session.execute(stmt)).scalars().all())


def _parse_ids(payload: dict) -> list[int] | None:
    raw = payload.get("ids")
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise HTTPException(status_code=400, detail={"error": "ids_must_be_list"})
    out: list[int] = []
    for v in raw:
        try:
            out.append(int(v))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail={"error": "ids_must_be_ints"})
    return out


@router.post("/admin/vault-ai/review/approve")
async def approve_review_items(payload: dict = Body(...)) -> dict[str, Any]:
    """Flip pending review rows to `approved`. Approval mutates NO OF state and
    triggers NO send — a downstream consumer applies later. On approve, each
    row's baseline_json is compared against the recomputed current world; a
    mismatch leaves the row `pending` and returns its id under `stale` instead
    of `approved` (contract §2).

    Body: `{account_id, kind}` for a section approve OR `{account_id, ids: [...]}`
    for an explicit set. Global approve is deliberately unsupported.
    """
    account_id = str(payload.get("account_id") or "")
    assert_account_owned(account_id)
    if not account_id:
        raise HTTPException(status_code=400, detail={"error": "account_id_required"})
    kind = payload.get("kind")
    ids = _parse_ids(payload)
    if kind is None and ids is None:
        raise HTTPException(status_code=400, detail={"error": "kind_or_ids_required"})
    if kind is not None:
        kind = str(kind)
        if kind not in _REVIEW_KINDS:
            raise HTTPException(status_code=400, detail={"error": "unknown_kind", "kind": kind})

    approved: list[int] = []
    stale: list[int] = []
    now = datetime.utcnow()
    async with get_session() as s:
        rows = await _load_target_rows(s, account_id, kind, ids)
        for row in rows:
            stored_baseline = load_json(row.baseline_json, None) or {}
            current = await _current_baseline_view(s, account_id, stored_baseline if isinstance(stored_baseline, dict) else {})
            if _is_stale(row.baseline_json, current):
                stale.append(row.id)
                continue
            row.status = "approved"
            row.resolved_at = now
            approved.append(row.id)
        await s.commit()
    return {"approved": approved, "stale": stale}


@router.post("/admin/vault-ai/review/reject")
async def reject_review_items(payload: dict = Body(...)) -> dict[str, Any]:
    """Flip pending review rows to `rejected`. No OF/vault mutation; the
    proposal is simply discarded so the queue clears."""
    account_id = str(payload.get("account_id") or "")
    assert_account_owned(account_id)
    if not account_id:
        raise HTTPException(status_code=400, detail={"error": "account_id_required"})
    ids = _parse_ids(payload)
    if not ids:
        raise HTTPException(status_code=400, detail={"error": "ids_required"})

    rejected: list[int] = []
    now = datetime.utcnow()
    async with get_session() as s:
        rows = await _load_target_rows(s, account_id, None, ids)
        for row in rows:
            row.status = "rejected"
            row.resolved_at = now
            rejected.append(row.id)
        await s.commit()
    return {"rejected": rejected}


# ── Edit-and-lock a mirror item (S10) ──────────────────────────────
# Operator edits to an item's description / tags / tier / caption / script.
# Per contract §3 write rule, each edited field is written into
# operator_overrides_json AND its name added to locked_fields_json AND projected
# into the legacy compat column of the same name — so a later forced describe
# rerun's is_locked() skips it (see _describe_one's locked-skip). We read the
# result back ONLY through effective_value(), never the raw column.

from vault_ai_effective import effective_value, locked_fields as _locked_fields  # noqa: E402

# The only fields the editor may override+lock. `tags` is stored as a JSON list;
# every other field is free text.
_EDITABLE_FIELDS = (
    "description", "tags", "explicitness_tier", "suggested_caption", "suggested_script",
    # The exposure flags, editable wherever an item is editable rather than
    # only inside the review modal. They decide the folder, the price and
    # whether something may be mass-sent, so an operator who spots a wrong one
    # while doing something else should be able to fix it there and then. Same
    # override+lock contract as `description`: the edit wins at read time and a
    # forced re-flag skips it.
    *vault_ai_brief.VIS_REGIONS,
    *vault_ai_brief.OVER_KEYS.values(),
    # Operator-set PPV price for this media, in CENTS. The AI never writes this
    # (vision classifies explicitness_tier instead — PLAN correction #3); it is
    # purely an operator choice, and the Composer prices an attachment set at the
    # MAX suggested price across the selection.
    "suggested_price_cents",
)


def _normalize_tags(v: Any) -> list[str]:
    """Accept a list OR a comma/newline-separated string → deduped, lower-cased,
    trimmed tag list (cap 12, matching the describe path)."""
    if isinstance(v, str):
        parts = _re.split(r"[,\n]", v)
    elif isinstance(v, (list, tuple)):
        parts = [str(x) for x in v]
    else:
        parts = []
    out: list[str] = []
    seen: set[str] = set()
    for p in parts:
        t = str(p).strip().lower()
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out[:12]


def _item_edit_view(item: VaultItem, locked: list[str]) -> dict[str, Any]:
    """Wire shape for an edited mirror row — every editable field read through
    effective_value() so the operator sees the override that just won, not a raw
    column. `video_description` is surfaced raw for the FE (S9 overlay reads it)."""
    return {
        "media_id": item.media_id,
        "id": item.media_id,
        "kind": item.kind,
        "description": effective_value(item, "description"),
        "video_description": item.video_description,
        "tags": effective_value(item, "tags", default=[]),
        "explicitness_tier": effective_value(item, "explicitness_tier"),
        "suggested_caption": effective_value(item, "suggested_caption"),
        "suggested_script": effective_value(item, "suggested_script"),
        "suggested_price_cents": effective_value(item, "suggested_price_cents"),
        "describe_status": item.describe_status,
        "locked_fields": locked,
    }


@router.patch("/admin/vault-ai/items/{media_id}")
async def edit_item(media_id: int, payload: dict = Body(...)) -> dict[str, Any]:
    """Override + lock one or more editable fields on a mirror item.

    Body: `{account_id, fields: {description?, tags?, explicitness_tier?,
    suggested_caption?, suggested_script?}}`. Each supplied field is written to
    `operator_overrides_json`, added to `locked_fields_json`, and projected into
    the same-named legacy column (contract §3 + §6c). Returns the updated row
    (read via effective_value) and the full locked-field list.
    """
    account_id = str(payload.get("account_id") or "")
    assert_account_owned(account_id)
    if not account_id:
        raise HTTPException(status_code=400, detail={"error": "account_id_required"})
    fields = payload.get("fields")
    if not isinstance(fields, dict) or not fields:
        raise HTTPException(status_code=400, detail={"error": "fields_required"})
    # Only known fields may be edited; ignore anything else the client sends.
    edits = {k: fields[k] for k in _EDITABLE_FIELDS if k in fields}
    if not edits:
        raise HTTPException(status_code=400, detail={"error": "no_editable_fields"})

    async with get_session() as s:
        item = await s.get(VaultItem, (account_id, media_id))
        if item is None:
            raise HTTPException(
                status_code=404, detail={"error": "not_in_mirror", "media_id": media_id}
            )
        overrides = load_json(item.operator_overrides_json, {}) or {}
        locked = set(load_json(item.locked_fields_json, []) or [])
        # compat-column projection per field (same-named legacy column).
        col_vals: dict[str, Any] = {}
        flag_edits: dict[str, Any] = {}
        for field, raw in edits.items():
            if field == "tags":
                ov: Any = _normalize_tags(raw)
                col_vals["tags"] = json.dumps(ov)
            elif field == "suggested_price_cents":
                # Cents, non-negative. Blank/None clears it back to "no price".
                if raw is None or (isinstance(raw, str) and not raw.strip()):
                    ov = None
                else:
                    try:
                        ov = max(0, int(float(raw)))
                    except (TypeError, ValueError):
                        raise HTTPException(
                            status_code=400,
                            detail={"error": "bad_price", "field": field},
                        )
                col_vals[field] = ov
            elif field in vault_ai_brief.VIS_REGIONS:
                # Enum-constrained, and stored in `ai_fields_json` rather than
                # a column of its own — so it is written through the same path
                # the review modal uses instead of being projected here.
                if raw not in vault_ai_brief.VIS_STATES:
                    raise HTTPException(
                        status_code=400,
                        detail={"error": "bad_region_state", "field": field,
                                "allowed": list(vault_ai_brief.VIS_STATES)})
                ov = raw
                flag_edits[field] = raw
            elif field in vault_ai_brief.OVER_KEYS.values():
                ov = str(raw).strip()[:60] if raw else None
                flag_edits[field] = ov
            elif raw is None:
                ov = None
                col_vals[field] = None
            else:
                ov = str(raw)
                col_vals[field] = ov
            overrides[field] = ov       # operator override (locked or not — read-time wins)
            locked.add(field)            # lock: a describe rerun's is_locked() skips it
        if flag_edits:
            merged = load_json(item.ai_fields_json, {}) or {}
            for k, v in flag_edits.items():
                if v is None:
                    merged.pop(k, None)
                else:
                    merged[k] = v
            col_vals["ai_fields_json"] = json.dumps(merged, ensure_ascii=False,
                                                    default=str)
        col_vals["operator_overrides_json"] = json.dumps(overrides, ensure_ascii=False)
        col_vals["locked_fields_json"] = json.dumps(sorted(locked))
        await s.execute(
            update(VaultItem)
            .where(VaultItem.account_id == account_id, VaultItem.media_id == media_id)
            .values(**col_vals)
        )
        await s.commit()
        # Re-read through the ORM so effective_value resolves the fresh columns.
        item = await s.get(VaultItem, (account_id, media_id))
        await s.refresh(item)
        locked_out = sorted(_locked_fields(item))
        view = _item_edit_view(item, locked_out)
    return {"item": view, "locked": locked_out}


@router.post("/admin/vault-ai/items/{media_id}/hide")
async def hide_item(media_id: int, payload: dict = Body(...)) -> dict[str, Any]:
    """Remove ONE media from the real OF vault (operator-confirmed).

    Same mechanism the duplicates sweep uses — OF's own "Remove selected items
    from vault" (`PUT /vault/media/hidden`) via `of_client.hide_vault_media`: the
    media is HIDDEN, not destroyed, so anything already attached to a sent PPV or
    a live post keeps working. **OF exposes no unhide**, so the UI must confirm
    before calling this.

    Unlike /duplicates/hide there is no cluster to re-derive — the operator is
    pointing at one specific item — so ownership is the only guard.
    """
    account_id = str(payload.get("account_id") or "")
    assert_account_owned(account_id)
    if not account_id:
        raise HTTPException(status_code=400, detail={"error": "account_id_required"})

    client = await asyncio.to_thread(ax._make_client, account_id)
    try:
        await asyncio.to_thread(client.hide_vault_media, [int(media_id)])
    except Exception as e:  # noqa: BLE001
        log.warning("vault item hide failed account=%s media=%s",
                    account_id, media_id, exc_info=True)
        raise HTTPException(status_code=502, detail={
            "error": "of_hide_failed", "detail": str(e)[:300],
            "media_id": int(media_id)})

    # Mirror the removal locally so the grid, the picker, the describe sweep and
    # every automation stop seeing it immediately — rather than waiting for the
    # next collect to notice it's gone.
    now = datetime.utcnow()
    async with get_session() as s:
        await s.execute(
            update(VaultItem)
            .where(VaultItem.account_id == account_id,
                   VaultItem.media_id == int(media_id))
            .values(removed_at=now)
        )
        await s.commit()
    try:
        await vault_cache.invalidate(account_id)
    except Exception:  # noqa: BLE001
        pass
    log.info("vault item hidden account=%s media=%s", account_id, media_id)
    return {"ok": True, "media_id": int(media_id), "hidden": True}
