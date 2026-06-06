"""phase_a_safety_tables

Revision ID: 0007_phase_a_safety_tables
Revises: 0006_phase_a_grok_daily_cost_pk
Create Date: 2026-05-22

Adds the two per-fan safety tables (T-A3 schema).

  • fan_triggers       — spec 03 §4.2: do-not-say list per fan.
  • fan_safety_flags   — spec 03 §4.3: abuse/minor/scam/suicide detection.

Skipped: emotional_probes (contested per spec 07 §3).

Composite FK (account_id, fan_id) -> fans CASCADE on both tables.
flagged_by_employee_id -> employees SET NULL on both, so deleting an
employee doesn't lose the audit trail.

Excluded (autogenerate proposed; out of scope for this revision):
  • messages.{unsent_*, is_promise, promise_due_date} — deferred to a
    follow-up migration before B.5 (per the 0005 docstring).
  • prompts.response_schema_json — lands in 0009.
"""
from __future__ import annotations

from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0007_phase_a_safety_tables"
down_revision: str | Sequence[str] | None = "0006_phase_a_grok_daily_cost_pk"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "fan_safety_flags",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.String(), nullable=False),
        sa.Column("fan_id", sa.BigInteger(), nullable=False),
        sa.Column("flag_type", sa.String(), nullable=False),
        sa.Column("severity", sa.String(), nullable=False),
        sa.Column("details", sa.Text(), nullable=True),
        sa.Column(
            "flagged_at",
            sa.DateTime(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column("flagged_by", sa.String(), nullable=False),
        sa.Column("flagged_by_employee_id", sa.Integer(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.Column("resolution", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(
            ["account_id", "fan_id"],
            ["fans.account_id", "fans.fan_id"],
            name=op.f("fk_fan_safety_flags_account_id_fans"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["flagged_by_employee_id"],
            ["employees.id"],
            name=op.f("fk_fan_safety_flags_flagged_by_employee_id_employees"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_fan_safety_flags")),
    )
    with op.batch_alter_table("fan_safety_flags", schema=None) as batch_op:
        batch_op.create_index(
            "ix_fan_safety_flags_fan", ["account_id", "fan_id"], unique=False
        )
        batch_op.create_index(
            "ix_fan_safety_flags_open_critical",
            ["account_id", "fan_id"],
            unique=False,
            sqlite_where=sa.text("resolved_at IS NULL AND severity = 'critical'"),
            postgresql_where=sa.text("resolved_at IS NULL AND severity = 'critical'"),
        )

    op.create_table(
        "fan_triggers",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.String(), nullable=False),
        sa.Column("fan_id", sa.BigInteger(), nullable=False),
        sa.Column("trigger_phrase", sa.Text(), nullable=False),
        sa.Column("severity", sa.String(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "flagged_at",
            sa.DateTime(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column("flagged_by_employee_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(
            ["account_id", "fan_id"],
            ["fans.account_id", "fans.fan_id"],
            name=op.f("fk_fan_triggers_account_id_fans"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["flagged_by_employee_id"],
            ["employees.id"],
            name=op.f("fk_fan_triggers_flagged_by_employee_id_employees"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_fan_triggers")),
    )
    with op.batch_alter_table("fan_triggers", schema=None) as batch_op:
        batch_op.create_index("ix_fan_triggers_fan", ["account_id", "fan_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("fan_triggers", schema=None) as batch_op:
        batch_op.drop_index("ix_fan_triggers_fan")
    op.drop_table("fan_triggers")

    with op.batch_alter_table("fan_safety_flags", schema=None) as batch_op:
        batch_op.drop_index(
            "ix_fan_safety_flags_open_critical",
            sqlite_where=sa.text("resolved_at IS NULL AND severity = 'critical'"),
            postgresql_where=sa.text("resolved_at IS NULL AND severity = 'critical'"),
        )
        batch_op.drop_index("ix_fan_safety_flags_fan")
    op.drop_table("fan_safety_flags")
