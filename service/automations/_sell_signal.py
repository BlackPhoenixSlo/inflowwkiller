"""service/automations/_sell_signal.py — did he just tell us he wants to buy?

## What this is

Two readers of one question, in one module.

**The model's**, a line it may append to its reply and the fan never sees:

    SELL: yes

That is the whole protocol. It answers ONE question — *did he ask to see or buy
something this turn?* — and the answer is a boolean.

**The code's**, for the two ways a man says yes without phrasing it as an ask:
`is_price_ask` ("how much?") and `is_affirmation` ("yes") — the latter only
against an offer we ourselves just made (`is_offer_shaped`). `wide_ask`
composes them into the provenance the lane carries. See "The widened readers".

## Why it is a boolean and not the JSON it started as

The first design carried a payload: `{"wants":…,"kind":…,"who":…,"heat":…}`. Every
field in it is already re-derived downstream by `content_resolver.read_contract`,
which reads the same thread with a dedicated prompt and returns a typed `Contract`
— subject, media kind, company, strictness, his exact quote. A second, weaker copy
of that judgement, produced by a model that is busy writing flirty prose, is not a
second opinion; it is a worse one competing with the good one.

So the signal supplies the only thing the resolver cannot: whether to spend the
call at all. `TRIGGER, NOT CONTRACT` is the whole design.

That also collapses the failure mode. A malformed JSON blob is a message a fan can
receive; `SELL: yes` that fails to parse is a two-word line the strip removes
anyway, and if the strip somehow missed it the fan reads "SELL: yes" instead of a
paragraph of braces.

## Why it exists at all

The trigger today is `CONTENT_ASK_RE`, and every gap in it has been found the same
way: in production, by a fan who asked and got banter. "Do you send ass pics?"
matched nothing until 2026-08-12 because the subject sits between the verb and the
media word, which no fixed option list can hold. The model reads the message
anyway to write the reply; asking it one more yes/no costs nothing.

## ARMED 2026-08-15 — `decide()` is an OR

It shipped shadow (return the regex verdict, log the disagreement) on the reasoning
that "the model said so" is not evidence before it is measured. The operator ruling
is to measure it in production instead: the roster runs small accounts for exactly
this case, and the shadow week could only ever have been collected by turning the
prompt block on anyway — so this arms and the same two log lines become the numbers.

Live in `ai_chatter` and `autoreply`. **`welcome_chatter_for_info` is still regex-only** — it was
not part of the ruling, and it is one import plus three lines when it should be.

## The widened readers (2026-08-29)

The trigger only ever asked "did he ASK", so the two turns where a sale is won
without an ask were invisible to every engine: a price question ("how much?"),
and a plain "yes" to an offer we just made in prose. Both are read here, in code,
because the model reader is measurably reluctant to sell (one live account: 184
replies, 4 offers, with the gate approving nearly all of them) — a prompt rule is
not a trigger.

Two rules keep them honest, and both are load-bearing:

  * **An affirmation counts only against our own offer.** "yes" answers anything;
    what makes it a buy signal is what it answers. `is_offer_shaped` reads the
    text of our latest outbound, which both calling engines already hold — no
    state, no stamp, no new query. Timestamps are NOT the guard: `autoreply`
    leaves `Turn.our_last_at` unset on purpose, so a recency rule would have
    silently disabled the whole feature in the engine that needs it most.
  * **Provenance travels with the turn, never with the words.** The engine that
    fired a widened reader stamps `Turn.wide_ask`; the lane reads the stamp. So
    an engine that never opted in (`welcome_chatter_for_info`, whose own prompt
    says not to offer content) is excluded by construction rather than by a
    name check in the lane.
"""
from __future__ import annotations

import logging
import re
from random import Random

from ._common import norm_text, strip_emojis
from ._language import norm_lang
from ._markers import protocol_marker_re

log = logging.getLogger("of-relay.automation.sell_signal")

# `Turn.wide_ask` values — also the `via=` in every log line this reader causes.
# Three, not two, so the riskiest reader can be measured — and switched off — on
# its own. A single "affirmation" bucket would hide the tease behind the sure thing.
WIDE_PRICE = "price"
WIDE_AFFIRMATION = "affirmation"
WIDE_TEASE = "tease"

# How often a "yes" to a TEASE (as opposed to a plain offer) is taken as a sale.
# Operator ruling 2026-08-29, and the fraction is the whole point: a tease is a
# real selling moment and an ambiguous one, so it is played sometimes rather than
# always. Per-account override `tease_sell_rate`; 0 switches the reader off.
TEASE_SELL_RATE = 0.33

# The colon carries the discrimination, exactly as it does for `STICKER:` — "sell"
# is an ordinary word in a real body ("i sell customs"), "SELL:" is not.
_MARKER_RE = protocol_marker_re(r"SELL[ \t]*:")

# Wrapper punctuation + sentence enders the model dresses a marker in.
_TAIL_TRIM = "*_~`\"'()[]{}.!, "

# What the model is told. One line, appended to the prompt's output contract —
# which says "no JSON, quotes, or metadata", and this is the sanctioned exception.
# The example is not decoration: on this roster an EXAMPLE reliably beats a rule,
# and the agreement case is the one the old wording ("asked to SEE or BUY") told
# the model to stay silent on — an answer is not an ask.
BLOCK = (
    "SELL SIGNAL — if his last message asked to SEE or BUY something, OR said "
    "yes to / asked the price of something on offer, end with a final line "
    "exactly:\n"
    "SELL: yes\n"
    "(e.g. you: \"want me to send u something?\" him: \"yes\" → SELL: yes)\n"
    "no buy intent → no line. stripped before he sees it — say NOTHING about "
    "content or prices."
)


def parse(raw: str) -> tuple[str, bool]:
    """Split a draft into (text the fan sees, he-asked).

    Strips EVERY occurrence, not just the first: the audit that produced
    `_markers` found models emitting a marker inline, mid-sentence, and twice.
    Text before a marker survives — both known leaks carried real fan-facing words
    ahead of the marker — and the marker runs to end of line.
    """
    if not raw:
        return raw or "", False
    hits = list(_MARKER_RE.finditer(raw))
    if not hits:
        return raw, False
    clean = _MARKER_RE.sub("", raw)
    # A tail of "yes"/"true" is the protocol; anything else is the model narrating
    # ("SELL: I shouldn't"), which is not a yes. The tail is stripped of the
    # markdown/roleplay dressing models wrap markers in — `*SELL: yes*` reaches
    # here as `yes*`, and the audit that produced `_markers` found exactly that
    # shape in production for both existing protocols.
    said_yes = any(h.group(1).strip().lower().strip(_TAIL_TRIM) in ("yes", "true")
                   for h in hits)
    return clean.strip(), said_yes


# ── The widened code readers ────────────────────────────────────────
#
# Canonicalisation borrows the house helpers rather than rolling its own — the same
# emoji stripper `is_qualifying_inbound` uses, and `norm_text`'s smart-punctuation
# fold. It is not that gate's exact tokenizer (this matches whole messages, that one
# counts tokens), but the two can never disagree about what an emoji or a curly
# apostrophe is, which is where a hand-rolled strip would drift.
_TRIM = " \t\n*_~`\"'()[]{}.!?,;:-…"


def _canon(text: str | None) -> str:
    """Lowercased, emoji-stripped, punctuation-trimmed. `norm_text` folds the smart
    punctuation a phone keyboard inserts; it does not lowercase or strip, so both
    happen here."""
    return strip_emojis(norm_text(text or "")).lower().strip(_TRIM).strip()


def _english(lang: str) -> bool:
    # `norm_lang` returns "" for unset/unknown, which every caller reads as `en`.
    # NOT `_language._es()`: `GUARD_LANGS` is {"es"}, so that helper answers False
    # for `sl` and would run these English patterns on a Slovene account.
    # es/sl get their own patterns when someone writes them.
    return norm_lang(lang) in ("", "en")


# "how much" carries the signal; the words after it decide whether he means money.
#
# TWO shapes, and the split came from replaying 21 days of live inbound (2026-08-29):
#
#   "how much FOR …" — transactional by the preposition, so whatever follows is the
#   thing he wants priced. An allowlist cannot cover that half: the live miss was
#   "How much for a pic of your soles?", and a list of nouns is the same losing game
#   `CONTENT_ASK_RE`'s own history documents (the subject sits between the verb and
#   the media word).
#
#   "how much" ALONE, or trailed only by closed-class words. Here the allowlist IS
#   the guard, and it is what refuses the questions about HER that the same corpus
#   is full of: "How much coffee do you drink in a day?", "How much protein is in
#   your dick?", "How much edging can your cock take?" — all correctly silent.
_PRICE_TAIL = (r"(?:is|are|was|for|it|that|this|they|them|those|the|a|an|to|do|does|"
               r"did|would|will|want|cost|costs|charge|u|you|ur|your|me|of|see|watch|"
               r"unlock|send|vid|vids|video|videos|pic|pics|photo|photos|clip|clips|"
               r"set|sets|content|smth|something|all|full|everything|"
               r"sir|babe|baby|hun|then|now|tho|though|please|pls)")
# Anchored at a CLAUSE, not just the string: "Yes sir! How much" is a real fan
# asking a real price and appeared twice in three weeks, while "you have no idea how
# much i'd love that" must stay silent — the difference is exactly whether a
# sentence ended right before it.
_CLAUSE = r"(?:^|[.!?,;]\s*)"
_PRICE_ASK_RE = re.compile(
    _CLAUSE + r"how much\s+for\b"
    r"|" + _CLAUSE + r"how much(?:\s+" + _PRICE_TAIL + r")*$"
    r"|^(?:what'?s?|whats|hows)\s+(?:the|ur|your|it)?\s*price"
    r"|\bwhat do (?:u|you) charge\b"
    r"|\bhow much (?:do|does|would) (?:it|they|that) cost\b"
    r"|^price$")


def is_price_ask(text: str | None, lang: str = "en") -> bool:
    """Is he asking what it costs? The strongest buying signal a fan sends — and
    until 2026-08-29 it matched nothing anywhere in the service."""
    if not text or not _english(lang):
        return False
    return bool(_PRICE_ASK_RE.search(_canon(text)))


# A FULL-MESSAGE allowlist, never a substring search: "yes" is a buy signal and
# "yes but im broke" is the opposite, and only the whole message can tell them
# apart. Every entry here is also in `_common._LOW_INFO_TOKENS` — which is the
# collision this reader exists inside, and why `sell_lane.qualify` lifts exactly
# one refusal for a turn that carries the provenance (see the note there).
_AFFIRMATIONS = frozenset({
    "yes", "yea", "yeah", "yep", "yup", "ya", "sure", "ok", "okay", "k",
    "please", "pls", "plz", "yes please", "yes pls", "ok please",
    "send it", "send them", "send", "do it", "go on", "go ahead",
    "fuck yes", "hell yes", "yes sure", "sure thing", "id love to", "i would",
    # Multi-word forms men actually used answering a real offer in the live
    # window. They stay safe for the same reason the one-word entries do: the
    # match is the WHOLE message, so "of course yes" is an acceptance while "of
    # course not now" is simply not in the set. Widening to a prefix test would
    # break that and let "yes but i cant afford it" through.
    "of course", "of course yes", "yes of course", "absolutely", "definitely",
    "hell yeah", "yes babe", "yes daddy", "ok yes", "yes ok", "yes sir",
    "i do", "i would love that", "id love that", "yes i do", "sounds good",
})


def is_affirmation(text: str | None, lang: str = "en") -> bool:
    """Is this message NOTHING BUT a yes? Never enough on its own — the caller must
    also know it answers an offer (`is_offer_shaped`); `wide_ask` pairs them."""
    if not text or not _english(lang):
        return False
    return _canon(text) in _AFFIRMATIONS


# What our own last message has to look like for his "yes" to mean a sale. A
# question is not enough ("do you like dogs?" → "yes" must sell nothing) and an
# offer word is not enough (she says "i love ur pics" all day), so both are
# required — or an explicit "let me send you…", which is an offer without a
# question mark.
_OFFER_TOKENS = (r"(?:wanna|want|see|send|show|unlock|special|content|vid|vids|"
                 r"video|videos|pic|pics|photo|photos|clip|clips|set|surprise|"
                 r"smth|something|naughty|spicy)")
_OFFER_SHAPE_RE = re.compile(
    r"\b" + _OFFER_TOKENS + r"\b(?=.*\?)"                    # a question that offers
    r"|\b(?:i can|i could|lemme|let me|want me to|wanna|should i)\s+"
    r"(?:\w+\s+){0,2}(?:send|show)\b")                       # an offer, stated


def is_offer_shaped(text: str | None) -> bool:
    """Did WE just put something on the table? Read off our latest outbound, which
    both calling engines already hold as history — this is what turns a bare "yes"
    from an answer into an acceptance.

    ⚠️ NOT `_canon`: that trims trailing punctuation, and the question mark IS half
    of this signal ("wanna see it?" is an offer, "wanna see it" is not a sentence
    she writes). Only case and emoji are folded here."""
    if not text:
        return False
    return bool(_OFFER_SHAPE_RE.search(strip_emojis(norm_text(text)).lower()))


# A TEASE is the other way she puts something on the table, and on this roster it
# is the COMMON way: 3.37% of 58,302 live outbound messages are shaped like this
# against 0.33% shaped like a plain offer question. She dangles rather than asks —
# "this is all youre getting for free. the rest is better", "u gotta earn that
# one", "maybe i'll give u something real to worship".
#
# Every alternative below was written off a real message a fan then said yes to.
# Two shapes were deliberately EXCLUDED after the same replay:
#   • "the rest OF YOU" — that is his body, not her vault ("i keep the rest of you
#     busy" was the one false positive the first draft produced).
#   • a bare "of me" — "thinking of me" is not an offer, and the phrase is far too
#     common to spend a priced send on.
_TEASE_RE = re.compile(
    r"\bthe rest\b(?!\s+of\s+(?:you|u)\b)"
    r"|\bmore of me\b|\bfor free\b|\balready yours\b"
    r"|\b(?:gotta|got to|have to|hav to|need to)\s+earn\b|\bearn (?:that|it|this)\b"
    r"|\bmaybe i(?:'?ll| will)\b|\bi might (?:let|show|send|give)\b"
    r"|\bi'?ll (?:give|show|send|let)\s+(?:u|you)\b"
    r"|\buse (?:ur|your) imagination\b|\bholdin(?:g)? back\b|\bbeen sav(?:ing|ed)\b"
    r"|\bwan(?:t|na) (?:to )?see what'?s\b")


def is_tease_shaped(text: str | None) -> bool:
    """Did our last message DANGLE something without asking a question?

    Weaker evidence than `is_offer_shaped`, which is why its "yes" is played on a
    dice rather than every time — see `TEASE_SELL_RATE`."""
    if not text:
        return False
    return bool(_TEASE_RE.search(strip_emojis(norm_text(text)).lower()))


def clamp_rate(raw) -> float:
    """A config rate as a real 0..1 float. Bad input reads as the shipped default,
    never as "always": this is the one knob where a typo (`33` meaning 33%) would
    turn a deliberate fraction into every single turn. Lives here rather than in
    either engine so the two cannot read the same blob two ways."""
    try:
        return min(1.0, max(0.0, float(raw)))
    except (TypeError, ValueError):
        return TEASE_SELL_RATE


def _tease_rolls(seed: str, rate: float) -> bool:
    """The dice — DETERMINISTIC per turn, and that is not a testing convenience.

    The engines re-evaluate the same unanswered inbound on every tick, and the
    lane's memo only remembers refusals it actually made. A fresh roll each tick
    would mean a "yes" that lost the dice at 21:04 wins it at 21:06 and gets a PPV
    two minutes late, answering a sentence that has scrolled away. Seeded on the
    fan and both messages, the same turn always gets the same answer, and a
    DIFFERENT turn gets an independent one."""
    if rate >= 1:
        return True
    if rate <= 0:
        return False
    return Random(f"tease:{seed}").random() < rate


def last_outbound(history) -> str:
    """Our most recent message body from a `(direction, body)` thread, newest last.
    Both engines carry exactly this shape (`_Cand.messages`, autoreply's
    `_history`), so the two can never read a different "our last message"."""
    for direction, body in reversed(list(history or [])):
        if direction == "out" and (body or "").strip():
            return body
    return ""


def wide_ask(text: str | None, our_last: str | None, *,
             lang: str = "en", seed: str = "",
             tease_rate: float = TEASE_SELL_RATE) -> str | None:
    """The widened trigger, composed ONCE for every engine: `"price"`,
    `"affirmation"`, `"tease"`, or None.

    Returned as provenance rather than a boolean because it is carried into the
    lane on `Turn.wide_ask`, where it does three jobs one bool could not: it arms
    `is_ask`, it lifts the low-information refusal his one-word answer would
    otherwise earn, and it is the `via=` that makes every outcome greppable.

    The two affirmation branches are ordered by how much his "yes" is worth: an
    answer to a plain offer is taken every time, an answer to a tease on a dice.
    `seed` should identify the fan; the turn's own words are mixed in here.
    """
    if is_price_ask(text, lang):
        return WIDE_PRICE
    if not is_affirmation(text, lang):
        return None
    if is_offer_shaped(our_last):
        return WIDE_AFFIRMATION
    if is_tease_shaped(our_last) and _tease_rolls(
            f"{seed}|{_canon(text)}|{_canon(our_last)}", tease_rate):
        return WIDE_TEASE
    return None


def record(*, regex_says: bool, model_says: bool, account_id: str,
           fan_id: int, engine: str, via: str | None = None) -> None:
    """Log which reader saw the ask, and nothing else.

    The rates ARE the point of arming this, and they are only comparable across
    engines if every engine records on the same event — one line per reply the model
    actually wrote. `autoreply` calls this directly rather than `decide`, because its
    two sale moments mean the OR is not what acts there (see the note at its call
    site) and a `decide` whose verdict is discarded reads like a bug.

    `via` is the widened reader that produced the engine's verdict, when one did.
    Without it the recall added by "how much?" and "yes" is indistinguishable in
    the log from the ordinary ask regex, and its precision — the whole open
    question about these two readers — cannot be measured at all.
    """
    tag = f" via={via}" if via else ""
    if model_says and not regex_says:
        log.info("sell_signal model-only ask (recall) engine=%s account=%s fan=%s%s",
                 engine, account_id, fan_id, tag)
    elif regex_says and not model_says:
        log.info("sell_signal regex-only ask engine=%s account=%s fan=%s%s",
                 engine, account_id, fan_id, tag)


def decide(*, regex_says: bool, model_says: bool, account_id: str,
           fan_id: int, engine: str, via: str | None = None) -> bool:
    """The trigger: **OR**. Either reader saw an ask, and it is an ask.

    ARMED 2026-08-15 (operator ruling) — this used to return `regex_says` and merely
    record the disagreement. The ruling is to measure it live on the small accounts
    the roster runs for exactly this, rather than wait on a shadow week that could
    only be collected by first turning the block on anyway.

    OR, never AND, and the asymmetry is the point. The two readers fail in opposite
    directions and only one of those failures costs anything:

      * model-only — the regex missed an ask. THIS IS THE FEATURE. "Do you send ass
        pics?" matched nothing until 2026-08-12 because the subject sits between the
        verb and the media word, which no fixed option list can hold.
      * regex-only — the model did not notice an ask the pattern caught. Under OR
        this changes nothing, which is why AND is not on the table: it would let a
        distracted model veto a plain, matched ask.

    Both are still logged (via `record`), because the rates are what tell us whether
    the model is adding recall or adding noise once this is live. A false positive
    costs one vault send to a man who did not ask; a false negative costs the sale.

    ⚠️ This is the ENGINE's copy of the OR — "take this turn to the lane at all".
    `SellLane.is_ask` is the AUTHORITY's, and the two must stay the same predicate;
    `test_sell_lane_wiring` pins the truth table across both.

    A widened reader is already folded into `regex_says` by the engine (it decides
    the composite; the lane re-reads it off `Turn.wide_ask`), so `via` is passed
    through for the log alone — adding it to the OR here would count it twice.
    """
    record(regex_says=regex_says, model_says=model_says,
           account_id=account_id, fan_id=fan_id, engine=engine, via=via)
    return regex_says or model_says
