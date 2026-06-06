"""b5_unsend_columns

Revision ID: 0010_b5_unsend_columns
Revises: 0009_phase_a_indexes
Create Date: 2026-05-22

Adds the five `messages` columns deferred from 0005-0009 (T-A2 audit fields
declared in models.py without a paired migration — this revision restores
model/schema parity so `/admin/messages` stops 500ing).

  • unsent_by_employee_id (Integer, FK employees.id ON DELETE SET NULL)
  • unsent_reason (Text)
  • unsent_at (DateTime)
  • is_promise (Boolean NOT NULL, server_default='0' — required so the
    SQLite batch rebuild can backfill existing message rows; without it
    the migration aborts the same way 0005's JSON columns would have)
  • promise_due_date (DateTime)
"""
from __future__ import annotations

from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0010_b5_unsend_columns"
down_revision: str | Sequence[str] | None = "0009_phase_a_indexes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("messages", schema=None) as batch_op:
        batch_op.add_column(sa.Column("unsent_by_employee_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("unsent_reason", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("unsent_at", sa.DateTime(), nullable=True))
        batch_op.add_column(
            sa.Column("is_promise", sa.Boolean(), nullable=False, server_default="0")
        )
        batch_op.add_column(sa.Column("promise_due_date", sa.DateTime(), nullable=True))
        batch_op.create_foreign_key(
            batch_op.f("fk_messages_unsent_by_employee_id_employees"),
            "employees",
            ["unsent_by_employee_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("messages", schema=None) as batch_op:
        batch_op.drop_constraint(
            batch_op.f("fk_messages_unsent_by_employee_id_employees"), type_="foreignkey"
        )
        batch_op.drop_column("promise_due_date")
        batch_op.drop_column("is_promise")
        batch_op.drop_column("unsent_at")
        batch_op.drop_column("unsent_reason")
        batch_op.drop_column("unsent_by_employee_id")
