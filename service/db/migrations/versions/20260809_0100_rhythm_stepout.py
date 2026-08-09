"""rhythm_stepout — the exchange counter behind "she goes out for an hour"

Revision ID: 0061_rhythm_stepout
Revises: 0060_day_log
Create Date: 2026-08-09

Additive (two columns):
  • rhythm_state.stepout_mark   — her whole-thread reply count as of the LAST
    step-out.
  • rhythm_state.stepout_target — the interval (7-15 exchanges) drawn for the
    NEXT one.

"Is she due" is then `total_out_n - stepout_mark >= stepout_target`, compared
against a count the thread itself carries. Deliberately a MARK and not a
countdown, for the same reason `ghost_anchor` is an anchor and not a stage
pointer: a tick that fails to run can leave a counter stale, and every later
interval silently inherits the drift.

Both NULL on every existing row, and NULL reads as "she has never stepped out",
which arms the first one off her current reply count. So an account that never
turns the feature on is byte-identical to today.

Additive + idempotent (inspector-guarded, house convention so a create_all-built
DB whose alembic_version lags doesn't trip).
"""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0061_rhythm_stepout"
down_revision: str | Sequence[str] | None = "0060_day_log"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _cols(inspector, table: str) -> set[str]:
    if table not in inspector.get_table_names():
        return set()
    return {c["name"] for c in inspector.get_columns(table)}


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    rs = _cols(inspector, "rhythm_state")
    if not rs:
        return
    if "stepout_mark" not in rs:
        op.add_column("rhythm_state", sa.Column("stepout_mark", sa.Integer, nullable=True))
    if "stepout_target" not in rs:
        op.add_column("rhythm_state", sa.Column("stepout_target", sa.Integer, nullable=True))


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    rs = _cols(inspector, "rhythm_state")
    if "stepout_target" in rs:
        op.drop_column("rhythm_state", "stepout_target")
    if "stepout_mark" in rs:
        op.drop_column("rhythm_state", "stepout_mark")
