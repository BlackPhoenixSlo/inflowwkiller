"""How many of the newest thread rows the reply model reads.

PURE — no I/O, no DB, no imports from `ai_chatter` (which imports this). The rule
lives here alone so it can be unit-tested without a temp DB, a FakeOF and an LLM
patch: every interesting case is three lists and a datetime.

THE RULE (operator-confirmed):

    the newest N rows of the thread,
    MINUS any row older than T before his newest inbound,
    BUT never fewer than FLOOR rows.

Precedence, in that order: N caps, T trims, FLOOR restores. FLOOR is the
last-resort guarantee — it is what stops the age cut from handing the model an
empty (or one-line) transcript when a fan comes back after a night. It can never
invent rows the thread does not have: a 3-row thread yields 3.

Why the anchor is HIS NEWEST INBOUND and not `now`: rhythm defers her reply by up
to 6 h (`rhythm.py:MAX_DELAY_S`), so a wall-clock anchor could cut away the very
line she is answering, and the window would move under a deferred draft between
the tick that deferred it and the tick that wakes it. Anchored on his line the
window is the same at both.

── TWO DIFFERENT GUARDS, AND WHICH ONE FIRES ─────────────────────────────────
They are easy to confuse; they answer different questions.

1. THE DEGRADE GUARD — "we cannot measure recency at all."
   No age limit configured, no anchor (a `force_ids` turn to a fan who never
   wrote), or a `msg_at` whose length does not match `messages` (a hand-built
   `_Cand`: `scripts_api`'s simulate preview and the unit tests set `messages`
   directly and never touch `msg_at`). Returns `(max_rows, "n")` — TODAY'S
   BEHAVIOUR, a flat N slice. Deliberately NOT the floor: with no usable clock
   the honest answer is the old behaviour, not six rows of unknown age.

2. THE FLOOR — "the clock worked, and it trimmed too hard."
   T left fewer than FLOOR rows. Returns `(FLOOR, "floor")`. Only reachable on
   the path where timestamps were usable.

And one hard invariant on top of both: **this function never returns 0 while an
anchor exists.** `c.messages[-0:]` is `c.messages[0:]` — the WHOLE thread, up to
~1,000 rows, uncapped by N. A zero would turn the safest-looking path into the
most expensive and most stale-reciting prompt the engine can emit. The row at the
anchor is his own newest line and can never be stale, so 1 is the honest minimum.
The call site carries the same guard again (`if tail > 0 else []`), belt and
braces, because the cost of getting this wrong is silent and large.
"""

from datetime import datetime, timedelta, timezone
from typing import Sequence

# The four config keys, named here so the engine, the validator and the tests
# all spell them the same way.
KEY_ENABLED = "history_window_enabled"
KEY_ROWS = "history_window_rows"
KEY_HOURS = "history_window_hours"
KEY_FLOOR = "history_window_floor"

# Defaults WHEN THE SWITCH IS ON. Off, the engine keeps `_HISTORY_TAIL` (20) and
# never calls this module — see `ai_chatter.run`.
DEFAULT_ROWS = 40
DEFAULT_HOURS = 12
DEFAULT_FLOOR = 6

# Hard bounds on N wherever it is read, including the job payload, which is NOT
# schema-checked: `history_tail` is absent from ai_chatter's knob catalog, and
# `_validate_payload_for_kind` passes unknown keys through untouched, so a
# raw-JSON `{"history_tail": -5}` would otherwise reach the slice as
# `c.messages[5:]` — nearly the whole thread again, by the same `-0` family of
# accident.
MIN_ROWS = 1
# 100, not 400: at the audit's ~13.6 tokens per rendered transcript line, 400 rows is
# ~5,400 tokens — roughly four times the ENTIRE current reply prompt, and far
# outside anything this change was costed for. A ceiling is only useful if it sits
# where a mistake stops being affordable.
MAX_ROWS = 100


def clamp_rows(value: "int | float | str | None", default: int) -> int:
    """Coerce an operator-supplied N into [MIN_ROWS, MAX_ROWS].

    Absent, zero, empty or unparseable → `default` (the house "0 means no ceiling
    from this knob" convention, which for a row count means "use the shipped
    one"). Negative or absurd → clamped, never passed to a slice.
    """
    try:
        n = int(value or 0)
    except (TypeError, ValueError):
        return default
    if n == 0:
        return default
    return max(MIN_ROWS, min(n, MAX_ROWS))


def _naive(dt: "datetime | None") -> "datetime | None":
    """Strip a tzinfo, converting to UTC first. Returns None unchanged.

    Everything in this codebase is naive UTC (`parse_ts`, `datetime.utcnow()`), so
    this is a no-op today. It exists because comparing an aware datetime to a naive
    one raises `TypeError`, and `tail_for` runs INSIDE `run()`'s per-fan loop — an
    exception there does not cost one fan's reply, it costs the account's whole
    sweep. `parse_ts` hands back an already-`datetime` value unnormalised, so a
    driver or data change is the only thing standing between us and that crash.
    Normalising is cheaper than being right about the future.
    """
    if dt is None or dt.tzinfo is None:
        return dt
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def tail_for(msg_at: "Sequence[datetime | None]", n_rows: int,
             anchor: "datetime | None", *, max_rows: int,
             max_age: "timedelta | None", floor: int = 0) -> "tuple[int, str]":
    """Return (tail, bound): how many of the NEWEST rows to slice, and which limit
    decided it.

    `msg_at` is `c.msg_at` oldest→newest, parallel to `c.messages`; `n_rows` is
    `len(c.messages)`; `anchor` is his newest inbound (`c.last_in_at`). All
    datetimes are naive UTC, like `parse_ts` and `datetime.utcnow()`.

    `bound` is one of:
      "n"     — N truncated the thread (or we degraded to today's slice)
      "t"     — the age cut decided
      "floor" — the age cut would have gone below FLOOR, so FLOOR won
      "none"  — the whole thread fit inside every limit; nothing bound

    "n" is reserved for a REAL truncation. A thread that simply runs out at
    exactly `max_rows` rows was not bound by anything and reports "none" — these
    labels feed the operator-facing counters, and "N is binding" has to mean
    there were more rows it could not have.

    A `None` timestamp DEGRADES to GUARD 1's answer — the flat `max_rows` slice,
    labelled "n" — and does NOT stop the walk with what it had measured so far.
    That is deliberate and it is worth stating plainly, because it is the one
    outcome here that can be BOTH more rows and staler ones than the age cut would
    have produced: on a 60-row thread whose first 50 rows are three days old, a
    NULL at row 55 returns 40 rows where the age cut alone would have returned 10.
    An unreadable clock is not the age cut deciding — it is the clock breaking —
    and the honest fallback for a broken clock is what the engine shipped before
    this window existed, not a window measured with the instrument that just
    failed. See the comment at the return site.

    (Reachability is low by construction: `Message.created_at` is
    `nullable=False`, and `ai_chatter._gather` DROPS a row whose text will not
    parse rather than appending `None`, so only a true SQL NULL gets this far.)

    The never-zero invariant then floors the result at 1.
    """
    max_rows = max(MIN_ROWS, min(int(max_rows), MAX_ROWS))
    floor = max(0, int(floor))
    n_rows = max(0, int(n_rows))
    # The floor can never exceed either the thread or the row cap: it is a
    # last-resort guarantee, not a second way to widen the window.
    eff_floor = min(floor, max_rows, n_rows)

    # GUARD 1 — no usable clock. Today's behaviour, NOT the floor (see the
    # module docstring). `max_rows` is already >= 1, so this cannot return 0.
    if max_age is None or anchor is None or len(msg_at) != n_rows:
        return max_rows, "n"

    within = 0
    bound = "none"
    truncated = False
    try:
        cutoff = _naive(anchor) - max_age
        for t in reversed(msg_at):               # newest first
            if within >= max_rows:
                truncated = True                 # there WAS another row
                bound = "n"
                break
            t = _naive(t)
            if t is None:
                # A row whose clock we cannot read. This is NOT the age cut
                # deciding — it is the clock breaking, so it takes GUARD 1's
                # answer (today's flat slice), not the floor. A NULL at the HEAD
                # of a thread is the expected shape (NULLs sort first under
                # ORDER BY created_at) and reaching it here means we already
                # walked back past every readable row; a None mid-walk means the
                # stored format changed under us. Either way "six rows of
                # unknown age" is a worse answer than "what we shipped
                # yesterday".
                return max_rows, "n"
            if t < cutoff:
                bound = "t"
                break
            within += 1
    except (TypeError, AttributeError, ValueError, OverflowError):
        # Anything we could not make comparable: a non-datetime in `msg_at` (no
        # `.tzinfo` — AttributeError), an exotic tzinfo, an out-of-range shift.
        # GUARD 1's answer, for GUARD 1's reason: with no usable clock, today's
        # behaviour — never a crash inside the fan loop, where it would cost the
        # account's whole sweep rather than one fan's reply.
        return max_rows, "n"
    if not truncated and bound == "none":
        # The walk consumed the whole thread with neither limit firing.
        bound = "none"

    if within < eff_floor:
        return eff_floor, "floor"
    # THE NEVER-ZERO INVARIANT. An anchor exists here by construction (guard 1
    # returned otherwise), so his own newest line is a row we can always honestly
    # show. Returning 0 would slice `[-0:]` = the whole thread.
    return max(1, within), bound
