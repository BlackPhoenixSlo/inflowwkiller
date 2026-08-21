"""service/banned_words_api.py — read/write account_ai_config.banned_words_config_json.

The "Block sensitive words" compliance filter: an operator-editable banned-word list
scanned against OUTBOUND text at the send chokepoint (the AI seller + the mass path).
Stored as its own column (not folded into the bool-only style blob) so a list + a mode
string persist cleanly:

    {"words": ["fuck", "child porn", ...], "mode": "block" | "mask"}

  "block" — a hit ABORTS that send (the message is dropped + logged).
  "mask"  — each hit is starred out ("fuck" → "f***") and the send proceeds.

  GET /admin/banned-words?account_id=  → {config, defaults}
  PUT /admin/banned-words              → upsert the JSON, returns {config}

Absent/NULL/empty words → OFF (nothing is scanned, current behavior byte-for-byte).
Owner-gated, mirrors style_config_api / make_right_config_api. The runtime reader is
automations._wordfilter.load_banned_words; the pure scanner is _wordfilter.scan_outbound.
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
from automations._wordfilter import BANNED_WORD_MODES, normalize_banned_config

log = logging.getLogger("of-relay.banned_words_api")

router = APIRouter()

_DEFAULTS: dict[str, Any] = {"words": [], "mode": "block"}


def _clean(cfg: dict) -> dict[str, Any]:
    """Normalize an operator payload into the stored dict shape. The parse/dedupe/mode
    rules live in the canonical `normalize_banned_config` (shared with the runtime
    reader) so the API and the scanner can't drift; this just re-shapes the tuple."""
    words, mode = normalize_banned_config(cfg)
    return {"words": words, "mode": mode}


def _load(row: AccountAiConfig | None) -> dict[str, Any]:
    """Stored config as an operator would see it; defaults on absent/NULL/parse-error."""
    if row is None or not getattr(row, "banned_words_config_json", None):
        return dict(_DEFAULTS)
    try:
        stored = json.loads(row.banned_words_config_json) or {}
    except Exception:
        return dict(_DEFAULTS)
    return _clean(stored)


@router.get("/admin/banned-words")
async def get_banned_words(account_id: str = Query(...)) -> dict[str, Any]:
    assert_account_owned(account_id)
    async with get_session() as s:
        row = await s.get(AccountAiConfig, account_id)
    return {"account_id": account_id, "config": _load(row),
            "defaults": dict(_DEFAULTS), "modes": list(BANNED_WORD_MODES)}


class _ConfigBody(BaseModel):
    account_id: str
    config: dict


@router.put("/admin/banned-words")
async def put_banned_words(body: _ConfigBody = Body(...)) -> dict[str, Any]:
    assert_account_owned(body.account_id)
    clean = _clean(body.config or {})
    now = datetime.utcnow()
    payload = json.dumps(clean)
    async with get_session() as s:
        await s.execute(
            sqlite_insert(AccountAiConfig)
            .values(account_id=body.account_id, utc_offset=0,
                    banned_words_config_json=payload, updated_at=now)
            .on_conflict_do_update(
                index_elements=["account_id"],
                set_={"banned_words_config_json": payload, "updated_at": now})
        )
    log.info("banned_words_saved account=%s words=%d mode=%s",
             body.account_id, len(clean["words"]), clean["mode"])
    return {"account_id": body.account_id, "config": clean}
