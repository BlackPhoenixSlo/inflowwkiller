"""
vault_cache.py — shared TTL cache for OF vault listing responses.

Every employee on the same OF account sees the same vault, so caching
the OF response keyed on (account_id, query_key) lets the second
picker-open (from any browser) skip the OF round-trip entirely.

API:
    await get(account_id, query_key)           -> dict | None  (None = miss/stale)
    await put(account_id, query_key, payload)  -> None         (write-through)
    await invalidate(account_id)               -> int          (rows deleted)

The TTL is read from `CHATTERLY_VAULT_CACHE_TTL_SECONDS` (default 86400
= 1 day). Vault content changes are creator-driven and all mutation
paths (/api/of/v2/upload, vault list/folder edits) call
`invalidate(account_id)` so the cache cannot serve a stale row after a
write the relay performed. External edits (e.g. the creator deleting
media in OF's own UI) age out via the TTL — worst case the picker
shows a ghost row for up to 1 day until either an upload invalidates
or the user hits Refresh.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from db.engine import get_session
from db.models import VaultResponseCache

log = logging.getLogger("of-relay.vault_cache")


def _ttl_seconds() -> int:
    raw = os.environ.get("CHATTERLY_VAULT_CACHE_TTL_SECONDS", "86400")
    try:
        v = int(raw)
        return v if v > 0 else 0
    except ValueError:
        return 86400


async def get(account_id: str, query_key: str) -> dict[str, Any] | None:
    """Return the cached response for (account_id, query_key) if it is
    within TTL. Returns None on miss or stale row. Stale rows are left in
    place — `put` overwrites them on the next upstream success."""
    ttl = _ttl_seconds()
    if ttl == 0:
        return None
    cutoff = datetime.utcnow() - timedelta(seconds=ttl)
    async with get_session() as s:
        row = (
            await s.execute(
                select(VaultResponseCache)
                .where(
                    VaultResponseCache.account_id == account_id,
                    VaultResponseCache.query_key == query_key,
                    VaultResponseCache.fetched_at >= cutoff,
                )
            )
        ).scalar_one_or_none()
    if row is None:
        return None
    try:
        return json.loads(row.response_json)
    except (ValueError, TypeError):
        # Corrupt blob — treat as miss and let put() overwrite it.
        log.warning("vault_cache: corrupt blob for account=%s key=%s", account_id, query_key)
        return None


async def put(account_id: str, query_key: str, payload: Any) -> None:
    """Write-through cache. Upserts on the composite PK so re-fetching
    the same page just refreshes `fetched_at`."""
    try:
        blob = json.dumps(payload, ensure_ascii=False, default=str)
    except (TypeError, ValueError) as e:
        # Don't break the upstream response just because we couldn't cache it.
        log.warning("vault_cache: payload not serializable (%s) — skipping write", e)
        return
    now = datetime.utcnow()
    stmt = sqlite_insert(VaultResponseCache).values(
        account_id=account_id,
        query_key=query_key,
        response_json=blob,
        fetched_at=now,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["account_id", "query_key"],
        set_={"response_json": blob, "fetched_at": now},
    )
    async with get_session() as s:
        await s.execute(stmt)


async def invalidate(account_id: str) -> int:
    """Drop every cached row for an account. Called after any mutation
    the relay mediates (upload, delete, folder edit) so the next read
    goes upstream and re-warms the cache with fresh data."""
    async with get_session() as s:
        result = await s.execute(
            delete(VaultResponseCache).where(VaultResponseCache.account_id == account_id)
        )
        return result.rowcount or 0
