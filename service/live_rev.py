"""
Live x-of-rev tracker.

OnlyFans pins a per-deploy build hash into the `x-of-rev` request header
on every signed `/api2/v2/*` call from their SPA. The relay replays
that header on outgoing calls; when our captured rev drifts too far
behind OF's current build, signed requests start failing intermittently
("session stale — re-capture required").

History: we used to scrape OF's homepage HTML to learn the current rev.
That never actually worked — OF doesn't embed the rev in the homepage
HTML, only in the SPA's runtime headers — so the probe logged
`no_rev_in_html` every cycle for nothing.

Today the cache is fed from two trustworthy sources:

  • **Session files** (passive seed): on first lookup we scan all
    accounts' latest `session_*.json` and pick the most recently
    captured `headers.x_of_rev`. That's what fresh logins write.
  • **API response headers** (opportunistic): every outgoing call in
    `of_client._http_call` peeks at the response's `x-of-rev` header
    and calls `note()`. OF echoes their current build back on most
    `/api2/v2/*` responses, so the cache stays current as long as the
    relay is actively making any API call.

`get()`, `refresh()`, `compare()` keep their previous signatures so
admin endpoints in server.py don't need changes.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any

log = logging.getLogger("of-relay.live-rev")

_lock = threading.Lock()
_cache: dict[str, Any] = {
    "rev": None,
    "fetched_at": 0.0,
    "source": None,
    "error": None,
}


def note(rev: str | None, source: str = "unknown") -> None:
    """External observer hook — call this whenever we see a fresh rev
    in the wild (response header, capture flow, etc.). No-op when rev
    is empty or already matches the cached value. Cheap; safe to call
    on every outgoing request."""
    if not rev:
        return
    rev = str(rev).strip()
    if not rev:
        return
    with _lock:
        if _cache["rev"] == rev:
            _cache["fetched_at"] = time.time()
            return
        prev = _cache["rev"]
        _cache.update(rev=rev, source=source, fetched_at=time.time(), error=None)
    if prev != rev:
        log.info("live x-of-rev updated %r → %r (source=%s)", prev, rev, source)


def _seed_from_sessions() -> None:
    """Scan every account's latest session file, pick the rev from the
    one with the freshest `captured_at`. Called lazily by `get()` when
    the cache is cold so a freshly-booted relay knows the live rev
    before its first outgoing call lands."""
    try:
        # Imported inside the function so module load order doesn't matter
        # (accounts.py also imports nothing from live_rev today, but stay
        # defensive — avoids any future circular-import surprise).
        import accounts as account_registry
    except Exception:
        log.warning("live_rev session seed: cannot import accounts module", exc_info=True)
        return
    candidates: list[tuple[str, str, str]] = []  # (captured_at, rev, account_id)
    try:
        for meta in account_registry.list_accounts():
            aid = meta.get("id")
            if not aid:
                continue
            sp = account_registry.latest_session_path(aid)
            if not sp:
                continue
            try:
                s = json.loads(sp.read_text())
            except Exception:
                log.warning("live_rev session seed: cannot read session for %s", aid, exc_info=True)
                continue
            rev = (s.get("headers") or {}).get("x_of_rev")
            ts = s.get("captured_at")
            if rev and ts:
                candidates.append((str(ts), str(rev), str(aid)))
    except Exception:
        log.warning("live_rev session seed walk failed", exc_info=True)
        return
    if not candidates:
        # Worth knowing — if NO account has a session-captured rev, drift
        # detection is degraded until the first /api2/v2/* response lands.
        log.info("live_rev session seed: no captured x_of_rev in any account session")
        return
    candidates.sort(reverse=True)  # ISO-8601 ts sorts lexicographically
    _, rev, aid = candidates[0]
    note(rev, source=f"session_seed:{aid}")


def refresh(force: bool = False) -> dict[str, Any]:
    """Compat shim — used to do an HTTP probe of OF's homepage. That
    source never actually contained the rev, so we replaced it with
    passive observation (see `note()` and `of_client._http_call`).
    Today this just seeds from session files if the cache is cold; the
    `force` kwarg is accepted but doesn't trigger any network call."""
    with _lock:
        have_rev = _cache["rev"]
    if not have_rev or force:
        _seed_from_sessions()
    return dict(_cache)


def get(stale_ok: bool = True) -> dict[str, Any]:
    """Read the cached snapshot. Cold start (no rev cached yet) triggers
    a one-time session-file seed. `stale_ok` is preserved for callers
    but has no practical effect anymore — we don't have a hot-refresh
    path now that the HTML probe is gone."""
    del stale_ok  # intentionally unused — kept for backward-compat
    if _cache["rev"] is None:
        return refresh()
    return dict(_cache)


def compare(session_rev: str | None) -> dict[str, Any]:
    """Compare a session's recorded x-of-rev against the live one.
    `drift=True` is only reported when both values are known and they
    disagree. When we don't yet have a live value (cold relay, no
    sessions, no API calls yet), drift defaults to False so we don't
    cry wolf."""
    snap = get()
    live = snap.get("rev")
    out: dict[str, Any] = {
        "live_rev": live,
        "session_rev": session_rev,
        "live_known": bool(live),
        "error": snap.get("error"),
        "fetched_at": snap.get("fetched_at"),
    }
    if not live or not session_rev:
        out["drift"] = False
        return out
    out["drift"] = (str(live).strip() != str(session_rev).strip())
    return out
