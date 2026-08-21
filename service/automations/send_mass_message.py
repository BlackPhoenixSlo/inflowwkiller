"""service/automations/send_mass_message.py — A10 `send_mass_message`.

Spec: library/one_section_of_automations/10_send_mass_message.md (+ 15_funnel_schema.md).

The DOM-era script opened `/my/chats/send`, ticked Fans/Following, excluded the
Mass_Exclude list, attached vault media and clicked Send. This is the
network-rewrite of that flow as a P4 automation: it POSTs OF's broadcast
endpoint via `of_client.send_mass_message` (`POST /api2/v2/messages/queue`) — NO
DOM — and persists the broadcast the same way the per-fan send path does.

What it reads (the prompt's "mass_message_funnels / lists / fans"):
  • `mass_message_funnels` — the funnel row supplies `opening_message` (and the
    opening vault folder/indices fallbacks, per 15 §"How it's consumed"). The
    payload may override the text directly.
  • `lists` / `list_members` — DB list ids in the payload are expanded to fan
    ids so we can write an optimistic row per recipient (OF list NAMES like
    'fans'/'recent' are passed through to OF untouched as `userLists`).
  • `fans` — explicit `included_users` recipients.

What it writes (the acceptance bar):
  • one `mass_runs` row (the broadcast anchor, linked to the funnel),
  • a `messages` row per *known* recipient — reusing the EXACT T-MASS optimistic
    persistence (`attribution.write_mass_optimistic_rows` + the echoed-id
    reconcile), so a broadcast shows up in the chat cache instead of never
    (the WS pump skips outbound events),
  • the `automation_runs` row is opened/closed by `run_once` — we just return a
    stats dict.

Self-registers via `@register("send_mass_message")` on import — no edit to any
shared file, so it builds in parallel with the other P4a automations. Uses its
OWN AsyncSession(s) (one per `get_session()` block) and `of_client` only.

Payload shape (all optional; an empty payload is a no-op run)::

    {
      "funnel_id": 3,                 # or "funnel_name"/"funnel": "strokes"
      "text": "override opener",      # else funnel.opening_message
      "price": 0,
      "media_files": [123, 456],      # explicit OF vault media ids
      "included_users": [1001, 1002], # explicit fan ids
      "list_ids": [7],                # our DB list ids → expanded to fan ids
      "user_lists": ["fans"],         # OF built-in list names, passed through
      "excluded_users": [9009],
      "exclude_list_ids": [4],
      "previews": [123],              # unlocked preview media ids (PPV)
      "online_only": true,            # OF resolves "online now" server-side
      "filters": {"online": 1}        # raw OF audience filters (Mass Online)
    }

Returns (stats dict) include `queue_id` (OF's broadcast id from the send
response) so a caller can schedule a forever-window unsend of THIS broadcast.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime

from sqlalchemy import select

from attribution import (
    reconcile_mass_placeholder,
    write_mass_optimistic_rows,
    write_outbound_attribution,
)
import automation_executor as ax  # shared _make_client seam (tests patch ax._make_client)
from automation_registry import register
from automations._wordfilter import (  # compliance word filter
    banned_hit_summary, filter_banned, load_banned_words,
)
from db.engine import get_session
from db.models import FunnelAccountMedia, ListMember, MassMessageFunnel, MassRun
from event_transcoder import _parse_iso, _to_cents

log = logging.getLogger("of-relay.automation.send_mass_message")


def _int_list(raw) -> list[int]:
    """Coerce a payload field to a list of ints, dropping anything non-numeric."""
    if not isinstance(raw, (list, tuple)):
        return []
    out: list[int] = []
    for v in raw:
        try:
            out.append(int(v))
        except (TypeError, ValueError):
            continue
    return out


async def _opener_media(funnel_id: int, account_id: str) -> tuple[list[int], list[int]]:
    """This model's opener vault media for the funnel (FunnelAccountMedia) →
    (media_files, previews). MEDIA is per-account because vault ids don't carry
    between models; the funnel only holds the shared opener TEXT. Returns ([], [])
    when this account hasn't mapped opener media yet."""
    async with get_session() as s:
        row = await s.get(FunnelAccountMedia, (int(funnel_id), str(account_id)))
    if row is None:
        return [], []
    try:
        media = _int_list(json.loads(row.opening_media_ids or "[]"))
    except Exception:
        media = []
    # The opener has no per-step preview slot of its own; previews ride the PPV
    # steps. Keep the signature symmetric for any future opener-preview use.
    return media, []


async def _resolve_funnel(s, payload: dict) -> MassMessageFunnel | None:
    """Look up the funnel by id (preferred) or unique name."""
    fid = payload.get("funnel_id")
    if fid is not None:
        try:
            return await s.get(MassMessageFunnel, int(fid))
        except (TypeError, ValueError):
            return None
    name = payload.get("funnel_name") or payload.get("funnel")
    if name:
        return (
            await s.execute(
                select(MassMessageFunnel).where(MassMessageFunnel.name == str(name))
            )
        ).scalar_one_or_none()
    return None


async def _members_of_lists(s, list_ids: list[int]) -> list[int]:
    """Expand DB list ids → their member fan ids (so explicit-list audiences get
    an optimistic row each, just like `included_users`)."""
    if not list_ids:
        return []
    rows = (
        await s.execute(
            select(ListMember.fan_id).where(ListMember.list_id.in_(list_ids))
        )
    ).all()
    return [int(r[0]) for r in rows]


@register("send_mass_message")
async def run(account_id: str, payload: dict, *, run_id: int) -> dict:
    """Broadcast a funnel opener (or an explicit text) to the resolved audience,
    mint a `mass_runs` row, and persist one optimistic `messages` row per known
    recipient. Returns a stats dict (lands in `automation_runs.stats_json`)."""
    payload = payload or {}
    # Attribution tag for MassRun / optimistic rows (Mass Messages tab badge). A
    # caller like ppv_send passes its own kind; default is this automation's name.
    attr_kind = str(payload.get("automation_kind") or "send_mass_message")
    # Optional caller-side id for this send (ppv_send stamps the PPV id), stored on
    # the mass_runs row so the caller's duplicate gate has a ledger that survives a
    # mid-run cancel — see `_last_ppv_send` in ppv_send.py.
    attr_ref = payload.get("automation_ref")

    # ── 1) Resolve text + audience (funnels / lists / fans) ──────────────
    async with get_session() as s:
        funnel = await _resolve_funnel(s, payload)
        # Capture the id INSIDE the session (the object expires on close) — it's
        # the dedup key for exclude_funnel_responders (R1/R2).
        funnel_id_resolved = int(funnel.id) if funnel is not None else None

        # Explicit fans + DB-list members → the KNOWN recipients we can write
        # optimistic rows for. OF list NAMES go through untouched as userLists.
        included = _int_list(payload.get("included_users") or payload.get("fan_ids"))
        list_ids = _int_list(payload.get("list_ids"))
        included += await _members_of_lists(s, list_ids)

        excluded = _int_list(payload.get("excluded_users"))
        excluded += await _members_of_lists(s, _int_list(payload.get("exclude_list_ids")))

    # ── 1b) DB/OF-sourced audience + DEFAULT-ON contact guard ────────────
    # recent-chat / unread ADD fans; exclude-replied / exclude-inbound DROP
    # fans — the SAME resolution the relay's /messages/queue handler runs, so a
    # mass_premade broadcast targets the identical audience as the Mass Online
    # composer. The two exclude windows are DEFAULT-ON (absent → 6h outbound /
    # 2h inbound; explicit 0 = off) so a rule or a manual blast that forgets to
    # set them still won't re-touch a fan we just messaged — the over-send bug
    # this whole change fixes. Runs OUTSIDE the session above (own sessions /
    # off-thread OF calls).
    from audiences import (
        BROADCAST_DEFAULT_INBOUND_H,
        BROADCAST_DEFAULT_OUTBOUND_H,
        MASSDMEXCLUDE_LIST,
        MASSPPVEXCLUDE_LIST,
        resolve_mass_audience,
        resolve_window_hours,
    )
    out_h = resolve_window_hours(
        payload.get("exclude_replied_hours"), BROADCAST_DEFAULT_OUTBOUND_H)
    in_h = resolve_window_hours(
        payload.get("exclude_inbound_hours"), BROADCAST_DEFAULT_INBOUND_H)
    resolved = await resolve_mass_audience(
        account_id,
        included_users=included,
        excluded_users=excluded,
        recent_chat_hours=payload.get("recent_chat_hours"),
        recent_chat_limit=payload.get("recent_chat_limit"),
        exclude_replied_hours=out_h or None,   # explicit 0 → None → guard off
        exclude_inbound_hours=in_h or None,
        # Opt-in "last chatted (either direction) within N h" guard — absent →
        # off. Do-Not-Mass + pending-send excludes apply regardless.
        exclude_last_chat_hours=payload.get("exclude_last_chat_hours"),
        exclude_funnel_responders=funnel_id_resolved,  # R1/R2 answered-once dedup
        unread_limit=payload.get("unread_limit"),
        # Priced broadcast → PPV opt-out list; unpriced text → DM opt-out list.
        exclude_list_name=(MASSPPVEXCLUDE_LIST if float(payload.get("price") or 0) > 0 else MASSDMEXCLUDE_LIST),
    )
    included = resolved["included_users"]
    excluded = resolved["excluded_users"]
    skipped_answered = resolved.get("skipped_funnel_responders") or []

    # Dedup while preserving order; drop excluded from the known set.
    excl_set = set(excluded)
    seen: set[int] = set()
    recipients: list[int] = []
    for fid in included:
        if fid in excl_set or fid in seen:
            continue
        seen.add(fid)
        recipients.append(fid)

    user_lists = payload.get("user_lists") or []  # OF built-in names / list ids
    # OF list names/ids to EXCLUDE server-side (e.g. 'Mass_Exclude') — passed
    # straight through to OF's excludeUserLists, same as the relay's send path.
    excluded_user_lists = payload.get("excluded_user_lists") or []
    # Mass Online targeting (same knobs as the relay's online blast): OF resolves
    # the audience server-side, so online_only/filters count as a valid audience
    # even with no explicit recipients/user_lists.
    online_only = bool(payload.get("online_only"))
    filters = payload.get("filters") if isinstance(payload.get("filters"), dict) else None
    text = payload.get("text") or (funnel.opening_message if funnel else None)
    if not text:
        log.warning("send_mass_message: no text (funnel=%s) — skipping", funnel)
        return {"status": "skipped", "reason": "no_text"}
    if not recipients and not user_lists and not online_only and not filters:
        log.warning("send_mass_message: empty audience — skipping")
        return {"status": "skipped", "reason": "empty_audience"}

    # ── Compliance word filter (operator-editable banned-word list) ──────
    # Scanned ONCE here, before the single OF broadcast call. filter_banned owns the
    # policy (whole-turn block; empty list → text unchanged); this site only reacts to
    # the verdict. A block returns cleanly — nothing is minted yet, so there's no
    # dangling run. Automations that don't route through here scan at their own send
    # chokepoint with the same helper.
    _banned, _banned_mode = await load_banned_words(account_id)
    _scanned, _hits = filter_banned([text], _banned, _banned_mode)
    if _scanned is None:
        _uniq = banned_hit_summary(_hits)
        log.warning("send_mass_message: BLOCKED broadcast — banned word(s) %r "
                    "account=%s", _uniq, account_id)
        return {"status": "skipped", "reason": "banned_words", "hits": _uniq}
    if _hits:
        log.info("send_mass_message: masked banned word(s) %r account=%s",
                 banned_hit_summary(_hits), account_id)
    text = _scanned[0]

    price = payload.get("price") or 0
    media_files = _int_list(payload.get("media_files"))
    previews = _int_list(payload.get("previews"))
    # No explicit opener media in the payload but this IS a funnel send → use the
    # model's per-account opener media (vault ids are per-account; the funnel only
    # holds shared text). Falls back to nothing if this model hasn't mapped any.
    if not media_files and funnel is not None:
        media_files, opener_previews = await _opener_media(funnel.id, account_id)
        if media_files and not previews:
            previews = opener_previews

    # Attribution: a broadcast from the worker is the system Automation actor
    # (no X-Employee-Id in a background run). Best-effort — NULL is acceptable.
    employee_id: int | None = None
    try:
        from employees import get_automation_employee_id
        employee_id = await get_automation_employee_id()
    except Exception:
        log.debug("automation employee lookup failed; mass run attribution NULL", exc_info=True)

    # OF cannot exclude individual ids from a list/online audience — the
    # `excludedUsers` body field doesn't exist (verified live 2026-06-12; it was
    # silently dropped and excluded fans got the blast). Mirror the computed
    # excludes into the per-account Auto_Exclude OF list, which OF honors via
    # `excludedLists`. The whole sync+mint+send runs under a per-account lock so
    # two concurrent broadcasts can't rewrite that list under each other. Build
    # the client first — the sync needs it.
    from audiences import broadcast_lock, ensure_exclude_list
    import audience_include as _audiences
    client = await asyncio.to_thread(ax._make_client, account_id)
    is_list_audience = bool(user_lists or online_only or filters)

    # ── Include-only audience (audience_mode) ────────────────────────────
    # Subtraction, never replacement: a list/online broadcast keeps its ORIGINAL
    # audience and gets the AUTOFENCE exclude list attached below; an explicit
    # recipient list is INTERSECTED with the include mirror (never widened).
    # Shadow mode logs both with denominators and changes nothing. Covers every
    # delegating sender too (mass_premade, arc_tease, vault_daily_reminder,
    # ppv_send) — they all fire through this run.
    audience_stats: dict = {}
    audience_pol = await _audiences.automation_audience(account_id)
    if audience_pol.mode != "off" and recipients:
        recipients = await _audiences.filter_candidates(
            account_id, recipients, kind="send_mass_message",
            policy=audience_pol, stats=audience_stats)
        if not recipients and not is_list_audience:
            halted = audience_stats.get("audience_halted")
            log.warning("send_mass_message: audience fence left no explicit "
                        "recipients account=%s (%s)", account_id,
                        halted or "audience_empty")
            # A HALT (stale mirror / kill switch) is an error — loud, like the
            # fence halt below; a genuinely-empty intersection is a plain skip.
            if halted:
                return {"status": "error", "reason": f"audience_halt:{halted}",
                        **audience_stats}
            return {"status": "skipped", "reason": "audience_empty",
                    **audience_stats}

    async with broadcast_lock(account_id):
        if audience_pol.mode != "off" and is_list_audience:
            try:
                fence_ids = await _audiences.audience_fence_for_broadcast(
                    account_id, client=client, policy=audience_pol,
                    stats=audience_stats)
            except _audiences.AudienceHalt as e:
                # HALT loudly — an enforce-mode blast never sends unfenced.
                log.error("send_mass_message: AUTOFENCE unhealthy account=%s "
                          "(%s) — broadcast halted", account_id, e.reason)
                return {"status": "error", "reason": f"audience_halt:{e.reason}",
                        **audience_stats}
            for _lid in fence_ids:
                if _lid not in excluded_user_lists:
                    excluded_user_lists = [*excluded_user_lists, _lid]
        if excl_set and is_list_audience:
            try:
                auto_lid = await ensure_exclude_list(
                    account_id, excl_set, client=client)
            except Exception:
                # Fail CLOSED — a broadcast without its guard is the over-send
                # bug itself. Nothing minted yet, so no dangling 'running' row.
                log.warning("send_mass_message: Auto_Exclude sync failed — "
                            "skipping broadcast account=%s", account_id,
                            exc_info=True)
                return {"status": "error", "reason": "exclude_list_sync_failed"}
            if auto_lid is not None and auto_lid not in excluded_user_lists:
                excluded_user_lists = [*excluded_user_lists, auto_lid]

        # ── 2) Mint the mass_runs row up front (the broadcast anchor) ────
        audience_filter = json.dumps({
            "user_lists": list(user_lists),
            "included_users": recipients,
            "excluded_users": sorted(excl_set),
            "list_ids": list_ids,
            "ref": str(attr_ref) if attr_ref else None,
        })
        async with get_session() as s:
            mr = MassRun(
                account_id=str(account_id),
                funnel_id=funnel.id if funnel else None,
                started_by_employee_id=employee_id,
                automation_kind=attr_kind,
                audience_filter=audience_filter,
                recipient_count=len(recipients),
                status="running",
            )
            s.add(mr)
            await s.flush()
            mass_run_id = int(mr.id)

        # ── 3) Fire the OF broadcast (off-thread so the loop never blocks) ─
        # Proactively SPACE this broadcast ≥ the gap after the account's
        # previous OF write (resends/drips/parallel sends never crowd OF's 10s
        # window), with the async retry as a backstop. The wait is
        # asyncio.sleep — loop/threads stay free.
        result = await ax.of_write_paced(
            account_id,
            lambda: client.send_mass_message(
                text=text,
                user_lists=list(user_lists),
                included_users=recipients,
                excluded_users=sorted(excl_set),
                excluded_user_lists=list(excluded_user_lists),
                price=price,
                media_files=media_files,
                previews=previews,
                filters=filters,
                online_only=online_only,
            ),
            send_purpose="fenced",
        )
    # OF's queue id — needed by callers that schedule a forever-window unsend
    # (mass_premade, the relay auto-unsend timer). NOT persisted on mass_runs.
    queue_id = result.get("id") if isinstance(result, dict) else None

    # ── 4) Persist per-recipient rows (mirror server._close_mass_run) ────
    created_at = (
        (_parse_iso(result.get("createdAt")) if isinstance(result, dict) else None)
        or datetime.utcnow()
    )
    price_cents = _to_cents(price)

    # 4a) Real rows for any per-fan ids OF echoed back.
    echoed_fans: set[int] = set()
    msgs = result.get("messages") if isinstance(result, dict) else None
    if isinstance(msgs, list):
        for m in msgs:
            if not isinstance(m, dict):
                continue
            fid_raw = m.get("userId") or m.get("toUserId") or m.get("recipientId")
            mid_raw = m.get("id") or m.get("messageId")
            try:
                fan_id = int(fid_raw)
                msg_id = int(mid_raw)
            except (TypeError, ValueError):
                continue
            echoed_fans.add(fan_id)
            await write_outbound_attribution(
                account_id=account_id,
                fan_id=fan_id,
                message_id=msg_id,
                sent_by_employee_id=employee_id,
                body=text,
                price_cents=price_cents,
                created_at=created_at,
                mass_run_id=mass_run_id,
                automation_kind=attr_kind,
                emit_live=True,  # WORKER→SSE bridge
            )
            await reconcile_mass_placeholder(
                account_id=account_id, fan_id=fan_id, mass_run_id=mass_run_id,
            )

    # 4b) Optimistic placeholders for the known recipients OF did NOT echo.
    pending = [fid for fid in recipients if fid not in echoed_fans]
    optimistic = 0
    if pending:
        optimistic = await write_mass_optimistic_rows(
            account_id=account_id,
            fan_ids=pending,
            mass_run_id=mass_run_id,
            sent_by_employee_id=employee_id,
            body=text,
            price_cents=price_cents,
            created_at=created_at,
            automation_kind=attr_kind,
            emit_live=True,  # WORKER→SSE bridge
        )

    # ── 5) Close the mass_runs row (stamp OF's queue id so the Mass Messages
    #        tab can join the cache → this run for automation attribution) ──
    async with get_session() as s:
        mr = await s.get(MassRun, mass_run_id)
        if mr is not None:
            mr.status = "ok"
            mr.completed_at = datetime.utcnow()
            if queue_id is not None:
                mr.queue_id = int(queue_id)

    # NOTE: a pure list/online broadcast leaves NO per-fan rows here (OF echoes
    # no ids), so its silent (non-replying) recipients aren't individually
    # recorded — the next blast re-touches them at its CADENCE (the intended
    # mass-blast behaviour). Fans with recent ACTIVITY are still guarded: their
    # inbound replies + our 1:1/auto-reply outbound write `messages` rows that
    # the Auto_Exclude guard reads. The local `fans` table is interaction-
    # derived (not an OF-list mirror) so it can't resolve a list audience to
    # stamp; online_blast, which has a real online snapshot, stamps NudgeState
    # itself. Off-platform exact recipients arrive via the scrape reconciler.
    log.info(
        "send_mass_message account=%s run=%s mass_run=%s recipients=%d echoed=%d optimistic=%d",
        account_id, run_id, mass_run_id, len(recipients), len(echoed_fans), optimistic,
    )
    return {
        "mass_run_id": mass_run_id,
        "queue_id": int(queue_id) if queue_id is not None else None,
        "funnel_id": funnel_id_resolved,
        "recipients": len(recipients),
        "user_lists": len(user_lists),
        "echoed_rows": len(echoed_fans),
        "optimistic_rows": optimistic,
        # R1/R2: fans dropped because they already answered this funnel.
        "skipped_already_answered": len(skipped_answered),
        "skipped_already_answered_ids": skipped_answered,
        **audience_stats,
    }
