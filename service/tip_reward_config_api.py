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
    # Inbound-image reply knobs (Flag 1 — a fan sends US a photo).
    "image_reply_count": (1, 10),
    "image_reply_basis_cents": (0, 1_000_000),
    "image_reply_cooldown_hours": (0, 8760),
    # Hot-thread teaser knobs (SELECTED by tip_reward, SENT by ai_chatter). The price
    # is re-clamped to OF's wire range at the send site; this cap just stops a typo.
    "hot_teaser_count": (1, 50),
    "hot_teaser_cooldown_hours": (0, 8760),
    "hot_teaser_free_max": (0, 1000),
    "hot_teaser_price_cents": (0, 100_000),
    # Conversational teaser ladder (the rungs list is validated separately below).
    "teaser_convo_after_fan_msgs": (1, 1000),
    "teaser_convo_count": (1, 50),
    # Context-aware picking (matched-to-his-ask swaps in the reward bundle).
    "context_pick_max": (0, 10),
    "context_pick_messages": (1, 100),
}
_MAX_TEASER_RUNGS = 10
_MAX_TIERS = 10
_MAX_FOLDERS_PER_TIER = 25
_CAPTION_MAX = 500
_ASK_TEMPLATE_MAX = 300

# Item-42 "tip request" sub-config numeric knobs (nudge a quiet mass-buyer with a
# free teaser image + tip ask). Shares this column but drives its OWN automation
# (`tip_request`) — nested here to avoid a cross-lane models.py schema change.
_TIP_REQUEST_INT_KNOBS = {
    "min_wait_hours": (0, 8760),
    "max_age_hours": (1, 8760),
    "cooldown_hours": (0, 8760),
    "guard_hours": (0, 8760),
    "limit": (1, 5000),
}


def _validate_tip_request(t: Any) -> dict:
    """Validate/clamp the nested `tip_request` sub-object. Unknown keys dropped;
    numeric knobs clamped so a typo can't blast a folder or chase ancient buys."""
    if not isinstance(t, dict):
        raise HTTPException(422, "tip_request must be an object")
    out: dict[str, Any] = {}
    if "enabled" in t:
        out["enabled"] = bool(t["enabled"])
    if "media_id" in t:
        mid = t["media_id"]
        if mid in (None, "", 0):
            out["media_id"] = None            # explicit clear (back to disabled)
        else:
            try:
                out["media_id"] = int(mid)
            except (TypeError, ValueError):
                raise HTTPException(422, "tip_request.media_id must be a number")
    if "caption" in t:
        out["caption"] = str(t["caption"] or "")[:_CAPTION_MAX]
    for k, (lo, hi) in _TIP_REQUEST_INT_KNOBS.items():
        if k in t and t[k] is not None:
            try:
                out[k] = max(lo, min(int(t[k]), hi))
            except (TypeError, ValueError):
                raise HTTPException(422, f"tip_request.{k} must be a number")
    return out


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
    # "Always reward" — fire on every tip even when an ai_chatter PPV offer is
    # open for the fan (overrides the standdown; the offer is still credited).
    if "always_reward" in cfg:
        out["always_reward"] = bool(cfg["always_reward"])
    # Inbound-image buying-signal handler (a fan sends US a photo). Two independent
    # flags, both default OFF, both independent of the tip `enabled` master switch.
    if "image_reply_enabled" in cfg:
        out["image_reply_enabled"] = bool(cfg["image_reply_enabled"])
    if "image_closer_enabled" in cfg:
        out["image_closer_enabled"] = bool(cfg["image_closer_enabled"])
    # Hot-thread proactive teaser (read by ai_chatter). Master flag + the two vault
    # folders; the numeric knobs are clamped in the _INT_KNOBS loop below.
    if "hot_teaser_enabled" in cfg:
        out["hot_teaser_enabled"] = bool(cfg["hot_teaser_enabled"])
    for _k in ("hot_teaser_free_folder", "hot_teaser_paid_folder"):
        if _k in cfg:
            out[_k] = str(cfg[_k] or "").strip()[:40]
    # Conversational teaser ladder — enable flag + the rungs list ({folder, price_cents}).
    if "teaser_convo_enabled" in cfg:
        out["teaser_convo_enabled"] = bool(cfg["teaser_convo_enabled"])
    if "teaser_convo_rungs" in cfg:
        rungs = cfg["teaser_convo_rungs"]
        if not isinstance(rungs, (list, tuple)):
            raise HTTPException(422, "teaser_convo_rungs must be a list")
        clean_rungs = []
        for r in rungs[:_MAX_TEASER_RUNGS]:
            if not isinstance(r, dict):
                raise HTTPException(422, "each teaser rung must be an object")
            try:
                price = max(0, min(int(r.get("price_cents") or 0), 100_000))
            except (TypeError, ValueError):
                raise HTTPException(422, "teaser rung price_cents must be a number")
            clean_rungs.append({"folder": str(r.get("folder") or "").strip()[:40],
                                "price_cents": price})
        out["teaser_convo_rungs"] = clean_rungs
    # The content-ask tip-ask toggle (ASK side). Independent of `enabled` (the
    # delivery side) — the ask can be on while reward delivery is off, or vice versa.
    if "ask_enabled" in cfg:
        out["ask_enabled"] = bool(cfg["ask_enabled"])
    # Context matcher (reads the thread, swaps in photos matching his ask).
    # Default TRUE in the automation; an explicit false here turns it off.
    if "context_pick_enabled" in cfg:
        out["context_pick_enabled"] = bool(cfg["context_pick_enabled"])
    for k, (lo, hi) in _INT_KNOBS.items():
        if k in cfg and cfg[k] is not None:
            try:
                out[k] = max(lo, min(int(cfg[k]), hi))
            except (TypeError, ValueError):
                raise HTTPException(422, f"{k} must be a number")
    if "caption" in cfg:
        out["caption"] = str(cfg["caption"] or "")[:_CAPTION_MAX]
    if "image_reply_caption" in cfg:
        out["image_reply_caption"] = str(cfg["image_reply_caption"] or "")[:_CAPTION_MAX]
    if "ask_template" in cfg:
        out["ask_template"] = str(cfg["ask_template"] or "")[:_ASK_TEMPLATE_MAX]
    if "tiers" in cfg:
        tiers = cfg["tiers"]
        if not isinstance(tiers, (list, tuple)):
            raise HTTPException(422, "tiers must be a list")
        out["tiers"] = [_validate_tier(t) for t in tiers[:_MAX_TIERS]]
    # Item 42: nudge-a-quiet-mass-buyer sub-config (its own automation, shares
    # this column). Passthrough-validated so a tip-reward save doesn't drop it.
    if "tip_request" in cfg:
        out["tip_request"] = _validate_tip_request(cfg["tip_request"])
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
