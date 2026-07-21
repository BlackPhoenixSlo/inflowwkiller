"""service/vault_scripts.py — recover the SCRIPT a vault batch was shot as.

A creator does not shoot media one item at a time. She shoots a *script*: a run
of clips that escalates, almost always along the same arc —

    clothed  →  stripping / lingerie  →  nude  →  touching  →  masturbation

and then uploads the whole run to OF in one go. Two facts make that arc
recoverable, and one fact makes it necessary:

  * Recoverable (1) — a batch is a tight burst in OF's own `createdAt`. On Aria
    166 of 249 consecutive gaps are under 10 seconds; day-long gaps separate the
    shoots. So a gap threshold splits the vault into candidate scripts with no
    vision work at all.
  * Recoverable (2) — every item's rung on the ladder is already in the V2
    describe fields (`clothing_state`, `acts`, `penetration`). Scoring is pure
    arithmetic over data we have: no LLM call, no OF traffic, instant re-run.
  * Necessary — the upload is frequently REVERSED. Phone galleries hand the
    picker newest-first, so the batch lands masturbation-first and the arc reads
    backwards. Sorting a folder by `createdAt` therefore shows the payoff before
    the tease, which is exactly backwards for selling it.

This module scores each item, groups the batch, decides which END of the batch
is the beginning, and writes a canonical `script_seq` so every downstream
consumer (picker, folder order, PPV assemble) can just ORDER BY it.

Orientation is decided per BATCH, never per item — that is the whole point of
looking at the previous and later clips. A single nude still says nothing about
direction; a run of eight that starts nude and ends dressed says the phone
handed them over backwards.

Nothing here mutates OF. `plan_scripts` is read-only and returns the proposal;
`apply_scripts` persists it to our own mirror columns.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Any, Iterable, Sequence

from sqlalchemy import select, update

import vault_ai_brief
from db.engine import get_session
from db.models import VaultFolderItem, VaultItem

log = logging.getLogger("of-relay.vault_scripts")

# Batches are split where the upload gap exceeds this. 15 minutes is chosen off
# the measured gap histogram, not taste: real bursts sit under 10s and the next
# populated bucket is hours away, so anything from ~2min to ~1h yields the same
# clusters. Exposed as a parameter because a creator who uploads a shoot in
# dribs over an evening needs it wider.
DEFAULT_GAP_SECONDS = 900

# A batch shorter than this is never re-oriented: two items cannot evidence a
# direction, they can only evidence a difference.
MIN_ORIENT_LEN = 3

# Kendall tau below which we refuse to call a direction and keep upload order.
# 0.30 is deliberately timid — misordering a script the operator curated by hand
# is worse than leaving a genuinely-reversed one alone for them to flip.
MIN_TAU = 0.30

# Minimum spread (max score − min score) before a batch may be re-oriented.
#
# Tau measures ONLY the consistency of a trend, never its size, so a batch
# scoring 60,55,55 returns tau = −1.00 — maximum confidence off a five-point
# wobble. Observed on Aria's batch 4542417745, which is three near-identical
# nude clips and no script at all. The floor demands a real rung transition
# (dressed→lingerie, lingerie→nude) before we act.
#
# It is deliberately set BELOW a full rung gap and above noise: a batch that
# only moves nude(60)→fingering(70) is left in upload order. That is the
# conservative side of a genuine trade-off — such a batch is a weak script, and
# leaving it alone costs the operator one manual flip, whereas acting on
# five-point noise silently scrambles sets that were already correct.
MIN_RANGE = 15

# A group smaller than this is not a shoot. Two items are a pair, and a folder
# per pair is worse than no folder.
MIN_SCRIPT_ITEMS = 3

SCORE_KIND = "ladder/v1"


# ── The ladder ──────────────────────────────────────────────────────
#
# Four independent axes, each mapped onto the SAME 0-100 arc, and an item's
# score is the MAX over the axes that are present.
#
# Max, not a weighted blend. A blend has to re-normalise when an axis is
# missing, and re-normalising makes items with different field coverage
# incomparable: under the blend a nude item with no `acts` scored 90 while a
# nude item with a toy actually inserted scored 87, because introducing the
# (mid-rung) acts component diluted the two saturated ones. Ordering a script by
# that is worse than not ordering it. Max has no such coupling — an absent axis
# contributes nothing instead of silently reweighting its neighbours, and every
# axis can only ever push an item LATER in the arc, which is what "how far along
# is this" actually means.
#
# The axes fail in different ways, which is why all four are kept: `acts` is the
# strongest signal but is absent on stills, `clothing_state` is present almost
# everywhere but saturates mid-arc, `penetration` is narrow but decisive, and
# `explicitness` is coarse but essentially never missing.

# Clothing tops out at 60, NOT 100: being naked is the MIDDLE of the arc, not
# the end of it. A nude still must not outrank a clip where she is fingering
# herself, and capping the axis is what guarantees that.
_CLOTHING: dict[str, int] = {
    "dressed": 0,
    "lingerie_on": 25,
    "partially_off": 40,
    "pulled_down": 50,
    "pulled_aside": 55,   # underwear still on but she is exposed — later than
                          # pulled_down in the arc, which is why V2 splits them
    "fully_nude": 60,
}

# The rung each act sits on. Gaps are intentional: `fingering` (70) to
# `toy_insertion` (85) is a real escalation, `rubbing_clit` to `fingering` is a
# smaller one, and the ordering of these numbers IS the product decision.
_ACTS: dict[str, int] = {
    "none": 0,
    "talking_to_camera": 0,
    "posing": 5,
    "shower": 15,
    "bath": 15,
    "tease": 20,
    "strip": 30,
    "undress": 35,
    "ass_shaking": 40,
    "twerk": 40,
    "groping_own_breasts": 45,
    "spreading": 55,
    "rubbing_clit": 65,
    "fingering": 70,
    "toy_on_clit": 78,
    "toy_insertion": 85,
    "riding_toy": 88,
    "blowjob": 88,
    "handjob": 85,
    "sex_missionary": 90,
    "sex_doggy": 90,
    "sex_riding": 90,
    "masturbation_orgasm": 95,
    "squirt": 98,
    "cumshot": 98,
}

# The model does not always return the menu spelling. Observed on real Aria
# batches: `stripping` for strip, `rubbing_breasts` for groping_own_breasts.
# Unknown acts are ignored rather than guessed at, so drift silently costs
# signal — these are the spellings actually seen, not speculative synonyms.
_ACT_ALIASES: dict[str, str] = {
    "stripping": "strip",
    "undressing": "undress",
    "rubbing_breasts": "groping_own_breasts",
    "groping_breasts": "groping_own_breasts",
    "twerking": "twerk",
    "teasing": "tease",
    "posing_for_camera": "posing",
    "masturbation": "masturbation_orgasm",
    "orgasm": "masturbation_orgasm",
    "squirting": "squirt",
}

_PENETRATION: dict[str, int] = {
    "none": 0, "fingers": 70, "toy": 85, "penis": 95,
}

# Coarse, but the one axis the model essentially always fills in. `hardcore` is
# defined as penetration/cum/a partner act, so it legitimately sits late.
_EXPLICITNESS: dict[str, int] = {
    "sfw": 0, "suggestive": 25, "explicit": 55, "hardcore": 85,
}


def _norm(v: Any) -> str:
    return str(v or "").strip().lower()


def escalation_score(fields: dict[str, Any]) -> int | None:
    """Where this item sits on the clothed→masturbation ladder, 0-100.

    The max over every axis that is present (see the ladder note above).

    Returns None when the V2 fields carry no usable signal at all (every axis
    missing or "unclear"). None is NOT zero: an unscoreable item must not be
    sorted to the front of a script as though it were the tame opener.
    """
    if not isinstance(fields, dict):
        return None

    rungs: list[int] = []

    clothing = _CLOTHING.get(_norm(fields.get("clothing_state")))
    if clothing is not None:
        rungs.append(clothing)

    # An item's act rung is the HIGHEST act present. A clip that opens with a
    # tease and ends with insertion belongs at the insertion end of the script.
    acts = fields.get("acts")
    act_rungs = []
    for raw_act in acts or []:
        a = _norm(raw_act)
        a = _ACT_ALIASES.get(a, a)
        if a in _ACTS:
            act_rungs.append(_ACTS[a])
    if act_rungs:
        rungs.append(max(act_rungs))

    pen = _PENETRATION.get(_norm(fields.get("penetration")))
    if pen:
        rungs.append(pen)

    explicit = _EXPLICITNESS.get(_norm(fields.get("explicitness")))
    if explicit is None:
        explicit = _EXPLICITNESS.get(_norm(fields.get("explicitness_tier")))
    if explicit is not None:
        rungs.append(explicit)

    return max(rungs) if rungs else None


# ── Batching ────────────────────────────────────────────────────────

def cluster_by_upload(
    rows: Sequence[dict[str, Any]], gap_seconds: int = DEFAULT_GAP_SECONDS
) -> list[list[dict[str, Any]]]:
    """Split items (ASC by `created_at`) wherever the upload gap exceeds
    `gap_seconds`. Purely temporal — no content signal is consulted, so a batch
    is whatever was pushed to OF in one sitting."""
    groups: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    prev: datetime | None = None
    for r in rows:
        ts = r.get("created_at")
        if prev is not None and ts is not None and (ts - prev).total_seconds() > gap_seconds:
            groups.append(current)
            current = []
        current.append(r)
        if ts is not None:
            prev = ts
    if current:
        groups.append(current)
    return groups


# ── Orientation ─────────────────────────────────────────────────────

def kendall_tau(scores: Sequence[float]) -> float:
    """Rank correlation between a batch's scores and its upload order, -1..1.

    Kendall rather than Pearson because scores are ordinal rungs, batches are
    short (often 3-8 items), and ties are everywhere — a run of four nude stills
    scores identically and must contribute NO evidence of direction rather than
    dragging a least-squares fit around.
    """
    n = len(scores)
    if n < 2:
        return 0.0
    con = dis = 0
    for i in range(n):
        for j in range(i + 1, n):
            d = scores[j] - scores[i]
            if d > 0:
                con += 1
            elif d < 0:
                dis += 1
    total = con + dis
    return 0.0 if total == 0 else (con - dis) / total


def orient(scores: Sequence[float | None]) -> tuple[str, float, str]:
    """('forward' | 'reverse' | 'flat', tau, reason) for one batch.

    'flat' means we keep upload order, but it covers three genuinely different
    situations and the operator needs to tell them apart — a 40-photo dump with
    no arc is not the same problem as a 2-item batch. `reason` names which:

      too_short   fewer than MIN_ORIENT_LEN scoreable items
      too_narrow  a real trend, but spanning less than MIN_RANGE (noise)
      wandering   enough spread, no consistent direction — usually a bulk photo
                  dump that simply is not a script
      ok          a direction was called (forward/reverse)
    """
    known = [s for s in scores if s is not None]
    if len(known) < MIN_ORIENT_LEN or len(set(known)) < 2:
        return "flat", 0.0, "too_short"
    if max(known) - min(known) < MIN_RANGE:
        # Consistent, but too small to be an arc. See MIN_RANGE.
        return "flat", 0.0, "too_narrow"
    tau = kendall_tau(known)
    if tau <= -MIN_TAU:
        return "reverse", tau, "ok"
    if tau >= MIN_TAU:
        return "forward", tau, "ok"
    return "flat", tau, "wandering"


# ── Planning ────────────────────────────────────────────────────────

async def _load_rows(account_id: str) -> list[dict[str, Any]]:
    async with get_session() as s:
        recs = (await s.execute(
            select(VaultItem.media_id, VaultItem.kind, VaultItem.created_at,
                   VaultItem.duration_seconds, VaultItem.ai_fields_json,
                   VaultItem.describe_status)
            .where(VaultItem.account_id == account_id,
                   VaultItem.removed_at.is_(None))
            .order_by(VaultItem.created_at.asc(), VaultItem.media_id.asc())
        )).all()

    out: list[dict[str, Any]] = []
    for mid, kind, created, dur, fields_json, status in recs:
        try:
            fields = json.loads(fields_json) if fields_json else {}
        except (TypeError, ValueError):
            fields = {}
        out.append({
            "media_id": int(mid), "kind": kind, "created_at": created,
            "duration_seconds": dur, "fields": fields, "describe_status": status,
            "score": escalation_score(fields),
        })
    return out


async def plan_scripts(
    account_id: str, *, gap_seconds: int = DEFAULT_GAP_SECONDS,
    videos_only: bool = False,
) -> dict[str, Any]:
    """Read-only proposal: batches, their orientation, and the canonical order.

    `videos_only` restricts BOTH the batching and the orientation vote to clips.
    Stills shot alongside a run are then left unscripted rather than diluting the
    clip arc — worth trying on a vault whose photos outnumber videos 9:1.
    """
    rows = await _load_rows(account_id)
    if videos_only:
        rows = [r for r in rows if r["kind"] == "video"]

    scripts: list[dict[str, Any]] = []
    for batch in cluster_by_upload(rows, gap_seconds):
        direction, tau, reason = orient([r["score"] for r in batch])
        ordered = list(reversed(batch)) if direction == "reverse" else list(batch)
        script_id = min(r["media_id"] for r in batch)
        kinds: dict[str, int] = {}
        for r in batch:
            kinds[r["kind"]] = kinds.get(r["kind"], 0) + 1
        scripts.append({
            "script_id": script_id,
            "direction": direction,
            "reason": reason,
            "tau": round(tau, 3),
            "size": len(batch),
            "kinds": kinds,
            "scoreable": sum(1 for r in batch if r["score"] is not None),
            "started_at": batch[0]["created_at"],
            "items": [
                {**r, "script_seq": i + 1, "script_id": script_id}
                for i, r in enumerate(ordered)
            ],
        })

    reversed_n = sum(1 for s in scripts if s["direction"] == "reverse")
    return {
        "account_id": account_id,
        "gap_seconds": gap_seconds,
        "videos_only": videos_only,
        "items": len(rows),
        "scripts": scripts,
        "summary": {
            "batches": len(scripts),
            "reversed": reversed_n,
            "forward": sum(1 for s in scripts if s["direction"] == "forward"),
            "flat": sum(1 for s in scripts if s["direction"] == "flat"),
            "flat_wandering": sum(1 for s in scripts if s["reason"] == "wandering"),
            "flat_too_narrow": sum(1 for s in scripts if s["reason"] == "too_narrow"),
            "flat_too_short": sum(1 for s in scripts if s["reason"] == "too_short"),
            "unscoreable_items": sum(1 for r in rows if r["score"] is None),
        },
    }


# ── Send order ──────────────────────────────────────────────────────
#
# `escalation_score` above answers "how far along the arc is this?", which is
# what DIRECTION detection needs: one monotone number Kendall tau can correlate
# against upload order. It is deliberately coarse.
#
# Deciding what to SEND FIRST is a different question, and a single 0-100 blob
# flattens distinctions that decide the sale. The key below is a tuple, and it
# reads its facts from `vault_ai_brief.sellability()` rather than re-deriving
# them — that module already owns the selling taxonomy (EXPLICITNESS_LADDER,
# _PAYOFF_ACTS, _REVEAL_TENSION), and a second parallel copy of it here would
# drift the moment either side is tuned.
#
# The axes, in priority order:
#
#  1. rung — sfw→suggestive→explicit→hardcore. "A night's sends walk it upward."
#     This is the coarse escalation and it dominates everything else.
#  2. build-up before payoff — an item WITHOUT a payoff act is a rung that tees
#     up the next send; one WITH a payoff can close on its own. Within a rung,
#     tease first and close last. This is the axis the old single score could
#     not express at all: a nude tease and a nude fingering clip both scored 60.
#  3. reveal tension — dressed → lingerie → mid-strip → fully_nude. Chronological
#     within the strip, which is also how it was filmed. Note this is ORDER, not
#     value: `vault_ai_brief` correctly ranks the "almost" states as the highest
#     VALUE to sell, but they still come before full nudity in a night.
#  4. the act ladder — fine-grain within all of the above.
#  5. filmed order — `script_id`/`script_seq`, so same-rung items keep the order
#     the shoot actually ran in (with the batch's direction already corrected).
#
# Anything we cannot place lands last, never in the opening slot.

_CLOTHING_PROGRESS: dict[str, int] = {
    "dressed": 0, "lingerie_on": 1, "partially_off": 2,
    "pulled_down": 3, "pulled_aside": 4, "fully_nude": 5,
}

_UNKNOWN = 99


def send_sort_key(fields: dict[str, Any], *, script_id: int | None = None,
                  script_seq: int | None = None, media_id: int = 0
                  ) -> tuple[int, ...]:
    """Sort key placing an item in SEND order — tease first, payoff last."""
    sell = vault_ai_brief.sellability(fields)

    rung = sell["rung"] if sell["rung"] is not None else _UNKNOWN
    closes = 1 if sell["stands_alone"] else 0
    clothing = _CLOTHING_PROGRESS.get(
        _norm(fields.get("clothing_state")), _UNKNOWN)
    act = escalation_score(fields)
    return (rung, closes, clothing, act if act is not None else _UNKNOWN,
            script_id or 0, script_seq or 0, media_id)


def send_order_reason(fields: dict[str, Any]) -> dict[str, Any]:
    """The human-readable 'why is it here' behind `send_sort_key`, for the
    review UI. Same facts, no second derivation."""
    sell = vault_ai_brief.sellability(fields)
    return {
        "tier": sell["tier"],
        "rung": sell["rung"],
        "closes": sell["stands_alone"],
        "payoff_acts": sell["payoff_acts"],
        "reveal": sell["reveal"],
        "clothing_state": _norm(fields.get("clothing_state")) or "unclear",
        "solo": sell["solo"],
        "beats": [b for b in (fields.get("beats") or []) if str(b).strip()],
        "acts": [str(a) for a in (fields.get("acts") or []) if str(a).strip()],
    }


# ── Folder send-order (the operator-facing output) ──────────────────
#
# The script columns on `vault_items` answer "which shoot is this, and how far
# along its arc?" — that is a property of the MEDIA, so it lives on the item.
# But "what do I send first?" is a property of the FOLDER, and the same photo can
# sit in several folders with a different position in each. So the order is
# written to `VaultFolderItem.manual_order`, per folder, using the convention
# already in the schema and the UI: 0 first, then 1,2,3… from the front, NULL
# unpinned (date order), then …-3,-2,-1 dead last.
#
# Send order is ESCALATING — tease first, payoff last. Two keys:
#   1. `script_score` — the coarse rung (clothed → nude → masturbation).
#   2. `script_seq`   — position inside its own shoot, which breaks ties among
#      items on the same rung using the batch's ALREADY-CORRECTED direction.
# Without (2) a folder of eight equally-nude clips would come out in arbitrary
# order; with it they stay in the order they were actually filmed.

async def plan_folder_order(account_id: str, folder_id: int) -> list[dict[str, Any]]:
    """`[{media_id, manual_order, score, …}]` for one internal folder, in send
    order. Read-only. Items we could not score keep their date order and go
    LAST — an unscoreable item must never open a folder."""
    async with get_session() as s:
        rows = (await s.execute(
            select(VaultItem.media_id, VaultItem.kind, VaultItem.created_at,
                   VaultItem.script_id, VaultItem.script_seq,
                   VaultItem.script_score, VaultItem.ai_fields_json)
            .join(VaultFolderItem,
                  (VaultFolderItem.account_id == VaultItem.account_id)
                  & (VaultFolderItem.media_id == VaultItem.media_id))
            .where(VaultItem.account_id == account_id,
                   VaultItem.removed_at.is_(None),
                   VaultFolderItem.folder_id == folder_id)
        )).all()

    items = []
    for m, k, c, sid, seq, sc, fields_json in rows:
        fields = vault_ai_brief.load_fields(fields_json)
        items.append({
            "media_id": int(m), "kind": k, "created_at": c, "script_id": sid,
            "script_seq": seq, "score": sc, "fields": fields,
            "why": send_order_reason(fields),
        })

    # Unplaceable items keep date order and go LAST — an item we could not read
    # must never open a folder.
    placed = [r for r in items if r["why"]["rung"] is not None]
    unplaced = [r for r in items if r["why"]["rung"] is None]
    placed.sort(key=lambda r: send_sort_key(
        r["fields"], script_id=r["script_id"], script_seq=r["script_seq"],
        media_id=r["media_id"]))
    unplaced.sort(key=lambda r: (r["created_at"] or datetime.min, r["media_id"]))

    return [{**r, "manual_order": i + 1}
            for i, r in enumerate(placed + unplaced)]


async def apply_folder_order(account_id: str, folder_id: int,
                             order: list[dict[str, Any]]) -> dict[str, int]:
    """Persist a folder's send order onto `VaultFolderItem.manual_order`.

    Scoped to ONE folder — the same media in another folder is untouched, which
    is the whole reason the order does not live on the item. No OF writes.
    """
    written = 0
    async with get_session() as s:
        for row in order:
            await s.execute(
                update(VaultFolderItem)
                .where(VaultFolderItem.account_id == account_id,
                       VaultFolderItem.folder_id == folder_id,
                       VaultFolderItem.media_id == row["media_id"])
                .values(manual_order=row["manual_order"])
            )
            written += 1
        await s.commit()
    log.info("vault folder order applied account=%s folder=%s items=%d",
             account_id, folder_id, written)
    return {"folder_id": folder_id, "items": written}


# ── Outfit scripts (what a script actually IS) ──────────────────────
#
# The upload-burst grouping above finds "media pushed to OF in one sitting".
# That is NOT a script. Measured on AriaFree, bursts are kind-PURE — she uploads
# her photos in one go and her clips in another — so burst grouping splits every
# real shoot down the middle and can never produce the mix an operator means.
#
# A script is a SHOOT: one outfit, worn across both stills and clips, escalating
# from covered to explicit, uploaded around the same time. The linking signal is
# therefore the OUTFIT, and it is already in the describe output as
# `clothing_items`. Grouping by it re-joins what the burst split:
#
#   black : 46 items = 36 photos + 10 videos, sfw→suggestive→explicit
#   pink  : 19 items = 14 photos +  5 videos, sfw→suggestive→explicit
#
# Colour is the key, not the full garment string. The model writes free text and
# names the same set five ways — "pink lace bra", "pink lace panties", "pink
# lace thong", "pink thong", "pink satin" — so an exact-string match finds
# nothing while the colour is stable across all of them. Material is only a
# fallback for when no colour was named.
#
# Colour alone would merge two different black shoots weeks apart, so a group is
# also split wherever it goes quiet for longer than `window_hours`.

# Multi-word colours FIRST — "dark blue" must win before "blue" matches inside it.
_COLOURS: tuple[str, ...] = (
    "dark blue", "light blue", "navy", "burgundy", "pink", "black", "white",
    "red", "blue", "green", "purple", "beige", "grey", "gray", "yellow",
    "orange", "brown", "silver", "gold", "cream", "lilac",
)

_MATERIALS: tuple[str, ...] = (
    "lace", "leather", "fishnet", "satin", "silk", "mesh", "denim", "velvet",
    "cotton",
)

# Colour alone is too coarse. AriaFree's "black" group came back as 46 items and
# the operator's read was blunt: three different sets. It was — one 15:27 burst
# alone held a leather jacket, a fur-lined jacket, four distinct tops, three
# bodysuits and two bra sets, all black. A bulk archive dump of many outfits
# that merely share a colour is not a shoot.
#
# So the key is colour PLUS the kind of garment. Ordered most-identifying first,
# because an item usually lists several pieces and the most distinctive one names
# the set: "black lace bodysuit, fishnet stockings" is a bodysuit shoot, not a
# hosiery shoot.
#
# Note this deliberately does NOT split on how undressed she is — `clothing_state`
# already carries that, and a shoot is *supposed* to run dressed → undressed.
# What it splits is which OUTFIT she had on.
_GARMENT_CLASSES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("bodysuit", ("bodysuit", "teddy", "leotard", "one-piece", "onepiece",
                  "corset", "bustier")),
    ("dress", ("dress", "gown", "robe")),
    ("swim", ("bikini", "swimsuit", "swimwear")),
    ("lingerie_set", ("bra", "bralette", "panties", "thong", "underwear",
                      "knickers", "briefs", "lingerie")),
    ("top", ("top", "shirt", "blouse", "crop", "tank", "halter", "sweater")),
    ("bottoms", ("skirt", "shorts", "pants", "jeans", "leggings", "bottoms")),
    ("outerwear", ("jacket", "coat", "hoodie", "cardigan")),
    ("hosiery", ("stockings", "fishnet", "tights", "socks")),
)

# Two different shoots in the same colour must not merge just because the colour
# matches. 48h keeps a shoot whose stills and clips went up on consecutive
# nights together (AriaFree's pink set spans 26 hours) while separating a black
# set shot a week later.
DEFAULT_OUTFIT_WINDOW_HOURS = 48


def short_outfit(outfit: str) -> str:
    """`pink lingerie_set` → `pink lingerie`. The outfit key is a machine key;
    this is what a human reads in a folder list."""
    return str(outfit).replace("_set", "").replace("_", " ").strip()


def outfit_colour(fields: dict[str, Any]) -> str:
    """The item's dominant garment colour, else its material. '' if unnamed."""
    if not isinstance(fields, dict):
        return ""
    text = " ".join(str(x).lower() for x in (fields.get("clothing_items") or []))
    if not text.strip():
        return ""
    for colour in _COLOURS:
        # Word-boundary match so "blue" cannot fire inside "dark blue".
        if re.search(rf"\b{re.escape(colour)}\b", text):
            return colour
    for material in _MATERIALS:
        if material in text:
            return material
    return ""


def garment_class(fields: dict[str, Any]) -> str:
    """Which KIND of outfit this is — bodysuit / lingerie_set / top / …"""
    if not isinstance(fields, dict):
        return ""
    text = " ".join(str(x).lower() for x in (fields.get("clothing_items") or []))
    for name, words in _GARMENT_CLASSES:
        if any(w in text for w in words):
            return name
    return ""


def outfit_key(fields: dict[str, Any]) -> str:
    """The outfit an item belongs to: `"<colour> <garment class>"`.

    Empty string when the describe named no garment at all — those items cannot
    be attributed to a shoot and are left out rather than pooled into a junk
    group. A colour with no recognised garment class still keys on the colour
    alone, so a set is never dropped just because its wording is unusual.
    """
    colour = outfit_colour(fields)
    if not colour:
        return ""
    kind = garment_class(fields)
    return f"{colour} {kind}".strip()


async def collect_outfit_scripts(
    account_id: str, *, window_hours: int = DEFAULT_OUTFIT_WINDOW_HOURS,
    min_items: int = MIN_SCRIPT_ITEMS, require_escalation: bool = True,
) -> list[dict[str, Any]]:
    """Shoots, recovered by outfit. Read-only.

    `require_escalation` drops groups that sit on ONE explicitness rung — a run
    of eight equally-suggestive stills in the same bra is a set, not a script,
    because there is nowhere to walk a fan to.

    Ranked best-first: a mix of stills AND clips, spanning the most rungs, is
    what an operator can actually build a night out of.
    """
    rows = await _load_rows(account_id)
    window = window_hours * 3600

    by_outfit: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        key = outfit_key(r.get("fields") or {})
        if key:
            by_outfit.setdefault(key, []).append(r)

    out: list[dict[str, Any]] = []
    for outfit, items in by_outfit.items():
        items.sort(key=lambda r: (r["created_at"] or datetime.min, r["media_id"]))
        # Split the colour into separate shoots wherever it goes quiet.
        segments: list[list[dict[str, Any]]] = []
        current: list[dict[str, Any]] = [items[0]]
        for prev, item in zip(items, items[1:]):
            gap = ((item["created_at"] - prev["created_at"]).total_seconds()
                   if item["created_at"] and prev["created_at"] else 0)
            if gap > window:
                segments.append(current)
                current = []
            current.append(item)
        segments.append(current)

        for seg in segments:
            if len(seg) < min_items:
                continue
            enriched = [{**r, "why": send_order_reason(r.get("fields") or {})}
                        for r in seg]
            rungs = {r["why"]["rung"] for r in enriched if r["why"]["rung"] is not None}
            if require_escalation and len(rungs) < 2:
                continue

            placed = [r for r in enriched if r["why"]["rung"] is not None]
            unplaced = [r for r in enriched if r["why"]["rung"] is None]
            placed.sort(key=lambda r: send_sort_key(
                r["fields"], script_id=r.get("script_id"),
                script_seq=r.get("script_seq"), media_id=r["media_id"]))
            ordered = placed + unplaced

            kinds: dict[str, int] = {}
            tiers: dict[str, int] = {}
            for r in ordered:
                kinds[r["kind"]] = kinds.get(r["kind"], 0) + 1
                tiers[r["why"]["tier"]] = tiers.get(r["why"]["tier"], 0) + 1

            started = ordered[0]["created_at"]
            out.append({
                "outfit": outfit,
                "name": ai_folder_name(short_outfit(outfit)),
                "size": len(ordered), "kinds": kinds, "tiers": tiers,
                "rungs": sorted(rungs),
                "mixed": len(kinds) > 1,
                "closes_on_own": sum(1 for r in ordered if r["why"]["closes"]),
                "started_at": min(r["created_at"] for r in seg if r["created_at"]),
                "ended_at": max(r["created_at"] for r in seg if r["created_at"]),
                "items": [{**r, "manual_order": i + 1}
                          for i, r in enumerate(ordered)],
            })

    # Mixed media first, then widest escalation, then biggest.
    out.sort(key=lambda s: (not s["mixed"], -len(s["rungs"]), -s["size"]))
    return out


# ── Purpose folders (what a weak shoot is still good for) ───────────
#
# Not every outfit group is a sellable shoot. AriaFree's `black top` (15 stills,
# ZERO closers) and `white top` (4 stills) have nothing to land a sale on — but
# they are perfectly good STORY material, and the explicit tail of a weak group
# still works as a free teaser on a mass send.
#
# So the leftovers get re-cut by heat instead of by outfit, into the two lanes
# an operator actually sends on:
#
#   Stories        sfw → suggestive   safe enough to post publicly
#   Mass / teaser  suggestive → explicit  the free hook on a broadcast
#
# The bands OVERLAP on `suggestive` deliberately, and that is the case the
# per-folder ordering was built for: a suggestive still belongs in both lanes and
# holds a DIFFERENT position in each — near the top of Stories (it is the
# spiciest thing there) and at the bottom of Mass (it is the mildest thing
# there). One item, two folders, two orders. Nothing about that works if order
# lives on the item.
# Every folder this module proposes is prefixed so an operator can always tell
# at a glance which folders a human curated and which the pipeline generated —
# and can delete the whole generated set without touching their own work.
AI_FOLDER_PREFIX = "AI-"


def ai_folder_name(name: str) -> str:
    """`AI-<name>`, never double-prefixed on a re-run."""
    clean = str(name).strip()
    if clean.startswith(AI_FOLDER_PREFIX):
        return clean[:120]
    return f"{AI_FOLDER_PREFIX}{clean}"[:120]


# Clothing states where something is revealed but she is still partly covered —
# the "one boob out" band. Distinct from fully_nude, and that distinction is the
# whole difference between the $10 and $50 tiers.
_PARTIAL_REVEAL = ("pulled_aside", "pulled_down", "partially_off")

# Tiers a paid tease can be cut from. Below explicit there is nothing to charge
# for; that material belongs in the free lanes.
_PAID_TIERS = ("explicit", "hardcore")


def _focus(fields: dict) -> set[str]:
    return {_norm(x) for x in (fields.get("body_focus") or []) if _norm(x)}


def _shows_genitals(fields: dict) -> bool:
    """Her vulva or anus is in frame AND bare.

    One signal now, not two. The flags pass answers "in frame and bare / in
    frame and covered / not in frame" directly, so this no longer has to
    triangulate visibility out of `body_focus` plus a fabric-only boolean —
    which is what let a dildo clip (`body_focus: None`) and an anal clip
    (`body_focus` without `pussy`) both read as showing nothing.
    """
    return (vault_ai_brief.is_bare(fields, "vulva_vis")
            or vault_ai_brief.is_bare(fields, "anus_vis"))


def _shows_breasts_bare(fields: dict) -> bool:
    """Her breasts are in frame with nothing over them — the $10 signal."""
    return vault_ai_brief.is_bare(fields, "breasts_vis")


def _is_nude(fields: dict) -> bool:
    """Is she actually wearing nothing?

    Prefers the cheap flags pass over `clothing_state`. The enum was measured
    wrong on ~1 in 3 of the items the $50 tier is built from — it reported
    `fully_nude` for shots with panties still on, and nothing in the stored row
    contradicted it. The flags are answered by looking, so they win outright.

    A NAMED garment settles it before `underwear_visible` is consulted, because
    that flag asks only about UNDERWEAR: a woman in a tank top and a skirt is
    wearing no underwear, so it answers False and she reads as nude. That is
    not a hypothetical — a street selfie in a leather jacket came back nude and
    padded a $100 row as explicit.
    """
    if vault_ai_brief.wearing_something(fields):
        return False
    seen = fields.get("underwear_visible")
    if isinstance(seen, bool):
        return not seen
    return _norm(fields.get("clothing_state")) == "fully_nude"


def _has_payoff_act(fields: dict) -> bool:
    """Something is actually HAPPENING, not just being shown.

    Reads `vault_ai_brief._PAYOFF_ACTS` — the same set that decides whether a
    piece can close a sale on its own — plus any recorded `penetration`. Kept
    separate from every visibility test on purpose: it is the one signal about
    an item that does not depend on the flags pass being right.
    """
    acts = {_norm(a) for a in (fields.get("acts") or []) if _norm(a)}
    if acts & vault_ai_brief._PAYOFF_ACTS:
        return True
    pen = _norm(fields.get("penetration"))
    return bool(pen) and pen not in ("none", "unclear")


def _lane_stories(why: dict, fields: dict, kind: str) -> bool:
    return why["tier"] in ("sfw", "suggestive")


def _lane_mass(why: dict, fields: dict, kind: str) -> bool:
    """Suggestive ONLY. The old suggestive→explicit band swallowed 92 of 103
    items and made every paid item free as well."""
    return why["tier"] == "suggestive"


def _lane_covered_explicit(why: dict, fields: dict, kind: str) -> bool:
    """Tagged explicit, but nothing is actually on show — a top or underwear is
    still on, or she is turned away. The tier says "explicit" because of what the
    scene IS, not because of what is VISIBLE, so this material is safe on a mass
    send or a story even though the heat rung alone would rule it out. It is
    equally not a tease: there is no reveal in it to charge for.

    Gated on the two flags rather than on `body_focus` alone. The earlier version
    required `ass`/`back` in focus as a proxy for "turned away" and tested
    `_shows_breasts` (in frame) instead of `_shows_breasts_bare` (in frame AND
    uncovered) — so a shot with a top still on was thrown out of this lane for
    having breasts in the picture, and landed in no lane at all.

    REQUIRES the flags to exist. This is the only lane that asserts something is
    safe to put in front of everyone, and every other test here reads "no
    evidence of exposure" — which an UNFLAGGED item satisfies trivially. Before
    this guard, AriaFree's two unflagged clips (anal penetration; a dildo) sat in
    this folder, and on V1-described Lexi all 472 members were rows with no
    `body_focus` at all. Absence of evidence is not evidence of absence, and for
    this lane specifically the default must be to exclude.

    The act guard below is deliberately NOT a flags check. This lane admitted
    the same two clips twice, through two different vacuous-truth holes, and
    both times the fix was a sharper reading of what is VISIBLE. A third hole in
    the same shape is likelier than not, so entry also turns on what is
    HAPPENING: a clip of penetration does not become mass-safe because a frame
    was framed tightly. Two independent reasons to exclude, so one being wrong
    is not enough to let it through.
    """
    if not vault_ai_brief.flags_known(fields):
        return False
    if _has_payoff_act(fields):
        return False
    if why["tier"] not in _PAID_TIERS:
        return False
    if vault_ai_brief.is_bare(fields, "anus_vis"):
        return False
    return all(_decently_covered(fields, r) for r in ("vulva_vis", "breasts_vis"))


def _decently_covered(fields: dict, region: str) -> bool:
    """Nothing on show there, AND the reason is clothing or being out of shot.

    `covered` deliberately lumps fabric together with a hand, a thigh and the
    bedsheet, because for "how much skin is on show" they are the same. For
    THIS lane they are not. A woman lying fully nude with her hands over her
    breasts has nothing technically exposed and is still a nude — putting her
    in a mass send is exactly the mistake this folder exists to prevent. A
    woman in a bodysuit is not, and a woman shot from behind is not.

    So: out of frame is fine, clothing over it is fine, a hand over it is not.
    Covered by something the model declined to NAME is also not — this is the
    one lane where "we could not tell you what is covering her" has to read as
    a no.
    """
    state = vault_ai_brief.vis(fields, region)
    if state == "not_in_frame":
        return True
    if state != "covered":
        return False
    return vault_ai_brief.is_clothing(vault_ai_brief.garment_over(fields, region))


def _lane_tease_10(why: dict, fields: dict, kind: str) -> bool:
    """$10 — a real reveal, but she is still a little hidden: breasts out, or
    nude with her pussy not in frame. Images only.

    Still COVERED is the whole point: fully nude is the $50 tier, never this
    one, however little is actually in frame. What remains is the middle band —
    breasts out with her pussy not visible and something still on.

    Content lanes may overlap freely, but the two PRICE tiers must not: an item
    cannot carry two prices.
    """
    if kind != "photo" or why["tier"] not in _PAID_TIERS:
        return False
    if _is_nude(fields):
        return False          # nude is a $50, not a $10
    if _lane_tease_50(why, fields, kind):
        return False
    return _shows_breasts_bare(fields) and not _shows_genitals(fields)


def _lane_tease_50(why: dict, fields: dict, kind: str) -> bool:
    """$50 — the full reveal: fully nude, pussy visible, or a full-body nude.
    Images only.

    Never `lingerie_on`. If she still has lingerie on it is not the full reveal
    whatever else the describe pass tagged — and that tag combination is usually
    a mislabel (panties pulled aside read as "lingerie on"), so trusting it
    would price a partial reveal at $50.
    """
    if kind != "photo" or why["tier"] not in _PAID_TIERS:
        return False
    if vault_ai_brief.vis(fields, "vulva_vis") == "covered":
        return False          # something over her vulva is never the full reveal
    if _norm(fields.get("clothing_state")) == "lingerie_on" and not _is_nude(fields):
        return False
    return (_shows_genitals(fields)
            or "full_body" in _focus(fields)
            or _is_nude(fields))


# (slug, display name, what it is for, predicate(why, fields, kind)). Lanes
# deliberately OVERLAP — a single item can serve several and holds an
# independent position in each.
# The display name is what becomes the FOLDER name, so it is kept short: these
# end up in a dropdown and (once mirrored) in the OF app, where
# "AI-$50 tease · images, fully nude or pussy visible" is unusable. The long
# form lives in `purpose`, which the preview shows underneath.
PURPOSE_LANES: tuple[tuple[str, str, str, Any], ...] = (
    ("stories", "stories",
     "sfw → suggestive — safe enough to post publicly", _lane_stories),
    ("mass", "mass",
     "suggestive only — mass messages and media", _lane_mass),
    ("covered_explicit", "safe explicit",
     "turned away — reads explicit but shows only her back or ass, so it is "
     "safe to mass", _lane_covered_explicit),
    ("tease_10", "tease 10",
     "images — boobs out or still covered, never fully nude (that is the $50)",
     _lane_tease_10),
    ("tease_50", "tease 50",
     "images — fully nude or pussy visible, never with lingerie still on",
     _lane_tease_50),
)


async def collect_purpose_folders(
    account_id: str, *, media_ids: Iterable[int] | None = None,
) -> list[dict[str, Any]]:
    """Re-cut a pool of media into the send lanes, by heat rather than outfit.

    `media_ids` restricts the pool — pass the items of the shoots you did NOT
    keep, so strong scripts stay intact as shoots and only the leftovers get
    repurposed. Omit it to consider the whole vault.

    Read-only; an item may legitimately appear in more than one lane.
    """
    rows = await _load_rows(account_id)
    if media_ids is not None:
        wanted = {int(m) for m in media_ids}
        rows = [r for r in rows if r["media_id"] in wanted]

    enriched = [{**r, "why": send_order_reason(r.get("fields") or {})} for r in rows]

    out: list[dict[str, Any]] = []
    for slug, name, purpose, matches in PURPOSE_LANES:
        picked = [r for r in enriched
                  if matches(r["why"], r.get("fields") or {}, r["kind"])]
        # No MIN_SCRIPT_ITEMS here: that floor exists because a 2-item SHOOT is
        # a pair, not a script. A lane is a bucket — two story-safe stills is a
        # perfectly good Stories folder, and suppressing it would silently hide
        # material the operator can post.
        if not picked:
            continue
        picked.sort(key=lambda r: send_sort_key(
            r["fields"], script_id=r.get("script_id"),
            script_seq=r.get("script_seq"), media_id=r["media_id"]))
        kinds: dict[str, int] = {}
        tier_counts: dict[str, int] = {}
        for r in picked:
            kinds[r["kind"]] = kinds.get(r["kind"], 0) + 1
            tier_counts[r["why"]["tier"]] = tier_counts.get(r["why"]["tier"], 0) + 1
        out.append({
            "lane": slug, "name": ai_folder_name(name), "purpose": purpose,
            "size": len(picked), "kinds": kinds, "tiers": tier_counts,
            "closes_on_own": sum(1 for r in picked if r["why"]["closes"]),
            "items": [{**r, "manual_order": i + 1}
                      for i, r in enumerate(picked)],
        })
    return out


# ── The whole plan: what the vault button previews and creates ──────
#
# One call so the UI previews EXACTLY what it will create. `apply_ai_folders`
# deliberately re-derives the plan server-side instead of trusting a posted list
# of media ids: the preview an operator looked at may be minutes old, and the
# vault can shift underneath it (a collect sweep, a duplicate hidden). Re-deriving
# means the worst case is "you got the current answer", not "you got a stale one
# written as fact".

async def flags_coverage(account_id: str) -> dict[str, Any]:
    """How many of the account's stills carry the exposure flags.

    `ready` is False when ANY still is missing them: the paid tiers silently
    fall back to `clothing_state` for those items, which is the unreliable
    signal the flags exist to replace.
    """
    async with get_session() as s:
        rows = (await s.execute(
            select(VaultItem.ai_fields_json).where(
                VaultItem.account_id == account_id,
                VaultItem.removed_at.is_(None),
                VaultItem.kind == "photo")
        )).all()
    stills = len(rows)
    flagged = 0
    for (fj,) in rows:
        fields = vault_ai_brief.load_fields(fj)
        if vault_ai_brief.flags_known(fields):
            flagged += 1
    return {
        "stills": stills, "flagged": flagged,
        "missing": stills - flagged,
        "ready": stills > 0 and flagged == stills,
    }


async def plan_ai_folders(
    account_id: str, *, keep: int = 2,
    window_hours: int = DEFAULT_OUTFIT_WINDOW_HOURS,
    min_items: int = MIN_SCRIPT_ITEMS,
) -> dict[str, Any]:
    """Every folder the pipeline proposes, scripts and lanes together.

    `keep` — how many of the top-ranked shoots stay as their own script folder.
    Lanes are always cut from the WHOLE vault, not just the shoots that were not
    kept: the explicit material a paid tease needs lives inside the good shoots,
    so restricting them to leftovers empties those lanes. An item belonging to
    both a script and a lane is the design.
    """
    shoots = await collect_outfit_scripts(
        account_id, window_hours=window_hours, min_items=min_items)
    lanes = await collect_purpose_folders(account_id)

    def _folder(name: str, source: str, purpose: str,
                items: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
        kinds: dict[str, int] = {}
        tiers: dict[str, int] = {}
        for r in items:
            kinds[r["kind"]] = kinds.get(r["kind"], 0) + 1
            tiers[r["why"]["tier"]] = tiers.get(r["why"]["tier"], 0) + 1
        return {
            "name": name, "source": source, "purpose": purpose,
            "size": len(items), "kinds": kinds, "tiers": tiers,
            "closes_on_own": sum(1 for r in items if r["why"]["closes"]),
            "items": [
                {"media_id": r["media_id"], "kind": r["kind"],
                 "manual_order": r["manual_order"], "score": r.get("score"),
                 "tier": r["why"]["tier"], "closes": r["why"]["closes"],
                 "clothing_state": r["why"]["clothing_state"]}
                for r in items
            ],
            **extra,
        }

    folders = [
        _folder(s["name"], "script", f"one shoot — {s['outfit']}", s["items"],
                outfit=s["outfit"], mixed=s["mixed"])
        for s in shoots[:max(0, keep)]
    ] + [
        _folder(f["name"], "lane", f["purpose"], f["items"], lane=f["lane"])
        for f in lanes
    ]

    unique = {r["media_id"] for f in folders for r in f["items"]}

    # How much of this account has been through the cheap flags pass. Without
    # it `_is_nude()` falls back to `clothing_state`, which was measured wrong
    # on ~1 in 3 of the stills the $50 tier is built from — so the folders would
    # come out quietly wrong rather than visibly broken. The caller surfaces
    # this so an operator is never one click from that.
    coverage = await flags_coverage(account_id)

    return {
        "account_id": account_id,
        "keep": keep,
        "shoots_found": len(shoots),
        "flags": coverage,
        "folders": folders,
        "summary": {
            "folders": len(folders),
            "scripts": sum(1 for f in folders if f["source"] == "script"),
            "lanes": sum(1 for f in folders if f["source"] == "lane"),
            "unique_media": len(unique),
            "memberships": sum(f["size"] for f in folders),
        },
    }


async def apply_ai_folders(account_id: str, plan: dict[str, Any]) -> dict[str, Any]:
    """Create the planned folders and fill them, in send order.

    Re-runnable: a folder whose `AI-` name already exists is REUSED and its
    membership rewritten, so pressing the button twice refreshes rather than
    piling up duplicates. Only `AI-`-prefixed folders are ever touched — a
    folder an operator made by hand is never rewritten, even on a name clash.

    Internal folders only. No OF writes, nothing sent.
    """
    from db.models import VaultFolder  # lazy: keeps the import graph flat

    created: list[dict[str, Any]] = []
    async with get_session() as s:
        for spec in plan.get("folders") or []:
            name = ai_folder_name(spec["name"])
            existing = (await s.execute(
                select(VaultFolder).where(
                    VaultFolder.account_id == account_id,
                    VaultFolder.name == name,
                    VaultFolder.deleted_at.is_(None),
                )
            )).scalars().first()

            if existing is not None:
                folder_id = existing.id
                reused = True
                # Wipe only THIS folder's membership so the rewrite is clean;
                # the media itself and every other folder are untouched.
                await s.execute(
                    VaultFolderItem.__table__.delete().where(
                        (VaultFolderItem.account_id == account_id)
                        & (VaultFolderItem.folder_id == folder_id)
                    )
                )
            else:
                folder = VaultFolder(account_id=account_id, name=name,
                                     created_by="vault_ai_script")
                s.add(folder)
                await s.flush()
                folder_id = folder.id
                reused = False

            for entry in spec.get("items") or []:
                s.add(VaultFolderItem(
                    account_id=account_id, folder_id=folder_id,
                    media_id=int(entry["media_id"]),
                    manual_order=int(entry["manual_order"]),
                ))
            created.append({"folder_id": folder_id, "name": name,
                            "items": len(spec.get("items") or []),
                            "reused": reused, "source": spec.get("source")})

        # Retire generated folders the CURRENT plan no longer produces. Without
        # this, every rule or naming change leaves its output behind and the
        # folder list silently accumulates two generations of the same idea.
        #
        # Scoped hard: only rows this module created (`created_by`) AND carrying
        # the AI- prefix are ever retired, and only by soft-delete. An operator's
        # own folder cannot be reached by this even if it collides on name.
        wanted = {c["name"] for c in created}
        stale = (await s.execute(
            select(VaultFolder).where(
                VaultFolder.account_id == account_id,
                VaultFolder.created_by == "vault_ai_script",
                VaultFolder.deleted_at.is_(None),
            )
        )).scalars().all()
        retired: list[dict[str, Any]] = []
        for folder in stale:
            if folder.name in wanted or not folder.name.startswith(AI_FOLDER_PREFIX):
                continue
            folder.deleted_at = datetime.utcnow()
            retired.append({"folder_id": folder.id, "name": folder.name,
                            "of_list_id": folder.of_list_id})
        await s.commit()

    log.info("AI folders applied account=%s folders=%d items=%d retired=%d",
             account_id, len(created), sum(c["items"] for c in created), len(retired))
    return {
        "created": created,
        "folders": len(created),
        "new": sum(1 for c in created if not c["reused"]),
        "reused": sum(1 for c in created if c["reused"]),
        "items": sum(c["items"] for c in created),
        # Retired locally only. Any OF list these were mirrored to is left
        # alone: deleting a folder on her real account is not something a
        # rename in our rules should trigger.
        "retired": retired,
    }


# ── Collecting scripts into proposed folders ────────────────────────
#
# A "script" is one shoot: media uploaded in the same burst, put back into the
# order it was filmed in (which often means undoing OF's reversal). Turning that
# into a folder is an ACTION, so it goes through the review queue rather than
# happening: `collect_scripts` only proposes, `queue_script_folders` writes
# `pending` rows, and NOTHING creates a folder until an operator approves and
# `apply_approved_script_folders` runs. Approval is not mutation — that split is
# the whole point of the review contract, and it is what makes a wrong grouping
# a discarded row instead of 40 photos in the wrong folder.
#
# Items inside a proposed folder are ordered by `send_sort_key`, NOT by filmed
# order, so the folder reads the same way `plan_folder_order` would rewrite it
# later. Filmed order still matters — it is the tiebreak inside that key, and it
# is only correct because the batch's direction was detected first.

def _script_name(items: list[dict[str, Any]], started: datetime | None) -> str:
    """`Script 2026-07-02 · solo_fingering (8)` — the date the shoot went up
    plus its dominant `primary_folder`, which is the only content label the
    describe pass actually commits to."""
    counts: dict[str, int] = {}
    for r in items:
        pf = _norm((r.get("fields") or {}).get("primary_folder"))
        if pf and pf != "other":
            counts[pf] = counts.get(pf, 0) + 1
    topic = max(counts, key=lambda k: counts[k]) if counts else "mixed"
    day = started.strftime("%Y-%m-%d") if started else "undated"
    return ai_folder_name(f"Script {day} · {topic} ({len(items)})")


async def collect_scripts(account_id: str, *,
                          gap_seconds: int = DEFAULT_GAP_SECONDS,
                          min_items: int = MIN_SCRIPT_ITEMS,
                          ) -> list[dict[str, Any]]:
    """Read-only. Every upload burst big enough to be a shoot, as a PROPOSED
    folder with its items already in send order. Creates nothing."""
    plan = await plan_scripts(account_id, gap_seconds=gap_seconds)

    out: list[dict[str, Any]] = []
    for s in plan["scripts"]:
        items = s["items"]
        if len(items) < min_items:
            continue
        enriched = []
        for r in items:
            fields = r.get("fields") or {}
            enriched.append({**r, "fields": fields,
                             "why": send_order_reason(fields)})
        placed = [r for r in enriched if r["why"]["rung"] is not None]
        unplaced = [r for r in enriched if r["why"]["rung"] is None]
        placed.sort(key=lambda r: send_sort_key(
            r["fields"], script_id=r.get("script_id"),
            script_seq=r.get("script_seq"), media_id=r["media_id"]))
        ordered = placed + unplaced

        out.append({
            "script_id": s["script_id"],
            "name": _script_name(ordered, s["started_at"]),
            "direction": s["direction"], "reason": s["reason"], "tau": s["tau"],
            "kinds": s["kinds"], "started_at": s["started_at"],
            "size": len(ordered),
            "closes_on_own": sum(1 for r in ordered if r["why"]["closes"]),
            "items": [{**r, "manual_order": i + 1}
                      for i, r in enumerate(ordered)],
        })
    return out


async def queue_script_folders(account_id: str, proposals: list[dict[str, Any]]
                               ) -> dict[str, Any]:
    """Write each proposal as a PENDING review row. Still creates no folder.

    The baseline snapshots the media hashes the grouping was derived from plus
    the target folder name, so if the vault shifts underneath (an item hidden as
    a duplicate, a folder of that name created by hand) the approve path refuses
    it as `stale` instead of applying a stale grouping.
    """
    from db.models import VaultAiReviewItem  # lazy: keeps the import graph flat
    import vault_ai_baseline as vab

    queued: list[int] = []
    async with get_session() as s:
        for p in proposals:
            media_ids = [r["media_id"] for r in p["items"]]
            skeleton = {
                "media_hashes": {str(m): None for m in media_ids},
                "folder_snapshot": {p["name"]: []},
            }
            baseline = await vab.current_baseline_view(s, account_id, skeleton)
            row = VaultAiReviewItem(
                account_id=account_id, kind="folder", status="pending",
                payload_json=json.dumps({
                    "source": "script",
                    "script_id": p["script_id"],
                    "folder_name": p["name"],
                    "direction": p["direction"],
                    "reversed": p["direction"] == "reverse",
                    "order": [{"media_id": r["media_id"],
                               "manual_order": r["manual_order"]}
                              for r in p["items"]],
                }, default=str),
                baseline_json=json.dumps(baseline, default=str),
            )
            s.add(row)
            await s.flush()
            queued.append(row.id)
        await s.commit()
    log.info("queued %d script-folder proposals account=%s", len(queued), account_id)
    return {"queued": queued, "count": len(queued)}


async def apply_approved_script_folders(account_id: str) -> dict[str, Any]:
    """Create the folders for review rows an operator has APPROVED.

    Only touches rows that are `kind='folder'`, `status='approved'` and carry
    our `source='script'` marker — another producer's folder proposals are left
    alone. Creates the internal folder, adds its media, writes `manual_order`
    per folder, then marks the row `applied`. No OF writes: internal folders are
    ours until someone explicitly mirrors them.
    """
    from db.models import VaultAiReviewItem, VaultFolder  # lazy

    created: list[dict[str, Any]] = []
    async with get_session() as s:
        rows = (await s.execute(
            select(VaultAiReviewItem).where(
                VaultAiReviewItem.account_id == account_id,
                VaultAiReviewItem.kind == "folder",
                VaultAiReviewItem.status == "approved",
            ).order_by(VaultAiReviewItem.id.asc())
        )).scalars().all()

        for row in rows:
            try:
                payload = json.loads(row.payload_json or "{}")
            except (TypeError, ValueError):
                continue
            if payload.get("source") != "script":
                continue  # not ours

            folder = VaultFolder(
                account_id=account_id,
                name=ai_folder_name(payload.get("folder_name") or "Script"),
                created_by="vault_ai_script")
            s.add(folder)
            await s.flush()

            for entry in payload.get("order") or []:
                s.add(VaultFolderItem(
                    account_id=account_id, folder_id=folder.id,
                    media_id=int(entry["media_id"]),
                    manual_order=int(entry["manual_order"]),
                ))
            row.status = "applied"
            row.resolved_at = datetime.utcnow()
            created.append({"review_id": row.id, "folder_id": folder.id,
                            "name": folder.name,
                            "items": len(payload.get("order") or [])})
        await s.commit()
    log.info("applied %d approved script folders account=%s", len(created), account_id)
    return {"created": created, "count": len(created)}


async def apply_scripts(account_id: str, plan: dict[str, Any]) -> dict[str, int]:
    """Persist a plan's `script_id` / `script_seq` / `script_score` onto the
    mirror so callers can ORDER BY it. Writes only our own columns — the OF
    vault is untouched, and a re-plan simply overwrites."""
    written = 0
    async with get_session() as s:
        for script in plan.get("scripts") or []:
            for item in script["items"]:
                await s.execute(
                    update(VaultItem)
                    .where(VaultItem.account_id == account_id,
                           VaultItem.media_id == item["media_id"])
                    .values(script_id=script["script_id"],
                            script_seq=item["script_seq"],
                            script_score=item["score"],
                            script_reversed=(script["direction"] == "reverse"))
                )
                written += 1
        await s.commit()
    log.info("vault scripts applied account=%s batches=%d items=%d",
             account_id, len(plan.get("scripts") or []), written)
    return {"scripts": len(plan.get("scripts") or []), "items": written}
