"""ppv_catalog — content catalog + ai_chatter offer state

Revision ID: 0034_ppv_catalog
Revises: 0033_tip_reward
Create Date: 2026-06-12

Five additive changes for the ai_chatter automation (freestyle PPV/tip selling
from a described content catalog):

  account_ai_config — + ai_chatter_config_json TEXT (per-account config:
                      enabled, mode backup|always, sla_minutes, spend gate,
                      offer caps). NULL = built-in defaults (DISABLED).

  catalog_scripts   — NEW. An ordered "sexting sequence" (theme shown to the
                      LLM, never fans). Singles are items with script_id NULL.

  catalog_items     — NEW. One sellable unit (video/image/image_set) with
                      description_for_ai (the pitch contract), media ids,
                      previews, price_cents / tip_unlock_cents.

  catalog_progress  — NEW. Per-(account, fan, script) position pin — which item
                      may be offered next; once-per-fan-per-script.

  content_offers    — NEW. One row per concrete offer; the OPEN offer anchors
                      tip accumulation + the is_paid ledger watch, and is the
                      rate-limit + conversion log.

Additive + nullable + idempotent (inspector-guarded, house convention).
"""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0034_ppv_catalog"
down_revision: str | Sequence[str] | None = "0033_tip_reward"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    cfg_cols = {c["name"] for c in inspector.get_columns("account_ai_config")}
    if "ai_chatter_config_json" not in cfg_cols:
        op.add_column(
            "account_ai_config",
            sa.Column("ai_chatter_config_json", sa.Text, nullable=True),
        )

    if "catalog_scripts" not in tables:
        op.create_table(
            "catalog_scripts",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("account_id", sa.String, nullable=False),
            sa.Column("name", sa.String, nullable=False),
            sa.Column("theme", sa.Text, nullable=True),
            sa.Column("status", sa.String, nullable=False, server_default="draft"),
            sa.Column("created_at", sa.DateTime, nullable=False),
            sa.Column("updated_at", sa.DateTime, nullable=False),
        )
        op.create_index(
            "ix_catalog_scripts_account",
            "catalog_scripts",
            ["account_id", "name"],
            unique=True,
        )

    if "catalog_items" not in tables:
        op.create_table(
            "catalog_items",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("account_id", sa.String, nullable=False),
            sa.Column(
                "script_id",
                sa.Integer,
                sa.ForeignKey("catalog_scripts.id", ondelete="CASCADE"),
                nullable=True,
            ),
            sa.Column("position", sa.Integer, nullable=True),
            sa.Column("kind", sa.String, nullable=False, server_default="video"),
            sa.Column("label", sa.String, nullable=True),
            sa.Column("description_for_ai", sa.Text, nullable=True),
            sa.Column("media_ids", sa.Text, nullable=False, server_default="[]"),
            sa.Column("preview_media_ids", sa.Text, nullable=False, server_default="[]"),
            sa.Column("duration_sec", sa.Integer, nullable=True),
            sa.Column("price_cents", sa.Integer, nullable=False, server_default="0"),
            sa.Column("tip_unlock_cents", sa.Integer, nullable=False, server_default="0"),
            sa.Column("is_free_teaser", sa.Boolean, nullable=False, server_default="0"),
            sa.Column("tags", sa.Text, nullable=False, server_default="[]"),
            sa.Column("enabled", sa.Boolean, nullable=False, server_default="1"),
            sa.Column("created_at", sa.DateTime, nullable=False),
            sa.Column("updated_at", sa.DateTime, nullable=False),
        )
        op.create_index(
            "ix_catalog_items_script",
            "catalog_items",
            ["account_id", "script_id", "position"],
        )

    if "catalog_progress" not in tables:
        op.create_table(
            "catalog_progress",
            sa.Column("account_id", sa.String, primary_key=True),
            sa.Column("fan_id", sa.BigInteger, primary_key=True),
            sa.Column(
                "script_id",
                sa.Integer,
                sa.ForeignKey("catalog_scripts.id", ondelete="CASCADE"),
                primary_key=True,
            ),
            sa.Column("position", sa.Integer, nullable=False, server_default="0"),
            sa.Column("status", sa.String, nullable=False, server_default="active"),
            sa.Column("started_at", sa.DateTime, nullable=False),
            sa.Column("updated_at", sa.DateTime, nullable=False),
        )

    if "content_offers" not in tables:
        op.create_table(
            "content_offers",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("account_id", sa.String, nullable=False),
            sa.Column("fan_id", sa.BigInteger, nullable=False),
            sa.Column(
                "item_id",
                sa.Integer,
                sa.ForeignKey("catalog_items.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("script_id", sa.Integer, nullable=True),
            sa.Column("mode", sa.String, nullable=False, server_default="tip"),
            sa.Column("price_cents", sa.Integer, nullable=False, server_default="0"),
            sa.Column("tip_unlock_cents", sa.Integer, nullable=False, server_default="0"),
            sa.Column("offer_message_id", sa.BigInteger, nullable=True),
            sa.Column("status", sa.String, nullable=False, server_default="open"),
            sa.Column("resolved_by", sa.String, nullable=True),
            sa.Column("tips_accum_cents", sa.Integer, nullable=False, server_default="0"),
            sa.Column("delivery_message_id", sa.BigInteger, nullable=True),
            sa.Column("offered_at", sa.DateTime, nullable=False),
            sa.Column("resolved_at", sa.DateTime, nullable=True),
            sa.Column("updated_at", sa.DateTime, nullable=False),
        )
        op.create_index(
            "ix_content_offers_open",
            "content_offers",
            ["account_id", "fan_id", "status"],
        )
        op.create_index(
            "ix_content_offers_item",
            "content_offers",
            ["account_id", "item_id", "offered_at"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    for table, indexes in (
        ("content_offers", ("ix_content_offers_open", "ix_content_offers_item")),
        ("catalog_progress", ()),
        ("catalog_items", ("ix_catalog_items_script",)),
        ("catalog_scripts", ("ix_catalog_scripts_account",)),
    ):
        if table in tables:
            existing = {ix["name"] for ix in inspector.get_indexes(table)}
            for ix in indexes:
                if ix in existing:
                    op.drop_index(ix, table_name=table)
            op.drop_table(table)

    cfg_cols = {c["name"] for c in inspector.get_columns("account_ai_config")}
    if "ai_chatter_config_json" in cfg_cols:
        op.drop_column("account_ai_config", "ai_chatter_config_json")
