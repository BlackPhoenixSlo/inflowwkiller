"""users + user_accounts — friends-only tenant split

Revision ID: 0022_users_and_user_accounts
Revises: 0021_mass_broadcast_cache
Create Date: 2026-05-24

Adds the two tables needed for username/password auth + per-user OF account
ownership. See plan/simple_username_auth_2026_05_24/PLAN.md.

Both tables are additive; nothing in the existing schema references them.
A friend without a `user_accounts` row sees an empty ScopeSwitcher; the
founder gets a backfill row per existing OF account once a founder user
row exists.

Password column is plaintext per user decision (friends-only scale). Do
NOT widen exposure of the DB file without first switching to a hash.
"""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0022_users_and_user_accounts"
# Merges two heads in the same commit: the main chain (0021_mass_broadcast_cache)
# and the dangling 0019_vault_response_cache that nobody chained forward from.
down_revision: str | Sequence[str] | None = (
    "0021_mass_broadcast_cache",
    "0019_vault_response_cache",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = set(inspector.get_table_names())

    if "users" not in existing:
        op.create_table(
            "users",
            sa.Column("id", sa.String, primary_key=True),
            sa.Column("username", sa.String, nullable=False),
            sa.Column("password", sa.String, nullable=False),
            sa.Column(
                "created_at", sa.DateTime,
                nullable=False, server_default=sa.text("CURRENT_TIMESTAMP"),
            ),
            sa.Column(
                "last_seen_at", sa.DateTime,
                nullable=False, server_default=sa.text("CURRENT_TIMESTAMP"),
            ),
            sa.UniqueConstraint("username", name="uq_users_username"),
        )

    if "user_accounts" not in existing:
        op.create_table(
            "user_accounts",
            sa.Column(
                "user_id", sa.String,
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                primary_key=True,
            ),
            sa.Column(
                "account_id", sa.String,
                sa.ForeignKey("accounts.id", ondelete="CASCADE"),
                primary_key=True,
            ),
            sa.Column(
                "created_at", sa.DateTime,
                nullable=False, server_default=sa.text("CURRENT_TIMESTAMP"),
            ),
        )


def downgrade() -> None:
    op.drop_table("user_accounts")
    op.drop_table("users")
