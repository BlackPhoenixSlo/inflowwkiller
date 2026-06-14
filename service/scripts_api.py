"""service/scripts_api.py — the ai_chatter content catalog + config + monitor API.

Everything the Scripts UI needs, owner-gated, all under /admin (covered by the
Next `/admin/:path*` rewrite — no next.config.ts change):

  GET  /admin/ai-chatter-config?account_id=     → {config, defaults}
  PUT  /admin/ai-chatter-config                 → validate + upsert the JSON
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
    ContentOffer, Fan,
)
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
    "max_fans_per_tick": (1, 100),
    "resume_after_manual_hours": (0, 168),
    "stall_ttl_hours": (1, 720),
}
_MODES = ("backup", "always")
_OFFER_MODES = ("tip", "ppv", "both")
_MAX_ITEMS = 60
_TXT = 2000


# ── ai_chatter config (mirrors tip_reward_config_api) ────────────────────────

def _validate_cfg(cfg: dict) -> dict:
    if not isinstance(cfg, dict):
        raise HTTPException(422, "config must be an object")
    out: dict[str, Any] = {}
    if "enabled" in cfg:
        out["enabled"] = bool(cfg["enabled"])
    if "intent_only" in cfg:
        out["intent_only"] = bool(cfg["intent_only"])
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
    return out


@router.get("/admin/ai-chatter-config")
async def get_ai_chatter_config(account_id: str = Query(...)) -> dict[str, Any]:
    assert_account_owned(account_id)
    async with get_session() as s:
        row = await s.get(AccountAiConfig, account_id)
    stored: dict = {}
    if row is not None and row.ai_chatter_config_json:
        try:
            stored = json.loads(row.ai_chatter_config_json) or {}
        except Exception:
            stored = {}
    return {"account_id": account_id, "config": stored,
            "defaults": dict(_AI_CHATTER_DEFAULTS)}


class _ConfigBody(BaseModel):
    account_id: str
    config: dict


@router.put("/admin/ai-chatter-config")
async def put_ai_chatter_config(body: _ConfigBody = Body(...)) -> dict[str, Any]:
    assert_account_owned(body.account_id)
    clean = _validate_cfg(body.config)
    now = datetime.utcnow()
    payload = json.dumps(clean)
    async with get_session() as s:
        await s.execute(
            sqlite_insert(AccountAiConfig)
            .values(account_id=body.account_id, utc_offset=0,
                    ai_chatter_config_json=payload, updated_at=now)
            .on_conflict_do_update(
                index_elements=["account_id"],
                set_={"ai_chatter_config_json": payload, "updated_at": now})
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
    log.info("ai_chatter_config_saved account=%s cfg=%s", body.account_id, clean)
    return {"account_id": body.account_id, "config": clean}


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
