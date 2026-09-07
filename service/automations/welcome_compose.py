"""
service/automations/welcome_compose.py — what a welcome SAYS, for one fan.

The text half of `send_welcome` (A09): the creator's local clock and the six
time-of-day slots, the deterministic stutter greeting, the activity / clock
line for the slot, the operator's pin, the optional AI restyle of that line
(the ONE `llm_client.chat` call in the welcome), the per-slot vault image, and
`_compose_bubbles`, which is the single composer both the live run and the
Brain panel's preview go through so they cannot disagree about the burst.

Nothing in here sends, writes send-state, or paces. `send_welcome` composes a
plan with these and ships it; `preview_compose` composes the same plan and
returns it. The other senders that greet by the same clock (`nudge_online`,
`mass_nudge`, `online_blast`) import the slot helpers from here.

SEND SHAPE (2026-07): the deterministic welcome goes out as TWO paced bubbles —
bubble 1 is the stutter greeting with the time-of-day image attached, bubble 2 is
the activity line AI-restyled into the creator's casual texting voice (verbatim
template on any LLM failure). The restyle is ONE cached LLM call per slot line
shared across fans; a fan who already texted us gets a fresh per-fan call (see
`_restyle_cache`). An operator-written `question` (payload) rides as an
optional THIRD bubble, sent word-for-word — never restyled, never near an LLM.
An operator-picked `gif_id` rides as an optional FOURTH bubble, composed apart
from the text (it carries none) but sent as part of the SAME burst.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta
from collections.abc import Awaitable, Callable

from sqlalchemy import select

import llm_client                  # call .chat at runtime so tests can patch it
from . import rhythm  # tz_hours_for — THE clock (fixed offset first, zone as fallback)
from ._common import (apply_word_restriction, bool_knob, load_voice_blocks,
                      load_strip_emojis, name_token, resolve_fan_name,
                      resolve_model, strip_emojis)
from .pacing import (ROLE_GAP as _ROLE_GAP, ROLE_OPENER as _ROLE_OPENER,
                     ROLE_TAIL as _ROLE_TAIL)
from db.engine import get_session
from db.models import AccountAiConfig, Fan, FanProfile, Message
from llm_client import LLMCapExceeded


log = logging.getLogger("of-relay.automation.send_welcome")


# First-letter → alliterative adjective for the stutter greeting. e.g. S → "Sexy
# Sofie". Drives every welcome now that the nameless riff is deterministic too.
_ADJS = {
    "A": "Adoring", "B": "Brave", "C": "Cute", "D": "Dreamy", "E": "Epic", "F": "Flirty",
    "G": "Gorgeous", "H": "Handsome", "I": "Incredible", "J": "Juicy", "K": "Kind", "L": "Lovely",
    "M": "Mighty", "N": "Naughty", "O": "Original", "P": "Playful", "Q": "Quick", "R": "Radiant",
    "S": "Sexy", "T": "Tasty", "U": "Unique", "V": "Vibrant", "W": "Wild", "X": "Xtra",
    "Y": "Yummy", "Z": "Zesty",
}

# The male table. These describe the FAN, who is male in both lanes — so this is
# not a pronoun fix. It is a REGISTER fix: "Yummy Mike" / "Juicy Mike" / "Cute
# Mike" is what SHE calls him, and a dom does not hand out those words. His
# vocabulary is what a man in charge notices about someone who just subscribed —
# eager, hungry, obedient, willing — which does the same alliterative job and
# lands the power dynamic in the first three words of the first message.
_ADJS_HIM = {
    "A": "Ambitious", "B": "Bold", "C": "Cocky", "D": "Devoted", "E": "Eager",
    "F": "Fearless", "G": "Game", "H": "Hungry", "I": "Impatient", "J": "Jumpy",
    "K": "Keen", "L": "Loyal", "M": "Mighty", "N": "Needy", "O": "Obedient",
    "P": "Patient", "Q": "Quick", "R": "Ready", "S": "Solid", "T": "Tough",
    "U": "Unruly", "V": "Vicious", "W": "Willing", "X": "Xtra", "Y": "Yearning",
    "Z": "Zealous",
}
_ADJ_DEFAULT = {"her": "Flirty", "him": "Eager"}
# What to call a subscriber whose handle yields no usable word at all.
_NAMELESS_GREET = {"her": "cutie", "him": "boy"}


def _adjs(voice: str) -> tuple[dict, str]:
    """(table, default) for this lane. Anything but "him" gets hers, unchanged."""
    if str(voice or "").strip().lower() == "him":
        return _ADJS_HIM, _ADJ_DEFAULT["him"]
    return _ADJS, _ADJ_DEFAULT["her"]

def _clock_hours(cfg: dict) -> float:
    """Her local offset in HOURS from a cfg dict carrying the RAW clock columns.

    The ONE place in this module that reads either clock column. Everything below
    takes hours and no longer cares which column answered — `rhythm.tz_hours_for`
    decides (the fixed offset, with a legacy IANA zone as the fallback), and a
    clockless account resolves to 0.0 == UTC, which is what a welcome has always
    used. A draft config from the Brain panel goes through here too, so a preview
    and a real send cannot disagree about the hour."""
    return rhythm.tz_hours_for(cfg.get("timezone"), cfg.get("utc_offset"))


def _model_hour(utc_offset: float | int | None) -> int:
    """Current hour in the model's timezone (utcnow + offset hours). Accepts
    fractional hours — a legacy IANA zone can still resolve to e.g. Kolkata's
    +5:30 (see `_clock_hours`)."""
    try:
        off = float(utc_offset)
    except (TypeError, ValueError):
        off = 0.0
    return (datetime.utcnow() + timedelta(hours=off)).hour


def _time_activity(hour: int, acts: dict) -> tuple[str, str]:
    """6-bucket time-of-day → (label, activity-string) per the spec mapping.
    Missing slots fall back to '' (spec edge case)."""
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
    """5-9→0, 9-12→1, 12-15→2, 15-18→3, 18-21→4, else 5 (same 6-bucket mapping
    as send_followup — selects which bot-folder image to attach). Deterministic
    per hour."""
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
    """Slot name for the hour — same 6 buckets as _time_activity/_photo_index."""
    return _SLOT_KEYS[_photo_index(hour)]


# Representative hour inside each time-of-day bucket — the inverse of _slot_key. A
# preview can pin ANY slot (not just the creator's current one) by asking for that
# slot's representative hour. Each value sits strictly inside its _photo_index bucket
# so the round-trip holds: _slot_key(_slot_hour(k)) == k for every slot key.
_SLOT_REPR_HOUR = {
    "morning_1": 7, "morning_2": 10, "afternoon_1": 13,
    "afternoon_2": 16, "evening": 19, "night": 22,
}


def _slot_hour(slot: str | None) -> int | None:
    """Representative hour for a slot key, or None for an unknown/empty slot (caller
    then falls back to the creator's current local hour)."""
    return _SLOT_REPR_HOUR.get(slot or "")


def _pinned_line(cfg: dict, hour: int) -> str | None:
    """The operator-approved FIXED activity line for this slot (the Brain "pin"),
    or None if the slot isn't pinned. The stored weekday is swapped to today's so a
    daily welcome never shows a stale day. Everything else is sent exactly as pinned
    — no LLM restyle, deterministic. Robust to a missing/echoed weekday: an absent
    or already-current day is a no-op replace, so the line still sends cleanly."""
    pins = cfg.get("welcome_pins") or {}
    pin = pins.get(_slot_key(hour))
    if not isinstance(pin, dict):
        return None
    line = str(pin.get("line") or "").strip()
    if not line:
        return None
    old_wd = str(pin.get("weekday") or "").strip()
    cur_wd = _model_weekday(_clock_hours(cfg))
    if old_wd and old_wd.lower() != cur_wd.lower():
        # Preserve the casing the operator wrote — a lowercase "thursday" in a
        # casual line stays lowercase, ALL-CAPS stays ALL-CAPS — instead of forcing
        # strftime's Title-case and capitalising the day mid-sentence.
        def _sub(m: "re.Match") -> str:
            s = m.group(0)
            if s.isupper():
                return cur_wd.upper()
            if s.islower():
                return cur_wd.lower()
            return cur_wd  # Title / mixed → the canonical Title-case weekday
        line = re.sub(rf"\b{re.escape(old_wd)}\b", _sub, line, flags=re.IGNORECASE)
    return line


def _slot_image_id(cfg: dict, hour: int) -> int | None:
    """Configured per-slot vault image id for the current time of day, or None.
    `cfg['time_images']` is {slot_key: media_id}; takes precedence over the
    legacy folder picker so an account can pin one image per slot (set via the
    templates UI). Falls back to None when the slot is unset/non-numeric."""
    imgs = cfg.get("time_images") or {}
    val = imgs.get(_slot_key(hour))
    try:
        return int(val) if val is not None else None
    except (TypeError, ValueError):
        return None


# ── DB seams (own session each — house pattern) ───────────────────────

async def _load_ai_config(account_id: str) -> dict:
    """Detached snapshot of account_ai_config (or {} when absent)."""
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
        pins: dict = {}
        if getattr(cfg, "welcome_pinned_json", None):
            try:
                pins = json.loads(cfg.welcome_pinned_json) or {}
            except Exception:
                pins = {}
        # The two clock columns RAW, exactly as stored. This dict used to carry a
        # `utc_offset` that was already RESOLVED, which gave the key two meanings —
        # resolved here, raw in the draft the Brain panel posts — and a shallow
        # merge of the draft then silently replaced one with the other. Raw in,
        # `_clock_hours` out: the merge compares like with like, and exactly one
        # function knows how a row becomes an hour.
        return {
            "persona": cfg.persona,
            "utc_offset": cfg.utc_offset,
            "timezone": getattr(cfg, "timezone", None),
            "location": cfg.location,
            "time_activities": acts,
            "time_images": imgs,
            "welcome_pins": pins,
            "model": cfg.model,
        }


def _model_weekday(utc_offset) -> str:
    """Weekday name in the creator's timezone (utcnow + offset hours)."""
    try:
        off = float(utc_offset)
    except (TypeError, ValueError):
        off = 0.0
    return (datetime.utcnow() + timedelta(hours=off)).strftime("%A")


# Canonical name parser now lives in _common.name_token (shared by every sender so
# they all derive greet-names identically); kept as a module-local alias so the
# existing call sites below read unchanged.
_name_token = name_token


async def _resolve_welcome_name(account_id: str, fan_id: int, sub: dict) -> str:
    """Best real first name to greet by, generated from whatever we have — the guy's
    real name, a team-curated/AI nickname, or the OF display name (W4: "generate the
    nickname from info we have"). Returns '' when all we have is a random handle /
    number, so the caller falls back to the LLM riff. Brand-new subs usually have no
    Fan row → we fall through to the notification's display name.

    Precedence (CURATED beats the RAW OF name): the team relabels fans via
    `custom_nickname` ('Garrett/City/Tag') — that's what the whole UI shows — but a
    fan's raw OF account name may be something else entirely (e.g. 'Kyle'); otherwise
    a fan curated as 'Garrett' gets welcomed as 'Kyle'. That IS `resolve_fan_name`'s
    order now, so this hands the row to the shared resolver instead of keeping a
    second one — the two used to disagree on 154 fans, and welcome minted names the
    chat lane refused to say ('Sparky10' → "hey Sparky"). Only the two sources a
    WELCOME has ride along: the gen_info profile nickname (same shape as
    generated_nickname) and the live notification name (same shape as a display
    name, and dead last, so it fills in for a brand-new sub with no Fan row)."""
    async with get_session() as s:
        prof = (await s.execute(select(FanProfile.nickname).where(
            FanProfile.account_id == str(account_id),
            FanProfile.fan_id == int(fan_id)))).scalar_one_or_none()
        fan = (await s.execute(select(
            Fan.real_name, Fan.generated_nickname, Fan.custom_nickname,
            Fan.of_display_name, Fan.home_country, Fan.home_city
        ).where(Fan.account_id == str(account_id), Fan.fan_id == int(fan_id)))).first()
    row = dict(zip(("real_name", "generated_nickname", "custom_nickname",
                    "of_display_name", "home_country", "home_city"), fan or ()))
    row["generated_nickname"] = row.get("generated_nickname") or prof
    row["of_display_name"] = row.get("of_display_name") or sub.get("name")
    # resolve_fan_name may return a full display name ('garrett baydala'); a welcome
    # greets by the first token. name_token is idempotent on one already ('Garrett').
    return _name_token(resolve_fan_name(row))


# A welcome is always [greeting] + the slot's activity line. The two halves are
# INDEPENDENT: the greeting is the only fan-specific part, and the activity bubble
# is a pure function of (cfg, hour). Keeping them apart is what lets a fan we
# can't name take the LLM greeting and the SAME deterministic activity line as
# everyone else — so the pin and the restyle downstream need no special case.

def _local_greeting(name: str, voice: str = "her") -> str:
    """V1 'precious' bubble 1 (NO LLM): 'Hey S-S-S-Sexy Sofie ! !!' — the stutter
    prefix sits on the first letter and leads into the alliterative nickname. The
    vault image rides this bubble.

    This is the FIRST thing a new subscriber ever reads, so the adjective sets the
    register before anything else does — see `_ADJS_HIM`."""
    L = name[0].upper()
    table, default = _adjs(voice)
    return f"Hey {L}-{L}-{L}-{table.get(L, default)} {name} ! !!"


# Longest run of letters in a handle — 'xx_rider_92' → 'rider'. 3+ so a separator
# fragment ('xx', 'zz') never wins over the actual word.
_HANDLE_WORD = re.compile(r"[A-Za-z]{3,}")


def _greet_token(sub: dict, voice: str = "her") -> str:
    """What to greet a fan by when `_resolve_welcome_name` found no real name in any
    source — all that's left is the raw OF handle. Two shapes, resolved in code:

        xx_rider_92  → 'rider'          (the word inside the handle)
        u4471223     → 'cutie'          (a bare id carries no word at all)

    🚨 A bare id used to mint 'fan #4471223' here, and the caller is
    `_local_greeting`, which alliterates whatever it is handed AS A NAME. The
    first thing 59 real subscribers ever read — the most recent on 2026-08-21 —
    was "Hey F-F-F-Flirty fan #521599677 ! !!". The docstring's own rule ("a bare
    id is an id, not a name") was right and the branch contradicted it: an id is
    not a word, so it belongs on the nameless path with every other handle we
    cannot say out loud, not in a template built to say a name.

    This used to be an LLM call per nameless fan. It produced exactly this shape
    ('Hey B-B-B-Bold bigdaddy69 ! !!'), so it was paying per fan for a table lookup
    the named path already does for free — and a daily-cap trip cost those fans
    their welcome entirely. Deterministic means no cost, no cap, no variance."""
    target = (sub.get("name") or sub.get("username") or "").strip()
    words = _HANDLE_WORD.findall(target)
    if words:
        return max(words, key=len).lower()
    # Nothing usable in the handle at all. NOT `v.fan_address` — hers is "cutie"
    # here and "babe" there, and reaching for the bundle silently changed what 17
    # live accounts greet a nameless subscriber with. Its own dict, its own words.
    return _NAMELESS_GREET["him" if str(voice or "").strip().lower() == "him"
                           else "her"]


def _activity_bubble(cfg: dict, hour: int | None = None, *,
                     time_only: bool = False) -> list[str]:
    """Bubble 2 — 'just woke up and made myself a coffee... it's Friday morning in
    Vancouver, Canada' — as a 0-or-1 element list, empty when the account has no
    activity for this slot. Verbatim here; run() AI-restyles it into casual texting
    tone before sending (verbatim is the fallback when the LLM is capped/down).

    Carries NOTHING fan-specific by construction — identical for every fan in the
    same slot, which is exactly what lets `_restyle_cache` be keyed on the line
    alone and shared across the run.

    `hour` lets a preview pin an arbitrary slot; None → the creator's current local
    hour. The V1 third line ('Will reply when I am back :)') is retired — it read
    canned; two paced bubbles land more human.

    `time_only` drops the ACTIVITY half and keeps only the clock: "it's Thursday
    afternoon in US". Same two-bubble shape, a much shorter second one — an opener
    that states where she is and what time it is there, with no scene attached. It
    does NOT depend on `time_activities`, so it still produces a line for a slot the
    creator never filled in (the activity path returns [] there)."""
    off = _clock_hours(cfg)
    if hour is None:
        hour = _model_hour(off)
    tod, activity = _time_activity(hour, cfg.get("time_activities") or {})
    if not activity and not time_only:
        return []
    # `where` carries its own " in " so the line degrades cleanly to "...it's Friday
    # morning" on an account with no location set — a dangling "in" is the kind of
    # thing a fan reads as a broken bot.
    location = (cfg.get("location") or "").strip()
    where = f" in {location}" if location else ""
    clock = f"it's {_model_weekday(off)} {tod}{where}"
    return [clock] if time_only else [f"{activity}... {clock}"]


# The time-only bubble OPENS with "it's" — "it's monday morning in US". The
# template already does; the restyle is what drops it ("thursday afternoon, in the
# US", "Thursday night, US."). Asking the prompt for it is not enough — a sampled
# rewrite obeys most of the time, and "most" is a line a fan reads. So the opener is
# re-attached in code after every path (fresh restyle, cached restyle, verbatim
# fallback), and the prompt asks for it only so the model doesn't fight the shape.
_ITS_PREFIX = re.compile(r"^\s*(it\s*['’´`]?\s*s|it\s+is)\b", re.IGNORECASE)


def _lead_with_its(line: str) -> str:
    """`line` guaranteed to start with an "it's" (already-present forms — it's / its /
    it´s / it is — are left exactly as the model wrote them)."""
    s = (line or "").strip()
    if not s or _ITS_PREFIX.match(s):
        return s
    return f"it's {s}"


# ── AI restyle of the activity bubble (casual texting tone) ───────────

_RESTYLE_TEMPERATURE = 0.9


def _one_line(text: str | None) -> str:
    """First non-empty line, unquoted. Both LLM calls in this module contract for a
    single line; this is where that contract is enforced when the model rambles."""
    out = (text or "").strip().strip('"').strip("'")
    return next((ln.strip() for ln in out.splitlines() if ln.strip()), "")


def _compose_restyle_system(cfg: dict, *, time_only: bool = False) -> str:
    persona = (cfg.get("persona")
               or "You are a warm, flirty OnlyFans creator texting a brand-new "
                  "subscriber.").strip()
    # The time-only line has NO activity in it, and the normal instruction ("keep
    # what you're doing") reads as a licence to supply one — the model would invent
    # a scene, which is the exact thing this mode exists to drop. So it gets its own
    # rule: keep the clock, add nothing.
    rule = (
        "Rewrite the given line from your welcome DM so it reads like a real, "
        "casual text you just fired off — relaxed texting tone, natural phrasing, "
        "a touch playful. It says ONLY the day / time of day and where you are. "
        "START the line with \"it's\". KEEP BOTH: the day + time of day, AND the "
        "place name exactly as written (never drop or vague-up the place). Add "
        "nothing else — do NOT invent what you're doing, plans, or a question: no "
        "activity at all. ONE short line only. Output only the rewritten line — "
        "no quotes, no preamble."
    ) if time_only else (
        "Rewrite the given line from your welcome DM so it reads like a real, "
        "casual text you just fired off — relaxed texting tone, natural phrasing, "
        "a touch playful. KEEP every fact (what you're doing, the weekday / time "
        "of day, where you're from). Do not add new facts or questions. ONE short "
        "line only. Output only the rewritten line — no quotes, no preamble."
    )
    return "\n\n".join([persona, rule])


# One restyle per (account, verbatim line), cached in-process: the line is
# IDENTICAL for every fan in the same time slot (it embeds only the activity +
# weekday + time-of-day + location — nothing fan-specific), and sampling showed
# the LLM's rewrites are near-identical paraphrases of it. So pay for ONE call
# and reuse it for the rest of the slot/day (the key rolls over naturally when
# the slot activity or weekday changes). EXCEPTION: a fan who has ALREADY
# texted us gets a fresh per-fan call — someone actively watching the chat
# shouldn't receive a visibly copy-pasted line. In-memory only: a restart just
# re-pays one call per slot.
_RESTYLE_CACHE_MAX = 64
_restyle_cache: dict[tuple[str, str], str] = {}


async def _fans_with_inbound(account_id: str, fan_ids: list[int]) -> set[int]:
    """Subset of `fan_ids` that has ≥1 INBOUND message — fans who already texted
    us. Their activity bubble gets a fresh per-fan restyle instead of the cached
    slot line. One grouped scan per tick (own session — house pattern)."""
    if not fan_ids:
        return set()
    async with get_session() as s:
        rows = (await s.execute(
            select(Message.fan_id).where(
                Message.account_id == str(account_id),
                Message.fan_id.in_([int(f) for f in fan_ids]),
                Message.direction == "in",
            ).distinct()
        )).all()
    return {int(r[0]) for r in rows}


async def _restyle_activity(
    account_id: str, fan_id: int, cfg: dict, model: str, line: str, *,
    time_only: bool = False,
) -> str:
    """One LLM call → the activity bubble in the creator's casual texting voice.
    Raises (incl. LLMCapExceeded) to the caller, which falls back to the verbatim
    template line — a restyle failure must never cost a fan their welcome."""
    res = await llm_client.chat(
        model=model,
        messages=[
            {"role": "system", "content": _compose_restyle_system(cfg, time_only=time_only)},
            {"role": "user", "content": line},
        ],
        purpose="welcome",
        account_id=account_id,
        fan_id=fan_id,
        temperature=_RESTYLE_TEMPERATURE,
    )
    return _one_line(res.content) or line


# ── Vault image (network-rewrite of the DOM "bot" folder click) ───────

# Folders to source the welcome image from, in priority order. Legacy "bot" (V1)
# first so existing accounts are unchanged; fall back to a welcome folder so images
# still attach on accounts that have no "bot" folder (e.g. jakabasej's "welcome
# script"). We deliberately do NOT fall through to arbitrary folders — picking from
# Streams/Posts/Stories would DM the wrong media. A future templates UI (W5) will
# make this per-account configurable.
_IMAGE_FOLDER_NAMES = ("bot", "welcome script", "welcome")


def _bot_folder_media_id(client, hour: int) -> int | None:
    """Pick a time-of-day vault photo (id at _photo_index(hour), clamped) from the
    first folder in `_IMAGE_FOLDER_NAMES` that exists and has photos. Best-effort:
    any failure / no named folder with photos → None (send text). Shared rule with
    send_followup so both pick from the same folder by the same time-of-day index;
    the cached vault preview (CACHING_PLAN.md) is keyed by the image's stable
    host+path, so the same id costs the bytes only once across welcomes/followups."""
    try:
        lists = client.vault_lists(view="main", limit=100)
    except Exception:
        log.debug("send_welcome vault_lists failed", exc_info=True)
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
            log.debug("send_welcome vault_media failed folder=%s", folder_id, exc_info=True)
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


# ── The burst's text, composed once for both paths ────────────────────

# The bubble ROLES a welcome burst is made of, in send order, are `pacing`'s and
# are IMPORTED at the top of this file under these same `_ROLE_*` names. `pacing`
# is the module that DISPATCHES on them (`welcome_burst_pace`), so it owns the
# vocabulary: a rename there now breaks this import loudly instead of quietly
# demoting the role to the documented "unknown role is paced as tail" fallback on
# a live send path. The Brain panel's preview caption echoes the same strings out
# of `bubble_roles` rather than re-deriving them from a bubble count.


async def _compose_bubbles(
    *, greeting: str, cfg: dict, hour: int, skip_time_bubble: bool,
    time_only: bool, ignore_pin: bool, question: str, strip_emoji_on: bool,
    restyle_fn: Callable[[str], Awaitable[str]] | None,
) -> tuple[list[str], list[str], bool]:
    """The whole text of one welcome burst: `(bubbles, roles, pinned)`.

    ONE composer for the live run and the Brain panel's preview, because the
    thing they must agree about is the PRECEDENCE, and it was written out twice:

        skip_time_bubble > time_only > pin > normal activity

    …followed, both times, by the same five tails — the "it's" opener, the
    verbatim question, the OF word restriction, the account-wide emoji strip and
    the blank filter. Five places knew that order (these two, plus three in
    BrainPanel), and the plan's own answer to keeping them aligned was "review
    discipline", which is what you reach for when there is no seam. A preview
    that composes a different burst from the one that ships is the single worst
    failure this panel has, because the operator approves what he was shown.

    `roles` runs parallel to `bubbles` — see `_ROLE_*`. It survives the blank
    filter, so a slot whose line strips down to nothing cannot leave the question
    wearing the time line's rhythm (or the caption calling it the time line).

    `restyle_fn` is the biggest difference between the two callers (`ignore_pin`
    is the other, and it is preview-only too — a Regenerate must not be answered
    with the operator's pinned line). The run path's takes the shared per-slot
    cache under a lock and counts what it did; the preview's deliberately bypasses
    both, so a regenerate cannot prime or pollute what the live run reuses.
    None ⇒ send the verbatim template line and make no LLM call at all.

    ⚠️ It is never called for a bubble that will not ship — the daily cap must
    not be spent on a line `skip_time_bubble` already dropped."""
    # The pin lookup does not even happen once the bubble it would fill has been
    # dropped or overruled: every pin ever minted is a rerolled ACTIVITY line, so
    # neither `time_only`'s clock line nor a removed bubble can be filled by one.
    pin = (None if (skip_time_bubble or ignore_pin or time_only)
           else _pinned_line(cfg, hour))
    pinned = False
    roles = [_ROLE_OPENER]
    if skip_time_bubble:
        bubbles = [greeting]
    elif pin is not None:
        bubbles = [greeting, pin]
        roles.append(_ROLE_GAP)
        pinned = True
    else:
        activity = _activity_bubble(cfg, hour, time_only=time_only)
        bubbles = [greeting, *activity]
        roles += [_ROLE_GAP] * len(activity)
        if activity and restyle_fn is not None:
            bubbles[1] = await restyle_fn(bubbles[1])
        if activity:
            # Re-attached in CODE after every path (fresh restyle, cached
            # restyle, verbatim fallback) — a sampled rewrite obeys "start with
            # it's" most of the time, and "most" is a line a fan reads.
            if time_only:
                bubbles[1] = _lead_with_its(bubbles[1])

    # The operator's question, word-for-word, after BOTH the pinned and the
    # composed paths — a pin replaces the activity line, never the question.
    if question:
        bubbles.append(question)
        roles.append(_ROLE_TAIL)

    # Last-mile per bubble: double the first vowel of any OF-restricted word (V1
    # ran apply_word_restriction on EVERY welcome). Covers the local template,
    # the restyled line and the LLM riff.
    bubbles = [apply_word_restriction(b) for b in bubbles]
    if strip_emoji_on:
        bubbles = [strip_emojis(b) for b in bubbles]
    kept = [(b, r) for b, r in zip(bubbles, roles) if b.strip()]
    return [b for b, _ in kept], [r for _, r in kept], pinned


# ── Compose-only preview (no send, no state write) ────────────────────

async def preview_compose(
    account_id: str, payload: dict | None = None, *,
    fan_id: int | None = None, test_name: str | None = None,
    model: str | None = None, restyle: bool = False, slot: str | None = None,
    config: dict | None = None, ignore_pin: bool = False,
) -> dict:
    """Compose the welcome a real run WOULD produce for one fan — the text + the
    chosen time-of-day image id — WITHOUT sending and WITHOUT writing send-state.
    Powers the Brain panel's "Preview"/"Regenerate" buttons (mirrors
    nudge_online.preview_compose).

    Name→text resolution mirrors run(): a resolvable real name takes the
    deterministic 'precious' local template; only a random handle / number with no
    usable name falls back to a single LLM riff. When no `fan_id` is given we greet a
    representative name so the verbatim preview is deterministic and free.

    `slot` pins any of the 6 time-of-day slots (else the creator's current local
    hour); an unknown slot falls back to now. `config` is a DRAFT override
    (unsaved on-screen edits — persona / activities / images / model) shallow-merged
    over the saved brain so a preview is WYSIWYG.

    `restyle=True` runs the SAME AI restyle of the activity bubble that a real run
    sends, so the preview shows the actual shipped text (and each regenerate rerolls
    a fresh sample). It is a real, cap-governed, audited `llm_client.chat` call — a
    cap hit degrades to the verbatim line (`cap_hit`), never an error. It deliberately
    does NOT read or write the shared per-slot `_restyle_cache` (so a preview can't
    prime or pollute what the live run reuses). Beyond that LLM audit/cost row this
    writes nothing: no send, no `welcome_sent`, no `messages`, no vault network call.

    If the slot is PINNED (operator kept a line), the preview shows that exact line
    (weekday refreshed) and `pinned=True`, skipping the restyle — this is what will
    ship. `ignore_pin=True` (the "Regenerate" button) bypasses the pin to sample a
    fresh candidate the operator can keep in its place.

    `time_only=True` mirrors the rule's own knob: bubble 2 is the short clock line
    (no activity). It bypasses the pin for the same reason the sender does — a pin
    is a stored ACTIVITY line, so honouring it would show (and ship) the long line
    the checkbox just turned off. That clock line still wears the `gap` role (it
    IS bubble 2, and `pacing` must pace it as one), so the roles alone cannot say
    whether what the operator is looking at may be PINNED — `pinnable` says it.

    `skip_time_bubble=True` mirrors the rule's own knob and OUTRANKS both: there is
    no bubble 2 at all, so the pin lookup, the activity line, the restyle (and its
    LLM call) and the "it's" opener are all skipped. Same precedence as the sender
    — `skip_time_bubble` > `time_only` > pin > normal. The flag itself is NOT
    echoed in the result: `bubble_roles` is, and a missing `gap` role is the same
    fact for every reason a burst can lack an activity bubble, not just this one.

    `question` mirrors the rule's `question` knob: the operator's own question,
    appended word-for-word as the last bubble (never restyled) — carried from the
    form so the preview shows the full burst before the rule is saved.

    ⚠️ THE RULE KNOBS ARRIVE AS ONE `payload` DICT, read with the SAME
    expressions `run()` uses, and that is the point. They used to be four named
    parameters threaded through eight hops — form state → mutation input type →
    HTTP body → a Pydantic model that silently DROPS anything it does not declare
    → the route's named forwarding → here. Every hop is a place to forget one,
    the code's own comment said forgetting one makes the preview "quietly show a
    burst nobody will receive", and a warning is not a mitigation. Now a knob the
    panel puts on the rule reaches this function whether or not anybody
    remembered it, and it is read here exactly as the sender reads it. Precedent:
    `mass_nudge.preview_compose(account_id, payload, ...)` on this same route.

    The remaining named arguments are the ones that are NOT rule knobs —
    `restyle` / `slot` / `config` / `ignore_pin` / `model` are preview controls
    (which slot to show, the unsaved draft brain, "Regenerate") and belong to the
    panel, not to the rule.

    Returns the send-shape as `bubbles` (image rides on bubble 1) + joined `text`,
    plus `image`, `name`, `slot`, and `restyled`/`cap_hit`/`pinned` flags."""
    # Read with `run()`'s own expressions, so "what the preview shows" and "what
    # the sender sends" cannot answer the same knob differently.
    payload = payload or {}
    # `False`, not the catalog's `True`: see the `time_only` TODO in `run()` for
    # why the sender reads an absent key as OFF and must keep doing so.
    time_only = bool_knob(payload, "time_only", False)
    skip_time_bubble = bool_knob(payload, "skip_time_bubble", False)
    question = str(payload.get("question") or "").strip()
    gif_id = str(payload.get("gif_id") or "").strip()

    _wv = (await load_voice_blocks(account_id)).voice
    cfg = await _load_ai_config(account_id)
    # Draft override wins over the saved brain (None never clobbers); the UI sends the
    # full time_activities/time_images dicts, so a shallow merge is correct. Both
    # sides hold the RAW clock columns and `_clock_hours` resolves whatever wins, so
    # the draft's clock cannot land in a different unit than the brain's — that
    # mismatch is what previewed Dana 3h away from what her welcome sent.
    if config:
        cfg = {**cfg, **{k: v for k, v in config.items() if v is not None}}
    strip_emoji_on = await load_strip_emojis(account_id)  # account-wide emoji strip

    # Pin the requested slot's representative hour; unknown/empty slot → current hour.
    hour = _slot_hour(slot)
    if hour is None:
        hour = _model_hour(_clock_hours(cfg))

    if fan_id is not None:
        sub = {"id": int(fan_id), "name": test_name, "username": None}
        name = await _resolve_welcome_name(account_id, int(fan_id), sub)
    else:
        # No fan chosen → a representative resolvable name so the preview shows the
        # signature local template deterministically (no LLM call, no cost).
        name = _name_token(test_name) or "Alex"
        sub = {"id": 0, "name": test_name or name, "username": None}

    greeting = _local_greeting(name or _greet_token(sub, _wv), _wv)
    restyled = False
    cap_hit = False

    async def _restyle_line(line: str) -> str:
        """The preview's restyle — exactly the rewrite that ships, deliberately
        CACHE-BYPASSED: a fresh sample per regenerate, and it must never prime or
        pollute the per-slot cache the live run reuses. A cap hit or any failure
        degrades to the verbatim line, never an error."""
        nonlocal restyled, cap_hit
        if not restyle:
            return line
        rmodel = (model or cfg.get("model")
                  or await resolve_model(account_id, "welcome", None))
        try:
            styled = await _restyle_activity(
                account_id, int(fan_id or 0), cfg, rmodel, line,
                time_only=time_only)
        except LLMCapExceeded:
            cap_hit = True
            return line
        except Exception:
            log.debug("preview restyle failed account=%s — verbatim line", account_id,
                      exc_info=True)
            return line
        if styled and styled != line:
            restyled = True
            return styled
        return line

    # Composed by the SAME function the live run uses — one precedence, one set
    # of tails, so a preview cannot show a burst the sender would not send.
    bubbles, roles, pinned = await _compose_bubbles(
        greeting=greeting, cfg=cfg, hour=hour,
        skip_time_bubble=skip_time_bubble, time_only=time_only,
        ignore_pin=ignore_pin, question=question,
        strip_emoji_on=strip_emoji_on, restyle_fn=_restyle_line)
    # Bubble 4 is returned SEPARATELY, never appended to `bubbles`: it carries no
    # text, and the panel renders it as an image. Folding it in would put a bare
    # giphy id through apply_word_restriction and into the joined `text`.
    #
    # It IS echoed rather than left to the panel, even though nothing here reads
    # it, so that one preview response is one whole send shape. The panel renders
    # the block from this dict alone; reading the GIF from live form state instead
    # would let a stale composition sit beside a GIF the operator swapped after
    # pressing Preview — the burst on screen would be one nobody ever composed.
    return {"text": "\n\n".join(bubbles), "bubbles": bubbles,
            "image": _slot_image_id(cfg, hour),
            "name": name, "slot": _slot_key(hour),
            "restyled": restyled, "cap_hit": cap_hit, "pinned": pinned,
            # May the operator KEEP the line he is looking at? Decided HERE,
            # where the knobs are, and echoed as one bit — because every attempt
            # to re-derive it on the client has been wrong. A pin is a stored
            # ACTIVITY line (`_compose_bubbles` refuses to look one up under
            # `time_only`, above), but `time_only`'s clock line carries the SAME
            # `gap` role, so "did the server emit a gap bubble" answers
            # `skip_time_bubble` and the unfilled slot and NOT this one. Pinning
            # the clock line stores something that never ships while the checkbox
            # is on, cannot be un-pinned from this screen (`pinned` stays False,
            # so the Unpin button never appears), and becomes that slot's
            # permanent activity line the moment the checkbox comes off.
            #
            # `ignore_pin` (Regenerate) is deliberately NOT in here: it composes a
            # fresh ACTIVITY line precisely so the operator can keep it.
            "pinnable": (_ROLE_GAP in roles) and not time_only,
            # ROLES, echoed one per bubble — not left to the panel to count.
            # Nothing about a welcome burst's LENGTH says what its bubbles are:
            # `skip_time_bubble` and an unfilled slot both drop the middle one,
            # and the operator's question slides into index 1. A positional
            # caption ("the 2nd line is the AI-restyled activity line") then
            # describes a bubble that is not there — and tells the operator to
            # turn on a restyle for his own verbatim question. The panel names
            # each bubble from THIS list instead.
            "bubble_roles": roles,
            # (No `skip_time_bubble` echo. It was kept "for the panel's checkbox
            # wiring", and the panel's checkbox reads the RULE's own payload
            # (`welcomeRule.payload?.skip_time_bubble`); nothing anywhere read it
            # off the preview. `bubble_roles` already answers the only question a
            # consumer had — "is there an activity bubble in THIS preview" — and
            # answers it for the unfilled-slot shape the flag says nothing about.)
            "gif_id": gif_id or None}
