"""service/automations/online_blast.py — broadcast to EVERY fan online now.

The SCALE sibling of mass_nudge. The two are deliberately different tools:

  • mass_nudge  — resolves the online ids and sends explicit `userIds`, so it
    keeps a precise per-fan 12h cooldown (NudgeState). Caps out around a few
    thousand online because it has to enumerate them (page the online list).
  • online_blast (THIS) — does ONE OnlyFans list-broadcast to the whole online
    audience: `send_mass_message(userLists=["fans"], online_only=True)`. OF
    resolves + fans out server-side in a SINGLE call, so it scales to 100k-fan
    accounts with tens of thousands online. The price: NO per-fan memory.

Dedup is OF-native exclusion of recent conversations, BOTH directions — fans we
DMed (`direction="out"`) OR who messaged us (`direction="in"`, written by the
WS/webhook pump) inside the window are passed in `excludedUsers`, so active
threads aren't blasted. With no per-fan state, the CADENCE is the de-facto
cooldown: run this hourly (or on a few-hour interval), not every few minutes.

Config (steps_json):
  with_image            true   # attach the slot image (one, rotated)
  exclude_replied_hours 8      # skip fans we DMed in last N hrs (0 = off)
  exclude_inbound_hours 8      # skip fans who messaged US in last N hrs (0 = off)
  unsend_after_hours    1      # auto-unsend the broadcast after N hrs (0 = keep)
  dry_run               false  # compose + resolve exclusions, send nothing
  slots                 {...}  # day-bucket → slot → {text:[...], image:[...]}

This is yesterday's original mass_nudge (a pure list-broadcast) brought back as
its own automation, plus the inbound-direction exclusion.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

import automation_executor as ax
from audiences import recent_chat_fan_ids
from automation_registry import register
from . import mass_nudge   # reuse slot composition + the default pools
from . import send_welcome  # _model_hour / _model_weekday / _slot_key

log = logging.getLogger("of-relay.automation.online_blast")

# Identical slot composition to mass_nudge — reuse its preview for the Settings UI.
preview_compose = mass_nudge.preview_compose


async def _excluded_ids(account_id: str, cfg: dict) -> list[int]:
    """Fan ids to exclude: recent OUTBOUND (we DMed) ∪ recent INBOUND (they
    messaged us), each within its own window. Empty when both windows are off."""
    excl: set[int] = set()
    out_h = cfg.get("exclude_replied_hours")
    if isinstance(out_h, (int, float)) and out_h > 0:
        excl |= set(await recent_chat_fan_ids(
            account_id, hours=float(out_h), direction="out"))
    in_h = cfg.get("exclude_inbound_hours")
    if isinstance(in_h, (int, float)) and in_h > 0:
        excl |= set(await recent_chat_fan_ids(
            account_id, hours=float(in_h), direction="in"))
    return sorted(excl)


@register("online_blast")
async def run(account_id: str, payload: dict, *, run_id: int) -> dict:
    cfg = {"with_image": True, **(payload or {})}
    off = await mass_nudge._utc_offset(account_id)
    hour = send_welcome._model_hour(off)
    slot = send_welcome._slot_key(hour)
    weekday = send_welcome._model_weekday(off)

    slots = cfg.get("slots") or mass_nudge._DEFAULT_SLOTS
    pool = mass_nudge._pick(slots, slot, weekday)
    texts = pool.get("text") or []
    if not texts:
        return {"sent": 0, "skipped": "no_text", "slot": slot}

    idx = mass_nudge._rotation_idx(len(texts))
    text = str(texts[idx])  # NO placeholder substitution — generic broadcast

    media: list[int] = []
    if cfg.get("with_image", True):
        imgs = pool.get("image") or []
        if imgs:
            media = [int(imgs[idx % len(imgs)])]

    excluded = await _excluded_ids(account_id, cfg)

    if cfg.get("dry_run"):
        return {"dry_run": True, "slot": slot, "variation_idx": idx, "text": text,
                "image_attached": bool(media), "excluded": len(excluded)}

    client = await asyncio.to_thread(ax._make_client, account_id)
    try:
        result = await asyncio.to_thread(lambda: client.send_mass_message(
            text,
            user_lists=["fans"],
            online_only=True,
            media_files=media,
            excluded_users=excluded or None,
        ))
    except Exception as e:
        log.warning("online_blast send failed account=%s", account_id, exc_info=True)
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
