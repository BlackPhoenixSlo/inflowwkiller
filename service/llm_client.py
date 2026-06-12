"""
service/llm_client.py — pluggable LLM client (Grok + DeepSeek).

ONE async entry point — `chat()` — that every automation runner will eventually
call instead of POSTing x.ai directly (see library/db_data/19_llm_providers.md §2,
library/db_data/05_grok_extraction.md §1+§7). Greenfield: nothing imports this yet.

Both x.ai (Grok) and DeepSeek speak the OpenAI `/chat/completions` shape, so the
client is one code path; only `base_url` + `api_key` + `model` differ.

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

Privacy (05 §8): prompt_json / response_json hold raw chat text → PII. They are
written to the DB only and NEVER logged. Log lines here carry model / provider /
purpose / latency / token counts — never message bodies.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from dotenv import dotenv_values, load_dotenv
from sqlalchemy import select, update as sa_update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from db.engine import get_session
from db.models import AccountAiConfig, GrokCall, GrokDailyCost

log = logging.getLogger("of-relay.llm_client")

_HERE = Path(__file__).resolve().parent
_ENV_FILE = _HERE / ".env"
# Mirror db.engine: load service/.env if it hasn't been loaded yet. override=False
# means a real value already in the process env (e.g. a host-exported key) wins.
load_dotenv(_ENV_FILE, override=False)


# ── Provider + model registry ───────────────────────────────────────────
# DB-backed model_pricing is a later migration (19 §3); this is the seed.

@dataclass(frozen=True)
class LLMProvider:
    name: str          # "grok" | "deepseek"
    base_url: str      # OpenAI-compatible base
    api_key_env: str   # env var holding the bearer key


@dataclass(frozen=True)
class LLMModel:
    id: str                           # OUR internal id (audit + config key)
    provider: str                     # -> LLMProvider.name
    # The name the PROVIDER expects on the wire. Our internal id is a stable
    # handle; the provider's real model name can differ (and change). DeepSeek
    # V4 is one physical model selected by NAME: "deepseek-chat" = thinking OFF,
    # "deepseek-reasoner" = thinking ON — both echo back model="deepseek-v4-flash".
    # Empty → send `id` verbatim. Verified live 2026-06-05 (19 §4).
    api_model: str = ""
    # Pricing is cents per 1k tokens; the cost path multiplies by 100 and counts
    # in MILLICENTS so sub-cent DeepSeek calls don't floor to 0 (model_pricing
    # table later supersedes these seeds — 19 §3). `input_per_1k_cents` is the
    # CACHE-MISS rate; `input_cache_hit_per_1k_cents` is the (far cheaper) rate
    # DeepSeek bills for `prompt_cache_hit_tokens`. With long stable system
    # prompts most input tokens are cache hits, so ignoring this over-estimates
    # cost by ~50× on the cached portion (real dashboard spend confirmed this).
    input_per_1k_cents: float = 0.0
    input_cache_hit_per_1k_cents: float = 0.0
    output_per_1k_cents: float = 0.0
    thinking: bool = False            # DeepSeek v4-pro reasoning mode

    def wire_model(self) -> str:
        """The model name to put on the request body (provider's real name)."""
        return self.api_model or self.id


# Both keys are CONFIRMED present in service/.env (verified 2026-06-04, 19 §4).
PROVIDERS: dict[str, LLMProvider] = {
    "grok":     LLMProvider("grok",     "https://api.x.ai/v1",      "GROK_API_KEY"),
    "deepseek": LLMProvider("deepseek", "https://api.deepseek.com", "DEEPSEEK_API_KEY"),
}

# Prices are cents per 1k tokens (small, real-as-of-2026-06 seeds — enough to
# stop a runaway loop via the cap; model_pricing later overrides). DeepSeek
# official pricing (per 1M tokens → cents per 1k = /10):
#   v4-flash: $0.14 miss / $0.0028 hit in · $0.28 out → 0.014 / 0.00028 / 0.028
#   v4-pro:   $0.435 miss / $0.003625 hit in · $0.87 out → 0.0435 / 0.0003625 / 0.087
# Cache hits are ~50× cheaper than misses; with long stable system prompts the
# bulk of input is cache hits, so the cost path now bills the hit portion at the
# hit rate (verified against the DeepSeek dashboard, where blended spend is far
# below the cache-miss rate). Grok-4.1-fast: ~$0.20/1M in, ~$0.50/1M out.
#
# api_model is the name DeepSeek actually wants: "deepseek-v4-flash" exists but
# REASONS by default; our non-thinking flash must send "deepseek-chat" (thinking
# off) and the thinking pro must send "deepseek-reasoner" (verified live).
MODELS: dict[str, LLMModel] = {
    "grok-4-1-fast-non-reasoning": LLMModel(
        "grok-4-1-fast-non-reasoning", "grok",
        input_per_1k_cents=0.02, output_per_1k_cents=0.05,
    ),
    "deepseek-v4-flash": LLMModel(  # non-thinking → deepseek-chat on the wire
        "deepseek-v4-flash", "deepseek", api_model="deepseek-chat",
        input_per_1k_cents=0.014, input_cache_hit_per_1k_cents=0.00028,
        output_per_1k_cents=0.028,
    ),
    "deepseek-v4-pro": LLMModel(  # thinking-capable → deepseek-reasoner on the wire
        "deepseek-v4-pro", "deepseek", api_model="deepseek-reasoner",
        input_per_1k_cents=0.0435, input_cache_hit_per_1k_cents=0.0003625,
        output_per_1k_cents=0.087, thinking=True,
    ),
}

# Cost-estimate knobs for the pre-flight cap reservation. The reserve assumes a
# full output so a single runaway loop can't slip many calls under the cap
# before the rollup catches up. Costs are counted in MILLICENTS (cents x100) so
# the seed prices above don't floor to 0 per call (the bug this wave fixes).
_CHARS_PER_TOKEN = 4
_ASSUMED_OUTPUT_TOKENS = 1024
_MILLICENTS_PER_CENT = 100  # internal cost unit: 1 cent = 100 millicents
_DEFAULT_CAP_CENTS = 100  # mirrors AccountAiConfig.daily_cost_cap_cents default
_REQUEST_TIMEOUT_S = 120.0

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

def _api_key(env_name: str) -> str:
    """Resolve a provider key. A non-empty process-env value wins; if it's
    missing or blank we fall back to the COPYed service/.env directly.

    Why the file fallback: docker-compose passes the key through as
    `${GROK_API_KEY:-}`, which injects an EMPTY string into the container when
    the host shell doesn't export it. load_dotenv(override=False) then refuses
    to backfill the already-set (empty) var, so the real value sitting in the
    COPYed service/.env would be ignored. Reading the file here recovers it —
    the Phase-F clean-build footgun, defused."""
    val = (os.environ.get(env_name) or "").strip()
    if val:
        return val
    try:
        return (dotenv_values(_ENV_FILE).get(env_name) or "").strip()
    except Exception:  # pragma: no cover — never let key lookup crash the caller
        return ""


def _resolve(model: str) -> tuple[LLMModel, LLMProvider, str]:
    """model -> (LLMModel, LLMProvider, api_key). Fail-fast on every gap."""
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
    key = _api_key(prov.api_key_env)
    if not key:
        raise LLMConfigError(
            f"{prov.api_key_env} is missing/empty — cannot call provider "
            f"{prov.name!r} for model {model!r}"
        )
    return mdl, prov, key


def _estimate_input_tokens(messages: list[dict]) -> int:
    chars = 0
    for m in messages:
        c = m.get("content", "")
        chars += len(c) if isinstance(c, str) else len(json.dumps(c, default=str))
    return chars // _CHARS_PER_TOKEN + 1


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


async def _insert_pending_call(*, purpose: str, provider: str, account_id: str | None,
                               fan_id: int | None, model: str, endpoint: str,
                               temperature: float, request_body: dict,
                               now: datetime) -> int | None:
    """Write the grok_calls audit row BEFORE the request fires (05 §4) with
    `provider` set and `status='pending'` (columns added in 0026). Returns the
    row id, or None if the audit write itself failed (never block the call on
    audit)."""
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
                prompt_json=json.dumps(request_body, ensure_ascii=False, default=str),
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
    reasoning_effort: str = "high",
) -> LLMResult:
    """Call an OpenAI-compatible LLM (Grok or DeepSeek) with full cost + audit.

    Flow (19 §2):
      1. Resolve model -> provider -> base_url + key (fail-fast on any gap).
      2. ATOMICALLY reserve the estimated cost against the daily soft cap
         (05 §7) BEFORE firing — raises LLMCapExceeded if it doesn't fit.
      3. Write a pending `grok_calls` row.
      4. POST {base_url}/chat/completions.
      5. Fill tokens/cost + finalize the call row; reconcile the daily rollup.

    Args:
        model:           a key of MODELS (e.g. "grok-4-1-fast-non-reasoning").
        messages:        OpenAI chat messages (already variable-substituted).
        purpose:         attribution tag (gen_info / welcome / of_ai_chat_reply / …).
        account_id:      per-account cap + audit attribution.
        fan_id:          optional fan attribution.
        response_format: e.g. {"type": "json_object"} for structured output.
        temperature:     sampling temperature.
        reasoning_effort: DeepSeek v4-pro thinking effort ("low"/"medium"/"high").

    Raises:
        LLMConfigError, LLMCapExceeded, LLMHTTPError.
    """
    mdl, prov, api_key = _resolve(model)

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
        "model": mdl.wire_model(),  # provider's real name (≠ our internal id for DeepSeek)
        "messages": messages,
        "temperature": temperature,
        "stream": False,
    }
    if response_format is not None:
        body["response_format"] = response_format
    if mdl.thinking:
        # DeepSeek v4-pro thinking mode (19 §2). reasoning_content comes back in
        # a separate field; we strip it before any JSON parse.
        body["thinking"] = {"type": "enabled"}
        body["reasoning_effort"] = reasoning_effort

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

    # ── 4. Fire ─────────────────────────────────────────────────────────
    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT_S) as client:
            resp = await client.post(url, headers=headers, json=body)
        latency_ms = int(round((time.monotonic() - t0) * 1000))
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPStatusError as e:
        latency_ms = int(round((time.monotonic() - t0) * 1000))
        # e.response.text is the provider's error body (not our prompt) → safe to keep.
        snippet = (e.response.text or "")[:500]
        msg = f"{prov.name} HTTP {e.response.status_code}: {snippet}"
        await _finalize_call(call_id, status="error", error_text=msg[:2000], latency_ms=latency_ms)
        await _adjust_daily(day, account_id, -reserve_mc, -1, now, prov.name)  # release reservation
        log.warning("llm_client: %s call failed model=%s purpose=%s status=%s latency=%dms",
                    prov.name, model, purpose, e.response.status_code, latency_ms)
        raise LLMHTTPError(msg, status=e.response.status_code) from e
    except (httpx.RequestError, ValueError) as e:
        # ValueError covers resp.json() on a non-JSON body.
        latency_ms = int(round((time.monotonic() - t0) * 1000))
        msg = f"{prov.name} request error: {type(e).__name__}: {e}"
        await _finalize_call(call_id, status="error", error_text=msg[:2000], latency_ms=latency_ms)
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
    cache_hit_tokens = usage.get("prompt_cache_hit_tokens")  # DeepSeek-specific

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
        response_json=json.dumps(data, ensure_ascii=False, default=str),
        response_text=content,
        latency_ms=latency_ms,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cache_hit_tokens=cache_hit_tokens,
        cost_cents=actual_mc,  # millicents (see _cost_millicents)
    )
    # Reconcile the rollup with the real cost (delta vs. the estimate reserved).
    await _adjust_daily(day, account_id, actual_mc - reserve_mc, 0, now, prov.name)

    log.info("llm_client: %s %s purpose=%s latency=%dms in=%s out=%s cache_hit=%s cost=%dmc",
             prov.name, model, purpose, latency_ms, tokens_in, tokens_out,
             cache_hit_tokens, actual_mc)

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
