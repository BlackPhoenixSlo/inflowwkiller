"""
_clock.py — the prompt clock: creator-local wall time for the chat prompt.

One concern, three consumers (ai_chatter, autoreply, welcome_chatter_for_info)
and three functions: read the account's offset, render it, wrap it in the prompt
sentence. Nothing else belongs here.

Why its OWN module and not `_common`: this used to live in `_common`, which is
2.4k lines spanning skip-lists, promo-spam, guardrails, style layers, typo
injection, word filtering and text normalisation. That breadth is not a style
complaint — it is a merge hazard. A `-X theirs` merge of `_common` deleted these
three functions as collateral from a conflict in an unrelated section, and took
the cat-sticker loaders and the banned-words wiring with them, because the
arbitration unit is the FILE and that file is twenty-seven features wide.

A leaf should be reachable without importing a grab-bag to get at it. Same
reasoning that produced `fan_state`, `jsonsafe` and `vision`.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from db.engine import get_session
from db.models import AccountAiConfig

from . import rhythm  # tz_offset_for — resolves IANA name or legacy hour offset


async def load_clock_tz(account_id: str) -> int | None:
    """Creator-local offset in MINUTES for the prompt clock. IANA `timezone`
    wins (DST-correct); legacy `utc_offset` hours is the fallback; None when
    neither is set — and None means NO clock line, never the server's clock."""
    async with get_session() as s:
        cfg = await s.get(AccountAiConfig, account_id)
    if cfg is None:
        return None
    return rhythm.tz_offset_for(getattr(cfg, "timezone", None),
                                getattr(cfg, "utc_offset", None))


def clock_line(tz_offset_minutes: int | None,
               now: datetime | None = None) -> str:
    """Creator-local wall clock for the chat prompt, e.g.
    'Thursday, July 23 — 12:32 AM (late night)'. Returns '' when the account
    has no timezone/utc_offset configured: the model must be told NOTHING
    rather than the server's clock — a fan asking "what time is it where you
    are?" caught the bot claiming 10am at 12:30am creator-time, then burning
    the thread trying to talk its way out."""
    if tz_offset_minutes is None:
        return ""
    local = (now or datetime.utcnow()) + timedelta(minutes=int(tz_offset_minutes))
    h = local.hour
    tod = ("late night" if h < 5 else "morning" if h < 12 else
           "afternoon" if h < 18 else "evening" if h < 22 else "late night")
    return f"{local.strftime('%A, %B %d — %I:%M %p')} ({tod})"


def clock_block(clock: str) -> str:
    """The prompt sentence for a resolved `clock_line` ('' in → '' out, so the
    prompt stays byte-equal when the account has no tz configured). ONE copy
    for all five conversational builders — a wording tweak here reaches every
    engine, so the persona's time behavior can never silently fork.

    The sentence is the COMPACTED one main's prompt-diet pass (0bf4d80) landed
    on every engine it reached. The one holdout that kept the long form was
    deep_convo, which no longer exists — the short sentence is now simply the
    sentence. `test_chat_clock` pins the marker prefix, not the wording."""
    return (
        f"RIGHT NOW for you it is {clock} — never claim a different time of "
        "day.\n\n" if clock else "")
