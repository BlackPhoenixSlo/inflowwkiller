"""account_voice — whose voice does this account write in

Revision ID: 0057_account_voice
Revises: 0056_quota_audit
Create Date: 2026-08-02

ONE nullable column on an existing table: `account_ai_config.voice`.

  NULL / '' / anything unrecognised  →  'her'  (every account that exists today)
  'him'                              →  the male lane

The normalisation lives in `automations/_voice.norm_voice`, NOT in a CHECK
constraint or an enum: a corrupt or hand-edited value must degrade to the
shipped female lane rather than raise on a send path, and the safe direction of
that failure is "stays a girl". A DB-level enum would turn a bad write into a
500 on the one code path that must never fail.

Nullable with NO server default, deliberately. A default of 'her' would rewrite
every existing row to say something they currently do not say, and the whole
safety argument for this lane is that untouched accounts render a byte-identical
prompt — which is asserted in tests/test_voice_lane.py, not assumed. NULL means
"nobody has answered this question for this account", which is the truth.

Inspector-guarded + idempotent (house convention, so a create_all-built DB whose
alembic_version lags doesn't trip).
"""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0057_account_voice"
down_revision: str | Sequence[str] | None = "0056_quota_audit"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    cols = {c["name"] for c in inspector.get_columns("account_ai_config")}
    if "voice" not in cols:
        op.add_column("account_ai_config",
                      sa.Column("voice", sa.String, nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    cols = {c["name"] for c in inspector.get_columns("account_ai_config")}
    if "voice" in cols:
        op.drop_column("account_ai_config", "voice")
