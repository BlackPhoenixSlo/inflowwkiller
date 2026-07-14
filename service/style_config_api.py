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
from automations._common import STYLE_AUTOMATIONS, typo_flag_key, nonnative_flag_key

log = logging.getLogger("of-relay.style_config_api")

router = APIRouter()


def _defaults() -> dict[str, bool]:
    """All-OFF — every automation keeps its current behavior until ticked on.
    Three independent toggle sets per automation: the humanizer (`<automation>`), the
    thumb-typo injector (`typos_<automation>`), and the non-native English layer
    (`nonnative_<automation>`)."""
    # Reflect the RUNTIME tri-state default (_common._STYLE_DEFAULT_ON), not a flat
    # False: ai_chatter's humanizer/typo/non-native ship ON. If _defaults reported them
    # off, the UI would render the boxes unchecked and an unrelated save would then
    # stamp that (false) view as explicit — silently killing default-on realism.
    from automations._common import _style_default
    out = {k: _style_default(k) for k in STYLE_AUTOMATIONS}
    out.update({typo_flag_key(k): _style_default(k) for k in STYLE_AUTOMATIONS})
    out.update({nonnative_flag_key(k): _style_default(k) for k in STYLE_AUTOMATIONS})
    # Account-wide (not per-automation): strip every emoji at the send chokepoint.
    out["strip_emojis"] = False
    return out


def _resolved_view(stored: dict) -> dict[str, bool]:
    """The RENDERED view: each known flag as the loader would resolve it — the explicit
    stored value if present, else the tri-state default. Used by GET so the UI shows
    ai_chatter's realism checked-by-default, and so a save doesn't flip an implicit-ON
    flag to explicit-OFF."""
    from automations._common import _resolve_style_flag
    if not isinstance(stored, dict):
        stored = {}
    out = {k: _resolve_style_flag(stored, k, k) for k in STYLE_AUTOMATIONS}
    out.update({typo_flag_key(k): _resolve_style_flag(stored, k, typo_flag_key(k))
                for k in STYLE_AUTOMATIONS})
    out.update({nonnative_flag_key(k): _resolve_style_flag(stored, k, nonnative_flag_key(k))
                for k in STYLE_AUTOMATIONS})
    out["strip_emojis"] = bool(stored.get("strip_emojis"))
    return out


def _persist(cfg: dict) -> dict[str, bool]:
    """What we WRITE back. ONLY the keys explicitly present in the merged config —
    absent keys are LEFT ABSENT so they keep resolving to the tri-state default at load
    time. Materializing every absent key to False (the old bug) stamped ai_chatter:false
    on the first save and killed default-on realism for every UI-touched account."""
    if not isinstance(cfg, dict):
        cfg = {}
    known = set(STYLE_AUTOMATIONS) \
        | {typo_flag_key(k) for k in STYLE_AUTOMATIONS} \
        | {nonnative_flag_key(k) for k in STYLE_AUTOMATIONS} \
        | {"strip_emojis"}
    return {k: bool(v) for k, v in cfg.items() if k in known}


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
    return {"account_id": account_id, "config": _resolved_view(stored),
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
        # Persist only explicitly-present keys (merged over what was already explicit),
        # so an untouched ai_chatter realism flag stays ABSENT → default-ON at load time.
        clean = _persist({**stored, **(body.config or {})})
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
    # Return the RESOLVED view (default-aware), not the sparse persisted dict, so the UI
    # renders ai_chatter's realism as checked-by-default right after a save.
    return {"account_id": body.account_id, "config": _resolved_view(clean)}
