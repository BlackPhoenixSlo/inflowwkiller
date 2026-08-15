"""reengage_buyers — win back recent buyers who went cold.

Selects fans who bought in the last N days but got no follow-up (last outbound older
than cold_hours, or never), and sends ONE warm, PERSONAL opener built from their own
stored gen_info lines (fan_profiles.tease/q + name) — the same lines the chatter's
"Lines" picker shows. Then normal ai_chatter / the Upseller takes over his reply.

The SELECTION is its own anti-spam cooldown: once we DM him his last-outbound is
fresh, so he stops qualifying as "cold" until cold_hours pass again; contact_guard
also excludes anyone DMed/nudged inside the guard window. No new state table.

Config (payload):
  lookback_days  3      buyers with a purchase in this window
  cold_hours     24     ...whose last outbound is older than this (or never)
  max_per_run    25     cap per run (hottest = most recent buyers first)
  tone           soft   soft | flirty
  guard_hours    12     contact-guard outbound/inbound window
  dry_run        false  plan + return the openers, send NOTHING
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta
from random import Random

from sqlalchemy import func, select

import automation_executor as ax
from attribution import write_outbound_attribution
from audiences import contact_guard_excludes
from automation_registry import register
from db.engine import get_session
from db.models import Blacklist, ContentOffer, Fan, FanProfile, Message, Transaction
from ._common import (
    hold_with_typing, load_hard_skip_ids, load_typing_indicator, load_typing_wpm,
    resolve_fan_name, typing_delay_seconds,
)

from . import _customs, _voice
from ._common import load_voice_blocks

log = logging.getLogger("of-relay.automation")

_BUY_KINDS = ("ppv_message", "tip", "ppv_post")

_FRAMES_SOFT = [
    "heyy {name} 🙈 been thinking about you",
    "hi {name} 💕 you popped into my head today",
    "miss talking to you {name} 🥺",
    "hey you 😊 hope your week's treating you okay {name}",
]
_FRAMES_FLIRTY = [
    "still thinking about last time {name} 😏",
    "cant stop thinking about you {name} 🥵",
    "you left me wanting more {name} 😈",
    "been a lil distracted thinking about you today {name} 🙈",
]
_FALLBACK_TAILS = [
    "what have you been up to?",
    "how's your week going babe?",
    "come keep me company? 😘",
]


_PET_DEFAULT = "babe"

# ── The MALE pools ───────────────────────────────────────────────────────────
# Hers pine — "been thinking about you", "miss talking to you", "cant stop
# thinking about you". That is the mechanic: the fan comes back to be wanted. A
# dom does not miss anyone; he NOTICED the absence and is calling him back, which
# is the same pull with the direction reversed.
#
# ⚠️ NO LINE HERE MAY CONTAIN AN ADDRESS WORD. `compose_opener` does
# `name = clean_name(...) or _PET_DEFAULT`, and the male default is "boy" — so a
# frame that also said "boy" produced "boy. ... boy?" in one message on the ~80%
# of fans OF gives us no name for. Hers has the identical hazard with "babe" and
# avoids it the same way: the {name} token is the ONLY address.
_FRAMES_SOFT_HIM = [
    "{name}. you went quiet on me",
    "{name}. noticed you disappeared",
    "been a while {name}",
    "{name}. you still around or what",
]
_FRAMES_FLIRTY_HIM = [
    "{name}. i keep coming back to last time 😏",
    "you left right when it got good {name}",
    "{name}. i wasnt finished with you 😈",
    "you owe me the rest of that conversation {name}",
]
_FALLBACK_TAILS_HIM = [
    "so what have you been doing with yourself",
    "hows the week been",
    "come keep me company",
]

# Keyed by NAME, not `id(pool)` — see the same note in make_right. Identity keys
# resolve silently to the female pool the moment a list is rebound or copied.
_POOLS: dict[str, tuple[list[str], list[str]]] = {
    "soft": (_FRAMES_SOFT, _FRAMES_SOFT_HIM),
    "flirty": (_FRAMES_FLIRTY, _FRAMES_FLIRTY_HIM),
    "tails": (_FALLBACK_TAILS, _FALLBACK_TAILS_HIM),
}


def _lane(name: str, voice: str) -> list[str]:
    hers, his = _POOLS[name]
    return his if str(voice or "").strip().lower() == "him" else hers


def _nz(s) -> str:
    return str(s).strip() if s and str(s).strip() else ""


def clean_name(raw: str) -> str:
    """The first token IF it looks like a real given name; else '' (→ pet name).
    OnlyFans often stores a location / tag / handle where a name should be
    ("Hawaii,USA/Spender"), and "heyy Hawaii,USA 🙈" reads like a bot — so anything
    that isn't a plain single alphabetic word is dropped."""
    first = (raw or "").split("/")[0].strip()
    # UNICODE-correct: accented given names (José, Núria, Iñaki) are real names, not
    # handles — the old [A-Za-z] class dropped them to "" → the "babe" pet fallback.
    return first if re.fullmatch(r"[A-Za-zÀ-ɏ][A-Za-zÀ-ɏ'’\-]{1,19}", first) else ""


def compose_opener(f: Fan, p: FanProfile | None, tone: str, rng: Random,
                   voice: str = "her") -> str:
    """TWO lines: a warm re-connection greeting, then one of HIS stored lines (a
    gen_info tease or question) underneath — so it reads as two bubbles, not one
    run-on. Flirty leans on teases, soft on questions; either falls back to the
    other, then to a generic tail. Name → a real given name if we have one, else
    the LANE's address ('babe' / 'boy') — never a raw location/tag."""
    name = (clean_name(resolve_fan_name(f) or "")
            or _voice.blocks(voice).fan_address)

    teases = [_nz(x) for x in ((p.tease1, p.tease2, p.tease3) if p else ()) if _nz(x)]
    quests = [_nz(x) for x in ((p.q1, p.q2, p.q3) if p else ()) if _nz(x)]

    flirty = tone == "flirty"
    frame = rng.choice(_lane("flirty" if flirty else "soft", voice)).replace("{name}", name)

    first, second = (teases, quests) if flirty else (quests, teases)
    line = rng.choice(first) if first else (rng.choice(second) if second else "")
    if not line:
        line = rng.choice(_lane("tails", voice))
    # Greeting on line 1, his line on line 2 (two bubbles).
    return f"{frame}\n{line}".strip()


async def _select_cold_buyers(account_id: str, lookback_days: int, cold_hours: int,
                              now: datetime) -> list[int]:
    since = now - timedelta(days=lookback_days)
    cold_before = now - timedelta(hours=cold_hours)
    async with get_session() as s:
        buyers = set((await s.execute(
            select(Transaction.fan_id).where(
                Transaction.account_id == str(account_id),
                Transaction.kind.in_(_BUY_KINDS),
                Transaction.occurred_at > since).distinct())).scalars().all())
        if not buyers:
            return []
        # last outbound per buyer
        rows = (await s.execute(
            select(Message.fan_id, func.max(Message.created_at)).where(
                Message.account_id == str(account_id),
                Message.direction == "out",
                Message.fan_id.in_([int(x) for x in buyers]))
            .group_by(Message.fan_id))).all()
        last_out = {int(fid): ts for fid, ts in rows}
        # Order the cold ones by most-recent purchase first (hottest lead).
        buy_at = dict((await s.execute(
            select(Transaction.fan_id, func.max(Transaction.occurred_at)).where(
                Transaction.account_id == str(account_id),
                Transaction.kind.in_(_BUY_KINDS),
                Transaction.occurred_at > since,
                Transaction.fan_id.in_([int(x) for x in buyers]))
            .group_by(Transaction.fan_id))).all())

    cold = [int(fid) for fid in buyers
            if (last_out.get(int(fid)) is None or last_out[int(fid)] < cold_before)]
    cold.sort(key=lambda fid: buy_at.get(fid) or since, reverse=True)
    return cold


@register("reengage_buyers")
async def run(account_id: str, payload: dict, *, run_id: int) -> dict:
    cfg = payload or {}
    lookback_days = int(cfg.get("lookback_days") or 3)
    cold_hours = int(cfg.get("cold_hours") or 24)
    max_per_run = int(cfg.get("max_per_run") or 25)
    guard_hours = int(cfg.get("guard_hours") or 12)
    tone = "flirty" if cfg.get("tone") == "flirty" else "soft"
    dry_run = bool(cfg.get("dry_run"))
    now = datetime.utcnow()

    cold = await _select_cold_buyers(account_id, lookback_days, cold_hours, now)
    if not cold:
        return {"sent": 0, "skipped": "no_cold_buyers", "candidates": 0}

    # Exclusions: recently contacted (shared guard), hard-skips, blacklist, paused,
    # and anyone mid-sale (an open offer — don't interrupt a live pitch).
    excl = await contact_guard_excludes(
        account_id, outbound_hours=guard_hours, inbound_hours=guard_hours)
    excl |= await load_hard_skip_ids(account_id)
    # ...and anyone owed a custom he has already paid for. This lane targets COLD
    # BUYERS — a man who tipped $200 for a voice note and is waiting on it is
    # exactly the profile this query selects, so without this he is the MOST
    # likely fan to get a "miss you, come see what's new" from the account that
    # owes him work. Same fact, same brake as the mid-sale ContentOffer exclusion
    # below: don't interrupt, and don't re-sell what is already bought.
    excl |= await _customs.owed_fan_ids(account_id)
    async with get_session() as s:
        # Blacklist is GLOBAL (fan_id only, no account scope) — mirror ai_chatter.
        excl |= set((await s.execute(select(Blacklist.fan_id))).scalars().all())
        excl |= set((await s.execute(
            select(ContentOffer.fan_id).where(
                ContentOffer.account_id == str(account_id),
                ContentOffer.status == "open"))).scalars().all())
        paused = set((await s.execute(
            select(Fan.fan_id).where(
                Fan.account_id == str(account_id),
                Fan.automation_paused_until.is_not(None),
                Fan.automation_paused_until > now))).scalars().all())
    excl |= {int(x) for x in paused}

    # Include-only audience — intersect BEFORE the [:max_per_run] slice so the
    # cap spends on fans the fence actually allows.
    import audience_include as _audiences
    audience_stats: dict = {}
    cold = await _audiences.filter_candidates(
        account_id, cold, kind="reengage_buyers", stats=audience_stats)

    picks = [fid for fid in cold if fid not in excl][:max_per_run]
    if not picks:
        return {"sent": 0, "skipped": "all_excluded", "candidates": len(cold),
                **audience_stats}

    async with get_session() as s:
        fans = {int(f.fan_id): f for f in (await s.execute(
            select(Fan).where(Fan.account_id == str(account_id),
                              Fan.fan_id.in_(picks)))).scalars().all()}
        profs = {int(p.fan_id): p for p in (await s.execute(
            select(FanProfile).where(FanProfile.account_id == str(account_id),
                                     FanProfile.fan_id.in_(picks)))).scalars().all()}
        s.expunge_all()

    _v = (await load_voice_blocks(account_id)).voice
    plans = []
    for fid in picks:
        f = fans.get(fid)
        if f is None:
            continue
        rng = Random(f"reengage:{account_id}:{fid}:{run_id}")
        plans.append((fid, f, compose_opener(f, profs.get(fid), tone, rng, _v)))

    if dry_run:
        return {"dry_run": True, "candidates": len(cold), "would_send": len(plans),
                "tone": tone,
                "preview": [{"fan_id": fid, "name": resolve_fan_name(f) or "",
                             "opener": text} for fid, f, text in plans[:15]]}

    # Human sending, same as the chatter: a realistic per-bubble typing delay (wpm)
    # with the live "…is typing" indicator. The two opener lines go as TWO bubbles
    # (type → greeting → type → his line), so it reads like she actually typed it.
    typing_wpm = await load_typing_wpm(account_id)
    typing_indicator = await load_typing_indicator(account_id)

    client = await asyncio.to_thread(ax._make_client, account_id)
    sent = 0
    errors = 0
    for fid, _f, text in plans:
        if not await ax.acquire_fan_lease(account_id, fid, "reengage_buyers"):
            continue
        try:
            bubbles = [b for b in text.split("\n") if b.strip()]
            any_ok = False
            for b in bubbles:
                await hold_with_typing(account_id, fid,
                                       typing_delay_seconds(b, typing_wpm),
                                       typing_indicator=typing_indicator)
                result = await asyncio.to_thread(
                    lambda t=b: client.send_message(fid, t, media_files=[]))
                msg_id = result.get("id") if isinstance(result, dict) else None
                if msg_id:
                    await write_outbound_attribution(
                        account_id=account_id, fan_id=int(fid), message_id=int(msg_id),
                        sent_by_employee_id=None, automation_kind="reengage_buyers",
                        body=str(result.get("text") or b), price_cents=0,
                        created_at=ax._parse_iso(result.get("createdAt")) or datetime.utcnow(),
                        emit_live=True,
                    )
                    any_ok = True
            if any_ok:
                sent += 1
        except Exception:
            errors += 1
            log.warning("reengage_buyers send failed account=%s fan=%s",
                        account_id, fid, exc_info=True)
        finally:
            await ax.release_fan_lease(account_id, fid)

    return {"sent": sent, "errors": errors, "candidates": len(cold),
            "picked": len(picks), "tone": tone, **audience_stats}
