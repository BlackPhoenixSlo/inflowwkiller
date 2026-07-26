"""translation_cache — durable store for the 🌐 chat-translate toggle

Revision ID: 0055_translation_cache
Revises: 0054_persona_claims
Create Date: 2026-07-25

One NEW table, no changes to any existing one — so unlike the additive-column
migrations either side of it this needs no batch mode and can't touch a row.
`create_all()` already materializes it on a fresh or catch-up boot (init_db in
db/engine.py); this file exists to keep the alembic chain linear for the
alembic-managed copies.

  translation_cache   text_hash (PK, md5 of target + NUL + source text)
                      target, src_lang, source_text, translated, created_at

Why a table at all: POST /admin/translate previously cached only in process
memory (8k entries, gone on every deploy) behind a per-tab JS Map (gone on every
reload). Chat text repeats hard — 193k rendered texts over 30 days of prod
collapse to 87k distinct — so a TEXT-keyed durable cache turns a ~1.5s Google
round trip per 40 fresh bubbles into a single indexed SELECT, shared across every
chatter and tab. Held at 50k rows by an age-ordered prune (translate_api._prune).

Inspector-guarded + idempotent (house convention so a create_all-built DB whose
alembic_version lags doesn't trip).
"""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0055_translation_cache"
down_revision: str | Sequence[str] | None = "0054_persona_claims"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "translation_cache" not in inspector.get_table_names():
        op.create_table(
            "translation_cache",
            sa.Column("text_hash", sa.String, primary_key=True),
            sa.Column("target", sa.String, nullable=False),
            sa.Column("src_lang", sa.String, nullable=False, server_default="und"),
            sa.Column("source_text", sa.Text, nullable=False),
            sa.Column("translated", sa.Text, nullable=False),
            sa.Column("created_at", sa.DateTime, nullable=False,
                      server_default=sa.text("CURRENT_TIMESTAMP")),
        )
    # Re-inspect: the instance above cached the pre-CREATE table list, so asking
    # it about the table we just made would report it absent.
    existing = {i["name"] for i in sa.inspect(bind).get_indexes("translation_cache")}
    if "ix_translation_cache_created" not in existing:
        op.create_index("ix_translation_cache_created", "translation_cache", ["created_at"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "translation_cache" in inspector.get_table_names():
        op.drop_table("translation_cache")
