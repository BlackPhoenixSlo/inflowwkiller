"""wall_media.price_cents — capture the price of paid wall posts

Revision ID: 0040_wall_media_price
Revises: 0039_user_is_admin
Create Date: 2026-07-02

One additive change: `wall_media.price_cents` INTEGER NOT NULL DEFAULT 0.

The wall-media walker (/admin/vault/wall-media) now records the price of the
post each media appeared in (OF's post object carries a top-level `price` in
dollars for paid/PPV wall posts). The vault picker renders this as a
"WALL $X.XX" badge so a chatter can see the item is already public PPV and at
what price. 0 = free wall post (or price not yet observed).

Additive + NOT-NULL-with-default + idempotent (inspector-guarded, house
convention so a create_all-built DB whose alembic_version lags doesn't trip).
Existing rows default to 0 and pick up a real price on the next forward /
force re-scan, whose upsert MAXes the price in.
"""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0040_wall_media_price"
down_revision: str | Sequence[str] | None = "0039_user_is_admin"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    cols = {c["name"] for c in inspector.get_columns("wall_media")}
    if "price_cents" not in cols:
        op.add_column(
            "wall_media",
            sa.Column("price_cents", sa.Integer, nullable=False, server_default="0"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    cols = {c["name"] for c in inspector.get_columns("wall_media")}
    if "price_cents" in cols:
        op.drop_column("wall_media", "price_cents")
