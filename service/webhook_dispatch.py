"""W7 webhook-priority dispatch — turn an inbound WS event into real-time
automation dispatch instead of waiting for the next 30s supervisor tick.

`event_transcoder._transcode_chat_message` calls `on_inbound_message` (fire-and-
forget) right after it persists an INBOUND DM. We:

  1. gate on a per-account flag (default OFF) + a global env kill-switch,
  2. stand down if the fan is on cooldown (a human chatter is handling them, or
     the bot just messaged them — W3's per-fan cooldown is the shared guard),
  3. pick the owning automation (a fan mid-funnel → reply_mass_funnel, else the
     generic of_ai_chat sweep),
  4. enqueue a job (deduped) and WAKE the supervisor so it drains now.

We do NOT send here and we do NOT rewrite the senders: we enqueue + wake the
EXISTING sweep, which finds the just-replied fan via its normal "fan spoke last"
gate (of_ai_chat.py: `c.last_dir == "in"`). W3's fan-lease + cooldown already
prevent double-sends, so kicking the sweep early is safe.

Design notes:
  • Whole body is wrapped so a dispatch failure can never abort the persist that
    called us — the transcoder is fire-and-forget but we belt-and-suspenders it.
  • The per-account gate lives in account_ai_config.webhook_config_json
    ({"enabled": bool}); absent/NULL → OFF. The global kill-switch is the env
    var W7_WEBHOOK_DISPATCH_DISABLED (set to 1/true to disable everywhere).
  • Memory: the live detector must NEVER run on jaka (its inbound is stranger/
    promo spam). The default-OFF gate enforces that — enable Lexi first.
"""
from __future__ import annotations

import logging
import asyncio
import json
import os
import random
from datetime import datetime, timedelta

from sqlalchemy import select

from db.engine import get_session
from db.models import AccountAiConfig, Fan, FunnelState, MassRun, ScheduledJob, SkipList
import automation_executor as ax

# deep_convo's terminal state — once a fan reaches it, no automation replies
# (a human owns the chat). Lazy value so a constant rename in deep_convo can't
# silently drift; falls back to the literal if the import ever fails.
def _deep_convo_done_state() -> str:
    try:
        from automations.deep_convo import _S_DONE
        return _S_DONE
    except Exception:
        return "done"

log = logging.getLogger("of-relay.webhook_dispatch")

# Window for the dedup check: skip enqueue if a pending job for (account, kind)
# is already due within this many seconds (the imminent sweep covers this fan).
_DEDUP_WINDOW_S = 5


def _global_kill_switch() -> bool:
    """True when W7 dispatch is disabled process-wide via env."""
    return os.environ.get("W7_WEBHOOK_DISPATCH_DISABLED", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


async def _load_config(account_id: str) -> dict | None:
    """Per-account gate + knobs: account_ai_config.webhook_config_json.

    Shape: {"enabled": bool, "delay_seconds": number, "jitter_seconds": number}.
    Returns the parsed dict when ENABLED, else None (absent/NULL/disabled/bad
    JSON → OFF, the safe default)."""
    async with get_session() as s:
        cfg = await s.get(AccountAiConfig, str(account_id))
    if cfg is None or not cfg.webhook_config_json:
        return None
    try:
        data = json.loads(cfg.webhook_config_json) or {}
    except Exception:
        log.warning("bad_webhook_config_json account=%s", account_id, exc_info=True)
        return None
    if not (isinstance(data, dict) and data.get("enabled")):
        return None
    return data


def _response_delay(cfg: dict) -> float:
    """How long the AI waits before its reply lands: a fixed `delay_seconds`
    base plus a uniform-random 0..`jitter_seconds` wiggle. Makes the bot feel
    human AND gives a live human chatter a head start (if the human sends during
    the wait, the cooldown is set and the bot stands down when its job runs).
    Both default to 0 → instant, current behavior. Negatives are clamped."""
    base = max(0.0, float(cfg.get("delay_seconds") or 0))
    jitter = max(0.0, float(cfg.get("jitter_seconds") or 0))
    return base + (random.uniform(0.0, jitter) if jitter > 0 else 0.0)


async def _wake_after(delay_s: float) -> None:
    """Wake the supervisor once a delayed job becomes due. Without this the
    delayed job would sit until the next 30s fallback tick. Never raises."""
    try:
        if delay_s > 0:
            await asyncio.sleep(delay_s)
        ax.wake_supervisor()
    except Exception:  # pragma: no cover — defensive
        pass


async def _fan_mid_funnel(account_id: str, fan_id: int) -> bool:
    """True iff this fan has a pending funnel_state row under one of this
    account's mass runs — i.e. reply_mass_funnel owns the fan through a
    multi-step flow and of_ai_chat must not interleave it (Wave-3 overlap)."""
    async with get_session() as s:
        row = (
            await s.execute(
                select(FunnelState.fan_id)
                .join(MassRun, MassRun.id == FunnelState.mass_run_id)
                .where(
                    MassRun.account_id == str(account_id),
                    FunnelState.fan_id == int(fan_id),
                    FunnelState.status == "pending",
                )
                .limit(1)
            )
        ).first()
    return row is not None


async def _classify_kind(account_id: str, fan_id: int) -> str | None:
    """Pick the automation that OWNS this fan's current stage — or None when no
    automation should reply (a human owns the chat). Routing to the wrong sweep
    is pure waste: of_ai_chat skips ANY skip-listed fan, so waking it for a fan
    already handed to deep_convo does nothing AND the fan gets no reply. Stages:

      • funnel      — pending funnel_state            → reply_mass_funnel
      • deep_convo  — handed off (skip reason 'info'), not done → deep_convo
      • terminal    — deep_convo 'done', or spent/too_long/other skip → None
      • ai_chat     — still gathering (not skip-listed) → of_ai_chat

    The fan-stage markers mirror the automations' OWN gates (of_ai_chat.py
    _load_stop_lists / _handoff_to_deep_convo; deep_convo.py deep_convo_state),
    so we never wake an automation that would just skip this fan.
    """
    if await _fan_mid_funnel(account_id, fan_id):
        return "reply_mass_funnel"

    # One read for both markers: the fan's deep_convo_state + its skip_list row.
    async with get_session() as s:
        dstate = (
            await s.execute(
                select(Fan.deep_convo_state).where(
                    Fan.account_id == str(account_id),
                    Fan.fan_id == int(fan_id),
                )
            )
        ).scalar_one_or_none()
        skip = (
            await s.execute(
                select(SkipList.reason).where(
                    SkipList.account_id == str(account_id),
                    SkipList.fan_id == int(fan_id),
                )
            )
        ).first()

    if dstate == _deep_convo_done_state():
        return None  # deep_convo finished → human owns the chat, don't dispatch

    if skip is not None:  # fan is skip-listed → of_ai_chat will never act
        return "deep_convo" if (skip[0] == "info") else None  # 'info' = handed off

    return "of_ai_chat"  # not skip-listed, not done → still in the of_ai_chat stage


async def _has_imminent_pending_job(account_id: str, kind: str, within_s: float) -> bool:
    """Dedup: a pending job for (account, kind) already due within `within_s`
    seconds means the imminent (possibly delayed) sweep already covers this fan
    — skip a second enqueue. The window must span our own response delay so two
    quick replies don't queue two jobs. enqueue_job itself does not dedup."""
    soon = datetime.utcnow() + timedelta(seconds=within_s)
    async with get_session() as s:
        row = (
            await s.execute(
                select(ScheduledJob.id)
                .where(
                    ScheduledJob.account_id == str(account_id),
                    ScheduledJob.kind == kind,
                    ScheduledJob.status == "pending",
                    ScheduledJob.run_at <= soon,
                )
                .limit(1)
            )
        ).first()
    return row is not None


async def on_inbound_message(account_id: str, fan_id: int, message_id: int) -> None:
    """React to one inbound fan DM: enqueue + wake the owning sweep so the bot
    replies in seconds instead of at the next tick. Never raises."""
    try:
        if _global_kill_switch():
            return
        cfg = await _load_config(account_id)
        if cfg is None:
            return
        # Stand down for a fan a human is handling or one the bot just messaged
        # (W3 per-fan cooldown is the shared cross-tick guard).
        if await ax.fan_on_cooldown(account_id, fan_id):
            log.debug("w7_skip_cooldown account=%s fan=%s", account_id, fan_id)
            return

        kind = await _classify_kind(account_id, fan_id)
        if kind is None:
            # Terminal stage (deep_convo done, or spent/too_long): no automation
            # should reply — a human owns this fan. Don't wake a sweep for nothing.
            log.debug("w7_skip_terminal account=%s fan=%s", account_id, fan_id)
            return

        delay = _response_delay(cfg)
        # Dedup window spans the delay so a quick second reply doesn't double-queue.
        if await _has_imminent_pending_job(account_id, kind, delay + _DEDUP_WINDOW_S):
            log.debug("w7_skip_dedup account=%s kind=%s", account_id, kind)
            ax.wake_supervisor()  # still wake so the already-pending job drains
            return

        run_at = datetime.utcnow() + timedelta(seconds=delay)
        await ax.enqueue_job(account_id, kind, payload={}, run_at=run_at)
        # Wake now if instant; otherwise wake once the delayed job is due (so it
        # doesn't wait for the next 30s fallback tick).
        if delay > 0:
            asyncio.create_task(_wake_after(delay))
        else:
            ax.wake_supervisor()
        log.info(
            "w7_dispatch account=%s fan=%s kind=%s msg=%s delay=%.1fs",
            account_id, fan_id, kind, message_id, delay,
        )
    except Exception:
        log.warning(
            "w7_dispatch_failed account=%s fan=%s", account_id, fan_id, exc_info=True
        )
