"""Paired offline replay of stored ai_chatter prompts across prompt ARMS.

Answers "would a different prompt have answered this fan better?" using replies the
model already gave, days ago, to real threads. Nothing here can reach a fan: there is
no OF client, no message write, no delivery path — only stored prompts in, text and a
markdown report out.

TWO PRODUCTION HAZARDS THIS DELIBERATELY AVOIDS, and why it does not just call
`llm_client.chat()`:

  * `chat()` ATOMICALLY reserves the estimated cost against the account's DAILY SOFT
    CAP before firing (llm_client §2 step 2) and raises LLMCapExceeded when it does
    not fit. A nightly 1,200-call replay through that path would spend the budget the
    LIVE bot needs, and real fans would silently stop getting replies.
  * `chat()` writes a `grok_calls` row per call. That table is already ~52% of the
    prod DB; 1,200 rows a night is a storage regression dressed as a test.

So this posts directly, reads its key the same way `llm_client._api_key` does, and
keeps its own ledger in the report. Prod is only ever READ, `mode=ro`.

Usage:
    python replay_arms.py --arms A,B --n 150 --since 2026-08-05
    python replay_arms.py --arms A,B --n 300 --out reports/
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import subprocess
import sys
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

# The REAL validators. A grader that re-implements a rule tests the re-implementation;
# importing them means "compliant" means the same thing here as in production.
from automations.ai_chatter import _DELIVERY_TALK_RE, _unbacked_talk  # noqa: E402
from automations.of_ai_chat import _looks_like_echo  # noqa: E402

PROVIDER_URL = "https://api.deepseek.com/chat/completions"
JUDGE_MODEL = "deepseek-v4-pro"     # a stronger model grades; flash is the subject
_CONTAINER_DB = "/app/service/chatterly.db"

# Reply calls carry the whole persona+rules stack; the small ones are the fact-extract
# side calls (~4KB) and are not what we are testing.
_REPLY_PROMPT_MIN = 6000

_TRANSCRIPT_HEAD = "Recent conversation (oldest→newest):\n"


# ── prod reads (read-only, always) ──────────────────────────────────────────

def _sql(stmt: str) -> list[list[str]]:
    """Rows from prod. In-container when we are ON the VPS, else through
    scripts/prod-read.sh (which is itself in-container + mode=ro)."""
    if os.path.exists(_CONTAINER_DB):
        import sqlite3
        conn = sqlite3.connect(f"file:{_CONTAINER_DB}?mode=ro", uri=True)
        try:
            return [[("" if v is None else str(v)) for v in row]
                    for row in conn.execute(stmt).fetchall()]
        finally:
            conn.close()
    out = subprocess.run([os.path.join(_REPO, "scripts", "prod-read.sh"), stmt],
                         capture_output=True, text=True, check=True).stdout
    lines = [ln for ln in out.splitlines() if ln.strip()]
    return [ln.split(" | ") for ln in lines[1:]]        # drop the header row


@dataclass
class Case:
    """One stored production reply call — the unit every arm is measured on."""
    call_id: int
    account_id: str
    fan_id: str
    called_at: str
    body: dict                       # the exact request body prod sent
    shipped: str                     # what the fan actually got

    @property
    def user(self) -> str:
        return self.body["messages"][1]["content"]


# STRATA — a rule that fires on 1% of traffic is invisible in a random sample, and a
# null result there means "the sample missed it", not "the arm does not help". Each
# stratum is a SQL predicate over the stored prompt that selects turns where a given
# rule was actually in play. `all` is the ordinary-traffic control.
#
# Measured 2026-08-12: random traffic put A vs B at 10/8/2 over 20 cases — noise, and
# expected, because the targeting failure this exists to test lives in `quote` (~7% of
# inbound) and its pathological half in `self_quote` (5 in 7 days). Sample the failure,
# not the average.
STRATA: dict[str, str] = {
    "all": "1=1",
    "quote": "prompt_json like '%[replying to%'",
    "offer": "prompt_json like '%LOCKED%'",
    "sticker": "prompt_json like '%CAT STICKERS%'",
    "custom_owed": "prompt_json like '%A CUSTOM IS ALREADY PAID FOR%'",
    "spanish": "prompt_json like '%OUTPUT LANGUAGE%'",
}


def fetch_cases(n: int, since: str, stratum: str = "all") -> list[Case]:
    """A random sample of stored reply prompts. `hex()` because prompt_json is JSON
    with newlines and pipes in it, and the wrapper joins columns on ' | '."""
    where = STRATA[stratum]
    rows = _sql(
        "select id, account_id, fan_id, called_at, hex(prompt_json), "
        "hex(coalesce(response_text,'')) from grok_calls "
        f"where purpose='ai_chatter' and called_at >= '{since}' "
        f"and length(prompt_json) > {_REPLY_PROMPT_MIN} "
        f"and status='done' and ({where}) order by random() limit {int(n)}")
    cases = []
    for r in rows:
        if len(r) < 6:
            continue
        try:
            body = json.loads(bytes.fromhex(r[4]).decode())
            shipped = bytes.fromhex(r[5]).decode()
        except (ValueError, UnicodeDecodeError):
            continue                 # truncated row → skip the case, never the run
        if len(body.get("messages") or []) < 2:
            continue
        cases.append(Case(int(r[0]), r[1], r[2], r[3], body, shipped))
    return cases


# ── the transcript, parsed back out of the stored prompt ────────────────────

@dataclass
class Turn:
    """What the fan is actually waiting on, recovered from the rendered transcript."""
    trailing_run: list[str] = field(default_factory=list)   # his unanswered messages
    quoted: str = ""                                        # the `[A]` line, if any
    has_quote: bool = False


_MARK_A = re.compile(r"\s*\[A(?:\s·[^\]]*)?\]$")
_MARK_REPLYING = re.compile(r"\s*\[replying to [^\]]*\]$")


def parse_turn(user: str) -> Turn:
    """The trailing FAN run and the quoted bubble. Derived, never guessed — this is
    exactly the information arm B stops making the model infer."""
    if _TRANSCRIPT_HEAD not in user:
        return Turn()
    block = user.split(_TRANSCRIPT_HEAD, 1)[1].split("\n\n", 1)[0]
    t = Turn()
    run: list[str] = []
    for line in block.splitlines():
        if line.startswith("FAN: "):
            body = line[5:]
            if _MARK_A.search(body):
                t.quoted = _MARK_A.sub("", body).strip()
            if _MARK_REPLYING.search(body):
                t.has_quote = True
            run.append(_MARK_REPLYING.sub("", _MARK_A.sub("", body)).strip())
        elif line.startswith("YOU: "):
            if _MARK_A.search(line[5:]):
                t.quoted = _MARK_A.sub("", line[5:]).strip()
            run = []                 # her line ends his run
    t.trailing_run = [x for x in run if x]
    return t


# ── the arms ────────────────────────────────────────────────────────────────

def arm_a(case: Case) -> dict:
    """Exactly what production sent. The control."""
    return dict(case.body)


def arm_b(case: Case) -> dict:
    """A + a deterministic CURRENT TASK footer. Nothing is removed; the only change
    is that the target of the reply is STATED instead of inferred from position and
    bracket notation. Built from `parse_turn`, so it is reproducible and carries no
    judgement of its own."""
    turn = parse_turn(case.user)
    if not turn.trailing_run:
        return dict(case.body)       # nothing to name → arm B IS arm A for this case
    said = " / ".join(turn.trailing_run)
    lines = ["CURRENT TASK", f'The message you are replying to: "{said}"']
    if turn.has_quote and turn.quoted:
        lines.append(f'It quote-replies this earlier message: "{turn.quoted}"')
        lines.append("Answer the two of them together.")
    lines.append("Everything above this block is context, not the thing to answer.")
    body = dict(case.body)
    msgs = [dict(m) for m in body["messages"]]
    msgs[1]["content"] = case.user + "\n\n" + "\n".join(lines)
    body["messages"] = msgs
    return body


# ── C and D: transformations of the SYSTEM block ────────────────────────────
#
# Both must be deterministic functions of a stored prompt, not hand-written prompts:
# there are 20 live accounts with different personas, configs, day-logs and languages,
# and a single authored system block would only be testable on one of them.
#
# Classified by opening line. Anything unrecognised falls to IDENTITY, which is the
# safe default — it keeps the block, near the top, unchanged.
_CATS: list[tuple[str, tuple[str, ...]]] = [
    ("turn", ("RIGHT NOW for you it is", "HIS NAME:", "STYLE FOR THIS MESSAGE",
              "YOUR GOAL THIS MESSAGE", "THIS MESSAGE:",
              "You know enough about him now")),
    ("hard", ("STAY ON ONLYFANS", "WHO YOU ARE (hard rule)", "NEVER NARRATE",
              "DON'T NARRATE", "BIO CONSISTENCY")),
    ("voice", ("THE FEEL OF TEXTING", "HOW YOU TEXT", "TEXT LIKE A REAL PERSON",
               "your english is a little broken", "OUTPUT LANGUAGE")),
    ("situational", ("CAT STICKERS", "DOG & WOLF STICKERS",
                     "CONTENT YOU CAN ACTUALLY SEND", "SELLING RULES",
                     "YOU ALREADY OFFERED HIM", "SECOND OFFER",
                     "WHAT YOU CAN OFFER HIM", "THE THREAD IS HOT",
                     "A CUSTOM IS ALREADY PAID FOR", "YOUR DAY SO FAR",
                     "HE HAS ALREADY TOLD YOU")),
    ("contract", ("Your reply is ONLY the message text",)),
]

# An unrecognised block defaults to IDENTITY, which arm D KEEPS — so a directive that
# nobody added to the lists above would silently survive the strip and flatter D. The
# audit that found this (300 prompts, 2026-08-12) caught four: DOG & WOLF STICKERS (the
# male pack — the lists knew only CAT), THE THREAD IS HOT, SECOND OFFER and WHAT YOU
# CAN OFFER HIM, together ~20% of cases.
#
# Naming them is not enough; the next block someone adds would land the same way. Every
# directive in this prompt SHOUTS its header and every persona line does not — bio is
# "First Name: <name>" / "Born and raised in <city>" / free operator prose. So a
# leading run of ≥9 uppercase characters means directive, whatever it says. The one
# genuine screaming IDENTITY header is allowlisted.
_SCREAMER = re.compile(r"^[A-Z][A-Z0-9 &'’\-]{8,}")
_SCREAMING_IDENTITY = ("THESE ARE THE FACTS ABOUT YOU",)
# The clock and his name are FACTS about her and about him, not behavioural rules, so
# arm D keeps them — "scrape info of fan and model" means the info, and a model with no
# clock hallucinates the time, which would be a failure of the strip, not of the rules.
_D_KEEPS_TURN = ("RIGHT NOW for you it is", "HIS NAME:")


def _blocks(system: str) -> list[str]:
    return [p for p in system.split("\n\n") if p.strip()]


def _classify(block: str) -> str:
    head = block.lstrip()
    for cat, heads in _CATS:
        if any(head.startswith(h) for h in heads):
            return cat
    if (_SCREAMER.match(head.splitlines()[0] if head else "")
            and not head.startswith(_SCREAMING_IDENTITY)):
        return "situational"         # an unnamed directive — never survives arm D
    return "identity"


def arm_c(case: Case) -> dict:
    """CONSOLIDATED — same blocks, same words, zero policy removed. Exact-duplicate
    blocks dropped, and the ~23 blocks grouped and ordered:

        identity → hard rules → voice → situational → THIS TURN → output contract

    The point of the arm. Today `STYLE FOR THIS MESSAGE`, the clock and `HIS NAME` —
    the only blocks that describe THIS turn — sit in the MIDDLE, buried under ~6KB of
    standing rules, and `THE FEEL OF TEXTING` announces that it "governs everything
    below" while sitting third with the persona above it. C tests whether that
    arrangement costs anything, WITHOUT deleting a single rule."""
    system = case.body["messages"][0]["content"]
    seen: set[str] = set()
    buckets: dict[str, list[str]] = defaultdict(list)
    for b in _blocks(system):
        key = b.strip()
        if key in seen:
            continue                 # exact duplicate — the only thing C removes
        seen.add(key)
        buckets[_classify(b)].append(b)
    order = ["identity", "hard", "voice", "situational", "turn", "contract"]
    out = "\n\n".join(b for cat in order for b in buckets[cat])
    body = dict(case.body)
    msgs = [dict(m) for m in body["messages"]]
    msgs[0]["content"] = out
    body["messages"] = msgs
    return body


def arm_d(case: Case) -> dict:
    """STRIPPED — the operator's proposal: persona + facts about her and him, plus the
    output contract, and NOTHING else. Every behavioural rule block is dropped.

    The contract stays because "the model emitted JSON" is a failure of format, not of
    policy, and D exists to test whether the POLICY rules earn their keep. The user
    message (his facts, his pins, her day, the transcript) is untouched in every arm."""
    system = case.body["messages"][0]["content"]
    keep = []
    for b in _blocks(system):
        cat = _classify(b)
        if cat in ("identity", "contract"):
            keep.append(b)
        elif cat == "turn" and b.lstrip().startswith(_D_KEEPS_TURN):
            keep.append(b)
    body = dict(case.body)
    msgs = [dict(m) for m in body["messages"]]
    msgs[0]["content"] = "\n\n".join(keep)
    body["messages"] = msgs
    return body


ARMS = {"A": arm_a, "B": arm_b, "C": arm_c, "D": arm_d}


# ── the wire ────────────────────────────────────────────────────────────────

def _api_key() -> str:
    """Same precedence as llm_client._api_key: process env, then the COPYed .env."""
    key = (os.environ.get("DEEPSEEK_API_KEY") or "").strip()
    if key:
        return key
    path = os.path.join(_HERE, ".env")
    if os.path.exists(path):
        for line in open(path):
            if line.startswith("DEEPSEEK_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("no DEEPSEEK_API_KEY (env or service/.env)")


def call(body: dict, key: str, *, retries: int = 3) -> str | None:
    """One completion. None on a hard failure — a dead case is dropped from BOTH
    arms downstream, never scored as a loss for one of them."""
    req = urllib.request.Request(
        PROVIDER_URL, data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                return json.load(r)["choices"][0]["message"]["content"].strip()
        except (urllib.error.URLError, KeyError, ValueError, TimeoutError):
            if attempt == retries - 1:
                return None
    return None


# ── graders ─────────────────────────────────────────────────────────────────

_OFFPLATFORM = re.compile(
    r"\b(snap(chat)?|telegram|whats\s?app|kik|insta(gram)?|only\s?fans\.com|"
    r"cash\s?app|venmo|paypal|gmail|\+\d{7,})\b", re.I)


def grade_hard(text: str, case: Case) -> list[str]:
    """Deterministic violations, using production's OWN predicates where they exist.
    Each is a rule someone added after it went wrong on a real thread."""
    bad = []
    if not text or not text.strip():
        bad.append("empty")
        return bad
    if _OFFPLATFORM.search(text):
        bad.append("offplatform")
    if any(_unbacked_talk(p) for p in text.split("\n") if p.strip()):
        bad.append("unbacked_price")
    if _DELIVERY_TALK_RE.search(text):
        bad.append("delivery_talk")
    turn = parse_turn(case.user)
    last = turn.trailing_run[-1] if turn.trailing_run else ""
    if last and _looks_like_echo(text, last):
        bad.append("echo")
    if len(text) > 900:
        bad.append("overlong")
    return bad


_JUDGE = (
    "You are grading two candidate replies from an OnlyFans creator's chat assistant.\n"
    "Below is the end of the real conversation. The fan is waiting on a reply.\n\n"
    "{convo}\n\n"
    "Reply 1: {one}\n\nReply 2: {two}\n\n"
    "Which reply better answers what the fan ACTUALLY said and is waiting on? "
    "Ignore style, emoji and length; judge only whether it engages the right thing "
    "and does not ask for something he already told her.\n"
    'Answer with ONLY one JSON object: {{"winner": 1 | 2 | 0, "why": "<8 words>"}} '
    "where 0 means genuinely equal.")


def judge(case: Case, a_text: str, x_text: str, key: str,
          arm: str = "B") -> tuple[str, str]:
    """Blind paired comparison of arm `arm` against the control A. Order is shuffled
    per (case, arm) — seeded, so a re-run grades identically — and the judge never
    learns which arm is which. Returns "A", the arm's name, "tie", or "?"."""
    convo = case.user.split(_TRANSCRIPT_HEAD, 1)[-1].split("\n\n", 1)[0][-1200:]
    flip = random.Random(f"{case.call_id}:{arm}").random() < 0.5
    one, two = (x_text, a_text) if flip else (a_text, x_text)
    body = {"model": JUDGE_MODEL, "temperature": 0,
            "messages": [{"role": "user", "content": _JUDGE.format(
                convo=convo, one=one, two=two)}]}
    out = call(body, key) or ""
    m = re.search(r'\{.*\}', out, re.S)
    if not m:
        return "?", ""
    try:
        v = json.loads(m.group(0))
    except ValueError:
        return "?", ""
    w, why = v.get("winner"), str(v.get("why") or "")[:60]
    if w == 0:
        return "tie", why
    if w in (1, 2):
        # `flip` means position 1 held the CHALLENGER, so "1 wins" == the arm wins.
        return (arm if (w == 1) == flip else "A"), why
    return "?", why


# ── run + report ────────────────────────────────────────────────────────────

@dataclass
class Result:
    case: Case
    texts: dict[str, str]
    hard: dict[str, list[str]]
    winner: dict[str, str] = field(default_factory=dict)   # arm -> "A"|arm|"tie"|"?"
    why: dict[str, str] = field(default_factory=dict)


def run(cases: list[Case], arms: list[str], key: str, *,
        workers: int = 6, do_judge: bool = True) -> list[Result]:
    """Every arm answers every case, or the case is dropped from all of them. A
    half-answered case scored as a loss for whichever arm happened to fail is how a
    paired design quietly becomes an unpaired one."""
    def one(case: Case) -> Result | None:
        texts, hard = {}, {}
        for name in arms:
            t = call(ARMS[name](case), key)
            if t is None:
                return None
            texts[name] = t
            hard[name] = grade_hard(t, case)
        r = Result(case, texts, hard)
        if do_judge and "A" in arms:
            for name in arms:
                if name != "A":
                    r.winner[name], r.why[name] = judge(
                        case, texts["A"], texts[name], key, arm=name)
        return r

    with ThreadPoolExecutor(max_workers=workers) as ex:
        return [r for r in ex.map(one, cases) if r is not None]


def report(results: list[Result], arms: list[str], since: str,
           stratum: str = "all") -> str:
    n = len(results)
    if not n:
        return "# replay: no cases\n"
    out = [f"# Prompt-arm replay — {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC",
           "",
           f"`{n}` paired cases · stratum `{stratum}` · sampled from stored "
           f"production prompts since `{since}`. Every arm answered the SAME "
           "cases. No fan received anything.",
           "", "## Hard-rule violations (production's own predicates)", "",
           "| rule | " + " | ".join(arms) + " |",
           "|---|" + "---|" * len(arms)]
    rules = sorted({v for r in results for a in arms for v in r.hard[a]})
    for rule in rules:
        cells = [str(sum(1 for r in results if rule in r.hard[a])) for a in arms]
        out.append(f"| {rule} | " + " | ".join(cells) + " |")
    clean = [str(sum(1 for r in results if not r.hard[a])) for a in arms]
    out.append("| **clean** | " + " | ".join(f"**{c}/{n}**" for c in clean) + " |")

    sizes = {a: sum(len(json.dumps(ARMS[a](r.case)["messages"][0]["content"]))
                    for r in results) // n for a in arms}
    out += ["", "## System-block size (mean chars)", "",
            "| " + " | ".join(arms) + " |", "|" + "---|" * len(arms),
            "| " + " | ".join(f"{sizes[a]:,}" for a in arms) + " |"]

    challengers = [a for a in arms if a != "A"]
    if any(r.winner for r in results) and challengers:
        out += ["", "## Each arm vs the control A (blind paired judge)", "",
                "| arm | wins | A wins | tie | share of decisive | z | verdict |",
                "|---|---|---|---|---|---|---|"]
        for a in challengers:
            t = Counter(r.winner.get(a) for r in results if r.winner.get(a))
            w, lose, tie = t.get(a, 0), t.get("A", 0), t.get("tie", 0)
            dec = w + lose
            share = w / dec if dec else 0.0
            z = (w - dec / 2) / ((dec * 0.25) ** 0.5) if dec else 0.0
            verdict = ("**better**" if z > 1.96 else
                       "**WORSE**" if z < -1.96 else "no difference")
            out.append(f"| {a} | {w} | {lose} | {tie} | {share:.1%} | {z:+.2f} | "
                       f"{verdict} |")
        out += ["", "`z` is a two-sided binomial test against 50/50 on the decisive "
                "cases; |z| > 1.96 is p < 0.05. Anything else is noise, however "
                "suggestive the raw counts look.", ""]
        for a in challengers:
            beat = [r for r in results if r.winner.get(a) == a]
            lost = [r for r in results if r.winner.get(a) == "A"]
            out += [f"### {a} vs A — where {a} LOST (read these; {a} is the change)",
                    ""]
            for r in lost[:8]:
                turn = parse_turn(r.case.user)
                out += [f"- fan `{r.case.fan_id}` — he said: "
                        f"*{' / '.join(turn.trailing_run)[:90]}*",
                        f"  - A: `{r.texts['A'][:110]}`",
                        f"  - {a}: `{r.texts[a][:110]}`"]
            out += ["", f"### {a} vs A — where {a} won", ""]
            for r in beat[:6]:
                turn = parse_turn(r.case.user)
                out += [f"- fan `{r.case.fan_id}` — he said: "
                        f"*{' / '.join(turn.trailing_run)[:90]}*",
                        f"  - A: `{r.texts['A'][:110]}`",
                        f"  - {a}: `{r.texts[a][:110]}`"]
    return "\n".join(out) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--arms", default="A,B")
    ap.add_argument("--n", type=int, default=150)
    ap.add_argument("--since", default="")
    ap.add_argument("--out", default="")
    ap.add_argument("--stratum", default="all",
                    choices=sorted(STRATA), help="which turns to sample")
    ap.add_argument("--json-out", dest="json_out", default="")
    ap.add_argument("--no-judge", action="store_true")
    a = ap.parse_args()

    since = a.since or f"{datetime.now(timezone.utc) - timedelta(days=7):%Y-%m-%d}"
    arms = [x.strip().upper() for x in a.arms.split(",") if x.strip()]
    for name in arms:
        if name not in ARMS:
            raise SystemExit(f"unknown arm {name!r} (have {sorted(ARMS)})")

    key = _api_key()
    cases = fetch_cases(a.n, since, a.stratum)
    print(f"{len(cases)} cases · arms {arms} · stratum {a.stratum}",
          file=sys.stderr)
    results = run(cases, arms, key, do_judge=not a.no_judge)
    text = report(results, arms, since, a.stratum)
    if a.json_out:
        # Raw paired output, for the human-grading page. The arm labels live HERE and
        # never in the page — a grader who can see which one is production is not a
        # blind grader.
        with open(a.json_out, "w") as fh:
            json.dump([{"call_id": r.case.call_id, "fan_id": r.case.fan_id,
                        "account_id": r.case.account_id, "called_at": r.case.called_at,
                        "convo": r.case.user.split(_TRANSCRIPT_HEAD, 1)[-1]
                                             .split("\n\n", 1)[0],
                        "shipped": r.case.shipped,
                        "texts": r.texts, "hard": r.hard,
                        "winner": r.winner} for r in results], fh, indent=1)
        print(a.json_out, file=sys.stderr)
    if a.out:
        os.makedirs(a.out, exist_ok=True)
        path = os.path.join(
            a.out, f"replay-{datetime.now(timezone.utc):%Y-%m-%d}-{a.stratum}.md")
        with open(path, "w") as fh:
            fh.write(text)
        print(path, file=sys.stderr)
    print(text)


if __name__ == "__main__":
    main()
