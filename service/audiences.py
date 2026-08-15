"""service/audiences.py — build mass-send audiences from the local DB.

These return plain `list[int]` fan-id lists meant to be handed to
`OFClient.send_mass_message(included_users=...)` (the `userIds` body field).
DB-first: no OF call needed — we already have every message in `messages`.

It is also the one home for the house broadcast POLICY constants below (the
default contact-guard windows, and the OF-side list names a "reach everyone"
send targets) — those are audience decisions too, they just resolve OF-side
rather than from our tables.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Iterable

from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from db.engine import get_session
from db.models import (
    Blacklist, Fan, FunnelResponder, FunnelState, List as ListModel, ListMember,
    MassRun, Message, NudgeState, ScheduledJob,
)

log = logging.getLogger("of-relay.audiences")

# Default contact-guard windows for LIST/ONLINE broadcasts (send_mass_message,
# mass_premade, the UI composer). Absent → these; explicit 0 → off — the same
# convention as online_blast's 8h and mass_nudge's 12h defaults.
BROADCAST_DEFAULT_OUTBOUND_H = 6.0  # skip fans WE touched in the last 6h
BROADCAST_DEFAULT_INBOUND_H = 2.0   # skip fans who messaged US in the last 2h

# The house "everyone" audience — OF's two built-in buckets, resolved server-side
# at send time. BOTH, always: "fans" alone is only the ACTIVE paid subs, and on a
# free page that is a fraction of the reachable list — the same day's fans+following
# blasts reached ~3,000 recipients (live 2026-08-11). Nothing else is targeted: a
# userLists send reaches exactly the named lists, so every other audience is
# excluded by omission.
# A tuple so a caller can't mutate the house default; copy it into payloads.
BROADCAST_LISTS: tuple[str, ...] = ("fans", "following")

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


async def _recent_nudged_ids(account_id: str, hours: float) -> set[int]:
    """Fan ids stamped in `NudgeState.last_nudged_at` within `hours` — the
    proactive-touch ledger mass_nudge / nudge_online / online_blast write
    INSTEAD of a `messages` row. Naive-UTC cutoff to match the stored stamps.
    `hours` None/0 → empty."""
    if not hours or hours <= 0:
        return set()
    cutoff = datetime.utcnow() - timedelta(hours=float(hours))
    async with get_session() as s:
        nudged = (await s.execute(
            select(NudgeState.fan_id).where(
                NudgeState.account_id == str(account_id),
                NudgeState.last_nudged_at.is_not(None),
                NudgeState.last_nudged_at >= cutoff,
            )
        )).scalars().all()
    return {int(n) for n in nudged}


async def contact_guard_excludes(
    account_id: str,
    *,
    outbound_hours: float | None = None,
    inbound_hours: float | None = None,
    either_hours: float | None = None,
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
      • `messages` in EITHER direction within `either_hours` — "last chatted"
        (the send-side `exclude_last_chat_hours`): a fan we OR they messaged
        recently, so a still-warm two-way chat isn't interrupted by a blast.
        Also folds in the nudge ledger (a nudge is an outbound contact),
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
        out |= await _recent_nudged_ids(account_id, float(outbound_hours))
    if inbound_hours and inbound_hours > 0:
        out |= set(await recent_chat_fan_ids(
            account_id, hours=float(inbound_hours), direction="in",
            exclude_bots=False, exclude_blacklisted=False,
        ))
    if either_hours and either_hours > 0:
        # BOTH directions — the inbound guard above only drops fans who messaged
        # US; a warm two-way chat needs the outbound side too.
        out |= set(await recent_chat_fan_ids(
            account_id, hours=float(either_hours), direction=None,
            exclude_bots=False, exclude_blacklisted=False,
        ))
        out |= await _recent_nudged_ids(account_id, float(either_hours))
    return out


# The two system exclude lists (kind='exclude'), applied by SEND TYPE:
#   MASSppvEXCLUDE — skipped from mass PPV sends (priced broadcasts)
#   MASSdmEXCLUDE  — skipped from mass DM sends (unpriced text + online blast + nudge)
# A fan on both is excluded from all mass. Mirrored to same-named pinned OF lists
# by service/lists.py.
MASSPPVEXCLUDE_LIST = "MASSppvEXCLUDE"
MASSDMEXCLUDE_LIST = "MASSdmEXCLUDE"


async def exclude_list_fan_ids(account_id: str, name: str) -> set[int]:
    """Fan ids on the account's kind='exclude' list with the given NAME
    (MASSPPVEXCLUDE or MASSDMEXCLUDE). Subtracted from the matching mass-send
    type so a flagged fan never receives that kind of broadcast.
    Local-only (of_list_id is NULL) — no OF round-trip."""
    async with get_session() as s:
        rows = (await s.execute(
            select(ListMember.fan_id).where(
                ListMember.list_id.in_(
                    select(ListModel.id).where(
                        ListModel.account_id == str(account_id),
                        ListModel.kind == "exclude",
                        ListModel.name == name,
                    )
                )
            )
        )).scalars().all()
    return {int(x) for x in rows if x is not None}


# Pending-job kinds whose payload names its recipients. These jobs sit in
# `scheduled_jobs` with a future `run_at` and write NO `messages` row until they
# fire — so the time-window contact guard (which reads `messages`) is blind to
# them. `pending_send_fan_ids` extracts their targets so a broadcast fired NOW
# doesn't double-hit a fan a ≤15-min scheduled/drip send is about to reach.
_PENDING_SEND_KINDS = ("scheduled_send", "send_mass_message", "mass_premade")


def _payload_recipient_ids(payload: dict) -> set[int]:
    """Fan ids a queued send payload targets: `fan_id` (scheduled_send 1:1) +
    `included_users`/`fan_ids` (explicit mass) + the same, nested per message
    in a mass_premade `messages` list. Non-numeric entries are dropped."""
    ids: set[int] = set()
    fid = payload.get("fan_id")
    if fid is not None:
        try:
            ids.add(int(fid))
        except (TypeError, ValueError):
            pass
    for key in ("included_users", "fan_ids"):
        for v in payload.get(key) or []:
            try:
                ids.add(int(v))
            except (TypeError, ValueError):
                pass
    for m in payload.get("messages") or []:
        if isinstance(m, dict):
            ids |= _payload_recipient_ids(m)
    return ids


async def pending_send_fan_ids(account_id: str) -> set[int]:
    """Fan ids ALREADY QUEUED for an outbound send whose `messages` row doesn't
    exist yet — the not-yet-fired scheduled / drip jobs the time-window contact
    guard can't see. Reads PENDING `scheduled_jobs` of `_PENDING_SEND_KINDS` and
    unions each payload's named recipients.

    Closes the "15-min-delay sends aren't tracked → re-runs double-hit" gap: a
    chatter's ≤15-min deferred 1:1 (`scheduled_send`) or a queued/drip broadcast
    that hasn't sent yet would otherwise slip past the guard, so a blast fired
    now re-hits a fan who is about to receive one. A job that is already firing
    is 'running' (not 'pending'), so this never cannibalises the caller's own
    recipients."""
    out: set[int] = set()
    async with get_session() as s:
        rows = (await s.execute(
            select(ScheduledJob.payload_json).where(
                ScheduledJob.account_id == str(account_id),
                ScheduledJob.status == "pending",
                ScheduledJob.kind.in_(_PENDING_SEND_KINDS),
            )
        )).scalars().all()
    for payload_json in rows:
        try:
            payload = json.loads(payload_json or "{}")
        except (ValueError, TypeError):
            continue
        if isinstance(payload, dict):
            out |= _payload_recipient_ids(payload)
    return out


async def funnel_responder_ids(account_id: str, funnel_id: int) -> set[int]:
    """Fans who ALREADY ANSWERED this funnel — the durable per-funnel dedup set
    (R1/R2). Union of:
      • the `funnel_responders` ledger (written by reply_mass_funnel at discovery
        for every confirmed opener-replier — precise + survives pruning/unsend),
      • `funnel_state` fans for any run of this funnel (belt-and-suspenders for
        fans mid-funnel whose ledger row predates this feature).
    A funnel Send subtracts these so a responder is never re-sent the opener,
    while a brand-new funnel (no rows) is unaffected. Keyed on funnel_id, so
    re-running the SAME funnel to a fresh audience still skips prior answerers
    (a future 'reset responders' action would DELETE the ledger rows)."""
    out: set[int] = set()
    async with get_session() as s:
        led = (await s.execute(
            select(FunnelResponder.fan_id).where(
                FunnelResponder.account_id == str(account_id),
                FunnelResponder.funnel_id == int(funnel_id),
            )
        )).scalars().all()
        out |= {int(x) for x in led}
        st = (await s.execute(
            select(FunnelState.fan_id)
            .join(MassRun, FunnelState.mass_run_id == MassRun.id)
            .where(
                MassRun.account_id == str(account_id),
                MassRun.funnel_id == int(funnel_id),
            )
        )).scalars().all()
        out |= {int(x) for x in st}
    return out


async def close_funnel_discovery_for_queue(
    account_id: str, queue_id: int | None = None, mass_run_id: int | None = None,
) -> int | None:
    """Stamp a funnel `mass_run`'s `discovery_closed_at` (idempotent) when its
    first mass is unsent/deleted — so reply_mass_funnel STOPS enrolling NEW
    repliers off it (#R4). The walker keeps advancing already-engaged fans until
    a purchase halts them (#30). Resolve by `mass_run_id` (validated to the
    account) if given, else the most recent funnel `MassRun` by
    (account_id, queue_id, funnel_id IS NOT NULL). Non-funnel runs are a no-op.
    Returns the closed mass_run_id, or None if unresolved (logged, never raises).
    Called from every unsend path (unsend_messages per-target + cache sweep, and
    the manual DELETE /messages/queue endpoint)."""
    async with get_session() as s:
        run = None
        if mass_run_id is not None:
            run = await s.get(MassRun, int(mass_run_id))
            if run is not None and run.account_id != str(account_id):
                run = None
        if run is None and queue_id is not None:
            run = (await s.execute(
                select(MassRun).where(
                    MassRun.account_id == str(account_id),
                    MassRun.queue_id == int(queue_id),
                    MassRun.funnel_id.is_not(None),
                ).order_by(MassRun.id.desc())
            )).scalars().first()
        if run is None:
            log.info("close_funnel_discovery unresolved account=%s queue=%s run=%s",
                     account_id, queue_id, mass_run_id)
            return None
        if run.funnel_id is None:
            return None  # not a funnel run → nothing to close
        if run.discovery_closed_at is None:  # idempotent
            run.discovery_closed_at = datetime.utcnow()
        return int(run.id)


async def resolve_mass_audience(
    account_id: str,
    *,
    included_users: list[int] | None = None,
    excluded_users: list[int] | None = None,
    recent_chat_hours: float | None = None,
    recent_chat_limit: int | None = None,
    exclude_replied_hours: float | None = None,
    exclude_inbound_hours: float | None = None,
    exclude_last_chat_hours: float | None = None,
    exclude_funnel_responders: int | None = None,
    unread_limit: int | None = None,
    exclude_pending_sends: bool = True,
    exclude_list_name: str | None = None,
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

    if exclude_replied_hours or exclude_inbound_hours or exclude_last_chat_hours:
        guard = await contact_guard_excludes(
            account_id,
            outbound_hours=exclude_replied_hours,
            inbound_hours=exclude_inbound_hours,
            either_hours=exclude_last_chat_hours,
        )
        excluded += sorted(guard)

    # Type-specific opt-out: subtract the matching mass-exclude list's members
    # (MASSPPVEXCLUDE for priced sends, MASSDMEXCLUDE for DM sends). Added to
    # `excluded` so they drop from the explicit userIds set below AND feed
    # `ensure_exclude_list` for list/online audiences. Caller passes the name by
    # send type; None → no list applied.
    if exclude_list_name:
        excluded += sorted(await exclude_list_fan_ids(account_id, exclude_list_name))

    # Pending/queued sends the messages-based guards can't see yet: a ≤15-min
    # scheduled 1:1 or a drip/queued broadcast writes no `messages` row until it
    # fires, so a blast now would double-hit a fan who's already about to be
    # reached. Exclude their queued targets. Always-on; pass
    # exclude_pending_sends=False to skip. See `pending_send_fan_ids`.
    if exclude_pending_sends:
        excluded += sorted(await pending_send_fan_ids(account_id))

    # ── Funnel-responder dedup (R1/R2): drop fans who already answered THIS
    #    funnel. `skipped_funnel_responders` = the ones actually about to be sent
    #    (in the resolved include set) — that's the count the UI prints. The full
    #    responder set is added to `excluded` so the caller's list-exclude path
    #    (Auto_Exclude) also drops them for a list/online audience. For a list
    #    send the caller materializes members into `included` first (D6), so the
    #    intersection below reflects the real skipped recipients.
    skipped_responders: list[int] = []
    responders_total = 0
    if exclude_funnel_responders:
        resp = await funnel_responder_ids(account_id, int(exclude_funnel_responders))
        if resp:
            responders_total = len(resp)
            inc_set = {int(i) for i in included}
            skipped_responders = sorted(inc_set & resp)
            excluded += sorted(resp)
    excluded = list(dict.fromkeys(excluded))

    # Subtract excludes from the explicit include set HERE — OF's queue body
    # has no per-user exclusion field (`excludedUsers` is silently dropped;
    # verified live 2026-06-12), so excluded ids left inside `userIds` WOULD
    # receive the blast. List/online audiences need `ensure_exclude_list`.
    excl_set = {int(x) for x in excluded}
    included = [i for i in included if int(i) not in excl_set]

    return {
        "included_users": included,
        "excluded_users": excluded,
        # Precise "removed from THIS send" (responders ∩ explicit includes).
        "skipped_funnel_responders": skipped_responders,
        # All known answerers for the funnel (ALL are excluded, incl. via the
        # Auto_Exclude list for OF-native audiences we can't enumerate locally) —
        # the honest "N already answered" number for a list/online funnel launch.
        "funnel_responders_total": responders_total,
    }


AUTO_EXCLUDE_LIST_NAME = "Auto_Exclude"


_LIST_PAGE = 100


def _page_all_checked(fetch) -> tuple[list[dict], bool]:
    """`_page_all` plus an explicit truncation flag: (rows, hit_backstop).
    The audience-fence / include-mirror paths must treat a truncated crawl as a
    FAILED sync (loud), never as a complete snapshot — a silently short read
    would drop every member past the ceiling and un-fence / un-admit them."""
    out: list[dict] = []
    seen_ids: set = set()
    offset = 0
    for _ in range(200):                              # backstop: 200 × 100 = 20k rows
        r = fetch(offset)
        items = (r.get("list") or []) if isinstance(r, dict) else (r or [])
        added = 0
        for it in items:
            if not isinstance(it, dict):
                continue
            iid = it.get("id")
            if iid is not None:
                if iid in seen_ids:
                    continue
                seen_ids.add(iid)
            out.append(it)
            added += 1
        if not items or added == 0:                   # exhausted / repeated page
            break
        if isinstance(r, dict) and not r.get("hasMore"):
            break
        offset += len(items)
    else:
        log.warning("_page_all hit the 200-page backstop (%d rows) — repeated-page "
                    "guard never fired; result may be truncated", len(out))
        return out, True
    return out, False


def _page_all(fetch):
    """Page an OF list endpoint to exhaustion, CAP-AGNOSTIC. `fetch(offset)`
    returns one page. Two shapes (both verified live 2026-06-12):
      • `/lists?format=infinite` → `{list, hasMore}` envelope — trust hasMore
        (OF honours limit=100 but may page; never guess from page length).
      • `/lists/{id}/users` → a BARE list with NO hasMore — page until an EMPTY
        page, so it's correct whatever page size OF actually honours (a short-
        page stop would under-read if OF ever caps below our requested limit).
    Without exhaustive paging, an exclude list with >1 page of members would
    only read page 1 and wrongly drop everyone past it as 'stale'.

    Guards (2026-07-04): some OF endpoints IGNORE `offset` and report
    hasMore=true forever (verified live on /users/me/stats/messages/group) —
    an unguarded exhaustion loop then spins forever inside a to_thread worker
    on the mass-broadcast hot path. Dedup by item id and stop on a page that
    adds nothing new, plus a hard page ceiling as the backstop."""
    return _page_all_checked(fetch)[0]


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
    # Judge the guard by its COVERAGE, never by this tick's add success rate.
    # The old test was `add_fail == len(to_add)` and it sank live broadcasts:
    # an un-addable id never becomes a member, so it returns in `to_add` on
    # EVERY later tick. Once the list reaches steady state (all the real fans
    # already on it) those permanent stragglers are the ONLY entries left to
    # add — so "every add failed" is the ROUTINE case, not the systemic one it
    # was meant to catch, and it fail-closed ~40% of ppv_send's broadcasts
    # (47 of 116 attempts over 45 days, measured from the broadcast:all cell in
    # automation_runs.stats_json — the run itself still reads 'ok' at the top,
    # which is why it hid; see the `reason` propagation in ppv_send). Note a
    # failed sync mints NO mass_runs row at all, so that table cannot be used
    # to count these. What actually makes the guard unusable is an EMPTY list, so
    # that is what we test: if not one id we need excluded is on the list, the
    # next broadcast would blast everyone — fail loudly. If 447 are on it and
    # one straggler won't add, the guard is intact; honour the docstring above
    # ("ONE such id must not abort the whole exclude build") and carry on.
    covered = len(ids & members) + (len(to_add) - add_fail)
    if ids and covered == 0:
        raise last_err or RuntimeError(
            "auto_exclude: no ids could be listed — exclude guard is empty")
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
