"""service/automations/pack_sender.py — sell him the thing he asked for.

A **pack** is one rung of a curated category (`feet-nude`), sold as a priced PPV
to a fan who asked for it. This module owns the product row, the audit that
stops it lying, the price/count, and the send.

## The one sentence

The caption states exactly what is attached — *"11 bare feet pics"* — and the
send is REFUSED rather than softened when the attached media does not match that
claim.

## Why the audit exists

On 2026-07-31 two fans made their first-ever purchase and deleted their entire
OnlyFans accounts within hours. One asked three times *"only feet right?"*, paid
$3.25, received a bra/face selfie with no feet, and wrote *"Goodbye, you stupid
liar."* Every rule here is that message, turned into a predicate.

## What resolves when

🚨 **`media_ids` is NOT a frozen snapshot of the rung** (operator ruling
2026-08-10, amending SPEC §3.2: *"the AI upseller finds content directly from the
VAULT — no seed images anywhere"*). The `CatalogItem` carries the product
IDENTITY only — rung, band, `description_for_ai` — and exists because
`ContentOffer.item_id` is a non-nullable FK, so attribution needs a row. The
media is resolved from the bound folder **at send time**, so adding a photo to
`feet-nude` puts it in the next send with nothing to re-cut.

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

⚠️ `avail` is un-BOUGHT, not un-sent (ticket 14). Keying on SENT would let one
declined $59 offer permanently strip 11 items from the rung — the fan's own
refusal exhausting his own shelf. And `ownership.owned_or_seen_media` is the
WRONG reader here: its signal 2 marks a delivered offer's whole `media_ids` as
owned, which under a whole-shelf item would silently delete the next sale.

## Flags

Everything is OFF by default and per-account. Nothing in this module can fire
without an operator turning `pack_send_enabled` on for that account.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select

import ownership
import vault_pack_picker
from db.engine import get_session
from db.models import (
    CatalogItem, VaultCacheRun, VaultFolder, VaultFolderItem, VaultItem, VaultSend,
)

from . import content_resolver, upsell

log = logging.getLogger("of-relay.automation.pack_sender")

# ── House constants ─────────────────────────────────────────────────
CENTS_PER_ITEM = 500          # tip_reward.dollars_per_image = 5, on all 10 accounts
MIN_ITEMS = 3                 # tip_reward.min_images scaled to a priced ask
# Operator ruling 2026-08-11: "it's nice to always have at least 7 but not
# needed (fill with some crap)." A SOFT target — the pack pads toward it with
# low-value items from the SAME rung once the price is already covered, and
# simply stops early when the shelf cannot reach it.
SOFT_TARGET_ITEMS = 7

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
PREVIEW_MIN, PREVIEW_MAX = 2, 3
_REPLY_MAX_CHARS = 600        # OF truncates the chat-list preview past this

# A pack may only be sold where an authored rung phrase exists. `script_packs`
# ships en/es/sl; prod is 16 en accounts and 1 es, and neither pilot is Spanish.
PACK_LANGUAGES = frozenset({"en", "es", "sl"})

# ⚠️ Mirror age is a WARNING, not a refusal — corrected 2026-08-11.
#
# The first cut refused any send when the vault mirror was stale, on the theory
# that rung membership had become OF-backed. That was wrong: `_shelf_media`
# reads `VaultFolderItem`, the INTERNAL membership the picker wrote. It is
# operator-authored, exact, and does not decay. The mirror is only our cache of
# OF's own listing, and media ids are stable — OF resolves them at send time
# whatever our cache says.
#
# What DOES matter is narrower and is checked directly: every id about to be
# charged for must still be a live `VaultItem` (not soft-deleted). A genuinely
# dead id fails loudly at the wire, which is the right place for it.
MIRROR_WARN_AGE = timedelta(days=7)

# ── Refusals ────────────────────────────────────────────────────────
REFUSE_DISABLED = "pack_disabled"
REFUSE_NO_SHELF = "no_shelf"
REFUSE_TOO_THIN = "shelf_too_thin"          # fewer than MIN_ITEMS un-bought
REFUSE_AUDIT = "audit_failed"
REFUSE_STALE_MIRROR = "stale_mirror"
REFUSE_LANGUAGE = "unsupported_language"
REFUSE_NO_PRICE = "no_price"
REFUSE_RESOLVER = "resolver_refused"

# 🚨 Fan-facing rung phrases. Internal folder names are TRIAGE vocabulary and are
# wrong for a fan: `nude` reads as HER BODY, which is rung 3. The negative half
# is load-bearing — "bare feet" alone leaves him free to expect rung 3 at $59.
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

RUNG_PHRASES: dict[tuple[str, str], str] = {
    ("feet", "tease"): "feet, covered",
    ("feet", "nude"): "bare feet, no nudity",
    ("feet", "nude-body"): "bare feet, and me nude with them",
}

# The corpus word, per (category, rung-agnostic). Real asks are short and
# literal — median 35 characters — so the noun is his, not ours.
ASK_NOUN: dict[str, str] = {"feet": "feet pics"}

# A voice line may not carry a number, a price, or a content claim: the clause
# above it is the contract, and a second claim underneath can contradict it.
_VOICE_BAN = re.compile(r"[0-9$€£]|\bpics?\b|\bphotos?\b|\bvideos?\b|\bset\b", re.I)


@dataclass(frozen=True)
class PackPlan:
    """What a send WOULD be. Returned by `plan_pack` so the operator (and the
    dry-run) can see the whole decision before anything is sent."""
    account_id: str
    fan_id: int
    category: str
    rung: str
    item_id: int | None
    media: list[int]            # the [:n] slice, in operator rank order
    previews: list[int]
    price_cents: int
    clause: str
    refusal: str | None = None
    detail: str = ""
    value_cents: int = 0        # rate-card worth of the attached payoff

    @property
    def ok(self) -> bool:
        return self.refusal is None and bool(self.media)


def _clamp_count(px_cents: int) -> int:
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
    if str(kind or "").lower() in ("video", "gif"):
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


def render_clause(category: str, rung: str, n: int) -> str:
    """The claim clause — rendered, never typed, never omitted.

    Grammar is `"{n} {rung-qualified ask noun}"`, the rung folded INTO the noun
    rather than trailing in a dash-clause, because OF truncates a long caption in
    the chat-list preview: a voice-first caption shows him the flirt and hides
    the subject at exactly the moment he decides whether to open it.
    """
    phrase = RUNG_PHRASES.get((category, rung))
    noun = ASK_NOUN.get(category, f"{category} pics")
    if rung == "nude":
        return f"{n} bare {noun}"
    if phrase:
        return f"{n} {noun} — {phrase}"
    return f"{n} {noun}"


# ── The shelf ───────────────────────────────────────────────────────

async def _shelf_media(account_id: str, category: str, rung: str) -> list[int]:
    """The rung's media in OPERATOR RANK order (manual_order, NULLs last).

    Read live: the folder is the product, so an item added today is sold today.
    """
    cat = vault_pack_picker.CATEGORIES.get(category)
    if cat is None:
        return []
    async with get_session() as s:
        folder = (await s.execute(
            select(VaultFolder).where(
                VaultFolder.account_id == str(account_id),
                VaultFolder.name == cat.folder_name(rung),
                VaultFolder.deleted_at.is_(None))
        )).scalar_one_or_none()
        if folder is None:
            return []
        return [int(m) for m in (await s.execute(
            select(VaultFolderItem.media_id).where(
                VaultFolderItem.account_id == str(account_id),
                VaultFolderItem.folder_id == folder.id,
            ).order_by(
                VaultFolderItem.manual_order.is_(None),
                VaultFolderItem.manual_order,
                VaultFolderItem.media_id,
            )
        )).scalars().all()]


async def _filter_kind(account_id: str, media: list[int], kind: str | None) -> list[int]:
    """Keep only media of the promised KIND. "send me a video" is a promise too,
    and a photo does not satisfy it."""
    if kind not in ("photo", "video") or not media:
        return media
    async with get_session() as s:
        rows = (await s.execute(
            select(VaultItem.media_id, VaultItem.kind).where(
                VaultItem.account_id == str(account_id),
                VaultItem.media_id.in_(media))
        )).all()
    by_id = {int(m): str(k or "") for m, k in rows}
    return [m for m in media if by_id.get(m, kind) == kind]


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


async def _rank_by_tier(account_id: str, media: list[int],
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


async def _bought_media(account_id: str, fan_id: int) -> set[int]:
    """What this fan has actually BOUGHT — signal 1 of ownership only.

    🚨 Deliberately NOT `ownership.owned_or_seen_media`. Its signal 2 marks a
    delivered non-free offer's WHOLE `media_ids` as owned; under a rung-wide item
    one delivered send would mark the entire shelf owned and silently delete the
    next sale. Do not "fix" ownership.py for this — the divergence is inert here
    (a pack is out of band) and conservative everywhere else.
    """
    async with get_session() as s:
        rows = (await s.execute(
            select(VaultSend.media_id).where(
                VaultSend.account_id == str(account_id),
                VaultSend.fan_id == int(fan_id),
                VaultSend.was_purchased.is_(True))
        )).scalars().all()
    return {int(m) for m in rows}


async def _mirror_age(account_id: str) -> timedelta | None:
    """How stale the vault mirror is. None when it has never been collected."""
    async with get_session() as s:
        last = (await s.execute(
            select(func.max(VaultCacheRun.finished_at)).where(
                VaultCacheRun.account_id == str(account_id),
                VaultCacheRun.status == "done")
        )).scalar_one_or_none()
    return None if last is None else (datetime.utcnow() - last)


# ── The audit ───────────────────────────────────────────────────────

async def audit_pack(account_id: str, category: str, rung: str,
                     media: list[int], previews: list[int]) -> list[str]:
    """Four set rules plus freshness. Returns the violations; empty is a pass.

    Runs at pack save AND again immediately before send — folder membership is
    mutable, and this map's own re-triage moved 19 items after the fact. A pack
    that passed on Monday can be a lie on Friday.

    ⚠️ Honest ceiling: this proves *"the attached media is what a human filed
    under this rung"*, never *"the attached media depicts this rung"*. That is
    still the whole distance between here and a caption that cannot lie about its
    own composition.
    """
    bad: list[str] = []
    M, P = list(media), list(previews)
    setM, setP = set(M), set(P)

    if not setP <= setM:
        bad.append(f"rule1 P⊄M: {sorted(setP - setM)}")

    R = set(await _shelf_media(account_id, category, rung))
    paid = setM - setP
    if not paid <= R:
        bad.append(f"rule2 paid items outside {category}-{rung}: {sorted(paid - R)}")

    T = set(await _shelf_media(account_id, category, "tease"))
    if T and not setP <= T:
        bad.append(f"rule3 previews outside the tease rung: {sorted(setP - T)}")

    if len(paid) < MIN_ITEMS:
        bad.append(f"rule4 |M−P| = {len(paid)} < {MIN_ITEMS}")

    # Rule 5 — every paid id must still EXIST. Membership is exact, but an item
    # soft-deleted after two clean collect sweeps is gone from her vault, and
    # charging for it would deliver nothing.
    if paid:
        async with get_session() as s:
            live = {int(m) for m in (await s.execute(
                select(VaultItem.media_id).where(
                    VaultItem.account_id == str(account_id),
                    VaultItem.media_id.in_(sorted(paid)),
                    VaultItem.removed_at.is_(None))
            )).scalars().all()}
        missing = sorted(paid - live)
        if missing:
            bad.append(f"rule5 media no longer in the vault: {missing}")
    return bad


async def mirror_warning(account_id: str) -> str:
    """A note for the log/operator when the cache is old — never a refusal."""
    age = await _mirror_age(account_id)
    if age is None:
        return "vault never collected"
    if age > MIRROR_WARN_AGE:
        return f"vault mirror {age.days}d old — Collect to refresh thumbnails"
    return ""


# ── The product row ─────────────────────────────────────────────────

async def ensure_pack_item(account_id: str, category: str, rung: str) -> CatalogItem:
    """The standalone `CatalogItem` for one rung — created once, then reused.

    `ContentOffer.item_id` is a NON-NULLABLE FK to `catalog_items`, so without
    this row a pack cannot be offered, cannot be attributed, and cannot answer
    "did the feet pack sell". That is the row's whole job.

    `enabled=False` on purpose: a pack must be EXCLUDED from the ordinary
    manifest (SPEC §4.1). `_offerable_for_fan` puts every enabled standalone into
    every fan's manifest, so an enabled pack would be pitched to people who never
    asked — and the lane has made 4 offers above $60 in a month and sold none.

    🚨 `description_for_ai` is COUNT-FREE. It is stored once and serves fans
    receiving 3 to 12 items, so a stored "11 bare feet pics" is a lie to the fan
    who got 7 — and it is the text `_pending_block` answers "its feet right?"
    from. The number lives only in the rendered caption.
    """
    cat = vault_pack_picker.CATEGORIES[category]
    tag = f"rung:{cat.folder_name(rung)}"
    phrase = RUNG_PHRASES.get((category, rung), f"{category}, {rung}")
    async with get_session() as s:
        row = (await s.execute(
            select(CatalogItem).where(
                CatalogItem.account_id == str(account_id),
                CatalogItem.script_id.is_(None),
                CatalogItem.tags.like(f"%{tag}%"))
        )).scalars().first()
        if row is None:
            row = CatalogItem(
                account_id=str(account_id), script_id=None, kind="image_set",
                label=f"{category} · {rung}",
                tags=json.dumps([tag]),
                media_ids="[]",           # resolved LIVE at send time
                preview_media_ids="[]",
                price_cents=RUNG_STICKER_CENTS.get(rung, 5900),
                enabled=False,            # never in the ordinary manifest
            )
            s.add(row)
        # Keep the sticker current even on a row created before this existed.
        row.price_cents = RUNG_STICKER_CENTS.get(rung, 5900)
        row.description_for_ai = (
            f"{phrase}. Sold as a set of photos from her {category} collection; "
            f"the number of photos depends on the price."
        )
        await s.commit()
        await s.refresh(row)
        return row


# ── Price and count ─────────────────────────────────────────────────

async def quote_pack(account_id: str, fan_id: int, item: CatalogItem,
                     avail: int) -> tuple[int, int] | None:
    """`(price_cents, n)` — or None meaning DO NOT OFFER.

    The price is quoted first, the count derives from it, and then the SHELF MAY
    VETO the price down to what it can honestly fill. A pack the shelf cannot
    fill is not discounted into existence; it is refused.
    """
    from .ai_chatter import _paid_ppv_facts        # local: avoid an import cycle

    max_paid, last_paid = await _paid_ppv_facts(str(account_id), int(fan_id))
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
    n = _clamp_count(px)
    if avail < n:                    # the shelf vetoes the price
        px = max(upsell.OF_PRICE_FLOOR_CENTS, avail * CENTS_PER_ITEM)
        n = _clamp_count(px)
    n = min(n, avail)
    if n < MIN_ITEMS:
        return None
    return px, n


# ── Planning a send ─────────────────────────────────────────────────

async def plan_pack(account_id: str, fan_id: int, category: str, rung: str, *,
                    cfg: dict | None = None,
                    media_kind: str | None = None) -> PackPlan:
    """Everything a send needs, or a typed refusal. Sends nothing.

    Deliberately separable from `send_pack` so the operator can dry-run the whole
    decision — price, count, exact media, the rendered caption clause — before a
    single message goes out.
    """
    cfg = cfg or {}
    empty = PackPlan(str(account_id), int(fan_id), category, rung, None, [], [], 0, "")

    if not cfg.get("pack_send_enabled"):
        return PackPlan(**{**empty.__dict__, "refusal": REFUSE_DISABLED})

    lang = str(cfg.get("language") or "en").strip().lower()
    if lang not in PACK_LANGUAGES:
        return PackPlan(**{**empty.__dict__, "refusal": REFUSE_LANGUAGE, "detail": lang})

    shelf = await _shelf_media(account_id, category, rung)
    if not shelf:
        return PackPlan(**{**empty.__dict__, "refusal": REFUSE_NO_SHELF})

    bought = await _bought_media(account_id, fan_id)
    avail_ids = [m for m in shelf if m not in bought]
    # A promised media KIND narrows the shelf before the price is quoted, so a
    # "send me a video" ask can never be filled with photos.
    avail_ids = await _filter_kind(account_id, avail_ids, media_kind)
    # ...and a fan who has bought nothing is served the tame end of the shelf
    # first. Ordering, not exclusion — see `_rank_by_tier`.
    _cap, max_tier = await spend_bounds(account_id, fan_id)
    avail_ids = await _rank_by_tier(account_id, avail_ids, max_tier)
    if len(avail_ids) < MIN_ITEMS:
        return PackPlan(**{**empty.__dict__, "refusal": REFUSE_TOO_THIN,
                           "detail": f"{len(avail_ids)} un-bought"})

    item = await ensure_pack_item(account_id, category, rung)
    quoted = await quote_pack(account_id, fan_id, item, len(avail_ids))
    if quoted is None:
        return PackPlan(**{**empty.__dict__, "item_id": item.id,
                           "refusal": REFUSE_NO_PRICE})
    px, _flat_n = quoted

    # Compose by VALUE, not by a flat count: a 40-second explicit clip is worth
    # more than eight tease stills, and the fan should get either — but only if
    # what he is charged is actually covered by what is attached.
    media, value = await compose_by_value(account_id, avail_ids, px)
    if len(media) < MIN_ITEMS:
        return PackPlan(**{**empty.__dict__, "item_id": item.id,
                           "refusal": REFUSE_TOO_THIN,
                           "detail": f"{len(media)} composable"})
    # Operator ruling 2026-08-11: "we can price more for that content if the AI
    # upseller chooses." So the rate card is a BASELINE, not a ceiling — the
    # upseller knows this fan's history and willingness and may quote above it.
    #
    # ⚠️ What ticket 06 actually protected against was charging a lot for FEW
    # items ($19.67 an item, 3 items, an account deleted). That protection now
    # lives in the COUNT, not the price: MIN_ITEMS is a hard floor, composition
    # pads toward SOFT_TARGET_ITEMS, and the caption states the real number. So
    # a high price arrives with a full pack rather than a thin one.
    #
    # `value_caps_price` restores the old hard veto for anyone who wants it.
    if value < px and bool(cfg.get("value_caps_price")):
        px = max(upsell.OF_PRICE_FLOOR_CENTS, value)
    if value < px:
        log.info("pack priced ABOVE rate card account=%s fan=%s px=%s value=%s "
                 "ratio=%.2f n=%s", account_id, fan_id, px, value,
                 px / max(1, value), len(media))
    tease = await _shelf_media(account_id, category, "tease")
    previews = [m for m in tease if m not in media][:PREVIEW_MAX]
    # Previews ride FREE inside the send and are never stamped owned, so they may
    # repeat across sends. `previews ⊆ media` is what the audit's rule 1 wants, so
    # they join the attached set while staying out of the paid count.
    attached = media + previews

    clause = render_clause(category, rung, len(media))
    warn = await mirror_warning(account_id)
    if warn:
        log.info("pack plan account=%s fan=%s: %s", account_id, fan_id, warn)
    bad = await audit_pack(account_id, category, rung, attached, previews)
    if bad:
        log.warning("pack audit REFUSED account=%s fan=%s %s-%s: %s",
                    account_id, fan_id, category, rung, "; ".join(bad))
        return PackPlan(**{**empty.__dict__, "item_id": item.id, "price_cents": px,
                           "clause": clause, "refusal": REFUSE_AUDIT,
                           "detail": "; ".join(bad)})

    return PackPlan(str(account_id), int(fan_id), category, rung, item.id,
                    media, previews, px, clause, value_cents=value)


def compose_caption(clause: str, voice_line: str | None) -> str:
    """Claim clause first, then her reply to the thread.

    ⚠️ The clause LEADS because OF truncates a long caption in the chat-list
    preview: a voice-first caption shows him the flirt and hides the subject at
    exactly the moment he decides whether to open it.

    The voice line is rejected — not trimmed — when it carries a digit, a
    currency symbol or a content-claim word. The clause above it is the contract
    and a second claim underneath can only contradict it.
    """
    line = " ".join(str(voice_line or "").split())
    if not line or _VOICE_BAN.search(line):
        return clause
    budget = _REPLY_MAX_CHARS - len(clause) - 2
    if budget <= 0:
        return clause
    return f"{clause}\n\n{line[:budget]}"

# ── The send ────────────────────────────────────────────────────────

async def send_pack(client, account_id: str, fan_id: int, category: str,
                    rung: str, *, cfg: dict | None = None,
                    voice_line: str | None = None,
                    dry_run: bool = False) -> dict:
    """Send ONE pack, priced, to ONE fan. Per-chat, attributed, out of band.

    Replays the shipped 1:1 PPV block (`ai_chatter.py:3258-3300`) with an
    EXPLICIT item instead of letting the model choose one. Five shipped helpers
    do the work; the only new thing here is that the item is decided before the
    message is written.

    ⚠️ **NOT `ppv_send` with `only_fan_ids`.** That looks free and is a trap:
    `send_mass_message.run` mints a `MassRun` row unconditionally, so a one-fan
    ppv_send is a MASS row — auto-unsent at 48h on both pilots, writing no
    `ContentOffer` at all, and priced by segment multipliers so the quote above
    would simply not run.

    🚨 `locked_text=False` and `paid_ppv` is never set. Either one paywalls the
    claim clause, and the clause is the one thing that must be readable BEFORE
    he pays — it is the contract.

    🚨 `_record_vault_sends` gets the `[:n]` SLICE, never the whole shelf.
    Recording the shelf would stamp every item sent and, on purchase, owned —
    silently deleting every future sale from this rung.
    """
    from attribution import write_outbound_attribution      # local: import cycle
    from .ai_chatter import _record_offer, _record_vault_sends

    cfg = cfg or {}
    plan = await plan_pack(account_id, fan_id, category, rung, cfg=cfg)
    if not plan.ok:
        log.info("pack refused account=%s fan=%s %s-%s: %s %s",
                 account_id, fan_id, category, rung, plan.refusal, plan.detail)
        return {"status": "refused", "reason": plan.refusal, "detail": plan.detail,
                "price_cents": plan.price_cents, "n": len(plan.media)}

    caption = compose_caption(plan.clause, voice_line)
    if dry_run:
        return {"status": "dry_run", "price_cents": plan.price_cents,
                "n": len(plan.media), "media": plan.media,
                "previews": plan.previews, "caption": caption,
                "item_id": plan.item_id}

    # The audit runs AGAIN here, immediately before the wire. Folder membership
    # is mutable and this map's own re-triage moved 19 items after the fact: a
    # pack that passed at plan time can be a lie by send time.
    bad = await audit_pack(account_id, category, rung,
                           plan.media + plan.previews, plan.previews)
    if bad:
        log.warning("pack audit REFUSED AT SEND account=%s fan=%s: %s",
                    account_id, fan_id, "; ".join(bad))
        return {"status": "refused", "reason": REFUSE_AUDIT, "detail": "; ".join(bad)}

    kwargs: dict = {"price": plan.price_cents / 100, "locked_text": False,
                    "media_files": list(plan.media)}
    if plan.previews:
        kwargs["previews"] = list(plan.previews)
    try:
        result = await asyncio.to_thread(
            lambda: client.send_message(int(fan_id), caption, **kwargs))
    except Exception as e:  # noqa: BLE001
        log.warning("pack send failed account=%s fan=%s", account_id, fan_id,
                    exc_info=True)
        return {"status": "error", "reason": "send_failed", "detail": str(e)[:200]}

    msg_id = result.get("id") if isinstance(result, dict) else None
    if not msg_id:
        return {"status": "error", "reason": "no_message_id"}

    await write_outbound_attribution(
        account_id=str(account_id), fan_id=int(fan_id), message_id=int(msg_id),
        sent_by_employee_id=None, automation_kind="pack_send",
        body=str(result.get("text") or caption), price_cents=plan.price_cents,
        created_at=datetime.utcnow(), emit_live=True)
    # The SLICE, never the shelf.
    await _record_vault_sends(str(account_id), int(fan_id), list(plan.media),
                              int(msg_id), plan.price_cents)
    async with get_session() as s:
        item = await s.get(CatalogItem, int(plan.item_id))
    await _record_offer(str(account_id), int(fan_id), item, "ppv", int(msg_id),
                        quoted_cents=plan.price_cents)

    log.info("pack SENT account=%s fan=%s %s-%s n=%s px=%s msg=%s",
             account_id, fan_id, category, rung, len(plan.media),
             plan.price_cents, msg_id)
    return {"status": "ok", "message_id": int(msg_id), "item_id": plan.item_id,
            "price_cents": plan.price_cents, "n": len(plan.media),
            "media": plan.media, "previews": plan.previews, "caption": caption}



# ── The ask trigger ─────────────────────────────────────────────────

# 🚨 `tease` is PREVIEWS ONLY — operator ruling 2026-08-10. It rides free inside
# a paid send and is never sold on its own, so an ask never resolves to it.
DEFAULT_RUNG = "nude"
# Rung 3 is quotable for 23 of 119 fans COLD and 104 of 119 after he buys rung 2
# (MAX_ASK_VS_HISTORY_MULT = 3.0 on his largest paid PPV). Cold it is refused for
# 96 of 119 — silently — which is the `gonna shower` failure shape. So it is
# never the first ask.
LADDER_RUNG = "nude-body"


async def _has_bought_from(account_id: str, fan_id: int, category: str) -> bool:
    """Has he bought anything from this category before? Gates the ladder rung."""
    bought = await _bought_media(account_id, fan_id)
    if not bought:
        return False
    cat = vault_pack_picker.CATEGORIES.get(category)
    if cat is None:
        return False
    for rung in cat.rungs:
        if bought & set(await _shelf_media(account_id, category, rung)):
            return True
    return False


async def send_pack_on_ask(client, account_id: str, fan_id: int, *,
                           cfg: dict | None = None,
                           voice_line: str | None = None,
                           dry_run: bool = False) -> dict:
    """He asked for content → sell him that rung. The whole loop, one call.

    The ask is read from the THREAD (`content_resolver.read_contract`), not from
    a stored profile: a profile fact is advisory and a thing he just said is
    binding. `fetishes` is deliberately not consulted.

    Rung choice is the shipped ladder, not a preference:
      * `tease` is previews only and is never sold;
      * `nude` is the product and the first ask;
      * `nude-body` only once he has bought from this category — cold it is
        refused for 96 of 119 fans, silently, which is the failure shape this
        map has warned about three times.

    Refuses — loudly, in the return value — rather than substituting. For a
    strict promise a generic send is worse than no send: it turns a recoverable
    delay into a second deception.
    """
    cfg = cfg or {}
    if not cfg.get("pack_send_enabled"):
        return {"status": "refused", "reason": REFUSE_DISABLED}

    contract = await content_resolver.read_contract(str(account_id), int(fan_id))
    if not contract.category:
        return {"status": "refused", "reason": content_resolver.NO_ASK,
                "detail": contract.subject or ""}

    category = contract.category
    rung = contract.rung if contract.rung in (DEFAULT_RUNG, LADDER_RUNG) else DEFAULT_RUNG
    if rung == LADDER_RUNG and not await _has_bought_from(account_id, fan_id, category):
        rung = DEFAULT_RUNG

    res = await send_pack(client, account_id, fan_id, category, rung, cfg=cfg,
                          voice_line=voice_line, dry_run=dry_run)
    # He asked for the product rung and it is spent — try the ladder rung, but
    # only if he has already bought from this category.
    if (res.get("reason") == REFUSE_TOO_THIN and rung == DEFAULT_RUNG
            and await _has_bought_from(account_id, fan_id, category)):
        res = await send_pack(client, account_id, fan_id, category, LADDER_RUNG,
                              cfg=cfg, voice_line=voice_line, dry_run=dry_run)
    res.setdefault("category", category)
    res.setdefault("asked", contract.quote)
    return res
