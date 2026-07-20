"""vault_ai_mirror — whole-vault local mirror + describe/ordering columns

Revision ID: 0046_vault_ai_mirror
Revises: 0045_safe_state
Create Date: 2026-07-20

Revives the dead `vault_items` table as the durable per-account vault mirror:
adds the cache bookkeeping (search_text, of_folder_ids, content_hash,
last_seen_run_id, updated_at), the per-folder manual-order index, and the
Qwen3-VL describe layer (description/tags/tier/flags/caption/script + status +
audit + operator overrides/locks). Adds `vault_cache_runs` to drive the
"Collect all" progress + the two-clean-sweep soft-delete guard.

Additive + nullable + idempotent (inspector-guarded, house convention so a
create_all-built DB whose alembic_version lags doesn't trip).
"""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0046_vault_ai_mirror"
down_revision: str | Sequence[str] | None = "0045_safe_state"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# (name, type) — all nullable, so init_db's boot ADD-COLUMN catch-up can also
# apply them on a lagging DB.
_VAULT_ITEM_COLS: list[tuple[str, sa.types.TypeEngine]] = [
    ("search_text", sa.Text()),
    ("of_folder_ids", sa.Text()),
    ("content_hash", sa.String()),
    ("content_hash_kind", sa.String()),
    ("updated_at_of", sa.String()),
    ("last_seen_run_id", sa.BigInteger()),
    ("removed_at", sa.DateTime()),
    ("updated_at", sa.DateTime()),
    ("manual_order", sa.Integer()),
    ("video_description", sa.Text()),
    ("explicitness_tier", sa.String()),
    ("story_suitable", sa.Boolean()),
    ("tip_vault_flag", sa.Boolean()),
    ("suggested_caption", sa.Text()),
    ("suggested_script", sa.Text()),
    ("describe_status", sa.String()),
    ("describe_generated_at", sa.DateTime()),
    ("describe_model", sa.String()),
    ("describe_call_id", sa.BigInteger()),
    ("frames_sampled", sa.Integer()),
    ("ai_fields_json", sa.Text()),
    ("operator_overrides_json", sa.Text()),
    ("locked_fields_json", sa.Text()),
    ("review_state", sa.String()),
    ("reviewed_at", sa.DateTime()),
    ("reviewed_by", sa.String()),
]


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "vault_items" in inspector.get_table_names():
        existing = {c["name"] for c in inspector.get_columns("vault_items")}
        for name, col_type in _VAULT_ITEM_COLS:
            if name not in existing:
                op.add_column("vault_items", sa.Column(name, col_type, nullable=True))
        idx = {i["name"] for i in inspector.get_indexes("vault_items")}
        if "ix_vault_account_seen" not in idx:
            op.create_index("ix_vault_account_seen", "vault_items",
                             ["account_id", "last_seen_run_id"])
        if "ix_vault_account_describe" not in idx:
            op.create_index("ix_vault_account_describe", "vault_items",
                             ["account_id", "describe_status"])

    if "vault_cache_runs" not in inspector.get_table_names():
        op.create_table(
            "vault_cache_runs",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("account_id", sa.String(),
                      sa.ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False),
            sa.Column("status", sa.String(), nullable=False, server_default="running"),
            sa.Column("phase", sa.String(), nullable=True),
            sa.Column("total_seen", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("upserted", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("pages_done", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("started_at", sa.DateTime(), nullable=False,
                      server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.Column("finished_at", sa.DateTime(), nullable=True),
        )
        op.create_index("ix_vault_cache_runs_account", "vault_cache_runs",
                        ["account_id", "started_at"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "vault_cache_runs" in inspector.get_table_names():
        op.drop_table("vault_cache_runs")

    if "vault_items" in inspector.get_table_names():
        existing = {c["name"] for c in inspector.get_columns("vault_items")}
        idx = {i["name"] for i in inspector.get_indexes("vault_items")}
        if "ix_vault_account_describe" in idx:
            op.drop_index("ix_vault_account_describe", table_name="vault_items")
        if "ix_vault_account_seen" in idx:
            op.drop_index("ix_vault_account_seen", table_name="vault_items")
        for name, _ in reversed(_VAULT_ITEM_COLS):
            if name in existing:
                op.drop_column("vault_items", name)
