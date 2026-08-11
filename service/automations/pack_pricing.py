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


async def spend_bounds(account_id: str, fan_id: int) -> tuple[int, str | None]:
    """`(cap_cents, max_tier)` for THIS fan, from what he has actually paid.

    `max_tier` is None for a proven buyer — no ceiling, he has earned the vault.
    Everyone else is bounded, and a fan who has never paid a cent is bounded
    twice: at $100 and at `suggestive`.
    """
    from .ai_chatter import _paid_ppv_facts        # local: avoid an import cycle

    max_paid, _last = await _paid_ppv_facts(str(account_id), int(fan_id))
    max_paid = int(max_paid or 0)
    if max_paid >= PROVEN_SINGLE_PPV_CENTS:
        return PACK_CAP_PROVEN_CENTS, None
    if max_paid > 0:
        return PACK_CAP_CENTS, WARM_MAX_TIER
    return PACK_CAP_CENTS, COLD_MAX_TIER


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


async def quote_pack(account_id: str, fan_id: int, item: CatalogItem,
                     avail: int) -> int | None:
    """The price for this fan — or None meaning DO NOT OFFER.

    The price is quoted first, the count derives from it, and then the SHELF MAY
    VETO the price down to what it can honestly fill. A pack the shelf cannot
    fill is not discounted into existence; it is refused.

    ⚠️ Returns the PRICE only. It used to return `(price, n)` and the sole caller
    discarded the count — composition derives its own from the rate card, so the
    flat-rate `n` was a second, disagreeing answer to "how many pieces" that
    nothing consumed. It survives here as the veto arithmetic, which is its
    only real job.
    """
    from .ai_chatter import _paid_ppv_facts        # local: avoid an import cycle

    max_paid, _last_paid = await _paid_ppv_facts(str(account_id), int(fan_id))
    fan = upsell.FanState(
        fan_id=int(fan_id), max_single_paid_cents=int(max_paid or 0),
        last_paid_cents=None,                     # a pack is a cold open, not a rung
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
    # The per-fan ceiling, applied AFTER the ladder has spoken. `next_price`
    # already refuses to ask a cold fan more than $59 and can never exceed the
    # $200 wire max; this is the operator's middle rule, which nothing else
    # enforces — $100 unless a real PPV proves he goes higher.
    cap, _tier = await spend_bounds(account_id, fan_id)
    px = min(int(quote.price_cents), cap)
    n = clamp_count(px)
    if avail < n:                    # the shelf vetoes the price
        px = max(upsell.OF_PRICE_FLOOR_CENTS, avail * CENTS_PER_ITEM)
        n = clamp_count(px)
    if min(n, avail) < MIN_ITEMS:
        return None
    return px
