"""service/vault_dupes.py — duplicate / re-upload detection over the vault mirror.

Re-uploads pile up in a creator's vault: on Lexi 706 of 2,309 items (30.6%) are
copies of something already there. Every copy costs a describe call, clutters the
picker, and splits a folder's ordering. This module finds them; hiding them is a
separate, operator-confirmed step (`vault_ai_api` → `of_client.hide_vault_media`).

Signals, all computed from the ALREADY-CACHED 300px thumbs on disk
(`vault_ai_api._thumb_path`) — no OF traffic, no LLM, ~2s for a 2.3k-item vault:

  md5    byte-identical thumb. Never a false positive.
  dHash  64-bit gradient hash — survives the re-encode OF applies to a re-upload.
  aHash  64-bit mean hash. Required to agree as well: dHash alone matches on
         layout, so two frames of one burst can collide; the second independent
         hash is what keeps them apart.

Clustering is SEED-BASED, never transitive. Items are walked oldest-first; each
unclaimed item becomes a seed (the ORIGINAL — earliest OF `createdAt`) and later
items join only if they are within threshold of THAT SEED. Union-find chaining
(a~b, b~c ⇒ a~c) would let a cluster drift away from its original, which is the
one failure that could hide a photo the operator meant to keep.

Threshold: default 2, and that is not a compromise. Measured on Lexi, the match
curve is flat past 2 — ≤0 finds 644 copies, ≤2 finds 706, ≤8 finds only 737 — so
loosening buys ~4% more at the cost of dragging in every "same shoot, different
frame" near-miss. All 199 sets at ≤2 were operator-confirmed correct (2026-07-21).

Videos additionally require a duration match (±1s) before their thumbs are
compared at all: two clips can share a poster frame and be different edits.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from pathlib import Path
from typing import Any

from sqlalchemy import select, update

from db.engine import get_session
from db.models import VaultItem

log = logging.getLogger("of-relay.vault_dupes")

# Bands the review UI colour-codes. Distance 0 on BOTH hashes is visually
# indistinguishable at thumb resolution; the higher bands are where an operator
# actually has to look, which is why the confirm screen shows them separately.
BANDS: tuple[tuple[int, int, str], ...] = (
    (0, 0, "identical"),
    (1, 2, "near"),
    (3, 5, "similar"),
    (6, 8, "loose"),
)

DEFAULT_THRESHOLD = 2
MAX_THRESHOLD = 8
HASH_KIND = "thumb/md5+dhash8+ahash8"
# Videos whose durations differ by more than this are never compared.
_VIDEO_DUR_TOLERANCE_S = 1


def band_of(distance: int) -> str:
    for lo, hi, name in BANDS:
        if lo <= distance <= hi:
            return name
    return "far"


# ── Hashing ─────────────────────────────────────────────────────────
#
# Pillow is imported lazily, INSIDE the functions that need it. It is a
# capture-host dep that requirements.txt deliberately keeps out of the relay
# image, and a module-level `from PIL import Image` here crash-looped the whole
# relay on deploy — every send down because a duplicate-finder could not load an
# image library. A vault helper must never be able to do that: if Pillow is
# absent, `fingerprint_file` returns None and only dupe detection goes quiet.


def dhash(img: "Image.Image") -> int:
    """64-bit gradient hash: each bit = "is this pixel darker than its right
    neighbour". Insensitive to the brightness/contrast shift a re-encode adds."""
    from PIL import Image
    g = img.convert("L").resize((9, 8), Image.LANCZOS)
    px = list(g.getdata())
    bits = 0
    for r in range(8):
        row = px[r * 9:(r + 1) * 9]
        for c in range(8):
            bits = (bits << 1) | (1 if row[c] < row[c + 1] else 0)
    return bits


def ahash(img: "Image.Image") -> int:
    """64-bit mean hash: each bit = "is this pixel above the image mean"."""
    from PIL import Image
    g = img.convert("L").resize((8, 8), Image.LANCZOS)
    px = list(g.getdata())
    avg = sum(px) / 64.0
    bits = 0
    for p in px:
        bits = (bits << 1) | (1 if p > avg else 0)
    return bits


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def pack_hash(md5: str, dh: int, ah: int) -> str:
    """The single `content_hash` string persisted on the row. Self-describing so
    a future hash version can be told apart (`content_hash_kind`)."""
    return f"{md5}:{dh:016x}:{ah:016x}"


def unpack_hash(packed: str | None) -> tuple[str, int, int] | None:
    if not packed:
        return None
    parts = packed.split(":")
    if len(parts) != 3:
        return None
    try:
        return parts[0], int(parts[1], 16), int(parts[2], 16)
    except ValueError:
        return None


def fingerprint_file(path: Path) -> tuple[str, int, int] | None:
    """(md5, dhash, ahash) for a cached thumb. None if missing/unreadable — a
    truncated cache file must not take down a whole scan."""
    try:
        from PIL import Image
        raw = path.read_bytes()
        with Image.open(path) as im:
            im.load()
            return hashlib.md5(raw).hexdigest(), dhash(im), ahash(im)
    except Exception:  # noqa: BLE001
        return None


# ── Persistence ─────────────────────────────────────────────────────

async def ensure_hashes(account_id: str, *, force: bool = False) -> dict[str, int]:
    """Fill `content_hash` for every mirrored item whose thumb is cached.

    Incremental: rows that already carry a current-kind hash are skipped unless
    `force`, so a re-scan after a collect only hashes the NEW media. Runs the
    (CPU-bound, ~1ms/item) Pillow work in a thread so it can't stall the relay's
    event loop.
    """
    from vault_ai_api import _thumb_path  # lazy: avoid an import cycle

    async with get_session() as s:
        rows = (await s.execute(
            select(VaultItem.media_id, VaultItem.content_hash,
                   VaultItem.content_hash_kind)
            .where(VaultItem.account_id == account_id,
                   VaultItem.removed_at.is_(None))
        )).all()

    todo = [int(mid) for mid, h, kind in rows
            if force or not h or kind != HASH_KIND]
    if not todo:
        return {"hashed": 0, "skipped": len(rows), "missing_thumb": 0}

    def _work() -> list[tuple[int, str]]:
        out: list[tuple[int, str]] = []
        for mid in todo:
            fp = fingerprint_file(_thumb_path(account_id, mid))
            if fp is not None:
                out.append((mid, pack_hash(*fp)))
        return out

    computed = await asyncio.to_thread(_work)
    async with get_session() as s:
        for mid, packed in computed:
            await s.execute(
                update(VaultItem)
                .where(VaultItem.account_id == account_id,
                       VaultItem.media_id == mid)
                .values(content_hash=packed, content_hash_kind=HASH_KIND)
            )
        await s.commit()
    return {"hashed": len(computed), "skipped": len(rows) - len(todo),
            "missing_thumb": len(todo) - len(computed)}


# ── Clustering ──────────────────────────────────────────────────────

def cluster(items: list[dict], threshold: int) -> list[dict]:
    """Seed-based grouping, oldest-first. Returns sets of >= 2 where `original`
    is the earliest-uploaded item and `dupes` are the removable copies."""
    items = sorted(items, key=lambda r: (r["created_at"], r["media_id"]))
    claimed: set[int] = set()
    clusters: list[dict] = []

    for i, seed in enumerate(items):
        if seed["media_id"] in claimed:
            continue
        dupes: list[dict] = []
        for cand in items[i + 1:]:
            if cand["media_id"] in claimed or cand["kind"] != seed["kind"]:
                continue
            if seed["kind"] == "video":
                if abs((seed["duration"] or 0) - (cand["duration"] or 0)) > \
                        _VIDEO_DUR_TOLERANCE_S:
                    continue
            if cand["md5"] == seed["md5"]:
                dd = da = 0
                exact = True
            else:
                dd = hamming(seed["dhash"], cand["dhash"])
                if dd > threshold:
                    continue
                da = hamming(seed["ahash"], cand["ahash"])
                if da > threshold:
                    continue
                exact = False
            dupes.append({**cand, "dhash_dist": dd, "ahash_dist": da,
                          "exact": exact})
        if not dupes:
            continue
        claimed.add(seed["media_id"])
        claimed.update(d["media_id"] for d in dupes)
        worst = max(max(d["dhash_dist"], d["ahash_dist"]) for d in dupes)
        clusters.append({
            "original": seed, "dupes": dupes, "worst": worst,
            "band": band_of(worst),
            "all_exact": all(d["exact"] for d in dupes),
        })
    clusters.sort(key=lambda c: (-len(c["dupes"]), c["worst"]))
    return clusters


async def load_fingerprints(account_id: str) -> tuple[list[dict], int]:
    """Every mirrored item that carries a usable hash, + the count that doesn't
    (no cached thumb yet — those are simply not candidates)."""
    async with get_session() as s:
        rows = (await s.execute(
            select(VaultItem.media_id, VaultItem.kind, VaultItem.created_at,
                   VaultItem.duration_seconds, VaultItem.content_hash,
                   VaultItem.send_count)
            .where(VaultItem.account_id == account_id,
                   VaultItem.removed_at.is_(None))
        )).all()
    items: list[dict] = []
    unhashed = 0
    for mid, kind, created, dur, packed, sends in rows:
        fp = unpack_hash(packed)
        if fp is None:
            unhashed += 1
            continue
        md5, dh, ah = fp
        items.append({"media_id": int(mid), "kind": kind, "created_at": created,
                      "duration": dur, "md5": md5, "dhash": dh, "ahash": ah,
                      "send_count": int(sends or 0)})
    return items, unhashed


async def find_duplicates(account_id: str, threshold: int = DEFAULT_THRESHOLD,
                          *, rehash: bool = True) -> dict[str, Any]:
    """Full scan: (re)hash new media, then cluster. The result feeds the review
    UI — nothing is hidden here."""
    threshold = max(0, min(MAX_THRESHOLD, int(threshold)))
    hashed = await ensure_hashes(account_id) if rehash else {}
    items, unhashed = await load_fingerprints(account_id)
    clusters = await asyncio.to_thread(cluster, items, threshold)
    return {
        "threshold": threshold,
        "scanned": len(items),
        "unhashed": unhashed,
        "sets": len(clusters),
        "removable": sum(len(c["dupes"]) for c in clusters),
        "hashed": hashed,
        "clusters": clusters,
    }
