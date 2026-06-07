"""service/account_config_api.py — read/write the per-account "Brain"
(`account_ai_config`) for the Automations → Brain panel.

`account_ai_config` is the AI voice + caps a model speaks with: persona,
welcome_rules, the 6 time-of-day activities + their per-slot vault images,
location/utc_offset (for the "it's <weekday> <tod> here" line), the daily spend
cap, and the LLM model (global + optional per-purpose override). gen_info /
send_welcome / send_followup / of_ai_chat all resolve it at run time, so this is
the one screen that fills the brain a fresh account starts without.

  GET /admin/account-config?account_id=  → {config, slots, model_options, purposes}
       `config` is the stored brain (defaults filled for the scalar caps); the
       rest is metadata so the editor can render dropdowns + the 6 image slots.
  PUT /admin/account-config              → upsert the brain, returns {config}

The nudge_online config lives on the SAME row (`nudge_config_json`) but is owned
by `nudge_config_api` — this upsert never touches it (it's absent from both the
insert values and the conflict `set_`, so an existing row keeps it).
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
from brain_defaults import BRAIN_DEFAULTS
from db.engine import get_session
from db.models import AccountAiConfig

log = logging.getLogger("of-relay.account_config_api")

router = APIRouter()

# The 6 time-of-day slots, ordered — must match send_welcome._SLOT_KEYS.
TIME_SLOTS: tuple[str, ...] = (
    "morning_1", "morning_2", "afternoon_1", "afternoon_2", "evening", "night",
)
# Purposes that support a per-purpose model override (model_by_purpose).
PURPOSES: tuple[str, ...] = (
    "gen_info", "of_ai_chat", "send_welcome", "send_followup", "deep_convo",
)


def _model_options() -> list[str]:
    """The LLM model ids the account may pick. Lazy import so this light API
    module doesn't pull the llm stack at startup."""
    try:
        from llm_client import MODELS
        return list(MODELS.keys())
    except Exception:  # pragma: no cover - defensive
        return []


def _parse_obj(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        v = json.loads(raw)
        return v if isinstance(v, dict) else {}
    except (TypeError, ValueError):
        return {}


def _clean_text(v: Any, field: str) -> str | None:
    """A trimmed string, or None when blank (clears the column)."""
    if v is None:
        return None
    if not isinstance(v, str):
        raise HTTPException(422, f"{field} must be text")
    s = v.strip()
    return s or None


def _validate_model(v: Any, field: str, allowed: set[str]) -> str | None:
    s = _clean_text(v, field)
    if s is None:
        return None
    if allowed and s not in allowed:
        raise HTTPException(422, f"{field} {s!r} is not a known model ({', '.join(sorted(allowed))})")
    return s


def _serialize(row: AccountAiConfig | None) -> dict[str, Any]:
    """The stored brain → a flat editor-friendly dict (scalar caps defaulted)."""
    return {
        "persona": row.persona if row else None,
        "welcome_rules": row.welcome_rules if row else None,
        "location": row.location if row else None,
        "utc_offset": row.utc_offset if row else 0,
        "daily_cost_cap_cents": row.daily_cost_cap_cents if row else 100,
        "model": row.model if row else None,
        "model_by_purpose": _parse_obj(row.model_by_purpose) if row else {},
        "time_activities": _parse_obj(row.time_activities_json) if row else {},
        "time_images": _parse_obj(row.time_images_json) if row else {},
    }


@router.get("/admin/account-config")
async def get_account_config(account_id: str = Query(...)) -> dict[str, Any]:
    assert_account_owned(account_id)
    async with get_session() as s:
        row = await s.get(AccountAiConfig, account_id)
    return {
        "account_id": account_id,
        "config": _serialize(row),
        # The Lexi-derived starter brain (no images). The editor seeds a blank
        # account from this and the "Reset to defaults" button refills from it —
        # so every model has a worked example to show, not an empty form.
        "defaults": BRAIN_DEFAULTS,
        "slots": list(TIME_SLOTS),
        "model_options": _model_options(),
        "purposes": list(PURPOSES),
    }


class _ConfigBody(BaseModel):
    account_id: str
    config: dict


@router.put("/admin/account-config")
async def put_account_config(body: _ConfigBody = Body(...)) -> dict[str, Any]:
    assert_account_owned(body.account_id)
    cfg = body.config
    if not isinstance(cfg, dict):
        raise HTTPException(422, "config must be an object")
    allowed = set(_model_options())

    persona = _clean_text(cfg.get("persona"), "persona")
    welcome_rules = _clean_text(cfg.get("welcome_rules"), "welcome_rules")
    location = _clean_text(cfg.get("location"), "location")
    model = _validate_model(cfg.get("model"), "model", allowed)

    # utc_offset — whole hours (e.g. Vancouver −6); sane range, default 0.
    utc_raw = cfg.get("utc_offset", 0)
    if isinstance(utc_raw, bool) or not isinstance(utc_raw, (int, float)):
        raise HTTPException(422, "utc_offset must be a whole number of hours")
    utc_offset = int(utc_raw)
    if not (-24 <= utc_offset <= 24):
        raise HTTPException(422, "utc_offset must be between -24 and 24 hours")

    cap_raw = cfg.get("daily_cost_cap_cents", 100)
    if isinstance(cap_raw, bool) or not isinstance(cap_raw, (int, float)) or cap_raw < 0:
        raise HTTPException(422, "daily_cost_cap_cents must be a non-negative number")
    cap = int(cap_raw)

    # model_by_purpose: {purpose: model}; each model validated against MODELS.
    mbp_in = cfg.get("model_by_purpose") or {}
    if not isinstance(mbp_in, dict):
        raise HTTPException(422, "model_by_purpose must be an object")
    mbp: dict[str, str] = {}
    for purpose, mv in mbp_in.items():
        cleaned = _validate_model(mv, f"model_by_purpose.{purpose}", allowed)
        if cleaned:
            mbp[str(purpose)] = cleaned

    # time_activities: keep only the 6 known slots, each a non-empty string.
    acts_in = cfg.get("time_activities") or {}
    if not isinstance(acts_in, dict):
        raise HTTPException(422, "time_activities must be an object")
    acts: dict[str, str] = {}
    for slot in TIME_SLOTS:
        s = _clean_text(acts_in.get(slot), f"time_activities.{slot}")
        if s:
            acts[slot] = s

    # time_images: keep only the 6 known slots, each a media-id int.
    imgs_in = cfg.get("time_images") or {}
    if not isinstance(imgs_in, dict):
        raise HTTPException(422, "time_images must be an object")
    imgs: dict[str, int] = {}
    for slot in TIME_SLOTS:
        v = imgs_in.get(slot)
        if v in (None, "", 0):
            continue
        if isinstance(v, bool) or not isinstance(v, (int, float, str)):
            raise HTTPException(422, f"time_images.{slot} must be a media id")
        try:
            imgs[slot] = int(v)
        except (TypeError, ValueError):
            raise HTTPException(422, f"time_images.{slot} must be a media id")

    now = datetime.utcnow()
    # Empty JSON maps store as NULL ("unset") so the senders' `if col:` checks
    # fall back to their legacy pickers instead of an empty-dict no-op.
    vals = {
        "account_id": body.account_id,
        "persona": persona,
        "welcome_rules": welcome_rules,
        "location": location,
        "utc_offset": utc_offset,
        "daily_cost_cap_cents": cap,
        "model": model,
        "model_by_purpose": json.dumps(mbp) if mbp else None,
        "time_activities_json": json.dumps(acts) if acts else None,
        "time_images_json": json.dumps(imgs) if imgs else None,
        "updated_at": now,
    }
    # nudge_config_json is intentionally absent → preserved on existing rows.
    set_ = {k: v for k, v in vals.items() if k != "account_id"}
    async with get_session() as s:
        await s.execute(
            sqlite_insert(AccountAiConfig)
            .values(**vals)
            .on_conflict_do_update(index_elements=["account_id"], set_=set_)
        )
        row = await s.get(AccountAiConfig, body.account_id)
    log.info("account_config_saved account=%s model=%s slots=%d imgs=%d",
             body.account_id, model, len(acts), len(imgs))
    return {"account_id": body.account_id, "config": _serialize(row)}
