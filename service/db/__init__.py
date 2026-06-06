"""
Database layer for the Fastt relay.

This package owns the SQL persistence side of the relay — everything that
was a JSON file under service/sessions/ + service/proxies.json + the
account_registry meta gets folded into a SQLite DB (DATABASE_URL points
at it via service/.env).

Layout:
    engine.py        Async SQLAlchemy engine + session factory + init_db().
    models.py        SQLModel/SQLAlchemy table definitions — the schema
                     from ARCHITECTURE_PLAN_V2.md §4.
    migrations/      Alembic migration scripts (versioned).
    import_legacy.py One-shot importer that ingests JSON files into SQL.

Phase A wires the engine + creates tables; nothing reads them yet. Phase B
plumbs message ingestion through. Phase E reads the AI tables.

Why SQLModel: it's a thin layer on top of SQLAlchemy 2.x that gives us
Pydantic-compatible typed rows for free — every model is both a SQLA
table and a Pydantic schema we can serialize to FastAPI responses
without duplicating the field list. Falls back to plain SQLAlchemy when
the SQLModel sugar gets in the way (composite PKs, partial indexes).
"""

from .engine import (  # noqa: F401
    engine,
    async_session_factory,
    get_session,
    init_db,
    DATABASE_URL,
)
