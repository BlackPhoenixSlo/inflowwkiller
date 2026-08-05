"""
service/automations/_ghost.py — the ghost cycle: whole DAYS she doesn't answer him.

`rhythm.py` next door is about how long ONE reply waits — seconds to an hour, plus
a night. This is a different animal at a different timescale: stretches of days
where she does not answer a fan at all, on a schedule that repeats, so a fan who
has her attention every single day learns that he does not get it automatically.
Operator ruling 2026-08-04: chat 3 days → dark 1, chat 4 → dark 2, chat 5 → dark
2.5, then back to the top.

It lives in its own module rather than inside rhythm.py because it shares nothing
with it: no RhythmCtx, no rng, no cover lines, and it is deliberately unreachable
from `decide()` / `decide_availability()` — a dark day must not change how fast a
reply lands on the days she IS there. Its only consumer is ai_chatter's candidate
filter, which skips the fan outright. (`test_ghost_cycle.py` pins that isolation.)

⚠️ This is the one silence in the product that fires on a fan who is OWED AN
ANSWER. Every discretionary pause in rhythm.py refuses while `answer_owed` — a gap
on an unanswered question is how a thread dies. The ghost overrides that ON
PURPOSE: withholding a reply he is waiting for IS the product, and a ghost that
only skipped turns she was never going to take would be an elaborate no-op. It
ships off, and the caller carries the exemptions (a live sale is never ghosted).

PURE: `now` is a parameter, there is no DB read and no wall clock. The single
piece of state this feature has anywhere is one nullable `rhythm_state.ghost_anchor`
column, and the caller owns it.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

MAX_ROWS = 8
MAX_DAYS = 30.0        # a stage longer than a month is a typo, not a plan
_DAY_S = 86400.0       # stages are authored in DAYS and compared in seconds
DEFAULT_CYCLE: tuple[tuple[float, float], ...] = (
    # (chat_days, ghost_days)
    (3.0, 1.0),
    (4.0, 2.0),
    (5.0, 2.5),
)


@dataclass(frozen=True)
class GhostWindow:
    """The dark stretch a fan is in RIGHT NOW. Only ever constructed when he is
    actually in one — `window()` returns None otherwise — so every field is real
    and the caller never has to test one before reading another:

        chat_started_at ──── started_at ──── (now) ──── ends_at
          the stage's        it went          the dark stretch he
          talking run        dark here        is in right now

    `chat_started_at` is the one the caller cannot derive on its own, and it is
    the interesting one: "was she EVER talking to him during the run this silence
    is supposed to be a withdrawal from?" A ghost is only a withdrawal if there
    was something to withdraw."""
    started_at: datetime
    ends_at: datetime
    chat_started_at: datetime


def parse_cycle(raw) -> tuple[tuple[float, float], ...] | None:
    """An operator-authored cycle → ((chat_days, ghost_days), …), or None when
    there is nothing usable to run (⇒ caller falls back to DEFAULT_CYCLE).

    Stored shape is a list of `{"chat_days": <days>, "ghost_days": <days>}`. Half
    days are real (the shipped cycle ends on 2.5), so these are floats, not ints.

    A row may declare `ghost_days: 0` — "chat 3 days, then chat 4 more" is a
    strange thing to author but an honest one, and rejecting it would make the
    editor lose an entire row's worth of typing to a transient zero. A cycle whose
    stages ALL total zero is rejected: it has no length, so no fan could ever have
    a position inside it.

    Returns None for anything malformed rather than raising. A bad cycle on the
    send path must degrade to the shipped one, never 500 a run — `scripts_api` is
    where a typo becomes a 422 the operator can actually see."""
    if not isinstance(raw, (list, tuple)) or not raw:
        return None
    out: list[tuple[float, float]] = []
    for row in list(raw)[:MAX_ROWS]:
        if not isinstance(row, dict):
            return None
        try:
            chat = float(row.get("chat_days"))
            ghost = float(row.get("ghost_days"))
        except (TypeError, ValueError):
            return None
        if not (0 <= chat <= MAX_DAYS) or not (0 <= ghost <= MAX_DAYS):
            return None
        out.append((chat, ghost))
    if sum(c + g for c, g in out) <= 0:
        return None
    return tuple(out)


def window(anchor: datetime, now: datetime,
           cycle: tuple[tuple[float, float], ...] | None = None) -> GhostWindow | None:
    """Is she dark on this fan right now? None ⇒ no, go talk to him — the same
    "None means carry on" shape `rhythm.decide_availability` already uses.

    The cycle REPEATS, so a fan's position is `(now - anchor) mod cycle_length` —
    there is no stored stage index and nothing to advance. That matters more than
    it looks: a persisted "which stage am I on" has to be stepped forward by some
    tick, and a tick that doesn't run (a paused executor, a fan nobody swept for a
    week) leaves the pointer stale, silently shifting every later stage. Modulo of
    a wall clock cannot drift, cannot double-advance, and re-derives the same
    answer from any two processes reading the same row.

    The price is that EDITING the cycle re-reads every fan's position against the
    new stage lengths, so a save can move a fan into or out of a dark day at once.
    That is a real property, and the reason the editor says so out loud.

    An anchor in the future (a clock skew, a hand-edited row) is never a ghost —
    Python's `%` on a negative elapsed returns a POSITIVE remainder, which would
    otherwise drop him at an arbitrary point in the cycle, dark stages included,
    for reasons no operator could ever reconstruct."""
    stages = cycle or DEFAULT_CYCLE
    total_s = sum((c + g) for c, g in stages) * _DAY_S
    if total_s <= 0:
        return None
    elapsed = (now - anchor).total_seconds()
    if elapsed < 0:
        return None
    pos = elapsed % total_s
    for chat_days, ghost_days in stages:
        chat_s = chat_days * _DAY_S
        if pos < chat_s:
            return None
        pos -= chat_s
        ghost_s = ghost_days * _DAY_S
        if pos < ghost_s:
            started = now - timedelta(seconds=pos)
            return GhostWindow(started_at=started,
                               ends_at=now + timedelta(seconds=ghost_s - pos),
                               chat_started_at=started - timedelta(seconds=chat_s))
        pos -= ghost_s
    return None                          # float dust past the last edge
