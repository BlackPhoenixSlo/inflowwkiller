"""funnel_account_media — per-(funnel, account) media binding

Revision ID: 0035_funnel_account_media
Revises: 0034_ppv_catalog
Create Date: 2026-06-17

A funnel's TEXT (opener + step copy/prompts/price) stays GLOBAL on
`mass_message_funnels` (shared across all of an owner's models). Its MEDIA does
NOT belong there: an OnlyFans vault id is per-account, so a media id picked on
model A is meaningless in model B's vault. This adds a NEW per-account table so
each model maps its OWN vault media to a shared funnel's opener + steps;
reply_mass_funnel / send_mass_message resolve it by (funnel_id, account_id) at
SEND time.

  funnel_account_media — NEW. Composite PK (funnel_id, account_id).
      opening_media_ids TEXT  — JSON int array (the opener's vault media).
      steps_media_json  TEXT  — JSON object keyed by step number →
                                {"media_files": [...], "previews": [...]}.

DATA-SAFE rollout: the old global media columns on `mass_message_funnels`
(vault_folder / media_indices / opening_*) are LEFT in place (nullable) as the
runtime fallback for any (funnel, account) pair not yet seeded here — remove
them in a later migration once every funnel has per-account media.

Additive + idempotent (inspector-guarded, house convention).
"""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0035_funnel_account_media"
down_revision: str | Sequence[str] | None = "0034_ppv_catalog"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "funnel_account_media" not in inspector.get_table_names():
        op.create_table(
            "funnel_account_media",
            sa.Column(
                "funnel_id",
                sa.Integer,
                sa.ForeignKey("mass_message_funnels.id", ondelete="CASCADE"),
                primary_key=True,
            ),
            sa.Column(
                "account_id",
                sa.String,
                sa.ForeignKey("accounts.id"),
                primary_key=True,
            ),
            sa.Column("opening_media_ids", sa.Text, nullable=False, server_default="[]"),
            sa.Column("steps_media_json", sa.Text, nullable=False, server_default="{}"),
            sa.Column("created_at", sa.DateTime, nullable=False),
            sa.Column("updated_at", sa.DateTime, nullable=False),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "funnel_account_media" in inspector.get_table_names():
        op.drop_table("funnel_account_media")
