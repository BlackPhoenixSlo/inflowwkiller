"""attr_view_orphan_rows — orphan revenue rows become visible; manual assignment wins

Revision ID: 0042_attr_view_orphan_rows
Revises: 0041_account_health
Create Date: 2026-07-10

BUG: a PPV transaction with no message link (`message_id IS NULL`, or a
dangling `message_id` with no matching `messages` row) matched NEITHER branch
of `per_employee_revenue_with_attribution` — `message_linked` INNER-JOINs on a
non-NULL message_id, and the standalone fallback covers TIP kinds only. Such
sales (2,341 `ppv%` rows on prod as of 2026-07-10) were invisible in the
per-employee stats: not under any chatter, not even in the "Unattributed"
bucket. Worse, the manual-assign endpoint
(POST /admin/ingest/transactions/{id}/attribute) writes only
`transactions.attributed_employee_id`, which the view never read for these
rows — so assigning an orphan PPV to a chatter changed NOTHING on the table.

FIX: add a third UNION branch (`orphan_rows`) emitting every revenue-kind
cleared/pending transaction that (a) has NO matching messages row via the
(account_id, fan_id, message_id) join — covers both NULL and dangling
message_id — and (b) is not a standalone tip (those are branch 2's, complete
with the 7-day lookback). The branch emits `attributed_employee_id` as the
employee (NULL → the "Unattributed" bucket), `automation_kind` NULL (so the
per-automation panel, which filters `automation_kind IS NOT NULL`, ignores
these rows by design).

Mutual exclusivity (exactly-once emission), partitioning all tracked-status
revenue-kind transactions:
  A. message_id set + messages match      → branch 1 only (join hits; NOT
     EXISTS in branch 3 is false)
  B. message_id set + no messages match   → branch 3 only (INNER JOIN misses)
  C. message_id NULL + tip kind           → branch 2 only (branch 3 excludes
     standalone tips; branch 1 requires non-NULL)
  D. message_id NULL + non-tip kind       → branch 3 only (join on NULL never
     matches → NOT EXISTS true)

Branches 1 and 2 are byte-identical to revision 0038. Pure view rebuild
(DROP + CREATE); no table/column/index changes.
"""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0042_attr_view_orphan_rows"
down_revision: str | Sequence[str] | None = "0041_account_health"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


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
),
orphan_rows AS (
  SELECT
    t.attributed_employee_id AS sent_by_employee_id,
    NULL AS automation_kind,
    t.account_id,
    t.kind,
    t.amount_cents,
    t.occurred_at
  FROM transactions t
  WHERE t.kind IN (
      'ppv', 'ppv_message', 'ppv_post', 'ppv_stream',
      'tip', 'tip_post', 'tip_stream',
      'custom'
    )
    AND t.status IN ('cleared', 'pending')
    AND NOT (t.kind IN ('tip', 'tip_post', 'tip_stream') AND t.message_id IS NULL)
    AND NOT EXISTS (
      SELECT 1 FROM messages m
       WHERE m.account_id = t.account_id
         AND m.fan_id = t.fan_id
         AND m.message_id = t.message_id
    )
)
SELECT sent_by_employee_id, automation_kind, account_id, kind, amount_cents, occurred_at
  FROM message_linked
UNION ALL
SELECT sent_by_employee_id, automation_kind, account_id, kind, amount_cents, occurred_at
  FROM standalone_tips_ranked
 WHERE rn = 1
UNION ALL
SELECT sent_by_employee_id, automation_kind, account_id, kind, amount_cents, occurred_at
  FROM orphan_rows
"""


# Prior view from 0038_attr_view_fan_id — restored on downgrade. Orphan
# revenue rows (case B/D above) simply vanish from the view again.
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


def upgrade() -> None:
    op.execute(sa.text("DROP VIEW IF EXISTS per_employee_revenue_with_attribution"))
    op.execute(sa.text(_NEW_VIEW_SQL))


def downgrade() -> None:
    op.execute(sa.text("DROP VIEW IF EXISTS per_employee_revenue_with_attribution"))
    op.execute(sa.text(_OLD_VIEW_SQL))
