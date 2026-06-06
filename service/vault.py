"""
vault.py — DB-backed read endpoints for vault items, presets, and saved
replies. Per T-B5 these are cache-first: vault content changes rarely
enough that serving from SQLite (mirrored by the WS transcoder + the OF
import paths) gives the UI a fast, paginated read without burning OF
rate-limit budget.

The OF-passthrough path at /api/of/v2/vault/* stays intact — switching it
to DB-first is a separate decision pending SSE reliability measurement
(plan 07 §2). These admin routes are additive.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Query
from sqlalchemy import select

from db.engine import get_session
from db.models import SavedReply, VaultItem, VaultPreset
from auth import assert_account_owned

log = logging.getLogger("of-relay.vault")
router = APIRouter()


def _vault_item_to_dict(v: VaultItem) -> dict[str, Any]:
    return {
        "account_id": v.account_id,
        "media_id": v.media_id,
        "kind": v.kind,
        "duration_seconds": v.duration_seconds,
        "width": v.width,
        "height": v.height,
        "thumb_url": v.thumb_url,
        "full_url": v.full_url,
        "folder_id": v.folder_id,
        "description": v.description,
        "suggested_price_cents": v.suggested_price_cents,
        "default_price_cents": v.default_price_cents,
        "tags": _safe_load_list(v.tags),
        "notes": v.notes,
        "send_count": v.send_count,
        "last_sent_at": v.last_sent_at.isoformat() if v.last_sent_at else None,
        "created_at": v.created_at.isoformat() if v.created_at else None,
    }


def _vault_preset_to_dict(p: VaultPreset) -> dict[str, Any]:
    return {
        "id": p.id,
        "name": p.name,
        "folder_name": p.folder_name,
        "index_in_folder": p.index_in_folder,
        "index_strategy": p.index_strategy,
        "notes": p.notes,
        "created_at": p.created_at.isoformat() if p.created_at else None,
    }


def _saved_reply_to_dict(r: SavedReply) -> dict[str, Any]:
    return {
        "id": r.id,
        "account_id": r.account_id,
        "title": r.title,
        "text": r.text,
        "price_cents": r.price_cents,
        "locked_text": bool(r.locked_text),
        "media": _safe_load_list(r.media_json),
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "updated_at": r.updated_at.isoformat() if r.updated_at else None,
    }


def _safe_load_list(raw: str | None) -> list[Any]:
    if not raw:
        return []
    try:
        v = json.loads(raw)
        return v if isinstance(v, list) else []
    except (ValueError, TypeError):
        return []


@router.get("/admin/vault/items")
async def list_vault_items(
    account_id: str = Query(...),
    folder: str | None = Query(
        None,
        description="Optional folder_id (BIGINT). Non-numeric values match no rows.",
    ),
    limit: int = Query(200, ge=1, le=500),
) -> dict[str, Any]:
    """Vault items for one account, newest first. Uses
    `ix_vault_account_created` for the ORDER BY scan."""
    assert_account_owned(account_id)
    folder_id: int | None
    if folder is None or folder == "":
        folder_id = None
    else:
        try:
            folder_id = int(folder)
        except ValueError:
            return {"items": []}

    async with get_session() as s:
        q = (
            select(VaultItem)
            .where(VaultItem.account_id == account_id)
            .order_by(VaultItem.created_at.desc())
            .limit(limit)
        )
        if folder_id is not None:
            q = q.where(VaultItem.folder_id == folder_id)
        rows = (await s.execute(q)).scalars().all()
        return {"items": [_vault_item_to_dict(r) for r in rows]}


@router.get("/admin/vault/presets")
async def list_vault_presets() -> dict[str, Any]:
    """All vault presets. Presets are model-agnostic (resolved per-model at
    send time), so no account filter."""
    async with get_session() as s:
        rows = (
            await s.execute(select(VaultPreset).order_by(VaultPreset.name))
        ).scalars().all()
        return {"presets": [_vault_preset_to_dict(r) for r in rows]}


@router.get("/admin/vault/saved-replies")
async def list_vault_saved_replies(account_id: str = Query(...)) -> dict[str, Any]:
    """Saved replies for one account, most-recently-edited first. Mirrors
    the shape of /admin/saved-replies (which also handles writes) — this
    path lives under /admin/vault/* to match the picker UI grouping."""
    assert_account_owned(account_id)
    async with get_session() as s:
        rows = (
            await s.execute(
                select(SavedReply)
                .where(SavedReply.account_id == account_id)
                .order_by(SavedReply.updated_at.desc())
            )
        ).scalars().all()
        return {"saved_replies": [_saved_reply_to_dict(r) for r in rows]}
