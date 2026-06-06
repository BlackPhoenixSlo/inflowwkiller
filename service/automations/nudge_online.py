"""service/automations/nudge_online.py — message a fan when they come online.

Spec: library/NUDGE_ONLINE_PLAN.md. Two automations cooperate:

  • nudge_online (THIS file) — the interval DETECTOR (every ~60s). Fetches the
    online set via of_client.iter_online_subscribers (OF's native
    filter[online]=1), diffs it against per-fan NudgeState.last_seen_online_at to
    find `newly_online`, runs the cheap-first gate sequence, and ENQUEUES a
    delayed `nudge_online_fire` one-shot per qualifying fan. It sends NOTHING
    itself.
  • nudge_online_fire (sibling file) — the one-shot that RE-VALIDATES at fire
    time and actually sends.

Three load-bearing behaviours (the sanity-pass fixes):
  1. WARM-UP — on the first tick for an account (no NudgeState rows yet) we seed
     last_seen for every online fan and send nothing, so enabling the automation
     doesn't stampede every already-online fan.
  2. CAP DEFERS, never drops — only the first `max_per_tick` newly-online fans are
     handled; the overflow's last_seen is deliberately left STALE so they
     re-qualify next tick (touch last_seen only for online MINUS overflow).
  3. Fully async — nudge NEVER sets the shared fans.automation_paused_until
     cooldown; it gates only on its own NudgeState cap, so of_ai_chat/deep_convo
     are never frozen by a nudge. The fan_lease is taken only at fire.

This module also owns the shared config / gate / compose helpers that the
fire-job imports (so the gate sequence and content composition are defined once).
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

import automation_executor as ax  # enqueue_job / _make_client / lease seams
from automation_registry import register
from db.engine import get_session
from db.models import (
    AccountAiConfig, Blacklist, Fan, FanLease, Message, NudgeState,
    SkipList, WelcomeSent,
)
from . import send_welcome  # reuse _slot_key / _resolve_welcome_name / _model_hour
from ._common import substitute_placeholders

log = logging.getLogger("of-relay.automation.nudge_online")


# ── Built-in config (per-account override via account_ai_config.nudge_config_json) ──
# The seed tease pools are intentionally generous per slot so repeat_mode=new
# rotation doesn't loop quickly. Operators expand these via the Settings UI (P3).
_DEFAULT_NUDGE_CONFIG: dict = {
    "enabled": True,
    # ── Two-part message model (redesign) ──────────────────────────────
    # A nudge can send up to TWO messages in order:
    #   1) the NUDGE LINE — a time-of-day greeting from the slot pools, with the
    #      slot image. Set nudge_line_text=False for an image-only opener.
    #   2) the INFO LINE — a second line from the fan's profile: a Q&A question or
    #      a tease (info_line_mode), text by default (no image).
    # Each part is independently toggled, and the image can ride either part.
    "send_nudge_line": True,
    "nudge_line_text": True,      # False → image-only opener (no text)
    "nudge_line_image": True,     # attach the slot image to the nudge line
    "send_info_line": True,
    "info_line_mode": "mix",      # qa | tease | mix (qa→tease fallback)
    "info_line_image": False,     # attach the slot image to the info line too
    "repeat_mode": "new",         # new (rotate) | same (resend prior line)
    "delay_minutes": 4,
    "jitter_minutes": 3,
    "gap_minutes": 5,             # online-gap before a fan re-qualifies as "newly online"
    "min_hours_between_nudges": 12,
    "max_per_tick": 25,
    "max_online_scan": 200,       # how many online fans one tick pulls
    "online_recent_minutes": 5,   # fire-time recency re-validate window
    "quiet_hours": [0, 7],        # creator-local [start, end) — no nudges in this band
    # Gate knobs (own defaults; tune via config):
    "require_welcomed": True,     # only nudge fans we've already welcomed
    "welcome_grace_hours": 1,     # …and not within this many hours (welcome+nudge collision)
    "active_convo_hours": 6,      # recent outbound / unanswered inbound ⇒ mid-thread, skip
    "max_no_reply": 3,            # stop after this many nudges with no reply since
    "slots": {
        "default": {
            "morning_1": {"text": [
                "morning sleepyhead {name} ☀️ you're up early 👀",
                "mmm {name} caught you first thing 😏 coffee w me?",
                "good morning {name} 💋 dreamt about you",
                "look who's up early 👀 morning {name}",
                "hii {name} 🌞 you + me + coffee? perfect start",
                "{name} 😘 早 you're online before me almost!",
            ], "image": []},
            "morning_2": {"text": [
                "heyy {name} 🌸 busy morning? thinking of you",
                "{name} you're online 👀 make my morning?",
                "mid-morning check-in {name} 😏 miss me yet?",
                "hi {name} 💕 sneaking on before lunch?",
                "{name} 🌼 perfect timing, was just thinking about you",
                "good late-morning {name} 😘 come say hi",
            ], "image": []},
            "afternoon_1": {"text": [
                "afternoon {name} 😘 break time? come keep me company",
                "hey you 👀 {name} sneaking on midday? naughty",
                "{name} 💕 lunch break? spend it with me 😏",
                "caught you {name} 😏 afternoon trouble already?",
                "hii {name} ☀️ how's your day going babe",
                "{name} 👀 online at the perfect moment again",
            ], "image": []},
            "afternoon_2": {"text": [
                "{name} 💕 perfect timing, was getting bored…",
                "caught you {name} 😏 what are you up to rn",
                "heyy {name} 👀 afternoon slump? let me fix that",
                "{name} 😘 you're online… dangerous combo",
                "mmm {name} 💋 come distract me from work",
                "hi {name} 🌅 winding down? me too soon",
            ], "image": []},
            "evening": {"text": [
                "evening {name} 🍷 finally relaxing? me too… alone 😉",
                "heyy {name} 👀 online tonight? been waiting for you",
                "{name} 💋 come unwind with me, had you on my mind",
                "good evening {name} 😏 the fun part of the day",
                "{name} 🌆 done with the day? come play",
                "mmm {name} 💕 perfect time, was hoping you'd pop on",
            ], "image": []},
            "night": {"text": [
                "up late {name}? 👀😏 can't sleep either…",
                "mmm {name} it's late 🌙 thinking naughty thoughts of you",
                "{name} 💕 just you and me online rn… what are you wearing",
                "still up {name}? 😈 i like the company",
                "late night {name} 🌙 my favorite time… and you're here",
                "{name} 👀 can't sleep? let's keep each other up 😏",
            ], "image": []},
        },
        "weekend": {
            "evening": {"text": [
                "happy friday {name} 🍷🔥 plans tonight? or me 😏",
                "weekend {name} 💋 you online means trouble… i like it",
                "{name} 🎉 weekend vibes, come celebrate with me",
            ], "image": []},
            "night": {"text": [
                "saturday night {name} 🌙 still up? come play 😈",
                "weekend night {name} 👀 no work tomorrow… let's misbehave",
            ], "image": []},
        },
        "friday": {
            "afternoon_2": {"text": [
                "friday feeling {name}? 😏 clocking off w me?",
                "almost weekend {name} 🍷 come start it early",
            ], "image": []},
        },
    },
}


# ── Config load (merged: defaults ← account_ai_config.nudge_config_json) ──

async def _load_nudge_config(account_id: str) -> dict:
    """Built-in defaults overlaid with the account's `nudge_config_json` (shallow:
    a provided `slots` replaces the default pools wholesale). Also pulls
    `utc_offset` + `time_images` off account_ai_config so the compose/quiet-hours
    helpers don't need a second read."""
    async with get_session() as s:
        cfg_row = await s.get(AccountAiConfig, str(account_id))
    merged = json.loads(json.dumps(_DEFAULT_NUDGE_CONFIG))  # deep copy
    utc_offset = 0
    time_images: dict = {}
    if cfg_row is not None:
        utc_offset = cfg_row.utc_offset or 0
        if cfg_row.time_images_json:
            try:
                time_images = json.loads(cfg_row.time_images_json) or {}
            except Exception:
                time_images = {}
        if cfg_row.nudge_config_json:
            try:
                user = json.loads(cfg_row.nudge_config_json) or {}
                if isinstance(user, dict):
                    merged.update(user)
            except Exception:
                log.warning("bad_nudge_config_json account=%s", account_id, exc_info=True)
    merged["utc_offset"] = utc_offset
    merged["time_images"] = time_images
    return merged


# ── Gate sequence (cheapest-first; mirrors send_welcome.py:522) ──────
# Runs at DETECT and is re-checked at FIRE (gate 4 especially — a convo may have
# started during the delay). fan_lease (gate 9) is taken only at fire.

def _in_quiet_hours(cfg: dict) -> bool:
    qh = cfg.get("quiet_hours")
    if not isinstance(qh, (list, tuple)) or len(qh) != 2:
        return False
    try:
        start, end = int(qh[0]), int(qh[1])
    except (TypeError, ValueError):
        return False
    if start == end:
        return False
    hour = send_welcome._model_hour(cfg.get("utc_offset") or 0)
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end  # wraps past midnight


async def _gate_skip_reason(
    account_id: str, fan_id: int, cfg: dict, now: datetime,
    *, fan_obj: dict | None = None, at_fire: bool = False,
) -> str | None:
    """Return a skip-reason string, or None if the fan passes every gate.

    1 is_bot · 2 blacklist/skiplist · 3 welcomed-grace · 4 active-convo (live
    lease / recent outbound / unanswered inbound) · 5 re-engagement cap · 6
    no-reply backoff · 7 muted (detect only — needs the online object) · 8 quiet
    hours. The fan_lease (9) is acquired by the fire-job, not here."""
    async with get_session() as s:
        fan = await s.get(Fan, (str(account_id), int(fan_id)))
        if fan is not None and fan.is_bot:
            return "is_bot"
        if await s.get(Blacklist, int(fan_id)) is not None:
            return "blacklist"
        if await s.get(SkipList, (str(account_id), int(fan_id))) is not None:
            return "skiplist"

        if cfg.get("require_welcomed", True):
            ws = await s.get(WelcomeSent, (str(account_id), int(fan_id)))
            if ws is None:
                return "not_welcomed"
            if ws.sent_at and (now - ws.sent_at) < timedelta(
                    hours=cfg.get("welcome_grace_hours", 1)):
                return "welcome_recent"

        # 4a: another automation holds this fan's send-lease → it's being handled.
        lease = await s.get(FanLease, (str(account_id), int(fan_id)))
        if lease is not None and lease.leased_until > now:
            return "active_lease"

        # 4b/4c: recent outbound, or an unanswered inbound (fan spoke last) → we'd
        # be barging into a live thread; a nudge there reads as a bot.
        acv = timedelta(hours=cfg.get("active_convo_hours", 6))
        last_out = (await s.execute(select(func.max(Message.created_at)).where(
            Message.account_id == str(account_id), Message.fan_id == int(fan_id),
            Message.direction == "out"))).scalar_one_or_none()
        last_in = (await s.execute(select(func.max(Message.created_at)).where(
            Message.account_id == str(account_id), Message.fan_id == int(fan_id),
            Message.direction == "in"))).scalar_one_or_none()
        if last_out is not None and (now - last_out) < acv:
            return "recent_outbound"
        if (last_in is not None and (now - last_in) < acv
                and (last_out is None or last_in > last_out)):
            return "unanswered_inbound"

        st = await s.get(NudgeState, (str(account_id), int(fan_id)))
        if st is not None:
            if st.last_nudged_at and (now - st.last_nudged_at) < timedelta(
                    hours=cfg.get("min_hours_between_nudges", 12)):
                return "reengagement_cap"
            # No-reply backoff: stop only if the streak is high AND they still
            # haven't replied since the last nudge (auto-recovers on a reply).
            if (st.no_reply_streak or 0) >= cfg.get("max_no_reply", 3):
                replied = (last_in is not None and st.last_nudged_at is not None
                           and last_in > st.last_nudged_at)
                if not replied:
                    return "no_reply_backoff"

    if not at_fire and isinstance(fan_obj, dict) and fan_obj.get("isMutedNotifications"):
        return "muted"
    if _in_quiet_hours(cfg):
        return "quiet_hours"
    return None


# ── Content composition (tease pools; qa is layered in P2) ───────────

def _pick_pool(slots: dict, slot: str, weekday_name: str) -> dict:
    """Day-bucket lookup order: per-day (e.g. 'friday') → weekend/weekday →
    default, for this time-slot. Always returns an entry with a non-empty
    'text' list (generic fallback if a slot is unconfigured)."""
    wd = (weekday_name or "").lower()
    is_weekend = wd in ("saturday", "sunday")
    for bucket in (wd, "weekend" if is_weekend else "weekday", "default"):
        b = slots.get(bucket)
        if isinstance(b, dict) and isinstance(b.get(slot), dict) and b[slot].get("text"):
            return b[slot]
    d = slots.get("default", {})
    if isinstance(d.get(slot), dict) and d[slot].get("text"):
        return d[slot]
    return {"text": ["hey {name} 👀 saw you pop online"], "image": []}


def _choose_idx(st, fan_id: int, pool_len: int, repeat_mode: str) -> int:
    """repeat_mode rotation. Never nudged (no last_slot) → deterministic seed off
    the fan id; `same` → reuse the last index; `new` → advance one (wraps)."""
    if pool_len <= 0:
        return 0
    if st is None or st.last_slot is None or st.last_variation_idx is None:
        return int(fan_id) % pool_len
    if repeat_mode == "same":
        return min(int(st.last_variation_idx), pool_len - 1)
    return (int(st.last_variation_idx) + 1) % pool_len


# qa content_mode (P2): ask about a fan fact. KNOWN facts first (warm small-talk
# off something we already know), else gather a MISSING one (of_ai_chat style),
# else the caller falls back to tease. Each ask is tracked in fans.questions_asked
# so we don't nag the same thing twice. Templates use only supported placeholders.
_QA_KNOWN = [
    ("pets",       "aww how's {pet} doing 🐾 miss that lil face"),
    ("hobbies",    "still into {hobby}? 😏 been thinking about that"),
    ("home_city",  "how's {city} treating you babe ☀️"),
    ("occupation", "how's the {job} grind today? 😘"),
]
_QA_MISSING = [
    ("home_city",  "where are you texting me from rn? 👀 curious"),
    ("hobbies",    "what do you get up to for fun babe? 😊"),
    ("occupation", "what do you do all day? 💼 wanna know you better"),
    ("his_age",    "how old are you btw? 😏 just curious"),
]


def _fan_has(fan, attr: str) -> bool:
    """True iff the fan has a usable value for `attr` (pets/recent_events JSON
    arrays count only when non-empty)."""
    if fan is None:
        return False
    v = getattr(fan, attr, None)
    if attr in ("pets", "recent_events"):
        return bool(v) and str(v).strip() not in ("", "[]", "{}")
    if isinstance(v, str):
        return bool(v.strip())
    return bool(v)


def _try_qa(fan, name: str) -> tuple[str, str] | None:
    """(text, qa_key) for a question to ask, or None to fall back to tease.
    qa_key is namespaced (`known:`/`ask:`) so the same fact isn't re-asked."""
    asked: set[str] = set()
    if fan is not None and getattr(fan, "questions_asked", None):
        try:
            asked = set(json.loads(fan.questions_asked) or [])
        except Exception:
            asked = set()
    for key, tmpl in _QA_KNOWN:
        if _fan_has(fan, key) and f"known:{key}" not in asked:
            return substitute_placeholders(tmpl, fan, name=name), f"known:{key}"
    for key, tmpl in _QA_MISSING:
        if not _fan_has(fan, key) and f"ask:{key}" not in asked:
            return substitute_placeholders(tmpl, fan, name=name), f"ask:{key}"
    return None


async def _record_question(account_id: str, fan_id: int, qa_key: str) -> None:
    """Append a qa_key to fans.questions_asked (no-nag tracker, shared with
    of_ai_chat). Upsert so a fan with no row yet is handled."""
    async with get_session() as s:
        fan = await s.get(Fan, (str(account_id), int(fan_id)))
        cur: list = []
        if fan is not None and fan.questions_asked:
            try:
                cur = json.loads(fan.questions_asked) or []
            except Exception:
                cur = []
        if qa_key not in cur:
            cur.append(qa_key)
        vals = {"questions_asked": json.dumps(cur), "updated_at": datetime.utcnow()}
        await s.execute(
            sqlite_insert(Fan)
            .values(account_id=str(account_id), fan_id=int(fan_id), **vals)
            .on_conflict_do_update(index_elements=["account_id", "fan_id"], set_=vals)
        )


class _SampleFan:
    """A stand-in fan for previews when no real fan is picked — so the preview
    shows personalization even on a brand-new account."""
    of_display_name = "Jack"
    of_username = "jack"
    real_name = "Jack"
    is_name_real = True
    home_city = "Ljubljana"
    his_age = "31"
    occupation = "chef"
    hobbies = "trail running"
    pets = '[{"kind": "dog", "name": "Rex"}]'
    questions_asked = None

    def __getattr__(self, _n):  # any other Fan column → None
        return None


def _slot_media(cfg: dict, pool: dict, hour: int, idx: int) -> list[int]:
    """One image id for a part: the slot pool's image (rotated), else the
    account's per-slot time_images id, else none."""
    imgs = pool.get("image") or []
    if imgs:
        return [int(imgs[idx % len(imgs)])]
    one = send_welcome._slot_image_id(cfg, hour)
    return [one] if one is not None else []


async def _compose_messages(
    account_id: str, fan_id: int | None, cfg: dict, fan_obj: dict | None, st,
    *, hour: int | None = None, randomize: bool = False,
) -> tuple[list[dict], str]:
    """Build the ordered list of messages a nudge would send, plus the slot.
    SIDE-EFFECT FREE (no questions_asked write — the caller records qa_key after a
    confirmed send). Each message: {kind, text, media, qa_key, idx}.

      • nudge line  — time-of-day greeting (+ image), unless toggled off / text off.
      • info line   — a Q&A or tease line from the fan's profile (+ image if enabled).

    `fan_id` None → a sample fan (for previews). `hour` overrides the slot clock.
    `randomize` → pick a RANDOM line + random qa/tease pick (the preview uses this
    so each click shows a different sample); real sends use repeat_mode rotation."""
    off = cfg.get("utc_offset") or 0
    h = (int(hour) if hour is not None else send_welcome._model_hour(off)) % 24
    slot = send_welcome._slot_key(h)
    weekday = send_welcome._model_weekday(off)

    if fan_id is not None:
        async with get_session() as s:
            fan = await s.get(Fan, (str(account_id), int(fan_id)))
        name = await send_welcome._resolve_welcome_name(account_id, int(fan_id), fan_obj or {})
        seed = int(fan_id)
    else:
        fan = _SampleFan()
        name = "Jack"
        seed = 0

    pool = _pick_pool(cfg.get("slots") or {}, slot, weekday)
    texts = pool.get("text") or []
    if not texts:
        nidx = 0
    elif randomize:
        nidx = random.randrange(len(texts))
    else:
        nidx = _choose_idx(st, seed, len(texts), cfg.get("repeat_mode", "new"))
    out: list[dict] = []

    # 1) Nudge line — greeting (+ image).
    if cfg.get("send_nudge_line", True):
        text = ""
        if cfg.get("nudge_line_text", True) and texts:
            text = substitute_placeholders(texts[nidx], fan, name=name)
        media = _slot_media(cfg, pool, h, nidx) if cfg.get("nudge_line_image", True) else []
        if text or media:
            out.append({"kind": "nudge", "text": text, "media": media, "qa_key": None, "idx": nidx})

    # 2) Info line — Q&A or tease from the fan's profile (text by default).
    if cfg.get("send_info_line", True):
        mode = cfg.get("info_line_mode", "mix")
        info_text = ""
        qa_key = None
        mix_qa = random.random() < 0.5 if randomize else (seed + h) % 2 == 0
        want_qa = mode == "qa" or (mode == "mix" and mix_qa)
        if want_qa:
            qa = _try_qa(fan, name)
            if qa is not None:
                info_text, qa_key = qa
        if not info_text and texts:
            # tease line — a different pool line than the nudge line where possible.
            if randomize and len(texts) > 1:
                tidx = random.choice([i for i in range(len(texts)) if i != nidx])
            elif cfg.get("send_nudge_line", True) and len(texts) > 1:
                tidx = (nidx + 1) % len(texts)
            else:
                tidx = nidx
            info_text = substitute_placeholders(texts[tidx], fan, name=name)
        media = _slot_media(cfg, pool, h, 0) if cfg.get("info_line_image", False) else []
        if info_text or media:
            out.append({"kind": "info", "text": info_text, "media": media,
                        "qa_key": qa_key, "idx": nidx})

    return out, slot


async def preview_compose(
    account_id: str, cfg: dict, *, fan_id: int | None = None, hour: int | None = None,
) -> dict:
    """Preview the full message list a nudge would send (NO send, NO state write).
    Randomized so each click shows a different sample line."""
    msgs, slot = await _compose_messages(account_id, fan_id, cfg, None, None,
                                         hour=hour, randomize=True)
    name = "Jack"
    if fan_id is not None:
        name = await send_welcome._resolve_welcome_name(
            account_id, int(fan_id), {"id": int(fan_id)}) or "(no name)"
    return {"slot": slot, "name": name,
            "messages": [{"kind": m["kind"], "text": m["text"], "media": m["media"]} for m in msgs]}


# ── Detector state helpers ────────────────────────────────────────────

async def _account_warmed(account_id: str) -> bool:
    """True once the account has ANY NudgeState row — the cheap warm-up flag
    (no extra column). The first tick that sees online fans seeds them silently."""
    async with get_session() as s:
        row = (await s.execute(
            select(NudgeState.fan_id).where(NudgeState.account_id == str(account_id)).limit(1)
        )).first()
    return row is not None


async def _upsert_last_seen(account_id: str, fan_ids: list[int], now: datetime) -> None:
    if not fan_ids:
        return
    async with get_session() as s:
        for fid in fan_ids:
            await s.execute(
                sqlite_insert(NudgeState)
                .values(account_id=str(account_id), fan_id=int(fid),
                        last_seen_online_at=now, updated_at=now)
                .on_conflict_do_update(
                    index_elements=["account_id", "fan_id"],
                    set_={"last_seen_online_at": now, "updated_at": now})
            )


async def _load_states(account_id: str, fan_ids: list[int]) -> dict[int, NudgeState]:
    if not fan_ids:
        return {}
    async with get_session() as s:
        rows = (await s.execute(
            select(NudgeState).where(
                NudgeState.account_id == str(account_id),
                NudgeState.fan_id.in_([int(x) for x in fan_ids]))
        )).scalars().all()
    return {int(r.fan_id): r for r in rows}


async def _set_pending(account_id: str, fan_id: int, job_id: int, now: datetime) -> None:
    async with get_session() as s:
        await s.execute(
            sqlite_insert(NudgeState)
            .values(account_id=str(account_id), fan_id=int(fan_id),
                    pending_job_id=int(job_id), last_seen_online_at=now, updated_at=now)
            .on_conflict_do_update(
                index_elements=["account_id", "fan_id"],
                set_={"pending_job_id": int(job_id), "updated_at": now})
        )


# ── The detector ─────────────────────────────────────────────────────

@register("nudge_online")
async def run(account_id: str, payload: dict, *, run_id: int) -> dict:
    payload = payload or {}
    cfg = await _load_nudge_config(account_id)
    if not cfg.get("enabled", True):
        return {"enabled": False, "online": 0, "enqueued": 0}

    limit = int(payload.get("limit") or cfg.get("max_online_scan") or 200)
    with_image = payload.get("with_image", cfg.get("with_image", True))

    # Test/live scope: pin to exactly ONE fan, bypassing the online scan. Used by
    # the jaka<->Lexi live driver so a real run can never target anyone else.
    # `test_force` additionally skips the gates (welcomed/active-convo) for a clean
    # one-shot; the online-recency re-validate still runs (we seed last_seen now).
    test_fan = payload.get("test_fan")
    if test_fan is not None:
        try:
            fid = int(test_fan)
        except (TypeError, ValueError):
            return {"test_fan": test_fan, "enqueued": 0, "skipped": "bad_test_fan"}
        now = datetime.utcnow()
        force = bool(payload.get("test_force"))
        await _upsert_last_seen(account_id, [fid], now)
        if not force:
            reason = await _gate_skip_reason(account_id, fid, cfg, now, at_fire=False)
            if reason:
                return {"test_fan": fid, "enqueued": 0, "skipped": reason}
        delay = int(cfg.get("delay_minutes", 4))
        jitter = int(cfg.get("jitter_minutes", 3))
        jit = random.randint(0, jitter * 60) if jitter > 0 else 0
        run_at = now + timedelta(minutes=delay, seconds=jit)
        job_id = await ax.enqueue_job(
            account_id, "nudge_online_fire",
            payload={"fan_id": fid, "with_image": bool(with_image), "test_force": force},
            run_at=run_at)
        await _set_pending(account_id, fid, job_id, now)
        return {"test_fan": fid, "enqueued": 1, "job_id": job_id}

    client = await asyncio.to_thread(ax._make_client, account_id)
    online = await asyncio.to_thread(lambda: client.iter_online_subscribers(max_fans=limit))

    online_objs: dict[int, dict] = {}
    for f in (online or []):
        if not isinstance(f, dict):
            continue
        try:
            fid = int(f.get("id"))
        except (TypeError, ValueError):
            continue
        online_objs.setdefault(fid, f)
    online_ids = list(online_objs.keys())
    now = datetime.utcnow()

    # WARM-UP: no state yet → seed last_seen for the whole online set, send nothing.
    if not await _account_warmed(account_id):
        if online_ids:
            await _upsert_last_seen(account_id, online_ids, now)
        return {"warmup": True, "online": len(online_ids),
                "seeded": len(online_ids), "enqueued": 0}

    states = await _load_states(account_id, online_ids)
    gap = timedelta(minutes=cfg.get("gap_minutes", 5))
    newly = [
        fid for fid in online_ids
        if (st := states.get(fid)) is None
        or st.last_seen_online_at is None
        or (now - st.last_seen_online_at) > gap
    ]

    max_per = int(cfg.get("max_per_tick", 25))
    handled = newly[:max_per]
    overflow = set(newly[max_per:])
    # Refresh last_seen for everyone online EXCEPT the deferred overflow, so the
    # overflow re-qualifies next tick (cap defers, never drops).
    refresh = [fid for fid in online_ids if fid not in overflow]
    await _upsert_last_seen(account_id, refresh, now)

    delay = int(cfg.get("delay_minutes", 4))
    jitter = int(cfg.get("jitter_minutes", 3))
    enqueued = 0
    skipped = 0
    skip_reasons: dict[str, int] = {}

    for fid in handled:
        st = states.get(fid)
        # Dedup: a fire-job is already pending for this fan.
        if st is not None and st.pending_job_id is not None:
            skipped += 1
            skip_reasons["pending"] = skip_reasons.get("pending", 0) + 1
            continue
        reason = await _gate_skip_reason(
            account_id, fid, cfg, now, fan_obj=online_objs.get(fid), at_fire=False)
        if reason:
            skipped += 1
            skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
            continue
        jit = random.randint(0, jitter * 60) if jitter > 0 else 0
        run_at = now + timedelta(minutes=delay, seconds=jit)
        job_id = await ax.enqueue_job(
            account_id, "nudge_online_fire",
            payload={"fan_id": int(fid), "with_image": bool(with_image)},
            run_at=run_at)
        await _set_pending(account_id, fid, job_id, now)
        enqueued += 1

    return {
        "online": len(online_ids),
        "newly_online": len(newly),
        "handled": len(handled),
        "deferred": len(overflow),
        "enqueued": enqueued,
        "skipped": skipped,
        "skip_reasons": skip_reasons,
    }
