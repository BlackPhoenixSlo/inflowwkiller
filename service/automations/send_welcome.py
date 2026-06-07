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
  • llm_client.chat() generates the welcome — that call writes the `grok_calls`
    audit row AND enforces the per-account daily cost cap atomically, so we don't
    re-implement either; persona / welcome_rules / time-of-day come from
    `account_ai_config`.
  • the existing optimistic send path: `of_client.send_message` →
    `attribution.write_outbound_attribution` (credits the system Automation
    employee, since no X-Employee-Id exists for a background run). The WS pump
    skips outbound, so this is the only producer of the outbound `messages` row.
  • dedup via `welcome_sent` (account_id, fan_id) — persisted, so a restart
    re-polls, sees the fan already welcomed, and skips: a sub yields EXACTLY one
    welcome. `welcome_sent` is written only AFTER a confirmed 200 send, so a send
    failure never marks a fan welcomed-without-a-welcome.
  • per-(account, fan) send-lease so A05/A06/A07/A11 can't double-message the
    same fan in an overlapping cycle; run_once's per-(account, kind) lock stops
    a slow tick stacking on the next.

Payload knobs (all optional): `limit` (notifications fetched), `max_welcomes`
(per-run batch cap), `model` (LLM override), `dry_run` (generate but don't send),
`with_image` (attach a time-of-day bot-folder vault image, default True — same
picker as send_followup), `test_fan` (+ `test_name`) to force one hardcoded
recipient.

Returns a stats dict → automation_runs.stats_json.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

import automation_executor as ax  # _make_client / _parse_iso / fan-lease seams
import llm_client                  # call .chat at runtime so tests can patch it
from attribution import write_outbound_attribution
from automation_registry import register
from ._common import apply_word_restriction, resolve_model
from db.engine import get_session
from db.models import AccountAiConfig, Fan, FanProfile, WelcomeSent
from llm_client import LLMCapExceeded

log = logging.getLogger("of-relay.automation.send_welcome")

# Default LLM model when account_ai_config.model is NULL (llm_client comment:
# NULL → grok-4-1-fast-non-reasoning, preserving current behavior).
_DEFAULT_MODEL = "grok-4-1-fast-non-reasoning"
_DEFAULT_NOTIF_LIMIT = 30      # how many subscribe-notifications to pull per tick
_DEFAULT_MAX_WELCOMES = 25     # batch cap per run (logged when it bites)
_WELCOME_TEMPERATURE = 0.85    # matches the spec's Grok call

# First-letter → alliterative adjective for the real-name stutter trick (V1
# _fan_name_hint / generate_welcome_local). e.g. S → "Sexy Sofie".
_ADJS = {
    "A": "Adoring", "B": "Brave", "C": "Cute", "D": "Dreamy", "E": "Epic", "F": "Flirty",
    "G": "Gorgeous", "H": "Handsome", "I": "Incredible", "J": "Juicy", "K": "Kind", "L": "Lovely",
    "M": "Mighty", "N": "Naughty", "O": "Original", "P": "Playful", "Q": "Quick", "R": "Radiant",
    "S": "Sexy", "T": "Tasty", "U": "Unique", "V": "Vibrant", "W": "Wild", "X": "Xtra",
    "Y": "Yummy", "Z": "Zesty",
}


def _stutter_nick(first_word: str) -> tuple[str, str]:
    """('S-S-S-Sofie', 'Sexy Sofie') — V1 alliteration trick for a real name."""
    L = first_word[0].upper() if first_word else "B"
    return (f"{L}-{L}-{L}-{first_word}", f"{_ADJS.get(L, 'Flirty')} {first_word}")


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


# ── Prompt-shaping helpers (ports of the spec's pure helpers) ─────────

def _fan_name_hint(display_name: str | None, username: str | None) -> tuple[str, str]:
    """Classify the fan's name and return (type, instruction) for the prompt."""
    target = (display_name or username or "").strip()
    if not target:
        return ("username", "Riff playfully on them being mysterious with no name yet.")
    if re.fullmatch(r"u?\d+", target):
        digits = re.sub(r"\D", "", target)
        return ("number",
                f"Make a playful joke about them being 'fan #{digits}' since they "
                f"have no real name set yet.")
    words = target.split()
    if (1 <= len(words) <= 3 and not any(c.isdigit() for c in target)
            and words[0][:1].isupper()):
        first = words[0]
        stutter, nickname = _stutter_nick(first)
        return ("real_name",
                f"Open with an excited stutter prefix on their first letter that runs "
                f"straight into a flirty alliterative nickname (a flirty adjective "
                f"starting with the same letter as their name) and an excited sign-off, "
                f"like 'Hey {stutter[:6]}{nickname} ! !!'.")
    return ("username", f"Riff playfully on their handle '{target}'.")


def _model_hour(utc_offset: int) -> int:
    """Current hour in the model's timezone (utcnow + offset hours)."""
    try:
        off = int(utc_offset)
    except (TypeError, ValueError):
        off = 0
    return int((datetime.utcnow().hour + off) % 24)


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


def _compose_system(cfg: dict, name_instr: str, tod: str, activity: str) -> str:
    persona = (cfg.get("persona")
               or "You are a warm, flirty OnlyFans creator welcoming a brand-new "
                  "subscriber to your page.").strip()
    parts = [persona]
    rules = (cfg.get("welcome_rules") or "").strip()
    if rules:
        parts.append("Welcome rules:\n" + rules)
    if name_instr:
        parts.append("Name handling: " + name_instr)
    if activity:
        parts.append(f"It is {tod} where you are and you are {activity}. "
                     f"You may reference this naturally.")
    parts.append(
        "Write ONE short, personal welcome DM (1–3 sentences, casual texting "
        "tone). Do not use the word 'welcome' more than once. Output only the "
        "message text — no surrounding quotes, no preamble."
    )
    return "\n\n".join(parts)


def _compose_user(name: str, tod: str) -> str:
    who = (f"The new subscriber's name/handle is: {name}." if name
           else "The new subscriber has not set a name.")
    return f"{who} Local time of day: {tod}."


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
        return {
            "persona": cfg.persona,
            "welcome_rules": cfg.welcome_rules,
            "utc_offset": cfg.utc_offset,
            "location": cfg.location,
            "time_activities": acts,
            "time_images": imgs,
            "model": cfg.model,
        }




async def _load_welcomed(account_id: str) -> set[int]:
    async with get_session() as s:
        rows = (await s.execute(
            select(WelcomeSent.fan_id).where(WelcomeSent.account_id == str(account_id))
        )).all()
    return {int(r[0]) for r in rows}


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
        off = int(utc_offset)
    except (TypeError, ValueError):
        off = 0
    return (datetime.utcnow() + timedelta(hours=off)).strftime("%A")


def _name_token(s: str | None, *, last: bool = False) -> str:
    """Pull a real-NAME token (Capitalized, letters only, len≥2) out of a string,
    or '' if it doesn't look like a name (handles like 'xx_gamer_99' / 'u123' are
    rejected — they aren't Capitalized real names). For a stored nickname pass
    last=True so 'Sexy Sofie' → 'Sofie' (the name, not the adjective)."""
    if not s:
        return ""
    seg = str(s).split("/")[0].strip()       # 'Ava/Free' → 'Ava'
    words = re.split(r"\s+", seg) if seg else []
    if not words:
        return ""
    raw = words[-1] if last else words[0]
    if not raw[:1].isupper():                 # real names are Capitalized; handles aren't
        return ""
    w = re.sub(r"[^A-Za-z]", "", raw)
    return w if len(w) >= 2 else ""


async def _resolve_welcome_name(account_id: str, fan_id: int, sub: dict) -> str:
    """Best real first name to greet by, generated from whatever we have — the guy's
    real name, the OF display name, or a stored nickname (W4: "generate the nickname
    from info we have"). Returns '' when all we have is a random handle / number, so
    the caller falls back to the LLM riff. Brand-new subs usually have no Fan row →
    we just use the notification's display name."""
    real_name = gen_nick = cust_nick = disp = prof = None
    async with get_session() as s:
        prof = (await s.execute(select(FanProfile.nickname).where(
            FanProfile.account_id == str(account_id),
            FanProfile.fan_id == int(fan_id)))).scalar_one_or_none()
        fan = (await s.execute(select(
            Fan.real_name, Fan.generated_nickname, Fan.custom_nickname, Fan.of_display_name
        ).where(Fan.account_id == str(account_id), Fan.fan_id == int(fan_id)))).first()
    if fan:
        real_name, gen_nick, cust_nick, disp = fan
    for tok in (
        _name_token(real_name),            # the guy's confirmed real name (gen_info)
        _name_token(sub.get("name")),      # OF display name from the subscribe notif
        _name_token(disp),                 # stored OF display name
        _name_token(prof, last=True),      # AI nickname — drop the adjective, keep name
        _name_token(gen_nick, last=True),
        _name_token(cust_nick, last=True),
    ):
        if tok:
            return tok
    return ""


def _local_welcome(name: str, cfg: dict) -> str:
    """V1 'precious' deterministic welcome (NO LLM call) — the signature charm:
    a stutter prefix on the first letter that runs straight into the alliterative
    nickname (adjective + name), an excited sign-off, then the verbatim time-of-day
    activity tagged with the creator's local weekday / time-of-day / location, then a
    busy sign-off:

        Hey S-S-S-Sexy Sofie ! !!

        By the way - just woke up and made myself a coffee ☕ my dogs are going crazy
        wanting a walk lol (it's Friday morning in Los Angeles, California where I am from)

        Will reply when I am back :)

    The stutter prefix ('S-S-S-') sits on the first letter and leads into the full
    alliterative nickname 'Sexy Sofie'. The image is attached separately by the caller."""
    L = name[0].upper()
    adj = _ADJS.get(L, "Flirty")
    stutter = f"{L}-{L}-{L}-{adj} {name}"
    lines = [f"Hey {stutter} ! !!"]

    hour = _model_hour(cfg.get("utc_offset") or 0)
    tod, activity = _time_activity(hour, cfg.get("time_activities") or {})
    location = (cfg.get("location") or "").strip()
    if activity:
        where = f" in {location}" if location else ""
        lines += ["", f"By the way - {activity} (it's {_model_weekday(cfg.get('utc_offset'))} "
                  f"{tod}{where} where I am from)"]
    lines += ["", "Will reply when I am back :)"]
    return "\n".join(lines).strip()


async def _generate_welcome(
    account_id: str, fan_id: int, sub: dict, cfg: dict, model: str
) -> str:
    """One Grok/LLM call → welcome text. Raises LLMCapExceeded when the daily
    cap is hit (caller stops the run); any other failure raises to the caller."""
    name = (sub.get("name") or sub.get("username") or "").strip()
    _, name_instr = _fan_name_hint(sub.get("name"), sub.get("username"))
    hour = _model_hour(cfg.get("utc_offset") or 0)
    tod, activity = _time_activity(hour, cfg.get("time_activities") or {})
    res = await llm_client.chat(
        model=model,
        messages=[
            {"role": "system", "content": _compose_system(cfg, name_instr, tod, activity)},
            {"role": "user", "content": _compose_user(name, tod)},
        ],
        purpose="welcome",
        account_id=account_id,
        fan_id=fan_id,
        temperature=_WELCOME_TEMPERATURE,
    )
    return (res.content or "").strip()


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
    model: str | None = None,
) -> dict:
    """Compose the welcome a real run WOULD produce for one fan — the text + the
    chosen time-of-day image id — WITHOUT sending and WITHOUT any state write.
    Powers the Brain panel's "Preview" button (mirrors nudge_online.preview_compose).

    Name→text resolution mirrors run(): a resolvable real name takes the
    deterministic 'precious' local template (no LLM, zero cost); only a random
    handle / number with no usable name falls back to a single LLM riff. When no
    `fan_id` is given we greet a representative name so the preview is deterministic
    and free. The image is the account's configured per-slot vault id for the
    current hour (`time_images`), or None — no vault network call here."""
    cfg = await _load_ai_config(account_id)
    hour = _model_hour(cfg.get("utc_offset"))

    if fan_id is not None:
        sub = {"id": int(fan_id), "name": test_name, "username": None}
        name = await _resolve_welcome_name(account_id, int(fan_id), sub)
    else:
        # No fan chosen → a representative resolvable name so the preview shows the
        # signature local template deterministically (no LLM call, no cost).
        name = _name_token(test_name) or "Alex"
        sub = {"id": 0, "name": test_name or name, "username": None}

    if name:
        text = _local_welcome(name, cfg)
    else:
        model = model or await resolve_model(account_id, "welcome", None)
        text = await _generate_welcome(account_id, int(fan_id or 0), sub, cfg, model)

    text = apply_word_restriction(text)
    return {"text": text, "image": _slot_image_id(cfg, hour),
            "name": name, "slot": _slot_key(hour)}


# ── The automation ───────────────────────────────────────────────────

@register("send_welcome")
async def run(account_id: str, payload: dict, *, run_id: int) -> dict:
    payload = payload or {}
    dry_run = bool(payload.get("dry_run"))
    with_image = payload.get("with_image", True)
    limit = int(payload.get("limit") or _DEFAULT_NOTIF_LIMIT)
    max_welcomes = int(payload.get("max_welcomes") or _DEFAULT_MAX_WELCOMES)

    cfg = await _load_ai_config(account_id)
    model = await resolve_model(account_id, "welcome", payload.get("model"))

    client = await asyncio.to_thread(ax._make_client, account_id)

    # Time-of-day vault image. Deterministic per hour, identical for every fan this
    # tick, so resolve ONCE — not per fan. Skipped on dry runs. Prefer the account's
    # configured per-slot image id (time_images); fall back to the legacy folder
    # picker when no slot id is set.
    hour = _model_hour(cfg.get("utc_offset"))
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
        # OF now 400s on ANY `type=` filter on /users/notifications (verified
        # live 2026-06: type=subscribes/all/subscriptions all 400; untyped works).
        # Fetch the whole feed and filter to subscribe events client-side in
        # _extract_new_subscribers.
        resp = await asyncio.to_thread(
            client.notifications, limit=limit, offset=0,
        )
        subs = _extract_new_subscribers(resp)

    # Dedup against welcome_sent (survives restarts → exactly one welcome).
    welcomed = await _load_welcomed(account_id)
    new_subs = [s for s in subs if s["id"] not in welcomed]
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
                # If we can resolve a real name (from the notif, the fan's real_name,
                # or a stored nickname) → deterministic V1 'precious' template, ALWAYS
                # prefix + stutter, no LLM, zero cost. Only a random handle / number
                # (no usable name) falls back to the LLM riff.
                name = await _resolve_welcome_name(account_id, fan_id, sub)
                if name:
                    text = _local_welcome(name, cfg)
                else:
                    text = await _generate_welcome(account_id, fan_id, sub, cfg, model)
            except LLMCapExceeded:
                cap_hit = True
                log.warning("send_welcome daily LLM cap reached account=%s — stopping run",
                            account_id)
                break
            except Exception:
                errors += 1
                log.warning("send_welcome generate failed account=%s fan=%s",
                            account_id, fan_id, exc_info=True)
                continue

            if not text:
                errors += 1
                continue

            # Last-mile: double the first vowel of any OF-restricted word (V1 ran
            # apply_word_restriction on EVERY welcome). Covers both the local
            # template and the LLM riff.
            text = apply_word_restriction(text)

            if dry_run:
                sent += 1  # would-send; do NOT mark welcome_sent on a dry run
                continue

            media_files = [bot_media_id] if bot_media_id is not None else []
            try:
                result = await asyncio.to_thread(
                    lambda: client.send_message(fan_id, text, media_files=media_files)
                )
            except Exception:
                errors += 1
                log.warning("send_welcome send failed account=%s fan=%s",
                            account_id, fan_id, exc_info=True)
                continue

            if media_files:
                image_attached += 1

            # Existing optimistic send path: persist the outbound row (Automation
            # employee), then mark welcome_sent — order matters so a crash before
            # marking re-welcomes (rare, safe) rather than marking with no send.
            msg_id = result.get("id") if isinstance(result, dict) else None
            if msg_id:
                await write_outbound_attribution(
                    account_id=account_id,
                    fan_id=int(fan_id),
                    message_id=int(msg_id),
                    sent_by_employee_id=None,  # → system Automation employee
                    body=str(result.get("text") or text),
                    price_cents=0,
                    created_at=ax._parse_iso(result.get("createdAt")) or datetime.utcnow(),
                    emit_live=True,  # WORKER→SSE bridge: surface the welcome live
                )
            await _mark_welcomed(account_id, fan_id, sub.get("username"))
            sent += 1
            sent_ok = True
        finally:
            # W3: keep the lease (let it expire) on a confirmed send + rest the
            # fan; release it only when we did NOT send so a retry can run sooner.
            if sent_ok:
                try:
                    await ax.start_fan_cooldown(account_id, fan_id)
                except Exception:
                    log.warning("send_welcome cooldown set failed account=%s fan=%s",
                                account_id, fan_id, exc_info=True)
            else:
                await ax.release_fan_lease(account_id, fan_id)

    return {
        "subscribers_seen": len(subs),
        "new_subscribers": new_total,
        "welcomes_sent": sent,
        "image_attached": image_attached,
        "skipped_locked": skipped_locked,
        "skipped_cooldown": skipped_cooldown,
        "errors": errors,
        "cap_hit": cap_hit,
        "batch_capped": batch_capped,
        "dry_run": dry_run,
    }
