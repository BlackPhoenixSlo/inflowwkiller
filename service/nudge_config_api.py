"""service/nudge_config_api.py — read/write account_ai_config.nudge_config_json.

The nudge_online automation's rich config (content_mode, repeat_mode, delays,
caps, quiet hours, and the per-slot tease pools) lives on
`account_ai_config.nudge_config_json` — separate from the automation_rules row
that only carries the 60s cadence. BOTH the detector and the delayed fire-job
read it from there, so the Settings UI persists it through these owner-gated
routes (NOT the rule payload).

  GET /admin/nudge-config?account_id=   → {config, defaults}
       `config` is what's stored (or {} if unset); `defaults` is the automation's
       built-in config so the UI can show effective values + the seed slot pools.
  PUT /admin/nudge-config               → upsert the JSON, returns {config}

The automation merges stored-over-defaults at run time (_load_nudge_config), so a
partial config here is fine — anything omitted falls back to the default.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from sqlalchemy import select

from auth import assert_account_owned
from db.engine import get_session
from db.models import AccountAiConfig, AutomationRule

log = logging.getLogger("of-relay.nudge_config_api")

router = APIRouter()

_NUM_KEYS = {
    "delay_minutes", "jitter_minutes", "gap_minutes", "min_hours_between_nudges",
    "max_per_tick", "max_online_scan", "online_recent_minutes",
    "welcome_grace_hours", "active_convo_hours", "max_no_reply",
}


def _defaults() -> dict:
    # Lazy import so this light API module doesn't pull the executor at startup.
    from automations.nudge_online import _DEFAULT_NUDGE_CONFIG
    return json.loads(json.dumps(_DEFAULT_NUDGE_CONFIG))


def _validate_config(cfg: dict) -> dict:
    """Coerce/clamp a posted config so a bad value can never reach the automation.
    Unknown keys are dropped; omitted keys simply fall back to defaults at run."""
    if not isinstance(cfg, dict):
        raise HTTPException(422, "config must be an object")
    out: dict[str, Any] = {}
    if "enabled" in cfg:
        out["enabled"] = bool(cfg["enabled"])
    if "with_image" in cfg:
        out["with_image"] = bool(cfg["with_image"])
    if "content_mode" in cfg:
        cm = str(cfg["content_mode"])
        if cm not in ("none", "tease", "qa", "mix"):
            raise HTTPException(422, "content_mode must be 'none', 'tease', 'qa' or 'mix'")
        out["content_mode"] = cm
    if "repeat_mode" in cfg:
        rm = str(cfg["repeat_mode"])
        if rm not in ("new", "same"):
            raise HTTPException(422, "repeat_mode must be 'new' or 'same'")
        out["repeat_mode"] = rm
    # Two-part message model: each part's toggles + the info line's mode.
    for bk in ("send_nudge_line", "nudge_line_text", "nudge_line_image",
               "send_info_line", "info_line_image"):
        if bk in cfg:
            out[bk] = bool(cfg[bk])
    if "info_line_mode" in cfg:
        im = str(cfg["info_line_mode"])
        if im not in ("qa", "tease", "mix"):
            raise HTTPException(422, "info_line_mode must be 'qa', 'tease' or 'mix'")
        out["info_line_mode"] = im
    for k in _NUM_KEYS:
        if k in cfg and cfg[k] is not None:
            try:
                v = int(cfg[k])
            except (TypeError, ValueError):
                raise HTTPException(422, f"{k} must be a number")
            out[k] = max(0, v)
    if "quiet_hours" in cfg:
        qh = cfg["quiet_hours"]
        if qh in (None, []):
            out["quiet_hours"] = [0, 0]  # disabled
        elif isinstance(qh, (list, tuple)) and len(qh) == 2:
            try:
                out["quiet_hours"] = [int(qh[0]) % 24, int(qh[1]) % 24]
            except (TypeError, ValueError):
                raise HTTPException(422, "quiet_hours must be two integers")
        else:
            raise HTTPException(422, "quiet_hours must be [start, end]")
    if "slots" in cfg:
        if not isinstance(cfg["slots"], dict):
            raise HTTPException(422, "slots must be an object")
        out["slots"] = cfg["slots"]  # shape is free-form (day → slot → {text,image})
    return out


@router.get("/admin/nudge-config")
async def get_nudge_config(account_id: str = Query(...)) -> dict[str, Any]:
    assert_account_owned(account_id)
    async with get_session() as s:
        row = await s.get(AccountAiConfig, account_id)
    stored: dict = {}
    if row is not None and row.nudge_config_json:
        try:
            stored = json.loads(row.nudge_config_json) or {}
        except Exception:
            stored = {}
    return {"account_id": account_id, "config": stored, "defaults": _defaults()}


class _ConfigBody(BaseModel):
    account_id: str
    config: dict


@router.put("/admin/nudge-config")
async def put_nudge_config(body: _ConfigBody = Body(...)) -> dict[str, Any]:
    assert_account_owned(body.account_id)
    clean = _validate_config(body.config)
    now = datetime.utcnow()
    payload = json.dumps(clean)
    async with get_session() as s:
        await s.execute(
            sqlite_insert(AccountAiConfig)
            .values(account_id=body.account_id, utc_offset=0,
                    nudge_config_json=payload, updated_at=now)
            .on_conflict_do_update(
                index_elements=["account_id"],
                set_={"nudge_config_json": payload, "updated_at": now})
        )
    log.info("nudge_config_saved account=%s keys=%s", body.account_id, sorted(clean))
    return {"account_id": body.account_id, "config": clean}


# ── Bulk roll-out across models (text-only — no vault) ────────────────

_DETECT_EVERY_S = 60


async def _upsert_config(s, account_id: str, payload: str, now: datetime) -> None:
    await s.execute(
        sqlite_insert(AccountAiConfig)
        .values(account_id=account_id, utc_offset=0,
                nudge_config_json=payload, updated_at=now)
        .on_conflict_do_update(
            index_elements=["account_id"],
            set_={"nudge_config_json": payload, "updated_at": now})
    )


async def _ensure_rule(s, account_id: str, enable: bool) -> str:
    """Create or update the account's nudge_online rule (60s cadence, text-only
    payload). automation_rules has no unique (account, kind), so find-or-create.
    Returns 'created' | 'updated'."""
    existing = (await s.execute(
        select(AutomationRule).where(
            AutomationRule.account_id == account_id,
            AutomationRule.kind == "nudge_online").limit(1)
    )).scalar_one_or_none()
    trigger = json.dumps({"every_seconds": _DETECT_EVERY_S})
    steps = json.dumps({"with_image": False})
    if existing is not None:
        existing.trigger_json = trigger
        existing.steps_json = steps
        existing.is_enabled = bool(enable)
        return "updated"
    s.add(AutomationRule(
        account_id=account_id, name="Nudge online", kind="nudge_online",
        trigger_json=trigger, steps_json=steps, is_enabled=bool(enable)))
    return "created"


async def _effective_cfg(account_id: str, partial: dict) -> dict:
    """Defaults ← the (possibly-unsaved) form config, plus the account's
    utc_offset + time_images — so the preview matches what a real run would do."""
    merged = _defaults()
    if isinstance(partial, dict):
        merged.update(partial)
    async with get_session() as s:
        row = await s.get(AccountAiConfig, account_id)
    merged["utc_offset"] = (row.utc_offset or 0) if row is not None else 0
    ti: dict = {}
    if row is not None and row.time_images_json:
        try:
            ti = json.loads(row.time_images_json) or {}
        except Exception:
            ti = {}
    merged["time_images"] = ti
    return merged


class _PreviewBody(BaseModel):
    account_id: str
    config: dict
    fan_id: int | None = None
    hour: int | None = None


@router.post("/admin/nudge-config/preview")
async def preview_nudge(body: _PreviewBody = Body(...)) -> dict[str, Any]:
    """Preview the EXACT message list a nudge would send for the current config —
    the nudge line and/or info line, honoring each part's toggles — for a chosen
    fan (or a sample) at a chosen hour. NO send, NO state write."""
    assert_account_owned(body.account_id)
    cfg = await _effective_cfg(body.account_id, body.config)
    from automations.nudge_online import preview_compose
    res = await preview_compose(body.account_id, cfg, fan_id=body.fan_id, hour=body.hour)
    return {"account_id": body.account_id, "fan_id": body.fan_id, **res}


class _BulkBody(BaseModel):
    account_ids: list[str]
    config: dict
    enable: bool = True


@router.put("/admin/nudge-config/bulk")
async def put_nudge_config_bulk(body: _BulkBody = Body(...)) -> dict[str, Any]:
    """Apply ONE shared config to many models at once and (optionally) enable each
    model's 60s nudge rule live. Text-only by design — with_image is forced off so
    no per-account vault media is needed (that's what makes 'all models' safe)."""
    if not body.account_ids:
        raise HTTPException(422, "account_ids cannot be empty")
    for aid in body.account_ids:
        assert_account_owned(aid)  # 403 on any non-owned id BEFORE any write
    clean = _validate_config(body.config)
    # bulk = text-only (no per-account vault): no image on either part.
    clean["nudge_line_image"] = False
    clean["info_line_image"] = False
    payload = json.dumps(clean)
    now = datetime.utcnow()
    applied: list[dict[str, str]] = []
    async with get_session() as s:
        for aid in body.account_ids:
            await _upsert_config(s, aid, payload, now)
            action = await _ensure_rule(s, aid, body.enable)
            applied.append({"account_id": aid, "rule": action})
    log.info("nudge_config_bulk applied=%d enable=%s", len(applied), body.enable)
    return {"applied": applied, "count": len(applied), "enabled": body.enable,
            "config": clean}


# ── Mass Nudge roll-out (config lives in the rule payload, no extra table) ──

_MIN_EVERY_S = 60
_MAX_EVERY_S = 30 * 24 * 60 * 60


async def _ensure_kind_rule(s, account_id: str, kind: str, name: str,
                            trigger_json: str, steps_json: str, enable: bool) -> str:
    existing = (await s.execute(
        select(AutomationRule).where(
            AutomationRule.account_id == account_id,
            AutomationRule.kind == kind).limit(1)
    )).scalar_one_or_none()
    if existing is not None:
        existing.trigger_json = trigger_json
        existing.steps_json = steps_json
        existing.is_enabled = bool(enable)
        return "updated"
    s.add(AutomationRule(account_id=account_id, name=name, kind=kind,
                         trigger_json=trigger_json, steps_json=steps_json,
                         is_enabled=bool(enable)))
    return "created"


class _MassNudgeBulkBody(BaseModel):
    account_ids: list[str]
    payload: dict
    enable: bool = True
    every_seconds: int = 3600


class _MassNudgePreviewBody(BaseModel):
    account_id: str
    payload: dict
    hour: int | None = None


@router.post("/admin/mass-nudge/preview")
async def preview_mass_nudge(body: _MassNudgePreviewBody = Body(...)) -> dict[str, Any]:
    """Compose the Mass Nudge broadcast for a chosen hour — text + image, no send."""
    assert_account_owned(body.account_id)
    from automations.mass_nudge import preview_compose
    return await preview_compose(body.account_id, body.payload, hour=body.hour)


@router.put("/admin/mass-nudge/bulk")
async def put_mass_nudge_bulk(body: _MassNudgeBulkBody = Body(...)) -> dict[str, Any]:
    """Create/enable the mass_nudge rule on many models at once with one shared
    payload (slots/exclude/unsend) and cadence. Mass Nudge keeps ALL config in the
    rule payload, so there's no separate config store to write."""
    if not body.account_ids:
        raise HTTPException(422, "account_ids cannot be empty")
    for aid in body.account_ids:
        assert_account_owned(aid)
    every = max(_MIN_EVERY_S, min(int(body.every_seconds or 3600), _MAX_EVERY_S))
    trigger = json.dumps({"every_seconds": every})
    steps = json.dumps(body.payload or {})
    now = datetime.utcnow()  # noqa: F841 (parity; ensure-rule doesn't stamp)
    applied: list[dict[str, str]] = []
    async with get_session() as s:
        for aid in body.account_ids:
            action = await _ensure_kind_rule(
                s, aid, "mass_nudge", "Mass Nudge", trigger, steps, body.enable)
            applied.append({"account_id": aid, "rule": action})
    log.info("mass_nudge_bulk applied=%d enable=%s every=%ds", len(applied), body.enable, every)
    return {"applied": applied, "count": len(applied), "enabled": body.enable,
            "every_seconds": every}
