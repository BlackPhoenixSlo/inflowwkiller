"""account_health — pause a dead-session account's automations until it recovers

Revision ID: 0041_account_health
Revises: 0040_wall_media_price
Create Date: 2026-07-10

One additive table: `account_health` (account_id PK, session_dead_at,
session_dead_reason, last_probe_at).

When OF rejects an account's stored session ("Wrong user." — the creator got
logged out / re-linked elsewhere), the account is flagged here and the
automation executor skips every run for it; a periodic `client.me()` probe or
a session re-capture clears the flag. See service/account_health.py.

Additive + idempotent (inspector-guarded, house convention so a
create_all-built DB whose alembic_version lags doesn't trip).
"""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0041_account_health"
down_revision: str | Sequence[str] | None = "0040_wall_media_price"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "account_health" not in inspector.get_table_names():
        op.create_table(
            "account_health",
            sa.Column("account_id", sa.String, primary_key=True),
            sa.Column("session_dead_at", sa.DateTime, nullable=True),
            sa.Column("session_dead_reason", sa.String, nullable=True),
            sa.Column("last_probe_at", sa.DateTime, nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "account_health" in inspector.get_table_names():
        op.drop_table("account_health")
