"""service/automations/sell_lane.py — the one gate every priced send passes through.

## Why this exists

`pack_sender.send_pack_on_ask` is a complete sale in one call and takes no
`ai_chatter` state: read the ask off the thread, resolve media from the curated
vault folder at send time, audit the caption against what is attached, price it,
send it. That made it look like any engine could just call it.

It cannot. Every guard that makes a priced send *safe* was evaluated in
`ai_chatter.run()`'s per-fan loop, ABOVE the single call site — never inside the
lane. A second caller wired straight to `pack_sender` would enter underneath all
of them, and the failure is not theoretical: three engines answering one fan in
the same minute is three PPVs to a man who just said he is broke, each one
individually "correct", none aware of the others.

## The shape

One object per run, per engine:

    lane = await sell_lane.for_run(account_id, engine="autoreply")
    ...
    if await lane.sell(client, fan_id, turn, fan=f):
        ...one line to stamp "we spoke this turn"...
    ...
    return {..., **lane.stats}

`lane` holds the config, the permission, the per-run counter snapshot, the tick
budget and the tally, so a calling engine adds four lines and no state of its own.
That matters more than it looks: the first version of this module was a 35-line
block copy-pasted into two engines, and the copies had already begun to disagree.

The closer takes the same sale in two halves, because it owes him a REPLY first:

    plan = await lane.plan(fan_id, turn, fan=f)      # decides; sends nothing
    ...write the reply, ship it...
    if plan is not None:
        await lane.deliver(client, plan)             # the priced box, second

`sell` is exactly those two calls back to back, and it stays the whole story for
Auto Convo and OF AI Chat: they sell INSTEAD of replying, so they have nothing to
put in front of it.

## The brakes, and why none of them can be skipped

`may_sell` is the only place they are evaluated, and there is no argument that
widens it. `blocked` (the caller's own verdict) can only narrow; `engine` is
recorded for logging and nothing else. There is deliberately no `skip_gate`: a
caller that has already qualified the fan simply pays for one more indexed read,
which is cheaper than a parameter that can turn the gate off.

🚨 EVERY TEXT PREDICATE GOES THROUGH `_language`. The engines arm the lane with
`_language.is_content_ask(text, lang)`, so a brake reading the English-only
detector would fire on a different set of messages than the trigger did — on an
`es` account the lane would open and the spend-regret brake would never close.
`detect_companion_intent` is the one exception: it has no Spanish form anywhere
yet, and `ai_chatter` calls the English one directly, so matching it here keeps
one behaviour rather than inventing a second.

## The catalog decoupling

The vault ask lane never reads `catalog_items` — it resolves media from the
operator-curated vault folders. But in `ai_chatter` its trigger rode `gate_ok`,
and `gate_ok` is only ever set inside a branch guarded by

    elif (catalog_items or v.sell_customs) and await _offer_caps_ok(...)

…so an account with a thin or empty catalog could not fire the vault lane AT ALL,
however plainly the fan asked. Measured 2026-08-15: the ask lane produced 2 offers
fleet-wide in 14 days while the blind cheapest-catalog-item pick produced 67. The
qualification bar was never the problem — the accidental inventory coupling was.

`qualify()` asks about the FAN (is he live, is he braked, is he paced), not about
what happens to be on a shelf. So it is evaluated here, on its own, and the vault
lane no longer needs a catalog to exist. That is also what makes the lane survive
catalog selling being removed from the chat prompt entirely.

## One shelf, one set of caps

Every number this module enforces is imported from `ai_chatter`, never restated:
`_MAX_OPEN_OFFERS`, `_open_offers`, `_unlocked_since_open_offers`,
`_offer_caps_ok`, `_paid_cents_7d`, `_ask_counters`. The first draft of this file
re-derived two of them and both had already drifted — the open-offer ceiling
disagreed at `max_open_offers=1`, and the re-derived count dropped the
unlocked-slot lift, so the seam refused fans the closer would have served. Two
copies of a cap is two caps; the imports are the fix and they are load-bearing.

## What this module does NOT do

It does not pick media, name a price, or write a caption — `content_resolver` and
`pack_sender` own that, including the adversarial VERIFY that refuses on doubt. A
verdict from here means "you may make an ask"; the lane may still refuse, loudly,
and that refusal is the honest answer.
"""
from __future__ import annotations

import dataclasses
import logging
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import select

from db.engine import get_session
from db.models import Fan, LadderState

from . import _customs, _language, upsell

if TYPE_CHECKING:                    # runtime import is lazy — see `may_sell`
    from . import pack_sender

log = logging.getLogger("of-relay.automation.sell_lane")

# Mirrors ai_chatter's own per-run ceiling. The FIRST offer to a never-offered fan
# is not paced by `_offer_caps_ok` (its min-msgs branch is skipped when he has no
# prior ContentOffer), so on the tick a floor is first enabled EVERY long-standing
# fan trips at once — ~1,283 on one roster. That is the burst shape the per-hour
# cap exists to prevent and the per-run snapshot cannot see.
DEFAULT_TICK_LIMIT = 3

# Reasons. Stable strings — they are logged, counted, and asserted on in tests.
OK = "ok"
R_DISABLED = "disabled"
R_NOT_ARMED = "not_armed"
R_CUSTOM_OWED = "custom_owed"
R_DECLINED = "declined"
R_SPEND_REGRET = "spend_regret"
R_COMPANION = "companion"
R_COOLDOWN = "cooldown"
R_STOPPED = "hard_stop"
R_TAPPED = "tapped"
R_PAUSED = "offers_paused"
R_SPEND_CAP_7D = "spend_cap_7d"
R_MAX_OPEN = "max_open_offers"
R_CAPS = "caps"
R_TICK_BUDGET = "tick_budget"
R_CALLER = "caller_brake"

# `pack_sender` reports a would-send as "dry_run". Both mean "he got an ask", and
# the string test was repeated at four call sites before this constant existed.
_SOLD_STATUSES = ("ok", "dry_run")

# ── The refusal memo ────────────────────────────────────────────────
#
# 🚨 MEASURED ON PROD 2026-08-15, and it is why this exists. One fan on one
# account: the SAME message — "Can I see the pussy play mommy?" — was read,
# matched and verified 580 times over 44 hours. 1,740 LLM calls. Zero offers, ever.
# The resolver did its job every time (602 of 607 VERIFY calls fleet-wide kept
# media); `pack_sender` refused downstream every time, nothing recorded that, and
# the next tick started over on the identical text.
#
# Nothing between the ticks can change the answer: same message, same shelf, same
# refusal. So a refusal is remembered against the inbound it refused, and the lane
# does not pay for it twice. He speaks again ⇒ new timestamp ⇒ a fresh attempt.
#
# In-process and bounded, deliberately. It needs no migration, and a relay restart
# re-arming one attempt per fan is the correct failure direction — the cost of
# being wrong is three cheap calls, and the cost of a durable wrong answer is a
# fan who can never be sold to again. Same shape as the other in-process caches
# in this package.
_MEMO_MAX = 4096
_refusal_memo: dict[tuple[str, int], tuple[datetime | None, str]] = {}


def _memo_get(account_id: str, fan_id: int, at: datetime | None) -> str | None:
    """The reason we refused this exact inbound last time, if we did. `at is None`
    never matches: a turn with no timestamp cannot be told apart from the next one."""
    if at is None:
        return None
    prev = _refusal_memo.get((str(account_id), int(fan_id)))
    return prev[1] if prev is not None and prev[0] == at else None


def _memo_put(account_id: str, fan_id: int, at: datetime | None, reason: str) -> None:
    if at is None:
        return
    if len(_refusal_memo) >= _MEMO_MAX:
        _refusal_memo.clear()      # a bounded cache, not an LRU — this is a hint
    _refusal_memo[(str(account_id), int(fan_id))] = (at, reason)


# Gate refusals that HIS OWN EXPLICIT ASK lifts. All four say the same thing —
# "this thread does not look ready" — and a man who just typed "send me a video"
# has disproved it himself. Sending nothing on a direct ask is the worst outcome
# there is, which is why `ai_chatter` grew an `ask_override` for exactly this.
#
# ⚠️ NARROWER than that override, deliberately. It bypassed the gate wholesale,
# including `account_hourly` / `account_daily` — and a burst of priced sends inside
# one hour is the OF-session shape those two exist to prevent, so an ask cannot buy
# its way past them. The DECISIONS (hard_stop / tapped / declined) are final for
# the same reason they always were, and `offers_paused` already has its own
# fan-pull lift inside `qualify`.
_PULL_LIFTS = frozenset({"low_information", "we_spoke_last", "stale", "rung_gap"})


@dataclasses.dataclass(frozen=True)
class Turn:
    """His side of this turn, as the seller needs to see it.

    One value instead of five positional arguments threaded through three layers.
    `lang` is not decoration: it selects which detector every brake reads, and the
    engines already resolve it per fan for their own prompt.
    """
    text: str | None
    at: datetime | None = None
    our_last_at: datetime | None = None
    fan_spoke_last: bool = True
    lang: str = "en"


@dataclasses.dataclass(frozen=True)
class AskCounters:
    """The per-run ask snapshot `upsell.qualify` paces against.

    A caller that already holds it (ai_chatter reads it once at run start) passes
    it in rather than letting this module re-query per fan: two snapshots of the
    same counters can disagree, and disagreeing caps are the failure this module
    exists to prevent.
    """
    asks_today: int = 0
    account_hour: int = 0
    account_day: int = 0


@dataclasses.dataclass(frozen=True)
class SellVerdict:
    """May this caller put a price in front of this fan right now, and why not."""
    may: bool
    reason: str

    def __bool__(self) -> bool:      # `if verdict:` reads the way it should
        return self.may


@dataclasses.dataclass(frozen=True)
class SellResult:
    """What came of it. Typed, because `res.get("status") in ("ok", "dry_run")` was
    the contract at four call sites and a string comparison is not a contract."""
    sold: bool
    reason: str
    n: int | None = None
    price_cents: int | None = None
    category: str | None = None

    def __bool__(self) -> bool:
        return self.sold

    @classmethod
    def refused(cls, reason: str) -> "SellResult":
        return cls(False, reason)

    @classmethod
    def from_pack(cls, res: dict) -> "SellResult":
        return cls(sold=res.get("status") in _SOLD_STATUSES,
                   reason=str(res.get("reason") or res.get("status") or ""),
                   n=res.get("n"), price_cents=res.get("price_cents"),
                   category=res.get("category"))


@dataclasses.dataclass(frozen=True)
class SellPlan:
    """The outcome of DECIDING: a sale waiting for the wire, or the refusal.

    Carries the fan and the turn it was decided for so `deliver` can count and
    memo it without the caller threading either back — the same reason `Turn`
    exists. Holding one costs nothing and dropping one sends nothing, which is
    what lets `ai_chatter` abandon a turn (he wrote again, the reply failed) after
    the resolver has already run.

    A REFUSAL IS A PLAN TOO — `delivery is None`, `result` holds the reason, and
    it has already been counted and memoed. That is what makes `sell` exactly
    `deliver(plan(...))` rather than a second copy of the same sequence with its
    own error handling, and it is the same shape `pack_sender.PackPlan` uses one
    layer down.
    """
    delivery: "pack_sender.Delivery | None"
    fan_id: int
    turn: Turn
    result: SellResult | None = None      # set iff `delivery is None`

    def __bool__(self) -> bool:
        return self.delivery is not None


class TickBudget:
    """One account-level ceiling on forced asks for this run.

    Bounds the burst `_offer_caps_ok` cannot see: it does not pace a fan's FIRST
    offer, so the tick a floor is first enabled, every never-offered fan on the
    roster trips at once.
    """

    __slots__ = ("limit", "spent")

    def __init__(self, limit: int = DEFAULT_TICK_LIMIT) -> None:
        self.limit = int(limit)
        self.spent = 0

    def available(self) -> bool:
        return self.spent < self.limit

    def charge(self) -> None:
        self.spent += 1


def _live(ts: datetime | None, now: datetime) -> bool:
    return ts is not None and ts > now


async def _load_fan(account_id: str, fan_id: int) -> Fan | None:
    async with get_session() as s:
        row = await s.get(Fan, (str(account_id), int(fan_id)))
        if row is not None:
            s.expunge(row)
        return row


async def _load_ladder(account_id: str, fan_id: int) -> LadderState | None:
    async with get_session() as s:
        row = (await s.execute(
            select(LadderState).where(LadderState.account_id == str(account_id),
                                      LadderState.fan_id == int(fan_id))
        )).scalars().first()
        if row is not None:
            s.expunge(row)
        return row


class SellLane:
    """The seller, bound to one account and one calling engine for one run.

    Construct with `for_run`. Holds the config, the permission, the counter
    snapshot, the tick budget and the tally — so an engine that wants to sell adds
    a construction line, a call, and `**lane.stats`, and owns no seller state.
    """

    def __init__(self, account_id: str, engine: str, cfg: dict, *,
                 permission_key: str | None = None,
                 budget: TickBudget | None = None) -> None:
        self.account_id = str(account_id)
        self.engine = str(engine)
        self.cfg = cfg
        self.budget = budget or TickBudget()
        self.sold = 0
        self.refused = 0
        self._counters: AskCounters | None = None
        self._by_fan: dict[int, int] = {}
        # The shelf switches are the master; the per-engine permission is the
        # trigger. A trigger without its master is inert by design, so both are
        # folded into one boolean the caller can read.
        armed = bool(cfg.get("pack_send_enabled") and cfg.get("pack_on_ask_enabled"))
        self.on = armed and (permission_key is None
                             or bool(cfg.get(permission_key)))

    # ── read-only predicates ────────────────────────────────────────

    def is_ask(self, turn: Turn) -> bool:
        """Did he ask to SEE something this turn, in HIS language?"""
        return _language.is_content_ask(turn.text, turn.lang)

    @property
    def stats(self) -> dict:
        return {"sold": self.sold, "sell_refused": self.refused}

    async def counters(self) -> AskCounters:
        """The per-RUN ask snapshot, fetched once and reused. A caller with its own
        snapshot passes it to `qualify` instead — see `AskCounters`."""
        if self._counters is None:
            from .ai_chatter import _ask_counters
            by_fan, hour, day = await _ask_counters(self.account_id,
                                                    datetime.utcnow())
            self._by_fan = {int(k): int(v) for k, v in (by_fan or {}).items()}
            self._counters = AskCounters(account_hour=int(hour),
                                         account_day=int(day))
        return self._counters

    async def _counters_for(self, fan_id: int) -> AskCounters:
        base = await self.counters()
        return dataclasses.replace(base,
                                   asks_today=self._by_fan.get(int(fan_id), 0))

    # ── the gate ────────────────────────────────────────────────────

    async def may_sell(self, fan_id: int, turn: Turn, *,
                       fan: Fan | None = None, blocked: bool = False,
                       now: datetime | None = None) -> SellVerdict:
        """Every durable brake, evaluated read-only. NEVER writes, NEVER sends.

        `blocked` is the caller's OWN verdict (ai_chatter passes `seller_off`). It
        can only narrow. There is no parameter that widens, and `self.engine` is
        used for logging alone.

        Read-only is load-bearing. The write-side consequences of a decline — the
        24h offers-pause, the COMPANION flip, the make-right apology — belong to
        the engine that owns the conversation, and a gate that quietly re-stamped
        them would make two engines fight over one fan's ladder.
        """
        now = now or datetime.utcnow()
        if not self.on:
            return SellVerdict(False, R_DISABLED)
        if blocked:
            return SellVerdict(False, R_CALLER)
        if not self.budget.available():
            return SellVerdict(False, R_TICK_BUDGET)

        # A paid-for custom that has not shipped outranks every selling surface. He
        # already gave us money for something he has not received; asking for more
        # is the single worst thing this system can do. No timeout, by operator
        # ruling — the marker is cleared by delivery, not by the clock.
        if fan is None:
            fan = await _load_fan(self.account_id, fan_id)
        if _customs.is_owed(fan):
            return SellVerdict(False, R_CUSTOM_OWED)

        # Signals off his latest message. All three refuse, so the ORDER only picks
        # which reason gets logged — and the most specific one is the most useful
        # to whoever reads it. That is why this is not ai_chatter's order: there,
        # HARD must win because the two carry different CONSEQUENCES (a 72h pause
        # and a make-right apology vs a 24h pause), and this module writes none.
        #
        # ⚠️ `is_hard_decline` is misnamed for `en`: it returns true for ANY decline
        # (`classify_decline(...) is not None`), hard, soft or bare-no. That is
        # exactly what a SELL gate wants — ai_chatter stops selling on all three —
        # so it is used deliberately, not by mistake.
        text, lang = turn.text or "", turn.lang
        if _language.detect_spend_regret(text, lang):
            return SellVerdict(False, R_SPEND_REGRET)
        if upsell.detect_companion_intent(text):   # no es form anywhere yet
            return SellVerdict(False, R_COMPANION)
        if _language.is_hard_decline(text, lang):
            return SellVerdict(False, R_DECLINED)

        lad = await _load_ladder(self.account_id, fan_id)
        if lad is not None:
            if lad.status == upsell.STATUS_STOPPED:
                return SellVerdict(False, R_STOPPED)
            if lad.status == upsell.STATUS_TAPPED:
                return SellVerdict(False, R_TAPPED)
            if lad.status == upsell.STATUS_COMPANION:
                return SellVerdict(False, R_COMPANION)
            if _live(lad.companion_until, now):
                return SellVerdict(False, R_COMPANION)
            if _live(lad.cooldown_until, now):
                return SellVerdict(False, R_COOLDOWN)
            # The broke pause yields to HIS OWN pull and nothing else — the measured
            # realness test (37/37 broke-then-buyers bought a FRESH offer).
            if (_live(lad.offers_paused_until, now)
                    and not _language.is_fan_pull(text, lang)):
                return SellVerdict(False, R_PAUSED)

        # Money brakes. Imported lazily: ai_chatter imports this module, so a
        # top-level import would be a cycle. Every number below is ai_chatter's own
        # — see the module docstring on why none of them is restated here.
        from .ai_chatter import (
            _MAX_OPEN_OFFERS, _offer_caps_ok, _open_offers, _paid_cents_7d,
            _unlocked_since_open_offers,
        )

        cap7 = int(self.cfg.get("spend_velocity_cap_7d_cents")
                   or upsell.SPEND_VELOCITY_CAP_7D)
        if cap7 and await _paid_cents_7d(self.account_id, fan_id, now) >= cap7:
            return SellVerdict(False, R_SPEND_CAP_7D)

        max_open = max(_MAX_OPEN_OFFERS,
                       int(self.cfg.get("max_open_offers") or 0))
        open_offers = await _open_offers(self.account_id, fan_id)
        open_count = len(open_offers)
        # An UNTRACKED unlock (tip_reward box, a tip, a hand-sent PPV) resolves no
        # offer, so without this lift the ceiling stays full and the seam gags
        # itself on a fan who has just paid.
        if open_count and await _unlocked_since_open_offers(
                self.account_id, fan_id, open_offers):
            open_count -= 1
        if open_count >= max_open:
            return SellVerdict(False, R_MAX_OPEN)

        if not await _offer_caps_ok(self.account_id, fan_id, self.cfg):
            return SellVerdict(False, R_CAPS)

        return SellVerdict(True, OK)

    async def qualify(self, fan_id: int, turn: Turn, *,
                      counters: AskCounters | None = None,
                      human_ask_at: datetime | None = None,
                      now: datetime | None = None) -> tuple[bool, str]:
        """`upsell.qualify` for a caller with no catalog — the gate, decoupled from
        inventory. See the module docstring for why that decoupling is the point.

        `human_ask_at` is a teammate's live PPV, which is an ask ALREADY IN FRONT
        OF HIM and paces ours. Callers that hold the thread signal pass it; without
        it we can stack a second price on top of an unpaid $45 seconds after a
        human quoted it.
        """
        now = now or datetime.utcnow()
        if not self.cfg.get("qualification_gate_enabled"):
            return True, "gate_off"

        pull = _language.is_fan_pull(turn.text or "", turn.lang)
        lad = await _load_ladder(self.account_id, fan_id)
        cnt = counters or await self._counters_for(fan_id)
        if human_ask_at is None:
            from .ai_chatter import _NO_MONEY, _human_money_signals
            human_ask_at = (await _human_money_signals(
                self.account_id, [int(fan_id)], now)).get(
                    int(fan_id), _NO_MONEY).ask
        last_ask = max((t for t in ((lad.last_ask_at if lad is not None else None),
                                    human_ask_at) if t is not None), default=None)

        ok, why = upsell.qualify(upsell.GateCtx(
            fan_id=int(fan_id),
            last_inbound_text=turn.text,
            last_inbound_at=turn.at,
            last_outbound_at=turn.our_last_at,
            fan_spoke_last=turn.fan_spoke_last,
            status=(lad.status if lad is not None else upsell.STATUS_IDLE),
            offers_paused_until=(lad.offers_paused_until if lad is not None else None),
            last_ask_at=last_ask,
            asks_today=cnt.asks_today,
            # Per-fan daily CENTS is stamped with a creator-local day this caller
            # cannot resolve, and reading it against the wrong day never resets —
            # blocking a fan for life. A stale day is a zero; the three ceilings
            # above still bound the day.
            daily_ask_cents=0,
            account_offers_last_hour=cnt.account_hour,
            account_offers_today=cnt.account_day,
            ladder_may_open=True,
            fan_pull=pull,
        ), now)
        if not ok and pull and why in _PULL_LIFTS:
            log.info("sell_lane explicit-ask lifts %s engine=%s account=%s fan=%s",
                     why, self.engine, self.account_id, fan_id)
            return True, f"pull_lifted:{why}"
        return ok, why

    # ── the send ────────────────────────────────────────────────────

    async def plan(self, fan_id: int, turn: Turn, *,
                   fan: Fan | None = None, blocked: bool = False,
                   counters: AskCounters | None = None,
                   human_ask_at: datetime | None = None) -> SellPlan:
        """Everything `sell` does EXCEPT the wire. Charges nothing, sends nothing.

        For the caller who has something to say between deciding and sending —
        `ai_chatter`, which owes him the ANSWER to his question before the priced
        box that answers it. Hand the result back to `deliver` once the reply has
        landed; drop it on the floor and nothing happened.

        Always returns a plan; a falsy one is a refusal, already counted.
        """
        refusal = await self._gate(fan_id, turn, fan=fan, blocked=blocked,
                                   counters=counters, human_ask_at=human_ask_at)
        if refusal is not None:
            return SellPlan(None, fan_id, turn, refusal)
        from . import pack_sender
        d, refused = await pack_sender.plan_on_ask(self.account_id, fan_id,
                                                   cfg=self.cfg)
        if d is None:
            res = SellResult.from_pack(refused)
            self._lane_refused(fan_id, turn, res)
            return SellPlan(None, fan_id, turn, res)
        return SellPlan(d, fan_id, turn)

    async def deliver(self, client, plan: SellPlan, *,
                      voice_line: str | None = None,
                      dry_run: bool = False) -> SellResult:
        """Put a decided `SellPlan` on the wire, and count it. THE charge.

        A refused plan passes straight through with its reason — it was counted
        where it was decided, so this can be called unconditionally.

        The brakes are NOT re-evaluated. They were read against this turn, and the
        only thing that moves between the plan and the reply landing is the vault
        itself — which `pack_sender`'s re-audit checks at the wire, where a stale
        folder becomes a lie. Re-running the gate here would instead mean the
        engine's own reply (an outbound, seconds old) could close a brake that was
        open when we decided, and he would get his answer with the content he
        asked for silently dropped.
        """
        # `is not None`, NOT `or` — a refusal `SellResult` is FALSY by design
        # (`__bool__` is "did he get it"), so `plan.result or …` would discard
        # every real reason and report `disabled` for all of them.
        if plan.result is not None:
            return plan.result
        if plan.delivery is None:                      # unreachable by `plan()`
            return SellResult.refused(R_DISABLED)
        from . import pack_sender
        res = SellResult.from_pack(await pack_sender.deliver(
            client, plan.delivery, voice_line=voice_line, dry_run=dry_run))
        if res.sold:
            self._sold(plan.fan_id, res)
        else:
            self._lane_refused(plan.fan_id, plan.turn, res)
        return res

    async def _gate(self, fan_id: int, turn: Turn, *,
                    fan: Fan | None, blocked: bool,
                    counters: AskCounters | None,
                    human_ask_at: datetime | None) -> SellResult | None:
        """Permission → he asked → memo → brakes → gate. The refusal, or None."""
        if not self.on:
            return SellResult.refused(R_DISABLED)
        if not self.is_ask(turn):
            return SellResult.refused(R_NOT_ARMED)
        memo = _memo_get(self.account_id, fan_id, turn.at)
        if memo is not None:
            return SellResult.refused(memo)

        verdict = await self.may_sell(fan_id, turn, fan=fan, blocked=blocked)
        ok, why = (await self.qualify(fan_id, turn, counters=counters,
                                      human_ask_at=human_ask_at)
                   if verdict.may else (False, verdict.reason))
        if ok:
            return None
        self.refused += 1
        log.info("sell_lane refused engine=%s account=%s fan=%s: %s",
                 self.engine, self.account_id, fan_id, why)
        return SellResult.refused(why)

    def _sold(self, fan_id: int, res: SellResult) -> None:
        self.sold += 1
        self.budget.charge()
        log.info("sell_lane SOLD engine=%s account=%s fan=%s %s n=%s px=%s",
                 self.engine, self.account_id, fan_id, res.category, res.n,
                 res.price_cents)

    def _lane_refused(self, fan_id: int, turn: Turn, res: SellResult) -> None:
        self.refused += 1
        # Remember it against THIS inbound. The brakes in `_gate` are not memoed —
        # they are time- and state-based and flip on their own; only the lane's
        # own refusal is a function of the message, which is what makes it safe
        # to answer from memory.
        _memo_put(self.account_id, fan_id, turn.at, res.reason)
        log.info("sell_lane lane refused engine=%s account=%s fan=%s: %s",
                 self.engine, self.account_id, fan_id, res.reason)

    async def sell(self, client, fan_id: int, turn: Turn, *,
                   fan: Fan | None = None, blocked: bool = False,
                   counters: AskCounters | None = None,
                   human_ask_at: datetime | None = None,
                   dry_run: bool = False) -> SellResult:
        """He asked for content → sell him that, from the vault. THE entry point.

        permission → he asked → brakes → gate → `pack_sender.plan_on_ask` → wire.
        Counts itself, logs itself; the caller reads the truthy result and stamps
        its own "we spoke this turn".

        🚨 THE CALLER MUST ALREADY HOLD THE FAN LEASE. All three calling engines
        take `ax.acquire_fan_lease` at the top of their per-fan body and hold it
        through the turn, so the cross-engine mutex the fan-out needs already
        exists and it is theirs. This module deliberately has no lease parameter:
        `acquire_fan_lease` is not re-entrant (a live lease blocks every kind, its
        own holder included), so acquiring here would refuse every real call, and
        releasing here would drop the caller's lease mid-turn and let a second
        engine in while the first is still typing.

        The ask detector runs HERE for every caller, including the ones that
        already tested it — all three arm on `_language.is_content_ask(text, lang)`
        and this is that same call, so an opt-out parameter would buy one saved
        regex match at the price of a way past the trigger.

        `plan` + `deliver` back to back, and nothing else — a refused plan carries
        its own reason through `deliver`, so there is no second copy of the
        sequence here to drift from the two-phase one. A caller that owes him a
        reply first calls the halves itself; one that has nothing to say in
        between (both keep-warm engines sell INSTEAD of replying) calls this.
        """
        return await self.deliver(
            client,
            await self.plan(fan_id, turn, fan=fan, blocked=blocked,
                            counters=counters, human_ask_at=human_ask_at),
            dry_run=dry_run)


async def for_run(account_id: str, *, engine: str,
                  permission_key: str | None = None,
                  cfg: dict | None = None,
                  budget: TickBudget | None = None) -> SellLane:
    """Build the lane for one engine's run. One config read.

    The config is the CLOSER's (`ai_chatter_config_json`) and so is the permission
    key, because everything they depend on lives there: the shelf switches, the
    offer caps, the qualification gate. Two engines reading two copies of a cap is
    two caps.

    🚨 It does NOT consult `enabled`. The AI Chatter being switched off must not
    switch off the seller — 7 of 21 live accounts run Auto Convo with no closer at
    all, and those are the accounts a content ask has never had any path to a sale
    on. `_load_config` merges the blob without reading that flag; nothing here
    reintroduces it.
    """
    if cfg is None:
        from .ai_chatter import _load_config
        cfg = await _load_config(account_id)
    return SellLane(account_id, engine, cfg, permission_key=permission_key,
                    budget=budget)
