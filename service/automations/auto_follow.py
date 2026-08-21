"""service/automations/auto_follow.py — Automation: auto_follow ("Auto-follow / Auto-like").

Trigger OnlyFans' re-engagement by LIKING fans' recent messages (the fan gets a
"X liked your message" push) — or by FOLLOWING fans. A cheap, notification-only
nudge that never sends a DM.

steps_json (per-run knobs):
  action     "like_messages" | "like_posts" | "follow" | "ping"  (default like_messages)
  targets    who to act on — shape depends on action (see _resolve_targets)
  daily_cap  max actions this RUN (enforced per-run; cadence × cap ≈ per day)
  dry_run    plan only, no OF mutation (DEFAULT TRUE — safe)
  quiet_days              ping: fan counts as quiet after N days silent (default 7)
  min_days_between_pings  ping: per-fan notification cooldown (default 14)

Actions:
  • like_messages  REAL. For each target fan, like their latest inbound message
    via of_client.like_message (VERIFIED LIVE — you can only like the OTHER
    party's messages). This is the re-engagement lever.
  • like_posts     REAL. Like each of the post ids in `targets.post_ids` via
    of_client.like_post.
  • follow         REAL. Follow (or RE-follow) each target fan so they get the
    "started following you" push. Default pool is OF's own win-back list
    (recent_expired_subscribers — fans whose sub to me just lapsed).

    ⚠️ MONEY GATE: OF's "subscribe" endpoint PAYS when the target profile has a
    price. Every fan is profile-checked first and only followed when their
    subscribePrice is exactly 0; a missing/unreadable price SKIPS the fan
    (never assume free). A lapsed relationship (subscribedByExpireDate set)
    goes through /resubscribe, a fresh one through /subscribe.
  • ping           REAL. Re-engage fans who STOPPED CHATTING: for each fan
    quiet ≥ quiet_days (default 7) that we currently follow, unfollow +
    immediately re-follow so OnlyFans fires a fresh "started following you"
    push; a quiet fan we don't follow yet just gets followed (their first
    ping). Per-fan cooldown in follow_ping_state (min_days_between_pings,
    default 14) so nobody gets nagged every run. Same money gate as follow.

Both follow and ping run through ONE shared loop (_follow_batch → _gated_follow)
so the money gate, the cap-on-notifications rule, and the cooldown stamping can
never drift apart between the two actions.

Self-registers via @register("auto_follow"); schedule with an automation_rules
row (kind="auto_follow", trigger_json={"every_seconds": N}, steps_json=knobs).
Defaults to OFF + dry_run — nothing acts until an operator enables a rule.
"""
from __future__ import annotations

import asyncio
import logging
from collections import Counter
from datetime import datetime, timedelta

from sqlalchemy import select

import automation_executor as ax
from automation_registry import register
from db.engine import get_session
from db.models import Fan, FollowPingState, Message
from ._common import load_hard_skip_ids

log = logging.getLogger("of-relay.automation.auto_follow")

_DEFAULT_DAILY_CAP = 50
_MAX_DAILY_CAP = 1000
_DEFAULT_RECENT_DAYS = 7
_DEFAULT_QUIET_DAYS = 7
_DEFAULT_PING_GAP_DAYS = 14
_HEADROOM_FACTOR = 5     # oversize a target pool ×N — see _headroom().
# Outcomes that fired a real notification at the fan — they consume the cap
# and start the ping cooldown.
_NOTIFY_OUTCOMES = ("followed", "refollowed", "pinged")


def _headroom(cap: int) -> int:
    """Pool size to fetch for a run capped at `cap`. The per-fan gates
    (paid-profile, ping cooldown, already-liked, hard_skip) drop candidates, so a
    pool sliced at exactly `cap` under-fills every run — oversize it ×
    _HEADROOM_FACTOR and act until `cap` notifications land. Shared by
    follow / ping / like_messages so the factor can't drift between them."""
    return max(cap, 1) * _HEADROOM_FACTOR


def _int_knob(payload: dict, key: str, default: int, *, lo: int, hi: int | None = None,
              zero_default: bool = False) -> int:
    """One clamped-int knob. None/unparseable → default. The default is NOT
    applied to a parseable value, so an explicit 0 stays 0 wherever lo allows
    it (daily_cap=0 is the operator's stop switch, not "unset").

    `zero_default` flips that for the day-count knobs (quiet_days,
    min_days_between_pings) where 0 is not a meaningful setting: a value ≤ 0
    means "use the house default", matching the pre-refactor `int(x or DEFAULT)`.
    Leave it False anywhere 0 is a real, distinct value."""
    raw = payload.get(key)
    try:
        v = default if raw is None else int(raw)
    except (TypeError, ValueError):
        v = default
    if zero_default and v <= 0:
        v = default
    if hi is not None:
        v = min(v, hi)
    return max(lo, v)


# ── Target pools ──────────────────────────────────────────────────────

async def _recent_active_fan_ids(account_id: str, days: int, limit: int) -> list[int]:
    """Fan ids who sent us an inbound message in the last `days` — newest
    activity first. The default target pool for like_messages."""
    cutoff = datetime.utcnow() - timedelta(days=max(1, days))
    async with get_session() as s:
        rows = (await s.execute(
            select(Message.fan_id, Message.created_at)
            .where(Message.account_id == str(account_id),
                   Message.direction == "in",
                   Message.is_unsent.is_(False),
                   Message.created_at >= cutoff)
            .order_by(Message.created_at.desc())
        )).all()
    seen: set[int] = set()
    out: list[int] = []
    for fid, _ in rows:
        fid = int(fid)
        if fid in seen:
            continue
        seen.add(fid)
        out.append(fid)
        if len(out) >= limit:
            break
    return out


async def _quiet_fan_ids(account_id: str, quiet_days: int, limit: int) -> list[int]:
    """Fans who HAVE chatted before but whose last inbound is ≥ quiet_days old —
    the "stopped chatting" pool for the ping action. Freshest lapse first, so
    the fans most recently worth winning back ping before long-dead ones.
    Reads the cached fans.last_message_received_at (rebuildable from messages)
    instead of a Message group-by — same truth, no full-table scan."""
    cutoff = datetime.utcnow() - timedelta(days=max(1, quiet_days))
    async with get_session() as s:
        rows = (await s.execute(
            select(Fan.fan_id)
            .where(Fan.account_id == str(account_id),
                   Fan.last_message_received_at.is_not(None),
                   Fan.last_message_received_at < cutoff)
            .order_by(Fan.last_message_received_at.desc())
            .limit(limit)
        )).all()
    return [int(r[0]) for r in rows]


async def _recent_expired_fan_ids(client) -> list[int]:
    """Fan ids from OF's own win-back pool (recent_expired_subscribers,
    verified live). The payload is either {"list":[user,...]} or a bare list —
    take ids defensively and drop anything unreadable."""
    res = await asyncio.to_thread(client.recent_expired_subscribers)
    rows = res.get("list") if isinstance(res, dict) else res
    out: list[int] = []
    for u in rows if isinstance(rows, list) else []:
        try:
            out.append(int(u["id"] if isinstance(u, dict) else u))
        except (KeyError, TypeError, ValueError):
            continue
    return out


async def _resolve_smart_list(smart_list_id: int, account_id: str) -> list[int]:
    """Expand a saved Smart List to its current fan-id set. Lazy import so the
    executor's plugin scan never pulls the FastAPI/pydantic router module.

    Scoped to `account_id`: a list id is a bare integer in steps_json, so a typo
    (or a list deleted and its id reused) would otherwise resolve ANOTHER
    creator's segment and spend this account's like budget on fans it has no
    relationship with.
    """
    from smart_lists_api import _load_normalised_fans, _rules_of, resolve
    from db.models import SmartList
    async with get_session() as s:
        row = await s.get(SmartList, int(smart_list_id))
        if row is None:
            return []
        if str(row.account_id) != str(account_id):
            log.warning("auto_follow smart_list=%s belongs to account=%s, not %s — ignoring",
                        smart_list_id, row.account_id, account_id)
            return []
        rules = _rules_of(row)
    fans = await _load_normalised_fans(account_id, rules)
    return resolve(rules, fans)


async def _resolve_targets(account_id: str, targets: dict, cap: int) -> list[int]:
    """Fan-id list for the message/follow actions. `targets`:
      {"source":"recent_active", "days":7}          (default)
      {"source":"fan_ids", "fan_ids":[...]}
      {"source":"smart_list", "smart_list_id": N}
    """
    targets = targets or {}
    source = str(targets.get("source") or "recent_active")
    if source == "fan_ids":
        raw = targets.get("fan_ids") or []
        return [int(x) for x in raw if str(x).lstrip("-").isdigit()][:cap]
    if source == "smart_list":
        sid = targets.get("smart_list_id")
        if sid is None:
            return []
        return (await _resolve_smart_list(int(sid), account_id))[:cap]
    # default: recently-active fans
    days = int(targets.get("days") or _DEFAULT_RECENT_DAYS)
    return await _recent_active_fan_ids(account_id, days, cap)


# ── The gated follow (shared by follow + ping) ────────────────────────

class _StrandedError(RuntimeError):
    """The ping's unfollow landed but every re-follow attempt failed — the fan
    is left UNFOLLOWED. Callers surface this in its own counter; it must never
    blend into the generic `errors` bucket."""


async def _gated_follow(client, fan_id: int, *, unfollow_first: bool = False) -> str:
    """Price-gate + follow one fan; the single decision path shared by the
    follow and ping actions. Returns the outcome:

      "followed"           fresh follow → notification
      "refollowed"         lapsed relationship re-armed via /resubscribe → notification
      "pinged"             active follow cycled (unfollow + re-follow) → notification
      "already_following"  active follow left alone (unfollow_first=False)
      "paid_profile"       skipped — profile has a price (subscribe would PAY)
      "no_price"           skipped — price unreadable (never assume free)

    Raises _StrandedError when an unfollow can't be undone; any other OF error
    propagates for the caller to count.
    """
    u = await asyncio.to_thread(client.get_user, fan_id)
    u = u if isinstance(u, dict) else {}
    # MONEY GATE — subscribe PAYS unless the profile is free. Only an explicit
    # 0 passes; a missing/unreadable price is a SKIP, not a benefit of the doubt.
    price = u.get("subscribePrice")
    if not isinstance(price, (int, float)):
        return "no_price"
    if float(price) != 0:
        return "paid_profile"
    # subscribedBy = I already follow them (viewer→target edge). If OF ever
    # flips this semantics the failure mode is a skip or a benign "already
    # subscribed" error — never a payment (gated above).
    if u.get("subscribedBy"):
        if not unfollow_first:
            return "already_following"
        await asyncio.to_thread(client.unfollow_user, fan_id)
        # VERIFIED LIVE 2026-07-23 (Ava→jaka): a just-unfollowed free follow
        # re-arms via POST /subscribe — /resubscribe 400s there ("Resubscribe
        # failed.", it only serves EXPIRED relationships). Keep resubscribe as
        # the fallback; we just ended a live follow and must not leave it
        # dangling.
        try:
            await asyncio.to_thread(client.follow_user, fan_id)
        except Exception:
            try:
                await asyncio.to_thread(client.resubscribe_user, fan_id)
            except Exception as e:
                raise _StrandedError(str(e)) from e
        return "pinged"
    if u.get("subscribedByExpireDate"):
        # Lapsed/expired relationship: /resubscribe is the canonical re-arm;
        # fall back to a plain follow (the price gate above already passed).
        try:
            await asyncio.to_thread(client.resubscribe_user, fan_id)
        except Exception:
            await asyncio.to_thread(client.follow_user, fan_id)
        return "refollowed"
    await asyncio.to_thread(client.follow_user, fan_id)
    return "followed"


async def _follow_batch(
    account_id: str, client, pool: list[int], cap: int, *, unfollow_first: bool,
) -> tuple[Counter, int, int]:
    """Run _gated_follow over `pool` until `cap` NOTIFICATIONS have fired
    (gate skips don't consume the cap). Returns (outcome counts, stranded,
    errors). Every notification stamps the ping cooldown, so neither action
    can double-notify a fan the other just reached."""
    counts: Counter = Counter()
    stranded = errors = 0
    label = "ping" if unfollow_first else "follow"
    for fid in pool:
        if sum(counts[o] for o in _NOTIFY_OUTCOMES) >= cap:
            break
        try:
            outcome = await _gated_follow(client, fid, unfollow_first=unfollow_first)
            counts[outcome] += 1
            if outcome in _NOTIFY_OUTCOMES:
                await _stamp_ping(account_id, fid)
        except _StrandedError:
            stranded += 1
            log.error("auto_follow %s STRANDED account=%s fan=%s — unfollowed "
                      "but re-follow failed", label, account_id, fid, exc_info=True)
        except Exception:
            errors += 1
            log.warning("auto_follow %s failed account=%s fan=%s",
                        label, account_id, fid, exc_info=True)
    return counts, stranded, errors


# ── Ping cooldown state ───────────────────────────────────────────────

async def _recently_pinged_ids(account_id: str, gap_days: int) -> set[int]:
    """Fans pinged inside the cooldown window — excluded from this run."""
    cutoff = datetime.utcnow() - timedelta(days=max(1, gap_days))
    async with get_session() as s:
        rows = (await s.execute(
            select(FollowPingState.fan_id)
            .where(FollowPingState.account_id == str(account_id),
                   FollowPingState.last_pinged_at >= cutoff)
        )).all()
    return {int(r[0]) for r in rows}


async def _stamp_ping(account_id: str, fan_id: int) -> None:
    """Record that this fan just got a follow-notification (ping OR first
    follow) so the cooldown covers every notification we caused."""
    now = datetime.utcnow()
    async with get_session() as s:
        row = await s.get(FollowPingState, (str(account_id), int(fan_id)))
        if row is None:
            s.add(FollowPingState(account_id=str(account_id), fan_id=int(fan_id),
                                  last_pinged_at=now, ping_count=1))
        else:
            row.last_pinged_at = now
            row.ping_count = (row.ping_count or 0) + 1


# ── Like helpers ──────────────────────────────────────────────────────

def _favorites_count(post: object) -> int | None:
    """`favoritesCount` off a /posts/{id} payload, or None if unreadable."""
    if not isinstance(post, dict):
        return None
    try:
        return int(post.get("favoritesCount"))
    except (TypeError, ValueError):
        return None


def _is_already_liked_error(exc: Exception) -> bool:
    """OF answers a repeat like with 400 'This message is already liked.'
    That's the steady state of a recurring rule, not a failure."""
    return "already liked" in str(exc).lower()


async def _latest_inbound_message_id(account_id: str, fan_id: int) -> int | None:
    async with get_session() as s:
        row = (await s.execute(
            select(Message.message_id)
            .where(Message.account_id == str(account_id),
                   Message.fan_id == int(fan_id),
                   Message.direction == "in",
                   Message.is_unsent.is_(False))
            .order_by(Message.created_at.desc(), Message.message_id.desc())
            .limit(1)
        )).first()
    return int(row[0]) if row and row[0] is not None else None


# ── Per-action runners (uniform signature, dispatched from run()) ─────

async def _run_follow(account_id: str, payload: dict, *, cap: int, dry_run: bool,
                      targets: dict, hard_skip: set) -> dict:
    client = await asyncio.to_thread(ax._make_client, account_id)
    source = str(targets.get("source") or "expired")
    headroom = _headroom(cap)
    if source == "expired":
        pool = await _recent_expired_fan_ids(client)
    else:
        pool = await _resolve_targets(account_id, targets, headroom)
    pool = [f for f in pool if f not in hard_skip][:headroom]

    if dry_run:
        # Plan only — the expired pool above is a read; no mutation happens.
        # The per-fan price gate runs at send time, so the plan may shrink.
        return {"action": "follow", "dry_run": True, "source": source,
                "candidates": len(pool), "would_follow": pool[:cap],
                "note": "paid profiles are skipped at send time (price gate)"}

    counts, _, errors = await _follow_batch(account_id, client, pool, cap,
                                            unfollow_first=False)
    return {"action": "follow", "dry_run": False, "source": source,
            "candidates": len(pool), "followed": counts["followed"],
            "refollowed": counts["refollowed"],
            "already_following": counts["already_following"],
            "paid_profile_skipped": counts["paid_profile"],
            "no_price_skipped": counts["no_price"],
            "errors": errors, "cap": cap}


async def _run_ping(account_id: str, payload: dict, *, cap: int, dry_run: bool,
                    targets: dict, hard_skip: set) -> dict:
    quiet_days = _int_knob(payload, "quiet_days", _DEFAULT_QUIET_DAYS, lo=1, zero_default=True)
    gap_days = _int_knob(payload, "min_days_between_pings", _DEFAULT_PING_GAP_DAYS, lo=1, zero_default=True)
    pool = await _quiet_fan_ids(account_id, quiet_days, limit=_headroom(cap))
    recently = await _recently_pinged_ids(account_id, gap_days)
    pool = [f for f in pool if f not in hard_skip and f not in recently]

    if dry_run:
        return {"action": "ping", "dry_run": True, "quiet_days": quiet_days,
                "min_days_between_pings": gap_days,
                "candidates": len(pool), "would_ping": pool[:cap],
                "note": "paid profiles are skipped at send time (price gate)"}

    client = await asyncio.to_thread(ax._make_client, account_id)
    counts, stranded, errors = await _follow_batch(account_id, client, pool, cap,
                                                   unfollow_first=True)
    return {"action": "ping", "dry_run": False, "quiet_days": quiet_days,
            "min_days_between_pings": gap_days, "candidates": len(pool),
            "pinged": counts["pinged"],
            "followed": counts["followed"] + counts["refollowed"],
            "paid_profile_skipped": counts["paid_profile"],
            "no_price_skipped": counts["no_price"],
            "stranded": stranded, "errors": errors, "cap": cap}


async def _run_like_posts(account_id: str, payload: dict, *, cap: int, dry_run: bool,
                          targets: dict, hard_skip: set) -> dict:
    post_ids = [int(x) for x in (targets.get("post_ids") or [])
                if str(x).lstrip("-").isdigit()][:cap]
    if not post_ids:
        return {"action": "like_posts", "dry_run": dry_run, "liked": 0,
                "candidates": 0, "skipped": "no_post_ids"}
    if dry_run:
        return {"action": "like_posts", "dry_run": True, "candidates": len(post_ids),
                "would_like": post_ids}
    client = await asyncio.to_thread(ax._make_client, account_id)
    liked = errors = already = 0
    for pid in post_ids:
        try:
            # OF's /favorites endpoint is a TOGGLE, not "set liked", and the
            # post payload carries no per-viewer like flag (only
            # favoritesCount / canToggleFavorite) — so we cannot ask "have I
            # liked this?". Running the rule twice therefore UNLIKED the post
            # while still reporting liked:1, and a scheduled rule alternated
            # like/unlike forever.
            #
            # Detect the direction from the count and undo an accidental
            # unlike. Costs two extra reads per post, but it only ever
            # settles on "liked", and it uses no unverified endpoint.
            before = _favorites_count(await asyncio.to_thread(client.get_post, pid))
            await asyncio.to_thread(client.like_post, pid)
            after = _favorites_count(await asyncio.to_thread(client.get_post, pid))
            if before is not None and after is not None and after < before:
                # We just removed our own like — put it back.
                await asyncio.to_thread(client.like_post, pid)
                already += 1
            else:
                liked += 1
        except Exception:
            errors += 1
            log.warning("auto_follow like_post failed account=%s post=%s",
                        account_id, pid, exc_info=True)
    return {"action": "like_posts", "dry_run": False, "candidates": len(post_ids),
            "liked": liked, "already_liked": already, "errors": errors}


async def _run_like_messages(account_id: str, payload: dict, *, cap: int, dry_run: bool,
                             targets: dict, hard_skip: set) -> dict:
    headroom = _headroom(cap)
    pool = [f for f in await _resolve_targets(account_id, targets, headroom)
            if f not in hard_skip]
    if not pool:
        return {"action": "like_messages", "dry_run": dry_run, "candidates": 0, "liked": 0}

    if dry_run:
        return {"action": "like_messages", "dry_run": True,
                "candidates": len(pool), "would_like_fans": pool[:cap]}

    client = await asyncio.to_thread(ax._make_client, account_id)
    liked = errors = no_message = already = 0
    for fid in pool:
        # already-liked consumes the cap too: it's the steady state of a
        # recurring rule (same latest message until the fan writes again) —
        # without this, a headroomed pool would burn 5×cap OF calls re-liking
        # the same messages every run.
        if liked + already >= cap:
            break
        mid = await _latest_inbound_message_id(account_id, fid)
        if mid is None:
            no_message += 1
            continue
        try:
            await asyncio.to_thread(client.like_message, mid)
            liked += 1
        except Exception as e:
            # OF 400s a repeat like; counting that as an error made a healthy
            # steady state read as total failure.
            if _is_already_liked_error(e):
                already += 1
                continue
            errors += 1
            log.warning("auto_follow like_message failed account=%s fan=%s msg=%s",
                        account_id, fid, mid, exc_info=True)
    return {"action": "like_messages", "dry_run": False, "candidates": len(pool),
            "liked": liked, "no_message": no_message, "already_liked": already,
            "errors": errors, "cap": cap}


# Dispatch table IS the action registry — validation is dict membership.
_ACTIONS = {
    "like_messages": _run_like_messages,
    "like_posts": _run_like_posts,
    "follow": _run_follow,
    "ping": _run_ping,
}


@register("auto_follow")
async def run(account_id: str, payload: dict, *, run_id: int) -> dict:
    payload = payload or {}
    action = str(payload.get("action") or "like_messages")
    runner = _ACTIONS.get(action)
    if runner is None:
        return {"skipped": "bad_action", "action": action}
    return await runner(
        account_id, payload,
        cap=_int_knob(payload, "daily_cap", _DEFAULT_DAILY_CAP, lo=0, hi=_MAX_DAILY_CAP),
        dry_run=bool(payload.get("dry_run", True)),   # default SAFE
        targets=payload.get("targets") or {},
        hard_skip=await load_hard_skip_ids(account_id),
    )
