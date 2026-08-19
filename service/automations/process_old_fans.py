"""service/automations/process_old_fans.py — Automation A08: process_old_fans.

Spec: library/one_section_of_automations/08_process_old_fans.md (network-rewrite
mapping at the bottom — the Google-Sheet + Playwright world is replaced by a
payload-supplied id list + DB reads; gen_info/apply_profiles stay DB-driven and
are composed INLINE instead of as `subprocess.run([...])` calls).

⚠️ DEPENDS ON A02 (gen_info) + A03 (apply_profiles): this automation imports both
and runs them as sub-steps over the same `run_id`. They are NOT shelled out —
they're the same `run(account_id, payload, *, run_id)` coroutines the executor
calls, invoked here directly with `force_ids` so they bypass their own
qualification / re-run / cooldown gates for the old-fan batch.

What "old fans" are: subscribers that predate the AI chatter. The legacy script
read them from a Google Sheet and scraped each chat with a browser. In this
backend world the scraping is already done by the WS pump + scrape_chats (their
messages/fans rows exist), so A08 is pure DB work:

  per tick (`run(account_id, payload, *, run_id)`):

    1. Build the old-fan id set from the payload: `fan_ids` (numeric sheet rows)
       + `usernames` (the `@user` sheet rows, resolved against `fans.of_username`
       for THIS account, case-insensitive). Unresolved usernames are counted, not
       guessed — without a fan_id there is nothing to key skip_list / fans on.
    2. Drop ids already onboarded (a `skip_list` row with reason
       `old_fan_pre_ai`) unless `reprocess` is set — the cheap idempotency gate so
       a re-run doesn't re-spend Grok on the whole sheet.
    3. FLAG every old fan (idempotent, own session): insert a `skip_list` row
       `(account_id, fan_id, reason="old_fan_pre_ai")` so of_ai_chat never opens
       them — the skip_list row is what stops the AI. The fans upsert carries
       identity-safe defaults so a sheet id that was never chatted still gets a row.
    4. Unless `flag_only` (the spec's `--no-scrape`): compose A02 then A03 over the
       batch — `gen_info.run(force_ids=…)` to generate/refresh the profile, then
       `apply_profiles.run(force_ids=…)` to materialise nickname + sticky note into
       OUR DB (DB-only, bounded by A03's own limits — see acceptance). A forced fan
       with NO chat history simply isn't profiled by gen_info (it builds candidates
       from `messages`); it stays flagged-only, which still satisfies "profiled +
       applied OR added to skip_list".

The legacy `data/ai_chat_skip_<model>.txt` mirror is intentionally dropped: in
this arch the `skip_list` table is the single source of truth that of_ai_chat
reads (of_ai_chat.py `_load_stop_lists`), so the txt file would be write-only.

Cross-model note: the legacy `flag_fan()` flagged the same id under every model in
models.json. Here automations run per `account_id` (the executor dispatches one
(account, kind) at a time), so A08 flags for the account it's invoked on. To flag
the old-fan list across accounts, schedule an `automation_rules` row per account.

Scheduling: self-registers via `@register("process_old_fans")` on import (the
executor auto-imports `service/automations/*`). Insert an `automation_rules` row
with `kind="process_old_fans"` and a `trigger_json` carrying the id list in its
payload to run it; the executor's generic `_materialize_due_rules` schedules it.
NO edit to automation_executor.py.

Payload knobs (all optional): `fan_ids` (numeric old-fan ids), `usernames`
(`@user` strings resolved via fans.of_username), `recent_limit` (int — when NO
fan_ids/usernames are given, take the N most-recent fans as the batch),
`recent_by` ("subscribed"|"messaged", default "subscribed" — orders that fallback
by `subscribed_at` desc or `last_message_received_at` desc), `model` (passed to
gen_info), `limit` (sweep ceiling forwarded to gen_info + apply_profiles),
`flag_only` (skip the gen_info/apply_profiles sub-steps — the `--no-scrape` path),
`reprocess` (re-run the batch even for already-onboarded fans).

Returns a stats dict → automation_runs.stats_json.
"""
from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from automation_registry import register
from db.engine import get_session
from db.models import Fan, SkipList
from ._common import HARD_SKIP_REASONS, coerce_ids
from .apply_profiles import run as apply_profiles_run
from .gen_info import run as gen_info_run

log = logging.getLogger("of-relay.automation.process_old_fans")

# ── Knobs (ported from 08_process_old_fans.md) ───────────────────────
_SKIP_REASON = "old_fan_pre_ai"
# Skip_list reasons that mean "this fan is already settled — don't re-onboard
# them as a fresh old-fan". Our own marker PLUS the durable HARD restricts
# (muted_creator / manual_restrict): those are deliberate "no automations"
# states, not a bare membership row to override.
_ONBOARDED_REASONS = frozenset({_SKIP_REASON}) | HARD_SKIP_REASONS


# ── Username resolution (DB-first replacement for the sidebar scrape) ─

async def _resolve_usernames(account_id: str, usernames: list[str]) -> tuple[set[int], list[str]]:
    """Map `@user`-style sheet entries to fan_ids via `fans.of_username` for this
    account (case-insensitive, `@` stripped). Returns (resolved_ids, unresolved).

    The legacy script scrolled the chat sidebar to resolve a username; we already
    have every chatted fan in `fans`, so a pure-Python lookup replaces it. A name
    with no fans row is returned as unresolved (counted, never guessed).
    """
    wanted = {u.lstrip("@").strip().lower() for u in usernames if u and u.strip()}
    if not wanted:
        return set(), []
    async with get_session() as s:
        rows = (await s.execute(
            select(Fan.fan_id, func.lower(Fan.of_username)).where(
                Fan.account_id == str(account_id),
                func.lower(Fan.of_username).in_(list(wanted)),
            )
        )).all()
    by_name = {name: int(fid) for fid, name in rows if name}
    resolved = {fid for fid in by_name.values()}
    unresolved = sorted(wanted - set(by_name))
    return resolved, unresolved


# ── "Most recent N" source (no explicit id list) ─────────────────────

async def _recent_fan_ids(account_id: str, recent_limit: int, recent_by: str) -> set[int]:
    """When the payload carries NO `fan_ids`/`usernames`, build the old-fan batch
    from the N most-recent fans on this account: ordered by `subscribed_at` desc
    (recent_by="subscribed") or `last_message_received_at` desc (recent_by=
    "messaged", served by ix_fans_last_msg). NULL timestamps sort last under SQLite
    DESC, so fans with a real value win the N slots."""
    order_col = (Fan.last_message_received_at if recent_by == "messaged"
                 else Fan.subscribed_at)
    async with get_session() as s:
        rows = (await s.execute(
            select(Fan.fan_id)
            .where(Fan.account_id == str(account_id))
            .order_by(order_col.desc())
            .limit(int(recent_limit))
        )).all()
    return {int(r[0]) for r in rows}


# ── Idempotency gate ─────────────────────────────────────────────────

async def _already_onboarded(account_id: str, ids: set[int]) -> set[int]:
    """Subset of `ids` that already carry a skip_list row with our reason — i.e.
    a previous A08 run already onboarded them (the `already_processed` gate)."""
    if not ids:
        return set()
    async with get_session() as s:
        rows = (await s.execute(
            select(SkipList.fan_id).where(
                SkipList.account_id == str(account_id),
                SkipList.fan_id.in_(list(ids)),
                SkipList.reason.in_(tuple(_ONBOARDED_REASONS)),
            )
        )).all()
    return {int(r[0]) for r in rows}


# ── Flagging (own session; sequential upserts — no parallel execute) ──

async def _flag_fans(account_id: str, ids: set[int], now: datetime) -> int:
    """Mark every old fan skip-listed, idempotently.

    One session, sequential awaits (the SQLAlchemy parallel-session footgun only
    bites concurrent execute() on a SHARED session — a loop of awaits is safe).
    Both upserts use do_nothing: a pre-existing skip row (any reason) is
    preserved, and the fans insert only ensures a never-chatted sheet id still
    gets a row that of_ai_chat will skip — an existing fan is left untouched.
    """
    flagged = 0
    async with get_session() as s:
        for fid in ids:
            await s.execute(
                sqlite_insert(SkipList)
                .values(account_id=str(account_id), fan_id=int(fid),
                        reason=_SKIP_REASON, added_at=now)
                .on_conflict_do_nothing(index_elements=["account_id", "fan_id"])
            )
            await s.execute(
                sqlite_insert(Fan)
                .values(account_id=str(account_id), fan_id=int(fid),
                        updated_at=now)
                .on_conflict_do_nothing(index_elements=["account_id", "fan_id"])
            )
            flagged += 1
    return flagged


# ── The automation entry point ───────────────────────────────────────

@register("process_old_fans")
async def run(account_id: str, payload: dict, *, run_id: int) -> dict:
    """Onboard a batch of pre-AI fans: flag them out of the AI chatter, then
    (unless flag_only) profile + apply over the batch via gen_info + apply_profiles.

    payload:
      fan_ids:      [int|str, …]  numeric old-fan ids (the sheet's column A).
      usernames:    [str, …]      `@user` sheet rows, resolved via fans.of_username.
      recent_limit: int           NO fan_ids/usernames → take the N most-recent fans.
      recent_by:    str           "subscribed" (default) | "messaged" — orders that
                                  fallback by subscribed_at / last_message_received_at.
      model:        str           override forwarded to gen_info.
      limit:        int           sweep ceiling forwarded to gen_info + apply_profiles.
      flag_only:    bool          only flip DB flags (the spec's --no-scrape).
      reprocess:    bool          re-run even fans already onboarded.
    """
    payload = payload or {}
    fan_ids = coerce_ids(payload.get("fan_ids"))
    raw_usernames = payload.get("usernames")
    usernames = [str(u) for u in raw_usernames] if isinstance(raw_usernames, (list, tuple)) else []
    flag_only = bool(payload.get("flag_only"))
    reprocess = bool(payload.get("reprocess"))

    resolved_ids, unresolved = await _resolve_usernames(account_id, usernames)
    old_ids = set(fan_ids) | resolved_ids

    # No explicit id list (and no usernames) → fall back to the N most-recent fans.
    # An explicit fan_ids/usernames payload always wins (recent_* ignored).
    if not fan_ids and not usernames and payload.get("recent_limit"):
        recent_by = str(payload.get("recent_by") or "subscribed").lower()
        if recent_by not in ("subscribed", "messaged"):
            recent_by = "subscribed"
        old_ids = await _recent_fan_ids(account_id, int(payload["recent_limit"]), recent_by)

    if not old_ids:
        return {
            "old_fans": 0,
            "unresolved_usernames": len(unresolved),
            "flagged": 0,
            "processed": 0,
            "note": "no old-fan ids in payload",
        }

    # Idempotency: drop fans we already onboarded, unless an explicit reprocess.
    already = set() if reprocess else await _already_onboarded(account_id, old_ids)
    to_process = old_ids - already

    now = datetime.utcnow()
    # Always (re)flag the full set — flagging is idempotent and cheap, and it
    # repairs a fan whose skip_list row was cleared.
    flagged = await _flag_fans(account_id, old_ids, now)

    gen_stats: dict | None = None
    apply_stats: dict | None = None
    if not flag_only and to_process:
        sub_payload: dict = {"force_ids": sorted(to_process)}
        if payload.get("model"):
            sub_payload["model"] = payload["model"]
        if payload.get("limit"):
            sub_payload["limit"] = int(payload["limit"])
        # Forward the OF-push request to apply_profiles (gen_info ignores it) so
        # onboarding can write nicknames/notes onto OnlyFans, not just our DB.
        if payload.get("push_to_of"):
            sub_payload["push_to_of"] = True

        # Compose A02 then A03 over the batch, sharing this run's run_id. They open
        # no automation_runs row of their own (only the executor does, for A08).
        try:
            gen_stats = await gen_info_run(account_id, sub_payload, run_id=run_id)
        except Exception:
            log.warning("process_old_fans_gen_info_failed account=%s", account_id, exc_info=True)
        try:
            apply_stats = await apply_profiles_run(account_id, sub_payload, run_id=run_id)
        except Exception:
            log.warning("process_old_fans_apply_failed account=%s", account_id, exc_info=True)

    return {
        "old_fans": len(old_ids),
        "unresolved_usernames": len(unresolved),
        "already_onboarded": len(already),
        "flagged": flagged,
        "processed": len(to_process),
        "flag_only": flag_only,
        "gen_info": gen_stats,
        "apply_profiles": apply_stats,
    }
