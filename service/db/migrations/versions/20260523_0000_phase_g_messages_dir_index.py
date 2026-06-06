"""phase_g_messages_dir_index

Revision ID: 0017_phase_g_messages_dir_index
Revises: 0016_phase_f_attr_view_include_pending
Create Date: 2026-05-23

Adds a composite index on (account_id, direction, created_at DESC) to
support the extended /admin/paid-messages endpoint's worst-case query —
account-scoped, direction=any, type=all — over a multi-day window. The
existing `ix_messages_account_fan_time` covers per-fan queries but not
the wider scan that drops both the `direction='out'` and `price_cents>0`
filters once the All Messages tab lands.

SQLite doesn't honor index column ordering (DESC is parsed and ignored
for query planning purposes pre-3.45), but we declare it anyway for
Postgres portability.
"""
from __future__ import annotations

from typing import Sequence

from alembic import op


revision: str = "0017_phase_g_messages_dir_index"
down_revision: str | Sequence[str] | None = "0016_phase_f_attr_view_include_pending"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_messages_dir_created",
        "messages",
        ["account_id", "direction", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_messages_dir_created", table_name="messages")
