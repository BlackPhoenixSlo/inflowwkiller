"""
stats.py — read-only admin reporting endpoints.

Four read paths, all driven by the already-persisted SQLite tables:

  GET /admin/stats/revenue          → transactions, grouped by day or kind
  GET /admin/stats/grok-cost        → grok_daily_cost rollup rows
  GET /admin/stats/automation-runs  → recent automation_runs (most recent first)
  GET /admin/stats/per-employee     → messages.sent_by_employee_id JOIN
                                       transactions on (account_id, message_id)
                                       for messages-sent / PPV / revenue per op

All four are derived from rows the ingest path already wrote (event_transcoder
for messages+transactions, the Grok runners for the cost tables, the automation
scheduler for runs). We never hit OF here and never recompute counters that
already live denormalized on `fans` / `chats`.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy import and_, case, func, literal_column, select, table

from auth import clamp_account_filter
from db.engine import get_session
from db.models import (
    Account,
    AutomationRun,
    Employee,
    Fan,
    GrokDailyCost,
    Message,
    Transaction,
)


# Lightweight ``Table`` proxy for the SQL view created in migration
# 0012_phase_d_stats. We don't declare it as a mapped ORM class because
# SQLAlchemy's mapper wants a primary key; the view exposes raw rows
# without one. A plain :func:`table()` proxy lets us write ``select()``
# queries against it without that headache.
_attr_view = table(
    "per_employee_revenue_with_attribution",
    literal_column("sent_by_employee_id"),
    literal_column("account_id"),
    literal_column("kind"),
    literal_column("amount_cents"),
    literal_column("occurred_at"),
)


log = logging.getLogger("of-relay.stats")
router = APIRouter()


# Statuses we count toward revenue. `pending` ("loading" in OF's vocab)
# clears in ~7d so excluding it lags reality by a week — the dashboard
# would underreport an account that's actively earning. Including it
# matches OF's own /statistics page (which counts pending subs the moment
# they fire). If a pending row later flips to `chargedback`/`refunded`
# the next ingest tick updates the row in place and the metric self-
# corrects. `refund_pending` (was cleared, now being returned) is
# excluded — that money is leaving.
_TRACKED_STATUSES: tuple[str, ...] = ("cleared", "pending")


def _parse_date(
    s: str | None,
    *,
    field: str,
    request: Request | None = None,
) -> datetime | None:
    """Accept ISO-8601 ("2026-05-22" or "2026-05-22T12:00:00Z" or
    "2026-05-22T23:59:59.999-07:00"). Returns naive UTC datetime to match
    the column convention. 400 on garbage so the caller doesn't silently
    get an empty result.

    Tz handling (Work Unit X):
    - Full ISO with tzinfo (`...-07:00`, `...Z`) is converted to UTC, then
      the tzinfo is stripped — the DB stores naive UTC.
    - Bare-date inputs (YYYY-MM-DD, no time) are inclusive on the `to`
      side — the UI's <input type="date"> can only emit bare dates and
      a user picking "to=05/22" means "through end-of-day May 22", not
      "midnight May 22". `from` keeps start-of-day.
    - For bare-date inputs, when an `X-Timezone-Offset-Minutes` header is
      present (JS `Date.getTimezoneOffset()` convention: +480 for UTC-8),
      the day boundary is interpreted in that local zone and shifted to
      UTC. A user on Pacific (offset=+420 during DST) querying through
      2026-05-22 actually wants up to 2026-05-23T06:59:59.999 UTC.
    """
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"invalid {field}: {e}") from e

    bare = "T" not in s and " " not in s
    if bare and field == "to":
        dt = dt.replace(hour=23, minute=59, second=59, microsecond=999_999)

    if dt.tzinfo is not None:
        # Full ISO with offset — convert to UTC then strip.
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt

    if bare and request is not None:
        # JS getTimezoneOffset returns minutes WEST of UTC (+480 for PST).
        raw = request.headers.get("x-timezone-offset-minutes")
        if raw is not None:
            try:
                offset_min = int(raw)
            except ValueError:
                offset_min = 0
            # Sanity-clamp to ±14h to defend against malformed clients.
            if -14 * 60 <= offset_min <= 14 * 60:
                # Local-wall-clock → UTC: add the westward offset.
                dt = dt + timedelta(minutes=offset_min)

    return dt


# ── 1. Revenue ──────────────────────────────────────────────────────

@router.get("/admin/stats/revenue")
async def revenue(
    account_id: str | None = Query(None),
    from_: str | None = Query(None, alias="from"),
    to: str | None = Query(None),
    group_by: str = Query("day", pattern="^(day|kind)$"),
) -> dict[str, Any]:
    """Sum + count of `transactions.amount_cents` bucketed by `day` (UTC) or
    by `kind` (ppv / tip / subscription / rebill / custom). When grouping by
    day, `kind` is null in the response; when grouping by kind, `day` is null.
    Both filters (account_id, from/to) are optional and AND-ed together."""
    account_ids = clamp_account_filter(account_id)
    start = _parse_date(from_, field="from")
    end = _parse_date(to, field="to")

    async with get_session() as s:
        # SQLite-native day bucket. Postgres would use date_trunc — the
        # SQLAlchemy `func.strftime` call still emits the SQLite function
        # name on Postgres backends, so this file is SQLite-targeted (the
        # rest of the relay is too: db/engine.py defaults to SQLite).
        day_expr = func.strftime("%Y-%m-%d", Transaction.occurred_at).label("day")

        if group_by == "day":
            cols = [day_expr, func.sum(Transaction.amount_cents).label("total_cents"),
                    func.count(Transaction.id).label("count")]
            grp = [day_expr]
            order = [day_expr]
        else:
            cols = [Transaction.kind.label("kind"),
                    func.sum(Transaction.amount_cents).label("total_cents"),
                    func.count(Transaction.id).label("count")]
            grp = [Transaction.kind]
            order = [func.sum(Transaction.amount_cents).desc()]

        q = select(*cols)
        if account_ids is not None:
            q = q.where(Transaction.account_id.in_(account_ids))
        if start:
            q = q.where(Transaction.occurred_at >= start)
        if end:
            q = q.where(Transaction.occurred_at <= end)
        q = q.group_by(*grp).order_by(*order)

        rows = (await s.execute(q)).all()

    if group_by == "day":
        out = [{"day": r.day, "kind": None,
                "total_cents": int(r.total_cents or 0), "count": int(r.count or 0)}
               for r in rows]
    else:
        out = [{"day": None, "kind": r.kind,
                "total_cents": int(r.total_cents or 0), "count": int(r.count or 0)}
               for r in rows]
    return {"rows": out}


# ── 2. Grok daily cost ──────────────────────────────────────────────

@router.get("/admin/stats/grok-cost")
async def grok_cost(
    account_id: str | None = Query(None),
    from_: str | None = Query(None, alias="from"),
    to: str | None = Query(None),
) -> dict[str, Any]:
    """Daily Grok spend rollup. Reads grok_daily_cost directly (no aggregation
    over grok_calls). When `account_id` is omitted, returns ALL rows in the
    range — including the global rollup row (account_id=NULL) and every
    per-account row; the caller can filter client-side."""
    account_ids = clamp_account_filter(account_id)
    start = _parse_date(from_, field="from")
    end = _parse_date(to, field="to")

    async with get_session() as s:
        q = select(
            GrokDailyCost.day,
            GrokDailyCost.account_id,
            GrokDailyCost.cost_cents,
            GrokDailyCost.call_count,
            GrokDailyCost.is_capped,
            GrokDailyCost.updated_at,
        )
        if account_ids is not None:
            q = q.where(GrokDailyCost.account_id.in_(account_ids))
        # `day` is a TEXT 'YYYY-MM-DD' so lexical compare equals chronological.
        if start:
            q = q.where(GrokDailyCost.day >= start.strftime("%Y-%m-%d"))
        if end:
            q = q.where(GrokDailyCost.day <= end.strftime("%Y-%m-%d"))
        q = q.order_by(GrokDailyCost.day.desc(), GrokDailyCost.account_id)
        rows = (await s.execute(q)).all()

    return {
        "rows": [
            {
                "day": r.day,
                "account_id": r.account_id,
                "cost_cents": int(r.cost_cents or 0),
                "call_count": int(r.call_count or 0),
                "is_capped": bool(r.is_capped),
                "updated_at": r.updated_at.isoformat() if r.updated_at else None,
            }
            for r in rows
        ]
    }


# ── 3. Automation runs ──────────────────────────────────────────────

@router.get("/admin/stats/per-fan-data")
async def per_fan_data(
    account_id: str | None = Query(None),
    limit: int = Query(2000, ge=1, le=5000),
) -> dict[str, Any]:
    """Per-fan profile data — exactly what push_to_sheets exports to the Google
    Sheet, read from our DB so the team can check it in-app. One account at a time
    (the UI picks one); with no account_id, every account the principal owns.
    Owner-gated via clamp_account_filter."""
    from automations.push_to_sheets import _HEADER, build_fan_data

    account_ids = clamp_account_filter(account_id)
    rows: list[dict] = []
    for aid in (account_ids or []):
        rows.extend(await build_fan_data(aid, limit))
    # Spend-desc so the highest-value fans surface first.
    rows.sort(key=lambda r: r.get("total_spend") or 0, reverse=True)
    return {"rows": rows, "count": len(rows), "columns": list(_HEADER)}


@router.get("/admin/stats/automation-runs")
async def automation_runs(
    kind: str | None = Query(None),
    account_id: str | None = Query(None),
    limit: int = Query(50, ge=1, le=500),
) -> dict[str, Any]:
    """Most-recent automation runs first. `stats_json` is returned verbatim
    (TEXT, already JSON-serialized by the runner)."""
    account_ids = clamp_account_filter(account_id)
    async with get_session() as s:
        q = select(AutomationRun)
        if kind:
            q = q.where(AutomationRun.kind == kind)
        if account_ids is not None:
            q = q.where(AutomationRun.account_id.in_(account_ids))
        q = q.order_by(AutomationRun.started_at.desc()).limit(limit)
        rows = (await s.execute(q)).scalars().all()

    return {
        "runs": [
            {
                "id": r.id,
                "account_id": r.account_id,
                "kind": r.kind,
                "started_at": r.started_at.isoformat() if r.started_at else None,
                "completed_at": r.completed_at.isoformat() if r.completed_at else None,
                "status": r.status,
                "stats_json": r.stats_json,
                "error_text": r.error_text,
            }
            for r in rows
        ]
    }


# ── 4. Per-employee revenue ─────────────────────────────────────────

_UNATTRIBUTED_BUCKET = "Unattributed"


def _empty_employee_row(eid: int | None, display_name: str | None) -> dict[str, Any]:
    return {
        "employee_id": eid,
        "display_name": display_name,
        "messages_sent": 0,
        "ppv_conversions": 0,
        "revenue_cents": 0,
        "revenue_by_kind": {"ppv": 0, "tip": 0},
    }


@router.get("/admin/stats/per-employee")
async def per_employee(
    from_: str | None = Query(None, alias="from"),
    to: str | None = Query(None),
    by_account: bool = Query(False),
) -> dict[str, Any]:
    """Per-employee revenue rollup. Reads from
    `per_employee_revenue_with_attribution` so PPV-message, tip-message, and
    standalone-tip (7-day lookback) attribution share one definition.

    Numbers per employee:
      • messages_sent     — count of outbound messages this op wrote in window
      • ppv_conversions   — distinct PPV transactions tied to their messages
      • revenue_cents     — SUM(amount_cents) across all view rows (ppv + tip)

    Special bucket: when the view emits `sent_by_employee_id IS NULL` (a
    standalone tip whose 7-day lookback found no outbound message), those
    rows land in an "Unattributed" synthetic bucket so the revenue stays
    visible in the per-employee total.

    ``by_account=true`` adds a `per_account[]` breakdown per employee."""
    start = _parse_date(from_, field="from")
    end = _parse_date(to, field="to")
    # Clamp to the signed-in friend's accounts so per-employee rollups
    # don't mix in other friends' chatter activity.
    account_ids = clamp_account_filter(None)

    # ── Q1: messages_sent per employee (still a direct Message scan;
    # the view only covers revenue rows, not bare message counts). ─────
    mq = (
        select(
            Message.sent_by_employee_id.label("employee_id"),
            Message.account_id.label("account_id"),
            func.count().label("messages_sent"),
        )
        .where(Message.sent_by_employee_id.is_not(None))
    )
    if account_ids is not None:
        mq = mq.where(Message.account_id.in_(account_ids))
    if start:
        mq = mq.where(Message.created_at >= start)
    if end:
        mq = mq.where(Message.created_at <= end)
    if by_account:
        mq = mq.group_by(Message.sent_by_employee_id, Message.account_id)
    else:
        mq = mq.group_by(Message.sent_by_employee_id)

    # ── Q2: revenue + ppv conversions from the view ────────────────
    emp_col = _attr_view.c.sent_by_employee_id
    acct_col = _attr_view.c.account_id
    amt_col = _attr_view.c.amount_cents
    kind_col = _attr_view.c.kind
    occ_col = _attr_view.c.occurred_at

    tq_cols: list[Any] = [
        emp_col.label("employee_id"),
        func.coalesce(func.sum(amt_col), 0).label("revenue_cents"),
        func.coalesce(
            func.sum(case((kind_col == "ppv", 1), else_=0)), 0
        ).label("ppv_conversions"),
    ]
    tq_group: list[Any] = [emp_col]
    if by_account:
        tq_cols.insert(1, acct_col.label("account_id"))
        tq_group.append(acct_col)
    tq = select(*tq_cols)
    if account_ids is not None:
        tq = tq.where(acct_col.in_(account_ids))
    if start:
        tq = tq.where(occ_col >= start)
    if end:
        tq = tq.where(occ_col <= end)
    tq = tq.group_by(*tq_group)

    # ── Q3: revenue_by_kind from the attribution view ───────────────────
    # Previously a Message⨝Transaction inner join, which silently dropped:
    #   • standalone tips attributed via the view's 7d lookback (no
    #     message_id, so no inner-join match) — these show up in the
    #     view but were missed in Q3, so TIP REV column was blank even
    #     though TOTAL was correct.
    #   • orphan tips manually assigned via the /attribute endpoint
    #     (transactions.attributed_employee_id set, message_id NULL) —
    #     view COALESCE picks them up; the old Q3 did not.
    # Reading from the view ensures Q3 matches Q2 exactly for revenue
    # totals and properly splits PPV vs Tip per the view's attribution.
    # NULL employee rows are excluded so the "Unattributed" bucket keeps
    # its {ppv:0,tip:0} defaults — orphan totals already flow via Q2.
    rev_kind_q = select(
        emp_col.label("employee_id"),
        acct_col.label("account_id"),
        func.coalesce(func.sum(case(
            (
                kind_col.in_((
                    "ppv", "ppv_message", "ppv_post", "ppv_stream",
                )),
                amt_col,
            ),
            else_=0,
        )), 0).label("ppv_cents"),
        func.coalesce(func.sum(case(
            (
                kind_col.in_(("tip", "tip_post", "tip_stream")),
                amt_col,
            ),
            else_=0,
        )), 0).label("tip_cents"),
    ).where(emp_col.is_not(None))
    if account_ids is not None:
        rev_kind_q = rev_kind_q.where(acct_col.in_(account_ids))
    if start:
        rev_kind_q = rev_kind_q.where(occ_col >= start)
    if end:
        rev_kind_q = rev_kind_q.where(occ_col <= end)
    rev_kind_q = rev_kind_q.group_by(emp_col, acct_col)

    # Each task opens its OWN AsyncSession — a single AsyncSession is not
    # safe for concurrent execute() under asyncio.gather (raises
    # IllegalStateChangeError); the engine's pool hands one connection
    # per task and SQLite WAL handles parallel readers fine.
    async def _run(stmt: Any) -> list[Any]:
        async with get_session() as ss:
            return (await ss.execute(stmt)).all()

    # Label lookup only — display_name isn't sensitive and the actual
    # data isolation already happened via `clamp_account_filter` above
    # (messages/transactions are scoped to the principal's accounts).
    # Filtering this lookup by user_id used to surface bare "Employee
    # #N" labels in the table whenever a message's sent_by_employee_id
    # pointed at a row outside the principal's roster — typically a
    # chatter mirror Employee created under a co-owner, or a legacy row
    # whose owner is no longer the current user. Lookup-everything keeps
    # the table readable; no rows that weren't already visible become
    # visible, since the data join is account-scoped.
    emp_lookup_q = select(Employee.id, Employee.display_name)
    if by_account:
        acct_lookup_q = select(Account.id, Account.nickname)
        if account_ids is not None:
            acct_lookup_q = acct_lookup_q.where(Account.id.in_(account_ids))
        msg_rows, tx_rows, rev_kind_rows, emp_rows, acct_rows = await asyncio.gather(
            _run(mq), _run(tq), _run(rev_kind_q), _run(emp_lookup_q), _run(acct_lookup_q),
        )
        account_labels: dict[str, str | None] = {aid: nick for aid, nick in acct_rows}
    else:
        msg_rows, tx_rows, rev_kind_rows, emp_rows = await asyncio.gather(
            _run(mq), _run(tq), _run(rev_kind_q), _run(emp_lookup_q),
        )
        account_labels = {}

    names: dict[int, str | None] = {eid: dname for eid, dname in emp_rows}

    # ── Merge ───────────────────────────────────────────────────────────
    # We key by (employee_id, account_id?) so by_account=True produces a
    # per_account[] list per employee. `None` employee_id is the
    # synthetic "Unattributed" bucket holding standalone-tip orphans.
    by_emp: dict[int | None, dict[str, Any]] = {}

    def _bucket(eid: int | None) -> dict[str, Any]:
        if eid in by_emp:
            return by_emp[eid]
        if eid is None:
            row = _empty_employee_row(None, _UNATTRIBUTED_BUCKET)
        else:
            row = _empty_employee_row(eid, names.get(eid))
        if by_account:
            row["per_account"] = {}
        by_emp[eid] = row
        return row

    def _acct_sub(emp_row: dict[str, Any], aid: str | None) -> dict[str, Any]:
        sub_map: dict[str | None, dict[str, Any]] = emp_row["per_account"]
        if aid in sub_map:
            return sub_map[aid]
        sub_map[aid] = {
            "account_id": aid,
            "account_nickname": account_labels.get(aid) if aid else None,
            "messages_sent": 0,
            "ppv_conversions": 0,
            "revenue_cents": 0,
            "revenue_by_kind": {"ppv": 0, "tip": 0},
        }
        return sub_map[aid]

    # Apply message counts. Messages with sent_by_employee_id IS NULL
    # don't appear here (mq filters them out) — those land in
    # /admin/stats/attribution-coverage, not the per-employee revenue
    # rollup. The Unattributed bucket exists only for orphan tip revenue.
    for r in msg_rows:
        eid = int(r.employee_id)
        emp_row = _bucket(eid)
        emp_row["messages_sent"] += int(r.messages_sent or 0)
        if by_account:
            aid = r.account_id
            sub = _acct_sub(emp_row, aid)
            sub["messages_sent"] += int(r.messages_sent or 0)

    for r in tx_rows:
        eid = int(r.employee_id) if r.employee_id is not None else None
        emp_row = _bucket(eid)
        emp_row["ppv_conversions"] += int(r.ppv_conversions or 0)
        emp_row["revenue_cents"] += int(r.revenue_cents or 0)
        if by_account:
            aid = r.account_id
            sub = _acct_sub(emp_row, aid)
            sub["ppv_conversions"] += int(r.ppv_conversions or 0)
            sub["revenue_cents"] += int(r.revenue_cents or 0)

    # Q3 → revenue_by_kind. Rows here are always grouped (emp, acct);
    # we sum into the employee top-level always, and into per_account
    # sub-rows when by_account=true. The Q3 filter excludes orphan tips,
    # so the synthetic "Unattributed" bucket keeps its {ppv:0,tip:0}
    # defaults — frontend doesn't need defensive coding.
    for r in rev_kind_rows:
        eid = int(r.employee_id)
        emp_row = _bucket(eid)
        ppv_c = int(r.ppv_cents or 0)
        tip_c = int(r.tip_cents or 0)
        emp_row["revenue_by_kind"]["ppv"] += ppv_c
        emp_row["revenue_by_kind"]["tip"] += tip_c
        if by_account:
            sub = _acct_sub(emp_row, r.account_id)
            sub["revenue_by_kind"]["ppv"] += ppv_c
            sub["revenue_by_kind"]["tip"] += tip_c

    # Flatten per_account map → sorted list (highest revenue first).
    if by_account:
        for emp_row in by_emp.values():
            sub_map: dict[str | None, dict[str, Any]] = emp_row["per_account"]
            emp_row["per_account"] = sorted(
                sub_map.values(),
                key=lambda x: x["revenue_cents"],
                reverse=True,
            )

    out = sorted(by_emp.values(), key=lambda x: x["revenue_cents"], reverse=True)
    return {"employees": out}


# ── 5. Attribution coverage ─────────────────────────────────────────

def _coverage_pct(attributed: int, total: int) -> float:
    """Percentage with 1-decimal rounding. 0/0 returns 0.0 — the caller
    decides whether 'no traffic' should be surfaced differently."""
    if total <= 0:
        return 0.0
    return round((attributed / total) * 100.0, 1)


@router.get("/admin/stats/attribution-coverage")
async def attribution_coverage(
    from_: str | None = Query(None, alias="from"),
    to: str | None = Query(None),
    account_id: str | None = Query(None),
) -> dict[str, Any]:
    """% of outbound messages with `sent_by_employee_id` filled, per account.
    Window defaults to the last 7 days (UTC). Buckets, per §1.3 of the
    attribution plan:

      attributed_to_human       — employees.is_active = 1
      attributed_to_automation  — employees.display_name = 'Automation'
      unattributed_count        — messages.sent_by_employee_id IS NULL

    If migration 0011 (system Automation employee) hasn't run yet the
    automation bucket is forced to 0 and `automation_employee_present`
    is `false` — never crashes."""
    account_ids = clamp_account_filter(account_id)
    if from_:
        start = _parse_date(from_, field="from")
    else:
        start = datetime.utcnow() - timedelta(days=7)
    if to:
        end = _parse_date(to, field="to")
    else:
        end = datetime.utcnow()

    async with get_session() as s:
        # Tolerant fallback probe — done before the aggregate so we can
        # zero the automation bucket if the seed row is missing.
        auto_present_row = (await s.execute(
            select(Employee.id).where(Employee.display_name == "Automation").limit(1)
        )).first()
        automation_employee_present = auto_present_row is not None

        # LEFT JOIN messages → employees so we can bucket on employee
        # attributes (is_active, display_name) while still counting rows
        # whose sent_by_employee_id is NULL or points at a deleted employee.
        attributed_expr = case((Message.sent_by_employee_id.is_not(None), 1), else_=0)
        human_expr = case(
            (and_(Message.sent_by_employee_id.is_not(None), Employee.is_active == True), 1),  # noqa: E712
            else_=0,
        )
        automation_expr = case((Employee.display_name == "Automation", 1), else_=0)
        unattributed_expr = case((Message.sent_by_employee_id.is_(None), 1), else_=0)

        q = (
            select(
                Message.account_id.label("account_id"),
                func.count().label("outbound_total"),
                func.coalesce(func.sum(attributed_expr), 0).label("attributed_count"),
                func.coalesce(func.sum(human_expr), 0).label("attributed_to_human"),
                func.coalesce(func.sum(automation_expr), 0).label("attributed_to_automation"),
                func.coalesce(func.sum(unattributed_expr), 0).label("unattributed_count"),
            )
            .select_from(Message)
            .outerjoin(Employee, Message.sent_by_employee_id == Employee.id)
            .where(Message.direction == "out")
        )
        if start:
            q = q.where(Message.created_at >= start)
        if end:
            q = q.where(Message.created_at <= end)
        if account_ids is not None:
            q = q.where(Message.account_id.in_(account_ids))
        q = q.group_by(Message.account_id).order_by(Message.account_id)

        rows = (await s.execute(q)).all()

        # Resolve nicknames for the accounts that showed up. Separate
        # lookup keeps the aggregate query unchanged and lets accounts
        # without a row in `accounts` still render (display_name=None).
        acct_ids = [r.account_id for r in rows if r.account_id]
        nickname_by_id: dict[str, str | None] = {}
        if acct_ids:
            nick_rows = (await s.execute(
                select(Account.id, Account.nickname).where(Account.id.in_(acct_ids))
            )).all()
            nickname_by_id = {aid: nick for aid, nick in nick_rows}

    per_account = []
    totals_outbound = 0
    totals_attributed = 0
    for r in rows:
        outbound_total = int(r.outbound_total or 0)
        attributed_count = int(r.attributed_count or 0)
        attributed_to_human = int(r.attributed_to_human or 0)
        attributed_to_automation = (
            int(r.attributed_to_automation or 0) if automation_employee_present else 0
        )
        unattributed_count = int(r.unattributed_count or 0)
        per_account.append({
            "account_id": r.account_id,
            "display_name": nickname_by_id.get(r.account_id),
            "outbound_total": outbound_total,
            "attributed_count": attributed_count,
            "attributed_to_human": attributed_to_human,
            "attributed_to_automation": attributed_to_automation,
            "unattributed_count": unattributed_count,
            "coverage_pct": _coverage_pct(attributed_count, outbound_total),
        })
        totals_outbound += outbound_total
        totals_attributed += attributed_count

    return {
        "from": start.strftime("%Y-%m-%d") if start else None,
        "to": end.strftime("%Y-%m-%d") if end else None,
        "automation_employee_present": automation_employee_present,
        "per_account": per_account,
        "totals": {
            "outbound_total": totals_outbound,
            "coverage_pct": _coverage_pct(totals_attributed, totals_outbound),
        },
    }


@router.get("/admin/stats/attribution-coverage/recent")
async def attribution_coverage_recent(
    limit: int = Query(20, ge=1, le=200),
) -> dict[str, Any]:
    """Debugging hook for the coverage endpoint: most-recent outbound
    messages whose `sent_by_employee_id` is NULL. Used to identify which
    code paths are still leaking attribution when coverage_pct drops."""
    account_ids = clamp_account_filter(None)
    async with get_session() as s:
        q = (
            select(
                Message.account_id,
                Message.fan_id,
                Message.message_id,
                Message.created_at,
                Message.body,
                Message.media_count,
            )
            .where(Message.direction == "out", Message.sent_by_employee_id.is_(None))
            .order_by(Message.created_at.desc())
            .limit(limit)
        )
        if account_ids is not None:
            q = q.where(Message.account_id.in_(account_ids))
        rows = (await s.execute(q)).all()

    return {
        "messages": [
            {
                "account_id": r.account_id,
                "fan_id": int(r.fan_id) if r.fan_id is not None else None,
                "message_id": int(r.message_id) if r.message_id is not None else None,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "body": (r.body or "")[:80],
                "media_count": int(r.media_count or 0),
            }
            for r in rows
        ]
    }


# ── 6. Per-model KPIs ───────────────────────────────────────────────


def _kind_bucket(kind: str | None) -> str:
    """Collapse Phase F's rich kind vocabulary to the 5 chip buckets the
    per-model card knows about. Without this, `ppv_message`/`ppv_post`/
    `tip_post`/etc. silently fall into `custom` because the response dict
    has no key for them — they go to `custom` in the chips and the PPV
    chip shows $0 even when there is real PPV revenue."""
    if not kind:
        return "custom"
    if kind.startswith("ppv"):
        return "ppv"
    if kind.startswith("tip"):
        return "tip"
    if kind == "subscription":
        return "subscription"
    if kind == "rebill":
        return "rebill"
    return "custom"


def _safe_div(num: int, den: int) -> int | None:
    """Integer division returning None when the denominator is 0 — the UI
    distinguishes "no data" from a real zero (a model with no new subs has
    no LTV, not LTV=0)."""
    if den <= 0:
        return None
    return int(num // den)


@router.get("/admin/stats/per-model")
async def per_model(
    from_: str | None = Query(None, alias="from"),
    to: str | None = Query(None),
    account_id: str | None = Query(None),
) -> dict[str, Any]:
    """Per-OF-model KPIs for the stats dashboard. Returns one row per
    account in the window (defaults: last 30 days UTC), sorted by total
    revenue descending.

    Per-model fields:
      • account_id / display_name
      • revenue_by_kind   — {ppv, tip, subscription, rebill, custom}
      • total_revenue_cents
      • messages_sent / ppv_conversions
      • new_subs_count    — DISTINCT fan_id with kind='subscription' in window
      • active_subs_count — point-in-time snapshot via subscription_expires_at
                             (see top-level `active_subs_source`)
      • ltv_cents         — total / new_subs OR null
      • arpu_cents        — total / active OR null

    The four heavy sub-queries (revenue, new-subs, active-subs, messages) run
    concurrently via asyncio.gather to keep p95 latency under 500ms with 30+
    accounts.

    Caveat: `fans.subscription_status` is currently never populated, so we
    fall back to `subscription_expires_at > CURRENT_TIMESTAMP`. If THAT
    column is also empty, `active_subs_count` and `arpu_cents` are null for
    every model and the top-level `active_subs_source` is "unavailable"."""
    account_ids = clamp_account_filter(account_id)
    if from_:
        start = _parse_date(from_, field="from")
    else:
        start = datetime.utcnow() - timedelta(days=30)
    if to:
        end = _parse_date(to, field="to")
    else:
        end = datetime.utcnow()

    # ── Build the five queries ─────────────────────────────────────────
    # Q1: revenue by (account, kind) from raw transactions in window.
    # This intentionally does NOT go through the view — the view drops
    # subscription/rebill/custom kinds, and we need those for the
    # per-model rollup.
    # Counts both cleared and pending — see _TRACKED_STATUSES. Pending
    # is still surfaced separately as pending_by_kind (subset, not
    # additional) so the UI can flag what's not yet bank-cleared.
    revenue_q = select(
        Transaction.account_id.label("account_id"),
        Transaction.kind.label("kind"),
        func.coalesce(func.sum(Transaction.amount_cents), 0).label("amount_cents"),
    ).where(
        Transaction.occurred_at >= start,
        Transaction.occurred_at <= end,
        Transaction.status.in_(_TRACKED_STATUSES),
    )
    if account_ids is not None:
        revenue_q = revenue_q.where(Transaction.account_id.in_(account_ids))
    revenue_q = revenue_q.group_by(Transaction.account_id, Transaction.kind)

    # Q2: new subs in window — DISTINCT fan_id with kind='subscription'.
    # Includes pending so a sub that fires today shows up immediately
    # instead of waiting ~7d to clear. Chargeback/refund flip the row's
    # status on next ingest tick and it drops out of the count.
    new_subs_q = select(
        Transaction.account_id.label("account_id"),
        func.count(func.distinct(Transaction.fan_id)).label("new_subs"),
    ).where(
        Transaction.kind == "subscription",
        Transaction.occurred_at >= start,
        Transaction.occurred_at <= end,
        Transaction.fan_id.is_not(None),
        Transaction.status.in_(_TRACKED_STATUSES),
    )
    if account_ids is not None:
        new_subs_q = new_subs_q.where(Transaction.account_id.in_(account_ids))
    new_subs_q = new_subs_q.group_by(Transaction.account_id)

    # Q6: pending revenue — sum of rows whose status hasn't cleared yet.
    # The synthesis §7 hint "max(occurred_at + payout_pending_days)" wants
    # the latest projected clearance date. SQLite's datetime() modifier
    # needs a literal like '+5 days', so we pull occurred_at + days down
    # and compute the max in Python during the pivot.
    pending_q = select(
        Transaction.account_id.label("account_id"),
        Transaction.kind.label("kind"),
        func.coalesce(func.sum(Transaction.amount_cents), 0).label("pending_cents"),
        Transaction.occurred_at.label("occurred_at"),
        Transaction.payout_pending_days.label("payout_pending_days"),
    ).where(
        Transaction.occurred_at >= start,
        Transaction.occurred_at <= end,
        Transaction.status != "cleared",
    )
    if account_ids is not None:
        pending_q = pending_q.where(Transaction.account_id.in_(account_ids))
    pending_q = pending_q.group_by(
        Transaction.account_id,
        Transaction.kind,
        Transaction.occurred_at,
        Transaction.payout_pending_days,
    )

    # Q3: active subs snapshot — best available signal.
    # Per DA: `subscription_status` is never written. Use the fallback
    # path on `subscription_expires_at` unconditionally; the response
    # surfaces which source produced the count (or marks it
    # unavailable if expires_at is also empty).
    active_subs_q = select(
        Fan.account_id.label("account_id"),
        func.count().label("active_subs"),
    ).where(
        Fan.subscription_expires_at.is_not(None),
        Fan.subscription_expires_at > func.current_timestamp(),
    )
    if account_ids is not None:
        active_subs_q = active_subs_q.where(Fan.account_id.in_(account_ids))
    active_subs_q = active_subs_q.group_by(Fan.account_id)

    # Q3b: transactions-based active-subs fallback. When
    # fans.subscription_expires_at is empty for every fan (most installs
    # today), derive "active" as DISTINCT fan_id with a cleared
    # subscription / rebill within 31 days of `end` — the period a
    # standard monthly OF sub covers. Multi-month subscribers leak out
    # of this window, so the count slightly under-reports yearly
    # subscribers. Acceptable until the WS pump populates expires_at
    # directly.
    tx_active_subs_q = select(
        Transaction.account_id.label("account_id"),
        func.count(func.distinct(Transaction.fan_id)).label("active_subs"),
    ).where(
        Transaction.kind.in_(("subscription", "rebill")),
        Transaction.status.in_(_TRACKED_STATUSES),
        Transaction.fan_id.is_not(None),
        Transaction.occurred_at >= end - timedelta(days=31),
        Transaction.occurred_at <= end,
    )
    if account_ids is not None:
        tx_active_subs_q = tx_active_subs_q.where(Transaction.account_id.in_(account_ids))
    tx_active_subs_q = tx_active_subs_q.group_by(Transaction.account_id)

    # Q4: messages_sent + ppv_conversions per account.
    messages_q = select(
        Message.account_id.label("account_id"),
        func.count().label("messages_sent"),
    ).where(
        Message.direction == "out",
        Message.sent_by_employee_id.is_not(None),
        Message.created_at >= start,
        Message.created_at <= end,
    )
    if account_ids is not None:
        messages_q = messages_q.where(Message.account_id.in_(account_ids))
    messages_q = messages_q.group_by(Message.account_id)

    # PPV conversions ride the view so we use the same attribution
    # definition the per-employee endpoint uses. (Counts every PPV
    # row tied to an attributed message in the window — duplicates
    # are not deduped because a re-purchase is a separate sale.)
    ppv_q = select(
        _attr_view.c.account_id.label("account_id"),
        func.coalesce(
            func.sum(case(
                (
                    _attr_view.c.kind.in_((
                        "ppv", "ppv_message", "ppv_post", "ppv_stream",
                    )),
                    1,
                ),
                else_=0,
            )), 0
        ).label("ppv_conversions"),
    ).where(
        _attr_view.c.occurred_at >= start,
        _attr_view.c.occurred_at <= end,
    )
    if account_ids is not None:
        ppv_q = ppv_q.where(_attr_view.c.account_id.in_(account_ids))
    ppv_q = ppv_q.group_by(_attr_view.c.account_id)

    # Run the heavy queries concurrently. AsyncSession is NOT safe to
    # share across `await` points (sa.exc.IllegalStateChangeError), so
    # each task opens its OWN session — the engine's connection pool
    # hands out one connection per task. SQLite WAL handles the parallel
    # readers fine and we keep the latency win.
    async def _run(stmt: Any) -> list[Any]:
        async with get_session() as ss:
            return (await ss.execute(stmt)).all()

    # pending_q failing must NOT 500 the dashboard — fall back to empty.
    async def _run_pending(stmt: Any) -> list[Any]:
        try:
            return await _run(stmt)
        except Exception:
            log.warning("per-model pending_q failed; reporting 0 pending", exc_info=True)
            return []

    r_rev, r_new, r_act, r_act_tx, r_msg, r_ppv, r_pending = await asyncio.gather(
        _run(revenue_q),
        _run(new_subs_q),
        _run(active_subs_q),
        _run(tx_active_subs_q),
        _run(messages_q),
        _run(ppv_q),
        _run_pending(pending_q),
    )

    # Account labels — single query, separate from the heavy aggregates.
    # Clamp by `account_ids` (not the bare query-param `account_id`) so the
    # seed loop below only materializes cards the principal is allowed to
    # see. Without this, a chatter/owner with 15 accounts would still get
    # a card for every account in the DB (most all-zeros).
    async with get_session() as s:
        acct_q = select(Account.id, Account.nickname)
        if account_ids is not None:
            acct_q = acct_q.where(Account.id.in_(account_ids))
        acct_rows = (await s.execute(acct_q)).all()

    # Pick the best active-subs source. Preferred path is
    # Fan.subscription_expires_at (a real expiry); fallback is the
    # 31-day transactions heuristic (Q3b); if neither has any signal we
    # mark the column unavailable so the UI renders "—" instead of a
    # misleading 0.
    expires_at_signal = any(int(r.active_subs or 0) > 0 for r in r_act)
    tx_signal = any(int(r.active_subs or 0) > 0 for r in r_act_tx)
    if expires_at_signal:
        active_subs_source: str = "subscription_expires_at"
    elif tx_signal:
        active_subs_source = "transactions_31d"
        r_act = r_act_tx
    else:
        active_subs_source = "unavailable"

    # ── Pivot rows by account_id ───────────────────────────────────────
    by_acct: dict[str, dict[str, Any]] = {}

    def _ensure(aid: str) -> dict[str, Any]:
        if aid in by_acct:
            return by_acct[aid]
        row: dict[str, Any] = {
            "account_id": aid,
            "display_name": None,
            "revenue_by_kind": {
                "ppv": 0,
                "tip": 0,
                "subscription": 0,
                "rebill": 0,
                "custom": 0,
            },
            "pending_by_kind": {
                "ppv": 0,
                "tip": 0,
                "subscription": 0,
                "rebill": 0,
                "custom": 0,
            },
            "total_revenue_cents": 0,
            "messages_sent": 0,
            "ppv_conversions": 0,
            "new_subs_count": 0,
            "active_subs_count": None if active_subs_source == "unavailable" else 0,
            "ltv_cents": None,
            "arpu_cents": None,
            "pending_revenue_cents": 0,
            "pending_clears_by": None,
        }
        by_acct[aid] = row
        return row

    # Seed with every known account so models with zero traffic still show.
    nickname_by_id: dict[str, str | None] = {aid: nick for aid, nick in acct_rows}
    for aid in nickname_by_id:
        row = _ensure(aid)
        row["display_name"] = nickname_by_id[aid]

    for r in r_rev:
        row = _ensure(r.account_id)
        bucket = _kind_bucket(r.kind)
        amount = int(r.amount_cents or 0)
        row["revenue_by_kind"][bucket] += amount
        row["total_revenue_cents"] += amount

    for r in r_new:
        row = _ensure(r.account_id)
        row["new_subs_count"] = int(r.new_subs or 0)

    if active_subs_source != "unavailable":
        for r in r_act:
            row = _ensure(r.account_id)
            row["active_subs_count"] = int(r.active_subs or 0)

    for r in r_msg:
        row = _ensure(r.account_id)
        row["messages_sent"] = int(r.messages_sent or 0)

    for r in r_ppv:
        row = _ensure(r.account_id)
        row["ppv_conversions"] = int(r.ppv_conversions or 0)

    # Merge pending revenue + project max clearance date per account.
    # SQLite datetime() needs a literal modifier so we compute the
    # projected clearance in Python: occurred_at + payout_pending_days
    # (default to 0 days if the column is NULL).
    pending_clears: dict[str, datetime] = {}
    for r in r_pending:
        row = _ensure(r.account_id)
        cents = int(r.pending_cents or 0)
        row["pending_revenue_cents"] += cents
        row["pending_by_kind"][_kind_bucket(r.kind)] += cents
        if r.occurred_at is not None:
            days = int(r.payout_pending_days or 0)
            projected = r.occurred_at + timedelta(days=days)
            cur = pending_clears.get(r.account_id)
            if cur is None or projected > cur:
                pending_clears[r.account_id] = projected
    for aid, dt in pending_clears.items():
        by_acct[aid]["pending_clears_by"] = dt.date().isoformat()

    for row in by_acct.values():
        row["ltv_cents"] = _safe_div(row["total_revenue_cents"], row["new_subs_count"])
        active = row["active_subs_count"]
        row["arpu_cents"] = (
            _safe_div(row["total_revenue_cents"], active)
            if isinstance(active, int) else None
        )

    per_model_rows = sorted(
        by_acct.values(),
        key=lambda x: x["total_revenue_cents"],
        reverse=True,
    )

    return {
        "from": start.strftime("%Y-%m-%d"),
        "to": end.strftime("%Y-%m-%d"),
        "active_subs_source": active_subs_source,
        "per_model": per_model_rows,
    }


# ── 7. Attribution orphans (debug) ──────────────────────────────────


@router.get("/admin/stats/attribution-orphans")
async def attribution_orphans(
    limit: int = Query(50, ge=1, le=500),
    account_id: str | None = Query(None),
) -> dict[str, Any]:
    """Most recent tip transactions whose attribution came back NULL —
    i.e., the view's standalone-tip branch couldn't find an outbound
    message within the 7-day lookback (or no message_id at all and no
    recent outbound). Surfaces data-quality issues; if this list grows,
    we likely want to extend the window or look at why outbound writes
    aren't getting a `sent_by_employee_id`."""
    account_ids = clamp_account_filter(account_id)
    async with get_session() as s:
        q = (
            select(
                Transaction.id,
                Transaction.account_id,
                Transaction.fan_id,
                Transaction.amount_cents,
                Transaction.occurred_at,
                Transaction.message_id,
            )
            .where(Transaction.kind.in_(("tip", "tip_post", "tip_stream")))
        )
        # The view emits NULL for two cases:
        #   (a) tip with message_id = NULL, no outbound in 7 days
        #   (b) tip with message_id pointing at a row whose
        #       sent_by_employee_id IS NULL
        # Case (a) is the typical orphan; we surface it directly. Case (b)
        # would already show up in /attribution-coverage. We keep things
        # focused on (a) so the dashboard's "data quality" drawer points
        # at the actionable failure (a tip with no chatter signal at all).
        q = q.where(Transaction.message_id.is_(None))
        if account_ids is not None:
            q = q.where(Transaction.account_id.in_(account_ids))
        q = q.order_by(Transaction.occurred_at.desc()).limit(limit)
        tx_rows = (await s.execute(q)).all()

    return {
        "tips": [
            {
                "transaction_id": int(r.id),
                "account_id": r.account_id,
                "fan_id": int(r.fan_id) if r.fan_id is not None else None,
                "amount_cents": int(r.amount_cents or 0),
                "occurred_at": r.occurred_at.isoformat() if r.occurred_at else None,
                "message_id": int(r.message_id) if r.message_id is not None else None,
            }
            for r in tx_rows
        ]
    }
