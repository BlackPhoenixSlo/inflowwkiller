"""service/make_right_config_api.py — read/write account_ai_config.make_right_config_json.

The `make_right` automation (the Resolution Agent — detect a wrong-content
outcome, esp. a double-charge, and make the fan whole with an apology + free
unseen content) is gated + tuned per-account by this JSON. The Automations
"Make It Right" tab persists it through these owner-gated routes.

  GET /admin/make-right-config?account_id=  → {config, defaults}
  PUT /admin/make-right-config              → upsert the JSON, returns {config}

Default (absent/NULL) → DISABLED + PREVIEW-ONLY (enabled AND auto_send both off)
until a creator ticks them on here. Refunds are NEVER auto-moved — `flag_refund`
only raises an operator-review flag.
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
from automations.make_right import _DEFAULTS

log = logging.getLogger("of-relay.make_right_config_api")

router = APIRouter()

# (key, min, max) — clamp every numeric knob so a typo can't over-gift.
_INT_KNOBS = {
    "lookback_days": (1, 365),
    "per_fan_cap": (1, 10),
    "gift_piece_value_cents": (1, 100_000),
    "gift_min_count": (1, 20),
    "gift_max_count": (1, 20),
    "guard_hours": (0, 8760),
    # Multi-turn exchange knobs.
    "free_steps": (0, 10),             # free pieces before the PPV pivot
    "gift_pieces_per_step": (1, 10),   # unseen pieces per free step
    "ppv_price_cents": (0, 1_000_000), # the PPV ask (0 → no PPV pivot)
    "nudge_hours": (1, 8760),          # silence before one nudge
    "close_hours": (1, 8760),          # silence before closing to an operator
    "undelivered_grace_hours": (0, 8760),  # class #2 grace window
}
_CAPTION_MAX = 500


def _validate(cfg: dict) -> dict:
    """Whitelist validator — any key not named here is DROPPED on save (mirrors
    tip_reward_config_api). So a new knob must be registered here or it silently
    never persists."""
    if not isinstance(cfg, dict):
        raise HTTPException(422, "config must be an object")
    out: dict[str, Any] = {}
    for k in ("enabled", "auto_send", "gift_value_match", "flag_refund",
              "open_with_gift", "detect_undelivered"):
        if k in cfg:
            out[k] = bool(cfg[k])
    if "gift_tier" in cfg:
        out["gift_tier"] = str(cfg["gift_tier"] or "").strip()[:40]
    if "ppv_folder" in cfg:
        out["ppv_folder"] = str(cfg["ppv_folder"] or "").strip()[:40]
    for k in ("apology_caption", "ppv_caption"):
        if k in cfg:
            out[k] = str(cfg[k] or "")[:_CAPTION_MAX]
    for k, (lo, hi) in _INT_KNOBS.items():
        if k in cfg and cfg[k] is not None:
            try:
                out[k] = max(lo, min(int(cfg[k]), hi))
            except (TypeError, ValueError):
                raise HTTPException(422, f"{k} must be a number")
    # gift_max_count must be ≥ gift_min_count (an inverted cap would gift nothing).
    if (out.get("gift_max_count") is not None and out.get("gift_min_count") is not None
            and out["gift_max_count"] < out["gift_min_count"]):
        raise HTTPException(422, "gift_max_count must be ≥ gift_min_count")
    # close must be ≥ nudge (nudge fires first, then close).
    if (out.get("close_hours") is not None and out.get("nudge_hours") is not None
            and out["close_hours"] < out["nudge_hours"]):
        raise HTTPException(422, "close_hours must be ≥ nudge_hours")
    return out


@router.get("/admin/make-right-config")
async def get_make_right_config(account_id: str = Query(...)) -> dict[str, Any]:
    assert_account_owned(account_id)
    async with get_session() as s:
        row = await s.get(AccountAiConfig, account_id)
    stored: dict = {}
    if row is not None and row.make_right_config_json:
        try:
            stored = json.loads(row.make_right_config_json) or {}
        except Exception:
            stored = {}
    return {"account_id": account_id, "config": stored, "defaults": dict(_DEFAULTS)}


class _ConfigBody(BaseModel):
    account_id: str
    config: dict


@router.put("/admin/make-right-config")
async def put_make_right_config(body: _ConfigBody = Body(...)) -> dict[str, Any]:
    assert_account_owned(body.account_id)
    clean = _validate(body.config)
    now = datetime.utcnow()
    payload = json.dumps(clean)
    async with get_session() as s:
        await s.execute(
            sqlite_insert(AccountAiConfig)
            .values(account_id=body.account_id, utc_offset=0,
                    make_right_config_json=payload, updated_at=now)
            .on_conflict_do_update(
                index_elements=["account_id"],
                set_={"make_right_config_json": payload, "updated_at": now})
        )
    log.info("make_right_config_saved account=%s cfg=%s", body.account_id, clean)
    return {"account_id": body.account_id, "config": clean}
