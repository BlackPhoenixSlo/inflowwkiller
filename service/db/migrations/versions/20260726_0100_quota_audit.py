"""quota_audit — item 21c's daily-quota decision ledger

Revision ID: 0056_quota_audit
Revises: 0055_translation_cache
Create Date: 2026-07-26

One NEW table, no changes to any existing one, so no batch mode and no row can be
touched. `create_all()` already materializes it on a fresh or catch-up boot
(init_db in db/engine.py); this file exists to keep the alembic chain linear for
the alembic-managed copies.

  quota_audit   (account_id, fan_id, day, reason, enforced)  ← FIVE-column PK
                n, quota, used, runway_left, wait_h, dry_h, updated_at
                + ix_quota_audit_day (account_id, day, reason), matching the model

`enforced` IS PART OF THE KEY, and that is the whole reason this file cannot be a
plain create-if-absent. The rollout is "shadow for a week, then flip
`daily_quota_enforce`", and that flip happens MID-DAY. With `enforced` as an
ordinary column it was last-write-wins beside an ACCUMULATING counter (`n`), so
the first enforced sweep of the day absorbed that morning's shadow evaluations
into the same row and relabelled every one of them as enforced — destroying the
shadow/served distinction on precisely the one day it matters most.

The table shipped inside this branch with the 4-column key before that was found,
so a box that booted the intermediate commit has the wrong shape on disk, and
neither `create_all()` (skips existing tables) nor init_db's additive catch-up
(only ever ADDs columns) can fix a PRIMARY KEY. SQLite cannot ALTER one either.
Hence the rename-create-copy-drop below, guarded so it only runs where the shape
is actually wrong.

Rows are safe to carry across: the copy re-aggregates by the NEW key, summing `n`
per group. On a shadow-only table every row already has enforced=0, so the sum is
a no-op; on a table that saw the flip mid-day it is the closest honest reading
available — the two labels were already conflated by the old key, and summing
keeps the evaluation count right even though it cannot un-mix them.

Inspector-guarded + idempotent (house convention so a create_all-built DB whose
alembic_version lags doesn't trip).
"""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0056_quota_audit"
down_revision: str | Sequence[str] | None = "0055_translation_cache"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PK = ["account_id", "fan_id", "day", "reason", "enforced"]


def _create(name: str = "quota_audit") -> None:
    op.create_table(
        name,
        sa.Column("account_id", sa.String, primary_key=True, nullable=False),
        sa.Column("fan_id", sa.BigInteger, primary_key=True, nullable=False),
        sa.Column("day", sa.String, primary_key=True, nullable=False),
        sa.Column("reason", sa.String, primary_key=True, nullable=False),
        sa.Column("enforced", sa.Boolean, primary_key=True, nullable=False,
                  server_default=sa.text("0")),
        sa.Column("n", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("quota", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("used", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("runway_left", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("wait_h", sa.Float),
        sa.Column("dry_h", sa.Float),
        sa.Column("updated_at", sa.DateTime, nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP")),
    )


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "quota_audit" not in inspector.get_table_names():
        _create()
        op.create_index("ix_quota_audit_day", "quota_audit",
                        ["account_id", "day", "reason"])
        return

    # Table exists. Is `enforced` already in the key? If so there is nothing to do
    # — this migration is then just recording that the chain has reached here.
    pk = set(sa.inspect(bind).get_pk_constraint("quota_audit").get(
        "constrained_columns") or [])
    if pk == set(_PK):
        existing = {i["name"] for i in sa.inspect(bind).get_indexes("quota_audit")}
        if "ix_quota_audit_day" not in existing:
            op.create_index("ix_quota_audit_day", "quota_audit",
                            ["account_id", "day", "reason"])
        return

    # Wrong shape: SQLite cannot ALTER a PRIMARY KEY, so rebuild and re-aggregate.
    op.rename_table("quota_audit", "quota_audit_old")
    _create()
    op.execute(
        """
        INSERT INTO quota_audit
            (account_id, fan_id, day, reason, enforced, n, quota, used,
             runway_left, wait_h, dry_h, updated_at)
        SELECT account_id, fan_id, day, reason,
               COALESCE(enforced, 0),
               SUM(COALESCE(n, 0)),
               MAX(COALESCE(quota, 0)), MAX(COALESCE(used, 0)),
               MAX(COALESCE(runway_left, 0)),
               MAX(wait_h), MAX(dry_h), MAX(updated_at)
          FROM quota_audit_old
         GROUP BY account_id, fan_id, day, reason, COALESCE(enforced, 0)
        """
    )
    op.drop_table("quota_audit_old")
    op.create_index("ix_quota_audit_day", "quota_audit",
                    ["account_id", "day", "reason"])


def downgrade() -> None:
    bind = op.get_bind()
    if "quota_audit" in sa.inspect(bind).get_table_names():
        op.drop_table("quota_audit")
