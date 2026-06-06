"""One-shot data backfill scripts.

Each module here is a standalone script that reshapes existing rows ahead of
a schema migration. They're idempotent (re-runnable) and report the number of
rows touched so the operator can confirm before moving on to the destructive
DDL step.

Conventional run:  `python -m db.backfills.<name>` from the `service/` dir.
"""
