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
    #     mass-queue unsend (opt-in; reads mass_broadcast_cache, no 24h window).
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

from sqlalchemy import and_, or_, select, update

import automation_executor as ax  # shared _make_client seam (tests patch ax._make_client)
from automation_registry import register
from db.engine import get_session
from db.models import Action, MassBroadcastCache, Message, Post

log = logging.getLogger("of-relay.automation.unsend")

# Per-chat (DELETE /messages/{id}) only works inside OF's ~24h edit window.
_PER_CHAT_WINDOW_H = 24
# Default policy: unsend outbound text bubbles older than this many hours.
_DEFAULT_TEXT_HOURS = 9
# Default age for the MASS free-text cleanup sweep (a free, image-less broadcast
# is a low-value engagement blast — unsend it once it's done its job).
_DEFAULT_MASS_TEXT_HOURS = 8
# Safety ceiling so a misconfigured sweep can't unsend an entire account.
_MAX_SWEEP = 200


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
    media beyond the 24h window / mass messages need an explicit `queue_id`."""
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
            MassBroadcastCache.sent_at <= now - timedelta(hours=media_hours),
        ))
    if price_hours is not None:
        classes.append(and_(
            MassBroadcastCache.is_free.is_(False),
            MassBroadcastCache.sent_at <= now - timedelta(hours=price_hours),
        ))
    if not classes:
        return []
    async with get_session() as s:
        rows = (
            await s.execute(
                select(MassBroadcastCache.queue_id)
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
    return [
        {"kind": "mass", "queue_id": int(r[0]), "mass_run_id": None, "from_cache": True}
        for r in rows
    ]


async def _flip_chat_unsent(account_id: str, fan_id: int, message_id: int) -> int:
    """Mark one per-chat message unsent in our DB. Returns rows flipped (0 if we
    don't hold the row — OF still unsent it; we just have nothing to mirror)."""
    now = datetime.utcnow()
    async with get_session() as s:
        res = await s.execute(
            update(Message)
            .where(
                Message.account_id == str(account_id),
                Message.fan_id == int(fan_id),
                Message.message_id == int(message_id),
            )
            .values(is_unsent=True, unsent_reason="automation:unsend_messages", unsent_at=now)
        )
    return res.rowcount or 0


async def _flip_mass_unsent(account_id: str, mass_run_id: int | None) -> int:
    """Mark every message of a mass run unsent once OF cancels the broadcast.
    Without a `mass_run_id` we can't map the OF queue id back to our rows, so we
    flip nothing locally (the OF unsend still happened). Returns rows flipped."""
    if mass_run_id is None:
        return 0
    now = datetime.utcnow()
    async with get_session() as s:
        res = await s.execute(
            update(Message)
            .where(
                Message.account_id == str(account_id),
                Message.mass_run_id == int(mass_run_id),
                Message.is_unsent.is_(False),
            )
            .values(is_unsent=True, unsent_reason="automation:unsend_messages", unsent_at=now)
        )
    return res.rowcount or 0


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
            .values(is_canceled=True)
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
        m_price = _pos_hours(policy.get("mass_price_hours"), None)
        wants_mass = any(h is not None for h in (m_text, m_media, m_price))
        wants_per_chat = ("text_hours" in policy or "media_hours" in policy) or not wants_mass
        if wants_per_chat:
            targets += await _sweep_targets(account_id, policy)
        if wants_mass:
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
        return {"targets": 0, "unsent": 0, "failed": 0, "rows_flipped": 0}

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
            elif t["kind"] == "post":
                await asyncio.to_thread(client.delete_post, t["post_id"])
                flipped = await _flip_post_deleted(
                    account_id, t["post_id"], t.get("local_post_id")
                )
            elif t["kind"] == "story":
                # Stories have no local mirror table — just delete on OF.
                await asyncio.to_thread(client.delete_story, t["story_id"])
                flipped = 0
            else:
                await asyncio.to_thread(
                    client.unsend_message, t["message_id"], t["fan_id"]
                )
                flipped = await _flip_chat_unsent(
                    account_id, t["fan_id"], t["message_id"]
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
    }
