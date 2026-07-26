"""
of_shapes.py — readers for the shapes OF's payloads actually arrive in.

Pure parsing of OF message frames and media objects, shared by layers that must
NOT import each other:

  • event_transcoder — the WS ingest path
  • messages         — the timeline HTTP API
  • vault_ai_api     — the vision / describe path

That spread is the whole reason this module exists. Parking a shared parser in
any one of them makes another import a layer above itself: `_giphy_dm_id` briefly
lived in `messages`, which meant the WS transcoder pulled in the FastAPI router,
pydantic, `stats` and `auth` at import time just to ask a question about a dict.

Rules for anything added here: pure functions over plain dicts, NO db, NO
network, NO FastAPI. Everything may import this module; this module imports
nothing from the app. That's what keeps it safe to call from any layer.

NOT here (yet): frame-level media-id plucking. `event_transcoder._media_ids` and
`automation_executor._media_ids` are near-twins that genuinely DISAGREE — the
latter also accepts digit strings — so folding them into one canonical reader is
a behavior change that deserves its own justification, not a silent side effect
of a move.
"""
from __future__ import annotations

# OF sub-blocks that may carry REAL (non-square) pixel dims, best first.
# `thumb` is deliberately EXCLUDED: OF thumbs are 300x300 squares that would
# distort the aspect ratio the skeleton/grid reserve.
DIM_KEYS = ("full", "source", "preview")

# Extensions that are NOT a single decodable image — video AND audio. OF's CDN
# urls carry a long `?Tag=…&Signature=…` query, so the test runs on the path only.
# Audio belongs here even though no one would "look at" a voice note: the still
# picker asks *is this an image*, and OF serves voice as `files.full` ending .mp3
# (verified on real payloads) with no poster of any kind. Leave it out and a voice
# note gets handed to a vision model as if it were a picture.
NON_IMAGE_EXTS = (".mp4", ".m3u8", ".mov", ".webm", ".m4v",
                  ".mp3", ".m4a", ".wav", ".aac", ".ogg", ".opus")


def media_urls(media: dict) -> tuple[str | None, str | None]:
    """(thumb_url, source_url) from an OF media object, tolerating shape drift."""
    thumb_url = source_url = None
    files = media.get("files") if isinstance(media, dict) else None
    if isinstance(files, dict):
        thumb = files.get("thumb")
        if isinstance(thumb, dict):
            thumb_url = thumb.get("url")
        for k in DIM_KEYS:
            sub = files.get(k)
            if isinstance(sub, dict) and sub.get("url"):
                source_url = sub.get("url")
                break
    if not source_url and isinstance(media, dict):
        source_url = media.get("url")
    return (thumb_url, source_url)


# Image variants to hand a VISION model, widest-useful first. This ordering is
# hard-won, not arbitrary (vault_ai_api learned it the expensive way):
#
#   • `thumb` is 300x300 and, because OF's stills are 3:4 portrait, it is a
#     centre-CROP — the top and bottom of the picture are simply GONE. Describe
#     ran on that for its whole life, which is why it kept reporting `fully_nude`
#     for shots whose only garment was a band of underwear at the bottom edge.
#     So the smallest variant is the WRONG default, however tempting.
#   • `preview` (~960px) keeps the real aspect ratio and ~13x the pixels.
#   • `full` (2316x3088) is bigger still but costs proportionally more bandwidth
#     for detail the task doesn't need — and the caller downscales anyway.
#   • `squarePreview`/`thumb` stay as last resorts for media exposing nothing else.
#
# For a VIDEO the same list resolves to OF's own poster frame: `preview` is a
# still image, while `full` is the .mp4 (skipped by the non-image test).
_VISION_VARIANTS = ("preview", "full", "source", "squarePreview", "thumb")


def _is_non_image_url(url: str | None) -> bool:
    """True when `url` points at video/audio rather than a decodable image."""
    return bool(url) and url.split("?", 1)[0].lower().endswith(NON_IMAGE_EXTS)


def still_url(media: dict) -> str | None:
    """The url of a single decodable IMAGE for this media object, or None.

    Two things this protects every vision consumer from:

    1. OF stores a **gif as an .mp4**, so its source url is a video even though
       `type` says "gif". Handing those bytes to an image decoder gets "cannot
       identify image file" (hit live as a DeepInfra HTTP 400 on a real fan-sent
       gif). Video/audio-looking urls are skipped, so a gif resolves to its still.
    2. A **video** resolves to OF's pre-extracted poster frame when it ships one —
       which is every video we're allowed to view (`canView: true` ⇒ preview +
       thumb present; locked ones carry only the .mp4 and correctly yield None).
       No ffmpeg, no downloading the clip.

    Returns None when nothing renderable is on offer — that is the ONE question
    callers should ask, instead of allow-listing media `type` values."""
    files = media.get("files") if isinstance(media, dict) else None
    if isinstance(files, dict):
        for key in _VISION_VARIANTS:
            f = files.get(key)
            url = f.get("url") if isinstance(f, dict) else None
            if url and not _is_non_image_url(url):
                return url
    # Shape drift: some payloads put the url straight on the media object.
    loose = media.get("url") if isinstance(media, dict) else None
    return loose if loose and not _is_non_image_url(loose) else None


def has_video(media: list | None) -> bool:
    """Does this media array carry a clip?

    Shared so the ingest dispatch and the free-fan describe budget can never
    disagree about which lane a DM spends. Reads the parsed `type` of each item —
    NEVER a substring of the stored frame. A raw `'"type": "video"' in raw_json`
    test looks equivalent and is not: a quoted/replied message embeds the ORIGINAL
    frame, so a photo sent as a reply to a video post matches and gets billed as a
    clip. (Measured cousin of this bug: a `LIKE '%"media": [{%'` scan over prod
    over-counted media DMs by 73% for exactly that reason.)"""
    return isinstance(media, list) and any(
        isinstance(m, dict) and m.get("type") == "video" for m in media)


def giphy_dm_id(frame: dict) -> str | None:
    """The Giphy id when this OF message frame is a GIF-ONLY dm, else None.

    OF sends a Giphy gif as a bare top-level `giphyId` with `media: []` and
    `mediaCount: 0` — there is no media object at all, which is why a media-only
    gate makes a gif an invisible turn. Both the ingest dispatch and the describe
    path classify frames this way, so they share ONE definition rather than
    re-deriving it and drifting on the (currently unseen) giphyId-plus-media case.
    Returns the id so callers get the predicate and the value in one call."""
    if not isinstance(frame, dict):
        return None
    media = frame.get("media")
    if isinstance(media, list) and media:
        return None
    gid = str(frame.get("giphyId") or "").strip()
    return gid or None
