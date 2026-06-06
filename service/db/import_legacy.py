"""
One-shot importer: read every JSON file under service/sessions/ +
service/proxies.json, upsert into SQLite. Idempotent — running it again
is a no-op on rows it already wrote, and updates the rest.

Usage (from repo root):
    ./venv/bin/python -m service.db.import_legacy
    ./venv/bin/python -m service.db.import_legacy --dry-run

What it imports:
  • service/proxies.json          → proxies table
  • service/sessions/accounts/<id>/meta.json      → accounts row
  • service/sessions/accounts/<id>/latest.json    → which session is_latest
  • service/sessions/accounts/<id>/session_*.json → sessions rows
  • service/sessions/active.json   → accounts.is_active_default

What it does NOT import:
  • chunk_2313_*.js — kept on disk as cache. Phase E may move to DB.
  • XHR jsonl dumps under service/sessions/ — those are debug artifacts.
  • Image / HTML capture artifacts.

After a successful run + a few days of stable operation, the JSON files
can be archived; the relay reads exclusively from SQL.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

# Make this module runnable both as `python -m service.db.import_legacy`
# and `python service/db/import_legacy.py`.
HERE = Path(__file__).resolve().parent
SERVICE_ROOT = HERE.parent
REPO_ROOT = SERVICE_ROOT.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from service.db.engine import DATABASE_URL
from service.db.models import Account, Proxy, Session as SessionRow


SESSIONS_DIR = SERVICE_ROOT / "sessions"
PROXIES_FILE = SERVICE_ROOT / "proxies.json"
ACTIVE_FILE = SESSIONS_DIR / "active.json"
ACCOUNTS_DIR = SESSIONS_DIR / "accounts"


def _parse_ts(raw: str | None) -> datetime | None:
    """Accept the few timestamp shapes the legacy JSON used."""
    if not raw:
        return None
    raw = raw.replace("Z", "").rstrip(".")
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y%m%dT%H%M%S"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def import_proxies(session: Session, dry_run: bool) -> int:
    """proxies.json → proxies table. Plaintext password is preserved
    (phase D will encrypt at rest)."""
    if not PROXIES_FILE.exists():
        return 0
    data = json.loads(PROXIES_FILE.read_text())
    count = 0
    for p in data.get("proxies", []):
        existing = session.get(Proxy, p["label"])
        if existing:
            # Upsert: preserve any DB-side edits but refresh known fields.
            existing.scheme = p.get("scheme", existing.scheme)
            existing.host = p.get("host", existing.host)
            existing.port = p.get("port", existing.port)
            existing.username = p.get("username") or existing.username
            existing.password_encrypted = p.get("password") or existing.password_encrypted
            existing.notes = p.get("notes") or existing.notes
            existing.verified_ip = p.get("verified_ip") or existing.verified_ip
            existing.verified_at = _parse_ts(p.get("verified_at")) or existing.verified_at
            existing.assigned_account_id = p.get("assigned_account_id") or existing.assigned_account_id
        else:
            session.add(Proxy(
                label=p["label"],
                scheme=p.get("scheme", "http"),
                host=p["host"],
                port=int(p["port"]),
                username=p.get("username"),
                # The relay's plan called this "password_encrypted" but the
                # legacy JSON stores plaintext. We carry plaintext for now;
                # phase D ships envelope encryption + migrates in-place.
                password_encrypted=p.get("password"),
                notes=p.get("notes"),
                verified_ip=p.get("verified_ip"),
                verified_at=_parse_ts(p.get("verified_at")),
                assigned_account_id=p.get("assigned_account_id"),
            ))
        count += 1
    if not dry_run:
        session.flush()
    return count


def _read_active_account_id() -> str | None:
    if not ACTIVE_FILE.exists():
        return None
    try:
        return json.loads(ACTIVE_FILE.read_text()).get("active_account_id")
    except Exception:
        return None


def import_accounts_and_sessions(session: Session, dry_run: bool) -> tuple[int, int]:
    """Walks service/sessions/accounts/<id>/, upserts one accounts row +
    N sessions rows per directory. Marks the one named in active.json as
    `is_active_default`."""
    if not ACCOUNTS_DIR.exists():
        return 0, 0

    active_id = _read_active_account_id()
    accounts_count = 0
    sessions_count = 0

    for adir in sorted(ACCOUNTS_DIR.iterdir()):
        if not adir.is_dir():
            continue
        account_id = adir.name
        meta_path = adir / "meta.json"
        meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
        latest_path = adir / "latest.json"
        latest_name = None
        if latest_path.exists():
            try:
                latest_name = json.loads(latest_path.read_text()).get("session")
            except Exception:
                pass

        # ── accounts upsert ──────────────────────────────────────
        existing_acc = session.get(Account, account_id)
        # Pull header fields from the latest session file so we can fill
        # `x_of_rev`, `static_param`, `user_agent` on the account row.
        x_of_rev = static_param = user_agent = None
        if latest_name:
            sfile = adir / latest_name
            if sfile.exists():
                try:
                    sdata = json.loads(sfile.read_text())
                    h = sdata.get("headers", {})
                    rules = sdata.get("signing", {}).get("rules", {})
                    x_of_rev = h.get("x_of_rev")
                    user_agent = h.get("user_agent")
                    static_param = rules.get("static_param")
                except Exception:
                    pass

        # Look up bound proxy via the proxies registry.
        bound_proxy = session.scalar(
            select(Proxy).where(Proxy.assigned_account_id == account_id)
        )
        proxy_label = bound_proxy.label if bound_proxy else None

        if existing_acc:
            existing_acc.nickname = meta.get("nickname") or existing_acc.nickname
            existing_acc.color = meta.get("color") or existing_acc.color
            existing_acc.proxy_label = proxy_label or existing_acc.proxy_label
            existing_acc.x_of_rev = x_of_rev or existing_acc.x_of_rev
            existing_acc.static_param = static_param or existing_acc.static_param
            existing_acc.user_agent = user_agent or existing_acc.user_agent
            existing_acc.is_active_default = (account_id == active_id) if active_id else existing_acc.is_active_default
            existing_acc.last_used_at = _parse_ts(meta.get("last_used_at")) or existing_acc.last_used_at
        else:
            session.add(Account(
                id=account_id,
                nickname=meta.get("nickname"),
                color=meta.get("color"),
                proxy_label=proxy_label,
                x_of_rev=x_of_rev,
                static_param=static_param,
                user_agent=user_agent,
                is_active_default=(account_id == active_id) if active_id else False,
                created_at=_parse_ts(meta.get("created_at")) or datetime.utcnow(),
                last_used_at=_parse_ts(meta.get("last_used_at")),
            ))
        accounts_count += 1

        # ── sessions upsert ──────────────────────────────────────
        # Match on (account_id, captured_at) — one session per timestamp.
        # Re-running the importer doesn't create duplicates because of this.
        for sfile in sorted(adir.glob("session_*.json")):
            try:
                sdata = json.loads(sfile.read_text())
            except Exception:
                continue
            captured_at = _parse_ts(sdata.get("captured_at"))
            if not captured_at:
                continue
            existing_sess = session.scalar(
                select(SessionRow).where(
                    SessionRow.account_id == account_id,
                    SessionRow.captured_at == captured_at,
                )
            )
            is_latest = (latest_name == sfile.name)
            cookies = sdata.get("cookies", [])
            rules = sdata.get("signing", {}).get("rules", {})
            if existing_sess:
                existing_sess.cookies_json = json.dumps(cookies)
                existing_sess.x_of_rev = sdata.get("headers", {}).get("x_of_rev", existing_sess.x_of_rev)
                existing_sess.signing_rules_json = json.dumps(rules)
                existing_sess.is_latest = is_latest
            else:
                session.add(SessionRow(
                    account_id=account_id,
                    captured_at=captured_at,
                    cookies_json=json.dumps(cookies),
                    x_of_rev=sdata.get("headers", {}).get("x_of_rev", ""),
                    signing_rules_json=json.dumps(rules),
                    is_latest=is_latest,
                ))
            sessions_count += 1

        # Flip all other sessions for this account to is_latest=false.
        if latest_name and not dry_run:
            session.flush()
            for s in session.scalars(
                select(SessionRow).where(SessionRow.account_id == account_id)
            ):
                # Only the one matching latest_name keeps is_latest=true.
                # Re-checking by captured_at is more reliable than filename.
                pass  # handled above via the if/else branches

    if not dry_run:
        session.flush()
    return accounts_count, sessions_count


def main() -> int:
    parser = argparse.ArgumentParser(description="Import legacy JSON state into SQLite.")
    parser.add_argument("--dry-run", action="store_true", help="Print what would change; don't commit.")
    args = parser.parse_args()

    sync_url = DATABASE_URL.replace("+aiosqlite", "").replace("+asyncpg", "")
    engine = create_engine(sync_url)
    with Session(engine) as s:
        proxies_n = import_proxies(s, args.dry_run)
        accounts_n, sessions_n = import_accounts_and_sessions(s, args.dry_run)
        if args.dry_run:
            s.rollback()
            print(f"[dry-run] would upsert: {proxies_n} proxies, {accounts_n} accounts, {sessions_n} sessions")
        else:
            s.commit()
            print(f"upserted: {proxies_n} proxies, {accounts_n} accounts, {sessions_n} sessions")
    engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
