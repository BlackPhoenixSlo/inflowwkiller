"""webhook dispatch — account_ai_config.webhook_config_json

Revision ID: 0029_webhook_dispatch
Revises: 0028_chatter_access
Create Date: 2026-06-06

Wave 7 webhook-priority dispatch (plan/AUTOMATIONS_BUILD_PROMPTS.md §WAVE 7).

  account_ai_config  — + webhook_config_json: per-account gate for real-time,
                       event-driven dispatch off inbound WS events. JSON
                       {"enabled": bool}. NULL/absent → OFF (the safe default —
                       no account reacts in real time until explicitly enabled;
                       a global env kill-switch W7_WEBHOOK_DISPATCH_DISABLED
                       overrides). Kept separate from nudge_config_json to avoid
                       the shallow-merge collision in nudge_online's config
                       loader.

Additive + nullable + no backfill — the safest migration shape. Idempotent:
guarded by an inspector check (house convention, mirrors 0027 nudge_online).
"""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0029_webhook_dispatch"
down_revision: str | Sequence[str] | None = "0028_chatter_access"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    ai_cols = {c["name"] for c in inspector.get_columns("account_ai_config")}
    if "webhook_config_json" not in ai_cols:
        op.add_column(
            "account_ai_config",
            sa.Column("webhook_config_json", sa.Text, nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    ai_cols = {c["name"] for c in inspector.get_columns("account_ai_config")}
    if "webhook_config_json" in ai_cols:
        op.drop_column("account_ai_config", "webhook_config_json")
