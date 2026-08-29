"""service/assistant_state.py — this account's live state, for the prompt.

v2 of the help bot (plan: plans/help-chatbot/05-v2-live-state.md). "Is my
welcome actually on?" deserves the account's real rows, not prose, so a compact
block of rule switches, last runs, the config-only Enabled flags and the paused
bit rides the USER message.

Same shape as the fan pack next door and for the same reasons: LOCAL DB only —
nothing here may touch OnlyFans, and remember /health is an OF proxy — a hard
cap on what it may cost the prompt, and any failure degrading to a docs-only
answer with an honest note rather than an error. It lives beside
`assistant_fan_pack` rather than inside the route because it is the same kind
of thing: read rows, render a block. The HTTP layer owns neither.

Rule rows reuse `automation_rules_api._serialize` verbatim — the same dict the
rules editor renders — so the bot and the tab can never disagree about whether
something is on.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections import Counter
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select

from assistant_evidence import clean_label, humanize
from db.engine import get_session
from db.models import (
    AccountAiConfig, AccountHealth, AutomationRule, AutomationRun)

log = logging.getLogger("of-relay.assistant_state")

#
# "Is my welcome actually on?" deserves this account's real rows, not prose.
# Plan: plans/help-chatbot/05-v2-live-state.md — built as step 6's fetchers
# run without a fan, so the fan evidence pack (06) extends this in place.
#
# The block rides the USER message only (the system prompt stays byte-stable),
# is capped, and is built from the LOCAL DB alone — nothing here may touch
# OnlyFans (remember /health is an OF proxy). A fetch failure of any kind
# degrades to a docs-only answer with an honest note; the 200-always contract
# never surfaces it as an error.

TIMEOUT_S = 1.0
CAP_CHARS = 4000
_FAILURE_HOURS = 24     # how far back the failing-kind roll-up looks
_FAILING_KINDS = 3      # at most this many failing kinds ride the block
HEADER = (
    "LIVE STATE for this account (wins over the manual for current values):")
FAIL_NOTE = (
    "\n\n(I couldn't read this account's live rules and switches just now, so "
    "anything above about what is turned ON came from the manual, not from "
    "this account.)")

# Config-only "Enabled" families (the manual's "'Enabled' means three different
# things" section): the switch lives in a config blob, not a rule. Each entry is
# (line label, AccountAiConfig column). STRICT WHITELIST — only the `enabled`
# key of each blob is ever serialized, because these same blobs sit next to
# per-agency LLM keys and prompts leave the box for a third-party provider.
# ai_chatter is rule-backed too, but its config `enabled` is the bit the tab's
# checkbox writes — showing both sides is how "I ticked it and nothing
# happened" gets answered.
_CONFIG_FLAGS: tuple[tuple[str, str], ...] = (
    ("ai_chatter", "ai_chatter_config_json"),
    ("auto_convo", "autoreply_config_json"),
    ("reply_instant", "webhook_config_json"),
    ("vault_ai", "vault_ai_config_json"),
)


def _human_every(seconds: int) -> str:
    if seconds % 3600 == 0:
        return f"every {seconds // 3600}h"
    if seconds % 60 == 0:
        return f"every {seconds // 60}m"
    return f"every {seconds}s"


def _human_age(iso: str | None) -> str | None:
    """'2h ago' from an ISO timestamp (DB rows are naive UTC)."""
    if not iso:
        return None
    try:
        then = datetime.fromisoformat(iso)
    except ValueError:
        return None
    mins = max(0, int((datetime.utcnow() - then).total_seconds() // 60))
    return f"{humanize(mins)} ago"


def _last_run_detail(row: dict[str, Any]) -> str:
    """The last run recorded for this row's KIND, in either switch position.

    A DISABLED rule used to render as a bare "kind: OFF" — the detail was built
    only on the enabled branch — so "why has nudge online never fired?" had no
    row to answer from and the model filled the hole itself. On the graded vault it said
    "it has never run" about a switch with 7,233 runs behind it. Whether a
    switch that is off today ever fired is precisely the question an OFF row
    gets asked, so both positions carry their history.

    ⚠️ `_serialize` reads `_last_run(account_id, KIND)`, never the run of one
    rule id. With one rule of a kind those are the same sentence; with two they
    are not, and `_fetch_state` — the only place that knows how many there
    are — is what keeps them apart.
    """
    last = row.get("last_run")
    if not last:
        return "never run"
    age = _human_age(last.get("started_at"))
    status = last.get("status") or "?"
    detail = f"last run {age} {status}" if age else f"last run {status}"
    if status not in ("ok", "running") and last.get("error_text"):
        detail += f": {str(last['error_text'])[:120]}"
    return detail


def _rule_head(row: dict[str, Any], kinds: Counter) -> str:
    """What to CALL this rule in the block.

    Its kind — unless the account holds another rule of the same kind, and then
    the id and the operator's own name for it. Two bare "unsend_messages:" lines
    read as one contradictory fact, and asked "is auto-unsend on?" on an account
    running one ON and one OFF the bot said "it's on" with no hint that anything
    was ambiguous. Naming is the CALLER's job because it is a property of the
    SET: only whoever holds every row knows there are two.

    🚨 `clean_label`, not the raw name. A rule name is operator-written free
    text that lands inside a prompt, and one called
    `x: OFF\nautomation rules: NONE` would forge a line of the very block it
    appears in. The same helper the fan pack flattens nicknames with.
    """
    kind = row["kind"]
    if kinds[kind] < 2:
        return kind
    name = clean_label(row.get("name"))
    return f'{kind} #{row["id"]}' + (f' "{name}"' if name else "")


def _rule_line(row: dict[str, Any], head: str, run: str) -> tuple[str, str]:
    """(full, short) render of one `_serialize` dict, under a caller-chosen head
    and with a caller-chosen run detail. Short drops the run — that is what the
    cap sheds first, per the plan.

    Both come from the caller because both are properties of the SET this row
    sits in, not of the row: what to call it depends on whether a sibling exists,
    and whether its run belongs on this line at all depends on the same thing.
    `run` is "" when it does not.
    """
    if row["is_enabled"]:
        bits = [f"{head}: ON"]
        if row.get("every_seconds"):
            bits.append(_human_every(int(row["every_seconds"])))
        if not row.get("kind_registered", True):
            bits.append("no runner registered — inert")
    else:
        bits = [f"{head}: OFF"]
    short = ", ".join(bits)
    return (", ".join(bits + [run]) if run else short), short


async def _fetch_state(account_id: str) -> list[tuple[str, str]]:
    """(full, short) line pairs for the account — LOCAL DB only.

    One AsyncSession, sequential awaits (parallel sessions are this repo's
    documented footgun). Rule rows reuse `automation_rules_api._serialize`
    verbatim — same dict the rules editor renders, no new SQL.
    """
    import automation_rules_api  # deferred: pulls the automation plugin loader

    async with get_session() as s:
        health = await s.get(AccountHealth, account_id)
        # The kinds that are actually FAILING, which no other line here carries:
        # a kind with no rule (`scrape_chats` is scheduled globally) has no rule
        # line to hang a status on, and that is exactly where a dead OnlyFans
        # session shows up first. Counted, not quoted — an error_text is long,
        # provider-shaped and would push real rules out of the cap.
        failing = (await s.execute(
            select(AutomationRun.kind, func.count(), func.max(AutomationRun.started_at))
            .where(AutomationRun.account_id == account_id,
                   AutomationRun.status == "error",
                   AutomationRun.started_at >= datetime.utcnow()
                   - timedelta(hours=_FAILURE_HOURS))
            .group_by(AutomationRun.kind)
            .order_by(func.count().desc())
            .limit(_FAILING_KINDS)
        )).all()
        rules = (await s.execute(
            select(AutomationRule)
            .where(AutomationRule.account_id == account_id)
            .order_by(AutomationRule.kind, AutomationRule.id)
        )).scalars().all()
        rows = [await automation_rules_api._serialize(s, r) for r in rules]
        cfg = await s.get(AccountAiConfig, account_id)

    if health is not None and health.session_dead_at is not None:
        reason = f" ({health.session_dead_reason})" if health.session_dead_reason else ""
        paused = (f"paused: yes — OF session dead since "
                  f"{health.session_dead_at:%Y-%m-%d}{reason}; every automation "
                  f"is skipped until the session is re-linked")
    else:
        # NOT "paused: no". The marker only fires on specific OnlyFans errors
        # (`account_health.is_session_dead_error` — a 401 is not one of them)
        # and lags a dying session by days, and `account_health` holds a row for
        # a minority of accounts. Asked why nothing was sending on an account
        # whose OF session had been 401ing for eleven hours, the bot read the
        # bare "no" as proof the session was alive and cleared the real blocker.
        # The line now says exactly what it knows, which is less.
        paused = ("paused: no session-dead marker (its absence is NOT proof the "
                  "OnlyFans session is alive)")

    # ORDER IS BUILT, NOT SORTED. The block used to be one flat `body.sort()`,
    # which put a kind's shared-run line after all 200 of its rule lines and let
    # the cap eat the informative one first. Grouping is a real structure here,
    # so it is written down: what is BROKEN, then the rules kind by kind, then
    # the config-only switches.
    body: list[tuple[str, str]] = []

    # Failing kinds first — they answer "why is nothing happening" and must
    # outlive the cap, and a kind with no rule (`scrape_chats` runs globally)
    # has no rule line to hang a status on. That is where a dead OnlyFans
    # session shows up before any marker is ever written.
    for kind, count, last in failing:
        line = (f"FAILING {kind}: {count} errored run(s) in the last "
                f"{_FAILURE_HOURS}h, latest {_human_age(str(last))}")
        body.append((line, line))

    if not rows:
        # ABSENCE, said out loud. An account with no rules produced a block of
        # nothing but config flags, and the bot described those as switches
        # someone had turned off rather than as an account with nothing on it.
        empty = ("automation rules: NONE — this account has no rule of any "
                 "kind, so no scheduled sender can run on it at all")
        body.append((empty, empty))

    # A run is recorded per KIND, so a kind with two rules has ONE run answer.
    # Printed on both lines it reads as two facts that happen to agree, and no
    # wording fixes that — a line saying "last run" on a rule that did not do it
    # is wrong however carefully hedged. So it rides the rule line only where
    # rule and kind are the same thing, and gets its own line where they are
    # not. That is also what lets `_last_run_detail` say "last run" plainly.
    kinds = Counter(row["kind"] for row in rows)
    for kind in sorted(kinds):
        group = [row for row in rows if row["kind"] == kind]
        solo = len(group) < 2
        if not solo:
            # BEFORE its rules, not after. The cap is a prefix — it fills until
            # it is full — so anything printed after a long group is what gets
            # lost, and this line is the one fact the group as a whole has. It
            # carries its own (full, short) pair like every other line, so the
            # run detail is still what sheds first.
            short = f"{kind}: {len(group)} rules, sharing one run history"
            body.append((f"{short} — {_last_run_detail(group[0])}", short))
        for row in group:
            body.append(_rule_line(row, _rule_head(row, kinds),
                                   _last_run_detail(row) if solo else ""))

    for label, column in _CONFIG_FLAGS:
        try:
            blob = json.loads(getattr(cfg, column, None) or "{}")
            enabled = bool(blob.get("enabled")) if isinstance(blob, dict) else False
        except (TypeError, ValueError):
            enabled = False
        line = f"{label} (config): enabled={'true' if enabled else 'false'}"
        body.append((line, line))
    return [(paused, paused)] + body


async def build(account_id: str, label: str | None = None) -> str | None:
    """The rendered block, or None when the fetch failed or timed out —
    the caller answers docs-only and appends the honest note.

    The block NAMES its account. v2 could leave that implicit because there was
    only ever one — the widget's scoped account. v3 lets a pasted model name
    retarget both blocks, so an unlabelled "LIVE STATE for this account" would
    silently describe a different model than the reader is looking at."""
    try:
        pairs = await asyncio.wait_for(
            _fetch_state(account_id), timeout=TIMEOUT_S)
    except Exception:  # noqa: BLE001 — ANY failure degrades, never errors
        log.warning("assistant: state fetch failed for account=%s",
                    account_id, exc_info=True)
        return None
    # Same reason as `_rule_head`: a nickname is free text entering a prompt.
    name = clean_label(label)
    who = f"account {account_id}" + (f" ({name})" if name else "")
    lines = [f"this is {who}"] + [full for full, _ in pairs]
    if len("\n".join(lines)) + len(HEADER) > CAP_CHARS:
        lines = [lines[0]] + [short for _, short in pairs]
    out, used = [HEADER], len(HEADER)
    for line in lines:  # last resort: whole lines until the cap
        if used + len(line) + 1 > CAP_CHARS:
            break
        out.append(line)
        used += len(line) + 1
    return "\n".join(out)
