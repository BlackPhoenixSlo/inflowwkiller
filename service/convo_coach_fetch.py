"""
convo_coach_fetch.py — the READ half of the 4-hourly convo coach
(`scripts/convo-coach.sh`).

Runs INSIDE fastt-relay on the VPS, for the same reason its older sibling
`convo_review_fetch.py` does: the production DB lives only there. It imports
that sibling — the shell ships BOTH files into the container's /tmp, and Python
puts a script's own directory on `sys.path`, so `import convo_review_fetch`
resolves to the copy sitting next to this one.

── What this answers that the reader does not ────────────────────────────────
`convo_review_fetch.py` asks *"which conversations, of all time, are worth
reading"* and ranks by revenue / volume / breakage. Useful once a month.

This asks a different, smaller question, every four hours:

    In the last few hours, which handful of threads did a fan actually REPLY
    in — and of those, which ones DIED and which ones LANDED?

Both halves matter. A thread that went nowhere shows what to fix; a thread that
closed shows what to keep. A thread where nobody answered us shows nothing at
all, which is why the empty ones are dropped rather than sampled.

── The three outcome buckets ─────────────────────────────────────────────────
`solved`    cleared money landed in the window — an unlock or a tip
`nowhere`   he talked, no money, and the thread has since stalled
`ongoing`   the last bubble is too recent to judge — SKIPPED, not reported

`ongoing` is the important one. Judging a conversation four minutes after her
last line manufactures a failure out of a fan who is simply still typing, and a
coach that cries wolf gets ignored. Nothing is classified until it has been
quiet for `--settle-min` minutes.

Nothing here writes: the DB is opened `mode=ro` and there are no network calls
at all unless `--media` is passed (off by default — the point of this loop is
to be cheap enough to run six times a day).
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any

# The shell drops this file and convo_review_fetch.py side by side in /tmp.
# sys.path[0] is already that directory when running a script by path, but say
# it out loud so an operator running this from elsewhere gets a real import
# instead of a ModuleNotFoundError twenty lines into a prod read.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import convo_review_fetch as rv  # noqa: E402  (path fix must precede it)

DB_PATH = os.environ.get("CONVO_REVIEW_DB", "/app/service/chatterly.db")

# Money that counts as "this conversation closed". Subscription rebills are
# excluded on purpose: they arrive on a timer, not because of anything she said.
MONEY_KINDS = ("ppv_message", "ppv_post", "tip")

# How the stall is described once a thread is judged dead. The wording is the
# finding's first line in the markdown, so it is written for a human, not a log.
STALL_TEXT = {
    "dead_offer": "ends on a PPV he never opened",
    "we_went_quiet": "he kept talking and nobody answered",
    "he_went_quiet": "she spoke last and he never came back",
    "no_ask": "he talked this much and was never offered anything",
    "faded": "it just petered out",
}

# Ranking weights for which dead thread is most worth reading. A fan who was
# never offered anything despite talking is the most actionable miss we have;
# a thread that merely faded is the least.
STALL_WEIGHT = {
    "no_ask": 50,
    "dead_offer": 40,
    "we_went_quiet": 35,
    "he_went_quiet": 15,
    "faded": 10,
}

# A thread is re-reviewable after this long. Without it the same loud fan is
# sampled every four hours forever and the batch stops being news.
DEFAULT_SEEN_DAYS = 14


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def sql_ts(dt: datetime) -> str:
    """SQLAlchemy stores DateTime as 'YYYY-MM-DD HH:MM:SS[.ffffff]' text, so a
    plain string compare is a correct chronological compare — as long as both
    sides are UTC and zero-padded, which this is."""
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s[:26], fmt)
        except ValueError:
            continue
    return None


# ── candidate selection ──────────────────────────────────────────────────────

def candidates(conn: sqlite3.Connection, since: str, cap: int) -> list[dict]:
    """Threads with real two-way traffic in the window, busiest first.

    The HAVING is the whole "not empty" rule: at least one inbound (he
    responded) AND at least one outbound (we said something that can be
    critiqued). A thread where we blasted and he never answered has nothing in
    it for a coach — that is a deliverability question, not a writing one.

    Capped, because everything downstream is per-thread work and a busy evening
    can put a few hundred threads in a four-hour window.
    """
    rows = rv.q(conn, """
        SELECT account_id, fan_id,
               SUM(direction = 'in')  AS in_w,
               SUM(direction = 'out') AS out_w,
               SUM(direction = 'out' AND price_cents > 0) AS priced_w,
               MAX(created_at)        AS last_at
        FROM messages
        WHERE created_at >= ? AND is_unsent = 0
        GROUP BY account_id, fan_id
        HAVING in_w >= 1 AND out_w >= 1
        ORDER BY in_w DESC
        LIMIT ?
    """, (since, cap))
    return [{"account_id": r["account_id"], "fan_id": r["fan_id"],
             "in_window": r["in_w"] or 0, "out_window": r["out_w"] or 0,
             "priced_window": r["priced_w"] or 0,
             "last_at": r["last_at"]} for r in rows]


def money_in_window(conn: sqlite3.Connection, acct: str, fan: int,
                    since: str) -> dict:
    """Cleared money on this thread inside the window.

    `occurred_at` and not `cleared_at`: OF clears on its own schedule, so a
    purchase made twenty minutes ago is still 'pending' for days. The question
    here is "did he buy during this conversation", which is what occurred_at
    answers. `chargedback` is excluded from the total but reported, because a
    thread that closed and then reversed is the most instructive kind there is.
    """
    r = rv.q(conn, f"""
        SELECT SUM(CASE WHEN status != 'chargedback' THEN amount_cents ELSE 0 END) AS rev,
               SUM(status != 'chargedback')      AS n,
               SUM(status = 'chargedback')       AS cb,
               SUM(kind = 'tip')                 AS tips
        FROM transactions
        WHERE account_id = ? AND fan_id = ? AND occurred_at >= ?
          AND kind IN {MONEY_KINDS}
    """, (acct, fan, since))
    row = r[0] if r else None
    return {"cents": (row["rev"] if row else 0) or 0,
            "n": (row["n"] if row else 0) or 0,
            "chargebacks": (row["cb"] if row else 0) or 0,
            "tips": (row["tips"] if row else 0) or 0}


def tail(conn: sqlite3.Connection, acct: str, fan: int, n: int = 12) -> list[sqlite3.Row]:
    """The last `n` bubbles, oldest-first. Scoped to one thread, so this rides
    ix_messages_account_fan_time instead of the window-function scan the
    all-threads reader needs."""
    rows = rv.q(conn, """
        SELECT message_id, direction, body, price_cents, is_paid, is_tip,
               media_count, automation_kind, created_at
        FROM messages
        WHERE account_id = ? AND fan_id = ? AND is_unsent = 0
        ORDER BY created_at DESC, message_id DESC
        LIMIT ?
    """, (acct, fan, n))
    return list(reversed(rows))


def thread_total(conn: sqlite3.Connection, acct: str, fan: int) -> int:
    r = rv.q(conn, "SELECT COUNT(*) AS c FROM messages WHERE account_id=? AND fan_id=?",
             (acct, fan))
    return (r[0]["c"] if r else 0) or 0


def classify(cand: dict, last: list[sqlite3.Row], money: dict,
             settle_min: int, now: datetime) -> tuple[str, str]:
    """(bucket, stall_reason). `stall_reason` is '' for anything but `nowhere`.

    Order matters. Money first — a thread that bought is `solved` even if it
    then went quiet, because going quiet after paying is not a failure. Then
    the settle gate. Only what survives both gets a post-mortem.
    """
    if money["cents"] > 0 or money["n"] > 0:
        return "solved", ""

    last_at = parse_ts(cand["last_at"])
    if last_at is not None and (now - last_at) < timedelta(minutes=settle_min):
        return "ongoing", ""

    if not last:
        return "ongoing", ""

    newest = last[-1]

    # Ends on an offer he never opened. The single most expensive silence we
    # have: the work was done, the media was sent, and the close missed.
    if (newest["direction"] == "out" and (newest["price_cents"] or 0) > 0
            and not newest["is_paid"]):
        return "nowhere", "dead_offer"

    # Trailing run of unanswered inbound — HE kept going, we stopped. Two is
    # enough here (the all-time reader uses three) because this window is
    # hours, not months: two unanswered messages in one evening is a drop.
    run_in = 0
    for r in reversed(last):
        if r["direction"] == "in":
            run_in += 1
        else:
            break
    if run_in >= 2:
        return "nowhere", "we_went_quiet"

    # He talked properly and was never offered a thing. Not a failure of the
    # writing — a failure of the ask ever being made.
    #
    # Both halves must be measured over the SAME stretch. The first cut counted
    # his messages across the whole window but looked for the offer only in the
    # twelve-message tail, so a thread that was offered to fifteen bubbles ago
    # and then chatted on was reported as "he talked this much and was never
    # offered anything" — a false accusation, and `no_ask` carries the heaviest
    # ranking weight, so it would have led batches with it.
    if cand["in_window"] >= 3 and not cand["priced_window"]:
        return "nowhere", "no_ask"

    if run_in == 1:
        return "nowhere", "we_went_quiet"
    if newest["direction"] == "out":
        return "nowhere", "he_went_quiet"
    return "nowhere", "faded"


def window_signals(conn: sqlite3.Connection, acct: str, fan: int,
                   since: str) -> tuple[dict[str, int], list[int]]:
    """The reader's breakage lexicons, re-asked for ONE thread in the window.

    Same phrase lists, so a callout means the same thing in both tools. Returns
    the counts (for ranking) and the message ids (so the viewer can ring the
    exact bubble).
    """
    sig: dict[str, int] = {}
    hits: list[int] = []
    for name, needles in (("bot_callout", rv.BOT_CALLOUT),
                          ("repeat_callout", rv.REPEAT_CALLOUT),
                          ("angry", rv.ANGRY)):
        clause, params = rv._like_clause("lower(body)", needles)
        rows = rv.q(conn, f"""
            SELECT message_id FROM messages
            WHERE account_id = ? AND fan_id = ? AND direction = 'in'
              AND body != '' AND created_at >= ? AND ({clause})
        """, [acct, fan, since] + params)
        if rows:
            sig[name] = len(rows)
            hits.extend(r["message_id"] for r in rows)

    # The same outbound paragraph twice inside one window. The all-time reader
    # needs three to call it a loop; inside a few hours, twice is already a fan
    # reading the same sentence twice.
    rows = rv.q(conn, """
        SELECT body, COUNT(*) AS c FROM messages
        WHERE account_id = ? AND fan_id = ? AND direction = 'out'
          AND is_unsent = 0 AND LENGTH(body) >= 30 AND created_at >= ?
        GROUP BY body HAVING c >= 2
    """, (acct, fan, since))
    if rows:
        sig["loop_out"] = len(rows)
        for r in rows:
            for h in rv.q(conn, """
                SELECT message_id FROM messages
                WHERE account_id=? AND fan_id=? AND direction='out' AND body=?
                  AND created_at >= ?
            """, (acct, fan, r["body"], since)):
                hits.append(h["message_id"])
    return sig, hits


def why_lines(t: dict) -> list[str]:
    """The human-readable reason this thread is in the batch. The markdown uses
    it as the heading, so it has to stand alone without the numbers beside it."""
    why: list[str] = []
    if t["outcome"] == "solved":
        bits = f"${t['window_cents']/100:,.2f} landed"
        if t["window_tips"]:
            bits += f" ({t['window_tips']} tip{'s' if t['window_tips'] != 1 else ''})"
        why.append(bits)
        if t["window_chargebacks"]:
            why.append("…and then charged back")
    else:
        why.append(STALL_TEXT.get(t["stall"], "went nowhere"))
    why.append(f"{t['in_window']} message{'s' if t['in_window'] != 1 else ''} from him")
    for name in sorted(t["signals"], key=lambda n: -rv.BREAK_WEIGHTS.get(n, 0)):
        if name in rv.REASON_TEXT:
            why.append(rv.REASON_TEXT[name])
    return why


def coach_score(t: dict) -> int:
    """How much is there to learn here. Breakage outranks the stall shape, and
    a fan who typed a lot outranks one who typed once — a longer exchange gives
    the reviewer more evidence per thread read."""
    s = STALL_WEIGHT.get(t.get("stall", ""), 0)
    s += sum(rv.BREAK_WEIGHTS.get(n, 0) * min(c, 3) for n, c in t["signals"].items())
    s += min(t["in_window"], 10) * 3
    return s


def pick(threads: list[dict], want_min: int, want_max: int, want_solved: int,
         per_account: int) -> list[dict]:
    """The batch: mostly failures, a couple of wins, never one account's night.

    The per-account cap is load-bearing. One busy model can own an entire
    four-hour window, and a coach that only ever reads one account teaches you
    about one account.
    """
    solved = sorted((t for t in threads if t["outcome"] == "solved"),
                    key=lambda t: -t["window_cents"])
    nowhere = sorted((t for t in threads if t["outcome"] == "nowhere"),
                     key=lambda t: -coach_score(t))

    chosen: list[dict] = []
    used: dict[str, int] = {}

    def take(pool: list[dict], limit: int) -> None:
        for t in pool:
            if len(chosen) >= want_max or limit <= 0:
                return
            if used.get(t["account_id"], 0) >= per_account:
                continue
            chosen.append(t)
            used[t["account_id"]] = used.get(t["account_id"], 0) + 1
            limit -= 1

    take(solved, want_solved)
    take(nowhere, want_max)
    # Short of the floor only because the per-account cap bit? Relax it rather
    # than hand back a two-thread batch — one account's threads still teach
    # something, and an empty report teaches nothing.
    if len(chosen) < want_min:
        for t in solved + nowhere:
            if len(chosen) >= want_min:
                break
            if t not in chosen:
                chosen.append(t)
    return chosen


def load_seen(path: str, days: int) -> set[str]:
    """Thread keys reviewed recently enough not to repeat. Shape on disk is
    {"acct:fan": "2026-09-11T08:00:00Z"} — a bare list is also accepted so an
    operator can hand-write one."""
    if not path or not os.path.exists(path):
        return set()
    try:
        with open(path) as fh:
            data = json.load(fh)
    except Exception:  # noqa: BLE001
        return set()
    if isinstance(data, list):
        return set(data)
    cutoff = now_utc() - timedelta(days=days)
    out = set()
    for k, ts in (data or {}).items():
        seen_at = parse_ts((ts or "").replace("T", " ").replace("Z", ""))
        if seen_at is None or seen_at >= cutoff:
            out.add(k)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="-", help="JSON destination ('-' = stdout)")
    ap.add_argument("--hours", type=float, default=4.0,
                    help="the window: how far back a fan reply counts (default 4)")
    ap.add_argument("--widen", default="12,24,72",
                    help="fallback windows, in hours, tried in order when the "
                         "first one is too quiet to fill the batch")
    ap.add_argument("--min", type=int, default=3, dest="want_min")
    ap.add_argument("--max", type=int, default=6, dest="want_max")
    ap.add_argument("--solved", type=int, default=2,
                    help="how many of the batch may be wins (default 2)")
    ap.add_argument("--per-account", type=int, default=2)
    ap.add_argument("--settle-min", type=int, default=45,
                    help="a thread this quiet counts as finished (default 45m)")
    ap.add_argument("--min-msgs", type=int, default=4,
                    help="skip threads shorter than this in total")
    ap.add_argument("--window", type=int, default=60,
                    help="bubbles of history to keep per thread (default 60)")
    ap.add_argument("--context", type=int, default=8)
    ap.add_argument("--candidate-cap", type=int, default=400)
    ap.add_argument("--skip-accounts", default=os.environ.get("CONVO_COACH_SKIP", ""),
                    help="comma-separated account ids or nicknames to leave out. "
                         "Point it at the test accounts: a bot-to-bot probe "
                         "thread scores like a real miss (a scripted fan talks a "
                         "lot and is never sold to) and will crowd out the "
                         "conversations you actually wanted read.")
    ap.add_argument("--seen", default="", help="JSON of thread keys already reviewed")
    ap.add_argument("--seen-days", type=int, default=DEFAULT_SEEN_DAYS)
    args = ap.parse_args()

    conn = rv.connect(DB_PATH)
    accounts = rv.account_names(conn)
    seen = load_seen(args.seen, args.seen_days)
    now = now_utc()

    # Matched against the id AND the nickname, so `--skip-accounts Jaka` works
    # without anyone having to look up an account id first.
    skip = {s.strip().lower() for s in args.skip_accounts.split(",") if s.strip()}
    if skip:
        rv.log(f"→ skipping accounts: {', '.join(sorted(skip))}")

    windows = [args.hours] + [float(x) for x in args.widen.split(",") if x.strip()]
    threads: list[dict] = []
    used_hours = args.hours
    since = ""

    for hours in windows:
        used_hours = hours
        since = sql_ts(now - timedelta(hours=hours))
        rv.log(f"→ window {hours:g}h (since {since}Z)")
        found: list[dict] = []
        for cand in candidates(conn, since, args.candidate_cap):
            k = rv.key(cand["account_id"], cand["fan_id"])
            if k in seen:
                continue
            acct_id = cand["account_id"]
            if (acct_id.lower() in skip
                    or accounts.get(acct_id, "").lower() in skip):
                continue
            total = thread_total(conn, cand["account_id"], cand["fan_id"])
            if total < args.min_msgs:
                continue
            money = money_in_window(conn, cand["account_id"], cand["fan_id"], since)
            last = tail(conn, cand["account_id"], cand["fan_id"])
            bucket, stall = classify(cand, last, money, args.settle_min, now)
            if bucket == "ongoing":
                continue
            sig, hits = window_signals(conn, cand["account_id"], cand["fan_id"], since)
            t = dict(cand)
            t.update({
                "key": k, "outcome": bucket, "stall": stall, "signals": sig,
                "hit_ids": hits,
                "window_cents": money["cents"], "window_tips": money["tips"],
                "window_chargebacks": money["chargebacks"],
                "account_name": accounts.get(cand["account_id"], cand["account_id"]),
                "buckets": [bucket],
                # The all-time reader's fields, so one renderer serves both.
                # `total` is not optional: the viewer's header reads
                # t.total.toLocaleString() unguarded, so omitting it throws on
                # the first click and the page opens to a dead pane.
                "total": total,
                "revenue_cents": money["cents"], "inbound": cand["in_window"],
                "break_score": 0,
            })
            found.append(t)
        rv.log(f"  {len(found)} judged ({sum(1 for t in found if t['outcome']=='solved')}"
               f" solved / {sum(1 for t in found if t['outcome']=='nowhere')} nowhere)")
        threads = found
        if len(found) >= args.want_min:
            break

    chosen = pick(threads, args.want_min, args.want_max, args.solved, args.per_account)
    rv.log(f"→ picked {len(chosen)}")

    rv.fan_names(conn, chosen)
    since_dt = parse_ts(since)
    for t in chosen:
        hit_ids = set(t.pop("hit_ids", []))
        t["messages"] = rv.load_messages(conn, t, args.window, args.context, hit_ids)
        # Mark what happened INSIDE the window. The history above it is context
        # the reviewer needs, but it is not what this batch is judging, and a
        # reviewer who cannot tell the two apart writes findings about a line
        # she sent last Tuesday.
        for m in t["messages"]:
            if "ts" in m:
                mt = parse_ts(m["ts"])
                m["in_window"] = bool(since_dt and mt and mt >= since_dt)
        t["why"] = why_lines(t)
        t["break_score"] = coach_score(t)

    payload = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
        "kind": "convo-coach",
        "title": "Convo coach",
        "window_hours": used_hours,
        "window_since": since,
        "threads": chosen,
        "media": {}, "media_meta": {},
        # Payload-driven tabs — convo_review_render.py falls back to its own
        # selling/long/breaks set when a payload omits this.
        "tabs": [{"f": "all", "label": "All"},
                 {"f": "nowhere", "label": "🧊 Went nowhere", "chip": "brk"},
                 {"f": "solved", "label": "💰 Closed", "chip": "sell"}],
        "counts": {"judged": len(threads), "picked": len(chosen)},
    }
    blob = json.dumps(payload, separators=(",", ":"))
    if args.out == "-":
        sys.stdout.write(blob)
    else:
        with open(args.out, "w") as fh:
            fh.write(blob)
        rv.log(f"wrote {args.out} ({len(blob)/1e6:.2f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
