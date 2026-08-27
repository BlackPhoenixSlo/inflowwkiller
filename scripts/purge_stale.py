#!/usr/bin/env python3
"""Purge stale models (accounts) and stale users from chatterly.db.

DRY RUN unless --apply. Prints the exact row counts it would delete.

Why this exists: deleting a model in the UI removes only the on-disk account
FOLDER (accounts.delete_account -> shutil.rmtree). The DB sync that follows
(db/repo.sync_from_disk) re-runs the legacy IMPORTER, which only upserts --
it never deletes. So every model ever deleted still has its accounts row,
its is_latest=1 sessions row, its user_accounts link, and its whole data
tail. Same story for users: nothing ever expires a `users` row, the 30-day
rule only rejects the COOKIE.

The trap this script exists to avoid:
  Tables carrying account_id fall into FOUR groups, not one:
    CASCADE   (42)  follow the parent row automatically.
    SET NULL  (1)   `proxies` -- unbound, kept. Correct: a proxy outlives
                    the model that used it.
    NO ACTION (2)   `mass_runs`, `funnel_account_media` -- these ABORT the
                    delete with "FOREIGN KEY constraint failed".
    no FK     (25)  `messages`, `grok_calls`, `automation_runs`,
                    `fan_profiles`, `actions`, ... -- silently STRANDED,
                    now attributable to nothing.
  So the two middle-and-last groups are deleted FIRST, by account_id, and
  only then the parent row.

All four groups are read off the live schema via PRAGMA on every run --
never hardcoded -- so a new table cannot silently escape into group four.

Usage (local):
    python3 scripts/purge_stale.py --db service/chatterly.db --models-test-fixtures --users-test-fixtures
    python3 scripts/purge_stale.py --db service/chatterly.db --models-test-fixtures --apply --i-have-a-backup

Usage (prod -- MUST run in-container; a host-side rw open on the live WAL
db is what corrupted prod on 2026-07-22):
    cat scripts/purge_stale.py | ssh root@YOUR_VPS_IP \
        'docker exec -i fastt-relay python3 - --db /app/service/chatterly.db <flags>'

This script never VACUUMs. Reclaiming the freed pages is a separate,
deliberate decision -- see scripts/prod-vacuum-disarm.sh and the 08-09
incident before you consider it.
"""
from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

GROUPS = ("orphan", "blocks", "cascade", "setnull")

TEST_ACCOUNT_RE = re.compile(r"^(acc_ai_\d+|acc[A-Z])$")
TEST_USER_RE = re.compile(r"^admin_[0-9a-f]{8}$")


def fk_groups(conn: sqlite3.Connection, parent: str, orphan_col: str | None = None) -> dict:
    """Group every table referencing `parent` by what a parent DELETE does to
    it. Read off the live schema via PRAGMA -- never a hardcoded list, because
    the whole failure mode here is a table nobody remembered.

    Each entry is (table, column, notnull). Keys:
      cascade  -- follows the parent automatically
      setnull  -- column blanked automatically, row kept
      blocks   -- NO ACTION / RESTRICT: ABORTS the delete unless handled first
      orphan   -- carries `orphan_col` but declares NO foreign key: silently
                  survives the delete, unattributable to anything
    """
    groups: dict[str, list[tuple[str, str, int]]] = {k: [] for k in GROUPS}
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    )]
    for t in tables:
        cols = {r[1]: r[3] for r in conn.execute(f'PRAGMA table_info("{t}")')}
        # Key off the FK's OWN column, never off a column NAME: proxies points
        # at accounts through `assigned_account_id`, and a name-based scan
        # skipped it entirely.
        fks = [(r[3], r[6]) for r in conn.execute(f'PRAGMA foreign_key_list("{t}")')
               if r[2] == parent]
        if not fks:
            if orphan_col and orphan_col in cols and t != parent:
                groups["orphan"].append((t, orphan_col, cols[orphan_col]))
            continue
        for col, action in fks:
            entry = (t, col, cols.get(col, 0))
            if action == "CASCADE":
                groups["cascade"].append(entry)
            elif action == "SET NULL":
                groups["setnull"].append(entry)
            else:                  # NO ACTION / RESTRICT -- aborts the delete
                groups["blocks"].append(entry)
    return {k: sorted(v) for k, v in groups.items()}


def neutralize(conn, groups, ids):
    """Clear everything that would abort or survive a parent DELETE.

    The `blocks` group splits on NULLABILITY, which is exactly the signal for
    what the row means:
      nullable  -> the column is an ATTRIBUTION stamp (who started this mass
                   run). Blank it; the row is somebody else's data and must
                   outlive the parent.
      NOT NULL  -> the row is OWNED by the parent and cannot exist without it.
                   Delete it.
    `orphan` rows have no FK at all and are always deleted -- left behind they
    are attributable to nothing and no query can ever find them again.
    """
    ph = ",".join("?" * len(ids))
    done = []
    for t, col, notnull in groups["blocks"]:
        if notnull:
            conn.execute(f"DELETE FROM {t} WHERE {col} IN ({ph})", ids)
            done.append(("deleted", t, col))
        else:
            conn.execute(f"UPDATE {t} SET {col}=NULL WHERE {col} IN ({ph})", ids)
            done.append(("blanked", t, col))
    for t, col, _nn in groups["orphan"]:
        conn.execute(f"DELETE FROM {t} WHERE {col} IN ({ph})", ids)
        done.append(("deleted", t, col))
    return done


def registry_ids(sessions_dir: str) -> set[str]:
    """The on-disk account registry -- the UI's source of truth for
    'which models exist'. A DB-only account is precisely one missing here."""
    root = os.path.join(sessions_dir, "accounts")
    if not os.path.isdir(root):
        raise SystemExit(f"no account registry at {root!r} -- pass --models explicitly")
    return {d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d))}


def pick_models(conn: sqlite3.Connection, args) -> list[str]:
    all_ids = [r[0] for r in conn.execute("SELECT id FROM accounts")]
    picked: set[str] = set()
    if args.models:
        unknown = set(args.models) - set(all_ids)
        if unknown:
            raise SystemExit(f"not in accounts: {sorted(unknown)}")
        picked |= set(args.models)
    if args.models_test_fixtures:
        picked |= {i for i in all_ids if TEST_ACCOUNT_RE.match(i)}
    if args.models_not_in_registry:
        picked |= set(all_ids) - registry_ids(args.sessions_dir)
    return sorted(picked - set(args.keep_models))


def pick_users(conn: sqlite3.Connection, args) -> list[str]:
    rows = conn.execute(
        "SELECT id, username, last_seen_at, is_admin FROM users"
    ).fetchall()
    picked: set[str] = set()
    if args.users:
        by_name = {u.lower(): i for i, u, _, _ in rows}
        for want in args.users:
            uid = by_name.get(want.lower(), want)
            if uid not in {r[0] for r in rows}:
                raise SystemExit(f"not in users: {want!r}")
            picked.add(uid)
    if args.users_test_fixtures:
        picked |= {i for i, u, _, _ in rows if TEST_USER_RE.match(u or "")}
    if args.users_idle_days is not None:
        cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=args.users_idle_days)
        for uid, _u, seen, _a in rows:
            if not seen:
                continue
            try:
                ts = datetime.fromisoformat(str(seen))
            except ValueError:
                continue
            if ts < cutoff:
                picked.add(uid)
    keep = {k.lower() for k in args.keep_users}
    keep_ids = {i for i, u, _, _ in rows if i.lower() in keep or (u or "").lower() in keep}
    return sorted(picked - keep_ids)


def pick_chatters(conn, args):
    """Chatters to remove. `--chatters-dead` means "resolves to no models":
    a chatter sees the UNION of its linked owners' accounts, so once those
    owners or their accounts are gone the login still works and shows an
    empty picker. That is the shape worth deleting, not idleness alone.
    """
    rows = conn.execute("SELECT id, username FROM chatters").fetchall()
    by_name = {u.lower(): i for i, u in rows}
    ids = {i for i, _ in rows}
    picked = set()
    for want in args.chatters:
        cid = by_name.get(want.lower(), want)
        if cid not in ids:
            raise SystemExit(f"not in chatters: {want!r}")
        picked.add(cid)
    if args.chatters_dead:
        for cid, _u in rows:
            n = conn.execute(
                """SELECT COUNT(DISTINCT ua.account_id) FROM chatter_users cu
                   JOIN user_accounts ua ON ua.user_id = cu.user_id
                   WHERE cu.chatter_id = ?""", (cid,)).fetchone()[0]
            if n == 0:
                picked.add(cid)
    keep = {k.lower() for k in args.keep_chatters}
    return sorted(picked - {i for i, u in rows if i.lower() in keep or u.lower() in keep})


def count_rows(conn, groups, ids):
    """Row counts per group, keyed `table.column`. Pure data.

    Keyed by column as well as table because one parent can be referenced
    twice from the same table (`messages` stamps both `sent_by_employee_id`
    and `unsent_by_employee_id`), and because the same table can appear
    under two different parents.
    """
    if not ids:
        return {k: {} for k in GROUPS}
    ph = ",".join("?" * len(ids))
    out = {k: {} for k in GROUPS}
    for bucket in GROUPS:
        for t, col, _nn in groups[bucket]:
            n = conn.execute(
                f"SELECT COUNT(*) FROM {t} WHERE {col} IN ({ph})", ids
            ).fetchone()[0]
            if n:
                out[bucket][f"{t}.{col}"] = n
    return out


def merge_counts(*counts):
    """Combine per-parent count maps into one report."""
    return {k: {kk: vv for c in counts for kk, vv in c[k].items()} for k in GROUPS}


def report(title, ids, labels, counts):
    print(f"\n=== {title}: {len(ids)} ===")
    for i in ids:
        print(f"  {i}  {labels.get(i, '')}")
    if not ids:
        return 0
    total = 0
    for bucket, note in (
        ("orphan", "deleted FIRST -- no FK, would otherwise be STRANDED"),
        ("blocks", "deleted FIRST -- NO ACTION FK, would otherwise ABORT the delete"),
        ("cascade", "removed automatically by ON DELETE CASCADE"),
        ("setnull", "column set to NULL, rows KEPT"),
    ):
        rows = counts[bucket]
        if not rows:
            continue
        print(f"\n  -- {bucket} tables ({note}) --")
        for t, n in sorted(rows.items(), key=lambda kv: -kv[1]):
            print(f"     {t:<40} {n:>9,}")
            if bucket != "setnull":
                total += n
    return total


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", default="service/chatterly.db")
    p.add_argument("--sessions-dir", default="service/sessions",
                   help="holds accounts/<id>/ -- the on-disk model registry")
    p.add_argument("--models", nargs="*", default=[], metavar="ID")
    p.add_argument("--models-test-fixtures", action="store_true",
                   help="acc_ai_N / accA-style rows left by the test suites")
    p.add_argument("--models-not-in-registry", action="store_true",
                   help="in the accounts table but with no folder on disk")
    p.add_argument("--keep-models", nargs="*", default=[], metavar="ID")
    p.add_argument("--users", nargs="*", default=[], metavar="ID_OR_NAME")
    p.add_argument("--users-test-fixtures", action="store_true",
                   help="admin_<8hex> rows left by the auth suites")
    p.add_argument("--users-idle-days", type=int, metavar="N",
                   help="last_seen_at older than N days")
    p.add_argument("--keep-users", nargs="*", default=[], metavar="ID_OR_NAME")
    p.add_argument("--chatters", nargs="*", default=[], metavar="ID_OR_NAME")
    p.add_argument("--chatters-dead", action="store_true",
                   help="chatters whose owners grant them ZERO models")
    p.add_argument("--keep-chatters", nargs="*", default=[], metavar="ID_OR_NAME")
    p.add_argument("--apply", action="store_true", help="actually delete")
    p.add_argument("--i-have-a-backup", action="store_true",
                   help="required with --apply")
    args = p.parse_args()

    if args.apply and not args.i_have_a_backup:
        print("refusing: --apply needs --i-have-a-backup.\n"
              "  snapshot first, e.g. in-container:\n"
              "    docker exec fastt-relay python3 -c \"import sqlite3;\\\n"
              "      s=sqlite3.connect('/app/service/chatterly.db');\\\n"
              "      d=sqlite3.connect('/app/service/chatterly.db.bak');\\\n"
              "      s.backup(d)\"", file=sys.stderr)
        return 2

    uri = f"file:{args.db}?mode={'rw' if args.apply else 'ro'}"
    conn = sqlite3.connect(uri, uri=True)
    conn.execute("PRAGMA foreign_keys=ON")   # OFF by default -- cascades are inert without it

    groups = fk_groups(conn, "accounts", orphan_col="account_id")
    print(f"db: {args.db}")
    print("schema: " + ", ".join(
        f"{len(groups[k])} {k}" for k in ("cascade", "setnull", "blocks", "orphan")
    ) + "  (blocks + orphan need an explicit DELETE)")

    models = pick_models(conn, args)
    users = pick_users(conn, args)

    m_labels = {i: (n or "") for i, n in
                conn.execute("SELECT id, nickname FROM accounts")}
    u_labels = {i: f"{n}  last_seen={str(s)[:10]}  admin={a}" for i, n, s, a in
                conn.execute("SELECT id, username, last_seen_at, is_admin FROM users")}

    u_groups = fk_groups(conn, "users", orphan_col="user_id")
    # `employees` is a parent in its own right (mass_runs stamps who started a
    # run), so its own referents are counted and cleared alongside the users'.
    e_groups = fk_groups(conn, "employees")
    emp_ids = []
    if users:
        ph = ",".join("?" * len(users))
        emp_ids = [r[0] for r in conn.execute(
            f"SELECT id FROM employees WHERE user_id IN ({ph})", users)]

    # A chatter owns mirror `employees` rows through `chatter_id`, which
    # carries NO foreign key — so those strand unless discovery finds them,
    # and the employees they point at are themselves stamped on mass_runs.
    # Three levels, cleared innermost-first.
    c_groups = fk_groups(conn, "chatters", orphan_col="chatter_id")
    chatters = pick_chatters(conn, args)
    chatter_emp_ids = []
    if chatters:
        cph = ",".join("?" * len(chatters))
        chatter_emp_ids = [r[0] for r in conn.execute(
            f"SELECT id FROM employees WHERE chatter_id IN ({cph})", chatters)]

    m_counts = count_rows(conn, groups, models)
    u_counts = merge_counts(count_rows(conn, u_groups, users),
                            count_rows(conn, e_groups, emp_ids))
    c_counts = merge_counts(count_rows(conn, c_groups, chatters),
                            count_rows(conn, e_groups, chatter_emp_ids))

    c_labels = {i: f"{u}  last_seen={str(ls)[:10]}" for i, u, ls in
                conn.execute("SELECT id, username, last_seen_at FROM chatters")}

    total = report("MODELS to purge", models, m_labels, m_counts)
    total += report("USERS to purge", users, u_labels, u_counts)
    total += report("CHATTERS to purge", chatters, c_labels, c_counts)

    if not models and not users and not chatters:
        print("\nnothing selected -- pass a selection flag (see --help)")
        return 0
    print(f"\ntotal rows affected: {total:,}")

    if not args.apply:
        print("\nDRY RUN -- nothing written. Re-run with --apply --i-have-a-backup")
        return 0

    with conn:
        if models:
            for action, t, col in neutralize(conn, groups, models):
                print(f"  {action} {t}.{col}")
            ph = ",".join("?" * len(models))
            conn.execute(f"DELETE FROM accounts WHERE id IN ({ph})", models)
        if users:
            # employees is a parent in its own right: `mass_runs` stamps who
            # started a run with NO ACTION, so that reference has to be
            # blanked before any employee row can go.
            if emp_ids:
                for action, t, col in neutralize(conn, e_groups, emp_ids):
                    print(f"  {action} {t}.{col}")
                eph = ",".join("?" * len(emp_ids))
                conn.execute(f"DELETE FROM employees WHERE id IN ({eph})", emp_ids)
            for action, t, col in neutralize(conn, u_groups, users):
                print(f"  {action} {t}.{col}")
            ph = ",".join("?" * len(users))
            conn.execute(f"DELETE FROM users WHERE id IN ({ph})", users)
        if chatters:
            if chatter_emp_ids:
                for action, t, col in neutralize(conn, e_groups, chatter_emp_ids):
                    print(f"  {action} {t}.{col}")
            for action, t, col in neutralize(conn, c_groups, chatters):
                print(f"  {action} {t}.{col}")
            cph = ",".join("?" * len(chatters))
            conn.execute(f"DELETE FROM chatters WHERE id IN ({cph})", chatters)
    print(f"\nAPPLIED. Deleted {len(models)} models, {len(users)} users, "
          f"{len(chatters)} chatters.")
    print("Note: freed pages are NOT returned to the filesystem -- no VACUUM here on purpose.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
