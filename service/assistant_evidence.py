"""service/assistant_evidence.py — the fan evidence pack, rendered.

The PURE half of the help bot's v3 answer (plan: plans/help-chatbot/06-v3-fan-why.md).
`assistant_fan_pack` reads the rows; everything here turns them into the text
that rides the user message. Nothing here opens a session, runs an engine or
touches the network — hand it an `Evidence` and it hands back a block, which is
what makes every line of the pack testable without a DB. (It does import the
schema and two pure predicates from the engines, and that is the point: a
second copy of "does this skip row block" or "where does one reply end" is the
bug this feature was built to stop repeating.)

TWO TIERS, AND THE DIFFERENCE IS THE FEATURE. PROVEN lines are stored rows and
are stated as fact. CURRENT-STATE lines describe how things stand now and can
only support a ranked hypothesis.

The cap that keeps the block affordable does NOT shed by tier, and learning
that was the point: value is not sorted by section. `Thread` and `Current` cut
themselves into named groups, and `_fit` walks a five-rung ladder over them —
supporting figures, then thread-to-a-floor, then the knobs, then thread-to-the-
bone, then the three lines that answer the question on their own. The framing
sentences live in the header, where the cap cannot reach them at all.

NO FAN-WRITTEN TEXT EVER. Message bodies, notes, self-descriptions and the
fan's own OF identity fields stay out: directions, timestamps, engine
attribution and amounts only. A fan who writes "ignore all previous
instructions" would otherwise be dictating half the diagnosis. His
operator-set `custom_nickname` is the one deliberate exception — it is how the
operator named him in the first place, and echoing it back is what proves the
right thread was found — so it goes through `clean_label` rather than straight
into a prompt.
"""
from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import datetime
from typing import Any, NamedTuple

from db.models import (
    Blacklist, Fan, FollowupState, MassRun, SkipList, WelcomeSent, parse_ts,
)

CAP_CHARS = 4000
TIMELINE_LINES = 14          # rendered thread lines, after bubble runs collapse
LABEL_CAP = 60               # a nickname is a label, not a paragraph
MAX_LISTS = 8                # …so one row type cannot eat the whole pack
MAX_AUDIENCES = 3            # audience lines rendered, so the thread survives

_FAN_HEADER = (
    "FAN EVIDENCE PACK (wins over the manual for this fan\'s facts).\n"
    "Two kinds of line follow, and the difference is the whole point.\n"
    "PROVEN = a stored row. State it as fact, with its stored reason and date.\n"
    "CURRENT-STATE = how things stand right now. It supports a RANKED "
    "HYPOTHESIS, never proof of what happened in the past.\n"
    "There is NO per-tick decision log in this product. Whether an engine "
    "looked at this fan on a given tick and chose silence is an INFERENCE from "
    "the rows below, and must be stated as one.")

# CONTENT money vs SUBSCRIPTION money, for the per-kind SPLIT only. The verdict
# comes from `content_payer_fans`; restating the kinds here is presentation, and
# deciding with them would be the second copy this feature refuses to write.
CONTENT_TX_KINDS = ("ppv_message", "ppv_post", "tip")

# ai_chatter knobs worth showing against this fan's own numbers. STRICT
# WHITELIST, for v2's reason — the blob is operator-writable and prompts leave
# the box for a third-party provider.
_AI_CHATTER_KNOBS: tuple[tuple[str, str], ...] = (
    ("enabled", "the master switch"),
    ("mode", "always | backup"),
    ("payers_only", "the payer floor"),
    ("engage_old_fans", "lifts the old_fan_pre_ai skip"),
    ("max_fans_per_tick", "fans per tick"),
    ("resume_after_manual_hours", "hold after a human writes"),
)


# ── The typed record everything below reads ──────────────────────────

class AudienceHome(NamedTuple):
    """Where an engine keeps its audience, and what to call that place.

    THE one answer to that question. The differences are irreducible facts
    about the codebase rather than special cases this module invented —
    reading the rule payload for `ppv_send` reports "nothing set" on a fully
    configured sender, and reading the top level for `mass_premade` does the
    same — so the knowledge is a table, and the fetcher asks the table rather
    than carrying its own `if kind == "ppv_send"`."""

    keys: frozenset[str]
    where: str
    # True when the config lives on the ACCOUNT rather than in the rule row —
    # which changes what the fetcher has to read, not just what we call it.
    from_account_config: bool = False
    # A payload that nests its audience one level down, and the key it is under.
    nested_under: str | None = None


# The audience keys a mass rule carries in its `steps_json` payload. WHITELIST,
# both for the secrets reason and because a payload also holds message text,
# vault ids and price ladders — none of which answers "who is in the audience".
# Names verified 2026-08-28 against send_mass_message / mass_nudge /
# online_blast; a key absent from a payload means the engine's own default, not
# "off", and the pack says so rather than implying the operator set it.
_RULE_AUDIENCE = AudienceHome(frozenset({
    "excluded_user_lists", "excluded_users", "exclude_inbound_hours",
    "exclude_last_chat_hours", "exclude_list_ids", "exclude_replied_hours",
    "fan_ids", "filters", "included_users", "list_ids", "max_online",
    "online_only", "recent_chat_hours", "recent_chat_limit", "unread_limit",
    "user_lists",
}), "the rule payload")

# ppv_send keeps its audience in the account-level PPV Library blob instead.
_AUDIENCE_HOMES: dict[str, AudienceHome] = {
    "ppv_send": AudienceHome(frozenset({
        "broadcast_exclude_lists", "broadcast_lists",
        "max_lifetime_spend_cents", "pause_hours", "price_max_cents",
        "price_min_cents", "reach_all",
    }), "the PPV Library config", from_account_config=True),
    # mass_premade's payload is a LIST of premade messages, each carrying its
    # own audience; the top level holds none at all.
    "mass_premade": AudienceHome(
        _RULE_AUDIENCE.keys, "the first premade message",
        nested_under="messages"),
}


def audience_home(kind: str) -> AudienceHome:
    """Where `kind` keeps its audience. The fetcher asks this to decide WHICH
    blob to hand back, so the two of us cannot disagree about ppv_send."""
    return _AUDIENCE_HOMES.get(kind, _RULE_AUDIENCE)


class Beat(NamedTuple):
    """One rendered thread row, already collapsed if it was a bubble run."""

    ts: datetime | None
    direction: str
    kind: str | None
    price_cents: int
    media_count: int
    mass_run_id: int | None
    is_tip: bool
    is_unsent: bool
    message_id: int | None
    count: int
    through: datetime | None


class Evidence(NamedTuple):
    """Everything the fetcher found, in one typed record.

    It exists so the renderers take ONE argument instead of nineteen. The
    fetcher shreds a session into these fields once; every function below reads
    them by name, and adding a fact to the pack is a field plus a line rather
    than another parameter threaded through two signatures.

    EVERY FIELD IS RAW. Not one of them holds a rendered string, and that is
    what makes the seam real: the fetcher never calls back into this module to
    pre-format anything, so "who reads the database" and "who writes the
    prose" is a question with one answer per module rather than two."""

    account_id: str
    label: str | None                        # account nickname, for the header
    fan: Fan
    skip: SkipList | None
    blocks_seller: bool                      # skip_reason_blocks on that row
    banned: Blacklist | None
    welcomed: WelcomeSent | None
    drip: FollowupState | None
    memberships: list[tuple[str, str, datetime]]   # list name, kind, added_at
    rows: list                               # raw thread rows, newest first
    paid_ids: set[int]                       # message_ids with a purchase row
    runs: dict[int, MassRun]                 # mass_run_id -> the run row
    money: list[tuple[str, int, int]]        # tx kind, count, cents
    missing: list[str]                       # scheduled kinds with NO rule
    cfg: dict[str, Any]                      # the resolved ai_chatter config
    is_payer: bool                           # content_payer_fans's verdict
    muted_creator: bool                      # should_skip_muted_creator's
    status: dict[str, Any] | None            # get_fan_ai_status, or None
    # (kind, its audience config as stored, rule enabled) for the mass engines
    # worth describing. Raw, like everything else here.
    audiences: list[tuple[str, str | None, bool]]


# ── Formatting primitives ─────────────────────────────────────────────

def humanize(minutes: int) -> str:
    """A minute count as the product says it: 45m, 3h, 2d.

    Shared with v2's `_human_age`, which is the only reason it is a function:
    the two callers differ in whether the span is behind us or ahead of us, and
    that is a sentence, not a bucketing rule."""
    if minutes < 60:
        return f"{minutes}m"
    if minutes < 48 * 60:
        return f"{minutes // 60}h"
    return f"{minutes // (24 * 60)}d"


def clean_label(value: str | None) -> str:
    """A name rendered as a LABEL: one line, no control characters, short."""
    flat = " ".join((value or "").split())
    return flat[:LABEL_CAP]


def usd(cents: Any) -> str:
    return f"${int(cents or 0) / 100:,.2f}"


def at(ts: datetime | None) -> str:
    """'2026-08-27 00:05:21 (36h ago)' — every mutable fact carries its age.

    FUTURE timestamps get their own branch, and they must: v2's `_human_age`
    was only ever fed `last_run.started_at`, so it clamps at zero, and v3 is
    the first caller to hand it a deadline. Unclamped, a fan resting under a
    SEVEN-DAY quarantine would read "(0m ago)" — the exact opposite of what an
    operator needs to know about him."""
    if ts is None:
        return "unknown time"
    stamp = f"{ts:%Y-%m-%d %H:%M:%S}"
    now = datetime.utcnow()
    if ts > now:
        return f"{stamp} (in {humanize(int((ts - now).total_seconds() // 60))})"
    return f"{stamp} ({humanize(max(0, int((now - ts).total_seconds() // 60)))} ago)"


# ── One line at a time ────────────────────────────────────────────────

def _folds(last: Beat, row: Any, ts: datetime | None, window) -> bool:
    """Is `row` another bubble of the beat already on the list?

    Every clause is a rule about what a bubble ISN'T, and they are spelled out
    rather than summarised because each one has a reason:
      • same direction and same engine — otherwise it is a different speaker;
      • neither priced nor tipped — the amount IS the evidence;
      • neither on a broadcast — a run id is what answers "why this mass";
      • neither carrying media — a photo is an event, and folding "he sent a
        picture" into the text row after it hides why he got the reply he got;
      • same unsent state, and inside the engine's own bubble window."""
    return (last.direction == row.direction
            and last.kind == row.automation_kind
            and last.price_cents == 0 and int(row.price_cents or 0) == 0
            and last.mass_run_id is None and row.mass_run_id is None
            and not last.is_tip and not row.is_tip
            and not last.media_count and not row.media_count
            and last.is_unsent == bool(row.is_unsent)
            and last.ts is not None and ts is not None
            and (last.ts - ts) <= window)


def collapse(rows: Iterable[Any]) -> list[Beat]:
    """Fold a run of consecutive same-engine bubbles into one beat.

    A four-bubble ai_chatter reply is ONE event to anyone reading the thread,
    and spending four lines of a capped pack on it costs the older history that
    actually answers the question. Priced, tipped and broadcast rows never fold
    — the amount and the run id are themselves the evidence.

    The window is the ENGINE's `_BUBBLE_WINDOW`, not a number picked here: it is
    already the one definition of "these rows are the same reply", and the
    cadence counters are built on it. A second opinion about where one reply
    ends would make the pack say "four bubbles" where the bot counted five."""
    from automations.ai_chatter import _BUBBLE_WINDOW

    beats: list[Beat] = []
    for row in rows:
        ts = parse_ts(row.created_at)
        last = beats[-1] if beats else None
        if last is not None and _folds(last, row, ts, _BUBBLE_WINDOW):
            # Rows arrive newest-first, so `ts` is the older edge of the run.
            beats[-1] = last._replace(ts=ts, count=last.count + 1)
            continue
        beats.append(Beat(
            ts=ts, direction=row.direction, kind=row.automation_kind,
            price_cents=int(row.price_cents or 0),
            media_count=int(row.media_count or 0), mass_run_id=row.mass_run_id,
            is_tip=bool(row.is_tip), is_unsent=bool(row.is_unsent),
            message_id=row.message_id, count=1, through=ts))
    return beats


def beat_line(b: Beat, paid_ids: set[int],
              runs: dict[int, MassRun], fan_id: int | None = None) -> str:
    """One timeline line — directions, times, engines and amounts, never a word
    he wrote or a word we wrote back.

    `fan_id` reaches `mass_summary` so a broadcast's stored recipient list can
    be read as this fan's membership of it rather than as a count of strangers."""
    bits = [at(b.ts), {"in": "IN ", "out": "OUT"}.get(b.direction, "SYS")]
    bits.append(b.kind or ("fan" if b.direction == "in" else "human send"))
    if b.count > 1 and b.through is not None:
        bits.append(f"x{b.count} bubbles through {b.through:%H:%M:%S}")
    if b.is_tip:
        bits.append("TIP")
    if b.price_cents:
        bits.append(usd(b.price_cents))
    if b.media_count:
        bits.append(f"{b.media_count} media")
    if b.price_cents:
        # PURCHASE TRUTH IS THE LEDGER. `messages.is_paid` carries it on ~1.4%
        # of priced sends, so reading it here would report almost every real
        # buyer as a man who never paid.
        bits.append("PURCHASED" if b.message_id in paid_ids
                    else "no LINKED purchase row")
    if b.mass_run_id is not None:
        run = runs.get(b.mass_run_id)
        bits.append(f"mass_run {b.mass_run_id}"
                    + (f" [{mass_summary(run, fan_id)}]" if run is not None else ""))
    if b.is_unsent:
        bits.append("UNSENT since")
    return "  " + "  ".join(bits)


# The audience keys that hold THE RECIPIENT LIST ITSELF rather than a filter.
#
# 🚨 They do NOT mean "someone picked these people", and a first attempt at this
# line said they did — it rendered them "INCLUDE-ONLY, N named ids", which told
# the model a human had hand-chosen the audience. Checked against every stored
# row: of 2,028 runs carrying a non-empty `included_users`, 2,023 are
# `ppv_send`, `recipient_count == len(included_users)` in **2,028 of 2,028**,
# and **none** co-occurs with a filter key. The column is always the audience
# the engine had already RESOLVED — `ppv_send` writes a price cell (the roster
# bucketed by spend and recency), `send_mass_message` writes the list after
# expansion and exclusions. So a 1,514-fan roster blast was being presented as
# fourteen hundred hand-picked people.
#
# What the row can honestly answer is the question actually being asked, which
# is never "who else was in it": it is "why HIM". So these keys render as
# MEMBERSHIP — is this fan in the list or not — and the count comes along only
# as scale.
_RECIPIENT_LIST_KEYS = frozenset({"included_users", "fan_ids"})
_EXCLUSION_KEYS = frozenset({"excluded_users"})


def _is_id_list(value: Any, key: str = "") -> bool:
    """Is this a list of IDS — by SHAPE, or by a key that always holds them?

    Shape first, which is this repo's documented lesson and the difference
    between a guard and a coincidence: a list of ints under a key nobody thought
    of is still a list of identities, and the audience blob is engine-authored
    JSON that grows without asking. `bool` is excluded because it is an `int` in
    Python and `[true]` is a flag, not an id.

    🚨 UNION, not replacement. Shape alone let string ids through: a rule payload
    is operator-written and `send_mass_message._int_list` exists precisely
    because `"included_users": ["123", "456"]` is a shape people type, and the
    coercion happens in the SENDER, long after this renderer has already put the
    raw list in a prompt. The keys below hold identities whatever type they
    arrive as, so a non-empty list under one of them is redacted on the strength
    of its name too."""
    if not isinstance(value, list) or not value:
        return False
    if key in _RECIPIENT_LIST_KEYS or key in _EXCLUSION_KEYS:
        return True
    return all(isinstance(v, int) and not isinstance(v, bool) for v in value)


def _holds(ids: list, fan_id: int) -> bool:
    """Is this fan in that id list, whatever type the ids arrived as?

    `123 in ["123"]` is False in Python, and a rule payload is operator-written
    JSON where string ids are an ordinary shape (`send_mass_message._int_list`
    exists for exactly that). Compared naively, a fan who WAS on the list gets
    told he was not — a confident wrong answer, which is the whole thing this
    pack is built to avoid. Comparison is on the string form, so it is right for
    both types and never raises on a ragged list."""
    want = str(fan_id)
    return any(str(i) == want for i in ids)


def _knob(key: str, value: Any, fan_id: int | None = None) -> str:
    """`key=value`, with an id LIST rendered as a count — and, for the two keys
    that hold the recipient list, as THIS FAN'S MEMBERSHIP of it.

    Two audience renderers need this and they must not disagree: a raw list of
    recipient ids is long, is other fans' identities, and belongs in no prompt.
    A count of four was as raw as a count of four hundred — the old length test
    only hid the long ones.

    The pack is built for exactly one fan, so a bare count answers a question
    nobody asked. `fan_id` turns the same row into the answer: he is on the list
    the run went out to, or he is not."""
    if _is_id_list(value, key):
        n = len(value)
        if fan_id is not None and key in _RECIPIENT_LIST_KEYS:
            where = ("THIS FAN IS ON IT" if _holds(value, fan_id)
                     else "THIS FAN IS NOT ON IT")
            return f"{key}={n} recipients the run had already resolved — {where}"
        if fan_id is not None and key in _EXCLUSION_KEYS:
            where = "including this fan" if _holds(value, fan_id) else "not this fan"
            return f"{key}={n} ids held back ({where})"
        return f"{key}={n} ids"
    if isinstance(value, list) and len(value) > 4:
        return f"{key}=[{len(value)} entries]"
    return f"{key}={json.dumps(value)}"


_KNOB_BUDGET = 220           # chars of audience knobs one run summary may carry


def _fit_knobs(audience: dict, fan_id: int | None = None) -> str:
    """The audience knobs, WHOLE — a cap may drop one, never slice one.

    🚨 This used to be `", ".join(...)[:220]`, and a slice through a joined
    string lands mid-VALUE: `min_cents=5000` becomes `min_cents=50`, and the
    pack presents it as a stored fact the answer must state as true. A cap may
    drop a knob; it may never invent a number. What was dropped is said out
    loud, because a silently short list reads as a complete one."""
    # EVERY KEY, empties included. A pass here once skipped them as "the
    # engine's default, not a setting", on the theory that they were crowding
    # the budget. Replayed over all 3,695 stored blobs through this very
    # renderer, the longest audience line is 126 characters against a budget of
    # 220 and NOTHING has ever been dropped — the ration was imaginary, and it
    # was paid for with the one distinction that matters here: `included_users`
    # is present-and-empty in 1,387 of those blobs (37.5%), and "the engine
    # resolved nobody onto this run by name" is a different fact from "this key
    # was never written". The budget stays as a guard against a blob nobody has
    # written yet, not as a live tax on the ones that exist.
    kept, used, dropped = [], 0, 0
    for k, v in sorted(audience.items()):
        knob = _knob(k, v, fan_id)
        if kept and used + len(knob) + 2 > _KNOB_BUDGET:
            dropped += 1
            continue
        kept.append(knob)
        used += len(knob) + 2
    if dropped:
        kept.append(f"(+{dropped} more knob(s) not shown)")
    return ", ".join(kept) if kept else "nothing set — engine defaults throughout"


def mass_summary(run: MassRun, fan_id: int | None = None) -> str:
    """The broadcast that carried a message, as its own row records it.

    `mass_runs.audience_filter` is the audience AT SEND TIME, which is the only
    honest answer to "why did HE get this one" — the rule's audience config
    today describes who would be included NOW, and the two drift apart daily.
    Id LISTS are rendered as counts: they are long, they are other fans' ids,
    and neither belongs in a prompt.

    ⚠️ WHAT THE ROW HOLDS VARIES BY KIND, and the line must not overclaim. Where
    `included_users` is populated it is the audience the engine had already
    RESOLVED, so "he was on the list" is the whole honest answer and inferring
    that somebody chose him is not. But 1,387 of 3,699 rows store no recipient
    list at all — only the FILTER (`user_lists`, `list_ids`) — because a
    list-or-filter send is resolved by OnlyFans. There the honest answer is that
    the stored audience IS the filter, and who it expanded to was never written
    down. Each key says which it is; the header says neither."""
    # "0 recipients" is not a headcount, it is a MISSING one — and it is the
    # common case: 1,390 of 3,699 stored runs carry it, including every one of
    # the 1,055 `send_mass_message` rows, because a list-or-filter send hands
    # its audience to OnlyFans and never enumerates it locally. Printed as a
    # number it reads "this went to nobody", on a beat sitting in the thread of
    # a fan who demonstrably received it.
    bits = [run.automation_kind or "manual broadcast"]
    bits.append(f"{run.recipient_count} recipients" if run.recipient_count
                else "recipient count not recorded — a list/filter send is "
                     "resolved by OnlyFans, so we never counted it here")
    try:
        audience = json.loads(run.audience_filter or "{}")
    except (TypeError, ValueError):
        audience = None
    if isinstance(audience, dict) and audience:
        # NEUTRAL LABEL. It once read "(WHO it reached, not the filter that
        # produced them)", which is true of `included_users` and false of every
        # other key in the block — and on the 1,387 zero-count rows the only
        # populated knob IS the filter (`user_lists=["fans","following"]`), so
        # the header asserted the opposite of its own contents. The WHO claim
        # belongs on the recipient key, where `_knob` already makes it and only
        # when there is a list to make it about.
        bits.append("audience at send time: " + _fit_knobs(audience, fan_id))
    return "; ".join(bits)


def audience_now(kind: str, raw: str | None, enabled: bool) -> str:
    """A mass engine's audience AS CONFIGURED TODAY.

    The paired line to `_mass_summary`, and the two answer different questions:
    that one says who was in the audience when the send actually went out, this
    one says who would be in it now. Asked "why did he get that mass last
    Tuesday", the bot needs both — the stored filter to answer it, and this to
    pivot to "and here is what would include him today".

    The line SAYS which of the two it is, in capitals, because the label is the
    only thing separating them once they are prose in the same prompt: given
    both, the bot answered "he got it because the engine reaches everyone" —
    today's switch — about a run whose own row recorded five named recipients."""
    home = audience_home(kind)
    where = home.where
    try:
        payload = json.loads(raw or "{}")
    except (TypeError, ValueError):
        payload = None
    if home.nested_under and isinstance(payload, dict):
        first = (payload.get(home.nested_under) or [None])[0]
        payload = first if isinstance(first, dict) else None
    if not isinstance(payload, dict):
        return (f"- {kind} audience as configured today: {where} is unreadable "
                f"(rule is {'on' if enabled else 'off'}).")
    parts = [_knob(k, payload[k]) for k in sorted(home.keys & set(payload))]
    return (f"- {kind} audience as configured today, from {where} "
            f"({'rule ON' if enabled else 'rule OFF'}): "
            + (", ".join(parts) if parts else
               "nothing set — every audience knob is on its engine default."))


def skip_line(skip: SkipList | None, blocks_seller: bool) -> str:
    """The skip_list row, verbatim — and WHOSE thread it actually closes.

    Two different questions, and answering only one of them is how a lifted row
    gets named as the blocker. `skip_reason_blocks` answers it for AI Seller;
    `OPERATOR_STOP_REASONS` is what the always-answer senders go by."""
    from automations._common import OPERATOR_STOP_REASONS

    if skip is None:
        return "- skip_list: no row."
    # OPERATOR_STOP_REASONS, not HARD_SKIP_REASONS: the two differ by
    # `ladder_stop`, and the always-answer senders (tip_reward, make_right's
    # apology) gate on the narrower set — so calling a ladder_stop row a hard
    # restrict for everyone would name a blocker that is not blocking them.
    stopped = skip.reason in OPERATOR_STOP_REASONS
    if blocks_seller:
        verdict = ("this DOES close the thread to AI Seller"
                   + (", and it is an OPERATOR restrict, which the always-answer "
                      "senders honour too."
                      if stopped else ", and to the other skip-list-aware senders."))
    else:
        verdict = ("skip_reason_blocks exempts this reason, so it does NOT block "
                   "AI SELLER. Do not name it as the reason AI Seller is quiet.")
    return (f"- skip_list: reason '{skip.reason}', added {at(skip.added_at)} "
            f"— {verdict}\n"
            "  CAREFUL: that verdict is about AI SELLER ONLY. Follow up quiet "
            "fans and Nudge Online read the skip list with NO reason filter, so "
            "ANY row here — exempt or not — stops both of them.")


def status_lines(status: dict[str, Any] | None) -> tuple[list[str], list[str]]:
    """(verdict, detail) from the fans-drawer AI status.

    Same first-hit-wins gate order `ai_chatter.run()` uses, so this cannot
    drift from the engine — and it is the badge the operator can open the chat
    and check. Split in two because the cap sheds from the end: the badge is
    the single most decisive line in the pack and must outlive the reply-budget
    numbers that support it. `None` means the read failed, which is itself
    something the answer has to admit rather than quietly omit."""
    if status is None:
        # The read failed or ran out of budget. Say so — an absent badge that
        # looks like an absent problem is worse than no badge at all.
        return (["- AI standing RIGHT NOW: unavailable (the fan-status read "
                 "failed). Say so rather than inferring the badge."], [])
    badge = f"state={status.get('state')} \"{status.get('label')}\""
    detail = str(status.get("detail") or "").strip().rstrip(".")
    if detail:
        badge += f" — {detail}"
    if status.get("until"):
        badge += f" (until {status['until']})"
    verdict = [f"- AI standing RIGHT NOW, from the product's own fan-status "
               f"badge (the first gate that fires wins, in the engine's "
               f"order): {badge}. Owning engine: {status.get('engine')}."]
    if status.get("graduated"):
        verdict.append(f"- graduation marker: '{status['graduated']}' — he LEFT "
                       "the info-gather loop. That is a hand-off, not a block.")
    out: list[str] = []

    cadence = status.get("cadence") or {}
    # `enabled` alone is not enough: fans.py returns a bare {"enabled": True}
    # when the thread has no messages to measure, and rendering that prints
    # "burst None/None", which looks like a broken cap rather than no data.
    if cadence.get("enabled") and cadence.get("tier"):
        daily = cadence.get("daily") or {}
        out.append(
            "- reply budget: burst "
            f"{cadence.get('used')}/{cadence.get('cap')} (tier "
            f"{cadence.get('tier')}, stopped={cadence.get('stopped')})"
            + (f"; daily quota {daily.get('used')}/"
               f"{daily.get('quota') or 'uncapped'}, "
               f"held={daily.get('held')}, enforced={daily.get('enforced')} "
               f"(the gate's own verdict: {daily.get('reason')})"
               if daily else "")
            + ".")
    nxt = status.get("next_action")
    out.append(f"- next job already queued for this fan: {nxt['kind']} at "
               f"{nxt['at']}." if nxt else
               "- next job already queued for this fan: none pending.")
    llm = status.get("llm") or {}
    out.append(
        f"- last LLM call about this fan: {llm.get('last_at') or 'never'}"
        + (f" ({llm.get('purpose')})" if llm.get("purpose") else "")
        + f"; {llm.get('calls_24h', 0)} calls in the last 24h. A call means she "
          "generated something for him; its ABSENCE is not proof she never "
          "considered him.")
    # "She is talking but she will not SELL to him" is its own question, and
    # these three lines are the only place it is answered.
    ladder = status.get("ladder") or {}
    ask = status.get("open_ask")
    gate = status.get("gate") or {}
    sell = [f"ladder {ladder.get('status', 'idle')} at rung "
            f"{ladder.get('rung', 0)}"]
    if ask:
        # A HUMAN chatter's ask comes back with price_cents=None — the amount is
        # genuinely unknown, and `usd(None)` would print "$0.00", which reads
        # as a free offer rather than as a missing number.
        amount = (f"of {usd(ask['price_cents'])}"
                  if ask.get("price_cents") is not None
                  else "of an unrecorded amount")
        sell.append(f"a LIVE unpaid ask {amount} from {ask.get('at')}, placed "
                    f"by the {ask.get('by')}")
    if gate.get("enabled"):
        sell.append("the offer gate says "
                    + ("yes" if gate.get("ok") else
                       f"NO ({gate.get('why')})"))
    out.append("- selling: " + "; ".join(sell) + ".")

    spend7 = status.get("spend_7d") or {}
    # `paid_cents` is only COMPUTED when the offer gate is on (fans.py reads
    # `_paid_cents_7d(...) if gate_on else 0`), so on the default account this
    # would print "$0.00 paid" about a man who has paid plenty.
    if spend7.get("cap_cents") and gate.get("enabled"):
        out.append(
            f"- 7-day spend brake: {usd(spend7.get('paid_cents'))} paid of a "
            f"{usd(spend7['cap_cents'])} cap, capped={spend7.get('capped')}. "
            "Past the cap he is not silenced — he stops being CHARGED (she "
            "goes companion for the window).")
    return verdict, out


# ── The two tiers, and the cap ────────────────────────────────────────

def proven_lines(ev: Evidence) -> list[str]:
    """The stored rows. Written first, and the LAST thing the cap gives up.

    The order is the order they are cut in, so a row whose absence would change
    the answer belongs early. `skip_list` leads because a lifted row named as
    the blocker is the single wrong answer this feature exists to prevent."""
    out = [skip_line(ev.skip, ev.blocks_seller)]
    out.append("- blacklist: no row." if ev.banned is None else
               f"- blacklist: BANNED GLOBALLY, reason '{ev.banned.reason}', "
               f"added {at(ev.banned.added_at)}. This applies across every "
               f"model.")
    if ev.memberships:
        # Bounded: a fan on thirty lists would otherwise push the thread — the
        # row that usually answers the question — out of the pack entirely.
        for name, kind, added in ev.memberships[:MAX_LISTS]:
            out.append(f'- list membership: "{clean_label(name)}" (kind '
                       f"{kind}), added {at(added)}.")
        if len(ev.memberships) > MAX_LISTS:
            out.append(f"- list membership: and "
                       f"{len(ev.memberships) - MAX_LISTS} more, not shown.")
    else:
        out.append("- list membership: on no local list — not on an exclude "
                   "list, and not inside an include-only audience folder.")
    out.append("- welcome_sent: no row; he was never sent the welcome."
               if ev.welcomed is None else
               f"- welcome_sent: {at(ev.welcomed.sent_at)}.")
    out.append(
        "- followup_state: no row; the follow-up drip has never tracked him."
        if ev.drip is None else
        f"- followup_state: phase {ev.drip.phase}, cycle {ev.drip.cycle}, "
        f"completed={bool(ev.drip.completed)}, updated {at(ev.drip.updated_at)}.")
    out.append(
        "- ABSENCE IS EVIDENCE — this account has NO rule at all of these "
        "scheduled fan-facing kinds, so no run of one can ever reach him here: "
        + ", ".join(ev.missing) + "."
        if ev.missing else
        "- every fan-facing kind has a rule row on this account; the LIVE "
        "STATE block says which of them are switched on.")
    out += _money_lines(ev)
    return out


def _money_lines(ev: Evidence) -> list[str]:
    """The ledger split — and, when it disagrees with the payer gate, why.

    The gate is a UNION of the ledger and the messages table, and 40% of known
    content buyers exist in only one of them. Without the second line the pack
    prints "content money $0.00" directly above "CONTENT payer: YES" and reads
    as though it contradicts itself."""
    content = sum(int(c or 0) for k, _n, c in ev.money if k in CONTENT_TX_KINDS)
    other = sum(int(c or 0) for k, _n, c in ev.money if k not in CONTENT_TX_KINDS)
    if not ev.money:
        out = ["- transactions ledger: no rows at all."]
    else:
        out = ["- transactions ledger (a priced send is PURCHASED only if it "
               "has a row here — messages.is_paid carries it on ~1.4% of "
               "them): "
               + "; ".join(f"{n} x {k} {usd(c)}" for k, n, c in ev.money)
               + f". Content money {usd(content)}, subscription/other "
                 f"{usd(other)}."]
    if ev.is_payer and not content:
        out.append(
            "  …and the ledger is not the whole story: he clears the payer "
            "gate on a purchase recorded in `messages` (an unlock or a tip) "
            "that never got a ledger row. Both count; the gate reads both.")
    return out


class Current(NamedTuple):
    """The CURRENT-STATE section, split at the seams the cap cuts on.

    Three tiers, most decisive first. `essential` is the badge, whose turn it
    is and whether he clears the payer floor — between them they answer the
    question. `core` is what he is measured against. `figures` is the
    supporting arithmetic, and it is the first thing to go.

    Named groups rather than indexes into one flat list: `_fit` used to be
    handed two derived integers describing a shape it did not build, which is
    a layout invariant living in two files at once."""

    head: list[str]
    essential: list[str]
    core: list[str]
    figures: list[str]


class Thread(NamedTuple):
    """The thread section, split at the seam the cap cuts on.

    `beats` is the only shed-able part; `head` and `tail` frame it. Keeping
    them apart is what lets `_fit` pop from a list of beats instead of
    reverse-engineering the layout from an index and a count."""

    head: list[str]
    beats: list[str]
    tail: list[str]


def thread_lines(ev: Evidence) -> Thread:
    """The thread, plus the two things its silence does NOT prove.

    Both caveats are load-bearing. A ledger row can land without a message_id
    (the attribution linker races the purchase), and Mass Nudge / Blast Online
    hand their audience to OnlyFans and write no per-fan row at all — so an
    absence here is not evidence, and the pack has to say which absences."""
    shown = collapse(ev.rows)[:TIMELINE_LINES]
    # ⚠️ NO "this thread covers X to Y" LINE HERE, though it is the obvious fix
    # for a dated question and was tried. `thread_lines` sees the beats it
    # SELECTED; `_fit` sheds them afterwards, down to THREAD_FLOOR. A span
    # computed here therefore names days whose rows the pack then drops — on
    # Alexj it claimed 08-23 while only the 27th and 28th survived, telling the
    # model a date was in range when the evidence for it was gone. A false
    # range is worse than no range, and the honest version has to be built where
    # the shedding happens, not here.
    head = ["- thread, newest first. Directions, times, engines and amounts "
            "only; message text is deliberately excluded:"]
    if any(b.price_cents for b in shown):
        head.append(
            '  ("no LINKED purchase row" means no transactions row points at '
            "that message — a real unlock whose ledger row never got linked "
            "looks the same, so weigh it against the money line above.)")
    beats = [beat_line(b, ev.paid_ids, ev.runs, ev.fan.fan_id) for b in shown]
    if not beats:
        beats = ["  (no messages stored for this fan at all)"]
    tail = [audience_now(kind, raw, on) for kind, raw, on in ev.audiences]
    if tail:
        # ONCE, above the group — `MAX_AUDIENCES` is 3, and the same sentence
        # repeated three times reads as three facts. The warning is what stops
        # today's switches being offered as the reason for a send that already
        # happened; that is a property of the whole group, not of each line.
        tail.insert(0, "  THE LINE(S) BELOW DESCRIBE NO PAST SEND. They are who "
                       "each engine would reach FROM NOW ON — never why anyone "
                       "was in a broadcast that has already gone out. For that, "
                       "read the mass_run summary on the beat itself.")
    tail.append("  NOTE: Mass Nudge and Blast Online hand the audience to "
                "OnlyFans and write NO per-fan row, so their absence above is "
                "not proof he was left out of one.")
    return Thread(head, beats, tail)


def current_lines(ev: Evidence) -> Current:
    """How things stand NOW — ranked hypotheses, never proof of the past.

    Grouped by how much each line DECIDES, because `_fit` sheds tier by tier.
    The product's own badge, whose turn it is and the payer floor are
    ESSENTIAL: between them they answer the question. The knobs his numbers are
    measured against are CORE. The pause and the mute sit there too because
    their common answer is "no", and "no" decides nothing. The supporting
    figures trail all of it."""
    verdict, figures = status_lines(ev.status)
    essential = list(verdict)
    essential.append(_turn_line(ev))
    essential.append(
        f"- lifetime_spend_cents: {usd(ev.fan.lifetime_spend_cents)}. CONTENT "
        "payer: " + ("YES — he has bought a tip or a PPV unlock, so he clears "
                     "the payer floor."
                     if ev.is_payer else
                     "NO — he has never bought content. A SUBSCRIPTION IS NOT A "
                     "PURCHASE, so the payer floor turns him away however large "
                     "that lifetime number looks."))

    knobs = [f"{key}={ev.cfg.get(key)} ({meaning})"
             for key, meaning in _AI_CHATTER_KNOBS]
    knobs.append(
        f"max_lifetime_spend_cents={usd(ev.cfg.get('max_lifetime_spend_cents'))} "
        f"(the whale hand-off to a human; he is at "
        f"{usd(ev.fan.lifetime_spend_cents)})")
    core = ["- AI Seller config on this account: " + "; ".join(knobs) + ".",
            _pause_line(ev.fan.automation_paused_until),
            "- muted-creator hard skip: "
            + ("YES — a peer creator we follow whose chat is muted. Every "
               "automation skips him and he does not appear on Settings → "
               "Restrictions."
               if ev.muted_creator else "no.")
            + f" subscription_status: {ev.fan.subscription_status or 'unknown'}"
            + (f", expires {at(ev.fan.subscription_expires_at)}"
               if ev.fan.subscription_expires_at else "") + "."]

    return Current(
        head=["", "CURRENT-STATE — supports a ranked hypothesis, never proof "
              "of the past:"],
        essential=essential, core=core, figures=figures)


def _turn_line(ev: Evidence) -> str:
    """Whose turn it is — reading the thread the way the ENGINE reads it.

    A broadcast does not take the turn (`ai_chatter._gather` sets `last_dir`
    only off non-broadcast rows), and getting that backwards would report a fan
    who is owed an answer as one who already got one."""
    turn = next((b for b in collapse(ev.rows)
                 if b.mass_run_id is None and not b.is_unsent
                 and b.direction in ("in", "out")), None)
    if turn is None:
        return ("- turn ownership: unknown — no non-broadcast message on the "
                "thread.")
    if turn.direction == "in":
        return (f"- turn ownership: the FAN spoke last ({at(turn.ts)}). He is a "
                "candidate for every engine that answers an unanswered message.")
    return (f"- turn ownership: WE spoke last ({turn.kind or 'a human'}, "
            f"{at(turn.ts)}). AI Seller and Auto Convo only speak when the FAN "
            "spoke last, so neither will re-open him on its own. A mass "
            "broadcast does not take the turn and was skipped for this line; "
            "the one blind spot is a blast fired from the OnlyFans app, which "
            "carries no run id.")


def _pause_line(pause: datetime | None) -> str:
    """`automation_paused_until` — and WHICH of its two writers stamped it.

    They are worlds apart: the post-reply brake stamps minutes, while
    `quarantine_if_undeliverable` stamps SEVEN DAYS on a fan OnlyFans would not
    accept a message for. Days out means the second, and it is an entirely
    different answer to give an operator."""
    if pause is None:
        return "- automation_paused_until: not set."
    now = datetime.utcnow()
    if pause <= now:
        why = ("EXPIRED, so it blocks nothing now — this is the post-reply "
               "brake, stamped after her last send.")
    elif (pause - now).days >= 2:
        why = ("STILL ACTIVE, and days out — that is the UNDELIVERABLE "
               "QUARANTINE: a send came back with no id, so his subscription "
               "has lapsed or the account is gone, and every sender rests him "
               "for a week before re-checking.")
    else:
        why = ("STILL ACTIVE; every pause-aware sender skips him until then. "
               "Minutes out, this is the post-reply brake.")
    return f"- automation_paused_until: {at(pause)} — {why}"


def render(ev: Evidence) -> str:
    """The whole block, under the cap."""
    nick = clean_label(ev.fan.custom_nickname)
    head = [f"{_FAN_HEADER}\n",
            f"Subject: fan {int(ev.fan.fan_id)}"
            + (f' "{nick}"' if nick else "")
            + f" on account {ev.account_id} ({ev.label}).", ""]
    return _fit(head, ["PROVEN — stored rows:"] + proven_lines(ev),
                thread_lines(ev), current_lines(ev))


# How many beats survive before the cap is allowed to start eating the
# CURRENT-STATE core. Five reaches back past a mass send to the last real
# exchange on every live thread measured on 2026-08-28.
THREAD_FLOOR = 5


def _fit(head: list[str], proven: list[str], thread: Thread,
         current: Current) -> str:
    """Assemble under the cap. The ORDER of sacrifice below IS the policy.

    A ladder rather than a single rule, and it got that way from live data.
    With "shed all of CURRENT-STATE first", a real fan with a busy thread lost
    the turn-ownership line — which on the very case this feature was built
    from IS the answer — while fourteen rows of thread survived. Five rungs
    rather than two because value is not sorted by section: the last few beats
    outrank a config knob, and the badge outranks both.

    The stored rows are last in every case. A pack that dropped the skip_list
    row to keep a config knob would be answering a different question."""
    groups = (head, proven, thread.head, thread.beats, thread.tail,
              current.head, current.essential, current.core, current.figures)

    def joined() -> str:
        return "\n".join(line for group in groups for line in group)

    def shed(group: list[str], down_to: int = 0) -> None:
        while len(group) > down_to and len(joined()) > CAP_CHARS:
            group.pop()               # oldest beat / least decisive line first

    shed(current.figures)                 # 1. the supporting arithmetic
    shed(thread.beats, THREAD_FLOOR)      # 2. the thread, down to a floor
    shed(current.core)                    # 3. the knobs, the pause, the mute
    shed(thread.beats, 1)                 # 4. the thread, to the bone
    shed(current.essential)               # 5. badge, turn, payer — truly last
    shed(current.head)
    # `.rstrip` because the section spacers are empty strings: shed the whole
    # of CURRENT-STATE and its leading blank is the last line left, which would
    # hand the model a block ending in a dangling newline.
    out = joined().rstrip("\n")
    if len(out) <= CAP_CHARS:
        return out
    # PROVEN alone is over the cap — cut at a line boundary so no line reads as
    # a fact that was only half-written. `rfind` returns -1 when there is no
    # newline to cut at, which would silently keep the whole overlong string.
    cut = out.rfind("\n", 0, CAP_CHARS)
    return out[:cut if cut > 0 else CAP_CHARS].rstrip("\n")
