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

import math as _math
from dataclasses import dataclass
from random import Random

from ._common import has_emoji
from .rhythm import INLINE_MAX_S as _INLINE_MAX_S
# The picture-back floor is rhythm's MEASURED one, imported rather than restated:
# two copies of "25" would drift, and the number is the whole justification for
# the floor (see `picture_back_target`).
from .rhythm import _FLOOR_S as _RHYTHM_FLOOR_S

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


def _think_gap(typing_phase: float, rng: Random, cfg: PaceConfig) -> tuple[float, float]:
    """`(think_at_s, think_for_s)` — the one mid-sentence stall, or (0, 0).

    Pure redistribution: the caller has already fixed its total, and this only
    decides where inside the TYPING phase the bar goes dark. Landed strictly
    inside the phase, never at either edge — a gap at offset 0 is just more quiet
    time, and one at the end is a pause before the send, which reads as exactly
    the machine beat it exists to break up.

    Shared by `bubble_pace` and `welcome_burst_pace` so the two cannot drift into
    two different notions of "she stopped typing for a second". The draw order
    (duration, then offset) is part of the contract: it is what makes a seeded
    Pace replay."""
    if not cfg.think_gaps or typing_phase < _THINK_MIN_TYPING_S:
        return 0.0, 0.0
    want = rng.uniform(*_THINK_S)
    think_for = min(want, typing_phase * _THINK_MAX_SHARE)
    span = typing_phase - think_for
    think_at = rng.uniform(span * 0.25, span * 0.75) if span > 0 else 0.0
    return think_at, think_for


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
    think_at, think_for = _think_gap(typing_phase, rng, cfg)

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


# ── The welcome burst (send_welcome's second consumer of this module) ────────
#
# A welcome is not a reply, so `hold_for_bubble`'s three-case precedence does not
# describe it: there is no inbound message rhythm answers, nothing was read before
# bubble 0, and the burst's SHAPE is fixed (greeting[+image] → the line → the
# operator's question → the GIF) rather than however many parts a draft split into.
# What carries over is the finding itself — dispersion not delay, and phases where
# the "…is typing" bar is honestly dark because she is not typing.
#
# ⚠️ HONESTY: these numbers are DRAWN, not measured. This module's table is 120 days
# of ESTABLISHED-thread reply gaps; no welcome burst has ever been measured. They are
# deliberately SHORTER than the operator's 30s/20s illustration for exactly that
# reason. An account that wants his numbers sets `pace_open_quiet_s` /
# `pace_gap_quiet_s` on the rule payload — one JSON edit, no deploy.
_WELCOME_PACE = PaceConfig(enabled=True, drift_pct=35.0, drift_cap_s=20.0,
                           think_gaps=True)
_WELCOME_GIF_S = (2.0, 6.0)          # a GIF is PICKED, not typed: the hold is all quiet
_WELCOME_OPEN_QUIET_S = (6.0, 18.0)  # before the greeting — only when an image is attempted
_WELCOME_GAP_QUIET_S = (10.0, 25.0)  # before the line that follows the greeting
# The bound on an OPERATOR-supplied range, applied before the total ceiling applies
# its own. 45s of dark screen is already past anything the measured table justifies;
# past that a fan is watching nothing happen on his very first message.
WELCOME_QUIET_MAX_S = 45.0


def _welcome_quiet(rng: Random, override: object,
                   default: tuple[float, float]) -> float:
    """One quiet draw from `override` if it is a usable [lo, hi], else `default`.

    The override rides the RULE PAYLOAD (raw JSON, no typed editor), so it arrives
    as whatever the operator typed. A malformed one falls back to the default
    rather than raising: a bad number on a rule must never cost a fan his welcome."""
    lo, hi = default
    if override is not None:
        try:
            lo, hi = float(override[0]), float(override[1])
        except (TypeError, ValueError, IndexError, KeyError):
            lo, hi = default
        else:
            lo = min(max(0.0, lo), WELCOME_QUIET_MAX_S)
            hi = min(max(0.0, hi), WELCOME_QUIET_MAX_S)
            if hi < lo:
                lo, hi = hi, lo
    return rng.uniform(lo, hi)


# The parts a welcome burst is made of, in send order. A bubble's ROLE, not its
# position, is what decides its rhythm — a burst is variable-length, so position
# decides nothing.
#
# ⚠️ THIS MODULE OWNS THE NAMES, and `send_welcome` imports them (it already
# imports `welcome_burst_pace` from here; nothing here imports it, so this is the
# only direction that can hold). They used to exist in four places: `WELCOME_ROLES`
# here, imported by nobody; `send_welcome._ROLE_*`, which is what actually crosses
# the boundary; bare literals in `welcome_burst_pace` below; and two more in
# BrainPanel. Renaming a role in the sender therefore did not break anything —
# it silently degraded that role to `bubble_pace`, the documented "unknown role is
# paced as tail" fallback, on a live send path. The fallback is a sound decision;
# the defect was that a TYPO and a DELIBERATE DEFAULT were indistinguishable.
ROLE_OPENER = "opener"   # the greeting, and the only bubble that carries an image
ROLE_GAP = "gap"         # the time / activity line after it — the optional one
ROLE_TAIL = "tail"       # the operator's question, appended word-for-word
ROLE_GIF = "gif"         # the text-less giphy bubble (never in `bubbles`)
WELCOME_ROLES = (ROLE_OPENER, ROLE_GAP, ROLE_TAIL, ROLE_GIF)


def welcome_burst_pace(*, role: str, has_image: bool,
                       typing_s: float, text: str, rng: Random,
                       open_quiet: tuple[float, float] | None = None,
                       gap_quiet: tuple[float, float] | None = None) -> Pace:
    """How long send_welcome holds before a bubble, and what the fan sees.

    Pure and seeded exactly like the rest of this module: the caller passes its own
    `Random` (seeded per bubble — `welcome_pace:{account}:{fan}:{idx}`), so a burst
    replays and a test is not flaky. There is NO env check in here; the caller
    decides whether the feature is on, and when it is off this function is never
    called at all, so nothing is drawn.

    ⚠️ IT DISPATCHES ON `role`, NOT ON THE BUBBLE'S INDEX, and that is a bug fix
    rather than a preference. The burst is VARIABLE-LENGTH: the middle bubble
    disappears when `skip_time_bubble` is on AND when `_activity_bubble` returns
    nothing for an unfilled slot (a live shape — `test_send_welcome`'s blank-slot
    pin case). In both of those the operator's QUESTION slides into position 1, and
    an index-dispatching sampler silently paid it the long U(10,25) "gap before the
    second line" instead of the short beat it is. Nobody decided that; it just fell
    out of the arithmetic. The caller knows each bubble's role while it is
    composing them, so it says so.

    The four roles, all drawn, in this fixed order:

      "gif"     fully quiet U(2,6)s. She picked it, she did not type it, and there
                is no indicator to run — the same seeded sticker beat ai_chatter
                uses. Replaces send_welcome's flat 3.0s.
      "opener"  the greeting. Quiet U(6,18)s ONLY when an image is ATTEMPTED
                (`has_image`) — she was choosing a picture — then the greeting's
                own typing time with the bar on. No image ⇒ no quiet at all,
                which is today's hold exactly.
      "gap"     the time/activity line after the greeting: quiet U(10,25)s, then
                an enter press and the typing time, with a think-gap allowed
                inside the typing phase. This is the bubble the burst may not
                have at all.
      "tail"    the operator's question and anything after it — an ordinary
                `bubble_pace` under `_WELCOME_PACE`. Usually a few seconds.

    An unknown role is paced as "tail": the conservative end (a few seconds, no
    long dark gap), because this is a live send path and the alternative to a
    sane fallback is a fan staring at nothing over a typo.

    ⚠️ `Pace.total_s` is still the ONLY number that moves a clock (see this
    module's INVARIANT). Quiet shrinks first so no hold exceeds `_TOTAL_CEIL_S`;
    the worst default single hold is 25 + 1.2 + 60 = 86.2s, and the clamped worst
    is 105s — both under `rhythm.INLINE_MAX_S`."""
    typing_s = max(0.0, float(typing_s or 0.0))

    if role == ROLE_GIF:
        total = min(rng.uniform(*_WELCOME_GIF_S), _TOTAL_CEIL_S)
        return Pace(total_s=total, quiet_s=total, added_s=total - typing_s)

    if role == ROLE_OPENER:
        # No draw at all without an image — the off-by-shape path stays byte-clean.
        quiet = (_welcome_quiet(rng, open_quiet, _WELCOME_OPEN_QUIET_S)
                 if has_image else 0.0)
        quiet = min(quiet, max(0.0, _TOTAL_CEIL_S - typing_s))
        return Pace(total_s=quiet + typing_s, quiet_s=quiet, added_s=quiet)

    if role == ROLE_GAP:
        quiet = _welcome_quiet(rng, gap_quiet, _WELCOME_GAP_QUIET_S)
        enter = rng.uniform(*_ENTER_S)
        typing_phase = typing_s + enter
        quiet = min(quiet, max(0.0, _TOTAL_CEIL_S - typing_phase))
        total = quiet + typing_phase
        think_at, think_for = _think_gap(typing_phase, rng, _WELCOME_PACE)
        return Pace(total_s=total, quiet_s=quiet, think_at_s=think_at,
                    think_for_s=think_for, added_s=total - typing_s)

    return bubble_pace(typing_s, text, rng, _WELCOME_PACE)


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


# ── The picture-back pause (tip_reward's image_reply lane) ───────────────────
#
# A third consumer, and the only one that returns SECONDS rather than a `Pace`:
# nothing is held while it elapses. He sends a photo; the reply is a PICTURE, and
# a picture is not typed — she is in her camera roll. So the whole pause is dark
# (there is no honest indicator to run), which means no process needs to be awake
# for it: `webhook_dispatch` defers the job's `run_at` and the executor picks it up
# when it comes due. A `Pace` would be a lie about the shape — see
# plans/image-reply/PLAN.md §D3.
#
# ⚠️ HONESTY, same as the welcome burst: these numbers are DRAWN, not measured. The
# one measured number in here is the FLOOR — `rhythm._FLOOR_S`, 25s, off the table
# in that module's docstring: 1:1 human replies under 25s are 1.4% of the corpus.
# Everything above it is a shape chosen to satisfy this module's own finding
# (DISPERSION, NOT DELAY — see the header): a flat pause is measurably worse than
# no pause at all, so the base is uniform and one draw in ~three carries a real
# lognormal "went looking through her camera roll" tail.
#
# THE MEASUREMENT THIS MAKES POSSIBLE, once it has run for a week: the `image_reply`
# row's `created_at` minus the trigger message's, i.e. `landed_after_s` on the
# `automation_runs` row. Compare its distribution to the FAN in→in column of this
# module's table before touching a constant.
_PICBACK_FLOOR_S = _RHYTHM_FLOOR_S   # 25s — the one measured number in here
_PICBACK_BASE_S = (5.0, 20.0)        # over the floor: reading it, deciding to answer
_PICBACK_DRIFT_P = 0.3               # one in ~three goes looking for the right one
_PICBACK_DRIFT_MU = 20.0             # median of that hunt, seconds (lognormal)
_PICBACK_DRIFT_SIGMA = 0.7
_PICBACK_DRIFT_CAP_S = 45.0          # so floor + base + drift can never exceed…
PICBACK_CEIL_S = 90.0                # …this. A pic later than this is not a reply.


def picture_back_target(rng: Random) -> float:
    """Seconds from HIS photo to HER picture landing. Not a hold — a target.

    ⚠️ Read that again: the number is measured from the TRIGGER, not from now. The
    caller subtracts the time already spent (the vision describe, the queue) so the
    describe is counted INSIDE this target rather than added on top of it — the
    fan experiences one number, and it is this one. `webhook_dispatch._deferred_run_at`
    is where that arithmetic lives.

    Pure and seeded like everything else in this module: the caller passes its own
    `Random` (seeded `picback:{account}:{fan}:{message_id}`), so a replay of the
    same photo draws the same target and a test can assert a distribution instead
    of a range.

    The draw, in this FIXED order so a seed replays even if a term is later
    re-tuned:

      floor 25s      `rhythm._FLOOR_S`. The measured one. A reaction faster than
                     this is where 1.4% of humans live.
      + U(5, 20)s    she read it and decided to answer.
      + a drift      on `p=0.3` only: lognormal(median 20s, sigma 0.7), capped at
                     45s. This is the whole point — the term that makes the output
                     a DISTRIBUTION rather than "25 to 45 seconds, always". A flat
                     pause moves every gap into one band and measures WORSE than no
                     pause (this module's header, 120 days of production).

    Result: floor 25s, p50 ~38s, a real tail, hard ceiling 90s."""
    total = _PICBACK_FLOOR_S + rng.uniform(*_PICBACK_BASE_S)
    # Drawn UNCONDITIONALLY (like `bubble_pace`'s roll) so re-tuning the rate does
    # not re-roll every later draw off the same seed.
    roll = rng.random()
    drift = min(rng.lognormvariate(_math.log(_PICBACK_DRIFT_MU), _PICBACK_DRIFT_SIGMA),
                _PICBACK_DRIFT_CAP_S)
    if roll < _PICBACK_DRIFT_P:
        total += drift
    return min(total, PICBACK_CEIL_S)
