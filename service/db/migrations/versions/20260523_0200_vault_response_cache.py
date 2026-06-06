"""vault_response_cache: shared server-side cache for OF vault listings

Revision ID: 0019_vault_response_cache
Revises: 0018_template_tags_previews
Create Date: 2026-05-23

Every employee on the same OF account hits the same vault, so the OF
passthrough now caches each (account_id, query_key) response and serves
subsequent reads from SQLite. Any vault-mutating call wipes the rows
for the account. TTL is enforced in the application layer (see
service/vault_cache.py), so we only need {pk, payload, fetched_at} on disk.
"""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0019_vault_response_cache"
down_revision: str | Sequence[str] | None = "0018_template_tags_previews"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # engine.init_db() runs Base.metadata.create_all() on every startup so
    # the table may already exist by the time alembic catches up. Guard
    # the CREATE so re-running this migration on an existing box is a no-op.
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "vault_response_cache" in inspector.get_table_names():
        return
    op.create_table(
        "vault_response_cache",
        sa.Column(
            "account_id", sa.String,
            sa.ForeignKey("accounts.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("query_key", sa.String, primary_key=True),
        sa.Column("response_json", sa.Text, nullable=False),
        sa.Column(
            "fetched_at", sa.DateTime,
            nullable=False, server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )


def downgrade() -> None:
    op.drop_table("vault_response_cache")
