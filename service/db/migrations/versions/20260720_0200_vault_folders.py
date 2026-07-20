"""vault_folders — internal folders + per-folder manual ordering

Revision ID: 0047_vault_folders
Revises: 0046_vault_ai_mirror
Create Date: 2026-07-20

Two new tables: `vault_folders` (internal, our own — no OF write) and
`vault_folder_items` (membership + per-folder manual_order). Lets the operator
make folders, select media, add to a folder, and reorder inside a folder with
zero OF writes.

Additive + idempotent (inspector-guarded, house convention).
"""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0047_vault_folders"
down_revision: str | Sequence[str] | None = "0046_vault_ai_mirror"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    names = inspector.get_table_names()

    if "vault_folders" not in names:
        op.create_table(
            "vault_folders",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("account_id", sa.String(),
                      sa.ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False),
            sa.Column("name", sa.String(), nullable=False),
            sa.Column("of_list_id", sa.BigInteger(), nullable=True),
            sa.Column("created_by", sa.String(), nullable=False, server_default="operator"),
            sa.Column("deleted_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False,
                      server_default=sa.text("CURRENT_TIMESTAMP")),
        )
        op.create_index("ix_vault_folders_account", "vault_folders",
                        ["account_id", "deleted_at"])

    if "vault_folder_items" not in names:
        op.create_table(
            "vault_folder_items",
            sa.Column("account_id", sa.String(),
                      sa.ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True),
            sa.Column("folder_id", sa.Integer(), primary_key=True),
            sa.Column("media_id", sa.BigInteger(), primary_key=True),
            sa.Column("manual_order", sa.Integer(), nullable=True),
            sa.Column("added_at", sa.DateTime(), nullable=False,
                      server_default=sa.text("CURRENT_TIMESTAMP")),
        )
        op.create_index("ix_vault_folder_items_folder", "vault_folder_items",
                        ["account_id", "folder_id"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    names = inspector.get_table_names()
    if "vault_folder_items" in names:
        op.drop_table("vault_folder_items")
    if "vault_folders" in names:
        op.drop_table("vault_folders")
