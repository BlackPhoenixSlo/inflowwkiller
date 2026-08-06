"""service/automations/_customs.py — the owed-custom ledger.

THE PRODUCT, for a creator whose vault is bought out: the fan tips and gets back
a VOICE NOTE made for him. Fulfilment is MANUAL by operator ruling — a human
records it and sends it. So the system has exactly four jobs, and none of them is
delivery:

    notice the money → make it unmissable → stop selling → let a human clear it

WHERE "OWED" LIVES: `fans.customs_owed_at`
------------------------------------------
One nullable timestamp. Set to the moment the qualifying tip landed; NULL means
nothing is owed. `is_owed` reads that column and nothing else, and the operator
sees the queue on /customs.

⚠️ IT USED TO BE A " Custom" SUFFIX ON `custom_nickname`, AND THAT SILENTLY DID
NOT WORK. The reasoning was sound — the operator lives in the OnlyFans app, where
a nickname renders next to the fan's name, so a marker there is seen without
anyone deciding to go and look. The problem is that `custom_nickname` has four
other writers. `of_ai_chat._maybe_push_nickname`, reached from ai_chatter on EVERY
tick, rebuilds the name from structured facts via `names.build_structured_nickname`
— which knows nothing about any marker and therefore drops it, then pushes the
shortened name to OnlyFans AND upserts it back into our own row. ai_chatter runs
every 60s. customs_watch runs every 900s. So the ledger was erased about a minute
after it was written, on both surfaces, and prod confirms the outcome exactly:
204 watcher runs, zero fans ever marked, in a feature that had been live for days.

Worse than a lost row: `_maybe_push_nickname` mutates the in-memory Fan BEFORE
`_build_messages` reads it, so `prompt_block` below — the "he has already paid,
stop selling" rule — had never rendered once on the engine that closes the sale.

A column has no other writers, so none of that is expressible.

WHAT COUNTS AS AN ORDER
-----------------------
A DM tip ≥ $100 (`kind='tip'`) that actually cleared. Not `tip_post` — a $200 tip
on a post is a man being generous about a photo, not a man ordering a voice note.
Not a quantity either: the transcript that priced this feature (fan 72033414,
2026-07-31 — $200 on account 7789837 at 10:51, delivered "heres your custom Alex"
at 14:47) shows the amount encoding LENGTH, not count. Two customs means two
separate tips.

WHO WE OFFER ONE TO
-------------------
Only a fan who has proven he pays — see `may_offer`. Operator ruling 2026-08-05:
*"we don't offer for everything to anyone"*.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # annotation only — this module stays import-light at runtime
    from db.models import Fan

log = logging.getLogger("of-relay.automation.customs")

# The floor. Both $100 and $200 appear on the live male accounts (7 and 8 times
# respectively); the transcript shows $100 is the opener the chatter counters,
# but it does get accepted, so it is an order.
MIN_CENTS = 10_000

# The CEILING the chatter may quote. Operator ruling 2026-08-04 said "100-400",
# and $400 was carried for a day; it is now $200, because the top half of that
# band is a number no fan has ever paid.
#
# THE LARGEST SINGLE DM TIP IN THE ENTIRE PRODUCTION DATABASE IS $200.00 — four
# occurrences, across two accounts, over the whole history. Quoting $400 as "the
# longest one" advertises a rung nobody has ever climbed, and it breached the
# $200 ceiling this codebase already enforces on every other priced surface
# (`upsell.OF_PRICE_MAX_CENTS`). Raising it again is a pricing decision, not a
# typo fix: check `max(amount_cents) where kind='tip'` first.
#
# ⚠️ THE FLOOR AND THE ASK ARE ONE POLICY, AND THEY USED TO LIVE IN DIFFERENT
# HALVES OF THE SYSTEM. `MIN_CENTS` is what makes a tip an ORDER on the WATCH
# side; nothing on the SELL side named a figure at all, so the model picked one.
# A model that asks for $30 gets paid $30, `qualifies` says no, nothing is
# marked, and the fan waits for a voice note nobody was ever told to record.
# Silent, and it costs the fan money — so the band ships with the permission for
# the same reason the fence does.
MAX_CENTS = 20_000

# The lifetime spend a fan must have BEFORE the customs block enters his prompt
# at all — operator ruling 2026-08-05: *"we can do customs only to [fans who]
# actually pay … we don't offer for everything to anyone"*.
#
# It is deliberately the same figure as `MIN_CENTS`: the qualification to be
# ASKED for $100 is having already spent $100. A man who has never paid that much
# in total is not a man who is about to tip it in one go, and putting the block in
# his prompt only makes the prompt longer and the reply vaguer.
#
# Measured on the four accounts that sell customs: 84 of 1,358 fans clear this
# (6%). So 94% of prompts get shorter, and the ones that keep the block are the
# ones where it can actually close.
SELL_MIN_SPEND_CENTS = 10_000

# Only a DM tip. `tip_post`/`tip_stream` are generosity, not an order.
ORDER_KINDS = ("tip",)


# The price sentence spliced onto `_voice.CUSTOMS_CONDITIONS`, so every surface
# that may OFFER a custom also names what it costs. Built from the two constants
# above rather than written out, so the figure the model quotes and the figure
# `qualifies` accepts cannot be edited apart.
#
# The amount is the LENGTH, not the quantity — see the module docstring: the
# transcript that priced this feature shows $200 buying ONE longer piece, not two
# shorter ones. Leading space: it joins the end of a sentence.
PRICE_RULE = (
    f" IT COSTS ${MIN_CENTS // 100}-${MAX_CENTS // 100}. The amount is the "
    f"LENGTH — ${MIN_CENTS // 100} buys a short one, ${MAX_CENTS // 100} the "
    "longest — so quote a real number inside that range when you name a price. "
    f"NEVER go below ${MIN_CENTS // 100}: a smaller tip does not buy a custom, "
    "and taking one means he pays and gets nothing. Never discount it, never "
    "offer a cheaper short version or a free sample, and never apologise for it."
)


# ── The ledger. Four questions and two writes, all over `Fan` ────────
#
# TYPED, AND THAT IS THE WHOLE GUARD. These four took `str | None` when the
# ledger was a nickname suffix. A call site left un-migrated is the failure this
# module most needs to prevent — it would read as "nothing owed" and a man who
# paid would be silently forgotten — so `fan.customs_owed_at` is accessed
# DIRECTLY rather than through `getattr(..., None)`. A stray nickname raises
# AttributeError at the first line it reaches, which is what a broken contract
# should do. There is nothing to remember and no guard to keep in sync.
#
# `mark` and `clear` MUTATE the row and do not commit — the caller owns the
# session, because both callers already have one open for other reasons.


def is_owed(fan: "Fan | None") -> bool:
    """Does this fan have a custom paid for and not yet delivered?

    Reads `fans.customs_owed_at` and nothing else. None → False, so a caller
    holding a fan that may not exist needs no guard of its own."""
    return fan is not None and fan.customs_owed_at is not None


async def owed_fan_ids(account_id) -> set[int]:
    """Every fan on this account with an outstanding custom — the SEND-SIDE brake.

    ⚠️ `is_owed` ABOVE ONLY EVER BOUND THE THREE ENGINES THAT HOLD A `Fan` ROW —
    `ai_chatter`, `of_ai_chat`, `autoreply`. Those are the engines that TALK. The
    engines that SELL on their own schedule (`ppv_send`, `tip_request`,
    `mass_nudge`, `reengage_buyers`) never load a Fan before choosing an audience;
    they build an EXCLUDE SET and subtract it. So the brake that stops the chat
    from pitching a second custom did not stop a PPV blast, a tip ask, or a
    re-engagement DM from reaching the same man on the next tick — which is the
    failure this whole feature exists to prevent, arriving by the one door nobody
    had shut.

    Returned as a set for exactly that shape: `excl |= await owed_fan_ids(aid)`.

    THE SET IS TINY AND THAT IS THE POINT. It holds only fans with money taken
    and work outstanding — currently a handful per account, cleared by the
    operator on /customs or by `customs_watch` seeing the voice note go out. It is
    NOT an inferred-state gate: `ppv_send`'s 07-23 ruling bars shrinking a blast
    on CHAT SIGNALS (which cost −86% of sends), and this is not one. It is the
    same distinction that leaves `is_owed` ungated on the seller flag — "we owe
    this man something he paid for" is a fact about money already taken, not a
    selling decision.

    Local imports: this module is deliberately import-light at runtime (see the
    TYPE_CHECKING guard at the top) and every caller already has the DB loaded.
    Mirrors `_common._load_skip_ids`."""
    from sqlalchemy import select

    from db.engine import get_session
    from db.models import Fan

    async with get_session() as s:
        rows = (await s.execute(
            select(Fan.fan_id).where(
                Fan.account_id == str(account_id),
                Fan.customs_owed_at.is_not(None),
            )
        )).all()
    return {int(r[0]) for r in rows}


def mark(fan: "Fan | None", tipped_at: datetime | None = None) -> bool:
    """Record that `fan` is owed a custom, as of `tipped_at`. Returns True when
    this CHANGED something.

    Idempotent: a fan already owed is left exactly as he was, so re-running the
    scanner over a tip it already marked neither moves the "waiting since" clock
    nor reports a second order. The stamp is the TIP's time, not now, because the
    queue sorts and ages by it — stamping `utcnow()` on every sweep would reset
    every fan's wait to zero every 15 minutes."""
    if fan is None or is_owed(fan):
        return False
    fan.customs_owed_at = tipped_at or datetime.utcnow()
    return True


def clear(fan: "Fan | None", cleared_at: datetime | None = None) -> bool:
    """Settle `fan`'s owed custom. Returns True when this CHANGED something.

    Stamps `customs_cleared_at` as well as blanking `customs_owed_at`: the second
    is what stops the prompt block and the queue, the first is what stops
    `customs_watch` re-marking the same tip on its next sweep. Both, or the
    operator's "Sent" click undoes itself in 15 minutes."""
    if fan is None or not is_owed(fan):
        return False
    fan.customs_owed_at = None
    fan.customs_cleared_at = cleared_at or datetime.utcnow()
    return True


def may_offer(fan: "Fan | None") -> bool:
    """Is this a fan we OFFER a custom to at all? (`SELL_MIN_SPEND_CENTS`.)

    Separate from `qualifies`, which asks whether money already received was an
    ORDER. This one gates the prompt: below the bar the customs block never
    renders, so the model cannot pitch what the fan was never going to buy.

    A fan who is already owed one still returns True — the gate is about whether
    he is a customs buyer, and `prompt_block` is what stops the second pitch. A
    None fan is False: no row, no proof he pays."""
    if fan is None:
        return False
    return int(fan.lifetime_spend_cents or 0) >= SELL_MIN_SPEND_CENTS


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
# The operator clears the debt by hand on /customs, and that stays the authority.
# But a hand-clear that is FORGOTTEN leaves the account permanently unable to sell
# to its best fan — the brake below has no timeout, deliberately, because a
# timeout would resume selling to a man who never got what he paid for. So
# delivery is also detected, and the two paths agree: whichever happens first wins.
#
# AUDIO ONLY. The product is a voice note (operator ruling 2026-08-05: *"we should
# sell only voice messages"*), so a voice note is the only thing that can be it.
# That is also rare and deliberate enough to be a trustworthy signal: 9 outbound
# audio messages across the whole platform, ever.
#
# ⚠️ A FREE VIDEO USED TO COUNT TOO, for a "later, short video" product that is not
# being built. It was a live false-clear: `customs_watch`'s clear loop falls back
# to the sweep window when the originating tip has aged out, so ANY free outbound
# video in the last 72 hours settled a debt incurred days earlier — a mass send, a
# tip_reward freebie, or a chatter being nice. Narrowing the product narrows the
# discriminator, and both get safer at once.
#
# A photo never counts. If the product ever grows past audio this needs a
# discriminator that is not price: free photos and free videos are both common,
# so a second type here would have to arrive with a predicate, not alone.
#
# There is deliberately NO `is_delivery()` helper beside this tuple. There was
# one, and once the product narrowed to audio it had decayed into
# `media_type == "audio"` carrying two parameters it ignored — while its only
# caller ALREADY filtered `MessageMedia.type.in_(DELIVERY_TYPES)` in SQL. The
# Python re-test could not answer anything the query had not already decided.
# One expression of the rule, in the query that reads the rows.
DELIVERY_TYPES = ("audio",)


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
