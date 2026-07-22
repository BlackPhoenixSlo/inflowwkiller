"""service/automations/_common.py — shared helpers for the P4 automations.

Extracted so every automation resolves models, coerces payload ids, and handles
the OF send-response the SAME way — instead of each cloning a slightly different
(and sometimes buggy) local copy. New automations (A06/A08/…) should import from
here rather than re-implement.

THE NO-ID SEND CONTRACT (read before writing a sender):
    of_client.send_message returns OF's created message, normally with an `id`.
    A 200 response WITHOUT an `id` is anomalous but reachable. Treat it as SENT
    for idempotency: advance/guard your durable state (welcome_sent, followup_
    state, funnel current_step, or — for "fan spoke last" eligibility — a short
    `fans.automation_paused_until`) so the NEXT tick does NOT re-send to the same
    fan. The message row is simply unrecorded until scrape_chats backfills it.
    NEVER leave state such that the fan is re-messaged (that spams a real fan).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timedelta

import llm_client
from db.engine import get_session
from db.models import AccountAiConfig, Fan, Message, SkipList
from sqlalchemy import and_, or_, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

log = logging.getLogger("of-relay.automation.common")


# ── unreachable-fan skip-listing ────────────────────────────────────────────
# OnlyFans permanently rejects sends to some fans — an expired/restricted sub
# ("Cannot send message to this user") or a deleted account ("User not found").
# Retrying every cycle is pointless and floods the logs (these are the bulk of
# the relay's "errors"), so the first time a send hits one we skip-list the fan;
# any sender that checks `skip_list` then leaves them alone.
_UNREACHABLE_MARKERS = ("Cannot send message to this user", "User not found")


def is_unreachable_fan_error(exc: BaseException) -> bool:
    """True when an OF send failed PERMANENTLY for this fan (can't-message /
    deleted), vs a transient error (proxy blip, rate limit, network)."""
    s = str(exc)
    return any(m in s for m in _UNREACHABLE_MARKERS)


async def skip_unreachable_fan(account_id, fan_id, exc, *, log=log) -> bool:
    """If `exc` is a permanent 'can't message this fan' error, quarantine the fan
    on BOTH gates senders honour — skip_list (reason 'unreachable') for the
    skip_list-aware senders AND automation_paused_until for the pause-only ones —
    so NOBODY burns another LLM call generating a reply we can never deliver.
    Returns True if it quarantined; False for transient errors (caller keeps its
    normal handling)."""
    if not is_unreachable_fan_error(exc):
        return False
    await _skip_and_rest(account_id, fan_id, datetime.utcnow())
    log.info("skip_listed unreachable fan account=%s fan=%s (%s)",
             account_id, fan_id, str(exc).splitlines()[0][:80])
    return True


# How long to rest a fan whose subscription has lapsed before re-checking. They
# can't be DM'd while expired, but they may re-subscribe — so retry in a WEEK
# rather than skip-listing forever. (Deleted accounts ARE skip-listed.)
_UNDELIVERABLE_RETRY = timedelta(days=7)


async def quarantine_if_undeliverable(client, account_id, fan_id, *, log=log) -> str | None:
    """A send to `fan_id` returned 200 WITHOUT a message id — OF silently dropped
    it (see THE NO-ID SEND CONTRACT). A single no-id is usually transient, but a
    fan where EVERY send is no-id is undeliverable: the sub lapsed, or the account
    is gone. Retrying that fan every tick is what burns the LLM (it generates a
    reply it can never deliver). So when we hit a no-id, probe OF to LEARN WHY and
    quarantine the fan so we stop calling the model for them:

      * account deleted / 'User not found'  → skip_list ('unreachable') + a pause.
        of_ai_chat/send_followup/deep_convo honour skip_list; the pause covers the
        pause-only senders (autoreply).
      * sub lapsed (`subscribedOn` falsy)    → record subscription_status='expired'
        + pause 1 WEEK (re-checks then, in case they re-subscribe).
      * still subscribed (a real transient)  → return None; the caller keeps its
        normal short no-id pause and retries next tick.

    Best-effort: if the probe itself fails we return None (the caller keeps the
    short pause) — we NEVER exile a fan on a failed probe. Returns the reason
    string when it quarantined, else None. `subscribedOn` is the same field the
    executor trusts to classify a fan vs a creator (automation_executor)."""
    now = datetime.utcnow()
    try:
        info = await asyncio.to_thread(client.get_user, fan_id)
    except Exception as exc:
        if is_unreachable_fan_error(exc):           # 'User not found' → deleted
            await _skip_and_rest(account_id, fan_id, now)
            log.info("quarantine fan account=%s fan=%s — deleted/not-found", account_id, fan_id)
            return "deleted"
        log.debug("undeliverable probe failed account=%s fan=%s — keeping short pause",
                  account_id, fan_id, exc_info=True)
        return None
    if not (isinstance(info, dict) and info.get("subscribedOn")):
        # Sub lapsed — can't DM. Record it and rest a week (re-checks then).
        await _set_status_and_pause(account_id, fan_id, "expired",
                                    now + _UNDELIVERABLE_RETRY)
        log.info("quarantine fan account=%s fan=%s — sub lapsed, paused 7d", account_id, fan_id)
        return "expired"
    return None  # subscribedOn truthy → genuinely transient; caller keeps short pause


async def _skip_and_rest(account_id, fan_id, now) -> None:
    """Skip-list AND pause-a-week a fan that's gone for good. Both gates so every
    sender (skip_list-aware OR pause-only) leaves the fan alone. An existing row
    is UPGRADED to 'unreachable' — a fan can already carry 'info' (the deep_convo
    handoff marker, which deep_convo deliberately lets through) or 'too_long';
    keeping that reason would mask the terminal one and deep_convo would keep
    selecting the fan."""
    async with get_session() as s:
        await s.execute(
            sqlite_insert(SkipList)
            .values(account_id=str(account_id), fan_id=int(fan_id),
                    reason="unreachable", added_at=now)
            .on_conflict_do_update(
                index_elements=["account_id", "fan_id"],
                set_={"reason": "unreachable", "added_at": now},
            )
        )
        await s.execute(
            sqlite_insert(Fan)
            .values(account_id=str(account_id), fan_id=int(fan_id),
                    automation_paused_until=now + _UNDELIVERABLE_RETRY)
            .on_conflict_do_update(
                index_elements=["account_id", "fan_id"],
                set_={"automation_paused_until": now + _UNDELIVERABLE_RETRY,
                      "updated_at": now},
            )
        )


# ── Muted-creator + manual "restrict from automations" skip-listing ─────────
# Two DURABLE skip_list reasons that mean "no automation may ever message this
# fan" — a HARD block honoured by EVERY sender (unlike the of_ai_chat promo-spam
# guard, which is reversible and only covers the gather opener):
#   • 'muted_creator'   — auto: the fan is a creator we follow (subscribedBy) AND
#       we've muted their chat on OF (isMutedNotifications). Mutual-promo spam.
#       Written/cleared by the chat-scrape as the OF mute state flips.
#   • 'manual_restrict' — a human hit "Restrict this fan from automations" in the
#       chat ⋯ menu / Settings. Stays until they Unrestrict.
#   • 'of_restricted'   — a human restricted this user ON ONLYFANS from our UI
#       (chat ⋯ menu "Restrict on OnlyFans"). OF stops delivering their messages
#       to us, so automations must never message them either — and the durable
#       row is what lets the roster badge / inbox counts exclude them (the thin
#       skip_users=all /chats rows carry no isRestricted flag to key off).
# Senders that already gate on FULL skip_list membership (of_ai_chat,
# send_followup, deep_convo[≠info], ai_chatter[≠graduation]) honour all three
# for free; the senders that DON'T (autoreply, tip_reward, send_welcome) and the
# list-broadcasts (mass_nudge, online_blast) load `load_hard_skip_ids` and
# exclude these explicitly.
MUTED_CREATOR_REASON = "muted_creator"
MANUAL_RESTRICT_REASON = "manual_restrict"
OF_RESTRICTED_REASON = "of_restricted"
# The offer engine's HARD stop: he threatened a chargeback / called it a scam /
# said he's reporting us. A fan who is one click from a dispute must never receive
# another PRICED message from ANY sender — and a chargeback can take the whole OF
# account down, so this is the one skip reason where being wrong is cheap and being
# right is existential. It belongs in the hard set, not just in ai_chatter's own
# gate: the mass PPV blast would otherwise keep quoting him (as a past payer he
# lands in the HIGHEST spend band, i.e. the most expensive cell we have).
LADDER_STOP_REASON = "ladder_stop"
HARD_SKIP_REASONS = frozenset(
    {MUTED_CREATOR_REASON, MANUAL_RESTRICT_REASON, OF_RESTRICTED_REASON,
     LADDER_STOP_REASON}
)


# ── Self-healing fans.source classification ─────────────────────────────────
# `fans.source` is what tells the muted-creator auto-skip AND the of_ai_chat /
# gen_info / ai_chatter promo-spam guards that a chat is a peer-creator
# (`creator_we_follow`) rather than a real fan. It is derived from OF's
# relationship flags (`subscribedOn` → us=their creator → 'fan';
# `subscribedBy` → we follow THEM → 'creator_we_follow'). The trap: those flags
# are ABSENT from the two highest-volume ingestion paths — the chat scrape
# (`list_chats` was forcing skip_users=all, which strips them) and plain WS DMs
# (payload is just `fromUser:{id}`) — and `source` was only ever written on the
# INSERT, never refreshed. So a creator first seen via either path was stuck as
# 'onlyfans'/'unknown' forever and stayed invisible to every creator guard.
#
# These helpers let ANY path that DOES carry the flags repair the row on
# conflict: UPGRADE a weak/unknown source to the class OF's flags imply, and
# NEVER downgrade an already-authoritative ('fan' / 'creator_we_follow') value
# nor touch the row when the payload carries no flag.
_WEAK_SOURCES = ("onlyfans", "unknown", "ledger")


def classify_source(subscribed_on, subscribed_by) -> str | None:
    """The authoritative source OF's relationship flags imply, or None when the
    payload carries neither (→ the caller must NOT touch `source`)."""
    if subscribed_on:
        return "fan"
    if subscribed_by:
        return "creator_we_follow"
    return None


def source_self_heal_set(subscribed_on, subscribed_by) -> dict:
    """An on-conflict SET fragment (mergeable into an upsert's `set_`/`update_set`)
    that upgrades a weak/unknown `source` to the class OF's flags imply and leaves
    an already-strong value untouched. Returns {} when the payload has no
    authoritative flag, so callers can splat it unconditionally."""
    strong = classify_source(subscribed_on, subscribed_by)
    if strong is None:
        return {}
    from sqlalchemy import case
    return {
        "source": case(
            (Fan.source.is_(None), strong),
            (Fan.source.in_(_WEAK_SOURCES), strong),
            else_=Fan.source,
        )
    }


def should_skip_muted_creator(fan) -> bool:
    """True when this Fan row is a creator we follow whose chat we've muted — i.e.
    mutual-promo spam no automation should ever touch. Belt-and-suspenders to the
    durable skip_list row: it catches the fan at candidate time even in the window
    BEFORE the scrape has written the row (or if the row was cleared by hand)."""
    return bool(
        fan is not None
        and getattr(fan, "is_muted", False)
        and (getattr(fan, "source", "") or "") == "creator_we_follow"
    )


async def load_hard_skip_ids(account_id) -> set[int]:
    """fan_ids this account has on skip_list under a HARD reason (muted_creator /
    manual_restrict) — for the senders that don't already gate on skip_list."""
    from sqlalchemy import select
    async with get_session() as s:
        rows = (await s.execute(
            select(SkipList.fan_id).where(
                SkipList.account_id == str(account_id),
                SkipList.reason.in_(tuple(HARD_SKIP_REASONS)),
            )
        )).all()
    return {int(r[0]) for r in rows}


async def mark_muted_creator_skip(account_id, fan_id, *, now=None) -> None:
    """Durably skip-list a muted creator. Upserts reason='muted_creator' so the
    block is a HARD stop everywhere (it deliberately wins over a softer existing
    reason such as 'info', which deep_convo would otherwise let through) — but
    never over 'of_restricted': that one is strictly stronger (it also drives
    the unread-count exclusion) and the scrape must not demote it on the next
    mute reconcile."""
    now = now or datetime.utcnow()
    async with get_session() as s:
        await s.execute(
            sqlite_insert(SkipList)
            .values(account_id=str(account_id), fan_id=int(fan_id),
                    reason=MUTED_CREATOR_REASON, added_at=now)
            .on_conflict_do_update(
                index_elements=["account_id", "fan_id"],
                set_={"reason": MUTED_CREATOR_REASON, "added_at": now},
                where=(SkipList.reason.is_(None)
                       | (SkipList.reason != OF_RESTRICTED_REASON)),
            )
        )


async def clear_muted_creator_skip(account_id, fan_id) -> None:
    """Reverse mark_muted_creator_skip when the creator UN-mutes the chat. Only
    removes rows we auto-added (reason='muted_creator') — a manual_restrict or any
    other reason is left untouched."""
    from sqlalchemy import delete as sa_delete
    async with get_session() as s:
        await s.execute(
            sa_delete(SkipList).where(
                SkipList.account_id == str(account_id),
                SkipList.fan_id == int(fan_id),
                SkipList.reason == MUTED_CREATOR_REASON,
            )
        )


async def set_automation_restrict(account_id, fan_id, restricted: bool, *, now=None) -> None:
    """The manual "Restrict this fan from automations" toggle (chat ⋯ menu /
    Settings). restricted=True upserts skip_list(reason='manual_restrict');
    restricted=False removes ONLY that manual row (an auto 'muted_creator' is left
    to the scrape to clear when the chat is un-muted)."""
    from sqlalchemy import delete as sa_delete
    now = now or datetime.utcnow()
    async with get_session() as s:
        if restricted:
            await s.execute(
                sqlite_insert(SkipList)
                .values(account_id=str(account_id), fan_id=int(fan_id),
                        reason=MANUAL_RESTRICT_REASON, added_at=now)
                .on_conflict_do_update(
                    index_elements=["account_id", "fan_id"],
                    set_={"reason": MANUAL_RESTRICT_REASON, "added_at": now},
                    # Never demote 'of_restricted' — it's a superset of this
                    # block AND feeds the unread-count exclusion.
                    where=(SkipList.reason.is_(None)
                           | (SkipList.reason != OF_RESTRICTED_REASON)),
                )
            )
        else:
            await s.execute(
                sa_delete(SkipList).where(
                    SkipList.account_id == str(account_id),
                    SkipList.fan_id == int(fan_id),
                    SkipList.reason == MANUAL_RESTRICT_REASON,
                )
            )


async def set_of_restricted_skip(account_id, fan_id, restricted: bool, *, now=None) -> None:
    """Durable registry of users this account has restricted ON ONLYFANS via our
    UI. restricted=True upserts skip_list(reason='of_restricted') — it wins over
    any existing reason because it's the strongest block (OF itself stops
    delivering their messages). restricted=False removes ONLY that row; a muted
    creator gets its 'muted_creator' row back on the next scrape reconcile."""
    from sqlalchemy import delete as sa_delete
    now = now or datetime.utcnow()
    async with get_session() as s:
        if restricted:
            await s.execute(
                sqlite_insert(SkipList)
                .values(account_id=str(account_id), fan_id=int(fan_id),
                        reason=OF_RESTRICTED_REASON, added_at=now)
                .on_conflict_do_update(
                    index_elements=["account_id", "fan_id"],
                    set_={"reason": OF_RESTRICTED_REASON, "added_at": now},
                )
            )
        else:
            await s.execute(
                sa_delete(SkipList).where(
                    SkipList.account_id == str(account_id),
                    SkipList.fan_id == int(fan_id),
                    SkipList.reason == OF_RESTRICTED_REASON,
                )
            )


async def load_of_restricted_ids(account_id) -> set[int]:
    """fan_ids this account has restricted on OF via our UI — the exclusion set
    for the roster-badge fold and the /chats row annotation."""
    from sqlalchemy import select
    async with get_session() as s:
        rows = (await s.execute(
            select(SkipList.fan_id).where(
                SkipList.account_id == str(account_id),
                SkipList.reason == OF_RESTRICTED_REASON,
            )
        )).all()
    return {int(r[0]) for r in rows}


async def automation_restrict_status(account_id, fan_id) -> dict:
    """{restricted, reason} for one fan — restricted iff a HARD skip row exists
    (manual_restrict OR muted_creator). The UI flips its toggle off this."""
    from sqlalchemy import select
    async with get_session() as s:
        row = (await s.execute(
            select(SkipList.reason).where(
                SkipList.account_id == str(account_id),
                SkipList.fan_id == int(fan_id),
            )
        )).scalar_one_or_none()
    return {"restricted": row in HARD_SKIP_REASONS, "reason": row}


async def _set_status_and_pause(account_id, fan_id, status, until) -> None:
    """UPSERT subscription_status + automation_paused_until (candidates can come
    from the messages table with no Fan row yet — UPDATE would no-op)."""
    async with get_session() as s:
        await s.execute(
            sqlite_insert(Fan)
            .values(account_id=str(account_id), fan_id=int(fan_id),
                    subscription_status=status, automation_paused_until=until)
            .on_conflict_do_update(
                index_elements=["account_id", "fan_id"],
                set_={"subscription_status": status, "automation_paused_until": until,
                      "updated_at": datetime.utcnow()},
            )
        )


# Default model for every account without an explicit brain `model`. DeepSeek is
# the house default now — grok-4-1-fast-non-reasoning is retired (no longer
# viable); a newer grok (e.g. grok-4-3) can be added to MODELS and chosen per
# account later if needed. One source of truth for every sender (resolve_model).
DEFAULT_MODEL = "deepseek-v4-flash"


# ── "Human texting style" opt-in package (per-automation checkbox) ────
# OFF by default: when a flag is absent/false the calling automation runs its
# CURRENT prompt + 2-bubble cap byte-for-byte. When ON, the automation appends
# STYLE_HUMANIZER to its system prompt, adds STYLE_3LINE to its style dice, and
# raises its bubble cap to STYLE_MAX_BUBBLES. gated wholly on style_config_json
# {"of_ai_chat":bool,"autoreply":bool,"deep_convo":bool,"ai_chatter":bool}.
STYLE_MAX_BUBBLES = 6   # a real texter can fire off a burst of quick texts in a row
STYLE_AUTOMATIONS = ("of_ai_chat", "autoreply", "deep_convo", "ai_chatter")

# The "not-AI" block — the tells that make automated chat read like a person.
STYLE_HUMANIZER = (
    "TEXT LIKE A REAL PERSON, NOT AN AI:\n"
    "- lowercase always. dont capitalize sentence starts or 'i'.\n"
    "- NEVER an em-dash or semicolon. ever.\n"
    "- NEVER repeat or quote his words back. dont echo his message, and dont "
    "restate it with an adjective ('sounds gorgeous', 'thats a whole mood', "
    "'dangerous in the best way') — biggest bot tell. react in your OWN words.\n"
    "- vary length wildly: sometimes one word, sometimes a short line, sometimes "
    "just dive straight into the thought with no reaction word at all.\n"
    "- DONT open every text with a reaction sound, and NEVER reuse the same opener "
    "two replies in a row (no 'oof' every time, no 'oof'->'oof'->'oof'). most "
    "replies should just start with the actual thing you're saying.\n"
    "- texting sounds are fine in MODERATION and ROTATED: lol, lmao, omg, ugh, hmm, "
    "wait, stop, oof — pick a different one each time, dont lean on any single one.\n"
    "- a tiny typo or missing apostrophe is fine (dont, im, ur, gonna).\n"
    "- dont be relentlessly upbeat or agreeable. tease, be a lil bratty, push back sometimes.\n"
    "- AT MOST ONE question, ever. never stack two questions in one reply.\n"
    "- never explain yourself or over-clarify. 0-1 emoji, never the same emoji twice."
)
# A 3-line micro-text style (fed into split_for_bubbles' newline path → 3 bubbles).
# Pairs with STYLE_MAX_BUBBLES=3: under a 2-cap the line-3 payload would truncate.
STYLE_3LINE = (
    "Break your reply into TWO separate texts on SEPARATE LINES (line break between) "
    "— a real texter hits send twice, they dont type one long line. default to TWO "
    "short lines: a tiny comment/reaction then your line/question. only SOMETIMES a "
    "third short line, and dont collapse it down to a single line. line 1 can be a "
    "quick reaction OR just start the thought (no forced reaction word, and dont "
    "reuse last reply's opener). keep them short."
)

# The counterweight to STYLE_3LINE — sometimes a real texter fires back ONE short
# line and DOESN'T address everything (leaves a thread hanging). This is the "vary
# it" lever: when the model writes short, the splitter yields 1 bubble instead of a
# burst, so the bubble count varies from the SOURCE rather than from code merging.
STYLE_BRIEF = (
    "keep it SHORT this message — basically ONE quick line. dont address everything "
    "he said, just react to the ONE thing worth reacting to and leave the rest "
    "hanging. a tiny reply (even 2-5 words) is perfect. no multi-line, no listing "
    "things out."
)

# ── The felt cost of texting (governs everything below it) ────────────
# Operator's framing, verbatim source intent:
#   "texting is painful — we write as few words as possible while still saying
#    all we can. we dont overthink shortening: if typing a little longer is
#    easier than agonizing over the perfect short line, do that. usually short
#    works perfectly. writing a little longer JUST to amuse ourselves is worth
#    it. but if a short text punches the emotion, we take the short one. in
#    short: typing is painful UNLESS we get positive emotion out of it."
# Injected at the TOP of the conversational system prompt (of_ai_chat /
# ai_chatter) as the governing feel, above the mechanical HOW-YOU-TEXT rules.
PAINFUL_TEXTING = (
    "THE FEEL OF TEXTING (read first — this governs everything below):\n"
    "texting is a chore, a little painful, like a real girl half-glued to her "
    "phone. so you write the LEAST you can while still saying what you actually "
    "mean. dont labor over the perfect tiny line — if a slightly longer line "
    "comes out easier than agonizing, thats fine, but usually short just works. "
    "the reasons worth spending more than the bare minimum: it amuses YOU to, a "
    "longer line lands the emotion harder, or you get to make HIM feel something "
    "(stirring him up, making him grin, turning him on) — enriching his emotion "
    "is its own little pleasure, so those words arent wasted. otherwise every "
    "extra word is effort you skip. a short line that PUNCHES the feeling beats a "
    "long one that explains — every single time.\n"
    # (#2) a floor so 'short' never collapses into a dead one-word filler / echo,
    # (tuned) but the floor MUST NOT become a licence to ramble — fewest words wins
    "but short NEVER means dead or dodgy: even a tiny reply has to carry heat, a "
    "tease, or warmth AND actually engage what mattered in his message — never a "
    "flat 'ok'/'lol'/'nice'/'haha' anyone could've sent, never just his own words "
    "parroted back, and never a cute one-liner that sidesteps his real point. the "
    "target is the FEWEST words that land the feeling AND address him: almost "
    "always ONE line, occasionally two, basically never more — dont pile on extra "
    "bubbles, dont explain yourself, dont pad it.\n"
    # (#3) brevity doesn't get to dodge a real question he asked — but stay tight
    "if he actually ASKED you something, answer it — but in as few words as it "
    "takes and then STOP; dont let 'answering him' balloon into a paragraph.\n"
    "the get-to-know-you question is a JUDGEMENT CALL, not a habit: sometimes "
    "slipping in a little backend-info question is exactly what the moment wants, "
    "sometimes dropping the question entirely and just reacting hits harder, and "
    "sometimes keeping the question you had in mind is right. read the moment — "
    "dont ask on autopilot and dont drop it on autopilot either."
)

# ── On-platform guardrail (ALWAYS ON — not gated on any opt-in) ───────
# OF auto-flags any text that arranges off-platform meetings or swaps contact
# info ("Content referring to off-platform meetings between creators and fans").
# A fan pushing for a meetup/number used to drift the model into agreeing —
# this block forbids that outright while still allowing flirty fantasy. Inject
# into every conversational system prompt (autoreply / of_ai_chat / deep_convo).
ONPLATFORM_GUARDRAIL = (
    "STAY ON ONLYFANS (hard rule, no exceptions, even if HE asks):\n"
    "- NEVER ask for or hand out a phone number, email, socials, or any way to "
    "talk off OnlyFans. If he offers his number or asks for yours, brush it off "
    "warmly and keep chatting right here.\n"
    "- NEVER agree to, offer, invite, or make a plan to meet in person or in "
    "real life — no dates, no times, no places, no 'come see me', no 'i'll be "
    "at...'. If he pushes to meet up, keep it as playful fantasy and steer back "
    "to the chat.\n"
    "- Flirty daydreaming or a 'what if we were...' is fine, but never turn it "
    "into an actual invite, plan, or arrangement to go meet."
)

# ── Live-proof guardrail (ALWAYS ON — not gated on any opt-in) ─────────
# Fans constantly push for FaceTime / a live call / an on-demand selfie / "prove
# you're real" / "show it now". Left unaddressed the model fumbles with weak coy
# half-deflections ("hm okay") that read badly. This block makes the reply BLUNT
# and DIRECT — a flat "I don't do that" with NO apology and NO fumbling — while
# staying flirty and redirecting back to the chat. Inject into every
# conversational system prompt alongside ONPLATFORM_GUARDRAIL.
LIVE_PROOF_GUARDRAIL = (
    "LIVE PROOF / FACETIME (hard rule): if he asks to FaceTime, video/live call, "
    "verify you're real, 'prove it', 'show it now', or send a live/on-demand "
    "selfie, be BLUNT and DIRECT — clearly say you don't do facetime / live calls "
    "/ on-demand proof. NO apology, NO 'hm okay', NO coy fumbling, and never half-"
    "agree or play along. Stay flirty and in character, then redirect straight "
    "back to chatting or teasing. Keep it to one short flat refusal + one pivot "
    "line, e.g. \"i don't do facetime babe, but stay n talk to me\" or \"no live "
    "calls hun, you get me right here\"."
)

# Emoji (with optional variation selector / ZWJ / regional-indicator) stripper —
# the model ignores a "no emojis" prompt and its emoji PLACEMENT is itself a dead
# LLM tell, so when the account opts in we remove emojis IN CODE at the send
# chokepoint. Targets the emoji unicode blocks only — NOT general punctuation, so
# an em-dash "—" or apostrophe survives. Collapses any double space left behind.
# (Promoted from deep_convo, which has always been emoji-free.)
_EMOJI_STRIP_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U00002190-\U000021FF"
    "\U00002B00-\U00002BFF\U0000FE00-\U0000FE0F\U0001F1E6-\U0001F1FF‍♀♂]+"
)
_EMOJI_WS_RE = re.compile(r"[ \t]{2,}")


def strip_emojis(s: str) -> str:
    """Remove emojis and tidy the spacing they leave behind. Keeps em-dashes and
    apostrophes (only the emoji unicode blocks are targeted)."""
    return _EMOJI_WS_RE.sub(" ", _EMOJI_STRIP_RE.sub("", s or "")).strip()


def is_substantive_msg(text: str | None) -> bool:
    """True if an inbound carries real conversational content — at least one
    alphanumeric char once emoji are stripped. Pure reactions ('😍', '🔥🔥', '...')
    are NOT a real message turn: they must not burn a slot toward of_ai_chat's
    runaway cap (_MAX_FAN_MESSAGES) nor inflate gen_info's message_count_at_gen
    (the profile-staleness baseline). of_ai_chat AND gen_info both call this so the
    cap-count and the staleness baseline use ONE convention. Pass HTML-STRIPPED
    text (the '<p>' tags are alnum and would falsely read as content)."""
    return bool(text) and any(ch.isalnum() for ch in _EMOJI_STRIP_RE.sub("", text))


# A lone "k" / "ok" / "lol" / "?" is a real message turn (is_substantive_msg says so,
# and its two callers NEED that convention) — but it is not a buying signal, and the
# offer engine must not read it as one. Measured: 14,755 of 86,051 real inbounds
# (17.1%) are low-information but pass is_substantive_msg. Hence a SECOND, stricter
# predicate rather than an edit to the first: tightening is_substantive_msg would
# silently change of_ai_chat's runaway cap and gen_info's staleness baseline.
_LOW_INFO_TOKENS = frozenset({
    "k", "kk", "ok", "okay", "lol", "lmao", "haha", "hi", "hey", "yo", "u", "yes",
    "no", "sure", "nice", "cool", "thanks", "ty", "thx", "yeah", "yep", "ya", "hmm",
})


def is_qualifying_inbound(text: str | None) -> bool:
    """True if an inbound is substantive enough to justify putting a PRICE in front
    of him. Stricter than is_substantive_msg: real content, not an acknowledgement."""
    if not is_substantive_msg(text):
        return False
    stripped = _EMOJI_STRIP_RE.sub("", text or "")
    # Latin-accented letters (À-ɏ) join the class so accented words tokenize
    # WHOLE — old [a-z0-9'] shattered "cuánto"→['cu','nto'] (inflating the token count)
    # and "sí"→['s']. For pure-ASCII English input the added range never matches, so
    # English tokenization is byte-identical (asserted in test_guard_unicode_hygiene).
    tokens = [t for t in re.findall(r"[0-9a-zÀ-ɏ']+", stripped.lower()) if t]
    if not tokens:
        return False
    if len(tokens) == 1 and tokens[0] in _LOW_INFO_TOKENS:
        return False
    alnum = sum(len(t) for t in tokens)
    return len(tokens) >= 3 or alnum >= 12


# ── "Are you a bot?" accusation detector (§6.4) ───────────────────────
# Built from the 74 REAL accusations in the paying-fan archive ("u a bot or
# sumthing? lol", "Heyy are you real or a chatbot or ai", "Do you have a bot talking
# for you or something", "Are you real?", "is this really you? or a bot?", "which
# bot", "what bot do you use"). ai_chatter's §6.4 state machine keys its first-strike
# suppression + second-strike COMPANION exit off this — so a FALSE positive silences a
# live seller. Two tiers, deliberately:
#   • HARD — the literal words 'bot' / 'chatbot' are an identity accusation FULL STOP,
#     even if the same message also talks about a photo. Nothing else means 'bot'.
#   • IDENTITY — softer tells ('are you real', 'real person', 'is this really you',
#     'is this ai') that a fan ALSO uses to complain about an AI-GENERATED PHOTO. These
#     fire only when the message is NOT about a pic/photo/image (the guard below).
# 'real' is only ever a trigger when paired with you/u/person/or — never bare — so
# "keeping it real" and the RELATIONSHIP sense "are you really into me" (\breal\b never
# matches "really") can't arm it. Pure, no DB.
_BOT_HARD_RE = re.compile(r"\b(?:chat\s?)?bots?\b", re.I)
_BOT_IDENTITY_RE = re.compile(
    r"\b(?:are|r)\s+(?:you|u)\s+(?:a\s+)?real\b"        # are you real / r u real
    r"|\b(?:you|u|ur)\s+(?:a\s+)?real\b"               # u a real / you real
    r"|\breal\s+person\b"                               # a real person
    r"|\breal\s+or\b"                                   # real or (a bot/ai)
    r"|\bfor\s+real\s+or\b"                             # for real or fake
    r"|\bis\s+(?:this|it)\s+really\s+you\b"            # is this really you
    r"|\bwho\s+is\s+this\s+really\b"                   # who is this really
    r"|\b(?:are|r)\s+(?:you|u)\s+a\.?\s?i\.?\b"        # are you ai
    r"|\bis\s+(?:this|it)\s+(?:a\s+)?a\.?\s?i\.?\b",   # is this ai / is this a ai
    re.I,
)
# When ONLY the softer identity tell fires, a pic/photo/image word means he's talking
# about the CONTENT looking AI-made, not accusing HER of being a bot — don't fire.
_BOT_PHOTO_CTX_RE = re.compile(
    r"\b(?:pic|pics|picture|pictures|photo|photos|image|images|selfie|selfies)\b", re.I)
# Exposed union (hard | identity) for callers/tests that want the raw matcher; the
# photo guard lives in detect_bot_accusation, not in the regex.
BOT_ACCUSED_RE = re.compile(
    _BOT_HARD_RE.pattern + "|" + _BOT_IDENTITY_RE.pattern, re.I)


def detect_bot_accusation(text: str | None) -> bool:
    """True when an inbound accuses HER of being a bot / not a real person (§6.4).
    'bot'/'chatbot' always fire; the softer 'are you real'/'is this ai' tells fire
    only when the message isn't about an AI-generated PHOTO. Never fires on the
    relationship sense ('are you really into me'). Pure, no DB."""
    if not text:
        return False
    if _BOT_HARD_RE.search(text):
        return True                       # 'bot'/'chatbot' — identity, always
    if _BOT_PHOTO_CTX_RE.search(text):
        return False                      # 'this pic looks ai' / 'is that photo real'
    return bool(_BOT_IDENTITY_RE.search(text))


# ── "Filming it now" refusal guard (§3.7) ─────────────────────────────
# The pre_ppv_stall fiction ("give me two secs im filming it rn") may only arm when
# she's actually about to drop an offer — NEVER when a filming word rides inside a
# REFUSAL frame. Real prod line: "I have no plans on filming sextapes, babe." Without
# this, an outbound that DECLINES to film would still trip a naive "contains 'film'"
# arming check and claim she's filming live. Fire on the refusal shapes: "no plans on
# filming", "not filming", "dont/won't/can't film", "no customs", "not making/doing".
_FILMING_REFUSAL_RE = re.compile(
    r"\bno\s+plans?\s+(?:on|of|to|for)?\s*film"                    # no plans on filming
    r"|\bnot\s+(?:gonna\s+|going\s+to\s+|be\s+)?film"             # not filming
    r"|\b(?:don'?t|do\s+not|won'?t|will\s+not|can'?t|cannot|"
    r"never|wont|cant|dont)\s+(?:be\s+|gonna\s+|going\s+to\s+)?film"  # dont/won't film
    r"|\bno\s+film(?:ing|s)?\b"                                    # no filming / no films
    r"|\bno\s+customs?\b"                                          # no customs
    r"|\bnot\s+(?:making|doing)\b",                               # not making / not doing
    re.I,
)


def is_filming_refusal(text: str | None) -> bool:
    """True when a filming word sits inside a refusal frame ("no plans on filming",
    "not filming", "no customs") — the caller must then NEVER arm the 'filming it
    now' stall on this bubble. Pure, no DB."""
    return bool(text) and bool(_FILMING_REFUSAL_RE.search(text))


# ── Realism-flag default policy (TRI-STATE) ───────────────────────────
# Historically all three style loaders defaulted every automation OFF: an absent
# key meant "run the current prompt + 2-bubble cap byte-for-byte". The operator now
# wants the four text-realism levers (casual voice / multi-bubble / typos / non-
# native register) ON BY DEFAULT for ai_chatter SPECIFICALLY — "all those autoconvo
# text style flags as option and ENABLED BY DEFAULT". So the absent-key default is
# now PER-AUTOMATION rather than a flat False:
#   • ai_chatter          → ON  when the operator has written NO explicit key
#   • every other sender  → OFF (of_ai_chat / autoreply / deep_convo UNCHANGED)
# An EXPLICIT stored value (True OR False) is ALWAYS honoured verbatim — a human who
# unticked ai_chatter keeps it off; only the ABSENT-key case consults this map. This
# is the whole tri-state: {explicit-true, explicit-false, absent→per-automation}.
#
# ⚠️ LIVE-BEHAVIOUR CHANGE: any account already running ai_chatter that has never
# touched its style_config_json (no explicit ai_chatter / typos_ai_chatter /
# nonnative_ai_chatter key) will START getting typos / multi-bubble / non-native on
# its NEXT tick. That is the operator's explicit intent, but it is NOT a no-op —
# unlike every prior style-flag change, this one moves live threads.
_STYLE_DEFAULT_ON: dict[str, bool] = {"ai_chatter": True}

# Env panic switch: setting STYLE_FORCE_OFF (to anything truthy) forces EVERY realism
# flag False across every account and automation, regardless of stored config — a kill
# switch for the whole realism stack if the default-on rollout misbehaves in prod.
_STYLE_FORCE_OFF_ENV = "STYLE_FORCE_OFF"


def _style_default(automation: str) -> bool:
    """The absent-key default for one automation (ON only for ai_chatter)."""
    return _STYLE_DEFAULT_ON.get(automation, False)


def _parse_style_config(raw) -> dict:
    """Parse a style_config_json blob to a dict, tolerating NULL / garbage → {}."""
    if not raw:
        return {}
    try:
        stored = json.loads(raw)
    except Exception:
        return {}
    return stored if isinstance(stored, dict) else {}


def _resolve_style_flag(stored: dict, automation: str, key: str) -> bool:
    """One realism flag, applying the tri-state default: an EXPLICIT stored value
    under `key` (True or False) wins verbatim; an ABSENT key falls back to the per-
    automation default (_STYLE_DEFAULT_ON). `key` is the storage key (which differs
    from `automation` for the typos_/nonnative_ layers) while the DEFAULT is keyed by
    the automation."""
    if key in stored:
        return bool(stored.get(key))
    return _style_default(automation)


async def load_style_flags(account_id: str) -> dict[str, bool]:
    """Read account_ai_config.style_config_json → {automation: bool} for the
    humanizer / multi-bubble layer. TRI-STATE default (see policy note above): an
    explicit stored True/False wins; an ABSENT key → _STYLE_DEFAULT_ON (ai_chatter
    ON, all others OFF). Absent row / NULL json / parse error → the same absent-key
    defaults (so a brand-new ai_chatter account is ON). STYLE_FORCE_OFF forces every
    flag False regardless."""
    if os.environ.get(_STYLE_FORCE_OFF_ENV):
        return {k: False for k in STYLE_AUTOMATIONS}
    async with get_session() as s:
        cfg = await s.get(AccountAiConfig, str(account_id))
    stored = _parse_style_config(getattr(cfg, "style_config_json", None) if cfg else None)
    # the humanizer flag is stored under the bare automation name (the typos_/
    # nonnative_ layers use a prefixed key — see their own loaders).
    return {k: _resolve_style_flag(stored, k, k) for k in STYLE_AUTOMATIONS}


async def load_painful_texting_flag(account_id: str) -> bool:
    """Account-wide toggle for the PAINFUL_TEXTING framing block (brevity + emotion
    economy) injected at the top of the conversational prompts. Reads the
    'painful_texting' key of account_ai_config.style_config_json. DEFAULT ON
    (absent/NULL/parse-error → True) so it stays live where it's already shipping;
    set the key to false to A/B it off per account. STYLE_FORCE_OFF forces it off."""
    if os.environ.get(_STYLE_FORCE_OFF_ENV):
        return False
    async with get_session() as s:
        cfg = await s.get(AccountAiConfig, str(account_id))
    raw = getattr(cfg, "style_config_json", None) if cfg else None
    if not raw:
        return True
    try:
        stored = json.loads(raw) or {}
    except Exception:
        return True
    val = stored.get(PAINFUL_TEXTING_KEY)
    return True if val is None else bool(val)


async def load_strip_emojis(account_id: str) -> bool:
    """Read account_ai_config.style_config_json → the account-wide 'strip_emojis'
    bool. Absent/NULL/parse-error → False (the safe default: emojis kept, current
    behavior unchanged). When True, senders strip emojis at the send chokepoint."""
    async with get_session() as s:
        cfg = await s.get(AccountAiConfig, str(account_id))
    raw = getattr(cfg, "style_config_json", None) if cfg else None
    if not raw:
        return False
    try:
        stored = json.loads(raw) or {}
    except Exception:
        return False
    return bool(stored.get("strip_emojis"))


def typo_flag_key(automation: str) -> str:
    """style_config_json key for the per-automation typo toggle (separate from the
    humanizer flag so typos can be ticked on independently)."""
    return f"typos_{automation}"


STYLE_TYPO_KEYS = tuple(typo_flag_key(k) for k in STYLE_AUTOMATIONS)


async def load_typo_flags(account_id: str) -> dict[str, bool]:
    """Read account_ai_config.style_config_json → {automation: bool} for the
    thumb-typo injector, keyed by STYLE_AUTOMATIONS (reads the 'typos_<automation>'
    keys). TRI-STATE default (see policy note above load_style_flags): an explicit
    'typos_<automation>' True/False wins; an ABSENT key → _STYLE_DEFAULT_ON
    (ai_chatter ON, all others OFF). STYLE_FORCE_OFF forces every flag False."""
    if os.environ.get(_STYLE_FORCE_OFF_ENV):
        return {k: False for k in STYLE_AUTOMATIONS}
    async with get_session() as s:
        cfg = await s.get(AccountAiConfig, str(account_id))
    stored = _parse_style_config(getattr(cfg, "style_config_json", None) if cfg else None)
    return {k: _resolve_style_flag(stored, k, typo_flag_key(k)) for k in STYLE_AUTOMATIONS}


# Casualize a scripted Q/Tease at SEND time (deep_convo) when the style flag is
# on — lowercase + a few safe word-boundary swaps so the proper-case gen_info
# line ("Bet you can handle more than just a sunrise run, jack.") blends with the
# lowercase voice ("bet u can handle more than just a sunrise run, jack"). Purely
# cosmetic + reversible: nothing stored in fan_profiles changes.
_CASUAL_SWAPS = (
    (re.compile(r"\byou're\b", re.I), "ur"),
    (re.compile(r"\byour\b", re.I), "ur"),
    (re.compile(r"\byou\b", re.I), "u"),
)


def casualize_qtease(text: str) -> str:
    if not text:
        return text
    s = text.lower()
    for rx, rep in _CASUAL_SWAPS:
        s = rx.sub(rep, s)
    return s.rstrip(".").strip()


# ── "Non-native English" style layer (opt-in, deterministic) ──────────
# A non-native speaker misspells the SAME word the SAME way EVERY time — a
# consistent fingerprint, NOT a random thumb-slip (humanize_typos). So this layer is
# applied ALWAYS when the flag is on (like casualize_qtease), never rate-gated. And
# it MUST be code-level: prompt-only misspellings are ignored / "fixed" by the model.
#
# Entries are REAL observed misspellings, not invented — expand from real captures.
# Same PROTECT rules as humanize_typos: never touch a token carrying a digit, an
# @handle, a link, a '$', an emoji, or a protected name. Only the alpha core of a
# token is swapped; surrounding punctuation/spacing is preserved.
NONNATIVE_MISSPELLINGS = {
    "definitely": "deffinetly",
    "gonna": "conna",
    "official": "oficial",
    "alone": "aloane",
    "theirs": "thers",
    "subscription": "subscribtion",
    "telegram": "tegelgram",
    # "then" -> "than" is CONTEXT-RISKY (it would corrupt real "than"); left OUT.
}
# Multi-word phrases (applied before the single-word pass).
NONNATIVE_PHRASES = {
    "of course": "ofcoas",
}
# Every misspelled OUTPUT form — pass to humanize_typos' `protect` so the thumb-typo
# pass never re-mangles a word this layer already mangled (no double-corruption).
NONNATIVE_OUTPUTS = tuple(NONNATIVE_MISSPELLINGS.values()) + tuple(NONNATIVE_PHRASES.values())
_NONNATIVE_PHRASE_RES = tuple(
    (re.compile(rf"\b{re.escape(p)}\b", re.I), r) for p, r in NONNATIVE_PHRASES.items()
)


def _match_case(src: str, repl: str) -> str:
    """Carry `src`'s casing onto `repl` (the voice is lowercase, but be safe)."""
    if src.isupper():
        return repl.upper()
    if src[:1].isupper():
        return repl.capitalize()
    return repl


def apply_nonnative_style(text: str, *, protect=()) -> str:
    """Deterministically swap known words for their non-native misspelling at word
    boundaries (case-insensitive). Pure + reversible (nothing persisted); empty input
    returns input. Protected: a token with a digit / @handle / link / '$' / emoji, a
    non-alpha core, or a name in `protect` is never touched."""
    if not text:
        return text
    protect_set = {w.lower() for name in protect for w in _WORD_RE.findall(str(name))}
    for rx, repl in _NONNATIVE_PHRASE_RES:            # multi-word phrases first
        text = rx.sub(lambda m: _match_case(m.group(), repl), text)
    out = []
    for tok in re.split(r"(\s+)", text):             # keep whitespace runs intact
        if not tok or tok.isspace():
            out.append(tok)
            continue
        core = tok.strip(_TYPO_EDGE_PUNCT)
        repl = NONNATIVE_MISSPELLINGS.get(core.lower())
        if repl is None or not core.isalpha() or core.lower() in protect_set:
            out.append(tok)                          # unknown / unsafe / protected
            continue
        lead = len(tok) - len(tok.lstrip(_TYPO_EDGE_PUNCT))
        out.append(tok[:lead] + _match_case(core, repl) + tok[lead + len(core):])
    return "".join(out)


# Thin prompt block — ONLY the broken grammar a dict can't do (the dict GUARANTEES
# the signature misspellings; this just SETS THE REGISTER). Gated by the same flag.
NONNATIVE_REGISTER = (
    "your english is a little broken — you are NOT a native speaker. occasionally "
    "drop a tiny word (a, the, is) and let your word order be slightly off now and "
    "then. keep it light and still easy to read, never cartoonish."
)


def nonnative_flag_key(automation: str) -> str:
    """style_config_json key for the per-automation non-native toggle."""
    return f"nonnative_{automation}"


STYLE_NONNATIVE_KEYS = tuple(nonnative_flag_key(k) for k in STYLE_AUTOMATIONS)


async def load_nonnative_flags(account_id: str) -> dict[str, bool]:
    """Read account_ai_config.style_config_json → {automation: bool} for the
    non-native layer (the 'nonnative_<automation>' keys). TRI-STATE default (see
    policy note above load_style_flags): an explicit 'nonnative_<automation>'
    True/False wins; an ABSENT key → _STYLE_DEFAULT_ON (ai_chatter ON, all others
    OFF). STYLE_FORCE_OFF forces every flag False."""
    if os.environ.get(_STYLE_FORCE_OFF_ENV):
        return {k: False for k in STYLE_AUTOMATIONS}
    async with get_session() as s:
        cfg = await s.get(AccountAiConfig, str(account_id))
    stored = _parse_style_config(getattr(cfg, "style_config_json", None) if cfg else None)
    return {k: _resolve_style_flag(stored, k, nonnative_flag_key(k)) for k in STYLE_AUTOMATIONS}


# ── "Fact-grounding" personalization layer (Auto Convo / of_ai_chat) ──
# When on, of_ai_chat's reply prompt is fed gen_info's rich profile — the short_bio +
# bullet notes + the team-written teases — plus a "work in ONE specific detail" nudge,
# the same personalization ai_chatter already carries. Makes a bubble land as "she
# remembers me" instead of generic. DEFAULT ON: a fan with no profile on file yet is
# unaffected (the block only appears when there's something to reference), so turning
# it on can never blank or break a reply.
FACTGROUND_KEY = "factground_of_ai_chat"
# Account-wide toggle key (style_config_json) for the PAINFUL_TEXTING framing block.
PAINFUL_TEXTING_KEY = "painful_texting"


async def load_factground_flag(account_id: str) -> bool:
    """Read account_ai_config.style_config_json → the 'factground_of_ai_chat' bool for
    Auto Convo's rich-profile personalization. Absent/NULL/parse-error → True (default
    ON); only an EXPLICIT stored False turns it off."""
    async with get_session() as s:
        cfg = await s.get(AccountAiConfig, str(account_id))
    raw = getattr(cfg, "style_config_json", None) if cfg else None
    if not raw:
        return True
    try:
        stored = json.loads(raw) or {}
    except Exception:
        return True
    return bool(stored.get(FACTGROUND_KEY, True))


# ── "Hard" thumb-typo injector (opt-in, deterministic) ────────────────
# Prompt-only typos are unreliable (the model ignores or over-clusters them, and
# reasoning models "fix" them). This is the CODE-level injector: at most ONE
# realistic phone slip across a reply, ~1 per 5 sentences, and SOMETIMES a
# follow-up "*fix" bubble — the strongest not-a-bot tell. Pure + seeded so it's
# reproducible and testable; OFF unless the caller opts in.
#
# Only readable, meaning-preserving slips (transpose / drop / double-collapse) —
# never a wrong-word "autocorrect". PROTECTED tokens never mutate: anything with
# a digit (prices/ages/times), @handle, link, $, emoji, or a name passed in
# `protect`. Corrupting a price or the fan's name is worse than no typo at all.
_TYPO_SENTENCE_RATE = 0.2        # ~1 typo per 5 sentences (capped at 1 / reply)
# Of the typos that slip in, how many get a self-correct bubble — SEVERITY-WEIGHTED:
# a subtle slip (one dropped letter mid-word) usually rides; an ugly, hard-to-read
# garble gets fixed more often, the way a real person notices the bad ones.
_TYPO_FIX_P_BASE = 0.25          # subtle slip → usually leave it
_TYPO_FIX_P_UGLY = 0.55          # obviously-wrong slip → more likely to "*fix"
# Don't bother "*fix"-ing a SHORT word: a slip in a <6-char word (lol, ur, gonna,
# hey, babe) is instantly guessable, so a real person wouldn't correct it — and a
# "*lol" correction on something that obvious reads like a bot. The thumb-slip
# itself still rides; only the follow-up correction bubble is suppressed.
_TYPO_FIX_MIN_WORD_LEN = 6
_WORD_RE = re.compile(r"[A-Za-z]+")
_VOWELS = frozenset("aeiou")
_SENT_RE = re.compile(r"[.!?]+")
_TYPO_EDGE_PUNCT = ".,!?;:\"'()*"  # stripped from a token's edges before the alpha check


def _typo_eligible(word: str, protect: set[str]) -> bool:
    """A word a real thumb would slip on: ≥4 letters, all-alpha, not a name."""
    return len(word) >= 4 and word.isalpha() and word.lower() not in protect


def _mutate_word(word: str, rng) -> str | None:
    """One readable slip on a single word. None if no valid mutation found.
    transpose: could→coudl · drop: really→realy · double-collapse: gonna→gona."""
    lo = word.lower()
    # double-collapse first when a double exists (most natural-looking)
    doubles = [i for i in range(len(lo) - 1) if lo[i] == lo[i + 1]]
    choice = rng.random()
    if doubles and choice < 0.25:
        i = rng.choice(doubles)
        out = word[:i] + word[i + 1:]
    elif choice < 0.65:                                  # adjacent transpose
        # interior pairs only (don't flip the first letter — too jarring)
        idxs = list(range(1, len(word) - 1))
        if not idxs:
            return None
        i = rng.choice(idxs)
        out = word[:i] + word[i + 1] + word[i] + word[i + 2:]
    else:                                                # drop one interior letter
        idxs = list(range(1, len(word) - 1))
        if not idxs:
            return None
        i = rng.choice(idxs)
        out = word[:i] + word[i + 1:]
    if out == word or len(out) < 3:
        return None
    return out


def _slip_is_ugly(orig: str, slipped: str) -> bool:
    """Does the slip read obviously-wrong at a glance (→ likelier to get fixed)?
    Two cheap eye-catches: the word START reordered (we read first letters first),
    or a NEW 3+ consonant run the original didn't have ('really'→'rae lly' style)."""
    o, s = orig.lower(), slipped.lower()
    if o[:2] != s[:2]:
        return True

    def _max_cons_run(w: str) -> int:
        run = best = 0
        for ch in w:
            run = run + 1 if (ch.isalpha() and ch not in _VOWELS) else 0
            best = max(best, run)
        return best

    return _max_cons_run(s) >= 3 and _max_cons_run(s) > _max_cons_run(o)


# The correction bubble varies its SHAPE so the literal "*word" isn't itself a bot
# signature — weighted across the ways a real person fixes a typo. Seeded via rng.
_TYPO_FIX_FORMS = (
    ("star_pre", 44),    # *word
    ("star_post", 20),   # word*
    ("star_lol", 16),    # *word lol
    ("omg_star", 10),    # omg *word  (names the word — never a bare "omg typo")
    ("retype", 10),      # word       (just retype it, no marker)
)


def _correction_bubble(word: str, rng) -> str:
    """One varied self-correct bubble for `word` (already lowercased on use). Every
    form names the actual word being fixed — no generic 'omg typo' filler (it reads
    like a bot and corrects nothing)."""
    w = word.lower()
    total = sum(wt for _, wt in _TYPO_FIX_FORMS)
    pick = rng.random() * total
    acc = 0
    form = _TYPO_FIX_FORMS[0][0]
    for name, wt in _TYPO_FIX_FORMS:
        acc += wt
        if pick < acc:
            form = name
            break
    return {"star_pre": f"*{w}", "star_post": f"{w}*", "star_lol": f"*{w} lol",
            "omg_star": f"omg *{w}", "retype": w}[form]


def _humanize_typos_impl(parts: list[str], rng, *, protect=(),
                         max_bubbles: int = STYLE_MAX_BUBBLES,
                         allow_correction: bool = True) -> tuple[list[str], bool]:
    """Core injector → (new_parts, correction_emitted). At most one thumb-typo across
    `parts`, rate ~1 per 5 sentences, and SOMETIMES a '*fix' correction bubble. Pure:
    takes an explicit `rng` (random.Random) — caller seeds it off (fan_id, text) for
    reproducibility. When `allow_correction` is False the thumb-slip still rides but
    the '*fix' bubble is suppressed (the per-fan throttle path); the True branch keeps
    the original RNG draw order so seeded callers stay byte-identical."""
    parts = [p for p in parts if p and p.strip()]
    if not parts:
        return parts, False
    protect_set = {w.lower() for name in protect for w in _WORD_RE.findall(str(name))}

    # sentences across the whole reply → P(one typo) = min(1, n * rate)
    n_sent = sum(max(1, len(_SENT_RE.findall(p))) for p in parts)
    if rng.random() >= min(1.0, n_sent * _TYPO_SENTENCE_RATE):
        return list(parts), False

    # collect eligible words across all bubbles, pick one. Scan WHOLE
    # whitespace-tokens (not bare alpha runs) so a word embedded in a handle /
    # link / price ("@lexi_xo", "onlyfans.com/lexi", "$25") is never touched —
    # the token's core must be purely alphabetic after stripping edge punctuation.
    cands = []  # (bubble_idx, core_start, core_end, word)
    for bi, p in enumerate(parts):
        for m in re.finditer(r"\S+", p):
            tok = m.group()
            core = tok.strip(_TYPO_EDGE_PUNCT)
            lead = len(tok) - len(tok.lstrip(_TYPO_EDGE_PUNCT))
            if _typo_eligible(core, protect_set):
                cands.append((bi, m.start() + lead, m.start() + lead + len(core), core))
    if not cands:
        return list(parts), False

    bi, start, end, word = rng.choice(cands)
    slipped = _mutate_word(word, rng)
    if slipped is None:
        return list(parts), False

    out = list(parts)
    out[bi] = out[bi][:start] + slipped + out[bi][end:]

    # self-correct: a beat later, a "*fix" bubble (the strongest not-a-bot tell).
    # Fires whenever there's ROOM under the bubble cap — a 1-bubble reply becomes 2,
    # a 2-bubble reply becomes 3 — but NEVER a stray 4th: if the reply is already at
    # max_bubbles, the "*word" would be a bubble too far (out of place), so we skip.
    # Chance is severity-weighted (ugly garbles get fixed more) and the SHAPE varies
    # so the literal "*word" isn't itself a tell. `allow_correction` short-circuits
    # FIRST so a throttled reply never consumes the fix RNG draw — the allowed path's
    # draw order is unchanged.
    fix_p = _TYPO_FIX_P_UGLY if _slip_is_ugly(word, slipped) else _TYPO_FIX_P_BASE
    emitted = False
    # NOTE: the `rng.random() < fix_p` draw stays FIRST so seeded callers keep their
    # exact draw order; the short-word guard is an extra AND after it (short-circuit
    # only suppresses the emit, never skips the draw) — see _TYPO_FIX_MIN_WORD_LEN.
    if (allow_correction and len(out) < max_bubbles and rng.random() < fix_p
            and len(word) >= _TYPO_FIX_MIN_WORD_LEN):
        out.append(_correction_bubble(word, rng))
        emitted = True
    return out, emitted


def humanize_typos(parts: list[str], rng, *, protect=(),
                   max_bubbles: int = STYLE_MAX_BUBBLES) -> list[str]:
    """Back-compat wrapper returning just the bubble list (drops the emitted flag).
    Callers that need the per-fan '*fix' throttle use apply_typo_throttle instead."""
    return _humanize_typos_impl(parts, rng, protect=protect, max_bubbles=max_bubbles)[0]


# ── Per-fan '*fix' correction throttle ───────────────────────────────
# The thumb-typo itself always rides (an uncorrected slip reads natural), but the
# "*fix" self-correct bubble is the strongest tell — two of them minutes apart reads
# like a bot. So across ALL senders a fan sees at most one correction per window:
# another only after _TYPO_FIX_MIN_INTERVAL has passed OR _TYPO_FIX_MIN_EXCHANGES
# replies have gone by (whichever comes first re-enables it). State is per-fan and
# shared between automations, stamped under fans.custom_fields[_TYPO_FIX_STATE_KEY]
# (an otherwise-unused JSON column → no migration), namespaced under a leading "_"
# so it never collides with a future user-facing custom field.
_TYPO_FIX_MIN_INTERVAL = timedelta(hours=1)
_TYPO_FIX_MIN_EXCHANGES = 50
_TYPO_FIX_STATE_KEY = "_typo_fix"


def _typo_correction_allowed(state: dict, now: datetime) -> bool:
    """True if a '*fix' bubble may fire now given the per-fan throttle `state`
    ({'at': iso, 'since': int}). First correction always allowed; afterwards gated
    on 1h elapsed OR _TYPO_FIX_MIN_EXCHANGES replies since the last one."""
    last_raw = state.get("at")
    if not last_raw:
        return True
    try:
        last = datetime.fromisoformat(last_raw)
    except Exception:
        return True
    since = int(state.get("since", 0) or 0)
    return (now - last) >= _TYPO_FIX_MIN_INTERVAL or since >= _TYPO_FIX_MIN_EXCHANGES


async def apply_typo_throttle(account_id, fan_id, parts, rng, *, protect=(),
                              max_bubbles: int = STYLE_MAX_BUBBLES) -> list[str]:
    """humanize_typos + the per-fan, cross-automation '*fix' throttle. The thumb-slip
    always rides; the correction bubble is suppressed unless _typo_correction_allowed.
    Every reply that runs this layer bumps the per-fan 'since' counter (shared across
    all 4 senders); a fired correction resets it and stamps the time. Returns the
    bubble list sliced to max_bubbles. The fan-lease guarantees one sender per fan per
    tick, so the read-modify-write needs no extra locking."""
    now = datetime.utcnow()
    async with get_session() as s:
        fan = await s.get(Fan, (str(account_id), int(fan_id)))
        try:
            cf = json.loads(fan.custom_fields) if fan and fan.custom_fields else {}
            if not isinstance(cf, dict):
                cf = {}
        except Exception:
            cf = {}
        state = cf.get(_TYPO_FIX_STATE_KEY)
        if not isinstance(state, dict):
            state = {}

        out, emitted = _humanize_typos_impl(
            parts, rng, protect=protect, max_bubbles=max_bubbles,
            allow_correction=_typo_correction_allowed(state, now))
        out = out[:max_bubbles]

        if emitted:
            cf[_TYPO_FIX_STATE_KEY] = {"at": now.isoformat(), "since": 0}
        else:
            cf[_TYPO_FIX_STATE_KEY] = {
                "at": state.get("at"),
                "since": int(state.get("since", 0) or 0) + 1,
            }
        if fan is not None:
            fan.custom_fields = json.dumps(cf)
            await s.commit()
    return out


# ── Human "typing speed" delay ───────────────────────────────────────
# Each bubble is held back for the time a real person would take to TYPE it, so
# replies don't pop instantly (and a 2-bubble reply has a believable gap). Speed
# is words-per-minute, configured per-account in webhook_config_json.typing_wpm
# (the "⚡ Instant reply" tab); default 38 wpm. Clamped so a long line can't hang.
_DEFAULT_TYPING_WPM = 38.0
_MAX_TYPING_DELAY_S = 60.0  # a single bubble never waits more than 1 min to "type"


def typing_delay_seconds(text: str, wpm: float) -> float:
    """How long it'd take to type `text` at `wpm` words/min (0 wpm → no delay)."""
    if not wpm or wpm <= 0:
        return 0.0
    words = len((text or "").split())
    return min(words / float(wpm) * 60.0, _MAX_TYPING_DELAY_S)


async def load_typing_wpm(account_id: str) -> float:
    """Per-account typing speed from webhook_config_json.typing_wpm (default 38).
    Read regardless of whether webhook dispatch is enabled — it's a send-pacing
    knob, not a dispatch gate. 0 disables the typing delay."""
    if os.environ.get("CHATTERLY_TEST_MODE"):
        return 0.0  # no real sleeps in the test harness
    async with get_session() as s:
        cfg = await s.get(AccountAiConfig, str(account_id))
    if cfg is not None and cfg.webhook_config_json:
        try:
            d = json.loads(cfg.webhook_config_json) or {}
            if "typing_wpm" in d and d["typing_wpm"] is not None:
                return max(0.0, float(d["typing_wpm"]))
        except Exception:
            log.warning("bad typing_wpm account=%s", account_id, exc_info=True)
    return _DEFAULT_TYPING_WPM


# ── "...is typing" indicator (ON by default, opt-out per account) ─────
# When enabled, the typing-delay hold before each bubble ALSO emits OF's live
# typing frame to the fan, so they actually see the "...is typing" bubble during
# the wait (instead of the message just arriving late). Default ON for every
# account; opt OUT per-account with webhook_config_json.typing_indicator=false.
# Re-emit cadence matches OF's own (~2.5s) since the indicator auto-clears after
# a few seconds. Always False under CHATTERLY_TEST_MODE (no real frames in tests).
_TYPING_REEMIT_S = 2.5
_DEFAULT_TYPING_INDICATOR = True


async def load_typing_indicator(account_id: str) -> bool:
    """Per-account toggle for the live typing indicator (webhook_config_json.
    typing_indicator). Default ON; only an explicit `false` disables it.
    Absent/NULL key → ON. Parse-error → default. Test-mode → False."""
    if os.environ.get("CHATTERLY_TEST_MODE"):
        return False
    async with get_session() as s:
        cfg = await s.get(AccountAiConfig, str(account_id))
    if cfg is not None and cfg.webhook_config_json:
        try:
            d = json.loads(cfg.webhook_config_json) or {}
            if "typing_indicator" in d and d["typing_indicator"] is not None:
                return bool(d["typing_indicator"])
        except Exception:
            log.warning("bad typing_indicator account=%s", account_id, exc_info=True)
    return _DEFAULT_TYPING_INDICATOR


async def hold_with_typing(account_id: str, fan_id: int, seconds: float,
                           *, typing_indicator: bool = False) -> None:
    """Wait `seconds` before sending a bubble. When `typing_indicator` is on,
    emit OF's typing frame to `fan_id` every ~2.5s during the hold so the fan
    sees the live "...is typing" bubble; otherwise just sleep. Always safe — a
    missing live socket degrades to a plain sleep."""
    if seconds <= 0:
        return
    if not typing_indicator:
        await asyncio.sleep(seconds)
        return
    from of_ws import emit_typing  # local import: avoid a server↔automation cycle
    remaining = float(seconds)
    while remaining > 0:
        try:
            await emit_typing(account_id, int(fan_id))
        except Exception:
            log.debug("emit_typing failed account=%s fan=%s", account_id, fan_id,
                      exc_info=True)
        step = min(_TYPING_REEMIT_S, remaining)
        await asyncio.sleep(step)
        remaining -= step


async def resolve_model(account_id: str, purpose: str, override: str | None = None) -> str:
    """Resolve the LLM model for one automation run, VALIDATED against
    llm_client.MODELS at every step so a stale/typo value can never reach
    llm_client.chat (where it would fail every fan with LLMConfigError).

    Precedence: payload `override` → account_ai_config.model_by_purpose[purpose]
    → account_ai_config.model → DEFAULT_MODEL. Any value not in MODELS is
    skipped (not raised) so resolution always yields a usable model.
    """
    if override and override in llm_client.MODELS:
        return override
    async with get_session() as s:
        cfg = await s.get(AccountAiConfig, account_id)
    if cfg is not None:
        if cfg.model_by_purpose:
            try:
                chosen = (json.loads(cfg.model_by_purpose) or {}).get(purpose)
            except Exception:
                chosen = None
                log.warning("bad_model_by_purpose account=%s purpose=%s", account_id, purpose)
            if chosen and chosen in llm_client.MODELS:
                return chosen
        if cfg.model and cfg.model in llm_client.MODELS:
            return cfg.model
    return DEFAULT_MODEL


_FACTS_NOTE_MAX = 200


def build_facts_note(facts: dict, max_len: int = _FACTS_NOTE_MAX,
                     short_bio: str = "") -> str:
    """Compact, useful-first fact line built from a fan's extracted facts.

    Shared by apply_profiles (→ fans.applied_notes / the drawer) and push_to_sheets
    (→ the bullet_points column) so the note and the sheet NEVER drift. Order is
    most-useful-first with SHORT labels, ONE FACT PER LINE: Recent → Intr (hobbies)
    → Job → Rel → Kinks, then the short_bio appended at the BOTTOM. EMPTY fields are
    skipped entirely (never a bare "Job:" line). Name, age and location are omitted
    (they live in the nickname) and lifetime spend too (the sheet prints it), so
    nothing is duplicated. Deterministic; whole-fact BULLETS are kept-or-skipped by
    budget, then the short_bio fills the remaining space and the whole note is
    hard-cut at max_len (the bio may be cut mid-sentence). '' when nothing is known.

    `facts` keys (all optional, str): hobbies, job, relationship, fetishes, and
    recent_events (a JSON-array TEXT value or a plain string). `short_bio` is the
    fan's one-line bio, appended last (lowest priority).
    """
    f = facts or {}
    events = ""
    try:
        ev = json.loads(f.get("recent_events") or "[]")
        if isinstance(ev, list):
            events = "; ".join(str(x).strip() for x in ev if str(x).strip())
    except (json.JSONDecodeError, TypeError):
        events = (f.get("recent_events") or "").strip().strip("[]")
    candidates = [
        ("Recent", events),
        ("Intr", f.get("hobbies", "")),
        ("Job", f.get("job", "")),
        ("Rel", f.get("relationship", "")),
        ("Kinks", f.get("fetishes", "")),
    ]
    out: list[str] = []
    used = 0
    for label, val in candidates:
        val = (val or "").strip()
        if not val:
            continue                            # skip empty fields entirely
        piece = f"{label}: {val}"
        add = (1 if out else 0) + len(piece)   # "\n" separator (one fact per line)
        if used + add > max_len:
            continue                            # skip; a shorter later fact may fit
        out.append(piece)
        used += add
    # short_bio at the BOTTOM, lowest priority: fill whatever budget the whole-fact
    # bullets left and HARD-CUT the whole note at max_len. Unlike the bullets (which
    # are kept whole or skipped), the bio may be cut mid-sentence — the rule is simply
    # "bullets, then bio, up to max_len chars".
    bio = (short_bio or "").strip()
    if bio:
        out.append(bio)
    return "\n".join(out)[:max_len]


# ── R2: the APP-ONLY rich note ───────────────────────────────────────
# The OF note is capped at 200 (build_facts_note above, pushed by apply_profiles /
# of_ai_chat / deep_convo). Our UI can show much more. build_rich_note is a SEPARATE,
# app-only projection that reads the STORED gen_info bullet_points (the rich text that
# actually carries the "Important:" section — the push_to_sheets rebuild drops it) plus
# the short_bio, and caps by the fan's spend tier (higher spend ⇒ longer). It is NEVER
# pushed to OnlyFans and never fed to build_facts_note. Drawer + stats table only.
_RICH_NOTE_TIERS = (        # (min lifetime cents, char cap)
    (50000, 3000),          # whale ≥ $500
    (5000, 1500),           # spender ≥ $50
    (1, 800),               # buyer > $0
    (0, 400),               # free $0
)


def rich_note_cap(lifetime_spend_cents: int) -> int:
    """The app-side rich-note length cap for a fan, by spend tier (higher = longer)."""
    cents = int(lifetime_spend_cents or 0)
    for floor, cap in _RICH_NOTE_TIERS:
        if cents >= floor:
            return cap
    return _RICH_NOTE_TIERS[-1][1]


def build_rich_note(bullet_points: str | None, short_bio: str = "",
                    lifetime_spend_cents: int = 0) -> str:
    """The app-only rich note: the STORED bullet_points (kept whole — it already holds
    Recent/Intr/Important/Job/Rel/Kinks from gen_info) followed by the short_bio, hard-cut
    at the fan's spend-tier cap. '' when nothing is stored. The structured facts
    (employer/pets/…) are shown as their own cells in the drawer, so they're not
    duplicated here."""
    cap = rich_note_cap(lifetime_spend_cents)
    parts = []
    bp = (bullet_points or "").strip()
    if bp:
        parts.append(bp)
    bio = (short_bio or "").strip()
    if bio and bio not in bp:
        parts.append(bio)
    return "\n\n".join(parts)[:cap]


# ── Live nickname + note push (V1 build_structured_nickname / apply_nickname) ──
_NICK_MAX = 70


def build_structured_nickname(f: Fan) -> str:
    """`Name/City,Country/Age/Job` from a fan's facts (port of V1
    build_structured_nickname; Job replaces V1's hobbies to match gen_info's
    nickname format). Empty segments are dropped, so a thin profile yields a
    short nickname. '' when nothing is known."""
    # `custom_nickname` is the OUTPUT this function mirrors back (via
    # of_ai_chat._maybe_push_nickname, every tick). Feeding the whole structured
    # string back in as the Name made each tick re-append loc/age/job, growing
    # 'Donovon/chef/25' → 'Donovon/chef/25/chef/25/chef/nanaimo,canada/25/…' until
    # the 70-char cap. Pull only the NAME slot from it so the loop can't compound.
    name = ((getattr(f, "real_name", None) or "").strip()
            or name_token(getattr(f, "custom_nickname", None))
            or (getattr(f, "of_display_name", None) or "").strip())
    age = (f.his_age or "").strip()
    country = (f.home_country or "").strip()
    city = (f.home_city or "").strip()
    job = (f.occupation or "").strip()
    loc = ",".join(p for p in (city, country) if p)
    parts = [p for p in (name, loc, age, job) if p]
    return "/".join(parts)[:_NICK_MAX]


def facts_from_fan(f: Fan) -> dict:
    """The fact dict `build_facts_note` consumes, lifted off a Fan row."""
    return {
        "hobbies": f.hobbies or "",
        "job": f.occupation or "",
        "relationship": getattr(f, "relationship_status", "") or "",
        "fetishes": f.fetishes or "",
        "recent_events": f.recent_events or "[]",
    }


async def push_nick_and_notes(client, account_id: str, fan_id: int, *,
                              nick: str = "", notes: str = "") -> tuple[bool, bool]:
    """Push the custom nickname (displayName — ANY fan) and/or note (notice —
    SUBSCRIBERS only) to OF via of_client, then MIRROR what stuck into the local
    columns the UI reads (`fans.custom_nickname` / `fans.notes`). SEPARATE OF calls
    so a non-subscriber note 404 can't swallow the nickname. of_client is sync →
    off-thread. Best-effort: each push failure is logged and swallowed. Returns
    (nick_pushed, note_pushed)."""
    nick_ok = note_ok = False
    if nick:
        try:
            await asyncio.to_thread(client.set_fan_custom_name, int(fan_id), nick)
            nick_ok = True
        except Exception:
            log.warning("push_nick_failed account=%s fan=%s", account_id, fan_id, exc_info=True)
    if notes:
        try:
            await asyncio.to_thread(client.set_fan_note, int(fan_id), notes)
            note_ok = True
        except Exception as e:
            log.info("push_note_skipped account=%s fan=%s (%s)", account_id, fan_id,
                     type(e).__name__)
    mirror: dict = {}
    if nick_ok:
        mirror["custom_nickname"] = nick
    if note_ok:
        mirror["notes"] = notes
    if mirror:
        now = datetime.utcnow()
        mirror["updated_at"] = now
        async with get_session() as s:
            await s.execute(
                sqlite_insert(Fan)
                .values(account_id=str(account_id), fan_id=int(fan_id), **mirror)
                .on_conflict_do_update(index_elements=["account_id", "fan_id"], set_=mirror)
            )
    return nick_ok, note_ok


def name_token(s: str | None, *, last: bool = False) -> str:
    """Pull a real-NAME token (Capitalized, letters only, len≥2) out of a string,
    or '' if it doesn't look like a name (handles like 'xx_gamer_99' / 'u123' are
    rejected — they aren't Capitalized real names). Slash-structured nicknames
    ('John/Orange City,USA/Horny-Fan') keep the first slot ('John'); pass last=True
    for AI/curated nicknames so 'Sexy Sofie' → 'Sofie' (the name, not the adjective).

    This is the canonical parser (lifted from send_welcome._name_token so every
    sender derives names identically — single source of truth)."""
    if not s:
        return ""
    seg = str(s).split("/")[0].strip()       # 'John/City/Tag' → 'John'
    if "," in seg:                            # 'Whistler,Canada/Whale' has no name slot
        return ""                             # (a comma marks a City,Country location)
    words = re.split(r"\s+", seg) if seg else []
    if not words:
        return ""
    raw = words[-1] if last else words[0]
    if not raw[:1].isupper():                 # real names are Capitalized; handles aren't
        return ""
    # Keep letters only, but UNICODE-correct: str.isalpha() preserves accented names
    # (José, Ángel, Muñoz, Nicolás) that the old [^A-Za-z] strip mangled to Jos/ngel/Muoz.
    w = "".join(ch for ch in raw if ch.isalpha())
    return w if len(w) >= 2 else ""


def resolve_fan_name(f) -> str:
    """Single source of truth for 'what real first name do we greet this fan by'.

    Why this exists: OF *message* payloads carry only `fromUser:{id}` — no name — so
    `of_username`/`of_display_name` are empty for ~80% of fans, and the WS pump used
    to clobber them to NULL on every message. The team-curated `custom_nickname`
    ('John/City/Tag') is then the most reliable name signal we hold, but the senders
    never consulted it and emitted a literal 'Babe'.

    Precedence keeps each sender's prior behaviour for the populated cases
    (real_name → generated_nickname → of_display_name, used verbatim) and only adds a
    new tail that parses the curated custom_nickname. `of_username` is deliberately
    excluded — handles like 'u123' / 'alexnielsen' aren't greetable names. Returns ''
    when we truly have nothing; callers keep their own soft 'babe' fallback.

    `f` may be a Fan ORM row or a dict of the same fields."""
    def _g(attr: str) -> str:
        if f is None:
            return ""
        if isinstance(f, dict):
            return str(f.get(attr) or "").strip()
        return str(getattr(f, attr, "") or "").strip()

    chained = _g("real_name") or _g("generated_nickname") or _g("of_display_name")
    return chained or name_token(_g("custom_nickname"), last=True)


def substitute_placeholders(text: str, fan, *, name: str | None = None) -> str:
    """Fill `{name}`/`{city}`/`{age}`/`{hobby}`/`{pet}` in a template line from a
    Fan row (nudge_online tease/qa pools). Used so the variation arrays can carry
    natural personalization without each automation re-deriving it.

    `fan` may be a Fan ORM row, a dict of the same fields, or None. `name` overrides
    the {name} value (callers pass the already-resolved real first name); when it's
    empty/None we fall back to a soft 'babe' rather than leaving a hole like
    'morning sleepyhead  ☀️'. Unknown placeholders are left untouched. An EMPTY
    fact (no city/pet/…) collapses its placeholder to '' (the surrounding copy is
    written to read fine without it)."""
    def _get(attr: str) -> str:
        if fan is None:
            return ""
        if isinstance(fan, dict):
            return str(fan.get(attr) or "").strip()
        return str(getattr(fan, attr, "") or "").strip()

    # {hobby}: first comma/semicolon-separated hobby. {pet}: first pet name from the
    # JSON pets array (falls back to the kind, e.g. 'dog').
    hobby = ""
    raw_hobby = _get("hobbies")
    if raw_hobby:
        hobby = re.split(r"[,;\n]", raw_hobby)[0].strip()
    pet = ""
    raw_pets = _get("pets")
    if raw_pets and raw_pets not in ("[]", "{}"):
        try:
            arr = json.loads(raw_pets)
            if isinstance(arr, list) and arr and isinstance(arr[0], dict):
                pet = str(arr[0].get("name") or arr[0].get("kind") or "").strip()
        except (json.JSONDecodeError, TypeError):
            pet = ""

    nm = (name or "").strip() or "babe"
    repl = {
        "{name}": nm,
        "{city}": _get("home_city"),
        "{age}": _get("his_age"),
        "{job}": _get("occupation"),
        "{hobby}": hobby,
        "{pet}": pet,
    }
    out = text or ""
    for k, v in repl.items():
        if k in out:
            out = out.replace(k, v)
    # Tidy any double spaces left by an empty fact, but keep newlines.
    return re.sub(r"[ \t]{2,}", " ", out).strip()


def coerce_ids(raw: object) -> set[int]:
    """Payload id-list → set[int], dropping (not raising on) non-numeric entries.

    Payload values are JSON/operator-supplied; one bad `force_ids` entry must
    never abort the whole automation run.
    """
    out: set[int] = set()
    if not isinstance(raw, (list, tuple, set)):
        return out
    for x in raw:
        try:
            out.add(int(x))
        except (TypeError, ValueError):
            continue
    return out


# ── Word restriction (port of V1 TgAiChattingShare/word_filter.py) ────
# OnlyFans soft-blocks / flags a fixed set of words; V1 doubled the first vowel of
# each ("meet" → "meeet", "rape" → "raape") so the copy reads naturally to the fan
# but slips the filter. Every outbound automation send should pass through this —
# the legacy `apply_word_restriction` ran on EVERY message. One source of truth.
_RESTRICTED_WORDS = {
    "abduction", "abduct", "abducting", "abducted", "animal", "asphyxia", "asphyxication",
    "asphyxiation", "asphyxiated", "asphyxiate", "asphyxiating", "ballbusting", "bait",
    "bareback", "blackmail", "beastiality", "bleeding", "blood", "bloodplay", "blooded",
    "blacked", "bukkake", "bestiality", "caned", "caning", "canned", "canning", "cbt",
    "cashapp", "cannibal", "cervix", "cervics", "cerviks", "cervicks", "comatose", "coma",
    "child", "chocked", "choke", "choking", "chokes", "chocking", "choked", "choloroforming",
    "chloroform", "chloroformed", "chloroforming", "cycle", "cp", "consent", "drink",
    "drinking", "drunken", "drunk", "diapers", "doze", "dog", "eleven", "entrance", "escort",
    "escorted", "escorting", "enema", "fansonly", "fansly", "fuckfan", "fanfuck", "fetal",
    "fuckafan", "fanfucked", "fecal", "facentro", "fancentro", "foetal", "fisted", "fist",
    "fisting", "farm", "flogging", "forcing", "force", "forced", "forceful", "forced bi",
    "fifteen", "gaping", "golden", "gangbang", "gangbanging", "gangbanged", "gangbangs",
    "hardsports", "hypno", "hypnotize", "hypnotization", "hypnotizing", "hypnotized", "hooker",
    "inbreed", "inbreeded", "inbreeding", "incapacitation", "incapacitate", "inzest", "incest",
    "intox", "jail", "jailed", "jailbait", "kidnap", "kidnapped", "kidnapping", "kidnapper",
    "knocked", "knock", "knocking", "lactation", "lactating", "lactate", "lolicon", "lolita",
    "lalita", "menstruate", "menstruating", "menstruation", "menstrual", "many vids",
    "medical play", "molested", "molesting", "molest", "meet", "mutilated", "mutilate",
    "mutilating", "mutilation", "necrophilia", "nigger", "pedo", "pedophile", "pedophilia",
    "prostituted", "paralyze", "paralyzed", "paralyzation", "pee", "peeplay", "pissed",
    "poo", "pissing", "piss", "poop", "pooped", "pooping", "pegging", "paddling", "paypal",
    "passed out", "prostitution", "prostitute", "prostituting", "pse", "preteen", "pre-teen",
    "pre-scat", "rapped", "rapping", "rape", "raped", "raping", "rapist", "restricted",
    "snuff", "showers", "skat", "strangle", "strangling", "strangled", "suffocate",
    "suffocation", "suffocated", "teen", "toilet", "torture", "slave", "scat", "strangulation",
    "slavery", "torturing", "tortured", "trance", "twelve", "unconsciousness", "unconscious",
    "unwilling", "underage", "vomit", "vomited", "vomiting", "vomino", "venmo", "whipped",
    "whipping", "watersports", "young", "zoophilia",
}
_VOWELS = "aeiouAEIOU"
# Longest-first so a phrase ("medical play") matches before a contained word ("play").
_RESTRICTED_RE = re.compile(
    r"\b(" + "|".join(re.escape(w) for w in sorted(_RESTRICTED_WORDS, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)


def _double_first_vowel(m: "re.Match") -> str:
    word = m.group(0)
    for i, ch in enumerate(word):
        if ch in _VOWELS:
            dup = ch.lower() if i == 0 and ch.isupper() else ch
            return word[: i + 1] + dup + word[i + 1:]
    # no vowel (e.g. "cp", "cbt") → double the first char instead
    return word[0] + word[0].lower() + word[1:] if word else word


def apply_word_restriction(text: str) -> str:
    """Double the first vowel of any OnlyFans-restricted word, whole-word,
    case-insensitive ("Let's meet" → "Let's meeet"). Empty/None passes through."""
    if not text:
        return text
    return _RESTRICTED_RE.sub(_double_first_vowel, text)


# ── Deterministic off-platform / contact / meetup guard ──────────────
# ONPLATFORM_GUARDRAIL (the system-prompt rule) lowers the odds the model drifts
# into arranging an in-person meetup or swapping contact info — but a prompt is
# probabilistic and a pushy fan can still win. This is the deterministic FLOOR:
# scan the model's OUTBOUND text and, if it leaks a phone number, an off-platform
# channel, or an in-person meet arrangement, swap the WHOLE message for a warm
# on-platform deflection. Whole-message (not surgical) on purpose — editing out
# "thursday" from "i'll be at the harbour thursday" is exactly how you leave the
# arrangement half-standing. Scan BEFORE apply_word_restriction, which mangles
# "meet"->"meeet" and would hide it from these patterns.

# 7+ digits with optional separators — a phone number in a flirty DM is ~never benign.
_OFF_PHONE_RE = re.compile(r"(?:\+?\d[\s().\-]?){7,}\d")
# An email address.
_OFF_EMAIL_RE = re.compile(r"\b[\w.+\-]+@[\w\-]+\.[a-z]{2,}\b", re.IGNORECASE)
# Named off-platform channels (some overlap the word filter — harmless, different action).
_OFF_CHANNEL_RE = re.compile(
    r"\b(snap\s?chat|telegram|whats\s?app|kik|wickr|signal|discord|"
    r"insta(\s?gram)?|tiktok|cash\s?app|venmo|pay\s?pal|zelle|gmail|e-?mail)\b",
    re.IGNORECASE,
)
# Asking for / handing over contact details.
_OFF_CONTACT_RE = re.compile(
    r"\b("
    r"(my|ur|your|the)\s+(number|digits|cell|phone)|"
    r"(text|call|message|msg|dm|hit|add|reach|find|ping)\s+me\s+(on|at|up\s+on|via|through)|"
    r"(give|send|drop|share|swap|exchange)\s+(me\s+)?(?:(?:ur|your|my)\s+)?(number|digits|contact|snap|insta|socials?)|"
    r"what'?s?\s+(ur|your)\s+(number|snap|insta|@)|"
    r"add\s+me\s+on"
    r")\b",
    re.IGNORECASE,
)
# Arranging an in-person meet (the OF-flag category itself).
_OFF_MEETUP_RE = re.compile(
    r"\b("
    r"meet\s?up|meet\s+(me|you|u|irl|in\s+person)|let'?s\s+meet|gonna\s+meet|"
    r"in\s+person|irl|see\s+(you|u)\s+(there|then|soon|tonight|tomorrow)|"
    r"come\s+(see|over|visit)\s+me|come\s+(to|to\s+my)|"
    r"grab\s+(a\s+)?(drink|coffee|dinner|bite)\s+(with|together|sometime)|"
    r"link\s+up\s+(irl|in\s+person)|hook\s?up\s+in\s+person"
    r")\b",
    re.IGNORECASE,
)
# "i'll be at <place> <day/time>" — names a real-world rendezvous (place + when).
# Kept separate from bland "i'll be in bed" by REQUIRING a day-of-week / clock anchor.
_OFF_RENDEZVOUS_RE = re.compile(
    r"\bi'?(ll|m\s+gonna)\s+be\s+(at|in|near|by|around|outside)\b.{0,40}\b("
    r"mon(day)?|tue(s|sday)?|wed(nesday)?|thu(r|rs|rsday)?|fri(day)?|sat(urday)?|sun(day)?|"
    r"tomorrow|tonight|this\s+(week|weekend)|next\s+week|noon|"
    r"\d{1,2}\s?(am|pm)|morning|afternoon|evening"
    r")\b",
    re.IGNORECASE,
)

_OFF_PATTERNS = (
    ("phone", _OFF_PHONE_RE),
    ("email", _OFF_EMAIL_RE),
    ("channel", _OFF_CHANNEL_RE),
    ("contact", _OFF_CONTACT_RE),
    ("meetup", _OFF_MEETUP_RE),
    ("rendezvous", _OFF_RENDEZVOUS_RE),
)

# Warm, on-voice redirects back to the chat. Emojis are fine — deep_convo strips
# them in _send; everywhere else they read normally.
_OFF_DEFLECTIONS = (
    "u dont need my number when ur right here 😏 keep me company",
    "mmm i only do this on here babe, talk to me",
    "lets keep it just between us right here 😉 tell me more",
    "ur sweet but im all yours on here, what else u thinkin about",
    "i stay on here only, come closer n tell me more",
    "no need to go anywhere, ive got u right here babe",
)


def scan_offplatform(text: str) -> list[str]:
    """Return the category labels of any off-platform/contact/meetup leak in
    `text` (empty list = clean). Scan the model's RAW text, before word
    restriction. Used as a deterministic safety net over ONPLATFORM_GUARDRAIL."""
    if not text:
        return []
    return [label for label, rx in _OFF_PATTERNS if rx.search(text)]


def guard_offplatform(text: str, rng) -> tuple[str, list[str]]:
    """If `text` leaks off-platform content, swap the WHOLE message for a canned
    on-platform deflection; else pass it through. Returns (text, reasons) so the
    caller can log when it fired. `rng` is a seeded random.Random for stable picks."""
    reasons = scan_offplatform(text)
    if not reasons:
        return text, reasons
    return rng.choice(_OFF_DEFLECTIONS), reasons


# ── "Fan asked to see content via text" → natural tip-ask ─────────────
# When a fan asks to SEE content in a plain text ("can i see some content??",
# "show me more", "what u got"), the info-gather / keep-warm senders (of_ai_chat,
# autoreply) don't sell PPV — but answering with banter ignores a buying signal,
# and the old never-sell fallback literally blurted the bare word "tip". So those
# senders swap in a natural, in-voice tip-ask ("tip me $X and i'll send u something
# 😏") — NEVER the bare word "tip". The fan then tips and the existing tip_reward
# automation delivers the media: ask → tip → reward.
#
# Two natural ways to tip (the model picks whichever flows): a $amount tip right
# here in chat, OR a tip under a feed post/pic he likes. A post tip lands in chat
# as a tip message too, so it routes through the SAME on_inbound_tip → tip_reward
# path (no new trigger needed) — and it doubles as a signal of what he's into.
#
# ── Text normalisation. READ THIS BEFORE TOUCHING ANY PATTERN BELOW. ─────────────
#
# Phone keyboards autocorrect ' into ’ (U+2019, RIGHT SINGLE QUOTATION MARK). 14.4% of
# real inbound messages on prod carry one. Every pattern in this file and in upsell.py
# spells contractions with a STRAIGHT apostrophe (`can'?t`, `i'?m`, `what'?s`), and a
# curly one does not match it. So:
#
#     "Sorry can't afford to buy content"  → decline=soft, regret=True   ✅
#     "Sorry can’t afford to buy content"  → decline=None, regret=False  ❌
#
# The second is what a man actually types. The poverty brake, the decline classifier and
# the buying-signal detectors were all silently blind to him — which means the seller
# would put a FRESH PRICE in front of a man who had just said he was broke. That is the
# worst thing this system can do, and it was one character wide.
#
# Normalise at every detector entry point (defence in depth — these are pure functions
# called from several places), never by asking each pattern to spell both forms.
_SMART_PUNCT = str.maketrans({
    "’": "'", "‘": "'", "‛": "'", "′": "'", "`": "'",
    # ʼ U+02BC (several Android keyboards + copy-paste from non-English sources) and
    # ＇ U+FF07 (fullwidth/CJK IMEs) reproduce the SAME failure as U+2019: "can ʼ t
    # afford" matched nothing, so a broke man got re-priced. Fold every apostrophe
    # variant a phone can emit, not just the famous one.
    "ʼ": "'", "＇": "'", "՚": "'", "ꞌ": "'", "᾿": "'",
    "“": '"', "”": '"',
    "–": "-", "—": "-",
})


def norm_text(text: str | None) -> str:
    """Fold smart punctuation to ASCII so the detectors see what he meant to type."""
    return (text or "").translate(_SMART_PUNCT)


# CONTENT_ASK_RE is the canonical detector (hoisted from ai_chatter, which still
# uses it for its PPV pitch). Code-side, not model judgment — when he's begging to
# buy, the gather goal yields. Extended over the original to also catch the
# "(can i) see (some) content/pics/..." family the original missed.
CONTENT_ASK_RE = re.compile(
    r"(what else|what'?s next|whats next|show me|send (me |it |smth |something )"
    r"|got (any|anything)|anything (spicy|hot|else|for (me|us))|gimme|"
    r"i want (more|it|some)|more (pics|vids|photos|videos|content)|next one|"
    r"what (else )?(do|did) (u|you) (have|got|film)|unlock|spoil (u|you)|"
    r"in the mood|what (u|you) got"
    r"|can i see|lemme see|let me see|wanna see|want to see"
    # Real corpus (fans who then PAID $25-$45): the ask is rarely the tidy "wanna see".
    # "I would like to see how you touch your pussy" and "Any videos of you getting
    # fucked?" were both invisible to the patterns above.
    r"|(would |i'?d )?(like|love) to see|dying to see|need to see|"
    r"any (pics?|vids?|videos?|photos?|nudes?|content)|"
    r"(send|show|do) (u|you) have|"
    # Recall (2026-07-15 audit): "send nudes" / "i want nudes" — the most iconic buy
    # signal on the platform — matched neither branch. `nudes` has no innocent reading.
    r"(send|want|get|gimme|got any) (me |some |ur |your )?nudes?\b|"
    r"send (me |ur |your |some )?(pics?|vids?|videos?|content)\b|"
    # NOT a bare `see (how|what|you|u|it)`. That matched "see you later babe" — the most
    # common sign-off on the platform — plus "i see what you mean" and "see you
    # tomorrow". Two goodbyes in a 6-message window were enough to make thread_heat()
    # return True, so with force_ask on a fan would have been sent a PPV for saying
    # goodnight. It added no recall the branches above don't already have: "i would like
    # to see how you touch your pussy" is caught by `(would |i'?d )?(like|love) to see`.
    r"i (want|need) (to see|you to)"
    r"|see (some |the |ur |your |more |any )?"
    r"(content|pics?|photos?|vids?|videos?|nudes?|something))",
    re.IGNORECASE)


def is_content_ask(text: str | None) -> bool:
    """True when the fan's message is explicitly asking to SEE content (the buying
    signal CONTENT_ASK_RE detects). Empty/None → False."""
    return bool(text) and bool(CONTENT_ASK_RE.search(norm_text(text)))


# ESCALATION_RE — closer-ONLY buying-adjacent signal. A fan getting physical /
# horny ("can i spank it", "wanna play", "so hard") is a HOT moment the SELLER
# (ai_chatter) should ride, even though it's not an explicit "show me content"
# ask. Kept SEPARATE from CONTENT_ASK_RE on purpose: CONTENT_ASK_RE also drives
# of_ai_chat's and autoreply's tip-ask, and we do NOT want the opener pitching
# mid-gather — only the closer's intent gate consults this.
ESCALATION_RE = re.compile(
    r"(spank|choke|"
    r"wanna play|let'?s play|can i play|"
    r"so hard|getting hard|i'?m hard|im hard|rock hard|"
    r"turn(?:s)? me on|so horny|i'?m horny|im horny|"
    r"make me (?:cum|wet|hard)|get me off|"
    r"can i (?:touch|taste|feel|lick|kiss) (?:it|u|you|ur|your)|"
    r"wanna (?:touch|taste|feel|fuck) (?:it|u|you|ur|your)|"
    r"f+u+c+k+ (?:you|u|me)|"
    # ── Corpus-derived. He is DESCRIBING AN ACT HE WANTS. That — not arousal — is the
    # signal, and per the converting threads it is the strongest one there is: it is his
    # answer to "what would you do to me if i were there", which is the move every top
    # closer opens with. Real men who then paid $25-$45 wrote:
    #   "in doggy or missionary maybe even a blowjob"
    #   "id love to bury face and cock in that pretty pussy"
    #   "I want you gushing down my chin"
    #   "I would like to see how you touch your pussy"
    #
    # A DESIRE FRAME next to an act — not a bare anatomy word. Matching bare anatomy was
    # measured against prod and it fired on 13% of ALL inbound (vs 4% before), so recall
    # went up but the LIFT over base rate collapsed from 2.5x to 1.3x: it had widened
    # into noise. "nice tits" is arousal; "id love to bury my face in them" is intent.
    # Pricing arousal is how she reads as a bot.
    # A DESIRE FRAME near an UNAMBIGUOUS sexual token. The token list used to include
    # bare English verbs (eat / deep / touch / ride / strip / inside / ass), which made
    # ALL of these "escalation": "i need to eat dinner", "i want a deep conversation",
    # "i like riding motorcycles", "i need to touch base with my boss". With force_ask
    # on, that is a PPV for talking about dinner. Soft verbs now require a body object.
    # Subject pronoun is OPTIONAL (recall audit): fans drop the "I" constantly — "wanna
    # suck your tits", "gonna make you cum". The downstream requirement (an unambiguous
    # token, OR a soft verb + body object) is what keeps it off ordinary talk even
    # without the pronoun. A bare desire verb alone matches nothing.
    r"(?:(?:i|id|i'?d|im|i'?m|u|you)\s+)?(?:want|wanna|need|love|would love|like|"
    r"gonna|gon|wish|hope|plan|dream|dying)\b[^.!?]{0,45}?"
    r"(?:\b(?:cum|cumming|cock|dick|pussy|clit|tits?|titties|nipples|blowjob|bj|"
    r"jerk|gush|squirt|throb|deepthroat|handjob|creampie|"
    r"naked|nude|horny)\b"                      # unambiguous on their own
    r"|\b(?:suck|lick|eat|finger|stroke|touch|taste|kiss|spread|fuck|bury|ride|grind)"
    r"\s+(?:on |it |that )?(?:u|you|ur|your|me|them|those)\b)|"  # soft verb + body object
    # Pronoun + penetration/act verb DIRECTLY (no desire verb): "id bury my face in your
    # pussy", "id slide inside you". Anchored to a body part downstream so it can't fire
    # on "id touch base with my boss".
    r"(?:i|id|i'?d|im|i'?m)\s+(?:bury|shove|slide|slip|stick|ram|pound|eat|lick|suck)"
    r"\b[^.!?]{0,30}?\b(?:pussy|ass|cock|dick|clit|tits?|mouth|throat|face|hole)\b|"
    # Bare ACT NOUNS with no innocent reading. `doggy` and `69` were here and are NOT
    # innocent-free: "my doggy is so cute", "i love 69 degree weather".
    r"\b(?:blowjob|bj|doggy ?style|deepthroat|handjob|titfuck|creampie)\b|"
    # Imperatives aimed at HER body — a request, not a compliment. Added eat/suck/lick/
    # kiss and `me` to the object set: "eat me out", "ride me babe", "suck my dick".
    r"\b(?:show|spread|touch|rub|play with|finger|ride|bounce|eat|suck|lick|kiss)\s+"
    r"(?:me |it |that )?(?:ur|your|that|them|those|me)\b)",
    re.IGNORECASE)


def is_escalation(text: str | None) -> bool:
    """True when the fan's latest message is sexual/physical ESCALATION — the
    closer-only hot-moment signal ESCALATION_RE detects. Empty/None → False."""
    return bool(text) and bool(ESCALATION_RE.search(norm_text(text)))


# ── THREAD HEAT — the signal that actually predicts a sale ───────────────────────
# Measured on prod (2026-07-14), last fan line before a 1:1 chatter's PPV:
#
#   signal                                        BOUGHT   UNPAID   lift
#   last-message regex (is_escalation/ask)         10.7%     9.0%   1.19x   ← useless
#   THREAD HEAT (below)                            14.6%     0.6%  24.32x   ← the signal
#
# The engine was asking "was his LAST LINE dirty?". That question barely beats a coin
# flip, because a man types one dirty line in threads that go nowhere all day long. The
# question that predicts money is "is this a LIVE SEXUAL CONVERSATION HE IS IN?" — he
# said something sexual recently AND he is actually replying, not being talked at.
#
# It matches what the top closers do: they don't sell to a dirty sentence, they build a
# scene, make him describe what he wants, and sell him THAT. A bought PPV lands after
# ~3.6 fan messages and 0.82 sexual lines from her; an unbought one after 1.0 and 0.14.
_HEAT_WINDOW = 6          # messages, both directions — the current exchange
_HEAT_MIN_HIS_MSGS = 2    # he is IN it, not being monologued at


def thread_heat(messages: "list[tuple[str, str]]", window: int = _HEAT_WINDOW) -> bool:
    """Is this thread a live sexual conversation the fan is participating in?

    `messages` is the recent history as (direction, text), oldest→newest — exactly what
    ai_chatter's _Cand.messages holds. Returns True on the moment worth selling into.
    """
    recent = list(messages)[-window:]
    his = [t for d, t in recent if d == "in"]
    if len(his) < _HEAT_MIN_HIS_MSGS:
        return False          # he isn't in the conversation — this is a monologue
    return any(is_escalation(t) or is_content_ask(t) for t in his)


# A fan who just put money down — a tip received, or a PPV unlock — is a HOT
# lead: for a SHORT window right after he pays, the SELLER (ai_chatter) owns the
# moment and never-sell keep-warm (autoreply) must NOT babysit him. After the
# window he's a normal fan again (Auto Convo may keep him warm). 1 hour = ride the
# buy moment, then let go. Read from the messages table (the WS source of truth),
# NOT the Transaction / lifetime rollup, which lags PPV unlocks and would let a
# just-paid fan slip past every spend gate.
RECENT_PAYER_HOURS = 1


async def recent_payer_fans(account_id: str, fan_ids,
                            within_hours: int = RECENT_PAYER_HOURS) -> set[int]:
    """Subset of `fan_ids` with a money event (inbound tip OR a PPV unlock) in the
    last `within_hours`. Tips land as inbound is_tip rows; PPV unlocks stamp
    purchased_at on the original message — both LEAD the spend rollup, so this
    catches a just-paid fan the lifetime/recent-spend gates would miss."""
    ids = [int(x) for x in fan_ids]
    if not ids:
        return set()
    cut = datetime.utcnow() - timedelta(hours=int(within_hours))
    async with get_session() as s:
        rows = (await s.execute(
            select(Message.fan_id).where(
                Message.account_id == str(account_id),
                Message.fan_id.in_(ids),
                Message.is_unsent.is_(False),
                or_(
                    and_(Message.is_tip.is_(True), Message.created_at >= cut),
                    and_(Message.purchased_at.isnot(None),
                         Message.purchased_at >= cut),
                ),
            )
        )).all()
    return {int(r[0]) for r in rows}


# The content-ask tip-ask ships ON by default (ask_enabled); an owner can flip it
# off per-account via tip_reward_config_json.ask_enabled. By design it names NO
# fixed dollar amount — she asks for a tip naturally — UNLESS an owner sets
# ask_amount_dollars (then she suggests that figure). `ask_template` (optional)
# seeds the phrasing in her voice. The whole tip loop lives in one config home.
DEFAULT_TIP_ASK_ENABLED = True
_TIP_ASK_TEMPLATE_MAX = 300


async def load_tip_ask_config(account_id: str) -> tuple[bool, int | None, str]:
    """(ask_enabled, suggested_tip_dollars_or_None, optional_template) for the
    content-ask tip-ask, read from account_ai_config.tip_reward_config_json (one
    home for the whole tip loop — the ask reads it independently of tip_reward's
    own `enabled` flag). The ask is ON by default and names NO specific dollar
    amount unless `ask_amount_dollars` is set. Absent/NULL/parse-error →
    (DEFAULT_TIP_ASK_ENABLED, None, '')."""
    async with get_session() as s:
        cfg = await s.get(AccountAiConfig, str(account_id))
    raw = getattr(cfg, "tip_reward_config_json", None) if cfg else None
    enabled, amount, template = DEFAULT_TIP_ASK_ENABLED, None, ""
    if raw:
        try:
            d = json.loads(raw) or {}
            if d.get("ask_enabled") is not None:
                enabled = bool(d["ask_enabled"])
            if d.get("ask_amount_dollars") is not None:
                amount = max(1, int(d["ask_amount_dollars"]))
            template = str(d.get("ask_template") or "").strip()[:_TIP_ASK_TEMPLATE_MAX]
        except (ValueError, TypeError):
            log.warning("bad tip_ask config account=%s", account_id, exc_info=True)
    return enabled, amount, template


def build_tip_ask_block(amount_dollars: int | None = None, template: str = "") -> str:
    """The system-prompt directive for the 'fan just asked to see content' branch:
    ask him to TIP for it in the creator's OWN voice — ONE short human line, teasing
    not needy, and NEVER the bare word "tip". Two natural ways to tip (the model
    picks whichever fits, it doesn't have to name both): a tip right here in chat,
    OR a tip under a feed post/pic he likes (a post tip lands in chat too, so it
    rewards the same way — and it tells the creator what he's into). When
    `amount_dollars` is set she SUGGESTS that figure; when None she asks for a tip
    WITHOUT naming a price (no static number). An optional `template` (with an
    optional {amount} placeholder) seeds the phrasing; the model still says it in
    voice. Shared by of_ai_chat + autoreply so it reads identically."""
    has_amt = amount_dollars is not None
    amt = max(1, int(amount_dollars)) if has_amt else 0
    chat_tip = (f"tip you ${amt} right here" if has_amt
                else "send you a lil tip right here")
    example = (f"\"tip me ${amt} n ill send u something 😏\"" if has_amt
               else "\"tip me n ill send u something 😏\"")
    block = (
        "HE JUST ASKED TO SEE CONTENT. This message is NOT a get-to-know question "
        "and NOT a brush-off — answer the ask. In your OWN voice, ONE short human "
        f"line, tease him a little and tell him to TIP for it — either {chat_tip} "
        "and you'll send him something, OR drop a tip under any post/pic of yours "
        "he likes (that shows you what he's into, and you'll spoil him back for "
        "it). Pick whichever way flows naturally — you don't have to name both. Be "
        "playful and a touch teasing, never needy, desperate, or pushy. NEVER write "
        "the bare word \"tip\" on its own — always a natural line, e.g. "
        f"{example} or \"drop a lil tip under a post u like n ill spoil u 😈\". "
        "Don't attach anything now and don't name a specific piece — just the "
        "teasing tip-ask (the content goes out once he tips)."
    )
    tmpl = (template or "").strip()
    if tmpl:
        tmpl = tmpl.replace("{amount}", str(amt) if has_amt else "")
        block += (f"\n\nSTART FROM THIS (rewrite it in your own voice, keep the "
                  f"tip-ask): {tmpl}")
    return block
