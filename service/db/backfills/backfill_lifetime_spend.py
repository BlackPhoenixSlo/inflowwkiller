"""backfill_lifetime_spend — copy legacy `fans.bought_amount` (USD) into
`fans.lifetime_spend_cents` (cents) before migration 0009 drops the legacy
column. See library/db_data/06_gaps_and_migrations.md §3 and §5.

Idempotent: only updates rows where `lifetime_spend_cents = 0 AND
bought_amount > 0`. Re-running after a successful pass is a no-op because
the second pass finds nothing matching the WHERE clause.

Run:
    cd service && python -m db.backfills.backfill_lifetime_spend

Reports the count of rows updated so the operator can confirm before
running the destructive 0009 migration (or learn there's no legacy data
to worry about).
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import text

from db.engine import get_session


log = logging.getLogger("backfill_lifetime_spend")


SQL = text(
    "UPDATE fans "
    "SET lifetime_spend_cents = CAST(bought_amount * 100 AS INTEGER) "
    "WHERE lifetime_spend_cents = 0 AND bought_amount > 0"
)


async def run() -> int:
    async with get_session() as s:
        result = await s.execute(SQL)
        return int(result.rowcount or 0)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    n = asyncio.run(run())
    if n == 0:
        log.info("backfill_lifetime_spend: 0 rows updated (no legacy data to migrate)")
    else:
        log.info("backfill_lifetime_spend: %d rows updated", n)


if __name__ == "__main__":
    main()
