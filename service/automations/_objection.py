"""Does this turn owe the fan an apology, and WHICH one?

One question, one entry point (`decide`), three detectors behind it:

    upsell.is_content_dispute   regex   he says we charged him for what was already
                                        his, or for what is free on the public page
    upsell.classify_decline     regex   chargeback / report / unsubscribe words
    _judge                      LLM     the same complaint in words nobody wrote down

They are ordered by cost, and the first hit wins. The answer is an `Objection` —
a `make_right` apology frame plus how hard to brake the selling — or None for "he
did not complain", which is the overwhelming majority of turns.

WHY THIS IS ONE FUNCTION AND NOT THREE CHECKS IN THE SWEEP. Every one of these
detectors ends in the same SHAPE of consequence — brake the selling, send one
apology — differing only in the words and the length of the brake. Left inline they
were a bool, a verdict string, a re-binding of both, and a nested try/except
threaded through the middle of `run()` — four moving pieces for a question with one
answer. Here the precedence is readable in one place, the sweep asks it in one line,
and the brake travels WITH the apology instead of being chosen next to it.

Same reasoning as `_leash.py`, and the same narrow seam: `ObjectionCand` below is
everything this module needs from a candidate, so the policy can be read and tested
without the sweep around it.

THE COST DISCIPLINE. `_judge` is the only gate in the chat engine that spends an
LLM call. It fires only inside the post-purchase window — his first few replies
after he actually paid — because that is where a miss is a chargeback and a false
positive is nearly free. Volume is bounded by PURCHASES, not by traffic: measured
at ~19 calls/day roster-wide, ~$0.005/month on deepseek-v4-flash.

A HINT regex used to gate the call, because on a 2-verdict prompt the model fired
3 times on hint-free chat and was wrong all 3 — "Too much clothing", "i thought it
was going to be some B/G content". It was removed once `content_letdown` existed:
re-measured on 200 hint-free prod messages the model fired 6 times and every one
was a real unhappy buyer. Those three were never hallucinations, they were this
class with no name, and the gate was only ever blocking them.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import NamedTuple, Protocol

from sqlalchemy import func, select

import llm_client
from db.engine import get_session
from db.models import Fan, Message

from . import upsell
from ._common import is_qualifying_inbound
from .fan_state import fan_state, set_fan_state

log = logging.getLogger("of-relay.automations.objection")

# The apology reasons, which are also `make_right._APOLOGY_FRAMES`' keys — the
# string this module returns IS the one that rides into the apology. make_right
# validates it again on arrival and degrades to HARD_DECLINE if it does not know
# the name, so a drift here costs the right words, never the apology itself.
# (`test_make_right` asserts the two sets still agree.)
HARD_DECLINE = "hard_decline"
CONTENT_DISPUTE = "content_dispute"
PAID_UNDELIVERED = "paid_undelivered"
# He paid, and what he got simply wasn't what he hoped for — "too much clothing",
# "i thought it was going to be some B/G content". NOT a billing claim: nothing was
# double-charged and nothing is free elsewhere, so the dup-charge words would be a
# false admission. He is still a paying fan who is unhappy, and the one thing we
# must not do is answer him with another price.
CONTENT_LETDOWN = "content_letdown"
# What the JUDGE alone may return. `hard_decline` is deliberately absent: that
# verdict belongs to the regex that can see chargeback words, and a model must not
# be able to talk us into the generic apology when it found nothing specific.
JUDGE_VERDICTS = (CONTENT_DISPUTE, PAID_UNDELIVERED, CONTENT_LETDOWN)

# How hard each verdict brakes the selling. A billing claim is a chargeback risk and
# takes the full 72h stop; a letdown is a man who is disappointed, not cheated, and
# 24h is the brake the codebase already models for "stop selling, keep talking".
# Mapping rather than branching at the call site: the sweep applies a consequence, it
# does not decide one.
_SEVERITY = {
    HARD_DECLINE: upsell.DECLINE_HARD,
    CONTENT_DISPUTE: upsell.DECLINE_HARD,
    PAID_UNDELIVERED: upsell.DECLINE_HARD,
    CONTENT_LETDOWN: upsell.DECLINE_SOFT,
}


class Objection(NamedTuple):
    """What this turn owes him: the apology to send, and how hard to stop selling.

    One value, because the two are decided together and applying one without the
    other is always a bug — a 72h blackout with no apology is the silent treatment,
    and an apology that keeps selling is what started all this."""
    reason: str          # a make_right apology frame key
    decline: str         # upsell.DECLINE_HARD | DECLINE_SOFT — the brake

    @classmethod
    def of(cls, reason: str) -> "Objection":
        return cls(reason, _SEVERITY[reason])


_STATE_KEY = "_pp_judge"     # fans.custom_fields slot: the inbound we last judged
_PURPOSE = "ai_chatter_post_purchase"
_TEXT_CLIP = 400             # same clip the reply path puts on a single message body

_SYSTEM = (
    "You read ONE message from a fan who has just PAID for content on OnlyFans, "
    "and answer whether he is UNHAPPY ABOUT THAT PURCHASE.\n"
    "Reply with strict JSON and nothing else: {\"verdict\": \"none\" | "
    '"content_dispute" | "paid_undelivered" | "content_letdown"}\n\n'
    "content_dispute — a BILLING claim: what he paid for was already his, he had "
    "already bought or unlocked it, or it is free/public on the creator's page or "
    "social media.\n"
    "paid_undelivered — he paid and says he received nothing at all.\n"
    "content_letdown — he received something new that was genuinely his, but it is "
    "not what he hoped for: too tame, too short, too few, the wrong kind of "
    "content, or not what he was led to expect.\n"
    "none — ANYTHING else.\n\n"
    "Choose content_dispute over content_letdown whenever he says he ALREADY had "
    "it or that it is public — that is about his money, not his taste.\n\n"
    "Answer `none` unless he is clearly UNHAPPY about what he just bought. These "
    "are all `none`, and getting them wrong is the expensive mistake:\n"
    "- praise, thanks, or excitement about what he unlocked\n"
    "- asking for more content, or asking what else there is\n"
    "- saying what he WOULD like to see next, or requesting a different kind\n"
    "- mentioning he already owns something WITHOUT complaining about it\n"
    "- haggling, or saying a future price is too high\n"
    "- saying he is broke or out of money\n"
    "- flirting, sexual talk, teasing, or ordinary conversation\n"
    "- complaining about something OTHER than the content he paid for\n\n"
    "He does not have to be angry or use insults. A calm, polite question — "
    '"why did I pay for these?" — is a content_dispute. But wanting MORE is not '
    "the same as being unhappy with what he got: a fan who says \"love it, got "
    'anything hotter?" is `none`.')


class ObjectionCand(Protocol):
    """What this module needs from a candidate — the whole seam, stated once.

    A Protocol rather than an import of ai_chatter's `_Cand`, for the same reason
    `LeashCand` is one: the dependency points one way, and it names exactly which
    fields a policy reads out of a class carrying forty."""
    last_body: str        # the last thread line — what the regexes score
    last_in_text: str     # HIS OWN WORDS, no `[he sent: …]` vision tag
    msg_ids: list[int]    # OF message ids, aligned with the thread rows


def judge_on(cfg: dict) -> bool:
    """Is the post-purchase judge armed for this account? Read here AND by the
    sweep, which uses it to decide whether to load `last_paid_at` at all — so the
    two cannot drift into a state where the judge is on and permanently starved of
    its own trigger. Either bound at 0 is an off switch."""
    return (bool(cfg.get("post_purchase_objection_check", True))
            and int(cfg.get("post_purchase_window_hours") or 0) > 0
            and int(cfg.get("post_purchase_max_msgs") or 0) > 0)


async def decide(account_id: str, fan_id: int, fan: Fan | None, c: ObjectionCand, *,
                 cfg: dict, model: str, last_paid_at: datetime | None,
                 now: datetime, dry_run: bool = False) -> "Objection | None":
    """What this turn owes him — apology + brake — or None. THE entry point.

    Cheapest first, first hit wins:

      1. `is_content_dispute` — free, and NOT gated on the seller flag. That gate is
         a SELLING feature flag; "he says we charged him for something that was
         already his" is a statement about money we have ALREADY taken, which is the
         same reasoning that leaves `_customs.is_owed` ungated. Measured, not tidy:
         of the two real disputes in the 135k-inbound corpus, one is on an account
         with the gate explicitly off, so gating this would keep missing him.
      2. `classify_decline` — free, gate-scoped, the pre-existing behaviour.
      3. `_judge` — one LLM call, post-purchase window only. Asked LAST so a
         sentence the regexes already recognise never costs anything. It is the only
         source of `content_letdown`, which brakes for 24h rather than 72h: a man
         who is disappointed is not a man who was cheated.

    Never raises. A detector that breaks must cost us a verdict, not the turn.

    `dry_run` reaches the judge for ONE reason: the stamp. A preview may spend the
    call (the sweep already spends a far larger one generating the reply it is
    previewing, and an operator reading a dry run wants the real verdict), but it
    must not CONSUME the at-most-once budget — a preview that leaves `_pp_judge.mid`
    on the inbound makes the next REAL run skip it, so the complaint is judged in
    the run that cannot act and skipped by the run that can. Same trade the stamp
    already makes for a provider blip: re-buying a verdict costs a fraction of a
    cent, missing a chargeback costs the account."""
    if upsell.is_content_dispute(c.last_body):
        return Objection.of(CONTENT_DISPUTE)
    # Read off cfg rather than taken as a parameter: the sweep derives `gate_on`
    # from this exact key, and a second copy threaded in as an argument is a second
    # thing that can disagree with it.
    if (bool(cfg.get("qualification_gate_enabled"))
            and upsell.classify_decline(c.last_body) == upsell.DECLINE_HARD):
        return Objection.of(HARD_DECLINE)
    try:
        # `last_in_text`, NOT `last_body`: the judge must read HIS words. last_body
        # carries the `[he sent: …]` vision tag, and asking a model "is he
        # complaining" over prose OUR OWN describer wrote is the documented way to
        # get an invented grievance.
        verdict = await _judge(account_id, fan_id, fan, c.last_in_text,
                               message_id=(c.msg_ids[-1] if c.msg_ids else None),
                               last_paid_at=last_paid_at, cfg=cfg, model=model,
                               now=now, dry_run=dry_run)
    except llm_client.LLMCapExceeded:
        return None          # out of budget ⇒ the regexes above stand alone
    except Exception:
        log.warning("post-purchase judge failed account=%s fan=%s",
                    account_id, fan_id, exc_info=True)
        return None
    # Outside the try ON PURPOSE. A verdict with no _SEVERITY row is OUR bug, not the
    # provider's, and swallowing it here would log a provider failure and drop the
    # apology — the two most misleading things it could do.
    return Objection.of(verdict) if verdict else None


async def _inbound_count_since(account_id: str, fan_id: int, since: datetime) -> int:
    """How many messages HE has sent since the purchase. The window is counted in
    HIS turns, not in wall-clock, because his turns are what bound the spend."""
    async with get_session() as s:
        return int((await s.execute(
            select(func.count()).select_from(Message).where(
                Message.account_id == str(account_id),
                Message.fan_id == int(fan_id),
                Message.direction == "in",
                Message.is_unsent.is_(False),
                Message.created_at > since))).scalar_one() or 0)


async def _judge(account_id: str, fan_id: int, fan: Fan | None, text: str, *,
                 message_id: int | None, last_paid_at: datetime | None,
                 cfg: dict, model: str, now: datetime,
                 dry_run: bool = False) -> str | None:
    """One cheap call: "is he complaining about what he just bought?"

    `upsell.is_content_dispute` closes the sentence that prod incident produced;
    this closes the ones nobody has written down yet. Five gates before a token is
    spent, cheapest first — the flag, a purchase inside the window, a SUBSTANTIVE
    inbound (the same 3-token bar the reply path uses, so "😍" and "thanks babe"
    never reach the model), one we have not already judged, and one of his first N
    replies to the unlock.

    Raises LLMCapExceeded / provider errors up to `decide`, which owns the policy
    that a broken judge costs a verdict and nothing else."""
    if not judge_on(cfg) or last_paid_at is None:
        return None
    if last_paid_at < now - timedelta(hours=int(cfg["post_purchase_window_hours"])):
        return None
    if not is_qualifying_inbound(text):
        return None
    # AT MOST ONCE per inbound: the reply for a turn can be deferred by rhythm, the
    # quota or a lease, and every deferred tick walks this code again with the same
    # message still last on the thread. No id ⇒ we cannot promise that, so we
    # decline to spend rather than re-buy the same verdict every sweep.
    if message_id is None:
        return None
    if int(fan_state(fan, _STATE_KEY).get("mid") or 0) == int(message_id):
        return None
    if await _inbound_count_since(account_id, fan_id, last_paid_at) > \
            int(cfg["post_purchase_max_msgs"]):
        return None

    res = await llm_client.chat(
        model=model,
        messages=[{"role": "system", "content": _SYSTEM},
                  {"role": "user", "content": text[:_TEXT_CLIP]}],
        purpose=_PURPOSE, account_id=account_id, fan_id=fan_id,
        response_format={"type": "json_object"}, temperature=0.0,
    )
    # Stamped only once the call CAME BACK — never before it. Stamping first meant a
    # provider blip or a cap rejection burned the fan's one chance to be heard, and
    # the complaint could then never be judged on any later tick. Re-buying a verdict
    # costs a fraction of a cent; missing a chargeback costs the account. A preview
    # is the same trade: it may read the verdict, it may not spend the one turn the
    # REAL run gets to act on it (see `decide`).
    if not dry_run:
        await set_fan_state(account_id, fan_id, _STATE_KEY, {"mid": int(message_id)})
    try:
        verdict = str((json.loads(res.content or "{}") or {}).get("verdict") or "").strip()
    except Exception:
        log.warning("post-purchase judge returned non-JSON account=%s fan=%s: %r",
                    account_id, fan_id, (res.content or "")[:120])
        return None
    # An unknown verdict is DROPPED, never forwarded: this string becomes make_right's
    # apology `reason`, and a hallucinated one would fall through its frame lookup to
    # "you got charged twice" — a false admission about a fan's money.
    if verdict not in JUDGE_VERDICTS:
        if verdict and verdict != "none":
            log.warning("post-purchase judge unknown verdict %r account=%s fan=%s",
                        verdict, account_id, fan_id)
        return None
    log.info("post-purchase objection account=%s fan=%s verdict=%s msg=%s",
             account_id, fan_id, verdict, message_id)
    return verdict
