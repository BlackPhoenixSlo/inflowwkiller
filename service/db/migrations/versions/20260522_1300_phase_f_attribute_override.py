"""phase_f_attribute_override

Revision ID: 0015_phase_f_attribute_override
Revises: 0014_phase_f_attr_view_widen
Create Date: 2026-05-22

Adds `transactions.attributed_employee_id` and rewrites
`per_employee_revenue_with_attribution` so a manual override beats the
7-day-lookback heuristic.

Motivation:
  Phase F's standalone-tip attribution does a LEFT JOIN against `messages`
  within a 7-day window. When the messages table predates the WS pump's
  history (i.e. the tip occurred before we started capturing outbound
  messages), no chatter can be matched → the tip falls into the
  "Unattributed" bucket. There is no way today for ops to manually fix
  these orphans. This migration unblocks the admin endpoint
  POST /admin/ingest/transactions/{tx_id}/attribute by adding the
  override column the view consults via COALESCE.

Override column:
  Nullable INTEGER FK-shaped (no constraint — employees.id is INTEGER PK
  but we keep it loose so soft-delete of employees doesn't cascade-null
  attribution history). NULL means "no override; use the view's normal
  attribution path".

View change:
  In both CTEs the attributed employee is now
  COALESCE(t.attributed_employee_id, m.sent_by_employee_id).
  • message_linked: override wins over the message's sender.
  • standalone_tips_ranked: override populates orphan tips that found no
    7-day match.

SELECT shape unchanged — the same five columns
(sent_by_employee_id, account_id, kind, amount_cents, occurred_at). No
reader edits required.
"""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0015_phase_f_attribute_override"
down_revision: str | Sequence[str] | None = "0014_phase_f_attr_view_widen"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_NEW_VIEW_SQL = """
CREATE VIEW per_employee_revenue_with_attribution AS
WITH message_linked AS (
  SELECT
    COALESCE(t.attributed_employee_id, m.sent_by_employee_id) AS sent_by_employee_id,
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
    COALESCE(t.attributed_employee_id, m.sent_by_employee_id) AS sent_by_employee_id,
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


def upgrade() -> None:
    with op.batch_alter_table("transactions") as batch:
        batch.add_column(sa.Column("attributed_employee_id", sa.Integer(), nullable=True))

    op.execute(sa.text("DROP VIEW IF EXISTS per_employee_revenue_with_attribution"))
    op.execute(sa.text(_NEW_VIEW_SQL))


def downgrade() -> None:
    op.execute(sa.text("DROP VIEW IF EXISTS per_employee_revenue_with_attribution"))
    op.execute(sa.text(_OLD_VIEW_SQL))

    with op.batch_alter_table("transactions") as batch:
        batch.drop_column("attributed_employee_id")
