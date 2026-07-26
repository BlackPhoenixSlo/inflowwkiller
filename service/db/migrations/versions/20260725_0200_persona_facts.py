"""persona_facts — structured creator canon pinned into every chat prompt

Revision ID: 0053_persona_facts
Revises: 0052_message_image_desc
Create Date: 2026-07-25

One additive, NULLABLE column (no server_default — NULL is the semantic default,
"never enriched"). init_db's ADD-COLUMN catch-up materializes it on prod with zero
risk of the raw-DEFAULT-interpolation footgun; this file keeps the chain linear.

  account_ai_config.persona_facts_json
        JSON dict of the creator's own facts — {age, born_city, born_country,
        home_city, home_country, upbringing, living_situation, job, family, pets,
        relationship}. All keys optional; empty/absent renders NOTHING into the
        prompt, so an un-enriched account is byte-identical to today.

Why: `persona` is one free-text blob (202-882 chars on live accounts) and the gaps
in it are exactly what fans probe. the graded vault's said "Born and raised in Argentina"
with no city, so a fan asking where she grew up got an improvisation — and a
966-turn thread walked Argentina → Chile → Córdoba before he stopped believing
her. Named empty slots are what the enrich button can see and fill; prose has none.

Hand-written op.add_column (NEVER --autogenerate). Inspector-guarded + idempotent
(house convention so a create_all-built DB whose alembic_version lags doesn't trip).
"""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0053_persona_facts"
down_revision: str | Sequence[str] | None = "0052_message_image_desc"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    cols = {c["name"] for c in inspector.get_columns("account_ai_config")}
    if "persona_facts_json" not in cols:
        op.add_column("account_ai_config",
                      sa.Column("persona_facts_json", sa.Text, nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    cols = {c["name"] for c in inspector.get_columns("account_ai_config")}
    if "persona_facts_json" in cols:
        op.drop_column("account_ai_config", "persona_facts_json")
