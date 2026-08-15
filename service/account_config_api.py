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
from typing import Any, NamedTuple

from fastapi import APIRouter, Body, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

import audience_include
import llm_client
from auth import assert_account_owned
from automations import rhythm  # tz_offset_for — the ONE clock resolver
from automations._persona import PERSONA_FACT_FIELDS, PERSONA_FACTS_OPERATOR_ONLY
from brain_defaults import brain_defaults
from db.engine import get_session
from db.models import (AccountAiConfig, AudienceEnrollment, AutomationRule,
                       List as ListModel)
from geo_timezones import resolve_timezone_for_place
from llm_client import MODELS, LLMCapExceeded
from sqlalchemy import func as sa_func, select

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
        # Include-only automation audience. audience_of_list_id is resolved by
        # the handlers (it lives on the local mirror row, not this row).
        "audience_mode": audience_include.norm_audience_mode(
            getattr(row, "audience_mode", None) if row else None),
        "audience_list_id": getattr(row, "audience_list_id", None) if row else None,
        "audience_auto_add": bool(getattr(row, "audience_auto_add", None)) if row else False,
    }


async def _audience_status(account_id: str, cfg: dict[str, Any]) -> dict[str, Any]:
    """The Brain Audience section's status block: list identity, mirror health,
    ledger counts, the visible 'outside the fence' queue — every number with its
    denominator, so shadow week is readable before enforce is a click."""
    list_id = cfg.get("audience_list_id")
    out: dict[str, Any] = {
        "mode": cfg.get("audience_mode") or "off",
        "auto_add": bool(cfg.get("audience_auto_add")),
        "list_id": list_id,
        "of_list_id": None,
        "list_name": None,
        "member_count": 0,
        "synced_at": None,
        "snapshot_age_s": None,
        "truncated": False,
        "paused": False,
        "baseline_ready": False,
        "pending_first_sync": False,
        "healthy": True,
        "effective_mode": cfg.get("audience_mode") or "off",
        "ledger": {},
    }
    if out["mode"] == "off":
        return out
    pol = await audience_include.automation_audience(account_id)
    out.update(effective_mode=pol.mode, healthy=pol.healthy, paused=pol.paused,
               pending_first_sync=(pol.reason == "pending_first_sync"),
               reason=pol.reason, member_count=len(pol.member_ids),
               of_list_id=pol.of_list_id)
    async with get_session() as s:
        row = await s.get(ListModel, int(list_id)) if list_id is not None else None
        if row is not None and row.account_id == str(account_id):
            meta = audience_include._parse_sync_meta(row.sync_meta_json)
            out.update(list_name=row.name,
                       synced_at=row.synced_at.isoformat() if row.synced_at else None,
                       truncated=bool(meta.get("truncated")),
                       baseline_ready=bool(meta.get("baseline_ready")))
            if row.synced_at is not None:
                out["snapshot_age_s"] = max(
                    0, int((datetime.utcnow() - row.synced_at).total_seconds()))
        counts = (await s.execute(
            select(AudienceEnrollment.status, sa_func.count())
            .where(AudienceEnrollment.account_id == str(account_id))
            .group_by(AudienceEnrollment.status)
        )).all()
    out["ledger"] = {str(st): int(n) for st, n in counts}
    # The named, visible "outside the fence" queue (one-click admit).
    out["outside_queue"] = int(out["ledger"].get("rejected_not_new", 0))
    return out


@router.get("/admin/account-config")
async def get_account_config(account_id: str = Query(...)) -> dict[str, Any]:
    assert_account_owned(account_id)
    async with get_session() as s:
        row = await s.get(AccountAiConfig, account_id)
    cfg = _serialize(row)
    audience = await _audience_status(account_id, cfg)
    # The picker's stored selection rides the config so the whole-object PUT
    # round-trips it (BrainConfig must carry these or a Save drops them).
    cfg["audience_of_list_id"] = audience.get("of_list_id")
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
        # Include-only audience status (mirror health, ledger, outside queue).
        "audience": audience,
    }


class _ConfigBody(BaseModel):
    account_id: str
    config: dict


class _AudiencePut(NamedTuple):
    """The PUT's resolved audience branches: which keys were SENT (absent ==
    unchanged, exactly like `voice`), the validated values, and the resulting
    effective state used for the enable-flow decision."""
    mode_sent: bool
    mode: str | None           # validated, only meaningful when mode_sent
    list_sent: bool
    list_id: int | None        # FINAL local lists.id (prev when not sent)
    auto_sent: bool
    auto: bool | None
    prev_mode: str
    prev_list: int | None
    final_mode: str


async def _audience_put_branches(cfg: dict, account_id: str) -> _AudiencePut:
    """Validate the include-only audience keys of a brain PUT and find-or-create
    the local mirror row. The picker sends the OF folder id; the server owns the
    mirror (kind='automation_include'), and the stored column is the LOCAL
    lists.id. Shadow is mandatory before enforce; direct-to-enforce takes an
    explicit, logged override (the legal-fence case) — never a plain click."""
    mode_raw = cfg.get("audience_mode")
    mode_sent = "audience_mode" in cfg and mode_raw not in (None, "")
    mode: str | None = None
    if mode_sent:
        mode = str(mode_raw).strip().lower()
        if mode not in audience_include.AUDIENCE_MODES:
            raise HTTPException(
                422, f"audience_mode {mode_raw!r} is not one of off/shadow/enforce")
    list_sent = "audience_of_list_id" in cfg
    of_list_id: int | None = None
    if list_sent and cfg.get("audience_of_list_id") not in (None, "", 0):
        try:
            of_list_id = int(cfg.get("audience_of_list_id"))
        except (TypeError, ValueError):
            raise HTTPException(422, "audience_of_list_id must be an OF list id")
    auto_sent = "audience_auto_add" in cfg
    auto = bool(cfg.get("audience_auto_add")) if auto_sent else None

    async with get_session() as s:
        prev = await s.get(AccountAiConfig, account_id)
    prev_mode = audience_include.norm_audience_mode(
        getattr(prev, "audience_mode", None) if prev else None)
    prev_list = getattr(prev, "audience_list_id", None) if prev else None
    final_mode = mode if mode_sent else prev_mode

    if mode_sent and mode == "enforce" and prev_mode not in ("shadow", "enforce"):
        if not cfg.get("audience_enforce_override"):
            raise HTTPException(
                422, "audience_mode: shadow before enforce — run shadow first "
                     "(or send audience_enforce_override=true; it is logged)")
        log.warning("audience DIRECT-TO-ENFORCE override account=%s (no shadow week)",
                    account_id)

    list_id = prev_list
    if list_sent:
        if of_list_id is None:
            list_id = None
        else:
            name = _clean_text(cfg.get("audience_list_name"),
                               "audience_list_name") or f"OF list {of_list_id}"
            async with get_session() as s:
                mirror = (await s.execute(
                    select(ListModel).where(
                        ListModel.account_id == str(account_id),
                        ListModel.kind == audience_include.AUDIENCE_INCLUDE_KIND,
                        ListModel.of_list_id == of_list_id,
                    )
                )).scalars().first()
                if mirror is None:
                    mirror = ListModel(
                        account_id=str(account_id), of_list_id=of_list_id,
                        name=name, kind=audience_include.AUDIENCE_INCLUDE_KIND,
                        is_system=True)
                    s.add(mirror)
                    await s.flush()
                elif name and mirror.name != name:
                    mirror.name = name
                list_id = int(mirror.id)
    if final_mode != "off" and list_id is None:
        raise HTTPException(
            422, "audience_mode needs a folder — pick one (audience_of_list_id) "
                 "before leaving off")
    return _AudiencePut(mode_sent, mode, list_sent, list_id, auto_sent, auto,
                        prev_mode, prev_list, final_mode)


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
    aud = await _audience_put_branches(cfg, body.account_id)

    voice_raw = cfg.get("voice")
    voice = parse_voice(voice_raw)
    voice_sent = "voice" in cfg and voice_raw not in (None, "")
    if voice_raw not in (None, "") and voice is None:
        raise HTTPException(
            422, f"voice {voice_raw!r} is not supported (expected "
                 f"{VOICE_HER!r} or {VOICE_HIM!r})")
    if language == "en":
        language = None                      # NULL == en; keeps the column sparse

    # timezone — a LEGACY IANA zone (e.g. America/Vancouver). No longer settable
    # from the Brain, and only consulted when the account has no fixed
    # `utc_offset` (rhythm.tz_offset_for); blank clears it. Kept accepted so a
    # settings-transfer of an older export still round-trips.
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
    # Audience columns join the upsert only when their key was SENT (absent ==
    # unchanged — see the branches above). NULL == off keeps the columns sparse.
    if aud.mode_sent:
        vals["audience_mode"] = None if aud.mode == "off" else aud.mode
    if aud.list_sent:
        vals["audience_list_id"] = aud.list_id
    if aud.auto_sent:
        vals["audience_auto_add"] = True if aud.auto else None
    # nudge_config_json is intentionally absent → preserved on existing rows.
    set_ = {k: v for k, v in vals.items() if k != "account_id"}
    async with get_session() as s:
        await s.execute(
            sqlite_insert(AccountAiConfig)
            .values(**vals)
            .on_conflict_do_update(index_elements=["account_id"], set_=set_)
        )
        row = await s.get(AccountAiConfig, body.account_id)

    # Enable flow: turning the audience on (or repointing the folder) find-or-
    # creates the audience_sync rule and enqueues an IMMEDIATE first sync — the
    # resolver keeps enforce sitting in shadow until that sync lands.
    if aud.final_mode != "off" and aud.list_id is not None and (
            aud.prev_mode == "off"
            or (aud.list_sent and aud.list_id != aud.prev_list)):
        await _ensure_audience_sync_rule(body.account_id)

    log.info("account_config_saved account=%s model=%s slots=%d imgs=%d",
             body.account_id, model, len(acts), len(imgs))
    out_cfg = _serialize(row)
    audience = await _audience_status(body.account_id, out_cfg)
    out_cfg["audience_of_list_id"] = audience.get("of_list_id")
    return {"account_id": body.account_id, "config": out_cfg,
            "audience": audience}


async def _ensure_audience_sync_rule(account_id: str) -> None:
    """Find-or-create the enabled audience_sync rule (automation_rules has no
    unique (account, kind) — mirror nudge_config_api._ensure_rule) and enqueue
    an immediate first sync so the mirror exists within seconds of enabling."""
    now = datetime.utcnow()
    async with get_session() as s:
        rule = (await s.execute(
            select(AutomationRule).where(
                AutomationRule.account_id == str(account_id),
                AutomationRule.kind == "audience_sync",
            ).order_by(AutomationRule.id)
        )).scalars().first()
        if rule is None:
            s.add(AutomationRule(
                account_id=str(account_id), name="Audience folder sync",
                kind="audience_sync",
                trigger_json=json.dumps({"every_seconds": 900}),
                steps_json=json.dumps({}), is_enabled=True, created_at=now))
        elif not rule.is_enabled:
            rule.is_enabled = True
    from automation_executor import enqueue_job, wake_supervisor  # local: load cycle
    await enqueue_job(str(account_id), "audience_sync", payload={})
    wake_supervisor()


class _AudiencePauseBody(BaseModel):
    account_id: str
    paused: bool


@router.post("/admin/audience/pause")
async def audience_pause(body: _AudiencePauseBody = Body(...)) -> dict[str, Any]:
    """The kill switch: PAUSE audience-governed automation (gated engines hold,
    enforce-mode blasts halt) — it never silently unscopes to the full roster.
    Runtime state on the mirror row, not in a validated config blob."""
    assert_account_owned(body.account_id)
    async with get_session() as s:
        cfg = await s.get(AccountAiConfig, body.account_id)
        list_id = getattr(cfg, "audience_list_id", None) if cfg else None
        row = await s.get(ListModel, int(list_id)) if list_id is not None else None
        if row is None or row.account_id != str(body.account_id):
            raise HTTPException(404, "no audience folder configured for this account")
        meta = audience_include._parse_sync_meta(row.sync_meta_json)
        meta["paused"] = bool(body.paused)
        row.sync_meta_json = json.dumps(meta)
    log.warning("audience kill-switch account=%s paused=%s",
                body.account_id, body.paused)
    return {"account_id": body.account_id, "paused": bool(body.paused)}


class _AudienceBackfillBody(BaseModel):
    account_id: str
    confirm: bool = False


@router.post("/admin/audience/backfill")
async def audience_backfill(body: _AudienceBackfillBody = Body(...)) -> dict[str, Any]:
    """Operator decision O4: existing-fan backfill is a COUNT-PREVIEWED button,
    never automatic. confirm=false returns the count; confirm=true flips every
    `rejected_not_new` ledger row to pending_add (next audience_sync issues the
    OF adds with its bounded retry machinery). `removed_after_confirmed` rows
    are operator removals — FINAL, never backfilled."""
    assert_account_owned(body.account_id)
    from automations.audience_sync import ST_PENDING, ST_REJECTED
    async with get_session() as s:
        rows = (await s.execute(
            select(AudienceEnrollment).where(
                AudienceEnrollment.account_id == str(body.account_id),
                AudienceEnrollment.status == ST_REJECTED,
            )
        )).scalars().all()
        if not body.confirm:
            return {"account_id": body.account_id, "would_admit": len(rows),
                    "confirmed": False}
        now = datetime.utcnow()
        for r in rows:
            r.status = ST_PENDING
            r.reason = "operator_backfill"
            r.attempts = 0
            r.next_retry_at = now
            r.updated_at = now
    log.info("audience backfill account=%s admitted=%d (pending OF adds via sync)",
             body.account_id, len(rows))
    return {"account_id": body.account_id, "admitted": len(rows), "confirmed": True}


class _AudienceAdmitBody(BaseModel):
    account_id: str
    fan_id: int


@router.post("/admin/audience/admit")
async def audience_admit(body: _AudienceAdmitBody = Body(...)) -> dict[str, Any]:
    """One-click admit from the visible 'outside the fence' queue: OF add first
    (confirm-then-local — the next sync confirms membership and mirrors it),
    ledger moves to pending_add with an operator reason."""
    assert_account_owned(body.account_id)
    async with get_session() as s:
        cfg = await s.get(AccountAiConfig, body.account_id)
        list_id = getattr(cfg, "audience_list_id", None) if cfg else None
        row = await s.get(ListModel, int(list_id)) if list_id is not None else None
    if row is None or row.account_id != str(body.account_id) or not row.of_list_id:
        raise HTTPException(404, "no audience folder configured for this account")
    import asyncio as _asyncio

    import automation_executor as ax
    from automations.audience_sync import ST_PENDING, _upsert_ledger
    client = await _asyncio.to_thread(ax._make_client, body.account_id)
    try:
        await _asyncio.to_thread(
            client.add_user_to_list, int(row.of_list_id), int(body.fan_id))
    except Exception as e:  # noqa: BLE001 — pending row retries on the next sync
        log.warning("audience admit OF add failed account=%s fan=%s (%s)",
                    body.account_id, body.fan_id, str(e).splitlines()[0][:80])
    await _upsert_ledger(body.account_id, int(body.fan_id), status=ST_PENDING,
                         reason="operator_admit", attempts=1)
    return {"account_id": body.account_id, "fan_id": int(body.fan_id),
            "status": ST_PENDING}


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
        "plausible and commit to it — a home city, "
        f"{p.possessive} relationship status, whether {p.subject} has kids. "
        "Leaving these blank is the WORST outcome, "
        "not the safe one: the chat AI will then improvise a DIFFERENT answer "
        "every time a fan asks, and fans notice. A committed invention beats an "
        "improvised one.\n"
        "- Keep every value SHORT and concrete: a city name, one clause, a plain "
        "phrase.\n"
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
        # DERIVED, never retyped. This list used to be a literal spelled out three
        # lines below the two constants it restates — both imported at the top of
        # this file — so adding a fact meant editing two places and only one
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
    # The account stores ONE clock — a fixed `utc_offset` (see rhythm.tz_offset_for).
    # So the resolved zone is only a means to an end here: it names her place, and
    # what we hand back is that place's offset RIGHT NOW, which is what the Brain's
    # clock dropdown holds. This is the seam where place and time correlate: both
    # come from the same answer to "where is she". Whole hours only — the column is
    # an integer, so a half-hour zone (Kolkata) yields no proposal rather than a
    # silently rounded one.
    off_min = rhythm.tz_offset_for(tz, None) if tz else None
    utc_offset = int(off_min // 60) if off_min is not None and off_min % 60 == 0 else None

    log.info("persona_enrich account=%s proposed=%d tz=%s utc_offset=%s (was tz=%s off=%s)",
             body.account_id, len(proposed), tz, utc_offset, row.timezone, row.utc_offset)
    return {
        "account_id": body.account_id,
        "known": known,          # already locked in — shown greyed
        "proposed": proposed,    # only the newly filled slots
        "facts": merged,         # what Save would store
        "timezone": tz,          # None ⇒ ambiguous, the UI asks
        "timezone_changed": tz_changed,
        "current_timezone": row.timezone,
        # THE clock to write (None ⇒ her place is ambiguous / not a whole-hour zone,
        # and the operator picks it from the dropdown instead).
        "utc_offset": utc_offset,
        "utc_offset_changed": utc_offset is not None and utc_offset != (row.utc_offset or 0),
    }
