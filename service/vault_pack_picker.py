"""The pack picker — triage a category's vault candidates into rungs.

One button, one modal, one save. The operator sees every item in the vault that
might belong to a category, files each on a rung (or rejects it), and saves. The
rungs become internal `VaultFolder`s; publishing them to her real OnlyFans vault
is a separate, explicit step (`vault_ai_api._push_folder_to_of`).

WHY A HUMAN IS IN THIS LOOP. The vault's own taxonomy cannot find this content.
Measured on prod 2026-08-10: `primary_folder='feet'` is set on 265 items
roster-wide but on ZERO items of either pilot account, and on ZERO of the 68
items the operator hand-picked on 08-01. `body_focus` DOES list "feet" — on 278
items on ACCOUNT_ID — but it lists feet whenever they are merely VISIBLE, not when
they are the subject, so a lingerie pose with her feet in frame matches. Hence a
4.5x over-inclusive generator (307 candidates -> 68 keepers, 22%), and hence this
module: selling straight off the tag would charge a fan for three photos he did
not ask for out of every four.

So the query proposes and the operator disposes. Everything here is the
disposing; nothing here talks to OnlyFans.

Routes live in `vault_ai_api` (house convention — that module owns every
`/admin/vault-ai/*` route and the `vault_*` siblings own the logic).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete, func, or_, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

import vault_ai_brief
from db.engine import get_session
from db.models import VaultFolder, VaultFolderItem, VaultItem

# Every folder this module makes is the OPERATOR's, not the pipeline's. This one
# string is what keeps `vault_scripts`' reaper off them: it retires only rows
# that are BOTH created_by="vault_ai_script" AND `AI-`-prefixed. Never backfill it.
CREATED_BY = "operator"

# A rung of this value (or null, or empty) means "not this category" — it clears
# the item from every rung. Nothing is stored about a rejection: there is no
# reject list to maintain and nothing repopulates one.
REJECT_VALUES = frozenset({"", "reject", "none"})


@dataclass(frozen=True)
class Category:
    """One sellable subject and its ladder.

    Adding a category is one entry in `CATEGORIES` plus an authored rung phrase
    per language — no new code. That is the whole of stage 3, so the shape is
    explicit rather than a bag of strings.

    `term` is LIKE-scanned against `search_text` (the denormalised
    tags + description + notes blob) AND against `ai_fields_json`. The union is
    the generator the 2026-08-01 triage was drawn from, and it is deliberately
    over-inclusive: rejecting is cheap, missing a keeper is not.

    `rungs` is the ladder, cheapest first. The folder name is f"{name}-{rung}",
    deliberately WITHOUT an `AI-` prefix so the reaper cannot reach these folders
    even if `created_by` were ever backfilled.
    """
    name: str
    term: str
    rungs: tuple[str, ...]

    def folder_name(self, rung: str) -> str:
        return f"{self.name}-{rung}"

    @property
    def folder_names(self) -> list[str]:
        return [self.folder_name(r) for r in self.rungs]


CATEGORIES: dict[str, Category] = {
    c.name: c for c in (
        Category(name="feet", term="feet", rungs=("tease", "nude", "nude-body")),
    )
}


class UnknownCategory(KeyError):
    """Raised instead of KeyError so the route layer can map it to a 400 without
    catching every KeyError the ORM might throw."""


class UnknownRung(ValueError):
    pass


def category(name: str | None) -> Category:
    try:
        return CATEGORIES[(name or "").strip().lower()]
    except KeyError:
        raise UnknownCategory(name) from None


def normalise_verdicts(cat: Category, rows: list[dict[str, Any]]) -> dict[int, str | None]:
    """`[{media_id, rung}]` -> `{media_id: rung|None}`, rejecting unknown rungs.

    Filing is validated here rather than at the DB write so a typo'd rung is a
    refusal, never a silently-dropped verdict — a verdict the operator believes
    they made is the one thing this module must not lose.
    """
    out: dict[int, str | None] = {}
    for row in rows or []:
        try:
            media_id = int(row["media_id"])
        except (KeyError, TypeError, ValueError):
            continue
        rung = str(row.get("rung") or "").strip().lower()
        if rung in REJECT_VALUES:
            out[media_id] = None
        elif rung in cat.rungs:
            out[media_id] = rung
        else:
            raise UnknownRung(rung)
    return out


async def _folders_by_name(s, account_id: str, cat: Category) -> dict[str, VaultFolder]:
    return {
        f.name: f for f in (await s.execute(
            select(VaultFolder).where(
                VaultFolder.account_id == account_id,
                VaultFolder.name.in_(cat.folder_names),
                VaultFolder.deleted_at.is_(None),
            )
        )).scalars().all()
    }


async def _shelf(s, account_id: str, cat: Category,
                 folders: dict[str, VaultFolder]) -> list[dict[str, Any]]:
    """The rungs and how full each is, cheapest first.

    One shape, returned by BOTH `candidates` and `triage`, so the client renders
    the shelf from the same source whether it just saved or just opened. A rung
    with no folder yet reports `folder_id: None` and count 0 rather than being
    absent — the ladder's shape should not depend on how far triage has got.
    """
    ids = [f.id for f in folders.values()]
    counts = dict((await s.execute(
        select(VaultFolderItem.folder_id, func.count()).where(
            VaultFolderItem.account_id == account_id,
            VaultFolderItem.folder_id.in_(ids),
        ).group_by(VaultFolderItem.folder_id)
    )).all()) if ids else {}
    out = []
    for rung in cat.rungs:
        folder = folders.get(cat.folder_name(rung))
        out.append({
            "rung": rung,
            "name": cat.folder_name(rung),
            "folder_id": folder.id if folder else None,
            "count": int(counts.get(folder.id, 0)) if folder else 0,
            "of_list_id": folder.of_list_id if folder else None,
        })
    return out


async def candidates(account_id: str, cat: Category, limit: int = 600) -> dict[str, Any]:
    """Every candidate for a category, each carrying the rung it is filed on now,
    plus the shelf those rungs currently make.

    The description rides along because roughly half the set is rejectable on
    text alone — "walks away from the camera on a paved street" is not a feet
    picture whatever the tag says — and that pass costs no pixels.

    🚨 The rung of an item is FOLDER MEMBERSHIP and nothing else. Never
    `ai_fields_json` (a shared blob — a read-modify-write clobbers whatever other
    writer touched it last) and never a column on `VaultItem` (membership is not
    single-valued: an item can be a feet keeper and a heels keeper at once).
    """
    async with get_session() as s:
        folders = await _folders_by_name(s, account_id, cat)
        shelf = await _shelf(s, account_id, cat, folders)

        by_id = {f.id: name for name, f in folders.items()}
        filed = {
            int(media_id): by_id[folder_id].removeprefix(f"{cat.name}-")
            for media_id, folder_id in (
                (await s.execute(
                    select(VaultFolderItem.media_id, VaultFolderItem.folder_id).where(
                        VaultFolderItem.account_id == account_id,
                        VaultFolderItem.folder_id.in_(list(by_id)),
                    )
                )).all() if by_id else []
            )
            if folder_id in by_id
        }

        items = (await s.execute(
            select(VaultItem.media_id, VaultItem.kind, VaultItem.ai_fields_json)
            .where(
                VaultItem.account_id == account_id,
                VaultItem.removed_at.is_(None),
                or_(
                    func.coalesce(VaultItem.search_text, "").like(f"%{cat.term}%"),
                    func.coalesce(VaultItem.ai_fields_json, "").like(f'%"{cat.term}"%'),
                ),
            )
            .order_by(VaultItem.media_id)
            .limit(max(1, min(int(limit), 2000)))
        )).all()

    out = [
        {
            "media_id": int(media_id),
            "kind": kind,
            "rung": filed.get(int(media_id)),
            "description": (
                (vault_ai_brief.load_fields(fields) or {}).get("description") or ""
            )[:400],
        }
        for media_id, kind, fields in items
    ]
    return {
        "category": cat.name,
        "rungs": list(cat.rungs),
        "folders": shelf,
        "candidates": out,
        "filed": sum(1 for r in out if r["rung"]),
    }


async def triage(
    account_id: str, cat: Category, verdicts: dict[int, str | None],
) -> list[dict[str, Any]]:
    """Save the operator's verdicts. Creates the rung folders on first save.

    All rungs are created on the first save even if empty, so the shelf's shape
    is visible from the start rather than appearing a rung at a time.

    A verdict is EXCLUSIVE within a category (filing on `nude` takes it off
    `tease`) and independent across categories. Both fall out of one pass:
    clearing this category's membership for the ruled media before re-adding is
    what makes filing exclusive AND makes a re-save a no-op.
    """
    async with get_session() as s:
        folders = await _folders_by_name(s, account_id, cat)
        for rung in cat.rungs:
            if cat.folder_name(rung) not in folders:
                folder = VaultFolder(
                    account_id=account_id, name=cat.folder_name(rung),
                    created_by=CREATED_BY,
                )
                s.add(folder)
                await s.flush()
                folders[folder.name] = folder

        folder_ids = [f.id for f in folders.values()]
        await s.execute(
            delete(VaultFolderItem).where(
                VaultFolderItem.account_id == account_id,
                VaultFolderItem.folder_id.in_(folder_ids),
                VaultFolderItem.media_id.in_(list(verdicts)),
            )
        )
        for media_id, rung in verdicts.items():
            if rung is None:
                continue
            await s.execute(
                sqlite_insert(VaultFolderItem)
                .values(account_id=account_id,
                        folder_id=folders[cat.folder_name(rung)].id,
                        media_id=media_id)
                .on_conflict_do_nothing(
                    index_elements=["account_id", "folder_id", "media_id"])
            )
        await s.commit()
        return await _shelf(s, account_id, cat, folders)
