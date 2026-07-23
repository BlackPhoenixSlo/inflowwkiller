"""service/automations/arc_tease.py — the arc's free-DM drop, resolved at fire time.

A booked mass_free day is a REFERENCE (`{"arc_id", "seq"}`), the same shape a
priced day uses (`{"ppv_id"}`): what the tease says lives in the account's
`active_arc` registry and who it reaches is `vault_arc.TEASE_AUDIENCE`, both
read when the job fires — never frozen into the job row at approval. (The
frozen shape is how an audience bug once shipped inside every booked job and
needed a data heal; a reference makes that class of drift impossible.)

Resolution then delegates to `send_mass_message.run` exactly like
`mass_premade` does — audience resolution, contact guards, pacing and
attribution all stay in the one sender.

Self-registers via `@register("arc_tease")` on import — no edit to any shared
file.
"""
from __future__ import annotations

import logging

from automation_registry import register
# Module-level so tests can stub the delegation seam.
from automations.send_mass_message import run as send_mass_run

log = logging.getLogger("of-relay.automation.arc_tease")


@register("arc_tease")
async def run(account_id: str, payload: dict, *, run_id: int) -> dict:
    """Resolve this drop's tease line from the active arc and broadcast it.
    A drop whose arc is gone (cancelled or replaced) skips — an arc that was
    stood down must stay down, even if its job row survived."""
    from vault_arc import TEASE_AUDIENCE, active_arc

    payload = payload or {}
    try:
        seq = int(payload.get("seq"))
    except (TypeError, ValueError):
        log.warning("arc_tease: unusable ref %r — skipping", payload)
        return {"status": "skipped", "reason": "bad_ref"}
    arc_id = str(payload.get("arc_id") or "")

    arc = await active_arc(account_id)
    if not arc or str(arc.get("arc_id") or "") != arc_id:
        log.info("arc_tease: arc %s no longer armed — skipping", arc_id)
        return {"status": "skipped", "reason": "arc_gone"}

    drop = next((d for d in (arc.get("drops") or [])
                 if d.get("seq") == seq and d.get("channel") == "mass_free"), None)
    text = str((drop or {}).get("text") or "").strip()
    if not text:
        log.warning("arc_tease: no text for arc=%s seq=%s — skipping", arc_id, seq)
        return {"status": "skipped", "reason": "no_text"}

    # OF's send body must be html-wrapped to match what the web client posts;
    # a bare string renders without the paragraph break.
    return await send_mass_run(account_id, {
        "text": f"<p>{text}</p>",
        "price": 0,
        "automation_kind": "arc_tease",
        **TEASE_AUDIENCE,
    }, run_id=run_id)
