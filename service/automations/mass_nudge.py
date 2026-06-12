"""service/automations/mass_nudge.py — broadcast a time-of-day nudge to everyone
online right now.

The high-traffic sibling of nudge_online. Instead of a personalized, delayed,
per-fan DM (one fire-job per fan — heavy when hundreds come online), this sends
ONE generic broadcast to fans online at send time, with text + image chosen by
time-of-day / day-of-week. No personalization (no {name}).

PER-FAN COOLDOWN (why this resolves ids instead of a pure list-broadcast):
running every few minutes would re-blast the same online fans every cycle. A
list-audience broadcast (`userLists=["fans"]` + online filter) can't prevent
that — it leaves NO per-fan outbound row to dedup against (the optimistic writer
only covers explicit `userIds`; the WS pump skips outbound; the list reconciler
is a TODO). So mass_nudge resolves the online ids itself, drops anyone nudged
(or DMed) inside the `exclude_replied_hours` window, sends to the survivors as
EXPLICIT `userIds`, and stamps NudgeState.last_nudged_at for exactly those
recipients. Run 2 inside the window therefore sends 0. The NudgeState cooldown
is SHARED with nudge_online, so a fan isn't double-nudged across both.

Config lives in the automation_rules payload (steps_json):

  payload = {
    "with_image": true,                 # attach the slot's image (one, rotated)
    "exclude_replied_hours": 12,         # cooldown: skip fans nudged/DMed in last N hrs
    "exclude_inbound_hours": 12,         # skip fans who MESSAGED US in last N hrs (repliers)
    "max_online": 500,                   # cap the online scan per run
    "unsend_after_hours": 8,             # auto-unsend the broadcast after N hours
    "dry_run": false,                    # resolve audience, send nothing, no bump
    "slots": { "default": { "evening": { "text": [...], "image": [...] } }, ... }
  }

Cadence is the rule's `every_seconds` (e.g. 300 = every 5 min). The executor's
one-job-per-(account,kind) guard + run_once lock prevent overlap. Line rotation
is time-derived (epoch hour) so it varies without storing an index.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

import automation_executor as ax
from audiences import recent_chat_fan_ids
from automation_registry import register
from db.engine import get_session
from db.models import AccountAiConfig, NudgeState
from . import send_welcome  # _model_hour / _model_weekday / _slot_key

log = logging.getLogger("of-relay.automation.mass_nudge")

_DEFAULT_COOLDOWN_HOURS = 12   # default re-nudge window if the rule omits it
_DEFAULT_MAX_ONLINE = 500      # default cap on the per-run online scan


async def _resolve_online_ids(client, max_online: int) -> list[int]:
    """Online-now fan ids via OF's native online filter (deduped, capped)."""
    rows = await asyncio.to_thread(
        lambda: client.iter_online_subscribers(max_fans=int(max_online)))
    out: list[int] = []
    seen: set[int] = set()
    for f in (rows or []):
        if not isinstance(f, dict):
            continue
        try:
            fid = int(f.get("id"))
        except (TypeError, ValueError):
            continue
        if fid not in seen:
            seen.add(fid)
            out.append(fid)
    return out


async def _recently_nudged_ids(
    account_id: str, fan_ids: list[int], window_hours: float, now: datetime,
) -> set[int]:
    """Subset of `fan_ids` whose NudgeState.last_nudged_at is within the window."""
    if not fan_ids or window_hours <= 0:
        return set()
    cutoff = now - timedelta(hours=float(window_hours))
    async with get_session() as s:
        rows = (await s.execute(
            select(NudgeState.fan_id).where(
                NudgeState.account_id == str(account_id),
                NudgeState.fan_id.in_([int(x) for x in fan_ids]),
                NudgeState.last_nudged_at.is_not(None),
                NudgeState.last_nudged_at >= cutoff,
            )
        )).scalars().all()
    return {int(r) for r in rows}


async def _bump_nudged(
    account_id: str, fan_ids: list[int], slot: str, now: datetime,
) -> None:
    """Stamp last_nudged_at / last_seen / nudge_count for the recipients so the
    next run inside the window skips them. Shares NudgeState with nudge_online."""
    if not fan_ids:
        return
    async with get_session() as s:
        for fid in fan_ids:
            await s.execute(
                sqlite_insert(NudgeState)
                .values(account_id=str(account_id), fan_id=int(fid),
                        last_nudged_at=now, last_seen_online_at=now,
                        nudge_count=1, last_slot=str(slot), updated_at=now)
                .on_conflict_do_update(
                    index_elements=["account_id", "fan_id"],
                    set_={"last_nudged_at": now, "last_seen_online_at": now,
                          "nudge_count": func.coalesce(NudgeState.nudge_count, 0) + 1,
                          "last_slot": str(slot), "updated_at": now})
            )


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
    cfg = {"with_image": True, **(payload or {})}
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

    now = datetime.utcnow()
    # Cooldown window (hrs): absent → default 12; explicit 0 → off (re-blast).
    excl_h = cfg.get("exclude_replied_hours")
    if excl_h is None:
        window = float(_DEFAULT_COOLDOWN_HOURS)
    else:
        try:
            window = max(0.0, float(excl_h))
        except (TypeError, ValueError):
            window = float(_DEFAULT_COOLDOWN_HOURS)
    max_online = int(cfg.get("max_online") or _DEFAULT_MAX_ONLINE)

    client = await asyncio.to_thread(ax._make_client, account_id)

    # 1) Who's online right now (resolved locally so we can dedup + record).
    online = await _resolve_online_ids(client, max_online)
    if not online:
        return {"sent": 0, "skipped": "none_online", "slot": slot,
                "online": 0, "excluded": 0}

    # 2) Drop fans inside the cooldown window: nudged recently (NudgeState,
    #    shared with nudge_online) OR DMed recently through any path.
    excl: set[int] = await _recently_nudged_ids(account_id, online, window, now)
    excl |= set(await recent_chat_fan_ids(
        account_id, hours=window, direction="out"))
    # Also skip fans who MESSAGED US recently (inbound, from the WS/webhook
    # pump) — they're already engaged, don't blast an active replier.
    in_h = cfg.get("exclude_inbound_hours")
    if isinstance(in_h, (int, float)) and in_h > 0:
        excl |= set(await recent_chat_fan_ids(
            account_id, hours=float(in_h), direction="in"))
    recipients = [fid for fid in online if fid not in excl]

    if cfg.get("dry_run"):
        return {"dry_run": True, "slot": slot, "variation_idx": idx,
                "text": text, "image_attached": bool(media),
                "online": len(online), "excluded": len(online) - len(recipients),
                "recipients": len(recipients), "window_hours": window}

    if not recipients:
        # Everyone online was nudged/DMed inside the window — the dedup working.
        return {"sent": 0, "skipped": "all_in_cooldown", "slot": slot,
                "online": len(online), "excluded": len(online),
                "recipients": 0, "window_hours": window}

    try:
        result = await asyncio.to_thread(lambda: client.send_mass_message(
            text,
            included_users=recipients,
            media_files=media,
        ))
    except Exception as e:
        log.warning("mass_nudge send failed account=%s", account_id, exc_info=True)
        return {"sent": 0, "skipped": "error", "error": repr(e)[:200], "slot": slot}

    # 3) Stamp the cooldown for exactly the fans we sent to.
    await _bump_nudged(account_id, recipients, slot, now)

    queue_id = result.get("id") if isinstance(result, dict) else None

    # Attribute this broadcast to mass_nudge in the Mass Messages tab (no
    # per-fan messages rows are written, so this is the only attribution link).
    from attribution import record_broadcast_mass_run
    await record_broadcast_mass_run(
        account_id=account_id, queue_id=queue_id,
        automation_kind="mass_nudge", recipient_count=len(recipients),
    )

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
        "online": len(online),
        "recipients": len(recipients),
        "excluded": len(online) - len(recipients),
        "window_hours": window,
        "unsend_job": unsend_job,
    }
