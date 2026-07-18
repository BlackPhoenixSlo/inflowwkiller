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
    LIVE_PROOF_GUARDRAIL, ONPLATFORM_GUARDRAIL, guard_offplatform,
    STYLE_3LINE, STYLE_BRIEF, STYLE_HUMANIZER, STYLE_MAX_BUBBLES,
    NONNATIVE_OUTPUTS, NONNATIVE_REGISTER, apply_nonnative_style, apply_word_restriction,
    build_facts_note, build_structured_nickname, build_tip_ask_block, coerce_ids,
    facts_from_fan, hold_with_typing, apply_typo_throttle, is_content_ask,
    is_substantive_msg,
    load_nonnative_flags, load_strip_emojis, load_style_flags, load_tip_ask_config,
    load_typing_indicator, load_typing_wpm, load_typo_flags, push_nick_and_notes,
    quarantine_if_undeliverable, resolve_fan_name, resolve_model,
    should_skip_muted_creator, skip_unreachable_fan, strip_emojis,
    typing_delay_seconds,
)
from . import gen_info  # profile_is_stale() — the refresh-if-stale hook below

# A 200-from-OF without a message id can't be persisted (message_id is the PK),
# so eligibility ("fan spoke last") would re-fire next tick → double-reply.
# Pause the fan briefly so scrape_chats can record the real send first.
_NOID_PAUSE = timedelta(hours=1)

log = logging.getLogger("of-relay.automation.of_ai_chat")

# ── Knobs (ported from 05_of_ai_chat.md) ─────────────────────────────
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

# When we deliberately SKIP the question this turn (a "breather"), vary HOW it reads
# so the no-question turns don't all look alike either — same anti-template logic as
# _STYLE_VARIANTS. One is rolled per breather. (A breather that ANSWERS a question he
# just asked is handled separately — see _build_messages.)
_BREATHER_VARIANTS = (
    "THIS MESSAGE: don't ask anything — just react to what he said in a few words "
    "and let it breathe. (You'll ask next time.)",
    "THIS MESSAGE: don't ask anything — tease him or be a lil bratty about what he "
    "said, playful not mean. (You'll ask next time.)",
    "THIS MESSAGE: don't ask anything — react, then add a small thought of your own "
    "to keep it flowing. (You'll ask next time.)",
)

# Emoji (with optional variation selector) + the LLM em-dash "tell".
_EMOJI = "[\U0001F300-\U0001FAFF\U00002600-\U000027BF❤]️?"
_EMOJI_RE = re.compile(_EMOJI)
_TRAIL_EMOJI_RE = re.compile(_EMOJI + r"\s*$")
_DASH_RE = re.compile(r"\s*[—–]\s*|\s*--\s*|\s+-\s+")  # em/en dash, --, spaced hyphen
_DASH_CLEAN_RE = re.compile(r"\s*[—–]\s*")     # any residual em/en dash → ", "


def _strip_dash(s: str) -> str:
    """An em/en dash never belongs in a casual text (it's a dead LLM tell). Any
    that survives a split is replaced with a comma so no bubble ever shows one."""
    return _DASH_CLEAN_RE.sub(", ", s).strip().rstrip(",").strip()


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
# Girl-style bubble breaks: a sentence boundary (the .?! is KEPT on the chunk), OR
# a comma / dash-family marker where a real texter would hit send — the comma or
# dash itself is CONSUMED so it never shows inside a bubble. Per the style spec
# every comma is a bubble break ("no, i dont think so" → "no" / "i dont think so").
_SENT_SPLIT_RE = re.compile(r"(?<=[.?!])\s+|\s*,\s*|\s*[—–]\s*|\s*--\s*|\s+-\s+")


# When a chunk is STILL long after the comma/sentence split, break it further at a
# mid emoji (the emoji is KEPT on the left bubble) or before a WH question word that
# has enough lead-in. Short chunks pass through untouched (don't over-split a tiny
# reply). "if the text is long, split at ? or emoji or when/who/what …"
_WH_RE = re.compile(r"\b(what|whats|where|when|who|why|which|how)\b", re.I)
_LONG_CHUNK = 40           # only emoji/WH-split a chunk longer than this many chars
_WH_MIN_LEAD_WORDS = 4     # …and only before a WH-word with ≥ this many words before it


def _split_emoji_wh(seg: str) -> list[str]:
    """Break a LONG segment further: after a mid emoji (emoji KEPT on the left), or
    before a WH question word with ≥ _WH_MIN_LEAD_WORDS words of lead-in. Recurses on
    the remainder. A short segment — or one with no such break — passes through."""
    seg = seg.strip()
    if not seg:
        return []
    if len(seg) <= _LONG_CHUNK:
        return [seg]
    for m in _EMOJI_RE.finditer(seg):           # emoji with content after → split after it
        before, after = seg[:m.end()].strip(), seg[m.end():].strip()
        if before and after:
            return [before] + _split_emoji_wh(after)
    for m in _WH_RE.finditer(seg):              # a WH-word that opens a fresh clause
        if m.start() == 0:
            continue
        before, after = seg[:m.start()].strip().rstrip(","), seg[m.start():].strip()
        if before and after and len(before.split()) >= _WH_MIN_LEAD_WORDS:
            return [before] + _split_emoji_wh(after)
    return [seg]


def _split_units(text: str) -> list[str]:
    """Break ONE line into texting bubbles: split on every comma + sentence boundary
    + dash (comma/dash CONSUMED, .?! KEPT), then break any still-LONG chunk further
    at a mid emoji or WH word. A trailing '.' is dropped (casual). NO cap here — the
    caller caps and merges overflow so nothing is ever dropped."""
    out: list[str] = []
    for chunk in _SENT_SPLIT_RE.split(text.strip()):
        for sub in _split_emoji_wh(chunk):
            if sub.endswith("."):
                sub = sub[:-1].strip()
            if sub:
                out.append(sub)
    return out


def _cap_merge(parts: list[str], max_bubbles: int) -> list[str]:
    """Cap the bubble count at max_bubbles WITHOUT dropping content — any overflow
    merges into the last bubble."""
    if len(parts) > max_bubbles:
        return parts[:max_bubbles - 1] + [" ".join(parts[max_bubbles - 1:])]
    return parts


_HAS_WORD_RE = re.compile(r"[A-Za-z0-9]")


def _merge_orphans(parts: list[str]) -> list[str]:
    """A bubble with NO letters/digits (a stranded emoji or bare punctuation, e.g. a
    trailing '😏' the sentence split peeled off after a '?') isn't a real text — glue
    it onto the END of the previous bubble so we never send a lone '😏' bubble."""
    out: list[str] = []
    for p in parts:
        if out and not _HAS_WORD_RE.search(p):
            out[-1] = (out[-1] + " " + p).strip()
        else:
            out.append(p)
    return out


def _casual_join(parts: list[str]) -> str:
    """Glue bubbles back into ONE casual line: a space after terminal punctuation
    (so '...bbq? bold claim' not '...bbq?, bold claim'), a comma between plain
    clauses (so a trailing vocative reads 'how old are you, jack')."""
    out = (parts[0] or "").strip()
    for p in parts[1:]:
        p = (p or "").strip()
        if not p:
            continue
        sep = " " if out.rstrip().endswith(("?", "!", ".", "…")) else ", "
        out = out.rstrip() + sep + p
    return out


def _target_bubbles(words: int) -> int:
    """How many bubbles a reply of `words` words SHOULD become — short stays on one
    line, longer earns more, but it never explodes (13 words is 2-3 lines, NOT 5).
    A real texter sends ~one short line per handful of words: 3 → 1, 5 → 2, 13 → 3."""
    if words <= 4:
        return 1
    if words <= 9:
        return 2
    if words <= 15:
        return 3
    return 4


def _group_to_target(units: list[str], target: int) -> list[str]:
    """Merge the fine-grained `units` (every comma / clause break) down to `target`
    bubbles by grouping CONTIGUOUS units into roughly equal runs — the extra units go
    to the LAST groups, so a trailing vocative ('where are you from' + 'jack?') rejoins
    instead of stranding. Joined casually (comma between clauses). Never drops content."""
    n = len(units)
    if target <= 1:
        return [_casual_join(units)] if units else []
    if target >= n:
        return units
    base, rem = divmod(n, target)
    sizes = [base] * (target - rem) + [base + 1] * rem   # bigger groups at the end
    out, i = [], 0
    for s in sizes:
        out.append(_casual_join(units[i:i + s]))
        i += s
    return out


# Per-reply split STRATEGY — a randomiser so replies don't all chop the same way:
#   length : bubble count tracks word count (the sweet spot; see _target_bubbles).
#   clause : split at (nearly) every comma / clause — a rapid multi-text burst.
#   terse  : one or two bubbles — a short, clipped reply.
# Weighted toward `length`; `clause`/`terse` add variety. Seeded per reply (off the
# text) so a given reply is stable, but different replies vary.
_SPLIT_STRATEGIES = ("length", "clause", "terse")
_SPLIT_WEIGHTS = (58, 22, 20)


def _strategy_target(parts: list[str], max_bubbles: int, rng) -> int:
    """Roll a split strategy and return the bubble-count target for it (clamped to
    [1, min(max_bubbles, len(parts))] — never invents a break, never drops one)."""
    n = max(1, len(parts))
    strat = rng.choices(_SPLIT_STRATEGIES, weights=_SPLIT_WEIGHTS)[0]
    if strat == "clause":                 # split at (nearly) every clause/comma
        target = n
    elif strat == "terse":                # a short, clipped reply
        target = min(2, n)
    else:                                 # length: count tracks word count (default)
        target = _target_bubbles(sum(len(p.split()) for p in parts))
    return max(1, min(target, max_bubbles, n))


def _split_sentences(text: str, max_bubbles: int) -> list[str]:
    """One line → bubbles (see _split_units), capped at max_bubbles (overflow merges
    into the last; nothing dropped). The single-line entry point used in tests;
    split_for_bubbles walks newline-separated lines through _split_units directly."""
    return _cap_merge(_merge_orphans(_split_units(text)), max_bubbles)


_QMARK_TAIL_RE = re.compile(r"\s*\?+\s*$")


def _cap_one_question(parts: list[str]) -> list[str]:
    """At most ONE question MARK across the bubbles — two '?' in a reply is a bot
    tell ('whats ur job? u gonna spill?'). Keep the '?' on the LAST question bubble
    (usually the real ask) and strip it from any earlier question bubble. NEVER drop
    a bubble — that loses the message: 'oh yeah? ... how old r u?' keeps both bubbles,
    only the last stays a question ('oh yeah' / '... how old r u?')."""
    q_idx = [i for i, p in enumerate(parts) if p.rstrip().endswith("?")]
    if len(q_idx) <= 1:
        return parts
    keep = q_idx[-1]
    return [(_QMARK_TAIL_RE.sub("", p) if (i in q_idx and i != keep) else p)
            for i, p in enumerate(parts)]


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


def split_for_bubbles(text: str, max_bubbles: int = 2, rng=None) -> list[str]:
    """Make one generated reply read like real texting, and NO em-dash ever survives
    (every return path runs _strip_dash). Content is NEVER dropped:

      • style mode (max_bubbles ≥ 3) → walk the model's OWN line breaks, then split
        each line on commas + sentence boundaries + dash + (for a long chunk) emoji /
        WH word. A per-reply STRATEGY randomiser (_strategy_target) then picks the
        bubble count — length-based / split-every-clause / terse — so replies don't
        all chop the same way. Pass `rng` (seeded off the reply) for a stable result.
      • non-style (cap 2) → the model's two-line style, else a single dash / a
        MID-LINE emoji break (2/3 of the time), else a trailing-emoji drop (1/3).
    """
    _rng = rng if rng is not None else random
    text = text.strip()
    if max_bubbles >= 3:                         # STYLE MODE
        # Gather every candidate break (line breaks → commas/sentences/dash → long
        # chunk at emoji/WH), de-mark extra '?', then group to the randomiser's
        # target bubble count (nothing dropped — overflow merges into the last).
        parts: list[str] = []
        for line in text.split("\n"):
            parts.extend(_split_units(line))
        parts = _cap_one_question(_merge_orphans(parts))
        parts = _group_to_target(parts, _strategy_target(parts, max_bubbles, _rng))
        if parts:
            return [_strip_dash(p) for p in parts]
    if "\n" in text:                            # non-style: the model's two-line style
        parts = [p.strip() for p in text.split("\n") if p.strip()][:max_bubbles]
        return [_strip_dash(p) for p in parts] or [_strip_dash(text)]
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


async def _gather(account_id: str,
                  fan_ids: set[int] | None = None) -> dict[int, _Candidate]:
    """One pass over the account's messages → per-fan inbound count, last
    message, and trimmed history.

    When `fan_ids` is given (W7 fan-scoped dispatch — react to just the fan who
    messaged), the scan is restricted to those fans IN SQL, so reacting to one
    inbound DM never reads the whole account's message history. None/empty → the
    full-account sweep (the periodic run)."""
    out: dict[int, _Candidate] = {}
    where = [Message.account_id == str(account_id), Message.is_unsent.is_(False)]
    if fan_ids:
        where.append(Message.fan_id.in_(fan_ids))
    async with get_session() as s:
        rows = (await s.execute(
            select(Message.fan_id, Message.direction, Message.body)
            .where(*where)
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
        # Count only substantive inbound toward the runaway-loop cap: emoji-only
        # reactions are not a real message turn (see is_substantive_msg). gen_info
        # counts the same way, so the staleness baseline stays on one scale.
        if direction == "in" and is_substantive_msg(text):
            c.fan_msg_n += 1
    return out


def _questions_still_needed(f: Fan, asked: set[str]) -> list[tuple[str, str]]:
    """The dynamic 'INFORMATION YOU STILL NEED' list — ported verbatim in spirit
    from V1 message_generator._build_questions_to_ask. The prompt CHANGES based on
    what we already know: each gap becomes ONE natural question, ordered by
    priority, skipping any topic already in `asked` so the bot never nags. The
    'save' side is gen_info (it extracts these same fields from the convo)."""
    # A durable name is real_name OR a team/generated custom_nickname — either one
    # means we already know what to call him, so the name gap is CLOSED (keeps this
    # in step with the sticky-name guard, which uses the same OR).
    name = _nonempty(f.real_name) or _nonempty(f.custom_nickname)
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


# Colorful, job-specific riffs for the grounded occupation example (flavor B):
# keyed on a lowercased occupation, generic fallback otherwise. Every riff rides
# on his REAL job — nothing invented — so "a chef? bet u cook fire" only ever
# reaches a fan we actually know is a chef.
_JOB_RIFFS = {
    "chef": "bet u cook fire, fave dish?",
    "cook": "bet u cook fire, fave dish?",
    "nurse": "bet u take good care of everyone",
    "teacher": "bet the class has a crush on u",
    "pilot": "bet uve got the best stories",
    "cop": "ooh someone who likes bein in charge",
    "police": "ooh someone who likes bein in charge",
    "firefighter": "so u run into fires huh, brave",
    "lawyer": "bet u always win the argument",
    "engineer": "brains huh, i like that",
    "doctor": "bet u know exactly what ur doin",
    "trucker": "all those miles alone, bet u get bored",
    "driver": "all those miles, bet u get bored",
}


def _good_examples(f: Fan, asked: set[str], have_durable_name: bool) -> str:
    """The "- GOOD:" few-shot is tailored to THIS fan, two flavors combined:

    (A) FACT-FREE asks for info we still NEED — generic phrasings that plant no
        specific answer, so the model can't leak an invented detail (the "a chef?"
        bug: it demonstrated a job he never mentioned, priming the model to guess
        it). Name-ask gated on the durable signal (custom_nickname OR real_name),
        matching the sticky-name guard.
    (B) GROUNDED riffs built from facts we ALREADY hold — the same flirty
        guess-and-riff style, but every detail is TRUE (his real job/city/age/
        hobby), so even copied verbatim it invents nothing.

    Lead with what we know (B), then ask for what's missing (A)."""
    needed = {k for k, _ in _questions_still_needed(f, asked)}

    # (A) fact-free asks for what's MISSING
    ask_ex: list[str] = []
    if not have_durable_name and "name" in needed:
        ask_ex.append('"haha thanks what should i call u?"')
    if "age" in needed:
        ask_ex.append('"aww how old are ya"')
    if "job" in needed:
        ask_ex.append('"so what do u do for work?"')
    if needed & {"location", "city", "country_from_city"}:
        ask_ex.append('"where u textin me from"')

    # (B) riffs grounded in real facts — never invents anything (clip guards a
    # runaway free-text field from bloating the prompt).
    def _clip(v) -> str:
        return str(v).strip()[:30]
    known_ex: list[str] = []
    if _nonempty(f.occupation):
        _job = _clip(f.occupation).lower()
        known_ex.append(f'"a {_job}? {_JOB_RIFFS.get(_job, "bet that keeps u busy")}"')
    if _nonempty(f.home_city):
        known_ex.append(f'"{_clip(f.home_city)}? ive always wanted to go there"')
    if _nonempty(f.his_age):
        known_ex.append(f'"{_clip(f.his_age)} and still trouble huh"')
    if _nonempty(f.hobbies):
        known_ex.append(f'"still into {_clip(f.hobbies).lower()}? love that about u"')

    # Mix: up to 2 grounded riffs THEN up to 2 asks — the model sees both flavors.
    ex = known_ex[:2] + ask_ex[:2]
    # Never empty, and never demonstrate asking something we already know.
    if not ex:
        ex = ['"mmm tell me more"', '"u always know what to say"',
              '"ok thats actually hot"']
    return "- GOOD: " + " / ".join(ex[:4]) + "\n"


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


def _turn_asked(bubbles: list[str]) -> bool:
    """A bot turn 'asked' if any of its bubbles ends with a question mark (a
    trailing emoji/space is ignored: 'how old r u? 😏' still counts)."""
    for b in bubbles:
        if _TRAIL_EMOJI_RE.sub("", b or "").rstrip().endswith("?"):
            return True
    return False


def _recent_ask_pattern(history: list[tuple[str, str]]) -> tuple[int, bool]:
    """From the (direction, body) tail, group contiguous bot ('out') runs into turns
    and return (trailing consecutive ASKING turns, last-bot-turn-was-a-breather).
    Lets the dice smooth itself: never interrogate (3+ asks in a row) and never two
    breathers back-to-back — both read robotic. Memoryless coin flips don't."""
    turns: list[bool] = []          # one bool per bot turn: did it end on a question?
    run: list[str] = []
    for d, b in history:
        if d == "out":
            run.append(b)
        elif run:
            turns.append(_turn_asked(run))
            run = []
    if run:
        turns.append(_turn_asked(run))
    if not turns:
        return 0, False
    streak = 0
    for asked_q in reversed(turns):
        if not asked_q:
            break
        streak += 1
    return streak, (not turns[-1])


def _build_messages(persona: str, f: Fan, c: _Candidate,
                    asked: set[str],
                    history_tail: int = _HISTORY_TAIL,
                    style_on: bool = False,
                    nonnative_on: bool = False,
                    content_ask: bool = False,
                    tip_ask_block: str = "") -> tuple[list[dict], list[str]]:
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
    name = resolve_fan_name(f)
    # A DURABLE name (team nickname OR extracted real_name) — either one is enough;
    # drives the GOOD-examples gate so we never demonstrate a name-ask for a fan we
    # already have a name for.
    have_durable_name = bool(name) and (_nonempty(f.custom_nickname)
                                        or _nonempty(f.real_name))
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

    # Gathering info is the PRIMARY job, so ASK most turns, but DON'T interrogate.
    # The ask/breather decision is a smoothed dice, not a memoryless coin: it reads
    # the recent turn pattern and the fan's last message so the bot never asks 3
    # turns straight, never volleys a question back right after HE asked one, and
    # naturally drifts interview -> banter as the gap list empties. When info is
    # already complete there's nothing left to ask.
    fan_run: list[str] = []
    for d, b in reversed(history):
        if d == "in" and b:
            fan_run.append(b)
        elif fan_run:
            break
    fan_last_text = " ".join(reversed(fan_run))
    fan_just_asked = "?" in fan_last_text                     # he asked us something
    fan_low_effort = len(fan_last_text.split()) <= 3 and not fan_just_asked
    ask_streak, last_breather = _recent_ask_pattern(history)
    # #3 taper: as the remaining gaps empty out, lean harder toward breathers so the
    # chat winds down from interview to banter instead of clinging to the last gap.
    breather_p = 0.55 if len(presented) <= 2 else 0.33

    # He's asking to SEE content RIGHT NOW (CONTENT_ASK_RE) and we have a tip-ask
    # to make: the gather goal yields — this message asks him to tip (in voice),
    # not another interview question. (of_ai_chat itself never sells PPV; the
    # content is delivered by tip_reward once he tips.)
    selling = content_ask and bool(tip_ask_block)

    ask = bool(question_lines) and not selling
    if ask:
        if ask_streak >= 3:
            ask = False                       # #1 never 3 asks in a row (interrogation)
        elif fan_just_asked:
            ask = False                       # #2 answer him, don't volley a Q back
        elif last_breather:
            ask = True                        # #1 never two breathers back-to-back
        elif fan_low_effort:
            ask = random.random() >= 0.6      # #2 low-effort msg -> lean breather/tease
        else:
            ask = random.random() >= breather_p   # #3 base dice (tapered)
    if not ask:
        presented = []  # nothing asked → don't mark a topic

    if selling:
        need_block = tip_ask_block            # tip-ask overrides the gather this turn
    elif not question_lines:
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
    elif fan_just_asked:
        # #2 he asked us something — answer it, don't ignore it to fire our own Q.
        need_block = (
            "THIS MESSAGE: he just asked you something — answer it warmly and briefly "
            "in your own words, and DON'T fire a question back this time. (You'll ask "
            "next time.)"
        )
    else:
        need_block = random.choice(_BREATHER_VARIANTS)  # #4 vary the breather itself

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
    # not WHETHER it asks (that's `ask` above). When the human-style opt-in is on,
    # BALANCE a multi-line variant (STYLE_3LINE) against a one-line brevity variant
    # (STYLE_BRIEF) — the source of bubble-count variation (the splitter just honors
    # the reply's length), so replies mix one-liners with a couple of short texts.
    style_extra = ((STYLE_3LINE,) * 2 + (STYLE_BRIEF,) * 2) if style_on else ()
    style = random.choice(_STYLE_VARIANTS + style_extra)
    # When selling, force a single teasing line — a multi-line/breather variant
    # would fight the "ONE short line" tip-ask block.
    if selling:
        style = "one short flirty line, teasing not needy, 0-1 emoji."

    # When the "human texting style" opt-in is ON, append the humanizer block —
    # the not-AI tells (no echo-with-adjective, vary length, one question, etc.).
    humanizer = f"\n\n{STYLE_HUMANIZER}" if style_on else ""
    # When the "non-native English" opt-in is ON, append the register block (the dict
    # in the send path guarantees the signature misspellings; this sets the grammar).
    nonnative = f"\n\n{NONNATIVE_REGISTER}" if nonnative_on else ""

    # When selling (he asked for content), DROP the two clauses that forbid
    # offering — they'd contradict the tip-ask block. of_ai_chat still never names
    # a piece or attaches media; it only asks for the tip.
    offer_clause = "" if selling else "don't offer pics or videos yet; "
    nudes_rule = "" if selling else (
        "- If he gets explicit early or asks for nudes/pics: don't go along with it "
        "and don't offer anything — playfully tease and slow it down, then steer "
        "back to getting to know him. Warm and flirty, never cold or preachy.\n")
    system = (
        f"{persona}\n"
        "You're texting a fan to get to know him while flirting. You are 100% human "
        "— never sound like a bot or an assistant. Use only what you've learned "
        f"about him; don't share your own info unless he asks; {offer_clause}he may "
        "send several texts in a row — read them all, reply to the latest.\n\n"
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
        f"{nudes_rule}"
        f"{_good_examples(f, asked, have_durable_name)}\n"
        f"{ONPLATFORM_GUARDRAIL}\n\n"
        f"{LIVE_PROOF_GUARDRAIL}"
        f"{humanizer}{nonnative}\n\n"
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
                            history_tail: int = _EXTRACT_HISTORY_TAIL,
                            purpose: str = _PURPOSE) -> Fan:
    """Extract his stated facts and persist any NEW ones onto the Fan row (fill
    empty fields only — never overwrite/guess). Updates `f` in place so this tick's
    reply + question list see the fresh facts. Raises LLMCapExceeded (caller stops);
    any other failure is swallowed and leaves `f` unchanged. `purpose` tags the
    grok_calls audit row — ai_chatter passes its own so cost audits stay honest."""
    res = await llm_client.chat(
        model=model, messages=_extract_messages(f, c, history_tail), purpose=purpose,
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
            # Spend-tier the cadence (same session): paying fans refresh faster.
            fresh_after, volume_cap = await gen_info.fan_cadence_knobs(
                s, account_id, fan_id, now)
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
                                         lines_empty=lines_empty,
                                         fresh_after=fresh_after, volume_cap=volume_cap):
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

    # PPVscriptAI hand-over: when ai_chatter is the FULL chatter it REPLACES us —
    # its population (every fan under its spend gate, default $1000) is a superset
    # of ours (the $1 gather pool), so running both would be a second bot voice on
    # the same fans → stand down for the whole account. But when ai_chatter is the
    # CLOSER (intent_only) it answers ONLY buyers and leaves pure chatter to us, so
    # we KEEP RUNNING and instead drop just the fans it owns (computed once by_fan
    # is built). force_ids still targets manually. Lazy import: cycle.
    ai_closer = False
    if not force_ids:
        try:
            from .ai_chatter import (is_enabled as _ai_chatter_enabled,
                                     is_intent_only as _ai_chatter_intent_only)
            if await _ai_chatter_enabled(account_id):
                if await _ai_chatter_intent_only(account_id):
                    ai_closer = True
                else:
                    return {"status": "skipped", "reason": "ai_chatter_owns_account"}
        except Exception:
            log.debug("of_ai_chat ai_chatter gate check failed", exc_info=True)

    model = await resolve_model(account_id, _PURPOSE, payload.get("model"))
    typing_wpm = await load_typing_wpm(account_id)  # per-bubble "typing" pacing
    typing_indicator = await load_typing_indicator(account_id)  # live "...is typing"
    style_on = (await load_style_flags(account_id))[_PURPOSE]  # human-style opt-in
    typo_on = (await load_typo_flags(account_id))[_PURPOSE]    # thumb-typo opt-in
    nonnative_on = (await load_nonnative_flags(account_id))[_PURPOSE]  # non-native opt-in
    strip_emoji_on = await load_strip_emojis(account_id)  # account-wide emoji strip
    max_bubbles = STYLE_MAX_BUBBLES if style_on else 2
    # Content-ask tip-ask: when a fan asks to SEE content, swap the gather question
    # for a natural "tip me $X" ask (the tip_reward automation delivers once he
    # tips). Account-level, so build the directive once. No double-pitch risk: when
    # ai_chatter (the closer) owns this account, run() already short-circuited
    # above, and mid-funnel fans are excluded from the candidate set below.
    tip_ask_enabled, tip_ask_amount, tip_ask_template = await load_tip_ask_config(account_id)
    # Off (per-account toggle) → empty block, so `selling` stays False and the
    # content-ask just gets a normal reply instead of a tip-ask.
    tip_ask_block = (build_tip_ask_block(tip_ask_amount, tip_ask_template)
                     if tip_ask_enabled else "")
    persona = await _load_persona(account_id)
    blacklist, skip_list = await _load_stop_lists(account_id)
    mid_funnel_fans = await _load_mid_funnel_fans(account_id)  # W7 cross-tick ownership
    by_fan = await _gather(account_id, only_fan_ids or None)

    # Closer-mode ai_chatter owns only the fans it will answer (open offer / buying
    # intent) — drop exactly those and cover the rest. Empty unless ai_closer.
    ai_owns: set[int] = set()
    if ai_closer:
        try:
            from .ai_chatter import engaged_subset as _ai_chatter_engaged
            ai_owns = await _ai_chatter_engaged(account_id, set(by_fan))
        except Exception:
            log.debug("of_ai_chat engaged_subset failed", exc_info=True)

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
    skipped_muted_creator = 0  # muted creator we follow — HARD skip (durable)
    skipped_ai_chatter = 0  # closer-mode ai_chatter owns this fan (buyer/open offer)
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
        # Closer-mode ai_chatter is actively answering this fan (open offer or
        # buying intent) — don't add a second voice. force_ids overrides.
        if fan_id in ai_owns and not forced:
            skipped_ai_chatter += 1
            continue
        # Only answer when the fan spoke last (the "You: " sidebar skip).
        if c.last_dir != "in":
            skipped_not_turn += 1
            continue
        f = fans.get(fan_id)
        if f is not None and f.automation_paused_until and f.automation_paused_until > now:
            skipped_listed += 1
            continue
        # Muted creator we follow — a HARD skip even when forced (the scrape also
        # writes a durable skip_list('muted_creator'); this catches the window
        # before that lands). Mutual-promo spam we never want to message.
        if should_skip_muted_creator(f):
            skipped_muted_creator += 1
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
            msgs, presented = _build_messages(
                persona, f, c, asked, history_tail,
                style_on=style_on, nonnative_on=nonnative_on,
                content_ask=is_content_ask(c.last_body), tip_ask_block=tip_ask_block)
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
            # Deterministic floor under ONPLATFORM_GUARDRAIL: if the model still
            # leaked a number / off-platform handle / meetup arrangement, swap for
            # a warm on-platform deflection before it can be sent.
            raw, _leak = guard_offplatform(raw, random.Random(f"{fan_id}:{raw}"))
            if _leak:
                log.info("of_ai_chat off-platform leak guarded account=%s fan=%s reasons=%s",
                         account_id, fan_id, _leak)
            # Count the turn the moment a reply is GENERATED — a "try" counts
            # whether or not it lands. An undeliverable fan (no-id) or an echo-only
            # drop never bumps via the confirmed-send path, so its turn_counter
            # would stay 0 forever and the _MAX_TURNS cap could never fire. Bumping
            # here makes the cap count attempts, so a fan we keep failing to send to
            # still hits 30 and graduates to deep_convo instead of looping for ever.
            if not dry_run:
                await _bump_attempt(account_id, fan_id, now)
            # Account-wide emoji strip (opt-in): remove emojis BEFORE the split so
            # the emoji-aware bubble logic simply never fires. The `if p.strip()`
            # filter below still drops any bubble left empty by the strip.
            if strip_emoji_on:
                raw = strip_emojis(raw)
            # Up to `max_bubbles` bubbles (newline / em-dash / mid-emoji); em-dashes
            # are always cleaned out. Cap (2, or 3 with the style opt-in) so we
            # never spam.
            parts = [apply_word_restriction(p)[:_REPLY_MAX_CHARS]
                     for p in split_for_bubbles(raw, max_bubbles,
                                                rng=random.Random(f"split:{fan_id}:{raw}"))
                     if p.strip()][:max_bubbles]
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
            name_protect = [n for n in (f.real_name, f.generated_nickname,
                                        f.of_display_name) if n]
            if nonnative_on:  # opt-in: deterministic non-native misspellings (always)
                parts = [apply_nonnative_style(p, protect=name_protect) for p in parts]
            if typo_on:  # opt-in: a realistic thumb-slip (+ a throttled "*fix" bubble)
                # protect the words non-native already mangled so we don't double-corrupt
                protect = name_protect + (list(NONNATIVE_OUTPUTS) if nonnative_on else [])
                parts = await apply_typo_throttle(
                    account_id, fan_id, parts, random.Random(f"{fan_id}:{raw}"),
                    protect=protect, max_bubbles=max_bubbles)

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
                        automation_kind=_PURPOSE,  # of_ai_chat
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
        "skipped_muted_creator": skipped_muted_creator,
        "skipped_ai_chatter": skipped_ai_chatter,
        "newly_skiplisted": newly_skiplisted,
        "skipped_locked": skipped_locked,
        "skipped_cooldown": skipped_cooldown,
        "errors": errors,
        "cap_hit": cap_hit,
        "dry_run": dry_run,
        "model": model,
    }
