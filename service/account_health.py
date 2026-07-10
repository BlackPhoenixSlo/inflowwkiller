"""service/account_health.py — per-account OF session health.

When OF rejects an account's stored session with "Wrong user." (the creator
got logged out / the login was re-linked elsewhere), EVERY authenticated call
for that account fails until a human re-captures the session. Letting the
automations keep firing against a dead session burns run slots, OF-proxy
round-trips and LLM spend for zero delivery — so the account is paused
wholesale instead:

  • `mark_session_dead()` flags the account. Called from the executor's
    run_once failure path (an automation hit the signature) and from
    server._persist_unhandled_errors (a UI-originated OF call hit it).
  • `automation_executor.run_once` SKIPS every run for a flagged account —
    pending jobs stay parked and resume untouched once the flag clears.
  • The executor probes each flagged account every _SESSION_PROBE_INTERVAL_S
    with a fresh-from-disk `client.me()`; the first success clears the flag.
  • A session re-capture (`POST /admin/session/bootstrap`) clears the flag
    immediately — no waiting for the next probe.

Kept in its own tiny module so BOTH server.py and automation_executor.py can
import it without a circular import (same reasoning as automation_registry.py).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import or_, select, update

from db.engine import get_session
from db.models import AccountHealth

log = logging.getLogger("of-relay.account-health")


def is_session_dead_error(exc: BaseException) -> str | None:
    """Classify `exc`: a short reason string when it definitely means "this
    account has no working session", else None.

    Deliberately STRICT — only two signatures flag an account:
      • OF's unlink rejection: HTTP 400 +
        `{"error":{"code":301,"message":"Wrong user."}}` → "wrong_user"
      • `OFClient.from_account`'s FileNotFoundError ("has no captured
        session") — the session dir is missing entirely, e.g. the account
        was deleted or never captured on this host → "no_session"
    Transient 429s / 5xx / signing hiccups must never pause a healthy account;
    they already have retry paths of their own."""
    from of_client import OFAPIError  # lazy — keep this module import-light

    if isinstance(exc, OFAPIError) and "Wrong user" in str(exc):
        return "wrong_user"
    if isinstance(exc, FileNotFoundError) and "no captured session" in str(exc):
        return "no_session"
    return None


async def is_session_dead(account_id: str) -> bool:
    """True while the account's session is flagged dead (automations pause)."""
    async with get_session() as s:
        dead_at = (await s.execute(
            select(AccountHealth.session_dead_at)
            .where(AccountHealth.account_id == account_id)
        )).scalar_one_or_none()
    return dead_at is not None


async def mark_session_dead(account_id: str, reason: str) -> bool:
    """Flag the account's session as dead. Returns True only when this call
    NEWLY flagged it (was healthy before) so callers can log the transition
    once instead of on every subsequent failure."""
    now = datetime.utcnow()
    async with get_session() as s:
        row = (await s.execute(
            select(AccountHealth).where(AccountHealth.account_id == account_id)
        )).scalar_one_or_none()
        if row is None:
            s.add(AccountHealth(
                account_id=account_id,
                session_dead_at=now,
                session_dead_reason=reason,
            ))
            return True
        newly = row.session_dead_at is None
        if newly:
            row.session_dead_at = now
            row.last_probe_at = None
        row.session_dead_reason = reason
    return newly


async def clear_session_dead(account_id: str) -> bool:
    """Unflag the account (session verified working again). Returns True if
    it was flagged."""
    async with get_session() as s:
        row = (await s.execute(
            select(AccountHealth).where(AccountHealth.account_id == account_id)
        )).scalar_one_or_none()
        if row is None or row.session_dead_at is None:
            return False
        row.session_dead_at = None
        row.session_dead_reason = None
        row.last_probe_at = None
    return True


async def dead_session_map() -> dict[str, str]:
    """account_id → ISO timestamp it was flagged, for every flagged account.
    The /admin/accounts listing stamps this onto its rows."""
    async with get_session() as s:
        rows = (await s.execute(
            select(AccountHealth.account_id, AccountHealth.session_dead_at)
            .where(AccountHealth.session_dead_at.isnot(None))
        )).all()
    return {aid: dead_at.isoformat() + "Z" for aid, dead_at in rows}


async def due_probe_ids(interval_s: int) -> list[str]:
    """Flagged accounts whose last recovery probe is older than `interval_s`
    (or that were never probed) — the executor's probe sweep input."""
    cutoff = datetime.utcnow() - timedelta(seconds=interval_s)
    async with get_session() as s:
        rows = (await s.execute(
            select(AccountHealth.account_id).where(
                AccountHealth.session_dead_at.isnot(None),
                or_(
                    AccountHealth.last_probe_at.is_(None),
                    AccountHealth.last_probe_at <= cutoff,
                ),
            )
        )).scalars().all()
    return list(rows)


async def stamp_probe(account_id: str) -> None:
    """Record that a recovery probe ran now (rate-limits the next one)."""
    async with get_session() as s:
        await s.execute(
            update(AccountHealth)
            .where(AccountHealth.account_id == account_id)
            .values(last_probe_at=datetime.utcnow())
        )
