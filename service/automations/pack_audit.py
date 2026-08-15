"""service/automations/pack_audit.py — can this caption lie?

The predicates that stand between a priced send and a broken promise. Nothing
here decides what to sell, prices it, or writes a word the fan reads; each
function takes an account and some media ids and returns the VIOLATIONS, empty
for a pass.

## Why it is its own file

On 2026-07-31 two fans made their first-ever purchase and deleted their entire
OnlyFans accounts within hours. One asked three times *"only feet right?"*, paid
$3.25, received a bra/face selfie with no feet, and wrote *"Goodbye, you stupid
liar."* Every rule in this module is that message turned into a predicate, and
they are the last thing that runs before the wire — twice, because folder
membership is mutable and a pack that passed at plan time can be a lie by send
time.

That makes them the part of the pack path most worth reading on their own. They
were the third region of `pack_sender` (after `pack_pricing` — what is it worth,
and `pack_claim` — what does the caption promise), and they are pure: no config,
no flags, no client, no writes.

The shelf read lives here too, because two of the five rules ARE shelf-membership
questions and the read is what makes them answerable.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import func, select

import vault_pack_picker
from db.engine import get_session
from db.models import VaultCacheRun, VaultFolder, VaultFolderItem, VaultItem

from . import content_resolver
from .pack_claim import Claim, is_substitute_claim
from .pack_pricing import MIN_ITEMS

log = logging.getLogger("of-relay.automation.pack_audit")

# ⚠️ Mirror age is a WARNING, not a refusal — corrected 2026-08-11.
#
# The first cut refused any send when the vault mirror was stale, on the theory
# that rung membership had become OF-backed. That was wrong: `shelf_media`
# reads `VaultFolderItem`, the INTERNAL membership the picker wrote. It is
# operator-authored, exact, and does not decay. The mirror is only our cache of
# OF's own listing, and media ids are stable — OF resolves them at send time
# whatever our cache says.
#
# What DOES matter is narrower and is checked directly: every id about to be
# charged for must still be a live `VaultItem` (not soft-deleted). A genuinely
# dead id fails loudly at the wire, which is the right place for it.
MIRROR_WARN_AGE = timedelta(days=7)


async def shelf_media(account_id: str, category: str, rung: str) -> list[int]:
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


async def _mirror_age(account_id: str) -> timedelta | None:
    """How stale the vault mirror is. None when it has never been collected."""
    async with get_session() as s:
        last = (await s.execute(
            select(func.max(VaultCacheRun.finished_at)).where(
                VaultCacheRun.account_id == str(account_id),
                VaultCacheRun.status == "done")
        )).scalar_one_or_none()
    return None if last is None else (datetime.utcnow() - last)


async def _live_media(account_id: str, media: list[int] | set[int]) -> set[int]:
    """Of these ids, the ones that are still un-deleted `VaultItem`s.

    The one query behind both modules' liveness rule: an id soft-deleted after
    two clean collect sweeps is gone from her vault, and charging for it delivers
    nothing.
    """
    ids = sorted({int(m) for m in media})
    if not ids:
        return set()
    async with get_session() as s:
        return {int(m) for m in (await s.execute(
            select(VaultItem.media_id).where(
                VaultItem.account_id == str(account_id),
                VaultItem.media_id.in_(ids),
                VaultItem.removed_at.is_(None))
        )).scalars().all()}


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

    R = set(await shelf_media(account_id, category, rung))
    paid = setM - setP
    if not paid <= R:
        bad.append(f"rule2 paid items outside {category}-{rung}: {sorted(paid - R)}")

    T = set(await shelf_media(account_id, category, "tease"))
    if T and not setP <= T:
        bad.append(f"rule3 previews outside the tease rung: {sorted(setP - T)}")

    if len(paid) < MIN_ITEMS:
        bad.append(f"rule4 |M−P| = {len(paid)} < {MIN_ITEMS}")

    # Rule 5 — every paid id must still EXIST.
    if paid:
        missing = sorted(paid - await _live_media(account_id, paid))
        if missing:
            bad.append(f"rule5 media no longer in the vault: {missing}")
    return bad


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

    live = await _live_media(account_id, media)
    dead = [m for m in media if m not in live]
    if dead:
        bad.append(f"{len(dead)} dead media: {dead[:4]}")

    if not company:
        solo = set(await content_resolver.solo_only(account_id, media))
        others = [m for m in media if m not in solo]
        if others:
            bad.append(f"{len(others)} with someone else in them: {others[:4]}")
    return bad


async def mirror_warning(account_id: str) -> str:
    """A note for the log/operator when the cache is old — never a refusal."""
    age = await _mirror_age(account_id)
    if age is None:
        return "vault never collected"
    if age > MIRROR_WARN_AGE:
        return f"vault mirror {age.days}d old — Collect to refresh thumbnails"
    return ""
