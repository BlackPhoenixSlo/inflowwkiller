"""service/automations/pack_pricing.py — what it is worth, what he may be
charged, and what fills that price.

Split out of `pack_sender` on 2026-08-11. This module answers three questions
and owns no send path: *what is one piece worth*, *what is the ceiling for THIS
fan*, and *which pieces cover the quote*. `pack_sender` decides where the ids
come from and puts them on the wire.

## Count follows price, at the house rate

    px    = next_price(...)                 # floats $10-200
    n     = clamp(3, px // 500, 12)         # $5 an item, min 3, max 12
    avail = rung − what he has BOUGHT       # per fan, un-BOUGHT not un-sent
    if avail < n:  px = avail * 500         # the shelf VETOES the price
    if avail < 3:  REFUSE

$5/item is not a new convention — `tip_reward.dollars_per_image` is 5 on all ten
prod accounts, `min_images` 2, `max_images` 12. A $59 pack of 3 would be $19.67
an item, 3.9x the house rate: the same shape as the sale that preceded a deleted
account, with the lie removed.
"""
from __future__ import annotations

import logging
import random
from dataclasses import dataclass

from sqlalchemy import select

from db.engine import get_session
from db.models import CatalogItem, VaultItem

from . import content_resolver, upsell
from .pack_claim import MOVING_KINDS

log = logging.getLogger("of-relay.automation.pack_pricing")

# ── House constants ─────────────────────────────────────────────────
CENTS_PER_ITEM = 500          # tip_reward.dollars_per_image = 5, on all 10 accounts
MIN_ITEMS = 3                 # tip_reward.min_images scaled to a priced ask
# Operator ruling 2026-08-11: "it's nice to always have at least 7 but not
# needed (fill with some crap)." A SOFT target — the pack pads toward it with
# low-value items from the SAME rung once the price is already covered, and
# simply stops early when the shelf cannot reach it.
SOFT_TARGET_ITEMS = 7
# Operator ruling 2026-08-11: "we can pick from 3-35 pieces depending on ask and
# price." Raised from tip_reward's max_images of 12, which exists so "a whale tip
# can't drain a folder in one shot" — a FREE-reward concern that does not apply
# to a priced pack.
#
# ⚠️ Price still governs, so 35 is rare and earned, not common: at $5 an item a
# 35-piece pack is a $175 ask, and `MAX_ASK_VS_HISTORY_MULT = 3.0` means that is
# only quotable to a fan whose largest single paid PPV was ~$58. A cold fan
# capped at COLD_OPEN_CEILING_CENTS ($59) still resolves to 11.
MAX_ITEMS = 35

# ── Per-item VALUE (operator ruling 2026-08-11) ─────────────────────
#
# "Videos 5-10$ per 10 sec … images, the explicit for more like 10$ … teases are
# less valuable and can be added as filler."
#
# ⚠️ This REPLACES the flat $5-an-item rate for composition, and it is a
# deliberate revision of SPEC ticket 06. Read that ruling before touching this:
# the flat rate existed because a $59 pack of 3 is $19.67 an item, and that shape
# preceded two account deletions. The protection is preserved a different way —
# value now decides the COUNT, so a high price must be MET with real content
# rather than justified by a small number of expensive-looking items.
VIDEO_CENTS_PER_10S = {"hardcore": 1000, "explicit": 1000,
                       "suggestive": 700, "sfw": 500}
IMAGE_CENTS = {"hardcore": 1000, "explicit": 1000, "suggestive": 300, "sfw": 100}
_DEFAULT_TIER = "suggestive"
FILLER_MAX_CENTS = 300        # at or below this, an item counts as filler

# ── What he may be asked, and how explicit it may be ────────────────
#
# Operator, 2026-08-11: "max is 100 or 200 depending on ppv and what he sold and
# bought etc. if he hasnt bought much we send less explicit at start."
#
# Two of the three halves were already enforced upstream by `upsell`, and are
# NOT re-implemented here: COLD_OPEN_CEILING_CENTS ($59) is the "first send is
# ~$60" rule, and OF_PRICE_MAX_CENTS ($200) is a hard wire invariant. What was
# missing is the MIDDLE — a fan who once paid $40 is quotable at 3x that under
# MAX_ASK_VS_HISTORY_MULT, i.e. $120, with nothing between $59 and the $200 wire
# max to say otherwise.
PACK_CAP_CENTS = 10_000            # the normal ceiling for a content-ask pack
PACK_CAP_PROVEN_CENTS = 20_000     # lifted only by a genuinely big single PPV
PROVEN_SINGLE_PPV_CENTS = 5_000    # one PPV at $50+ is what "proven" means

# The explicitness ladder is climbed by SPENDING, not by asking. A man who has
# never paid gets the tease, and the payoff is what he is buying — leading with
# hardcore to a cold fan spends the only thing there is left to sell him.
TIER_RANK: dict[str, int] = {"sfw": 0, "suggestive": 1, "explicit": 2, "hardcore": 3}
COLD_MAX_TIER = "suggestive"       # never paid → nothing above this
WARM_MAX_TIER = "explicit"         # has paid, but not big → explicit, not hardcore

# 🚨 The STICKER, per rung. Not what any fan is charged — `next_price` quotes
# that per fan — but the anchor `derive_band` needs to place the item in a price
# band at all. SPEC §6.1 warned that `band_lo`/`band_hi` are NULL on all 165
# catalog items, so a row created with price_cents=0 collapses the band to the
# floor: the first dry-run against the live shelf quoted $8.21 for $42 of
# content. These are SPEC §6's own figures — the $59 median for the product
# rung, and 3x that for rung 3, which is where MAX_ASK_VS_HISTORY_MULT lands a
# fan who bought rung 2.
RUNG_STICKER_CENTS: dict[str, int] = {
    "tease": 0,            # never sold — previews only
    "nude": 5900,
    "nude-body": 17700,
}
DEFAULT_STICKER_CENTS = 5900


def clamp_count(px_cents: int) -> int:
    """The legacy flat-rate count. Still the FLOOR/CEILING guard on composition."""
    return max(MIN_ITEMS, min(int(px_cents) // CENTS_PER_ITEM, MAX_ITEMS))


def item_value_cents(kind: str | None, duration_seconds: int | None,
                     explicitness_tier: str | None) -> int:
    """What one piece is worth, per the operator's 2026-08-11 rate card.

    A video is priced by LENGTH — $5–10 per 10 seconds depending on how explicit
    it is — because 10 seconds of hardcore and 10 seconds of a sfw pan are not
    the same product. A still is priced by explicitness alone: explicit ~$10,
    a tease is filler.

    Rounded to whole 10-second blocks so a 43-second clip is priced as 4 blocks,
    not 4.3 — the fan is buying content, not a stopwatch reading.
    """
    tier = str(explicitness_tier or _DEFAULT_TIER).strip().lower()
    if str(kind or "").lower() in MOVING_KINDS:
        blocks = max(1, round(int(duration_seconds or 0) / 10))
        return blocks * VIDEO_CENTS_PER_10S.get(tier, VIDEO_CENTS_PER_10S[_DEFAULT_TIER])
    return IMAGE_CENTS.get(tier, IMAGE_CENTS[_DEFAULT_TIER])


async def _values_for(account_id: str, media: list[int]) -> dict[int, int]:
    if not media:
        return {}
    async with get_session() as s:
        rows = (await s.execute(
            select(VaultItem.media_id, VaultItem.kind, VaultItem.duration_seconds,
                   VaultItem.explicitness_tier).where(
                VaultItem.account_id == str(account_id),
                VaultItem.media_id.in_(media))
        )).all()
    return {int(m): item_value_cents(k, d, t) for m, k, d, t in rows}


async def compose_by_value(account_id: str, avail_ids: list[int],
                           target_cents: int) -> tuple[list[int], int]:
    """Fill `target_cents` with real content, then pad toward the soft target.

    Returns `(media, value_cents)`. `value_cents` may be LESS than the target —
    that is the shelf vetoing the price, and the caller must drop the ask to it
    rather than charge for content that is not there.

    Two passes, and the order is the point:
      1. **Payoff first**, in operator rank order, until the price is covered.
         The fan's money buys the good stuff, not the padding.
      2. **Filler after**, only once the price is already met, and only from the
         SAME rung — so the caption's claim stays true of every attached item.
         Filler never raises the price; it raises the count toward 7.
    """
    values = await _values_for(account_id, avail_ids)
    kinds_by_id = await content_resolver.kind_of(account_id, avail_ids)
    payoff = [m for m in avail_ids if values.get(m, 0) > FILLER_MAX_CENTS]
    filler = [m for m in avail_ids if values.get(m, 0) <= FILLER_MAX_CENTS]

    chosen: list[int] = []
    total = 0
    for mid in payoff:                       # rank order — best first
        if total >= target_cents or len(chosen) >= MAX_ITEMS:
            break
        chosen.append(mid)
        total += values.get(mid, 0)
    # The price is not yet met and there is no more payoff: take filler too, so
    # the shelf's honest ceiling is computed from everything it actually has.
    for mid in filler:
        if total >= target_cents or len(chosen) >= MAX_ITEMS:
            break
        chosen.append(mid)
        total += values.get(mid, 0)

    # ── One moving thing, if the shelf has one ─────────────────────
    #
    # Operator, 2026-08-11, on a 7-item $52 pack of stills: "4 picks for 44 is a
    # bit much, maybe one short vid if we have." A stack of photos at that price
    # reads thin however the rate card scores it — a clip is what makes it feel
    # like a set rather than a contact sheet.
    #
    # Cheapest qualifying clip, and only when the pack is otherwise ALL stills:
    # this is about the shape of the pack, not about spending more. The pilot
    # vault has 252 clips of 10s or under, so the swap is nearly always available.
    if chosen and not any(kinds_by_id.get(m) in MOVING_KINDS for m in chosen):
        clip = next((m for m in avail_ids
                     if m not in chosen and kinds_by_id.get(m) in MOVING_KINDS), None)
        if clip is not None:
            # Swap out the WEAKEST still rather than appending: appending would
            # quietly raise what he gets for the same price every single time,
            # which is the inventory burn the filler-only rule just closed.
            weakest = min(chosen, key=lambda m: values.get(m, 0))
            if values.get(clip, 0) >= values.get(weakest, 0):
                chosen[chosen.index(weakest)] = clip
                total += values.get(clip, 0) - values.get(weakest, 0)

    # Pad toward the soft target at NO extra charge — from FILLER ONLY.
    #
    # 🚨 This used to pad from `avail_ids`, which meant the free padding could be
    # $40 clips: a $60 ask walked out with $270 of content attached and the next
    # ask had nothing left to sell. The operator's rule is "nice to always have
    # at least 7 but not needed (fill with some crap)" — crap is the whole point
    # of the pass. If there is no filler left, a 4-item pack is the right answer.
    if len(chosen) < SOFT_TARGET_ITEMS:
        for mid in filler:
            if len(chosen) >= SOFT_TARGET_ITEMS or len(chosen) >= MAX_ITEMS:
                break
            if mid not in chosen:
                chosen.append(mid)
    return chosen, total


# ── The price ladder (operator ruling 2026-08-12) ───────────────────
#
# Fixed price points, and the fan has to BUY a rung to earn the next one. It
# applies to THIS lane only — the fan asked, or the pack came off the vault
# shelf. The convo teaser, ppv_send, mass and the unprompted upseller keep their
# own pricing; the operator scoped it that way deliberately.
#
# Why a ladder at all: nothing was laddering this lane. The quote called
# `next_price` with `rung_index=0` and `last_paid_cents=None` — every pack was a
# cold open — so prod prices came out as $4.71, $40.91, $50.21 with no relation
# to what the fan had ever paid. That path survives as `_negotiate_legacy`, the
# rollback, and it still opens cold on every send.
PRICE_LADDER_CENTS: tuple[int, ...] = (
    670, 1_000, 2_700, 3_600, 5_900, 7_200, 8_700, 9_800, 12_900, 14_400)

# Where a fan who has NEVER paid opens. Not rung 0.
#
# 🚨 Measured over 90 days of first-ever priced offers, revenue per ask by the
# price we opened at: $20-30 → $2.50, >$65 → $2.36, $12-20 → $0.96, $30-45 →
# $0.61, and <=$8 → $0.09. Opening every cold fan at $6.70 would put the whole
# book in the worst row, ~27x below the best. $6.70 earns its place as the
# bottom of the WALK-DOWN, which is where it actually converts: the case that
# prompted this was a never-paid fan who refused $20.14, $10.00 and $20.14
# again, then bought on the fourth ask at $6.58.
#
# The measurement is observational: the <=$8 and $30-45 rows carry the huge
# automatic streams, so their denominators hold fans who were never going to
# buy. Read it as "cheap does not rescue a cold ask", not as a causal curve.
COLD_OPEN_RUNG = 2                 # $27.00

# A purchase earns ONE rung, or TWO when the pack carries video — video is worth
# more per piece (see VIDEO_CENTS_PER_10S) so it may skip a step.
STEP_ON_BUY = 1
STEP_ON_BUY_WITH_VIDEO = 2

# This lane's own attribution. ⚠️ `pack_sender` MUST stamp its sends with this
# constant and not with a copy of the string: if the two drift, `misses` is
# silently always 0, the walk-down stops existing, and nothing raises.
PACK_ATTR_KIND = "pack_send"

# What `negotiate_pack` refuses with. Declared next to the arithmetic that
# raises them and re-exported by `pack_sender` — two tables of refusal strings
# is exactly the drift this module was split out to end.
REFUSE_NO_PRICE = "no_price"
REFUSE_TOO_THIN = "shelf_too_thin"

# How far a refusal streak may walk him DOWN the ladder.
#
# 🚨 Found on live prod data minutes after the first deploy: unbounded, this
# floored EVERY existing fan at $6.70 — including a proven $50.20 buyer whose
# next ask should have been $59. Two compounding causes, both fixed: the count
# ran over every priced message (90,460 broadcast PPVs in 30 days against 2
# sends from this lane, so ignoring a blast read as refusing an ask), and it had
# no ceiling, so any long history could only ever reach the floor.
#
# The cap is right independent of that bug. The walk-down exists to FIND the
# price he will pay, and $27 → $10 → $6.70 has already found it; past that it
# stops being discovery and just gives the vault away.
MAX_SOFTEN_RUNGS = 2


def ladder_on(cfg: dict | None) -> bool:
    """Is the ladder governing this account? Default ON.

    🚨 `pack_price_ladder` MUST stay named in `scripts_api._validate_cfg`. That
    validator drops every key it does not list, so an unnamed flag is a brake
    with no cable attached — the save returns 200, the key never lands, and the
    only rollback left is a redeploy. It shipped that way on 2026-08-12 and the
    commit message claimed a switch that did not exist.
    """
    return bool((cfg or {}).get("pack_price_ladder", True))


def ladder_floor_index(cents: int) -> int:
    """Index of the highest rung at or below `cents`; 0 when below every rung."""
    idx = 0
    for i, rung in enumerate(PRICE_LADDER_CENTS):
        if rung <= cents:
            idx = i
    return idx


def snap_to_ladder(cents: int) -> int:
    """The nearest rung at or below `cents`, never under the bottom rung.

    Every price the ladder itself emits lands on a rung. The shelf veto and the
    per-fan cap both compute raw numbers, and a $43.12 quote from either would
    make the ladder a suggestion rather than the rule. (`value_caps_price` is
    the one documented exception — see `negotiate_pack`.)
    """
    if cents <= PRICE_LADDER_CENTS[0]:
        return PRICE_LADDER_CENTS[0]
    return PRICE_LADDER_CENTS[ladder_floor_index(cents)]


def next_rung_above(cents: int) -> int | None:
    """The first rung strictly above `cents`, or None at the top."""
    for rung in PRICE_LADDER_CENTS:
        if rung > cents:
            return rung
    return None


def ladder_rung(*, max_paid_cents: int, misses: int, has_video: bool) -> int:
    """Which rung to quote: what he has PROVEN, plus a step, minus his refusals.

    `misses` is how many priced offers he has left unbought since his last
    purchase, so the walk-down is read off the thread rather than stored — one
    less piece of state to desync from the messages it describes.

    A fan who never paid has proven nothing, so there is no step to take: he
    opens at `COLD_OPEN_RUNG` and softens from there. Video lifts a CLIMB, not
    an opening — "buy that level, max go 2 up if there is video involved".
    """
    top = len(PRICE_LADDER_CENTS) - 1
    if max_paid_cents <= 0:
        target = COLD_OPEN_RUNG
    else:
        step = STEP_ON_BUY_WITH_VIDEO if has_video else STEP_ON_BUY
        target = ladder_floor_index(max_paid_cents) + step
    soften = min(max(0, misses), MAX_SOFTEN_RUNGS)
    return max(0, min(top, target - soften))


async def has_moving_media(account_id: str, media_ids: list[int]) -> bool:
    """Is any of this video (or a gif)? Reads the same `MOVING_KINDS` the caption
    counts by, so "2 vids" in the words and the +2 rung agree on what a vid is."""
    if not media_ids:
        return False
    async with get_session() as s:
        rows = (await s.execute(
            select(VaultItem.kind).where(
                VaultItem.account_id == str(account_id),
                VaultItem.media_id.in_([int(m) for m in media_ids]))
        )).all()
    return any(str(k or "").strip().lower() in MOVING_KINDS for (k,) in rows)


async def unbought_since_last_paid(account_id: str, fan_id: int) -> int:
    """Offers from THIS LANE he has let pass since he last bought anything.

    Counted from `messages` for the same reason `_paid_ppv_facts` is: a stored
    counter and the thread it summarises drift, and the thread is the one the
    fan actually lived through.

    Asymmetric on purpose. The PURCHASE that resets the streak is any priced
    unlock — money is money, and a fan who just bought a $50 broadcast is not in
    a refusal streak. The REFUSALS are this lane's only: he never asked for the
    mass PPV, so ignoring it says nothing about the price he would pay for the
    thing he DID ask for.
    """
    from db.models import Message

    async with get_session() as s:
        last_paid = (await s.execute(
            select(Message.created_at).where(
                Message.account_id == str(account_id),
                Message.fan_id == int(fan_id),
                Message.direction == "out",
                Message.price_cents > 0,
                Message.is_paid.is_(True),
            ).order_by(Message.created_at.desc()).limit(1)
        )).scalar_one_or_none()
        q = select(Message.message_id).where(
            Message.account_id == str(account_id),
            Message.fan_id == int(fan_id),
            Message.direction == "out",
            Message.price_cents > 0,
            Message.is_paid.is_not(True),
            Message.automation_kind == PACK_ATTR_KIND,
        )
        if last_paid is not None:
            q = q.where(Message.created_at > last_paid)
        return len((await s.execute(q)).all())


def bounds_for(max_paid_cents: int) -> tuple[int, str | None]:
    """`(cap_cents, max_tier)` from what he has paid. The rule, without the read.

    `max_tier` is None for a proven buyer — no ceiling, he has earned the vault.
    Everyone else is bounded, and a fan who has never paid a cent is bounded
    twice: at $100 and at `suggestive`.

    Pure so `ladder_context` can apply it to a `max_paid` it already holds.
    `spend_bounds` is the same rule for a caller that has nothing in hand yet.
    """
    if max_paid_cents >= PROVEN_SINGLE_PPV_CENTS:
        return PACK_CAP_PROVEN_CENTS, None
    if max_paid_cents > 0:
        return PACK_CAP_CENTS, WARM_MAX_TIER
    return PACK_CAP_CENTS, COLD_MAX_TIER


async def spend_bounds(account_id: str, fan_id: int) -> tuple[int, str | None]:
    """`(cap_cents, max_tier)` for THIS fan, from what he has actually paid."""
    from .ai_chatter import _paid_ppv_facts        # local: avoid an import cycle

    max_paid, _last = await _paid_ppv_facts(str(account_id), int(fan_id))
    return bounds_for(int(max_paid or 0))


async def rank_by_tier(account_id: str, media: list[int],
                       max_tier: str | None) -> list[int]:
    """Put what he has earned FIRST, keeping everything else behind it.

    🚨 A preference, deliberately not a filter. Dropping over-tier items outright
    is one line shorter and empties the shelf for a cold fan whenever the shelf
    happens to be explicit — the silent-refusal shape this map has warned about
    three times, and what the first cut of this did to 8 of 10 test cases.
    Composition consumes this list in order, so the tame end is spent first and
    the explicit end is only reached when there is nothing else to send.

    An item with no tier on file sorts as `_DEFAULT_TIER`: the describe pass has
    not reached every item, and a NULL is missing data, not evidence of hardcore.
    """
    if not max_tier or not media:
        return media
    ceiling = TIER_RANK.get(max_tier, TIER_RANK[_DEFAULT_TIER])
    async with get_session() as s:
        rows = (await s.execute(
            select(VaultItem.media_id, VaultItem.explicitness_tier).where(
                VaultItem.account_id == str(account_id),
                VaultItem.media_id.in_(media))
        )).all()
    by_id = {int(m): str(t or _DEFAULT_TIER).strip().lower() for m, t in rows}
    rank = {m: TIER_RANK.get(by_id.get(m, _DEFAULT_TIER),
                             TIER_RANK[_DEFAULT_TIER]) for m in media}
    # Stable: within "earned" and within "over-tier", operator rank order holds.
    return ([m for m in media if rank[m] <= ceiling]
            + [m for m in media if rank[m] > ceiling])


# ── Settling the price and the pack together ────────────────────────


@dataclass(frozen=True)
class LadderCtx:
    """Everything the ladder knows about ONE fan, read ONCE.

    🚨 Why this exists. The ladder used to be applied in three passes — quote,
    lift, revoke — each re-deriving these same facts from the database. That cost
    20 queries an ask with `_paid_ppv_facts` running five times on identical
    arguments, but the real damage was that no pass could see what the previous
    one had decided: the revoke re-quoted the PRE-LIFT price and took it without
    recomposing, measured shipping three explicit stills worth $30.00 for $10.00.
    Read the fan once and everything after it is arithmetic that cannot disagree
    with itself. One ask is 7 statements now, down from 20.
    """
    max_paid_cents: int
    misses: int
    cap_cents: int


async def ladder_context(account_id: str, fan_id: int) -> LadderCtx:
    """The only time the negotiation reads the fan. Once, up front."""
    from .ai_chatter import _paid_ppv_facts        # local: avoid an import cycle

    max_paid, _last_paid = await _paid_ppv_facts(str(account_id), int(fan_id))
    max_paid = int(max_paid or 0)
    cap, _tier = bounds_for(max_paid)
    return LadderCtx(max_paid_cents=max_paid, cap_cents=cap,
                     misses=await unbought_since_last_paid(account_id, fan_id))


def veto_to_shelf(px_cents: int, avail: int) -> int:
    """The shelf's honest ceiling: a price it cannot fill drops to what it can.

    A pack the shelf cannot cover is never discounted into existence at the
    original count — it is priced at what is actually there, or refused.
    """
    if avail >= clamp_count(px_cents):
        return px_cents
    return max(upsell.OF_PRICE_FLOOR_CENTS, avail * CENTS_PER_ITEM)


def ladder_price(ctx: LadderCtx, *, has_video: bool, avail: int) -> int | None:
    """The rung this fan is owed against a shelf of `avail` items, or None:
    DO NOT OFFER. PURE — which is the whole point of `LadderCtx`.

    The per-fan ceiling applies AFTER the ladder has spoken. `upsell` already
    refuses to ask a cold fan more than $59 and can never exceed the $200 wire
    max; the operator's MIDDLE rule — $100 unless a real PPV proves he goes
    higher — is enforced nowhere else.

    Everything this returns is a rung. The cap and the veto both compute raw
    arithmetic and a $43.12 out of either would make the ladder advisory;
    snapping DOWN can only reduce the ask, so it cannot breach the cap it has
    just passed.
    """
    if avail < MIN_ITEMS:        # no price makes a pack out of two photos
        return None
    rung = ladder_rung(max_paid_cents=ctx.max_paid_cents, misses=ctx.misses,
                       has_video=has_video)
    capped = min(PRICE_LADDER_CENTS[rung], ctx.cap_cents)
    return snap_to_ladder(veto_to_shelf(capped, avail))


async def _negotiate_legacy(account_id: str, fan_id: int, item: CatalogItem,
                            avail_ids: list[int],
                            ) -> tuple[int, list[int], int] | None:
    """`(price, media, value)` the pre-ladder way — `pack_price_ladder: false`.

    Kept as the no-deploy rollback for an account the rungs do not suit, and
    kept honest about what it is: it quotes every pack as a COLD OPEN
    (`rung_index=0`, `last_paid_cents=None`) whatever the fan has paid, which is
    how prod came to send $4.71, $40.91 and $50.21 with no relation to history.
    There is no lift and no revoke here — neither concept exists off the rungs.
    """
    avail = len(avail_ids)
    if avail < MIN_ITEMS:
        return None
    from .ai_chatter import _paid_ppv_facts        # local: avoid an import cycle

    max_paid, _last_paid = await _paid_ppv_facts(str(account_id), int(fan_id))
    max_paid = int(max_paid or 0)
    fan = upsell.FanState(
        fan_id=int(fan_id), max_single_paid_cents=max_paid,
        last_paid_cents=None,                 # a pack is a cold open, not a rung
        has_ever_paid=bool(max_paid))
    band, _src = upsell.derive_band(
        human_asks_cents=[], account_median_cents=None,
        item_price_cents=int(item.price_cents or 0))
    rng = random.Random(f"pack:{account_id}:{fan_id}:{item.id}")
    quote = upsell.next_price(
        fan=fan, band=band, last_paid_cents=None, rung_index=0,
        key=f"pack:{item.id}", account_id=str(account_id), rng=rng,
        library_bounds=(upsell.OF_PRICE_FLOOR_CENTS, upsell.OF_PRICE_MAX_CENTS),
        escalation_mult=None, max_ask_vs_history_mult=None)
    if quote is None:
        return None
    cap, _tier = bounds_for(max_paid)
    px = veto_to_shelf(min(int(quote.price_cents), cap), avail)
    media, value = await compose_by_value(account_id, avail_ids, px)
    return px, media, value


@dataclass(frozen=True)
class Pack:
    """A priced, composed pack — or the refusal that stopped it.

    `media` and `value_cents` survive a `REFUSE_TOO_THIN` on purpose: the count
    that fell short is the diagnostic, and a refusal that throws away its own
    evidence sends an operator back to the database to find out why.
    """
    media: list[int]
    price_cents: int
    value_cents: int
    refusal: str | None = None


async def negotiate_pack(account_id: str, fan_id: int, item: CatalogItem,
                         avail_ids: list[int], cfg: dict | None = None, *,
                         min_items: int = MIN_ITEMS) -> Pack:
    """Settle the PRICE and the CONTENT together — each one decides the other.

    Composition stops the moment the price is covered, so the count a price buys
    depends on the shelf, and whether the pack earned its video step depends on
    the count. Three rules, in a fixed order that IS the correctness argument:

      1. Quote the rung he is owed, assuming the shelf's video lands in the pack.
      2. REVOKE that assumption if no clip actually made it in.
      3. LIFT to the cheapest rung the shelf can honestly fill.

    🚨 Revoke BEFORE lift, always. The lift is the answer to "this pack composed
    under the floor", and it raises the price precisely because the lower one
    could not be filled. A revoke running afterwards re-quotes the PRE-LIFT price
    and takes it without recomposing — measured shipping three explicit stills
    worth $30.00 for $10.00, the exact giveaway the lift exists to prevent. With
    the revoke first, the lift has the last word and nothing can lower the price
    out from under a pack that was composed to cover it.

    Padding a thin pack out from the payoff pile is NOT the alternative: the
    operator's 08-11 ruling is that free padding never spends payoff, because a
    $60 pack that walks out with $270 attached has spent the next ask. So the
    price rises to what the shelf can fill instead. On a shelf of $10 stills the
    walk-down bottoms out at $36 rather than $6.70 — correct, because three $10
    stills for $6.70 is not a discount, it is a giveaway.

    `item` is read only by the legacy path, which needs its sticker price to
    derive a band. The ladder does not price off the catalog row at all.
    """
    if ladder_on(cfg):
        settled = await _negotiate_on_ladder(account_id, fan_id, avail_ids,
                                             min_items=min_items)
    else:
        settled = await _negotiate_legacy(account_id, fan_id, item, avail_ids)
    if settled is None:
        return Pack([], 0, 0, REFUSE_NO_PRICE)

    px, media, value = settled
    if len(media) < min_items:
        return Pack(media, px, value, REFUSE_TOO_THIN)
    # Operator ruling 2026-08-11: "we can price more for that content if the AI
    # upseller chooses." So the rate card is a BASELINE, not a ceiling — the
    # upseller knows this fan's history and willingness and may quote above it.
    #
    # ⚠️ What ticket 06 actually protected against was charging a lot for FEW
    # items ($19.67 an item, 3 items, an account deleted). That protection now
    # lives in the COUNT, not the price: `min_items` is a hard floor, composition
    # pads toward SOFT_TARGET_ITEMS, and the caption states the real number.
    #
    # `value_caps_price` restores the old hard veto for anyone who wants it.
    #
    # ⚠️ It OUTRANKS the ladder, and the resulting ask is the only one this lane
    # emits that is not a rung. That is deliberate. Snapping the capped price
    # back down to a rung was the first instinct and it is worse: an 8-item
    # shelf worth $24 against the $36 rung would be quoted $10, giving away $14
    # to preserve a shape. The flag's whole contract is "never charge more than
    # the attached content is worth", so an operator who turns it on has
    # subordinated the rungs to value on purpose. Off by default everywhere.
    if value < px and bool((cfg or {}).get("value_caps_price")):
        px = max(upsell.OF_PRICE_FLOOR_CENTS, value)
    if value < px:
        log.info("pack priced ABOVE rate card account=%s fan=%s px=%s value=%s "
                 "ratio=%.2f n=%s", account_id, fan_id, px, value,
                 px / max(1, value), len(media))
    return Pack(media, px, value)


async def _negotiate_on_ladder(account_id: str, fan_id: int,
                               avail_ids: list[int], *,
                               min_items: int) -> tuple[int, list[int], int] | None:
    """`(price, media, value)` off the rungs, or None meaning DO NOT OFFER.

    The three rules of `negotiate_pack`, over ONE `LadderCtx`. Every price here
    is a rung, so the ask a fan sees is never the residue of arithmetic.
    """
    avail = len(avail_ids)
    ctx = await ladder_context(account_id, fan_id)
    shelf_has_video = await has_moving_media(account_id, avail_ids)

    px = ladder_price(ctx, has_video=shelf_has_video, avail=avail)
    if px is None:
        return None
    log.info("pack ladder account=%s fan=%s px=%s max_paid=%s misses=%s "
             "shelf_video=%s avail=%s", account_id, fan_id, px,
             ctx.max_paid_cents, ctx.misses, shelf_has_video, avail)
    media, value = await compose_by_value(account_id, avail_ids, px)

    # 2. The +2 was granted on a shelf that HAD video; charge it only if a clip
    #    actually made it into the pack. `compose_by_value` declines to swap in a
    #    clip worth less than the still it would displace, so a cheap short clip
    #    on an explicit shelf lands here every time.
    if shelf_has_video and media and not await has_moving_media(account_id, media):
        stills_px = ladder_price(ctx, has_video=False, avail=avail)
        if stills_px is not None and stills_px < px:
            log.info("pack ladder video step REVOKED account=%s fan=%s %s->%s "
                     "(shelf had video, pack does not)",
                     account_id, fan_id, px, stills_px)
            px = stills_px
            media, value = await compose_by_value(account_id, avail_ids, px)

    # 3. The shelf may LIFT a rung — the mirror of the veto in `ladder_price`.
    while len(media) < min_items:
        lifted = next_rung_above(px)
        if lifted is None or lifted > ctx.cap_cents:
            break                        # the shelf cannot be filled honestly
        log.info("pack ladder LIFTED account=%s fan=%s %s->%s (a lower rung "
                 "composed under the %s-item floor)",
                 account_id, fan_id, px, lifted, min_items)
        px = lifted
        media, value = await compose_by_value(account_id, avail_ids, px)
    return px, media, value
