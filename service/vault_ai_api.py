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
import re
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


def _describe_image_of(files: dict | None) -> str | None:
    """The image variant to send the VISION model — not the same choice as the
    UI thumb.

    `thumb` is 300x300 and, because OF's stills are 3:4 portrait, it is a
    centre-CROP: the top and bottom of the picture are simply gone. Describe ran
    on that for its whole life, which is why it kept reporting `fully_nude` for
    shots whose only garment was a band of underwear at the bottom edge — the
    model never saw that part of the frame.

    `preview` (960x1280) keeps the real aspect ratio and ~13x the pixels, so an
    edge-of-frame waistband survives. `full` (2316x3088) is bigger still but
    costs proportionally more vision tokens for detail the task does not need.
    Ordered widest-useful first; `thumb` stays as the last resort for media that
    exposes nothing else.
    """
    if not isinstance(files, dict):
        return None
    for k in ("preview", "full", "squarePreview", "thumb"):
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
        "suggested_script": item.suggested_script,
    }
    # The V2 describe taxonomy (acts / clothing_state / beats / primary_folder /
    # …) lives ONLY in ai_fields_json — there are no columns for it. Without
    # this projection the browser receives just `description` + `tags` and the
    # other 21 fields the model produced are invisible, which is exactly what
    # made the vault look like it had lost them. Projected read-only: edits
    # still go through the override+lock path on the real columns.
    ai = _load_json(item.ai_fields_json, None)
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


import vault_dupes  # noqa: E402
import vault_ai_brief  # noqa: E402  (canonical FLAG_KEYS + flags_known)
import vault_scripts  # noqa: E402

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
) -> dict[str, Any]:
    """PREVIEW the folders the pipeline would create. Read-only, free, instant —
    no LLM, no OF traffic, nothing written. This is what the vault button shows
    before the operator confirms."""
    assert_account_owned(account_id)
    return await vault_scripts.plan_ai_folders(account_id, keep=keep)


async def _sync_of_list_membership(client, account_id: str, of_list_id: int) -> int:
    """Re-read ONE OF list and rewrite the mirror's per-item `of_folder_ids`.

    Without this, mirroring is invisible in the UI. Item→folder membership is
    written ONLY by a full collect (it is parsed from `listStates` on each media
    dict), so a folder we just pushed to OF exists there but no cached item
    claims to be in it — the picker asks "who is in list X", gets nothing, and
    honestly renders "No media in this filter" while the dropdown, which counts
    from OF, says 4. Real on OF, invisible locally.

    Membership is taken from OF rather than from the plan we just sent, because
    `add_media_to_vault_list` only ADDS: a list carries the union of every
    generation of the rules that has ever run. OF is the only thing that knows
    what is actually in there, so the mirror is made to agree with OF, not with
    our intent. Both directions are written — ids gained AND ids dropped — or
    a folder that shrinks would keep its stale members in the picker forever.
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
            has, should = int(of_list_id) in ids, int(item.media_id) in live
            if has == should:
                continue
            if should:
                ids.append(int(of_list_id))
            else:
                ids = [x for x in ids if x != int(of_list_id)]
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
    """
    by_name = {f["name"]: f for f in plan.get("folders") or []}
    client = await asyncio.to_thread(ax._make_client, account_id)
    out: list[dict[str, Any]] = []

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
            # Bind membership into the mirror NOW. A full re-collect would also
            # do it, but making the operator re-page the whole vault to see the
            # folder they just built is not a fix — and until they do, the grid
            # shows nothing.
            synced = await _sync_of_list_membership(client, account_id, int(of_list_id))
            out.append({**made, "of_list_id": int(of_list_id),
                        "of_added": len(media_ids), "mirror_synced": synced})
        except Exception as e:  # noqa: BLE001
            log.warning("OF mirror failed folder=%s name=%s",
                        made["folder_id"], made["name"], exc_info=True)
            out.append({**made, "of_error": str(e)[:200]})
    return out


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
    """
    account_id = str(payload.get("account_id") or "")
    assert_account_owned(account_id)
    if not payload.get("confirm"):
        raise HTTPException(status_code=400, detail={"error": "confirm_required"})
    keep = int(payload.get("keep") or 2)
    plan = await vault_scripts.plan_ai_folders(account_id, keep=keep)
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
    for region, over in (("breasts_vis", "over_breasts"), ("vulva_vis", "over_vulva")):
        if out.get(region) == "bare" and _garment_over(out, over):
            out[region] = "covered"
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
    marked the whole clip uncovered. 20 of 21 AriaFree clips folded to
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
    raw = _load_json(item.raw_json, {}) or {}
    want = _FLAGS_VIDEO_FRAMES if is_video else 1
    images = await _collect_images(account_id, item, raw, want=want)
    if not images:
        return {"media_id": media_id, "ok": False, "status": "no_image"}

    per_frame: list[dict[str, Any]] = []
    cost = 0
    result = None
    for img in images:
        content = [{"type": "text", "text": _FLAGS_PROMPT},
                   {"type": "image_url", "image_url": {"url": _shrink_data_url(img)}}]
        try:
            result = await llm_client.chat(
                model=model, messages=[{"role": "user", "content": content}],
                purpose="describe_flags", account_id=account_id, temperature=0.1,
            )
        except LLMCapExceeded as e:
            if not per_frame:
                return {"media_id": media_id, "ok": False, "status": "capped", "detail": str(e)}
            break  # keep what we already read rather than losing the whole clip
        except LLMError as e:
            if not per_frame:
                return {"media_id": media_id, "ok": False, "status": "error",
                        "detail": str(e)[:300]}
            break
        cost += int(result.cost_cents or 0)
        parsed = _parse_describe(result.content)
        if any(k in parsed for k in _FLAG_KEYS):
            per_frame.append(_ground_in_named_garment(parsed))

    if not per_frame:
        return {"media_id": media_id, "ok": False, "status": "unparsed",
                "raw": (result.content if result else "" or "")[:200]}

    data = _fold_clip_flags(per_frame)

    fields = _load_json(item.ai_fields_json, {}) or {}
    for key in _FLAG_KEYS:
        if key in data:
            fields[key] = data[key]
    # Persisted, not merely used and discarded. Two jobs: when a region says
    # `covered` this is WHY in the model's own words (a wrong flag is far
    # cheaper to diagnose from "white tank top" than from "covered"), and
    # `vault_scripts._is_nude` reads it as the direct evidence that she is
    # dressed. Taken from the FIRST frame, which is where a clip starts out.
    for over in ("over_breasts", "over_vulva"):
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


@router.post("/admin/vault-ai/flags-all")
async def flags_all(payload: dict = Body(...)) -> dict[str, Any]:
    """Run the cheap flags pass over an account's photos AND clips.

    Enrichment only: merges the three booleans into each row's existing
    `ai_fields_json` and touches nothing else. Skips items that already carry
    all of them unless `force`.

    Clips are included because the flags gate `AI-safe explicit`, the one folder
    that claims something is safe to mass-send. While clips went unflagged they
    fell into it by default — on AriaFree that meant a penetration clip and a
    dildo clip in the mass-safe folder.
    """
    account_id = str(payload.get("account_id") or "")
    assert_account_owned(account_id)
    force = bool(payload.get("force"))
    limit = int(payload.get("limit") or 0)

    async with get_session() as s:
        rows = (await s.execute(
            select(VaultItem.media_id, VaultItem.ai_fields_json)
            .where(VaultItem.account_id == account_id,
                   VaultItem.removed_at.is_(None),
                   VaultItem.kind.in_(("photo", "video")))
            .order_by(VaultItem.created_at.asc())
        )).all()

    todo = []
    for mid, fj in rows:
        f = _load_json(fj, {}) or {}
        # `flags_known`, not a key-presence test: a row carrying the superseded
        # boolean shape, or a region the model answered with something outside
        # the enum, has not really been flagged and must re-run.
        if force or not vault_ai_brief.flags_known(f):
            todo.append(int(mid))
    if limit:
        todo = todo[:limit]

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
            "flagged": done, "failed": failed, "cost_millicents": cost}


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
#     emit `nude` + `lingerie` + `topless` on the SAME item — 78% of Aria's rows
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
    "lingerie'. Pulled down to the thighs is 'pulled_down'. Only say fully_nude when no "
    "garment is on her body.\n"
    "2b. BEFORE you choose clothing_state, answer these two literally, by LOOKING at "
    "the frame rather than summarising the scene:\n"
    "    underwear_visible — is ANY garment visible anywhere on her body or pushed "
    "aside/down, however small? Waistband, strap, a thong string, lace at the hip, "
    "stockings — all count as TRUE.\n"
    "    genitals_covered — is her vulva covered by ANY fabric right now? Pushed aside "
    "so it is exposed = FALSE. Covered by a thong = TRUE. Out of frame or hidden by her "
    "pose/leg/hand rather than by fabric = FALSE (nothing is covering it).\n"
    "    These two govern: if underwear_visible is true you must NOT say fully_nude, "
    "whatever the overall impression. 'Nude except for a thong' is NOT fully_nude — it "
    "is pulled_aside or lingerie_on. This is the single most common error on this "
    "task, and a wrong answer here sells the wrong thing.\n"
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
# raising this to 9. Photos always send exactly 1.
_DESCRIBE_FRAMES = 9

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
# cheaper over AriaFree, about $0.001 for a 103-item vault.
#
# The image still has to keep its ASPECT RATIO. `files.thumb` is 300x300 and
# OF's stills are 3:4, so the thumb is a centre-CROP with the bottom of the
# frame missing — which is precisely where a waistband sits, and precisely why
# describe kept saying `fully_nude`. So we start from `preview` and downscale it
# ourselves: fewer pixels, whole picture. 448px on the long edge is the measured
# floor — 320px flips an item that 448 and 640 agree on, and 640 buys nothing.
_FLAGS_MAX_EDGE = 448

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
_FLAGS_PROMPT = (
    "You are indexing one photo from an adult creator's own vault. She owns it "
    "and has asked for it to be catalogued so it can be sorted. Report only "
    "what you can SEE in this frame.\n\n"
    "Reply with STRICT JSON only, no prose. Fill the two `over_` fields FIRST, "
    "then decide the states from them:\n"
    '{"over_breasts": "<what is over her chest, or null>",\n'
    ' "over_vulva":   "<what is over her crotch, or null>",\n'
    ' "breasts_vis": "...", "vulva_vis": "...", "anus_vis": "...",\n'
    ' "underwear_visible": true|false}\n\n'
    "over_breasts / over_vulva — NAME the thing, in two or three words: "
    '"white tank top", "black lace bra", "her right hand", "the bedsheet". Use '
    "null ONLY when there is genuinely nothing there and you are looking at "
    "skin, or when that part of her is not in the photo. Naming it first is the "
    "point of this step: it is much easier to say what she is wearing than to "
    "judge how exposed a picture is, and the second answer follows from the "
    "first.\n\n"
    "For vulva_vis, breasts_vis and anus_vis answer with EXACTLY one of these "
    "three words. The question is whether you can see SKIN there, not whether "
    "that part of her body is in the photo:\n"
    '  "bare"         — nothing at all is over it. You are seeing skin.\n'
    '  "covered"      — it is in the photo but something is over it: any '
    "clothing at all, her hand, her arm, her thigh, bedding, or an object.\n"
    '  "not_in_frame" — it is not in this photo. This includes her being '
    "turned away, shot from behind or lying face down, so that side of her "
    "body is simply not what the camera is looking at.\n\n"
    "THE MISTAKE TO AVOID: clothing that shows the SHAPE of her body is still "
    'clothing. A tank top, t-shirt, bra, bodysuit or swimsuit is "covered" no '
    "matter how tight it is, how much cleavage there is, how sheer it looks, or "
    'how sexy the photo is. "bare" means the garment is not there — pulled '
    "down, pulled aside, or never on. If you named anything in the matching "
    '`over_` field, the state MUST be "covered".\n\n'
    "Judge each region on its own. She is very often bare in one place and "
    "covered in another, and that difference is the whole point of this check. "
    "Do not let how explicit the picture FEELS decide the answers — a picture "
    "can be very explicit with nothing actually on show, and a plain one can "
    "have everything on show.\n\n"
    'vulva_vis — her vulva specifically. Panties or a thong over it is '
    '"covered". Pulled aside so the skin is exposed is "bare". Only her thighs '
    'or stomach in shot is "not_in_frame".\n'
    'breasts_vis — her breasts. A bra, top, bodysuit or bikini over them is '
    '"covered", and so is an arm folded across them. If she is photographed '
    'from behind or lying face down, answer "not_in_frame". "bare" needs '
    "actual bare breast, at minimum an exposed nipple or an uncovered breast.\n"
    'anus_vis — her anus specifically. A bare backside with the anus not on '
    'show is "covered", NOT "bare".\n\n'
    "underwear_visible — is ANY garment visible anywhere on her body, worn or "
    "pushed aside or pulled down, however small? A waistband, a strap, a thong "
    "string, a band of lace at the hip or at the very bottom edge of the "
    "picture, stockings — all TRUE. Look at the edges of the frame before you "
    "answer FALSE.\n"
)


def _shrink_data_url(data_url: str, max_edge: int = _FLAGS_MAX_EDGE) -> str:
    """Downscale a base64 data-URL to `max_edge` on its LONG side, preserving
    aspect. Returns the input unchanged if Pillow can't read it — a cheap pass
    must never take down the caller."""
    import io

    try:
        from PIL import Image

        _, _, b64 = data_url.partition(",")
        with Image.open(io.BytesIO(base64.b64decode(b64))) as im:
            im = im.convert("RGB")
            im.thumbnail((max_edge, max_edge), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=82)
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception:  # noqa: BLE001
        log.warning("flags shrink failed; sending full-size", exc_info=True)
        return data_url


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


def _spread(n_have: int, want: int) -> list[int]:
    """`want` frame indices spread evenly over `n_have` slots, endpoints
    included. 12 frames → 3 gives [1,5,10]; → 9 gives [0,1,3,4,5,6,8,9,11].
    Endpoints matter for `beats`: the model has to see how a clip ENDS."""
    if want >= n_have:
        return list(range(n_have))
    if want <= 1:
        return [n_have // 2]
    step = (n_have - 1) / (want - 1)
    return sorted({int(round(i * step)) for i in range(want)})


async def _collect_images(account_id: str, item: VaultItem, raw: dict,
                          want: int = _DESCRIBE_FRAMES) -> list[str]:
    """Return base64 data-URLs to send the model. Photos → the thumb; videos →
    up to `want` warmed storyboard frames (warmed on demand if missing).
    Downloaded through the account's OF client so the URL's source-IP signature
    matches."""
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
            for i in _spread(srv._STORYBOARD_FRAMES, want):
                p = srv._storyboard_frame_path(h, i)
                if p.is_file():
                    urls.append(_img_data_url(p.read_bytes()))
            if urls:
                return urls
        # DRM-only (no sliceable mp4) or storyboard produced nothing → fall back
        # to OF's pre-extracted poster frames. OF DOES serve these even for DRM,
        # so we can still describe/tag the clip instead of giving up (blocked_drm).
        return await _download_frames(client, _video_poster_frames(raw)[:want])
    # photo / gif
    files = raw.get("files") or {}
    url = _describe_image_of(files) or item.thumb_url or item.full_url
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
    raw = _load_json(item.raw_json, {}) or {}
    want = int(frames) if frames else _frames_for_duration(item.duration_seconds)
    images = await _collect_images(account_id, item, raw, want=want)
    if not images:
        async with get_session() as s:
            await s.execute(
                update(VaultItem)
                .where(VaultItem.account_id == account_id, VaultItem.media_id == media_id)
                .values(describe_status="blocked_drm", describe_generated_at=datetime.utcnow())
            )
            await s.commit()
        return {"media_id": media_id, "ok": False, "status": "blocked_drm"}

    prompt = _DESCRIBE_PROMPT if str(prompt_version) == "v1" else _DESCRIBE_PROMPT_V2
    content = [{"type": "text", "text": prompt}]
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
    locked = set(_load_json(item.locked_fields_json, []))
    # Stamp WHICH variant produced this row: a v1 row is a candidate for a later
    # v2 re-scan, and without the stamp "already described" is ambiguous.
    data = {**data, "_prompt_version": ("v1" if str(prompt_version) == "v1" else "v2"),
            "_frames": len(images)}
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
    return await _describe_one(
        account_id, media_id, model=model,
        prompt_version=str(payload.get("prompt_version") or "v2"),
        frames=payload.get("frames"),
    )


# ── Describe ALL (background sweep, gen_info-style) ─────────────────
_describe_running: set[str] = set()
_describe_progress: dict[str, dict[str, Any]] = {}
_DESCRIBE_CONCURRENCY = 4


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
                VaultItem.account_id == account_id, VaultItem.removed_at.is_(None)
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
                res = await _describe_one(account_id, mid, model=model,
                                          prompt_version=prompt_version)
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
                                       VaultItem.removed_at.is_(None))
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
    return {"account_id": account_id, "prompt_version": pv,
            "total": int(total or 0),
            "undescribed": int(undescribed or 0),
            "restage": int(stale or 0)}


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

import hashlib  # noqa: E402

from db.models import AccountAiConfig, VaultAiReviewItem  # noqa: E402
import vault_ai_baseline as vab  # noqa: E402

_REVIEW_KINDS = ("folder", "ppv", "reminder")


def _review_item_view(row: VaultAiReviewItem) -> dict[str, Any]:
    """Wire shape for a review row (contract §2 `item`)."""
    return {
        "id": row.id,
        "kind": row.kind,
        "status": row.status,
        "payload": _load_json(row.payload_json, {}),
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
    stored = _load_json(stored_baseline_json, None)
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
            stored_baseline = _load_json(row.baseline_json, None) or {}
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
        overrides = _load_json(item.operator_overrides_json, {}) or {}
        locked = set(_load_json(item.locked_fields_json, []) or [])
        # compat-column projection per field (same-named legacy column).
        col_vals: dict[str, Any] = {}
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
            elif raw is None:
                ov = None
                col_vals[field] = None
            else:
                ov = str(raw)
                col_vals[field] = ov
            overrides[field] = ov       # operator override (locked or not — read-time wins)
            locked.add(field)            # lock: a describe rerun's is_locked() skips it
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
