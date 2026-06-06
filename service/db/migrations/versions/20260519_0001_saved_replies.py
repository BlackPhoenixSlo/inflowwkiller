"""saved replies (local-only "templates" minus welcome slot)

Revision ID: 0002_saved_replies
Revises: 0001_initial
Create Date: 2026-05-19

OF's /messages/templates endpoint rejects creates for everything except
the welcome slot (template=reply_on_subscribe). We keep saved replies in
the local DB and the UI merges them with OF's welcome message at render
time.
"""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0002_saved_replies"
down_revision: str | Sequence[str] | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "saved_replies",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("account_id", sa.String, nullable=False),
        sa.Column("title", sa.String, nullable=True),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("price_cents", sa.Integer, nullable=False, server_default="0"),
        sa.Column("locked_text", sa.Boolean, nullable=False, server_default=sa.text("0")),
        sa.Column("media_json", sa.Text, nullable=False, server_default="[]"),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE",
                                name="fk_saved_replies_account_id_accounts"),
    )
    op.create_index("ix_saved_replies_account", "saved_replies", ["account_id", "updated_at"])


def downgrade() -> None:
    op.drop_index("ix_saved_replies_account", table_name="saved_replies")
    op.drop_table("saved_replies")
