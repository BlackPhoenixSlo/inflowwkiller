"""service/funnels_api.py — CRUD over `mass_message_funnels` so funnels can be
created/edited from the UI (W8 FUNNEL-API lane, before the P1 frontend).

A funnel is a GLOBAL definition (NO `account_id` column): one opener
(`opening_message`, sent by A10 send_mass_message) plus an ordered `steps_json`
list that A11 reply_mass_funnel walks per replying fan. Because funnels are
shared across all of an owner's models, these routes gate on OWNER auth but do
NOT scope by account — there's no account to scope to. (Contrast
automation_rules_api / nudge_config_api, which `assert_account_owned` because
their rows ARE account-scoped.)

Routes (covered by the existing `/admin/:path*` Next rewrite in
app/next.config.ts — NO next.config change, NO new app/admin/funnels page):

  GET    /admin/funnels            list (id, name, description, opening_message,
                                   step_count)
  GET    /admin/funnels/{id}       full funnel incl. parsed steps
  POST   /admin/funnels            create {name(unique,req), description,
                                   opening_message(req), steps[]}
  PUT    /admin/funnels/{id}       partial update
  DELETE /admin/funnels/{id}       409 if any mass_run references it (don't
                                   orphan live funnel_state); else delete

`_validate_steps` enforces EXACTLY the two shapes reply_mass_funnel walks
(service/automations/reply_mass_funnel.py): a REPLY step
  {"step": int>=1, "check_intervals_min": [positive ints],
   "messages": [non-empty str] (<=2 used)  OR  "prompt": str / "generate": true}
and a PPV step
  {"step": int>=1, "type": "paid_ppv", "price": int>=0,
   "media_files": [raw vault ids], "messages": [str], "locked_text": bool?}.
VERIFIED FACT: PPV media MUST be RAW vault ids in `media_files` —
reply_mass_funnel._send_step does `int(m) for m in media_files if isdigit`, so
each id must be an int or a digit string. Unknown keys pass through untouched
(forward-compat: the seeded funnels carry a `trigger` flag and legacy
vault_folder/media_indices on steps that the executor ignores).

Either shape may carry an OPTIONAL `next` map that turns the linear walker into a
BRANCHING storyline — reply_mass_funnel.resolve_next reads it to pick the step
after this one from the fan's reply:
  {"next": {"bought": 5, "ignored": 2, "keyword": {"more": 3}, "default": 3}}
Every target is a step NUMBER and must reference an existing step (else 422).
ABSENT `next` is the classic +1 advance, so every existing funnel is unaffected.

The opener-media columns (vault_folder/media_indices/opening_*) are left
writable + nullable but NOT depended on: send_mass_message.run uses only
funnel.opening_message (text) and takes opener media from the send-time
payload.media_files — so the API never requires those columns.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from datetime import datetime

from fastapi import APIRouter, Body, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError

from auth import assert_account_owned
from db.engine import get_session
from db.models import FunnelAccountMedia, MassMessageFunnel, MassRun

log = logging.getLogger("of-relay.funnels_api")

router = APIRouter()


# ── Step validation (the two shapes reply_mass_funnel walks) ──────────

def _whole_int(key: str, val: Any) -> int:
    """A JSON number that is a whole int (rejects bools, fractions, strings)."""
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        raise HTTPException(422, f"{key} must be a whole number")
    n = int(val)
    if n != val:
        raise HTTPException(422, f"{key} must be a whole number")
    return n


def _nonempty_messages(step: dict) -> list[str]:
    """The step's static, non-empty message strings (the verbatim copy)."""
    msgs = step.get("messages")
    if not isinstance(msgs, list):
        return []
    return [str(m) for m in msgs if str(m).strip()]


def _is_generated(step: dict) -> bool:
    """Mirror reply_mass_funnel._is_generated: static non-empty `messages` win;
    otherwise the step opts into generation via `prompt` / `generate`: true."""
    if _nonempty_messages(step):
        return False
    return bool(step.get("generate") or (isinstance(step.get("prompt"), str)
                                         and step["prompt"].strip()))


# The keys reply_mass_funnel.resolve_next understands on a step's `next` map.
_NEXT_KEYS = frozenset({"bought", "ignored", "default", "keyword"})


def _validate_next(idx: int, step: dict) -> None:
    """Shape-check an OPTIONAL `next` branch map — the storyline routing
    reply_mass_funnel.resolve_next reads to pick the step after this one
    ({"bought": n, "ignored": n, "default": n, "keyword": {word: n}}). Every
    target is a STEP NUMBER (validated to reference a real step across the whole
    funnel in _validate_steps). ABSENT `next` → the classic linear +1 walk, so
    an existing funnel is unaffected. Only the shape is checked here."""
    nxt = step.get("next")
    if nxt is None:
        return
    if not isinstance(nxt, dict):
        raise HTTPException(422, f"step[{idx}].next must be an object")
    for k in nxt:
        if k not in _NEXT_KEYS:
            raise HTTPException(
                422, f"step[{idx}].next has unknown key {k!r} "
                     "(allowed: bought, ignored, default, keyword)")
    for k in ("bought", "ignored", "default"):
        if k in nxt:
            _whole_int(f"step[{idx}].next.{k}", nxt[k])
    if "keyword" in nxt:
        kw = nxt["keyword"]
        if not isinstance(kw, dict) or not kw:
            raise HTTPException(
                422, f"step[{idx}].next.keyword must be a non-empty object "
                     "of {{word: step}}")
        for word, target in kw.items():
            if not str(word).strip():
                raise HTTPException(422, f"step[{idx}].next.keyword has a blank keyword")
            _whole_int(f"step[{idx}].next.keyword[{word!r}]", target)


def _next_targets(step: dict) -> list[tuple[str, int]]:
    """(label, target-step-number) pairs a validated `next` routes to — used to
    check every target references an existing step. Assumes _validate_next has
    already run (values are whole ints)."""
    nxt = step.get("next")
    if not isinstance(nxt, dict):
        return []
    out: list[tuple[str, int]] = []
    for k in ("bought", "ignored", "default"):
        if k in nxt:
            out.append((k, int(nxt[k])))
    kw = nxt.get("keyword")
    if isinstance(kw, dict):
        for word, target in kw.items():
            out.append((f"keyword[{word!r}]", int(target)))
    return out


def _validate_intervals(idx: int, step: dict) -> None:
    """`check_intervals_min`, when present, must be a non-empty list of positive
    ints (the reply-poll cadence). Absent → reply_mass_funnel uses its [2,4,10]
    default, so it's optional here — but a malformed value is rejected."""
    iv = step.get("check_intervals_min")
    if iv is None:
        return
    if not isinstance(iv, list) or not iv:
        raise HTTPException(422, f"step[{idx}].check_intervals_min must be a non-empty list")
    for x in iv:
        n = _whole_int(f"step[{idx}].check_intervals_min", x)
        if n <= 0:
            raise HTTPException(422, f"step[{idx}].check_intervals_min must be positive ints")


def _validate_step(idx: int, step: Any) -> dict:
    if not isinstance(step, dict):
        raise HTTPException(422, f"step[{idx}] must be an object")

    # `step` index: required, 1-based (funnels number steps from 1).
    if "step" not in step:
        raise HTTPException(422, f"step[{idx}] is missing 'step'")
    s_no = _whole_int(f"step[{idx}].step", step["step"])
    if s_no < 1:
        raise HTTPException(422, f"step[{idx}].step must be >= 1")

    stype = step.get("type")
    if stype is not None and stype != "paid_ppv":
        raise HTTPException(422, f"step[{idx}] has unknown type {stype!r} (only 'paid_ppv')")
    is_ppv = stype == "paid_ppv"

    # Text contract (shared): static non-empty `messages` OR a generated step
    # (`prompt`/`generate`). Reject an empty, non-generated step either way.
    if not _is_generated(step) and not _nonempty_messages(step):
        raise HTTPException(
            422, f"step[{idx}] needs non-empty 'messages' or 'prompt'/'generate': true")

    if is_ppv:
        if "price" in step and step["price"] is not None:
            price = _whole_int(f"step[{idx}].price", step["price"])
            if price < 0:
                raise HTTPException(422, f"step[{idx}].price must be >= 0")
        # MEDIA is NO LONGER a funnel-step field. Vault ids are per-account, so a
        # funnel (shared across all of an owner's models) can't carry them — each
        # model maps its own via funnel_account_media (the /media routes below).
        # Any `media_files`/`previews` still posted here pass through untouched
        # (back-compat for legacy funnels), but they're not validated and the UI
        # no longer sends them.
        if "locked_text" in step and not isinstance(step["locked_text"], bool):
            raise HTTPException(422, f"step[{idx}].locked_text must be true or false")
    else:
        _validate_intervals(idx, step)

    # Optional branching map — allowed on either shape (a PPV step may route to a
    # follow-up on purchase, a reply step on a keyword). Target existence is
    # checked across all steps once they're collected (_validate_steps).
    _validate_next(idx, step)

    return step


def _validate_steps(steps: Any) -> list[dict]:
    if steps is None:
        return []
    if not isinstance(steps, list):
        raise HTTPException(422, "steps must be a list")
    out = [_validate_step(i, st) for i, st in enumerate(steps)]
    # Cross-step: every `next` branch target must reference an EXISTING step
    # number (the funnel numbers steps via the `step` field). A dangling target
    # would strand a fan mid-storyline, so reject the whole funnel with a 422.
    step_numbers = {int(st["step"]) for st in out}
    for i, st in enumerate(out):
        for label, target in _next_targets(st):
            if target not in step_numbers:
                raise HTTPException(
                    422, f"step[{i}].next.{label} → step {target} references no "
                         "existing step")
    # Round-trips cleanly (rejects NaN/Infinity and non-serialisable values).
    try:
        json.dumps(out)
    except (TypeError, ValueError) as e:
        raise HTTPException(422, f"steps are not JSON-serialisable: {e}")
    return out


# ── Serialization ─────────────────────────────────────────────────────

def _safe_steps(raw: str | None) -> list[dict]:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return [s for s in parsed if isinstance(s, dict)] if isinstance(parsed, list) else []


def _safe_indices(raw: str | None) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return raw


def _summary(fn: MassMessageFunnel) -> dict[str, Any]:
    return {
        "id": fn.id,
        "name": fn.name,
        "description": fn.description,
        "opening_message": fn.opening_message,
        "step_count": len(_safe_steps(fn.steps_json)),
    }


def _full(fn: MassMessageFunnel) -> dict[str, Any]:
    return {
        **_summary(fn),
        "steps": _safe_steps(fn.steps_json),
        # Opener-media columns: surfaced read-only; NOT consumed by send today.
        "vault_folder": fn.vault_folder,
        "media_indices": _safe_indices(fn.media_indices),
        "opening_vault_folder": fn.opening_vault_folder,
        "opening_media_indices": _safe_indices(fn.opening_media_indices),
    }


# ── Request bodies ────────────────────────────────────────────────────

class FunnelCreate(BaseModel):
    name: str = Field(..., min_length=1)
    opening_message: str = Field(..., min_length=1)
    description: str | None = None
    steps: list[dict[str, Any]] | None = None
    # Opener-media columns — optional, stored verbatim, NOT consumed at send.
    vault_folder: str | None = None
    media_indices: Any | None = None
    opening_vault_folder: str | None = None
    opening_media_indices: Any | None = None


class FunnelUpdate(BaseModel):
    name: str | None = None
    opening_message: str | None = None
    description: str | None = None
    steps: list[dict[str, Any]] | None = None
    vault_folder: str | None = None
    media_indices: Any | None = None
    opening_vault_folder: str | None = None
    opening_media_indices: Any | None = None


# ── Routes ────────────────────────────────────────────────────────────

@router.get("/admin/funnels")
async def list_funnels() -> dict[str, Any]:
    async with get_session() as s:
        rows = (await s.execute(
            select(MassMessageFunnel).order_by(MassMessageFunnel.name)
        )).scalars().all()
        return {"funnels": [_summary(fn) for fn in rows]}


@router.get("/admin/funnels/{funnel_id}")
async def get_funnel(funnel_id: int) -> dict[str, Any]:
    async with get_session() as s:
        fn = await s.get(MassMessageFunnel, funnel_id)
        if fn is None:
            raise HTTPException(404, "funnel not found")
        return _full(fn)


def _indices_json(val: Any) -> str | None:
    """Coerce a media-indices value to its stored JSON text (or None)."""
    if val is None:
        return None
    if isinstance(val, str):
        return val  # already a JSON string (e.g. "all" or "[2]") — store verbatim
    try:
        return json.dumps(val)
    except (TypeError, ValueError) as e:
        raise HTTPException(422, f"media_indices is not JSON-serialisable: {e}")


@router.post("/admin/funnels")
async def create_funnel(body: FunnelCreate = Body(...)) -> dict[str, Any]:
    name = body.name.strip()
    if not name:
        raise HTTPException(422, "name cannot be blank")
    opening = body.opening_message.strip()
    if not opening:
        raise HTTPException(422, "opening_message cannot be blank")
    steps = _validate_steps(body.steps)

    async with get_session() as s:
        fn = MassMessageFunnel(
            name=name,
            description=(body.description.strip() if body.description else None),
            opening_message=opening,
            steps_json=json.dumps(steps),
            vault_folder=body.vault_folder,
            media_indices=_indices_json(body.media_indices) or "[]",
            opening_vault_folder=body.opening_vault_folder,
            opening_media_indices=_indices_json(body.opening_media_indices),
        )
        s.add(fn)
        try:
            await s.flush()
        except IntegrityError:
            await s.rollback()
            raise HTTPException(409, f"a funnel named {name!r} already exists")
        out = _full(fn)
    log.info("funnel_created id=%s name=%s steps=%d", out["id"], name, len(steps))
    return out


@router.put("/admin/funnels/{funnel_id}")
async def update_funnel(funnel_id: int, body: FunnelUpdate = Body(...)) -> dict[str, Any]:
    async with get_session() as s:
        fn = await s.get(MassMessageFunnel, funnel_id)
        if fn is None:
            raise HTTPException(404, "funnel not found")
        if body.name is not None:
            nm = body.name.strip()
            if not nm:
                raise HTTPException(422, "name cannot be blank")
            fn.name = nm
        if body.opening_message is not None:
            om = body.opening_message.strip()
            if not om:
                raise HTTPException(422, "opening_message cannot be blank")
            fn.opening_message = om
        if body.description is not None:
            fn.description = body.description.strip() or None
        if body.steps is not None:
            fn.steps_json = json.dumps(_validate_steps(body.steps))
        if body.vault_folder is not None:
            fn.vault_folder = body.vault_folder
        if body.media_indices is not None:
            fn.media_indices = _indices_json(body.media_indices) or "[]"
        if body.opening_vault_folder is not None:
            fn.opening_vault_folder = body.opening_vault_folder
        if body.opening_media_indices is not None:
            fn.opening_media_indices = _indices_json(body.opening_media_indices)
        try:
            await s.flush()
        except IntegrityError:
            await s.rollback()
            raise HTTPException(409, f"a funnel named {fn.name!r} already exists")
        out = _full(fn)
    log.info("funnel_updated id=%s", funnel_id)
    return out


@router.delete("/admin/funnels/{funnel_id}")
async def delete_funnel(funnel_id: int) -> dict[str, Any]:
    async with get_session() as s:
        fn = await s.get(MassMessageFunnel, funnel_id)
        if fn is None:
            raise HTTPException(404, "funnel not found")
        # Don't orphan live funnel_state: a mass_run anchored to this funnel is
        # still being walked by reply_mass_funnel. Block the delete instead.
        refs = (await s.execute(
            select(func.count()).select_from(MassRun).where(MassRun.funnel_id == funnel_id)
        )).scalar_one()
        if refs:
            raise HTTPException(
                409, f"funnel is referenced by {refs} mass run(s); cannot delete")
        await s.delete(fn)
    log.info("funnel_deleted id=%s", funnel_id)
    return {"deleted": funnel_id}


# ── Per-account media (funnel_account_media) ──────────────────────────
#
# A funnel's TEXT is global; its MEDIA is per-account (OF vault ids don't carry
# between models). These routes ARE account-scoped, so — unlike the funnel CRUD
# above — they `assert_account_owned` (mirrors nudge_config_api / automation_
# rules_api). One row per (funnel, account): the model's opener media + a
# step-number-keyed map of {media_files, previews}.

def _vault_ids(key: str, raw: Any) -> list[int]:
    """Coerce a media-id list to ints (raw vault ids). reply_mass_funnel /
    of_client expect ints, so each entry must be an int or a digit string."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise HTTPException(422, f"{key} must be a list of vault ids")
    out: list[int] = []
    for m in raw:
        if isinstance(m, bool) or not (
            isinstance(m, int) or (isinstance(m, str) and m.isdigit())
        ):
            raise HTTPException(422, f"{key} must be raw vault ids (int/digit-string)")
        out.append(int(m))
    return out


def _validate_steps_media(raw: Any) -> dict[str, dict]:
    """`steps_media` → a clean {step_no_str: {media_files, previews}} map. Keys
    are step numbers (the funnel numbers steps from 1); previews are clamped to
    ids actually in that step's media set (a free teaser is a SUBSET of the
    locked send)."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise HTTPException(422, "steps_media must be an object keyed by step number")
    out: dict[str, dict] = {}
    for k, v in raw.items():
        ks = str(k)
        if not ks.isdigit():
            raise HTTPException(422, f"steps_media key {k!r} must be a step number")
        if not isinstance(v, dict):
            raise HTTPException(422, f"steps_media[{ks}] must be an object")
        media = _vault_ids(f"steps_media[{ks}].media_files", v.get("media_files"))
        previews = _vault_ids(f"steps_media[{ks}].previews", v.get("previews"))
        previews = [m for m in previews if m in media]
        if media or previews:
            out[ks] = {"media_files": media, "previews": previews}
    return out


def _media_payload(row: FunnelAccountMedia | None, funnel_id: int, account_id: str) -> dict[str, Any]:
    if row is None:
        return {"funnel_id": funnel_id, "account_id": account_id,
                "opening_media_ids": [], "steps_media": {}}
    try:
        opening = json.loads(row.opening_media_ids or "[]")
    except (TypeError, ValueError):
        opening = []
    try:
        steps_media = json.loads(row.steps_media_json or "{}")
    except (TypeError, ValueError):
        steps_media = {}
    return {
        "funnel_id": funnel_id,
        "account_id": account_id,
        "opening_media_ids": opening if isinstance(opening, list) else [],
        "steps_media": steps_media if isinstance(steps_media, dict) else {},
    }


class FunnelMediaBody(BaseModel):
    opening_media_ids: list[Any] | None = None
    steps_media: dict[str, Any] | None = None


@router.get("/admin/funnels/{funnel_id}/media/{account_id}")
async def get_funnel_media(funnel_id: int, account_id: str) -> dict[str, Any]:
    assert_account_owned(account_id)
    async with get_session() as s:
        if await s.get(MassMessageFunnel, funnel_id) is None:
            raise HTTPException(404, "funnel not found")
        row = await s.get(FunnelAccountMedia, (funnel_id, account_id))
    return _media_payload(row, funnel_id, account_id)


@router.put("/admin/funnels/{funnel_id}/media/{account_id}")
async def put_funnel_media(
    funnel_id: int, account_id: str, body: FunnelMediaBody = Body(...)
) -> dict[str, Any]:
    assert_account_owned(account_id)
    opening = _vault_ids("opening_media_ids", body.opening_media_ids)
    steps_media = _validate_steps_media(body.steps_media)
    now = datetime.utcnow()
    opening_json = json.dumps(opening)
    steps_json = json.dumps(steps_media)
    async with get_session() as s:
        if await s.get(MassMessageFunnel, funnel_id) is None:
            raise HTTPException(404, "funnel not found")
        await s.execute(
            sqlite_insert(FunnelAccountMedia)
            .values(funnel_id=funnel_id, account_id=account_id,
                    opening_media_ids=opening_json, steps_media_json=steps_json,
                    created_at=now, updated_at=now)
            .on_conflict_do_update(
                index_elements=["funnel_id", "account_id"],
                set_={"opening_media_ids": opening_json,
                      "steps_media_json": steps_json, "updated_at": now})
        )
        row = await s.get(FunnelAccountMedia, (funnel_id, account_id))
        out = _media_payload(row, funnel_id, account_id)
    log.info("funnel_media_saved funnel=%s account=%s opener=%d steps=%d",
             funnel_id, account_id, len(opening), len(steps_media))
    return out


@router.delete("/admin/funnels/{funnel_id}/media/{account_id}")
async def delete_funnel_media(funnel_id: int, account_id: str) -> dict[str, Any]:
    assert_account_owned(account_id)
    async with get_session() as s:
        row = await s.get(FunnelAccountMedia, (funnel_id, account_id))
        if row is not None:
            await s.delete(row)
    log.info("funnel_media_cleared funnel=%s account=%s", funnel_id, account_id)
    return {"cleared": True, "funnel_id": funnel_id, "account_id": account_id}
