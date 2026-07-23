"""service/vault_arc.py — turn an APPROVED PPV week/month into sends that fire themselves.

`vault_ppv_week` proposes; this arms. The operator generates an arc, reads it, and
confirms once — from then on each day's drop leaves on its own day and the arc
stops when it runs out. Nothing here regenerates or re-approves: an arc that has
played out waits for a human, which is the whole difference between "runs
automatically" and "sends forever unattended".

HOW A DAY BECOMES A SEND
------------------------
Every channel `vault_ppv_week` planned maps onto an EXISTING sender, because the
value in those senders is the part that must not be re-implemented — audience
selection, per-fan ownership dedup (never re-sell a frame he already unlocked),
caps, the contact guard, exclude lists:

    mass_ppv (+ its paired feed_paid) → `ppv_send`        one job, `also_post_to_feed`
    feed_paid alone (the opener)      → `auto_posts`      priced wall post
    feed_free (the recap)             → `auto_posts`      price 0
    mass_free (nudge / free taste)    → `arc_tease`       resolved at fire time

Each becomes ONE future-dated `scheduled_jobs` row. `_due_job_pairs` claims those
straight off the table, so a one-shot needs no `AutomationRule` behind it and
there is no recurring cadence to leak — when the row runs, that drop is over.

WHY THE LIBRARY ENTRIES ARE FLAGGED
-----------------------------------
A priced DM day needs a `ppv_library_config_json` entry for `ppv_send` to load.
Those entries are marked `arc_owned` and `ppv_library_config_api._sync_rules`
skips them: without that flag, the operator opening the PPV Library and pressing
Save would mint a RECURRING `ppv_send` rule per arc day, quietly converting a
one-shot story beat into a weekly blast forever.

Suggest-only is not weakened, it is spent deliberately: nothing here runs without
`approve()`, and `approve()` is only reachable from an explicit operator confirm.
"""
from __future__ import annotations

import json
import logging
import random
from datetime import datetime, timedelta
from typing import Any

log = logging.getLogger("chatterly.vault_arc")

# The id prefix that marks a Library entry as owned by an arc. Deliberately NOT
# `vai-` (`vault_ai_to_chatter.VAI_PREFIX`): a `vai-` entry joins the pool the AI
# Chatter/Upseller may pick from 1:1, and an arc day is a one-to-many story beat
# that should land on its planned evening, not get quoted mid-conversation.
ARC_PREFIX = "arc-"

# Where the drops land in the creator's local day. Two bands, so a day that both
# posts and asks does not fire twice on the same minute: the wall builds the day,
# the DM closes it at night.
_BAND_WALL = (10 * 60, 15 * 60)     # feed posts + free nudges
_BAND_CLOSE = (18 * 60, 23 * 60)    # the priced ask
_DEFAULT_SLEEP = ("03:00", "10:00")

# The tease audience: OF's built-in fans+following lists (server-side fan-out,
# the same audience the daily premade blasts name) with the default 6h/2h
# re-touch guards explicitly disarmed — a wall tease goes to EVERYONE. Without
# an audience `send_mass_message` refuses the payload (empty_audience skip)
# while the one-shot job is stamped done: a silent no-send. A free mass never
# counts toward ppv_caps (those count only ppv_send fires).
TEASE_AUDIENCE: dict[str, Any] = {
    "user_lists": ["fans", "following"],
    "exclude_replied_hours": 0,
    "exclude_inbound_hours": 0,
}


def _sleep_window(cfg: dict | None) -> tuple[str, str]:
    win = ((cfg or {}).get("rhythm") or {}).get("sleep_window")
    if isinstance(win, (list, tuple)) and len(win) == 2:
        return (str(win[0]), str(win[1]))
    return _DEFAULT_SLEEP


def _place(band: tuple[int, int], sleep: tuple[str, str], rng: random.Random) -> int:
    """A creator-local minute-of-day inside `band`, jittered per drop and pushed
    clear of the sleep window.

    This is the "let Human Rhythm place it" contract: the arc never lands on the
    same clock minute two days running (a fixed 20:00 every night is the tell),
    and it never fires into hours she is supposed to be asleep."""
    from automations import rhythm

    lo, hi = band
    minute = rng.randrange(lo, hi)
    # Walk forward out of the sleep band rather than clamping to its edge — a
    # clamp would stack every affected day onto the wake minute exactly.
    for _ in range(24):
        probe = datetime(2000, 1, 1) + timedelta(minutes=minute % 1440)
        if not rhythm.in_sleep_window(probe, sleep):
            return minute % 1440
        minute += 30
    return minute % 1440


def _run_at_utc(day_date: datetime, minute_local: int, tz_offset_minutes: int) -> datetime:
    """Creator-local (date, minute-of-day) → the naive UTC instant the job row wants."""
    local = day_date.replace(hour=0, minute=0, second=0, microsecond=0) \
        + timedelta(minutes=int(minute_local))
    return local - timedelta(minutes=int(tz_offset_minutes or 0))


def _entry_id(arc_id: str, seq: int) -> str:
    return f"{ARC_PREFIX}{arc_id}-d{seq}"


def _library_entry(arc_id: str, seq: int, day: dict[str, Any], *,
                   also_feed: bool) -> dict[str, Any]:
    """The PPV-Library entry a priced DM day sends through. `caption_texts` carries
    the day's in-voice line, which `ppv_send._pick_caption` uses VERBATIM — the
    caption the operator approved is the caption that goes on the wire."""
    media = [int(m) for m in (day.get("media_ids") or [])]
    previews = [int(m) for m in (day.get("preview_media_ids") or []) if int(m) in set(media)]
    return {
        "id": _entry_id(arc_id, seq),
        "name": f"Arc {arc_id} · {day.get('weekday')} · {day.get('theme') or ''}"[:80],
        "arc_owned": True,
        "media_ids": media,
        "preview_options": previews,
        "caption_texts": [str(day.get("caption") or "").strip()],
        "caption_pool_key": "",
        "feed_captions": [],
        "feed_caption_pool_key": "",
        "base_price_cents": int(day.get("price_cents") or 0),
        "also_post_to_feed": bool(also_feed),
        "feed_enabled": True,
        "sends_per_week": 1,          # inert: arc entries never get a cadence rule
        "resend_monthly": False,
        "exclude_buyers": True,       # never re-pitch a frame he already unlocked
        "enabled": True,
    }


def _post_job(day: dict[str, Any], price_cents: int) -> dict[str, Any]:
    """An `auto_posts` payload for a wall drop. `price` is DOLLARS (the OF /posts
    field), 0 for the free recap."""
    media = [int(m) for m in (day.get("media_ids") or [])]
    previews = [int(m) for m in (day.get("preview_media_ids") or []) if int(m) in set(media)]
    post: dict[str, Any] = {
        "text": str(day.get("caption") or "").strip(),
        "media_files": media,
        "price": round(int(price_cents) / 100, 2) if price_cents else 0,
    }
    if price_cents and previews:
        post["preview_media_files"] = previews
    return {"posts": [post]}


def plan_jobs(plan: dict[str, Any], *, arc_id: str, start_date: datetime,
              tz_offset_minutes: int, sleep: tuple[str, str] = _DEFAULT_SLEEP,
              ) -> list[dict[str, Any]]:
    """PURE — an approved week/month plan → the ordered job rows that will fire it.

    No DB, no network. `start_date` is the creator-local date the arc's first day
    lands on; every later day is +1. Placement is seeded off `(arc_id, day, kind)`
    so re-planning the same arc yields the same schedule instead of reshuffling."""
    days = plan.get("days")
    if days is None:
        days = [d for wp in (plan.get("weeks") or []) for d in wp.get("days") or []]

    jobs: list[dict[str, Any]] = []
    for seq, day in enumerate(days):
        chans = day.get("channels") or []
        kinds = {c.get("kind") for c in chans}
        date = start_date + timedelta(days=seq)
        price = int(day.get("price_cents") or 0)

        def _at(band: tuple[int, int], tag: str) -> datetime:
            rng = random.Random(f"{arc_id}:{seq}:{tag}")
            return _run_at_utc(date, _place(band, sleep, rng), tz_offset_minutes)

        # A priced DM carries its paired feed post inside `ppv_send`
        # (`also_post_to_feed`) — two jobs would post the set twice.
        if "mass_ppv" in kinds and price > 0:
            jobs.append({
                "seq": seq, "weekday": day.get("weekday"), "channel": "mass_ppv",
                "kind": "ppv_send", "run_at": _at(_BAND_CLOSE, "ppv"),
                "price_cents": price,
                "entry": _library_entry(arc_id, seq, day,
                                        also_feed="feed_paid" in kinds),
                "payload": {"ppv_id": _entry_id(arc_id, seq)},
            })
        elif "feed_paid" in kinds and price > 0:
            # The opener: the wall carries the paid drop, the DM only points at it.
            jobs.append({
                "seq": seq, "weekday": day.get("weekday"), "channel": "feed_paid",
                "kind": "auto_posts", "run_at": _at(_BAND_WALL, "post"),
                "price_cents": price, "entry": None,
                "payload": _post_job(day, price),
            })

        if "feed_free" in kinds:
            jobs.append({
                "seq": seq, "weekday": day.get("weekday"), "channel": "feed_free",
                "kind": "auto_posts", "run_at": _at(_BAND_WALL, "post"),
                "price_cents": 0, "entry": None,
                "payload": _post_job(day, 0),
            })

        for c in chans:
            if c.get("kind") != "mass_free":
                continue
            text = str(c.get("text") or day.get("caption") or "").strip()
            if not text:
                continue
            jobs.append({
                "seq": seq, "weekday": day.get("weekday"), "channel": "mass_free",
                "kind": "arc_tease", "run_at": _at(_BAND_WALL, "dm"),
                "price_cents": 0, "entry": None,
                # A REFERENCE, like a priced day's {"ppv_id"}: the tease line
                # rides the active_arc registry (approve() copies `text` into
                # the booked drop) and `automations/arc_tease.py` materializes
                # text + TEASE_AUDIENCE when the job fires — never frozen into
                # the job row a month ahead.
                "text": text,
                "payload": {"arc_id": arc_id, "seq": seq},
            })

    jobs.sort(key=lambda j: (j["run_at"], j["seq"]))
    return jobs


# ── arming (the only part that touches state) ─────────────────────────────────

async def _load_configs(account_id: str):
    from db.engine import get_session
    from db.models import AccountAiConfig
    async with get_session() as s:
        row = await s.get(AccountAiConfig, account_id)
        lib: dict = {}
        vai: dict = {}
        if row is not None:
            for blob, into in ((row.ppv_library_config_json, "lib"),
                               (row.vault_ai_config_json, "vai")):
                try:
                    parsed = json.loads(blob) if blob else {}
                except Exception:
                    parsed = {}
                if into == "lib":
                    lib = parsed or {}
                else:
                    vai = parsed or {}
        tz = None if row is None else row.utc_offset
        timezone = getattr(row, "timezone", None) if row is not None else None
    return lib, vai, tz, timezone


def preflight(lib_cfg: dict) -> str | None:
    """The one reason an approved arc would silently send nothing. `ppv_send.run`
    refuses every entry when the PPV-Library master is off, so a priced arc armed
    against a dark library would look scheduled and quietly skip all week. Return
    a reason to REFUSE the approval rather than let that happen."""
    if not bool((lib_cfg or {}).get("enabled")):
        return "ppv_library_master_off"
    return None


async def approve(account_id: str, plan: dict[str, Any], *,
                  employee_id: int | None = None,
                  start_date: datetime | None = None) -> dict[str, Any]:
    """Arm an arc: write its Library entries, book every drop as a one-shot job,
    and record what was booked. Replaces any arc still in flight."""
    from datetime import timezone as _tz

    from sqlalchemy import update
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    from automations import rhythm
    from automation_executor import enqueue_job
    from db.engine import get_session
    from db.models import AccountAiConfig

    lib, vai, utc_offset, timezone = await _load_configs(account_id)
    reason = preflight(lib)
    if reason:
        return {"status": "refused", "reason": reason}

    now = datetime.utcnow()
    # Microseconds, not seconds: two approvals inside the same second would share
    # an arc id, so the second one's Library entries would collide with the first's
    # by id and the replace-in-flight sweep would eat its own new rows.
    arc_id = now.strftime("%Y%m%dT%H%M%S%f")
    tz_off = rhythm.tz_offset_for(timezone, utc_offset, now) or 0
    local_today = (now + timedelta(minutes=tz_off)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    start = start_date or local_today
    sleep = _sleep_window(vai)

    jobs = plan_jobs(plan, arc_id=arc_id, start_date=start,
                     tz_offset_minutes=tz_off, sleep=sleep)
    if not jobs:
        return {"status": "refused", "reason": "nothing_to_schedule"}

    # A drop whose slot already passed today would fire the instant it is booked —
    # push it to the same slot tomorrow rather than blasting on approval.
    for j in jobs:
        while j["run_at"] <= now:
            j["run_at"] += timedelta(days=1)

    await cancel(account_id, _reason="superseded")

    entries = [j["entry"] for j in jobs if j["entry"]]
    if entries:
        lib_now, _, _, _ = await _load_configs(account_id)
        ppvs = [p for p in (lib_now.get("ppvs") or [])
                if not str((p or {}).get("id") or "").startswith(ARC_PREFIX)]
        lib_now["ppvs"] = ppvs + entries
        blob = json.dumps(lib_now)
        async with get_session() as s:
            await s.execute(
                sqlite_insert(AccountAiConfig)
                .values(account_id=account_id, utc_offset=utc_offset or 0,
                        ppv_library_config_json=blob, updated_at=now)
                .on_conflict_do_update(
                    index_elements=["account_id"],
                    set_={"ppv_library_config_json": blob, "updated_at": now}))

    booked: list[dict[str, Any]] = []
    for j in jobs:
        job_id = await enqueue_job(account_id, j["kind"], payload=j["payload"],
                                   run_at=j["run_at"],
                                   created_by_employee_id=employee_id)
        drop = {
            "job_id": job_id, "seq": j["seq"], "weekday": j["weekday"],
            "channel": j["channel"], "kind": j["kind"],
            "price_cents": j["price_cents"],
            "run_at": j["run_at"].replace(tzinfo=_tz.utc).isoformat(),
        }
        if "text" in j:  # the tease line arc_tease resolves at fire time
            drop["text"] = j["text"]
        booked.append(drop)

    _, vai_now, _, _ = await _load_configs(account_id)
    vai_now.setdefault("ppv_week", {})["active_arc"] = {
        "arc_id": arc_id, "approved_at": now.replace(tzinfo=_tz.utc).isoformat(),
        "tz_offset_minutes": tz_off, "drops": booked,
    }
    blob = json.dumps(vai_now)
    async with get_session() as s:
        await s.execute(
            sqlite_insert(AccountAiConfig)
            .values(account_id=account_id, utc_offset=utc_offset or 0,
                    vault_ai_config_json=blob, updated_at=now)
            .on_conflict_do_update(
                index_elements=["account_id"],
                set_={"vault_ai_config_json": blob, "updated_at": now}))

    log.info("vault_arc_approved account=%s arc=%s drops=%d first=%s last=%s",
             account_id, arc_id, len(booked),
             booked[0]["run_at"], booked[-1]["run_at"])
    return {"status": "armed", "arc_id": arc_id, "drops": booked}


async def active_arc(account_id: str) -> dict[str, Any] | None:
    """The account's armed arc registry, or None. The one read path for
    `arc_tease` resolution and `status()`."""
    _, vai, _, _ = await _load_configs(account_id)
    return (vai.get("ppv_week") or {}).get("active_arc") or None


async def heal_armed_arcs() -> None:
    """One-way data heal (07-23), run at startup after init_db. Arcs approved
    before the TEASE_AUDIENCE fix booked mass_free drops as bare {text, price}
    payloads that `send_mass_message` skips (empty_audience) while the job is
    stamped done — the teases silently never left. New arcs enqueue the
    audience at booking; this patches the jobs already booked.

    Which jobs are arc teases is not inferred from the payload: every approve()
    records its booked job ids in `active_arc.drops`, and that registry is the
    match key. A tease that skipped within the last 24h is re-opened — an
    audience-less payload can never have sent, so the flip cannot double-send;
    older misses stay done (a days-old wall tease is stale). Idempotent:
    patched payloads carry user_lists and are never touched again.

    Dead code once every pre-fix arc has played out (last booked tease
    2026-08-18) — delete it then."""
    from sqlalchemy import select

    from db.engine import get_session
    from db.models import AccountAiConfig, ScheduledJob

    async with get_session() as s:
        blobs = (await s.execute(
            select(AccountAiConfig.vault_ai_config_json)
            .where(AccountAiConfig.vault_ai_config_json.is_not(None)))).scalars().all()
        tease_ids: list[int] = []
        for blob in blobs:
            try:
                cfg = json.loads(blob) if blob else {}
            except Exception:
                continue
            drops = (((cfg.get("ppv_week") or {}).get("active_arc") or {})
                     .get("drops")) or []
            # Pre-fix arcs booked teases as kind send_mass_message; post-fix
            # arcs book kind arc_tease and never need healing. One malformed
            # entry must not abort the heal for every other account.
            for d in drops:
                if d.get("kind") != "send_mass_message":
                    continue
                try:
                    tease_ids.append(int(d["job_id"]))
                except (KeyError, TypeError, ValueError):
                    continue
        if not tease_ids:
            return

        jobs = (await s.execute(
            select(ScheduledJob).where(ScheduledJob.id.in_(tease_ids)))).scalars().all()
        patched = reopened = 0
        fresh_since = datetime.utcnow() - timedelta(hours=24)
        for j in jobs:
            try:
                payload = json.loads(j.payload_json or "{}")
            except Exception:
                continue
            if "user_lists" in payload:
                continue
            if j.status == "done" and j.run_at and j.run_at >= fresh_since:
                j.status = "pending"
                j.attempts = 0
                reopened += 1
            j.payload_json = json.dumps({**payload, **TEASE_AUDIENCE})
            patched += 1
    if patched:
        log.info("arc_tease_heal patched=%d reopened=%d", patched, reopened)


async def cancel(account_id: str, *, _reason: str = "cancelled") -> dict[str, Any]:
    """Stand an arc down: drop every drop that has not fired yet and disable its
    Library entries. Already-sent drops are history and are left alone."""
    from sqlalchemy import delete, select
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    from db.engine import get_session
    from db.models import AccountAiConfig, ScheduledJob

    lib, vai, utc_offset, _ = await _load_configs(account_id)
    arc = (vai.get("ppv_week") or {}).get("active_arc") or {}
    ids = [int(d["job_id"]) for d in (arc.get("drops") or []) if d.get("job_id")]

    removed = 0
    if ids:
        async with get_session() as s:
            pending = (await s.execute(
                select(ScheduledJob.id).where(ScheduledJob.id.in_(ids),
                                              ScheduledJob.status == "pending")
            )).scalars().all()
            if pending:
                await s.execute(delete(ScheduledJob)
                                .where(ScheduledJob.id.in_(list(pending))))
                removed = len(pending)

    now = datetime.utcnow()
    ppvs = lib.get("ppvs") or []
    touched = False
    for p in ppvs:
        if str((p or {}).get("id") or "").startswith(ARC_PREFIX) and p.get("enabled"):
            p["enabled"] = False
            touched = True
    if touched:
        blob = json.dumps(lib)
        async with get_session() as s:
            await s.execute(
                sqlite_insert(AccountAiConfig)
                .values(account_id=account_id, utc_offset=utc_offset or 0,
                        ppv_library_config_json=blob, updated_at=now)
                .on_conflict_do_update(
                    index_elements=["account_id"],
                    set_={"ppv_library_config_json": blob, "updated_at": now}))

    if arc:
        vai.setdefault("ppv_week", {})["active_arc"] = None
        blob = json.dumps(vai)
        async with get_session() as s:
            await s.execute(
                sqlite_insert(AccountAiConfig)
                .values(account_id=account_id, utc_offset=utc_offset or 0,
                        vault_ai_config_json=blob, updated_at=now)
                .on_conflict_do_update(
                    index_elements=["account_id"],
                    set_={"vault_ai_config_json": blob, "updated_at": now}))
        log.info("vault_arc_%s account=%s arc=%s cancelled_drops=%d",
                 _reason, account_id, arc.get("arc_id"), removed)
    return {"status": "cleared", "cancelled_drops": removed}


async def status(account_id: str) -> dict[str, Any]:
    """What the armed arc is doing right now — each drop's live job state, read
    straight off `scheduled_jobs` so it can never drift from what will fire."""
    from sqlalchemy import select

    from db.engine import get_session
    from db.models import ScheduledJob

    arc = await active_arc(account_id)
    if not arc:
        return {"active": False}

    drops = list(arc.get("drops") or [])
    ids = [int(d["job_id"]) for d in drops if d.get("job_id")]
    states: dict[int, str] = {}
    if ids:
        async with get_session() as s:
            for jid, st in (await s.execute(
                select(ScheduledJob.id, ScheduledJob.status)
                    .where(ScheduledJob.id.in_(ids)))).all():
                states[int(jid)] = str(st)
    for d in drops:
        # A row that is simply GONE already ran and was reaped — not "missing".
        d["state"] = states.get(int(d.get("job_id") or 0), "done")

    pending = sum(1 for d in drops if d["state"] == "pending")
    return {
        "active": True, "arc_id": arc.get("arc_id"),
        "approved_at": arc.get("approved_at"),
        "drops": drops, "pending": pending, "fired": len(drops) - pending,
        "next_at": min((d["run_at"] for d in drops if d["state"] == "pending"),
                       default=None),
    }
