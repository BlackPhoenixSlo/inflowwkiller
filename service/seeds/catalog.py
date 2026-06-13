#!/usr/bin/env python3
"""service/seeds/catalog.py — seed a demo content catalog for ai_chatter.

A *catalog* is what the ai_chatter automation reads and sells from: scripts
(ordered "sexting sequences" of described items, sold piece by piece) and
singles (one-off clips / photo sets). Every item carries `description_for_ai` —
what the fan actually SEES — which is the only thing the LLM may tease from.

Seeds Ava (234567890) by default with one demo script (a trimmed port of the
agency "kitchen snack" sequence) + two singles. Scripts seed as status='draft'
(ai_chatter only offers status='enabled'), with EMPTY media_ids — bind real
vault media via --media-from-folder (live OF read) or the M4 Scripts UI.

Run:  ./venv/bin/python service/seeds/catalog.py                    # seed Ava
      ./venv/bin/python service/seeds/catalog.py --account 234567890
      ./venv/bin/python service/seeds/catalog.py --list
      ./venv/bin/python service/seeds/catalog.py --media-from-folder script_kitchen
        # assign that vault folder's media (oldest→newest) to the demo script's
        # items in position order (items keep their existing media if the folder
        # runs short). Requires working creds for --account.

Idempotent: scripts upsert by (account_id, name); the seeded script's items are
refreshed wholesale (delete + reinsert) so re-running restores the demo text.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

_SERVICE = Path(__file__).resolve().parent.parent
if str(_SERVICE) not in sys.path:
    sys.path.insert(0, str(_SERVICE))

from sqlalchemy import delete, select                            # noqa: E402
from sqlalchemy.dialects.sqlite import insert as sqlite_insert   # noqa: E402

from db.engine import get_session                                # noqa: E402
from db.models import CatalogItem, CatalogScript                 # noqa: E402

DEFAULT_ACCOUNT = "234567890"  # Ava — the live-test target (a buyer account)

# ── Demo script: trimmed port of the agency "kitchen snack" sequence ─────────
# description_for_ai is FAN-VISIBLE truth, present tense — the pitch contract.
KITCHEN_SCRIPT: dict = {
    "name": "kitchen_snack",
    "theme": (
        "Set as if you're making a light snack in the kitchen right now, wearing "
        "a tee and underwear. Starts cute and casual, escalates piece by piece, "
        "ends back at the snack with a giggle. The fan should feel it's live."
    ),
    "items": [
        {"position": 0, "kind": "image", "label": "OPENER",
         "description_for_ai": "a cheeky kitchen selfie, tee slipping off one shoulder, mid-snack",
         "duration_sec": None, "price_cents": 0, "tip_unlock_cents": 0,
         "is_free_teaser": True, "tags": ["kitchen", "selfie", "sfw"]},
        {"position": 1, "kind": "video", "label": "TEASE",
         "description_for_ai": "a short playful clip by the counter, pulling the shirt up just a little at the end",
         "duration_sec": 15, "price_cents": 500, "tip_unlock_cents": 400,
         "is_free_teaser": False, "tags": ["kitchen", "tease"]},
        {"position": 2, "kind": "video", "label": "ASS TEASE",
         "description_for_ai": "bathroom mirror video, turning around and lifting the shirt for a proper ass tease, ending with a covered boob tease",
         "duration_sec": 60, "price_cents": 800, "tip_unlock_cents": 700,
         "is_free_teaser": False, "tags": ["mirror", "ass"]},
        {"position": 3, "kind": "video", "label": "BOOB TEASE / NIPPLE PLAY",
         "description_for_ai": "phone propped on the kitchen counter, shirt up, hands covering at first then slow nipple play",
         "duration_sec": 80, "price_cents": 1200, "tip_unlock_cents": 1000,
         "is_free_teaser": False, "tags": ["topless", "nipple_play"]},
        {"position": 4, "kind": "video", "label": "ASS REVEAL",
         "description_for_ai": "underwear comes off — ass shaking and sensual caressing on the counter, walking off cheekily at the end",
         "duration_sec": 130, "price_cents": 1800, "tip_unlock_cents": 1500,
         "is_free_teaser": False, "tags": ["ass", "nude"]},
        {"position": 5, "kind": "video", "label": "IMPLIED MASTURBATION",
         "description_for_ai": "selfie-style on the couch, nipple play sliding into implied masturbation, visibly more worked up",
         "duration_sec": 140, "price_cents": 2500, "tip_unlock_cents": 2000,
         "is_free_teaser": False, "tags": ["implied", "finale"]},
    ],
}

# ── Singles: standalone sellable pieces (script_id NULL) ─────────────────────
SINGLES: list[dict] = [
    {"kind": "video", "label": "BED DANCE",
     "description_for_ai": "a slow teasing dance on the bed in red lingerie, lots of body rolls and close-ups",
     "duration_sec": 95, "price_cents": 1500, "tip_unlock_cents": 1200,
     "is_free_teaser": False, "tags": ["dance", "lingerie", "bed"]},
    {"kind": "image_set", "label": "GOLDEN HOUR SET",
     "description_for_ai": "a polished 8-photo boudoir set by the window at sunset, implied nude, magazine-shoot quality",
     "duration_sec": None, "price_cents": 2000, "tip_unlock_cents": 1800,
     "is_free_teaser": False, "tags": ["photoset", "boudoir", "implied"]},
]


async def seed(account_id: str) -> int:
    now = datetime.utcnow()
    async with get_session() as s:
        await s.execute(
            sqlite_insert(CatalogScript)
            .values(account_id=account_id, name=KITCHEN_SCRIPT["name"],
                    theme=KITCHEN_SCRIPT["theme"], status="draft",
                    created_at=now, updated_at=now)
            .on_conflict_do_update(
                index_elements=["account_id", "name"],
                set_={"theme": KITCHEN_SCRIPT["theme"], "updated_at": now})
        )
    async with get_session() as s:
        script_id = (await s.execute(
            select(CatalogScript.id).where(
                CatalogScript.account_id == account_id,
                CatalogScript.name == KITCHEN_SCRIPT["name"])
        )).scalar_one()

    # Refresh the demo script's items wholesale (seed semantics).
    async with get_session() as s:
        await s.execute(delete(CatalogItem).where(
            CatalogItem.account_id == account_id,
            CatalogItem.script_id == int(script_id)))
        for it in KITCHEN_SCRIPT["items"]:
            s.add(CatalogItem(
                account_id=account_id, script_id=int(script_id),
                position=it["position"], kind=it["kind"], label=it["label"],
                description_for_ai=it["description_for_ai"],
                media_ids="[]", preview_media_ids="[]",
                duration_sec=it["duration_sec"], price_cents=it["price_cents"],
                tip_unlock_cents=it["tip_unlock_cents"],
                is_free_teaser=it["is_free_teaser"],
                tags=json.dumps(it["tags"]), enabled=True,
                created_at=now, updated_at=now))

    # Singles refresh by (account, label) among script-less items.
    async with get_session() as s:
        labels = [x["label"] for x in SINGLES]
        await s.execute(delete(CatalogItem).where(
            CatalogItem.account_id == account_id,
            CatalogItem.script_id.is_(None),
            CatalogItem.label.in_(labels)))
        for it in SINGLES:
            s.add(CatalogItem(
                account_id=account_id, script_id=None, position=None,
                kind=it["kind"], label=it["label"],
                description_for_ai=it["description_for_ai"],
                media_ids="[]", preview_media_ids="[]",
                duration_sec=it["duration_sec"], price_cents=it["price_cents"],
                tip_unlock_cents=it["tip_unlock_cents"],
                is_free_teaser=it["is_free_teaser"],
                tags=json.dumps(it["tags"]), enabled=True,
                created_at=now, updated_at=now))
    return int(script_id)


async def bind_media_from_folder(account_id: str, folder: str) -> int:
    """Assign a vault folder's media (oldest→newest) to the demo script's items
    in position order. Live OF read — requires working creds for the account."""
    import automation_executor as ax  # late import: pulls the OF client stack

    client = await asyncio.to_thread(ax._make_client, account_id)

    def _folder_media() -> list[int]:
        lists = client.vault_lists(view="main", limit=100)
        folders = lists.get("list") if isinstance(lists, dict) else lists
        list_id = None
        for f in (folders or []):
            if isinstance(f, dict) and str(f.get("name", "")).strip().lower() == folder.strip().lower():
                list_id = int(f["id"])
                break
        if list_id is None:
            raise SystemExit(f"[catalog] vault folder not found: {folder!r}")
        media = client.vault_media(list_id=list_id, type="all", limit=100)
        items = media.get("list") if isinstance(media, dict) else media
        ids = [int(it["id"]) for it in (items or [])
               if isinstance(it, dict) and it.get("id") is not None]
        return list(reversed(ids))  # vault is recent-first → oldest-first

    media_ids = await asyncio.to_thread(_folder_media)
    async with get_session() as s:
        script_id = (await s.execute(
            select(CatalogScript.id).where(
                CatalogScript.account_id == account_id,
                CatalogScript.name == KITCHEN_SCRIPT["name"])
        )).scalar_one_or_none()
        if script_id is None:
            raise SystemExit("[catalog] seed first — demo script missing")
        rows = (await s.execute(
            select(CatalogItem).where(
                CatalogItem.account_id == account_id,
                CatalogItem.script_id == int(script_id))
            .order_by(CatalogItem.position))).scalars().all()
        bound = 0
        for row, mid in zip(rows, media_ids):
            row.media_ids = json.dumps([mid])
            row.updated_at = datetime.utcnow()
            bound += 1
    print(f"[catalog] bound {bound}/{len(rows)} items from folder {folder!r} "
          f"({len(media_ids)} media available)")
    return bound


async def _list(account_id: str) -> None:
    async with get_session() as s:
        scripts = (await s.execute(
            select(CatalogScript).where(CatalogScript.account_id == account_id)
            .order_by(CatalogScript.id))).scalars().all()
        items = (await s.execute(
            select(CatalogItem).where(CatalogItem.account_id == account_id)
            .order_by(CatalogItem.script_id, CatalogItem.position))).scalars().all()
    for sc in scripts:
        print(f"script {sc.id} {sc.name!r} [{sc.status}]")
        for it in items:
            if it.script_id == sc.id:
                print(f"   #{it.position} [{it.label}] ${it.price_cents/100:.0f}/"
                      f"tip ${it.tip_unlock_cents/100:.0f} media={it.media_ids} "
                      f"— {(it.description_for_ai or '')[:60]}")
    for it in items:
        if it.script_id is None:
            print(f"single {it.id} [{it.label}] ${it.price_cents/100:.0f} "
                  f"— {(it.description_for_ai or '')[:60]}")


async def _main(a: argparse.Namespace) -> None:
    if a.list:
        await _list(a.account)
        return
    sid = await seed(a.account)
    print(f"[catalog] seeded demo catalog for account {a.account} (script id {sid})")
    if a.media_from_folder:
        await bind_media_from_folder(a.account, a.media_from_folder)
    await _list(a.account)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", default=DEFAULT_ACCOUNT)
    ap.add_argument("--list", action="store_true", help="just list the catalog")
    ap.add_argument("--media-from-folder", default=None,
                    help="bind this vault folder's media to the demo script (live)")
    a = ap.parse_args()
    asyncio.run(_main(a))
