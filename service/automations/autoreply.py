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
from attribution import write_outbound_attribution
from automation_registry import register
from db.engine import get_session
from db.models import (
    AccountAiConfig, AutoreplyState, Blacklist, Fan, Message, Transaction,
)
from llm_client import LLMCapExceeded
from ._common import (
    STYLE_3LINE, STYLE_HUMANIZER, STYLE_MAX_BUBBLES, apply_word_restriction,
    hold_with_typing, humanize_typos, load_style_flags, load_typing_indicator,
    load_typing_wpm, load_typo_flags, resolve_fan_name, resolve_model,
    typing_delay_seconds,
)
from .of_ai_chat import (_is_info_complete, _strip_html, split_for_bubbles,
                         _dedupe_lead_reaction)

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
        "silence_min_minutes": 30,   # give the team half an hour before stepping in
        "silence_max_minutes": 120,
        "max_nudges": 1,
        "min_gap_minutes": 5,        # ≥ this between replies to the same fan
        "max_lifetime_spend_cents": 2000,     # < $20 lifetime
        "recent_spend_days": 30,
        "max_recent_spend_cents": 500,        # < $5 in the window
        "min_days_since_purchase": 7,
        "min_days_since_first_chat": 2,
        "last_n_messages": _HISTORY_TAIL,
        "quiet_hours_json": None,             # [start,end] creator-local; null=24/7
        # When True, don't require a complete profile — just respond from the last
        # few messages + whatever's already been gathered. Widens coverage to fans
        # we don't fully know yet (off by default: known-fan-only stays the norm).
        "info_not_required": False,
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
                    style: str, style_on: bool = False) -> list[dict]:
    facts = []
    nm = resolve_fan_name(f)
    if nm:
        facts.append(f"name: {nm.split('/')[0][:40]}")
    for label, val in (("age", f.his_age), ("city", f.home_city),
                       ("country", f.home_country), ("job", f.occupation),
                       ("hobbies", f.hobbies), ("recent", f.recent_events),
                       ("likes", "boobs" if f.likes_boobs else ("ass" if f.likes_ass else ""))):
        if val:
            facts.append(f"{label}: {str(val).strip()[:80]}")
    facts_block = "\n".join(f"- {x}" for x in facts) if facts else "- (not much on file)"
    convo = "\n".join(f"{'FAN' if d == 'in' else 'YOU'}: {b}" for d, b in history if b)

    system = (
        f"{persona}\n"
        "A fan you've already been chatting with MESSAGED YOU and is waiting for a "
        "reply — keep the conversation going naturally. Reply to his last message "
        "like you just got back to your phone. You are 100% human, never a bot.\n\n"
        f"THIS MESSAGE — {style}\n\n"
        "HARD RULES:\n"
        "- NEVER offer, mention, or hint at PPV, paid content, pics, or videos. "
        "This is purely keeping the conversation going.\n"
        "- You already KNOW him — do NOT re-ask his name, age, location, job or "
        "hobbies. Use what you know to make it personal.\n"
        "- Don't apologize for being slow or mention the delay — just reply "
        "naturally, warm and easy, never needy.\n"
        "- Match the vibe of the recent messages: if it was getting sexual, keep it "
        "flirty/suggestive; if it was casual, stay casual.\n"
        "- SHORT and human: lowercase, contractions, u/ur/ya, 0-1 emoji, vary your "
        "wording — never reuse a line or emoji you've already used here. No "
        "paragraphs, no narrating.\n\n"
        f"{STYLE_HUMANIZER + chr(10) + chr(10) if style_on else ''}"
        "Your reply is ONLY the message text — no JSON, quotes, or metadata."
    )
    user = (
        f"What you know about him:\n{facts_block}\n\n"
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
    typo_on = (await load_typo_flags(account_id))[_PURPOSE]    # thumb-typo opt-in
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

    # Spend-window + last-purchase gates (one transactions query for the set).
    win_start = now - timedelta(days=int(cfg["recent_spend_days"]))
    purchase_cut = now - timedelta(days=int(cfg["min_days_since_purchase"]))
    spend = await _spend_and_last_purchase(account_id, [int(f.fan_id) for (f, _) in cands], win_start)

    persona = await _load_persona(account_id)
    client = None
    sent = skipped_spend = skipped_cap = skipped_raced = errors = 0

    for f, inbound_at in cands:
        if sent >= max_sends:
            break
        fid = int(f.fan_id)
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
        msgs = _build_messages(persona, f, history, style, style_on=style_on)
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
        parts = [apply_word_restriction(p)[:_REPLY_MAX_CHARS]
                 for p in split_for_bubbles(raw, max_bubbles) if p.strip()][:max_bubbles]
        # Don't open with the same reaction word we just used ('oof' every send).
        if style_on and parts:
            parts = _dedupe_lead_reaction(parts, [b for d, b in history if d == "out"])
        if not parts:
            errors += 1
            continue
        if typo_on:  # opt-in: a realistic thumb-slip (+ maybe a "*fix" bubble)
            protect = [n for n in (f.real_name, f.generated_nickname,
                                   f.of_display_name) if n]
            parts = humanize_typos(parts, random.Random(f"{f.fan_id}:{raw}"),
                                   protect=protect, max_bubbles=max_bubbles)[:max_bubbles]
        if dry_run:
            sent += 1
            continue

        if client is None:
            try:
                client = await asyncio.to_thread(ax._make_client, account_id)
            except Exception:
                log.warning("autoreply client init failed account=%s", account_id,
                            exc_info=True)
                break

        sent_ok = False
        for idx, part in enumerate(parts):
            await hold_with_typing(account_id, fid,
                                   typing_delay_seconds(part, typing_wpm),
                                   typing_indicator=typing_indicator)  # "typing"
            try:
                result = await asyncio.to_thread(client.send_message, fid, part)
            except Exception:
                errors += 1
                log.warning("autoreply send failed account=%s fan=%s", account_id, fid,
                            exc_info=True)
                break
            mid = result.get("id") if isinstance(result, dict) else None
            if mid:
                await write_outbound_attribution(
                    account_id=account_id, fan_id=fid, message_id=int(mid),
                    sent_by_employee_id=None, body=str(result.get("text") or part),
                    price_cents=0,
                    created_at=ax._parse_iso(result.get("createdAt")) or datetime.utcnow(),
                    emit_live=True,
                )
                sent_ok = True
        if sent_ok:
            await _save_state(account_id, fid, spell_inbound_at=spell_anchor,
                              nudges_sent=nudges + 1, last_nudge_at=datetime.utcnow())
            sent += 1

    return {"enabled": True, "candidates": len(cands), "sent": sent,
            "skipped_spend": skipped_spend, "skipped_cap": skipped_cap,
            "skipped_raced": skipped_raced, "errors": errors, "dry_run": dry_run,
            "model": model}


async def _load_persona(account_id: str) -> str:
    async with get_session() as s:
        cfg = await s.get(AccountAiConfig, str(account_id))
    return (cfg.persona if cfg and cfg.persona else "").strip() or (
        "You are a warm, flirty OnlyFans creator texting a fan you've been chatting with."
    )
