"""service/autoreply_config_api.py — read/write account_ai_config.autoreply_config_json.

The `autoreply` automation (keep-warm re-engagement of quiet, low-spend known
fans) is gated + tuned per-account by this JSON. The Settings "Auto-reply" tab
persists it through these owner-gated routes.

  GET /admin/autoreply-config?account_id=  → {config, defaults}
  PUT /admin/autoreply-config              → upsert the JSON, returns {config}

Default (absent/NULL) → OFF — no account auto-replies until enabled here.
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
from automations.autoreply import _defaults

log = logging.getLogger("of-relay.autoreply_config_api")

router = APIRouter()

# (key, min, max) — clamp every numeric knob so a typo can't park fans or spam.
_INT_KNOBS = {
    "silence_min_minutes": (1, 1440),
    "silence_max_minutes": (2, 10080),
    "max_nudges": (0, 10),
    "min_gap_minutes": (1, 1440),
    "max_lifetime_spend_cents": (0, 10_000_000),
    "recent_spend_days": (1, 365),
    "max_recent_spend_cents": (0, 10_000_000),
    "min_days_since_purchase": (0, 3650),
    "min_days_since_first_chat": (0, 3650),
    "last_n_messages": (2, 60),
}


def _validate(cfg: dict) -> dict:
    if not isinstance(cfg, dict):
        raise HTTPException(422, "config must be an object")
    out: dict[str, Any] = {}
    for b in ("enabled", "info_not_required"):
        if b in cfg:
            out[b] = bool(cfg[b])
    for k, (lo, hi) in _INT_KNOBS.items():
        if k in cfg and cfg[k] is not None:
            try:
                out[k] = max(lo, min(int(cfg[k]), hi))
            except (TypeError, ValueError):
                raise HTTPException(422, f"{k} must be a number")
    if "quiet_hours_json" in cfg:
        qh = cfg["quiet_hours_json"]
        if qh is None:
            out["quiet_hours_json"] = None
        elif (isinstance(qh, (list, tuple)) and len(qh) == 2
              and all(isinstance(x, (int, float)) for x in qh)):
            out["quiet_hours_json"] = [int(qh[0]) % 24, int(qh[1]) % 24]
        else:
            raise HTTPException(422, "quiet_hours_json must be [start,end] or null")
    # silence_max must exceed silence_min (a 0/inverted window would never fire).
    if (out.get("silence_max_minutes") is not None
            and out.get("silence_min_minutes") is not None
            and out["silence_max_minutes"] <= out["silence_min_minutes"]):
        raise HTTPException(422, "silence_max_minutes must be > silence_min_minutes")
    return out


@router.get("/admin/autoreply-config")
async def get_autoreply_config(account_id: str = Query(...)) -> dict[str, Any]:
    assert_account_owned(account_id)
    async with get_session() as s:
        row = await s.get(AccountAiConfig, account_id)
    stored: dict = {}
    if row is not None and row.autoreply_config_json:
        try:
            stored = json.loads(row.autoreply_config_json) or {}
        except Exception:
            stored = {}
    return {"account_id": account_id, "config": stored, "defaults": _defaults()}


class _ConfigBody(BaseModel):
    account_id: str
    config: dict


@router.put("/admin/autoreply-config")
async def put_autoreply_config(body: _ConfigBody = Body(...)) -> dict[str, Any]:
    assert_account_owned(body.account_id)
    clean = _validate(body.config)
    now = datetime.utcnow()
    payload = json.dumps(clean)
    async with get_session() as s:
        await s.execute(
            sqlite_insert(AccountAiConfig)
            .values(account_id=body.account_id, utc_offset=0,
                    autoreply_config_json=payload, updated_at=now)
            .on_conflict_do_update(
                index_elements=["account_id"],
                set_={"autoreply_config_json": payload, "updated_at": now})
        )
    log.info("autoreply_config_saved account=%s cfg=%s", body.account_id, clean)
    return {"account_id": body.account_id, "config": clean}
