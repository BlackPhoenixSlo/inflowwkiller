"""chatter_access — per-chatter visibility limits (models + folders)

Revision ID: 0028_chatter_access
Revises: 0027_nudge_online
Create Date: 2026-06-06

Owner-set, subtractive allowlists (library/CHATTER_ACCESS_PROMPTS.md):

  chatter_account_access — per (chatter, owner): if >=1 row, that owner's
                           contribution to the chatter's account union is
                           restricted to the listed account_ids. Empty ⇒
                           full access (existing chatters unaffected).
  chatter_folder_access  — per (chatter, account): if >=1 row, the
                           chatter's vault browser for that account is
                           restricted to those OF vault folder ids.
                           folder_id is BigInteger (matches
                           vault_items.folder_id; OF ids exceed int32).
                           folder_name is denormalised at write time so the
                           chatter's authoritative folder menu renders
                           without re-paginating vault/lists (DA-3).

Idempotent: guarded by inspector checks (house convention; init_db's
create_all may have already built these on a freshly-booted DB).
"""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0028_chatter_access"
down_revision: str | Sequence[str] | None = "0027_nudge_online"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = set(inspector.get_table_names())

    if "chatter_account_access" not in existing:
        op.create_table(
            "chatter_account_access",
            sa.Column(
                "chatter_id", sa.String, nullable=False,
            ),
            sa.Column(
                "user_id", sa.String, nullable=False,
            ),
            sa.Column(
                "account_id", sa.String, nullable=False,
            ),
            sa.Column(
                "created_at", sa.DateTime,
                nullable=False, server_default=sa.text("CURRENT_TIMESTAMP"),
            ),
            sa.ForeignKeyConstraint(
                ["chatter_id"], ["chatters.id"], ondelete="CASCADE",
                name="fk_chatter_account_access_chatter_id_chatters",
            ),
            sa.ForeignKeyConstraint(
                ["user_id"], ["users.id"], ondelete="CASCADE",
                name="fk_chatter_account_access_user_id_users",
            ),
            sa.ForeignKeyConstraint(
                ["account_id"], ["accounts.id"], ondelete="CASCADE",
                name="fk_chatter_account_access_account_id_accounts",
            ),
            sa.PrimaryKeyConstraint(
                "chatter_id", "user_id", "account_id",
                name="pk_chatter_account_access",
            ),
        )
        op.create_index(
            "ix_chatter_account_access_chatter",
            "chatter_account_access", ["chatter_id"],
        )

    if "chatter_folder_access" not in existing:
        op.create_table(
            "chatter_folder_access",
            sa.Column(
                "chatter_id", sa.String, nullable=False,
            ),
            sa.Column(
                "account_id", sa.String, nullable=False,
            ),
            sa.Column(
                "folder_id", sa.BigInteger, nullable=False,
            ),
            sa.Column(
                "folder_name", sa.String, nullable=True,
            ),
            sa.Column(
                "created_at", sa.DateTime,
                nullable=False, server_default=sa.text("CURRENT_TIMESTAMP"),
            ),
            sa.ForeignKeyConstraint(
                ["chatter_id"], ["chatters.id"], ondelete="CASCADE",
                name="fk_chatter_folder_access_chatter_id_chatters",
            ),
            sa.ForeignKeyConstraint(
                ["account_id"], ["accounts.id"], ondelete="CASCADE",
                name="fk_chatter_folder_access_account_id_accounts",
            ),
            sa.PrimaryKeyConstraint(
                "chatter_id", "account_id", "folder_id",
                name="pk_chatter_folder_access",
            ),
        )
        op.create_index(
            "ix_chatter_folder_access_chatter",
            "chatter_folder_access", ["chatter_id"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = set(inspector.get_table_names())

    if "chatter_folder_access" in existing:
        op.drop_table("chatter_folder_access")

    if "chatter_account_access" in existing:
        op.drop_table("chatter_account_access")
