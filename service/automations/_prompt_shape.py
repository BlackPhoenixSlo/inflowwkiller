"""Shape of the assembled chat prompt — grouping, the facts ablation, the task line.

THE CANONICAL COPY. `replay_arms.py` imports this module for its arms, so what the
offline replay measured is the same code production runs; a second implementation
would mean shipping something that was never the thing under test.

It operates on the ASSEMBLED system string rather than on `_build_messages`'s
f-string, deliberately. The "blocks" only exist as `\\n\\n`-separated runs in the
output — there are no named variables to re-order — and rebuilding that f-string by
hand would produce a different artifact from the one that was measured.

Three independent transforms, each off by default:

  regroup            — same blocks, same words, grouped and ordered
  drop_facts         — remove THESE ARE THE FACTS ABOUT YOU where it is redundant
  task_line          — state which message this turn is answering

Every one is a no-op that returns its input unchanged when disabled, so an account
with the flag off builds a byte-identical prompt.
"""
from __future__ import annotations

import re
from typing import NamedTuple

# Block category, by opening line. Anything unrecognised falls to `identity` — which
# `drop_facts` never removes and `regroup` places first, so an unknown block degrades
# to "left alone near the top" rather than to "silently deleted".
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

# Every directive in this prompt SHOUTS its header and no persona line does — bio is
# "First Name: <name>" / "Born and raised in <city>" / free operator prose. A
# leading run of >=9 uppercase characters therefore means directive, whatever it says,
# which is what stops a newly-added block from being mistaken for persona. Found by
# audit 2026-08-12: DOG & WOLF STICKERS, THE THREAD IS HOT, SECOND OFFER and WHAT YOU
# CAN OFFER HIM were all being read as identity.
_SCREAMER = re.compile(r"^[A-Z][A-Z0-9 &'’\-]{8,}")
_SCREAMING_IDENTITY = ("THESE ARE THE FACTS ABOUT YOU",)

_ORDER = ("identity", "hard", "voice", "situational", "turn", "contract")

FACTS_HEAD = "THESE ARE THE FACTS ABOUT YOU"
TRANSCRIPT_HEAD = "Recent conversation (oldest→newest):\n"

# What a fan actually asks and what she must never invent. If ANY of these lives only
# inside the facts block, that block is load-bearing for this account and stays.
# Measured over 7 live accounts: six carry age/country/work in separate bio lines and
# lose only second-tier detail (birthday, exact city, height, pets). The seventh
# carries NOTHING outside the block and would be left with no age, no country and no
# job while `WHO YOU ARE (hard rule)` still says they are "in your profile above" —
# which is the whole reason this is a per-account check and not a global switch.
_CORE_FIELD = re.compile(
    r"^(age|first name|name|nationality|country|born country|work|occupation)$", re.I)
_FIELD_LINE = re.compile(r"^\s*[-*]?\s*([A-Za-z][A-Za-z /']{2,24}):\s*(.+)$", re.M)


def blocks(system: str) -> list[str]:
    return [p for p in system.split("\n\n") if p.strip()]


def classify(block: str) -> str:
    head = block.lstrip()
    for cat, heads in _CATS:
        if any(head.startswith(h) for h in heads):
            return cat
    if (_SCREAMER.match(head.splitlines()[0] if head else "")
            and not head.startswith(_SCREAMING_IDENTITY)):
        return "situational"
    return "identity"


def regroup(system: str) -> str:
    """Same blocks, same words — grouped and ordered, exact duplicates dropped.

    Today `STYLE FOR THIS MESSAGE`, the clock and `HIS NAME` — the only blocks that
    describe THIS turn — sit in the middle under ~6KB of standing rules, and `THE FEEL
    OF TEXTING` announces that it "governs everything below" while sitting third with
    the persona above it."""
    seen: set[str] = set()
    buckets: dict[str, list[str]] = {c: [] for c in _ORDER}
    for b in blocks(system):
        if b.strip() in seen:
            continue
        seen.add(b.strip())
        buckets[classify(b)].append(b)
    return "\n\n".join(b for cat in _ORDER for b in buckets[cat])


def facts_are_redundant(system: str, elsewhere: str = "") -> bool:
    """True when dropping the facts block costs no CORE identity.

    `elsewhere` defaults to "" ON PURPOSE: the decision must be a function of the
    account's own (constant) SYSTEM blocks only, never the per-fan user message. If it
    read the transcript, the same account would drop the block for one fan and keep it
    for the next — a system prefix that varies per fan, which defeats DeepSeek's
    prefix cache (the whole reason static text is kept static) and makes behaviour
    non-deterministic. Measured live 2026-08-13: account 2024813 flipped 1-of-2 that
    way. So callers pass nothing; the bio lines carry the duplication or they do not.

    Conservative by construction: a block whose fields cannot be parsed returns False
    (keep it), because "I could not read it" must never resolve to "delete it"."""
    facts = [b for b in blocks(system) if b.lstrip().startswith(FACTS_HEAD)]
    if not facts:
        return False
    rest = "\n".join(b for b in blocks(system)
                     if not b.lstrip().startswith(FACTS_HEAD)) + "\n" + elsewhere
    found_core = False
    for f in facts:
        for key, val in _FIELD_LINE.findall(f):
            if not _CORE_FIELD.match(key.strip()):
                continue
            found_core = True
            if val.strip().rstrip(".") not in rest:
                return False          # a core fact lives ONLY here
    return found_core


def split_facts(text: str) -> tuple[str, str]:
    """(everything else, the facts block) — THE one place that decides what "the
    facts block" is.

    Both halves matter now, and to different callers. `drop_facts` below throws the
    second one away; `ai_chatter._build_messages` keeps it and re-attaches it to the
    USER message on the turns a fan has actually asked who she is
    (`persona_facts_on_ask_only`). Those were briefly two implementations against the
    same header — a partition in `_persona` and this block filter — which is how the
    two silently disagree about a persona whose canon is not last.

    Both halves re-join on the canonical blank line, exactly as `drop_facts` has
    always done. Returns (text, "") when there is no canon, so an account that never
    filled one in builds a byte-identical prompt."""
    kept: list[str] = []
    facts: list[str] = []
    for b in blocks(text or ""):
        (facts if b.lstrip().startswith(FACTS_HEAD) else kept).append(b)
    return "\n\n".join(kept), "\n\n".join(facts)


def drop_facts(system: str) -> str:
    """The facts block removed.

    ⚠️ REACHES A FACTS BLOCK ONLY WITH `persona_facts_on_ask_only` OFF. `ai_chatter`
    is `apply`'s only caller, and with that gate ON it splits the canon off the
    persona BEFORE assembling the system string — so nothing is left here to drop and
    `facts_are_redundant` returns False at its first guard. The gate supersedes this
    transform where both apply (it is strictly stronger: the canon rides only when he
    asks, rather than only when some other block happens to repeat it), and this stays
    as the ablation for accounts on the legacy path. If the gate's default holds for a
    week, this and `facts_are_redundant` come out with `prompt_drop_facts_enabled`."""
    return split_facts(system)[0]


def task_line(user: str) -> str:
    """The CURRENT TASK footer — "" when there is nothing to name.

    NOT a task for the fan: it tells the MODEL which message it is answering. The
    transcript is a flat FAN/YOU list, so today the model infers the live line from
    position. Derived from the transcript, so it carries no judgement of its own."""
    if TRANSCRIPT_HEAD not in user:
        return ""
    block = user.split(TRANSCRIPT_HEAD, 1)[1].split("\n\n", 1)[0]
    run: list[str] = []
    quoted = ""
    has_quote = False
    for line in block.splitlines():
        if line.startswith("FAN: "):
            body = line[5:]
            if _MARK_A.search(body):
                quoted = _MARK_A.sub("", body).strip()
            if _MARK_REPLYING.search(body):
                has_quote = True
            run.append(_MARK_REPLYING.sub("", _MARK_A.sub("", body)).strip())
        elif line.startswith("YOU: "):
            if _MARK_A.search(line[5:]):
                quoted = _MARK_A.sub("", line[5:]).strip()
            run = []
    run = [x for x in run if x]
    if not run:
        return ""
    out = ["CURRENT TASK", f'The message you are replying to: "{" / ".join(run)}"']
    if has_quote and quoted:
        out.append(f'It quote-replies this earlier message: "{quoted}"')
        out.append("Answer the two of them together.")
    out.append("Everything above this block is context, not the thing to answer.")
    return "\n\n" + "\n".join(out)


_MARK_A = re.compile(r"\s*\[A(?:\s·[^\]]*)?\]$")
_MARK_REPLYING = re.compile(r"\s*\[replying to [^\]]*\]$")


class Shape(NamedTuple):
    """Which transforms this account has opted into. `from_cfg`/`OFF` mirror
    `pacing.PaceConfig` — the house shape for "a config slice a builder needs"."""
    regroup: bool = False
    drop_facts: bool = False
    task: bool = False

    @classmethod
    def from_cfg(cls, cfg: dict) -> "Shape":
        return cls(regroup=bool(cfg.get("prompt_regroup_enabled")),
                   drop_facts=bool(cfg.get("prompt_drop_facts_enabled")),
                   task=bool(cfg.get("prompt_task_line_enabled")))

    @property
    def any_on(self) -> bool:
        return self.regroup or self.drop_facts or self.task


OFF = Shape()


def reshape(system: str, user: str, shape: Shape = OFF) -> tuple[str, str]:
    """(system, user) after the enabled transforms. All off ⇒ the inputs are returned
    unchanged, so a prompt is byte-identical to today unless a flag says otherwise."""
    if not shape.any_on:
        return system, user
    if shape.regroup:
        system = regroup(system)
    # NB: the redundancy check reads the SYSTEM blocks only, never `user` — the drop
    # must be stable per account, not flip per fan (see facts_are_redundant).
    if shape.drop_facts and facts_are_redundant(system):
        system = drop_facts(system)
    if shape.task:
        user = user + task_line(user)
    return system, user
