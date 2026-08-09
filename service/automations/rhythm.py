"""
service/automations/rhythm.py — Human Rhythm: how long she waits before replying.

Today the ONLY delay before a bubble is `typing_delay_seconds(text, wpm)` — a pure
function of message length (~38 wpm). Every reply therefore lands in a few seconds,
with no variance and no gaps, which is the single loudest bot tell in the product.

Real chatters (measured on this account's own archive: inbound → human reply gap)
are BIMODAL: 37% under 60s, median 106s, and then a genuine tail — 4.2% at 1-4h,
2.4% at 4-8h, 4.4% at 8-24h. Two behaviours, not one:

  • inside a live scene she is FAST (she's on her phone, he's typing back),
  • outside one she is a person with a life, and sometimes she is simply gone.

So this module models exactly two things — a per-ACCOUNT availability calendar
(she sleeps on the creator's clock) and a contextual delay sampler. It deliberately
does NOT reproduce the 4-24h tail: those are the replies where the sale was already
lost. `max_delay_seconds` caps at 4h.

Design constraints that shaped this (each one is a real bug avoided):

  • SCHEDULER, NEVER SLEEPER. A long delay is not `await asyncio.sleep(3h)`. The
    fan lease TTL is 900s and the executor has only _MAX_CONCURRENT_RUNS=4 GLOBAL
    slots across every account on the box — an inline hold would expire the lease,
    burn a slot, and starve `to_thread` (→ relay 500s). `decide()` returns a
    `wake_at`; the caller releases the lease and enqueues a job. Deferral is capped
    at ONE hop per decision (`deferrals`) so a re-rolled sample cannot livelock a
    fan into never being answered.
  • SLEEP IS A PROPERTY OF THE GIRL, not of a fan. It is decided once per account
    per local night — otherwise she is asleep for one fan and awake for another in
    the same minute, which is worse than no model at all.
  • NEVER STRAND A LADDER ACROSS A BREAK. A hot ladder abandoned at 01:58 that
    resumes at 09:00 with "goodmorning baby" followed by a $79 rung is a disaster.
    Break rolls are suppressed while an offer is open/hot, and a ladder is never
    opened within LADDER_GRACE_M of a sleep boundary.
  • PURE + SEEDED. `now` and `rng` are injected; same seed ⇒ same sequence. No
    wall-clock, no global random, so the tests are not flaky.

Ships DISABLED (`rhythm.enabled = false`). Off ⇒ the delay is byte-identically
`typing_delay_seconds()` and rhythm_state is never written.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from random import Random

log = logging.getLogger("of-relay.automation.rhythm")

# ── Hard constants. Deliberately NOT operator knobs: each one is a safety bound,
# and an operator has no basis on which to answer any of them.
INLINE_MAX_S = 120          # longer than this ⇒ release the lease + schedule a wake
MAX_DELAY_S = 6 * 3600      # the break tail reaches ~4h; cap the roll at 6h
LADDER_GRACE_M = 30         # never open a ladder inside this of a sleep boundary
COVER_MIN_GAP_S = 2 * 3600  # stop apologising for 20-min gaps — that IS the tell.
                            # Only 32 latency-excuse lines exist in the whole paying
                            # archive against ~993 replies that took >2h.
COVER_REPEAT_DAYS = 7       # never reuse the same cover line on a fan within a week
COVER_ROLL = 0.10           # even past the gap, only ~1 in 10 gets a spoken excuse
# …but that 1-in-10 is calibrated on the gaps the archive is actually MADE of: 2-6h,
# a woman who got busy for an evening. Vanishing for a DAY and strolling back with a
# breezy "hey babe" is a different event, and reads as a bot with no object permanence
# — the same reasoning that rejected silence-instead-of-cover, one order of magnitude
# up. Long absences are now mostly acknowledged. (Judgement call, not an archive rate:
# multi-day gaps are too rare in the corpus to fit a number to. Deliberately not 1.0 —
# a woman who explains herself EVERY time is its own tell.)
COVER_ROLL_LONG = 0.60      # ≥12h away
COVER_ROLL_VERY_LONG = 0.85 # ≥24h away
_COVER_LONG_GAP_S = 12 * 3600
_COVER_VERY_LONG_GAP_S = 24 * 3600


def _cover_roll_for(gap_s: float) -> float:
    """How likely she is to SAY something about a gap of this size."""
    if gap_s >= _COVER_VERY_LONG_GAP_S:
        return COVER_ROLL_VERY_LONG
    if gap_s >= _COVER_LONG_GAP_S:
        return COVER_ROLL_LONG
    return COVER_ROLL

# ── The reply curve — HEAT-DRIVEN (v3). Reply speed is NOT one fixed random: it tracks
# the temperature of THIS conversation. Measured on prod (real human 1:1, 2,107 replies):
#   SEXTING (a PPV went out < 20min ago): median 78s, p90 459s — ~every minute, 68% <2min
#   CHAT    (no recent PPV):              median 374s, p90 7421s — 5x slower, long tail
# i.e. sexting is 4.8x faster than chat. So the distribution INTERPOLATES between a hot
# pole (fast, tight, no break) and a cold pole (slow, wide, breaks) by a per-fan `heat`
# score. Hot ⇒ both reply fast; boring/no-sale ⇒ she drifts and takes breaks. This is the
# operator's "random to the chat that's currently going," and it is what the data shows —
# unlike the naive per-fan mirror, which was a diurnal confound (kept only as a whisper).
# Poles refit to the CLEANER within-fan split (an unpaid offer open <60min ago vs no
# priced offer in 60min): median 124s with an offer open, 415s without — and it holds
# INSIDE 80% of individual fans, so it's a real per-thread heat effect, not a fan mix.
_HOT_MU, _HOT_SIGMA = math.log(124), 1.35    # offer-open pole (measured within-fan)
_COLD_MU, _COLD_SIGMA = math.log(415), 2.05  # no-offer pole (measured; wide = break tail)
_FLOOR_S = 25               # human 1:1 replies under 25s are 1.4% of the distribution;
                            # an 8s floor lived where almost no human does. Also the
                            # single clearest "are you a bot" pacing tell.
_ASK_OPEN_CEIL_S = 1200     # while an ask is LIVE she slows when he slows but NEVER
                            # vanishes: hard 20-min ceiling, and no break roll at all.
_FREE_CHAT_CEIL_S = 1800    # ordinary banter may stretch to 30 min before the tail.
_P_BREAK_COLD = 0.12        # a fully COLD/boring free-chat turn: ~12% she's just gone.
                            # Scales with coldness — a hot scene never breaks (see below).
_BREAK_MU, _BREAK_SIGMA, _BREAK_CLAMP = math.log(7500), 1.1, (30 * 60, 6 * 3600)
# NO-SLEEP mode (`RhythmCtx.no_sleep`): she never takes the long overnight break and
# never needs a timezone — she just "steps away" for a bit (yoga, an errand, busy).
# Same COLD-scene trigger, a much shorter tail: ~40 min typical, 2 h hard cap, so a
# fan is never ghosted for 8 hours. This is the operator's "hot/cold/busy, no sleep."
_BREAK_MU_SHORT, _BREAK_SIGMA_SHORT = math.log(2400), 0.9   # ~40 min median
_BREAK_CLAMP_SHORT = (10 * 60, 2 * 3600)                    # 10 min – 2 h, never a night

# Cosmetic mirror only. The strong "she mirrors his pace" signal was REFUTED as a
# diurnal/liveness confound: under within-fan + within-hour demeaning the causal
# forward arrow (0.197) was matched exactly by an ACAUSAL backward-arrow placebo
# (0.196) — i.e. it's just both parties being awake in the same hour. We keep a
# whisper (beta 0.12) purely so a fan who fires back in 8s doesn't get a robotic
# fixed cadence; we do NOT calibrate on the confounded number. A mandatory sim test
# fails the build if the placebo beta >= the forward beta.
_MIRROR_BETA = 0.12
_MIRROR_CLAMP = (0.7, 1.6)
_FAN_MEDIAN_LATENCY_S = 110.0   # his median: mirror_mult == 1.0 here

# HE CAME BACK. A fan who has been gone half an hour and gets an answer in eight
# seconds has been told she was sitting on her phone waiting for him — the single
# moment where an instant reply is least believable, and unlike the refuted pace
# MIRROR this is not a coefficient read off his latency: it is a bound on one rare,
# nameable event, so the diurnal confound that killed `mirror_mult` does not apply.
# (Measured over 14 days: 417 of these turns were answered inside 2 minutes.)
_RETURN_GAP_S = 28 * 60         # "he's been away a while", not "he paused mid-chat"
_RETURN_BAND = (120.0, 420.0)   # 2-7 min. A BAND, not a floor — see decide().

# ── SHE STEPS OUT. Every N exchanges of a cold chat she is simply gone for an hour
# or two, and comes back when she comes back — or sooner, if he writes again.
#
# This is deliberately NOT the break roll below. The break roll is a per-turn 12%
# coin flip that refuses while an answer is owed; this is a COUNTER, it fires on a
# schedule the fan cannot predict from any single turn, and it fires whether or not
# he is owed a reply. That last part is the risky half and it is on purpose: a
# silence that only ever lands on turns she was not going to take is an elaborate
# no-op (the same reasoning as the ghost cycle's owed-answer exemption). The
# persistence exit below is what protects the fan who actually needed an answer.
#
# 1-2h is chosen against the measured human curve, not picked round: the bot's
# structural deficit versus a human chatter is the >=1h region (human ~7% of replies
# against 0.28-1.75% for us, and it is the one gap that holds on 5 of 6 accounts).
# The 6-24min region is a hole too, but it is bounded above by the cross-lane leak
# (autoreply intercepts ~33% at 30min) and below by _RETURN_BAND. An hour clears both.
#
# Only the PAUSE lives here. The schedule (how many exchanges between step-outs) and
# the exit predicate (has he written again) are in `_stepout.py`, which shares
# nothing with this module — no ctx, no rng, no clock.
# Authored in MINUTES because that is the unit the config stores and the operator
# types; the seconds every caller here actually wants are DERIVED, so the conversion
# happens once, at the definition, instead of at each of the three places that used
# to do their own `* 60` / `// 60`.
STEPOUT_MIN_MINUTES = 60
STEPOUT_MAX_MINUTES = 120
STEPOUT_MIN_S = STEPOUT_MIN_MINUTES * 60
STEPOUT_MAX_S = STEPOUT_MAX_MINUTES * 60

# Soft fast-reply nudge (the hard >35%-share floor was REFUTED — non-monotonic, top
# bucket empty). If she's been replying too fast too often, floor the NEXT draw at 60s.
_FAST_REPLY_S = 30
_FAST_REPLY_WINDOW = 20
_FAST_REPLY_TRIGGER = 5     # >=5 of the last 20 realized replies under 30s ⇒ nudge
_FAST_NUDGE_FLOOR_S = 60

# ── Opening speed. Her reply gap on a fan's EARLY turns (his 2nd-15th inbound)
# predicts whether he ever speaks again, and the effect is steepest on exactly the
# fans worth keeping. Measured 2026-07-26 over 25,693 early turns, split by how fast
# HE had just replied:
#     his last reply   gap <10min   10-30min   30-120min   >2h
#     HOT   (<2min)      92.7%       81.7%      72.2%     57.2%   (n=10002 / 208)
#     WARM (2-10min)     89.9%       80.1%      83.3%     65.4%
#     COLD (>10min)      82.5%       77.1%      70.4%     60.5%   (n=4374 / 2329)
# The gradient survives inside every engagement stratum, so it is not merely "cold
# threads die" — and a HOT fan loses 35.5 points to a >2h gap against a COLD fan's
# 22. Attention is cheapest to spend and most expensive to withhold at the start.
# (Observational, and his-latency is a coarse heat proxy — but the direction holds in
# all three strata, which selection alone does not explain. The one non-monotonic
# cell, WARM 30-120min at 83.3%, is n=258 and inside the noise.)
# THE OPENING — his first _OPENING_TURNS inbounds, answered on a STATIC schedule.
#
# Static and not a draw, because a fan meets his opening exactly once: he has nothing
# to compare it against, so per-fan variance is invisible to him. (The earlier version
# drew from a pole and justified it as "the first replies vary the way a person's do" —
# that describes a sequence he never sees.) Heat cannot do this job either: it only
# interpolates toward _HOT_MU, median 124s, which puts most opening replies over
# INLINE_MAX_S (120s) — they would defer through the scheduler, spend the fan's single
# deferral hop, and inherit the wake_at poll's latency on the one reply that must not
# be late.
#
# TWO values, not one, and they must differ. Turn 2 reaches this schedule only when
# turn 1 produced no gif — i.e. he opened with a question — and a single constant would
# then hand him two identical gaps back to back, which is the one repeat he CAN see. In
# the ordinary gif-first flow turn 2 is the beat, so he only ever meets the first value.
#
# 60s: fast enough for the continuation curve (92.7% at <2min), far enough above
# _FLOOR_S that it never reads as a machine. Note this targets TOTAL inbound→reply
# latency, so the realised gap is max(60, poll_lag + _FLOOR_S) — "60 seconds, unless
# the poll was already slower than that".
_OPENING_S = (60.0, 100.0)
_OPENING_TURNS = len(_OPENING_S)   # derived: one source of truth for "how many"

# THE WARM-UP — the first _WARMUP_S of the conversation. Covers what the opening
# cannot: turn 3 was still free to draw the ordinary cold reply, which put a 30-minute
# silence eight minutes into a brand new thread. A bigger turn count does not fix that
# (it makes the first four replies fast and leaves the fifth free to vanish); the ask
# was bounded by the CLOCK — "no 30, never, for the next 20 minutes". So inside the
# window no break may roll and no single reply may exceed _WARMUP_CEIL_S. Measured
# from HIS FIRST inbound, not from the welcome: the conversation starts when he
# answers, and an old fan re-engaging months later is not in a warm-up.
#
# The opening and the warm-up OVERLAP but neither contains the other, so they stay two
# predicates rather than one phase enum: `turn_index` can still be 1 an hour into a
# thread (he answered, then went quiet, then answered again), and the warm-up covers
# turn 9 if he is chatty. Collapsing them would silently drop one of those cases.
_WARMUP_S = 20 * 60

# THE BEAT — the rest after a solo gif. A gif closes no loop and opens none, so
# nothing is owed in either direction and drifting off reads as her attention
# wandering rather than as her ignoring him. The two 07-26 test subjects are the same
# length of silence with opposite outcomes, and what differed was whether an answer
# was owed. One fan answered a gif with a throwaway line, waited ~40min, came back
# with a gif of his own and then bought. Another volunteered real content, waited
# ~30min, and collapsed to one word before going silent. Hence the beat is gated on
# `answer_owed` and NEVER on a turn counter — a positional script would have fired
# straight into the second fan's most engaged moment.
#
# The beat is its OWN DRAW, exactly like the opening — NOT a floor-and-cap laid over
# the ordinary cold reply. That was the first implementation and it was quietly broken:
# the cold pole's median (~340s) and long tail sit mostly OUTSIDE the beat's window, so
# most draws were pinned to whichever bound they crossed. Measured over 2,000 seeds,
# 51.4% of opening beats came out at EXACTLY 300.000s and 39.1% of later ones at
# EXACTLY 420.000s — a fixed inter-message interval repeating across every new fan on
# every account, which is the most machine-detectable thing this design could possibly
# emit, and precisely what the spread was supposed to prevent. Percentile summaries hid
# it completely; only a distinct-value count showed the point mass.
#
# Triangular rather than lognormal because a triangular draw cannot produce boundary
# mass: it is generated inside the range instead of being clamped into it. Mode at five
# minutes because that is the number the operator asked for twice.
_BEAT_S = (240.0, 420.0)     # 4-7 min, and the draw never touches either end
_BEAT_MODE_S = 300.0         # 5 min

# Derived, not typed twice: the warm-up must not clamp tighter than the beat's own top
# or the two would fight and the beat would be silently truncated inside the window.
_WARMUP_CEIL_S = max(480, _BEAT_S[1])

# A fan who spoke this recently is mid-conversation (used only for context labelling
# now that both live contexts share one draw).
SCENE_TTL_S = 12 * 60

# The HOUSE NIGHT, on the creator's own clock. Operator ruling 2026-08-04: a
# four-hour pause, 02:00-06:00. NOT "always awake" — a girl who never sleeps is a
# bot, and the default has to be safe in the absence of evidence.
#
# It was ("03:00", "10:00") — seven hours, chosen as a conservative stand-in when
# the only alternative was deriving one. Two things make four the better default
# now: an OF creator who is dark from 3am to 10am misses the whole US morning, and
# a shorter night is the one that fails cheaply — an account that should sleep
# longer loses a little realism, an account that sleeps too long loses replies.
DEFAULT_SLEEP = ("02:00", "06:00")

CONTEXT_ASK_OPEN = "ask_open"      # an unpaid rung is live, or he paid < 60min ago
CONTEXT_FREE_CHAT = "free_chat"    # banter / sext, no open ask
CONTEXT_UNAVAILABLE = "unavailable"
# She stepped out (see STEPOUT_MIN_S). A SEPARATE context from `unavailable` on
# purpose: `unavailable` is written for both the sleep window and the break roll and
# cannot tell them apart, so the candidate gate has no way to ask "is this the silence
# the fan is allowed to interrupt?". Only this one may be ended early by persistence.
CONTEXT_STEPOUT = "stepout"
# Back-compat aliases (older callers / tests referenced these names).
CONTEXT_SCENE = CONTEXT_ASK_OPEN
CONTEXT_ENGAGED = CONTEXT_FREE_CHAT

# ── Cover lines: verbatim register from the corpus. Fired only after a gap a human
# would notice (>=20min). Silence-instead-of-cover was considered and rejected: a
# six-hour silence followed by a cold "hey babe" reads MORE like a bot, not less.
COVER_BUSY = [
    "sorry babe was in the shower 🚿",
    "ugh work was insane today, im all yours now",
    "sorry i was driving 🙈",
    "my sister called and would NOT shut up lol",
    "sorry baby i was making dinner 🥺",
]
COVER_ASLEEP = [
    "omg i fell asleep on you 🙈 morning baby",
    "sorry i passed out last night, i was so tired",
    "just woke up n ur the first thing i thought about 🥰",
]
COVER_LONG = [
    "ok im back and i missed you 😩",
    "i hate that i left you hanging, come here",
]
# Sells the illusion she is filming it RIGHT NOW. Corpus-universal before a PPV.
PRE_PPV_STALL = [
    "ok give me two secs im filming it rn 🎥",
    "hold on... im getting it ready for you 😈",
    "one sec baby, setting my phone up",
]

# ── The MALE pools ───────────────────────────────────────────────────────────
# This module was the LAST blocker on running ai_chatter for a male creator, and
# the nastiest kind: `ai_chatter._DEFAULTS` has `"rhythm_enabled": True`, so
# switching the engine on switched these on too. `_pick_cover` prepends the line
# as its own bubble with only `apply_word_restriction` between it and the fan —
# no model, no voice check — so "sorry babe was in the shower 🚿" was one config
# tick away from a 38-year-old combat-sports coach.
#
# Hers apologise for the gap and offer domestic reasons (shower, dinner, sister
# called). His do not apologise for working: he was training, with a client, or
# he simply left the phone face down. COVER_BUSY[4] is the one that carries the
# register — he saw it and chose not to answer yet, which is the male equivalent
# of her missing him and should not be softened.
#
# Deliberately spread across five DIFFERENT excuses. The first draft put three of
# five in a gym; with COVER_REPEAT_DAYS=7 a fan sees these rarely, but a pool that
# is all one theme reads as a script the moment he sees the second one.
COVER_BUSY_HIM = [
    "was on the mats. phone was in my bag",
    "clients back to back all afternoon",
    "was driving",
    "just got in, straight in the shower 🚿",
    "phone was face down all evening. on purpose",
]
COVER_ASLEEP_HIM = [
    "i was out cold. training day",
    "im up at 5 for a session so i went to bed early",
    "morning. your message was sitting there when i woke up",
]
COVER_LONG_HIM = [
    "im back. come here",
    "camp week. i saw your messages, i just didnt answer them yet",
]
# Each keeps a "you" — the stall only sells if he is doing it FOR him — and keeps
# the wait to seconds. "two minutes" hands the fan permission to leave the app.
# ⚠️ PRE_PPV_STALL has NO caller anywhere in the tree (verified 2026-08-03). Laned
# regardless: an unused female-only constant is exactly what someone wires up
# later without noticing it only has one voice.
PRE_PPV_STALL_HIM = [
    "two secs, filming it for you now 🎥",
    "hold on. setting the camera up",
    "stay there. nearly ready for you 😈",
]

# kind -> (hers, his). `_pick_cover` takes the KIND, not the list, so a call site
# can't reach past the voice by naming a pool directly.
_COVER_POOLS: dict[str, tuple[list[str], list[str]]] = {
    "busy": (COVER_BUSY, COVER_BUSY_HIM),
    "asleep": (COVER_ASLEEP, COVER_ASLEEP_HIM),
    "long": (COVER_LONG, COVER_LONG_HIM),
    "pre_ppv": (PRE_PPV_STALL, PRE_PPV_STALL_HIM),
}


@dataclass(frozen=True)
class RhythmCtx:
    """Everything the sampler needs. Assembled by the caller (ai_chatter) so this
    module stays pure and trivially testable."""
    account_id: str
    fan_id: int
    text: str = ""
    typing_delay_s: float = 0.0          # the existing wpm delay — always added on
    last_inbound_at: datetime | None = None
    last_outbound_at: datetime | None = None
    ladder_open: bool = False            # an unpaid rung is live ⇒ ask_open, no break
    last_paid_at: datetime | None = None  # paid < 60min ago also counts as ask_open
    his_last_latency_s: float | None = None  # how fast HE replied last (feeds HEAT)
    fan_hot: bool = False                # he's escalating / asking for content right now
    recent_realized_s: tuple[float, ...] = ()  # last ~20 realized reply latencies (nudge)
    sleep_window: tuple[str, str] = DEFAULT_SLEEP
    tz_offset_minutes: int | None = None  # creator-local offset from UTC
    no_sleep: bool = False               # skip the overnight sleep; only short breaks
    last_cover_at: datetime | None = None
    # Which cover-line pool. Default "her" keeps every existing caller and test
    # byte-identical; ai_chatter passes voice_blocks.voice. A cover line is sent
    # VERBATIM with no model in the loop, so this is the only thing standing
    # between a male creator and "sorry babe was in the shower".
    voice: str = "her"
    # TODAY's cover lines, drawn from the same generated day the chat prompt is
    # reading (automations/_daylog.covers_for). () ⇒ the shipped pools below, which
    # is every existing account and every test, byte-identical.
    #
    # ⚠️ This exists because a cover line is sent VERBATIM with no model in the loop
    # and no consistency check, and COVER_BUSY makes CLAIMS ABOUT HER DAY — "ugh work
    # was insane today", "sorry i was driving", "sorry babe was in the shower". The
    # moment the chat prompt also carries a day ("just got back from a little hike"),
    # those are two independent sources of what she was doing, they cannot be
    # reconciled after the fact, and the fan sees both. So when she has a real day,
    # the covers come from it: one row, one story.
    day_covers: tuple[str, ...] = ()
    # Sample ordinary reply latency from PACE_BUCKETS instead of the archive
    # lognormal. Default False so every existing account keeps its measured
    # curve — see the PACE_BUCKETS note.
    pace_buckets: bool = False
    # The operator's own bands, already parsed by `parse_pace_curve`. None ⇒ the
    # shipped PACE_BUCKETS. Only consulted when `pace_buckets` is on, so an account
    # that authored a curve and then turned the mode off keeps its measured
    # lognormal rather than half of each.
    pace_curve: tuple[tuple[float, float, float], ...] | None = None
    # Is an ANSWER OWED on this turn? True when his last inbound asked something or
    # volunteered real content (`_common.is_qualifying_inbound`), False for a
    # dead-end reaction ("lol", an emoji, a gif). This is the ONE gate on every
    # discretionary silence: breaks and the post-gif rest both refuse to fire while
    # he is waiting on an answer. `is_qualifying_inbound` is the right predicate and
    # `is_substantive_msg` is NOT — the latter passes a bare "lol", which is the
    # exact turn a break is safe on.
    answer_owed: bool = False
    after_gif_solo: bool = False         # her last outbound was a gif with no text
    turn_index: int = 0                  # which inbound of the thread this is (1=first)
    # HIS FIRST inbound on this thread — the clock the warm-up window runs off. None
    # means the caller did not wire it, and an unknown start is NOT a warm-up, so a
    # caller that never supplies it keeps today's behaviour exactly.
    thread_started_at: datetime | None = None
    # Add this many seconds to EVERY reply — a flat translation of the whole curve,
    # floor and ceiling together, applied after every other rule has chosen a window.
    # It exists because the fleet's fast bands are the loudest tell we have: measured
    # against the same accounts' human chatters, we sit at 15.1%/17.5% in <15s and
    # 15-30s where a human sits at 7.2%/7.9%, and we UNDERSHOOT 30-60s and 1-2m
    # (7.8/9.0 against 16.5/14.4). Translating the curve drains the two fast bands
    # into the two the human actually lives in without touching the shape.
    # 0.0 keeps every existing caller and test byte-identical.
    reply_bonus_s: float = 0.0
    # `_stepout.is_due` says she is due to step out. The COUNTING lives over there —
    # this module owns the pause, not the schedule.
    stepout_due: bool = False
    # …and how long, in seconds. None ⇒ the shipped 1-2h. None and not 0, because 0
    # is a value an operator can type and it must not silently mean "ignore me"
    # (same convention as `pace_curve` above).
    stepout_min_s: float | None = None
    stepout_max_s: float | None = None
    enabled: bool = True


@dataclass(frozen=True)
class Decision:
    """delay_s: hold inline before sending (always <= INLINE_MAX_S when defer=False).
    wake_at: if set, the caller must RELEASE THE LEASE and enqueue a resume job —
    it must not sleep. cover_line: prepend as its own bubble (she explains the gap)."""
    delay_s: float
    context: str
    wake_at: datetime | None = None
    cover_line: str | None = None

    @property
    def defer(self) -> bool:
        return self.wake_at is not None


# ── Time ─────────────────────────────────────────────────────────────────────
def local_now(utc_now: datetime, tz_offset_minutes: int | None) -> datetime:
    """Creator-local time. Everything else in the executor (wake_at, run_at, leases,
    cooldowns) stays UTC — this converter exists ONLY for the sleep window and the
    daily-counter rollover."""
    if tz_offset_minutes is None:
        return utc_now
    return utc_now + timedelta(minutes=int(tz_offset_minutes))


def tz_offset_for(timezone: str | None, utc_offset: int | None,
                  utc_now: datetime | None = None) -> int | None:
    """Resolve an account's creator-local offset IN MINUTES. THE one clock — every
    engine that tells a fan the time resolves it here.

    `utc_offset` (whole HOURS, 0 == unset) WINS. It is what the Brain's one
    place-and-time dropdown writes, so it is the answer somebody chose and can see
    on screen. The IANA `timezone` is only the fallback for an account that has a
    zone and no offset. Returns None when neither is set — and None means the
    clock stays OFF for that account (no clock line, no sleep window). We do NOT
    default to UTC: a UTC default silently puts a US creator to sleep through her
    peak earning window, and nothing in the product would explain the revenue drop.

    ⚠️ The precedence was the OTHER WAY around until 2026-08-08, and that is what
    this reversal is for. Two clocks were stored per account and the silent one won:
    Isabelle sat on `America/Los_Angeles` next to a stored -4, so her welcome told
    new subscribers "it's Friday night in US" at 07:38 Eastern and named yesterday's
    weekday after Eastern midnight. Nothing on any screen said which of the two was
    live. A fixed offset does NOT track DST — that is the accepted cost of having
    exactly one clock, and it is somebody's job twice a year (US: Nov 1; EU: Oct 25)."""
    if utc_offset:
        return int(utc_offset) * 60
    if timezone:
        try:
            from datetime import timezone as _tz
            from zoneinfo import ZoneInfo
            # The stored datetimes are naive UTC; stamp UTC before converting, or
            # astimezone() would read them as the SERVER's local time (the VPS is
            # UTC, a laptop is not — that divergence would only show up in tests).
            ref = (utc_now or datetime.utcnow()).replace(tzinfo=_tz.utc)
            off = ref.astimezone(ZoneInfo(timezone)).utcoffset()
            if off is not None:
                return int(off.total_seconds() // 60)
        except Exception:
            log.warning("bad timezone %r — and no utc_offset to fall back to", timezone)
    return None


def tz_hours_for(timezone: str | None, utc_offset: int | None,
                 utc_now: datetime | None = None) -> float:
    """`tz_offset_for` in HOURS, with no-clock resolving to 0.0 (UTC).

    Four callers used to spell this out themselves — `off_min / 60.0 if off_min is
    not None else (cfg.utc_offset or 0)` — and that expression is precisely where
    "which of the two clocks is this?" got decided four separate times. The senders
    want hours because `_model_hour` / `_model_weekday` take hours; they should not
    each re-derive the units AND the no-clock default.

    0.0 for a clockless account is what the welcome has always used (it reads as
    UTC). Engines that must NOT invent a clock — the chat prompt line, the sleep
    window — need to tell "no clock" from "UTC", so they call `tz_offset_for` and
    keep the None."""
    off_min = tz_offset_for(timezone, utc_offset, utc_now)
    return off_min / 60.0 if off_min is not None else 0.0


def _parse_hhmm(s: str) -> time:
    h, _, m = str(s).partition(":")
    return time(int(h) % 24, int(m or 0) % 60)


def in_sleep_window(local_dt: datetime, window: tuple[str, str]) -> bool:
    """Is she asleep at this creator-local moment? Handles the wrap past midnight
    (03:00-10:00 does not wrap; 22:00-06:00 does)."""
    start, end = _parse_hhmm(window[0]), _parse_hhmm(window[1])
    t = local_dt.time()
    if start <= end:
        return start <= t < end
    return t >= start or t < end          # wraps midnight


def next_wake(local_dt: datetime, window: tuple[str, str]) -> datetime:
    """Creator-local moment she wakes up. Only meaningful inside the window."""
    end = _parse_hhmm(window[1])
    wake = local_dt.replace(hour=end.hour, minute=end.minute, second=0, microsecond=0)
    if wake <= local_dt:
        wake += timedelta(days=1)
    return wake


def minutes_to_sleep(local_dt: datetime, window: tuple[str, str]) -> float:
    """Creator-local minutes until she goes to sleep (inf if already asleep). The
    ladder uses this: a ladder is never OPENED within LADDER_GRACE_M of the boundary,
    because an abandoned hot ladder that resumes 7 hours later is worse than no sale."""
    if in_sleep_window(local_dt, window):
        return 0.0
    start = _parse_hhmm(window[0])
    nxt = local_dt.replace(hour=start.hour, minute=start.minute, second=0, microsecond=0)
    if nxt <= local_dt:
        nxt += timedelta(days=1)
    return (nxt - local_dt).total_seconds() / 60.0


def ladder_may_open(utc_now: datetime, ctx: RhythmCtx) -> bool:
    """False when a sleep boundary is too close to finish what we'd start. In no-sleep
    mode there is no boundary, so a ladder may always open."""
    if ctx.no_sleep or ctx.tz_offset_minutes is None:
        return True
    lnow = local_now(utc_now, ctx.tz_offset_minutes)
    return minutes_to_sleep(lnow, ctx.sleep_window) > LADDER_GRACE_M


# ── The sampler ──────────────────────────────────────────────────────────────
def _lognormal(rng: Random, mu: float, sigma: float, clamp: tuple[int, int]) -> float:
    v = rng.lognormvariate(mu, sigma)
    return float(min(max(v, clamp[0]), clamp[1]))


def _pick_cover(rng: Random, kind: str, ctx: RhythmCtx,
                utc_now: datetime) -> str | None:
    """One cover line per gap, and never the same fan twice in a week.

    Takes the KIND ("busy"/"asleep"/"long"), not the list, so the voice is
    resolved HERE and a call site cannot reach past it by naming a pool. Both
    lanes are the same length, so the rng draw is unchanged for her."""
    if ctx.last_cover_at is not None:
        if (utc_now - ctx.last_cover_at) < timedelta(days=COVER_REPEAT_DAYS):
            return None
    # TODAY's day wins over the shipped pool — see RhythmCtx.day_covers. Only for
    # the gap kinds that make a claim about what she was DOING: "asleep" is left on
    # its own pool because the day log's beats describe waking hours, and "i fell
    # asleep on you" cannot contradict a hike. Empty tuple ⇒ shipped pools, so this
    # is additive and an account with no day log draws exactly what it always did.
    if ctx.day_covers and kind in ("busy", "long"):
        return rng.choice(list(ctx.day_covers))
    hers, his = _COVER_POOLS[kind]
    return rng.choice(his if str(ctx.voice or "").strip().lower() == "him" else hers)


def is_night(ctx: RhythmCtx, at_utc: datetime) -> bool:
    """Is `at_utc` inside her night? THE one spelling of that question.

    Asked twice, about two different instants: of `now`, to decide whether she is
    asleep right now, and of a prospective `wake_at`, to refuse a pause that would
    deliver a message at 3am. An account with no clock (`tz_offset_minutes is None`)
    and one in no-sleep mode both have no night, so nothing can be inside it."""
    if ctx.tz_offset_minutes is None or ctx.no_sleep:
        return False
    return in_sleep_window(local_now(at_utc, ctx.tz_offset_minutes), ctx.sleep_window)


def decide_availability(ctx: RhythmCtx, utc_now: datetime, rng: Random) -> Decision | None:
    """Is she AROUND to answer at all? None = yes, go generate a reply.

    Split out of decide() and called BEFORE the LLM on purpose: whether she is asleep
    or has stepped away has nothing to do with what he said, so there is no reason to
    pay for a reply we are about to throw away. Deciding this after generation burned
    an LLM call on every deferred tick (~15% of them) and then generated a SECOND one
    on the wake — double cost for a message she was never going to send yet."""
    if not ctx.enabled:
        return None

    # ── 1. Is she asleep? Account-level, decided by the calendar, not per fan.
    # Skipped entirely in no-sleep mode — she never goes down for the night.
    if is_night(ctx, utc_now):
        lnow = local_now(utc_now, ctx.tz_offset_minutes)
        wake_local = next_wake(lnow, ctx.sleep_window)
        wake_utc = wake_local - timedelta(minutes=ctx.tz_offset_minutes)
        # Wake with a human stagger — she does not answer 40 fans at 10:00:00.
        wake_utc += timedelta(seconds=rng.randint(0, 45 * 60))
        return Decision(delay_s=0.0, context=CONTEXT_UNAVAILABLE, wake_at=wake_utc,
                        cover_line=_pick_cover(rng, "asleep", ctx, utc_now))

    # ── 1b. She STEPS OUT — the counter fired (see STEPOUT_MIN_S). Below the sleep
    # check because a woman who is asleep does not also go to the shops, and above the
    # break roll because when both are due this is the one the operator asked for.
    #
    # No cover line at any duration. A returning human says NOTHING about the gap:
    # measured over 8 weeks and 19 accounts, every human acknowledgement bucket out to
    # 12h sits at or BELOW its own no-gap false-positive floor, and the complete
    # inventory past 20 minutes is 21 messages, most of them about content rather than
    # time. Explaining a two-hour absence is a bigger tell than the absence.
    if ctx.stepout_due and not ctx.fan_hot \
            and _context_of(ctx, utc_now) == CONTEXT_FREE_CHAT \
            and not protected_start(ctx, utc_now):
        lo_s = STEPOUT_MIN_S if ctx.stepout_min_s is None else float(ctx.stepout_min_s)
        hi_s = STEPOUT_MAX_S if ctx.stepout_max_s is None else float(ctx.stepout_max_s)
        out_s = min(rng.uniform(lo_s, max(lo_s, hi_s)), MAX_DELAY_S)
        wake_utc = utc_now + timedelta(seconds=out_s)
        # …unless she would come back mid-night. The resume run sends IMMEDIATELY —
        # it skips this function entirely — so a step-out begun at 01:00 delivers a
        # message at 03:00, which is the loudest anti-human event the system can
        # produce and it would be manufactured by the feature meant to prevent them.
        # Skipping (rather than shortening) keeps the duration honest; the counter
        # stays armed and she steps out on the next eligible turn instead.
        if not is_night(ctx, wake_utc):
            return Decision(delay_s=0.0, context=CONTEXT_STEPOUT, wake_at=wake_utc)

    # ── 2. She's a person: roll a break — but ONLY when the scene is COLD. A hot chat
    # never breaks (heat→0 break prob); a boring, sale-less one does, more the colder it
    # gets. This is the operator's "if it's hot both reply fast; if boring or no sales she
    # takes a break." An open ask forces heat high, so a live sell is structurally
    # break-proof (a ladder stranded mid-sell is the worst outcome in the system).
    # An OWED ANSWER is break-proof, and so is the opening. Both are the same rule
    # the two 07-26 subjects taught: silence is cheap on a turn that closed cleanly
    # and expensive on one that didn't. Without the `answer_owed` gate a break rolls
    # straight into a fan who just volunteered something — which is precisely how fan
    # a fan went from a full volunteered sentence to a one-word reply to gone. Note
    # this is deliberately NOT a turn-counter script: a gap at exchange 3 is fine
    # after a throwaway line and ruinous after a question, so position never
    # decides it.
    # `pace_buckets` REPLACES this roll rather than stacking with it. The operator
    # specified a complete distribution — 85/10/4/1 summing to 100% — so its own
    # top band IS the "she is away" case. Layering a 12% break on top produced 4%
    # at 15-60min and 2% past an hour, measured: four times the tail asked for,
    # plus one that was not asked for at all. The SLEEP window above still
    # applies; a night is not a break.
    if _context_of(ctx, utc_now) == CONTEXT_FREE_CHAT and not ctx.pace_buckets \
            and not ctx.answer_owed and not protected_start(ctx, utc_now):
        _, _, p_break = _heat_params(scene_heat(ctx, utc_now))
        if rng.random() < p_break:
            if ctx.no_sleep:
                break_s = _lognormal(rng, _BREAK_MU_SHORT, _BREAK_SIGMA_SHORT, _BREAK_CLAMP_SHORT)
            else:
                break_s = _lognormal(rng, _BREAK_MU, _BREAK_SIGMA, _BREAK_CLAMP)
            wake_utc = utc_now + timedelta(seconds=min(break_s, MAX_DELAY_S))
            return Decision(delay_s=0.0, context=CONTEXT_UNAVAILABLE, wake_at=wake_utc,
                            cover_line=_pick_cover(rng, "busy", ctx, utc_now))

    return None


def in_opening(ctx: RhythmCtx) -> bool:
    """Is this one of his first few inbounds? `turn_index` is 1-based and 0 means the
    caller did not supply it — an unknown turn is NOT treated as the opening, so a
    caller that never wires it keeps today's behaviour exactly."""
    return 1 <= ctx.turn_index <= _OPENING_TURNS


def in_warmup(ctx: RhythmCtx, utc_now: datetime) -> bool:
    """Are we inside the first _WARMUP_S of the conversation? Unlike `in_opening`
    this is a CLOCK, not a turn count — the ask was "no 30-minute gap, ever, for the
    first twenty minutes", and a fan who fires off eight messages in four minutes
    would walk straight out of any turn-based window."""
    if ctx.thread_started_at is None:
        return False
    return (utc_now - ctx.thread_started_at).total_seconds() <= _WARMUP_S


def protected_start(ctx: RhythmCtx, utc_now: datetime) -> bool:
    """Is this thread young enough that she may not simply wander off? The union of
    the two windows — they overlap without either containing the other, so both are
    asked. This is the ONLY place a break consults them."""
    return in_opening(ctx) or in_warmup(ctx, utc_now)


def _context_of(ctx: RhythmCtx, utc_now: datetime) -> str:
    """ask_open while an unpaid rung is live OR he paid within the last 60min (the
    strike-while-hot window); free_chat otherwise. Break rolls and the reply ceiling
    both key off this, never off a mu shift."""
    if ctx.ladder_open:
        return CONTEXT_ASK_OPEN
    if ctx.last_paid_at is not None and \
            (utc_now - ctx.last_paid_at).total_seconds() <= 3600:
        return CONTEXT_ASK_OPEN
    return CONTEXT_FREE_CHAT


def scene_heat(ctx: RhythmCtx, utc_now: datetime) -> float:
    """How HOT is this conversation right now, in [0, 1]? 1 = white-hot (mid-sext,
    both firing back in seconds, buying); 0 = cold/boring (slow, quiet, no sale).

    The reply distribution interpolates between the measured sexting pole (median 78s)
    and the chat pole (median 374s) by this score, and the break probability scales with
    its INVERSE. This is the operator's rule made literal: 'if it's hot both reply fast;
    if boring or no sales she takes a break.' It reads only per-FAN signals — heat is a
    property of THIS thread, never an account average."""
    h = 0.30                                   # a neutral thread sits low-warm
    # An open/hot ladder IS an active sell — the hottest signal we have.
    if ctx.ladder_open:
        h += 0.35
    # He just paid — the strike-while-hot window is live.
    if ctx.last_paid_at is not None and \
            (utc_now - ctx.last_paid_at).total_seconds() <= 20 * 60:
        h += 0.20
    # He's escalating / asking for content this very turn.
    if ctx.fan_hot:
        h += 0.20
    # HIS pace: a man firing back in seconds is hot; one who takes 10min is cooling.
    if ctx.his_last_latency_s is not None and ctx.his_last_latency_s > 0:
        h += 0.30 * max(0.0, 1.0 - ctx.his_last_latency_s / 300.0)
    return float(min(1.0, max(0.0, h)))


def _heat_params(heat: float) -> tuple[float, float, float]:
    """(mu, sigma, p_break) interpolated between the cold and hot poles by heat."""
    mu = _COLD_MU + (_HOT_MU - _COLD_MU) * heat            # hot → ln78, cold → ln374
    sigma = _COLD_SIGMA + (_HOT_SIGMA - _COLD_SIGMA) * heat
    p_break = _P_BREAK_COLD * (1.0 - heat) ** 2            # hot → 0, cold → 0.12
    return mu, sigma, p_break


# A hard bound on a heavy-tailed draw does not clip the tail, it STACKS it on one
# value. Every clamp in this module had that defect: 20.9% of opening replies came out
# at exactly 25.000s (the floor) and 37.5% of warm-up replies at exactly 480.000s (the
# ceiling). A repeated identical interval is the most machine-readable thing a chat can
# emit, so the bounds are applied SOFTLY — a draw that would have landed on a bound is
# spread across a band just inside it instead. Same window, same median, no spike.
_EDGE_SPREAD = 0.08          # band width, as a fraction of (ceiling - floor)


def _fit(core: float, floor: float, ceiling: float, rng: Random) -> float:
    """Bring `core` inside [floor, ceiling] WITHOUT piling mass on either bound."""
    span = (ceiling - floor) * _EDGE_SPREAD
    if span <= 0:
        return floor
    if core >= ceiling:
        return rng.uniform(ceiling - span, ceiling)
    if core <= floor:
        return rng.uniform(floor, floor + span)
    return core



# ── The operator's own reply-time distribution (opt-in) ─────────────────────
# Operator ruling 2026-08-04: 85% inside 2 minutes, 10% at 2-6, 4% at 6-15, 1%
# at 15-60. That is a FASTER, more attentive creator than the archive-fitted
# lognormal above, which sits at a 124s median hot / 415s cold and puts nothing
# like 85% under two minutes.
#
# ⚠️ OPT-IN, AND THAT IS THE WHOLE DESIGN. Six live accounts run rhythm today
# (523982374, 25166249, 267492960, 571598796, 515679424, 506355167) on a curve
# measured from their OWN archive. Making this the default would re-pace all of
# them overnight to a distribution nobody fitted to their data — the standing
# rule is that the accounts already earning do not move. `RhythmCtx.pace_buckets`
# defaults False, so every existing caller samples exactly what it sampled
# before.
#
# Weights are declared, not derived, and they must sum to 1.0 — asserted at
# import so a typo is a startup failure rather than a silently skewed curve.
PACE_BUCKETS: tuple[tuple[float, float, float], ...] = (
    # (weight, low_seconds, high_seconds)
    (0.85, 0.0, 120.0),        # 0-2 min    — she is on her phone
    (0.10, 120.0, 360.0),      # 2-6 min    — put it down for a moment
    (0.04, 360.0, 900.0),      # 6-15 min   — genuinely doing something
    (0.01, 900.0, 3600.0),     # 15-60 min  — gone, but not for the night
)
assert abs(sum(w for w, _, _ in PACE_BUCKETS) - 1.0) < 1e-9, \
    "PACE_BUCKETS weights must sum to 1.0"

# Bounds on an operator-authored curve (`parse_pace_curve`). The row cap is a UI
# affordance, not a modelling claim; the 24h ceiling is the honest one — anything
# past MAX_DELAY_S is clamped downstream anyway, so a longer band would be a
# number the UI shows and the engine quietly ignores.
PACE_CURVE_MAX_ROWS = 8
PACE_CURVE_MAX_MINUTES = 24 * 60


def parse_pace_curve(raw) -> tuple[tuple[float, float, float], ...] | None:
    """An operator-authored reply-time curve → the same (weight, lo_s, hi_s) shape
    as PACE_BUCKETS, or None when there is nothing usable to run.

    Stored shape is a list of `{"pct": <0-100>, "up_to_min": <minutes>}` — CUT
    POINTS, not intervals. Each row's lower edge is the previous row's upper edge,
    so contiguity is structural: the UI cannot author a gap between 6 and 15
    minutes, or two bands that overlap, because there is nowhere to express one.

    Weights are NORMALISED rather than required to sum to 100. An operator typing
    85/10/4/1 into four boxes will pass through 99 and 101 on the way, and a curve
    that refuses to load at 99 means the engine silently reverts to a different
    distribution mid-edit — the failure mode is invisible, so we don't create it.

    Returns None (⇒ caller falls back to PACE_BUCKETS) for anything malformed. A
    bad curve must never be an exception on a send path."""
    if not isinstance(raw, (list, tuple)) or not raw:
        return None
    out: list[tuple[float, float, float]] = []
    lo = 0.0
    for row in list(raw)[:PACE_CURVE_MAX_ROWS]:
        if not isinstance(row, dict):
            return None
        try:
            pct = float(row.get("pct"))
            up_to = float(row.get("up_to_min"))
        except (TypeError, ValueError):
            return None
        if pct < 0 or not (0 < up_to <= PACE_CURVE_MAX_MINUTES):
            return None
        hi = up_to * 60.0
        if hi <= lo:                     # cut points must strictly increase
            return None
        out.append((pct, lo, hi))
        lo = hi
    total = sum(w for w, _, _ in out)
    if total <= 0:                       # every band at 0% is not a distribution
        return None
    return tuple((w / total, a, b) for w, a, b in out)


def sample_pace_bucket(rng: Random, curve=None) -> float:
    """A latency in seconds from `curve` (default PACE_BUCKETS) — pick a band by
    weight, then a uniform point inside it.

    Uniform WITHIN the band rather than another lognormal: the operator
    specified the shape at bucket granularity, and layering a second curve inside
    would quietly move the mass they asked for toward the low edge of each band."""
    buckets = curve or PACE_BUCKETS
    r = rng.random()
    cum = 0.0
    for weight, lo, hi in buckets:
        cum += weight
        if r < cum:
            return rng.uniform(lo, hi)
    lo, hi = buckets[-1][1], buckets[-1][2]
    return rng.uniform(lo, hi)          # float dust past the last edge


def beat_applies(ctx: RhythmCtx) -> bool:
    """She sent a gif and he owes her nothing back ⇒ let it breathe. The one
    deliberate pause in the design, and the only condition that gets its own draw."""
    return ctx.after_gif_solo and not ctx.answer_owed


def _draw(ctx: RhythmCtx, utc_now: datetime, rng: Random) -> float:
    """The raw reply latency, before bounds. ONE ordered decision — the first branch
    that matches owns the distribution, so precedence is a readable list rather than
    something that emerges from clamps interacting.

    The beat outranks the opening deliberately: on his second message, right after
    the opening gif, the operator wants the five-minute beat, not a 55-second snap.
    (Which is also why the opening is usually sampled ONCE per fan — turn 2 only
    reaches this pole when turn 1 produced no gif, e.g. he opened with a question.)"""
    if beat_applies(ctx):
        return rng.triangular(_BEAT_S[0], _BEAT_S[1], _BEAT_MODE_S)
    if in_opening(ctx):
        return _OPENING_S[ctx.turn_index - 1]
    # Below the two DELIBERATE pauses (the gif beat, the opening arc) — those are
    # staged moments and the operator's curve is about ordinary replies.
    if ctx.pace_buckets:
        return sample_pace_bucket(rng, ctx.pace_curve)
    mu, sigma, _ = _heat_params(scene_heat(ctx, utc_now))
    return rng.lognormvariate(mu, sigma)


def _bounds(ctx: RhythmCtx, utc_now: datetime, context: str) -> tuple[float, float]:
    """(floor, ceiling) for that draw, resolved in ONE place. Pure — no rng, so it
    can be called in any order relative to the draw.

    Postcondition: floor <= ceiling, ALWAYS. Without it the pair can invert (a wide
    floor under a narrow ceiling) and which end wins is then decided by how the caller
    happens to nest its min/max — `min(max(x, floor), ceiling)` and
    `max(min(x, ceiling), floor)` give opposite answers from the same numbers."""
    if beat_applies(ctx):
        # The beat's draw is already inside its own range, so nothing may narrow it —
        # not the opening ceiling, not the warm-up. Clamping it is what produced the
        # point mass at 300s/420s that this whole shape exists to remove. Its top
        # (7 min) is under _WARMUP_CEIL_S, so it never breaks the warm-up promise.
        return float(_FLOOR_S), _BEAT_S[1]

    ceiling = _ASK_OPEN_CEIL_S if context == CONTEXT_ASK_OPEN else _FREE_CHAT_CEIL_S
    if in_warmup(ctx, utc_now):
        ceiling = min(ceiling, _WARMUP_CEIL_S)

    floor = float(_FLOOR_S)
    # Soft fast-reply nudge: if she's been snapping back <30s too often lately, floor
    # this draw at 60s. The hard >35%-share floor was refuted (non-monotonic); this
    # keeps only the direction the data supports (accused threads reply faster).
    recent = ctx.recent_realized_s[-_FAST_REPLY_WINDOW:]
    if sum(1 for d in recent if d < _FAST_REPLY_S) >= _FAST_REPLY_TRIGGER:
        floor = max(floor, _FAST_NUDGE_FLOOR_S)
    return min(floor, ceiling), ceiling


def mirror_mult(his_last_latency_s: float | None) -> float:
    """Cosmetic pace-mirror: a whisper on TOP of the heat model. See _MIRROR_BETA. His
    latency already drives `heat`; this just keeps a fan who fires back in 8s from getting
    an identical fixed cadence back. Deliberately weak (beta 0.12) — the strong version
    was an acausal confound."""
    if not his_last_latency_s or his_last_latency_s <= 0:
        return 1.0
    m = (his_last_latency_s / _FAN_MEDIAN_LATENCY_S) ** _MIRROR_BETA
    return float(min(max(m, _MIRROR_CLAMP[0]), _MIRROR_CLAMP[1]))


def decide(ctx: RhythmCtx, utc_now: datetime, rng: Random) -> Decision:
    """How long before she answers — and whether she is even around to answer.

    The returned delay ALWAYS includes ctx.typing_delay_s (the existing wpm hold),
    so the caller replaces its old delay with this one rather than adding to it.

    Callers that want to skip the LLM on an away/asleep tick call decide_availability()
    FIRST and only reach here when it returns None. decide() re-checks it anyway, so a
    caller that doesn't is still correct — just wasteful."""
    typing = max(0.0, float(ctx.typing_delay_s))

    # Disabled ⇒ byte-identical to today: the wpm typing delay, nothing else.
    if not ctx.enabled:
        return Decision(delay_s=typing, context=CONTEXT_ENGAGED)

    gap_s = 0.0
    if ctx.last_outbound_at is not None:
        gap_s = max(0.0, (utc_now - ctx.last_outbound_at).total_seconds())

    away = decide_availability(ctx, utc_now, rng)
    if away is not None:
        return Decision(delay_s=typing, context=away.context, wake_at=away.wake_at,
                        cover_line=away.cover_line)

    # ── Normal reply. HEAT-DRIVEN (v3): the draw's median + spread interpolate between
    # the fast sexting pole (78s) and the slow chat pole (374s) by this thread's live
    # heat. A hot scene ⇒ ~every-minute replies; a cold one ⇒ minutes, drifting toward a
    # break. The ceiling still keys off whether an ask is open (she never vanishes mid-sell).
    # Draw, then clamp. WHICH distribution and WHICH bounds is the whole policy, and it
    # lives in _draw()/_bounds() — nothing here depends on statement order, and _bounds
    # is pure so the two may be called in either order.
    # NOTE: his reply speed is ALREADY a heat input (scene_heat's latency term). The old
    # mirror_mult multiplied it AGAIN here — a double-count of a signal that was itself a
    # confound. Dropped: heat owns his-pace now. mirror_mult stays defined for any future
    # non-heat caller, but the decide() path must not re-apply it.
    context = _context_of(ctx, utc_now)
    core = _draw(ctx, utc_now, rng)
    floor, ceiling = _bounds(ctx, utc_now, context)
    # He was gone a while and came back (see _RETURN_GAP_S). Clamp into the 2-7 min
    # band: long enough that she plainly was not waiting, short enough that a fan who
    # just re-engaged is never left to cool off. The TAIL is cut as well as the floor
    # — he is back, which is exactly when a break would cost the most.
    #
    # INTERSECTED with the bounds already chosen, never substituted for them: a
    # tighter cap elsewhere still wins, and if another rule leaves no room the band is
    # dropped rather than forced. `decide_availability` ran above, so sleep windows
    # and breaks are untouched by this.
    # ── The flat bonus (see RhythmCtx.reply_bonus_s). A TRANSLATION of the window —
    # floor, ceiling and the drawn core all move together — so the shape of the curve
    # is untouched and no mass piles on a bound.
    #
    # ⚠️ APPLIED BEFORE THE RETURN BAND, and that order is load-bearing. `_RETURN_BAND`
    # opens at 120.0 and `INLINE_MAX_S` is 120 — THE SAME NUMBER — while the defer test
    # is strict (`delay > INLINE_MAX_S`). Translating the band itself lifts its floor to
    # 130 and every single returning-fan reply is then structurally over the line: not
    # more likely to defer, but CERTAIN to, at any bonus above zero (measured: 6.5% →
    # 100.0% at bonus=0.5s). That defer is decided POST-LLM, so the generated reply is
    # thrown away and regenerated on the wake — the exact double-billing
    # `decide_availability` was split out to avoid — plus a released lease, a scheduled
    # job and up to a tick of poll lag, on the one turn the band exists to keep tight.
    # Applied first, the band clamps the already-translated window and its floor stays
    # at 120, which is what "a tighter cap still wins" was always supposed to mean.
    bonus = max(0.0, float(ctx.reply_bonus_s or 0.0))
    if bonus:
        floor += bonus
        ceiling += bonus
        core += bonus
    if (ctx.his_last_latency_s or 0.0) > _RETURN_GAP_S:
        lo, hi = max(floor, _RETURN_BAND[0]), min(ceiling, _RETURN_BAND[1])
        if lo <= hi:
            floor, ceiling = lo, hi
    core = _fit(core, floor, ceiling, rng)

    # Target TOTAL inbound→reply latency, not a delay stacked on top of however long
    # the executor poll already sat. Otherwise every reply is (poll interval + draw)
    # and the whole account shows a telltale spike parked at ~7min.
    already = 0.0
    if ctx.last_inbound_at is not None:
        already = max(0.0, (utc_now - ctx.last_inbound_at).total_seconds())
    delay = max(floor, core - already)
    delay = min(delay + typing, ceiling)

    # She was genuinely gone a while — and only THEN, ~1 in 10, does she mention it.
    # Apologising for every 20-min gap is itself the tell.
    cover = None
    if gap_s >= COVER_MIN_GAP_S and rng.random() < _cover_roll_for(gap_s):
        kind = "long" if gap_s >= 4 * 3600 else "busy"
        cover = _pick_cover(rng, kind, ctx, utc_now)

    if delay > INLINE_MAX_S:
        # Too long to hold the lease for. Hand it to the scheduler (the wake_at poll).
        return Decision(delay_s=typing, context=context,
                        wake_at=utc_now + timedelta(seconds=delay), cover_line=cover)

    return Decision(delay_s=delay, context=context, cover_line=cover)


def ppv_drop_delay(rng: Random, *, stalled: bool = False) -> float:
    """The gap between her setup line ("ok look what i made") and the priced attach
    landing. Measured on 3,670 human 1:1 PPVs it peaks at 60-120s (47.4%) with a hard
    floor: a paywall that lands the same second as the tease reads as automated.

    Bounded to [45, 115] so it ALWAYS holds inline (< INLINE_MAX_S) — no parked offer,
    no scheduler, no self-manufactured stranded promise. The 45s floor is an invariant:
    no priced attach may ever leave < 45s after our own preceding outbound. `stalled`
    (she said she's filming it now) biases toward the top of the band."""
    mu = math.log(100 if stalled else 90)
    drop = rng.lognormvariate(mu, 0.5)
    return float(min(max(drop, 45.0), 115.0))


def derive_sleep_window(hour_counts: dict[int, int]) -> tuple[str, str]:
    """Derive the account's sleep window from its own outbound hour-of-day histogram
    (creator-local hours): the longest contiguous block under 5% of peak. Read-only in
    the UI — the operator is not asked a question the data already answers.

    Too little history ⇒ DEFAULT_SLEEP. Never "always awake"."""
    if not hour_counts:
        return DEFAULT_SLEEP
    counts = {h: int(hour_counts.get(h, 0) or 0) for h in range(24)}
    peak = max(counts.values()) if counts else 0
    total = sum(counts.values())
    if peak <= 0 or total < 200:          # not enough evidence to claim a pattern
        return DEFAULT_SLEEP
    quiet = {h for h, n in counts.items() if n < 0.05 * peak}
    if not quiet:
        return DEFAULT_SLEEP

    # Longest contiguous quiet run on the 24h circle.
    best_start, best_len = None, 0
    for start in range(24):
        if start not in quiet:
            continue
        length = 0
        while length < 24 and ((start + length) % 24) in quiet:
            length += 1
        if length > best_len:
            best_start, best_len = start, length
    if best_start is None or best_len < 3:   # a 1-2h dip is not a sleep window
        return DEFAULT_SLEEP
    end = (best_start + best_len) % 24
    return (f"{best_start:02d}:00", f"{end:02d}:00")
