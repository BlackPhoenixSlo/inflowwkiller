"""nudge_online — nudge_state table + account_ai_config.nudge_config_json

Revision ID: 0027_nudge_online
Revises: 0026_llm_provider
Create Date: 2026-06-05

The "message a fan when they come online" automation (library/NUDGE_ONLINE_PLAN.md).

  nudge_state        — NEW per-(account, fan) table: poll-and-diff online
                       timestamps (last_seen_online_at), the re-engagement cap
                       (last_nudged_at / nudge_count), no-reply backoff
                       (no_reply_streak), the pending fire-job dedup latch
                       (pending_job_id), and repeat_mode rotation
                       (last_variation_idx / last_slot). Dedicated so it never
                       collides with fans.last_online_at (WS-pump owned).
  account_ai_config  — + nudge_config_json (per-account nudge config; NULL →
                       the automation's built-in defaults).

Idempotent: guarded by inspector checks (house convention, mirrors 0025/0026).
"""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0027_nudge_online"
down_revision: str | Sequence[str] | None = "0026_llm_provider"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = set(inspector.get_table_names())

    if "nudge_state" not in existing:
        op.create_table(
            "nudge_state",
            sa.Column("account_id", sa.String, nullable=False),
            sa.Column("fan_id", sa.BigInteger, nullable=False),
            sa.Column("last_seen_online_at", sa.DateTime, nullable=True),
            sa.Column("last_nudged_at", sa.DateTime, nullable=True),
            sa.Column("nudge_count", sa.Integer, nullable=False, server_default=sa.text("0")),
            sa.Column("no_reply_streak", sa.Integer, nullable=False, server_default=sa.text("0")),
            sa.Column("pending_job_id", sa.Integer, nullable=True),
            sa.Column("last_variation_idx", sa.Integer, nullable=True),
            sa.Column("last_slot", sa.String, nullable=True),
            sa.Column(
                "updated_at", sa.DateTime,
                nullable=False, server_default=sa.text("CURRENT_TIMESTAMP"),
            ),
            sa.PrimaryKeyConstraint("account_id", "fan_id", name="pk_nudge_state"),
        )
        op.create_index(
            "ix_nudge_state_account", "nudge_state",
            ["account_id", "last_seen_online_at"],
        )

    ai_cols = {c["name"] for c in inspector.get_columns("account_ai_config")}
    if "nudge_config_json" not in ai_cols:
        op.add_column(
            "account_ai_config",
            sa.Column("nudge_config_json", sa.Text, nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = set(inspector.get_table_names())

    ai_cols = {c["name"] for c in inspector.get_columns("account_ai_config")}
    if "nudge_config_json" in ai_cols:
        op.drop_column("account_ai_config", "nudge_config_json")

    if "nudge_state" in existing:
        op.drop_table("nudge_state")
