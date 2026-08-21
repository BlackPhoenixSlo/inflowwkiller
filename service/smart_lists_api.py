"""service/smart_lists_api.py — Smart Lists: saved dynamic fan segments.

A Smart List is a named query over per-fan facts, saved once and reused as a
mass-message audience (include/exclude). Internal only — never round-tripped to
OnlyFans.

The DSL (`rules_json`):

    {
      "match": "all" | "any",          # AND vs OR across the rules (default all)
      "rules": [
        {"field": "lifetime_spend",  "op": ">=", "value": 10000},   # cents
        {"field": "recent_spend",    "op": ">=", "value": 5000, "days": 30},  # cents in window
        {"field": "last_active_days","op": "<=", "value": 7},       # days since last inbound
        {"field": "status",          "op": "==", "value": "active"},# subscription_status
        {"field": "tag",             "op": "has","value": "whale"}  # fans.tags contains
      ]
    }

`resolve(rules, fans)` is a PURE function over already-normalised fan dicts (see
`_normalise_fan`) so it is trivially unit-testable without a DB — the router
builds the fan dicts from `fans` + `transactions`, then calls it.

Routes (owner-gated via `auth`, all under /admin/* so the existing relay
rewrite + share-token gate cover them):

  GET    /admin/smart-lists?account_id=            list saved segments
  POST   /admin/smart-lists                        create
  PATCH  /admin/smart-lists/{list_id}              update name/rules
  DELETE /admin/smart-lists/{list_id}              delete
  POST   /admin/smart-lists/preview                {account_id, rules} -> {count, sample}
  GET    /admin/smart-lists/{list_id}/resolve      -> {fan_ids, count} (composer expands to userIds)
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any, Iterable

from fastapi import APIRouter, Body, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from auth import assert_account_owned
from db.engine import get_session
from db.models import Fan, SmartList, Transaction

log = logging.getLogger("of-relay.smart_lists_api")

router = APIRouter()

# ── DSL definition ────────────────────────────────────────────────────

# field -> value kind. Drives which operators/coercions are legal.
_NUMERIC_FIELDS = {"lifetime_spend", "recent_spend", "last_active_days"}
_STRING_FIELDS = {"status"}
_TAG_FIELDS = {"tag"}
_FIELDS = _NUMERIC_FIELDS | _STRING_FIELDS | _TAG_FIELDS

_NUMERIC_OPS = {">", ">=", "<", "<=", "==", "!="}
_STRING_OPS = {"==", "!="}
_TAG_OPS = {"has", "not_has"}

_PREVIEW_SAMPLE = 25
_MAX_RULES = 20


def _num_cmp(a: float, op: str, b: float) -> bool:
    if op == ">":
        return a > b
    if op == ">=":
        return a >= b
    if op == "<":
        return a < b
    if op == "<=":
        return a <= b
    if op == "==":
        return a == b
    if op == "!=":
        return a != b
    return False


def _eval_rule(rule: dict, fan: dict) -> bool:
    """Evaluate ONE rule against a normalised fan dict. Unknown field / bad
    shape → False (never matches), so a malformed rule can only ever SHRINK a
    segment, never leak fans into it."""
    field = rule.get("field")
    op = rule.get("op")
    value = rule.get("value")

    if field == "lifetime_spend":
        return _num_cmp(float(fan.get("lifetime_spend") or 0), op, float(value))

    if field == "recent_spend":
        days = int(rule.get("days") or 30)
        rs = fan.get("recent_spend") or {}
        # keys may be int (from the resolver) or str (from JSON round-trips)
        cents = rs.get(days, rs.get(str(days), 0))
        return _num_cmp(float(cents or 0), op, float(value))

    if field == "last_active_days":
        la = fan.get("last_active_days")
        if la is None:
            return False  # never active → fails any recency threshold
        return _num_cmp(float(la), op, float(value))

    if field == "status":
        cur = (fan.get("status") or "").lower()
        want = str(value or "").lower()
        return cur == want if op == "==" else (cur != want if op == "!=" else False)

    if field == "tag":
        tags = {str(t).lower() for t in (fan.get("tags") or [])}
        present = str(value or "").lower() in tags
        if op == "has":
            return present
        if op == "not_has":
            return not present
        return False

    return False


def resolve(rules_obj: dict | None, fans: Iterable[dict]) -> list[int]:
    """PURE. Return the sorted fan_id list matching `rules_obj` over `fans`.

    `rules_obj` = {"match": "all"|"any", "rules": [...]}. Empty/absent rules →
    EVERY fan matches (a blank segment is "everyone", not "no one"). `fans` is
    an iterable of dicts shaped by `_normalise_fan`.
    """
    rules_obj = rules_obj or {}
    rules = rules_obj.get("rules") or []
    match = str(rules_obj.get("match") or "all").lower()
    out: list[int] = []
    for fan in fans:
        fid = fan.get("fan_id")
        if fid is None:
            continue
        if not rules:
            out.append(int(fid))
            continue
        results = [_eval_rule(r, fan) for r in rules]
        ok = all(results) if match != "any" else any(results)
        if ok:
            out.append(int(fid))
    return sorted(out)


# ── Validation (persisted rules) ──────────────────────────────────────

def _validate_rules(rules_obj: Any) -> dict:
    """Coerce + bound-check a rules object for storage. Raises 422 on anything
    the resolver couldn't act on (so a bad rule is rejected at write time, not
    silently dropped at read)."""
    if rules_obj is None:
        return {"match": "all", "rules": []}
    if not isinstance(rules_obj, dict):
        raise HTTPException(422, "rules must be an object")
    match = str(rules_obj.get("match") or "all").lower()
    if match not in ("all", "any"):
        raise HTTPException(422, "match must be 'all' or 'any'")
    raw = rules_obj.get("rules") or []
    if not isinstance(raw, list):
        raise HTTPException(422, "rules.rules must be a list")
    if len(raw) > _MAX_RULES:
        raise HTTPException(422, f"at most {_MAX_RULES} rules")
    clean: list[dict] = []
    for r in raw:
        if not isinstance(r, dict):
            raise HTTPException(422, "each rule must be an object")
        field = r.get("field")
        op = r.get("op")
        if field not in _FIELDS:
            raise HTTPException(422, f"unknown field {field!r}")
        entry: dict[str, Any] = {"field": field, "op": op}
        if field in _NUMERIC_FIELDS:
            if op not in _NUMERIC_OPS:
                raise HTTPException(422, f"{field} needs a numeric operator ({', '.join(sorted(_NUMERIC_OPS))})")
            try:
                entry["value"] = float(r.get("value"))
            except (TypeError, ValueError):
                raise HTTPException(422, f"{field} value must be a number")
            if field == "recent_spend":
                days = r.get("days", 30)
                try:
                    entry["days"] = max(1, min(int(days), 3650))
                except (TypeError, ValueError):
                    raise HTTPException(422, "recent_spend days must be an integer")
        elif field in _STRING_FIELDS:
            if op not in _STRING_OPS:
                raise HTTPException(422, f"{field} needs == or !=")
            entry["value"] = str(r.get("value") or "")
        else:  # tag
            if op not in _TAG_OPS:
                raise HTTPException(422, "tag needs 'has' or 'not_has'")
            entry["value"] = str(r.get("value") or "")
            if not entry["value"]:
                raise HTTPException(422, "tag value cannot be blank")
        clean.append(entry)
    return {"match": match, "rules": clean}


# ── Fan loading (build normalised dicts from the DB) ──────────────────

def _normalise_fan(row: Fan, now: datetime, recent_spend: dict[int, int]) -> dict:
    """One DB Fan row -> the pure-resolver's fan dict. `recent_spend` maps each
    referenced window (days) to that fan's spend in cents over the window."""
    try:
        tags = json.loads(row.tags) if row.tags else []
    except Exception:
        tags = []
    la: float | None = None
    if row.last_message_received_at is not None:
        la = max(0.0, (now - row.last_message_received_at).total_seconds() / 86400.0)
    return {
        "fan_id": int(row.fan_id),
        "lifetime_spend": int(row.lifetime_spend_cents or 0),
        "last_active_days": la,
        "status": row.subscription_status,
        "tags": tags if isinstance(tags, list) else [],
        "recent_spend": recent_spend,
        # display bits for the preview sample (ignored by the resolver)
        "_name": row.custom_nickname or row.generated_nickname or row.of_display_name
                 or row.of_username or str(row.fan_id),
    }


def _windows_in_rules(rules_obj: dict) -> set[int]:
    """The distinct recent_spend windows (days) a rules object references."""
    out: set[int] = set()
    for r in (rules_obj.get("rules") or []):
        if r.get("field") == "recent_spend":
            try:
                out.add(max(1, min(int(r.get("days") or 30), 3650)))
            except (TypeError, ValueError):
                continue
    return out


async def _load_normalised_fans(account_id: str, rules_obj: dict) -> list[dict]:
    """Load every fan for the account as a normalised resolver dict, with each
    recent_spend window pre-aggregated (one grouped transactions query per
    distinct window). Pure resolve() then filters these."""
    now = datetime.utcnow()
    windows = _windows_in_rules(rules_obj)
    async with get_session() as s:
        fan_rows = (await s.execute(
            select(Fan).where(Fan.account_id == str(account_id))
        )).scalars().all()

        spend_by_window: dict[int, dict[int, int]] = {}
        for days in windows:
            cutoff = now - timedelta(days=days)
            rows = (await s.execute(
                select(Transaction.fan_id, func.sum(Transaction.amount_cents))
                .where(Transaction.account_id == str(account_id),
                       Transaction.fan_id.isnot(None),
                       Transaction.occurred_at >= cutoff)
                .group_by(Transaction.fan_id)
            )).all()
            spend_by_window[days] = {int(fid): int(total or 0) for fid, total in rows}

    out: list[dict] = []
    for row in fan_rows:
        rs = {days: spend_by_window.get(days, {}).get(int(row.fan_id), 0) for days in windows}
        out.append(_normalise_fan(row, now, rs))
    return out


# ── Serialisation ─────────────────────────────────────────────────────

def _rules_of(row: SmartList) -> dict:
    try:
        return json.loads(row.rules_json) if row.rules_json else {"match": "all", "rules": []}
    except Exception:
        return {"match": "all", "rules": []}


def _serialize(row: SmartList) -> dict:
    return {
        "id": row.id,
        "account_id": row.account_id,
        "name": row.name,
        "rules": _rules_of(row),
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


async def _assert_name_free(s, account_id: str, name: str, *, exclude_id: int | None = None) -> None:
    """409 if `name` is already taken on this account. UNIQUE(account_id, name)
    is the real guard; this checks first so the operator gets an accurate
    message instead of a raw IntegrityError (which a rename would otherwise
    surface as a 500)."""
    q = select(SmartList.id).where(SmartList.account_id == account_id, SmartList.name == name)
    if exclude_id is not None:
        q = q.where(SmartList.id != exclude_id)
    if (await s.execute(q)).first():
        raise HTTPException(409, f"a smart list named {name!r} already exists")


# ── Routes ────────────────────────────────────────────────────────────

@router.get("/admin/smart-lists")
async def list_smart_lists(account_id: str = Query(...)) -> dict[str, Any]:
    assert_account_owned(account_id)
    async with get_session() as s:
        rows = (await s.execute(
            select(SmartList).where(SmartList.account_id == account_id)
            .order_by(SmartList.name)
        )).scalars().all()
    return {"lists": [_serialize(r) for r in rows]}


class _CreateBody(BaseModel):
    account_id: str
    name: str
    rules: dict | None = None


@router.post("/admin/smart-lists")
async def create_smart_list(body: _CreateBody = Body(...)) -> dict[str, Any]:
    assert_account_owned(body.account_id)
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(422, "name is required")
    rules = _validate_rules(body.rules)
    now = datetime.utcnow()
    async with get_session() as s:
        await _assert_name_free(s, body.account_id, name)
        row = SmartList(account_id=body.account_id, name=name,
                        rules_json=json.dumps(rules), created_at=now, updated_at=now)
        s.add(row)
        try:
            await s.flush()
        except IntegrityError as e:
            # Lost a race on the name, or account_id has no accounts row. Report
            # the real reason — a blanket "already exists" here used to mask an
            # unknown-account FK failure.
            raise HTTPException(409, f"could not save smart list: {repr(e)[:160]}")
        out = _serialize(row)
    log.info("smart_list_created id=%s account=%s name=%s", out["id"], body.account_id, name)
    return out


class _UpdateBody(BaseModel):
    name: str | None = None
    rules: dict | None = None


@router.patch("/admin/smart-lists/{list_id}")
async def update_smart_list(list_id: int, body: _UpdateBody = Body(...)) -> dict[str, Any]:
    async with get_session() as s:
        row = await s.get(SmartList, list_id)
        if row is None:
            raise HTTPException(404, "smart list not found")
        assert_account_owned(row.account_id)
        if body.name is not None:
            nm = body.name.strip()
            if not nm:
                raise HTTPException(422, "name cannot be blank")
            if nm != row.name:
                await _assert_name_free(s, row.account_id, nm, exclude_id=row.id)
            row.name = nm
        if body.rules is not None:
            row.rules_json = json.dumps(_validate_rules(body.rules))
        row.updated_at = datetime.utcnow()
        await s.flush()
        out = _serialize(row)
    log.info("smart_list_updated id=%s", list_id)
    return out


@router.delete("/admin/smart-lists/{list_id}")
async def delete_smart_list(list_id: int) -> dict[str, Any]:
    async with get_session() as s:
        row = await s.get(SmartList, list_id)
        if row is None:
            raise HTTPException(404, "smart list not found")
        assert_account_owned(row.account_id)
        await s.delete(row)
    log.info("smart_list_deleted id=%s", list_id)
    return {"deleted": list_id}


class _PreviewBody(BaseModel):
    account_id: str
    rules: dict | None = None


@router.post("/admin/smart-lists/preview")
async def preview_smart_list(body: _PreviewBody = Body(...)) -> dict[str, Any]:
    """Resolve a (possibly unsaved) rules object against the account's fans and
    return the match COUNT plus a small SAMPLE — the segment-builder's live
    counter."""
    assert_account_owned(body.account_id)
    rules = _validate_rules(body.rules)
    fans = await _load_normalised_fans(body.account_id, rules)
    matched_ids = set(resolve(rules, fans))
    sample = []
    for f in fans:
        if f["fan_id"] in matched_ids:
            sample.append({
                "fan_id": f["fan_id"],
                "name": f["_name"],
                "lifetime_spend_cents": f["lifetime_spend"],
                "last_active_days": None if f["last_active_days"] is None
                                    else round(f["last_active_days"], 1),
                "status": f["status"],
            })
            if len(sample) >= _PREVIEW_SAMPLE:
                break
    return {"count": len(matched_ids), "total_fans": len(fans), "sample": sample}


@router.get("/admin/smart-lists/{list_id}/resolve")
async def resolve_smart_list(list_id: int) -> dict[str, Any]:
    """Expand a saved list to the current matching fan-id set — the composer
    calls this to turn an include/exclude Smart-List chip into explicit
    userIds/excludedUsers on the send."""
    async with get_session() as s:
        row = await s.get(SmartList, list_id)
        if row is None:
            raise HTTPException(404, "smart list not found")
        assert_account_owned(row.account_id)
        account_id = row.account_id
        rules = _rules_of(row)
    fans = await _load_normalised_fans(account_id, rules)
    fan_ids = resolve(rules, fans)
    return {"list_id": list_id, "account_id": account_id,
            "fan_ids": fan_ids, "count": len(fan_ids)}
