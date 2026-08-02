"""service/automations/autoreply.py — Automation: autoreply ("Auto Convo").

CONTINUE the chat when the team didn't reply fast enough. When a KNOWN, LOW-SPEND
fan MESSAGED and nobody (team or AI) answered within the window (default 5 min –
3 h), send ONE casual, **never-PPV** reply — tone-matched to the recent messages,
just keeping the conversation going. It does NOT gather info (we already know him)
and it does NOT sell. After the window it's too stale → send_followup's drip owns
it (26h+).

How it stays out of the other senders' way (timing, not trigger):
  • of_ai_chat / deep_convo answer their fans within SECONDS (webhook) — so by
    silence_min those fans aren't waiting. Auto Convo only catches fans no one
    covered in time (graduated / team-owned / AI not enabled for them).
  • send_followup re-engages much later (26h+).

Eligibility (ALL must hold; every knob is per-account in autoreply_config_json):
  1. enabled + not in quiet hours
  2. the FAN spoke last (an unanswered inbound) and the wait (now − his last
     inbound) is in [silence_min, silence_max] — the team didn't reply in time
  3. info-complete (≥75% bio bar — a fan we actually know)
  4. lifetime spend < max_lifetime_spend_cents  AND  spend in the last
     recent_spend_days < max_recent_spend_cents   (low spenders only)
  5. days since last purchase ≥ min_days_since_purchase (never-bought = ∞ → pass)
  6. days since first chat/subscribe ≥ min_days_since_first_chat (established)
  7. not blacklisted, not on automation_paused_until (a human is in the chat)
  8. not already covered this wait (max_nudges per his message, min_gap apart) —
     and re-checked at send time that he's STILL waiting (AutoreplyState).

Like nudge_online, this NEVER sets fans.automation_paused_until (it gates on its
own AutoreplyState), so of_ai_chat/deep_convo are never frozen by it. Self-
registers via @register("autoreply"); schedule with an automation_rules row
(kind="autoreply", trigger_json={"every_seconds": N}).
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

import automation_executor as ax
import llm_client
from ._persona import compose_persona
from . import _language
from . import _pins  # his own pinned long-form message (reader only)
from . import _voice  # whose voice this account writes in (NULL → 'her')
from attribution import write_outbound_attribution
from automation_registry import register
from db.engine import get_session
from db.models import (
    AccountAiConfig, AutoreplyState, Blacklist, Fan, Message, SkipList, Transaction,
)
from llm_client import LLMCapExceeded
from ._common import (
    BIO_CONSISTENCY_GUARDRAIL,
    # PAINFUL_TEXTING / LIVE_PROOF_GUARDRAIL are NOT imported here: both vary by
    # creator voice, so this engine reads them off `_voice.blocks(...)` instead.
    # `_common` still exports the female lane for the engines that don't.
    NO_NARRATION_RULE, ONPLATFORM_GUARDRAIL,
    STYLE_3LINE, STYLE_HUMANIZER, STYLE_MAX_BUBBLES,
    NONNATIVE_OUTPUTS, NONNATIVE_REGISTER, apply_nonnative_spacing, apply_nonnative_style, apply_word_restriction,
    build_tip_ask_block, hold_with_typing, apply_typo_throttle, is_content_ask,
    load_nonnative_flags, load_spacing_flags, load_voice_blocks,
    load_painful_texting_flag,
    load_strip_emojis, load_style_flags, load_tip_ask_config,
    load_typing_indicator, load_typing_wpm, load_typo_flags, load_hard_skip_ids,
    load_promo_spam_ids,
    recent_payer_fans, resolve_fan_name, resolve_model, should_skip_muted_creator,
    skip_unreachable_fan, typing_delay_seconds,
)
from automations._outbound import finalize_draft
from .of_ai_chat import (_is_info_complete, _strip_html,
                         split_for_bubbles, _dedupe_lead_reaction,
                         _clock_line, _load_clock_tz)

log = logging.getLogger("of-relay.automation.autoreply")

_PURPOSE = "autoreply"
_DEFAULT_LIMIT = 200          # candidate sweep ceiling
_DEFAULT_MAX_SENDS = 25       # per-run send cap
_TEMPERATURE = 0.95
_HISTORY_TAIL = 16            # last N messages handed to the model for tone
_MSG_CLIP = 300
_REPLY_MAX_CHARS = 600


def _defaults() -> dict:
    """Built-in OFF state + sensible knobs (the UI seeds from this)."""
    return {
        "enabled": False,
        "silence_min_minutes": 24,   # give the team this long before stepping in
        "silence_max_minutes": 1115,  # ~18.5h — past this the thread is too stale
        "max_nudges": 1,
        "min_gap_minutes": 5,        # ≥ this between replies to the same fan
        "max_lifetime_spend_cents": 2_000_000,  # < $20,000 lifetime (whales are the
                                                # closer's; the hot-lead skip owns them)
        "recent_spend_days": 1,
        "max_recent_spend_cents": 50_000,       # < $500 in the window
        "min_days_since_purchase": 0,           # a recent buyer still gets kept warm
        "min_days_since_first_chat": 0,         # brand-new fans included
        "last_n_messages": _HISTORY_TAIL,
        "quiet_hours_json": None,             # [start,end] creator-local; null=24/7
        # When True, don't require a complete profile — just respond from the last
        # few messages + whatever's already been gathered. Widens coverage to fans
        # we don't fully know yet.
        "info_not_required": True,
    }


async def _load_config(account_id: str) -> dict | None:
    """Per-account gate + knobs; returns the merged config when ENABLED, else None
    (absent/NULL/disabled/bad JSON → OFF, the safe default)."""
    async with get_session() as s:
        row = await s.get(AccountAiConfig, str(account_id))
    stored: dict = {}
    if row is not None and row.autoreply_config_json:
        try:
            stored = json.loads(row.autoreply_config_json) or {}
        except Exception:
            log.warning("bad_autoreply_config account=%s", account_id, exc_info=True)
            stored = {}
    cfg = {**_defaults(), **stored}
    if not cfg.get("enabled"):
        return None
    cfg["_utc_offset"] = int(getattr(row, "utc_offset", 0) or 0) if row else 0
    return cfg


def _in_quiet_hours(cfg: dict, now: datetime) -> bool:
    """True if the creator-local hour is inside quiet_hours_json=[start,end].
    null / [0,0] → never quiet (24/7). Mirrors the executor's gate."""
    qh = cfg.get("quiet_hours_json")
    if not qh or not isinstance(qh, (list, tuple)) or len(qh) != 2:
        return False
    start, end = int(qh[0]), int(qh[1])
    if start == end:
        return False
    hour = int((now + timedelta(hours=cfg.get("_utc_offset", 0))).hour)
    return start <= hour < end if start < end else (hour >= start or hour < end)


# ── Re-engagement style variety (controls vibe + shape, never sells) ──
_STYLE_VARIANTS = (
    "answer his last message directly, then a light tease.",
    "react to what he said and keep the banter going, casual.",
    "a short flirty reply, NO question.",
    "reply warm + playful, like you've been thinking about him.",
    "if the recent vibe was sexual, keep it going — one suggestive line (still NO pics/PPV).",
    ("two TINY texts — output as TWO LINES with a line break between (a quick "
     "reaction, then your line). 0-1 emoji."),
)


def _build_messages(persona: str, f: Fan, history: list[tuple[str, str]],
                    style: str, style_on: bool = False,
                    nonnative_on: bool = False,
                    content_ask: bool = False,
                    tip_ask_block: str = "",
                    painful_on: bool = True,
                    lang: str = "en",
                    clock: str = "",
                    v: "_voice.VoiceBlocks" = _voice.HER) -> list[dict]:
    facts = []
    nm = resolve_fan_name(f)
    if nm:
        facts.append(f"name: {nm.split('/')[0][:40]}")
    # `likes` is boobs/ass and nothing else — it exists to route a straight man to
    # the right body content out of a WOMAN's vault, and `gen_info` fills it
    # fleet-wide. On a male account it asserts, inside a facts block the model is
    # told never to contradict, a preference for a body the creator does not have.
    # Dropped for the male lane rather than re-mapped: what a submissive man is
    # into is a different axis entirely and `fetishes` already carries it as free
    # text. Resolved BEFORE the tuple so the lane check is one named decision
    # rather than a branch buried in a literal.
    likes = "" if v.is_male else ("boobs" if f.likes_boobs
                                 else ("ass" if f.likes_ass else ""))
    for label, val in (("age", f.his_age), ("city", f.home_city),
                       ("country", f.home_country), ("job", f.occupation),
                       ("hobbies", f.hobbies), ("recent", f.recent_events),
                       ("likes", likes)):
        if val:
            facts.append(f"{label}: {str(val).strip()[:80]}")
    facts_block = "\n".join(f"- {x}" for x in facts) if facts else "- (not much on file)"
    convo = "\n".join(f"{'FAN' if d == 'in' else 'YOU'}: {b}" for d, b in history if b)

    # He asked to SEE content right now: this is the one case Auto Convo answers
    # with a SALES line instead of pure keep-warm banter — a natural "tip me $X"
    # ask (the tip_reward automation delivers once he tips), NEVER the bare word
    # "tip" (the bug this fixes). Otherwise the usual never-sell rules apply.
    selling = content_ask and bool(tip_ask_block)
    directive = tip_ask_block if selling else f"THIS MESSAGE — {style}"
    if selling:
        hard_rules = (
            "HARD RULES:\n"
            "- You already KNOW him — do NOT re-ask his name, age, location, job or "
            "hobbies. Use what you know to make it personal.\n"
            "- Don't apologize for being slow or mention the delay — just reply "
            "naturally, warm and easy, never needy.\n"
            "- SHORT and human: lowercase, contractions, u/ur/ya, 0-1 emoji, vary "
            "your wording. No paragraphs.\n"
            f"{NO_NARRATION_RULE}"
        )
    else:
        # ⚠️ THE NEVER-SELL RULE AND THE CUSTOMS CARVE-OUT ARE THE SAME PROMPT.
        # `v.live_proof` below says "ONE THING YOU DO OFFER: a paid CUSTOM"; this
        # rule said "NEVER offer, mention, or hint at PPV, paid content, pics, or
        # videos". Both rendered, ~30 lines apart, and which one the model obeyed
        # was a coin flip per generation — so week one produced either no customs
        # at all (and the wrong conclusion that customs don't sell) or a violation
        # of the lane's defining rule, with no way to tell from outside which.
        #
        # The ban stays absolute for everything Auto Convo has never sold. It now
        # names the single exception the account has explicitly opted into, so the
        # two rules describe one policy instead of contradicting each other. With
        # `sell_customs` off this is byte-identical to what has always shipped.
        _never_sell = (
            "- NEVER offer, mention, or hint at PPV, paid content, pics, or videos. "
            "This is purely keeping the conversation going.\n"
            if not v.sell_customs else
            "- NEVER offer, mention, or hint at PPV, pics, or videos — this is "
            "keeping the conversation going, not selling. The ONE exception is a "
            "custom voice note (see the LIVE PROOF rule below): you may mention "
            "that if it comes up naturally. Nothing else, ever.\n"
        )
        hard_rules = (
            "HARD RULES:\n"
            f"{_never_sell}"
            "- You already KNOW him — do NOT re-ask his name, age, location, job or "
            "hobbies. Use what you know to make it personal.\n"
            "- Don't apologize for being slow or mention the delay — just reply "
            "naturally, warm and easy, never needy.\n"
            "- Match the vibe of the recent messages: if it was getting sexual, keep "
            "it flirty/suggestive; if it was casual, stay casual.\n"
            "- SHORT and human: lowercase, contractions, u/ur/ya, 0-1 emoji, vary "
            "your wording — never reuse a line or emoji you've already used here. "
            "No paragraphs.\n"
            f"{NO_NARRATION_RULE}"
        )

    # The prompt clock ("" when the account has no tz configured → byte-equal
    # prompt) — same block as of_ai_chat. A model with no clock invents one.
    clock_block = (
        f"RIGHT NOW for you it is {clock}. If the time, your day, or what "
        "you're doing comes up, stay consistent with this clock — never claim "
        "a different time of day.\n\n" if clock else "")
    system = (
        f"{persona}\n"
        "A fan you've already been chatting with MESSAGED YOU and is waiting for a "
        "reply — keep the conversation going naturally. Reply to his last message "
        "like you just got back to your phone. You are 100% human, never a bot.\n\n"
        f"{v.painful_texting + chr(10) + chr(10) if painful_on else ''}"
        f"{clock_block}"
        f"{directive}\n\n"
        f"{hard_rules}\n"
        f"{ONPLATFORM_GUARDRAIL}\n\n"
        f"{v.live_proof}\n\n"
        f"{BIO_CONSISTENCY_GUARDRAIL}\n\n"
        f"{STYLE_HUMANIZER + chr(10) + chr(10) if style_on else ''}"
        f"{NONNATIVE_REGISTER + chr(10) + chr(10) if nonnative_on else ''}"
        "Your reply is ONLY the message text — no JSON, quotes, or metadata."
        f"{_language.output_language_directive(lang)}"
    )
    # His own long-form message, pinned on the thread and read back here (_pins).
    user = (
        f"What you know about him:\n{facts_block}\n\n"
        f"{_pins.pins_block(f)}"
        f"Recent conversation (oldest→newest):\n{convo}\n\n"
        "Reply to his last message now, in THIS MESSAGE's style."
    )
    return [{"role": "system", "content": system},
            {"role": "user", "content": user}]


async def _candidates(account_id: str, cfg: dict, now: datetime) -> list[tuple[Fan, datetime]]:
    """[(fan, his_last_inbound_at)] where the FAN spoke last and is STILL WAITING
    on a reply — nobody (team or AI) answered within [silence_min, silence_max].
    That's the "cover a slow team" trigger: give the team `silence_min` to reply,
    and stop after `silence_max` (too stale → send_followup's drip owns it).

    Read from the MESSAGES table (source of truth — Fan.last_message_* lag scrape).
    of_ai_chat/deep_convo answer their fans within seconds, so by silence_min those
    aren't waiting; Auto Convo only catches fans no one covered in time. Also gated
    on info-complete + low spend + established + not blacklisted/human-handled."""
    sil_min = int(cfg["silence_min_minutes"])
    sil_max = int(cfg["silence_max_minutes"])
    horizon = now - timedelta(minutes=sil_max)   # ignore chats quiet longer than max
    first_chat_cut = now - timedelta(days=int(cfg["min_days_since_first_chat"]))

    async with get_session() as s:
        bl = {int(r[0]) for r in (await s.execute(select(Blacklist.fan_id))).all()}
        # The LATEST message per fan within the silence-max horizon (most-recent
        # first; first row seen per fan_id is its latest).
        rows = (await s.execute(
            select(Message.fan_id, Message.direction, Message.created_at)
            .where(Message.account_id == str(account_id),
                   Message.is_unsent.is_(False),
                   Message.created_at >= horizon)
            .order_by(Message.fan_id, Message.created_at.desc(), Message.message_id.desc())
        )).all()
    last: dict[int, tuple[str, datetime]] = {}
    for fid, direction, ts in rows:
        fid = int(fid)
        if fid not in last:
            last[fid] = (direction, ts)
    # Fans where the FAN spoke last (an unanswered inbound) and the wait is in
    # [min, max] — i.e. the team hasn't replied fast enough.
    waiting: dict[int, datetime] = {}
    for fid, (direction, ts) in last.items():
        if direction != "in" or ts is None or fid in bl:
            continue
        wait_min = (now - ts).total_seconds() / 60.0
        if sil_min <= wait_min <= sil_max:
            waiting[fid] = ts
    if not waiting:
        return []

    async with get_session() as s:
        frows = (await s.execute(
            select(Fan).where(
                Fan.account_id == str(account_id),
                Fan.fan_id.in_(list(waiting.keys())),
                Fan.lifetime_spend_cents < int(cfg["max_lifetime_spend_cents"]),
            )
        )).scalars().all()
    out: list[tuple[Fan, datetime]] = []
    for f in frows:
        if f.automation_paused_until and f.automation_paused_until > now:
            continue  # a human is in the chat → stand down
        started = f.subscribed_at or f.created_at
        if started and started > first_chat_cut:
            continue  # established relationship only
        if not cfg.get("info_not_required") and not _is_info_complete(f):
            continue  # known fan only (unless "info not needed" mode is on)
        out.append((f, waiting[int(f.fan_id)]))
    return out


async def _spend_and_last_purchase(account_id: str, fan_ids: list[int],
                                   window_start: datetime) -> dict[int, tuple[int, datetime | None]]:
    """{fan_id: (recent_spend_cents, last_purchase_at)} for the candidate set."""
    out: dict[int, tuple[int, datetime | None]] = {fid: (0, None) for fid in fan_ids}
    if not fan_ids:
        return out
    async with get_session() as s:
        rows = (await s.execute(
            select(Transaction.fan_id, Transaction.amount_cents, Transaction.occurred_at)
            .where(Transaction.account_id == str(account_id),
                   Transaction.fan_id.in_([int(x) for x in fan_ids]))
        )).all()
    for fid, amt, when in rows:
        fid = int(fid)
        recent, last = out.get(fid, (0, None))
        if when is not None and (last is None or when > last):
            last = when
        if when is not None and when >= window_start:
            recent += int(amt or 0)
        out[fid] = (recent, last)
    return out


async def _load_state(account_id: str, fan_id: int) -> AutoreplyState | None:
    async with get_session() as s:
        return await s.get(AutoreplyState, (str(account_id), int(fan_id)))


async def _save_state(account_id: str, fan_id: int, *, spell_inbound_at,
                      nudges_sent: int, last_nudge_at) -> None:
    now = datetime.utcnow()
    vals = dict(account_id=str(account_id), fan_id=int(fan_id),
                spell_inbound_at=spell_inbound_at, nudges_sent=int(nudges_sent),
                last_nudge_at=last_nudge_at, updated_at=now)
    async with get_session() as s:
        await s.execute(
            sqlite_insert(AutoreplyState).values(**vals)
            .on_conflict_do_update(index_elements=["account_id", "fan_id"], set_={
                "spell_inbound_at": spell_inbound_at, "nudges_sent": int(nudges_sent),
                "last_nudge_at": last_nudge_at, "updated_at": now})
        )


async def _history(account_id: str, fan_id: int, tail: int) -> list[tuple[str, str]]:
    async with get_session() as s:
        rows = (await s.execute(
            select(Message.direction, Message.body)
            .where(Message.account_id == str(account_id), Message.fan_id == int(fan_id),
                   Message.is_unsent.is_(False))
            .order_by(Message.created_at.desc(), Message.message_id.desc())
            .limit(tail)
        )).all()
    return [(d, _strip_html(b)[:_MSG_CLIP]) for d, b in reversed(rows)]


async def _fan_still_waiting(account_id: str, fan_id: int, inbound_at) -> bool:
    """Re-validate just before sending: the fan must STILL be waiting — no reply
    (outbound) landed since his last inbound. If the team or of_ai_chat answered
    in the meantime, stand down (don't double-message). Reads the messages table
    (the truth), not Fan.last_message_sent_at (which lags scrape)."""
    async with get_session() as s:
        row = (await s.execute(
            select(Message.message_id).where(
                Message.account_id == str(account_id), Message.fan_id == int(fan_id),
                Message.direction == "out", Message.is_unsent.is_(False),
                Message.created_at > inbound_at).limit(1)
        )).first()
    return row is None


async def _load_unreachable_ids(account_id: str) -> set[int]:
    """Fans skip-listed 'unreachable' (deleted/blocked — every send 404s and burns
    an LLM call). autoreply otherwise ignores skip_list, but this one reason must
    be honored: the 7d undeliverable pause eventually expires and, without this,
    a widened autoreply would re-generate + re-fail on a dead fan."""
    async with get_session() as s:
        rows = (await s.execute(
            select(SkipList.fan_id).where(SkipList.account_id == str(account_id),
                                          SkipList.reason == "unreachable")
        )).all()
    return {int(r[0]) for r in rows}


@register("autoreply")
async def run(account_id: str, payload: dict, *, run_id: int) -> dict:
    payload = payload or {}
    cfg = await _load_config(account_id)
    if cfg is None:
        return {"enabled": False, "candidates": 0, "sent": 0}
    now = datetime.utcnow()
    if _in_quiet_hours(cfg, now):
        return {"enabled": True, "quiet_hours": True, "candidates": 0, "sent": 0}

    model = await resolve_model(account_id, _PURPOSE, payload.get("model"))
    typing_wpm = await load_typing_wpm(account_id)
    typing_indicator = await load_typing_indicator(account_id)  # live "...is typing"
    style_on = (await load_style_flags(account_id))[_PURPOSE]  # human-style opt-in
    account_lang = await _language.load_account_language(account_id)  # output language + guards
    # Whose voice this account writes in AND whether it may sell customs, as one
    # resolved bundle off ONE row read. Built once per run beside the language for
    # the same reason: both are account-constant and cannot change mid-sweep.
    # NULL → 'her' → every string below renders exactly as it always has.
    voice_blocks = await load_voice_blocks(account_id)
    typo_on = (await load_typo_flags(account_id))[_PURPOSE]    # thumb-typo opt-in
    nonnative_on = (await load_nonnative_flags(account_id))[_PURPOSE]  # non-native opt-in
    # Space-before-"?" — its own tri-state key, read independently but only
    # ever APPLIED inside the non-native block below, so it can narrow that
    # register and never widen it.
    spacing_on = (await load_spacing_flags(account_id))[_PURPOSE]
    painful_on = await load_painful_texting_flag(account_id)  # brevity/emotion framing (default ON)
    strip_emoji_on = await load_strip_emojis(account_id)  # account-wide emoji strip
    max_bubbles = STYLE_MAX_BUBBLES if style_on else 2
    style_pool = _STYLE_VARIANTS + ((STYLE_3LINE,) if style_on else ())
    dry_run = bool(payload.get("dry_run"))
    only = payload.get("only_fan_ids") or payload.get("test_fan")
    only_set = ({int(only)} if isinstance(only, int)
                else {int(x) for x in only} if only else None)
    limit = int(payload.get("limit") or _DEFAULT_LIMIT)
    max_sends = int(payload.get("max_sends") or _DEFAULT_MAX_SENDS)
    tail = int(cfg.get("last_n_messages") or _HISTORY_TAIL)
    max_nudges = int(cfg["max_nudges"])
    # Minimum time between Auto Convo replies to the SAME fan — never faster than
    # min_gap, and never faster than silence_min (so a fan who fires back can't
    # pull instant replies; he waits the same minimum again).
    reply_gap = timedelta(minutes=max(int(cfg["min_gap_minutes"]),
                                      int(cfg["silence_min_minutes"])))

    cands = await _candidates(account_id, cfg, now)  # [(fan, our_last_out_at)]
    if only_set is not None:
        cands = [(f, t) for (f, t) in cands if int(f.fan_id) in only_set]
    cands = cands[:limit]

    # autoreply gates on automation_paused_until, not skip_list — so load the
    # HARD skips (muted_creator / manual "restrict this fan") explicitly and skip
    # them here too, so a restricted fan is silenced on EVERY automation.
    hard_skip = await load_hard_skip_ids(account_id)
    promo_spam = await load_promo_spam_ids(account_id)
    # The info-complete gate used to ACCIDENTALLY shield no-info / promo /
    # unreachable fans from Auto Convo. With info_not_required widening coverage,
    # those exclusions must be explicit — mirror what every other chat sender does
    # so a widened autoreply never texts a dead fan or peer-creator promo spam.
    # (Mid-funnel fans are NOT excluded here by design — the funnel replies fast,
    # so a fan is never left waiting long enough mid-funnel for Auto Convo to step
    # in; if the funnel stalls, a keep-warm line is preferable to silence.)
    unreachable = await _load_unreachable_ids(account_id)
    cand_ids = [int(f.fan_id) for (f, _) in cands]
    # Hot leads (tip/PPV unlock within RECENT_PAYER_HOURS) belong to the CLOSER,
    # not never-sell Auto Convo — skip them here; ai_chatter owns/sells the moment.
    # Both sides call recent_payer_fans with the SAME (default) window, so the
    # skip-set and the closer's own-set can never diverge.
    hot_leads = await recent_payer_fans(account_id, cand_ids)

    # Spend-window + last-purchase gates (one transactions query for the set).
    win_start = now - timedelta(days=int(cfg["recent_spend_days"]))
    purchase_cut = now - timedelta(days=int(cfg["min_days_since_purchase"]))
    spend = await _spend_and_last_purchase(account_id, cand_ids, win_start)

    persona = await _load_persona(account_id)
    clock_tz = await _load_clock_tz(account_id)  # None ⇒ no clock line in the prompt
    # Content-ask tip-ask: when a fan asks to SEE content, Auto Convo answers with a
    # natural "tip me $X" line instead of pure keep-warm banter (tip_reward delivers
    # once he tips), NEVER the bare word "tip". Account-level → build once.
    tip_ask_enabled, tip_amount, tip_template = await load_tip_ask_config(account_id)
    # Off (per-account toggle) → empty block; the content-ask just gets keep-warm
    # banter, no tip-ask.
    tip_ask_block = (build_tip_ask_block(tip_amount, tip_template,
                                         voice_blocks.sell_customs)
                     if tip_ask_enabled else "")
    # Double-pitch guard: when ai_chatter (the closer) owns this account, IT handles
    # selling — Auto Convo stays purely keep-warm even on a content-ask.
    ai_chatter_owns = False
    try:
        from .ai_chatter import is_enabled as _ai_chatter_enabled
        ai_chatter_owns = await _ai_chatter_enabled(account_id)
    except Exception:
        log.debug("autoreply ai_chatter gate check failed", exc_info=True)
    client = None
    sent = skipped_spend = skipped_cap = skipped_raced = errors = 0
    skipped_restricted = 0   # muted creator / manual "restrict from automations"
    skipped_unreachable = 0  # skip_list 'unreachable' (dead/blocked → send 404s)
    skipped_spam = 0         # peer-creator promo: creator_we_follow + $0 + blasted
    skipped_hot_lead = 0     # tipped/unlocked recently → closer's moment
    skipped_cooldown = 0     # a sibling sender paused him this tick
    skipped_locked = 0       # another sender holds the fan-lease

    for f, inbound_at in cands:
        if sent >= max_sends:
            break
        fid = int(f.fan_id)
        # Durably restricted (muted peer-creator, or hand-restricted) → never reply.
        if fid in hard_skip or should_skip_muted_creator(f):
            skipped_restricted += 1
            continue
        # Dead/blocked fan — sending 404s and burns an LLM call.
        if fid in unreachable:
            skipped_unreachable += 1
            continue
        # Peer-creator promo spam — the jaka problem. Needs real promo evidence,
        # not just OF's creator_we_follow flag (see load_promo_spam_ids).
        if fid in promo_spam:
            skipped_spam += 1
            continue
        # Just-paid hot lead → the closer (ai_chatter) owns the moment, not us.
        if fid in hot_leads:
            skipped_hot_lead += 1
            continue
        recent_spend, last_purchase = spend.get(fid, (0, None))
        # recent spend gate
        if recent_spend >= int(cfg["max_recent_spend_cents"]):
            skipped_spend += 1
            continue
        # days since last purchase ≥ min (never bought → passes)
        if last_purchase is not None and last_purchase > purchase_cut:
            skipped_spend += 1
            continue

        st = await _load_state(account_id, fid)
        # Min cadence: never reply to this fan again within `reply_gap`, even if
        # he re-messages fast (applies ACROSS messages, not just the same one).
        if st is not None and st.last_nudge_at and (now - st.last_nudge_at) < reply_gap:
            skipped_cap += 1
            continue
        # Per-message dedup: don't cover the SAME waiting message twice (a new fan
        # message → new anchor → fresh spell, so it can be covered after the gap).
        spell_anchor = inbound_at
        nudges = int(st.nudges_sent or 0) if (st is not None and st.spell_inbound_at == spell_anchor) else 0
        if nudges >= max_nudges:
            skipped_cap += 1
            continue

        # Re-validate right before sending: he must STILL be waiting (no reply
        # landed since his message — else the team/of_ai_chat got it first).
        if not await _fan_still_waiting(account_id, fid, inbound_at):
            skipped_raced += 1
            continue

        history = await _history(account_id, fid, tail)
        style = random.choice(style_pool)
        # He spoke last (the trigger) → his ask is the latest inbound. Tip-ask only
        # when the closer doesn't own the account (double-pitch guard above).
        last_in = next((b for d, b in reversed(history) if d == "in"), "")
        # Per-fan language: fans.language (manual pin or gen_info detection) overrides
        # the account default; unset → account default.
        fan_lang = _language.resolve_language(account_lang, getattr(f, "language", None))
        content_ask = (not ai_chatter_owns) and _language.is_content_ask(last_in, fan_lang)
        msgs = _build_messages(persona, f, history, style, style_on=style_on,
                               nonnative_on=nonnative_on, lang=fan_lang,
                               content_ask=content_ask, tip_ask_block=tip_ask_block,
                               painful_on=painful_on, clock=_clock_line(clock_tz),
                               v=voice_blocks)
        try:
            res = await llm_client.chat(model=model, messages=msgs, purpose=_PURPOSE,
                                        account_id=account_id, fan_id=fid,
                                        temperature=_TEMPERATURE)
        except LLMCapExceeded:
            log.warning("autoreply LLM cap reached account=%s — stopping", account_id)
            break
        except Exception:
            errors += 1
            log.warning("autoreply generate failed account=%s fan=%s", account_id, fid,
                        exc_info=True)
            continue

        raw = (res.content or "").strip()
        # Deterministic floor under ONPLATFORM_GUARDRAIL: if the model still leaked
        # a number / off-platform handle / meetup arrangement, swap for a deflection.
        # The shared send chokepoint (_outbound): off-platform guard, then the
        # account-wide emoji strip, before the split. No PHASE 2 consistency check
        # — autoreply does not implement it, which is why there is no
        # `consistency_autoreply` flag to switch on (see _common.CONSISTENCY_AUTOMATIONS).
        raw, _leak = await finalize_draft(
            raw, account_id=account_id, fan_id=fid, purpose=_PURPOSE,
            strip_emoji=strip_emoji_on, v=voice_blocks)
        parts = [apply_word_restriction(p)[:_REPLY_MAX_CHARS]
                 for p in split_for_bubbles(raw, max_bubbles,
                                            rng=random.Random(f"split:{f.fan_id}:{raw}"))
                 if p.strip()][:max_bubbles]
        # Don't open with the same reaction word we just used ('oof' every send).
        if style_on and parts:
            parts = _dedupe_lead_reaction(parts, [b for d, b in history if d == "out"])
        if not parts:
            errors += 1
            continue
        name_protect = [n for n in (f.real_name, f.generated_nickname,
                                    f.of_display_name) if n]
        if nonnative_on:  # opt-in: deterministic non-native misspellings (always)
            # ONE rng for the whole reply — see apply_nonnative_spacing.
            _q_rng = random.Random(f"{f.fan_id}:{raw}:q")
            parts = [apply_nonnative_style(p, protect=name_protect)
                     for p in parts]
            if spacing_on:
                parts = [apply_nonnative_spacing(p, _q_rng) for p in parts]
        if typo_on:  # opt-in: a realistic thumb-slip (+ a throttled "*fix" bubble)
            protect = name_protect + (list(NONNATIVE_OUTPUTS) if nonnative_on else [])
            parts = await apply_typo_throttle(
                account_id, f.fan_id, parts, random.Random(f"{f.fan_id}:{raw}"),
                protect=protect, max_bubbles=max_bubbles)
        if dry_run:
            sent += 1
            continue

        # Just before sending: a fresh cooldown read (catches a fan a sibling
        # paused EARLIER THIS TICK, after our candidate snapshot), then the
        # DB-atomic fan-lease so two senders can't both message him this tick.
        if await ax.fan_on_cooldown(account_id, fid):
            skipped_cooldown += 1
            continue
        if not await ax.acquire_fan_lease(account_id, fid, _PURPOSE):
            skipped_locked += 1
            continue

        # Everything from here holds the lease — a try/finally guarantees it's
        # released on every non-send path (like of_ai_chat/ai_chatter/deep_convo)
        # so a future raise can never leak it for the full TTL. autoreply NEVER
        # sets a cooldown (it must not freeze the other senders): on a confirmed
        # send it KEEPS the lease to expire by TTL (no sibling double-messages
        # right after us), on anything else it RELEASES for a faster retry.
        sent_ok = False
        try:
            # Re-check AFTER acquiring: the LLM call above can take longer than a
            # sibling's short (10-15s) cooldown, so someone may have answered him
            # while we generated. If he's no longer waiting, stand down.
            if not await _fan_still_waiting(account_id, fid, inbound_at):
                skipped_raced += 1
                continue

            if client is None:
                try:
                    client = await asyncio.to_thread(ax._make_client, account_id)
                except Exception:
                    log.warning("autoreply client init failed account=%s", account_id,
                                exc_info=True)
                    break

            for idx, part in enumerate(parts):
                await hold_with_typing(account_id, fid,
                                       typing_delay_seconds(part, typing_wpm),
                                       typing_indicator=typing_indicator)  # "typing"
                try:
                    result = await asyncio.to_thread(client.send_message, fid, part)
                except Exception as e:
                    errors += 1
                    # Permanent (deleted/blocked) → quarantine, so the next tick's
                    # paused-gate drops the fan BEFORE the LLM call. Transients retry.
                    await skip_unreachable_fan(account_id, fid, e, log=log)
                    log.warning("autoreply send failed account=%s fan=%s", account_id, fid,
                                exc_info=True)
                    break
                mid = result.get("id") if isinstance(result, dict) else None
                if mid:
                    await write_outbound_attribution(
                        account_id=account_id, fan_id=fid, message_id=int(mid),
                        sent_by_employee_id=None, automation_kind=_PURPOSE,  # autoreply
                        body=str(result.get("text") or part),
                        price_cents=0,
                        created_at=ax._parse_iso(result.get("createdAt")) or datetime.utcnow(),
                        emit_live=True,
                    )
                    sent_ok = True
            if sent_ok:
                await _save_state(account_id, fid, spell_inbound_at=spell_anchor,
                                  nudges_sent=nudges + 1, last_nudge_at=datetime.utcnow())
                sent += 1
        finally:
            if not sent_ok:
                await ax.release_fan_lease(account_id, fid)

    return {"enabled": True, "candidates": len(cands), "sent": sent,
            "skipped_spend": skipped_spend, "skipped_cap": skipped_cap,
            "skipped_raced": skipped_raced, "skipped_restricted": skipped_restricted,
            "skipped_unreachable": skipped_unreachable, "skipped_spam": skipped_spam,
            "skipped_hot_lead": skipped_hot_lead, "skipped_cooldown": skipped_cooldown,
            "skipped_locked": skipped_locked,
            "errors": errors, "dry_run": dry_run, "model": model}


async def _load_persona(account_id: str) -> str:
    async with get_session() as s:
        cfg = await s.get(AccountAiConfig, str(account_id))
    return compose_persona(cfg, fallback=(
        "You are a warm, flirty OnlyFans creator texting a fan you've been "
        "chatting with."))
