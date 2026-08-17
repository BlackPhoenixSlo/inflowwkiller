"""
service/automations/scheduled_send.py — one-shot deferred human send.

NOT a recurring automation. This is the server-side replacement for the old
in-browser `setTimeout` local-wait path (≤15 min scheduled DMs): a chatter
schedules a message, the relay enqueues ONE `scheduled_jobs` row with
`run_at = fireAt` + the scheduling chatter's `sent_by_employee_id` in the
payload, and the executor fires it at that time. Because it lives in the DB:

  • it survives the chatter's tab reload / close (the timer no longer lives in
    one browser),
  • every chatter on the account sees it (the list endpoint reads the same
    rows) and any of them can cancel it before it fires,
  • the delivered bubble is attributed to the chatter who SCHEDULED it
    (`sent_by_employee_id` from the payload), not to "Automation" — so the
    "Sent by X" label is correct no matter who is viewing.

We fire it ourselves (not OF's native /messages/queue) precisely so the
near-term timing OF's queue is unreliable about (the original "OF lags or
rejects too-near times" note) is a non-issue: the supervisor's 30 s tick is the
only latency, and we control it.

Mirrors the scrape_chats reference + send_followup: of_client ONLY (no DOM),
built via the executor's `_make_client` seam (tests inject a fake), OF write
spaced through `of_write_paced`, and the outbound row persisted via
`attribution.write_outbound_attribution` (the WS pump skips outbound, so this is
the only producer of the row). Unlike the automations it does NOT acquire a
fan-lease — a human-intended message always sends — but it DOES set the same
post-send cooldown a manual 1:1 send does, so it yields the automations after.

Payload (set by POST /api/of/v2/scheduled-sends): `fan_id` (required), `text`,
`price`, `locked_text`, `media_files` (vault ids or fresh-upload claim dicts),
`previews`, `tagged_users`, `giphy_id`, `sent_by_employee_id`.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime

import automation_executor as ax  # _make_client / of_write_paced / _parse_iso / cooldown seams
from attribution import write_outbound_attribution
from automation_registry import register

log = logging.getLogger("of-relay.automation.scheduled_send")

_KIND = "scheduled_send"


@register(_KIND)
async def run(account_id: str, payload: dict, *, run_id: int) -> dict:
    fan_id = payload.get("fan_id")
    text = str(payload.get("text") or "")
    media_files = payload.get("media_files") or []
    giphy_id = payload.get("giphy_id")

    # Nothing to deliver → no-op (the job still completes, never re-fires).
    if not fan_id or (not text.strip() and not media_files and not giphy_id):
        log.info("scheduled_send empty account=%s fan=%s — skip", account_id, fan_id)
        return {"status": "skipped", "reason": "empty", "fan_id": fan_id}

    price = float(payload.get("price") or 0)
    locked_text = bool(payload.get("locked_text"))
    previews = payload.get("previews") or []
    tagged_users = payload.get("tagged_users") or []
    employee_id = payload.get("sent_by_employee_id")

    # Include-only audience at-fire recheck. The creation route stamps every
    # human-scheduled send `send_purpose: "manual"`, which bypasses (a human
    # send always fires); anything automation-enqueued into this kind without
    # the stamp is re-checked against the fence at FIRE time — that is what
    # catches dead-session backlogs and mode flips between enqueue and fire.
    import audience_include as _audiences
    allowed, why = await _audiences.check_at_fire(
        account_id, fan_id, kind=_KIND, payload=payload)
    if not allowed:
        log.info("scheduled_send audience-blocked account=%s fan=%s (%s)",
                 account_id, fan_id, why)
        return {"status": "skipped", "reason": f"audience:{why}", "fan_id": fan_id}
    purpose = "manual" if str(payload.get("send_purpose") or "") == "manual" else "gated"

    client = await asyncio.to_thread(ax._make_client, account_id)
    result = await ax.of_write_paced(
        account_id,
        lambda: client.send_message(
            fan_id,
            text,
            price=price,
            locked_text=locked_text,
            media_files=media_files,
            previews=previews,
            tagged_users=tagged_users,
            giphy_id=giphy_id,
            # The flag keys on who COMPOSED, not who fired. A `manual` stamp is
            # the chat composer's ≤15-min deferral — the same human at the same
            # tag picker as the immediate send (server.py of_send_message) and
            # the >15-min OF-queue send, both of which pass False. Leaving the
            # default True here made those three disagree the moment the co-tag
            # started landing (`rfTag`): the deferred one named a co-performer
            # the operator never picked, while the other two did not. Derived
            # rather than hardcoded so a job enqueued WITHOUT that stamp — an
            # automation, via automation_rules_api — still gets the auto tag.
            auto_tag=(purpose != "manual"),
        ),
        send_purpose=purpose,
    )

    msg_id = result.get("id") if isinstance(result, dict) else None
    if msg_id:
        # _to_cents lives in event_transcoder alongside _parse_iso; import here
        # so this plugin's module import stays cheap.
        from event_transcoder import _to_cents

        await write_outbound_attribution(
            account_id=account_id,
            fan_id=int(fan_id),
            message_id=int(msg_id),
            sent_by_employee_id=employee_id,   # the chatter who scheduled it
            body=str(result.get("text") or text),
            price_cents=_to_cents(price),
            created_at=ax._parse_iso(result.get("createdAt")) or datetime.utcnow(),
            automation_kind=None,              # human send, just deferred — not an automation
            emit_live=True,                    # surface live to every open chat (no optimistic row exists)
        )
        # Match a manual 1:1 send: rest the fan so automations yield after a
        # human (deferred) touch. Best-effort — a cooldown miss never fails the send.
        try:
            await ax.start_fan_cooldown(account_id, int(fan_id))
        except Exception:
            log.warning("scheduled_send cooldown set failed account=%s fan=%s",
                        account_id, fan_id, exc_info=True)

    log.info("scheduled_send ok account=%s fan=%s msg=%s", account_id, fan_id, msg_id)
    return {"status": "ok" if msg_id else "sent_no_id", "fan_id": fan_id, "message_id": msg_id}
