"""service/llm_providers.py — WHO we call and WHICH credential we call them with.

Provider identity only: the base URL, the env var its key lives in, and how that
key is resolved. Everything about a CALL — models, pricing, cost accounting, cap
reservation, the request itself — is `llm_client`, which imports this.

It is its own module because `tenant_keys` needs the same two things (the list of
providers, and the deployment's own key for one) and `llm_client` imports
`tenant_keys` on the money path. With the registry in llm_client that was a cycle
papered over with function-local imports; split along the seam it already had, the
dependencies run one way: llm_client → llm_providers ← tenant_keys.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values, load_dotenv

_HERE = Path(__file__).resolve().parent
_ENV_FILE = _HERE / ".env"
# Mirror db.engine: load service/.env if it hasn't been loaded yet. override=False
# means a real value already in the process env (e.g. a host-exported key) wins.
load_dotenv(_ENV_FILE, override=False)


@dataclass(frozen=True)
class LLMProvider:
    name: str          # "grok" | "deepseek"
    base_url: str      # OpenAI-compatible base
    api_key_env: str   # env var holding the bearer key


# Both keys are CONFIRMED present in service/.env (verified 2026-06-04, 19 §4).
PROVIDERS: dict[str, LLMProvider] = {
    "grok":     LLMProvider("grok",     "https://api.x.ai/v1",      "GROK_API_KEY"),
    "deepseek": LLMProvider("deepseek", "https://api.deepseek.com", "DEEPSEEK_API_KEY"),
    # DeepInfra hosts the Qwen3-VL vision models (OpenAI-compatible, NSFW-permissive).
    "deepinfra": LLMProvider("deepinfra", "https://api.deepinfra.com/v1/openai", "DEEPINFRA_API_KEY"),
    # Z.ai (GLM). The base URL is the one the API actually answers on, NOT the
    # one its OpenAI-compatibility docs give: `https://api.z.ai/api/openai/v1`
    # returns `{"code":500,"msg":"404 NOT_FOUND"}` — under an HTTP **200**, so a
    # caller that trusts the status code sees a successful request with no
    # choices rather than a routing error. Probed 2026-08-27.
    "zai": LLMProvider("zai", "https://api.z.ai/api/paas/v4", "ZAI_API_KEY"),
}


def _api_key(env_name: str) -> str:
    """Resolve a provider key. A non-empty process-env value wins; if it's
    missing or blank we fall back to the COPYed service/.env directly.

    Why the file fallback: docker-compose passes the key through as
    `${GROK_API_KEY:-}`, which injects an EMPTY string into the container when
    the host shell doesn't export it. load_dotenv(override=False) then refuses
    to backfill the already-set (empty) var, so the real value sitting in the
    COPYed service/.env would be ignored. Reading the file here recovers it —
    the Phase-F clean-build footgun, defused.

    A value pasted into the Setup → Keys UI (secrets.json) wins over both, so a
    self-hoster can add a key without touching env/.env or restarting."""
    try:
        from secrets_store import stored as _stored
        s = _stored(env_name)
        if s:
            return s
    except Exception:  # never let the key store crash a provider call
        pass
    val = (os.environ.get(env_name) or "").strip()
    if val:
        return val
    try:
        return (dotenv_values(_ENV_FILE).get(env_name) or "").strip()
    except Exception:  # pragma: no cover — never let key lookup crash the caller
        return ""


def house_key(provider: str) -> str:
    """The DEPLOYMENT's own key for one provider ("" if it has none, including
    for a name that is not a provider).

    Public so callers ask for a provider's key rather than re-deriving which env
    var holds it — that mapping is this module's business.
    """
    prov = PROVIDERS.get(provider)
    if prov is None:
        return ""
    return _api_key(prov.api_key_env)
