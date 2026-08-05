"""customs owed ledger moves from the nickname to its own column

The " Custom" suffix on `fans.custom_nickname` could not survive: that column has
four other writers, and `of_ai_chat._maybe_push_nickname` (reached from ai_chatter
every 60s) rebuilds the name from structured facts and drops the suffix — in the DB
and on OnlyFans. `customs_watch` only runs every 900s, so the ledger was erased
about a minute after it was written. Prod: 204 watcher runs, zero fans ever marked.

NO BACKFILL, DELIBERATELY. Two reasons:
  • On the four accounts that sell customs, no fan carries the suffix — there is
    nothing to migrate.
  • The one fan platform-wide whose nickname ends in "Custom" is
    114838426/27550310, "Kyle 3/13 Photoset Custom" — a human operator's note about
    a PHOTOSET, on an account with no customs automation at all. Reading it as an
    owed voice note would invent a debt that never existed.

Revision ID: 0059_customs_owed_column
Revises: 0058_ghost_cycle
Create Date: 2026-08-05
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0059_customs_owed_column"
down_revision: str | Sequence[str] | None = "0058_ghost_cycle"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("fans", sa.Column("customs_owed_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("fans", "customs_owed_at")
