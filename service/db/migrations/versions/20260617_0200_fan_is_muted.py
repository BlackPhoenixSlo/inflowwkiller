"""fan_is_muted — track creator-muted chats so automations leave them alone

Revision ID: 0036_fan_is_muted
Revises: 0035_funnel_account_media
Create Date: 2026-06-17

One additive change: `fans.is_muted` BOOLEAN NOT NULL DEFAULT 0.

OF exposes `isMutedNotifications` on every `/chats` list item (the creator-side
"Mute notifications" toggle — verified live: it lives on the top-level chat
object, not on `withUser`). The chat-scrape now persists it here so the
automations can durably skip-list a fan who is BOTH muted AND a creator we
follow (mutual-promo spam) — see `_common.should_skip_muted_creator`.

Additive + NOT-NULL-with-default + idempotent (inspector-guarded, house
convention so a create_all-built DB whose alembic_version lags doesn't trip).
"""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0036_fan_is_muted"
down_revision: str | Sequence[str] | None = "0035_funnel_account_media"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    fan_cols = {c["name"] for c in inspector.get_columns("fans")}
    if "is_muted" not in fan_cols:
        op.add_column(
            "fans",
            sa.Column("is_muted", sa.Boolean, nullable=False, server_default="0"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    fan_cols = {c["name"] for c in inspector.get_columns("fans")}
    if "is_muted" in fan_cols:
        op.drop_column("fans", "is_muted")
