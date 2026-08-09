"""service/automation_preview_api.py — compose-only preview for automations.

Surfaces what an automation WOULD send for one fan, WITHOUT sending and WITHOUT
writing any state — the Brain panel's "Preview" buttons. Mirrors the nudge-config
preview (nudge_config_api.preview_nudge → nudge_online.preview_compose).

  POST /admin/automation-preview  {account_id, kind, fan_id?, test_name?}
       → {account_id, kind, text, image, name, slot, ...}

Owner-gated; covered by the /admin/:path* relay rewrite (no next.config change).
Wired kinds: send_welcome, send_followup. Any other kind 422s until it grows a
preview_compose of its own.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Body, HTTPException
from pydantic import BaseModel

from auth import assert_account_owned
from llm_client import LLMCapExceeded

log = logging.getLogger("of-relay.automation_preview_api")

router = APIRouter()


class _PreviewBody(BaseModel):
    account_id: str
    kind: str = "send_welcome"
    fan_id: int | None = None
    test_name: str | None = None
    # send_welcome preview extras (Pydantic drops undeclared fields, so they MUST
    # live on the model to reach preview_compose): show the real AI-restyled line,
    # pin a time-of-day slot, and preview against the unsaved on-screen draft config.
    restyle: bool = False
    slot: str | None = None
    model: str | None = None
    config: dict[str, Any] | None = None
    # "Regenerate" sets this so a preview bypasses a pinned slot line to sample a
    # fresh candidate the operator can then keep in its place.
    ignore_pin: bool = False
    # Mirror of the send_welcome rule's `time_only` knob so the preview shows the
    # SHORT bubble 2 (day / time of day / location, no activity) the moment the
    # checkbox is ticked — before the rule is saved.
    time_only: bool = False


@router.post("/admin/automation-preview")
async def automation_preview(body: _PreviewBody = Body(...)) -> dict[str, Any]:
    """Compose the message an automation would produce for one fan — text + chosen
    image id — with NO send and NO send-state write. send_welcome may additionally
    run the real AI restyle (`restyle`), which is a cap-governed/audited LLM call."""
    assert_account_owned(body.account_id)
    if body.kind == "send_welcome":
        from automations.send_welcome import preview_compose
        try:
            res = await preview_compose(
                body.account_id, fan_id=body.fan_id, test_name=body.test_name,
                model=body.model, restyle=body.restyle, slot=body.slot,
                config=body.config, ignore_pin=body.ignore_pin,
                time_only=body.time_only,
            )
        except LLMCapExceeded:
            # Belt-and-braces: preview_compose already degrades a capped restyle to
            # the verbatim line, but stay safe if a future path re-raises.
            raise HTTPException(429, "daily AI cost cap reached")
    elif body.kind == "send_followup":
        from automations.send_followup import preview_compose
        res = await preview_compose(
            body.account_id, fan_id=body.fan_id, test_name=body.test_name,
        )
    else:
        raise HTTPException(422, f"preview not supported for kind={body.kind!r}")
    return {"account_id": body.account_id, "kind": body.kind, **res}


# ── Re-engage buyers — dry-run preview (the openers she WOULD send) ────────

class _ReengageBody(BaseModel):
    account_id: str
    lookback_days: int = 3
    cold_hours: int = 24
    max_per_run: int = 25
    guard_hours: int = 12
    tone: str = "soft"


@router.post("/admin/reengage-preview")
async def reengage_preview(body: _ReengageBody = Body(...)) -> dict[str, Any]:
    """Run reengage_buyers in DRY-RUN and return the cold buyers + the exact personal
    openers she'd send (no send, no state write). The 'Send now' button uses the
    normal /admin/automation/enqueue path with dry_run:false."""
    assert_account_owned(body.account_id)
    from automations.reengage_buyers import run as _run
    return await _run(body.account_id, {
        "lookback_days": body.lookback_days, "cold_hours": body.cold_hours,
        "max_per_run": body.max_per_run, "guard_hours": body.guard_hours,
        "tone": body.tone, "dry_run": True,
    }, run_id=0)


# ── Make It Right (Resolution Agent) — dry-run preview (detected incidents +
#    the proposed apology + free gift; NO send, NO state write) ──────────────

class _MakeRightBody(BaseModel):
    account_id: str


@router.post("/admin/make-right-preview")
async def make_right_preview(body: _MakeRightBody = Body(...)) -> dict[str, Any]:
    """Run make_right in DRY-RUN and return the detected wrong-content incidents
    (headline: double-charges) + the make-right each would get (apology text, the
    free unseen gift media, whether a refund is flagged for an operator, or
    operator_only when the fan is over the cap / no gift is available). Nothing
    sends and nothing is logged. Detection reads the account's persisted config
    (lookback, cap, gift policy); the 'Run now' button uses the normal
    /admin/automation/enqueue path with kind:make_right, which only actually sends
    when both `enabled` and `auto_send` are on."""
    assert_account_owned(body.account_id)
    from automations.make_right import run as _run
    return await _run(body.account_id, {"dry_run": True}, run_id=0)


# ── Welcome line PIN — "reroll until I like one, then send THAT" ──────────

class _PinBody(BaseModel):
    account_id: str
    slot: str
    # The approved restyled line to fix for this slot. None / "" → UNPIN (back to
    # the auto AI restyle). The server stamps the current weekday so a daily welcome
    # can swap it to today's day at send time.
    line: str | None = None


@router.post("/admin/welcome-pin")
async def welcome_pin(body: _PinBody = Body(...)) -> dict[str, Any]:
    """Pin (or, with an empty line, unpin) the operator-approved welcome activity
    line for one time-of-day slot. Persisted on account_ai_config.welcome_pinned_json
    as {slot: {line, weekday}}; send_welcome sends that exact line (weekday refreshed)
    for the slot instead of re-rolling a fresh AI restyle. NO send, owner-gated."""
    import json

    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    from automations.send_welcome import _SLOT_KEYS, _model_weekday
    from db.engine import get_session
    from db.models import AccountAiConfig

    assert_account_owned(body.account_id)
    if body.slot not in _SLOT_KEYS:
        raise HTTPException(422, f"unknown slot {body.slot!r}")

    async with get_session() as s:
        cfg = await s.get(AccountAiConfig, body.account_id)
        pins: dict[str, Any] = {}
        if cfg is not None and getattr(cfg, "welcome_pinned_json", None):
            try:
                pins = json.loads(cfg.welcome_pinned_json) or {}
            except Exception:
                pins = {}
        line = (body.line or "").strip()
        if line:
            # Her clock through the ONE helper, exactly as the sender stamps it.
            from automations import rhythm
            pins[body.slot] = {
                "line": line,
                "weekday": _model_weekday(rhythm.tz_hours_for(
                    getattr(cfg, "timezone", None),
                    getattr(cfg, "utc_offset", None))),
            }
        else:
            pins.pop(body.slot, None)  # unpin
        blob = json.dumps(pins) if pins else None
        if cfg is None:
            # No config row + nothing to pin (an unpin on a never-configured account)
            # → no-op; don't materialize a spurious all-defaults row.
            if blob is None:
                return {"account_id": body.account_id, "slot": body.slot,
                        "pinned": False, "pins": {}}
            await s.execute(
                sqlite_insert(AccountAiConfig)
                .values(account_id=body.account_id, welcome_pinned_json=blob)
                .on_conflict_do_update(
                    index_elements=["account_id"],
                    set_={"welcome_pinned_json": blob},
                )
            )
        else:
            cfg.welcome_pinned_json = blob

    return {"account_id": body.account_id, "slot": body.slot,
            "pinned": bool(line), "pins": pins}
