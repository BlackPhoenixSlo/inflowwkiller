"""saved_replies: add gif_id + gif_url

Revision ID: 0019_saved_reply_gif
Revises: 0018_template_tags_previews
Create Date: 2026-05-23

GIF support on saved replies. A template can now carry a single Giphy id
plus its animated preview URL. When the template is picked, the composer
seeds its picked-GIF state so the GIF rides along on send. Both fields
default to NULL (existing rows have no GIF). The animated URL is cached
so the picker chip can render without a fresh Giphy roundtrip.
"""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0019_saved_reply_gif"
down_revision: str | Sequence[str] | None = "0018_template_tags_previews"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "saved_replies",
        sa.Column("gif_id", sa.String, nullable=True),
    )
    op.add_column(
        "saved_replies",
        sa.Column("gif_url", sa.Text, nullable=True),
    )


def downgrade() -> None:
    with op.batch_alter_table("saved_replies") as batch:
        batch.drop_column("gif_url")
        batch.drop_column("gif_id")
