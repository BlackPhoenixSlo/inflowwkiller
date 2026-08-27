"""
Small async repository helpers — the bits of SQL we need now.

This is intentionally NOT a generic ORM facade. Each function here exists
because something in the relay/UI calls it; we add new functions when we
add new callers. Resist the temptation to predict "what else might need
a `get_account_by_proxy_label` someday" — that abstraction can wait until
the second caller appears.

Two access patterns supported:
  • Async (FastAPI handlers, SSE writer, WS pump):
        async with get_session() as s: ...
  • Sync (one-shot tools, the legacy importer):
        from sqlalchemy.orm import Session
        with Session(sync_engine) as s: ...

The async path is preferred everywhere; sync exists only for tools that
can't easily switch (subprocess scripts, ad-hoc REPL).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, date
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from jsonsafe import dump_capped
from .engine import get_session
from .models import (
    Account,
    EventInbox,
    GrokDailyCost,
    Proxy,
    UserAccount,
)

log = logging.getLogger("of-relay.db")


# ── Account ownership ────────────────────────────────────────────────

async def ensure_user_account_link(user_id: str, account_id: str) -> None:
    """Give a freshly captured OF account its owner. Idempotent.

    The caller MUST have synced the registry into SQL first — `accounts` is
    this row's parent and `PRAGMA foreign_keys=ON` is wired at connect, so
    linking ahead of the import is a FOREIGN KEY violation, not a duplicate.
    The bootstrap path used to fire the import and this link as two racing
    tasks and swallow the resulting IntegrityError as though it could only
    mean "already linked" — which is how a captured account reached HTTP 200
    owned by nobody (2026-08-22: blake, blake and ACCOUNT_ID all landed that
    way). Ordering the two is the fix; this function stays a plain insert and
    deliberately does NOT create the parent, because inventing an `accounts`
    row the importer never wrote is how you get a DB account with no registry
    folder — see the 21-vs-6 divergence in scripts/purge_stale.py.

    Anything that goes wrong here raises. Silence is the bug this replaces.
    """
    async with get_session() as s:
        if await s.get(UserAccount, (user_id, account_id)) is not None:
            return
        s.add(UserAccount(user_id=user_id, account_id=account_id))


# ── Event inbox ──────────────────────────────────────────────────────

async def insert_event(
    *,
    account_id: str | None,
    source: str,
    event_type: str,
    payload: dict[str, Any],
    provider_event_id: str | None = None,
) -> int | None:
    """Persist one raw event. Idempotent on (source, provider_event_id)
    when that pair is set — re-deliveries return the existing row's id
    instead of creating a duplicate.

    Returns the row's id, or None if the write failed (always logged).

    Fire-and-forget intended: callers do NOT await the result on the WS
    pump's hot path; they await on `asyncio.create_task(insert_event(...))`
    so a slow SQL write can never stall event broadcast.
    """
    try:
        async with get_session() as s:
            # Ensure parent accounts row exists when account_id is set.
            # An account can be set up on disk (sessions/accounts/<id>/) +
            # bound to a proxy in proxies.json without ever being inserted
            # into the accounts table — startup events for such an account
            # would FK-violate. Idempotent ON CONFLICT no-op; same pattern
            # used by the wall_media writer at server.py:4392.
            if account_id:
                await s.execute(
                    sqlite_insert(Account)
                    .values(id=account_id, is_active_default=False)
                    .on_conflict_do_nothing(index_elements=["id"])
                )
            # SQLite path uses ON CONFLICT for the partial unique index.
            # We don't have a "RETURNING id" until SQLAlchemy 2.x core
            # supports it on SQLite reliably; we accept a follow-up SELECT
            # on the conflict path because dedup is rare.
            stmt = sqlite_insert(EventInbox).values(
                account_id=account_id,
                source=source,
                provider_event_id=provider_event_id,
                event_type=event_type,
                payload_json=dump_capped(payload),
            )
            if provider_event_id:
                # Skip insert when a duplicate hits the partial unique idx.
                stmt = stmt.on_conflict_do_nothing(
                    index_elements=["source", "provider_event_id"],
                )
            result = await s.execute(stmt)
            return result.lastrowid
    except Exception:
        log.exception("insert_event failed (source=%s type=%s)", source, event_type)
        return None


async def mark_event_processed(
    event_id: int, *, status: str = "ok", error: str | None = None,
) -> None:
    """Mark a row processed. Called by the worker that drains event_inbox
    into canonical tables (chats / messages / fans / transactions). Phase A
    doesn't ship that worker — but the column is filled by manual hands too
    (debug tools, dry-run scripts)."""
    try:
        async with get_session() as s:
            await s.execute(
                update(EventInbox)
                .where(EventInbox.id == event_id)
                .values(
                    processed_at=datetime.utcnow(),
                    processed_status=status,
                    processed_error=error,
                )
            )
    except Exception:
        log.exception("mark_event_processed failed (id=%s)", event_id)


# ── Account / proxy reads ────────────────────────────────────────────
# Sparse on purpose. accounts.py + proxies.py are still the source of truth
# for writes in phase A; these reads are for the new code paths (SSE
# subscriber count by account, drift banner queries, etc.) that prefer
# typed rows over JSON files.

async def list_active_account_ids() -> list[str]:
    """All accounts that have a session. Powers the per-pump supervisor's
    SQL-side cross-check (phase B)."""
    async with get_session() as s:
        result = await s.execute(select(Account.id))
        return [r[0] for r in result.all()]


async def get_account_row(account_id: str) -> Account | None:
    async with get_session() as s:
        return await s.get(Account, account_id)


async def get_proxy_by_label(label: str) -> Proxy | None:
    async with get_session() as s:
        return await s.get(Proxy, label)


# ── Grok cost rollup (used by the soft-cap gate in service/ai/grok.py) ──

async def add_to_daily_cost(cost_cents: int, *, day: date | None = None) -> int:
    """Increment today's Grok cost. Returns the new total."""
    day_str = (day or date.today()).isoformat()
    async with get_session() as s:
        # Upsert: try insert; on conflict bump the column.
        stmt = sqlite_insert(GrokDailyCost).values(
            day=day_str,
            cost_cents=cost_cents,
            call_count=1,
        ).on_conflict_do_update(
            index_elements=["day"],
            set_={
                "cost_cents": GrokDailyCost.cost_cents + cost_cents,
                "call_count": GrokDailyCost.call_count + 1,
                "updated_at": datetime.utcnow(),
            },
        )
        await s.execute(stmt)
        # Re-read for the caller (single round trip in WAL mode).
        row = await s.get(GrokDailyCost, day_str)
        return row.cost_cents if row else cost_cents


async def get_daily_cost(day: date | None = None) -> int:
    """Today's Grok spend in cents. 0 if no calls yet."""
    day_str = (day or date.today()).isoformat()
    async with get_session() as s:
        row = await s.get(GrokDailyCost, day_str)
        return row.cost_cents if row else 0


# ── JSON ↔ SQL sync ─────────────────────────────────────────────────
#
# Phase A keeps the legacy JSON files (accounts.py / proxies.py / session
# bootstrapping) as the *write source of truth* — we don't refactor those
# to SQL yet because the existing UI reads from them. To bring SQL in line
# with whatever the JSON files say, we call `sync_from_disk()` after every
# mutating admin endpoint.
#
# Implementation: re-runs the legacy importer's logic. The importer is
# already idempotent so calling it on every save is correct — and cheap,
# since you have 2 accounts + 3 proxies. A future Phase B will reverse the
# direction (SQL writes → JSON shims for back-compat) and eventually drop
# the JSON files entirely.

async def sync_from_disk() -> dict[str, int]:
    """Re-run the legacy → SQL importer. Idempotent. Fire-and-forget safe.

    Runs the importer's `import_proxies` + `import_accounts_and_sessions`
    in a thread so the SQLAlchemy sync engine doesn't block the event
    loop. Returns counts for log/debug.
    """
    import asyncio
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session as SyncSession

    from .engine import DATABASE_URL
    from .import_legacy import import_proxies, import_accounts_and_sessions

    def _run() -> dict[str, int]:
        sync_url = DATABASE_URL.replace("+aiosqlite", "").replace("+asyncpg", "")
        eng = create_engine(sync_url)
        try:
            with SyncSession(eng) as s:
                proxies_n = import_proxies(s, dry_run=False)
                accounts_n, sessions_n = import_accounts_and_sessions(s, dry_run=False)
                s.commit()
                return {"proxies": proxies_n, "accounts": accounts_n, "sessions": sessions_n}
        finally:
            eng.dispose()

    try:
        return await asyncio.get_running_loop().run_in_executor(None, _run)
    except Exception:
        log.exception("sync_from_disk failed")
        return {"proxies": -1, "accounts": -1, "sessions": -1}
