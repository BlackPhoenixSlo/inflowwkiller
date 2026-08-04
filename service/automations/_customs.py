"""service/automations/_customs.py — the owed-custom ledger, kept in a nickname.

THE PRODUCT, for a male creator with a bought-out vault: the fan tips $100-$200
and gets back a voice note (later, a short video) made for him. Fulfilment is
MANUAL by operator ruling — a human records it and sends it. So the system has
exactly four jobs, and none of them is delivery:

    notice the money → make it unmissable → stop selling → let a human clear it

WHY A NICKNAME AND NOT A TABLE
------------------------------
Operator ruling 2026-08-04: *"we record that in ai info and nickname. That
nickname word custom is deleted manually after voice is send."*

A new `customs_owed` table would be the obvious build and the wrong one. The
operator does not work in our admin — they work in the OnlyFans app, where the
custom nickname renders next to the fan's name in the inbox list. A marker there
is seen without anyone deciding to go and look, and deleting it is a gesture the
operator already performs. A table needs a queue, a page, and a habit; the
nickname needs none of the three, and its deletion IS the completion signal.

The cost is that "owed" is a string suffix rather than a row, so this module
exists to keep that string in exactly one place.

⚠️ SUFFIX, NEVER SUBSTRING
--------------------------
Prod has `Johny/Colombia/31/Customer Service` — a job title `gen_info` wrote. A
`"custom" in nick.lower()` test marks that fan as owing a video forever, and
`_clear` would then chew the word "Customer" out of his name. Both directions
are wrong, both are silent, and this was found by grepping prod rather than by
reasoning. Match the SUFFIX with a word boundary and nothing else.

WHAT COUNTS AS AN ORDER
-----------------------
A DM tip ≥ $100 (`kind='tip'`). Not `tip_post` — a $200 tip on a post is a man
being generous about a photo, not a man ordering a voice note. Not a quantity
either: the live transcript that priced this feature (fan 72033414, 2026-07-31)
shows $200 buying ONE longer video, not two shorter ones — the amount encodes
LENGTH. Two customs means two separate tips.
"""
from __future__ import annotations

import logging
import re

log = logging.getLogger("of-relay.automation.customs")

# The marker. A trailing word, space-separated, so a nickname reads
# "Alex (VIP on Paid Page) Custom" in the OF inbox and the operator deletes the
# last word to mark it delivered.
MARKER = "Custom"

# Anchored to the END and preceded by a boundary — see the docstring. A leading
# `\s*` on the group so `_clear` removes the separator too and does not leave
# "Alex (VIP on Paid Page) ".
_MARKER_RE = re.compile(r"\s*\b" + MARKER + r"\s*$", re.I)

# The floor. Both $100 and $200 appear on the live male accounts (7 and 8 times
# respectively); the transcript shows $100 is the opener the chatter counters,
# but it does get accepted, so it is an order.
MIN_CENTS = 10_000

# Only a DM tip. `tip_post`/`tip_stream` are generosity, not an order.
ORDER_KINDS = ("tip",)


def is_owed(nickname: str | None) -> bool:
    """Does this nickname carry the owed marker?"""
    return bool(nickname) and bool(_MARKER_RE.search(nickname))


def mark(nickname: str | None) -> str:
    """`nickname` with the marker appended — idempotent, so re-running the
    scanner over a tip it already marked cannot produce "Alex Custom Custom"."""
    base = (nickname or "").strip()
    if is_owed(base):
        return base
    return f"{base} {MARKER}".strip()


def clear(nickname: str | None) -> str:
    """`nickname` with the marker removed. Provided for completeness and for the
    tests; in production the OPERATOR clears it by hand, which is the whole
    point — a machine that clears its own marker has no completion signal."""
    return _MARKER_RE.sub("", (nickname or "").strip()).strip()


def qualifies(kind: str, amount_cents: int,
              min_cents: int | None = None) -> bool:
    """Is this transaction an order for a custom?

    `min_cents` overrides the floor PER ACCOUNT, carried on the automation rule's
    `trigger_json`. What a $100 DM tip MEANS is an account-level fact, not a
    global one: on the male accounts it demonstrably is an order (fan 14673050
    said so in as many words), while the female accounts took 5 of them last
    month that were almost certainly generosity. One constant cannot be right for
    both, and getting it wrong in the generous direction stops the bot selling to
    a good fan until a human notices."""
    floor = MIN_CENTS if min_cents is None else max(1, int(min_cents))
    return (str(kind or "") in ORDER_KINDS
            and int(amount_cents or 0) >= floor)


# ── Delivery ─────────────────────────────────────────────────────────
# The operator clears the marker by hand, and that stays the authority. But a
# hand-clear that is FORGOTTEN leaves the account permanently unable to sell to
# its best fan — the brake below has no timeout, deliberately, because a timeout
# would resume selling to a man who never got what he paid for. So delivery is
# also detected, and the two paths agree: whichever happens first wins.
#
# WHAT COUNTS, and why it is not "he sent a video":
#   • AUDIO, any price — a voice note IS the v1 product, and it is rare and
#     deliberate: 4 outbound audio messages across both live male accounts, ever.
#   • A FREE VIDEO — for the "later, short video" case. FREE is load-bearing:
#     the same accounts have 170 PRICED outbound videos against 9 free ones, so
#     "an outbound video" would clear the marker on every ordinary vault PPV.
#     A custom is already paid for, so it goes out at price 0; a priced video is
#     a new sale and cannot be the thing he is waiting for.
#
# A photo never counts. If the product ever includes photo sets this needs a
# different discriminator than price, because free photos are common.
DELIVERY_TYPES = ("audio", "video")


def is_delivery(media_type: str, price_cents: int, is_tip: bool = False) -> bool:
    """Does this OUTBOUND media message look like the custom being delivered?

    Caller must already have established direction='out' and that the message is
    newer than the tip — this is only the media test."""
    t = str(media_type or "").strip().lower()
    if t == "audio":
        return True
    if t == "video":
        return int(price_cents or 0) == 0 and not is_tip
    return False


# ── What the model is told while one is owed ─────────────────────────
#
# ⚠️ THIS BLOCK REVERSES THE ETA BAN IN `_voice.CUSTOMS_CONDITIONS`, DELIBERATELY.
# That constant says "NEVER say WHEN it will land … a guess becomes a promise he
# will hold you to". Operator ruling 2026-08-04: *"says while chatting I will do
# it later today if asked when"*. Both cannot be true, so the scope is split:
#
#   • the fence still bans an ETA when the custom is being SOLD (nothing is owed
#     yet, nobody has recorded anything, and any date is invented),
#   • this block permits exactly ONE phrasing once the money is in.
#
# "Later today" is a same-day COMMITMENT made automatically to a man who just
# paid. It is only safe because fulfilment is same-day by operator ruling — if
# that ever stops being true this line is the first thing that must change.
_OWED_BLOCK = {
    "her": (
        "\n\nA CUSTOM IS ALREADY PAID FOR AND NOT YET SENT.\n"
        "- Do NOT sell him anything. No PPV, no new custom, no tip ask, no "
        "upsell of any kind, not even a soft one. He has paid and is waiting.\n"
        "- Keep talking to him normally — warm, flirty, interested. The "
        "conversation continues; only the selling stops.\n"
        "- If he asks WHEN: say you'll do it later today. Exactly that much — "
        "no hour, no 'in a bit', no second promise on top.\n"
        "- Never imply it is already sent, and never ask him to pay again for "
        "the thing he has already paid for."
    ),
    "him": (
        "\n\nA CUSTOM IS ALREADY PAID FOR AND NOT YET SENT.\n"
        "- Do NOT sell him anything. No PPV, no new custom, no tip ask, no "
        "upsell of any kind, not even a soft one. He has paid and is waiting.\n"
        "- Keep talking to him normally. The conversation continues; only the "
        "selling stops.\n"
        "- If he asks WHEN: tell him you'll do it later today. Exactly that "
        "much — no hour, no 'in a bit', no second promise on top. Say it flatly, "
        "the way a man states a plan, not the way someone apologises for a "
        "delay.\n"
        "- Never imply it is already sent, and never ask him to pay again for "
        "the thing he has already paid for."
    ),
}


def prompt_block(owed: bool, voice: str = "her") -> str:
    """The block to append while a custom is outstanding ('' otherwise).

    Returns '' for both lanes when nothing is owed, so every caller can append
    it unconditionally and no engine grows an `if`."""
    if not owed:
        return ""
    return _OWED_BLOCK["him" if str(voice or "").strip().lower() == "him"
                       else "her"]
