"""
Outbound message attribution writer.

When the relay's `POST /api/of/v2/chats/{chat_id}/messages` handler returns
successfully from OF, we immediately write a `messages` row stamped with
the human actor (from `X-Employee-Id`) or — if no header was present —
the system "Automation" employee. The transcoder pump intentionally skips
outbound chat_message events (event_transcoder.py:158-170), so this is
the single source of truth for outbound persistence.

Design notes:
  • Idempotent: insert-or-ignore against (account_id, fan_id, message_id)
    composite PK so retries / future ws-echo collisions are no-ops.
  • Never raises: the OF send already succeeded; the user's request must
    not 500 because of a DB hiccup or a missing Automation row (migration
    0011 not yet run).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Iterable

from sqlalchemy import delete
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from db.engine import get_session
from db.models import Message

log = logging.getLogger("of-relay.attribution")

# Default stand-down after a human takes over a chat (the W7 hard-yield). 1 min;
# per-account override via account_ai_config.webhook_config_json.manual_yield_minutes.
_DEFAULT_MANUAL_YIELD_S = 60


async def _manual_yield_seconds(account_id: str) -> int:
    """Per-account manual-chatter stand-down in seconds (Instant reply settings).
    Default 60s; 0 disables the yield. Bad/absent config → default."""
    try:
        from db.models import AccountAiConfig
        async with get_session() as s:
            cfg = await s.get(AccountAiConfig, str(account_id))
        if cfg and cfg.webhook_config_json:
            mins = (json.loads(cfg.webhook_config_json) or {}).get("manual_yield_minutes")
            if mins is not None:
                return max(0, int(float(mins) * 60))
    except Exception:
        log.debug("manual_yield_seconds read failed", exc_info=True)
    return _DEFAULT_MANUAL_YIELD_S


# Synthetic-id band for optimistic mass placeholders. Picked to sit in the
# gap between two hard constraints:
#   • ABOVE every real OF message_id (currently ~9.9e12 and rising slowly)
#     so a freshly-broadcast row sorts as the newest message (the timeline
#     orders by message_id DESC) — that's where a just-sent message belongs
#     and it keeps the row on page 1 of GET /admin/messages.
#   • BELOW JS's 2**53 safe-integer ceiling (9.007e15) so the id round-trips
#     through the frontend's Number-typed `message_id` without precision
#     loss — two placeholders that differ by 1 (consecutive mass_run_ids)
#     must stay distinct as React keys.
# 5e15 leaves ~500x headroom over today's OF ids and ~4e15 of room under the
# ceiling for the (small, autoincrement) mass_run_id. Bump it toward 2**53 if
# OF's ids ever climb close.
_MASS_PLACEHOLDER_BASE = 5_000_000_000_000_000


def mass_placeholder_message_id(mass_run_id: int) -> int:
    """Collision-proof synthetic `message_id` for an un-reconciled mass
    placeholder row.

    Stable per `(account_id, fan_id, mass_run_id)`: exactly one optimistic
    row per recipient per broadcast, so re-running the close path (which
    is `on_conflict_do_nothing`) never duplicates. See `_MASS_PLACEHOLDER_BASE`
    for why it lands where it does.
    """
    return _MASS_PLACEHOLDER_BASE + int(mass_run_id)


async def write_outbound_attribution(
    *,
    account_id: str,
    fan_id: int,
    message_id: int,
    sent_by_employee_id: int | None,
    body: str,
    price_cents: int,
    created_at: datetime,
    mass_run_id: int | None = None,
    funnel_step: int | None = None,
    automation_kind: str | None = None,
    emit_live: bool = False,
) -> None:
    """Write the outbound `messages` row.

    `automation_kind`: which automation sent this row (`of_ai_chat`,
    `send_welcome`, `deep_convo`, `followup`, `autoreply`, `send_mass_message`,
    `reply_mass_funnel`, `nudge_online`, …). NULL for human / relay sends. This
    is the per-automation breakdown of the otherwise-flat `sent_by_employee_id`
    Automation sentinel — the Messages tab and the per-automation stats panel
    read it.

    `funnel_step` (reply_mass_funnel / A11): the 1-indexed funnel step this row
    represents, so the chat history and audits can tell a funnel message apart
    from a plain reply. NULL for non-funnel sends.

    `emit_live` (WORKER→SSE bridge): when True, emit a synthetic
    `api2_chat_message` SSE event after a successful write so an open chat
    updates live (the OF-WS pump skips outbound, so worker writes have no live
    path otherwise). Defaults False — the human per-chat send path (server.py)
    relies on optimistic UI + reconcile and must NOT get a second live row, so
    only background workers (automations) opt in.

    Best-effort: catches every exception so the caller (the send handler)
    never raises because of a DB write. The OF send already succeeded
    upstream — we don't want a transient DB failure to surface as a 500
    to the user when the message actually went through.

    `sent_by_employee_id` resolution:
      • Caller-provided id wins (parsed from `X-Employee-Id`).
      • If None, fall back to the system Automation employee via
        `get_automation_employee_id()`.
      • If that lookup itself fails (LookupError from migration 0011 not
        having run, or anything else), log a WARNING and write NULL —
        attribution is degraded but the row still lands.
    """
    if sent_by_employee_id is None:
        # Chatter session present? Then the resolver in employees.py
        # SHOULD have returned a mirror Employee id, but raised /
        # short-circuited (logged at WARN via that path). Writing
        # Automation here would falsely credit the system sentinel for
        # a chatter's send. Leave it NULL so the row is honest and the
        # UI's "render only on truthy display_name" guard hides the
        # bogus label entirely.
        try:
            from chatters import get_request_chatter
            chatter = get_request_chatter()
        except Exception:
            chatter = None
        if chatter is not None:
            log.warning(
                "outbound message %s left sent_by_employee_id=NULL — "
                "chatter resolver returned None for chatter=%s account=%s. "
                "Inspect logs for 'chatter→employee resolution failed'.",
                message_id, chatter.id, account_id,
            )
        else:
            try:
                from employees import get_automation_employee_id
                sent_by_employee_id = await get_automation_employee_id()
            except LookupError:
                log.warning(
                    "Automation employee not found (migration 0011 not run?); "
                    "writing message %s with sent_by_employee_id=NULL",
                    message_id,
                )
            except Exception:
                log.warning(
                    "Automation employee lookup raised; writing message %s "
                    "with sent_by_employee_id=NULL",
                    message_id,
                    exc_info=True,
                )

    try:
        async with get_session() as s:
            stmt = sqlite_insert(Message).values(
                account_id=str(account_id),
                fan_id=int(fan_id),
                message_id=int(message_id),
                direction="out",
                sender_name="",
                body=body,
                media_ids="[]",
                media_count=0,
                price_cents=int(price_cents),
                is_paid=False if (price_cents or 0) > 0 else None,
                is_tip=False,
                sent_by_employee_id=sent_by_employee_id,
                mass_run_id=mass_run_id,
                funnel_step=funnel_step,
                automation_kind=automation_kind,
                raw_json=None,
                created_at=created_at,
                ingested_at=datetime.utcnow(),
            ).on_conflict_do_nothing(
                index_elements=["account_id", "fan_id", "message_id"],
            )
            res = await s.execute(stmt)
        log.info(
            "attribution: account=%s fan=%s msg=%s emp=%s mass_run=%s",
            account_id, fan_id, message_id, sent_by_employee_id, mass_run_id,
        )
        # Emit ONLY when a row was actually inserted — on_conflict_do_nothing
        # makes a re-run/duplicate a no-op, and re-broadcasting it would re-bump
        # an already-shown chat to the top of the inbox for nothing.
        if emit_live and (res.rowcount or 0) > 0:
            from events import publish_db_message  # local import avoids a cycle
            await publish_db_message(
                account_id=str(account_id), fan_id=int(fan_id),
                message_id=int(message_id), body=body, created_at=created_at,
                price_cents=int(price_cents),
            )

        # W7 HARD YIELD — manual chatter always wins. A HUMAN sending a 1:1 chat
        # message rests the fan so no automation (any tier) fires on a fan a
        # human is handling. Reuses W3's per-fan cooldown. Scope is deliberate:
        #   • mass_run_id is None  → only genuine 1:1 sends; a manual BROADCAST
        #     attributes the triggering human per-recipient with mass_run_id SET,
        #     and we must NOT cooldown the whole audience (they should reply).
        #   • sent_by_employee_id != Automation sentinel → excludes automation
        #     sends (they resolve to the sentinel above and set their OWN
        #     cooldown post-send); NULL (degraded chatter resolution) excluded.
        # Own try/except so a sentinel-lookup hiccup never breaks the (already
        # committed) send. Late imports avoid the attribution↔executor cycle.
        if (res.rowcount or 0) > 0 and mass_run_id is None and sent_by_employee_id is not None:
            try:
                from employees import get_automation_employee_id
                auto_id = await get_automation_employee_id()
                if sent_by_employee_id != auto_id:
                    # Stand-down duration is per-account (Instant reply settings,
                    # webhook_config_json.manual_yield_minutes); default 1 min. 0
                    # disables the manual yield for that account.
                    secs = await _manual_yield_seconds(str(account_id))
                    if secs > 0:
                        from automation_executor import start_fan_cooldown
                        await start_fan_cooldown(
                            str(account_id), int(fan_id), cooldown_s=secs)
            except Exception:
                log.debug("w7 hard-yield cooldown skipped", exc_info=True)
    except Exception:
        log.exception(
            "attribution write failed (account=%s fan=%s msg=%s)",
            account_id, fan_id, message_id,
        )


async def write_mass_optimistic_rows(
    *,
    account_id: str,
    fan_ids: Iterable[int],
    mass_run_id: int,
    sent_by_employee_id: int | None,
    body: str,
    price_cents: int,
    created_at: datetime,
    automation_kind: str | None = None,
    emit_live: bool = False,
) -> int:
    """Write one optimistic outbound `messages` row per *known* recipient at
    mass-send time. Returns the number of rows attempted.

    `emit_live` (WORKER→SSE bridge): when True, emit a synthetic
    `api2_chat_message` per recipient after the write so each open chat shows
    the broadcast live. Defaults False; only background workers opt in.

    Why this exists: OF's broadcast endpoint (`POST /messages/queue`) returns
    a queue object, not per-fan message ids, and the WS pump deliberately
    skips outbound chat_message events (event_transcoder.py ~236). So without
    this, a mass send lands NO `messages` row and the chat cache shows nothing
    until — if ever — a reconciler backfills it. Each row carries:

      • a synthetic `message_id` (`mass_placeholder_message_id`) that can't
        collide with a real OF id,
      • `temp_id` so a later reconciler can match + replace it,
      • `mass_run_id` linking it to the broadcast.

    When OF echoes the real per-fan id, the caller writes the real row via
    `write_outbound_attribution` and drops the placeholder via
    `reconcile_mass_placeholder`.

    Only EXPLICIT recipients (`userIds`) are knowable here; list-based
    audiences (`userLists`) aren't expanded at send time and are left to the
    WS-pump reconciler (TODO in server._close_mass_run).

    Best-effort: never raises (the OF send already succeeded). Idempotent via
    `on_conflict_do_nothing` on the composite PK.
    """
    placeholder_id = mass_placeholder_message_id(mass_run_id)
    # NULL = no PPV; False = PPV not yet purchased. Mirrors the per-chat
    # outbound writer above so the PPV/All-Messages tabs read consistently.
    is_paid = False if (price_cents or 0) > 0 else None

    seen: set[int] = set()
    rows: list[dict] = []
    for raw in fan_ids:
        try:
            fid = int(raw)
        except (TypeError, ValueError):
            continue
        if fid in seen:
            continue
        seen.add(fid)
        rows.append({
            "account_id": str(account_id),
            "fan_id": fid,
            "message_id": placeholder_id,
            "direction": "out",
            "sender_name": "",
            "body": body,
            "media_ids": "[]",
            "media_count": 0,
            "price_cents": int(price_cents),
            "is_paid": is_paid,
            "is_tip": False,
            "sent_by_employee_id": sent_by_employee_id,
            "temp_id": f"mass:{int(mass_run_id)}:{fid}",
            "mass_run_id": int(mass_run_id),
            "automation_kind": automation_kind,
            "raw_json": None,
            "created_at": created_at,
            "ingested_at": datetime.utcnow(),
        })

    if not rows:
        return 0

    try:
        async with get_session() as s:
            stmt = sqlite_insert(Message).values(rows).on_conflict_do_nothing(
                index_elements=["account_id", "fan_id", "message_id"],
            )
            await s.execute(stmt)
        log.info(
            "mass optimistic: account=%s run=%s rows=%d emp=%s",
            account_id, mass_run_id, len(rows), sent_by_employee_id,
        )
        if emit_live:
            from events import publish_db_message  # local import avoids a cycle
            for r in rows:
                await publish_db_message(
                    account_id=str(account_id), fan_id=int(r["fan_id"]),
                    message_id=int(r["message_id"]), body=body,
                    created_at=created_at, price_cents=int(price_cents),
                )
    except Exception:
        log.exception(
            "mass optimistic write failed (account=%s run=%s)",
            account_id, mass_run_id,
        )
    return len(rows)


async def record_broadcast_mass_run(
    *,
    account_id: str,
    queue_id: int | None,
    automation_kind: str,
    recipient_count: int = 0,
) -> int | None:
    """Insert a `mass_runs` row that exists purely to ATTRIBUTE a broadcast to
    its automation in the Mass Messages tab.

    Used by the list-audience broadcasters (`mass_nudge`, `online_blast`) that
    fire OF's `send_mass_message` directly and write NO per-fan `messages` rows
    (so they never clutter the Messages tab) — but should still show "sent by
    online_blast" in the cache view. The Mass Messages tab joins
    `mass_broadcast_cache.queue_id → mass_runs.queue_id`.

    Stamped with the Automation sentinel employee + status='ok' (the OF send
    already succeeded by the time we're called). Best-effort: never raises;
    returns the new run id, or None if the write failed / no queue_id.
    """
    if queue_id is None:
        return None
    try:
        from db.models import MassRun  # local import: avoids a models import cycle
        employee_id: int | None = None
        try:
            from employees import get_automation_employee_id
            employee_id = await get_automation_employee_id()
        except Exception:
            log.debug("automation employee lookup failed; broadcast run NULL", exc_info=True)
        async with get_session() as s:
            mr = MassRun(
                account_id=str(account_id),
                started_by_employee_id=employee_id,
                automation_kind=automation_kind,
                queue_id=int(queue_id),
                recipient_count=int(recipient_count),
                status="ok",
                completed_at=datetime.utcnow(),
            )
            s.add(mr)
            await s.flush()
            return int(mr.id)
    except Exception:
        log.exception(
            "broadcast mass_run record failed (account=%s queue=%s kind=%s)",
            account_id, queue_id, automation_kind,
        )
        return None


async def reconcile_mass_placeholder(
    *,
    account_id: str,
    fan_id: int,
    mass_run_id: int,
) -> None:
    """Drop the optimistic placeholder row for `(account, fan, run)` once the
    real OF `message_id` for that recipient has been persisted.

    Best-effort: the real row is already written, so a failed cleanup leaves a
    harmless duplicate (the placeholder), never a missing message.
    """
    try:
        async with get_session() as s:
            await s.execute(
                delete(Message).where(
                    Message.account_id == str(account_id),
                    Message.fan_id == int(fan_id),
                    Message.message_id == mass_placeholder_message_id(mass_run_id),
                )
            )
    except Exception:
        log.exception(
            "mass placeholder reconcile failed (account=%s fan=%s run=%s)",
            account_id, fan_id, mass_run_id,
        )
