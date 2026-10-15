"""
Proxy registry — persistent store of per-account proxies.

Each session JSON in service/sessions/ can be paired with a proxy in this
registry by label. OFClient consults `get_proxy_for_session(filename)` on
load and routes all curl_cffi traffic through it.

Single source of truth lives in service/proxies.json. Schema:

  {
    "proxies": [
      {
        "label":              "hu-1",
        "scheme":             "http",      // http | https | socks5
        "host":               "188.244.113.72",
        "port":               12323,
        "username":           "...",       // optional
        "password":           "...",       // optional — plain text, see notes
        "notes":              "Budapest, HU AS211439 LOCOTORPI",
        "assigned_session":   "session_20260517T192858Z.json",  // or null
        "verified_ip":        "188.244.113.72",
        "verified_at":        "2026-05-17T22:14:03Z",
        "verified_geo":       "Budapest, Budapest, HU"
      }
    ]
  }

Phase-1 (testing) stores credentials in plain text. Phase-2 should move
secrets into the OS keychain — see TODO section O.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REGISTRY_PATH = HERE / "proxies.json"

_lock = threading.Lock()


def _empty() -> dict[str, Any]:
    return {"proxies": []}


def load() -> dict[str, Any]:
    if not REGISTRY_PATH.exists():
        return _empty()
    try:
        data = json.loads(REGISTRY_PATH.read_text())
    except json.JSONDecodeError:
        return _empty()
    if not isinstance(data, dict) or "proxies" not in data:
        return _empty()
    return data


def save(data: dict[str, Any]) -> None:
    REGISTRY_PATH.write_text(json.dumps(data, indent=2, sort_keys=False))


def list_proxies() -> list[dict[str, Any]]:
    return load().get("proxies", [])


def get_by_label(label: str) -> dict[str, Any] | None:
    for p in list_proxies():
        if p.get("label") == label:
            return p
    return None


def get_for_session(session_filename: str) -> dict[str, Any] | None:
    """Return the proxy currently assigned to `session_filename`, or None.
    Legacy: pre-multi-account installs bound proxies to a specific session
    file. New installs bind to account_id (see `get_for_account`)."""
    for p in list_proxies():
        if p.get("assigned_session") == session_filename:
            return p
    return None


def _ids_on(p: dict[str, Any]) -> list[str]:
    """Return the list of account ids bound to this proxy.

    Reads the canonical `assigned_account_ids` list and, for legacy entries
    written before multi-binding was supported, falls back to the singular
    `assigned_account_id`. Callers should treat the result as a set."""
    ids = p.get("assigned_account_ids")
    if isinstance(ids, list) and ids:
        return [str(x) for x in ids]
    legacy = p.get("assigned_account_id")
    return [str(legacy)] if legacy else []


def get_for_account(account_id: str) -> dict[str, Any] | None:
    """Return the proxy assigned to this OF account (preferred — survives
    re-captures because each new session is bucketed under the same id).

    Each account belongs to at most ONE proxy (enforced in `assign_account`);
    a proxy may serve many accounts (and the picker flags >1 with a shared-
    egress warning, because OF can correlate same-IP accounts)."""
    if not account_id:
        return None
    aid = str(account_id)
    for p in list_proxies():
        if aid in _ids_on(p):
            return p
    return None


def proxy_url(p: dict[str, Any]) -> str:
    """Render a proxy entry to the URL form curl_cffi/requests expects."""
    scheme = p.get("scheme", "http")
    host, port = p["host"], p["port"]
    user, pw = p.get("username"), p.get("password")
    if user and pw:
        return f"{scheme}://{user}:{pw}@{host}:{port}"
    return f"{scheme}://{host}:{port}"


def proxies_dict_for_requests(p: dict[str, Any]) -> dict[str, str]:
    """Shape curl_cffi.requests.Session.proxies wants."""
    u = proxy_url(p)
    return {"http": u, "https": u}


def upsert(entry: dict[str, Any]) -> dict[str, Any]:
    """Add or replace by label. Returns the stored entry."""
    label = entry.get("label")
    if not label:
        raise ValueError("proxy entry needs a 'label'")
    for required in ("host", "port"):
        if not entry.get(required):
            raise ValueError(f"proxy entry missing '{required}'")
    entry.setdefault("scheme", "http")
    entry.setdefault("notes", "")
    entry.setdefault("assigned_session", None)     # legacy per-file binding
    entry.setdefault("assigned_account_id", None)  # legacy singular — mirrored from list[0]
    entry.setdefault("assigned_account_ids", [])   # canonical: many-accounts-per-proxy
    entry.setdefault("user_id", None)              # owner; null = shared default pool
    entry.setdefault("verified_ip", None)
    entry.setdefault("verified_at", None)
    entry.setdefault("verified_geo", None)

    with _lock:
        data = load()
        for i, p in enumerate(data["proxies"]):
            if p.get("label") == label:
                # Preserve any fields the caller didn't supply.
                merged = {**p, **{k: v for k, v in entry.items() if v is not None or k == "assigned_session"}}
                data["proxies"][i] = merged
                save(data)
                return merged
        data["proxies"].append(entry)
        save(data)
        return entry


class ProxyPermissionError(PermissionError):
    """Raised by `upsert_from_line` when the acting user may not mutate the
    matched proxy (shared default, or another user's private proxy)."""


def _looks_like_hostport(s: str) -> bool:
    """True when `s` is `host:port` with a numeric, in-range port."""
    bits = s.split(":")
    return len(bits) == 2 and bits[1].isdigit() and 0 < int(bits[1]) <= 65535


def parse_proxy_line(line: str, default_scheme: str = "http") -> dict[str, Any]:
    """Parse ONE pasted proxy line into a registry entry dict.

    Accepted formats (optional trailing whitespace-separated label on any):
      * scheme://user:pass@host:port
      * user:pass@host:port
      * host:port:user:pass
      * host:port@user:pass
      * host:port

    The label defaults to `host:port` when none is supplied. Raises
    ValueError on anything it cannot parse (the bulk endpoint turns that into
    a per-line error with the password redacted — see `redact_line`)."""
    raw = line.strip()
    if not raw:
        raise ValueError("empty line")

    # A proxy token never contains whitespace, so the first whitespace split
    # cleanly separates the proxy from an optional human label.
    bits = raw.split()
    tok = bits[0]
    label = " ".join(bits[1:]).strip() if len(bits) > 1 else None

    scheme = default_scheme
    if "://" in tok:
        scheme, tok = tok.split("://", 1)
        scheme = scheme.strip().lower() or default_scheme

    username: str | None = None
    password: str | None = None
    host: str | None = None
    port_s: str | None = None

    if "@" in tok:
        # rsplit so a ':' inside the password doesn't confuse the @ split.
        left, right = tok.rsplit("@", 1)
        if _looks_like_hostport(left) and not _looks_like_hostport(right):
            hostport, cred = left, right              # host:port@user:pass
        else:
            cred, hostport = left, right              # user:pass@host:port (default)
        if not _looks_like_hostport(hostport):
            raise ValueError(f"could not find host:port in {tok!r}")
        host, port_s = hostport.split(":")
        if ":" in cred:
            username, password = cred.split(":", 1)
        else:
            username = cred or None
    else:
        parts = tok.split(":")
        if len(parts) == 2:
            host, port_s = parts                       # host:port
        elif len(parts) == 4:
            host, port_s, username, password = parts   # host:port:user:pass
        else:
            raise ValueError(f"unrecognized proxy format: {tok!r}")

    if not host:
        raise ValueError("missing host")
    try:
        port = int(port_s)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ValueError(f"port is not a number: {port_s!r}")
    if not (0 < port <= 65535):
        raise ValueError(f"port out of range: {port}")

    entry: dict[str, Any] = {
        "label": label or f"{host}:{port}",
        "scheme": scheme,
        "host": host,
        "port": port,
    }
    if username:
        entry["username"] = username
    if password:
        entry["password"] = password
    return entry


def _mask_secret_field(s: str, *, allow_port: bool = True) -> str:
    """Mask the value after the first ':' so `redact_line` never prints a
    password. With `allow_port` (the host:port side) an obvious port (short +
    all digits) is left visible; on the credential side (`allow_port=False`)
    the value is masked unconditionally — a password can be all digits too."""
    if ":" not in s:
        return s
    a, b = s.split(":", 1)
    if allow_port and b.isdigit() and len(b) <= 5:
        return s
    return f"{a}:***"


def redact_line(line: str) -> str:
    """Best-effort password mask for a line that FAILED to parse, so the
    error we surface/log never echoes the credential back. Over-masks rather
    than risk a leak."""
    raw = line.strip()
    bits = raw.split()
    tok = bits[0] if bits else ""
    suffix = (" " + " ".join(bits[1:])) if len(bits) > 1 else ""

    prefix = ""
    body = tok
    if "://" in tok:
        scheme, body = tok.split("://", 1)
        prefix = f"{scheme}://"

    if "@" in body:
        # `user:pass@host:port` — the left side is always credentials, so mask
        # its password even if it is all digits; keep host:port visible.
        left, right = body.rsplit("@", 1)
        body = f"{_mask_secret_field(left, allow_port=False)}@{_mask_secret_field(right)}"
    else:
        parts = body.split(":")
        if len(parts) >= 4:
            # host:port:user:pass[:...] — mask the secret field(s)
            # unconditionally, keep host:port (and user) visible.
            body = ":".join(parts[:3] + ["***"])
    return prefix + body + suffix


def upsert_from_line(line: str, *, default_scheme: str = "http",
                     user_id: str | None = None) -> dict[str, Any]:
    """Parse + upsert a single pasted line. Preserves an existing entry's
    account bindings (`assigned_account_ids` / legacy `assigned_session`) and
    its owner, and enforces the ownership rule:

      * a signed-in user (`user_id` set) may NOT overwrite a shared-default
        proxy (`user_id is None`) or another user's private proxy;
      * an unauthed caller (`user_id is None` — founder curl / cron) bypasses,
        matching every other proxy route's gate.

    A brand-new proxy created by a signed-in user is tagged with their id."""
    entry = parse_proxy_line(line, default_scheme=default_scheme)
    label = entry["label"]
    existing = get_by_label(label)
    if existing is not None:
        owner = existing.get("user_id")
        if user_id is not None:
            if owner is None:
                raise ProxyPermissionError(
                    f"proxy {label!r} is a shared default (read-only)")
            if owner != user_id:
                raise ProxyPermissionError(
                    f"proxy {label!r} belongs to another user")
        # Preserve owner + bindings so a re-paste never silently unbinds
        # accounts or reassigns ownership.
        entry["user_id"] = owner
        entry["assigned_account_ids"] = list(existing.get("assigned_account_ids") or [])
        entry["assigned_session"] = existing.get("assigned_session")
        entry.setdefault("verified_ip", existing.get("verified_ip"))
        entry.setdefault("verified_geo", existing.get("verified_geo"))
        entry.setdefault("verified_at", existing.get("verified_at"))
    else:
        entry["user_id"] = user_id
    return upsert(entry)


def remove(label: str) -> bool:
    with _lock:
        data = load()
        before = len(data["proxies"])
        data["proxies"] = [p for p in data["proxies"] if p.get("label") != label]
        if len(data["proxies"]) == before:
            return False
        save(data)
        return True


def assign(label: str, session_filename: str | None) -> dict[str, Any]:
    """Bind a proxy to a session file (LEGACY). Prefer `assign_account`.
    Enforces 1:1 — if another proxy already owns that session, it is unassigned."""
    with _lock:
        data = load()
        target = None
        for p in data["proxies"]:
            if p.get("label") == label:
                target = p
            elif session_filename and p.get("assigned_session") == session_filename:
                p["assigned_session"] = None
        if target is None:
            raise KeyError(f"no proxy with label {label!r}")
        target["assigned_session"] = session_filename
        save(data)
        return target


def _sync_legacy_singular(p: dict[str, Any]) -> None:
    """Mirror `assigned_account_ids[0]` into the legacy `assigned_account_id`
    so older readers (db/import_legacy.py, any out-of-band tooling) still see
    SOMETHING when a proxy is bound. None when the list is empty."""
    ids = p.get("assigned_account_ids") or []
    p["assigned_account_id"] = str(ids[0]) if ids else None


def assign_account(label: str, account_id: str | None) -> dict[str, Any]:
    """Bind an OF account to a proxy. Each account belongs to AT MOST one
    proxy — if the account is currently bound to a different proxy, it is
    moved here. Many accounts may share the same proxy (the UI surfaces a
    warning when >1 share egress because OF can correlate same-IP behaviour).

    `account_id=None` is preserved as a no-op (the old "unbind the single
    binding" gesture moved to `unbind_account`); silently ignoring keeps
    legacy callers safe."""
    if account_id is None:
        # Existing callers (and the older UI) sometimes hit this with null
        # to express "unbind". With multi-binding, that gesture is ambiguous
        # (unbind WHICH account?) so the explicit `unbind_account` path
        # owns it. Look up the target so callers still get the proxy back.
        for p in list_proxies():
            if p.get("label") == label:
                return p
        raise KeyError(f"no proxy with label {label!r}")

    aid = str(account_id)
    with _lock:
        data = load()
        target = None
        for p in data["proxies"]:
            # Lift legacy singular into the list before mutating, so a
            # pre-multi-binding entry doesn't silently lose its original
            # binding when a second account is added.
            ids = _ids_on(p)
            if p.get("label") == label:
                target = p
                if aid not in ids:
                    ids.append(aid)
            else:
                # Account moves: drop it from every OTHER proxy. Same
                # invariant the old single-binding code enforced, just
                # expressed against a list.
                ids = [x for x in ids if x != aid]
            p["assigned_account_ids"] = ids
            _sync_legacy_singular(p)
        if target is None:
            raise KeyError(f"no proxy with label {label!r}")
        # Clear the legacy filename binding so the two don't fight each other.
        target["assigned_session"] = None
        save(data)
        return target


def unbind_account(label: str, account_id: str) -> dict[str, Any]:
    """Remove a single account from a proxy's binding list. Account is left
    without a proxy (falls back to direct egress until reassigned)."""
    aid = str(account_id)
    with _lock:
        data = load()
        target = None
        for p in data["proxies"]:
            if p.get("label") == label:
                target = p
                # Lift legacy singular into the list first so a pre-multi
                # entry can be unbound just like any other.
                ids = _ids_on(p)
                p["assigned_account_ids"] = [x for x in ids if x != aid]
                _sync_legacy_singular(p)
                break
        if target is None:
            raise KeyError(f"no proxy with label {label!r}")
        save(data)
        return target


def migrate_legacy_assignments(filename_to_account: dict[str, str]) -> int:
    """Promote `assigned_session` (filename) bindings into the per-account
    list using a {session_filename: account_id} map (built from the account
    dirs). Idempotent. Returns how many proxies were rebound.

    Also lifts any pre-multi-binding `assigned_account_id` value into the
    `assigned_account_ids` list so reads through the canonical accessor
    pick it up. The list is the source of truth; the singular stays
    mirrored for legacy readers."""
    rebound = 0
    with _lock:
        data = load()
        for p in data["proxies"]:
            # Lift legacy singular into the list if not yet migrated.
            ids = list(p.get("assigned_account_ids") or [])
            legacy = p.get("assigned_account_id")
            if legacy and str(legacy) not in [str(x) for x in ids]:
                ids.append(str(legacy))
                p["assigned_account_ids"] = ids
            if ids:
                _sync_legacy_singular(p)
                continue  # already on the new scheme
            sf = p.get("assigned_session")
            if sf and sf in filename_to_account:
                aid = str(filename_to_account[sf])
                p["assigned_account_ids"] = [aid]
                _sync_legacy_singular(p)
                # Don't clear assigned_session immediately — keep it as an audit
                # crumb so an admin can still see which capture originally
                # introduced this binding.
                rebound += 1
        if rebound:
            save(data)
    return rebound


def record_verification(label: str, *, ip: str | None, geo: str | None,
                        ok: bool) -> None:
    """Persist the last egress-IP probe result for a proxy."""
    with _lock:
        data = load()
        for p in data["proxies"]:
            if p.get("label") == label:
                if ok:
                    p["verified_ip"] = ip
                    p["verified_geo"] = geo
                    p["verified_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                else:
                    p["verified_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                    p["verified_ip"] = None
                save(data)
                return


def probe(p: dict[str, Any], timeout_s: int = 15) -> dict[str, Any]:
    """Fetch the egress IP through the proxy + a coarse geo lookup.
    Returns {ok, ip, geo, error?}. Safe to call from a request handler."""
    import urllib.request
    import urllib.error
    import socket

    url = proxy_url(p)
    ip = None
    geo = None
    err = None
    try:
        # Build an opener so we can target the proxy precisely.
        proxy_handler = urllib.request.ProxyHandler({"http": url, "https": url})
        opener = urllib.request.build_opener(proxy_handler)
        with opener.open("https://api.ipify.org", timeout=timeout_s) as r:
            ip = r.read().decode().strip()
    except (urllib.error.URLError, socket.timeout, OSError) as e:
        err = f"proxy_probe_failed: {e}"

    if ip:
        try:
            with urllib.request.urlopen(f"https://ipinfo.io/{ip}/json", timeout=timeout_s) as r:
                info = json.loads(r.read().decode())
                parts = [info.get("city"), info.get("region"), info.get("country")]
                geo = ", ".join(p for p in parts if p)
        except Exception:
            geo = None

    return {"ok": ip is not None, "ip": ip, "geo": geo, "error": err}
