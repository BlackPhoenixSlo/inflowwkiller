"""
Account registry — manages multiple OF creator accounts in one server.

Each account is a directory under service/sessions/accounts/<user_id>/ holding:

  meta.json     — { id, nickname, color, incogniton_profile_id,
                    created_at, last_used_at }
  latest.json   — { "session": "session_<ts>.json", "ts": "<ts>" }
  session_*.json + chunk_2313_*.js — captured sessions + signing chunks

service/sessions/active.json points at the account that hosts the WS pump and
serves any request that doesn't pass X-Account-Id.

Legacy flat layout (service/sessions/session_*.json) is auto-migrated into
this structure on first call to `migrate_legacy()` — bucketed by user_id
from each session's `headers.user_id`. A `.migrated` marker prevents repeat.

Thread-safe via a process-wide lock; all writes go through `save_meta()` /
`set_active_account_id()` / `record_session()`.
"""
from __future__ import annotations

import json
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
SESSIONS_DIR = HERE / "sessions"
ACCOUNTS_DIR = SESSIONS_DIR / "accounts"
ACTIVE_FILE = SESSIONS_DIR / "active.json"
MIGRATED_MARKER = SESSIONS_DIR / ".migrated"

_lock = threading.Lock()

# Color palette for new accounts (cycled). Distinguishable on the dark UI.
_COLORS = ["#7C5CFF", "#22C55E", "#F59E0B", "#EF4444", "#06B6D4",
           "#EC4899", "#84CC16", "#A855F7", "#0EA5E9", "#F97316"]


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ensure_dirs() -> None:
    SESSIONS_DIR.mkdir(exist_ok=True)
    ACCOUNTS_DIR.mkdir(exist_ok=True)


class NotAnAccountId(ValueError):
    """The verdict, as a TYPE — not as a message anyone has to string-match.

    `account_dir` raises this to mean "that string cannot name an account".
    `_account_path` catches *only* this, so a ValueError raised from BELOW the
    guard can never be mistaken for "no such account". Three of those exist and
    are all reachable: `json.JSONDecodeError` (a ValueError), `mkdir`'s
    "embedded null character", and `str.encode`'s UnicodeEncodeError on a lone
    surrogate. While the verdict was a bare ValueError the last of those was
    answered *correctly by accident* — swallowed by an `except` written for
    something else entirely.

    Subclasses ValueError so every existing `except ValueError` still works.
    """


def account_dir(account_id: str) -> Path:
    """The one place an id becomes a path — so the rule lives here, not at the
    eleven call sites (two of which write and one of which `rmtree`s).

    An id is a single directory NAME, never a path, and it must be a name the
    filesystem can actually hold: `ACCOUNTS_DIR / "/etc"` is `/etc`; NAME_MAX
    is 255 **bytes**, not characters; NUL and lone surrogates are legal in a
    `str` and illegal in a filename. Every one of those arrived as real request
    input — the first two as `?account_id=`, the surrogate as a JSON body.

    A string that cannot name one directory here is not an account, and saying
    so is an ANSWER — hence `NotAnAccountId`. OSError still means "I could not
    look" and must never be swallowed.

    WRITERS call this directly and let it raise: refusing to `mkdir` or
    `rmtree` a path we can't name is the point. READERS go through
    `_account_path`, which turns the same raise into "no such account".
    """
    aid = str(account_id)
    try:
        raw = aid.encode()          # NAME_MAX counts BYTES, so ask for bytes
    except UnicodeEncodeError:
        raw = None                  # a lone surrogate is not a filename at all
    if (raw is None or len(raw) > 255 or b"\x00" in raw
            or aid in ("", ".", "..") or "/" in aid or "\\" in aid):
        raise NotAnAccountId(f"not an account id: {aid[:40]!r}")
    return ACCOUNTS_DIR / aid


def _account_path(account_id: str, *parts: str) -> Path | None:
    """A reader's view of `account_dir`: None exactly where a writer raises.

    The one place the verdict becomes a return value. Every read surface goes
    through it — `is_account`, `load_meta`, `latest_session_path`,
    `get_active_account_id` — because when each of them owned its own
    `except` instead, fixing one left the next one raising, and a raise out of
    a reader is a 500 per request for any authed principal
    (`/admin/accounts/{id}` calls `get_account` before its 404 check).

    `NotAnAccountId`, never bare ValueError: a verdict is an answer, and
    anything else that failed is not one.
    """
    try:
        return account_dir(account_id).joinpath(*parts)
    except NotAnAccountId:
        return None


# ─── Migration of legacy flat layout ────────────────────────────

def migrate_legacy() -> dict[str, Any]:
    """Move pre-multi-account files into accounts/<user_id>/.

    Idempotent: leaves a `.migrated` marker. Returns a dict describing what
    moved (or `{"migrated": False, "reason": ...}` if no-op).
    """
    _ensure_dirs()
    with _lock:
        if MIGRATED_MARKER.exists():
            return {"migrated": False, "reason": "already-migrated"}

        legacy_sessions = sorted(SESSIONS_DIR.glob("session_*.json"))
        if not legacy_sessions:
            MIGRATED_MARKER.write_text(_utc_now())
            return {"migrated": False, "reason": "no-legacy-sessions"}

        moved: list[dict[str, str]] = []
        for sp in legacy_sessions:
            try:
                s = json.loads(sp.read_text())
            except Exception:
                continue
            uid = (s.get("headers") or {}).get("user_id")
            if not uid:
                continue
            uid = str(uid)
            adir = account_dir(uid)
            adir.mkdir(parents=True, exist_ok=True)
            target = adir / sp.name
            if not target.exists():
                shutil.move(str(sp), str(target))
            else:
                sp.unlink(missing_ok=True)
            moved.append({"file": sp.name, "account": uid})

            chunk_name = (s.get("signing") or {}).get("chunk_2313_file")
            if chunk_name:
                ch = SESSIONS_DIR / chunk_name
                if ch.exists():
                    ct = adir / chunk_name
                    if not ct.exists():
                        shutil.move(str(ch), str(ct))

        # Initialise meta.json + latest.json per account-dir from its newest
        # session, if not already present.
        seen_accounts: set[str] = set()
        for entry in moved:
            uid = entry["account"]
            if uid in seen_accounts:
                continue
            seen_accounts.add(uid)
            _initialise_account_from_sessions(uid)

        # Pick the newest session globally → active account
        newest: tuple[Path, str] | None = None
        for adir in ACCOUNTS_DIR.iterdir():
            if not adir.is_dir():
                continue
            for sp in adir.glob("session_*.json"):
                if newest is None or sp.stat().st_mtime > newest[0].stat().st_mtime:
                    newest = (sp, adir.name)
        if newest:
            ACTIVE_FILE.write_text(json.dumps({"active_account_id": newest[1]}, indent=2))

        MIGRATED_MARKER.write_text(_utc_now())
        return {"migrated": True, "moved": moved, "active": newest[1] if newest else None}


def _initialise_account_from_sessions(account_id: str) -> None:
    """After a migration places session files in an account dir, materialise
    meta.json + latest.json from the newest session there."""
    adir = account_dir(account_id)
    sessions = sorted(adir.glob("session_*.json"), key=lambda p: p.stat().st_mtime)
    if not sessions:
        return
    newest = sessions[-1]
    try:
        s = json.loads(newest.read_text())
    except Exception:
        return

    meta_path = adir / "meta.json"
    if not meta_path.exists():
        meta = {
            "id": account_id,
            "nickname": f"Account {account_id}",
            "color": _COLORS[abs(hash(account_id)) % len(_COLORS)],
            "incogniton_profile_id": s.get("profile_id") if s.get("profile_id") not in (None, "paste-curl") else None,
            "created_at": _utc_now(),
            "last_used_at": s.get("captured_at"),
        }
        meta_path.write_text(json.dumps(meta, indent=2))

    latest_path = adir / "latest.json"
    if not latest_path.exists():
        latest_path.write_text(json.dumps({
            "session": newest.name,
            "ts": newest.stem.removeprefix("session_"),
        }, indent=2))


# ─── Meta CRUD ──────────────────────────────────────────────────

def load_meta(account_id: str) -> dict[str, Any] | None:
    p = _account_path(account_id, "meta.json")
    if p is None or not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def save_meta(account_id: str, meta: dict[str, Any]) -> None:
    with _lock:
        adir = account_dir(account_id)
        adir.mkdir(parents=True, exist_ok=True)
        (adir / "meta.json").write_text(json.dumps(meta, indent=2))


def list_accounts() -> list[dict[str, Any]]:
    """Return all accounts with their meta, sorted by last_used_at desc."""
    _ensure_dirs()
    out: list[dict[str, Any]] = []
    if not ACCOUNTS_DIR.is_dir():
        return out
    for adir in ACCOUNTS_DIR.iterdir():
        if not adir.is_dir():
            continue
        meta = load_meta(adir.name)
        if not meta:
            # Account dir exists with sessions but no meta — materialise it
            _initialise_account_from_sessions(adir.name)
            meta = load_meta(adir.name)
            if not meta:
                continue
        latest = (adir / "latest.json")
        meta["has_session"] = latest.exists()
        out.append(meta)
    out.sort(key=lambda m: m.get("last_used_at") or "", reverse=True)
    return out


def is_account(account_id: str) -> bool:
    """Does this id name a real account? ONE stat, no descriptor.

    The isolation gate asks about one candidate, so it wants membership, never
    the whole set — enumerating cost a descriptor per request, which is how an
    fd shortage twice took account isolation down with it (2026-07-28, 07-30).
    `stat(2)` takes a path, so this keeps answering while the process is out
    of fds. A directory here IS an account, even one with unreadable meta.

    Errors are NOT swallowed: False means "not an account", a raise means "I
    could not look". Collapsing those is what turned the gate off — the caller
    reads a negative as "let it through", so a falsy default on OSError here
    is fail-OPEN, not fail-closed. `_account_path` is the one exception, and
    only for `NotAnAccountId`: that IS the verdict "this can't name an account".
    """
    p = _account_path(account_id)
    return p is not None and p.is_dir()


# (identity of ACCOUNTS_DIR, the ids it held then). One assignment publishes
# both, so no lock is needed: the worst race is two threads scanning the same
# directory and storing the same set.
_IDS_CACHE: tuple[tuple[int, int, int, int], frozenset[str]] | None = None


def account_ids() -> frozenset[str]:
    """Every account id — the SET, without opening a single file.

    `list_accounts()` answers this question the expensive way: it scandirs, then
    reads `meta.json` and stats `latest.json` for every account, to build dicts
    the caller then throws away except for `["id"]`. `auth.py` was doing that on
    EVERY request an admin made (there and again on the impersonation path), so
    the master's session cost a directory walk per request and the descriptors
    it spent showed up as the process-wide shortage that takes the relay down
    (2026-07-28, 2026-08-08) — see `is_account` above for the same lesson learnt
    on the isolation gate.

    Cached against the identity of ACCOUNTS_DIR itself, which is the exact
    clock for this question: creating or removing an account creates or removes
    a child entry, and that bumps the parent's mtime AND ctime. Anything that
    happens *inside* an account (a new session, an edited nickname) does not
    change the id set and correctly does not invalidate. So a freshly captured
    account is visible on the very next request, with no TTL to wait out — the
    property auth.py's caller depends on. A TTL would have been the wrong
    device twice over: it delays the new account the comment promises, and it
    keeps a DELETED account authorised until it expires.

    `(dev, inode)` rides along so that replacing the directory itself — a
    restored backup, a re-pointed bind mount — reads as a change rather than
    as "same mtime, same answer".

    A directory here IS an account, even one whose meta is missing or
    unreadable. That makes this a superset of `list_accounts()`, which skips a
    directory it cannot materialise. For the caller that matters — the master's
    account set — a superset is the safe direction: the comment at that call
    site exists precisely so a registry-only account can never 403 the master.

    Errors are NOT swallowed. An empty set is the answer "there are no
    accounts", never "I could not look"; conflating those is how a guard here
    turned account isolation OFF for the length of an fd outage. The cache makes
    the raise nearly unreachable in the case that matters: `stat(2)` needs no
    descriptor, so a warm cache keeps answering while the process is out of
    them, and only a cold one has to scandir.
    """
    global _IDS_CACHE
    try:
        st = ACCOUNTS_DIR.stat()
    except FileNotFoundError:
        return frozenset()
    stamp = (st.st_dev, st.st_ino, st.st_mtime_ns, st.st_ctime_ns)
    cached = _IDS_CACHE
    if cached is not None and cached[0] == stamp:
        return cached[1]
    ids = frozenset(d.name for d in ACCOUNTS_DIR.iterdir() if d.is_dir())
    _IDS_CACHE = (stamp, ids)
    return ids


def get_account(account_id: str) -> dict[str, Any] | None:
    return load_meta(account_id)


def upsert_account(account_id: str, *,
                   nickname: str | None = None,
                   color: str | None = None,
                   incogniton_profile_id: str | None = None) -> dict[str, Any]:
    """Create or update an account's metadata. Pass only the fields you want
    to change — others are preserved."""
    existing = load_meta(account_id) or {}
    meta = {
        "id": account_id,
        "nickname": nickname if nickname is not None else existing.get("nickname") or f"Account {account_id}",
        "color": color if color is not None else existing.get("color") or _COLORS[abs(hash(account_id)) % len(_COLORS)],
        "incogniton_profile_id": (
            incogniton_profile_id if incogniton_profile_id is not None
            else existing.get("incogniton_profile_id")
        ),
        "created_at": existing.get("created_at") or _utc_now(),
        "last_used_at": existing.get("last_used_at"),
    }
    save_meta(account_id, meta)
    return meta


def delete_account(account_id: str) -> bool:
    """Remove an account directory entirely. If it was active, clear active.
    Returns True if anything was deleted."""
    with _lock:
        adir = account_dir(account_id)
        if not adir.is_dir():
            return False
        shutil.rmtree(adir)
        active = _read_active_unlocked()
        if active == account_id:
            ACTIVE_FILE.write_text(json.dumps({"active_account_id": None}, indent=2))
        return True


# ─── Active-account pointer ────────────────────────────────────

def _read_active_unlocked() -> str | None:
    if not ACTIVE_FILE.exists():
        return None
    try:
        return json.loads(ACTIVE_FILE.read_text()).get("active_account_id")
    except Exception:
        return None


def get_active_account_id() -> str | None:
    """Return the explicitly-set active account, or — if none — the newest
    account that has a session (so a fresh install just works)."""
    with _lock:
        active = _read_active_unlocked()
    latest = _account_path(active, "latest.json") if active else None
    if latest is not None and latest.exists():
        return active

    # Fallback: most-recently-used account with a session
    candidates = [a for a in list_accounts() if a.get("has_session")]
    if not candidates:
        return None
    fallback = candidates[0]["id"]
    set_active_account_id(fallback)
    return fallback


def set_active_account_id(account_id: str | None) -> None:
    _ensure_dirs()
    with _lock:
        ACTIVE_FILE.write_text(json.dumps({"active_account_id": account_id}, indent=2))


# ─── Session pointer ───────────────────────────────────────────

def latest_session_path(account_id: str) -> Path | None:
    """Return the absolute path to the account's current session JSON,
    or None if the account has no session yet."""
    latest = _account_path(account_id, "latest.json")
    if latest is None or not latest.exists():
        return None
    try:
        # `latest.parent` IS the account dir — no second `account_dir` call,
        # so nothing in this try can raise the verdict.
        sp = latest.parent / json.loads(latest.read_text())["session"]
    except (json.JSONDecodeError, KeyError, TypeError):
        # Exactly the three ways a corrupt or half-written latest.json reads as
        # "no session yet": unparseable, no "session" key, or parsed to a
        # non-dict (`null` / a list — TypeError, which the old `ValueError` here
        # did NOT catch, so that case was a 500).
        #
        # Deliberately NOT `except Exception`: under fd exhaustion read_text()
        # raises OSError, and swallowing it here reported a healthy account as
        # "has no captured session. Run the bootstrap." Let OSError propagate.
        return None
    return sp if sp.exists() else None


def record_session(account_id: str, session_path: Path) -> None:
    """Update latest.json + bump last_used_at on this account's meta."""
    with _lock:
        adir = account_dir(account_id)
        adir.mkdir(parents=True, exist_ok=True)
        ts = session_path.stem.removeprefix("session_")
        (adir / "latest.json").write_text(json.dumps({
            "session": session_path.name, "ts": ts,
        }, indent=2))
        meta = load_meta(account_id) or {
            "id": account_id,
            "nickname": f"Account {account_id}",
            "color": _COLORS[abs(hash(account_id)) % len(_COLORS)],
            "incogniton_profile_id": None,
            "created_at": _utc_now(),
        }
        meta["last_used_at"] = _utc_now()
        (adir / "meta.json").write_text(json.dumps(meta, indent=2))
