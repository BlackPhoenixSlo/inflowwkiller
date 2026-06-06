"""phase_c_automation_employee

Revision ID: 0011_phase_c_automation_employee
Revises: 0010_b5_unsend_columns
Create Date: 2026-05-22

Seeds the system "Automation" employee row. This is the actor attributed
to outbound messages where no human pressed send — mass-message runs,
scheduled jobs, and any other automation that re-routes through the
relay's HTTP layer without an `X-Employee-Id` header.

Notes:
  • `INSERT OR IGNORE` keeps the migration idempotent if re-run (a unique
    constraint isn't defined on `display_name`, so we rely on the explicit
    check inside the helper instead — and we don't want to insert twice
    if someone manually inserted the row earlier).
  • `is_active=0` so the row is excluded from the employee picker. The
    UI filters on `is_active` for the dropdown but shows disabled rows in
    the Settings → Employees table with a strikethrough; that's fine for
    a system row.
  • The runtime helper looks up by `display_name='Automation'` — never
    by a hardcoded id — so re-stamping or re-seeding into a fresh DB
    just works.
"""
from __future__ import annotations

from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0011_phase_c_automation_employee"
down_revision: str | Sequence[str] | None = "0010_b5_unsend_columns"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(sa.text(
        "INSERT OR IGNORE INTO employees "
        "(display_name, color, is_active, created_at) "
        "VALUES ('Automation', '#808080', 0, CURRENT_TIMESTAMP)"
    ))


def downgrade() -> None:
    op.execute(sa.text(
        "DELETE FROM employees "
        "WHERE display_name = 'Automation' AND is_active = 0"
    ))
