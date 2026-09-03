"""
Fansly backend for the relay — the per-account bridge to the shim.

`_load_client(account_id)` returns an `OFClient` for OnlyFans accounts. For
accounts whose meta says `platform == "fansly"`, it returns a `FanslyShimClient`
instead (see fansly/fansly_shim.py) — same read surface, OF-shaped output, so the
inbox route works unchanged.

This module is the seam that keeps the two worlds apart:
  • the Fansly code lives in the sibling `fansly/` package (kept separate so it
    can be reviewed / open-sourced independently); we add it to the path here,
    in one place, rather than scattering sys.path edits.
  • each Fansly account's captured session lives beside its OF cousins, at
    service/sessions/accounts/<id>/fansly_session.json — written by the
    login-capture extension's Fansly path.

Nothing here runs for OnlyFans accounts; importing it has no effect until a
Fansly account is actually loaded.
"""
from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

import accounts as account_registry

_HERE = Path(__file__).resolve().parent
_FANSLY_PKG = _HERE.parent / "fansly"
if str(_FANSLY_PKG) not in sys.path:
    sys.path.insert(0, str(_FANSLY_PKG))

# Imported lazily-but-once; only reached when a Fansly account is loaded.
from fansly_client import FanslySession  # noqa: E402
from fansly_shim import FanslyShimClient  # noqa: E402

SESSION_FILENAME = "fansly_session.json"

_lock = threading.Lock()
_pool: dict[str, FanslyShimClient] = {}


def session_path(account_id: str) -> Path:
    """Where this Fansly account's captured session lives — next to its OF
    cousins under the account dir."""
    return account_registry.account_dir(account_id) / SESSION_FILENAME


def is_fansly(account_id: str) -> bool:
    return account_registry.get_platform(account_id) == "fansly"


def get_client(account_id: str) -> FanslyShimClient:
    """Return (and cache) the shim client for a Fansly account.

    Raises FileNotFoundError when the account has no captured Fansly session —
    the same signal `_load_client` already translates into a structured 503, so
    the "capture a session" remedy path is shared with OnlyFans."""
    with _lock:
        cached = _pool.get(account_id)
        if cached is not None:
            return cached

        path = session_path(account_id)
        if not path.exists():
            raise FileNotFoundError(
                f"account {account_id} has no Fansly session at {path.name} — "
                f"capture one with the login extension (Fansly tab)"
            )

        data = json.loads(path.read_text())
        session = FanslySession(data, path)
        client = FanslyShimClient(session)
        # Reuse the captured device id; only hit /device/id if we truly lack one.
        if not session.device_id:
            client.ensure_device_id()
        _pool[account_id] = client
        return client


def drop(account_id: str | None = None) -> None:
    """Evict a cached client (e.g. after a fresh capture is written).

    `None` evicts every account, matching client_pool.evict — which is what
    calls this, so one invalidation covers both platforms' pools."""
    with _lock:
        if account_id is None:
            _pool.clear()
        else:
            _pool.pop(account_id, None)
