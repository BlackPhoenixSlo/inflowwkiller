"""safe_state — v2 safe seller state machine columns

Revision ID: 0045_safe_state
Revises: 0044_upsell
Create Date: 2026-07-14

Additive (columns only — no new tables):
  • ladder_state.objection_at / discount_count / bot_accused_count /
    companion_until / cooldown_until — the v2 safe-state machine
    (SPEND_REGRET → COOLDOWN/COMPANION, earned-discount beat + one-per-item,
    bot-accusation 2nd-strike, post-multibuy cooldown). offers_paused_until
    already exists from 0044. NONE of these ever touches
    fans.automation_paused_until — a decline/companion/cooldown is ladder-scoped,
    or it blanks the fan out of welcome/followup/mass too.
  • rhythm_state.recent_turns_json — rolling last ~20 realized turns (§3.4),
    recorded at the SEND site and fed back into the next RhythmCtx.
  • catalog_items.band_lo / band_hi — the derived-band snapshot per media
    (§4.2 deflation watch); NULL until first quote.

Ships inert: with the AI-Seller lane flags off none of these is written and
ai_chatter/ppv_send behave byte-identically to today.

Additive + idempotent (inspector-guarded, house convention so a create_all-built
DB whose alembic_version lags doesn't trip).
"""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0045_safe_state"
down_revision: str | Sequence[str] | None = "0044_upsell"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _cols(inspector, table: str) -> set[str]:
    if table not in inspector.get_table_names():
        return set()
    return {c["name"] for c in inspector.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    ls = _cols(inspector, "ladder_state")
    if ls:
        if "objection_at" not in ls:
            op.add_column("ladder_state", sa.Column("objection_at", sa.DateTime, nullable=True))
        if "discount_count" not in ls:
            op.add_column("ladder_state",
                          sa.Column("discount_count", sa.Integer, nullable=False, server_default="0"))
        if "bot_accused_count" not in ls:
            op.add_column("ladder_state",
                          sa.Column("bot_accused_count", sa.Integer, nullable=False, server_default="0"))
        if "companion_until" not in ls:
            op.add_column("ladder_state", sa.Column("companion_until", sa.DateTime, nullable=True))
        if "cooldown_until" not in ls:
            op.add_column("ladder_state", sa.Column("cooldown_until", sa.DateTime, nullable=True))

    rs = _cols(inspector, "rhythm_state")
    if rs and "recent_turns_json" not in rs:
        op.add_column("rhythm_state", sa.Column("recent_turns_json", sa.Text, nullable=True))

    ci = _cols(inspector, "catalog_items")
    if ci:
        if "band_lo" not in ci:
            op.add_column("catalog_items", sa.Column("band_lo", sa.Integer, nullable=True))
        if "band_hi" not in ci:
            op.add_column("catalog_items", sa.Column("band_hi", sa.Integer, nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    ci = _cols(inspector, "catalog_items")
    for col in ("band_hi", "band_lo"):
        if col in ci:
            op.drop_column("catalog_items", col)

    rs = _cols(inspector, "rhythm_state")
    if "recent_turns_json" in rs:
        op.drop_column("rhythm_state", "recent_turns_json")

    ls = _cols(inspector, "ladder_state")
    for col in ("cooldown_until", "companion_until", "bot_accused_count",
                "discount_count", "objection_at"):
        if col in ls:
            op.drop_column("ladder_state", col)
