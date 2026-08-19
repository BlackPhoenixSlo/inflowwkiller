"""
service/automations/send_welcome.py — Automation A09: send_welcome.

Spec: library/one_section_of_automations/09_send_welcome.md.

TRIGGER-SOURCE VERDICT (verified 2026-06-04): the WS pump does NOT emit a
new-subscriber/subscription event into `event_inbox` in any consumable form.
`event_transcoder.transcode` only fully transcodes `api2_chat_message` and the
PPV-unlock family; EVERY other event (subscribes included) falls through to a
best-effort fan-touch + a one-shot "unknown shape" log — nothing an automation
can hang off. So this automation is SCHEDULED, not event-driven: an
`automation_rules` row with `trigger_json = {"every_seconds": 300}` enqueues a
job every ~5 min (the executor materializes it like any periodic rule); each run
polls OF's subscribe-notifications feed and welcomes every new subscriber not
already in `welcome_sent`.

It mirrors the scrape_chats reference in automation_executor.py:
  • of_client ONLY (no DOM), constructed via the executor's `_make_client` seam
    so tests inject a fake with no network.
  • its OWN AsyncSession per write.
  • the welcome itself is DETERMINISTIC — `_local_greeting` + `_activity_bubble`,
    no LLM, so a daily-cap trip can never cost a fan their welcome. persona /
    location / time-of-day come from `account_ai_config`. The single remaining
    llm_client.chat() call is the optional activity-bubble restyle (one per slot,
    cached); that call writes the `grok_calls` audit row and enforces the
    per-account daily cost cap atomically, so we don't re-implement either.
  • the existing optimistic send path: `of_client.send_message` →
    `attribution.write_outbound_attribution` (credits the system Automation
    employee, since no X-Employee-Id exists for a background run). The WS pump
    skips outbound, so this is the only producer of the outbound `messages` row.
  • dedup via `welcome_sent` (account_id, fan_id) — persisted, so a restart
    re-polls, sees the fan already welcomed, and skips: a sub yields EXACTLY one
    welcome. `welcome_sent` is written only AFTER a confirmed 200 send, so a send
    failure never marks a fan welcomed-without-a-welcome.
  • NOT-NEW gate: the `type=subscribed` feed includes RENEWALS / re-subs and
    `welcome_sent` has no row for fans pre-dating this automation, so an
    ESTABLISHED fan (even a whale mid-funnel) is skipped rather than re-welcomed
    as if brand new. A genuinely new sub may still carry a little history first
    (a mass blast → 1-2 outbound; their own opening DMs → a handful inbound), so
    the gate only trips once a fan EXCEEDS the tolerance: >`new_max_outbound`
    (default 2) outbound OR >`new_max_inbound` (default 8) inbound. Plus the
    cross-automation contact guard (`contact_guard_excludes`) so a fan another
    automation just touched isn't double-messaged. Both run on the notification
    path only — `test_fan` forces a send past them.
  • per-(account, fan) send-lease so A05/A06/A07/A11 can't double-message the
    same fan in an overlapping cycle; run_once's per-(account, kind) lock stops
    a slow tick stacking on the next.

SEND SHAPE (2026-07): the deterministic welcome goes out as TWO paced bubbles —
bubble 1 is the stutter greeting with the time-of-day image attached, bubble 2 is
the activity line AI-restyled into the creator's casual texting voice (verbatim
template on any LLM failure; V1's canned third line is retired). The restyle is
ONE cached LLM call per slot line shared across fans; a fan who already texted us
gets a fresh per-fan call (see _restyle_cache). An operator-written `question`
(payload) rides as an optional THIRD bubble, sent word-for-word — never restyled,
never near an LLM. An operator-picked `gif_id` rides as an optional FOURTH
bubble — a send carrying a top-level `giphyId` beside EMPTY text (the verified
wire shape, same as ai_chatter's cat stickers), so it is composed apart from the
text but sent as part of the SAME burst: one failure policy for all four.

Each text bubble is held for its human typing time with the live "...is typing"
indicator (webhook_config_json.typing_wpm / typing_indicator — same knobs as
welcome_chatter_for_info); the GIF is picked rather than typed, so it gets a flat
beat and no indicator.

Payload knobs (all optional): `limit` (notifications fetched), `max_welcomes`
(per-run batch cap), `model` (LLM override), `dry_run` (generate but don't send),
`with_image` (attach a time-of-day bot-folder vault image, default True — same
picker as send_followup), `restyle` (AI-restyle the activity bubble, default
True; False sends the verbatim template line, zero LLM cost), `time_only`
(bubble 2 drops the activity and says ONLY the day/time-of-day/location —
"it's Thursday afternoon in US"; default False), `question` (an operator-written
question appended VERBATIM as a third bubble — no restyle, no LLM; blank/absent
= off), `gif_id` (a giphy id sent as a fourth, text-less bubble; blank/absent =
off), `test_fan` (+ `test_name`) to force one hardcoded recipient.

Returns a stats dict → automation_runs.stats_json.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

import automation_executor as ax  # _make_client / _parse_iso / fan-lease seams
import llm_client                  # call .chat at runtime so tests can patch it
from . import rhythm  # tz_hours_for — THE clock (fixed offset first, zone as fallback)
from attribution import write_outbound_attribution
from audiences import contact_guard_excludes, resolve_window_hours
from automation_registry import register
from ._common import (apply_word_restriction, hold_with_typing, load_voice_blocks,
                      load_hard_skip_ids, load_strip_emojis,
                      load_typing_indicator, load_typing_wpm, name_token,
                      resolve_fan_name, resolve_model, send_dropping_bad_media,
                      skip_unreachable_fan, strip_emojis, typing_delay_seconds)
from db.engine import get_session
from db.models import AccountAiConfig, Fan, FanProfile, Message, WelcomeSent
from llm_client import LLMCapExceeded


log = logging.getLogger("of-relay.automation.send_welcome")

_DEFAULT_NOTIF_LIMIT = 50      # how many subscribe-notifications to pull per tick
_DEFAULT_MAX_WELCOMES = 25     # batch cap per run (logged when it bites)
_GUARD_DEFAULT_H = 12.0        # cross-automation contact-guard window (payload override)
# How long a fan rests after his welcome lands, before the chat engine may answer.
#
# A new subscriber is the hottest lead there is and he replies FAST — measured
# 07-26, one answered 50 seconds after the welcome. He then waited ~14 minutes,
# because two brakes were running and the wrong one won: this sender set a
# deliberate 10-minute rest (`_FAN_COOLDOWN_S`) and then left its 15-minute fan
# LEASE lying around to expire (`_LEASE_TTL_S`), and the generic infra timeout
# silently outranked the policy. The lease is now handed back on a confirmed
# send, so this constant is the only thing pacing him — one brake, one number,
# and it is the one somebody chose.
_WELCOME_REST_S = 150          # 2.5 min — long enough not to talk over the welcome
# Beat before bubble 4. A GIF is PICKED, not typed, so it gets a flat pause and no
# "...is typing" frame — typing_delay_seconds would price it as if she typed it.
_GIF_HOLD_S = 3.0

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


# ── New-subscriber parsing (defensive — OF notification shape varies) ──

def _find_user(item: dict) -> dict | None:
    """Pull the subscriber user-blob out of one notification item. OF nests it
    under a handful of keys depending on the feed/version, so we sniff several."""
    for key in ("user", "fromUser", "subscriber", "author"):
        u = item.get(key)
        if isinstance(u, dict) and u.get("id"):
            return u
    data = item.get("data")
    if isinstance(data, dict):
        for key in ("user", "relatedUser", "subscriber"):
            u = data.get(key)
            if isinstance(u, dict) and u.get("id"):
                return u
    return None


def _extract_new_subscribers(resp: object) -> list[dict]:
    """Notifications response → de-duped [{id, username, name}] in feed order."""
    if isinstance(resp, list):
        items = resp
    elif isinstance(resp, dict):
        items = resp.get("list") or resp.get("notifications") or resp.get("items") or []
    else:
        items = []
    out: list[dict] = []
    seen: set[int] = set()
    for it in items:
        if not isinstance(it, dict):
            continue
        # OF dropped the `type=` query filter (it now 400s), so the feed is mixed
        # (tips/comments/mentions/subscribes). Keep only subscribe events — items
        # with a subscribe-ish `type` (e.g. "subscribed"). Untyped items (test
        # fixtures / unknown shapes) pass through for back-compat.
        t = str(it.get("type") or "").lower()
        if t and "subscrib" not in t:
            continue
        user = _find_user(it)
        if not user:
            continue
        try:
            uid = int(user.get("id"))
        except (TypeError, ValueError):
            continue
        if uid in seen:
            continue
        seen.add(uid)
        out.append({"id": uid, "username": user.get("username"), "name": user.get("name")})
    return out


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




async def _load_welcomed(account_id: str) -> set[int]:
    async with get_session() as s:
        rows = (await s.execute(
            select(WelcomeSent.fan_id).where(WelcomeSent.account_id == str(account_id))
        )).all()
    return {int(r[0]) for r in rows}


async def _established_fan_ids(
    account_id: str, fan_ids: list[int], *, max_outbound: int, max_inbound: int
) -> set[int]:
    """Subset of `fan_ids` that look like an EXISTING relationship (so they must
    NOT be welcomed as if brand new), not a genuinely fresh subscriber.

    Why this gate exists: the OF subscribe-notifications feed (`type=subscribed`)
    carries RENEWALS and re-subscribes, not just first-time subs, and the
    `welcome_sent` ledger has no row for fans who pre-date this automation. So
    `welcome_sent`-only dedup re-welcomes established fans (even a long-tenure
    whale mid-funnel) the moment their sub renews / they reappear in the feed.

    A genuinely new sub can still carry a LITTLE history before the welcome tick,
    so we DON'T treat any message as disqualifying:
      • a mass blast may land first → a couple OUTBOUND rows (incl. the optimistic
        placeholders mass sends write), and
      • the fan may fire off a few opening messages → several INBOUND rows.
    We treat a fan as established only once they EXCEED a tolerance: more than
    `max_outbound` outbound OR more than `max_inbound` inbound messages. One
    grouped scan over the (≤`limit`) notification fans per tick.

    The scan itself is HOISTED to audience_include.established_fan_ids so the
    audience roster-diff auto-add applies the identical renewal tolerance; this
    stays as the module's local name so its call sites and tests don't move."""
    import audience_include
    return await audience_include.established_fan_ids(
        account_id, fan_ids, max_outbound=max_outbound, max_inbound=max_inbound)


async def _mark_welcomed(account_id: str, fan_id: int, username: str | None) -> None:
    """Idempotent welcome_sent claim — written only after a confirmed send."""
    async with get_session() as s:
        await s.execute(
            sqlite_insert(WelcomeSent)
            .values(
                account_id=str(account_id),
                fan_id=int(fan_id),
                fan_username=username,
                sent_at=datetime.utcnow(),
            )
            .on_conflict_do_nothing(index_elements=["account_id", "fan_id"])
        )


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
    source — all that's left is the raw OF handle. Three shapes, resolved in code:

        u4471223     → 'fan #4471223'   (a bare id is an id, not a name)
        xx_rider_92  → 'rider'          (the word inside the handle)
        (unusable)   → 'cutie'          (letterless handle — vanishingly rare)

    This used to be an LLM call per nameless fan. It produced exactly this shape
    ('Hey B-B-B-Bold bigdaddy69 ! !!'), so it was paying per fan for a table lookup
    the named path already does for free — and a daily-cap trip cost those fans
    their welcome entirely. Deterministic means no cost, no cap, no variance."""
    target = (sub.get("name") or sub.get("username") or "").strip()
    if re.fullmatch(r"u?\d+", target):
        return f"fan #{re.sub(r'\D', '', target)}"
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


# ── Compose-only preview (no send, no state write) ────────────────────

async def preview_compose(
    account_id: str, *, fan_id: int | None = None, test_name: str | None = None,
    model: str | None = None, restyle: bool = False, slot: str | None = None,
    config: dict | None = None, ignore_pin: bool = False,
    time_only: bool = False, question: str | None = None,
    gif_id: str | None = None,
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
    the checkbox just turned off.

    `question` mirrors the rule's `question` knob: the operator's own question,
    appended word-for-word as the last bubble (never restyled) — carried from the
    form so the preview shows the full burst before the rule is saved.

    Returns the send-shape as `bubbles` (image rides on bubble 1) + joined `text`,
    plus `image`, `name`, `slot`, and `restyled`/`cap_hit`/`pinned` flags."""
    _wv = (await load_voice_blocks(account_id)).voice
    cfg = await _load_ai_config(account_id)
    # Draft override wins over the saved brain (None never clobbers); the UI sends the
    # full time_activities/time_images dicts, so a shallow merge is correct. Both
    # sides hold the RAW clock columns and `_clock_hours` resolves whatever wins, so
    # the draft's clock cannot land in a different unit than the brain's — that
    # mismatch is what previewed Isabelle 3h away from what her welcome sent.
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
    # A PINNED slot line IS bubble 2 (unless the operator hit Regenerate →
    # ignore_pin): the exact approved line that will ship, weekday-refreshed, no LLM
    # call. It ships even when the slot's raw activity was later blanked.
    restyled = False
    cap_hit = False
    pinned = False
    pin = None if (ignore_pin or time_only) else _pinned_line(cfg, hour)
    if pin is not None:
        bubbles = [greeting, pin]
        pinned = True
    else:
        bubbles = [greeting, *_activity_bubble(cfg, hour, time_only=time_only)]
        if restyle and len(bubbles) > 1:
            # AI restyle of the activity bubble — exactly what ships. Cache-bypassed
            # (fresh sample per regenerate; never touches the live run's
            # _restyle_cache); a cap hit or any failure degrades to the verbatim line.
            rmodel = (model or cfg.get("model")
                      or await resolve_model(account_id, "welcome", None))
            try:
                styled = await _restyle_activity(
                    account_id, int(fan_id or 0), cfg, rmodel, bubbles[1],
                    time_only=time_only)
                if styled and styled != bubbles[1]:
                    bubbles[1] = styled
                    restyled = True
            except LLMCapExceeded:
                cap_hit = True
            except Exception:
                log.debug("preview restyle failed account=%s — verbatim line", account_id,
                          exc_info=True)
        if time_only and len(bubbles) > 1:
            bubbles[1] = _lead_with_its(bubbles[1])

    # Bubble 3: the operator's question, word-for-word — rides both the pinned
    # and the composed paths, exactly like the send does.
    question = (question or "").strip()
    if question:
        bubbles.append(question)

    bubbles = [apply_word_restriction(b) for b in bubbles]
    if strip_emoji_on:
        bubbles = [strip_emojis(b) for b in bubbles]
    bubbles = [b for b in bubbles if b.strip()]
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
            "gif_id": (gif_id or "").strip() or None}


# ── The automation ───────────────────────────────────────────────────

@register("send_welcome")
async def run(account_id: str, payload: dict, *, run_id: int) -> dict:
    payload = payload or {}
    _wv = (await load_voice_blocks(account_id)).voice
    dry_run = bool(payload.get("dry_run"))
    with_image = payload.get("with_image", True)
    limit = int(payload.get("limit") or _DEFAULT_NOTIF_LIMIT)
    max_welcomes = int(payload.get("max_welcomes") or _DEFAULT_MAX_WELCOMES)

    cfg = await _load_ai_config(account_id)
    strip_emoji_on = await load_strip_emojis(account_id)  # account-wide emoji strip
    model = await resolve_model(account_id, "welcome", payload.get("model"))
    typing_wpm = await load_typing_wpm(account_id)       # per-bubble "typing" pacing
    typing_on = await load_typing_indicator(account_id)  # live "...is typing" frames
    restyle = bool(payload.get("restyle", True))         # AI-restyle the activity bubble
    # Bubble 2 says only the day / time of day / location — no activity. Off by
    # default: every account that has ever run keeps the line it sends today.
    # ON BY DEFAULT — but stamped at rule CREATION (`automation_rules_api`
    # schema default True), not defaulted here. Defaulting at the read turns
    # every rule ever written without the key, including sixteen tests that
    # assert the activity path, into a clock-only welcome retroactively. The
    # schema default gives a new account the new behaviour; existing rules were
    # migrated by an explicit flip, so an absent key stays absent-means-off and
    # keeps meaning what it meant when it was written.
    time_only = bool(payload.get("time_only"))
    # Bubble 3 (optional): an operator-written question appended VERBATIM — no
    # restyle, no LLM, the same exact text for every fan. Blank/absent = off.
    # The default ("what's yours?") is stamped at rule creation/save by the Brain
    # panel, NOT here — same pattern (and same reason) as time_only above: a read
    # default would retroactively append it to every rule saved before the knob
    # existed.
    question = str(payload.get("question") or "").strip()
    # Bubble 4 (optional): the giphy id the operator picked in the Brain panel,
    # sent as its own text-less bubble. Blank/absent = off, and stamped at save by
    # the panel — never defaulted here, for the same reason as `question` above.
    gif_id = str(payload.get("gif_id") or "").strip()

    client = await asyncio.to_thread(ax._make_client, account_id)

    # Time-of-day vault image. Deterministic per hour, identical for every fan this
    # tick, so resolve ONCE — not per fan. Skipped on dry runs. Prefer the account's
    # configured per-slot image id (time_images); fall back to the legacy folder
    # picker when no slot id is set.
    hour = _model_hour(_clock_hours(cfg))
    bot_media_id: int | None = None
    if with_image and not dry_run:
        bot_media_id = _slot_image_id(cfg, hour)
        if bot_media_id is None:
            bot_media_id = await asyncio.to_thread(_bot_folder_media_id, client, hour)

    # Source the candidate subscribers.
    test_fan = payload.get("test_fan")
    if test_fan:
        nm = payload.get("test_name") or ""
        subs = [{"id": int(test_fan), "username": nm or None, "name": nm or None}]
    else:
        # Scope the fetch to the subscribe feed. `type=subscribed` (past tense,
        # the value the OF web UI's /my/notifications/subscribed tab uses) is the
        # ONLY working filter — `subscribes`/`subscriptions` 400 (verified live
        # 2026-06). The untyped feed is unusable: a content-moderation event
        # (`deactivated_media`) flood can bury every subscribe past offset 1000+,
        # silently starving welcomes (this happened to Ava 2026-06-10 → 4 days of
        # missed welcomes). _extract_new_subscribers still filters client-side as a
        # belt-and-braces guard.
        resp = await asyncio.to_thread(
            client.notifications, limit=limit, offset=0, type="subscribed",
        )
        subs = _extract_new_subscribers(resp)

    # Dedup against welcome_sent (survives restarts → exactly one welcome).
    welcomed = await _load_welcomed(account_id)
    new_subs = [s for s in subs if s["id"] not in welcomed]

    # Auto-discovery hygiene — NOTIFICATION path only (`test_fan` is an explicit
    # force, so the live drivers / UI "send test" bypass these). Two filters:
    #   • NOT-NEW: the `type=subscribed` feed includes renewals / re-subs, and
    #     `welcome_sent` has no row for fans who pre-date this automation — so
    #     welcoming a fan we ALREADY have a real conversation with would re-welcome
    #     an established fan (e.g. a $999 whale mid-funnel). A genuinely new sub may
    #     still have a LITTLE history first (a mass blast → 1-2 outbound; their own
    #     opening DMs → a handful inbound), so we only skip once a fan EXCEEDS the
    #     tolerances (default >2 outbound or >8 inbound; payload-overridable).
    #   • CONTACT GUARD: a fan another automation touched inside the window
    #     shouldn't ALSO get a welcome this tick (defense-in-depth — an actually-
    #     new sub has no prior outbound, so this never blocks a real first welcome).
    max_out = int(payload.get("new_max_outbound", 2))
    max_in = int(payload.get("new_max_inbound", 8))
    skipped_existing = 0
    skipped_guard = 0
    skipped_restricted = 0   # muted peer-creator / hand-restricted "no automations"
    if not test_fan and new_subs:
        known = await _established_fan_ids(
            account_id, [s["id"] for s in new_subs],
            max_outbound=max_out, max_inbound=max_in)
        if known:
            new_subs = [s for s in new_subs if s["id"] not in known]
            skipped_existing = len(known)
            log.info("send_welcome skipped %d established fan(s) account=%s",
                     skipped_existing, account_id)
        guard_h = resolve_window_hours(payload.get("guard_hours"), _GUARD_DEFAULT_H)
        if new_subs and guard_h > 0:
            guard_ids = await contact_guard_excludes(account_id, outbound_hours=guard_h)
            if guard_ids:
                before = len(new_subs)
                new_subs = [s for s in new_subs if s["id"] not in guard_ids]
                skipped_guard = before - len(new_subs)
        # Durably restricted (muted peer-creator / hand-restricted) never get welcomed.
        if new_subs:
            hard_skip = await load_hard_skip_ids(account_id)
            if hard_skip:
                before = len(new_subs)
                new_subs = [s for s in new_subs if s["id"] not in hard_skip]
                skipped_restricted = before - len(new_subs)

    # Include-only audience — ordered AFTER the auto-add fast-path, on purpose:
    # a provably-new sub is enrolled into the operator's folder first (pending
    # OF confirm) and counts as INSIDE, so enforce mode never eats the welcome
    # of the very fan it is about to admit. Fans the ledger refuses (returning
    # churner, pre-baseline) stay subject to the gate: shadow logs, enforce skips.
    skipped_audience = 0
    if new_subs:
        import audience_include as _audiences
        from . import audience_sync as _audience_sync
        _pol = await _audiences.automation_audience(account_id)
        if _pol.mode != "off":
            enrolled: set[int] = set()
            if _pol.auto_add:
                for _s in new_subs:
                    try:
                        if await _audience_sync.fast_path_enroll(
                                account_id, _s["id"], client=client):
                            enrolled.add(int(_s["id"]))
                    except Exception:  # noqa: BLE001 — the roster diff is the guarantee
                        log.warning("audience fast-path enroll failed account=%s fan=%s",
                                    account_id, _s["id"], exc_info=True)
            kept = set(await _audiences.filter_candidates(
                account_id, [s["id"] for s in new_subs], kind="send_welcome",
                policy=_pol, extra_allowed_ids=enrolled))
            before = len(new_subs)
            new_subs = [s for s in new_subs if int(s["id"]) in kept]
            skipped_audience = before - len(new_subs)

    new_total = len(new_subs)

    batch_capped = new_total > max_welcomes
    if batch_capped:
        log.warning(
            "send_welcome batch capped account=%s new=%d cap=%d (rest next tick)",
            account_id, new_total, max_welcomes,
        )
        new_subs = new_subs[:max_welcomes]

    sent = 0
    skipped_locked = 0
    errors = 0
    cap_hit = False
    image_attached = 0
    restyled = 0          # fresh LLM restyles this run
    restyled_cached = 0   # bubbles served from the per-slot restyle cache
    pinned_used = 0       # bubbles served from an operator-pinned slot line (no LLM)
    gifs_sent = 0         # bubble-4 GIFs that landed
    restyle_capped = False  # cap tripped mid-run → stop attempting restyles

    # Fans who already texted us get a FRESH per-fan restyle (never the cached
    # slot line); everyone else shares one cached rewrite per slot.
    texted_ids: set[int] = set()
    if restyle and new_subs:
        texted_ids = await _fans_with_inbound(account_id, [s["id"] for s in new_subs])

    skipped_cooldown = 0
    for sub in new_subs:
        fan_id = sub["id"]
        # Another automation messaged this fan recently → rest it (W3 cooldown).
        if await ax.fan_on_cooldown(account_id, fan_id):
            skipped_cooldown += 1
            continue
        # One bot message per fan per cycle — don't race A05/A06/A07/A11.
        if not await ax.acquire_fan_lease(account_id, fan_id, "send_welcome"):
            skipped_locked += 1
            continue
        sent_ok = False
        try:
            try:
                # Only the greeting TOKEN varies — a resolvable name (from the notif,
                # the fan's real_name, or a stored nickname), else a token derived
                # from the raw handle. Same deterministic stutter either way, and no
                # LLM: the daily cap can no longer cost a nameless fan their welcome.
                name = await _resolve_welcome_name(account_id, fan_id, sub)
                greeting = _local_greeting(name or _greet_token(sub, _wv), _wv)
            except Exception:
                errors += 1
                log.warning("send_welcome generate failed account=%s fan=%s",
                            account_id, fan_id, exc_info=True)
                continue

            # An operator-PINNED line for this slot IS bubble 2: send exactly the
            # approved line (weekday refreshed to today), no LLM call, for every fan
            # this slot. That is the whole point of the pin — "reroll until I like
            # one, then use THAT" — so it overrides the payload's restyle flag, the
            # texted-fan fresh-call path, and a slot whose raw activity was later
            # blanked.
            #
            # `time_only` is the ONE thing that outranks it. Every pin ever minted is
            # a rerolled ACTIVITY line, so a pinned slot would keep shipping the long
            # scene after the operator ticked the box that says "drop the scene" — the
            # checkbox would silently do nothing on exactly the slots someone cared
            # enough about to pin. Unticking it restores the pin untouched.
            pinned = None if time_only else _pinned_line(cfg, hour)
            if pinned is not None:
                bubbles = [greeting, pinned]
                pinned_used += 1
            else:
                # AI-restyle the activity bubble into the creator's casual texting
                # voice so the templated line doesn't read canned. ONE cached rewrite
                # per slot line is shared by every fan (the rewrites are near-identical
                # paraphrases — no point paying per fan); a fan who already texted us
                # gets a fresh per-fan call instead. Best-effort: any failure (cap
                # included) falls back to the verbatim template line — a restyle
                # hiccup must never cost a fan their welcome. Once the daily cap trips
                # we stop attempting restyles for the rest of the run.
                bubbles = [greeting, *_activity_bubble(cfg, hour,
                                                       time_only=time_only)]
                if restyle and len(bubbles) > 1 and not restyle_capped:
                    ck = (str(account_id), bubbles[1])
                    fresh = fan_id in texted_ids
                    cached = None if fresh else _restyle_cache.get(ck)
                    if cached is not None:
                        if cached != bubbles[1]:
                            restyled_cached += 1
                        bubbles[1] = cached
                    else:
                        try:
                            styled = await _restyle_activity(
                                account_id, fan_id, cfg, model, bubbles[1],
                                time_only=time_only)
                            if not fresh:
                                # Cache even a verbatim echo — a model that refuses to
                                # rewrite shouldn't be re-asked for every fan this slot.
                                _restyle_cache[ck] = styled
                                while len(_restyle_cache) > _RESTYLE_CACHE_MAX:
                                    _restyle_cache.pop(next(iter(_restyle_cache)))
                            if styled != bubbles[1]:
                                restyled += 1
                                bubbles[1] = styled
                        except LLMCapExceeded:
                            cap_hit = True
                            restyle_capped = True
                            log.warning("send_welcome restyle capped account=%s — verbatim "
                                        "activity line for the rest of this run", account_id)
                        except Exception:
                            log.warning("send_welcome restyle failed account=%s fan=%s — "
                                        "sending verbatim line", account_id, fan_id,
                                        exc_info=True)
                if time_only and len(bubbles) > 1:
                    bubbles[1] = _lead_with_its(bubbles[1])

            # Bubble 3: the operator's question, word-for-word, after BOTH the
            # pinned and the composed paths — a pin replaces the activity line,
            # never the question.
            if question:
                bubbles.append(question)

            # Last-mile per bubble: double the first vowel of any OF-restricted word
            # (V1 ran apply_word_restriction on EVERY welcome). Covers the local
            # template, the restyled line and the LLM riff.
            bubbles = [apply_word_restriction(b) for b in bubbles]
            if strip_emoji_on:
                bubbles = [strip_emojis(b) for b in bubbles]
            bubbles = [b for b in bubbles if b.strip()]
            if not bubbles:
                errors += 1
                continue

            if dry_run:
                sent += 1  # would-send; do NOT mark welcome_sent on a dry run
                continue

            # The burst: image on bubble 1 only; each bubble held for its human
            # typing time first (live "...is typing" frames when enabled) — same
            # pacing as welcome_chatter_for_info, so the welcome lands like a person texting,
            # not a bot blast.
            media_ids = [bot_media_id] if bot_media_id is not None else []

            # The burst as ONE plan: every text bubble, then the GIF. Bubble 4 is
            # a (text="", giphy_id=…) send — OF carries a GIF as a top-level
            # `giphyId` beside EMPTY text — which is why it joins HERE and not
            # `bubbles`: the composition above would have word-restricted it,
            # emoji-stripped it, then dropped it as blank.
            #
            # Riding the same loop is what keeps ONE burst policy for four bubbles:
            # bubble 0 failing is fatal, any later failure stops the burst, and
            # every landed send is persisted the same way. Sending the GIF after
            # the loop instead fired it even when the burst had already been
            # abandoned — a fan whose question bubble failed got a lone greeting
            # and then an unexplained animation.
            #
            # `bubbles` is non-empty here (guarded above), so the GIF is never
            # index 0: a GIF cannot ship as a welcome on its own.
            plan: list[tuple[str, str | None]] = [(b, None) for b in bubbles]
            if gif_id:
                plan.append(("", gif_id))

            landed = 0
            for idx, (part, gid) in enumerate(plan):
                # A GIF is PICKED, not typed: no typing time and no "...is typing"
                # frame, just the beat between bubbles.
                await hold_with_typing(
                    account_id, fan_id,
                    _GIF_HOLD_S if gid else typing_delay_seconds(part, typing_wpm),
                    typing_indicator=typing_on and not gid)
                try:
                    if gid:
                        result = await asyncio.to_thread(
                            lambda g=gid: client.send_message(fan_id, "", giphy_id=g))
                    else:
                        # A refused ATTACHMENT degrades to text-only rather than
                        # losing the greeting. Otherwise one dead vault id blocks
                        # EVERY welcome on the account: a failed bubble-0 leaves the
                        # welcome_sent claim unwritten, so the next sweep regenerates
                        # and re-fails, forever.
                        outcome = await send_dropping_bad_media(
                            client, fan_id, part, media_ids if idx == 0 else [],
                            log=log, send_purpose="gated")
                        result = outcome.result
                except Exception as e:
                    if idx == 0:
                        errors += 1
                        # Permanent (deleted/blocked) → quarantine AND claim
                        # welcome_sent — that claim is THIS sender's own gate, so the
                        # new-sub sweep never regenerates a welcome for a fan we can't
                        # deliver to. Transient errors leave the claim unwritten and
                        # retry next tick.
                        if await skip_unreachable_fan(account_id, fan_id, e, log=log):
                            await _mark_welcomed(account_id, fan_id, sub.get("username"))
                        log.warning("send_welcome send failed account=%s fan=%s",
                                    account_id, fan_id, exc_info=True)
                    else:
                        # The greeting already landed → the fan IS welcomed; losing
                        # the follow-up bubble is cosmetic. Stop the burst, still mark.
                        log.warning("send_welcome bubble %d failed account=%s fan=%s",
                                    idx + 1, account_id, fan_id, exc_info=True)
                    break
                landed += 1
                # A landed send is either the GIF or a text bubble, and only a text
                # bubble can carry the time-of-day image — so `outcome` is read only
                # in the branch that defines it.
                if gid:
                    gifs_sent += 1
                elif idx == 0 and outcome.media_landed:
                    image_attached += 1
                # Existing optimistic send path: persist each landed bubble
                # (Automation employee). The WS pump skips outbound, so this is the
                # only producer of the outbound `messages` rows.
                msg_id = result.get("id") if isinstance(result, dict) else None
                if msg_id:
                    await write_outbound_attribution(
                        account_id=account_id,
                        fan_id=int(fan_id),
                        message_id=int(msg_id),
                        sent_by_employee_id=None,  # → system Automation employee
                        automation_kind="welcome",  # matches grok_calls.purpose
                        # `part` is "" for the GIF bubble, which is exactly the
                        # body a GIF-only row wants: its content is the giphyId.
                        body=str(result.get("text") or part),
                        price_cents=0,
                        created_at=ax._parse_iso(result.get("createdAt")) or datetime.utcnow(),
                        emit_live=True,  # WORKER→SSE bridge: surface the welcome live
                    )
            if landed == 0:
                continue
            # Mark welcome_sent AFTER at least one bubble landed — a crash
            # mid-welcome re-welcomes (rare, safe) rather than marking with no send.
            await _mark_welcomed(account_id, fan_id, sub.get("username"))
            sent += 1
            sent_ok = True
        finally:
            # W3: rest the fan, then hand the lease straight back — do NOT sit on
            # it. Holding it meant the 15-minute lease TTL, not the rest we chose,
            # decided when he could be answered.
            #
            # ORDER IS LOAD-BEARING: the cooldown must be in force BEFORE the lease
            # drops. A tick landing in the gap would see a fan with neither brake
            # and could reply on top of the welcome — the exact double-message the
            # lease exists to stop. And if the cooldown write fails we KEEP the
            # lease, so a fan is never left with no brake at all; it expires on its
            # own and we are back to the old, slower-but-safe behaviour.
            rested = False
            if sent_ok:
                try:
                    await ax.start_fan_cooldown(account_id, fan_id,
                                                cooldown_s=_WELCOME_REST_S)
                    rested = True
                except Exception:
                    log.warning("send_welcome cooldown set failed account=%s fan=%s "
                                "— keeping the lease as the fallback brake",
                                account_id, fan_id, exc_info=True)
            if rested or not sent_ok:
                await ax.release_fan_lease(account_id, fan_id)

    return {
        "subscribers_seen": len(subs),
        "new_subscribers": new_total,
        "welcomes_sent": sent,
        "image_attached": image_attached,
        "restyled": restyled,
        "restyled_cached": restyled_cached,
        "pinned_used": pinned_used,
        "gifs_sent": gifs_sent,
        "skipped_locked": skipped_locked,
        "skipped_cooldown": skipped_cooldown,
        "skipped_existing": skipped_existing,
        "skipped_guard": skipped_guard,
        "skipped_restricted": skipped_restricted,
        "skipped_audience": skipped_audience,
        "errors": errors,
        "cap_hit": cap_hit,
        "batch_capped": batch_capped,
        "dry_run": dry_run,
    }
