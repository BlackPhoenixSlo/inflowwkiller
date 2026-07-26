"""
Event transcoder — turns raw OF WebSocket events into canonical SQL rows.

Pattern: `service/events.py:handle_event` calls `transcode(event)` for every
event after the event_inbox write. Recognized events get upserted to
`messages` / `fans` / `chats` / `transactions`. Unknown shapes are logged
(once per event-type-shape) so we can extend over time without losing data.

The most important event by far is **api2_chat_message** — every single DM
flowing through OF's realtime channel. Storing these forever feeds the AI
pipeline:

    fan thread (DB) → Grok → fan_facts + fan_profiles rows

Without persistent message history, every Grok run would have to re-fetch
from OF's API (slow, costly, rate-limited). With it, we can replay,
audit, and re-process at zero marginal cost.

Direction inference:
  • `fromUser.id == account_id` → 'out' (our model sent it)
  • `fromUser.id != account_id` → 'in' (someone else sent it; either our
    fan, OR a creator we follow blasting their mass-message). Both are
    persisted; `fans.source` distinguishes at query time.

Failure mode: every operation is `try/except` + `log.exception` + return.
A bad payload must never break the WS pump or the SSE fan-out.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from db.engine import get_session
from db.models import Chat, Fan, Message, Transaction
from of_shapes import giphy_dm_id, has_video   # shared OF payload-shape readers
from automations._common import source_self_heal_set
import relay_cache
from jsonsafe import dump_capped

# Outbound-echo cache-bust debounce (per account). A mass blast echoes one
# outbound chat_message per recipient; busting list_chats/roster_count on each
# would keep those caches permanently cold for the blast's duration. One bust
# per window suffices — the 60/90s frontend polls are the refetch cadence.
_OUT_BUST_DEBOUNCE_S = 20.0
_last_out_bust: dict[str, float] = {}

log = logging.getLogger("of-relay.transcode")

# One-time "unknown event shape" log: log the first occurrence of each
# (event_type, payload top-level keys) tuple so we see what's coming
# without spamming once we know the shape.
_seen_unknown: set[tuple[str, tuple[str, ...]]] = set()


# ── Helpers ──────────────────────────────────────────────────────────

def _parse_iso(s: str | None) -> datetime | None:
    """OF returns ISO 8601 with `+00:00` suffix. fromisoformat handles it
    on 3.11+; we strip a trailing `Z` for the older variant just in case."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return None


def _to_cents(price: Any) -> int:
    """OF prices come as floats like 9.99. Convert via int(round(... * 100))
    to avoid 9.99 → 998 floating-point traps."""
    try:
        if price is None or price == "":
            return 0
        return int(round(float(price) * 100))
    except Exception:
        return 0


def _media_ids(media: list | None) -> list[int]:
    """Pluck media.id from each item; skip non-int."""
    if not isinstance(media, list):
        return []
    out: list[int] = []
    for m in media:
        if isinstance(m, dict):
            mid = m.get("id")
            if isinstance(mid, int):
                out.append(mid)
    return out


# ── Top-level dispatch ─────────────────────────────────────────────

async def transcode(event: dict) -> None:
    """Route an event to the right canonical-table writer. Called
    fire-and-forget from `events.handle_event`."""
    if not isinstance(event, dict):
        return
    account_id = event.get("__account_id")
    # Pick the first non-sentinel top-level key as the event type, matching
    # events.py:_first_real_key.
    event_type = None
    payload: Any = None
    for k, v in event.items():
        if not k.startswith("__"):
            event_type = k
            payload = v
            break
    if not event_type:
        return

    try:
        if event_type == "api2_chat_message" and isinstance(payload, dict):
            await _transcode_chat_message(account_id, payload)
        elif event_type in ("paidMessage", "messageUnlock", "purchase") and isinstance(payload, dict):
            # PPV-unlock path. Shape not yet observed in production — we
            # log + record a best-effort transaction so the first one in
            # the wild leaves enough breadcrumbs to refine the parser.
            await _transcode_ppv_unlock(account_id, event_type, payload)
        else:
            # Toasts/online/subscribe/etc. don't get full transcodes yet,
            # but they often carry up-to-date avatar + display name. Touch
            # the Fan row so the inbox cold-start renders from DB without
            # waiting on OF.
            await _touch_fan_from_event(account_id, payload)
            _log_unknown(event_type, payload)
    except Exception:
        log.exception("transcode failed (account=%s type=%s)", account_id, event_type)


async def _touch_fan_from_event(account_id: Any, payload: Any) -> None:
    """Opportunistic avatar/display-name refresh for non-message events.

    OF embeds a creator-side user blob under a few different keys (toasts
    use `data.relatedUser`, online events use `user`, …). We sniff the
    common shapes and update Fan(avatar_url, of_username, of_display_name)
    if we got an id + at least one field worth writing. This is a touch,
    not a transcode — it never creates a new Fan row, only updates ones
    that already exist (the chat-message path creates).

    Avatar shape varies: messages give `avatar` (string URL), toasts give
    `avatarThumbs` (dict of size→URL). We prefer c144, fall back to c50.
    """
    if not account_id or not isinstance(payload, dict):
        return
    user = None
    if isinstance(payload.get("fromUser"), dict):
        user = payload["fromUser"]
    elif isinstance(payload.get("user"), dict):
        user = payload["user"]
    elif isinstance(payload.get("data"), dict):
        d = payload["data"]
        if isinstance(d.get("relatedUser"), dict):
            user = d["relatedUser"]
        elif isinstance(d.get("user"), dict):
            user = d["user"]
    if not user:
        return
    fan_id = user.get("id")
    if not isinstance(fan_id, int):
        return
    avatar = user.get("avatar")
    if not avatar:
        thumbs = user.get("avatarThumbs")
        if isinstance(thumbs, dict):
            avatar = thumbs.get("c144") or thumbs.get("c50") or next(
                (v for v in thumbs.values() if isinstance(v, str)), None,
            )
    name = user.get("name")
    uname = user.get("username")
    if not (avatar or name or uname):
        return

    # UPDATE-only (no insert) — we don't want a stray toast to mint a Fan
    # row for someone the model has never actually messaged.
    updates: dict[str, Any] = {"updated_at": datetime.utcnow()}
    if avatar:
        updates["avatar_url"] = avatar
    if name:
        updates["of_display_name"] = name
    if uname:
        updates["of_username"] = uname

    async with get_session() as s:
        existing = await s.get(Fan, (str(account_id), int(fan_id)))
        if existing is None:
            return
        for k, v in updates.items():
            setattr(existing, k, v)
        await s.commit()


# ── api2_chat_message ─────────────────────────────────────────────

async def _transcode_chat_message(account_id: str | None, m: dict) -> None:
    """Upsert messages + fans + chats rows for one OF chat_message event.

    `m` is the payload after key-strip — i.e. the contents of
    `event['api2_chat_message']`. Shape we rely on:

        m = {
            'id': 9684362558908,           # OF message_id (BIGINT)
            'text': '<p>...</p>',
            'price': 9.99,                  # may be 0/null for free
            'isFree': bool,
            'isOpened': bool,               # PPV unlocked or not
            'isTip': bool,
            'isMediaReady': bool,
            'mediaCount': int,
            'media': [{'id': ...}, ...],
            'lockedText': bool,
            'fromUser': {'id': int, 'username': str, 'name': str, ...},
            'createdAt': '2026-05-18T19:06:47+00:00',
            'changedAt': '...',
            'responseType': 'message',
            'isFromQueue': bool,
        }

    OF sometimes omits fields; everything below copes with None.
    """
    message_id = m.get("id")
    from_user = m.get("fromUser") or {}
    fan_id = from_user.get("id")
    if not message_id or not fan_id or not account_id:
        # Without all three we can't form a primary key — log + bail.
        log.debug("chat_message missing message_id/fan_id/account_id: %s",
                  {"id": message_id, "fan_id": fan_id, "account_id": account_id})
        return

    # Direction: 'out' if we sent it, 'in' otherwise.
    direction = "out" if str(fan_id) == str(account_id) else "in"

    # For OUTBOUND messages we sent ourselves, fromUser.id == account_id,
    # which means we can't infer the chat partner from this payload alone.
    # OF's chat_message event for outbound doesn't carry the recipient as a
    # field. The outbound write happens at send-time in our send handler
    # (phase B); here we still want to capture the message body but the
    # (account_id, fan_id, message_id) PK requires SOME fan_id. Skip for now
    # and let the send-side handler own outbound persistence.
    if direction == "out":
        # Persistence is owned by the send handler, but the cached OF chat
        # window is stale either way (lastMessage direction flipped → a fan
        # drops off owe-reply, previews moved). This echo is the ONLY signal
        # for sends made outside our send endpoint (OF web, another device,
        # a different automation instance) — bust both readers of the window
        # so the next list/badge fetch re-reads OF. Debounced per account: a
        # mass blast echoes one outbound frame per recipient, and busting on
        # every one keeps the caches permanently cold for the blast's whole
        # duration (each 60/90s poll then pays a full live OF re-read). One
        # bust per window is enough — polls are the refetch cadence anyway,
        # so extra busts between polls buy nothing.
        aid_out = str(account_id)
        now_mono = time.monotonic()
        if now_mono - _last_out_bust.get(aid_out, 0.0) >= _OUT_BUST_DEBOUNCE_S:
            _last_out_bust[aid_out] = now_mono
            relay_cache.invalidate("list_chats", aid_out)
            relay_cache.invalidate("roster_count", aid_out)
        log.debug("skipping outbound chat_message %s — will be written at send time", message_id)
        return

    # Build the fields we need.
    created_at = _parse_iso(m.get("createdAt")) or datetime.utcnow()
    price_cents = _to_cents(m.get("price"))
    # A tip's $ value rides in top-level `tipAmount` (DOLLARS), NOT `price` — OF
    # sends a tip as a chat_message with isTip=true and price=0 (the note the fan
    # attached is in `tipText`). Read tipAmount for the real amount; fall back to
    # price_cents for any isTip event that somehow lacks tipAmount. Keep this
    # SEPARATE from price_cents so PPV/paid-unlock logic below is untouched.
    is_tip = bool(m.get("isTip"))
    tip_cents = _to_cents(m.get("tipAmount")) if is_tip else 0
    if is_tip and tip_cents == 0:
        tip_cents = price_cents
    media_ids = _media_ids(m.get("media"))
    is_paid = m.get("isOpened") if price_cents > 0 else None  # NULL for free messages
    sender_name = (from_user.get("name") or from_user.get("username") or "")[:255]
    body = m.get("text") or ""

    # We persist the full payload as raw_json so future migrations can
    # backfill new columns without re-fetching from OF. 64 KB cap keeps
    # one rogue media-array from blowing up disk usage.
    raw = dump_capped(m)

    async with get_session() as s:
        # ── messages upsert ─────────────────────────────────────
        # Idempotent via the composite PK. on_conflict_do_update lets us
        # refresh fields that OF may have flipped (is_paid after unlock,
        # is_unsent after deletion).
        msg_stmt = sqlite_insert(Message).values(
            account_id=str(account_id),
            fan_id=int(fan_id),
            message_id=int(message_id),
            direction=direction,
            sender_name=sender_name,
            body=body,
            media_ids=json.dumps(media_ids),
            media_count=int(m.get("mediaCount") or 0),
            price_cents=price_cents,
            is_paid=is_paid,
            is_tip=is_tip,
            purchased_at=_parse_iso(m.get("openedAt")) if is_paid else None,
            is_unsent=False,
            raw_json=raw,
            created_at=created_at,
        ).on_conflict_do_update(
            index_elements=["account_id", "fan_id", "message_id"],
            set_={
                "body": body,
                "is_paid": is_paid,
                "is_tip": is_tip,
                "media_count": int(m.get("mediaCount") or 0),
                "raw_json": raw,
            },
        )
        await s.execute(msg_stmt)

        # ── fans upsert ─────────────────────────────────────────
        # New fan: insert with the fields OF gave us. Existing fan: bump
        # last_message_received_at and refresh display info, leaving
        # AI-extracted facts (his_age, hobbies, ...) untouched.
        fan_stmt = sqlite_insert(Fan).values(
            account_id=str(account_id),
            fan_id=int(fan_id),
            of_username=from_user.get("username"),
            of_display_name=from_user.get("name"),
            avatar_url=from_user.get("avatar"),
            last_message_received_at=created_at,
            # Distinguish "real fan of our creator" vs "creator we follow."
            # `subscribedOn=True` would mean this person is subscribed to us
            # (= a real fan). `subscribedBy=True` means our account is
            # subscribed to them (we're the fan, they're a creator).
            source=(
                "fan" if from_user.get("subscribedOn") else
                "creator_we_follow" if from_user.get("subscribedBy") else
                "unknown"
            ),
        )
        # ON CONFLICT: never CLOBBER a populated name/avatar with the blanks OF sends
        # on a plain message. OF chat-message payloads carry only `fromUser:{id}` (no
        # username/name/avatar), so unconditionally writing `.get(...)` here used to
        # null out names captured earlier by a toast/online event — the root cause of
        # ~80% of fans showing no name. Only refresh these when OF actually gives them.
        on_conflict = {
            "last_message_received_at": created_at,
            "updated_at": datetime.utcnow(),
        }
        if from_user.get("username"):
            on_conflict["of_username"] = from_user["username"]
        if from_user.get("name"):
            on_conflict["of_display_name"] = from_user["name"]
        if from_user.get("avatar"):
            on_conflict["avatar_url"] = from_user["avatar"]
        # Self-heal a weak/unknown source if THIS payload carries OF's
        # subscribedOn/subscribedBy (rare on a plain DM, present on toast/online
        # frames) — upgrade-only, never downgrades a known 'fan'/'creator_we_follow'.
        on_conflict.update(source_self_heal_set(
            from_user.get("subscribedOn"), from_user.get("subscribedBy")))
        fan_stmt = fan_stmt.on_conflict_do_update(
            index_elements=["account_id", "fan_id"],
            set_=on_conflict,
        )
        await s.execute(fan_stmt)

        # ── chats upsert ────────────────────────────────────────
        # The "preview" we show in the inbox is the first ~120 chars of body,
        # HTML-stripped just enough that it doesn't read raw <p> tags.
        preview = _strip_html(body)[:120]
        chat_stmt = sqlite_insert(Chat).values(
            account_id=str(account_id),
            fan_id=int(fan_id),
            last_message_id=int(message_id),
            last_message_at=created_at,
            last_message_preview=preview,
            unread_count=1 if direction == "in" else 0,
        ).on_conflict_do_update(
            index_elements=["account_id", "fan_id"],
            set_={
                "last_message_id": int(message_id),
                "last_message_at": created_at,
                "last_message_preview": preview,
                # Increment unread only for inbound; don't reset to 1.
                "unread_count": (
                    Chat.unread_count + 1 if direction == "in" else Chat.unread_count
                ),
            },
        )
        await s.execute(chat_stmt)

        # ── transaction (tip path) ───────────────────────────────
        # A tip rides on a chat_message with isTip=true; its $ value is in
        # `tipAmount` (dollars), captured above as tip_cents — `price` is 0 here.
        # OF also sends a 'tipReceived' / 'paidMessage' event for PPV
        # unlocks (different shape) — we'll wire those when we see one
        # in the unknown-event log.
        if is_tip and tip_cents > 0 and direction == "in":
            await _record_inbound_payment(
                s,
                account_id=str(account_id),
                fan_id=int(fan_id),
                message_id=int(message_id),
                kind="tip",
                amount_cents=tip_cents,
                occurred_at=created_at,
                raw=raw,
            )

    log.info(
        "chat_message persisted: account=%s fan=%s msg=%s direction=%s price=%s tip=%s",
        account_id, fan_id, message_id, direction, price_cents,
        tip_cents if is_tip else 0,
    )

    # Cache invalidation — must run AFTER the `async with get_session()`
    # block has exited (auto-commit on success) so a concurrent reader
    # that re-fetches doesn't cache the pre-commit upstream state.
    # No-ops against namespaces Stage C hasn't wired in yet.
    aid_s = str(account_id)
    relay_cache.invalidate("list_chats", aid_s)
    relay_cache.invalidate("init", aid_s)
    relay_cache.invalidate("notifications_count", aid_s)
    # Roster strip badge: an inbound DM bumps this model's unread, our reply
    # clears its owe-reply — bust the cached count so the next 60s poll recomputes
    # from OF. This is what keeps the unread badge live off the WS pump.
    relay_cache.invalidate("roster_count", aid_s)

    # W7 webhook-priority dispatch: an inbound DM is now persisted, so kick the
    # owning automation to react in real time (gated per-account, default OFF).
    # Reaching here means direction == "in" (outbound returned early above).
    # Fire-and-forget on the running loop; on_inbound_message never raises, but
    # we guard the create_task too so a dispatch hiccup can't break the pump.
    try:
        from webhook_dispatch import on_inbound_message
        asyncio.create_task(
            on_inbound_message(aid_s, int(fan_id), int(message_id))
        )
    except Exception:
        log.debug("webhook_dispatch hook failed", exc_info=True)

    # tip_reward: a fan tip rides on this inbound message (isTip; $ in tipAmount,
    # captured as tip_cents — price is 0 for a tip). Kick the (gated) image-reward
    # automation in real time. Separate hook from on_inbound_message so it fires
    # even on a terminal-stage fan.
    if is_tip and tip_cents > 0:
        try:
            from webhook_dispatch import on_inbound_tip
            asyncio.create_task(
                on_inbound_tip(aid_s, int(fan_id), int(message_id), int(tip_cents))
            )
        except Exception:
            log.debug("tip_reward dispatch hook failed", exc_info=True)
    # Inbound IMAGE (a fan-sent photo, NOT a tip) = a buying signal. Kick the
    # (gated, default-OFF) image-reply / closer hook in real time. Separate from
    # the tip hook above: a tip-with-media is the tip path's; a bare media DM is
    # this one's. Guard on `not is_tip` (not just the tip_cents>0 branch above) so
    # a degenerate isTip frame with a 0/missing tipAmount that ALSO carries media
    # is never misread as a bare buying-signal photo. The hook decides the flags.
    # A GIPHY dm rides the same hook even though it has NO media (see
    # of_shapes.giphy_dm_id — the shared classifier the describe path also uses):
    # gating on media_ids alone left a gif as a completely blank turn — no text,
    # no picture, nothing for the AI to answer. We report WHAT arrived
    # (`has_media`); what that's worth — buying signal vs. joke — is the hook's
    # call, not ours.
    elif (media_ids or giphy_dm_id(m)) and not is_tip:
        try:
            from webhook_dispatch import on_inbound_image
            asyncio.create_task(
                on_inbound_image(aid_s, int(fan_id), int(message_id),
                                 has_media=bool(media_ids),
                                 is_video=has_video(m.get("media")))
            )
        except Exception:
            log.debug("image dispatch hook failed", exc_info=True)


async def _transcode_ppv_unlock(
    account_id: str | None, event_type: str, payload: dict
) -> None:
    """Best-effort PPV-unlock handler. The exact OF event shape is still
    unknown to us — _log_unknown logs the first occurrence so we can
    iterate. We attempt to extract message_id, fan_id, and price from
    the most likely fields; on miss, we log and move on rather than
    crashing the pump."""
    if not account_id:
        return
    _log_unknown(event_type, payload)  # keep the discovery log alive
    msg = payload.get("message") if isinstance(payload.get("message"), dict) else payload
    message_id = msg.get("id") or payload.get("messageId") or payload.get("message_id")
    fan = (msg.get("fromUser") or payload.get("user") or payload.get("fan") or {})
    fan_id = fan.get("id") if isinstance(fan, dict) else None
    amount = msg.get("price") or payload.get("price") or payload.get("amount")
    amount_cents = _to_cents(amount)
    occurred = _parse_iso(
        payload.get("openedAt") or payload.get("createdAt") or msg.get("openedAt")
    ) or datetime.utcnow()
    if not (message_id and fan_id and amount_cents > 0):
        log.info(
            "ppv-unlock event missing fields (type=%s keys=%s) — skipping payment row",
            event_type, tuple(sorted(payload.keys())),
        )
        return
    raw = dump_capped(payload)
    async with get_session() as s:
        await _record_inbound_payment(
            s,
            account_id=str(account_id),
            fan_id=int(fan_id),
            message_id=int(message_id),
            kind="ppv",
            amount_cents=amount_cents,
            occurred_at=occurred,
            raw=raw,
        )

    # Post-commit cache invalidation. See `_transcode_chat_message` for the
    # ordering rationale.
    aid_s = str(account_id)
    relay_cache.invalidate("init", aid_s)
    relay_cache.invalidate("notifications_count", aid_s)

    # Tell the browser a sale landed — same event the ledger ingest emits, so
    # the fan drawer's revenue caches refresh on the spot instead of waiting
    # for the 5-min payouts tick.
    try:
        from events import broadcast as _sse_broadcast
        await _sse_broadcast({
            "transaction_recorded": {
                "fan_id": int(fan_id),
                "kind": "ppv",
                "amount_cents": amount_cents,
                "reason": "ws_unlock",
            },
            "__account_id": aid_s,
        })
    except Exception:
        log.debug("transaction_recorded broadcast failed", exc_info=True)


async def _record_inbound_payment(
    s,
    *,
    account_id: str,
    fan_id: int,
    message_id: int,
    kind: str,
    amount_cents: int,
    occurred_at: datetime,
    raw: str,
) -> None:
    """Insert a Transaction + bump fans.lifetime_spend_cents — idempotent
    on (account_id, fan_id, message_id, kind). No unique index exists yet
    (would need a migration), so we SELECT-then-INSERT inside the caller's
    session. Safe because the WS pump processes events serially per
    account."""
    existing = await s.execute(
        select(Transaction.id).where(
            Transaction.account_id == account_id,
            Transaction.fan_id == fan_id,
            Transaction.message_id == message_id,
            Transaction.kind == kind,
        ).limit(1)
    )
    if existing.scalar() is not None:
        return  # already recorded — don't double-count

    s.add(Transaction(
        account_id=account_id,
        fan_id=fan_id,
        kind=kind,
        message_id=message_id,
        amount_cents=amount_cents,
        currency="USD",
        occurred_at=occurred_at,
        raw_json=raw,
    ))

    # Bump the denormalized rollup so the FanDrawer's lifetime-spend
    # stat is one indexed read instead of a SUM(amount_cents) every open.
    fan = await s.get(Fan, (account_id, fan_id))
    if fan is not None:
        fan.lifetime_spend_cents = (fan.lifetime_spend_cents or 0) + amount_cents
        fan.updated_at = datetime.utcnow()

    log.info(
        "transaction persisted: account=%s fan=%s kind=%s amount=%s",
        account_id, fan_id, kind, amount_cents,
    )


def _strip_html(s: str) -> str:
    """Quick-and-dirty tag strip for the chat-list preview. OF returns
    minimal HTML (<p>, <span class>, <strong>); regex is enough.
    Full sanitization happens client-side when the body is rendered."""
    import re
    if not s:
        return ""
    return re.sub(r"<[^>]+>", "", s).strip()


# ── Unknown-event logger ───────────────────────────────────────────

def _log_unknown(event_type: str, payload: Any) -> None:
    """Log the first time we see a (type, key-shape) combo. Helps us
    build out the transcoder over real samples instead of guessing."""
    if isinstance(payload, dict):
        shape = tuple(sorted(payload.keys()))
    else:
        shape = (type(payload).__name__,)
    fingerprint = (event_type, shape)
    if fingerprint in _seen_unknown:
        return
    _seen_unknown.add(fingerprint)
    log.info("new event shape: type=%s keys=%s", event_type, shape)
