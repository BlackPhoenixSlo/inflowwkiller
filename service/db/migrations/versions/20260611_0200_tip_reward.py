"""tip_reward — fan-tip image reward automation

Revision ID: 0033_tip_reward
Revises: 0032_automation_kind
Create Date: 2026-06-11

Two additive changes for the "send vault images when a fan tips" automation:

  account_ai_config — + tip_reward_config_json TEXT  (per-account config:
                      enabled, $/image, min/max images, caption, rolling-window
                      hours, folder tiers). NULL = automation's built-in
                      defaults (DISABLED, empty folders).

  tip_reward_log    — NEW table. One row per tip the automation acted on, PK
                      (account_id, tip_message_id) → INSERT-OR-IGNORE gives
                      exactly-one-reward-per-tip idempotency (a webhook can
                      replay a tip) + an audit trail.

Additive + nullable + idempotent (inspector-guarded, house convention).
"""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0033_tip_reward"
down_revision: str | Sequence[str] | None = "0032_automation_kind"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    cfg_cols = {c["name"] for c in inspector.get_columns("account_ai_config")}
    if "tip_reward_config_json" not in cfg_cols:
        op.add_column(
            "account_ai_config",
            sa.Column("tip_reward_config_json", sa.Text, nullable=True),
        )

    if "tip_reward_log" not in inspector.get_table_names():
        op.create_table(
            "tip_reward_log",
            sa.Column("account_id", sa.String, primary_key=True),
            sa.Column("tip_message_id", sa.BigInteger, primary_key=True),
            sa.Column("fan_id", sa.BigInteger, nullable=False),
            sa.Column("tip_cents", sa.Integer, nullable=False, server_default="0"),
            sa.Column("basis_cents", sa.Integer, nullable=False, server_default="0"),
            sa.Column("tier_name", sa.String, nullable=True),
            sa.Column("images_sent", sa.Integer, nullable=False, server_default="0"),
            sa.Column("reward_message_id", sa.BigInteger, nullable=True),
            sa.Column("created_at", sa.DateTime, nullable=False),
        )
        op.create_index(
            "ix_tip_reward_log_fan",
            "tip_reward_log",
            ["account_id", "fan_id", "created_at"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "tip_reward_log" in inspector.get_table_names():
        idx = {ix["name"] for ix in inspector.get_indexes("tip_reward_log")}
        if "ix_tip_reward_log_fan" in idx:
            op.drop_index("ix_tip_reward_log_fan", table_name="tip_reward_log")
        op.drop_table("tip_reward_log")

    cfg_cols = {c["name"] for c in inspector.get_columns("account_ai_config")}
    if "tip_reward_config_json" in cfg_cols:
        op.drop_column("account_ai_config", "tip_reward_config_json")
