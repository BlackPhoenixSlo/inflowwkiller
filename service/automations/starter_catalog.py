"""service/automations/starter_catalog.py — the "Load starter pack" templates.

Ten prewritten sellable pieces per lane. The operator clicks once, they land in
Singles, and the only thing left to do is attach media (`media_ids` empty ⇒ "not
offerable", so nothing can sell by accident before that).

WHY THIS IS SERVER-SIDE, like `script_packs`
--------------------------------------------
`description_for_ai` is not a label. It is the pitch contract — the text the model
is told the fan will SEE, and the only thing it is permitted to claim about a piece
("What the fan SEES, present tense — the AI may only claim what's written here").
It is prompt text that one click persists onto the account.

It lived in `UpsellerTab.tsx` as two hand-keyed tables the browser picked between.
That put a lane in the client for one array, right after the script pack — the same
kind of text, one click from the same store — had moved to the server. Two answers
to "which lane is this account?" on opposite sides of the wire is one too many: the
column is the server's, `_voice.norm_voice` is the server's, and after this the
browser holds no lane at all and renders whatever it is handed.

WHY TWO COMPLETE TABLES rather than one shape with per-lane overrides
--------------------------------------------------------------------
Seven of the ten rows differ, and `_voice.py`'s own design note (see its header)
rejects deriving one lane from the other — that is the `base.replace(FRAME_HER,
FRAME_HIM)` design it was written to kill. Per-lane COMPLETE values, with the
invariants that must NOT vary asserted in `tests/test_voice_lane.py` instead:
same length, same price ladder, same `kind`, `tip_unlock_cents == price_cents`.
The ladder is what the upseller walks; it must not move because the lane did.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from . import _voice

# Shape parity with the UI's NEW_ITEM / the CatalogItem columns the singles
# endpoint writes. Everything not named here is a per-row value below.
_BASE: dict[str, Any] = {
    "kind": "video", "label": "", "description_for_ai": "", "media_ids": [],
    "preview_media_ids": [], "duration_sec": None, "price_cents": 0,
    "tip_unlock_cents": 0, "is_free_teaser": False, "tags": [], "enabled": True,
}


def _rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{**_BASE, **r} for r in rows]


STARTER_SINGLES_HER: list[dict[str, Any]] = _rows([
    {"label": "Ass tease", "kind": "video",
     "description_for_ai": "a short teasing clip of me turning around and playing with my ass for you",
     "price_cents": 800, "tip_unlock_cents": 800},
    {"label": "Feet set", "kind": "image_set",
     "description_for_ai": "a set of photos of my bare feet and soles, close up",
     "price_cents": 1000, "tip_unlock_cents": 1000},
    {"label": "Lingerie set", "kind": "image_set",
     "description_for_ai": "photos of me posing in my lingerie before I take it off",
     "price_cents": 1200, "tip_unlock_cents": 1200},
    {"label": "Shower strip", "kind": "video",
     "description_for_ai": "a video of me slowly getting undressed and stepping into the shower",
     "price_cents": 1500, "tip_unlock_cents": 1500},
    {"label": "Full nude set", "kind": "image_set",
     "description_for_ai": "a set of fully nude photos of me on my bed",
     "price_cents": 1800, "tip_unlock_cents": 1800},
    {"label": "Bed dance", "kind": "video",
     "description_for_ai": "a video of me dancing and teasing on my bed for you",
     "price_cents": 2000, "tip_unlock_cents": 2000},
    {"label": "Dirty talk / JOI", "kind": "video",
     "description_for_ai": "a video of me talking dirty and telling you exactly what to do",
     "price_cents": 2500, "tip_unlock_cents": 2500},
    {"label": "Toy play", "kind": "video",
     "description_for_ai": "a video of me playing with my toy until I finish",
     "price_cents": 3500, "tip_unlock_cents": 3500},
    {"label": "Solo finish", "kind": "video",
     "description_for_ai": "a longer video of me touching myself all the way to the end",
     "price_cents": 4500, "tip_unlock_cents": 4500},
    {"label": "B/G collab", "kind": "video",
     "description_for_ai": "a video of me with a partner — the full scene",
     "price_cents": 6000, "tip_unlock_cents": 6000},
])

STARTER_SINGLES_HIM: list[dict[str, Any]] = _rows([
    {"label": "Post-gym", "kind": "video",
     "description_for_ai": "a short clip of me straight after training, shirtless and still pumped",
     "price_cents": 800, "tip_unlock_cents": 800},
    {"label": "Feet set", "kind": "image_set",
     "description_for_ai": "a set of photos of my bare feet and soles, close up",
     "price_cents": 1000, "tip_unlock_cents": 1000},
    {"label": "Underwear set", "kind": "image_set",
     "description_for_ai": "photos of me in my underwear before it comes off",
     "price_cents": 1200, "tip_unlock_cents": 1200},
    {"label": "Shower", "kind": "video",
     "description_for_ai": "a video of me stripping down and stepping into the shower",
     "price_cents": 1500, "tip_unlock_cents": 1500},
    {"label": "Full nude set", "kind": "image_set",
     "description_for_ai": "a set of fully nude photos of me on my bed",
     "price_cents": 1800, "tip_unlock_cents": 1800},
    {"label": "Body tour", "kind": "video",
     "description_for_ai": "a slow video of me showing you every part of my body",
     "price_cents": 2000, "tip_unlock_cents": 2000},
    {"label": "Dirty talk / JOI", "kind": "video",
     "description_for_ai": "a video of me talking dirty and telling you exactly what to do",
     "price_cents": 2500, "tip_unlock_cents": 2500},
    {"label": "Toy play", "kind": "video",
     "description_for_ai": "a video of me using a toy on myself until I finish",
     "price_cents": 3500, "tip_unlock_cents": 3500},
    {"label": "Solo finish", "kind": "video",
     "description_for_ai": "a longer video of me stroking all the way to the end",
     "price_cents": 4500, "tip_unlock_cents": 4500},
    {"label": "Collab", "kind": "video",
     "description_for_ai": "a video of me with a partner — the full scene",
     "price_cents": 6000, "tip_unlock_cents": 6000},
])

STARTER_BY_VOICE: dict[str, list[dict[str, Any]]] = {
    _voice.VOICE_HER: STARTER_SINGLES_HER,
    _voice.VOICE_HIM: STARTER_SINGLES_HIM,
}


def starter_singles(voice: object = _voice.VOICE_HER) -> list[dict[str, Any]]:
    """The starter rows for this account's lane — a DEEP copy.

    The caller attaches media to these rows, so handing out anything shared lets one
    account's edit rewrite the template every other account is then offered. This
    hand-enumerated the list-valued fields ("the two list-valued fields", when there
    are three) — a copy depth kept in sync by counting is the kind that goes stale on
    the first new field. `deepcopy` says it once and cannot miscount.

    Unknown/NULL/garbage → the shipped lane, same as everywhere else."""
    return deepcopy(STARTER_BY_VOICE[_voice.norm_voice(voice)])
