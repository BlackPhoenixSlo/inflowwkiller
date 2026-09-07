"""
service/automations/welcome_turn.py — whose turn is it after the welcome?

The `stop_on_reply` half of `send_welcome` (plans/welcome-pacing §C): did the
fan say something WITH WORDS during the burst, and if a bubble of ours landed
on top of his reply, hand the turn to whichever chat engine owns brand-new
subs. Both engines gate their candidates on "the fan spoke last", and a
finishing bubble moves that — without the handoff `stop_on_reply` would ship a
girl who stops spamming and starts ignoring people.

Three things live here and nowhere else:

  • `newest_worded_inbound` — the ONE reply predicate. Words decide, not the
    message type (operator: "dont stop on single tip only on text"); the SQL
    clauses are `_common.worded_inbound_where`, shared with the engines'
    Python-side twin `_common.inbound_is_words`.
  • `handoff_engine` / `enqueue_turn_handoff` — which engine answers a rescued
    fan, and the one job that opens its turn gate (`turn_handoff_ids`).
  • `hand_back_turn` — the classification: his row against the last bubble of
    ours that landed, provider stamps only (`Landed`).

`WELCOME_REST_S` is the fan's rest after his welcome lands; it lives here
because the handoff job's delay is measured from it, and `send_welcome` starts
that rest in its own `finally`.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import NamedTuple

from sqlalchemy import select

import automation_executor as ax  # enqueue_job
from ._common import is_hard_skipped, worded_inbound_where
from db.engine import get_session
from db.models import AutomationRule, Message


log = logging.getLogger("of-relay.automation.send_welcome")

# How long a fan rests after his welcome lands, before the chat engine may answer.
#
# A new subscriber is the hottest lead there is and he replies FAST — measured
# 07-26, one answered 50 seconds after the welcome. He then waited ~14 minutes,
# because two brakes were running and the wrong one won: this sender set a
# deliberate 10-minute rest and then left its 15-minute fan LEASE lying around
# to expire, and the generic infra timeout silently outranked the policy. The
# lease is now handed back on a confirmed send, so this constant is the only
# thing pacing him — one brake, one number, and it is the one somebody chose.
WELCOME_REST_S = 150          # 2.5 min — long enough not to talk over the welcome

# How long AFTER the welcome rest the handoff job is due. Both engines check
# `ax.fan_on_cooldown` in their send loops and `send_welcome` starts a
# `WELCOME_REST_S` cooldown in its own `finally`, so a job due any earlier is a
# job that dies on our own brake. The pad also keeps V2's "don't talk over the
# welcome" promise and reads like a person: she notices his message a few
# minutes later, not in the same breath as the GIF.
HANDOFF_DELAY_PAD_S = 15


# ── stop_on_reply: did he say something, and whose turn is it now? ────

async def newest_worded_inbound(account_id: str, fan_id: int
                                 ) -> tuple[datetime, int] | None:
    """`(created_at, message_id)` of his newest inbound row that used WORDS, or
    None when there is no such row. RAISES on a query error — each caller decides
    what a failed read means for it (see `fan_replied_since`).

    ⚠️ WORDS DECIDE, not the message TYPE. Operator, 2026-09-06: *"dont stop on
    single tip only on text"*. So `is_tip` is deliberately absent from the
    predicate — the question is whether he said something to her:

      • a text message        → words. The literal ask.
      • a tip WITH a note     → words. The note is addressed at her, and it is the
                                strongest engagement signal on the platform.
      • a BARE tip            → not words. Applause, not a conversational turn:
                                there is nothing to answer, the finishing welcome
                                reads fine over it, and the ledger-derived rows
                                land up to 5 minutes late (transaction_ingest's
                                poll), so aborting on one would be unreliable
                                exactly when it fired.
      • media with NO caption → not words. "only on text" admits no wordless
                                message. A CAPTIONED photo carries text and does
                                stop the burst.

    Ordered by `(created_at, message_id)`: `message_id` is OF's own BigInteger and
    both timestamps are provider-stamped, so the tiebreak needs no clock of ours.

    🚩 One consequence worth knowing: a fan who answers with a bare photo and no
    caption gets the rest of the burst over the top of it. If that ever reads
    wrong, re-admitting empty-bodied non-tip rows is one clause here."""
    async with get_session() as s:
        row = (await s.execute(
            select(Message.created_at, Message.message_id).where(
                Message.account_id == str(account_id),
                Message.fan_id == int(fan_id),
                Message.direction == "in",
                Message.is_unsent.is_(False),
                *worded_inbound_where(),
            )
            .order_by(Message.created_at.desc(), Message.message_id.desc())
            .limit(1)
        )).first()
    return (row[0], int(row[1])) if row is not None else None


async def fan_replied_since(account_id: str, fan_id: int,
                             anchor: tuple[datetime, int] | None
                             ) -> tuple[datetime, int] | None:
    """His newest worded inbound IF it is newer than `anchor`, else None.

    The anchor is a ROW, not a clock (§C1) — snapshotted at the top of his task —
    so a sub who messaged us BEFORE his welcome still gets the full burst, and no
    part of this depends on our own wall clock agreeing with OF's.

    ANY failure reads as "no reply seen": logged, swallowed, burst continues.
    This knob may only ever REMOVE bubbles, and a read that fails must never be
    the thing that costs a fan his welcome.

    The COMPARISON is inside the same `try` as the query, deliberately. It is a
    tuple compare against a row this process did not build — a `created_at` that
    came back None makes `newest > anchor` a TypeError, and an exception escaping
    from here does not degrade to "finish the burst": it kills the fan's whole
    task, mid-welcome, on the one path whose entire contract is that it cannot."""
    try:
        newest = await newest_worded_inbound(account_id, fan_id)
        if newest is None:
            return None
        return newest if (anchor is None or newest > anchor) else None
    except Exception:
        log.warning("send_welcome reply check failed account=%s fan=%s — "
                    "finishing the burst", account_id, fan_id, exc_info=True)
        return None


async def rule_enabled(account_id: str, kind: str) -> bool:
    """Is there an ENABLED `automation_rules` row of this kind on the account?
    The house way to ask whether an automation is switched on for an account when
    it keeps no config blob of its own (precedent: `customs_watch.flags`)."""
    async with get_session() as s:
        row = (await s.execute(
            select(AutomationRule.id).where(
                AutomationRule.account_id == str(account_id),
                AutomationRule.kind == str(kind),
                AutomationRule.is_enabled.is_(True),
            ).limit(1)
        )).first()
    return row is not None


async def handoff_engine(account_id: str) -> str | None:
    """Which chat engine should answer the reply our welcome talked over — or
    None when this account runs no chat engine at all.

    `welcome_chatter_for_info` owns brand-new subs by construction: ai_chatter's
    payer floor hands every fan who has never bought CONTENT to it, and a
    brand-new sub has bought nothing (a subscription is not a purchase). So:

      1. ai_chatter is enabled AND owns the whole account → it replaces the
         gatherer entirely, so it is the only voice there is.
      2. else an enabled welcome_chatter_for_info rule → the gatherer.
      3. else ai_chatter enabled in SUBSET mode → it, forced past its own payer
         floor. Better the seller's voice once than silence.
      4. else → nobody. Nothing would have answered him today either.

    Exactly ONE job is ever enqueued, so a subset-mode account (both engines live)
    cannot produce two voices: the new sub is not in `engaged_subset`, rule 2
    picks the gatherer, and rule 1 already declined."""
    from .ai_chatter import is_enabled as _ai_enabled
    from .ai_chatter import owns_whole_account as _ai_owns_all
    ai_on = await _ai_enabled(account_id)
    if ai_on and await _ai_owns_all(account_id):
        return "ai_chatter"
    if await rule_enabled(account_id, "welcome_chatter_for_info"):
        return "welcome_chatter_for_info"
    return "ai_chatter" if ai_on else None


async def enqueue_turn_handoff(account_id: str, fan_id: int) -> str | None:
    """Hand this fan's TURN to a chat engine. Returns the kind enqueued, or None
    when no engine exists.

    WHY THIS EXISTS AT ALL: the bubble that finished landing after his reply moved
    `last_dir` to "out", and BOTH engines gate their candidates on the fan having
    spoken last. Without this job nothing answers him until he double-texts —
    which is a worse product than the burst it fixed.

    The payload carries three keys and they do three different jobs:
      `only_fan_ids`     scope the run to him (no full-account sweep),
      `force_ids`        skip the discretionary gates (mid-funnel, promo-spam,
                         the payer floor, the content-payer skiplist),
      `turn_handoff_ids` the ONE new seam — the turn gate itself, which no
                         existing force bypasses in either engine. It is
                         self-verifying on the engine side: if a human or another
                         automation answered him inside the window, the engine
                         drops the job rather than double-replying.
    Blacklist, `automation_paused_until`, the muted-creator hard skip, the fan
    lease and the fan cooldown ALL still bind — see the enumeration in §C3.

    `run_at` clears our own 150s rest (both engines check `fan_on_cooldown` in
    their send loops); the executor's 30s tick then picks it up within half a
    minute, so he is answered about three minutes after he wrote."""
    kind = await handoff_engine(account_id)
    if kind is None:
        return None
    await ax.enqueue_job(
        account_id, kind,
        payload={"only_fan_ids": [int(fan_id)], "force_ids": [int(fan_id)],
                 "turn_handoff_ids": [int(fan_id)]},
        run_at=datetime.utcnow() + timedelta(
            seconds=WELCOME_REST_S + HANDOFF_DELAY_PAD_S))
    return kind


class Landed(NamedTuple):
    """The OF row one of our bubbles became — and whether OF actually told us.

    `provider_stamped` is the field that matters. The whole abort classification
    is one comparison, "whose row is newer, his reply or the bubble that landed
    on top of it", and it is only honest while BOTH sides are OF's own numbers:
    his row is provider-stamped by definition (the WS pump wrote it from OF's
    payload), so a bubble of ours carrying OUR wall clock and a fabricated id is
    not comparable to it at all. It used to be compared anyway —
    `_parse_iso(...) or datetime.utcnow()` paired with `int(msg_id or 0)` — and
    the fabricated `0` lost every same-second tie, so an id-less send read as
    "his turn" and silently declined the handoff. That is a fan nobody answers.

    When the stamp is ours, the comparison is skipped and the turn is handed off.
    The asymmetry is deliberate: a handoff we did not need costs at most one
    reply he was getting anyway (the engine-side guard is self-verifying, and a
    fan who really did speak last is a normal candidate there), while a handoff
    we needed and skipped costs him every reply until he double-texts."""
    created_at: datetime
    message_id: int
    provider_stamped: bool



async def hand_back_turn(
    account_id: str, fan_id: int, *,
    anchor: tuple[datetime, int] | None, check_reply: bool,
    aborted: tuple[datetime, int] | None, last_landed: Landed | None,
    landed: int,
) -> str:
    """After the burst: did he speak, and WHOSE TURN is it now?

    `aborted` is his reply when a checkpoint cut the burst short (None ⇒ the
    burst ran out); `last_landed` is the last bubble of ours that landed. Runs
    AFTER the follow-back, deliberately — the handoff job's `run_at` is measured
    from now, and the rest it has to clear does not start until the caller's
    `finally`, so every OF call between here and there eats into the pad.

    Returns one word the caller books:
      "quiet"      he said nothing during the burst — nothing to do.
      "his"        his row is NEWER than our last bubble: the turn is his, and
                   both engines pick him up on their own cadence once the rest
                   expires. The common case — the quiet-phase checkpoint catches
                   most replies before a bubble ever ships.
      "handoff"    a bubble she was already typing landed ON TOP of his reply
                   and took the turn with it; a forced-turn job was enqueued.
      "no_engine"  same, but the account runs no chat engine at all.
      "restricted" same, but he was hard-restricted mid-burst — no job.
      "failed"     the handoff could not be enqueued (logged, swallowed: he has
                   his welcome, and a failed handoff must not cost him the rest).
    """
    # ── CHECKPOINT 4 (§C2, the TAIL). Every check in the burst runs BEFORE a
    # send; not one of them runs after the LAST send. On the shipped default
    # shape (no `gif_id`) that leaves the final bubble uncovered: he answers
    # while she is typing the question, it lands on top of his reply, the loop
    # runs out with `aborted` still None, and nothing below would run. His
    # `last_dir` is now "out" and nothing answers him until he double-texts —
    # invisibly, because the burst was never cut short.
    #
    # So ask once more, after the burst — but ONLY when the loop did not already
    # break on a reply. That reply is classified below; re-reading it here would
    # hand one turn off twice.
    his_reply = aborted
    if check_reply and his_reply is None:
        his_reply = await fan_replied_since(account_id, fan_id, anchor)
    if his_reply is None:
        return "quiet"

    # Only OF's own numbers may decide this. An unstamped bubble (no
    # `createdAt`, no id — OF answered 200 with a body we could not read) is not
    # comparable to his provider-stamped row, so we do not guess: the turn is
    # handed back. See `Landed`.
    his_turn = last_landed is None or (
        last_landed.provider_stamped
        and his_reply > (last_landed.created_at, last_landed.message_id))
    log.info("send_welcome saw a reply mid-burst account=%s fan=%s "
             "landed=%d stopped=%s turn=%s", account_id, fan_id,
             landed, aborted is not None, "his" if his_turn else "handoff")
    if his_turn:
        return "his"
    try:
        # THE BELT (§C3's skip_reasons row). `force_ids` bypasses ai_chatter's
        # skip_reason gate and this job carries it — near-vacuous, because he
        # was deliverable seconds ago, but a restriction CAN land mid-burst (a
        # scrape discovers he muted us) and `test_fan` reaches this path past
        # the run's own hard-skip filter entirely. ONE row, `LIMIT 1`, read
        # fresh — not the account's whole hard-skip set built for a single
        # membership inside a paced burst with six of them in flight.
        if await is_hard_skipped(account_id, fan_id):
            log.info("send_welcome skipped the turn handoff for a restricted "
                     "fan account=%s fan=%s", account_id, fan_id)
            return "restricted"
        kind = await enqueue_turn_handoff(account_id, fan_id)
        if kind is None:
            log.warning("send_welcome ate a reply and this account runs no chat "
                        "engine account=%s fan=%s", account_id, fan_id)
            return "no_engine"
        log.info("send_welcome handed the turn to %s account=%s fan=%s",
                 kind, account_id, fan_id)
        return "handoff"
    except Exception:
        log.warning("send_welcome turn handoff failed account=%s fan=%s — his "
                    "welcome already landed", account_id, fan_id, exc_info=True)
        return "failed"
