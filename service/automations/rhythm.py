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

# Fallback when an account has too little history to derive a sleep window. NOT
# "always awake" — a girl who never sleeps is a bot, and the default has to be safe
# in the absence of evidence.
DEFAULT_SLEEP = ("03:00", "10:00")

CONTEXT_ASK_OPEN = "ask_open"      # an unpaid rung is live, or he paid < 60min ago
CONTEXT_FREE_CHAT = "free_chat"    # banter / sext, no open ask
CONTEXT_UNAVAILABLE = "unavailable"
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
    """Resolve an account's creator-local offset IN MINUTES.

    IANA `timezone` wins (DST-correct). Falls back to the legacy `utc_offset` HOURS
    column when it is explicitly set, so an account that already answered this
    question doesn't have to answer it twice. Returns None when neither is set —
    and None means the feature stays OFF for that account. We do NOT default to
    UTC: a UTC default silently puts a US creator to sleep through her peak earning
    window, and nothing in the product would explain the revenue drop."""
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
            log.warning("bad timezone %r — falling back to utc_offset", timezone)
    if utc_offset:
        return int(utc_offset) * 60
    return None


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


def _pick_cover(rng: Random, pool: list[str], ctx: RhythmCtx,
                utc_now: datetime) -> str | None:
    """One cover line per gap, and never the same fan twice in a week."""
    if ctx.last_cover_at is not None:
        if (utc_now - ctx.last_cover_at) < timedelta(days=COVER_REPEAT_DAYS):
            return None
    return rng.choice(pool)


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
    if ctx.tz_offset_minutes is not None and not ctx.no_sleep:
        lnow = local_now(utc_now, ctx.tz_offset_minutes)
        if in_sleep_window(lnow, ctx.sleep_window):
            wake_local = next_wake(lnow, ctx.sleep_window)
            wake_utc = wake_local - timedelta(minutes=ctx.tz_offset_minutes)
            # Wake with a human stagger — she does not answer 40 fans at 10:00:00.
            wake_utc += timedelta(seconds=rng.randint(0, 45 * 60))
            return Decision(delay_s=0.0, context=CONTEXT_UNAVAILABLE, wake_at=wake_utc,
                            cover_line=_pick_cover(rng, COVER_ASLEEP, ctx, utc_now))

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
    if _context_of(ctx, utc_now) == CONTEXT_FREE_CHAT \
            and not ctx.answer_owed and not protected_start(ctx, utc_now):
        _, _, p_break = _heat_params(scene_heat(ctx, utc_now))
        if rng.random() < p_break:
            if ctx.no_sleep:
                break_s = _lognormal(rng, _BREAK_MU_SHORT, _BREAK_SIGMA_SHORT, _BREAK_CLAMP_SHORT)
            else:
                break_s = _lognormal(rng, _BREAK_MU, _BREAK_SIGMA, _BREAK_CLAMP)
            wake_utc = utc_now + timedelta(seconds=min(break_s, MAX_DELAY_S))
            return Decision(delay_s=0.0, context=CONTEXT_UNAVAILABLE, wake_at=wake_utc,
                            cover_line=_pick_cover(rng, COVER_BUSY, ctx, utc_now))

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
        pool = COVER_LONG if gap_s >= 4 * 3600 else COVER_BUSY
        cover = _pick_cover(rng, pool, ctx, utc_now)

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
