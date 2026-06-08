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
import random
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
    STYLE_3LINE, STYLE_HUMANIZER, STYLE_MAX_BUBBLES, apply_word_restriction,
    build_facts_note, build_structured_nickname, coerce_ids, facts_from_fan,
    hold_with_typing, humanize_typos, load_style_flags, load_typing_indicator,
    load_typing_wpm, load_typo_flags, push_nick_and_notes,
    quarantine_if_undeliverable, resolve_model,
    skip_unreachable_fan, typing_delay_seconds,
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
_MAX_TURNS = 30                  # hard per-fan reply cap → hand off to deep_convo.
                                 # _MAX_FAN_MESSAGES counts the FAN's messages; this
                                 # counts OUR confirmed replies (turn_counter), so a
                                 # fan who chats forever without revealing the bio
                                 # facts still graduates instead of looping for ever.
_INFO_COMPLETE_RATIO = 0.75      # ≥ 3 of 4 bio groups filled → enough signal
_DEFAULT_LIMIT = 200             # candidate sweep ceiling
_DEFAULT_MAX_REPLIES = 25        # per-run send cap (logged when it bites)
_REPLY_TEMPERATURE = 0.85        # warm/varied, matches the legacy persona call
_HISTORY_TAIL = 20               # last N messages handed to the REPLY model. The
                                 # durable info already rides in the facts block, so
                                 # the reply doesn't need the full backlog — ~20
                                 # recent turns is plenty of context and halves the
                                 # (uncached) input vs the old 40.
_EXTRACT_HISTORY_TAIL = 8        # the fact-extract call only needs to see what he
                                 # JUST said to catch a newly-stated fact — a short
                                 # tail keeps this second per-turn call cheap.
_MSG_CLIP = 400                  # clip each message body
_REPLY_MAX_CHARS = 600           # trim a runaway generation before sending
# of_ai_chat is a LIVE chat — the generic 30-min W3 cooldown would make the bot
# go silent and feel broken. Use a SHORT rest so back-and-forth stays snappy:
# just long enough to stop a double-fire within a cycle. Unlike the other senders,
# of_ai_chat also RELEASES its lease on success (see finally) so this is the sole gate.
_REPLY_COOLDOWN_S = 10

# When of_ai_chat hands a fan to deep_convo, kick deep_convo this many seconds
# later (instead of waiting for its periodic sweep) — long enough for the
# handoff's gen_info regen to (re)build the deep questions first.
_HANDOFF_DEEPCONVO_DELAY_S = 10

# Per-message style variety — _build_messages rolls ONE of these into the prompt
# each reply so the bot doesn't always look the same (the dead giveaway: identical
# "😏 + one short line + question" every time). Mixes emoji/no-emoji, one line vs
# two tiny bubbles, punctuation on/off, and an occasional react-only (no question).
_STYLE_VARIANTS = (
    "one short line, one emoji that fits.",
    "one short line, NO emoji this time.",
    ("TWO separate texts — output them as TWO LINES with a line break between "
     "(line 1 = a 2-4 word reaction, line 2 = your line/question). Both tiny. "
     "Example:\ndamn really?\nso what's ur fave map?"),
    ("TWO separate texts on two lines (line break between) — react on line 1, "
     "then tease or ask on line 2. Keep both super short, 0-1 emoji."),
    "one short line, all lowercase, no ending punctuation — lazy/casual.",
    "one short line; if you use an emoji make it NOT 😏 (try 😈 🙈 😅 🥵) or skip it.",
    ("DON'T gush or elaborate on his last answer — a 1-2 word ack (or none), then "
     "straight to your next question. e.g. \"cool, what do u do for work?\""),
)

# Emoji (with optional variation selector) + the LLM em-dash "tell".
_EMOJI = "[\U0001F300-\U0001FAFF\U00002600-\U000027BF❤]️?"
_EMOJI_RE = re.compile(_EMOJI)
_TRAIL_EMOJI_RE = re.compile(_EMOJI + r"\s*$")
_DASH_RE = re.compile(r"\s*[—–]\s*|\s+-\s+")   # em/en dash or a spaced hyphen
_DASH_CLEAN_RE = re.compile(r"\s*[—–]\s*")     # any residual em/en dash → ", "


def _strip_dash(s: str) -> str:
    """An em/en dash never belongs in a casual text (it's a dead LLM tell). Any
    that survives a split is replaced with a comma so no bubble ever shows one."""
    return _DASH_CLEAN_RE.sub(", ", s).strip().rstrip(",").strip()


def _casual_join(parts: list[str]) -> str:
    """Glue split bubbles back into ONE casual line WITHOUT a comma after terminal
    punctuation — ", ".join gives the bot-tell 'a double shift?, ur a beast'; this
    gives 'a double shift? ur a beast'. Comma only between plain clauses."""
    out = (parts[0] or "").strip()
    for p in parts[1:]:
        p = (p or "").strip()
        if not p:
            continue
        sep = " " if out.rstrip().endswith(("?", "!", ".", "…")) else ", "
        out = out.rstrip() + sep + p
    return out


# Real texters break a thought into separate sends: a leading reaction, then the
# statement, then the question. The model usually writes it as ONE line with
# punctuation ("oof, restaurant life is brutal. how old are ya"), so in style mode
# we split it ourselves on a leading interjection + sentence boundaries → up to 3
# bubbles ("oof" / "restaurant life is brutal" / "how old are ya").
_REACTIONS = (r"oo+f|lo+l|lma+o|omg|ha+ha+|hah|mm+|mhm+|ugh|hmm+|aw+|damn|stop|oh|yo|"
              r"wait|bet|nice|fair|true|lowkey|ok|okay|yeah|yep|nah")
# Peels a lead reaction that's FUSED into a longer line ("oof, restaurant..." →
# "oof" + rest); requires trailing content, so it won't match a bare reaction.
_LEAD_REACT_RE = re.compile(rf"^({_REACTIONS})\b[,!.]*\s+", re.I)
# Matches the reaction word at the start whether or not anything follows — used to
# fingerprint an opener (incl. a standalone "oof" bubble) for repeat-detection.
_REACT_WORD_RE = re.compile(rf"^({_REACTIONS})\b", re.I)
_SENT_SPLIT_RE = re.compile(r"(?<=[.?!])\s+")


def _split_sentences(text: str, max_bubbles: int) -> list[str]:
    """Break a single-line reply into texting bubbles: peel a leading reaction word,
    then split the rest on sentence boundaries. Trailing '.' dropped (casual). Over
    `max_bubbles` → the overflow merges into the last bubble (never drop the
    question). Returns [] / [text] when there's nothing to split."""
    rest = text.strip()
    parts: list[str] = []
    m = _LEAD_REACT_RE.match(rest)
    if m:
        lead = rest[:m.end()].strip().rstrip(",.!").strip()
        if lead:
            parts.append(lead)
        rest = rest[m.end():].strip()
    for chunk in _SENT_SPLIT_RE.split(rest):
        chunk = chunk.strip()
        if chunk.endswith("."):
            chunk = chunk[:-1].strip()
        if chunk:
            parts.append(chunk)
    parts = [p for p in parts if p]
    if len(parts) > max_bubbles:                # keep everything: merge the tail
        parts = parts[:max_bubbles - 1] + [" ".join(parts[max_bubbles - 1:])]
    return parts


def _cap_one_question(parts: list[str]) -> list[str]:
    """At most ONE question across the bubbles — two questions in a reply is a bot
    tell ('whats ur job? u gonna spill?'). Keep the first question bubble; drop any
    later question-only bubble."""
    out: list[str] = []
    seen_q = False
    for p in parts:
        q = p.rstrip().endswith("?")
        if q and seen_q:
            continue
        out.append(p)
        seen_q = seen_q or q
    return out


def _react_token(s: str) -> str | None:
    """The normalized leading reaction word of a bubble, or None if it doesn't open
    with one. Repeated letters collapse so 'oof'/'oooof' and 'lol'/'lolll' map
    together (so we catch the same opener even when the model stretches it)."""
    m = _REACT_WORD_RE.match((s or "").strip())
    if not m:
        return None
    return re.sub(r"(.)\1+", r"\1", m.group(1).lower())


def _dedupe_lead_reaction(parts: list[str], recent_out: list[str]) -> list[str]:
    """Kill the 'oof every message' tell: if this reply opens with the same reaction
    word the bot just used in its last couple of sends, drop that lead — either the
    standalone reaction bubble, or the leading word when it's fused into one line.
    Never empties the reply (a lone reaction with nothing after is left alone)."""
    if not parts:
        return parts
    recent = {t for b in recent_out[-3:] if (t := _react_token(b))}
    if not recent:
        return parts
    tok = _react_token(parts[0])
    if not tok or tok not in recent:
        return parts
    if len(parts) > 1:
        return parts[1:]                              # drop the standalone "oof"
    m = _LEAD_REACT_RE.match(parts[0].strip())        # fused → strip just the opener
    rest = parts[0].strip()[m.end():].strip() if m else ""
    return [rest] if rest else parts


def split_for_bubbles(text: str, max_bubbles: int = 2) -> list[str]:
    """Make one generated reply read like real texting — up to `max_bubbles`
    bubbles (default 2; the style opt-in raises it to 3), and NO em-dash ever
    survives (every return path runs _strip_dash):

      • explicit two-line style (newline) → bubbles on the line breaks.
      • style mode (max_bubbles ≥ 3) → split a single line on a leading reaction +
        sentence boundaries ("oof, x. how old r u" → "oof"/"x"/"how old r u").
      • em-dash ("got it jack — how old are ya") → two bubbles ("got it jack" /
        "how old are ya").
      • a MID-LINE emoji → break 2/3 of the time, emoji stays on bubble 1
        ("haha noted jack 😈" / "so where ya from?") — but DROP it 1/2 of those.
      • not broken + a TRAILING emoji → drop it 1/3 of the time.
    """
    text = text.strip()
    if "\n" in text:                            # the "two tiny lines" style
        parts = [p.strip() for p in text.split("\n") if p.strip()][:max_bubbles]
        return [_strip_dash(p) for p in parts] or [_strip_dash(text)]
    if max_bubbles >= 3:                         # style mode: sentence-aware split
        parts3 = _cap_one_question(_split_sentences(text, max_bubbles))
        if len(parts3) >= 2:
            # VARY the count so it's not always a 3-way split — mix 1/2/3 like a
            # real person (sometimes one line, sometimes a couple, sometimes three).
            if len(parts3) >= 3:
                # 2 lines is the sweet spot; 3 is the rare case and 1 the breather —
                # a 3-way split every time is a bot tell, but so is never splitting.
                target = random.choices([len(parts3), 2, 1], weights=[20, 55, 25])[0]
            else:
                target = random.choices([2, 1], weights=[70, 30])[0]
            if target >= len(parts3):
                merged = parts3
            elif target == 1:
                merged = [_casual_join(parts3)]       # one casual line (no '?,' joins)
            else:                                     # keep the question as its own bubble
                merged = parts3[:target - 1] + [_casual_join(parts3[target - 1:])]
            return [_strip_dash(p) for p in merged]
    dm = _DASH_RE.search(text)
    if dm:
        before, after = text[:dm.start()].strip(), text[dm.end():].strip()
        if before and after:
            return [_strip_dash(before), _strip_dash(after)]
    if random.random() < (2.0 / 3.0):
        for m in _EMOJI_RE.finditer(text):
            after = text[m.end():].strip()
            if not after:                       # trailing emoji → not a mid-break
                continue
            before = text[:m.end()].strip()     # keep the emoji on bubble 1…
            if random.random() < 0.5:
                before = text[:m.start()].strip()   # …but drop it half the time
            if before:
                return [_strip_dash(before), _strip_dash(after)]
    if random.random() < (1.0 / 3.0):
        stripped = _TRAIL_EMOJI_RE.sub("", text).strip()
        if stripped and stripped != text:
            return [_strip_dash(stripped)]
    return [_strip_dash(text)]


_ECHO_NORM_RE = re.compile(r"[^a-z0-9]+")


def _norm_echo(s: str) -> str:
    return _ECHO_NORM_RE.sub("", (s or "").lower())


def _looks_like_echo(part: str, last_in: str) -> bool:
    """True if `part` just parrots the fan's last message (verbatim or near-so).
    A small-model degeneracy the humanizer can trigger ('FAN: x' → 'YOU: x'). The
    12-char floor keeps legit short reactions (lol / haha / u up) from tripping."""
    a, b = _norm_echo(part), _norm_echo(last_in)
    if len(a) < 12 or len(b) < 12:
        return False
    if a == b:
        return True
    lo, hi = (a, b) if len(a) <= len(b) else (b, a)
    return lo in hi and len(lo) / len(hi) >= 0.7


# Hold strong refs to delayed handoff→deep_convo wake tasks (create_task only
# keeps a weak ref, so a fire-and-forget sleeper can be GC'd mid-wait).
_deepconvo_wake_tasks: set = set()


def _schedule_deepconvo_wake(delay_s: float) -> None:
    """After a handoff, wake the supervisor once the chained deep_convo job is due
    so it doesn't wait for the next 30s tick. Never raises."""
    async def _w() -> None:
        try:
            await asyncio.sleep(delay_s)
            ax.wake_supervisor()
        except Exception:  # pragma: no cover — defensive
            pass
    t = asyncio.create_task(_w())
    _deepconvo_wake_tasks.add(t)
    t.add_done_callback(_deepconvo_wake_tasks.discard)

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
    # CORE facts (name/age/location/job): pursue if still empty, but at MOST TWICE
    # — `_mark_question_asked` escalates "X" → "X:2", so once "X:2" is set we stop
    # asking and let the fan graduate even without it (he dodged → move on). Other
    # topics are asked ONCE. The reorder below spreads re-asks out (ask a fresh
    # topic, poke a dodged one LATER — never the same question twice in a row).
    if not name and "name:2" not in asked:
        q.append(("name", "- You DON'T know his real name yet. Ask what his name is "
                  "/ what you should call him."))
    if not age and "age:2" not in asked:
        q.append(("age", "- You DON'T know his age yet. Find out how old he is."))
    if not country and not city and "location:2" not in asked:
        q.append(("location", "- You DON'T know where he lives. Ask where he's from."))
    if not country and city and "country_from_city" not in asked:
        q.append(("country_from_city",
                  f"- He mentioned {str(f.home_city).strip()} but you don't know "
                  "which country. Confirm or guess the country."))
    if country and not city and "city" not in asked:
        q.append(("city",
                  f"- You know he's from {str(f.home_country).strip()} but not which "
                  "city/area. Ask what part."))
    if not occupation and "job:2" not in asked:
        q.append(("job", "- You DON'T know what he does for work. Ask what he does for a living."))
    if not hobbies and "hobbies" not in asked:
        q.append(("hobbies", "- You DON'T know his hobbies yet. Ask what he likes to do."))
    if not events and "recent_events" not in asked:
        q.append(("recent_events", "- Ask what's new in his life lately."))
    # boobs/ass goes LAST — get the get-to-know-you facts in first; the spicy
    # question only after everything else.
    if not boobs and not ass and not fetishes and "boobs_ass" not in asked:
        q.append(("boobs_ass", "- You DON'T know what he's into (boobs/ass). Ask playfully."))
    # Poke-later: a topic asked ONCE already (in `asked`) drops to the BACK, so we
    # ask a fresh question this turn and circle back to a dodged one later — never
    # the same question two turns running. Stable sort keeps the priority order
    # within each group (boobs still last among the un-asked).
    q.sort(key=lambda kv: kv[0] in asked)
    return q


# Canonical ask priority (the order the model naturally fixates on a gap — name
# first). Used to decide WHICH topic a turn's question actually nudged, so the
# 2-ask cap escalates reliably regardless of the poke-later reorder above.
_ASK_PRIORITY = ("name", "age", "location", "country_from_city", "city",
                 "job", "hobbies", "recent_events", "boobs_ass")


def _primary_ask_target(presented: list[str]) -> str | None:
    """The topic a turn's question most likely hit: the highest-PRIORITY one still
    needed (the model fixates on the most glaring gap, esp. name) — NOT the
    reordered front. Marking THIS is what makes 'never ask a 3rd time' a hard rule:
    a still-empty core fact escalates "name"->"name:2" over two ask-turns, then
    `_questions_still_needed` drops it so the gather can complete (and gen_info
    makes the nickname itself)."""
    s = set(presented)
    for k in _ASK_PRIORITY:
        if k in s:
            return k
    return presented[0] if presented else None


def _build_messages(persona: str, f: Fan, c: _Candidate,
                    asked: set[str],
                    history_tail: int = _HISTORY_TAIL,
                    style_on: bool = False) -> tuple[list[dict], list[str]]:
    """Compose the (system, user) pair — a faithful port of V1
    prompts.create_chat_response: a short, GIRLY, 100%-human reply that flirts
    WHILE gathering the one piece of info we still need. The 'still need' block is
    driven off `_questions_still_needed` (what we already know), so early chats
    interrogate-by-flirting and complete profiles just banter. Returns
    (messages, presented_keys) so the caller can mark the asked topic and not nag.

    Persona/location/etc. are config (account_ai_config) — UI-editable later; the
    instruction scaffolding around them is this template."""
    # Present the remaining gaps as SOFT goals: the bot works one in WHEN IT FITS
    # the flow (no rigid order) so the chat feels natural, not like an interrogation.
    # Handoff still fires at the 75% info-complete bar (see _is_info_complete).
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

    # Gathering info is the PRIMARY job, so ASK most turns. Only ~1 in 7 is a
    # react-only breather (keeps it human, not an interrogation). When info is
    # already complete there's nothing left to ask.
    ask = bool(question_lines) and random.random() >= 0.14
    if not ask:
        presented = []  # nothing asked → don't mark a topic

    if not question_lines:
        need_block = "You know enough about him now — just chat and flirt naturally."
    elif ask:
        # The list is already ordered (fresh topics first, dodged ones poked
        # later), so just tell it to ask ONE naturally — don't insist or re-ask
        # the same thing he dodged; move on and circle back.
        need_block = (
            "YOUR GOAL THIS MESSAGE: find out ONE of these about him. Pick whichever "
            "flows best from what he just said and weave it in naturally — any order "
            "is fine. If he dodged something earlier, DON'T re-ask it back-to-back; "
            "just move on to another one and you can poke it again later:\n" + question_lines
        )
    else:
        need_block = (
            "THIS MESSAGE: don't ask a question — just react or tease to what he said "
            "and keep it flowing. (You'll ask next time.)"
        )

    # HARD RULE: if we already asked his name twice and he dodged, NEVER ask a
    # third time — give him a playful nickname and use it. (`_questions_still_needed`
    # has already dropped 'name' from the list; this stops the model re-asking it
    # unprompted out of fixation.)
    name_dodged = (not _nonempty(f.real_name) and "name:2" in asked)
    dodge_note = (
        "\n\nIMPORTANT: you asked his name twice and he dodged — do NOT ask his name "
        "again. Pick a playful nickname for him and just use it from now on."
        if name_dodged else "")

    # Per-message style dice so replies don't all look identical (the bot tell:
    # the same "😏 + one short line + question" every time). Controls HOW it looks,
    # not WHETHER it asks (that's `ask` above). The style opt-in adds a 3-line
    # micro-text variant (pairs with the raised bubble cap at the send site).
    # When the human-style opt-in is on, weight the 3-bubble micro-text variant
    # ~3x so genuine 3-line posts actually show up (~30% vs ~12% at 1x).
    style = random.choice(_STYLE_VARIANTS + ((STYLE_3LINE,) * 3 if style_on else ()))

    # When the "human texting style" opt-in is ON, append the humanizer block —
    # the not-AI tells (no echo-with-adjective, vary length, one question, etc.).
    humanizer = f"\n\n{STYLE_HUMANIZER}" if style_on else ""

    system = (
        f"{persona}\n"
        "You're texting a fan to get to know him while flirting. You are 100% human "
        "— never sound like a bot or an assistant. Use only what you've learned "
        "about him; don't share your own info unless he asks; don't offer pics or "
        "videos yet. He may send several texts in a row — read them all, reply to "
        "the latest.\n\n"
        f"{need_block}{dodge_note}\n\n"
        f"STYLE FOR THIS MESSAGE — {style}\n\n"
        "HOW YOU TEXT (a real 22yo girl, not an assistant):\n"
        "- Short and casual. lowercase, contractions, u/ur/ya. React to what he "
        "said in a few words first.\n"
        "- VARY it every time — don't open the same way twice, and don't reuse a "
        "phrase or an emoji you've already used in this chat.\n"
        "- At most ONE question, never one he already answered (if his answer was "
        "vague, ask a quick follow-up instead of re-asking). Don't narrate, no "
        "paragraphs.\n"
        "- If he gets explicit early or asks for nudes/pics: don't go along with it "
        "and don't offer anything — playfully tease and slow it down, then steer "
        "back to getting to know him. Warm and flirty, never cold or preachy.\n"
        "- GOOD: \"haha thanks 😏 what should i call u?\" / \"aww how old are ya\" / "
        "\"a chef? bet u cook fire, fave dish?\""
        f"{humanizer}\n\n"
        "Your reply is ONLY the message text — no JSON, quotes, or metadata."
    )
    user = (
        f"What you know about him:\n{facts_block}\n\n"
        f"Recent conversation (oldest→newest):\n{convo}\n\n"
        "Reply to his last message now, in the STYLE FOR THIS MESSAGE above."
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
                      history_tail: int = _EXTRACT_HISTORY_TAIL) -> list[dict]:
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
                            history_tail: int = _EXTRACT_HISTORY_TAIL) -> Fan:
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


async def _reset_deep_convo_backoff(account_id: str, fan_id: int) -> None:
    """Zero a fan's deep_convo backoff so they arrive at deep_convo CLEAN. A fan
    handed off from of_ai_chat is actively chatting RIGHT NOW — they must never
    inherit a stale `deep_convo_skip_level/remaining` (e.g. from an earlier drift,
    or from messages being temporarily invisible), which would make deep_convo
    skip them for several cycles instead of replying. UPSERT for the no-Fan-row
    case, mirroring _mark_question_asked."""
    now = datetime.utcnow()
    cleared = {"deep_convo_skip_level": 0, "deep_convo_skip_remaining": 0,
               "updated_at": now}
    async with get_session() as s:
        await s.execute(
            sqlite_insert(Fan)
            .values(account_id=str(account_id), fan_id=int(fan_id), **cleared)
            .on_conflict_do_update(
                index_elements=["account_id", "fan_id"], set_=cleared)
        )


async def _handoff_to_deep_convo(client, account_id: str, fan_id: int, f: Fan) -> None:
    """End of the normal chat: skip-list the fan ('info'), clear any stale
    deep_convo backoff (they're chatting now — deep_convo must engage promptly,
    not skip them), enqueue a gen_info regen, and push the FINAL structured
    nickname + fact-sheet NOTE to OF (notes land here, at the end of the AI convo)."""
    await _add_to_skip_list(account_id, fan_id, "info")
    await _reset_deep_convo_backoff(account_id, fan_id)
    await ax.enqueue_job(account_id, "gen_info", payload={"force_ids": [int(fan_id)]})
    # Kick deep_convo right after the gen_info regen instead of waiting for the
    # slow periodic sweep — the fan just finished gathering and is sitting on an
    # unanswered turn. The short delay lets gen_info (re)build the Q/Teases first;
    # if it isn't done, deep_convo falls back to the existing profile Q.
    await ax.enqueue_job(
        account_id, "deep_convo", payload={"only_fan_ids": [int(fan_id)]},
        run_at=datetime.utcnow() + timedelta(seconds=_HANDOFF_DEEPCONVO_DELAY_S),
    )
    _schedule_deepconvo_wake(_HANDOFF_DEEPCONVO_DELAY_S)
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
    """Record that we asked about `key` (V1 QuestionsAsked). ESCALATING: the first
    ask marks "key"; a second ask (key already present) marks "key:2" — that's the
    2-ask cap for core facts (_questions_still_needed drops a topic once "key:2" is
    set). UPSERT (not UPDATE): a candidate sourced from `messages` may have no Fan
    row yet — a bare UPDATE would match 0 rows and silently lose the tracker."""
    mark = key if key not in asked else f"{key}:2"
    new_set = sorted(asked | {mark})
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


async def _bump_attempt(account_id: str, fan_id: int, now: datetime) -> None:
    """Count one of_ai_chat turn — bumped when a reply is GENERATED, whether or
    not it lands (so the _MAX_TURNS cap counts attempts, not just confirmed
    sends; an undeliverable fan must still graduate). NOT last_message_sent_at —
    that's only set on a real send (see _mark_reply_sent), else a never-delivered
    reply would corrupt 'last sent'. UPSERT (not UPDATE): candidates come from the
    messages table, so an eligible fan may have no Fan row yet on its first turn —
    a plain UPDATE would no-op and lose the count."""
    async with get_session() as s:
        await s.execute(
            sqlite_insert(Fan)
            .values(account_id=str(account_id), fan_id=int(fan_id),
                    turn_counter=1, updated_at=now)
            .on_conflict_do_update(
                index_elements=["account_id", "fan_id"],
                set_={"turn_counter": Fan.turn_counter + 1, "updated_at": now},
            )
        )


async def _mark_reply_sent(account_id: str, fan_id: int, now: datetime) -> None:
    """Stamp last_message_sent_at on a CONFIRMED send. The turn itself was already
    counted by _bump_attempt when the reply was generated."""
    async with get_session() as s:
        await s.execute(
            update(Fan)
            .where(Fan.account_id == str(account_id), Fan.fan_id == int(fan_id))
            .values(last_message_sent_at=now, updated_at=now)
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
    history_tail = int(payload.get("history_tail") or _HISTORY_TAIL)  # reply tail
    force_ids = coerce_ids(payload.get("force_ids"))
    # W7 fan-scope: react to ONLY the fan(s) who just messaged (no full-account
    # sweep). Unlike force_ids this does NOT bypass the gates — a scoped fan still
    # gets the spend/too_long/info checks; it only limits the candidate set.
    only_fan_ids = coerce_ids(payload.get("only_fan_ids"))

    model = await resolve_model(account_id, _PURPOSE, payload.get("model"))
    typing_wpm = await load_typing_wpm(account_id)  # per-bubble "typing" pacing
    typing_indicator = await load_typing_indicator(account_id)  # live "...is typing"
    style_on = (await load_style_flags(account_id))[_PURPOSE]  # human-style opt-in
    typo_on = (await load_typo_flags(account_id))[_PURPOSE]    # thumb-typo opt-in
    max_bubbles = STYLE_MAX_BUBBLES if style_on else 2
    persona = await _load_persona(account_id)
    blacklist, skip_list = await _load_stop_lists(account_id)
    mid_funnel_fans = await _load_mid_funnel_fans(account_id)  # W7 cross-tick ownership
    by_fan = await _gather(account_id)
    if only_fan_ids:
        by_fan = {fid: c for fid, c in by_fan.items() if fid in only_fan_ids}

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
    skipped_spam = 0        # promo-spam: $0 spend + creator_we_follow (peer creator)
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
            # Promo-spam guard: peer creators who blast the inbox (mutual-promo
            # spam — the jaka problem). `source == "creator_we_follow"` means WE
            # subscribed to THEM (subscribedBy), i.e. they're a creator, never a
            # fan of ours. Live data: real fans are NEVER this value, while ~3/4 of
            # jaka's stranger inbound is. Gated on $0 spend so a paying collab is
            # never silenced. REVERSIBLE skip (no skip_list / gen_info write) — if
            # they ever spend, the next tick reconsiders them.
            if (
                int(f.lifetime_spend_cents or 0) == 0
                and (f.source or "") == "creator_we_follow"
            ):
                skipped_spam += 1
                continue
            if int(f.lifetime_spend_cents or 0) > _SPEND_GATE_CENTS:
                await _skip_and_collect(account_id, fan_id, "spent")
                newly_skiplisted += 1
                continue
            if c.fan_msg_n >= _MAX_FAN_MESSAGES:
                await _skip_and_collect(account_id, fan_id, "too_long")
                newly_skiplisted += 1
                continue
            # Hard reply cap: after _MAX_TURNS confirmed replies, graduate to
            # deep_convo regardless of info-completeness — a fan who never reveals
            # the bio facts (dodges every question) otherwise stays here for ever.
            if int(f.turn_counter or 0) >= _MAX_TURNS:
                await _handoff_to_deep_convo(client, account_id, fan_id, f)
                newly_skiplisted += 1
                continue
            # Hand off to deep_convo once there's NOTHING LEFT TO ASK — every topic
            # is either filled (enough info) or has hit its ask cap (he dodged).
            # That's the "go to deep convo if enough info or dodged" rule: we don't
            # keep a fan in the gather forever, but we DO ask each thing (core facts
            # up to 2x) before moving on. Then (re)generate the Q/Teases.
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
                # Extract sees only the last few turns (a newly-stated fact is
                # always recent) — its own short tail, NOT the reply's.
                f = await _extract_and_fill(account_id, fan_id, f, c, model,
                                            _EXTRACT_HISTORY_TAIL)
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
            # If the inline fill left NOTHING more to ask (all filled or asked to
            # cap), hand off to deep_convo this turn (no redundant reply).
            if (fan_id not in force_ids
                    and not _questions_still_needed(f, asked)):
                await _handoff_to_deep_convo(client, account_id, fan_id, f)
                newly_skiplisted += 1
                continue  # finally releases the lease (sent_ok stays False)
            msgs, presented = _build_messages(persona, f, c, asked, history_tail,
                                              style_on=style_on)
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

            raw = (res.content or "").strip()
            # Count the turn the moment a reply is GENERATED — a "try" counts
            # whether or not it lands. An undeliverable fan (no-id) or an echo-only
            # drop never bumps via the confirmed-send path, so its turn_counter
            # would stay 0 forever and the _MAX_TURNS cap could never fire. Bumping
            # here makes the cap count attempts, so a fan we keep failing to send to
            # still hits 30 and graduates to deep_convo instead of looping for ever.
            if not dry_run:
                await _bump_attempt(account_id, fan_id, now)
            # Up to `max_bubbles` bubbles (newline / em-dash / mid-emoji); em-dashes
            # are always cleaned out. Cap (2, or 3 with the style opt-in) so we
            # never spam.
            parts = [apply_word_restriction(p)[:_REPLY_MAX_CHARS]
                     for p in split_for_bubbles(raw, max_bubbles) if p.strip()][:max_bubbles]
            # Drop any bubble that just parrots his last message (small-model echo
            # tell the humanizer can trigger). If the whole reply was an echo, skip.
            parts = [p for p in parts if not _looks_like_echo(p, c.last_body)]
            # Don't open with the SAME reaction word two sends running ('oof' every
            # message). Look at our recent outbound bubbles and strip a repeat lead.
            if style_on and parts:
                recent_out = [b for d, b in c.messages if d == "out"]
                parts = _dedupe_lead_reaction(parts, recent_out)
            if not parts:
                errors += 1
                log.debug("of_ai_chat dropped echo-only reply account=%s fan=%s",
                          account_id, fan_id)
                continue
            if typo_on:  # opt-in: a realistic thumb-slip (+ maybe a "*fix" bubble)
                protect = [n for n in (f.real_name, f.generated_nickname,
                                       f.of_display_name) if n]
                parts = humanize_typos(parts, random.Random(f"{fan_id}:{raw}"),
                                       protect=protect, max_bubbles=max_bubbles)[:max_bubbles]

            if dry_run:
                sent += 1  # would-send; persist nothing on a dry run
                continue

            send_failed = False
            first_no_id = False
            for idx, part in enumerate(parts):
                # Hold each bubble for the time it'd take to TYPE it (~typing_wpm),
                # showing the live "...is typing" bubble during the wait if enabled.
                await hold_with_typing(account_id, fan_id,
                                       typing_delay_seconds(part, typing_wpm),
                                       typing_indicator=typing_indicator)
                try:
                    result = await asyncio.to_thread(client.send_message, fan_id, part)
                except Exception as e:
                    errors += 1
                    await skip_unreachable_fan(account_id, fan_id, e, log=log)
                    log.warning("of_ai_chat send failed account=%s fan=%s",
                                account_id, fan_id, exc_info=True)
                    send_failed = True
                    break
                msg_id = result.get("id") if isinstance(result, dict) else None
                if msg_id:
                    await write_outbound_attribution(
                        account_id=account_id,
                        fan_id=int(fan_id),
                        message_id=int(msg_id),
                        sent_by_employee_id=None,  # → system Automation employee
                        body=str(result.get("text") or part),
                        price_cents=0,
                        created_at=ax._parse_iso(result.get("createdAt")) or datetime.utcnow(),
                        emit_live=True,  # WORKER→SSE bridge: surface the reply live
                    )
                    sent_ok = True
                elif idx == 0:
                    first_no_id = True  # nothing landed → pause + retry next tick
                    break
            if send_failed:
                continue
            if first_no_id and not sent_ok:
                # 200 but no id on the first bubble: can't persist (message_id is the
                # PK). Probe WHY first — an undeliverable fan (lapsed sub / deleted)
                # would otherwise re-fire ('fan spoke last') every tick forever,
                # burning a reply we can never deliver. quarantine_if_undeliverable
                # rests/skips those; only a genuinely transient no-id falls through
                # to the short pause (which also stops the immediate double-reply).
                errors += 1
                reason = await quarantine_if_undeliverable(client, account_id, fan_id)
                if reason is None:
                    await _pause_fan(account_id, fan_id, now + _NOID_PAUSE)
                    log.warning("of_ai_chat send returned no id account=%s fan=%s — paused %s",
                                account_id, fan_id, _NOID_PAUSE)
                continue
            # At least one bubble landed → stamp the send (the turn was already
            # counted by _bump_attempt when the reply was generated).
            await _mark_reply_sent(account_id, fan_id, now)
            # Info-gather (V1): mark the topic we nudged him on as asked so we never
            # re-ask it. gen_info fills the actual fact from his answer. (Empty on a
            # no-question style roll → nothing marked.) HARD RULE: mark the highest-
            # PRIORITY still-needed topic (what the model actually fixates on), not
            # the poke-later-reordered front — that's what makes a still-empty core
            # fact escalate to ":2" and DROP after two asks (never a 3rd time), so a
            # dodgy fan graduates to deep_convo with a self-generated nickname.
            target = _primary_ask_target(presented)
            if target:
                await _mark_question_asked(account_id, fan_id, target, asked)
            # Keep stored info current: refresh the profile when the gate says stale.
            await _maybe_refresh_profile(account_id, fan_id, c.fan_msg_n, now)
            sent += 1
            # We just replied → clear the unread badge. A mark-read failure must
            # never break the (already-persisted) send.
            try:
                await asyncio.to_thread(client.mark_chat_read, fan_id)
            except Exception:
                log.warning("of_ai_chat mark_chat_read failed account=%s fan=%s",
                            account_id, fan_id, exc_info=True)
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
        "skipped_spam": skipped_spam,
        "newly_skiplisted": newly_skiplisted,
        "skipped_locked": skipped_locked,
        "skipped_cooldown": skipped_cooldown,
        "errors": errors,
        "cap_hit": cap_hit,
        "dry_run": dry_run,
        "model": model,
    }
