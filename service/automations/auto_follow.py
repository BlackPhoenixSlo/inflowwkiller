"""service/automations/auto_follow.py — Automation: auto_follow ("Auto-follow / Auto-like").

Trigger OnlyFans' re-engagement by LIKING fans' recent messages (the fan gets a
"X liked your message" push) — or by FOLLOWING fans. A cheap, notification-only
nudge that never sends a DM.

steps_json (per-run knobs):
  action     "like_messages" | "like_posts" | "follow" | "ping"  (default like_messages)
  targets    who to act on — shape depends on action (see _resolve_targets)
  daily_cap  max actions this RUN (enforced per-run; cadence × cap ≈ per day)
  dry_run    plan only, no OF mutation (DEFAULT TRUE — safe)
  money_gate follow action only: price-check before following (DEFAULT TRUE —
             false BUYS every paid profile in the pool, see the follow action)
  quiet_days              ping: fan counts as quiet after N days silent (default 7)
  min_days_between_pings  follow + ping: per-fan notification cooldown, in days
             (default 14, clamped 1..365). ONE ledger, shared by both actions —
             whichever of them notified a fan last, neither notifies him again
             inside it. EXCEPT a hand-named `targets.source: "fan_ids"` list,
             which is an operator override and follows exactly the fans named,
             ledger or no ledger. It still STAMPS, but be clear about who that
             stamp protects: the OTHER sources, which will now steer clear of
             him. It does NOT protect him from `fan_ids` — a recurring rule
             carrying that source re-follows the same named fans every single
             tick, because the exemption is precisely a decision to ignore the
             only record that would say "already done". See _merge_pools, and
             the ⚠️ RECURRING CHARGE note under the follow action.

Actions:
  • like_messages  REAL. For each target fan, like their latest inbound message
    via of_client.like_message (VERIFIED LIVE — you can only like the OTHER
    party's messages). This is the re-engagement lever.
  • like_posts     REAL. Like each of the post ids in `targets.post_ids` via
    of_client.like_post.
  • follow         REAL. Follow (or RE-follow) each target fan so they get the
    "started following you" push. Default pool is OF's own win-back list
    (recent_expired_subscribers — fans whose sub to me just lapsed).

    ⚠️ MONEY GATE: OF's "subscribe" endpoint PAYS when the target profile has a
    price. Every fan is profile-checked first and only followed when their
    subscribePrice is exactly 0; a missing/unreadable price SKIPS the fan
    (never assume free). A lapsed relationship (subscribedByExpireDate set)
    goes through /resubscribe, a fresh one through /subscribe.

    `money_gate: false` (default TRUE) REMOVES that check and follows blind —
    no profile read, every priced fan BOUGHT. Measured on live 337749380:
    ~3% of stored fans are priced, ≈$177 to backfill 711 of them. Opt-in per
    rule so the rules already in prod keep gating.

    ⚠️ RECURRING CHARGE — `money_gate: false` + `targets.source: "fan_ids"`.
    These two knobs are each defensible alone and together they are the one
    combination in this module that PAYS THE SAME FAN ON EVERY TICK, forever.
    Nothing is left to stop it. The gate is what reads the profile, so with it
    off there is NO read — a fan we already follow is never recognised as
    already-followed, and `gated_follow` returns "followed" unconditionally.
    The cooldown ledger was the only other brake, and `fan_ids` is deliberately
    exempt from reading it. Run-measured, 2 named fans over 6 ticks:

        GATED   + fan_ids   →  2 follow calls   (the profile read self-limits:
                                _classify sees subscribedBy and says
                                already_following, so nothing is bought twice)
        UNGATED + fan_ids   → 12 follow calls   (2 fans × 6 ticks)
        UNGATED + fan_ids, one $15 profile → 6 charges on that ONE fan

    This is NOT new damage. Before the ledger read landed the follow action
    consulted no ledger for ANY source, so this loop existed for all of them;
    the change added the brake everywhere except here, which is a large net
    improvement, not a regression. And no rule in prod is shaped this way —
    373 is a ping, 374 is `recent_active`, 395 is `smart_list`, and the UI
    emits no `fan_ids` at all. It takes a hand-written payload setting both
    knobs. Written down so the next person to write one by hand knows what he
    is buying: a one-off `fan_ids` run is what the exemption is for; a
    RECURRING rule with the gate also off is a standing order to re-buy.

  • BACKFILL pool — `targets: {"source": "all_stored"}` (now the DEFAULT for
    this action) follows back EVERY fan on file: current subs, old fans never
    followed, and fans who unsubbed but are still stored. Sliced by daily_cap
    per tick, and each tick EXCLUDES the fans already notified inside
    min_days_between_pings — that ledger is the pool's only progress marker, so
    it is what carries the window forward through the table. A 4-hourly rule
    drains the backlog over a few days, then idles.

    `source` also takes a LIST and loops over it, first-seen order, deduped:
      {"source": ["expired", "all_stored"]}   win-backs first, then everyone
    A source that errors is logged and skipped so it can't sink the others —
    except `all_stored`, whose failure is our own database breaking and is
    raised rather than hidden behind an empty, successful-looking run.

  ⚠️ DELIBERATE BEHAVIOR CHANGE (2026-09-06). The `follow` action now reads
    the cooldown ledger too, and that ledger read reaches EVERY pooled source,
    not only the new backfill — rules 374 (`recent_active`) and 395
    (`smart_list`) never consulted it before and do now. This was measured, not
    assumed: over 6 ticks of 374 the same 20 distinct fans are reached, follow
    calls drop 120 → 20 and get_user reads 240 → 140. Same people, a fifth of
    the calls, and nobody gets the same push twice inside the window — which is
    what we wanted anyway. Do not describe this as "changes nothing already
    running"; it changes those two rules, on purpose, for the better. The
    single carve-out is `fan_ids`, which an operator types by hand.
  • ping           REAL. Re-engage fans who STOPPED CHATTING: for each fan
    quiet ≥ quiet_days (default 7) that we currently follow, unfollow +
    immediately re-follow so OnlyFans fires a fresh "started following you"
    push; a quiet fan we don't follow yet just gets followed (their first
    ping). Per-fan cooldown in follow_ping_state (min_days_between_pings,
    default 14) so nobody gets nagged every run. Same money gate as follow.

Both follow and ping run through ONE shared loop (_follow_batch → gated_follow)
so the money gate, the cap-on-notifications rule, and the cooldown stamping can
never drift apart between the two actions.

DRY RUN runs the gate too. It reads each candidate's profile and classifies it
with the same `_classify` the live run uses, so `would_follow`/`would_ping` list
only fans who would actually be notified. Previewing the raw candidate pool
instead is not a smaller lie — a rule aimed at fans we already follow reported
"8 candidates" for six days and fired nothing. The preview mutates nothing; it
examines at most `daily_cap` fans and says how many of the pool it checked.

Self-registers via @register("auto_follow"); schedule with an automation_rules
row (kind="auto_follow", trigger_json={"every_seconds": N}, steps_json=knobs).
Defaults to OFF + dry_run — nothing acts until an operator enables a rule.
"""
from __future__ import annotations

import asyncio
import logging
from collections import Counter
from datetime import datetime, timedelta

from sqlalchemy import or_, select

import automation_executor as ax
from automation_registry import register
from db.engine import get_session
from db.models import Fan, FollowPingState, Message
from ._common import load_hard_skip_ids

log = logging.getLogger("of-relay.automation.auto_follow")

_DEFAULT_DAILY_CAP = 50
_MAX_DAILY_CAP = 1000
_DEFAULT_RECENT_DAYS = 7
_DEFAULT_QUIET_DAYS = 7
_DEFAULT_PING_GAP_DAYS = 14
_HEADROOM_FACTOR = 5     # oversize a target pool ×N — see _headroom().
# Outcomes that fired a real notification at the fan — they consume the cap
# and start the ping cooldown.
_NOTIFY_OUTCOMES = ("followed", "refollowed", "pinged")
# The outcomes that mean "we read his profile and decided not to notify him" —
# the backfill window's progress marker (`Fan.follow_examined_at`). Deliberately
# NOT the error path: a 404 or a transient OF failure is not a verdict, and the
# two are indistinguishable from the exception alone (see `_all_stored_fan_ids`).
_EXAMINED_OUTCOMES = ("already_following", "paid_profile", "no_price")


def _headroom(cap: int) -> int:
    """Pool size to fetch for a run capped at `cap`. The per-fan gates
    (paid-profile, ping cooldown, already-liked, hard_skip) drop candidates, so a
    pool sliced at exactly `cap` under-fills every run — oversize it ×
    _HEADROOM_FACTOR and act until `cap` notifications land. Shared by
    follow / ping / like_messages so the factor can't drift between them."""
    return max(cap, 1) * _HEADROOM_FACTOR


def _int_knob(payload: dict, key: str, default: int, *, lo: int, hi: int | None = None,
              zero_default: bool = False) -> int:
    """One clamped-int knob. None/unparseable → default. The default is NOT
    applied to a parseable value, so an explicit 0 stays 0 wherever lo allows
    it (daily_cap=0 is the operator's stop switch, not "unset").

    `zero_default` flips that for the day-count knobs (quiet_days,
    min_days_between_pings) where 0 is not a meaningful setting: a value ≤ 0
    means "use the house default", matching the pre-refactor `int(x or DEFAULT)`.
    Leave it False anywhere 0 is a real, distinct value."""
    raw = payload.get(key)
    try:
        v = default if raw is None else int(raw)
    except (TypeError, ValueError):
        v = default
    if zero_default and v <= 0:
        v = default
    if hi is not None:
        v = min(v, hi)
    return max(lo, v)


# ── Target pools ──────────────────────────────────────────────────────

async def _recent_active_fan_ids(account_id: str, days: int, limit: int) -> list[int]:
    """Fan ids who sent us an inbound message in the last `days` — newest
    activity first. The default target pool for like_messages."""
    cutoff = datetime.utcnow() - timedelta(days=max(1, days))
    async with get_session() as s:
        rows = (await s.execute(
            select(Message.fan_id, Message.created_at)
            .where(Message.account_id == str(account_id),
                   Message.direction == "in",
                   Message.is_unsent.is_(False),
                   Message.created_at >= cutoff)
            .order_by(Message.created_at.desc())
        )).all()
    seen: set[int] = set()
    out: list[int] = []
    for fid, _ in rows:
        fid = int(fid)
        if fid in seen:
            continue
        seen.add(fid)
        out.append(fid)
        if len(out) >= limit:
            break
    return out


async def _quiet_fan_ids(account_id: str, quiet_days: int, limit: int) -> list[int]:
    """Fans who HAVE chatted before but whose last inbound is ≥ quiet_days old —
    the "stopped chatting" pool for the ping action. Freshest lapse first, so
    the fans most recently worth winning back ping before long-dead ones.
    Reads the cached fans.last_message_received_at (rebuildable from messages)
    instead of a Message group-by — same truth, no full-table scan."""
    cutoff = datetime.utcnow() - timedelta(days=max(1, quiet_days))
    async with get_session() as s:
        rows = (await s.execute(
            select(Fan.fan_id)
            .where(Fan.account_id == str(account_id),
                   Fan.last_message_received_at.is_not(None),
                   Fan.last_message_received_at < cutoff)
            .order_by(Fan.last_message_received_at.desc())
            .limit(limit)
        )).all()
    return [int(r[0]) for r in rows]


async def _all_stored_fan_ids(account_id: str, limit: int,
                              exclude: set[int] | None = None,
                              examined_before: datetime | None = None) -> list[int]:
    """EVERY fan we have stored for this account — the follow-back BACKFILL pool.

    The three tiers an operator means by "follow back everyone": fans currently
    subscribed, old fans we simply never followed, and fans who have since
    unsubbed but are still on file. All three are just rows in `fans`, so one
    query covers them; OF's own subscriber lists page out at ~20 and would miss
    the long tail entirely.

    Newest-seen first, so a drip-fed backfill reaches the fans most likely to
    still care before the ancient ones.

    ⚠️ `exclude` IS THE PROGRESS MARKER, and it is not optional in practice.
    This query is stateless: without it, every tick re-fetches the identical
    `LIMIT limit` head of the table. On the ungated path that head is followed
    again on every tick, forever, at real money; on the gated path the run stalls
    at `followed=0, already=limit` and never looks past row `limit`, so the fans
    who sort last — the never-messaged tier this pool exists for — are
    unreachable at any cap. Callers pass the ping-cooldown ledger
    (`_recently_pinged_ids`), which is stamped for every follow we fire, so each
    tick's window slides forward through the whole table.

    ⚠️ THE LEDGER IS ONLY HALF THE MARKER, and the missing half was the same bug
    over again. `_follow_batch` stamps `_NOTIFY_OUTCOMES` only, so the window
    slid past fans we NOTIFIED and past nobody else. Once `limit` consecutive
    head rows are fans we will never notify, the window is pinned again and the
    never-messaged tier is unreachable at any cap: `followed=0`, an empty ledger,
    `status ok`, and nothing anywhere saying so. `examined_before` is the other
    half — `Fan.follow_examined_at`, written for every verdict in
    `_EXAMINED_OUTCOMES`.

    All THREE of those verdicts, not just `already_following`. That one is the
    steady state of any account that has been following fans back, so it is the
    shape that bites first — but a head of `paid_profile` + `no_price` pins the
    window exactly as hard, and unlike an `already_following` stamp those two
    verdicts do not otherwise expire. Each is a profile we successfully READ and
    decided against, which is progress; the stamp lapses after `gap_days` so a
    fan whose price changes is looked at again.

    Deliberately NOT the same column as the ping ledger: every pingable fan is by
    definition one we already follow, so a shared timestamp would let this sweep
    starve the ping action of its entire population.

    The ERROR path stays unstamped on purpose — an exception is not a verdict,
    and the 404 case below is why.

    Excluded in SQL rather than after the slice, because filtering a
    `LIMIT`-ed head only shrinks it — it does not advance it. `examined_before`
    likewise: it is a WHERE clause on the table, so it costs no bound parameters
    and has none of the ledger's 32766-parameter ceiling to worry about.

    KNOWN, ACCEPTED (2026-09-06): a fan whose follow PERMANENTLY fails — a 404
    on a deleted account — is never stamped, so he is never excluded, so he is
    re-attempted on every tick from now on. That was always true; what this
    pool changes is that it is now a permanent steady state rather than a
    transient one. Once the live tail of the table has drained, the window
    stops advancing and the run settles at `followed=0, errors=N` forever, with
    nothing in the stats saying the pool is dead rather than merely quiet. Left
    alone on purpose: rule 374's 404 noise is out of scope, and a "stamp the
    failures too" fix would need to tell a dead account apart from a transient
    OF error, which we cannot do from the error alone. Written down so the next
    reader recognises the shape instead of debugging it.

    The bound parameter count is fine: the ledger only holds fans notified
    inside min_days_between_pings, so it is capped at roughly
    `daily_cap × ticks_per_day × gap_days` (≈2.5k at prod's cap 30 / 4-hourly /
    14 days) against SQLite's 32766-parameter ceiling. Deliberately NOT
    truncated to a safe prefix — a silently dropped tail would re-follow those
    fans, which is the bug this argument exists to prevent.
    """
    q = (select(Fan.fan_id)
         .where(Fan.account_id == str(account_id))
         .order_by(Fan.last_message_received_at.desc().nullslast(),
                   Fan.fan_id.desc())
         .limit(limit))
    if exclude:
        q = q.where(Fan.fan_id.not_in([int(f) for f in exclude]))
    if examined_before is not None:
        q = q.where(or_(Fan.follow_examined_at.is_(None),
                        Fan.follow_examined_at < examined_before))
    async with get_session() as s:
        rows = (await s.execute(q)).all()
    return [int(r[0]) for r in rows]


async def _recent_expired_fan_ids(client) -> list[int]:
    """Fan ids from OF's own win-back pool (recent_expired_subscribers,
    verified live). The payload is either {"list":[user,...]} or a bare list —
    take ids defensively and drop anything unreadable."""
    res = await asyncio.to_thread(client.recent_expired_subscribers)
    rows = res.get("list") if isinstance(res, dict) else res
    out: list[int] = []
    for u in rows if isinstance(rows, list) else []:
        try:
            out.append(int(u["id"] if isinstance(u, dict) else u))
        except (KeyError, TypeError, ValueError):
            continue
    return out


async def _resolve_smart_list(smart_list_id: int, account_id: str) -> list[int]:
    """Expand a saved Smart List to its current fan-id set. Lazy import so the
    executor's plugin scan never pulls the FastAPI/pydantic router module.

    Scoped to `account_id`: a list id is a bare integer in steps_json, so a typo
    (or a list deleted and its id reused) would otherwise resolve ANOTHER
    creator's segment and spend this account's like budget on fans it has no
    relationship with.
    """
    from smart_lists_api import _load_normalised_fans, _rules_of, resolve
    from db.models import SmartList
    async with get_session() as s:
        row = await s.get(SmartList, int(smart_list_id))
        if row is None:
            return []
        if str(row.account_id) != str(account_id):
            log.warning("auto_follow smart_list=%s belongs to account=%s, not %s — ignoring",
                        smart_list_id, row.account_id, account_id)
            return []
        rules = _rules_of(row)
    fans = await _load_normalised_fans(account_id, rules)
    return resolve(rules, fans)


async def _resolve_targets(account_id: str, targets: dict, cap: int) -> list[int]:
    """Fan-id list for the message/follow actions. `targets`:
      {"source":"recent_active", "days":7}          (default)
      {"source":"fan_ids", "fan_ids":[...]}
      {"source":"smart_list", "smart_list_id": N}
    """
    targets = targets or {}
    source = str(targets.get("source") or "recent_active")
    if source == "fan_ids":
        raw = targets.get("fan_ids") or []
        return [int(x) for x in raw if str(x).lstrip("-").isdigit()][:cap]
    if source == "smart_list":
        sid = targets.get("smart_list_id")
        if sid is None:
            return []
        return (await _resolve_smart_list(int(sid), account_id))[:cap]
    # default: recently-active fans
    days = int(targets.get("days") or _DEFAULT_RECENT_DAYS)
    return await _recent_active_fan_ids(account_id, days, cap)


def _source_list(targets: dict) -> list[str]:
    """The pool sources for one follow run, in priority order.

    `source` takes a single name OR a list, so a rule can stack pools:
        {"source": "all_stored"}                        → the backfill
        {"source": ["expired", "recent_active"]}        → two pools, in order
    Unknown names fall through to _resolve_targets, which treats anything it
    doesn't recognise as recent_active — the pre-existing behaviour.

    Default is `all_stored`: "follow back everyone" is what the operator asked
    for (2026-09-06), so the no-config rule does the widest useful thing rather
    than the narrow win-back list the follow action used to default to.

    Checked before changing it: both live `follow` rules in prod (374
    recent_active, 395 smart_list) set an explicit source, and 373 is a `ping`,
    which never reaches here — so this DEFAULT widens nothing already running.
    That is a claim about the default only. The cooldown read those two rules
    now go through is a real, deliberate change to them — see the module
    docstring's BEHAVIOR CHANGE note.
    """
    raw = (targets or {}).get("source") or "all_stored"
    names = raw if isinstance(raw, (list, tuple)) else [raw]
    out: list[str] = []
    for n in names:
        n = str(n).strip()
        if n and n not in out:      # order-preserving dedup
            out.append(n)
    return out or ["all_stored"]


async def _merge_pools(account_id: str, client, sources: list[str],
                       targets: dict, headroom: int,
                       exclude: set[int] | None = None,
                       examined_before: datetime | None = None) -> list[int]:
    """Concatenate every source's pool into ONE candidate list, first-seen
    order, no duplicates — a fan in two pools is still followed once.

    Each source is fetched to the full `headroom`; the merged list is sliced by
    the caller. A source that raises (a dead OF list, say) is logged and
    skipped rather than sinking the whole run — with several pools stacked, one
    bad source must not cost the others their turn. The one exception is
    `all_stored`: it is a query against OUR OWN database, not somebody else's
    list, so a failure there is a broken machine rather than a pool that
    happens to be unreachable. Swallowing it produced a clean, successful,
    EMPTY run — `{'followed': 0, 'candidates': 0, 'errors': 0}` — every tick,
    forever, with nothing anywhere saying the backfill had stopped. It escapes.

    `exclude` (the ping cooldown) is applied INSIDE each fetch where the source
    is a database query, so the `LIMIT headroom` window slides past fans we
    already actioned instead of re-fetching them and shrinking. For the
    list-shaped sources OF hands us whole there is nothing to slide, so they are
    filtered after the fact.

    ⚠️ `fan_ids` IS EXEMPT FROM THE COOLDOWN (operator's call, 2026-09-06).
    Every other source is a pool we chose for the operator, so the ledger is
    what stops us nagging the same fan; a hand-written `{"source": "fan_ids",
    "fan_ids": [...]}` is not a pool, it is an instruction naming the fans, and
    a cooldown the operator was never asked about silently dropping half of a
    two-name list is the wrong answer. The exemption is the READ only — those
    follows still STAMP the ledger (`_follow_batch`), which is what keeps the
    automatic sources from re-notifying a fan an operator just reached by hand.
    Exactly `fan_ids` and nothing else: every unrecognised name falls through
    `_resolve_targets` to recent_active, which stays filtered.

    ⚠️ The price of the exemption, stated plainly: with `money_gate: false`
    there is no profile read either, and then a recurring `fan_ids` rule has
    NOTHING left that could tell it these fans were already followed — it
    re-issues /subscribe on every tick, and on a priced profile that is a fresh
    charge every tick. Measured: 6 ticks of an ungated 2-fan `fan_ids` rule fire
    12 follow calls, against 2 for the same rule with the gate on (the gate's
    read is what notices `subscribedBy` and stops). The gate is the last brake
    once this one is removed; do not remove both on a rule that repeats. See the
    ⚠️ RECURRING CHARGE note in the module docstring.
    """
    exclude = exclude or set()
    seen: set[int] = set()
    pool: list[int] = []
    for name in sources:
        # A hand-named list overrides the ledger; a pool we picked does not.
        # ⚠️ With money_gate:false this is the last brake gone — no ledger read
        # AND no profile read means a recurring fan_ids rule pays every tick.
        skip = set() if name == "fan_ids" else exclude
        try:
            if name == "expired":
                ids = await _recent_expired_fan_ids(client)
            elif name == "all_stored":
                # `examined_before` only here: this is the one source that is a
                # sliding WINDOW over our own table. Every other source is a
                # bounded list somebody handed us, with nothing to slide.
                ids = await _all_stored_fan_ids(account_id, headroom,
                                                exclude=exclude,
                                                examined_before=examined_before)
            else:
                ids = await _resolve_targets(account_id, {**targets, "source": name},
                                             headroom)
        except Exception:
            if name == "all_stored":
                # Our own DB, not a third party's list — see the docstring.
                log.error("auto_follow source=all_stored FAILED account=%s — the "
                          "backfill query is broken, not an unreachable pool",
                          account_id, exc_info=True)
                raise
            log.warning("auto_follow source=%s failed account=%s — skipping it",
                        name, account_id, exc_info=True)
            continue
        for fid in ids:
            if fid not in seen and fid not in skip:
                seen.add(fid)
                pool.append(fid)
        if len(pool) >= headroom:
            break
    return pool


# ── The gated follow (shared by follow + ping) ────────────────────────

class _StrandedError(RuntimeError):
    """The ping's unfollow landed but every re-follow attempt failed — the fan
    is left UNFOLLOWED. Callers surface this in its own counter; it must never
    blend into the generic `errors` bucket."""


def _classify(u: dict, *, unfollow_first: bool) -> str:
    """What the gate decides about one fan, from their profile payload alone.

    Pure and side-effect free, so the DRY RUN and the live run can never
    disagree about the outcome — they call this same function. That mattered:
    the preview used to report the raw candidate pool as `would_follow`, and a
    rule pointed at fans we already follow previewed as "8 ready" while the
    live run fired zero notifications. Six days of clean dry runs said nothing
    was wrong.

      "followed"           fresh follow → notification
      "refollowed"         lapsed relationship re-armed via /resubscribe → notification
      "pinged"             active follow cycled (unfollow + re-follow) → notification
      "already_following"  active follow left alone (unfollow_first=False)
      "paid_profile"       skipped — profile has a price (subscribe would PAY)
      "no_price"           skipped — price unreadable (never assume free)
    """
    # MONEY GATE — subscribe PAYS unless the profile is free. Only an explicit
    # 0 passes; a missing/unreadable price is a SKIP, not a benefit of the doubt.
    price = u.get("subscribePrice")
    if not isinstance(price, (int, float)):
        return "no_price"
    if float(price) != 0:
        return "paid_profile"
    # subscribedBy = I already follow them (viewer→target edge). VERIFIED
    # against live payloads: subscribedByData.price carries the FAN's own
    # price and subscribedOnData.regularPrice carries OURS, so `By` is the
    # edge we pay for — us→them. If OF ever flips this the failure mode is a
    # skip or a benign "already subscribed" error, never a payment (gated
    # above).
    if u.get("subscribedBy"):
        return "pinged" if unfollow_first else "already_following"
    if u.get("subscribedByExpireDate"):
        return "refollowed"
    return "followed"


async def gated_follow(client, fan_id: int, *, unfollow_first: bool = False,
                        money_gate: bool = True) -> str:
    """Price-gate + follow one fan; the single decision path shared by the
    follow and ping actions. Returns the `_classify` outcome it carried out.

    Raises _StrandedError when an unfollow can't be undone; any other OF error
    propagates for the caller to count.

    ⚠️ `money_gate=False` REMOVES the paid-profile check and follows blind, with
    no get_user read at all. OF's /subscribe PAYS whenever the target's own
    subscribePrice is non-zero, so this WILL buy subscriptions: measured on the
    live 337749380 pool, ~3% of stored fans are priced, ≈$177 for a 711-fan
    backfill (peers like littlelexi @ $5 and elfbat @ $15 sit in that 3%).
    Opt-in per rule and never the default — the three rules live in prod today
    all run gated, and adding a knob must not change what they already do.
    """
    if not money_gate:
        # No read, no verdict — we cannot know if they're already followed, so
        # a repeat "already subscribed" error from OF is the caller's to count.
        # unfollow_first is deliberately ignored: cycling a follow blind would
        # risk stranding a fan we never confirmed we had.
        await asyncio.to_thread(client.follow_user, fan_id)
        return "followed"
    u = await asyncio.to_thread(client.get_user, fan_id)
    u = u if isinstance(u, dict) else {}
    outcome = _classify(u, unfollow_first=unfollow_first)
    if outcome in ("no_price", "paid_profile", "already_following"):
        return outcome
    if outcome == "pinged":
        await asyncio.to_thread(client.unfollow_user, fan_id)
        # VERIFIED LIVE 2026-07-23 (Lexi→jaka): a just-unfollowed free follow
        # re-arms via POST /subscribe — /resubscribe 400s there ("Resubscribe
        # failed.", it only serves EXPIRED relationships). Keep resubscribe as
        # the fallback; we just ended a live follow and must not leave it
        # dangling.
        try:
            await asyncio.to_thread(client.follow_user, fan_id)
        except Exception:
            try:
                await asyncio.to_thread(client.resubscribe_user, fan_id)
            except Exception as e:
                raise _StrandedError(str(e)) from e
        return "pinged"
    if outcome == "refollowed":
        # Lapsed/expired relationship: /resubscribe is the canonical re-arm;
        # fall back to a plain follow (the price gate above already passed).
        try:
            await asyncio.to_thread(client.resubscribe_user, fan_id)
        except Exception:
            await asyncio.to_thread(client.follow_user, fan_id)
        return "refollowed"
    await asyncio.to_thread(client.follow_user, fan_id)
    return "followed"


async def _follow_batch(
    account_id: str, client, pool: list[int], cap: int, *, unfollow_first: bool,
    money_gate: bool = True,
) -> tuple[Counter, int, int]:
    """Run gated_follow over `pool` until `cap` NOTIFICATIONS have fired
    (gate skips don't consume the cap). Returns (outcome counts, stranded,
    errors). Every notification stamps the ping cooldown, so neither action
    can double-notify a fan the other just reached."""
    counts: Counter = Counter()
    stranded = errors = 0
    label = "ping" if unfollow_first else "follow"
    for fid in pool:
        if sum(counts[o] for o in _NOTIFY_OUTCOMES) >= cap:
            break
        try:
            outcome = await gated_follow(client, fid, unfollow_first=unfollow_first,
                                          money_gate=money_gate)
            counts[outcome] += 1
            if outcome in _NOTIFY_OUTCOMES:
                await stamp_ping(account_id, fid)
            elif outcome in _EXAMINED_OUTCOMES:
                # A DURABLE verdict, arrived at from a profile we successfully
                # READ: nothing is owed (`already_following`), or following would
                # cost money (`paid_profile`), or the profile carries no price we
                # can trust (`no_price`). None of the three fires a notification,
                # so none belongs in the ping ledger — but all three are exactly
                # the progress the backfill window needs, and leaving them
                # unrecorded is what pinned that window at the head of the table
                # forever.
                #
                # All THREE, not just `already_following`: a head made of priced
                # and unreadable profiles pins the window every bit as hard, and
                # it was the shape left open when only the steady-state verdict
                # was stamped. The stamp expires after `gap_days`, so a fan whose
                # price changes is re-examined; the ERROR path stays unstamped and
                # its 404 residual is documented on `_all_stored_fan_ids`.
                await _stamp_examined(account_id, fid)
        except _StrandedError:
            stranded += 1
            log.error("auto_follow %s STRANDED account=%s fan=%s — unfollowed "
                      "but re-follow failed", label, account_id, fid, exc_info=True)
        except Exception:
            errors += 1
            log.warning("auto_follow %s failed account=%s fan=%s",
                        label, account_id, fid, exc_info=True)
    return counts, stranded, errors


async def _client_or_none(account_id: str):
    """The OF client, or None when this account has no usable session.

    Only the DRY-RUN previews use this. A preview must reach OF to run the
    gate, but "no session" is an ordinary, recurring state here — and a plan
    that hard-errors is worse than one that says it could not look. The live
    paths keep raising, so a real run still surfaces a dead session loudly.
    """
    try:
        return await asyncio.to_thread(ax._make_client, account_id)
    except Exception:
        log.warning("auto_follow preview: no OF client for account=%s", account_id,
                    exc_info=True)
        return None


async def _preview_batch(
    client, pool: list[int], cap: int, *, unfollow_first: bool,
) -> tuple[Counter, list[int], int]:
    """What `_follow_batch` WOULD do, without doing it. Returns (outcome
    counts, the ids that would actually be notified, errors).

    Reads only — one get_user per examined fan, the same call the live run
    makes, then `_classify`. It examines at most `cap` fans rather than the
    whole ×5 headroom pool: the live run stops at `cap` NOTIFICATIONS, so a
    preview can never need more than `cap` verdicts to report a full budget,
    and a pool where nobody qualifies would otherwise cost 5× the reads for a
    plan that does nothing. `examined` is returned alongside `candidates` so a
    partial look is never mistaken for a full one.
    """
    counts: Counter = Counter()
    would: list[int] = []
    errors = 0
    for fid in pool[:max(cap, 0)]:
        try:
            u = await asyncio.to_thread(client.get_user, fid)
        except Exception:
            errors += 1
            log.warning("auto_follow preview get_user failed fan=%s", fid, exc_info=True)
            continue
        outcome = _classify(u if isinstance(u, dict) else {},
                            unfollow_first=unfollow_first)
        counts[outcome] += 1
        if outcome in _NOTIFY_OUTCOMES:
            would.append(fid)
    return counts, would, errors


def _preview_result(pool: list[int], cap: int, counts: Counter,
                    errors: int) -> dict:
    """The shared shape of both dry-run reports."""
    return {
        "candidates": len(pool),
        "examined": min(len(pool), max(cap, 0)),
        "already_following": counts["already_following"],
        "paid_profile_skipped": counts["paid_profile"],
        "no_price_skipped": counts["no_price"],
        "errors": errors,
        "cap": cap,
    }


# ── Ping cooldown state ───────────────────────────────────────────────

async def _recently_pinged_ids(account_id: str, gap_days: int) -> set[int]:
    """Fans pinged inside the cooldown window — excluded from this run."""
    cutoff = datetime.utcnow() - timedelta(days=max(1, gap_days))
    async with get_session() as s:
        rows = (await s.execute(
            select(FollowPingState.fan_id)
            .where(FollowPingState.account_id == str(account_id),
                   FollowPingState.last_pinged_at >= cutoff)
        )).all()
    return {int(r[0]) for r in rows}


async def stamp_ping(account_id: str, fan_id: int) -> None:
    """Record that this fan just got a follow-notification (ping OR first
    follow) so the cooldown covers every notification we caused."""
    now = datetime.utcnow()
    async with get_session() as s:
        row = await s.get(FollowPingState, (str(account_id), int(fan_id)))
        if row is None:
            s.add(FollowPingState(account_id=str(account_id), fan_id=int(fan_id),
                                  last_pinged_at=now, ping_count=1))
        else:
            row.last_pinged_at = now
            row.ping_count = (row.ping_count or 0) + 1


async def _stamp_examined(account_id: str, fan_id: int) -> None:
    """Record that the money gate READ this fan and found nothing to do.

    Separate from `stamp_ping` because the two facts are different: that one
    means "we fired a notification at him" and gates how often we may do it
    again; this one means "we looked, and there was nothing to fire" — already
    followed, priced, or unpriced (`_EXAMINED_OUTCOMES`) — and gates how often the
    BACKFILL bothers re-reading his profile. One timestamp for both would let the
    follow sweep silence the re-follow ping for every fan the ping exists to
    reach."""
    async with get_session() as s:
        row = await s.get(Fan, (str(account_id), int(fan_id)))
        if row is not None:
            row.follow_examined_at = datetime.utcnow()


# ── Like helpers ──────────────────────────────────────────────────────

def _favorites_count(post: object) -> int | None:
    """`favoritesCount` off a /posts/{id} payload, or None if unreadable."""
    if not isinstance(post, dict):
        return None
    try:
        return int(post.get("favoritesCount"))
    except (TypeError, ValueError):
        return None


def _is_already_liked_error(exc: Exception) -> bool:
    """OF answers a repeat like with 400 'This message is already liked.'
    That's the steady state of a recurring rule, not a failure."""
    return "already liked" in str(exc).lower()


async def _latest_inbound_message_id(account_id: str, fan_id: int) -> int | None:
    async with get_session() as s:
        row = (await s.execute(
            select(Message.message_id)
            .where(Message.account_id == str(account_id),
                   Message.fan_id == int(fan_id),
                   Message.direction == "in",
                   Message.is_unsent.is_(False))
            .order_by(Message.created_at.desc(), Message.message_id.desc())
            .limit(1)
        )).first()
    return int(row[0]) if row and row[0] is not None else None


# ── Per-action runners (uniform signature, dispatched from run()) ─────

async def _run_follow(account_id: str, payload: dict, *, cap: int, dry_run: bool,
                      targets: dict, hard_skip: set) -> dict:
    client = (await _client_or_none(account_id) if dry_run
              else await asyncio.to_thread(ax._make_client, account_id))
    if client is None:
        return {"action": "follow", "dry_run": True, "candidates": 0,
                "skipped": "of_unreachable"}
    # ⚠️ MONEY: opt-in per rule, default ON. See gated_follow — off means every
    # priced profile in the pool gets BOUGHT.
    money_gate = bool(payload.get("money_gate", True))
    headroom = _headroom(cap)
    sources = _source_list(targets)
    # The follow action reads the SAME cooldown ledger the ping action does
    # (`_run_ping` below), and for the same reason: `_follow_batch` stamps every
    # notification it fires, so a fan we followed on an earlier tick must not be
    # bought again on this one. Without this the pool is a stateless
    # `ORDER BY … LIMIT headroom` with no progress marker — the ungated path
    # re-followed the identical head of the list on every tick at real money,
    # and the gated path idled at `already=headroom` while the rest of the
    # table, including every never-messaged fan, stayed out of reach.
    # hi=365: an absurd gap (99999, say) makes `_recently_pinged_ids` return the
    # whole ledger, and `all_stored` then binds one parameter per stamped fan.
    # SQLite RAISES past its 32766-parameter ceiling — it does not truncate —
    # and that used to land as a clean, successful, empty run. A year is longer
    # than any real cooldown, so the clamp costs nothing and makes the
    # docstring's bound a guarantee rather than an estimate.
    gap_days = _int_knob(payload, "min_days_between_pings", _DEFAULT_PING_GAP_DAYS,
                         lo=1, hi=365, zero_default=True)
    recently = await _recently_pinged_ids(account_id, gap_days)
    # The OTHER half of the progress marker (see `_all_stored_fan_ids`): fans the
    # gate already read and found nothing to do for. Same cadence as the ping gap
    # on purpose — one operator knob for "how long before this fan is worth
    # touching again" — and the same 365-day clamp, so the window can be widened
    # but never turned into a permanent exclusion by a typo.
    examined_before = datetime.utcnow() - timedelta(days=gap_days)
    pool = await _merge_pools(account_id, client, sources, targets, headroom,
                              exclude=recently,
                              examined_before=examined_before)
    source = ",".join(sources)
    pool = [f for f in pool if f not in hard_skip][:headroom]

    if dry_run:
        # Plan only, and the plan RUNS THE GATE — every id in `would_follow` has
        # been price-checked and confirmed not-yet-followed. Listing the raw
        # pool instead is what let a rule aimed at fans we already follow
        # preview as "8 ready" for six days while doing nothing.
        if not money_gate:
            # The live ungated run reads NO profile, so a gated preview would
            # describe a different run than the one that will happen — and its
            # `paid_profile_skipped` would promise a protection that is switched
            # off. Report the plan honestly: everyone examined gets followed,
            # and some of them will cost money we cannot name without reading.
            would = pool[:max(cap, 0)]
            return {"action": "follow", "dry_run": True, "source": source,
                    "money_gate": False, "would_follow": would,
                    "candidates": len(pool), "examined": len(would),
                    "unpriced_follows": len(would), "cap": cap,
                    "warning": "money_gate off — paid profiles WILL be charged"}
        counts, would, errors = await _preview_batch(client, pool, cap,
                                                     unfollow_first=False)
        return {"action": "follow", "dry_run": True, "source": source,
                "would_follow": would,
                **_preview_result(pool, cap, counts, errors)}

    counts, _, errors = await _follow_batch(account_id, client, pool, cap,
                                            unfollow_first=False,
                                            money_gate=money_gate)
    # A run that walked a full pool and notified NOBODY is what a pinned window
    # looks like from the outside — and it is otherwise indistinguishable from a
    # quiet one: `status ok`, `followed: 0`, no error. That shape went unnoticed
    # for six days once already (the dry-run preview), so it gets a name in the
    # stats rather than a shrug, and `_bits.runStatsChunks` renders it.
    #
    # "Notified nobody", NOT "every fan was already_following". The pinned head
    # has two shapes and the narrower test only saw one of them: a head made of
    # `paid_profile` + `no_price` pins the window just as hard (those verdicts
    # never expire, where an `already_following` stamp does), and on that shape
    # `already_following` is 0 — so the counter read False on exactly the state it
    # exists to name. Notifying is what this action is FOR, so its absence is the
    # honest test. The `break` at the cap needs one notification, so a run that
    # notified nobody necessarily walked the whole pool.
    #
    # `errors` disqualifies it: a pool we could not READ is not a pool we have
    # finished with, and errors have their own chunk.
    notified = sum(counts[o] for o in _NOTIFY_OUTCOMES)
    pool_exhausted = bool(pool) and notified == 0 and not errors
    return {"action": "follow", "dry_run": False, "source": source,
            "money_gate": money_gate,
            "candidates": len(pool), "followed": counts["followed"],
            "refollowed": counts["refollowed"],
            "already_following": counts["already_following"],
            "paid_profile_skipped": counts["paid_profile"],
            "no_price_skipped": counts["no_price"],
            "pool_exhausted": pool_exhausted,
            "errors": errors, "cap": cap}


async def _run_ping(account_id: str, payload: dict, *, cap: int, dry_run: bool,
                    targets: dict, hard_skip: set) -> dict:
    quiet_days = _int_knob(payload, "quiet_days", _DEFAULT_QUIET_DAYS, lo=1, zero_default=True)
    # Same hi=365 as the follow action: one knob, one ledger, one bound.
    gap_days = _int_knob(payload, "min_days_between_pings", _DEFAULT_PING_GAP_DAYS,
                         lo=1, hi=365, zero_default=True)
    pool = await _quiet_fan_ids(account_id, quiet_days, limit=_headroom(cap))
    recently = await _recently_pinged_ids(account_id, gap_days)
    pool = [f for f in pool if f not in hard_skip and f not in recently]

    client = (await _client_or_none(account_id) if dry_run
              else await asyncio.to_thread(ax._make_client, account_id))
    if dry_run:
        if client is None:
            return {"action": "ping", "dry_run": True, "quiet_days": quiet_days,
                    "min_days_between_pings": gap_days, "candidates": len(pool),
                    "skipped": "of_unreachable"}
        counts, would, errors = await _preview_batch(client, pool, cap,
                                                     unfollow_first=True)
        return {"action": "ping", "dry_run": True, "quiet_days": quiet_days,
                "min_days_between_pings": gap_days, "would_ping": would,
                **_preview_result(pool, cap, counts, errors)}

    counts, stranded, errors = await _follow_batch(account_id, client, pool, cap,
                                                   unfollow_first=True)
    return {"action": "ping", "dry_run": False, "quiet_days": quiet_days,
            "min_days_between_pings": gap_days, "candidates": len(pool),
            "pinged": counts["pinged"],
            "followed": counts["followed"] + counts["refollowed"],
            "paid_profile_skipped": counts["paid_profile"],
            "no_price_skipped": counts["no_price"],
            "stranded": stranded, "errors": errors, "cap": cap}


async def _run_like_posts(account_id: str, payload: dict, *, cap: int, dry_run: bool,
                          targets: dict, hard_skip: set) -> dict:
    post_ids = [int(x) for x in (targets.get("post_ids") or [])
                if str(x).lstrip("-").isdigit()][:cap]
    if not post_ids:
        return {"action": "like_posts", "dry_run": dry_run, "liked": 0,
                "candidates": 0, "skipped": "no_post_ids"}
    if dry_run:
        return {"action": "like_posts", "dry_run": True, "candidates": len(post_ids),
                "would_like": post_ids}
    client = await asyncio.to_thread(ax._make_client, account_id)
    liked = errors = already = 0
    for pid in post_ids:
        try:
            # OF's /favorites endpoint is a TOGGLE, not "set liked", and the
            # post payload carries no per-viewer like flag (only
            # favoritesCount / canToggleFavorite) — so we cannot ask "have I
            # liked this?". Running the rule twice therefore UNLIKED the post
            # while still reporting liked:1, and a scheduled rule alternated
            # like/unlike forever.
            #
            # Detect the direction from the count and undo an accidental
            # unlike. Costs two extra reads per post, but it only ever
            # settles on "liked", and it uses no unverified endpoint.
            before = _favorites_count(await asyncio.to_thread(client.get_post, pid))
            await asyncio.to_thread(client.like_post, pid)
            after = _favorites_count(await asyncio.to_thread(client.get_post, pid))
            if before is not None and after is not None and after < before:
                # We just removed our own like — put it back.
                await asyncio.to_thread(client.like_post, pid)
                already += 1
            else:
                liked += 1
        except Exception:
            errors += 1
            log.warning("auto_follow like_post failed account=%s post=%s",
                        account_id, pid, exc_info=True)
    return {"action": "like_posts", "dry_run": False, "candidates": len(post_ids),
            "liked": liked, "already_liked": already, "errors": errors}


async def _run_like_messages(account_id: str, payload: dict, *, cap: int, dry_run: bool,
                             targets: dict, hard_skip: set) -> dict:
    headroom = _headroom(cap)
    pool = [f for f in await _resolve_targets(account_id, targets, headroom)
            if f not in hard_skip]
    if not pool:
        return {"action": "like_messages", "dry_run": dry_run, "candidates": 0, "liked": 0}

    if dry_run:
        return {"action": "like_messages", "dry_run": True,
                "candidates": len(pool), "would_like_fans": pool[:cap]}

    client = await asyncio.to_thread(ax._make_client, account_id)
    liked = errors = no_message = already = 0
    for fid in pool:
        # already-liked consumes the cap too: it's the steady state of a
        # recurring rule (same latest message until the fan writes again) —
        # without this, a headroomed pool would burn 5×cap OF calls re-liking
        # the same messages every run.
        if liked + already >= cap:
            break
        mid = await _latest_inbound_message_id(account_id, fid)
        if mid is None:
            no_message += 1
            continue
        try:
            await asyncio.to_thread(client.like_message, mid)
            liked += 1
        except Exception as e:
            # OF 400s a repeat like; counting that as an error made a healthy
            # steady state read as total failure.
            if _is_already_liked_error(e):
                already += 1
                continue
            errors += 1
            log.warning("auto_follow like_message failed account=%s fan=%s msg=%s",
                        account_id, fid, mid, exc_info=True)
    return {"action": "like_messages", "dry_run": False, "candidates": len(pool),
            "liked": liked, "no_message": no_message, "already_liked": already,
            "errors": errors, "cap": cap}


# Dispatch table IS the action registry — validation is dict membership.
_ACTIONS = {
    "like_messages": _run_like_messages,
    "like_posts": _run_like_posts,
    "follow": _run_follow,
    "ping": _run_ping,
}


@register("auto_follow")
async def run(account_id: str, payload: dict, *, run_id: int) -> dict:
    payload = payload or {}
    action = str(payload.get("action") or "like_messages")
    runner = _ACTIONS.get(action)
    if runner is None:
        return {"skipped": "bad_action", "action": action}
    return await runner(
        account_id, payload,
        cap=_int_knob(payload, "daily_cap", _DEFAULT_DAILY_CAP, lo=0, hi=_MAX_DAILY_CAP),
        dry_run=bool(payload.get("dry_run", True)),   # default SAFE
        targets=payload.get("targets") or {},
        hard_skip=await load_hard_skip_ids(account_id),
    )
