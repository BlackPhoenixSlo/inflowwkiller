"""saved_replies: add script_id + script_step

Revision ID: 0020_saved_reply_script
Revises: 0019_saved_reply_gif
Create Date: 2026-05-23

Template scripts (Small Fixes #5). A saved reply can opt into a script by
setting `script_id` (free-text group name) + `script_step` (1-based order).
The composer caches the last-sent step per chat in localStorage; if a
template with `script_id === cursor.script_id && script_step === cursor.step + 1`
exists, the chat shows a one-tap "next in script" bubble. Both columns
default to NULL (existing rows have no script).
"""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0020_saved_reply_script"
down_revision: str | Sequence[str] | None = "0019_saved_reply_gif"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "saved_replies",
        sa.Column("script_id", sa.String, nullable=True),
    )
    op.add_column(
        "saved_replies",
        sa.Column("script_step", sa.Integer, nullable=True),
    )


def downgrade() -> None:
    with op.batch_alter_table("saved_replies") as batch:
        batch.drop_column("script_step")
        batch.drop_column("script_id")
