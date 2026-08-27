"""Brain co-performer tag settings: always-tag-videos + per-account username

Two nullable columns on account_ai_config, read by media_cotag on every media
send:

  • cotag_tag_videos — NULL ≡ OFF (most creators are solo). True = the
    operator's opt-in on a collab account: any send attaching a vault VIDEO
    carries the co-performer tag even when the describe verdict says solo.
    Video describes are cut from stills and miss the POV partner, which is how
    blake/blake clips shipped untagged.
  • cotag_username — the handle to tag, no '@'. NULL → OF_COTAG_USERNAME env →
    the built-in 'jakabasej'.

No backfill: NULL is the intended default state for both. init_db's ADD-COLUMN
catch-up applies the same columns on a create_all-built box, so this migration
exists to keep the alembic chain canonical.

Revision ID: 0063_cotag_brain_settings
Revises: 0062_rename_of_ai_chat
Create Date: 2026-08-19
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0063_cotag_brain_settings"
down_revision: str | Sequence[str] | None = "0062_rename_of_ai_chat"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("account_ai_config",
                  sa.Column("cotag_tag_videos", sa.Boolean(), nullable=True))
    op.add_column("account_ai_config",
                  sa.Column("cotag_username", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("account_ai_config", "cotag_username")
    op.drop_column("account_ai_config", "cotag_tag_videos")
