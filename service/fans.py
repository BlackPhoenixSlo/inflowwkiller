"""
fans.py — server-side endpoints for the per-fan drawer / profile view.

Two endpoints, both keyed on (account_id, fan_id):

  GET    /admin/fans/{account_id}/{fan_id}   → full row (creates a stub if
                                               we haven't seen this fan yet)
  PATCH  /admin/fans/{account_id}/{fan_id}   → write custom_nickname, notes,
                                               tags (string list as JSON)

The OF user details (display name, avatar) are intentionally NOT mirrored
here — those go stale, and the inbox already batch-fetches /users/list to
get fresh ones. Our DB stores the human-curated overlay: nickname our team
picked, notes, tags, plus the auto-computed lifetime_spend_cents the event
transcoder will fill once Phase B.3 starts persisting transactions.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from db.engine import get_session
from db.models import Account, Employee, Fan, FanProfile, Message, Transaction
from auth import assert_account_owned


async def _ensure_account_row(s: AsyncSession, account_id: str) -> None:
    """Insert a minimal accounts row if missing. The FS-based account
    registry (sessions/accounts/<id>/) is the source of truth for who's
    logged in, but the DB `accounts` table only gets populated by
    `import_legacy` or explicit calls. Without a row here, any FK→accounts
    insert (fans, transactions, etc.) raises IntegrityError. Idempotent —
    safe to call on every write path that touches account-scoped tables."""
    stmt = (
        sqlite_insert(Account)
        .values(id=account_id, is_active_default=False)
        .on_conflict_do_nothing(index_elements=["id"])
    )
    await s.execute(stmt)

log = logging.getLogger("of-relay.fans")

# Strong refs to fire-and-forget background tasks so the event loop doesn't GC
# them mid-flight (asyncio only holds weak refs to tasks).
_bg_tasks: set[asyncio.Task] = set()


def _spawn_bg(coro) -> None:
    """Run `coro` detached on the loop, keeping a ref until it finishes."""
    t = asyncio.ensure_future(coro)
    _bg_tasks.add(t)
    t.add_done_callback(_bg_tasks.discard)
router = APIRouter()


def _row_to_dict(f: Fan) -> dict[str, Any]:
    return {
        "account_id": f.account_id,
        "fan_id": f.fan_id,
        "of_username": f.of_username,
        "of_display_name": f.of_display_name,
        "avatar_url": f.avatar_url,
        "custom_nickname": f.custom_nickname,
        "generated_nickname": f.generated_nickname,
        "real_name": f.real_name,
        "his_age": f.his_age,
        "home_country": f.home_country,
        "home_city": f.home_city,
        "hobbies": f.hobbies,
        "fetishes": f.fetishes,
        "self_description": f.self_description,
        "description": f.description,
        "notes": f.notes,
        # Written by automation A03 apply_profiles (our DB only — never pushed to
        # OF; no native OF endpoint exists for fan notes). Surfaced for the drawer.
        "applied_notes": f.applied_notes,
        "applied_notes_at": (
            f.applied_notes_at.isoformat() if f.applied_notes_at else None
        ),
        "tags": _safe_load_list(f.tags),
        "lifetime_spend_cents": f.lifetime_spend_cents,
        "subscription_status": f.subscription_status,
        "subscribed_at": f.subscribed_at.isoformat() if f.subscribed_at else None,
        "last_message_received_at": (
            f.last_message_received_at.isoformat() if f.last_message_received_at else None
        ),
        "source": f.source,
        "is_followed": f.is_followed,
        # ── §2.4 extended Grok facts ─
        "occupation": f.occupation,
        "employer": f.employer,
        "relationship_status": f.relationship_status,
        "partner_name": f.partner_name,
        "has_kids": f.has_kids,
        "family_names": _safe_load_obj(f.family_names),
        "pets": _safe_load_array(f.pets),
        "mood_at_last_message": f.mood_at_last_message,
        "sentiment_trend": f.sentiment_trend,
        "relationship_stage": f.relationship_stage,
        "communication_style": _safe_load_obj(f.communication_style),
        "automation_paused_until": (
            f.automation_paused_until.isoformat() if f.automation_paused_until else None
        ),
        "persona_age_claimed": f.persona_age_claimed,
        "persona_location_claimed": f.persona_location_claimed,
        "persona_job_claimed": f.persona_job_claimed,
        "profile_last_synced_at": (
            f.profile_last_synced_at.isoformat() if f.profile_last_synced_at else None
        ),
        "grok_facts_updated_at": (
            f.grok_facts_updated_at.isoformat() if f.grok_facts_updated_at else None
        ),
        "created_at": f.created_at.isoformat() if f.created_at else None,
        "updated_at": f.updated_at.isoformat() if f.updated_at else None,
    }


def _safe_load_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        v = json.loads(raw)
        return [str(x) for x in v] if isinstance(v, list) else []
    except (ValueError, TypeError):
        return []


def _safe_load_obj(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        v = json.loads(raw)
        return v if isinstance(v, dict) else {}
    except (ValueError, TypeError):
        return {}


def _safe_load_array(raw: str | None) -> list[Any]:
    if not raw:
        return []
    try:
        v = json.loads(raw)
        return v if isinstance(v, list) else []
    except (ValueError, TypeError):
        return []


_RELATIONSHIP_STATUSES = frozenset({
    "single", "dating", "engaged", "married",
    "divorced", "widowed", "complicated", "unknown",
})


class FanUpdateBody(BaseModel):
    custom_nickname: str | None = Field(None, description="Set null to clear")
    notes: str | None = None
    tags: list[str] | None = None
    real_name: str | None = None
    home_country: str | None = None
    home_city: str | None = None
    his_age: str | None = None
    hobbies: str | None = None
    fetishes: str | None = None
    # ── §2.4 extended Grok facts (T-A4 schema) ─
    occupation: str | None = None
    employer: str | None = None
    relationship_status: str | None = None
    partner_name: str | None = None
    has_kids: bool | None = None
    family_names: dict[str, Any] | None = None
    pets: list[Any] | None = None
    mood_at_last_message: str | None = None
    sentiment_trend: str | None = None
    relationship_stage: str | None = None
    communication_style: dict[str, Any] | None = None
    automation_paused_until: datetime | None = None
    # Persona continuity — humans may correct what gen_info claimed.
    persona_age_claimed: str | None = None
    persona_location_claimed: str | None = None
    persona_job_claimed: str | None = None
    profile_last_synced_at: datetime | None = None
    grok_facts_updated_at: datetime | None = None

    @field_validator("relationship_status")
    @classmethod
    def _check_relationship_status(cls, v: str | None) -> str | None:
        if v is None or v in _RELATIONSHIP_STATUSES:
            return v
        raise ValueError(
            f"relationship_status must be one of {sorted(_RELATIONSHIP_STATUSES)}"
        )


# NOTE: route order matters. FastAPI matches paths in declaration order
# and falls through on path-param TYPE failure — so the literal-third-
# segment routes (`by-ids`, `spend-batch`) MUST be declared BEFORE the
# int-parameter route `/{fan_id}`, otherwise "by-ids" gets matched as
# fan_id, fails int parsing, and returns 422 ("Input should be a valid
# integer"). The single-segment `/admin/fans/{account_id}` route can
# live anywhere — its shape doesn't collide.


@router.get("/admin/fans/{account_id}")
async def list_recent_fans(account_id: str, limit: int = 50) -> dict[str, Any]:
    """Recently-active fans for this account — primarily used by a future
    fan-browser screen. The inbox doesn't call this; it uses OF's chat list."""
    assert_account_owned(account_id)
    limit = max(1, min(int(limit or 50), 200))
    async with get_session() as s:
        q = (
            select(Fan)
            .where(Fan.account_id == account_id)
            .order_by(Fan.last_message_received_at.desc().nullslast())
            .limit(limit)
        )
        rows = (await s.execute(q)).scalars().all()
        return {"fans": [_row_to_dict(r) for r in rows]}


@router.get("/admin/fans/{account_id}/by-ids")
async def fans_by_ids(
    account_id: str,
    ids: str = Query("", description="Comma-separated fan ids (max 200)"),
) -> dict[str, Any]:
    """Bulk identity lookup against our LOCAL SQLite `fans` table.

    Returns `{fans: {fan_id_str: {id, name, username, avatar}}}` for every id
    we have a row for. Missing ids are omitted; the caller decides whether
    to back-fill from OF /users/list (paying the upstream cost) or accept
    the gap. Avatars come from the WS transcoder, which writes them
    whenever a message lands — covers anyone the model has talked to.

    Used by the chat-list enrichment to instantly paint names+avatars
    from local data instead of firing 8 parallel /users/list batches on
    every chats refetch. Cap at 200 ids per call to keep the IN-clause
    scan cheap on the (account_id, fan_id) composite primary key."""
    assert_account_owned(account_id)
    parsed: list[int] = []
    for chunk in ids.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            parsed.append(int(chunk))
        except ValueError:
            continue
    if not parsed:
        return {"fans": {}}
    parsed = parsed[:200]

    out: dict[str, dict[str, Any]] = {}
    async with get_session() as s:
        rows = (await s.execute(
            select(
                Fan.fan_id, Fan.of_username, Fan.of_display_name,
                Fan.avatar_url, Fan.custom_nickname,
            )
            .where(Fan.account_id == account_id, Fan.fan_id.in_(parsed))
        )).all()
        for fid, uname, dname, avatar, nickname in rows:
            out[str(fid)] = {
                "id": int(fid),
                "name": dname,
                "username": uname,
                "avatar": avatar,
                "customNickname": nickname,
            }
    return {"fans": out}


@router.get("/admin/fans/{account_id}/spend-batch")
async def fans_spend_batch(
    account_id: str,
    ids: str = Query("", description="Comma-separated fan ids"),
) -> dict[str, Any]:
    """Bulk spend lookup for ChatList row chips.

    Returns lifetime_spend_cents + last_purchase_at per fan, keyed by
    fan_id (string). Missing fans are omitted (frontend treats absence
    as $0 / no purchase). Caps at 200 ids per call so the index scan
    on (account_id, fan_id) stays cheap."""
    assert_account_owned(account_id)
    parsed: list[int] = []
    for chunk in ids.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            parsed.append(int(chunk))
        except ValueError:
            continue
    if not parsed:
        return {"spend": {}}
    parsed = parsed[:200]

    out: dict[str, dict[str, Any]] = {}
    async with get_session() as s:
        rows = (await s.execute(
            select(
                Fan.fan_id,
                Fan.lifetime_spend_cents,
                Fan.subscription_status,
                Fan.subscription_expires_at,
            )
            .where(Fan.account_id == account_id, Fan.fan_id.in_(parsed))
        )).all()
        for fid, cents, sub_status, sub_expires in rows:
            out[str(fid)] = {
                "spend_cents": int(cents or 0),
                "last_purchase_at": None,
                "subscription_status": sub_status,
                "expired_at": sub_expires.isoformat() if sub_expires else None,
            }

        # Last purchase timestamp — single grouped MAX query.
        tx_rows = (await s.execute(
            select(Transaction.fan_id, func.max(Transaction.occurred_at))
            .where(
                Transaction.account_id == account_id,
                Transaction.fan_id.in_(parsed),
                Transaction.amount_cents > 0,
            )
            .group_by(Transaction.fan_id)
        )).all()
        for fid, ts in tx_rows:
            if fid is None:
                continue
            entry = out.setdefault(str(fid), {
                "spend_cents": 0,
                "last_purchase_at": None,
                "subscription_status": None,
                "expired_at": None,
            })
            entry["last_purchase_at"] = ts.isoformat() if ts else None

    return {"spend": out}


# ── /{fan_id} routes — MUST be declared after every literal-third-segment
#    route above (see ordering note near FanUpdateBody). ────────────────

@router.get("/admin/fans/{account_id}/{fan_id}")
async def get_fan(account_id: str, fan_id: int) -> dict[str, Any]:
    """Return the fan row. Creates an empty stub on first access so the
    drawer always has a row to edit — avoids 404-then-create dance from
    the UI."""
    assert_account_owned(account_id)
    async with get_session() as s:
        f = await s.get(Fan, (account_id, fan_id))
        if f is None:
            await _ensure_account_row(s, account_id)
            f = Fan(account_id=account_id, fan_id=fan_id, source="onlyfans")
            s.add(f)
            await s.commit()
            await s.refresh(f)
        return _row_to_dict(f)


_LINE_SLOTS = ("q1", "q2", "q3", "tease1", "tease2", "tease3")


@router.get("/admin/fans/{account_id}/{fan_id}/lines")
async def get_fan_lines(account_id: str, fan_id: int) -> dict[str, Any]:
    """The fan's saved conversation openers from gen_info — questions (Q1-3) and
    teases (Tease1-3) — powering the composer's 'Lines' picker. Empty slots are
    omitted; each item carries its `slot` so the UI can consume it after sending."""
    assert_account_owned(account_id)
    questions: list[dict[str, str]] = []
    teases: list[dict[str, str]] = []
    async with get_session() as s:
        p = await s.get(FanProfile, (account_id, fan_id))
        if p is not None:
            for slot in ("q1", "q2", "q3"):
                v = (getattr(p, slot, None) or "").strip()
                if v:
                    questions.append({"slot": slot, "text": v})
            for slot in ("tease1", "tease2", "tease3"):
                v = (getattr(p, slot, None) or "").strip()
                if v:
                    teases.append({"slot": slot, "text": v})
    return {"questions": questions, "teases": teases}


@router.delete("/admin/fans/{account_id}/{fan_id}/lines/{slot}")
async def consume_fan_line(account_id: str, fan_id: int, slot: str) -> dict[str, Any]:
    """Delete (null) ONE used line so it's never offered twice. `slot` ∈ the six
    Q/Tease columns. Idempotent: a missing profile / already-empty slot still 200s."""
    assert_account_owned(account_id)
    if slot not in _LINE_SLOTS:
        raise HTTPException(status_code=400, detail=f"bad line slot: {slot!r}")
    async with get_session() as s:
        p = await s.get(FanProfile, (account_id, fan_id))
        deleted = False
        class_emptied = False
        if p is not None and (getattr(p, slot, None) or ""):
            setattr(p, slot, None)
            await s.commit()
            deleted = True
            # After nulling, did the consumed slot's whole class empty out? (all three
            # Q slots — or all three Tease slots — now null.) If so, the picker has no
            # more openers of that kind for this fan.
            cls = ("q1", "q2", "q3") if slot.startswith("q") else ("tease1", "tease2", "tease3")
            class_emptied = not any((getattr(p, s_, None) or "").strip() for s_ in cls)
    # Refill the emptied class via a GATED one-fan gen_info (refill_ids, NOT force_ids):
    # it only regenerates if there are new inbound messages since the last gen, so an
    # idle fan's lines stay empty until they write again — no wasted LLM call.
    if deleted and class_emptied:
        try:
            import automation_executor as ax  # lazy: avoid an import cycle at load
            await ax.enqueue_job(account_id, "gen_info", payload={"refill_ids": [fan_id]})
        except Exception:
            log.warning("line-refill enqueue failed account=%s fan=%s",
                        account_id, fan_id, exc_info=True)
    return {"ok": True, "slot": slot, "deleted": deleted}


@router.post("/admin/fans/{account_id}/{fan_id}/lines/generate")
async def generate_fan_lines(account_id: str, fan_id: int) -> dict[str, Any]:
    """Manually kick off a FRESH batch of lines for one fan — the composer's
    'Generate new batch' button. Unlike the consume-triggered refill (refill_ids,
    gated on new inbound messages) this FORCES a regen (force_ids): the chatter
    explicitly wants new openers now, so it bypasses the stale/new-message gates.
    Enqueues a gen_info job; the executor claims it on its next ~30s tick and the
    picker polls /lines for the result."""
    assert_account_owned(account_id)
    try:
        import automation_executor as ax  # lazy: avoid an import cycle at load
        job_id = await ax.enqueue_job(account_id, "gen_info", payload={"force_ids": [fan_id]})
        # Don't make the chatter wait for the supervisor's next ~30s tick: kick a
        # run_once NOW. It atomically CLAIMS the just-enqueued job (so the supervisor
        # can't double-run it) and its per-(account, kind) lock no-ops if a gen_info
        # for this account is already in flight — the queued job is picked up then.
        _spawn_bg(ax.run_once(account_id, "gen_info"))
    except Exception:
        log.warning("line-generate enqueue failed account=%s fan=%s",
                    account_id, fan_id, exc_info=True)
        raise HTTPException(status_code=500, detail="failed to enqueue line generation")
    return {"ok": True, "job_id": job_id}


@router.get("/admin/fans/{account_id}/{fan_id}/ppv-history")
async def fan_ppv_history(
    account_id: str,
    fan_id: int,
    limit: int = Query(12, ge=1, le=200),
) -> dict[str, Any]:
    """Recent PPV unlocks by this fan — powers the spend chart in the
    fan drawer (max/avg + per-purchase price points).

    Source: the `transactions` ledger (NOT the `messages` table). The
    messages table only carries rows from after we started watching the
    account; the transactions ledger is backfilled by the Phase F
    payouts ingest, so it has historical purchases too — without it,
    long-tenure fans look empty in the chart.

    Filter: `kind='ppv_message'`, `amount_cents > 0`, `status='cleared'`
    (the project-wide convention for "revenue actually settled"). Tips
    are kind='tip' and so are excluded by the filter. Ordered newest
    first via the (account_id, fan_id, occurred_at) partial index, then
    reversed so the response is chronological (oldest → newest) — the
    chart reads left to right."""
    assert_account_owned(account_id)
    async with get_session() as s:
        # Outer-join on messages so we get sent_by_employee_id + the
        # original sent_at for rows where the transaction carries a
        # message_id (i.e. WS-pump-originated tx). Phase-F-ingested
        # ledger rows (message_id NULL) just yield NULLs here — the
        # frontend enriches those via /chats/{fan_id}/media opened=1.
        rows = (await s.execute(
            select(
                Transaction.id,
                Transaction.message_id,
                Transaction.amount_cents,
                Transaction.net_cents,
                Transaction.fee_cents,
                Transaction.vat_cents,
                Transaction.occurred_at,
                Message.created_at.label("sent_at"),
                Message.sent_by_employee_id,
                Employee.display_name.label("employee_name"),
                Employee.color.label("employee_color"),
            )
            .select_from(Transaction)
            .outerjoin(
                Message,
                (Message.account_id == Transaction.account_id)
                & (Message.fan_id == Transaction.fan_id)
                & (Message.message_id == Transaction.message_id),
            )
            .outerjoin(Employee, Employee.id == Message.sent_by_employee_id)
            .where(
                Transaction.account_id == account_id,
                Transaction.fan_id == fan_id,
                Transaction.kind == "ppv_message",
                Transaction.amount_cents > 0,
                Transaction.status == "cleared",
            )
            .order_by(Transaction.occurred_at.desc())
            .limit(limit)
        )).all()

    items = [
        {
            "transaction_id": int(r.id),
            "message_id": int(r.message_id) if r.message_id is not None else None,
            "price_cents": int(r.amount_cents or 0),
            "net_cents": int(r.net_cents) if r.net_cents is not None else None,
            "fee_cents": int(r.fee_cents) if r.fee_cents is not None else None,
            "vat_cents": int(r.vat_cents) if r.vat_cents is not None else None,
            "purchased_at": r.occurred_at.isoformat() if r.occurred_at else None,
            "sent_at": r.sent_at.isoformat() if r.sent_at else (
                r.occurred_at.isoformat() if r.occurred_at else None
            ),
            "sent_by_employee_id": (
                int(r.sent_by_employee_id) if r.sent_by_employee_id is not None else None
            ),
            "employee_name": r.employee_name,
            "employee_color": r.employee_color,
        }
        for r in reversed(rows)
    ]
    # Per-kind lifetime totals for the drawer's stats grid. OF's
    # subscribedOnData.{tips,messages,posts}Summ is the primary source,
    # but it's frequently missing on long-tenure / banned / hidden
    # accounts — fall back to our ledger so the grid never shows "—" for
    # a fan who clearly has activity. Sum over ALL transactions (not just
    # the limited PPV list above); excludes refunds (`amount > 0`) and
    # still-pending rows (`status = 'cleared'`).
    async with get_session() as s2:
        breakdown_rows = (await s2.execute(
            select(Transaction.kind, func.sum(Transaction.amount_cents))
            .where(
                Transaction.account_id == account_id,
                Transaction.fan_id == fan_id,
                Transaction.amount_cents > 0,
                Transaction.status == "cleared",
            )
            .group_by(Transaction.kind)
        )).all()
    tips_cents = 0
    messages_cents = 0
    posts_cents = 0
    for kind, total in breakdown_rows:
        cents = int(total or 0)
        if kind in ("tip", "tip_post", "tip_stream"):
            tips_cents += cents
        elif kind == "ppv_message":
            messages_cents += cents
        elif kind == "ppv_post":
            posts_cents += cents
        # other kinds (subscription, rebill, ppv_stream, unknown) aren't
        # surfaced in the drawer's grid right now — they're aggregated in
        # the `Lifetime spend` figure via subscribedOnData.totalSumm.

    if items:
        prices = [it["price_cents"] for it in items]
        summary = {
            "count": len(prices),
            "max_cents": max(prices),
            "avg_cents": sum(prices) // len(prices),
            "tips_cents": tips_cents,
            "messages_cents": messages_cents,
            "posts_cents": posts_cents,
        }
    else:
        summary = {
            "count": 0, "max_cents": 0, "avg_cents": 0,
            "tips_cents": tips_cents,
            "messages_cents": messages_cents,
            "posts_cents": posts_cents,
        }
    return {"items": items, "summary": summary}


@router.patch("/admin/fans/{account_id}/{fan_id}")
async def update_fan(
    account_id: str, fan_id: int, body: FanUpdateBody = Body(...),
) -> dict[str, Any]:
    """Partial update. Only fields explicitly present in the body are
    written; null is a deliberate clear, omission is "leave alone"."""
    assert_account_owned(account_id)
    payload = body.model_dump(exclude_unset=True)
    async with get_session() as s:
        f = await s.get(Fan, (account_id, fan_id))
        if f is None:
            await _ensure_account_row(s, account_id)
            f = Fan(account_id=account_id, fan_id=fan_id, source="onlyfans")
            s.add(f)
        for k, v in payload.items():
            if k == "tags":
                f.tags = json.dumps(v or [])
            elif k == "family_names":
                f.family_names = json.dumps(v or {})
            elif k == "communication_style":
                f.communication_style = json.dumps(v or {})
            elif k == "pets":
                f.pets = json.dumps(v or [])
            else:
                setattr(f, k, v)
        f.updated_at = datetime.utcnow()
        await s.commit()
        await s.refresh(f)
        return _row_to_dict(f)
