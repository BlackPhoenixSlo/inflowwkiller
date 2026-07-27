"""
_pins.py — which of a fan's OWN messages the model gets to re-read later.

THE PROBLEM
-----------
`gen_info` flattens a fan into fields, and a field is an INDEX, not the substance.
One live fan carries `occupation: service technician for properties`. Behind that
five-word field sits a 695-character paragraph he wrote himself, about taps,
ventilation, winter heating and playground safety. The field is what survives into
every prompt; the paragraph is what would actually make her sound like someone who
listened. Nothing in the stack could get it back — history scrolls past, and the
extractor only ever wrote the five words.

An OF chat pin is a per-thread slot that survives scrollback, and we already have
the two API halves for it. So: pin his own long-form message, and read it back
into the prompt. **Pins are INPUT for the model** — not decoration for the fan, who
per the 2026-07-27 probe does not see them at all. The secondary win is the human
chatters', who get a rail with the substance on it instead of the index.

THE SHAPE — reader and writer, one module
-----------------------------------------
    classify(text, lang)  →  pure. Is this message pinnable, and how rich is it?
    pins_block(fan)       →  the READER. Renders state into the prompt. FREE.
    consider(...)         →  the WRITER. Reads OF, decides, pins, evicts.

The reader ships WITH the writer on purpose: a pin nothing reads is a write-only
feature, and the first cut of every "remember this" system dies there.

WHY THE READER COSTS NOTHING
----------------------------
The single `filter=pinned` OF call lives inside the WRITER, and on its way past it
captures EVERY pin it sees — including the ones humans made — into
`fans.custom_fields["_pins"]`. So the reader only ever touches local state, and can
run on every reply of every engine without a budget. That is what makes "read
lazily, only when a candidate exists — never on a sweep" affordable: the read is
not a reader cost at all, it is the writer's, and the writer is rare.

It also picks up 6 human pins that already exist across 96 threads and that nothing
in the stack has ever read.

WHY A REJECT LIST AND NOT A RANKER
----------------------------------
Two ranking shortcuts were tried against real prod data and BOTH pick the wrong
message:

  • Rank by length. The two longest inbound messages on the primary thread are
    3113 and 1943 characters and both are explicit fantasy scripts. The job
    paragraph is 695 — fourth.
  • Reuse the extractor's source message. Extraction is fill-empty-only, so it
    cites the FIRST mention: `fans.occupation` came from a 47-character message on
    07-21, while the paragraph worth pinning landed on 07-26. It reliably picks
    the thin one.

So selection is topic-anchored first and rich second, and it is a whole-message
REJECT scan rather than a score threshold. The failure that shape exists to stop is
specific and is not hypothetical: a calmly worded message about losing a job,
separating from a wife, and when the kids are out of the apartment. No explicit
term, no price, no illness — it passes every naive filter, and it would pin a man's
crisis to the top of an erotic chat with an absence schedule attached.

ANY hit in ANY category rejects the WHOLE message. There is no scoring its way out,
and no per-category severity. A false negative costs one un-pinned paragraph; a
false positive is the paragraph above.

WHY ENGLISH ONLY
----------------
The reject lexicon is English. On a Spanish thread it would be blind — it would
scan a message it cannot read and return "nothing to reject", which is the worst
possible answer. `lang != "en"` is therefore a hard no-pin, not a degraded mode.
Adding a language is adding its lexicon, and nothing else.

WHERE THE STATE LIVES
---------------------
`fans.custom_fields["_pins"]`, via `fan_state` — the canonical accessor for the
shared blob (every writer must read the whole blob and write it back, so
hand-rolling the cycle clobbers a sibling automation's key). No migration: this
is per-fan scratch state, which is exactly what that column is.

The blob holds LIVE state only — what is pinned right now. The audit trail
(pinned, skipped, evicted, and why) is the log, because it is append-only by
nature and a blob that accumulates history grows without a bound anyone owns.
Both are greppable; only one of them has to be pruned.

SHADOW FIRST
------------
`enabled` turns the computation on; `write` turns the OF mutation on. Enabling
gives you `pins_shadow` log lines and NOTHING pinned — read them, judge them, and
flip `write` only if the picks hold up. Kill criterion, owner-set: fewer than 90%
of shadow picks correct on a human read.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import datetime, timedelta
from typing import NamedTuple

from sqlalchemy import select

from db.engine import get_session
from db.models import AccountAiConfig, Message
from jsonsafe import load_dict

from ._persona import _pinned_block
from .fan_state import fan_state, set_fan_state

log = logging.getLogger("of-relay.automation.pins")

STATE_KEY = "_pins"          # the fans.custom_fields slot (see fan_state.py)

# The two `style_config_json` keys. NAMED, and exported, because that column's
# write path is an ALLOWLIST (`style_config_api._persist`) that drops unknown keys
# silently on a 200 — a feature whose key is not in it ships permanently inert.
# Both must appear there and in `test_style_config._flag_readers`.
PINS_ENABLED_KEY = "pins_enabled"    # compute + log (shadow)
PINS_WRITE_KEY = "pins_write"        # ALSO mutate OnlyFans

# Bumped whenever the rules below change. Stamped on every record, because it is
# the only way to ask "what did the OLD classifier pin?" after a rule moves — and
# the first thing you want when a pin turns out to be wrong.
CLASSIFIER_VERSION = 1

MIN_CHARS = 200              # below this it is a remark, not a paragraph
_READ_MIN_CHARS = 40         # below this a pin is prompt noise (see _safe_to_read)
MIN_ANCHORS = 2              # "multiple topic signals in one neutral factual span"
CAP = 2                      # how many pins WE keep on a thread (see CAP below)
WRITE_COOLDOWN = timedelta(hours=24)   # at most one mutation per fan per day
_TEXT_CLIP = 700             # what we store and render per pin

# CAP is a QUALITY budget, not an API limit. The 07-27 probe held two pins on one
# thread with no error and the real OF ceiling is somewhere above ten. Two is what
# a prompt can carry without the pinned block starting to outweigh the live
# conversation it is supposed to be supporting.


def _now() -> str:
    """The state blob's timestamp format, in one place — it is written from three
    paths now and compared by `_cooling_down`."""
    return datetime.utcnow().isoformat()


def _off() -> bool:
    """The GLOBAL kill switch, read at call time so it can be flipped by env on a
    restart without touching any account's config. The per-account switch is
    `enabled` on `consider`; this one turns the feature off everywhere at once."""
    return os.getenv("PINS_OFF") == "1"


# ── §5.4 SELECTION ───────────────────────────────────────────────────
# Every pattern below is matched against the WHOLE normalised message. Order is
# presentation only — the scan does not stop early, because the log wants to name
# the category that rejected, and the FIRST hit is the most useful name for it.

_TAGS = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def _rejected(body: str) -> str | None:
    """The first reject category `body` trips, or None when it is clean. `body`
    must already be `_match`ed — the patterns are lowercase.

    ONE scan, shared by the writer's `classify` and the reader's `_safe_to_read`.
    It was written twice, which also left the reader with no way to say WHY a pin
    silently stopped reaching the prompt."""
    return next((name for name, pattern in _REJECT if pattern.search(body)), None)


def _clean(text: str | None) -> str:
    """Message body → the text we STORE and SHOW THE MODEL. Strips OF's
    ProseMirror tags and collapses whitespace. Case is left alone.

    Its own step rather than a reach into `of_ai_chat._strip_html`: that module
    imports THIS one, and a leaf that imports its caller is a cycle waiting for
    the next import to close it. Whitespace collapsing matters independently —
    without it a phrase split across a newline slips every multi-word pattern,
    which is a reject-list hole and not merely untidy."""
    return _WS.sub(" ", _TAGS.sub(" ", text or "")).strip()


def _match(text: str | None) -> str:
    """`_clean` + casefold — the form every pattern in this file is written
    against, and the ONLY thing casefolding is for.

    Split from `_clean` because one function doing both quietly casefolded the
    text on its way into storage: a fan who wrote "I work as a Technician for the
    Council" was pinned as "i work as a technician for the council". The whole
    pitch of this feature is that the model re-reads HIS OWN WORDS, so a matching
    concern had no business reaching the stored copy."""
    return _clean(text).casefold()


_REJECT: tuple[tuple[str, re.Pattern[str]], ...] = (
    # Explicit / kink. Bare anatomy counts HERE, deliberately unlike
    # `_common.ESCALATION_RE` — that one dropped bare anatomy because it was
    # optimising LIFT for a buying-intent gate and the words had widened into
    # noise. A reject list optimises RECALL, and noise is the goal.
    ("explicit", re.compile(
        r"\b(fuck|fucking|cock|dick|pussy|tits|boobs|ass|arse|cum|cumming|horny|"
        r"orgasm|nude|nudes|naked|blowjob|handjob|anal|kink|kinky|fetish|bdsm|"
        r"spank|choke|moan|wet|hard on|hard-on|jerk|wank|masturbat|suck|lick|"
        r"stroke|panties|lingerie|strip|sexy|sex|turn me on|make me cum)\b")),
    # Money / price. "paid work" and "the company pays well" fall in here and are
    # lost as occupation material. That is the trade: a pinned line that mentions
    # what he earns is a pinned line about what he can be charged.
    ("money", re.compile(
        r"(\$|€|£|\b(pay|paid|paying|price|priced|cost|costs|cheap|expensive|"
        r"afford|money|cash|salary|wage|income|earn|rent|mortgage|tip|tipped|"
        r"subscri|refund|bank|paypal|card|broke|budget|dollars?|euros?)\b)")),
    ("health", re.compile(
        r"\b(sick|ill|illness|hospital|doctor|surgery|cancer|diagnos|meds|"
        r"medication|therapy|therapist|depress|anxiety|panic|injur|disab|covid|"
        r"clinic|treatment|recovery|rehab|chronic|painkiller)\b")),
    # First-person distress. The category the opening failure case lives in.
    ("distress", re.compile(
        r"\b(divorce|divorced|separated|separating|split up|widow|passed away|"
        r"died|death|funeral|grief|grieving|debt|loan|evict|homeless|fired|"
        r"laid off|redundan|unemploy|lost my job|out of work|custody|court|"
        r"lawyer|solicitor|lawsuit|legal|abuse|abusive|addict|alcoholic|jail|"
        r"prison|bankrupt|struggling|suicid|lonely|depressed)\b")),
    # Credentials, contact details, exact workplace, addresses, schools,
    # itineraries — anything that turns a paragraph into a way to find him.
    ("identifiers", re.compile(
        r"(\S+@\S+|\bhttps?://|www\.|\.com\b|\.net\b|\+\d{7,}|\b\d{7,}\b|"
        r"\b(whatsapp|telegram|snapchat|snap|instagram|insta|kik|discord|email|"
        r"e-mail|phone number|my number|address|street|avenue|road|apartment|"
        r"postcode|zip code|password|username|login|school|college|university|"
        r"kindergarten|daycare|nursery|flight|airport|hotel|passport|licence|"
        r"license plate)\b)")),
    # Identifying third parties. A named or related person is someone who did not
    # consent to being in an erotic chat's permanent slot.
    ("third_party", re.compile(
        r"\b(wife|husband|girlfriend|boyfriend|fianc|my ex|ex-wife|ex wife|"
        r"my partner|son|daughter|kids|children|my child|mom|mum|mother|dad|"
        r"father|sister|brother|grandma|grandpa|granddaughter|grandson|nephew|"
        r"niece|neighbour|neighbor|roommate|flatmate|my friend)\b")),
    # Schedules — the PRECISION is what makes it a schedule. Vague routine prose
    # ("up late, films in the afternoon") is the topic we WANT and carries none of
    # these. A clock time or a named recurring day is an availability window, and
    # an availability window attached to a home is the second half of the opening
    # failure case.
    # Widened 2026-07-27 by the FIRST shadow run, which is what shadow is for.
    # It surfaced a real fan message giving a full week of availability — "have to
    # be there by 8.... Monday and Thursday 730 as it's order day … swing shift …
    # there at 1130, half hour break at 4 and back on at 430" — and the pattern
    # below let it through on three counts at once: `730`/`1130`/`430` are not
    # `\d{1,2}:\d{2}`, `by` was not a preposition here, and "Monday and Thursday"
    # is neither "every Monday" nor "on Mondays".
    #
    # So: ANY weekday name now counts. Naming the days you are somewhere IS an
    # availability window, and the vague routine prose this topic wants ("up
    # late, films in the afternoon") never names one. And a preposition followed
    # by digits counts up to four of them, which catches bare 24h clock times.
    # "at 3 different sites" trips it too — a false positive costs one un-pinned
    # paragraph, which is the direction this list is built to fail in.
    ("schedule", re.compile(
        r"(\b\d{1,2}\s*(am|pm)\b|\b\d{1,2}[:.]\d{2}\b|"
        r"\b(mon|tues|wednes|thurs|fri|satur|sun)days?\b|"
        r"\b(from|until|till|between|after|before|by|at|around) \d{1,4}\b)")),
    # Roleplay / hypothetical framing. A bare `if` is in here on purpose and it is
    # the widest pattern in the file by a distance. It is kept because the thing
    # it guards against — a fantasy premise pinned as though it were a fact about
    # his life — is indistinguishable from fact once the framing scrolls away, and
    # an un-pinned paragraph costs nothing.
    ("hypothetical", re.compile(
        r"\b(if|imagine|pretend|suppose|hypothetical|roleplay|role play|"
        r"fantas(y|ise|ize|ies)|would be|wish i|let's say|lets say|what would|"
        r"dream about)\b")),
    # Mixed topic, the residual. Everything above already carries most of what
    # "mixed" means (money, health, people, sex are each their own reject); this
    # catches the remaining domains that are neither occupation nor routine.
    ("offtopic", re.compile(
        r"\b(politic|election|president|government|church|jesus|religio|pray|"
        r"war|vaccine|conspiracy|crypto|bitcoin|stocks?|gambl|betting)\b")),
)

# The two topics we start with, and the anchor terms that say a message is ABOUT
# one of them. `MIN_ANCHORS` distinct anchors must hit before a message counts as
# anchored at all — one stray "work" in a paragraph about something else is a
# mention, not a topic.
_TOPICS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("occupation", re.compile(
        r"\b(work|works|working|worked|job|shift|shifts|boss|manager|client|"
        r"clients|customer|customers|colleague|coworker|office|site|sites|crew|"
        r"trade|apprentice|contractor|technician|engineer|mechanic|plumber|"
        r"electrician|carpenter|builder|driver|nurse|teacher|chef|repair|"
        r"repairs|fixing|install|installing|maintenance|tools|overtime|career|"
        r"profession|company|workshop|warehouse|factory|machine|machines)\b")),
    ("routine", re.compile(
        r"\b(usually|normally|routine|every day|everyday|most days|these days|"
        r"wake up|get up|breakfast|lunch|dinner|cook|cooking|gym|workout|"
        r"training|running|walk|walking|commute|weekend|weekends|after work|"
        r"before work|evenings|mornings|afternoons|nights|bedtime|hobby|"
        r"hobbies|fishing|hiking|camping|garden|gardening|reading|guitar)\b")),
)


class Pick(NamedTuple):
    """A message judged pinnable. `score` is factual density — how many distinct
    anchor terms of its topic it hit — and it picks the topic WITHIN this one
    message (occupation vs routine). There is no candidate set to rank: `consider`
    classifies exactly one message, the fan's latest inbound. Beyond that the
    score is a shadow-log field, so a human reading the log can see how strongly
    anchored a pick was."""
    topic: str
    score: int
    text: str


def classify(text: str | None, lang: str = "en") -> Pick | None:
    """The whole of §5.4 in one pure function: `Pick` if this message may be
    pinned, `None` if it may not.

    PURE and cheap on purpose — it is the gate every other cost in this module
    sits behind, so it must be safe to run on a message that will never be
    pinned, which is nearly all of them."""
    if lang != "en":                    # see WHY ENGLISH ONLY
        return None
    clean = _clean(text)                # what we would STORE — his own words
    body = clean.casefold()             # what the patterns run against
    if len(clean) < MIN_CHARS:
        return None
    hit = _rejected(body)
    if hit:
        log.debug("pins_reject category=%s chars=%d", hit, len(clean))
        return None
    best: Pick | None = None
    for topic, pattern in _TOPICS:
        anchors = {m.group(0) for m in pattern.finditer(body)}
        if len(anchors) >= MIN_ANCHORS and (best is None or len(anchors) > best.score):
            best = Pick(topic, len(anchors), clean[:_TEXT_CLIP])
    return best


# ── §5.3 THE READER ──────────────────────────────────────────────────

def pins_block(fan) -> str:
    """The fan's captured pins, as the block the model reads. `""` when he has
    none — which is nearly every fan, and which must render a byte-identical
    prompt to the one that shipped before this module existed.

    IMPERATIVE, not an inventory, and that is not a style preference. A passive
    "what you know about him" list is the block the model demonstrably ignores:
    measured on prod, it asked a $691 fan his job with `occupation` populated AND
    injected. The imperative clock line is the one that held, so this copies its
    voice — it tells her what to DO with the paragraph, not merely that it exists.

    Goes in the USER message at every call site, never the system prompt: this is
    per-fan text, and per-fan text in the system prompt fragments the shared
    cached prefix and costs multiples of the block's own tokens.

    EVERY line is re-scanned by `_safe_to_read` on the way out — see there. The
    writer's scan is not sufficient, because most of what lands in state was never
    put there by the writer.

    CAPPED by `_rank`, and this is the cap that actually bounds the prompt. The
    writer only reconciles a thread on a turn it is already writing to — trimming
    on any other turn would mean an OF read every turn, the one cost §5.2 refuses
    — so a thread humans kept pinning to stays over budget on OnlyFans, and this
    slice is what stops that becoming N extra paragraphs competing with the live
    conversation."""
    safe = [p for p in _have(fan_state(fan, STATE_KEY))
            if _safe_to_read(str(p.get("text") or ""))]
    lines = [f"- {p['text']}" for p in _rank(safe)[:CAP]]
    return _pinned_block(
        "HE WROTE THIS TO YOU HIMSELF, IN HIS OWN WORDS. Treat it as something you "
        "remember about him and bring a SPECIFIC detail from it into the conversation "
        "when it fits — ask him how that part is going. Never quote it back at him, "
        "never recite it as a list, and never let on that you have it saved:", lines)


def _safe_to_read(text: str) -> bool:
    """May this pin be shown to the model? The reject scan, at READ time.

    Two reasons it cannot live only in the writer:

      1. MOST PINS IN STATE WERE NOT CHOSEN BY US. `_capture` deliberately takes
         every pin on the thread, human ones included — that is the point of
         reading, and there are 6 such pins across 96 threads. A human's pin has
         been through nobody's reject list. Feeding one to the model would route
         around §5.4 entirely: a chatter pins "his wife left him last month" as a
         note-to-self, and the erotic chat's next reply riffs on it. Verified on a
         live thread — the first shadow run rendered two unvetted human pins
         straight into the prompt, which is what put this function here.
      2. THE RULES MOVE. A pin chosen under classifier v1 outlives the lexicon
         that chose it. Re-scanning at read makes a tightened rule apply to
         everything already pinned, immediately, with no backfill and no
         migration — which is what `cv` is for and the only cheap way to honour it.

    Length floor, not `MIN_CHARS`: the writer's floor asks "is this a paragraph
    worth pinning", and a human pinning a one-liner has already answered the
    is-it-worth-it question their own way. This floor only asks whether the line
    carries enough to be a fact in a prompt — a four-character pin is noise
    competing with the live conversation for the model's attention.

    Matches against `_match`, NOT the stored text. Stored text keeps its
    capitalisation now, and every pattern here is lowercase — scanning the raw
    copy would silently stop matching "Divorce" the moment storage stopped
    casefolding."""
    if len(_clean(text)) < _READ_MIN_CHARS:
        return False
    hit = _rejected(_match(text))
    if hit:
        log.debug("pins_hidden category=%s", hit)
    return hit is None


def _have(state: dict) -> list[dict]:
    """The pin records on a state blob, defensively. A slot written by an older
    version, or half-written, must read as "no pins" rather than raise three lines
    into a prompt builder."""
    have = state.get("have") if isinstance(state, dict) else None
    return [p for p in have if isinstance(p, dict)] if isinstance(have, list) else []


def _id(record: dict) -> int:
    """A pin's message id, from one of our records OR one of OF's rows — the same
    coercion either way, which is why it is a function and not the seven inline
    copies it used to be."""
    return int(record.get("id") or 0)


def _rank(pins: list[dict]) -> list[dict]:
    """Which pins matter most: OURS first, then most recent.

    ONE answer, shared by the reader (what to render) and the writer (what to
    keep). They used to disagree — the reader kept ours and dropped humans, the
    writer deleted ours and protected humans — and two implementations of one
    policy did exactly what that always does. On a thread already holding CAP
    human pins the writer pinned the fan's paragraph, immediately unpinned it,
    logged success and stamped the 24h cooldown, so it repeated every day and
    never pinned anything. Sharing the order makes that unrepresentable.

    Ours first is also the owner's stated call — we MAY unpin a human's pin — and
    the paragraph the model needs outranks a note nobody has ever read."""
    def newest(group: list[dict]) -> list[dict]:
        return sorted(group, key=lambda p: str(p.get("at") or ""), reverse=True)
    return (newest([p for p in pins if p.get("src") == "us"])
            + newest([p for p in pins if p.get("src") != "us"]))


# ── §5.5 THE WRITER ──────────────────────────────────────────────────

async def load_flags(account_id: str) -> tuple[bool, bool]:
    """`(enabled, write)` for one account — the per-account half of the switch.

    Lives in `account_ai_config.style_config_json`, which despite its name is the
    account-wide automation-flag blob already (painful_texting, strip_emojis, the
    cat-sticker percentages, the typo and non-native flags all read it). Two keys
    there is migration-free AND flippable through the merge-PUT that column
    already has, which is what makes shadow→live a one-line change on a live
    account rather than a deploy.

    BOTH default OFF. `enabled` alone gives you shadow: the picks are computed and
    logged, and nothing on OnlyFans is touched. `write` is the second, separate
    flip, and it is the one that should wait for a human to have read the log."""
    async with get_session() as s:
        cfg = await s.get(AccountAiConfig, str(account_id))
    stored = load_dict(getattr(cfg, "style_config_json", None) if cfg else None)
    return bool(stored.get(PINS_ENABLED_KEY)), bool(stored.get(PINS_WRITE_KEY))


async def consider(account_id: str, fan, last_body: str | None, client, *,
                   lang: str = "en", enabled: bool = False,
                   write: bool = False) -> None:
    """One turn's worth of pin work for one fan. Never raises into a send path.

    Takes the engine's ALREADY-BUILT `client` rather than constructing one: both
    call sites hold one by the time they get here, and a second OFClient for a
    turn that will almost always decide "no candidate" is pure waste. It also
    means this module needs no construction seam of its own — the engine's fake
    is the fake.

    THE COST LADDER, cheapest first — every rung is the gate for the next:
        1. two booleans                    (kill switches)
        2. a length check on text in hand  (cuts ~all turns, no I/O)
        3. one regex pass                  (`classify`, pure)
        4. one local SELECT                (the id + the UNCLIPPED body)
        5. one regex pass on the full body (authoritative — see below)
        6. ONE OF read                     (the only recurring network cost)
        7. the OF writes                   (only when `write`)

    Rung 3 runs on `last_body`, which the engines clip at 400 characters, so a
    reject term living in the tail of a 695-character paragraph is invisible to
    it. That is why rung 5 exists and why it re-classifies the FULL body: rung 3
    can only produce false ACCEPTS, and rung 5 is where they die. Never collapse
    the two — the clipped pass is a cost gate, not a safety one."""
    if _off() or not enabled:
        return
    try:
        fan_id = int(getattr(fan, "fan_id", 0) or 0)
        if not fan_id or len(_clean(last_body)) < MIN_CHARS:
            return
        if classify(last_body, lang) is None:
            return

        row = await _latest_inbound(account_id, fan_id)
        if row is None:
            return
        message_id, body = row
        pick = classify(body, lang)          # authoritative pass on the full text
        if pick is None:
            return

        state = fan_state(fan, STATE_KEY)
        if any(_id(p) == message_id for p in _have(state)):
            return                           # already ours; idempotent by id
        if _cooling_down(state):
            return

        # The ONE OF read — and the capture. Everything already pinned on this
        # thread lands in state here, human pins included, BEFORE anything is
        # evicted. That ordering is the whole reason the reader is free, and it is
        # also the only moment we ever learn a human pinned something.
        pinned = await asyncio.to_thread(client.get_pinned_messages, fan_id)
        state = _capture(state, pinned)

        if not write:
            log.info("pins_shadow account=%s fan=%s msg=%s topic=%s score=%d "
                     "held=%d cv=%d | %s", account_id, fan_id, message_id,
                     pick.topic, pick.score, len(_have(state)), CLASSIFIER_VERSION,
                     pick.text[:300])
            await set_fan_state(account_id, fan_id, STATE_KEY, state)
            return

        state = await _reconcile(account_id, fan_id, client, state, {
            "id": message_id, "topic": pick.topic, "text": pick.text, "src": "us",
            "at": _now(), "cv": CLASSIFIER_VERSION})
        await set_fan_state(account_id, fan_id, STATE_KEY, state)
    except Exception:                        # noqa: BLE001
        # A pin is never worth failing a reply over. The reply is the product.
        log.warning("pins_failed account=%s fan=%s", account_id,
                    getattr(fan, "fan_id", "?"), exc_info=True)


def _cooling_down(state: dict) -> bool:
    """At most one mutation per fan per 24h. Read off the state's own stamp rather
    than a separate cooldown key, so "when did we last change his pins" has one
    answer and cannot drift from what is actually pinned."""
    raw = state.get("at") if isinstance(state, dict) else None
    try:
        return datetime.utcnow() - datetime.fromisoformat(str(raw)) < WRITE_COOLDOWN
    except (TypeError, ValueError):
        return False                         # never stamped, or corrupt → not cooling


def _capture(state: dict, pinned: list[dict]) -> dict:
    """OF's pinned list → state, preserving what we already knew about each pin.

    OF is the TRUTH about which messages are pinned — a human may have unpinned
    ours between turns — so anything absent from `pinned` drops out. But OF cannot
    tell us the topic or the classifier version that put a pin there, so a record
    we already hold is kept and only its text refreshed. Pins we have never seen
    before are captured as `human`, which is exactly the 6 pins across 96 threads
    that nothing has ever read."""
    known = {_id(p): p for p in _have(state)}
    have = []
    for m in pinned:
        mid = _id(m)
        if not mid:
            continue
        text = _clean(m.get("text"))[:_TEXT_CLIP]
        prior = known.get(mid)
        if prior:
            have.append({**prior, "text": text or prior.get("text", "")})
        else:
            have.append({"id": mid, "topic": "", "text": text, "src": "human",
                         "at": _now(), "cv": 0})
    return {**(state if isinstance(state, dict) else {}), "have": have}


async def _reconcile(account_id: str, fan_id: int, client, state: dict,
                     candidate: dict) -> dict:
    """Make OF's pinned set match what we want it to be. Returns the new state.

    A RECONCILIATION, not a sequence of pins and evictions: name the set we want,
    diff it against what OF actually holds, apply the difference. Three things
    that the imperative version needed guards for fall out of the framing:

      • `pin_message` IS A TOGGLE — POSTing an already-pinned id UNPINS it and
        still answers `{"success": true}` (see of_client.pin_message). Here we
        only ever POST ids in `to_add`, which is BY CONSTRUCTION disjoint from
        what OF holds. The hazard is unreachable rather than guarded against.
      • A candidate that would not survive the cap is never POSTed at all, so the
        old "pin it, then immediately evict it, then log success" cannot happen.
      • Nothing is removed until the add is verified up, so a failed add can
        never leave the thread holding less than it started with.

    `candidate` REPLACES its own stale record rather than joining it, so a message
    already on the thread but MISSING FROM STATE (the desync that found the toggle)
    re-stamps rather than re-pinning. When state does hold it, `consider` returns
    before reaching here — that check is the cheap one and shadows this path."""
    current = _have(state)
    current_ids = {_id(p) for p in current}
    desired = _rank([p for p in current if _id(p) != _id(candidate)]
                    + [candidate])[:CAP]
    desired_ids = {_id(p) for p in desired}
    to_add = [p for p in desired if _id(p) not in current_ids]
    to_remove = [p for p in current if _id(p) not in desired_ids]

    if to_add:
        for p in to_add:
            await asyncio.to_thread(client.pin_message, _id(p), fan_id)
        after = await asyncio.to_thread(client.get_pinned_messages, fan_id)
        landed = {_id(m) for m in after}
        if not landed.issuperset({_id(p) for p in to_add}):
            # Stamped even though it FAILED, because the cooldown rations write
            # ATTEMPTS and we just spent one. Stamping only successes bounds
            # nothing: a message OF accepts but never shows re-POSTed on every
            # reply turn forever — measured at 5 POSTs over 5 turns — and since
            # `pin_message` is a toggle, a merely-lagging list gets flipped on and
            # off on alternate turns.
            log.warning("pins_add_unverified account=%s fan=%s add=%s — nothing "
                        "removed, cooling off", account_id, fan_id,
                        [_id(p) for p in to_add])
            return {**_capture(state, after), "at": _now()}
    for p in to_remove:
        await asyncio.to_thread(client.unpin_message, _id(p), fan_id)
        log.info("pins_evicted account=%s fan=%s msg=%s src=%s cv=%s",
                 account_id, fan_id, _id(p), p.get("src"), p.get("cv"))

    if not to_add and not to_remove:
        log.info("pins_adopted account=%s fan=%s msg=%s topic=%s — already on the "
                 "thread, claimed without a write", account_id, fan_id,
                 _id(candidate), candidate.get("topic"))
        return {**state, "have": desired}
    log.info("pins_applied account=%s fan=%s added=%s removed=%s topic=%s cv=%d",
             account_id, fan_id, [_id(p) for p in to_add],
             [_id(p) for p in to_remove], candidate.get("topic"),
             CLASSIFIER_VERSION)
    # The cooldown stamps a MUTATION, so adoption above leaves it alone — a turn
    # that changed nothing on OnlyFans must not spend the fan's daily write.
    return {**state, "have": desired, "at": _now()}


async def _latest_inbound(account_id: str, fan_id: int) -> tuple[int, str] | None:
    """The fan's newest inbound message — id and UNCLIPPED body.

    The engines only run a turn when the fan spoke last, so "newest inbound" IS
    the message the turn is about, and asking the DB is what gets us both the id
    (needed to pin) and the full text (needed to reject safely). Deliberately not
    matched by body against what the engine holds: that text has been HTML-
    stripped, clipped and had a vision tag glued on, so an equality match would
    fail on exactly the long messages this feature is for."""
    async with get_session() as s:
        row = (await s.execute(
            select(Message.message_id, Message.body)
            .where(Message.account_id == str(account_id),
                   Message.fan_id == int(fan_id),
                   Message.direction == "in",
                   Message.is_unsent.is_(False))
            .order_by(Message.created_at.desc(), Message.message_id.desc())
            .limit(1))).first()
    return (int(row[0]), str(row[1] or "")) if row else None


# OFClient is synchronous `requests` and every call here is inside an async
# automation turn, so each one goes through `asyncio.to_thread` — a blocked loop
# starves the relay's OF calls, a failure mode this codebase has already hit.
# There used to be a one-line `_to_thread` wrapper around it; it was `asyncio`'s
# own function with a docstring, and it could not stop a call site forgetting
# either. The stdlib name is clearer at the point of use.
