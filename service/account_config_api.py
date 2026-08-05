"""service/account_config_api.py — read/write the per-account "Brain"
(`account_ai_config`) for the Automations → Brain panel.

`account_ai_config` is the AI voice + caps a model speaks with: persona,
the 6 time-of-day activities + their per-slot vault images,
location/utc_offset (for the "it's <weekday> <tod> here" line), the daily spend
cap, and the LLM model (global + optional per-purpose override). gen_info /
send_welcome / send_followup / of_ai_chat all resolve it at run time, so this is
the one screen that fills the brain a fresh account starts without.

  GET /admin/account-config?account_id=  → {config, slots, model_options, purposes}
       `config` is the stored brain (defaults filled for the scalar caps); the
       rest is metadata so the editor can render dropdowns + the 6 image slots.
  PUT /admin/account-config              → upsert the brain, returns {config}

The nudge_online config lives on the SAME row (`nudge_config_json`) but is owned
by `nudge_config_api` — this upsert never touches it (it's absent from both the
insert values and the conflict `set_`, so an existing row keeps it).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

import llm_client
from auth import assert_account_owned
from automations._persona import PERSONA_FACT_FIELDS, PERSONA_FACTS_OPERATOR_ONLY
from brain_defaults import brain_defaults
from db.engine import get_session
from db.models import AccountAiConfig
from geo_timezones import resolve_timezone_for_place
from llm_client import MODELS, LLMCapExceeded

log = logging.getLogger("of-relay.account_config_api")

router = APIRouter()

# The 6 time-of-day slots, ordered — must match send_welcome._SLOT_KEYS.
TIME_SLOTS: tuple[str, ...] = (
    "morning_1", "morning_2", "afternoon_1", "afternoon_2", "evening", "night",
)
# Purposes that support a per-purpose model override (model_by_purpose).
PURPOSES: tuple[str, ...] = (
    "gen_info", "of_ai_chat", "send_welcome", "send_followup", "deep_convo",
)
# Languages the account can be set to. The code is the routing/guard key; the label is
# for the editor dropdown. Sourced from the language layer so there's one list.
from automations._language import KNOWN_LANGS, LANG_DISPLAY, norm_lang  # noqa: E402
from automations._voice import (  # noqa: E402
    VOICE_HER, VOICE_HIM, fact_labels, fact_placeholders, norm_voice, parse_voice,
    pronouns as _voice_pronouns)
LANGUAGES: tuple[dict, ...] = tuple(
    {"code": c, "label": LANG_DISPLAY.get(c, c)} for c in KNOWN_LANGS
)


# These three were lazily imported to keep "this light API module" from pulling
# the llm stack at startup. That saved nothing: the module-level
# `from automations._language import ...` below already pulls llm_client
# transitively, so the stack is loaded before any of them runs. The comments
# outlived the fact and cost a reader real time re-deriving it, so the imports are
# now where they belong.

def _model_options() -> list[str]:
    """The LLM model ids the account may pick."""
    return list(MODELS.keys())


def _persona_fact_fields() -> tuple[tuple[str, str], ...]:
    """The creator-canon field contract."""
    return PERSONA_FACT_FIELDS


def _persona_facts_operator_only() -> frozenset[str]:
    """Slots the enrich pass may not propose — what she will and won't do on
    camera is a business decision, not something to infer."""
    return PERSONA_FACTS_OPERATOR_ONLY


def _persona_fact_meta(voice: object) -> list[dict[str, Any]]:
    """The creator-canon fields as editor metadata: {key, label, operator_only,
    placeholder}. Single source of truth — the BrainPanel renders entirely from
    this. The placeholder rides along because a separate 26-key map in TypeScript
    is a second enumeration to forget.

    LANED. `_voice.fact_labels` exists precisely to override the gendered labels
    ("Why she started OF"), and it had exactly one caller — the PROMPT. So the model
    was told "why HE started OF" while the operator filling the box read "why SHE
    started OF", on the same field, on the same account. Labels are what the operator
    types AGAINST; a female label is an instruction to write female canon into a male
    account's prompt."""
    operator_only = _persona_facts_operator_only()
    # Both lane columns come from `_voice`, which owns text that reads differently
    # per lane. Placeholders are indexed, not `.get`-with-a-default: the table is
    # total for every declared slot (`_voice.assert_canon_parity`, at import), so
    # there is no "this lane has no answer" case for the loop to invent one for —
    # and no way to spell "fall through to the other lane", which is how her copy
    # reached his boxes twice already. Labels ARE `.get`, because 21 of the 26
    # declared ones name the field rather than the creator; the gendered five are
    # bound by the same import-time assert.
    labels, placeholders = fact_labels(voice), fact_placeholders(voice)
    return [{"key": k, "label": labels.get(k, label),
             "operator_only": k in operator_only, "placeholder": placeholders[k]}
            for k, label in _persona_fact_fields()]


def _parse_obj(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        v = json.loads(raw)
        return v if isinstance(v, dict) else {}
    except (TypeError, ValueError):
        return {}


def _clean_text(v: Any, field: str) -> str | None:
    """A trimmed string, or None when blank (clears the column)."""
    if v is None:
        return None
    if not isinstance(v, str):
        raise HTTPException(422, f"{field} must be text")
    s = v.strip()
    return s or None


def _validate_model(v: Any, field: str, allowed: set[str]) -> str | None:
    s = _clean_text(v, field)
    if s is None:
        return None
    if allowed and s not in allowed:
        raise HTTPException(422, f"{field} {s!r} is not a known model ({', '.join(sorted(allowed))})")
    return s


def _serialize(row: AccountAiConfig | None) -> dict[str, Any]:
    """The stored brain → a flat editor-friendly dict (scalar caps defaulted)."""
    return {
        "persona": row.persona if row else None,
        "location": row.location if row else None,
        "persona_facts": (_parse_obj(row.persona_facts_json) if row else {}),
        "language": (norm_lang(row.language) or "en") if row else "en",
        "voice": (norm_voice(row.voice) if row else VOICE_HER),
        "timezone": row.timezone if row else None,
        "utc_offset": row.utc_offset if row else 0,
        "daily_cost_cap_cents": row.daily_cost_cap_cents if row else 100,
        "model": row.model if row else None,
        "model_by_purpose": _parse_obj(row.model_by_purpose) if row else {},
        "time_activities": _parse_obj(row.time_activities_json) if row else {},
        "time_images": _parse_obj(row.time_images_json) if row else {},
    }


@router.get("/admin/account-config")
async def get_account_config(account_id: str = Query(...)) -> dict[str, Any]:
    assert_account_owned(account_id)
    async with get_session() as s:
        row = await s.get(AccountAiConfig, account_id)
    cfg = _serialize(row)
    # Built once per lane, and the SINGULAR fields below are indexed out of these
    # rather than recomputed. Two calls that "obviously" agree are still two
    # answers to one question: nothing forced `defaults == defaults_by_voice[voice]`,
    # and it was that possible disagreement the client then had to defend against.
    # Indexing makes the agreement structural and drops three `deepcopy`s and two
    # 26-row builds per request on the way.
    brains = {v: brain_defaults(v) for v in (VOICE_HER, VOICE_HIM)}
    canon = {v: _persona_fact_meta(v) for v in (VOICE_HER, VOICE_HIM)}
    return {
        "account_id": account_id,
        "config": cfg,
        # The starter brain (no images), RESOLVED for this account's lane. The
        # editor seeds a blank account from this and the "Reset to defaults"
        # button refills from it — so every model has a worked example to show,
        # not an empty form.
        #
        # It served the Ava capture unconditionally, three lines under a
        # `_serialize` that already reports the voice correctly. Reset is the
        # sharp end: BrainPanel's `defaultsWithImages` preserves `voice` across a
        # refill but copies the persona and the six time-of-day lines wholesale,
        # so one click turned a male account into a 22-year-old woman with the
        # right value still in the voice dropdown. 2024813 was seeded that way
        # and ran her yoga/beach/cat lines under a red-pill-dom persona.
        "defaults": brains[cfg["voice"]],
        # Both lanes, because "Reset to defaults" must follow the CREATOR DROPDOWN,
        # not the stored column. The dropdown writes local form state; the column
        # only moves on Save. So switching to Male and pressing Reset — the exact
        # flow the male starter brain was built for — refilled with Ava, and the
        # panel had no way to know better: the lane had already been resolved away
        # server-side. The singular above stays for the readers that have no
        # dropdown (the blank-account seed, the chat drawer's canon list), which
        # genuinely want the stored lane.
        "defaults_by_voice": brains,
        "slots": list(TIME_SLOTS),
        "model_options": _model_options(),
        "purposes": list(PURPOSES),
        "languages": list(LANGUAGES),
        # The creator-canon field contract, served the same way as slots /
        # purposes / languages above. The editor renders straight off this, so
        # the 26-field list has ONE home — a duplicated copy in the UI would
        # drift silently and give an operator a box that never reaches a fan.
        "persona_fact_fields": canon[cfg["voice"]],
        # Per lane, for the same reason `defaults_by_voice` is: the Creator dropdown
        # is local form state until Save, so a panel that only ever saw the STORED
        # lane relabels "Why she started OF" one Save too late — after the operator
        # has already filled the box under the wrong prompt.
        "persona_fact_fields_by_voice": canon,
    }


class _ConfigBody(BaseModel):
    account_id: str
    config: dict


@router.put("/admin/account-config")
async def put_account_config(body: _ConfigBody = Body(...)) -> dict[str, Any]:
    assert_account_owned(body.account_id)
    cfg = body.config
    if not isinstance(cfg, dict):
        raise HTTPException(422, "config must be an object")
    allowed = set(_model_options())

    persona = _clean_text(cfg.get("persona"), "persona")
    location = _clean_text(cfg.get("location"), "location")
    model = _validate_model(cfg.get("model"), "model", allowed)

    # persona_facts — the structured creator canon pinned into every chat prompt.
    # An ALLOWLIST, not a passthrough: unknown keys are dropped rather than stored,
    # because nothing downstream would ever render them and a silently-kept key
    # reads to an operator as a fact that is in play when it is not. Values are
    # coerced to trimmed strings (a list joins to "a, b") and clipped; empty
    # values are dropped so an empty dict stores NULL == "never enriched".
    facts_raw = cfg.get("persona_facts") or {}
    if not isinstance(facts_raw, dict):
        raise HTTPException(422, "persona_facts must be an object")
    facts: dict[str, str] = {}
    for key, _label in _persona_fact_fields():
        val = facts_raw.get(key)
        if isinstance(val, (list, tuple)):
            val = ", ".join(str(v).strip() for v in val if str(v).strip())
        elif isinstance(val, bool) or val is None:
            val = "" if val is None else str(val)
        val = str(val).strip()
        if val:
            facts[key] = val[:240]

    # language: an ISO 639-1 code from the known set; anything else (incl. "en" or
    # unset) stores NULL so the code default ("en") applies.
    lang_raw = cfg.get("language")
    language = norm_lang(lang_raw)
    if lang_raw and not language:
        raise HTTPException(422, f"language {lang_raw!r} is not a supported code")
    language = language or None
    # voice: whose voice this account writes in. An unknown value is a 422 rather
    # than a silent fallback — a typo'd 'hom' resolving to "her" is exactly the
    # silent-wrong-gender failure the lane exists to prevent — which is why this
    # uses `parse_voice` (None == unrecognised) and not `norm_voice` (which
    # collapses everything to "her", the right rule for reads and the wrong one
    # here).
    #
    # ⚠️ An ABSENT key means UNCHANGED, not "reset to the default lane". This
    # endpoint upserts, so a key missing from `vals` is preserved on an existing
    # row — the same treatment `nudge_config_json` gets below.
    #
    # The Brain panel now has a Creator dropdown and always sends this key, so the
    # rule is belt-and-braces for that caller. It still matters for the others:
    # The starter brains carry no `voice`, so any client rebuilding a config object
    # from the defaults posts without it, and a settings-transfer import of a
    # pre-lane backup would otherwise clear a male account back to female.
    #
    # "" IS TREATED THE SAME AS ABSENT, on purpose. A blank string is what an empty
    # form field posts, not a considered choice, and the only value it could
    # possibly mean here is the default lane — which is the one outcome that must
    # never happen by accident. Clearing a male account back to female stays
    # possible and stays EXPLICIT: post the literal "her".
    voice_raw = cfg.get("voice")
    voice = parse_voice(voice_raw)
    voice_sent = "voice" in cfg and voice_raw not in (None, "")
    if voice_raw not in (None, "") and voice is None:
        raise HTTPException(
            422, f"voice {voice_raw!r} is not supported (expected "
                 f"{VOICE_HER!r} or {VOICE_HIM!r})")
    if language == "en":
        language = None                      # NULL == en; keeps the column sparse

    # timezone — an IANA zone (e.g. America/Vancouver). Wins over utc_offset in
    # rhythm.tz_offset_for (DST-correct); blank clears the column so the legacy
    # offset (or "no clock") applies again.
    tz = _clean_text(cfg.get("timezone"), "timezone")
    if tz is not None:
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo(tz)
        except Exception:
            raise HTTPException(
                422, f"timezone {tz!r} is not a known IANA zone (e.g. America/Vancouver)")

    # utc_offset — whole hours (e.g. Vancouver −6); sane range, default 0.
    utc_raw = cfg.get("utc_offset", 0)
    if isinstance(utc_raw, bool) or not isinstance(utc_raw, (int, float)):
        raise HTTPException(422, "utc_offset must be a whole number of hours")
    utc_offset = int(utc_raw)
    if not (-24 <= utc_offset <= 24):
        raise HTTPException(422, "utc_offset must be between -24 and 24 hours")

    cap_raw = cfg.get("daily_cost_cap_cents", 100)
    if isinstance(cap_raw, bool) or not isinstance(cap_raw, (int, float)) or cap_raw < 0:
        raise HTTPException(422, "daily_cost_cap_cents must be a non-negative number")
    cap = int(cap_raw)

    # model_by_purpose: {purpose: model}; each model validated against MODELS.
    mbp_in = cfg.get("model_by_purpose") or {}
    if not isinstance(mbp_in, dict):
        raise HTTPException(422, "model_by_purpose must be an object")
    mbp: dict[str, str] = {}
    for purpose, mv in mbp_in.items():
        cleaned = _validate_model(mv, f"model_by_purpose.{purpose}", allowed)
        if cleaned:
            mbp[str(purpose)] = cleaned

    # time_activities: keep only the 6 known slots, each a non-empty string.
    acts_in = cfg.get("time_activities") or {}
    if not isinstance(acts_in, dict):
        raise HTTPException(422, "time_activities must be an object")
    acts: dict[str, str] = {}
    for slot in TIME_SLOTS:
        s = _clean_text(acts_in.get(slot), f"time_activities.{slot}")
        if s:
            acts[slot] = s

    # time_images: keep only the 6 known slots, each a media-id int.
    imgs_in = cfg.get("time_images") or {}
    if not isinstance(imgs_in, dict):
        raise HTTPException(422, "time_images must be an object")
    imgs: dict[str, int] = {}
    for slot in TIME_SLOTS:
        v = imgs_in.get(slot)
        if v in (None, "", 0):
            continue
        if isinstance(v, bool) or not isinstance(v, (int, float, str)):
            raise HTTPException(422, f"time_images.{slot} must be a media id")
        try:
            imgs[slot] = int(v)
        except (TypeError, ValueError):
            raise HTTPException(422, f"time_images.{slot} must be a media id")

    now = datetime.utcnow()
    # Empty JSON maps store as NULL ("unset") so the senders' `if col:` checks
    # fall back to their legacy pickers instead of an empty-dict no-op.
    vals = {
        "account_id": body.account_id,
        "persona": persona,
        "location": location,
        "persona_facts_json": json.dumps(facts) if facts else None,
        "language": language,
        "timezone": tz,
        "utc_offset": utc_offset,
        "daily_cost_cap_cents": cap,
        "model": model,
        "model_by_purpose": json.dumps(mbp) if mbp else None,
        "time_activities_json": json.dumps(acts) if acts else None,
        "time_images_json": json.dumps(imgs) if imgs else None,
        "updated_at": now,
    }
    # `voice` only joins the upsert when the caller sent a REAL value, so neither a
    # missing key nor a blank field can clear the lane while saving something else.
    # NULL == "her" keeps the column sparse.
    if voice_sent:
        vals["voice"] = voice if voice == VOICE_HIM else None
    # nudge_config_json is intentionally absent → preserved on existing rows.
    set_ = {k: v for k, v in vals.items() if k != "account_id"}
    async with get_session() as s:
        await s.execute(
            sqlite_insert(AccountAiConfig)
            .values(**vals)
            .on_conflict_do_update(index_elements=["account_id"], set_=set_)
        )
        row = await s.get(AccountAiConfig, body.account_id)
    log.info("account_config_saved account=%s model=%s slots=%d imgs=%d",
             body.account_id, model, len(acts), len(imgs))
    return {"account_id": body.account_id, "config": _serialize(row)}


# ── 🪄 Enrich: fill the creator canon from what we already know ────────
# The gaps in a free-text persona are exactly what fans probe. the graded vault's said
# "Born and raised in Argentina" with NO city, so a fan asking where she grew up
# got an improvisation, and a 966-turn thread walked Argentina → Chile → Córdoba
# before he stopped believing her. This proposes values for the empty slots.
#
# TWO SOURCES, deliberately split:
#   • the LLM writes only the NARRATIVE fields (city, upbringing, living
#     situation, family, pets) — persona colour, which is what it is good at.
#   • the TIMEZONE is resolved IN CODE from the resolved city/country
#     (geo_timezones.resolve_timezone_for_place). Six accounts were 1-4h wrong and
#     the prompt clock then hard-instructs the model to DEFEND the wrong hour;
#     letting a model guess the zone is how that happens. Ambiguous ⇒ None ⇒ the
#     UI asks instead of guessing.
#
# It PROPOSES only — never writes. The operator reviews and saves through the
# normal POST, so nothing reaches a fan-facing prompt unread.
# The enrichable fact keys, in declaration order, minus the ones only an operator
# may set. Derived from the same constants the validator and the UI read, so the
# prompt cannot ask the model for a key the writer would drop.
_ENRICH_KEYS = ", ".join(
    k for k, _ in PERSONA_FACT_FIELDS if k not in PERSONA_FACTS_OPERATOR_ONLY
)

def _enrich_system(voice: object) -> str:
    """The 🪄 Enrich system prompt, LANED.

    ⚠️ This is the male lane's ONLY canon-authoring path — `brain_defaults`
    deliberately ships `persona_facts: {}` on both starter brains because Enrich
    is what fills them — and its output lands, after one operator Save, inside
    `_persona`'s "THESE ARE THE FACTS ABOUT YOU … you must never say anything
    that contradicts them". It shipped hardcoded female through eleven review
    rounds: the labels beside the boxes said "Why he started OF" while the model
    filling them was told nine times that its subject was a woman. A model handed
    a male persona in the user turn and a female subject as a system RULE takes
    the rule — the same argument `_voice.py`'s header opens with.

    `voice` is REQUIRED, deliberately. A default would read as "caller didn't
    say ⇒ female", which is how this got here; with no default the interpreter is
    the guard and a new call site cannot forget."""
    p = _voice_pronouns(voice)
    return (
        "You fill in the background profile of an OnlyFans creator persona so "
        f"{p.possessive} chat AI never has to improvise an answer about "
        f"{p.reflexive} and contradict it later. You are given {p.possessive} "
        "existing persona text and any facts already confirmed. Respond with a "
        "SINGLE JSON object and nothing else.\n"
        "RULES:\n"
        "- Anything the persona already states, COPY EXACTLY. Never change a "
        "stated fact — those are locked.\n"
        "- Fill only what is MISSING, and stay strictly consistent with what is "
        "stated (a persona that says Argentina gets an Argentine city, never a "
        "Chilean one).\n"
        "- You are AUTHORING a persona, not extracting facts about a real "
        "person. So where the persona is silent, INVENT something ordinary and "
        "plausible and commit to it — a home city, who "
        f"{p.subject} lives with, whether {p.subject} has kids, a sentence about "
        f"{p.possessive} childhood. Leaving these blank is the WORST outcome, "
        "not the safe one: the chat AI will then improvise a DIFFERENT answer "
        "every time a fan asks, and fans notice. A committed invention beats an "
        "improvised one.\n"
        "- Keep every value SHORT and concrete: a city name, one clause, a plain "
        "phrase. `upbringing` is at most one sentence.\n"
        "- Prefer a real, ordinary, plausible place a real person would be from. "
        "No celebrities, no landmarks, nothing exotic or newsworthy.\n"
        "- Use \"\" ONLY when a value would risk contradicting something already "
        "stated. Never leave a slot empty merely because the persona did not "
        "spell it out — that is the slot you are here to fill.\n"
        "- Do NOT output a timezone, an offset, or a time — those are computed.\n"
        "- NEVER output `tattoos` at all — not a description and not \"none\". "
        "Body art is visible in "
        f"{p.possessive} photos, so both an invented tattoo and a wrongly denied "
        "one are things a fan can see are false.\n"
        "- `birthday` must agree with `age`. `height` in both cm and feet if "
        "stated.\n"
        "- These are also rapport material, not just a consistency check: "
        "`music`, `travel`, `dreams`, `her_type` and `school` are what "
        f"{p.subject} RELATES to a fan with, so make them specific enough to "
        "start a conversation ('grunge, mostly Alice in Chains' beats 'rock').\n"
        # DERIVED, never retyped. This list used to be a literal spelled out three
        # lines below the two constants it restates — both imported at the top of
        # this file — so adding a 27th fact meant editing two places and only one
        # of them was enforced anywhere. They had not drifted yet; that is the
        # only reason this is a cheap fix rather than a bug hunt.
        f"Keys: {_ENRICH_KEYS}.\n"
        "NEVER output `kinks`, `limits` or `tattoos` — the first two are the "
        "creator's own business decision, and the third is visible in "
        f"{p.possessive} photos."
    )


class EnrichBody(BaseModel):
    account_id: str
    # Optional operator steer, e.g. "from Rosario, lives with a roommate".
    hint: str | None = None


@router.post("/admin/account-config/enrich")
async def enrich_account_config(body: EnrichBody = Body(...)) -> dict[str, Any]:
    """Propose values for the empty creator-canon slots. Read-only: returns a
    proposal for the operator to review, edit and save. Never writes."""
    assert_account_owned(body.account_id)
    async with get_session() as s:
        row = await s.get(AccountAiConfig, body.account_id)
    if row is None:
        raise HTTPException(404, f"no brain configured for {body.account_id!r}")

    persona = (row.persona or "").strip()
    location = (row.location or "").strip()
    known = _parse_obj(row.persona_facts_json)
    if not persona and not location and not known:
        raise HTTPException(
            422, "nothing to enrich from — write a persona (or set a location) first")

    payload = {
        "persona": persona,
        "location": location,
        "already_confirmed": known,
        "operator_hint": (body.hint or "").strip(),
    }
    voice = norm_voice(row.voice)
    # `model` does not vary by lane (the test pins it equal), but passing the row's
    # voice anyway keeps this off the list of calls that take the shipped lane by
    # DEFAULT — the distinction the guard exists to make is "chose it" vs "didn't say".
    model = row.model or brain_defaults(voice).get("model") or "deepseek-v4-flash"
    try:
        res = await llm_client.chat(
            model=model,
            messages=[{"role": "system", "content": _enrich_system(voice)},
                      {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
            purpose="enrich_persona",
            account_id=body.account_id,
            response_format={"type": "json_object"},
            temperature=0.4,
        )
        proposed_raw = json.loads(res.content or "{}")
        if not isinstance(proposed_raw, dict):
            proposed_raw = {}
    except LLMCapExceeded:
        raise HTTPException(429, "daily LLM cost cap reached for this account")
    except HTTPException:
        raise
    except Exception:
        log.exception("persona enrich failed account=%s", body.account_id)
        raise HTTPException(502, "the enrich model did not return a usable profile")

    # Same allowlist + coercion the save path uses — the model does not get to
    # invent field names, and a stated fact is never overwritten by a proposal.
    proposed: dict[str, str] = {}
    operator_only = _persona_facts_operator_only()
    for key, _label in _persona_fact_fields():
        if key in operator_only:
            continue        # the model does not get to author these — see the note
        val = proposed_raw.get(key)
        if isinstance(val, (list, tuple)):
            val = ", ".join(str(v).strip() for v in val if str(v).strip())
        val = "" if val is None else str(val).strip()
        if val and not known.get(key):
            proposed[key] = val[:240]

    merged = {**known, **proposed}

    # The timezone is CODE, not model output — see the note above.
    tz = resolve_timezone_for_place(
        city=merged.get("home_city"),
        country=merged.get("home_country"),
        free_text=location or merged.get("born_city") or merged.get("born_country"),
    )
    tz_changed = bool(tz) and tz != (row.timezone or "")

    log.info("persona_enrich account=%s proposed=%d tz=%s (was %s)",
             body.account_id, len(proposed), tz, row.timezone)
    return {
        "account_id": body.account_id,
        "known": known,          # already locked in — shown greyed
        "proposed": proposed,    # only the newly filled slots
        "facts": merged,         # what Save would store
        "timezone": tz,          # None ⇒ ambiguous, the UI asks
        "timezone_changed": tz_changed,
        "current_timezone": row.timezone,
    }
