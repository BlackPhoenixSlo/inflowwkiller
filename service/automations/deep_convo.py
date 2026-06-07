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
from attribution import write_outbound_attribution
from automation_registry import register
from db.engine import get_session
from db.models import AccountAiConfig, Blacklist, Fan, FanProfile, Message
from llm_client import LLMCapExceeded
from ._common import (
    STYLE_HUMANIZER, apply_word_restriction, build_facts_note,
    build_structured_nickname, casualize_qtease, coerce_ids, facts_from_fan,
    hold_with_typing, humanize_typos, load_style_flags, load_typing_indicator,
    load_typing_wpm, load_typo_flags, push_nick_and_notes, resolve_model,
    typing_delay_seconds,
)

log = logging.getLogger("of-relay.automation.deep_convo")

# ── Knobs (ported from 06_deep_convo.md) ─────────────────────────────
_DEFAULT_MODEL = "grok-4-1-fast-non-reasoning"   # llm_client fallback (19 §4)
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

# deep_convo is emoji-FREE (user mandate). The model ignores a "no emojis" prompt,
# so we strip them in code at the send chokepoint. Targets the emoji unicode blocks
# (and the variation-selector / ZWJ joiners) — NOT general punctuation, so an
# em-dash "—" or apostrophe survives. Collapse any double space left behind.
_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U00002190-\U000021FF"
    "\U00002B00-\U00002BFF\U0000FE00-\U0000FE0F\U0001F1E6-\U0001F1FF‍♀♂]+"
)
_WS_RE = re.compile(r"[ \t]{2,}")


def _strip_emojis(s: str) -> str:
    """Remove emojis (deep_convo is emoji-free) and tidy the spacing they leave."""
    return _WS_RE.sub(" ", _EMOJI_RE.sub("", s or "")).strip()


# ── Text helpers (local copies — house pattern) ──────────────────────

def _strip_html(s: str | None) -> str:
    if not s:
        return ""
    if _HTML_OPEN not in s:
        return s.strip()
    return _TAG_RE.sub("", s).strip()


def _nonempty(v) -> bool:
    """True when a fan-fact column carries real signal (not '', not '[]'/'{}')."""
    if v is None:
        return False
    if isinstance(v, str):
        s = v.strip()
        return bool(s) and s not in ("[]", "{}")
    return bool(v)


def _first_nonempty(*vals: str | None) -> str:
    for v in vals:
        if _nonempty(v):
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
        _nonempty(f.his_age),
        _nonempty(f.home_country) or _nonempty(f.home_city),
        _nonempty(f.hobbies),
        (_nonempty(f.recent_events) or _nonempty(f.fetishes)
         or bool(f.likes_boobs) or bool(f.likes_ass)),
    )
    return (sum(1 for g in groups if g) / len(groups)) >= _INFO_COMPLETE_RATIO


async def _load_persona(account_id: str) -> str:
    async with get_session() as s:
        cfg = await s.get(AccountAiConfig, account_id)
    return (cfg.persona if cfg and cfg.persona else "").strip() or (
        "You are a warm, flirty OnlyFans creator chatting with one of your fans."
    )


# ── Loaders (own session each) ────────────────────────────────────────

async def _load_blacklist() -> set[int]:
    async with get_session() as s:
        rows = (await s.execute(select(Blacklist.fan_id))).all()
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
                    style_on: bool = False) -> list[dict]:
    """Compose the (system, user) pair for an in-between reply during the drill
    (the Q + Tease themselves are sent verbatim from the profile — not generated)."""
    facts = []
    name = (f.real_name or f.generated_nickname or f.of_display_name or "").strip()
    if name:
        facts.append(f"name/nickname: {name.split('/')[0][:40]}")
    for label, val in (("age", f.his_age), ("city", f.home_city),
                       ("country", f.home_country), ("hobbies", f.hobbies),
                       ("occupation", f.occupation), ("fetishes", f.fetishes)):
        if _nonempty(val):
            facts.append(f"{label}: {str(val).strip()[:80]}")
    facts_block = ("\n".join(f"- {x}" for x in facts)
                   if facts else "- (nothing on file yet)")

    history = c.messages[-_HISTORY_TAIL:]
    convo = "\n".join(
        f"{'FAN' if d == 'in' else 'YOU'}: {b}" for d, b in history if b
    )

    system = (
        f"{persona}\n"
        "You're deepening things with a fan you already know well — you're past the "
        "getting-to-know-you stage, so be more playful, flirty and a little naughty, "
        "building on what he just said. No need to say 'Hey!' or use his name at the "
        "start. Keep it short, 1 question max, max 2 verbs. Never sound like a bot — "
        "you are 100% human, girly, warm. Don't offer pics or videos. Do NOT use any "
        "emojis in this reply.\n\n"
        f"{STYLE_HUMANIZER + chr(10) + chr(10) if style_on else ''}"
        "IMPORTANT: your reply is ONLY the chat message text. Never include JSON, "
        "code blocks, curly braces, or any metadata. Just text him back."
    )
    user = (
        f"What you know about him:\n{facts_block}\n\n"
        f"Recent conversation (oldest→newest):\n{convo}\n\n"
        "Don't apologize. Generate a brief, 1 verb max, casual, GIRLY reply (do girl "
        "things and the occasional little typo) that flirts and builds on his last "
        "message. Reply now."
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
            notes=build_facts_note(facts_from_fan(f), short_bio=short_bio),
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
            body=str(result.get("text") or text),
            price_cents=0,
            created_at=ax._parse_iso(result.get("createdAt")) or datetime.utcnow(),
            emit_live=True,  # WORKER→SSE bridge: surface the send live
        )
    else:
        # 200 but no id → unrecordable (message_id is the PK). deep_convo_state is
        # the durable guard, so the caller still advances it; scrape backfills the
        # row later. NEVER leave state such that the fan is re-messaged.
        log.warning("deep_convo send returned no id account=%s fan=%s — "
                    "advancing state on the 200 (no-id contract)", account_id, fan_id)


async def _send(client, account_id: str, fan_id: int, text: str,
                *, typing_wpm: float | None = None,
                typing_indicator: bool | None = None,
                typo: bool = False, protect=()) -> bool:
    """Send one message + persist it through the optimistic path. Returns True on
    a 200 (whether or not OF echoed an id). Raises on a transport failure so the
    caller can leave state un-advanced and retry next tick.

    typing_wpm / typing_indicator are loaded once per run by the caller and
    threaded in; they fall back to a per-call load when omitted. When `typo` is
    on, the message may pick up a realistic thumb-slip and an optional follow-up
    "*fix" bubble — the FIRST bubble is the real message (its failure raises, as
    before); the cosmetic "*fix" is best-effort and never fails the send."""
    text = _strip_emojis(text)  # deep_convo is emoji-free (covers Q, replies, tease)
    text = apply_word_restriction(text)  # last-mile OF-restricted word substitution
    if typing_wpm is None:
        typing_wpm = await load_typing_wpm(account_id)
    if typing_indicator is None:
        typing_indicator = await load_typing_indicator(account_id)

    parts = [text]
    if typo:
        parts = humanize_typos([text], random.Random(f"{fan_id}:{text}"),
                               protect=protect, max_bubbles=2) or [text]

    main, *extra = parts
    await _send_one(client, account_id, fan_id, main, typing_wpm, typing_indicator)
    for fix in extra:  # the "*fix" bubble is cosmetic — never fail the send on it
        try:
            await _send_one(client, account_id, fan_id, fix, typing_wpm, typing_indicator)
        except Exception:
            log.debug("deep_convo *fix bubble send failed (cosmetic)", exc_info=True)
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
    typo_on = (await load_typo_flags(account_id))[_PURPOSE]    # thumb-typo opt-in
    typing_wpm = await load_typing_wpm(account_id)            # per-bubble pacing
    typing_indicator = await load_typing_indicator(account_id)  # live "...is typing"
    persona = await _load_persona(account_id)
    blacklist = await _load_blacklist()
    profiles, skipped_no_qtease = await _load_profiles(account_id)  # both present
    if only_fan_ids:
        profiles = {fid: v for fid, v in profiles.items() if fid in only_fan_ids}

    now = datetime.utcnow()
    skipped_listed = 0          # blacklist / paused
    skipped_not_complete = 0    # profile < 75% complete
    skipped_high_spend = 0      # lifetime spend > $200
    skipped_done = 0
    backed_off = 0              # waiting on a fan reply this tick

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
        if fan_id in blacklist:
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

        # Don't deep-convo whales: a fan past $200 lifetime spend is already a
        # paying regular, so skip the engagement drill (force_ids bypasses).
        if not forced and f is not None and int(f.lifetime_spend_cents or 0) > max_spend_cents:
            skipped_high_spend += 1
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
                                                  style_on=style_on)
                    if lead:
                        await _send(client, account_id, fan_id, lead,
                                    typing_wpm=typing_wpm, typing_indicator=typing_indicator,
                            typo=typo_on, protect=typo_protect)
                        if _STEP_GAP_S:
                            await hold_with_typing(account_id, fan_id, _jittered_gap(),
                                                   typing_indicator=typing_indicator)
                await _send(client, account_id, fan_id, q,
                            typing_wpm=typing_wpm, typing_indicator=typing_indicator,
                            typo=typo_on, protect=typo_protect)
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
                                       style_on=style_on)
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
                            typo=typo_on, protect=typo_protect)
                sent_ok = True
                await _save_state(account_id, fan_id, now,
                                  deep_convo_state=_S_CHATTED_1, **reset)
                sent += 1
                advanced += 1

            elif state == _S_CHATTED_1:
                reply = await _generate(model, persona, f, c, account_id, fan_id,
                                       style_on=style_on)
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
                            typo=typo_on, protect=typo_protect)
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
                            typo=typo_on, protect=typo_protect)
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
                            typo=typo_on, protect=typo_protect)
                sent_ok = True
                await _save_state(account_id, fan_id, now, deep_convo_state=_S_DONE)
                sent += 1
                completed += 1

        except Exception:
            errors += 1
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
                    style_on: bool = False) -> str | object:
    """Generate ONE in-between reply. Returns the text, '' on a generation error,
    or the _CAP sentinel when the daily LLM cap is hit (caller stops the run)."""
    try:
        res = await llm_client.chat(
            model=model,
            messages=_build_messages(persona, f, c, style_on=style_on),
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
                     style_on: bool = False) -> list[dict]:
    history = c.messages[-_HISTORY_TAIL:]
    convo = "\n".join(f"{'FAN' if d == 'in' else 'YOU'}: {b}" for d, b in history if b)
    system = (
        f"{persona}\n"
        "He just said something (maybe asked you a question). Reply in ONE short, warm, "
        "human sentence that DIRECTLY answers / acknowledges his LAST message. Do NOT "
        "ask a question yourself (you'll ask one separately next). If his message is "
        "explicit or asks for nudes/pics, do NOT go along with it — tease and slow it "
        "down warmly instead. No emojis. Output ONLY the message text.\n\n"
        f"{STYLE_HUMANIZER if style_on else ''}"
    )
    user = f"Recent conversation (oldest→newest):\n{convo}\n\nAnswer his last message in one short sentence."
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


async def _generate_leadin(model: str, persona: str, f: Fan, c: _Candidate,
                           account_id: str, fan_id: int,
                           style_on: bool = False) -> str:
    """A short bubble that answers the fan's question BEFORE the scripted Q goes out,
    so the Q doesn't read like a bot talking past him. Best-effort — any failure
    (incl. the LLM cap) just skips the lead-in and we send the Q alone."""
    try:
        res = await llm_client.chat(
            model=model, messages=_leadin_messages(persona, f, c, style_on=style_on),
            purpose=_PURPOSE,
            account_id=account_id, fan_id=fan_id, temperature=_REPLY_TEMPERATURE,
        )
    except Exception:
        return ""
    return ((getattr(res, "content", "") or "").strip())[:_REPLY_MAX_CHARS]
