"""service/ppv_library_config_api.py — read/write account_ai_config.ppv_library_config_json.

The "PPV Library" tab persists a per-account store of premade PPVs through these
owner-gated routes. On save it ALSO syncs the automation rules: one `ppv_send`
AutomationRule per enabled PPV, cadence `every_seconds = 604800 / sends_per_week`.
The rule materializer is the scheduler — there is no rotator.

  GET /admin/ppv-library-config?account_id=  → {config, defaults, pools, matrix}
  PUT /admin/ppv-library-config              → upsert the JSON + sync rules

Default (absent/NULL) → DISABLED with an empty library until enabled + configured.
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
from db.models import AccountAiConfig, AutomationRule
from automations.ppv_send import (
    PPV_CAPTION_POOLS,
    RECENCY_BANDS,
    SPEND_BANDS,
    _DEFAULTS,
    _PRICE_CEIL_CENTS,
    _PRICE_FLOOR_CENTS,
)

log = logging.getLogger("of-relay.ppv_library_config_api")

router = APIRouter()

_MAX_PPVS = 50
_MAX_MEDIA = 50
_MAX_PREVIEWS = 10
_MAX_CAPTION_TEXTS = 20
_CAPTION_MAX = 1500     # long multi-paragraph PPV copy (the "screenshot" look) fits
_NAME_MAX = 60
_WEEK_SECONDS = 7 * 24 * 60 * 60
_MIN_EVERY_S = 3600                 # never faster than hourly per PPV
_MAX_EVERY_S = 30 * 24 * 60 * 60    # the rules-engine ceiling


def _int_list(raw: Any, cap: int) -> list[int]:
    out: list[int] = []
    for x in (raw or []):
        try:
            v = int(x)
        except (TypeError, ValueError):
            continue
        if v > 0 and v not in out:
            out.append(v)
        if len(out) >= cap:
            break
    return out


def _validate_ppv(p: Any) -> dict:
    if not isinstance(p, dict):
        raise HTTPException(422, "each ppv must be an object")
    pid = str(p.get("id") or "").strip()
    if not pid:
        raise HTTPException(422, "each ppv needs a stable id")
    name = str(p.get("name") or "").strip()[:_NAME_MAX]
    try:
        base = int(p.get("base_price_cents") or 0)
    except (TypeError, ValueError):
        raise HTTPException(422, "base_price_cents must be a number")
    base = max(_PRICE_FLOOR_CENTS, min(base, _PRICE_CEIL_CENTS))
    pool_key = str(p.get("caption_pool_key") or "").strip()
    caption_texts = p.get("caption_texts")
    clean_texts: list[str] = []
    if isinstance(caption_texts, (list, tuple)):
        for t in caption_texts[:_MAX_CAPTION_TEXTS]:
            s = str(t or "").strip()[:_CAPTION_MAX]
            if s:
                clean_texts.append(s)
    if pool_key and pool_key not in PPV_CAPTION_POOLS:
        raise HTTPException(422, f"unknown caption_pool_key: {pool_key}")
    if not pool_key and not clean_texts:
        raise HTTPException(422, f"ppv {pid}: pick a caption pool or write captions")
    try:
        spw = int(p.get("sends_per_week") or 1)
    except (TypeError, ValueError):
        raise HTTPException(422, "sends_per_week must be a number")
    spw = max(1, min(spw, 14))
    media = _int_list(p.get("media_ids"), _MAX_MEDIA)
    media_set = set(media)
    # OF previews are the attached media shown free — keep only previews that are
    # part of the content (anything else → OF "Wrong preview" at send time).
    previews = [x for x in _int_list(p.get("preview_options"), _MAX_PREVIEWS) if x in media_set]
    return {
        "id": pid,
        "name": name,
        "media_ids": media,
        "caption_pool_key": pool_key,
        "caption_texts": clean_texts,
        "base_price_cents": base,
        "preview_options": previews,
        "sends_per_week": spw,
        "resend_monthly": bool(p.get("resend_monthly")),
        # Skip fans who already unlocked this PPV's media (don't re-pitch owned content).
        "exclude_buyers": bool(p.get("exclude_buyers", True)),
        "enabled": bool(p.get("enabled", True)),
    }


def _validate(cfg: dict) -> dict:
    if not isinstance(cfg, dict):
        raise HTTPException(422, "config must be an object")
    out: dict[str, Any] = {"enabled": bool(cfg.get("enabled"))}
    # "Send to everyone": also broadcast each PPV to ALL subscribers at the
    # default price (every known fan excluded so no double-send). UI default ON;
    # the runtime treats an ABSENT key as off, so it never blasts until saved.
    out["reach_all"] = bool(cfg.get("reach_all", True))
    # Pause between re-messaging the same fan (the contact guard), in hours.
    # 0 = no pause (send to everyone). Clamp to a sane ceiling.
    try:
        out["pause_hours"] = max(0, min(int(cfg.get("pause_hours") or 0), 168))
    except (TypeError, ValueError):
        out["pause_hours"] = 0
    # Optional creator-local quiet window [start_hour, end_hour] (0-23). Applies to
    # every PPV rule (baked into each rule's quiet_hours_json). Null/absent = 24/7.
    qh = cfg.get("quiet_hours")
    if isinstance(qh, (list, tuple)) and len(qh) == 2:
        try:
            out["quiet_hours"] = [int(qh[0]) % 24, int(qh[1]) % 24]
        except (TypeError, ValueError):
            raise HTTPException(422, "quiet_hours must be two hour numbers")
    # Per-account throttle: max PPV sends per day / week / month, EVEN-SPREAD — cap
    # N/window = one send every window/N (2/day → every 12h). A too-soon send is held
    # to its slot. 0/absent = that cap is off.
    caps = cfg.get("ppv_caps")
    if isinstance(caps, dict):
        clean_caps: dict[str, int] = {}
        for k in ("per_day", "per_week", "per_month"):
            try:
                v = int(caps.get(k) or 0)
            except (TypeError, ValueError):
                v = 0
            if v > 0:
                clean_caps[k] = min(v, 10_000)
        if clean_caps:
            out["ppv_caps"] = clean_caps
    raw = cfg.get("ppvs") or []
    if not isinstance(raw, (list, tuple)):
        raise HTTPException(422, "ppvs must be a list")
    seen: set[str] = set()
    ppvs: list[dict] = []
    for p in raw[:_MAX_PPVS]:
        item = _validate_ppv(p)
        if item["id"] in seen:
            raise HTTPException(422, f"duplicate ppv id: {item['id']}")
        seen.add(item["id"])
        ppvs.append(item)
    out["ppvs"] = ppvs
    return out


def _every_seconds(sends_per_week: int) -> int:
    return max(_MIN_EVERY_S, min(round(_WEEK_SECONDS / max(1, sends_per_week)), _MAX_EVERY_S))


async def _sync_rules(s, account_id: str, cfg: dict) -> dict[str, int]:
    """One `ppv_send` rule per PPV. Match existing rules by the ppv_id baked into
    steps_json (so a rename never orphans a rule). Enabled rule iff master-enabled
    AND the PPV is enabled; removed PPVs' rules are disabled (not deleted — keeps
    any in-flight job's FK intact)."""
    master = bool(cfg.get("enabled"))
    qh = cfg.get("quiet_hours")
    quiet_json = json.dumps(qh) if isinstance(qh, list) and len(qh) == 2 else None
    existing = (await s.execute(
        select(AutomationRule).where(
            AutomationRule.account_id == account_id,
            AutomationRule.kind == "ppv_send",
        )
    )).scalars().all()
    by_ppv: dict[str, AutomationRule] = {}
    for r in existing:
        try:
            pid = str((json.loads(r.steps_json or "{}") or {}).get("ppv_id") or "")
        except Exception:
            pid = ""
        if pid:
            by_ppv[pid] = r

    stats = {"created": 0, "updated": 0, "disabled": 0}
    desired_ids: set[str] = set()
    for p in cfg.get("ppvs") or []:
        pid = p["id"]
        desired_ids.add(pid)
        trigger = json.dumps({"every_seconds": _every_seconds(p["sends_per_week"])})
        steps = json.dumps({"account_id": account_id, "ppv_id": pid})
        enable = master and p.get("enabled", True)
        name = f"PPV: {p.get('name') or pid}"[:_NAME_MAX]
        r = by_ppv.get(pid)
        if r is not None:
            r.trigger_json = trigger
            r.steps_json = steps
            r.name = name
            r.is_enabled = enable
            r.quiet_hours_json = quiet_json
            stats["updated"] += 1
        else:
            s.add(AutomationRule(
                account_id=account_id, name=name, kind="ppv_send",
                trigger_json=trigger, steps_json=steps, is_enabled=enable,
                quiet_hours_json=quiet_json))
            stats["created"] += 1
    for pid, r in by_ppv.items():
        if pid not in desired_ids and r.is_enabled:
            r.is_enabled = False
            stats["disabled"] += 1
    return stats


def _matrix_view() -> dict:
    return {
        "spend_bands": [
            {"name": n, "min_cents": lo, "max_cents": (None if hi == float("inf") else hi), "mult": m}
            for n, lo, hi, m in SPEND_BANDS
        ],
        "recency_bands": [
            {"name": n, "max_days": (None if hi == float("inf") else hi), "mult": m}
            for n, hi, m in RECENCY_BANDS
        ],
    }


@router.get("/admin/ppv-library-config")
async def get_ppv_library_config(account_id: str = Query(...)) -> dict[str, Any]:
    assert_account_owned(account_id)
    async with get_session() as s:
        row = await s.get(AccountAiConfig, account_id)
    stored: dict = {}
    if row is not None and row.ppv_library_config_json:
        try:
            stored = json.loads(row.ppv_library_config_json) or {}
        except Exception:
            stored = {}
    return {
        "account_id": account_id,
        "config": stored,
        "defaults": dict(_DEFAULTS),
        "pools": sorted(PPV_CAPTION_POOLS),
        "caption_pools": PPV_CAPTION_POOLS,   # key → lines, for the UI caption preview
        "matrix": _matrix_view(),
    }


class _ConfigBody(BaseModel):
    account_id: str
    config: dict


@router.put("/admin/ppv-library-config")
async def put_ppv_library_config(body: _ConfigBody = Body(...)) -> dict[str, Any]:
    assert_account_owned(body.account_id)
    clean = _validate(body.config)
    now = datetime.utcnow()
    payload = json.dumps(clean)
    async with get_session() as s:
        await s.execute(
            sqlite_insert(AccountAiConfig)
            .values(account_id=body.account_id, utc_offset=0,
                    ppv_library_config_json=payload, updated_at=now)
            .on_conflict_do_update(
                index_elements=["account_id"],
                set_={"ppv_library_config_json": payload, "updated_at": now})
        )
        rules = await _sync_rules(s, body.account_id, clean)
    log.info("ppv_library_config_saved account=%s ppvs=%d rules=%s",
             body.account_id, len(clean["ppvs"]), rules)
    return {"account_id": body.account_id, "config": clean, "rules": rules}


class _PreviewBody(BaseModel):
    account_id: str
    base_price_cents: int = 2500


@router.post("/admin/ppv-library-config/preview")
async def preview_ppv_library(body: _PreviewBody = Body(...)) -> dict[str, Any]:
    """Dry-run: with the account's CURRENT fans, how many land in each spend×recency
    cell and what would they pay for this base price. No send, no state write — the
    operator's safety net (surfaces a thin-data 'everyone is cheap' collapse)."""
    assert_account_owned(body.account_id)
    from automations.ppv_send import segment_preview
    base = max(_PRICE_FLOOR_CENTS, min(int(body.base_price_cents or 0), _PRICE_CEIL_CENTS))
    return {"account_id": body.account_id, **await segment_preview(body.account_id, base)}
