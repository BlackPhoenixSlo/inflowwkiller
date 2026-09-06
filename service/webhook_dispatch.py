"""W7 webhook-priority dispatch — turn an inbound WS event into real-time
automation dispatch instead of waiting for the next 30s supervisor tick.

`event_transcoder._transcode_chat_message` calls `on_inbound_message` (fire-and-
forget) right after it persists an INBOUND DM. We:

  1. gate on a per-account flag (default OFF) + a global env kill-switch,
  2. pick the owning automation (a fan mid-funnel → reply_mass_funnel, else the
     generic welcome_chatter_for_info sweep),
  3. if the fan is on a per-fan cooldown (a human is handling them, or the bot
     just replied — W3's shared guard), DEFER the reply to the moment that
     cooldown ends rather than dropping it to the slow periodic sweep,
  4. enqueue a job (deduped) and WAKE the supervisor at the due time.

We do NOT send here and we do NOT rewrite the senders: we enqueue + wake the
EXISTING sweep, which finds the just-replied fan via its normal "fan spoke last"
gate (welcome_chatter_for_info.py: `c.last_dir == "in"`). W3's fan-lease + cooldown already
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
from db.models import (
    AccountAiConfig, Fan, FunnelState, MassRun, Message, ResolutionLog, ScheduledJob,
    SkipList,
)
import automation_executor as ax
from automation_registry import kind_family
from of_shapes import giphy_dm_id, has_video
# The widest pause `picture_back_target` can draw, and therefore the image_reply
# dedup window: it must SPAN that pause, or a replay arriving while the first job
# is still pending sees nothing queued and enqueues a second freebie.
#
# IMPORTED, not retyped. The correctness of the dedup is "this number >= the
# widest target", and while the number was written out here that condition was the
# accidental equality of two independent literals in two files: raising the
# ceiling to 120 would have made replays double-send a freebie with every suite
# green. Same remedy `TIP_LEDGER_PREFIX` already got one file over. Aliased
# because the two names are two FACTS that happen to coincide — the alias is
# where "and they must" is written down.
from automations.pacing import PICBACK_CEIL_S as _PICBACK_DEDUP_WINDOW_S

log = logging.getLogger("of-relay.webhook_dispatch")


# Free-fan describe budget, per fan per rolling 24h. A fan who has never paid is
# still worth reading — he may pay later, and the read is what makes her reply land
# — but he must not be able to empty the vision budget by dumping his camera roll.
# A clip costs the same call as a photo yet arrives in bursts, so it gets its own
# tighter lane rather than eating the photo allowance.
_FREE_FAN_IMAGE_CAP_24H = 3
_FREE_FAN_VIDEO_CAP_24H = 1


async def _describes_last_24h(account_id: str, fan_id: int) -> tuple[int, int]:
    """(images, videos) this fan has had described in the last 24h.

    Counted off `messages.image_desc IS NOT NULL` — the describe IS its own ledger,
    so there's no extra table to keep in step. Video-ness comes from PARSING the
    stored frame through the shared `of_shapes.has_video`, not from a substring of
    the JSON and not from our own description text: a quoted/replied message embeds
    the original frame, so a substring test bills a photo-reply-to-a-video-post
    against the clip lane.

    GIFs are excluded, and that is the difference between a budget and a tripwire.
    A Giphy dm is "described" from Giphy's own public title — no model call, no
    cost — yet it still writes `image_desc`, so counting stored descriptions billed
    a fan for something nobody paid for. Three gifs, a free thing anyone can send
    from the OF composer, and a never-paid fan's entire vision allowance was spent
    before he sent a single photograph. The budget exists to bound MODEL SPEND, so
    it counts only reads that could have cost something."""
    since = datetime.utcnow() - timedelta(hours=24)
    async with get_session() as s:
        rows = (await s.execute(
            select(Message.raw_json).where(
                Message.account_id == str(account_id),
                Message.fan_id == int(fan_id),
                Message.direction == "in",
                Message.image_desc.is_not(None),
                Message.created_at >= since,
            )
        )).scalars().all()
    videos = 0
    billable = 0
    for raw in rows:
        try:
            frame = json.loads(raw or "{}")
        except (TypeError, ValueError):
            billable += 1                  # truncated payload → counts as an image
            continue
        if giphy_dm_id(frame) is not None:
            continue                       # free to describe ⇒ free to receive
        billable += 1
        if has_video(frame.get("media") if isinstance(frame, dict) else None):
            videos += 1
    return (billable - videos, videos)


async def _free_fan_describe_allowed(account_id: str, fan_id: int,
                                     is_video: bool) -> bool:
    """Is another describe within this never-paid fan's daily budget?"""
    images, videos = await _describes_last_24h(account_id, fan_id)
    return (videos < _FREE_FAN_VIDEO_CAP_24H) if is_video \
        else (images < _FREE_FAN_IMAGE_CAP_24H)


async def _should_describe(account_id: str, fan_id: int, *,
                           scope: str, is_video: bool, is_gif: bool = False) -> bool:
    """Is THIS inbound media worth a vision call? The whole describe policy, in
    one place, as one question.

    Phrasing it spend-first is what collapses it: a fan who has paid is always
    read, under either scope and with no budget, so every remaining rule is about
    a fan who has never paid. That turns what was a four-deep `if/elif` ladder
    mutating a `describe_on` flag inside the dispatch function into four flat
    lines — and it asks for lifetime spend ONCE instead of once per branch.

    Order matters below: `scope == "paid"` is the operator's explicit setting and
    outranks the free-fan lanes underneath it."""
    if await _fan_lifetime_spend_cents(account_id, fan_id) > 0:
        return True
    if scope == "paid":
        return False
    # Peer creators blasting mutual promo are not prospects and never convert, so
    # reading their marketing images is pure spend. Uses the CONSERVATIVE shared
    # predicate (follows us + never paid + no real exchange + actual promo
    # markers), not the bare `creator_we_follow` source that once muted 71 real
    # fans.
    from automations._common import load_promo_spam_ids  # lazy: avoid cycle
    if fan_id in await load_promo_spam_ids(account_id):
        log.debug("describe skipped (promo-spam creator) account=%s fan=%s",
                  account_id, fan_id)
        return False
    # A gif is read from Giphy's own public title — no model call, no cost — so the
    # SPEND budget below does not apply. The ledger already excludes gifs; without
    # the same exemption here the two halves disagree, and a free fan who has spent
    # his three photo reads then sends a gif gets `image_desc` NULL — a completely
    # blank turn for the chat engine, saving nothing, because the read was free.
    #
    # Deliberately BELOW scope and promo-spam, not above them: those two are policy
    # ("only read payers", "these are not prospects"), and being free is not a
    # reason to overrule an operator. Only the BUDGET is about money.
    if is_gif:
        return True
    # A $0 fan is still worth reading — he may pay later, and the read is what
    # makes her first reply land — but on a daily budget.
    if not await _free_fan_describe_allowed(account_id, fan_id, is_video):
        log.info("describe capped (free fan daily budget) account=%s fan=%s video=%s",
                 account_id, fan_id, is_video)
        return False
    return True


async def _fan_lifetime_spend_cents(account_id: str, fan_id: int) -> int:
    """The fan's lifetime spend (cents), 0 when unknown/absent. One home for the
    spend read this module does from several dispatch gates."""
    async with get_session() as s:
        v = (await s.execute(
            select(Fan.lifetime_spend_cents).where(
                Fan.account_id == str(account_id),
                Fan.fan_id == int(fan_id),
            )
        )).scalar_one_or_none()
    return int(v or 0)

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


# ── The human pause before a media reaction lands ────────────────────
#
# The picture IS the reply, and a picture is not typed: the whole pause is DARK,
# so nothing needs to be awake for it. We draw a target, put it on the job's
# `run_at`, and let the executor pick the job up when it comes due. Holding it
# inline instead would serialise every reaction on the account and burn one of the
# executor's four GLOBAL run slots for a minute of doing nothing. See
# plans/image-reply/PLAN.md §D3 and `pacing.picture_back_target`.

async def _message_created_at(account_id: str, fan_id: int,
                              message_id: int) -> datetime | None:
    """When HIS message actually landed — the anchor the pause is measured from.

    None when the row is not there (a webhook that outran the persist); the caller
    then falls back to `now`, which is the same thing to within a second."""
    async with get_session() as s:
        row = await s.get(Message, (str(account_id), int(fan_id), int(message_id)))
    return getattr(row, "created_at", None) if row is not None else None


def _deferred_run_at(now: datetime, trigger_at: datetime | None,
                     target_s: float) -> datetime:
    """When the reaction job should run, so the FAN experiences `target_s`.

    ⚠️ THE POINT OF THIS FUNCTION: the target is measured from HIS message, not
    from now, so everything already spent — the vision describe above us, the
    queue, the webhook hop — is counted INSIDE it rather than added on top. A
    describe that ate 20s of a 40s target leaves 20s left to wait, not 40. Double-
    counting it was the sharpest arithmetic error the plan's review caught (§D3,
    DA MAJOR 5): the number the fan feels is trigger→picture, and a design that
    only controls hold-LENGTH does not control that number at all.

    Two clamps, both load-bearing:
      max(now, …)         a describe SLOWER than the whole target means the pause
                          is already spent. Run now; never a negative wait.
      min(…, now+target)  a trigger timestamp in the FUTURE (OF clock skew — and
                          FakeOF's fixed `createdAt` in the tests, the same footgun
                          shape) would otherwise push the picture arbitrarily far
                          out. The target is the MOST we will ever wait, whatever
                          the clocks claim.

    Pure — no clock read, no DB — so the arithmetic is testable without a job."""
    target_s = max(0.0, float(target_s or 0.0))
    ceiling = now + timedelta(seconds=target_s)
    if trigger_at is None:
        return ceiling
    return min(max(now, trigger_at + timedelta(seconds=target_s)), ceiling)


async def _media_reply_pace_target(account_id: str, fan_id: int, message_id: int,
                                   *, seed_prefix: str) -> float:
    """Draw this photo's pause, or 0.0 when the operator turned the knob off.

    Seeded on the TRIGGER, so a webhook replay of the same photo draws the same
    target and cannot walk the picture further out on every retry."""
    from automations.tip_reward_config import _load_config
    cfg = await _load_config(account_id)
    if not cfg.get("media_reply_pace_enabled", True):
        return 0.0
    from automations import pacing
    seed = f"{seed_prefix}:{account_id}:{int(fan_id)}:{int(message_id)}"
    return pacing.picture_back_target(random.Random(seed))


async def _fan_in_resolution(account_id: str, fan_id: int) -> bool:
    """True iff this fan has an IN-PROGRESS make_right resolution — the multi-turn
    apology→free→…→PPV exchange owns his replies until it closes, so the normal
    chatter must not interleave it (same one-voice rule as a funnel)."""
    async with get_session() as s:
        row = (
            await s.execute(
                select(ResolutionLog.id).where(
                    ResolutionLog.account_id == str(account_id),
                    ResolutionLog.fan_id == int(fan_id),
                    ResolutionLog.status == "in_progress",
                ).limit(1)
            )
        ).first()
    return row is not None


async def _fan_mid_funnel(account_id: str, fan_id: int) -> bool:
    """True iff this fan has a pending funnel_state row under one of this
    account's mass runs — i.e. reply_mass_funnel owns the fan through a
    multi-step flow and welcome_chatter_for_info must not interleave it (Wave-3 overlap)."""
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
    is pure waste: welcome_chatter_for_info skips ANY skip-listed fan, so waking it for a fan
    it will refuse does nothing AND the fan gets no reply. Stages:

      • funnel      — pending funnel_state       → reply_mass_funnel
      • terminal    — any skip_list row ('info' graduation, spent/too_long,
                      hard blocks, …) → None
      • ai_chat     — still gathering (not skip-listed) → welcome_chatter_for_info

    The skip_list is the ONE stage marker, mirroring the automations' OWN gates
    (welcome_chatter_for_info _load_stop_lists / _graduate), so we never wake an automation
    that would just skip this fan. A graduated fan ('info') is only chatted by
    ai_chatter where that engine is enabled — its gate above owns the fan before
    this read is reached.
    """
    # A fan mid make-right exchange owns his replies until it closes — advance the
    # apology→free→…→PPV resolution rather than waking the normal chatter.
    if await _fan_in_resolution(account_id, fan_id):
        return "make_right"

    if await _fan_mid_funnel(account_id, fan_id):
        return "reply_mass_funnel"

    # PPVscriptAI: when ai_chatter is enabled it owns every fan under its spend
    # gate (it replaces welcome_chatter_for_info for them — one bot voice per fan); a fan
    # at/over the gate is a whale → human territory, no automation reply.
    # Lazy import to avoid a module cycle (ai_chatter imports welcome_chatter_for_info).
    try:
        from automations.ai_chatter import gate_for as _ai_gate_for
        ai_gate = await _ai_gate_for(account_id)
    except Exception:
        ai_gate = None
    if ai_gate is not None:
        spend = await _fan_lifetime_spend_cents(account_id, fan_id)
        return "ai_chatter" if spend < ai_gate else None

    async with get_session() as s:
        skip = (
            await s.execute(
                select(SkipList.reason).where(
                    SkipList.account_id == str(account_id),
                    SkipList.fan_id == int(fan_id),
                )
            )
        ).first()

    if skip is not None:  # skip-listed (any reason) → no automation replies
        return None

    return "welcome_chatter_for_info"  # not skip-listed → still in the welcome_chatter_for_info stage


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


async def _imminent_pending_job(
    account_id: str, kind: str, fan_id: int, within_s: float
) -> tuple[datetime, dict] | None:
    """The pending (account, kind) job already targeting THIS fan and due within
    `within_s` seconds — `(run_at, payload)` — or None if there is none.

    Per-fan (not per-kind) because W7 is fan-scoped now: two different fans
    replying must each get their own job. The window spans our response delay so
    two quick replies from the SAME fan don't double-queue.

    Returns the LATEST-due match, not the first row the DB happened to hand back,
    because the one caller that reads the `run_at` uses it as a FLOOR — the words
    it schedules must land behind every picture that is already queued, not just
    behind one of them. Most callers only want the boolean and take
    `_has_imminent_pending_job` below."""
    soon = datetime.utcnow() + timedelta(seconds=within_s)
    async with get_session() as s:
        rows = (
            await s.execute(
                select(ScheduledJob.run_at, ScheduledJob.payload_json).where(
                    ScheduledJob.account_id == str(account_id),
                    # kind_family: a pre-rename pending job must still dedup —
                    # this guard is what stands between a fan and a double send.
                    ScheduledJob.kind.in_(kind_family(kind)),
                    ScheduledJob.status == "pending",
                    ScheduledJob.run_at <= soon,
                )
            )
        ).all()
    fid = int(fan_id)
    best: tuple[datetime, dict] | None = None
    for run_at, pj in rows:
        try:
            p = json.loads(pj or "{}")
        except Exception:
            continue
        # Three shapes because three producers name the fan differently: the chat
        # engines take a LIST (`only_fan_ids`), the funnel takes `test_fan`, and
        # the reaction lanes (image_reply / tip_reward) are single-fan jobs that
        # carry a bare `fan_id`. Missing that third key made this function answer
        # "nothing queued" for every reaction job — a dedup that silently never
        # deduped the one lane whose replays actually double-SEND.
        if not (fid in (p.get("only_fan_ids") or [])
                or p.get("test_fan") == fid
                or p.get("fan_id") == fid):
            continue
        at = run_at or datetime.utcnow()
        if best is None or at > best[0]:
            best = (at, p)
    return best


async def _has_imminent_pending_job(
    account_id: str, kind: str, fan_id: int, within_s: float
) -> bool:
    """Is a reaction already queued for this fan? → skip a second enqueue.

    The predicate form of `_imminent_pending_job`, for the callers that only ask
    the yes/no question."""
    return await _imminent_pending_job(account_id, kind, fan_id, within_s) is not None


async def on_inbound_message(account_id: str, fan_id: int, message_id: int, *,
                             extra_payload: dict | None = None,
                             not_before: datetime | None = None) -> None:
    """React to one inbound fan DM: enqueue + wake the owning sweep so the bot
    replies in seconds instead of at the next tick. Never raises.

    `extra_payload` merges extra keys into the CHAT-ENGINE payload (it is ignored
    for reply_mass_funnel, which takes `test_fan` and nothing else). Its one caller
    today is the media lane: `on_inbound_image` hands the words job off to us after
    the vision describe, carrying `intent_fan_ids` when the image-closer flag is on.
    That flag used to enqueue its OWN ai_chatter job alongside the transcoder's —
    two jobs racing off one photo, one of them photo-blind. One job now, sighted,
    with the closer's intent riding on it. See plans/image-reply/PLAN.md §D2.

    `not_before` FLOORS the job's `run_at`. Its one caller is the same media lane,
    for the one exit where a picture is already queued behind us: the words must
    follow the picture, not race it (`tip_reward._hand_off_words`), and the
    ordinary `run_at` here is `now + _response_delay`, which defaults to zero.
    A floor and not a replacement — the operator delay and the cooldown deferral
    still apply on top, whichever lands later."""
    try:
        if _global_kill_switch():
            return
        cfg = await _load_config(account_id)
        if cfg is None:
            return
        kind = await _classify_kind(account_id, fan_id)
        if kind is None:
            # Terminal stage (skip-listed, or a whale over the gate): no automation
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
        # sweep — we DEFER the reply to the moment the cooldown ends. The chat
        # engines re-check the cooldown at run time, so if a human keeps the
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
        if not_before is not None and run_at < not_before:
            # Never EARLIER than the caller's floor; the cooldown deferral above
            # may already have pushed us past it, in which case it does nothing.
            run_at = not_before
        wait_s = max(0.0, (run_at - now).total_seconds())

        # Dedup over the FULL wait (cooldown remainder + delay) so a second
        # message during the rest doesn't queue its own deferred job.
        if await _has_imminent_pending_job(account_id, kind, fan_id, wait_s + _DEDUP_WINDOW_S):
            log.debug("w7_skip_dedup account=%s kind=%s fan=%s", account_id, kind, fan_id)
            ax.wake_supervisor()  # still wake so the already-pending job drains
            return

        # Fan-scope the run so it reacts to ONLY this fan (no full-account sweep):
        # reply_mass_funnel takes `test_fan`; the chat engines take
        # `only_fan_ids` (gates still apply — see each automation's run()).
        payload = (
            {"test_fan": int(fan_id)} if kind == "reply_mass_funnel"
            else {"only_fan_ids": [int(fan_id)], **(extra_payload or {})}
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
    # ── customs: mark him owed NOW, not up to 15 minutes from now ──────
    #
    # ⚠️ FIRST, AND ABOVE EVERY RETURN BELOW. `customs_watch` sweeps on a 900s rule
    # while `ai_chatter` ticks every 60s, so a man could tip $200 for a voice note
    # and be pitched again a dozen times before anything recorded that he had
    # bought one. The tip's `Transaction` row is ALREADY written live by
    # `event_transcoder._record_inbound_payment` (the WS frame carries the dollars
    # in `tipAmount`), so the sweep's own data is there within a second — only the
    # CADENCE was slow. This closes that window to one job hop.
    #
    # IT MUST NOT SIT BELOW THE TIP_REWARD GATES. Two returns follow: the
    # `has_open_tip_offer` standdown (which returns unless `always_reward`) and
    # `if not enabled`. Both are decisions about whether to send REWARD MEDIA, and
    # a customs order is not that — putting this after either would mean a tip on
    # an account with tip_reward switched off never marks anything, which is
    # precisely the "gated on an unrelated flag" bug this lane has already shipped
    # twice (see `_customs`, `04c8950`).
    #
    # Independently try/except'd for the same reason: a tip_reward failure must not
    # cost us the debt, and vice versa.
    try:
        from automations.customs_watch import order_floor, watch_flags
        cw_on, cw_payload = await watch_flags(account_id)
        # GATED ON THE RULE, and that gate is load-bearing: a $100 DM tip is an
        # ORDER on the male accounts and was GENEROSITY five times last month on
        # the female ones. Firing this account-blind would gag the bot on every
        # generous fan — Amia alone took 47 such tips in 90 days.
        if cw_on and int(tip_cents or 0) >= order_floor(cw_payload):
            # ENQUEUE THE SWEEP, DO NOT MARK INLINE. Marking here would be a second
            # expression of "is this tip an order?" — it would have to restate the
            # settled-status filter, the `customs_cleared_at` re-mark guard and the
            # already-delivered check. This codebase's own lesson is that a safety
            # property restated in two voices is a safety property with no owner
            # (`_voice.CUSTOMS_CONDITIONS` shipped exactly that drift). So the fast
            # path runs the SAME `run`, scoped to one fan and carrying the rule's
            # own payload, so the floor and dry_run cannot diverge.
            await ax.enqueue_job(
                account_id, "customs_watch",
                payload={**cw_payload, "only_fan_ids": [int(fan_id)]},
            )
            ax.wake_supervisor()
            log.info("customs_watch_dispatch account=%s fan=%s msg=%s cents=%s",
                     account_id, fan_id, message_id, tip_cents)
    except Exception:
        log.warning("customs_watch_dispatch_failed account=%s fan=%s",
                    account_id, fan_id, exc_info=True)

    try:
        from automations.tip_reward import reward_flags  # lazy: avoid import cycle
        enabled, always_reward = await reward_flags(account_id)

        # PPVscriptAI: a fan with an OPEN tip-capable ai_chatter offer is paying
        # toward THAT — the offer claims the tip (the run's unlock watcher
        # accumulates + delivers) and the generic tip_reward normally stands down,
        # so the fan doesn't get reward media on top of the unlocked piece.
        # `always_reward` overrides that standdown: the offer is STILL credited
        # below, and the tip_reward fires anyway (the creator opted in).
        try:
            from automations.ai_chatter import has_open_tip_offer
            if await has_open_tip_offer(account_id, fan_id):
                await ax.enqueue_job(account_id, "ai_chatter",
                                     payload={"only_fan_ids": [int(fan_id)]})
                ax.wake_supervisor()
                log.info("ai_chatter_tip_claim account=%s fan=%s msg=%s cents=%s%s",
                         account_id, fan_id, message_id, tip_cents,
                         " (always_reward: tip_reward also firing)" if always_reward else "")
                if not always_reward:
                    return
        except Exception:
            log.warning("ai_chatter_tip_claim_failed account=%s fan=%s — falling "
                        "back to tip_reward", account_id, fan_id, exc_info=True)

        if not enabled:
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


async def on_inbound_image(account_id: str, fan_id: int, message_id: int,
                           has_media: bool = True, is_video: bool = False) -> None:
    """A fan sent an IMAGE (non-tip inbound media) → a buying signal. Two
    independent, separately-gated reactions (both config in tip_reward_config_json,
    both default OFF):

      • image_reply_enabled  → enqueue ONE free vault item from the tip folder's
        'under $10' (basic) tier — the tip_reward `image_reply` mode, per-fan
        throttled (also dedups webhook replays of the same image).
      • image_closer_enabled → flag the image as buying intent on the words job
        (`intent_fan_ids` — it drives the cadence gate's pic tier; the photo
        carries no text the intent regexes can match). Requires ai_chatter
        enabled; with it off this flag is inert.

    THE WORDS. `event_transcoder` no longer fires `on_inbound_message` for a media
    DM (see its `is_media_dm`): the words for this photo are OUR responsibility,
    because only this path knows when the vision describe has landed. Firing both
    made the text photo-blind — the W7 job ran ~1s after the photo, before the
    describe wrote `messages.image_desc`, and she answered an empty line.

    Whoever finishes with the photo LAST hands the words off:
      • all three flags off, or the fan has no describe budget → we hand off
        immediately (an ordinary media DM must still get its ordinary reply);
      • describe on, pic lane off → we hand off right after the describe returns;
      • pic lane on → `tip_reward` hands off in its own `finally`, after the
        picture, on every exit path (sent, throttled, no media, disabled, error);
      • the describe raised, or the whole coroutine was CANCELLED mid-describe
        (a relay restart, a redeploy) → the `except BaseException` hands off
        best-effort. Losing the words because vision hiccuped — or because we
        were redeployed while waiting on it — is the one outcome worth catching
        that widely; see the handler for why the re-raise makes it safe.

    Gated SEPARATELY from the tip hook: an image reply / closer pivot should fire
    even on a fan no chat sweep would answer. Raises nothing of its own; a
    TEARDOWN (`CancelledError`, `KeyboardInterrupt`, `SystemExit`) is handed back
    to its raiser AFTER the words have been handed off.

    `has_media=False` = the DM carried no media, only a Giphy id (see
    event_transcoder). We still describe it — free, off Giphy's public title — but
    NEITHER buying-signal reaction fires: a gif is a joke, not a nude.

    `is_video` picks which lane of the free-fan daily budget this DM spends."""
    try:
        from automations.tip_reward_config import (
            image_describe_flags, image_reply_flags,
        )
        send_img, run_closer = await image_reply_flags(account_id)
        describe_on, describe_seed, describe_scope = await image_describe_flags(account_id)

        # A media-less DM is a Giphy gif. It reaches this hook so the AI can know
        # what he sent, but it is NOT the buying signal a nude photo is: a laughing
        # gif must never earn a free vault item or pivot the closer into selling.
        # Name it, then let the ordinary reply lane answer.
        if not has_media:
            send_img = run_closer = False

        # Scope, promo-spam and the free-fan budget are ONE question, and it lives
        # in _should_describe — this function already owns enough (gifs, the vault
        # reply, the closer kick) without a spend policy interleaved into it.
        if describe_on:
            describe_on = await _should_describe(
                account_id, fan_id, scope=describe_scope, is_video=is_video,
                # A media-less DM IS the gif case, already established above.
                is_gif=not has_media)

        # `intent_fan_ids` only means something to ai_chatter, and only when it is
        # the engine that answers him. Resolve it BEFORE any hand-off so every exit
        # path below carries the same payload.
        if run_closer:
            from automations.ai_chatter import is_enabled as _ai_enabled
            if not await _ai_enabled(account_id):
                run_closer = False  # closer flag on but ai_chatter off → inert
                log.debug("image_closer inert (ai_chatter disabled) account=%s fan=%s",
                          account_id, fan_id)
        extra = {"intent_fan_ids": [int(fan_id)]} if run_closer else None

        if not (send_img or run_closer or describe_on):
            # Nothing to do with the photo itself — but the transcoder deferred the
            # words to us, so a media DM on an all-flags-off account must still get
            # its ordinary reply. This is the regression guard for the whole change.
            await on_inbound_message(account_id, fan_id, message_id,
                                     extra_payload=extra)
            return

        # Vision-describe the photo FIRST and BLOCK on it (a few seconds, reads as
        # human typing) — the description is cached on messages.image_desc, and the
        # words job we hand off below reads it straight back into its history as
        # "[he sent: …]". Awaiting here is what lets the FIRST reply rate the
        # picture instead of a later one. Never raises; a describe miss just leaves
        # image_desc NULL and the reply proceeds photo-blind (prior behavior).
        if describe_on:
            from inbound_describe import describe_inbound_message  # lazy: avoid cycle
            await describe_inbound_message(account_id, int(message_id), describe_seed)

        if send_img:
            # The pic lane runs, so IT owns the hand-off — its `finally` calls
            # on_inbound_message after the picture, on every exit path. Two sends,
            # hers, in order: the picture, then the words about it.
            #
            # Dedup FIRST: a webhook replay of the same photo would otherwise chain
            # a second deferred job behind the first and send two freebies a minute
            # apart. The pic lane's own per-fan cooldown catches the same replay,
            # but only after the job has been claimed and a slot spent — and with
            # `image_reply_cooldown_hours: 0` (a documented setting) it does not
            # catch it at all. The window spans the longest target we can draw.
            #
            # ⚠️ The match is (account, kind-family, FAN) — `_imminent_pending_job`
            # never reads `trigger_message_id`, so this suppresses the freebie for a
            # genuinely NEW second photo inside 90s as well as for a replay. That is
            # deliberate: two freebies a minute apart is the failure, and one is the
            # right answer to two photos in one breath. Only the PICTURE is
            # suppressed — the words still reach him, one way or the other.
            pending = await _imminent_pending_job(
                account_id, "image_reply", fan_id,
                within_s=_PICBACK_DEDUP_WINDOW_S)
            if pending is not None:
                pending_at, pending_payload = pending
                log.info("image_reply_dedup account=%s fan=%s msg=%s (one already queued)",
                         account_id, fan_id, message_id)
                # WHO says the words. This exit used to be the one of the five that
                # owned the photo and handed nothing on — but handing off
                # unconditionally is worse, because the pending job's own `finally`
                # is the enqueuer that knows WHEN the picture landed and we do not.
                # Ours would be due at `now + _response_delay` (0 by default) while
                # the picture is up to 90s out: the words would go first, ai_chatter
                # would set its per-fan cooldown, and the freebie would land
                # WORDLESS — the exact failure `_phantom`/`bound_to_pic` exists to
                # prevent — and our earlier job would dedup away the correct,
                # cooldown-deferred one the pic lane was about to enqueue.
                #
                # So: speak only for a pending job that will not speak for itself
                # (`force`, `dry_run`, or no trigger message — `tip_reward`'s own
                # guard, asked rather than re-typed), and even then never before
                # the picture it belongs behind. The pending job's hand-off is
                # fan-scoped and its gather sees BOTH inbound rows, so in the
                # ordinary case this photo is answered by it too.
                from automations.tip_reward import hands_off_words  # lazy: cycle
                if not hands_off_words(pending_payload):
                    await on_inbound_message(account_id, fan_id, message_id,
                                             extra_payload=extra,
                                             not_before=pending_at)
                return

            # The pause. Deferred, never held: `run_at` in the future costs no slot
            # and no thread, and the describe above us is counted INSIDE the target
            # rather than added to it (see `_deferred_run_at`).
            now = datetime.utcnow()
            target_s = await _media_reply_pace_target(
                account_id, fan_id, message_id, seed_prefix="picback")
            run_at = _deferred_run_at(
                now, await _message_created_at(account_id, fan_id, message_id),
                target_s)
            wait_s = max(0.0, (run_at - now).total_seconds())
            await ax.enqueue_job(
                account_id, "image_reply",
                payload={"fan_id": int(fan_id), "image_reply": True,
                         "trigger_message_id": int(message_id),
                         "pace_target_s": round(target_s, 2),
                         # `extra`, not a second copy of its expression: it was
                         # resolved above precisely so every exit path carries the
                         # same payload, and this is the exit whose payload
                         # `tip_reward._hand_off_words` reads back out to rebuild
                         # it. A key added to `extra` reached three exits and not
                         # this one.
                         **(extra or {})},
                run_at=run_at,
            )
            # Wake AT the due time, not now: the 30s fallback tick would otherwise
            # add up to another half-minute on top of a pause we just drew to the
            # second. `_schedule_wake` keeps a strong ref (a GC'd wake task is a
            # bug this file has already been bitten by).
            if wait_s > 0:
                _schedule_wake(wait_s)
            else:
                ax.wake_supervisor()
            log.info("image_reply_deferred account=%s fan=%s msg=%s target=%.1fs wait=%.1fs",
                     account_id, fan_id, message_id, target_s, wait_s)
        else:
            # No picture is coming; the describe (if any) has landed. We are the
            # last one holding the photo, so the words are ours to hand off.
            await on_inbound_message(account_id, fan_id, message_id,
                                     extra_payload=extra)
        log.info("image_dispatch account=%s fan=%s msg=%s reply=%s closer=%s describe=%s",
                 account_id, fan_id, message_id, send_img, run_closer, describe_on)
    except BaseException as exc:
        # ⚠️ `BaseException`, not `Exception`, and the re-raise below is what makes
        # that safe. `asyncio.CancelledError` is a BaseException, and this coroutine
        # spends SECONDS awaiting a live vision call inside a fire-and-forget task —
        # so a relay restart, a redeploy or a supervisor cancel lands here far more
        # often than any hiccup does. Under `except Exception:` the cancel walked
        # straight through, the hand-off never ran, nothing was logged, and the
        # fan's reply was gone permanently: `event_transcoder` no longer fires
        # `on_inbound_message` for a media DM, so this really is the last line
        # between a media DM and silence. (`describe_inbound_message`'s own guard
        # is `except Exception:` too, and its docstring's "NEVER raises" is true
        # for Exception and false for a cancel — so the cancel reaches us intact.)
        #
        # `send_welcome`'s admission loop already reasons exactly this way about
        # its own children; the reasoning simply had not crossed the file boundary.
        # A BaseException that is not an Exception is a TEARDOWN — a cancel, a
        # SIGINT, a SystemExit. Hand the words off, then give it back to whoever
        # raised it: swallowing one here would be a worse bug than the one this
        # `except` widened to catch.
        teardown = not isinstance(exc, Exception)
        log.warning("image_dispatch_failed account=%s fan=%s teardown=%s",
                    account_id, fan_id, teardown, exc_info=True)
        # Best-effort: a crash anywhere above (a describe hiccup, a config read)
        # must not cost the fan his TEXT reply — the transcoder is no longer
        # firing it for him. Nested guard because on_inbound_message is itself
        # wrapped, and this is the last line between a media DM and silence.
        #
        # No `extra_payload`: the crash may have come before `run_closer` was even
        # resolved, and losing the closer's intent hint is a far smaller loss than
        # losing the reply. If a hand-off already happened before the crash, this
        # second call is absorbed by on_inbound_message's own per-fan dedup.
        #
        # A plain await, even on the teardown path: a cancel that has already been
        # DELIVERED (which is what put us in this handler) leaves no pending one
        # behind, so the enqueue below runs to completion and the durable
        # `scheduled_jobs` row is written before we hand the teardown back. Only a
        # second, harder cancel can interrupt it — logged, not shielded, because a
        # shielded task we then abandon by re-raising is an orphan the loop
        # complains about and nobody awaits.
        try:
            await on_inbound_message(account_id, fan_id, message_id)
        except Exception:  # pragma: no cover — defensive
            log.warning("image_dispatch handoff failed account=%s fan=%s",
                        account_id, fan_id, exc_info=True)
        except asyncio.CancelledError:  # pragma: no cover — a second cancel
            log.warning("image_dispatch handoff cancelled account=%s fan=%s",
                        account_id, fan_id)
        if teardown:
            raise
