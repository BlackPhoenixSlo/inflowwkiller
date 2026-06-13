"""W7 webhook-priority dispatch — turn an inbound WS event into real-time
automation dispatch instead of waiting for the next 30s supervisor tick.

`event_transcoder._transcode_chat_message` calls `on_inbound_message` (fire-and-
forget) right after it persists an INBOUND DM. We:

  1. gate on a per-account flag (default OFF) + a global env kill-switch,
  2. pick the owning automation (a fan mid-funnel → reply_mass_funnel, else the
     generic of_ai_chat sweep),
  3. if the fan is on a per-fan cooldown (a human is handling them, or the bot
     just replied — W3's shared guard), DEFER the reply to the moment that
     cooldown ends rather than dropping it to the slow periodic sweep,
  4. enqueue a job (deduped) and WAKE the supervisor at the due time.

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

from sqlalchemy import select, update

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

# When a fan replies DURING their per-fan cooldown, we defer the reply to the
# moment the cooldown ends instead of dropping it to the slow periodic sweep.
# But a cooldown further out than this (the 30-min welcome/followup rest) is too
# long to park a chat reply on — let the periodic sweep catch that rare case.
_MAX_DEFER_S = 300


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


# Hold strong refs to in-flight delayed-wake tasks. asyncio.create_task() only
# keeps a WEAK reference, so a fire-and-forget task can be garbage-collected
# mid-sleep — which silently drops the wake and leaves the delayed job sitting
# until an unrelated drain (observed live: a +9s reply landed ~3 min late).
_wake_tasks: set = set()


def _schedule_wake(delay_s: float) -> None:
    """Spawn a delayed-wake task and KEEP a reference so it can't be GC'd."""
    t = asyncio.create_task(_wake_after(delay_s))
    _wake_tasks.add(t)
    t.add_done_callback(_wake_tasks.discard)


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

    # PPVscriptAI: when ai_chatter is enabled it owns every fan under its spend
    # gate (it replaces of_ai_chat/deep_convo for them — one bot voice per fan);
    # a fan at/over the gate is a whale → human territory, no automation reply.
    # Lazy import to avoid a module cycle (ai_chatter imports of_ai_chat).
    try:
        from automations.ai_chatter import gate_for as _ai_gate_for
        ai_gate = await _ai_gate_for(account_id)
    except Exception:
        ai_gate = None
    if ai_gate is not None:
        async with get_session() as s:
            spend = (
                await s.execute(
                    select(Fan.lifetime_spend_cents).where(
                        Fan.account_id == str(account_id),
                        Fan.fan_id == int(fan_id),
                    )
                )
            ).scalar_one_or_none()
        return "ai_chatter" if int(spend or 0) < ai_gate else None

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


async def _fan_paused_until(account_id: str, fan_id: int) -> datetime | None:
    """The fan's automation_paused_until (their per-fan cooldown end), or None.
    Read fresh — a sender that paused this fan earlier in the same tick must be
    visible so we defer to the real end, not a stale snapshot."""
    async with get_session() as s:
        until = (
            await s.execute(
                select(Fan.automation_paused_until).where(
                    Fan.account_id == str(account_id),
                    Fan.fan_id == int(fan_id),
                )
            )
        ).scalar_one_or_none()
    return until


async def _clear_deep_convo_backoff(account_id: str, fan_id: int) -> None:
    """Zero a fan's deep_convo backoff — they just messaged, so deep_convo must
    answer now, not skip them because the sweep escalated their backoff while they
    were (briefly) quiet between the question and this reply."""
    async with get_session() as s:
        await s.execute(
            update(Fan)
            .where(Fan.account_id == str(account_id), Fan.fan_id == int(fan_id))
            .values(deep_convo_skip_level=0, deep_convo_skip_remaining=0)
        )


async def _has_imminent_pending_job(
    account_id: str, kind: str, fan_id: int, within_s: float
) -> bool:
    """Per-FAN dedup: a pending (account, kind) job already targeting THIS fan and
    due within `within_s` seconds means a reaction is already queued — skip a
    second enqueue. Per-fan (not per-kind) because W7 is fan-scoped now: two
    different fans replying must each get their own job. The window spans our
    response delay so two quick replies from the SAME fan don't double-queue."""
    soon = datetime.utcnow() + timedelta(seconds=within_s)
    async with get_session() as s:
        rows = (
            await s.execute(
                select(ScheduledJob.payload_json).where(
                    ScheduledJob.account_id == str(account_id),
                    ScheduledJob.kind == kind,
                    ScheduledJob.status == "pending",
                    ScheduledJob.run_at <= soon,
                )
            )
        ).scalars().all()
    fid = int(fan_id)
    for pj in rows:
        try:
            p = json.loads(pj or "{}")
        except Exception:
            continue
        if fid in (p.get("only_fan_ids") or []) or p.get("test_fan") == fid:
            return True
    return False


async def on_inbound_message(account_id: str, fan_id: int, message_id: int) -> None:
    """React to one inbound fan DM: enqueue + wake the owning sweep so the bot
    replies in seconds instead of at the next tick. Never raises."""
    try:
        if _global_kill_switch():
            return
        cfg = await _load_config(account_id)
        if cfg is None:
            return
        kind = await _classify_kind(account_id, fan_id)
        if kind is None:
            # Terminal stage (deep_convo done, or spent/too_long): no automation
            # should reply — a human owns this fan. Don't wake a sweep for nothing.
            log.debug("w7_skip_terminal account=%s fan=%s", account_id, fan_id)
            return

        delay = _response_delay(cfg)
        # ai_chatter "backup" mode: the bot is the FALLBACK chatter — its reply
        # waits out the human SLA. We enqueue now with the SLA folded into the
        # delay; at fire time the run's "fan spoke last" gate re-checks, so a
        # human answering inside the window simply makes the job a no-op.
        if kind == "ai_chatter":
            try:
                from automations.ai_chatter import _load_config as _ai_cfg
                acfg = await _ai_cfg(account_id)
                if str(acfg.get("mode") or "backup") == "backup":
                    delay += max(0, int(acfg.get("sla_minutes") or 0)) * 60
            except Exception:
                log.debug("ai_chatter sla read failed account=%s", account_id,
                          exc_info=True)
        now = datetime.utcnow()

        # A fan a human is handling, or one the bot just replied to, is on a
        # per-fan cooldown (W3's shared cross-tick guard, 45-90s for chat). Rather
        # than DROP this reply to the slow periodic sweep — fans usually reply
        # within ~30s, INSIDE the cooldown, so they'd always wait out the next
        # sweep — we DEFER the reply to the moment the cooldown ends. of_ai_chat /
        # deep_convo re-check the cooldown at run time, so if a human keeps the
        # fan paused the deferred job simply skips: deferring is safe either way.
        # A cooldown beyond _MAX_DEFER_S (the 30-min welcome/followup rest) is too
        # long to park a chat reply — let the periodic sweep catch that rare case.
        paused_until = await _fan_paused_until(account_id, fan_id)
        deferred = paused_until is not None and paused_until > now
        if deferred:
            if (paused_until - now).total_seconds() > _MAX_DEFER_S:
                log.debug("w7_skip_cooldown_far account=%s fan=%s until=%s",
                          account_id, fan_id, paused_until)
                return
            run_at = paused_until + timedelta(seconds=delay)
        else:
            run_at = now + timedelta(seconds=delay)
        wait_s = max(0.0, (run_at - now).total_seconds())

        # Dedup over the FULL wait (cooldown remainder + delay) so a second
        # message during the rest doesn't queue its own deferred job.
        if await _has_imminent_pending_job(account_id, kind, fan_id, wait_s + _DEDUP_WINDOW_S):
            log.debug("w7_skip_dedup account=%s kind=%s fan=%s", account_id, kind, fan_id)
            ax.wake_supervisor()  # still wake so the already-pending job drains
            return

        # The fan just MESSAGED — they're engaged. deep_convo's periodic sweep
        # escalates a mid-drill fan's backoff every tick they haven't replied yet,
        # and that backoff then SUPPRESSES this webhook-triggered reply (the fan
        # waits out the slow sweep instead of getting an instant answer). A fresh
        # inbound means "act now", so clear the deep_convo backoff before dispatch.
        if kind == "deep_convo":
            await _clear_deep_convo_backoff(account_id, fan_id)

        # Fan-scope the run so it reacts to ONLY this fan (no full-account sweep):
        # reply_mass_funnel takes `test_fan`; of_ai_chat / deep_convo take
        # `only_fan_ids` (gates still apply — see each automation's run()).
        payload = (
            {"test_fan": int(fan_id)} if kind == "reply_mass_funnel"
            else {"only_fan_ids": [int(fan_id)]}
        )
        await ax.enqueue_job(account_id, kind, payload=payload, run_at=run_at)
        # Wake now if due immediately; otherwise wake once the (possibly deferred)
        # job is due so it doesn't wait for the next fallback tick.
        if wait_s > 0:
            _schedule_wake(wait_s)  # keeps a ref so the wake can't be GC'd
        else:
            ax.wake_supervisor()
        log.info(
            "w7_dispatch account=%s fan=%s kind=%s msg=%s wait=%.1fs%s",
            account_id, fan_id, kind, message_id, wait_s,
            " (deferred to cooldown end)" if deferred else "",
        )
    except Exception:
        log.warning(
            "w7_dispatch_failed account=%s fan=%s", account_id, fan_id, exc_info=True
        )


async def on_inbound_tip(account_id: str, fan_id: int, message_id: int,
                         tip_cents: int) -> None:
    """A fan just tipped → if the tip_reward automation is enabled for this
    account, enqueue ONE fan-scoped tip_reward job and wake the executor. Gated
    SEPARATELY from the W7 reply dispatch above: a tip reward should fire even on
    a terminal-stage fan that no chat automation would reply to. Never raises."""
    try:
        # PPVscriptAI: a fan with an OPEN tip-capable ai_chatter offer is paying
        # toward THAT — the offer claims the tip (the run's unlock watcher
        # accumulates + delivers) and the generic tip_reward stands down, so the
        # fan never gets reward media on top of the unlocked piece.
        try:
            from automations.ai_chatter import has_open_tip_offer
            if await has_open_tip_offer(account_id, fan_id):
                await ax.enqueue_job(account_id, "ai_chatter",
                                     payload={"only_fan_ids": [int(fan_id)]})
                ax.wake_supervisor()
                log.info("ai_chatter_tip_claim account=%s fan=%s msg=%s cents=%s",
                         account_id, fan_id, message_id, tip_cents)
                return
        except Exception:
            log.warning("ai_chatter_tip_claim_failed account=%s fan=%s — falling "
                        "back to tip_reward", account_id, fan_id, exc_info=True)

        from automations.tip_reward import is_enabled  # lazy: avoid import cycle
        if not await is_enabled(account_id):
            return
        await ax.enqueue_job(
            account_id, "tip_reward",
            payload={"fan_id": int(fan_id), "tip_message_id": int(message_id),
                     "tip_cents": int(tip_cents)},
        )
        ax.wake_supervisor()
        log.info("tip_reward_dispatch account=%s fan=%s msg=%s cents=%s",
                 account_id, fan_id, message_id, tip_cents)
    except Exception:
        log.warning("tip_reward_dispatch_failed account=%s fan=%s",
                    account_id, fan_id, exc_info=True)
