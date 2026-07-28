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


def account_dir(account_id: str) -> Path:
    return ACCOUNTS_DIR / str(account_id)


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
    p = account_dir(account_id) / "meta.json"
    if not p.exists():
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


def list_account_ids() -> set[str]:
    """Just the ids — ONE directory scan, no per-account file reads.

    `list_accounts()` opens meta.json and stats latest.json for every account to
    build its dicts. The account-isolation middleware runs on every `/admin/*`
    request carrying an account_id and uses nothing from those dicts but `id`,
    so it was paying ~2 file opens per account per request. On a vault pane that
    releases 500 tiles at once, with 17 accounts, that is ~17k file opens
    competing for the very descriptors the tiles need — it is what turned an
    fd shortage into `OSError: [Errno 24] ... '/app/service/sessions/accounts'`
    (2026-07-28).

    A directory here IS an account: the gate's question is only "does this id
    name a real account", and one whose meta is missing or unreadable still
    does. That also fails CLOSED — `list_accounts()` drops such a directory,
    which let an id it couldn't parse past the gate unchecked.
    """
    _ensure_dirs()
    try:
        return {d.name for d in ACCOUNTS_DIR.iterdir() if d.is_dir()}
    except OSError:
        return set()


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
    if active and (account_dir(active) / "latest.json").exists():
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
    latest = account_dir(account_id) / "latest.json"
    if not latest.exists():
        return None
    try:
        meta = json.loads(latest.read_text())
        sp = account_dir(account_id) / meta["session"]
    except (ValueError, KeyError):
        # A corrupt or half-written latest.json reads as "no session yet".
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
