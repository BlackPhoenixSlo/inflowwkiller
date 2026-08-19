#!/usr/bin/env python3
"""service/seeds/catalog.py — seed a demo content catalog for ai_chatter.

A *catalog* is what the ai_chatter automation reads and sells from: SINGLES —
one-off clips / photo sets (`script_id IS NULL`). Ordered-script ladders were
removed 2026-08-19 (singles sold 18, scripts 0/21); singles are the whole
catalog. Every item carries `description_for_ai` — what the fan actually SEES —
which is the only thing the LLM may tease from.

Seeds the named account with two demo singles, EMPTY media_ids — bind real
vault media via the singles editor ("Fill content").

Run:  ./venv/bin/python service/seeds/catalog.py --account <account_id>
      ./venv/bin/python service/seeds/catalog.py --account <account_id> --list

The account id is deliberately NOT baked in (SEED_ACCOUNT env works too): this
file syncs to the public deploy repo, and a creator id used as a VALUE in synced
Python trips deploy-fastt.sh's guard — a scrubbed id would ship code that
matches no account.

Idempotent: the seeded singles refresh by (account, label); re-running restores
the demo text.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path

_SERVICE = Path(__file__).resolve().parent.parent
if str(_SERVICE) not in sys.path:
    sys.path.insert(0, str(_SERVICE))

from sqlalchemy import delete, select                            # noqa: E402

from db.engine import get_session                                # noqa: E402
from db.models import CATALOG_IS_SINGLE, CatalogItem             # noqa: E402

# ── Singles: standalone sellable pieces (script_id NULL) ─────────────────────
# description_for_ai is FAN-VISIBLE truth, present tense — the pitch contract.
SINGLES: list[dict] = [
    {"kind": "video", "label": "BED DANCE",
     "description_for_ai": "a slow teasing dance on the bed in red lingerie, lots of body rolls and close-ups",
     "duration_sec": 95, "price_cents": 1500,
     "is_free_teaser": False, "tags": ["dance", "lingerie", "bed"]},
    {"kind": "image_set", "label": "GOLDEN HOUR SET",
     "description_for_ai": "a polished 8-photo boudoir set by the window at sunset, implied nude, magazine-shoot quality",
     "duration_sec": None, "price_cents": 2000,
     "is_free_teaser": False, "tags": ["photoset", "boudoir", "implied"]},
]


async def seed(account_id: str) -> None:
    now = datetime.utcnow()
    # Singles refresh by (account, label) among script-less items.
    async with get_session() as s:
        labels = [x["label"] for x in SINGLES]
        await s.execute(delete(CatalogItem).where(
            CatalogItem.account_id == account_id,
            CATALOG_IS_SINGLE,
            CatalogItem.label.in_(labels)))
        for it in SINGLES:
            s.add(CatalogItem(
                account_id=account_id,
                kind=it["kind"], label=it["label"],
                description_for_ai=it["description_for_ai"],
                media_ids="[]", preview_media_ids="[]",
                duration_sec=it["duration_sec"], price_cents=it["price_cents"],
                is_free_teaser=it["is_free_teaser"],
                tags=json.dumps(it["tags"]), enabled=True,
                created_at=now, updated_at=now))


async def _list(account_id: str) -> None:
    async with get_session() as s:
        items = (await s.execute(
            select(CatalogItem).where(CatalogItem.account_id == account_id,
                                      CATALOG_IS_SINGLE)
            .order_by(CatalogItem.id))).scalars().all()
    for it in items:
        print(f"single {it.id} [{it.label}] ${it.price_cents/100:.0f} "
              f"media={it.media_ids} — {(it.description_for_ai or '')[:60]}")


async def _main(a: argparse.Namespace) -> None:
    if a.list:
        await _list(a.account)
        return
    await seed(a.account)
    print(f"[catalog] seeded {len(SINGLES)} demo singles for account {a.account}")
    await _list(a.account)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", default=os.environ.get("SEED_ACCOUNT") or None,
                    help="account id to seed (or set SEED_ACCOUNT)")
    ap.add_argument("--list", action="store_true", help="just list the singles")
    a = ap.parse_args()
    if not a.account:
        ap.error("--account is required (or set SEED_ACCOUNT)")
    asyncio.run(_main(a))
