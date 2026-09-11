"""
convo_check_fetch.py — dump N random conversations, last 20 messages each,
with the mechanical faults already marked.

Runs INSIDE fastt-relay (the prod DB lives only there). Read-only: `mode=ro`,
SELECTs only, no OF calls. Prints plain text to stdout — no JSON, no files,
nothing to clean up.

RANDOM on purpose. convo_review and convo_coach both rank threads by how broken
they look, which can tell you a defect exists and can never tell you the typical
conversation is fine. This draws uniformly among threads with real two-way
traffic, so what you read is representative.

── The division of labour ────────────────────────────────────────────────────
This file marks only what a machine can be *certain* of, and never guesses at
intent:

  BLAST     `mass_run_id` is set — this line was written for a list, not for him
  REPEAT    she already sent this exact line in this thread
  BARE      her whole message normalises to a placeholder token
  LEAK?     her text carries image-caption or moderation phrasing
  ECHO n    the identical line also went to sampled conversation n

Everything else — did she answer the question, did she bluff, was the price
move right — is judgement, and stays with the reader. A marker here is evidence,
not a verdict: a BLAST is only a fault if it landed mid-conversation, and this
file deliberately does not decide that.
"""
from __future__ import annotations

import argparse
import html as _html
import os
import random
import re
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone

DB = os.environ.get("CONVO_REVIEW_DB", "/app/service/chatterly.db")

_BR = re.compile(r"(?i)<br\s*/?>")
_P = re.compile(r"(?i)</p\s*>")
_TAG = re.compile(r"<[^>]+>")

# Placeholder tokens that have actually been sent live. Matched on the
# HTML-STRIPPED body: OF stores `<p>test</p>`, so a check against the raw
# column matches nothing and reports a clean bill of health.
BARE = {"test", "testing", "asd", "asdf", "asdfasdf", "asdsfd", "bnmm",
        "x", "a", "123", "aaa", "hello world", "."}

# Phrases that belong to an image-description or its moderation pass and must
# never reach a fan. Seeded from a real leak: a vision caption shipped whole,
# moderation check included, truncated mid-word.
LEAK = ("the image is", "sfw", "nsfw", "no other people", "a photo of",
        "a selfie of", "the standout detail", "appears to be a",
        "no rings", "no children", "image shows", "this image")

_PUNCT = re.compile(r"[^a-z0-9 ]+")

# Characters of shared opening text that make two messages "the same copy".
# 40 is long enough that ordinary phrasing does not collide and short enough to
# catch a blast caption whose last clause was swapped per fan.
HEAD = 40


def text(s):
    if not s:
        return ""
    s = _TAG.sub("", _P.sub("\n", _BR.sub("\n", s)))
    return re.sub(r"\s+", " ", _html.unescape(s)).strip()


def norm(s):
    """For equality tests — case, punctuation and spacing carry no meaning when
    asking 'is this the same line again'."""
    return _PUNCT.sub("", text(s).lower()).strip()


def ts(s):
    for f in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s[:26], f)
        except (ValueError, TypeError):
            pass
    return None


def ago(delta):
    m = int(delta.total_seconds() // 60)
    if m < 60:
        return f"{m}m"
    if m < 60 * 48:
        return f"{m // 60}h{m % 60:02d}m"
    return f"{m // 1440}d"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=4, help="conversations to pull")
    ap.add_argument("--tail", type=int, default=20, help="messages per convo")
    ap.add_argument("--hours", type=float, default=24.0,
                    help="a fan must have replied this recently to be eligible")
    ap.add_argument("--min-fan", type=int, default=4,
                    help="fan messages required INSIDE the shown tail (default 4)")
    ap.add_argument("--min-pairs", type=int, default=3,
                    help="fan-said-then-she-answered turns required (default 3)")
    ap.add_argument("--per-account", type=int, default=2,
                    help="cap per account so one busy model can't own the draw")
    ap.add_argument("--gap-min", type=int, default=45,
                    help="show an elapsed marker at gaps this long (default 45m)")
    ap.add_argument("--skip-accounts", default=os.environ.get("CONVO_COACH_SKIP", ""))
    ap.add_argument("--seed", default="", help="reproduce a previous draw")
    args = ap.parse_args()

    if args.seed:
        random.seed(args.seed)

    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=30)
    c.row_factory = sqlite3.Row
    accounts = {r["id"]: (r["nickname"] or r["id"])
                for r in c.execute("SELECT id, nickname FROM accounts")}
    skip = {s.strip().lower() for s in args.skip_accounts.split(",") if s.strip()}
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    rows = c.execute("""
        SELECT account_id, fan_id
        FROM messages
        WHERE created_at >= datetime('now', ?) AND is_unsent = 0
        GROUP BY account_id, fan_id
        HAVING SUM(direction='in') >= 1 AND SUM(direction='out') >= 1
    """, (f"-{args.hours} hours",)).fetchall()

    # Skip on BOTH sides. Our accounts message each other, so a test account
    # shows up as a `fan_id` too — the first live draw returned one of our own
    # models chatting another of our own accounts, and an account-side-only
    # filter let it straight through.
    ours = {str(a).lower() for a in accounts}
    pool = [r for r in rows
            if r["account_id"].lower() not in skip
            and accounts.get(r["account_id"], "").lower() not in skip
            and str(r["fan_id"]).lower() not in skip
            and str(r["fan_id"]) not in ours]

    def tail_of(acct, fan):
        return list(reversed(c.execute("""
            SELECT direction, body, price_cents, is_paid, is_tip, media_count,
                   image_desc, automation_kind, mass_run_id, created_at
            FROM messages WHERE account_id=? AND fan_id=? AND is_unsent=0
            ORDER BY created_at DESC, message_id DESC LIMIT ?
        """, (acct, fan, args.tail)).fetchall()))

    # ONE fan reply is not a conversation. A scripted opener — six of her lines
    # around a single "hi mommy" — passes any inbound>=1 test and contains no
    # answer of hers that can be right or wrong. Qualify on the tail we SHOW.
    tails, one_sided = {}, 0
    for r in pool:
        t = tail_of(r["account_id"], r["fan_id"])
        n_fan = sum(1 for m in t if m["direction"] == "in")
        pairs = sum(1 for a, b in zip(t, t[1:])
                    if a["direction"] == "in" and b["direction"] == "out")
        if n_fan >= args.min_fan and pairs >= args.min_pairs:
            tails[(r["account_id"], r["fan_id"])] = t
        else:
            one_sided += 1

    if not tails:
        print(f"(no real back-and-forth in the last {args.hours:g}h — "
              f"{one_sided} threads were one-sided. Try --hours 48 or --min-fan 2.)")
        return 0

    # Cap per account. Without it one busy model owns the draw: three of the
    # first four pulls were the same account, which reads as a model-wide
    # problem when it is a sampling artifact.
    picked, used = [], defaultdict(int)
    for k in random.sample(list(tails), len(tails)):
        if used[k[0]] < args.per_account:
            picked.append(k)
            used[k[0]] += 1
        if len(picked) == args.n:
            break

    # The same outbound line in two different conversations is a blast, whether
    # or not mass_run_id says so. Cross-referencing the sample is the only way
    # to see it — it is invisible inside any single thread.
    seen_in = defaultdict(set)
    head_in = defaultdict(set)
    for i, k in enumerate(picked, 1):
        for m in tails[k]:
            if m["direction"] == "out" and len(norm(m["body"])) >= 12:
                seen_in[norm(m["body"])].add(i)
                # Blast copy is written once and then personalised at the tail:
                # "red lace, pulled aside, staring right at you" went to two
                # fans in the same minute under two different run ids, and only
                # the closing sentence differed — exact match saw nothing.
                if len(norm(m["body"])) >= HEAD:
                    head_in[norm(m["body"])[:HEAD]].add(i)

    print(f"{len(pool)} threads had a fan reply in the last {args.hours:g}h; "
          f"{one_sided} one-sided and dropped; {len(tails)} with real "
          f"back-and-forth. Showing {len(picked)} at random "
          f"(max {args.per_account}/account).\n")

    total_flags = defaultdict(int)

    for i, (acct, fan) in enumerate(picked, 1):
        f = c.execute("""SELECT of_username, of_display_name, custom_nickname,
                                real_name, lifetime_spend_cents
                         FROM fans WHERE account_id=? AND fan_id=?""",
                      (acct, fan)).fetchone()
        name = ((f["custom_nickname"] if f else None) or (f["real_name"] if f else None)
                or (f["of_display_name"] if f else None)
                or (f["of_username"] if f else None) or f"fan {fan}")
        spend = (f["lifetime_spend_cents"] if f else 0) or 0

        msgs = tails[(acct, fan)]
        n_fan = sum(1 for m in msgs if m["direction"] == "in")

        print(f"═══ {i}. {text(name)} — {text(accounts.get(acct, acct))} "
              f"(${spend/100:,.0f} lifetime · {n_fan} of these {len(msgs)} are his) "
              f"· {acct}:{fan}")

        said, prev_t = set(), None
        for m in msgs:
            t = ts(m["created_at"])
            if prev_t and t and (t - prev_t).total_seconds() > args.gap_min * 60:
                print(f"          ⏱  {ago(t - prev_t)} later")
            prev_t = t or prev_t

            who = "FAN" if m["direction"] == "in" else "HER"
            kind = f" [{m['automation_kind']}]" if m["automation_kind"] else ""
            body = text(m["body"])
            n = norm(m["body"])

            flags = []
            if m["direction"] == "out":
                if m["mass_run_id"]:
                    flags.append(f"BLAST run={m['mass_run_id']}")
                if n and n in said:
                    flags.append("REPEAT")
                if n in BARE:
                    flags.append("BARE")
                low = body.lower()
                if any(p in low for p in LEAK):
                    flags.append("LEAK?")
                others = sorted(seen_in.get(n, set()) - {i})
                if others:
                    flags.append("ECHO conv " + ",".join(map(str, others)))
                elif len(n) >= HEAD:
                    heads = sorted(head_in.get(n[:HEAD], set()) - {i})
                    if heads:
                        flags.append("ECHO-HEAD conv " + ",".join(map(str, heads)))
                if n:
                    said.add(n)
            for fl in flags:
                total_flags[fl.split(" ")[0]] += 1

            tags = []
            if m["media_count"]:
                d = text(m["image_desc"])
                if m["direction"] == "in":
                    tags.append(f"{m['media_count']} media"
                                + (f" ({d[:70]})" if d else " — NO DESCRIPTION, "
                                   "she could not see this"))
                else:
                    tags.append(f"{m['media_count']} media")
            if m["price_cents"]:
                tags.append(f"${m['price_cents']/100:.2f} "
                            + ("OPENED" if m["is_paid"] else "NOT OPENED"))
            if m["is_tip"]:
                tags.append("tip")

            line = f"  {m['created_at'][5:16]}  {who}{kind}: {body or '(no text)'}"
            if tags:
                line += "   << " + " · ".join(tags)
            if flags:
                line += "   ‼ " + " ‼ ".join(flags)
            print(line)

        # Who is waiting, and for how long. This is the difference between a
        # thread still in flight and one that was dropped.
        last = msgs[-1]
        lt = ts(last["created_at"])
        if lt:
            who_last = "HE spoke last" if last["direction"] == "in" else "she spoke last"
            note = f"  └─ {who_last}, {ago(now - lt)} ago"
            if last["direction"] == "in":
                note += "  ← nothing has come back yet"
            print(note)
        print()

    if total_flags:
        print("mechanical flags across the sample: "
              + " · ".join(f"{k}×{v}" for k, v in sorted(total_flags.items())))
        print("(evidence, not verdicts — a BLAST is only a fault if it landed "
              "mid-conversation)")
    else:
        print("no mechanical flags in this sample.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
