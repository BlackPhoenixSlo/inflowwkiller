"""
Chatter auth — second principal alongside User. See Todo.txt STEP 8.

A Chatter is a human messenger; one Chatter can be linked to many owners
(Users) via the `chatter_users` join. The chatter's available-accounts
list is the SET UNION of every linked owner's `user_accounts` rows. Login
mints a cookie that's distinct from the User session cookie (different
name, different signing secret) so a stolen chatter cookie can't be used
to claim founder/owner privileges.

This module is intentionally a parallel to service/auth.py — same flow
(register → login → me → logout, sliding 30-day expiry, HMAC-signed
cookies) but no shared globals so the two principals can never accidentally
mix. Cookie precedence (when both happen to be present): the User cookie
wins for /admin/* (owner perspective); the Chatter cookie only drives
/chatter/* + provides current_actor() to the audit pipeline when no User
cookie is set. See service/auth.py session_middleware for the composition.

Tables (migration 0024_chatter_login):
  • chatters         — id/username/password (plaintext, friends-scale)/etc.
  • chatter_users    — many-to-many: which owners each chatter is linked to.
  • chatters_invites — owner-issued one-shot signup tokens (24h TTL).
"""

from __future__ import annotations

import hmac
import logging
import os
import secrets
import uuid
from contextvars import ContextVar
from datetime import datetime, timedelta
from hashlib import sha256
from pathlib import Path

log = logging.getLogger("of-relay.chatters")

from fastapi import APIRouter, Body, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError

from db.engine import get_session
from db.models import (
    Account,
    Chatter,
    ChatterAccountAccess,
    ChatterFolderAccess,
    ChatterInvite,
    ChatterUser,
    User,
    UserAccount,
)
from auth import (
    USERNAME_RE,
    RESERVED_USERNAMES,
    _normalize_username,
    _validate_password,
)


# ── Constants ─────────────────────────────────────────────────────────

COOKIE_NAME = "chatterly_chatter_session"
COOKIE_MAX_AGE_SECONDS = 30 * 24 * 3600
SESSION_INACTIVITY_SECONDS = 30 * 24 * 3600
INVITE_TTL_SECONDS = 24 * 3600

# Distinct secret from the User session secret on purpose: cross-principal
# forgery should require compromising BOTH files, not one. Persisted to
# service/.chatter_secret on first boot if CHATTER_SESSION_SECRET is
# unset. Same disk pattern as service/.session_secret.
_SECRET_FILE = Path(__file__).resolve().parent / ".chatter_secret"


def _load_session_secret() -> bytes:
    env = os.environ.get("CHATTER_SESSION_SECRET", "").strip()
    if env:
        return env.encode("utf-8")
    try:
        if _SECRET_FILE.exists():
            return _SECRET_FILE.read_text().strip().encode("utf-8")
    except OSError:
        pass
    new = secrets.token_urlsafe(48)
    try:
        _SECRET_FILE.write_text(new)
        os.chmod(_SECRET_FILE, 0o600)
    except OSError:
        pass
    return new.encode("utf-8")


_SESSION_SECRET = _load_session_secret()


# ── Identity snapshot ────────────────────────────────────────────────

class ChatterIdentity:
    """In-process snapshot of the signed-in chatter for one request.

    `account_ids` is precomputed as the SET UNION across all linked
    owners' UserAccount rows so `_resolve_account_id` doesn't pay a
    query per call. `owner_user_ids` is kept alongside for the audit
    pipeline's per-(chatter, account.owner) Employee lookup."""

    __slots__ = ("id", "username", "display_name", "color",
                 "owner_user_ids", "account_ids")

    def __init__(
        self,
        id_: str,
        username: str,
        display_name: str | None,
        color: str | None,
        owner_user_ids: frozenset[str],
        account_ids: frozenset[str],
    ):
        self.id = id_
        self.username = username
        self.display_name = display_name
        self.color = color
        self.owner_user_ids = owner_user_ids
        self.account_ids = account_ids


_request_chatter: ContextVar["ChatterIdentity | None"] = ContextVar(
    "chatterly_request_chatter", default=None,
)


def get_request_chatter() -> "ChatterIdentity | None":
    """Read the current request's signed-in chatter (or None)."""
    return _request_chatter.get()


# ── Cookie signing ────────────────────────────────────────────────────

def _sign(payload: str) -> str:
    return hmac.new(_SESSION_SECRET, payload.encode("utf-8"), sha256).hexdigest()


def _make_cookie(chatter_id: str) -> str:
    issued = str(int(datetime.utcnow().timestamp()))
    payload = f"{chatter_id}.{issued}"
    return f"{payload}.{_sign(payload)}"


def _parse_cookie(value: str) -> str | None:
    """Return the chatter_id if the cookie's HMAC verifies, else None."""
    parts = value.split(".")
    if len(parts) != 3:
        return None
    chatter_id, issued, sig = parts
    expected = _sign(f"{chatter_id}.{issued}")
    if not hmac.compare_digest(sig, expected):
        return None
    return chatter_id


def _set_session_cookie(response: Response, chatter_id: str) -> None:
    response.set_cookie(
        COOKIE_NAME,
        _make_cookie(chatter_id),
        max_age=COOKIE_MAX_AGE_SECONDS,
        httponly=True,
        samesite="lax",
        secure=os.environ.get("SESSION_COOKIE_SECURE", "").lower() in {"1", "true", "yes"},
    )


def _clear_session_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, samesite="lax")


# ── Helpers ───────────────────────────────────────────────────────────

def _validate_username(username: str) -> None:
    """Same rules as User.username so a chatter who later wants to become
    an owner doesn't have to rename. Mirrors auth._validate_username."""
    if not USERNAME_RE.fullmatch(username):
        raise HTTPException(
            status_code=400,
            detail="username must be 3-32 chars, lowercase a-z 0-9 . _ -",
        )
    if username in RESERVED_USERNAMES:
        raise HTTPException(status_code=400, detail=f"username {username!r} is reserved")


async def _account_restrictions_for(s, chatter_id: str) -> dict[str, frozenset[str]]:
    """Per-owner account allowlist for a chatter (Chatter Access, step 2/3).

    Returns {owner_user_id: {allowed_account_id, ...}} built from
    chatter_account_access. An owner PRESENT in the map restricts the
    chatter to that owner's listed accounts; an owner ABSENT imposes NO
    restriction (full access to that owner's models). This is purely
    SUBTRACTIVE and scoped per (chatter, owner): owner A's rows never
    touch owner B's contribution. Empty map ⇒ no restrictions anywhere
    (backwards-compatible with chatters created before this feature)."""
    rows = (await s.execute(
        select(
            ChatterAccountAccess.user_id,
            ChatterAccountAccess.account_id,
        ).where(ChatterAccountAccess.chatter_id == chatter_id)
    )).all()
    out: dict[str, set[str]] = {}
    for uid, aid in rows:
        out.setdefault(uid, set()).add(aid)
    return {uid: frozenset(aids) for uid, aids in out.items()}


def _filter_owner_accounts(
    pairs: "list[tuple[str, str]]",
    restrictions: dict[str, frozenset[str]],
) -> set[str]:
    """Apply the per-owner allowlist from `_account_restrictions_for` to a
    list of (owner_user_id, account_id) candidate pairs, returning the set
    of account_ids the chatter may see. An owner not in `restrictions`
    contributes all of its accounts; an owner in it contributes only the
    intersection with its allowlist."""
    allowed: set[str] = set()
    for uid, aid in pairs:
        limit = restrictions.get(uid)
        if limit is None or aid in limit:
            allowed.add(aid)
    return allowed


async def allowed_folder_ids_for_chatter(account_id: str) -> "frozenset[int] | None":
    """Folder-visibility gate for the vault relay (Chatter Access, step 4).

    Returns:
      • None  — NO folder restriction applies. Either the acting principal
        is NOT a chatter (DA-1: a User cookie present ⇒ owner perspective,
        even with a chatter cookie also set), or the chatter simply has no
        chatter_folder_access rows for this account. Caller passes media
        through unchanged.
      • frozenset[int] — the chatter IS restricted; only these OF vault
        folder ids may be shown. Always non-empty (DA-2: an empty restricted
        set is never persisted; "see nothing" = unlink the chatter).

    DA-1 is enforced via current_actor().kind == "chatter" — NOT
    `get_request_chatter() is not None`, which is also true for a
    dual-cookie owner. Mirrors `_resolve_account_id`'s user-first order."""
    actor = current_actor()
    if actor is None or actor.kind != "chatter":
        return None
    async with get_session() as s:
        rows = (await s.execute(
            select(ChatterFolderAccess.folder_id).where(
                ChatterFolderAccess.chatter_id == actor.id,
                ChatterFolderAccess.account_id == account_id,
            )
        )).all()
    if not rows:
        return None
    return frozenset(r[0] for r in rows)


async def _load_identity_from_db(chatter_id: str) -> "ChatterIdentity | None":
    """Hydrate a ChatterIdentity by joining
        chatters → chatter_users → user_accounts (+ users for usernames).

    Returns None if the chatter row is missing or inactive. Computes
    `account_ids` as a SET (de-dupes when two owners both granted the same
    account). One DB roundtrip; cheap enough to do per-request — the
    middleware caches in the ContextVar so deep code paths don't re-pay.

    Chatter Access (step 2): per-owner chatter_account_access rows trim each
    owner's contribution to the union BEFORE the set-union, so the resulting
    frozenset (and therefore the existing `_resolve_account_id` /
    `assert_account_owned` gate) only ever sees allowed accounts."""
    async with get_session() as s:
        row = (await s.execute(
            select(Chatter).where(Chatter.id == chatter_id)
        )).scalar_one_or_none()
        if row is None or not row.is_active:
            return None

        owner_rows = (await s.execute(
            select(ChatterUser.user_id).where(ChatterUser.chatter_id == chatter_id)
        )).all()
        owner_ids = {r[0] for r in owner_rows}

        if owner_ids:
            acct_rows = (await s.execute(
                select(
                    UserAccount.user_id,
                    UserAccount.account_id,
                ).where(UserAccount.user_id.in_(list(owner_ids)))
            )).all()
            restrictions = await _account_restrictions_for(s, chatter_id)
            account_ids: set[str] = _filter_owner_accounts(
                [(uid, aid) for uid, aid in acct_rows], restrictions
            )
        else:
            account_ids = set()

        return ChatterIdentity(
            id_=row.id,
            username=row.username,
            display_name=row.display_name,
            color=row.color,
            owner_user_ids=frozenset(owner_ids),
            account_ids=frozenset(account_ids),
        )


# ── Public routes ────────────────────────────────────────────────────

router = APIRouter(prefix="/chatter", tags=["chatter-auth"])


class _LoginBody(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=256)


class _RegisterBody(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=256)
    display_name: str | None = Field(None, max_length=64)


class OwnerAccountDTO(BaseModel):
    account_id: str
    nickname: str | None = None
    color: str | None = None


class OwnerDTO(BaseModel):
    user_id: str
    username: str
    accounts: list[OwnerAccountDTO]


class FlatAccountDTO(BaseModel):
    account_id: str
    nickname: str | None = None
    color: str | None = None
    owner_id: str
    owner_username: str


class ChatterMeResp(BaseModel):
    chatter_id: str
    username: str
    display_name: str | None = None
    color: str | None = None
    owners: list[OwnerDTO]


class InvitePreviewResp(BaseModel):
    """Pre-register peek at an invite token. The chatter-login UI fetches
    this to show 'invited by @alice — expires in N hours' so the new
    chatter knows what they're accepting before they create an account.

    Read-only: does NOT consume the invite. Only the actual POST
    /chatter/register call marks it used."""
    username: str = Field(..., description="username of the issuing owner")
    expires_at: datetime
    used: bool


@router.get("/invite/{token}", response_model=InvitePreviewResp)
async def invite_preview(token: str) -> InvitePreviewResp:
    """Return the issuing owner + status for an invite token. Surfaced on
    the /chatter-login form so the new chatter sees which owner is
    inviting them. 404 if the token doesn't exist (typo / forged URL);
    we DON'T 410 on used/expired here — the form needs to render an
    explanatory message rather than a blank rejection."""
    async with get_session() as s:
        row = (await s.execute(
            select(ChatterInvite, User.username)
            .join(User, User.id == ChatterInvite.user_id)
            .where(ChatterInvite.token == token)
        )).first()
        if row is None:
            raise HTTPException(status_code=404, detail="unknown invite token")
        invite, username = row.ChatterInvite, row.username
    return InvitePreviewResp(
        username=username,
        expires_at=invite.expires_at,
        used=invite.used_at is not None or invite.expires_at < datetime.utcnow(),
    )


@router.post("/register", response_model=ChatterMeResp)
async def register(
    response: Response,
    body: _RegisterBody = Body(...),
    token: str = Query(..., description="invite token from /admin/chatters/invite"),
) -> ChatterMeResp:
    """Register a new chatter. Gated by an owner-issued invite token —
    we never accept random self-signups. On success the issuing owner is
    auto-linked so the chatter sees that owner's accounts on first login.

    The invite is consumed on success (used_at set, used_by_chatter_id
    stamped); subsequent attempts with the same token 410 Gone.
    """
    username = _normalize_username(body.username)
    _validate_username(username)
    _validate_password(body.password)

    now = datetime.utcnow()
    new_id = uuid.uuid4().hex

    async with get_session() as s:
        invite = (await s.execute(
            select(ChatterInvite).where(ChatterInvite.token == token)
        )).scalar_one_or_none()
        if invite is None:
            raise HTTPException(status_code=404, detail="unknown invite token")
        if invite.used_at is not None:
            raise HTTPException(status_code=410, detail="invite already used")
        if invite.expires_at < now:
            raise HTTPException(status_code=410, detail="invite expired")
        # Defensive: the issuing owner may have been deleted (CASCADE
        # would also wipe the invite, but it's cheap to confirm).
        owner_exists = (await s.execute(
            select(User.id).where(User.id == invite.user_id)
        )).scalar_one_or_none()
        if owner_exists is None:
            raise HTTPException(status_code=410, detail="issuing owner no longer exists")

        s.add(Chatter(
            id=new_id,
            username=username,
            password=body.password,  # plaintext, friends-scale
            display_name=(body.display_name or "").strip() or None,
            created_at=now,
            last_seen_at=now,
        ))
        try:
            await s.flush()
        except IntegrityError:
            raise HTTPException(status_code=409, detail="username already taken")

        # Auto-link to the issuing owner; consume invite atomically with
        # the chatter insert so a partial register never leaves a
        # consumed invite around.
        s.add(ChatterUser(
            chatter_id=new_id, user_id=invite.user_id, created_at=now,
        ))
        invite.used_at = now
        invite.used_by_chatter_id = new_id
        await s.flush()

    _set_session_cookie(response, new_id)
    identity = await _load_identity_from_db(new_id)
    return await _me_response(identity)


@router.post("/login", response_model=ChatterMeResp)
async def login(response: Response, body: _LoginBody = Body(...)) -> ChatterMeResp:
    username = _normalize_username(body.username)
    if not username:
        raise HTTPException(status_code=400, detail="username required")

    async with get_session() as s:
        row = (await s.execute(
            select(Chatter).where(Chatter.username == username)
        )).scalar_one_or_none()
        if row is None or row.password != body.password:
            # Same response for both to avoid leaking which is wrong.
            raise HTTPException(status_code=401, detail="invalid username or password")
        if not row.is_active:
            raise HTTPException(
                status_code=403,
                detail="this chatter is disabled — ask your owner to re-enable",
            )
        await s.execute(update(Chatter).where(
            Chatter.id == row.id,
        ).values(last_seen_at=datetime.utcnow()))
        cid = row.id

    _set_session_cookie(response, cid)
    identity = await _load_identity_from_db(cid)
    return await _me_response(identity)


@router.post("/logout")
async def logout(response: Response) -> dict[str, bool]:
    # Wipe every principal cookie (chatter + user + impersonate) so a
    # surviving cross-principal cookie can't re-authenticate the next
    # request after explicit logout. Reuses the auth helper so the set
    # of cookies stays single-sourced.
    from auth import clear_all_principal_cookies
    clear_all_principal_cookies(response)
    return {"ok": True}


@router.get("/me", response_model=ChatterMeResp | None)
async def me() -> ChatterMeResp | None:
    """Who is signed in on this request (or None). Used by the Next app
    shell to choose between the chatter dashboard and a redirect to
    /chatter-login."""
    c = get_request_chatter()
    if c is None:
        return None
    return await _me_response(c)


@router.get("/me/owners", response_model=list[OwnerDTO])
async def me_owners() -> list[OwnerDTO]:
    """Grouped accounts per linked owner. Two DB roundtrips total
    (owner usernames + flat accounts join). The UI groups the picker
    using this shape; /me/accounts returns the same data flattened with
    owner_id/owner_username denormalised onto every row."""
    c = _require_chatter()
    return await _owners_for(c)


@router.get("/me/accounts", response_model=list[FlatAccountDTO])
async def me_accounts() -> list[FlatAccountDTO]:
    """Flat list — every account the chatter can see, with owner_id and
    owner_username denormalised so the picker can render owner-group
    headers in O(owners + accounts) without a client-side join."""
    c = _require_chatter()
    return await _flat_accounts_for(c)


class FolderAccessDTO(BaseModel):
    folder_id: int
    folder_name: str | None = None


class FolderAccessResp(BaseModel):
    account_id: str
    restricted: bool
    folders: list[FolderAccessDTO]


@router.get("/me/folder-access", response_model=FolderAccessResp)
async def me_folder_access(account_id: str = Query(..., min_length=1)) -> FolderAccessResp:
    """The chatter's AUTHORITATIVE folder menu for one account (DA-3).

    `restricted=False` ⇒ no folder limits for this account; the picker
    shows OF's full folder list as usual. `restricted=True` ⇒ the chatter
    may ONLY see `folders` (names from the denormalised column, not from
    paginated vault/lists). Order is deterministic (folder_name, folder_id)
    so the picker's "first allowed folder" default is stable across loads.

    The chatter must be linked to an owner that granted this account — we
    don't leak grants for accounts outside the chatter's union."""
    c = _require_chatter()
    if account_id not in c.account_ids:
        raise HTTPException(
            status_code=403,
            detail="account not available to you",
        )
    async with get_session() as s:
        rows = (await s.execute(
            select(
                ChatterFolderAccess.folder_id,
                ChatterFolderAccess.folder_name,
            ).where(
                ChatterFolderAccess.chatter_id == c.id,
                ChatterFolderAccess.account_id == account_id,
            )
        )).all()
    folders = sorted(
        (FolderAccessDTO(folder_id=fid, folder_name=fname) for fid, fname in rows),
        key=lambda f: ((f.folder_name or "").lower(), f.folder_id),
    )
    return FolderAccessResp(
        account_id=account_id,
        restricted=bool(folders),
        folders=folders,
    )


# ── Internal helpers ─────────────────────────────────────────────────

def _require_chatter() -> "ChatterIdentity":
    c = get_request_chatter()
    if c is None:
        raise HTTPException(status_code=401, detail="chatter sign-in required")
    return c


async def _me_response(c: "ChatterIdentity | None") -> ChatterMeResp:
    if c is None:
        # _set_session_cookie just fired, so the next request should pick
        # it up — but right now this request didn't pass through the
        # middleware (response is being built mid-handler). Hydrate
        # directly so the caller gets a usable payload right away.
        raise HTTPException(status_code=500, detail="chatter identity unavailable")
    owners = await _owners_for(c)
    return ChatterMeResp(
        chatter_id=c.id,
        username=c.username,
        display_name=c.display_name,
        color=c.color,
        owners=owners,
    )


async def _owners_for(c: "ChatterIdentity") -> list[OwnerDTO]:
    if not c.owner_user_ids:
        return []
    async with get_session() as s:
        user_rows = (await s.execute(
            select(User.id, User.username).where(
                User.id.in_(list(c.owner_user_ids))
            )
        )).all()
        usernames = {r.id: r.username for r in user_rows}

        # accounts per owner — we need nickname + color from accounts table.
        acct_rows = (await s.execute(
            select(
                UserAccount.user_id,
                UserAccount.account_id,
                Account.nickname,
                Account.color,
            ).join(Account, Account.id == UserAccount.account_id, isouter=True)
            .where(UserAccount.user_id.in_(list(c.owner_user_ids)))
        )).all()
        # Chatter Access (step 3): drop accounts this owner has restricted
        # away from the chatter, so the picker only lists allowed models.
        restrictions = await _account_restrictions_for(s, c.id)

    grouped: dict[str, list[OwnerAccountDTO]] = {uid: [] for uid in c.owner_user_ids}
    for r in acct_rows:
        limit = restrictions.get(r.user_id)
        if limit is not None and r.account_id not in limit:
            continue
        grouped[r.user_id].append(OwnerAccountDTO(
            account_id=r.account_id,
            nickname=r.nickname,
            color=r.color,
        ))
    # Sort accounts within each owner; sort owners by username.
    out: list[OwnerDTO] = []
    for uid in sorted(c.owner_user_ids, key=lambda u: usernames.get(u, "").lower()):
        accs = sorted(grouped.get(uid, []), key=lambda a: (a.nickname or a.account_id).lower())
        out.append(OwnerDTO(
            user_id=uid, username=usernames.get(uid, uid), accounts=accs,
        ))
    return out


async def _flat_accounts_for(c: "ChatterIdentity") -> list[FlatAccountDTO]:
    if not c.owner_user_ids:
        return []
    async with get_session() as s:
        rows = (await s.execute(
            select(
                UserAccount.user_id,
                UserAccount.account_id,
                Account.nickname,
                Account.color,
                User.username,
            )
            .join(User, User.id == UserAccount.user_id)
            .join(Account, Account.id == UserAccount.account_id, isouter=True)
            .where(UserAccount.user_id.in_(list(c.owner_user_ids)))
        )).all()
        # Chatter Access (step 3): same per-owner allowlist as the union.
        restrictions = await _account_restrictions_for(s, c.id)
    # De-dupe by account_id. When two linked owners both have the same
    # account in their UserAccount table (cross-grant), the chatter sees
    # it exactly once. The owner_username displayed is the alphabetically
    # first owner's — a deterministic, audit-friendly choice.
    seen: dict[str, FlatAccountDTO] = {}
    for r in rows:
        limit = restrictions.get(r.user_id)
        if limit is not None and r.account_id not in limit:
            continue
        existing = seen.get(r.account_id)
        if existing is None or (r.username or "").lower() < existing.owner_username.lower():
            seen[r.account_id] = FlatAccountDTO(
                account_id=r.account_id,
                nickname=r.nickname,
                color=r.color,
                owner_id=r.user_id,
                owner_username=r.username or r.user_id,
            )
    return sorted(
        seen.values(),
        key=lambda a: (a.owner_username.lower(), (a.nickname or a.account_id).lower()),
    )


# ── Middleware: resolve cookie → ChatterIdentity ─────────────────────

# Owner-only path prefixes under /admin/* that a chatter-only session
# MUST never reach. Anything not on this list falls through to its
# handler, where the per-account ownership gate (`_resolve_account_id`
# / `assert_account_owned` / the account-isolation middleware) enforces
# the chatter's account_ids union. Single place to add new owner-only
# surfaces — keep this curated.
_CHATTER_BLOCKED_ADMIN_PREFIXES: tuple[str, ...] = (
    "/admin/users",        # founder-only user CRUD + grants + transfers
    "/admin/impersonate",  # founder-only
    "/admin/chatters",     # owner-side chatter management (link/unlink/invite)
    "/admin/proxies",      # proxy registry — owner-only
    "/admin/session",      # session bootstrap / capture — owner-only
    "/admin/rev",          # drift detection — owner-only
    "/admin/accounts",     # raw multi-tenant account list — chatter reads /chatter/me/accounts
    "/admin/audit",        # multi-tenant audit log
)


def _chatter_allowed_admin(path: str, method: str) -> bool:
    """Return True if a chatter session may reach this /admin/* path.

    Carved out specifically: GET /admin/employees (read-only roster used
    by the messages filter dropdown). Sub-paths and mutating methods on
    /admin/employees stay owner-only — a chatter shouldn't be able to
    enumerate one employee's audit log or rename someone else's roster.
    """
    if any(path.startswith(p) for p in _CHATTER_BLOCKED_ADMIN_PREFIXES):
        return False
    if path.startswith("/admin/employees"):
        return path == "/admin/employees" and method == "GET"
    return True


async def chatter_session_middleware(request: Request, call_next):
    """Read the chatter cookie, hydrate ChatterIdentity, stash on the
    ContextVar, then call the route. Wipes the cookie on the response if
    the chatter row went stale (deleted, disabled, idle too long).

    Registered AFTER `_auth_session_middleware` in server.py so it runs
    INSIDE auth on the inbound path — by the time we reach the admin
    gate below, `get_request_user()` reflects the User cookie's truth.

    Both cookies may be present (founder testing on the same browser);
    when BOTH are valid the User cookie wins for /admin/* — the chatter
    cookie still hydrates `_request_chatter` so /chatter/me works without
    a logout-in-other-tab dance.

    No ContextVar reset in finally: BaseHTTPMiddleware shares contextvars
    with outer middlewares' post-blocks (verified pattern in auth.py),
    and the request task ends with the response so cleanup is automatic.
    Resetting would hide the chatter from the audit middleware's
    post-block — the whole reason for setting it in the first place.
    """
    cookie = request.cookies.get(COOKIE_NAME)
    expired = False
    if cookie:
        chatter_id = _parse_cookie(cookie)
        if chatter_id:
            async with get_session() as s:
                row = (await s.execute(
                    select(Chatter).where(Chatter.id == chatter_id)
                )).scalar_one_or_none()
                if row is not None and row.is_active:
                    now = datetime.utcnow()
                    age = (now - row.last_seen_at).total_seconds()
                    if age > SESSION_INACTIVITY_SECONDS:
                        expired = True
                    else:
                        if age > 300:
                            await s.execute(update(Chatter).where(
                                Chatter.id == chatter_id,
                            ).values(last_seen_at=now))
            if not expired:
                identity = await _load_identity_from_db(chatter_id)
                if identity is not None:
                    _request_chatter.set(identity)

    # Admin gate: chatter-only sessions are 403'd from owner-only
    # /admin/* surfaces (managed user/proxy/account/etc. mgmt). Per-
    # account /admin/* endpoints (saved-replies, mass-messages, fans,
    # vault, stats) fall through to their handler's account-id gate,
    # which now consults the chatter's account_ids union. User cookie
    # takes precedence — if also set, gate stays open and treats the
    # request as an owner action.
    from auth import get_request_user as _get_user_local
    chatter_active = _request_chatter.get() is not None
    if chatter_active and _get_user_local() is None:
        path = request.url.path
        if path.startswith("/admin/") and not _chatter_allowed_admin(
            path, request.method,
        ):
            return Response(
                "owner-only endpoint — chatter sessions cannot access this admin surface",
                status_code=403,
            )

    response = await call_next(request)
    if expired:
        _clear_session_cookie(response)
    return response


# ── Unified actor view ───────────────────────────────────────────────

class CurrentActor:
    """Whichever principal is driving this request — owner or chatter.

    Owner endpoints assert `kind == "user"` (the chatter principal must
    be 403'd from founder-only / owner-only routes). Audit + account-id
    gating reads `account_ids` regardless of kind so the choke point in
    `_resolve_account_id` stays single-sourced.
    """
    __slots__ = ("kind", "id", "username", "account_ids")

    def __init__(self, kind: str, id_: str, username: str, account_ids: frozenset[str]):
        self.kind = kind
        self.id = id_
        self.username = username
        self.account_ids = account_ids


def current_actor() -> "CurrentActor | None":
    """Single source of truth: who's behind this request, and what can
    they touch. User cookie takes precedence — owner perspective wins
    when both happen to be present, the chatter cookie is informational
    until the user logs out."""
    # Local import so importing this module doesn't pull auth's globals
    # at module load (avoids any circular-init risk when chatters.py is
    # first imported from server.py).
    from auth import get_request_user

    u = get_request_user()
    if u is not None:
        return CurrentActor(
            kind="user", id_=u.id, username=u.username, account_ids=u.account_ids,
        )
    c = _request_chatter.get()
    if c is not None:
        return CurrentActor(
            kind="chatter", id_=c.id, username=c.username, account_ids=c.account_ids,
        )
    return None


def assert_owner() -> "CurrentActor":
    """Use at the top of any owner-only endpoint that must reject chatter
    cookies (transfer model, grant account, founder password gates,
    impersonate, etc.). 401 if no principal, 403 if a chatter is trying
    to act as an owner."""
    actor = current_actor()
    if actor is None:
        raise HTTPException(status_code=401, detail="sign in required")
    if actor.kind != "user":
        raise HTTPException(
            status_code=403,
            detail="owner-only endpoint — chatter sessions cannot perform this action",
        )
    return actor


# ── Owner-side admin router (/admin/chatters/*) ──────────────────────
#
# Linking/unlinking/inviting chatters is an OWNER action: the founder
# password is NOT required because ownership IS the auth (the calling
# user can only mutate THEIR OWN chatter_users rows). This mirrors the
# user-account grant/revoke conventions on the User side.

admin_router = APIRouter(prefix="/admin/chatters", tags=["admin-chatters"])


class _LinkBody(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    display_name: str | None = Field(None, max_length=64)


class AdminChatterRow(BaseModel):
    id: str
    username: str
    display_name: str | None
    color: str | None
    is_active: bool
    employee_id_for_this_owner: int | None
    created_at: datetime
    last_seen_at: datetime


class AdminInviteResp(BaseModel):
    token: str
    expires_at: datetime
    # Convenience: the URL the owner shares with the new chatter.
    # Built relative; the owner copies a full URL from the UI side.
    accept_path: str


@admin_router.get("", response_model=list[AdminChatterRow])
async def list_chatters() -> list[AdminChatterRow]:
    """Every chatter linked to the current owner. The
    `employee_id_for_this_owner` is the auto-created mirror Employee
    row (NULL if none yet — first audited action under this owner will
    materialise it via the chatter middleware path)."""
    actor = assert_owner()
    from db.models import Employee
    async with get_session() as s:
        rows = (await s.execute(
            select(Chatter, Employee.id.label("emp_id"))
            .join(ChatterUser, ChatterUser.chatter_id == Chatter.id)
            .join(
                Employee,
                (Employee.chatter_id == Chatter.id) & (Employee.user_id == actor.id),
                isouter=True,
            )
            .where(ChatterUser.user_id == actor.id)
            .order_by(Chatter.username)
        )).all()
    return [
        AdminChatterRow(
            id=r.Chatter.id,
            username=r.Chatter.username,
            display_name=r.Chatter.display_name,
            color=r.Chatter.color,
            is_active=r.Chatter.is_active,
            employee_id_for_this_owner=r.emp_id,
            created_at=r.Chatter.created_at,
            last_seen_at=r.Chatter.last_seen_at,
        )
        for r in rows
    ]


@admin_router.post("/link")
async def link_chatter(body: _LinkBody = Body(...)) -> dict:
    """Link an existing chatter to the current owner by username.
    Idempotent — second call with the same username returns 200 with
    `already_linked: true`. We never create the Chatter row here —
    chatters self-register via an invite token (POST /admin/chatters/invite)
    so an owner can't squat on someone else's username."""
    actor = assert_owner()
    target_username = _normalize_username(body.username)
    if not target_username:
        raise HTTPException(status_code=400, detail="username required")

    async with get_session() as s:
        chatter = (await s.execute(
            select(Chatter).where(Chatter.username == target_username)
        )).scalar_one_or_none()
        if chatter is None:
            raise HTTPException(
                status_code=404,
                detail=f"no chatter named {target_username!r} — send them an invite link instead",
            )
        # Update display_name if the owner provided one and the chatter
        # has no preferred display_name set. Owner can override their own
        # view via the Employee mirror row's display_name; this only fills
        # the global-default slot.
        if body.display_name and not chatter.display_name:
            chatter.display_name = body.display_name.strip()

        try:
            s.add(ChatterUser(
                chatter_id=chatter.id, user_id=actor.id, created_at=datetime.utcnow(),
            ))
            await s.flush()
            already_linked = False
        except IntegrityError:
            await s.rollback()
            already_linked = True

    return {
        "ok": True,
        "chatter_id": chatter.id,
        "username": chatter.username,
        "already_linked": already_linked,
    }


@admin_router.delete("/{chatter_id}/link")
async def unlink_chatter(chatter_id: str) -> dict:
    """Remove the chatter_users row tying this chatter to the current
    owner. Other owners' links survive.

    The mirror Employee row (if any) is left untouched — chatter_id stays
    set so the row continues to be recognised as a chatter mirror by the
    "Who's chatting?" picker filter (`!e.chatter_id`) and the Employees
    settings table. Previously we nulled chatter_id here to "downgrade
    to label-only", which made unlinked mirrors appear in the owner's
    user picker — owners then accidentally picked them and impersonated
    the (no-longer-linked) chatter. Keeping chatter_id set also lets
    re-linking the same chatter reuse the existing mirror via
    resolve_chatter_employee_id, preserving audit history continuity."""
    actor = assert_owner()
    async with get_session() as s:
        result = await s.execute(
            delete(ChatterUser).where(
                ChatterUser.chatter_id == chatter_id,
                ChatterUser.user_id == actor.id,
            )
        )
        if (result.rowcount or 0) == 0:
            raise HTTPException(
                status_code=404,
                detail=f"chatter {chatter_id!r} is not linked to you",
            )
    return {"ok": True, "chatter_id": chatter_id}


@admin_router.post("/invite", response_model=AdminInviteResp)
async def invite_chatter() -> AdminInviteResp:
    """Mint a single-use signup token good for 24h. The owner copies the
    `<dashboard>/chatter-login?token=…` URL and shares it with the new
    chatter; first login through that URL auto-links them to this owner."""
    actor = assert_owner()
    token = secrets.token_urlsafe(24)
    now = datetime.utcnow()
    expires = now + timedelta(seconds=INVITE_TTL_SECONDS)
    async with get_session() as s:
        s.add(ChatterInvite(
            token=token,
            user_id=actor.id,
            created_at=now,
            expires_at=expires,
        ))
        await s.flush()
    return AdminInviteResp(
        token=token,
        expires_at=expires,
        accept_path=f"/chatter-login?token={token}",
    )


# ── Chatter Access: owner-set visibility limits (step 5) ─────────────
#
# All three endpoints are OWNER-only and strictly scoped to the calling
# owner's OWN accounts — owner A can neither read nor write owner B's
# grants for a shared chatter. Both allowlists are SUBTRACTIVE; an empty
# set = full access for that owner (DA-2: "see nothing" = unlink the
# chatter, never an empty restricted set).

class FolderGrant(BaseModel):
    folder_id: int
    # Denormalised at write time (DA-3) so the chatter's authoritative
    # folder menu can render names without re-paginating OF's vault/lists.
    folder_name: str | None = None


class AccessResp(BaseModel):
    # This owner's account allowlist for the chatter (empty = all of this
    # owner's accounts).
    account_ids: list[str]
    # {account_id: [folder_id, ...]} — this owner's folder allowlists,
    # only for accounts the owner owns. Empty list for an account = all
    # folders.
    folder_access: dict[str, list[int]]


class _AccountAccessBody(BaseModel):
    account_ids: list[str] = Field(default_factory=list)


class _FolderAccessBody(BaseModel):
    account_id: str = Field(..., min_length=1)
    folders: list[FolderGrant] = Field(default_factory=list)


def _assert_owns(actor: "CurrentActor", account_id: str) -> None:
    if account_id not in actor.account_ids:
        raise HTTPException(
            status_code=403,
            detail=f"account {account_id!r} is not one of yours",
        )


async def _assert_linked(s, chatter_id: str, owner_id: str) -> None:
    linked = (await s.execute(
        select(ChatterUser.chatter_id).where(
            ChatterUser.chatter_id == chatter_id,
            ChatterUser.user_id == owner_id,
        )
    )).first()
    if linked is None:
        raise HTTPException(
            status_code=404,
            detail=f"chatter {chatter_id!r} is not linked to you",
        )


@admin_router.get("/{chatter_id}/access", response_model=AccessResp)
async def get_chatter_access(chatter_id: str) -> AccessResp:
    """This owner's visibility grants for one linked chatter — the account
    allowlist (empty = all of this owner's models) plus the per-account
    folder allowlists (only for accounts this owner owns)."""
    actor = assert_owner()
    async with get_session() as s:
        await _assert_linked(s, chatter_id, actor.id)
        acct_rows = (await s.execute(
            select(ChatterAccountAccess.account_id).where(
                ChatterAccountAccess.chatter_id == chatter_id,
                ChatterAccountAccess.user_id == actor.id,
            )
        )).all()
        # Folder grants are keyed (chatter, account); scope to accounts this
        # owner actually owns so cross-owner folder grants never leak.
        fa_rows = []
        if actor.account_ids:
            fa_rows = (await s.execute(
                select(
                    ChatterFolderAccess.account_id,
                    ChatterFolderAccess.folder_id,
                ).where(
                    ChatterFolderAccess.chatter_id == chatter_id,
                    ChatterFolderAccess.account_id.in_(list(actor.account_ids)),
                )
            )).all()
    folder_access: dict[str, list[int]] = {}
    for aid, fid in fa_rows:
        folder_access.setdefault(aid, []).append(fid)
    # Scope the account allowlist to accounts this owner STILL owns. A model
    # transfer (or a revoked grant) can leave orphaned chatter_account_access
    # rows for an account the owner no longer holds; surfacing one seeds the
    # editor's "Limit models" set, and "Save access" then re-sends that now-
    # foreign id → 403 "account X is not one of yours". Mirrors the
    # folder_access scoping above.
    return AccessResp(
        account_ids=[r[0] for r in acct_rows if r[0] in actor.account_ids],
        folder_access=folder_access,
    )


@admin_router.put("/{chatter_id}/account-access")
async def put_chatter_account_access(
    chatter_id: str, body: _AccountAccessBody = Body(...),
) -> dict:
    """Replace THIS owner's account allowlist for the chatter atomically.
    Empty list = remove the restriction (chatter sees all of this owner's
    models again). Rejects any account id the owner doesn't own."""
    actor = assert_owner()
    # Validate ownership of every supplied id before touching the DB.
    for aid in body.account_ids:
        _assert_owns(actor, aid)
    deduped = sorted(set(body.account_ids))
    async with get_session() as s:
        await _assert_linked(s, chatter_id, actor.id)
        await s.execute(
            delete(ChatterAccountAccess).where(
                ChatterAccountAccess.chatter_id == chatter_id,
                ChatterAccountAccess.user_id == actor.id,
            )
        )
        for aid in deduped:
            s.add(ChatterAccountAccess(
                chatter_id=chatter_id,
                user_id=actor.id,
                account_id=aid,
                created_at=datetime.utcnow(),
            ))
    return {"ok": True, "chatter_id": chatter_id, "account_ids": deduped}


@admin_router.put("/{chatter_id}/folder-access")
async def put_chatter_folder_access(
    chatter_id: str, body: _FolderAccessBody = Body(...),
) -> dict:
    """Replace the folder allowlist for (chatter, account) atomically.
    Empty list = remove the restriction (chatter sees all folders for that
    account). The owner must own the account. folder_name is denormalised
    at write time (DA-3)."""
    actor = assert_owner()
    _assert_owns(actor, body.account_id)
    # De-dupe by folder_id, keeping the last-supplied name.
    by_id: dict[int, str | None] = {}
    for g in body.folders:
        by_id[g.folder_id] = g.folder_name
    async with get_session() as s:
        await _assert_linked(s, chatter_id, actor.id)
        await s.execute(
            delete(ChatterFolderAccess).where(
                ChatterFolderAccess.chatter_id == chatter_id,
                ChatterFolderAccess.account_id == body.account_id,
            )
        )
        for fid, fname in by_id.items():
            s.add(ChatterFolderAccess(
                chatter_id=chatter_id,
                account_id=body.account_id,
                folder_id=fid,
                folder_name=fname,
                created_at=datetime.utcnow(),
            ))
    return {
        "ok": True,
        "chatter_id": chatter_id,
        "account_id": body.account_id,
        "folder_ids": sorted(by_id.keys()),
    }
