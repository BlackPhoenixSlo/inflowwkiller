"""service/audience_include.py — the include-only automation audience.

"Run automations ONLY on fans in this folder": a three-state per-account mode
(off | shadow | enforce) resolved from account_ai_config, gated against a LOCAL
mirror of one operator-picked OF folder (lists/list_members, reconciled by the
`audience_sync` automation), with the AUTOFENCE subtractive exclude list for
mass broadcasts and a send-purpose transport assert backing the sender
classification manifest at the bottom of this file.

Split out of audiences.py on purpose: that module is the shared audience
UTILITY layer (contact guards, Auto_Exclude, mass resolution) and this is one
FEATURE's policy engine. This module imports from audiences, never the other
way around.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime
from typing import Iterable, NamedTuple

from sqlalchemy import func, select

from audiences import _LIST_PAGE, _page_all, _page_all_checked, resolve_window_hours
from db.engine import get_session
from db.models import (
    AccountAiConfig, Fan, List as ListModel, ListMember, Message,
)

log = logging.getLogger("of-relay.audience_include")

AUDIENCE_INCLUDE_KIND = "automation_include"
AUTOFENCE_KIND = "autofence"
AUTOFENCE_LIST_NAME = "AUTOFENCE"
# Grace applies to sync HEALTH only, and asymmetrically: a stale snapshot keeps
# AUTHORIZING sends during grace (admissions may lag) — it never un-removes a
# fan, because removals land on the mirror itself and take effect immediately.
AUDIENCE_GRACE_HOURS_DEFAULT = 24.0
# Fence scale bound: the sync crawl's _page_all backstop is 20k rows, so a
# fence (or include list) at/above that bound cannot be verified complete.
AUDIENCE_MAX_LIST_ROWS = 20_000

AUDIENCE_MODES = ("off", "shadow", "enforce")


class AudienceHalt(RuntimeError):
    """An enforce-mode audience-governed send must HALT (mirror/fence unhealthy,
    kill switch, scale bound). Loud by design: callers report the reason and
    send NOTHING — never degrade to an unfenced/unscoped send."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"audience_halt:{reason}" + (f" {detail}" if detail else ""))
        self.reason = reason


class AudiencePolicy(NamedTuple):
    mode: str                      # EFFECTIVE mode: off | shadow | enforce
    configured_mode: str           # what the operator set (mode may downgrade)
    list_id: int | None            # local lists.id of the include mirror
    of_list_id: int | None
    member_ids: frozenset          # gate-time snapshot (local mirror)
    synced_at: datetime | None
    healthy: bool                  # snapshot complete + fresh within grace
    paused: bool                   # kill switch: pause, never unscope
    auto_add: bool
    reason: str | None             # why downgraded / unhealthy, for logs + UI


_AUDIENCE_OFF = AudiencePolicy(
    mode="off", configured_mode="off", list_id=None, of_list_id=None,
    member_ids=frozenset(), synced_at=None, healthy=True, paused=False,
    auto_add=False, reason=None,
)


def norm_audience_mode(v) -> str:
    """NULL/absent/unknown ≡ 'off' — a corrupt value must degrade to the shipped
    behavior (everything runs), never raise on a send path and never enforce."""
    s = str(v or "").strip().lower()
    return s if s in AUDIENCE_MODES else "off"


def _parse_sync_meta(raw) -> dict:
    try:
        v = json.loads(raw or "{}")
        return v if isinstance(v, dict) else {}
    except (TypeError, ValueError):
        return {}


async def automation_audience(account_id: str) -> AudiencePolicy:
    """Resolve the account's audience policy + frozen member snapshot — the ONE
    read every gated engine calls at its candidate seam (before LLM spend)."""
    async with get_session() as s:
        cfg = await s.get(AccountAiConfig, str(account_id))
        mode = norm_audience_mode(getattr(cfg, "audience_mode", None))
        list_id = getattr(cfg, "audience_list_id", None) if cfg else None
        auto_add = bool(getattr(cfg, "audience_auto_add", None)) if cfg else False
        if mode == "off":
            return _AUDIENCE_OFF
        row = await s.get(ListModel, int(list_id)) if list_id is not None else None
        if (row is None or row.account_id != str(account_id)
                or row.kind != AUDIENCE_INCLUDE_KIND):
            # Config points at nothing usable. Fail CLOSED for enforce (halt,
            # loudly — never silently unscope to the full roster), log for shadow.
            log.error("audience: account=%s mode=%s but include list %r is missing/foreign",
                      account_id, mode, list_id)
            return AudiencePolicy(
                mode=mode, configured_mode=mode, list_id=list_id, of_list_id=None,
                member_ids=frozenset(), synced_at=None, healthy=False,
                paused=False, auto_add=auto_add, reason="include_list_missing")
        members = (await s.execute(
            select(ListMember.fan_id).where(ListMember.list_id == int(row.id))
        )).scalars().all()

    meta = _parse_sync_meta(row.sync_meta_json)
    paused = bool(meta.get("paused"))
    grace_h = resolve_window_hours(meta.get("grace_hours"), AUDIENCE_GRACE_HOURS_DEFAULT)
    synced_at = row.synced_at
    effective = mode
    reason = None
    healthy = True
    if synced_at is None:
        # Just enabled, first sync not landed: enforce SITS IN SHADOW until the
        # mirror exists (plan §5.4) — enforcing an empty snapshot would darken
        # the whole roster on a click; shadow prices it instead.
        if mode == "enforce":
            effective, reason = "shadow", "pending_first_sync"
        healthy = False if mode == "enforce" else True
    else:
        age_h = max(0.0, (datetime.utcnow() - synced_at).total_seconds() / 3600.0)
        if bool(meta.get("truncated")):
            healthy, reason = False, "sync_truncated"
        elif grace_h and age_h > grace_h:
            healthy, reason = False, "snapshot_stale"
    return AudiencePolicy(
        mode=effective, configured_mode=mode, list_id=int(row.id),
        of_list_id=int(row.of_list_id) if row.of_list_id else None,
        member_ids=frozenset(int(m) for m in members),
        synced_at=synced_at, healthy=healthy, paused=paused,
        auto_add=auto_add, reason=reason)


async def filter_candidates(
    account_id: str,
    fan_ids,
    *,
    kind: str,
    policy: AudiencePolicy | None = None,
    stats: dict | None = None,
    intent_ids: Iterable[int] | None = None,
    extra_allowed_ids: Iterable[int] | None = None,
) -> list[int]:
    """Intersect an engine's candidate fan ids with the audience policy — ONE
    call at each gated engine's candidate seam, BEFORE any LLM spend.

    off     → input unchanged (byte-identical behavior, the default).
    shadow  → input unchanged; would-be skips logged + counted WITH their
              denominator (audience_skipped / audience_before).
    enforce → members ∩ input. Unhealthy mirror or kill switch → EMPTY, loudly
              (fail closed; pause is never an unscope). `extra_allowed_ids`
              admits fans the caller just enrolled (send_welcome's fast-path
              auto-add: a provably-new sub whose OF add is pending confirm).

    Every number travels with its denominator: stats gains audience_mode,
    audience_before, audience_skipped, audience_skipped_with_intent (and
    audience_halted when enforcement halted the run).
    """
    ids = [int(f) for f in (fan_ids or [])]
    pol = policy if policy is not None else await automation_audience(account_id)
    if pol.mode == "off":
        return ids          # pure no-op: no stats keys, byte-identical run output
    if stats is not None:
        stats["audience_mode"] = pol.mode
        stats["audience_before"] = len(ids)
    allowed = pol.member_ids | {int(x) for x in (extra_allowed_ids or [])}
    intent = {int(x) for x in (intent_ids or [])}
    dropped = [f for f in ids if f not in allowed]
    with_intent = len([f for f in dropped if f in intent])
    if pol.mode == "shadow":
        if stats is not None:
            stats["audience_skipped"] = len(dropped)
            stats["audience_skipped_with_intent"] = with_intent
        if dropped:
            log.info("audience_shadow account=%s kind=%s would_skip=%d of=%d "
                     "(with_intent=%d, snapshot=%s)",
                     account_id, kind, len(dropped), len(ids), with_intent,
                     pol.synced_at)
        return ids
    # enforce
    if pol.paused or not pol.healthy:
        why = "kill_switch" if pol.paused else (pol.reason or "unhealthy")
        if stats is not None:
            stats["audience_halted"] = why
            stats["audience_skipped"] = len(ids)
            stats["audience_skipped_with_intent"] = len([f for f in ids if f in intent])
        log.error("audience_halt account=%s kind=%s reason=%s — %d candidate(s) "
                  "held, nothing sent (pause never unscopes)",
                  account_id, kind, why, len(ids))
        return []
    kept = [f for f in ids if f in allowed]
    if stats is not None:
        stats["audience_skipped"] = len(dropped)
        stats["audience_skipped_with_intent"] = with_intent
    if dropped:
        log.info("audience_enforced account=%s kind=%s kept=%d skipped=%d of=%d",
                 account_id, kind, len(kept), len(dropped), len(ids))
    return kept


async def audience_allows_fan(
    account_id: str,
    fan_id,
    *,
    kind: str,
    policy: AudiencePolicy | None = None,
) -> tuple[bool, str | None]:
    """Scalar gate for the per-fan libraries (pack_sender's wire point) and the
    at-fire recheck. Returns (allowed, reason). Shadow logs and allows."""
    pol = policy if policy is not None else await automation_audience(account_id)
    if pol.mode == "off":
        return True, None
    inside = int(fan_id) in pol.member_ids
    if pol.mode == "shadow":
        if not inside:
            log.info("audience_shadow account=%s kind=%s fan=%s would_block",
                     account_id, kind, fan_id)
        return True, None
    if pol.paused:
        return False, "kill_switch"
    if not pol.healthy:
        return False, pol.reason or "unhealthy"
    return (True, None) if inside else (False, "outside_audience")


async def check_at_fire(
    account_id: str,
    fan_id,
    *,
    kind: str,
    payload: dict | None = None,
) -> tuple[bool, str | None]:
    """The at-fire audience recheck for work fired from OUR queue (pre-resolved
    recipients, dead-session backlogs, mode flips between enqueue and fire).
    An operator-explicit send carries the typed `send_purpose: "manual"` payload
    stamp and BYPASSES the audience — human sends are exempt by design.

    Scope is honest: this covers our scheduled_jobs queue only. Work handed to
    OF's native scheduler is fired by OF and is beyond any app-side recheck —
    which is why enforce mode must keep automation-originated delayed sends in
    the local queue (see enforce_local_queue_only)."""
    if isinstance(payload, dict) and str(payload.get("send_purpose") or "") == "manual":
        return True, "manual"
    return await audience_allows_fan(account_id, fan_id, kind=kind)


async def enforce_local_queue_only(account_id: str) -> bool:
    """True when automation-originated DELAYED sends must use OUR scheduled_jobs
    queue instead of OF-native scheduledDate (enforce mode: OF fires native
    scheduled sends itself, so the at-fire recheck can never reach them).
    Operator-manual sends may keep using OF-native scheduling — they are exempt."""
    pol = await automation_audience(account_id)
    return pol.configured_mode == "enforce"


# ── AUTOFENCE: subtractive mass enforcement (Track A) ────────────────────────
# Every enforce-mode broadcast keeps its ORIGINAL userLists/filters audience and
# attaches ONE stable per-account AUTOFENCE exclude list (sendable universe −
# include members) via excludedLists — the exact wire shape Auto_Exclude uses in
# production today, so day-1 mechanics rest on zero unverified wire behavior.
# OF resolves excludedLists AT FIRE TIME, so the stable id reaches even
# OF-scheduled sends. Track B (swapping userLists to the custom list id) is a
# probe-gated optimization — NOT implemented; see plan §5 probe 0a.
#
# Rebuild protocol (pre-mortem amendment): shadow-compute → verify-complete
# crawl → ordered delta (adds BEFORE removals) → re-crawl, symmetric difference
# must be 0. Unhealthy fence ⇒ the blast HALTS loudly (AudienceHalt) — never an
# unfenced send. Callers hold broadcast_lock; this must NOT re-take it.


async def _audience_universe_ids(account_id: str) -> set[int]:
    """The local sendable universe — every fan id we know for the account (the
    same source ppv_send's house broadcast subtracts today). Over-broad is the
    SAFE direction for an exclude fence: a stale/extra id excludes someone the
    include list never admitted, which is what enforce mode means."""
    async with get_session() as s:
        rows = (await s.execute(
            select(Fan.fan_id).where(Fan.account_id == str(account_id))
        )).scalars().all()
    return {int(r) for r in rows if r is not None}


def _crawl_list_ids_blocking(client, of_list_id) -> tuple[set[int], bool]:
    """(member ids, hit_backstop) for an OF custom list. Blocking."""
    rows, truncated = _page_all_checked(
        lambda off: client.list_users_in(of_list_id, limit=_LIST_PAGE, offset=off))
    return {int(u["id"]) for u in rows if u.get("id") is not None}, truncated


def _build_autofence_blocking(client, of_list_id, target: set[int]) -> dict:
    """Reconcile the stable AUTOFENCE list to exactly `target`, verified.
    Blocking (sync OF client) — call via asyncio.to_thread. Raises AudienceHalt
    on any state that cannot be verified complete.

    Per-user add failures (OF 403s the odd deleted/blocked/self id — the same
    permanent stragglers that fail-closed 47/116 Auto_Exclude broadcasts) are
    tolerated: an un-addable id is almost always also undeliverable. They are
    REMOVED from the verification target and counted loudly, so the zero-diff
    invariant stays exact against the achievable fence instead of wedging
    forever on one deleted account."""
    current, truncated = _crawl_list_ids_blocking(client, of_list_id)
    if truncated:
        raise AudienceHalt("fence_crawl_truncated",
                           f"list {of_list_id} member crawl hit the 20k backstop")
    unaddable: set[int] = set()
    to_add = sorted(target - current)
    for uid in to_add:                       # adds BEFORE removals
        try:
            client.add_user_to_list(of_list_id, uid)
        except Exception as e:  # noqa: BLE001 — per-user tolerance, see docstring
            unaddable.add(uid)
            log.warning("autofence add failed list=%s uid=%s (%s)",
                        of_list_id, uid, str(e).splitlines()[0][:80])
    for uid in sorted(current - target):
        try:
            client.remove_user_from_list(of_list_id, uid)
        except Exception as e:  # noqa: BLE001 — verify below decides if it matters
            log.warning("autofence remove failed list=%s uid=%s (%s)",
                        of_list_id, uid, str(e).splitlines()[0][:80])
    effective = target - unaddable
    after, truncated = _crawl_list_ids_blocking(client, of_list_id)
    if truncated:
        raise AudienceHalt("fence_verify_truncated",
                           f"list {of_list_id} verify crawl hit the 20k backstop")
    diff = after ^ effective
    if diff:
        raise AudienceHalt(
            "fence_verify_diff",
            f"list {of_list_id} differs from target by {len(diff)} id(s) after rebuild")
    if unaddable:
        log.warning("autofence built with %d un-addable id(s) skipped list=%s "
                    "(size=%d)", len(unaddable), of_list_id, len(effective))
    return {"size": len(effective), "unaddable": len(unaddable)}


def _find_or_create_fence_blocking(client) -> int:
    """The OF-side AUTOFENCE list id — find by name, create if absent. Only ever
    called when no local autofence row pins an id yet; after that the STORED id
    is authoritative for the account's lifetime (already-scheduled OF sends keep
    a valid excludedLists reference across rebuilds)."""
    lists = _page_all(lambda off: client.get_lists(limit=_LIST_PAGE, offset=off))
    lid = next((it.get("id") for it in lists
                if (it.get("name") or "") == AUTOFENCE_LIST_NAME), None)
    if lid is None:
        lid = client.create_list(AUTOFENCE_LIST_NAME).get("id")
    if lid is None:
        raise AudienceHalt("fence_create_failed", "OF returned no list id")
    return int(lid)


async def _persist_fence_state(
    account_id: str, *, of_list_id: int | None, healthy: bool,
    reason: str | None = None, size: int | None = None,
    unaddable: int | None = None,
) -> None:
    now = datetime.utcnow()
    meta = {"healthy": healthy, "reason": reason, "size": size,
            "unaddable": unaddable, "built_at": now.isoformat()}
    async with get_session() as s:
        row = (await s.execute(
            select(ListModel).where(
                ListModel.account_id == str(account_id),
                ListModel.kind == AUTOFENCE_KIND,
            )
        )).scalars().first()
        if row is None:
            row = ListModel(account_id=str(account_id), of_list_id=of_list_id,
                            name=AUTOFENCE_LIST_NAME, kind=AUTOFENCE_KIND,
                            is_system=True)
            s.add(row)
        if of_list_id is not None and not row.of_list_id:
            row.of_list_id = int(of_list_id)
        if healthy:
            row.synced_at = now
        row.sync_meta_json = json.dumps(meta)


async def ensure_autofence(
    account_id: str,
    *,
    client=None,
    policy: AudiencePolicy | None = None,
) -> int | None:
    """Build/verify the stable AUTOFENCE list and return its OF id, or None when
    the mode doesn't fence (off/shadow). Raises AudienceHalt when an enforce-mode
    fence cannot be verified healthy — the caller's broadcast must then HALT
    loudly, never send unfenced. ⚠️ Caller holds broadcast_lock (this doesn't)."""
    pol = policy if policy is not None else await automation_audience(account_id)
    if pol.mode != "enforce":
        return None
    if pol.paused:
        raise AudienceHalt("kill_switch", "audience automation is paused")
    if not pol.healthy:
        raise AudienceHalt(pol.reason or "mirror_unhealthy",
                           "include mirror is not a trustworthy snapshot")
    universe = await _audience_universe_ids(account_id)
    target = universe - pol.member_ids
    if len(target) > AUDIENCE_MAX_LIST_ROWS:
        await _persist_fence_state(account_id, of_list_id=None, healthy=False,
                                   reason="fence_scale")
        raise AudienceHalt("fence_scale",
                           f"fence would need {len(target)} rows (bound "
                           f"{AUDIENCE_MAX_LIST_ROWS}) — enforcement unsupported")
    c = client
    if c is None:
        import automation_executor as ax
        c = await asyncio.to_thread(ax._make_client, account_id)
    # Stable id: the stored autofence row wins; OF lookup only on first build.
    async with get_session() as s:
        row = (await s.execute(
            select(ListModel).where(
                ListModel.account_id == str(account_id),
                ListModel.kind == AUTOFENCE_KIND,
            )
        )).scalars().first()
        of_lid = int(row.of_list_id) if (row is not None and row.of_list_id) else None
    try:
        if of_lid is None:
            of_lid = await asyncio.to_thread(_find_or_create_fence_blocking, c)
        built = await asyncio.to_thread(_build_autofence_blocking, c, of_lid, target)
    except AudienceHalt as e:
        await _persist_fence_state(account_id, of_list_id=of_lid, healthy=False,
                                   reason=e.reason)
        raise
    except Exception as e:
        await _persist_fence_state(account_id, of_list_id=of_lid, healthy=False,
                                   reason="fence_build_error")
        raise AudienceHalt("fence_build_error", str(e).splitlines()[0][:120]) from e
    await _persist_fence_state(account_id, of_list_id=of_lid, healthy=True,
                               size=built["size"], unaddable=built["unaddable"])
    log.info("autofence ok account=%s list=%s size=%d unaddable=%d",
             account_id, of_lid, built["size"], built["unaddable"])
    return of_lid


async def audience_fence_for_broadcast(
    account_id: str,
    *,
    client=None,
    policy: AudiencePolicy | None = None,
    stats: dict | None = None,
) -> list[int]:
    """The fence-health gate every mass sender calls before an audience-governed
    submission, INSIDE its broadcast_lock. Returns the OF list id(s) to append to
    excluded_user_lists. off → []; shadow → [] (would-be fence size logged with
    its denominator); enforce → the verified stable fence id, or AudienceHalt."""
    pol = policy if policy is not None else await automation_audience(account_id)
    if stats is not None and pol.mode != "off":
        stats["audience_mode"] = pol.mode
    if pol.mode == "off":
        return []
    if pol.mode == "shadow":
        universe = await _audience_universe_ids(account_id)
        would = len(universe - pol.member_ids)
        if stats is not None:
            stats["audience_fence_would_exclude"] = would
            stats["audience_universe"] = len(universe)
        log.info("audience_shadow account=%s mass fence would exclude %d of %d "
                 "known fans", account_id, would, len(universe))
        return []
    fence_id = await ensure_autofence(account_id, client=client, policy=pol)
    if stats is not None and fence_id is not None:
        stats["audience_fence_list_id"] = int(fence_id)
    return [int(fence_id)] if fence_id is not None else []


# ── Send-purpose assert at the shared outbound seams ────────────────────────
# Defense-in-depth behind the classification manifest: every automation send
# routed through of_write_paced / send_dropping_bad_media declares WHY it is
# allowed to reach a fan. An untagged send raises under the test harness and
# logs loudly in prod, so a future sender that dodges the manifest still trips
# the wire. Tags: gated / fenced / manual / not_fan_dm / exempt:<reason>.

AUDIENCE_SEND_PURPOSE_BASES = ("gated", "fenced", "manual", "exempt", "not_fan_dm")


def audit_send_purpose(purpose, *, where: str = "send") -> None:
    ok = purpose is not None and \
        str(purpose).split(":", 1)[0] in AUDIENCE_SEND_PURPOSE_BASES
    if ok:
        return
    msg = (f"send_purpose missing/unknown ({purpose!r}) at {where} — every "
           f"automation-originated fan DM must declare one of "
           f"{AUDIENCE_SEND_PURPOSE_BASES}")
    if os.environ.get("CHATTERLY_TEST_MODE") == "1":
        raise AssertionError(msg)
    log.error("send_purpose_untagged where=%s purpose=%r", where, purpose)


# ── Sender classification (enforced by tests/test_audience_coverage.py) ─────
# Every send-capable file in service/automations/ MUST appear here; the coverage
# test rebuilds the send-capable inventory from registered kinds + the send
# primitives and FAILS on any unclassified sender. Enforcement is an audited
# invariant, not a promise — the UI prints the exemption register beside the
# mode control ("ONLY" never appears without it).
AUDIENCE_SENDER_CLASSIFICATION: dict[str, str] = {
    # gated at the candidate seam (per-fan automations, before LLM spend)
    "ai_chatter": "gated",          # pre-seller_owned_fans; `always` can't resurrect
    "of_ai_chat": "gated",          # same snapshot as ai_chatter
    "deep_convo": "gated",
    "autoreply": "gated",           # operator decision O1: gate (3/3)
    "send_followup": "gated",
    "reengage_buyers": "gated",
    "tip_request": "gated",
    "arc_tease": "gated",           # delegates into the fenced mass path
    "cat_stickers": "gated",        # library; gif rides the engines' gated reply
    "pack_sender": "gated",         # library; per-fan check at its single wire point
    "reply_mass_funnel": "gated",
    "mass_nudge": "gated",
    "vault_daily_reminder": "gated",  # delegates into the fenced mass path
    "send_welcome": "gated",        # ordered AFTER auto-add
    "scheduled_send": "gated",      # at-fire check; typed `manual` stamp bypasses
    "nudge_online_fire": "gated",   # send-capable at HEAD; producer gated too
    # mass senders — original audience kept, AUTOFENCE subtracted
    "send_mass_message": "fenced",
    "mass_premade": "fenced",       # pure delegation (test asserts no bypass)
    "online_blast": "fenced",
    "ppv_send": "fenced",           # house broadcast FIRES fenced, never skipped
    # exempt, named reason (printed in the Brain UI exemption register)
    "tip_reward": "exempt:reactive tip acknowledgment — answers a fan action; "
                  "operator-stop gated only",
    "make_right": "exempt:reactive repair/apology — gating it produces "
                  "dispute-shaped failures; operator-stop gated only",
    "unsend_messages": "exempt:cleanup — removes messages, sends nothing new",
    "pacing": "exempt:infrastructure — paces writes, never originates a send",
    "_common": "exempt:the shared send seam itself",
    # not a fan DM
    "auto_posts": "not_fan_dm",
    "auto_stories": "not_fan_dm",
}


async def established_fan_ids(
    account_id: str, fan_ids, *, max_outbound: int, max_inbound: int
) -> set[int]:
    """Subset of `fan_ids` with an EXISTING message relationship — more than
    `max_outbound` outbound OR `max_inbound` inbound rows. Hoisted from
    send_welcome (which keeps a thin delegate) so the audience roster-diff can
    reuse the SAME renewal tolerance: a fan who "appears new" on the roster but
    already carries real history is a returning churner, and returning churners
    are never silently auto-added — they queue for one-click admit."""
    if not fan_ids:
        return set()
    ids = [int(f) for f in fan_ids]
    out_c: dict[int, int] = {}
    in_c: dict[int, int] = {}
    async with get_session() as s:
        rows = (await s.execute(
            select(Message.fan_id, Message.direction, func.count())
            .where(Message.account_id == str(account_id), Message.fan_id.in_(ids))
            .group_by(Message.fan_id, Message.direction)
        )).all()
    for fid, direction, cnt in rows:
        if direction == "out":
            out_c[int(fid)] = int(cnt)
        elif direction == "in":
            in_c[int(fid)] = int(cnt)
    return {
        fid for fid in ids
        if out_c.get(fid, 0) > max_outbound or in_c.get(fid, 0) > max_inbound
    }
