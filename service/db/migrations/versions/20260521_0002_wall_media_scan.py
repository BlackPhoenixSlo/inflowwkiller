"""wall-media incremental scan tables

Revision ID: 0003_wall_media_scan
Revises: 0002_saved_replies
Create Date: 2026-05-21

Replaces the prior stateless 5-page (~250-post) wall walk with persistent
storage so the blue "posted on wall" ring in the vault picker stays
eventually-correct for prolific creators and warm opens skip the 13.5s
re-walk. See models.WallMedia / WallScanState for the algorithm notes.
"""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0003_wall_media_scan"
down_revision: str | Sequence[str] | None = "0002_saved_replies"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "wall_media",
        sa.Column("account_id", sa.String, nullable=False),
        sa.Column("media_id", sa.BigInteger, nullable=False),
        sa.Column("post_id", sa.BigInteger, nullable=True),
        sa.Column("post_published_at", sa.DateTime, nullable=True),
        sa.Column("first_seen_at", sa.DateTime, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.PrimaryKeyConstraint("account_id", "media_id", name="pk_wall_media"),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE",
                                name="fk_wall_media_account_id_accounts"),
    )
    op.create_index("ix_wall_media_account_id", "wall_media", ["account_id"])

    op.create_table(
        "wall_scan_state",
        sa.Column("account_id", sa.String, nullable=False),
        sa.Column("newest_post_published_at", sa.DateTime, nullable=True),
        sa.Column("oldest_post_published_at", sa.DateTime, nullable=True),
        sa.Column("fully_backfilled", sa.Boolean, nullable=False, server_default=sa.text("0")),
        sa.Column("last_scan_at", sa.DateTime, nullable=True),
        sa.Column("scanned_posts_total", sa.Integer, nullable=False, server_default="0"),
        sa.PrimaryKeyConstraint("account_id", name="pk_wall_scan_state"),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE",
                                name="fk_wall_scan_state_account_id_accounts"),
    )


def downgrade() -> None:
    op.drop_table("wall_scan_state")
    op.drop_index("ix_wall_media_account_id", table_name="wall_media")
    op.drop_table("wall_media")
