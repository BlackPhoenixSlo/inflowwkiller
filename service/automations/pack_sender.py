"""service/automations/pack_sender.py — sell him the thing he asked for.

A **pack** is one rung of a curated category (`feet-nude`), sold as a priced PPV
to a fan who asked for it. This module owns the product row, the plan (what
would be sent, at what price), and the wire.

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
  * `pack_audit` — the rules that refuse a send whose media does not match its
    caption, plus the shelf read two of them are asked against. *Can it lie?*
    (2026-08-15, when the plan/deliver split pushed this file over 1,000 again.)
  * `pack_farewell` — welcome_chatter_for_info's parting gather-close PPV: its own category,
    catalog row, price rule and trigger. *A different product.* (2026-08-16.)
    It imports the spine FROM here and nothing here imports it back.
This module keeps the product row, the planning, and the wire — decide what to
send (`plan_*`), then send it (`deliver`). Those halves are apart because the
closer owes the fan his ANSWER before the priced box that answers it.

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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import datetime

from sqlalchemy import select

import vault_pack_picker
from db.engine import get_session
from db.models import CATALOG_IS_SINGLE, CatalogItem, VaultItem, VaultSend

from . import content_resolver, pack_pricing
# Re-exported on purpose: `audit_pack` / `audit_ask` / `mirror_warning` are part
# of this module's surface (the operator UI and the pack tests reach them through
# it), and the import line is where a reader learns the rules moved out.
from .pack_audit import (  # noqa: F401
    audit_ask, audit_pack, mirror_warning, shelf_media,
)
from .pack_claim import (
    Claim, ask_clause, compose_caption, needs_action, product_description,
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

# "This account's SHELF cannot serve this ask" — missing, spent/filtered thin,
# failing its own audit, or unpriceable. None of these is a verdict about the
# fan or the account's permission to sell, so `plan_on_ask` answers them from
# the whole vault instead of returning silence (the Footworm dead end,
# 2026-08-28: `feet` is the ONE curated category, so on an account that never
# built the shelf a perfectly-read feet ask died `no_shelf` while the vault
# held 524 matchable videos). Deliberately NOT `REFUSE_DISABLED` /
# `REFUSE_LANGUAGE`: those are brakes, and the vault lane brakes the same way.
_SHELF_CANNOT_SERVE = frozenset({
    REFUSE_NO_SHELF, REFUSE_TOO_THIN, REFUSE_AUDIT, REFUSE_NO_PRICE,
})


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


async def _live_of_kind(account_id: str, media: list[int],
                        kind: str | None) -> list[int]:
    """The ids still in her vault, narrowed to the promised KIND. ONE read.

    Both facts are columns of the SAME mirror row, so asking for them separately
    buys a second round-trip over the same id set on every priced send.

    KIND is a promise: "send me a video" is not satisfied by a photo.

    🚨 LIVENESS IS NOT THE AUDIT'S JOB ALONE, even though the audit is where the
    rule is written down (`pack_audit._live_media`, reached as `audit_ask` rule 2
    and `audit_pack` rule 5). The audit is a VETO — it refuses the WHOLE send
    over one dead id — so a stale `removed_at` does not cost a pick, it silences
    the lane for every fan at once. On 2026-08-11 a render-path 404 stamped 22
    items in one second; two sat in a gather-close folder OF still serves from,
    `rank_by_tier` put one of those first for every fan, and the parting PPV then
    refused three fans out of three, having never once shipped. Dropped here it
    costs one pick instead, and the audit still stands at the wire — where a dead
    id means the media died between the pick and the send, which is the case it
    can actually act on.

    An id with no mirror row at all USED to pass the kind filter (it had no kind
    on file to disagree with) and then die at that audit. It cannot now: a row we
    have never seen is exactly a row we cannot promise will arrive.
    """
    if not media:
        return media
    async with get_session() as s:
        rows = (await s.execute(
            select(VaultItem.media_id, VaultItem.kind).where(
                VaultItem.account_id == str(account_id),
                VaultItem.media_id.in_(media),
                VaultItem.removed_at.is_(None))
        )).all()
    live = {int(m): str(k or "") for m, k in rows}
    # Only "photo"/"video" are real promises; anything else (None included) is
    # "he named no kind", which every kind satisfies.
    want = kind if kind in ("photo", "video") else None
    kept, dead = [], []
    for m in media:
        k = live.get(m)                       # None = no live row, "" = no kind
        if k is None:
            dead.append(m)
        elif want is None or k == want:
            kept.append(m)
    if dead:
        # Never silent. A curated shelf is mirror-derived and cannot go stale
        # this way, but a folder pool is read LIVE from OF — so a steady stream
        # here says OUR mirror is wrong, not that she deleted anything. That is
        # a vault-collect problem, and this line is the only place it surfaces.
        #
        # ⚠️ Worded so it can never be confused with `audit_ask`'s "N dead media"
        # — that one is a REFUSAL and this one is a skip on a send that goes out.
        # Grepping the two together is how a healthy lane reads as a broken one.
        log.info("pick skipped %d dead media (send proceeds) account=%s: %s",
                 len(dead), account_id, dead[:4])
    return kept


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


# ── The product row ─────────────────────────────────────────────────

async def _singleton_item(s, account_id: str, tag: str) -> CatalogItem | None:
    """The account's ONE standalone `CatalogItem` carrying `tag`, or None.

    Every lane's product row is found the same way — this query existed in
    three copies before the third lane arrived, and the copies had already
    started to differ in whitespace only. Creation stays in each `ensure_*`,
    because the fields ARE the lane."""
    return (await s.execute(
        select(CatalogItem).where(
            CatalogItem.account_id == str(account_id),
            CATALOG_IS_SINGLE,
            CatalogItem.tags.like(f"%{tag}%"))
    )).scalars().first()


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
        row = await _singleton_item(s, account_id, tag)
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

    LIVENESS and KIND come off the same mirror row and are read together, first:
    a promise narrows before anything is priced, and an id that cannot arrive is
    dropped here rather than left to veto the whole send (`_live_of_kind`). SOLO
    is applied even to a curated shelf, because an operator who filed a photo
    under `feet` said nothing about who else was in it. TIER is last because it
    ORDERS rather than excludes — see `rank_by_tier`.
    """
    media_ids = await _live_of_kind(account_id, media_ids, media_kind)
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

    shelf = await shelf_media(account_id, category, rung)
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

    tease = await shelf_media(account_id, category, "tease")
    previews = [m for m in tease if m not in priced.media][:PREVIEW_MAX]
    # Previews ride FREE inside the send and are never stamped owned, so they may
    # repeat across sends. `previews ⊆ media` is what the audit's rule 1 wants, so
    # they join the attached set while staying out of the paid count.
    claim = render_clause(category, rung,
                          await _kind_list(account_id, priced.media))
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
        row = await _singleton_item(s, account_id, tag)
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


@dataclass(frozen=True)
class _AskEnding:
    """The two ways an ask ends, and everything that differs between them.

    🚨 These three facts ALWAYS move together — a substitute needs a lower floor
    (the closest thing she owns is often a single clip), a framing clause, and
    the audit rule that checks the frame survived. Carrying them as three
    separate arguments is how `plan_ask` came to be a copy of `plan_pack` with
    ten of fourteen steps byte-identical; carrying them as one value is why
    there is now a single planner instead of two.

    There is deliberately no "may describe the media" field: both clauses take
    the phrase and `pack_claim.needs_action` decides from the SUBJECT whether
    one is worth fetching, so a fourth flag here would have been `True` in both
    endings — a constant wearing the costume of a choice.
    """
    min_items: int
    clause: Callable[[list[str], str | None, str], Claim]
    substitute: bool


# `MIN_ITEMS` (3) exists to stop a lot of money buying few items — the shape
# that got an account deleted — and it still binds a real ask. A substitute is
# not a pack, though: it is the one honest clip she has, and refusing to send it
# under a pack's floor is how this lane goes quiet again.
_FITS = _AskEnding(MIN_ITEMS, ask_clause, False)
_SUBSTITUTE = _AskEnding(1, substitute_clause, True)


async def _kind_list(account_id: str, media: list[int]) -> list[str]:
    """The kind of each attached item, in send order — "photo" when unknown.

    Every claim builder is counted BY KIND ("1 pic + 1 vid + 1 voice note"), so
    this pairing was written out at all three claim sites; a fourth would have
    copied it again.
    """
    kinds = await content_resolver.kind_of(account_id, media)
    return [kinds.get(m, "photo") for m in media]


async def _ask_claim(account_id: str, fan_id: int, media: list[int], *,
                     subject: str | None, substitute: bool,
                     clause: Callable[[list[str], str | None, str], Claim]) -> Claim:
    """The caption for a DECIDED slice — and the one place a model-written
    phrase is allowed into one.

    Both subject-free lanes render through here: the ask (either ending) and the
    gather-close farewell. They had the same hole — `ask_clause` with nothing to
    name ships a bare count, which is how an $87 send went out captioned "3
    vids" — and a fix applied at one of them would have left the other as the
    counter-example.

    ⚠️ The phrase is read off the media being SENT, never the resolved pool: the
    price decides how many of the ranked ids he actually gets, and describing a
    clip that composition dropped is the same lie as an over-stated count, one
    field across. `needs_action` is what keeps this from being an LLM call on
    every priced send.
    """
    action = ""
    if needs_action(subject, substitute):
        action = await content_resolver.action_phrase(account_id, fan_id, media)
    return clause(await _kind_list(account_id, media), subject, action)


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

    claim = await _ask_claim(account_id, fan_id, priced.media,
                             subject=contract.subject,
                             substitute=ending.substitute, clause=ending.clause)
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
    # 🚨 ALWAYS SOMETHING FROM THE VAULT — operator ruling 2026-08-15. Measured
    # that day: one fan asked once, and the same message was resolved 580 times
    # over 44 hours because every attempt refused and he was sent NOTHING. Silence
    # is not the safe answer; it is just the failure nobody logs.
    #
    # Three refusal branches cannot carry alternatives of their own
    # (`LLM_UNAVAILABLE` twice, `VERIFY_REJECTED`) because at that point no
    # trustworthy pool is left. `last_resort` is the floor under them: his profile
    # terms first, the vault at large after, SOLO throughout.
    #
    # ⚠️ A STRICT contract remains the exception, and this is the one place the
    # ruling is not applied. "only feet, right?" — asked three times, answered with
    # a bra selfie — is why this module exists at all: that fan paid $3.25, deleted
    # his OnlyFans account, and wrote "Goodbye, you stupid liar." For an exclusive
    # promise a substitute is not a recovery, it is a second deception, so a strict
    # ask still fails closed and the caller says something instead of sending it.
    alts = res.alternatives or await content_resolver.last_resort(
        str(account_id), int(fan_id), seen=bought, count=MAX_ITEMS)
    if contract.strict or not alts:
        return replace(empty, refusal=REFUSE_RESOLVER, detail=res.refusal or "")
    log.info("substitute for %r account=%s fan=%s (%s, %s from last_resort)",
             contract.subject, account_id, fan_id, res.refusal,
             "0" if res.alternatives else len(alts))
    return await _plan_ask_from(account_id, fan_id, contract,
                                alts, cfg, empty, _SUBSTITUTE)


# ── The send ────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Delivery:
    """A plan that PASSED every decide-time check, carrying the re-audit that must
    still run against the wire. **Nothing has been sent.**

    🚨 THE SEAM, and why it is worth a type. Deciding what to sell reads the
    thread, calls the resolver (up to two LLM calls) and prices the result — all
    read-only, all repeatable. Sending charges a real card and has no undo. They
    were one call because there was one caller; the closer needs them apart, for
    two reasons that are both about the fan:

      * He asked a QUESTION. The answer belongs in front of him before the PPV
        that answers it — a priced box arriving first, with the reply behind it,
        reads as a vending machine.
      * Whether to strip the model's own catalog offer off that reply is only
        knowable once the resolver has actually found media. Guessing early
        deletes a live catalog offer on every ask the vault then refuses.

    The re-audit is a closure rather than a rule name because the two lanes check
    different things — rung membership for a curated pack, the claim's own count
    and company for a whole-vault ask — and each needs the arguments its planner
    already resolved.
    """
    plan: PackPlan
    reaudit: Callable[[], Awaitable[list[str]]]

    @property
    def price_cents(self) -> int:
        return self.plan.price_cents

    @property
    def n(self) -> int:
        return len(self.plan.media)


def _refusal(plan: PackPlan) -> dict:
    """A refused plan, in the wire-result shape every caller already reads."""
    return {"status": "refused", "reason": plan.refusal, "detail": plan.detail,
            "price_cents": plan.price_cents, "n": len(plan.media),
            "category": plan.category}


async def deliver(client, d: Delivery, *, voice_line: str | None = None,
                  dry_run: bool = False) -> dict:
    """Put a decided `Delivery` on the wire. The half that spends money."""
    return await _deliver(client, d.plan, voice_line=voice_line,
                          dry_run=dry_run, reaudit=d.reaudit)


async def plan_pack_delivery(account_id: str, fan_id: int, category: str,
                             rung: str, *, cfg: dict | None = None,
                             company: bool = False,
                             media_kind: str | None = None
                             ) -> tuple[Delivery | None, dict]:
    """Decide a curated-rung pack. `(delivery, refusal)` — exactly one is truthy."""
    cfg = cfg or {}
    plan = await plan_pack(account_id, fan_id, category, rung, cfg=cfg,
                           company=company, media_kind=media_kind)
    if not plan.ok:
        log.info("pack refused account=%s fan=%s %s-%s: %s %s",
                 account_id, fan_id, category, rung, plan.refusal, plan.detail)
        return None, _refusal(plan)
    return Delivery(
        plan,
        # The audit runs AGAIN immediately before the wire. Folder membership is
        # mutable and this map's own re-triage moved 19 items after the fact: a
        # pack that passed at plan time can be a lie by send time. With the two
        # phases split that gap is now a whole reply wide, which makes the
        # re-audit load-bearing rather than belt-and-braces.
        lambda: audit_pack(account_id, category, rung,
                           plan.media + plan.previews, plan.previews),
    ), {}


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
    d, refused = await plan_pack_delivery(account_id, fan_id, category, rung,
                                          cfg=cfg, company=company,
                                          media_kind=media_kind)
    if d is None:
        return refused
    return await deliver(client, d, voice_line=voice_line, dry_run=dry_run)


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
                "item_id": plan.item_id, "category": plan.category}

    bad = await reaudit()
    if bad:
        log.warning("pack audit REFUSED AT SEND account=%s fan=%s: %s",
                    account_id, fan_id, "; ".join(bad))
        return {"status": "refused", "reason": REFUSE_AUDIT, "detail": "; ".join(bad)}

    # Include-only audience, at the single wire point every pack path shares.
    # Belt-and-suspenders: the chat engines are already fenced at their candidate
    # seams, so in enforce mode a fenced fan should never reach here — but this
    # library is callable from any engine, and the manifest classifies it gated.
    import audience_include as _audiences
    _allowed, _why = await _audiences.audience_allows_fan(
        account_id, fan_id, kind="pack_sender")
    if not _allowed:
        log.warning("pack send audience-blocked account=%s fan=%s (%s)",
                    account_id, fan_id, _why)
        return {"status": "refused", "reason": f"audience:{_why}"}

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
            "media": plan.media, "previews": plan.previews, "caption": caption,
            # The plan already knows which lane sold him — `ASK_CATEGORY` for a
            # whole-vault ask, the curated name otherwise. It used to be stapled
            # on by the caller afterwards, in three places, from a value it had
            # re-derived.
            "category": plan.category}



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
        if bought & set(await shelf_media(account_id, category, rung)):
            return True
    return False


async def plan_ask_delivery(account_id: str, fan_id: int,
                            contract: content_resolver.Contract, *,
                            cfg: dict | None = None
                            ) -> tuple[Delivery | None, dict]:
    """Decide a whole-vault ask. `(delivery, refusal)` — exactly one is truthy."""
    cfg = cfg or {}
    if not cfg.get("pack_send_enabled"):
        return None, {"status": "refused", "reason": REFUSE_DISABLED}
    plan = await plan_ask(account_id, fan_id, contract, cfg=cfg)
    if not plan.ok:
        log.info("ask refused account=%s fan=%s subject=%r: %s %s",
                 account_id, fan_id, contract.subject, plan.refusal, plan.detail)
        return None, _refusal(plan)
    return Delivery(
        plan,
        lambda: audit_ask(account_id, plan.media, plan.claim,
                          contract.company, substitute=plan.substitute),
    ), {}


# ── The gather-close farewell ───────────────────────────────────────
#
# MOVED 2026-08-16 to `pack_farewell.py`: it is a whole product (its own
# category, catalog row, price rule and trigger) and it shares only the spine
# above. It imports FROM here; nothing here imports it back, and re-exporting
# it for convenience would make the pair a cycle.


async def plan_on_ask(account_id: str, fan_id: int, *,
                      cfg: dict | None = None) -> tuple[Delivery | None, dict]:
    """He asked for content → decide what to sell him. **Sends nothing.**

    The whole of `send_pack_on_ask` except the wire: read the ask off the thread,
    pick the lane (curated rung or whole vault), resolve, audit, price. See
    `Delivery` for why the two halves are apart, and `send_pack_on_ask` — which is
    now this plus one line — for the rung ladder's reasoning.

    The ladder RETRY lives here rather than around the send because
    `REFUSE_TOO_THIN` is a plan-time refusal: the shelf is spent, which is known
    before anything is charged. Retrying at send time would have meant a second
    wire call after the first had already been reported.
    """
    cfg = cfg or {}
    if not cfg.get("pack_send_enabled"):
        return None, {"status": "refused", "reason": REFUSE_DISABLED}

    contract = await content_resolver.read_contract(str(account_id), int(fan_id))
    if not contract.asked:
        return None, {"status": "refused", "reason": content_resolver.NO_ASK,
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
        return await plan_ask_delivery(account_id, fan_id, contract, cfg=cfg)

    category = contract.category
    rung = contract.rung if contract.rung in (DEFAULT_RUNG, LADDER_RUNG) else DEFAULT_RUNG
    if rung == LADDER_RUNG and not await _has_bought_from(account_id, fan_id, category):
        rung = DEFAULT_RUNG

    d, refused = await plan_pack_delivery(account_id, fan_id, category, rung,
                                          cfg=cfg, company=contract.company,
                                          media_kind=contract.media_kind)
    # He asked for the product rung and it is spent — try the ladder rung, but
    # only if he has already bought from this category.
    if (d is None and refused.get("reason") == REFUSE_TOO_THIN
            and rung == DEFAULT_RUNG
            and await _has_bought_from(account_id, fan_id, category)):
        d, refused = await plan_pack_delivery(
            account_id, fan_id, category, LADDER_RUNG, cfg=cfg,
            company=contract.company, media_kind=contract.media_kind)
    # The shelf cannot serve him, but he still asked: go to the whole vault,
    # same as a subject no category maps. Brakes never fall through.
    if d is None and refused.get("reason") in _SHELF_CANNOT_SERVE:
        log.info("pack shelf cannot serve account=%s fan=%s %s: %s — "
                 "falling through to the vault-wide ask",
                 account_id, fan_id, category, refused.get("reason"))
        return await plan_ask_delivery(account_id, fan_id, contract, cfg=cfg)
    return d, refused


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

    Decide-then-send, in one call, for every caller that has nothing to say
    between the two. `sell_lane` splits them for the closer — see `Delivery`.
    """
    d, refused = await plan_on_ask(account_id, fan_id, cfg=cfg)
    if d is None:
        return refused
    return await deliver(client, d, voice_line=voice_line, dry_run=dry_run)
