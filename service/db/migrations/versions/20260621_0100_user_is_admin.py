"""user_is_admin — permanent master/admin role flag on users

Revision ID: 0039_user_is_admin
Revises: 0038_attr_view_fan_id
Create Date: 2026-06-21

One additive change: `users.is_admin` BOOLEAN NOT NULL DEFAULT 0.

A "master" user (is_admin=1) is given the FULL account set as its request-time
`account_ids` snapshot — sourced from the in-process account registry (the SAME
source the isolation middleware and `_resolve_account_id` trust), NOT this DB —
so every existing per-account gate (`assert_account_owned`, `clamp_account_filter`,
the ~80 scattered `aid in user.account_ids` checks) passes for free. The
employee axis filters on a different column (`employees.user_id`) and gets
explicit `if not user.is_admin` bypasses in employees.py / auth.py.

Additive + NOT-NULL-with-default + idempotent (inspector-guarded, house
convention so a create_all-built DB whose alembic_version lags doesn't trip).
"""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0039_user_is_admin"
down_revision: str | Sequence[str] | None = "0038_attr_view_fan_id"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    user_cols = {c["name"] for c in inspector.get_columns("users")}
    if "is_admin" not in user_cols:
        op.add_column(
            "users",
            sa.Column("is_admin", sa.Boolean, nullable=False, server_default="0"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    user_cols = {c["name"] for c in inspector.get_columns("users")}
    if "is_admin" in user_cols:
        op.drop_column("users", "is_admin")
