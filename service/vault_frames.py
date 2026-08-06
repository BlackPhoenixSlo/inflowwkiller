"""
vault_frames.py — ONE vault media → the pictures of it.

Everything between "here is a media id" and "here are the frames a vision model
can read" lives here: which url in an OF payload is the picture, which store
holds its permanent bytes, and how a clip becomes stills.

Split out of `vault_ai_api` for the same reason `vision.py` was: describing a
media has nothing to do with routing, folder mirroring or PPV weeks, and the
2026-08-04 incident was a direct consequence of it being buried in 3.5k lines of
those — the fetch it hand-rolled had drifted years away from the ladder
`vault_stills` had grown for exactly this problem, and nothing sat close enough
to either to notice.

The order every lookup takes, and the whole point of this module:

    permanent local bytes  →  the url we stored  →  re-sign with OF  →  give up

OF's signed urls resolve for about a day. A collect stores them; a describe sweep
runs whenever an operator presses the button. Anything that reads a stored url
and stops there works only when those two happen to be close together, which is
not a property the code can rely on and did not have on 2026-08-04, when a sweep
31 hours behind its collect read nothing at all and recorded 979 photos as
un-renderable while their bytes sat on disk.

Rules for anything added here: no routes, no DB writes beyond what the still
store already owns, no prompts. What to ASK about a picture belongs to the
consumer; this module only produces the picture.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, NamedTuple

import automation_executor as ax  # _make_client seam (same one automations use)
import vault_mirror  # the OF-media → mirror-row mapping (thumb_of)
import vault_stills
import vision
from db.models import VaultItem  # the mirror row `collect` is handed (annotation)
from of_shapes import still_url  # shared OF payload-shape reader

log = logging.getLogger("of-relay.vault_frames")

# How many storyboard frames a describe asks for when nothing overrides it.
DESCRIBE_FRAMES = 9


# ── What a payload offers ───────────────────────────────────────────

def describe_image_of(files: dict | None) -> str | None:
    """The image variant to send the VISION model — not the same choice as the UI
    thumb. Takes a bare `files` dict (vault items store one); the ordering and the
    centre-crop lesson behind it live in `of_shapes.still_url`, shared with the
    inbound-DM describe path so both ask the same question of the same payloads."""
    return still_url({"files": files}) if isinstance(files, dict) else None


def video_url(m: dict) -> str | None:
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


def poster_frames(raw: dict) -> list[str]:
    """OF's own pre-extracted poster-frame URLs for a video
    (`files.preview.options[]`). For SAMPLE-AES DRM clips — which ffmpeg can't
    slice — these are the ONLY renderable representation (same frames the chat
    VaultPicker slideshows). Falls back to the single poster thumb."""
    files = raw.get("files") or {}
    prev = files.get("preview") or {}
    out = [o.get("url") for o in (prev.get("options") or []) if isinstance(o, dict) and o.get("url")]
    if not out:
        one = vault_mirror.thumb_of(files)
        if one:
            out = [one]
    return out


# ── Which url each permanent store holds ────────────────────────────
#
# One rule per store, written once and used for BOTH the stored payload and a
# re-signed one, so the two can't disagree about which picture they mean. The
# three render routes and the describe ladder below share them for that reason.

def pick_thumb(m: dict) -> str | None:
    """The 300px square — the grid + picker tile."""
    return vault_mirror.thumb_of(m.get("files") if isinstance(m.get("files"), dict) else {})


def pick_image(m: dict) -> str | None:
    """Aspect-preserving preview → full → …; the square only if the media exposes
    nothing else."""
    return describe_image_of(m.get("files") if isinstance(m.get("files"), dict) else {})


def pick_poster(i: int) -> vault_stills.Pick:
    """Frame `i` of the poster set, or None if the video has fewer frames."""
    def pick(m: dict) -> str | None:
        frames = poster_frames(m)
        return frames[i] if i < len(frames) else None
    return pick


# ── The storyboard ──────────────────────────────────────────────────

def warm_one_sync(client: Any, url: str, dur: float) -> str:
    """Extract + cache the 12-frame storyboard for ONE video, if not already
    on disk. Incremental: `_storyboard_all_frames_present` short-circuits a
    video we've already warmed (so re-collect only does NEW videos). Reuses
    the relay's own storyboard helpers; downloads through the account's OF
    client so the URL's source-IP signature matches.

    Returns its own outcome vocabulary; `_WARM_REASON` is the only place that
    translates it into this module's."""
    import server as srv  # lazy: avoid the server↔vault_frames import cycle

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


def spread(n_have: int, want: int) -> list[int]:
    """`want` frame indices spread evenly over `n_have` slots, endpoints
    included. 12 frames → 3 gives [1,5,10]; → 9 gives [0,1,3,4,5,6,8,9,11].
    Endpoints matter for `beats`: the model has to see how a clip ENDS."""
    if want >= n_have:
        return list(range(n_have))
    if want <= 1:
        return [n_have // 2]
    step = (n_have - 1) / (want - 1)
    return sorted({int(round(i * step)) for i in range(want)})


def storyboard_frames(url: str | None, want: int) -> list[str]:
    """`want` frames of this video's storyboard, IF the whole set is on disk.

    Keyed by url rather than by hash so no caller has to know that the store is
    hash-addressed — which matters more than it looks: `_video_path_hash` reads
    the host and path, so frames warmed under a re-signed url answer to a
    different key than the stored one, and a caller holding the wrong key finds
    nothing and concludes the clip is un-renderable."""
    if not url:
        return []
    import server as srv  # lazy

    h = srv._video_path_hash(url)
    if not srv._storyboard_all_frames_present(h):
        return []
    out: list[str] = []
    for i in spread(srv._STORYBOARD_FRAMES, want):
        p = srv._storyboard_frame_path(h, i)
        if p.is_file():
            out.append(vision.img_data_url(p.read_bytes()))
    return out


# ── The ladder ──────────────────────────────────────────────────────

class Collected(NamedTuple):
    """The frames to send a vision model, and WHY there aren't any.

    `reason` uses `vault_stills.Still`'s vocabulary unchanged, because it answers
    the same question one layer up. It is what the caller stamps on the row, and
    the two failing values want OPPOSITE retry policies:

        fetch_failed  we could not read bytes we have every reason to think
                      exist — a dead signature, OF unreachable. Retry at once.
        no_variant    OF answered, and its answer carries nothing renderable.
                      Asking again changes nothing; cool off.

    Collapsing those two is the whole 2026-08-04 incident: every failure was
    recorded as the second, so 1,777 items inherited a 7-day cool-off and the
    sweep that would have fixed them skipped them."""

    images: list[str]
    reason: str  # "ok" | "gone" | "no_variant" | "fetch_failed"


# `warm_one_sync`'s outcome in the vocabulary above. Anything not listed either
# produced frames or means "OF has nothing sliceable", which is `no_variant`.
_WARM_REASON = {"dl_fail": "fetch_failed", "error": "fetch_failed"}


async def _poster_stills(account_id: str, media_id: int, media: dict,
                         want: int, warm: str) -> Collected:
    """OF's pre-extracted poster frames, through the permanent store.

    The fallback for a clip ffmpeg cannot slice — OF serves these even for DRM.
    Reading them through `vault_stills` (rather than fetching the urls directly)
    is what makes a DRM clip describable a SECOND time: the first pass leaves the
    frames on disk, and every pass after that costs no OF call at all."""
    frames: list[str] = []
    transient = False
    # `pick_poster` re-derives each url from the payload — one rule, one place —
    # so all this needs from the list is how many frames are on offer.
    for i in range(len(poster_frames(media)[:want])):
        still = await vault_stills.bytes_for(
            account_id, media_id, vault_stills.poster_path(account_id, media_id, i),
            pick_poster(i))
        if still.data is not None:
            frames.append(vision.img_data_url(still.data))
        elif still.reason == "gone":
            return Collected([], "gone")
        elif still.reason == "fetch_failed":
            transient = True
    if frames:
        return Collected(frames, "ok")
    if transient:
        return Collected([], "fetch_failed")
    return Collected([], _WARM_REASON.get(warm, "no_variant"))


async def _collect_video(account_id: str, media_id: int, raw: dict,
                         client: Any, want: int) -> Collected:
    """Frames for one clip: the warmed storyboard, else OF's poster set.

    The storyboard is both the cheap path and the durable one — its key is taken
    from the url with the query string stripped, so frames warmed under one
    signature keep answering under the next. That is the entire reason clips
    survived the 2026-08-04 expiry and photos did not.

    A clip we never warmed still needs a live mp4, and the stored url is as dead
    as any other. So the miss re-signs ONCE, and everything after it — the mp4,
    the key its frames are written under, the poster urls — comes from that one
    fresh payload. Mixing a fresh mp4 with stale poster urls is how two halves of
    the same read end up describing different media."""
    frames = storyboard_frames(video_url(raw), want)
    if frames:
        return Collected(frames, "ok")

    fresh = await vault_stills.resolve_fresh(account_id, media_id, client)
    if fresh.media is None:
        # `gone` vs `fetch_failed` comes back WITH the miss — the two want
        # opposite retry policies and the store already knows which it was.
        return Collected([], fresh.reason)
    media = fresh.media

    warm = ""
    fresh_url = video_url(media)
    if fresh_url:
        warm = await asyncio.to_thread(
            warm_one_sync, client, fresh_url,
            float(media.get("duration") or raw.get("duration") or 0))
        frames = storyboard_frames(fresh_url, want)
        if frames:
            return Collected(frames, "ok")

    return await _poster_stills(account_id, media_id, media, want, warm)


async def collect(account_id: str, item: VaultItem, raw: dict,
                  want: int = DESCRIBE_FRAMES) -> Collected:
    """Frames for one vault item, as base64 data-URLs.

    Photos read IMAGE_DIR, never THUMB_DIR. The thumb is a 300px centre-crop of a
    3:4 portrait — its top and bottom are simply gone — and reading a flag off
    that crop is what made the vision model over-report `fully_nude`. `pick_image`
    is `describe_image_of`, so this is the same variant describe has always sent."""
    client = await asyncio.to_thread(ax._make_client, account_id)
    if item.kind == "video":
        return await _collect_video(account_id, item.media_id, raw, client, want)
    still = await vault_stills.bytes_for(
        account_id, item.media_id,
        vault_stills.image_path(account_id, item.media_id), pick_image)
    if still.data is None:
        return Collected([], still.reason)
    return Collected([vision.img_data_url(still.data)], "ok")
