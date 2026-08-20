"""service/tenant_keys_api.py — Setup → Keys, THIS agency's own LLM keys.

Distinct from /admin/secrets (secrets_store) in both scope and blast radius:
those are the DEPLOYMENT's house keys, these are one owner's. An owner may only
ever read or write their own row — the user id comes from the session and is
never taken from the request, so there is no cross-tenant write to defend
against.

A signed-in User is required. A Chatter session resolves to no user here (a
different principal with a different cookie) and so gets a 401: chatters do the
messaging work, they do not hold the agency's billing credentials. Writes while
impersonating are already refused by the session middleware (impersonation is
read-only), so a founder cannot overwrite a friend's key while wearing their
identity.

The store itself is `tenant_keys`, which stays free of web imports because the
LLM money path imports it.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, HTTPException, Request
from pydantic import BaseModel, Field

import tenant_keys
from auth import (
    AuthedUser, _assert_admin_password, get_request_user, require_master,
)

router = APIRouter(prefix="/admin/my-llm-keys", tags=["tenant-keys"])


def _require_user() -> AuthedUser:
    user = get_request_user()
    if user is None:
        raise HTTPException(
            status_code=401,
            detail="sign in as an account owner to manage your API keys",
        )
    return user


@router.get("")
async def my_llm_keys_status() -> dict[str, Any]:
    """Masked status of the signed-in agency's provider keys, plus any of their
    accounts that a second owner is also linked to. Never the raw value — the
    hint is `••••` + the last four characters.

    The shared-account list rides along here rather than getting its own
    endpoint because it answers the same question the keys do — "why did my
    models stop replying" — and the card is where someone asking it looks."""
    user = _require_user()
    return {
        "providers": await tenant_keys.status(user.id),
        "shared_accounts": await tenant_keys.shared_accounts(user.account_ids),
    }


@router.put("")
async def my_llm_keys_set(request: Request) -> dict[str, Any]:
    """Set or clear this agency's keys. Body is {provider: value, …}; an empty
    string clears that provider. Send ONLY the providers you changed — an absent
    provider keeps its stored key, which is what lets the form leave untouched
    fields blank rather than round-tripping a mask back to us."""
    user = _require_user()
    try:
        body = await request.json()
    except Exception:
        body = None
    if not isinstance(body, dict):
        raise HTTPException(
            status_code=400, detail="expected a JSON object of provider→key")
    try:
        await tenant_keys.set_keys(user.id, body)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"providers": await tenant_keys.status(user.id)}


# ── Founder: set an agency's keys FOR them ────────────────────────────
#
# The managed flow this deployment actually runs: an agency connects its OF
# model and the founder does the rest. Everything else in that flow already
# works — a master sees every account through the fat set without needing a
# link, and Admin → Manage grants and revokes them. The one thing missing was
# the credential: `/admin/my-llm-keys` writes only the SIGNED-IN owner's row,
# and impersonation is read-only, so an agency that had not pasted a key could
# not be fixed from any screen.
#
# It is also what turns the upgrade step from "sign in as each owner in turn"
# into one page.
#
# Two gates, matching the neighbouring grant/revoke endpoints and then some:
# a signed-in MASTER for both verbs (a masked hint still leaks the last four
# characters of another tenant's key, so the read is not public either), plus
# the founder password on the write, because this stores a credential someone
# else's models will spend.

admin_router = APIRouter(prefix="/admin/users", tags=["tenant-keys"])


class AgencyKeysBody(BaseModel):
    admin_password: str = Field(..., min_length=1)
    # {provider: value}; "" clears. Only the providers named are touched.
    providers: dict[str, str] = Field(default_factory=dict)


async def _assert_agency(user_id: str) -> None:
    """404 on an id that names no agency, before either verb answers.

    Not defensive tidying — both verbs fail badly without it. The read hands
    back every provider unset, which is indistinguishable from a real agency
    that never pasted a key, so a mistyped id looks like the problem the screen
    exists to fix. The write dies on the `user_llm_keys` foreign key as a 500.
    Same check, same wording, as the grant endpoints one file over.
    """
    if not await tenant_keys.agency_exists(user_id):
        raise HTTPException(status_code=404, detail=f"unknown user {user_id!r}")


@admin_router.get("/llm-key-status")
async def agency_llm_key_status_all() -> dict[str, Any]:
    """Every agency, with how many of its models can currently TALK and which
    providers it already has a key for. Feeds the founder's Manage screen.

    Registered BEFORE `/{user_id}/llm-keys` and matched first because it is a
    literal path — but it is also a different shape (a list, no user id), so
    the two cannot be confused by a caller.

    Master only, and not merely because keys are involved: this is a roster of
    every tenant on the deployment, which is not one agency's business to read.
    Provider NAMES only — never a value or a hint.
    """
    require_master()
    return {"agencies": await tenant_keys.key_overview()}


@admin_router.get("/{user_id}/llm-keys")
async def agency_llm_keys_status(user_id: str) -> dict[str, Any]:
    """Masked status of ONE agency's provider keys, for the founder's Manage
    screen. Never the raw value."""
    require_master()
    await _assert_agency(user_id)
    return {"user_id": user_id, "providers": await tenant_keys.status(user_id)}


@admin_router.put("/{user_id}/llm-keys")
async def agency_llm_keys_set(
    user_id: str, body: AgencyKeysBody = Body(...),
) -> dict[str, Any]:
    """Set or clear one agency's keys on their behalf. Same rules as the
    self-serve card: "" clears, an absent provider keeps its stored value, and a
    masked placeholder is refused rather than stored."""
    require_master()
    _assert_admin_password(body.admin_password)
    await _assert_agency(user_id)
    try:
        await tenant_keys.set_keys(user_id, dict(body.providers))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"user_id": user_id, "providers": await tenant_keys.status(user_id)}
