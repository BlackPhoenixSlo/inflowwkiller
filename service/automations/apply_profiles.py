"""service/automations/apply_profiles.py — Automation A03: apply_profiles.

Spec: library/one_section_of_automations/03_apply_profiles.md.

TRANSPORT (corrected 2026-06-05, verified live): OnlyFans DOES have a native
endpoint — `PUT /api2/v2/subscriptions/{user_id}` with `displayName` (the custom
nickname, works for ANY fan) and `notice` (the private creator note, SUBSCRIBERS
ONLY — 404 for non-subscribers). Already wired in of_client as
`set_fan_custom_name` / `set_fan_note` / `update_subscription`. The earlier "no
native endpoint" note was wrong (the 3rd-party-only claim was stale).

By default this automation still writes to OUR DB only: each fan's generated
nickname + a 200-char FACT-SHEET note (age/job/loc/recent/hobbies/kinks — the
facts we have, NOT Q/Tease) → `fans.applied_notes`/`applied_notes_at` + the
`fan_profiles` dedup columns, so the fan drawer surfaces them. The OF push
(behind the `push_to_of` payload flag, `_OF_PUSH_SUPPORTED`) wires onto the
of_client helpers above — send `displayName` separately from `notice` and gate
`notice` on an active subscription so a non-subscriber 404 can't swallow the
nickname write.

What it does, per tick (`run(account_id, payload, *, run_id)`):

  1. Load every `fan_profiles` row for the account (DB only — no OF traffic).
  2. Skip globally-blacklisted fans.
  3. Per fan compute `target_nick` (cleaned nickname) and `target_notes` (a
     200-char FACT SHEET built from the fans-row facts we have — age/job/loc/
     recent/hobbies/kinks — re-derived each run so it tracks the facts, W2h).
  4. 24-hour cooldown: if the last applied (nickname, notes) tuple still matches
     the targets AND was applied < 24h ago, skip the fan. `force_ids` bypasses.
  5. Apply: write `applied_notes`/`applied_notes_at` onto the `fans` row, and
     update the `fan_profiles` dedup columns (`applied_notes`,
     `notes_applied_successfully`, `last_applied_at`, `last_applied_nickname`,
     `last_applied_notes`). It NEVER writes `fans.generated_nickname` — gen_info
     is the SOLE author of that column; apply_profiles only READS the nickname
     (to push it to OF + stamp the dedup columns). Each fan's write uses its OWN
     AsyncSession (the SQLAlchemy parallel-session footgun — never share one
     across branches).

Scheduling: self-registers via `@register("apply_profiles")` on import (the
executor auto-imports `service/automations/*`). Insert an `automation_rules` row
with `kind="apply_profiles"` + `trigger_json={"every_seconds": N}` to run it
periodically; the executor's generic `_materialize_due_rules` turns it into a
`scheduled_jobs` row each cycle. NO edit to automation_executor.py.

Payload knobs (all optional): `limit` (sweep ceiling), `force_ids` (bypass the
cooldown for specific fans), `push_to_of` (request an OF push — currently a
no-op, logged, because no native endpoint exists).

Returns a stats dict → automation_runs.stats_json.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from automation_registry import register
from db.engine import get_session
from db.models import Blacklist, Fan, FanProfile
from of_client import OFClient   # OF push (nickname/note) — apply_profiles APPLIES, not gathers
from ._common import build_facts_note, coerce_ids
from .names import strip_spend_tier

log = logging.getLogger("of-relay.automation.apply_profiles")

# ── Knobs (ported from 03_apply_profiles.md) ─────────────────────────
_NOTES_MAX = 200          # textarea[maxlength='200'] in the legacy DOM flow
_COOLDOWN = timedelta(hours=24)
_DEFAULT_LIMIT = 500      # fans swept per tick (keeps a run bounded)

# OF push transport: PUT /api2/v2/subscriptions/{id} with {displayName, notice} —
# wired in of_client (set_fan_custom_name / set_fan_note). Honoured when the
# `push_to_of` payload flag is set; default runs stay DB-only.
_OF_PUSH_SUPPORTED = True

_SLASH_RE = re.compile(r"/{2,}")
_WS_RE = re.compile(r"\s+")


# ── Pure helpers (ports of the spec's pure functions) ────────────────

def _clean_nickname(s: str | None) -> str:
    """Drop empty + placeholder ('??') segments and any derived SPEND TIER, collapse
    slashes, trim (same as gen_info — the model sometimes emits '??' for unknown
    slots).

    The tier strip matters MORE here than in gen_info: this module reads the stored
    `fan_profiles.nickname` — written before the tier was retired for ~1,000 fans —
    and pushes it to the OF chat header. Without the strip it would keep re-asserting
    a '/Free' onto fans who have since paid. See names.strip_spend_tier."""
    if not s:
        return ""
    parts = [p.strip() for p in strip_spend_tier(s).split("/")]
    parts = [p for p in parts if any(ch.isalnum() for ch in p)]
    return _WS_RE.sub(" ", "/".join(parts)).strip()


def _first_nonempty(*vals: str | None) -> str:
    for v in vals:
        if v and v.strip():
            return v.strip()
    return ""


def _persistent_notes(prof: "_Prof") -> str:
    """The sticky 200-char Q+Tease note. Once `applied_notes` is set we reuse it
    verbatim so the note never re-randomises; otherwise pick the first non-empty
    Q + first non-empty Tease, join, and clip (spec get_persistent_notes)."""
    existing = (prof.applied_notes or "").strip()
    if existing:
        return existing[:_NOTES_MAX]
    q = _first_nonempty(prof.q1, prof.q2, prof.q3)
    t = _first_nonempty(prof.tease1, prof.tease2, prof.tease3)
    return (q + "\n" + t).strip()[:_NOTES_MAX]


def _facts_note(prof: "_Prof") -> str:
    """The NOTE: a compact, useful-first fact line from the fans-row facts we have —
    NOT Q/Tease (W2h). Delegates to the shared `build_facts_note` so the drawer note
    and the sheet's bullet_points column are byte-for-byte identical (Recent → Intr →
    Job → Rel → Kinks, short labels; name/age/location/spend omitted)."""
    return build_facts_note(prof.facts, _NOTES_MAX, short_bio=prof.short_bio)


def _within_cooldown(last_applied_at: datetime | None, now: datetime) -> bool:
    if last_applied_at is None:
        return False
    return (now - last_applied_at) < _COOLDOWN


def _is_up_to_date(prof: "_Prof", target_nick: str, target_notes: str,
                   now: datetime) -> bool:
    """24h dedup: skip iff the last applied (nick, notes) still match the targets
    AND were applied < 24h ago. None and '' compare equal (empty == absent)."""
    return (
        (prof.last_applied_nickname or "") == target_nick
        and (prof.last_applied_notes or "") == target_notes
        and _within_cooldown(prof.last_applied_at, now)
    )


# ── DB seams (own session each — house pattern) ──────────────────────

class _Prof:
    """Detached snapshot of the fan_profiles fields A03 reads — so the load
    session can close before the per-fan write sessions open."""
    __slots__ = ("fan_id", "nickname", "short_bio", "q1", "q2", "q3", "tease1",
                 "tease2", "tease3", "applied_notes", "last_applied_at",
                 "last_applied_nickname", "last_applied_notes", "facts")

    def __init__(self, row: FanProfile):
        self.fan_id = int(row.fan_id)
        self.nickname = row.nickname
        self.short_bio = row.short_bio or ""   # appended at the bottom of the note
        self.q1, self.q2, self.q3 = row.q1, row.q2, row.q3
        self.tease1, self.tease2, self.tease3 = row.tease1, row.tease2, row.tease3
        self.applied_notes = row.applied_notes
        self.last_applied_at = row.last_applied_at
        self.last_applied_nickname = row.last_applied_nickname
        self.last_applied_notes = row.last_applied_notes
        self.facts: dict[str, str] = {}   # fans-row facts → the 200-char notes (W2h)


async def _load_blacklist() -> set[int]:
    async with get_session() as s:
        rows = (await s.execute(select(Blacklist.fan_id))).all()
    return {int(r[0]) for r in rows}


async def _load_profiles(account_id: str, limit: int) -> list[_Prof]:
    async with get_session() as s:
        rows = (await s.execute(
            select(FanProfile)
            .where(FanProfile.account_id == account_id)
            .order_by(FanProfile.fan_id)
            .limit(limit)
        )).scalars().all()
        profs = [_Prof(r) for r in rows]
        # Pull the fans-row facts for the same fans → the 200-char notes sheet (W2h).
        if profs:
            frows = (await s.execute(
                select(
                    Fan.fan_id, Fan.real_name, Fan.his_age, Fan.home_city,
                    Fan.home_country, Fan.occupation, Fan.hobbies,
                    Fan.relationship_status, Fan.fetishes, Fan.recent_events,
                ).where(
                    Fan.account_id == account_id,
                    Fan.fan_id.in_([p.fan_id for p in profs]),
                )
            )).all()
            by_id = {int(r.fan_id): r for r in frows}
            for p in profs:
                r = by_id.get(p.fan_id)
                if r is not None:
                    p.facts = {
                        "age": (r.his_age or "").strip(),
                        "job": (r.occupation or "").strip(),
                        "city": (r.home_city or "").strip(),
                        "country": (r.home_country or "").strip(),
                        "hobbies": (r.hobbies or "").strip(),
                        "relationship": (r.relationship_status or "").strip(),
                        "fetishes": (r.fetishes or "").strip(),
                        "recent_events": (r.recent_events or "").strip(),
                    }
    return profs


async def _apply(account_id: str, fan_id: int, target_nick: str,
                 target_notes: str, now: datetime) -> None:
    """Write the cleaned nickname + sticky note into OUR DB, in ONE fresh
    session. Upserts (not bare updates) so a stray missing fans/fan_profiles row
    never fails the run — same defensive pattern as gen_info._persist."""
    # fan_profiles dedup state. applied_notes is sticky: only (re)write it when we
    # actually have a note, so an empty target never clears a previously chosen one.
    prof_set: dict = {
        "notes_applied_successfully": bool(target_notes),
        "last_applied_at": now,
        "last_applied_nickname": target_nick or None,
        "last_applied_notes": target_notes or None,
    }
    if target_notes:
        prof_set["applied_notes"] = target_notes

    # fans row — what the drawer shows. apply_profiles is NOT an author of
    # generated_nickname: gen_info OWNS that column (it writes it in _persist).
    # We only READ the nickname (target_nick, above) to push it to OF + stamp the
    # fan_profiles dedup columns; writing it here would race a concurrent gen_info
    # regen and clobber a fresher nickname. So the fans row gets notes only.
    fan_set: dict = {
        "applied_notes": target_notes or None,
        "applied_notes_at": now,
        "updated_at": now,
    }

    async with get_session() as s:
        await s.execute(
            sqlite_insert(FanProfile)
            .values(account_id=str(account_id), fan_id=int(fan_id), **prof_set)
            .on_conflict_do_update(
                index_elements=["account_id", "fan_id"], set_=prof_set
            )
        )
        await s.execute(
            sqlite_insert(Fan)
            .values(account_id=str(account_id), fan_id=int(fan_id), **fan_set)
            .on_conflict_do_update(
                index_elements=["account_id", "fan_id"], set_=fan_set
            )
        )


async def _push_to_of(client: OFClient, fan_id: int, nick: str, notes: str) -> tuple[bool, bool]:
    """Push nickname (displayName — ANY fan) + note (notice — SUBSCRIBERS only) to OF
    via the native PUT /subscriptions/{id}. SEPARATE calls so a non-subscriber 404 on
    the note can't swallow the nickname write. of_client is sync → run off-thread.
    Returns (nick_pushed, note_pushed)."""
    nick_ok = note_ok = False
    if nick:
        try:
            await asyncio.to_thread(client.set_fan_custom_name, fan_id, nick)
            nick_ok = True
        except Exception:
            log.warning("apply_profiles_push_nick_failed fan=%s", fan_id, exc_info=True)
    if notes:
        try:
            await asyncio.to_thread(client.set_fan_note, fan_id, notes)
            note_ok = True
        except Exception as e:
            # `notice` 404s for non-subscribers — expected; log quietly and move on.
            log.info("apply_profiles_push_note_skipped fan=%s (%s)", fan_id, type(e).__name__)
    return nick_ok, note_ok


async def _write_mirror(account_id: str, fan_id: int, *, nick: str, notes: str,
                        now: datetime) -> None:
    """Mirror the values we JUST pushed to OF into the local columns the UI reads —
    `fans.custom_nickname` (← OF displayName) and `fans.notes` (← OF notice). The
    chat-list rail and the drawer's local fallback read THESE, not `applied_notes`,
    so writing them makes the change show up immediately instead of only after the
    next live `get_user` sync. Only the columns we actually pushed are written, so a
    non-subscriber whose note 404'd never gets a phantom local note."""
    set_: dict = {"updated_at": now}
    if nick:
        set_["custom_nickname"] = nick
    if notes:
        set_["notes"] = notes
    if len(set_) == 1:                          # nothing pushed → nothing to mirror
        return
    async with get_session() as s:
        await s.execute(
            sqlite_insert(Fan)
            .values(account_id=str(account_id), fan_id=int(fan_id), **set_)
            .on_conflict_do_update(index_elements=["account_id", "fan_id"], set_=set_)
        )


# ── The automation entry point ───────────────────────────────────────

@register("apply_profiles")
async def run(account_id: str, payload: dict, *, run_id: int) -> dict:
    payload = payload or {}
    limit = int(payload.get("limit") or _DEFAULT_LIMIT)
    force_ids = coerce_ids(payload.get("force_ids"))

    # Honour the OF-push request only when a transport actually exists. Today it
    # never does (no native endpoint), so we log and proceed DB-only — we never
    # pretend the value reached OnlyFans.
    push_requested = bool(payload.get("push_to_of"))
    of_client_obj = None
    if push_requested and _OF_PUSH_SUPPORTED:
        try:
            of_client_obj = OFClient.from_account(account_id)
        except Exception:
            log.warning("apply_profiles push_to_of: no OF session for %s — DB only",
                        account_id)
    pushed_to_of = of_client_obj is not None

    now = datetime.utcnow()
    blacklist = await _load_blacklist()
    profiles = await _load_profiles(account_id, limit)

    applied = 0
    skipped_cooldown = 0
    skipped_blacklist = 0
    skipped_empty = 0
    errors = 0
    pushed_nick = 0
    pushed_note = 0

    for prof in profiles:
        if prof.fan_id in blacklist:
            skipped_blacklist += 1
            continue

        target_nick = _clean_nickname(prof.nickname)
        target_notes = _facts_note(prof)
        if not target_nick and not target_notes:
            skipped_empty += 1
            continue

        if prof.fan_id not in force_ids and _is_up_to_date(
            prof, target_nick, target_notes, now
        ):
            skipped_cooldown += 1
            continue

        try:
            await _apply(account_id, prof.fan_id, target_nick, target_notes, now)
            applied += 1
            if of_client_obj is not None:
                nk, nt = await _push_to_of(of_client_obj, prof.fan_id,
                                           target_nick, target_notes)
                pushed_nick += int(nk)
                pushed_note += int(nt)
                # Mirror what actually landed on OF into the UI-read columns.
                await _write_mirror(account_id, prof.fan_id,
                                    nick=target_nick if nk else "",
                                    notes=target_notes if nt else "", now=now)
        except Exception:
            errors += 1
            log.warning("apply_profiles_apply_failed account=%s fan=%s",
                        account_id, prof.fan_id, exc_info=True)

    # Swap the cache after a bulk OF update: drop the in-process chat-list cache for
    # this account so the inbox rail re-reads the freshly-mirrored nicknames instead
    # of serving a stale page. Best-effort + lazy import — a missing/!=process
    # relay_cache (e.g. in tests) must never fail the run.
    if pushed_nick or pushed_note:
        try:
            import relay_cache  # noqa: PLC0415 — lazy, optional in-process dep
            relay_cache.invalidate("list_chats", account_id)
        except Exception:
            log.debug("apply_profiles relay_cache invalidate skipped", exc_info=True)

    return {
        "profiles_seen": len(profiles),
        "applied": applied,
        "skipped_cooldown": skipped_cooldown,
        "skipped_blacklist": skipped_blacklist,
        "skipped_empty": skipped_empty,
        "errors": errors,
        "pushed_to_of": pushed_to_of,
        "pushed_nick": pushed_nick,
        "pushed_note": pushed_note,
    }
