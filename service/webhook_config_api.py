"""service/webhook_config_api.py — read/write account_ai_config.webhook_config_json.

W7 webhook-priority dispatch is gated per-account by
`account_ai_config.webhook_config_json` ({"enabled", "delay_seconds",
"jitter_seconds"}). `webhook_dispatch.on_inbound_message` reads it on every
inbound DM, so the Settings toggle persists it through these owner-gated routes.

  GET /admin/webhook-config?account_id=  → {config, defaults}
  PUT /admin/webhook-config              → upsert the JSON, returns {config}

Default (absent/NULL) → OFF — no account reacts in real time until enabled here.
A global env kill-switch (W7_WEBHOOK_DISPATCH_DISABLED) overrides this.
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

log = logging.getLogger("of-relay.webhook_config_api")

router = APIRouter()

# Clamp the response delay so a typo can't park a fan for hours.
_MAX_DELAY_S = 600


def _defaults() -> dict:
    """The built-in OFF state — what the UI shows before anything is saved."""
    return {"enabled": False, "delay_seconds": 0, "jitter_seconds": 0}


def _validate_config(cfg: dict) -> dict:
    """Coerce/clamp a posted config; unknown keys dropped."""
    if not isinstance(cfg, dict):
        raise HTTPException(422, "config must be an object")
    out: dict[str, Any] = {}
    if "enabled" in cfg:
        out["enabled"] = bool(cfg["enabled"])
    for k in ("delay_seconds", "jitter_seconds"):
        if k in cfg and cfg[k] is not None:
            try:
                v = int(cfg[k])
            except (TypeError, ValueError):
                raise HTTPException(422, f"{k} must be a number")
            out[k] = max(0, min(v, _MAX_DELAY_S))
    return out


@router.get("/admin/webhook-config")
async def get_webhook_config(account_id: str = Query(...)) -> dict[str, Any]:
    assert_account_owned(account_id)
    async with get_session() as s:
        row = await s.get(AccountAiConfig, account_id)
    stored: dict = {}
    if row is not None and row.webhook_config_json:
        try:
            stored = json.loads(row.webhook_config_json) or {}
        except Exception:
            stored = {}
    return {"account_id": account_id, "config": stored, "defaults": _defaults()}


class _ConfigBody(BaseModel):
    account_id: str
    config: dict


@router.put("/admin/webhook-config")
async def put_webhook_config(body: _ConfigBody = Body(...)) -> dict[str, Any]:
    assert_account_owned(body.account_id)
    clean = _validate_config(body.config)
    now = datetime.utcnow()
    payload = json.dumps(clean)
    async with get_session() as s:
        await s.execute(
            sqlite_insert(AccountAiConfig)
            .values(account_id=body.account_id, utc_offset=0,
                    webhook_config_json=payload, updated_at=now)
            .on_conflict_do_update(
                index_elements=["account_id"],
                set_={"webhook_config_json": payload, "updated_at": now})
        )
    log.info("webhook_config_saved account=%s cfg=%s", body.account_id, clean)
    return {"account_id": body.account_id, "config": clean}
