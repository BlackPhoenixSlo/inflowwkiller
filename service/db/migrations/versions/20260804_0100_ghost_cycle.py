"""ghost_cycle — the per-fan scheduled-silence anchor

Revision ID: 0058_ghost_cycle
Revises: 0057_account_voice
Create Date: 2026-08-04

Additive (one column):
  • rhythm_state.ghost_anchor — when this fan's repeating chat/dark cycle
    started. The ONLY state the ghost cycle keeps: the stage is derived by
    modulo off this timestamp (`_ghost.window`), so there is no pointer a
    missed tick could leave stale.

Ships inert: nothing reads or writes it unless `rhythm_ghost_enabled` is on
(and that key itself is inert without `rhythm_enabled`), so ai_chatter behaves
byte-identically to today.

Additive + idempotent (inspector-guarded, house convention so a create_all-built
DB whose alembic_version lags doesn't trip).
"""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0058_ghost_cycle"
down_revision: str | Sequence[str] | None = "0057_account_voice"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _cols(inspector, table: str) -> set[str]:
    if table not in inspector.get_table_names():
        return set()
    return {c["name"] for c in inspector.get_columns(table)}


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    rs = _cols(inspector, "rhythm_state")
    if rs and "ghost_anchor" not in rs:
        op.add_column("rhythm_state", sa.Column("ghost_anchor", sa.DateTime, nullable=True))


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "ghost_anchor" in _cols(inspector, "rhythm_state"):
        op.drop_column("rhythm_state", "ghost_anchor")
