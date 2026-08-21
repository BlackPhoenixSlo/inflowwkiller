"""service/style_config_api.py — read/write account_ai_config.style_config_json.

The "human texting style" package (short/casual girl voice + 3-bubble splitting +
casualized lowercase Q/Tease) is opt-in PER AUTOMATION. This JSON holds the three
checkboxes the Settings "Auto Convo" tab persists:

    {"welcome_chatter_for_info": bool, "autoreply": bool, "ai_chatter": bool}

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
from automations._common import (
    STYLE_AUTOMATIONS, STYLE_CONSISTENCY_KEYS, typo_flag_key, nonnative_flag_key, spacing_flag_key,
    FACTGROUND_KEY, PAINFUL_TEXTING_KEY, SELL_CUSTOMS_KEY, CAT_STICKERS_KEY,
    CAT_STICKER_SKIP_PCT_KEY,
    CAT_STICKER_SOLO_PCT_KEY, CAT_STICKER_GAP_MIN_KEY,
    _parse_style_config)
from automations._pins import PINS_ENABLED_KEY, PINS_WRITE_KEY
from automations._daylog import DAY_LOG_DEFAULT, DAY_LOG_ENABLED_KEY

# NUMERIC knobs (not checkboxes): key → (default, max). Persisted as numbers —
# the bool coercion in _persist must never touch these.
_NUMERIC_KNOBS: dict[str, tuple[float, float]] = {
    CAT_STICKER_SKIP_PCT_KEY: (0.0, 100.0),   # % of replies that hide the pack
    CAT_STICKER_SOLO_PCT_KEY: (5.0, 100.0),   # % nudged to a gif-only reply
    CAT_STICKER_GAP_MIN_KEY: (0.0, 7 * 24 * 60.0),  # per-fan minutes between gifs
}


def _clamp_num(key: str, v) -> float:
    default, hi = _NUMERIC_KNOBS[key]
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return default
    return min(max(float(v), 0.0), hi)

log = logging.getLogger("of-relay.style_config_api")

router = APIRouter()


def _defaults() -> dict[str, Any]:
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
    # The space-before-'?' habit. Its own key, same tri-state default as the
    # non-native layer it narrows — see _common.spacing_flag_key.
    out.update({spacing_flag_key(k): _style_default(k) for k in STYLE_AUTOMATIONS})
    # Account-wide (not per-automation): strip every emoji at the send chokepoint.
    out["strip_emojis"] = False
    # PHASE 2 pre-send self-consistency — OFF, and NOT tri-state. Unlike the layers
    # above it costs a second LLM call per reply it fires on, so `load_consistency_flags`
    # requires an explicit True. Reporting a default of False here matches that exactly.
    out.update({k: False for k in STYLE_CONSISTENCY_KEYS})
    # Auto Convo (welcome_chatter_for_info) rich-profile grounding — DEFAULT ON (see load_factground_flag).
    out[FACTGROUND_KEY] = True
    # Account-wide brevity/emotion framing — DEFAULT ON (see load_painful_texting_flag).
    out[PAINFUL_TEXTING_KEY] = True
    # May this creator sell CUSTOMS (paid content recorded to order, delivered
    # here later)? DEFAULT **OFF** — it governs what the bot may promise a paying
    # fan, so it takes an explicit opt-in (see load_sell_customs_flag).
    out[SELL_CUSTOMS_KEY] = False
    # Cat-sticker reaction pack — DEFAULT ON (see load_cat_stickers_flag).
    out[CAT_STICKERS_KEY] = True
    # Sticker rate knobs — numeric (see cat_stickers.load_cat_sticker_config).
    out.update({k: d for k, (d, _) in _NUMERIC_KNOBS.items()})
    # Pins — both OFF, and NOT tri-state (mirrors _pins.load_flags exactly).
    out[PINS_ENABLED_KEY] = False
    out[PINS_WRITE_KEY] = False
    return out


def _resolved_view(stored: dict) -> dict[str, Any]:
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
    out.update({spacing_flag_key(k): _resolve_style_flag(stored, k, spacing_flag_key(k))
                for k in STYLE_AUTOMATIONS})
    out.update({nonnative_flag_key(k): _resolve_style_flag(stored, k, nonnative_flag_key(k))
                for k in STYLE_AUTOMATIONS})
    out["strip_emojis"] = bool(stored.get("strip_emojis"))
    # OFF unless explicitly stored — deliberately NOT _resolve_style_flag, whose
    # tri-state default carries ai_chatter=True and would render this checked (and
    # then bill a second LLM call per reply) on every account that never asked for it.
    # Mirrors load_consistency_flags exactly.
    out.update({k: bool(stored.get(k, False)) for k in STYLE_CONSISTENCY_KEYS})
    # DEFAULT ON: an absent key resolves True (matches load_factground_flag), so the box
    # renders checked and a save doesn't flip an implicit-ON flag to explicit-OFF.
    out[FACTGROUND_KEY] = bool(stored.get(FACTGROUND_KEY, True))
    # DEFAULT ON (matches load_painful_texting_flag): absent → True, so the box renders
    # checked and a save never flips the implicit-ON to explicit-OFF.
    out[PAINFUL_TEXTING_KEY] = bool(stored.get(PAINFUL_TEXTING_KEY, True))
    # DEFAULT OFF unless explicitly stored — deliberately NOT the tri-state
    # default. Mirrors load_sell_customs_flag: a creator who does not make customs
    # must never have the bot agree to one on her behalf.
    out[SELL_CUSTOMS_KEY] = bool(stored.get(SELL_CUSTOMS_KEY, False))
    # DEFAULT ON (matches load_cat_stickers_flag) — same absent → True contract.
    out[CAT_STICKERS_KEY] = bool(stored.get(CAT_STICKERS_KEY, True))
    # Numeric knobs: stored value clamped, absent/garbage → the default.
    out.update({k: _clamp_num(k, stored.get(k)) for k in _NUMERIC_KNOBS})
    # Pins. OFF unless explicitly stored — same contract as the consistency keys,
    # deliberately NOT the tri-state default.
    #
    # Rendered here because a switch you cannot read back is a switch you cannot
    # trust: these keys PERSISTED correctly but the response omitted them, so an
    # operator flipping shadow on saw a body with no `pins_*` in it and had no way
    # to tell a successful save from a silently-dropped key. That matters most for
    # `pins_write`, which is the one that starts mutating OnlyFans.
    out[PINS_ENABLED_KEY] = bool(stored.get(PINS_ENABLED_KEY, False))
    out[PINS_WRITE_KEY] = bool(stored.get(PINS_WRITE_KEY, False))
    # The day log. Same reason as the pins pair above: the operator flipping it on
    # must be able to tell a successful save from a silently-dropped key, and this
    # one is the switch that starts writing a generated day into the chat prompt.
    out[DAY_LOG_ENABLED_KEY] = bool(stored.get(DAY_LOG_ENABLED_KEY, DAY_LOG_DEFAULT))
    return out


def _persist(cfg: dict) -> dict[str, Any]:
    """What we WRITE back. ONLY the keys explicitly present in the merged config —
    absent keys are LEFT ABSENT so they keep resolving to the tri-state default at load
    time. Materializing every absent key to False (the old bug) stamped ai_chatter:false
    on the first save and killed default-on realism for every UI-touched account."""
    if not isinstance(cfg, dict):
        cfg = {}
    # This set is the ONLY gate on what reaches style_config_json — a key absent
    # from it is dropped SILENTLY on a 200, so a loader reading that key can never
    # see True and its feature ships inert. `consistency_*` was exactly that: the
    # PHASE 2 pre-send check had a loader, an LLM call, tests and a drawer panel,
    # and no way to switch on. When you add a `load_*_flags` reader, add its keys
    # HERE in the same commit; `test_style_config.case_every_loader_key_survives_persist`
    # fails the build if a loader's key set is not a subset of this one.
    known = set(STYLE_AUTOMATIONS) \
        | {typo_flag_key(k) for k in STYLE_AUTOMATIONS} \
        | {nonnative_flag_key(k) for k in STYLE_AUTOMATIONS} \
        | {spacing_flag_key(k) for k in STYLE_AUTOMATIONS} \
        | set(STYLE_CONSISTENCY_KEYS) \
        | {"strip_emojis", FACTGROUND_KEY, PAINFUL_TEXTING_KEY, SELL_CUSTOMS_KEY,
           CAT_STICKERS_KEY,
           PINS_ENABLED_KEY, PINS_WRITE_KEY,
           DAY_LOG_ENABLED_KEY}
    out: dict[str, Any] = {k: bool(v) for k, v in cfg.items() if k in known}
    # Numeric knobs keep their number — bool() would turn "skip 30%" into True.
    out.update({k: _clamp_num(k, cfg[k]) for k in _NUMERIC_KNOBS if k in cfg})
    return out


@router.get("/admin/style-config")
async def get_style_config(account_id: str = Query(...)) -> dict[str, Any]:
    assert_account_owned(account_id)
    async with get_session() as s:
        row = await s.get(AccountAiConfig, account_id)
    # THE parse chokepoint (legacy-key normalization lives in it) — a raw
    # json.loads here would show a pre-rename blob's flags as absent.
    stored = _parse_style_config(row.style_config_json) if row is not None else {}
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
        # Parse through the chokepoint: a pre-rename blob's legacy keys are
        # normalized to the new names HERE, before _persist's known-key filter
        # — a raw json.loads would let the first save silently destroy them.
        stored = _parse_style_config(row.style_config_json) if row is not None else {}
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
