"""
Friend auth — username/password registration + login + 30-day sliding session.

See plan/simple_username_auth_2026_05_24/PLAN.md.

Two tables (users, user_accounts) + three endpoints (/auth/register,
/auth/login, /auth/logout) + a session middleware that populates a
`_request_user` ContextVar consulted by `_resolve_account_id` in server.py
to gate every account-scoped endpoint.

Passwords are stored plaintext per user decision (friends-only scale; the
relay sits behind the share-token gate). When the DB file ever moves to
a wider surface, switch to argon2/bcrypt before exposing.

Session cookie payload: f"{user_id}.{issued_at_epoch}.{hmac_sig}". The
hmac is keyed by os.environ["SESSION_SECRET"] (auto-generated and
persisted on first boot if missing, so dev doesn't have to think about
it). 30-day inactivity expiry — every authed request bumps
`users.last_seen_at`; once the gap exceeds 30 days the cookie is
rejected and the user has to log in again.
"""

from __future__ import annotations

import hmac
import logging
import os

import secrets_store
from dotenv import dotenv_values
import re
import secrets
import uuid
from contextvars import ContextVar
from datetime import datetime
from hashlib import sha256
from pathlib import Path

log = logging.getLogger("of-relay.auth")

from fastapi import APIRouter, Body, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError

from db.engine import get_session
from db.models import (
    ChatterAccountAccess,
    ChatterFolderAccess,
    ChatterUser,
    User,
    UserAccount,
)


# ── Constants ─────────────────────────────────────────────────────────

# Friends-only DB exposure; risk accepted in the plan. Bump to a real KDF
# before any wider deployment.
COOKIE_NAME = "chatterly_session"
COOKIE_MAX_AGE_SECONDS = 30 * 24 * 3600  # browser-side hint; server is the source of truth
SESSION_INACTIVITY_SECONDS = 30 * 24 * 3600

# Founder impersonation. Short-lived so a forgotten "view as" session
# can't quietly persist across the founder's coffee break. 1h is plenty
# for a support call; re-entering admin_password is cheap.
IMPERSONATE_COOKIE_NAME = "chatterly_impersonate"
IMPERSONATE_TTL_SECONDS = 60 * 60

USERNAME_RE = re.compile(r"^[a-z0-9_.-]{3,32}$")
RESERVED_USERNAMES = frozenset({"admin", "root", "system", "chatterly", "founder"})


# Persisted to service/.session_secret on first boot if SESSION_SECRET is
# unset, so dev sessions survive restarts without env-var ceremony. In
# prod, set SESSION_SECRET explicitly (env var beats the file).
_SECRET_FILE = Path(__file__).resolve().parent / ".session_secret"


def _load_session_secret() -> bytes:
    env = os.environ.get("SESSION_SECRET", "").strip()
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
        # Read-only FS (some container setups) — fall back to in-process
        # only. Sessions don't survive restart but auth still works.
        pass
    return new.encode("utf-8")


_SESSION_SECRET = _load_session_secret()

# Set by the session middleware on every request that carries a valid
# cookie. `None` for unauthed requests (landing page hits, /auth/*,
# background tasks). Cached read of (id, username, account_ids) so
# `_resolve_account_id` doesn't query per call.
_request_user: ContextVar["AuthedUser | None"] = ContextVar(
    "chatterly_request_user", default=None
)


class AuthedUser:
    """In-process snapshot of the signed-in user, valid for one request.

    Stored on `_request_user` by the session middleware. Holds the user's
    own row plus the set of OF account_ids they're allowed to touch.
    Snapshot once at middleware time so deep callers don't pay a query
    per `_resolve_account_id` call.
    """

    __slots__ = ("id", "username", "account_ids", "is_admin")

    def __init__(
        self, id_: str, username: str, account_ids: frozenset[str],
        is_admin: bool = False,
    ):
        self.id = id_
        self.username = username
        # For a master (is_admin) this is the fat set — the registry UNIONED
        # with every account owned in `user_accounts` (each source holds
        # accounts the other lacks; see _master_account_ids) — so every
        # per-account gate that tests membership passes. The employee axis is
        # a separate column — gated explicitly via `is_admin`.
        self.account_ids = account_ids
        self.is_admin = is_admin


def get_request_user() -> AuthedUser | None:
    """Read the current request's signed-in user (or None)."""
    return _request_user.get()


# ── Impersonation overlay ─────────────────────────────────────────────
#
# When a founder POSTs to /admin/impersonate with admin_password, we set a
# second cookie. The session middleware uses it to OVERLAY `_request_user`
# with the impersonated user for the request — every downstream gate
# (`assert_account_owned`, `clamp_account_filter`, the account-isolation
# middleware) then naturally treats the founder as that user. The original
# founder identity is kept in `_impersonation` so the UI banner can render
# "you are viewing as @X (real: @Y) — Exit" and so mutating requests can
# be 403'd (impersonation is read-only — see middleware).

class Impersonation:
    """Original founder identity, kept alongside `_request_user` whenever a
    valid impersonate cookie applies. The middleware sets this; readers
    consult `get_impersonation()`."""
    __slots__ = ("real_user_id", "real_username",
                 "impersonated_user_id", "impersonated_username")

    def __init__(
        self,
        real_user_id: str, real_username: str,
        impersonated_user_id: str, impersonated_username: str,
    ):
        self.real_user_id = real_user_id
        self.real_username = real_username
        self.impersonated_user_id = impersonated_user_id
        self.impersonated_username = impersonated_username


_impersonation: ContextVar["Impersonation | None"] = ContextVar(
    "chatterly_impersonation", default=None,
)


def get_impersonation() -> "Impersonation | None":
    return _impersonation.get()


def _actor_account_ids() -> frozenset[str] | None:
    """Return the account_ids set for the current request's active
    principal, or None if no one is signed in.

    User cookie wins when both are present (owner perspective trumps a
    chatter cookie that happens to be in the same browser). The chatter
    branch is reached via a lazy import so this module stays free of a
    hard `chatters` dependency at boot — `chatters.py` imports `auth.py`
    at module load, so the reverse must defer.
    """
    user = _request_user.get()
    if user is not None:
        return user.account_ids
    try:
        from chatters import get_request_chatter
    except ImportError:
        return None
    chatter = get_request_chatter()
    if chatter is not None:
        return chatter.account_ids
    return None


async def assert_employee_filter_owned(employee_id: int | None) -> None:
    """Raise 403 if the active principal isn't authorized to filter by
    this `employee_id` on a per-employee revenue / message / tip query.

    Why: per-account data filters (`account_id`) catch cross-account
    leaks, but `employee_id` is a SECOND axis. Without this gate, a
    chatter with access to account_X could ask `?employee_id=<any id>`
    and read every message/tip attributed to that random employee on
    account_X — including co-chatter mirror Employees or even other
    owners' employees if they ever wrote on account_X.

    Rules:
      • Owner — the Employee row's `user_id` must equal current user.id.
        Filtering by another owner's employee is a cross-agency leak.
      • Chatter — the Employee row's `user_id` must be one of the
        chatter's linked owners (so chatters can slice team stats
        within any owner they work for), OR the row's `chatter_id`
        must equal the current chatter.id (their own mirror, even if
        unowned). Filtering by an employee under an owner the chatter
        ISN'T linked to is still a cross-agency leak.
      • Unauthed — no-op (internal scripts / cron carry no cookie).

    No-op when `employee_id is None`. Async because we have to hit the
    DB to look up the row.
    """
    if employee_id is None:
        return
    # Local import — db.engine imports are heavy; defer to call time
    # so this helper stays cheap on the hot path when no filter is set.
    from db.engine import get_session
    from db.models import Employee
    async with get_session() as s:
        row = await s.get(Employee, employee_id)
    if row is None:
        # Unknown employee: 404 would also work, but 403 is honest about
        # "you're asking about something you can't have" without leaking
        # whether the row exists. Match the rest of this file's posture.
        raise HTTPException(status_code=403, detail="not your employee")
    user = _request_user.get()
    if user is not None:
        if user.is_admin:
            return  # master may slice any employee's revenue/messages
        if row.user_id != user.id:
            raise HTTPException(status_code=403, detail="not your employee")
        return
    try:
        from chatters import get_request_chatter
    except ImportError:
        return
    chatter = get_request_chatter()
    if chatter is not None:
        # Allow team-transparency: any Employee owned by one of the
        # chatter's linked owners is fair game (a chatter linked to
        # Alice + Bob can slice by Alice's own roster employees, by
        # Bob's, by their own mirrors under either, or by co-chatters'
        # mirrors under either). Cross-agency rows — Employee.user_id
        # belongs to an owner the chatter isn't linked to — still 403.
        if row.user_id in chatter.owner_user_ids:
            return
        if row.chatter_id == chatter.id:
            return
        raise HTTPException(status_code=403, detail="not your employee")


def allow_anonymous_account_access() -> bool:
    """The escape hatch for the fail-closed gate below. Default OFF.

    Set ALLOW_ANONYMOUS_ADMIN=1 in docker-compose.override.yml to restore the
    old abstain-on-anonymous behaviour without a code change — there purely so
    a lockout is a one-line revert plus a restart, never a redeploy.
    Read per call, not captured at import, so a test can flip it.
    """
    return os.environ.get("ALLOW_ANONYMOUS_ADMIN", "0").strip().lower() \
        in ("1", "true", "yes", "on")


def assert_account_owned(account_id: str | None) -> None:
    """Raise 403 if the current principal doesn't own this account_id, and 401
    if there is no principal at all.

    ⚠️ THIS USED TO NO-OP FOR ANONYMOUS CALLERS, and that was the bug, not a
    missing check. The rationale on the old code was "system/cron paths,
    internal curl flows" — an audit on 2026-08-10 found NO runtime caller that
    needed it. The one internal caller that touches /admin (`scripts/diag.sh`)
    carries the share token, which is the PERIMETER, not this gate. What the
    abstention actually did was constrain only the people who had identified
    themselves: @alice was stopped from reading @bob's account, while an
    anonymous request sailed through all ~176 call sites of this function.
    Verified live the same day: GET /admin/accounts returned 200 with no
    cookie, and /admin/vault-ai/image served real creator media.

    A gate that abstains when it cannot identify the caller is not a gate.
    `clamp_account_filter` is still the right tool for endpoints where
    account_id is genuinely optional.

    Both User and Chatter principals participate: a chatter linked to
    owners @alice and @bob sees the SET UNION of both account_ids sets
    and clears this gate for any account either owner holds.
    """
    if not account_id:
        return
    ids = _actor_account_ids()
    if ids is None:
        if allow_anonymous_account_access():
            return
        raise HTTPException(status_code=401, detail="sign in required")
    if account_id not in ids:
        raise HTTPException(status_code=403, detail="not your account")


def clamp_account_filter(account_id: str | None) -> list[str] | None:
    """For endpoints with OPTIONAL `account_id` (stats, transactions,
    paid-messages, etc.) that would otherwise fall through to a global
    "all accounts" query — clamp the filter to what the active principal
    is allowed to see. The principal may be a User OR a Chatter (chatter
    sees the SET UNION across linked owners).

    Returns:
      • `[account_id]` if account_id is given and the principal owns it
        (or the request is unauthed).
      • `list(principal.account_ids)` if account_id is None and someone
        is signed in.
      • `None` if the request is unauthed and no account_id was given
        (back-compat: no clamp, query stays global).
    """
    ids = _actor_account_ids()
    if account_id:
        if ids is not None and account_id not in ids:
            raise HTTPException(status_code=403, detail="not your account")
        return [account_id]
    if ids is None:
        return None
    return list(ids)


# ── Cookie signing ────────────────────────────────────────────────────

def _sign(payload: str) -> str:
    return hmac.new(_SESSION_SECRET, payload.encode("utf-8"), sha256).hexdigest()


def _make_cookie(user_id: str) -> str:
    issued = str(int(datetime.utcnow().timestamp()))
    payload = f"{user_id}.{issued}"
    return f"{payload}.{_sign(payload)}"


def _parse_cookie(value: str) -> str | None:
    """Return the user_id if the cookie's HMAC verifies, else None.

    Does NOT check inactivity expiry — that needs a DB read and happens
    in the middleware. Pure crypto check here.
    """
    parts = value.split(".")
    if len(parts) != 3:
        return None
    user_id, issued, sig = parts
    expected = _sign(f"{user_id}.{issued}")
    if not hmac.compare_digest(sig, expected):
        return None
    return user_id


def _make_impersonate_cookie(impersonated_user_id: str, founder_user_id: str) -> str:
    """Format: `{impersonated_user_id}.{founder_user_id}.{issued_epoch}.{sig}`.

    Binding to `founder_user_id` means a leaked impersonate cookie alone
    is useless — the session middleware requires the active session cookie
    to belong to the same founder. Logging in as a different user
    effectively invalidates the impersonation.
    """
    issued = str(int(datetime.utcnow().timestamp()))
    payload = f"{impersonated_user_id}.{founder_user_id}.{issued}"
    return f"{payload}.{_sign(payload)}"


def _parse_impersonate_cookie(value: str) -> tuple[str, str, int] | None:
    """Returns `(impersonated_user_id, founder_user_id, issued_epoch)` or
    None on any malformation / signature mismatch."""
    parts = value.split(".")
    if len(parts) != 4:
        return None
    imp, founder, issued, sig = parts
    expected = _sign(f"{imp}.{founder}.{issued}")
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        issued_int = int(issued)
    except ValueError:
        return None
    return imp, founder, issued_int


# ── Username/password helpers ─────────────────────────────────────────

def _normalize_username(raw: str) -> str:
    return (raw or "").strip().lower()


def _validate_username(username: str) -> None:
    if not USERNAME_RE.fullmatch(username):
        raise HTTPException(
            status_code=400,
            detail="username must be 3-32 chars, lowercase a-z 0-9 . _ -",
        )
    if username in RESERVED_USERNAMES:
        raise HTTPException(status_code=400, detail=f"username {username!r} is reserved")


def _validate_password(password: str) -> None:
    if not password or len(password) < 6:
        raise HTTPException(status_code=400, detail="password must be at least 6 chars")
    if len(password) > 256:
        raise HTTPException(status_code=400, detail="password too long")


def _set_session_cookie(response: Response, user_id: str) -> None:
    response.set_cookie(
        COOKIE_NAME,
        _make_cookie(user_id),
        max_age=COOKIE_MAX_AGE_SECONDS,
        httponly=True,
        samesite="lax",
        # Secure is appropriate in prod (HTTPS via tunnel) but breaks
        # localhost dev. Honor an env opt-in.
        secure=os.environ.get("SESSION_COOKIE_SECURE", "").lower() in {"1", "true", "yes"},
    )


def _clear_session_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, samesite="lax")


def _set_impersonate_cookie(
    response: Response, impersonated_user_id: str, founder_user_id: str,
) -> None:
    response.set_cookie(
        IMPERSONATE_COOKIE_NAME,
        _make_impersonate_cookie(impersonated_user_id, founder_user_id),
        max_age=IMPERSONATE_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        secure=os.environ.get("SESSION_COOKIE_SECURE", "").lower() in {"1", "true", "yes"},
    )


def _clear_impersonate_cookie(response: Response) -> None:
    response.delete_cookie(IMPERSONATE_COOKIE_NAME, samesite="lax")


def clear_all_principal_cookies(response: Response) -> None:
    """Defensively clear every principal-bearing cookie this app sets.

    Used on logout (both /auth/logout and /chatter/logout) so a stale
    cross-principal cookie can't survive an identity switch. The chatter
    cookie name is imported lazily to keep the auth → chatters boot order
    one-directional (chatters.py imports auth.py at module load)."""
    response.delete_cookie(COOKIE_NAME, samesite="lax")
    response.delete_cookie(IMPERSONATE_COOKIE_NAME, samesite="lax")
    try:
        from chatters import COOKIE_NAME as _CHATTER_COOKIE_NAME
        response.delete_cookie(_CHATTER_COOKIE_NAME, samesite="lax")
    except ImportError:
        pass


# ── Public routes ─────────────────────────────────────────────────────

router = APIRouter(prefix="/auth", tags=["auth"])


class RegisterBody(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=256)


class LoginBody(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=256)


class ImpersonatingResp(BaseModel):
    real_user_id: str
    real_username: str


class AuthResp(BaseModel):
    user_id: str
    username: str
    # Present only when this request is running under an active impersonate
    # cookie. UI uses it to render the "Viewing as @X (real: @Y)" banner
    # and to wipe RQ caches across the transition.
    impersonating: ImpersonatingResp | None = None
    # Master role. The UI needs it to HIDE founder-only surfaces (the server's
    # house-key panel) rather than render them and let the 403 arrive as a
    # broken card. Not a gate in itself — every founder-only route checks the
    # role server-side; this only decides what is worth drawing.
    #
    # While impersonating this is the IMPERSONATED user's role, matching the
    # rest of the app's view. A founder wearing an agency's identity therefore
    # stops seeing the house-key panel, which is right: impersonation is
    # read-only anyway, and the panel is not that agency's business.
    is_admin: bool = False


@router.post("/register", response_model=AuthResp)
async def register(response: Response, body: RegisterBody = Body(...)) -> AuthResp:
    username = _normalize_username(body.username)
    _validate_username(username)
    _validate_password(body.password)

    new_id = uuid.uuid4().hex
    async with get_session() as s:
        s.add(User(
            id=new_id,
            username=username,
            password=body.password,  # plaintext per user decision
        ))
        try:
            await s.flush()
        except IntegrityError:
            raise HTTPException(status_code=409, detail="username already taken")

    _set_session_cookie(response, new_id)
    return AuthResp(user_id=new_id, username=username)


@router.post("/login", response_model=AuthResp)
async def login(response: Response, body: LoginBody = Body(...)) -> AuthResp:
    username = _normalize_username(body.username)
    if not username:
        raise HTTPException(status_code=400, detail="username required")

    async with get_session() as s:
        row = (await s.execute(
            select(User).where(User.username == username)
        )).scalar_one_or_none()
        if row is None or row.password != body.password:
            # Same response for both so we don't leak which is wrong.
            raise HTTPException(status_code=401, detail="invalid username or password")
        # Bump last_seen_at so the 30-day inactivity window restarts.
        await s.execute(
            update(User).where(User.id == row.id).values(last_seen_at=datetime.utcnow())
        )
        uid = row.id
        uname = row.username
        is_admin = bool(row.is_admin)

    _set_session_cookie(response, uid)
    return AuthResp(user_id=uid, username=uname, is_admin=is_admin)


@router.post("/logout")
async def logout(response: Response) -> dict[str, bool]:
    # Wipe every principal cookie (user + impersonate + chatter) so a
    # surviving cross-principal cookie can't re-authenticate the next
    # request after the user explicitly signed out.
    clear_all_principal_cookies(response)
    return {"ok": True}


@router.get("/me", response_model=AuthResp | None)
async def me() -> AuthResp | None:
    """Who is signed in on this request (or None). Used by the Next app
    shell to decide between rendering the app or redirecting to /login.

    When impersonating, `user_id`/`username` reflect the IMPERSONATED user
    (matches the rest of the app's view) and `impersonating` carries the
    founder's real identity for the banner."""
    u = get_request_user()
    if u is None:
        return None
    imp = get_impersonation()
    return AuthResp(
        user_id=u.id,
        username=u.username,
        is_admin=bool(u.is_admin),
        impersonating=ImpersonatingResp(
            real_user_id=imp.real_user_id,
            real_username=imp.real_username,
        ) if imp is not None else None,
    )


# ── Middleware: resolve cookie → AuthedUser ───────────────────────────

async def _master_account_ids(s) -> frozenset[str]:
    """The master (is_admin) fat set: the registry UNIONED with every account
    any user owns in `user_accounts`. Each source holds accounts the other
    lacks — the registry knows captured sessions the DB may not, the DB knows
    retired DB-only accounts with no captured session; replacing the union
    with the registry alone made the master WEAKER than a regular owner (a
    rules DELETE 403'd the live master on 2026-08-19). Recomputed each
    request, so a freshly captured account appears without re-login.

    Deferred registry import mirrors the `chatters` pattern — auth.py keeps no
    hard registry dependency. `account_ids()` and not `list_accounts()`: we
    want the id SET, and the listing builds it by reading a meta.json per
    account — a directory walk on every request the master makes, which is
    descriptors spent in the middleware on data we discard."""
    import accounts as _registry
    rows = (await s.execute(select(UserAccount.account_id).distinct())).all()
    return _registry.account_ids() | frozenset(str(r[0]) for r in rows)


async def session_middleware(request: Request, call_next):
    """Read the session cookie, load the user + their account_ids, stash
    on the ContextVar, then call the route. Idle-too-long users get their
    cookie cleared on the response.

    Runs AFTER the share-token gate (registered earlier so it's outer in
    Starlette's reverse-order middleware stack) so only share-authed
    requests reach this layer. /auth/* endpoints are exempt from the
    share-token gate via the existing allowlist below.
    """
    cookie = request.cookies.get(COOKIE_NAME)
    expired = False
    clear_impersonate = False
    if cookie:
        user_id = _parse_cookie(cookie)
        if user_id:
            async with get_session() as s:
                row = (await s.execute(
                    select(User).where(User.id == user_id)
                )).scalar_one_or_none()
                if row is not None:
                    now = datetime.utcnow()
                    age = (now - row.last_seen_at).total_seconds()
                    if age > SESSION_INACTIVITY_SECONDS:
                        expired = True
                    else:
                        is_admin = bool(getattr(row, "is_admin", False))
                        if is_admin:
                            # Master role: the FULL fat set — the same source
                            # the isolation middleware and _resolve_account_id
                            # check against. Why a union and why per-request:
                            # see _master_account_ids.
                            account_ids = await _master_account_ids(s)
                        else:
                            acct_rows = (await s.execute(
                                select(UserAccount.account_id).where(
                                    UserAccount.user_id == user_id
                                )
                            )).all()
                            account_ids = frozenset(str(r[0]) for r in acct_rows)
                        # Only bump last_seen_at once per ~5 min to avoid
                        # write amplification — a chat session fires
                        # hundreds of requests/min.
                        if age > 300:
                            # A cosmetic activity stamp must NEVER be able to
                            # fail the request it rides on. Two things here:
                            #
                            # Its OWN session, because a lock timeout would
                            # otherwise poison `s` — SQLAlchemy demands a
                            # rollback after any error, and the impersonation
                            # overlay below still has reads to do on it.
                            #
                            # And SWALLOWED, because a missed bump costs
                            # nothing (the next request retries) while a raised
                            # one costs the whole call. Unguarded, this UPDATE
                            # sat on the 30 s busy_timeout whenever the DB was
                            # write-contended and surfaced as a 500 on real API
                            # calls — /admin/messages, /api/of/v2/chats — which
                            # is how a write-storm turned into a dead-looking
                            # UI on 2026-08-21. Note the throttle cannot
                            # re-arm on its own while the write keeps failing:
                            # `age` is read from the row, so a never-committed
                            # stamp stays stale and EVERY subsequent request
                            # retries the write. Failing quietly is what breaks
                            # that loop.
                            try:
                                async with get_session() as s2:
                                    await s2.execute(update(User).where(
                                        User.id == user_id
                                    ).values(last_seen_at=now))
                            except Exception:
                                log.debug("last_seen_at bump skipped "
                                          "(non-fatal)", exc_info=True)
                        _request_user.set(
                            AuthedUser(row.id, row.username, account_ids, is_admin)
                        )

                        # Impersonation overlay. Resolved only when the
                        # founder is signed in (the impersonate cookie
                        # binds to a specific founder user_id). Any
                        # parse/load failure clears the bad cookie on the
                        # response so the next request is clean.
                        imp_cookie = request.cookies.get(IMPERSONATE_COOKIE_NAME)
                        if imp_cookie:
                            parsed = _parse_impersonate_cookie(imp_cookie)
                            if parsed is None:
                                clear_impersonate = True
                            else:
                                imp_uid, founder_uid, issued = parsed
                                age_imp = int(now.timestamp()) - issued
                                if age_imp > IMPERSONATE_TTL_SECONDS or founder_uid != row.id:
                                    clear_impersonate = True
                                else:
                                    imp_row = (await s.execute(
                                        select(User).where(User.id == imp_uid)
                                    )).scalar_one_or_none()
                                    if imp_row is None:
                                        clear_impersonate = True
                                    else:
                                        imp_is_admin = bool(getattr(imp_row, "is_admin", False))
                                        if imp_is_admin:
                                            # Impersonating a master = the same
                                            # fat set as a real master (read-
                                            # only — mutation is blocked below
                                            # for any impersonation).
                                            imp_account_ids = await _master_account_ids(s)
                                        else:
                                            imp_acct_rows = (await s.execute(
                                                select(UserAccount.account_id).where(
                                                    UserAccount.user_id == imp_uid
                                                )
                                            )).all()
                                            imp_account_ids = frozenset(
                                                str(r[0]) for r in imp_acct_rows)
                                        _request_user.set(AuthedUser(
                                            imp_row.id, imp_row.username,
                                            imp_account_ids, imp_is_admin,
                                        ))
                                        _impersonation.set(Impersonation(
                                            real_user_id=row.id,
                                            real_username=row.username,
                                            impersonated_user_id=imp_row.id,
                                            impersonated_username=imp_row.username,
                                        ))

    # Impersonation is READ-ONLY. Any mutation while a valid impersonate
    # cookie is in effect is rejected, except the exit endpoint (DELETE
    # /admin/impersonate) and logout (which clears the founder session
    # entirely, implicitly killing the overlay too). Without this, the
    # founder could silently send a message AS the friend and the audit
    # log would record it under the friend's identity.
    if _impersonation.get() is not None:
        method = request.method
        path = request.url.path
        if method in ("POST", "PATCH", "PUT", "DELETE") and not (
            path.startswith("/admin/impersonate") or path.startswith("/auth/")
        ):
            return Response(
                "read-only while impersonating — exit impersonation to mutate",
                status_code=403,
            )

    response = await call_next(request)
    if expired:
        _clear_session_cookie(response)
    if clear_impersonate:
        _clear_impersonate_cookie(response)
    return response


# ── Admin user management ─────────────────────────────────────────────
#
# Founder-only: list users + delete one with a typed "delete" + admin
# password confirmation. Gated by ADMIN_PASSWORD env var (NOT in source).
# Sits under /admin/* so the existing share-token + (eventually)
# session-cookie gating both apply.

admin_router = APIRouter(prefix="/admin/users", tags=["admin-users"])

ADMIN_PASSWORD_ENV = "ADMIN_PASSWORD"
_ENV_FILE = Path(__file__).resolve().parent / ".env"


def _read_admin_password() -> str:
    """The founder second factor: the process ENV first, the UI store as the
    bootstrap fallback.

    This is the one key whose precedence is inverted, and the inversion is the
    whole rotation story. `secrets_store.get` is store-first, which is right for
    a key the UI owns — but ADMIN_PASSWORD is also BOOTSTRAP ONLY
    (`secrets_store._BOOTSTRAP_ONLY`), so store-first made a UI-set password
    PERMANENT: the store refused to change it and a new `.env` value could never
    outrank it. Both the error message and DEPLOY.md told operators to rotate in
    `.env`, which would have silently done nothing.

    Env-first fixes that without loosening the gate. A master session can write
    the store, but it can never displace a value it cannot reach — so the two
    factors stay two, and `.env` + restart really is the rotation path.

    The store still carries the FIRST one. On this deployment the variable was
    never set or documented, so all five founder-only writes (grant, revoke,
    transfer, delete user, set another agency's AI keys) answered 503 and
    nothing said why; a second factor nobody can configure is not a second
    factor, it is an outage.
    """
    env = (os.environ.get(ADMIN_PASSWORD_ENV) or "").strip()
    if not env:
        # service/.env directly, not just os.environ, for a NATIVE run
        # (scripts/start.sh), where the process reads that file and this must
        # not depend on which other module happened to call load_dotenv first.
        #
        # In the CONTAINER this file does not exist — the Dockerfile COPY is a
        # .py allow-list and nothing mounts it — so the container path is the
        # `environment:` entry in docker-compose.yml, fed from the compose-level
        # ~/fastt/.env. Both are covered here, which is the point: getting it
        # wrong reads as an empty password, i.e. a 503 on all five founder
        # writes that looks like a relay fault rather than a config gap.
        env = (dotenv_values(_ENV_FILE).get(ADMIN_PASSWORD_ENV) or "").strip()
    return env or secrets_store.stored(ADMIN_PASSWORD_ENV)


class AdminUserRow(BaseModel):
    id: str
    username: str
    created_at: datetime
    last_seen_at: datetime
    account_count: int


@admin_router.get("", response_model=list[AdminUserRow])
async def admin_list_users() -> list[AdminUserRow]:
    async with get_session() as s:
        rows = (await s.execute(
            select(
                User.id, User.username, User.created_at, User.last_seen_at,
                func.count(UserAccount.account_id).label("account_count"),
            )
            .join(UserAccount, UserAccount.user_id == User.id, isouter=True)
            .group_by(User.id)
            .order_by(User.created_at.desc())
        )).all()
    return [
        AdminUserRow(
            id=r.id, username=r.username,
            created_at=r.created_at, last_seen_at=r.last_seen_at,
            account_count=r.account_count or 0,
        )
        for r in rows
    ]


class DeleteUserBody(BaseModel):
    confirm: str = Field(..., description="must equal the literal string 'delete'")
    admin_password: str = Field(..., min_length=1, max_length=512)


def require_master() -> AuthedUser:
    """THE master-role gate. Founder-only surfaces call this; nothing re-derives it.

    Two gates exist on purpose and are not interchangeable. This one proves WHO
    is asking from the session, and is what a READ needs — a masked key hint or
    a roster of every agency is a leak whether or not the caller knows a
    password. `_assert_admin_password` proves the caller holds the founder
    secret, and is what a WRITE needs.

    They are not yet applied together everywhere they should be: the account
    grant, revoke, transfer and delete endpoints below carry the password ALONE,
    so any signed-in owner who learns it can move accounts between agencies.
    That predates this helper and is not changed here — it is written down so
    the next person adding a founder route knows the difference between the
    policy and the current state.

    401 when nobody is signed in rather than 403: the role has to be PROVEN, so
    an absent principal is refused on the same footing as a wrong one, and
    `ALLOW_ANONYMOUS_ADMIN` (which resolves to no user) buys nothing here.
    """
    user = get_request_user()
    if user is None:
        raise HTTPException(
            status_code=401, detail="sign in as the master account")
    if not user.is_admin:
        raise HTTPException(
            status_code=403, detail="master account required")
    return user


def _assert_admin_password(supplied: str) -> None:
    """Founder-gate. 503 if the relay never had the env var configured,
    403 if the caller's password doesn't match. Centralised so every
    founder-only endpoint reads the same."""
    expected = _read_admin_password()
    if not expected:
        raise HTTPException(
            status_code=503,
            detail=(
                f"{ADMIN_PASSWORD_ENV} is not configured on this relay — set it "
                f"in the relay's environment (~/fastt/.env, beside "
                f"docker-compose.yml) and restart, or once from Setup → Server keys"
            ),
        )
    if not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=403, detail="wrong admin password")


@admin_router.delete("/{user_id}")
async def admin_delete_user(user_id: str, body: DeleteUserBody = Body(...)) -> dict:
    if body.confirm != "delete":
        raise HTTPException(status_code=400, detail="confirm must equal 'delete'")
    _assert_admin_password(body.admin_password)
    async with get_session() as s:
        row = (await s.execute(
            select(User).where(User.id == user_id)
        )).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail=f"unknown user {user_id!r}")
        # CASCADE on user_accounts handles the join rows; OF accounts
        # themselves are untouched.
        await s.execute(delete(User).where(User.id == user_id))
    return {"ok": True, "deleted_user_id": user_id}


# ── Founder grant: hand an OF account to a friend ───────────────────
#
# Without this, a new friend has zero accounts until they capture one via
# the login extension. The founder may want to gift access ("here, try
# mine") without needing the friend to install anything. The grant just
# inserts a user_accounts row — the OF session bytes live on disk under
# service/sessions/<account_id>/ and don't move. Revoke is the inverse
# DELETE; the underlying account stays put for everyone else.

class GrantAccountBody(BaseModel):
    account_id: str = Field(..., min_length=1, max_length=64)
    admin_password: str = Field(..., min_length=1, max_length=512)


class RevokeAccountBody(BaseModel):
    admin_password: str = Field(..., min_length=1, max_length=512)


class GrantedAccount(BaseModel):
    account_id: str
    nickname: str | None = None
    color: str | None = None


class GrantOverview(BaseModel):
    granted: list[GrantedAccount]
    available: list[GrantedAccount]


@admin_router.get("/{user_id}/accounts", response_model=GrantOverview)
async def admin_list_user_accounts(user_id: str) -> GrantOverview:
    """Founder UI populates its grant picker from this. `granted` is the
    friend's current set; `available` is every other OF account on the
    relay (so the founder can click one to grant). Open to any session-
    holder for symmetry with the existing /admin/users list — only the
    POST/DELETE mutators carry the admin_password gate."""
    # Import locally to avoid a hard module-level dep from auth into the
    # account registry (which imports plenty itself).
    from accounts import list_accounts

    async with get_session() as s:
        # 404 fast on unknown user so the UI shows a clean error rather
        # than an empty pair of lists that look like "no accounts."
        u = (await s.execute(
            select(User.id).where(User.id == user_id)
        )).scalar_one_or_none()
        if u is None:
            raise HTTPException(status_code=404, detail=f"unknown user {user_id!r}")
        granted_ids = set((await s.execute(
            select(UserAccount.account_id).where(UserAccount.user_id == user_id)
        )).scalars().all())

    all_accounts = list_accounts()
    granted: list[GrantedAccount] = []
    available: list[GrantedAccount] = []
    for a in all_accounts:
        aid = str(a.get("id") or "")
        if not aid:
            continue
        item = GrantedAccount(
            account_id=aid,
            nickname=a.get("nickname"),
            color=a.get("color"),
        )
        (granted if aid in granted_ids else available).append(item)
    granted.sort(key=lambda x: (x.nickname or x.account_id).lower())
    available.sort(key=lambda x: (x.nickname or x.account_id).lower())
    return GrantOverview(granted=granted, available=available)


@admin_router.post("/{user_id}/accounts")
async def admin_grant_user_account(
    user_id: str, body: GrantAccountBody = Body(...),
) -> dict:
    _assert_admin_password(body.admin_password)
    from accounts import get_account as _get_account
    if _get_account(body.account_id) is None:
        raise HTTPException(status_code=404, detail=f"unknown account {body.account_id!r}")
    async with get_session() as s:
        u = (await s.execute(
            select(User.id).where(User.id == user_id)
        )).scalar_one_or_none()
        if u is None:
            raise HTTPException(status_code=404, detail=f"unknown user {user_id!r}")
        try:
            s.add(UserAccount(user_id=user_id, account_id=body.account_id))
            await s.flush()
        except IntegrityError:
            # Already linked — treat as success so a double-click in the UI
            # doesn't surface as an error.
            await s.rollback()
    return {"ok": True, "user_id": user_id, "account_id": body.account_id}


@admin_router.delete("/{user_id}/accounts/{account_id}")
async def admin_revoke_user_account(
    user_id: str, account_id: str, body: RevokeAccountBody = Body(...),
) -> dict:
    _assert_admin_password(body.admin_password)
    async with get_session() as s:
        result = await s.execute(
            delete(UserAccount).where(
                UserAccount.user_id == user_id,
                UserAccount.account_id == account_id,
            )
        )
        # 404 if nothing was removed — better than silently returning ok
        # so the founder knows the row was already gone (typo? race?).
        if (result.rowcount or 0) == 0:
            raise HTTPException(
                status_code=404,
                detail=f"user {user_id!r} does not hold account {account_id!r}",
            )
    return {"ok": True, "user_id": user_id, "account_id": account_id}


class TransferAccountBody(BaseModel):
    to_user_id: str = Field(..., min_length=1, max_length=64)
    account_id: str = Field(..., min_length=1, max_length=64)


@admin_router.post("/transfer")
async def admin_transfer_user_account(body: TransferAccountBody = Body(...)) -> dict:
    """Hand one of your own OF models to another friend.

    Source is always the signed-in user — you can only transfer accounts
    you already own, and ownership is the auth (no admin password).
    Destination gains access, source loses it, all in one DB session.
    """
    me = get_request_user()
    if me is None:
        raise HTTPException(status_code=401, detail="sign in required")
    if me.id == body.to_user_id:
        raise HTTPException(
            status_code=400, detail="cannot transfer to yourself"
        )
    if body.account_id not in me.account_ids:
        raise HTTPException(
            status_code=403,
            detail=f"you don't own account {body.account_id!r}",
        )
    async with get_session() as s:
        target_exists = (await s.execute(
            select(User.id).where(User.id == body.to_user_id)
        )).scalar_one_or_none()
        if target_exists is None:
            raise HTTPException(
                status_code=404, detail=f"unknown user {body.to_user_id!r}"
            )
        # The destination may ALREADY hold this account — that is the shape a
        # grant leaves behind (two links, one account), and handing it to the
        # co-owner is how an operator resolves it. Checked before the delete
        # rather than caught after: the insert's IntegrityError used to trigger
        # a rollback that undid the DELETE TOO, so the whole transfer became a
        # no-op while still returning ok:true — a silent success that leaves the
        # account with two owners, which now stops its AI entirely
        # (tenant_keys.owners_of refuses rather than bill the wrong agency).
        dest_already_holds = (await s.execute(
            select(UserAccount.user_id).where(
                UserAccount.user_id == body.to_user_id,
                UserAccount.account_id == body.account_id,
            )
        )).first() is not None
        result = await s.execute(
            delete(UserAccount).where(
                UserAccount.user_id == me.id,
                UserAccount.account_id == body.account_id,
            )
        )
        if (result.rowcount or 0) == 0:
            # AuthedUser.account_ids is a request-time snapshot; if the
            # link was already removed in another tab, surface a clean
            # error instead of crediting the destination without a debit.
            raise HTTPException(
                status_code=404,
                detail="account link is missing; refresh and retry",
            )
        try:
            if not dest_already_holds:
                s.add(UserAccount(
                    user_id=body.to_user_id, account_id=body.account_id,
                ))
                await s.flush()
            # The account is leaving this owner — drop the chatter visibility
            # grants they set for it. Left behind, these orphan: the access
            # editor re-surfaces a now-foreign account_id and "Save access"
            # 403s ("account X is not one of yours"). The destination owner
            # configures chatter access from scratch.
            await s.execute(delete(ChatterAccountAccess).where(
                ChatterAccountAccess.user_id == me.id,
                ChatterAccountAccess.account_id == body.account_id,
            ))
            await s.execute(delete(ChatterFolderAccess).where(
                ChatterFolderAccess.account_id == body.account_id,
                ChatterFolderAccess.chatter_id.in_(
                    select(ChatterUser.chatter_id).where(ChatterUser.user_id == me.id)
                ),
            ))
        except IntegrityError:
            await s.rollback()
    return {
        "ok": True,
        "from_user_id": me.id,
        "to_user_id": body.to_user_id,
        "account_id": body.account_id,
    }


# ── Founder impersonate ─────────────────────────────────────────────
#
# Support workflow: a friend reports "my Stats look wrong" and the founder
# wants to see exactly what they see without making them screenshot or
# handing over a password. POST starts the overlay; DELETE clears it. The
# session middleware applies the overlay on every request and 403s
# mutating verbs so the founder can't accidentally send messages from the
# friend's account while debugging.

impersonate_router = APIRouter(prefix="/admin/impersonate", tags=["admin-impersonate"])


class ImpersonateStartBody(BaseModel):
    user_id: str = Field(..., min_length=1, max_length=64)
    admin_password: str = Field(..., min_length=1, max_length=512)


@impersonate_router.post("")
async def admin_start_impersonate(
    response: Response, body: ImpersonateStartBody = Body(...),
) -> dict:
    _assert_admin_password(body.admin_password)
    me = _request_user.get()
    if me is None:
        # Without a signed-in founder we can't bind the cookie to anyone —
        # the middleware would refuse to apply it anyway.
        raise HTTPException(
            status_code=401, detail="sign in as the founder before impersonating",
        )
    if body.user_id == me.id:
        raise HTTPException(status_code=400, detail="cannot impersonate yourself")
    async with get_session() as s:
        target = (await s.execute(
            select(User).where(User.id == body.user_id)
        )).scalar_one_or_none()
        if target is None:
            raise HTTPException(status_code=404, detail=f"unknown user {body.user_id!r}")
        target_username = target.username
    _set_impersonate_cookie(response, body.user_id, me.id)
    # Loud log — the founder is taking on someone else's identity. Worth
    # a WARNING-level entry so it shows up in any standard log scan.
    log.warning(
        "impersonate START: founder=%s (id=%s) -> user=%s (id=%s)",
        me.username, me.id, target_username, body.user_id,
    )
    return {
        "ok": True,
        "impersonating": {"user_id": body.user_id, "username": target_username},
        "ttl_seconds": IMPERSONATE_TTL_SECONDS,
    }


@impersonate_router.delete("")
async def admin_stop_impersonate(response: Response) -> dict:
    imp = _impersonation.get()
    if imp is not None:
        log.warning(
            "impersonate STOP: founder=%s (id=%s) -> user=%s (id=%s)",
            imp.real_username, imp.real_user_id,
            imp.impersonated_username, imp.impersonated_user_id,
        )
    _clear_impersonate_cookie(response)
    return {"ok": True}
