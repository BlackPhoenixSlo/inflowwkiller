"""
service/automations/of_ai_chat.py — Automation A05: of_ai_chat (the reply loop).

Spec: library/one_section_of_automations/05_of_ai_chat.md — but the DOM world it
describes is GONE. The legacy script drove `.tiptap.ProseMirror` over a live
OnlyFans page; here the same brain runs over OUR DB (the WS pump + scrape_chats
already filled `messages`/`fans`) and sends through the existing optimistic send
path. Per the network-rewrite mapping at the bottom of the spec:

    sidebar enumeration      → DB scan of `messages` per fan
    open chat / load history → the trimmed history we already have in `messages`
    send via ProseMirror     → of_client.send_message → write_outbound_attribution

What it does, per tick (`run(account_id, payload, *, run_id)`):

  1. Load the stop-lists once: global `blacklist` (by fan_id) + the per-account
     `skip_list`. These are the cheap pre-filters the spec's anti-loop layers
     depend on.
  2. One pass over the account's `messages` → per-fan inbound count, the LAST
     message (direction + body), and a trimmed history for the prompt. A chat is
     a candidate ONLY when the fan spoke last (`last.direction == 'in'`); if WE
     sent last there is nothing to answer (the spec's "You: " sidebar skip).
  3. Stop-conditions that PERSIST a skip (so a banned fan stays banned across
     ticks): lifetime spend > $1 → "spent"; ≥ 10 fan messages → "too_long";
     info-complete (≥ 75% of the four bio field-groups filled) → "info". Each
     writes a `skip_list` row and drops the fan. `automation_paused_until` (human
     override) and the blacklist/skip_list membership drop a fan WITHOUT writing
     a new skip row.
  4. For each surviving candidate: take the per-(account, fan) send-lease so
     A06/A07/A09/A11 can't double-message the same fan in an overlapping cycle,
     generate ONE reply via `llm_client.chat` (which writes the `grok_calls`
     audit row itself + enforces the daily cost cap atomically), apply the OF
     word-restriction ("meet" → "meeet"), send via `of_client.send_message`, then
     persist through the existing optimistic path: `write_outbound_attribution`
     (Automation employee, `emit_live=True` so an open chat updates live) and bump
     `fans.turn_counter` (drives persona depth next time).

Concurrency / sessions: each DB write uses its OWN AsyncSession — never one
shared across branches (the SQLAlchemy parallel-session footgun). The daily-spend
cap is serialized inside llm_client's atomic reserve, so this automation does NOT
need the executor's account_spend_lock. Replies are sent sequentially with the
per-fan lease as the anti-double-message guard.

Scheduling: self-registers via `@register("of_ai_chat")` on import (the executor
auto-imports `service/automations/*`). To run it periodically, insert an
`automation_rules` row with `kind="of_ai_chat"` and
`trigger_json={"every_seconds": N}`. NO edit to automation_executor.py.

Payload knobs (all optional): `limit` (candidate sweep ceiling), `max_replies`
(per-run send cap), `model` (LLM override), `dry_run` (generate but don't send),
`force_ids` ([fan_id, …] bypass the spend/too_long/info gates — manual targeting).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

import automation_executor as ax  # _make_client / _parse_iso / fan-lease seams
import llm_client                  # call .chat at runtime so tests can patch it
from attribution import write_outbound_attribution
from automation_registry import register
from db.engine import get_session
from db.models import (
    AccountAiConfig, Blacklist, Fan, FanProfile, FunnelState, MassRun, Message, SkipList,
)
from llm_client import LLMCapExceeded
from ._common import (
    apply_word_restriction, build_facts_note, build_structured_nickname, coerce_ids,
    facts_from_fan, push_nick_and_notes, resolve_model,
)
from . import gen_info  # profile_is_stale() — the refresh-if-stale hook below

# A 200-from-OF without a message id can't be persisted (message_id is the PK),
# so eligibility ("fan spoke last") would re-fire next tick → double-reply.
# Pause the fan briefly so scrape_chats can record the real send first.
_NOID_PAUSE = timedelta(hours=1)

log = logging.getLogger("of-relay.automation.of_ai_chat")

# ── Knobs (ported from 05_of_ai_chat.md) ─────────────────────────────
_DEFAULT_MODEL = "grok-4-1-fast-non-reasoning"   # llm_client fallback (19 §4)
_PURPOSE = "of_ai_chat"          # also the account_ai_config.model_by_purpose key
_SPEND_GATE_CENTS = 100          # bought_amount > $1 → hand off to humans
_MAX_FAN_MESSAGES = 10           # runaway-conversation cutoff (spec MAX_FAN_MESSAGES)
_INFO_COMPLETE_RATIO = 0.75      # ≥ 3 of 4 bio groups filled → enough signal
_DEFAULT_LIMIT = 200             # candidate sweep ceiling
_DEFAULT_MAX_REPLIES = 25        # per-run send cap (logged when it bites)
_REPLY_TEMPERATURE = 0.85        # warm/varied, matches the legacy persona call
_HISTORY_TAIL = 40               # last N messages handed to the model
_MSG_CLIP = 400                  # clip each message body
_REPLY_MAX_CHARS = 600           # trim a runaway generation before sending
# of_ai_chat is a LIVE chat — the generic 30-min W3 cooldown would make the bot
# go silent and feel broken. Use a short rest instead: long enough that a fast
# fan reply doesn't get an INSTANT (AI-looking) answer, short enough to stay
# responsive. Perceived gap between our replies = this + up to one 30s tick, so
# 45s lands a fan-felt ~45-75s pause. Unlike the other senders, of_ai_chat also
# RELEASES its lease on success (see finally) so this cooldown is the sole gate.
_REPLY_COOLDOWN_S = 45

_HTML_OPEN = "<"
_TAG_RE = re.compile(r"<[^>]+>")


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


def _is_info_complete(f: Fan) -> bool:
    """Port of the spec's `is_info_complete`: four bio field-groups, ≥ 75% filled.

      1. age                        (his_age)
      2. location                   (home_country OR home_city)
      3. hobbies                    (hobbies)
      4. depth                      (recent_events OR fetishes OR likes_boobs OR likes_ass)
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


# ── Stop-lists + candidate gathering ─────────────────────────────────

async def _load_stop_lists(account_id: str) -> tuple[set[int], set[int]]:
    """(blacklist fan_ids [global], skip_list fan_ids [this account])."""
    async with get_session() as s:
        bl = (await s.execute(select(Blacklist.fan_id))).all()
        sk = (await s.execute(
            select(SkipList.fan_id).where(SkipList.account_id == str(account_id))
        )).all()
    return {int(r[0]) for r in bl}, {int(r[0]) for r in sk}


async def _load_mid_funnel_fans(account_id: str) -> set[int]:
    """Fans with a pending funnel_state under one of this account's mass runs.
    W7 cross-tick ownership: reply_mass_funnel owns these fans through a
    multi-step flow (2–10 min waits between steps), so of_ai_chat must NOT
    interleave them — else the bot answers mid-funnel and double-touches.
    Fixes the Wave-3 audience-overlap note."""
    async with get_session() as s:
        rows = (await s.execute(
            select(FunnelState.fan_id)
            .join(MassRun, MassRun.id == FunnelState.mass_run_id)
            .where(MassRun.account_id == str(account_id),
                   FunnelState.status == "pending")
        )).all()
    return {int(r[0]) for r in rows}


class _Candidate:
    __slots__ = ("fan_id", "fan_msg_n", "last_dir", "last_body", "messages")

    def __init__(self, fan_id: int):
        self.fan_id = fan_id
        self.fan_msg_n = 0
        self.last_dir = ""
        self.last_body = ""
        self.messages: list[tuple[str, str]] = []  # (direction, body) oldest→newest


async def _gather(account_id: str) -> dict[int, _Candidate]:
    """One pass over the account's messages → per-fan inbound count, last
    message, and trimmed history."""
    out: dict[int, _Candidate] = {}
    async with get_session() as s:
        rows = (await s.execute(
            select(Message.fan_id, Message.direction, Message.body)
            .where(Message.account_id == str(account_id),
                   Message.is_unsent.is_(False))
            .order_by(Message.fan_id, Message.created_at, Message.message_id)
        )).all()
    for fan_id, direction, body in rows:
        c = out.get(fan_id)
        if c is None:
            c = out[fan_id] = _Candidate(int(fan_id))
        text = _strip_html(body)[:_MSG_CLIP]
        c.messages.append((direction, text))
        c.last_dir = direction
        c.last_body = text
        if direction == "in":
            c.fan_msg_n += 1
    return out


def _questions_still_needed(f: Fan, asked: set[str]) -> list[tuple[str, str]]:
    """The dynamic 'INFORMATION YOU STILL NEED' list — ported verbatim in spirit
    from V1 message_generator._build_questions_to_ask. The prompt CHANGES based on
    what we already know: each gap becomes ONE natural question, ordered by
    priority, skipping any topic already in `asked` so the bot never nags. The
    'save' side is gen_info (it extracts these same fields from the convo)."""
    name = _nonempty(f.real_name)
    age = _nonempty(f.his_age)
    country = _nonempty(f.home_country)
    city = _nonempty(f.home_city)
    occupation = _nonempty(f.occupation)
    hobbies = _nonempty(f.hobbies)
    fetishes = _nonempty(f.fetishes)
    boobs = bool(f.likes_boobs)
    ass = bool(f.likes_ass)
    events = _nonempty(f.recent_events)

    q: list[tuple[str, str]] = []
    # Name + age are the fundamentals — gather them first.
    if not name and "name" not in asked:
        q.append(("name", "- You DON'T know his real name yet. Ask what his name is "
                  "/ what you should call him."))
    if not age and "age" not in asked:
        q.append(("age", "- You DON'T know his age yet. Find out how old he is."))
    if not country and not city and "location" not in asked:
        q.append(("location", "- You DON'T know where he lives. Ask where he's from."))
    if not country and city and "country_from_city" not in asked:
        q.append(("country_from_city",
                  f"- He mentioned {str(f.home_city).strip()} but you don't know "
                  "which country. Confirm or guess the country."))
    if country and not city and "city" not in asked:
        q.append(("city",
                  f"- You know he's from {str(f.home_country).strip()} but not which "
                  "city/area. Ask what part."))
    if not occupation and "job" not in asked:
        q.append(("job", "- You DON'T know what he does for work. Ask what he does for a living."))
    if not hobbies and "hobbies" not in asked:
        q.append(("hobbies", "- You DON'T know his hobbies yet. Ask what he likes to do."))
    if not boobs and not ass and not fetishes and "boobs_ass" not in asked:
        q.append(("boobs_ass", "- You DON'T know what he's into (boobs/ass). Ask playfully."))
    if not events and "recent_events" not in asked:
        q.append(("recent_events", "- Ask what's new in his life lately."))
    return q


def _build_messages(persona: str, f: Fan, c: _Candidate,
                    asked: set[str],
                    history_tail: int = _HISTORY_TAIL) -> tuple[list[dict], list[str]]:
    """Compose the (system, user) pair — a faithful port of V1
    prompts.create_chat_response: a short, GIRLY, 100%-human reply that flirts
    WHILE gathering the one piece of info we still need. The 'still need' block is
    driven off `_questions_still_needed` (what we already know), so early chats
    interrogate-by-flirting and complete profiles just banter. Returns
    (messages, presented_keys) so the caller can mark the asked topic and not nag.

    Persona/location/etc. are config (account_ai_config) — UI-editable later; the
    instruction scaffolding around them is this template."""
    questions = _questions_still_needed(f, asked)
    question_lines = "\n".join(line for _, line in questions)
    presented = [k for k, _ in questions]

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

    history = c.messages[-history_tail:]
    convo = "\n".join(
        f"{'FAN' if d == 'in' else 'YOU'}: {b}" for d, b in history if b
    )

    need_block = (
        "INFORMATION YOU STILL NEED (ordered by priority — ask about the FIRST one "
        "you don't already know, ONE per message, naturally woven into the convo. "
        "Get his name and age first):\n" + question_lines
        if question_lines else
        "You already know everything you need. Just have a natural flirty conversation."
    )

    system = (
        f"{persona}\n"
        "Your job is to understand the fan. Continue the friendly, flirty "
        "conversation — no need to say 'Hey!' or use his name at the start every "
        "time. Don't share your own info unless he asks, use only what you've "
        "learned about him, and don't offer any pics or videos yet. Keep replies "
        "short, 1 question max, max 2 verbs. Never sound like a bot — you are 100% "
        "human. He may text several messages in a row — read them all and reply to "
        "the latest. You're learning: his name; how old he is; where he lives "
        "(country + city/area); his hobbies; his fetishes; whether he likes boobs "
        "or ass or both.\n\n"
        f"{need_block}\n\n"
        "CRITICAL RULES:\n"
        "- If he gets explicit early, asks for nudes/pics/video, or tries to jump "
        "straight into sex stuff (e.g. \"bend you over\", \"send nudes\"): do NOT go "
        "along with it and do NOT offer anything. Playfully TEASE and slow it down — "
        "you want to get to know him first — then steer back to your one "
        "get-to-know-you question. Stay warm, flirty and teasing, never cold, "
        "preachy or robotic.\n"
        "- NEVER repeat a question or a phrase you've already used (e.g. don't ask "
        "'what are you chilling with' twice). Vary your wording and move forward.\n"
        "- NEVER ask a question he already answered in the chat history, even "
        "vaguely. You get ONE chance per topic.\n"
        "- If his answer was vague, ask a follow-up to CLARIFY — don't re-ask the "
        "same thing.\n"
        "- Only ask 1 question per message. Never stack multiple questions.\n"
        "- If there are no questions listed above, just chat naturally — do NOT "
        "invent new interrogation questions.\n\n"
        "IMPORTANT: your reply is ONLY the chat message text. Never include JSON, "
        "code blocks, curly braces, or any metadata. Just write the reply as if "
        "texting him."
    )
    user = (
        f"What you know about him:\n{facts_block}\n\n"
        f"Recent conversation (oldest→newest):\n{convo}\n\n"
        "Ask deep questions about his answers, or the next question if his reply is "
        "short. Don't apologize. Generate a brief, 1 verb max, casual, GIRLY reply "
        "(do girl things and the occasional little typo/mistake). Reply to his last "
        "message now."
    )
    return ([{"role": "system", "content": system},
             {"role": "user", "content": user}], presented)


# ── Persistence seams (own session each) ─────────────────────────────

async def _add_to_skip_list(account_id: str, fan_id: int, reason: str) -> None:
    async with get_session() as s:
        await s.execute(
            sqlite_insert(SkipList)
            .values(account_id=str(account_id), fan_id=int(fan_id),
                    reason=reason, added_at=datetime.utcnow())
            .on_conflict_do_nothing(index_elements=["account_id", "fan_id"])
        )


async def _skip_and_collect(account_id: str, fan_id: int, reason: str) -> None:
    """Cut a fan out of the AI chat AND enqueue a FINAL gen_info regen, so the facts
    we extracted inline each turn are turned into a fresh profile/nickname/note before
    a human takes over (`spent` → paying fan) or the convo is abandoned (`too_long`).
    Mirrors the info-complete handoff's gen_info enqueue (_handoff_to_deep_convo): the
    early-exit cutoffs used to skip-list only, leaving the structured profile stale."""
    await _add_to_skip_list(account_id, fan_id, reason)
    await ax.enqueue_job(account_id, "gen_info", payload={"force_ids": [int(fan_id)]})


# ── Inline fact fill (V1 grok_analisis.update_fan_with_grok_analysis) ─────────
# Every tick, BEFORE replying, of_ai_chat extracts the facts he actually stated and
# saves them NOW (so "31" lands in his_age this turn, not on a later gen_info pass).
# This is the "fill it yourself with the full model" step the legacy chat AI did.
_EXTRACT_FIELDS = ("real_name", "his_age", "home_country", "home_city",
                   "hobbies", "occupation", "fetishes")
_EXTRACT_SYSTEM = (
    "You are a profile analyzer. From the chat, extract ONLY facts the fan ACTUALLY "
    "stated. Return ONLY a JSON object with these exact fields. Keep the CURRENT value "
    "if there is no new info; use \"\" if it was never stated — NEVER guess (don't "
    "guess a city from a country, don't invent a job/hobby):\n"
    '{"real_name":"","his_age":"","home_country":"","home_city":"","hobbies":"",'
    '"occupation":"","fetishes":"","likes_boobs":false,"likes_ass":false,'
    '"recent_events":[]}'
)


def _extract_messages(f: Fan, c: _Candidate,
                      history_tail: int = _HISTORY_TAIL) -> list[dict]:
    current = {k: (getattr(f, k) or "") for k in _EXTRACT_FIELDS}
    current["likes_boobs"] = bool(f.likes_boobs)
    current["likes_ass"] = bool(f.likes_ass)
    history = c.messages[-history_tail:]
    convo = "\n".join(f"{'FAN' if d == 'in' else 'YOU'}: {b}" for d, b in history if b)
    user = (f"CURRENT (ground truth — keep unless he says something new):\n"
            f"{json.dumps(current)}\n\nChat (oldest→newest):\n{convo}\n\n"
            "Update the JSON with anything new he stated.")
    return [{"role": "system", "content": _EXTRACT_SYSTEM},
            {"role": "user", "content": user}]


async def _extract_and_fill(account_id: str, fan_id: int, f: Fan,
                            c: _Candidate, model: str,
                            history_tail: int = _HISTORY_TAIL) -> Fan:
    """Extract his stated facts and persist any NEW ones onto the Fan row (fill
    empty fields only — never overwrite/guess). Updates `f` in place so this tick's
    reply + question list see the fresh facts. Raises LLMCapExceeded (caller stops);
    any other failure is swallowed and leaves `f` unchanged."""
    res = await llm_client.chat(
        model=model, messages=_extract_messages(f, c, history_tail), purpose=_PURPOSE,
        account_id=account_id, fan_id=fan_id, temperature=0.2,
        response_format={"type": "json_object"},
    )
    data = getattr(res, "parsed", None)
    if data is None:
        try:
            data = json.loads((res.content or "").strip())
        except Exception:
            return f
    if not isinstance(data, dict):
        return f

    updates: dict = {}
    for col in _EXTRACT_FIELDS:
        v = data.get(col)
        if isinstance(v, str) and v.strip() and not _nonempty(getattr(f, col, None)):
            updates[col] = v.strip()[:200]
    for col in ("likes_boobs", "likes_ass"):
        if data.get(col) is True and not bool(getattr(f, col, False)):
            updates[col] = True
    ev = data.get("recent_events")
    if isinstance(ev, list) and ev and not _nonempty(f.recent_events):
        updates["recent_events"] = json.dumps([str(x)[:120] for x in ev][:6])
    if not updates:
        return f

    now = datetime.utcnow()
    async with get_session() as s:
        await s.execute(
            sqlite_insert(Fan)
            .values(account_id=str(account_id), fan_id=int(fan_id),
                    grok_facts_updated_at=now, updated_at=now, **updates)
            .on_conflict_do_update(
                index_elements=["account_id", "fan_id"],
                set_={**updates, "grok_facts_updated_at": now, "updated_at": now},
            )
        )
    for k, v in updates.items():
        setattr(f, k, v)
    return f


async def _handoff_to_deep_convo(client, account_id: str, fan_id: int, f: Fan) -> None:
    """End of the normal chat: skip-list the fan ('info'), enqueue a gen_info regen,
    and push the FINAL structured nickname + fact-sheet NOTE to OF (notes land here,
    at the end of the AI convo)."""
    await _add_to_skip_list(account_id, fan_id, "info")
    await ax.enqueue_job(account_id, "gen_info", payload={"force_ids": [int(fan_id)]})
    try:
        await push_nick_and_notes(
            client, account_id, fan_id,
            nick=build_structured_nickname(f),
            notes=build_facts_note(facts_from_fan(f)),
        )
    except Exception:
        log.debug("of_ai_chat handoff push failed account=%s fan=%s",
                  account_id, fan_id, exc_info=True)


async def _maybe_push_nickname(client, account_id: str, fan_id: int, f: Fan) -> None:
    """Update the structured nickname EVERY convo tick and push it to OF when it
    changed (V1 apply_nickname_in_chat). Mirrors to fans.custom_nickname. Notes are
    NOT touched here — those go out once at handoff/deep-convo start."""
    nick = build_structured_nickname(f)
    if nick and nick != (f.custom_nickname or ""):
        await push_nick_and_notes(client, account_id, fan_id, nick=nick)
        f.custom_nickname = nick


async def _mark_question_asked(account_id: str, fan_id: int, key: str,
                               asked: set[str]) -> None:
    """Record that we asked about `key` so the bot never re-asks it (V1
    QuestionsAsked). UPSERT (not UPDATE): a candidate sourced from `messages` may
    have no Fan row yet — a bare UPDATE would match 0 rows and silently lose the
    tracker, re-opening the nag."""
    new_set = sorted(asked | {key})
    now = datetime.utcnow()
    async with get_session() as s:
        await s.execute(
            sqlite_insert(Fan)
            .values(account_id=str(account_id), fan_id=int(fan_id),
                    questions_asked=json.dumps(new_set), updated_at=now)
            .on_conflict_do_update(
                index_elements=["account_id", "fan_id"],
                set_={"questions_asked": json.dumps(new_set), "updated_at": now},
            )
        )


async def _bump_turn_counter(account_id: str, fan_id: int, now: datetime) -> None:
    async with get_session() as s:
        await s.execute(
            update(Fan)
            .where(Fan.account_id == str(account_id), Fan.fan_id == int(fan_id))
            .values(turn_counter=Fan.turn_counter + 1,
                    last_message_sent_at=now, updated_at=now)
        )


async def _maybe_refresh_profile(account_id: str, fan_id: int, fan_msg_n: int,
                                 now: datetime) -> bool:
    """Refresh-if-stale hook: after an AI-chat reply lands, regenerate the fan's
    profile from the latest messages IF the Fibonacci/age gate says it's due
    (`gen_info.profile_is_stale`). Enqueues a gen_info job (force_ids=[fan]) so the
    regen runs ASYNC on the supervisor — the chat reply is never blocked or slowed.
    Best-effort: any failure here is logged and swallowed (never breaks the reply).
    Returns True iff a refresh was enqueued."""
    try:
        async with get_session() as s:
            row = (await s.execute(
                select(FanProfile.message_count_at_gen, FanProfile.last_generated_at,
                       FanProfile.q1, FanProfile.q2, FanProfile.q3,
                       FanProfile.tease1, FanProfile.tease2, FanProfile.tease3)
                .where(FanProfile.account_id == str(account_id),
                       FanProfile.fan_id == int(fan_id))
            )).first()
        prev_count = int(row[0]) if row else None
        last_gen_at = row[1] if row else None
        # Refill openers too when a whole Q/Tease class has been consumed (all null)
        # — the empty-lines gate inside profile_is_stale still requires new messages.
        lines_empty = False
        if row:
            q_empty = not any((v or "").strip() for v in (row[2], row[3], row[4]))
            tease_empty = not any((v or "").strip() for v in (row[5], row[6], row[7]))
            lines_empty = q_empty or tease_empty
        if not gen_info.profile_is_stale(prev_count, last_gen_at, fan_msg_n, now,
                                         lines_empty=lines_empty):
            return False
        await ax.enqueue_job(account_id, "gen_info",
                             payload={"force_ids": [int(fan_id)]})
        return True
    except Exception:
        log.debug("of_ai_chat profile-refresh enqueue skipped account=%s fan=%s",
                  account_id, fan_id, exc_info=True)
        return False


async def _pause_fan(account_id: str, fan_id: int, until: datetime) -> None:
    """Set automation_paused_until so the no-id send isn't re-replied next tick
    (the no-id send contract — see automations/_common.py).

    UPSERT, not UPDATE: candidates come from the messages table, so an eligible
    fan may have NO Fan row yet (gather builds a transient one) — a plain UPDATE
    would match 0 rows and silently fail to pause, re-introducing the double-reply.
    """
    async with get_session() as s:
        await s.execute(
            sqlite_insert(Fan)
            .values(account_id=str(account_id), fan_id=int(fan_id),
                    automation_paused_until=until)
            .on_conflict_do_update(
                index_elements=["account_id", "fan_id"],
                # updated_at = now, NOT the future pause instant (that leaks into
                # push_to_sheets' "last updated"). Same fix as start_fan_cooldown.
                set_={"automation_paused_until": until, "updated_at": datetime.utcnow()},
            )
        )


# ── The automation ───────────────────────────────────────────────────

@register("of_ai_chat")
async def run(account_id: str, payload: dict, *, run_id: int) -> dict:
    payload = payload or {}
    dry_run = bool(payload.get("dry_run"))
    limit = int(payload.get("limit") or _DEFAULT_LIMIT)
    max_replies = int(payload.get("max_replies") or _DEFAULT_MAX_REPLIES)
    history_tail = int(payload.get("history_tail") or _HISTORY_TAIL)
    force_ids = coerce_ids(payload.get("force_ids"))

    model = await resolve_model(account_id, _PURPOSE, payload.get("model"))
    persona = await _load_persona(account_id)
    blacklist, skip_list = await _load_stop_lists(account_id)
    mid_funnel_fans = await _load_mid_funnel_fans(account_id)  # W7 cross-tick ownership
    by_fan = await _gather(account_id)

    # of_client only — no DOM. Built via the executor seam tests override.
    client = await asyncio.to_thread(ax._make_client, account_id)

    # Fan rows carry spend / paused / the bio facts the gates + prompt read.
    async with get_session() as s:
        fan_rows = (await s.execute(
            select(Fan).where(Fan.account_id == str(account_id))
        )).scalars().all()
    fans: dict[int, Fan] = {int(f.fan_id): f for f in fan_rows}

    now = datetime.utcnow()
    candidates: list[_Candidate] = []
    skipped_listed = 0      # blacklist / skip_list / paused
    skipped_not_turn = 0    # we (or nobody) spoke last
    newly_skiplisted = 0    # spent / too_long / info this tick

    for fan_id, c in by_fan.items():
        forced = fan_id in force_ids
        if fan_id in blacklist or fan_id in skip_list:
            skipped_listed += 1
            continue
        # W7: a fan mid-funnel is owned by reply_mass_funnel across its 2–10 min
        # step waits — don't interleave it here (force_ids can still override).
        if fan_id in mid_funnel_fans and not forced:
            skipped_listed += 1
            continue
        # Only answer when the fan spoke last (the "You: " sidebar skip).
        if c.last_dir != "in":
            skipped_not_turn += 1
            continue
        f = fans.get(fan_id)
        if f is not None and f.automation_paused_until and f.automation_paused_until > now:
            skipped_listed += 1
            continue

        if not forced and f is not None:
            if int(f.lifetime_spend_cents or 0) > _SPEND_GATE_CENTS:
                await _skip_and_collect(account_id, fan_id, "spent")
                newly_skiplisted += 1
                continue
            if c.fan_msg_n >= _MAX_FAN_MESSAGES:
                await _skip_and_collect(account_id, fan_id, "too_long")
                newly_skiplisted += 1
                continue
            # End of the normal chat: only hand off to deep_convo once we have
            # LOTS of info — both the ≥75% completeness floor AND no remaining
            # topic to ask (we've walked the whole question list). Then (re)generate
            # the profile + Q/Teases from the now-complete info so deep_convo has
            # fresh, tailored material. If still info-complete but topics remain,
            # keep gathering (don't hand off yet).
            if _is_info_complete(f):
                try:
                    asked_f = set(json.loads(f.questions_asked or "[]"))
                except Exception:
                    asked_f = set()
                if not _questions_still_needed(f, asked_f):
                    await _handoff_to_deep_convo(client, account_id, fan_id, f)
                    newly_skiplisted += 1
                    continue
        candidates.append(c)

    # Deterministic order; most-recent-talkers first via inbound volume.
    candidates.sort(key=lambda x: (-x.fan_msg_n, x.fan_id))
    candidates = candidates[:limit]

    sent = 0
    skipped_locked = 0
    skipped_cooldown = 0
    errors = 0
    cap_hit = False

    for c in candidates:
        if sent >= max_replies:
            log.info("of_ai_chat batch capped account=%s cap=%d (rest next tick)",
                     account_id, max_replies)
            break
        fan_id = c.fan_id
        # Another automation messaged this fan recently → rest it (W3 cooldown).
        if await ax.fan_on_cooldown(account_id, fan_id):
            skipped_cooldown += 1
            continue
        # One bot message per fan per cycle — don't race A06/A07/A09/A11.
        if not await ax.acquire_fan_lease(account_id, fan_id, "of_ai_chat"):
            skipped_locked += 1
            continue
        sent_ok = False
        try:
            f = fans.get(fan_id) or Fan(account_id=str(account_id), fan_id=fan_id)
            # Inline fact fill (V1): save the facts he just stated BEFORE replying,
            # so age/job/location land this turn. Cap → stop the run cleanly.
            try:
                f = await _extract_and_fill(account_id, fan_id, f, c, model, history_tail)
            except LLMCapExceeded:
                cap_hit = True
                log.warning("of_ai_chat LLM cap reached (extract) account=%s — stopping",
                            account_id)
                break
            except Exception:
                log.debug("of_ai_chat fact-extract failed account=%s fan=%s",
                          account_id, fan_id, exc_info=True)
            # Update + push the structured nickname EVERY tick (V1 apply_nickname).
            try:
                await _maybe_push_nickname(client, account_id, fan_id, f)
            except Exception:
                log.debug("of_ai_chat nick push failed account=%s fan=%s",
                          account_id, fan_id, exc_info=True)
            try:
                asked = set(json.loads(f.questions_asked or "[]"))
            except Exception:
                asked = set()
            # If the inline fill just completed his profile, hand off to deep_convo
            # this turn (no redundant reply) — mirrors V1's analysis→complete→skip.
            if (fan_id not in force_ids and _is_info_complete(f)
                    and not _questions_still_needed(f, asked)):
                await _handoff_to_deep_convo(client, account_id, fan_id, f)
                newly_skiplisted += 1
                continue  # finally releases the lease (sent_ok stays False)
            msgs, presented = _build_messages(persona, f, c, asked, history_tail)
            try:
                res = await llm_client.chat(
                    model=model,
                    messages=msgs,
                    purpose=_PURPOSE,
                    account_id=account_id,
                    fan_id=fan_id,
                    temperature=_REPLY_TEMPERATURE,
                )
            except LLMCapExceeded:
                cap_hit = True
                log.warning("of_ai_chat daily LLM cap reached account=%s — stopping",
                            account_id)
                break
            except Exception:
                errors += 1
                log.warning("of_ai_chat generate failed account=%s fan=%s",
                            account_id, fan_id, exc_info=True)
                continue

            reply = apply_word_restriction((res.content or "").strip())[:_REPLY_MAX_CHARS]
            if not reply:
                errors += 1
                continue

            if dry_run:
                sent += 1  # would-send; persist nothing on a dry run
                continue

            try:
                result = await asyncio.to_thread(client.send_message, fan_id, reply)
            except Exception:
                errors += 1
                log.warning("of_ai_chat send failed account=%s fan=%s",
                            account_id, fan_id, exc_info=True)
                continue

            msg_id = result.get("id") if isinstance(result, dict) else None
            if msg_id:
                await write_outbound_attribution(
                    account_id=account_id,
                    fan_id=int(fan_id),
                    message_id=int(msg_id),
                    sent_by_employee_id=None,  # → system Automation employee
                    body=str(result.get("text") or reply),
                    price_cents=0,
                    created_at=ax._parse_iso(result.get("createdAt")) or datetime.utcnow(),
                    emit_live=True,  # WORKER→SSE bridge: surface the reply live
                )
                await _bump_turn_counter(account_id, fan_id, now)
                # Info-gather (V1): mark the top missing topic we just nudged him on
                # as asked, so we walk age→location→hobbies one per reply and never
                # re-ask the same thing. gen_info fills the actual fact from his answer.
                if presented:
                    await _mark_question_asked(account_id, fan_id, presented[0], asked)
                # Keep the fan's stored info current: refresh the profile from the
                # latest messages when the Fibonacci/age gate says it's stale.
                await _maybe_refresh_profile(account_id, fan_id, c.fan_msg_n, now)
                sent += 1
                sent_ok = True
                # We just replied, so the chat is "handled" — clear its unread
                # badge. Reading never marks read in V2, so a NEW fan message
                # re-marks it unread by itself. A mark-read failure must never
                # break the (already-persisted) send.
                try:
                    await asyncio.to_thread(client.mark_chat_read, fan_id)
                except Exception:
                    log.warning("of_ai_chat mark_chat_read failed account=%s fan=%s",
                                account_id, fan_id, exc_info=True)
            else:
                # 200 but no id: can't persist (message_id is the PK), so pause
                # the fan briefly — otherwise "fan spoke last" re-fires next tick
                # and we double-reply before scrape records the real send.
                await _pause_fan(account_id, fan_id, now + _NOID_PAUSE)
                errors += 1
                log.warning("of_ai_chat send returned no id account=%s fan=%s — paused %s",
                            account_id, fan_id, _NOID_PAUSE)
        finally:
            # W3 (live-chat variant): on a confirmed reply, set a SHORT cooldown
            # (_REPLY_COOLDOWN_S) and then RELEASE the lease so the next reply can
            # re-acquire once the rest elapses — keeping the 900s lease here would
            # silence the bot for 15 min. The cooldown is committed BEFORE the
            # release, so it's the cross-tick guard the moment the lease drops.
            # If the cooldown write fails we KEEP the lease (900s) as the fallback
            # double-message guard rather than release into an unguarded window.
            if sent_ok:
                try:
                    await ax.start_fan_cooldown(
                        account_id, fan_id, cooldown_s=_REPLY_COOLDOWN_S
                    )
                    await ax.release_fan_lease(account_id, fan_id)
                except Exception:
                    log.warning("of_ai_chat cooldown set failed account=%s fan=%s "
                                "— keeping lease as fallback guard",
                                account_id, fan_id, exc_info=True)
            else:
                await ax.release_fan_lease(account_id, fan_id)

    return {
        "candidates": len(candidates),
        "replies_sent": sent,
        "skipped_listed": skipped_listed,
        "skipped_not_turn": skipped_not_turn,
        "newly_skiplisted": newly_skiplisted,
        "skipped_locked": skipped_locked,
        "skipped_cooldown": skipped_cooldown,
        "errors": errors,
        "cap_hit": cap_hit,
        "dry_run": dry_run,
        "model": model,
    }
