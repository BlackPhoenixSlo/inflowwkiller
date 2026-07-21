"""Canonical baseline (staleness) computation — the ONE implementation shared by
the producer (`vault_ai_service`) and the review API (`vault_ai_api`).

Why this exists: a review item stores a `baseline_json` snapshot of the world it
was proposed against; on approve the API recomputes that snapshot and refuses the
approval as `stale` if anything moved. For that to work, the producer must store
*exactly* what the checker recomputes when nothing has changed. The first
integration bug was precisely a drift here — the producer hashed the config with
sha1[:12] of the merged dict while the checker used sha256[:16] of the raw column,
so every approval came back `stale` and nothing could ever be approved. Keeping a
single function both call makes that class of bug impossible.

Contract §2 baseline shape (keys are OPTIONAL — a producer includes only the keys
it wants staleness-checked; the checker recomputes only the keys present):
    { "config_version": <str>,
      "media_hashes":   {media_id: content-hash-or-None},
      "folder_snapshot":{folder_name: [media_id, ...]} }

Producers snapshot "now" by calling `current_baseline_view` with a skeleton whose
values are ignored (only the keys/ids matter); the checker calls it with the
stored baseline. Same function ⇒ same result ⇒ no drift.
"""
from __future__ import annotations

import hashlib
from typing import Any

from sqlalchemy import select

from db.models import AccountAiConfig, VaultFolder, VaultFolderItem, VaultItem


def config_version(raw_config_json: str | None) -> str:
    """Canonical config fingerprint: sha256 of the RAW stored
    `vault_ai_config_json` column value, first 16 hex chars. Empty string when
    no config row exists, so a config-less account is stable (not perpetually
    stale)."""
    return hashlib.sha256((raw_config_json or "").encode("utf-8")).hexdigest()[:16]


async def current_baseline_view(
    session, account_id: str, stored: dict[str, Any],
) -> dict[str, Any]:
    """Recompute ONLY the keys present in `stored`, from the CURRENT world, so
    producer-write and checker-read are byte-identical when nothing changed."""
    out: dict[str, Any] = {}

    if "config_version" in stored:
        cfg = await session.get(AccountAiConfig, account_id)
        raw = (cfg.vault_ai_config_json if cfg else None) or ""
        out["config_version"] = config_version(raw)

    stored_hashes = stored.get("media_hashes")
    if isinstance(stored_hashes, dict):
        want_ids: list[int] = []
        for k in stored_hashes.keys():
            try:
                want_ids.append(int(k))
            except (TypeError, ValueError):
                continue
        got: dict[int, str | None] = {}
        if want_ids:
            rows = (
                await session.execute(
                    select(
                        VaultItem.media_id, VaultItem.content_hash,
                        VaultItem.updated_at_of, VaultItem.removed_at,
                    ).where(
                        VaultItem.account_id == account_id,
                        VaultItem.media_id.in_(want_ids),
                    )
                )
            ).all()
            for mid, chash, uof, removed in rows:
                got[int(mid)] = None if removed is not None else (chash or uof or "")
        out["media_hashes"] = {str(mid): got.get(mid) for mid in want_ids}

    stored_snap = stored.get("folder_snapshot")
    if isinstance(stored_snap, dict):
        snap: dict[str, list[int]] = {}
        for name in stored_snap.keys():
            folder = (
                await session.execute(
                    select(VaultFolder).where(
                        VaultFolder.account_id == account_id,
                        VaultFolder.name == name,
                        VaultFolder.deleted_at.is_(None),
                    )
                )
            ).scalars().first()
            if folder is None:
                snap[str(name)] = []
                continue
            ids = [
                int(r[0]) for r in (
                    await session.execute(
                        select(VaultFolderItem.media_id).where(
                            VaultFolderItem.account_id == account_id,
                            VaultFolderItem.folder_id == folder.id,
                        )
                    )
                ).all()
            ]
            snap[str(name)] = ids
        out["folder_snapshot"] = snap

    return out
