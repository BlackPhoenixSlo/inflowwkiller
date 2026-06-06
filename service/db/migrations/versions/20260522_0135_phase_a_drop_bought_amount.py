"""phase_a_drop_bought_amount

Revision ID: 0008_phase_a_drop_bought_amount
Revises: 0007_phase_a_safety_tables
Create Date: 2026-05-22

Drops the legacy `fans.bought_amount` NUMERIC column. Replaced by
`lifetime_spend_cents` (BIGINT, cents) — float money was always wrong.

PROD ROLLOUT PRECONDITION:
  Before applying this revision on prod, run
  service/db/backfills/backfill_lifetime_spend.py to copy any remaining
  bought_amount > 0 values into lifetime_spend_cents. Sanity check:
      SELECT COUNT(*) FROM fans
      WHERE bought_amount > 0 AND lifetime_spend_cents = 0;
  Must return 0 before upgrade head. Otherwise the spend data for those
  fans is silently lost.

T-B4 already shipped: service/fans.py no longer references bought_amount,
so dropping it here will not break the fan API (verified via grep before
generating this revision).

Excluded (autogenerate proposed; out of scope for this revision):
  • messages.{unsent_*, is_promise, promise_due_date} — deferred to a
    follow-up migration before B.5.
  • prompts.response_schema_json — lands in 0009.

Downgrade re-adds the column with server_default='0' so the batch
rebuild can backfill existing rows without violating NOT NULL.
"""
from __future__ import annotations

from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0008_phase_a_drop_bought_amount"
down_revision: str | Sequence[str] | None = "0007_phase_a_safety_tables"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("fans", schema=None) as batch_op:
        batch_op.drop_column("bought_amount")


def downgrade() -> None:
    with op.batch_alter_table("fans", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "bought_amount",
                sa.NUMERIC(precision=10, scale=2),
                nullable=False,
                server_default="0",
            )
        )
