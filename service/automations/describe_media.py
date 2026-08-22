"""service/automations/describe_media.py — Vault-AI S5: describe cadence sweep.

Every-N-hour self-registering sweep that fills the vault_items describe layer
via the EXISTING describe path (`vault_ai_api._describe_one` — Qwen3-VL vision
on the mirror), then chases each fresh description with a 2nd DeepSeek TEXT call
that writes `suggested_caption` + `suggested_script` (feat 8 — nothing writes
these today).

Contract: plans/VAULT_AI_ACTIONS_CONTRACT.md §1 (`vault_ai_config_json`) and §3
(`effective_value` / `is_locked`). Descriptions AUTO-APPLY (never emit review
items — first run would drop 500+ review cards, worse than manual). Sends
NOTHING: no fan-lease, no fan cooldown, no spend lock. Bulk-laned (see
`automation_executor._BULK_KINDS`) so a long sweep can't consume a sender's run
slot.

Cadence: `describe.cadence_hours` (default 6) drives one `automation_rules` row
(kind="describe_media", `trigger_json={every_seconds: cadence_hours*3600}`); the
executor's generic materialiser turns it into jobs each cycle. No executor edit
beyond the one-line `_BULK_KINDS` membership.

Bounds per run: `describe.max_items_per_run` AND a millicent budget derived from
`describe.describe_all_cap_percent` × the account's daily LLM cap. The real
per-request cap gate lives inside `llm_client._reserve`; this budget is the
sweep's OWN soft ceiling so a single run can't monopolise the day's cap.

Dedupe: by stable `media_id`. Items with `describe_status == "described"` are
NOT re-described. Items already described but missing `suggested_caption` /
`suggested_script` (and unlocked) get just the copy call. `force_ids` bypasses
dedupe. Locked fields (`locked_fields_json`) are ALWAYS respected on write —
`_describe_one` already honours them for describe fields; `_write_copy` does the
same for caption/script.

Models come from `vault_ai_config.models`: describe from `models.describe`
(default `qwen3-vl-30b`); copy from `models.caption` (default `deepseek-v4-flash`),
each coerced to a valid `llm_client.MODELS` key at runtime so a mistyped
bake-off row can't wedge the sweep. One text call per item emits BOTH caption +
script (single JSON body); later bake-off wiring can split per-field if that
ever pays.

Config master `enabled=false` (default) ⇒ no-op — the automation is safe to
schedule with an accountaiconfig row that hasn't opted in.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Any

from sqlalchemy import or_, select, update

from automation_registry import register
from db.engine import get_session
from db.models import AccountAiConfig, VaultItem
import llm_client
import vault_ai_brief
import vault_ai_config
from llm_client import LLMCapExceeded, LLMError
from vault_ai_api import _describe_one as _vault_describe_one
from vault_ai_api import _DRM_RETRY_AFTER  # shared DRM re-attempt cooldown
from vault_ai_effective import is_locked

from ._common import load_voice_blocks

log = logging.getLogger("of-relay.automation.describe_media")

_PURPOSE_COPY = "describe_media_copy"

# Defaults + merge come from `vault_ai_config` (the contract's one home) — this
# module used to keep a partial hand-copy, which meant the API's effective view
# and the sweep's were two blobs that only happened to agree. Master
# `enabled=false` there, so a freshly-scheduled account never describes until an
# operator opts in.

# Kinds the sweep can process (`vault_ai_api._upsert_from_media` writes these).
# Photos + gifs go through the image branch of `_describe_one`; videos go
# through the storyboard/poster-frame branch. Config toggles group them so an
# operator can turn either family off without touching per-kind lists.
_IMAGE_KINDS = ("photo", "gif")
_VIDEO_KINDS = ("video",)

# The one adjective that tells the model HOW the creator sounds selling his or her
# own body. "sultry" is a word for a woman — a man writing sultry copy about
# himself reads as someone impersonating one — and it is the only gendered span in
# this prompt, so the rest is shared verbatim.
_COPY_TONE = {"her": "sultry", "him": "blunt and filthy"}


def _copy_system_prompt(voice: str = "her") -> str:
    tone = _COPY_TONE["him" if str(voice or "").strip().lower() == "him" else "her"]
    return (
    "You write first-person sales copy for an OnlyFans creator's PPV send, from "
    "the recorded facts about one piece of vault media:\n"
    "  caption — 1 teasing line, no hashtags/@, <= 140 chars.\n"
    f"  script  — 2-4 short sentences, {tone}, specific to what is shown, "
    "<= 500 chars.\n"
    'Respond with STRICT JSON only: {"caption": "...", "script": "..."}.'
    )


# Back-compat: the shipped female prompt, byte-identical.
_COPY_SYSTEM_PROMPT = _copy_system_prompt("her")

_JSON_OBJ_RE = re.compile(r"\{.*\}", re.DOTALL)


# ── Config ──────────────────────────────────────────────────────────

async def _load_effective_config(account_id: str) -> dict:
    """The account's effective vault-ai config. Missing / unparseable JSON ⇒
    pure defaults (master `enabled=false`) — `vault_ai_config.effective` owns
    that degradation, so a bad blob can never wedge the sweep."""
    async with get_session() as s:
        raw = await s.scalar(
            select(AccountAiConfig.vault_ai_config_json).where(
                AccountAiConfig.account_id == account_id
            )
        )
    stored = vault_ai_config.parse_stored(raw)
    if raw and stored is None:
        log.warning("vault_ai_config_json_bad_json account=%s", account_id)
    return vault_ai_config.effective(stored)


def _resolve_model(name: Any, *, fallback: str) -> str:
    """Coerce a config-declared model to a valid `llm_client.MODELS` key. The
    contract speaks internal ids, so this is only a typo guard — a mistyped
    bake-off row must NEVER wedge the whole sweep with an LLMConfigError."""
    return str(name) if str(name or "") in llm_client.MODELS else fallback


async def _budget_millicents(account_id: str, cap_percent: int) -> int:
    """Millicents this run may spend before backing off ("describe-all reserve
    X% of today's LLM cap" from the plan). 0 disables the soft ceiling — the
    hard cap inside `llm_client._reserve` still applies per-call."""
    async with get_session() as s:
        cap_cents = await s.scalar(
            select(AccountAiConfig.daily_cost_cap_cents).where(
                AccountAiConfig.account_id == account_id
            )
        )
    cap_cents = int(cap_cents if cap_cents is not None else llm_client._DEFAULT_CAP_CENTS)
    pct = max(0, min(100, int(cap_percent)))
    # cents × percent = (cents × percent/100) × 100 millicents/cent → cents × percent.
    return cap_cents * pct


# ── Candidate selection ─────────────────────────────────────────────

def _enabled_kinds(images: bool, videos: bool) -> list[str]:
    kinds: list[str] = []
    if images:
        kinds.extend(_IMAGE_KINDS)
    if videos:
        kinds.extend(_VIDEO_KINDS)
    return kinds


async def _describe_candidates(account_id: str, kinds: list[str], limit: int) -> list[int]:
    """media_ids that haven't yet been successfully described. NEWEST-first —
    `created_at` is OF's own `createdAt`, i.e. when the media was actually shot
    and uploaded, not when we mirrored it.

    This is the drip that matters most, because it is the one with a `limit`:
    on a vault whose backlog is larger than a cadence can clear, whatever sorts
    first is what ever gets described at all. Oldest-first spent that budget on
    2018 uploads while the shoot from this week — the media a chatter is
    actually being asked for — sat undescribed and therefore invisible to every
    gated lane. Recency is the best available proxy for what will sell next.

    `media_id` breaks ties: OF hands whole uploads the same `createdAt`, and
    without a tiebreak the order inside a batch is SQLite's to choose, which
    makes a resumed drip non-deterministic about what it already covered.

    `blocked_drm` is NOT terminal (poster-frame fallback describes it) so
    anything != "described" is a retry candidate — matches the on-demand
    `Describe all` behaviour."""
    if not kinds or limit <= 0:
        return []
    async with get_session() as s:
        stmt = (
            select(VaultItem.media_id)
            .where(VaultItem.account_id == account_id)
            .where(VaultItem.removed_at.is_(None))
            .where(VaultItem.kind.in_(kinds))
            .where(
                (VaultItem.describe_status.is_(None))
                | (VaultItem.describe_status != "described")
            )
            # blocked_drm is a retry candidate (poster-frame fallback may now
            # work) but not on EVERY 6h sweep — a truly un-renderable clip
            # re-settles to blocked_drm each pass and costs an OF poster-fetch for
            # nothing (cost-audit finding). Cool a freshly-blocked DRM item off for
            # _DRM_RETRY_AFTER; a manual force/restage still revisits it.
            .where(
                or_(
                    VaultItem.describe_status.is_(None),
                    VaultItem.describe_status != "blocked_drm",
                    VaultItem.describe_generated_at.is_(None),
                    VaultItem.describe_generated_at
                    < datetime.utcnow() - _DRM_RETRY_AFTER,
                )
            )
            .order_by(VaultItem.created_at.desc(), VaultItem.media_id.desc())
            .limit(limit)
        )
        rows = (await s.execute(stmt)).all()
    return [int(r[0]) for r in rows]


async def _copy_candidates(account_id: str, kinds: list[str], limit: int) -> list[int]:
    """Already-described items whose caption OR script is still empty AND not
    locked — the feat-8 fill-in-what-the-substrate-never-wrote pass.

    Newest-first for the same reason as `_describe_candidates`: this pass runs
    on the same cadence and would otherwise write copy for the oldest media in
    the vault while the item just described sits captionless."""
    if not kinds or limit <= 0:
        return []
    async with get_session() as s:
        rows = (await s.execute(
            select(VaultItem)
            .where(VaultItem.account_id == account_id)
            .where(VaultItem.removed_at.is_(None))
            .where(VaultItem.kind.in_(kinds))
            .where(VaultItem.describe_status == "described")
            .order_by(VaultItem.created_at.desc(), VaultItem.media_id.desc())
        )).scalars().all()
    out: list[int] = []
    for it in rows:
        need_cap = (not (it.suggested_caption or "").strip()
                    and not is_locked(it, "suggested_caption"))
        need_scr = (not (it.suggested_script or "").strip()
                    and not is_locked(it, "suggested_script"))
        if need_cap or need_scr:
            out.append(int(it.media_id))
            if len(out) >= limit:
                break
    return out


# ── Copy call (2nd LLM pass — feat 8) ───────────────────────────────

def _parse_copy(text: str) -> dict[str, Any]:
    """Extract the JSON body from the copy model output — tolerates bare JSON,
    fenced JSON, or a prose wrapper (same shape guard as `_parse_describe`)."""
    m = _JSON_OBJ_RE.search(text or "")
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except (TypeError, ValueError):
        return {}


async def _write_copy(
    account_id: str, media_id: int, caption: str, script: str,
) -> None:
    """Persist caption/script, skipping any field the operator has locked."""
    async with get_session() as s:
        item = await s.get(VaultItem, (account_id, media_id))
        if item is None:
            return
        vals: dict[str, Any] = {}
        if caption and not is_locked(item, "suggested_caption"):
            vals["suggested_caption"] = caption
        if script and not is_locked(item, "suggested_script"):
            vals["suggested_script"] = script
        if not vals:
            return
        await s.execute(
            update(VaultItem)
            .where(VaultItem.account_id == account_id,
                   VaultItem.media_id == media_id)
            .values(**vals)
        )
        await s.commit()


async def _copy_one(account_id: str, media_id: int, model: str) -> dict[str, Any]:
    """Turn the vision description into caption + script via one DeepSeek call.

    Returns a status dict (`ok`, `status`, `cost_millicents`) mirroring the
    shape of `_vault_describe_one` so the caller's accounting is uniform.
    """
    async with get_session() as s:
        item = await s.get(VaultItem, (account_id, media_id))
    if item is None:
        return {"ok": False, "status": "missing", "cost_millicents": 0}
    desc = (item.video_description or item.description or "").strip()
    if not desc:
        return {"ok": False, "status": "no_description", "cost_millicents": 0}
    # The V2 taxonomy (beats / clothing_state / acts / heat) — the copy call used
    # to get the prose sentence ALONE, which threw away every fact that tells the
    # writer where the sellable moment is and whether the piece can close on its
    # own. `copy_brief` renders the facts plus the how-to-use briefing.
    fields = vault_ai_brief.load_fields(item.ai_fields_json)
    user_body = vault_ai_brief.copy_brief(
        fields, description=desc, duration_seconds=item.duration_seconds,
        kind=item.kind,
    )
    need_cap = (not (item.suggested_caption or "").strip()
                and not is_locked(item, "suggested_caption"))
    need_scr = (not (item.suggested_script or "").strip()
                and not is_locked(item, "suggested_script"))
    if not (need_cap or need_scr):
        return {"ok": True, "status": "already_copied", "cost_millicents": 0}

    try:
        result = await llm_client.chat(
            model=model,
            messages=[
                {"role": "system",
                 "content": _copy_system_prompt(
                     (await load_voice_blocks(account_id)).voice)},
                {"role": "user", "content": user_body},
            ],
            purpose=_PURPOSE_COPY,
            account_id=account_id,
            response_format={"type": "json_object"},
            temperature=0.6,
        )
    except LLMCapExceeded as e:
        return {"ok": False, "status": "capped", "detail": str(e),
                "cost_millicents": 0}
    except LLMError as e:
        return {"ok": False, "status": "error", "detail": str(e)[:300],
                "cost_millicents": 0}

    data = result.parsed if isinstance(result.parsed, dict) else _parse_copy(result.content)
    caption = str(data.get("caption") or "").strip() if isinstance(data, dict) else ""
    script = str(data.get("script") or "").strip() if isinstance(data, dict) else ""
    if not (caption or script):
        return {"ok": False, "status": "empty",
                "cost_millicents": int(result.cost_cents or 0)}
    await _write_copy(account_id, media_id, caption, script)
    return {"ok": True, "status": "copied",
            "cost_millicents": int(result.cost_cents or 0),
            "call_id": result.call_id}


# ── Entry point ─────────────────────────────────────────────────────

@register("describe_media")
async def run(account_id: str, payload: dict, *, run_id: int) -> dict:
    """Cadence sweep: describe undescribed items, then fill missing copy.

    payload:
      force_ids: [media_id, …]  re-describe + re-copy these regardless of dedupe.
      limit:     int             override `describe.max_items_per_run`.
      copy_only: bool            skip describe, only fill missing caption/script
                                 on already-described items.
    """
    payload = payload or {}
    cfg = await _load_effective_config(account_id)
    if not cfg.get("enabled"):
        return {"skipped": "disabled"}

    d = cfg.get("describe") or {}
    models = cfg.get("models") or {}
    describe_model = _resolve_model(models.get("describe"), fallback="qwen3-vl-30b")
    caption_model = _resolve_model(models.get("caption"), fallback="deepseek-v4-flash")
    script_model = _resolve_model(models.get("script"), fallback="deepseek-v4-flash")

    max_items = int(payload.get("limit") or d.get("max_items_per_run") or 40)
    cap_percent = int(d.get("describe_all_cap_percent") or 80)
    images = bool(d.get("images", True))
    videos = bool(d.get("videos", True))
    copy_only = bool(payload.get("copy_only"))
    force_ids: list[int] = []
    for v in payload.get("force_ids") or []:
        try:
            force_ids.append(int(v))
        except (TypeError, ValueError):
            continue

    kinds = _enabled_kinds(images, videos)
    if not kinds:
        return {"skipped": "no_kinds_enabled"}

    budget_mc = await _budget_millicents(account_id, cap_percent)
    spent_mc = 0
    described = 0
    copied = 0
    capped = False
    failed = 0
    first_error = ""
    over_budget = 0

    def _budget_hit() -> bool:
        return bool(budget_mc) and spent_mc >= budget_mc

    # ── Describe pass ────────────────────────────────────────────────
    if not copy_only:
        ids: list[int] = list(force_ids)
        remaining = max_items - len(ids)
        if remaining > 0:
            more = await _describe_candidates(account_id, kinds, remaining)
            seen = set(ids)
            for mid in more:
                if mid not in seen:
                    ids.append(mid)
                    seen.add(mid)
        for media_id in ids[:max_items]:
            if capped:
                break
            if _budget_hit():
                over_budget += 1
                break
            try:
                res = await _vault_describe_one(
                    account_id, media_id, model=describe_model)
            except Exception:  # noqa: BLE001 — one bad item can't kill the sweep
                failed += 1
                log.warning("describe_failed account=%s media=%s",
                            account_id, media_id, exc_info=True)
                continue
            spent_mc += int(res.get("cost_millicents") or 0)
            status = res.get("status")
            if status == "capped":
                capped = True
                break
            if not res.get("ok"):
                # vault_sweep's rule: anything that is not `ok` and not `capped`
                # is a counted failure and the walk goes on. Counting nothing
                # made a whole vault failing read as a finished one; STOPPING
                # here would repeat the fault vault_sweep was extracted to end
                # (one bad item abandoning the rest of the pass).
                failed += 1
                first_error = first_error or str(res.get("detail") or "")[:300]
                continue
            described += 1
            # Chase this fresh description with the 2nd (copy) call so
            # caption + script hit the row in the SAME sweep — the vault
            # is instantly grouped-approvable rather than needing another
            # cadence cycle. Skipped if a cap already blocked us.
            if capped or _budget_hit():
                continue
            try:
                cp = await _copy_one(
                    account_id, media_id, model=caption_model)
            except Exception:  # noqa: BLE001
                failed += 1
                log.warning("copy_failed account=%s media=%s",
                            account_id, media_id, exc_info=True)
                continue
            spent_mc += int(cp.get("cost_millicents") or 0)
            if cp.get("status") == "capped":
                capped = True
            elif cp.get("ok") and cp.get("status") == "copied":
                copied += 1
            else:
                failed += 1
                first_error = first_error or str(cp.get("detail") or "")[:300]

    # ── Standalone copy pass: described-but-uncopied backlog ─────────
    if not capped:
        copy_ids = await _copy_candidates(account_id, kinds, max_items)
        for media_id in copy_ids:
            if _budget_hit():
                over_budget += 1
                break
            try:
                cp = await _copy_one(
                    account_id, media_id, model=caption_model)
            except Exception:  # noqa: BLE001
                failed += 1
                log.warning("copy_failed account=%s media=%s",
                            account_id, media_id, exc_info=True)
                continue
            spent_mc += int(cp.get("cost_millicents") or 0)
            if cp.get("status") == "capped":
                capped = True
                break
            if cp.get("ok") and cp.get("status") == "copied":
                copied += 1
            else:
                failed += 1
                first_error = first_error or str(cp.get("detail") or "")[:300]

    return {
        "described": described,
        "copied": copied,
        "capped": capped,
        "over_budget": over_budget,
        "failed": failed,
        # The sentence an operator reads when a run did nothing — the refusal
        # itself, not a count. Same key and same meaning as vault_sweep's, which
        # is what the "Describe all" button reports.
        "first_error": first_error,
        "budget_mc": budget_mc,
        "spent_mc": spent_mc,
        "describe_model": describe_model,
        "caption_model": caption_model,
        "script_model": script_model,
    }
