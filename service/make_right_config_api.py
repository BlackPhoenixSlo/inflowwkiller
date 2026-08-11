"""service/make_right_config_api.py — read/write account_ai_config.make_right_config_json.

The `make_right` automation (the Resolution Agent — detect a wrong-content
outcome, esp. a double-charge, and make the fan whole with an apology + free
unseen content) is gated + tuned per-account by this JSON. The Automations
"Make It Right" tab persists it through these owner-gated routes.

  GET /admin/make-right-config?account_id=  → {config, defaults}
  PUT /admin/make-right-config              → upsert the JSON, returns {config}

Default (absent/NULL) → ON (house policy 2026-08-05: `enabled` AND `auto_send`
both default True in `make_right._DEFAULTS`, so every model catches its own
double-charges). Unticking either switch here stores a `false` that beats the
default. Refunds are NEVER auto-moved — `flag_refund` only raises an
operator-review flag.

Saving also OWNS the account's `make_right` automation rule: the scanner never
runs without one (a config alone is inert — the executor only materializes jobs
from `automation_rules`), so a PUT creates/enables it when the master switch is
on and disables it when it is off.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from auth import assert_account_owned
from db.engine import get_session
from jsonsafe import truthy
from db.models import AccountAiConfig, AutomationRule
from automations.make_right import _DEFAULTS

log = logging.getLogger("of-relay.make_right_config_api")

router = APIRouter()

# Sweep cadence for the rule this module owns. The agent is a safety net, not a
# chat engine: 15 min is fast enough to catch a double-charge and to advance an
# open exchange after his reply (silence checks are self-scheduled on top), and
# cheap — detection is pure SQL, no LLM call.
SWEEP_EVERY_S = 900
_RULE_NAME = "Make It Right (resolution agent)"

# (key, min, max) — clamp every numeric knob so a typo can't over-gift.
_INT_KNOBS = {
    "lookback_days": (1, 365),
    "max_msgs_since_charge": (0, 1000),   # freshness gate; 0 = off (apologise however late)
    "per_fan_cap": (1, 10),
    "guard_hours": (0, 8760),
    # Multi-turn exchange knobs.
    "free_steps": (0, 10),             # free TURNS before the PPV pivot
    # The objection lane's own turn count. It was the ONE exchange knob missing
    # from this table, and this validator drops every key it does not name — so a
    # UI save or a `prod-makeright-on.sh key=value` stamp of it was silently
    # discarded and only a deploy could move it. Same trap `scripts_api`
    # documents for its own bools; same one-line close.
    "apology_free_steps": (0, 10),
    # The ONLY gift-size knob (2026-08-06). `gift_piece_value_cents` /
    # `gift_min_count` / `gift_max_count` / `gift_value_match` were removed from
    # this table with it: they solved-and-clamped a piece count that v2 stopped
    # reading, so they validated numbers nothing obeyed. Dropping them from the
    # whitelist also cleans them out of every stored config on its next save.
    "gift_pieces_per_step": (1, 10),   # unseen pieces on the one gift bubble per turn
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
    # `gift_on_subject` — make the free apology gift CONTAIN what he asked for,
    # instead of the next unseen thing off the tip shelf. Registered here for the
    # reason the docstring gives: unnamed keys are dropped, so without this line
    # the tab's checkbox saves 200 and changes nothing.
    #
    # ⚠️ `truthy`, not `bool` — `bool("false")` is True, so a quoted boolean
    # from the raw-config modal or a `prod-makeright-on.sh key=value` stamp
    # turns a content-SENDING flag on. `scripts_api` grew this exact guard on
    # 2026-08-11 and these bools were left on the old one, which is how the two
    # halves of one feature end up with two different answers to "is it on".
    for k in ("enabled", "auto_send", "flag_refund",
              "open_with_gift", "detect_undelivered", "gift_on_subject"):
        if k in cfg:
            out[k] = truthy(cfg[k])
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
    # (The old gift_max_count ≥ gift_min_count check died with those two keys. One
    # count knob cannot contradict itself, which is the whole reason for one.)
    # close must be ≥ nudge (nudge fires first, then close).
    if (out.get("close_hours") is not None and out.get("nudge_hours") is not None
            and out["close_hours"] < out["nudge_hours"]):
        raise HTTPException(422, "close_hours must be ≥ nudge_hours")
    return out


async def ensure_rule(s, account_id: str, enable: bool) -> str:
    """Create or update the account's `make_right` rule (15-min sweep, empty
    payload). `automation_rules` has no unique (account, kind), so find-or-create
    — mirrors nudge_config_api._ensure_rule. Returns 'created'|'updated'.

    This is why the agent had never fired in prod: the config was saveable but
    nothing ever wrote a rule, so `_materialize_due_rules` had nothing to schedule.
    """
    existing = (await s.execute(
        select(AutomationRule).where(
            AutomationRule.account_id == str(account_id),
            AutomationRule.kind == "make_right").limit(1)
    )).scalar_one_or_none()
    trigger = json.dumps({"every_seconds": SWEEP_EVERY_S})
    if existing is not None:
        existing.trigger_json = trigger
        existing.is_enabled = bool(enable)
        return "updated"
    s.add(AutomationRule(
        account_id=str(account_id), name=_RULE_NAME, kind="make_right",
        trigger_json=trigger, steps_json=json.dumps({}), is_enabled=bool(enable)))
    return "created"


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
    enable = bool(clean.get("enabled", _DEFAULTS["enabled"]))
    async with get_session() as s:
        await s.execute(
            sqlite_insert(AccountAiConfig)
            .values(account_id=body.account_id, utc_offset=0,
                    make_right_config_json=payload, updated_at=now)
            .on_conflict_do_update(
                index_elements=["account_id"],
                set_={"make_right_config_json": payload, "updated_at": now})
        )
        rule = await ensure_rule(s, body.account_id, enable)
    log.info("make_right_config_saved account=%s cfg=%s rule=%s enabled=%s",
             body.account_id, clean, rule, enable)
    return {"account_id": body.account_id, "config": clean,
            "rule": rule, "rule_enabled": enable}
