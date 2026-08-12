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

## Where the rest of it lives

Split on 2026-08-11, when this file crossed 1,200 lines:
  * `pack_pricing` — the rate card, the per-fan ceiling, the tier ordering, and
    which pieces cover a quote. *What is it worth and what may he be charged.*
  * `pack_claim` — every word the fan reads, and the count it promises.
    *The contract.*
This module keeps the shelf, the audits, the product row, and the wire.

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
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from sqlalchemy import func, select

import ownership
import vault_pack_picker
from db.engine import get_session
from db.models import (
    CatalogItem, VaultCacheRun, VaultFolder, VaultFolderItem, VaultItem, VaultSend,
)

from . import content_resolver, pack_pricing
from .pack_claim import (
    Claim, ask_clause, compose_caption, is_substitute_claim, product_description,
    render_clause, substitute_clause,
)
from .pack_pricing import (
    MAX_ITEMS, MIN_ITEMS, RUNG_STICKER_CENTS, DEFAULT_STICKER_CENTS,
    PACK_ATTR_KIND, negotiate_pack, rank_by_tier, spend_bounds,
)

log = logging.getLogger("of-relay.automation.pack_sender")

PREVIEW_MAX = 3

# 🚨 ENGLISH ONLY, narrowed from {en, es, sl} on 2026-08-11.
#
# The gate admitted the three languages `script_packs` ships, but the CLAUSE — the
# one line the fan reads before he pays, and the thing this module exists to keep
# honest — is built in English by `render_clause`/`ask_clause` and by the authored
# `RUNG_PHRASES`, which only carry ("feet", …) in English. An `es` account
# therefore passed the gate and paywalled "3 pics of cuero": a mixed-language
# claim, on the exact field a dispute turns on.
#
# That was survivable while the flags shipped OFF and neither pilot was Spanish.
# It stopped being survivable the moment the default flipped ON across the
# roster, which includes one `es` account. Widen this again when the clause is
# authored per language, not before.
PACK_LANGUAGES = frozenset({"en"})

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
# Two of these are RE-EXPORTED, not re-declared: `pack_pricing` raises them from
# inside the negotiation and this module reports them, and a second copy of the
# string would be free to drift from the one the arithmetic actually returns.
REFUSE_DISABLED = "pack_disabled"
REFUSE_NO_SHELF = "no_shelf"
REFUSE_TOO_THIN = pack_pricing.REFUSE_TOO_THIN   # fewer than min_items un-bought
REFUSE_AUDIT = "audit_failed"
REFUSE_LANGUAGE = "unsupported_language"
REFUSE_NO_PRICE = pack_pricing.REFUSE_NO_PRICE
REFUSE_RESOLVER = "resolver_refused"


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
    claim: Claim                # the caption's promise, and the count it makes
    refusal: str | None = None
    detail: str = ""
    value_cents: int = 0        # rate-card worth of the attached payoff
    # This send is NOT what he asked for. The claim frames it as her own version
    # instead of promising his noun, and `audit_ask` gets a fourth rule.
    substitute: bool = False

    @property
    def ok(self) -> bool:
        return self.refusal is None and bool(self.media)


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
                price_cents=RUNG_STICKER_CENTS.get(rung, DEFAULT_STICKER_CENTS),
                enabled=False,            # never in the ordinary manifest
            )
            s.add(row)
        # Keep the sticker current even on a row created before this existed.
        row.price_cents = RUNG_STICKER_CENTS.get(rung, DEFAULT_STICKER_CENTS)
        row.description_for_ai = product_description(category, rung)
        await s.commit()
        await s.refresh(row)
        return row


# ── Price and count ─────────────────────────────────────────────────

# ── Planning a send ─────────────────────────────────────────────────

# ── The shared spine ────────────────────────────────────────────────

def _guard(cfg: dict, empty: PackPlan) -> PackPlan | None:
    """The two refusals every planner owes before it touches the vault."""
    if not cfg.get("pack_send_enabled"):
        return replace(empty, refusal=REFUSE_DISABLED)
    lang = str(cfg.get("language") or "en").strip().lower()
    if lang not in PACK_LANGUAGES:
        return replace(empty, refusal=REFUSE_LANGUAGE, detail=lang)
    return None


@dataclass(frozen=True)
class _Priced:
    """A priced, composed pack — or the refusal that stopped it."""
    media: list[int]
    price_cents: int
    value_cents: int
    item_id: int | None
    refusal: PackPlan | None = None


async def _price_and_compose(account_id: str, fan_id: int, avail_ids: list[int],
                             item: CatalogItem, cfg: dict,
                             empty: PackPlan, *,
                             min_items: int = MIN_ITEMS) -> _Priced:
    """Quote the fan, then fill the quote with content. Identical for every source.

    🚨 This is the half `plan_ask` used to COPY from `plan_pack`. Ten of the two
    functions' fourteen steps were byte-identical, and the duplication had
    already drifted inside one session: the `media_kind` fix landed on the pack
    path only, and the "priced above the rate card" log existed in one copy.
    Two sources of media, ONE pricing rule.

    The negotiation itself is `pack_pricing.negotiate_pack` — price and content
    decide each other, and that argument belongs beside the ladder it is made of
    rather than in the sender. What is left here is the only part that is the
    SENDER's: turning a refusal reason into the typed `PackPlan` a planner
    returns.
    """
    pack = await negotiate_pack(account_id, fan_id, item, avail_ids, cfg,
                                min_items=min_items)
    if pack.refusal == REFUSE_NO_PRICE:
        return _Priced([], 0, 0, item.id,
                       replace(empty, item_id=item.id, refusal=REFUSE_NO_PRICE))
    if pack.refusal is not None:
        return _Priced([], pack.price_cents, pack.value_cents, item.id,
                       replace(empty, item_id=item.id, refusal=REFUSE_TOO_THIN,
                               detail=f"{len(pack.media)} composable"))
    return _Priced(pack.media, pack.price_cents, pack.value_cents, item.id)


async def _available(account_id: str, fan_id: int, media_ids: list[int], *,
                     company: bool, media_kind: str | None = None) -> list[int]:
    """The house rules every source obeys, in the order they must run.

    KIND is a promise, so it narrows before anything is priced. SOLO is applied
    even to a curated shelf, because an operator who filed a photo under `feet`
    said nothing about who else was in it. TIER is last because it ORDERS rather
    than excludes — see `rank_by_tier`.
    """
    if media_kind:
        media_ids = await _filter_kind(account_id, media_ids, media_kind)
    if not company:
        media_ids = await content_resolver.solo_only(account_id, media_ids)
    _cap, max_tier = await spend_bounds(account_id, fan_id)
    return await rank_by_tier(account_id, media_ids, max_tier)


async def plan_pack(account_id: str, fan_id: int, category: str, rung: str, *,
                    cfg: dict | None = None,
                    media_kind: str | None = None,
                    company: bool = False) -> PackPlan:
    """Everything a send needs, or a typed refusal. Sends nothing.

    Deliberately separable from `send_pack` so the operator can dry-run the whole
    decision — price, count, exact media, the rendered caption clause — before a
    single message goes out.

    A CURATED source: the media is one rung of one operator-built shelf, and the
    claim is an authored rung phrase. Everything between "here are the ids" and
    "here is the price" is `_price_and_compose`, shared with `plan_ask`.
    """
    cfg = cfg or {}
    empty = PackPlan(str(account_id), int(fan_id), category, rung, None, [], [],
                     0, Claim("", 0))
    stop = _guard(cfg, empty)
    if stop is not None:
        return stop

    shelf = await _shelf_media(account_id, category, rung)
    if not shelf:
        return replace(empty, refusal=REFUSE_NO_SHELF)
    bought = await _bought_media(account_id, fan_id)
    avail_ids = await _available(account_id, fan_id,
                                 [m for m in shelf if m not in bought],
                                 company=company, media_kind=media_kind)
    if len(avail_ids) < MIN_ITEMS:
        return replace(empty, refusal=REFUSE_TOO_THIN,
                       detail=f"{len(avail_ids)} un-bought")

    item = await ensure_pack_item(account_id, category, rung)
    priced = await _price_and_compose(account_id, fan_id, avail_ids, item, cfg, empty)
    if priced.refusal is not None:
        return priced.refusal

    tease = await _shelf_media(account_id, category, "tease")
    previews = [m for m in tease if m not in priced.media][:PREVIEW_MAX]
    # Previews ride FREE inside the send and are never stamped owned, so they may
    # repeat across sends. `previews ⊆ media` is what the audit's rule 1 wants, so
    # they join the attached set while staying out of the paid count.
    kinds = await content_resolver.kind_of(account_id, priced.media)
    claim = render_clause(category, rung,
                          [kinds.get(m, "photo") for m in priced.media])
    warn = await mirror_warning(account_id)
    if warn:
        log.info("pack plan account=%s fan=%s: %s", account_id, fan_id, warn)
    bad = await audit_pack(account_id, category, rung,
                           priced.media + previews, previews)
    if bad:
        log.warning("pack audit REFUSED account=%s fan=%s %s-%s: %s",
                    account_id, fan_id, category, rung, "; ".join(bad))
        return replace(empty, item_id=item.id, price_cents=priced.price_cents,
                       claim=claim, refusal=REFUSE_AUDIT, detail="; ".join(bad))
    return PackPlan(str(account_id), int(fan_id), category, rung, item.id,
                    priced.media, previews, priced.price_cents, claim,
                    value_cents=priced.value_cents)


# ── The vault-wide ask ──────────────────────────────────────────────
#
# 🚨 THE GAP THIS CLOSES. Until 2026-08-11 `send_pack_on_ask` refused unless the
# subject mapped to a CURATED category, and exactly one exists (`feet`). So a
# man asking for leather, for booty, for "vids for purchase", or saying a bare
# "show me" could never be sold anything — the resolver found him good media and
# the sender threw it away. That is the operator's ruling from the same day
# ("the pool is the curated shelf UNION the whole vault") applied to the SEND
# path, which had only ever been applied to the POOL.
ASK_CATEGORY = "ask"          # the attribution bucket — NOT a curated category

async def ensure_ask_item(account_id: str) -> CatalogItem:
    """ONE reusable `CatalogItem` for vault-wide ask sends, per account.

    `ContentOffer.item_id` is a non-nullable FK, so a send with no row cannot be
    attributed and cannot answer "did answering his ask make money". One row per
    account rather than one per subject: subjects are the fan's own words and
    unbounded, and a table growing a row per phrase a man types is a leak.

    `enabled=False` for the same reason `ensure_pack_item` sets it —
    `_offerable_for_fan` puts every enabled standalone into every fan's
    manifest, and this must only ever reach someone who asked.
    """
    tag = "rung:vault-ask"
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
                label="vault · asked for", enabled=False,
                # COUNT-FREE and SUBJECT-FREE: one row serves every ask, so any
                # number or noun stored here is a lie to most of the fans who
                # receive it. Both live only in the rendered clause.
                description_for_ai="content from her vault, picked to match what "
                                   "he asked for",
                price_cents=RUNG_STICKER_CENTS.get("nude", DEFAULT_STICKER_CENTS),
                tags=json.dumps([tag]))
            s.add(row)
            await s.flush()
        return row


async def audit_ask(account_id: str, media: list[int], claim: Claim,
                    company: bool, *, substitute: bool = False) -> list[str]:
    """What can honestly be checked when there is no rung to check against.

    `audit_pack`'s rules 2 and 3 are rung-membership rules and have no meaning
    here — there is no shelf, and inventing one would be theatre. Three things
    still hold and all three have drawn blood before:

      1. the count in the claim is the count he receives (the claim IS the
         contract, and a caption that over-counts is the 2026-07-31 shape);
      2. every id is still a live `VaultItem` — a soft-deleted id is charged for
         and never arrives;
      3. nobody else is in it unless he asked (2026-08-11, "very important").

    🚨 Rule 1 compares two integers. It used to recover the count by summing every
    digit in the RENDERED clause, and the clause ends in the fan's own words — so
    "34DD tits" summed to 41 against 7 attached items, "2 girls" to 9, "your top 3
    sets" to 10, and each of those men got a silent `audit_failed` instead of the
    thing he asked for. `Claim` carries the number the caption actually made.
    """
    bad: list[str] = []
    if not media:
        return ["nothing attached"]
    n = len(media)
    if claim.n != n:
        bad.append(f"claim states {claim.n}, attaching {n}: {claim.text!r}")

    # 4. A SUBSTITUTE says so. Rules 1-3 check counts, liveness and company and
    #    none of them reads the noun, so `ask_clause`'s "4 vids of joi" over
    #    media that is not joi passes all three — a lie with a clean audit. The
    #    frame is the only thing standing between "this is my version of it" and
    #    a promise she cannot keep, so its survival is itself an audit rule.
    if substitute and not is_substitute_claim(claim.text):
        bad.append(f"substitute claim lost its frame: {claim.text!r}")

    async with get_session() as s:
        live = {int(m) for m in (await s.execute(
            select(VaultItem.media_id).where(
                VaultItem.account_id == str(account_id),
                VaultItem.media_id.in_(media),
                VaultItem.removed_at.is_(None))
        )).scalars().all()}
    dead = [m for m in media if m not in live]
    if dead:
        bad.append(f"{len(dead)} dead media: {dead[:4]}")

    if not company:
        solo = set(await content_resolver.solo_only(account_id, media))
        others = [m for m in media if m not in solo]
        if others:
            bad.append(f"{len(others)} with someone else in them: {others[:4]}")
    return bad


@dataclass(frozen=True)
class _AskEnding:
    """The two ways an ask ends, and everything that differs between them.

    🚨 These three facts ALWAYS move together — a substitute needs a lower floor
    (the closest thing she owns is often a single clip), a framing clause, and
    the audit rule that checks the frame survived. Carrying them as three
    separate arguments is how `plan_ask` came to be a copy of `plan_pack` with
    ten of fourteen steps byte-identical; carrying them as one value is why
    there is now a single planner instead of two.
    """
    min_items: int
    clause: Callable[[list[str], str | None], Claim]
    substitute: bool


# `MIN_ITEMS` (3) exists to stop a lot of money buying few items — the shape
# that got an account deleted — and it still binds a real ask. A substitute is
# not a pack, though: it is the one honest clip she has, and refusing to send it
# under a pack's floor is how this lane goes quiet again.
_FITS = _AskEnding(MIN_ITEMS, ask_clause, False)
_SUBSTITUTE = _AskEnding(1, substitute_clause, True)


async def _plan_ask_from(account_id: str, fan_id: int,
                         contract: content_resolver.Contract,
                         media_ids: list[int], cfg: dict, empty: PackPlan,
                         ending: _AskEnding) -> PackPlan:
    """Price and caption resolved ids. The one spine both endings ride.

    `media_kind` is not passed to `_available`: the resolver has already
    enforced it as part of the contract.
    """
    avail_ids = await _available(account_id, fan_id, media_ids,
                                 company=contract.company)
    if len(avail_ids) < ending.min_items:
        return replace(empty, refusal=REFUSE_TOO_THIN,
                       detail=f"{len(avail_ids)} un-bought")

    item = await ensure_ask_item(account_id)
    priced = await _price_and_compose(account_id, fan_id, avail_ids, item, cfg,
                                      empty, min_items=ending.min_items)
    if priced.refusal is not None:
        return priced.refusal

    kinds = await content_resolver.kind_of(account_id, priced.media)
    claim = ending.clause([kinds.get(m, "photo") for m in priced.media],
                          contract.subject)
    # ⚠️ No previews. `plan_pack` draws them from the `tease` rung, and a
    # vault-wide ask has no rung to draw from. Attaching an arbitrary vault item
    # as a free preview would give away payoff — audit rule 3, in spirit. The
    # cost is that he sees OF's own blur instead of a chosen frame, which is
    # worth revisiting once there is a tease shelf per subject.
    bad = await audit_ask(account_id, priced.media, claim, contract.company,
                          substitute=ending.substitute)
    if bad:
        log.warning("ask audit REFUSED account=%s fan=%s substitute=%s: %s",
                    account_id, fan_id, ending.substitute, "; ".join(bad))
        return replace(empty, item_id=item.id, price_cents=priced.price_cents,
                       claim=claim, refusal=REFUSE_AUDIT, detail="; ".join(bad))
    return PackPlan(str(account_id), int(fan_id), ASK_CATEGORY, "", item.id,
                    priced.media, [], priced.price_cents, claim,
                    value_cents=priced.value_cents,
                    substitute=ending.substitute)


async def plan_ask(account_id: str, fan_id: int,
                   contract: content_resolver.Contract, *,
                   cfg: dict | None = None) -> PackPlan:
    """A priced pack drawn from the WHOLE VAULT, for an ask with no curated rung.

    The same spine as `plan_pack` — same price ladder, same value composition,
    same house rules — differing only in where the ids come from (the resolver,
    not a shelf), which catalog row carries attribution, and how the claim is
    made.

    Two endings, one spine. Either the resolver found what he asked for, or it
    found the nearest thing and the caption says so.
    """
    cfg = cfg or {}
    empty = PackPlan(str(account_id), int(fan_id), ASK_CATEGORY, "", None,
                     [], [], 0, Claim("", 0))
    stop = _guard(cfg, empty)
    if stop is not None:
        return stop

    bought = await _bought_media(account_id, fan_id)
    # MAX_ITEMS wide: the resolver ranks, and composition decides how much of
    # that ranking the price actually buys.
    res = await content_resolver.resolve(
        str(account_id), int(fan_id), count=MAX_ITEMS, seen=bought,
        contract=contract, require_curated=False)
    if res.ok:
        return await _plan_ask_from(account_id, fan_id, contract,
                                    res.media_ids, cfg, empty, _FITS)

    # 🚨 "i don't have that, but this is close" — the 2026-08-11 operator ruling,
    # finally reaching a fan. Until now every refusal here returned nothing and
    # ai_chatter fell through to a reply written before the ask was even read:
    # one fan asked for the same thing twice and got an unrelated priced set,
    # captioned with nothing, that he never opened.
    #
    # A STRICT contract is the exception. "only feet, right?" may never be
    # answered with something adjacent — that is the one promise where a
    # substitute is a second deception rather than a recovery, and
    # `Contract.strict` exists to carry exactly that.
    if contract.strict or not res.alternatives:
        return replace(empty, refusal=REFUSE_RESOLVER, detail=res.refusal or "")
    log.info("substitute for %r account=%s fan=%s (%s)",
             contract.subject, account_id, fan_id, res.refusal)
    return await _plan_ask_from(account_id, fan_id, contract,
                                res.alternatives, cfg, empty, _SUBSTITUTE)


# ── The send ────────────────────────────────────────────────────────

async def send_pack(client, account_id: str, fan_id: int, category: str,
                    rung: str, *, cfg: dict | None = None,
                    voice_line: str | None = None,
                    company: bool = False,
                    media_kind: str | None = None,
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
    cfg = cfg or {}
    plan = await plan_pack(account_id, fan_id, category, rung, cfg=cfg,
                           company=company, media_kind=media_kind)
    if not plan.ok:
        log.info("pack refused account=%s fan=%s %s-%s: %s %s",
                 account_id, fan_id, category, rung, plan.refusal, plan.detail)
        return {"status": "refused", "reason": plan.refusal, "detail": plan.detail,
                "price_cents": plan.price_cents, "n": len(plan.media)}

    return await _deliver(
        client, plan, voice_line=voice_line, dry_run=dry_run,
        # The audit runs AGAIN immediately before the wire. Folder membership is
        # mutable and this map's own re-triage moved 19 items after the fact: a
        # pack that passed at plan time can be a lie by send time.
        reaudit=lambda: audit_pack(account_id, category, rung,
                                   plan.media + plan.previews, plan.previews))


async def _deliver(client, plan: PackPlan, *, voice_line: str | None,
                   dry_run: bool, reaudit) -> dict:
    """The wire, shared by every pack path.

    Extracted when the vault-wide ask arrived: two senders that both mint a PPV,
    write attribution, stamp vault sends and record an offer WILL drift, and the
    half that drifts is the half that stops being attributed.
    """
    from attribution import write_outbound_attribution      # local: import cycle
    from .ai_chatter import _record_offer, _record_vault_sends

    account_id, fan_id = plan.account_id, plan.fan_id
    caption = compose_caption(plan.claim, voice_line)
    if dry_run:
        return {"status": "dry_run", "price_cents": plan.price_cents,
                "n": len(plan.media), "media": plan.media,
                "previews": plan.previews, "caption": caption,
                "item_id": plan.item_id}

    bad = await reaudit()
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

    # 🚨 EVERYTHING BELOW IS POST-WIRE. The fan has been charged; there is no
    # undo. A raise here used to propagate, so the caller saw a failure for a PPV
    # that HAD been sent — and the next tick, finding no ownership row and no
    # offer, would send it again. A double charge caused by a bookkeeping error
    # is still a double charge.
    #
    # So the send is reported as OK and each record is attempted independently.
    # A lost record is a reporting bug, loud in the log and recoverable from the
    # message id; a lost SEND is money.
    for what, coro in (
        ("attribution", write_outbound_attribution(
            account_id=str(account_id), fan_id=int(fan_id), message_id=int(msg_id),
            # The CONSTANT, never a copy of the string: `pack_pricing` reads this
            # attribution back to count what he has refused, and a drift between
            # the two silently zeroes the walk-down instead of raising.
            sent_by_employee_id=None, automation_kind=PACK_ATTR_KIND,
            body=str(result.get("text") or caption), price_cents=plan.price_cents,
            created_at=datetime.utcnow(), emit_live=True)),
        # The SLICE, never the shelf.
        ("vault_sends", _record_vault_sends(
            str(account_id), int(fan_id), list(plan.media), int(msg_id),
            plan.price_cents)),
    ):
        try:
            await coro
        except Exception:  # noqa: BLE001
            log.exception("pack POST-SEND %s failed account=%s fan=%s msg=%s "
                          "— the fan WAS charged", what, account_id, fan_id, msg_id)
    try:
        async with get_session() as s:
            item = await s.get(CatalogItem, int(plan.item_id))
        await _record_offer(str(account_id), int(fan_id), item, "ppv", int(msg_id),
                            quoted_cents=plan.price_cents)
    except Exception:  # noqa: BLE001
        log.exception("pack POST-SEND offer record failed account=%s fan=%s msg=%s "
                      "— the fan WAS charged", account_id, fan_id, msg_id)

    log.info("pack SENT account=%s fan=%s %s-%s n=%s px=%s msg=%s",
             account_id, fan_id, plan.category, plan.rung or "-",
             len(plan.media), plan.price_cents, msg_id)
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


async def send_ask(client, account_id: str, fan_id: int,
                   contract: content_resolver.Contract, *,
                   cfg: dict | None = None,
                   voice_line: str | None = None,
                   dry_run: bool = False) -> dict:
    """Sell him what he asked for, drawn from the whole vault. One call."""
    cfg = cfg or {}
    if not cfg.get("pack_send_enabled"):
        return {"status": "refused", "reason": REFUSE_DISABLED}
    plan = await plan_ask(account_id, fan_id, contract, cfg=cfg)
    if not plan.ok:
        log.info("ask refused account=%s fan=%s subject=%r: %s %s",
                 account_id, fan_id, contract.subject, plan.refusal, plan.detail)
        return {"status": "refused", "reason": plan.refusal, "detail": plan.detail,
                "price_cents": plan.price_cents, "n": len(plan.media)}
    return await _deliver(
        client, plan, voice_line=voice_line, dry_run=dry_run,
        reaudit=lambda: audit_ask(account_id, plan.media, plan.claim,
                                  contract.company, substitute=plan.substitute))


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
    if not contract.asked:
        return {"status": "refused", "reason": content_resolver.NO_ASK,
                "detail": contract.subject or ""}
    # ⚠️ A CUSTOM REQUEST used to short-circuit to a refusal here, on the
    # reasoning that "i don't have that, but check this" is a REPLY and not a
    # sale. Nothing ever wrote that reply — `alternatives` has no reader
    # anywhere in the service — so the branch spent a year turning the operator
    # ruling into silence. It now falls through like any other ask: the
    # resolver returns CUSTOM_REQUEST *with* substitutes attached, and
    # `plan_ask` frames them as her own version instead of promising his shoot.

    # 🚨 No curated category is the COMMON case, not the error case. Exactly one
    # category exists (`feet`), so before 2026-08-11 a man asking for leather,
    # for booty, for "vids for purchase", or saying a bare "show me" was refused
    # here while the resolver was finding him good media two calls away.
    if not contract.category:
        res = await send_ask(client, account_id, fan_id, contract, cfg=cfg,
                             voice_line=voice_line, dry_run=dry_run)
        res.setdefault("category", ASK_CATEGORY)
        res.setdefault("asked", contract.quote)
        return res

    category = contract.category
    rung = contract.rung if contract.rung in (DEFAULT_RUNG, LADDER_RUNG) else DEFAULT_RUNG
    if rung == LADDER_RUNG and not await _has_bought_from(account_id, fan_id, category):
        rung = DEFAULT_RUNG

    res = await send_pack(client, account_id, fan_id, category, rung, cfg=cfg,
                          voice_line=voice_line, company=contract.company,
                          media_kind=contract.media_kind, dry_run=dry_run)
    # He asked for the product rung and it is spent — try the ladder rung, but
    # only if he has already bought from this category.
    if (res.get("reason") == REFUSE_TOO_THIN and rung == DEFAULT_RUNG
            and await _has_bought_from(account_id, fan_id, category)):
        res = await send_pack(client, account_id, fan_id, category, LADDER_RUNG,
                              cfg=cfg, voice_line=voice_line,
                              company=contract.company,
                              media_kind=contract.media_kind, dry_run=dry_run)
    res.setdefault("category", category)
    res.setdefault("asked", contract.quote)
    return res
