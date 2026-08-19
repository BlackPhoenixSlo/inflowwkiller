"""service/automations/gen_info.py — Automation 02: fan-profile generation.

Spec: library/one_section_of_automations/02_gen_info.md (network-rewrite mapping at
the bottom — the DOM/JSON-file world is replaced by DB reads + llm_client).

What it does, per tick (`run(account_id, payload, *, run_id)`):

  1. Read the account's fans + their messages from OUR DB (no OF traffic; the WS
     pump + scrape_chats already filled `messages`/`fans`). of_client is never
     touched here — gen_info is pure backend.
  2. Qualify a fan: ≥ 7 fan messages OR ≥ $1 lifetime spend (legacy gate ported
     to cents). `payload.force_ids` bypasses the gate (process_old_fans path).
  2b. Auto-scrape guard: a PAYING fan with thin LOCAL history (we never deep-
     scraped them — the WS pump only sees live traffic) is deferred this pass and
     a one-fan `scrape_chats` is enqueued, so the next pass profiles on the real
     transcript instead of emitting "<name>/Whale" with no facts. See the
     `_THIN_HISTORY_*` knobs + `_enqueue_thin_fan_scrapes`.
  3. Re-run gate: skip a fan whose `fan_profiles` row is still fresh — only
     regenerate when their message count has grown ≥ 22% since the last gen
     (legacy 1.22× factor). `force_ids` bypasses this too.
  4. For each qualifying fan call `llm_client.chat(... response_format=
     json_object ...)` — which writes the `grok_calls` audit row itself (pending
     → done/error) and enforces the per-account/provider daily soft-cap — and
     parse the structured profile.
  5. Persist: upsert the `fan_profiles` row (nickname / short_bio /
     bullet_points / Q1-3 / Tease1-3 / original_name + the generating
     grok_calls id) AND write the AI-extracted facts back onto the `fans` row
     (real_name / age / location / hobbies / occupation / … + grok_facts_updated_at).

Concurrency: fans are processed with a bounded `asyncio.Semaphore` (cap 5, the
legacy ThreadPoolExecutor width). Each fan's DB write uses its OWN AsyncSession —
never a shared one across gather() branches (the SQLAlchemy parallel-session
footgun). The daily-spend cap is serialized inside llm_client's atomic reserve,
so gen_info does NOT need the executor's account_spend_lock.

Scheduling: this self-registers via `@register("gen_info")` on import (the
executor auto-imports `service/automations/*`). To run it periodically, insert an
`automation_rules` row with `kind="gen_info"` and
`trigger_json={"every_seconds": N}`; the executor's generic `_materialize_due_rules`
turns it into a `scheduled_jobs` row each cycle (after scrape_chats has refreshed
the messages). NO edit to automation_executor.py.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import case, func, or_, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from automation_registry import register
from db.engine import get_session
from db.models import AccountAiConfig, Fan, FanProfile, Message, ScrapeHistory, Transaction
import llm_client
from llm_client import LLMCapExceeded, LLMError
from ._common import (
    build_structured_nickname,
    coerce_ids,
    is_greetable_name,
    is_substantive_msg,
    load_promo_spam_ids,
    push_nick_and_notes,
    resolve_model,
    should_skip_muted_creator,
)
from .names import strip_spend_tier

log = logging.getLogger("of-relay.automation.gen_info")

# ── Knobs (ported from 02_gen_info.md) ───────────────────────────────
_DEFAULT_MODEL = "grok-4-1-fast-non-reasoning"   # llm_client fallback (19 §4)
_PURPOSE = "gen_info"
_QUAL_MIN_FAN_MSGS = 1           # profile ANY fan who has written ≥1 message …
_QUAL_MIN_SPEND_CENTS = 1        # …or spent anything (spend no longer gates; all in)
# Re-run cadence (spend-tiered). HARD GATE first: never (re)compile a profile until
# the fan has sent >= _MIN_NEW_MSGS new inbound messages since the last generation
# (or >= that many total when never profiled) — no fresh chat ⇒ nothing new to mine
# ⇒ no LLM call. Once that gate is met, regenerate on EITHER a time window OR a
# new-message volume cap, both set by the fan's spend tier (_tier_knobs): paying fans
# stay freshest, cold $0 fans back off. Replaces the old Fibonacci milestone ladder.
_MIN_NEW_MSGS = 8                          # the gate: < this many new msgs ⇒ never recompile
_NEW_SUB_WINDOW = timedelta(days=7)        # subbed within this ⇒ "new sub" (middle tier)
_RECENT_SPEND_WINDOW = timedelta(days=7)   # cleared spend within this ⇒ "paying" tier
_PAYING_LIFETIME_FLOOR_CENTS = 50000       # >= $500 lifetime ⇒ always paying (whale floor)
# (fresh_after, volume_cap) per tier — fresh_after = time-based refresh window,
# volume_cap = refresh after this many NEW inbound messages.
_CADENCE_PAYING = (timedelta(days=2), 55)  # spent in last 7d OR >= $500 lifetime
_CADENCE_MIDDLE = (timedelta(days=2), 89)  # new sub <7d OR lapsed payer (any lifetime spend)
_CADENCE_COLD = (timedelta(days=7), 89)    # $0 lifetime and not a new sub
_MAX_CONCURRENCY = 4             # background fan-out cap (≤4 — never block proxy/LLM)
_DEFAULT_FAN_LIMIT = 200         # sweep ceiling when no force_ids
_MSG_HEAD = 250                  # trim: keep first 250 …
_MSG_TAIL = 250                  # … + last 250 when a chat exceeds 500
_MSG_CLIP = 250                  # clip each message body to 250 chars
# Spenders get a WIDER window of the already-stored history fed to the model (we
# have it in the DB — use it), since their profile is worth more context.
_MSG_HEAD_SPENDER = 500
_MSG_TAIL_SPENDER = 500
_SPENDER_CENTS = 5000            # ≥ $50 lifetime → "spender" → wider context window

# Auto-scrape guard. A PAYING fan with thin LOCAL history is almost always a fan we
# never deep-scraped: the WS pump only captures live traffic, so a whale who chatted
# before we connected looks empty in our DB and profiles as "<name>/Whale" with no
# facts (the Alex Nielsen case — 1 inbound msg locally, 200 on OF). When a candidate
# has < _THIN_HISTORY_MSG_FLOOR inbound messages AND > _THIN_HISTORY_MIN_SPEND_CENTS
# lifetime spend, enqueue a one-fan scrape_chats and DEFER profiling them this pass —
# don't burn an LLM call on near-empty input; the NEXT pass profiles on the real
# transcript. Gated on scrape_history so we never loop: a fan deep-scraped within
# _SCRAPE_REFRESH_AGE is left alone (if it's STILL thin after a scrape, OF genuinely
# has little, so we profile it honestly with what we have). Forced ids are never
# auto-scraped — an explicit force means "profile now with whatever is here".
_THIN_HISTORY_MSG_FLOOR = 30          # < this many inbound msgs locally = thin
_THIN_HISTORY_MIN_SPEND_CENTS = 100   # > $1 lifetime spend → a paying fan worth scraping
_SCRAPE_REFRESH_AGE = timedelta(days=7)
_AUTOSCRAPE_BATCH_MAX = 25            # cap scrapes enqueued per pass (highest-spend first)

# The structured fields Grok must return. Profile fields land in fan_profiles;
# fact fields are written back onto the fans row.
_SYSTEM_PROMPT = (
    "You build a concise CRM profile of an OnlyFans fan from their chat history "
    "with the creator. You may be given KNOWN INFO already confirmed about him — "
    "treat it as ground truth and BUILD ON it (confirm/refine from the chat, never "
    "drop a known fact). Respond with a SINGLE JSON object and nothing else. Use an "
    "empty string (or [] for lists) for anything you cannot determine — never guess, "
    'never write "Unknown" or "??". Keys:\n'
    '  "nickname": slash-joined tag "Name/City,Country/Age/Job" (<=60 chars). '
    "The Name MUST be his real first name (from the chat or the KNOWN real name). "
    "NEVER use a number, a fan id, a u<digits> handle, a username, or a PLACE "
    "(country/state/city — those belong in the second section) as the Name — "
    "leave the Name section EMPTY if you don't know it. Leave any unknown section "
    'EMPTY (no "??", no filler), e.g. "Garrett/Delta,Canada/30/Paramedic". '
    "NEVER add a spend/status label (Free, Buyer, Spender, Whale, VIP) — his "
    "spending is not part of his name.\n"
    '  "short_bio": one sentence describing who he is and his spending,\n'
    '  "bullet_points": markdown "- Label: value" lines, MOST USEFUL FIRST with '
    'SHORT labels. Lead with "Recent:" (what is going on in his life lately), then '
    '"Intr:" (hobbies/interests), then "Important:" (what matters most about him — '
    'what he wants, gripes/complaints, pay day). Then "Job:", "Rel:" (relationship), '
    '"Kinks:". OMIT his name, age and location (those are already in the nickname) '
    "and his lifetime spend (shown elsewhere),\n"
    '  "Q1","Q2","Q3": three empathetic, DEEP questions that each reference a REAL '
    "specific thing he said (his job, a hobby, a recent event) — concrete, not generic "
    'filler like "what are you into"; NO emojis,\n'
    '  "Tease1","Tease2","Tease3": three flirty, suggestive teases to send after a '
    "silence — each ties to something real about him and uses his name where it sounds "
    "natural (e.g. \"Bet you could handle more than you think, Nahtan.\"); NO emojis,\n"
    '  "original_name": his real first name if stated, else "" (never "Unknown"),\n'
    '  "real_name": same as original_name,\n'
    '  "his_age": his age as a string (ONLY if he stated it; "" if unknown — never guess),\n'
    '  "home_country": his country (only if stated, or obvious from a city he named; "" otherwise),\n'
    '  "home_city": his city (ONLY a city he actually named; "" if not stated — do NOT guess one from the country),\n'
    '  "hobbies": comma-separated interests he actually mentioned ("" if none — do NOT invent),\n'
    '  "occupation": his job (only if stated; ""),\n'
    '  "relationship_status": single/married/etc (only if stated; ""),\n'
    '  "fetishes": comma-separated kinks he mentioned ("" if none),\n'
    '  "recent_events": a JSON array of short strings — things happening in his life '
    "lately (trip, new job, breakup, birthday, holiday); [] if none.\n"
    # ── Richer facts (appended at the END so the stable prompt prefix — and its
    #    DeepSeek cache — is untouched). Same never-guess discipline. ──
    '  "employer": the company/place he works (ONLY if named; ""),\n'
    '  "partner_name": his partner\'s name (ONLY if he named one; ""),\n'
    '  "relationship_stage": where things stand with YOU the creator (e.g. new/regular/'
    'devoted/cooling) — ONLY if the chat clearly shows it; "" otherwise,\n'
    '  "has_kids": true or false ONLY if he clearly stated it; otherwise the empty '
    'string "" (never guess),\n'
    '  "pets": a JSON array of short strings like "dog Toby" for pets he mentioned; [] '
    "if none (do NOT invent),\n"
    '  "communication_style": a JSON object with any of {"chatty":true/false,'
    '"kink_forward":true/false,"romantic":true/false,"blunt":true/false} you can '
    "actually tell from how he writes; {} if unsure,\n"
    '  "language": the ISO 639-1 code (two letters, e.g. "en", "es", "pt") of the '
    "language HE writes in — judge from his own messages, not yours. If he mixes "
    'languages, pick the dominant one. "" if you truly cannot tell.\n'
    "\nCRITICAL: only record facts the fan ACTUALLY stated in the chat. If something "
    "wasn't said, leave it EMPTY — never guess or infer (don't guess a city from a "
    "country, don't invent hobbies or a job he never mentioned).\n"
)

_HTML_OPEN = "<"
_TAG_RE = re.compile(r"<[^>]+>")
_SLASH_RE = re.compile(r"/{2,}")
_WS_RE = re.compile(r"\s+")


# ── Small text helpers (local copies — house pattern) ────────────────

def _strip_html(s: str | None) -> str:
    if not s:
        return ""
    if _HTML_OPEN not in s:
        return s.strip()
    return _TAG_RE.sub("", s).strip()


def _clean_nickname(s: str | None) -> str:
    """Drop empty + placeholder ('??') segments and any derived SPEND TIER, collapse
    slashes, trim. The model sometimes fills unknown slots with '??' despite the
    prompt (W2h).

    The tier strip is here rather than at one call site because every path through
    this module runs through this function, and the one that matters most is the
    READ: `_known_block` hands the stored nickname back to the model as ground truth
    to 'refine + keep complete', so a legacy '/Free' would be copied forward into
    every regeneration for the rest of the fan's life. Stripping on the way in AND
    on the way out means a fan heals on his next profile pass instead of needing a
    sweep. See names.strip_spend_tier for why the tier was retired."""
    if not s:
        return ""
    parts = [p.strip() for p in strip_spend_tier(s).split("/")]
    parts = [p for p in parts if any(ch.isalnum() for ch in p)]   # drop empty/'??'/',' slots
    return _WS_RE.sub(" ", "/".join(parts)).strip()


def _blank_unknown(s: Any) -> str:
    """Grok may emit 'Unknown' for original_name → treat as blank (02 edge case)."""
    s = (s or "")
    s = s.strip() if isinstance(s, str) else str(s)
    return "" if s.lower() in ("unknown", "n/a", "none") else s


def _as_text(v: Any) -> str:
    """Coerce a profile text field to a plain string for the TEXT column. The model
    sometimes returns a multi-line field (notably bullet_points) as a JSON ARRAY of
    lines instead of a string — join those with newlines rather than letting a list
    reach the DB (sqlite bind error 'type list is not supported')."""
    if v is None:
        return ""
    if isinstance(v, (list, tuple)):
        return "\n".join(str(x).strip() for x in v if str(x).strip())
    return str(v).strip() if not isinstance(v, str) else v


# An "empty" bullet line: a label with no value after the colon, e.g. "- Job:",
# "Rel:", "* Kinks: " (model emits these despite the empty-string instruction).
_EMPTY_BULLET_RE = re.compile(r"^[-*\s]*[A-Za-z][\w /]*:\s*$")


def _clean_bullets(v: Any) -> str:
    """bullet_points → text with EMPTY label-only lines dropped (skip empty fields).
    Handles the JSON-array shape via _as_text first, then filters blank-value lines
    so a stored bullet block never carries a bare 'Job:' / 'Rel:' / 'Kinks:'."""
    text = _as_text(v)
    if not text:
        return ""
    lines = [
        ln for ln in (l.rstrip() for l in text.split("\n"))
        if ln.strip() and not _EMPTY_BULLET_RE.match(ln.strip())
    ]
    return "\n".join(lines)


def _parse_events(raw: Any) -> list[str]:
    """recent_events is a JSON-array TEXT column (default '[]'). Parse defensively
    to a list of non-empty strings (W2h)."""
    if not raw:
        return []
    try:
        v = json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, TypeError):
        s = str(raw).strip()
        return [s] if s and s != "[]" else []
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    s = str(v).strip()
    return [s] if s else []


def _here(c: "_Candidate") -> tuple:
    """This fan's own stored location — never his name. Passed to is_greetable_name
    so a city no static list can hold ('Millbrook') is caught for THIS fan."""
    return (c.known.get("home_country"), c.known.get("home_city"))


def _guard_name(nickname: str, c: "_Candidate") -> str:
    """Blank the Name slot (first '/'-segment) when it isn't a real first name —
    `is_greetable_name`, the same predicate the senders greet by: a digit-handle
    (111222333, T900, Sparky10, u7654321, the fan id), junk with no letters (',',
    '.'), a place, or this fan's own city/country. With no name to be found the
    model writes the COUNTRY into the Name slot ('Canada/Canada/office job'), which
    every downstream sender then greeted him by. The _name_hint fallback re-adds a
    real OF name afterwards (W2h) — it uses the same predicate, so it cannot hand
    back what this just dropped."""
    if not nickname:
        return nickname
    parts = nickname.split("/")
    name = parts[0].strip()
    if not name:
        return nickname
    if is_greetable_name(name, here=_here(c)):
        return nickname
    parts[0] = ""                       # drop the bad name; keep the rest of the tag
    return _clean_nickname("/".join(parts))


def _name_hint(c: "_Candidate") -> str:
    """A real first name to anchor the nickname — prefer the chat-extracted real
    name, then the OF display name, then the OF USERNAME. `is_greetable_name` decides
    what qualifies (same predicate as _guard_name and the senders), so a bare number
    or the fan id is never used and digit-handles ('T900', 'Sparky10', 'u7654321',
    'gamer4you'), junk ('.', 'C') and PLACES are skipped. Without the place check
    this handed straight back what _guard_name had just dropped: a fan whose OF
    display name is 'Canada' got 'Canada' re-installed as his name one line later.
    The username fallback means a fan whose display name is just their numeric id but
    whose handle is a real word (e.g. id 111222333 / name 'sam k' → 'Sam') gets a
    name, never a number. Capitalised (real name/username used, never digits)."""
    for src in (c.known.get("real_name", ""), c.of_name, c.of_username):
        s = (src or "").strip()
        tok = s.split(" ")[0].strip(".,/") if s else ""
        if len(tok) >= 2 and tok.isalpha() and is_greetable_name(tok, here=_here(c)):
            return tok[:1].upper() + tok[1:]
    return ""


# The nickname's spend tier (`_spend_tier`, and the Bonus slot it filled) is gone —
# see names.strip_spend_tier. `c.spend_cents` is still gathered: _tier_knobs below
# spends the PROFILING BUDGET by spend tier, which is a live decision made fresh on
# every pass. That is the difference — a tier is fine to compute, and was only ever
# harmful once it got frozen into a label a human reads days later.
def _tier_knobs(lifetime_cents: int, recent_cents: int,
                subscribed_at: "datetime | None", now: datetime) -> tuple:
    """(fresh_after, volume_cap) for a fan, by spend tier — spend the profiling
    budget where the money is. PAYING (spent in the last _RECENT_SPEND_WINDOW, or a
    >= $500 lifetime whale) gets the freshest cadence; MIDDLE (a new sub, or a lapsed
    payer with any lifetime spend) the default; COLD ($0 and not new) the laziest.
    First match wins."""
    if recent_cents > 0 or lifetime_cents >= _PAYING_LIFETIME_FLOOR_CENTS:
        return _CADENCE_PAYING
    new_sub = subscribed_at is not None and (now - subscribed_at) < _NEW_SUB_WINDOW
    if new_sub or lifetime_cents > 0:
        return _CADENCE_MIDDLE
    return _CADENCE_COLD


def profile_is_stale(prev_count: int | None, last_gen_at: datetime | None,
                     now_count: int, now: datetime,
                     lines_empty: bool = False, *,
                     fresh_after: timedelta = _CADENCE_MIDDLE[0],
                     volume_cap: int = _CADENCE_MIDDLE[1],
                     class_spent: bool = False) -> bool:
    """Should this fan's profile be regenerated? Shared with welcome_chatter_for_info's
    refresh-if-stale hook. `fresh_after`/`volume_cap` come from _tier_knobs (the
    caller classifies the fan's spend tier); they default to the MIDDLE tier.

    HARD GATE: returns False unless >= _MIN_NEW_MSGS new inbound messages have arrived
    since the last generation (or >= that many total when never profiled) — no fresh
    chat ⇒ nothing to re-mine ⇒ no LLM call. Past the gate, regenerate when never
    profiled, when `volume_cap` new messages have piled up, or when the profile is
    older than `fresh_after`.

    `lines_empty` is the one EXEMPTION from the gate: when a whole Q- or Tease-class
    has been consumed (all three slots null) AND there's any new inbound message,
    refill the openers now — welcome_chatter_for_info needs openers to talk, so that functional
    refill isn't held to the >= _MIN_NEW_MSGS freshness gate.

    `class_spent` (from `_openers.class_spent`) is the NON-destructive twin of that
    signal: the lines are still on the row, but every one in a class has been
    delivered. Deliberately NOT an exemption — it is checked AFTER the gate, so a fan
    who has burnt his questions still waits for real new chat before we re-mine him.
    Two reasons. Re-mining the same transcript hands back the same lines, and since a
    regen re-arms the used-set, the fan would simply be re-sent openers he has already
    had — the repeat this whole mechanism exists to stop. And keeping it behind the
    gate makes the trigger self-limiting: firing moves the message baseline, so it
    cannot fire again until another _MIN_NEW_MSGS arrive. `lines_empty` can afford to
    skip the gate because a null class means the bot has nothing to say; a spent class
    still has banter."""
    nc = int(now_count)
    prev = int(prev_count) if prev_count is not None else 0
    new_msgs = nc - prev
    if lines_empty and new_msgs > 0:
        return True
    if new_msgs < _MIN_NEW_MSGS:           # the gate — too little new chat to bother
        return False
    if prev_count is None:                 # never profiled, now past the gate
        return True
    if class_spent:                        # openers all delivered — mine fresh ones
        return True
    if new_msgs >= volume_cap:             # volume: every `volume_cap` new messages
        return True
    if last_gen_at is not None and (now - last_gen_at) >= fresh_after:  # time
        return True
    return False


async def fan_cadence_knobs(s, account_id: str, fan_id: int, now: datetime,
                            *, lifetime_spend_cents: int | None = None) -> tuple:
    """(fresh_after, volume_cap) for one fan, classified from the DB on an open
    session `s` — the per-fan analogue of the batch tier computation the sweep does
    inline. Reads lifetime spend + subscribed_at off the fans row and sums CLEARED
    spend in the last _RECENT_SPEND_WINDOW. Used by welcome_chatter_for_info's refresh hook."""
    fan_id = int(fan_id)
    if lifetime_spend_cents is None:
        frow = (await s.execute(
            select(Fan.lifetime_spend_cents, Fan.subscribed_at)
            .where(Fan.account_id == str(account_id), Fan.fan_id == fan_id)
        )).first()
        lifetime_spend_cents = int(frow[0] or 0) if frow else 0
        subscribed_at = frow[1] if frow else None
    else:
        subscribed_at = (await s.execute(
            select(Fan.subscribed_at)
            .where(Fan.account_id == str(account_id), Fan.fan_id == fan_id)
        )).scalar()
    recent = (await s.execute(
        select(func.coalesce(func.sum(Transaction.amount_cents), 0))
        .where(Transaction.account_id == str(account_id),
               Transaction.fan_id == fan_id,
               Transaction.status == "cleared",
               Transaction.occurred_at >= now - _RECENT_SPEND_WINDOW)
    )).scalar() or 0
    return _tier_knobs(int(lifetime_spend_cents), int(recent), subscribed_at, now)


def _known_block(c: "_Candidate") -> str:
    """KNOWN INFO (ground truth) from prior stored facts so the model BUILDS ON what
    we already learned + keeps the nickname complete (W2h). '' when nothing known."""
    k = c.known or {}
    lines: list[str] = []
    real = (k.get("real_name") or "").strip()
    if real:
        lines.append(f"Real name: {real}")
    loc = ", ".join(x for x in ((k.get("home_city") or "").strip(),
                                (k.get("home_country") or "").strip()) if x)
    if loc:
        lines.append(f"Location: {loc}")
    for label, key in (("Age", "his_age"), ("Job", "occupation"),
                       ("Hobbies", "hobbies"), ("Relationship", "relationship_status"),
                       ("Fetishes", "fetishes")):
        v = (k.get(key) or "").strip()
        if v:
            lines.append(f"{label}: {v}")
    events = _parse_events(k.get("recent_events"))
    if events:
        lines.append("Recent events: " + "; ".join(events))
    prior_nick = _clean_nickname(k.get("generated_nickname"))
    if prior_nick:
        lines.append(f"Current nickname (refine + keep complete): {prior_nick}")
    if not lines:
        return ""
    return ("KNOWN INFO (already confirmed — ground truth; confirm or correct from "
            "the chat, never drop it):\n" + "\n".join(lines) + "\n\n")


# ── Candidate selection ──────────────────────────────────────────────

class _Candidate:
    __slots__ = ("fan_id", "fan_msg_n", "total_msg_n", "spend_cents",
                 "recent_spend_cents", "subscribed_at",
                 "source", "is_muted", "of_name", "of_username", "known", "messages")

    def __init__(self, fan_id: int):
        self.fan_id = fan_id
        self.fan_msg_n = 0
        self.total_msg_n = 0
        self.spend_cents = 0
        self.recent_spend_cents = 0          # cleared spend in the last _RECENT_SPEND_WINDOW
        self.subscribed_at = None            # fans.subscribed_at — for the new-sub tier
        self.source = ""    # "fan" = subscribedOn (real fan); else peer creator/unknown
        self.is_muted = False  # OF chat muted — a muted creator is spam, never profile
        self.of_name = ""
        self.of_username = ""
        # Prior extracted facts from the fans row — fed back as ground truth so
        # profiles ACCRETE and the nickname stays complete (W2h). DB-only, no OF call.
        self.known: dict[str, str] = {}
        self.messages: list[tuple[str, str]] = []  # (direction, body)


async def _gather_candidates(
    account_id: str, force_ids: set[int], limit: int, now: datetime
) -> list[_Candidate]:
    """One pass over the account's messages → per-fan counts + trimmed history.

    Spend is summed from PURCHASED outbound PPV/tips (legacy `sender==Model and
    is_paid`). Fan-message count is inbound messages. The fan's display name +
    subscribed_at + recent cleared spend are lifted for the cadence tiering. Returns
    candidates that pass the qualification gate (or are forced)."""
    counts: dict[int, _Candidate] = {}
    async with get_session() as s:
        rows = (
            await s.execute(
                select(
                    Message.fan_id,
                    Message.direction,
                    Message.body,
                    Message.price_cents,
                    Message.is_paid,
                    Message.is_tip,
                )
                .where(Message.account_id == account_id)
                .order_by(Message.fan_id, Message.created_at)
            )
        ).all()

        for fan_id, direction, body, price_cents, is_paid, is_tip in rows:
            c = counts.get(fan_id)
            if c is None:
                c = counts[fan_id] = _Candidate(int(fan_id))
            c.total_msg_n += 1
            text = _strip_html(body)[:_MSG_CLIP]
            # Count only substantive inbound (matches welcome_chatter_for_info): emoji-only
            # reactions don't inflate fan_msg_n, so message_count_at_gen and the
            # welcome_chatter_for_info staleness baseline stay on ONE scale (no suppressed/false
            # refreshes for emoji-heavy fans).
            if direction == "in" and is_substantive_msg(text):
                c.fan_msg_n += 1
            if direction == "out" and is_paid and (price_cents or 0) > 0:
                c.spend_cents += int(price_cents or 0)
            c.messages.append((direction, text))

        # Lift display name + username + prior extracted facts (ground truth, W2h)
        # for the fans we care about — ONE DB read, no OF call (proxy-safe).
        prior: dict[int, dict] = {}
        subdates: dict[int, "datetime | None"] = {}   # fan_id → subscribed_at (tiering)
        if counts:
            frows = (await s.execute(
                select(
                    Fan.fan_id, Fan.of_display_name, Fan.of_username,
                    Fan.real_name, Fan.his_age, Fan.home_city, Fan.home_country,
                    Fan.hobbies, Fan.occupation, Fan.relationship_status,
                    Fan.fetishes, Fan.recent_events, Fan.recent_events_timeline,
                    Fan.generated_nickname,
                    Fan.custom_nickname, Fan.source, Fan.is_muted, Fan.subscribed_at,
                ).where(
                    Fan.account_id == account_id,
                    Fan.fan_id.in_(list(counts)),
                )
            )).all()
            for r in frows:
                subdates[int(r.fan_id)] = r.subscribed_at
                prior[int(r.fan_id)] = {
                    "of_display_name": r.of_display_name or "",
                    "of_username": r.of_username or "",
                    "real_name": r.real_name or "",
                    "his_age": r.his_age or "",
                    "home_city": r.home_city or "",
                    "home_country": r.home_country or "",
                    "hobbies": r.hobbies or "",
                    "occupation": r.occupation or "",
                    "relationship_status": r.relationship_status or "",
                    "fetishes": r.fetishes or "",
                    "recent_events": r.recent_events or "[]",
                    "recent_events_timeline": r.recent_events_timeline or "[]",
                    "generated_nickname": r.generated_nickname or "",
                    "custom_nickname": r.custom_nickname or "",
                    "source": r.source or "",
                    "is_muted": bool(r.is_muted),
                }

        # Lifetime spend from the transactions ledger (cleared, fan-attributed) is
        # more accurate than the message-derived PPV/tips sum, so take the HIGHER of
        # the two — a fan's true spend is at least whatever either source shows
        # ("usually the higher wins"). Same session, sequential read (not gather()).
        if counts:
            txrows = (await s.execute(
                select(Transaction.fan_id, func.sum(Transaction.amount_cents))
                .where(Transaction.account_id == account_id)
                .where(Transaction.status == "cleared")
                .where(Transaction.fan_id.is_not(None))
                .where(Transaction.fan_id.in_(list(counts)))
                .group_by(Transaction.fan_id)
            )).all()
            for fid, total in txrows:
                c = counts.get(int(fid))
                if c is not None:
                    c.spend_cents = max(c.spend_cents, int(total or 0))

        # Recent cleared spend (last _RECENT_SPEND_WINDOW) → the "paying" cadence tier.
        if counts:
            txrecent = (await s.execute(
                select(Transaction.fan_id, func.sum(Transaction.amount_cents))
                .where(Transaction.account_id == account_id)
                .where(Transaction.status == "cleared")
                .where(Transaction.fan_id.is_not(None))
                .where(Transaction.fan_id.in_(list(counts)))
                .where(Transaction.occurred_at >= now - _RECENT_SPEND_WINDOW)
                .group_by(Transaction.fan_id)
            )).all()
            for fid, total in txrecent:
                c = counts.get(int(fid))
                if c is not None:
                    c.recent_spend_cents = int(total or 0)

    promo_spam = await load_promo_spam_ids(account_id)
    qualifying: list[_Candidate] = []
    for fan_id, c in counts.items():
        info = prior.get(fan_id) or {}
        c.of_name = info.get("of_display_name") or ""
        c.of_username = info.get("of_username") or ""
        c.source = info.get("source") or ""
        c.is_muted = bool(info.get("is_muted"))
        c.subscribed_at = subdates.get(fan_id)
        c.known = info
        forced = fan_id in force_ids
        # Promo-spam guard (shares welcome_chatter_for_info's rule): don't waste an LLM profile
        # on peer creators who blast the inbox. Either we've MUTED their chat (a
        # deliberate "silence this creator" — never profile one even if they once
        # paid), or they're in the account's promo-blaster set.
        #
        # The old test was `creator_we_follow` + $0, on the belief that "real fans
        # are NEVER this value". That was false — the flag only means OF says we're
        # subscribed to them, which is true of any fan a free page followed back —
        # and it silently stopped profiling 71 real fans on one live account.
        # Re-evaluated each run.
        #
        # `c.spend_cents` re-checks the spend the SET already tested, deliberately:
        # the set reads `fans.lifetime_spend_cents`, which LAGS ingest, while
        # c.spend_cents is the fresher max(ledger, paid-PPV) this pass already
        # computed. Live now: 9 of Ava's fans have real spend the column still
        # reports as $0. Narrowing here can only ever RESCUE a payer from the guard,
        # never silence one — the direction a spam gate must always fail in.
        is_spam = not forced and (
            should_skip_muted_creator(c)
            or (fan_id in promo_spam and c.spend_cents == 0)
        )
        qualified = forced or (
            not is_spam
            and (c.fan_msg_n >= _QUAL_MIN_FAN_MSGS
                 or c.spend_cents >= _QUAL_MIN_SPEND_CENTS)
        )
        if qualified:
            qualifying.append(c)

    # Stable, deterministic order; cap the sweep so a tick stays bounded. Forced ids
    # sort FIRST so they always survive the `[:limit]` slice: the rest of the order is
    # spend-desc, so a $0 fan on a big roster ranks below the cap and would otherwise be
    # truncated away — an explicit force (the composer's "Generate new batch") must never
    # be silently dropped just because the fan is poor.
    qualifying.sort(
        key=lambda x: (x.fan_id not in force_ids, -x.spend_cents, -x.fan_msg_n, x.fan_id))
    return qualifying[:max(limit, len(force_ids))]


def _trim_messages(messages: list[tuple[str, str]], spender: bool = False) -> str:
    """Fan-only transcript: head + tail window (02 build_prompt). Spenders get a
    WIDER window (500+500 vs 250+250) — we already have the history in the DB, so a
    high-value fan's profile gets more of it; everyone else stays at the default.

    V1 fed Grok the FAN side only and produced sharper profiles, so we drop the
    creator's outbound lines (direction != "in") before trimming — the head/tail
    window then counts fan messages, not the mixed stream."""
    head = _MSG_HEAD_SPENDER if spender else _MSG_HEAD
    tail = _MSG_TAIL_SPENDER if spender else _MSG_TAIL
    fan_msgs = [(d, b) for d, b in messages if d == "in"]
    if len(fan_msgs) > (head + tail):
        fan_msgs = fan_msgs[:head] + fan_msgs[-tail:]
    lines = []
    for _direction, body in fan_msgs:
        if not body:
            continue
        lines.append(f"FAN: {body}")
    return "\n".join(lines)


def _build_user_prompt(c: _Candidate) -> str:
    history = _trim_messages(c.messages, spender=c.spend_cents >= _SPENDER_CENTS)
    disp = (c.of_name or "").strip()
    uname = (c.of_username or "").strip()
    name_line = f"Fan OF display name: {disp or '(unknown)'}"
    if uname and uname.lower() != disp.lower():
        name_line += f"  (username/handle: {uname} — a handle, NOT a real name)"
    return (
        f"{name_line}\n"
        f"Fan message count: {c.fan_msg_n}\n"
        f"Lifetime spend: ${c.spend_cents / 100:.2f}\n\n"
        f"{_known_block(c)}"
        f"Chat history (oldest→newest):\n{history}\n"
    )


# ── Persistence ──────────────────────────────────────────────────────

# The extracted facts written back onto the `fans` row, as (json_key, column, kind).
# `kind` drives per-type serialization so a bool/JSON field never reaches the DB as a
# Python repr: the naive `str(val)` path turned has_kids=False into "" (dropped) and
# a bool True into the string "True", which RAISES StatementError on a Boolean column
# and rolls back the whole persist. Adding a field here is now type-safe.
#   str      → cleaned text ("" ⇒ skip); a list is comma-joined
#   bool     → real True/False from yes/no/true/false/1/0 ("unknown"/"" ⇒ skip)
#   json_arr → json.dumps(list)   ("[]"/empty ⇒ skip)
#   json_obj → json.dumps(dict)   ({}/empty ⇒ skip)
_FACT_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("real_name", "real_name", "str"),
    ("his_age", "his_age", "str"),
    ("home_country", "home_country", "str"),
    ("home_city", "home_city", "str"),
    ("hobbies", "hobbies", "str"),
    ("occupation", "occupation", "str"),
    ("relationship_status", "relationship_status", "str"),
    ("fetishes", "fetishes", "str"),
    # ── Richer facts (R1): fill columns gen_info never wrote before. ──
    ("employer", "employer", "str"),
    ("partner_name", "partner_name", "str"),
    ("relationship_stage", "relationship_stage", "str"),
    ("has_kids", "has_kids", "bool"),
    ("pets", "pets", "json_arr"),
    ("communication_style", "communication_style", "json_obj"),
)

# Profile columns that must be KEPT when the LLM returns them empty (FIX-2): a sparse
# response (~7% are fully empty) must NOT null out a fan's stored nickname / bio /
# openers. Nulling q/tease trips `lines_empty` → the one exemption from _MIN_NEW_MSGS
# → a forced regen every inbound → unbounded. These are guarded in the upsert set_.
_PROFILE_KEEP_IF_EMPTY = frozenset((
    "nickname", "short_bio", "bullet_points", "original_name",
    "q1", "q2", "q3", "tease1", "tease2", "tease3",
))


# ── Language detection (R1) ──────────────────────────────────────────
# gen_info DETECTS the fan's language and stores it as an observation (source="ai").
# It does NOT yet drive routing — the bot's output language is the ACCOUNT default
# (resolve_language) until per-fan routing is switched on. So a wrong detection is a
# display/collection error, never a mis-route. Two guards make it safe:
#   • EVIDENCE FLOOR — need real fan text before trusting a guess (44 fans have zero
#     inbound; 19.7% have < 400 chars). Below the floor ⇒ leave NULL ⇒ "use account".
#   • MANUAL WINS — never overwrite a language an operator pinned (language_source
#     == "manual"). Enforced per-column in the upsert CASE, not a row-level where=.
_LANG_EVIDENCE_MIN_CHARS = 40          # min inbound fan text before we trust detection
_KNOWN_LANGS = frozenset((             # ISO 639-1 we accept; others ⇒ leave NULL
    "en", "es", "sl", "pt", "fr", "de", "it", "nl", "ru", "pl", "tr", "ar",
    "sv", "no", "da", "fi", "cs", "ro", "hu", "el", "he", "ja", "ko", "zh",
))
_LANG_NAME_MAP = {                      # tolerate a name if the model ignores "ISO code"
    "english": "en", "spanish": "es", "español": "es", "espanol": "es",
    "slovenian": "sl", "slovene": "sl", "slovenščina": "sl",
    "portuguese": "pt", "french": "fr", "german": "de", "italian": "it",
}


def _norm_lang(raw: Any) -> str:
    """Normalize a model language value to a known ISO 639-1 code, else "". Accepts
    'es', 'ES', 'es-MX', or a language NAME ('Spanish'); rejects anything unknown so a
    junk value never becomes a routing key."""
    s = (raw or "")
    s = s.strip().lower() if isinstance(s, str) else ""
    if not s:
        return ""
    code = s.split("-")[0].split("_")[0]           # 'es-MX' → 'es'
    if code in _KNOWN_LANGS:
        return code
    return _LANG_NAME_MAP.get(s, "")


def _enough_language_evidence(c: "_Candidate") -> bool:
    """True when the fan has written enough text to trust a language guess."""
    chars = sum(len(b) for d, b in c.messages if d == "in" and b)
    return chars >= _LANG_EVIDENCE_MIN_CHARS


def _serialize_fact(kind: str, raw: Any) -> tuple[bool, Any]:
    """(should_write, typed_value) for one extracted fact. Never returns should_write
    for an empty/unknown value, so a sparse run never clobbers a prior fact. See
    _FACT_FIELDS for the kinds. This is the single guard against binding a Python repr
    (dict/bool) to a typed column."""
    if raw is None:
        return (False, None)
    if kind == "bool":
        if isinstance(raw, bool):
            return (True, raw)
        s = str(raw).strip().lower()
        if s in ("true", "yes", "y", "1"):
            return (True, True)
        if s in ("false", "no", "n", "0"):
            return (True, False)
        return (False, None)                     # "unknown"/"" ⇒ leave the column alone
    if kind == "json_arr":
        if isinstance(raw, (list, tuple)):
            items = [x for x in raw if x not in (None, "", {}, [])]
            return (True, json.dumps(items, ensure_ascii=False)) if items else (False, None)
        s = str(raw).strip()
        if s and s != "[]":
            return (True, json.dumps([s], ensure_ascii=False))
        return (False, None)
    if kind == "json_obj":
        if isinstance(raw, dict):
            obj = {k: v for k, v in raw.items() if v not in (None, "", [], {})}
            return (True, json.dumps(obj, ensure_ascii=False)) if obj else (False, None)
        return (False, None)
    # str (default): flatten a list to a comma-joined string.
    if isinstance(raw, (list, tuple)):
        val = ", ".join(str(x).strip() for x in raw if str(x).strip())
    else:
        val = raw.strip() if isinstance(raw, str) else str(raw).strip()
    return (True, val) if val else (False, None)


async def _persist(
    account_id: str, c: _Candidate, data: dict, call_id: int | None, now: datetime,
    *, client=None
) -> None:
    """Upsert fan_profiles + write extracted facts onto the fans row, in ONE
    fresh session (never shared across gather branches). When `client` is given,
    also (re)assert the structured OF nickname so the chat-header name stays in
    sync — see _sync_of_nickname."""
    nickname = _guard_name(_clean_nickname(data.get("nickname")), c)
    if not nickname:                       # AI gave nothing usable → keep the old one
        nickname = _guard_name(_clean_nickname(c.known.get("generated_nickname")), c)
    # Anchor the nickname with the fan's REAL name (the model often drops it even
    # though OF gives us of_display_name). Prepend the hint unless it's already lead.
    hint = _name_hint(c)
    if hint and (not nickname or nickname.split("/")[0].strip().lower() != hint.lower()):
        nickname = _clean_nickname(hint + "/" + nickname)
    # A name-only nickname ("Mani") now STAYS name-only. It used to get a derived
    # spend tier appended as a Bonus slot ("Mani" → "Mani/Spender") so the label
    # "carried a useful flag" — but the flag was stamped once and never re-derived,
    # so it aged into a lie in the one place the team reads it. A bare name is a
    # thin label; a confidently wrong tier is a harmful one.
    original_name = _blank_unknown(data.get("original_name") or data.get("real_name"))

    prof_values = dict(
        account_id=str(account_id),
        fan_id=int(c.fan_id),
        original_name=original_name or None,
        message_count_at_gen=c.fan_msg_n,
        total_spend_at_gen_cents=c.spend_cents,
        nickname=nickname or None,
        short_bio=_as_text(data.get("short_bio")) or None,
        bullet_points=_clean_bullets(data.get("bullet_points")) or None,
        q1=_as_text(data.get("Q1")) or None,
        q2=_as_text(data.get("Q2")) or None,
        q3=_as_text(data.get("Q3")) or None,
        tease1=_as_text(data.get("Tease1")) or None,
        tease2=_as_text(data.get("Tease2")) or None,
        tease3=_as_text(data.get("Tease3")) or None,
        last_generated_at=now,
        generated_by_grok_call_id=call_id,
    )
    prof_update = {k: v for k, v in prof_values.items()
                   if k not in ("account_id", "fan_id")}

    # Extracted facts → fans columns. Only overwrite with a non-empty value so a
    # later sparse generation never wipes a fact an earlier one found.
    fact_update: dict[str, Any] = {"grok_facts_updated_at": now,
                                   "updated_at": now}
    for key, col, kind in _FACT_FIELDS:
        ok, val = _serialize_fact(kind, data.get(key))
        if ok:
            fact_update[col] = val
    if original_name and "real_name" not in fact_update:
        fact_update["real_name"] = original_name
    if nickname:
        fact_update["generated_nickname"] = nickname
    # recent_events: merge new events onto the prior list (accrete, dedup, cap 20)
    # and store as the JSON-array TEXT column (NOT NULL default '[]'). Only write
    # when non-empty so a sparse run never clears prior events.
    new_events = _parse_events(data.get("recent_events"))
    if new_events:
        prior_events = _parse_events(c.known.get("recent_events"))
        fresh = [e for e in new_events if e not in prior_events]
        merged = prior_events + fresh
        fact_update["recent_events"] = json.dumps(merged[:20], ensure_ascii=False)
        # Dated timeline (step 6): stamp each NEWLY-learned event with today's date and
        # accrete onto the prior timeline (keeps old dates), newest last, cap 30. Read by
        # none of the six recent_events readers — the flat list above is untouched.
        if fresh:
            try:
                prior_tl = json.loads(c.known.get("recent_events_timeline") or "[]")
                prior_tl = [t for t in prior_tl if isinstance(t, dict) and t.get("event")]
            except (json.JSONDecodeError, TypeError):
                prior_tl = []
            seen_ev = {t.get("event") for t in prior_tl}
            day = now.strftime("%Y-%m-%d")
            for e in fresh:
                if e not in seen_ev:
                    prior_tl.append({"date": day, "event": e})
            fact_update["recent_events_timeline"] = json.dumps(prior_tl[-30:], ensure_ascii=False)

    # Language: a detected observation (source="ai"), written only past the evidence
    # floor and NEVER over an operator's manual pin. Kept out of fact_update because it
    # needs a per-column CASE guard, not a plain overwrite.
    detected_lang = _norm_lang(data.get("language"))
    write_lang = bool(detected_lang) and _enough_language_evidence(c)

    async with get_session() as s:
        # FIX-2: for the content columns, KEEP the stored value when the LLM returned
        # an empty/NULL one — a sparse (~7%) response must not wipe a fan's nickname /
        # bio / openers (which would trip the lines_empty forced-regen loop). Metadata
        # columns (counts, timestamps, call id) always take the new value.
        prof_stmt = sqlite_insert(FanProfile).values(**prof_values)
        prof_set: dict[str, Any] = {}
        for k in prof_update:
            new_val = prof_stmt.excluded[k]
            if k in _PROFILE_KEEP_IF_EMPTY:
                prof_set[k] = case(
                    (or_(new_val.is_(None), new_val == ""), getattr(FanProfile, k)),
                    else_=new_val,
                )
            else:
                prof_set[k] = new_val
        await s.execute(
            prof_stmt.on_conflict_do_update(
                index_elements=["account_id", "fan_id"], set_=prof_set
            )
        )
        # The fans row exists (scrape/WS created it); guard anyway with an upsert
        # carrying identity-safe defaults so gen_info never fails on a stray fan.
        fan_set: dict[str, Any] = dict(fact_update)
        if write_lang:
            # FIX-3, per-column: keep the operator's pin when language_source=='manual';
            # otherwise record the AI observation. Bare `Fan.language*` = the EXISTING
            # row's value in an ON CONFLICT DO UPDATE set_.
            manual = Fan.language_source == "manual"
            fan_set["language"] = case((manual, Fan.language), else_=detected_lang)
            fan_set["language_source"] = case((manual, Fan.language_source), else_="ai")
        await s.execute(
            sqlite_insert(Fan)
            .values(
                account_id=str(account_id),
                fan_id=int(c.fan_id),
                generated_nickname=nickname or None,
                grok_facts_updated_at=now,
            )
            .on_conflict_do_update(
                index_elements=["account_id", "fan_id"], set_=fan_set
            )
        )

    if client is not None:
        await _sync_of_nickname(client, account_id, c, fact_update)


async def _sync_of_nickname(
    client, account_id: str, c: "_Candidate", fact_update: dict[str, Any]
) -> None:
    """Re-assert the structured OF nickname (Name/City,Country/Age/Job) after a
    profile regen, using the SAME canonical builder the senders use so OF gets the
    identical format. This is the periodic self-heal: welcome_chatter_for_info pushes the name
    each gather tick, but once a fan graduates the name is only
    asserted once — any later enrichment (or an external edit on onlyfans.com that
    clears it back to the bare profile name) would otherwise never reach OF. We
    re-push on every regen of a STRUCTURED nickname (≥2 segments) so drift is
    corrected; bare-name-only profiles are skipped (OF already falls back to the
    profile name). Best-effort — never breaks the gen_info run.

    `fact_update` carries the freshly-written column values; we overlay them on
    `c.known` so the nickname reflects facts found THIS run."""
    def _val(col: str) -> str:
        v = fact_update.get(col)
        if isinstance(v, str) and v.strip():
            return v.strip()
        return str(c.known.get(col) or "").strip()

    ftmp = Fan(
        account_id=str(account_id), fan_id=int(c.fan_id),
        real_name=_val("real_name"),
        his_age=_val("his_age"),
        home_country=_val("home_country"),
        home_city=_val("home_city"),
        occupation=_val("occupation"),
        custom_nickname=str(c.known.get("custom_nickname") or "").strip(),
        of_display_name=str(c.known.get("of_display_name") or "").strip(),
    )
    nick = build_structured_nickname(ftmp)
    # Only push a genuinely STRUCTURED name (has at least one fact beyond the name).
    if not nick or "/" not in nick:
        return
    try:
        await push_nick_and_notes(client, account_id, int(c.fan_id), nick=nick)
    except Exception:
        log.debug("gen_info of-nickname push failed account=%s fan=%s",
                  account_id, c.fan_id, exc_info=True)


# ── Re-run gate ──────────────────────────────────────────────────────

async def _existing_profiles(
    account_id: str, fan_ids: list[int]
) -> dict[int, tuple[int, datetime | None, bool]]:
    """(message_count_at_gen, last_generated_at, lines_empty) of existing
    fan_profiles rows — feeds the Fibonacci-milestone + age + empty-lines re-run gate
    (`profile_is_stale`). `lines_empty` is True when the whole Q-class (q1/q2/q3) OR
    the whole Tease-class (tease1/2/3) has been fully consumed (all null)."""
    if not fan_ids:
        return {}
    async with get_session() as s:
        rows = (
            await s.execute(
                select(
                    FanProfile.fan_id,
                    FanProfile.message_count_at_gen,
                    FanProfile.last_generated_at,
                    FanProfile.q1, FanProfile.q2, FanProfile.q3,
                    FanProfile.tease1, FanProfile.tease2, FanProfile.tease3,
                ).where(
                    FanProfile.account_id == account_id,
                    FanProfile.fan_id.in_(fan_ids),
                )
            )
        ).all()
    out: dict[int, tuple[int, datetime | None, bool]] = {}
    for fid, cnt, gen_at, q1, q2, q3, t1, t2, t3 in rows:
        q_empty = not any((v or "").strip() for v in (q1, q2, q3))
        tease_empty = not any((v or "").strip() for v in (t1, t2, t3))
        out[int(fid)] = (int(cnt or 0), gen_at, q_empty or tease_empty)
    return out


# ── The automation entry point ───────────────────────────────────────

async def _enqueue_thin_fan_scrapes(
    account_id: str, candidates: list[_Candidate], force_ids: set[int], now: datetime
) -> set[int]:
    """Prevent "<name>/Whale with no facts": find PAYING fans with thin LOCAL history
    that we've never (recently) deep-scraped, enqueue a one-shot `scrape_chats` for
    them, and return their fan_ids so run() DEFERS profiling them this pass. See the
    _THIN_HISTORY_* / _SCRAPE_REFRESH_AGE knobs. Returns only the fans actually
    enqueued — a thin fan already scraped within the cooldown is left to profile
    normally (OF genuinely has little for them)."""
    thin = [
        c for c in candidates
        if c.fan_id not in force_ids
        and c.fan_msg_n < _THIN_HISTORY_MSG_FLOOR
        and c.spend_cents > _THIN_HISTORY_MIN_SPEND_CENTS
    ]
    if not thin:
        return set()

    # Don't re-enqueue a fan we just scraped — a thin fan stays thin until the scrape
    # lands, and scrape jobs, while idempotent (ScrapeHistory fast-skip), aren't free.
    fresh_cutoff = now - _SCRAPE_REFRESH_AGE
    async with get_session() as s:
        hist = {
            int(fid): at
            for fid, at in (await s.execute(
                select(ScrapeHistory.fan_id, ScrapeHistory.last_scrape_at).where(
                    ScrapeHistory.account_id == account_id,
                    ScrapeHistory.fan_id.in_([c.fan_id for c in thin]),
                )
            )).all()
        }
    # `thin` preserves the candidate order (spend desc), so the highest-value fans are
    # scraped first; the rest get picked up on later passes — keeps any one job bounded.
    to_scrape = [
        c.fan_id for c in thin
        if (at := hist.get(c.fan_id)) is None or at < fresh_cutoff
    ][:_AUTOSCRAPE_BATCH_MAX]
    if not to_scrape:
        return set()

    import automation_executor as ax  # lazy: avoid an import cycle at module load
    await ax.enqueue_job(account_id, "scrape_chats", payload={"fan_ids": to_scrape})
    log.info("gen_info_autoscrape account=%s deferred=%d fans=%s",
             account_id, len(to_scrape), to_scrape)
    return set(to_scrape)


@register("gen_info")
async def run(account_id: str, payload: dict, *, run_id: int) -> dict:
    """Generate/refresh fan_profiles for the qualifying fans of one account.

    payload:
      force_ids:  [fan_id, …]  bypass qualification + re-run gates.
      refill_ids: [fan_id, …]  narrow the sweep to JUST these ids but still run the
                               GATED check (profile_is_stale with lines_empty) — used
                               by the chat Lines picker when a whole Q/Tease class
                               empties. Unlike force_ids it does NOT bypass the
                               new-message guard, so a refill with nothing new no-ops.
      limit:      int          sweep ceiling (default 200).
      model:      str          override the resolved model for this run.
    """
    payload = payload or {}
    force_ids = coerce_ids(payload.get("force_ids"))
    refill_ids = coerce_ids(payload.get("refill_ids"))
    limit = int(payload.get("limit") or _DEFAULT_FAN_LIMIT)
    model = await resolve_model(account_id, _PURPOSE, payload.get("model"))
    # Localize the fan-facing openers (Q/Tease) to the account language —
    # reengage sends them verbatim. Facts stay as stated; appended at the END so the
    # stable prompt prefix (and its cache) is untouched. "" for en.
    from . import _language
    system_prompt = _SYSTEM_PROMPT + _language.qtease_directive(
        await _language.load_account_language(account_id))

    now = datetime.utcnow()
    candidates = await _gather_candidates(account_id, force_ids, limit, now)
    # Refill path: narrow to just the requested ids; they still go through the gated
    # stale check below (NOT forced) so an empty-lines refill with no new messages
    # no-ops instead of burning an LLM call.
    if refill_ids:
        candidates = [c for c in candidates if c.fan_id in refill_ids]

    # Prevention: paying fans with thin local history get a deep-scrape enqueued and
    # are deferred from this pass (profiled next pass on the real transcript).
    deferred = await _enqueue_thin_fan_scrapes(account_id, candidates, force_ids, now)
    # Re-run gate (skipped for forced ids): needs >= _MIN_NEW_MSGS new inbound msgs,
    # then regenerates on the fan's spend-tiered time window or new-message volume cap.
    prior = await _existing_profiles(account_id, [c.fan_id for c in candidates])
    todo: list[_Candidate] = []
    skipped_fresh = 0
    for c in candidates:
        if c.fan_id in deferred:
            continue
        if c.fan_id in force_ids:
            todo.append(c)
            continue
        stored = prior.get(c.fan_id)
        prev_count = stored[0] if stored else None
        last_gen_at = stored[1] if stored else None
        lines_empty = stored[2] if stored else False
        fresh_after, volume_cap = _tier_knobs(
            c.spend_cents, c.recent_spend_cents, c.subscribed_at, now)
        if profile_is_stale(prev_count, last_gen_at, c.fan_msg_n, now,
                            lines_empty=lines_empty,
                            fresh_after=fresh_after, volume_cap=volume_cap):
            todo.append(c)
        else:
            skipped_fresh += 1
    sem = asyncio.Semaphore(_MAX_CONCURRENCY)
    generated = 0
    capped = 0
    failed = 0

    # OF client for the self-healing nickname re-push (best-effort). gen_info is
    # otherwise OF-free; build it once and only when there's work, so a sweep that
    # regenerates nothing never touches OF.
    of_client = None
    if todo:
        try:
            import automation_executor as ax  # lazy: avoid an import cycle at load
            of_client = await asyncio.to_thread(ax._make_client, account_id)
        except Exception:
            log.debug("gen_info of-client init failed account=%s", account_id,
                      exc_info=True)

    async def _one(c: _Candidate) -> None:
        nonlocal generated, capped, failed
        async with sem:
            try:
                result = await llm_client.chat(
                    model=model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": _build_user_prompt(c)},
                    ],
                    purpose=_PURPOSE,
                    account_id=account_id,
                    fan_id=c.fan_id,
                    response_format={"type": "json_object"},
                    temperature=0,
                )
            except LLMCapExceeded:
                capped += 1
                log.info("gen_info_capped account=%s fan=%s", account_id, c.fan_id)
                return
            except LLMError:
                failed += 1
                log.warning("gen_info_llm_failed account=%s fan=%s",
                            account_id, c.fan_id, exc_info=True)
                return

            data = result.parsed if isinstance(result.parsed, dict) else None
            if data is None:
                failed += 1
                log.warning("gen_info_unparseable account=%s fan=%s call_id=%s",
                            account_id, c.fan_id, result.call_id)
                return
            try:
                await _persist(account_id, c, data, result.call_id, now,
                               client=of_client)
                generated += 1
            except Exception:
                failed += 1
                log.warning("gen_info_persist_failed account=%s fan=%s",
                            account_id, c.fan_id, exc_info=True)

    if todo:
        await asyncio.gather(*(_one(c) for c in todo))

    return {
        "candidates": len(candidates),
        "deferred_for_scrape": len(deferred),
        "skipped_fresh": skipped_fresh,
        "attempted": len(todo),
        "generated": generated,
        "capped": capped,
        "failed": failed,
        "model": model,
    }
