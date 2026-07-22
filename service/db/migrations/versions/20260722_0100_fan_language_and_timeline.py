"""fan_language_and_timeline — per-account + per-fan language, and a dated timeline

Revision ID: 0051_fan_language_and_timeline
Revises: 0050_resolution_log
Create Date: 2026-07-22

NB: chains after 0050_resolution_log (the make_right Resolution Agent migration), not
0049 — both were authored the same day; language sits on top so the alembic chain stays
linear (no fork). Prod self-heals via init_db regardless of chain order.

Four additive, NULLABLE columns (no server_default — NULL is the semantic default,
resolved in code, so init_db's ADD-COLUMN catch-up materializes every one of them on
prod with zero risk of the raw-DEFAULT-interpolation footgun):

  account_ai_config.language          ISO 639-1 the creator writes in (NULL == "en").
                                      Gates output language AND which guard vocab runs.
  fans.language                       per-fan override / detection seam (NULL == account).
  fans.language_source                'manual' when an operator set fans.language.
  fans.recent_events_timeline         JSON [{date, event}] dated timeline (NULL == none).

Hand-written op.add_column (NEVER --autogenerate: render_as_batch would rewrite the
whole 1.5 GB `fans` table). Inspector-guarded + idempotent (house convention so a
create_all-built DB whose alembic_version lags doesn't trip).
"""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0051_fan_language_and_timeline"
down_revision: str | Sequence[str] | None = "0050_resolution_log"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    cfg_cols = {c["name"] for c in inspector.get_columns("account_ai_config")}
    if "language" not in cfg_cols:
        op.add_column("account_ai_config", sa.Column("language", sa.String, nullable=True))

    fan_cols = {c["name"] for c in inspector.get_columns("fans")}
    if "language" not in fan_cols:
        op.add_column("fans", sa.Column("language", sa.String, nullable=True))
    if "language_source" not in fan_cols:
        op.add_column("fans", sa.Column("language_source", sa.String, nullable=True))
    if "recent_events_timeline" not in fan_cols:
        op.add_column("fans", sa.Column("recent_events_timeline", sa.Text, nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    fan_cols = {c["name"] for c in inspector.get_columns("fans")}
    for col in ("recent_events_timeline", "language_source", "language"):
        if col in fan_cols:
            op.drop_column("fans", col)

    cfg_cols = {c["name"] for c in inspector.get_columns("account_ai_config")}
    if "language" in cfg_cols:
        op.drop_column("account_ai_config", "language")
