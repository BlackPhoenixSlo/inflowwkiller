"""service/tip_reward_config_api.py — read/write account_ai_config.tip_reward_config_json.

The `tip_reward` automation (send vault images when a fan tips) is gated + tuned
per-account by this JSON. The Automations "Tip Reward" tab persists it through
these owner-gated routes.

  GET /admin/tip-reward-config?account_id=  → {config, defaults}
  PUT /admin/tip-reward-config              → upsert the JSON, returns {config}

Default (absent/NULL) → DISABLED with empty folders until enabled + configured here.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from auth import assert_account_owned
from db.engine import get_session
from db.models import AccountAiConfig
from automations.tip_reward import _DEFAULTS

log = logging.getLogger("of-relay.tip_reward_config_api")

router = APIRouter()

# (key, min, max) — clamp every numeric knob so a typo can't drain a folder.
_INT_KNOBS = {
    "dollars_per_image": (1, 10_000),
    "min_images": (0, 50),
    "max_images": (1, 50),
    "window_hours": (1, 8760),
    # The content-ask tip-ask amount (read by of_ai_chat/autoreply — the ASK side
    # of the loop). Kept here so the whole tip loop has one config surface.
    "ask_amount_dollars": (1, 10_000),
}
_MAX_TIERS = 10
_MAX_FOLDERS_PER_TIER = 25
_CAPTION_MAX = 500
_ASK_TEMPLATE_MAX = 300


def _validate_tier(t: Any) -> dict:
    if not isinstance(t, dict):
        raise HTTPException(422, "each tier must be an object")
    name = str(t.get("name") or "").strip()[:40]
    try:
        min_basis = max(0, int(t.get("min_basis_cents") or 0))
    except (TypeError, ValueError):
        raise HTTPException(422, "tier min_basis_cents must be a number")
    raw_folders = t.get("folders") or []
    if not isinstance(raw_folders, (list, tuple)):
        raise HTTPException(422, "tier folders must be a list")
    folders: list[str] = []
    for f in raw_folders:
        nm = str(f or "").strip()
        if nm and nm not in folders:
            folders.append(nm)
        if len(folders) >= _MAX_FOLDERS_PER_TIER:
            break
    return {"name": name, "min_basis_cents": min_basis, "folders": folders}


def _validate(cfg: dict) -> dict:
    if not isinstance(cfg, dict):
        raise HTTPException(422, "config must be an object")
    out: dict[str, Any] = {}
    if "enabled" in cfg:
        out["enabled"] = bool(cfg["enabled"])
    # The content-ask tip-ask toggle (ASK side). Independent of `enabled` (the
    # delivery side) — the ask can be on while reward delivery is off, or vice versa.
    if "ask_enabled" in cfg:
        out["ask_enabled"] = bool(cfg["ask_enabled"])
    for k, (lo, hi) in _INT_KNOBS.items():
        if k in cfg and cfg[k] is not None:
            try:
                out[k] = max(lo, min(int(cfg[k]), hi))
            except (TypeError, ValueError):
                raise HTTPException(422, f"{k} must be a number")
    if "caption" in cfg:
        out["caption"] = str(cfg["caption"] or "")[:_CAPTION_MAX]
    if "ask_template" in cfg:
        out["ask_template"] = str(cfg["ask_template"] or "")[:_ASK_TEMPLATE_MAX]
    if "tiers" in cfg:
        tiers = cfg["tiers"]
        if not isinstance(tiers, (list, tuple)):
            raise HTTPException(422, "tiers must be a list")
        out["tiers"] = [_validate_tier(t) for t in tiers[:_MAX_TIERS]]
    # max_images must be ≥ min_images (an inverted cap would send nothing useful).
    if (out.get("max_images") is not None and out.get("min_images") is not None
            and out["max_images"] < out["min_images"]):
        raise HTTPException(422, "max_images must be ≥ min_images")
    return out


@router.get("/admin/tip-reward-config")
async def get_tip_reward_config(account_id: str = Query(...)) -> dict[str, Any]:
    assert_account_owned(account_id)
    async with get_session() as s:
        row = await s.get(AccountAiConfig, account_id)
    stored: dict = {}
    if row is not None and row.tip_reward_config_json:
        try:
            stored = json.loads(row.tip_reward_config_json) or {}
        except Exception:
            stored = {}
    return {"account_id": account_id, "config": stored, "defaults": dict(_DEFAULTS)}


class _ConfigBody(BaseModel):
    account_id: str
    config: dict


@router.put("/admin/tip-reward-config")
async def put_tip_reward_config(body: _ConfigBody = Body(...)) -> dict[str, Any]:
    assert_account_owned(body.account_id)
    clean = _validate(body.config)
    now = datetime.utcnow()
    payload = json.dumps(clean)
    async with get_session() as s:
        await s.execute(
            sqlite_insert(AccountAiConfig)
            .values(account_id=body.account_id, utc_offset=0,
                    tip_reward_config_json=payload, updated_at=now)
            .on_conflict_do_update(
                index_elements=["account_id"],
                set_={"tip_reward_config_json": payload, "updated_at": now})
        )
    log.info("tip_reward_config_saved account=%s cfg=%s", body.account_id, clean)
    return {"account_id": body.account_id, "config": clean}
