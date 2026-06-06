"""
Automation executor — the background worker that runs the 12 automations
(MASTER_PLAN §5, db_data/08_build_order Phase B, one_section_of_automations).

This is the P3 *harness*: a single async supervisor task per process that
consumes `scheduled_jobs`, evaluates `automation_rules`, and runs one bounded
task per (account, automation_kind) on a tick. Every run writes an
`automation_runs` row (kind, started/finished, status, stats_json, error_text)
which `GET /admin/stats/automation-runs` already reads. Automation 01
`scrape_chats` ships here as the reference; P4 copies this pattern for the rest.

It MIRRORS the two existing supervisors — it does not invent a new pattern:
  • server.py `_pump_supervisor` (15s reconcile ticks, per-account tasks,
    crash-restart) spawned in `_start_event_pumps`.
  • transaction_ingest.py `start_supervisor` (per-account asyncio.Lock guards a
    slow tick from stacking; `await asyncio.to_thread(client.<sync OF call>)` so
    the network never blocks the event loop; sleep-at-end so the cadence holds).

Two concurrency invariants the DA caught (MASTER_PLAN §5.6), both real bugs:
  1. **Per-account lock** — the daily soft-cap check+spend is serialized per
     account, else parallel LLM workers each read "under cap" and overspend.
     `account_spend_lock()` exposes the lock; `reserve_daily_spend()` is the
     seam P4's llm_client fills in. scrape_chats does not spend, so it skips it.
  2. **Per-(account, fan) lease** — A05/A06/A07/A11 can all message the SAME fan
     in overlapping ticks. `acquire_fan_lease()` writes a short-TTL row in
     `fan_lease` (PK (account_id, fan_id)); a live lease blocks every other
     automation from sending to that fan until it expires or is released. One
     bot message per fan per cycle. scrape_chats does not send, so it does not
     lease — but the harness provides + tests the primitive for P4.

The testable seam is `run_once(account_id, kind)`: it executes exactly one
automation run and returns. Tests call it directly — no infinite loop, no
timing, no network (inject a fake client via `_make_client`).

Guardrail (MASTER_PLAN §5): a route handler may only INSERT a `scheduled_jobs`
row (see `enqueue_job`) to trigger work — it must NEVER run an automation
inline. The worker loop is the only thing that calls `run_once`.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, NamedTuple

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from db.engine import get_session
from db.models import (
    AccountAiConfig,
    AutomationRule,
    AutomationRun,
    Chat,
    Fan,
    FanLease,
    Message,
    ScheduledJob,
    ScrapeHistory,
)
from of_client import OFClient
from automation_registry import register, get_automation, load_automation_plugins

log = logging.getLogger("of-relay.automation")

# ── Knobs ────────────────────────────────────────────────────────────
_TICK_INTERVAL_S = 30          # supervisor cadence (sleep-at-end like tx-ingest)
_MAX_CONCURRENT_RUNS = 4       # bound total in-flight (account, kind) runs per tick
# Fan-lease TTL (W3): held for the WHOLE send, NOT released on success (let it
# expire) — only released on not-sent/error. So the TTL must clear the worst-case
# LLM-generate + OF-send latency, else a slow run's lease expires mid-send and a
# second automation could steal the fan. 900s (15 min) sits well above that.
_LEASE_TTL_S = 900
# After every CONFIRMED bot send, the fan rests: fans.automation_paused_until is
# set this far out and EVERY sender skips a paused fan BEFORE acquiring the lease.
# This is the load-bearing anti-double-message guard across overlapping ticks.
_FAN_COOLDOWN_S = 1800         # 30 min
_MAX_JOB_ATTEMPTS = 3          # transient-failure retries before a job is parked 'error'
_JOB_RETRY_BACKOFF_S = 60      # requeue delay after a failed attempt

# Send priority (W3): lower rank = higher priority. Used by the supervisor to
# DISPATCH higher-priority kinds first within a tick, so on the rare fan overlap
# the more important automation acquires the fan-lease before a lower one does
# (e.g. a brand-new sub gets `send_welcome`, not `send_followup`). NOTE: a LIVE
# lease still blocks EVERY other kind regardless of priority — priority never
# steals a live lease (that would risk a double-message); it only orders who
# tries first. Unknown kinds sort last (after the ranked senders).
_KIND_PRIORITY: dict[str, int] = {
    "send_welcome": 1,
    "of_ai_chat": 2,
    "deep_convo": 3,
    "send_followup": 4,
    "reply_mass_funnel": 5,
    "send_mass_message": 6,
}
_DEFAULT_PRIORITY = 50


def kind_priority(kind: str) -> int:
    """Dispatch rank for an automation kind — lower runs first. See _KIND_PRIORITY."""
    return _KIND_PRIORITY.get(kind, _DEFAULT_PRIORITY)


# ── W7 supervisor wake ───────────────────────────────────────────────
# The supervisor normally drains on a fixed _TICK_INTERVAL_S cadence. W7 makes
# dispatch event-driven: an inbound WS event (webhook_dispatch.on_inbound_message)
# calls wake_supervisor() to drain NOW instead of waiting for the next tick. The
# 30s tick stays as a fallback. The Event is created lazily inside
# automation_supervisor() on the running loop; wake_supervisor() is a no-op until
# then (and during tests, which call run_once directly without a supervisor).
_wake_event: asyncio.Event | None = None


def wake_supervisor() -> None:
    """Signal the supervisor to drain immediately (W7 real-time dispatch).
    No-op when no supervisor is running (e.g. tests). Loop-safe: callers run on
    the same event loop as the supervisor, so a plain .set() suffices."""
    ev = _wake_event
    if ev is not None:
        ev.set()

# scrape_chats paging — mirror of_client.iter_messages' Fast Fetch defaults.
# OF ignores `limit` and returns FEWER than asked, so every loop is bounded by a
# message COUNT cap (never by an assumed page size) and stops on hasMore=False.
_SCRAPE_PAGE_SIZE = 50
_SCRAPE_MAX_PAGES = 40         # safety ceiling per end per chat (count caps bound it first)
_SCRAPE_RECENT_CAP = 100       # newest-end messages pulled each scrape (first/last-100 window)
_SCRAPE_OLDEST_CAP = 100       # convo-start messages, grabbed ONCE on the first scrape
_SCRAPE_CHAT_LIMIT = 100       # how many sidebar chats one all-chats sweep covers (paginated 10/page)
_PAGE_SLEEP_S = 0.3            # inter-page / inter-chat politeness gap (matches of_client)

# Per-account lock: serializes the soft-cap check+spend so two LLM workers can't
# both pass the cap. defaultdict(asyncio.Lock) is the proven pattern from
# transaction_ingest.py:_tick_locks — Lock construction needs no running loop.
_account_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

# Per-(account, kind) lock: stops a slow run from stacking with the next tick.
# Same idea as tx-ingest's per-account tick lock, scoped to the automation kind
# (scrape and of_ai_chat for one account can run concurrently; two scrapes can't).
_run_locks: dict[tuple[str, str], asyncio.Lock] = defaultdict(asyncio.Lock)


# ── Tiny parsing helpers (local copies — same house pattern as ───────
#    transaction_ingest.py / event_transcoder.py each keeping their own) ─

def _to_cents(price: Any) -> int:
    """OF prices arrive as floats like 9.99 → 999 cents. 0/None/'' → 0."""
    try:
        if price is None or price == "":
            return 0
        return int(round(float(price) * 100))
    except Exception:
        return 0


def _parse_iso(s: str | None) -> datetime | None:
    """OF ISO 8601 (`+00:00` or trailing `Z`) → naive UTC datetime."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return None


def _media_ids(media: list | None) -> list[int]:
    """Pluck integer media ids; skip anything non-int."""
    if not isinstance(media, list):
        return []
    out: list[int] = []
    for m in media:
        mid = (m or {}).get("id") if isinstance(m, dict) else None
        if isinstance(mid, int):
            out.append(mid)
        elif isinstance(mid, str) and mid.isdigit():
            out.append(int(mid))
    return out


_HTML_OPEN = "<"


def _strip_html(s: str | None) -> str:
    """Cheap tag-stripper for inbox previews (OF wraps bodies in <p>…</p>)."""
    if not s:
        return ""
    if _HTML_OPEN not in s:
        return s.strip()
    import re
    return re.sub(r"<[^>]+>", "", s).strip()


# ── Concurrency primitive 1: per-account spend lock ──────────────────

def account_spend_lock(account_id: str) -> asyncio.Lock:
    """The per-account lock that serializes the daily soft-cap check+spend
    (MASTER_PLAN §5.6). P4's LLM automations MUST hold this across
    `read-today's-spend → compare-to-cap → record-the-call`, otherwise two
    workers each observe "under cap" and overspend. Exposed as a primitive so
    every LLM automation shares the SAME lock object per account."""
    return _account_locks[account_id]


async def reserve_daily_spend(account_id: str, est_cents: int) -> bool:
    """Seam for P4 (llm_client / 05_grok_extraction §7). Acquires the
    per-account lock so the check+spend is atomic, then returns whether the
    estimated spend is allowed under the account's daily soft-cap.

    Left permissive (always allows) until the LLM-cost tables land in P4 —
    scrape_chats never calls it. The CONTRACT that matters now is that the lock
    is the single serialization point; the accounting body is filled in later.
    """
    async with account_spend_lock(account_id):
        # TODO(P4/T-LLMCLIENT): sum today's grok_calls cost for this account,
        # compare against account_ai_config soft-cap, deny when exceeded.
        return True


# ── Concurrency primitive 2: per-(account, fan) send lease ───────────

async def acquire_fan_lease(
    account_id: str,
    fan_id: int,
    automation_kind: str,
    *,
    ttl_s: int = _LEASE_TTL_S,
) -> bool:
    """Try to take the send-lease for one fan. Returns True iff we now hold it.

    DB-atomic: a single `INSERT … ON CONFLICT DO UPDATE … WHERE leased_until
    <= now` either inserts a fresh lease, steals an EXPIRED one, or — when a
    live lease is held — updates nothing (rowcount 0). So two automations
    racing the same fan in overlapping ticks can never both win: the first
    write commits a live lease, the second's WHERE excludes the row.

    A live lease blocks every other kind REGARDLESS of priority — priority
    (see kind_priority) only orders who *tries* first at dispatch, it never
    steals a live lease (that would risk a double-message).

    W3 lease lifecycle: the holder KEEPS the lease through the whole send and
    lets it expire by TTL on success (so a slow LLM+send can't have its lease
    stolen mid-flight); it calls `release_fan_lease()` ONLY when it did not send
    (skip/error/dry-run), to free the fan for a retry sooner. After a confirmed
    send the holder also calls `start_fan_cooldown()`, the durable cross-tick
    guard. One bot message per fan per cycle.
    """
    now = datetime.utcnow()
    until = now + timedelta(seconds=ttl_s)
    stmt = (
        sqlite_insert(FanLease)
        .values(
            account_id=str(account_id),
            fan_id=int(fan_id),
            automation_kind=automation_kind,
            leased_until=until,
            acquired_at=now,
        )
        .on_conflict_do_update(
            index_elements=["account_id", "fan_id"],
            set_={
                "automation_kind": automation_kind,
                "leased_until": until,
                "acquired_at": now,
            },
            # Only an expired (or exactly-now) lease may be overwritten. A live
            # lease leaves rowcount at 0 → we did NOT acquire.
            where=FanLease.leased_until <= now,
        )
    )
    async with get_session() as s:
        res = await s.execute(stmt)
        got = (res.rowcount or 0) > 0
    if got:
        log.debug(
            "fan_lease_acquired account=%s fan=%s kind=%s ttl=%ds",
            account_id, fan_id, automation_kind, ttl_s,
        )
    else:
        log.debug(
            "fan_lease_blocked account=%s fan=%s kind=%s (live lease held)",
            account_id, fan_id, automation_kind,
        )
    return got


async def release_fan_lease(account_id: str, fan_id: int) -> None:
    """Drop a fan's lease early (after the send completes)."""
    async with get_session() as s:
        await s.execute(
            delete(FanLease).where(
                FanLease.account_id == str(account_id),
                FanLease.fan_id == int(fan_id),
            )
        )


async def _sweep_expired_leases() -> int:
    """Cheap housekeeping: drop every lease past its TTL (uses
    ix_fan_lease_expiry). Run once per supervisor tick. Returns rows removed."""
    now = datetime.utcnow()
    async with get_session() as s:
        res = await s.execute(
            delete(FanLease).where(FanLease.leased_until < now)
        )
    return res.rowcount or 0


# ── Concurrency primitive 3: per-fan post-send cooldown (W3) ─────────
#
# The load-bearing anti-double-message fix. After ANY automation confirms a send
# to a fan it sets `fans.automation_paused_until = now + cooldown`; EVERY sender
# calls `fan_on_cooldown()` BEFORE acquiring the lease and skips a paused fan.
# Unlike the lease (a short in-tick latch), the cooldown is the durable guard
# that holds ACROSS ticks — it survives even after the lease has expired/cleared.
# NOTE (per-fan, not per-kind): the rest is shared across automations, so whichever
# sender fired LAST sets it and the SHORTEST cooldown governs cross-automation
# re-touch (of_ai_chat's 45s can let another kind re-touch sooner than 30 min).
# Known + accepted — not a bug; WAVE 7's priority dispatch is where per-kind fan
# ownership lands.

async def start_fan_cooldown(
    account_id: str, fan_id: int, *, cooldown_s: int = _FAN_COOLDOWN_S
) -> datetime:
    """Rest a fan after a confirmed send: set automation_paused_until = now+cooldown.

    UPSERT, not UPDATE — many senders source candidates from the messages table,
    so an eligible fan may have no `fans` row yet; a bare UPDATE would match 0
    rows and silently fail to pause, re-opening the double-message. Returns the
    paused-until instant.
    """
    now = datetime.utcnow()
    until = now + timedelta(seconds=cooldown_s)
    async with get_session() as s:
        await s.execute(
            sqlite_insert(Fan)
            .values(
                account_id=str(account_id),
                fan_id=int(fan_id),
                automation_paused_until=until,
                updated_at=now,
            )
            .on_conflict_do_update(
                index_elements=["account_id", "fan_id"],
                # updated_at = now (the row WAS touched now); NOT `until` — a
                # future updated_at leaks into push_to_sheets' "last updated".
                set_={"automation_paused_until": until, "updated_at": now},
            )
        )
    return until


async def fan_on_cooldown(account_id: str, fan_id: int) -> bool:
    """True iff `fans.automation_paused_until` is in the future. A fresh DB read
    (not the gather-time snapshot) so a fan another sender paused EARLIER IN THE
    SAME TICK is still skipped. Call this BEFORE acquire_fan_lease in every
    sender."""
    now = datetime.utcnow()
    async with get_session() as s:
        until = (
            await s.execute(
                select(Fan.automation_paused_until).where(
                    Fan.account_id == str(account_id),
                    Fan.fan_id == int(fan_id),
                )
            )
        ).scalar_one_or_none()
    return until is not None and until > now


# ── automation_runs telemetry ────────────────────────────────────────

async def _open_run(account_id: str | None, kind: str) -> int:
    """Insert a 'running' automation_runs row up-front so the dashboard shows
    in-flight runs, and return its id for the finalizer."""
    async with get_session() as s:
        run = AutomationRun(
            account_id=account_id,
            kind=kind,
            started_at=datetime.utcnow(),
            status="running",
        )
        s.add(run)
        await s.flush()
        return run.id


async def _finalize_run(
    run_id: int,
    status: str,
    stats: dict | None,
    error_text: str | None,
) -> None:
    async with get_session() as s:
        run = await s.get(AutomationRun, run_id)
        if run is None:  # pragma: no cover — defensive
            return
        run.status = status
        run.completed_at = datetime.utcnow()
        run.stats_json = json.dumps(stats, default=str) if stats is not None else None
        run.error_text = error_text


# ── OF write helper: off-thread + async retry on rate limit ──────────

# OF throttles consecutive POSTs (create_post, /messages/queue) with a 400
# `{"message":"Please allow 10 seconds"}`. We retry once after an ASYNC pause —
# asyncio.sleep yields the event loop, so the relay keeps serving other work
# (never a blocking time.sleep on the main loop).
_RATE_LIMIT_MARK = "allow 10 seconds"


async def of_write_with_retry(fn, *, tries: int = 2, pause_s: float = 11.0):
    """Run a blocking OF write `fn` off-thread; on OF's rate-limit 400, pause
    asynchronously and retry (up to `tries` total attempts). Any other error —
    or the final attempt — propagates unchanged."""
    last_exc: Exception | None = None
    for attempt in range(1, tries + 1):
        try:
            return await asyncio.to_thread(fn)
        except Exception as e:  # noqa: BLE001 — re-raised below if not retryable
            if _RATE_LIMIT_MARK in str(e).lower() and attempt < tries:
                log.info("of_write_rate_limited attempt=%d — async pause %.0fs then retry",
                         attempt, pause_s)
                await asyncio.sleep(pause_s)
                last_exc = e
                continue
            raise
    assert last_exc is not None  # unreachable: loop either returns or raises
    raise last_exc


# Proactive per-account spacing so consecutive OF writes never crowd the 10s
# window in the first place. We reserve a monotonically-advancing "next slot"
# per account; each writer sleeps (async!) until its slot. The lock is held
# only for the few microseconds of slot arithmetic — NOT during the wait or the
# write — so the event loop and worker threads stay free for everything else
# (other accounts, other automations, the relay's own traffic). Per-account
# because OF throttles per-account; different girls send concurrently.
_OF_WRITE_GAP_S = 11.0
_next_write_at: dict[str, float] = {}
_write_gate_locks: dict[str, asyncio.Lock] = {}


def _write_gate_lock(account_id: str) -> asyncio.Lock:
    lock = _write_gate_locks.get(account_id)
    if lock is None:
        lock = asyncio.Lock()
        _write_gate_locks[account_id] = lock
    return lock


async def of_write_paced(account_id, fn, *, min_gap_s: float = _OF_WRITE_GAP_S,
                         tries: int = 2, pause_s: float = 11.0):
    """SPACE this OF write ≥ `min_gap_s` after the previous one for this account
    (proactive — so we rarely trip the limit at all), then run it through the
    reactive async retry as a backstop.

    The spacing wait is `asyncio.sleep`: it yields the event loop, so during the
    wait the worker threads and the loop remain available for other work. If the
    account has been idle longer than the gap, there's no wait at all."""
    aid = str(account_id)
    loop = asyncio.get_running_loop()
    async with _write_gate_lock(aid):
        slot = max(loop.time(), _next_write_at.get(aid, 0.0))
        _next_write_at[aid] = slot + min_gap_s  # reserve this slot for ourselves
    wait = slot - loop.time()
    if wait > 0.001:
        log.info("of_write_paced account=%s spacing %.1fs (loop/threads stay free)", aid, wait)
        await asyncio.sleep(wait)
    return await of_write_with_retry(fn, tries=tries, pause_s=pause_s)


# ── scheduled_jobs queue ─────────────────────────────────────────────

class _ClaimedJob(NamedTuple):
    id: int
    payload_json: str


async def enqueue_job(
    account_id: str,
    kind: str,
    *,
    payload: dict | None = None,
    run_at: datetime | None = None,
    rule_id: int | None = None,
    created_by_employee_id: int | None = None,
) -> int:
    """Public trigger seam: a route handler calls THIS (insert a row + return)
    instead of running an automation inline. Returns the new job id."""
    async with get_session() as s:
        job = ScheduledJob(
            account_id=account_id,
            kind=kind,
            payload_json=json.dumps(payload or {}, default=str),
            run_at=run_at or datetime.utcnow(),
            status="pending",
            rule_id=rule_id,
            created_by_employee_id=created_by_employee_id,
        )
        s.add(job)
        await s.flush()
        return job.id


async def _claim_due_job(account_id: str, kind: str) -> _ClaimedJob | None:
    """Claim the earliest due pending job for (account, kind): flip it to
    'running' and bump attempts in one transaction so a concurrent tick can't
    grab the same row. Returns a detached (id, payload) tuple or None."""
    now = datetime.utcnow()
    async with get_session() as s:
        row = (
            await s.execute(
                select(ScheduledJob)
                .where(
                    ScheduledJob.account_id == account_id,
                    ScheduledJob.kind == kind,
                    ScheduledJob.status == "pending",
                    ScheduledJob.run_at <= now,
                )
                .order_by(ScheduledJob.run_at)
                .limit(1)
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        row.status = "running"
        row.attempts = (row.attempts or 0) + 1
        return _ClaimedJob(id=row.id, payload_json=row.payload_json or "{}")


async def _mark_job(job_id: int, status: str, last_error: str | None) -> None:
    async with get_session() as s:
        job = await s.get(ScheduledJob, job_id)
        if job is None:  # pragma: no cover
            return
        job.status = status
        if last_error is not None:
            job.last_error = last_error[:500]


async def _mark_job_failure(job_id: int, err: str) -> None:
    """Park a failed job. Retry (status back to 'pending' with a backoff) while
    attempts remain; otherwise leave it 'error' for inspection."""
    async with get_session() as s:
        job = await s.get(ScheduledJob, job_id)
        if job is None:  # pragma: no cover
            return
        job.last_error = err[:500]
        if (job.attempts or 0) < _MAX_JOB_ATTEMPTS:
            job.status = "pending"
            job.run_at = datetime.utcnow() + timedelta(seconds=_JOB_RETRY_BACKOFF_S)
        else:
            job.status = "error"


async def _due_job_pairs() -> list[tuple[str, str]]:
    """Distinct (account_id, kind) over all pending, due jobs — the supervisor
    spawns one bounded run per pair per tick."""
    now = datetime.utcnow()
    async with get_session() as s:
        rows = (
            await s.execute(
                select(ScheduledJob.account_id, ScheduledJob.kind)
                .where(
                    ScheduledJob.status == "pending",
                    ScheduledJob.run_at <= now,
                )
                .group_by(ScheduledJob.account_id, ScheduledJob.kind)
            )
        ).all()
    return [(r[0], r[1]) for r in rows]


def _due_clock_slot(
    now: datetime, times: list, tz_offset_minutes: int, last_started: datetime | None
) -> str | None:
    """Clock-time ("cron") trigger: given `times` like ["09:00","20:30"] expressed
    in the user's local zone (`tz_offset_minutes` = minutes to add to UTC for
    local), return the most-recent slot that is DUE — i.e. its target instant
    today has passed AND we haven't already run since then — else None.

    `now`/`last_started` are UTC (datetime.utcnow). For each "HH:MM" we build
    today's local target, convert to UTC (target_utc = local - offset), and
    treat it as due when now ≥ target_utc and last_started < target_utc."""
    best: tuple[datetime, str] | None = None
    off = timedelta(minutes=int(tz_offset_minutes or 0))
    for raw in times:
        if not isinstance(raw, str) or ":" not in raw:
            continue
        try:
            hh, mm = (int(x) for x in raw.split(":", 1))
        except ValueError:
            continue
        if not (0 <= hh < 24 and 0 <= mm < 60):
            continue
        # today's local target, as a UTC instant
        local_today = (now + off).replace(hour=hh, minute=mm, second=0, microsecond=0)
        target_utc = local_today - off
        if now < target_utc:
            continue  # slot hasn't arrived yet today
        if last_started is not None and last_started >= target_utc:
            continue  # already ran for this slot
        if best is None or target_utc > best[0]:
            best = (target_utc, raw)
    return None if best is None else best[1]


def _in_quiet_hours(quiet_hours_json: str | None, utc_offset: int, now: datetime) -> bool:
    """Rule-level quiet hours. OPT-IN: a NULL/empty/malformed `quiet_hours_json`
    (every rule's default) returns False → the rule fires 24/7, unchanged. When set
    to `[start, end]` (creator-LOCAL hours, same convention as nudge_online), return
    True when the creator's local hour falls in the band so the materializer holds
    the job until the band ends. `start == end` disables it. The band [start, end)
    wraps midnight when start > end (e.g. [22, 6] = 10pm–6am).

    Local hour mirrors send_welcome._model_hour: (utcnow().hour + utc_offset) % 24.
    A `{"start": s, "end": e}` object form is accepted too, for the rules UI."""
    if not quiet_hours_json:
        return False
    try:
        qh = json.loads(quiet_hours_json)
    except Exception:
        return False
    if isinstance(qh, dict):
        qh = [qh.get("start"), qh.get("end")]
    if not isinstance(qh, (list, tuple)) or len(qh) != 2:
        return False
    try:
        start, end = int(qh[0]), int(qh[1])
    except (TypeError, ValueError):
        return False
    if start == end:                      # disabled (incl. [0, 0])
        return False
    hour = (now.hour + int(utc_offset or 0)) % 24
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end     # wraps past midnight


async def _materialize_due_rules() -> int:
    """Turn due `automation_rules` into `scheduled_jobs` rows (the uniform path
    — rules never execute inline, they only enqueue). Conservative: handles a
    `trigger_json` of `{"every_seconds": N}`, enqueues only when the rule's last
    run is older than N AND no pending job for (account, kind) already exists.
    A malformed rule is logged + skipped, never fatal. Returns jobs enqueued."""
    now = datetime.utcnow()
    enqueued = 0
    offsets: dict[str, int] = {}   # account_id → utc_offset (creator-local quiet hours)
    async with get_session() as s:
        rules = (
            await s.execute(
                select(AutomationRule).where(AutomationRule.is_enabled.is_(True))
            )
        ).scalars().all()

        for rule in rules:
            try:
                trigger = json.loads(rule.trigger_json or "{}")
            except Exception:
                log.warning("automation_rule_bad_trigger rule_id=%s", rule.id)
                continue
            every = trigger.get("every_seconds")
            daily_at = trigger.get("daily_at")
            has_interval = isinstance(every, (int, float)) and not isinstance(every, bool) and every > 0
            has_clock = isinstance(daily_at, list) and daily_at
            if not has_interval and not has_clock:
                continue  # neither an interval nor clock trigger — nothing to do

            # Already a DUE job for this (account, kind)? Don't pile on. We only
            # count jobs that are due now (run_at <= now): a future-dated one-shot
            # (e.g. auto_stories' delete-later cleanup, which shares the
            # `unsend_messages` kind) must NOT block a recurring rule from
            # materializing its own hourly job — otherwise the rule is starved
            # for as long as any future cleanup sits pending.
            pending = (
                await s.execute(
                    select(ScheduledJob.id)
                    .where(
                        ScheduledJob.account_id == rule.account_id,
                        ScheduledJob.kind == rule.kind,
                        ScheduledJob.status.in_(("pending", "running")),
                        ScheduledJob.run_at <= now,
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            if pending is not None:
                continue

            last_started = (
                await s.execute(
                    select(AutomationRun.started_at)
                    .where(
                        AutomationRun.account_id == rule.account_id,
                        AutomationRun.kind == rule.kind,
                    )
                    .order_by(AutomationRun.started_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()

            # Cadence anchor for the interval trigger: the last time THIS rule
            # enqueued a job (rule_id == rule.id), NOT the last run of the kind.
            # `unsend_messages` (and other kinds) double as one-shot actions —
            # auto_stories' delete-later cleanup, the "Unsend this" button, test
            # drivers — all of which write AutomationRun rows for the same
            # (account, kind). Anchoring the "is it due?" check on `last_started`
            # let those one-shots reset the rule's clock every few minutes, so a
            # rule whose interval (e.g. 1h) is longer than the one-shot cadence
            # NEVER became due. Anchoring on the rule's own jobs decouples it.
            last_rule_job_at = (
                await s.execute(
                    select(func.max(ScheduledJob.created_at))
                    .where(ScheduledJob.rule_id == rule.id)
                )
            ).scalar_one_or_none()

            # ── run-limit ("how many times to run") ──────────────────────
            # Count this (account, kind)'s prior runs; auto-disable the rule
            # once it has fired max_runs times so it stops cleanly.
            max_runs = trigger.get("max_runs")
            if isinstance(max_runs, (int, float)) and not isinstance(max_runs, bool) and max_runs > 0:
                run_count = (
                    await s.execute(
                        select(func.count())
                        .select_from(AutomationRun)
                        .where(
                            AutomationRun.account_id == rule.account_id,
                            AutomationRun.kind == rule.kind,
                        )
                    )
                ).scalar_one()
                if run_count >= int(max_runs):
                    rule.is_enabled = False
                    log.info("automation_rule_run_limit_reached rule_id=%s runs=%s/%s",
                             rule.id, run_count, int(max_runs))
                    continue

            # ── due? interval vs. clock-time ─────────────────────────────
            if has_interval:
                if last_rule_job_at is not None and (now - last_rule_job_at).total_seconds() < every:
                    continue
            else:  # has_clock — fire once per configured "HH:MM" slot per day
                tz_off = trigger.get("tz_offset_minutes") or 0
                slot = _due_clock_slot(now, daily_at, tz_off, last_started)
                if slot is None:
                    continue

            # ── quiet hours (opt-in; default off) ────────────────────────
            # A due rule inside its creator-local quiet band does NOT enqueue this
            # tick — it'll materialize on the first tick after the band ends (it's
            # still "due" then, last_started is unchanged). Unset/[] = fires 24/7.
            if rule.quiet_hours_json:
                acct = rule.account_id
                if acct not in offsets:
                    offsets[acct] = (await s.execute(
                        select(AccountAiConfig.utc_offset).where(
                            AccountAiConfig.account_id == acct)
                    )).scalar_one_or_none() or 0
                if _in_quiet_hours(rule.quiet_hours_json, offsets[acct], now):
                    log.info("automation_rule_quiet_hours rule_id=%s account=%s kind=%s",
                             rule.id, acct, rule.kind)
                    continue

            s.add(
                ScheduledJob(
                    account_id=rule.account_id,
                    kind=rule.kind,
                    payload_json=rule.steps_json or "{}",
                    run_at=now,
                    status="pending",
                    rule_id=rule.id,
                )
            )
            enqueued += 1
    if enqueued:
        log.info("automation_rules_materialized count=%d", enqueued)
    return enqueued


# ── Automation 01: scrape_chats (the reference) ──────────────────────
#
# Spec: one_section_of_automations/01_scrape_chats.md + its network-rewrite
# mapping (DOM → signed OF REST). DOM scraping is GONE — we page
# `GET /api2/v2/chats/{id}/messages` via of_client and write
# messages/chats/fans + scrape_history. Fast-skip is by `scrape_history`
# last_message_id (OF ids are monotonic), stronger than the legacy text snapshot.


def _make_client(account_id: str) -> OFClient:
    """OFClient construction seam. Tests override this to inject a fake (no
    network, no session files). Kept tiny so the override surface is one name."""
    return OFClient.from_account(account_id)


async def _upsert_message(
    s, account_id: str, fan_id: int, own_user_id: str, m: dict
) -> None:
    """Upsert one OF message row. Unlike the WS transcoder (which defers
    OUTBOUND to send-time because the event lacks a recipient), the scrape knows
    the chat partner — fan_id is the chat id — so it persists BOTH directions.
    Direction is 'out' when the sender is us, else 'in'. Idempotent via the
    composite PK; on conflict we refresh the fields OF can flip after the fact
    (body edits, is_paid after unlock)."""
    message_id = m.get("id")
    if message_id is None:
        return
    from_user = m.get("fromUser") or {}
    from_id = from_user.get("id")
    direction = "out" if str(from_id) == str(own_user_id) else "in"

    created_at = _parse_iso(m.get("createdAt")) or datetime.utcnow()
    price_cents = _to_cents(m.get("price"))
    media_ids = _media_ids(m.get("media"))
    is_paid = m.get("isOpened") if price_cents > 0 else None  # NULL = free message
    sender_name = (from_user.get("name") or from_user.get("username") or "")[:255]
    body = m.get("text") or ""
    raw = json.dumps(m, default=str)[:64 * 1024]

    stmt = (
        sqlite_insert(Message)
        .values(
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
            is_tip=bool(m.get("isTip")),
            purchased_at=_parse_iso(m.get("openedAt")) if is_paid else None,
            is_unsent=bool(m.get("isUnsent")),
            raw_json=raw,
            created_at=created_at,
        )
        .on_conflict_do_update(
            index_elements=["account_id", "fan_id", "message_id"],
            set_={
                "body": body,
                "is_paid": is_paid,
                "is_tip": bool(m.get("isTip")),
                "is_unsent": bool(m.get("isUnsent")),
                "media_count": int(m.get("mediaCount") or 0),
                "raw_json": raw,
            },
        )
    )
    await s.execute(stmt)


async def _upsert_fan_identity(
    s,
    account_id: str,
    fan_id: int,
    own_user_id: str,
    with_user: dict | None,
    collected: list[dict],
    *,
    last_in_at: datetime | None,
    last_out_at: datetime | None,
) -> None:
    """Ensure the fans row exists + carry identity (username/name/avatar). Prefer
    the chat's `withUser`; else lift it from the newest INBOUND message's
    fromUser (outbound fromUser is us). AI-extracted facts are never touched
    here — only identity + last-message timestamps."""
    info: dict = {}
    if with_user and with_user.get("id"):
        info = with_user
    else:
        for m in collected:
            fu = m.get("fromUser") or {}
            if fu.get("id") and str(fu.get("id")) != str(own_user_id):
                info = fu
                break

    insert_extra: dict[str, Any] = {}
    if last_in_at is not None:
        insert_extra["last_message_received_at"] = last_in_at
    if last_out_at is not None:
        insert_extra["last_message_sent_at"] = last_out_at

    update_set: dict[str, Any] = {"updated_at": datetime.utcnow()}
    for col, val in (
        ("of_username", info.get("username")),
        ("of_display_name", info.get("name")),
        ("avatar_url", info.get("avatar")),
    ):
        if val is not None:
            update_set[col] = val
    if last_in_at is not None:
        update_set["last_message_received_at"] = last_in_at
    if last_out_at is not None:
        update_set["last_message_sent_at"] = last_out_at

    stmt = (
        sqlite_insert(Fan)
        .values(
            account_id=str(account_id),
            fan_id=int(fan_id),
            of_username=info.get("username"),
            of_display_name=info.get("name"),
            avatar_url=info.get("avatar"),
            source=(
                "fan" if info.get("subscribedOn") else
                "creator_we_follow" if info.get("subscribedBy") else
                "onlyfans"
            ),
            **insert_extra,
        )
        .on_conflict_do_update(
            index_elements=["account_id", "fan_id"],
            set_=update_set,
        )
    )
    await s.execute(stmt)


async def _upsert_chat_and_history(
    s,
    account_id: str,
    fan_id: int,
    *,
    newest_id: int | None,
    newest_text: str | None,
    newest_at: datetime | None,
) -> None:
    """Advance the inbox chat preview + the scrape_history fast-skip cursor —
    both MONOTONICALLY, so backfilling old history can't clobber a newer preview
    a WS event already wrote (guarded by a WHERE on the conflict update)."""
    if newest_id:
        chat_stmt = (
            sqlite_insert(Chat)
            .values(
                account_id=str(account_id),
                fan_id=int(fan_id),
                last_message_id=newest_id,
                last_message_at=newest_at,
                last_message_preview=newest_text,
            )
            .on_conflict_do_update(
                index_elements=["account_id", "fan_id"],
                set_={
                    "last_message_id": newest_id,
                    "last_message_at": newest_at,
                    "last_message_preview": newest_text,
                },
                where=(
                    (Chat.last_message_id.is_(None))
                    | (Chat.last_message_id < newest_id)
                ),
            )
        )
        await s.execute(chat_stmt)

    hist_stmt = (
        sqlite_insert(ScrapeHistory)
        .values(
            account_id=str(account_id),
            fan_id=int(fan_id),
            last_message_id=newest_id,
            last_message_text=newest_text,
            last_scrape_at=datetime.utcnow(),
        )
        .on_conflict_do_update(
            index_elements=["account_id", "fan_id"],
            set_={
                "last_message_id": newest_id,
                "last_message_text": newest_text,
                "last_scrape_at": datetime.utcnow(),
            },
        )
    )
    await s.execute(hist_stmt)


async def _fetch_recent_end(
    client: OFClient, chat_id: int, *,
    last_seen_id: int | None, page_size: int, cap: int, max_pages: int,
) -> tuple[list[dict], bool]:
    """The newest-end catch-up: page `order=desc` with the `id` older-cursor,
    looping on `hasMore`. Stops at `last_seen_id` (fast skip), once `cap`
    messages are collected, or when OF runs out. Returns (messages,
    long_convo) where `long_convo` is whether page 1 reported more history
    behind it — the spec's cheap "is this a long convo" probe (no messagesCount
    exists). Never assumes the page size equals `limit`."""
    collected: list[dict] = []
    cursor: int | None = None
    pages = 0
    long_convo = False
    while pages < max_pages and len(collected) < cap:
        # Sync curl_cffi call → off-thread so the event loop never blocks
        # (same idiom as transaction_ingest's `await asyncio.to_thread`).
        resp = await asyncio.to_thread(
            client.get_messages, chat_id,
            limit=page_size, order="desc", before_id=cursor,
        )
        rows = resp.get("list") or []
        if not rows:
            break
        has_more = bool(resp.get("hasMore"))
        if pages == 0:
            long_convo = has_more  # detect "long" via hasMore on page 1
        pages += 1
        stop = False
        for m in rows:
            mid = m.get("id")
            if mid is None:
                continue
            if last_seen_id is not None and int(mid) <= int(last_seen_id):
                stop = True
                break
            collected.append(m)
            if len(collected) >= cap:
                break
        if stop or not has_more:
            break
        cursor = rows[-1].get("id")
        if cursor is None:
            break
        await asyncio.sleep(_PAGE_SLEEP_S)
    return collected, long_convo


async def _fetch_oldest_end(
    client: OFClient, chat_id: int, *,
    page_size: int, cap: int, max_pages: int,
) -> list[dict]:
    """The convo START: page `order=asc` from `offset=0` FORWARD. OF honors asc
    paging and returns the true first messages, but ignores `limit` (returns
    fewer), so loop bumping `offset` by the rows actually received until `cap`
    is met or `hasMore` is False. Bounded by `cap` so we grab "how it started",
    never the whole convo."""
    collected: list[dict] = []
    offset = 0
    pages = 0
    while pages < max_pages and len(collected) < cap:
        resp = await asyncio.to_thread(
            client.get_messages, chat_id,
            limit=page_size, order="asc", offset=offset,
        )
        rows = resp.get("list") or []
        if not rows:
            break
        pages += 1
        for m in rows:
            if m.get("id") is None:
                continue
            collected.append(m)
            if len(collected) >= cap:
                break
        if not resp.get("hasMore"):
            break
        offset += len(rows)  # NEVER assume len(rows) == page_size
        await asyncio.sleep(_PAGE_SLEEP_S)
    return collected


async def _scrape_one_chat(
    account_id: str,
    own_user_id: str,
    client: OFClient,
    chat_id: int,
    with_user: dict | None,
    *,
    page_size: int,
    max_pages: int,
    recent_cap: int = _SCRAPE_RECENT_CAP,
    oldest_cap: int = _SCRAPE_OLDEST_CAP,
) -> tuple[int, int]:
    """Page one chat and write everything in ONE fresh AsyncSession.

    Recent end always: a bounded newest-first catch-up that stops at the
    last-seen message id (fast skip). On the FIRST scrape of a LONG convo (page
    1 still had history behind it), ALSO grab the convo START via asc/offset so
    the AI gets "how it started" + "where it is now" — the bounded middle is
    intentionally skipped. Capped at ~recent_cap + oldest_cap messages/fan, so a
    long history persists oldest+newest, never thousands. Returns
    (rows_inserted, rows_seen)."""
    fan_id = int(chat_id)

    async with get_session() as s:
        hist = await s.get(ScrapeHistory, (account_id, fan_id))
        last_seen_id = hist.last_message_id if hist else None

    first_scrape = last_seen_id is None
    collected, long_convo = await _fetch_recent_end(
        client, chat_id,
        last_seen_id=last_seen_id, page_size=page_size,
        cap=recent_cap, max_pages=max_pages,
    )

    # Both-ends pull, ONCE: only on the first scrape of a long convo. Later
    # cycles fast-skip the recent end and leave the already-captured start alone.
    if first_scrape and long_convo:
        await asyncio.sleep(_PAGE_SLEEP_S)
        oldest = await _fetch_oldest_end(
            client, chat_id,
            page_size=page_size, cap=oldest_cap, max_pages=max_pages,
        )
        # The two ends can overlap on a medium convo — dedup by id.
        seen_ids = {int(m["id"]) for m in collected if m.get("id") is not None}
        for m in oldest:
            mid = m.get("id")
            if mid is not None and int(mid) not in seen_ids:
                seen_ids.add(int(mid))
                collected.append(m)

    if not collected:
        return (0, 0)

    # Precise insert count: which of the collected ids are genuinely new (a WS
    # event may already have landed some of them).
    ids = [int(m["id"]) for m in collected if m.get("id") is not None]
    newest_id = last_seen_id or 0
    newest_at: datetime | None = None
    newest_text: str | None = None
    last_in_at: datetime | None = None
    last_out_at: datetime | None = None
    for m in collected:
        mid = int(m["id"]) if m.get("id") is not None else 0
        at = _parse_iso(m.get("createdAt"))
        from_id = (m.get("fromUser") or {}).get("id")
        is_out = str(from_id) == str(own_user_id)
        if mid > newest_id:
            newest_id = mid
            newest_at = at
            newest_text = _strip_html(m.get("text") or "")[:200]
        if at is not None:
            if is_out:
                last_out_at = at if last_out_at is None else max(last_out_at, at)
            else:
                last_in_at = at if last_in_at is None else max(last_in_at, at)

    async with get_session() as s:
        existing = set()
        if ids:
            res = await s.execute(
                select(Message.message_id).where(
                    Message.account_id == account_id,
                    Message.fan_id == fan_id,
                    Message.message_id.in_(ids),
                )
            )
            existing = {r[0] for r in res}
        inserted = sum(1 for i in ids if i not in existing)

        await _upsert_fan_identity(
            s, account_id, fan_id, own_user_id, with_user, collected,
            last_in_at=last_in_at, last_out_at=last_out_at,
        )
        for m in collected:
            await _upsert_message(s, account_id, fan_id, own_user_id, m)
        await _upsert_chat_and_history(
            s, account_id, fan_id,
            newest_id=newest_id, newest_text=newest_text, newest_at=newest_at,
        )

    return (inserted, len(collected))


@register("scrape_chats")
async def _automation_scrape_chats(
    account_id: str, payload: dict, *, run_id: int
) -> dict:
    """Reference automation. Targets come from the job payload
    (`chat_ids`/`fan_ids`) or, when absent, a sweep of the most-recent chats
    (`limit`). Each chat is scraped into its own session; one slow/broken chat
    is logged and skipped, never failing the whole run."""
    client = await asyncio.to_thread(_make_client, account_id)
    own_user_id = client.user_id

    page_size = int(payload.get("page_size") or _SCRAPE_PAGE_SIZE)
    max_pages = int(payload.get("max_pages") or _SCRAPE_MAX_PAGES)
    recent_cap = int(payload.get("recent_cap") or _SCRAPE_RECENT_CAP)
    oldest_cap = int(payload.get("oldest_cap") or _SCRAPE_OLDEST_CAP)

    explicit = payload.get("chat_ids") or payload.get("fan_ids")
    targets: list[tuple[int, dict | None]] = []
    if explicit:
        targets = [(int(c), None) for c in explicit]
    else:
        # OF's /chats returns ~10 per page regardless of `limit`, so PAGINATE by
        # offset until we've collected `limit` chats or OF runs dry — a single call
        # would only ever cover the first 10 sidebar chats (the bug that left most
        # fans un-scraped). `_SCRAPE_CHAT_LIMIT` bounds the sweep; a page cap guards
        # against a misbehaving hasMore.
        limit = int(payload.get("limit") or _SCRAPE_CHAT_LIMIT)
        seen_chat_ids: set[int] = set()
        offset = 0
        for _ in range(_SCRAPE_MAX_PAGES):
            if len(targets) >= limit:
                break
            page = await asyncio.to_thread(
                client.list_chats, limit=_SCRAPE_PAGE_SIZE, offset=offset,
                order="recent",
            )
            rows = page.get("list") or []
            if not rows:
                break
            for ch in rows:
                wu = ch.get("withUser") or {}
                cid = wu.get("id") or ch.get("id")
                if cid is None or int(cid) in seen_chat_ids:
                    continue
                seen_chat_ids.add(int(cid))
                targets.append((int(cid), wu))
            offset += len(rows)
            if page.get("hasMore") is False:
                break
        targets = targets[:limit]

    chats_scanned = 0
    chats_failed = 0
    messages_inserted = 0
    messages_seen = 0
    for chat_id, with_user in targets:
        try:
            ins, seen = await _scrape_one_chat(
                account_id, own_user_id, client, chat_id, with_user,
                page_size=page_size, max_pages=max_pages,
                recent_cap=recent_cap, oldest_cap=oldest_cap,
            )
            messages_inserted += ins
            messages_seen += seen
            chats_scanned += 1
        except Exception:
            chats_failed += 1
            log.warning(
                "scrape_chat_failed account=%s chat=%s",
                account_id, chat_id, exc_info=True,
            )
        await asyncio.sleep(_PAGE_SLEEP_S)

    return {
        "targets": len(targets),
        "chats_scanned": chats_scanned,
        "chats_failed": chats_failed,
        "messages_inserted": messages_inserted,
        "messages_seen": messages_seen,
    }


# Registry lives in automation_registry.py. `scrape_chats` self-registers via
# the @register decorator above; P4 automations (02..12) drop one file each into
# service/automations/ and self-register the same way — NO edit to this file, so
# they can be built in parallel without colliding on a shared dict.


# ── The testable seam: run exactly one automation run ────────────────

async def run_once(
    account_id: str,
    kind: str,
    *,
    payload: dict | None = None,
    job_id: int | None = None,
) -> dict:
    """Execute exactly one automation run for (account_id, kind) and return a
    summary dict. No loop, no sleeping, no supervisor — tests call this
    directly.

    Job binding: when neither `payload` nor `job_id` is given, the earliest due
    `scheduled_jobs` row for (account, kind) is CLAIMED and its payload used (so
    "enqueue a scrape job → run_once → it executes" works). A test may also pass
    an explicit `payload` to bypass the queue. Every run writes an
    `automation_runs` row; a bound job is marked done/error/requeued.
    """
    load_automation_plugins()  # ensure P4 plugin modules under service/automations/ self-registered
    fn = get_automation(kind)
    if fn is None:
        log.warning("automation_unknown_kind account=%s kind=%s", account_id, kind)
        run_id = await _open_run(account_id, kind)
        await _finalize_run(run_id, "error", None, f"unknown automation kind: {kind}")
        return {"status": "error", "reason": "unknown_kind", "run_id": run_id}

    # Don't stack a second run of the same (account, kind) over a slow one.
    lock = _run_locks[(account_id, kind)]
    if lock.locked():
        return {"status": "skipped", "reason": "in_flight"}

    async with lock:
        if payload is None and job_id is None:
            claimed = await _claim_due_job(account_id, kind)
            if claimed is not None:
                try:
                    payload = json.loads(claimed.payload_json or "{}")
                except Exception:
                    payload = {}
                job_id = claimed.id
        payload = payload or {}

        run_id = await _open_run(account_id, kind)
        t0 = time.monotonic()
        try:
            stats = await fn(account_id, payload, run_id=run_id) or {}
            stats["duration_ms"] = int((time.monotonic() - t0) * 1000)
            await _finalize_run(run_id, "ok", stats, None)
            if job_id is not None:
                await _mark_job(job_id, "done", None)
            log.info(
                "automation_run_ok account=%s kind=%s run_id=%s job=%s stats=%s",
                account_id, kind, run_id, job_id, stats,
            )
            return {"status": "ok", "run_id": run_id, "job_id": job_id, **stats}
        except asyncio.CancelledError:
            # Shutdown / supervisor cancel: park the run, requeue the job.
            await _finalize_run(run_id, "error", None, "cancelled")
            if job_id is not None:
                await _mark_job_failure(job_id, "cancelled")
            raise
        except Exception as e:
            err = repr(e)[:2000]
            log.warning(
                "automation_run_failed account=%s kind=%s run_id=%s",
                account_id, kind, run_id, exc_info=True,
            )
            await _finalize_run(run_id, "error", None, err)
            if job_id is not None:
                await _mark_job_failure(job_id, err)
            return {"status": "error", "run_id": run_id, "job_id": job_id, "error": err}


# ── Supervisor ───────────────────────────────────────────────────────

async def _reconcile_orphans() -> None:
    """Boot-time self-heal. A fresh process owns ZERO in-flight work, so any row
    still marked 'running' was orphaned by the previous process's restart/crash:
      • a stuck `ScheduledJob.status='running'` makes _materialize_due_rules skip
        that (account, kind) forever — it looks like work is still in progress, so
        the rule silently stops materializing new jobs (a real clog);
      • a stuck `AutomationRun.status='running'` shows as forever-running in the UI
        and inflates the max_runs count (cosmetic).
    Requeue the jobs (the work never finished) and mark the runs interrupted.
    Idempotent; called once before the supervisor's first tick, so it can never
    touch a legitimately in-flight run (none exist yet at this point)."""
    now = datetime.utcnow()
    async with get_session() as s:
        jobs = await s.execute(
            update(ScheduledJob)
            .where(ScheduledJob.status == "running")
            .values(status="pending")
        )
        runs = await s.execute(
            update(AutomationRun)
            .where(AutomationRun.status == "running")
            .values(status="error", completed_at=now,
                    error_text="interrupted (relay restart)")
        )
    if jobs.rowcount or runs.rowcount:
        log.info("reconciled_orphans requeued_jobs=%s interrupted_runs=%s",
                 jobs.rowcount, runs.rowcount)


async def _drain_due_jobs_once() -> None:
    """One supervisor pass: materialize due rules → jobs, sweep expired leases,
    then spawn ONE bounded `run_once` per due (account, kind) pair. Bounded by
    `_MAX_CONCURRENT_RUNS`; the per-(account, kind) lock inside run_once keeps a
    slow run from stacking with the next tick."""
    try:
        await _materialize_due_rules()
    except Exception:
        log.warning("automation_rule_materialize_failed", exc_info=True)
    try:
        await _sweep_expired_leases()
    except Exception:
        log.warning("automation_lease_sweep_failed", exc_info=True)

    pairs = await _due_job_pairs()
    if not pairs:
        return

    # Priority dispatch (W3): higher-priority kinds are spawned first so on the
    # rare same-fan overlap they win the lease race (a new sub gets welcome, not
    # followup). Ties broken by account_id for determinism.
    pairs.sort(key=lambda p: (kind_priority(p[1]), p[0]))

    sem = asyncio.Semaphore(_MAX_CONCURRENT_RUNS)

    async def _one(aid: str, kind: str) -> None:
        async with sem:
            try:
                await run_once(aid, kind)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning(
                    "automation_run_dispatch_failed account=%s kind=%s",
                    aid, kind, exc_info=True,
                )

    await asyncio.gather(*(_one(a, k) for a, k in pairs))


async def automation_supervisor() -> None:
    """Async supervisor loop — mirror of transaction_ingest.start_supervisor /
    server._pump_supervisor: drain a tick, sleep AT END so cadence holds
    regardless of tick duration, return on CancelledError, log+continue on
    anything else. Spawned from server._start_event_pumps and cancelled in
    _stop_event_pumps alongside the other supervisors."""
    global _wake_event
    _wake_event = asyncio.Event()  # created on the running loop (W7 wake)
    log.info(
        "automation_supervisor_started tick=%ds max_concurrent=%d",
        _TICK_INTERVAL_S, _MAX_CONCURRENT_RUNS,
    )
    # Self-heal orphaned 'running' rows from the previous process before the
    # first tick, so a crash/restart mid-run can't permanently starve a rule.
    try:
        await _reconcile_orphans()
    except Exception:
        log.warning("reconcile_orphans_failed", exc_info=True)
    while True:
        try:
            cycle_started = time.monotonic()
            await _drain_due_jobs_once()
            elapsed = time.monotonic() - cycle_started
            # W7: wake early on an inbound event, else hold the 30s cadence.
            # Wait FIRST, clear AFTER: a wake that landed during the drain above
            # leaves the event set, so wait_for returns immediately and we
            # re-drain it on the next cycle instead of losing it.
            try:
                await asyncio.wait_for(
                    _wake_event.wait(), timeout=max(0.0, _TICK_INTERVAL_S - elapsed)
                )
            except asyncio.TimeoutError:
                pass  # normal fallback tick
            _wake_event.clear()
        except asyncio.CancelledError:
            return
        except Exception:
            log.warning("automation supervisor cycle failed", exc_info=True)
            await asyncio.sleep(_TICK_INTERVAL_S)
