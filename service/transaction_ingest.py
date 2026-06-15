"""
Background ingest supervisor for OF's /api2/v2/payouts/transactions ledger.

Single async supervisor task per process. 10-min ticks. Sequential per-account
inside one refresh tick. 0.4s inter-page sleep.

Drops X-Priority entirely (the request-context priority does not propagate
to a background task — see library/db_data/12_transaction_ingest_synthesis.md §2
row 3). The rate-limit posture is cadence + sequential walk, not a header.

7-day rolling window steady-state; 90-day cold-start backfill.

3-step dedup writer (synthesis §5):
  Step 1: provider_id fast path (handles re-polls + loading->cleared flips).
  Step 2: fingerprint fallback for kind='tip' to find the WS-pump sibling row
          (account_id, fan_id, kind='tip', amount_cents +/- tolerance,
          occurred_at +/- 300s, source='ws', provider_transaction_id IS NULL).
          Patches in place; promotes to source='ledger'.
  Step 3: fresh insert as source='ledger'.

fans.lifetime_spend_cents rule (synthesis §5):
  Bump on Step 3 only. Never on Step 1 update. Never on Step 2 (the WS path
  already bumped at event_transcoder.py:378-381). On status->chargedback or
  refunded, apply delta with max(0, current - prev_amount_cents) clamp.

paidMessage convergence: when a ledger row arrives with kind='ppv_message'
AND a plausible unpaid outbound message exists, in the SAME transaction
link transaction.message_id and set messages.is_paid=true,
messages.purchased_at=occurred_at. This is currently the ONLY reliable
PPV-unlock signal (WS pump has caught 0 unlock events).

Canonical background-task template: _perflog_evictor_loop at
service/server.py:1374-1394. Sync OF client wrap idiom: server.py:1887
and server.py:2388 (`await asyncio.to_thread(...)`).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select, text

import accounts as account_registry
from db.engine import engine, get_session
from db.models import (
    Chatter,
    ChatterUser,
    Fan,
    Message,
    Transaction,
    TransactionScanHistory,
    User,
    UserAccount,
)
from of_client import OFClient

log = logging.getLogger("of-relay.ingest_tx")

# ── Knobs ────────────────────────────────────────────────────────────
_TICK_INTERVAL_S = 300                  # 5 minutes (was 600 — too slow vs the 30m green tier ceiling)
_PAGE_SLEEP_S = 0.4
_ROLLING_WINDOW_DAYS = 7
_DEFAULT_BACKFILL_DAYS = 90
_FAIL_BACKOFF_S = 3600                  # auto-pause for 1h after 3 fails
_PAGE_LIMIT = 100                       # OF max per request
_BACKFILL_CONCURRENCY = 4               # synthesis review §4
_PPV_LINK_LOOKBACK_DAYS = 30
_FAILS_BEFORE_PAUSE = 3
_STALE_INFLIGHT_S = 900                 # 15 min — escape valve for crashed ticks (see _tick_locks comment)

# Gated dead code. Flip to True when Planner B's WS dispatcher PR starts
# writing kind='ppv_pending' rows on unlock events (synthesis §5 line 193).
_ENABLE_PPV_FINGERPRINT = False

# Re-entrant tick guard: if run_one_tick(aid) takes >600s the next supervisor
# cycle would fire concurrently and double-write. Per-account asyncio.Lock.
_tick_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

# Cycle gate: only scan accounts whose owner (User) or linked chatter has
# been active within the last _ONLINE_WINDOW_S seconds. last_seen_at is
# bumped per-request with a ~5-min throttle (auth.py / chatters.py), so a
# 10-min window covers the throttle slack without keeping a logged-out
# dashboard's broken sessions burning OF proxy quota every 5 minutes.
_ONLINE_WINDOW_S = 600

# A run_one_tick stamps current_status='refreshing'/'backfilling' at start
# (line ~641) and overwrites it at end via _mark_success / _record_failure.
# If the tick task is cancelled (uvicorn reload, deploy, SIGTERM) or the
# process dies between those points, the stamp persists in the DB and the
# /run endpoint's 409 guard locks the account out permanently. _STALE_INFLIGHT_S
# bounds how long a real in-flight stamp can be trusted before treating it
# as a tombstone from a crashed prior holder. 15min >> typical tick duration
# (under 60s steady-state, a few min during a fresh 90d backfill).

# ── Kind catalog (HTML-stripped description prefix -> kind) ─────────
# Order matters — match top-to-bottom. Public so the admin
# /unknown-kinds endpoint can render the prefix table.
KIND_CATALOG: tuple[tuple[str, str], ...] = (
    # OF's vocab: "Subscription from X" = first charge (new sub);
    # "Recurring subscription from X" = monthly renewal. Verified
    # against the data: of 57 fans with both labels, 56 had the plain
    # "Subscription from" row strictly earlier than "Recurring".
    # Mapping directly from the prefix beats the `_maybe_promote_to_rebill`
    # heuristic — that heuristic depends on insertion order and quietly
    # fails when historical backfill writes newest-first.
    ("Recurring subscription from ", "rebill"),        # explicit renewal
    ("Subscription from ",           "subscription"),  # first charge; promoted by safety net
    ("Tip in chat from ",            "tip"),
    ("Tip from ",                    "tip"),
    ("Tip on post from ",            "tip_post"),
    ("Tip on stream from ",          "tip_stream"),
    ("Payment for message from ",    "ppv_message"),   # OF's actual prefix for PPV unlocks
    ("Message from ",                "ppv_message"),
    ("Post from ",                   "ppv_post"),
    ("Stream from ",                 "ppv_stream"),
    ("Custom request from ",         "custom"),
    ("Custom from ",                 "custom"),
)

# OF's status vocab differs from the original capture. Map to Phase F vocab.
_STATUS_MAP = {
    "done":           "cleared",
    "loading":        "pending",
    "undo":           "chargedback",
    "pending_return": "refund_pending",
}

_HTML_TAG = re.compile(r"<[^>]+>")
_REFUND_STATUSES = ("chargedback", "refunded")
_NON_REVENUE_STATUSES = ("chargedback", "refunded", "failed")


def _normalize_status(raw_status: str | None) -> str:
    if not raw_status:
        return "cleared"
    return _STATUS_MAP.get(raw_status, raw_status)


# ── Parsing helpers ─────────────────────────────────────────────────

def _strip_html(s: str | None) -> str:
    if not s:
        return ""
    return _HTML_TAG.sub("", s).strip()


def _classify_kind(description: str | None) -> str:
    text_ = _strip_html(description) if description and "<" in description else (description or "").strip()
    for prefix, kind in KIND_CATALOG:
        if text_.startswith(prefix):
            return kind
    return "unknown"


def _to_cents(v: Any) -> int | None:
    if v is None or v == "":
        return None
    try:
        return int(round(float(v) * 100))
    except Exception:
        return None


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return None


def _parse_row(raw: dict) -> dict:
    """Map an OF /payouts/transactions row to the local-column dict."""
    user = raw.get("user") or {}
    return {
        "provider_id": raw.get("id"),
        "fan_id": user.get("id"),
        "amount_cents": _to_cents(raw.get("amount")) or 0,
        "currency": raw.get("currency") or "USD",
        "occurred_at": _parse_iso(raw.get("createdAt")) or datetime.utcnow(),
        "status": _normalize_status(raw.get("status")),
        "cleared_at": _parse_iso(raw.get("clearedAt")) if raw.get("clearedAt") else None,
        "payout_pending_days": raw.get("payoutPendingDays"),
        "net_cents": _to_cents(raw.get("net")),
        "fee_cents": _to_cents(raw.get("fee")),
        "vat_cents": _to_cents(raw.get("vatAmount")),
        "description": _strip_html(raw.get("description") or ""),
        "raw_json": json.dumps(raw, default=str)[:32 * 1024],
    }


# ── Writer pieces ───────────────────────────────────────────────────

async def _maybe_promote_to_rebill(s, account_id: str, fan_id: int | None, kind: str) -> str:
    """First subscription stays 'subscription'; second+ becomes 'rebill'."""
    if kind != "subscription" or not fan_id:
        return kind
    res = await s.execute(
        select(Transaction.id).where(
            Transaction.account_id == account_id,
            Transaction.fan_id == fan_id,
            Transaction.kind.in_(("subscription", "rebill")),
        ).limit(1)
    )
    return "rebill" if res.scalar() is not None else "subscription"


async def _fingerprint_lookup(s, account_id: str, parsed: dict) -> Transaction | None:
    """WS-pump sibling lookup for the same logical tip event.

    Loose tolerance: amount +/- max(10c, 1%) for OF fee rounding ($5.00 ->
    $4.99 net); time +/- 300s for WS-vs-ledger clock drift. Restricted to
    source='ws' AND provider_transaction_id IS NULL so we never collide with
    another ledger row."""
    fan_id = parsed["fan_id"]
    amount = parsed["amount_cents"]
    if not fan_id:
        return None
    tol = max(10, amount // 100)
    lo, hi = amount - tol, amount + tol
    t_lo = parsed["occurred_at"] - timedelta(seconds=300)
    t_hi = parsed["occurred_at"] + timedelta(seconds=300)
    res = await s.execute(
        select(Transaction).where(
            Transaction.account_id == account_id,
            Transaction.fan_id == fan_id,
            Transaction.kind == "tip",
            Transaction.source == "ws",
            Transaction.provider_transaction_id.is_(None),
            Transaction.amount_cents >= lo,
            Transaction.amount_cents <= hi,
            Transaction.occurred_at >= t_lo,
            Transaction.occurred_at <= t_hi,
        )
        .order_by(Transaction.occurred_at.desc())
        .limit(1)
    )
    return res.scalar_one_or_none()


async def _link_ppv_message(
    s,
    *,
    account_id: str,
    fan_id: int,
    amount_cents: int,
    occurred_at: datetime,
    tx: Transaction,
) -> None:
    """Heuristic: most-recent unpaid outbound PPV by this fan with matching
    price, sent within _PPV_LINK_LOOKBACK_DAYS before the unlock. Refine
    once we observe real ppv_message ledger rows."""
    cutoff = occurred_at - timedelta(days=_PPV_LINK_LOOKBACK_DAYS)
    res = await s.execute(
        select(Message).where(
            Message.account_id == account_id,
            Message.fan_id == fan_id,
            Message.direction == "out",
            Message.price_cents == amount_cents,
            ((Message.is_paid.is_(None)) | (Message.is_paid.is_(False))),
            Message.created_at >= cutoff,
            Message.created_at <= occurred_at,
        )
        .order_by(Message.created_at.desc())
        .limit(1)
    )
    msg = res.scalar_one_or_none()
    if msg is None:
        return
    msg.is_paid = True
    msg.purchased_at = occurred_at
    tx.message_id = msg.message_id


async def _bump_lifetime(s, account_id: str, fan_id: int, amount_cents: int) -> None:
    fan = await s.get(Fan, (account_id, fan_id))
    if fan is None:
        # PPV-only / non-replying fans never trigger the WS-pump's inbound
        # chat_message path that creates fans rows (event_transcoder.py
        # early-returns on direction=="out"). Without this stub, their
        # lifetime_spend silently stays 0 while the transactions ledger
        # holds the truth — and stats/sort-by-spend rank them as $0.
        # `source="ledger"` flags the row as ledger-originated so a
        # later inbound DM can upsert the OF profile fields cleanly.
        s.add(Fan(
            account_id=account_id,
            fan_id=fan_id,
            source="ledger",
            lifetime_spend_cents=amount_cents,
        ))
        return
    fan.lifetime_spend_cents = (fan.lifetime_spend_cents or 0) + amount_cents
    fan.updated_at = datetime.utcnow()


async def _apply_refund_delta(account_id: str, fan_id: int, prev_amount_cents: int) -> None:
    """SELECT-then-UPDATE on fans.lifetime_spend_cents needs writer-lock
    serialization. SQLite's busy_timeout does NOT cover SQLITE_BUSY_SNAPSHOT
    (a snapshot conflict, not a lock-wait). Open a fresh connection in
    AUTOCOMMIT mode and issue BEGIN IMMEDIATE upfront so the read-modify-write
    pair is atomic against any concurrent writer."""
    await _apply_lifetime_delta(account_id, fan_id, -prev_amount_cents)


async def _apply_lifetime_delta(account_id: str, fan_id: int, delta_cents: int) -> None:
    """Signed-delta sibling of _apply_refund_delta. Same BEGIN IMMEDIATE
    serialization rationale."""
    if delta_cents == 0:
        return
    async with engine.connect() as conn:
        conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
        await conn.execute(text("BEGIN IMMEDIATE"))
        try:
            cur = await conn.execute(
                text(
                    "SELECT lifetime_spend_cents FROM fans "
                    "WHERE account_id=:aid AND fan_id=:fid"
                ),
                {"aid": account_id, "fid": fan_id},
            )
            row = cur.first()
            if row is None:
                await conn.execute(text("COMMIT"))
                return
            new_val = max(0, (row[0] or 0) + delta_cents)
            await conn.execute(
                text(
                    "UPDATE fans SET lifetime_spend_cents=:v, "
                    "updated_at=CURRENT_TIMESTAMP "
                    "WHERE account_id=:aid AND fan_id=:fid"
                ),
                {"v": new_val, "aid": account_id, "fid": fan_id},
            )
            await conn.execute(text("COMMIT"))
        except Exception:
            await conn.execute(text("ROLLBACK"))
            raise


async def _write_one(account_id: str, raw: dict) -> tuple[int, int]:
    """Returns (inserted, patched). One transaction per row. See the module
    docstring for the 3-step dedup contract."""
    parsed = _parse_row(raw)
    if not parsed["provider_id"]:
        log.info("ingest_tx_skip no_provider_id account=%s", account_id)
        return (0, 0)

    raw_kind = _classify_kind(parsed["description"])
    if raw_kind == "unknown":
        log.info(
            "ingest_tx_unknown_kind account=%s provider=%s description=%r",
            account_id, parsed["provider_id"], parsed["description"][:120],
        )

    fingerprint_eligible = raw_kind == "tip" or (
        _ENABLE_PPV_FINGERPRINT and raw_kind.startswith("ppv")
    )

    refund_flip_pending: tuple[int, int] | None = None  # (fan_id, prev_amount)
    lifetime_delta_pending: tuple[int, int] | None = None  # (fan_id, delta_cents)
    broadcast_reason: str | None = None  # set when the row is news, not a re-poll

    async with get_session() as s:
        # Step 1: existing row by provider_id (re-poll, status flip).
        prev = (await s.execute(
            select(Transaction).where(
                Transaction.account_id == account_id,
                Transaction.provider_transaction_id == parsed["provider_id"],
            ).limit(1)
        )).scalar_one_or_none()
        kind = await _maybe_promote_to_rebill(s, account_id, parsed["fan_id"], raw_kind)

        if prev is not None:
            prev_status = prev.status
            prev_amount = prev.amount_cents
            prev.status = parsed["status"]
            prev.cleared_at = parsed["cleared_at"]
            prev.amount_cents = parsed["amount_cents"]
            prev.net_cents = parsed["net_cents"]
            prev.fee_cents = parsed["fee_cents"]
            prev.vat_cents = parsed["vat_cents"]
            prev.description = parsed["description"]
            prev.raw_json = parsed["raw_json"]
            prev.payout_pending_days = parsed["payout_pending_days"]
            if prev_status != parsed["status"]:
                log.info(
                    "ingest_tx_status_flip provider=%s old=%s new=%s",
                    parsed["provider_id"], prev_status, parsed["status"],
                )
                # loading→cleared makes the row appear in ppv-history (it
                # filters status='cleared'); chargebacks remove it. Either
                # way the drawer needs to hear about it. Plain re-polls
                # (no flip) stay silent — they happen every 5-min tick.
                broadcast_reason = "status_flip"
            if (
                parsed["status"] in _REFUND_STATUSES
                and prev_status not in _REFUND_STATUSES
                and parsed["fan_id"]
            ):
                log.info(
                    "ingest_tx_chargeback provider=%s amount=%d",
                    parsed["provider_id"], prev_amount,
                )
                # Defer the fan UPDATE to outside this session so the
                # BEGIN IMMEDIATE in _apply_refund_delta is the only writer
                # touching the fan row in its critical section.
                refund_flip_pending = (parsed["fan_id"], prev_amount)
            # session commits on context exit
            after_session_kind = "patched"

        elif fingerprint_eligible:
            ws_row = await _fingerprint_lookup(s, account_id, parsed)
            if ws_row is not None:
                prev_ws_amount = ws_row.amount_cents or 0
                ws_row.provider_transaction_id = parsed["provider_id"]
                ws_row.source = "ledger"
                ws_row.status = parsed["status"]
                ws_row.cleared_at = parsed["cleared_at"]
                ws_row.amount_cents = parsed["amount_cents"]
                ws_row.net_cents = parsed["net_cents"]
                ws_row.fee_cents = parsed["fee_cents"]
                ws_row.vat_cents = parsed["vat_cents"]
                ws_row.description = parsed["description"]
                ws_row.raw_json = parsed["raw_json"]
                ws_row.payout_pending_days = parsed["payout_pending_days"]
                log.info(
                    "ingest_tx_fingerprint_match account=%s fan=%s "
                    "provider=%s amount=%d kind=%s prev_amount=%d",
                    account_id, parsed["fan_id"], parsed["provider_id"],
                    parsed["amount_cents"], kind, prev_ws_amount,
                )
                # WS pump already bumped lifetime_spend_cents by prev_ws_amount
                # at event_transcoder.py:378-381. Reconcile against the
                # authoritative ledger amount. Skip if status went non-revenue
                # (rare on first ledger sight of a tip — out of scope here).
                if (
                    parsed["fan_id"]
                    and parsed["status"] not in _NON_REVENUE_STATUSES
                    and parsed["amount_cents"] != prev_ws_amount
                ):
                    lifetime_delta_pending = (
                        parsed["fan_id"],
                        parsed["amount_cents"] - prev_ws_amount,
                    )
                broadcast_reason = "ledger_promote"
                after_session_kind = "patched"
            else:
                after_session_kind = "inserted"
        else:
            after_session_kind = "inserted"

        if prev is None and after_session_kind == "inserted":
            new_tx = Transaction(
                account_id=account_id,
                fan_id=parsed["fan_id"],
                kind=kind,
                message_id=None,
                amount_cents=parsed["amount_cents"],
                currency=parsed["currency"],
                occurred_at=parsed["occurred_at"],
                raw_json=parsed["raw_json"],
                provider_transaction_id=parsed["provider_id"],
                status=parsed["status"],
                cleared_at=parsed["cleared_at"],
                payout_pending_days=parsed["payout_pending_days"],
                net_cents=parsed["net_cents"],
                fee_cents=parsed["fee_cents"],
                vat_cents=parsed["vat_cents"],
                description=parsed["description"],
                source="ledger",
            )
            s.add(new_tx)
            if kind == "ppv_message" and parsed["fan_id"]:
                await _link_ppv_message(
                    s,
                    account_id=account_id,
                    fan_id=parsed["fan_id"],
                    amount_cents=parsed["amount_cents"],
                    occurred_at=parsed["occurred_at"],
                    tx=new_tx,
                )
            if (
                parsed["fan_id"]
                and parsed["status"] not in _NON_REVENUE_STATUSES
            ):
                await _bump_lifetime(
                    s, account_id, parsed["fan_id"], parsed["amount_cents"]
                )
            broadcast_reason = "insert"

    # Apply the chargeback delta after the row UPDATE committed. BEGIN
    # IMMEDIATE inside _apply_refund_delta serializes against any other
    # writer. Tradeoff: if this call fails between the two, the row stays
    # marked chargedback but the fan counter is stale; the next tick won't
    # re-fire (prev_status == new_status), so manual repair would be needed.
    # Acceptable at our scale; surfaced via the chargeback log line above.
    if refund_flip_pending is not None:
        fan_id, prev_amount = refund_flip_pending
        await _apply_refund_delta(account_id, fan_id, prev_amount)

    # Same BEGIN IMMEDIATE rationale as the refund path. WS pump's original
    # bump used the WS-observed amount; ledger arrived with the canonical
    # amount and we patched the row above, so reconcile the counter too.
    if lifetime_delta_pending is not None:
        fan_id, delta = lifetime_delta_pending
        await _apply_lifetime_delta(account_id, fan_id, delta)

    # Announce news (insert / promote / status flip) to SSE subscribers AFTER
    # the commit, so the browser's refetch can't race the write. The frontend
    # (useInboxRealtime) drops the fan drawer's revenue caches on this event —
    # without it the chart/Sales panel sits on 5-30 min staleTimes after a
    # sale.
    #
    # The 48h recency cap applies to INSERTS only: a 90-day cold backfill
    # inserts hundreds of ancient rows and we don't want a drawer invalidation
    # per row nobody is looking at. But status flips (a held PPV settling
    # pending→cleared days after purchase) and ledger promotes only happen on
    # rows we've already seen inside the 7-day refresh window — naturally
    # bounded, AND they're exactly the update the drawer is waiting on. Gating
    # those on `occurred_at >= now-48h` silently dropped every settle-flip for
    # PPVs older than 2 days, so the drawer never heard about them.
    is_recent_insert = (
        parsed["occurred_at"] is not None
        and parsed["occurred_at"] >= datetime.utcnow() - timedelta(hours=48)
    )
    if (
        broadcast_reason is not None
        and parsed["fan_id"]
        and (broadcast_reason != "insert" or is_recent_insert)
    ):
        try:
            from events import broadcast as _sse_broadcast
            await _sse_broadcast({
                "transaction_recorded": {
                    "fan_id": int(parsed["fan_id"]),
                    "kind": kind,
                    "amount_cents": parsed["amount_cents"],
                    "status": parsed["status"],
                    "reason": broadcast_reason,
                },
                "__account_id": str(account_id),
            })
        except Exception:
            log.debug("transaction_recorded broadcast failed", exc_info=True)

    if prev is not None:
        return (0, 1)
    if after_session_kind == "patched":
        return (0, 1)
    return (1, 0)


# ── Supervisor / tick ───────────────────────────────────────────────

def _client_for(account_id: str) -> OFClient | None:
    try:
        return OFClient.from_account(account_id)
    except Exception:
        return None


async def _scan_state(s, account_id: str) -> TransactionScanHistory:
    row = await s.get(TransactionScanHistory, account_id)
    if row is None:
        row = TransactionScanHistory(account_id=account_id)
        s.add(row)
        await s.flush()
    return row


async def _is_paused(account_id: str) -> bool:
    async with get_session() as s:
        row = await s.get(TransactionScanHistory, account_id)
        if row is None or row.paused_until is None:
            return False
        return row.paused_until > datetime.utcnow()


async def _decide_mode(account_id: str) -> str:
    async with get_session() as s:
        row = await s.get(TransactionScanHistory, account_id)
        if row is None or not row.fully_backfilled:
            return "backfill"
        return "refresh"


def _window_for(mode: str, state: TransactionScanHistory) -> tuple[datetime, datetime]:
    """refresh: trailing 7d (5d pending + 2d safety).
    backfill: walk from oldest_seen_occurred_at (or now) BACK to backfill_floor."""
    now = datetime.utcnow()
    if mode == "refresh":
        return (now - timedelta(days=_ROLLING_WINDOW_DAYS), now)
    floor = state.backfill_floor or (now - timedelta(days=_DEFAULT_BACKFILL_DAYS))
    end = state.oldest_seen_occurred_at or now
    return (floor, end)


async def _mark_interrupted(account_id: str) -> None:
    """Cleanup path for run_one_tick when neither _mark_success nor
    _record_failure ran — typically CancelledError on shutdown.

    Sets status='idle' (not 'error') so we don't auto-pause after three
    process restarts; consecutive_failures is left untouched because this
    isn't an ingest failure, it's a process interruption. Only writes if
    the row is still parked at the start-of-tick stamp, so it's safe to
    call redundantly."""
    try:
        async with get_session() as s:
            row = await _scan_state(s, account_id)
            if row.current_status in ("refreshing", "backfilling"):
                prior_status = row.current_status
                row.current_status = "idle"
                row.last_error = "interrupted_mid_tick"
                row.updated_at = datetime.utcnow()
                log.warning(
                    "ingest_tx_interrupted_cleanup account=%s prior_status=%s",
                    account_id, prior_status,
                )
    except BaseException:
        # Best-effort. If THIS cleanup itself is cancelled, the boot-time
        # sweep in start_supervisor() picks up the stuck row next process.
        log.warning(
            "ingest_tx_interrupted_cleanup_failed account=%s",
            account_id, exc_info=True,
        )


async def _clear_stale_inflight() -> int:
    """Boot-time sweep. Any row parked at current_status IN
    ('refreshing','backfilling') with a last_scan_at older than 60s is by
    definition stale — the asyncio.Lock that protected it lived in the
    previous process. Reset to 'idle' so the next supervisor tick +
    manual /run can proceed.

    The 60s lower bound guards against a real-but-young scan that was
    spawned via admin /run in the few ms between process boot and the
    supervisor task getting CPU time. A real scan refreshes last_scan_at
    well within 60s; only crashed/restarted scans drift past it.

    Returns count of rows reset (for the startup log)."""
    cutoff = datetime.utcnow() - timedelta(seconds=60)
    async with get_session() as s:
        rows = (await s.execute(
            select(TransactionScanHistory).where(
                TransactionScanHistory.current_status.in_(("refreshing", "backfilling")),
                # last_scan_at IS NULL would only happen on a row that
                # somehow got status stamped without a timestamp — treat
                # it as stale too (the OR catches NULL since SQL NULL
                # comparisons against `<` are FALSE).
                (TransactionScanHistory.last_scan_at < cutoff)
                | (TransactionScanHistory.last_scan_at.is_(None)),
            )
        )).scalars().all()
        for row in rows:
            log.warning(
                "ingest_tx_boot_clear_stale account=%s status=%s last_scan_at=%s",
                row.account_id, row.current_status,
                row.last_scan_at.isoformat() if row.last_scan_at else None,
            )
            row.current_status = "idle"
            row.last_error = "interrupted_before_boot"
            row.updated_at = datetime.utcnow()
        return len(rows)


async def _record_failure(account_id: str, exc: Exception) -> None:
    async with get_session() as s:
        row = await _scan_state(s, account_id)
        row.consecutive_failures = (row.consecutive_failures or 0) + 1
        row.last_error = repr(exc)[:500]
        row.updated_at = datetime.utcnow()
        if row.consecutive_failures >= _FAILS_BEFORE_PAUSE:
            row.paused_until = datetime.utcnow() + timedelta(seconds=_FAIL_BACKOFF_S)
            row.current_status = "paused"
            log.info(
                "ingest_tx_account_paused account=%s consecutive_failures=%d "
                "last_error=%s",
                account_id, row.consecutive_failures, row.last_error,
            )
        else:
            row.current_status = "error"


async def _mark_success(
    account_id: str,
    *,
    mode: str,
    inserted: int,
    patched: int,
    last_marker: int | None,
    pages: int,
    page_rows_min_occurred_at: datetime | None,
    page_rows_max_occurred_at: datetime | None,
    floor_reached: bool,
) -> None:
    async with get_session() as s:
        row = await _scan_state(s, account_id)
        row.last_scan_at = datetime.utcnow()
        row.consecutive_failures = 0
        row.last_error = None
        if last_marker is not None:
            row.last_marker = last_marker
        if page_rows_min_occurred_at is not None and (
            row.oldest_seen_occurred_at is None
            or page_rows_min_occurred_at < row.oldest_seen_occurred_at
        ):
            row.oldest_seen_occurred_at = page_rows_min_occurred_at
        if page_rows_max_occurred_at is not None and (
            row.newest_seen_occurred_at is None
            or page_rows_max_occurred_at > row.newest_seen_occurred_at
        ):
            row.newest_seen_occurred_at = page_rows_max_occurred_at
        row.rows_inserted_total = (row.rows_inserted_total or 0) + inserted
        row.rows_patched_total = (row.rows_patched_total or 0) + patched
        # Distinguish "actively scanning" from "tick completed". The
        # tick START stamps current_status='refreshing'/'backfilling';
        # success stamps 'ok' so the /run endpoint's collision check
        # (status IN ('refreshing','backfilling')) only fires when a
        # tick is genuinely in flight. Before this, every successful
        # refresh left status='refreshing' and locked out user
        # re-syncs until the next supervisor tick.
        if mode == "backfill" and floor_reached:
            row.fully_backfilled = True
            row.current_status = "ok"
        elif mode == "backfill":
            # Still walking history → keep the long-form label so the
            # banner reflects "still working" rather than "done".
            row.current_status = "backfilling"
        else:
            row.current_status = "ok"
        row.updated_at = datetime.utcnow()


async def run_one_tick(account_id: str, *, mode: str = "refresh") -> dict:
    """One supervisor tick for one account.

    refresh:  window = (utcnow - 7d, utcnow)
    backfill: window = (backfill_floor, oldest_seen_occurred_at or utcnow)

    Sleeps 0.4s between pages. Stops on empty page OR hasMore=false. Bounded
    by a per-account asyncio.Lock so a slow (>600s) tick can't stack with the
    next supervisor cycle. Returns
    {"rows_inserted": int, "rows_patched": int, "pages": int,
     "duration_ms": int, "error": str|None}."""
    lock = _tick_locks[account_id]
    async with lock:
        t0 = time.monotonic()
        # `finalized` tracks whether SOMETHING has overwritten the
        # start-of-tick stamp (success path → _mark_success, error path →
        # _record_failure, skip-on-stale → never stamped). If false at
        # finally entry we run _mark_interrupted as last-resort cleanup.
        # Wraps the start-stamp session too so a CancelledError landing
        # inside `async with get_session().__aexit__` (i.e., during the
        # commit await) still gets cleaned up.
        finalized = False
        try:
            async with get_session() as s:
                state = await _scan_state(s, account_id)
                # Race guard re-check inside the lock. The previous
                # `lock.locked()` peek before `async with` was racy: two
                # callers (admin /run + supervisor, or two clicks) could
                # both observe False and both queue into the lock, with the
                # second re-walking the full window after the first
                # finished. Serializing then re-reading current_status
                # catches the case where an in-flight peer (or crashed
                # prior holder) has already stamped the account.
                if state.current_status in ("refreshing", "backfilling"):
                    # Stale-timestamp escape valve. A real in-flight tick
                    # holds the lock too, so it can't get here. Anything
                    # parked at this status is from a *prior* process or
                    # crashed task. last_scan_at was stamped at tick START
                    # (see below), so its age == how long the stamp has been
                    # orphaned. Past _STALE_INFLIGHT_S it's a tombstone.
                    age_s = (
                        (datetime.utcnow() - state.last_scan_at).total_seconds()
                        if state.last_scan_at is not None
                        else _STALE_INFLIGHT_S + 1
                    )
                    if age_s < _STALE_INFLIGHT_S:
                        log.info(
                            "ingest_tx_scan_skip account=%s status=%s age_s=%.0f",
                            account_id, state.current_status, age_s,
                        )
                        # Skip path didn't touch the stamp, so we mark
                        # finalized=True to suppress the finally cleanup.
                        finalized = True
                        return {
                            "skipped": True, "rows_inserted": 0, "rows_patched": 0,
                            "pages": 0, "duration_ms": int((time.monotonic() - t0) * 1000),
                            "error": None,
                        }
                    log.warning(
                        "ingest_tx_scan_proceed_after_stale account=%s status=%s age_s=%.0f",
                        account_id, state.current_status, age_s,
                    )
                start_dt, end_dt = _window_for(mode, state)
                if mode == "backfill" and state.backfill_floor is None:
                    state.backfill_floor = start_dt
                # Stamp last_scan_at AND current_status at tick START.
                # last_scan_at: dashboard tier reflects "actively scanning"
                #   instead of the previous tick's completion stamp.
                # current_status: admin /run endpoint pre-checks
                #   current_status in ("refreshing","backfilling") to
                #   return 409 on collision. Without the early write, the
                #   schema default 'idle' makes the first tick of an
                #   account invisible to the 409 check.
                # _mark_success / _record_failure both overwrite at end,
                # AND the try/finally below catches CancelledError so the
                # stamp can't stick across process restarts.
                state.last_scan_at = datetime.utcnow()
                state.current_status = "backfilling" if mode == "backfill" else "refreshing"

            # Start stamp is committed. Every exit path below must
            # overwrite it.
            client = _client_for(account_id)
            if client is None:
                err = RuntimeError("no_session")
                log.warning("ingest_tx_no_session account=%s", account_id)
                await _record_failure(account_id, err)
                finalized = True
                return {
                    "rows_inserted": 0, "rows_patched": 0, "pages": 0,
                    "duration_ms": int((time.monotonic() - t0) * 1000),
                    "error": "no_session",
                }

            pages = inserted = patched = 0
            offset = 0
            last_marker: int | None = None
            rows_min_ts: datetime | None = None
            rows_max_ts: datetime | None = None
            floor_reached = False

            try:
                log.info(
                    "ingest_tx_scan_start account=%s mode=%s start=%s end=%s",
                    account_id, mode, start_dt.isoformat(), end_dt.isoformat(),
                )
                while True:
                    # NO `type=` — capture proves it's a filter, not a partition
                    # (synthesis §2 row 5). One unfiltered request per page.
                    resp = await asyncio.to_thread(
                        client.transactions,
                        limit=_PAGE_LIMIT,
                        offset=offset,
                        start=start_dt.isoformat(),
                        end=end_dt.isoformat(),
                    )
                    rows = resp.get("list") or []
                    marker = resp.get("nextMarker")
                    if marker is not None:
                        last_marker = marker
                    log.info(
                        "ingest_tx_page_fetched account=%s page=%d count=%d marker=%s",
                        account_id, pages, len(rows), marker,
                    )
                    if not rows:
                        floor_reached = True
                        break
                    for raw in rows:
                        occ = _parse_iso(raw.get("createdAt"))
                        if occ is not None:
                            if rows_min_ts is None or occ < rows_min_ts:
                                rows_min_ts = occ
                            if rows_max_ts is None or occ > rows_max_ts:
                                rows_max_ts = occ
                        try:
                            ins, pat = await _write_one(account_id, raw)
                        except Exception:
                            log.warning(
                                "ingest_tx_write_failed account=%s provider=%s",
                                account_id, raw.get("id"), exc_info=True,
                            )
                            continue
                        inserted += ins
                        patched += pat
                    pages += 1
                    if not resp.get("hasMore"):
                        floor_reached = True
                        break
                    offset += len(rows)
                    await asyncio.sleep(_PAGE_SLEEP_S)
            except Exception as e:
                log.warning("ingest_tx_scan_failed account=%s", account_id, exc_info=True)
                await _record_failure(account_id, e)
                finalized = True
                return {
                    "rows_inserted": inserted, "rows_patched": patched,
                    "pages": pages,
                    "duration_ms": int((time.monotonic() - t0) * 1000),
                    "error": repr(e),
                }

            await _mark_success(
                account_id,
                mode=mode,
                inserted=inserted,
                patched=patched,
                last_marker=last_marker,
                pages=pages,
                page_rows_min_occurred_at=rows_min_ts,
                page_rows_max_occurred_at=rows_max_ts,
                floor_reached=floor_reached,
            )
            finalized = True
            dur_ms = int((time.monotonic() - t0) * 1000)
            log.info(
                "ingest_tx_scan_end account=%s mode=%s pages=%d inserted=%d "
                "patched=%d duration_ms=%d",
                account_id, mode, pages, inserted, patched, dur_ms,
            )
            return {
                "rows_inserted": inserted, "rows_patched": patched,
                "pages": pages, "duration_ms": dur_ms, "error": None,
            }
        finally:
            # Catches CancelledError (uvicorn reload, deploy, supervisor
            # cancel) and any BaseException so the start-of-tick stamp
            # doesn't strand the account at 'refreshing'/'backfilling'.
            # `finalized` is True iff _mark_success or _record_failure
            # already overwrote the stamp.
            if not finalized:
                await _mark_interrupted(account_id)


async def _online_account_ids() -> set[str]:
    """Union of OF accounts visible to any principal active within the last
    _ONLINE_WINDOW_S seconds.

    Sources combined:
      • owners (users.last_seen_at) → user_accounts.account_id
      • chatters (chatters.last_seen_at + is_active=True) →
            chatter_users → user_accounts.account_id

    Empty set ⇒ nobody's online; the supervisor cycle skips this tick so
    we don't hammer broken sessions while every dashboard is closed. The
    /admin/ingest/transactions/{aid}/run admin endpoint stays unaffected —
    a manual re-sync is an explicit user gesture and bypasses this gate.
    """
    cutoff = datetime.utcnow() - timedelta(seconds=_ONLINE_WINDOW_S)
    async with get_session() as s:
        owner_rows = (await s.execute(
            select(UserAccount.account_id)
            .join(User, User.id == UserAccount.user_id)
            .where(User.last_seen_at >= cutoff)
        )).all()
        chatter_rows = (await s.execute(
            select(UserAccount.account_id)
            .join(ChatterUser, ChatterUser.user_id == UserAccount.user_id)
            .join(Chatter, Chatter.id == ChatterUser.chatter_id)
            .where(Chatter.last_seen_at >= cutoff)
            .where(Chatter.is_active.is_(True))
        )).all()
    return {r[0] for r in owner_rows} | {r[0] for r in chatter_rows}


async def trigger_backfill(account_id: str, *, since: datetime | None = None) -> None:
    """Reset backfill bookkeeping so the next supervisor tick walks back to
    `since` (defaults to utcnow - 90d). Idempotent."""
    floor = since or (datetime.utcnow() - timedelta(days=_DEFAULT_BACKFILL_DAYS))
    async with get_session() as s:
        row = await _scan_state(s, account_id)
        row.backfill_floor = floor
        row.fully_backfilled = False
        row.current_status = "backfilling"
        row.paused_until = None
        row.consecutive_failures = 0
        row.updated_at = datetime.utcnow()


async def start_supervisor() -> None:
    """Async supervisor loop. Mirror of `_perflog_evictor_loop` shape
    (service/server.py:1374-1394): sleep inside try, return on
    CancelledError, log + continue on other exceptions.

    Refresh ticks strictly sequential (SQLite WAL serializes writes anyway,
    and we want bounded concurrent OF load). Backfill ticks may run up to
    _BACKFILL_CONCURRENCY in parallel — synthesis review §4: a 30-account
    cold-start drops from ~100min to ~25min."""
    log.info(
        "ingest_tx_supervisor_started tick=%ds window=%dd backfill=%dd",
        _TICK_INTERVAL_S, _ROLLING_WINDOW_DAYS, _DEFAULT_BACKFILL_DAYS,
    )
    # Boot-time sweep: clear any rows parked at 'refreshing'/'backfilling'
    # from the previous process. The asyncio.Lock that protected them is
    # gone; the stamp is by definition a tombstone. Without this sweep
    # both the /run endpoint's 409 guard and the lock-internal guard in
    # run_one_tick lock the account out until the stale-age escape (15min)
    # kicks in. Sweep makes the recovery instant.
    try:
        cleared = await _clear_stale_inflight()
        if cleared:
            log.warning("ingest_tx_supervisor_boot_cleared count=%d", cleared)
    except Exception:
        # A failed sweep doesn't block startup; the stale-age escape
        # valve is the second line of defense.
        log.warning("ingest_tx_supervisor_boot_sweep_failed", exc_info=True)
    # Sleep AT END so the inter-scan gap is _TICK_INTERVAL_S regardless
    # of how long the cycle took. Original code slept first, so a slow
    # cycle pushed the last-account stamp to (_TICK_INTERVAL_S + duration)
    # past the prior cycle — for sequential 3-account walks that drifted
    # straight past the 30-min green-tier ceiling.
    while True:
        try:
            cycle_started = time.monotonic()
            account_ids = [
                m["id"] for m in account_registry.list_accounts()
                if m.get("has_session")
            ]
            # Gate the cycle on online principals — see _online_account_ids
            # docstring. Idle dashboard ⇒ no scan, no proxy burn.
            online = await _online_account_ids()
            account_ids = [aid for aid in account_ids if aid in online]
            if not account_ids:
                elapsed = time.monotonic() - cycle_started
                await asyncio.sleep(max(0.0, _TICK_INTERVAL_S - elapsed))
                continue
            refresh_aids: list[str] = []
            backfill_aids: list[str] = []
            for aid in account_ids:
                if await _is_paused(aid):
                    continue
                if (await _decide_mode(aid)) == "backfill":
                    backfill_aids.append(aid)
                else:
                    refresh_aids.append(aid)

            for aid in refresh_aids:
                try:
                    await run_one_tick(aid, mode="refresh")
                except Exception as e:
                    log.warning("ingest_tx_scan failed account=%s", aid, exc_info=True)
                    await _record_failure(aid, e)

            if backfill_aids:
                sem = asyncio.Semaphore(_BACKFILL_CONCURRENCY)

                async def _bf(aid: str) -> None:
                    async with sem:
                        try:
                            await run_one_tick(aid, mode="backfill")
                        except Exception as e:
                            log.warning(
                                "ingest_tx_backfill failed account=%s",
                                aid, exc_info=True,
                            )
                            await _record_failure(aid, e)

                await asyncio.gather(*(_bf(a) for a in backfill_aids))

            elapsed = time.monotonic() - cycle_started
            await asyncio.sleep(max(0.0, _TICK_INTERVAL_S - elapsed))
        except asyncio.CancelledError:
            return
        except Exception:
            log.warning("ingest supervisor cycle failed", exc_info=True)
            await asyncio.sleep(_TICK_INTERVAL_S)
