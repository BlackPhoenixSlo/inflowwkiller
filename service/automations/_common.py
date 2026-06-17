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
from db.models import AccountAiConfig, Fan, SkipList
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
# Senders that already gate on FULL skip_list membership (of_ai_chat,
# send_followup, deep_convo[≠info], ai_chatter[≠graduation]) honour both for
# free; the senders that DON'T (autoreply, tip_reward, send_welcome) and the
# list-broadcasts (mass_nudge, online_blast) load `load_hard_skip_ids` and
# exclude these explicitly.
MUTED_CREATOR_REASON = "muted_creator"
MANUAL_RESTRICT_REASON = "manual_restrict"
HARD_SKIP_REASONS = frozenset({MUTED_CREATOR_REASON, MANUAL_RESTRICT_REASON})


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
    reason such as 'info', which deep_convo would otherwise let through)."""
    now = now or datetime.utcnow()
    async with get_session() as s:
        await s.execute(
            sqlite_insert(SkipList)
            .values(account_id=str(account_id), fan_id=int(fan_id),
                    reason=MUTED_CREATOR_REASON, added_at=now)
            .on_conflict_do_update(
                index_elements=["account_id", "fan_id"],
                set_={"reason": MUTED_CREATOR_REASON, "added_at": now},
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


async def load_style_flags(account_id: str) -> dict[str, bool]:
    """Read account_ai_config.style_config_json → {automation: bool}. Absent/NULL
    or any parse error → all-OFF (the safe default: current behavior unchanged)."""
    off = {k: False for k in STYLE_AUTOMATIONS}
    async with get_session() as s:
        cfg = await s.get(AccountAiConfig, str(account_id))
    raw = getattr(cfg, "style_config_json", None) if cfg else None
    if not raw:
        return off
    try:
        stored = json.loads(raw) or {}
    except Exception:
        return off
    return {k: bool(stored.get(k)) for k in STYLE_AUTOMATIONS}


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
    keys). Absent/NULL/parse-error → all-OFF (current behavior unchanged)."""
    off = {k: False for k in STYLE_AUTOMATIONS}
    async with get_session() as s:
        cfg = await s.get(AccountAiConfig, str(account_id))
    raw = getattr(cfg, "style_config_json", None) if cfg else None
    if not raw:
        return off
    try:
        stored = json.loads(raw) or {}
    except Exception:
        return off
    return {k: bool(stored.get(typo_flag_key(k))) for k in STYLE_AUTOMATIONS}


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
    non-native layer (the 'nonnative_<automation>' keys). Absent/NULL/parse-error →
    all-OFF (current behavior unchanged byte-for-byte)."""
    off = {k: False for k in STYLE_AUTOMATIONS}
    async with get_session() as s:
        cfg = await s.get(AccountAiConfig, str(account_id))
    raw = getattr(cfg, "style_config_json", None) if cfg else None
    if not raw:
        return off
    try:
        stored = json.loads(raw) or {}
    except Exception:
        return off
    return {k: bool(stored.get(nonnative_flag_key(k))) for k in STYLE_AUTOMATIONS}


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


def humanize_typos(parts: list[str], rng, *, protect=(),
                   max_bubbles: int = STYLE_MAX_BUBBLES) -> list[str]:
    """Inject at most one thumb-typo across `parts` (the bubble list), rate ~1 per
    5 sentences, and sometimes append a '*fix' correction bubble. Pure: takes an
    explicit `rng` (random.Random) and returns a NEW list — caller seeds it off
    (fan_id, text) for reproducibility. Empty/typo-free input returns a copy."""
    parts = [p for p in parts if p and p.strip()]
    if not parts:
        return parts
    protect_set = {w.lower() for name in protect for w in _WORD_RE.findall(str(name))}

    # sentences across the whole reply → P(one typo) = min(1, n * rate)
    n_sent = sum(max(1, len(_SENT_RE.findall(p))) for p in parts)
    if rng.random() >= min(1.0, n_sent * _TYPO_SENTENCE_RATE):
        return list(parts)

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
        return list(parts)

    bi, start, end, word = rng.choice(cands)
    slipped = _mutate_word(word, rng)
    if slipped is None:
        return list(parts)

    out = list(parts)
    out[bi] = out[bi][:start] + slipped + out[bi][end:]

    # self-correct: a beat later, a "*fix" bubble (the strongest not-a-bot tell).
    # Fires whenever there's ROOM under the bubble cap — a 1-bubble reply becomes 2,
    # a 2-bubble reply becomes 3 — but NEVER a stray 4th: if the reply is already at
    # max_bubbles, the "*word" would be a bubble too far (out of place), so we skip.
    # Chance is severity-weighted (ugly garbles get fixed more) and the SHAPE varies
    # so the literal "*word" isn't itself a tell.
    fix_p = _TYPO_FIX_P_UGLY if _slip_is_ugly(word, slipped) else _TYPO_FIX_P_BASE
    if len(out) < max_bubbles and rng.random() < fix_p:
        out.append(_correction_bubble(word, rng))
    return out


# ── Human "typing speed" delay ───────────────────────────────────────
# Each bubble is held back for the time a real person would take to TYPE it, so
# replies don't pop instantly (and a 2-bubble reply has a believable gap). Speed
# is words-per-minute, configured per-account in webhook_config_json.typing_wpm
# (the "⚡ Instant reply" tab); default 60 wpm. Clamped so a long line can't hang.
_DEFAULT_TYPING_WPM = 60.0
_MAX_TYPING_DELAY_S = 60.0  # a single bubble never waits more than 1 min to "type"


def typing_delay_seconds(text: str, wpm: float) -> float:
    """How long it'd take to type `text` at `wpm` words/min (0 wpm → no delay)."""
    if not wpm or wpm <= 0:
        return 0.0
    words = len((text or "").split())
    return min(words / float(wpm) * 60.0, _MAX_TYPING_DELAY_S)


async def load_typing_wpm(account_id: str) -> float:
    """Per-account typing speed from webhook_config_json.typing_wpm (default 60).
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
    w = re.sub(r"[^A-Za-z]", "", raw)
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
    r"|see (some |the |ur |your |more |any )?"
    r"(content|pics?|photos?|vids?|videos?|nudes?|something))",
    re.IGNORECASE)


def is_content_ask(text: str | None) -> bool:
    """True when the fan's message is explicitly asking to SEE content (the buying
    signal CONTENT_ASK_RE detects). Empty/None → False."""
    return bool(text) and bool(CONTENT_ASK_RE.search(text))


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
