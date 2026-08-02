"""service/automations/send_followup.py — Automation A07: send_followup.

Spec: library/one_section_of_automations/07_send_followup.md.

Drip re-engagement for fans who've gone quiet. Per fan, a small state machine
tracks "silence started at" and sends up to three Grok-generated nudges at
silence thresholds **26 h → 64 h → 256 h** (≥18 h minimum gap between sends).
Each nudge can attach a time-of-day-selected image from the `bot` vault folder.
If the fan replies mid-sequence → `reply_cooldown` (26 h pause); a reply after
all three nudges → `restart_cooldown` (72 h) then the cycle resets.

This is the NETWORK-REWRITE of the DOM-era script (07 §"Network-rewrite
mapping"): the sidebar/chat scraping is replaced by DB reads — the WS pump +
scrape_chats already fill `messages`/`fans` — and the vault DOM clicks are
replaced by `of_client.vault_lists`/`vault_media`. The state machine is purely
client-side and carried over unchanged.

It mirrors the scrape_chats reference + send_welcome:
  • of_client ONLY (no DOM), built via the executor's `_make_client` seam so
    tests inject a fake with no network. Reads come from OUR DB.
  • its OWN AsyncSession per read/write (`get_session()`); never a shared
    session across branches.
  • llm_client.chat() generates each nudge — that call writes the `grok_calls`
    audit row AND enforces the per-account daily cost cap atomically.
  • the existing optimistic send path: `of_client.send_message` →
    `attribution.write_outbound_attribution(emit_live=True)`. The WS pump skips
    outbound, so this is the only producer of the outbound `messages` row.
  • per-(account, fan) send-lease so A05/A06/A09/A11 can't double-message the
    same fan in an overlapping cycle.
  • `followup_state` is the durable cursor — a restart resumes from it.

Eligibility (07 §Core algorithm step 2): fan `lifetime_spend_cents >= 100`
($1 lifetime spend) AND (`total_likes` is NULL or ≥ 1) AND not blacklisted.

Scheduling: SCHEDULED via an `automation_rules` row with
`trigger_json = {"every_seconds": 3300}` (55 min); the executor materializes a
job each cycle. NO edit to automation_executor.py.

Payload knobs (all optional): `model` (LLM override), `dry_run` (generate but
don't send / advance), `with_image` (attach bot-folder vault image, default
True), `limit` (eligible-fan sweep ceiling).

Returns a stats dict → automation_runs.stats_json.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

import automation_executor as ax  # _make_client / _parse_iso / fan-lease seams
import llm_client                  # call .chat at runtime so tests can patch it
from . import rhythm  # tz_offset_for — IANA timezone beats the legacy utc_offset
from . import _voice  # whose voice this account writes in (NULL → 'her')
from attribution import write_outbound_attribution
from audiences import contact_guard_excludes, resolve_window_hours
from automation_registry import register
from ._common import (
    _sell_customs_from_row, apply_word_restriction, load_strip_emojis,
    quarantine_if_undeliverable, resolve_fan_name,
    resolve_model, skip_unreachable_fan, strip_emojis,
)
from db.engine import get_session
from db.models import (
    AccountAiConfig,
    Blacklist,
    Fan,
    FanProfile,
    FollowupState,
    Message,
    SkipList,
)
from llm_client import LLMCapExceeded

log = logging.getLogger("of-relay.automation.send_followup")

_DEFAULT_MODEL = "deepseek-v4-flash"  # unused (resolve_model → _common.DEFAULT_MODEL); kept in sync
_PURPOSE = "followup"
_TEMPERATURE = 0.9                 # matches the spec's Grok call

_ELIGIBLE_MIN_SPEND_CENTS = 100    # ≥ $1 lifetime spend
_DEFAULT_FAN_LIMIT = 200           # eligible-fan sweep ceiling per tick

# Silence thresholds per step (hours) and the minimum gap between consecutive
# sends. Carried over from the DOM script unchanged (07 §pick_next_step).
# Overridable per-rule via payload["step_hours"] = [h1, h2, h3] (see
# _resolve_step_thresholds) — the Brain panel's "Follow-ups" delays write it.
# Step 3 must sit UNDER _MAX_SILENCE_H or the cap retires the fan first and the
# final nudge never fires (it was 256h > the 168h cap — dead for every fan).
_STEP_THRESHOLDS_H = {1: 26, 2: 64, 3: 120}
_MIN_GAP_H = 18
# Hard cap: never follow up a fan who's been silent longer than this. A chat
# cold for over a week is ghosted/dead — reminders just waste sends and draw
# "Cannot send message to this user" rejections. A fan retired here is not gone
# for good: an inbound after the silence anchor restarts the cycle, same as a
# finished drip (see _Entry.replied_since_anchor).
_MAX_SILENCE_H = 24 * 7
# Cross-automation contact guard window: skip a due step when ANY sender
# touched the fan this recently. Nudges stamp only NudgeState (no messages
# row), so the messages-table reads above can't see them — the shared
# `contact_guard_excludes` can. absent → 12h, explicit 0 = off.
_GUARD_DEFAULT_H = 12

# Cooldowns (hours) for the reply / restart branches (07 §Core algorithm).
_REPLY_COOLDOWN_H = 26
_RESTART_COOLDOWN_H = 72

# Per-step nudge instruction handed to the LLM as the user turn. Each escalates
# the re-engagement warmth without ever guilt-tripping the fan.
# Ported verbatim-in-spirit from V1 send_followup.build_followup_prompt's
# step_instructions: the "questions&teases" voice, step-2 references ONE real
# profile detail, step-3 has a hint of FOMO. 1-3 lines, real emojis, no markdown.
_STEP_USER_INSTRUCTIONS = {
    1: ("Write a short, light, flirty message: a casual question-and-tease OR a "
        "simple personal question based on their profile. Keep it 1-3 lines, "
        "conversational texting style, real emojis, no markdown. Don't be needy "
        "or mention that they haven't replied — be natural, like you just "
        "thought of them."),
    2: ("Write a more personal question-and-tease that references ONE specific "
        "detail from their profile info. Keep it 1-3 lines, flirty but genuine. "
        "Real emojis, no markdown. Don't mention the silence — just reach out "
        "naturally, like you remembered something about them."),
    3: ("This is the FINAL follow-up. Write a warm, slightly playful "
        "question-and-tease with a hint of FOMO — something new, exclusive, or "
        "that you're specifically thinking of them. Keep it 1-3 lines, real "
        "emojis, no markdown. After this you stop messaging until they reply."),
}


# ── Prompt-shaping helpers (ports of the spec's pure helpers) ─────────

def _model_hour(utc_offset: float | int | None) -> int:
    """Current hour in the model's timezone (utcnow + offset hours). Accepts
    fractional hours — _load_ai_config resolves IANA zones and e.g. Kolkata
    is +5:30."""
    try:
        off = float(utc_offset)
    except (TypeError, ValueError):
        off = 0.0
    return (datetime.utcnow() + timedelta(hours=off)).hour


def _model_clock(utc_offset: float | int | None) -> str:
    """Full local wall-clock for the account's timezone, e.g.
    'Friday, June 05 — 02:30 PM' (V1's now_str). Gives the AI a concrete
    sense of what time it is for the creator, not just a coarse bucket."""
    try:
        off = float(utc_offset)
    except (TypeError, ValueError):
        off = 0.0
    local = datetime.utcnow() + timedelta(hours=off)
    return local.strftime("%A, %B %d — %I:%M %p")


def _time_activity(hour: int, acts: dict) -> tuple[str, str]:
    """6-bucket time-of-day → (label, activity-string). Same mapping as
    send_welcome / send_welcome_plusIMG. Missing slots fall back to ''."""
    if 5 <= hour < 9:
        return ("morning", acts.get("morning_1", ""))
    if 9 <= hour < 12:
        return ("morning", acts.get("morning_2", ""))
    if 12 <= hour < 15:
        return ("afternoon", acts.get("afternoon_1", ""))
    if 15 <= hour < 18:
        return ("afternoon", acts.get("afternoon_2", ""))
    if 18 <= hour < 21:
        return ("evening", acts.get("evening", ""))
    return ("night", acts.get("night", ""))


def _photo_index(hour: int) -> int:
    """5-9→0, 9-12→1, 12-15→2, 15-18→3, 18-21→4, else 5 (same as
    send_welcome_plusIMG — selects which bot-folder image to attach)."""
    if 5 <= hour < 9:
        return 0
    if 9 <= hour < 12:
        return 1
    if 12 <= hour < 15:
        return 2
    if 15 <= hour < 18:
        return 3
    if 18 <= hour < 21:
        return 4
    return 5


# The 6 time-of-day slot keys, ordered by _photo_index, matching time_activities.
_SLOT_KEYS = ("morning_1", "morning_2", "afternoon_1", "afternoon_2", "evening", "night")


def _slot_key(hour: int) -> str:
    """Slot name for the hour — same 6 buckets as _photo_index."""
    return _SLOT_KEYS[_photo_index(hour)]


def _slot_image_id(cfg: dict, hour: int) -> int | None:
    """Configured per-slot vault image id for the current time of day, or None.
    `cfg['time_images']` is {slot_key: media_id}; takes precedence over the legacy
    folder picker (mirrors send_welcome)."""
    imgs = cfg.get("time_images") or {}
    val = imgs.get(_slot_key(hour))
    try:
        return int(val) if val is not None else None
    except (TypeError, ValueError):
        return None


def _compose_system(cfg: dict, tod: str, activity: str, clock: str,
                    fan_name: str, fan_bio: str) -> str:
    # The FaceTime refusal is voice-varying: the female one names the fan "babe"
    # and "hun", which from a male creator reads as a woman typing. cfg carries
    # the resolved bundle (see _load_ai_config); absent → the female lane, which
    # is what every account that has ever run this engine gets.
    v = cfg.get("_voice") or _voice.HER
    persona = (cfg.get("persona")
               or "You are a warm, flirty OnlyFans creator re-engaging a fan "
                  "who has gone quiet.").strip()
    parts = [persona]
    location = (cfg.get("location") or "").strip()
    if location:
        parts.append(f"You are based in {location}.")
    if clock:
        where = f" in {location}" if location else ""
        parts.append(f"Right now it is {clock} ({tod}{where}).")
    if activity:
        parts.append(f"You are currently {activity}. "
                     f"You may reference this naturally.")
    who = []
    if fan_name:
        who.append(f"The fan's name/handle is {fan_name}.")
    if fan_bio:
        who.append(f"What you know about him: {fan_bio}")
    if who:
        parts.append(" ".join(who))
    parts.append(
        "Write ONE re-engagement DM (1-3 lines, casual texting tone, real emojis, "
        "no markdown). Use the fan's first name naturally if it's a real name. "
        "Never mention that they haven't replied or that it's been a while, and "
        "never sound desperate or needy. Output only the message text — no "
        "surrounding quotes, no preamble."
    )
    parts.append(v.live_proof)
    return "\n\n".join(parts)


def _compose_user(step: int, fan_name: str, silence_hours: float,
                  fan_address: str = "babe") -> str:
    """Per-fan user turn: personalises the step instruction with the fan's
    first name and the actual hours of silence (V1 injected both; V2 had a
    static template). Keeps each nudge AI-generated per fan, not a template."""
    # A male creator calling a straight male fan "babe" is the same
    # wrong-register tell as the refusal above — fall back to something the
    # male lane can say. (The female lane keeps "babe" byte-for-byte.)
    name = (fan_name or "").strip() or fan_address
    header = (f"Fan first name: {name}\n"
              f"Hours of silence: ~{silence_hours:.0f}h\n"
              f"Follow-up step: {step} of 3\n\n")
    return header + _STEP_USER_INSTRUCTIONS[step]


# ── In-memory state entry ─────────────────────────────────────────────

class _Entry:
    """Mutable mirror of one followup_state row (or a fresh tracking entry)."""
    __slots__ = ("phase", "cycle", "silence_started_at", "fan_last_reply_at",
                 "cooldown_until", "messages_sent")

    def __init__(self, row: FollowupState | None = None):
        if row is None:
            self.phase = "tracking"
            self.cycle = 1
            self.silence_started_at = None
            self.fan_last_reply_at = None
            self.cooldown_until = None
            self.messages_sent: dict[str, str] = {}
        else:
            self.phase = row.phase or "tracking"
            self.cycle = row.cycle or 1
            self.silence_started_at = row.silence_started_at
            self.fan_last_reply_at = row.fan_last_reply_at
            self.cooldown_until = row.cooldown_until
            try:
                self.messages_sent = json.loads(row.messages_sent or "{}") or {}
            except Exception:
                self.messages_sent = {}

    def replied_since_anchor(self, last_fan: datetime | None) -> bool:
        """Fan wrote back after we started counting his silence. Anchored on
        `silence_started_at`, NOT the last outbound — any other automation's
        send (a PPV blast a minute later) would otherwise bury the reply."""
        return bool(last_fan and (self.silence_started_at is None
                                  or last_fan > self.silence_started_at))

    def last_sent_dt(self) -> datetime | None:
        """Latest send timestamp across messages_sent (for the ≥18h gap)."""
        best: datetime | None = None
        for iso in self.messages_sent.values():
            dt = ax._parse_iso(iso)
            if dt and (best is None or dt > best):
                best = dt
        return best


def _resolve_step_thresholds(payload: dict) -> dict[int, float]:
    """Per-step silence thresholds (hours). `payload['step_hours'] = [h1, h2, h3]`
    overrides the defaults; any missing/invalid entry falls back to that step's
    default ({1:26, 2:64, 3:120}). Written by the Brain panel's "Follow-ups"
    delays so an operator can tune the drip cadence per account.

    A threshold at/over _MAX_SILENCE_H counts as invalid: the cap retires the fan
    before that step could ever fire, so honouring it would silently kill it."""
    out = dict(_STEP_THRESHOLDS_H)
    sh = (payload or {}).get("step_hours")
    if isinstance(sh, (list, tuple)):
        for i, step in enumerate((1, 2, 3)):
            if i < len(sh):
                try:
                    val = float(sh[i])
                except (TypeError, ValueError):
                    continue
                if 0 < val < _MAX_SILENCE_H:
                    out[step] = val
                elif val >= _MAX_SILENCE_H:
                    log.warning("send_followup step %s override %.0fh ignored "
                                "(≥ %sh cap) — using %sh",
                                step, val, _MAX_SILENCE_H, out[step])
    return out


def _pick_next_step(
    silence_hours: float, sent: dict, thresholds: dict[int, float] = _STEP_THRESHOLDS_H,
) -> int | None:
    """step 1 → 26h, step 2 → 64h, step 3 → 256h (or the per-rule `thresholds`
    override); else None (07 §pick_next_step)."""
    if "1" not in sent and silence_hours >= thresholds[1]:
        return 1
    if "2" not in sent and silence_hours >= thresholds[2]:
        return 2
    if "3" not in sent and silence_hours >= thresholds[3]:
        return 3
    return None


# ── DB seams (own session each — house pattern) ───────────────────────

async def _load_ai_config(account_id: str) -> dict:
    async with get_session() as s:
        cfg = await s.get(AccountAiConfig, account_id)
        if cfg is None:
            return {}
        acts: dict = {}
        if cfg.time_activities_json:
            try:
                acts = json.loads(cfg.time_activities_json) or {}
            except Exception:
                acts = {}
        imgs: dict = {}
        if cfg.time_images_json:
            try:
                imgs = json.loads(cfg.time_images_json) or {}
            except Exception:
                imgs = {}
        # Effective creator-local offset in HOURS — IANA `timezone` wins
        # (DST-correct), stored utc_offset is the legacy fallback (mirrors
        # send_welcome._load_ai_config).
        off_min = rhythm.tz_offset_for(getattr(cfg, "timezone", None),
                                       cfg.utc_offset)
        return {
            "persona": cfg.persona,
            "utc_offset": (off_min / 60.0 if off_min is not None
                           else (cfg.utc_offset or 0)),
            "location": cfg.location,
            "time_activities": acts,
            "time_images": imgs,
            "model": cfg.model,
            # The voice bundle rides on cfg because BOTH _compose_system call
            # sites already receive it, and it is resolved off the row this
            # function already holds — no second query. NULL voice → the female
            # bundle → byte-identical to what this engine has always sent.
            "_voice": _voice.blocks(getattr(cfg, "voice", None),
                                    _sell_customs_from_row(cfg)),
        }




async def _load_blacklist() -> set[int]:
    async with get_session() as s:
        rows = (await s.execute(select(Blacklist.fan_id))).all()
    return {int(r[0]) for r in rows}


async def _load_skip_list(account_id: str) -> set[int]:
    """Per-account skip_list — fans we've stopped auto-messaging (incl. ones OF
    rejected as unreachable). send_followup must honor it like of_ai_chat does,
    or it re-tries deleted/expired fans every cycle."""
    async with get_session() as s:
        rows = (await s.execute(
            select(SkipList.fan_id).where(SkipList.account_id == str(account_id)))).all()
    return {int(r[0]) for r in rows}


async def _load_eligible_fans(account_id: str, limit: int) -> list[dict]:
    """Fans with ≥$1 lifetime spend AND (total_likes NULL or ≥1). Ordered by
    spend desc so a bounded sweep favors the most valuable fans.

    NB: a quarantined / rested fan (`automation_paused_until` in the future) is NOT
    filtered here — the per-fan `fan_on_cooldown` check in the run loop already
    skips it before any LLM call, so the undeliverable-quarantine pause is honoured
    there (same column the W3 cooldown uses)."""
    async with get_session() as s:
        rows = (await s.execute(
            select(Fan.fan_id, Fan.of_display_name, Fan.real_name,
                   Fan.generated_nickname, Fan.custom_nickname,
                   Fan.home_country, Fan.home_city)
            .where(
                Fan.account_id == account_id,
                Fan.lifetime_spend_cents >= _ELIGIBLE_MIN_SPEND_CENTS,
                (Fan.total_likes.is_(None)) | (Fan.total_likes >= 1),
            )
            .order_by(Fan.lifetime_spend_cents.desc())
            .limit(limit)
        )).all()
    # resolve_fan_name walks real_name → curated custom_nickname ('John/City/Tag' →
    # 'John') → generated_nickname → of_display_name, instead of emitting a literal
    # 'Babe' downstream. of_username is intentionally dropped (handles aren't names).
    # home_country/home_city ride along because they are what the resolver checks a
    # candidate against — without them 'Millbrook' passes as his name. Empty '' here →
    # _compose_user's soft 'babe' fallback as before.
    return [
        {"fan_id": int(r.fan_id), "name": resolve_fan_name(r)}
        for r in rows
    ]


async def _load_last_message_times(
    account_id: str, fan_ids: list[int]
) -> dict[int, dict[str, datetime | None]]:
    """Per fan: {'in': latest inbound created_at, 'out': latest outbound}. Two
    grouped MAX() queries instead of N per-fan round trips."""
    out: dict[int, dict[str, datetime | None]] = {
        fid: {"in": None, "out": None} for fid in fan_ids
    }
    if not fan_ids:
        return out
    async with get_session() as s:
        rows = (await s.execute(
            select(Message.fan_id, Message.direction, func.max(Message.created_at))
            .where(
                Message.account_id == account_id,
                Message.fan_id.in_(fan_ids),
                Message.direction.in_(("in", "out")),
            )
            .group_by(Message.fan_id, Message.direction)
        )).all()
    for fid, direction, ts in rows:
        slot = out.get(int(fid))
        if slot is not None:
            slot[direction] = ts
    return out


async def _load_state(account_id: str) -> dict[int, FollowupState]:
    async with get_session() as s:
        rows = (await s.execute(
            select(FollowupState).where(FollowupState.account_id == account_id)
        )).scalars().all()
    return {int(r.fan_id): r for r in rows}


async def _load_bios(account_id: str, fan_ids: list[int]) -> dict[int, str]:
    """Per fan: bullet_points + short_bio joined (V1 fed BOTH into the system
    prompt). Either may be empty; we keep whatever is present."""
    if not fan_ids:
        return {}
    async with get_session() as s:
        rows = (await s.execute(
            select(FanProfile.fan_id, FanProfile.short_bio, FanProfile.bullet_points).where(
                FanProfile.account_id == account_id,
                FanProfile.fan_id.in_(fan_ids),
            )
        )).all()
    out: dict[int, str] = {}
    for fid, bio, bullets in rows:
        chunk = "\n".join(p.strip() for p in (bullets, bio) if p and p.strip())
        if chunk:
            out[int(fid)] = chunk
    return out


async def _save_entry(account_id: str, fan_id: int, e: _Entry, now: datetime) -> None:
    """Upsert one followup_state row from the in-memory entry."""
    values = dict(
        account_id=str(account_id),
        fan_id=int(fan_id),
        phase=e.phase,
        cycle=e.cycle,
        silence_started_at=e.silence_started_at,
        fan_last_reply_at=e.fan_last_reply_at,
        cooldown_until=e.cooldown_until,
        messages_sent=json.dumps(e.messages_sent, default=str),
        completed=(e.phase == "completed"),
        updated_at=now,
    )
    update = {k: v for k, v in values.items()
              if k not in ("account_id", "fan_id")}
    async with get_session() as s:
        await s.execute(
            sqlite_insert(FollowupState)
            .values(**values)
            .on_conflict_do_update(
                index_elements=["account_id", "fan_id"], set_=update
            )
        )


# ── Vault image (network-rewrite of the DOM "bot" folder click) ───────

# Folders to source the followup image from, in priority order. Legacy "bot" (V1)
# first so existing accounts are unchanged; fall back to a welcome folder so images
# still attach on accounts with no "bot" folder. We do NOT fall through to arbitrary
# folders (Streams/Posts would DM the wrong media). Mirrors send_welcome.
_IMAGE_FOLDER_NAMES = ("bot", "welcome script", "welcome")


def _bot_folder_media_id(client, hour: int) -> int | None:
    """Pick a time-of-day vault photo (id at _photo_index(hour), clamped) from the
    first folder in `_IMAGE_FOLDER_NAMES` that exists and has photos. Best-effort:
    any failure / no named folder with photos → None (send text)."""
    try:
        lists = client.vault_lists(view="main", limit=100)
    except Exception:
        log.debug("send_followup vault_lists failed", exc_info=True)
        return None
    folders = lists.get("list") if isinstance(lists, dict) else lists
    by_name: dict[str, int] = {}
    for f in (folders or []):
        if isinstance(f, dict) and f.get("id") is not None:
            by_name.setdefault(str(f.get("name", "")).strip().lower(), f.get("id"))

    for nm in _IMAGE_FOLDER_NAMES:
        folder_id = by_name.get(nm)
        if folder_id is None:
            continue
        try:
            media = client.vault_media(list_id=int(folder_id), type="photo", limit=50)
        except Exception:
            log.debug("send_followup vault_media failed folder=%s", folder_id, exc_info=True)
            continue
        items = media.get("list") if isinstance(media, dict) else media
        items = [it for it in (items or []) if isinstance(it, dict) and it.get("id")]
        if not items:
            continue
        idx = min(_photo_index(hour), len(items) - 1)
        try:
            return int(items[idx]["id"])
        except (TypeError, ValueError, KeyError):
            continue
    return None


# ── Compose-only preview (no send, no state write) ────────────────────

async def preview_compose(
    account_id: str, *, fan_id: int | None = None, test_name: str | None = None,
    step: int | None = None, model: str | None = None,
) -> dict:
    """Compose the follow-up nudge a real run WOULD send for one fan — the text +
    the chosen time-of-day image id — WITHOUT sending and WITHOUT any state write.
    Powers the Brain panel's "Preview" button (same shape as send_welcome's:
    {text, image, name, slot, step}). Unlike welcome there is no deterministic
    local template, so this makes ONE LLM call (the daily cost cap still applies).

    `step` (1–3, default 1) picks which escalation to preview; the representative
    silence is that step's threshold. Fan name/bio come from the DB when `fan_id`
    is given (overridable with `test_name`), else a generic 'babe' stand-in."""
    cfg = await _load_ai_config(account_id)
    strip_emoji_on = await load_strip_emojis(account_id)  # account-wide emoji strip
    hour = _model_hour(cfg.get("utc_offset"))
    tod, activity = _time_activity(hour, cfg.get("time_activities") or {})
    clock = _model_clock(cfg.get("utc_offset"))

    try:
        step = int(step) if step is not None else 1
    except (TypeError, ValueError):
        step = 1
    step = min(3, max(1, step))

    name = (test_name or "").strip()
    bio = ""
    if fan_id is not None:
        async with get_session() as s:
            row = (await s.execute(
                select(Fan.of_display_name, Fan.of_username).where(
                    Fan.account_id == str(account_id),
                    Fan.fan_id == int(fan_id),
                )
            )).first()
        if row and not name:
            name = (row[0] or row[1] or "").strip()
        bio = (await _load_bios(account_id, [int(fan_id)])).get(int(fan_id), "")
    if not name:
        name = "babe"

    # Representative silence = the step's silence threshold (what triggers it).
    silence_hours = float(_STEP_THRESHOLDS_H[step])
    model = model or await resolve_model(account_id, _PURPOSE, None)
    res = await llm_client.chat(
        model=model,
        messages=[
            {"role": "system", "content": _compose_system(
                cfg, tod, activity, clock, name, bio)},
            {"role": "user", "content": _compose_user(
                step, name, silence_hours,
                (cfg.get("_voice") or _voice.HER).fan_address)},
        ],
        purpose=_PURPOSE,
        account_id=account_id,
        fan_id=int(fan_id) if fan_id is not None else 0,
        temperature=_TEMPERATURE,
    )
    text = apply_word_restriction((getattr(res, "content", "") or "").strip())
    if strip_emoji_on:
        text = strip_emojis(text)
    return {"text": text, "image": _slot_image_id(cfg, hour),
            "name": name, "slot": _slot_key(hour), "step": step}


# ── The automation ───────────────────────────────────────────────────

@register("send_followup")
async def run(account_id: str, payload: dict, *, run_id: int) -> dict:
    payload = payload or {}
    dry_run = bool(payload.get("dry_run"))
    with_image = payload.get("with_image", True)
    limit = int(payload.get("limit") or _DEFAULT_FAN_LIMIT)

    cfg = await _load_ai_config(account_id)
    strip_emoji_on = await load_strip_emojis(account_id)  # account-wide emoji strip
    model = await resolve_model(account_id, _PURPOSE, payload.get("model"))
    thresholds = _resolve_step_thresholds(payload)
    hour = _model_hour(cfg.get("utc_offset"))
    tod, activity = _time_activity(hour, cfg.get("time_activities") or {})
    clock = _model_clock(cfg.get("utc_offset"))

    now = datetime.utcnow()
    state = await _load_state(account_id)

    # ── 1) Time-only transitions (no client) ─────────────────────────────
    transitioned = 0
    for fid, row in list(state.items()):
        e = _Entry(row)
        changed = False
        if e.phase == "reply_cooldown" and e.cooldown_until and now > e.cooldown_until:
            e.phase = "tracking"
            e.silence_started_at = None
            e.cooldown_until = None
            changed = True
        elif e.phase == "restart_cooldown" and e.cooldown_until and now > e.cooldown_until:
            e.phase = "tracking"
            e.messages_sent = {}
            e.cycle += 1
            e.silence_started_at = None
            e.cooldown_until = None
            changed = True
        if changed:
            await _save_entry(account_id, fid, e, now)
            state[fid] = await _reload_one(account_id, fid)
            transitioned += 1

    # ── 2) Eligibility (no client) ───────────────────────────────────────
    blacklist = await _load_blacklist()
    skip_ids = await _load_skip_list(account_id)
    eligible = [f for f in await _load_eligible_fans(account_id, limit)
                if f["fan_id"] not in blacklist and f["fan_id"] not in skip_ids]
    eligible_ids = [f["fan_id"] for f in eligible]

    # ── 3) DB reads that replace the DOM scrape ──────────────────────────
    msg_times = await _load_last_message_times(account_id, eligible_ids)
    bios = await _load_bios(account_id, eligible_ids)

    # Cross-automation contact guard (one account-wide set per run): a fan a
    # blast/nudge/chatter touched inside the window must not also get a drip
    # step this tick. Outbound-only — inbound replies already flip the phase.
    guard_h = resolve_window_hours(payload.get("exclude_replied_hours"), _GUARD_DEFAULT_H)
    guard_ids: set[int] = set()
    if guard_h > 0:
        guard_ids = await contact_guard_excludes(
            account_id, outbound_hours=guard_h)

    sent = 0
    advanced = 0
    skipped_locked = 0
    skipped_cooldown = 0
    skipped_guard = 0
    skipped_unread = 0
    errors = 0
    cap_hit = False
    image_attached = 0

    for fan in eligible:
        fid = fan["fan_id"]
        e = _Entry(state.get(fid))

        if e.phase in ("reply_cooldown", "restart_cooldown"):
            continue  # still cooling down (time transitions ran above)

        times = msg_times.get(fid, {"in": None, "out": None})
        last_fan = times["in"]
        last_model = times["out"]

        fan_replied = bool(last_fan and (last_model is None or last_fan > last_model))

        # He wrote back after we stopped counting his silence — either the drip
        # ran out of steps or the cap retired him. Same restart either way; the
        # restart_cooldown expiry above owns the fresh cycle.
        if e.phase in ("all_sent_waiting", "completed"):
            if e.replied_since_anchor(last_fan):
                e.phase = "restart_cooldown"
                e.cooldown_until = now + timedelta(hours=_RESTART_COOLDOWN_H)
                e.fan_last_reply_at = last_fan
                await _save_entry(account_id, fid, e, now)
                advanced += 1
            continue

        # phase == "tracking"
        if fan_replied:
            # Preserve the fan's reply for human review; pause the drip.
            e.phase = "reply_cooldown"
            e.cooldown_until = now + timedelta(hours=_REPLY_COOLDOWN_H)
            e.fan_last_reply_at = last_fan
            await _save_entry(account_id, fid, e, now)
            skipped_unread += 1
            advanced += 1
            continue

        # Establish the silence anchor if missing.
        if e.silence_started_at is None:
            e.silence_started_at = last_model or now
        silence_hours = (now - e.silence_started_at).total_seconds() / 3600.0

        # Cap: a fan silent longer than a week is cold — stop the drip entirely
        # instead of nagging a ghosted/dead chat.
        if silence_hours > _MAX_SILENCE_H:
            e.phase = "completed"
            await _save_entry(account_id, fid, e, now)
            continue

        next_step = _pick_next_step(silence_hours, e.messages_sent, thresholds)
        if next_step is None:
            # Persist the silence anchor we may have just set.
            await _save_entry(account_id, fid, e, now)
            continue

        # Enforce ≥18h between consecutive sends.
        last_sent = e.last_sent_dt()
        if last_sent and (now - last_sent).total_seconds() / 3600.0 < _MIN_GAP_H:
            await _save_entry(account_id, fid, e, now)
            continue

        # A blast/nudge/manual send touched this fan inside the guard window —
        # piling a drip step on top reads as spam. The step stays "due" and
        # fires on a later tick once the window clears.
        if fid in guard_ids:
            skipped_guard += 1
            continue
        # Another automation messaged this fan recently → rest it (W3 cooldown).
        if await ax.fan_on_cooldown(account_id, fid):
            skipped_cooldown += 1
            continue
        # One bot message per fan per cycle — don't race A05/A06/A09/A11.
        if not await ax.acquire_fan_lease(account_id, fid, "send_followup"):
            skipped_locked += 1
            continue
        sent_ok = False
        try:
            try:
                res = await llm_client.chat(
                    model=model,
                    messages=[
                        {"role": "system", "content": _compose_system(
                            cfg, tod, activity, clock,
                            fan["name"], bios.get(fid, ""))},
                        {"role": "user", "content": _compose_user(
                            next_step, fan["name"], silence_hours,
                            (cfg.get("_voice") or _voice.HER).fan_address)},
                    ],
                    purpose=_PURPOSE,
                    account_id=account_id,
                    fan_id=fid,
                    temperature=_TEMPERATURE,
                )
            except LLMCapExceeded:
                cap_hit = True
                log.warning("send_followup daily LLM cap reached account=%s — stopping",
                            account_id)
                break
            except Exception:
                errors += 1
                log.warning("send_followup generate failed account=%s fan=%s",
                            account_id, fid, exc_info=True)
                continue

            text = apply_word_restriction((getattr(res, "content", "") or "").strip())
            if strip_emoji_on:
                text = strip_emojis(text)
            if not text:
                errors += 1
                continue

            if dry_run:
                sent += 1  # would-send; don't advance state on a dry run
                continue

            # Optional time-of-day vault image. Prefer the account's configured
            # per-slot image id (time_images); fall back to the legacy folder picker.
            media_files: list[int] = []
            if with_image:
                mid = _slot_image_id(cfg, hour)
                if mid is None:
                    client_for_vault = await asyncio.to_thread(ax._make_client, account_id)
                    mid = await asyncio.to_thread(_bot_folder_media_id, client_for_vault, hour)
                if mid is not None:
                    media_files = [mid]

            client = await asyncio.to_thread(ax._make_client, account_id)
            try:
                result = await asyncio.to_thread(
                    lambda: client.send_message(fid, text, media_files=media_files)
                )
            except Exception as e:
                errors += 1
                # A known permanent marker → skip_list and done. Otherwise the send
                # raised something we don't recognise — but a lapsed-sub fan raises
                # on EVERY send, and followup only advances state on a returned
                # send, so it re-fires the same nudge every tick forever (the
                # paying-but-lapsed loop). Probe WHY and quarantine the undeliverable
                # ones (1-week rest); a genuine transient just retries next tick.
                if not await skip_unreachable_fan(account_id, fid, e, log=log):
                    await quarantine_if_undeliverable(client, account_id, fid)
                log.warning("send_followup send failed account=%s fan=%s",
                            account_id, fid, exc_info=True)
                continue

            if media_files:
                image_attached += 1

            # Optimistic send path: persist the outbound row, then advance state.
            msg_id = result.get("id") if isinstance(result, dict) else None
            if msg_id:
                await write_outbound_attribution(
                    account_id=account_id,
                    fan_id=int(fid),
                    message_id=int(msg_id),
                    sent_by_employee_id=None,  # → system Automation employee
                    automation_kind=_PURPOSE,  # followup
                    body=str(result.get("text") or text),
                    price_cents=0,
                    created_at=ax._parse_iso(result.get("createdAt")) or datetime.utcnow(),
                    emit_live=True,  # WORKER→SSE bridge
                )

            e.messages_sent[str(next_step)] = now.isoformat()
            if len(e.messages_sent) >= 3:
                e.phase = "all_sent_waiting"
            await _save_entry(account_id, fid, e, now)
            sent += 1
            advanced += 1
            sent_ok = True
        finally:
            # W3: keep the lease (let it expire) + rest the fan on a confirmed
            # send; release it only when nothing went out.
            if sent_ok:
                try:
                    await ax.start_fan_cooldown(account_id, fid)
                except Exception:
                    log.warning("send_followup cooldown set failed account=%s fan=%s",
                                account_id, fid, exc_info=True)
            else:
                await ax.release_fan_lease(account_id, fid)

    return {
        "eligible": len(eligible),
        "transitioned": transitioned,
        "followups_sent": sent,
        "state_advanced": advanced,
        "skipped_unread": skipped_unread,
        "skipped_locked": skipped_locked,
        "skipped_cooldown": skipped_cooldown,
        "skipped_guard": skipped_guard,
        "image_attached": image_attached,
        "errors": errors,
        "cap_hit": cap_hit,
        "dry_run": dry_run,
        "model": model,
    }


async def _reload_one(account_id: str, fan_id: int) -> FollowupState | None:
    async with get_session() as s:
        return await s.get(FollowupState, (str(account_id), int(fan_id)))
