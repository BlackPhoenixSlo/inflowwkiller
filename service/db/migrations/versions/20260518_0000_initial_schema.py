"""initial schema

Revision ID: 0001_initial
Revises:
Create Date: 2026-05-18

Hand-written because Alembic's --autogenerate hits a Python 3.13 +
SQLAlchemy 2.0.35 recursion bug on this wide schema (35 tables with
composite PKs + partial indexes). Since this is the *first* migration —
there's no diff to compute — we just delegate to
`Base.metadata.create_all` and let SQLAlchemy emit the right DDL for
whichever dialect we're pointed at.

Subsequent migrations (column adds, etc.) will be auto-generatable
because the diff will be small enough that the recursion limit doesn't
matter.
"""
from __future__ import annotations

from typing import Sequence

from alembic import op

from service.db.models import Base


revision: str = "0001_initial"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind)


def downgrade() -> None:
    bind = op.get_bind()
    Base.metadata.drop_all(bind=bind)
