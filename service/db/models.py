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

from datetime import date, datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
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
    type_coerce,
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


class AccountHealth(Base):
    """Per-account OF session health (see service/account_health.py).

    `session_dead_at` set ⇒ OF rejected the stored session ("Wrong user." —
    the creator got logged out / re-linked elsewhere). While set, the
    automation executor skips EVERY run for the account; a periodic
    `client.me()` probe (or a session re-capture) clears it. One row per
    account, created on first flag. Soft reference to accounts.id — no FK,
    matching `Account.proxy_label`: the accounts table is a mirror of the
    sessions dir and may lag it."""
    __tablename__ = "account_health"

    account_id: Mapped[str] = mapped_column(String, primary_key=True)
    session_dead_at: Mapped[datetime | None] = mapped_column(DateTime)
    session_dead_reason: Mapped[str | None] = mapped_column(String)
    last_probe_at: Mapped[datetime | None] = mapped_column(DateTime)


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
    automation pack reads is here so gen_info / followup / welcome_chatter_for_info all
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
    # DATED timeline, additive/deduped: JSON array [{date, event}] built by gen_info
    # from the real message timestamps. Read by NONE of the six recent_events readers,
    # so the flat string-array shape (and every reader) is left unchanged. NULL == none.
    recent_events_timeline: Mapped[str | None] = mapped_column(Text)
    self_description: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    likes_boobs: Mapped[bool] = mapped_column(Boolean, default=False)
    likes_ass: Mapped[bool] = mapped_column(Boolean, default=False)
    timezone: Mapped[str | None] = mapped_column(String)
    # ISO 639-1 language for THIS fan (per-fan override / detection seam). NULL means
    # "use the account default" (resolve_language). language_source is 'manual' when an
    # operator set it (manual always wins over any AI detection). Both nullable — the
    # per-fan UI + gen_info detection ship LATER; the columns exist now so resolve_language
    # and the PATCH guard have a coherent target (no second migration when per-fan lands).
    language: Mapped[str | None] = mapped_column(String)
    language_source: Mapped[str | None] = mapped_column(String)

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

    # ── welcome_chatter_for_info info-gathering: topics we've already asked (V1 QuestionsAsked)
    # JSON list of keys (age/location/hobbies/…); a topic here is never re-asked,
    # so the bot gathers progressively without nagging. NULL == "[]". ──────────
    questions_asked: Mapped[str | None] = mapped_column(Text)

    # ── Persona continuity (what WE told THIS fan) ───────────
    # TIER B — the per-fan claims ledger. What SHE told THIS fan about herself,
    # stored ONLY where it deviates from the account canon (persona_facts_json) or
    # covers something canon didn't. Most fans stay NULL; that is what keeps it
    # cheap. JSON list of {topic, claim, at}, latest-wins per topic, capped at 20
    # (see _common.merge_persona_claims).
    #
    # Why per-fan when canon exists: a claim already made cannot be retracted.
    # Once she has told a fan "estoy acá en Chile", correcting him to Argentina
    # mid-thread is WORSE than staying wrong — he experiences the correction as
    # the lie. Canon keeps new threads right; this keeps existing threads coherent.
    #
    # The three persona_*_claimed scalars below are the fast path for the topics
    # fans ask most; this column carries everything else. Both are already
    # serialized by fans.py and PATCH-editable, so the correction UI is free.
    persona_claims_json: Mapped[str | None] = mapped_column(Text)
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
    # THE OWED-CUSTOM LEDGER. Set to the moment the qualifying tip landed; NULL
    # means nothing is owed. This column IS the ledger — `_customs.is_owed` reads
    # nothing else.
    #
    # ⚠️ IT USED TO BE A " Custom" SUFFIX ON `custom_nickname`, AND THAT COULD NOT
    # WORK. `custom_nickname` has four other writers, and one of them —
    # `welcome_chatter_for_info._maybe_push_nickname`, reached from ai_chatter on EVERY tick —
    # rebuilds the name from structured facts via `names.build_structured_nickname`,
    # which knows nothing about the marker and therefore drops it. ai_chatter runs
    # every 60s; customs_watch every 900s. So the ledger was erased roughly a minute
    # after it was written, in our DB and on OnlyFans both, and prod confirms it:
    # 204 customs_watch runs, zero fans ever marked. Worse, the in-memory mutation
    # happens BEFORE `_build_messages` reads it, so `_customs.prompt_block` — the
    # "he has paid, stop selling" rule — never rendered once on the engine that
    # closes the sale.
    #
    # A column has no other writers. Nothing about a fan's name can touch it.
    customs_owed_at: Mapped[datetime | None] = mapped_column(DateTime)
    # When an owed CUSTOM was last resolved for this fan (delivered, or cleared
    # by an operator). `customs_watch` ignores tips at or before it.
    #
    # WITHOUT THIS the scanner had no "done" state at all: owed was derived from
    # (a qualifying tip in the window) AND (no marker) AND (no detected
    # delivery), so clearing the marker was indistinguishable from never having
    # marked it — and the next sweep, 15 minutes later, re-marked the same tip.
    # The operator's "Sent" click undid itself on a 72-hour loop.
    #
    # A timestamp rather than a seen-tips table: the fact being recorded is
    # "everything this fan owed up to here is settled", which is one moment, not
    # a set of rows. Nullable, so init_db's additive catch-up adds it in place.
    customs_cleared_at: Mapped[datetime | None] = mapped_column(DateTime)
    # When the gather-close PPV was last ATTEMPTED for this fan. THE RETRY CLOCK,
    # not a receipt: a refusal stamps it too, and that is the whole reason it
    # exists as a column. The graduation fires once and then skip-lists the fan,
    # so it could never storm — but `ai_chatter`'s payer-floor rescue re-evaluates
    # every ~30s tick for as long as he stays unanswered, and an attempt is a
    # vault read plus a claim LLM call. Unstamped, the 2026-08-23 refusal would
    # have replayed roughly 2,400 times before he wrote again.
    #
    # One writer (`pack_farewell.send_gather_close`) for the same reason
    # `customs_owed_at` above is a column rather than a suffix on a name someone
    # else rewrites. Nullable, so init_db's additive catch-up adds it in place —
    # a NOT-NULL column here would be silently skipped on boot (engine.py:179).
    gather_close_at: Mapped[datetime | None] = mapped_column(DateTime)

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

    # ── DEAD COLUMNS: the retired deep_convo drill's state machine ───
    # The engine was removed (2026-08); NOTHING reads or writes these any more.
    # They stay declared because dropping a SQLite column rewrites the whole
    # table, and table-level surgery on the live WAL DB has corrupted prod twice
    # (2026-07-22, 2026-08-09). Removing them is its own deploy with its own
    # backup — never fold it into a code change.
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
    # Which automation sent this outbound row (welcome_chatter_for_info, send_welcome,
    # followup, autoreply, send_mass_message, reply_mass_funnel,
    # nudge_online, mass_nudge, online_blast; old rows can carry retired
    # kinds). NULL = human send or a legacy
    # pre-0032 row. Distinct from sent_by_employee_id, which lumps every
    # automation under the single system "Automation" sentinel employee.
    automation_kind: Mapped[str | None] = mapped_column(String)

    raw_json: Mapped[str | None] = mapped_column(Text)
    # Qwen3-VL description of an inbound photo the FAN sent, cached at ingest by
    # webhook_dispatch.on_inbound_image (gated by tip_reward image_describe_enabled).
    # NULL = never described (no image, feature off, or a describe failure). The chat
    # engines read it back into history as "[he sent: …]" so the AI can rate /
    # react to it. Added 0052; nullable so init_db's ADD-COLUMN catch-up materializes
    # it on prod with no backfill. See inbound_describe.describe_inbound_message.
    image_desc: Mapped[str | None] = mapped_column(Text)
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


# ── Reading messages.created_at when the DATA is dirtier than the SCHEMA ─────
# The column is DateTime NOT NULL, but on 2026-07-22 exactly one row on one
# account held the empty string '' there (an ingest wrote a blank where OF gave
# no createdAt). SQLAlchemy converts a DateTime column while MATERIALISING the
# result, so that single cell raised `ValueError: Invalid isoformat string: ''`
# out of str_to_datetime and took the WHOLE result set down with it — and since
# ai_chatter._gather reads every message of an account in ONE query, one bad cell
# silenced that account's automation completely. Wrapping the consuming `for`
# loop in try/except cannot save it: the raise happens inside .execute()/.all(),
# before the first row is ever handed to the loop.
#
# So a reader that must survive dirty data selects `created_at_text()` instead of
# the mapped column. type_coerce swaps the PYTHON-side type only — the emitted
# SQL is byte-identical, so WHERE/ORDER BY on the real column still work — but the
# DateTime result processor never runs and the driver's raw value comes back
# untouched. `parse_ts()` then converts it, returning None for anything it cannot
# read so the caller can drop that ONE row (with a warning) instead of the account.
def created_at_text():
    """`Message.created_at` selected WITHOUT the DateTime parse (see above)."""
    return type_coerce(Message.created_at, String).label("created_at")


def parse_ts(value) -> datetime | None:
    """Best-effort DB timestamp → naive-UTC datetime; None when unusable.

    Tolerates what the DateTime processor refuses: '', whitespace, a stray 'Z',
    junk. Returns naive UTC because that is what the rest of the schema stores —
    an aware datetime leaking out of here would blow up the very arithmetic
    (`utcnow() - ts`) the callers do with it.
    """
    if value is None or isinstance(value, datetime):
        return value                      # None, or a driver that types it for us
    s = str(value).strip()
    if not s:
        return None                       # the '' cell that started all this
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt


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

    # ── Vault-AI mirror + describe layer (added 0046) ──────────────
    # All nullable so init_db's boot ADD-COLUMN catch-up applies them on a
    # create_all-built DB whose alembic_version lags (house convention).
    #
    # Cache/mirror bookkeeping. `search_text` is the denormalised, lower-cased
    # blob (tags + description + video_description + notes) we LIKE-scan for
    # instant LOCAL search once the vault is collected. `of_folder_ids` is a
    # JSON int array of the OF vault-list ids this item belongs to (filled by
    # the per-folder pass) so folder filtering is local too. `updated_at_of`
    # holds OF's own updatedAt when present; `last_seen_run_id` is bumped every
    # collect sweep so a vanished item can be soft-deleted after two clean runs.
    search_text: Mapped[str | None] = mapped_column(Text)
    # OF-search terms this item matched (harvested from OF's own vault search so
    # our local search is a superset of OF's). Folded into search_text too.
    of_terms: Mapped[str | None] = mapped_column(Text)
    of_folder_ids: Mapped[str | None] = mapped_column(Text)
    content_hash: Mapped[str | None] = mapped_column(String)
    content_hash_kind: Mapped[str | None] = mapped_column(String)
    updated_at_of: Mapped[str | None] = mapped_column(String)
    last_seen_run_id: Mapped[int | None] = mapped_column(BigInteger)
    removed_at: Mapped[datetime | None] = mapped_column(DateTime)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime)

    # Per-folder manual ordering. Signed: 0 = absolute first, 1,2,3… pinned
    # from the front, -1 = absolute last, -2,-3… from the back; NULL = normal
    # sort. Toggled per folder in the UI.
    manual_order: Mapped[int | None] = mapped_column(Integer)

    # AI describe output (Qwen3-VL). Effective value = locked override >
    # override > AI field > legacy column; operator edits win and lock.
    video_description: Mapped[str | None] = mapped_column(Text)
    explicitness_tier: Mapped[str | None] = mapped_column(String)
    story_suitable: Mapped[bool | None] = mapped_column(Boolean)
    # AI rank 0-100 pairing story_suitable (feat 6); NULL until vault_ai_service
    # scores it. Effective value still flows through operator override/lock.
    story_score: Mapped[int | None] = mapped_column(Integer)
    tip_vault_flag: Mapped[bool | None] = mapped_column(Boolean)
    # AI rank 0-100 pairing tip_vault_flag (feat 5); NULL until scored.
    tip_vault_score: Mapped[int | None] = mapped_column(Integer)
    suggested_caption: Mapped[str | None] = mapped_column(Text)
    suggested_script: Mapped[str | None] = mapped_column(Text)
    describe_status: Mapped[str | None] = mapped_column(String)
    describe_generated_at: Mapped[datetime | None] = mapped_column(DateTime)
    describe_model: Mapped[str | None] = mapped_column(String)
    describe_call_id: Mapped[int | None] = mapped_column(BigInteger)
    frames_sampled: Mapped[int | None] = mapped_column(Integer)
    ai_fields_json: Mapped[str | None] = mapped_column(Text)
    operator_overrides_json: Mapped[str | None] = mapped_column(Text)
    locked_fields_json: Mapped[str | None] = mapped_column(Text)
    review_state: Mapped[str | None] = mapped_column(String)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime)
    reviewed_by: Mapped[str | None] = mapped_column(String)

    # ── Script recovery (service/vault_scripts.py) ────────────────
    # A shoot is uploaded as one burst and escalates clothed→nude→masturbation,
    # but phone galleries hand the picker newest-first so the burst often lands
    # REVERSED. `script_id` is the batch (= its earliest media_id), `script_seq`
    # is 1..n in canonical escalating order, and `script_reversed` records that
    # we flipped upload order to get there. Sorting a folder is then ORDER BY
    # script_id, script_seq. `script_score` is the 0-100 ladder rung.
    script_id: Mapped[int | None] = mapped_column(BigInteger)
    script_seq: Mapped[int | None] = mapped_column(Integer)
    script_score: Mapped[int | None] = mapped_column(Integer)
    script_reversed: Mapped[bool | None] = mapped_column(Boolean)

    __table_args__ = (
        Index("ix_vault_account_created", "account_id", "created_at"),
        Index("ix_vault_account_send", "account_id", "send_count"),
        Index("ix_vault_account_seen", "account_id", "last_seen_run_id"),
        Index("ix_vault_account_describe", "account_id", "describe_status"),
        Index("ix_vault_account_script", "account_id", "script_id", "script_seq"),
    )


class VaultCacheRun(Base):
    """One "Collect all" sweep of the OF vault into the `vault_items` mirror.

    Drives the button's progress UI and gives soft-delete the "two clean
    sweeps agreed" guard (trap 4): only items whose `last_seen_run_id` lags
    two completed runs are candidates for removal, so a mid-upload page shift
    can't false-delete. `status` ∈ running|done|error|canceled.
    """
    __tablename__ = "vault_cache_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[str] = mapped_column(
        String, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(String, nullable=False, default="running")
    phase: Mapped[str | None] = mapped_column(String)
    total_seen: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    upserted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pages_done: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = _ts_now()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)

    __table_args__ = (Index("ix_vault_cache_runs_account", "account_id", "started_at"),)


class VaultFolder(Base):
    """An INTERNAL vault folder (our own, not OF's vault-list). Lets the
    operator organize the mirror — new folder, select media, add to folder,
    reorder — with zero OF writes (OF-mirror is a separate, later concern).
    `of_list_id` is NULL until/unless the folder is mirrored to OF.
    """
    __tablename__ = "vault_folders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[str] = mapped_column(
        String, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    of_list_id: Mapped[int | None] = mapped_column(BigInteger)
    created_by: Mapped[str] = mapped_column(String, nullable=False, default="operator")
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = _ts_now()

    __table_args__ = (Index("ix_vault_folders_account", "account_id", "deleted_at"),)


class VaultFolderItem(Base):
    """Membership of a media item in an INTERNAL vault folder, with a
    per-folder `manual_order` (0 = first, 1,2,3… from front, -1 = last,
    -2,-3… from back, NULL = normal order) so videos can be ordered inside a
    folder independently of the global vault order."""
    __tablename__ = "vault_folder_items"

    account_id: Mapped[str] = mapped_column(
        String, ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True
    )
    folder_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    media_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    manual_order: Mapped[int | None] = mapped_column(Integer)
    added_at: Mapped[datetime] = _ts_now()

    __table_args__ = (Index("ix_vault_folder_items_folder", "account_id", "folder_id"),)


class VaultOfQueryLog(Base):
    """Which OF vault-search queries we've already harvested (so we don't re-hit
    OF every keystroke). TTL'd; on a fresh hit the terms already live in each
    matched item's `of_terms`/`search_text`, so the local search is instant."""
    __tablename__ = "vault_of_query_log"

    account_id: Mapped[str] = mapped_column(
        String, ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True
    )
    query: Mapped[str] = mapped_column(String, primary_key=True)
    match_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    fetched_at: Mapped[datetime] = _ts_now()


class VaultAiReviewItem(Base):
    """A pending AI-proposed ACTION awaiting operator approval (suggest-only).

    ACTIONS ONLY — folder assignment, PPV draft, daily-reminder send. Media
    *descriptions* auto-apply and are NEVER queued here: routing the first-run
    hundreds of descriptions through review would be worse than manual
    (VAULT_AI_PLAN §0 / correction #4).

    `status` lifecycle: pending → approved (operator OK'd, but nothing has hit
    OF yet — approval ≠ mutation, correction #2) → applied (a consumer acted on
    it) | rejected. `baseline_json` snapshots the inputs the proposal was built
    from (media hash / overrides / folder / config) so a consumer can flag the
    approval `stale` if the world changed underneath it before it was applied.
    """
    __tablename__ = "vault_ai_review_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[str] = mapped_column(
        String, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String, nullable=False)  # folder|ppv|reminder
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    payload_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    baseline_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _ts_now()
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime)

    __table_args__ = (
        Index("ix_vault_ai_review_account_status", "account_id", "status"),
        Index("ix_vault_ai_review_account_kind", "account_id", "kind", "status"),
    )


class VaultDailyUsage(Base):
    """ACCOUNT-level daily image-rotation history for the daily-reminder card.

    Distinct from `VaultSend` (fan-scoped, tip_reward): this backs the GLOBAL
    "N unseen images/day" pool so the daily reminder never repeats a media
    across the account within the rotation window. Record ONLY after an approved
    send SUCCEEDS (select-then-record, mirroring pick_hot_teaser), so a
    failed/rejected proposal never burns a media out of the pool. Composite PK
    (account_id, media_id, sent_on) makes the record idempotent per day.
    """
    __tablename__ = "vault_daily_usage"

    account_id: Mapped[str] = mapped_column(
        String, ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True
    )
    media_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    sent_on: Mapped[date] = mapped_column(Date, primary_key=True)
    created_at: Mapped[datetime] = _ts_now()

    __table_args__ = (
        Index("ix_vault_daily_usage_account_day", "account_id", "sent_on"),
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
       regular            — mirrors an OF list
       exclude            — automatic skip in mass DM
       hidden             — drop from default inbox
       post_label         — labels on profile posts
       smart              — saved query (query_json)
       automation_include — the include-only automation audience mirror: a local
                            snapshot of ONE operator-picked OF folder
                            (of_list_id), reconciled OF→local by the
                            `audience_sync` automation. `list_members` under
                            this row is the gate-time read for every gated
                            engine (no OF round-trip at send time).
       autofence          — the per-account AUTOFENCE system list: ONE stable
                            OF list id holding (sendable universe − include
                            members), attached to enforce-mode broadcasts via
                            `excludedLists`. Membership lives OF-side; the row
                            pins the stable id + fence health in sync_meta_json."""
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
    # audience_sync runtime state (automation_include / autofence rows only).
    # Last COMPLETE successful OF→local reconcile; NULL = never synced, and the
    # audience resolver treats an enforce-mode account whose mirror has never
    # synced as still-in-shadow rather than darkening the roster.
    synced_at: Mapped[datetime | None] = mapped_column(DateTime)
    # Free-shape RUNTIME state (scan generations, baseline readiness, truncation,
    # fence health, kill-switch pause). Deliberately NOT in a validated config
    # blob: config validators are strict allowlists that silently drop unknown
    # keys, which is exactly the fail-open trap runtime state must not sit behind.
    sync_meta_json: Mapped[str | None] = mapped_column(Text)
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


class AudienceEnrollment(Base):
    """Stateful auto-add ledger for the include-only automation audience.

    One row per (account, fan) the roster-diff has ever judged. States:
      pending_add            — provably new; OF add issued, membership not yet
                               confirmed by a complete list crawl. Only THIS
                               state retries (bounded backoff via attempts /
                               next_retry_at).
      confirmed              — seen inside the OF folder after our add. From
                               here, later ABSENCE from the folder is operator
                               intent and is FINAL (→ removed_after_confirmed);
                               the machine never re-adds.
      removed_after_confirmed— operator removed the fan after we confirmed him.
                               Terminal. Never re-added, never retried.
      rejected_not_new       — cannot be proven new (pre-dates enable, returning
                               churner, incomplete baseline). Never silently
                               added; surfaced in the visible "outside the
                               fence" queue with one-click admit.
    """
    __tablename__ = "audience_enrollments"

    account_id: Mapped[str] = mapped_column(String, primary_key=True)
    fan_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    status: Mapped[str] = mapped_column(String, nullable=False)
    # Which roster-scan generation first saw this fan (baseline discipline: a
    # fan is provably new only when first seen AFTER a two-scan-stable baseline).
    first_seen_gen: Mapped[int | None] = mapped_column(Integer)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime)
    reason: Mapped[str | None] = mapped_column(String)
    first_seen_at: Mapped[datetime] = _ts_now()
    updated_at: Mapped[datetime] = _ts_now()

    __table_args__ = (
        Index("ix_audience_enroll_status", "account_id", "status"),
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
    # IANA zone ("America/New_York") for Human Rhythm's sleep window. DST-correct,
    # unlike utc_offset — which stays the fallback when this is NULL, so an account
    # that already set an offset doesn't have to re-answer. Neither set ⇒ rhythm
    # stays disabled rather than defaulting to UTC: a UTC default would put a US
    # creator to sleep through her peak earning window and nothing would explain
    # the revenue drop. See service/automations/rhythm.py:local_now.
    timezone: Mapped[str | None] = mapped_column(String)
    location: Mapped[str | None] = mapped_column(String)
    # STRUCTURED creator facts — the canon she must never contradict, pinned into
    # every chat prompt above the free-text persona. JSON dict, all keys optional.
    # The key set is owned by _common.PERSONA_FACT_FIELDS (a save-path allowlist,
    # so an unknown key is dropped rather than stored) and is driven by what fans
    # measurably ask: relationship, age, kids, location, name, family, job ...
    #
    # Why this exists as fields and not more prose: `persona` averaged 202-882
    # chars across live accounts while the style scaffolding around it ran ~5,000,
    # and the gaps were exactly what fans probe. the graded vault's persona said "Born and
    # raised in Argentina" with NO city — so when a fan asked where she grew up the
    # model improvised, and a 966-turn thread walked Argentina → Chile → Córdoba
    # before the fan said "ya no creo nada". A named empty slot is what the enrich
    # button fills; free prose has no slots to notice are missing.
    #
    # Empty/absent keys render NOTHING (byte-identical prompt), same discipline as
    # the clock and location blocks. Nullable, no server_default: init_db's
    # ADD-COLUMN catch-up materializes it on prod with no backfill.
    persona_facts_json: Mapped[str | None] = mapped_column(Text)
    # ISO 639-1 language this creator writes in (en/es/sl/…). NULL == "en". Gates BOTH
    # the output language of every conversational prompt AND which guard vocabulary
    # runs (English-only guards would mangle Spanish, and vice-versa — see
    # resolve_language + the Spanish guard layer). Nullable, no server_default: NULL is
    # treated as "en" in code, so init_db materializes it on prod with zero risk.
    language: Mapped[str | None] = mapped_column(String)
    # Whose voice this account writes in: NULL/'her' == a female creator (every
    # account that has ever run here), 'him' == the male lane. Same shape and same
    # reasoning as `language` directly above: nullable, no server_default, and
    # normalised in code (automations/_voice.norm_voice) rather than by a CHECK or
    # an enum — a corrupt value must degrade to the shipped lane, never raise on a
    # send path. NULL keeps every existing account byte-identical, which is the
    # whole safety argument for a second lane and is asserted in test_voice_lane.
    voice: Mapped[str | None] = mapped_column(String)
    # JSON dict {morning_1, morning_2, afternoon_1, afternoon_2, evening, night}
    time_activities_json: Mapped[str | None] = mapped_column(Text)
    # TODAY's generated day, overwritten each creator-local date (automations/_daylog):
    # {"date","weekday","beats":[{id,slot,text}],"covers":[{id,kind,text}]}. The six
    # slots above are its TONE SEED, not its content — read literally they are the
    # same day forever, which is a schedule rather than a life. NULL == no day log,
    # and every renderer returns "" for it, so an un-generated account produces a
    # byte-identical chat prompt. Only today is kept: nothing reads yesterday, and a
    # growing history in a config column is how grok_calls became 52% of the DB.
    day_log_json: Mapped[str | None] = mapped_column(Text)
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
    # "welcome_chatter_for_info":"grok-4-1-fast-non-reasoning"}. NULL → use `model` for all.
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
    # girl voice + 3-bubble splitting). JSON
    # {"welcome_chatter_for_info": bool, "autoreply": bool, "ai_chatter": bool}. Absent/NULL or
    # a missing key → the per-automation default (see _common._STYLE_DEFAULT_ON).
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
    # chatter that REPLACES welcome_chatter_for_info for fans under the spend gate. JSON
    # {enabled, mode: "backup"|"always", sla_minutes, max_lifetime_spend_cents,
    # max_offers_per_fan_per_day, min_fan_msgs_between_offers,
    # max_fans_per_tick, stall_ttl_hours,
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
    # Vault-AI actions layer: per-account config for describe cadence, tier→price
    # bands, folder taxonomy, and the daily-reminder card. JSON shape is frozen in
    # plans/VAULT_AI_ACTIONS_CONTRACT.md — {enabled, suggest_only, models{},
    # describe{cadence_hours, describe_all_cap_percent, ...}, pricing{bands by
    # explicitness_tier}, folders{taxonomy}, daily_reminder{folder, lines,
    # per_fan_cooldown_hours, ...}}. Absent/NULL → built-in defaults (DISABLED —
    # master off, suggest-only; nothing describes/sends until a creator enables
    # it). Own column to avoid the nudge/webhook shallow-merge collision.
    vault_ai_config_json: Mapped[str | None] = mapped_column(Text)
    # make_right (Resolution Agent): per-account config for the "detect a wrong-content
    # incident (headline: charged twice for the same content) and make it right"
    # safety net. Absent/NULL → make_right's built-in defaults (DISABLED + preview-only).
    # Own column to avoid the nudge/webhook shallow-merge collision.
    make_right_config_json: Mapped[str | None] = mapped_column(Text)
    # Include-only automation audience mode. Three COLUMNS, deliberately not keys
    # in a *_config_json blob: the mass senders never read those blobs, and every
    # blob writer is a strict allowlist whose one stale copy silently drops the
    # keys — a fail-open on an audience fence. NULL audience_mode ≡ 'off'
    # (default OFF; nullable so init_db's ADD-COLUMN catch-up lands it on prod
    # with no backfill). 'shadow' logs would-be skips and gates nothing;
    # 'enforce' gates. Shadow is mandatory before enforce (config API rule).
    audience_mode: Mapped[str | None] = mapped_column(String)
    # Local lists.id of the kind='automation_include' mirror row (NOT the OF
    # list id — that lives on the List row). Plain Integer, no FK: the additive
    # ALTER-ADD-COLUMN catch-up can't create constraints, and a fresh-box
    # create_all growing one the upgraded boxes lack would fork the schema.
    audience_list_id: Mapped[int | None] = mapped_column(Integer)
    # Auto-add newly subscribed fans to the include folder (roster-diff pull;
    # the mixed notifications feed is only a latency fast-path). NULL ≡ off.
    audience_auto_add: Mapped[bool | None] = mapped_column(Boolean)
    # "Block sensitive words" compliance filter: an operator-editable banned-word
    # list scanned against OUTBOUND text at the send chokepoint (AI + mass paths).
    # JSON {"words": [str, ...], "mode": "block"|"mask"}. "block" ABORTS a send that
    # trips a banned word (drop + log); "mask" replaces each hit with first-char +
    # asterisks and sends it. Whole-word + case-insensitive (an "ass" rule never trips
    # on "class"). Absent/NULL/empty words → OFF (nothing scanned, current behavior).
    # This is SEPARATE from apply_word_restriction (the fixed OF-restricted doubler)
    # and the promo-spam audience guard. Own column to avoid the shallow-merge collision.
    banned_words_config_json: Mapped[str | None] = mapped_column(Text)
    # Co-performer tagging (media_cotag) — the Brain's per-account overrides for
    # the global env knobs. Both nullable so init_db's ADD-COLUMN catch-up lands
    # them on prod with no backfill.
    #
    # NULL ≡ OFF: most creators are solo, and their videos really are just them.
    # True = the operator ticked "Always tag videos" on a COLLAB account: every
    # send attaching a vault VIDEO then carries the co-performer tag even when
    # the describe verdict reads it as solo — video describes are cut from
    # stills and miss the POV partner, which is how blake/blake clips shipped
    # untagged. The describe-verdict decision itself is never weakened by this.
    cotag_tag_videos: Mapped[bool | None] = mapped_column(Boolean)
    # The handle to tag, stored without the '@' (media_cotag folds it the way OF
    # stores usernames). NULL → OF_COTAG_USERNAME → the built-in 'jakabasej'.
    cotag_username: Mapped[str | None] = mapped_column(String)
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
    # Branching loop guard: how many times a `next` map has re-routed this fan.
    # A backward `next` can form a cycle; reply_mass_funnel halts the fan once
    # this passes ~20 hops (done + last_error='loop_guard') so a mis-authored
    # funnel can't spin forever. Nullable/additive (None == 0), self-heals via
    # ALTER TABLE like the columns above — a linear funnel never bumps it.
    hops: Mapped[int | None] = mapped_column(Integer)
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
    """LEGACY (retired 2026-08-19): an ordered 'sexting sequence' — a themed
    series of catalog items sold one piece at a time. Script ladders were
    removed (singles sold 18, scripts 0/21); nothing writes or reads these rows
    anymore. The table stays mapped because prod rows exist and deleting them
    would CASCADE into catalog_items → content_offers (offer history)."""
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
    bundle). SINGLES (`script_id IS NULL`) are the whole catalog since the
    2026-08-19 script-ladder removal; rows with `script_id` set are legacy data
    — never offered again, kept because their open offers must still deliver
    and deleting them cascades into content_offers.

    `description_for_ai` is the contract: what the fan actually SEES, 1-2
    sentences, present tense. The LLM may only tease/claim what's written here —
    it is the sole source for pitches and for answering "what will we do?"."""
    __tablename__ = "catalog_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[str] = mapped_column(
        String, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    # LEGACY pair (retired 2026-08-19 with the script ladders): both default
    # NULL, nothing writes anything else, and `position` has no reader. Kept
    # for the legacy script rows; see CATALOG_IS_SINGLE below.
    script_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("catalog_scripts.id", ondelete="CASCADE")
    )
    position: Mapped[int | None] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String, nullable=False, default="video")
    label: Mapped[str | None] = mapped_column(String)      # e.g. "BOOB TEASE"
    description_for_ai: Mapped[str | None] = mapped_column(Text)
    media_ids: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    # Free teaser frames attached unlocked on a PPV send (OF `previews`).
    preview_media_ids: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    duration_sec: Mapped[int | None] = mapped_column(Integer)
    # v2 §4.2 — snapshot of the DERIVED price band per media, frozen at first quote
    # and recomputed only from the human 1:1 PAID subset. Exists so the deflationary
    # spiral (seller ingesting its own asks) can be watched/frozen; NULL until quoted.
    band_lo: Mapped[int | None] = mapped_column(Integer)
    band_hi: Mapped[int | None] = mapped_column(Integer)
    # Unlock terms. PPV is the only lane (2026-08-19): price_cents > 0 makes an
    # item sellable; is_free_teaser items are sent free to build momentum
    # (initiation pics, item 1). tip_unlock_cents is LEGACY data — nothing
    # offers on it anymore; it survives for the pre-ruling rows.
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


# The one canonical singles predicate (2026-08-19 ruling): every catalog READ —
# the seller's shelf, the editor, the settings transfer, the gap report — scopes
# with this exact clause so legacy script rows never resurface. Writers need
# nothing: `script_id`/`position` default NULL, so a plain CatalogItem(...) is a
# single by construction.
CATALOG_IS_SINGLE = CatalogItem.script_id.is_(None)


class CatalogProgress(Base):
    """LEGACY (retired 2026-08-19 with the script ladders): per-(account, fan,
    script) position pin. Nothing writes or reads these rows anymore; the table
    stays mapped because prod rows exist."""
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
        # ⚠️ `ix_jobs_due` leads on `run_at`, and EVERY hot query here bounds
        # `run_at <= now` — an open range from the oldest row. So the planner
        # walks that index from the beginning of time and filters status /
        # account_id / kind row by row. That was fine while this table held a
        # dozen rows a day; on 2026-08-12 the engines came on and it started
        # taking ~20k/day. Measured 2026-08-25 at 263,790 rows (of which 316
        # were actually pending/running): the executor spent 6.7s of every 30s
        # tick scanning, and `/admin/automation-rules` paid 833ms of it per
        # load. These three make the same questions O(matching rows).
        #
        # `ix_jobs_due` is deliberately KEPT: init_db()'s catch-up can create a
        # missing index but never drops a stale one, so removing it here would
        # leave it on disk everywhere anyway — and an A/B showed that taking an
        # index away can flip the planner onto a worse one.
        #
        # _has_pending_job (automation_rules_api), _claim_due_job and
        # _materialize_due_rules' pending guard — all (account, kind, status)
        # with a run_at bound.
        Index("ix_jobs_account_kind_status_run",
              "account_id", "kind", "status", "run_at"),
        # _due_job_pairs — status-first, no account, once per supervisor tick.
        Index("ix_jobs_status_run", "status", "run_at"),
        # The cadence anchor (`max(created_at) WHERE rule_id = ?`) had NO index
        # to use at all — a full table scan per rule per tick, 1.4s across 85
        # enabled rules.
        Index("ix_jobs_rule_created", "rule_id", "created_at"),
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
        # `_last_run` and the max_runs counter both filter (account_id, kind).
        # `ix_runs_kind_started` answers those by walking the KIND partition and
        # discarding rows belonging to other accounts — cheap while every model
        # runs every kind, and 297ms for the one shape that matters most: a kind
        # this account has NEVER run, where it walks the partition to find
        # nothing. That is exactly what a freshly-created rule asks.
        Index("ix_runs_account_kind_started", "account_id", "kind", "started_at"),
        # `/admin/stats/automation-runs` — ORDER BY started_at DESC over the
        # principal's accounts. clamp_account_filter turns the card's
        # unqualified call into `account_id IN (<every account you own>)`, so the
        # filtered shape IS the user-facing one; unindexed it scanned all 409k
        # rows into a temp b-tree (1.0s) on a card that polls every 60s.
        #
        # ⚠️ This one must ship WITH `ix_runs_account_kind_started`, never
        # instead of it. Measured on a snapshot: alone, the planner prefers it
        # for `_last_run` too and then walks the whole account partition
        # filtering kind — 0.2ms → 156ms, and max_runs_count 285ms → 1,443ms,
        # i.e. FIVE TIMES WORSE than adding no index at all.
        Index("ix_runs_account_started", "account_id", "started_at"),
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
    cooldown), so the chat engines are never frozen by a nudge — it gates
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


class FollowPingState(Base):
    """Per-(account, fan) state for auto_follow's re-follow PING — unfollow +
    immediate re-follow of a quiet fan so OnlyFans fires a fresh "started
    following you" push. Dedicated table (same pattern as NudgeState) so the
    cooldown survives restarts: without it every run would re-ping the same
    quiet fans and the "notification" would read as harassment."""
    __tablename__ = "follow_ping_state"

    account_id: Mapped[str] = mapped_column(String, primary_key=True)
    fan_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    last_pinged_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    ping_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


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
    on its own cap), so the chat engines are never frozen by it.
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


class QuotaAudit(Base):
    """Item 21c — the daily-quota decision ledger. One row per (account, fan, UTC day,
    outcome), counter upserted, so a shadow week answers "would we have held him, how
    often, and WHY not" without re-deriving anything from message history.

    Tall on purpose: `reason` is a value, not a column, so a new outcome needs no
    migration and every question is one GROUP BY. Row count is bounded by
    fans x days x outcomes, and one fan can only produce a handful of outcomes a day.

    `n` counts EVALUATIONS, not replies — the gate runs once per candidate per tick, so
    a fan sitting in a 72h backoff accrues many `held` evaluations while receiving no
    messages at all. Read it as "how much of her attention this fan was asking for".
    The remaining columns are a LAST-SEEN snapshot (overwritten each upsert), there to
    make a row readable on its own without joining back to the thread.

    Written whether or not `daily_quota_enforce` is on: in shadow mode `enforced` is
    False and the reply still went out, which is exactly the counterfactual the ledger
    exists to record. Prune with the other audit tables."""
    __tablename__ = "quota_audit"

    account_id: Mapped[str] = mapped_column(String, primary_key=True)
    fan_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    day: Mapped[str] = mapped_column(String, primary_key=True)      # 'YYYY-MM-DD' UTC
    # held | runway | under_quota | uncapped | signal_lift | backoff_served | no_ladder
    reason: Mapped[str] = mapped_column(String, primary_key=True)
    n: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Was the verdict SERVED, or only recorded? False = shadow mode.
    #
    # PART OF THE KEY, not a snapshot column. The rollout is "shadow for a week, then
    # flip `daily_quota_enforce`" — and the flip happens mid-day. As a plain column it
    # was last-write-wins over an ACCUMULATING counter, so the first enforced sweep
    # absorbed that morning's shadow evaluations into the same row and relabelled all
    # of them enforced. The one day the distinction matters most is the one day it was
    # destroyed. In the key, the two regimes are simply different rows.
    enforced: Mapped[bool] = mapped_column(Boolean, primary_key=True, default=False)
    # Last-seen snapshot of the numbers behind the verdict.
    quota: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    runway_left: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    wait_h: Mapped[float | None] = mapped_column(Float)
    dry_h: Mapped[float | None] = mapped_column(Float)
    updated_at: Mapped[datetime] = _ts_now()

    __table_args__ = (
        Index("ix_quota_audit_day", "account_id", "day", "reason"),
    )


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


class UserLlmKey(Base):
    """One agency's LLM provider key. THE tenancy boundary for model spend.

    An owner (`users` row) is an agency; the OF accounts linked to it via
    `user_accounts` are its creators. Every LLM call that creator makes bills
    the key stored here, so two agencies can never spend on one credential —
    the invariant this table exists for. One row per (owner, provider); an
    agency that only uses DeepSeek simply has no deepinfra/grok row.

    `provider` holds a key of `llm_providers.PROVIDERS` ("deepseek" | "deepinfra" |
    "grok"). It is validated on the WRITE path against that registry rather
    than by a CHECK constraint, and the read path only ever queries a name that
    came out of the same registry — so a renamed provider can't silently make a
    stored key unreachable (which would fail over to the house key, i.e. to the
    deployment owner's bill).

    Plaintext, like `proxies.json` passwords and `secrets.json` — the same
    known limitation, tracked with them. `ON DELETE CASCADE` is what makes
    offboarding an agency actually destroy its credentials; `PRAGMA
    foreign_keys=ON` is wired at connect (db/engine.py) so it really fires.
    """
    __tablename__ = "user_llm_keys"

    user_id: Mapped[str] = mapped_column(
        String, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    provider: Mapped[str] = mapped_column(String, primary_key=True)
    api_key: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = _ts_now()


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


# ── Human Rhythm (0043) + Offer Engine (0044) ────────────────────────────────
# Both ship DISABLED. With the flags off nothing below is ever written, and the
# reply delay stays exactly typing_delay_seconds() (see service/automations/rhythm.py).

class RhythmState(Base):
    """Per (account, fan) reply-pacing state for the Human Rhythm sampler.

    `wake_at` is the scheduler seam: a decided delay longer than INLINE_MAX_S is
    NOT slept through (that would hold the fan lease and starve the executor's
    global run slots — see MEMORY relay_threadpool_starvation_500s). Instead the
    lease is released, a job is enqueued for `wake_at`, and the resume run sends
    inline. `deferrals` hard-caps that hop at 1 so a re-rolled sample can never
    livelock a fan into never being answered."""
    __tablename__ = "rhythm_state"

    account_id: Mapped[str] = mapped_column(
        String, ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True
    )
    fan_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    context: Mapped[str] = mapped_column(String, nullable=False, default="engaged")
    wake_at: Mapped[datetime | None] = mapped_column(DateTime)
    deferrals: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_cover_at: Mapped[datetime | None] = mapped_column(DateTime)
    # v2 §3.4: rolling last ~20 realized turns as JSON [{d,b,i}] (realized delay,
    # bubble count, informal flag) — recorded at the SEND site, fed back into the
    # next RhythmCtx (recent_realized_s / his_last_latency_s). Subsumes v1's
    # recent_delays_json + the two style-share accumulators (one column, not three).
    recent_turns_json: Mapped[str | None] = mapped_column(Text)
    # Ghost cycle (`_ghost.window`): when THIS fan's repeating chat/dark cycle
    # started. Deliberately the only ghost state there is — the stage is derived by
    # modulo off this one timestamp, so there is no pointer for a missed tick to
    # leave stale. Set from his first inbound (so the roster is staggered by its own
    # history instead of going dark on one calendar day), and pushed to `now` when
    # the cycle restarts: a live sale, or a thread she was not present in anyway.
    ghost_anchor: Mapped[datetime | None] = mapped_column(DateTime)
    # Step-out (rhythm.STEPOUT_MIN_S): she goes out for an hour or two every 7-15
    # exchanges. `stepout_mark` is her whole-thread reply count at the LAST step-out
    # and `stepout_target` is the interval drawn for the NEXT one, so "is she due" is
    # `total_out_n - stepout_mark >= stepout_target` — a comparison against a count
    # the thread itself carries, not a counter some tick has to remember to increment.
    # Same reasoning as `ghost_anchor` above: a tick that does not run can leave a
    # pointer stale, and it cannot leave a MARK stale.
    stepout_mark: Mapped[int | None] = mapped_column(Integer)
    stepout_target: Mapped[int | None] = mapped_column(Integer)
    updated_at: Mapped[datetime] = _ts_now()

    __table_args__ = (Index("ix_rhythm_state_wake_at", "wake_at"),)


class LadderState(Base):
    """Per (account, fan) selling state for the Offer Engine.

    status: idle → open (a rung is out) → hot (he PAID; strike-while-hot window)
            → tapped (bare "no" / two unpaid rungs) | stopped (hard stop).

    `offers_paused_until` is the SOFT-decline ("i'm broke") brake. It is scoped to
    this engine on purpose: fans.automation_paused_until is shared across EVERY
    automation (automation_executor.py), so writing a decline into it would blank
    the fan out of welcome/followup/mass too — a cross-automation blackout with no
    UI. A poverty plea stops selling, never talking."""
    __tablename__ = "ladder_state"

    account_id: Mapped[str] = mapped_column(
        String, ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True
    )
    fan_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default="idle")
    rung_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_ask_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_paid_at: Mapped[datetime | None] = mapped_column(DateTime)
    session_idle_at: Mapped[datetime | None] = mapped_column(DateTime)
    offers_paused_until: Mapped[datetime | None] = mapped_column(DateTime)
    daily_ask_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    daily_day: Mapped[str | None] = mapped_column(String)   # creator-local YYYY-MM-DD
    hot_until: Mapped[datetime | None] = mapped_column(DateTime)
    unpaid_rungs: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # v2 safe-state machine (spec §5/§6/§10.2). All default-inert — with the lane
    # flags off none of these is ever written. NEVER routed into
    # fans.automation_paused_until (that column is shared across every automation).
    objection_at: Mapped[datetime | None] = mapped_column(DateTime)   # last VOICED price objection (the discount "beat" clock, §5.1)
    discount_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # cuts already given on the CURRENT media (one-per-item, §5.1)
    bot_accused_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # §6.4 — 2nd strike ends selling
    companion_until: Mapped[datetime | None] = mapped_column(DateTime)  # §6.3 seller OFF / conversation ON
    cooldown_until: Mapped[datetime | None] = mapped_column(DateTime)   # §6.2 post-multibuy ease-off (talk only)
    updated_at: Mapped[datetime] = _ts_now()

    __table_args__ = (Index("ix_ladder_state_status", "account_id", "status"),)


class LadderQuote(Base):
    """One row per price we QUOTED — the conversion log and the experiment's
    instrument. `pre_clamp_cents` + `clamped_by` exist so a clamped quote can be
    excluded from the price analysis: silently truncating an unknown subset of
    quotes at the library ceiling would bias every estimate the arm produces.

    `media_key` (sorted media-id hash) — not catalog_item_id — because a fan who
    bought media through a MASS blast has no content_offers row at all, and a
    catalog-keyed ownership check would happily re-sell him what he already owns."""
    __tablename__ = "ladder_quote"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[str] = mapped_column(
        String, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    fan_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    rung_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    media_key: Mapped[str] = mapped_column(String, nullable=False)
    item_id: Mapped[int | None] = mapped_column(Integer)
    band_lo: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    band_hi: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    base_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    arm_mult: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    price_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pre_clamp_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    clamped_by: Mapped[str | None] = mapped_column(String)  # library|history|cold_ceiling|band|none
    kind: Mapped[str] = mapped_column(String, nullable=False, default="rung")  # rung|discount
    message_id: Mapped[int | None] = mapped_column(BigInteger)
    sent_at: Mapped[datetime] = _ts_now()
    paid: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime)

    __table_args__ = (
        Index("ix_ladder_quote_fan", "account_id", "fan_id"),
        Index("ix_ladder_quote_media", "account_id", "fan_id", "media_key"),
    )


class PendingOffer(Base):
    """An offer the gate BLOCKED (fan went quiet / low-information reply), parked
    until his next qualifying inbound. A gate that can only DELETE sends cannot
    beat its own revenue metric — the human corpus never sends cold either, but it
    does eventually send. One open row per fan; TTL'd."""
    __tablename__ = "pending_offer"

    account_id: Mapped[str] = mapped_column(
        String, ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True
    )
    fan_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    media_key: Mapped[str | None] = mapped_column(String)
    item_id: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = _ts_now()
    expires_at: Mapped[datetime | None] = mapped_column(DateTime)


class ResolutionLog(Base):
    """make_right (Resolution Agent) ledger — one row per detected wrong-content
    incident (headline: a fan charged twice for the same content). `incident_key`
    is the idempotency key (unique per account); the "up to twice per fan" cap is a
    COUNT of status='resolved' rows for the fan. Schema mirrors the
    20260721_0000_resolution_log migration exactly."""
    __tablename__ = "resolution_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[str] = mapped_column(
        String, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    fan_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    incident_key: Mapped[str] = mapped_column(String, nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    detected_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_now, server_default=text("CURRENT_TIMESTAMP")
    )
    status: Mapped[str] = mapped_column(
        String, nullable=False, server_default=text("'detected'"), default="detected"
    )
    remediation_json: Mapped[str | None] = mapped_column(Text)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime)

    __table_args__ = (
        UniqueConstraint("account_id", "incident_key", name="uq_resolution_incident"),
        Index("ix_resolution_fan", "account_id", "fan_id", "status"),
    )


class TranslationCache(Base):
    """Durable store behind POST /admin/translate — one row per (target, source text).

    The 🌐 toggle used to hold its translations in exactly two places, and BOTH
    were volatile: a module-level Map in the browser tab (gone on every reload or
    "↗ pop out") and an 8k-entry dict in the relay process (gone on every deploy).
    So the same Spanish thread was re-translated against Google's free gtx
    endpoint over and over — measured from the VPS, 40 uncached bubbles cost
    ~1.5s of wall clock before the first English word appears, and that bill was
    paid again on the next page load.

    Keyed by the TEXT, not by message id, which is the whole reason this pays for
    itself: over 30 days of prod chat, 193,471 rendered texts collapse to 86,949
    distinct ones — a 55% hit rate before a single fan repeats himself, because
    her side is largely scripted. A per-message column would translate "hola"
    thousands of times over.

    Server-side rather than localStorage for the same reason `vault_response_cache`
    is: it is shared. The first chatter to open a thread warms it for the whole
    team, in every tab, permanently — and it keeps the origin's contended ~5MB
    localStorage budget for the react-query snapshot.

    `text_hash` (md5 of target + NUL + text) is the primary key, so the source
    text is stored for pruning/debugging only and never looked up by value.
    """
    __tablename__ = "translation_cache"

    text_hash: Mapped[str] = mapped_column(String, primary_key=True)
    target: Mapped[str] = mapped_column(String, nullable=False)
    # ISO 639-1 as DETECTED by gtx ("es", "en", …) or "und". The UI tags bubbles
    # with it and renders same-language ones untouched, so it is part of the
    # cached answer, not metadata.
    src_lang: Mapped[str] = mapped_column(String, nullable=False, default="und")
    source_text: Mapped[str] = mapped_column(Text, nullable=False)
    translated: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = _ts_now()

    __table_args__ = (
        # Backs the age-ordered prune (see translate_api._prune_locked). Without
        # it the cap sweep is a full scan of the largest table nobody reads.
        Index("ix_translation_cache_created", "created_at"),
    )


# ── Growth surfaces (gaps-lane: Infloww-gap features) ────────────────
# Five net-new tables backing the /growth page + the auto_follow automation.
# create_all makes these on boot (no alembic); every one is additive — no
# existing table is touched. See GAP_FEATURES_SPEC.md.

class SmartList(Base):
    """A saved dynamic fan segment. `rules_json` is a small DSL over per-fan
    facts (lifetime_spend / recent_spend(days) / last_active_days / status /
    tag) resolved by smart_lists_api.resolve() into a fan-id set. Consumed as a
    named audience in the mass composer (include/exclude). Internal — never
    round-tripped to OnlyFans."""
    __tablename__ = "smart_lists"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[str] = mapped_column(
        String, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    # JSON: {"match": "all"|"any", "rules": [{"field","op","value"}, ...]}
    rules_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = _ts_now()
    updated_at: Mapped[datetime] = _ts_now()

    __table_args__ = (
        Index("ix_smart_lists_account", "account_id"),
        UniqueConstraint("account_id", "name", name="uq_smart_lists_account_name"),
    )


class TrialLinkMirror(Base):
    """Light local mirror of an OnlyFans free-trial link (source of truth is OF
    /trials). We store the create params + last-seen usage so the Growth table
    renders without a live round-trip and can label a link the operator named.
    `of_trial_id` links back to the OF entry; NULL until a create round-trips."""
    __tablename__ = "trial_link_mirrors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[str] = mapped_column(
        String, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    of_trial_id: Mapped[int | None] = mapped_column(BigInteger)
    name: Mapped[str] = mapped_column(String, nullable=False)
    subscribe_days: Mapped[int] = mapped_column(Integer, nullable=False, default=7)
    subscribe_counts: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    url: Mapped[str | None] = mapped_column(Text)
    # Last-seen usage snapshot from OF (claimCounts) — refreshed on list().
    claim_counts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = _ts_now()

    __table_args__ = (
        Index("ix_trial_mirror_account", "account_id", "of_trial_id"),
    )


class TrackingLink(Base):
    """Internal UTM / tracking link. A `slug` (globally unique) is served at the
    public GET /t/{slug}, which records one TrackingClick row and 302s to
    `target_url`. Analytics = COUNT(clicks) over time (subs/$ attribution is a
    TODO — no reliable click→subscriber join exists yet)."""
    __tablename__ = "tracking_links"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[str] = mapped_column(
        String, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    slug: Mapped[str] = mapped_column(String, nullable=False)
    target_url: Mapped[str] = mapped_column(Text, nullable=False)
    # Denormalized click counter (kept in sync on each capture) so the list
    # renders without an aggregate per row; TrackingClick holds the detail.
    click_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = _ts_now()

    __table_args__ = (
        UniqueConstraint("slug", name="uq_tracking_links_slug"),
        Index("ix_tracking_links_account", "account_id"),
    )


class TrackingClick(Base):
    """One row per hit on GET /t/{slug}. Kept thin — timestamp + a little
    provenance for the analytics readout (clicks over time / by referer)."""
    __tablename__ = "tracking_clicks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tracking_link_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("tracking_links.id", ondelete="CASCADE"), nullable=False
    )
    clicked_at: Mapped[datetime] = _ts_now()
    referer: Mapped[str | None] = mapped_column(Text)
    user_agent: Mapped[str | None] = mapped_column(Text)
    ip: Mapped[str | None] = mapped_column(String)

    __table_args__ = (
        Index("ix_tracking_clicks_link_time", "tracking_link_id", "clicked_at"),
    )


class PromoCampaign(Base):
    """Light local mirror of an OnlyFans subscription promo campaign (source of
    truth is OF /promotions). Stores the create params + last-seen performance
    so the Growth list renders and the operator can name/track a campaign.
    `of_promo_id` links back to the OF entry."""
    __tablename__ = "promo_campaigns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[str] = mapped_column(
        String, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    of_promo_id: Mapped[int | None] = mapped_column(BigInteger)
    name: Mapped[str] = mapped_column(String, nullable=False)
    # OF promos are a PERCENT discount off the sub (1–100). `discount_percent` is
    # the create-time value (OF does not echo it on list, so the mirror is the
    # only record). price_cents is kept for schema stability but is 0 for OF promos.
    discount_percent: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    price_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    subscribe_counts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    subscribe_days: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    message: Mapped[str | None] = mapped_column(Text)
    promo_type: Mapped[str] = mapped_column(String, nullable=False, default="all")
    status: Mapped[str] = mapped_column(String, nullable=False, default="active")
    # Last-seen performance snapshot from OF — claimsCount (people who claimed).
    subscriber_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # ── Auto-reactivate (promo_reactivate automation) ────────────────
    # Opt-in per campaign: when OF marks the promo finished (time or claims run
    # out), the automation re-creates it with the SAME discount/caps/message and
    # repoints of_promo_id at the new one. reactivate_count is the cycle counter
    # (also the cap check); last_reactivated_at drives the cooldown.
    auto_reactivate: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    reactivate_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_reactivated_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = _ts_now()

    __table_args__ = (
        Index("ix_promo_campaigns_account", "account_id", "of_promo_id"),
    )
