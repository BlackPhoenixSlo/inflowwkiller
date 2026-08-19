"""rename automation kind: of_ai_chat → welcome_chatter_for_info

DATA-ONLY migration — no schema change. The A05 reply loop was renamed on
2026-08-19 (module `automations/welcome_chatter_for_info.py`, registry kind
`welcome_chatter_for_info`), and the old token lives on in prod rows:

  • automation_rules.kind / scheduled_jobs.kind — DISPATCH: an unregistered
    kind goes inert, so these rows are why the rename ships with this
    migration (the LEGACY_KINDS map below covers the deploy window).
  • automation_runs.kind, messages.automation_kind, grok_calls.purpose —
    HISTORY: stats.py keys its three scans on this shared token vocabulary,
    so old rows must carry the new token or the automation's sends/spend
    split into two lines and config lookups miss.
  • account_ai_config.model_by_purpose / style_config_json — JSON KEYS:
    {"of_ai_chat": …} / {"typos_of_ai_chat": …} etc. Plain text replace is
    safe here: the token only ever appears as a (possibly prefixed) key,
    never as a value, in both columns.

`messages` and `grok_calls` are the big tables — the UPDATEs are one-time
full scans; fine, but run the deploy backup first as usual.

The code-side shim is ONE map entry: automation_registry.LEGACY_KINDS
["of_ai_chat"]. Dispatch (get_automation), the executor's priority and job
claim/dedupe (kind_family), the rules/transfer APIs' validation, and the
config-key fallbacks in _common/_ai_chatter all resolve through it. After
this migration has run on prod, delete that entry (every shim then no-ops —
the loops that read the map can be swept out at leisure) and drop:
  • the `of_ai_chat` token in scripts/health-check.sh's jaka alternation,
  • the `of_ai_chat: "Info-gather"` legacy label rows in
    app/components/messages/MessageRowGeneric.tsx and
    app/components/stats/PerAutomationTable.tsx.
The chokepoint routing itself (style_config_api GET/PUT and
load_factground_flag parsing through _parse_style_config) is permanent —
don't remove it.

Revision ID: 0062_rename_of_ai_chat
Revises: 0061_rhythm_stepout
Create Date: 2026-08-19
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0062_rename_of_ai_chat"
down_revision: str | Sequence[str] | None = "0061_rhythm_stepout"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD = "of_ai_chat"
_NEW = "welcome_chatter_for_info"

# (table, column) pairs where the token is stored VERBATIM.
_TOKEN_COLS = (
    ("automation_rules", "kind"),
    # `name` defaults to the kind on create (rules_api), so rules created
    # without an explicit name are literally NAMED the old token. Verbatim
    # match only — a hand-typed name that merely contains it is left alone.
    ("automation_rules", "name"),
    ("scheduled_jobs", "kind"),
    ("automation_runs", "kind"),
    ("messages", "automation_kind"),
)

# JSON blobs where the token appears only inside (possibly prefixed) KEYS.
_JSON_COLS = (
    ("account_ai_config", "model_by_purpose"),
    ("account_ai_config", "style_config_json"),
)

# ai_chatter_config_json holds ONE renamed key — but unlike the blobs above it
# also carries free-text values (pack lines, folder names), so the rename is a
# QUOTED-KEY replace, never a blanket token replace.
_SELL_ON_ASK_OLD = '"of_ai_chat_sell_on_ask"'
_SELL_ON_ASK_NEW = '"welcome_chatter_for_info_sell_on_ask"'


def upgrade() -> None:
    for table, col in _TOKEN_COLS:
        op.execute(
            f"UPDATE {table} SET {col} = '{_NEW}' WHERE {col} = '{_OLD}'"
        )
    # grok_calls.purpose also carries prefixed variants (e.g. of_ai_chat_reply)
    # — rewrite the prefix, keep the suffix.
    op.execute(
        f"UPDATE grok_calls SET purpose = '{_NEW}' || substr(purpose, {len(_OLD) + 1}) "
        f"WHERE purpose LIKE '{_OLD}%'"
    )
    for table, col in _JSON_COLS:
        op.execute(
            f"UPDATE {table} SET {col} = replace({col}, '{_OLD}', '{_NEW}') "
            f"WHERE {col} LIKE '%{_OLD}%'"
        )
    op.execute(
        "UPDATE account_ai_config SET ai_chatter_config_json = "
        f"replace(ai_chatter_config_json, '{_SELL_ON_ASK_OLD}', '{_SELL_ON_ASK_NEW}') "
        f"WHERE ai_chatter_config_json LIKE '%{_SELL_ON_ASK_OLD}%'"
    )


def downgrade() -> None:
    for table, col in _TOKEN_COLS:
        op.execute(
            f"UPDATE {table} SET {col} = '{_OLD}' WHERE {col} = '{_NEW}'"
        )
    op.execute(
        f"UPDATE grok_calls SET purpose = '{_OLD}' || substr(purpose, {len(_NEW) + 1}) "
        f"WHERE purpose LIKE '{_NEW}%'"
    )
    for table, col in _JSON_COLS:
        op.execute(
            f"UPDATE {table} SET {col} = replace({col}, '{_NEW}', '{_OLD}') "
            f"WHERE {col} LIKE '%{_NEW}%'"
        )
    op.execute(
        "UPDATE account_ai_config SET ai_chatter_config_json = "
        f"replace(ai_chatter_config_json, '{_SELL_ON_ASK_NEW}', '{_SELL_ON_ASK_OLD}') "
        f"WHERE ai_chatter_config_json LIKE '%{_SELL_ON_ASK_NEW}%'"
    )
