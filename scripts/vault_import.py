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

Every carrier post id is written to disk BEFORE the post exists, so a crash is
recoverable: `--sweep` finds and deletes anything left behind. Run it if this
script ever dies mid-import — an undeleted carrier publishes on its date.

In Docker, run it inside the relay container so it sees the session:
    cat scripts/vault_import.py | docker exec -i chatterly-relay python3 - --account <id> --sweep
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

SERVICE = Path(__file__).resolve().parent.parent / "service"
if str(SERVICE) not in sys.path:
    sys.path.insert(0, str(SERVICE))


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

    if args.sweep:
        res = vault_upload.sweep(lambda aid: client_pool.get(aid))
        print(f"carriers deleted: {res['carriers_deleted']}   "
              f"still live: {res['carriers_failed']}   dirs removed: {res['dirs_removed']}")
        if res["carriers_failed"]:
            print("Some carriers could NOT be deleted — check your scheduled posts "
                  "in the OnlyFans UI. They publish on their scheduled date.")
            return 1
        return 0

    max_files = args.max_files or vault_upload.MAX_FILES
    max_bytes = (args.max_mb * 1024 * 1024) if args.max_mb else vault_upload.MAX_BYTES

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
            for d in gdrive.resolve(args.drive, max_files=max_files * 2):
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
        drive_links=list(args.drive), list_id=args.list_id,
        max_files=max_files, max_bytes=max_bytes)

    items = state.get("items") or []
    print(f"\nrun {state['run_id']} — {state['status']}")
    for i in items:
        mark = {"done": "✓", "failed": "✗", "skipped": "–"}.get(i["status"], "?")
        extra = "  (already in vault)" if i.get("deduped") else ""
        err = f"  {i['error']}" if i.get("error") else ""
        print(f"  {mark} {i['name'][:44]:44} {str(i.get('vault_id') or ''):>11}{extra}{err}")

    done = sum(1 for i in items if i["status"] == "done")
    failed = sum(1 for i in items if i["status"] == "failed")
    live = [i for i in items if i.get("carrier_post_id") and not i.get("carrier_deleted")]
    print(f"\n{done} in vault, {failed} failed, "
          f"{sum(1 for i in items if i['status'] == 'skipped')} skipped")
    if state.get("filed_into_list"):
        print(f"filed {state['filed_into_list']} into vault folder {state['list_id']}")
    if live:
        print(f"\n⚠ {len(live)} CARRIER POST(S) STILL LIVE — run --sweep now, or "
              f"delete them in the OnlyFans UI. They publish on their scheduled date.")
        return 1
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
