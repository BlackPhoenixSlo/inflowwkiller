#!/usr/bin/env python3
"""service/seeds/funnels.py — port the V1 JSON funnels into the DB.

The legacy chatter loaded funnel definitions from
`OF_AUTOMATIONV1/mass_message_funnels/*.json`. In this backend world a funnel is a
row in `mass_message_funnels` (opening_message + steps_json), consumed by A10
send_mass_message (the opener) and A11 reply_mass_funnel (the reply steps).

This seed upserts those funnels by `name` (idempotent — re-running just refreshes
the text). Add new funnels to `FUNNELS` below or load them from the V1 JSON dir.

Run:  ./venv/bin/python service/seeds/funnels.py            # seed all FUNNELS
      ./venv/bin/python service/seeds/funnels.py --list     # show what's in DB
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

from sqlalchemy import select                                   # noqa: E402
from sqlalchemy.dialects.sqlite import insert as sqlite_insert  # noqa: E402

from db.engine import get_session                               # noqa: E402
from db.models import MassMessageFunnel                         # noqa: E402

# ── Funnel definitions (ported verbatim from OF_AUTOMATIONV1/mass_message_funnels) ──
FUNNELS: list[dict] = [
    {
        "name": "strokes",
        "description": ("Strokes engagement funnel — opens with a spicy question, "
                        "warms up with 3 reply steps, closes with paid PPV content."),
        "opening_message": "How many strokes does it take to get you off? 😈",
        "vault_folder": "Mass_Messages",
        "media_indices": [2],
        "steps": [
            {"step": 1, "trigger": "any_reply", "check_intervals_min": [2, 4, 10],
             "messages": [
                 "bet you lose count once it starts getting slopyyy",
                 "or do you stay focused even with all that pressure building? c:"]},
            {"step": 2, "trigger": "any_reply", "check_intervals_min": [2, 4, 10],
             "messages": [
                 "i can feel you pulsating just thinking about it...",
                 "so tell me would you explode fast or last if I were the one "
                 "dictating the tempo?"]},
            {"step": 3, "trigger": "any_reply", "check_intervals_min": [2, 4, 10],
             "messages": [
                 "how about you pull your friend out, and we find out how many "
                 "strokes it will take you to bust?",
                 "if you last long enough, I promise to praise you and reward "
                 "your hard work"]},
            {"step": 4, "trigger": "any_reply", "type": "paid_ppv",
             "vault_folder": "Mass_Messages", "media_indices": "all", "price": 24,
             "messages": [
                 "if it wasn't obvious already, i love to be in control. not just "
                 "of you, but your every thought, action & desire. open this & "
                 "show me how obedient ure going to be, taking out ur cock & "
                 "slowly moving it up and down the shaft while i spread my legs "
                 "infront of your face showing just how pink it is"]},
        ],
    },
    {
        "name": "strokes_fr",
        "description": ("Strokes funnel (French) — opens with a spicy question, "
                        "warms up with 3 reply steps, closes with paid PPV content."),
        "opening_message": "Combien de va-et-vient il te faut pour jouir ? 😈",
        "vault_folder": "Mass_Messages",
        "media_indices": [4],
        "steps": [
            {"step": 1, "trigger": "any_reply", "check_intervals_min": [2, 4, 10],
             "messages": [
                 "Je parie que tu perds le compte dès que ça devient bien glissant "
                 "et tout mouillé",
                 "ou tu restes concentré même avec toute cette pression qui monte ? c:"]},
            {"step": 2, "trigger": "any_reply", "check_intervals_min": [2, 4, 10],
             "messages": [
                 "Je te sens palpiter rien qu'en y pensant...",
                 "Dis-moi, tu exploserais vite ou tu tiendrais longtemps si c'était "
                 "moi qui dictait le rythme ?"]},
            {"step": 3, "trigger": "any_reply", "check_intervals_min": [2, 4, 10],
             "messages": [
                 "Et si tu sortais ta queue, qu'on découvre combien de va-et-vient "
                 "il te faut pour décharger ?",
                 "Si tu tiens assez longtemps, je promets de te féliciter et de "
                 "récompenser tes efforts"]},
            {"step": 4, "trigger": "any_reply", "type": "paid_ppv",
             "vault_folder": "Mass_Messages", "media_indices": "all", "price": 24,
             "messages": [
                 "Au cas où c'était pas déjà clair, j'adore être aux commandes. Pas "
                 "seulement sur toi, mais sur chacune de tes pensées, tes actions et "
                 "tes désirs. Ouvre ça et montre-moi à quel point tu vas être "
                 "obéissant, en sortant ta queue et en la caressant lentement de "
                 "haut en bas pendant que j'écarte mes jambes devant ton visage pour "
                 "te montrer à quel point c'est rose et trempé"]},
        ],
    },
]


async def seed() -> list[tuple[int, str]]:
    now = datetime.utcnow()
    for f in FUNNELS:
        vals = dict(
            name=f["name"], description=f.get("description"),
            opening_message=f["opening_message"],
            vault_folder=f.get("vault_folder"),
            media_indices=json.dumps(f.get("media_indices", [])),
            steps_json=json.dumps(f["steps"]), updated_at=now,
        )
        async with get_session() as s:
            await s.execute(
                sqlite_insert(MassMessageFunnel).values(**vals)
                .on_conflict_do_update(
                    index_elements=["name"],
                    set_={k: v for k, v in vals.items() if k != "name"})
            )
    return await _list()


async def _list() -> list[tuple[int, str]]:
    async with get_session() as s:
        rows = (await s.execute(
            select(MassMessageFunnel.id, MassMessageFunnel.name)
            .order_by(MassMessageFunnel.id))).all()
    return [(int(i), n) for i, n in rows]


async def _main(list_only: bool):
    rows = await _list() if list_only else await seed()
    verb = "in DB" if list_only else "seeded"
    print(f"[funnels] {verb}: " + ", ".join(f"{i}:{n}" for i, n in rows))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true", help="just list DB funnels")
    a = ap.parse_args()
    asyncio.run(_main(a.list))
