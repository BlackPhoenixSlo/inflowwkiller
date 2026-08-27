"""service/automations/tip_ladder.py — pure bundle math for tips/PPVs.

PURE (no DB, no I/O) so it unit-tests without a harness. The callers
(tip_reward's hot_teaser, tip_request) own their own config + per-fan state
and pass the knobs in.

bundle_plan — how many vault photos ride a tip/PPV at a given PRICE, so value
scales with the ask: `BundleSizing` in, a per-tier `BundlePlan` out. Paired
with bundle_weight, which scores a bundle by folder tier (free 0 / standard 1
/ premium 3) — the "premium photos are worth more" knob the operator asked for.

(The adaptive tip-ASK primitive that used to live here went with ai_chatter's
tip lane — 2026-08-19 ppv-only ruling.)
"""
from __future__ import annotations

from typing import NamedTuple

# Folder-tier weights: what one photo from each tier is "worth" when scoring a
# bundle's value. Free filler counts for nothing; a premium ($50-folder) photo
# is worth 3 standard ones. Overridable per account via the *_weight_* knobs.
WEIGHT_FREE = 0
WEIGHT_STANDARD = 1
WEIGHT_PREMIUM = 3

class BundlePlan(NamedTuple):
    """A price-scaled photo bundle, composed by folder tier.
      • premium/normal/free — how many photos from each tier.
      • total — photos the fan sees (premium + normal + free).
      • weight — value score (premium×3 + normal×1 + free×0).
    """
    premium: int
    normal: int
    free: int
    total: int
    weight: int


class BundleSizing(NamedTuple):
    """How many photos a send carries, in the operator's OWN numbers.

    One value, passed whole, so "how big is this send" has a single home and no
    caller can half-supply it. The names are the Tip Reward tab's fields, because
    that is where every one of them comes from ($ per image → cents_per_photo,
    Minimum → min_photos, Maximum → max_photos); `free_photos` is the $0 rung's
    taste. Building one from a config is the config module's job — this module
    stays pure policy and never learns a config key.
    """
    cents_per_photo: int
    min_photos: int
    max_photos: int
    free_photos: int


def bundle_plan(price_cents: int, sizing: BundleSizing, *,
                w_premium: int = WEIGHT_PREMIUM) -> BundlePlan:
    """Compose a price-scaled photo bundle by WEIGHT BUDGET (the operator's model).

    Budget = round(price / `cents_per_photo`), floored at `min_photos` and capped
    at `max_photos`. Fill PREMIUM-first (each worth `w_premium`), the remainder as
    NORMAL (worth 1), then PAD with FREE photos so the bundle still SHOWS `budget`
    photos total — a full-looking set whose value is concentrated in the premium
    shots. A $0 ask sends `free_photos` free photos, and `free_photos=0` means no
    free tease at all — on EVERY caller, the one rule.

    Worked example (the operator's, at $10/photo): $80 → budget 8 → 8//3 = 2
    premium (w6) + 2 normal (w2) + 4 free = 8 photos, weight 8. A softened ~$40 →
    budget 4 → 1 premium + 1 normal + 2 free = 4 photos.

    The `min_photos` floor is what stops a SOFTENED ask landing under one weight
    unit and rounding to a lonely single — live on ACCOUNT_ID_4, a $13.34 tease sent
    exactly one image at $10/photo, as did its $6.67 predecessor. `max_photos`
    stays the hard ceiling, clamping the floor if the two ever cross."""
    price = int(price_cents or 0)
    if price <= 0:
        fc = max(0, int(sizing.free_photos))
        return BundlePlan(premium=0, normal=0, free=fc, total=fc, weight=0)
    wp = max(1, int(w_premium))
    budget = max(1, round(price / max(1, int(sizing.cents_per_photo))))
    budget = max(budget, max(1, int(sizing.min_photos)))
    budget = min(budget, max(1, int(sizing.max_photos)))
    premium = budget // wp
    normal = budget - premium * wp          # remainder, each worth 1
    paid = premium + normal
    free = max(0, budget - paid)             # pad the visible bundle back up
    total = paid + free
    weight = premium * wp + normal
    return BundlePlan(premium=premium, normal=normal, free=free,
                      total=total, weight=weight)


def bundle_weight(
    n_free: int = 0,
    n_standard: int = 0,
    n_premium: int = 0,
    *,
    w_free: int = WEIGHT_FREE,
    w_standard: int = WEIGHT_STANDARD,
    w_premium: int = WEIGHT_PREMIUM,
) -> int:
    """Value-weight of a bundle: free photos count for nothing, standard for 1,
    premium for 3 (all overridable). The operator's "premium photos are worth
    more" score — used to log/justify a bundle and to prefer premium folders on
    a high-priced ask."""
    return (max(0, int(n_free)) * int(w_free)
            + max(0, int(n_standard)) * int(w_standard)
            + max(0, int(n_premium)) * int(w_premium))
