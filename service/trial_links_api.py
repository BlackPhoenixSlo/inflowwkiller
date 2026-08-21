"""service/trial_links_api.py — Free-trial link generator (Growth surface).

Mint / label / delete OnlyFans free-trial links and read their redemptions. OF
is the source of truth (of_client.trial_links / create_trial_link /
delete_trial_link); we keep a light `trial_link_mirrors` row per link so the
operator's name survives and the table renders even when a live OF fetch is
unavailable.

Routes (owner-gated, under /admin/*):
  GET    /admin/trial-links?account_id=   merged mirror ∪ live OF (best-effort)
  POST   /admin/trial-links               create on OF + upsert mirror
  DELETE /admin/trial-links/{mirror_id}   delete on OF + drop mirror row

The GET's OF refresh is best-effort: if the account has no live session (or OF
errors) the endpoint still returns the mirror rows, so the page never hard-fails
on a read.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select

import automation_executor as ax
from api_errors import raise_if_free_page_denied
from auth import assert_account_owned
from db.engine import get_session
from db.models import TrialLinkMirror
from of_mirror import delete_mirrored, merge_mirrors
from of_promos import coerce_of_id, of_field, paged_of_fetch, safe_int

log = logging.getLogger("of-relay.trial_links_api")

router = APIRouter()


def _normalise_of_trial(item: dict) -> dict:
    """One OF /trials entry -> our flat shape (coercion never raises).

    Field names captured LIVE (2026-07-22, Ava): an entry carries id,
    trialLinkName, url, subscribeDays, subscribeCounts, claimCounts,
    clicksCounts, createdAt, expiredAt, isFinished, sharedWith, user.
    Note there is no `name` key — the operator's label is `trialLinkName`.
    """
    finished = bool(of_field(item, "isFinished", default=False))
    return {
        "of_trial_id": coerce_of_id(of_field(item, "id", "trialId")),
        "name": of_field(item, "name", "trialLinkName", default=""),
        "url": of_field(item, "url", "shareUrl", "link"),
        "subscribe_days": safe_int(of_field(item, "subscribeDays", "subscribe_days", default=0)),
        "subscribe_counts": safe_int(of_field(item, "subscribeCounts", "subscribe_counts", default=0)),
        "claim_counts": safe_int(of_field(item, "claimCounts", "claimsCount", "usedCount", default=0)),
        # A spent/expired link keeps appearing in /trials. Without these the UI
        # rendered a dead link identically to a live one.
        "clicks_count": safe_int(of_field(item, "clicksCounts", "clickCounts", default=0)),
        "expired_at": of_field(item, "expiredAt"),
        "is_finished": finished,
        "status": "finished" if finished else "active",
    }


async def _fetch_of_trials(account_id: str) -> list[dict] | None:
    """Best-effort live OF fetch of /trials, PAGED. Thin wrapper over
    of_promos.paged_of_fetch (the shared skeleton /promotions uses too) — same
    None-vs-list contract: None = OF not reachable, [] = reachable, no links.

    /trials pages at THREE items by default, so an un-paged call showed 3 of
    Ava's 18 links — the operator saw a fraction of their own links and could
    not tell. limit=100 returns the lot in one request; the loop covers an
    account with more. Rows arrive under "list" (OF) with "items" as a fallback.
    """
    return await paged_of_fetch(
        account_id,
        page_call=lambda c, off, lim: c.trial_links(offset=off, limit=lim),
        normalise=_normalise_of_trial,
        id_key="of_trial_id",
        payload_keys=("list", "items"),
        max_pages=10,
        label="trial_links",
    )


@router.get("/admin/trial-links")
async def list_trial_links(account_id: str = Query(...)) -> dict[str, Any]:
    assert_account_owned(account_id)
    async with get_session() as s:
        mirrors = (await s.execute(
            select(TrialLinkMirror).where(TrialLinkMirror.account_id == account_id)
            .order_by(TrialLinkMirror.created_at.desc())
        )).scalars().all()

    fetched = await _fetch_of_trials(account_id)

    # Mirror rows carry the operator's name; live OF carries the usage counters.
    def _from_mirror(m: TrialLinkMirror, live: dict | None) -> dict:
        return {
            "id": m.id,
            "of_trial_id": m.of_trial_id,
            "name": m.name,
            "url": (live or {}).get("url") or m.url,
            "subscribe_days": (live or {}).get("subscribe_days") or m.subscribe_days,
            "subscribe_counts": (live or {}).get("subscribe_counts") or m.subscribe_counts,
            "claim_counts": (live or {}).get("claim_counts", m.claim_counts),
            "clicks_count": (live or {}).get("clicks_count"),
            "expired_at": (live or {}).get("expired_at"),
            # Only OF knows whether a link is spent. With no live row to read
            # (OF down, or the link no longer listed) say so rather than
            # implying it's live — /trials ordering is unstable, so "absent"
            # is not proof of "finished".
            "status": (live or {}).get("status", "unknown"),
            "created_at": m.created_at.isoformat() if m.created_at else None,
            "source": "mirror",
        }

    # OF links that have no mirror row (created outside fastt) — read-only.
    def _from_live(t: dict) -> dict:
        return {
            "id": None,
            "of_trial_id": t["of_trial_id"],
            "name": t["name"],
            "url": t["url"],
            "subscribe_days": t["subscribe_days"],
            "subscribe_counts": t["subscribe_counts"],
            "claim_counts": t["claim_counts"],
            "clicks_count": t["clicks_count"],
            "expired_at": t["expired_at"],
            "status": t["status"],
            "created_at": None,
            "source": "onlyfans",
        }

    out = merge_mirrors(mirrors, fetched, of_id_attr="of_trial_id",
                        live_id_key="of_trial_id",
                        from_mirror=_from_mirror, from_live=_from_live)
    # None = the OF fetch failed (not reachable); [] = reachable but no links.
    return {"links": out, "of_available": fetched is not None}


class _CreateBody(BaseModel):
    account_id: str
    name: str
    subscribe_days: int = 7
    subscribe_counts: int = 1
    expired_at: str | None = None


@router.post("/admin/trial-links")
async def create_trial_link(body: _CreateBody = Body(...)) -> dict[str, Any]:
    assert_account_owned(body.account_id)
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(422, "name is required")
    # OF rejects subscribeDays < 1 outright ("SubscribeDays must be at least 1"),
    # so 0 is invalid here, NOT "unlimited". subscribeCounts=0 IS accepted and
    # means unlimited claims. Both verified live 2026-07-23 on Ava.
    days = max(1, min(int(body.subscribe_days), 365))
    counts = max(0, min(int(body.subscribe_counts), 100000))

    try:
        # _make_client is inside the try so a missing/expired session surfaces
        # as a clean 502 (with a reason) rather than an unhandled 500.
        client = await asyncio.to_thread(ax._make_client, body.account_id)
        res = await asyncio.to_thread(
            lambda: client.create_trial_link(
                name=name, subscribe_days=days,
                subscribe_counts=counts, expired_at=body.expired_at),
        )
    except Exception as e:
        log.warning("create_trial_link OF call failed account=%s", body.account_id, exc_info=True)
        raise_if_free_page_denied(e, "trial links")
        raise HTTPException(502, f"OnlyFans trial link create failed (session/API): {repr(e)[:200]}")

    of = _normalise_of_trial(res if isinstance(res, dict) else {})
    now = datetime.utcnow()
    async with get_session() as s:
        row = TrialLinkMirror(
            account_id=body.account_id,
            of_trial_id=of.get("of_trial_id"),
            name=name, subscribe_days=days, subscribe_counts=counts,
            url=of.get("url"), claim_counts=of.get("claim_counts") or 0,
            created_at=now,
        )
        s.add(row)
        await s.flush()
        mirror_id = row.id
    log.info("trial_link_created id=%s account=%s of_id=%s", mirror_id, body.account_id, of.get("of_trial_id"))
    return {"id": mirror_id, "of_trial_id": of.get("of_trial_id"), "name": name,
            "url": of.get("url"), "subscribe_days": days, "subscribe_counts": counts,
            "claim_counts": of.get("claim_counts") or 0,
            "clicks_count": of.get("clicks_count") or 0,
            "expired_at": of.get("expired_at"), "status": "active",
            "source": "mirror"}


@router.delete("/admin/trial-links/{mirror_id}")
async def delete_trial_link(mirror_id: int) -> dict[str, Any]:
    return await delete_mirrored(
        TrialLinkMirror, mirror_id, of_id_attr="of_trial_id",
        of_delete=lambda c, oid: c.delete_trial_link(oid),
        not_found="trial link not found", label="trial_link")
