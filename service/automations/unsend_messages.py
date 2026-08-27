"""service/automations/unsend_messages.py — A12 unsend_messages (P4a).

Spec: library/one_section_of_automations/12_unsend_messages.md (network-rewrite
mapping at the bottom) + the PROMPTS.md A12 block. The legacy DOM script walked
`/my/statistics/engagement/messages` and clicked Unsend per row; this is the
API rewrite — NO DOM, NO Playwright. Two OF endpoints, picked per message:

  • per-chat bubble  → DELETE /api2/v2/messages/{message_id}  (of_client.unsend_message)
        OF's per-message edit window is ~24h; older per-chat messages 400.
  • mass broadcast   → DELETE /api2/v2/messages/queue/{queue_id} (of_client.cancel_scheduled)
        Unsends the broadcast from EVERY recipient. No edit window
        (`unsendSeconds=1000000`) — works basically forever.
  • feed post        → DELETE /api2/v2/posts/{post_id} (of_client.delete_post)
        Deletes a self-cleaning auto_posts feed post. Flips our `posts` row
        status='deleted'. This is the cleanup leg of the auto_posts automation.

After OF confirms, we flip `messages.is_unsent` for the rows we hold and write
an `actions` audit row (employee_id NULL = Automation). `run_once` opens and
finalizes the `automation_runs` row — `run` just returns a stats dict.

Targeting — the payload drives what gets unsent:

  payload = {
    "targets": [
      {"message_id": 123, "fan_id": 456},        # per-chat (24h window)
      {"queue_id": 789, "mass_run_id": 12},      # mass (mass_run_id optional,
    ],                                            #   used only to flip our rows)
    # OR, with no explicit targets, a DB policy sweep of our OWN messages:
    "policy": {"text_hours": 9, "media_hours": 12, "mass_text_hours": 8},
    #   text : media_count==0, text_hours..24h  → per-chat unsend (default)
    #   media: media_count>0,  media_hours..24h → per-chat unsend (opt-in)
    #   mass_text_hours: free + image-less BROADCASTS older than N hours →
    #     mass-queue unsend (opt-in; no 24h window). A real run first pulls
    #     broadcast history from OF into mass_broadcast_cache (paginated) so the
    #     sweep sees blasts the UI never opened the tab to cache.
    #     `"mass_text": true` (no hours) uses the 8h default.
    "dry_run": True,                              # preview only — no OF call
  }

A target carrying `queue_id` is a mass unsend; otherwise it's a per-chat unsend.
The policy sweep only covers the *achievable* per-chat path: outbound bubbles
inside the 24h edit window (older than `text_hours`/`media_hours`, younger than
24h). Text-only bubbles sweep by default; MEDIA bubbles sweep only when the
operator opts in with `policy.media_hours` (reviving V1's separate-window
auto-unsend of media — bounded by the per-chat 24h window, since a pure-DB sweep
can't recover OF's queue id to call the forever-window mass endpoint). Mass /
media-older-than-the-window unsends still need an explicit `queue_id` target.
(See spec "Network-rewrite mapping": media>5d is a mass-queue delete.)

`dry_run` short-circuits AFTER target resolution but BEFORE any OF call: it
returns the exact `would_unsend` list the run would have unsent, touching
neither OF nor our `messages` rows — a preview seam for the operator UI.

Unsend touches no fan inbox and spends no LLM budget, so — like scrape_chats —
it takes neither `acquire_fan_lease` nor `account_spend_lock`.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta

from sqlalchemy import and_, func, or_, select, update

import automation_executor as ax  # shared _make_client seam (tests patch ax._make_client)
from automation_registry import register
from db.engine import get_session
from db.models import Action, MassBroadcastCache, MassRun, Message, Post
from messages import mark_mass_runs_unsent, mark_unsent  # the mirror-write half

UNSENT_REASON = "automation:unsend_messages"  # stamped on every row we flip

log = logging.getLogger("of-relay.automation.unsend")

# Per-chat (DELETE /messages/{id}) only works inside OF's ~24h edit window.
_PER_CHAT_WINDOW_H = 24
# Default policy: unsend outbound text bubbles older than this many hours.
_DEFAULT_TEXT_HOURS = 9
# Default age for the MASS free-text cleanup sweep (a free, image-less broadcast
# is a low-value engagement blast — unsend it once it's done its job).
_DEFAULT_MASS_TEXT_HOURS = 8
# A priced PPV broadcast is NEVER auto-unsent before this many hours (prod: 47% of
# mass-PPV revenue, 68% of it first-ever buys, lands >4h after send; the curve knees
# at 24h). A backend floor for the price class, not merely a UI default.
_MIN_MASS_PRICE_HOURS = 24
# Safety ceiling so a misconfigured sweep can't unsend an entire account.
_MAX_SWEEP = 200

# Mass sweep reads `mass_broadcast_cache`, which is otherwise only refreshed when a
# human opens the Mass-messages tab — so on an account whose blasts come from
# automations the cache is empty/stale and the hourly sweep finds nothing. Before
# the mass sweep we pull broadcast history from OF and write it through. Mass sends
# have no unsend window, so the sweep can act on anything in this lookback; 30 days
# covers any realistic backlog while bounding the OF roundtrip. OF returns
# broadcasts newest-first with `hasMore`, so we PAGINATE — a single page can't
# cover a high-volume account's older blasts — up to a page cap.
_MASS_REFRESH_LOOKBACK_DAYS = 30
_MASS_REFRESH_PAGE = 100
_MASS_REFRESH_MAX_PAGES = 50  # 50 × 100 = 5000 broadcasts — far past any real hourly volume
# Steady-state lookback once the cache holds rows for the account. A broadcast
# only needs to be IN the cache by the time it ages past the smallest class
# threshold (hours); the sweep itself reads the whole cache table, and canceled
# state is flipped locally on every unsend — so re-pulling 30 days of history
# every run buys nothing. 72h absorbs a weekend of relay downtime. The full
# 30-day pull still runs on a COLD cache (new account / first-ever run).
_MASS_REFRESH_WARM_LOOKBACK_H = 72


def _norm_targets(raw: object) -> list[dict]:
    """Normalize payload `targets` into per-chat / mass dicts. A target with a
    `queue_id` is a mass unsend; one with a `message_id` is per-chat. Anything
    else is skipped (logged, never fatal)."""
    out: list[dict] = []
    if not isinstance(raw, list):
        return out
    for t in raw:
        if not isinstance(t, dict):
            continue
        # Coerce per-target so ONE non-numeric payload id (queue_id="q-789",
        # etc.) skips that target instead of aborting the whole run.
        try:
            if t.get("post_id") is not None:
                out.append({
                    "kind": "post",
                    "post_id": int(t["post_id"]),
                    # local row id, so we can flip OUR posts row even if the
                    # of_post_id <-> posts.of_post_id lookup ever drifts.
                    "local_post_id": (
                        int(t["local_post_id"]) if t.get("local_post_id") is not None else None
                    ),
                })
            elif t.get("story_id") is not None:
                out.append({
                    "kind": "story",
                    "story_id": int(t["story_id"]),
                })
            elif isinstance(t.get("hide_upload"), dict):
                # Hide a fresh vault item by the md5+size of the bytes that were
                # uploaded to create it (auto_stories dupe-cleanup). `exclude_ids`
                # are vault ids we must NEVER hide (the source item), guarding
                # against a hash collision resolving back to the original.
                hu = t["hide_upload"]
                md5 = hu.get("md5")
                size = hu.get("size")
                if md5 and size is not None:
                    out.append({
                        "kind": "hide_upload",
                        "md5": str(md5),
                        "size": int(size),
                        "exclude_ids": [int(x) for x in (hu.get("exclude_ids") or [])],
                    })
                else:
                    log.warning("unsend_target_bad_hide target=%r — skipped", t)
            elif t.get("queue_id") is not None:
                out.append({
                    "kind": "mass",
                    "queue_id": int(t["queue_id"]),
                    "mass_run_id": (
                        int(t["mass_run_id"]) if t.get("mass_run_id") is not None else None
                    ),
                })
            elif t.get("message_id") is not None and t.get("fan_id") is not None:
                out.append({
                    "kind": "chat",
                    "message_id": int(t["message_id"]),
                    "fan_id": int(t["fan_id"]),
                })
            else:
                log.warning("unsend_target_unroutable target=%r", t)
        except (TypeError, ValueError):
            log.warning("unsend_target_bad_id target=%r — skipped", t)
    return out


def _pos_hours(raw: object, default: float | None) -> float | None:
    """A policy hour-window value → positive float, else `default`. Used so a
    missing/zero/garbage `text_hours`/`media_hours` falls back cleanly instead of
    selecting everything (or nothing) by accident."""
    if isinstance(raw, (int, float)) and raw > 0:
        return float(raw)
    return default


async def _sweep_targets(account_id: str, policy: dict) -> list[dict]:
    """Policy sweep over our OWN `messages`: outbound bubbles inside the 24h edit
    window → per-chat unsend targets.

      • text-only (`media_count == 0`) older than `text_hours` — always swept.
      • media (`media_count > 0`) older than `media_hours` — swept ONLY when the
        operator sets `policy.media_hours` (revives V1's media auto-unsend).

    Scoped to the achievable per-chat path on purpose (see module docstring):
    media beyond the 24h window / mass messages need an explicit `queue_id`.

    MASS-broadcast rows (`mass_run_id` set) are excluded here: their per-recipient
    bubbles carry synthetic optimistic ids (the 5e15-band placeholders) that 400 on
    the per-chat endpoint, and a real mass blast must be unsent via the forever-window
    queue endpoint (the `_sweep_mass_targets` path), not one recipient at a time."""
    text_hours = _pos_hours(policy.get("text_hours"), _DEFAULT_TEXT_HOURS)
    media_hours = _pos_hours(policy.get("media_hours"), None)  # None ⇒ skip media
    now = datetime.utcnow()
    window_floor = now - timedelta(hours=_PER_CHAT_WINDOW_H)  # must still be unsendable

    # Per (text / media) age class, the bubble must predate its own threshold AND
    # still sit inside the 24h per-chat window. Media is an opt-in second class.
    media_clause = False
    if media_hours is not None:
        media_clause = and_(
            Message.media_count > 0,
            Message.created_at <= now - timedelta(hours=media_hours),
        )
    text_clause = and_(
        Message.media_count == 0,
        Message.created_at <= now - timedelta(hours=text_hours),
    )

    async with get_session() as s:
        rows = (
            await s.execute(
                select(Message.fan_id, Message.message_id)
                .where(
                    Message.account_id == str(account_id),
                    Message.direction == "out",
                    Message.is_unsent.is_(False),
                    Message.mass_run_id.is_(None),  # mass blasts → _sweep_mass_targets
                    Message.created_at >= window_floor,
                    or_(text_clause, media_clause),
                )
                .order_by(Message.created_at)
                .limit(_MAX_SWEEP)
            )
        ).all()
    return [
        {"kind": "chat", "fan_id": int(r[0]), "message_id": int(r[1])}
        for r in rows
    ]


async def _sweep_mass_targets(
    account_id: str, *,
    text_hours: float | None = None,
    media_hours: float | None = None,
    price_hours: float | None = None,
) -> list[dict]:
    """Policy sweep over `mass_broadcast_cache` (OF's own /stats/messages/group list)
    for broadcasts OF still lets us unsend, by CLASS — each with its own age gate:

      • text  (free + image-less)  older than `text_hours`   — plain engagement blasts
      • image (has media)          older than `media_hours`  — photo/video sets
      • price (PPV, not free)       older than `price_hours`  — paid content

    A class is swept only when its hours are given; the classes OR together (a paid
    image matches both `image` and `price`, so it goes once either threshold passes —
    deduped by queue_id). The mass-queue endpoint has NO 24h window, so age is the
    only gate. `is_tip` campaigns and already-canceled rows are always skipped.
    Returns mass targets carrying the OF `queue_id` (+ `from_cache` so the run also
    flips the cache row); mass_run_id is unknown from the cache, so the local
    `messages` mirror is best-effort (the OF unsend still hits every recipient)."""
    now = datetime.utcnow()
    classes = []
    if text_hours is not None:
        classes.append(and_(
            MassBroadcastCache.is_free.is_(True),
            MassBroadcastCache.media_count == 0,
            MassBroadcastCache.sent_at <= now - timedelta(hours=text_hours),
        ))
    if media_hours is not None:
        classes.append(and_(
            MassBroadcastCache.media_count > 0,
            MassBroadcastCache.is_free.is_(True),   # FREE images only — a PRICED image is
            MassBroadcastCache.sent_at <= now - timedelta(hours=media_hours),
        ))                                          # governed by the price class (24h floor),
    if price_hours is not None:                     # so the media window can't unsend a PPV early
        classes.append(and_(
            MassBroadcastCache.is_free.is_(False),
            MassBroadcastCache.sent_at <= now - timedelta(hours=price_hours),
        ))
    if not classes:
        return []
    async with get_session() as s:
        rows = (
            await s.execute(
                select(MassBroadcastCache.queue_id, MassRun.id)
                .select_from(MassBroadcastCache)
                # Recover OUR mass_run_id from the run's recorded OF queue_id so
                # the post-unsend `_flip_mass_unsent` can mirror is_unsent onto
                # the per-recipient placeholder rows (attribution's 5e15 band).
                # Without it those rows stayed "sent" locally forever, and the
                # chat pane's DB seed kept resurrecting long-unsent broadcasts.
                .outerjoin(MassRun, and_(
                    MassRun.account_id == MassBroadcastCache.account_id,
                    MassRun.queue_id == MassBroadcastCache.queue_id,
                ))
                .where(
                    MassBroadcastCache.account_id == str(account_id),
                    MassBroadcastCache.sent_at.is_not(None),
                    MassBroadcastCache.is_tip.is_(False),
                    MassBroadcastCache.is_canceled.is_(False),
                    MassBroadcastCache.can_unsend.is_(True),
                    or_(*classes),
                )
                .order_by(MassBroadcastCache.sent_at)
                .limit(_MAX_SWEEP)
            )
        ).all()
    out: list[dict] = []
    seen_q: set[int] = set()
    for qid_raw, run_id in rows:
        qid = int(qid_raw)
        if qid in seen_q:
            continue  # dup runs on one queue_id — one OF unsend is enough
        seen_q.add(qid)
        out.append({
            "kind": "mass", "queue_id": qid,
            "mass_run_id": int(run_id) if run_id is not None else None,
            "from_cache": True,
        })
    return out


def _parse_item_date(raw: object) -> datetime | None:
    """OF broadcast item `date` ('2026-07-04T15:42:29+00:00') → naive UTC, or None."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        d = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if d.tzinfo is not None:
        d = (d - d.utcoffset()).replace(tzinfo=None)
    return d


async def _refresh_mass_cache(account_id: str, client) -> bool:
    """Write-through OF's broadcast history into `mass_broadcast_cache` so the mass
    sweep that follows reads a current list (see `_MASS_REFRESH_*`). Mirrors the
    UI's /admin/mass-messages/refresh, but PAGINATES so an account that blasts
    faster than one page won't leave its older broadcasts un-swept.

    ⚠️ OF IGNORES `offset` on /users/me/stats/messages/group — verified live
    2026-07-04: offset=0/100/300 all return the identical newest page, with
    `hasMore: true` forever. A pager striding by offset alone re-reads the same
    page to the 50-page cap (× ~10s/page = the 500-850s unsend runs that starved
    the relay's thread pool). `endDate` IS honored (narrowed windows return
    strictly older items, zero overlap), so pagination works like this: stride by
    offset while pages yield NEW queue ids (keeps well-behaved servers / test
    fakes on the cheap path), and the first time a page repeats itself, switch
    permanently to date-window stepping — endDate ← oldest seen date − 1s,
    offset reset to 0. Two fruitless pages in a row end the run.

    Steady-state runs also narrow the pull to `_MASS_REFRESH_WARM_LOOKBACK_H`
    when the account already has cache rows — see that constant's comment.

    Best-effort by design: a client without `mass_message_history` (test fakes)
    skips cleanly. Returns True on success OR a clean skip; False only when the OF
    pull actually errored — so the caller can flag that the run swept a STALE cache
    instead of reporting a misleading 'nothing to do'. `upsert_many` keeps
    `is_canceled` monotonic, so a broadcast unsent out of band drops out of the
    next sweep instead of erroring on a re-cancel."""
    import mass_broadcast_cache as cache  # local: matches server.py; avoids import cycle
    fetch = getattr(client, "mass_message_history", None)
    if not callable(fetch):
        return True  # client can't pull history → sweep the cached rows as-is
    async with get_session() as s:
        cached_rows = (
            await s.execute(
                select(func.count())
                .select_from(MassBroadcastCache)
                .where(MassBroadcastCache.account_id == str(account_id))
            )
        ).scalar() or 0
    end = datetime.utcnow()
    lookback = (
        timedelta(hours=_MASS_REFRESH_WARM_LOOKBACK_H)
        if cached_rows
        else timedelta(days=_MASS_REFRESH_LOOKBACK_DAYS)
    )
    start = end - lookback
    fmt = "%Y-%m-%d %H:%M:%S"
    written = 0
    offset = 0  # advance by ROWS RETURNED, not page index — OF may cap a page
    #             below the requested limit, and striding by the limit would skip
    #             the un-returned rows of a short page.
    end_cursor = end
    seen_qids: set[int] = set()
    date_mode = False  # flips on the first repeated page → offset is ignored
    fruitless = 0      # consecutive pages that yielded zero NEW rows
    try:
        for _ in range(_MASS_REFRESH_MAX_PAGES):
            resp = await asyncio.to_thread(
                fetch, start_date=start.strftime(fmt),
                end_date=end_cursor.strftime(fmt),
                limit=_MASS_REFRESH_PAGE, offset=offset,
            )
            items = (resp.get("items") if isinstance(resp, dict) else None) or []
            fresh: list[dict] = []
            for it in items:
                try:
                    qid = int(it.get("id"))
                except (TypeError, ValueError):
                    continue
                if qid not in seen_qids:
                    seen_qids.add(qid)
                    fresh.append(it)
            if fresh:
                written += await cache.upsert_many(str(account_id), fresh)
                fruitless = 0
            else:
                fruitless += 1
                if fruitless >= 2:
                    break  # two dead pages — no cursor makes progress anymore
            # Stop at the last page (no more / empty); only keep paging while OF
            # says there's more AND this page actually returned rows.
            if not (isinstance(resp, dict) and resp.get("hasMore") and items):
                break
            if fresh and not date_mode:
                offset += len(items)
                continue
            # Date-window step (entered on the first repeated page, then always).
            date_mode = True
            oldest = None
            for it in items:
                d = _parse_item_date(it.get("date"))
                if d is not None and (oldest is None or d < oldest):
                    oldest = d
            if oldest is None or oldest <= start:
                break  # un-dateable page / window exhausted — nothing older to pull
            nxt = oldest - timedelta(seconds=1)
            if nxt >= end_cursor:
                break  # cursor must strictly shrink or we'd loop on one window
            end_cursor = nxt
            offset = 0
        else:
            # Loop ran the full page cap without OF saying "no more" — flag the
            # truncation rather than silently leaving the oldest blasts un-swept.
            log.warning(
                "mass_cache_refresh_truncated account=%s hit %d-page cap (%d rows) — "
                "older broadcasts may not be swept this run",
                account_id, _MASS_REFRESH_MAX_PAGES, written,
            )
    except Exception:
        log.error("mass_cache_refresh_failed account=%s — sweeping STALE cache",
                  account_id, exc_info=True)
        return False
    return True


async def _flip_mass_unsent(account_id: str, mass_run_id: int | None) -> int:
    """Mark a mass run's BROADCAST rows unsent once OF cancels the queue.
    Without a `mass_run_id` we can't map the OF queue id back to our rows, so we
    flip nothing locally (the OF unsend still happened). Returns rows flipped.
    The funnel-step / purchased-row guards live in `messages.mark_mass_runs_unsent`,
    shared with the relay's manual "unsend from everyone" route."""
    if mass_run_id is None:
        return 0
    return await mark_mass_runs_unsent(account_id, [int(mass_run_id)],
                                       reason=UNSENT_REASON)


async def _flip_cache_canceled(account_id: str, queue_id: int) -> int:
    """Mark a `mass_broadcast_cache` row canceled after OF unsends the broadcast, so
    the /settings Mass-messages tab reflects it without waiting for the next refresh
    (mirrors mass_broadcast_cache.mark_canceled). Returns rows flipped."""
    async with get_session() as s:
        res = await s.execute(
            update(MassBroadcastCache)
            .where(
                MassBroadcastCache.account_id == str(account_id),
                MassBroadcastCache.queue_id == int(queue_id),
                MassBroadcastCache.is_canceled.is_(False),
            )
            # Match the canonical post-cancel shape (mass_broadcast_cache.mark_canceled):
            # a canceled broadcast is no longer unsendable.
            .values(is_canceled=True, can_unsend=False, unsend_seconds=0)
        )
    return res.rowcount or 0


async def _flip_post_deleted(
    account_id: str, of_post_id: int, local_post_id: int | None
) -> int:
    """Mark OUR posts row deleted after OF confirms `DELETE /posts/{id}`. Matches
    by local row id when we have it (auto_posts passes it), else by of_post_id.
    Returns rows flipped (0 if we don't hold the row — OF still deleted it)."""
    now = datetime.utcnow()
    if local_post_id is not None:
        where = and_(
            Post.account_id == str(account_id),
            Post.id == int(local_post_id),
        )
    else:
        where = and_(
            Post.account_id == str(account_id),
            Post.of_post_id == int(of_post_id),
        )
    async with get_session() as s:
        res = await s.execute(
            update(Post)
            .where(where, Post.status != "deleted")
            .values(status="deleted", updated_at=now)
        )
    return res.rowcount or 0


async def _write_action(account_id: str, target: dict, flipped: int) -> None:
    """Audit row — one per unsend. employee_id NULL = attributed to Automation."""
    if target["kind"] == "mass":
        action, target_type, target_id = "unsend_mass", "queue", str(target["queue_id"])
    elif target["kind"] == "post":
        action, target_type, target_id = "delete_post", "post", str(target["post_id"])
    elif target["kind"] == "story":
        action, target_type, target_id = "delete_story", "story", str(target["story_id"])
    elif target["kind"] == "hide_upload":
        action, target_type = "hide_vault_media", "vault_media"
        target_id = str(target.get("hidden_id") or target.get("md5") or "")
    else:
        action, target_type, target_id = "unsend_message", "message", str(target["message_id"])
    async with get_session() as s:
        s.add(Action(
            employee_id=None,
            account_id=str(account_id),
            action=action,
            target_type=target_type,
            target_id=target_id,
            payload_json=json.dumps({**target, "rows_flipped": flipped}, default=str),
        ))


@register("unsend_messages")
async def run(account_id: str, payload: dict, *, run_id: int) -> dict:
    """Unsend targeted messages via the OF API and mirror the result locally.

    Each target is unsent through the window-appropriate endpoint, then its
    `messages.is_unsent` is flipped and an `actions` row recorded. One failing
    target is logged and skipped — it never fails the whole run."""
    payload = payload or {}
    targets = _norm_targets(payload.get("targets"))
    client = None              # built once, lazily, and reused by refresh + unsend
    mass_refresh_failed = False
    if not targets:
        policy = payload.get("policy") or {}
        # The per-chat sweep and the MASS free-text sweep are independent. A rule
        # that asks ONLY for the mass cleanup (mass_text / mass_text_hours, with no
        # text_hours/media_hours) must NOT also unsend per-chat bubbles — otherwise
        # an "8h mass cleanup" schedule would silently nuke every 9-24h-old per-chat
        # message too. The bare/legacy policy ({} or text/media keys) still sweeps
        # per-chat as before.
        # MASS thresholds by class (None = that class is off). `mass_text` with no
        # hours uses the 8h default. mass_media_hours / mass_price_hours unsend image
        # and PPV broadcasts respectively (the /settings Auto-unsend panel sets these).
        m_text = _pos_hours(policy.get("mass_text_hours"),
                            _DEFAULT_MASS_TEXT_HOURS if policy.get("mass_text") else None)
        m_media = _pos_hours(policy.get("mass_media_hours"), None)
        # PRICED PPV broadcasts get a HARD 24h floor, whatever the operator set. Measured
        # on prod: 44% of mass-PPV PURCHASES and 47% of the REVENUE land >4h after send,
        # and 68% of that late revenue is the fan's FIRST-ever purchase to the account —
        # new money, not whale re-buys. The revenue curve knees at 24h (4h captures 53%,
        # 24h captures 90%, 48h adds ~1pp). A sub-24h price-unsend deletes new-fan
        # conversion; this floor makes that impossible even by misconfiguration.
        m_price = _pos_hours(policy.get("mass_price_hours"), None)
        if m_price is not None and m_price < _MIN_MASS_PRICE_HOURS:
            log.info("unsend mass_price_hours=%.1f floored to %d (a priced PPV lands 47%% "
                     "of its revenue >4h after send)", m_price, _MIN_MASS_PRICE_HOURS)
            m_price = float(_MIN_MASS_PRICE_HOURS)
        wants_mass = any(h is not None for h in (m_text, m_media, m_price))
        wants_per_chat = ("text_hours" in policy or "media_hours" in policy) or not wants_mass
        if wants_per_chat:
            targets += await _sweep_targets(account_id, policy)
        if wants_mass:
            # A real run pulls fresh broadcast history into the cache first; dry_run
            # is a no-OF-write preview, so it reads the cache as the UI last warmed it.
            # (We refresh on EVERY real run rather than gating on the cache's 24h TTL:
            # the hourly sweep acts on broadcasts only a few hours old, so the UI TTL
            # is far too stale to keep them swept on time.)
            if not payload.get("dry_run"):
                client = await asyncio.to_thread(ax._make_client, account_id)
                mass_refresh_failed = not await _refresh_mass_cache(account_id, client)
            targets += await _sweep_mass_targets(
                account_id, text_hours=m_text, media_hours=m_media, price_hours=m_price)

    # Preview seam: resolve targets, then return WITHOUT touching OF or our rows.
    if payload.get("dry_run"):
        return {
            "dry_run": True,
            "targets": len(targets),
            "would_unsend": targets,
            "unsent": 0,
            "failed": 0,
            "rows_flipped": 0,
        }

    if not targets:
        return {"targets": 0, "unsent": 0, "failed": 0, "rows_flipped": 0,
                "mass_refresh_failed": mass_refresh_failed}

    # Reuse the client the mass refresh already built; only construct one here when
    # this run had no mass refresh (per-chat / post / story / explicit targets).
    if client is None:
        client = await asyncio.to_thread(ax._make_client, account_id)

    unsent = 0
    failed = 0
    rows_flipped = 0
    for t in targets:
        try:
            if t["kind"] == "mass":
                await asyncio.to_thread(client.cancel_scheduled, t["queue_id"])
                flipped = await _flip_mass_unsent(account_id, t.get("mass_run_id"))
                if t.get("from_cache"):
                    flipped += await _flip_cache_canceled(account_id, t["queue_id"])
                # #R4: the first mass is gone → stop enrolling NEW funnel repliers
                # off it (the walker keeps going until a buy). Idempotent; covers
                # both the per-target (mass_run_id) and cache-sweep (queue only)
                # paths. Best-effort — never fail an unsend on this.
                try:
                    from audiences import close_funnel_discovery_for_queue
                    await close_funnel_discovery_for_queue(
                        account_id, queue_id=t.get("queue_id"),
                        mass_run_id=t.get("mass_run_id"))
                except Exception:
                    log.warning("close_funnel_discovery failed account=%s queue=%s",
                                account_id, t.get("queue_id"), exc_info=True)
            elif t["kind"] == "post":
                await asyncio.to_thread(client.delete_post, t["post_id"])
                flipped = await _flip_post_deleted(
                    account_id, t["post_id"], t.get("local_post_id")
                )
            elif t["kind"] == "story":
                # Stories have no local mirror table — just delete on OF.
                await asyncio.to_thread(client.delete_story, t["story_id"])
                flipped = 0
            elif t["kind"] == "hide_upload":
                # Resolve the re-uploaded vault item by its source-byte hash,
                # then hide it (OF's "Remove from vault"). A miss = the dupe is
                # already gone / still transcoding, or the hash resolved to an
                # excluded source id — none of which is a failure.
                hit = await asyncio.to_thread(
                    client.vault_media_lookup_hash, t["md5"], t["size"]
                )
                vid = (hit or {}).get("id") or (hit or {}).get("mediaId")
                if vid is not None and int(vid) not in t.get("exclude_ids", []):
                    await asyncio.to_thread(client.hide_vault_media, [int(vid)])
                    t["hidden_id"] = int(vid)
                    flipped = 1
                else:
                    flipped = 0
            else:
                await asyncio.to_thread(
                    client.unsend_message, t["message_id"], t["fan_id"]
                )
                flipped = await mark_unsent(
                    account_id, t["message_id"], fan_id=t["fan_id"],
                    reason=UNSENT_REASON,
                )
            await _write_action(account_id, t, flipped)
            unsent += 1
            rows_flipped += flipped
        except Exception:
            failed += 1
            log.warning(
                "unsend_failed account=%s target=%r", account_id, t, exc_info=True
            )

    return {
        "targets": len(targets),
        "unsent": unsent,
        "failed": failed,
        "rows_flipped": rows_flipped,
        "mass_refresh_failed": mass_refresh_failed,
    }
