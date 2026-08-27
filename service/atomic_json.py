"""service/atomic_json.py — write one small JSON file completely, or not at all.

WHY THIS EXISTS. Two modules keep small JSON files that are read at IMPORT and
must never be found half-written: the concurrency store (`lane_config`) and the
restart ledger (`restart_budget`). A torn file in either is not a lost setting,
it is a process that boots on the wrong numbers — `lane_config._load` answers a
corrupt store by falling back to code defaults, and a ledger that fails to parse
reads as "no restarts yet", which is the one answer a rate limit must never give
by accident.

The recipe is the boring one, and the reasons are the point:

  • the temp file goes in the SAME DIRECTORY as the target, because `os.replace`
    is only atomic within one filesystem — a temp in `/tmp` would degrade to a
    copy across a mount, which is exactly the torn write being prevented;
  • `fsync` BEFORE the rename, so the bytes are on the disk and not merely in
    the page cache at the moment the rename publishes them;
  • `fsync` the PARENT DIRECTORY after the rename, because fsyncing the file
    only helps against a host crash — and in that same crash an un-fsynced
    directory can lose the rename itself, leaving the old file or none. Doing
    the first without the second is half the durability with all of the cost;
    `settings_transfer_api.py:647` already got this right and is where the
    trick is borrowed from. Best-effort: a filesystem that refuses to open a
    directory must not fail a write that already landed;
  • `unlink` on any failure, so a write that dies leaves no debris beside the
    file it failed to replace.

⚠️ TWO OTHER COPIES of this exact recipe live in the relay and are deliberately
NOT migrated here — this module exists so that adding a restart ledger did not
add a fourth, not as a half-finished sweep:

  • `secrets_store.py:328` — same shape, missing the fsync;
  • `server.py:1583` (`_lane_stats_flush`) — uses a FIXED `.tmp` name instead of
    `mkstemp`, so two concurrent writers collide. Migrating it would fix that.

Three near-misses that look like copies and are not, listed so the next sweep
does not "unify" them into a regression:
`vault_stills.py:197` and `server.py:2142` write BYTES, not JSON, and can never
call this; `settings_transfer_api.py:646` does strictly MORE (it also
`chmod`s 0o600), so folding it in needs a `mode=` argument first.

A LEAF: imports nothing from the relay, because both its callers run at module
import, long before the event loop or the DB exists.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def write_atomic(target: Path, data: Any) -> None:
    """Serialise `data` to `target` as JSON, atomically.

    Raises `OSError` when the write cannot be completed. Callers decide what
    that means, and the two current ones decide opposite things on purpose: a
    tuning value that cannot be saved falls back to its default (fail open),
    while a restart that cannot be recorded is not granted (fail closed).
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    # The temp carries the target's own name so a leftover is traceable to the
    # write that died, and so two files sharing a directory cannot be confused
    # for each other's debris.
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix="." + target.name + ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    # After the rename, and outside the failure path: the data is already
    # published, so a directory that will not fsync is a durability shortfall,
    # not a failed write. Raising here would tell a caller its write was lost
    # when it was not — and `restart_budget` treats that as "refuse the
    # restart".
    try:
        dir_fd = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass
