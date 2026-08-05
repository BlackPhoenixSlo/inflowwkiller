"""service/automations/_persona.py — who the CREATOR is, and what she has said.

The creator-side twin of the fan profile. Everything in this codebase used to
point at the FAN: `fans` carries 69 columns about him, `fan_profiles` holds a
generated profile of him, `gen_info` refreshes it, and every chat prompt renders
it under "What you know about him". Nothing recorded what SHE said about HERSELF
— so in a long thread, where the reply prompt only carries the last
`_HISTORY_TAIL` bubbles, she re-answered every biographical question from scratch
and contradicted herself. Measured on prod: 56% of repeat biographical questions
arrived with the earlier answer already outside the window (mean gap 127
messages), and one 966-turn thread walked "no soy chilena, soy argentina de
verdad" → "y sí, estoy acá en Chile" → "en córdoba" before the fan wrote "ya no
creo nada".

Two layers, and the split is what keeps the whole thing cheap:

  CANON  (`account_ai_config.persona_facts_json`) — one answer for all 8,050
         fans. Account-constant, so the loaders compose it onto the persona
         string once per run rather than threading a param through five builders.

  TIER B (`fans.persona_claims_json`) — what she told ONE fan, recorded ONLY
         where it deviates from canon or covers something canon didn't. Most fans
         stay empty. It exists because a claim already made cannot be retracted:
         once she has told him "estoy acá en Chile", correcting him to Argentina
         mid-thread lands as the lie, not the fix. Canon keeps NEW threads right;
         this keeps EXISTING threads coherent.

Every renderer here returns "" when its input is unset, so an un-enriched account
and a fan with no deviations both produce a byte-identical prompt.

Extracted from `_common.py` (which it had grown by ~500 lines) following the
`_language.py` precedent in this package: one cohesive domain, one file.
"""
from __future__ import annotations

import logging
import re

import llm_client                   # call .chat at runtime so tests can patch it
from db.models import Fan
from jsonsafe import load_dict, load_json
from llm_client import LLMCapExceeded

from . import _voice
from ._common import nonempty

log = logging.getLogger("of-relay.persona")


# ── Persona register age (the "HOW YOU TEXT" voice, NOT a claimed fact) ──
# The chat builders open their style block with "HOW YOU TEXT (a real 22yo girl,
# not an assistant)". That 22 is a REGISTER instruction — text young and casual —
# not an assertion about her age, and nothing downstream reads it as one. It was
# hardcoded from Ava's default persona, so an account whose persona says 49 was
# still being told to text like a 22-year-old. Deriving it from the persona keeps
# voice and profile in the same key.
#
# Accepts both shapes actually in use: a "Age: 27 5/24/99" line and prose like
# "a flirty and fabulous 22-year-old OnlyFans creator". Anything under 18 is
# REJECTED (falls back to the default) — an under-18 age must never reach a
# prompt, whatever an operator typed into the persona box.
DEFAULT_REGISTER_AGE = 22
_PERSONA_AGE_LINE_RE = re.compile(r"^[ \t]*age[ \t]*:[ \t]*(\d{1,3})\b", re.I | re.M)
_PERSONA_AGE_PROSE_RE = re.compile(r"\b(\d{1,3})[\s-]*(?:year[\s-]*old|yo)\b", re.I)


def persona_register_age(persona: str | None) -> int:
    """Age for the HOW-YOU-TEXT register line. Never a claim about the creator —
    see the note above. Falls back to DEFAULT_REGISTER_AGE when the persona has
    no usable age, so a persona with no age yields a byte-identical prompt."""
    for rx in (_PERSONA_AGE_LINE_RE, _PERSONA_AGE_PROSE_RE):
        m = rx.search(persona or "")
        if not m:
            continue
        try:
            age = int(m.group(1))
        except ValueError:
            continue
        if 18 <= age <= 99:
            return age
    return DEFAULT_REGISTER_AGE


def _persona_location_line(location: str | None) -> str:
    """Internal to compose_persona — "You are based in X." for the chat prompts. `account_ai_config.location`
    has always been set (10 of 12 live accounts) and rendered by send_welcome and
    send_followup — but NO chat engine read it, so the welcome message named a
    country the chat engine then had no idea about. Phrasing matches
    send_followup._compose_system verbatim. "" when unset → byte-identical
    prompt for accounts with no location."""
    loc = (location or "").strip()
    return f"You are based in {loc}.\n\n" if loc else ""


# ── Structured creator canon (account_ai_config.persona_facts_json) ───
# The facts she must never contradict, pinned ABOVE the free-text persona. Order
# is deliberate: identity first, then origin, then the softer colour — a bullet
# buried mid-prompt loses to the goal/style lines (same lesson the sticker marker
# and the clock block both learned the hard way).
# The slate is driven by what fans MEASURABLY ask (inbound sweep, 2026-07-25,
# all accounts, test account excluded — hits in brackets). Two counts were thrown
# out after reading the messages rather than trusting the keyword: "favorites"
# (532) is mostly fans naming THEIR favourites or calling her "my favorite girl",
# and "gym" (323) is almost entirely peer-creator promo captions landing in the
# inbox, not questions.
PERSONA_FACT_FIELDS: tuple[tuple[str, str], ...] = (
    ("age", "Age"),                                  # [173]
    ("birthday", "Birthday"),                        # [12] low volume, huge blast radius
    ("height", "Height"),                            # [22]
    ("job", "Work"),                                 # [81]
    ("why_of", "Why she started OF"),                # [2] rare but THE authenticity question
    ("school", "School / studies"),                  # [44]
    ("home_city", "Lives in"),                       # [92]
    ("home_country", "Country"),
    ("born_city", "Born in"),
    ("born_country", "Born country"),
    ("upbringing", "Growing up"),                    # [31]
    ("living_situation", "Living situation"),        # [9]
    ("relationship", "Relationship"),                # [325] the most-asked of all
    ("ex", "Her ex"),                                # [44]
    ("kids", "Kids"),                                # [163] binary — catastrophic to flip
    ("family", "Family"),                            # [82]
    ("pets", "Pets"),                                # [23]
    # Fans do not ask WHETHER she has tattoos — they read them off her photos
    # ("El tatuaje entre tus pechos que dice?", "Motionless in white tattoo
    # spotted?"). Body art is VISIBLE EVIDENCE, so unlike a birthplace there is a
    # picture to check her answer against. [179]
    ("tattoos", "Tattoos / marks"),
    ("languages", "Languages"),                      # [24]
    ("music", "Music taste"),                        # [28]
    ("travel", "Been to / travel"),                  # [180 raw, heavily diluted]
    ("dreams", "Dreams / goals"),                    # [79]
    ("routine", "Daily routine / work hours"),       # "Are you working tonight?"
    ("her_type", "Her type in men"),                 # [10] asked implicitly far more
    # ── Operator-only (see PERSONA_FACTS_OPERATOR_ONLY) ──
    ("kinks", "What she's into"),                    # [65] and it saturates the sample
    ("limits", "What she won't do"),                 # fans don't ask, they PUSH
)

# Slots the 🪄 enrich pass must NEVER propose. Two different reasons, both learned
# the hard way:
#
#   kinks / limits — a BUSINESS decision. A model guessing either invents a limit
#       that costs a sale or agrees to something the creator never offers.
#
#   tattoos — PHOTOGRAPHIC EVIDENCE. Caught live on 2026-07-25: asked to enrich
#       the graded vault, the model answered `tattoos: "None"` while 77 of her 138
#       described vault items are tagged `tattoo` and 13 name a position outright
#       ("revealing a tattooed forearm"). Every other slot here is unfalsifiable
#       persona fiction — a fan cannot check where she grew up. Body art he can
#       see, which makes a confident wrong answer worse than a blank one. This
#       slot belongs to the vault-sourced pass; until that exists, the operator
#       fills it.
PERSONA_FACTS_OPERATOR_ONLY: frozenset[str] = frozenset({"kinks", "limits", "tattoos"})

# The editor hints for these slots — the example text under each box — live in
# `_voice._FACT_PLACEHOLDERS`, one lane each, read through `fact_placeholders()`.
# They were here, in the female lane only, until the male lane needed them too:
# every one of them is a worked example of the creator's own life, so unlike the
# LABELS (21 of which just name the field) there is no lane-neutral version to
# keep as a default. `_voice` owns text that reads differently per lane.
#
# This is the slate's home, so this is where both tables get bound to it: a canon
# field with no male placeholder, or a gendered label with no male form, fails
# HERE at import rather than serving her copy on his account.
_voice.assert_canon_parity(PERSONA_FACT_FIELDS)

_FACT_VALUE_CLIP = 240      # one fact should be a line, never a paragraph


def _pinned_block(header: str, lines: list[str]) -> str:
    """`header` + bullets, or "" when there are no bullets.

    Both pinned blocks in this module render the same shape, and the "" case is
    load-bearing in both: an un-enriched account and a fan with no deviations must
    each produce a byte-identical prompt to what shipped before them."""
    return f"{header}\n" + "\n".join(lines) + "\n\n" if lines else ""


def _parse_persona_facts(raw) -> dict:
    """`persona_facts_json` → dict, keys narrowed to the known field contract.

    `load_dict` owns the defensive half (NULL / already-parsed / corrupt / a
    stored list all collapse to {}), so this only has to say what is SPECIFIC to
    persona facts: unknown keys are dropped, because the block that renders them
    reads labels from PERSONA_FACT_FIELDS and a stray key would render unlabelled."""
    return {k: v for k, v in load_dict(raw).items() if k in dict(PERSONA_FACT_FIELDS)}


def _persona_facts_block(raw, voice=None) -> str:
    """Internal to compose_persona — the pinned-canon block for the chat prompts. "" when nothing is set, so an
    un-enriched account sends a byte-identical prompt — same discipline as the
    clock and location blocks.

    Rendered as an imperative, not an inventory. The passive "What you know about
    him" list is the one the model demonstrably ignores (it asked a $691 fan his
    job with `occupation` populated and injected); the imperative clock line is
    the one that held. This block copies the clock's voice.

    `voice` re-labels the three third-person-female slots for the male lane. It
    matters here more than anywhere else those labels appear: this block asserts
    its contents as facts the model may never contradict, so on a male account
    the un-lane'd labels do not merely read oddly — they pin the creator's gender
    as canon. Default None → `VOICE_HER` → `overrides` is empty → every label
    unchanged."""
    facts = _parse_persona_facts(raw)
    overrides = _voice.fact_labels(voice)
    lines = []
    for key, declared in PERSONA_FACT_FIELDS:
        label = overrides.get(key, declared)
        val = facts.get(key)
        if isinstance(val, (list, tuple)):
            val = ", ".join(str(v).strip() for v in val if str(v).strip())
        val = str(val).strip() if val is not None else ""
        if val:
            lines.append(f"- {label}: {val[:_FACT_VALUE_CLIP]}")
    return _pinned_block(
        "THESE ARE THE FACTS ABOUT YOU. They are true, they never change, and "
        "you must never say anything that contradicts them:", lines)


# ── TIER B: the per-fan claims ledger ────────────────────────────────
# Canon (persona_facts_json) answers "who is she" once, for all 8,050 fans. This
# is the other half: what she actually SAID to ONE fan, stored ONLY where it
# deviates from canon or covers something canon didn't. Most fans stay empty —
# that is the whole point, and what makes it cheap.
#
# Why per-fan at all, when canon exists: a claim already made cannot be retracted.
# Once she has told JH "estoy acá en Chile", correcting her to Argentina mid-
# thread is WORSE than staying wrong — the fan experiences the correction as the
# lie. Canon keeps new threads right; the ledger keeps existing threads coherent.
#
# LATEST-WINS, not fill-empty-only. _extract_and_fill's first-wins is correct for
# FAN facts (he is not going to change where he was born) but wrong here: a bad
# first extraction would be permanent and a canon edit would never propagate.
# Follows _mark_question_asked's escalating precedent instead.
_CLAIMS_MAX = 20            # cap, cloning gen_info's recent_events discipline
_CLAIM_TEXT_CLIP = 180


# The cost gate. The extract call is skipped once the FAN's profile saturates
# (~43% of the ai_chatter lane was that waste), which is exactly the deep-thread
# moment claims matter. Rather than re-enable it wholesale, fire it only on turns
# where she DEMONSTRABLY said something about herself. Bio-interrogation is 10% of
# threads and only some turns inside those make a claim, so this stays a few
# percent of replies instead of doubling every one.
_SELF_CLAIM_RE = re.compile(
    r"\b("
    # EN — first person + a biographical predicate
    r"i'?m\s+\d{2}\b|i\s+am\s+\d{2}\b|\d{2}\s*years?\s*old|"
    r"i\s+live\s|i\s+grew\s+up|i\s+was\s+born|born\s+in\s|i'?m\s+from|i\s+am\s+from|"
    r"my\s+(job|work|boyfriend|ex|husband|mom|mum|dad|brother|sister|family|"
    r"dog|cat|tattoo|birthday|name)\b|"
    r"i\s+(work|studied|study|speak)\s|i\s+don'?t\s+have\s+(kids|children)|"
    r"i\s+have\s+(a\s+)?(kid|kids|children|tattoo)|"
    # ES. NB: a[ñn]os / cumplea[ñn]os — fans and the model both write these
    # unaccented, so both spellings must match.
    r"tengo\s+\d{2}\s*a[ñn]os|vivo\s+en\s|estoy\s+(en|ac[áa]\s+en)\s|"
    r"crec[íi]\s|nac[íi]\s|soy\s+(de|argentina|chilena|colombiana|mexicana)\b|"
    r"me\s+llamo\s|"
    r"mi\s+(trabajo|novio|ex|esposo|mam[áa]|pap[áa]|hermano|hermana|familia|"
    r"perro|gato|tatuaje|cumplea[ñn]os|nombre)\b|"
    r"trabajo\s+en\s|estudi[ée]\s|hablo\s"
    r")",
    re.I,
)


# The other half of the gate. An ELLIPTICAL answer carries no first-person verb —
# "en córdoba, cora 😅" answering "¿y en qué parte vivís?" states her city while
# matching none of the patterns above. Observed in the real Chile cascade, where
# that exact line was the third step. So the gate ALSO fires when HIS last message
# was a biographical question about HER: the claim is then in the exchange even
# when it is not in her sentence.
#
# Patterns are the ones fans measurably use (inbound sweep, 2026-07-25, hits in
# brackets) rather than guesses: relationship [325], age [173], kids [163],
# location [92], name [89], family [82], job [81], upbringing [31].
_ASKS_ABOUT_HER_RE = re.compile(
    r"\b("
    r"how old (are|r) (you|u)|your age|what'?s your name|your real name|"
    r"where (do|are) (you|u) (live|from)|where (you|u) (live|from)|what city|"
    r"live alone|who do (you|u) live with|"
    r"do (you|u) have (a )?(boyfriend|kids|children|tattoos?)|are (you|u) (single|married)|"
    r"what do (you|u) do for|your job|do (you|u) work|did (you|u) grow up|"
    r"your family|your (mom|mum|dad|brother|sister)|"
    r"cu[áa]ntos a[ñn]os ten[ée]s|cuantos a[ñn]os tienes|tu edad|tu nombre|"
    r"d[óo]nde viv[íi]s|donde vives|en qu[ée] parte viv[íi]s|de d[óo]nde (eres|sos)|"
    r"viv[íi]s sola|vives sola|con qui[ée]n viv[íi]s|"
    r"ten[ée]s novio|tienes novio|sos soltera|eres soltera|tienes hijos|ten[ée]s hijos|"
    r"en qu[ée] trabaj[áa]s|tu trabajo|tu familia|d[óo]nde creciste|tu infancia"
    r")",
    re.I,
)


def asks_about_her(text: str | None) -> bool:
    """Is HE asking a biographical question about HER? Deterministic, and the
    second trigger for the pre-send check — see the note above."""
    return bool(_ASKS_ABOUT_HER_RE.search(text or ""))


def mentions_self_claim(text: str | None) -> bool:
    """Did SHE just state something about herself? Deterministic — this decides
    whether a claims extract is worth its money, so it must not itself cost a
    call. False negatives are survivable (the claim is simply unrecorded); false
    positives only cost one cheap extract."""
    return bool(_SELF_CLAIM_RE.search(text or ""))


def parse_persona_claims(raw) -> list[dict]:
    """`persona_claims_json` → [{topic, claim, at}]. Defensive: anything unparseable
    collapses to [] rather than raising into a send path."""
    data = load_json(raw, [])
    if not isinstance(data, list):
        return []
    out = []
    for row in data:
        if not isinstance(row, dict):
            continue
        topic = str(row.get("topic") or "").strip()[:40]
        claim = str(row.get("claim") or "").strip()[:_CLAIM_TEXT_CLIP]
        if topic and claim:
            out.append({"topic": topic, "claim": claim,
                        "at": str(row.get("at") or "")[:32]})
    return out


def merge_persona_claims(prior, new_claims, *, now_iso: str = "") -> list[dict]:
    """Accrete new claims onto the prior list — LATEST-WINS per topic, capped.

    Idempotent by construction: re-recording the same (topic, claim) rewrites the
    same row rather than growing the list, which is what makes the capture safe
    under the executor's job-retry (a run that crashes after sending re-runs the
    whole candidate loop)."""
    merged = {c["topic"]: c for c in parse_persona_claims(prior)}
    for row in parse_persona_claims(new_claims):
        merged[row["topic"]] = {**row, "at": row.get("at") or now_iso}
    # Newest last so the cap drops the STALEST topic, not the newest one.
    ordered = sorted(merged.values(), key=lambda c: c.get("at") or "")
    return ordered[-_CLAIMS_MAX:]


def resolved_claims(raw, *, age_claimed: str | None = None,
                    location_claimed: str | None = None,
                    job_claimed: str | None = None) -> list[dict]:
    """Everything she has told THIS fan, merged — one row per topic, in the exact
    order and with the exact values the prompt block prints.

    `persona_claims_block` renders this and nothing else, so the operator UI can
    show it and be showing the prompt rather than a second opinion about it. The
    alternative — rebuilding the override rule in TypeScript — is the same
    two-implementations-of-one-rule hazard that made the browser and the server
    disagree about which media had a readable still.

    Each row carries `column`: the PATCH-editable field behind it, or None. Only
    three topics have one, and that is a storage detail rather than a limit on
    what she can claim — every other topic still reaches the prompt from the
    ledger, it simply has no fast path to correct it on."""
    rows = parse_persona_claims(raw)
    overrides = (("persona_age_claimed", "your age", age_claimed),
                 ("persona_location_claimed", "where you are", location_claimed),
                 ("persona_job_claimed", "your work", job_claimed))
    overridden = {col for col, _label, val in overrides if nonempty(val)}
    out: list[dict] = [
        {"label": label, "value": str(val).strip()[:_CLAIM_TEXT_CLIP],
         "column": col, "at": None}
        for col, label, val in overrides if col in overridden
    ]
    out += [
        {"label": row["topic"], "value": row["claim"],
         "column": _column_for_topic(row["topic"]), "at": row.get("at")}
        for row in rows if _column_for_topic(row["topic"]) not in overridden
    ]
    return out


def persona_claims_block(raw, *, age_claimed: str | None = None,
                         location_claimed: str | None = None,
                         job_claimed: str | None = None) -> str:
    """The "you already told HIM this" block. "" when the fan has no deviations —
    which is most fans once canon is filled, so the prompt is byte-identical for
    them and this costs nothing.

    Goes in the USER message (per-fan, never prefix-cached) and is phrased as an
    imperative. The passive "What you know about him" inventory is the one the
    model demonstrably ignores — it asked a $691 fan his job with `occupation`
    populated AND injected. The imperative clock line is the one that held."""
    rows = parse_persona_claims(raw)
    # The three scalars are an OPERATOR OVERRIDE, not a second copy. of_ai_chat
    # mirrors extracted claims into them (they are serialized by fans.py and
    # PATCH-editable, which is where the correction UI comes from), so a set
    # scalar WINS over every list row on its topic and that row is not printed.
    #
    # Matching is by TOPIC, not by text. Text-matching looked equivalent while the
    # scalar merely echoed the list — but the moment an operator PATCHed a
    # correction the texts diverged and BOTH rendered, or, before that, the
    # scalar was suppressed by its own mirror and the PATCH silently did nothing.
    # Topic-matching is what makes "humans may correct what gen_info claimed"
    # (fans.py) actually reach the prompt.
    lines = [f"- {r['label']}: {r['value']}" for r in resolved_claims(
        raw, age_claimed=age_claimed, location_claimed=location_claimed,
        job_claimed=job_claimed)]
    return _pinned_block(
        "YOU HAVE ALREADY TOLD HIM THE FOLLOWING ABOUT YOURSELF. It is true for "
        "him now, whatever else you might say to anyone else — repeat it "
        "consistently and NEVER give him a different version:", lines)




# ── Claim topics → the three scalar mirror columns ────────────────────
# `fans.persona_age_claimed` / `_location_claimed` / `_job_claimed` are the fast
# path for the topics fans ask most; they were already serialized by fans.py and
# PATCH-editable, so mirroring into them gives the correction UI for free.
#
# This is a lookup table, not control flow — it was originally inlined as a
# nested loop over a tuple-of-tuples in the middle of _extract_and_fill's persist
# path, which is exactly where a reader does not want to meet a keyword matcher.
#
# Matching is substring-on-lowercase because the topic strings are authored by
# the model, not chosen from an enum. That is deliberately forgiving: a missed
# mirror costs nothing (the claim is still carried in persona_claims_json and
# still reaches the prompt), so a loose match is the cheap side of the trade.
_CLAIM_TOPIC_COLUMNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("persona_age_claimed", ("age", "edad")),
    ("persona_location_claimed", ("location", "hometown", "city", "lives",
                                  "country", "ciudad", "pais", "país")),
    ("persona_job_claimed", ("job", "work", "trabajo", "occupation")),
)


def _column_for_topic(topic: str) -> str | None:
    """Which mirror column a model-authored topic string belongs to, if any.

    One lookup shared by the mirror writer and the renderer's override rule — if
    they disagreed about what "hometown" maps to, a PATCHed correction would land
    on a topic that still printed its own row underneath."""
    t = (topic or "").lower()
    for col, keys in _CLAIM_TOPIC_COLUMNS:
        if any(k in t for k in keys):
            return col
    return None


def claims_to_scalars(merged: list[dict]) -> dict[str, str]:
    """The mirror columns implied by a merged claims list, newest-wins.

    `merged` is ordered oldest→newest (see merge_persona_claims), so a later row
    on the same topic overwrites an earlier one — the same latest-wins rule the
    list itself follows, rather than an accident of iteration order."""
    out: dict[str, str] = {}
    for row in merged:
        col = _column_for_topic(row["topic"])
        if col:
            out[col] = row["claim"][:200]
    return out


# ── The one place the identity preamble is assembled ──────────────────
def compose_persona(cfg, *, fallback: str) -> str:
    """The creator's full identity preamble for a chat prompt: her prose persona,
    her pinned location, then the structured canon.

    ONE composer because there used to be three, and they had already diverged:
    deep_convo folded the location line into the persona string (threading a
    param through _generate/_generate_leadin was awkward) while of_ai_chat,
    ai_chatter and autoreply each carried a `location` builder param, a
    `location_block` local, a `_load_location()` call and an argument at the call
    site. Same concept, two mechanisms, and nothing stopping a fourth engine
    picking a third. Composing here deletes that plumbing outright and makes the
    divergence impossible rather than merely fixed.

    Safe to call with cfg=None (no brain row yet). Every part is optional and
    renders nothing when unset, so an account with no location and no canon gets
    exactly `fallback` — byte-identical to the prompt that shipped before any of
    this existed."""
    persona = (getattr(cfg, "persona", None) or "").strip() or fallback
    location = _persona_location_line(getattr(cfg, "location", None)).rstrip()
    if location:
        persona = f"{persona}\n{location}"
    # The voice rides on `cfg`, which every caller already holds — so the lane
    # reaches the canon labels with no signature change and no engine has to know
    # this module consults it. `getattr` because the tests (and any caller with a
    # hand-built config object) predate the column.
    facts = _persona_facts_block(getattr(cfg, "persona_facts_json", None),
                                 getattr(cfg, "voice", None))
    return f"{persona}\n\n{facts}".rstrip() if facts else persona


# ── PHASE 2: the pre-send consistency check ──────────────────────────
# The guardrail states the rule and the ledger supplies the answers, but nothing
# VERIFIED the draft. That matters because the model demonstrably ignores facts it
# can already see: measured on prod, 323 of 468 times the AI re-asked a question
# it had already asked, the earlier asking was still inside the history window,
# and it asked a $691 fan his job with `occupation` populated AND injected.
# Injection is necessary and not sufficient; this is the sufficiency half.
#
# COST. Never run this on every reply — most replies say nothing about her. It is
# gated on mentions_self_claim() over the DRAFT (not the history), the same
# deterministic predicate the claims extract uses, measured at 5.06% of real 1:1
# outbound. A reply that makes no self-claim cannot contradict one, so skipping it
# is free rather than a heuristic.
#
# The verifier returns the REMEDY as well as the verdict: one call yields both,
# and a model-written fix is in her voice and her language, where a canned
# deflection would be neither. Rewriting outbound text at the send chokepoint is
# established here — guard_offplatform already does it.
_CONSISTENCY_SYSTEM = (
    "You check one outgoing message from an OnlyFans creator against what she has "
    "already established about herself, and you are STRICT about contradictions "
    "but generous about everything else.\n"
    "You are given HER PROFILE (true of her everywhere) and, when present, WHAT "
    "SHE ALREADY TOLD THIS FAN (true for this conversation, even where it differs "
    "from the profile — she cannot un-say it).\n"
    "Respond with a SINGLE JSON object and nothing else:\n"
    '{"contradicts": true|false, "which": "", "fix": ""}\n'
    "RULES:\n"
    "- `contradicts` is true ONLY when the message states something about HERSELF "
    "that cannot both be true alongside the profile or what she already told him "
    "— a different age, city, country, job, living situation, relationship, "
    "family, or pet. A different WORDING of the same fact is not a "
    "contradiction.\n"
    "- Flirting, teasing, fantasy, plans, moods, opinions, sexual talk and "
    "anything about HIM are NEVER contradictions. Neither is a fact she simply "
    "has not mentioned before — she is allowed to say new things.\n"
    "- When in doubt, answer false. A false alarm rewrites a message that was "
    "fine, which is worse than letting a borderline line through.\n"
    "- `which`: if true, name the conflict in a few words (e.g. \"says Chile, "
    "already told him Argentina\"). Empty when false.\n"
    "- `fix`: if true, rewrite HER message so it agrees with what she already "
    "said. Keep her exact voice, length, punctuation, emoji and LANGUAGE, and "
    "change as little as possible — only the contradicting detail. Never "
    "apologise, never correct herself out loud, never mention a mistake, never "
    "say she forgot. Empty when false."
)


def consistency_messages(draft: str, persona: str, claims_block: str) -> list[dict]:
    """The (system, user) pair for the pre-send check. `claims_block` is
    persona_claims_block's output — "" for a fan with no deviations, which is most
    fans, and then the profile alone is the yardstick."""
    told = claims_block.strip()
    user = (f"HER PROFILE:\n{persona.strip()}\n\n"
            + (f"{told}\n\n" if told else "")
            + f"HER OUTGOING MESSAGE:\n{draft.strip()}\n\n"
            "Does this message contradict anything above about HER?")
    return [{"role": "system", "content": _CONSISTENCY_SYSTEM},
            {"role": "user", "content": user}]


def parse_consistency_verdict(raw) -> tuple[bool, str, str]:
    """(contradicts, which, fix). Defensive: anything unparseable, or a `true`
    verdict with no usable `fix`, reads as CLEAN.

    Failing open is deliberate. This sits between generation and send, so a bad
    parse must let the original message through — a verifier that can silently
    swallow replies is a worse failure than the contradiction it exists to catch."""
    data = load_dict(raw)               # NULL / prose / half-written JSON → {} → CLEAN
    if not bool(data.get("contradicts")):
        return (False, "", "")
    fix = str(data.get("fix") or "").strip()
    if not fix:
        return (False, "", "")          # a verdict we cannot act on is not a verdict
    return (True, str(data.get("which") or "").strip()[:120], fix)


def fan_claims_block(f: Fan) -> str:
    """persona_claims_block for a Fan row — the free-text ledger plus the three
    scalar columns that override it.

    One argument instead of four getattrs, because the long form had reached three
    verbatim copies (both chat builders and the consistency check) and every new
    caller was another place to spell the column names."""
    return persona_claims_block(
        getattr(f, "persona_claims_json", None),
        age_claimed=getattr(f, "persona_age_claimed", None),
        location_claimed=getattr(f, "persona_location_claimed", None),
        job_claimed=getattr(f, "persona_job_claimed", None))


async def verify_self_consistency(account_id: str, fan_id: int, f: Fan,
                                  draft: str, persona: str, model: str,
                                  purpose: str,
                                  last_inbound: str = "") -> str:
    """PHASE 2 — check the DRAFT against her canon and this fan's ledger, and
    return the message to actually send (the draft, or a minimal rewrite).

    The guardrail states the rule and the ledger supplies the answers; nothing
    verified the outcome. That gap is measurable: 323 of 468 times the AI re-asked
    a question it had already asked, the earlier asking was still inside the
    window, and it asked a $691 fan his job with `occupation` populated AND
    injected. Injection is necessary, not sufficient.

    GATED, so this is not a second call per reply: it fires only when the draft
    itself makes a self-claim (mentions_self_claim — the same deterministic
    predicate the claims extract uses, measured at 5.06% of real 1:1 outbound),
    and only when there is something to check against. A reply that says nothing
    about her cannot contradict anything, so skipping it is free rather than a
    guess.

    FAILS OPEN at every step. It sits between generation and send, so a cap, a
    timeout, a bad parse or an unusable verdict all return the draft untouched — a
    verifier that can silently swallow replies would be a worse failure than the
    contradiction it exists to catch. LLMCapExceeded is caught here for the same
    reason: the reply must still go out."""
    # Two triggers. Her own first-person claim, OR his biographical question —
    # an elliptical answer ("en córdoba, cora 😅") states a fact while matching no
    # first-person pattern, and that was step three of the real Chile cascade.
    if not draft.strip():
        return draft
    if not (mentions_self_claim(draft) or asks_about_her(last_inbound)):
        return draft
    claims_block = fan_claims_block(f)
    if not persona.strip() and not claims_block:
        return draft            # nothing to be inconsistent WITH
    try:
        res = await llm_client.chat(
            model=model,
            messages=consistency_messages(draft, persona, claims_block),
            # Deliberately the CALLING ENGINE's purpose, not a distinct
            # "…_consistency" one: purpose is the key into
            # account_ai_config.model_by_purpose, so a new value would sit outside
            # any per-purpose model override an operator has set. The tradeoff is
            # accepted and real — this call is therefore NOT separable from the
            # reply call in grok_calls, so the 2.09% gate rate is measured offline
            # rather than queryable in prod.
            purpose=purpose, account_id=account_id, fan_id=fan_id,
            temperature=0.0, response_format={"type": "json_object"},
        )
        verdict = getattr(res, "parsed", None)
        contradicts, which, fix = parse_consistency_verdict(
            verdict if verdict is not None else res.content)
    except LLMCapExceeded:
        return draft
    except Exception:
        log.warning("consistency check failed account=%s fan=%s", account_id, fan_id,
                    exc_info=True)
        return draft
    if not contradicts:
        return draft
    log.info("consistency fix account=%s fan=%s which=%s", account_id, fan_id, which)
    return fix
