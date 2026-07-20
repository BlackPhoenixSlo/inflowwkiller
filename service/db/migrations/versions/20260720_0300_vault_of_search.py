"""vault_of_search — harvest OF's own vault search into our index

Revision ID: 0048_vault_of_search
Revises: 0047_vault_folders
Create Date: 2026-07-20

Adds `vault_items.of_terms` (OF-search terms an item matched, folded into
search_text) + `vault_of_query_log` (harvested-query TTL/dedup) so our local
search is a superset of OF's server-side search.

Additive + idempotent (inspector-guarded, house convention).
"""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0048_vault_of_search"
down_revision: str | Sequence[str] | None = "0047_vault_folders"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "vault_items" in inspector.get_table_names():
        cols = {c["name"] for c in inspector.get_columns("vault_items")}
        if "of_terms" not in cols:
            op.add_column("vault_items", sa.Column("of_terms", sa.Text(), nullable=True))

    if "vault_of_query_log" not in inspector.get_table_names():
        op.create_table(
            "vault_of_query_log",
            sa.Column("account_id", sa.String(),
                      sa.ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True),
            sa.Column("query", sa.String(), primary_key=True),
            sa.Column("match_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("fetched_at", sa.DateTime(), nullable=False,
                      server_default=sa.text("CURRENT_TIMESTAMP")),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "vault_of_query_log" in inspector.get_table_names():
        op.drop_table("vault_of_query_log")
    if "vault_items" in inspector.get_table_names():
        cols = {c["name"] for c in inspector.get_columns("vault_items")}
        if "of_terms" in cols:
            op.drop_column("vault_items", "of_terms")
