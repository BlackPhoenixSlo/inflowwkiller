"""phase_a_indexes

Revision ID: 0009_phase_a_indexes
Revises: 0008_phase_a_drop_bought_amount
Create Date: 2026-05-22

Originally slated as "indexes and stragglers." After 0005-0008 the only
straggler left is `prompts.response_schema_json` (T-A3 follow-up — adds
a nullable Text field to Prompt for future response-schema validation).
No new indexes turned up; the partial / composite indexes all landed
with their owning column in 0005 / 0007.

Still excluded (autogenerate keeps proposing them; out of scope):
  • messages.{unsent_*, is_promise, promise_due_date} — T-A2 columns
    deferred until B.5. A standalone follow-up migration must add them
    before any code that touches them ships.
"""
from __future__ import annotations

from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0009_phase_a_indexes"
down_revision: str | Sequence[str] | None = "0008_phase_a_drop_bought_amount"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("prompts", schema=None) as batch_op:
        batch_op.add_column(sa.Column("response_schema_json", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("prompts", schema=None) as batch_op:
        batch_op.drop_column("response_schema_json")
