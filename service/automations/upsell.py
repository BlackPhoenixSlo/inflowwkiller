"""
service/automations/upsell.py — the Offer Engine: who we may sell to, and for how much.

WHAT THE DATA ACTUALLY SAID (measured on this agency's own archive, not vibes):

The seductive finding was "higher prices convert BETTER" — the marginal table says
$0-9 pays 24%, $60-69 pays 43%, $200 pays 50%. Acting on that would have been the
most expensive mistake in this file. It is Simpson's paradox. Controlling for the
FAN (same fan, offers at several price levels, his own final tap-out dropped):

    within-fan price quartile:  Q1 cheapest 43.3%  →  Q4 priciest 28.9%
    mean within-fan effect of asking more:  -23.4pp
    of 746 fans with >=2 price levels: 288 convert WORSE when asked more, 69 better

The marginal gradient is composition: a chatter only quotes $200 to a fan who is
already buying. Price was measuring the FAN, not causing the sale. On ordinary
(non-top-revenue) threads the gradient actually INVERTS: $100+ converts at 9.7%.

So the rule is:

    ** QUALIFICATION drives the sale. PRICE drives the margin. **

    cold (never bought):  16.8% @ $0-9 ... 24.0% @ $30-59 ... 16.4% @ $100+   ← elastic
    warm (already bought): 43.6% ... 38.9% ... 44.3%                          ← FLAT

Once a fan has paid once, his conversion is ~flat across price — THAT is the moment
worth money, and it is why the ladder escalates only after a paid rung. And the
timing is real: post-purchase re-offer conversion decays monotonically —
    <5min 66.7% · 5-30min 57.8% · 30m-2h 48.2% · 2-24h 47.3% · >24h 45.0%
so `strike while hot` is kept. What is NOT kept is the fantasy that we can raise
price on a cold fan and get away with it.

After an UNPAID rung, the corpus is unambiguous about the worst possible move:
    repeat the same price → 6.8% paid   (the single worst action measured)
    discount 0.75-0.9x    → 15.6%       ($5.43 EV — the best)
    halve (<0.5x)         → 19.7%       (converts, but only $2.62 EV — we're giving it away)
    raise                 → 11.1%
So an unpaid rung is answered ONCE, with a discount, and then the ladder stops.

Ships DISABLED. With `qualification_gate_enabled` false, ai_chatter/ppv_send behave
exactly as they do today (the legacy SPEND_BANDS matrix stays live for mass sends).
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from random import Random

import vault_ai_to_chatter

from ._common import norm_text

log = logging.getLogger("of-relay.automation.upsell")


def vault_ai_offer_candidates(ppv_library_config: dict | None) -> list[dict]:
    """Offer-Engine seam (S8): the armed vault-AI PPV drafts, as pickable offers.

    The 1:1 seller's own shelf is `catalog_items`; this is the seam that lets an
    approved-AND-ARMED solo draft — exported into `ppv_library_config` by the vault-AI
    consumer (correction #2) — ride the same offer path, its `suggested_script` as the
    copy and its `media_ids` as the media. Un-approved / un-armed drafts are invisible:
    the adapter gates on the library master AND the entry's own `enabled`, so nothing
    is sellable until an explicit arm. Pure pass-through to the adapter — no DB, no
    send, no mutation."""
    return vault_ai_to_chatter.armed_vault_ai_offers(ppv_library_config)

# ── Price constants. These are SAFETY choices, owned as taste — NOT EV claims.
# Every "optimal price" number anyone produced from this corpus was contaminated by
# the selection effect above, so nothing here is justified by one.
COLD_OPEN_POS = 0.35           # a first ask sits low in the item's band, but not TOO low:
                               # the BEST-executed real sales (>=3 paid rungs) open at a
                               # first paid rung of p25 $20 / p50 $25 / p75 $39 — a $10
                               # open (the near-miss "john" thread) under-prices a whale.
                               # 0.35 lands a typical band's cold open near $20-25.
COLD_OPEN_CEILING_CENTS = 5_900  # a fan who has never paid is never asked more than this
# The real conversion cliff on the escalation ratio is 3x, not 1.6x (≤1.6x→54.7%,
# 1.6-3x→52.5%, >3x→31.3%). The 1.6x line had zero predictive power and froze a $5
# fan below $7.50 forever. A plain constant — the operator has no basis to answer it.
MAX_ASK_VS_HISTORY_MULT = 3.0   # never ask >3x the largest single PPV he has EVER paid
# This is a CEILING, so no price FLOOR may approach it (see ai_chatter's
# proven_spend_floor_mult). A floor at 3x leaves a zero-width window in which to
# price — every item collapses to the same number — and past ~$67 of proven spend
# 3x exceeds OF_PRICE_MAX_CENTS, so `ceiling < floor` for the whole catalog and the
# fan is silently never offered anything again. The best buyers go dark first.
ESCALATION_MULT = 1.75          # after a PAID rung. One constant, not a preset.
DISCOUNT_BAND = (0.80, 0.90)    # of the price HE SAW — a cut of 10-20%, never more.
                                # House policy (operator, 2026-08-01): the set price is
                                # the price, with only an OCCASIONAL cut of up to 20%.
                                # This band used to open at 0.60 (a 40% cut) on the
                                # measurement that depth buys nothing — flat 20-25%
                                # conversion anywhere from 0.40x to 0.90x. That finding
                                # is why narrowing it is SAFE rather than a sacrifice:
                                # if a 40% cut converts no better than a 20% one, the
                                # extra 20 points were margin given away for nothing.
                                # 0.80 is a hard floor no LLM proposal may cross.
OF_PRICE_FLOOR_CENTS = 300      # OF rejects priced messages under $3.00
OF_PRICE_MAX_CENTS = 20_000     # ...and over $200. A HARD INVARIANT, enforced in
                                # next_price's caps, in human_cents, AND re-clamped at
                                # the send site. Nothing enforced it inside upsell before;
                                # at 3.0x on a whale the wire max is now reachable.

# Pacing / anti-abuse. Hard constants: the churn path is ASKS, and a fan who buys
# nothing never trips a spend cap — so the caps are on asks, not on dollars alone.
RUNG_GAP_S = 180                 # minimum seconds between two rungs
RESEND_AFTER_M = 90              # unpaid this long ⇒ one discount resend
STOP_AFTER_UNPAID_RUNGS = 1      # then the ladder taps out. No begging.
MAX_ASKS_PER_FAN_PER_DAY = 8     # the hot window CANNOT lift this
DAILY_ASK_CEILING_CENTS = 25_000
LADDER_OFFERS_PER_HOUR_MAX = 20  # per ACCOUNT: burst shape is what flags an OF session
LADDER_OFFERS_PER_DAY_MAX = 120
SESSION_TTL_M = 90               # a ladder frozen at hot/rung-4 for 3 weeks must not
SESSION_IDLE_M = 20              # fire "i cant resist anymore 🥵" into a dead thread
# A tap-out is a PAUSE, not a life sentence. Nothing reset `tapped`, so ONE ignored PPV
# barred a fan from every future 1:1 price FOREVER — in the 8-persona simulation, 7 of 8
# fans (including a whale who had bought twice and was still asking for content) were
# permanently un-sellable within 24h. He said "not now", not "never". A new day is a new
# chance; a PURCHASE clears it instantly (he is hot again, by definition).
TAPPED_TTL_H = 24
HOT_WINDOW_M = 120               # strike-while-hot. Was 45; a fan who JUST paid got a
                                 # 12-min liveness window while a cold fan got 24h — but
                                 # buyer p75 reply latency is 908s (15min). A 12-min
                                 # window on a proven buyer is indefensible.
HOT_ESCALATE_TTL_S = 7200        # past the hot window, DON'T lock him out — downgrade to
                                 # OPEN and re-price off band (not off last_paid×mult).
ACTIVE_TTL_S = 12 * 60           # he must have spoken this recently for a HOT rung
COLD_ACTIVE_TTL_S = 24 * 3600    # ...or this recently for a cold open
AFTERCARE_SILENCE_M = 45         # a PAID rung gone silent this long → one free warm bubble
SPEND_VELOCITY_CAP_7D = 30_000   # $300/7d paid ⇒ downgrade to companion (spend brake, not
                                 # a buy-COUNT stop, which is dead). Operator-editable VALUE.
MULTIBUY_COOLDOWN_H = (12, 18)   # after a session with >=3 paid rungs, ease off this long
PENDING_OFFER_TTL_D = 14
ARM_RATE = 0.20                  # 20% of QUOTES get an exploration multiplier
ARM_MULTS = (0.7, 0.85, 1.0, 1.2, 1.4)

STATUS_IDLE, STATUS_OPEN, STATUS_HOT = "idle", "open", "hot"
STATUS_TAPPED, STATUS_STOPPED = "tapped", "stopped"
# The safe state machine (Codex named its absence the biggest v1 gap): a fan who says
# "I just want to talk" or who is out of money is neither sellable NOR silent. COMPANION
# keeps the conversation on and the seller OFF.
STATUS_COMPANION = "companion"
STATUS_AFTERCARE = "aftercare"


# ── Decline detection ────────────────────────────────────────────────────────
# This is the most dangerous regex in the product, so it is THREE regexes with three
# different consequences. A single broad _DECLINE_RE was scored against the real
# inbound corpus and matched 4.05% of all inbounds — tapping out 37.9% OF ALL THREADS.
# Its hits included "No problem!", "No way!!!!", "A hot shower, no overthinking, comfy
# bed, hbu?" and "talk to you a lil bit later today beautiful 🥰". Wired to a shared
# 72h pause it would have been a three-day, cross-automation, UI-invisible blackout on
# more than a third of the book — and it would have passed every unit test, because
# tests only ever feed it true declines.
#
# Measured hit rates on 86,051 real inbounds: HARD 0.03% · SOFT 0.09% · BARE-NO 0.16%.

# HARD — he is angry or gone. Permanent stop, no more paid messages, ever.
_HARD_STOP_RE = re.compile(
    # `disput(e|ing)` — the pattern had only `dispute`, so "im disputing this charge"
    # (present participle: how a man actually types it while doing it) was invisible.
    # Same word-form bug as `tapped out` vs "wallet is tapping out".
    r"\b(charge ?back|refund|disput(?:e|ing) (?:this|the charge|it)|"
    r"report(?:ing)? you|scam(?:mer)?|"
    r"unsubscrib\w*|stop messaging|blocked you|blocking you)\b", re.I)

# SOFT — a poverty plea. Stop SELLING for 24h. KEEP TALKING. This is the highest-value
# moment to be a person: he is still here, he is just broke this week.
# `broke` must be the ADJECTIVE, never the past-tense verb: a bare \bbroke\b also eats
# "i broke my phone screen", "we broke up last year", "you broke me lol" — all of which
# are conversation, not refusal. So it is anchored to a copula.
_SOFT_BROKE_RE = re.compile(
    # FIRST PERSON only. `is|are` matched "my brother is broke" and "the printer is
    # broke" (dialectal "is broken") — each one a 24h selling blackout on a fan who
    # said nothing about his own wallet.
    r"\b(?:(?:i'?m|im|i am|feeling)\s+broke|"
    r"can'?t afford|cant afford|no money|lost my job|out of (?:money|cash)|"
    # ── Corpus-derived (2026-07-14). Every line below is a REAL refusal from prod that
    # the pattern above let through — and letting one through means the seller answers a
    # man who just said he is broke with a fresh price. Cody said BOTH of the first two
    # and the AI would have re-priced him.
    # Anchored the same way as _SPEND_REGRET_RE — see the warning above it. Unanchored,
    # these matched "got bills paid off finally, feeling generous" and "that's too much
    # for me to handle 😈" and blacked out the fan for a day.
    r"wallet(?:'?s| is) (?:tapping|tapped) out|"
    r"i (?:have|got) bills to (?:pay|cover)|bills to pay|"
    r"i can'?t (?:pay|tip|spend)(?: (?:more|any ?more|for|that|it))?|"
    r"i cant (?:pay|tip|spend)|"
    # Same lookahead as _SPEND_REGRET_RE — "that's too much for me to handle 😈" is dirty
    # talk, not a refusal, and without this it blacked the fan out for a day.
    r"(?:that'?s|thats|that'?ll be|thatll be) too much(?! for me to (?:handle|take))"
    r"(?= ?(?:money|for me|for my|,|\.|!|$))|"
    r"(?:waiting|wait) (?:for|til|till|until) pay ?day|"
    r"only (?:getting|get) paid (?:end|at the end|on)|"
    r"not (?:in )?the budget|money(?:'?s| is) tight)\b", re.I)

# BARE-NO — only when "no" is the WHOLE message. "no way babe 😈" is enthusiasm, not
# refusal; "talk later babe" is scheduling, not refusal. Both are excluded on purpose:
# `later` and `not now` are NOT in this regex at all.
_BARE_NO_RE = re.compile(
    r"^\W*(?:no|nope|no thanks?|no thank you|not interested|stop|no more|enough)\W*$", re.I)

DECLINE_HARD, DECLINE_SOFT, DECLINE_BARE_NO = "hard", "soft", "bare_no"


def classify_decline(text: str | None) -> str | None:
    """None = he did not decline. Evaluated BEFORE any price-objection handling, so
    "too expensive, i can't afford this" can never be routed into the haggle counter
    and upsold at a fan who just told us he is broke.

    norm_text FIRST: 14.4% of real inbound carries a curly apostrophe, and `can'?t` does
    not match `can’t`. Without it this function answered None for "Sorry can’t afford to
    buy content" — see the normalisation note in _common."""
    t = norm_text(text).strip()
    if not t:
        return None
    if _HARD_STOP_RE.search(t):
        return DECLINE_HARD
    if _SOFT_BROKE_RE.search(t):
        return DECLINE_SOFT
    if _BARE_NO_RE.match(t):
        return DECLINE_BARE_NO
    return None


# ── SPEND_REGRET — the poverty brake that actually catches poverty ───────────
# v1's _SOFT_BROKE_RE caught ~3 of 19 real distress lines, and the affordability path
# routed them into a DISCOUNT (another priced ask at a man saying stop). This detector
# is DELIBERATELY OVER-INCLUSIVE: a false stop costs one unsent ask; a false negative
# charges a man who told us he's out of money. On a hit we ALWAYS soft-stop (talk, don't
# sell) for 24h and he is NEVER discount-eligible. Red-line: ≥85% recall on the labelled
# distress corpus before ship.
# ⚠️ "Over-inclusive by design" is NOT a licence to match ordinary English. A false stop
# is not free: it is a 24h selling blackout, and the review found this pattern blacking
# out the exact men we most want to sell to —
#     "that's enough teasing, show me"              → a man BEGGING to buy
#     "i have too much money and nothing to spend"  → a whale opening his wallet
#     "got bills paid off finally, feeling generous"→ the opposite of broke
#     "that's too much for me to handle 😈"          → dirty talk
#     "i hate tipping culture in restaurants"       → small talk
#     "the printer is broke again"                  → a broken printer
# Every clause below is now anchored to a FIRST-PERSON subject and a MONEY context.
_SPEND_REGRET_RE = re.compile(
    r"\b(spent? (too much|all my|my last)|no more money|out of (money|cash|funds)|"
    r"can'?t (spend|afford)( (any)?more)?|"
    # `maxed out` — not "i'm maxed out at the gym".
    r"maxed out (my |on )?(card|credit)|i'?m maxed out(?! at)|"
    # `broke` must be the adjective AND first person. `is|are` caught broke relatives and
    # broken appliances ("the printer is broke again" — dialectal for "is broken").
    r"(?:i'?m|im|i am|feeling)\s+broke|"
    # Copula-less intensifier form: "broke af", "broke rn", "broke as hell". Common, and
    # the intensifier is what keeps it off "the printer is broke again".
    r"\bbroke (af|rn|as hell|as fuck|atm|right now)\b|"
    r"skint|"
    # Recall (2026-07-15 audit): real poverty lines the anchors were one notch too tight
    # to catch. Each still names money / a pay-deferral, so none reopens a named FP.
    r"no cash\b|short on cash|(?:i'?m|im|a bit|a little) short (this|till|until|for)|"
    r"not got the (money|funds|cash)|haven'?t got the (money|funds|cash)|"
    r"(gotta|got to|have to|need to) (pay|cover) (rent|bills|my rent)|"
    r"(next|til|till|until|wait for|after) (my )?(pay ?check|paycheque|payday)|"
    r"can'?t (swing|justify|afford) (that|this|it|spending)|"
    # "too expensive FOR ME" is a refusal; bare "its too expensive" is a haggle that
    # belongs on the discount path, not a 24h blackout — deliberately NOT matched here.
    # `much` is excluded — "too much for me" is handled by the lookahead clause below so
    # it doesn't eat "too much for me to handle 😈".
    r"too (expensive|pricey) for me\b|too (rich|pricey) for my|"
    # `that's it/all/enough` is a universal discourse marker. Only a refusal WITH its
    # money/finality suffix counts; bare "that's enough" is how he says "stop teasing".
    r"that'?s (it|all|enough) for (now|today|tonight|me)|"
    r"no more (for )?(now|today|tonight)|"
    r"(wait|till|until) (payday|i get paid|my next (pay ?check|paycheque))|"
    r"(only|can only) afford \$?\d+|budget is \$?\d+|\$?\d+ is (a lot|too much) for me|"
    r"not ready to (spend|send)( more)?|stop(ped)? (letting me|my card)|"
    r"card (declined|stopped)|lost my job|between jobs|"
    r"i'?m on a (tight )?budget(?! ?(flight|trip|airline|hotel))|"
    # ── Corpus-derived (2026-07-14). Real distress lines this brake let through: Cody
    # said "my wallet is tapping out" and "I have bills to pay" and was invisible to it.
    r"wallet('?s| is) (tapping|tapped) out|tapped out\b(?! ass)|"
    r"i (have|got) bills to (pay|cover)|bills to pay|"
    # "can't do it" alone is mid-sext ambiguous ("i can't do it without cumming"), so it
    # is NOT here — Cody's line is caught by "bills to pay" in the same sentence.
    r"i can'?t (pay|tip)( (more|any ?more|for it|that))?|i cant (pay|tip)|"
    # A refusal is about the PRICE. "that's too much for me to handle 😈" and "you're too
    # much babe" are dirty talk — the lookahead is what separates them, and without it a
    # man mid-scene got a 24h selling blackout.
    r"(that'?s|thats|that'?ll be|thatll be) too much(?! for me to (handle|take))"
    r"(?= ?(money|for me|for my|,|\.|!|$))|"
    r"too (much|expensive) for me right now|"
    r"(waiting|wait) (for|til|till|until) pay ?day|"
    r"only (getting|get) paid (end|at the end|on)|"
    r"money('?s| is) tight|not in the budget|"
    # He hates tipping ON HERE — not tipping culture in restaurants.
    r"(hate|dont like|don'?t like) tipping (on|in) (here|this|of|onlyfans))\b", re.I)


def detect_spend_regret(text: str | None) -> bool:
    """He is signalling he is out of money / done spending. Over-inclusive by design.
    A hit ⇒ unconditional 24h soft stop + COOLDOWN, never a discount. Distinct from a
    bare haggle ("can you do $30"), which alone may reach the discount path.

    norm_text FIRST — `can'?t` never matched the curly apostrophe a phone keyboard
    produces, and 14.4% of real inbound has one."""
    return bool(text) and bool(_SPEND_REGRET_RE.search(norm_text(text)))


# ── COMPANION intent — he wants to talk, not to buy ──────────────────────────
_COMPANION_RE = re.compile(
    r"\b(just (wanna|want to|like) talk(ing)?|dont want (pics|nudes|content|to buy)|"
    r"not here (to|for) (buy|content|sex)|lets just (talk|chat)|"
    r"just (like|enjoy) (talking|chatting|our chats?)|"
    r"can we just talk|dont need (pics|content))\b", re.I)


def detect_companion_intent(text: str | None) -> bool:
    """He declines content while asking to keep talking. Seller OFF, conversation ON."""
    return bool(text) and bool(_COMPANION_RE.search(norm_text(text)))


# ── STATED CAP — his WORDS, an advisory ceiling only ─────────────────────────
# The one anchor we honour (the operator's "let's keep it at 30"). It is NOT price
# personality (that is right-censoring, not a type) — it is a number he actually typed
# inside a cap/counter-offer frame. The regex supplies the cents; there is NO LLM marker.
#
# ⚠️ THE BARE `\bfor` WAS A LOADED GUN. It read a "stated cap" out of any "for <digits>":
#     "been up for 18 hours babe im exhausted"  -> cap = $18
#     "i worked for 12 hours today"             -> cap = $12
# And detect_stated_cap feeds _fan_pull(), which in qualify() is the ONE thing that lifts
# `offers_paused_until` — the 24h pause the POVERTY BRAKE writes. So: he says "my wallet
# is tapped out" at 14:00 (pause written, correctly), mentions at 14:20 that he's been up
# for 18 hours, and the gate re-opens and prices him. The single worst thing this system
# can do, reachable from a man saying he's tired.
# `for` now requires a currency marker; the bare-number frames must name a cap explicitly.
_STATED_CAP_RE = re.compile(
    r"(?:can you (?:do|make it)|could you do|would you do|how about|i can (?:do|pay|afford)|"
    r"my (?:limit|budget|max) is|(?:keep|stay|leave) (?:it )?(?:at|to)|"
    r"\bfor \$|i'?ll (?:pay|give you|do)|(?:no more than|max)|is (?:my )?(?:limit|budget|max))"
    r"\s*\$?\s*(\d{1,3})(?!\d)", re.I)
# A number in an escalation/enthusiasm frame is NOT a cap ("I'd pay 200 for the right one").
_CAP_NEGATION_RE = re.compile(r"\b(no|not|isn'?t|too much|expensive)\b", re.I)


def detect_stated_cap(text: str | None) -> int | None:
    """The price HE named as his ceiling, in cents, or None. Advisory + one-directional:
    it may only LOWER the ask, never raise the floor. Range-clamped and rate-limited by
    the caller (a fan-writable price API must be defended). Evidence: next ask ≤1.6× the
    cap → 52% buy; >1.6× → 6%."""
    if not text:
        return None
    text = norm_text(text)
    m = _STATED_CAP_RE.search(text)
    if not m:
        return None
    # A cap stated inside a negation frame ("that's too much, not paying 50") is a
    # complaint about a price, not a counter-offer — let the decline/discount path own it.
    span_start = max(0, m.start() - 20)
    if _CAP_NEGATION_RE.search(text[span_start:m.start()]):
        return None
    try:
        dollars = int(m.group(1))
    except (TypeError, ValueError):
        return None
    if dollars < 3 or dollars > 200:       # outside OF's wire range — not a real cap
        return None
    return dollars * 100


# ── Pricing ──────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class FanState:
    fan_id: int
    max_single_paid_cents: int = 0     # the LARGEST single PPV he has ever actually paid
    last_paid_cents: int | None = None  # the rung he just bought (None = never)
    has_ever_paid: bool = False


@dataclass(frozen=True)
class Quote:
    price_cents: int
    base_cents: int
    arm_mult: float
    pre_clamp_cents: int
    clamped_by: str | None
    band_lo: int
    band_hi: int


def media_key(media_ids) -> str:
    """Stable key for a media SET. Keyed on media, not catalog_item_id, because a fan
    who bought a clip through a MASS blast has no content_offers row at all — a
    catalog-keyed ownership check would cheerfully re-sell him what he already owns."""
    if isinstance(media_ids, str):
        try:
            media_ids = json.loads(media_ids) or []
        except Exception:
            media_ids = []
    ids = sorted(int(x) for x in (media_ids or []))
    if not ids:
        return ""
    return hashlib.sha1(",".join(str(i) for i in ids).encode()).hexdigest()[:16]


def stable_hash(*parts) -> int:
    return int(hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:12], 16)


def arm_mult(account_id: str, fan_id: int, rung_index: int, key: str) -> float:
    """The price experiment randomises the QUOTE, not the FAN.

    Assigning an arm per-fan (hash(account, fan) % 5) would make it a randomised *fan*
    experiment with price as a nuisance variable: the armed fan's whole ladder history
    shifts, which is exactly the ladder-position contamination the arm exists to escape.
    Per-quote, the fan is his own control across rungs."""
    r = Random(stable_hash(account_id, fan_id, rung_index, key))
    if r.random() >= ARM_RATE:
        return 1.0
    return r.choice(ARM_MULTS)


def human_cents(dollars_cents: float, account_id: str, rng: Random,
                *, max_cents: int | None = None, min_cents: int = OF_PRICE_FLOOR_CENTS) -> int:
    """Whole dollars + a human-looking cents tail, ALWAYS inside [min_cents, max_cents].

    Humans in the archive priced at $35.20, $45.20, $50.20, $75.55 — never a uniform
    ending. round_to_99()'s fixed `.99` on 100% of sends is a perfect bot fingerprint,
    and so was the first proposal here (a 6-value alphabet at 100% frequency). The
    tail is drawn from a per-account seeded distribution weighted toward the endings
    the humans actually used, with support everywhere.

    The bound is not decoration: the tail is added AFTER the price is clamped, so
    without it a $200.00 quote becomes $200.99 — and OF rejects any priced message
    over $200 outright. The ceiling wins over the disguise."""
    cents = max(min_cents, int(round(dollars_cents)))
    whole = max((cents // 100) * 100, (min_cents // 100) * 100)
    seeded = Random(stable_hash("cents", account_id, rng.random()))
    pool = [0, 20, 20, 50, 50, 55, 69, 99] + list(range(0, 100, 7))
    out = whole + seeded.choice(pool)
    if max_cents is not None and out > max_cents:
        out = whole if whole <= max_cents else int(max_cents)   # drop the tail, keep the bound
    return max(min_cents, out)


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(v, hi))


def next_price(*, fan: FanState, band: tuple[int, int], last_paid_cents: int | None,
               rung_index: int, key: str, account_id: str, rng: Random,
               library_bounds: tuple[int, int],
               stated_cap_cents: int | None = None,
               escalation_mult: float | None = None,
               max_ask_vs_history_mult: float | None = None,
               proven_floor_cents: int = 0,
               catalog_floor_cents: int = 0) -> Quote | None:
    """The price for the next rung — or None, meaning DO NOT OFFER THIS ITEM.

    `return None` is the load-bearing line in this module. The obvious design applies
    the fan-history cap AFTER the item's floor, which quotes a fan whose largest paid
    PPV was $9 a flagship finish video at $13.50 — and then the ladder's monotonicity
    freezes it there forever. Here, an item he cannot plausibly afford is simply not
    offered, and the item selector picks a cheaper one instead. The ladder climbs by
    UNLOCKING BETTER ITEMS, which produces a 9→19→39→59→79 shape structurally, rather
    than by a multiplier fighting three caps."""
    lo, hi = band
    lo_b, hi_b = library_bounds
    # Operator-tunable aggressiveness (Upsell tab). Both default to the data-backed
    # constants; raising max_ask_vs_history_mult past 3.0 is the "to the moon" lever,
    # but conversion halves past 3x his history (52%→31%) — the UI says so.
    esc_mult = ESCALATION_MULT if escalation_mult is None else float(escalation_mult)
    hist_mult = (MAX_ASK_VS_HISTORY_MULT if max_ask_vs_history_mult is None
                 else float(max_ask_vs_history_mult))

    # Ceiling first, floor last — so a floor that cannot survive the ceiling yields
    # NO OFFER rather than a cheap one. Every candidate ceiling is named, and the
    # BINDING one is recorded on the quote: a quote silently truncated at the library
    # max would otherwise bias every estimate the price arm produces, for an unknown
    # subset of fans.
    # The item's own ceiling is the WIDER of the band's 1.5x and 3x the operator's
    # sticker (house policy: "you can price the content up to 3x the set price").
    # A sticker-derived band tops out at 1.4x, so `hi * 1.5` alone capped every such
    # item at 2.1x its own price — below the stated policy, and invisibly so.
    # MAX, not a third `caps` entry: `caps` is minimised below, so an entry there could
    # only ever LOWER the ceiling and could never raise it to 3x.
    item_caps = [(int(hi * 1.5), "band")]
    if catalog_floor_cents:
        item_caps.append((int(catalog_floor_cents * 3.0), "sticker_3x"))
    caps: list[tuple[int, str]] = [
        max(item_caps, key=lambda c: c[0]),
        (int(hi_b), "library"),
        (OF_PRICE_MAX_CENTS, "of_max"),     # HARD invariant — the $200 wire max, always
    ]
    # These two are COMPLEMENTARY and must be gated on the SAME fact, or there is a
    # fan for whom neither fires. `has_ever_paid` is true for a man who only ever
    # TIPPED (its caller derives it from the fan's cumulative-payment field), but the
    # history cap needs a proven SINGLE payment — so a man who tipped $5 and never
    # unlocked a PPV had max_single_paid_cents=0 (no history cap) AND has_ever_paid
    # True (no cold ceiling), falling through both and leaving only the band.
    # That was invisible while the band was the $3-$20 constant and capped him at $30;
    # sourcing the band from the item's sticker opened it to the full $200 catalog.
    # Measured 2026-07-27: 160 prod fans have tipped (avg $31, up to $200) and never
    # bought a PPV. Gate both on max_single_paid_cents so exactly one always applies.
    #
    # PAYING NEVER LOWERS YOUR CEILING. The history cap is floored at the cold-open
    # ceiling a fan who has NEVER paid already gets, because the acquisition strategy
    # deliberately drives the first purchase down to the $3 wire minimum — and a $3
    # first buy read literally puts his ceiling at $9, i.e. BELOW the $59 he had while
    # he was still a stranger. Buying would make him less sellable than not buying.
    # Measured 2026-08-01 on Isabelle (337749380): a fan whose only purchases were two
    # $3 convo teasers could be quoted 2 of her 15 catalog items; the other 13 returned
    # None and he was silently offered nothing at all.
    #
    # A cheap trial is CENSORED evidence — it proves "he will pay at least $3", never
    # "he will pay at most $9". Only a purchase that clears the cold-open ceiling is
    # evidence of a HIGHER ceiling, which is exactly what `max` keeps: past $19.67 paid
    # (3 x 19.67 > 5900) history takes over and the floor stops binding.
    if fan.max_single_paid_cents:
        caps.append((max(int(fan.max_single_paid_cents * hist_mult),
                         COLD_OPEN_CEILING_CENTS), "history"))
    if last_paid_cents is None and not fan.max_single_paid_cents:
        caps.append((COLD_OPEN_CEILING_CENTS, "cold_ceiling"))
    # His stated cap ("keep it at 30") is a CEILING only, never a floor. Applied at 1.6x
    # (next ask ≤1.6x cap still converts at 52%). It may LOWER the ask / bias selection
    # cheaper; it must NEVER raise the floor (that empties the catalog and freezes him).
    if stated_cap_cents:
        caps.append((int(stated_cap_cents * 1.6), "stated_cap"))
    ceiling, ceiling_src = min(caps, key=lambda c: c[0])

    # PROVEN-SPEND FLOOR: a fan who has already paid $X should not be re-offered a
    # CHEAPER item — the ladder climbs by unlocking BETTER items, so an item priced
    # below what he proved he'll pay yields NO OFFER here and the selector moves up
    # a tier (a $50 buyer gets the $60 video, not the $24 set re-run at cold-open
    # lows). Unlike the stated cap, this floor is INTENTIONAL and only ever raises
    # the floor for a PROVEN buyer; it's opt-in (default 0 → old behavior). If it
    # can't survive the ceiling the item is simply skipped, never discounted.
    #
    # CATALOG (SOLO) FLOOR: the same rule for the item's OWN sticker. The base 1:1
    # chatter sells a single at the price the operator set on it (`price_cents`);
    # smart pricing derives a band from PAST ASKS and the account median, which for
    # a cold fan lands LOW in that band and could quote the single BELOW its
    # sticker — i.e. the upseller undercutting the chatter's own solo price. Making
    # the sticker a floor means smart pricing may only ever price a single AT or
    # ABOVE what it's set to; a fan who can't clear that at his ceiling is offered a
    # cheaper single instead, never this one at a discount.
    floor = max(lo, lo_b, OF_PRICE_FLOOR_CENTS,
                int(proven_floor_cents or 0), int(catalog_floor_cents or 0))
    if ceiling < floor:
        return None                      # not qualified for THIS item. Do not discount it.

    if last_paid_cents:
        want = last_paid_cents * esc_mult            # he just paid — strike while hot
    else:
        want = lo + (hi - lo) * COLD_OPEN_POS        # cold open: LOW in the band

    base = _clamp(want, floor, ceiling)
    arm = arm_mult(account_id, fan.fan_id, rung_index, key)
    pre_clamp = want * arm                           # what the model wanted, unclamped
    px = _clamp(pre_clamp, floor, ceiling)

    # `clamped_by` names the bound that actually MOVED the price, or None if the quote
    # is what the model wanted. This is the experiment's own instrument: a quote
    # silently truncated at a ceiling, recorded as if it were free, biases every price
    # estimate — so the clamp is measured against the UNCLAMPED intent, not against
    # the already-clamped base.
    clamped_by = None
    if abs(px - pre_clamp) > 0.5:
        clamped_by = ceiling_src if pre_clamp > ceiling else "floor"

    return Quote(
        price_cents=human_cents(px, account_id, rng,
                                max_cents=int(ceiling), min_cents=int(floor)),
        base_cents=int(base), arm_mult=arm, pre_clamp_cents=int(pre_clamp),
        clamped_by=clamped_by, band_lo=lo, band_hi=hi)


def discount_price(last_sent_cents: int, rng: Random, account_id: str) -> int:
    """The one answer to an unpaid rung. Computed off the price HE ACTUALLY SAW (not a
    pre-arm base he never saw), so a "discount" can never arrive ABOVE the original.
    The arm is NOT re-applied here — one arm per (fan, media) event, ever."""
    lo, hi = DISCOUNT_BAND
    px = int(last_sent_cents * rng.uniform(lo, hi))
    px = max(OF_PRICE_FLOOR_CENTS, min(px, last_sent_cents - 1))
    return px


@dataclass(frozen=True)
class DiscountCtx:
    """What may_discount needs. Assembled by ai_chatter."""
    last_inbound_text: str = ""
    objection_at: datetime | None = None   # when a price objection last arrived
    last_inbound_at: datetime | None = None
    discount_count_this_item: int = 0      # cuts already given on THIS media
    cuts_last_30d: int = 0                 # the reinforcer governor numerator
    asks_last_30d: int = 0
    spend_regret: bool = False             # distress is NEVER a discount trigger


def may_discount(ctx: DiscountCtx) -> tuple[bool, str]:
    """He must WORK for a cut, and only a VOICED affordability objection earns one.

    95.8% of our historical cuts were unprompted and converted at HALF the rate
    (20.7% vs 42.9%). The operator's "they'll learn to haggle" fear is measurably
    backwards (discounted fans later haggle LESS) — so we do NOT penalise a haggle;
    we gate on an ask and rate-limit the REINFORCER, not the price."""
    if ctx.spend_regret:
        return False, "distress"           # a broke man is soft-stopped, never discounted
    # 1. ASK REQUIRED — a voiced affordability objection (soft decline, stated cap, or a
    #    haggle line). No voiced objection ⇒ no cut; the answer is a PIVOT to a cheaper item.
    has_objection = bool(
        classify_decline(ctx.last_inbound_text) == DECLINE_SOFT
        or detect_stated_cap(ctx.last_inbound_text)
        or ctx.objection_at is not None
    )
    if not has_objection:
        return False, "unprompted"
    # 2. THE BEAT — at least one fan turn since the objection. Never cut on the same
    #    breath he asked. This IS "he must work for it"; the data says more earns nothing.
    if ctx.objection_at is not None and ctx.last_inbound_at is not None \
            and ctx.last_inbound_at <= ctx.objection_at:
        return False, "same_turn"
    # 3. ONE CUT PER ITEM.
    if ctx.discount_count_this_item >= 1:
        return False, "already_cut"
    # 4. THE GOVERNOR — cap how RELIABLY an objection pays off (the reinforcer), without
    #    a haggle penalty (which is backwards). >35% of asks ending in a cut ⇒ pivot.
    if ctx.asks_last_30d >= 4 and ctx.cuts_last_30d / max(1, ctx.asks_last_30d) > 0.35:
        return False, "governor"
    return True, "ok"


def derive_band(*, human_asks_cents: list[int], account_median_cents: int | None,
                item_price_cents: int = 0,
                override: tuple[int, int] | None = None) -> tuple[tuple[int, int], str]:
    """The item's plausible price range → ((lo, hi), source).

    Content SHOULD constrain price (a 12-minute video is not a selfie) — but it cannot
    be derived from duration, because `catalog_items.duration_sec` is populated on
    0 of 9 rows in prod: a duration-derived tier taxonomy would classify the entire
    vault by a NULL. So the band is derived EMPIRICALLY from what humans actually
    charged for this exact media, then the account's own median ask, then the price
    the OPERATOR put on this item.

    That third tier is not decoration. Without it an account whose every priced row is
    a blast or the seller's own — both correctly excluded from the ask history by the
    deflationary-spiral filter — got the `fallback` constant for EVERY item, and since
    the ceiling is `hi × 1.5` that pinned the whole account at $30. Measured 2026-07-27:
    4 of 8 accounts, all 346 of their quotes at (300, 2000), with 10 of one account's 12
    catalog rungs unquotable to ANY fan for six days. An operator who prices an item at
    $90 has stated what it is worth; that beats a constant written for a cold account.

    `src` is provenance and is reported as such — a sticker-derived band must never
    claim to be an `account` one, or every estimate computed off these quotes is
    attributed to a median that was never involved."""
    if override:
        return (int(override[0]), int(override[1])), "override"
    asks = sorted(int(x) for x in (human_asks_cents or []) if x)
    if asks:
        median = asks[len(asks) // 2]
        return (int(median * 0.6), int(median * 1.4)), "empirical"
    if account_median_cents:
        return (int(account_median_cents * 0.6), int(account_median_cents * 1.4)), "account"
    if item_price_cents:
        return (int(item_price_cents * 0.6), int(item_price_cents * 1.4)), "sticker"
    return (OF_PRICE_FLOOR_CENTS, 2_000), "fallback"


# ── The gate — this IS the feature ───────────────────────────────────────────
@dataclass(frozen=True)
class GateCtx:
    fan_id: int
    last_inbound_text: str | None = None
    last_inbound_at: datetime | None = None
    last_outbound_at: datetime | None = None
    fan_spoke_last: bool = False
    status: str = STATUS_IDLE
    offers_paused_until: datetime | None = None
    last_ask_at: datetime | None = None
    asks_today: int = 0
    daily_ask_cents: int = 0
    account_offers_last_hour: int = 0
    account_offers_today: int = 0
    ladder_may_open: bool = True     # rhythm: not within 30min of her bedtime
    fan_pull: bool = False           # HE explicitly asked to buy this turn (content-ask /
                                     # "send it" / named a price) — his own initiation


def qualify(ctx: GateCtx, now: datetime) -> tuple[bool, str]:
    """May we put a PRICE in front of this fan right now? (ok, reason).

    Live conversational signal only — lifetime spend is NOT an input. Selling into
    silence is the actual gap: it is what ppv_send does today (0.26% paid), and it is
    the one claim that survived every review round."""
    from ._common import is_qualifying_inbound

    if ctx.status == STATUS_STOPPED:
        return False, "hard_stop"
    if ctx.status == STATUS_TAPPED:
        return False, "tapped"
    if ctx.offers_paused_until and ctx.offers_paused_until > now and not ctx.fan_pull:
        # He said he's broke → we don't PUSH. But the "broke" line is real distress only
        # ~85% of the time: measured, 11% of fans who say it still buy within 24h, 18%
        # within 7d. The realness test is whether HE comes back PULLING — an explicit
        # "send it" / content-ask / a named price. We never proactively re-offer during
        # the pause, but if he initiates a buy himself, we honour it (that is not farming
        # a broke man — that is a man telling us the "broke" wasn't final).
        return False, "offers_paused"        # he said he's broke. Talk, don't sell.

    if not ctx.fan_spoke_last:
        return False, "we_spoke_last"        # never stack an offer on our own message
    if not is_qualifying_inbound(ctx.last_inbound_text):
        return False, "low_information"      # "k" / "ok" / "lol" is not a buying signal
    if classify_decline(ctx.last_inbound_text):
        return False, "declined"

    if ctx.last_inbound_at is None:
        return False, "no_inbound"
    age = (now - ctx.last_inbound_at).total_seconds()
    hot = ctx.status == STATUS_HOT
    if age > (ACTIVE_TTL_S if hot else COLD_ACTIVE_TTL_S):
        return False, "stale"

    if ctx.last_ask_at and (now - ctx.last_ask_at).total_seconds() < RUNG_GAP_S:
        return False, "rung_gap"
    if ctx.asks_today >= MAX_ASKS_PER_FAN_PER_DAY:
        return False, "fan_daily_asks"       # the hot window cannot lift this
    if ctx.daily_ask_cents >= DAILY_ASK_CEILING_CENTS:
        return False, "fan_daily_cents"
    if ctx.account_offers_last_hour >= LADDER_OFFERS_PER_HOUR_MAX:
        return False, "account_hourly"       # burst shape is what flags an OF session
    if ctx.account_offers_today >= LADDER_OFFERS_PER_DAY_MAX:
        return False, "account_daily"
    if ctx.status != STATUS_HOT and not ctx.ladder_may_open:
        return False, "near_bedtime"         # don't start what we can't finish

    return True, "ok"


def tap_expired(status: str, updated_at: datetime | None, now: datetime) -> bool:
    """Has a tap-out served its time? A `tapped` fan is one who said "no" once, or let a
    PPV go unopened. That is a mood, not a verdict — and it must decay, or the ladder is
    a one-way door that silently retires every fan who ever ignored a message.

    STOPPED does NOT decay: that fan threatened a chargeback. Only an operator clears him."""
    if status != STATUS_TAPPED or updated_at is None:
        return False
    return (now - updated_at) >= timedelta(hours=TAPPED_TTL_H)


def session_expired(status: str, last_ask_at: datetime | None,
                    session_idle_at: datetime | None, now: datetime) -> bool:
    """Enforced ON READ. A ladder left at hot/rung-4 three weeks ago must not wake up
    and fire "i cant resist anymore 🥵" into a dead thread."""
    if status not in (STATUS_OPEN, STATUS_HOT):
        return False
    if last_ask_at and (now - last_ask_at) > timedelta(minutes=SESSION_TTL_M):
        return True
    if session_idle_at and (now - session_idle_at) > timedelta(minutes=SESSION_IDLE_M):
        return True
    return False
