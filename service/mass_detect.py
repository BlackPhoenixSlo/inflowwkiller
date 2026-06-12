"""
mass_detect.py — retro-tag OF-native mass blasts the WS pump captured as
individual per-fan rows.

When a creator fires a mass message straight from OnlyFans (not through our
send path), the WS pump writes one real per-fan `messages` row per recipient
with `mass_run_id = NULL` and `automation_kind = NULL`. Nothing links those
N rows, so the Messages page renders them as N identical "from us" rows
instead of one collapsed "MASS · sent to N" summary.

This sweep sessionizes those untagged outbound rows into BURSTS and stamps a
synthetic `MassRun` on each, so the existing collapse in messages.py renders
one summary row per blast (recipient_count = the burst's distinct fans).

Burst rule — consecutive rows (ordered by time within an identical message)
belong to the same blast while:
  • same (account_id, body, price_cents, media_count), AND
  • the gap to the previous row is ≤ `gap_seconds` (default 180s).
A new identical message sent hours later is a DIFFERENT burst (the user's
"yooo sent twice as a mass" → two summary rows, not one).

Safety filters:
  • only `automation_kind IS NULL` rows — per-fan automations (welcome,
    of_ai_chat, …) carry a kind and personalize their text, so they're never
    swept up here.
  • only bursts with ≥ `min_recipients` distinct fans become a MassRun;
    smaller clusters stay as individual rows.

Idempotent: only ever touches `mass_run_id IS NULL` rows, so a re-run tags
only blasts that appeared since the last run. `dry_run=True` reports what it
would do without writing.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, select, update

from db.engine import get_session
from db.models import MassRun, Message

log = logging.getLogger(__name__)

# A mass blast to many fans sprays over seconds/minutes (rate-limited by OF);
# 3 minutes between consecutive sends comfortably bridges that without merging
# two distinct blasts of the same line.
DEFAULT_GAP_SECONDS = 180
# Identical body to this many distinct fans in one burst = a mass send. Below
# this we leave the rows alone (could be a couple of genuine 1:1s).
DEFAULT_MIN_RECIPIENTS = 5

# Marks runs minted by this sweep (vs. send_mass_message / reply_mass_funnel /
# manual UI broadcasts). queue_id stays NULL — these never had an OF queue id.
DETECTED_KIND = "detected_blast"


async def detect_mass_bursts(
    *,
    account_ids: list[str] | None = None,
    gap_seconds: int = DEFAULT_GAP_SECONDS,
    min_recipients: int = DEFAULT_MIN_RECIPIENTS,
    window_start: datetime | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Sessionize untagged outbound rows into bursts and tag each as a MassRun.

    Returns a summary dict: {runs_created, rows_tagged, bursts: [...]}. When
    `dry_run`, no writes happen and `runs_created`/`rows_tagged` reflect what
    WOULD be written.
    """
    gap = timedelta(seconds=gap_seconds)

    # Pull candidate rows ordered so consecutive identical sends are adjacent
    # and time-ascending within an identical message — the order the burst
    # walk depends on.
    q = (
        select(
            Message.account_id,
            Message.fan_id,
            Message.message_id,
            Message.body,
            Message.price_cents,
            Message.media_count,
            Message.created_at,
        )
        .where(Message.direction == "out")
        .where(Message.mass_run_id.is_(None))
        .where(Message.automation_kind.is_(None))
        .order_by(
            Message.account_id,
            Message.body,
            Message.price_cents,
            Message.media_count,
            Message.created_at,
            Message.message_id,
        )
    )
    if account_ids is not None:
        q = q.where(Message.account_id.in_(account_ids))
    if window_start is not None:
        q = q.where(Message.created_at >= window_start)

    async with get_session() as s:
        rows = (await s.execute(q)).all()

    # ── Walk the ordered rows, closing a burst when the identity key changes
    #    or the time gap exceeds `gap`. Each burst collects its member PKs and
    #    distinct fan set.
    bursts: list[dict[str, Any]] = []
    cur_key: tuple | None = None
    cur_members: list[tuple[int, int]] = []  # (fan_id, message_id)
    cur_fans: set[int] = set()
    cur_first: datetime | None = None
    cur_last: datetime | None = None
    cur_account: str | None = None
    cur_body: str = ""

    def _close() -> None:
        if cur_account is None or len(cur_fans) < min_recipients:
            return
        bursts.append({
            "account_id": cur_account,
            "body": cur_body,
            "recipients": len(cur_fans),
            "rows": len(cur_members),
            "members": list(cur_members),
            "first": cur_first,
            "last": cur_last,
        })

    for r in rows:
        key = (r.account_id, r.body, r.price_cents, r.media_count)
        contiguous = (
            key == cur_key
            and cur_last is not None
            and r.created_at is not None
            and (r.created_at - cur_last) <= gap
        )
        if not contiguous:
            _close()
            cur_key = key
            cur_members = []
            cur_fans = set()
            cur_first = r.created_at
            cur_account = r.account_id
            cur_body = r.body or ""
        cur_members.append((int(r.fan_id), int(r.message_id)))
        cur_fans.add(int(r.fan_id))
        cur_last = r.created_at
    _close()

    runs_created = 0
    rows_tagged = 0
    summary_bursts: list[dict[str, Any]] = []
    for b in bursts:
        summary_bursts.append({
            "account_id": b["account_id"],
            "body": (b["body"] or "")[:60],
            "recipients": b["recipients"],
            "rows": b["rows"],
            "first": b["first"].isoformat() if b["first"] else None,
            "last": b["last"].isoformat() if b["last"] else None,
        })
        runs_created += 1
        rows_tagged += b["rows"]

    if not dry_run:
        for b in bursts:
            async with get_session() as s:
                run = MassRun(
                    account_id=b["account_id"],
                    audience_filter="{}",
                    recipient_count=b["recipients"],
                    automation_kind=DETECTED_KIND,
                    started_at=b["first"] or datetime.utcnow(),
                    completed_at=b["last"],
                    status="completed",
                )
                s.add(run)
                await s.flush()  # assign run.id
                rid = run.id
                # Stamp the member rows. message_id is OF-globally-unique, so
                # (account_id, message_id) pins each row; chunk to keep the IN
                # list bounded.
                msg_ids = [m[1] for m in b["members"]]
                for i in range(0, len(msg_ids), 500):
                    chunk = msg_ids[i : i + 500]
                    await s.execute(
                        update(Message)
                        .where(
                            and_(
                                Message.account_id == b["account_id"],
                                Message.message_id.in_(chunk),
                            )
                        )
                        .values(mass_run_id=rid)
                    )

    log.info(
        "detect_mass_bursts: %s runs, %s rows tagged (dry_run=%s)",
        runs_created, rows_tagged, dry_run,
    )
    return {
        "runs_created": runs_created,
        "rows_tagged": rows_tagged,
        "dry_run": dry_run,
        "bursts": summary_bursts,
    }
