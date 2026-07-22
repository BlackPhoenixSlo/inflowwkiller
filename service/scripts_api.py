"""service/scripts_api.py — the ai_chatter content catalog + config + monitor API.

Everything the Scripts UI needs, owner-gated, all under /admin (covered by the
Next `/admin/:path*` rewrite — no next.config.ts change):

  GET  /admin/ai-chatter-config?account_id=     → {config, defaults, timezone,
                                                  utc_offset, tz_offset_minutes,
                                                  derived_sleep_window,
                                                  effective_sleep_window,
                                                  script_pack}
  PUT  /admin/ai-chatter-config                 → validate + upsert the JSON
                                                  (+ optional `timezone` column)
  GET  /admin/scripts?account_id=               → scripts(+items+stats) + singles
  POST /admin/scripts                           → create/rename a script
  DELETE /admin/scripts/{script_id}?account_id= → delete (items cascade)
  PUT  /admin/scripts/{script_id}/items         → REPLACE the script's items
  PUT  /admin/catalog/singles                   → REPLACE the singles
  POST /admin/scripts/import-folder             → live vault read → append items
  POST /admin/scripts/paste-import              → Notion-style table → items
  POST /admin/scripts/simulate                  → dry prompt+LLM, returns the
                                                  bubbles + parsed offer
  GET  /admin/ai-chatter/sessions?account_id=   → progress pins + open offers
  POST /admin/ai-chatter/offers/{id}/cancel     → chatter kill switch

Items are saved wholesale per collection (the editor owns the full ordered
list) — position is the array index. `description_for_ai` is the pitch
contract: what the fan actually sees, present tense.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import delete as sa_delete, func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

import automation_executor as ax
import llm_client
from auth import assert_account_owned
from db.engine import get_session
from db.models import (
    AccountAiConfig, AutomationRule, CatalogItem, CatalogProgress, CatalogScript,
    ContentOffer, Fan, Message, created_at_text, parse_ts,
)
from automations import rhythm, script_packs
from automations.ai_chatter import (
    _CONTENT_ASK_RE,
    _DEFAULTS as _AI_CHATTER_DEFAULTS,
    _build_messages, _load_catalog, _load_persona, _manifest_block,
    _offerable_for_fan, _parse_offer_marker,
)
from automations.of_ai_chat import split_for_bubbles

log = logging.getLogger("of-relay.scripts_api")

router = APIRouter()

_INT_KNOBS = {
    "sla_minutes": (0, 1440),
    "max_lifetime_spend_cents": (0, 100_000_000),
    "max_offers_per_fan_per_day": (0, 50),
    "min_fan_msgs_between_offers": (0, 100),
    "min_fan_msgs_before_escalation_pitch": (0, 100),
    "max_fans_per_tick": (1, 100),
    "resume_after_manual_hours": (0, 168),
    "stall_ttl_hours": (1, 720),
    # Cadence controller (items 10/17/18/21).
    "session_gap_minutes": (5, 1440),
    "post_purchase_minutes": (0, 1440),
    "offer_expiry_minutes": (0, 10_080),
    "nudge_after_minutes": (1, 1440),
    # Old-fan engagement: ~one info question per this many replies.
    "old_fan_question_every": (1, 100),
    # Upsell "after a buy" knobs.
    "stop_after_unpaid_rungs": (1, 5),
    "spend_velocity_cap_7d_cents": (0, 100_000_000),
    # Tip ladder: the opening ask, the never-go-below floor, and the ceiling on
    # an escalated ask. Floor/cap are the guard rails the ×step walk runs between.
    "tip_ladder_base_cents": (100, 100_000),
    "tip_ladder_floor_cents": (100, 100_000),
    "tip_ladder_cap_cents": (100, 1_000_000),
    # How many asks may sit unanswered at once (runtime floors this at 2).
    "max_open_offers": (1, 10),
}

# Float-valued knobs (ladder aggressiveness). Clamped to sane ranges: the ceiling
# knob may exceed 3x (the "to the moon" lever), but not unbounded.
_FLOAT_KNOBS = {
    "escalation_mult": (1.0, 10.0),
    "max_ask_history_mult": (1.0, 20.0),
    # Tip ladder escalation: ×step after each tip he pays, and the cut band that
    # decides how far the next ask backs off when he doesn't.
    "tip_ladder_step": (1.0, 5.0),
    "tip_ladder_cut_lo": (0.0, 1.0),
    "tip_ladder_cut_hi": (0.0, 1.0),
    # Proven-spend floor: never ask below his biggest single paid × this.
    "proven_spend_floor_mult": (0.0, 2.0),
    # Discounts. Capped well under 1.0 — a 100%-off "sale" is a giveaway, not a nudge.
    "haggle_discount_pct": (0.0, 0.9),
    "teaser_discount_pct": (0.0, 0.9),
}
_MODES = ("backup", "always")
_OFFER_MODES = ("tip", "ppv", "both")
# msg_limits_by_signal is a NESTED object; validate its four tier caps against the
# built-in defaults (single source of truth) and always emit the COMPLETE dict so
# _load_config's shallow merge can never drop a tier. 0 = unlimited (see _cadence_gate).
_MSG_LIMIT_DEFAULTS: dict = dict(_AI_CHATTER_DEFAULTS["msg_limits_by_signal"])
_MSG_LIMIT_MAX = 500
_MAX_ITEMS = 60
_TXT = 2000

# Editable line pack: bounds on what the UI may store per slot.
_MAX_PACK_LINES = 30
_PACK_LINE_MAX = 400
# How many recent outbound rows the sleep-window derivation reads. The histogram
# only needs a SHAPE, and derive_sleep_window ignores anything under 200 samples
# anyway — an unbounded scan here would starve the relay's thread pool.
_HOUR_HIST_LIMIT = 5000


def _validate_sleep_window(raw: Any) -> list[str] | None:
    """None ⇒ derive it from the account's own history. A 2-tuple of "HH:MM"
    strings ⇒ the operator overrode it in Advanced."""
    if raw is None:
        return None
    if not isinstance(raw, (list, tuple)) or len(raw) != 2:
        raise HTTPException(422, "sleep_window must be null or two HH:MM strings")
    out: list[str] = []
    for v in raw:
        s = str(v or "").strip()
        h, _, m = s.partition(":")
        try:
            hh, mm = int(h), int(m or 0)
        except (TypeError, ValueError):
            raise HTTPException(422, f"bad sleep_window time: {s!r}")
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            raise HTTPException(422, f"bad sleep_window time: {s!r}")
        out.append(f"{hh:02d}:{mm:02d}")
    if out[0] == out[1]:
        raise HTTPException(422, "sleep_window start and end can't be the same")
    return out


def _validate_pack_overrides(raw: Any) -> dict[str, list[str]]:
    """{slot: [lines]} over the SHIPPED slots only. A slot whose lines are all
    blank is DROPPED, not stored empty — script_packs.render falls back to the
    shipped default for a missing slot, so an empty override can never turn into
    an empty message."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise HTTPException(422, "script_pack_overrides must be an object")
    known = set(script_packs.slots())
    out: dict[str, list[str]] = {}
    for slot, lines in raw.items():
        if slot not in known:
            continue                     # a renamed/removed slot is dropped, not an error
        if not isinstance(lines, (list, tuple)):
            raise HTTPException(422, f"script_pack_overrides.{slot} must be a list")
        clean = [str(x).strip()[:_PACK_LINE_MAX]
                 for x in lines[:_MAX_PACK_LINES] if str(x).strip()]
        if clean:
            out[slot] = clean
    return out


# ── ai_chatter config (mirrors tip_reward_config_api) ────────────────────────

def _validate_cfg(cfg: dict) -> dict:
    if not isinstance(cfg, dict):
        raise HTTPException(422, "config must be an object")
    out: dict[str, Any] = {}
    if "enabled" in cfg:
        out["enabled"] = bool(cfg["enabled"])
    if "intent_only" in cfg:
        out["intent_only"] = bool(cfg["intent_only"])
    if "engage_old_fans" in cfg:
        out["engage_old_fans"] = bool(cfg["engage_old_fans"])
    if "pivot_on_escalation" in cfg:
        out["pivot_on_escalation"] = bool(cfg["pivot_on_escalation"])
    if "unsend_expired_offer" in cfg:
        out["unsend_expired_offer"] = bool(cfg["unsend_expired_offer"])
    if "cadence_enabled" in cfg:
        out["cadence_enabled"] = bool(cfg["cadence_enabled"])
    if "nudge_enabled" in cfg:
        out["nudge_enabled"] = bool(cfg["nudge_enabled"])
    # 1:1 offer engine + human pacing + the editable line pack (all ship OFF).
    if "qualification_gate_enabled" in cfg:
        out["qualification_gate_enabled"] = bool(cfg["qualification_gate_enabled"])
    # force_ask: attach the ask when the THREAD IS HOT and the model wrote no offer
    # marker. Inert without the gate (guarded at runtime), and it only ever fires on
    # thread_heat — a 24.3x signal, vs the gate's ~1x.
    if "force_ask" in cfg:
        out["force_ask"] = bool(cfg["force_ask"])
    # The floor: ask after this many of his messages with no ask on the table. 0 = off.
    # Clamped — a 1 here would price a man on his first hello.
    if "ask_after_fan_msgs" in cfg:
        out["ask_after_fan_msgs"] = max(0, min(100, int(cfg["ask_after_fan_msgs"] or 0)))
    if "smart_pricing_enabled" in cfg:
        out["smart_pricing_enabled"] = bool(cfg["smart_pricing_enabled"])
    if "rhythm_enabled" in cfg:
        out["rhythm_enabled"] = bool(cfg["rhythm_enabled"])
    if "rhythm_no_sleep" in cfg:
        out["rhythm_no_sleep"] = bool(cfg["rhythm_no_sleep"])
    # v2/v3 upsell lane — the hard takeover + the "after a buy" behaviors. All are
    # gated by qualification_gate_enabled at RUNTIME, so storing True with the gate
    # off is inert; we still persist the operator's choice.
    if "upsell_takes_over" in cfg:
        out["upsell_takes_over"] = bool(cfg["upsell_takes_over"])
    if "post_buy_rung_enabled" in cfg:
        out["post_buy_rung_enabled"] = bool(cfg["post_buy_rung_enabled"])
    if "gift_enabled" in cfg:
        out["gift_enabled"] = bool(cfg["gift_enabled"])
    if "filming_stall_enabled" in cfg:
        out["filming_stall_enabled"] = bool(cfg["filming_stall_enabled"])
    # Tip ladder + proven-spend floor + the hot-lead trinity. All ship OFF; the
    # numeric knobs for each ride in _INT_KNOBS / _FLOAT_KNOBS below.
    if "tip_ladder_enabled" in cfg:
        out["tip_ladder_enabled"] = bool(cfg["tip_ladder_enabled"])
    if "proven_spend_floor_enabled" in cfg:
        out["proven_spend_floor_enabled"] = bool(cfg["proven_spend_floor_enabled"])
    if "hotsell_trinity_enabled" in cfg:
        out["hotsell_trinity_enabled"] = bool(cfg["hotsell_trinity_enabled"])
    # Smart pricing without the gate is meaningless (there is no ladder to price),
    # and letting the pair drift apart would be a state the UI can't represent.
    if out.get("smart_pricing_enabled") and not out.get(
            "qualification_gate_enabled", cfg.get("qualification_gate_enabled")):
        out["smart_pricing_enabled"] = False
    if "sleep_window" in cfg:
        out["sleep_window"] = _validate_sleep_window(cfg["sleep_window"])
    if "script_pack_overrides" in cfg:
        out["script_pack_overrides"] = _validate_pack_overrides(cfg["script_pack_overrides"])
    if "mode" in cfg and cfg["mode"] is not None:
        if cfg["mode"] not in _MODES:
            raise HTTPException(422, f"mode must be one of {_MODES}")
        out["mode"] = cfg["mode"]
    if "offer_mode" in cfg and cfg["offer_mode"] is not None:
        if cfg["offer_mode"] not in _OFFER_MODES:
            raise HTTPException(422, f"offer_mode must be one of {_OFFER_MODES}")
        out["offer_mode"] = cfg["offer_mode"]
    for k, (lo, hi) in _INT_KNOBS.items():
        if k in cfg and cfg[k] is not None:
            try:
                out[k] = max(lo, min(int(cfg[k]), hi))
            except (TypeError, ValueError):
                raise HTTPException(422, f"{k} must be a number")
    for k, (lo, hi) in _FLOAT_KNOBS.items():
        if k in cfg and cfg[k] is not None:
            try:
                out[k] = max(lo, min(float(cfg[k]), hi))
            except (TypeError, ValueError):
                raise HTTPException(422, f"{k} must be a number")
    ml = cfg.get("msg_limits_by_signal")
    if ml is not None:
        if not isinstance(ml, dict):
            raise HTTPException(422, "msg_limits_by_signal must be an object")
        merged = dict(_MSG_LIMIT_DEFAULTS)
        for tier in _MSG_LIMIT_DEFAULTS:
            if ml.get(tier) is not None:
                try:
                    merged[tier] = max(0, min(int(ml[tier]), _MSG_LIMIT_MAX))
                except (TypeError, ValueError):
                    raise HTTPException(422, f"msg_limits_by_signal.{tier} must be a number")
        out["msg_limits_by_signal"] = merged   # always complete → shallow-merge safe
    return out


async def _outbound_hour_histogram(account_id: str,
                                   tz_offset_minutes: int | None) -> dict[int, int]:
    """Hour-of-day histogram of our OWN outbound messages, in CREATOR-LOCAL hours.

    Local, not UTC: a US creator's 3am dip sits at 08:00 UTC, and a window derived
    in UTC would put her to sleep through her peak. Bounded scan (the derivation
    only needs a shape, and it discards anything under 200 samples).

    Reads created_at as TEXT (created_at_text) and parses it here: the None-guard
    below used to cover only a SQL NULL, but a created_at of '' is not NULL — it
    made SQLAlchemy raise `Invalid isoformat string: ''` while materialising the
    rows, i.e. before this loop ever ran, 500-ing the whole Rhythm view. A
    histogram is a SHAPE; a handful of unreadable rows can simply be left out."""
    async with get_session() as s:
        rows = (await s.execute(
            select(created_at_text())
            .where(Message.account_id == account_id, Message.direction == "out")
            .order_by(Message.created_at.desc())
            .limit(_HOUR_HIST_LIMIT))).scalars().all()
    counts: dict[int, int] = {h: 0 for h in range(24)}
    bad = 0
    for raw in rows:
        ts = parse_ts(raw)
        if ts is None:
            if raw is not None:
                bad += 1            # unreadable, as opposed to simply absent
            continue
        counts[rhythm.local_now(ts, tz_offset_minutes).hour] += 1
    if bad:
        log.warning("rhythm histogram[%s]: skipped %d outbound row(s) with an "
                    "unreadable created_at", account_id, bad)
    return counts


async def _rhythm_view(account_id: str, row: AccountAiConfig | None,
                       stored: dict) -> dict[str, Any]:
    """Everything the Human Rhythm UI needs, RESOLVED server-side — the operator is
    never asked a question the data already answers."""
    tz = getattr(row, "timezone", None) if row else None
    utc_offset = int(getattr(row, "utc_offset", 0) or 0) if row else 0
    tz_off = rhythm.tz_offset_for(tz, utc_offset)
    derived = rhythm.derive_sleep_window(
        await _outbound_hour_histogram(account_id, tz_off))
    override = stored.get("sleep_window")
    effective = list(override) if isinstance(override, (list, tuple)) and len(override) == 2 \
        else list(derived)
    return {
        "timezone": tz,
        "utc_offset": utc_offset,
        # None ⇒ neither a timezone nor a non-zero utc_offset is set. Rhythm stays
        # OFF for the account in that state (rhythm.tz_offset_for), so the UI blocks
        # the checkbox instead of letting her sleep through her best hours.
        "tz_offset_minutes": tz_off,
        "derived_sleep_window": list(derived),
        "effective_sleep_window": effective,
        "default_sleep_window": list(rhythm.DEFAULT_SLEEP),
    }


@router.get("/admin/ai-chatter-config")
async def get_ai_chatter_config(account_id: str = Query(...)) -> dict[str, Any]:
    assert_account_owned(account_id)
    async with get_session() as s:
        row = await s.get(AccountAiConfig, account_id)
        if row is not None:
            s.expunge_all()
    stored: dict = {}
    if row is not None and row.ai_chatter_config_json:
        try:
            stored = json.loads(row.ai_chatter_config_json) or {}
        except Exception:
            stored = {}
    return {
        "account_id": account_id,
        "config": stored,
        "defaults": dict(_AI_CHATTER_DEFAULTS),
        # The shipped lines, PRE-FILLED in the editor. An account with no config
        # row at all still gets the full pack — that is the "plug in your content
        # and go" surface: the words are already written.
        "script_pack": {slot: list(lines) for slot, lines in script_packs.PACK.items()},
        **await _rhythm_view(account_id, row, stored),
    }


class _ConfigBody(BaseModel):
    account_id: str
    config: dict
    # The creator's IANA timezone — a COLUMN, not part of the config blob (rhythm,
    # welcome and the daily rollover all read it). Absent ⇒ leave it alone; ""  ⇒
    # clear it back to NULL.
    timezone: str | None = None


@router.put("/admin/ai-chatter-config")
async def put_ai_chatter_config(body: _ConfigBody = Body(...)) -> dict[str, Any]:
    assert_account_owned(body.account_id)
    clean = _validate_cfg(body.config)
    tz: str | None = None
    if body.timezone is not None:
        tz = body.timezone.strip() or None
        if tz:
            try:
                from zoneinfo import ZoneInfo
                ZoneInfo(tz)
            except Exception:
                raise HTTPException(422, f"unknown timezone: {tz!r}")
    now = datetime.utcnow()
    payload = json.dumps(clean)
    async with get_session() as s:
        values: dict[str, Any] = {"ai_chatter_config_json": payload, "updated_at": now}
        if body.timezone is not None:
            values["timezone"] = tz
        await s.execute(
            sqlite_insert(AccountAiConfig)
            .values(account_id=body.account_id, utc_offset=0, **values)
            .on_conflict_do_update(index_elements=["account_id"], set_=values)
        )
    if clean.get("enabled"):
        # Go-live safety: an enabled AI Seller with NO sweep rule is a silent
        # dead bot in backup mode (only W7 wakes would ever fire it). Ensure
        # the fallback rule exists once — never duplicated, and an owner who
        # later disables the rule on purpose is respected.
        async with get_session() as s:
            existing = (await s.execute(
                select(AutomationRule.id).where(
                    AutomationRule.account_id == body.account_id,
                    AutomationRule.kind == "ai_chatter").limit(1))).first()
            if existing is None:
                s.add(AutomationRule(
                    account_id=body.account_id, name="AI Seller sweep",
                    kind="ai_chatter",
                    trigger_json=json.dumps({"every_seconds": 60}),
                    steps_json="{}", is_enabled=True,
                    created_at=datetime.utcnow()))
                log.info("ai_chatter_sweep_rule_created account=%s", body.account_id)
    log.info("ai_chatter_config_saved account=%s tz=%s cfg=%s",
             body.account_id, tz, clean)
    async with get_session() as s:
        row = await s.get(AccountAiConfig, body.account_id)
        if row is not None:
            s.expunge_all()
    return {"account_id": body.account_id, "config": clean,
            **await _rhythm_view(body.account_id, row, clean)}


# ── vault-ai config (account_ai_config.vault_ai_config_json) ─────────────────
# Shape frozen in plans/VAULT_AI_ACTIONS_CONTRACT.md §1. Absent/NULL → these
# defaults, master OFF. Own the whole blob (no shallow-merge with other
# `*_config_json`). GET returns the EFFECTIVE view (defaults + stored); PATCH
# deep-merges a partial into the stored blob and returns the new effective view.

_VAULT_AI_DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "suggest_only": True,
    "models": {
        "describe": "qwen3-vl-30b",
        "escalation": "qwen3-vl-235b",
        "caption": "deepseek-chat",
        "script": "deepseek-chat",
        "escalate_below_confidence": 65,
    },
    "describe": {
        "cadence_hours": 6,
        "describe_all_cap_percent": 80,
        "max_items_per_run": 40,
        "images": True,
        "videos": True,
    },
    "pricing": {
        "enabled": True,
        "bands_by_tier": {
            "safe":       [300, 800],
            "suggestive": [500, 1500],
            "explicit":   [1000, 3000],
            "graphic":    [2000, 6000],
            "unknown":    [500, 1500],
        },
    },
    "tier_labels": {
        "safe": "Safe", "suggestive": "Suggestive", "explicit": "Explicit",
        "graphic": "Graphic", "unknown": "Unclassified",
    },
    "folders": {
        "mode": "internal",
        "taxonomy": [],
        "max_folders_per_item": 3,
    },
    "scoring": {"story": True, "tip": True},
    "daily_reminder": {
        "enabled": False,
        "auto_post": False,
        "auto_mass_message": False,
        "folder_name": "",
        "lines": [],
        "images_per_day": 2,
        "on_under_min_unseen": "use_fewer",
        "repeat_after_days": 30,
        "per_fan_cooldown_hours": 48,
        "daily_at": ["10:00"],
        "tz_offset_minutes": 0,
    },
    # ── PPV week/month arc (service/vault_ppv_week.py) ────────────────────────
    # Assembles a story-shaped WEEK (soft→payoff, copy references yesterday) from
    # already-derived vault content, repeated as exclusive waves across the month.
    # Every day hits the feed AND DMs (combine). Still suggest-only: the operator
    # generates a preview and confirms before anything is armed.
    "ppv_week": {
        "enabled": False,          # arc automation on (still nothing auto-sends)
        "weeks": 4,                # 1 = a single week, 4 = a month of waves
        "combine_feed_and_dm": True,   # each drop = paid post (feed) + mass PPV (DM)
        "in_voice_copy": True,     # captions written via PAINFUL_TEXTING, not template
    },
}


def _clone_defaults() -> dict:
    return json.loads(json.dumps(_VAULT_AI_DEFAULTS))


def _deep_merge(dst: dict, src: dict) -> dict:
    """In-place recursive dict merge; non-dict values (incl. lists) REPLACE.

    Lists replace so a taxonomy edit / band change stores exactly what the
    operator typed (a per-index merge would trap old items in an updated list).
    """
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _deep_merge(dst[k], v)
        else:
            dst[k] = v
    return dst


def _load_vault_ai_stored(row: AccountAiConfig | None) -> dict:
    if row is None or not row.vault_ai_config_json:
        return {}
    try:
        val = json.loads(row.vault_ai_config_json)
    except (TypeError, ValueError):
        return {}
    return val if isinstance(val, dict) else {}


def _effective_vault_ai(stored: dict) -> dict:
    """Defaults + operator overrides = the blob the UI + consumers see."""
    return _deep_merge(_clone_defaults(), stored or {})


@router.get("/admin/account-ai-config/vault-ai")
async def get_vault_ai_config(account_id: str = Query(...)) -> dict[str, Any]:
    assert_account_owned(account_id)
    async with get_session() as s:
        row = await s.get(AccountAiConfig, account_id)
    return {"account_id": account_id, "config": _effective_vault_ai(_load_vault_ai_stored(row))}


class _VaultAiPatchBody(BaseModel):
    account_id: str
    config: dict


@router.patch("/admin/account-ai-config/vault-ai")
async def patch_vault_ai_config(body: _VaultAiPatchBody = Body(...)) -> dict[str, Any]:
    assert_account_owned(body.account_id)
    if not isinstance(body.config, dict):
        raise HTTPException(422, "config must be an object")
    now = datetime.utcnow()
    async with get_session() as s:
        row = await s.get(AccountAiConfig, body.account_id)
        stored = _load_vault_ai_stored(row)
        _deep_merge(stored, body.config)
        payload = json.dumps(stored)
        await s.execute(
            sqlite_insert(AccountAiConfig)
            .values(account_id=body.account_id, utc_offset=0,
                    vault_ai_config_json=payload, updated_at=now)
            .on_conflict_do_update(
                index_elements=["account_id"],
                set_={"vault_ai_config_json": payload, "updated_at": now})
        )
    log.info("vault_ai_config_patched account=%s keys=%s",
             body.account_id, sorted(body.config.keys()))
    return {"account_id": body.account_id, "config": _effective_vault_ai(stored)}


@router.get("/admin/vault-ai/ppv-week")
async def generate_vault_ppv_week(
    account_id: str = Query(...),
    weeks: int = Query(4, ge=1, le=4),
    use_llm: bool = Query(False),
    combine: bool | None = Query(None),
) -> dict[str, Any]:
    """Build a WEEK (weeks=1) or MONTH (weeks>1) escalation arc from the account's
    already-described vault and return it as a self-contained HTML review page.

    Read-only and suggest-only — nothing is armed or sent. `use_llm=true` writes
    every caption in-voice (PAINFUL_TEXTING); it makes ~7·weeks model calls, so
    the operator opts into the slower path deliberately. The account's Vault-AI
    config (price bands + combine toggle) drives the plan.
    """
    assert_account_owned(account_id)
    import vault_ppv_week as vw

    async with get_session() as s:
        row = await s.get(AccountAiConfig, account_id)
    config = _effective_vault_ai(_load_vault_ai_stored(row))
    weeks = max(1, min(int(weeks or 4), 4))
    if combine is not None:
        config.setdefault("ppv_week", {})["combine_feed_and_dm"] = bool(combine)

    if weeks == 1:
        plan = await vw.build_week(account_id, config=config, use_llm=bool(use_llm))
        html = vw.render_week_html(plan, title=f"Vault PPV week · {account_id}")
        coverage = plan["coverage"]
    else:
        plan = await vw.build_month(account_id, weeks=weeks, config=config,
                                    use_llm=bool(use_llm))
        html = vw.render_month_html(plan, title=f"Vault PPV month · {account_id}")
        coverage = plan["weeks"][0]["coverage"] if plan.get("weeks") else {}

    return {"account_id": account_id, "weeks": weeks,
            "summary": plan["summary"], "coverage": coverage, "html": html}


# ── PPV arc arming (the one place suggest-only is spent, on an explicit confirm)

class _ArcApproveBody(BaseModel):
    account_id: str
    weeks: int = 1
    use_llm: bool = False
    combine: bool | None = None


@router.post("/admin/vault-ai/ppv-week/approve")
async def approve_vault_ppv_arc(body: _ArcApproveBody = Body(...)) -> dict[str, Any]:
    """Arm the arc the operator just reviewed: book every day's drop as its own
    future-dated job so the week runs itself, then stop when it runs out.

    The plan is REBUILT here rather than accepted from the client — the browser
    only ever received rendered HTML, and taking media ids and prices back from a
    request body would make "what was approved" forgeable. Same account config and
    same exclusivity rules as the preview, so what was read is what is armed."""
    assert_account_owned(body.account_id)
    import vault_arc
    import vault_ppv_week as vw

    async with get_session() as s:
        row = await s.get(AccountAiConfig, body.account_id)
    config = _effective_vault_ai(_load_vault_ai_stored(row))
    if not config.get("enabled"):
        raise HTTPException(409, "vault_ai_off")
    weeks = max(1, min(int(body.weeks or 1), 4))
    if body.combine is not None:
        config.setdefault("ppv_week", {})["combine_feed_and_dm"] = bool(body.combine)

    if weeks == 1:
        plan = await vw.build_week(body.account_id, config=config,
                                   use_llm=bool(body.use_llm))
    else:
        plan = await vw.build_month(body.account_id, weeks=weeks, config=config,
                                    use_llm=bool(body.use_llm))

    res = await vault_arc.approve(body.account_id, plan)
    if res.get("status") == "refused":
        raise HTTPException(409, res.get("reason") or "refused")
    log.info("vault_arc_armed account=%s weeks=%d drops=%d",
             body.account_id, weeks, len(res.get("drops") or []))
    return res


@router.get("/admin/vault-ai/ppv-week/status")
async def vault_ppv_arc_status(account_id: str = Query(...)) -> dict[str, Any]:
    """What the armed arc is doing — live job state per drop. Read-only."""
    assert_account_owned(account_id)
    import vault_arc
    return await vault_arc.status(account_id)


@router.post("/admin/vault-ai/ppv-week/cancel")
async def cancel_vault_ppv_arc(account_id: str = Body(..., embed=True)) -> dict[str, Any]:
    """Stand the arc down — every drop that has not fired yet is dropped. Drops
    already sent are history and stay sent."""
    assert_account_owned(account_id)
    import vault_arc
    return await vault_arc.cancel(account_id)


# ── Catalog read (scripts + singles + per-item conversion stats) ─────────────

def _item_out(it: CatalogItem, stats: dict[int, dict]) -> dict:
    return {
        "id": int(it.id), "script_id": it.script_id, "position": it.position,
        "kind": it.kind, "label": it.label,
        "description_for_ai": it.description_for_ai,
        "media_ids": json.loads(it.media_ids or "[]"),
        "preview_media_ids": json.loads(it.preview_media_ids or "[]"),
        "duration_sec": it.duration_sec, "price_cents": int(it.price_cents or 0),
        "tip_unlock_cents": int(it.tip_unlock_cents or 0),
        "is_free_teaser": bool(it.is_free_teaser),
        "tags": json.loads(it.tags or "[]"), "enabled": bool(it.enabled),
        "stats": stats.get(int(it.id), {"offers": 0, "delivered": 0}),
    }


@router.get("/admin/scripts")
async def list_scripts(account_id: str = Query(...)) -> dict[str, Any]:
    assert_account_owned(account_id)
    async with get_session() as s:
        scripts = (await s.execute(
            select(CatalogScript).where(CatalogScript.account_id == account_id)
            .order_by(CatalogScript.id))).scalars().all()
        items = (await s.execute(
            select(CatalogItem).where(CatalogItem.account_id == account_id)
            .order_by(CatalogItem.script_id, CatalogItem.position, CatalogItem.id)
        )).scalars().all()
        offer_rows = (await s.execute(
            select(ContentOffer.item_id, ContentOffer.status,
                   func.count()).where(ContentOffer.account_id == account_id)
            .group_by(ContentOffer.item_id, ContentOffer.status))).all()
        s.expunge_all()
    stats: dict[int, dict] = {}
    for item_id, status, n in offer_rows:
        d = stats.setdefault(int(item_id), {"offers": 0, "delivered": 0})
        d["offers"] += int(n)
        if status == "delivered":
            d["delivered"] += int(n)
    return {
        "account_id": account_id,
        "scripts": [{
            "id": int(sc.id), "name": sc.name, "theme": sc.theme,
            "status": sc.status,
            "items": [_item_out(it, stats) for it in items
                      if it.script_id == sc.id],
        } for sc in scripts],
        "singles": [_item_out(it, stats) for it in items if it.script_id is None],
    }


# ── Script create/update/delete ──────────────────────────────────────────────

class _ScriptBody(BaseModel):
    account_id: str
    id: int | None = None
    name: str
    theme: str | None = None
    status: str = "draft"


@router.post("/admin/scripts")
async def upsert_script(body: _ScriptBody = Body(...)) -> dict[str, Any]:
    assert_account_owned(body.account_id)
    if body.status not in ("draft", "enabled", "disabled"):
        raise HTTPException(422, "status must be draft|enabled|disabled")
    name = body.name.strip()[:80]
    if not name:
        raise HTTPException(422, "name required")
    now = datetime.utcnow()
    async with get_session() as s:
        if body.id is not None:
            sc = await s.get(CatalogScript, int(body.id))
            if sc is None or sc.account_id != body.account_id:
                raise HTTPException(404, "script not found")
            sc.name, sc.theme, sc.status = name, (body.theme or "")[:_TXT], body.status
            sc.updated_at = now
            sid = int(sc.id)
        else:
            sc = CatalogScript(account_id=body.account_id, name=name,
                               theme=(body.theme or "")[:_TXT],
                               status=body.status, created_at=now, updated_at=now)
            s.add(sc)
            await s.flush()
            sid = int(sc.id)
    return {"id": sid, "status": "ok"}


@router.delete("/admin/scripts/{script_id}")
async def delete_script(script_id: int, account_id: str = Query(...)) -> dict[str, Any]:
    assert_account_owned(account_id)
    async with get_session() as s:
        sc = await s.get(CatalogScript, int(script_id))
        if sc is None or sc.account_id != account_id:
            raise HTTPException(404, "script not found")
        await s.execute(sa_delete(CatalogItem).where(
            CatalogItem.account_id == account_id,
            CatalogItem.script_id == int(script_id)))
        await s.delete(sc)
    return {"status": "ok"}


# ── Items (wholesale replace per collection) ─────────────────────────────────

def _validate_item(raw: Any) -> dict:
    if not isinstance(raw, dict):
        raise HTTPException(422, "each item must be an object")
    def _ints(key):
        v = raw.get(key) or []
        if not isinstance(v, (list, tuple)):
            raise HTTPException(422, f"{key} must be a list")
        out = []
        for x in v:
            try:
                out.append(int(x))
            except (TypeError, ValueError):
                raise HTTPException(422, f"{key} entries must be ints")
        return out
    kind = str(raw.get("kind") or "video")
    if kind not in ("video", "image", "image_set"):
        raise HTTPException(422, "kind must be video|image|image_set")
    def _cents(key):
        try:
            return max(0, min(int(raw.get(key) or 0), 100_000_00))
        except (TypeError, ValueError):
            raise HTTPException(422, f"{key} must be a number")
    media = _ints("media_ids")
    previews = [m for m in _ints("preview_media_ids") if m in media]
    dur = raw.get("duration_sec")
    try:
        dur = max(0, int(dur)) if dur is not None else None
    except (TypeError, ValueError):
        dur = None
    return {
        "kind": kind, "label": str(raw.get("label") or "").strip()[:80] or None,
        "description_for_ai": str(raw.get("description_for_ai") or "").strip()[:_TXT] or None,
        "media_ids": json.dumps(media), "preview_media_ids": json.dumps(previews),
        "duration_sec": dur, "price_cents": _cents("price_cents"),
        "tip_unlock_cents": _cents("tip_unlock_cents"),
        "is_free_teaser": bool(raw.get("is_free_teaser")),
        "tags": json.dumps([str(t)[:40] for t in (raw.get("tags") or [])][:12]),
        "enabled": bool(raw.get("enabled", True)),
    }


class _ItemsBody(BaseModel):
    account_id: str
    items: list[dict]


async def _replace_items(account_id: str, script_id: int | None,
                         items: list[dict]) -> int:
    if len(items) > _MAX_ITEMS:
        raise HTTPException(422, f"too many items (max {_MAX_ITEMS})")
    clean = [_validate_item(it) for it in items]
    now = datetime.utcnow()
    async with get_session() as s:
        cond = (CatalogItem.account_id == account_id,
                CatalogItem.script_id == script_id if script_id is not None
                else CatalogItem.script_id.is_(None))
        await s.execute(sa_delete(CatalogItem).where(*cond))
        for i, it in enumerate(clean):
            s.add(CatalogItem(account_id=account_id, script_id=script_id,
                              position=i if script_id is not None else None,
                              created_at=now, updated_at=now, **it))
    return len(clean)


@router.put("/admin/scripts/{script_id}/items")
async def put_script_items(script_id: int, body: _ItemsBody = Body(...)) -> dict:
    assert_account_owned(body.account_id)
    async with get_session() as s:
        sc = await s.get(CatalogScript, int(script_id))
        if sc is None or sc.account_id != body.account_id:
            raise HTTPException(404, "script not found")
    n = await _replace_items(body.account_id, int(script_id), body.items)
    return {"status": "ok", "items": n}


@router.put("/admin/catalog/singles")
async def put_singles(body: _ItemsBody = Body(...)) -> dict:
    assert_account_owned(body.account_id)
    n = await _replace_items(body.account_id, None, body.items)
    return {"status": "ok", "items": n}


@router.get("/admin/catalog/singles/suggest")
async def suggest_singles_content(
    account_id: str = Query(...),
    only_empty: bool = Query(True),
) -> dict[str, Any]:
    """Propose vault media for singles that have TEXT but no CONTENT.

    Read-only and free — no LLM, no OF traffic; the match is derived from each
    row's own `label`/`description_for_ai` against the stored V2 describe
    fields. Nothing is saved: the UI patches the rows it likes and the operator
    still presses Save, so a proposal can always be thrown away.

    `only_empty=false` re-proposes over rows that already carry media, for
    comparison — it still does not write.
    """
    import vault_solo_fill

    assert_account_owned(account_id)
    res = await vault_solo_fill.suggest_singles(account_id, only_empty=only_empty)
    # `items` carries whole vault rows (describe blobs); the client only needs
    # the ids, and the thumbs come from the existing by-id vault read.
    return {
        **{k: v for k, v in res.items() if k != "proposals"},
        "proposals": [{k: v for k, v in p.items()
                       if k not in ("items", "previews")}
                      for p in res["proposals"]],
    }


class _GenerateLinesBody(BaseModel):
    account_id: str
    limit: int = 8


@router.post("/admin/catalog/singles/generate-lines")
async def generate_catalog_lines(body: _GenerateLinesBody = Body(...)) -> dict[str, Any]:
    """Write NEW caption lines from the vault's own vision facts.

    The sibling of `/suggest-texts`: that one offers 14 fixed captions that are
    identical for every account, this one writes lines about the media THIS
    creator actually has and nothing is selling yet. Same row shape, so the same
    pick-list / Fill content / Save path handles both.

    POST because it costs money — one cheap DeepSeek call per media group,
    through `llm_client` so it is capped and audited. Writes nothing to the
    catalog; the operator still picks and saves.
    """
    import vault_catalog_copy

    assert_account_owned(body.account_id)
    return await vault_catalog_copy.generate_lines(
        body.account_id, limit=max(1, min(int(body.limit), 12)))


class _DraftFillBody(BaseModel):
    account_id: str
    only_empty: bool = True
    # Unsaved editor rows — label / description_for_ai / kind each.
    drafts: list[dict[str, Any]] = []
    # Let a row take media another row already sells. Operator toggle: with 18
    # rows over 101 items a caption can match 69 photos and still fill with
    # none, because every one is spoken for.
    allow_reuse: bool = False


@router.post("/admin/catalog/singles/suggest")
async def suggest_singles_content_with_drafts(
    body: _DraftFillBody = Body(...),
) -> dict[str, Any]:
    """Same as the GET, but also fills rows that are not saved yet.

    "Suggest text sets" appends caption rows to the editor with no row id, and
    the GET form reads its targets from the DB — so Fill content was a no-op on
    exactly the rows it existed to serve (verified: 0 of 13 filled). Posting the
    drafts lets them compete for the vault alongside the saved rows. Still
    writes NOTHING; the operator saves.
    """
    import vault_solo_fill

    assert_account_owned(body.account_id)
    res = await vault_solo_fill.suggest_singles(
        body.account_id, only_empty=body.only_empty, drafts=body.drafts,
        allow_reuse=body.allow_reuse)
    return {
        **{k: v for k, v in res.items() if k != "proposals"},
        "proposals": [{k: v for k, v in p.items()
                       if k not in ("items", "previews")}
                      for p in res["proposals"]],
    }


@router.get("/admin/catalog/singles/suggest-texts")
async def suggest_singles_texts(account_id: str = Query(...)) -> dict[str, Any]:
    """Propose CAPTION rows to add — text, price and kind, no media.

    The other half of `/singles/suggest`: that one fills content into a row that
    already has text, but nothing produced the text, so an empty catalog stayed
    empty. These rows drop straight into the singles editor and are then filled
    by "Fill content" and written by "Save singles" — so this endpoint writes
    NOTHING and a suggestion can always be thrown away.

    Read-only and free — no LLM, no OF. Every proposal is checked against the
    UNBOUND vault first, so a caption is only offered if this vault can honestly
    fill it; the rest come back under `shoot` as a shot-list. If the vault has
    no V2 describe fields, `blocked` says to run Describe rather than reporting
    a content gap that isn't one.
    """
    import vault_catalog_seed

    assert_account_owned(account_id)
    return await vault_catalog_seed.suggest_texts(account_id)


# ── Imports ──────────────────────────────────────────────────────────────────

class _FolderImportBody(BaseModel):
    account_id: str
    script_id: int
    folder: str


@router.post("/admin/scripts/import-folder")
async def import_folder(body: _FolderImportBody = Body(...)) -> dict[str, Any]:
    """Append one item per media in a vault folder (oldest→newest, the Drive
    numbering convention) with media bound and a placeholder description."""
    assert_account_owned(body.account_id)
    async with get_session() as s:
        sc = await s.get(CatalogScript, int(body.script_id))
        if sc is None or sc.account_id != body.account_id:
            raise HTTPException(404, "script not found")
        start = (await s.execute(
            select(func.max(CatalogItem.position)).where(
                CatalogItem.account_id == body.account_id,
                CatalogItem.script_id == int(body.script_id)))
        ).scalar_one_or_none()
    start = (int(start) + 1) if start is not None else 0

    client = await asyncio.to_thread(ax._make_client, body.account_id)

    def _folder_media() -> list[dict]:
        lists = client.vault_lists(view="main", limit=100)
        folders = lists.get("list") if isinstance(lists, dict) else lists
        list_id = None
        for f in (folders or []):
            if (isinstance(f, dict) and
                    str(f.get("name", "")).strip().lower() == body.folder.strip().lower()):
                list_id = int(f["id"])
                break
        if list_id is None:
            raise HTTPException(404, f"vault folder not found: {body.folder!r}")
        media = client.vault_media(list_id=list_id, type="all", limit=100)
        items = media.get("list") if isinstance(media, dict) else media
        out = [m for m in (items or []) if isinstance(m, dict) and m.get("id")]
        return list(reversed(out))  # vault is recent-first → oldest-first

    media_items = await asyncio.to_thread(_folder_media)
    now = datetime.utcnow()
    async with get_session() as s:
        for i, m in enumerate(media_items):
            mtype = str(m.get("type") or "").lower()
            s.add(CatalogItem(
                account_id=body.account_id, script_id=int(body.script_id),
                position=start + i,
                kind="video" if mtype == "video" else "image",
                label=f"#{start + i + 1}", description_for_ai=None,
                media_ids=json.dumps([int(m["id"])]),
                preview_media_ids="[]",
                duration_sec=int(m.get("duration") or 0) or None,
                price_cents=0, tip_unlock_cents=0, is_free_teaser=False,
                tags="[]", enabled=True, created_at=now, updated_at=now))
    return {"status": "ok", "imported": len(media_items), "from_position": start}


class _PasteImportBody(BaseModel):
    account_id: str
    script_id: int
    table: str


_DUR_RE = re.compile(r"(?:(\d+)\s*min)?\s*(?:(\d+)\s*sec)?", re.I)


def _parse_duration(s: str) -> int | None:
    s = (s or "").strip()
    if not s or "image" in s.lower():
        return None
    m = _DUR_RE.search(s)
    if not m or (m.group(1) is None and m.group(2) is None):
        return None
    return int(m.group(1) or 0) * 60 + int(m.group(2) or 0)


@router.post("/admin/scripts/paste-import")
async def paste_import(body: _PasteImportBody = Body(...)) -> dict[str, Any]:
    """Parse the agency Notion table (Label | No | Video | Duration) into items
    (text-first — bind media after via the editor/folder import). Tolerant: any
    pipe-separated row with a description column."""
    assert_account_owned(body.account_id)
    async with get_session() as s:
        sc = await s.get(CatalogScript, int(body.script_id))
        if sc is None or sc.account_id != body.account_id:
            raise HTTPException(404, "script not found")
        start = (await s.execute(
            select(func.max(CatalogItem.position)).where(
                CatalogItem.account_id == body.account_id,
                CatalogItem.script_id == int(body.script_id)))
        ).scalar_one_or_none()
    start = (int(start) + 1) if start is not None else 0

    rows: list[dict] = []
    for line in (body.table or "").splitlines():
        if "|" not in line:
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 3:
            continue
        label, _no, desc = cells[0], cells[1], cells[2]
        dur = _parse_duration(cells[3] if len(cells) > 3 else "")
        low = (label + desc).lower()
        if not desc or "label" in low and "video" in low:  # header row
            continue
        if set("".join(cells)) <= {"-", " ", ":"}:          # separator row
            continue
        is_image = dur is None and "image" in (cells[3] if len(cells) > 3 else "").lower()
        rows.append({"label": label[:80] or None, "desc": desc[:_TXT],
                     "dur": dur, "kind": "image" if is_image else "video"})
    if not rows:
        raise HTTPException(422, "no item rows found in the pasted table")

    now = datetime.utcnow()
    async with get_session() as s:
        for i, r in enumerate(rows):
            s.add(CatalogItem(
                account_id=body.account_id, script_id=int(body.script_id),
                position=start + i, kind=r["kind"], label=r["label"],
                description_for_ai=r["desc"], media_ids="[]",
                preview_media_ids="[]", duration_sec=r["dur"],
                price_cents=0, tip_unlock_cents=0, is_free_teaser=False,
                tags="[]", enabled=True, created_at=now, updated_at=now))
    return {"status": "ok", "imported": len(rows), "from_position": start}


# ── Simulate (dry prompt + LLM — catches a bad script before any fan) ────────

class _SimulateBody(BaseModel):
    account_id: str
    fan_says: str = "ngl im kinda in the mood.. u got anything for me? 🥵"


@router.post("/admin/scripts/simulate")
async def simulate(body: _SimulateBody = Body(...)) -> dict[str, Any]:
    assert_account_owned(body.account_id)
    from automations.ai_chatter import _Cand, _load_config  # local: avoid cycles
    cfg = await _load_config(body.account_id)
    scripts, items = await _load_catalog(body.account_id)
    offerable = await _offerable_for_fan(body.account_id, 0,
                                         str(cfg.get("offer_mode") or "both"),
                                         scripts, items)
    sell_block = (_manifest_block(offerable, scripts,
                                  str(cfg.get("offer_mode") or "both"))
                  if offerable else "")
    persona = await _load_persona(body.account_id)
    c = _Cand(0)
    c.messages = [("in", body.fan_says.strip()[:400])]
    c.last_dir, c.last_body = "in", body.fan_says.strip()[:400]
    msgs, _presented = _build_messages(
        persona, Fan(account_id=body.account_id, fan_id=0), c, set(), 20,
        sell_block=sell_block,
        content_ask=bool(sell_block) and bool(_CONTENT_ASK_RE.search(c.last_body)))
    model = await _resolve_sim_model(body.account_id)
    res = await llm_client.chat(model=model, messages=msgs,
                                purpose="ai_chatter_simulate",
                                account_id=body.account_id, fan_id=0,
                                temperature=0.85)
    raw = (res.content or "").strip()
    clean, offer_id = _parse_offer_marker(raw)
    item = offerable.get(offer_id) if offer_id is not None else None
    return {
        "bubbles": split_for_bubbles(clean, 6),
        "offer": None if item is None else {
            "item_id": int(item.id), "label": item.label,
            "price_cents": int(item.price_cents or 0),
            "tip_unlock_cents": int(item.tip_unlock_cents or 0),
            "is_free_teaser": bool(item.is_free_teaser),
        },
        "offerable_count": len(offerable),
        "manifest_present": bool(sell_block),
    }


async def _resolve_sim_model(account_id: str) -> str:
    from automations._common import resolve_model
    return await resolve_model(account_id, "ai_chatter", None)


# ── Monitor: sessions (progress pins) + open offers + kill switch ────────────

@router.get("/admin/ai-chatter/sessions")
async def sessions(account_id: str = Query(...)) -> dict[str, Any]:
    assert_account_owned(account_id)
    async with get_session() as s:
        prog = (await s.execute(
            select(CatalogProgress, CatalogScript.name)
            .join(CatalogScript, CatalogScript.id == CatalogProgress.script_id)
            .where(CatalogProgress.account_id == account_id)
            .order_by(CatalogProgress.updated_at.desc()).limit(200))).all()
        offers = (await s.execute(
            select(ContentOffer, CatalogItem.label)
            .join(CatalogItem, CatalogItem.id == ContentOffer.item_id)
            .where(ContentOffer.account_id == account_id)
            .order_by(ContentOffer.offered_at.desc()).limit(200))).all()
        fan_ids = {int(p.fan_id) for p, _ in prog} | {int(o.fan_id) for o, _ in offers}
        names: dict[int, str] = {}
        if fan_ids:
            for fid, nick, disp in (await s.execute(
                    select(Fan.fan_id, Fan.custom_nickname, Fan.of_display_name)
                    .where(Fan.account_id == account_id,
                           Fan.fan_id.in_(fan_ids)))).all():
                names[int(fid)] = (nick or disp or "").split("/")[0]
    return {
        "progress": [{
            "fan_id": int(p.fan_id), "fan_name": names.get(int(p.fan_id), ""),
            "script": script_name, "position": int(p.position),
            "status": p.status, "updated_at": p.updated_at.isoformat() if p.updated_at else None,
        } for p, script_name in prog],
        "offers": [{
            "id": int(o.id), "fan_id": int(o.fan_id),
            "fan_name": names.get(int(o.fan_id), ""),
            "item_label": label, "mode": o.mode, "status": o.status,
            "price_cents": int(o.price_cents or 0),
            "tip_unlock_cents": int(o.tip_unlock_cents or 0),
            "tips_accum_cents": int(o.tips_accum_cents or 0),
            "resolved_by": o.resolved_by,
            "offered_at": o.offered_at.isoformat() if o.offered_at else None,
        } for o, label in offers],
    }


class _CancelBody(BaseModel):
    account_id: str


@router.post("/admin/ai-chatter/offers/{offer_id}/cancel")
async def cancel_offer(offer_id: int, body: _CancelBody = Body(...)) -> dict:
    assert_account_owned(body.account_id)
    now = datetime.utcnow()
    async with get_session() as s:
        res = await s.execute(
            update(ContentOffer)
            .where(ContentOffer.id == int(offer_id),
                   ContentOffer.account_id == body.account_id,
                   ContentOffer.status == "open")
            .values(status="cancelled", resolved_by="manual",
                    resolved_at=now, updated_at=now))
    if (res.rowcount or 0) == 0:
        raise HTTPException(404, "no open offer with that id")
    return {"status": "ok"}
