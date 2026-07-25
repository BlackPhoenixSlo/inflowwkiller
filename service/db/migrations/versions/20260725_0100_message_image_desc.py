"""message_image_desc — cache the vision description of an inbound photo per message

Revision ID: 0052_message_image_desc
Revises: 0051_fan_language_and_timeline
Create Date: 2026-07-25

One additive, NULLABLE column (no server_default — NULL is the semantic default,
"never described": no image, feature off, or a describe failure). init_db's ADD-COLUMN
catch-up materializes it on prod with zero risk of the raw-DEFAULT-interpolation
footgun; this file keeps the alembic chain linear.

  messages.image_desc   Qwen3-VL description of an inbound photo the FAN sent, cached
                        at ingest by webhook_dispatch.on_inbound_image (gated by
                        tip_reward image_describe_enabled). The chat engines read it
                        back into history as "[photo he sent: …]" so the AI can rate /
                        react to it.

Hand-written op.add_column (NEVER --autogenerate: render_as_batch would rewrite the
whole `messages` table). Inspector-guarded + idempotent (house convention so a
create_all-built DB whose alembic_version lags doesn't trip).
"""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0052_message_image_desc"
down_revision: str | Sequence[str] | None = "0051_fan_language_and_timeline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    msg_cols = {c["name"] for c in inspector.get_columns("messages")}
    if "image_desc" not in msg_cols:
        op.add_column("messages", sa.Column("image_desc", sa.Text, nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    msg_cols = {c["name"] for c in inspector.get_columns("messages")}
    if "image_desc" in msg_cols:
        op.drop_column("messages", "image_desc")
