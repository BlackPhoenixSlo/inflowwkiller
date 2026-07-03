"""
ORM models — the full schema from ARCHITECTURE_PLAN_V2.md §4.

Convention notes (carried throughout):
  • Snake_case column names. Mirrors the SQL in the plan.
  • All UTC timestamps stored as DATETIME with default CURRENT_TIMESTAMP.
    SQLAlchemy maps to Python `datetime` (naive UTC — we never store local).
  • Money in `*_cents` integers (BIGINT for lifetime spend). Never floats.
  • Composite PKs on every per-account-and-X table (chats, messages, fans,
    vault_items, …) so unified queries are `ORDER BY` instead of UNION.
  • JSON-shaped fields (tags, custom_fields, raw_json, recent_events, etc.)
    stored as TEXT and serialized at the application layer. Drizzle/JSONB
    is a Postgres luxury we don't need yet; the relay reads JSON once on
    load and never queries inside.
  • Foreign keys use `ON DELETE CASCADE` where the child only makes sense
    in the context of its parent (sessions → account, list_members → list).
    `ON DELETE SET NULL` where the link is informational (proxies →
    account_id, actions → employee_id).

Why a mix of SQLModel and SQLAlchemy declarative: SQLModel gives Pydantic
serialization for free on simple-PK rows we expose via FastAPI. The
composite-PK + partial-index tables (most of ours) fall back to plain
SQLAlchemy because SQLModel can't express them cleanly. We define both
flavors against the same `metadata` so Alembic sees the whole schema.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


# Shared metadata + base. Naming convention so Alembic-generated
# constraint names are stable (otherwise it falls back to anonymous names
# that change across DB engines and break downgrades).
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

metadata = MetaData(naming_convention=NAMING_CONVENTION)


class Base(DeclarativeBase):
    metadata = metadata


# ── Reusable defaults ─────────────────────────────────────────────────

def _now() -> datetime:
    """UTC-naive 'now' — kept in one helper so we can swap to UTC-aware
    later without hunting through column defaults."""
    return datetime.utcnow()


# A timestamp that defaults to NOW() at insert time. The `server_default`
# lets the DB fill it for raw-SQL inserts (importer, ad-hoc), while
# `default=_now` covers ORM inserts where the server clock is sometimes
# slightly off the application clock.
def _ts_now() -> Column:
    return mapped_column(
        DateTime, nullable=False, default=_now, server_default=text("CURRENT_TIMESTAMP")
    )


# ── §4.1 Identity / connections ──────────────────────────────────────

class Account(Base):
    """One OF model account. Same row as service/sessions/accounts/<id>/."""
    __tablename__ = "accounts"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    nickname: Mapped[str | None] = mapped_column(String)
    color: Mapped[str | None] = mapped_column(String)
    proxy_label: Mapped[str | None] = mapped_column(String)  # soft FK → proxies.label
    x_of_rev: Mapped[str | None] = mapped_column(String)
    static_param: Mapped[str | None] = mapped_column(String)
    user_agent: Mapped[str | None] = mapped_column(String)
    is_active_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = _ts_now()
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime)


class Proxy(Base):
    """Proxy registry — replaces service/proxies.json."""
    __tablename__ = "proxies"

    label: Mapped[str] = mapped_column(String, primary_key=True)
    scheme: Mapped[str] = mapped_column(String, nullable=False)
    host: Mapped[str] = mapped_column(String, nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False)
    username: Mapped[str | None] = mapped_column(String)
    # Encrypted at rest later (phase D). Plaintext for now to match the
    # current proxies.json — flagged in the plan as a known limitation.
    password_encrypted: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    verified_ip: Mapped[str | None] = mapped_column(String)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime)
    assigned_account_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("accounts.id", ondelete="SET NULL")
    )


class Session(Base):
    """Captured session blobs — replaces session_*.json files."""
    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[str] = mapped_column(
        String, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    captured_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    cookies_json: Mapped[str] = mapped_column(Text, nullable=False)
    x_of_rev: Mapped[str] = mapped_column(String, nullable=False)
    signing_rules_json: Mapped[str] = mapped_column(Text, nullable=False)
    is_latest: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    __table_args__ = (
        # Hot path: "give me the active session for account X."
        Index("ix_sessions_account_latest", "account_id", "is_latest"),
    )


class Employee(Base):
    """Your team. No passwords — the picker reads display_name + color.

    `user_id` scopes the roster to one signed-in friend (see
    plan/simple_username_auth_2026_05_24/PLAN.md). NULL = system sentinel
    (e.g. the 'Automation' attribution row); never appears in any user's
    picker. Application-level filtering: every read in employees.py is
    constrained to `user_id == current_user.id`.
    """
    __tablename__ = "employees"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    display_name: Mapped[str] = mapped_column(String, nullable=False)
    color: Mapped[str | None] = mapped_column(String)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = _ts_now()
    user_id: Mapped[str | None] = mapped_column(String, index=True)
    # Set when this Employee is the per-owner mirror of a Chatter (see
    # service/chatters.py + migration 0024_chatter_login). Audit writes
    # under a chatter session resolve (chatter_id, account.owner_user_id)
    # → this row and stamp actions.employee_id. NULL = label-only Employee
    # (legacy, founder-managed). FK is application-level; SQLite ALTER
    # cannot add an inline FK constraint.
    chatter_id: Mapped[str | None] = mapped_column(String, index=True)

    __table_args__ = (
        # Partial UNIQUE index — at most one mirror Employee per
        # (chatter, owner) pair. The chatter auto-create path uses
        # ON CONFLICT DO NOTHING against this index, so two simultaneous
        # first-mutations from the same chatter against two new owners
        # don't race-insert duplicate mirror rows. Legacy / label-only
        # Employees (chatter_id IS NULL) are excluded so they can
        # coexist freely with the mirrors. Mirrored in migration
        # 0024_chatter_login for fresh-from-alembic deploys.
        Index(
            "uq_employees_chatter_owner",
            "chatter_id", "user_id",
            unique=True,
            sqlite_where=text("chatter_id IS NOT NULL"),
            postgresql_where=text("chatter_id IS NOT NULL"),
        ),
    )


class EmployeeAccountAccess(Base):
    """Which models can each employee touch. NULL account_id = all models."""
    __tablename__ = "employee_account_access"

    employee_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("employees.id", ondelete="CASCADE"), primary_key=True
    )
    # nullable + part of PK — represents "any account." SQLite tolerates
    # this via the (employee_id, COALESCE(account_id, '*')) composite-key
    # convention; we enforce uniqueness in code on insert.
    account_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True
    )


class Action(Base):
    """Audit log — one row per mutating request, written by middleware."""
    __tablename__ = "actions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employee_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("employees.id", ondelete="SET NULL")
    )
    account_id: Mapped[str | None] = mapped_column(String)
    action: Mapped[str] = mapped_column(String, nullable=False)
    target_type: Mapped[str | None] = mapped_column(String)
    target_id: Mapped[str | None] = mapped_column(String)
    payload_json: Mapped[str | None] = mapped_column(Text)
    at: Mapped[datetime] = _ts_now()

    __table_args__ = (
        Index("ix_actions_employee_at", "employee_id", "at"),
        Index("ix_actions_account_at", "account_id", "at"),
    )


# ── §4.2 Fans (the wide AI-readable table) ───────────────────────────

class Fan(Base):
    """One row per (account, fan). Wide table by design — every field the
    automation pack reads is here so gen_info / followup / of_ai_chat all
    have a single source. Heavy columns (raw_json) get their own rows in
    sibling tables when they grow."""
    __tablename__ = "fans"

    account_id: Mapped[str] = mapped_column(
        String, ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True
    )
    fan_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    # ── Identity (from OF) ────────────────────────────────────
    of_username: Mapped[str | None] = mapped_column(String)
    of_display_name: Mapped[str | None] = mapped_column(String)
    avatar_url: Mapped[str | None] = mapped_column(Text)

    # ── Our labels ───────────────────────────────────────────
    custom_nickname: Mapped[str | None] = mapped_column(String)
    generated_nickname: Mapped[str | None] = mapped_column(String)
    fan_chosen_nickname: Mapped[str | None] = mapped_column(String)

    # ── AI-extracted facts (Grok fills these) ────────────────
    real_name: Mapped[str | None] = mapped_column(String)
    is_name_real: Mapped[bool] = mapped_column(Boolean, default=True)
    his_age: Mapped[str | None] = mapped_column(String)
    home_country: Mapped[str | None] = mapped_column(String)
    home_city: Mapped[str | None] = mapped_column(String)
    hobbies: Mapped[str | None] = mapped_column(Text)
    fetishes: Mapped[str | None] = mapped_column(Text)
    # JSON array of {date, event}
    recent_events: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    self_description: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    likes_boobs: Mapped[bool] = mapped_column(Boolean, default=False)
    likes_ass: Mapped[bool] = mapped_column(Boolean, default=False)
    timezone: Mapped[str | None] = mapped_column(String)

    # ── §2.4 Extended Grok-extracted facts ───────────────────
    occupation: Mapped[str | None] = mapped_column(Text)
    employer: Mapped[str | None] = mapped_column(Text)
    relationship_status: Mapped[str | None] = mapped_column(Text)
    partner_name: Mapped[str | None] = mapped_column(Text)
    has_kids: Mapped[bool | None] = mapped_column(Boolean)
    # JSON: {partner, kids:[{name,age}], parents:[...], siblings:[...]}
    family_names: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    # JSON: [{kind, breed, name}]
    pets: Mapped[str] = mapped_column(Text, nullable=False, default="[]")

    # ── Mood / stage (Grok + computed) ───────────────────────
    mood_at_last_message: Mapped[str | None] = mapped_column(Text)
    sentiment_trend: Mapped[str | None] = mapped_column(Text)
    relationship_stage: Mapped[str | None] = mapped_column(Text)
    # JSON: {chatty_level, kink_forward, romantic_level, blunt_level}
    communication_style: Mapped[str] = mapped_column(Text, nullable=False, default="{}")

    # ── Automation pause (human override) ────────────────────
    automation_paused_until: Mapped[datetime | None] = mapped_column(DateTime)

    # ── of_ai_chat info-gathering: topics we've already asked (V1 QuestionsAsked)
    # JSON list of keys (age/location/hobbies/…); a topic here is never re-asked,
    # so the bot gathers progressively without nagging. NULL == "[]". ──────────
    questions_asked: Mapped[str | None] = mapped_column(Text)

    # ── Persona continuity (what WE told THIS fan) ───────────
    persona_age_claimed: Mapped[str | None] = mapped_column(Text)
    persona_location_claimed: Mapped[str | None] = mapped_column(Text)
    persona_job_claimed: Mapped[str | None] = mapped_column(Text)

    # ── Sync provenance ──────────────────────────────────────
    profile_last_synced_at: Mapped[datetime | None] = mapped_column(DateTime)
    grok_facts_updated_at: Mapped[datetime | None] = mapped_column(DateTime)

    # ── Notes (may be written back to OF via apply_profiles) ─
    notes: Mapped[str | None] = mapped_column(Text)
    applied_notes: Mapped[str | None] = mapped_column(Text)
    applied_notes_at: Mapped[datetime | None] = mapped_column(DateTime)

    # ── Tags / custom fields ─────────────────────────────────
    tags: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    custom_fields: Mapped[str] = mapped_column(Text, nullable=False, default="{}")

    # ── Behavior counters (denormalized) ─────────────────────
    lifetime_spend_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    total_likes: Mapped[int | None] = mapped_column(Integer)
    is_bot: Mapped[bool] = mapped_column(Boolean, default=False)
    turn_counter: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # ── Timestamps (rebuildable from messages but cached for speed) ─
    last_message_received_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_message_sent_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_online_at: Mapped[datetime | None] = mapped_column(DateTime)
    subscribed_at: Mapped[datetime | None] = mapped_column(DateTime)
    subscription_expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    subscription_status: Mapped[str | None] = mapped_column(String)
    is_followed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # OF chat-level "Mute notifications" (isMutedNotifications on the /chats item —
    # the creator silenced this chat). Persisted from the scrape; when a muted fan
    # is also a creator we follow (subscribedBy) the automations skip-list them so
    # nobody wastes an LLM call on mutual-promo spam. See _common.should_skip_muted_creator.
    is_muted: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("0"), default=False)
    joined_date: Mapped[str | None] = mapped_column(String)

    # ── deep_convo state machine (4-step engagement drill) ───
    deep_convo_state: Mapped[str] = mapped_column(String, nullable=False, default="missing")
    deep_convo_skip_level: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    deep_convo_skip_remaining: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    deep_convo_updated_at: Mapped[datetime | None] = mapped_column(DateTime)
    deep_convo_q_text: Mapped[str | None] = mapped_column(Text)
    deep_convo_tease_text: Mapped[str | None] = mapped_column(Text)

    source: Mapped[str] = mapped_column(String, nullable=False, default="onlyfans")
    raw_json: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = _ts_now()
    updated_at: Mapped[datetime] = _ts_now()

    __table_args__ = (
        Index("ix_fans_last_msg", "account_id", "last_message_received_at"),
        Index("ix_fans_spend", "account_id", "lifetime_spend_cents"),
        Index("ix_fans_relationship_stage", "account_id", "relationship_stage"),
        Index(
            "ix_fans_paused_until",
            "account_id",
            "automation_paused_until",
            sqlite_where=text("automation_paused_until IS NOT NULL"),
            postgresql_where=text("automation_paused_until IS NOT NULL"),
        ),
    )


# ── §4.3 Messages (every word forever) ───────────────────────────────

class Message(Base):
    """One row per OF message. message_id is OF's native id — primary
    dedup key. temp_id is client-generated for optimistic reconcile."""
    __tablename__ = "messages"

    account_id: Mapped[str] = mapped_column(String, primary_key=True)
    fan_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    message_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    direction: Mapped[str] = mapped_column(String, nullable=False)  # 'in' | 'out' | 'system'
    sender_name: Mapped[str] = mapped_column(String, nullable=False, default="")
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    media_ids: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    media_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    price_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # NULL = no PPV at all; False = PPV not yet purchased; True = unlocked.
    is_paid: Mapped[bool | None] = mapped_column(Boolean)
    is_tip: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    purchased_at: Mapped[datetime | None] = mapped_column(DateTime)
    is_unsent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    unsent_by_employee_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("employees.id", ondelete="SET NULL")
    )
    unsent_reason: Mapped[str | None] = mapped_column(Text)
    unsent_at: Mapped[datetime | None] = mapped_column(DateTime)
    is_promise: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    promise_due_date: Mapped[datetime | None] = mapped_column(DateTime)

    sent_by_employee_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("employees.id", ondelete="SET NULL")
    )
    temp_id: Mapped[str | None] = mapped_column(String)

    mass_run_id: Mapped[int | None] = mapped_column(Integer)  # FK declared via table_args
    funnel_step: Mapped[int | None] = mapped_column(Integer)
    # Which automation sent this outbound row (of_ai_chat, send_welcome,
    # deep_convo, followup, autoreply, send_mass_message, reply_mass_funnel,
    # nudge_online, mass_nudge, online_blast). NULL = human send or a legacy
    # pre-0032 row. Distinct from sent_by_employee_id, which lumps every
    # automation under the single system "Automation" sentinel employee.
    automation_kind: Mapped[str | None] = mapped_column(String)

    raw_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)  # OF's createdAt
    ingested_at: Mapped[datetime] = _ts_now()

    __table_args__ = (
        ForeignKeyConstraint(["mass_run_id"], ["mass_runs.id"], ondelete="SET NULL"),
        Index("ix_messages_account_fan_time", "account_id", "fan_id", "created_at"),
        # Backs the All-Messages tab's wider scan (direction-scoped per
        # account, no fan filter) — see 18_stats_per_fan_csv.md §SQL impact
        # and migration 0017_phase_g_messages_dir_index.
        Index("ix_messages_dir_created", "account_id", "direction", "created_at"),
        # Partial index — only the rows with active temp_ids participate
        # in the reconcile lookup. Tiny on most DBs because rare.
        Index(
            "ix_messages_temp",
            "account_id",
            "temp_id",
            sqlite_where=text("temp_id IS NOT NULL"),
            postgresql_where=text("temp_id IS NOT NULL"),
        ),
        Index(
            "ix_messages_mass",
            "mass_run_id",
            sqlite_where=text("mass_run_id IS NOT NULL"),
            postgresql_where=text("mass_run_id IS NOT NULL"),
        ),
        # Backs the per-automation stats panel (count messages grouped by
        # automation_kind, scoped per account + time). Partial — only
        # automation sends carry a kind.
        Index(
            "ix_messages_automation_kind",
            "account_id",
            "automation_kind",
            "created_at",
            sqlite_where=text("automation_kind IS NOT NULL"),
            postgresql_where=text("automation_kind IS NOT NULL"),
        ),
    )


class MessageMedia(Base):
    """One row per media item attached to a message — the render-stability
    cache (18_chat_render_stability.md §1.1). width/height start NULL and are
    populated lazily: by T-API from the OF REST /chats/{id}/messages media
    `files`/`info`, and/or by a client onload→PATCH for items OF didn't size.
    The skeleton box must never depend on dims existing; raw_json carries only
    the media id (not dimensions), so there is NO raw_json backfill path."""
    __tablename__ = "message_media"

    account_id: Mapped[str] = mapped_column(String, primary_key=True)
    fan_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    message_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    media_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    type: Mapped[str | None] = mapped_column(String)  # photo | video | gif | audio
    width: Mapped[int | None] = mapped_column(Integer)   # NULLABLE — see docstring
    height: Mapped[int | None] = mapped_column(Integer)  # NULLABLE — see docstring
    thumb_url: Mapped[str | None] = mapped_column(Text)
    source_url: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[int | None] = mapped_column(Integer)  # video/audio length
    ingested_at: Mapped[datetime] = _ts_now()
    updated_at: Mapped[datetime] = _ts_now()

    __table_args__ = (
        # Child of messages — the dims/media cache only makes sense per
        # message, so cascade-delete with the parent (incl. when a mass
        # placeholder row is reconciled away).
        ForeignKeyConstraint(
            ["account_id", "fan_id", "message_id"],
            ["messages.account_id", "messages.fan_id", "messages.message_id"],
            ondelete="CASCADE",
        ),
        # The client dims-PATCH and any per-media lookup go by media_id.
        Index("ix_message_media_media", "account_id", "media_id"),
    )


class MessageFlag(Base):
    """Per-employee local flags. Never sent to OF."""
    __tablename__ = "message_flags"

    account_id: Mapped[str] = mapped_column(String, primary_key=True)
    fan_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    message_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    flagged_by_employee_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("employees.id", ondelete="CASCADE"), primary_key=True
    )
    flagged_unread: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    starred: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    updated_at: Mapped[datetime] = _ts_now()


class ScrapeHistory(Base):
    """Last-seen-message fast-skip — ported verbatim from the automation pack."""
    __tablename__ = "scrape_history"

    account_id: Mapped[str] = mapped_column(String, primary_key=True)
    fan_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    last_message_id: Mapped[int | None] = mapped_column(BigInteger)
    last_message_text: Mapped[str | None] = mapped_column(Text)
    last_scrape_at: Mapped[datetime] = _ts_now()


# ── §4.4 Transactions (split for fast spend queries) ─────────────────

class Transaction(Base):
    """Every PPV unlock, tip, subscription, rebill, custom. Separate
    table so 'lifetime spend by fan' and 'revenue by day' are single
    index scans instead of message aggregates."""
    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[str] = mapped_column(
        String, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    fan_id: Mapped[int | None] = mapped_column(BigInteger)  # NULL for subscription rebills
    kind: Mapped[str] = mapped_column(String, nullable=False)
    message_id: Mapped[int | None] = mapped_column(BigInteger)
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String, nullable=False, default="USD")
    occurred_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    raw_json: Mapped[str | None] = mapped_column(Text)
    ingested_at: Mapped[datetime] = _ts_now()

    # Phase F (0013_phase_f_transaction_ingest): OF payouts/transactions
    # ledger fields. `source` distinguishes WS-pump rows (provider_id
    # NULL) from ingest-written rows (provider_id set). `status` is a
    # soft string — stats filter `WHERE status='cleared'` is the contract.
    provider_transaction_id: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, server_default=text("'cleared'")
    )
    cleared_at: Mapped[datetime | None] = mapped_column(DateTime)
    payout_pending_days: Mapped[int | None] = mapped_column(Integer)
    net_cents: Mapped[int | None] = mapped_column(Integer)
    fee_cents: Mapped[int | None] = mapped_column(Integer)
    vat_cents: Mapped[int | None] = mapped_column(Integer)
    description: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(
        String(8), nullable=False, server_default=text("'ws'")
    )

    # 0015: manual override for the attribution view's COALESCE. Set via
    # POST /admin/ingest/transactions/{id}/attribute when ops want a tip
    # (or any tx) to count toward a specific employee, bypassing the
    # 7-day-lookback heuristic.
    attributed_employee_id: Mapped[int | None] = mapped_column(Integer)

    __table_args__ = (
        Index("ix_tx_account_time", "account_id", "occurred_at"),
        Index(
            "ix_tx_fan_time",
            "account_id",
            "fan_id",
            "occurred_at",
            sqlite_where=text("fan_id IS NOT NULL"),
            postgresql_where=text("fan_id IS NOT NULL"),
        ),
        Index("ix_tx_kind", "account_id", "kind", "occurred_at"),
        # Phase F: partial unique on the OF hash id so re-polls of the
        # same ledger row hit ON CONFLICT DO UPDATE (handles loading→cleared).
        # WS-pump rows (NULL provider_id) skip uniqueness via the partial WHERE.
        Index(
            "uq_tx_provider_id",
            "account_id",
            "provider_transaction_id",
            unique=True,
            sqlite_where=text("provider_transaction_id IS NOT NULL"),
            postgresql_where=text("provider_transaction_id IS NOT NULL"),
        ),
        # Leading column is `status` (not account_id) because the pending_q
        # in /admin/stats/per-model filters status != 'cleared' WITHOUT an
        # account_id equality (cross-account aggregate).
        Index("ix_tx_status_occurred", "status", "occurred_at"),
        # Phase F (0014): cross-writer dedup target. WS pump and the
        # Phase F PPV writer race for the same (account, fan, message_id)
        # — partial-unique lets ON CONFLICT promote ppv_pending → ppv_message.
        # `kind` deliberately excluded from the key (see migration docstring).
        Index(
            "uq_tx_msg",
            "account_id",
            "fan_id",
            "message_id",
            unique=True,
            sqlite_where=text("message_id IS NOT NULL"),
            postgresql_where=text("message_id IS NOT NULL"),
        ),
    )


# ── §4.5 Vault / Posts / Chats / Lists ──────────────────────────────

class Chat(Base):
    """One row per conversation = (account, fan). Hot-read for the inbox list."""
    __tablename__ = "chats"

    account_id: Mapped[str] = mapped_column(
        String, ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True
    )
    fan_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    last_message_id: Mapped[int | None] = mapped_column(BigInteger)
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_message_preview: Mapped[str | None] = mapped_column(Text)
    unread_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_pinned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_priority: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    hidden_locally: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    list_ids: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    preview_updated_at: Mapped[datetime | None] = mapped_column(DateTime)

    __table_args__ = (
        # Powers the unified-inbox ORDER BY.
        Index("ix_chats_last_msg", "last_message_at"),
    )


class VaultItem(Base):
    """OF vault mirror + our metadata."""
    __tablename__ = "vault_items"

    account_id: Mapped[str] = mapped_column(
        String, ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True
    )
    media_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    kind: Mapped[str] = mapped_column(String, nullable=False)

    duration_seconds: Mapped[int | None] = mapped_column(Integer)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    thumb_url: Mapped[str | None] = mapped_column(Text)
    full_url: Mapped[str | None] = mapped_column(Text)
    folder_id: Mapped[int | None] = mapped_column(BigInteger)

    description: Mapped[str | None] = mapped_column(Text)
    suggested_price_cents: Mapped[int | None] = mapped_column(Integer)
    default_price_cents: Mapped[int | None] = mapped_column(Integer)
    tags: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    notes: Mapped[str | None] = mapped_column(Text)
    send_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_sent_at: Mapped[datetime | None] = mapped_column(DateTime)

    raw_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    __table_args__ = (
        Index("ix_vault_account_created", "account_id", "created_at"),
        Index("ix_vault_account_send", "account_id", "send_count"),
    )


class VaultResponseCache(Base):
    """Shared server-side cache for the OF vault listing endpoints.

    Every employee on the same OF account sees the same vault, so the
    first picker-open warms the cache for everyone else. Read path checks
    `fetched_at` against a configurable TTL; any vault-mutating call
    (upload, delete, folder edit) wipes every row for the account so the
    next read goes upstream.

    `query_key` packs the full request shape (`type=…|list=…|offset=…|limit=…`
    or `lists:view=…|limit=…|offset=…`) so each (account, params) tuple
    has its own slot — pagination pages cache independently.
    """
    __tablename__ = "vault_response_cache"

    account_id: Mapped[str] = mapped_column(
        String, ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True
    )
    query_key: Mapped[str] = mapped_column(String, primary_key=True)
    response_json: Mapped[str] = mapped_column(Text, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_now, server_default=text("CURRENT_TIMESTAMP"),
    )


class VaultPreset(Base):
    """Named "send image at folder X index N." Resolves per-model at send
    time so a single preset works across all your models. See plan §13.12."""
    __tablename__ = "vault_presets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    folder_name: Mapped[str] = mapped_column(String, nullable=False)
    index_in_folder: Mapped[int | None] = mapped_column(Integer)
    index_strategy: Mapped[str] = mapped_column(String, nullable=False, default="fixed")
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _ts_now()


class SavedReply(Base):
    """Local-only saved replies (a.k.a. "templates" minus the welcome slot).

    OF's API rejects creates against `/messages/templates` for everything
    except `template=reply_on_subscribe`, so we keep saved replies in our
    own DB and never round-trip them through OF. The welcome message
    stays on OF.

    `media_json` is a serialized array of vault-media references
    (`[{id, type, files?}]`) so the editor can re-render thumbs without
    a second fetch. We don't FK to vault_items because the media may
    not have been imported yet on first edit. """
    __tablename__ = "saved_replies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[str] = mapped_column(
        String, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False,
    )
    title: Mapped[str | None] = mapped_column(String)   # short label for the picker
    text: Mapped[str] = mapped_column(Text, nullable=False)
    price_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    locked_text: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    media_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    # OF user ids of creators to @-tag when this template fires. Picker
    # in the editor enforces validity — empty array means "no tagging".
    tagged_users_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    # Vault-media ids inside `media_json` that ride along UNLOCKED when
    # `price_cents > 0`. Empty array means "everything PPV-locked".
    previews_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    # Optional Giphy id; when set, picking this template seeds the composer's
    # picked-GIF state so the GIF is sent alongside. NULL = no GIF.
    gif_id: Mapped[str | None] = mapped_column(String)
    # Cached animated preview URL — lets the editor and picker render the
    # GIF chip without a fresh Giphy roundtrip.
    gif_url: Mapped[str | None] = mapped_column(Text)
    # Optional script grouping. When both are set, sending this template in
    # a chat advances a per-chat cursor; the composer then surfaces the
    # template with the next `script_step` (same `script_id`) as a one-tap
    # suggestion bubble. NULL on either field opts the template out.
    script_id: Mapped[str | None] = mapped_column(String)
    script_step: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = _ts_now()
    updated_at: Mapped[datetime] = _ts_now()

    __table_args__ = (
        Index("ix_saved_replies_account", "account_id", "updated_at"),
    )


class VaultSend(Base):
    """Per-fan send history. Powers 'remember new price' + analytics."""
    __tablename__ = "vault_sends"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[str] = mapped_column(String, nullable=False)
    fan_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    media_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    message_id: Mapped[int | None] = mapped_column(BigInteger)
    price_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sent_at: Mapped[datetime] = _ts_now()
    was_purchased: Mapped[bool | None] = mapped_column(Boolean)

    __table_args__ = (
        Index("ix_vault_sends_fan", "account_id", "fan_id", "sent_at"),
        Index("ix_vault_sends_media", "account_id", "media_id", "sent_at"),
    )


class TipRewardLog(Base):
    """One row per tip the tip_reward automation has already acted on — the
    idempotency guard (a webhook can fire the same tip twice) AND an audit trail.

    PK is (account_id, tip_message_id): the OF message the tip rode in on uniquely
    identifies the tip, so an INSERT-OR-IGNORE on it guarantees exactly one reward
    per tip. `images_sent=0` is a valid, recorded outcome (e.g. the fan has already
    received every image in the tier's folders) — it still blocks re-processing."""
    __tablename__ = "tip_reward_log"

    account_id: Mapped[str] = mapped_column(String, primary_key=True)
    tip_message_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    fan_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    tip_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # max(this tip, rolling-window tip sum) — the value the folder tier was picked on.
    basis_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tier_name: Mapped[str | None] = mapped_column(String)
    images_sent: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reward_message_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = _ts_now()

    __table_args__ = (
        Index("ix_tip_reward_log_fan", "account_id", "fan_id", "created_at"),
    )


class Post(Base):
    """Draft / scheduled / posted / failed — the post timeline."""
    __tablename__ = "posts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[str] = mapped_column(
        String, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    of_post_id: Mapped[int | None] = mapped_column(BigInteger)
    temp_id: Mapped[str | None] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, nullable=False)
    text: Mapped[str | None] = mapped_column(Text)
    price_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    media_ids: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    label_ids: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime)
    failed_reason: Mapped[str | None] = mapped_column(Text)
    excluded_list_ids: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    created_by_employee_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("employees.id", ondelete="SET NULL")
    )
    raw_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _ts_now()
    updated_at: Mapped[datetime] = _ts_now()

    __table_args__ = (
        Index("ix_posts_account_status", "account_id", "status", "scheduled_for"),
    )


class List(Base):
    """OF list / our local list. kind drives behavior:
       regular     — mirrors an OF list
       exclude     — automatic skip in mass DM
       hidden      — drop from default inbox
       post_label  — labels on profile posts
       smart       — saved query (query_json)"""
    __tablename__ = "lists"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[str] = mapped_column(
        String, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    of_list_id: Mapped[int | None] = mapped_column(BigInteger)
    name: Mapped[str] = mapped_column(String, nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    query_json: Mapped[str | None] = mapped_column(Text)
    is_system: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = _ts_now()


class ListMember(Base):
    __tablename__ = "list_members"

    list_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("lists.id", ondelete="CASCADE"), primary_key=True
    )
    fan_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    added_at: Mapped[datetime] = _ts_now()
    added_by_employee_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("employees.id", ondelete="SET NULL")
    )


# ── §4.6 AI / automation tables ─────────────────────────────────────

class FanProfile(Base):
    """Grok-generated profile. Linked back to the grok_calls row that produced it."""
    __tablename__ = "fan_profiles"

    account_id: Mapped[str] = mapped_column(String, primary_key=True)
    fan_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    original_name: Mapped[str | None] = mapped_column(String)
    message_count_at_gen: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_spend_at_gen_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    nickname: Mapped[str | None] = mapped_column(String)
    short_bio: Mapped[str | None] = mapped_column(Text)
    bullet_points: Mapped[str | None] = mapped_column(Text)
    q1: Mapped[str | None] = mapped_column(Text)
    q2: Mapped[str | None] = mapped_column(Text)
    q3: Mapped[str | None] = mapped_column(Text)
    tease1: Mapped[str | None] = mapped_column(Text)
    tease2: Mapped[str | None] = mapped_column(Text)
    tease3: Mapped[str | None] = mapped_column(Text)
    applied_notes: Mapped[str | None] = mapped_column(Text)
    notes_applied_successfully: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    last_applied_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_applied_nickname: Mapped[str | None] = mapped_column(String)
    last_applied_notes: Mapped[str | None] = mapped_column(Text)
    last_generated_at: Mapped[datetime] = _ts_now()
    # Cross-reference to the prompt that generated this profile + the call audit row.
    generated_by_grok_call_id: Mapped[int | None] = mapped_column(Integer)


# ── §4.2/4.3 Per-fan safety + trigger lists ──────────────────────────

class FanTrigger(Base):
    """Do-not-say list per fan. Per spec 03 §4.2."""
    __tablename__ = "fan_triggers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[str] = mapped_column(String, nullable=False)
    fan_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    trigger_phrase: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str] = mapped_column(String, nullable=False)  # soft / hard
    reason: Mapped[str | None] = mapped_column(Text)
    flagged_at: Mapped[datetime] = _ts_now()
    flagged_by_employee_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("employees.id", ondelete="SET NULL")
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["account_id", "fan_id"],
            ["fans.account_id", "fans.fan_id"],
            ondelete="CASCADE",
        ),
        Index("ix_fan_triggers_fan", "account_id", "fan_id"),
    )


class FanSafetyFlag(Base):
    """Abuse / minor / scam / suicide-risk detection. Per spec 03 §4.3.
    Append-only — never delete. Critical flags should block automations
    from sending to this fan."""
    __tablename__ = "fan_safety_flags"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[str] = mapped_column(String, nullable=False)
    fan_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    flag_type: Mapped[str] = mapped_column(String, nullable=False)
    # minor / suicide_risk / harassment / scam / bot / impersonator
    severity: Mapped[str] = mapped_column(String, nullable=False)  # info / warn / critical
    details: Mapped[str | None] = mapped_column(Text)
    flagged_at: Mapped[datetime] = _ts_now()
    flagged_by: Mapped[str] = mapped_column(String, nullable=False)  # grok / human / automated
    flagged_by_employee_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("employees.id", ondelete="SET NULL")
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime)
    resolution: Mapped[str | None] = mapped_column(String)
    # false_positive / confirmed / unclear

    __table_args__ = (
        ForeignKeyConstraint(
            ["account_id", "fan_id"],
            ["fans.account_id", "fans.fan_id"],
            ondelete="CASCADE",
        ),
        Index("ix_fan_safety_flags_fan", "account_id", "fan_id"),
        # Partial index: open (unresolved) critical flags drive the
        # automation-block check — keep that lookup cheap.
        Index(
            "ix_fan_safety_flags_open_critical",
            "account_id", "fan_id",
            sqlite_where=text("resolved_at IS NULL AND severity = 'critical'"),
            postgresql_where=text("resolved_at IS NULL AND severity = 'critical'"),
        ),
    )


class FollowupState(Base):
    """Drip state machine — 26h → 64h → 256h thresholds (automation pack §07)."""
    __tablename__ = "followup_state"

    account_id: Mapped[str] = mapped_column(String, primary_key=True)
    fan_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    phase: Mapped[str] = mapped_column(String, nullable=False, default="tracking")
    cycle: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    silence_started_at: Mapped[datetime | None] = mapped_column(DateTime)
    fan_last_reply_at: Mapped[datetime | None] = mapped_column(DateTime)
    cooldown_until: Mapped[datetime | None] = mapped_column(DateTime)
    messages_sent: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    completed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = _ts_now()


class WelcomeSent(Base):
    """Dedup welcome-message sends per fan."""
    __tablename__ = "welcome_sent"

    account_id: Mapped[str] = mapped_column(String, primary_key=True)
    fan_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    fan_username: Mapped[str | None] = mapped_column(String)
    sent_at: Mapped[datetime] = _ts_now()


class SkipList(Base):
    """Per-account 'don't auto-reply' list."""
    __tablename__ = "skip_list"

    account_id: Mapped[str] = mapped_column(String, primary_key=True)
    fan_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    reason: Mapped[str | None] = mapped_column(String)
    added_at: Mapped[datetime] = _ts_now()


class Blacklist(Base):
    """Global ban — applies across every model."""
    __tablename__ = "blacklist"

    fan_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    reason: Mapped[str | None] = mapped_column(String)
    added_at: Mapped[datetime] = _ts_now()


class AccountAiConfig(Base):
    """Per-model AI voice + caps. Persona + time_activities used by welcome/followup."""
    __tablename__ = "account_ai_config"

    account_id: Mapped[str] = mapped_column(
        String, ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True
    )
    persona: Mapped[str | None] = mapped_column(Text)
    welcome_rules: Mapped[str | None] = mapped_column(Text)
    utc_offset: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    location: Mapped[str | None] = mapped_column(String)
    # JSON dict {morning_1, morning_2, afternoon_1, afternoon_2, evening, night}
    time_activities_json: Mapped[str | None] = mapped_column(Text)
    # Parallel per-slot vault image IDs, same 6 keys → media_id (int). When set,
    # welcome/followup attach the configured image for the current slot directly
    # (no vault-folder lookup). A future templates UI writes this. NULL → fall
    # back to the legacy folder picker. e.g. {"morning_1": 3663527656, ...}
    time_images_json: Mapped[str | None] = mapped_column(Text)
    # Operator-approved FIXED welcome activity line per slot (the "pin" from the
    # Brain preview: reroll until you like one, keep it, and send_welcome sends that
    # exact line instead of re-rolling a fresh AI restyle each run). JSON dict
    # {slot_key: {"line": str, "weekday": str}} — the stored weekday is swapped to
    # the current day at send so a daily welcome never says the wrong weekday. NULL /
    # missing slot → normal AI restyle. e.g. {"afternoon_1": {"line": "just got back
    # from the beach, Thursday arvo in Argentina lol", "weekday": "Thursday"}}
    welcome_pinned_json: Mapped[str | None] = mapped_column(Text)
    daily_cost_cap_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    # Per-account LLM model override. NULL → llm_client's default model
    # (grok-4-1-fast-non-reasoning), preserving current behavior. See 19 §4.
    model: Mapped[str | None] = mapped_column(String)
    # Optional per-purpose override, JSON e.g. {"gen_info":"deepseek-v4-flash",
    # "of_ai_chat":"grok-4-1-fast-non-reasoning"}. NULL → use `model` for all.
    model_by_purpose: Mapped[str | None] = mapped_column(Text)
    # nudge_online (P4): per-account config for the "message a fan when they come
    # online" automation — JSON {enabled, content_mode, repeat_mode, delay_minutes,
    # jitter_minutes, gap_minutes, min_hours_between_nudges, max_per_tick,
    # quiet_hours, online_recent_minutes, slots:{...}}. NULL → nudge_online's
    # built-in defaults (disabled until a rule + config enable it).
    nudge_config_json: Mapped[str | None] = mapped_column(Text)
    # W7 webhook-priority dispatch: per-account gate for real-time, event-driven
    # dispatch off inbound WS events. JSON {enabled: bool}. NULL/absent → OFF
    # (the safe default — no account reacts in real time until explicitly
    # enabled). A global env kill-switch (W7_WEBHOOK_DISPATCH_DISABLED) overrides
    # this. Kept separate from nudge_config_json to avoid the shallow-merge
    # collision in nudge_online._load_nudge_config.
    webhook_config_json: Mapped[str | None] = mapped_column(Text)
    # Per-account config for the `autoreply` automation (keep-warm re-engagement
    # of quiet, low-spend known fans). JSON: {enabled, silence_min_minutes,
    # silence_max_minutes, max_nudges, min_gap_minutes, max_lifetime_spend_cents,
    # recent_spend_days, max_recent_spend_cents, min_days_since_purchase,
    # min_days_since_first_chat, last_n_messages, quiet_hours_json}. Absent/NULL =
    # OFF. Separate column to avoid the nudge/webhook shallow-merge collision.
    autoreply_config_json: Mapped[str | None] = mapped_column(Text)
    # Per-automation opt-in for the "human texting style" package (short/casual
    # girl voice + 3-bubble splitting + casualized lowercase Q/Tease). JSON
    # {"of_ai_chat": bool, "autoreply": bool, "deep_convo": bool}. Absent/NULL or
    # a missing key → OFF for that automation (CURRENT behavior, byte-for-byte).
    # Own column to avoid the nudge/webhook shallow-merge collision.
    style_config_json: Mapped[str | None] = mapped_column(Text)
    # tip_reward (P4): per-account config for the "send vault images when a fan
    # tips" automation. JSON {enabled, dollars_per_image, min_images, max_images,
    # caption, window_hours, tiers:[{name, min_basis_cents, folders:[name,...]}]}.
    # Absent/NULL → tip_reward's built-in defaults (DISABLED — ships off, empty
    # folders, until a creator fills folder names + ticks enabled). Own column to
    # avoid the nudge/webhook shallow-merge collision.
    tip_reward_config_json: Mapped[str | None] = mapped_column(Text)
    # ai_chatter (PPVscriptAI): per-account config for the freestyle selling
    # chatter that REPLACES of_ai_chat for fans under the spend gate. JSON
    # {enabled, mode: "backup"|"always", sla_minutes, max_lifetime_spend_cents,
    # offer_mode: "tip"|"ppv"|"both", max_offers_per_fan_per_day,
    # min_fan_msgs_between_offers, max_fans_per_tick, stall_ttl_hours,
    # resume_after_manual_hours}. Absent/NULL → ai_chatter's built-in defaults
    # (DISABLED — ships off until a creator enables it). Own column to avoid
    # the nudge/webhook shallow-merge collision.
    ai_chatter_config_json: Mapped[str | None] = mapped_column(Text)
    # PPV Library: the premade-PPV store + global price-matrix knobs for the
    # `ppv_send` automation. JSON {enabled, matrix:{spend_bands, recency_bands},
    # ppvs:[{id, name, media_ids, caption_pool_key, base_price_cents,
    # preview_options, sends_per_week, resend_monthly, enabled}]}. On save the
    # config API upserts one `ppv_send` AutomationRule per enabled PPV (cadence =
    # 604800/sends_per_week). Absent/NULL → DISABLED, empty library. Own column to
    # avoid the nudge/webhook shallow-merge collision.
    ppv_library_config_json: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = _ts_now()


class MassMessageFunnel(Base):
    """Funnel definitions — port of the JSON funnels (automation pack §15) into DB."""
    __tablename__ = "mass_message_funnels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(Text)
    opening_message: Mapped[str] = mapped_column(Text, nullable=False)
    vault_folder: Mapped[str | None] = mapped_column(String)
    media_indices: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    opening_vault_folder: Mapped[str | None] = mapped_column(String)
    opening_media_indices: Mapped[str | None] = mapped_column(Text)
    steps_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = _ts_now()
    updated_at: Mapped[datetime] = _ts_now()


class FunnelAccountMedia(Base):
    """Per-(funnel, account) MEDIA binding for a shared funnel.

    A funnel's TEXT (opener + step copy/prompts/price) is GLOBAL — one
    definition shared across all of an owner's models (MassMessageFunnel). The
    MEDIA is NOT: an OnlyFans vault id is per-account, so an id picked on model A
    doesn't exist in model B's vault. Each model maps its OWN vault media to the
    shared funnel here, and reply_mass_funnel / send_mass_message resolve it by
    (funnel_id, account_id) at SEND time.

    `opening_media_ids` — JSON int array: the opener's vault media for this model.
    `steps_media_json`  — JSON object keyed by the step NUMBER (string), each
                          value `{"media_files": [int...], "previews": [int...]}`
                          — the vault media (and free-preview subset) this model
                          attaches to that funnel step. e.g.
                          `{"4": {"media_files": [123], "previews": [123]}}`.
                          Keyed by step number (not array index) so it survives a
                          step list that doesn't start at / isn't dense from 1.
    """
    __tablename__ = "funnel_account_media"

    funnel_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("mass_message_funnels.id", ondelete="CASCADE"),
        primary_key=True,
    )
    account_id: Mapped[str] = mapped_column(
        String, ForeignKey("accounts.id"), primary_key=True,
    )
    opening_media_ids: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    steps_media_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = _ts_now()
    updated_at: Mapped[datetime] = _ts_now()


class MassRun(Base):
    """One broadcast = one mass_runs row. funnel_state links per-fan state to this."""
    __tablename__ = "mass_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[str] = mapped_column(
        String, ForeignKey("accounts.id"), nullable=False
    )
    funnel_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("mass_message_funnels.id")
    )
    started_by_employee_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("employees.id")
    )
    audience_filter: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    recipient_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # OF's broadcast queue id (result["id"]) — lets the Mass Messages tab join
    # mass_broadcast_cache.queue_id → this row to surface which automation +
    # funnel produced a cached broadcast. NULL for legacy pre-0032 runs.
    queue_id: Mapped[int | None] = mapped_column(BigInteger)
    # Which automation minted this broadcast (send_mass_message,
    # reply_mass_funnel, mass_nudge, online_blast). NULL = manual broadcast
    # from the UI or a legacy pre-0032 run.
    automation_kind: Mapped[str | None] = mapped_column(String)
    started_at: Mapped[datetime] = _ts_now()
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String, nullable=False, default="running")
    # When the first mass was unsent/deleted → reply_mass_funnel STOPS enrolling
    # NEW repliers off this run (only replies at/before this cutoff enroll); the
    # walker keeps advancing fans already engaged until #30 halts them on a buy.
    # Stamped by close_funnel_discovery_for_queue on every unsend path.
    # Nullable/additive: init_db self-heals it via ALTER TABLE (no migration).
    discovery_closed_at: Mapped[datetime | None] = mapped_column(DateTime)

    __table_args__ = (
        # Mass Messages tab joins the cache to a run by (account_id, queue_id).
        Index(
            "ix_mass_runs_queue",
            "account_id",
            "queue_id",
            sqlite_where=text("queue_id IS NOT NULL"),
            postgresql_where=text("queue_id IS NOT NULL"),
        ),
    )


class FunnelState(Base):
    """Per-(mass_run, fan) state machine for reply_mass_funnel."""
    __tablename__ = "funnel_state"

    mass_run_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("mass_runs.id", ondelete="CASCADE"), primary_key=True
    )
    fan_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    current_step: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_check_at: Mapped[datetime | None] = mapped_column(DateTime)
    check_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    last_error: Mapped[str | None] = mapped_column(Text)
    # When this funnel last sent a paid_ppv step to the fan — the baseline for the
    # "offer taken" halt (#30). Nullable/additive: init_db self-heals it via
    # ALTER TABLE, so no migration (mirrors the rest of funnel_state).
    last_ppv_sent_at: Mapped[datetime | None] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = _ts_now()


class FunnelResponder(Base):
    """Durable per-(account, funnel) ledger of fans who ANSWERED a funnel's
    opener — the "already answered this campaign" dedup source (R1/R2).

    Written by reply_mass_funnel at DISCOVERY (a confirmed recipient of a run of
    this funnel who replied to the opener), so it is:
      • precise — only real opener-repliers, NOT anyone who happened to message
        us after the run started (avoids over-excluding ai_chatter/human replies),
      • durable — survives funnel_state pruning + the run's messages being unsent,
      • per-funnel — keyed on funnel_id, so re-clicking Send skips prior answerers
        while a fresh audience is unaffected.
    resolve_mass_audience subtracts these ids from a funnel send's recipients.
    (A future "reset responders for this funnel" action would DELETE these rows to
    allow re-targeting the same funnel to the same fans.)"""
    __tablename__ = "funnel_responders"

    account_id: Mapped[str] = mapped_column(
        String, ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True
    )
    funnel_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("mass_message_funnels.id", ondelete="CASCADE"),
        primary_key=True,
    )
    fan_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # The run that first observed the reply (provenance; not part of the key so a
    # later run of the same funnel doesn't double-insert the same answerer).
    mass_run_id: Mapped[int | None] = mapped_column(Integer)
    first_replied_at: Mapped[datetime] = _ts_now()


class CatalogScript(Base):
    """An ordered 'sexting sequence' — a themed series of catalog items that
    ai_chatter sells one piece at a time (escalation order = item position).
    Singles (one-off clips / photo sets) are catalog_items with script_id NULL.
    `theme` is shown to the LLM (setting/outfit/arc), never to fans."""
    __tablename__ = "catalog_scripts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[str] = mapped_column(
        String, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    theme: Mapped[str | None] = mapped_column(Text)
    # draft → not offered; enabled → ai_chatter may sell it; disabled → retired.
    status: Mapped[str] = mapped_column(String, nullable=False, default="draft")
    created_at: Mapped[datetime] = _ts_now()
    updated_at: Mapped[datetime] = _ts_now()

    __table_args__ = (
        Index("ix_catalog_scripts_account", "account_id", "name", unique=True),
    )


class CatalogItem(Base):
    """One sellable unit: a video, an image, or an image set (N media sold as one
    bundle). Belongs to a script (script_id + position = escalation order) or
    stands alone (script_id NULL = a single, e.g. a bed-dance clip).

    `description_for_ai` is the contract: what the fan actually SEES, 1-2
    sentences, present tense. The LLM may only tease/claim what's written here —
    it is the sole source for pitches and for answering "what will we do?"."""
    __tablename__ = "catalog_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[str] = mapped_column(
        String, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    script_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("catalog_scripts.id", ondelete="CASCADE")
    )
    position: Mapped[int | None] = mapped_column(Integer)  # order within script
    kind: Mapped[str] = mapped_column(String, nullable=False, default="video")
    label: Mapped[str | None] = mapped_column(String)      # e.g. "BOOB TEASE"
    description_for_ai: Mapped[str | None] = mapped_column(Text)
    media_ids: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    # Free teaser frames attached unlocked on a PPV send (OF `previews`).
    preview_media_ids: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    duration_sec: Mapped[int | None] = mapped_column(Integer)
    # Unlock terms. 0 disables that mode for this item; is_free_teaser items are
    # sent free to build momentum (initiation pics, item 1).
    price_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tip_unlock_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_free_teaser: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    tags: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = _ts_now()
    updated_at: Mapped[datetime] = _ts_now()

    __table_args__ = (
        Index("ix_catalog_items_script", "account_id", "script_id", "position"),
    )


class CatalogProgress(Base):
    """Per-(account, fan, script) position pin. NOT a chat state machine —
    ai_chatter freestyles the conversation; this row only constrains WHAT it may
    offer next (escalation order) and enforces once-per-fan-per-script. `position`
    is the index of the NEXT item to offer. status: active|stalled|abandoned|done.
    A stalled row past the TTL goes abandoned and the fan returns to the normal
    pool (and becomes a 'we never finished 😏' followup hook)."""
    __tablename__ = "catalog_progress"

    account_id: Mapped[str] = mapped_column(String, primary_key=True)
    fan_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    script_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("catalog_scripts.id", ondelete="CASCADE"), primary_key=True
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String, nullable=False, default="active")
    started_at: Mapped[datetime] = _ts_now()
    updated_at: Mapped[datetime] = _ts_now()


class ContentOffer(Base):
    """One concrete offer ai_chatter made ('unlock this for $N / tip $M and i'll
    send it'). The OPEN offer is the unlock watcher's anchor: inbound tips since
    `offered_at` accumulate into `tips_accum_cents` toward `tip_unlock_cents`;
    the ledger ingest flipping messages.is_paid on `offer_message_id` resolves a
    PPV. Doubles as the offer rate-limit source and the per-item conversion log.

    status: open → unlocked (paid, tip mode awaiting delivery) → delivered;
    PPV resolves straight to delivered (media was inside the locked message).
    expired = stall TTL; cancelled = superseded/killed by a human."""
    __tablename__ = "content_offers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[str] = mapped_column(
        String, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    fan_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    item_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("catalog_items.id", ondelete="CASCADE"), nullable=False
    )
    script_id: Mapped[int | None] = mapped_column(Integer)
    mode: Mapped[str] = mapped_column(String, nullable=False, default="tip")
    # Terms snapshot at offer time (catalog rows can be edited later).
    price_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tip_unlock_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    offer_message_id: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String, nullable=False, default="open")
    resolved_by: Mapped[str | None] = mapped_column(String)  # tip|ppv_ledger|ppv_fastpath|manual
    tips_accum_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    delivery_message_id: Mapped[int | None] = mapped_column(BigInteger)
    offered_at: Mapped[datetime] = _ts_now()
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = _ts_now()

    __table_args__ = (
        # The watcher's hot path: the open offer for (account, fan).
        Index("ix_content_offers_open", "account_id", "fan_id", "status"),
        # Conversion stats per item ("which content actually sells").
        Index("ix_content_offers_item", "account_id", "item_id", "offered_at"),
    )


class AutomationRule(Base):
    """Declarative automation. Produces scheduled_jobs at runtime."""
    __tablename__ = "automation_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[str] = mapped_column(
        String, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    trigger_json: Mapped[str] = mapped_column(Text, nullable=False)
    steps_json: Mapped[str] = mapped_column(Text, nullable=False)
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    quiet_hours_json: Mapped[str | None] = mapped_column(Text)
    frequency_caps_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _ts_now()


class ScheduledJob(Base):
    """Worker queue. The single async worker reads from this."""
    __tablename__ = "scheduled_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[str] = mapped_column(
        String, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String, nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    run_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    rule_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("automation_rules.id", ondelete="SET NULL")
    )
    created_by_employee_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("employees.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = _ts_now()

    __table_args__ = (
        Index("ix_jobs_due", "run_at", "status"),
    )


class AutomationRun(Base):
    """Periodic-run audit log. So you can see 'gen_info_sweep last ran 8 min ago.'"""
    __tablename__ = "automation_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[str | None] = mapped_column(String)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    started_at: Mapped[datetime] = _ts_now()
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String, nullable=False, default="running")
    stats_json: Mapped[str | None] = mapped_column(Text)
    error_text: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        Index("ix_runs_kind_started", "kind", "started_at"),
    )


class FanLease(Base):
    """Per-fan send lease used by the automation executor (T-EXEC /
    MASTER_PLAN §5). Before any automation sends to a fan it must hold this
    lease, so two automations (A05/A06/A07/A11) can't message the same fan in
    overlapping ticks. PK (account_id, fan_id) = at most one live lease per
    fan; the holder writes a short leased_until TTL and releases (or lets it
    expire) after the send. One bot message per fan per cycle."""
    __tablename__ = "fan_lease"

    account_id: Mapped[str] = mapped_column(String, primary_key=True)
    fan_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    automation_kind: Mapped[str] = mapped_column(String, nullable=False)
    leased_until: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    acquired_at: Mapped[datetime] = _ts_now()

    __table_args__ = (
        # Cheap expiry sweeps: DELETE ... WHERE leased_until < now.
        Index("ix_fan_lease_expiry", "leased_until"),
    )


class NudgeState(Base):
    """Per-(account, fan) state for the nudge_online automation — "message a fan
    when they come online". Dedicated table (NOT fans.last_online_at, which the WS
    pump overwrites) so the detector's poll-and-diff and the re-engagement cap own
    their own timestamps.

      • last_seen_online_at — refreshed every tick a fan is in the online set;
        the detector's `newly_online` diff and the fire-job's recency re-validate
        both read this. Stale (> gap_minutes) ⇒ the fan re-qualifies.
      • last_nudged_at / nudge_count — drive the `min_hours_between_nudges`
        re-engagement cap (default 12h).
      • no_reply_streak — consecutive nudges with no inbound since; the no-reply
        backoff gate stops nagging a non-responder.
      • pending_job_id — the enqueued `nudge_online_fire` job awaiting its delay;
        dedup latch (skip re-scheduling while one is pending). CLEARED by the
        fire-job on send OR cancel.
      • last_variation_idx / last_slot — `repeat_mode` rotation: `new` advances
        the index through the slot's pool, `same` reuses it. NULL last_slot ⇒
        never nudged (seed the index from the fan id).

    Nudge is fully async: it NEVER sets fans.automation_paused_until (the shared
    cooldown), so of_ai_chat/deep_convo are never frozen by a nudge — it gates
    only on its own cap here.
    """
    __tablename__ = "nudge_state"

    account_id: Mapped[str] = mapped_column(String, primary_key=True)
    fan_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    last_seen_online_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_nudged_at: Mapped[datetime | None] = mapped_column(DateTime)
    nudge_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    no_reply_streak: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pending_job_id: Mapped[int | None] = mapped_column(Integer)
    last_variation_idx: Mapped[int | None] = mapped_column(Integer)
    last_slot: Mapped[str | None] = mapped_column(String)
    updated_at: Mapped[datetime] = _ts_now()

    __table_args__ = (
        # Detector warm-up check + per-account state load.
        Index("ix_nudge_state_account", "account_id", "last_seen_online_at"),
    )


class AutoreplyState(Base):
    """Per-(account, fan) state for the `autoreply` automation — re-engage a quiet,
    low-spend known fan when WE spoke last and they've gone silent for a few
    minutes. Tracks the current silence "spell" so we never over-nudge:

      • spell_inbound_at — the fan's last_message_received_at at the time the
        current spell began. When the fan replies (their received_at advances
        past this), the spell resets and nudges_sent goes back to 0.
      • nudges_sent — autoreplies sent in the CURRENT spell; gated by the
        configurable max_nudges (default 1) so we stop until they reply.
      • last_nudge_at — enforces min_gap_minutes between nudges in a spell.

    Like nudge_online, autoreply NEVER sets fans.automation_paused_until (it gates
    on its own cap), so of_ai_chat/deep_convo are never frozen by it.
    """
    __tablename__ = "autoreply_state"

    account_id: Mapped[str] = mapped_column(String, primary_key=True)
    fan_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    spell_inbound_at: Mapped[datetime | None] = mapped_column(DateTime)
    nudges_sent: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_nudge_at: Mapped[datetime | None] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = _ts_now()


# ── §4.7 Prompts (editable templates, versioned) ────────────────────

class Prompt(Base):
    """Editable Grok prompt template. Per-account override via NULL fallback.
    Variables substituted at call time via simple {{var}} replacement."""
    __tablename__ = "prompts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    purpose: Mapped[str] = mapped_column(String, nullable=False)
    account_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("accounts.id", ondelete="CASCADE")
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    variables_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    # Optional JSON-schema for Grok's response — when set, the runtime
    # rejects responses that don't validate. Nullable until §9 wires up.
    response_schema_json: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = _ts_now()
    created_by_employee_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("employees.id", ondelete="SET NULL")
    )

    __table_args__ = (
        # (name, account_id, version) is unique. SQLite treats NULL as
        # distinct in UNIQUE, so all-NULL account_id rows coexist by name.
        UniqueConstraint("name", "account_id", "version", name="uq_prompts_name_account_version"),
        # Active lookup: (name, account_id) → highest active version.
        Index(
            "ix_prompts_active",
            "name",
            "account_id",
            "version",
            sqlite_where=text("is_active = 1"),
            postgresql_where=text("is_active = TRUE"),
        ),
    )


# ── §4.8 Grok call log ──────────────────────────────────────────────

class GrokCall(Base):
    """Every Grok API call. Logged BEFORE the request so failures don't lose audit."""
    __tablename__ = "grok_calls"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    purpose: Mapped[str] = mapped_column(String, nullable=False)
    account_id: Mapped[str | None] = mapped_column(String)
    fan_id: Mapped[int | None] = mapped_column(BigInteger)
    model: Mapped[str] = mapped_column(String, nullable=False)
    endpoint: Mapped[str] = mapped_column(String, nullable=False)
    temperature: Mapped[float | None] = mapped_column(Numeric(4, 2))
    prompt_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("prompts.id", ondelete="SET NULL")
    )
    prompt_json: Mapped[str] = mapped_column(Text, nullable=False)
    response_json: Mapped[str | None] = mapped_column(Text)
    response_text: Mapped[str | None] = mapped_column(Text)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    tokens_in: Mapped[int | None] = mapped_column(Integer)
    tokens_out: Mapped[int | None] = mapped_column(Integer)
    cost_cents: Mapped[int | None] = mapped_column(Integer)
    # Provider that served this call ('grok' | 'deepseek' | …). Default 'grok'
    # backfills the pre-multiprovider rows. See 19_llm_providers.md §3.
    provider: Mapped[str] = mapped_column(
        String, nullable=False, server_default=text("'grok'")
    )
    # DeepSeek bills prompt_cache_hit_tokens separately; NULL for providers
    # that don't report it.
    cache_hit_tokens: Mapped[int | None] = mapped_column(Integer)
    # 'pending' (written before the request) → 'done' | 'error'. llm_client
    # uses this once present; pre-migration it fell back to response_json IS NULL.
    status: Mapped[str] = mapped_column(
        String, nullable=False, server_default=text("'pending'")
    )
    was_dry_run: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    error_text: Mapped[str | None] = mapped_column(Text)
    called_at: Mapped[datetime] = _ts_now()

    __table_args__ = (
        Index("ix_grok_purpose_time", "purpose", "called_at"),
        Index(
            "ix_grok_fan",
            "account_id",
            "fan_id",
            "called_at",
            sqlite_where=text("fan_id IS NOT NULL"),
            postgresql_where=text("fan_id IS NOT NULL"),
        ),
        Index("ix_grok_prompt", "prompt_id", "called_at"),
    )


class GrokDailyCost(Base):
    """Daily LLM spend rollup. Source of truth for the soft cap enforcement.

    PK is `(day, account_id, provider)` so caps roll up per-account AND
    per-provider (grok vs deepseek vs …). account_id is nullable + part of the
    PK to allow a global rollup row alongside per-account rows; SQLite treats
    NULL as distinct in composite PKs. `provider` is NOT NULL (PK member) and
    defaults to 'grok' so pre-existing rows backfill cleanly — see migration
    0026_llm_provider, which mirrors the 0006 create-copy-swap PK rebuild."""
    __tablename__ = "grok_daily_cost"

    day: Mapped[str] = mapped_column(String, primary_key=True)  # 'YYYY-MM-DD' UTC
    account_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True
    )
    provider: Mapped[str] = mapped_column(
        String, primary_key=True, nullable=False, server_default=text("'grok'")
    )
    cost_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    call_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_capped: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    updated_at: Mapped[datetime] = _ts_now()


class ModelPricing(Base):
    """Registry of truth for per-model token pricing. The MODELS dict in
    service/llm_client.py is just the seed; this table is authoritative so
    rates can change without a code deploy. Rates are cents per 1k tokens and
    CAN be fractional (sub-cent per 1k), so Numeric — not the integer *_cents
    convention used for settled amounts."""
    __tablename__ = "model_pricing"

    model: Mapped[str] = mapped_column(String, primary_key=True)
    provider: Mapped[str] = mapped_column(String, nullable=False)
    input_per_1k_cents: Mapped[float] = mapped_column(
        Numeric(12, 6), nullable=False, server_default=text("0")
    )
    output_per_1k_cents: Mapped[float] = mapped_column(
        Numeric(12, 6), nullable=False, server_default=text("0")
    )
    updated_at: Mapped[datetime] = _ts_now()


# ── §4.9 Realtime / event inbox ─────────────────────────────────────

class EventInbox(Base):
    """Every WS event lands here first. Idempotent via (source, provider_event_id)."""
    __tablename__ = "event_inbox"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("accounts.id", ondelete="CASCADE")
    )
    source: Mapped[str] = mapped_column(String, nullable=False)
    provider_event_id: Mapped[str | None] = mapped_column(String)
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    received_at: Mapped[datetime] = _ts_now()
    processed_at: Mapped[datetime | None] = mapped_column(DateTime)
    processed_status: Mapped[str | None] = mapped_column(String)
    processed_error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        # Partial unique — only enforced when provider_event_id is present.
        Index(
            "uq_event_dedupe",
            "source",
            "provider_event_id",
            unique=True,
            sqlite_where=text("provider_event_id IS NOT NULL"),
            postgresql_where=text("provider_event_id IS NOT NULL"),
        ),
        Index(
            "ix_event_unprocessed",
            "processed_at",
            sqlite_where=text("processed_at IS NULL"),
            postgresql_where=text("processed_at IS NULL"),
        ),
    )


# ── §4.9b Application error log ─────────────────────────────────────

class AppError(Base):
    """Bug-hunter table: every unhandled exception (server or browser)
    lands here so we can review them without scraping log files.

    `source`  = "server" (FastAPI middleware) | "browser" (POSTed from
                the useErrorReporter hook).
    `kind`    = short tag — "unhandledrejection", "react-render",
                "fetch-error", "HTTPException", etc. Free-form; we just
                use it for filtering in /admin/errors.
    `message` = e.message or whatever short label the source provides.
    `stack`   = full stack trace if available.
    `url`     = window.location.href (browser) / request.url (server).
    `context` = JSON blob with anything else worth keeping (account_id,
                employee_id, user agent, query params, …).
    """
    __tablename__ = "app_errors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    occurred_at: Mapped[datetime] = _ts_now()
    source: Mapped[str] = mapped_column(String, nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False, default="error")
    message: Mapped[str] = mapped_column(Text, nullable=False)
    stack: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(Text)
    account_id: Mapped[str | None] = mapped_column(String)
    employee_id: Mapped[int | None] = mapped_column(Integer)
    user_agent: Mapped[str | None] = mapped_column(Text)
    context_json: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        Index("ix_app_errors_occurred", "occurred_at"),
        Index("ix_app_errors_source_kind", "source", "kind"),
    )


# ── §4.10 Speeds ─────────────────────────────────────────────────────

class Shortcut(Base):
    """Per-employee shortcuts: emoji bar, text replacements, hotkeys, scripts, chat jumps."""
    __tablename__ = "shortcuts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employee_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("employees.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String, nullable=False)
    trigger: Mapped[str | None] = mapped_column(String)
    body: Mapped[str | None] = mapped_column(Text)
    media_ids: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    price_cents: Mapped[int | None] = mapped_column(Integer)
    hotkey: Mapped[str | None] = mapped_column(String)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


# ── §4.11 Wall-media scan (incremental "posted on wall" tracking) ────

class WallMedia(Base):
    """Vault media IDs we've ever observed in this model's wall posts.
    Drives the blue "posted on wall" ring in the VaultPicker.

    Populated incrementally by /admin/vault/wall-media — each call walks
    OF's /posts feed forward (or backwards during backfill) and upserts
    rows here. Lookups read the union of every row for the account, so
    the ring is eventually-correct for prolific creators with >250
    lifetime posts (the old in-memory cap silently mis-flagged those)."""
    __tablename__ = "wall_media"

    account_id: Mapped[str] = mapped_column(
        String, ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True
    )
    media_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    post_id: Mapped[int | None] = mapped_column(BigInteger)
    # Wall-post publishedAt — used during backfill to walk older history
    # via OF's before_publish_time cursor.
    post_published_at: Mapped[datetime | None] = mapped_column(DateTime)
    # Highest price (in cents) this media was ever posted at on the wall.
    # 0 = free wall post (or price not yet observed). Drives the picker's
    # "WALL $X.XX" badge so a chatter can see the item is already public
    # PPV and at what price. Upserted via MAX() so a media seen in both a
    # free and a paid post keeps the paid signal (don't-undersell).
    price_cents: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    first_seen_at: Mapped[datetime] = _ts_now()

    # Redundant w/ the composite PK's leading column on SQLite, but kept
    # so `create_all()` and the Alembic migration produce identical
    # schemas across fresh-install / migrate paths.
    __table_args__ = (
        Index("ix_wall_media_account_id", "account_id"),
    )


class PerfEventRow(Base):
    """Client-side perfLog events, batch-ingested from the frontend so we
    can ask things like "across all chatters, what's the p95 from
    `vault.media requested` → `delivered`?" or "are popout windows on
    LAN-tunnel hosts slower than direct same-host opens?".

    Append-only. Pruned by `_perf_events_evict_once()` on a background
    timer (default 7 days; tune via env). No FK to accounts — the
    employee/account identity is best-effort `meta` payload, since the
    frontend may not know either at the moment a tab.open fires."""
    __tablename__ = "perf_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tab_id: Mapped[str] = mapped_column(String, nullable=False)
    parent_tab_id: Mapped[str | None] = mapped_column(String)
    op_id: Mapped[str] = mapped_column(String, nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    phase: Mapped[str] = mapped_column(String, nullable=False)
    # Client epoch ms — store as BigInteger because raw ms is more useful
    # than a parsed datetime when reconstructing intra-second sequences
    # (multiple events can land in the same millisecond).
    client_ts_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    received_at: Mapped[datetime] = _ts_now()
    # Soft identity hints — present when the client knew them at log time.
    employee_id: Mapped[int | None] = mapped_column(Integer)
    account_id: Mapped[str | None] = mapped_column(String)
    # JSON-encoded free-form meta. Capped at INGEST_META_MAX_BYTES
    # server-side so a runaway logger can't blow the row size.
    meta_json: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        Index("ix_perf_events_received_at", "received_at"),
        Index("ix_perf_events_tab_kind", "tab_id", "kind"),
        Index("ix_perf_events_op", "op_id"),
    )


class WallScanState(Base):
    """Per-account scan watermark for the wall-media walker. One row per
    account.

    Two phases:
      • Backfill (fully_backfilled=False): we haven't seen the bottom of
        the post feed yet. Subsequent calls walk backward from
        oldest_post_published_at.
      • Refresh (fully_backfilled=True): we've reached the bottom. Each
        call walks forward and stops as soon as it hits a post with
        publishedAt <= newest_post_published_at."""
    __tablename__ = "wall_scan_state"

    account_id: Mapped[str] = mapped_column(
        String, ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True
    )
    newest_post_published_at: Mapped[datetime | None] = mapped_column(DateTime)
    oldest_post_published_at: Mapped[datetime | None] = mapped_column(DateTime)
    fully_backfilled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    last_scan_at: Mapped[datetime | None] = mapped_column(DateTime)
    scanned_posts_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class TransactionScanHistory(Base):
    """Per-account watermark for the Phase F payouts/transactions ingest
    supervisor (service/transaction_ingest.py). Mirrors WallScanState's
    backfill→refresh state machine with the additional fields the ingest
    job needs (error/pause bookkeeping + insert/patch counters).

    Phases:
      • Backfill (fully_backfilled=False): walks 90d back from connect
        time toward `backfill_floor`; advances `oldest_seen_occurred_at`.
      • Refresh (fully_backfilled=True): re-fetches the trailing 7-day
        window every 10 min. `last_marker` is observability only —
        steady-state polling re-walks the window from scratch each tick.

    Auto-pause: after 3 consecutive failures the supervisor sets
    `paused_until = utcnow() + 1h` and `current_status='paused'`. Tick
    skips paused accounts until the cooldown elapses.
    """
    __tablename__ = "transaction_scan_history"

    account_id: Mapped[str] = mapped_column(
        String, ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True
    )
    last_scan_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_marker: Mapped[int | None] = mapped_column(Integer)  # observability only
    oldest_seen_occurred_at: Mapped[datetime | None] = mapped_column(DateTime)
    newest_seen_occurred_at: Mapped[datetime | None] = mapped_column(DateTime)
    fully_backfilled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("0")
    )
    backfill_floor: Mapped[datetime | None] = mapped_column(DateTime)
    current_status: Mapped[str] = mapped_column(
        String, nullable=False, server_default=text("'idle'")
    )
    last_error: Mapped[str | None] = mapped_column(String)
    consecutive_failures: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    paused_until: Mapped[datetime | None] = mapped_column(DateTime)
    rows_inserted_total: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    rows_patched_total: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    updated_at: Mapped[datetime] = _ts_now()


class MassBroadcastCache(Base):
    """Local cache of past mass-message broadcasts (OF queue rows).

    Source feed: GET /users/me/stats/messages/group. We pull this on a
    light schedule + on user-triggered "Refresh" and upsert here so the
    /settings → Mass messages tab opens instantly off SQLite instead of
    waiting for an OF roundtrip per account.

    Unlike per-chat unsend, mass-broadcast unsend has no edit window —
    `can_unsend` and `unsend_seconds` echo OF's flags so the UI knows
    whether the row is still actionable. When the user cancels, the
    DELETE on /messages/queue/{queue_id} succeeds and we flip
    `is_canceled=True` locally to avoid waiting for the next refresh.
    """
    __tablename__ = "mass_broadcast_cache"

    # NB: `text` would shadow the imported sqlalchemy.text() function
    # inside this class body, breaking every later `server_default=text(...)`
    # below — so we store OF's broadcast text under `body_text` and
    # serialize back to `text` at the boundary in mass_broadcast_cache.py.
    account_id: Mapped[str] = mapped_column(
        String, ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True
    )
    queue_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)
    body_text: Mapped[str | None] = mapped_column(Text)
    raw_text: Mapped[str | None] = mapped_column(Text)
    giphy_id: Mapped[str | None] = mapped_column(String)
    is_free: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("1"))
    is_tip: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("0"))
    media_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    media_json: Mapped[str | None] = mapped_column(Text)  # OF media[] passthrough
    previews_json: Mapped[str | None] = mapped_column(Text)  # int[] passthrough
    sent_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    viewed_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    is_canceled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("0"))
    can_unsend: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("1"))
    unsend_seconds: Mapped[int | None] = mapped_column(Integer)
    template: Mapped[str | None] = mapped_column(String)
    fetched_at: Mapped[datetime] = _ts_now()

    __table_args__ = (
        Index("ix_mass_broadcast_cache_account_sent_at", "account_id", "sent_at"),
    )


# ── §X.X Multi-tenant auth (friends-only) ─────────────────────────
#
# See plan/simple_username_auth_2026_05_24/PLAN.md.
#
# Two tables. `users` holds (username, password) — plaintext per user
# decision; the relay is only exposed to a small friends group behind the
# share-token gate. `user_accounts` is the ownership join: which OF
# `accounts` rows a given user can see. `_resolve_account_id` consults
# this on every request to gate cross-user reads/writes.

class User(Base):
    """A friend with login access. No email, no OAuth — username + password
    only. Password stored plaintext per user decision (friends-only scale)."""
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String, primary_key=True)  # uuid4 hex
    username: Mapped[str] = mapped_column(String, nullable=False, unique=True)  # stored lowercase
    password: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = _ts_now()
    # Sliding 30-day inactivity expiry — bumped on every authenticated
    # request by the session middleware. Cookie is rejected once
    # (now - last_seen_at) > 30 days.
    last_seen_at: Mapped[datetime] = _ts_now()
    # Permanent master role. A master sees/acts on EVERY account (its
    # request-time account_ids snapshot becomes the full registry set — see
    # auth._auth_session_middleware) and sees every owner's employee roster
    # (the user_id scoping in employees.py is bypassed). Distinct from the
    # transient founder impersonation cookie. Flip via the ADMIN_PASSWORD
    # founder gate or a DB update; migration 0039_user_is_admin.
    is_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("0"), default=False)


class UserAccount(Base):
    """Which OF accounts a user can see. Inserted on register (founder
    backfill) and on every successful login-capture (paste-curl / playwright
    bootstrap). Deleting a user CASCADE-clears these; deleting an OF account
    CASCADE-clears these too. The OF `accounts` row is the source of truth —
    these are pure ownership links."""
    __tablename__ = "user_accounts"

    user_id: Mapped[str] = mapped_column(
        String, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    account_id: Mapped[str] = mapped_column(
        String, ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True
    )
    created_at: Mapped[datetime] = _ts_now()


# ── §X.Y Chatter login (one human, union of owners' accounts) ────────
#
# See Todo.txt STEP 8. A Chatter is a separate principal from a User.
# Owners (Users) link Chatters via chatter_users; the chatter then sees
# the SET UNION of every linked owner's UserAccount rows as their picker
# choices. Audit attribution per-action still goes through Employee, but
# the Employee row is auto-created on demand per (chatter, owner) pair so
# each owner sees a single "Tim" in their roster regardless of which
# other owners Tim is also linked to.

class Chatter(Base):
    """A human chatter with login access. Distinct from User — Users are
    OF account OWNERS (founders, friends who run accounts); Chatters are
    the people who do the messaging work. One Chatter can be linked to
    many owners; one owner can link many Chatters. Picker selection
    persists per-tab in localStorage.

    Password column is plaintext per the same friends-scale risk decision
    as User.password — switch to a KDF before any wider exposure."""
    __tablename__ = "chatters"

    id: Mapped[str] = mapped_column(String, primary_key=True)  # uuid4 hex
    username: Mapped[str] = mapped_column(
        String, nullable=False, unique=True,
    )  # stored lowercase
    password: Mapped[str] = mapped_column(String, nullable=False)
    display_name: Mapped[str | None] = mapped_column(String)
    color: Mapped[str | None] = mapped_column(String)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = _ts_now()
    # Sliding 30-day inactivity expiry, same as User.last_seen_at — bumped
    # by the chatter session middleware on every authenticated request.
    last_seen_at: Mapped[datetime] = _ts_now()


class ChatterUser(Base):
    """Owner ↔ Chatter link. The chatter's available-accounts list is the
    UNION across all linked owners' UserAccount rows. CASCADE on both
    sides: deleting either party removes the link; existing Employee
    rows tied to the chatter on that owner stay (audit continuity) but
    become label-only when chatter_id is cleared via application code."""
    __tablename__ = "chatter_users"

    chatter_id: Mapped[str] = mapped_column(
        String, ForeignKey("chatters.id", ondelete="CASCADE"), primary_key=True,
    )
    user_id: Mapped[str] = mapped_column(
        String, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True,
    )
    created_at: Mapped[datetime] = _ts_now()


class ChatterInvite(Base):
    """Owner-issued single-use signup token. The owner mints one + shares
    the URL with the new chatter; on first login that token grants access
    to /chatter/register AND auto-inserts the chatter_users row pointing
    back at the issuing owner. 24h TTL by default (see chatters.py)."""
    __tablename__ = "chatters_invites"

    token: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(
        String, ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    created_at: Mapped[datetime] = _ts_now()
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime)
    used_by_chatter_id: Mapped[str | None] = mapped_column(String)


# ── §X.Z Chatter access (owner-set per-chatter visibility limits) ────
#
# See library/CHATTER_ACCESS_PROMPTS.md. Two SUBTRACTIVE allowlists scoped
# per (chatter, owner). Both default to "empty = full access" so existing
# chatters are unaffected. Restriction applies ONLY when the acting
# principal is a chatter (no User cookie) — a dual-cookie owner is never
# limited (DA-1). To remove ALL access the owner UNLINKS the chatter; an
# empty restricted set is never persisted (DA-2).

class ChatterAccountAccess(Base):
    """Per (chatter, owner): which of THAT owner's models the chatter may
    see. If >=1 row exists for (chatter_id, user_id), that owner's
    contribution to the chatter's account union is restricted to the listed
    account_ids. No rows ⇒ no restriction (full access to that owner's
    models). Owner A's rows never affect owner B's contribution."""
    __tablename__ = "chatter_account_access"

    chatter_id: Mapped[str] = mapped_column(
        String, ForeignKey("chatters.id", ondelete="CASCADE"), primary_key=True,
    )
    user_id: Mapped[str] = mapped_column(
        String, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True,
    )
    account_id: Mapped[str] = mapped_column(
        String, ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True,
    )
    created_at: Mapped[datetime] = _ts_now()

    __table_args__ = (
        Index("ix_chatter_account_access_chatter", "chatter_id"),
    )


class ChatterFolderAccess(Base):
    """Per (chatter, account): which OF vault folders the chatter may see.
    If >=1 row exists for (chatter_id, account_id), the chatter's vault
    browser for that account is restricted to the listed folder ids. No
    rows ⇒ no restriction.

    `folder_id` is BigInteger to match vault_items.folder_id (OF folder ids
    exceed int32); NOT a FK — vault folders aren't a local table.
    `folder_name` is denormalised at write time so the chatter's
    authoritative folder menu (/chatter/me/folder-access, DA-3) can render
    names without re-paginating OF's vault/lists."""
    __tablename__ = "chatter_folder_access"

    chatter_id: Mapped[str] = mapped_column(
        String, ForeignKey("chatters.id", ondelete="CASCADE"), primary_key=True,
    )
    account_id: Mapped[str] = mapped_column(
        String, ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True,
    )
    folder_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    folder_name: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime] = _ts_now()

    __table_args__ = (
        Index("ix_chatter_folder_access_chatter", "chatter_id"),
    )
