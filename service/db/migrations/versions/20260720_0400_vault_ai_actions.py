"""vault_ai_actions — the Vault-AI actions layer schema

Revision ID: 0049_vault_ai_actions
Revises: 0048_vault_of_search
Create Date: 2026-07-20

Adds the durable substrate for the suggest-only "actions" layer on top of the
already-shipped describe/mirror substrate:

  * vault_items.story_score / .tip_vault_score  — AI 0-100 ranks pairing the
    existing story_suitable / tip_vault_flag booleans (feats 5 & 6).
  * account_ai_config.vault_ai_config_json      — per-account config blob
    (cadence, tier→price bands, folders, daily reminder). Shape frozen in
    plans/VAULT_AI_ACTIONS_CONTRACT.md.
  * vault_ai_review_items  — pending AI-proposed ACTIONS (folder | ppv |
    reminder) awaiting operator approval. Descriptions auto-apply and are NOT
    queued here. baseline_json enables stale-approval detection.
  * vault_daily_usage      — ACCOUNT-level image-rotation history for the daily
    reminder (distinct from fan-scoped VaultSend); record only after a send
    succeeds.

Additive + idempotent (inspector-guarded, house convention): init_db's
create_all + boot ADD-COLUMN catch-up may already have applied any of these on
a create_all-built DB whose alembic_version lags, so every step is guarded by
an existence check.
"""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0049_vault_ai_actions"
down_revision: str | Sequence[str] | None = "0048_vault_of_search"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    # ── Additive columns on existing tables ───────────────────────────────
    if "vault_items" in tables:
        cols = {c["name"] for c in inspector.get_columns("vault_items")}
        if "story_score" not in cols:
            op.add_column("vault_items", sa.Column("story_score", sa.Integer(), nullable=True))
        if "tip_vault_score" not in cols:
            op.add_column("vault_items", sa.Column("tip_vault_score", sa.Integer(), nullable=True))

    if "account_ai_config" in tables:
        cols = {c["name"] for c in inspector.get_columns("account_ai_config")}
        if "vault_ai_config_json" not in cols:
            op.add_column(
                "account_ai_config",
                sa.Column("vault_ai_config_json", sa.Text(), nullable=True),
            )

    # ── New table: review queue (ACTIONS only) ────────────────────────────
    if "vault_ai_review_items" not in tables:
        op.create_table(
            "vault_ai_review_items",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("account_id", sa.String(),
                      sa.ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False),
            sa.Column("kind", sa.String(), nullable=False),  # folder|ppv|reminder
            sa.Column("status", sa.String(), nullable=False, server_default="pending"),
            sa.Column("payload_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("baseline_json", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False,
                      server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.Column("resolved_at", sa.DateTime(), nullable=True),
        )
        op.create_index("ix_vault_ai_review_account_status",
                        "vault_ai_review_items", ["account_id", "status"])
        op.create_index("ix_vault_ai_review_account_kind",
                        "vault_ai_review_items", ["account_id", "kind", "status"])

    # ── New table: account-level daily image-rotation history ─────────────
    if "vault_daily_usage" not in tables:
        op.create_table(
            "vault_daily_usage",
            sa.Column("account_id", sa.String(),
                      sa.ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True),
            sa.Column("media_id", sa.BigInteger(), primary_key=True),
            sa.Column("sent_on", sa.Date(), primary_key=True),
            sa.Column("created_at", sa.DateTime(), nullable=False,
                      server_default=sa.text("CURRENT_TIMESTAMP")),
        )
        op.create_index("ix_vault_daily_usage_account_day",
                        "vault_daily_usage", ["account_id", "sent_on"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "vault_daily_usage" in tables:
        op.drop_table("vault_daily_usage")
    if "vault_ai_review_items" in tables:
        op.drop_table("vault_ai_review_items")

    if "account_ai_config" in tables:
        cols = {c["name"] for c in inspector.get_columns("account_ai_config")}
        if "vault_ai_config_json" in cols:
            op.drop_column("account_ai_config", "vault_ai_config_json")

    if "vault_items" in tables:
        cols = {c["name"] for c in inspector.get_columns("vault_items")}
        if "tip_vault_score" in cols:
            op.drop_column("vault_items", "tip_vault_score")
        if "story_score" in cols:
            op.drop_column("vault_items", "story_score")
