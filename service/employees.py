"""
Employee CRUD + audit middleware.

The "employee picker" is auth-lite: no passwords, no sessions. The user
clicks who they are on first page load; the browser stores the chosen id
in localStorage and sends it as `X-Employee-Id` on every subsequent call.
The relay treats that header as the actor for audit purposes and refuses
to mutate state without it (in phase D — phase A only logs).

This module owns:
  • `GET    /admin/employees`           — list active + inactive
  • `POST   /admin/employees`           — create
  • `PATCH  /admin/employees/{id}`      — rename / recolor / disable
  • `DELETE /admin/employees/{id}`      — soft-delete (sets is_active=false)
  • `GET    /admin/audit`               — paginated audit log
  • `audit_middleware`                  — FastAPI middleware that writes
    a row to `actions` for every mutating request that succeeded.

Why a separate file and not inline in server.py: server.py is already
~2200 lines. Each new concern that fits cleanly into its own module
saves us a refactor pain later. Imported once from server.py at startup.
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import delete, desc, func, select

from db.engine import get_session
from db.models import Action, Employee, EmployeeAccountAccess, UserAccount
from auth import get_request_user

log = logging.getLogger("of-relay.employees")

router = APIRouter()

# Per-friend chatter quota. Counts ACTIVE rows only — soft-deleted
# employees stay around so audit history reads cleanly (see DELETE handler
# below), and recycling slots after firing someone is normal. 0 / unset
# disables the cap. Founder bypasses by raising this in the env or by
# leaving it unset on their own deploy.
MAX_EMPLOYEES_PER_USER = int(os.environ.get("MAX_EMPLOYEES_PER_USER", "20") or "0")

# Default colors cycled when an employee is created without one specified.
# Picked to be distinguishable on the dark UI + colorblind-safe enough.
_DEFAULT_COLORS = [
    "#8b5cf6",  # violet
    "#22d3ee",  # cyan
    "#f59e0b",  # amber
    "#ef4444",  # red
    "#10b981",  # emerald
    "#ec4899",  # pink
    "#84cc16",  # lime
    "#06b6d4",  # sky
]


# ── Ownership helper ──────────────────────────────────────────────────

async def _load_owned_employee(s, employee_id: int) -> Employee:
    """Return the Employee row only if owned by the signed-in friend.
    Raises 404 if missing or not owned. Unauthed callers see every row
    (back-compat). Centralised so per-employee endpoints stay one-liners.
    """
    e = await s.get(Employee, employee_id)
    if e is None:
        raise HTTPException(status_code=404, detail=f"no employee with id {employee_id}")
    user = get_request_user()
    if user is not None and e.user_id != user.id:
        raise HTTPException(status_code=404, detail=f"no employee with id {employee_id}")
    return e


# ── CRUD endpoints ────────────────────────────────────────────────────

class _EmployeeCreateBody(BaseModel):
    display_name: str = Field(..., min_length=1, max_length=64)
    color: str | None = Field(None, description="Hex color; defaults to a cycled palette entry")
    is_active: bool = True


class _EmployeeUpdateBody(BaseModel):
    display_name: str | None = Field(None, min_length=1, max_length=64)
    color: str | None = None
    is_active: bool | None = None


@router.get("/admin/employees")
async def list_employees(include_disabled: bool = Query(True)) -> dict[str, Any]:
    """Every employee row owned by the signed-in friend. The browser picker
    reads this on first load.

    Default returns all rows so the Settings → Employees table can show
    both active and disabled (with a strikethrough). Pass
    `?include_disabled=false` for the inline picker dropdown.

    `chatter_id` is exposed so the owner's "Who's chatting?" picker can
    hide auto-created per-(chatter, owner) mirror rows (the picker is
    "which member of my team is at the keyboard," not "impersonate a
    chatter"). Callers like the orphan-tip assign dropdown still want
    them visible so they can attribute revenue to a chatter.

    Unauthed requests (no session cookie) see the full roster (back-compat
    for internal scripts) but never the system Automation sentinel.
    """
    user = get_request_user()
    async with get_session() as s:
        stmt = select(Employee).order_by(Employee.created_at)
        if not include_disabled:
            stmt = stmt.where(Employee.is_active == True)  # noqa: E712
        if user is not None:
            stmt = stmt.where(Employee.user_id == user.id)
        else:
            # Hide the system Automation sentinel from unauthed listings.
            stmt = stmt.where(Employee.user_id.isnot(None))
        rows = (await s.execute(stmt)).scalars().all()
        return {
            "employees": [
                {
                    "id": e.id,
                    "display_name": e.display_name,
                    "color": e.color,
                    "is_active": e.is_active,
                    "chatter_id": e.chatter_id,
                    "created_at": e.created_at.isoformat() if e.created_at else None,
                }
                for e in rows
            ]
        }


@router.post("/admin/employees")
async def create_employee(body: _EmployeeCreateBody = Body(...)) -> dict[str, Any]:
    """Create one, owned by the signed-in friend. No de-dupe on
    display_name — Tim and Tim coexist if you want them to. The picker
    disambiguates by id."""
    user = get_request_user()
    async with get_session() as s:
        # Quota gate — applies only to authed friends. Unauthed callers
        # (legacy curl flows / Automation tooling) are exempt because they
        # can't be one bad actor in a multi-tenant deploy.
        if user is not None and MAX_EMPLOYEES_PER_USER > 0:
            active_count = (await s.execute(
                select(func.count(Employee.id)).where(
                    Employee.user_id == user.id,
                    Employee.is_active == True,  # noqa: E712
                )
            )).scalar_one()
            if active_count >= MAX_EMPLOYEES_PER_USER:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"employee cap reached ({active_count}/{MAX_EMPLOYEES_PER_USER}). "
                        f"Disable an existing chatter or ask the founder to raise "
                        f"MAX_EMPLOYEES_PER_USER on the relay."
                    ),
                )
        # Cycle colors when caller didn't pick one. We use the count of
        # the SIGNED-IN USER's existing employees (or all if unauthed),
        # so each user's palette is independent.
        if not body.color:
            base_q = select(Employee)
            if user is not None:
                base_q = base_q.where(Employee.user_id == user.id)
            count = (await s.execute(base_q)).scalars().all()
            body_color = _DEFAULT_COLORS[len(count) % len(_DEFAULT_COLORS)]
        else:
            body_color = _normalize_color(body.color)

        e = Employee(
            display_name=body.display_name.strip(),
            color=body_color,
            is_active=body.is_active,
            user_id=user.id if user is not None else None,
        )
        s.add(e)
        await s.flush()
        return {
            "id": e.id,
            "display_name": e.display_name,
            "color": e.color,
            "is_active": e.is_active,
        }


@router.patch("/admin/employees/{employee_id}")
async def update_employee(
    employee_id: int, body: _EmployeeUpdateBody = Body(...)
) -> dict[str, Any]:
    """Rename, recolor, or toggle active. Empty body = no-op."""
    async with get_session() as s:
        e = await _load_owned_employee(s, employee_id)
        # Re-enabling a disabled chatter is functionally the same as
        # creating one — enforce the same quota so a user can't dodge it
        # by toggling is_active.
        if (
            body.is_active is True
            and e.is_active is False
            and e.user_id
            and MAX_EMPLOYEES_PER_USER > 0
        ):
            active_count = (await s.execute(
                select(func.count(Employee.id)).where(
                    Employee.user_id == e.user_id,
                    Employee.is_active == True,  # noqa: E712
                )
            )).scalar_one()
            if active_count >= MAX_EMPLOYEES_PER_USER:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"cannot re-enable: employee cap reached "
                        f"({active_count}/{MAX_EMPLOYEES_PER_USER})."
                    ),
                )
        if body.display_name is not None:
            e.display_name = body.display_name.strip()
        if body.color is not None:
            e.color = _normalize_color(body.color)
        if body.is_active is not None:
            e.is_active = body.is_active
        return {
            "id": e.id,
            "display_name": e.display_name,
            "color": e.color,
            "is_active": e.is_active,
        }


class _EmployeeTransferBody(BaseModel):
    to_user_id: str = Field(..., min_length=1, max_length=64)


@router.post("/admin/employees/{employee_id}/transfer")
async def transfer_employee(
    employee_id: int, body: _EmployeeTransferBody = Body(...)
) -> dict[str, Any]:
    """Hand one of your employees over to another friend.

    Ownership is the auth — `_load_owned_employee` 404s if the chatter
    isn't yours. The destination user must exist; transferring to
    yourself is a 400. Per-account access grants are wiped because
    `EmployeeAccountAccess.account_id` rows can point at accounts the
    new owner doesn't hold, and silently carrying those across owners
    would be a cross-tenant leak. The new owner re-grants access from
    their own picker.
    """
    from db.models import User  # local import — avoids circular dep at boot
    user = get_request_user()
    if user is None:
        raise HTTPException(status_code=401, detail="sign in required")
    if user.id == body.to_user_id:
        raise HTTPException(
            status_code=400, detail="cannot transfer to yourself"
        )
    async with get_session() as s:
        e = await _load_owned_employee(s, employee_id)
        # Chatter-mirror rows cannot be transferred: the chatter→owner
        # link lives in `chatter_users`, not on the Employee row, so
        # moving the mirror to another friend would orphan it (the new
        # owner isn't in the chatter's linked set, so the chatter's
        # next sign-in re-creates a fresh mirror under the original
        # owner and the transferred row goes stale). The owner-facing
        # flow for handing a chatter to another friend is on the
        # Chatters tab (unlink + the new owner re-invites).
        if e.chatter_id is not None:
            raise HTTPException(
                status_code=400,
                detail=(
                    "this employee is a chatter mirror — transfer it from "
                    "the Chatters tab (unlink, then have the new owner "
                    "invite the chatter)."
                ),
            )
        target_exists = (await s.execute(
            select(User.id).where(User.id == body.to_user_id)
        )).scalar_one_or_none()
        if target_exists is None:
            raise HTTPException(
                status_code=404, detail=f"unknown user {body.to_user_id!r}"
            )
        # Same quota rule as create / re-enable: an active employee landing
        # on a full destination would silently let that user dodge the cap.
        if e.is_active and MAX_EMPLOYEES_PER_USER > 0:
            active_count = (await s.execute(
                select(func.count(Employee.id)).where(
                    Employee.user_id == body.to_user_id,
                    Employee.is_active == True,  # noqa: E712
                )
            )).scalar_one()
            if active_count >= MAX_EMPLOYEES_PER_USER:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"destination employee cap reached "
                        f"({active_count}/{MAX_EMPLOYEES_PER_USER})."
                    ),
                )
        e.user_id = body.to_user_id
        await s.execute(
            delete(EmployeeAccountAccess).where(
                EmployeeAccountAccess.employee_id == employee_id
            )
        )
        return {
            "ok": True,
            "id": e.id,
            "display_name": e.display_name,
            "user_id": e.user_id,
        }


@router.get("/admin/employees/{employee_id}/actions")
async def list_employee_actions(
    employee_id: int,
    limit: int = Query(50, ge=1, le=500),
    from_at: str | None = Query(
        None,
        description="ISO timestamp cursor; returns rows strictly older than this.",
    ),
) -> dict[str, Any]:
    """Per-employee audit log. `from_at` is a keyset cursor: pass the
    `at` of the oldest row from the previous page to get the next page.
    Newest-first; missing employee returns 404 so the UI can show a stale
    link clearly instead of an empty page that looks like silence."""
    async with get_session() as s:
        await _load_owned_employee(s, employee_id)
        stmt = (
            select(Action)
            .where(Action.employee_id == employee_id)
            .order_by(desc(Action.at))
            .limit(limit)
        )
        if from_at:
            try:
                cursor = datetime.fromisoformat(from_at.replace("Z", "+00:00"))
            except ValueError as e:
                raise HTTPException(
                    status_code=400, detail=f"invalid from_at {from_at!r}: {e}"
                ) from e
            stmt = stmt.where(Action.at < cursor)
        rows = (await s.execute(stmt)).scalars().all()
        return {
            "employee_id": employee_id,
            "actions": [
                {
                    "id": a.id,
                    "account_id": a.account_id,
                    "action": a.action,
                    "target_type": a.target_type,
                    "target_id": a.target_id,
                    "payload": _safe_loads(a.payload_json),
                    "at": a.at.isoformat() if a.at else None,
                }
                for a in rows
            ],
            "limit": limit,
            "next_from_at": rows[-1].at.isoformat() if len(rows) == limit and rows[-1].at else None,
        }


@router.get("/admin/employees/{employee_id}/access")
async def list_employee_access(employee_id: int) -> dict[str, Any]:
    """Which accounts this employee can touch. `account_ids` may contain
    `None` — that's the wildcard "every account" row from §1 of 04. The UI
    surfaces it as an "all accounts" toggle and treats it as exclusive of
    any explicit ids."""
    async with get_session() as s:
        await _load_owned_employee(s, employee_id)
        rows = (await s.execute(
            select(EmployeeAccountAccess.account_id)
            .where(EmployeeAccountAccess.employee_id == employee_id)
        )).all()
        ids = [r[0] for r in rows]
        return {
            "employee_id": employee_id,
            "account_ids": ids,
            "wildcard": None in ids,
        }


class _EmployeeAccessBody(BaseModel):
    account_ids: list[str | None] = Field(
        default_factory=list,
        description="Account ids to grant. Include `null` for the wildcard.",
    )
    wildcard: bool | None = Field(
        None,
        description="If true, replaces the list with a single NULL (all-accounts) row.",
    )


@router.put("/admin/employees/{employee_id}/access")
async def set_employee_access(
    employee_id: int, body: _EmployeeAccessBody = Body(...)
) -> dict[str, Any]:
    """Replace the full access set in one shot. Idempotent. We delete +
    re-insert rather than diff because the table is tiny (one row per
    granted account) and atomic replacement makes the intent obvious in
    the audit log."""
    async with get_session() as s:
        await _load_owned_employee(s, employee_id)

        if body.wildcard:
            target: list[str | None] = [None]
        else:
            # De-dupe while preserving the wildcard sentinel.
            seen: set[str | None] = set()
            target = []
            for a in body.account_ids:
                key = a if a else None
                if key in seen:
                    continue
                seen.add(key)
                target.append(key)

        await s.execute(
            delete(EmployeeAccountAccess).where(
                EmployeeAccountAccess.employee_id == employee_id
            )
        )
        for aid in target:
            s.add(EmployeeAccountAccess(employee_id=employee_id, account_id=aid))
        await s.flush()
        return {
            "employee_id": employee_id,
            "account_ids": target,
            "wildcard": None in target,
        }


@router.delete("/admin/employees/{employee_id}")
async def delete_employee(employee_id: int) -> dict[str, Any]:
    """SOFT delete — sets `is_active=false`. We never hard-delete because
    `actions.employee_id` keeps pointing at this row and we want the audit
    history readable. To truly remove, edit the SQL by hand."""
    async with get_session() as s:
        e = await _load_owned_employee(s, employee_id)
        e.is_active = False
        return {"ok": True, "id": employee_id, "is_active": False}


# ── Audit log read endpoint ─────────────────────────────────────────

@router.get("/admin/audit")
async def list_audit(
    employee_id: int | None = Query(None),
    account_id: str | None = Query(None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    """Paginated `actions` rows, filterable by employee + account.

    Scoped to the signed-in friend: only actions whose `account_id` is in
    their `user_accounts` set OR whose `employee_id` is one they own.
    Filter parameters (`employee_id`, `account_id`) are intersected with
    that scope — they can never widen it. Unauthed callers see everything
    (back-compat for internal curl flows).
    """
    user = get_request_user()
    async with get_session() as s:
        stmt = select(Action).order_by(desc(Action.at)).limit(limit).offset(offset)
        if user is not None:
            owned_emp_ids = (await s.execute(
                select(Employee.id).where(Employee.user_id == user.id)
            )).scalars().all()
            owned_emp_set = set(owned_emp_ids)
            conditions = []
            if user.account_ids:
                conditions.append(Action.account_id.in_(list(user.account_ids)))
            if owned_emp_set:
                conditions.append(Action.employee_id.in_(list(owned_emp_set)))
            if not conditions:
                return {"actions": [], "limit": limit, "offset": offset}
            from sqlalchemy import or_
            stmt = stmt.where(or_(*conditions))
            if employee_id is not None:
                if employee_id not in owned_emp_set:
                    return {"actions": [], "limit": limit, "offset": offset}
                stmt = stmt.where(Action.employee_id == employee_id)
            if account_id is not None:
                if account_id not in user.account_ids:
                    return {"actions": [], "limit": limit, "offset": offset}
                stmt = stmt.where(Action.account_id == account_id)
        else:
            if employee_id is not None:
                stmt = stmt.where(Action.employee_id == employee_id)
            if account_id is not None:
                stmt = stmt.where(Action.account_id == account_id)
        rows = (await s.execute(stmt)).scalars().all()
        return {
            "actions": [
                {
                    "id": a.id,
                    "employee_id": a.employee_id,
                    "account_id": a.account_id,
                    "action": a.action,
                    "target_type": a.target_type,
                    "target_id": a.target_id,
                    "payload": _safe_loads(a.payload_json),
                    "at": a.at.isoformat() if a.at else None,
                }
                for a in rows
            ],
            "limit": limit,
            "offset": offset,
        }


# ── System "Automation" employee lookup ─────────────────────────────

# Process-local cache of the Automation employee's id. Populated lazily on
# the first call; reused for the lifetime of the process. Skipped under
# pytest because fixtures swap DBs between tests and a cached id from
# the wrong DB would silently misattribute action rows.
_automation_emp_id_cache: int | None = None

# Guard so the middleware logs the "migration 0011 not run" WARNING at most
# once per process — without this, a misconfigured deploy would spam the log
# on every header-less /api/of/* request.
_automation_missing_warned: bool = False

# Guard so the middleware logs the "X-Employee-Id ignored under chatter
# session" WARNING at most once per process — a misconfigured client could
# otherwise spam it on every chatter request.
_chatter_header_ignored_warned: bool = False


def _warn_chatter_header_ignored_once() -> None:
    global _chatter_header_ignored_warned
    if _chatter_header_ignored_warned:
        return
    _chatter_header_ignored_warned = True
    log.warning(
        "X-Employee-Id from chatter client ignored — audit pipeline "
        "auto-resolves (chatter_id, account_owner) → Employee.id."
    )


async def resolve_outbound_employee_id(
    request: Request,
    account_id: str | None,
) -> int | None:
    """Pick the Employee.id that should be stamped on an outbound message.

    Used by the chat-send, mass-send, and queue-create handlers — anywhere
    a write needs `sent_by_employee_id` and the audit-middleware path
    isn't the writer (those handlers explicitly persist a `messages` row
    via service/attribution.py).

    Resolution order:
      1. `X-Employee-Id` header — owner UI picks an employee from the
         roster and sends it on every relay call.
      2. Chatter session — header is empty (chatters don't pick); look
         up or lazy-create the per-(chatter, account_owner) mirror
         Employee. Header from a chatter client is dropped (logged once)
         to keep audit attribution single-sourced.
      3. None — caller falls through to its Automation fallback.
    """
    emp_id_raw = request.headers.get("x-employee-id")
    user = get_request_user()
    # Owner session: trust the header (the audit middleware re-validates
    # ownership before writing to `actions`; we mirror that gate here).
    if user is not None and emp_id_raw and emp_id_raw.isdigit():
        emp_id = int(emp_id_raw)
        async with get_session() as s:
            owned = await s.get(Employee, emp_id)
        if owned is not None and owned.user_id == user.id:
            return emp_id
        # Spoof attempt — drop. The audit middleware will log it too.
        return None
    # Chatter session: ignore the header (defensive), resolve mirror.
    if user is None:
        from chatters import get_request_chatter as _grc
        chatter = _grc()
        if chatter is not None:
            if emp_id_raw:
                _warn_chatter_header_ignored_once()
            return await resolve_chatter_employee_id(
                chatter_id=chatter.id,
                owner_user_ids=chatter.owner_user_ids,
                account_id=account_id,
                display_name=chatter.display_name or chatter.username,
                color=chatter.color,
            )
    # Unauthed (internal curl / cron) — caller's Automation fallback wins.
    if emp_id_raw and emp_id_raw.isdigit():
        return int(emp_id_raw)
    return None


async def resolve_chatter_employee_id(
    chatter_id: str,
    owner_user_ids: set[str] | frozenset[str],
    account_id: str | None,
    *,
    display_name: str | None,
    color: str | None,
) -> int | None:
    """Find — or lazily create — the Employee row that mirrors this chatter
    under the account's OWNING user. Returns the Employee.id, or None if
    we can't pin the account to one of the chatter's linked owners.

    Uses the classic SELECT → INSERT → catch IntegrityError → SELECT
    upsert pattern (NOT dialect-specific ON CONFLICT). Previously used
    sqlite_insert + on_conflict_do_nothing against the partial unique
    index `uq_employees_chatter_owner`, which only works if that index
    actually exists on the live DB — and on prod boxes that came up via
    `init_db()` / `create_all()` rather than `alembic upgrade head`, the
    partial index isn't always present. Symptom: silent insert failure,
    SELECT-after-insert returns None, attribution lands as NULL, bubble
    shows no "Sent by …" label. This rewrite tolerates the missing
    index AND surfaces every decision point in the relay log.

    Race-safe regardless of index presence: if a sibling request raced
    us to the insert, we get IntegrityError (when the index IS present)
    or a duplicate row (when it isn't — friends-scale, acceptable). On
    IntegrityError we rollback + re-SELECT to capture the sibling's id.
    """
    if not account_id:
        log.debug("chatter→emp: no account_id (chatter=%s)", chatter_id)
        return None
    if not owner_user_ids:
        log.warning(
            "chatter→emp: chatter has zero linked owners (chatter=%s) — "
            "register a link via /admin/chatters/link",
            chatter_id,
        )
        return None
    try:
        async with get_session() as s:
            # 1. Which of the chatter's owners actually holds this account?
            #    If more than one (cross-grant edge case), pick the
            #    alphabetically first owner_id for stable attribution.
            owner_rows = (await s.execute(
                select(UserAccount.user_id).where(
                    UserAccount.account_id == account_id,
                    UserAccount.user_id.in_(list(owner_user_ids)),
                )
            )).all()
            if not owner_rows:
                log.warning(
                    "chatter→emp: no owner in chatter's linked set holds "
                    "account=%s (chatter=%s, owners=%s) — chatter.account_ids "
                    "is inconsistent with user_accounts table",
                    account_id, chatter_id, sorted(owner_user_ids),
                )
                return None
            owner_id = sorted(r[0] for r in owner_rows)[0]

            # 2. Fast path: row already exists.
            existing = (await s.execute(
                select(Employee.id).where(
                    Employee.chatter_id == chatter_id,
                    Employee.user_id == owner_id,
                )
            )).scalar_one_or_none()
            if existing is not None:
                return existing

            # 3. Plain INSERT (no dialect-specific ON CONFLICT). Catches
            #    IntegrityError from the partial unique index OR from a
            #    racing sibling that beat us to the same (chatter, owner)
            #    pair — either way the post-rollback SELECT picks up the
            #    canonical id.
            from sqlalchemy.exc import IntegrityError
            new_row = Employee(
                display_name=(display_name or "Chatter")[:64],
                color=color,
                is_active=True,
                created_at=datetime.utcnow(),
                user_id=owner_id,
                chatter_id=chatter_id,
            )
            s.add(new_row)
            try:
                await s.flush()
                log.info(
                    "chatter→emp: created mirror Employee id=%s "
                    "(chatter=%s owner=%s account=%s display=%r)",
                    new_row.id, chatter_id, owner_id, account_id,
                    new_row.display_name,
                )
                return new_row.id
            except IntegrityError:
                # Sibling raced us — roll back the failed insert and
                # SELECT to capture the canonical id from the winner.
                await s.rollback()
                existing = (await s.execute(
                    select(Employee.id).where(
                        Employee.chatter_id == chatter_id,
                        Employee.user_id == owner_id,
                    )
                )).scalar_one_or_none()
                if existing is None:
                    log.error(
                        "chatter→emp: IntegrityError on insert but no "
                        "matching row found post-rollback (chatter=%s "
                        "owner=%s) — schema may be missing the partial "
                        "unique index uq_employees_chatter_owner",
                        chatter_id, owner_id,
                    )
                return existing
    except Exception:
        log.exception(
            "chatter→emp: resolution failed (chatter=%s account=%s)",
            chatter_id, account_id,
        )
        return None


def _warn_automation_missing_once() -> None:
    global _automation_missing_warned
    if _automation_missing_warned:
        return
    _automation_missing_warned = True
    log.warning("Automation fallback unavailable — migration 0011 not run.")


async def get_automation_employee_id() -> int:
    """Returns the id of the system Automation employee row.

    No caching when running under pytest (detected via
    'PYTEST_CURRENT_TEST' in os.environ) — fixtures may swap DBs.
    Otherwise cached after first lookup.

    Raises LookupError if the row doesn't exist (migration 0011 hasn't
    run). Callers should catch this and degrade gracefully (e.g.
    write NULL instead of crashing the request).
    """
    global _automation_emp_id_cache
    in_test = "PYTEST_CURRENT_TEST" in os.environ
    if _automation_emp_id_cache is not None and not in_test:
        return _automation_emp_id_cache
    async with get_session() as s:
        row = (await s.execute(
            select(Employee.id).where(
                Employee.display_name == "Automation",
                Employee.is_active == False,  # noqa: E712 — SQL equality, not Python is-comparison
            )
        )).scalar_one_or_none()
    if row is None:
        raise LookupError(
            "Automation employee not found. Migration 0011 must run before "
            "this helper. Caller should handle this gracefully."
        )
    if not in_test:
        _automation_emp_id_cache = row
    return row


# ── Audit middleware ────────────────────────────────────────────────

# Endpoints we DON'T audit even when they're "POST/PATCH/DELETE." Adding
# a row for every health probe / SSE retry would drown the real signal.
_AUDIT_SKIP_PREFIXES = (
    "/health",
    "/events",
    "/ws/",
    "/admin/audit",     # don't audit reads of the audit log itself
    "/admin/rev/",       # drift probe — read-only
    "/docs",
    "/openapi.json",
    "/ui/", "/app/",     # static UI
)
# Note: do NOT include "/" here — every absolute path starts with "/", so
# `startswith("/")` would silently skip every endpoint. The root path is
# handled by the static-UI mount which only serves GET anyway.


async def audit_middleware(request: Request, call_next):
    """FastAPI HTTP middleware. Wraps every request so:
      • if method is mutating (POST / PATCH / PUT / DELETE) and the
        response is 2xx, we log a row to `actions`.
      • employee_id comes from the `X-Employee-Id` header (NULL if absent).
      • payload_json captures a truncated snapshot of the body for replay.

    Failure isolation: every step is try/except + log. A bad audit write
    never affects the actual response the user sees.
    """
    # Snapshot enough to log BEFORE call_next so a failed downstream still
    # gets recorded (with status_code set in the post-block).
    method = request.method
    path = request.url.path
    should_audit = (
        method in ("POST", "PATCH", "PUT", "DELETE")
        and not any(path.startswith(p) for p in _AUDIT_SKIP_PREFIXES)
    )

    # Reading the body consumes it; we have to re-inject for the handler.
    body_bytes = b""
    binary_payload_snapshot: str | None = None
    if should_audit:
        # Skip buffering binary uploads — a 30MB image would otherwise sit in
        # RSS for the lifetime of the request just so we can snapshot it.
        ctype = request.headers.get("content-type", "")
        if ctype.startswith(("multipart/", "application/octet-stream")):
            binary_payload_snapshot = (
                f"<binary upload, {request.headers.get('content-length', '?')} bytes>"
            )
        else:
            try:
                body_bytes = await request.body()
            except Exception:
                body_bytes = b""

            # Reinject so downstream handlers (Pydantic body parsers) can still
            # read it. FastAPI's request._body cache makes this stick.
            async def _receive() -> dict:
                return {"type": "http.request", "body": body_bytes, "more_body": False}
            request._receive = _receive  # type: ignore[attr-defined]

    response = await call_next(request)

    if should_audit and 200 <= response.status_code < 300:
        try:
            emp_id_raw = request.headers.get("x-employee-id")
            emp_id: int | None = None
            if emp_id_raw and emp_id_raw.isdigit():
                emp_id = int(emp_id_raw)
            account_id = (
                request.headers.get("x-account-id")
                or request.query_params.get("account_id")
            )
            user = get_request_user()
            # Chatter principal: the X-Employee-Id header is meaningless
            # (the chatter doesn't pick from /admin/employees and we want
            # one canonical per-owner mirror Employee anyway). Drop it
            # outright, then auto-resolve (chatter, account.owner_id) →
            # Employee, materialising the row on first hit. The User
            # cookie takes precedence — if a founder happens to be both
            # owner-signed-in AND chatter-signed-in (testing on the same
            # browser), the request still attributes to their User-owned
            # Employee via the existing spoof guard below.
            if user is None:
                # Lazy import: chatters imports employees indirectly via
                # the audit middleware path, so deferring keeps boot
                # order one-way (chatters → auth → employees).
                from chatters import get_request_chatter as _grc
                chatter = _grc()
                if chatter is not None:
                    if emp_id is not None:
                        _warn_chatter_header_ignored_once()
                        emp_id = None
                    emp_id = await resolve_chatter_employee_id(
                        chatter_id=chatter.id,
                        owner_user_ids=chatter.owner_user_ids,
                        account_id=account_id,
                        display_name=chatter.display_name or chatter.username,
                        color=chatter.color,
                    )
            # Cross-user spoof guard: if a signed-in friend passes an
            # X-Employee-Id, the chatter MUST belong to them. Otherwise
            # drop the attribution to NULL (or Automation downstream for
            # /api/of/* paths). Stops friend A from writing actions under
            # friend B's chatter.
            if emp_id is not None and user is not None:
                async with get_session() as _vs:
                    owned = await _vs.get(Employee, emp_id)
                if owned is None or owned.user_id != user.id:
                    log.warning(
                        "x-employee-id=%s rejected: not owned by user=%s (path=%s)",
                        emp_id, user.id, path,
                    )
                    emp_id = None
            # Header-less fallback for OF proxy paths only. When no human
            # pressed send (mass-message, scheduled jobs, automation that
            # happens to round-trip through HTTP), we attribute to the
            # system Automation employee so the row doesn't disappear from
            # per-employee stats.
            #
            # NOTE: in-process automation runners (subprocess.run /
            # direct function calls in service/automations/runners.py) do
            # NOT pass through this middleware. They must write their own
            # audit + attribution explicitly. This fallback only catches
            # the rare HTTP-routed automation case.
            #
            # /admin/* paths are intentionally NOT covered — a missing
            # X-Employee-Id there indicates a misconfigured client and
            # should stay NULL so it surfaces in the audit log.
            if emp_id is None and path.startswith("/api/of/"):
                try:
                    emp_id = await get_automation_employee_id()
                except LookupError:
                    _warn_automation_missing_once()
            await _write_action(
                employee_id=emp_id,
                account_id=account_id,
                action=f"{method} {path}",
                payload_json=binary_payload_snapshot if binary_payload_snapshot is not None else _safe_snapshot(body_bytes),
                target_type=None,
                target_id=_extract_target_id(path),
            )
        except Exception:
            log.exception("audit write failed (path=%s)", path)

    return response


async def _write_action(
    *, employee_id: int | None, account_id: str | None,
    action: str, payload_json: str | None,
    target_type: str | None, target_id: str | None,
) -> None:
    async with get_session() as s:
        s.add(Action(
            employee_id=employee_id,
            account_id=account_id,
            action=action,
            target_type=target_type,
            target_id=target_id,
            payload_json=payload_json,
            at=datetime.utcnow(),
        ))


def _safe_snapshot(body_bytes: bytes) -> str | None:
    """Best-effort string snapshot of the request body for the audit log.
    JSON: pretty-print up to 4 KB. Binary: skip. Empty: NULL."""
    if not body_bytes:
        return None
    try:
        text = body_bytes.decode("utf-8", errors="replace")[:4096]
        # If it looks like JSON, normalize so the audit-log view is readable.
        try:
            parsed = json.loads(text)
            # Redact obvious secrets so audit isn't a vector for leaks.
            if isinstance(parsed, dict):
                for k in list(parsed.keys()):
                    if any(s in k.lower() for s in ("password", "token", "secret", "api_key")):
                        parsed[k] = "***"
            return json.dumps(parsed)[:4096]
        except Exception:
            return text
    except Exception:
        return None


def _extract_target_id(path: str) -> str | None:
    """Heuristic: the last path segment is the target id if it's not a
    static verb. Catches `/admin/accounts/123456789`, `/admin/proxies/hu-1`.
    Returns None for fixed-shape paths like `/admin/session/bootstrap`."""
    parts = [p for p in path.split("/") if p]
    if not parts:
        return None
    last = parts[-1]
    # Don't treat known-action segments as ids.
    if last in {"active", "bootstrap", "assign", "test", "reload-session",
                "status", "events", "audit", "employees", "accounts",
                "proxies", "sessions"}:
        return None
    return last


def _safe_loads(s: str | None) -> Any:
    if not s:
        return None
    try:
        return json.loads(s)
    except Exception:
        return s


def _normalize_color(c: str) -> str:
    """Accept '#ABC', '#AABBCC', or 'abc' / 'aabbcc'; return '#aabbcc'."""
    c = c.strip().lower().lstrip("#")
    if re.fullmatch(r"[0-9a-f]{3}", c):
        c = "".join(ch * 2 for ch in c)
    if not re.fullmatch(r"[0-9a-f]{6}", c):
        raise HTTPException(status_code=400, detail=f"invalid color {c!r}, expected #RRGGBB")
    return "#" + c


# ── Optional: response header that tells the UI who the relay thinks
# the current employee is. Useful for sanity-checking the picker.

def install_response_header(request: Request, response: Response) -> None:
    """Echo back the X-Employee-Id we saw, so the client knows the relay
    actually parsed it. Called from a FastAPI dependency in phase B."""
    eid = request.headers.get("x-employee-id")
    if eid:
        response.headers["X-Employee-Id-Seen"] = eid
