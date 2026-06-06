"""chatters + chatter_users + chatters_invites + employees.chatter_id

Revision ID: 0024_chatter_login
Revises: 0023_employees_per_user
Create Date: 2026-05-24

Step 8 of plan: a chatter signs in once and sees the union of every
owner's UserAccount rows. Three new tables and one Employee column.

  chatters            — auth row (username + plaintext password, friends-scale)
  chatter_users       — many-to-many: which Users (owners) each Chatter is
                        linked to. Account_ids per request is the SET UNION
                        across all linked owners.
  chatters_invites    — owner-issued single-use tokens that grant
                        /chatter/register access + auto-link the new
                        Chatter to the issuing owner on first login.
  employees.chatter_id — when a per-owner Employee row exists for a given
                        Chatter, the audit pipeline resolves to that row.

Both the table creates and the column add use idempotent guards (inspector
check before each DDL) so re-running on a partially-migrated copy is a
no-op. SQLite ALTER TABLE ADD COLUMN cannot carry an inline FK reference,
so `employees.chatter_id` is added without the FK constraint — ownership
enforcement lives at the application layer, same convention as
0023_employees_per_user.user_id.
"""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0024_chatter_login"
down_revision: str | Sequence[str] | None = "0023_employees_per_user"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    if "chatters" not in existing_tables:
        op.create_table(
            "chatters",
            sa.Column("id", sa.String, primary_key=True),
            sa.Column("username", sa.String, nullable=False),
            sa.Column("password", sa.String, nullable=False),
            sa.Column("display_name", sa.String, nullable=True),
            sa.Column("color", sa.String, nullable=True),
            sa.Column(
                "is_active", sa.Boolean,
                nullable=False, server_default=sa.text("1"),
            ),
            sa.Column(
                "created_at", sa.DateTime,
                nullable=False, server_default=sa.text("CURRENT_TIMESTAMP"),
            ),
            sa.Column(
                "last_seen_at", sa.DateTime,
                nullable=False, server_default=sa.text("CURRENT_TIMESTAMP"),
            ),
            sa.UniqueConstraint("username", name="uq_chatters_username"),
        )

    if "chatter_users" not in existing_tables:
        op.create_table(
            "chatter_users",
            sa.Column(
                "chatter_id", sa.String,
                sa.ForeignKey("chatters.id", ondelete="CASCADE"),
                primary_key=True,
            ),
            sa.Column(
                "user_id", sa.String,
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                primary_key=True,
            ),
            sa.Column(
                "created_at", sa.DateTime,
                nullable=False, server_default=sa.text("CURRENT_TIMESTAMP"),
            ),
        )
        # Reverse lookup: "every chatter linked to this owner" — read by
        # GET /admin/chatters on every owner-side render.
        op.create_index(
            "ix_chatter_users_user_id", "chatter_users", ["user_id"],
        )

    if "chatters_invites" not in existing_tables:
        op.create_table(
            "chatters_invites",
            sa.Column("token", sa.String, primary_key=True),
            sa.Column(
                "user_id", sa.String,
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "created_at", sa.DateTime,
                nullable=False, server_default=sa.text("CURRENT_TIMESTAMP"),
            ),
            sa.Column("expires_at", sa.DateTime, nullable=False),
            sa.Column("used_at", sa.DateTime, nullable=True),
            sa.Column(
                "used_by_chatter_id", sa.String, nullable=True,
            ),
        )
        op.create_index(
            "ix_chatters_invites_user_id", "chatters_invites", ["user_id"],
        )

    # Employee.chatter_id — when set, the audit pipeline resolves
    # (chatter_id, account_owner_id) → this Employee.id and stamps
    # actions.employee_id with it. NULL = label-only Employee (legacy /
    # owner-only role), unchanged behavior.
    emp_cols = {c["name"] for c in inspector.get_columns("employees")}
    if "chatter_id" not in emp_cols:
        op.add_column(
            "employees",
            sa.Column("chatter_id", sa.String, nullable=True),
        )
        op.create_index(
            "ix_employees_chatter_id", "employees", ["chatter_id"],
        )
    # Partial UNIQUE index: at most one Employee row per (chatter, owner)
    # pair. Legacy / label-only Employees (chatter_id IS NULL) are
    # excluded so the existing roster keeps coexisting with chatter
    # mirrors. The audit pipeline relies on this for its ON CONFLICT DO
    # NOTHING + SELECT-after-insert auto-create pattern under a chatter
    # session — without it, two simultaneous messages on two new owners
    # would race-insert duplicate mirror rows.
    emp_indexes = {ix["name"] for ix in inspector.get_indexes("employees")}
    if "uq_employees_chatter_owner" not in emp_indexes:
        op.create_index(
            "uq_employees_chatter_owner",
            "employees",
            ["chatter_id", "user_id"],
            unique=True,
            sqlite_where=sa.text("chatter_id IS NOT NULL"),
            postgresql_where=sa.text("chatter_id IS NOT NULL"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    emp_indexes = {ix["name"] for ix in inspector.get_indexes("employees")}
    if "uq_employees_chatter_owner" in emp_indexes:
        op.drop_index("uq_employees_chatter_owner", table_name="employees")
    if "ix_employees_chatter_id" in emp_indexes:
        op.drop_index("ix_employees_chatter_id", table_name="employees")
    emp_cols = {c["name"] for c in inspector.get_columns("employees")}
    if "chatter_id" in emp_cols:
        op.drop_column("employees", "chatter_id")

    existing_tables = set(inspector.get_table_names())
    if "chatters_invites" in existing_tables:
        op.drop_table("chatters_invites")
    if "chatter_users" in existing_tables:
        op.drop_table("chatter_users")
    if "chatters" in existing_tables:
        op.drop_table("chatters")
