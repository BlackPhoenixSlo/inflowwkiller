"""service/style_config_api.py — read/write account_ai_config.style_config_json.

The "human texting style" package (short/casual girl voice + 3-bubble splitting +
casualized lowercase Q/Tease) is opt-in PER AUTOMATION. This JSON holds the three
checkboxes the Settings "Auto Convo" tab persists:

    {"of_ai_chat": bool, "autoreply": bool, "deep_convo": bool}

  GET /admin/style-config?account_id=  → {config, defaults}
  PUT /admin/style-config              → upsert the JSON, returns {config}

Absent/NULL or a missing key → OFF for that automation (CURRENT behavior runs
byte-for-byte unchanged). Owner-gated, mirrors autoreply_config_api.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Body, Query
from pydantic import BaseModel
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from auth import assert_account_owned
from db.engine import get_session
from db.models import AccountAiConfig
from automations._common import STYLE_AUTOMATIONS, typo_flag_key

log = logging.getLogger("of-relay.style_config_api")

router = APIRouter()


def _defaults() -> dict[str, bool]:
    """All-OFF — every automation keeps its current behavior until ticked on.
    Two independent toggle sets per automation: the humanizer (`<automation>`) and
    the thumb-typo injector (`typos_<automation>`)."""
    out = {k: False for k in STYLE_AUTOMATIONS}
    out.update({typo_flag_key(k): False for k in STYLE_AUTOMATIONS})
    return out


def _validate(cfg: dict) -> dict[str, bool]:
    """Coerce to exactly the known boolean flags (humanizer + typo, per
    automation); ignore anything else."""
    if not isinstance(cfg, dict):
        cfg = {}
    out = {k: bool(cfg.get(k)) for k in STYLE_AUTOMATIONS}
    out.update({typo_flag_key(k): bool(cfg.get(typo_flag_key(k)))
                for k in STYLE_AUTOMATIONS})
    return out


@router.get("/admin/style-config")
async def get_style_config(account_id: str = Query(...)) -> dict[str, Any]:
    assert_account_owned(account_id)
    async with get_session() as s:
        row = await s.get(AccountAiConfig, account_id)
    stored: dict = {}
    if row is not None and row.style_config_json:
        try:
            stored = json.loads(row.style_config_json) or {}
        except Exception:
            stored = {}
    return {"account_id": account_id, "config": _validate(stored),
            "defaults": _defaults()}


class _ConfigBody(BaseModel):
    account_id: str
    config: dict


@router.put("/admin/style-config")
async def put_style_config(body: _ConfigBody = Body(...)) -> dict[str, Any]:
    assert_account_owned(body.account_id)
    now = datetime.utcnow()
    async with get_session() as s:
        # MERGE partial updates onto the stored config so two surfaces (the Auto
        # Convo tab and the rule editor) never wipe each other's flags — a caller
        # may send just the one automation's two keys.
        row = await s.get(AccountAiConfig, body.account_id)
        stored: dict = {}
        if row is not None and row.style_config_json:
            try:
                stored = json.loads(row.style_config_json) or {}
            except Exception:
                stored = {}
        clean = _validate({**stored, **(body.config or {})})
        payload = json.dumps(clean)
        await s.execute(
            sqlite_insert(AccountAiConfig)
            .values(account_id=body.account_id, utc_offset=0,
                    style_config_json=payload, updated_at=now)
            .on_conflict_do_update(
                index_elements=["account_id"],
                set_={"style_config_json": payload, "updated_at": now})
        )
    log.info("style_config_saved account=%s cfg=%s", body.account_id, clean)
    return {"account_id": body.account_id, "config": clean}
