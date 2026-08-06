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
    "exclude_replied_hours": 12,         # cooldown: skip fans nudged/DMed in last N hrs (absent → 12, 0 = off)
    "exclude_inbound_hours": 12,         # skip fans who MESSAGED US in last N hrs (absent → 12, 0 = off)
    "excluded_users": [123, ...],        # explicit fan ids to never nudge (optional)
    "max_online": 500,                   # cap the online scan per run
    "unsend_after_hours": 8,             # auto-unsend the broadcast after N hours
    "dry_run": false,                    # resolve audience, send nothing, no bump
    "slots": { "default": { "evening": { "text": [...], "image": [...] } }, ... }
  }

Audience dedup is `audiences.contact_guard_excludes` — the shared cross-
automation guard (messages-out ∪ NudgeState ∪ messages-in), so a fan DMed by
ai_chat, blasted with explicit ids, or nudged by nudge_online inside the window
is skipped here too.

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
from audiences import (
    MASSDMEXCLUDE_LIST,
    contact_guard_excludes,
    exclude_list_fan_ids,
    resolve_window_hours,
)
from automation_registry import register
from ._common import load_hard_skip_ids
from . import _customs
from db.engine import get_session
from db.models import AccountAiConfig, NudgeState
from . import send_welcome  # _model_hour / _model_weekday / _slot_key

from ._common import load_voice_blocks

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


# ── The MALE slots ───────────────────────────────────────────────────────────
# A mass nudge goes to MANY fans at once but each one reads it inside a 1:1
# thread, so nothing here may address a crowd. Hers get away with "morning loves"
# and "who's online?" because the plural reads as a broadcast persona; from a dom
# it reads as a man talking to a room the fan is not in. His are singular and
# direct — he is messaging one person, and the fan has no way to know otherwise.
#
# Same slot keys and the same TWO lines per slot: `_pick_slot` indexes these, so a
# missing slot is a silent no-send and a different count changes the rotation.
_DEFAULT_SLOTS_HIM: dict = {
    "default": {
        "morning_1": {"text": ["up already 😏", "morning. talk to me"], "image": []},
        "morning_2": {"text": ["you around this morning 👀", "still in bed or up?"], "image": []},
        "afternoon_1": {"text": ["afternoon. what are you doing", "you free 😏"], "image": []},
        "afternoon_2": {"text": ["bored? come here", "im around if you are"], "image": []},
        "evening": {"text": ["evening. trainings done, im free 🔥", "you on tonight?"], "image": []},
        "night": {"text": ["up late. im still here", "cant sleep either 😏"], "image": []},
    },
    "weekend": {
        "evening": {"text": ["weekends yours. what are you doing with it 🔥",
                             "its the weekend 😈 come here"], "image": []},
    },
}


def _slots_for(cfg: dict, voice: str) -> dict:
    """Operator slots always win. Otherwise the LANE's built-ins — a male account
    with no configured slots must not fall through to "morning loves"."""
    return cfg.get("slots") or (
        _DEFAULT_SLOTS_HIM if str(voice or "").strip().lower() == "him"
        else _DEFAULT_SLOTS)


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
    slots = _slots_for(cfg, (await load_voice_blocks(account_id)).voice)
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

    slots = _slots_for(cfg, (await load_voice_blocks(account_id)).voice)
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
    # Guard windows (hrs): absent → default 12; explicit 0 → off (re-blast).
    window = resolve_window_hours(
        cfg.get("exclude_replied_hours"), _DEFAULT_COOLDOWN_HOURS)
    in_window = resolve_window_hours(
        cfg.get("exclude_inbound_hours"), _DEFAULT_COOLDOWN_HOURS)
    max_online = int(cfg.get("max_online") or _DEFAULT_MAX_ONLINE)

    client = await asyncio.to_thread(ax._make_client, account_id)

    # 1) Who's online right now (resolved locally so we can dedup + record).
    online = await _resolve_online_ids(client, max_online)
    if not online:
        return {"sent": 0, "skipped": "none_online", "slot": slot,
                "online": 0, "excluded": 0}

    # 2) Drop fans inside the guard window — the shared cross-automation set:
    #    nudged (NudgeState, shared with nudge_online), DMed through ANY path
    #    (messages-out: 1:1 senders, chatters, explicit-id blasts), or actively
    #    messaging us (messages-in), plus any explicit excluded_users.
    extra = [int(x) for x in (cfg.get("excluded_users") or [])
             if str(x).lstrip("-").isdigit()]
    excl = await contact_guard_excludes(
        account_id, outbound_hours=window, inbound_hours=in_window,
        extra_ids=extra,
    )
    # Durably restricted fans (muted peer-creator / hand-restricted) never get a
    # broadcast either — fold them into the exclusion set.
    excl |= await load_hard_skip_ids(account_id)
    # A fan owed an undelivered custom gets no broadcast either. He has paid and is
    # waiting; a "hey you up 😏" from the account that owes him work reads as being
    # ignored, and the nudge lane is a proactive TOUCH he did not ask for. The chat
    # engines already stand down for him (`_customs.is_owed`) — they hold a Fan
    # row, and this audience path never does.
    excl |= await _customs.owed_fan_ids(account_id)
    # MASSDMEXCLUDE members — mass_nudge is a mass DM, so it honors the DM opt-out.
    excl |= await exclude_list_fan_ids(account_id, MASSDMEXCLUDE_LIST)
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
