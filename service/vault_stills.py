"""
vault_stills.py — the PERMANENT, signature-free copy of every vault picture.

OF CDN urls are time-signed: the `Policy` on the url a sweep stored stops
resolving after a couple of days. Anything that renders a vault tile from a
stored url therefore goes blank on its own schedule. The fix is to hold the
BYTES, keyed by media id, and never depend on the url again:

    thumbs   300x300 square      the grid + picker tile
    images   ~960x1280 preview   the review UI (the square is a centre-crop —
                                 its top and bottom are simply gone)
    posters  frame i of a DRM    the hover slideshow for clips ffmpeg can't
             video               slice (progressive videos get a storyboard
                                 instead, cached elsewhere)

These stores are the ONLY copy that outlives OF's signature, so they must be
bind-mounted (see docker-compose.yml). Left on container-local disk they are
destroyed by every rebuild — and once they are gone AND the stored signature has
aged out, the picture is unrecoverable without re-asking OF.

Which is what `bytes_for` exists to do. On a disk miss it tries the url we have,
and if that fails it re-reads the ONE media from OF for a freshly signed url set,
writes the refreshed payload back to the mirror, and retries. That leaves exactly
one unbounded case — media OF has actually deleted — so it stamps the mirror row
`removed_at` when OF says "not found" and short-circuits on it afterwards. A dead
tile then costs one row read, not two doomed OF calls on every render.

`serve` is that ladder behind an HTTP response, and is the whole route body; the
three endpoints in vault_ai_api differ only in which store they read and which
url they pick out of a media dict. The DESCRIBE sweep calls `bytes_for` directly.
It used to fetch the stored url itself with no disk read and no re-sign, so on
2026-08-04 a sweep that ran 31 hours after the signatures expired recorded 979
photos as un-renderable while their bytes sat on this disk — which is why the
ladder is a function both callers share rather than a route body.

A disk hit is served straight off the filesystem, but a MISS is OF-bound, and a
folder pane can ask for 500 of them at once. That path is therefore capped — see
`_MAX_INFLIGHT_FETCHES`. File descriptors are a process resource: uncapped, a
cold vault pane doesn't render slowly, it exhausts them and takes the DB and the
send lane down with it.
"""
from __future__ import annotations

import asyncio
import logging
import os
import weakref
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, NamedTuple

from fastapi import HTTPException
from fastapi.responses import FileResponse, Response

import automation_executor as ax  # _make_client seam (same one automations use)
import vault_mirror  # the OF-media → mirror-row mapping, shared with the sweep
from db.engine import get_session
from db.models import VaultItem
from jsonsafe import load_json

log = logging.getLogger("of-relay.vault_stills")

# Module-level so a test can redirect the whole store with three assignments; the
# path builders read them at call time.
THUMB_DIR = Path(os.environ.get("VAULT_THUMB_DIR", "/tmp/of-relay-vault-thumbs"))
POSTER_DIR = Path(os.environ.get("VAULT_POSTER_DIR", "/tmp/of-relay-vault-posters"))
IMAGE_DIR = Path(os.environ.get("VAULT_IMAGE_DIR", "/tmp/of-relay-vault-images"))

_HEADERS = {"Cache-Control": "public, max-age=604800"}

# What a media dict yields for one store. Same callable is used against the url we
# already have AND against a re-signed payload — the picking rule cannot differ
# between "stored" and "fresh" if it is written once.
Pick = Callable[[dict], "str | None"]


def thumb_path(account_id: str, media_id: int) -> Path:
    return THUMB_DIR / account_id / f"{media_id}.jpg"


def poster_path(account_id: str, media_id: int, idx: int) -> Path:
    return POSTER_DIR / account_id / f"{media_id}_{idx}.jpg"


def image_path(account_id: str, media_id: int) -> Path:
    return IMAGE_DIR / account_id / f"{media_id}.jpg"


# Every store here holds ONE still — a 300px square, a ~960x1280 preview, a video
# poster frame. Those run tens of KB; the cap is set well above the largest real
# one and well below a video, so it can only ever catch a mis-picked url.
_MAX_STILL_BYTES = 8 * 1024 * 1024

# Magic numbers for the formats OF actually serves. A still is written to disk as
# `{media_id}.jpg` and served as image/jpeg, so "the server said 200" is not
# enough to believe it is a picture.
_IMAGE_MAGIC = (
    b"\xff\xd8\xff",      # JPEG
    b"\x89PNG\r\n\x1a\n",  # PNG
    b"GIF87a", b"GIF89a",  # GIF
)


# How many stills may be OF-bound at once, process-wide.
#
# One folder pane can release 500+ <img> tags in a single paint (an AI-folder
# lane runs to 569 items), and on a cold store EVERY one of them is a miss: a
# thread off the shared to_thread pool, a curl handle, and up to two OF calls.
# Unbounded, that is exactly the EMFILE `client_pool` was built to stop —
# pinning the client capped handles per CALL, but nothing capped calls in
# FLIGHT. Descriptors are a PROCESS resource, so running out doesn't fail the
# tiles, it fails everything: sqlite can't open the DB ("unable to open
# database file"), the isolation middleware can't scandir the session dir, sends
# die. Observed on 2026-07-28 opening the AI-folders modal on a 1596-item vault.
#
# So this is a fairness device as much as an fd one — it also leaves the shared
# executor room for the automation lane (see the 2026-07-04 starvation
# incident, where OF-bound work pinned every thread and sends timed out).
#
# Queueing behind it is the POINT. A tile that waits renders late; a tile that
# takes the last descriptor takes the relay down with it.
import lane_config as _lane_config
_MAX_INFLIGHT_FETCHES = _lane_config.resolve("VAULT_STILL_CONCURRENCY", 6)

# Per-loop, because the test harness runs each case in its own `asyncio.run()`
# and an `asyncio.Semaphore` that has parked a waiter belongs to the loop it
# parked it on. Weak keys so a finished loop doesn't pin its semaphore forever.
_SLOTS: "weakref.WeakKeyDictionary[Any, asyncio.Semaphore]" = weakref.WeakKeyDictionary()


def _fetch_slot() -> asyncio.Semaphore:
    """The gate every OF-bound still fetch passes through."""
    loop = asyncio.get_running_loop()
    sem = _SLOTS.get(loop)
    if sem is None:
        sem = _SLOTS[loop] = asyncio.Semaphore(_MAX_INFLIGHT_FETCHES)
    return sem


def _looks_like_an_image(data: bytes) -> bool:
    if data.startswith(_IMAGE_MAGIC):
        return True
    # WEBP is RIFF....WEBP — the size field sits between the two markers.
    return len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP"


def fetch_bytes_sync(client: Any, url: str) -> bytes | None:
    """One still, through the account's client so the source IP matches the url's
    signed Policy.

    None on anything we should not cache, which is deliberately more than "non-200":
    an expired signature reads as a 403 here rather than an exception, but OF also
    answers a wrong-variant url with a perfectly good 200 carrying an MP4, and an
    edge in front of it can answer with an HTML interstitial. Both used to be
    written to `{media_id}.jpg` and served back as image/jpeg — permanently, since
    a disk hit short-circuits before any of this runs. Returning None instead sends
    the caller down the re-sign path, which is the correct response to a url that
    did not yield a picture."""
    try:
        r = client.http.get(url, timeout=client.timeout_s)
        if r.status_code != 200:
            return None
        declared = r.headers.get("Content-Length") if hasattr(r, "headers") else None
        if declared and declared.isdigit() and int(declared) > _MAX_STILL_BYTES:
            log.warning("still too large to cache url=%.80s bytes=%s", url, declared)
            return None
        data = r.content
        if not data or len(data) > _MAX_STILL_BYTES:
            return None
        if not _looks_like_an_image(data):
            log.warning("refusing to cache a non-image body url=%.80s head=%r",
                        url, data[:8])
            return None
        return data
    except Exception:  # noqa: BLE001
        # Logged, not swallowed. This return is indistinguishable from "OF said
        # 403" to the caller, and a describe sweep that stamps 2,000 rows
        # `fetch_failed` needs SOME line in the log saying which failure it was.
        log.warning("still fetch raised url=%.80s", url, exc_info=True)
    return None


def write(p: Path, data: bytes) -> None:
    """Persist a fetched still. Best-effort — a full disk must not break the
    response we already have in hand.

    Written to a sibling temp file and RENAMED into place, because a disk hit
    short-circuits every validation below: a partial file (full disk, killed
    container, two writers racing the same media) would otherwise be cached as
    that media's picture permanently, and the bytes it truncated are the only
    copy that outlives OF's signature. `os.replace` is atomic within a
    filesystem, so a reader sees the whole file or no file."""
    tmp = p.with_name(f".{p.name}.{os.getpid()}.part")
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_bytes(data)
        os.replace(tmp, p)
    except Exception:  # noqa: BLE001
        try:
            tmp.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass


class _Resolved(NamedTuple):
    """The outcome of re-reading one media from OF. `gone` is the distinction the
    whole retry budget rests on: a media OF has DELETED can never be fetched again,
    so it must be remembered; a timeout or a 500 must not be."""
    media: dict | None
    gone: bool


def _resolve_fresh_sync(client: Any, media_id: int) -> _Resolved:
    """Re-read one media from OF for a freshly signed url set.

    OF has no batch by-id form, so this is one call per id — only ever taken on a
    real miss. A 404 means the media is gone for good and is reported as such; any
    other failure is transient and stays retryable."""
    try:
        m = client.vault_media_by_id(media_id)
        return _Resolved(m if isinstance(m, dict) else None, False)
    except Exception as e:  # noqa: BLE001
        resp = getattr(e, "response", None)
        return _Resolved(None, getattr(resp, "status_code", None) == 404)


async def _refresh_mirror(account_id: str, media_id: int, media: dict) -> None:
    """Write a re-signed payload back over the mirror row so the NEXT miss starts
    from a url that still resolves.

    Routed through the shared mirror mapping rather than hand-updating the two url
    columns: `raw_json` and the columns DERIVED from it (`of_folder_ids`,
    `thumb_url`, …) have to move together, and a second hand-rolled mapper is how
    they drift apart. `vault_mirror` owns that mapping precisely so this can be a
    module-scope import — reaching into `vault_ai_api` here meant a lazy
    in-function import to dodge a cycle, which is a cycle with the failure
    deferred to runtime rather than a resolved dependency."""
    async with get_session() as s:
        item = await s.get(VaultItem, (account_id, media_id))
        if item is None:
            return
        vals = vault_mirror.row_values(account_id, media, int(item.last_seen_run_id or 0))
        if vals is None:
            return
        for k in vault_mirror.MIRROR_REFRESH_FIELDS:
            setattr(item, k, vals[k])
        await s.commit()


async def _mark_gone(account_id: str, media_id: int) -> None:
    """OF says this media no longer exists. Stamp the row the same way a collect
    sweep soft-deletes a vanished item — which is also what stops `serve` from
    re-asking OF about it on every future render."""
    now = datetime.utcnow()
    async with get_session() as s:
        item = await s.get(VaultItem, (account_id, media_id))
        if item is not None and item.removed_at is None:
            item.removed_at = now
            item.updated_at = now
            await s.commit()


class Still(NamedTuple):
    """One still, plus WHY when there isn't one.

    The reason is the whole point of this type. `bytes | None` was enough while
    the only caller was an HTTP route that turns every miss into a 404, but the
    describe sweep records its miss in the DB — and recording "un-renderable"
    for what was really an expired signature is precisely the bug this module
    exists to stop. `no_variant` and `fetch_failed` want opposite retry
    policies, so the distinction has to survive the return."""

    data: bytes | None
    reason: str  # "ok" | "gone" | "no_variant" | "fetch_failed"


class Fresh(NamedTuple):
    """A re-signed media payload, plus WHY when there isn't one.

    `media is None` used to be the whole answer, and it collapsed the two
    outcomes that matter: OF has DELETED this media (permanent — stop asking)
    versus OF did not answer (transient — ask again next sweep). Both callers
    needed the distinction back and both recovered it the same way, by re-reading
    `VaultItem.removed_at` in a second session — a bit this function had already
    computed and then discarded, re-derived with a round-trip, twice, in two
    modules. `reason` carries it instead."""

    media: dict | None
    reason: str  # "ok" | "gone" | "fetch_failed"


async def resolve_fresh(account_id: str, media_id: int, client: Any) -> Fresh:
    """Re-read one media from OF for a freshly signed url set, and write the
    refreshed payload back over the mirror row.

    Returns the fresh media dict, or the reason there isn't one — a deleted media
    additionally stamps `removed_at`, which is what stops the next caller
    re-asking. Shared with the video path in `vault_frames`: a stale mp4 url is
    the same stale signature as a stale jpeg url, and re-deriving it from the
    same fresh payload is what keeps the two from disagreeing about which media
    they are looking at."""
    fresh = await asyncio.to_thread(_resolve_fresh_sync, client, media_id)
    if fresh.gone:
        await _mark_gone(account_id, media_id)
        return Fresh(None, "gone")
    if fresh.media is None:
        return Fresh(None, "fetch_failed")
    await _refresh_mirror(account_id, media_id, fresh.media)
    return Fresh(fresh.media, "ok")


async def bytes_for(account_id: str, media_id: int, path: Path, pick: Pick) -> Still:
    """One still's bytes: disk if we have them, otherwise fetch and keep them.

    `pick` reads the url for THIS store out of a media dict.

    There is ONE fallback and the caller does not supply it. A media we have never
    collected simply has no stored url, which lands in the same re-sign branch a
    STALE url lands in: ask OF for this one media by id and take the freshly
    signed set. The route used to accept a `u=` query param for that case, which
    made every render endpoint a fetch-any-url proxy gated by a host substring
    check — and bought nothing, because the by-id read already covers it.

    The disk hit is the reason this is worth calling from a background sweep and
    not just from a route: a describe pass over an already-collected vault does
    it 1,900 times and touches OF zero times."""
    if path.is_file():
        return Still(path.read_bytes(), "ok")

    # Everything past here costs descriptors — a DB connection, a curl handle,
    # a thread — so it runs `_MAX_INFLIGHT_FETCHES` at a time. A warm store
    # never reaches this line; a cold one no longer takes the process down.
    async with _fetch_slot():
        # Re-check under the slot. A pane of 500 tiles queues requests for
        # DIFFERENT media, but a re-render (scrolling back, a React remount)
        # queues several for the SAME one, and whoever held the slot first has
        # already written it. Without this they each pay the full OF round-trip
        # to re-fetch bytes that are now sitting on disk.
        if path.is_file():
            return Still(path.read_bytes(), "ok")

        async with get_session() as s:
            item = await s.get(VaultItem, (account_id, media_id))
        # The negative cache. Set below (or by a collect sweep) when OF reported
        # the media deleted; without it, every render of a dead tile pays two
        # OF calls.
        if item is not None and item.removed_at is not None:
            return Still(None, "gone")
        # The second negative cache, and the cheaper one: a kind that can never
        # yield a picture (see `vault_mirror.has_still`). `no_variant` is the
        # honest verdict — asking again changes nothing — and unlike `gone` it
        # must NOT soft-delete: the voice note is perfectly fine, it is just not
        # a picture. Nothing renders a `_thumb` for one any more, so this is the
        # backstop for the surfaces that build the url from an id alone
        # (`mirrorThumbSrc`, `mirrorFullSrc`, the legacy panes).
        if item is not None and not vault_mirror.has_still(item.kind):
            return Still(None, "no_variant")

        url: str | None = None
        if item is not None:
            url = pick(load_json(item.raw_json, {}) or {}) or item.thumb_url

        client = await asyncio.to_thread(ax._make_client, account_id)
        data = None
        if url:
            data = await asyncio.to_thread(fetch_bytes_sync, client, url)
        if data is None:
            # The stored url is dead (or we never had one) — re-sign, retry once.
            fresh = await resolve_fresh(account_id, media_id, client)
            if fresh.media is None:
                return Still(None, fresh.reason)
            retry = pick(fresh.media)
            if not retry:
                # OF answered, and its answer has nothing this store can render.
                # Not a transient failure — asking again changes nothing.
                return Still(None, "no_variant")
            if retry == url:
                return Still(None, "fetch_failed")
            data = await asyncio.to_thread(fetch_bytes_sync, client, retry)
            if data is None:
                return Still(None, "fetch_failed")

        write(path, data)
        return Still(data, "ok")


async def serve(account_id: str, media_id: int, path: Path, pick: Pick) -> Response:
    """The HTTP adapter over `bytes_for` — every render endpoint's whole body.

    Raises 404 for a media we can't render: `media_gone` (OF has deleted it —
    cheap and permanent after the first time), `no_variant` (OF offers nothing
    this store can show) or `fetch_failed` (transient; retrying is fine)."""
    # Ahead of `bytes_for`'s own disk read so a warm tile streams from disk
    # instead of being loaded into memory — a folder pane paints 500 of these.
    if path.is_file():
        return FileResponse(path, media_type="image/jpeg", headers=_HEADERS)
    still = await bytes_for(account_id, media_id, path, pick)
    if still.data is None:
        raise HTTPException(
            status_code=404,
            detail="media_gone" if still.reason == "gone" else still.reason)
    return Response(content=still.data, media_type="image/jpeg", headers=_HEADERS)


def dir_stats(d: Path) -> dict[str, int]:
    """(files, bytes) under one account's store. Cheap enough at vault scale (a few
    hundred to a few thousand jpgs)."""
    n = 0
    total = 0
    try:
        for f in d.iterdir():
            if f.is_file():
                n += 1
                try:
                    total += f.stat().st_size
                except OSError:
                    pass
    except OSError:
        pass
    return {"files": n, "bytes": total}


def purge(account_id: str, dirs: list[Path]) -> int:
    """Delete one account's files under `dirs`. Returns how many went."""
    removed = 0
    for base in dirs:
        try:
            for f in (base / account_id).iterdir():
                if f.is_file():
                    try:
                        f.unlink()
                        removed += 1
                    except OSError:
                        pass
        except OSError:
            continue
    return removed
