"""persona_claims — the per-fan record of what SHE told HIM about herself

Revision ID: 0054_persona_claims
Revises: 0053_persona_facts
Create Date: 2026-07-25

One additive, NULLABLE column (no server_default — NULL is the semantic default,
"this fan has heard nothing that deviates from canon", which is most fans).
init_db's ADD-COLUMN catch-up materializes it on prod; this keeps the chain linear.

  fans.persona_claims_json
        JSON list of {topic, claim, at} — what she said about HERSELF to THIS fan,
        recorded only where it deviates from account canon (persona_facts_json) or
        covers something canon didn't. Latest-wins per topic, capped at 20.

Why per-fan when canon already exists: a claim already made cannot be retracted.
Measured on prod, one 966-turn thread went "no soy chilena, soy argentina de
verdad" → "y sí, estoy acá en Chile" → "en córdoba"; correcting that fan back to
Argentina mid-thread would land as the lie, not the fix. Canon keeps NEW threads
right; this column keeps EXISTING threads coherent.

Hand-written op.add_column (NEVER --autogenerate: render_as_batch would rewrite
the whole `fans` table). Inspector-guarded + idempotent (house convention).
"""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0054_persona_claims"
down_revision: str | Sequence[str] | None = "0053_persona_facts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    cols = {c["name"] for c in inspector.get_columns("fans")}
    if "persona_claims_json" not in cols:
        op.add_column("fans", sa.Column("persona_claims_json", sa.Text, nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    cols = {c["name"] for c in inspector.get_columns("fans")}
    if "persona_claims_json" in cols:
        op.drop_column("fans", "persona_claims_json")
