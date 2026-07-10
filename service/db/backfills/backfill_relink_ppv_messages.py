"""backfill_relink_ppv_messages — one-shot relink of orphaned chat-PPV sales.

Retries the payment→message link (transaction_ingest._link_ppv_message rules:
exact price + fan + 30-day window, UNAMBIGUOUS candidates only) for every
`kind='ppv_message'` transaction whose `message_id` is NULL. Once linked, the
sale credits the message's own sender ("Sent by …") in the per-employee view.

ATTRIBUTION-ONLY WRITES: runs the linker with `stamp_paid=False` — it writes
ONLY `transactions.message_id` and never mutates the message row, so this bulk
pass has zero downstream side effects (funnel step-advance, bought-media
dedupe, and the ai_chatter offer watcher all read `messages.is_paid`, which we
leave alone). Full rollback = one UPDATE nulling the journaled message_ids.

Fill-only + idempotent: only message-NULL rows are enumerated; existing links
and manual `attributed_employee_id` overrides are untouchable by construction.
Traverses ALL matching history via keyset pagination (occurred_at ASC, id
tiebreak) in chunks — never "first N only". Each row commits in its own
session, so the write lock stays short against a live relay.

Because this writes a money-attribution field it defaults to a DRY RUN — a
genuinely read-only report of what it WOULD link. Pass --apply to commit.

Also prints, read-only:
  • residue report — remaining orphans by kind × account × age bucket
    (ppv_post/ppv_stream have no chat message and are NEVER auto-linkable;
    they surface in the stats Unattributed bucket + the manual panel);
  • suspect-links report — ALREADY-linked ppv_message rows whose message is
    >24h older than the sale while a closer same-price message exists (the
    known misattribution signature). Review by hand; nothing is rewritten.

Run (from the `service/` dir):
    python -m db.backfills.backfill_relink_ppv_messages                # dry run
    python -m db.backfills.backfill_relink_ppv_messages --apply        # commit
    python -m db.backfills.backfill_relink_ppv_messages --account 123 --since-days 90
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from datetime import datetime, timedelta

from sqlalchemy import select, text

from db.engine import get_session
from db.models import Employee, Message
from transaction_ingest import relink_orphan_ppvs

log = logging.getLogger("backfill_relink_ppv_messages")

_CHUNK = 200


async def _credit_summary(journal: list[dict]) -> dict[str, int]:
    """Per-employee display-name → linked-sale count for the report."""
    if not journal:
        return {}
    msg_ids = [j["message_id"] for j in journal]
    async with get_session() as s:
        rows = (await s.execute(
            select(Message.message_id, Employee.display_name)
            .join(Employee, Employee.id == Message.sent_by_employee_id, isouter=True)
            .where(Message.message_id.in_(msg_ids))
        )).all()
    names: dict[int, str] = {mid: (name or "<no sender>") for mid, name in rows}
    out: dict[str, int] = {}
    for j in journal:
        label = names.get(j["message_id"], "<no sender>")
        out[label] = out.get(label, 0) + 1
    return out


async def _residue_report(account_id: str | None) -> list[tuple]:
    sql = """
        SELECT kind, account_id,
               CASE WHEN occurred_at >= datetime('now', '-7 days')  THEN '<7d'
                    WHEN occurred_at >= datetime('now', '-30 days') THEN '7-30d'
                    ELSE '>30d' END AS age,
               COUNT(*) AS n, SUM(amount_cents)/100.0 AS usd
          FROM transactions
         WHERE kind LIKE 'ppv%' AND message_id IS NULL
           AND status IN ('cleared', 'pending')
    """
    if account_id:
        sql += " AND account_id = :aid"
    sql += " GROUP BY kind, account_id, age ORDER BY n DESC"
    async with get_session() as s:
        return list((await s.execute(
            text(sql), {"aid": account_id} if account_id else {}
        )).all())


async def _suspect_links_report(account_id: str | None) -> list[tuple]:
    """ALREADY-linked ppv_message rows where the linked message is >24h older
    than the sale AND a closer same-price outbound message exists — the
    signature of the old linker matching a stale message. Read-only."""
    sql = """
        SELECT t.id, t.account_id, t.fan_id, t.amount_cents/100.0 AS usd,
               t.occurred_at, t.message_id AS linked_msg, m.created_at AS linked_at
          FROM transactions t
          JOIN messages m ON m.account_id = t.account_id
                         AND m.fan_id = t.fan_id AND m.message_id = t.message_id
         WHERE t.kind = 'ppv_message'
           AND t.status IN ('cleared', 'pending')
           AND m.created_at < datetime(t.occurred_at, '-1 day')
           AND EXISTS (
             SELECT 1 FROM messages m2
              WHERE m2.account_id = t.account_id AND m2.fan_id = t.fan_id
                AND m2.direction = 'out' AND m2.price_cents = t.amount_cents
                AND m2.message_id != t.message_id
                AND m2.created_at > m.created_at
                AND m2.created_at <= t.occurred_at)
    """
    if account_id:
        sql += " AND t.account_id = :aid"
    sql += " ORDER BY t.occurred_at DESC LIMIT 50"
    async with get_session() as s:
        return list((await s.execute(
            text(sql), {"aid": account_id} if account_id else {}
        )).all())


async def run(*, apply: bool, account_id: str | None, since_days: int | None) -> dict:
    since = (
        datetime.utcnow() - timedelta(days=since_days)
        if since_days is not None else None
    )
    totals = {
        "examined": 0, "linked": 0, "ambiguous_skipped": 0,
        "no_candidate": 0, "conflict_skipped": 0,
    }
    journal: list[dict] = []
    after = None
    while True:
        stats = await relink_orphan_ppvs(
            account_id=account_id,
            since=since,
            dry_run=not apply,
            limit=_CHUNK,
            newest_first=False,
            after=after,
            stamp_paid=False,
        )
        for k in totals:
            totals[k] += stats[k]
        journal.extend(stats["journal"])
        after = stats["next_after"]
        if after is None:
            break

    verb = "linked" if apply else "WOULD link"
    log.info("")
    log.info("=== ppv relink %s ===", "APPLIED" if apply else "DRY RUN")
    log.info("account:            %s", account_id or "ALL")
    log.info("since:              %s", since.isoformat() if since else "all history")
    log.info("examined:           %d", totals["examined"])
    log.info("%s:         %d", verb, totals["linked"])
    log.info("ambiguous (manual): %d", totals["ambiguous_skipped"])
    log.info("no candidate:       %d", totals["no_candidate"])
    log.info("conflicts skipped:  %d", totals["conflict_skipped"])

    credit = await _credit_summary(journal)
    if credit:
        log.info("--- credit summary (message sender) ---")
        for name, n in sorted(credit.items(), key=lambda kv: -kv[1]):
            log.info("  %-24s %d", name, n)

    # Journal — the rollback audit trail. One JSON line per applied link:
    # reversal is UPDATE transactions SET message_id=NULL WHERE id IN (tx_ids).
    if journal:
        log.info("--- journal (%s) ---", "applied" if apply else "planned")
        for j in journal:
            log.info("JOURNAL %s", json.dumps(j))

    residue = await _residue_report(account_id)
    if residue:
        log.info("--- residue: still-orphaned ppv* rows (kind × account × age) ---")
        for kind, aid, age, n, usd in residue:
            log.info("  %-12s %-14s %-6s n=%-5d $%.2f", kind, aid, age, n, usd or 0)

    suspects = await _suspect_links_report(account_id)
    if suspects:
        log.info("--- suspect existing links (stale-message signature, top 50) ---")
        for row in suspects:
            log.info(
                "  tx=%d acct=%s fan=%s $%.2f occurred=%s linked_msg=%s sent=%s",
                row.id, row.account_id, row.fan_id, row.usd,
                row.occurred_at, row.linked_msg, row.linked_at,
            )
        log.info("  (review by hand — reassign via the stats panel; nothing auto-rewritten)")

    return {**totals, "journal_entries": len(journal)}


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--apply", action="store_true",
                   help="commit links (default: dry run, read-only)")
    p.add_argument("--account", default=None, help="scope to one account id")
    p.add_argument("--since-days", type=int, default=None,
                   help="only transactions newer than N days (default: all history)")
    args = p.parse_args()
    asyncio.run(run(
        apply=args.apply, account_id=args.account, since_days=args.since_days,
    ))


if __name__ == "__main__":
    main()
