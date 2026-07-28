"""
convo_review_fetch.py — the READ half of the convo reader (`scripts/convo-review.sh`).

Runs INSIDE fastt-relay on the VPS, because two things it needs live only there:
the production DB, and an OFClient whose egress IP matches what OF signed.

It answers one question — *which old conversations are worth reading, and what
did they look like* — and answers it as a JSON blob on stdout (or `--out`).
Nothing here writes: the DB is opened `mode=ro`, and the only network calls are
GETs.

── Why the media is BYTES and not urls ────────────────────────────────────────
Every OF CDN url is signed with a policy that pins BOTH an expiry (~2 days) and
a source IP (the account's proxy, e.g. 194.31.182.8/32). So the `thumb_url` we
stored in `message_media` months ago is dead twice over: expired, and unusable
from a laptop even if it weren't. A viewer that renders stored urls renders
nothing.

The only durable copy is the bytes. So for each conversation we re-page OF for
freshly-signed urls, fetch each still through the account's own client, downscale
it, and hand back base64. The caller keeps those on disk forever, keyed by media
id — which is also why re-runs are cheap: `--have` lists what the caller already
holds and we skip re-fetching it.

── The three buckets ──────────────────────────────────────────────────────────
`selling`  threads that made money, by cleared revenue
`long`     real back-and-forth, by how much HE actually typed
`breaks`   threads that went wrong — he called the bot out, the same line went
           out three times, he charged back, or he talked and nobody answered

A thread can sit in more than one bucket, and carries the human-readable reason
it landed there (`why`), so the viewer never has to re-derive the judgement.
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import os
import sqlite3
import sys
import time
from collections import defaultdict
from typing import Any, Iterable

DB_PATH = os.environ.get("CONVO_REVIEW_DB", "/app/service/chatterly.db")

# This file is copied to the container's /tmp and run from there, so sys.path[0]
# is /tmp — and `docker exec -w /app/service` does NOT help, because Python only
# puts the CWD on the path for `-c`, never for a script path. Without this every
# `import automation_executor` fails at media time and the run silently produces
# a text-only page (it did, on the first real run).
APP_DIR = os.environ.get("CONVO_REVIEW_APP_DIR", "/app/service")
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

# ── breakage lexicons ────────────────────────────────────────────────────────
# Substring tests on lowercased inbound bodies. Deliberately narrow: a loose
# '%bot%' matches "bottom" and "both", and a loose '%stop%' matches "stop by".
BOT_CALLOUT = (
    "are you a bot", "is this a bot", "you're a bot", "youre a bot", "u a bot",
    "talking to a bot", "are you real", "are you ai", "is this ai", "u ai",
    "chatgpt", "not a real person", "real person or", "am i talking to a robot",
    "are you a robot", "this is a bot",
)
REPEAT_CALLOUT = (
    "you already", "u already", "same message", "keep repeating", "repeating yourself",
    "said that already", "asked you that", "you sent that", "already told you",
    "already asked", "already said",
)
ANGRY = (
    "scam", "scammer", "refund", "rip off", "ripoff", "ripping me off",
    "leave me alone", "stop messaging", "stop sending", "unsubscrib",
    "report you", "waste of money", "fuck off", "blocked you", "you don't listen",
    "you dont listen", "not paying", "chargeback",
)

# Weights only decide the ORDER of the breaks tab. A chargeback outranks a
# grumble; a bot callout outranks a repeat; silence is the weakest signal
# because it is the most frequently innocent (he simply stopped).
BREAK_WEIGHTS = {
    "chargeback": 100,
    "bot_callout": 60,
    "angry": 45,
    "repeat_callout": 35,
    "loop_out": 30,
    "ghosted": 12,
    "dead_offer": 10,
}

MONEY_KINDS = ("ppv_message", "ppv_post", "tip")

# Mirrors attribution._MASS_PLACEHOLDER_BASE: message ids at or above this are
# local mass-run placeholders, never real OF message ids.
MASS_PLACEHOLDER_BASE = 5_000_000_000_000_000


def log(msg: str) -> None:
    """Progress goes to stderr — stdout carries the JSON payload."""
    print(msg, file=sys.stderr, flush=True)


# ── DB ───────────────────────────────────────────────────────────────────────

def connect(path: str) -> sqlite3.Connection:
    """Read-only handle on the live DB.

    `mode=ro` on the URI, opened from INSIDE the container while the relay holds
    the same file. A host-side sqlite3 against this WAL DB false-reports
    "malformed" — same reason scripts/prod-read.sh runs in here.
    """
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def q(conn: sqlite3.Connection, sql: str, args: Iterable = ()) -> list[sqlite3.Row]:
    return conn.execute(sql, tuple(args)).fetchall()


def key(account_id: str, fan_id: int) -> str:
    return f"{account_id}:{fan_id}"


# ── ranking ──────────────────────────────────────────────────────────────────

def thread_stats(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """One row per (account, fan): volume, media, span. ~8k rows."""
    rows = q(conn, """
        SELECT account_id, fan_id,
               COUNT(*)                        AS total,
               SUM(direction = 'in')           AS inbound,
               SUM(direction = 'out')          AS outbound,
               SUM(media_count > 0)            AS media_msgs,
               SUM(is_paid = 1)                AS unlocked,
               MIN(created_at)                 AS first_at,
               MAX(created_at)                 AS last_at
        FROM messages
        GROUP BY account_id, fan_id
    """)
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        out[key(r["account_id"], r["fan_id"])] = {
            "account_id": r["account_id"], "fan_id": r["fan_id"],
            "total": r["total"] or 0, "inbound": r["inbound"] or 0,
            "outbound": r["outbound"] or 0, "media_msgs": r["media_msgs"] or 0,
            "unlocked": r["unlocked"] or 0,
            "first_at": r["first_at"], "last_at": r["last_at"],
            "revenue_cents": 0, "buys": 0, "tips": 0, "chargebacks": 0,
            "signals": {},
        }
    return out


def add_revenue(conn: sqlite3.Connection, stats: dict[str, dict]) -> None:
    """Cleared money per thread, plus the chargeback count (a breaks signal)."""
    rows = q(conn, f"""
        SELECT account_id, fan_id,
               SUM(CASE WHEN status = 'cleared' THEN amount_cents ELSE 0 END) AS rev,
               SUM(status = 'cleared' AND kind IN ('ppv_message','ppv_post'))  AS buys,
               SUM(status = 'cleared' AND kind = 'tip')                        AS tips,
               SUM(status IN ('chargedback','refund_pending'))                 AS cb
        FROM transactions
        WHERE fan_id IS NOT NULL AND kind IN {MONEY_KINDS}
        GROUP BY account_id, fan_id
    """)
    for r in rows:
        st = stats.get(key(r["account_id"], r["fan_id"]))
        if st is None:
            continue
        st["revenue_cents"] = r["rev"] or 0
        st["buys"] = r["buys"] or 0
        st["tips"] = r["tips"] or 0
        st["chargebacks"] = r["cb"] or 0
        if st["chargebacks"]:
            st["signals"]["chargeback"] = st["chargebacks"]


def _like_clause(col: str, needles: Iterable[str]) -> tuple[str, list[str]]:
    """(sql, params). Bound, not interpolated — half these phrases contain an
    apostrophe ("you're a bot"), which is a syntax error inline."""
    ns = list(needles)
    return " OR ".join([f"{col} LIKE ?"] * len(ns)), [f"%{n}%" for n in ns]


def add_text_signals(conn: sqlite3.Connection, stats: dict[str, dict],
                     hits: dict[str, list[int]]) -> None:
    """Inbound callouts. Records both the count (for ranking) and the message
    ids (so the viewer can ring the exact bubble he said it in)."""
    for name, needles in (("bot_callout", BOT_CALLOUT),
                          ("repeat_callout", REPEAT_CALLOUT),
                          ("angry", ANGRY)):
        clause, params = _like_clause("lower(body)", needles)
        rows = q(conn, f"""
            SELECT account_id, fan_id, message_id
            FROM messages
            WHERE direction = 'in' AND body != '' AND ({clause})
        """, params)
        for r in rows:
            k = key(r["account_id"], r["fan_id"])
            st = stats.get(k)
            if st is None:
                continue
            st["signals"][name] = st["signals"].get(name, 0) + 1
            hits.setdefault(k, []).append(r["message_id"])


def add_loop_signal(conn: sqlite3.Connection, stats: dict[str, dict],
                    hits: dict[str, list[int]]) -> None:
    """The same outbound line sent to the SAME fan 3+ times — the AI stuck in a
    groove. Grouped per fan on purpose: one caption reused across a mass blast is
    normal, the same paragraph three times in ONE thread is not. Unsent rows are
    excluded (auto-unsend retires mass copy by design, not by failure)."""
    rows = q(conn, """
        SELECT account_id, fan_id, body, COUNT(*) AS c
        FROM messages
        WHERE direction = 'out' AND is_unsent = 0 AND LENGTH(body) >= 30
        GROUP BY account_id, fan_id, body
        HAVING c >= 3
    """)
    worst: dict[str, tuple[int, str]] = {}
    for r in rows:
        k = key(r["account_id"], r["fan_id"])
        st = stats.get(k)
        if st is None:
            continue
        st["signals"]["loop_out"] = st["signals"].get("loop_out", 0) + 1
        if r["c"] > worst.get(k, (0, ""))[0]:
            worst[k] = (r["c"], r["body"])
    # Pull the ids of the repeated bubbles so the viewer can mark them.
    for k, (_c, body) in worst.items():
        acct, fan = k.split(":", 1)
        for r in q(conn, """
            SELECT message_id FROM messages
            WHERE account_id = ? AND fan_id = ? AND direction = 'out' AND body = ?
        """, (acct, int(fan), body)):
            hits.setdefault(k, []).append(r["message_id"])


def add_tail_signals(conn: sqlite3.Connection, stats: dict[str, dict]) -> None:
    """How the conversation ENDED — the two silent failures.

    `ghosted`     it ends on 3+ unanswered inbound messages: he kept talking,
                  nothing came back.
    `dead_offer`  it ends on a PPV he never unlocked, with no reply after it.

    The ROW_NUMBER filter matters: the naive form ships all 600k+ rows into
    Python to throw away all but six per thread, and this runs inside the live
    relay container.
    """
    rows = q(conn, """
        SELECT account_id, fan_id, direction, is_paid, price_cents FROM (
            SELECT account_id, fan_id, direction, is_paid, price_cents,
                   ROW_NUMBER() OVER (PARTITION BY account_id, fan_id
                                      ORDER BY created_at DESC, message_id DESC) AS rn
            FROM messages
        ) WHERE rn <= 6
        ORDER BY account_id, fan_id, rn
    """)
    tail: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for r in rows:
        tail[key(r["account_id"], r["fan_id"])].append(r)
    for k, last in tail.items():
        st = stats.get(k)
        if st is None:
            continue
        run_in = 0
        for r in last:
            if r["direction"] == "in":
                run_in += 1
            else:
                break
        if run_in >= 3:
            st["signals"]["ghosted"] = run_in
        newest = last[0]
        if (newest["direction"] == "out" and (newest["price_cents"] or 0) > 0
                and not newest["is_paid"]):
            st["signals"]["dead_offer"] = 1


def break_score(st: dict) -> int:
    return sum(BREAK_WEIGHTS.get(n, 0) * min(c, 4) for n, c in st["signals"].items())


REASON_TEXT = {
    "chargeback": "charged back",
    "bot_callout": "called the bot out",
    "angry": "angry / scam-refund talk",
    "repeat_callout": "called out the repeats",
    "loop_out": "we sent the same line 3+ times",
    "ghosted": "he talked, nobody answered",
    "dead_offer": "ends on a PPV he never opened",
}


def pick(stats: dict[str, dict], n_sell: int, n_long: int, n_break: int,
         min_msgs: int) -> list[dict]:
    """Rank into the three buckets and return the union, best-first."""
    alive = [s for s in stats.values() if s["total"] >= min_msgs]

    selling = sorted((s for s in alive if s["revenue_cents"] > 0),
                     key=lambda s: -s["revenue_cents"])[:n_sell]
    longest = sorted(alive, key=lambda s: -s["inbound"])[:n_long]
    breaks = sorted((s for s in alive if s["signals"]),
                    key=lambda s: -break_score(s))[:n_break]

    for s in selling:
        s.setdefault("buckets", []).append("selling")
    for s in longest:
        s.setdefault("buckets", []).append("long")
    for s in breaks:
        s.setdefault("buckets", []).append("breaks")

    chosen = [s for s in alive if s.get("buckets")]
    for s in chosen:
        why = []
        if "selling" in s["buckets"]:
            why.append(f"${s['revenue_cents']/100:,.0f} from "
                       f"{s['buys']} unlock{'s' if s['buys'] != 1 else ''}"
                       + (f" + {s['tips']} tip{'s' if s['tips'] != 1 else ''}"
                          if s["tips"] else ""))
        if "long" in s["buckets"]:
            why.append(f"{s['inbound']:,} messages from him")
        for name in sorted(s["signals"], key=lambda n: -BREAK_WEIGHTS.get(n, 0)):
            if "breaks" in s["buckets"] and name in REASON_TEXT:
                why.append(REASON_TEXT[name])
        s["why"] = why
        s["break_score"] = break_score(s)
    # Money first, then breakage, then sheer volume — the order the tabs inherit.
    chosen.sort(key=lambda s: (-s["revenue_cents"], -s["break_score"], -s["inbound"]))
    return chosen


# ── message loading ──────────────────────────────────────────────────────────

def fan_names(conn: sqlite3.Connection, threads: list[dict]) -> None:
    for t in threads:
        r = q(conn, """
            SELECT of_username, of_display_name, custom_nickname, generated_nickname,
                   real_name, lifetime_spend_cents
            FROM fans WHERE account_id = ? AND fan_id = ?
        """, (t["account_id"], t["fan_id"]))
        row = dict(r[0]) if r else {}
        t["fan_name"] = (row.get("custom_nickname") or row.get("real_name")
                         or row.get("of_display_name") or row.get("generated_nickname")
                         or row.get("of_username") or f"fan {t['fan_id']}")
        t["fan_username"] = row.get("of_username") or ""
        t["lifetime_cents"] = row.get("lifetime_spend_cents") or 0


def account_names(conn: sqlite3.Connection) -> dict[str, str]:
    return {r["id"]: (r["nickname"] or r["id"])
            for r in q(conn, "SELECT id, nickname FROM accounts")}


def load_messages(conn: sqlite3.Connection, t: dict, window: int,
                  context: int, hit_ids: set[int]) -> list[dict]:
    """The thread, trimmed to what is worth reading.

    Keeps the last `window` messages — plus a `context`-sized window around any
    money event or breakage hit that falls OUTSIDE it, so a sale from March
    doesn't get cropped out of a thread that ran until July. Skipped stretches
    come back as a `gap` marker rather than silently vanishing.
    """
    rows = q(conn, """
        SELECT message_id, direction, body, media_count, media_ids, price_cents,
               is_paid, is_tip, is_unsent, automation_kind, image_desc, created_at,
               sender_name
        FROM messages
        WHERE account_id = ? AND fan_id = ?
        ORDER BY created_at ASC, message_id ASC
    """, (t["account_id"], t["fan_id"]))
    n = len(rows)
    keep: set[int] = set(range(max(0, n - window), n))
    for i, r in enumerate(rows):
        # Money that LANDED, or a breakage hit. Deliberately not every PPV
        # *offer*: a ppv-heavy thread offers on almost every turn, so anchoring
        # on price_cents alone keeps the whole thread and the trim does nothing.
        anchored = (r["is_paid"] == 1 or r["is_tip"] or r["message_id"] in hit_ids)
        if anchored:
            keep.update(range(max(0, i - context), min(n, i + context + 1)))

    out: list[dict] = []
    prev = -1
    for i in sorted(keep):
        if prev >= 0 and i > prev + 1:
            out.append({"gap": i - prev - 1})
        r = rows[i]
        try:
            media_ids = json.loads(r["media_ids"] or "[]")
        except (ValueError, TypeError):
            media_ids = []
        out.append({
            "id": r["message_id"],
            "dir": r["direction"],
            "body": r["body"] or "",
            "ts": r["created_at"],
            "price": r["price_cents"] or 0,
            "paid": r["is_paid"],
            "tip": bool(r["is_tip"]),
            "unsent": bool(r["is_unsent"]),
            "kind": r["automation_kind"] or "",
            "desc": r["image_desc"] or "",
            "media_ids": [m for m in media_ids if isinstance(m, int)],
            "n_media": r["media_count"] or 0,
            "hit": r["message_id"] in hit_ids,
        })
        prev = i
    t["shown"] = sum(1 for m in out if "id" in m)
    t["truncated"] = t["shown"] < n
    return out


# ── media ────────────────────────────────────────────────────────────────────

def downscale(data: bytes, max_px: int, quality: int) -> bytes | None:
    """OF hands back a 960x1280 (or bigger) JPEG per still; a page of forty of
    those is 10 MB of base64. Downscale to something readable and cheap. Returns
    None when the bytes aren't a decodable image (a locked PPV's .mp4, an edge's
    HTML interstitial) — the caller then simply has no picture for that item."""
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(data))
        im.load()
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        im.thumbnail((max_px, max_px), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=quality, optimize=True, progressive=True)
        return buf.getvalue()
    except Exception as e:  # noqa: BLE001
        log(f"    downscale failed: {e}")
        return None


def fetch_thread_media(client, fan_id: int, want_ids: set[int], max_pages: int,
                       page_size: int, delay_s: float) -> dict[int, list[dict]]:
    """message_id → its media objects, freshly signed.

    Pages BACKWARD from the newest message (OF's only history direction) and
    stops the moment every message we care about has been seen — a thread whose
    media is all recent costs one page, not thirty.
    """
    found: dict[int, list[dict]] = {}
    remaining = set(want_ids)
    cursor = None
    for page_no in range(max_pages):
        if not remaining:
            break
        try:
            page = client.get_messages(fan_id, limit=page_size, order="desc",
                                       before_id=cursor)
        except Exception as e:  # noqa: BLE001
            log(f"    page {page_no} failed: {e}")
            break
        msgs = page.get("list") or []
        if not msgs:
            break
        for m in msgs:
            mid = m.get("id")
            if mid in remaining:
                remaining.discard(mid)
                media = m.get("media")
                if isinstance(media, list) and media:
                    found[mid] = media
                gid = _giphy(m)
                if gid:
                    found.setdefault(mid, []).append({"_giphy": gid})
        if not page.get("hasMore"):
            break
        cursor = msgs[-1].get("id")
        if delay_s:
            time.sleep(delay_s)
    return found


def giphy_ids(conn: sqlite3.Connection, account_id: str, fan_id: int,
              keep: set[int]) -> dict[int, str]:
    """message_id → giphy id, for the gifs in this thread.

    A Giphy DM is invisible to every other path here: OF sends it as a bare
    top-level `giphyId` with `media: []` and `mediaCount: 0`, so it has no
    `message_media` row and never enters the media hunt. The only trace is the
    stored frame — which is also the limit of this: `raw_json` is nulled at 60
    days (server._raw_json_evictor_loop), so gifs older than that are simply
    gone, with nothing left to say they were ever there.
    """
    rows = q(conn, """
        SELECT message_id, raw_json FROM messages
        WHERE account_id = ? AND fan_id = ? AND media_count = 0
          AND raw_json IS NOT NULL AND raw_json LIKE '%giphyId%'
    """, (account_id, fan_id))
    out: dict[int, str] = {}
    for r in rows:
        if r["message_id"] not in keep:
            continue
        try:
            gid = _giphy(json.loads(r["raw_json"]))
        except (ValueError, TypeError):
            continue
        if gid:
            out[r["message_id"]] = gid
    return out


def stored_media(conn: sqlite3.Connection, account_id: str, fan_id: int,
                 message_ids: Iterable[int]) -> dict[int, list[dict]]:
    """`message_media` rows reshaped into OF-media dicts, as the fallback for
    messages OF's history pages never reached.

    These urls are usually DEAD (signed for ~2 days), so most of these fetches
    fail — but the ones from the last couple of days succeed, and that is free
    coverage for the newest media in a long thread whose page budget ran out.
    """
    ids = [i for i in message_ids]
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    rows = q(conn, f"""
        SELECT message_id, media_id, type, thumb_url, source_url
        FROM message_media
        WHERE account_id = ? AND fan_id = ? AND message_id IN ({marks})
    """, [account_id, fan_id, *ids])
    out: dict[int, list[dict]] = {}
    for r in rows:
        files = {}
        if r["source_url"]:
            files["preview"] = {"url": r["source_url"]}
        if r["thumb_url"]:
            files["thumb"] = {"url": r["thumb_url"]}
        if files:
            out.setdefault(r["message_id"], []).append(
                {"id": r["media_id"], "type": r["type"], "files": files})
    return out


def _giphy(frame: dict) -> str | None:
    from of_shapes import giphy_dm_id
    return giphy_dm_id(frame)


def fetch_still(client, media: dict, max_px: int, quality: int) -> bytes | None:
    """One media object → downscaled JPEG bytes, or None.

    Goes through `still_url`, which is what keeps this honest about OF's shapes:
    a video resolves to its poster frame, a "gif" (stored as .mp4) resolves to
    its still, and a LOCKED ppv resolves to None instead of handing us an mp4 to
    mislabel as a picture."""
    from of_shapes import still_url
    url = still_url(media)
    if not url:
        return None
    try:
        r = client.http.get(url, timeout=client.timeout_s)
        if r.status_code != 200 or not r.content:
            return None
    except Exception:  # noqa: BLE001
        return None
    return downscale(r.content, max_px, quality)


def fetch_giphy(gid: str, max_bytes: int) -> bytes | None:
    """Giphy gifs stay ANIMATED — that is the whole point of them in a DM, and
    unlike OF's CDN, giphy's is public and unsigned. Fetched at the 200px variant
    and passed through untouched (re-encoding would flatten it to one frame)."""
    import requests
    for variant in ("200.gif", "200w.gif", "giphy.gif"):
        try:
            r = requests.get(f"https://i.giphy.com/media/{gid}/{variant}", timeout=20)
            if r.status_code == 200 and r.content and len(r.content) <= max_bytes:
                return r.content
        except Exception:  # noqa: BLE001
            continue
    return None


def media_priority(m: dict) -> tuple:
    """Which pictures survive the per-thread cap. Unlocked PPVs first (the proof
    a sale happened), then what HE sent, then everything else — newest first
    inside each tier."""
    tier = 2
    if m.get("paid") == 1:
        tier = 0
    elif m.get("dir") == "in":
        tier = 1
    return (tier, -(m.get("id") or 0))


# ── main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="-", help="JSON destination ('-' = stdout)")
    ap.add_argument("--selling", type=int, default=18)
    ap.add_argument("--long", type=int, default=18)
    ap.add_argument("--breaks", type=int, default=18)
    ap.add_argument("--min-msgs", type=int, default=8,
                    help="ignore threads shorter than this")
    ap.add_argument("--window", type=int, default=700,
                    help="messages kept from the end of each thread")
    ap.add_argument("--context", type=int, default=12,
                    help="messages kept either side of an older money/breakage event")
    ap.add_argument("--media-per-thread", type=int, default=36)
    ap.add_argument("--gifs-per-thread", type=int, default=12)
    ap.add_argument("--media-total", type=int, default=900)
    ap.add_argument("--max-px", type=int, default=460)
    ap.add_argument("--quality", type=int, default=72)
    ap.add_argument("--max-pages", type=int, default=14,
                    help="OF history pages per thread when hunting media")
    ap.add_argument("--page-size", type=int, default=50)
    ap.add_argument("--delay", type=float, default=0.35)
    ap.add_argument("--giphy-max-bytes", type=int, default=900_000)
    ap.add_argument("--no-media", action="store_true",
                    help="text only — skip every OF call")
    ap.add_argument("--have", default="",
                    help="path to a JSON list of media keys the caller already "
                         "holds on disk; those are not re-fetched")
    args = ap.parse_args()

    have: set[str] = set()
    if args.have:
        try:
            with open(args.have) as fh:
                have = set(json.load(fh))
            log(f"caller already holds {len(have)} media")
        except Exception as e:  # noqa: BLE001
            log(f"could not read --have ({e}); fetching everything")

    if not args.no_media:
        # Fail here, loudly, rather than per-account inside the loop — a missing
        # import there just logs "no client" 47 times and hands back a text-only
        # page that looks like a successful run.
        try:
            import automation_executor  # noqa: F401
            import of_shapes            # noqa: F401
        except ImportError as e:
            log(f"FATAL: the relay's modules are not importable from {APP_DIR} ({e}).\n"
                f"       Run this inside fastt-relay, or pass --no-media for text only.")
            return 2

    conn = connect(DB_PATH)
    log("ranking threads…")
    stats = thread_stats(conn)
    add_revenue(conn, stats)
    hits: dict[str, list[int]] = {}
    add_text_signals(conn, stats, hits)
    add_loop_signal(conn, stats, hits)
    add_tail_signals(conn, stats)
    threads = pick(stats, args.selling, args.long, args.breaks, args.min_msgs)
    log(f"{len(stats)} threads → {len(threads)} picked")

    fan_names(conn, threads)
    accounts = account_names(conn)
    for t in threads:
        t["account_name"] = accounts.get(t["account_id"], t["account_id"])

    media_blobs: dict[str, str] = {}   # "<acct>:<media_id>" → base64 jpeg/gif
    media_meta: dict[str, dict] = {}
    budget = args.media_total
    clients: dict[str, Any] = {}

    for idx, t in enumerate(threads, 1):
        hit_ids = set(hits.get(key(t["account_id"], t["fan_id"]), []))
        t["messages"] = load_messages(conn, t, args.window, args.context, hit_ids)
        log(f"[{idx}/{len(threads)}] {t['account_name']} ← {t['fan_name']} "
            f"({t['shown']}/{t['total']} msgs, {'/'.join(t['buckets'])})")
        if args.no_media or budget <= 0:
            continue
        acct = t["account_id"]

        # Gifs first, and separately: they need no OF call and no client at all
        # (giphy's CDN is public and unsigned), so they still land for an account
        # whose session is dead.
        by_id = {m["id"]: m for m in t["messages"] if "id" in m}
        gifs = 0
        for mid, gid in giphy_ids(conn, acct, t["fan_id"], set(by_id)).items():
            # A gif runs ~6x the bytes of a downscaled still, so it gets its own
            # ceiling rather than being allowed to eat the whole media budget.
            if gifs >= args.gifs_per_thread:
                break
            gifs += 1
            mk = f"{acct}:g{gid}"
            if mk not in have and mk not in media_blobs:
                if budget <= 0:
                    break
                data = fetch_giphy(gid, args.giphy_max_bytes)
                if not data:
                    continue          # no key on the message: nothing to render
                media_blobs[mk] = base64.b64encode(data).decode()
                media_meta[mk] = {"gif": True, "type": "gif"}
                budget -= 1
            by_id[mid].setdefault("media", []).append(mk)
            by_id[mid]["is_gif"] = True

        wanted = [m for m in t["messages"] if m.get("n_media") or m.get("media_ids")]
        wanted.sort(key=media_priority)
        wanted = wanted[:args.media_per_thread]
        if not wanted:
            continue

        if acct not in clients:
            try:
                import automation_executor as ax
                clients[acct] = ax._make_client(acct)
            except Exception as e:  # noqa: BLE001
                log(f"    no client for {acct}: {e}")
                clients[acct] = None
        client = clients[acct]
        if client is None:
            # Say so on the thread itself. Without a session for this account
            # nothing can re-sign its media, and a silently picture-less
            # conversation reads as "he never sent anything".
            t["media_note"] = "no captured session for this account — its "\
                              "pictures can't be re-signed"
            continue

        # A message whose every media id is already on disk needs no OF call at
        # all — `media_ids` gives us the keys straight from the DB. This is what
        # makes a re-run cheap: without it, a fully-cached thread still pays the
        # full history-paging bill just to re-derive urls it never fetches.
        todo = []
        for m in wanted:
            ks = [f"{acct}:{i}" for i in (m.get("media_ids") or [])]
            if ks and all(k in have or k in media_blobs for k in ks):
                m["media"] = m.get("media", []) + ks
            else:
                todo.append(m)
        if not todo:
            continue

        # Mass placeholder ids (the 5e15 band) are OURS, not OF's — hunting them
        # through history burns the whole page budget on rows OF will never return.
        want_ids = {m["id"] for m in todo if m["id"] < MASS_PLACEHOLDER_BASE}
        fresh = fetch_thread_media(client, t["fan_id"], want_ids,
                                   args.max_pages, args.page_size, args.delay)
        missed = [m["id"] for m in todo if m["id"] not in fresh]
        for mid, objs in stored_media(conn, acct, t["fan_id"], missed).items():
            fresh.setdefault(mid, objs)
        got = 0
        for msg in todo:
            if budget <= 0:
                break
            objs = fresh.get(msg["id"]) or []
            keys: list[str] = []
            for obj in objs:
                gid = obj.get("_giphy")
                mk = f"{acct}:g{gid}" if gid else f"{acct}:{obj.get('id')}"
                if mk in have or mk in media_blobs:
                    keys.append(mk)
                    continue
                if budget <= 0:
                    break
                data = (fetch_giphy(gid, args.giphy_max_bytes) if gid
                        else fetch_still(client, obj, args.max_px, args.quality))
                if not data:
                    continue
                media_blobs[mk] = base64.b64encode(data).decode()
                media_meta[mk] = {"gif": bool(gid),
                                  "type": "gif" if gid else (obj.get("type") or "photo")}
                keys.append(mk)
                budget -= 1
                got += 1
            if keys:
                msg["media"] = msg.get("media", []) + keys
        if got:
            log(f"    +{got} media (budget {budget} left)")

    payload = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
        "threads": threads,
        "media": media_blobs,
        "media_meta": media_meta,
        "counts": {"scanned": len(stats), "picked": len(threads),
                   "media_new": len(media_blobs)},
    }
    blob = json.dumps(payload, separators=(",", ":"))
    if args.out == "-":
        sys.stdout.write(blob)
    else:
        with open(args.out, "w") as fh:
            fh.write(blob)
        log(f"wrote {args.out} ({len(blob)/1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
