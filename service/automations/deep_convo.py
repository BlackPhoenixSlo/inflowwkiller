"""service/automations/deep_convo.py — Automation A06: deep_convo.

Spec: library/one_section_of_automations/06_deep_convo.md — the DOM world it
describes (`.tiptap.ProseMirror`, `page.goto(.../chat/{cid})`) is GONE. The same
brain runs over OUR DB (the WS pump + scrape_chats already filled
`messages`/`fans`/`fan_profiles`) and sends through the existing optimistic send
path. Per the network-rewrite mapping at the bottom of the spec:

    sidebar / open chat / load history → DB scan of `messages` per fan
    "wait for reply" oracle            → last `messages` row direction per fan
    send via ProseMirror               → of_client.send_message → write_outbound_attribution

A fixed 4-message engagement drill on fans whose profile is already ≥ 75% complete
AND who have at least one Q + one Tease in their `fan_profiles` row:

    send the Q  →  wait for fan reply  →  Grok reply  →  wait for fan reply
                →  Grok reply  →  send the Tease  →  state = "done"

State lives on the `fans.deep_convo_*` columns (NO separate table):

    deep_convo_state     "missing"/"not_started" → "q_sent" → "chatted_1"
                         → "chatted_2" → "done"
    deep_convo_skip_level / deep_convo_skip_remaining   exponential backoff on the
                         "waiting for fan reply" states so we don't reopen the same
                         chat every tick: 0 → 1 → 2 → 4 → 8 → 16 → 32 → 64 → 128
                         cycles to skip, capped at level 8.
    deep_convo_q_text / deep_convo_tease_text   the Q we sent + the Tease we will send.

Once a fan is `deep_convo_state == "done"`, of_ai_chat / process_old_fans know
not to revisit.

It mirrors of_ai_chat (A05) + send_followup (A07):
  • of_client ONLY (no DOM), built via the executor's `_make_client` seam so tests
    inject a fake with no network. Reads come from OUR DB.
  • its OWN AsyncSession per read/write (`get_session()`); never one shared across
    branches (the SQLAlchemy parallel-session footgun).
  • llm_client.chat() generates each in-between reply (the Q + Tease are sent
    verbatim from the profile) — that call writes the `grok_calls` audit row AND
    enforces the per-account daily cost cap atomically.
  • the existing optimistic send path: `of_client.send_message` →
    `attribution.write_outbound_attribution(emit_live=True)`.
  • per-(account, fan) send-lease so A05/A07/A09/A11 can't double-message the same
    fan in an overlapping cycle. ONE state transition per fan per tick.

THE NO-ID SEND CONTRACT (automations/_common.py): `deep_convo_state` IS the durable
guard, so we advance it after EVERY successful 200 — even one without a message
`id` (which can't be persisted, since message_id is the PK). The next tick reads
the advanced state and does not re-send. The message row is simply unrecorded
until scrape_chats backfills it. State is also checkpointed BETWEEN the chatted_1
reply and the Tease (transient `chatted_2`) so a crash mid-drill recovers by just
sending the Tease — never the reply twice.

Scheduling: self-registers via `@register("deep_convo")` on import (the executor
auto-imports `service/automations/*`). NOT part of the default cadence — invoke
manually (enqueue a `scheduled_jobs` row) or insert an `automation_rules` row with
`kind="deep_convo"` and `trigger_json={"every_seconds": N}`. NO edit to
automation_executor.py.

Payload knobs (all optional): `limit` (candidate sweep ceiling), `max_sends`
(per-run send-transition cap), `max_spend_cents` (skip fans above this lifetime
spend; default $200), `model` (LLM override), `dry_run` (generate but don't send /
advance), `force_ids` ([fan_id, …] bypass the info-complete + spend + backoff gates
— manual targeting; still needs a Q + Tease and is never re-drilled once done).

Returns a stats dict → automation_runs.stats_json.
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

import automation_executor as ax  # _make_client / _parse_iso / fan-lease seams
import llm_client                  # call .chat at runtime so tests can patch it
from ._persona import compose_persona
from . import _language
from . import _pins  # his own pinned long-form message (reader only)
from attribution import write_outbound_attribution
from automation_registry import register
from db.engine import get_session
from db.models import AccountAiConfig, Blacklist, Fan, FanProfile, Message, SkipList
from llm_client import LLMCapExceeded
from . import _voice
from ._common import (
    nonempty,
    BIO_CONSISTENCY_GUARDRAIL,
    ONPLATFORM_GUARDRAIL,
    NONNATIVE_OUTPUTS, NONNATIVE_REGISTER, apply_nonnative_spacing, apply_nonnative_style,
    apply_word_restriction, build_facts_note,
    build_structured_nickname, casualize_qtease, coerce_ids, facts_from_fan,
    hold_with_typing, apply_typo_throttle, load_nonnative_flags,
    load_painful_texting_flag, load_style_flags, load_voice_blocks,
    load_typing_indicator, load_typing_wpm, load_typo_flags, push_nick_and_notes,
    quarantine_if_undeliverable, resolve_fan_name, resolve_model,
    should_skip_muted_creator, skip_unreachable_fan, typing_delay_seconds,
)
from ._outbound import finalize_draft
from .of_ai_chat import _clock_line, _load_clock_tz  # the prompt clock

log = logging.getLogger("of-relay.automation.deep_convo")

# ── Knobs (ported from 06_deep_convo.md) ─────────────────────────────
_PURPOSE = "deep_convo"           # also the account_ai_config.model_by_purpose key
_INFO_COMPLETE_RATIO = 0.75       # ≥ 3 of 4 bio groups filled → profile complete
_MAX_SPEND_CENTS = 200_00         # skip fans whose lifetime spend exceeds $200
_DEFAULT_LIMIT = 200              # candidate sweep ceiling
_DEFAULT_MAX_SENDS = 25           # per-run send-transition cap
_REPLY_TEMPERATURE = 0.85         # warm/varied, matches the legacy persona call
_HISTORY_TAIL = 40                # last N messages handed to the model
_MSG_CLIP = 400                   # clip each message body
_REPLY_MAX_CHARS = 600            # trim a runaway generation before sending
# deep_convo is a live back-and-forth (Q → reply → tease), so like of_ai_chat the
# generic 30-min W3 rest would freeze it. Use a short ~1-2 min cooldown AND
# RELEASE the lease on success (see finally) — keeping the 900s lease would block
# the fan's own next step (a fan reply 2 min after our Q) for the full 15 min.
_REPLY_COOLDOWN_S = 15            # short rest between our turns to the same fan
# chatted_1 double-texts (reply then tease) — sleep a human-ish typing pause
# between them so they don't post in the same instant (like reply_mass_funnel).
# Jittered ±_STEP_GAP_JITTER; tests set _STEP_GAP_S = 0 to skip.
_STEP_GAP_S = 4.0
_STEP_GAP_JITTER = 0.5           # ±50% → ~2-6s actual


def _jittered_gap() -> float:
    """A human-ish typing pause around _STEP_GAP_S (±_STEP_GAP_JITTER)."""
    return _STEP_GAP_S * (1.0 + random.uniform(-_STEP_GAP_JITTER, _STEP_GAP_JITTER))

# Backoff schedule (cycles to skip), indexed by skip_level, capped at level 8.
_BACKOFF = [0, 1, 2, 4, 8, 16, 32, 64, 128]
_MAX_LEVEL = len(_BACKOFF) - 1

# State constants.
_START_STATES = {"", "missing", "not_started", None}
_S_Q_SENT = "q_sent"
_S_CHATTED_1 = "chatted_1"
_S_CHATTED_2 = "chatted_2"
_S_DONE = "done"
_WAITING_STATES = {_S_Q_SENT, _S_CHATTED_1}  # mid-drill states that gate on a fan reply
# The fan-reply gate ALSO covers a not-yet-started drill: the FIRST Q must land
# AFTER the fan speaks (a reply to our last chat message), never as an unprompted
# double-text on top of of_ai_chat's reply. So "not_started" gates on the reply
# oracle too — deep_convo only ever acts when the fan spoke last.
_REPLY_GATED_STATES = _WAITING_STATES | {"not_started"}

_HTML_OPEN = "<"
_TAG_RE = re.compile(r"<[^>]+>")

# deep_convo is emoji-FREE (user mandate, ALWAYS — independent of the account-wide
# strip_emojis toggle). The model ignores a "no emojis" prompt, so we strip them in
# code at the send chokepoint — `strip_emoji=True` where this module calls
# _outbound.finalize_draft, which is the one place any engine strips.


# A dash the model emits is BOTH a bot tell and a missed bubble break — a real
# texter hits send there. deep_convo sends single lines (no split_for_bubbles), so
# without this an em-dash reply ("got it jack — how old r u") went out as ONE
# bubble with the dash showing. Split on the dash family into separate sends; a
# residual em/en dash with nothing to split on softens to a comma.
_DC_DASH_RE = re.compile(r"\s*[—–]\s*|\s*--\s*|\s+-\s+")  # em/en dash, --, spaced hyphen
_DC_DASH_CLEAN_RE = re.compile(r"\s*[—–]\s*")            # any residual em/en → ", "


def _strip_dash(s: str) -> str:
    return _DC_DASH_CLEAN_RE.sub(", ", s).strip().rstrip(",").strip()


def _split_on_dash(text: str) -> list[str]:
    """Split a deep_convo line on the dash family (em/en/--/spaced hyphen) into
    separate bubbles, dash removed. No dash → the line with any stray em/en dash
    softened to a comma. Commas are LEFT ALONE here (the scripted Q/Tease are
    proper-case single lines that legitimately carry commas)."""
    parts = [p.strip() for p in _DC_DASH_RE.split(text) if p.strip()]
    return parts if len(parts) >= 2 else [_strip_dash(text)]


# ── Text helpers (local copies — house pattern) ──────────────────────

def _strip_html(s: str | None) -> str:
    if not s:
        return ""
    if _HTML_OPEN not in s:
        return s.strip()
    return _TAG_RE.sub("", s).strip()


def _first_nonempty(*vals: str | None) -> str:
    for v in vals:
        if nonempty(v):
            return _strip_html(v) if isinstance(v, str) and _HTML_OPEN in v else v.strip()
    return ""


def _is_info_complete(f: Fan) -> bool:
    """Port of the spec's `is_info_complete`: four bio field-groups, ≥ 75% filled
    (identical to of_ai_chat A05 so the two automations agree on "complete").

      1. age      (his_age)
      2. location (home_country OR home_city)
      3. hobbies  (hobbies)
      4. depth    (recent_events OR fetishes OR likes_boobs OR likes_ass)
    """
    groups = (
        nonempty(f.his_age),
        nonempty(f.home_country) or nonempty(f.home_city),
        nonempty(f.hobbies),
        (nonempty(f.recent_events) or nonempty(f.fetishes)
         or bool(f.likes_boobs) or bool(f.likes_ass)),
    )
    return (sum(1 for g in groups if g) / len(groups)) >= _INFO_COMPLETE_RATIO


async def _load_persona(account_id: str) -> str:
    """Her full identity preamble — prose, location, canon. `persona` feeds ONLY
    the two prompt builders here (_build_messages, _leadin_messages), so both get
    it without threading a param through _generate/_generate_leadin."""
    async with get_session() as s:
        cfg = await s.get(AccountAiConfig, account_id)
    return compose_persona(cfg, fallback=(
        "You are a warm, flirty OnlyFans creator chatting with one of your fans."))


# ── Loaders (own session each) ────────────────────────────────────────

async def _load_blacklist() -> set[int]:
    async with get_session() as s:
        rows = (await s.execute(select(Blacklist.fan_id))).all()
    return {int(r[0]) for r in rows}


async def _load_stop_skips(account_id: str) -> set[int]:
    """Skip-listed fans deep_convo must leave alone — every reason EXCEPT 'info'.
    'info' is of_ai_chat's handoff marker (its whole population graduates to
    deep_convo with that row), but 'unreachable' (deleted/blocked — the send 404s
    and burns an LLM call every tick), 'too_long', 'spent' and 'old_fan_pre_ai'
    all mean some sender decided the AI is done with this fan."""
    async with get_session() as s:
        rows = (await s.execute(
            select(SkipList.fan_id).where(SkipList.account_id == str(account_id),
                                          SkipList.reason != "info")
        )).all()
    return {int(r[0]) for r in rows}


async def _load_profiles(account_id: str) -> tuple[dict[int, tuple[str, str]], int]:
    """(fan_id → (q, tease) for profiles that carry BOTH a Q and a Tease,
    count of profiles dropped for missing a Q or a Tease)."""
    out: dict[int, tuple[str, str]] = {}
    dropped = 0
    async with get_session() as s:
        rows = (await s.execute(
            select(FanProfile.fan_id,
                   FanProfile.q1, FanProfile.q2, FanProfile.q3,
                   FanProfile.tease1, FanProfile.tease2, FanProfile.tease3)
            .where(FanProfile.account_id == str(account_id))
        )).all()
    for fid, q1, q2, q3, t1, t2, t3 in rows:
        q = _first_nonempty(q1, q2, q3)
        tease = _first_nonempty(t1, t2, t3)
        if q and tease:
            out[int(fid)] = (q, tease)
        else:
            dropped += 1
    return out, dropped


class _Candidate:
    __slots__ = ("fan_id", "last_dir", "messages")

    def __init__(self, fan_id: int):
        self.fan_id = fan_id
        self.last_dir = ""
        self.messages: list[tuple[str, str]] = []  # (direction, body) oldest→newest

    def fan_replied(self) -> bool:
        """The "wait for reply" oracle: True iff the fan spoke last. We persist
        our own sends, so after we send the last row is 'out' → not replied."""
        return self.last_dir == "in"


async def _gather_messages(account_id: str, fan_ids: set[int]) -> dict[int, _Candidate]:
    """One pass over the account's messages → per-fan last direction + trimmed
    history, restricted to the candidate fan_ids."""
    out: dict[int, _Candidate] = {}
    if not fan_ids:
        return out
    async with get_session() as s:
        rows = (await s.execute(
            select(Message.fan_id, Message.direction, Message.body)
            .where(Message.account_id == str(account_id),
                   Message.fan_id.in_(fan_ids),
                   Message.is_unsent.is_(False))
            .order_by(Message.fan_id, Message.created_at, Message.message_id)
        )).all()
    for fan_id, direction, body in rows:
        c = out.get(fan_id)
        if c is None:
            c = out[fan_id] = _Candidate(int(fan_id))
        c.messages.append((direction, _strip_html(body)[:_MSG_CLIP]))
        c.last_dir = direction
    return out


def _build_messages(persona: str, f: Fan, c: _Candidate,
                    style_on: bool = False, nonnative_on: bool = False,
                    painful_on: bool = True, lang: str = "en",
                    clock: str = "",
                    v: "_voice.VoiceBlocks" = _voice.HER) -> list[dict]:
    """Compose the (system, user) pair for an in-between reply during the drill
    (the Q + Tease themselves are sent verbatim from the profile — not generated)."""
    facts = []
    name = resolve_fan_name(f)
    if name:
        facts.append(f"name/nickname: {name.split('/')[0][:40]}")
    for label, val in (("age", f.his_age), ("city", f.home_city),
                       ("country", f.home_country), ("hobbies", f.hobbies),
                       ("occupation", f.occupation), ("fetishes", f.fetishes)):
        if nonempty(val):
            facts.append(f"{label}: {str(val).strip()[:80]}")
    facts_block = ("\n".join(f"- {x}" for x in facts)
                   if facts else "- (nothing on file yet)")

    history = c.messages[-_HISTORY_TAIL:]
    convo = "\n".join(
        f"{'FAN' if d == 'in' else 'YOU'}: {b}" for d, b in history if b
    )

    # The prompt clock ("" when the account has no tz configured → byte-equal
    # prompt) — same block as of_ai_chat. A model with no clock invents one.
    clock_block = (
        f"RIGHT NOW for you it is {clock}. If the time, your day, or what "
        "you're doing comes up, stay consistent with this clock — never claim "
        "a different time of day.\n\n" if clock else "")
    system = (
        f"{persona}\n"
        "You're deepening things with a fan you already know well — you're past the "
        "getting-to-know-you stage, so be more playful, flirty and a little naughty, "
        "building on what he just said. No need to say 'Hey!' or use his name at the "
        "start. Keep it short, 1 question max, max 2 verbs. Never sound like a bot — "
        f"you are 100% human, {v.human_self}. Don't offer pics or videos. Do NOT "
        "use any emojis in this reply.\n\n"
        f"{v.painful_texting + chr(10) + chr(10) if painful_on else ''}"
        f"{clock_block}"
        f"{ONPLATFORM_GUARDRAIL}\n\n"
        f"{v.live_proof}\n\n"
        f"{BIO_CONSISTENCY_GUARDRAIL}\n\n"
        f"{v.humanizer + chr(10) + chr(10) if style_on else v.emoji_vocab}"
        f"{NONNATIVE_REGISTER + chr(10) + chr(10) if nonnative_on else ''}"
        "IMPORTANT: your reply is ONLY the chat message text. Never include JSON, "
        "code blocks, curly braces, or any metadata. Just text him back."
        f"{_language.output_language_directive(lang)}"
    )
    # His own long-form message, pinned on the thread and read back here (_pins).
    user = (
        f"What you know about him:\n{facts_block}\n\n"
        f"{_pins.pins_block(f)}"
        f"Recent conversation (oldest→newest):\n{convo}\n\n"
        f"Don't apologize. Generate a brief, 1 verb max, casual {v.reply_register} "
        "that flirts and builds on his last message. Reply now."
    )
    return [{"role": "system", "content": system},
            {"role": "user", "content": user}]


# ── State persistence (UPSERT onto the fans row, own session) ─────────

async def _save_state(account_id: str, fan_id: int, now: datetime, **fields) -> None:
    """UPSERT the deep_convo_* columns. UPSERT (not UPDATE) for the house no-id
    contract: a candidate always has a Fan row (info-complete reads it), but an
    UPSERT is robust to a concurrent delete and matches the other senders."""
    values = {"account_id": str(account_id), "fan_id": int(fan_id),
              "deep_convo_updated_at": now, "updated_at": now, **fields}
    async with get_session() as s:
        await s.execute(
            sqlite_insert(Fan)
            .values(**values)
            .on_conflict_do_update(
                index_elements=["account_id", "fan_id"],
                set_={k: v for k, v in values.items()
                      if k not in ("account_id", "fan_id")},
            )
        )


# ── Send seam ─────────────────────────────────────────────────────────

async def _push_profile(client, account_id: str, fan_id: int, f: Fan) -> None:
    """Push the structured nickname + fact-sheet note to OF at deep-convo start.
    Pulls the gen_info short_bio (generated at handoff) so the note is the full
    bullet sheet. Best-effort — never breaks the drill."""
    try:
        async with get_session() as s:
            prof = await s.get(FanProfile, (str(account_id), int(fan_id)))
        short_bio = (prof.short_bio if prof else "") or ""
        await push_nick_and_notes(
            client, account_id, fan_id,
            nick=build_structured_nickname(f),
            # 200 pinned EXPLICITLY — this PUTs to OnlyFans; never inherit a widened default.
            notes=build_facts_note(facts_from_fan(f), short_bio=short_bio, max_len=200),
        )
    except Exception:
        log.debug("deep_convo profile push failed account=%s fan=%s",
                  account_id, fan_id, exc_info=True)


async def _send_one(client, account_id: str, fan_id: int, text: str,
                    typing_wpm: float, typing_indicator: bool) -> None:
    """Hold for the typing delay, send one bubble, persist via the optimistic
    path. Raises on a transport failure (caller decides whether to swallow)."""
    await hold_with_typing(
        account_id, int(fan_id),
        typing_delay_seconds(text, typing_wpm),
        typing_indicator=typing_indicator,
    )
    result = await asyncio.to_thread(client.send_message, fan_id, text)
    msg_id = result.get("id") if isinstance(result, dict) else None
    if msg_id:
        await write_outbound_attribution(
            account_id=account_id,
            fan_id=int(fan_id),
            message_id=int(msg_id),
            sent_by_employee_id=None,  # → system Automation employee
            automation_kind=_PURPOSE,  # deep_convo
            body=str(result.get("text") or text),
            price_cents=0,
            created_at=ax._parse_iso(result.get("createdAt")) or datetime.utcnow(),
            emit_live=True,  # WORKER→SSE bridge: surface the send live
        )
    else:
        # 200 but no id → unrecordable (message_id is the PK). deep_convo_state is
        # the durable guard, so the caller still advances it; scrape backfills the
        # row later. NEVER leave state such that the fan is re-messaged.
        # But first probe WHY: a fan whose sub lapsed / account is gone no-id's on
        # EVERY send — without this, deep_convo re-engages them every tick (the 513-
        # call loop). quarantine_if_undeliverable rests/skips the undeliverable
        # ones; a genuinely transient no-id just advances state as before.
        log.warning("deep_convo send returned no id account=%s fan=%s — "
                    "advancing state on the 200 (no-id contract)", account_id, fan_id)
        await quarantine_if_undeliverable(client, account_id, fan_id)


async def _send(client, account_id: str, fan_id: int, text: str,
                *, typing_wpm: float | None = None,
                typing_indicator: bool | None = None,
                typo: bool = False, nonnative: bool = False, protect=(),
                lang: str = "en", v: "_voice.VoiceBlocks" = _voice.HER) -> bool:
    """Send one message + persist it through the optimistic path. Returns True on
    a 200 (whether or not OF echoed an id). Raises on a transport failure so the
    caller can leave state un-advanced and retry next tick.

    typing_wpm / typing_indicator are loaded once per run by the caller and
    threaded in; they fall back to a per-call load when omitted. An LLM dash in the
    text is split into separate bubbles (a real texter hits send there) so it never
    goes out as one line with the dash showing. When `typo` is on, the message may
    pick up a realistic thumb-slip and an optional follow-up "*fix" bubble. The
    FIRST bubble is the real message (its failure raises, as before); every
    follow-up bubble — a dash-split half or the cosmetic "*fix" — is best-effort and
    never fails the send (re-raising would re-send the first bubble = a duplicate)."""
    # Deterministic floor under ONPLATFORM_GUARDRAIL — every deep_convo bubble
    # (lead-in, scripted Q, tease) funnels through here, so one guard covers them
    # all. Scan BEFORE word restriction (which would hide "meet" as "meeet").
    # The shared send chokepoint (_outbound). strip_emoji is UNCONDITIONALLY True
    # here, not read from the account flag: deep_convo is emoji-free by design
    # (covers Q, replies and tease alike). That divergence from the other engines
    # is deliberate and now declared rather than implicit. No PHASE 2 consistency
    # check — deep_convo does not implement it.
    text, _leak = await finalize_draft(
        text, account_id=account_id, fan_id=fan_id, purpose=_PURPOSE,
        strip_emoji=True, v=v)
    if nonnative:  # opt-in: deterministic non-native misspellings (BEFORE word restrict)
        text = apply_nonnative_spacing(
            apply_nonnative_style(text, protect=protect),
            random.Random(f"{fan_id}:{text}:q"))
    text = _language.apply_word_restriction(text, lang)  # last-mile OF-restricted word substitution (language-gated)
    if typing_wpm is None:
        typing_wpm = await load_typing_wpm(account_id)
    if typing_indicator is None:
        typing_indicator = await load_typing_indicator(account_id)

    # An LLM em-dash/--/spaced-hyphen is both a bot tell and a missed bubble break —
    # split it into separate sends (dash removed). Commas are left alone (the
    # scripted Q/Tease are proper-case single lines that legitimately carry commas).
    parts = _split_on_dash(text)
    if typo:
        # protect words non-native already mangled so the thumb-typo can't double-corrupt
        typo_protect = list(protect) + (list(NONNATIVE_OUTPUTS) if nonnative else [])
        parts = (await apply_typo_throttle(
            account_id, fan_id, parts, random.Random(f"{fan_id}:{text}"),
            protect=typo_protect, max_bubbles=len(parts) + 1)) or parts

    main, *extra = parts
    await _send_one(client, account_id, fan_id, main, typing_wpm, typing_indicator)
    for follow in extra:  # dash-split halves + the cosmetic "*fix" — best-effort
        try:
            await _send_one(client, account_id, fan_id, follow, typing_wpm, typing_indicator)
        except Exception:
            log.debug("deep_convo follow-up bubble send failed (best-effort)", exc_info=True)
    return True


# ── The automation ───────────────────────────────────────────────────

@register("deep_convo")
async def run(account_id: str, payload: dict, *, run_id: int) -> dict:
    payload = payload or {}
    dry_run = bool(payload.get("dry_run"))
    limit = int(payload.get("limit") or _DEFAULT_LIMIT)
    max_sends = int(payload.get("max_sends") or _DEFAULT_MAX_SENDS)
    max_spend_cents = int(payload.get("max_spend_cents") or _MAX_SPEND_CENTS)
    force_ids = coerce_ids(payload.get("force_ids"))
    # W7 fan-scope: drill ONLY the fan(s) who just messaged (no full sweep). Keeps
    # all gates — it only limits the candidate set.
    only_fan_ids = coerce_ids(payload.get("only_fan_ids"))

    model = await resolve_model(account_id, _PURPOSE, payload.get("model"))
    style_on = (await load_style_flags(account_id))[_PURPOSE]  # human-style opt-in
    account_lang = await _language.load_account_language(account_id)  # output language
    typo_on = (await load_typo_flags(account_id))[_PURPOSE]    # thumb-typo opt-in
    nonnative_on = (await load_nonnative_flags(account_id))[_PURPOSE]  # non-native opt-in
    painful_on = await load_painful_texting_flag(account_id)  # brevity/emotion framing (default ON)
    voice_blocks = await load_voice_blocks(account_id)   # ONE row, both axes
    typing_wpm = await load_typing_wpm(account_id)            # per-bubble pacing
    typing_indicator = await load_typing_indicator(account_id)  # live "...is typing"
    persona = await _load_persona(account_id)
    clock_tz = await _load_clock_tz(account_id)  # None ⇒ no clock line in the prompt
    blacklist = await _load_blacklist()
    stop_skips = await _load_stop_skips(account_id)  # skip_list, reason ≠ 'info'
    profiles, skipped_no_qtease = await _load_profiles(account_id)  # both present
    if only_fan_ids:
        profiles = {fid: v for fid, v in profiles.items() if fid in only_fan_ids}

    now = datetime.utcnow()
    skipped_listed = 0          # blacklist / paused
    skipped_not_complete = 0    # profile < 75% complete
    skipped_high_spend = 0      # lifetime spend > $200
    skipped_ai_chatter = 0      # fan owned by ai_chatter (under its spend gate)
    skipped_done = 0
    backed_off = 0              # waiting on a fan reply this tick

    # When ai_chatter is enabled it OWNS the fans it will actually answer — one bot
    # voice per fan, so deep_convo's scripted drill must not interleave THOSE. In
    # FULL-chatter mode that's every fan under its spend gate; in CLOSER mode
    # (intent_only) it answers only buyers and leaves pure chatter to us, so we must
    # yield ONLY its open-offer/intent fans, not blanket-skip every sub-gate fan.
    # `ai_engaged` resolves that per-fan; `ai_gate` keeps the spend bound.
    # Lazy import: ai_chatter imports of_ai_chat helpers, avoid a module cycle.
    try:
        from .ai_chatter import (gate_for as _ai_chatter_gate,
                                  engaged_subset as _ai_chatter_engaged)
        ai_gate = await _ai_chatter_gate(account_id)
        # Fail-safe below mirrors the OLD blanket-yield (everyone) on error, so a
        # transient failure never double-voices a fan.
        ai_engaged = await _ai_chatter_engaged(account_id, set(profiles))
    except Exception:
        ai_gate = None
        ai_engaged = set(profiles)

    # Fan rows carry the bio facts (info-complete gate + prompt) and the
    # deep_convo_* state. Only fans that HAVE a profile-Q+Tease can be candidates.
    async with get_session() as s:
        fan_rows = (await s.execute(
            select(Fan).where(Fan.account_id == str(account_id),
                              Fan.fan_id.in_(set(profiles)))
        )).scalars().all() if profiles else []
    fans: dict[int, Fan] = {int(f.fan_id): f for f in fan_rows}

    # Decide who acts THIS tick (and burn down backoff for the rest).
    actionable: list[tuple[int, str]] = []  # (fan_id, normalized_state)
    for fan_id, (_q, _tease) in profiles.items():
        forced = fan_id in force_ids
        if fan_id in blacklist or fan_id in stop_skips:
            skipped_listed += 1
            continue
        f = fans.get(fan_id)
        if f is None:
            # No Fan row → can't judge completeness or hold state. Skip unless forced.
            if not forced:
                skipped_not_complete += 1
                continue
        if f is not None and f.automation_paused_until and f.automation_paused_until > now:
            skipped_listed += 1
            continue
        # Muted creator we follow — HARD skip (stop_skips already blocks the durable
        # skip_list row; this also catches the pre-scrape window). Counts as listed.
        if should_skip_muted_creator(f):
            skipped_listed += 1
            continue

        # Don't deep-convo whales: a fan past $200 lifetime spend is already a
        # paying regular, so skip the engagement drill (force_ids bypasses).
        if not forced and f is not None and int(f.lifetime_spend_cents or 0) > max_spend_cents:
            skipped_high_spend += 1
            continue

        # ai_chatter owns the fans it will answer (full chatter: everyone sub-gate;
        # closer: only its open-offer/intent fans) — yield those (force_ids bypasses).
        # A sub-gate pure-chatter fan in closer mode is NOT in ai_engaged, so it
        # falls through to us — exactly the "leave pure chatter to Auto Convo" intent.
        if (not forced and ai_gate is not None
                and int((f.lifetime_spend_cents if f else 0) or 0) < ai_gate
                and fan_id in ai_engaged):
            skipped_ai_chatter += 1
            continue

        state = (f.deep_convo_state if f else None)
        if state == _S_DONE:
            skipped_done += 1
            continue
        if not forced and (f is None or not _is_info_complete(f)):
            skipped_not_complete += 1
            continue

        # Backoff: burn one cycle and skip if we're still waiting them out.
        if not forced and f is not None and int(f.deep_convo_skip_remaining or 0) > 0:
            await _save_state(account_id, fan_id, now,
                              deep_convo_skip_remaining=int(f.deep_convo_skip_remaining) - 1)
            backed_off += 1
            continue

        norm = "not_started" if (state in _START_STATES) else state
        actionable.append((fan_id, norm))

    # Need message history for the actionable fans (the reply oracle + prompt).
    by_fan = await _gather_messages(account_id, {fid for fid, _ in actionable})

    # Deterministic, bounded.
    actionable.sort(key=lambda x: x[0])
    actionable = actionable[:limit]

    sent = 0                    # send-transitions performed
    q_sent_n = 0
    advanced = 0                # reply-step advances (chatted_1 / chatted_2)
    completed = 0               # reached "done" this tick
    skipped_locked = 0
    skipped_cooldown = 0
    errors = 0
    cap_hit = False

    for fan_id, state in actionable:
        if sent >= max_sends:
            log.info("deep_convo batch capped account=%s cap=%d (rest next tick)",
                     account_id, max_sends)
            break

        c = by_fan.get(fan_id) or _Candidate(fan_id)
        f = fans.get(fan_id) or Fan(account_id=str(account_id), fan_id=fan_id)
        # Per-fan language: fans.language (manual pin or gen_info detection) overrides
        # the account default; unset → account default.
        fan_lang = _language.resolve_language(account_lang, getattr(f, "language", None))
        q, tease = profiles[fan_id]
        if style_on:  # casualize the proper-case scripted Q/Tease to match the voice
            q, tease = casualize_qtease(q), casualize_qtease(tease)
        typo_protect = [n for n in (f.real_name, f.generated_nickname,
                                    f.of_display_name) if n]  # never garble names

        # Reply-gated states (incl. the not-yet-started Q) only fire when the fan
        # spoke last — the Q/replies always come AFTER his message, never before.
        if state in _REPLY_GATED_STATES and not c.fan_replied():
            lvl = min(int(f.deep_convo_skip_level or 0) + 1, _MAX_LEVEL)
            await _save_state(account_id, fan_id, now,
                              deep_convo_skip_level=lvl,
                              deep_convo_skip_remaining=_BACKOFF[lvl])
            backed_off += 1
            continue

        # Another automation messaged this fan recently → rest it (W3 cooldown).
        if await ax.fan_on_cooldown(account_id, fan_id):
            skipped_cooldown += 1
            continue
        # One bot message per fan per cycle — don't race A05/A07/A09/A11.
        if not await ax.acquire_fan_lease(account_id, fan_id, "deep_convo"):
            skipped_locked += 1
            continue
        client = None
        sent_ok = False
        try:
            if not dry_run:
                client = await asyncio.to_thread(ax._make_client, account_id)

            # The fan replied (or we're starting / recovering) — reset backoff.
            reset = {"deep_convo_skip_level": 0, "deep_convo_skip_remaining": 0}

            if state == "not_started":
                if dry_run:
                    sent += 1
                    continue
                # Don't talk past him: if he asked something, answer it in one short
                # bubble first, THEN send the scripted Q (human-ish typing pause between).
                if _fan_asked(c):
                    lead = await _generate_leadin(model, persona, f, c, account_id, fan_id,
                                                  style_on=style_on, nonnative_on=nonnative_on,
                                                  lang=fan_lang, clock=_clock_line(clock_tz),
                                                  v=voice_blocks)
                    if lead:
                        await _send(client, account_id, fan_id, lead,
                                    typing_wpm=typing_wpm, typing_indicator=typing_indicator,
                            typo=typo_on, nonnative=nonnative_on, protect=typo_protect,
                            lang=fan_lang, v=voice_blocks)
                        if _STEP_GAP_S:
                            await hold_with_typing(account_id, fan_id, _jittered_gap(),
                                                   typing_indicator=typing_indicator)
                await _send(client, account_id, fan_id, q,
                            typing_wpm=typing_wpm, typing_indicator=typing_indicator,
                            typo=typo_on, nonnative=nonnative_on, protect=typo_protect, v=voice_blocks)
                sent_ok = True
                await _save_state(account_id, fan_id, now, deep_convo_state=_S_Q_SENT,
                                  deep_convo_q_text=q, deep_convo_tease_text=tease,
                                  **reset)
                sent += 1
                q_sent_n += 1
                # Start of deep convo: push the final nickname + fact-sheet NOTE to OF
                # (the bio/bullets gen_info generated at handoff land in the note here).
                await _push_profile(client, account_id, fan_id, f)

            elif state == _S_Q_SENT:
                reply = await _generate(model, persona, f, c, account_id, fan_id,
                                       style_on=style_on, nonnative_on=nonnative_on,
                                       painful_on=painful_on, lang=fan_lang,
                                       clock=_clock_line(clock_tz), v=voice_blocks)
                if reply is _CAP:
                    cap_hit = True
                    break
                if not reply:
                    errors += 1
                    continue
                if dry_run:
                    sent += 1
                    continue
                await _send(client, account_id, fan_id, reply,
                            typing_wpm=typing_wpm, typing_indicator=typing_indicator,
                            typo=typo_on, nonnative=nonnative_on, protect=typo_protect,
                            lang=fan_lang, v=voice_blocks)
                sent_ok = True
                await _save_state(account_id, fan_id, now,
                                  deep_convo_state=_S_CHATTED_1, **reset)
                sent += 1
                advanced += 1

            elif state == _S_CHATTED_1:
                reply = await _generate(model, persona, f, c, account_id, fan_id,
                                       style_on=style_on, nonnative_on=nonnative_on,
                                       painful_on=painful_on, lang=fan_lang,
                                       clock=_clock_line(clock_tz), v=voice_blocks)
                if reply is _CAP:
                    cap_hit = True
                    break
                if not reply:
                    errors += 1
                    continue
                if dry_run:
                    sent += 1
                    continue
                # Send the reply, checkpoint at the transient chatted_2 (so a crash
                # before the Tease recovers by sending ONLY the Tease), then Tease.
                await _send(client, account_id, fan_id, reply,
                            typing_wpm=typing_wpm, typing_indicator=typing_indicator,
                            typo=typo_on, nonnative=nonnative_on, protect=typo_protect,
                            lang=fan_lang, v=voice_blocks)
                sent_ok = True
                await _save_state(account_id, fan_id, now,
                                  deep_convo_state=_S_CHATTED_2, **reset)
                advanced += 1
                tease_text = f.deep_convo_tease_text or tease
                if _STEP_GAP_S:  # typing pause before the second bubble (shows "...is typing")
                    await hold_with_typing(account_id, fan_id, _jittered_gap(),
                                           typing_indicator=typing_indicator)
                await _send(client, account_id, fan_id, tease_text,
                            typing_wpm=typing_wpm, typing_indicator=typing_indicator,
                            typo=typo_on, nonnative=nonnative_on, protect=typo_protect, v=voice_blocks)
                await _save_state(account_id, fan_id, now, deep_convo_state=_S_DONE)
                sent += 1
                completed += 1

            elif state == _S_CHATTED_2:
                # Recovery: the reply went out last tick but the Tease send failed.
                if dry_run:
                    sent += 1
                    continue
                tease_text = f.deep_convo_tease_text or tease
                await _send(client, account_id, fan_id, tease_text,
                            typing_wpm=typing_wpm, typing_indicator=typing_indicator,
                            typo=typo_on, nonnative=nonnative_on, protect=typo_protect, v=voice_blocks)
                sent_ok = True
                await _save_state(account_id, fan_id, now, deep_convo_state=_S_DONE)
                sent += 1
                completed += 1

        except Exception as e:
            errors += 1
            await skip_unreachable_fan(account_id, fan_id, e, log=log)
            log.warning("deep_convo step failed account=%s fan=%s state=%s",
                        account_id, fan_id, state, exc_info=True)
        finally:
            # W3 (live-chat variant, mirrors of_ai_chat): on a confirmed send set
            # the SHORT cooldown and then RELEASE the lease so the fan's next step
            # (their reply ~1-2 min later) can re-acquire — keeping the 900s lease
            # would freeze the conversation for 15 min. Cooldown is committed
            # BEFORE the release, so it guards the moment the lease drops; if the
            # cooldown write fails we KEEP the lease (900s) as the fallback guard.
            if sent_ok:
                try:
                    await ax.start_fan_cooldown(
                        account_id, fan_id, cooldown_s=_REPLY_COOLDOWN_S
                    )
                    await ax.release_fan_lease(account_id, fan_id)
                except Exception:
                    log.warning("deep_convo cooldown set failed account=%s fan=%s "
                                "— keeping lease as fallback guard",
                                account_id, fan_id, exc_info=True)
            else:
                await ax.release_fan_lease(account_id, fan_id)

    return {
        "profiles": len(profiles),
        "actionable": len(actionable),
        "q_sent": q_sent_n,
        "replies_advanced": advanced,
        "completed": completed,
        "sends": sent,
        "backed_off": backed_off,
        "skipped_listed": skipped_listed,
        "skipped_no_qtease": skipped_no_qtease,
        "skipped_not_complete": skipped_not_complete,
        "skipped_high_spend": skipped_high_spend,
        "skipped_ai_chatter": skipped_ai_chatter,
        "skipped_done": skipped_done,
        "skipped_locked": skipped_locked,
        "skipped_cooldown": skipped_cooldown,
        "errors": errors,
        "cap_hit": cap_hit,
        "dry_run": dry_run,
        "model": model,
    }


# Sentinel so _generate can signal "daily LLM cap reached" without raising
# through the per-fan try/except (which would just count it as a generic error).
_CAP = object()


async def _generate(model: str, persona: str, f: Fan, c: _Candidate,
                    account_id: str, fan_id: int,
                    style_on: bool = False, nonnative_on: bool = False,
                    painful_on: bool = True, lang: str = "en",
                    clock: str = "",
                    v: "_voice.VoiceBlocks" = _voice.HER) -> str | object:
    """Generate ONE in-between reply. Returns the text, '' on a generation error,
    or the _CAP sentinel when the daily LLM cap is hit (caller stops the run)."""
    try:
        res = await llm_client.chat(
            model=model,
            messages=_build_messages(persona, f, c, style_on=style_on,
                                     nonnative_on=nonnative_on, painful_on=painful_on,
                                     lang=lang, clock=clock, v=v),
            purpose=_PURPOSE,
            account_id=account_id,
            fan_id=fan_id,
            temperature=_REPLY_TEMPERATURE,
        )
    except LLMCapExceeded:
        log.warning("deep_convo daily LLM cap reached account=%s — stopping", account_id)
        return _CAP
    except Exception:
        log.warning("deep_convo generate failed account=%s fan=%s",
                    account_id, fan_id, exc_info=True)
        return ""
    return ((getattr(res, "content", "") or "").strip())[:_REPLY_MAX_CHARS]


def _fan_asked(c: _Candidate) -> bool:
    """True if the fan's last burst (his trailing run of inbound msgs) has a '?' —
    i.e. he asked something we'd look like a wall ignoring before the scripted Q."""
    for d, b in reversed(c.messages):
        if d != "in":
            break
        if "?" in (b or ""):
            return True
    return False


def _leadin_messages(persona: str, f: Fan, c: _Candidate,
                     style_on: bool = False, nonnative_on: bool = False,
                     lang: str = "en", clock: str = "",
                     v: "_voice.VoiceBlocks" = _voice.HER) -> list[dict]:
    history = c.messages[-_HISTORY_TAIL:]
    convo = "\n".join(f"{'FAN' if d == 'in' else 'YOU'}: {b}" for d, b in history if b)
    # The lead-in DIRECTLY answers his last message — "what time is it there?"
    # lands exactly here, so it needs the prompt clock most of all.
    clock_block = (
        f"RIGHT NOW for you it is {clock}. If the time, your day, or what "
        "you're doing comes up, stay consistent with this clock — never claim "
        "a different time of day.\n\n" if clock else "")
    system = (
        f"{persona}\n"
        "He just said something (maybe asked you a question). Reply in ONE short, warm, "
        "human sentence that DIRECTLY answers / acknowledges his LAST message. Do NOT "
        "ask a question yourself (you'll ask one separately next). If his message is "
        "explicit or asks for nudes/pics, do NOT go along with it — tease and slow it "
        "down warmly instead. No emojis. Output ONLY the message text.\n\n"
        f"{clock_block}"
        f"{ONPLATFORM_GUARDRAIL}\n\n"
        f"{v.live_proof}\n\n"
        f"{BIO_CONSISTENCY_GUARDRAIL}\n\n"
        f"{v.humanizer + chr(10) + chr(10) if style_on else v.emoji_vocab}"
        f"{NONNATIVE_REGISTER if nonnative_on else ''}"
        f"{_language.output_language_directive(lang)}"
    )
    user = (f"{_pins.pins_block(f)}"
            f"Recent conversation (oldest→newest):\n{convo}\n\n"
            "Answer his last message in one short sentence.")
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


async def _generate_leadin(model: str, persona: str, f: Fan, c: _Candidate,
                           account_id: str, fan_id: int,
                           style_on: bool = False, nonnative_on: bool = False,
                           lang: str = "en", clock: str = "",
                           v: "_voice.VoiceBlocks" = _voice.HER) -> str:
    """A short bubble that answers the fan's question BEFORE the scripted Q goes out,
    so the Q doesn't read like a bot talking past him. Best-effort — any failure
    (incl. the LLM cap) just skips the lead-in and we send the Q alone."""
    try:
        res = await llm_client.chat(
            model=model, messages=_leadin_messages(persona, f, c, style_on=style_on,
                                                    nonnative_on=nonnative_on, lang=lang,
                                                    clock=clock, v=v),
            purpose=_PURPOSE,
            account_id=account_id, fan_id=fan_id, temperature=_REPLY_TEMPERATURE,
        )
    except Exception:
        return ""
    return ((getattr(res, "content", "") or "").strip())[:_REPLY_MAX_CHARS]
