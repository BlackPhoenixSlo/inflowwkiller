"""service/assistant_fan_pack.py — the fan evidence pack, fetched.

The IMPURE half of the help bot's v3 answer (plan:
plans/help-chatbot/06-v3-fan-why.md). The operator pastes a chat header, an @u
handle and a model name and asks "why didn't AI chatter reply to him?"; this
module works out who he means, reads the rows, and hands `assistant_evidence` a
typed record to render.

AN EVIDENCE PACK, NOT A RE-DERIVATION. Code fetches typed facts; the model
ranks causes. Nothing here runs an engine's fetch or send path — those have
live OnlyFans side effects, and re-running today's gates to explain yesterday's
send is historical fiction dressed as diagnosis.

The MANDATORY exception is the opposite case: a predicate that exists to be the
SINGLE COPY of a question must be reused, never re-implemented.
  • `ai_chatter.skip_reason_blocks` — does this skip_list row actually close
    the thread? Its docstring records what a second copy costs: the fans badge
    hand-rolled the rule, missed the engage_old_fans lift, and labelled 96 fans
    "🚫 Skipped (old_fan_pre_ai)" while the engine was happily replying to them.
    A pack that printed the row as a blocker would be that bug in prose.
  • `_common.content_payer_fans` — "he is a customer" means a tip or a PPV
    unlock. A SUBSCRIPTION IS NOT A PURCHASE, and `lifetime_spend_cents` cannot
    tell them apart.
  • `fans.get_fan_ai_status` — the fan-status badge, which already evaluates
    ai_chatter's gates in run()'s own order, first-hit-wins, and is documented
    read-only with no OF calls. It is what the operator sees in the chat
    drawer, so reusing it means the bot and the UI cannot disagree.
All are local reads, and the relay imports every engine at boot, so none of
them adds a side effect.

LOCAL DB ONLY. Nothing in this path may touch OnlyFans — remember /health is an
OF proxy. Any failure degrades to a docs-only answer with an honest note; the
endpoint's 200-always contract never surfaces it as an error, because a bot
that looks broken exactly when someone is debugging a silent fan is the failure
mode the whole feature exists to avoid.
"""
from __future__ import annotations

import asyncio
import logging
import re
from time import monotonic
from typing import Any, NamedTuple

from sqlalchemy import func, or_, select

import assistant_evidence as ev
from auth import _actor_account_ids
from db.engine import get_session
from db.models import (
    Account, AccountAiConfig, AutomationRule, Blacklist, Fan, FollowupState,
    List, ListMember, MassRun, Message, SkipList, Transaction, WelcomeSent,
    created_at_text,
)

log = logging.getLogger("of-relay.assistant_fan_pack")

RESOLVE_TIMEOUT_S = 0.3      # three small indexed reads: who is he?
TIMEOUT_S = 1.5              # the evidence itself (local, indexed reads)
STATUS_BUDGET_S = 0.8        # …of which the shared status endpoint may take this
_RENDER_RESERVE_S = 0.15     # …and this much is held back to render what we have
_TIMELINE_SCAN = 40          # message rows read
_NICKNAME_PROBES = 4         # pasted lines tried as a nickname

# Names what was missing, not "the manual only": the two blocks fail
# independently, and an answer that still had the account's live state while
# only the fan's rows were unreadable is not a manual-only answer.
FAIL_NOTE = (
    "\n\n(I couldn't read this fan's own rows just now, so nothing above is "
    "specific to him — check the chat's AI status for his live standing.)")

# OnlyFans user ids are 8-10 digits. A shorter number in a question is a cap, a
# price or a year, and must not be reported as a missing fan — but any number
# that MATCHES a real Fan row is self-validating and is still accepted.
_MIN_FAN_ID = 10_000_000
_INT64_WALL = 2 ** 63        # SQLite refuses anything at or past this

# The kinds that put a BROADCAST on a thread. Used only to pick whose audience
# to describe when nothing on this fan's thread names a run.
_MASS_KINDS = frozenset({
    "mass_nudge", "mass_premade", "online_blast", "ppv_send",
    "send_mass_message",
})

# The fan-facing kinds whose ABSENCE is evidence: "nobody re-opens a silent fan
# on an account with no follow-up rule" was the real answer in the case this
# feature was built from. Intersected with the LIVE registry before rendering,
# so the list can never name a kind that has been retired.
#
# ⚠️ SCHEDULED KINDS ONLY. The claim "no rule ⇒ nothing of that kind reaches
# him" is only true for kinds that ONLY ever run off a rule. `scheduled_send`
# is enqueued straight from the chat composer by a human, `tip_reward` fires
# from `webhook_dispatch.on_inbound_tip` on its config flag alone, and
# `arc_tease` is BOOKED as a ScheduledJob by `vault_arc` when an operator
# approves an arc — all three reach a fan on an account with no rule at all, so
# listing them here would make the pack's most decisive line a false one.
_FAN_FACING_KINDS: tuple[str, ...] = (
    "ai_chatter", "autoreply", "customs_watch", "make_right",
    "mass_nudge", "mass_premade", "nudge_online", "online_blast", "ppv_send",
    "reengage_buyers", "reply_mass_funnel", "send_followup",
    "send_mass_message", "send_welcome", "tip_request",
    "welcome_chatter_for_info",
)

_DEAD_TX_STATUSES = ("chargedback", "refund_pending")

# The chat header's nickname shape, "Name/City,Country/Age". Matched anywhere
# in a line, so a fan named mid-sentence ("why is Marco/Turin,Italy/38 quiet")
# still resolves. The three segments are not alike: the name and the age carry
# no spaces, the MIDDLE one does ("Dubai,United Arab Emirates"), which is why
# whitespace tokenising cannot find this and why the first segment is anchored
# to a word boundary — unanchored, the match swallows "why is " along with it.
# Requiring TWO slashes is what keeps an ordinary "and/or" out.
_NICK_SHAPE_RE = re.compile(
    r"(?<![^\s])[^\s/]{1,40}/[^/\n]{0,60}/[^\s/]{1,20}")

_HANDLE_RE = re.compile(r"@u(\d{4,})")
_LONG_INT_RE = re.compile(r"\d{5,}")


class Subject(NamedTuple):
    """WHO the question is about, answered before any evidence is read.

    Resolution is its own step with its own budget for a reason: it is three
    small indexed reads, while the evidence fetch is a dozen. Welded together,
    a slow evidence read threw away the resolved account too — so a question
    about blake came back describing blake, silently, because the fallback
    is the widget's scoped account."""

    account_id: str        # the scoped account unless the paste named another
    label: str | None      # its nickname, so BOTH blocks can name the model
    fan: Any               # the Fan row, or None
    note: str | None       # what the bot must ASK FOR, when we cannot resolve


class FanPack(NamedTuple):
    """The resolved subject of a question, plus how the answer degrades."""

    account_id: str        # the scoped account unless the paste named another
    label: str | None      # its nickname, so BOTH blocks can name the model
    block: str | None      # the pack, a lookup note, or None (no fan asked about)
    ok: bool               # False ⇒ the fetch failed; answer docs-only and say so


def _handles(question: str) -> list[int]:
    """The `@u…` fan ids in a paste. A handle is a FAN id BY DEFINITION, so
    `_resolve_account` needs them separately: letting one match an account id
    would redirect the whole answer to a model nobody named."""
    return [int(t) for t in _HANDLE_RE.findall(question) if int(t) < _INT64_WALL]


def _fan_id_candidates(question: str) -> list[int]:
    """Every long integer in the paste, HANDLES FIRST.

    `@u534895567` is the chat header's own way of writing a fan id, so it
    outranks a bare number in the same paste — which is just as likely to be
    the model's account id, a price or a year.

    Anything at or past SQLite's 64-bit integer wall is dropped rather than
    bound into a query: the driver raises OverflowError on it, and a stray
    twenty-digit run in a paste would take the whole pack down with it."""
    out: list[int] = []
    for value in _handles(question) + [int(t) for t
                                       in _LONG_INT_RE.findall(question)]:
        if value < _INT64_WALL and value not in out:
            out.append(value)
    return out


def _names_it(text: str, name: str) -> bool:
    """Is `name` in `text` as a whole token?

    `\\b` is the wrong boundary here: account nicknames end in digits
    ("blake"), where `\\b` fails to fire before a following digit and fires
    happily in the middle of "blake2".

    CASE-SENSITIVE, deliberately. Account nicknames are free text and nothing
    stops one being "Vault" or "Growth"; matched loosely, "how do I use the
    vault?" would silently retarget a docs question at whichever model happens
    to be named that, and answer about the wrong account. Operators paste the
    nickname verbatim out of the model picker, so exact spelling costs the real
    workflow nothing."""
    if len(name) < 3:
        return False                  # a two-character nickname matches everything
    return re.search(rf"(?<![A-Za-z0-9]){re.escape(name)}(?![A-Za-z0-9])",
                     text) is not None


async def _resolve_account(s, question: str, scoped: str) -> tuple[str, str]:
    """(account_id, label). The widget's scoped account is the DEFAULT, not the
    answer — operators paste the model name out of habit and expect it honoured.

    Only accounts the caller ALREADY OWNS are considered, so a stray number in a
    paste can never turn a help question into a 403. `ask()` still runs the
    result through `assert_account_owned`, which makes that a structural
    guarantee rather than a property of this filter."""
    owned = _actor_account_ids()
    q = select(Account.id, Account.nickname)
    if owned is not None:
        q = q.where(Account.id.in_(list(owned)))
    rows = (await s.execute(q)).all()

    label = next((n for i, n in rows if i == scoped and n), scoped)
    # Handles are excluded: the two id spaces are both just digits and do
    # collide, and a handle names a FAN.
    handles = set(_handles(question))
    written = {str(n) for n in _fan_id_candidates(question)
               if n not in handles}
    for aid, nick in rows:                     # an account id, spelled out
        if aid != scoped and aid in written:
            return aid, (nick or aid)
    for aid, nick in rows:                     # "blake · id 534895567"
        if aid != scoped and nick and _names_it(question, nick):
            return aid, nick
    return scoped, label


async def _resolve_fan(s, account_id: str, question: str) -> tuple[Any, str | None]:
    """(fan_row_or_None, note). `note` is what the bot must ASK FOR — this
    never guesses a fan.

    Ids first, then the name, because an id is unambiguous and a name is not.
    VERIFIED 2026-08-28: operators paste the chat header line and it IS
    `custom_nickname` verbatim ("Alexj/Dubai,United Arab Emirates/55"), so that
    column wins any tie."""
    ids = _fan_id_candidates(question)
    for fan_id in ids:
        fan = await s.get(Fan, (account_id, fan_id))
        if fan is not None:
            return fan, None

    shaped = [m.strip() for m in _NICK_SHAPE_RE.findall(question)]
    probes = shaped + [ln.strip() for ln in question.splitlines()]
    seen: set[str] = set()
    probes = [p for p in probes
              if 2 < len(p) <= ev.LABEL_CAP * 2
              and not (p.lower() in seen or seen.add(p.lower()))
              ][:_NICKNAME_PROBES]
    for probe in probes:
        low = probe.lower()
        # FIVE name columns, not three. The AMBIGUOUS branch below is only
        # reachable if every column an operator might be reading is searched:
        # Dana holds three men called Nick, but "Nick" is `of_display_name`
        # on one and `real_name` on another (whose `custom_nickname` reads
        # "Nick/Tela"), so matching three columns found ONE row, skipped the
        # guard, and the bot diagnosed the wrong man — telling the operator he
        # had spent $0.00 when the fan he meant is a $28 content payer.
        # Equality stays EXACT: a prefix match would pull in "Nickdiaz209" and,
        # generally, turn every short name into a crowd.
        rows = (await s.execute(
            select(Fan).where(
                Fan.account_id == account_id,
                or_(func.lower(Fan.custom_nickname) == low,
                    func.lower(Fan.of_display_name) == low,
                    func.lower(Fan.generated_nickname) == low,
                    func.lower(Fan.real_name) == low,
                    func.lower(Fan.of_username) == low.lstrip("@")),
            ).limit(4)
        )).scalars().all()
        if not rows:
            continue
        best = [f for f in rows
                if (f.custom_nickname or "").lower() == low] or rows
        if len(best) == 1:
            return best[0], None
        return None, (
            f'AMBIGUOUS: "{ev.clean_label(probe)}" matches {len(best)} fans on '
            f"this account (ids {', '.join(str(f.fan_id) for f in best)}). Ask "
            f"which fan id is meant — do not guess, and give no diagnosis until "
            f"you know.")

    # A NOT FOUND note tells the model to stop and ask, so it must only fire on
    # a number that is actually shaped like a fan id. "why is my 100000 cap not
    # working" is a docs question, and answering it with "ask me for the fan id"
    # would be the widget refusing to do its original job.
    fan_shaped = [n for n in ids if n >= _MIN_FAN_ID]
    if fan_shaped:
        return None, (
            f"NOT FOUND: no fan {fan_shaped[0]} exists on account "
            f"{account_id}. Ask for the fan id from the chat header, or for "
            f"the right model.")
    if shaped:
        # Unmistakably a pasted chat header, and nothing matched it. Silence
        # here is the worst outcome: the operator asked about a specific man
        # and would get a general answer with no hint that he was never found.
        return None, (
            f'NOT FOUND: no fan named "{ev.clean_label(shaped[0])}" on account '
            f"{account_id}. That nickname may have been regenerated since — "
            f"ask for the fan id from the chat header, or for the right model.")
    return None, None                       # no fan in the question at all


# ── Rendering ─────────────────────────────────────────────────────────


async def _fan_status(account_id: str, fan_id: int,
                      budget: float) -> dict[str, Any] | None:
    """The shared fan-status badge, on its own budget.

    Reused rather than re-derived: it evaluates ai_chatter's gates in run()'s
    order, first-hit-wins, and is documented read-only with no OF calls — so
    the bot's verdict and the chat drawer's badge cannot disagree. It is also
    the most expensive thing in this file (a whole-thread gather plus the
    cadence leash), so it gets a slice of the budget and degrades to nothing
    rather than costing the rest of the pack.

    `budget` is what the CALLER has left, not a constant: a fixed 0.8s inside a
    1.5s outer timeout is only honoured while the reads before it are fast, and
    the moment they are not the outer timeout fires first and drops the entire
    pack to save the one part that was meant to be optional."""
    import fans as fans_api

    try:
        status = await asyncio.wait_for(
            fans_api.get_fan_ai_status(account_id, fan_id), timeout=budget)
    except Exception:  # noqa: BLE001 — a missing badge is not a failed pack
        log.warning("assistant: fan status unavailable account=%s fan=%s",
                    account_id, fan_id, exc_info=True)
        return None
    return status


async def _resolve(scoped: str, question: str) -> Subject:
    """Who the paste is about. Three small reads, its own session, no engines."""
    async with get_session() as s:
        account_id, label = await _resolve_account(s, question, scoped)
        fan, note = await _resolve_fan(s, account_id, question)
    return Subject(account_id, label, fan, note)


async def _pack(subject: Subject) -> str:
    """The evidence for a RESOLVED fan, rendered — LOCAL DB ONLY.

    One session for this module's own reads, then the shared predicates, which
    each open their own. Sequential throughout: parallel sessions are this
    repo's documented footgun and nothing here is worth racing for."""
    import automation_rules_api                      # deferred: plugin loader
    from automation_registry import canonical_kind
    from automations._common import (content_payer_fans,
                                     should_skip_muted_creator)
    from automations.ai_chatter import _load_config, skip_reason_blocks

    account_id, label, fan = subject.account_id, subject.label, subject.fan
    fan_id = int(fan.fan_id)
    started = monotonic()
    async with get_session() as s:
        skip = await s.get(SkipList, (account_id, fan_id))
        banned = await s.get(Blacklist, fan_id)
        welcomed = await s.get(WelcomeSent, (account_id, fan_id))
        drip = await s.get(FollowupState, (account_id, fan_id))
        rules = [(canonical_kind(k), steps, bool(on)) for k, steps, on in (
            await s.execute(
                select(AutomationRule.kind, AutomationRule.steps_json,
                       AutomationRule.is_enabled)
                .where(AutomationRule.account_id == account_id))).all()]
        rule_kinds = {k for k, _s, _o in rules}
        # ppv_send reads its audience from here, not from its rule payload.
        ai_cfg = await s.get(AccountAiConfig, account_id)
        ppv_cfg = getattr(ai_cfg, "ppv_library_config_json", None)
        memberships = (await s.execute(
            select(List.name, List.kind, ListMember.added_at)
            .join(ListMember, ListMember.list_id == List.id)
            .where(List.account_id == account_id, ListMember.fan_id == fan_id)
            .order_by(List.name)
        )).all()
        # `created_at_text()` rather than the mapped column: ONE row holding ''
        # in created_at raises while the result set materialises and takes the
        # whole query with it (db/models.py records the prod incident). A help
        # bot that falls back to docs-only over a single dirty cell fails on
        # exactly the account someone is debugging.
        rows = (await s.execute(
            select(created_at_text(), Message.direction, Message.automation_kind,
                   Message.price_cents, Message.media_count, Message.mass_run_id,
                   Message.is_tip, Message.is_unsent, Message.message_id)
            .where(Message.account_id == account_id, Message.fan_id == fan_id)
            .order_by(Message.created_at.desc())
            .limit(_TIMELINE_SCAN)
        )).all()
        money = (await s.execute(
            select(Transaction.kind, func.count(Transaction.id),
                   func.sum(Transaction.amount_cents))
            .where(Transaction.account_id == account_id,
                   Transaction.fan_id == fan_id,
                   Transaction.status.notin_(_DEAD_TX_STATUSES))
            .group_by(Transaction.kind).order_by(Transaction.kind)
        )).all()
        # Off the RAW rows, not the collapsed ones: a priced or broadcast row
        # never folds anyway, and reading them here keeps this module free of
        # any call into the renderer.
        priced = [r.message_id for r in rows
                  if (r.price_cents or 0) > 0 and r.message_id]
        paid_ids = {int(m) for (m,) in (await s.execute(
            select(Transaction.message_id)
            .where(Transaction.account_id == account_id,
                   Transaction.message_id.in_(priced or [0]),
                   Transaction.status.notin_(_DEAD_TX_STATUSES))
        )).all() if m is not None}
        run_ids = [r.mass_run_id for r in rows if r.mass_run_id is not None]
        runs = {int(r.id): r for r in (await s.execute(
            select(MassRun).where(MassRun.id.in_(run_ids or [0]))
        )).scalars().all()}

    # Only for the kinds that actually put a broadcast on THIS thread: "what
    # would include him today" is a question about the mass he got, and every
    # other rule's audience is noise against a 4k cap.
    blasted = {r.automation_kind for r in rows
               if r.mass_run_id is not None and r.automation_kind}
    if not blasted:
        # THEN-vs-NOW needs somewhere to pivot TO. "Why did he get that mass
        # last Tuesday" with no surviving send row is the spec's own example,
        # and the honest answer is "unknown — here is what would include him
        # today", which needs today's audience even when nothing was found.
        blasted = {k for k, _s, on in rules if on and k in _MASS_KINDS}
    # WHICH blob each engine's audience lives in is the renderer's table to
    # answer, not a second `if kind == "ppv_send"` over here.
    audiences = [
        (k, ppv_cfg if ev.audience_home(k).from_account_config else steps, on)
        for k, steps, on in sorted(rules) if k in blasted][:ev.MAX_AUDIENCES]

    # THE SHARED PREDICATES — the single copy of each of their questions. A
    # second hand-rolled opinion here is the fans-badge bug written out in prose.
    cfg = await _load_config(account_id)
    is_payer = fan_id in await content_payer_fans(account_id, [fan_id])
    # `str(reason or "")` is the engine's own coercion (`_load_stop_lists`
    # builds `{fan_id: str(r[1] or "")}`), and it changes the verdict: a
    # skip_list row with reason NULL reaches the engine as "" and BLOCKS, while
    # a raw None reaches this predicate as "no row" and does not.
    blocks_seller = skip is not None and skip_reason_blocks(
        str(skip.reason or ""),
        engage_old_fans=bool(cfg.get("engage_old_fans")))
    left = TIMEOUT_S - (monotonic() - started) - _RENDER_RESERVE_S
    status = await _fan_status(account_id, fan_id,
                               max(0.05, min(STATUS_BUDGET_S, left)))

    live_kinds = {row["kind"] for row in automation_rules_api._kind_catalog()}
    missing = sorted((set(_FAN_FACING_KINDS) & live_kinds) - rule_kinds)

    return ev.render(ev.Evidence(
        account_id=account_id, label=label, fan=fan, skip=skip,
        blocks_seller=blocks_seller, banned=banned, welcomed=welcomed,
        drip=drip, memberships=memberships, rows=rows, paid_ids=paid_ids,
        runs=runs, money=money, missing=missing, cfg=cfg, is_payer=is_payer,
        muted_creator=should_skip_muted_creator(fan), status=status,
        audiences=audiences))


async def build(scoped: str, question: str) -> FanPack:
    """Resolve + fetch, on a budget. ANY failure degrades to a docs-only answer
    with an honest note — the 200-always contract never surfaces it as an
    error, and a bot that 500s when someone is debugging a silent fan is the
    failure mode this endpoint exists to avoid."""
    try:
        who = await asyncio.wait_for(_resolve(scoped, question),
                                     timeout=RESOLVE_TIMEOUT_S)
    except Exception:  # noqa: BLE001 — ANY failure degrades, never errors
        log.warning("assistant: fan resolution failed for account=%s", scoped,
                    exc_info=True)
        return FanPack(scoped, None, None, False)

    if who.fan is None:
        # No fan in the question, or one we refuse to guess at. Either way
        # there is no evidence to read, and the note (if any) IS the answer.
        return FanPack(who.account_id, who.label, None if who.note is None
                       else f"FAN LOOKUP:\n- {who.note}", True)
    try:
        block = await asyncio.wait_for(_pack(who), timeout=TIMEOUT_S)
    except Exception:  # noqa: BLE001 — ANY failure degrades, never errors
        log.warning("assistant: fan pack failed for account=%s fan=%s",
                    who.account_id, who.fan.fan_id, exc_info=True)
        # The SUBJECT survives the evidence failing. Falling back to the scoped
        # account here would answer about a different model than the one the
        # operator named, and say nothing about having done so.
        return FanPack(who.account_id, who.label, None, False)
    return FanPack(who.account_id, who.label, block, True)
