"""phase_a_fan_columns

Revision ID: 0005_phase_a_fan_columns
Revises: 0004_perf_events
Create Date: 2026-05-22

Adds the Phase-A fan-schema wiring + chats.preview_updated_at.

What's in here:
  • chats.preview_updated_at (T-A1)
  • fans: 17 new columns from spec 03 §2.4 + persona_* fields from spec 07 §4
    M1/M5 (T-A4). Includes server_default on the three NOT NULL JSON columns
    so the batch_alter_table rebuild can backfill existing rows without
    violating NOT NULL.
  • fans: ix_fans_relationship_stage, ix_fans_paused_until (partial).

What was DELIBERATELY excluded from this revision (autogenerate proposed
them but they're out of scope):
  • messages.{unsent_by_employee_id, unsent_reason, unsent_at, is_promise,
    promise_due_date} — T-A2 audit columns are in models.py but the
    unsend_messages automation (spec 12) ships in B.5. Cut a follow-up
    migration before B.5 lands code that touches these.
  • grok_daily_cost.account_id — 0006_phase_a_grok_daily_cost_pk handles
    this with a create-new + copy + swap to also extend the PK.
  • prompts.response_schema_json — out of any known terminal scope; defer
    until its owning task surfaces.
"""
from __future__ import annotations

from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0005_phase_a_fan_columns"
down_revision: str | Sequence[str] | None = "0004_perf_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("chats", schema=None) as batch_op:
        batch_op.add_column(sa.Column("preview_updated_at", sa.DateTime(), nullable=True))

    with op.batch_alter_table("fans", schema=None) as batch_op:
        batch_op.add_column(sa.Column("occupation", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("employer", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("relationship_status", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("partner_name", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("has_kids", sa.Boolean(), nullable=True))
        # server_default required: batch rebuild copies existing rows; without
        # it, NOT NULL fans rows get NULL injected and the migration aborts.
        batch_op.add_column(
            sa.Column("family_names", sa.Text(), nullable=False, server_default="{}")
        )
        batch_op.add_column(
            sa.Column("pets", sa.Text(), nullable=False, server_default="[]")
        )
        batch_op.add_column(sa.Column("mood_at_last_message", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("sentiment_trend", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("relationship_stage", sa.Text(), nullable=True))
        batch_op.add_column(
            sa.Column("communication_style", sa.Text(), nullable=False, server_default="{}")
        )
        batch_op.add_column(sa.Column("automation_paused_until", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("persona_age_claimed", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("persona_location_claimed", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("persona_job_claimed", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("profile_last_synced_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("grok_facts_updated_at", sa.DateTime(), nullable=True))
        batch_op.create_index(
            "ix_fans_paused_until",
            ["account_id", "automation_paused_until"],
            unique=False,
            sqlite_where=sa.text("automation_paused_until IS NOT NULL"),
            postgresql_where=sa.text("automation_paused_until IS NOT NULL"),
        )
        batch_op.create_index(
            "ix_fans_relationship_stage",
            ["account_id", "relationship_stage"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("fans", schema=None) as batch_op:
        batch_op.drop_index("ix_fans_relationship_stage")
        batch_op.drop_index(
            "ix_fans_paused_until",
            sqlite_where=sa.text("automation_paused_until IS NOT NULL"),
            postgresql_where=sa.text("automation_paused_until IS NOT NULL"),
        )
        batch_op.drop_column("grok_facts_updated_at")
        batch_op.drop_column("profile_last_synced_at")
        batch_op.drop_column("persona_job_claimed")
        batch_op.drop_column("persona_location_claimed")
        batch_op.drop_column("persona_age_claimed")
        batch_op.drop_column("automation_paused_until")
        batch_op.drop_column("communication_style")
        batch_op.drop_column("relationship_stage")
        batch_op.drop_column("sentiment_trend")
        batch_op.drop_column("mood_at_last_message")
        batch_op.drop_column("pets")
        batch_op.drop_column("family_names")
        batch_op.drop_column("has_kids")
        batch_op.drop_column("partner_name")
        batch_op.drop_column("relationship_status")
        batch_op.drop_column("employer")
        batch_op.drop_column("occupation")

    with op.batch_alter_table("chats", schema=None) as batch_op:
        batch_op.drop_column("preview_updated_at")
