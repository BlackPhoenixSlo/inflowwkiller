"""service/automations/_voice.py — whose voice is this account writing in?

ONE question, one file: is the creator on this account a woman (every account
that has ever run here) or a man (the new lane)? Every string that reads
differently because of that answer lives HERE, in both lanes, and `_common`
re-exports the female ones so nothing else had to move.

WHY A LANE AND NOT A PERSONA EDIT
---------------------------------
The persona box is prose, prepended to a system prompt that then states the
creator's identity as a RULE a few lines below it — `of_ai_chat`/`ai_chatter`
say "a real 22yo girl" outright, and `_persona`'s canon block says "THESE ARE
THE FACTS ABOUT YOU ... you must never say anything that contradicts them" over
labels reading "Why she started OF". A model handed prose-context and a rule
takes the rule, so no amount of persona text fixes a string that is not
conditional on anything. It has to be a lane.

WHY THE TEXT LIVES HERE AND NOT IN `_common`
--------------------------------------------
The first cut of this module kept the female text in `_common` and patched it —
`base.replace(FRAME_HER, FRAME_HIM)`. Two things were wrong with that and both
are the same thing: the varying spans were not first-class. Edit a comma in
`_common` and the male branch silently serves female text, so the mechanism
needed a test asserting its own anchors still matched — a test that exists only
because the design is fragile is the design telling you it is fragile. And the
`base` argument itself was an artifact of the import direction (`_common`
imports this module, so this module could not import back), i.e. an API bent
around a dependency edge rather than an ownership decision.

Owning both lanes fixes all of it: the blocks are BUILT from their varying
parts, so there is no patching, nothing to drift, and no anchor test.

THE ONE INVARIANT
-----------------
`voice` is NULL on every existing row and `norm_voice` maps NULL/""/garbage to
`VOICE_HER`. Everything below returns the EXACT text that shipped before this
module existed when the voice is `VOICE_HER`. That is not a nicety — it is the
whole safety argument for adding a second lane to prompts that earn money today,
and `tests/test_voice_lane.py` asserts it rather than trusting it.

Same shape as `_language.py`, deliberately: a nullable per-account column, one
module owning the variance, a `norm_*` that collapses everything unknown to the
default, and an async loader the engines call once per run.

HOW ENGINES USE IT
------------------
Resolve ONCE per run and read fields off the bundle:

    v = await _common.load_voice_blocks(account_id)   # ONE row read, BOTH axes
    ...
    f"{v.painful_texting}\\n\\n{v.live_proof}"

The loader lives in `_common`, not here, because it needs the account row and
this module deliberately does not import the database — see the import block.
There is no `load_account_voice`: resolving the voice without the customs flag
is what let `of_ai_chat` ship half a lane, so the two axes only come as a pair.

Not `_voice.painful_texting(CONST, voice)` at each site. `PAINFUL_TEXTING` and
`LIVE_PROOF_GUARDRAIL` are interpolated raw at ~11 places across six engines;
a per-constant wrapper call would make extending the lane a matter of
remembering to wrap each one, where forgetting renders female text on a male
account with no error anywhere. A bundle turns that silent-wrong into an
AttributeError and keeps new voice-varying blocks a one-file change.

WHAT IS **NOT** HERE
--------------------
The FAN. He is male in both lanes — this account's fans are men either way — so
every "him/his/he" in a prompt is correct as written and none of it routes
through this file. Only the CREATOR's half flips.
"""
from __future__ import annotations

from dataclasses import dataclass

# Pure text, like this module — no database, no engine. The customs PRICE lives
# there because that is where the order FLOOR lives, and a sell-side band that
# can drift from the watch-side floor is the bug it exists to prevent.
from . import _customs

# NO database import, deliberately. This module is text + normalisation and
# nothing else; the account read that resolves a bundle lives in `_common`
# (`load_voice_blocks`), which already owns the account loaders and already reads
# the row this needs. Keeping storage out means `blocks()` is a pure function of
# its arguments, which is what makes the byte-identity test a real proof rather
# than a fixture-dependent one.

VOICE_HER = "her"          # the shipped default: a female creator
VOICE_HIM = "him"          # the male lane
_VOICES = (VOICE_HER, VOICE_HIM)


def parse_voice(v: object) -> str | None:
    """A legal voice, or None when the value is not one.

    THE normalisation boundary, in its honest form. `norm_voice` below is this
    plus the storage default, and the two exist separately because the callers
    genuinely differ:

      • reading STORAGE, a bad value must degrade to the shipped lane — the
        failure mode is "stays a girl", which is the safe direction on a send
        path. That is `norm_voice`.
      • validating an API WRITE, a bad value must be rejected — a typo'd 'hom'
        silently resolving to "her" is the exact silent-wrong-gender failure the
        lane exists to prevent. That is this function.

    Before this split the API had to ask "was it recognised?" by re-deriving
    `str(v).strip().lower()` itself and comparing — a second copy of the
    normalisation that would drift the day this one changed."""
    if not isinstance(v, str):
        return None
    s = v.strip().lower()
    return s if s in _VOICES else None


def norm_voice(v: object) -> str:
    """Any stored value → a legal voice. NULL, "", whitespace, an unknown string
    and a non-string all collapse to `VOICE_HER`, so a corrupt column can never
    silently flip an account's identity.

    Call it on values arriving from storage; everything downstream takes an
    already-normalised `str` and compares directly, so a reader never has to
    wonder which side of the boundary it is on."""
    return parse_voice(v) or VOICE_HER


# ── The felt cost of texting ─────────────────────────────────────────
# Governs everything below it in the prompt and is the brevity governor — the
# reason replies read as texts rather than paragraphs. Its MECHANIC transfers to
# a male lane untouched (terseness reads as authority); only the opening FRAME
# and the FLOOR clause carry gender. Building the block from those two parts is
# what lets the male lane keep the governor instead of trading it for
# correctness by switching the whole block off.
_PAINFUL_FRAME = {
    VOICE_HER: (
        "texting is a chore, a little painful, like a real girl half-glued to her "
        "phone. so you write the LEAST you can while still saying what you actually "
        "mean."
    ),
    VOICE_HIM: (
        "texting is a chore and you are not eager. you write the LEAST you can while "
        "still saying what you actually mean — a short line reads like a man with "
        "better things to do, and that is exactly right."
    ),
}
# Hers requires every reply to carry "heat, a tease, or warmth"; his may land as
# flat certainty or amusement instead. Same rule (never a dead one-word filler),
# register corrected.
_PAINFUL_FLOOR = {
    VOICE_HER: (
        "but short NEVER means dead or dodgy: even a tiny reply has to carry heat, a "
        "tease, or warmth AND actually engage what mattered in his message"
    ),
    VOICE_HIM: (
        "but short NEVER means dead or dodgy: even a tiny reply has to land — heat, "
        "amusement, or flat certainty — AND actually engage what mattered in his "
        "message"
    ),
}


def _painful_texting(voice: str) -> str:
    """Operator's framing, verbatim source intent:
      "texting is painful — we write as few words as possible while still saying
       all we can. we dont overthink shortening: if typing a little longer is
       easier than agonizing over the perfect short line, do that. usually short
       works perfectly. writing a little longer JUST to amuse ourselves is worth
       it. but if a short text punches the emotion, we take the short one. in
       short: typing is painful UNLESS we get positive emotion out of it."
    Injected at the TOP of the conversational system prompt as the governing
    feel, above the mechanical HOW-YOU-TEXT rules."""
    return (
        "THE FEEL OF TEXTING (read first — this governs everything below):\n"
        f"{_PAINFUL_FRAME[voice]} "
        "dont labor over the perfect tiny line — if a slightly longer line "
        "comes out easier than agonizing, thats fine, but usually short just works. "
        "the reasons worth spending more than the bare minimum: it amuses YOU to, a "
        "longer line lands the emotion harder, or you get to make HIM feel something "
        "(stirring him up, making him grin, turning him on) — enriching his emotion "
        "is its own little pleasure, so those words arent wasted. otherwise every "
        "extra word is effort you skip. a short line that PUNCHES the feeling beats a "
        "long one that explains — every single time.\n"
        # a floor so 'short' never collapses into a dead one-word filler / echo,
        # but the floor MUST NOT become a licence to ramble — fewest words wins
        f"{_PAINFUL_FLOOR[voice]} — never a "
        "flat 'ok'/'lol'/'nice'/'haha' anyone could've sent, never just his own words "
        "parroted back, and never a cute one-liner that sidesteps his real point. the "
        "target is the FEWEST words that land the feeling AND address him: almost "
        "always ONE line, occasionally two, basically never more — dont pile on extra "
        "bubbles, dont explain yourself, dont pad it.\n"
        # brevity doesn't get to dodge a real question he asked — but stay tight
        "if he actually ASKED you something, answer it — but in as few words as it "
        "takes and then STOP; dont let 'answering him' balloon into a paragraph.\n"
        "the get-to-know-you question is a JUDGEMENT CALL, not a habit: sometimes "
        "slipping in a little backend-info question is exactly what the moment wants, "
        "sometimes dropping the question entirely and just reacting hits harder, and "
        "sometimes keeping the question you had in mind is right. read the moment — "
        "dont ask on autopilot and dont drop it on autopilot either."
    )


# ── Live proof / FaceTime ────────────────────────────────────────────
# Fans constantly push for FaceTime / a live call / an on-demand selfie / "prove
# you're real". Left unaddressed the model fumbles with weak coy half-deflections
# ("hm okay") that read badly. The RULE is neutral and identical in both lanes;
# only the two worked examples carry a voice — and they matter more than they
# look, being the only concrete register the model is shown for a refusal, which
# is exactly where an alpha persona most needs to sound like itself.
#
_LIVE_PROOF_EXAMPLES = {
    VOICE_HER: (
        "e.g. \"i don't do facetime babe, but stay n talk to me\" or \"no live "
        "calls hun, you get me right here\"."
    ),
    VOICE_HIM: (
        "e.g. \"i don't do facetime. talk to me here\" or \"no live calls. you get "
        "me right here or not at all\"."
    ),
}


# ── The customs carve-out (account opt-in, DEFAULT OFF) ──────────────
# Appended ONLY when the account sells customs (`_common.SELL_CUSTOMS_KEY`). It
# is a separate sentence deliberately: the refusal above is not softened by one
# word, so a fan asking to FaceTime gets the identical flat "no" whether or not
# customs are on. This only adds a thing the creator MAY offer instead.
#
# ⚠️ THE FENCE — ONE OWNER, THREE LEAD-INS.
#
# A carve-out worded loosely — "you can send him a voice note" — reads to a model
# as permission to be helpful in real time, and one over-eager reply agreeing to a
# video call is both a bot-detection trap and an OF policy risk. So the permission
# carries FOUR conditions (paid / recorded later / delivered here on OnlyFans /
# never live) plus an absolute ban on naming WHEN it lands.
#
# Those conditions are stated ONCE, here, because three surfaces need them and the
# first attempt wrote all three by hand:
#
#   _live_proof_guardrail  (below)                  — as a REFUSAL rule
#   _common.build_tip_ask_block                     — as a SELLING rule
#   ai_chatter._manifest_block                      — as a MANIFEST rule
#
# They drifted immediately. Tightening the ETA ban from "never promise a specific
# time or day" to "never say WHEN, including how long" landed on two of the three,
# and `ai_chatter` kept the weak form — so "give me an hour" stayed a compliant
# reply on the engine that does the actual selling. A safety property restated in
# three voices is a safety property with no owner.
#
# The LEAD-IN differs per surface and should: one is refusing a call, one is
# pricing a tip, one is bounding a manifest. Everything from "He pays FIRST"
# onward is policy and is identical by construction. `test_voice_lane` asserts all
# three rendered surfaces carry it.
# ⚠️ THE V1 SCOPE IS PART OF THE POLICY, NOT PART OF THE LEAD-IN.
#
# Operator ruling 2026-08-02: "tip ask will be for custom voicenotes at start,
# later maybe short video or calls (start with voicenotes as its what mostly is,
# rest is non important)". A VOICE NOTE is the only product v1 sells.
#
# That ruling first landed in the tip-ask lead-in only — "Only offer a voice note
# — not a video, not a call" — while the refusal carve-out and the ai_chatter
# manifest both still said "a voice note or short clip". So an opted-in
# `of_ai_chat` prompt carried BOTH rules at once, and `ai_chatter` — the engine
# that closes the sale — permitted short clips with no voice-note-only clause
# anywhere in its prompt. Exactly the failure the shared constant below was
# created to end, repeated in the half of the string that had not been shared yet.
#
# So the scope sentence lives HERE, with the conditions, and widening v1 to clips
# or calls is a one-line edit in one file rather than three edits someone has to
# remember to keep in step.
CUSTOMS_CONDITIONS = (
    "It is ALWAYS a voice note — never a video, never a photo set, never a live "
    "call, and never anything else, no matter what he asks for or offers to pay. "
    "He pays FIRST, you record it LATER, and it arrives here on OnlyFans once it's "
    "made — it is not live and it is not a call. NEVER present it as happening now, "
    "in real time, or while he waits. NEVER say WHEN it will land — not a time, not "
    "a day, and not how long it will take. No 'in an hour', no 'later tonight', no "
    "'by tomorrow', no estimate of any kind: you do not know, and a guess becomes a "
    "promise he will hold you to. And it NEVER turns a facetime/live-call request "
    "into a yes — the answer to a live call is still no."
    # The price rides the SHARED fence for the same reason the scope sentence
    # does: it is policy, it must be identical on all three surfaces, and the
    # engine that closes the sale is the one that had no figure at all.
    + _customs.PRICE_RULE
)

_CUSTOMS_CARVE_OUT = (
    " ONE THING YOU **DO** OFFER: a paid CUSTOM — a VOICE NOTE you RECORD LATER and "
    "send him here on OnlyFans once it's made. You may offer or agree to that. "
    f"{CUSTOMS_CONDITIONS}"
)


def _live_proof_guardrail(voice: str, sell_customs: bool = False) -> str:
    return (
        "LIVE PROOF / FACETIME (hard rule): if he asks to FaceTime, video/live call, "
        "verify you're real, 'prove it', 'show it now', or send a live/on-demand "
        "selfie, be BLUNT and DIRECT — clearly say you don't do facetime / live calls "
        "/ on-demand proof. NO apology, NO 'hm okay', NO coy fumbling, and never half-"
        "agree or play along. Stay flirty and in character, then redirect straight "
        "back to chatting or teasing. Keep it to one short flat refusal + one pivot "
        f"line, {_LIVE_PROOF_EXAMPLES[voice]}"
        f"{_CUSTOMS_CARVE_OUT if sell_customs else ''}"
    )


# ── Off-platform deflections ─────────────────────────────────────────
# `guard_offplatform` DISCARDS the model's draft and sends one of these verbatim
# when a reply leaks contact info or a meetup. That makes them the one place a
# canned voice reaches a fan with no model in the loop and no toggle over it —
# "mmm i only do this on here babe" from an alpha persona ends that thread. Fans
# push for contact details constantly, which is why the guard exists at all, so
# this fires often enough to matter on day one.
#
# Equal length in both lanes on purpose: the caller picks with a seeded rng, so a
# matching count keeps the pick stable in shape and makes a future third lane
# obviously wrong if it ships a different one.
OFF_DEFLECTIONS = {
    VOICE_HER: (
        "u dont need my number when ur right here 😏 keep me company",
        "mmm i only do this on here babe, talk to me",
        "lets keep it just between us right here 😉 tell me more",
        "ur sweet but im all yours on here, what else u thinkin about",
        "i stay on here only, come closer n tell me more",
        "no need to go anywhere, ive got u right here babe",
    ),
    VOICE_HIM: (
        "you dont need my number. im right here",
        "i only do this on here. talk to me",
        "keep it between us, right here. go on",
        "not happening. what else you thinkin about",
        "i stay on here only. come closer n tell me more",
        "no need to go anywhere. ive got you right here",
    ),
}


# ── Persona canon labels ─────────────────────────────────────────────
# `_persona.PERSONA_FACT_FIELDS` labels three slots in the third person female.
# They render inside "THESE ARE THE FACTS ABOUT YOU", so on a male account the
# prompt asserts, as unbreakable fact, that the creator is a woman — strictly
# worse than a stray pronoun in a style line. Only the LABELS differ; the KEYS
# (`why_of`, `ex`, `her_type`) are storage and never move, so an account can be
# flipped either way without touching `persona_facts_json`.
_FACT_LABELS = {
    VOICE_HER: {},          # every label as PERSONA_FACT_FIELDS declares it
    VOICE_HIM: {
        "why_of": "Why he started OF",
        "ex": "His ex",
        "her_type": "His type",
        # The two operator-only slots. Easy to miss — they are the last entries in
        # the slate and the only ones the 🪄 Enrich pass never touches, so they get
        # read less than the rest. `limits` in particular is where "no live calls"
        # is typed, which makes it load-bearing for the customs carve-out below.
        "kinks": "What he's into",
        "limits": "What he won't do",
    },
}


def fact_labels(voice: object) -> dict[str, str]:
    """Label OVERRIDES for the persona canon, keyed by field. Empty for the
    female lane, so the caller's `.get(key, declared_label)` is a no-op — one
    dict lookup hoisted out of the 26-field render loop rather than a
    per-field call that mostly returns its own argument.

    Returns a COPY. The female entry is a mutable `{}` and handing the module's
    own dict to a caller is an escape hatch onto shared state that nothing needs;
    26 keys twice per render is not worth the class of bug it invites."""
    return dict(_FACT_LABELS[norm_voice(voice)])


# ── Persona canon PLACEHOLDERS ───────────────────────────────────────
# The example text under each canon box. Same column of the same concept as the
# label above — what the operator writes AGAINST.
#
# ⚠️ TOTAL PER LANE, AND DELIBERATELY NOT THE SAME SHAPE AS `_FACT_LABELS`.
#
# The labels above are OVERRIDES because 21 of the 26 are the field's NAME, not
# her copy: "Age", "Kids", "Pets" are what the slot is called, in any lane.
# A placeholder is never that. It is a worked example of the creator's own life,
# so every one of them carries a voice and there is no such thing as a neutral
# default to fall back to. Both lanes are therefore written out in full and
# `fact_placeholders` is TOTAL — the caller indexes it and cannot express
# "fall through to the other lane", which is the whole point:
#
#   the first cut kept only HER table and derived his by regexing it for
#   she/her/girl/woman and blanking the matches. It caught only what the word
#   list knew — `job | "content creator, waitressed before"` sailed through, so
#   21 of 26 shipped as her copy — and where it did fire it only DELETED, so
#   `tattoos` lost its hint instead of getting one. Replacing the regex with a
#   `.get(key, hers[key])` fallback would have kept the same defect in a quieter
#   form: a canon slot added later with no male value silently serves hers.
#
# `assert_canon_parity` below binds both tables to the declared slate AT IMPORT,
# so that slot cannot exist. Keys are storage and never move (`her_type` is
# "His type" here, same column), so an account can be flipped either way without
# touching `persona_facts_json`.
_FACT_PLACEHOLDERS: dict[str, dict[str, str]] = {
    # HER: the table exactly as it shipped, moved here from `_persona` — one
    # column, one owner. It had no other reader.
    VOICE_HER: {
        "age": "22",
        "birthday": "24 May",
        "height": "165 cm / 5'5\"",
        "job": "content creator, waitressed before",
        "why_of": "to pay off her studies",
        "school": "studied design, dropped out",
        "home_city": "Córdoba",
        "home_country": "Argentina",
        "born_city": "Rosario",
        "born_country": "Argentina",
        "upbringing": "normal childhood, played in the street with her cousins",
        "living_situation": "lives alone in a small flat",
        "relationship": "single",
        "ex": "3 years, ended badly, doesn't talk about him",
        "kids": "none",
        "family": "mum, one older brother",
        "pets": "a grey cat, Nube",
        "tattoos": "script on her ribs, small rose on left wrist",
        "languages": "Spanish, some English",
        "music": "grunge, mostly Alice in Chains",
        "travel": "Chile once, wants to see Europe",
        "dreams": "save for her own place",
        "routine": "up late, films in the afternoon",
        "her_type": "older, calm, makes her laugh",
        "kinks": "you fill this in — Enrich never guesses",
        "limits": "you fill this in — Enrich never guesses",
    },
    VOICE_HIM: {
        "age": "32",
        "birthday": "24 May",
        "height": "185 cm / 6'1\"",
        "job": "content creator, construction before",
        "why_of": "to pay off his own gym",
        "school": "trade school, never finished",
        "home_city": "Austin",
        "home_country": "United States",
        "born_city": "Detroit",
        "born_country": "United States",
        "upbringing": "normal childhood, played ball in the street with his cousins",
        "living_situation": "lives alone, small place near the gym",
        "relationship": "single",
        # "doesn't talk about it", not "about her" — the ex IS a woman here, so
        # the pronoun would be correct, and a reader scanning for a leak would
        # have to stop and work that out every time. Not worth the doubt.
        "ex": "3 years, ended badly, doesn't talk about it",
        "kids": "none",
        "family": "mum, one older brother",
        # Not a cat. The male lane's sticker pack is dogs and wolves for the same
        # reason: the pet is register.
        "pets": "a pitbull, Rocco",
        "tattoos": "sleeve on his right arm, script across his chest",
        "languages": "English, some Spanish",
        "music": "grunge, mostly Alice in Chains",
        "travel": "Mexico once, wants to see Europe",
        "dreams": "save for his own gym",
        "routine": "up early, trains, films at night",
        "her_type": "younger, sharp, keeps up with him",
        # Not examples at all — an instruction, and identical in both lanes on
        # purpose: 🪄 Enrich never proposes either.
        "kinks": "you fill this in — Enrich never guesses",
        "limits": "you fill this in — Enrich never guesses",
    },
}


def fact_placeholders(voice: object) -> dict[str, str]:
    """The placeholder for EVERY declared canon slot, in this lane. TOTAL, not
    overrides — see the block above for why this column cannot be sparse the way
    `fact_labels` is.

    A COPY, and `norm_voice` so a corrupt column reads as the shipped lane."""
    return dict(_FACT_PLACEHOLDERS[norm_voice(voice)])


# ── Talking ABOUT the creator, in the third person ───────────────────
# Everything else in this module is text the creator SAYS. This is the one shape
# that isn't: a prompt that instructs a model to author her background canon
# ("fill in the profile of a creator so her chat AI never has to improvise an
# answer about herself"). It is third-person instruction with no register in it —
# the register lives in the `persona` box, which the operator writes — so unlike
# a refusal example or a nudge line there is nothing to rewrite per lane except
# the grammar.
#
# That is why this is three words and not a per-lane copy of the prompt, and it
# is NOT the `base.replace(FRAME_HER, FRAME_HIM)` the header rejects: these are
# interpolated INTO a template at the varying spans, the same way
# `_PAINFUL_FRAME` is. Nothing patches a rendered string.
#
# ⚠️ Do not reach for this to de-gender creator VOICE. A reply, a caption or a
# canned line carries register, and swapping its pronouns leaves a woman's
# sentence with a man's pronouns in it — which is the entire reason every other
# table in this file is written out twice.
@dataclass(frozen=True)
class CreatorPronouns:
    subject: str        # she / he
    possessive: str     # her / his
    reflexive: str      # herself / himself


_PRONOUNS = {
    VOICE_HER: CreatorPronouns("she", "her", "herself"),
    VOICE_HIM: CreatorPronouns("he", "his", "himself"),
}


def pronouns(voice: object) -> CreatorPronouns:
    """The creator's third-person pronouns for THIS lane. See the block above for
    the one job this has and the several it does not."""
    return _PRONOUNS[norm_voice(voice)]


# One canon KEY is spelled female — `her_type`. Keys are STORAGE and deliberately
# never move (that is what lets an account be flipped either way without touching
# `persona_facts_json`), so the male lane cannot rename it. But a prompt that
# hands a model the key list has just written "her_type" into a male account's
# authoring turn, and no word scan catches it: `\bher\b` does not match across an
# underscore.
#
# So the male lane says out loud what the key is. "" for her — her keys read
# correctly, so every prompt appending this is byte-identical in the female lane,
# the same construction as `EMOJI_VOCAB_RULE` above.
STORAGE_KEY_NOTE = {
    VOICE_HER: "",
    VOICE_HIM: (
        " (`her_type` is a STORAGE key name, not a claim about him — it means the "
        "type of person HE goes for. Output it under that exact key anyway.)"
    ),
}


def assert_canon_parity(declared: tuple[tuple[str, str], ...]) -> None:
    """Bind both lane tables to the canon slate, AT IMPORT.

    `_persona` owns the slate and calls this the moment it has declared it, so a
    new canon field with no male form cannot boot the relay — the same discipline
    as `script_packs._assert_lane_parity`, and stronger than a test, which only
    fails for whoever runs it.

    It lives here and takes the slate as an argument because the dependency runs
    that way: `_persona` imports this module, never the reverse.

    NOT a rediscovery of the regex this replaced. The word scan is applied ONLY to
    the label column, where a value is one to three words NAMING a field — a scan
    is adequate for "Her ex" and inadequate for "content creator, waitressed
    before", which is exactly why the placeholder column is checked for COVERAGE
    instead.

    ⚠️ `raise AssertionError`, never a bare `assert` — matching
    `script_packs._assert_lane_parity` in mechanism as well as in timing. A bare
    `assert` is stripped by `-O`/`PYTHONOPTIMIZE`, so the whole guard would
    silently evaporate under a flag nobody sets today, which is exactly the kind
    of "off in production only" that makes an import-time check worse than the
    test it replaced."""
    import re

    def _check(ok: object, msg: str) -> None:
        if not ok:
            raise AssertionError(msg)

    keys = {k for k, _ in declared}
    for lane, table in _FACT_PLACEHOLDERS.items():
        _check(set(table) == keys,
               f"{lane} placeholders vs canon slate — missing "
               f"{sorted(keys - set(table))}, unknown {sorted(set(table) - keys)}")
    female = re.compile(r"\b(she|her|hers|herself|girl|girls|woman|women)\b", re.I)
    for lane, overrides in _FACT_LABELS.items():
        _check(not set(overrides) - keys,
               f"{lane} label override for an undeclared slot: "
               f"{sorted(set(overrides) - keys)}")
    him = _FACT_LABELS[VOICE_HIM]
    ungendered = [k for k, label in declared if female.search(label) and k not in him]
    _check(not ungendered, f"gendered label with no male form: {ungendered}")
    still = [k for k, v in him.items() if female.search(v)]
    _check(not still, f"male label override is still female: {still}")


# ── The humanizer: pushback register + emoji vocabulary ──────────────
# STYLE_HUMANIZER is the opt-in "text like a real person" block. Almost all of it
# is genuinely neutral — lowercase, no em-dash, don't echo his words, vary the
# length, at most one question — and transfers to a male creator untouched.
#
# TWO SPANS DO NOT.
#
# 1. "tease, be a lil BRATTY, push back sometimes". Bratty is a submissive-playful
#    register: it reads as someone performing for approval. On a dom it inverts the
#    whole power axis the lane exists to establish — and it is the one line in the
#    block that tells the model what ATTITUDE to hold, so it is load-bearing
#    exactly where being wrong costs most.
#
# 2. "0-1 emoji, never the same emoji twice" names a BUDGET and no VOCABULARY. The
#    model then picks from what it associates with an OF creator, which is the
#    female sexting set — 🥺 🥰 😘 💦 🥵 💕. Those do not read as flirty-from-a-man,
#    they read as a woman typing. Naming the set is the whole fix; the budget was
#    never the problem.
_HUMANIZER_PUSHBACK = {
    VOICE_HER: (
        "- dont be relentlessly upbeat or agreeable. tease, be a lil bratty, push "
        "back sometimes.\n"
    ),
    VOICE_HIM: (
        "- never eager, never agreeable by default, and NEVER seeking his approval. "
        "you tease by withholding, not by performing. push back, call him on things, "
        "let a flat one-liner do the work — amusement and certainty, not enthusiasm. "
        "you are not trying to be liked.\n"
    ),
}
# 0-1 emoji stays the budget in BOTH lanes — this only names which ones exist.
# Deliberately short: a long list gets sampled evenly and reads like a rotation.
# These are the ones that survive coming from a man without softening him.
_EMOJI_VOCAB = {
    VOICE_HER: "",     # unchanged — the shipped block names no set, and must not
    VOICE_HIM: (
        "- your emoji set is SMALL and dry: 😏 😈 🔥 💪 👊 🖤 🥊 🐺. one at most, "
        "often none, and never two replies in a row. NEVER use 🥺 🥰 😘 💕 💦 🥵 😍 "
        "🙈 ✨ 💅 or any cutesy/pleading face — those read as a woman typing and "
        "undo everything else in this block.\n"
    ),
}

# ⚠️ The vocabulary above lives inside the HUMANIZER, which is an OPT-IN style
# block (`style_config_json`, per automation). That is fine for the engines that
# carry the humanizer — and a trap for the ones that do not.
#
# `send_followup` is one that does not. Its prompt asks for "real emojis" and then
# says nothing at all about WHICH, so the model picks, and a model writing a
# flirty DM picks 😘🥰🥺 — the female set — with nothing to stop it. On the male
# accounts the only thing holding that back was `strip_emojis: true`, i.e. an
# account-wide switch that removes ALL emoji, including the dry ones he should be
# using. Turning that switch off to "let his emoji through" would have handed the
# model a free choice it gets wrong.
#
# So the vocabulary is exposed as a STANDALONE bundle field too. Hers is "" —
# every prompt that appends it is byte-identical for her, which is why this can be
# added to an engine without touching the female lane at all.
EMOJI_VOCAB_RULE = {
    VOICE_HER: "",
    VOICE_HIM: (
        "\n\nEMOJI (hard rule): your set is SMALL and dry — 😏 😈 🔥 💪 👊 🖤 🥊 🐺. "
        "One at most, often none. NEVER use 🥺 🥰 😘 💕 💦 🥵 😍 🙈 ✨ 💅 or any "
        "cutesy/pleading face — those read as a woman typing."
    ),
}


# ── The texter noun ──────────────────────────────────────────────────
# "guy" and not "man": the whole line exists to set a TEXTING register (short,
# lowercase, casual), and "man" reads formal enough to pull the register the
# wrong way. The age beside it already carries the seniority.
_TEXTER_NOUN = {VOICE_HER: "girl", VOICE_HIM: "guy"}

# What the creator calls a fan whose real name we could not resolve. This IS
# creator voice, not fan gender: "babe" is what SHE says, and it is the fallback
# baked into send_followup. From a male creator to a straight male fan it is the
# same wrong-register tell as a female FaceTime refusal.
#
# "boy" over the original "man": operator call 2026-08-03, off live output. The
# dom lane was already landing it unprompted — a fan opened "Yes Sir, please" and
# the reply came back "Good boy." — so it is the word the persona actually uses,
# and it carries the power dynamic that "man" (neutral, peer-to-peer) throws away.
#
# ⚠️ THIS FIRES ON A MISSING NAME, NOT ON A DYNAMIC. `of_display_name` is empty
# for ~80% of fans, so whatever sits here is what the account calls a STRANGER on
# an unsolicited nudge or a post-buy bridge. That rules out anything a fan has to
# have opted into — harder degradation belongs in the model's in-scene reply,
# where it can read his signals, not in a constant keyed off an empty column.
_FAN_ADDRESS = {VOICE_HER: "babe", VOICE_HIM: "boy"}


def _humanizer(voice: str) -> str:
    """The "text like a real person" block, built from its varying spans.

    Female output is byte-identical to the STYLE_HUMANIZER that shipped before
    this module existed (asserted against the golden, not assumed) — the female
    pushback line IS the original line, and the female emoji vocab is "" because
    the shipped block deliberately names no set."""
    return (
        "TEXT LIKE A REAL PERSON, NOT AN AI:\n"
        "- lowercase always. dont capitalize sentence starts or 'i'.\n"
        "- NEVER an em-dash or semicolon. ever.\n"
        "- NEVER repeat or quote his words back. dont echo his message, and dont "
        "restate it with an adjective ('sounds gorgeous', 'thats a whole mood', "
        "'dangerous in the best way') — biggest bot tell. react in your OWN words.\n"
        "- vary length wildly: sometimes one word, sometimes a short line, sometimes "
        "just dive straight into the thought with no reaction word at all.\n"
        "- DONT open every text with a reaction sound, and NEVER reuse the same opener "
        "two replies in a row (no 'oof' every time, no 'oof'->'oof'->'oof'). most "
        "replies should just start with the actual thing you're saying.\n"
        "- texting sounds are fine in MODERATION and ROTATED: lol, lmao, omg, ugh, hmm, "
        "wait, stop, oof — pick a different one each time, dont lean on any single one.\n"
        "- a tiny typo or missing apostrophe is fine (dont, im, ur, gonna).\n"
        f"{_HUMANIZER_PUSHBACK[voice]}"
        "- AT MOST ONE question, ever. never stack two questions in one reply.\n"
        f"{_EMOJI_VOCAB[voice]}"
        "- never explain yourself or over-clarify. 0-1 emoji, never the same emoji twice."
    )


# ── Unlock reactions ─────────────────────────────────────────────────
# `ai_chatter`'s watcher fires one of these the instant an unlock lands, with NO
# model in the loop — the bot only speaks "in voice" again the next time the fan
# actually writes. That makes this the SECOND canned-text surface after
# `OFF_DEFLECTIONS`, and the worse-placed of the two: it lands on the highest-
# emotion beat in the funnel, immediately after money moved.
#
# Hers is squealing — "omg enjoy babe 😘", "eeek ok enjoy 💕 dont be shy after".
# From a 38-year-old combat-sports coach that is not a wrong pet name, it is a
# different person answering. His are flat approval: the sale is acknowledged and
# the frame stays his.
#
# Equal length in both lanes, same reason as OFF_DEFLECTIONS — the caller picks
# with a seeded rng and a matching count keeps the pick stable in shape.
UNLOCK_REACTIONS = {
    VOICE_HER: (
        "omg enjoy babe 😘",
        "mmm enjoy 🙈 tell me what u think after",
        "ur the best 😏 enjoy",
        "eeek ok enjoy 💕 dont be shy after",
    ),
    VOICE_HIM: (
        "good man. enjoy",
        "thats what i like. tell me what you think after",
        "knew you would 😏 enjoy it",
        "go on then. dont be shy after",
    ),
}

# The fallback when the account's line pack has no rung for an escalation. NOT a
# rare path — the prod catalog runs ~34% empty rungs, so this ships often.
UNLOCK_PROMPT_FALLBACK = {
    VOICE_HER: "unlock this babe 😏",
    VOICE_HIM: "unlock it 😏",
}

# The aftercare fallback, same "pack has no line" path as above.
AFTERCARE_FALLBACK = {
    VOICE_HER: "mmm come here 🥰",
    VOICE_HIM: "come here",
}

# ── Re-engage nudges ─────────────────────────────────────────────────
# The THIRD model-free surface. `_run_nudge` picks one of these verbatim, so —
# like OFF_DEFLECTIONS and UNLOCK_REACTIONS — no prompt rule can reach it.
#
# Hers leans on 🙈 and 🥺 six times across fifteen lines, and both of those are on
# the male lane's EXPLICIT BAN list in `_EMOJI_VOCAB`. That is the sharpest form
# of the drift this module exists to stop: the prompt tells him never to type 🥺
# while the code types it for him, unprompted, into an unsolicited message.
#
# The register differs beyond emoji. Hers MISSES him — "i miss to talk with u",
# "dont leave me like this" — which is the pull that works from her. The same
# beat from a dom inverts: he is not pining, he noticed the silence and expects
# an answer. Five beats, three lines each, mirroring hers so a repeatedly-nudged
# fan never redraws the same line.
#
# ⚠️ HIS ARE NATIVE ENGLISH ON PURPOSE. Hers are hand-written in the non-native
# register (see `_NUDGE_LINES`) because `nonnative_ai_chatter` defaults ON and
# cannot reach canned text — including the space-before-'?' tic baked at 4-in-15.
# Broken English is a fact about those personas, not about all creators: it reads
# wrong on a 38-year-old English-speaking coach. If a male account ever wants the
# non-native register, that is a second pool here, not an edit to these.
NUDGE_LINES = {
    VOICE_HER: (
        # still there?
        "hey {name} u still there ? 🙈",
        "{name} u still there 🙈 or no",
        "hey {name} u are still with me ? 🙈",
        # miss talking / what are u up to
        "i miss to talk with u {name} 🥺 what u doing",
        "miss talk with u {name} 🥺 what u up to now",
        "i am missing u {name} 🥺 hiw is ur day",
        # went quiet / everything ok
        "{name} why u so quiet.. all ok with u",
        "u become so quiet {name}.. everything is ok ?",
        "u go quiet on me {name}.. is everything fine",
        # cant stop thinking / u around
        "i cant stop to think about our chat 😏 u are around {name}?",
        "our chat is still in my head 😏 u around {name} ?",
        "cant stop think about what we say 😏 {name} u there",
        # dont leave me hanging
        "hey u 👀 {name} dont make me wait so much",
        "hey u 👀 dont leave me like this {name}",
        "{name} 👀 dont let me waiting here",
    ),
    VOICE_HIM: (
        # still there?
        "{name} you still there",
        "still with me {name} or what",
        "you there {name}",
        # what are you up to — NOT "i miss you"
        "what you up to {name}",
        "{name} what you doing over there",
        "hows your day going {name}",
        # went quiet / everything ok
        "{name} you went quiet.. all good",
        "quiet all of a sudden {name}.. everything ok",
        "you go quiet on me {name}.. all good over there",
        # still thinking about the chat — confident, not pining
        "been thinking about our chat 😏 you around {name}",
        "our chat stuck with me 😏 {name} you there",
        "still thinking about what you said 😏 {name}",
        # dont keep me waiting — his time, not his longing
        "{name} dont keep me waiting",
        "dont leave me hanging {name} 👀",
        "{name} 👀 im waiting",
    ),
}

# ⚠️ The ONE worked example in the selling prompt — the model is shown exactly one
# sentence of what a sell-caption sounds like, and writes every caption against it.
# Hers puts the creator's own body in the frame in the second person's imagination,
# which is the whole technique. Left unlaned it hands a male creator female anatomy
# to narrate as his own, in the single most load-bearing line of the engine that
# closes sales.
SELL_CAPTION_EXAMPLE = {
    VOICE_HER: "'here we go babe, legs spread apart, waiting for you'",
    VOICE_HIM: "'this is me after the gym, still dripping, thinking about you'",
}


# ── Self-description inside a prompt ─────────────────────────────────
# `deep_convo` states the creator's own nature twice — "you are 100% human,
# girly, warm" in the system turn and "a casual, GIRLY reply (do girl things…)"
# in the user turn. That is not a stray pronoun: it is an instruction to BE a
# woman, given twice, in the engine's two most load-bearing sentences. A male
# account running it produced a man told to do girl things.
#
# Kept as two separate constants because the two sentences do different jobs —
# one describes what he IS, the other what the reply should DO — and collapsing
# them was how the second one got missed on the first pass.
HUMAN_SELF = {
    VOICE_HER: "girly, warm",
    VOICE_HIM: "dry, direct, unbothered",
}
REPLY_REGISTER = {
    VOICE_HER: "GIRLY reply (do girl things and the occasional little typo)",
    VOICE_HIM: "reply in HIS register — blunt, unhurried, the occasional little typo",
}


# ── The bundle ───────────────────────────────────────────────────────

@dataclass(frozen=True)
class VoiceBlocks:
    """Every voice-varying prompt block, resolved for one account.

    Frozen and built once per run. Engines read fields off it rather than
    calling a wrapper per constant — see the module docstring on why that
    distinction is load-bearing across ~11 interpolation sites."""
    voice: str
    painful_texting: str
    live_proof: str
    off_deflections: tuple[str, ...]
    # The opt-in "text like a real person" block. Laned for two spans: the
    # pushback ATTITUDE (bratty inverts a dom) and the emoji VOCABULARY (unnamed,
    # the model reaches for the female sexting set).
    humanizer: str
    # The noun in "HOW YOU TEXT (a real 22yo girl, not an assistant)" — the style
    # header of both forked chat builders. One word, but it sits four lines above
    # the live-proof rule, so laning the blocks around it and leaving this would
    # hand the model a prompt that states the creator's gender twice, differently.
    # A contradicting prompt is worse than a consistently wrong one: the model
    # picks per generation and the account reads as female in some replies only.
    texter_noun: str
    # What to call a fan whose name we could not resolve — "babe" is hers.
    # NOT a rare path: `of_display_name` is empty for ~80% of fans, so on a male
    # account this is the DEFAULT address, not the exception.
    fan_address: str
    # Canned, model-free, fired the instant an unlock lands. See UNLOCK_REACTIONS.
    unlock_reactions: tuple[str, ...]
    # Fallback escalation line when the account's pack has no rung (~34% of prod).
    unlock_prompt: str
    aftercare: str
    # Canned re-engage pool, model-free. Hers types banned-for-him emoji.
    nudge_lines: tuple[str, ...]
    # The emoji set as a STANDALONE rule, for engines with no humanizer. "" for her.
    emoji_vocab: str
    # What the creator IS, and what a reply should BE — deep_convo states both.
    human_self: str
    reply_register: str
    # The one worked example the sell-caption prompt shows the model.
    sell_caption_example: str
    sell_customs: bool = False

    @property
    def is_male(self) -> bool:
        return self.voice == VOICE_HIM


def blocks(voice: object, sell_customs: bool = False) -> VoiceBlocks:
    """The bundle for one account (`voice` normalised here, so callers may pass
    raw input).

    `sell_customs` is a SECOND, INDEPENDENT axis — `_common.SELL_CUSTOMS_KEY`,
    default off. It rides on this bundle rather than getting its own because both
    resolve once per run and both land in the same prompt, but it is deliberately
    not derived from the voice: some female accounts sell voice notes and some
    male accounts will not."""
    v = norm_voice(voice)
    return VoiceBlocks(
        voice=v,
        painful_texting=_painful_texting(v),
        live_proof=_live_proof_guardrail(v, sell_customs),
        off_deflections=OFF_DEFLECTIONS[v],
        humanizer=_humanizer(v),
        texter_noun=_TEXTER_NOUN[v],
        fan_address=_FAN_ADDRESS[v],
        unlock_reactions=UNLOCK_REACTIONS[v],
        unlock_prompt=UNLOCK_PROMPT_FALLBACK[v],
        aftercare=AFTERCARE_FALLBACK[v],
        nudge_lines=NUDGE_LINES[v],
        emoji_vocab=EMOJI_VOCAB_RULE[v],
        human_self=HUMAN_SELF[v],
        reply_register=REPLY_REGISTER[v],
        sell_caption_example=SELL_CAPTION_EXAMPLE[v],
        sell_customs=bool(sell_customs),
    )


def for_fan(b: VoiceBlocks, fan) -> VoiceBlocks:
    """`b` narrowed to ONE fan: the account's bundle, with `sell_customs` forced
    off when this fan is not someone we offer a custom to (`_customs.may_offer`).

    It takes the FAN, not a precomputed boolean, so the spend policy is decided in
    one place. Handing each engine `for_fan(b, _customs.may_offer(f))` made three
    call sites responsible for remembering which predicate to pass, and a fourth
    engine could quietly pass a different one.

    WHY THIS IS A BUNDLE REBUILD AND NOT A BOOLEAN AT THE CALL SITE. `sell_customs`
    is not just a flag the engines read — `live_proof` is DERIVED from it, because
    the customs carve-out is spliced into the live-proof guardrail (that is where
    "the answer to a live call is still no, but you DO offer a custom" has to live
    to make sense). An engine that tested a fan-level boolean before rendering the
    sell surface, and then rendered `v.live_proof` from the account bundle, would
    tell the model in one paragraph that it has nothing to offer and in another
    that it may offer a custom. One call here moves both, and every other field is
    untouched.

    Cheap: pure construction off module constants, no I/O. Returns `b` itself when
    the account does not sell customs at all, so the common path allocates nothing
    and never even asks the question.
    """
    if not b.sell_customs or _customs.may_offer(fan):
        return b
    return blocks(b.voice, False)


# The default lane, built once at import. `_common` re-exports the fields off
# this so every existing importer of PAINFUL_TEXTING / LIVE_PROOF_GUARDRAIL
# keeps working untouched — the text moved, nothing else did.
HER = blocks(VOICE_HER)
