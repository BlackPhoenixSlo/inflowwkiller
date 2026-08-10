#!/usr/bin/env python3
"""Emit the in-container program that seeds the feet rungs on prod.

Companion to `prod-feet-folders.sh` — kept as its own file because macOS bash
3.2 mis-parses a heredoc containing `)` inside `$( )`.

Parsing happens HERE, on the laptop, so a malformed seed file fails before
anything reaches prod, and the counts each rung heading claims are asserted.

🚨 The media ids live only in `.scratch/…/feet-folders.md` and are read at
runtime. They are never written into a tracked file: `.scratch/` is excluded
from deploy-fastt.sh's Flow A, and a bare 4[0-9]{9} media id must never enter
a file that ships.

Usage: _feet_folders_program.py <seed_file> <1|0 apply>
"""
from __future__ import annotations

import json
import re
import sys


def parse(seed_file: str) -> dict[str, dict[str, list[int]]]:
    txt = open(seed_file).read()
    plan: dict[str, dict[str, list[int]]] = {}
    for part in txt.split("## "):
        m = re.match(r"(\d{6,}) —", part)
        if not m:
            continue
        account_id = m.group(1)
        plan[account_id] = {}
        for rung, claimed, ids in re.findall(
            r"\*\*`feet-([a-z-]+)` \((\d+)\)\*\*\n```\n(.*?)\n```", part, re.S
        ):
            media_ids = [int(x) for x in re.findall(r"\d+", ids)]
            if len(media_ids) != int(claimed) or len(set(media_ids)) != len(media_ids):
                sys.exit(
                    f"seed file corrupt: {account_id} feet-{rung} claims {claimed}, "
                    f"parsed {len(media_ids)} ({len(set(media_ids))} distinct)"
                )
            plan[account_id][rung] = media_ids
    if not plan:
        sys.exit("seed file parsed to nothing — check the heading format")
    return plan


PROGRAM = '''import asyncio

from sqlalchemy import select

import vault_pack_picker
from db.engine import get_session
from db.models import VaultFolder, VaultFolderItem, VaultItem

APPLY = {apply!r}
PROBE = {probe!r}
PLAN = {plan}


async def main():
    cat = vault_pack_picker.category("feet")
    if PROBE:
        # Read-only live test of the DEPLOYED picker against real prod data:
        # the same call the modal makes when the operator opens it.
        for account_id in PLAN:
            got = await vault_pack_picker.candidates(account_id, cat)
            print("%s candidates=%-4s filed=%-4s rungs=%s"
                  % (account_id, len(got["candidates"]), got["filed"], got["rungs"]))
            for f in got["folders"]:
                print("    %-16s folder_id=%-6s count=%-4s of_list_id=%s"
                      % (f["name"], f["folder_id"], f["count"], f["of_list_id"]))
            sample = got["candidates"][0] if got["candidates"] else None
            if sample:
                print("    sample media_id=%s kind=%s rung=%s desc=%r"
                      % (sample["media_id"], sample["kind"], sample["rung"],
                         sample["description"][:70]))
        return
    for account_id, rungs in PLAN.items():
        verdicts = {{int(m): rung for rung, ids in rungs.items() for m in ids}}

        # Every id must still exist in THIS account's mirror. A phantom
        # membership row would pass the pack audit (all four of its rules are
        # membership tests) and put an unresolvable id in a priced send.
        async with get_session() as s:
            present = set((await s.execute(
                select(VaultItem.media_id).where(
                    VaultItem.account_id == account_id,
                    VaultItem.media_id.in_(list(verdicts)),
                )
            )).scalars().all())
        missing = [m for m in verdicts if m not in present]
        if missing:
            raise SystemExit("%s: %d media id(s) not in vault_items - refusing"
                             % (account_id, len(missing)))

        counts = {{r: len(ids) for r, ids in rungs.items()}}
        if not APPLY:
            print("%s DRY RUN would file %d items %s"
                  % (account_id, len(verdicts), counts))
            continue

        folders = await vault_pack_picker.triage(account_id, cat, verdicts)
        print("%s filed %d items" % (account_id, len(verdicts)))
        for f in folders:
            print("    %-16s folder_id=%-5s count=%-3s of_list_id=%s"
                  % (f["name"], f["folder_id"], f["count"], f["of_list_id"]))

    # Read back independently of what triage() returned. created_by is the one
    # predicate keeping vault_scripts' reaper off these folders and it had
    # never been exercised in production before today.
    print("--- read-back ---")
    async with get_session() as s:
        for account_id in PLAN:
            rows = (await s.execute(
                select(VaultFolder).where(
                    VaultFolder.account_id == account_id,
                    VaultFolder.name.like("feet-%"),
                    VaultFolder.deleted_at.is_(None),
                ).order_by(VaultFolder.name)
            )).scalars().all()
            for f in rows:
                n = len((await s.execute(
                    select(VaultFolderItem.media_id).where(
                        VaultFolderItem.account_id == account_id,
                        VaultFolderItem.folder_id == f.id)
                )).scalars().all())
                ok = f.created_by == "operator" and f.of_list_id is None
                print("%s %-16s id=%-5s items=%-3s created_by=%-10s of_list_id=%s%s"
                      % (account_id, f.name, f.id, n, f.created_by, f.of_list_id,
                         "" if ok else "   <-- CHECK ME"))


asyncio.run(main())
'''

if __name__ == "__main__":
    seed_file, mode = sys.argv[1], sys.argv[2]
    sys.stdout.write(PROGRAM.format(
        apply=(mode == "1"), probe=(mode == "probe"),
        plan=json.dumps(parse(seed_file))))
