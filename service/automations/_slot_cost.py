"""How a media item is BILLED against a pull budget — one home.

A pull is handed a single `cost`, and that one value carries the whole policy:
what may ride, and what it is worth in photo-slots. Eligibility and price can
therefore never disagree, and there is no combination of them left to get wrong.

Split out of tip_reward 2026-08-20: both the reward pipeline (tip_reward) and
the teaser ladder (teaser_select) bill pulls, and tip_reward was reaching for
these through teaser_select — a costing primitive imported via the ladder.

Every name below is PUBLIC. The leading underscore belongs to the MODULE (the
house mark for a leaf, like `_clock` / `_wordfilter`), never to the names three
other modules import by hand — an underscore that everyone crosses says nothing
about what is internal here. `_VideoSlotCost` / `_PhotosOnly` are the genuinely
internal ones: callers want the singletons, not the classes.
"""
from __future__ import annotations

from automations.pack_claim import MOVING_KINDS
from db.engine import get_session
from db.models import VaultItem
from sqlalchemy import select

class SlotCost:
    """HOW A LANE BILLS ONE MEDIA ITEM against its pull budget: the item's price in
    photo-slots, or None for "may never ride". ONE value carries the whole policy,
    so a pull takes a single `cost` — eligibility and price cannot disagree, and
    there is no combination of them left to get wrong.

    The three shapes, all `SlotCost`:
      • `PER_ITEM`     — everything is one slot, clips included. The unbudgeted
                          lanes: the hot teaser, and the $0 image-reply freebie
                          where nothing was paid for a clip's length to overflow.
      • `PHOTOS_ONLY`  — clips are ineligible; a "Videos too" checkbox left off.
      • `video_slot_cost(...)` — clips ride, each billed off the rate card.
    `PER_ITEM` is the base class itself: the plain count every pull used before
    any of this existed, and still the default."""

    def __call__(self, mid: int, mtype: str, duration: int) -> int | None:
        return 1

    def slots_of(self, ids: list[int]) -> int:
        """Slots a picked list consumed — how `pull_stages` measures a shortfall
        without re-reading anything."""
        return len(ids)


class _VideoSlotCost(SlotCost):
    """Clips priced by the rate card ("Videos too" on). A clip costs
    ceil(rate-card value / dollars_per_image): 60s of sfw at the $5/item default is
    3000¢/500¢ = 6 slots, explicit footage twice that. Built by `video_slot_cost`
    (which does the ONE mirror read); pure and memoizing afterwards, so an instance
    can cross into `asyncio.to_thread`.

    The tier and (preferably) the duration come from the vault mirror; a clip the
    mirror has never seen — or one the describe pass hasn't tiered — bills at the
    SFW base off the OF payload's own duration, so missing data can only ever
    UNDER-charge, never inflate a clip out of the bundle."""

    def __init__(self, mirror: dict[int, tuple[int | None, str | None]],
                 cents_per_slot: int) -> None:
        self._mirror = mirror
        self._per = cents_per_slot
        self._memo: dict[int, int] = {}

    def __call__(self, mid: int, mtype: str, duration: int) -> int:
        mid = int(mid)
        got = self._memo.get(mid)
        if got is None:
            if mtype in MOVING_KINDS:
                from automations import pack_pricing  # local: its import chain
                # drags the whole vault stack (content_resolver →
                # vault_pack_picker/vault_scripts) — pay for it on the first
                # videos-ON reward, not at module import.
                dur, tier = self._mirror.get(mid, (None, None))
                value = pack_pricing.item_value_cents(
                    mtype, int(dur or duration or 0), str(tier or "sfw"))
                got = max(1, -(-value // self._per))     # ceil division
            else:
                got = 1                                  # a photo is one slot
            self._memo[mid] = got
        return got

    def slots_of(self, ids: list[int]) -> int:
        # Every picked id was priced on the way in (`gather_unseen` costs an item
        # before taking it), so the memo has it — no second read, and a KeyError
        # here would mean someone counted ids this cost never priced.
        return sum(self._memo[int(m)] for m in ids)


class _PhotosOnly(SlotCost):
    """Images only (a "Videos too" checkbox left off): video and gif are refused at
    the folder scan. A blank type still passes as a photo, as it always has (older
    payloads / test fakes)."""

    def __call__(self, mid: int, mtype: str, duration: int) -> int | None:
        return None if mtype in MOVING_KINDS else 1


PER_ITEM = SlotCost()


PHOTOS_ONLY = _PhotosOnly()


def cents_per_slot(cfg: dict) -> int:
    """ONE derivation of the "$ per image" knob, in cents. It is the number that
    ties the tip's slot budget (`_media_count`), the bundle sizing
    (`_bundle_sizing`) and the video slot cost (`SlotCost`) together — three
    inline copies had already grown two different (dead — `_load_config` always
    merges the default) fallbacks, and the day those disagree for real, the
    budget and the cost stop meaning the same thing."""
    return max(1, int(cfg.get("dollars_per_image") or 5)) * 100


async def video_slot_cost(account_id: str, cents_per_slot: int) -> _VideoSlotCost:
    """Build the rate-card cost: ONE account-wide select over the vault mirror's
    moving items. Account-wide because the folder ids aren't known until the sync
    scan runs (and vaults are hundreds of rows, not thousands).

    Takes the RATE, not the config: the teaser ladder reaches this holding a
    `BundleSizing` rather than a cfg dict, and both callers must bill at the one
    `cents_per_slot` number rather than re-deriving it."""
    async with get_session() as s:
        rows = (await s.execute(
            select(VaultItem.media_id, VaultItem.duration_seconds,
                   VaultItem.explicitness_tier).where(
                VaultItem.account_id == str(account_id),
                VaultItem.kind.in_(MOVING_KINDS))
        )).all()
    return _VideoSlotCost({int(m): (d, t) for m, d, t in rows}, int(cents_per_slot))
