"""service/audiences.py — build mass-send audiences from the local DB.

These return plain `list[int]` fan-id lists meant to be handed to
`OFClient.send_mass_message(included_users=...)` (the `userIds` body field).
DB-first: no OF call needed — we already have every message in `messages`.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Iterable

from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from db.engine import get_session
from db.models import Blacklist, Fan, Message, NudgeState

log = logging.getLogger("of-relay.audiences")

# Default contact-guard windows for LIST/ONLINE broadcasts (send_mass_message,
# mass_premade, the UI composer). Absent → these; explicit 0 → off — the same
# convention as online_blast's 8h and mass_nudge's 12h defaults.
BROADCAST_DEFAULT_OUTBOUND_H = 6.0  # skip fans WE touched in the last 6h
BROADCAST_DEFAULT_INBOUND_H = 2.0   # skip fans who messaged US in the last 2h

# One lock per account around the Auto_Exclude sync + OF send: two concurrent
# broadcasts rewriting the same OF list would attach each other's exclude sets.
# Module-level is process-wide — the relay runs the executor AND the API.
_broadcast_locks: dict[str, asyncio.Lock] = {}


def broadcast_lock(account_id: str) -> asyncio.Lock:
    return _broadcast_locks.setdefault(str(account_id), asyncio.Lock())


async def recent_chat_fan_ids(
    account_id: str,
    *,
    hours: float = 2.0,
    direction: str | None = None,
    limit: int | None = None,
    exclude_bots: bool = True,
    exclude_blacklisted: bool = True,
) -> list[int]:
    """Fan ids we exchanged a message with in the last `hours`, newest first.

    `direction`: None = either side (default), 'in' = fans who messaged us,
    'out' = fans we messaged. `limit` caps the list (the "N ids" cap).
    Bots and blacklisted fans are dropped by default.
    """
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    async with get_session() as s:
        # Newest activity per fan, so a `limit` keeps the MOST recent chatters.
        q = (
            select(Message.fan_id)
            .where(Message.account_id == account_id, Message.created_at >= since)
        )
        if direction in ("in", "out"):
            q = q.where(Message.direction == direction)
        q = (
            q.group_by(Message.fan_id)
            .order_by(func.max(Message.created_at).desc())
        )
        if limit:
            q = q.limit(int(limit))
        ids = [int(r) for r in (await s.execute(q)).scalars().all() if r]
        if not ids:
            return []

        drop: set[int] = set()
        if exclude_bots:
            bots = (await s.execute(
                select(Fan.fan_id).where(
                    Fan.account_id == account_id,
                    Fan.fan_id.in_(ids),
                    Fan.is_bot.is_(True),
                )
            )).scalars().all()
            drop.update(int(b) for b in bots)
        if exclude_blacklisted:
            # Blacklist is global (no account scoping) — just fan_id.
            bl = (await s.execute(
                select(Blacklist.fan_id).where(Blacklist.fan_id.in_(ids))
            )).scalars().all()
            drop.update(int(b) for b in bl)

        return [i for i in ids if i not in drop]


def resolve_window_hours(value, default: float) -> float:
    """The shared None→default / 0→off convention for contact-guard windows
    (pioneered by mass_nudge): key absent/None → `default` hours; an explicit
    0 (or negative) → 0.0 = guard off; unparseable → `default`."""
    if value is None:
        return float(default)
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return float(default)


async def contact_guard_excludes(
    account_id: str,
    *,
    outbound_hours: float | None = None,
    inbound_hours: float | None = None,
    extra_ids: Iterable[int] | None = None,
) -> set[int]:
    """Fan ids a PROACTIVE touch must skip — the cross-automation contact guard.

    Unions every "we already touched them / they're already engaged" ledger:
      • `messages` outbound within `outbound_hours` — 1:1 sends AND explicit-id
        mass sends (those write optimistic rows the moment they fire),
      • `NudgeState.last_nudged_at` within `outbound_hours` — mass_nudge /
        nudge_online stamp NudgeState INSTEAD of messages (deliberate: per-fan
        rows for every nudge would reshuffle inbox previews constantly), and
        online_blast stamps its online snapshot here too
        (`stamp_broadcast_touch` — OF echoes no per-fan ids for a list send),
      • `messages` inbound within `inbound_hours` — active repliers,
      • any `extra_ids` (explicit excludes, exclude-list members, …).

    Bots/blacklisted are NOT filtered out here — this builds an EXCLUDE set,
    not an audience; dropping a blacklisted fan from the excludes would let a
    broadcast reach them. (`recent_chat_fan_ids`' defaults are for includes.)
    A window that is None/0 is off. Returns a plain set[int].
    """
    out: set[int] = {int(x) for x in (extra_ids or [])}
    if outbound_hours and outbound_hours > 0:
        out |= set(await recent_chat_fan_ids(
            account_id, hours=float(outbound_hours), direction="out",
            exclude_bots=False, exclude_blacklisted=False,
        ))
        # Nudge ledger: naive-UTC cutoff to match the stored stamps.
        cutoff = datetime.utcnow() - timedelta(hours=float(outbound_hours))
        async with get_session() as s:
            nudged = (await s.execute(
                select(NudgeState.fan_id).where(
                    NudgeState.account_id == str(account_id),
                    NudgeState.last_nudged_at.is_not(None),
                    NudgeState.last_nudged_at >= cutoff,
                )
            )).scalars().all()
        out |= {int(n) for n in nudged}
    if inbound_hours and inbound_hours > 0:
        out |= set(await recent_chat_fan_ids(
            account_id, hours=float(inbound_hours), direction="in",
            exclude_bots=False, exclude_blacklisted=False,
        ))
    return out


async def resolve_mass_audience(
    account_id: str,
    *,
    included_users: list[int] | None = None,
    excluded_users: list[int] | None = None,
    recent_chat_hours: float | None = None,
    recent_chat_limit: int | None = None,
    exclude_replied_hours: float | None = None,
    exclude_inbound_hours: float | None = None,
    unread_limit: int | None = None,
    client=None,
) -> dict:
    """Merge the DB/OF-sourced audience knobs into explicit include/exclude
    fan-id lists — the SAME resolution the relay's `/messages/queue` handler
    does, lifted out so the automation path (mass_premade → send_mass_message)
    targets the identical audience as the Mass Online composer.

    ADD to the include set:
      • `recent_chat_hours` (capped to `recent_chat_limit`) — fans we exchanged
        a message with recently (newest first, from the local `messages` table),
      • `unread_limit` — fans with unread messages (the OF "Unread" inbox tab;
        needs an OF `client` — one is built lazily off-thread if not given).
    ADD to the exclude set (via `contact_guard_excludes` — any outbound TOUCH,
    so nudges stamped only in NudgeState count too, and bots/blacklisted are
    kept in the excludes):
      • `exclude_replied_hours` — fans we sent an OUTBOUND message to (or
        nudged) recently,
      • `exclude_inbound_hours` — fans who messaged us INBOUND recently.

    Bots + blacklisted fans are dropped from the INCLUDE side by
    `recent_chat_fan_ids`. Returns `{"included_users": [...],
    "excluded_users": [...]}` (deduped, order kept).
    """
    included = list(included_users or [])
    excluded = list(excluded_users or [])

    if recent_chat_hours:
        recent_ids = await recent_chat_fan_ids(
            account_id, hours=recent_chat_hours, limit=recent_chat_limit,
        )
        included = list(dict.fromkeys([*included, *recent_ids]))

    if unread_limit:
        c = client
        if c is None:
            # Off-thread: building the client + the unread lookup are blocking.
            import automation_executor as ax
            c = await asyncio.to_thread(ax._make_client, account_id)
        unread_ids = await asyncio.to_thread(
            lambda: c.unread_chat_fan_ids(max_fans=int(unread_limit))
        )
        included = list(dict.fromkeys([*included, *unread_ids]))

    if exclude_replied_hours or exclude_inbound_hours:
        guard = await contact_guard_excludes(
            account_id,
            outbound_hours=exclude_replied_hours,
            inbound_hours=exclude_inbound_hours,
        )
        excluded += sorted(guard)
    excluded = list(dict.fromkeys(excluded))

    # Subtract excludes from the explicit include set HERE — OF's queue body
    # has no per-user exclusion field (`excludedUsers` is silently dropped;
    # verified live 2026-06-12), so excluded ids left inside `userIds` WOULD
    # receive the blast. List/online audiences need `ensure_exclude_list`.
    excl_set = {int(x) for x in excluded}
    included = [i for i in included if int(i) not in excl_set]

    return {"included_users": included, "excluded_users": excluded}


AUTO_EXCLUDE_LIST_NAME = "Auto_Exclude"


_LIST_PAGE = 100


def _page_all(fetch):
    """Page an OF list endpoint to exhaustion, CAP-AGNOSTIC. `fetch(offset)`
    returns one page. Two shapes (both verified live 2026-06-12):
      • `/lists?format=infinite` → `{list, hasMore}` envelope — trust hasMore
        (OF honours limit=100 but may page; never guess from page length).
      • `/lists/{id}/users` → a BARE list with NO hasMore — page until an EMPTY
        page, so it's correct whatever page size OF actually honours (a short-
        page stop would under-read if OF ever caps below our requested limit).
    Without exhaustive paging, an exclude list with >1 page of members would
    only read page 1 and wrongly drop everyone past it as 'stale'."""
    out: list[dict] = []
    offset = 0
    while True:
        r = fetch(offset)
        if isinstance(r, dict):                       # {list, hasMore} envelope
            items = r.get("list") or []
            out.extend(it for it in items if isinstance(it, dict))
            if not items or not r.get("hasMore"):
                break
        else:                                         # bare list (no hasMore)
            items = r or []
            out.extend(it for it in items if isinstance(it, dict))
            if not items:                             # empty page → exhausted
                break
        offset += len(items)
    return out


def _sync_exclude_list_blocking(client, ids: set[int]) -> int:
    """Reconcile the account's Auto_Exclude OF custom list to exactly `ids`.
    Blocking (sync OF client) — call via asyncio.to_thread. Returns list id."""
    lists = _page_all(lambda off: client.get_lists(limit=_LIST_PAGE, offset=off))
    list_id = next((it.get("id") for it in lists
                    if (it.get("name") or "") == AUTO_EXCLUDE_LIST_NAME), None)
    if list_id is None:
        list_id = client.create_list(AUTO_EXCLUDE_LIST_NAME).get("id")

    member_rows = _page_all(
        lambda off: client.list_users_in(list_id, limit=_LIST_PAGE, offset=off))
    members = {int(u["id"]) for u in member_rows if u.get("id") is not None}

    # Stale members are REMOVED, not just superseded — a fan left behind from
    # a previous tick would be excluded from every future broadcast forever.
    #
    # Per-user adds are best-effort: OF 403s "Unable to add user to user list"
    # for the odd un-listable id (deleted/blocked/restricted account, the creator
    # herself). ONE such id must not abort the whole exclude build and sink the
    # broadcast — skip it and carry on. An un-addable id is almost always also
    # undeliverable, so leaving it off the guard is low-risk. If EVERY add fails
    # the list is unusable, so re-raise the last error (a systemic auth problem,
    # not a stray fan).
    to_add = sorted(ids - members)
    add_fail = 0
    last_err: Exception | None = None
    for uid in to_add:
        try:
            client.add_user_to_list(list_id, uid)
        except Exception as e:  # noqa: BLE001 — per-user tolerance, see above
            add_fail += 1
            last_err = e
    if to_add and add_fail == len(to_add):
        raise last_err  # nothing landed → the guard is empty, fail loudly
    if add_fail:
        log.warning("auto_exclude partial account-list=%s added=%d skipped=%d (last=%r)",
                    list_id, len(to_add) - add_fail, add_fail, last_err)
    for uid in sorted(members - ids):
        try:
            client.remove_user_from_list(list_id, uid)
        except Exception:  # noqa: BLE001 — a stale member we can't drop is harmless
            log.warning("auto_exclude remove failed account-list=%s uid=%s", list_id, uid)
    return int(list_id)


async def ensure_exclude_list(
    account_id: str,
    ids: Iterable[int],
    *,
    client=None,
) -> int | None:
    """Mirror `ids` into the per-account Auto_Exclude OF custom list and return
    its id — the ONLY way to exclude individual fans from a `userLists`/online
    broadcast. OF's queue endpoint has no per-user exclusion field (verified
    live 2026-06-12: `excludedUsers` is silently dropped) but DOES honor
    `excludedLists`, so callers pass the returned id via `excluded_user_lists`.

    Returns None for an empty `ids` (nothing to exclude — don't attach the
    list, it may hold stale members from a previous run). Raises on OF
    errors: callers decide whether to fail the broadcast (recommended — a
    blast without its guard is exactly the bug this exists to fix) or warn.
    """
    idset = {int(x) for x in (ids or set())}
    if not idset:
        return None
    c = client
    if c is None:
        import automation_executor as ax
        c = await asyncio.to_thread(ax._make_client, account_id)
    list_id = await asyncio.to_thread(_sync_exclude_list_blocking, c, idset)
    log.info("auto_exclude synced account=%s list=%s ids=%d",
             account_id, list_id, len(idset))
    return list_id


async def stamp_broadcast_touch(
    account_id: str,
    fan_ids: Iterable[int],
    *,
    now: datetime | None = None,
) -> int:
    """Record a list/online broadcast's send-time audience snapshot in
    NudgeState.last_nudged_at so the NEXT proactive touch sees it through
    `contact_guard_excludes`. OF echoes no per-fan ids for list sends and the
    scrape reconciler is hours late — without this, broadcasts are invisible
    to each other. Touches ONLY last_nudged_at/updated_at: nudge_count,
    no_reply_streak and the slot rotation belong to mass_nudge/nudge_online.

    Slightly over-broad by design (a fan who churned between our snapshot and
    OF's resolution gets stamped anyway) — over-broad on a cooldown is the
    safe direction. Returns the number of fans stamped.

    COUPLING (online_blast only caller today): seeding NudgeState rows makes
    nudge_online's `_account_warmed` (which returns True if ANY row exists) see
    the account as warmed, so if online_blast runs before nudge_online's first
    tick, nudge_online skips its silent warm-up seeding. The 12h last_nudged_at
    cap still protects the blasted fans; only relevant when BOTH automations run
    on one account. Insert sets last_seen_online_at=NULL (we don't claim a live
    sighting) — a stamped fan re-qualifies for nudge_online's online diff."""
    ids = sorted({int(x) for x in (fan_ids or [])})
    if not ids:
        return 0
    ts = now or datetime.utcnow()
    async with get_session() as s:
        for fid in ids:
            await s.execute(
                sqlite_insert(NudgeState)
                .values(account_id=str(account_id), fan_id=fid,
                        last_nudged_at=ts, nudge_count=0, updated_at=ts)
                .on_conflict_do_update(
                    index_elements=["account_id", "fan_id"],
                    set_={"last_nudged_at": ts, "updated_at": ts})
            )
    return len(ids)
