"""
service/automations/_stepout.py — she steps out: when she goes, and what brings
her back.

`rhythm.py` next door owns how long ONE reply waits, and `_ghost.py` owns whole
DAYS dark on a repeating calendar. This is the middle timescale: every 7-15 of her
replies on a chat that has gone quiet, she is simply gone for an hour or two, the
way a person with somewhere else to be is gone. Operator ruling 2026-08-09.

It lives in its own module for the same reason `_ghost.py` does: the SCHEDULE
shares nothing with the reply sampler. `is_due` counts her replies and `persisted`
reads HIS message times — neither needs a RhythmCtx, an rng, a cover pool or a
clock. rhythm keeps only the pause itself (how long, and where it sits in the
availability ladder), which is exactly what rhythm is for.

PURE: every input is a parameter, there is no DB read and no wall clock. The whole
feature's state is two nullable `rhythm_state` columns and the caller owns both.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from random import Random

# How many of HER replies between one step-out and the next. Drawn per step-out
# rather than fixed, so the interval is never something a fan could count.
MIN_EXCHANGES = 7
MAX_EXCHANGES = 15

# HE KEPT WRITING. Two messages at least a minute apart, while she is gone, ends it.
# A minute is the floor because a burst of bubbles is one thought arriving in
# pieces, not two attempts — the whole point is that he put the phone down and
# picked it up again. There is deliberately NO upper bound on the gap: a man who
# waits twenty minutes and writes again has shown MORE patience, not less, and an
# upper bound would make him the one fan who cannot earn an answer.
PERSIST_MSGS = 2
PERSIST_GAP_MIN_S = 60.0


@dataclass(frozen=True)
class Config:
    """One feature's settings as one name.

    `ai_chatter.run()` is already one of the largest functions in the codebase, and
    seven loose locals for one feature is seven more things in scope for every line
    of it. Parsed once, here, so the coercion rules (and the None-not-zero sentinel
    for the durations) live beside the code that defines what they mean.

    `min_s`/`max_s` are None when the operator has stored nothing — None and not 0,
    because 0 is a number somebody can type into the field and it must not silently
    read as "ignore me" (`rhythm.RhythmCtx` documents the same convention)."""

    on: bool
    lo: int
    hi: int
    break_msgs: int
    break_gap_s: float
    min_s: float | None
    max_s: float | None

    @classmethod
    def from_cfg(cls, cfg: dict, *, enabled: bool) -> "Config":
        """`enabled` is the caller's own gate (rhythm off ⇒ the sampler never runs,
        so the step-out cannot either) AND-ed with the feature's own switch."""
        lo = max(1, _num(cfg, "rhythm_stepout_min_exchanges", MIN_EXCHANGES, int))
        hi = max(lo, _num(cfg, "rhythm_stepout_max_exchanges", MAX_EXCHANGES, int))
        return cls(
            on=bool(enabled) and bool(cfg.get("rhythm_stepout_enabled")),
            lo=lo, hi=hi,
            break_msgs=max(0, _num(cfg, "rhythm_stepout_break_msgs", PERSIST_MSGS, int)),
            break_gap_s=max(0.0, _num(cfg, "rhythm_stepout_break_gap_s",
                                      PERSIST_GAP_MIN_S, float)),
            min_s=_minutes_to_s(cfg.get("rhythm_stepout_min_minutes")),
            max_s=_minutes_to_s(cfg.get("rhythm_stepout_max_minutes")),
        )


def _num(cfg: dict, key: str, default, cast):
    """MISSING means "the shipped default"; a stored 0 means ZERO.

    `cfg.get(key) or default` conflates the two, and every knob here has a meaning
    for 0 — `break_msgs=0` disables the early exit on purpose. The first version of
    this class used `or` for the break knobs and the module default for the exchange
    knobs, so the same absent key meant "off" on one line and "shipped default" two
    lines up; a test built a Config from a bare dict, got `break_msgs=0`, and the
    step-out silently could not be interrupted."""
    v = cfg.get(key)
    return default if v is None else cast(v)


def _minutes_to_s(minutes) -> float | None:
    """THE minutes→seconds boundary. The config and the UI speak minutes; every
    consumer wants seconds; this is the one place that converts."""
    return None if minutes is None else float(minutes) * 60.0


def draw_target(account_id: str, fan_id: int, lo: int, hi: int,
                salt: str = "") -> int:
    """The interval, in HER replies, until the next step-out.

    Seeded rather than stored: the same (fan, salt) always draws the same number,
    so a tick that re-reads it mid-interval cannot move the goalposts, and nothing
    has to be persisted just to keep a draw stable. `salt` is her reply count at the
    firing that scheduled it, which is what makes consecutive intervals differ."""
    lo = max(1, int(lo))
    hi = max(lo, int(hi))
    return lo + Random(f"stepout:{account_id}:{fan_id}:{salt}").randrange(hi - lo + 1)


def is_due(*, mark: int | None, target: int | None, total_out_n: int,
           account_id: str, fan_id: int, lo: int, hi: int) -> bool:
    """Is she due to step out on this fan?

    Counts HER replies (`total_out_n`, over the whole thread) against a MARK — where
    that counter stood at her last step-out — rather than decrementing a countdown.
    Same reasoning as `_ghost`'s anchor: a tick that fails to run cannot leave a mark
    stale, while every later interval would silently inherit a countdown's drift.

    A fan who has never stepped out has no mark, and there is no honest mark to
    invent for him. Taking "now" makes him due one interval from today, which puts
    the whole roster on one phase and takes every thread dark on the same afternoon —
    `_ghost`'s documented mistake, in a feature that fires forty times faster. Taking
    "the start of the thread" is worse: every established fan is already hundreds of
    exchanges past due and they all go dark on the rollout.

    So a fan with no mark is read PERIODICALLY instead, phased by his own id: about
    one fan in `target` is due on any given reply, and the roster spreads itself over
    one full interval with no write, no backfill and no rollout event. The mark rule
    takes over from his first real step-out.

    ⚠️ The obvious-looking third option — derive a mark as `total_out_n - offset` —
    is a silent NO-OP, and it shipped here first. The derived mark moves with the
    counter it is subtracted from, so the difference is the offset forever and the
    condition can never be met. It reads correct, it type-checks, and the feature
    simply never fires."""
    n = int(total_out_n)
    if n <= 0:
        # "She stepped out" is only a fact about someone who was there.
        return False
    period = int(target) if target else draw_target(account_id, fan_id, lo, hi)
    period = max(1, period)
    if mark is None:
        phase = Random(f"stepout-phase:{account_id}:{fan_id}").randrange(period)
        return ((n + phase) % period) == 0
    # Clamped because `total_out_n` is a COUNT OF ROWS, and rows can go away — an
    # unsend, a re-scrape, a repaired thread. A mark stranded above the counter makes
    # `n - mark` negative forever, and that fan is silently never due again. Free.
    return (n - min(int(mark), n)) >= period


def persisted(in_run: Sequence[datetime], since: datetime | None, *,
              msgs: int = PERSIST_MSGS, gap_s: float = PERSIST_GAP_MIN_S) -> bool:
    """Has he written again, on purpose, since `since`?

    `in_run` is the trailing run of HIS messages that she has not answered — the
    caller clears it on her own outbound, so this is always "since she last spoke".

    Requires `msgs` messages AND `msgs - 1` gaps of at least `gap_s` between them.
    Both halves matter and the second is the whole point: five bubbles fired off in
    twenty seconds is ONE thought, and counting it as persistence would end the
    silence for a fan who never noticed there was one.

    Pure and total — an empty run, a None `since`, and unsorted input are all fine.
    An exception here would surface inside the candidate loop and take the whole
    account's run with it.

    ⚠️ There is deliberately NO `msgs <= 1` fast path. One shipped here first, as
    `return bool(in_run)`, and it skipped BOTH filters — so with `break_msgs = 1` the
    messages that were already sitting unanswered when she stepped out counted as him
    "writing again", and the pause died on the very next 30-second tick with nothing
    new from him at all. The general expression below is already correct at msgs=1
    (`len(ts) < 1` ⇒ False, `real_gaps >= 0` ⇒ True), so the special case bought
    nothing but the divergence."""
    ts = sorted(t for t in in_run if t is not None and (since is None or t > since))
    if len(ts) < msgs:
        return False
    real_gaps = sum(1 for a, b in zip(ts, ts[1:])
                    if (b - a).total_seconds() >= gap_s)
    return real_gaps >= msgs - 1
