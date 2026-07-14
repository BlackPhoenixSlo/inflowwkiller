"""
service/automations/script_packs.py — the default script pack.

Every line below is a REAL line from this agency's own 1:1 chat archive (bespoke
single-fan outbound, non-mass, non-automation), lightly normalised: names swapped
for {name}, prices for {price}, HTML stripped. Nothing here is invented, because a
model-invented "sexy line" reads like a model-invented sexy line.

The operator attaches media and does nothing else — that is the whole point. Lines
are overridable per account (`script_pack_overrides_json`) from the UI, but the
shipped defaults are meant to work untouched.

ONE pack, not three. A "Gentle / Balanced / Aggressive" preset was designed and
then cut: nothing in the product selects a pack, persona voice is already governed
by the account's AI persona config, and offering a non-technical operator a choice
between three untested voices is a decision with no evidence behind it.
"""
from __future__ import annotations

import re
from random import Random

# Slots the engine actually fires. A slot that nothing references is dead weight —
# `bump_no_reply` (the "you're ignoring me 🥺" guilt line) was deliberately CUT from
# every shipping path: it is aimed at the majority who did NOT buy, it is the
# highest-churn message anyone proposed, and there is zero measurement behind it.
PACK: dict[str, list[str]] = {
    # Opens a scene. Free, never priced.
    "question_hook": [
        "cant stop thinking about our chat 😏 u around {name}?",
        "hmmmmm, can we have some fun, dirty fun, the kind that leaves us breathless and aching?🥵",
        "what kind do you play when youre all alone?",
        "hey kitten, do you like 69?",
        "got me in such a mood tonight... what are you up to right now?",
        "are you free now, {name}?",
    ],
    # The cold open — the FIRST priced thing he ever sees. Sits LOW in the band.
    "rung_open": [
        "I need your honest opinion about this babe😘",
        "you will like this vid, check it out",
        "here you go, babe😘",
        "you can have a taste of this when you get home🥵🥵",
        "Are you going to let me dance for you😋🥰",
    ],
    # After a PAID rung, inside the hot window. This is where the money is.
    "rung_escalate": [
        "{name}, watch me strip here in my bedroom. Do you think you can help me out using your mouth only? 🥵",
        "mmm, {name}. Do you wanna bury your face here between my legs and make my pussy wet?🥵",
        "do you really want me right here babyyyy😋",
        "i know my worth honey, and i know you gonna enjoy seeing me getting fucked 😈",
        "dont nut all over the place baby🥵💦",
    ],
    # Immediately after an unlock — FREE. Keeps the scene alive so the next rung
    # lands on a conversation, not on silence.
    "post_buy_bridge": [
        "mmm yeah? did you like that 😏",
        "awww baby, how i wish i saw the mess you just caused there",
        "youll have to let me know all about it",
    ],
    "edge_hold": [
        "u love that right?",
        "mhmmm yeah?",
        "hmm, is that a yes?",
    ],
    # Sells the illusion she is filming it RIGHT NOW. Corpus-universal.
    "pre_ppv_stall": [
        "ok give me two secs im filming it rn 🎥",
        "hold on... im getting it ready for you 😈",
        "one sec baby, setting my phone up",
    ],
    # Answers "too expensive" by citing the CONTENT, never his wallet.
    "objection_price": [
        "well, my usual price for sexting is higher than that babe 🤤 this one's a full video though",
        "its a long one baby, not a quick clip — i promise its worth it",
    ],
    # Concede ONCE, then stop. Never haggle twice — that teaches him to haggle.
    "haggle_counter": [
        "mmm ok... {price} and its yours, but thats me being nice 😏",
    ],
    # ONLY on the unsend-first path: the original must be pulled before the cheaper
    # copy goes out, or he can buy both and we've sold the same clip twice.
    "discount_resend": [
        "hey stranger... where u been? made this hopin itd bring u back — {price} just for you",
        "i took it down and put it back cheaper for you baby, {price} 😘",
    ],
    # SOFT decline ("i'm broke"): keep TALKING, stop SELLING. A poverty plea is the
    # highest-value moment to be a person and the worst moment to be a salesman.
    "soft_broke_ack": [
        # He said he's short on money → she eases off, NO pressure. But these lines must
        # NOT declare "i just wanna talk" — because if HE later pulls (asks for content),
        # the pause lifts and she sells, and a line that foreclosed selling would make her
        # contradict herself. So: warm, no-rush, no promise to never sell.
        "aww no rush at all baby 🥺 im not going anywhere",
        "no worries love, whenever you're ready — im just happy you're here",
        "totally okay babe, lets just enjoy this 😘",
    ],
    # Session end by SILENCE after a PAID rung (§7.1) → ONE warm free closer, then she
    # goes quiet (she does not keep poking him). Reseeded from real corpus closers —
    # warm, NO gift/revenue copy: the "free gift" is §7.2 and code-owned, an aftercare
    # line must never imply one.
    "aftercare": [
        "thanks baby 🥰",
        "dream of me!! talk to you tomorrow!",
        "no pressure okay? 😘",
        "night night, message me when you wake up",
    ],
    # Fan says he just wants to TALK (companion intent, §6.3): seller OFF, conversation
    # ON. Acknowledge it warmly and MEAN it — never a pitch, never a "but if you change
    # your mind" hook.
    "companion_ack": [
        "aww i love just talking to you too 🥰",
        "honestly this is nice, no pressure at all",
        "i'm happy just like this babe",
    ],
}

_PLACEHOLDER_RE = re.compile(r"\{(name|price)\}")


def render(slot: str, *, rng: Random, name: str = "babe", price_cents: int | None = None,
           overrides: dict[str, list[str]] | None = None) -> str | None:
    """Pick one line for `slot` and fill its placeholders.

    `overrides` is the account's edited pack (UI). An override REPLACES the shipped
    pool for that slot; an empty list falls back to the default rather than sending
    an empty message."""
    pool = None
    if overrides:
        candidate = overrides.get(slot)
        if isinstance(candidate, list) and any(str(x).strip() for x in candidate):
            pool = [str(x) for x in candidate if str(x).strip()]
    if pool is None:
        pool = PACK.get(slot) or []
    if not pool:
        return None

    line = rng.choice(pool)
    price = ""
    if price_cents:
        price = (f"${price_cents // 100}" if price_cents % 100 == 0
                 else f"${price_cents / 100:.2f}")
    return _PLACEHOLDER_RE.sub(
        lambda m: (name if m.group(1) == "name" else price), line
    ).strip()


def slots() -> list[str]:
    """Slot names, for the UI's editor."""
    return list(PACK)
