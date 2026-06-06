"""service/automations/mass_premade.py — P3 `mass_premade` (timed broadcasts).

The "ready-made mass messages, 2 ways" idea from library/AUTOMATIONS_SIMPLE.txt
§"NEXT IDEAS" (P3). A thin ORCHESTRATOR over the existing send + unsend
automations — it owns only the TIMERS, never the wire calls:

  Way 1 (premade → send → resend later → delete):
      one message with `resend_count` ≥ 1 and `resend_after_hours`. Each send
      also re-enqueues `mass_premade` for the next resend (count-1), and each
      send schedules its own unsend at `unsend_after_hours`.
  Way 2 (a list of messages, send + unsend on a timer):
      a multi-item `messages` list (resend_count = 0). Posts the first, drips
      the rest forward at each item's `delay_minutes`, each with its own unsend.

Per run it: (1) delegates the SEND+persist to `send_mass_message.run` (which
mints the mass_runs row, writes optimistic rows, and now returns the OF
`queue_id`), (2) if `unsend_after_hours` is set, enqueues a one-shot
`unsend_messages` job at now+ttl carrying `{queue_id, mass_run_id}` — the SAME
forever-window unsend the relay's Mass Online auto-unsend uses, (3) re-enqueues
itself for the resend and/or the rest of the list.

The whole queue rides in the job payload — one run does one send and hands the
rest forward, so there's no new table. Self-registers via
`@register("mass_premade")`. Spend/lease are handled inside send_mass_message;
this layer only schedules.

Payload shape::

    {
      "messages": [
        {"text": "<p>..</p>", "media_files": [123], "previews": [123],
         "price": 0, "online_only": true, "filters": {"online": 1},
         "included_users": [..], "excluded_users": [..],
         "user_lists": ["fans"], "list_ids": [7],
         "unsend_after_hours": 8,                 # delete this broadcast later
         "resend_after_hours": 24, "resend_count": 1,   # Way 1: send→resend→delete
         "delay_minutes": 0},                     # Way 2: stagger after previous
        {"text": "...", "unsend_after_hours": 6}
      ],
      "dry_run": false                            # plan only — no send, no enqueue
    }
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

import automation_executor as ax  # enqueue_job + _make_client
from automation_registry import register
from automations._pools import pick_media, pick_text

log = logging.getLogger("of-relay.automation.mass_premade")

# Keys forwarded verbatim to send_mass_message; the timer keys
# (unsend_after_hours/resend_*/delay_minutes) stay here in the orchestrator.
_SEND_KEYS = (
    "text", "media_files", "previews", "price", "online_only", "filters",
    "included_users", "excluded_users", "user_lists", "excluded_user_lists",
    "list_ids", "exclude_list_ids", "funnel_id", "funnel_name", "funnel",
    # Mass Online audience parity — resolved inside send_mass_message via
    # audiences.resolve_mass_audience (recent-chat / unread include, replied /
    # inbound exclude). The TIMER keys stay in this orchestrator, not here.
    "recent_chat_hours", "recent_chat_limit",
    "exclude_replied_hours", "exclude_inbound_hours", "unread_limit",
)


def _pos_float(raw: object) -> float | None:
    if isinstance(raw, (int, float)) and raw > 0:
        return float(raw)
    return None


@register("mass_premade")
async def run(account_id: str, payload: dict, *, run_id: int) -> dict:
    """Send the first message of the queue, schedule its unsend, and re-enqueue
    for the resend / the rest of the list. Returns a stats dict."""
    payload = payload or {}
    messages = payload.get("messages") or []
    if not isinstance(messages, list) or not messages:
        return {"status": "skipped", "reason": "no_messages"}

    # ── dry_run: preview the whole plan, touch nothing ──────────────────
    if payload.get("dry_run"):
        plan = []
        for m in messages:
            if not isinstance(m, dict):
                continue
            plan.append({
                "text": (pick_text(m))[:80],
                "text_variants": len(m.get("texts")) if isinstance(m.get("texts"), list) else 1,
                "media_pool": len(m.get("media_files") or []),
                "media_folder_id": m.get("media_folder_id"),
                "media_count": m.get("media_count") or 1,
                "online_only": bool(m.get("online_only")),
                "user_lists": list(m.get("user_lists") or []),
                "excluded_user_lists": list(m.get("excluded_user_lists") or []),
                "exclude_replied_hours": _pos_float(m.get("exclude_replied_hours")),
                "exclude_inbound_hours": _pos_float(m.get("exclude_inbound_hours")),
                "recent_chat_hours": _pos_float(m.get("recent_chat_hours")),
                "unread_limit": int(m.get("unread_limit") or 0) or None,
                "unsend_after_hours": _pos_float(m.get("unsend_after_hours")),
                "resend_after_hours": _pos_float(m.get("resend_after_hours")),
                "resend_count": int(m.get("resend_count") or 0),
                "delay_minutes": _pos_float(m.get("delay_minutes")) or 0,
            })
        return {"dry_run": True, "messages": len(messages), "plan": plan, "sent": 0}

    spec = messages[0] if isinstance(messages[0], dict) else {}
    rest = messages[1:]

    # ── 1) Randomise text + media for THIS fire, then delegate the SEND ─
    # Pick one text from the variant pool and sample media_count images from the
    # image pool (pre-picked ids and/or a vault folder). A client is only needed
    # to expand a folder. See automations/_pools.py.
    text = pick_text(spec)
    client = (
        await asyncio.to_thread(ax._make_client, account_id)
        if spec.get("media_folder_id") is not None else None
    )
    media = await asyncio.to_thread(pick_media, spec, client)

    from automations.send_mass_message import run as send_mass_run
    send_payload = {k: spec[k] for k in _SEND_KEYS if k in spec}
    send_payload["text"] = text          # resolved text wins over any pool/raw
    send_payload["media_files"] = media  # resolved sample wins over the pool
    res = await send_mass_run(account_id, send_payload, run_id=run_id)

    if res.get("status") == "skipped":
        # Nothing went out (no text / empty audience) — don't strand the queue.
        log.warning("mass_premade: send skipped (%s) — draining rest", res.get("reason"))
        remaining = await _drip_rest(account_id, rest)
        return {"status": "skipped", "reason": res.get("reason"), "remaining": remaining}

    queue_id = res.get("queue_id")
    mass_run_id = res.get("mass_run_id")
    now = datetime.utcnow()

    # ── 2) Schedule the unsend (forever-window mass delete) ─────────────
    unsend_job_id: int | None = None
    unsend_at_iso: str | None = None
    unsend_after = _pos_float(spec.get("unsend_after_hours"))
    if unsend_after and queue_id is not None:
        unsend_at = now + timedelta(hours=unsend_after)
        unsend_job_id = await ax.enqueue_job(
            account_id, "unsend_messages",
            payload={"targets": [{"queue_id": int(queue_id), "mass_run_id": mass_run_id}]},
            run_at=unsend_at,
        )
        unsend_at_iso = unsend_at.isoformat() + "Z"
    elif unsend_after and queue_id is None:
        log.warning("mass_premade: unsend_after set but OF returned no queue id — no auto-unsend")

    # ── 3a) Way 1 resend: re-enqueue this message with count-1 ──────────
    resend_job_id: int | None = None
    resend_at_iso: str | None = None
    resend_count = int(spec.get("resend_count") or 0)
    resend_after = _pos_float(spec.get("resend_after_hours"))
    if resend_count > 0 and resend_after:
        resend_spec = {**spec, "resend_count": resend_count - 1}
        resend_at = now + timedelta(hours=resend_after)
        resend_job_id = await ax.enqueue_job(
            account_id, "mass_premade",
            payload={"messages": [resend_spec]},
            run_at=resend_at,
        )
        resend_at_iso = resend_at.isoformat() + "Z"

    # ── 3b) Way 2 drip: hand the rest of the list forward ───────────────
    remaining = await _drip_rest(account_id, rest)

    return {
        "status": "ok",
        "sent": 1,
        "queue_id": queue_id,
        "mass_run_id": mass_run_id,
        "recipients": res.get("recipients"),
        "unsend_job_id": unsend_job_id,
        "unsend_at": unsend_at_iso,
        "resend_job_id": resend_job_id,
        "resend_at": resend_at_iso,
        "resends_left": max(resend_count - 1, 0) if resend_count > 0 else 0,
        "remaining": remaining,
    }


async def _drip_rest(account_id: str, rest: list) -> int:
    """Re-enqueue `mass_premade` with the remaining list at now+delay_minutes (the
    next item's stagger). Returns the count handed forward (0 = list done)."""
    if not rest:
        return 0
    head = rest[0] if isinstance(rest[0], dict) else {}
    delay_min = _pos_float(head.get("delay_minutes")) or 0
    run_at = datetime.utcnow() + timedelta(minutes=delay_min)
    await ax.enqueue_job(
        account_id, "mass_premade",
        payload={"messages": rest},
        run_at=run_at,
    )
    return len(rest)
