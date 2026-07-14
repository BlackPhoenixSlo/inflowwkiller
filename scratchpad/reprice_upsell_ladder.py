"""Double the upsell_ladder prices (hard cap $200), idempotently.

Sets each tagged single to a FIXED target by label (not price*2 in place), so
re-running is safe. Only touches rows tagged ["upsell_ladder"] on the 6 models —
never existing content. price_cents == tip_unlock_cents (kept equal, clean).
"""
import asyncio
import json
import sys
from datetime import datetime

sys.path.insert(0, "/app/service")

from sqlalchemy import select
from db.engine import get_session
from db.models import CatalogItem

CAP = 20000  # $200 OF hard ceiling

# original price → doubled, capped at $200
NEW_PRICE = {
    "Quick rate":         min(400 * 2, CAP),   # 800
    "Selfie set":         min(600 * 2, CAP),   # 1200
    "Lingerie set":       min(1200 * 2, CAP),  # 2400
    "Shower strip":       min(1800 * 2, CAP),  # 3600
    "Full nude set":      min(2500 * 2, CAP),  # 5000
    "Bed tease":          min(3000 * 2, CAP),  # 6000
    "Body rate video":    min(4500 * 2, CAP),  # 9000
    "Dirty talk / JOI":   min(5500 * 2, CAP),  # 11000
    "Toy play":           min(7500 * 2, CAP),  # 15000
    "Long solo finish":   min(10000 * 2, CAP), # 20000
    "Collab scene":       min(15000 * 2, CAP), # 30000 -> 20000 (capped)
    "Custom / exclusive": min(20000 * 2, CAP), # 40000 -> 20000 (capped)
}


async def main():
    now = datetime.utcnow()
    async with get_session() as s:
        rows = (await s.execute(
            select(CatalogItem).where(CatalogItem.tags.like('%upsell_ladder%')))).scalars().all()
        changed = 0
        for it in rows:
            target = NEW_PRICE.get(it.label)
            if target is None:
                continue
            if it.price_cents != target or it.tip_unlock_cents != target:
                it.price_cents = target
                it.tip_unlock_cents = target
                it.updated_at = now
                changed += 1
        print(f"repriced {changed} of {len(rows)} ladder rows")

    # read-back summary
    async with get_session() as s:
        rows = (await s.execute(
            select(CatalogItem.label, CatalogItem.price_cents)
            .where(CatalogItem.tags.like('%upsell_ladder%')))).all()
    seen = {}
    for label, px in rows:
        seen.setdefault(label, px)
    for label in NEW_PRICE:
        print(f"  {label:<20} ${seen.get(label, 0)//100}")
    print("done")


asyncio.run(main())
