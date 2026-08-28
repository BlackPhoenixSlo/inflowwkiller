"""
service/llm_client.py — pluggable LLM client (Grok + DeepSeek + Z.ai).

ONE async entry point — `chat()` — that every automation runner will eventually
call instead of POSTing x.ai directly (see library/db_data/19_llm_providers.md §2,
library/db_data/05_grok_extraction.md §1+§7). Greenfield: nothing imports this yet.

Every provider here speaks the OpenAI `/chat/completions` shape, so the client is
one code path; `base_url` + `api_key` + `model` differ, and so — per MODEL, not
per provider — does the field that states the reasoning mode. That field is
carried verbatim by the registry (`llm_models.LLMModel.reasoning_body`) rather
than derived here; getting it wrong is silent on some models.

Audit + cost live in the Grok tables, generalized for multi-provider use by
migration 0026 (provider / status / cache_hit_tokens columns + a provider-keyed
daily rollup):

  * `grok_calls`      — one row per call, written BEFORE the request fires with
                        `provider` set and `status='pending'`; finalized to
                        `status='done'` (+ tokens/cost/cache_hit_tokens) or
                        `status='error'` (+ error_text).
  * `grok_daily_cost` — per-(day, account_id, provider) rollup (the PK gained
                        `provider` in 0026). The soft-cap reservation is ATOMIC:
                        a single INSERT … ON CONFLICT DO UPDATE that increments
                        cost_cents only while it stays under the cap. The
                        conflict target is the full (day, account_id, provider)
                        PK. A plain read-then-decide would let concurrent workers
                        each pass the check and collectively blow the cap. NOTE:
                        the cap is enforced per-(account, provider) row; a true
                        cross-provider account total would sum the provider rows
                        before reserving.

Privacy (05 §8): `prompt_json` holds the request with every base64 `data:` URI
replaced by an `<img:mime:bytes:digest>` manifest — all prompt TEXT survives,
no image bytes are persisted (the images are already cached in the vault, so
the base64 was only ever a re-encoded copy). `response_json` is not stored at
all. `response_text` is the one column holding model output verbatim (capped at
_RESP_TEXT_CAP) — it is PII, and it is kept because it is the documented
first-line forensic tool. Nothing here is ever logged: log lines carry model /
provider / purpose / latency / token counts, never message bodies. See the
redaction block above _insert_pending_call.
"""

from __future__ import annotations

import json
import asyncio
import hashlib
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx
from sqlalchemy import select, update as sa_update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

import tenant_keys
from db.engine import get_session
from db.models import AccountAiConfig, GrokCall, GrokDailyCost
from llm_providers import LLMProvider, PROVIDERS, house_key

log = logging.getLogger("of-relay.llm_client")

# ── Model registry ──────────────────────────────────────────────────────
# Moved to `llm_models` — WHAT there is to call, next to WHO serves it
# (`llm_providers`), leaving this module as HOW a call is made. Re-exported so
# the ~70 modules that read `llm_client.MODELS` keep working; this is still the
# canonical import site for them.
from llm_models import LLMModel, MODELS  # noqa: E402,F401


# Cost-estimate knobs for the pre-flight cap reservation. The reserve assumes a
# full output so a single runaway loop can't slip many calls under the cap
# before the rollup catches up. Costs are counted in MILLICENTS (cents x100) so
# the seed prices in `llm_models` don't floor to 0 per call.
_CHARS_PER_TOKEN = 4
_ASSUMED_OUTPUT_TOKENS = 1024
_MILLICENTS_PER_CENT = 100  # internal cost unit: 1 cent = 100 millicents
_DEFAULT_CAP_CENTS = 100  # mirrors AccountAiConfig.daily_cost_cap_cents default
_REQUEST_TIMEOUT_S = 120.0

# Transient-failure retry for the single fire (19 §2 step 4). Every automation
# funnels through here, and a one-shot POST turned a flaky provider into a
# permanent per-item failure: a DeepInfra `ReadTimeout` stamped
# `describe_status='failed'` with no retry, so a 97-item vault sweep left a
# random 3-5 undone every run and each re-run only rescued some. Only transient
# faults retry — timeouts, transport/connection errors, 429 and 5xx. A 4xx
# (auth, bad request, real content refusal) will not improve on a re-fire, so it
# fails fast as before. The audit row and the cap reservation are made ONCE,
# outside the loop, and released once on final give-up.
_LLM_MAX_ATTEMPTS = 3
_LLM_RETRY_BACKOFF_S = 1.5          # ×attempt: 1.5s, then 3.0s
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

# Defensive: strip a <think>…</think> reasoning block if a provider ever inlines
# it into `content` instead of the separate `reasoning_content` field.
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
# Strip a ```json … ``` (or bare ```) fence some models wrap JSON output in.
_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


# ── Errors (fail-fast, distinct types so callers can branch) ─────────────

class LLMError(Exception):
    """Base for all llm_client failures."""


class LLMConfigError(LLMError):
    """Unknown model, unknown provider, or missing/empty API key. Fail fast —
    never silently POST a None bearer token."""


class LLMCapExceeded(LLMError):
    """Per-account daily soft-cap would be exceeded — request was NOT sent."""

    def __init__(self, account_id: str | None, cap_cents: int, would_be_cents: int):
        self.account_id = account_id
        self.cap_cents = cap_cents
        self.would_be_cents = would_be_cents
        super().__init__(
            f"daily LLM cost cap reached for account={account_id!r}: "
            f"{would_be_cents}c would exceed cap {cap_cents}c"
        )


class LLMHTTPError(LLMError):
    """Upstream returned a non-2xx or the request never completed."""

    def __init__(self, message: str, *, status: int | None = None):
        self.status = status
        super().__init__(message)


# ── Result ───────────────────────────────────────────────────────────────

@dataclass
class LLMResult:
    call_id: int | None          # grok_calls.id (None only if the audit write failed)
    model: str
    provider: str
    content: str                 # main assistant text (reasoning stripped)
    parsed: Any | None           # json.loads(content) when response_format=json_object
    reasoning_content: str | None
    tokens_in: int | None
    tokens_out: int | None
    cache_hit_tokens: int | None  # DeepSeek prompt_cache_hit_tokens (also persisted to grok_calls)
    cost_cents: int               # MILLICENTS (cents x100) — see _cost_millicents
    latency_ms: int
    raw: dict = field(repr=False, default_factory=dict)


# ── Helpers ────────────────────────────────────────────────────────────────

def _resolve(model: str) -> tuple[LLMModel, LLMProvider]:
    """model -> (LLMModel, LLMProvider). Fail-fast on every gap.

    The KEY is deliberately not resolved here: which credential a call bills
    depends on WHOSE account it is, not on which model it picked. See
    `_tenant_api_key`.
    """
    mdl = MODELS.get(model)
    if mdl is None:
        raise LLMConfigError(
            f"unknown model {model!r}; known: {sorted(MODELS)}"
        )
    prov = PROVIDERS.get(mdl.provider)
    if prov is None:
        raise LLMConfigError(
            f"model {model!r} maps to unknown provider {mdl.provider!r}"
        )
    return mdl, prov


def _resolve_effort(mdl: LLMModel, requested: str, stored: str = "") -> str:
    """The `reasoning_effort` to put on the wire ("" = send no such field).

    Precedence: an explicit caller value, then the account\'s saved pick, then
    the model\'s own pin.

    ONE failure policy lives here — an effort this model cannot honour raises,
    before the cap reservation and the audit row, in the same place a missing
    credential is caught. A request that cannot be legal must cost neither a cap
    slot nor a `grok_calls` row.

    The other policy — that a SAVED value which no longer fits is dropped rather
    than raised on — deliberately does not live here. It belongs where config
    enters the process (`_account_effort`), because it is a statement about
    config outliving the choice that made it, not about this call. Keeping the
    two apart is what lets this function have a single rule.
    """
    effort = (requested or stored or mdl.reasoning_effort or "").strip()
    if not effort:
        return ""
    if effort not in mdl.effort_choices:
        raise LLMConfigError(
            f"reasoning_effort {effort!r} is not valid for model {mdl.id!r}; "
            f"it accepts {list(mdl.effort_choices) or 'no effort at all'}"
        )
    return effort


def effort_options(model: str) -> list[str]:
    """The reasoning-effort values an operator may pick for one model, weakest
    first; empty when the model has no such control.

    The one source the Brain editor\'s dropdown and its save-time validation
    both read, so an option that can be offered is exactly an option that can be
    stored.
    """
    mdl = MODELS.get(model)
    return list(mdl.effort_choices) if mdl else []


def effort_options_by_model() -> dict[str, list[str]]:
    """Every model\'s effort choices, keyed by model id — the whole-registry
    view an editor needs to repopulate its dropdown when the model changes.

    Here rather than composed in the API layer because it is a fact about the
    registry, and a caller that rebuilds it from `MODELS` is a second place that
    has to know an unlisted model means "no control" rather than "not a model".
    """
    return {mid: list(m.effort_choices) for mid, m in MODELS.items()}


async def _account_effort(account_id: str, mdl: LLMModel) -> str:
    """The reasoning effort an operator saved for this account, or "" — and ""
    is also the answer when what they saved no longer fits this model.

    Dropping rather than raising is the point. Config outlives the choice that
    made it: moving an account from DeepSeek to z.ai strands a "medium" that is
    legal on neither the new provider nor any future one, and a value saved
    months ago must never be able to take a reply lane down today. `resolve_model`
    treats a stale model id exactly this way for exactly this reason.

    Read only when the model can use one, so the models carrying the traffic pay
    no query for a feature they cannot express.
    """
    if not account_id or not mdl.effort_choices:
        return ""
    async with get_session() as s:
        val = await s.scalar(
            select(AccountAiConfig.reasoning_effort).where(
                AccountAiConfig.account_id == account_id
            )
        )
    saved = (val or "").strip()
    if saved and saved not in mdl.effort_choices:
        log.warning("stored reasoning_effort %r does not fit model %s %s — "
                    "using the model default %r",
                    saved, mdl.id, list(mdl.effort_choices), mdl.reasoning_effort)
        return ""
    return saved


async def _tenant_api_key(account_id: str, prov: LLMProvider) -> str:
    """The credential THIS account's call must bill.

    One agency = one key set: the owner of `account_id` (a `users` row) supplies
    the key for every creator it runs, so two agencies can never spend on one
    credential. The house key — the process-wide credential this deployment booted
    with (`llm_providers.house_key`) — is reachable only when the account has NO owner at all (an
    orphan, or the NULL house-account rollup): there is no tenant to leak
    across, and refusing those would take the deployment's own maintenance
    calls down with them.

    Everything else FAILS CLOSED. An owner that has not pasted a key for this
    provider gets an error, never a silent fall-through to the house key: a
    generic `or house_key` turns every configuration and ownership defect into a
    successful cross-tenant request billed to the deployment owner, which is
    both the invariant this function exists to hold and the only alarm that
    would ever have reported it broken.
    """
    owners = await tenant_keys.owners_of(account_id)
    if len(owners) > 1:
        # Two AGENCIES claim this creator (a master's oversight link is already
        # dropped by owners_of). REACHABLE BY DESIGN, not a corruption alarm:
        # two owners each completing a session capture for the same account, or
        # `auth.admin_grant_user_account` adding a link without removing one.
        # Neither row order nor link age says whose credential pays, so refuse —
        # billing the wrong agency silently is the one outcome this path exists
        # to prevent. The Setup card names the account and its owners; the fix
        # is for one of them to transfer it to the other (DEPLOY.md → Per-agency
        # AI keys). TRANSFER itself is safe: it deletes the old link in the same
        # transaction as the insert, so it never creates this state.
        raise LLMConfigError(
            f"account {account_id!r} has more than one owner — cannot tell "
            f"which agency's {prov.name!r} key to bill"
        )
    if not owners:
        key = house_key(prov.name)
        if not key:
            raise LLMConfigError(
                f"{prov.api_key_env} is missing/empty — cannot call provider "
                f"{prov.name!r} for un-owned account {account_id!r}"
            )
        return key
    owner = owners[0]
    key = await tenant_keys.get_key(owner, prov.name)
    if not key:
        # The one log line this feature needs: an agency's models can be split
        # across providers (`model_by_purpose` is per account AND per purpose),
        # so "they brought a DeepSeek key" does not mean every one of their
        # calls has a key. This names the missing pair instead of leaving one
        # purpose mysteriously dead on one account.
        log.warning("tenant_key_missing provider=%s account=%s owner=%s",
                    prov.name, account_id, owner)
        raise LLMConfigError(
            f"agency {owner!r} has no {prov.name!r} API key — add it in "
            f"Setup → Your AI keys (account {account_id!r})"
        )
    return key


# Vision: a single image consumes hundreds–thousands of tokens, NOT the ~10
# the URL/base64 string length implies. Reserve a conservative flat estimate per
# image so the daily-cap reservation can't be blown wide open by a describe pass
# (the "Auto-complete all" silent-overrun bug). Actual usage is reconciled from
# the provider's usage block after the call; this only governs the pre-flight.
_IMAGE_TOKENS = 1200


def _estimate_input_tokens(messages: list[dict]) -> int:
    chars = 0
    image_tokens = 0
    for m in messages:
        c = m.get("content", "")
        if isinstance(c, str):
            chars += len(c)
        elif isinstance(c, list):
            # OpenAI multimodal content: list of {type: text|image_url, ...} parts.
            for part in c:
                if isinstance(part, dict) and part.get("type") == "image_url":
                    image_tokens += _IMAGE_TOKENS
                elif isinstance(part, dict) and isinstance(part.get("text"), str):
                    chars += len(part["text"])
                else:
                    chars += len(json.dumps(part, default=str))
        else:
            chars += len(json.dumps(c, default=str))
    return chars // _CHARS_PER_TOKEN + image_tokens + 1


def _cost_millicents(model: str, tokens_in: int, tokens_out: int,
                     cache_hit_tokens: int = 0) -> int:
    """Cost in MILLICENTS (cents x100), the internal unit for the cap rollup.

    Counting in millicents is the whole point of this path: DeepSeek's per-call
    cost is a fraction of a cent, so the old `int(round(cents))` floored every
    call to 0 and the daily cap never tripped. At x100 resolution a real
    automation call records a non-zero cost and the rollup accumulates toward the
    cap. The grok_calls / grok_daily_cost `cost_cents` INT columns now hold
    millicents (no schema change — same column, finer unit).

    `cache_hit_tokens` (DeepSeek `prompt_cache_hit_tokens`) is the slice of
    `tokens_in` billed at the cheap cache-hit rate; the remainder is cache-miss.
    Clamped to tokens_in so a malformed usage block can't go negative. Pass 0
    (the default) for the pre-flight reservation — no cache info exists yet, and
    assuming all-miss keeps the reserve conservative for the cap."""
    mdl = MODELS.get(model)
    if mdl is None:
        return 0
    hit = max(0, min(cache_hit_tokens, tokens_in))
    miss = tokens_in - hit
    cents = (
        miss / 1000.0 * mdl.input_per_1k_cents
        + hit / 1000.0 * mdl.input_cache_hit_per_1k_cents
        + tokens_out / 1000.0 * mdl.output_per_1k_cents
    )
    return int(round(cents * _MILLICENTS_PER_CENT))


def _cache_hit_tokens(usage: dict) -> int | None:
    """Cached-input tokens, from whichever field the provider reports them in.

    DeepSeek puts the count at the top level as `prompt_cache_hit_tokens`; the
    OpenAI-shaped providers nest it at `prompt_tokens_details.cached_tokens`.
    This is the count the cost path bills at the (far cheaper) hit rate, so
    reading the wrong one is not a cosmetic miss — it prices the entire input.

    Test for ABSENCE, not truthiness. DeepSeek legitimately reports 0 on a cold
    prompt, and `top or nested` would throw that real zero away and go looking
    in the other provider's field; where a provider populates both shapes, that
    silently bills a cache MISS at the hit rate. The nested object is read
    defensively because a provider is free to send it as null, or as something
    that isn't a mapping at all.
    """
    top = usage.get("prompt_cache_hit_tokens")
    if top is not None:
        return top
    details = usage.get("prompt_tokens_details")
    if isinstance(details, dict):
        return details.get("cached_tokens")
    return None


def _reasoning_tokens(usage: dict) -> int | None:
    """Tokens the model spent thinking, when the provider says. Diagnostic only
    — it is already counted inside `completion_tokens`, so it is logged and not
    billed separately (double-counting it would inflate every reasoning call)."""
    details = usage.get("completion_tokens_details")
    if isinstance(details, dict):
        return details.get("reasoning_tokens")
    return None


def _clean_content(text: str) -> str:
    """Drop any inlined reasoning + markdown JSON fence so json.loads can run."""
    if not text:
        return text
    text = _THINK_RE.sub("", text).strip()
    if text.startswith("```"):
        text = _FENCE_RE.sub("", text).strip()
    return text


async def _get_cap_cents(account_id: str | None) -> int:
    if not account_id:
        return _DEFAULT_CAP_CENTS
    async with get_session() as s:
        cap = await s.scalar(
            select(AccountAiConfig.daily_cost_cap_cents).where(
                AccountAiConfig.account_id == account_id
            )
        )
    return int(cap) if cap is not None else _DEFAULT_CAP_CENTS


async def cap_state(account_id: str | None, provider: str) -> dict[str, Any]:
    """Today's spend against the per-(account, provider) daily cap. Read-only.

    The cap is ENFORCED in `_reserve`, per call, which is the only place it can
    be enforced correctly. This is the same row read for callers that need to
    answer "would starting this achieve anything" BEFORE they start it: a
    background sweep whose every LLM call is refused looks, from the outside,
    exactly like one that is working — it fans out its fetches, counts its way
    to done, and leaves the work undone.

    `capped` is computed from the money rather than from the row's sticky
    `is_capped` flag. That flag is set when a reservation is refused and never
    cleared, so raising `daily_cost_cap_cents` mid-day makes calls succeed again
    while it stays True — a gate reading it would keep refusing against a budget
    that is no longer spent. It is still reported, as `flagged`, because "we hit
    the cap today" is worth showing even after the cap is raised.
    """
    day = datetime.utcnow().strftime("%Y-%m-%d")
    cap_mc = int(await _get_cap_cents(account_id) * _MILLICENTS_PER_CENT)
    async with get_session() as s:
        row = (await s.execute(
            select(GrokDailyCost.cost_cents, GrokDailyCost.is_capped).where(
                GrokDailyCost.day == day,
                # `== None` renders as `= NULL` and matches nothing, so the
                # house-account row has to be addressed with IS NULL.
                GrokDailyCost.account_id.is_(None) if account_id is None
                else GrokDailyCost.account_id == account_id,
                GrokDailyCost.provider == provider,
            )
        )).first()
    spent_mc = int(row[0] or 0) if row else 0
    return {
        "day": day,
        "provider": provider,
        "cap_millicents": cap_mc,
        "spent_millicents": spent_mc,
        "remaining_millicents": max(0, cap_mc - spent_mc),
        "capped": spent_mc >= cap_mc,
        "flagged": bool(row[1]) if row else False,
    }


async def _reserve(day: str, account_id: str | None, reserve_cents: int, cap_cents: int,
                   now: datetime, provider: str) -> int:
    """Atomically reserve `reserve_cents` against the per-(day, account,
    provider) cap.

    One statement does the read-check-write: INSERT … ON CONFLICT DO UPDATE that
    bumps cost_cents/call_count ONLY while the running total stays <= cap. The
    conflict target is the full (day, account_id, provider) PK (migration 0026).
    If the bump would breach the cap the DO UPDATE's WHERE is false → the row is
    neither inserted nor updated → RETURNING is empty → we know we're capped.
    This is the whole point: concurrent workers can't each pass an independent
    read.

    Returns the new running cost_cents on success. Raises LLMCapExceeded if the
    reservation didn't fit. SQLite serializes writers, so the increment + check
    are race-free even under the worker fan-out.
    """
    stmt = (
        sqlite_insert(GrokDailyCost)
        .values(
            day=day,
            account_id=account_id,
            provider=provider,
            cost_cents=reserve_cents,
            call_count=1,
            is_capped=False,
            updated_at=now,
        )
        .on_conflict_do_update(
            index_elements=["day", "account_id", "provider"],
            set_={
                "cost_cents": GrokDailyCost.cost_cents + reserve_cents,
                "call_count": GrokDailyCost.call_count + 1,
                "updated_at": now,
            },
            where=(GrokDailyCost.cost_cents + reserve_cents <= cap_cents),
        )
        .returning(GrokDailyCost.cost_cents)
    )
    async with get_session() as s:
        total = (await s.execute(stmt)).scalar_one_or_none()

        if total is None:
            # DO UPDATE skipped (cap breach on an existing row). Flag it (05 §7
            # step 2) and report the capped state.
            existing = await s.scalar(
                select(GrokDailyCost.cost_cents).where(
                    GrokDailyCost.day == day,
                    GrokDailyCost.account_id == account_id,
                    GrokDailyCost.provider == provider,
                )
            )
            await s.execute(
                sa_update(GrokDailyCost)
                .where(
                    GrokDailyCost.day == day,
                    GrokDailyCost.account_id == account_id,
                    GrokDailyCost.provider == provider,
                )
                .values(is_capped=True, updated_at=now)
            )
            raise LLMCapExceeded(
                account_id, cap_cents, (existing or 0) + reserve_cents
            )

        if total > cap_cents:
            # Only reachable on the fresh-INSERT path (the DO UPDATE WHERE can
            # never return an over-cap total): a single call's reserve alone
            # exceeds the cap. Back out the row we just inserted and report.
            await s.execute(
                sa_update(GrokDailyCost)
                .where(
                    GrokDailyCost.day == day,
                    GrokDailyCost.account_id == account_id,
                    GrokDailyCost.provider == provider,
                )
                .values(
                    cost_cents=GrokDailyCost.cost_cents - reserve_cents,
                    call_count=GrokDailyCost.call_count - 1,
                    is_capped=True,
                    updated_at=now,
                )
            )
            raise LLMCapExceeded(account_id, cap_cents, total)

    return total


async def _adjust_daily(day: str, account_id: str | None, cost_delta: int,
                        count_delta: int, now: datetime, provider: str) -> None:
    """Reconcile the rollup after the call: apply the (actual - reserved) cost
    delta on success, or release the whole reservation on failure. No-op when
    both deltas are zero (the common case while seed prices are 0). Scoped to the
    (day, account_id, provider) row the matching _reserve created."""
    if cost_delta == 0 and count_delta == 0:
        return
    async with get_session() as s:
        await s.execute(
            sa_update(GrokDailyCost)
            .where(
                GrokDailyCost.day == day,
                GrokDailyCost.account_id == account_id,
                GrokDailyCost.provider == provider,
            )
            .values(
                cost_cents=GrokDailyCost.cost_cents + cost_delta,
                call_count=GrokDailyCost.call_count + count_delta,
                updated_at=now,
            )
        )


# ── Audit receipt (05 §8) ──────────────────────────────────────────────────
#
# `prompt_json` used to hold `json.dumps(request_body)` — the ENTIRE outbound
# body, verbatim. For the vision lanes that body carries base64 `data:` URIs:
# measured on prod 2026-08-03, `describe_media` averaged 203 KB/row and was
# **99.3% base64**, making grok_calls 1,325 MB = 52% of the whole DB. Those
# bytes are a re-encoded copy of vault media we already own, and the column
# has never been read back — an exhaustive sweep (python/TS/shell/SQL/alembic)
# found ZERO readers, and `grep -oE 'GrokCall\.[a-z_]+'` never names it.
#
# So we keep the WORDS and drop the PIXELS: every `data:` URI becomes an
# `<img:mime:bytes:digest>` manifest, and everything else is stored verbatim.
# Measured over real prod rows: 1,199 MB -> 318 MB. Policy points the same way
# (05 §8: "90-day retention is the privacy ceiling; pruning is mandatory, not
# optional"); there is no retain-minimum for this table.
#
# This stays IN `prompt_json` rather than moving to new columns, deliberately:
# that column is `NOT NULL` with no server_default, and the boot-time schema
# walker (db/engine.py:179) SKIPS exactly that shape while the service never
# runs alembic (db/engine.py:114). Adding or dropping columns here is the one
# change that could fail silently — `_insert_pending_call` swallows below — so
# we make zero schema changes and the constraint stays satisfied.
_DATA_URI_RE = re.compile(r'data:([a-zA-Z0-9.+/-]+);base64,([A-Za-z0-9+/=]+)')

_RESP_TEXT_CAP = int(os.environ.get("LLM_RESP_TEXT_CAP", "4000"))

# What a row stores when redaction itself failed. A literal, in one place, so
# "is this row degraded?" is one grep and not a copy-pasted string.
_DEGRADED_STUB = '{"v":1,"degraded":true}'

# Incremented on every fallback to _DEGRADED_STUB. Exposed via
# audit_degraded_count() -> /admin/stats/grok-calls.
_AUDIT_DEGRADED = 0

# If disk ever gets tight again, the lever is the retention window that already
# exists — GROK_CALLS_RETAIN_S (server.py), 30d today. Dropping it to 7d frees
# the same order of magnitude without a second storage format for this column.


def _b64_decoded_len(payload: str) -> int:
    """Decoded byte count of a base64 payload, without decoding it."""
    return max(0, (len(payload) * 3) // 4 - payload.count("="))


def _redact_body(body: Any) -> Any:
    """Deep copy of `body` with every base64 `data:` URI replaced by a manifest.

    MUST deep-copy: the caller's `messages` list is the SAME object that gets
    POSTed (chat() passes `messages` straight into `body`, then httpx serializes
    that dict). A shallow copy would rewrite the outbound request and send
    `<img:…>` to the provider instead of the image — breaking every vision lane
    on the first call.
    """
    if isinstance(body, dict):
        return {k: _redact_body(v) for k, v in body.items()}
    if isinstance(body, list):
        return [_redact_body(v) for v in body]
    if isinstance(body, str):
        return _DATA_URI_RE.sub(
            lambda m: f"<img:{m.group(1)}:{_b64_decoded_len(m.group(2))}b:"
                      f"{hashlib.sha256(m.group(2).encode()).hexdigest()[:16]}>",
            body,
        )
    return body


def _redacted_json(request_body: dict) -> str:
    """What lands in `prompt_json`: the full body with every data: URI
    manifested. All prompt text survives; no image bytes do.

    Never raises. The caller (`_insert_pending_call`) swallows exceptions so a
    failed audit can't take down a live LLM call — which means a raise here
    would be INVISIBLE. `audit_degraded` on /admin/stats/grok-calls is how you
    find out it happened.
    """
    global _AUDIT_DEGRADED
    try:
        return json.dumps(_redact_body(request_body), ensure_ascii=False, default=str)
    except Exception:
        _AUDIT_DEGRADED += 1
        log.exception("llm_client: prompt redaction failed — storing stub")
        return _DEGRADED_STUB


def audit_degraded_count() -> int:
    """How many audit rows fell back to the stub since boot. Read by
    /admin/stats/grok-calls so a degraded audit is visible, not silent."""
    return _AUDIT_DEGRADED


async def _insert_pending_call(*, purpose: str, provider: str, account_id: str | None,
                               fan_id: int | None, model: str, endpoint: str,
                               temperature: float, request_body: dict,
                               now: datetime) -> int | None:
    """Write the grok_calls audit row BEFORE the request fires (05 §4) with
    `provider` set and `status='pending'` (columns added in 0026). Returns the
    row id, or None if the audit write itself failed (never block the call on
    audit)."""
    prompt_audit = _redacted_json(request_body)
    try:
        async with get_session() as s:
            call = GrokCall(
                purpose=purpose,
                provider=provider,
                status="pending",
                account_id=account_id,
                fan_id=fan_id,
                model=model,
                endpoint=endpoint,
                temperature=temperature,
                prompt_json=prompt_audit,
                was_dry_run=False,
                called_at=now,
            )
            s.add(call)
            await s.flush()
            return call.id
    except Exception:
        log.exception("llm_client: failed to write pending grok_calls row "
                      "(purpose=%s model=%s) — proceeding without audit id",
                      purpose, model)
        return None


async def _finalize_call(call_id: int | None, **values: Any) -> None:
    if call_id is None:
        return
    try:
        async with get_session() as s:
            await s.execute(
                sa_update(GrokCall).where(GrokCall.id == call_id).values(**values)
            )
    except Exception:
        log.exception("llm_client: failed to finalize grok_calls id=%s", call_id)


# ── Public entry point ─────────────────────────────────────────────────────

async def chat(
    *,
    model: str,
    messages: list[dict],
    purpose: str,
    account_id: str,
    fan_id: int | None = None,
    response_format: dict | None = None,
    temperature: float = 0.7,
    reasoning_effort: str = "",
) -> LLMResult:
    """Call an OpenAI-compatible LLM (Grok, DeepSeek or Z.ai) with full cost + audit.

    Flow (19 §2):
      1. Resolve model -> provider -> base_url, then the KEY of the agency that
         owns `account_id` (fail-fast on any gap — see _tenant_api_key).
      2. ATOMICALLY reserve the estimated cost against the daily soft cap
         (05 §7) BEFORE firing — raises LLMCapExceeded if it doesn't fit.
      3. Write a pending `grok_calls` row.
      4. POST {base_url}/chat/completions.
      5. Fill tokens/cost + finalize the call row; reconcile the daily rollup.

    Args:
        model:           a key of MODELS (e.g. "grok-4-1-fast-non-reasoning").
        messages:        OpenAI chat messages (already variable-substituted).
        purpose:         attribution tag (gen_info / welcome / welcome_chatter_for_info_reply / …).
        account_id:      per-account cap + audit attribution, AND the agency
                         whose API key pays for the call (_tenant_api_key).
        fan_id:          optional fan attribution.
        response_format: e.g. {"type": "json_object"} for structured output.
        temperature:     sampling temperature.
        reasoning_effort: OPTIONAL override of how hard a reasoning model may
                         think. Leave it empty and the model's own pin is used
                         — the right answer almost everywhere, and why this no
                         longer defaults to a value. The accepted strings are
                         PER PROVIDER and not interchangeable: DeepSeek takes
                         "low"/"medium"/"high", Z.ai takes "low"/"high"/"max"
                         and 400s on "medium" — so the legal set is per MODEL
                         (`effort_choices`), not per provider. Passing one to a
                         model that has none is an error, not a no-op. See
                         `effort_options` / `_resolve_effort`.

    Raises:
        LLMConfigError, LLMCapExceeded, LLMHTTPError.
    """
    mdl, prov = _resolve(model)
    # Before the cap reservation and the audit row, exactly where the old
    # combined _resolve raised: a call with no usable credential must cost
    # neither a cap slot nor a grok_calls row. Same for an effort string the
    # provider would reject.
    stored_effort = "" if reasoning_effort else await _account_effort(account_id, mdl)
    effort = _resolve_effort(mdl, reasoning_effort, stored=stored_effort)
    api_key = await _tenant_api_key(account_id, prov)

    url = prov.base_url.rstrip("/") + "/chat/completions"
    endpoint = httpx.URL(url).path  # "/v1/chat/completions" (grok) | "/chat/completions" (deepseek)

    now = datetime.utcnow()
    day = now.strftime("%Y-%m-%d")

    # ── 2. Atomic pre-flight cap reservation (in millicents) ────────────
    est_in = _estimate_input_tokens(messages)
    reserve_mc = _cost_millicents(model, est_in, _ASSUMED_OUTPUT_TOKENS)
    cap_mc = await _get_cap_cents(account_id) * _MILLICENTS_PER_CENT  # cap is stored in cents
    await _reserve(day, account_id, reserve_mc, cap_mc, now, prov.name)  # raises LLMCapExceeded

    # ── Build the OpenAI-compatible body ────────────────────────────────
    body: dict[str, Any] = {
        "model": mdl.wire_model(),  # provider's real name (≠ our id for DeepInfra)
        "messages": messages,
        "temperature": temperature,
        "stream": False,
    }
    if response_format is not None:
        body["response_format"] = response_format
    # The reasoning mode, stated explicitly and never by omission — the model
    # registry holds the exact body keys, so there is nothing to branch on here.
    #
    # Omission is the failure this rule was written from. DeepSeek V4 reasons by
    # default, so an absent `thinking` field means "whatever the provider
    # defaults to today", and that default moved under us (see llm_models); on
    # the flash model it burns the whole output budget reasoning and returns
    # content="" — a silent empty reply, worse than a 400. Omitting the field on
    # glm-4.5-air leaves it reasoning too. `enable_thinking: false` is the
    # obvious-looking alternative on both and is SILENTLY IGNORED by both.
    #
    # reasoning_content comes back in its own field; we strip it before any JSON
    # parse.
    body.update(mdl.reasoning_keys())
    if effort:
        body["reasoning_effort"] = effort

    # ── 3. Pending audit row ────────────────────────────────────────────
    call_id = await _insert_pending_call(
        purpose=purpose, provider=prov.name, account_id=account_id, fan_id=fan_id,
        model=model, endpoint=endpoint, temperature=temperature,
        request_body=body, now=now,
    )

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    # ── 4. Fire (retry transient faults; see _LLM_MAX_ATTEMPTS) ─────────
    # The reservation + audit row above are made ONCE. Only the network round
    # trip repeats; a permanent failure finalizes and releases exactly once,
    # below, when the attempts run out.
    data = None
    for attempt in range(_LLM_MAX_ATTEMPTS):
        last = attempt + 1 >= _LLM_MAX_ATTEMPTS
        t0 = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT_S) as client:
                resp = await client.post(url, headers=headers, json=body)
            latency_ms = int(round((time.monotonic() - t0) * 1000))
            resp.raise_for_status()
            data = resp.json()
            break
        except httpx.HTTPStatusError as e:
            latency_ms = int(round((time.monotonic() - t0) * 1000))
            # e.response.text is the provider's error body (not our prompt) → safe to keep.
            snippet = (e.response.text or "")[:500]
            msg = f"{prov.name} HTTP {e.response.status_code}: {snippet}"
            if e.response.status_code in _RETRYABLE_STATUS and not last:
                log.warning("llm_client: %s HTTP %s — retry %d/%d model=%s purpose=%s",
                            prov.name, e.response.status_code, attempt + 1,
                            _LLM_MAX_ATTEMPTS, model, purpose)
                await asyncio.sleep(_LLM_RETRY_BACKOFF_S * (attempt + 1))
                continue
            # On an error row, upgrade the receipt to carry the image-redacted
            # body: `error_text` is the PROVIDER's sentence, not ours, so this
            # is the only at-rest record of what we actually sent.
            await _finalize_call(call_id, status="error", error_text=msg[:2000],
                                 latency_ms=latency_ms,
                                 prompt_json=_redacted_json(body))
            await _adjust_daily(day, account_id, -reserve_mc, -1, now, prov.name)  # release reservation
            log.warning("llm_client: %s call failed model=%s purpose=%s status=%s latency=%dms",
                        prov.name, model, purpose, e.response.status_code, latency_ms)
            raise LLMHTTPError(msg, status=e.response.status_code) from e
        except (httpx.RequestError, ValueError) as e:
            # httpx.RequestError = timeout / connection / transport, all transient.
            # ValueError = resp.json() on a non-JSON body; a re-fire may get a clean one.
            latency_ms = int(round((time.monotonic() - t0) * 1000))
            msg = f"{prov.name} request error: {type(e).__name__}: {e}"
            if not last:
                log.warning("llm_client: %s %s — retry %d/%d model=%s purpose=%s latency=%dms",
                            prov.name, type(e).__name__, attempt + 1,
                            _LLM_MAX_ATTEMPTS, model, purpose, latency_ms)
                await asyncio.sleep(_LLM_RETRY_BACKOFF_S * (attempt + 1))
                continue
            await _finalize_call(call_id, status="error", error_text=msg[:2000],
                                 latency_ms=latency_ms,
                                 prompt_json=_redacted_json(body))
            await _adjust_daily(day, account_id, -reserve_mc, -1, now, prov.name)
            log.warning("llm_client: %s call errored model=%s purpose=%s latency=%dms (%s)",
                        prov.name, model, purpose, latency_ms, type(e).__name__)
            raise LLMHTTPError(msg) from e

    # ── 5. Parse + cost + finalize ──────────────────────────────────────
    choice = (data.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    raw_content = message.get("content") or ""
    reasoning_content = message.get("reasoning_content")  # DeepSeek thinking
    content = _clean_content(raw_content)

    usage = data.get("usage") or {}
    tokens_in = usage.get("prompt_tokens")
    tokens_out = usage.get("completion_tokens")
    cache_hit_tokens = _cache_hit_tokens(usage)
    reasoning_tokens = _reasoning_tokens(usage)

    actual_mc = _cost_millicents(model, tokens_in or 0, tokens_out or 0,
                                 cache_hit_tokens or 0)

    parsed: Any | None = None
    if response_format and response_format.get("type") == "json_object":
        try:
            parsed = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            # Don't crash the caller — surface raw content + parsed=None. Log
            # only the length (never the body — it's PII per 05 §8).
            log.warning("llm_client: response_format=json_object but content did "
                        "not parse (model=%s purpose=%s len=%d)",
                        model, purpose, len(content or ""))

    await _finalize_call(
        call_id,
        status="done",
        # response_json is NOT stored. Its only ever-reader was the one-shot
        # `status` backfill in migration 20260604_0200 (which tested IS NOT
        # NULL, never the content) and that has long since run. Everything we
        # actually use from it — usage counts, finish_reason, fingerprint — is
        # denormalized into columns right here.
        response_text=(content or "")[:_RESP_TEXT_CAP],
        latency_ms=latency_ms,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cache_hit_tokens=cache_hit_tokens,
        cost_cents=actual_mc,  # millicents (see _cost_millicents)
    )
    # Reconcile the rollup with the real cost (delta vs. the estimate reserved).
    await _adjust_daily(day, account_id, actual_mc - reserve_mc, 0, now, prov.name)

    # reasoning= is DIAGNOSTIC ONLY and is deliberately not a column. Providers
    # that report it count it INSIDE completion_tokens (confirmed on both), so
    # cost and the cap are already right without it; a prod migration for a
    # counter that only ever answers "is this model thinking more than we think"
    # is not proportionate. Absent (None) on providers that don't report it.
    log.info("llm_client: %s %s purpose=%s latency=%dms in=%s out=%s cache_hit=%s "
             "reasoning=%s cost=%dmc",
             prov.name, model, purpose, latency_ms, tokens_in, tokens_out,
             cache_hit_tokens, reasoning_tokens, actual_mc)

    return LLMResult(
        call_id=call_id,
        model=model,
        provider=prov.name,
        content=content,
        parsed=parsed,
        reasoning_content=reasoning_content,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cache_hit_tokens=cache_hit_tokens,
        cost_cents=actual_mc,  # millicents (see _cost_millicents)
        latency_ms=latency_ms,
        raw=data,
    )
