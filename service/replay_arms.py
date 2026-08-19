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
from automations.welcome_chatter_for_info import _looks_like_echo  # noqa: E402
# The arms call the SHIPPED transform, never a copy of it — otherwise the
# replay measures something production does not run.
from automations import _prompt_shape as PS  # noqa: E402

PROVIDER_URL = "https://api.deepseek.com/chat/completions"
JUDGE_MODEL = "deepseek-v4-pro"     # ONLY used with --judge. The ARMS always run on
                                    # the model prod used (flash) — the judge is a
                                    # separate grader, and a human grader beats it.
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
    if not lines:
        raise RuntimeError(f"prod-read returned nothing for: {stmt[:80]}")
    # prod-read.sh has TWO output shapes: ' | '-joined from the in-container python,
    # and sqlite3's `-column` when `docker exec` fails and it falls back to the host
    # CLI. Splitting the second on ' | ' yields one garbage column per row and the
    # caller then sees ZERO usable cases — a silent empty result that reads as a
    # passing test. Seen live 2026-08-12 when one `docker exec` failed transiently
    # (prod itself was fine). Fail loudly instead; the retry is free.
    if " | " not in lines[0] and stmt.count(",") >= 1:
        raise RuntimeError(
            "prod-read fell back to the host sqlite3 CLI (docker exec failed) — its "
            "column output cannot be parsed safely. Re-run; prod is usually fine.")
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
    # For the arm-G ablation only: ~1 account in 4 has no FACTS-ABOUT-YOU block, and
    # on those G is byte-identical to F — a case that cannot answer the question it
    # was sampled for. Select the turns where the block is actually there.
    "has_facts": "prompt_json like '%THESE ARE THE FACTS ABOUT YOU%'",
    # Turns where he ASKS about her — the only turns that can catch an arm inventing a
    # birthday after the facts block was cut. `bio_invent` is dead weight without them.
    "personal": ("(prompt_json like '%how old%' or prompt_json like '%your age%' "
                 "or prompt_json like '%where are you from%' "
                 "or prompt_json like '%where do you live%' "
                 "or prompt_json like '%how tall%' "
                 "or prompt_json like '%real name%' "
                 "or prompt_json like '%your name%' "
                 "or prompt_json like '%boyfriend%' "
                 "or prompt_json like '%do you have a bf%')"),
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


def _task_footer(user: str) -> str:
    """The CURRENT TASK block — "" when there is nothing to name.

    NOT a task for the fan: it tells the MODEL which message it is answering. The
    transcript is a flat FAN/YOU list, so today the model infers the live line from
    position; this states it. Deterministic, from `parse_turn`, so it carries no
    judgement of its own. Shared by arms B and E."""
    turn = parse_turn(user)
    if not turn.trailing_run:
        return ""
    said = " / ".join(turn.trailing_run)
    lines = ["CURRENT TASK", f'The message you are replying to: "{said}"']
    if turn.has_quote and turn.quoted:
        lines.append(f'It quote-replies this earlier message: "{turn.quoted}"')
        lines.append("Answer the two of them together.")
    lines.append("Everything above this block is context, not the thing to answer.")
    return "\n\n" + "\n".join(lines)


def _rebuild(case: Case, system: str, user: str) -> dict:
    body = dict(case.body)
    body["messages"] = [{**case.body["messages"][0], "content": system},
                        {**case.body["messages"][1], "content": user}]
    return body


def arm_b(case: Case) -> dict:
    """A + the CURRENT TASK footer. Nothing removed; the only change is that the
    target of the reply is STATED instead of inferred from position."""
    return _rebuild(case, case.body["messages"][0]["content"],
                    case.user + _task_footer(case.user))


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


def _strip_who(case: Case) -> tuple[str, str]:
    """C's ordering with WHO-THEY-ARE removed from both halves — her persona and his
    facts. The mirror of the earlier strip: that one kept the people and dropped the
    rules and answered "do the rules earn their keep"; this one keeps every rule and
    drops the people, and answers "does the persona earn its keep".

    Her side is the `identity` blocks. His side is everything the USER message carries
    before the transcript — "What you know about him", his claims, his pinned message,
    and her day — so what is left is the rules, the turn, and the conversation."""
    system = "\n\n".join(b for b in _blocks(arm_c(case)["messages"][0]["content"])
                        if _classify(b) != "identity")
    user = case.user
    head = user.find(_TRANSCRIPT_HEAD)
    return system, (user[head:] if head != -1 else user)


def arm_d(case: Case) -> dict:
    """C, minus her persona and minus his facts."""
    return _rebuild(case, *_strip_who(case))


def arm_e(case: Case) -> dict:
    """D + the CURRENT TASK footer. Isolates whether naming the target message pays
    once the persona is gone — B tests the same footer against the FULL prompt, so
    E-vs-D and B-vs-A are the same question asked in two different contexts."""
    system, user = _strip_who(case)
    return _rebuild(case, system, user + _task_footer(user))


def arm_f(case: Case) -> dict:
    """C + the CURRENT TASK footer. C's regrouping is the change the operator liked;
    this asks whether naming the target message pays ON TOP of it. B asked the same
    question against the UNGROUPED prompt and came back null over 350 cases, so a
    difference here is about the pairing, not the footer alone."""
    return _rebuild(case, arm_c(case)["messages"][0]["content"],
                    case.user + _task_footer(case.user))


# 1.8KB and the single biggest identity block: her age, birthday, height, where she
# grew up. Note what depends on it — the `WHO YOU ARE (hard rule)` block says those
# facts "are in the facts above; never invent them". Drop this and that rule points at
# nothing, so the failure to watch for is not a worse tone, it is her INVENTING a
# birthday. `bio_invent` in the graders is there for exactly this arm.
_FACTS_HEAD = "THESE ARE THE FACTS ABOUT YOU"


def arm_g(case: Case) -> dict:
    """F minus the FACTS-ABOUT-YOU block — does that 1.8KB earn its place?"""
    system = "\n\n".join(
        b for b in _blocks(arm_c(case)["messages"][0]["content"])
        if not b.lstrip().startswith(_FACTS_HEAD))
    return _rebuild(case, system, case.user + _task_footer(case.user))


# ── H: F with the VOICE prose compressed ────────────────────────────────────
#
# Every rule below survives; only the words explaining it are cut. The line this
# deliberately does NOT cross: a HARD rule is never touched. `STAY ON ONLYFANS`,
# `WHO YOU ARE`, `SELLING RULES`, `CONTENT YOU CAN ACTUALLY SEND` and the facts block
# stay byte-identical, because a rule about off-platform contact or money is not
# "useless crap" however wordy it reads, and a compression that quietly loosens one is
# the worst possible outcome of a test that looks like a win.

_FEEL = """THE FEEL OF TEXTING (read first — this governs everything below):
texting is a chore, like a real girl half-glued to her phone. write the FEWEST words \
that land the feeling AND actually address him — almost always ONE line, occasionally \
two, basically never more. no extra bubbles, no padding, no explaining yourself. dont \
labor over the perfect tiny line; if a slightly longer one comes out easier than \
agonizing, thats fine.
spend more words ONLY when it amuses you, when a longer line lands the emotion harder, \
or when it makes HIM feel something. otherwise skip them. a short line that PUNCHES \
beats a long one that explains, every time.
short NEVER means dead or dodgy: even a tiny reply carries heat, a tease or warmth AND \
engages what mattered in his message. never a flat ok/lol/nice/haha, never his own \
words parroted back, never a cute one-liner that sidesteps his real point.
if he ASKED you something, answer it in as few words as it takes, then STOP.
the get-to-know backend-info question is a JUDGEMENT CALL, not a habit — sometimes \
asking is exactly what the moment wants, sometimes dropping it and just reacting hits \
harder. read the moment; dont do either on autopilot."""


_REAL = """TEXT LIKE A REAL PERSON, NOT AN AI:
- lowercase always, including 'i'. NEVER an em-dash or semicolon.
- NEVER echo or quote his words back, and never restate them with an adjective \
('sounds gorgeous', 'thats a whole mood', 'dangerous in the best way') — biggest bot \
tell. react in your OWN words.
- vary length wildly: sometimes one word, sometimes a short line. DONT open every text \
with a reaction sound — most replies should just start with the actual thing you're \
saying — and NEVER reuse the same opener two replies running.
- texting sounds (lol, lmao, omg, ugh, hmm, wait, stop, oof) in MODERATION: pick a \
different one each time, dont lean on any single one.
- a tiny typo or missing apostrophe is fine (dont, im, ur, gonna).
- dont be relentlessly upbeat or agreeable. tease, be a lil bratty, push back sometimes.
- AT MOST ONE question, ever. 0-1 emoji, never the same emoji twice. never explain \
yourself or over-clarify."""


_STICKER_RULES = """- MOST replies need NO sticker — use one only when the emotion is strong, never force it.
- To attach one, end your reply with a line that is exactly: STICKER: <tag>
- A sticker can BE the whole reply — when a reaction says it all, output ONLY the \
STICKER line, no text.
- Max ONE per reply. The line is protocol, stripped before sending; he only sees the gif.
- NEVER mention or describe the sticker in your text."""


_TAG_LINE = re.compile(r"^- ([a-z_]+): *(.+)$", re.M)


def _squeeze(block: str) -> str:
    """One voice block, compressed. Unrecognised blocks pass through untouched."""
    head = block.lstrip()
    if head.startswith("THE FEEL OF TEXTING"):
        return _FEEL
    if head.startswith("TEXT LIKE A REAL PERSON"):
        return _REAL
    if head.startswith(("CAT STICKERS", "DOG & WOLF STICKERS")):
        # The glosses STAY. Cutting them to bare tag names was the plan and it was
        # wrong: they are not decoration, they are the routing table. Codex named the
        # pairs that collapse without them — love/kiss, dance/celebrate, miss_you/
        # waiting, sad/pout/grumpy, eyeroll/grumpy, shocked/confused, beg/pout, and
        # `money`, which without "after he spoils you" fires on a man describing money
        # TROUBLE. So this block only loses its list scaffolding (22 × "- " and a
        # newline), which is the honest answer to "shorten the stickers": most of its
        # length IS content. Tags are read from the block, so the male DOG & WOLF pack
        # compresses through the same path.
        kind = head.split(" —")[0]
        tags = _TAG_LINE.findall(block)
        if not tags:
            return block                       # unfamiliar shape → leave it alone
        roster = " · ".join(f"{t} ({g.strip().rstrip('.')})" for t, g in tags)
        return (f"{kind} — a pack of reaction gifs, the kind real girls spam in "
                f"texts. Tags: {roster}.\n{_STICKER_RULES}")
    if head.startswith("HOW YOU TEXT"):
        # First line carries her AGE and the trailing GOOD: line is per-account — both
        # are content, not prose, so both survive verbatim.
        lines = block.splitlines()
        good = [ln for ln in lines if ln.lstrip().startswith("- GOOD:")]
        return "\n".join([lines[0],
                          "- short, casual, lowercase, contractions, u/ur/ya. react to "
                          "what he said in a few words first.",
                          "- VARY it every time — never open the same way twice, never "
                          "reuse a phrase or emoji from this chat.",
                          "- ONE question at most, never one he already answered (vague "
                          "answer → quick follow-up, not a re-ask). no paragraphs.",
                          "- NEVER narrate: no *asterisk actions*, no describing your "
                          "face, body or what youre doing, no writing about yourself "
                          "from the outside. real people type words, not stage "
                          "directions. if the only thing left is a reaction, send nothing.",
                          "- he gets explicit early → dont go along with it, PLAYFULLY "
                          "tease and slow it down, THEN steer back to getting to know "
                          "him. warm and flirty, never cold or preachy."] + good)
    return block


def arm_h(case: Case) -> dict:
    """F with the voice prose compressed — same rules, fewer words."""
    system = "\n\n".join(_squeeze(b) for b in
                         _blocks(arm_c(case)["messages"][0]["content"]))
    return _rebuild(case, system, case.user + _task_footer(case.user))


ARMS = {"A": arm_a, "B": arm_b, "C": arm_c, "D": arm_d, "E": arm_e,
        "F": arm_f, "G": arm_g, "H": arm_h}


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


# SPEND. This module bypasses `llm_client`, which means it also bypasses the daily cap
# and the `grok_calls` ledger — so its spend appears on the provider bill and NOWHERE in
# the operator's dashboards. That is a footgun: a 350-case × 4-arm sweep is ~2,400 calls
# and ~5x this account's ENTIRE normal daily LLM spend, and the first the operator would
# hear of it is the invoice. So the module counts its own money and REFUSES to exceed
# the budget it was given.
_PRICE_PER_1K_CENTS = {                      # (input, output), from llm_client.MODELS
    "deepseek-v4-flash": (0.014, 0.028),
    "deepseek-v4-pro": (0.0435, 0.087),
}
_SPENT_CENTS = 0.0
_BUDGET_CENTS = 0.0                          # 0 = unset; main() always sets it


class BudgetExceeded(RuntimeError):
    """Raised instead of quietly spending more than the run was authorised for."""


def _charge(model: str, chars_in: int, chars_out: int) -> None:
    global _SPENT_CENTS
    cin, cout = _PRICE_PER_1K_CENTS.get(model, (0.014, 0.028))
    _SPENT_CENTS += (chars_in / 4 / 1000) * cin + (chars_out / 4 / 1000) * cout
    if _BUDGET_CENTS and _SPENT_CENTS > _BUDGET_CENTS:
        raise BudgetExceeded(f"spent ${_SPENT_CENTS / 100:.2f} of "
                             f"${_BUDGET_CENTS / 100:.2f} budget")


def spent_usd() -> float:
    return _SPENT_CENTS / 100


def call(body: dict, key: str, *, retries: int = 3) -> str | None:
    """One completion. None on a hard failure — a dead case is dropped from BOTH
    arms downstream, never scored as a loss for one of them."""
    req = urllib.request.Request(
        PROVIDER_URL, data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                txt = json.load(r)["choices"][0]["message"]["content"].strip()
            _charge(body.get("model", ""),
                    sum(len(m["content"]) for m in body["messages"]), len(txt))
            return txt
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
    if _invents_bio(text, case):
        bad.append("bio_invent")
    return bad


_AGE_CLAIM = re.compile(r"\b(?:i'?m|im|i am)\s+(\d{2})\b", re.I)
_HEIGHT_CLAIM = re.compile(r"\b(\d)\s*'\s*(\d{1,2})\b")


def _invents_bio(text: str, case: Case) -> bool:
    """She stated an age or height that is NOWHERE in the prompt production sent.

    Only checks values that are checkable, and always against arm A's system block —
    the full one — so an arm that DROPPED the facts is measured against the truth it
    was supposed to know, which is the whole point of the check. `WHO YOU ARE (hard
    rule)` tells her those facts "are in the facts above"; when an arm removes them,
    this is what catches her filling the hole herself."""
    truth = case.body["messages"][0]["content"]
    for m in _AGE_CLAIM.finditer(text):
        if m.group(1) not in truth:
            return True
    for m in _HEIGHT_CLAIM.finditer(text):
        if f"{m.group(1)}'{m.group(2)}" not in truth.replace(" ", ""):
            return True
    return False


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
    ap.add_argument("--judge", action="store_true",
                    help="add an LLM judge pass (extra calls, pro-priced). Off by "
                         "default: you are the judge.")
    ap.add_argument("--max-usd", type=float, default=1.0,
                    help="hard spend ceiling; the run aborts rather than exceed it")
    a = ap.parse_args()

    since = a.since or f"{datetime.now(timezone.utc) - timedelta(days=7):%Y-%m-%d}"
    arms = [x.strip().upper() for x in a.arms.split(",") if x.strip()]
    for name in arms:
        if name not in ARMS:
            raise SystemExit(f"unknown arm {name!r} (have {sorted(ARMS)})")

    key = _api_key()
    cases = fetch_cases(a.n, since, a.stratum)
    if not cases:
        raise SystemExit("no cases matched — refusing to report on an empty run")
    print(f"{len(cases)} cases · arms {arms} · stratum {a.stratum}",
          file=sys.stderr)
    global _BUDGET_CENTS
    _BUDGET_CENTS = a.max_usd * 100
    try:
        results = run(cases, arms, key, do_judge=a.judge)
    except BudgetExceeded as e:
        raise SystemExit(f'ABORTED: {e}')
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
                        "winner": r.winner,
                        # The PROMPTS, per arm — so the page can show what each arm
                        # actually sent, not just what came back.
                        "prompts": {a: {
                            "sys_blocks": _blocks(ARMS[a](r.case)["messages"][0]["content"]),
                            "user": ARMS[a](r.case)["messages"][1]["content"],
                        } for a in r.texts},
                        } for r in results], fh, indent=1)
        print(a.json_out, file=sys.stderr)
    if a.out:
        os.makedirs(a.out, exist_ok=True)
        path = os.path.join(
            a.out, f"replay-{datetime.now(timezone.utc):%Y-%m-%d}-{a.stratum}.md")
        with open(path, "w") as fh:
            fh.write(text)
        print(path, file=sys.stderr)
    print(text)
    print(f"\nspent ${spent_usd():.3f} on {len(cases)} cases x {len(arms)} arms"
          f"{' + judge' if a.judge else ' (no judge)'}", file=sys.stderr)


if __name__ == "__main__":
    main()
