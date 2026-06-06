"""phase_d_stats

Revision ID: 0012_phase_d_stats
Revises: 0011_phase_c_automation_employee
Create Date: 2026-05-22

Stats-dashboard plumbing:

  • `per_employee_revenue_with_attribution` view — one place that resolves
    the three tip-attribution cases (PPV+message, tip+message, standalone
    tip via 7-day lookback to the most recent outbound). Reused by
    /admin/stats/per-employee and /admin/stats/per-model.

  • `ix_fans_subscription_status` partial index — defensive index for the
    active-subs snapshot query. `fans.subscription_status` is currently
    never written (the codebase only reads it via `subscription_expires_at`
    today), so the active-subs path falls back to expires_at; the index
    is still cheap insurance for when/if status starts being populated.

SQLite quirk: there's no `CREATE OR REPLACE VIEW`. We DROP + CREATE
unconditionally so a future migration can change the view schema. Same
for the index, which lets the migration be re-applied safely.

SQLite version: the standalone-tip CTE uses ROW_NUMBER() (SQLite ≥ 3.25,
2018). The window function falls outside what very old SQLite supports;
if that ever becomes a constraint, swap for a correlated subquery.
"""
from __future__ import annotations

from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0012_phase_d_stats"
down_revision: str | Sequence[str] | None = "0011_phase_c_automation_employee"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_VIEW_SQL = """
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


_INDEX_SQL = """
CREATE INDEX ix_fans_subscription_status
ON fans(subscription_status)
WHERE subscription_status IS NOT NULL
"""


def upgrade() -> None:
    # DROP then CREATE so this migration is idempotent AND a future
    # migration can replace the view definition without manual cleanup.
    op.execute(sa.text("DROP VIEW IF EXISTS per_employee_revenue_with_attribution"))
    op.execute(sa.text(_VIEW_SQL))

    op.execute(sa.text("DROP INDEX IF EXISTS ix_fans_subscription_status"))
    op.execute(sa.text(_INDEX_SQL))


def downgrade() -> None:
    op.execute(sa.text("DROP VIEW IF EXISTS per_employee_revenue_with_attribution"))
    op.execute(sa.text("DROP INDEX IF EXISTS ix_fans_subscription_status"))
