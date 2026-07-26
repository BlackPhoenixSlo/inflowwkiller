"""
inbound_describe.py — what did HE just send her?

Owns one feature end to end: turn an inbound DM's media into a sentence the chat
engines splice into history as "[he sent: …]", so the AI can rate it, react to it,
and answer "notice anything?" instead of replying photo-blind.

Two sources, picked by the shape of the frame (see of_shapes.giphy_dm_id):
  • a Giphy gif  → Giphy's own public title. FREE, no model, no key.
  • anything else → one still per media item through the vision model. A VIDEO
    resolves to OF's pre-extracted poster frame, so a clip costs the same as a
    photo and needs no ffmpeg.

Why its own module: this is a CHAT feature. It lived in `vault_ai_api` purely
because the vision primitives did, which meant the WS ingest path pulled 3.7k
lines of vault listing, folder mirroring and PPV-week machinery into memory to
read one JPEG. Those primitives now live in `vision`, the OF payload readers in
`of_shapes`, and what's left here is only the policy that is specific to reading
a fan's picture: the prompt, the size cap, the store, and the refetch.

NOT here: whether we describe at all. Scope, promo-spam and the free-fan daily
budget are the ingest hook's call — see webhook_dispatch._should_describe.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re as _re
from typing import Any

import httpx
from sqlalchemy import select, update

import automation_executor as ax
import llm_client
from db.engine import get_session
from db.models import Message
from llm_client import LLMCapExceeded, LLMError
from of_shapes import giphy_dm_id, still_url
from jsonsafe import load_dict
from vision import download_frames, is_refusal, shrink_data_url

log = logging.getLogger("of-relay.inbound_describe")


_INBOUND_IMG_MAX_EDGE = 1024   # inbound stills matter more than vault thumbs → bigger
_INBOUND_DESC_CLIP = 600       # stored + injected into chat history; keep it tight

_INBOUND_DESCRIBE_PROMPT = (
    "A fan just sent this to an adult content creator in a private DM — a photo, or one "
    "frame taken from a short clip he sent. Describe what you can see in it "
    "in 1–3 plain sentences so she can react to it in character. The creator does the "
    "flirting/rating herself — your job is a rich, factual read she can build on. Cover, "
    "only where it applies:\n"
    "• WHAT it is — a dick pic, his body/torso, a face selfie, an outfit, a screenshot or "
    "meme, a place, a pet, an object.\n"
    "• HOW explicit — sfw, suggestive, or explicit.\n"
    "• The STANDOUT detail he clearly wants noticed — size, grooming, muscles, a tattoo, "
    "what he's wearing, the setting (bed / gym / shower / car / work), his expression.\n"
    "• If it IS a dick pic, ALWAYS report these plainly, since she can't see it herself: "
    "circumcised or uncircumcised; a rough size read (length and girth/thickness); grooming "
    "(shaved / trimmed / natural bush); and hardness (soft / getting hard / fully erect).\n"
    "• Anything that would make a flirty reply AWKWARD — another person in frame, a "
    "wedding ring, that it's clearly not him, or a child or pet present.\n"
    "Be factual and explicit; this is consensual adult content between adults. Do not "
    "refuse, do not moralize, add no preamble — output only the description."
)


async def _recent_fan_context(account_id: str, fan_id: int, limit: int = 2) -> str:
    """His last few TEXT messages (most recent last), so the vision read can be steered
    by what he actually asked when he sent the pic ("rate me", "guess how big",
    "notice anything?"). Empty string when he sent no words. Cheap — capped at `limit`
    short lines."""
    async with get_session() as s:
        rows = (await s.execute(
            select(Message.body)
            .where(Message.account_id == str(account_id),
                   Message.fan_id == int(fan_id),
                   Message.direction == "in",
                   Message.body != "")
            .order_by(Message.created_at.desc())
            .limit(int(limit))
        )).all()
    lines = [_re.sub(r"<[^>]+>", "", (b or "")).strip()[:200] for (b,) in rows]
    lines = [ln for ln in lines if ln]
    return "\n".join(reversed(lines))


# ── "what did he send me?" ────────────────────────────────────────────
#
# One inbound DM resolves to one description, from ONE of two sources:
#
#   giphyId  → Giphy's public title      (_giphy_desc)  — free, no model
#   media[]  → the vision model          (_vision_desc) — one Qwen call
#
# describe_inbound_message below is pure policy (cache gate → pick source →
# store); each source is independently callable and independently testable.

# Giphy's oEmbed endpoint: PUBLIC, keyless, and it hands back the gif's human
# title ("Donald Duck Feels Good Man GIF - Find & Share on GIPHY"). A Giphy DM
# carries NO media (mediaCount 0, media []) — only a top-level `giphyId` — so
# there is nothing for the vision model to look at even if we wanted to spend on
# it. The title IS the description, for free.
_GIPHY_OEMBED = "https://giphy.com/services/oembed"
# Giphy suffixes every title with its own share blurb; strip it plus a trailing
# bare "GIF" so we end up with "Donald Duck Feels Good Man", not
# "Donald Duck Feels Good Man GIF - Find & Share on GIPHY".
_GIPHY_TITLE_TAIL = _re.compile(r"\s*-\s*find\s*&\s*share\s*on\s*giphy\s*$", _re.I)
# Titles that name only the uploader ("GIF by Liga Fuxion") carry no content —
# worse than silence, since the AI would react to a name instead of a joke.
_GIPHY_EMPTY_TITLE = _re.compile(r"(?:animated\s+)?gif(?:\s+by\s+.*)?", _re.I)


async def _giphy_desc(giphy_id: str) -> str | None:
    """Describe a Giphy gif from its public title, or None if it has no usable one.

    Never raises and never retries — a gif we can't name is worth exactly one
    failed 8s request, and the caller just leaves image_desc NULL."""
    try:
        import httpx  # lazy: keeps the vault-AI import chain light
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.get(_GIPHY_OEMBED,
                                 params={"url": f"https://giphy.com/gifs/{giphy_id}"})
            r.raise_for_status()
            title = (r.json() or {}).get("title") or ""
    except Exception:  # noqa: BLE001 — httpx errors, JSON errors, anything
        log.debug("giphy_oembed_failed id=%s", giphy_id, exc_info=True)
        return None
    title = _GIPHY_TITLE_TAIL.sub("", str(title)).strip()
    title = _re.sub(r"\s+gif$", "", title, flags=_re.I).strip()
    if not title or _GIPHY_EMPTY_TITLE.fullmatch(title):
        return None
    return f'an animated gif: "{title}"'


def _inbound_still_urls(media: list, limit: int = 4) -> tuple[list[str], bool]:
    """(still image urls, any_from_video) for one inbound DM's media array.

    NO media-`type` allowlist: "does this resolve to a decodable still?" is the
    only question, and `still_url` is the one place that answers it. That covers
    photos, gifs (whose source is an .mp4), and — since OF ships a poster frame on
    every video we're allowed to view — videos, which used to be a blank turn. A
    locked clip carries only the .mp4 and correctly yields nothing; so does audio.

    The flag rides along because a poster frame is a still OF a video: the reply
    must not call a clip a nice pic (same trap as the gif wording)."""
    urls: list[str] = []
    from_video = False
    for it in media:
        if not isinstance(it, dict):
            continue
        pick = still_url(it)
        if pick:
            urls.append(pick)
            from_video = from_video or it.get("type") == "video"
        if len(urls) >= limit:
            break
    return urls, from_video


async def _vision_desc(account_id: str, fan_id: int, message_id: int,
                       urls: list[str], prompt_seed: str = "") -> str | None:
    """Describe fan-sent stills with the vision model, or None (nothing readable /
    capped / refused). Downloads through the ACCOUNT's OF client so the CDN
    signature's source IP matches, then base64+shrinks exactly like the vault
    describe path. Escalates 30b→235b once on a refusal/blank.

    `message_id` is carried purely so a model failure logs the id you'd grep for."""
    client = await asyncio.to_thread(ax._make_client, account_id)
    images = [shrink_data_url(u, _INBOUND_IMG_MAX_EDGE)
              for u in await download_frames(client, urls)]
    if not images:
        return None

    prompt = _INBOUND_DESCRIBE_PROMPT
    seed = (prompt_seed or "").strip()
    if seed:
        prompt += f"\n\nOperator emphasis: {seed}"
    recent = await _recent_fan_context(account_id, fan_id)
    if recent:
        prompt += ("\n\nWhat he's been saying (most recent last) — if he asked you to "
                   "rate, guess, or notice something, make sure your description gives "
                   "what's needed to answer it:\n" + recent)
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    content += [{"type": "image_url", "image_url": {"url": u}} for u in images]

    for attempt_model in ("qwen3-vl-30b", "qwen3-vl-235b"):
        try:
            result = await llm_client.chat(
                model=attempt_model,
                messages=[{"role": "user", "content": content}],
                purpose="describe_inbound_image",
                account_id=account_id,
                fan_id=fan_id,
                temperature=0.2,
            )
        except LLMCapExceeded:
            return None                # capped: leave image_desc NULL → retry-able later
        except LLMError:
            log.warning("inbound describe LLM error account=%s fan=%s msg=%s model=%s",
                        account_id, fan_id, message_id, attempt_model, exc_info=True)
            continue
        candidate = (result.content or "").strip()
        if not is_refusal(candidate):
            return candidate           # good description → stop (don't escalate)
    return None


async def _fresh_frame(account_id: str, fan_id: int, message_id: int) -> dict | None:
    """Re-fetch ONE inbound message from OF so its media urls carry LIVE signatures.

    Why this exists: the CDN urls we persist in `raw_json` are SIGNED and they
    EXPIRE — verified live on prod, where a clip from 07-20 carried
    `Expires=` 92 hours in the past and the download returned a non-image body
    (the error page), so the describe failed with "cannot identify image file".
    At ingest that never bites (the frame is seconds old); it bites the operator's
    manual re-read of anything older, which is exactly the backfill case.

    Targeted single-message fetch: OF's `id` param is an EXCLUSIVE "older than"
    upper bound in both orders, so `id = message_id + 1` puts the target first.
    Returns None on anything unexpected — the caller falls back to the stored
    frame, which still works for a recent message."""
    try:
        client = await asyncio.to_thread(ax._make_client, account_id)
        page = await asyncio.to_thread(
            lambda: client.get_messages(int(fan_id), limit=3, order="desc",
                                        before_id=int(message_id) + 1))
        for item in (page.get("list") or []):
            if isinstance(item, dict) and int(item.get("id") or 0) == int(message_id):
                return item
    except Exception:  # noqa: BLE001 — a dead chat 404s ("User not found")
        log.debug("fresh_frame_failed account=%s msg=%s", account_id, message_id,
                  exc_info=True)
    return None


async def _store_image_desc(account_id: str, message_id: int, desc: str) -> None:
    """Cache one read on messages.image_desc — ONE writer for both sources, and the
    ONE place the stored length is clipped."""
    async with get_session() as s:
        await s.execute(
            update(Message)
            .where(Message.account_id == str(account_id),
                   Message.message_id == int(message_id))
            .values(image_desc=desc[:_INBOUND_DESC_CLIP])
        )
        await s.commit()


async def describe_inbound_message(account_id: str, message_id: int,
                                   prompt_seed: str = "",
                                   force: bool = False,
                                   refresh: bool = False) -> str | None:
    """Describe what a FAN sent in ONE inbound DM and cache it on messages.image_desc.

    Returns the description (also stored), or None when there's nothing to describe /
    it was capped / it failed. NEVER raises — a describe problem must not take down
    the ingest path (on_inbound_image) that awaits it.

    `force=True` re-runs the source over an already-described message and overwrites
    the cached read — the manual "re-read" from the chat bubble. The ingest hook never
    sets it (a webhook replay must stay idempotent).

    `refresh=True` re-fetches the frame from OF first, because stored CDN urls expire
    (see _fresh_frame). Costs one OF call, so only the manual path sets it — at ingest
    the stored signature is seconds old."""
    try:
        async with get_session() as s:
            row = (await s.execute(
                select(Message.fan_id, Message.raw_json, Message.image_desc)
                .where(Message.account_id == str(account_id),
                       Message.message_id == int(message_id))
            )).first()
        if row is None:
            return None
        fan_id, raw_json, existing = int(row.fan_id), row.raw_json, row.image_desc
        if existing and not force:
            return existing

        raw = load_dict(raw_json)
        if refresh:
            # Signed urls expire; a stale one downloads an error page, not a picture.
            raw = await _fresh_frame(account_id, fan_id, message_id) or raw
        media = raw.get("media") if isinstance(raw.get("media"), list) else []

        # A gif-only dm has no media, so the free title lane IS its only source; a
        # media dm has nothing Giphy could name. Mutually exclusive by payload
        # shape (one classifier, shared with the ingest dispatch) — this PICKS the
        # source, it doesn't fall back from one to the other.
        giphy_id = giphy_dm_id(raw)
        if giphy_id:
            desc = await _giphy_desc(giphy_id)
            source = "giphy"
        else:
            urls, from_video = _inbound_still_urls(media)
            desc = (await _vision_desc(account_id, fan_id, message_id, urls, prompt_seed)
                    if urls else None)
            source = "vision"
            if desc and from_video:
                # OF's poster frame is ONE moment of a clip. Say so, or she reacts to
                # a video as if it were a photo — and a clip is worth more excitement,
                # not less. The model only ever saw the still, so don't let the line
                # imply she watched it.
                desc = f"a video (this is one frame of it): {desc}"
        if not desc:
            return None

        await _store_image_desc(account_id, message_id, desc)
        log.info("inbound_described account=%s fan=%s msg=%s source=%s len=%d",
                 account_id, fan_id, message_id, source, len(desc))
        return desc
    except Exception:  # noqa: BLE001
        log.warning("describe_inbound_message_failed account=%s msg=%s",
                    account_id, message_id, exc_info=True)
        return None


async def redescribe_inbound(account_id: str, message_id: int,
                             force: bool = False) -> str | None:
    """Describe one inbound message ON DEMAND (the chat bubble's 👁 / ↻ button).

    Same work as the ingest hook, minus the hook's gating — the operator clicking
    the button IS the decision.

    Owns the config read so callers (the chat-pane endpoint) don't have to know the
    emphasis seed lives in tip_reward's config blob. That location is historical:
    `image_describe_prompt` is a DESCRIBE knob filed under tip_reward, which is why
    this module has to reach into an automation to read its own feature's setting.
    If that config ever moves, this function is the only place to change."""
    from automations.tip_reward import image_describe_flags  # lazy: avoid cycle

    _on, seed, _scope = await image_describe_flags(account_id)
    return await describe_inbound_message(account_id, message_id, seed, force=force,
                                          refresh=True)
