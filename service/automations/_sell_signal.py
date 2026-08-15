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

## Shadow first — this does not trigger anything yet

`decide()` returns the regex verdict unchanged and merely RECORDS whether the model
agreed. Until there is a week of that on live traffic, nobody knows whether the
signal adds recall or adds false positives, and "the model said so" is not evidence
before it is measured. Arming it is one line here, changed once the numbers exist.
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


def decide(*, regex_says: bool, model_says: bool, account_id: str,
           fan_id: int, engine: str) -> bool:
    """The trigger, and the shadow record. Returns the REGEX verdict, unchanged.

    Both disagreements are logged because they mean opposite things and only one
    of them is a reason to arm this:

      * model-only — the regex missed an ask. This is the recall the feature is for.
      * regex-only — the model did not notice an ask the pattern caught. Harmless
        today, and a warning about arming an OR that could ever become an AND.
    """
    if model_says and not regex_says:
        log.info("sell_signal SHADOW model-only ask engine=%s account=%s fan=%s",
                 engine, account_id, fan_id)
    elif regex_says and not model_says:
        log.info("sell_signal SHADOW regex-only ask engine=%s account=%s fan=%s",
                 engine, account_id, fan_id)
    return regex_says
