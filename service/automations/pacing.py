"""
service/automations/pacing.py — how a bubble is TYPED, not when a reply starts.

`rhythm.py` owns WHEN she answers (bubble 0's total inbound→reply latency). This
module owns everything AFTER that: the gaps BETWEEN the bubbles of one reply, and
what the fan's "…is typing" indicator does while they land. The two never overlap
— rhythm touches only `idx == 0`, pacing only `idx >= 1`.

WHY IT EXISTS. Until now the only thing between two bubbles was
`typing_delay_seconds(text, wpm)` = `words / wpm * 60`. Bubble length is narrowly
distributed, so the gap was too — and measured over 120 days of production the
result is a distribution no person produces:

    inter-bubble gap      BOT      HUMAN chatter    FAN (in→in)
    0-2s                  7.9%          4.5%           1.3%
    3-5s                 23.8%         18.0%           5.2%
    6-10s                43.1%         30.0%          14.8%     ← the pile
    11-20s               22.1%         20.5%          22.2%
    21-40s                2.1%         10.1%          18.9%
    41-60s                0.1%          3.7%           7.7%
    61-120s               0.1%          6.0%           9.6%
    2-5min                0.2%          4.1%          10.2%
    5-15min               0.5%          3.0%          10.2%
    ── above 20s          3.0%         26.9%          56.6%
    n                    35,688        1,952         23,426

The tell is NOT that she is too fast on average. The bottom of the distribution
already matches (p10 3s vs 4s). The tell is the last row: **she never once stops
mid-reply.** Nothing in the old code path could produce a 90-second inter-bubble
gap, so across 35,688 samples it essentially never happened.

WHY IT IS NOT A CONSTANT. The obvious fix — "add N seconds to every bubble" — was
simulated against the table above and is WORSE THAN DOING NOTHING. A flat +11s
moves 66% of all gaps into the 11-20s band: the same pile, relocated to the one
band that already matched, plus a hard floor at 11s where humans put 44% of their
gaps below. Distance-to-human goes 47.8 → 134.3. The same is true of a flat
5-10s "thinking" pause (→ 118.5). Every budget tier of a grid search spends its
seconds on RARE LONG pauses instead; the fitted optimum for a 5-10s always-on
break is probability ZERO. So:

    dispersion, not delay. One bubble in ~three gets a real pause; the rest are
    left alone, which is what preserves the fast bursts a person actually sends.

THE THREE TERMS (all on idx >= 1 only):
  • ENTER PRESS — uniform(0.4, 1.2)s, always. The cheapest term in the file: it
    moves bot p10 from 3s to 4s, which is exactly the human p10.
  • EMOJI REACH — uniform(2, 6)s, only when the FINAL bubble text carries one.
    17.4% of bot bubbles do (humans: 12.8%). Charged on the finalized string, so
    an account running `strip_emojis` pays nothing — the emoji is already gone by
    the time `parts` is built (`finalize_draft` strips before the split).
  • THE DRIFT — a lognormal pause on `p_drift` of bubbles, capped. This is the
    entire fix; the two above are garnish. Capped at 90s by default so the hold
    stays INLINE-SAFE (`rhythm.INLINE_MAX_S` is 120): a gap that needs the
    scheduler is a different feature with a lease, a job row and a wake poll, and
    this module deliberately does not open that door.

THE INDICATOR IS THE OTHER HALF, AND IT IS FREE. `hold_with_typing` re-emits OF's
live typing frame every 2.5s for the WHOLE hold, so today the bar is on for
exactly as long as the gap, on every bubble, on every account — a perfectly
reliable oracle that says "the entire delay was typing". Real people type, stop,
and type again. So a pace carries THREE numbers the caller hands straight to
`hold_with_typing`, and only the first one affects timing at all:

    total_s   what the caller holds. THE ONLY number that moves a clock.
    quiet_s   leading slice with the indicator OFF (she hasn't started typing:
              she's reading, thinking, reaching for the emoji tray).
    think_*   a silent slice INSIDE the typing phase — she stopped mid-sentence.
              Costs zero added latency. It is a redistribution, not an addition.

INVARIANT, load-bearing: `total_s` is the whole hold. `quiet_s` and `think_*`
only decide WHEN frames are emitted inside it. That is what keeps every existing
guard honest without touching one of them — `rhythm.INLINE_MAX_S`, the fan lease
TTL, `ai_chatter._RUN_INLINE_BUDGET_S`, the deferral boundary and `run_sim`'s
virtual clock all read a single total and keep reading the same one.

PURE + SEEDED, like rhythm.py: `rng` is injected, there is no wall clock and no
global random, so a test is not flaky and a sim replays. The caller passes its
OWN `Random` (seeded per bubble), never rhythm's — sharing one would shift every
later rhythm draw and silently re-roll the seeded rhythm tests.

`hold_for_bubble` is the ONE entry point — it owns which of the three cases a
bubble is in (rhythm's, pacing's, or nobody's) so that precedence is a readable
list here rather than a branch tangle inside a 2,000-line send loop. `bubble_pace`
and `silent_hold` are the samplers it dispatches to.

Ships DISABLED (`PaceConfig.enabled` False ⇒ `hold_for_bubble` returns the
caller's typing time and nothing else, byte-identically, without drawing from the
rng at all), because the accounts already earning do not move on a distributional
hunch.
"""
from __future__ import annotations

from dataclasses import dataclass
from random import Random

from ._common import has_emoji
from .rhythm import INLINE_MAX_S as _INLINE_MAX_S

# ── The three terms. Deliberately NOT operator knobs: an operator has no basis on
# which to answer "how long does it take to press enter", and every one of these is
# measured or bounded rather than chosen. The RATE and the CAP are knobs (see
# PaceConfig) because those are policy — how human vs how fast — and that is a
# question about the account, not about typing.
_ENTER_S = (0.4, 1.2)        # find the send button and hit it
_EMOJI_S = (2.0, 6.0)        # open the tray, scroll, pick
_EMOJI_RATE_NOTE = 0.174     # measured share of bot bubbles carrying an emoji.
                             # Not used in code — the check below is exact, per
                             # bubble. Recorded so the number in the docstring has
                             # a home next to the thing it justifies.

# The drift: a lognormal whose median is ~e^mu seconds. mu=3.8 ⇒ ~45s median, and
# sigma=1.1 gives the long right shoulder the 61-120s / 2-5min bands need. These
# came out of a grid search against the blended human+fan target above; at the
# inline-safe cap (90s) they are the best-fitting pair in the sweep.
_DRIFT_MU, _DRIFT_SIGMA = 3.8, 1.1

# A pause is only believable as "she stopped typing" if there was typing to stop.
# Below this the typing phase is over before a person could have paused inside it,
# so the think-gap is skipped rather than scaled to something meaningless.
_THINK_MIN_TYPING_S = 6.0
_THINK_S = (5.0, 10.0)       # the operator's number, and a natural mid-sentence stall
_THINK_MAX_SHARE = 0.6       # never eat more than this much of the typing phase

# `has_emoji` is imported from `_common`, where `strip_emojis` already lives. The
# emoji charge is only correct if it asks the SAME question the send path answers
# when it decides whether the character survives, so the two share one pattern
# rather than two that a test has to police. Note this is always evaluated on the
# FINALIZED bubble (post `finalize_draft`, post typo/non-native, post
# `strip_emojis`) — an account running the emoji strip has none left by then and
# correctly pays nothing.


@dataclass(frozen=True)
class PaceConfig:
    """The operator-facing surface. Every field is persisted in
    `ai_chatter_config_json` next to the `rhythm_*` keys and validated in
    `scripts_api` — a key this dataclass has and that validator does not is
    silently dropped on save, which is the failure mode this comment exists to
    prevent."""
    enabled: bool = False
    # How often a bubble gets a real pause. The whole fix lives here. 0 ⇒ the
    # drift never fires and only the enter/emoji garnish remains.
    drift_pct: float = 30.0
    # The ceiling on one drift, in seconds. Bounded by the caller to stay inline
    # safe; see `MAX_DRIFT_CAP_S`.
    drift_cap_s: float = 90.0
    # The mid-typing stall. Costs no latency — it only blanks the indicator.
    think_gaps: bool = True

    @classmethod
    def from_cfg(cls, cfg: dict) -> "PaceConfig":
        """Read one account's merged `ai_chatter_config_json`.

        The KEY NAMES live HERE, next to the fields they fill and next to the
        validator warning above — not in the engine. A caller that has to spell
        them out is a caller that can misspell one, and a misspelt key reads as a
        silently disabled feature rather than as an error."""
        return cls(
            enabled=bool(cfg.get("pacing_enabled")),
            drift_pct=float(cfg.get("pacing_drift_pct") or 0.0),
            drift_cap_s=float(cfg.get("pacing_drift_cap_s") or 0.0),
            think_gaps=bool(cfg.get("pacing_think_gaps")),
        )


# The ceiling on a WHOLE paced bubble — typing time included, not just the drift.
# Derived from rhythm rather than retyped so the two cannot drift apart; rhythm.py
# is pure stdlib, so this adds no import weight and no cycle.
#
# ⚠️ TWO different 120s limits bind here, and the SECOND one is the tight one.
# `rhythm.INLINE_MAX_S` bounds the HOLD. `ai_chatter._BUBBLE_WINDOW` (also 120s)
# bounds the gap between the `created_at` of two consecutive outbound rows — which
# is the hold PLUS the `to_thread(client.send_message)` round-trip, and on the
# price-validation-400 retry path it is the hold plus TWO of them. Past that window
# the bubbles stop being read as ONE reply, so each starts burning its own unit of
# `msg_limits_by_signal` — the regression that once turned 184 replies into 1202
# cadence skips. So the margin is sized for a network round-trip, not float dust.
_TOTAL_CEIL_S = _INLINE_MAX_S - 15.0

# The bound the API puts on the operator's `drift_cap_s` knob (see
# `scripts_api._FLOAT_KNOBS`). It is the SAME number as the internal ceiling and
# must stay that way: when it was larger, the UI advertised a maximum the engine
# could never deliver — a knob whose top third does nothing, which is precisely the
# "number the UI shows and the engine quietly ignores" failure the pace-curve
# bounds already warn about one module over.
MAX_DRIFT_CAP_S = _TOTAL_CEIL_S


@dataclass(frozen=True)
class Pace:
    """What the caller holds, and what the fan sees while it holds.

    ONLY `total_s` moves a clock. `quiet_s` / `think_at_s` / `think_for_s`
    redistribute the indicator inside that same total — see the module docstring's
    INVARIANT. `added_s` is bookkeeping: the part of `total_s` this module added on
    top of the caller's typing time, so the caller can charge it to its inline
    budget without re-deriving it."""
    total_s: float
    quiet_s: float = 0.0
    think_at_s: float = 0.0
    think_for_s: float = 0.0
    added_s: float = 0.0
    drifted: bool = False


def bubble_pace(typing_s: float, text: str, rng: Random,
                cfg: PaceConfig | None = None,
                budget_s: float | None = None) -> Pace:
    """The pace for ONE bubble of a multi-bubble reply (idx >= 1).

    `typing_s` is the caller's existing `typing_delay_seconds(text, wpm)` — the
    only length-correlated term, and it is kept exactly as-is. Everything this
    function adds is drawn, never fixed.

    Disabled (or no config) ⇒ `Pace(total_s=typing_s)`: the same number the caller
    already had, zero quiet time, no think gap. `hold_with_typing` then behaves
    byte-identically to today. No rng is drawn on that path, so turning the
    feature on and off cannot shift any other seeded sequence."""
    typing_s = max(0.0, float(typing_s or 0.0))
    if cfg is None or not cfg.enabled:
        return Pace(total_s=typing_s)

    # ── The garnish. Always drawn (in this fixed order, so a seed replays).
    enter = rng.uniform(*_ENTER_S)
    emoji = rng.uniform(*_EMOJI_S) if has_emoji(text) else 0.0

    # ── The fix. One bubble in ~three genuinely stops.
    drift = 0.0
    cap = min(max(0.0, float(cfg.drift_cap_s)), MAX_DRIFT_CAP_S)
    # ⚠️ The DRIFT cap is not the TOTAL cap, and conflating them is a live bug this
    # test caught: `typing_s` reaches _MAX_TYPING_DELAY_S (60s) on a long bubble, so
    # 60 + garnish + a 90s drift is 157s — past rhythm.INLINE_MAX_S, where the hold
    # outlives the 900s fan lease and burns one of the executor's 4 GLOBAL run slots.
    # Bound what is LEFT after the terms already drawn, so the ceiling holds no
    # matter how long the bubble or how generous the knob.
    cap = min(cap, max(0.0, _TOTAL_CEIL_S - (typing_s + enter + emoji)))
    # `budget_s` is the caller's REMAINING inline-hold budget for this run (see
    # ai_chatter._RUN_INLINE_BUDGET_S — one run() occupies 1 of the executor's 4
    # GLOBAL slots for the SUM of its holds). A drift is the only term here big
    # enough to matter to it, so the budget caps the drift rather than vetoing the
    # bubble: the garnish still lands, the long pause simply doesn't. At 0 budget
    # this collapses to today's behaviour, which is the correct way to run out.
    if budget_s is not None:
        cap = min(cap, max(0.0, float(budget_s)))
    # Draw the roll UNCONDITIONALLY so the rng sequence does not depend on the
    # rate or the budget — an operator moving the slider, or a busy run, must not
    # re-roll every later bubble into a different draw.
    roll = rng.random()
    if cap > 0 and roll < max(0.0, min(100.0, float(cfg.drift_pct))) / 100.0:
        drift = min(rng.lognormvariate(_DRIFT_MU, _DRIFT_SIGMA), cap)

    # She is not typing while she reaches for an emoji, and she is not typing
    # while she is doing something else entirely. Both are QUIET — the indicator
    # stays dark and the fan simply sees nothing, which is what a person looks
    # like when they have put the phone down.
    quiet = emoji + drift
    typing_phase = typing_s + enter
    total = quiet + typing_phase

    # ── The mid-sentence stall. Pure redistribution: it takes a slice OUT of the
    # typing phase rather than adding one, so `total` above is already final and
    # nothing downstream sees a different number because of it.
    think_at = think_for = 0.0
    if cfg.think_gaps and typing_phase >= _THINK_MIN_TYPING_S:
        want = rng.uniform(*_THINK_S)
        think_for = min(want, typing_phase * _THINK_MAX_SHARE)
        # Land it inside the phase, never at either edge: a gap at 0 is just more
        # quiet time, and one at the end is a gap before the send that reads as
        # the same machine pause it is meant to break up.
        span = typing_phase - think_for
        think_at = rng.uniform(span * 0.25, span * 0.75) if span > 0 else 0.0

    return Pace(total_s=total, quiet_s=quiet, think_at_s=think_at,
                think_for_s=think_for, added_s=total - typing_s,
                drifted=drift > 0)


def silent_hold(total_s: float, typing_s: float,
                cfg: PaceConfig | None = None) -> Pace:
    """A hold with nothing being typed inside it, beyond `typing_s`.

    Two callers, one shape. A solo GIF is picked, not typed (`typing_s=0`, so the
    whole hold is quiet). The PPV drop is her waiting on the file while the caption
    is already written (`typing_s` is that caption). Both used to run the "…is
    typing" bar for their entire hold, which is the same false oracle the quiet
    phase exists to remove — and the gif case is its loudest instance, since the
    gif-first opener makes a solo gif the default first reply to every new fan.

    Pacing off ⇒ a plain `Pace`, so the indicator behaves exactly as it always
    did."""
    total_s = max(0.0, float(total_s or 0.0))
    if cfg is None or not cfg.enabled:
        return Pace(total_s=total_s)
    typing_s = min(max(0.0, float(typing_s or 0.0)), total_s)
    return Pace(total_s=total_s, quiet_s=total_s - typing_s)


def hold_for_bubble(*, idx: int, text: str, typing_s: float,
                    rhythm_delay_s: float | None, cfg: PaceConfig | None,
                    rng: Random, budget_s: float | None = None) -> Pace:
    """THE entry point: how long to hold before bubble `idx`, and what the fan
    sees while we hold. One function so the precedence below is a readable list
    in one place instead of a branch tangle inside a 2,000-line send loop.

    `rhythm_delay_s` is `rhythm.decide()`'s number when it owns this bubble, else
    None. Three cases, in order:

    1. RHYTHM OWNS IT (bubble 0, rhythm on). The latency is already decided and
       pacing adds NOTHING — it only carves that one number into (quiet, typing)
       so the bar stops running through the part where she had not yet picked the
       phone up. Clamped so quiet is never negative: rhythm's ceiling can land the
       total BELOW the typing time (`min(delay + typing, ceiling)`), and a negative
       quiet would invert the phases and show the bar before the hold.

    2. PACING OWNS IT (bubble 1+). The gaps rhythm never touched — see the module
       docstring for why they are the actual tell.

    3. NOBODY OWNS IT. Bubble 0 with no rhythm delay, or pacing off ⇒ the plain
       wpm typing time, byte-identically what shipped before this module existed.

    ⚠️ Case 3 is why `idx` is a parameter rather than something the caller resolves
    into a boolean. `rhythm_delay_s` is None on TWO paths that both arrive with
    idx == 0 — rhythm off, and the rhythm_resume WAKE — so "not case 1" is NOT the
    same set as "bubble 1+". Conflating them let bubble 0 of a resumed reply draw a
    pause ON TOP of the scheduled wake it had already served: the fan sat through
    rhythm's break and then up to another ~100s of silence before the cover line
    ("sorry babe was in the shower") landed. `rhythm_enabled` ships True, so that
    was every resumed reply on every account with pacing on."""
    typing_s = max(0.0, float(typing_s or 0.0))

    if rhythm_delay_s is not None:                                   # 1
        total = max(0.0, float(rhythm_delay_s))
        if cfg is None or not cfg.enabled:
            return Pace(total_s=total)
        return Pace(total_s=total, quiet_s=total - min(typing_s, total))

    if idx >= 1 and cfg is not None and cfg.enabled:                 # 2
        return bubble_pace(typing_s, text, rng, cfg, budget_s=budget_s)

    return Pace(total_s=typing_s)                                    # 3
