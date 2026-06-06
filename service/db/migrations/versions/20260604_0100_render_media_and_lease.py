"""render_media_and_lease — message_media + fan_lease tables

Revision ID: 0025_render_media_and_lease
Revises: 0024_chatter_login
Create Date: 2026-06-04

Two new standalone tables (no rebuilds), grouped because both are greenfield
infra consumed by later waves:

  message_media — one row per media item on a message; the render-stability
                  cache (18_chat_render_stability.md §1.1). width/height are
                  NULLABLE and populated lazily by T-API (OF REST files/info)
                  and/or a client onload→PATCH. Child of `messages` with
                  ON DELETE CASCADE so a reconciled mass placeholder takes its
                  media rows with it.
  fan_lease     — per-fan send lease for the automation executor (T-EXEC /
                  MASTER_PLAN §5). PK (account_id, fan_id) = one live lease
                  per fan, so two automations can't message a fan at once.

Idempotent: each create is guarded by an inspector table-existence check, so
re-running on a partially-migrated copy is a no-op (house convention, mirrors
0024_chatter_login).
"""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0025_render_media_and_lease"
down_revision: str | Sequence[str] | None = "0024_chatter_login"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = set(inspector.get_table_names())

    if "message_media" not in existing:
        op.create_table(
            "message_media",
            sa.Column("account_id", sa.String, nullable=False),
            sa.Column("fan_id", sa.BigInteger, nullable=False),
            sa.Column("message_id", sa.BigInteger, nullable=False),
            sa.Column("media_id", sa.BigInteger, nullable=False),
            sa.Column("type", sa.String, nullable=True),
            sa.Column("width", sa.Integer, nullable=True),
            sa.Column("height", sa.Integer, nullable=True),
            sa.Column("thumb_url", sa.Text, nullable=True),
            sa.Column("source_url", sa.Text, nullable=True),
            sa.Column("duration_ms", sa.Integer, nullable=True),
            sa.Column(
                "ingested_at", sa.DateTime,
                nullable=False, server_default=sa.text("CURRENT_TIMESTAMP"),
            ),
            sa.Column(
                "updated_at", sa.DateTime,
                nullable=False, server_default=sa.text("CURRENT_TIMESTAMP"),
            ),
            sa.PrimaryKeyConstraint(
                "account_id", "fan_id", "message_id", "media_id",
                name="pk_message_media",
            ),
            sa.ForeignKeyConstraint(
                ["account_id", "fan_id", "message_id"],
                ["messages.account_id", "messages.fan_id", "messages.message_id"],
                name="fk_message_media_account_id_messages",
                ondelete="CASCADE",
            ),
        )
        op.create_index(
            "ix_message_media_media", "message_media", ["account_id", "media_id"],
        )

    if "fan_lease" not in existing:
        op.create_table(
            "fan_lease",
            sa.Column("account_id", sa.String, nullable=False),
            sa.Column("fan_id", sa.BigInteger, nullable=False),
            sa.Column("automation_kind", sa.String, nullable=False),
            sa.Column("leased_until", sa.DateTime, nullable=False),
            sa.Column(
                "acquired_at", sa.DateTime,
                nullable=False, server_default=sa.text("CURRENT_TIMESTAMP"),
            ),
            sa.PrimaryKeyConstraint("account_id", "fan_id", name="pk_fan_lease"),
        )
        op.create_index(
            "ix_fan_lease_expiry", "fan_lease", ["leased_until"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = set(inspector.get_table_names())
    if "fan_lease" in existing:
        op.drop_table("fan_lease")
    if "message_media" in existing:
        op.drop_table("message_media")
