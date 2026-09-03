"""service/of_tracking_api.py — OnlyFans-NATIVE tracking links (/campaigns).

Distinct from the internal /t/<slug> redirector (tracking_links_api.py). These
are OnlyFans' OWN tracking links: OF mints a short profile URL per campaign and
reports, natively, how many people CLICKED it (countTransitions) and how many of
those actually SUBSCRIBED (countSubscribers) — real subscriber attribution the
internal redirect can never have.

OF is the entire source of truth (it stores the name too), so there is NO mirror
table — we read/create/delete straight against /campaigns.

The public share URL:
    https://onlyfans.com/<username>/c<campaignCode>

⚠️ UNCONFIRMED FORMAT. The /campaigns payload carries only `campaignCode`, never
the URL — OF's web app builds the link client-side behind its "Copy link"
button, so it appears in no API body or capture. This `/<user>/c<code>` form is
strongly implied (the code is the only variable; the captured "google" campaign
is code 5) but was NOT verifiable from our side: OF registers a click via a
client-side JS beacon with bot-filtering, so a server-side hit never moves the
counter, whatever URL we try. Shipping it as the operator asked, for them to
confirm with one real click. `url_verified: false` rides every row so the UI can
say so. Single source of truth for the format is `_campaign_url` below — fix it
in one place the moment a real link is pasted.

TODO (parked): the operator wants the COLLECTIVE SPEND of the fans a link
brought in, printed next to the click/sub counts. Not possible from this
payload — countSubscribers is a scalar, and no OF endpoint returns WHICH fan
ids a campaign produced. Design note + the three ways to get it anyway:
library/TRACKING_LINK_SPEND_PARKED.md

Routes (owner-gated, under /admin/*):
  GET    /admin/of-tracking-links?account_id=   list campaigns (+ url, stats)
  POST   /admin/of-tracking-links               create {name}
  DELETE /admin/of-tracking-links/{campaign_id}?account_id=   delete on OF
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query
from pydantic import BaseModel

import automation_executor as ax
from auth import assert_account_owned
from of_promos import safe_int

log = logging.getLogger("of-relay.of_tracking_api")

router = APIRouter()

# The share-URL format lives in exactly one place. See the module note.
_URL_VERIFIED = False


def _campaign_url(username: str | None, code: Any) -> str | None:
    if not username or code is None:
        return None
    return f"https://onlyfans.com/{username}/c{code}"


def _serialize(c: dict, username: str | None) -> dict:
    code = c.get("campaignCode")
    return {
        "id": c.get("id"),
        "code": code,
        "name": c.get("campaignName") or "",
        "url": _campaign_url(username, code),
        "url_verified": _URL_VERIFIED,
        # OF's native attribution — the whole reason to prefer these.
        "clicks": safe_int(c.get("countTransitions")),
        "subscribers": safe_int(c.get("countSubscribers")),
        "created_at": c.get("createdAt"),
        "end_date": c.get("endDate"),
        "is_deleted": bool(c.get("isDeleted")),
        "source": "onlyfans",
    }


async def _client_and_username(account_id: str):
    """(client, username) or raises 502 if the account has no live session.
    username is best-effort — a missing me() must not break list/create."""
    client = await asyncio.to_thread(ax._make_client, account_id)
    username = None
    try:
        me = await asyncio.to_thread(client.me)
        username = (me or {}).get("username")
    except Exception:
        log.info("of-tracking: me() failed account=%s — url will be null", account_id, exc_info=True)
    return client, username


@router.get("/admin/of-tracking-links")
async def list_of_tracking_links(account_id: str = Query(...)) -> dict[str, Any]:
    assert_account_owned(account_id)
    try:
        client, username = await _client_and_username(account_id)
        raw = await asyncio.to_thread(client.tracking_links)
    except Exception:
        log.info("of-tracking list failed account=%s", account_id, exc_info=True)
        return {"links": [], "username": None, "url_verified": _URL_VERIFIED,
                "of_available": False}
    rows = raw.get("list", raw) if isinstance(raw, dict) else raw
    if not isinstance(rows, list):
        return {"links": [], "username": username, "url_verified": _URL_VERIFIED,
                "of_available": False}
    links = [_serialize(c, username) for c in rows
             if isinstance(c, dict) and not c.get("isDeleted")]
    return {"links": links, "username": username, "url_verified": _URL_VERIFIED,
            "of_available": True}


class _CreateBody(BaseModel):
    account_id: str
    name: str


@router.post("/admin/of-tracking-links")
async def create_of_tracking_link(body: _CreateBody = Body(...)) -> dict[str, Any]:
    assert_account_owned(body.account_id)
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(422, "name is required")

    try:
        client, username = await _client_and_username(body.account_id)
        res = await asyncio.to_thread(lambda: client.create_tracking_link(name=name))
    except Exception as e:
        log.warning("of-tracking create failed account=%s", body.account_id, exc_info=True)
        raise HTTPException(502, f"OnlyFans tracking-link create failed (session/API): {repr(e)[:200]}")

    # OF's create returns the campaign (bare dict) or a list holding it.
    created = res
    if isinstance(res, list) and res:
        created = res[0]
    if not isinstance(created, dict):
        # Fall back to re-reading the list to find it by name.
        try:
            raw = await asyncio.to_thread(client.tracking_links)
            rows = raw.get("list", raw) if isinstance(raw, dict) else raw
            created = next((c for c in rows if isinstance(c, dict)
                            and c.get("campaignName") == name and not c.get("isDeleted")), {})
        except Exception:
            created = {}
    out = _serialize(created, username)
    log.info("of_tracking_created account=%s id=%s code=%s", body.account_id, out["id"], out["code"])
    return out


@router.delete("/admin/of-tracking-links/{campaign_id}")
async def delete_of_tracking_link(campaign_id: int, account_id: str = Query(...)) -> dict[str, Any]:
    assert_account_owned(account_id)
    try:
        client = await asyncio.to_thread(ax._make_client, account_id)
        await asyncio.to_thread(client.delete_tracking_link, int(campaign_id))
    except Exception as e:
        log.warning("of-tracking delete failed account=%s id=%s", account_id, campaign_id, exc_info=True)
        raise HTTPException(502, f"OnlyFans tracking-link delete failed: {repr(e)[:200]}")
    log.info("of_tracking_deleted account=%s id=%s", account_id, campaign_id)
    return {"deleted": campaign_id}
