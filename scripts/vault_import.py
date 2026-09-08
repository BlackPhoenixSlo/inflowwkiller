#!/usr/bin/env python3
"""Bulk-import media into an OnlyFans vault, from local files or Google Drive.

    # local files (globs are your shell's job)
    ./venv/bin/python scripts/vault_import.py --account <id> ~/media/*.jpg

    # a public Drive folder, straight into a vault folder
    ./venv/bin/python scripts/vault_import.py --account <id> \
        --drive 'https://drive.google.com/drive/folders/<id>' --list-id 12345

    # see what would run, touching nothing
    ./venv/bin/python scripts/vault_import.py --account <id> --drive <link> --dry-run

    # clean up after a crash (deletes orphaned carrier posts)
    ./venv/bin/python scripts/vault_import.py --account <id> --sweep

HOW IT WORKS, AND WHY IT LOOKS LIKE THIS. OnlyFans has no "save to vault"
endpoint, so each new file is attached to a scheduled post 30 days out, its
vault id is read off that post, and the post is deleted — typically within a
couple of seconds, and never visible to a fan. Files already in the vault are
recognised by their S3 ETag and skipped without uploading a byte.

The INTENT to create a carrier is written to disk before the post exists, so a
crash is recoverable even when the create call's response was lost: `--sweep`
finds anything left behind — by id when one was learned, and otherwise by
matching the carrier marker against OnlyFans' own scheduled-post list. Run it if
this script ever dies mid-import; an undeleted carrier publishes on its date.

In Docker, run it inside the relay container so it sees the session:
    cat scripts/vault_import.py | docker exec -i chatterly-relay python3 - --account <id> --sweep

That is the same process tree as a running relay, so an import started here and
one started from the dashboard would be two importers on one account. They are
excluded by a pid-bearing lock file per account (vault_upload._acquire_lock);
the second one exits rather than fighting for the write window.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

SERVICE = Path(__file__).resolve().parent.parent / "service"
if str(SERVICE) not in sys.path:
    sys.path.insert(0, str(SERVICE))


# One glyph per item status. A status this map has never heard of prints "?"
# rather than being silently uncounted — `_check_marks` below is what makes that
# a promise instead of a hope, by comparing the map against the module that
# actually defines the vocabulary.
_MARKS = {"done": "✓", "failed": "✗", "skipped": "–",
          "pending": "·", "uploading": "↑"}


def _check_marks(statuses) -> None:
    missing = [s for s in statuses if s not in _MARKS]
    if missing:
        print(f"note: no glyph for item status {', '.join(missing)} — "
              f"they will print as '?'")


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("files", nargs="*", help="local media files to import")
    p.add_argument("--account", required=True, help="Fastt account id")
    p.add_argument("--drive", action="append", default=[],
                   help="public Google Drive file or folder link (repeatable)")
    p.add_argument("--list-id", type=int, default=None,
                   help="vault folder to file everything into when done")
    p.add_argument("--max-files", type=int, default=None,
                   help="override the per-batch file cap")
    p.add_argument("--max-mb", type=int, default=None,
                   help="override the per-file size cap, in MB")
    p.add_argument("--dry-run", action="store_true",
                   help="resolve and report the work; upload nothing")
    p.add_argument("--sweep", action="store_true",
                   help="delete carrier posts orphaned by an earlier crash, then exit")
    args = p.parse_args(argv)

    import client_pool
    import vault_upload
    _check_marks(vault_upload.ITEM_STATUSES)

    if args.sweep:
        res = vault_upload.sweep(lambda aid: client_pool.get(aid))
        print(f"carriers deleted: {res['carriers_deleted']}   "
              f"still live: {res['carriers_failed']}   dirs removed: {res['dirs_removed']}")
        if res["carriers_failed"]:
            print("Some carriers could NOT be deleted — check your scheduled posts "
                  "in the OnlyFans UI. They publish on their scheduled date.")
            return 1
        return 0

    # Every policy in one object, defaults and all — the CLI does not restate
    # them, and an override reaches every check that reads it.
    base = vault_upload.Limits()
    limits = vault_upload.Limits(
        max_files=args.max_files or base.max_files,
        max_bytes=(args.max_mb * 1024 * 1024) if args.max_mb else base.max_bytes,
    ).checked()
    max_files, max_bytes = limits.max_files, limits.max_bytes

    missing = [f for f in args.files if not Path(f).is_file()]
    if missing:
        print(f"not a file: {missing[0]}")
        return 1
    if not args.files and not args.drive:
        print("nothing to do — pass local files, --drive links, or --sweep")
        return 1

    if args.dry_run:
        total = 0
        for f in args.files:
            n = Path(f).stat().st_size
            total += n
            flag = "  SKIP (over cap)" if n > max_bytes else ""
            print(f"  local   {Path(f).name:40} {n / 1e6:8.1f} MB{flag}")
        if args.drive:
            import gdrive
            for d in gdrive.resolve(args.drive, max_files=vault_upload.resolve_budget(limits)):
                ok, why = gdrive.is_uploadable(d)
                size = d.size or 0
                total += size if ok else 0
                print(f"  drive   {d.name:40} {size / 1e6:8.1f} MB"
                      f"{'' if ok else '  SKIP (' + why + ')'}")
        print(f"\n  ~{total / 1e6:.0f} MB total, cap {max_files} files / "
              f"{max_bytes / 1e6:.0f} MB each. Nothing was uploaded.")
        return 0

    client = client_pool.get(args.account)
    print(f"account={args.account} of_user_id={client.user_id}")
    state = vault_upload.start(
        args.account, client=client, local_paths=list(args.files),
        drive_links=list(args.drive), list_id=args.list_id, limits=limits)

    items = state.get("items") or []
    print(f"\nrun {state['run_id']} — {state['status']}")
    for i in items:
        mark = _MARKS.get(i["status"], "?")
        extra = "  (already in vault)" if i.get("deduped") else ""
        err = f"  {i['error']}" if i.get("error") else ""
        print(f"  {mark} {i['name'][:44]:44} {str(i.get('vault_id') or ''):>11}{extra}{err}")

    done = sum(1 for i in items if i["status"] == "done")
    failed = sum(1 for i in items if i["status"] == "failed")
    # The same roll-up the dashboard shows, from the same function — an intent
    # with no id counts too, and that is the carrier whose create response was
    # lost, the one only `--sweep` can find. This used to be a fourth
    # hand-rolled definition of it.
    undeletable, unrecorded = vault_upload.carriers_outstanding(args.account, [state])
    print(f"\n{done} in vault, {failed} failed, "
          f"{sum(1 for i in items if i['status'] == 'skipped')} skipped")
    if state.get("filed_into_list"):
        print(f"filed {state['filed_into_list']} into vault folder {state['list_id']}")
    if undeletable or unrecorded:
        print(f"\n⚠ {undeletable + unrecorded} CARRIER POST(S) STILL LIVE — run "
              f"--sweep now, or delete them in the OnlyFans UI. They publish on "
              f"their scheduled date.")
        return 1
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
