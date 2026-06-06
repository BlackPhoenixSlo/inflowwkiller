"""employees.user_id — scope chatter roster per friend

Revision ID: 0023_employees_per_user
Revises: 0022_users_and_user_accounts
Create Date: 2026-05-24

Adds a nullable `user_id` column to `employees` so each registered friend
sees only their own chatters in the picker. Existing rows are backfilled
to the founder user (if one exists named 'jakabasej') so the current
roster keeps working unchanged. NULL `user_id` is reserved for system
rows (e.g. the 'Automation' sentinel) that should never appear in any
user's picker.

The audit-log and Action FK to employee_id is unaffected; per-user
isolation in /admin/audit is done at query time via account_id +
employee_id filters.
"""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0023_employees_per_user"
down_revision: str | Sequence[str] | None = "0022_users_and_user_accounts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    cols = {c["name"] for c in inspector.get_columns("employees")}
    if "user_id" not in cols:
        # SQLite: no inline FK with `ALTER TABLE ADD COLUMN`, so we add
        # the column without the FK constraint. The FK is informational
        # at the SQLite layer anyway (PRAGMA foreign_keys is off in many
        # connection paths); ownership enforcement lives at the
        # application layer.
        op.add_column(
            "employees",
            sa.Column("user_id", sa.String, nullable=True),
        )
        op.create_index(
            "ix_employees_user_id", "employees", ["user_id"]
        )
    # Backfill: existing rows go to the founder (username='jakabasej') if
    # such a user exists. The Automation sentinel row stays NULL so it
    # never shows up in any picker.
    bind.execute(sa.text(
        "UPDATE employees SET user_id = ("
        "  SELECT id FROM users WHERE username = 'jakabasej'"
        ") WHERE user_id IS NULL AND display_name <> 'Automation'"
    ))


def downgrade() -> None:
    op.drop_index("ix_employees_user_id", table_name="employees")
    op.drop_column("employees", "user_id")
