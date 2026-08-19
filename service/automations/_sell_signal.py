"""service/automations/_sell_signal.py — the model tells us he asked to buy.

## What this is

One line the model may append to its reply, stripped before the fan sees it:

    SELL: yes

That is the whole protocol. It answers ONE question — *did he ask to see or buy
something this turn?* — and the answer is a boolean.

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
"""
from __future__ import annotations

import logging

from ._markers import protocol_marker_re

log = logging.getLogger("of-relay.automation.sell_signal")

# The colon carries the discrimination, exactly as it does for `STICKER:` — "sell"
# is an ordinary word in a real body ("i sell customs"), "SELL:" is not.
_MARKER_RE = protocol_marker_re(r"SELL[ \t]*:")

# Wrapper punctuation + sentence enders the model dresses a marker in.
_TAIL_TRIM = "*_~`\"'()[]{}.!, "

# What the model is told. One line, appended to the prompt's output contract —
# which says "no JSON, quotes, or metadata", and this is the sanctioned exception.
BLOCK = (
    "SELL SIGNAL — if his last message asked to SEE or BUY something, end with "
    "a final line exactly:\n"
    "SELL: yes\n"
    "no ask → no line. stripped before he sees it — say NOTHING about content "
    "or prices."
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


def record(*, regex_says: bool, model_says: bool, account_id: str,
           fan_id: int, engine: str) -> None:
    """Log which reader saw the ask, and nothing else.

    The rates ARE the point of arming this, and they are only comparable across
    engines if every engine records on the same event — one line per reply the model
    actually wrote. `autoreply` calls this directly rather than `decide`, because its
    two sale moments mean the OR is not what acts there (see the note at its call
    site) and a `decide` whose verdict is discarded reads like a bug.
    """
    if model_says and not regex_says:
        log.info("sell_signal model-only ask (recall) engine=%s account=%s fan=%s",
                 engine, account_id, fan_id)
    elif regex_says and not model_says:
        log.info("sell_signal regex-only ask engine=%s account=%s fan=%s",
                 engine, account_id, fan_id)


def decide(*, regex_says: bool, model_says: bool, account_id: str,
           fan_id: int, engine: str) -> bool:
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
    """
    record(regex_says=regex_says, model_says=model_says,
           account_id=account_id, fan_id=fan_id, engine=engine)
    return regex_says or model_says
