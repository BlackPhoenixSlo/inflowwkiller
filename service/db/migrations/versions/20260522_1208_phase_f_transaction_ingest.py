"""phase_f_transaction_ingest

Revision ID: 0013_phase_f_transaction_ingest
Revises: 0012_phase_d_stats
Create Date: 2026-05-22

Schema extensions for the Phase F transaction-ingest job (OF
`/api2/v2/payouts/transactions` ledger poller). Adds 9 new columns to
`transactions`, the partial-unique provider-id index, a status/time index,
and a per-account watermark table.

New columns on `transactions`:

  • `provider_transaction_id` String(64), nullable — OF's hash id (e.g.
    "59851b818e60358f25c05aafc51ec96d"). NULL on WS-pump rows so the
    partial unique index doesn't collide them.
  • `status` String(24), NOT NULL, server_default='cleared' —
    loading / cleared / failed / chargedback / refunded / unknown
    pass-through. Existing rows backfill to 'cleared' so the stats
    `WHERE status='cleared'` filter doesn't drop history overnight.
  • `cleared_at` DateTime, nullable — stamped on transition into cleared.
  • `payout_pending_days` Integer, nullable — OF's payoutPendingDays (5).
  • `net_cents`, `fee_cents`, `vat_cents` Integer, nullable — financial
    breakdown from the ledger row.
  • `description` Text, nullable — HTML-stripped OF description, source
    for the kind classifier + debug surface (cheaper than re-parsing
    raw_json per render).
  • `source` String(8), NOT NULL, server_default='ws' — 'ws' for the
    transcoder-written rows, 'ledger' for ingest-written rows.

Status soft-string contract:
  Status is stored as a free string with NO CHECK constraint. Unknown OF
  values (`refunded`, `reversed`, `pending_verification`, …) land
  verbatim. The contract is in the stats filters, not the schema:
  `WHERE status = 'cleared'` — anything else is excluded from revenue
  rollups. This lets new OF status strings ship without a schema change.

Partial-unique-index gotcha (load-bearing for upserts):
  `uq_tx_provider_id` is a partial unique index
  `WHERE provider_transaction_id IS NOT NULL`. SQLAlchemy's
  `on_conflict_do_update` against this index MUST pass
  `index_where=text("provider_transaction_id IS NOT NULL")` or the upsert
  silently no-ops on conflict. Verified by the 5-line repro in the
  Phase F terminal prompt (pre-flight gate §10.3).

`source='ws'` backfill:
  ALTER ADD COLUMN with a server_default backfills existing rows on
  SQLite, but we follow up with an explicit UPDATE belt-and-suspenders
  so the post-condition holds even if a different engine ever runs this.

SQLite quirk:
  All 9 columns are added inside one `batch_alter_table` block so
  Alembic emits a single table-rebuild (move data, drop, rename) rather
  than nine. Same template as 20260521_0002_wall_media_scan.py — see
  also the unsend-columns migration (20260522_0148_b5_unsend_columns.py)
  which uses the same idiom.

Indexes:
  • uq_tx_provider_id  — partial unique on (account_id, provider_transaction_id)
                          WHERE provider_transaction_id IS NOT NULL.
                          Enables `ON CONFLICT DO UPDATE` for `loading→cleared`
                          status flips on re-poll.
  • ix_tx_status_occurred — (status, occurred_at) — leading column is
                            `status` (not account_id) because the new
                            pending_q on `/admin/stats/per-model` filters
                            `status != 'cleared'` WITHOUT an account_id
                            equality (per R1 perf review §1.3). An
                            (account_id, …) index would have been an
                            index-skip for that cross-account aggregate.

New table `transaction_scan_history` mirrors WallScanState — per-account
watermark + backfill bookkeeping for the supervisor loop.
"""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0013_phase_f_transaction_ingest"
down_revision: str | Sequence[str] | None = "0012_phase_d_stats"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Pasted verbatim from 0012_phase_d_stats — we drop the view before the
# batch_alter_table rebuild and recreate it here so 0013 stays self-
# contained. 0014_phase_f_attr_view_widen replaces this body with the
# widened one.
_OLD_VIEW_SQL = """
CREATE VIEW per_employee_revenue_with_attribution AS
WITH message_linked AS (
  SELECT
    m.sent_by_employee_id,
    m.account_id,
    t.kind,
    t.amount_cents,
    t.occurred_at
  FROM transactions t
  INNER JOIN messages m
    ON m.account_id = t.account_id
   AND m.message_id = t.message_id
  WHERE t.message_id IS NOT NULL
    AND t.kind IN ('ppv', 'tip')
),
standalone_tips_ranked AS (
  SELECT
    t.id AS tx_id,
    t.account_id,
    t.amount_cents,
    t.kind,
    t.occurred_at,
    m.sent_by_employee_id,
    ROW_NUMBER() OVER (
      PARTITION BY t.id
      ORDER BY m.created_at DESC
    ) AS rn
  FROM transactions t
  LEFT JOIN messages m
    ON m.account_id = t.account_id
   AND m.fan_id = t.fan_id
   AND m.direction = 'out'
   AND m.sent_by_employee_id IS NOT NULL
   AND m.created_at <= t.occurred_at
   AND m.created_at > datetime(t.occurred_at, '-7 days')
  WHERE t.kind = 'tip' AND t.message_id IS NULL
)
SELECT sent_by_employee_id, account_id, kind, amount_cents, occurred_at
  FROM message_linked
UNION ALL
SELECT sent_by_employee_id, account_id, kind, amount_cents, occurred_at
  FROM standalone_tips_ranked
 WHERE rn = 1
"""


def upgrade() -> None:
    # SQLite's batch_alter_table rebuilds the `transactions` table
    # (CREATE _alembic_tmp_transactions → copy rows → DROP transactions
    # → RENAME). The 0012 attribution view binds `transactions` by name,
    # so the rebuild fails: `error in view ...: no such table: main.transactions`.
    # Drop the view first; recreate it (old body) at the end. 0014 will
    # then DROP+CREATE it with the widened body.
    op.execute(sa.text("DROP VIEW IF EXISTS per_employee_revenue_with_attribution"))

    # 2a — Add 9 columns in ONE batch_alter_table so SQLite emits a single
    # table rebuild rather than nine.
    with op.batch_alter_table("transactions") as batch:
        batch.add_column(sa.Column("provider_transaction_id", sa.String(64), nullable=True))
        batch.add_column(
            sa.Column(
                "status",
                sa.String(24),
                nullable=False,
                server_default=sa.text("'cleared'"),
            )
        )
        batch.add_column(sa.Column("cleared_at", sa.DateTime, nullable=True))
        batch.add_column(sa.Column("payout_pending_days", sa.Integer, nullable=True))
        batch.add_column(sa.Column("net_cents", sa.Integer, nullable=True))
        batch.add_column(sa.Column("fee_cents", sa.Integer, nullable=True))
        batch.add_column(sa.Column("vat_cents", sa.Integer, nullable=True))
        batch.add_column(sa.Column("description", sa.Text, nullable=True))
        batch.add_column(
            sa.Column(
                "source",
                sa.String(8),
                nullable=False,
                server_default=sa.text("'ws'"),
            )
        )

    # 2b — Belt-and-suspenders backfill. SQLite ALTER ADD COLUMN with a
    # default backfills all rows, but make it explicit so any other engine
    # that ever runs this gets the same post-condition.
    op.execute(sa.text("UPDATE transactions SET source='ws' WHERE source IS NULL"))
    op.execute(sa.text("UPDATE transactions SET status='cleared' WHERE status IS NULL"))

    # 2c — Two new indexes. Raw SQL because Alembic's create_index doesn't
    # always emit the `WHERE` clause cleanly on SQLite.
    op.execute(
        sa.text(
            """
            CREATE UNIQUE INDEX uq_tx_provider_id
              ON transactions(account_id, provider_transaction_id)
              WHERE provider_transaction_id IS NOT NULL
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE INDEX ix_tx_status_occurred
              ON transactions(status, occurred_at)
            """
        )
    )

    # 2d — Watermark table. Mirrors WallScanState (models.py:1165-1186)
    # with the extra fields for backfill/error/pause bookkeeping.
    op.create_table(
        "transaction_scan_history",
        sa.Column("account_id", sa.String, nullable=False),
        sa.Column("last_scan_at", sa.DateTime, nullable=True),
        sa.Column("last_marker", sa.Integer, nullable=True),
        sa.Column("oldest_seen_occurred_at", sa.DateTime, nullable=True),
        sa.Column("newest_seen_occurred_at", sa.DateTime, nullable=True),
        sa.Column(
            "fully_backfilled",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("backfill_floor", sa.DateTime, nullable=True),
        sa.Column(
            "current_status",
            sa.String,
            nullable=False,
            server_default=sa.text("'idle'"),
        ),
        sa.Column("last_error", sa.String, nullable=True),
        sa.Column(
            "consecutive_failures",
            sa.Integer,
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("paused_until", sa.DateTime, nullable=True),
        sa.Column(
            "rows_inserted_total",
            sa.Integer,
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "rows_patched_total",
            sa.Integer,
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime,
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint("account_id", name="pk_transaction_scan_history"),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["accounts.id"],
            ondelete="CASCADE",
            name="fk_transaction_scan_history_account_id_accounts",
        ),
    )

    # Recreate the view (old body) so 0013 leaves the schema fully
    # functional. 0014 will replace this with the widened body.
    op.execute(sa.text(_OLD_VIEW_SQL))


def downgrade() -> None:
    # Drop the view first — the batch_alter rebuild below would fail
    # with `error in view ...: no such table: main.transactions`
    # otherwise.
    op.execute(sa.text("DROP VIEW IF EXISTS per_employee_revenue_with_attribution"))

    # Reverse order: scan history table, then indexes, then columns.
    op.drop_table("transaction_scan_history")
    op.execute(sa.text("DROP INDEX IF EXISTS ix_tx_status_occurred"))
    op.execute(sa.text("DROP INDEX IF EXISTS uq_tx_provider_id"))

    # Drop 9 columns in REVERSE order added (source first,
    # provider_transaction_id last) inside one batch.
    with op.batch_alter_table("transactions") as batch:
        batch.drop_column("source")
        batch.drop_column("description")
        batch.drop_column("vat_cents")
        batch.drop_column("fee_cents")
        batch.drop_column("net_cents")
        batch.drop_column("payout_pending_days")
        batch.drop_column("cleared_at")
        batch.drop_column("status")
        batch.drop_column("provider_transaction_id")

    # Recreate the view so the post-downgrade state matches 0012's HEAD.
    op.execute(sa.text(_OLD_VIEW_SQL))
