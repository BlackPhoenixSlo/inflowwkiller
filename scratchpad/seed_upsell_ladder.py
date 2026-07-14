"""Seed the 12-piece solo-single upsell ladder onto the 6 live models.

Runs INSIDE the fastt-relay container (piped via `docker exec -i ... python -`),
so it uses the relay's own ORM + the live prod DB. Idempotent: skips any single
(script_id IS NULL) whose label already exists for that account, so re-running
never duplicates and it never touches existing content. Media is left EMPTY —
the rows are inert ("no media = not offerable") until the operator attaches media.
Tagged ["upsell_ladder"] for easy identification/removal.
"""
import asyncio
import json
import sys
from datetime import datetime

sys.path.insert(0, "/app/service")

from sqlalchemy import select
from db.engine import get_session
from db.models import CatalogItem, Account

TARGET_NICKNAMES = ["AriaFree", "AriaPaid", "SofiaPaid", "sakai free", "Lexi", "maile free"]

# (label, kind, description_for_ai, price_cents, tip_unlock_cents)
LADDER = [
    ("Quick rate",        "image",     "rate me babe? 🙈 a quick lil peek just for you",                      400,   400),
    ("Selfie set",        "image_set", "no makeup, just me being cute for you 😘",                            600,   600),
    ("Lingerie set",      "image_set", "wore this thinking of you… wanna see it come off?",                 1200,  1200),
    ("Shower strip",      "video",     "watch me get undressed and step into the shower 🚿",                 1800,  1800),
    ("Full nude set",     "image_set", "all of me, nothing left on 🤭",                                      2500,  2500),
    ("Bed tease",         "video",     "dancing and teasing on my bed just for you baby",                   3000,  3000),
    ("Body rate video",   "video",     "close up… tell me exactly what you'd do to me 🥵",                   4500,  4500),
    ("Dirty talk / JOI",  "video",     "let me talk you through it and tell you exactly what to do",         5500,  5500),
    ("Toy play",          "video",     "playing with my toy until i finish for you 😈",                      7500,  7900),
    ("Long solo finish",  "video",     "the full thing, start to finish, no cuts",                         10000, 10000),
    ("Collab scene",      "video",     "me and him, the whole scene 🔥",                                    15000, 15000),
    ("Custom / exclusive","video",     "one of one, made just for you — nobody else ever sees this",        20000, 20000),
]


async def main():
    now = datetime.utcnow()
    async with get_session() as s:
        accts = {a.nickname: a.id for a in (await s.execute(select(Account))).scalars().all()}
    print(f"resolved {len(accts)} accounts")

    for nick in TARGET_NICKNAMES:
        aid = accts.get(nick)
        if aid is None:
            print(f"  ! {nick}: NOT FOUND — skipped")
            continue
        async with get_session() as s:
            existing = set((await s.execute(
                select(CatalogItem.label).where(
                    CatalogItem.account_id == aid,
                    CatalogItem.script_id.is_(None)))).scalars().all())
            inserted = 0
            for label, kind, desc, price, tip in LADDER:
                if label in existing:
                    continue
                s.add(CatalogItem(
                    account_id=aid, script_id=None, position=None, kind=kind,
                    label=label, description_for_ai=desc,
                    media_ids="[]", preview_media_ids="[]", duration_sec=None,
                    price_cents=price, tip_unlock_cents=tip, is_free_teaser=False,
                    tags=json.dumps(["upsell_ladder"]), enabled=True,
                    created_at=now, updated_at=now))
                inserted += 1
        print(f"  ✓ {nick} ({aid}): +{inserted} singles "
              f"({len(LADDER) - inserted} already present, skipped)")

    print("done")


asyncio.run(main())
