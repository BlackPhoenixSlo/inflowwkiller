"""service/ownership.py — the one home for "what does this fan OWN" semantics.

Phase 2 of plan/PREVENTION_FIX_NO_DUPLICATE_PPV.md. This module owns all four
faces of the concept:

  • the WRITERS — every lane stamps purchases through here;
  • the READERS — `owned_or_seen_media` (fan → owned media) and
    `owners_of_media` (media → owning fans), the two dedup checks every
    seller consults;
  • the media parsing (`item_media` / `item_previews`) both readers and the
    pickers share;
  • the HERO/FILLER split (`hero_media_map`) — the operator's 07-23 ruling on
    which subset of a priced bundle hard-blocks a re-sale.

It imports only db/sqlalchemy (a dependency root), so automations import it
freely — which is what killed the old "local mirror of _item_media to avoid a
circular import" wart in ppv_send.

The writers, which every lane calls:

  • wall-POST purchases (`stamp_post_purchase`) — resolved from OF's purchase
    notification feed (`type=paided_post`, post id in `{POST_URL}`), media ids
    from the local `wall_media` scan with a live `get_post` fallback;
  • chat purchases in ANY lane (`stamp_message_ownership`) — fired wherever
    `Message.is_paid` flips (ledger linker, relink sweep, purchase
    notifications): media from the row's `media_ids` JSON when real, else the
    VaultSend rows the send recorded (teaser/offer lanes), else the mass
    broadcast cache for placeholder rows;
  • direct media-id stamps (`stamp_media_owned`) — the primitive the writers
    above resolve into, and itself a lane entry: the offer-delivery lane
    (`ai_chatter._mark_media_purchased`) calls it directly when the caller
    already holds the real media ids.

Stamps are per-media VaultSend rows with `was_purchased=True`. The table has
NO unique key, so idempotency is SELECT-first: existing (account, fan, media)
rows are UPDATEd (Phase 1's rule), only never-sent media get first-time
INSERTs (a wall-post buy has no send history at all). Re-running any stamp is
a no-op.

Stamping is deliberately ALL media of the bought unit — hero and filler alike.
The fan holds every file he paid for; the hero/filler distinction lives at
PICK time (the sellers block on hero overlap only).
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta
from typing import Any, NamedTuple

from sqlalchemy import select, update

from db.engine import get_session
from db.models import (
    CatalogItem,
    ContentOffer,
    MassBroadcastCache,
    MassRun,
    Message,
    VaultItem,
    VaultSend,
    WallMedia,
)

log = logging.getLogger("of-relay.ownership")

# A delivered offer counts as OWNED only when the money was real — NEVER a
# free teaser (resolved_by='free'), or every paid item reusing that media
# becomes permanently unsellable (product rule: dedup BOUGHT, never
# sent-that-can-be-resent). The one copy of the predicate all readers share.
OWNED_RESOLVED_BY = ("tip", "ppv_ledger", "ppv_txn", "ppv_fastpath", "manual")


def item_media(item: Any) -> list[int]:
    """Media ids bound to a CatalogItem-shaped row (duck-typed on .media_ids)."""
    try:
        return [int(m) for m in json.loads(item.media_ids or "[]")]
    except Exception:
        return []


def item_previews(item: Any) -> list[int]:
    """The item's free-preview ids, filtered to ⊆ its media (OF rejects a
    preview that isn't attached)."""
    try:
        ids = [int(m) for m in json.loads(item.preview_media_ids or "[]")]
    except Exception:
        return []
    media = set(item_media(item))
    return [m for m in ids if m in media]


async def hero_media_map(
    account_id: str, units: dict[int, tuple[list[int], list[int]]]
) -> dict[int, list[int]]:
    """HERO media per unit — the thing the price is FOR (operator ruling
    2026-07-23): every video, plus every non-preview image. Previews are the
    free-visible tease slice (OF attaches them unlocked), so a fan owning ONLY
    a unit's previews must not block the sale — a bundle may repeat tease
    filler. Ownership dedup keys on THIS set, not the full media list.

    `units` maps any key → (media_ids, preview_ids); previews are intersected
    with media here, so pre-intersected and raw caller inputs behave alike.

    Detection: hero = media − previews, re-promoting any preview-listed id the
    vault mirror knows is a video (belt: a video is never mere filler, even
    misconfigured into previews). Unknown-to-the-vault preview ids stay
    filler — previews are free-visible by construction either way. A unit
    with no previews (or previews-only, the degenerate case) keeps its FULL
    media set as hero — exactly the pre-Phase-2 any-media behavior."""
    inter: dict[int, tuple[list[int], set[int]]] = {}
    prev_all: set[int] = set()
    for key, (media, previews) in units.items():
        m = [int(x) for x in media]
        p = {int(x) for x in previews} & set(m)
        inter[key] = (m, p)
        prev_all |= p
    vids: set[int] = set()
    if prev_all:
        async with get_session() as s:
            vids = {int(x) for x in (await s.execute(
                select(VaultItem.media_id).where(
                    VaultItem.account_id == str(account_id),
                    VaultItem.media_id.in_(sorted(prev_all)),
                    VaultItem.kind == "video",
                )
            )).scalars().all()}
    out: dict[int, list[int]] = {}
    for key, (media, prev) in inter.items():
        filler = prev - vids
        hero = [m for m in media if m not in filler]
        out[key] = hero or media
    return out


async def seen_media(account_id: str, fan_id: int) -> set[int]:
    """Media this fan has been SENT — every VaultSend row, bought or not.

    The other half of the pair below: `owned_or_seen_media` answers "may we sell
    him this again?" (purchased only), this one answers "has he laid eyes on it?"
    (anything we delivered). A teaser picker wants freshness, so it reads THIS;
    a resale guard wants proof of purchase, so it reads that one. Confusing them
    is a money bug in both directions, which is why they sit together.

    It lived as two byte-identical private copies — one in ai_chatter, one in
    teaser_select — while the fan-to-media reads they belong beside were already
    public here.
    """
    async with get_session() as s:
        ids = (await s.execute(
            select(VaultSend.media_id).where(
                VaultSend.account_id == str(account_id),
                VaultSend.fan_id == int(fan_id))
        )).scalars().all()
    return {int(x) for x in ids}


async def owned_or_seen_media(account_id: str, fan_id: int) -> set[int]:
    """Media we must NOT re-offer this fan: only what he actually OWNS — a tip/PPV
    unlock (VaultSend.was_purchased) or a paid Message whose media JSON overlaps.
    Deliberately EXCLUDES anything merely SENT: a free teaser AND an offered-but-
    UNBOUGHT PPV (whose VaultSend carries price_cents>0 as the ASKED price, not proof
    of purchase) can both ride again in a future offer. (Product rule: dedup bought,
    never sent-that-can-be-resent.)"""
    out: set[int] = set()
    async with get_session() as s:
        seen_ids = (await s.execute(
            select(VaultSend.media_id).where(
                VaultSend.account_id == str(account_id),
                VaultSend.fan_id == int(fan_id),
                VaultSend.was_purchased.is_(True))
        )).scalars().all()
        paid_rows = (await s.execute(
            select(Message.media_ids).where(
                Message.account_id == str(account_id),
                Message.fan_id == int(fan_id),
                Message.is_paid.is_(True))
        )).all()
        # A NON-FREE delivered content_offer is durable item-level proof of purchase — it survives a
        # missed/crashed was_purchased flip and covers gate-off accounts (which write no ladder_quote).
        delivered_items = (await s.execute(
            select(ContentOffer.item_id).where(
                ContentOffer.account_id == str(account_id),
                ContentOffer.fan_id == int(fan_id),
                ContentOffer.status == "delivered",
                ContentOffer.resolved_by.in_(OWNED_RESOLVED_BY))
        )).scalars().all()
        owned_items = (await s.execute(
            select(CatalogItem).where(
                CatalogItem.id.in_([int(i) for i in delivered_items])))
        ).scalars().all() if delivered_items else []
        s.expunge_all()
    out |= {int(x) for x in seen_ids}
    for (mids_json,) in paid_rows:
        try:
            out |= {int(x) for x in json.loads(mids_json or "[]")}
        except Exception:
            continue
    for it in owned_items:
        out |= set(item_media(it))
    return out


# A priced send must be at least half NEW photography (operator ruling
# 2026-08-08). Held as an exact FRACTION of the non-preview photo set — it
# scales with bundle size instead of hard-coding a count, and integer
# cross-multiplication keeps a money decision away from float rounding (a
# 0.5 literal makes 1-of-2 a coin toss on the boundary). Move the bar by
# changing these two, e.g. 2/3.
FRESH_PHOTO_MIN_NUM = 1
FRESH_PHOTO_MIN_DEN = 2

# The two grounds for blocking. Named constants, not bare literals, because the
# HTTP layer switches on them to pick the operator's message — a renamed string
# would otherwise degrade one block into the other's wording with every module
# still internally consistent and every test still green.
RESALE_OWNED_VIDEO = "owned_video"
RESALE_MOSTLY_OWNED_PHOTOS = "mostly_owned_photos"


class ResaleVerdict(NamedTuple):
    """Why a priced send is — or is not — a re-sale of content the fan holds.

    A NamedTuple rather than a bare set because there are two distinct grounds
    for blocking and the caller has to tell the operator WHICH one it hit; a
    set of ids can't carry that, and the counts are what make the photo message
    actionable ("3 of 4 already bought")."""

    reason: str                 # "" = allow, else a RESALE_* constant
    owned_videos: list[int]     # already-paid videos anywhere in the send
    owned_photos: list[int]     # already-paid NON-PREVIEW photos
    photos_total: int           # non-preview photos the vault mirror could classify

    @property
    def blocked(self) -> bool:
        return bool(self.reason)

    @property
    def owned_media(self) -> list[int]:
        """The ids this verdict is ABOUT — scoped to the reason, so the payload
        never lists photos under a message that only mentions video."""
        return (self.owned_videos if self.reason == RESALE_OWNED_VIDEO
                else self.owned_photos)


async def resale_verdict(
    account_id: str,
    fan_id: int,
    media_ids: list[int],
    previews: list[int] | None = None,
) -> ResaleVerdict:
    """Judge a PRICED send against what this fan has already paid for.

    The re-sale guard for the human/extension send path
    (`POST /api/of/v2/chats/{id}/messages`) which — unlike ppv_send and
    ai_chatter, both of which check ownership — had no check at all, and
    re-sold one whale the same two clips four times at a climbing price
    ($40 → $80 → $100 → $120, ledger-confirmed).

    Two operator rulings, deliberately different in shape:

    • VIDEO is absolute — one already-bought video blocks the send. Checked
      across the WHOLE media list including previews, matching
      `hero_media_map`'s belt: a video is never mere filler, even if someone
      misconfigured it into the preview pool.
    • PHOTOS are proportional — the non-preview photo set must be at least
      `FRESH_PHOTO_MIN_NUM/DEN` new. Re-using some stills to frame a fresh set
      is normal selling; charging again for a set that is mostly his already is
      the abuse. Previews are excluded because they are the free-visible tease
      slice, not the thing being sold.

    ⚠️ A paid video listed in `previews` STILL blocks. An OF preview rides
    unlocked, so strictly he is not being re-charged for it — this is deliberate
    extra strictness inherited from `hero_media_map`'s belt, and the one place
    this guard is stricter than "is he paying twice". If that ever proves wrong,
    read the video line off `payload` instead of `target`; nothing else moves.

    Media the vault mirror can't classify counts toward NEITHER rule and is
    logged: it can't be called a video, and it can't sit in the photo
    denominator without silently moving the ratio. Blocking a live send on a
    guess is worse than the miss."""
    target = {int(x) for x in media_ids if x}
    if not target or not fan_id:
        return ResaleVerdict("", [], [], 0)
    owned_all = await owned_or_seen_media(account_id, int(fan_id))
    prev = {int(x) for x in (previews or []) if x} & target
    payload = target - prev            # what the price is actually FOR

    async with get_session() as s:
        rows = (await s.execute(
            select(VaultItem.media_id, VaultItem.kind).where(
                VaultItem.account_id == str(account_id),
                VaultItem.media_id.in_(sorted(target)))
        )).all()
    kinds = {int(m): (k or "") for m, k in rows}
    unknown = sorted(target - set(kinds))
    if unknown:
        log.warning("resale_verdict: %d media unknown to the vault mirror "
                    "account=%s fan=%s ids=%s — counted toward no rule",
                    len(unknown), account_id, fan_id, unknown[:10])

    # Videos: the whole list, previews included (a video is never filler).
    owned_videos = sorted(m for m in target
                          if kinds.get(m) == "video" and m in owned_all)
    # Photos: the payload only — previews are the free tease slice.
    photos = {m for m in payload if kinds.get(m) == "photo"}
    owned_photos = sorted(photos & owned_all)
    fresh = len(photos) - len(owned_photos)

    # Only the REASON varies — decide it, then build the verdict once, so the
    # precedence (a paid video outranks the photo ratio) is stated rather than
    # implied by return order.
    # The photo test is cross-multiplied `fresh / total < NUM / DEN`; exactly
    # half is ALLOWED ("at LEAST 50% unbought"), so the comparison is strict.
    stale_photos = (bool(photos) and fresh * FRESH_PHOTO_MIN_DEN
                    < len(photos) * FRESH_PHOTO_MIN_NUM)
    reason = (RESALE_OWNED_VIDEO if owned_videos
              else RESALE_MOSTLY_OWNED_PHOTOS if stale_photos
              else "")
    return ResaleVerdict(reason, owned_videos, owned_photos, len(photos))


async def owners_of_media(account_id: str, media_ids: list[int]) -> set[int]:
    """Fan ids who ALREADY own any of this media — so we never re-pitch/re-sell content a fan holds.
    THREE signals:
      1. a `VaultSend.was_purchased is True` row (a tip/PPV unlock in ANY lane — the reliable,
         lane-independent signal; this is what makes an ai_chatter LADDER buy visible to the mass
         lane and to the discount-resend guard);
      2. a NON-FREE delivered `content_offer` whose item media overlaps (durable item-level proof;
         `resolved_by='free'` teasers are EXCLUDED — a teaser is resendable, not owned);
      3. a paid `Message` whose per-message `media_ids` JSON overlaps (kept for inbound/real-id rows;
         inert for pipeline PPVs, which store `media_ids='[]'`)."""
    target = {int(x) for x in media_ids}
    if not target:
        return set()
    owners: set[int] = set()
    async with get_session() as s:
        rows = (await s.execute(
            select(Message.fan_id, Message.media_ids).where(
                Message.account_id == str(account_id),
                Message.is_paid.is_(True),
            )
        )).all()
        vs = (await s.execute(
            select(VaultSend.fan_id).where(
                VaultSend.account_id == str(account_id),
                VaultSend.was_purchased.is_(True),
                VaultSend.media_id.in_(list(target)),
            )
        )).scalars().all()
        owners |= {int(x) for x in vs if x is not None}
        co = (await s.execute(
            select(ContentOffer.fan_id, ContentOffer.item_id).where(
                ContentOffer.account_id == str(account_id),
                ContentOffer.status == "delivered",
                ContentOffer.resolved_by.in_(OWNED_RESOLVED_BY),
            )
        )).all()
        co_media: dict[int, set[int]] = {}
        if co:
            for ci in (await s.execute(
                select(CatalogItem).where(CatalogItem.id.in_(
                    [int(i) for _, i in co if i is not None])))
            ).scalars().all():
                co_media[int(ci.id)] = set(item_media(ci))
    for fid, mids_json in rows:
        if fid is None:
            continue
        try:
            mids = {int(x) for x in json.loads(mids_json or "[]")}
        except Exception:
            continue
        if mids & target:
            owners.add(int(fid))
    for fid, iid in co:
        if fid is not None and iid is not None and co_media.get(int(iid), set()) & target:
            owners.add(int(fid))
    return owners


async def stamp_media_owned(
    account_id: str,
    fan_id: int,
    media_ids: list[int],
    *,
    price_cents: int = 0,
    message_id: int | None = None,
    source: str = "",
) -> int:
    """Flip/insert `was_purchased=True` VaultSend rows for every media id.
    Returns how many media ids were newly stamped (already-True ids count 0).
    """
    ids = sorted({int(m) for m in media_ids if m})
    if not ids or not fan_id:
        return 0
    stamped = 0
    async with get_session() as s:
        rows = (await s.execute(
            select(VaultSend.media_id, VaultSend.was_purchased).where(
                VaultSend.account_id == str(account_id),
                VaultSend.fan_id == int(fan_id),
                VaultSend.media_id.in_(ids),
            )
        )).all()
        existing = {int(m) for m, _ in rows}
        already_true = {int(m) for m, p in rows if p is True}
        to_flip = sorted((existing - already_true))
        if to_flip:
            await s.execute(
                update(VaultSend)
                .where(VaultSend.account_id == str(account_id),
                       VaultSend.fan_id == int(fan_id),
                       VaultSend.media_id.in_(to_flip))
                .values(was_purchased=True)
            )
        for m in ids:
            if m in existing:
                continue
            s.add(VaultSend(
                account_id=str(account_id),
                fan_id=int(fan_id),
                media_id=int(m),
                message_id=int(message_id) if message_id else None,
                price_cents=int(price_cents or 0),
                was_purchased=True,
            ))
        stamped = len(to_flip) + len([m for m in ids if m not in existing])
        await s.commit()
    if stamped:
        log.info(
            "ownership_stamp account=%s fan=%s media=%d newly_stamped=%d source=%s",
            account_id, fan_id, len(ids), stamped, source or "?")
    return stamped


# post → media resolution cache. Positive entries save an OF GET per repeat
# sighting of the same purchase notification; negative entries rate-limit the
# retry for posts OF won't serve (deleted/archived). Process-local — a relay
# restart just refetches once.
_POST_MEDIA_TTL_OK = timedelta(hours=24)
_POST_MEDIA_TTL_MISS = timedelta(hours=1)
_POST_MEDIA_CACHE_MAX = 512
_post_media_cache: dict[tuple[str, int], tuple[list[int] | None, datetime]] = {}


async def media_for_post(
    account_id: str, post_id: int, client: Any = None
) -> list[int]:
    """Media ids of a wall post. LIVE single-post GET first — it is the only
    COMPLETE source: the `wall_media` scan keys rows by media id and keeps the
    FIRST-seen post_id (server.py upsert), so a paid post that re-uses media
    from an earlier post has an incomplete row-set there, and stamping the
    subset would silently leave the re-used media re-sellable. The scan is the
    fallback when the live fetch fails/empties (logged as possibly-partial).
    [] when neither source knows the post — callers must treat that as
    "cannot stamp, log and move on"."""
    key = (str(account_id), int(post_id))
    cached = _post_media_cache.get(key)
    if cached is not None:
        ids, at = cached
        ttl = _POST_MEDIA_TTL_OK if ids else _POST_MEDIA_TTL_MISS
        if datetime.utcnow() - at < ttl:
            return list(ids or [])
    live: list[int] = []
    if client is not None:
        try:
            detail = await asyncio.to_thread(client.get_post, int(post_id))
            media = detail.get("media") if isinstance(detail, dict) else None
            for m in media or []:
                try:
                    live.append(int(m["id"] if isinstance(m, dict) else m))
                except (KeyError, TypeError, ValueError):
                    continue
        except Exception:
            log.debug("ownership_post_fetch_failed account=%s post=%s",
                      account_id, post_id, exc_info=True)
    if live:
        _cache_post_media(key, live)
        return live
    async with get_session() as s:
        wall = (await s.execute(
            select(WallMedia.media_id).where(
                WallMedia.account_id == str(account_id),
                WallMedia.post_id == int(post_id),
            )
        )).scalars().all()
    if wall:
        log.info(
            "ownership_post_media_from_wall account=%s post=%s n=%d "
            "(live fetch unavailable — may be partial for media re-used "
            "across posts)", account_id, post_id, len(wall))
        out = [int(x) for x in wall]
        _cache_post_media(key, out)
        return out
    _cache_post_media(key, None)
    return []


def _cache_post_media(key: tuple[str, int], ids: list[int] | None) -> None:
    while len(_post_media_cache) >= _POST_MEDIA_CACHE_MAX:
        _post_media_cache.pop(next(iter(_post_media_cache)))
    _post_media_cache[key] = (ids, datetime.utcnow())


async def stamp_post_purchase(
    account_id: str,
    fan_id: int,
    post_id: int,
    *,
    price_cents: int = 0,
    client: Any = None,
) -> dict[str, Any]:
    """A fan bought a wall post → own every media id in it. Unresolvable post
    (never wall-scanned AND the live fetch failed/empty) stamps nothing and
    logs — being honest beats guessing the wrong media.

    `resolved=False` → the caller should retry on a later sighting (the wall
    scan / live fetch may land); `resolved=True` → fully handled (stamped may
    still be 0 when an earlier sighting already stamped everything)."""
    media = await media_for_post(account_id, post_id, client)
    if not media:
        log.info(
            "ownership_post_unresolved account=%s fan=%s post=%s "
            "(no wall_media rows, no live media) — nothing stamped",
            account_id, fan_id, post_id)
        return {"resolved": False, "stamped": 0}
    stamped = await stamp_media_owned(
        account_id, fan_id, media,
        price_cents=price_cents, source=f"post:{post_id}")
    return {"resolved": True, "stamped": stamped}


async def stamp_message_ownership(
    account_id: str, fan_id: int, message_id: int, *, price_cents: int = 0
) -> int:
    """A priced chat message got PAID → own its media, whatever lane sent it.

    Three cumulative legs (a row can satisfy several; the union is stamped):
      1. the Message row's own `media_ids` JSON (real ids from WS/scrape rows);
      2. the VaultSend rows the send recorded under this message id (offer /
         teaser / make_right lanes write these with real ids) — flipped True;
      3. `mass_run_id` rows (echoed reals AND 5e15 placeholders alike):
         MassRun.queue_id → MassBroadcastCache.media_json = the blast's media.
    """
    stamped = 0
    async with get_session() as s:
        msg = (await s.execute(
            select(Message).where(
                Message.account_id == str(account_id),
                Message.fan_id == int(fan_id),
                Message.message_id == int(message_id),
            ).limit(1)
        )).scalar_one_or_none()
    row_media: list[int] = []
    mass_run_id = None
    if msg is not None:
        mass_run_id = msg.mass_run_id
        try:
            row_media = [int(x) for x in json.loads(msg.media_ids or "[]")]
        except Exception:
            row_media = []
        if not price_cents:
            price_cents = int(msg.price_cents or 0)
    if row_media:
        stamped += await stamp_media_owned(
            account_id, fan_id, row_media,
            price_cents=price_cents, message_id=message_id,
            source=f"msg:{message_id}")

    # Leg 2: the send's own VaultSend rows (real media ids, keyed by message).
    async with get_session() as s:
        res = await s.execute(
            update(VaultSend)
            .where(VaultSend.account_id == str(account_id),
                   VaultSend.fan_id == int(fan_id),
                   VaultSend.message_id == int(message_id),
                   VaultSend.was_purchased.is_not(True))
            .values(was_purchased=True)
        )
        await s.commit()
    if res.rowcount:
        stamped += int(res.rowcount)
        log.info(
            "ownership_stamp account=%s fan=%s via=vault_send msg=%s flipped=%d",
            account_id, fan_id, message_id, res.rowcount)

    # Leg 3: mass rows — the blast's media set lives in the broadcast cache.
    if mass_run_id is not None:
        mass_media = await _mass_run_media(account_id, int(mass_run_id))
        if mass_media:
            stamped += await stamp_media_owned(
                account_id, fan_id, mass_media,
                price_cents=price_cents, message_id=message_id,
                source=f"mass_run:{mass_run_id}")
        else:
            log.info(
                "ownership_mass_unresolved account=%s fan=%s run=%s msg=%s "
                "(no queue_id / no broadcast-cache media) — mass media not stamped",
                account_id, fan_id, mass_run_id, message_id)
    return stamped


async def try_stamp_message(
    account_id: str, fan_id: int, message_id: int, *,
    price_cents: int = 0, context: str = "",
) -> int:
    """Best-effort `stamp_message_ownership`: a stamp must never break the
    tick/poll/sweep it rides on. One uniform failure log; 0 on error."""
    try:
        return await stamp_message_ownership(
            account_id, fan_id, message_id, price_cents=price_cents)
    except Exception:  # noqa: BLE001
        log.warning("ownership_stamp_failed context=%s account=%s fan=%s id=%s",
                    context, account_id, fan_id, message_id, exc_info=True)
        return 0


async def try_stamp_post(
    account_id: str, fan_id: int, post_id: int, *,
    price_cents: int = 0, client: Any = None, context: str = "",
) -> dict[str, Any]:
    """Best-effort `stamp_post_purchase` — same never-raises contract; the
    error shape (`resolved=False`) leaves the notification retryable."""
    try:
        return await stamp_post_purchase(
            account_id, fan_id, post_id,
            price_cents=price_cents, client=client)
    except Exception:  # noqa: BLE001
        log.warning("ownership_stamp_failed context=%s account=%s fan=%s id=%s",
                    context, account_id, fan_id, post_id, exc_info=True)
        return {"resolved": False, "stamped": 0}


async def _mass_run_media(account_id: str, mass_run_id: int) -> list[int]:
    """MassRun → OF queue row in the broadcast cache → media ids. [] when the
    run never got its queue_id or the cache hasn't seen the row."""
    async with get_session() as s:
        queue_id = (await s.execute(
            select(MassRun.queue_id).where(MassRun.id == int(mass_run_id))
        )).scalar_one_or_none()
        if not queue_id:
            return []
        blob = (await s.execute(
            select(MassBroadcastCache.media_json).where(
                MassBroadcastCache.account_id == str(account_id),
                MassBroadcastCache.queue_id == int(queue_id),
            )
        )).scalar_one_or_none()
    out: list[int] = []
    try:
        for m in json.loads(blob or "[]"):
            out.append(int(m["id"] if isinstance(m, dict) else m))
    except (TypeError, ValueError, KeyError):
        return []
    return out
