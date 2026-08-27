"""How much is she allowed to say to this man, and why.

Everything that bounds ai_chatter's reply volume lives here — the two gates, the
spend rules that lift them, and the ledger that records the verdicts:

    _cadence_gate   item 21   the BURST cap. How long one sitting runs. Reopens
                              after `session_gap_minutes` of her silence.
    _quota_gate     item 21c  the DAILY ceiling. How many sittings he gets. The
                              burst cap alone had no bound: a fan who keeps typing
                              collects a fresh cap every hour, ~24 a day.
    _spend_caps     item 21b  what his rolling-window spend adds to the burst cap.
    _daily_quotas   item 21c  what it adds to the daily ceiling.

They compose deliberately. Both are PURE functions of a candidate plus config, both
return the effective limit they decided on (never a recomputation for the UI to
drift from), and both only ever RAISE a leash for money — a proven spender is never
rationed as tightly as a stranger.

Extracted from ai_chatter.py, which was 6,000 lines and growing. The seam is narrow
on purpose: `LeashCand` below is the entire contract these gates need from a
candidate, which is why they can be read, tested and reasoned about without the
sweep around them.
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime, timedelta
from random import Random
from typing import NamedTuple, Protocol

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from db.engine import get_session
from db.models import ContentOffer, Message, QuotaAudit, Transaction

from ._common import CONTENT_ASK_RE, ESCALATION_RE, recent_payer_fans

log = logging.getLogger("of-relay.automations.leash")

# Ledger kinds that count as money from the fan. One definition, shared by every
# spend window below so "spend" can only ever mean one thing.
_TIP_KINDS = ("tip", "tip_post", "tip_stream")


class LeashCand(Protocol):
    """What the gates need from a candidate — the whole seam, stated once.

    A Protocol rather than an import of ai_chatter's `_Cand`: it keeps the
    dependency pointing one way, and it documents exactly which fields the leash
    reads out of a class that carries far more."""
    fan_id: int
    last_body: str
    session_out_n: int      # her replies in the CURRENT burst
    day_out_n: int          # her replies in the current quota day (see `quota_used`)
    day_out_n_at_stop: int  # …and what that count stood at when she last spoke: the
                            # ration that put her on a rung, frozen while she serves it
    total_out_n: int        # her replies over the whole thread
    her_last_at: datetime | None
    first_at: datetime | None
    pic_sent: bool          # his MOST RECENT inbound carried media


# ── Signal tiers ────────────────────────────────────────────────────────────────
# The classification `_cadence_gate` makes and `_quota_gate` consumes. Naming them
# stops the two gates from answering "is this man buying" with two hand-rolled
# booleans that drift apart — which is exactly what happened before: three call
# sites, three formulas, and the status endpoint's copy was silently missing the
# pic term, so the drawer disagreed with the bot for any fan who sent a photo.
TIER_POST_PURCHASE = "post_purchase"
TIER_PIC_SENT = "pic_sent"
TIER_BUYING_SIGNAL = "buying_signal"
TIER_NO_SIGNAL = "no_signal"
TIER_BASELINE = "baseline"

# Tiers that mean "he is reaching for his wallet RIGHT NOW". The daily quota reads
# this instead of re-deriving it, so the two gates agree by construction.
HOT_TIERS = frozenset({TIER_POST_PURCHASE, TIER_PIC_SENT, TIER_BUYING_SIGNAL})


def _cadence_gate(c: LeashCand, *, pending: ContentOffer | None, recent_payer: bool,
                  money_at: datetime | None, pic: bool,
                  now: datetime, cad: dict,
                  spend_cap: int = 0) -> tuple[bool, str, int]:
    """Decide whether ai_chatter should keep engaging this fan THIS tick, and under
    which signal tier. Returns (stop, tier, cap) — a pure function of the fan's live
    state; every limit comes from `cad` (the config). `cap` is the EFFECTIVE reply
    cap that decided `stop` (0 = uncapped, e.g. the post-purchase window); it is the
    single source of truth for "how many replies is she allowed in this BURST", so
    the status endpoint shows the same number the bot ran on instead of recomputing
    it. A "stop" means skip the reply this tick (no LLM, no pause): the fan reopens
    on a real buying signal (tier upgrade) or after a session-gap of silence resets
    his burst.

    The DAY is bounded separately, by `_quota_gate` — which consumes the `tier`
    returned here rather than classifying the fan a second time.

      • Post-purchase window (item 17): a fan who paid within post_purchase_minutes
        stays engaged with no cap; once that window lapses, the buy still counts as a
        buying signal, so he falls back to the buying_signal tier (cap 20) rather than
        being cut off — a proven buyer keeps a full leash for the recent-payer hour.
      • Otherwise classify the signal (item 21) and stop once the burst reply count
        (`session_out_n`) reaches that tier's cap (item 10 / "selling stops").

    `spend_cap` (item 21b) is the floor his rolling-window PAID spend earns, precomputed
    by _spend_caps. It only ever RAISES the leash: the effective cap is the MAX of the
    live signal tier's cap and the spend cap, so a proven spender is never cut off as
    short as a stranger, but a hotter live signal still wins if it's higher."""
    body = c.last_body or ""
    live_signal = bool(pic or CONTENT_ASK_RE.search(body) or ESCALATION_RE.search(body))

    # Item 17 — post-purchase talk window: a just-paid fan gets an UNCAPPED burst for
    # post_purchase_minutes after his last money event. Past it there's no early exit —
    # he falls through to the tier logic below, where the buy still reads as a buying
    # signal (recent_payer → buying_signal, cap 20): a full leash, never a hard stop.
    ppm = int(cad.get("post_purchase_minutes") or 0)
    if money_at is not None and ppm and (now - money_at) <= timedelta(minutes=ppm):
        return (False, TIER_POST_PURCHASE, 0)         # just paid → uncapped burst

    limits = cad.get("msg_limits_by_signal") or {}
    if pic:
        tier = TIER_PIC_SENT
    elif recent_payer or live_signal:
        tier = TIER_BUYING_SIGNAL
    elif pending is not None:
        # An offer is on the table. Fresh → keep working it (buying_signal); stale
        # (older than offer_expiry_minutes, still unbought) → short-leash no_signal.
        oem = int(cad.get("offer_expiry_minutes") or 0)
        stale = bool(oem and pending.offered_at
                     and (now - pending.offered_at) > timedelta(minutes=oem))
        tier = TIER_NO_SIGNAL if stale else TIER_BUYING_SIGNAL
    else:
        tier = TIER_BASELINE

    cap = int(limits.get(tier) or 0)
    # A proven spender's floor lifts the leash but never lowers it: take whichever
    # cap is larger. (A spend_cap of 0 — no rule matched — leaves the signal cap as
    # is; a signal cap of 0, i.e. an unconfigured tier, means "no cap" and must stay
    # uncapped, so only fold in spend_cap when there IS a signal cap to raise.)
    if cap:
        cap = max(cap, int(spend_cap or 0))
    return (bool(cap and c.session_out_n >= cap), tier, cap)


async def _last_money_at(account_id: str, fan_ids) -> dict[int, datetime]:
    """{fan_id: newest money-event time} — the later of an inbound tip (is_tip, so
    the event time is created_at) and a PPV unlock (purchased_at). Drives the
    post-purchase talk window (item 17). Fans with no money event are absent."""
    ids = [int(x) for x in fan_ids]
    if not ids:
        return {}
    async with get_session() as s:
        rows = (await s.execute(
            select(Message.fan_id,
                   func.max(func.coalesce(Message.purchased_at, Message.created_at)))
            .where(Message.account_id == str(account_id),
                   Message.fan_id.in_(ids),
                   Message.is_unsent.is_(False),
                   or_(Message.is_tip.is_(True), Message.purchased_at.isnot(None)))
            .group_by(Message.fan_id)
        )).all()
    return {int(fid): ts for fid, ts in rows if ts is not None}


# ── Spend windows ───────────────────────────────────────────────────────────────

async def _paid_spend_by_window(account_id: str, fan_ids, windows: list[int],
                                now: datetime) -> dict[int, dict[int, int]]:
    """{days: {fan_id: PAID cents in the trailing `days`}} — the shared engine under
    both spend-driven leashes (`_spend_caps` and `_daily_quotas`), so "spend" can
    only ever mean one thing.

    Spend = PPV unlocks (paid outbound, priced by purchased_at/created_at) PLUS tips
    (the transactions ledger, by occurred_at) — the same two sources `_buyer_facts`
    and `_paid_cents_7d` trust. One batched group-by per DISTINCT window, so cost is
    flat in fans and in rules."""
    ids = [int(x) for x in fan_ids]
    spend_by_window: dict[int, dict[int, int]] = {}
    async with get_session() as s:
        for days in windows:
            since = now - timedelta(days=days)
            ppv = (await s.execute(
                select(Message.fan_id,
                       func.coalesce(func.sum(Message.price_cents), 0))
                .where(Message.account_id == str(account_id),
                       Message.fan_id.in_(ids),
                       Message.direction == "out",
                       Message.is_paid.is_(True),
                       Message.price_cents > 0,
                       func.coalesce(Message.purchased_at, Message.created_at) >= since)
                .group_by(Message.fan_id)
            )).all()
            tips = (await s.execute(
                select(Transaction.fan_id,
                       func.coalesce(func.sum(Transaction.amount_cents), 0))
                .where(Transaction.account_id == str(account_id),
                       Transaction.fan_id.in_(ids),
                       Transaction.kind.in_(_TIP_KINDS),
                       Transaction.status.in_(("cleared", "pending")),
                       Transaction.occurred_at >= since)
                .group_by(Transaction.fan_id)
            )).all()
            tot: dict[int, int] = {}
            for fid, cents in ppv:
                tot[int(fid)] = tot.get(int(fid), 0) + int(cents or 0)
            for fid, cents in tips:
                tot[int(fid)] = tot.get(int(fid), 0) + int(cents or 0)
            spend_by_window[days] = tot
    return spend_by_window


def spend_windows(*rule_sets: list[dict]) -> list[int]:
    """The union of rolling windows a set of spend rules needs.

    Exists so the two folds below can share ONE scan. They used to `await
    _paid_spend_by_window` independently, over the same fans, the same `now` and
    overlapping windows — two sessions and eight group-bys where one and four do,
    on a 15s-per-open-chat poll and again on every ~30s roster sweep. Nothing about
    the two was ever different except the fold itself."""
    return sorted({int(r["days"]) for rules in rule_sets for r in (rules or [])
                   if int(r.get("days") or 0) > 0})


def spend_caps(spend_by_window: dict[int, dict[int, int]],
               rules: list[dict]) -> dict[int, int]:
    """{fan_id: the highest BURST cap his rolling-window paid spend earns} for the
    item-21b spend floor. `rules` is msg_limits_by_spend — [{days, min_cents, cap}].
    Only fans that clear at least one rule appear; the rest keep their signal cap.

    A rule with no positive cap or a non-positive window/threshold is inert — an
    operator zeroing a rung turns it off rather than capping anyone at 0.

    A pure fold over an already-fetched scan: the caller owns the one round trip,
    so `account_id`/`fan_ids`/`now` are not parameters here and cannot drift from
    whatever the sibling fold was handed."""
    rules = [r for r in (rules or [])
             if int(r.get("cap") or 0) > 0
             and int(r.get("days") or 0) > 0
             and int(r.get("min_cents") or 0) > 0]
    out: dict[int, int] = {}
    for r in rules:
        for fid, cents in spend_by_window.get(int(r["days"]), {}).items():
            if cents >= int(r["min_cents"]):
                out[fid] = max(out.get(fid, 0), int(r["cap"]))
    return out


# A daily-quota rule of 0 means UNLIMITED, not "inert" — the one place the 0
# convention differs from `_spend_caps`, because a whale rule that silently did
# nothing when an operator typed 0 would ration the very fan we least want rationed.
SPEND_QUOTA_UNLIMITED = 0


def daily_quotas(spend_by_window: dict[int, dict[int, int]],
                 rules: list[dict]) -> dict[int, int]:
    """{fan_id: the biggest DAILY quota his rolling-window paid spend earns} — the
    direct answer to "don't cut off the men who pay".

    `rules` is daily_quota_by_spend — [{days, min_cents, quota}]. Same shape, windows
    and highest-wins rule as `spend_caps`, except for the 0 convention above. Fans
    with no matching rule are absent (they keep the non-payer baseline); a fan
    matching an unlimited rule maps to SPEND_QUOTA_UNLIMITED.

    Only rules with a positive window AND threshold are considered — a rule with no
    minimum would match everybody, quietly promoting every stranger to whale.

    Pure fold, same contract as `spend_caps` — see there for why the scan is the
    caller's."""
    rules = [r for r in (rules or [])
             if int(r.get("days") or 0) > 0 and int(r.get("min_cents") or 0) > 0
             and int(r.get("quota") or 0) >= 0]
    out: dict[int, int] = {}
    for r in rules:
        q = int(r.get("quota") or 0)
        for fid, cents in spend_by_window.get(int(r["days"]), {}).items():
            if cents < int(r["min_cents"]):
                continue
            # Unlimited beats every finite quota and can never be beaten back down.
            if out.get(fid) == SPEND_QUOTA_UNLIMITED:
                continue
            out[fid] = SPEND_QUOTA_UNLIMITED if q == SPEND_QUOTA_UNLIMITED \
                else max(out.get(fid, 0), q)
    return out


# ── The daily ceiling ───────────────────────────────────────────────────────────

# How long a quota day lasts once it has opened. Never a CALENDAR day: a midnight
# rollover would hand every throttled fan a fresh ration at the same instant and make
# the whole roster lurch. It opens on one of her replies instead — see `quota_used`.
QUOTA_WINDOW = timedelta(hours=24)

# …and it also closes early on this much of her silence. Twelve hours is "she went to
# bed and got on with her day": past it, the sitting that spent the ration is plainly
# over, and holding its count against him is holding yesterday against him.
QUOTA_IDLE_RESET = timedelta(hours=12)

# ±25% on every backoff rung. Enough that no two fans come back on the same schedule
# and no fan comes back on the same one twice; small enough that a 4h rung stays a
# few hours and a 72h rung stays about three days.
_QUOTA_JITTER = 0.25

# Every way the gate can come out. `held` is the only True; the rest are the distinct
# reasons she was left alone to talk, and telling them apart is the whole point of a
# shadow rollout — "we held nobody" means something very different depending on
# whether fans are sitting in the runway, riding buying signals, or serving backoff.
QUOTA_HELD = "held"                       # over quota, inside the backoff → skip
QUOTA_RUNWAY = "runway"                   # still inside his per-fan runway
QUOTA_UNDER = "under_quota"               # simply hasn't used up the base ration
QUOTA_UNLIMITED = "unlimited"             # whale, or no ceiling configured
QUOTA_SIGNAL_LIFT = "signal_lift"         # over the base ration; the buying-signal
                                          # floor is what set his ceiling
QUOTA_SPEND_LIFT = "spend_lift"           # over the base ration; his SPEND is what
                                          # set his ceiling
QUOTA_BACKOFF_SERVED = "backoff_served"   # over quota, but the silence has been served
QUOTA_NO_LADDER = "no_ladder"             # over quota with no ladder to serve
QUOTA_OFF = "off"                         # feature disabled


class _Quota(NamedTuple):
    """What the daily quota decided for one fan — the single source of truth for
    "may she answer him again yet", so the status endpoint, the audit ledger and the
    log all report the same numbers the bot ran on.

    Every field defaults, so each exit in `_quota_gate` names only what it actually
    determined instead of counting zeros into eight positions."""
    hold: bool = False          # skip this fan this tick
    quota: int = 0              # his effective replies-per-24h (0 = no ceiling)
    used: int = 0               # replies he has already had in that window
    wait_h: float = 0.0         # the backoff rung being served (hours, jittered)
    # No `rung` here on purpose — it is DERIVED, by `quota_rung(dry_h, ladder)`, from
    # two things that are both recorded. It was once carried as a field "for the
    # log/UI" and read by nothing, which is worse than absent: it makes the next
    # reader believe the shadow week answers a question it cannot. The status strip
    # now does show the rung (2026-07-28) and takes it from that function, so the
    # ladder and the name for it can never disagree — which a stored copy of a value
    # computed from a config the operator can edit could not promise.
    dry_h: float = 0.0          # hours dry (since his last money event, or first
                                # contact) AS OF her last reply — the streak that
                                # picked the rung, not the one running now
    reason: str = QUOTA_OFF     # which branch decided it (one of the QUOTA_* above)
    runway_left: int = 0        # replies of his per-fan runway still unspent
    has_paid: bool = False      # he has a money event AT ALL — the fact `dry_h` cannot
                                # carry, because a man first seen two days ago and a man
                                # who PAID two days ago both read 48. Stored because it
                                # is an INPUT, not a verdict: the window that turns it
                                # into "recent buyer" is operator config, so THAT stays
                                # derived (see the `rung` note above), never stored.
                                # Only set where `dry_h` is, for the same reason — the
                                # exits that never look at his money leave both at their
                                # defaults rather than reporting half an answer.


def quota_used(times, now: datetime, *, window: timedelta = QUOTA_WINDOW,
               idle: timedelta = QUOTA_IDLE_RESET) -> int:
    """How many replies of hers the CURRENT quota day has spent — the `used` half of
    the daily ceiling, folded over the reply times `_gather` already collected.

    The day TUMBLES. It opens on a reply of hers and is over only once BOTH of its
    rules are satisfied — `idle` since her last reply AND `window` since her first.
    Widest wins, the same fold `ppv_send._cap_release` makes across its day/week/month
    caps: two rules, take the one that asks for more. Whichever of the two is still
    outstanding keeps the day open, and until it closes the count stands.

    Requiring BOTH is what makes it a ration rather than a nap. On `idle` alone, a
    long night would hand back a fresh cap every morning and the ceiling would be a
    12-hour one wearing a day's name; on `window` alone, a day that opened at breakfast
    would reset at breakfast whether or not she was mid-conversation.

    What it must NOT be is either sliding window. Slid against `now` the count DECAYS —
    the replies that tripped the quota age out one by one, the fan reads as under quota
    again, and a 72h rung would expire in 24h. Slid against HER LAST REPLY (which is
    what this was until 2026-07-31) it never decays but it never MOVES either, because
    the anchor is a reply she is not allowed to send: the count freezes and only serving
    the whole rung ever clears it. That is the ratchet this replaces — live, fans sat at
    `used=106` against a quota of 10, dripping one reply per rung for days, and a man who
    had paid $503 in eleven days went 64 hours dark with eight of his messages unanswered.

    Note this decides the RATION ONLY, never the silence: a fan part-way through a
    backoff rung keeps serving it even after his day has turned over. `_quota_gate`
    owns that, and reads this function twice to keep the two questions apart.

    Pure, so the bot and the drawer share one number instead of two implementations of
    it; the empty history and the closed day are the same answer, 0."""
    def closed(at: datetime, opened: datetime, last: datetime) -> bool:
        """Is a day that opened at `opened` and last spoke at `last` over, as of `at`?"""
        return at - opened >= window and at - last >= idle

    start, n = None, 0
    for i, t in enumerate(times):
        if start is None or closed(t, start, times[i - 1]):
            start, n = t, 0              # the next reply landed past both rules
        n += 1
    return 0 if start is None or closed(now, start, times[-1]) else n


def quota_recent_buyer(cad: Any, *, has_paid: bool, dry_h: float) -> bool:
    """Has this fan got money on the board inside `quota_recent_buyer_days`?

    Needs BOTH facts, which is the whole reason `_Quota` carries `has_paid`: `dry_h`
    alone cannot answer it, since a man first seen two days ago and a man who PAID two
    days ago both read 48 and only one of them has earned a short leash.

    Read off the same FROZEN `dry_h` the rung is, never off `now`, so a fan cannot be
    reclassified part-way through a hold he is already serving — the same argument the
    rung and the jitter both already make one layer down.

    0 days retires the split and puts everyone back on the one long ladder, which is
    the behaviour that shipped before 2026-08-27 and the off switch for this rule."""
    days = float(cad.get("quota_recent_buyer_days") or 0)
    return bool(has_paid and days > 0 and dry_h <= days * 24.0)


def quota_ladder(cad: Any, *, recent_buyer: bool = False) -> list[float]:
    """The backoff rungs as configured, cleaned of blanks and non-positives. One
    reader, so the gate and the status copy cannot disagree about which rungs exist.

    TWO ladders, picked by `recent_buyer`. The long one is a NON-PAYER's leash and its
    shape only reads as one: the bands are as wide as their own rungs (see
    `quota_rung`), so on [4, 12, 24, 72] a mere 40h of dry time already lands a fan on
    the 72h rung. Aimed at a man who paid the day before yesterday that is a
    punishment for buying — live, a $10 fan whose spending had LIFTED his ration to
    25/day went 82.5h dark the moment he used it, 2.1 days after his purchase.

    An EMPTY ladder is legal on either side and means what it always means here:
    nothing to serve, so that cohort is never held at all."""
    key = "quota_backoff_hours_recent_buyer" if recent_buyer else "quota_backoff_hours"
    return [float(h) for h in (cad.get(key) or ())
            if float(h or 0) > 0]


def quota_rung(dry_h: float, ladder: list[float]) -> int:
    """Which rung a dry streak of `dry_h` hours stands on (0-based; -1 for no ladder).

    The ladder is CYCLIC: `dry_h` is taken modulo its total, so the longest rung is
    followed by the SHORTEST — 72h → 4h, not 72h → 72h.

    Each band is exactly as wide as its own rung, which is the counter-intuitive part
    and the reason this is worth naming: on the default [4, 12, 24, 72] the last rung
    owns 72 of the 112 hours in a lap, so ~64% of the time a held fan is found on it.
    He did not jump there — he walked the whole ladder to get there, repeatedly.

    Shared with `fan_status_copy`, which has to name the rung the gate actually
    served. `_Quota` deliberately carries no `rung` field (see its docstring), so
    this function is the single home for the arithmetic rather than a stored value
    that could go stale against the ladder it was computed from.
    """
    if not ladder:
        return -1
    phase = dry_h % sum(ladder)
    acc = 0.0
    for i, h in enumerate(ladder):
        acc += h
        if phase < acc:
            return i
    return len(ladder) - 1


def _quota_gate(c: LeashCand, *, spend_quota: int | None, money_at: datetime | None,
                tier: str, now: datetime, cad: dict) -> _Quota:
    """Item 21c — the ceiling above the burst cap. Returns a `_Quota`; `hold` is a
    cheap pre-LLM SKIP exactly like the cadence stop, never a durable pause.

    `_cadence_gate` bounds a BURST and reopens every `session_gap_minutes`, so a fan
    who keeps typing simply collects a fresh cap every hour. This bounds the DAY.

    `tier` is the classification `_cadence_gate` already made — passed in, never
    re-derived, so the two gates cannot disagree about whether a man is buying.

    Nothing applies at all until she has spent his RUNWAY (`daily_quota_free_replies`,
    100 of her replies): 84% of everyone who ever bought did so inside 25 replies and
    99% inside 100, so the ceiling only ever meets fans who have had a full run and
    bought nothing.

    The leash, in order — every step can only RAISE it, never shorten:
      • baseline `daily_quota_replies` (the stranger's ration),
      • plus `daily_quota_after_sale` if he paid within the last 24h,
      • at least `daily_quota_buying_signal` if his tier is HOT — a man reaching for
        his wallet is never the man we ration,
      • at least his spend quota, which may be UNLIMITED for a whale.

    Past the quota she goes quiet for one rung of `quota_backoff_hours`, chosen by how
    long he had been DRY when she stopped answering (no money since `money_at`, or
    since first contact if he has never paid) — chosen ONCE, at that moment, so the
    rung cannot climb under a fan who is already serving it. The ladder is CYCLIC:
    `dry_h` is taken modulo the ladder's total, so a
    fan who never pays walks 4h → 12h → 24h → 72h and then starts again at 4h rather
    than being frozen at the last rung forever.

    That cycle is commercial, not sentimental. Measured in ACTIVE chat days, the men
    who take 15-30 of them to convert are the best customers on the roster — ~$241
    lifetime against ~$74 for the ones who buy on day one. Freezing a quiet fan at
    three-day silence forever would slowly strand exactly that cohort. Any purchase
    moves the anchor and drops him straight back to rung 0.

    But that ladder is a NON-PAYER's, and a purchase resetting the anchor was never
    enough on its own: the rungs are as wide as themselves, so 40h of dry time already
    lands a man on 72h, and a fan who paid two days ago was being sent to three days
    of silence for spending. So the ladder is chosen before the rung is —
    `quota_recent_buyer` picks the short `quota_backoff_hours_recent_buyer` for anyone
    with money on the board inside `quota_recent_buyer_days`, and everyone else keeps
    the long one."""
    if not cad.get("daily_quota_enabled"):
        return _Quota(reason=QUOTA_OFF)
    used = int(c.day_out_n or 0)

    # The per-fan runway: she gets a deep chat before any ceiling applies. Counted in
    # HER replies over the life of the thread — not rows, which count both directions
    # and count bubbles (2.82 rows per reply, measured), so a row-denominated runway
    # of 100 would really be ~23 replies and would ration men still being courted.
    free = int(cad.get("daily_quota_free_replies") or 0)
    runway_left = max(free - int(c.total_out_n or 0), 0)
    if free > 0 and runway_left > 0:
        return _Quota(used=used, reason=QUOTA_RUNWAY, runway_left=runway_left)

    # Build the ceiling in named steps, remembering WHICH one set it. The ledger's
    # whole value is that attribution: "nobody was held" is worth very little if half
    # the roster is riding a signal lift, and blaming the wrong term would make the
    # shadow numbers unactionable.
    base = int(cad.get("daily_quota_replies") or 0)
    if money_at is not None and (now - money_at) <= QUOTA_WINDOW:
        base += int(cad.get("daily_quota_after_sale") or 0)
    quota, lifted_by = base, QUOTA_UNDER
    if tier in HOT_TIERS:
        signal_floor = int(cad.get("daily_quota_buying_signal") or 0)
        if signal_floor > quota:
            quota, lifted_by = signal_floor, QUOTA_SIGNAL_LIFT
    if spend_quota is not None:
        if int(spend_quota) == SPEND_QUOTA_UNLIMITED:
            return _Quota(used=used, reason=QUOTA_UNLIMITED,   # whale — never rationed
                          runway_left=runway_left)
        if int(spend_quota) > quota:
            quota, lifted_by = int(spend_quota), QUOTA_SPEND_LIFT

    # A non-positive quota means "no daily ceiling", matching `cap` in _cadence_gate.
    if quota <= 0:
        return _Quota(used=used, reason=QUOTA_UNLIMITED, runway_left=runway_left)
    # Two different questions, and conflating them is what this gate got wrong.
    #   `used`  — what TODAY's ration has left, which is what she may spend next.
    #   `spent` — what she had spent WHEN SHE WENT QUIET, which is what put her on a
    #             rung. It is the ration's own final count and never moves while she
    #             is silent, so a fan whose day turns over mid-backoff keeps serving
    #             the rung he was given: the day resets his ALLOWANCE, never his wait.
    # They are equal until a day closes under him, which is exactly when it matters.
    spent = int(c.day_out_n_at_stop or 0)
    if spent < quota or c.her_last_at is None:
        # Only credit a lift when the base ration would NOT have covered him anyway.
        reason = lifted_by if used >= base else QUOTA_UNDER
        return _Quota(quota=quota, used=used, reason=reason, runway_left=runway_left)

    # Dry streak → which LADDER, then which rung of it. `first_at` stands in for a fan
    # who has never paid at all; with neither anchor we cannot judge him, so he starts
    # at the shortest rung.
    #
    # Measured to HER LAST REPLY — the moment the hold began — and NOT to `now`. The
    # hold is served from that same frozen anchor, so reading the streak at `now`
    # lets the rung climb while the fan is already waiting on it, and his release
    # date runs away from him: live, a fan on the 24h rung was recomputed onto the
    # 72h one a day into his own hold and had his return pushed out 36.7 hours,
    # having done nothing but wait. Worse, it is self-reinforcing — she is silent, so
    # he cannot buy, so the streak he is being judged on can only grow.
    #
    # It is the argument the jitter below already makes, one step earlier: a wait
    # that re-rolls every sweep is not a wait. Both halves of it must hold still.
    anchor = money_at or c.first_at
    dry_h = (max((c.her_last_at - anchor).total_seconds() / 3600.0, 0.0)
             if anchor else 0.0)
    has_paid = money_at is not None

    # WHICH ladder, before which rung of it. Money on the board inside
    # `quota_recent_buyer_days` puts him on the short one; everyone else walks the long
    # one. Derived here rather than stored, from the two facts `_Quota` does carry, so
    # `fan_status_copy` reaches the same verdict and an operator editing the window
    # moves the gate and the copy together.
    #
    # It is a CLIFF, deliberately. The first reply after he crosses the window drops him
    # onto the long ladder at whatever rung his streak has already reached — rung 3, on
    # the default shape, for anything past 40h dry. There is no glide between them, and
    # there should not be a third ladder to build one: widen the window if it lands too
    # hard.
    ladder = quota_ladder(cad, recent_buyer=quota_recent_buyer(
        cad, has_paid=has_paid, dry_h=dry_h))
    if not ladder:                                   # no ladder ⇒ nothing to serve
        return _Quota(quota=quota, used=used, reason=QUOTA_NO_LADDER,
                      dry_h=dry_h, has_paid=has_paid, runway_left=runway_left)

    cycle_h = sum(ladder)
    rung = quota_rung(dry_h, ladder)
    # Jitter, because a woman who drifts back after EXACTLY 24.0 hours, every time, on
    # every fan, is a cron job wearing her name. Seeded on (fan, rung, cycle) so it is
    # stable across ticks — a wait that re-rolled each sweep would let a fan slip
    # through the moment the dice came up short — while still differing per fan, per
    # rung, and on each new lap of the ladder.
    jitter = Random(f"{c.fan_id}:{rung}:{int(dry_h // cycle_h)}").uniform(
        1.0 - _QUOTA_JITTER, 1.0 + _QUOTA_JITTER)
    wait_h = ladder[rung] * jitter
    hold = (now - c.her_last_at) < timedelta(hours=wait_h)
    # `backoff_served` means what it says — over quota, but the silence is done, so
    # this reply is on credit. A fan whose DAY turned over while he waited is not on
    # credit: he is simply under quota again, and labelling his fresh ration as
    # served backoff would make the ledger read as a roster on permanent drip.
    if hold:
        reason = QUOTA_HELD
    elif used >= quota:
        reason = QUOTA_BACKOFF_SERVED
    else:
        reason = lifted_by if used >= base else QUOTA_UNDER
    return _Quota(hold=hold, quota=quota, used=used, wait_h=wait_h,
                  dry_h=dry_h, has_paid=has_paid, runway_left=runway_left,
                  reason=reason)


async def _write_quota_audit(account_id: str, rows: list[tuple[int, _Quota]], *,
                             enforced: bool, now: datetime) -> None:
    """Upsert one run's quota verdicts into `quota_audit` — ONE statement, whatever the
    fan count, so the ledger can never become the reason a sweep is slow.

    Called at the END of run() rather than inside the candidate loop on purpose: a
    write per fan per tick would put the audit trail on the hot path of the thing it is
    auditing, and a slow sweep starves live OF sends through the same thread pool.

    Failures are swallowed and logged. This is a ledger for a rollout, not a source of
    truth for anything the bot does — losing a row must never cost a reply."""
    if not rows:
        return
    day = now.strftime("%Y-%m-%d")
    # Collapse this run's verdicts per (fan, reason): a fan evaluated twice in one
    # sweep is +2, not two single-row upserts racing each other. Later entries win the
    # snapshot columns, which is what "last verdict" means.
    counts = Counter((fan_id, q.reason) for fan_id, q in rows)
    latest = {(fan_id, q.reason): q for fan_id, q in rows}
    payload = []
    for (fan_id, reason), n in counts.items():
        q = latest[(fan_id, reason)]
        payload.append(
            {"account_id": str(account_id), "fan_id": int(fan_id), "day": day,
             "reason": reason, "n": n, "enforced": bool(enforced),
             "quota": int(q.quota), "used": int(q.used),
             "runway_left": int(q.runway_left),
             "wait_h": float(q.wait_h), "dry_h": float(q.dry_h), "updated_at": now})
    stmt = sqlite_insert(QuotaAudit).values(payload)
    stmt = stmt.on_conflict_do_update(
        # `enforced` is in the key: a shadow row and an enforced row for the same
        # (fan, day, reason) are DIFFERENT rows, so flipping the flag mid-day cannot
        # relabel the morning's shadow evaluations. It is therefore not in `set_`.
        index_elements=["account_id", "fan_id", "day", "reason", "enforced"],
        set_={"n": QuotaAudit.n + stmt.excluded.n,
              "quota": stmt.excluded.quota,
              "used": stmt.excluded.used,
              "runway_left": stmt.excluded.runway_left,
              "wait_h": stmt.excluded.wait_h,
              "dry_h": stmt.excluded.dry_h,
              "updated_at": stmt.excluded.updated_at})
    try:
        async with get_session() as s:
            await s.execute(stmt)
            await s.commit()
    except Exception:
        log.exception("leash[%s]: quota_audit write failed (%d rows) — the verdicts "
                      "still stand, only the ledger row is lost",
                      account_id, len(payload))


# ── Both leashes, for ONE fan, in the engine's order ─────────────────────────────
class LeashReading(NamedTuple):
    """What the two gates decided about one fan, plus the numbers behind it.

    Exists so a caller can ASK the leash rather than re-run it. The status endpoint
    used to inline this whole pipeline — eight calls in a specific order, with the
    burst gate's `tier` threaded into the quota gate — which is how the drawer ended
    up disagreeing with the bot twice: once by re-deriving the buying-signal tier
    without the pic term, and once by passing `pic=False` outright."""
    tier: str
    used: int
    cap: int
    stopped: bool
    last_message_at: datetime | None
    quota: _Quota | None        # None when the daily ceiling is off for this account


async def read_leash(account_id: str, fan_id: int, c: LeashCand, *,
                     cfg: dict, now: datetime,
                     pending: ContentOffer | None) -> LeashReading:
    """Run the burst cap and the daily ceiling for ONE fan and report the verdict.

    The order is the engine's and is load-bearing: `_cadence_gate` classifies the
    signal tier, and `_quota_gate` CONSUMES that tier rather than re-deriving it —
    two derivations of "is this man buying" is precisely the drift `_leash` was
    extracted to end.

    Deliberately per-fan and un-batched, unlike `run()`, which resolves the same
    signals for a whole sweep in one query each. This serves a 15s drawer poll for
    one open chat, where the fan set is exactly one and batching buys nothing; the
    SHARED thing is the gates and their ordering, not the fetch strategy. Reuses the
    caller's `_gather` result for the same reason — that scan is the expensive part
    and the endpoint already needs it for the offer gate.
    """
    payers = await recent_payer_fans(account_id, [int(fan_id)])
    money = (await _last_money_at(account_id, [int(fan_id)])).get(int(fan_id))

    # Item 21b's proven-spender floor and 21c's spend lift come off ONE scan over the
    # union of their windows — two awaits over the same fan and overlapping windows
    # was two sessions and eight group-bys on a 15s poll.
    quota_on = bool(cfg.get("daily_quota_enabled"))
    cap_rules = cfg.get("msg_limits_by_spend") or []
    quota_rules = (cfg.get("daily_quota_by_spend") or []) if quota_on else []
    windows = spend_windows(cap_rules, quota_rules)
    spend_by_window = (await _paid_spend_by_window(
        account_id, [int(fan_id)], windows, now) if windows else {})

    stop, tier, cap = _cadence_gate(
        c, pending=pending, recent_payer=int(fan_id) in payers, money_at=money,
        pic=bool(c.pic_sent), now=now, cad=cfg,
        spend_cap=spend_caps(spend_by_window, cap_rules).get(int(fan_id), 0))

    quota = None
    if quota_on:
        quota = _quota_gate(
            c, spend_quota=daily_quotas(spend_by_window, quota_rules).get(int(fan_id)),
            money_at=money, tier=tier, now=now, cad=cfg)

    return LeashReading(tier=tier, used=int(c.session_out_n or 0), cap=cap,
                        stopped=bool(stop), quota=quota,
                        last_message_at=c.last_in_at or c.last_out_at)
