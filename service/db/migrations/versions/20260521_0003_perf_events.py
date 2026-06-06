"""client perf-log ingest table

Revision ID: 0004_perf_events
Revises: 0003_wall_media_scan
Create Date: 2026-05-21

Backing store for /admin/perflog/ingest — append-only telemetry rows
batched from the frontend perfLog client. Pruned by the relay's
background evictor; safe to wipe at any time (no FKs depend on it).
"""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0004_perf_events"
down_revision: str | Sequence[str] | None = "0003_wall_media_scan"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "perf_events",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("tab_id", sa.String, nullable=False),
        sa.Column("parent_tab_id", sa.String, nullable=True),
        sa.Column("op_id", sa.String, nullable=False),
        sa.Column("kind", sa.String, nullable=False),
        sa.Column("phase", sa.String, nullable=False),
        sa.Column("client_ts_ms", sa.BigInteger, nullable=False),
        sa.Column("received_at", sa.DateTime, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("employee_id", sa.Integer, nullable=True),
        sa.Column("account_id", sa.String, nullable=True),
        sa.Column("meta_json", sa.Text, nullable=True),
    )
    op.create_index("ix_perf_events_received_at", "perf_events", ["received_at"])
    op.create_index("ix_perf_events_tab_kind", "perf_events", ["tab_id", "kind"])
    op.create_index("ix_perf_events_op", "perf_events", ["op_id"])


def downgrade() -> None:
    op.drop_index("ix_perf_events_op", table_name="perf_events")
    op.drop_index("ix_perf_events_tab_kind", table_name="perf_events")
    op.drop_index("ix_perf_events_received_at", table_name="perf_events")
    op.drop_table("perf_events")
