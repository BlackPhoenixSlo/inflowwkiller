"""backfill_orphan_attribution — one-shot sweep that assigns
`transactions.attributed_employee_id` for every revenue row that currently
lands in the per-employee stats "Unattributed" bucket, using the last-toucher
heuristic in `attribution_backfill`. See that module for the rule.

Idempotent: only touches rows with `attributed_employee_id IS NULL` whose
effective attribution is NULL. Re-running finds nothing new.

Because this writes a money-attribution field, it defaults to a DRY RUN —
it prints exactly what it WOULD change and writes nothing. Pass --apply to
commit. (This --dry-run-by-default is a deliberate deviation from the other,
non-monetary backfills in this dir, which have no flag.)

Run (from the `service/` dir):
    python -m db.backfills.backfill_orphan_attribution              # dry run
    python -m db.backfills.backfill_orphan_attribution --apply      # commit
    python -m db.backfills.backfill_orphan_attribution --apply --lookback-days 14
    python -m db.backfills.backfill_orphan_attribution --account 123456789
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from sqlalchemy import select

from db.engine import get_session
from db.models import Employee
from attribution_backfill import DEFAULT_LOOKBACK_DAYS, attribute_orphans


log = logging.getLogger("backfill_orphan_attribution")


async def _employee_labels(emp_ids: list[int]) -> dict[int, str]:
    if not emp_ids:
        return {}
    async with get_session() as s:
        rows = (await s.execute(
            select(Employee.id, Employee.display_name).where(Employee.id.in_(emp_ids))
        )).all()
    return {eid: name for eid, name in rows}


async def run(*, apply: bool, lookback_days: int, account_id: str | None) -> dict:
    stats = await attribute_orphans(
        account_id=account_id,
        lookback_days=lookback_days,
        dry_run=not apply,
    )
    labels = await _employee_labels(list(stats["by_employee"].keys()))

    verb = "attributed" if apply else "WOULD attribute"
    log.info("")
    log.info("=== orphan attribution %s ===", "APPLIED" if apply else "DRY RUN")
    log.info("account:            %s", account_id or "ALL")
    log.info("rule: last NON-MASS 1:1 chatter (incl. Automation) within %d days", lookback_days)
    log.info("message-linked orphans: %d", stats["orphans_message_linked"])
    log.info("standalone-tip orphans: %d", stats["orphans_standalone_tip"])
    log.info("%s:  %d", verb, stats["attributed"])
    log.info("unresolved (no 1:1 chatter in window, left Unattributed): %d", stats["unresolved"])
    if stats["by_employee"]:
        log.info("--- credited employees ---")
        for eid, n in sorted(stats["by_employee"].items(), key=lambda kv: -kv[1]):
            log.info("  %-6s %-22s %d sale(s)", eid, labels.get(eid, "?"), n)
    if not apply:
        log.info("")
        log.info("dry run — nothing written. Re-run with --apply to commit.")
    return stats


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description="Attribute orphan revenue transactions.")
    ap.add_argument("--apply", action="store_true",
                    help="Commit the writes (default is a dry run).")
    ap.add_argument("--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS,
                    help=f"Last-toucher window in days (default {DEFAULT_LOOKBACK_DAYS}).")
    ap.add_argument("--account", default=None,
                    help="Limit to one account_id (default: all).")
    args = ap.parse_args()
    asyncio.run(run(
        apply=args.apply,
        lookback_days=args.lookback_days,
        account_id=args.account,
    ))


if __name__ == "__main__":
    main()
