"""resolution_log + make_right_config_json — the Resolution Agent (make_right)

Revision ID: 0050_resolution_log
Revises: 0049_vault_ai_actions
Create Date: 2026-07-21

Two additive changes for the make_right ("Resolution Agent") safety net:
  1. `account_ai_config.make_right_config_json` — the per-account config blob
     (DISABLED + preview-only by default).
  2. `resolution_log` — one row per detected wrong-content incident (headline:
     charged twice for the same content). `incident_key` (unique per account) is
     the idempotency key; the "up to twice per fan" cap is a COUNT of
     status='resolved' rows for the fan.

Additive + idempotent (inspector-guarded, house convention).
"""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0050_resolution_log"
down_revision: str | Sequence[str] | None = "0049_vault_ai_actions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    names = inspector.get_table_names()

    # (1) additive column on account_ai_config — guard on existing columns.
    if "account_ai_config" in names:
        cols = {c["name"] for c in inspector.get_columns("account_ai_config")}
        if "make_right_config_json" not in cols:
            op.add_column(
                "account_ai_config",
                sa.Column("make_right_config_json", sa.Text(), nullable=True),
            )

    # (2) the resolution_log ledger.
    if "resolution_log" not in names:
        op.create_table(
            "resolution_log",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("account_id", sa.String(),
                      sa.ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False),
            sa.Column("fan_id", sa.BigInteger(), nullable=False),
            sa.Column("incident_key", sa.String(), nullable=False),
            sa.Column("kind", sa.String(), nullable=False),
            sa.Column("detected_at", sa.DateTime(), nullable=False,
                      server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.Column("status", sa.String(), nullable=False,
                      server_default="detected"),
            sa.Column("remediation_json", sa.Text(), nullable=True),
            sa.Column("resolved_at", sa.DateTime(), nullable=True),
            sa.UniqueConstraint("account_id", "incident_key",
                                name="uq_resolution_incident"),
        )
        op.create_index("ix_resolution_fan", "resolution_log",
                        ["account_id", "fan_id", "status"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    names = inspector.get_table_names()
    if "resolution_log" in names:
        op.drop_table("resolution_log")
    if "account_ai_config" in names:
        cols = {c["name"] for c in inspector.get_columns("account_ai_config")}
        if "make_right_config_json" in cols:
            op.drop_column("account_ai_config", "make_right_config_json")
