"""attr_view_fan_id — fix mass-PPV revenue over-attribution in the view

Revision ID: 0038_attr_view_fan_id
Revises: 0037_ppv_library_config
Create Date: 2026-06-20

BUG: the per-automation / per-employee revenue panel massively over-counted
mass-PPV earnings (e.g. ~$20 of real `ppv_send` sales rendered as $1,361).

ROOT CAUSE: a mass broadcast writes one optimistic `messages` row per
recipient (attribution.write_mass_optimistic_rows), and ALL of them share a
single synthetic `message_id` (`mass_placeholder_message_id` = 5e15 + run_id)
— they're distinguished only by `fan_id` in the composite PK. These rows are
never reconciled away because OF's broadcast endpoint echoes no per-fan ids.

When one recipient unlocks the PPV, `transaction_ingest._link_ppv_message`
stamps that ONE transaction with `tx.message_id = <placeholder>`. The
attribution view's `message_linked` CTE then joined transactions↔messages on
`(account_id, message_id)` ONLY — so a single unlock fanned out across EVERY
placeholder row in the run, multiplying its amount by the recipient count.

FIX: add `m.fan_id = t.fan_id` to the `message_linked` join, exactly as the
sibling `standalone_tips_ranked` CTE already does. Both transaction writers
(`_link_ppv_message` ledger path, `_record_inbound_payment` WS path) set
`fan_id` alongside `message_id`, and a paid message always belongs to the
buyer — so the extra condition never drops a legitimately-linked transaction;
it only collapses the placeholder fan-out to the one correct recipient row.

The view is computed live, so applying this immediately corrects every
displayed figure — no transaction/message backfill is required. The
already-stamped `tx.message_id = <placeholder>` values stay valid: with the
fan_id condition they resolve to exactly that fan's placeholder row.

Pure view rebuild (DROP + CREATE). No table/column/index changes.
"""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0038_attr_view_fan_id"
down_revision: str | Sequence[str] | None = "0037_ppv_library_config"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Corrected view: `message_linked` now joins on fan_id too (see module
# docstring). `standalone_tips_ranked` is unchanged from 0032 (it already
# matched on fan_id). SELECT shape is identical to 0032 so every reader is
# unaffected.
_NEW_VIEW_SQL = """
CREATE VIEW per_employee_revenue_with_attribution AS
WITH message_linked AS (
  SELECT
    COALESCE(t.attributed_employee_id, m.sent_by_employee_id) AS sent_by_employee_id,
    m.automation_kind,
    m.account_id,
    t.kind,
    t.amount_cents,
    t.occurred_at
  FROM transactions t
  INNER JOIN messages m
    ON m.account_id = t.account_id
   AND m.fan_id = t.fan_id
   AND m.message_id = t.message_id
  WHERE t.message_id IS NOT NULL
    AND t.kind IN (
      'ppv', 'ppv_message', 'ppv_post', 'ppv_stream',
      'tip', 'tip_post', 'tip_stream',
      'custom'
    )
    AND t.status IN ('cleared', 'pending')
),
standalone_tips_ranked AS (
  SELECT
    t.id AS tx_id,
    t.account_id,
    t.amount_cents,
    t.kind,
    t.occurred_at,
    COALESCE(t.attributed_employee_id, m.sent_by_employee_id) AS sent_by_employee_id,
    m.automation_kind,
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
    AND t.status IN ('cleared', 'pending')
)
SELECT sent_by_employee_id, automation_kind, account_id, kind, amount_cents, occurred_at
  FROM message_linked
UNION ALL
SELECT sent_by_employee_id, automation_kind, account_id, kind, amount_cents, occurred_at
  FROM standalone_tips_ranked
 WHERE rn = 1
"""


# Prior (buggy) view from 0032_automation_kind — restored on downgrade. The
# `message_linked` join omits fan_id; see this module's docstring for why that
# over-attributes mass-PPV revenue.
_OLD_VIEW_SQL = """
CREATE VIEW per_employee_revenue_with_attribution AS
WITH message_linked AS (
  SELECT
    COALESCE(t.attributed_employee_id, m.sent_by_employee_id) AS sent_by_employee_id,
    m.automation_kind,
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
    AND t.status IN ('cleared', 'pending')
),
standalone_tips_ranked AS (
  SELECT
    t.id AS tx_id,
    t.account_id,
    t.amount_cents,
    t.kind,
    t.occurred_at,
    COALESCE(t.attributed_employee_id, m.sent_by_employee_id) AS sent_by_employee_id,
    m.automation_kind,
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
    AND t.status IN ('cleared', 'pending')
)
SELECT sent_by_employee_id, automation_kind, account_id, kind, amount_cents, occurred_at
  FROM message_linked
UNION ALL
SELECT sent_by_employee_id, automation_kind, account_id, kind, amount_cents, occurred_at
  FROM standalone_tips_ranked
 WHERE rn = 1
"""


def upgrade() -> None:
    op.execute(sa.text("DROP VIEW IF EXISTS per_employee_revenue_with_attribution"))
    op.execute(sa.text(_NEW_VIEW_SQL))


def downgrade() -> None:
    op.execute(sa.text("DROP VIEW IF EXISTS per_employee_revenue_with_attribution"))
    op.execute(sa.text(_OLD_VIEW_SQL))
