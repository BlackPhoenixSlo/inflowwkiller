"""service/fix_nicknames.py — repair OF nicknames whose Name slot isn't a name.

The read-time guards stopped her SAYING the wrong name immediately, but the label
the team reads in the OF chat header is only rewritten when a fan next comes
through of_ai_chat (`_maybe_push_nickname`) or gen_info (`_sync_of_nickname`).
A quiet fan keeps a header that says he is called Canada. This sweeps them.

    docker compose exec -T relay python service/fix_nicknames.py             # dry run
    docker compose exec -T relay python service/fix_nicknames.py --apply
    ... --account ACCOUNT_ID --limit 50 --include-lossy

DRY RUN BY DEFAULT — `--apply` is the only thing that writes to OF.

It reuses exactly what the live code uses: `build_structured_nickname` for the
new label, `push_nick_and_notes` for the write (which also mirrors the result
into `fans.custom_nickname`). So a swept fan lands on the same value the next
convo tick would have produced anyway — this only makes it sooner.

LOSSY rows are held back. `build_structured_nickname` emits Name/City,Country/
Age/Job from stored FACTS, so any extra segment a human typed — 'BJ,cowgirl',
'Married', 'Whale' — is not in its output and would be destroyed by the rewrite.
Those are skipped unless `--include-lossy` is passed, and always listed, because
throwing away someone's curation to fix a cosmetic bug is a bad trade to make
silently.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from sqlalchemy import select

sys.path.insert(0, "/app/service")

from automation_executor import _make_client            # noqa: E402
from automations._common import push_nick_and_notes     # noqa: E402
from automations.names import (                         # noqa: E402
    SPEND_TIERS, build_structured_nickname, is_greetable_name, name_token,
)
from db.engine import get_session                       # noqa: E402
from db.models import Fan                               # noqa: E402

log = logging.getLogger("fix_nicknames")

_PUSH_GAP_S = 0.4      # be kind to the proxy — it 403s on heavy sweeps


def _segments(label: str) -> list[str]:
    return [p.strip() for p in (label or "").split("/") if p.strip()]


_TIER_WORDS = {label.lower() for label, _ in SPEND_TIERS}


def _lost(old: str, new: str) -> list[str]:
    """Segments of the OLD label (minus its bad name slot) that the NEW one does
    not carry. Non-empty ⇒ the rewrite would destroy something a human typed.

    The spend tier is NOT a loss: gen_info derives it from the ledger and
    build_structured_nickname deliberately omits it, so 'Canada/Spender' →
    '_/Canada' drops nothing anybody wrote. Without this the guard held back
    two thirds of the sweep for a word the machine had put there itself."""
    new_blob = new.lower()
    return [seg for seg in _segments(old)[1:]
            if seg.lower() not in new_blob and seg.lower() not in _TIER_WORDS]


async def _candidates(account: str | None) -> list[tuple[Fan, str, str, list[str]]]:
    async with get_session() as s:
        q = select(Fan).where(Fan.custom_nickname.is_not(None), Fan.custom_nickname != "")
        if account:
            q = q.where(Fan.account_id == str(account))
        fans = (await s.execute(q)).scalars().all()

    out = []
    for f in fans:
        cur = (f.custom_nickname or "").strip()
        slot0 = _segments(cur)[0] if _segments(cur) else ""
        # slot 0 CLAIMS to be his name. It's wrong when it isn't one — a place, a
        # spend tier, a handle, or his own city. '_' is the marker: already fixed.
        if not slot0 or slot0 == "_":
            continue
        here = (f.home_country or "", f.home_city or "")
        if is_greetable_name(name_token(cur), here=here):
            continue
        new = build_structured_nickname(f)
        if new and new != cur:
            out.append((f, cur, new, _lost(cur, new)))
    return out


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="actually push to OF")
    ap.add_argument("--account", help="limit to one account id")
    ap.add_argument("--limit", type=int, default=0, help="cap the number pushed")
    ap.add_argument("--include-lossy", action="store_true",
                    help="also rewrite labels that carry a human-typed extra segment")
    args = ap.parse_args()

    rows = await _candidates(args.account)
    safe = [r for r in rows if not r[3]]
    lossy = [r for r in rows if r[3]]
    print(f"wrong name slot: {len(rows)}   safe: {len(safe)}   lossy: {len(lossy)}")

    if lossy:
        print(f"\nHELD BACK — the rewrite would drop a human-typed segment"
              f"{' (included anyway)' if args.include_lossy else ''}:")
        for f, cur, new, lost in lossy[:25]:
            print(f"   {f.fan_id:>10}  {cur[:40]!r:42} → {new[:40]!r}   loses {lost}")
        if len(lossy) > 25:
            print(f"   … and {len(lossy) - 25} more")

    todo = safe + (lossy if args.include_lossy else [])
    if args.limit:
        todo = todo[:args.limit]

    print(f"\n{'PUSHING' if args.apply else 'DRY RUN — would push'} {len(todo)}:")
    for f, cur, new, _ in todo[:15]:
        print(f"   {f.account_id}/{f.fan_id}  {cur[:40]!r:42} → {new[:40]!r}")
    if len(todo) > 15:
        print(f"   … and {len(todo) - 15} more")
    if not args.apply:
        print("\n(dry run — nothing written. re-run with --apply)")
        return 0

    ok = fail = 0
    clients: dict[str, object] = {}
    for f, cur, new, _ in todo:
        try:
            client = clients.get(f.account_id) or clients.setdefault(
                f.account_id, _make_client(f.account_id))
            pushed, _n = await push_nick_and_notes(
                client, f.account_id, f.fan_id, nick=new)
            ok += pushed
            fail += (not pushed)
        except Exception as e:                      # one bad fan never stops the sweep
            fail += 1
            print(f"   ! {f.account_id}/{f.fan_id}: {type(e).__name__}: {e}")
        await asyncio.sleep(_PUSH_GAP_S)
    print(f"\npushed ok: {ok}   failed: {fail}")
    return 0 if not fail else 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    raise SystemExit(asyncio.run(main()))
