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

log = logging.getLogger("of-relay.automation_preview_api")

router = APIRouter()


class _PreviewBody(BaseModel):
    account_id: str
    kind: str = "send_welcome"
    fan_id: int | None = None
    test_name: str | None = None


@router.post("/admin/automation-preview")
async def automation_preview(body: _PreviewBody = Body(...)) -> dict[str, Any]:
    """Compose the message an automation would produce for one fan — text + chosen
    image id — with NO send and NO state write."""
    assert_account_owned(body.account_id)
    if body.kind == "send_welcome":
        from automations.send_welcome import preview_compose
    elif body.kind == "send_followup":
        from automations.send_followup import preview_compose
    else:
        raise HTTPException(422, f"preview not supported for kind={body.kind!r}")
    res = await preview_compose(
        body.account_id, fan_id=body.fan_id, test_name=body.test_name,
    )
    return {"account_id": body.account_id, "kind": body.kind, **res}
