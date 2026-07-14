"""upsell — qualification gate + hot-window ladder (offer engine)

Revision ID: 0044_upsell
Revises: 0043_rhythm
Create Date: 2026-07-13

Additive:
  • ladder_state(account_id, fan_id)  — selling state machine per fan.
      NOTE offers_paused_until lives HERE, not on fans.automation_paused_until:
      that column is shared by every automation, so routing a decline into it
      would blank the fan out of welcome/followup/mass as well — a 3-day
      cross-automation blackout with no UI. A poverty plea stops selling, not talking.
  • ladder_quote(...)  — one row per price we QUOTED (the conversion log + the
      price experiment's instrument). pre_clamp_cents/clamped_by exist so quotes
      truncated at the library ceiling can be EXCLUDED from the price analysis
      instead of silently biasing it.
  • pending_offer(account_id, fan_id) — offers the gate blocked, parked until the
      fan's next qualifying inbound (a gate that can only delete sends cannot beat
      its own revenue metric).

Ships inert: with selling.qualification_gate_enabled false, none of these tables
is written and ppv_send/ai_chatter behave byte-identically to today.

Additive + idempotent (inspector-guarded).
"""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0044_upsell"
down_revision: str | Sequence[str] | None = "0043_rhythm"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "ladder_state" not in tables:
        op.create_table(
            "ladder_state",
            sa.Column("account_id", sa.String, primary_key=True),
            sa.Column("fan_id", sa.BigInteger, primary_key=True),
            sa.Column("status", sa.String, nullable=False, server_default="idle"),
            sa.Column("rung_index", sa.Integer, nullable=False, server_default="0"),
            sa.Column("last_ask_at", sa.DateTime, nullable=True),
            sa.Column("last_paid_at", sa.DateTime, nullable=True),
            sa.Column("session_idle_at", sa.DateTime, nullable=True),
            sa.Column("offers_paused_until", sa.DateTime, nullable=True),
            sa.Column("daily_ask_cents", sa.Integer, nullable=False, server_default="0"),
            sa.Column("daily_day", sa.String, nullable=True),
            sa.Column("hot_until", sa.DateTime, nullable=True),
            sa.Column("unpaid_rungs", sa.Integer, nullable=False, server_default="0"),
            sa.Column("updated_at", sa.DateTime, nullable=True),
        )
        op.create_index("ix_ladder_state_status", "ladder_state", ["account_id", "status"])

    if "ladder_quote" not in tables:
        op.create_table(
            "ladder_quote",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("account_id", sa.String, nullable=False),
            sa.Column("fan_id", sa.BigInteger, nullable=False),
            sa.Column("rung_index", sa.Integer, nullable=False, server_default="0"),
            sa.Column("media_key", sa.String, nullable=False),
            sa.Column("item_id", sa.Integer, nullable=True),
            sa.Column("band_lo", sa.Integer, nullable=False, server_default="0"),
            sa.Column("band_hi", sa.Integer, nullable=False, server_default="0"),
            sa.Column("base_cents", sa.Integer, nullable=False, server_default="0"),
            sa.Column("arm_mult", sa.Float, nullable=False, server_default="1.0"),
            sa.Column("price_cents", sa.Integer, nullable=False, server_default="0"),
            sa.Column("pre_clamp_cents", sa.Integer, nullable=False, server_default="0"),
            sa.Column("clamped_by", sa.String, nullable=True),
            sa.Column("kind", sa.String, nullable=False, server_default="rung"),
            sa.Column("message_id", sa.BigInteger, nullable=True),
            sa.Column("sent_at", sa.DateTime, nullable=True),
            sa.Column("paid", sa.Boolean, nullable=False, server_default="0"),
            sa.Column("paid_at", sa.DateTime, nullable=True),
        )
        op.create_index("ix_ladder_quote_fan", "ladder_quote", ["account_id", "fan_id"])
        op.create_index("ix_ladder_quote_media", "ladder_quote",
                        ["account_id", "fan_id", "media_key"])

    if "pending_offer" not in tables:
        op.create_table(
            "pending_offer",
            sa.Column("account_id", sa.String, primary_key=True),
            sa.Column("fan_id", sa.BigInteger, primary_key=True),
            sa.Column("media_key", sa.String, nullable=True),
            sa.Column("item_id", sa.Integer, nullable=True),
            sa.Column("created_at", sa.DateTime, nullable=True),
            sa.Column("expires_at", sa.DateTime, nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "pending_offer" in tables:
        op.drop_table("pending_offer")
    if "ladder_quote" in tables:
        op.drop_index("ix_ladder_quote_media", table_name="ladder_quote")
        op.drop_index("ix_ladder_quote_fan", table_name="ladder_quote")
        op.drop_table("ladder_quote")
    if "ladder_state" in tables:
        op.drop_index("ix_ladder_state_status", table_name="ladder_state")
        op.drop_table("ladder_state")
