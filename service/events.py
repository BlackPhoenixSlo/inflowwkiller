"""
SSE broadcaster + event_inbox writer.

This module sits BETWEEN the existing `_broadcast_event` in server.py and
the new browser app. Two responsibilities:

  1. Every event that flows through the OF-WS pump → write a row to
     `event_inbox` (idempotent on `provider_event_id` when present). This
     gives us a durable, queryable record of every OF event for replay,
     debugging, and the phase B canonical-table writer.

  2. Maintain a pool of SSE-style asyncio queues keyed by `scope`. When a
     subscriber connects to `GET /events?scope=...`, they get a queue;
     when an event arrives, it's pushed to every matching queue. Scopes:
       • "all"               — every event from every account
       • "model:<account_id>" — only that account's events

The original `_broadcast_event` already handles browser WebSocket
delivery via `_event_subscribers`. We add SSE in parallel because:
  • SSE is one-way + native to fetch — simpler client code.
  • SSE survives proxies/CDNs that mangle WebSocket upgrades.
  • EventSource + Last-Event-ID gives reliable replay-on-reconnect that
    we'd otherwise hand-roll on top of WS.

Both transports stay live during the migration; phase B picks one to keep.

API:
    register_with(server_broadcast_fn):
        Wire this module's `handle_event` into the relay's existing
        broadcast pipeline. Called once at startup.

    subscribe(scope) -> Queue:
        Get an asyncio.Queue that yields every matching event.
        Caller MUST also call `unsubscribe(scope, queue)` on disconnect.

    sse_stream(request, scope) -> AsyncIterator[bytes]:
        Convenience generator for FastAPI's StreamingResponse. Handles
        the queue lifecycle, heartbeats, and graceful shutdown.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import AsyncIterator, Awaitable, Callable

from fastapi import Request

from db.repo import insert_event

log = logging.getLogger("of-relay.events")

# Maximum queued events per subscriber. Tuned for "bursty inbound + slow
# browser" — at 256, a 100 events/sec spike rides through without drops,
# but a wedged tab (laptop sleeping) gets evicted within seconds rather
# than ballooning memory.
_QUEUE_MAX = 256

# Heartbeat interval. Two pings fly on this cadence:
#   1. `: ping\n\n` — SSE comment, keeps proxies/CDNs from idle-closing
#      the TCP socket (their ~60 s threshold).
#   2. `event: ping\ndata: {...}\n\n` — named event the browser bumps
#      `lastEventAt` on, so the frontend's `isConnected()` can degrade
#      when the upstream OF-WS pump stalls but the socket stays open.
# 20 s gives the frontend two missed pings before its 45 s STALE_MS
# boundary trips — robust without being slow.
_HEARTBEAT_S = 20

# Subscriber registry: scope → set of queues.
_subs: dict[str, set[asyncio.Queue]] = {}
_subs_lock = asyncio.Lock()


# ── Persist worker (bounded backpressure) ────────────────────────────
#
# SQLite is single-writer, so spawning one `asyncio.create_task` per event
# for the event_inbox insert AND the transcoder upserts means under a burst
# the pending-task list can balloon faster than the WAL can drain. That's
# the actual failure mode at high WS ingest rates — not the DB itself, but
# unbounded create_task piling up.
#
# Solution: one bounded queue, one consumer task that drains serially.
# Producers (handle_event) push and never block; on overflow we drop the
# oldest item so freshness wins over completeness. SSE fan-out stays inline
# below so realtime UI is unaffected — only DB writes get queued.
_PERSIST_QUEUE_MAX = 2000
_persist_queue: asyncio.Queue | None = None
_persist_worker_task: asyncio.Task | None = None
_persist_drops = 0


# ── Subscriber lifecycle ─────────────────────────────────────────────

async def subscribe(scope: str) -> asyncio.Queue:
    """Return a fresh queue subscribed to `scope`. Caller MUST unsubscribe.

    Scope grammar:
      • "all"               — every event
      • "model:<account_id>" — events tagged with that __account_id

    Unknown scopes are accepted (registered but never matched), so the
    browser doesn't 500 on a typo — it just sees no traffic and reconnects.
    """
    q: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_MAX)
    async with _subs_lock:
        _subs.setdefault(scope, set()).add(q)
    return q


async def unsubscribe(scope: str, q: asyncio.Queue) -> None:
    async with _subs_lock:
        bucket = _subs.get(scope)
        if bucket:
            bucket.discard(q)
            if not bucket:
                _subs.pop(scope, None)


def subscriber_count(scope: str | None = None) -> int:
    """For health / debug. Not async — reads are safe without the lock."""
    if scope is None:
        return sum(len(qs) for qs in _subs.values())
    return len(_subs.get(scope, set()))


# ── Event ingest ────────────────────────────────────────────────────

async def handle_event(event: dict) -> None:
    """Called by server.py's `_broadcast_event` for every OF event.

    Responsibilities:
      1. Persist to event_inbox (async fire-and-forget — never blocks the
         WS pump on a slow SQL write).
      2. Push to every matching subscriber queue.

    The event dict carries the relay's `__account_id` / `__account_name`
    /  `__account_color` sentinel keys plus the OF-native top-level event
    name(s). We strip `__`-prefixed keys before persisting (they're our
    metadata, not OF's), but they stay in the SSE payload so the browser
    knows which model an event belongs to.
    """
    account_id = event.get("__account_id") if isinstance(event, dict) else None
    # Pick the first non-sentinel key as the event_type (OF puts the type
    # at the top level — e.g. `{"newMessage": {...}}`).
    event_type = _first_real_key(event)

    # Strip sentinels for the persisted payload — it's already inferable
    # from `account_id`, no need to duplicate.
    persistable = {k: v for k, v in event.items() if not k.startswith("__")} if isinstance(event, dict) else {}

    # Enqueue for the persist worker. Bounded queue gives backpressure;
    # on overflow we drop the oldest so a slow disk loses history, not
    # the freshest events. Before the worker is started (race at startup
    # between the first event and `start_persist_worker`), we fall back
    # to the old fire-and-forget create_task path so we don't lose data.
    global _persist_drops
    if _persist_queue is not None:
        item = (account_id, event_type, persistable, event)
        try:
            _persist_queue.put_nowait(item)
        except asyncio.QueueFull:
            _persist_drops += 1
            with _suppress_queue_empty(_persist_queue):
                _persist_queue.get_nowait()
            try:
                _persist_queue.put_nowait(item)
            except Exception:
                pass
            if _persist_drops % 100 == 1:
                log.warning(
                    "persist queue at capacity — %d events displaced so far "
                    "(SQLite writer can't keep up; check disk / batch size)",
                    _persist_drops,
                )
    else:
        # Pre-startup path: keep the old behavior so no event is lost.
        from event_transcoder import transcode as _transcode  # local import to avoid cycles
        asyncio.create_task(_persist_event_safe(
            account_id=account_id,
            event_type=event_type or "unknown",
            payload=persistable,
        ))
        asyncio.create_task(_transcode(event))

    # Fan out to subscribers — scope "all" + scope-by-account.
    await broadcast(event)


async def broadcast(event: dict) -> None:
    """Fan ONE event out to every matching SSE subscriber queue — and nothing
    else (no event_inbox persist, no transcode). `handle_event` uses this for
    OF-WS events after it persists; `publish_db_message` uses it directly for
    a worker-written row that's ALREADY in the DB (re-persisting/transcoding
    would double-write the canonical message row).

    Scope routing mirrors `handle_event`: "all" + "model:<__account_id>".
    """
    account_id = event.get("__account_id") if isinstance(event, dict) else None
    targets: list[asyncio.Queue] = []
    async with _subs_lock:
        targets.extend(_subs.get("all", set()))
        if account_id:
            targets.extend(_subs.get(f"model:{account_id}", set()))

    for q in targets:
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            # Drop oldest, push new — slow tab loses history, not freshness.
            with _suppress_queue_empty(q):
                q.get_nowait()
            try:
                q.put_nowait(event)
            except Exception:
                # Truly dead — let the SSE generator notice on its next get().
                pass


async def publish_db_message(
    *,
    account_id: str,
    fan_id: int,
    message_id: int,
    body: str = "",
    created_at: object | None = None,
    price_cents: int = 0,
    media: list | None = None,
    media_count: int = 0,
    sender_name: str = "",
) -> None:
    """WORKER→SSE BRIDGE. Emit a synthetic `api2_chat_message` event for an
    OUTBOUND `messages` row a background worker just wrote, so an open chat
    updates live instead of only on the next refetch.

    Why this is needed: the OF-WS pump deliberately skips outbound events, so a
    worker write (welcome / AI reply / follow-up / mass / funnel) has no live
    path. We mint the same `api2_chat_message` envelope the frontend already
    consumes — BUT OF's native outbound event omits the recipient, so we also
    carry `__fan_id` (the open chat the patcher must target). `fromUser.id`
    equals `account_id` (the creator's OF user id), which is how the frontend
    marks the bubble outbound (event_transcoder §direction / useInboxRealtime).

    Fan-out ONLY — the row is already persisted; this never touches the DB.
    Best-effort: a broadcast hiccup must never fail the worker's send.
    """
    try:
        try:
            of_uid: object = int(account_id)
        except (TypeError, ValueError):
            of_uid = account_id
        created = created_at.isoformat() if hasattr(created_at, "isoformat") else (created_at or None)
        event = {
            "api2_chat_message": {
                "id": int(message_id),
                "text": body or "",
                "fromUser": {"id": of_uid, "name": sender_name or ""},
                "toUser": {"id": int(fan_id)},
                "createdAt": created,
                "mediaCount": int(media_count or 0),
                "media": media or [],
                "price": (price_cents or 0) / 100.0,
            },
            "__account_id": str(account_id),
            "__fan_id": int(fan_id),  # OF omits the recipient on outbound — carry it
        }
        await broadcast(event)
    except Exception:
        log.exception("publish_db_message failed (account=%s fan=%s msg=%s)",
                      account_id, fan_id, message_id)


async def _persist_event_safe(
    *, account_id: str | None, event_type: str, payload: dict,
) -> None:
    """Wraps insert_event so a bad payload can't take down the broadcaster."""
    try:
        # OF events sometimes carry an `id` field at top level — use it as
        # the dedup key when present so re-deliveries (reconnect replays)
        # don't double-write.
        provider_id = None
        if isinstance(payload, dict):
            for k in ("id", "messageId", "eventId"):
                v = payload.get(k)
                if v is not None:
                    provider_id = f"{event_type}:{v}"
                    break
        await insert_event(
            account_id=account_id,
            source="of_ws",
            event_type=event_type,
            payload=payload,
            provider_event_id=provider_id,
        )
    except Exception:
        log.exception("persist_event_safe failed (type=%s)", event_type)


async def _persist_worker_loop() -> None:
    """Singleton consumer that drains `_persist_queue` serially.

    Runs `insert_event` then `transcode` for each item, sequentially. This
    matches SQLite's single-writer reality — fanning out via create_task
    just makes them fight over `busy_timeout`. The serial loop also gives
    us a free hot path for adding write batching later (group N items into
    one commit) without changing the producer side.
    """
    assert _persist_queue is not None
    from event_transcoder import transcode as _transcode  # local import to avoid cycles
    while True:
        try:
            account_id, event_type, persistable, event = await _persist_queue.get()
        except asyncio.CancelledError:
            return
        try:
            await _persist_event_safe(
                account_id=account_id,
                event_type=event_type or "unknown",
                payload=persistable,
            )
        except Exception:
            log.exception("persist worker: _persist_event_safe failed")
        try:
            await _transcode(event)
        except Exception:
            log.exception("persist worker: transcode failed")


def start_persist_worker() -> None:
    """Start the singleton persist worker. Idempotent.

    Must be called from inside the running event loop (e.g. FastAPI's
    `@app.on_event("startup")`), after `init_db()` but before any WS pump
    spawns — otherwise the first burst of events takes the pre-startup
    fallback path in `handle_event`.
    """
    global _persist_queue, _persist_worker_task
    if _persist_worker_task is not None and not _persist_worker_task.done():
        return
    _persist_queue = asyncio.Queue(maxsize=_PERSIST_QUEUE_MAX)
    _persist_worker_task = asyncio.create_task(
        _persist_worker_loop(), name="event-persist-worker",
    )
    log.info("persist worker started (queue maxsize=%d)", _PERSIST_QUEUE_MAX)


async def stop_persist_worker() -> None:
    """Cancel the worker. Pending items in the queue are lost — call from
    shutdown only. A graceful drain would await `_persist_queue.join()`
    first, but in our shutdown flow we accept the loss because the OF
    pumps are already cancelled and there's no live producer."""
    global _persist_worker_task
    if _persist_worker_task is None:
        return
    _persist_worker_task.cancel()
    try:
        await _persist_worker_task
    except (asyncio.CancelledError, Exception):
        pass
    _persist_worker_task = None


def persist_stats() -> dict[str, int]:
    """For /health-style introspection. Cheap to call."""
    return {
        "queue_size": _persist_queue.qsize() if _persist_queue is not None else 0,
        "queue_max": _PERSIST_QUEUE_MAX,
        "displaced": _persist_drops,
        "worker_alive": int(
            _persist_worker_task is not None and not _persist_worker_task.done()
        ),
    }


def _first_real_key(event: dict | object) -> str | None:
    """Pick the first top-level key that isn't a `__`-prefixed sentinel."""
    if not isinstance(event, dict):
        return None
    for k in event:
        if not k.startswith("__"):
            return k
    return None


class _suppress_queue_empty:
    """Context manager: swallow asyncio.QueueEmpty. Tiny helper to keep
    the producer side branch-free."""
    def __init__(self, q: asyncio.Queue):
        self.q = q
    def __enter__(self): return self
    def __exit__(self, exc_type, exc, tb):
        return exc_type is asyncio.QueueEmpty


# ── SSE generator (FastAPI StreamingResponse body) ──────────────────

async def sse_stream(request: Request, scope: str) -> AsyncIterator[bytes]:
    """Yield SSE-formatted bytes for one subscriber. Handles:
      • Initial `: connected` comment so the browser's onopen fires.
      • Heartbeat comments every _HEARTBEAT_S seconds.
      • JSON-encoded events with `event:` line set to the OF event type.
      • Last-Event-ID resumption: if the client sends that header, we
        push event_inbox rows that came after that id before live ones
        (phase B; phase A skips this branch and starts from "now").
      • Graceful unsubscribe when the client disconnects.

    SSE wire format (one frame):
        id: <event_inbox.id>\\n
        event: <event_type>\\n
        data: <json>\\n
        \\n
    """
    q = await subscribe(scope)
    try:
        yield b": connected\n\n"
        last_beat = time.monotonic()
        while True:
            if await request.is_disconnected():
                return
            try:
                event = await asyncio.wait_for(q.get(), timeout=1.0)
            except asyncio.TimeoutError:
                if time.monotonic() - last_beat >= _HEARTBEAT_S:
                    # `: ping` keeps proxies' idle timers warm; the named
                    # `event: ping` lets the browser bump its lastEventAt
                    # so isConnected() degrades when the OF-WS pump stalls
                    # while the SSE socket itself stays open. Intentionally
                    # not logged — fires every 20 s per subscriber.
                    ts = json.dumps({"ts": time.time()})
                    yield (
                        b": ping\n\n"
                        + f"event: ping\ndata: {ts}\n\n".encode("utf-8")
                    )
                    last_beat = time.monotonic()
                continue

            event_type = _first_real_key(event) or "event"
            # SSE spec: each line is "key: value\n"; frame ends with "\n\n".
            # We never use the `id:` line yet — that's wired in phase B
            # alongside Last-Event-ID replay.
            try:
                data = json.dumps(event, default=str)
            except Exception:
                data = json.dumps({"error": "serialize_failed", "type": event_type})
            yield f"event: {event_type}\ndata: {data}\n\n".encode("utf-8")
            last_beat = time.monotonic()
    finally:
        await unsubscribe(scope, q)


# ── Hook installation ───────────────────────────────────────────────

def register_with(
    install: Callable[[Callable[[dict], Awaitable[None]]], None],
) -> None:
    """Called once at startup. Hands `handle_event` to the caller's
    install hook so the relay's existing `_broadcast_event` can fan
    events into us in addition to the legacy WS subscribers.

    Decoupled like this so this module has zero imports from server.py
    — server.py wires the connection at startup.
    """
    install(handle_event)
