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
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query
from fastapi.responses import FileResponse, Response
from sqlalchemy import case, delete, func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

import automation_executor as ax  # _make_client seam (same one automations use)
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

# On-disk 300px thumbnail cache. OF CDN urls are IP+time-signed and EXPIRE, so a
# mirror row's stored url 403s later (the broken-tile bug). We download the thumb
# bytes once through the account's client and serve them from disk forever after —
# permanent + instant, no signature dependence.
_THUMB_DIR = Path(os.environ.get("VAULT_THUMB_DIR", "/tmp/of-relay-vault-thumbs"))
# Poster-frame cache for DRM videos. Progressive videos get an ffmpeg storyboard
# (server-side scrub cache); DRM (SAMPLE-AES) clips can't be sliced, so OF's own
# pre-extracted poster frames are the only hover preview — cached here so a DRM
# hover is instant from disk instead of a ~0.7s cold OF fetch.
_POSTER_DIR = Path(os.environ.get("VAULT_POSTER_DIR", "/tmp/of-relay-vault-posters"))


def _thumb_path(account_id: str, media_id: int) -> Path:
    return _THUMB_DIR / account_id / f"{media_id}.jpg"


def _poster_path(account_id: str, media_id: int, idx: int) -> Path:
    return _POSTER_DIR / account_id / f"{media_id}_{idx}.jpg"


def _fetch_thumb_sync(client: Any, url: str) -> bytes | None:
    try:
        r = client.http.get(url, timeout=client.timeout_s)
        if r.status_code == 200 and r.content:
            return r.content
    except Exception:  # noqa: BLE001
        pass
    return None


def _parse_iso(v: Any) -> datetime | None:
    if not v or not isinstance(v, str):
        return None
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:  # noqa: BLE001
        return None


def _thumb_of(files: dict | None) -> str | None:
    if not isinstance(files, dict):
        return None
    for k in ("thumb", "squarePreview", "preview", "full"):
        f = files.get(k)
        if isinstance(f, dict) and f.get("url"):
            return f["url"]
    return None


def _video_url(m: dict) -> str | None:
    """Signed progressive mp4 URL for a video (240p → 720p → full). None for
    DRM-only videos (no sliceable source) — those can't be warmed."""
    vs = m.get("videoSources")
    if isinstance(vs, dict):
        for k in ("240", "720"):
            if vs.get(k):
                return vs[k]
    files = m.get("files")
    full = files.get("full") if isinstance(files, dict) else None
    if isinstance(full, dict) and full.get("url"):
        return full["url"]
    return None


def _warm_one_sync(client: Any, url: str, dur: float) -> str:
    """Extract + cache the 12-frame storyboard for ONE video, if not already
    on disk. Incremental: `_storyboard_all_frames_present` short-circuits a
    video we've already warmed (so re-collect only does NEW videos). Reuses
    the relay's own storyboard helpers; downloads through the account's OF
    client so the URL's source-IP signature matches."""
    import server as srv  # lazy: avoid the server↔vault_ai_api import cycle

    h = srv._video_path_hash(url)
    if srv._storyboard_all_frames_present(h):
        return "cached"
    dest = srv._STORYBOARD_DIR / h
    src_path = dest / "source.tmp"
    try:
        dest.mkdir(parents=True, exist_ok=True)
        r = client.http.get(url, timeout=client.timeout_s, stream=True)
        if r.status_code not in (200, 206):
            return "dl_fail"
        with src_path.open("wb") as f:
            for chunk in r.iter_content(chunk_size=256 * 1024):
                if chunk:
                    f.write(chunk)
        try:
            r.close()
        except Exception:  # noqa: BLE001
            pass
        ok = srv._storyboard_extract_batch(
            src_path, dest, srv._ALL_FRAME_INDICES, max(0.1, dur or 1.0)
        )
        return "warmed" if ok else "extract_fail"
    except Exception:  # noqa: BLE001
        log.warning("warm storyboard failed url=%s", url[:100], exc_info=True)
        return "error"
    finally:
        try:
            src_path.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass


def _row_values(account_id: str, m: dict, run_id: int) -> dict[str, Any] | None:
    """Mirror-field values for one OF media dict. None if it has no id."""
    mid = m.get("id")
    if mid is None:
        return None
    files = m.get("files") if isinstance(m.get("files"), dict) else {}
    kind = m.get("type") or "photo"
    dur = m.get("duration")
    now = datetime.utcnow()
    # OF custom-folder membership: listStates[].hasMedia is the per-item "is this
    # media in that list" flag (Posts.hasMedia aligns with top-level hasPosts).
    of_folders = [
        ls.get("id") for ls in (m.get("listStates") or [])
        if isinstance(ls, dict) and ls.get("type") == "custom" and ls.get("hasMedia") and ls.get("id")
    ]
    return {
        "account_id": account_id,
        "media_id": int(mid),
        "kind": str(kind),
        "duration_seconds": int(dur) if isinstance(dur, (int, float)) and dur else None,
        "width": m.get("width") if isinstance(m.get("width"), int) else None,
        "height": m.get("height") if isinstance(m.get("height"), int) else None,
        "thumb_url": _thumb_of(files),
        "full_url": (files.get("full") or {}).get("url") if isinstance(files.get("full"), dict) else None,
        "raw_json": json.dumps(m, ensure_ascii=False, default=str),
        "created_at": _parse_iso(m.get("createdAt")) or now,
        "updated_at_of": m.get("updatedAt") if isinstance(m.get("updatedAt"), str) else None,
        "last_seen_run_id": run_id,
        "updated_at": now,
        "of_folder_ids": json.dumps(of_folders),
        # base searchable blob — describe enriches this later; on conflict we
        # DON'T overwrite it (preserve describe output).
        "search_text": str(kind).lower(),
        "tags": "[]",
    }


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
                    vals = _row_values(account_id, m, run_id)
                    if vals is None:
                        continue
                    total += 1
                    if vals.get("thumb_url"):
                        thumbs.append((vals["media_id"], vals["thumb_url"]))
                    if (m.get("type") == "video"):
                        vurl = _video_url(m)
                        if vurl:
                            videos.append((vurl, float(m.get("duration") or 0)))
                        else:
                            # DRM: no sliceable mp4 → cache OF poster frames so
                            # the hover preview serves from disk, not a cold fetch.
                            pf = _video_poster_frames(m)
                            if pf:
                                drm_posters.append((vals["media_id"], pf))
                    # Preserve AI / operator / describe fields on conflict —
                    # only refresh the mirror bookkeeping.
                    refresh = {
                        k: vals[k] for k in (
                            "kind", "duration_seconds", "width", "height",
                            "thumb_url", "full_url", "raw_json", "updated_at_of",
                            "last_seen_run_id", "updated_at", "of_folder_ids",
                        )
                    }
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
        # Cache 300px thumbs to disk (incremental: skip ones already cached).
        tdone = 0
        _warm_progress[account_id] = {"phase": "thumbs", "thumbs": 0, "thumb_total": len(thumbs)}
        for mid, turl in thumbs:
            tp = _thumb_path(account_id, mid)
            if not tp.is_file():
                data = await asyncio.to_thread(_fetch_thumb_sync, client, turl)
                if data:
                    try:
                        tp.parent.mkdir(parents=True, exist_ok=True)
                        tp.write_bytes(data)
                    except Exception:  # noqa: BLE001
                        pass
            tdone += 1
            if tdone % 10 == 0:
                _warm_progress[account_id] = {"phase": "thumbs", "thumbs": tdone, "thumb_total": len(thumbs)}
        # Cache DRM poster frames to disk (incremental). DRM clips can't be
        # ffmpeg-storyboarded, so these OF frames are the hover preview — caching
        # them makes a DRM hover instant instead of a cold ~0.7s OF fetch.
        for mid, frames in drm_posters:
            for idx, furl in enumerate(frames):
                pp = _poster_path(account_id, mid, idx)
                if pp.is_file():
                    continue
                data = await asyncio.to_thread(_fetch_thumb_sync, client, furl)
                if data:
                    try:
                        pp.parent.mkdir(parents=True, exist_ok=True)
                        pp.write_bytes(data)
                    except Exception:  # noqa: BLE001
                        pass
        async with get_session() as s:
            run = await s.get(VaultCacheRun, run_id)
            if run:
                run.phase = "warming"
                await s.commit()
        warmed = 0
        _warm_progress[account_id] = {"phase": "warming", "warmed": 0, "warm_total": len(videos)}
        for vurl, vdur in videos:
            try:
                await asyncio.to_thread(_warm_one_sync, client, vurl, vdur)
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
    async with get_session() as s:
        run = VaultCacheRun(account_id=account_id, status="running", phase="media")
        s.add(run)
        await s.commit()
        run_id = run.id
    asyncio.create_task(_run_collect(account_id, run_id))
    return {"account_id": account_id, "run_id": run_id, "status": "running"}


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
            "status": row.status,
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
            "id": last.id, "status": last.status, "total_seen": last.total_seen,
            "finished_at": last.finished_at.isoformat() if last.finished_at else None,
        },
    }


@router.get("/admin/vault-ai/thumb")
async def vault_thumb(account_id: str = Query(...), media_id: int = Query(...)):
    """Serve the cached 300px thumbnail from disk (permanent, signature-free).
    On a miss, fetch it through the account's client, cache it, then serve —
    so tiles never break on an expired OF url."""
    assert_account_owned(account_id)
    p = _thumb_path(account_id, media_id)
    headers = {"Cache-Control": "public, max-age=604800"}
    if p.is_file():
        return FileResponse(p, media_type="image/jpeg", headers=headers)
    async with get_session() as s:
        item = await s.get(VaultItem, (account_id, media_id))
    if item is None:
        raise HTTPException(status_code=404, detail="not_in_mirror")
    raw = _load_json(item.raw_json, {}) or {}
    files = raw.get("files") if isinstance(raw.get("files"), dict) else {}
    url = _thumb_of(files) or item.thumb_url
    if not url:
        raise HTTPException(status_code=404, detail="no_thumb")
    client = await asyncio.to_thread(ax._make_client, account_id)
    data = await asyncio.to_thread(_fetch_thumb_sync, client, url)
    if not data:
        raise HTTPException(status_code=404, detail="fetch_failed")
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    except Exception:  # noqa: BLE001
        pass
    return Response(content=data, media_type="image/jpeg", headers=headers)


def _is_of_cdn(u: str) -> bool:
    """Only fetch+cache poster frames from OnlyFans' own CDN — a caller-supplied
    `u` must not be an open proxy to arbitrary hosts."""
    try:
        from urllib.parse import urlparse
        host = (urlparse(u).hostname or "").lower()
    except Exception:  # noqa: BLE001
        return False
    return host.endswith("onlyfans.com") or "cdn" in host and "onlyfans" in host


@router.get("/admin/vault-ai/poster")
async def vault_poster(
    account_id: str = Query(...), media_id: int = Query(...), i: int = Query(0, ge=0),
    u: str | None = Query(None),
):
    """Serve a cached DRM video poster frame from disk (permanent, signature-free,
    media-id keyed so it survives OF url re-signs). Self-heals on a miss: resolve
    the frame url from the mirror if collected, else from the caller-supplied OF
    url `u` — so hovering an UN-collected DRM video still caches to the permanent
    store (the cache fills as you browse; Collect just pre-warms the whole vault)."""
    assert_account_owned(account_id)
    p = _poster_path(account_id, media_id, i)
    headers = {"Cache-Control": "public, max-age=604800"}
    if p.is_file():
        return FileResponse(p, media_type="image/jpeg", headers=headers)
    # Prefer the mirror's (fresh) url; fall back to the caller's OF url.
    url: str | None = None
    async with get_session() as s:
        item = await s.get(VaultItem, (account_id, media_id))
    if item is not None:
        frames = _video_poster_frames(_load_json(item.raw_json, {}) or {})
        if i < len(frames):
            url = frames[i]
    if url is None and u and _is_of_cdn(u):
        url = u
    if not url:
        raise HTTPException(status_code=404, detail="no_poster_frame")
    client = await asyncio.to_thread(ax._make_client, account_id)
    data = await asyncio.to_thread(_fetch_thumb_sync, client, url)
    if not data:
        raise HTTPException(status_code=404, detail="fetch_failed")
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    except Exception:  # noqa: BLE001
        pass
    return Response(content=data, media_type="image/jpeg", headers=headers)


def _overlay(item: VaultItem, manual_order: int | None = None) -> dict[str, Any]:
    try:
        base = json.loads(item.raw_json) if item.raw_json else {"id": item.media_id, "type": item.kind}
    except Exception:  # noqa: BLE001
        base = {"id": item.media_id, "type": item.kind}
    base["_ai"] = {
        "describe_status": item.describe_status,
        "description": item.description,
        "video_description": item.video_description,
        "tags": _load_json(item.tags, []),
        "explicitness_tier": item.explicitness_tier,
        "story_suitable": item.story_suitable,
        "tip_vault_flag": item.tip_vault_flag,
        "suggested_caption": item.suggested_caption,
        "suggested_price_cents": item.suggested_price_cents,
        "manual_order": item.manual_order if manual_order is None else manual_order,
        "review_state": item.review_state,
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
        m = _load_json(raw, {}) or {}
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


def _load_json(v: str | None, default: Any) -> Any:
    if not v:
        return default
    try:
        return json.loads(v)
    except Exception:  # noqa: BLE001
        return default


# ── Describe (Qwen3-VL vision) ──────────────────────────────────────

import base64  # noqa: E402
import re as _re  # noqa: E402

import llm_client  # noqa: E402
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

_REFUSAL_MARKERS = ("i can't", "i cannot", "i'm unable", "cannot assist", "content policy",
                    "i'm sorry", "not able to", "against my")


def _is_refusal(text: str) -> bool:
    low = (text or "").strip().lower()
    return not low or (len(low) < 40 and any(m in low for m in _REFUSAL_MARKERS))


def _img_data_url(b: bytes) -> str:
    return "data:image/jpeg;base64," + base64.b64encode(b).decode()


def _video_poster_frames(raw: dict) -> list[str]:
    """OF's own pre-extracted poster-frame URLs for a video
    (`files.preview.options[]`). For SAMPLE-AES DRM clips — which ffmpeg can't
    slice — these are the ONLY renderable representation (same frames the chat
    VaultPicker slideshows). Falls back to the single poster thumb."""
    files = raw.get("files") or {}
    prev = files.get("preview") or {}
    out = [o.get("url") for o in (prev.get("options") or []) if isinstance(o, dict) and o.get("url")]
    if not out:
        one = _thumb_of(files)
        if one:
            out = [one]
    return out


async def _download_frames(client: Any, urls: list[str]) -> list[str]:
    """Download each frame URL through the account's OF client (source-IP
    signing) and return them as base64 data-URLs. Skips failures."""
    out: list[str] = []
    for u in urls:
        try:
            resp = await asyncio.to_thread(lambda u=u: client.http.get(u, timeout=client.timeout_s))
            if resp.status_code == 200 and resp.content:
                out.append(_img_data_url(resp.content))
        except Exception:  # noqa: BLE001
            log.warning("poster frame fetch failed", exc_info=True)
    return out


async def _collect_images(account_id: str, item: VaultItem, raw: dict) -> list[str]:
    """Return base64 data-URLs to send the model. Photos → the thumb; videos →
    up to 3 warmed storyboard frames (warmed on demand if missing). Downloaded
    through the account's OF client so the URL's source-IP signature matches."""
    client = await asyncio.to_thread(ax._make_client, account_id)
    if item.kind == "video":
        import server as srv  # lazy
        url = _video_url(raw)
        if url:
            # Progressive/sliceable → ffmpeg storyboard frames.
            h = srv._video_path_hash(url)
            if not srv._storyboard_all_frames_present(h):
                await asyncio.to_thread(_warm_one_sync, client, url, float(raw.get("duration") or 0))
            urls: list[str] = []
            for i in (2, 6, 10):
                p = srv._storyboard_frame_path(h, i)
                if p.is_file():
                    urls.append(_img_data_url(p.read_bytes()))
            if urls:
                return urls
        # DRM-only (no sliceable mp4) or storyboard produced nothing → fall back
        # to OF's pre-extracted poster frames. OF DOES serve these even for DRM,
        # so we can still describe/tag the clip instead of giving up (blocked_drm).
        return await _download_frames(client, _video_poster_frames(raw)[:3])
    # photo / gif
    files = raw.get("files") or {}
    url = _thumb_of(files) or item.thumb_url or item.full_url
    if not url:
        return []
    try:
        resp = await asyncio.to_thread(lambda: client.http.get(url, timeout=client.timeout_s))
        if resp.status_code == 200 and resp.content:
            return [_img_data_url(resp.content)]
    except Exception:  # noqa: BLE001
        log.warning("describe image fetch failed media=%s", item.media_id, exc_info=True)
    return []


def _parse_describe(text: str) -> dict[str, Any]:
    m = _re.search(r"\{.*\}", text or "", _re.DOTALL)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except Exception:  # noqa: BLE001
        return {}


async def _describe_one(account_id: str, media_id: int, model: str = "qwen3-vl-30b") -> dict[str, Any]:
    async with get_session() as s:
        item = await s.get(VaultItem, (account_id, media_id))
    if item is None:
        raise HTTPException(status_code=404, detail={"error": "not_in_mirror", "media_id": media_id})
    raw = _load_json(item.raw_json, {}) or {}
    images = await _collect_images(account_id, item, raw)
    if not images:
        async with get_session() as s:
            await s.execute(
                update(VaultItem)
                .where(VaultItem.account_id == account_id, VaultItem.media_id == media_id)
                .values(describe_status="blocked_drm", describe_generated_at=datetime.utcnow())
            )
            await s.commit()
        return {"media_id": media_id, "ok": False, "status": "blocked_drm"}

    content = [{"type": "text", "text": _DESCRIBE_PROMPT}]
    content += [{"type": "image_url", "image_url": {"url": u}} for u in images]

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
            return {"media_id": media_id, "ok": False, "status": "capped", "detail": str(e)}
        except LLMError as e:
            return {"media_id": media_id, "ok": False, "status": "error", "detail": str(e)[:300]}
        used_model = attempt_model
        if not _is_refusal(result.content):
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
    search_text = (desc + " " + " ".join(tags)).lower()

    # Human edits win: never overwrite an effective field the operator locked.
    locked = set(_load_json(item.locked_fields_json, []))
    vals: dict[str, Any] = {
        "describe_status": "described",
        "describe_model": used_model,
        "describe_call_id": result.call_id if result else None,
        "describe_generated_at": datetime.utcnow(),
        "ai_fields_json": json.dumps(data, ensure_ascii=False, default=str),
        "frames_sampled": len(images) if is_video else None,
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
        "images_sent": len(images),
    }


@router.post("/admin/vault-ai/describe")
async def describe_one(payload: dict = Body(...)) -> dict[str, Any]:
    account_id = str(payload.get("account_id") or "")
    assert_account_owned(account_id)
    try:
        media_id = int(payload.get("media_id"))
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=400, detail={"error": "media_id_required"})
    model = str(payload.get("model") or "qwen3-vl-30b")
    return await _describe_one(account_id, media_id, model=model)


# ── Describe ALL (background sweep, gen_info-style) ─────────────────
_describe_running: set[str] = set()
_describe_progress: dict[str, dict[str, Any]] = {}
_DESCRIBE_CONCURRENCY = 4


async def _run_describe_all(account_id: str, force: bool) -> None:
    try:
        async with get_session() as s:
            stmt = select(VaultItem.media_id).where(
                VaultItem.account_id == account_id, VaultItem.removed_at.is_(None)
            )
            if not force:
                # Skip only items already successfully described. blocked_drm is
                # NO LONGER terminal — poster-frame fallback can now describe DRM
                # clips — so a normal "Describe all" retries them (and refused/
                # failed). Truly un-renderable clips just re-settle to blocked_drm.
                stmt = stmt.where(VaultItem.describe_status.isnot("described"))
            ids = [r[0] for r in (await s.execute(stmt)).all()]
        total = len(ids)
        done = 0
        capped = False
        _describe_progress[account_id] = {"total": total, "done": 0, "capped": False}
        sem = asyncio.Semaphore(_DESCRIBE_CONCURRENCY)

        async def _one(mid: int) -> None:
            nonlocal done, capped
            async with sem:
                if capped:
                    return
                res = await _describe_one(account_id, mid)
                if res.get("status") == "capped":
                    capped = True
                done += 1
                _describe_progress[account_id] = {"total": total, "done": done, "capped": capped}

        # Run in bounded chunks so a giant vault doesn't spawn thousands of tasks.
        for i in range(0, len(ids), 50):
            if capped:
                break
            await asyncio.gather(*(_one(m) for m in ids[i:i + 50]))
        log.info("describe_all done account=%s done=%s/%s capped=%s", account_id, done, total, capped)
    except Exception:  # noqa: BLE001
        log.exception("describe_all failed account=%s", account_id)
    finally:
        _describe_running.discard(account_id)
        # keep last progress snapshot for the status endpoint to report "done"


@router.post("/admin/vault-ai/describe-all")
async def describe_all(payload: dict = Body(...)) -> dict[str, Any]:
    account_id = str(payload.get("account_id") or "")
    assert_account_owned(account_id)
    if account_id in _describe_running:
        raise HTTPException(status_code=409, detail={"error": "describe_already_running"})
    force = bool(payload.get("force"))
    _describe_running.add(account_id)
    asyncio.create_task(_run_describe_all(account_id, force))
    return {"account_id": account_id, "status": "running"}


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
            terms = set(_load_json(item.of_terms, []))
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
