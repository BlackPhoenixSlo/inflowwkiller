"""service/promotions_api.py — Profile Promotion campaigns (Growth surface).

Run subscription-promo campaigns for the creator's profile (discounted price for
N claims / N days) and read their performance. OF is the source of truth
(of_client.promotions / create_promo / delete_promo); a light `promo_campaigns`
row keeps the operator's name + last-seen subscriber count so the list renders
without a live round-trip.

Routes (owner-gated, under /admin/*):
  GET    /admin/promotions?account_id=    merged mirror ∪ live OF (best-effort)
  POST   /admin/promotions                create on OF + upsert mirror
  DELETE /admin/promotions/{mirror_id}    delete on OF + drop mirror row
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select

import automation_executor as ax
from api_errors import raise_if_free_page_denied
from auth import assert_account_owned
from db.engine import get_session
from db.models import PromoCampaign
from of_mirror import delete_mirrored, merge_mirrors
from of_promos import extract_created_promo, fetch_of_promos, normalise_of_promo

log = logging.getLogger("of-relay.promotions_api")

router = APIRouter()


def _iso(dt) -> str | None:
    return dt.isoformat() if dt else None


@router.get("/admin/promotions")
async def list_promotions(account_id: str = Query(...)) -> dict[str, Any]:
    assert_account_owned(account_id)
    async with get_session() as s:
        mirrors = (await s.execute(
            select(PromoCampaign).where(PromoCampaign.account_id == account_id)
            .order_by(PromoCampaign.created_at.desc())
        )).scalars().all()

    fetched = await fetch_of_promos(account_id)

    def _from_mirror(m: PromoCampaign, live: dict | None) -> dict:
        return {
            "id": m.id,
            "of_promo_id": m.of_promo_id,
            "name": m.name,
            # OF never echoes the discount on list → the mirror is the record.
            "discount_percent": m.discount_percent,
            "price_cents": (live or {}).get("price_cents", m.price_cents),
            "subscribe_counts": (live or {}).get("subscribe_counts") or m.subscribe_counts,
            # `or` hid the truth here: OF stores subscribeDays=0 for a percent
            # discount, 0 is falsy, so the operator's typed duration won and the
            # row claimed "30d" for a promo with no end date at all.
            "subscribe_days": live["subscribe_days"] if live else m.subscribe_days,
            "message": (live or {}).get("message") or m.message,
            "promo_type": (live or {}).get("promo_type") or m.promo_type,
            "status": (live or {}).get("status") or m.status,
            "subscriber_count": (live or {}).get("subscriber_count", m.subscriber_count),
            # auto-reactivate state (promo_reactivate automation)
            "auto_reactivate": bool(m.auto_reactivate),
            "reactivate_count": int(m.reactivate_count or 0),
            "last_reactivated_at": _iso(m.last_reactivated_at),
            "created_at": _iso(m.created_at),
            "source": "mirror",
        }

    def _from_live(p: dict) -> dict:
        return {
            "id": None,
            "of_promo_id": p["of_promo_id"],
            "name": p.get("message") or f"Promo #{p['of_promo_id']}",
            "discount_percent": None,   # OF doesn't echo the discount on list
            "price_cents": p["price_cents"],
            "subscribe_counts": p["subscribe_counts"],
            "subscribe_days": p["subscribe_days"],
            "message": p["message"],
            "promo_type": p["promo_type"],
            "status": p["status"],
            "subscriber_count": p["subscriber_count"],
            # OF-only rows aren't mirrored, so they can't be auto-reactivated.
            "auto_reactivate": False,
            "reactivate_count": 0,
            "last_reactivated_at": None,
            "created_at": None,
            "source": "onlyfans",
        }

    out = merge_mirrors(mirrors, fetched, of_id_attr="of_promo_id",
                        live_id_key="of_promo_id",
                        from_mirror=_from_mirror, from_live=_from_live)
    # None = the OF fetch failed (not reachable); [] = reachable but no promos.
    return {"campaigns": out, "of_available": fetched is not None}


class _CreateBody(BaseModel):
    account_id: str
    name: str
    discount: int = Field(50, ge=1, le=100)   # PERCENT off the subscription (OF contract)
    subscribe_counts: int = 10                # max claims
    subscribe_days: int = 30                  # discounted-access duration (days)
    message: str = ""
    promo_type: str = "all"


@router.post("/admin/promotions")
async def create_promotion(body: _CreateBody = Body(...)) -> dict[str, Any]:
    assert_account_owned(body.account_id)
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(422, "name is required")
    discount = max(1, min(int(body.discount), 100))
    # 0 = UNLIMITED claims. Verified live: OF accepts subscribeCounts=0 and
    # echoes it back as null (no cap) — Ava's history has a promo sitting at
    # claimsCount=100 against a "0" cap. The old max(1, ...) floor made the
    # unlimited option unreachable from the UI.
    counts = max(0, min(int(body.subscribe_counts), 100000))
    days = max(0, min(int(body.subscribe_days), 3650))
    # A 100%-off promo IS a free trial: OF sets price=0 and HONOURS subscribeDays
    # (verified live — 100/3d stored price 0, subscribeDays 3; 40%/3d stored
    # price 3, subscribeDays 0). OF rejects subscribeDays < 1, so a free promo
    # needs a real duration or the create 400s.
    if discount >= 100:
        days = max(1, days)

    try:
        # _make_client inside the try so a missing/expired session surfaces as a
        # clean 502 (with a reason) rather than an unhandled 500.
        client = await asyncio.to_thread(ax._make_client, body.account_id)
        res = await asyncio.to_thread(
            lambda: client.create_promo(
                discount=discount, subscribe_counts=counts,
                subscribe_days=days, message=body.message or "",
                type=body.promo_type or "all"),
        )
    except Exception as e:
        log.warning("create_promo OF call failed account=%s", body.account_id, exc_info=True)
        raise_if_free_page_denied(e, "promotions")
        raise HTTPException(502, f"OnlyFans promotion create failed (session/API): {repr(e)[:200]}")

    # OF's create returns a LIST holding the created promo — extract_created_promo
    # handles that (and the wrapped/bare shapes) so of_promo_id is captured; without
    # it delete/re-arm can't reach OF and a live promo is orphaned.
    of = normalise_of_promo(extract_created_promo(res))
    # Store what OF ACTUALLY did, not what was asked for. For a percent discount
    # OF discards subscribeDays and stores 0 (finishedAt stays null — the offer
    # runs until the claim cap fills or it's ended). subscribeDays is only
    # honoured for a 100%-off / free promo. Echoing the operator's number back
    # made the UI promise an expiry that does not exist.
    effective_days = of.get("subscribe_days")
    if effective_days is None:
        effective_days = days
    now = datetime.utcnow()
    async with get_session() as s:
        row = PromoCampaign(
            account_id=body.account_id, of_promo_id=of.get("of_promo_id"),
            name=name, discount_percent=discount, price_cents=0,
            subscribe_counts=counts, subscribe_days=effective_days,
            message=body.message or "", promo_type=body.promo_type or "all",
            status="active", subscriber_count=of.get("subscriber_count") or 0,
            created_at=now,
        )
        s.add(row)
        await s.flush()
        mirror_id = row.id
    log.info("promo_created id=%s account=%s of_id=%s discount=%s%%",
             mirror_id, body.account_id, of.get("of_promo_id"), discount)
    return {"id": mirror_id, "of_promo_id": of.get("of_promo_id"), "name": name,
            "discount_percent": discount, "price_cents": 0, "subscribe_counts": counts,
            "subscribe_days": effective_days, "message": body.message or "",
            "promo_type": body.promo_type or "all", "status": "active",
            "subscriber_count": of.get("subscriber_count") or 0,
            "auto_reactivate": False, "reactivate_count": 0,
            "last_reactivated_at": None, "source": "mirror"}


class _PatchBody(BaseModel):
    """Only the operator-toggleable field: keep this promo permanently on offer."""
    auto_reactivate: bool


@router.patch("/admin/promotions/{mirror_id}")
async def update_promotion(mirror_id: int, body: _PatchBody = Body(...)) -> dict[str, Any]:
    """Turn auto-reactivate on/off for one campaign. When on, the
    `promo_reactivate` automation re-creates it on OF each time it finishes."""
    async with get_session() as s:
        row = await s.get(PromoCampaign, mirror_id)
        if row is None:
            raise HTTPException(404, "promotion not found")
        assert_account_owned(row.account_id)
        if body.auto_reactivate and not (1 <= int(row.discount_percent or 0) <= 100):
            raise HTTPException(
                422, "this campaign has no saved discount, so it can't be re-created")
        if body.auto_reactivate:
            # OF runs one promo at a time, so a second armed campaign would just
            # take turns finishing the first — a real promo minted every tick.
            other = (await s.execute(
                select(PromoCampaign.name).where(
                    PromoCampaign.account_id == row.account_id,
                    PromoCampaign.auto_reactivate.is_(True),
                    PromoCampaign.id != row.id,
                ).limit(1)
            )).scalar_one_or_none()
            if other is not None:
                raise HTTPException(
                    409, f"“{other}” is already set to keep running. OnlyFans only runs one "
                         f"promotion at a time, so turn that one off first.")
        if body.auto_reactivate and row.of_promo_id is None:
            # The automation fails closed on these (it can't look the promo up to
            # tell finished from live), so arming one would silently do nothing.
            raise HTTPException(
                422, "this campaign isn't linked to an OnlyFans promo, so its state "
                     "can't be checked — re-create it to enable Keep running")
        row.auto_reactivate = bool(body.auto_reactivate)
        out = {"id": row.id, "auto_reactivate": row.auto_reactivate,
               "reactivate_count": int(row.reactivate_count or 0),
               "last_reactivated_at": _iso(row.last_reactivated_at)}
    log.info("promo_auto_reactivate id=%s -> %s", mirror_id, body.auto_reactivate)
    return out


@router.delete("/admin/promotions/{mirror_id}")
async def delete_promotion(mirror_id: int) -> dict[str, Any]:
    return await delete_mirrored(
        PromoCampaign, mirror_id, of_id_attr="of_promo_id",
        of_delete=lambda c, oid: c.delete_promo(oid),
        not_found="promotion not found", label="promo")
