"""user_llm_keys — one LLM provider key per agency (owner), not one per box

Before this, every account on the deployment called DeepSeek/DeepInfra/Grok
with ONE process-wide key (`llm_providers._api_key` → secrets.json → env → .env).
That is fine for a single-operator box and wrong the moment a second agency
signs up: their creators' traffic would ride the deployment owner's credential
and bill.

One row per (user_id, provider). `llm_client._tenant_api_key` resolves the owner
from `user_accounts` and reads this table; an account with NO owner (an orphan,
or the NULL house-account rollup) still falls back to the process-wide key,
which is the only fallback left — an owner that HAS no row for the provider
its models use fails closed rather than quietly spending the house key.

Clearing a key empties the row rather than deleting it, so a cleared key and a
never-set one read the same to every consumer — both are "no key", both fail
closed.

Nothing seeds this table. An upgrade of a box that already has owners needs each
of them to paste a key once (DEPLOY.md → "Per-agency AI keys"); until then their
accounts refuse and the relay logs `tenant_key_missing`. That manual step is
deliberate — every automatic version of it was a way to hand the deployment
owner's credential to an account that should never have had it.

Revision ID: 0064_user_llm_keys
Revises: 0063_cotag_brain_settings
Create Date: 2026-08-20
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0064_user_llm_keys"
down_revision: str | Sequence[str] | None = "0063_cotag_brain_settings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "user_llm_keys",
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("api_key", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "provider"),
    )


def downgrade() -> None:
    op.drop_table("user_llm_keys")
