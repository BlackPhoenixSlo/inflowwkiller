"""service/automations/_daylog.py — what the creator actually DID today.

The creator-side twin of `fans.recent_events`. `_persona.py` gave her a durable
identity (age, city, job, pets — facts that never change); this gives her a DAY.

The gap it closes, measured over 7 days of real chat: of 2,232 outbound turns,
**26 (1.17%)** said anything at all about her own life. One of the highest-spending
fans on the roster asked, in as many words, how her day had gone — and got five
bubbles containing no fact, feeling or event of hers, then a question about his job.
Three causes composed:

  1. Both chat engines carried `"don't share your own info unless he asks"`.
  2. Nothing in any chat prompt said what she had been doing — `_persona`'s canon is
     26 STATIC biographical slots and `recent_events` exists only on the FAN.
  3. `account_ai_config.time_activities_json` HELD her day the whole time and had
     zero chat-engine readers — `send_welcome`/`send_followup` only. Exactly the bug
     `_persona._persona_location_line` documents for `location`, one field over.

WHY THIS IS A PRODUCER AND NOT JUST A READER. The first design simply rendered
those six slots into the prompt. Six slots are a SCHEDULE, not a life: a fan who
talks to her daily hears the same hike at 2pm on Monday, Tuesday and Wednesday, and
country dancing every single evening forever. Before, she seemed empty; after, she
would have an obviously mechanical fake life — and the daily fans, the ones who pay,
are exactly the people positioned to notice. So the six slots stay, but as a TONE
SEED handed to a generator, and the payload is a dated row that differs day to day.

Cost is one cheap call per (account, local date) — ~17/day against a fleet spend of
~$0.17/day — and it is LAZY: generated on the first chat turn that needs it, behind
a single-flight lock, so an account that talks to nobody today costs nothing. Not a
cron; a cron would pay for all 17 whether or not they ran (and `ppv_send` already
taught this codebase what a second firing path costs).

Three things the dated row buys that the static slots could not:

  BEAT IDS       Dedupe is by id, not by matching text. The model paraphrases
                 ("went for a hike earlier n im dead lol"), translates it into
                 Spanish, and the splitter cuts it across bubbles — every one of
                 which defeats a string comparison. An id survives all three.
  ONE STORY      `rhythm.COVER_BUSY` ships creator-life claims VERBATIM with no
                 model in the loop and no consistency check ("ugh work was insane
                 today", "sorry i was driving"). Those flatly contradict a day line.
                 The covers are generated as beats OF THE SAME ROW, so there is one
                 story rather than two competing ones. See `covers_for`.
  PROVENANCE     A beat id marks which span was transient, so "just got back from
                 hiking, i always go near my place in Vancouver" can still have
                 Vancouver verified as durable canon while the hike is exempt.

Every renderer returns "" when its input is unset — an account with no day log and
an account with no `time_activities` both produce a byte-identical prompt to what
shipped before this module existed. That discipline is `_persona`'s and it is what
makes this safe to wire into the highest-earning prompt in the system.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta

import llm_client                   # call .chat at runtime so tests can patch it
from db.models import Fan
from jsonsafe import load_dict
from llm_client import LLMCapExceeded

from . import _persona, _voice
from .fan_state import fan_state

log = logging.getLogger("of-relay.daylog")


# ── The six slots ────────────────────────────────────────────────────
# Byte-identical buckets to `send_followup._photo_index` / `send_welcome`, because
# the day log is the same day those two already narrate. If these drifted, the
# welcome message would claim she was at the beach while the chat engine put her on
# a trail — which is the exact class of split-brain the `location` bug caused.
SLOT_KEYS: tuple[str, ...] = (
    "morning_1", "morning_2", "afternoon_1", "afternoon_2", "evening", "night")

# Human labels for the generator prompt — the model writes a better day when it
# knows "evening" means roughly 6-9pm than when it is handed a bare key.
_SLOT_HOURS: dict[str, str] = {
    "morning_1": "5am-9am, just woken up",
    "morning_2": "9am-12pm, mid-morning",
    "afternoon_1": "12pm-3pm, early afternoon",
    "afternoon_2": "3pm-6pm, late afternoon",
    "evening": "6pm-9pm, evening",
    "night": "9pm-5am, late night",
}


def slot_index(hour: int) -> int:
    """5-9→0, 9-12→1, 12-15→2, 15-18→3, 18-21→4, else 5.

    Duplicated from `send_followup._photo_index` deliberately rather than imported:
    importing it here would make every chat reply depend on the follow-up sender's
    module, and that module runs live OF work at import in some of its siblings.
    The two are asserted equal in test_daylog."""
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


def slot_key(hour: int) -> str:
    """Slot name for a creator-local hour."""
    return SLOT_KEYS[slot_index(hour)]


def local_now(tz_offset_minutes: int | None, now: datetime | None = None) -> datetime | None:
    """Creator-local wall time, or None when the account has no timezone.

    None is load-bearing and must never be defaulted to the server clock: an account
    with no tz gets NO day log at all, the same way `_clock_line` returns "" rather
    than telling the model a time it cannot stand behind."""
    if tz_offset_minutes is None:
        return None
    return (now or datetime.utcnow()) + timedelta(minutes=int(tz_offset_minutes))


# Her day starts at 05:00, not at midnight — the same boundary the slot table uses
# (`morning_1` is 5am-9am, and `night` runs 9pm THROUGH to 5am).
#
# This is not cosmetic. On a midnight boundary, a fan messaging at 2am would roll the
# creator onto a BRAND NEW day and read its `night` beat — "home, boots off, dog on my
# feet" — describing an evening she has not lived yet. The night slot spans midnight,
# so the date must too, or the one slot most likely to be active during insomnia hours
# is also the one guaranteed to be a claim about the future.
_DAY_STARTS_AT_HOUR = 5


def local_date_str(tz_offset_minutes: int | None, now: datetime | None = None) -> str:
    """The creator's CURRENT DAY, "" when she has no timezone.

    Her local date shifted so the day turns over at 05:00 rather than midnight —
    see `_DAY_STARTS_AT_HOUR`. This is the cache key for the whole module, and it is
    her LOCAL day, never UTC's: an account at -07:00 turns over seven hours after the
    server does, and using UTC would hand her tomorrow while she is still living
    tonight."""
    lnow = local_now(tz_offset_minutes, now)
    return (lnow - timedelta(hours=_DAY_STARTS_AT_HOUR)).strftime("%Y-%m-%d") if lnow else ""


# ── The stored row ───────────────────────────────────────────────────
# `account_ai_config.day_log_json`:
#   {"date": "2026-08-06", "weekday": "Thursday",
#    "beats":  [{"id": "m1", "slot": "morning_1", "text": "..."}, ...],
#    "covers": [{"id": "c1", "kind": "busy", "text": "..."}, ...]}
#
# ONE row, overwritten each local day. Yesterday is not kept: nothing reads it, and
# a growing history in a config column is how `grok_calls` became 52% of the DB.
_BEAT_CLIP = 160            # a beat is half a sentence she would actually text
_MAX_BEATS = 6
_MAX_COVERS = 3


_MAX_TOPICS = 6
_TOPIC_CLIP = 24


def _topics(raw) -> list[str]:
    """A beat's subject tags, lowercased and clipped. [] when absent.

    Forgiving about shape on purpose — the model returns `["fishing","lake"]`,
    `"fishing, lake"` and `"errand fishing"` on different days, and a tag list is a
    ranking aid rather than a contract. A dropped tag costs one missed bridge, which
    is the safe direction; raising here would cost the whole day log."""
    if isinstance(raw, str):
        raw = re.split(r"[,;/]", raw)
    if not isinstance(raw, (list, tuple)):
        return []
    out = []
    for t in raw[:_MAX_TOPICS]:
        s = str(t).strip().lower()[:_TOPIC_CLIP]
        if s and s not in out:
            out.append(s)
    return out


# A small synonym map, used ONLY to widen the overlap search.
#
# This codebase has been burned by a lexicon before — the pins v2 keyword matcher
# kept 0 of 83 and had to be replaced by an LLM judge. The difference here is the
# COST OF A MISS. There, a wrong keyword decided whether a fan's message was used at
# all; here, a miss means she simply doesn't mention that part of her day, and a
# false positive surfaces a true thing about her to a fan who may not care. Both
# failure directions are cheap, so a flat table is the right tool and an extra model
# call would not be.
#
# Every entry earns its place from a real profile: fans write "hiking" while the day
# log writes "trail", and a canon says "Southern Rock" while a beat says
# "skynyrd". Only the domains that actually recur are here — this is not an attempt
# at a general thesaurus.
_SYNONYMS: tuple[tuple[str, ...], ...] = (
    ("hiking", "hike", "trail", "trails", "walk", "walking", "ridge", "lookout", "trek"),
    ("fishing", "fish", "trout", "salmon", "rod", "lure", "pier", "tackle"),
    ("music", "song", "songs", "band", "guitar", "playlist", "album", "skynyrd",
     "rock", "country", "vinyl", "concert", "gig"),
    ("dancing", "dance", "two-step", "twostep", "dancehall"),
    ("cooking", "cook", "baking", "bake", "dinner", "chili", "grill", "bbq",
     "barbecue", "recipe", "kitchen"),
    ("movie", "movies", "film", "films", "cinema", "western", "netflix", "watching"),
    ("dogs", "dog", "puppy", "pups", "pup", "heeler", "collie", "shelter", "animal"),
    ("gym", "workout", "lifting", "climbing", "bouldering", "running", "swim",
     "swimming", "laps"),
    ("reading", "book", "books", "novel", "magazine"),
    ("cars", "car", "truck", "driving", "drive", "road", "roadtrip"),
    ("building", "carpenter", "carpentry", "woodworking", "hardware", "renovation",
     "architecture", "heritage", "houses", "construction", "tools"),
    ("shopping", "thrift", "thrifting", "vintage", "market", "groceries", "grocery"),
    ("outdoors", "camping", "campfire", "lake", "river", "beach", "mountain", "park"),
)
_SYN_INDEX: dict[str, set[str]] = {}
for _group in _SYNONYMS:
    for _w in _group:
        _SYN_INDEX.setdefault(_w, set()).update(_group)


def _covers_of(data: dict) -> list:
    """The cover rows, wherever the model happened to put them.

    Asked for `{"beats": [...], "covers": [...]}`, the model returns the covers as a
    seventh ELEMENT OF `beats` often enough to matter — and that lands past the
    `_MAX_BEATS` slice, so the beat loop never even sees it and three good covers
    vanish without a trace. Prod's first generated day lost all three exactly this
    way. Look in both places rather than tightening the prompt and hoping: a cover
    is cheap to find here and, when missing, `rhythm` silently falls back to the
    generic pool that can contradict her actual day — which is the whole reason
    covers are generated alongside the beats."""
    top = data.get("covers")
    if isinstance(top, list) and top:
        return top
    for row in (data.get("beats") or []):
        if isinstance(row, dict) and isinstance(row.get("covers"), list):
            return row["covers"]
    return []


def parse_day_log(raw) -> dict:
    """`day_log_json` → the row, narrowed and clipped. {} for anything unusable.

    Defensive because this sits on a send path: a corrupt column must degrade to
    "she has no day today" (byte-identical prompt), never raise into a reply."""
    data = load_dict(raw)
    date = str(data.get("date") or "").strip()[:10]
    if not date:
        return {}
    beats, seen = [], set()
    for row in (data.get("beats") or [])[:_MAX_BEATS]:
        if not isinstance(row, dict):
            continue
        bid = str(row.get("id") or "").strip()[:8]
        slot = str(row.get("slot") or "").strip()
        text = str(row.get("text") or "").strip()[:_BEAT_CLIP]
        # A beat with no id cannot be deduped, and an unknown slot can never be
        # selected — either one silently becomes a line she repeats forever.
        if not (bid and text) or slot not in SLOT_KEYS or bid in seen:
            continue
        seen.add(bid)
        beats.append({"id": bid, "slot": slot, "text": text,
                      # What this beat is ABOUT, for matching against the fan (see
                      # `relatable_beat`). Model-authored free text rather than an
                      # enum: the point is to meet whatever HIS profile happens to
                      # say, and his hobbies column is free text too.
                      "topics": _topics(row.get("topics"))})
    covers = []
    for row in _covers_of(data)[:_MAX_COVERS]:
        if not isinstance(row, dict):
            continue
        cid = str(row.get("id") or "").strip()[:8]
        text = str(row.get("text") or "").strip()[:_BEAT_CLIP]
        if cid and text:
            covers.append({"id": cid, "kind": str(row.get("kind") or "busy").strip()[:12],
                           "text": text})
    if not beats:
        return {}                   # covers alone are not a day
    return {"date": date, "weekday": str(data.get("weekday") or "").strip()[:12],
            "beats": beats, "covers": covers}


def is_fresh(day_log: dict, tz_offset_minutes: int | None,
             now: datetime | None = None) -> bool:
    """Is this row today's, in HER timezone? False → regenerate."""
    today = local_date_str(tz_offset_minutes, now)
    return bool(today) and bool(day_log) and day_log.get("date") == today


def beat_for_hour(day_log: dict, hour: int) -> dict:
    """The beat covering a creator-local hour, or {} when the row has no such slot.

    Falls BACKWARD to the most recent earlier slot rather than forward: at 4pm with
    no `afternoon_2` beat, "just got back from a hike" (12pm) is still true — she
    did do it — while the evening's "getting ready to go out" is a lie about the
    future. A day log that only half-generated therefore degrades into a stale-but-
    true line instead of a confident wrong one."""
    if not day_log:
        return {}
    by_slot = {b["slot"]: b for b in day_log.get("beats", [])}
    for i in range(slot_index(hour), -1, -1):
        hit = by_slot.get(SLOT_KEYS[i])
        if hit:
            return hit
    return {}


# ── Per-fan dedupe (the beat-id ledger) ──────────────────────────────
# `fans.custom_fields._daylog` = {"date": "2026-08-06", "used": ["m1", "a1"]}
#
# Deliberately NOT `persona_claims_json`. That ledger exists because a claim cannot
# be un-said — "she already told him Argentina, so Chile is now the lie". A day
# EXPIRES. Pinning "just got back from a hike" there as durable canon would have
# `verify_self_consistency` rewrite tomorrow's beat to agree with yesterday's, which
# is the precise failure that ledger was built to prevent, inverted.
STATE_KEY = "_daylog"


def used_beat_ids(f: Fan | None, date: str) -> set[str]:
    """Beat ids this fan has already heard TODAY. Empty on a new day — the ledger is
    day-scoped, so it self-prunes and never grows."""
    st = fan_state(f, STATE_KEY)
    if not date or st.get("date") != date:
        return set()
    return {str(x) for x in (st.get("used") or []) if x}


def mark_beat_used(f: Fan | None, date: str, beat_id: str) -> dict:
    """The new `_daylog` state value after telling this fan `beat_id`.

    Pure — returns the dict for the caller to persist on the SAME event that writes
    the outbound `messages` row. A second persistence boundary is what lets
    auto-unsend and the send path disagree about whether he ever heard it."""
    if not (date and beat_id):
        return fan_state(f, STATE_KEY)
    prior = used_beat_ids(f, date)
    prior.add(str(beat_id))
    return {"date": date, "used": sorted(prior)}


# ── Does he ask what she is doing? ───────────────────────────────────
# `_persona._ASKS_ABOUT_HER_RE` is the sibling of this and CANNOT see this class: it
# matches age / name / where you live / boyfriend / kids / job / family only. It does
# not match "wyd", "how was your day", or — the message that started all of this —
# "How did the world treat my favorite person today?".
#
# So the oblique form is first-class here, not an afterthought. `treat` and the
# `how did ... today` shape are in because a real fan wrote exactly that and got
# nothing back. Kept deterministic: this decides whether an INSTRUCTION renders, so
# it must not itself cost a call.
_ASKS_ABOUT_HER_DAY_RE = re.compile(
    r"("
    # EN — direct
    r"\bwyd\b|\bw\s?y\s?d\b|"
    r"\bwhat(?:'?s| is| are| r| you| u)?[^?.!]{0,24}\b(?:up to|doing|been up to|"
    r"get up to|got up to)\b|"
    r"\bhow(?:'?s| was| is| did| has)\b[^?.!]{0,40}\b(?:day|today|evening|morning|"
    r"night|weekend)\b|"
    r"\bhow(?:'?s| was| is| did)\b[^?.!]{0,24}\b(?:treat|treating)\b|"
    r"\bwhat(?:'?d| did| have)?\s?(?:you|u|ya)\b[^?.!]{0,24}\b(?:do|been doing|get "
    r"up to)\b|"
    r"\bwhat(?:'?s| is)\s?(?:new|goin|going)\b|"
    r"\bbusy\s?(?:day|today|night)\b|"
    r"\btell me about (?:your|ur) day\b|"
    # ES
    r"\bqu[ée]\s+(?:hac[ée]s|estas haciendo|est[áa]s haciendo|hiciste)\b|"
    r"\bc[óo]mo\s+(?:estuvo|va|fue|te fue)\b[^?.!]{0,24}\b(?:d[íi]a|hoy|noche)\b|"
    r"\bqu[ée]\s+tal\s+(?:tu\s+)?d[íi]a\b"
    r")",
    re.I,
)


def asks_about_her_day(text: str | None) -> bool:
    """Is he asking what she is doing / how her day was?

    False negatives are survivable — the day block is still in the prompt and she
    MAY use it. A false positive only volunteers a true thing about her unasked,
    which is the behaviour this whole module exists to produce. So this leans
    permissive on purpose, unlike the bio-question sibling it complements."""
    return bool(_ASKS_ABOUT_HER_DAY_RE.search(text or ""))


# ── Renderers ────────────────────────────────────────────────────────
def day_block(day_log: dict, hour: int, voice: object = None) -> str:
    """The account-constant YOUR DAY block for the SYSTEM prompt. "" when there is
    no usable beat, so an un-generated account is byte-identical.

    SYSTEM side is correct and costs nothing: this is one value for all 9,290 fans
    of an account, and `_clock_line` already interpolates `%I:%M %p` into the same
    prompt — the shared prefix re-tokenizes every minute regardless, so a block
    placed beside it adds no incremental fragmentation.

    Phrased as an IMPERATIVE, following the clock line rather than the "What you
    know about him" inventory. That inventory is the one the model demonstrably
    ignores (it asked a $691 fan his job with `occupation` populated AND injected);
    the imperative clock line is the one that held."""
    beat = beat_for_hour(day_log, hour)
    if not beat:
        return ""
    v = _voice.blocks(voice)
    lane = ("" if not v.is_male else
            " Say it flat — it is information, not an invitation for him to ask "
            "about you.")
    return (
        f"YOUR DAY SO FAR: {beat['text']}\n"
        "That is WHAT HAPPENED, not a line to recite. Never quote it back word for "
        "word — say it the way it would come out of your mouth this minute, and "
        "differently every time it comes up. Lead with a different part of it, or "
        "mention a detail from it that isn't written down. It is the ONE thing about "
        "yourself you may bring up without being asked: half a sentence, in passing, "
        "never a stage direction, never twice in one conversation, and never a "
        "different version of what happened later today. NEVER invent an activity, "
        "and never stretch one to match his."
        f"{lane}\n\n")


def relatable_block(day_log: dict, hour: int | None, f: Fan | None) -> str:
    """The ALSO TODAY line — the part of her day that overlaps THIS fan. "" when
    nothing does, which is most fans.

    USER message, because it is per-fan and would otherwise fragment the prefix
    cache. It is offered, never required: a bridge that has to be forced is the
    thing that reads as a database lookup wearing a personality."""
    if hour is None:
        return ""
    beat = relatable_beat(day_log, hour, f)
    if not beat:
        return ""
    cur = beat_for_hour(day_log, hour)
    # Already the thing she is doing now — the day block covers it, and repeating it
    # here would have her mention one activity twice in one reply.
    if cur and cur.get("id") == beat.get("id"):
        return ("HE WOULD ACTUALLY CARE ABOUT WHAT YOU DID TODAY — it lines up with "
                "something he's into. Say what it was and let him have the opening; "
                "don't explain the connection or tell him it reminded you of him.\n\n")
    return (f"ALSO TODAY, EARLIER: {beat['text']}\n"
            "That one lines up with something he's into. If it fits the moment, "
            "mention it instead of (or after) what you're doing now — one clause, "
            "dropped in like it just occurred to you. Don't announce that it's "
            "something he likes, don't ask if he likes it, and skip it entirely if "
            "the conversation is somewhere else.\n\n")


# The per-turn half. `day_block` PERMITS; this REQUIRES.
#
# Permission alone is inert and was voted so by every reviewer: against a system
# prompt otherwise dominated by fan-directed material ("get to know him", "at most
# one question", "react to what he said"), "you may mention your day" loses. For
# the real message above, the model could still legally answer "aw better now that ur
# here / how was yours?" and satisfy every other rule in the prompt.
#
# Gated on BOTH conditions — he asked, AND there is an unused beat. Requiring a
# disclosure with no beat to disclose is an instruction to invent one, which walks
# straight into BIO_CONSISTENCY_GUARDRAIL.
# ── Matching a beat to HIM ───────────────────────────────────────────
# The bridge ("bet you'd love it up there") was the weakest part of the first build:
# roughly 3 rolls in 10, and zero in some threads. The cause was structural, not a
# prompt-strength problem. The model was handed her ONE current activity and his
# whole profile and asked to connect them — but if she spent the afternoon fishing
# and he is into architecture, there is nothing to connect, and the honest move is
# the one it took: say nothing. Turning the instruction up would only have bought
# forced bridges ("was hiking and thought of ur carpentry"), which is worse.
#
# So the fix is at the DAY, not the prompt: the generator now writes a day that
# spans several different domains, and this picks the part of it that actually
# overlaps THIS fan. She is not tailoring her life to him — all of it happened. She
# is doing what anyone does and leading with the part he'd care about, the way you
# tell your climbing friend about the climb and your sister about the dog.
_STOP = frozenset(
    "and the with a to my out for in of on at some little bit really just got back "
    "from her his their this that then was were been are is am not too very more "
    "into like likes love loves doing did day today going go went new other stuff "
    "things thing lot lots good great nice".split())


def _words(*vals) -> set[str]:
    out: set[str] = set()
    for v in vals:
        if isinstance(v, (list, tuple)):
            v = " ".join(str(x) for x in v)
        for w in re.findall(r"[a-zA-Z']{3,}", str(v or "").lower()):
            if w in _STOP:
                continue
            out.add(w)
            # Cheap singular/plural fold, so "dogs" meets "dog" and "houses" meets
            # "house". A stemmer would be a dependency for one rule.
            out.add(w[:-1] if w.endswith("s") and len(w) > 4 else w + "s")
            # …and the domain, so his "hiking" meets her "trail".
            out |= _SYN_INDEX.get(w, set())
    return out


def _concepts(words: set[str]) -> int:
    """How many DISTINCT things the overlap actually covers.

    Counting the words themselves scores the size of the synonym group, not the
    strength of the match: `cooking` drags in bake/barbecue/bbq/grill/kitchen/recipe,
    so on the first real day this ran, a lunchtime "leftover chili" scored 20 against
    a fan while "two-stepping at the legion hall" — a direct hit on the country music
    in his profile — scored 14 and lost. He would have heard about the leftovers all
    evening and never about the dancing. Fold each matched word back to its group so
    one shared interest counts once, however many words that group happens to spell
    it with, and the recency tiebreak gets to do its job."""
    groups, loose = set(), 0
    for w in words:
        group = _SYN_INDEX.get(w)
        if group:
            groups.add(min(group))      # any stable member names the group
        else:
            loose += 1                  # its own concept, and its own singular/plural
    return len(groups) + (loose + 1) // 2


def fan_interests(f: Fan | None) -> set[str]:
    """What HE is into, as bare words — hobbies, job, and what he's had going on.

    `fetishes` is deliberately excluded: this picks which part of her ordinary day
    to mention, and matching an errand to a fetish produces a non-sequitur at best."""
    if f is None:
        return set()
    return _words(getattr(f, "hobbies", None), getattr(f, "occupation", None),
                  getattr(f, "recent_events", None), getattr(f, "home_city", None))


def relatable_beat(day_log: dict, hour: int, f: Fan | None) -> dict:
    """The beat from EARLIER today that best overlaps this fan's interests, or {}.

    Earlier-or-current only, never later: "heading out dancing tonight" at 2pm is a
    plan, and a plan stated as a thing she did is the one lie this module must not
    tell. Ties break toward the MOST RECENT, because a thing from an hour ago is
    more natural to bring up than one from this morning.

    Returns {} when nothing overlaps — which is most fans, and is correct. A forced
    bridge reads worse than no bridge, and the prompt only ever offers this as an
    option."""
    if not day_log:
        return {}
    his = fan_interests(f)
    if not his:
        return {}
    cur = slot_index(hour)
    best, best_score = {}, 0
    for beat in day_log.get("beats", []):
        idx = SLOT_KEYS.index(beat["slot"]) if beat["slot"] in SLOT_KEYS else -1
        if idx < 0 or idx > cur:
            continue
        hits = _concepts(his & _words(beat.get("topics"), beat.get("text")))
        # `>=` so a later slot wins an equal score — recency as the tiebreak.
        if hits and hits >= best_score:
            best, best_score = beat, hits
    return best


def required_beat_id(day_log: dict, hour: int | None, f: Fan | None,
                     last_inbound: str) -> str:
    """The beat we are about to REQUIRE her to tell him, or "".

    The ledger's write key, and deliberately a PREDICTION rather than a detection.
    The alternative — read the sent reply back and decide whether it "contains" the
    beat — is the design the pre-mortem killed: the model paraphrases ("went for a
    hike earlier n im dead lol"), the Spanish lane translates it, and the splitter
    cuts it across bubbles, so a conservative match repeats the beat and a loose one
    suppresses a beat she never actually said.

    Recording what we REQUIRED needs no matching at all and is only wrong in the one
    case where the model disobeyed an explicit instruction — which costs her one
    unused beat for one day, not a repeat. The permissive case (the day is merely in
    the system prompt and she MAY mention it) is deliberately NOT recorded: she
    probably did not say it, and marking it would silence the required answer later."""
    if hour is None or not asks_about_her_day(last_inbound):
        return ""
    beat = beat_for_hour(day_log, hour)
    if not beat or beat["id"] in used_beat_ids(f, day_log.get("date", "")):
        return ""
    return beat["id"]


def day_ask_block(day_log: dict, hour: int, f: Fan | None, last_inbound: str) -> str:
    """The "he asked — answer it" instruction, or "".

    Goes in the USER message: it is keyed to THIS fan (his question, his ledger of
    already-heard beats), and per-fan text in the system prompt fragments the shared
    cached prefix — the same rule `_persona.persona_claims_block` follows."""
    if not asks_about_her_day(last_inbound):
        return ""
    beat = beat_for_hour(day_log, hour)
    if not beat:
        return ""
    if beat["id"] in used_beat_ids(f, day_log.get("date", "")):
        # He asked again and she has already told him this one. Do NOT re-assert it
        # — repeating today's only beat verbatim is the cuckoo clock in miniature.
        return ("HE IS ASKING ABOUT YOUR DAY AGAIN — you already told him what you "
                "did today, so don't repeat it. Add a small NEW detail about it or "
                "turn it back on him warmly.\n\n")
    return ("HE ASKED ABOUT YOUR DAY — answer it. Your reply MUST say what you "
            "actually did, from YOUR DAY SO FAR above, in your own words, before "
            "anything else. Don't bounce it back to him and don't answer with only "
            "a question. One beat, half a sentence — then you may ask him one "
            "thing.\n\n")


def covers_for(day_log: dict) -> tuple[str, ...]:
    """Gap-cover lines drawn from TODAY's row, for `rhythm._pick_cover`.

    () when there is no day log, and `rhythm` then falls back to its shipped pools
    exactly as before — so this is additive and an un-generated account is unchanged.

    This is the whole reason covers are generated alongside the beats. `COVER_BUSY`
    ships "ugh work was insane today" / "sorry i was driving" / "sorry babe was in
    the shower" and sends them VERBATIM: no model in the loop, no consistency check,
    nothing that could know she had told him she spent the afternoon on a trail.
    Two independent sources of "what she was doing" cannot be reconciled after the
    fact, so there is only ever one."""
    return tuple(c["text"] for c in (day_log or {}).get("covers", []) if c.get("text"))


# ── The producer ─────────────────────────────────────────────────────
_SYSTEM = (
    "You write ONE day in the life of an OnlyFans creator, as SHE would text it.\n"
    "You are given who she is, and a sample of the kind of day she has. Write a "
    "DIFFERENT, believable day for the date given — same person, same city, same "
    "habits, but not the same day.\n"
    "Respond with a SINGLE JSON object and nothing else:\n"
    '{"beats":[{"id":"","slot":"","text":""}],"covers":[{"id":"","kind":"","text":""}]}\n'
    "RULES:\n"
    "- Exactly 6 beats, one per slot, in this order: morning_1, morning_2, "
    "afternoon_1, afternoon_2, evening, night. `id` is a short unique string, and "
    "`topics` is 2-4 lowercase words naming what the beat is ABOUT ('hiking', "
    "'movie', 'cooking', 'dogs', 'thrifting', 'architecture', 'car').\n"
    "- SPREAD THE DAY ACROSS DIFFERENT AREAS OF HER LIFE. Her hobbies are what she "
    "loves, not all she does. At most TWO beats may share a topic, and the six "
    "together should touch at least four DIFFERENT areas — pick from things like: "
    "what she watched or is watching, what she cooked or ate, errands and chores, "
    "friends and family, music, something she bought or is saving for, her body "
    "(gym, bath, hair, nails), somewhere in town she went, something she fixed or "
    "made, something she read or is learning, weather, her pets, her hobbies.\n"
    "- The point of that spread is that a real person has an ordinary life around "
    "her hobbies. A day that is all fishing, or all thrift shops, is a schedule.\n"
    "- The 6 beats are ONE CONTINUOUS DAY in order. Later beats may refer back to "
    "earlier ones ('still sore from this morning'), never contradict them, and "
    "never describe the same activity twice.\n"
    "- Each `text` is HALF A SENTENCE she would actually text: lowercase, casual, "
    "concrete, 3-14 words. No stage directions, no asterisks, no roleplay. At most "
    "one emoji, usually none.\n"
    "- CONCRETE beats a generic one. 'took the dogs up the ridge trail, legs are "
    "dead' beats 'went for a walk'. Name the thing.\n"
    "- It must be an ORDINARY day. No emergencies, no drama, nothing that invites a "
    "crisis conversation, nothing sexual, nothing about filming or her job as a "
    "creator, and nothing that costs money she would have to explain.\n"
    "- NEVER contradict the facts about her. If she has dogs she does not have a "
    "cat; if she lives inland she was not at the beach.\n"
    "- 3 `covers`: short apologies for having been away from her phone, each "
    "CONSISTENT with the beats above ('sorry, was still out with the dogs'). "
    "`kind` is one of: busy, asleep, long. These explain a GAP, so they look "
    "backwards at what she was doing — never a new activity."
)


def _user_prompt(persona: str, seed_acts: dict, weekday: str, date: str) -> str:
    """The generator's user message. `seed_acts` is `time_activities_json` — the
    operator's own six lines, handed over as a TONE SEED and explicitly not as the
    answer. That distinction is the difference between this and the design it
    replaced: read literally, those six lines are the same day forever."""
    seed = "\n".join(f"- {k}: {seed_acts[k]}" for k in SLOT_KEYS
                     if str(seed_acts.get(k) or "").strip())
    slots = "\n".join(f"- {k} ({_SLOT_HOURS[k]})" for k in SLOT_KEYS)
    return (
        f"WHO SHE IS:\n{persona.strip()}\n\n"
        + (f"THE KIND OF DAY SHE HAS (tone and texture only — do NOT reuse these "
           f"lines, write a different day):\n{seed}\n\n" if seed else "")
        + f"THE SLOTS:\n{slots}\n\n"
        f"Write her day for {weekday}, {date}.")


# One in-process lock per account. The executor runs one job per (account, kind) per
# tick, but ai_chatter and of_ai_chat are DIFFERENT kinds and can both hit a cold
# cache in the same tick — that is two generations and two different days for one
# creator, which is worse than none. The DB re-read inside the lock is what makes
# the second caller adopt the first's day instead of writing over it.
_locks: dict[str, asyncio.Lock] = {}


def _lock_for(account_id: str) -> asyncio.Lock:
    lock = _locks.get(str(account_id))
    if lock is None:
        lock = _locks[str(account_id)] = asyncio.Lock()
    return lock


async def generate_day_log(account_id: str, persona: str, seed_acts: dict,
                           tz_offset_minutes: int | None, model: str,
                           purpose: str, now: datetime | None = None) -> dict:
    """Generate today's row. {} on ANY failure — cap, timeout, bad JSON, no tz.

    FAILS OPEN at every step, for the same reason `verify_self_consistency` does:
    this runs on a reply path, and a day log that cannot be produced must leave the
    prompt byte-identical rather than block or delay a message to a paying fan."""
    lnow = local_now(tz_offset_minutes, now)
    if not lnow or not persona.strip():
        return {}
    # Both stamped from the SHIFTED day (see _DAY_STARTS_AT_HOUR), so a row generated
    # at 1am is dated — and named — for the day she is still living, not the one the
    # calendar just rolled into. `date` must match `local_date_str` exactly or
    # `is_fresh` regenerates on every single turn between midnight and 5am.
    day_start = lnow - timedelta(hours=_DAY_STARTS_AT_HOUR)
    date = day_start.strftime("%Y-%m-%d")
    weekday = day_start.strftime("%A")
    try:
        res = await llm_client.chat(
            model=model,
            messages=[{"role": "system", "content": _SYSTEM},
                      {"role": "user", "content": _user_prompt(
                          persona, seed_acts or {}, weekday, date)}],
            purpose=purpose, account_id=account_id,
            # Warm. A day log at 0.0 converges on the same beats every day, which
            # is the cuckoo clock arriving through the generator instead of the
            # config — the one failure this module exists to prevent.
            temperature=0.9,
            response_format={"type": "json_object"},
        )
        parsed = getattr(res, "parsed", None)
        raw = parsed if parsed is not None else res.content
    except LLMCapExceeded:
        return {}
    except Exception:
        log.warning("day log generation failed account=%s", account_id, exc_info=True)
        return {}
    data = load_dict(raw)
    data["date"], data["weekday"] = date, weekday
    row = parse_day_log(data)
    if row:
        log.info("day log account=%s date=%s beats=%d covers=%d",
                 account_id, date, len(row["beats"]), len(row["covers"]))
    return row


# The switch. `style_config_json` despite its name is the account-wide automation
# flag blob (painful_texting, strip_emojis, the sticker percentages, the typo and
# non-native flags and the pins pair all read it), so ONE key there covers BOTH chat
# engines — which is what the day log needs, because the day is a property of the
# CREATOR, not of whichever engine happened to answer. Two engine-scoped flags could
# be flipped independently and hand the same fan two different days.
#
# DEFAULT OFF, and flippable through the merge-PUT that column already has, so
# turning it on for one account is a config change rather than a deploy.
DAY_LOG_ENABLED_KEY = "day_log_enabled"


async def load_enabled(account_id: str) -> bool:
    """Is the day log on for this account? Default OFF."""
    from db.engine import get_session
    from db.models import AccountAiConfig
    async with get_session() as s:
        cfg = await s.get(AccountAiConfig, str(account_id))
    stored = load_dict(getattr(cfg, "style_config_json", None) if cfg else None)
    return bool(stored.get(DAY_LOG_ENABLED_KEY))


async def ensure_day_log(account_id: str, cfg_row, model: str, purpose: str,
                         now: datetime | None = None) -> dict:
    """Today's row for this account — from the column, or generated and stored.

    The ONE entry point a chat engine calls, once per run. Returns {} for every
    reason it might not have a day (no tz, no persona, cap hit, bad JSON, feature
    off), and every renderer in this module maps {} to "", so the caller needs no
    branch and an account without a day log keeps a byte-identical prompt.

    SINGLE-FLIGHT. `ai_chatter` and `of_ai_chat` are different job kinds, so the
    executor's one-job-per-(account,kind) rule does NOT stop them both meeting a
    cold cache in the same tick. Without the lock that is two generations and two
    different days for one creator — a worse failure than having no day at all,
    because the two would then contradict each other in adjacent threads. The
    re-read INSIDE the lock is what makes the second caller adopt the first's day
    rather than overwrite it (the `ppv_send` double-fire lesson: the lock alone
    only serialises the writes, it does not make the second one unnecessary)."""
    if cfg_row is None:
        return {}
    from . import rhythm                      # local: rhythm imports nothing from here
    tz_off = rhythm.tz_offset_for(getattr(cfg_row, "timezone", None),
                                  getattr(cfg_row, "utc_offset", None))
    if tz_off is None:
        return {}                             # no clock ⇒ no day, same rule as _clock_line
    stored = parse_day_log(getattr(cfg_row, "day_log_json", None))
    if is_fresh(stored, tz_off, now):
        return stored

    async with _lock_for(account_id):
        # Re-read: a peer coroutine may have generated and stored today's row while
        # we waited. Adopting it is not merely an optimisation — regenerating would
        # give this engine a DIFFERENT day for the same creator on the same date.
        fresh = await _load_day_log_row(account_id)
        if is_fresh(fresh, tz_off, now):
            return fresh
        # The FULL identity — prose, location, and the structured canon — not the
        # raw `persona` column. That column is one sentence on most accounts (315
        # chars on the busiest account, and it is all hobbies and body stats), while the canon
        # beside it holds 23 filled fields: school, family, living situation, daily
        # routine, dreams, travel, upbringing, music. Handing the generator only the
        # prose is what made her days cluster on her hobbies — they were the only
        # thing it knew about her. `compose_persona` is the same composer both chat
        # engines use, so the day is written from exactly the person the reply is
        # written by.
        persona = _persona.compose_persona(cfg_row, fallback="").strip()
        seed = load_dict(getattr(cfg_row, "time_activities_json", None))
        row = await generate_day_log(account_id, persona, seed, tz_off,
                                     model, purpose, now)
        if row:
            await _store_day_log(account_id, row)
        return row


async def _load_day_log_row(account_id: str) -> dict:
    """Re-read just this column, for the inside-the-lock freshness check."""
    from db.engine import get_session
    from db.models import AccountAiConfig
    async with get_session() as s:
        cfg = await s.get(AccountAiConfig, str(account_id))
        return parse_day_log(getattr(cfg, "day_log_json", None)) if cfg else {}


async def _store_day_log(account_id: str, row: dict) -> None:
    """Persist today's row, overwriting yesterday's. Own session, never raises into
    the reply path — a day we generated but failed to store is regenerated next
    tick, which costs one call; an exception here would cost the message."""
    import json

    from db.engine import get_session
    from db.models import AccountAiConfig
    try:
        async with get_session() as s:
            cfg = await s.get(AccountAiConfig, str(account_id))
            if cfg is None:
                return
            cfg.day_log_json = json.dumps(row, ensure_ascii=False)
            await s.commit()
    except Exception:
        log.warning("day log store failed account=%s", account_id, exc_info=True)
