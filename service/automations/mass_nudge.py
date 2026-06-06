"""service/automations/mass_nudge.py — broadcast a time-of-day nudge to everyone
online right now.

The high-traffic sibling of nudge_online. Instead of a personalized, delayed,
per-fan DM (one fire-job per fan — heavy when hundreds come online), this sends
ONE mass broadcast to fans online at send time, with generic text + image chosen
by time-of-day / day-of-week. No personalization (no {name}), no per-fan state.

Config lives ENTIRELY in the automation_rules payload (steps_json) — there's no
per-fan state to persist, so no extra table/column:

  payload = {
    "with_image": true,                 # attach the slot's image (one, rotated)
    "exclude_replied_hours": 6,          # skip fans we DMed in the last N hours
    "unsend_after_hours": 8,             # auto-unsend the broadcast after N hours
    "online_only": true,                 # (default true) target fans online now
    "dry_run": false,                    # compose + resolve, send nothing
    "slots": { "default": { "evening": { "text": [...], "image": [...] } }, ... }
  }

Cadence is the rule's `every_seconds` (e.g. 3600 = hourly). The executor's
one-job-per-(account,kind) guard + run_once lock prevent overlap, so frequency =
cadence; no extra cooldown needed. Line rotation is time-derived (epoch hour) so
it varies without storing an index.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from sqlalchemy import select

import automation_executor as ax
from audiences import recent_chat_fan_ids
from automation_registry import register
from db.engine import get_session
from db.models import AccountAiConfig
from . import send_welcome  # _model_hour / _model_weekday / _slot_key

log = logging.getLogger("of-relay.automation.mass_nudge")


def _pick(slots: dict, slot: str, weekday_name: str) -> dict:
    """Day-bucket lookup (per-day → weekend/weekday → default) for this slot.
    Returns {} when nothing is configured — NO {name} fallback (mass = generic,
    no personalization), so an empty slot cleanly skips instead of sending a
    literal '{name}'."""
    wd = (weekday_name or "").lower()
    is_weekend = wd in ("saturday", "sunday")
    for bucket in (wd, "weekend" if is_weekend else "weekday", "default"):
        b = slots.get(bucket)
        if isinstance(b, dict) and isinstance(b.get(slot), dict) and b[slot].get("text"):
            return b[slot]
    return {}

# Generic, no-name default pools (mass = no per-fan personalization).
_DEFAULT_SLOTS: dict = {
    "default": {
        "morning_1": {"text": [
            "morning loves ☀️ who's up early? 👀",
            "good morning 💋 come start the day with me",
        ], "image": []},
        "morning_2": {"text": [
            "heyy 🌸 online this morning? say hi 👀",
            "mid-morning check-in 😏 who's around?",
        ], "image": []},
        "afternoon_1": {"text": [
            "afternoon 😘 who's online? keep me company",
            "midday already 👀 what are you all up to",
        ], "image": []},
        "afternoon_2": {"text": [
            "bored this afternoon? 😏 come chat",
            "perfect timing 💕 i'm online right now",
        ], "image": []},
        "evening": {"text": [
            "evening everyone 🍷 who's online? 👀",
            "online tonight? come unwind with me 😉",
        ], "image": []},
        "night": {"text": [
            "up late? 🌙😏 i'm still here",
            "late night crew 👀 who's awake with me",
        ], "image": []},
    },
    "weekend": {
        "evening": {"text": [
            "weekend vibes 🍷🔥 who's online tonight?",
            "it's the weekend 😈 come play",
        ], "image": []},
    },
}


def _rotation_idx(n: int) -> int:
    """Time-derived index so the line rotates each hour without stored state."""
    if n <= 0:
        return 0
    return int(datetime.utcnow().timestamp() // 3600) % n


async def _utc_offset(account_id: str) -> int:
    async with get_session() as s:
        off = (await s.execute(
            select(AccountAiConfig.utc_offset).where(AccountAiConfig.account_id == str(account_id))
        )).scalar_one_or_none()
    try:
        return int(off or 0)
    except (TypeError, ValueError):
        return 0


async def preview_compose(account_id: str, payload: dict, *, hour: int | None = None) -> dict:
    """Compose the broadcast the Settings UI would send for a chosen hour — NO send.
    Returns {text, slot, media, hour, lines}."""
    cfg = {"with_image": True, **(payload or {})}
    off = await _utc_offset(account_id)
    h = int(hour) % 24 if hour is not None else send_welcome._model_hour(off)
    slot = send_welcome._slot_key(h)
    weekday = send_welcome._model_weekday(off)
    slots = cfg.get("slots") or _DEFAULT_SLOTS
    pool = _pick(slots, slot, weekday)
    texts = pool.get("text") or []
    if not texts:
        return {"text": "", "slot": slot, "media": [], "hour": h, "lines": 0}
    idx = _rotation_idx(len(texts))
    media: list[int] = []
    if cfg.get("with_image", True):
        imgs = pool.get("image") or []
        if imgs:
            media = [int(imgs[idx % len(imgs)])]
    return {"text": str(texts[idx]), "slot": slot, "media": media,
            "hour": h, "lines": len(texts)}


@register("mass_nudge")
async def run(account_id: str, payload: dict, *, run_id: int) -> dict:
    cfg = {"with_image": True, "online_only": True, **(payload or {})}
    off = await _utc_offset(account_id)
    hour = send_welcome._model_hour(off)
    slot = send_welcome._slot_key(hour)
    weekday = send_welcome._model_weekday(off)

    slots = cfg.get("slots") or _DEFAULT_SLOTS
    pool = _pick(slots, slot, weekday)
    texts = pool.get("text") or []
    if not texts:
        return {"sent": 0, "skipped": "no_text", "slot": slot}

    idx = _rotation_idx(len(texts))
    text = str(texts[idx])  # NO placeholder substitution — generic broadcast

    media: list[int] = []
    if cfg.get("with_image", True):
        imgs = pool.get("image") or []
        if imgs:
            media = [int(imgs[idx % len(imgs)])]

    # Optional exclusion: don't blast fans we DMed recently (active threads).
    excluded: list[int] = []
    excl_h = cfg.get("exclude_replied_hours")
    if isinstance(excl_h, (int, float)) and excl_h > 0:
        excluded = list(await recent_chat_fan_ids(
            account_id, hours=float(excl_h), direction="out"))

    if cfg.get("dry_run"):
        return {"dry_run": True, "slot": slot, "variation_idx": idx,
                "text": text, "image_attached": bool(media),
                "excluded": len(excluded), "online_only": bool(cfg.get("online_only", True))}

    client = await asyncio.to_thread(ax._make_client, account_id)
    try:
        result = await asyncio.to_thread(lambda: client.send_mass_message(
            text,
            user_lists=["fans"],
            online_only=bool(cfg.get("online_only", True)),
            media_files=media,
            excluded_users=excluded or None,
        ))
    except Exception as e:
        log.warning("mass_nudge send failed account=%s", account_id, exc_info=True)
        return {"sent": 0, "skipped": "error", "error": repr(e)[:200], "slot": slot}

    queue_id = result.get("id") if isinstance(result, dict) else None

    # Optional auto-unsend: enqueue the A12 unsend job for the broadcast.
    unsend_h = cfg.get("unsend_after_hours")
    unsend_job = None
    if isinstance(unsend_h, (int, float)) and unsend_h > 0 and queue_id:
        unsend_job = await ax.enqueue_job(
            account_id, "unsend_messages",
            payload={"targets": [{"queue_id": int(queue_id)}]},
            run_at=datetime.utcnow() + timedelta(hours=float(unsend_h)))

    return {
        "sent": 1,
        "queue_id": queue_id,
        "slot": slot,
        "variation_idx": idx,
        "text_preview": text[:80],
        "image_attached": bool(media),
        "excluded": len(excluded),
        "unsend_job": unsend_job,
    }
