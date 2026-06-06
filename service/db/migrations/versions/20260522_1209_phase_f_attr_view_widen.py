"""phase_f_attr_view_widen

Revision ID: 0014_phase_f_attr_view_widen
Revises: 0013_phase_f_transaction_ingest
Create Date: 2026-05-22

Widens `per_employee_revenue_with_attribution` (originally defined by
0012_phase_d_stats) so the Phase F ingest's richer `kind` vocabulary
(`ppv_message`, `ppv_post`, `ppv_stream`, `tip_post`, `tip_stream`,
`custom`) flows into per-employee revenue rollups, and adds
`uq_tx_msg` — a partial unique index on
`(account_id, fan_id, message_id) WHERE message_id IS NOT NULL` that
is the cross-writer dedup target for the WS pump and the Phase F PPV
writer.

Why a SEPARATE migration (not folded into 0013):
  0013's schema columns are independently useful + lower-risk. 0014 is a
  pure-DDL view rewrite + one partial-unique-index add. If 0014 fails
  (e.g. a view-body typo), 0013's columns stay intact. Per
  library/db_data/13_convergence_plan.md §10.3.

⚠ Without this migration, every Phase F PPV ledger row falls outside the
  attribution view (0012 filters `kind IN ('ppv', 'tip')` only).
  Per-employee revenue would silently go to ~0 for PPV the day Phase F
  ships. This is the §6 failure mode in
  library/db_data/13_phase_f_da_review.md.

Status filter:
  Both CTEs now include `t.status = 'cleared'` so loading / refunded /
  chargedback rows do not inflate per-employee revenue. Pending revenue
  surfaces via a separate `pending_revenue_cents` field on the stats
  endpoint, not the attribution view.

uq_tx_msg index — dedup target for two writers:
  • WS pump (after Planner B's PR): ON CONFLICT DO NOTHING on insert.
  • Phase F PPV-row writer: ON CONFLICT DO UPDATE to promote
    kind='ppv_pending' → 'ppv_message' and fill financial fields.
  ⚠ `kind` is deliberately NOT in the unique key. A single OF
  message_id maps to at most one transaction (a message is either a tip
  or a PPV, never both, and an unlock references the original PPV's
  message_id). Including `kind` would let WS's `ppv_pending` rows fail
  to collide with the Phase F writer's later `ppv_message` rows →
  double-count.

SELECT shape unchanged: the new view returns the same five columns
(sent_by_employee_id, account_id, kind, amount_cents, occurred_at) so
`service/stats.py` literal_column proxies need no edit.
"""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0014_phase_f_attr_view_widen"
down_revision: str | Sequence[str] | None = "0013_phase_f_transaction_ingest"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_NEW_VIEW_SQL = """
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
    AND t.kind IN (
      'ppv', 'ppv_message', 'ppv_post', 'ppv_stream',
      'tip', 'tip_post', 'tip_stream',
      'custom'
    )
    AND t.status = 'cleared'
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
  WHERE t.kind IN ('tip', 'tip_post', 'tip_stream')
    AND t.message_id IS NULL
    AND t.status = 'cleared'
)
SELECT sent_by_employee_id, account_id, kind, amount_cents, occurred_at
  FROM message_linked
UNION ALL
SELECT sent_by_employee_id, account_id, kind, amount_cents, occurred_at
  FROM standalone_tips_ranked
 WHERE rn = 1
"""


# Pasted verbatim from 0012_phase_d_stats so downgrade restores the
# original view definition exactly.
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


_UQ_TX_MSG_SQL = """
CREATE UNIQUE INDEX uq_tx_msg
  ON transactions(account_id, fan_id, message_id)
  WHERE message_id IS NOT NULL
"""


def upgrade() -> None:
    op.execute(sa.text("DROP VIEW IF EXISTS per_employee_revenue_with_attribution"))
    op.execute(sa.text(_NEW_VIEW_SQL))

    op.execute(sa.text("DROP INDEX IF EXISTS uq_tx_msg"))
    op.execute(sa.text(_UQ_TX_MSG_SQL))


def downgrade() -> None:
    op.execute(sa.text("DROP INDEX IF EXISTS uq_tx_msg"))
    op.execute(sa.text("DROP VIEW IF EXISTS per_employee_revenue_with_attribution"))
    op.execute(sa.text(_OLD_VIEW_SQL))
