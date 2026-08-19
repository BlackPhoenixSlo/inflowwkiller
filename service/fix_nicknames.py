"""service/fix_nicknames.py — repair OF nicknames whose Name slot isn't a name.

The read-time guards stopped her SAYING the wrong name immediately, but the label
the team reads in the OF chat header is only rewritten when a fan next comes
through welcome_chatter_for_info (`_maybe_push_nickname`) or gen_info (`_sync_of_nickname`).
A quiet fan keeps a header that says he is called Canada. This sweeps them.

    docker compose exec -T -w /app/service relay python fix_nicknames.py      # dry run
    docker compose exec -T -w /app/service relay python fix_nicknames.py --apply
    ... --account ACCOUNT_ID --limit 50 --tier-only

DRY RUN BY DEFAULT — `--apply` is the only thing that writes to OF.

It reuses what the live code uses: `build_structured_nickname` for the label and
`push_nick_and_notes` for the write (which also mirrors into
`fans.custom_nickname`), so a swept fan lands where his next convo tick would
have put him anyway — this only makes it sooner.

THE REPAIR NEVER LOSES ANYTHING. `build_structured_nickname` emits Name/City,
Country/Age/Job from stored FACTS, so a segment a human typed ('BJ,cowgirl',
'Single dad', 'Married') is absent from its output — a straight rewrite would
destroy it. So the rebuild is re-joined with whatever it dropped, and when we
hold no facts at all the label is KEPT and only the name slot gets marked:

    'Canada/BJ,cowgirl'       → '_/Canada/BJ,cowgirl'     (rebuild + the note back)
    'Surrey,Canada/Single dad'→ '_/Surrey,Canada/Single dad'
    'USA/Spender'             → '_/USA'                   (no facts: demote, drop tier)

That is why there is no --include-lossy flag: nothing is lossy, so nothing needs
a human to approve the loss. The derived spend tier is the one exception — nothing
authors Free/Buyer/Spender/Whale any more (see names.strip_spend_tier) and
build_structured_nickname never emitted them, so dropping one destroys nothing
anybody wrote.

The sweep picks a fan up for EITHER of two reasons, and they get different repairs:

    name slot isn't a name   → rebuild from facts, re-append what the rebuild drops
    name slot is fine, tier  → strip the tier ONLY, leave the rest of the label alone
    present ('Jim/UK/Free')    ('Jim/UK'), because nothing else about it is wrong

The second reason is the backfill for the tier retirement: ~766 prod fans carry a
tier in the OF chat header, and the live code paths only heal a fan when he next
comes through gen_info/apply_profiles. A quiet fan would keep his forever.
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
    _NICK_MAX, _NO_NAME,   # the 70-char cap and the empty-name marker — same two
                           # constants the live push uses; don't re-declare them
    SPEND_TIER_WORDS, build_structured_nickname, is_greetable_name, name_token,
    strip_spend_tier,
)
from db.engine import get_session                       # noqa: E402
from db.models import Fan                               # noqa: E402

log = logging.getLogger("fix_nicknames")

_PUSH_GAP_S = 0.4      # be kind to the proxy — it 403s on heavy sweeps


def _segments(label: str) -> list[str]:
    return [p.strip() for p in (label or "").split("/") if p.strip()]


def _lost(old: str, new: str) -> list[str]:
    """Segments of the OLD label (minus its bad name slot) that the NEW one does
    not carry. Non-empty ⇒ the rewrite would destroy something a human typed.

    The spend tier is NOT a loss: gen_info derives it from the ledger and
    build_structured_nickname deliberately omits it, so 'Canada/Spender' →
    '_/Canada' drops nothing anybody wrote. Without this the guard held back
    two thirds of the sweep for a word the machine had put there itself."""
    new_blob, segs = new.lower(), _segments(old)
    out = [seg for seg in segs[1:]
           if seg.lower() not in new_blob and seg.lower() not in SPEND_TIER_WORDS]
    # The note can live INSIDE slot 0 — 'Alex- rough sex kink. Gets paid on the
    # 1st' is ONE segment, so the loop above sees nothing to lose and the rebuild
    # would have kept 'Alex' and thrown the rest away. Rescue whatever of that slot
    # the rebuild didn't take. Skipped when slot 0 is a LOCATION (has a comma):
    # build_structured_nickname reconstructs the location itself, so re-appending
    # it would just duplicate the country.
    head = segs[0] if segs else ""
    if head and "," not in head:
        leftover = head.replace(name_token(head), "", 1).strip(" -,;/")
        if (any(c.isalpha() for c in leftover) and leftover.lower() not in new_blob
                and leftover.lower() not in SPEND_TIER_WORDS):
            out.insert(0, leftover)
    return out


def _repair(f: Fan, cur: str) -> str:
    """The label this fan SHOULD carry — a rebuild that gives nothing back.

    Rebuild from facts, then re-append every segment the rebuild dropped, so the
    fix costs the team none of their own words. With no facts to rebuild from,
    keep the label exactly as it is and only mark the empty name slot."""
    new = build_structured_nickname(f)
    if not new:
        # Nothing to rebuild from: keep every word a human contributed and mark the
        # name slot. When even that is empty the label was pure derived tier
        # ('Spender/Buyer') — it carries no information at all, so the marker ALONE
        # is the honest value. A bare '_' says "we don't know his name"; leaving
        # 'Spender' there says he is called Spender.
        keep = [s for s in _segments(cur) if s.lower() not in SPEND_TIER_WORDS]
        return "/".join([_NO_NAME] + keep)[:_NICK_MAX]
    return "/".join([new] + _lost(cur, new))[:_NICK_MAX]


async def _candidates(account: str | None, *,
                      tier_only: bool = False) -> list[tuple[Fan, str, str]]:
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
        if not slot0 or slot0 == _NO_NAME:
            continue
        here = (f.home_country or "", f.home_city or "")
        # Ask is_greetable_name about SLOT 0 itself, not about name_token's reading
        # of it. name_token additionally demands a capital and 2+ letters — fine for
        # picking a name to say out loud, wrong for judging a stored label: it
        # reported 26 perfectly good ones ('jay', 'A', 'RG/Vancouver Island,Canada')
        # as broken. A comma still marks a location slot, which is not a name.
        if "," not in slot0 and is_greetable_name(slot0, here=here):
            # His NAME is fine — but the label may still carry a retired spend tier
            # ('DaddyWilko/Free'). That is the second reason to sweep a fan, and the
            # repair is a STRIP, not a rebuild: a rebuild restructures a label a
            # human may have curated, and here there is nothing wrong with it except
            # one machine-authored word. Nothing else about the label is touched.
            #
            # The test is "a tier segment is PRESENT", deliberately not "the strip
            # changed the string". strip_spend_tier also trims each segment, so the
            # looser test enrolled fans whose only defect was a trailing space
            # ('Kevin /48/NJ- USA') — a live OF write per fan to fix whitespace
            # nobody can see. This sweep touches a tier or it leaves him alone.
            if any(seg.lower() in SPEND_TIER_WORDS for seg in _segments(cur)):
                stripped = strip_spend_tier(cur)
                if stripped:
                    out.append((f, cur, stripped[:_NICK_MAX]))
            continue
        # `--tier-only`: the tier retirement is one job and the name-slot rebuild is
        # another, older one. They share this sweep but not a reason to run, and on
        # prod the rebuild branch is another 373 fans — 373 live OF writes nobody
        # asked for. Keeping them separately runnable is what makes either safe to
        # fire; drop the rebuild candidates here rather than after the push.
        if tier_only:
            continue
        new = _repair(f, cur)
        if new and new != cur:
            out.append((f, cur, new))
    return out


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="actually push to OF")
    ap.add_argument("--account", help="limit to one account id")
    ap.add_argument("--limit", type=int, default=0, help="cap the number pushed")
    ap.add_argument("--tier-only", action="store_true",
                    help="ONLY strip retired spend tiers; skip the name-slot rebuild")
    args = ap.parse_args()

    todo = await _candidates(args.account, tier_only=args.tier_only)
    if args.limit:
        todo = todo[:args.limit]

    print(f"{'PUSHING' if args.apply else 'DRY RUN — would push'} {len(todo)}:")
    for f, cur, new in todo[:15]:
        print(f"   {f.account_id}/{f.fan_id}  {cur[:40]!r:42} → {new[:40]!r}")
    if len(todo) > 15:
        print(f"   … and {len(todo) - 15} more")
    if not args.apply:
        print("\n(dry run — nothing written. re-run with --apply)")
        return 0

    # An account with no captured session cannot reach OF at all — every fan on it
    # raises the same FileNotFoundError. Probe ONCE per account and drop the whole
    # group, so the run reports "no session" as the one fact it is instead of 162
    # identical stack traces burying the real failures.
    ok = fail = 0
    clients: dict[str, object] = {}
    dead: dict[str, int] = {}
    for f, cur, new in todo:
        aid = f.account_id
        if aid in dead:
            dead[aid] += 1
            continue
        try:
            if aid not in clients:
                clients[aid] = _make_client(aid)
        except Exception as e:
            dead[aid] = 1
            print(f"   – account {aid}: skipping all — {type(e).__name__}: "
                  f"{str(e).splitlines()[0][:90]}")
            continue
        for attempt in (1, 2, 3):
            try:
                pushed, _n = await push_nick_and_notes(
                    clients[aid], aid, f.fan_id, nick=new)
                ok += pushed
                fail += (not pushed)
                break
            except Exception as e:
                # The relay is writing to the same SQLite the mirror updates; a
                # busy moment is worth waiting out, not failing on. NB the OF push
                # happens BEFORE the mirror inside push_nick_and_notes, so a lock
                # here means OF already took the new label.
                if "database is locked" in str(e) and attempt < 3:
                    await asyncio.sleep(1.5 * attempt)
                    continue
                fail += 1
                print(f"   ! {aid}/{f.fan_id}: {type(e).__name__}: "
                      f"{str(e).splitlines()[0][:90]}")
                break
        await asyncio.sleep(_PUSH_GAP_S)
    if dead:
        print("\nskipped — account has no captured session on this host "
              "(cannot reach OF; their labels stay as-is):")
        for aid, n in sorted(dead.items(), key=lambda kv: -kv[1]):
            print(f"   {aid}: {n} fans")
    print(f"\npushed ok: {ok}   failed: {fail}   skipped (no session): {sum(dead.values())}")
    return 0 if not fail else 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    raise SystemExit(asyncio.run(main()))
