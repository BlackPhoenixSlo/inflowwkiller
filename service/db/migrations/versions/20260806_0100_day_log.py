"""day_log — the creator's generated day, one row per account, overwritten daily

Revision ID: 0060_day_log
Revises: 0059_customs_owed_column
Create Date: 2026-08-06

ONE additive, NULLABLE column (no server_default — NULL is the semantic default and
is resolved in code, so init_db's ADD-COLUMN catch-up materializes it on prod with
zero risk of the raw-DEFAULT-interpolation footgun):

  account_ai_config.day_log_json   TODAY's day as {"date","weekday","beats","covers"}.
                                   NULL == no day log; every renderer in
                                   automations/_daylog returns "" for it, so an
                                   un-generated account's chat prompt is byte-identical
                                   to what shipped before the feature existed.

Why a column and not a table: only TODAY is ever read. Yesterday's day has no reader,
and an append-only history in a config column is exactly how `grok_calls` grew to 52%
of the prod DB. The row is overwritten when the creator-LOCAL date rolls over.

Hand-written op.add_column (NEVER --autogenerate). Inspector-guarded + idempotent,
house convention, so a create_all-built DB whose alembic_version lags doesn't trip.
`account_ai_config` is small (one row per account, 17 rows on prod) so this is
instant and needs no batch mode.
"""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0060_day_log"
down_revision: str | Sequence[str] | None = "0059_customs_owed_column"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    cfg_cols = {c["name"] for c in inspector.get_columns("account_ai_config")}
    if "day_log_json" not in cfg_cols:
        op.add_column("account_ai_config", sa.Column("day_log_json", sa.Text, nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    cfg_cols = {c["name"] for c in inspector.get_columns("account_ai_config")}
    if "day_log_json" in cfg_cols:
        op.drop_column("account_ai_config", "day_log_json")
