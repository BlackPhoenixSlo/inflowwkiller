"""service/brain_defaults.py — the default "Brain" (account_ai_config) a fresh
model starts with, so every account has a worked example to show instead of a
blank form.

These are generic FICTIONAL examples — persona, location/offset, time-of-day
activity lines, model and spend cap. The Automations → Brain panel surfaces them
via `GET /admin/account-config` (returned as `defaults`, resolved for the
account's `voice`); the editor seeds a blank account from them and the "Reset to
defaults" button refills from them. Nothing is written until the operator hits
Save, and each model is meant to be edited from here to match the real creator.

ONE PER LANE. `persona` is prepended to a system prompt that states the creator's
identity a few lines below it, and `time_activities` are read out as the
creator's own day by the prompt clock in every engine. There is no gender-free
version of "chilling at home with my cat" that is also a useful example — and a
male account seeded from the female brain does not read as a placeholder the
operator will notice, it reads as a woman's diary on a man's account.

IMAGES ARE EXCLUDED ON PURPOSE: `time_images` maps slots to vault media ids,
which are unique per account, so the default carries no images (empty map). The
Reset button likewise preserves whatever images the account already has.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from automations._voice import VOICE_HER, VOICE_HIM, norm_voice

# The default brain — same shape as account_config_api._serialize() output.
# Replace these with your own creator's details from the Brain panel.
BRAIN_DEFAULTS_HER: dict[str, Any] = {
    "persona": "You are Ava, a flirty and fun 23-year-old OnlyFans creator from Los Angeles, California. You're 168 cm of playful confidence with a warm, teasing charm. You love beach days, yoga, live music, weekend road trips, and your rescue cat.",
    # No `welcome_rules`: the welcome's SHAPE is fixed in code
    # (send_welcome._compose_system) so every account's riff matches the local
    # template. The column survives for the settings export only.
    "location": "Los Angeles, California",
    # Shape parity with _serialize(). Deliberately EMPTY: the canon is per-creator
    # identity, so a starter brain ships none rather than seeding every new account
    # with the example creator's biography. The 🪄 Enrich button fills it from the
    # persona the operator actually writes.
    "persona_facts": {},
    "timezone": None,
    "utc_offset": -8,
    "daily_cost_cap_cents": 100,
    "model": "deepseek-v4-flash",
    "model_by_purpose": {},
    "time_activities": {
        "morning_1": "just woke up and made myself a coffee",
        "morning_2": "out for a morning yoga session",
        "afternoon_1": "just got back from the beach",
        "afternoon_2": "chilling at home with my cat",
        "evening": "getting ready to head out",
        "night": "just got home and gonna shower"
    },
    "time_images": {}
}

# The male lane. Same shape, same keys, same non-identity values (model, cap,
# offset) — only the creator's half differs, which is the same rule `_voice.py`
# follows. The register is the lane's, not a translation of hers: terse, and the
# fan pursues.
BRAIN_DEFAULTS_HIM: dict[str, Any] = {
    "persona": "You are Ryan, a 32-year-old OnlyFans creator from the United States. 6'1\", 190 lbs, a fighter's build, and an unapologetically alpha frame — the coach who tells you the truth nobody else will. You train daily, you pick the men worth your time, and you share the gym, the road, and content that isn't anywhere else. You don't get subscribed to. You get applied to.",
    "location": "United States",
    # Empty for the same reason hers is — the canon is per-creator identity, and
    # 🪄 Enrich fills it from whatever persona the operator actually writes.
    "persona_facts": {},
    "timezone": None,
    "utc_offset": -8,
    "daily_cost_cap_cents": 100,
    "model": "deepseek-v4-flash",
    "model_by_purpose": {},
    # The prompt clock reads these out as his own day, so they carry the lane as
    # hard as the persona does. Same six slots, same everyday register — a day
    # this creator would actually have.
    "time_activities": {
        "morning_1": "just woke up, coffee first",
        "morning_2": "at the gym, mid session",
        "afternoon_1": "just got back from training",
        "afternoon_2": "at home, getting some work done",
        "evening": "cleaning up, heading out later",
        "night": "just got in, about to shower"
    },
    "time_images": {}
}

BRAIN_DEFAULTS_BY_VOICE: dict[str, dict[str, Any]] = {
    VOICE_HER: BRAIN_DEFAULTS_HER,
    VOICE_HIM: BRAIN_DEFAULTS_HIM,
}


def brain_defaults(voice: object = VOICE_HER) -> dict[str, Any]:
    """The starter brain for this account's lane.

    Two literals, one registry, one accessor — deliberately the same four-name shape
    as `script_packs` (PACK / PACK_HIM / PACKS_BY_VOICE / shipped_pack), because a
    reader who has learned one lane pair in this codebase has learned them all. There
    is deliberately NO bare `BRAIN_DEFAULTS` alias: it would have existed for a single
    `model` fallback, and a name meaning "the female one, but we're using it because
    this field doesn't vary" is the kind of shortcut that later reads as intent.

    A DEEP copy. The Brain panel's Reset writes over what it is handed, and
    `time_activities` is a nested dict — a shallow `dict()` would protect the ten
    top-level keys while handing out the module's own six activity lines BY
    REFERENCE, so one account's edit would rewrite the default every other account is
    then offered. Half-protection reads as protection, which is worse than none.

    Unknown/NULL/garbage → the female lane, same as `_voice.norm_voice` everywhere
    else, so a corrupt column can never flip an account's starter identity."""
    return deepcopy(BRAIN_DEFAULTS_BY_VOICE[norm_voice(voice)])
