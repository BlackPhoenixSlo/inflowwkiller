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
import os
import re
import time
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

import accounts as account_registry
from attribution_backfill import attribute_orphans
from cadence import boot_grace
import client_pool
from db.engine import engine, get_session
from db.models import (
    Account,
    AutomationRule,
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
import ownership
import purchase_notifications

log = logging.getLogger("of-relay.ingest_tx")


def _env_float(name: str, default: float) -> float:
    """Read a float knob from the environment. A malformed value must not take
    the relay down at import time — it degrades to the default, loudly."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        log.warning("ingest_bad_env name=%s value=%r — using default %.0f",
                    name, raw, default)
        return default


# ── Knobs ────────────────────────────────────────────────────────────
_TICK_INTERVAL_S = 300                  # 5 minutes (was 600 — too slow vs the 30m green tier ceiling)
_PAGE_SLEEP_S = 0.4
_ROLLING_WINDOW_DAYS = 7
_DEFAULT_BACKFILL_DAYS = 90
_FAIL_BACKOFF_S = 3600                  # auto-pause for 1h after 3 fails
_PAGE_LIMIT = 100                       # OF max per request
_BACKFILL_CONCURRENCY = 4               # synthesis review §4
# Minimum gap between cycles, ALWAYS honoured — the anti-spin floor.
# Both loops below sleep `interval - elapsed` so the inter-scan GAP stays
# constant regardless of cycle duration (that fix is why they sleep at end
# rather than at start). But `max(0.0, ...)` means a cycle that OVERRUNS its
# interval sleeps zero and re-enters immediately — a hot loop that pegs the
# CPU and never backs off. It is self-sustaining: the spin starves the event
# loop and the SQLite writer, which makes the next cycle slower still, so the
# sleep stays zero forever. Observed 2026-08-21: relay at ~102% CPU on a
# 2-vCPU box, load 7.48, and every request's `last_seen_at` bump timing out
# on the 30 s busy_timeout ("database is locked") because the writer never
# got a gap. Recovery required the backlog to drain by luck.
# So: overrunning yields the floor, never zero. Fast cycles are unaffected
# (interval - elapsed is far larger than the floor), so the anti-drift intent
# is preserved exactly.
_MIN_CYCLE_GAP_S = 5.0
# Boot grace — how long each loop below holds its FIRST cycle after process
# start. `_MIN_CYCLE_GAP_S` bounds a cycle that OVERRUNS; this bounds the one
# that starts too early, which the floor cannot see. At t=0 the relay is still
# spawning the WS pumps, the pump supervisor, the automation executor's
# immediate first drain and the fleet-wide scrape_chats seed, so a full-fleet
# ledger walk fired at t=0 competes with the startup it is part of — and a
# RESTART is precisely what produced the 2026-08-21 CPU cap (three inside
# ~64 min). Costs at most one cycle of ledger freshness on a boot and nothing
# at all in steady state. Zero disables, for a recovery boot where freshness
# beats politeness. See cadence.boot_grace.
_BOOT_GRACE_S = _env_float("INGEST_BOOT_GRACE_S", 90.0)
_FAST_BOOT_GRACE_S = _env_float("INGEST_FAST_BOOT_GRACE_S", 30.0)
_PPV_LINK_LOOKBACK_DAYS = 30
_FAILS_BEFORE_PAUSE = 3
# Per-tick orphan-attribution sweep window (see attribution_backfill). Covers
# a refresh scan's 7-day window plus slack for late-clearing pending rows.
_ATTR_SWEEP_SINCE_DAYS = 14
# Wall-post ownership belt: keep re-reading the purchases feed this long after
# the account's last ppv_post sale (cost gate for _has_recent_post_txn;
# stamping is idempotent). Deliberately its own knob — tuning the attribution
# window above must not silently retune this safety gate.
_POST_OWNERSHIP_SINCE_DAYS = 14
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
    ("Post purchase by ",            "ppv_post"),       # OF's actual ledger prefix for feed-post unlocks
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


# Pathological-case guard for the linker's candidate fetch. A fan's same-price
# outbound messages in a 30-day window are normally a handful; if we ever see
# this many, the case is ambiguous by definition and we refuse to guess.
_PPV_LINK_CANDIDATE_CAP = 50


async def _link_ppv_message(
    s,
    *,
    account_id: str,
    fan_id: int,
    amount_cents: int,
    occurred_at: datetime,
    tx: Transaction,
    stamp_paid: bool = True,
) -> bool:
    """Attach a `ppv_message` ledger row to the outbound PPV it paid for — the
    message whose `sent_by_employee_id` ("Sent by …") then credits the sale in
    the per-employee view.

    Candidates: same (account, fan), outbound, EXACT price match, sent within
    _PPV_LINK_LOOKBACK_DAYS before the unlock, and not already claimed by
    another transaction. The claim guard is kind-agnostic on purpose — it
    mirrors the `uq_tx_msg` unique index, where a second claim of any kind is
    a constraint violation.

    Deliberately does NOT require the message to still be unpaid: the WS pump
    flips `is_paid` within seconds of an unlock while this ledger tick runs
    every ~5 minutes, so the purchased message is usually ALREADY paid by the
    time the money shows up — the old unpaid filter orphaned exactly the
    fastest buyers (or worse, matched a stale same-price message). Instead the
    flip is a POSITIVE signal: it marks which message the fan actually opened.

    Links ONLY when unambiguous: exactly one candidate, or exactly one
    candidate carrying the is_paid flag. Anything murkier stays unlinked for
    the Sales-needing-attribution panel (ops' "clear cases only" rule).
    Sequential unlocks still auto-link — each prior unlock's message is
    already claimed and excluded by the guard when the next tick fires.

    `stamp_paid=False` (historical backfill) writes ONLY `tx.message_id` — no
    message mutation, so bulk relinks have zero downstream side effects
    (funnels / offer watcher / bought-media dedupe all read `is_paid`) and
    reversal is a single UPDATE nulling the journaled message_ids.

    Returns True iff a link was made.
    """
    msg, _reason = await _select_ppv_link_candidate(
        s,
        account_id=account_id,
        fan_id=fan_id,
        amount_cents=amount_cents,
        occurred_at=occurred_at,
    )
    if msg is None:
        return False
    if stamp_paid:
        msg.is_paid = True
        msg.purchased_at = occurred_at
    tx.message_id = msg.message_id
    return True


async def _select_ppv_link_candidate(
    s,
    *,
    account_id: str,
    fan_id: int,
    amount_cents: int,
    occurred_at: datetime,
) -> tuple[Message | None, str]:
    """Pure READ: pick the message a PPV payment belongs to, or refuse.
    Returns (message, 'ok') on an unambiguous pick, else (None, reason) with
    reason in {'no_candidate', 'ambiguous'}. Shared by the live linker and the
    relink sweep's dry-run so the reported plan is exactly what apply would do.
    """
    cutoff = occurred_at - timedelta(days=_PPV_LINK_LOOKBACK_DAYS)
    already_claimed = (
        select(Transaction.id)
        .where(
            Transaction.account_id == Message.account_id,
            Transaction.fan_id == Message.fan_id,
            Transaction.message_id == Message.message_id,
        )
        .exists()
    )
    res = await s.execute(
        select(Message).where(
            Message.account_id == account_id,
            Message.fan_id == fan_id,
            Message.direction == "out",
            Message.price_cents == amount_cents,
            Message.created_at >= cutoff,
            Message.created_at <= occurred_at,
            ~already_claimed,
        )
        .order_by(Message.created_at.desc())
        .limit(_PPV_LINK_CANDIDATE_CAP)
    )
    candidates = list(res.scalars().all())
    if not candidates or len(candidates) >= _PPV_LINK_CANDIDATE_CAP:
        return (None, "no_candidate" if not candidates else "ambiguous")
    if len(candidates) == 1:
        return (candidates[0], "ok")
    paid = [m for m in candidates if m.is_paid]
    if len(paid) != 1:
        return (None, "ambiguous")  # leave for the manual panel
    return (paid[0], "ok")


# Synthetic message_id band for LEDGER-ingested chat tips the WS pump never saw
# (so no real chat_message row exists). Sits ABOVE the mass-placeholder band
# (5e15, attribution._MASS_PLACEHOLDER_BASE) and BELOW JS's 2**53 safe-integer
# ceiling (9.007e15), so the id round-trips through the Number-typed frontend
# `message_id` without precision loss and can't collide with a real OF id
# (~9.9e12) or a mass placeholder. id = base + transactions.id (the globally
# unique autoincrement PK) → stable + unique per (account, fan). NOT a sort key:
# the per-chat thread orders by created_at, so the tip lands in its true
# chronological slot no matter where this id sorts.
_TIP_MSG_ID_BASE = 6_000_000_000_000_000


async def _synthesize_tip_message(
    s,
    *,
    account_id: str,
    fan_id: int,
    amount_cents: int,
    occurred_at: datetime,
    tx: Transaction,
) -> None:
    """Write an INBOUND `messages` row for a chat tip that only exists in the
    ledger, so the tip shows INLINE in the chat thread instead of only in the
    revenue panel.

    Only called on a FRESH ledger insert of a chat `kind='tip'` with no WS
    sibling (the fingerprint step found none) — so there's no real chat_message
    row to duplicate. Idempotent via on_conflict_do_nothing on the composite PK.

    The tip $ goes in the BODY, not price_cents (which stays 0, mirroring the WS
    tip row): the frontend reads a bubble's price from price_cents and a non-zero
    value would paint the tip as a locked PPV. `is_tip=True` paints it blue.

    Deliberately does NOT touch `chats` (preview/unread): a cold 90-day backfill
    replays old tips and must not resurrect the inbox badge; the message row
    alone satisfies "show the tip inline". Never advances live SSE either — the
    5-min ledger poll isn't a live path (the WS pump owns that).
    """
    # Flush so the autoincrement tx.id is populated for the synthetic id.
    await s.flush()
    if tx.id is None:
        return
    synth_id = _TIP_MSG_ID_BASE + int(tx.id)
    amount = (amount_cents or 0) / 100
    body = f"💸 Sent a ${amount:.2f} tip"
    stmt = sqlite_insert(Message).values(
        account_id=str(account_id),
        fan_id=int(fan_id),
        message_id=synth_id,
        direction="in",
        sender_name="",
        body=body,
        media_ids="[]",
        media_count=0,
        price_cents=0,
        is_paid=None,
        is_tip=True,
        raw_json=None,
        created_at=occurred_at,
        ingested_at=datetime.utcnow(),
    ).on_conflict_do_nothing(
        index_elements=["account_id", "fan_id", "message_id"],
    )
    await s.execute(stmt)
    log.info("ingest_tx_synth_tip_msg account=%s fan=%s tx=%s msg=%s cents=%d",
             account_id, fan_id, tx.id, synth_id, amount_cents or 0)


async def relink_orphan_ppvs(
    *,
    account_id: str | None = None,
    since: datetime | None = None,
    dry_run: bool = False,
    limit: int = 500,
    newest_first: bool = True,
    after: tuple[str, int] | None = None,
    stamp_paid: bool = True,
) -> dict[str, Any]:
    """Retry the payment→message link for orphaned chat PPVs
    (`kind='ppv_message'`, `message_id IS NULL`). Fill-only: enumerates only
    message-NULL rows and never touches `attributed_employee_id`, so manual
    assignments and existing links are untouchable by construction.

    Two callers, two shapes:
      • per-tick sweep (run_one_tick): account + recent `since` window,
        `newest_first=True` + bounded `limit` — fresh linkable rows are always
        reached even if the window holds permanently-ambiguous rows.
      • one-shot backfill: `newest_first=False` with `after` as a keyset
        cursor `(occurred_at_iso, id)` — chunked traversal of ALL history,
        oldest first, `stamp_paid=False` (attribution only, zero message-state
        side effects).

    CONCURRENCY: each row gets its OWN session + immediate commit — write
    locks stay short against the live WS pump/ticks and a failure loses one
    row, not the batch. An IntegrityError (uq_tx_msg race with a live tick
    claiming the same message) rolls back that row only → `conflict_skipped`.

    DRY RUN is genuinely read-only: it runs the same candidate selector and
    reports what apply WOULD do, tracking would-be claims in memory so two
    orphans can't both "plan" the same message (such repeats count as
    `conflict_skipped`).

    Returns {examined, linked, ambiguous_skipped, no_candidate,
    conflict_skipped, journal: [{tx_id, message_id}], next_after} —
    `next_after` is the keyset cursor for the next chunk (None = exhausted).
    """
    from sqlalchemy.exc import IntegrityError
    from sqlalchemy import tuple_ as sa_tuple

    stats: dict[str, Any] = {
        "examined": 0, "linked": 0, "ambiguous_skipped": 0,
        "no_candidate": 0, "conflict_skipped": 0,
        "journal": [], "next_after": None, "dry_run": dry_run,
    }

    q = (
        select(
            Transaction.id, Transaction.account_id, Transaction.fan_id,
            Transaction.amount_cents, Transaction.occurred_at,
        )
        .where(
            Transaction.kind == "ppv_message",
            Transaction.message_id.is_(None),
            Transaction.fan_id.is_not(None),
            Transaction.status.in_(("cleared", "pending")),
        )
    )
    if account_id is not None:
        q = q.where(Transaction.account_id == account_id)
    if since is not None:
        q = q.where(Transaction.occurred_at >= since)
    if newest_first:
        q = q.order_by(Transaction.occurred_at.desc(), Transaction.id.desc())
    else:
        q = q.order_by(Transaction.occurred_at.asc(), Transaction.id.asc())
        if after is not None:
            q = q.where(
                sa_tuple(Transaction.occurred_at, Transaction.id)
                > (datetime.fromisoformat(after[0]), after[1])
            )
    q = q.limit(limit)

    async with get_session() as s:
        rows = (await s.execute(q)).all()

    dry_claimed: set[tuple[str, int, int]] = set()

    for tx_id, aid, fid, cents, occ in rows:
        stats["examined"] += 1
        async with get_session() as s:
            try:
                tx = (await s.execute(
                    select(Transaction).where(
                        Transaction.id == tx_id,
                        Transaction.message_id.is_(None),  # re-check: live tick may have won
                    )
                )).scalar_one_or_none()
                if tx is None:
                    stats["conflict_skipped"] += 1
                    continue
                msg, reason = await _select_ppv_link_candidate(
                    s, account_id=aid, fan_id=int(fid),
                    amount_cents=int(cents or 0), occurred_at=occ,
                )
                if msg is None:
                    stats["ambiguous_skipped" if reason == "ambiguous"
                          else "no_candidate"] += 1
                    continue
                claim_key = (str(aid), int(fid), int(msg.message_id))
                if dry_run:
                    if claim_key in dry_claimed:
                        stats["conflict_skipped"] += 1
                        continue
                    dry_claimed.add(claim_key)
                    stats["linked"] += 1
                    stats["journal"].append(
                        {"tx_id": int(tx_id), "message_id": int(msg.message_id)}
                    )
                    continue  # read-only: no mutation, session commits nothing
                if stamp_paid:
                    msg.is_paid = True
                    msg.purchased_at = occ
                tx.message_id = msg.message_id
                await s.commit()
                stats["linked"] += 1
                stats["journal"].append(
                    {"tx_id": int(tx_id), "message_id": int(msg.message_id)}
                )
                log.info(
                    "relink_ppv_linked tx=%d account=%s fan=%s msg=%d cents=%d stamp_paid=%s",
                    tx_id, aid, fid, int(msg.message_id), int(cents or 0), stamp_paid,
                )
                # Paid → owned. Only on stamp_paid runs: the attribution-only
                # backfill promises zero downstream side effects. After the
                # commit above, so no lock is held.
                if stamp_paid:
                    await ownership.try_stamp_message(
                        str(aid), int(fid), int(msg.message_id),
                        price_cents=int(cents or 0),
                        context=f"relink tx={tx_id}")
            except IntegrityError:
                await s.rollback()
                stats["conflict_skipped"] += 1

    if len(rows) == limit and not newest_first and rows:
        last = rows[-1]
        stats["next_after"] = (last.occurred_at.isoformat(), int(last.id))
    return stats


async def _has_recent_post_txn(account_id: str, *, since: datetime) -> bool:
    """Any wall-post purchase on the ledger recently? Cheap gate for the
    per-tick post-ownership sweep — most accounts sell nothing off the wall
    and should not pay a notifications fetch every 5 minutes."""
    async with get_session() as s:
        row = (await s.execute(
            select(Transaction.id).where(
                Transaction.account_id == account_id,
                Transaction.kind == "ppv_post",
                Transaction.occurred_at >= since,
            ).limit(1)
        )).scalar_one_or_none()
    return row is not None


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
    ownership_stamp_pending: tuple[int, int, int] | None = None  # (fan_id, message_id, cents)
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
                # The linked message is PAID → its media is OWNED. Deferred to
                # after this session commits (ownership opens its own session;
                # running it here would write-deadlock against ours). Covers
                # every lane the linker reaches: catalog offers, priced
                # teasers (VaultSend rows keyed by message id) and mass
                # placeholders (media via the broadcast cache).
                if new_tx.message_id is not None:
                    ownership_stamp_pending = (
                        int(parsed["fan_id"]), int(new_tx.message_id),
                        int(parsed["amount_cents"] or 0),
                    )
            if (
                parsed["fan_id"]
                and parsed["status"] not in _NON_REVENUE_STATUSES
            ):
                await _bump_lifetime(
                    s, account_id, parsed["fan_id"], parsed["amount_cents"]
                )
                # A chat tip that only exists in the ledger (WS pump missed it)
                # has NO chat_message row → it never shows in the thread, only
                # in the revenue panel. Synthesize an inbound row so it appears
                # inline. Chat tips only (kind=='tip'); tips on posts/streams
                # (tip_post/tip_stream) don't ride a chat and are excluded.
                if kind == "tip":
                    await _synthesize_tip_message(
                        s, account_id=account_id, fan_id=parsed["fan_id"],
                        amount_cents=parsed["amount_cents"],
                        occurred_at=parsed["occurred_at"], tx=new_tx,
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

    # Ownership stamp for the freshly-linked PPV buy (best-effort by contract —
    # see ownership.try_stamp_message; the purchases-notification poll re-stamps
    # the same message idempotently on its next sighting anyway).
    if ownership_stamp_pending is not None:
        fan_id, msg_id, cents = ownership_stamp_pending
        await ownership.try_stamp_message(
            account_id, fan_id, msg_id, price_cents=cents, context="ingest")

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
    """The pooled OFClient for `account_id`, or None if it cannot be built.

    Goes through `client_pool` rather than `OFClient.from_account` so the two
    ingest ticks (300s scan, 30s fast tick) reuse one long-lived client per
    account instead of building ~18k throwaway ones a day. Each throwaway
    started with an empty connection cache — a cold TLS handshake per call plus
    a pure-overhead x-hash GET, since of_client caches x-hash per instance. It
    was also the last fd-per-call site in production, the pattern that took the
    relay to EMFILE (see client_pool's module docstring).

    Do NOT close the returned client: pooled clients are shared and long-lived,
    and eviction is the pool's job (re-bootstrap / proxy edit already call it).
    """
    try:
        return client_pool.get(account_id)
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
            # Repeated-page guard: some OF endpoints IGNORE `offset` and report
            # hasMore=true forever (verified live 2026-07-04 on the group-stats
            # endpoint) — without this, the loop is UNBOUNDED and wedges this
            # account's _tick_lock for good. A page whose ids we've all seen
            # means no cursor makes progress → stop WITHOUT floor_reached, so a
            # backfill never marks itself complete off a truncated scan.
            seen_tx_ids: set = set()
            _PAGE_CEILING = 500  # 500 × 100 rows — far past any real window

            try:
                log.info(
                    "ingest_tx_scan_start account=%s mode=%s start=%s end=%s",
                    account_id, mode, start_dt.isoformat(), end_dt.isoformat(),
                )
                while True:
                    if pages >= _PAGE_CEILING:
                        log.warning(
                            "ingest_tx_page_ceiling account=%s pages=%d — scan truncated",
                            account_id, pages,
                        )
                        break
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
                    page_ids = {raw.get("id") for raw in rows if raw.get("id") is not None}
                    if page_ids and page_ids <= seen_tx_ids:
                        log.warning(
                            "ingest_tx_repeated_page account=%s page=%d offset=%d — "
                            "OF ignored the offset cursor; stopping scan",
                            account_id, pages, offset,
                        )
                        break
                    seen_tx_ids |= page_ids
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

            # Relink: retry the payment→message link for recent orphaned chat
            # PPVs whose message row arrived AFTER the transaction did (scrape
            # lag, WS gap). Runs BEFORE attribute_orphans so the linker gets
            # first shot — a linked sale credits its sender directly and needs
            # no last-toucher heuristic. newest-first + bounded limit so
            # permanently-ambiguous rows in the window can't starve fresh
            # linkable ones. Best-effort, same contract as the sweep below.
            try:
                rl = await relink_orphan_ppvs(
                    account_id=account_id,
                    since=datetime.utcnow() - timedelta(days=_ATTR_SWEEP_SINCE_DAYS),
                    limit=200,
                )
                if rl["linked"] or rl["ambiguous_skipped"]:
                    log.info(
                        "ingest_tx_relink_sweep account=%s linked=%d ambiguous=%d "
                        "no_candidate=%d conflicts=%d",
                        account_id, rl["linked"], rl["ambiguous_skipped"],
                        rl["no_candidate"], rl["conflict_skipped"],
                    )
            except Exception:
                log.warning(
                    "ingest_tx_relink_sweep_failed account=%s", account_id,
                    exc_info=True,
                )

            # Wall-POST ownership belt: the ledger's ppv_post row names WHO and
            # HOW MUCH but never WHICH post — only the purchases-notification
            # feed carries the post id. The 30s fast poll parses it live, but
            # that loop is GATED (selling automations ∪ online principals), so
            # an account selling only off its wall would never stamp. Whenever
            # this account has a recent ppv_post txn, re-read the feed here —
            # stamping is idempotent, and the announce channel this also feeds
            # is DEDUPED against the fast poll (not absent from this path).
            try:
                recent_post_buy = await _has_recent_post_txn(
                    account_id,
                    since=datetime.utcnow()
                    - timedelta(days=_POST_OWNERSHIP_SINCE_DAYS),
                )
                if recent_post_buy:
                    pn = await purchase_notifications.poll_purchases(
                        account_id, client, limit=50)
                    if pn.get("posts") or pn.get("stamped"):
                        log.info(
                            "ingest_tx_post_ownership_sweep account=%s %s",
                            account_id, pn,
                        )
            except Exception:
                log.warning(
                    "ingest_tx_post_ownership_sweep_failed account=%s",
                    account_id, exc_info=True,
                )

            # Auto-assign: credit the last-toucher (human OR Automation) for
            # any sale this scan surfaced that still resolves to no employee,
            # so the per-employee stats never leave revenue in "Unattributed".
            # Best-effort — a failure here must never fail the scan. Scoped to
            # this account + a recent window so the sweep stays cheap per tick;
            # deep history is handled by the one-shot
            # db.backfills.backfill_orphan_attribution.
            try:
                attr = await attribute_orphans(
                    account_id=account_id,
                    since=datetime.utcnow() - timedelta(days=_ATTR_SWEEP_SINCE_DAYS),
                )
                if attr["attributed"]:
                    log.info(
                        "ingest_tx_attr_sweep account=%s attributed=%d unresolved=%d",
                        account_id, attr["attributed"], attr["unresolved"],
                    )
            except Exception:
                log.warning(
                    "ingest_tx_attr_sweep_failed account=%s", account_id,
                    exc_info=True,
                )
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


# ── Fast freshness poll (30s) ────────────────────────────────────────
# Layered ON TOP of the 5-min supervisor: one tiny request per online account
# fetching just the most recent _FAST_PAGE_LIMIT rows, so a PPV unlock / tip
# lands in the transactions table within ~30s instead of up to 5min. That is
# what lets ai_chatter's offer watcher clear "waiting on tip" the tick after
# the fan pays. The supervisor still owns the authoritative 7-day window,
# relink sweep, orphan attribution, and backfill — this adds none of that.
_FAST_TICK_INTERVAL_S = 30
_FAST_PAGE_LIMIT = 10


async def run_fast_tick(account_id: str) -> dict:
    """One freshness poll for one account: fetch the newest _FAST_PAGE_LIMIT
    transactions (single request, no window, no pagination) and write them
    idempotently.

    Fully idempotent WITH the 5-min supervisor: _write_one dedups on
    provider_id (Step 1), so both loops writing the same rows converge and
    never double-count. The write phase runs under the SAME per-account
    _tick_locks lock the supervisor uses, so a fast tick and a supervisor tick
    can't both INSERT the same provider_id concurrently — the second sees the
    first's committed row and patches instead. The OF fetch happens OUTSIDE the
    lock so a long backfill holding the lock only delays this account's writes,
    never the fetch of others."""
    client = _client_for(account_id)
    if client is None:
        return {"rows_inserted": 0, "rows_patched": 0, "error": "no_session"}
    # Purchase notifications FIRST, and independently of the ledger fetch below.
    # The ledger is the money record but it lands hours late (measured 209/560/628
    # min), so it is useless for "did he just buy?". OF's purchases notification
    # names the exact message id the moment it is paid — flipping is_paid off it
    # lets ai_chatter._resolve_open_offers close the offer on its next tick via the
    # path it already uses. Writes NO money: the ledger stays the sole revenue
    # writer, so nothing here can double-count. Never raises.
    await purchase_notifications.poll_purchases(
        account_id, client, limit=_FAST_PAGE_LIMIT)
    try:
        resp = await asyncio.to_thread(
            client.transactions, limit=_FAST_PAGE_LIMIT, offset=0)
    except Exception as e:
        log.debug("ingest_tx_fast_fetch_failed account=%s", account_id, exc_info=True)
        return {"rows_inserted": 0, "rows_patched": 0, "error": repr(e)}
    rows = resp.get("list") or []
    inserted = patched = 0
    lock = _tick_locks[account_id]
    async with lock:
        for raw in rows:
            try:
                ins, pat = await _write_one(account_id, raw)
            except Exception:
                log.warning("ingest_tx_fast_write_failed account=%s provider=%s",
                            account_id, raw.get("id"), exc_info=True)
                continue
            inserted += ins
            patched += pat
    if inserted or patched:
        log.info("ingest_tx_fast account=%s inserted=%d patched=%d",
                 account_id, inserted, patched)
    return {"rows_inserted": inserted, "rows_patched": patched, "error": None}


async def start_fast_poll() -> None:
    """30s freshness loop — see run_fast_tick. Same online-gate and pause
    respect as the main supervisor (idle dashboards ⇒ no proxy burn; an account
    the supervisor backed off after 3 fails stays skipped). Sequential per
    cycle — SQLite WAL serializes writes and we want bounded concurrent OF
    load. Sleep AT END so the gap is _FAST_TICK_INTERVAL_S regardless of cycle
    duration."""
    log.info("ingest_tx_fast_poll_started tick=%ds limit=%d",
             _FAST_TICK_INTERVAL_S, _FAST_PAGE_LIMIT)
    # One tick interval of politeness, once, so the first fleet walk does not
    # land on top of the pumps still connecting. See _FAST_BOOT_GRACE_S.
    await boot_grace(_FAST_BOOT_GRACE_S, name="ingest-fast-poll")
    while True:
        try:
            cycle_started = time.monotonic()
            account_ids = [
                m["id"] for m in account_registry.list_accounts()
                if m.get("has_session")
            ]
            wanted = await _fast_poll_account_ids()
            account_ids = [aid for aid in account_ids if aid in wanted]
            for aid in account_ids:
                if await _is_paused(aid):
                    continue
                try:
                    await run_fast_tick(aid)
                except Exception:
                    log.debug(
                        "ingest_tx_fast_cycle_account_failed account=%s",
                        aid, exc_info=True)
            elapsed = time.monotonic() - cycle_started
            await asyncio.sleep(max(_MIN_CYCLE_GAP_S, _FAST_TICK_INTERVAL_S - elapsed))
        except asyncio.CancelledError:
            return
        except Exception:
            log.warning("ingest_tx_fast_poll cycle failed", exc_info=True)
            await asyncio.sleep(_FAST_TICK_INTERVAL_S)


# Automations whose behaviour changes the moment a fan pays: they hold an open
# offer, or they gate on spend. For these, "did he buy?" is the ledger's
# `kind='ppv_message'` row — the WS pump catches ~0 unlock events and the OF
# re-read fastpath is capped at 20 messages, so the ingest IS the signal
# (see project_offer_stuck_ambiguous_linker_and_fast_poll). They run around the
# clock, so their purchase detection cannot wait for someone to open the app.
_SELLING_KINDS = ("ai_chatter", "welcome_chatter_for_info", "ppv_send", "tip_request",
                  "reengage_buyers")


async def _fast_poll_account_ids() -> set[str]:
    """Accounts the 30s loop should cover.

    Two independent reasons to poll fast, unioned:

      • a SELLING automation is enabled on the account — it may be sitting on an
        open offer right now, and the only reliable "he paid" signal is a ledger
        row. At 5-minute granularity the AI keeps saying "waiting on your tip"
        and re-offering content the fan already bought, at 3am with nobody
        watching. This half does NOT depend on anyone being online.
      • somebody is actually looking at the account — dashboard freshness, the
        original reason this loop was gated at all.

    So an idle, non-selling account still costs nothing when no one is around,
    but a live seller never learns about a purchase late.
    """
    async with get_session() as s:
        selling = {r[0] for r in (await s.execute(
            select(AutomationRule.account_id)
            .where(AutomationRule.kind.in_(_SELLING_KINDS),
                   AutomationRule.is_enabled.is_(True))
        )).all()}
    return selling | await _online_account_ids()


async def _online_account_ids() -> set[str]:
    """Union of OF accounts visible to any principal active within the last
    _ONLINE_WINDOW_S seconds.

    Sources combined:
      • ADMINS (users.is_admin + last_seen_at) → EVERY account. An admin sees
        the whole roster in the UI, so scoping their tick to the accounts they
        happen to be listed against in `user_accounts` silently starves the
        rest. That is not hypothetical: the graded vault had NO user_accounts row at
        all and the graded vault's owner was offline, so neither account's ledger was
        ever scanned — no `transactions` rows, an empty PPV chart on every one
        of their fans, and `fans.lifetime_spend_cents` frozen at 0 (which also
        starves the welcome_chatter_for_info "hand payers to humans" gate). Ownership is NOT
        the right lever for "should this run": a live, connected account must
        be ingested regardless of who is listed as its owner.
      • owners (users.last_seen_at) → user_accounts.account_id
      • chatters (chatters.last_seen_at + is_active=True) →
            chatter_users → user_accounts.account_id

    Non-admin principals stay scoped to their own accounts, so a multi-tenant
    box doesn't burn one agency's proxy because another agency is online.

    ⚠️ FAST-POLL ONLY. The 5-minute supervisor deliberately does NOT consult
    this: it is the sole writer of the `transactions` ledger that
    `fans.lifetime_spend_cents` — and therefore every automation's spend
    decision — is derived from, and automations run whether or not anyone is
    watching. Only the 30s freshness loop, which exists purely so an open
    dashboard updates quickly, may be skipped when nobody is looking.

    Empty set ⇒ nobody's online; the fast poll skips this tick. The
    /admin/ingest/transactions/{aid}/run admin endpoint stays unaffected —
    a manual re-sync is an explicit user gesture and bypasses this gate.
    """
    cutoff = datetime.utcnow() - timedelta(seconds=_ONLINE_WINDOW_S)
    async with get_session() as s:
        admin_online = (await s.execute(
            select(User.id).where(User.last_seen_at >= cutoff,
                                  User.is_admin.is_(True)).limit(1)
        )).first()
        if admin_online:
            # Every known account — the per-account `has_session` / paused
            # filters in the callers still decide what actually gets scanned.
            return {r[0] for r in (await s.execute(select(Account.id))).all()}
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
    # AFTER the sweep, never before it: the sweep is a single cheap UPDATE that
    # unlocks accounts the previous process left stamped in-flight, and holding
    # it for the grace would leave /run 409-ing for that whole window. The grace
    # covers the expensive part — the fleet walk below. See _BOOT_GRACE_S.
    await boot_grace(_BOOT_GRACE_S, name="ingest-supervisor")
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
            # NO online gate here. This loop is not a dashboard convenience —
            # it is the only writer of the `transactions` ledger, and
            # `event_transcoder` bumps `fans.lifetime_spend_cents` off those
            # rows. The AUTOMATIONS run 24/7 with no online gate of their own
            # (automation_executor never consults one) and read that number to
            # decide real money questions: ai_chatter's whale gate
            # (max_lifetime_spend_cents), the proven-spend floor, the tip-ladder
            # base, welcome_chatter_for_info's hand-to-human
            # gate. Gating the producer on "is a human watching" while the
            # consumers never stop meant that with every dashboard closed, a fan
            # who had already blown past the whale cap still read as $0 and the
            # AI kept selling to him. Freshness for a watching human is what the
            # 30s fast poll is for — THAT one keeps the online gate.
            if not account_ids:
                elapsed = time.monotonic() - cycle_started
                await asyncio.sleep(max(_MIN_CYCLE_GAP_S, _TICK_INTERVAL_S - elapsed))
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
            await asyncio.sleep(max(_MIN_CYCLE_GAP_S, _TICK_INTERVAL_S - elapsed))
        except asyncio.CancelledError:
            return
        except Exception:
            log.warning("ingest supervisor cycle failed", exc_info=True)
            await asyncio.sleep(_TICK_INTERVAL_S)
