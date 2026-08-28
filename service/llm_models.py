"""service/llm_models.py — WHICH models exist, what they cost, and how each one
states its reasoning mode on the wire.

Split out of `llm_client` for the same reason `llm_providers` was: that module
is HOW to make a call, and this is WHAT there is to call. The seam is clean in
one direction — nothing here imports the client — so the dependencies stay
llm_client -> llm_models, with no cycle to paper over.

The registry holds WIRE FACTS, deliberately. `api_model` is the provider's real
model name; `reasoning_body` is the literal body fragment that states the
reasoning mode. Neither is a token that some branch elsewhere translates into
the real thing, because that indirection is exactly what let a model describe
itself one way and behave another — see the 2026-07-24 note below.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

# Frozen empty default. A dataclass field may not default to a mutable literal,
# and a shared read-only mapping is cheaper than a default_factory per model.
_NO_REASONING: Mapping[str, Any] = MappingProxyType({})


def _body(**keys: Any) -> Mapping[str, Any]:
    """A read-only reasoning body fragment.

    Read-only at the top level only — the NESTED values stay plain dicts on
    purpose, because a `MappingProxyType` is not JSON-serializable and would
    fail on the way to the provider. Callers must therefore take a copy rather
    than splice this straight into a request; `LLMModel.reasoning_keys()` is
    that copy, and is the only supported way to read this.
    """
    return MappingProxyType(dict(keys))


# ── Model registry ──────────────────────────────────────────────────────
# Which models exist, what they cost, and which provider (llm_providers) serves
# each. DB-backed model_pricing is a later migration (19 §3); this is the seed.

@dataclass(frozen=True)
class LLMModel:
    id: str                           # OUR internal id (audit + config key)
    provider: str                     # -> LLMProvider.name
    # The name the PROVIDER expects on the wire, when it differs from our id
    # (DeepInfra publishes vendor-prefixed paths like "Qwen/Qwen3-VL-30B-…").
    # Empty → send `id` verbatim, which is the case for every model whose
    # provider names it the way we do. NEVER encode a MODE here: `reasoning_body`
    # below is the single source of truth for reasoning on/off — see the note
    # above MODELS for the outage that rule was written from.
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
    # The body keys that state this model's reasoning mode, VERBATIM. Empty =
    # the model has no reasoning control at all.
    #
    # This is the wire contract itself rather than a token some branch turns
    # into one, and that is the whole design. The predecessor carried three
    # fields — a `thinking` bool, a `reasoning_control` discriminator and the
    # effort — which between them admitted about thirty combinations of which
    # four were legal. One of the illegal ones shipped: glm-5.3-flash, a model
    # whose defining property is that it CANNOT stop reasoning, was recorded as
    # `thinking=False`. Nothing read it, so nothing complained. Holding the body
    # makes that state unrepresentable instead of merely unread.
    #
    # Providers do NOT agree on the shape, and not even within one provider:
    # glm-5.3-flash 400s on the `thinking` object that glm-4.5-air requires, and
    # accepts the `reasoning_effort` string that glm-4.5-air takes with a 200 and
    # then ignores. So this is per MODEL. Never state a mode by omission — see
    # the 2026-07-24 note above MODELS for what that costs.
    reasoning_body: Mapping[str, Any] = _NO_REASONING
    # Reasoning efforts an operator may pick for this model, WEAKEST FIRST
    # (the editor offers [0] as the default on a fresh pick). Empty = no effort
    # string travels for this model, whatever its provider accepts elsewhere.
    # Per model, not per provider, for the reason above: two models on one
    # provider disagree about whether the field is read at all.
    effort_choices: tuple[str, ...] = ()
    # The effort used when no caller and no account config says otherwise.
    # Pinned here rather than defaulted in `chat()`, because ~30 call sites
    # never pass one and a model inheriting a default chosen for a different
    # provider would run at a setting nobody measured for it. Must be one of
    # `effort_choices` — asserted below.
    reasoning_effort: str = ""
    # May this model be handed an image? A POLICY flag, not a capability claim:
    # it says which models the image paths are allowed to route to, and every
    # describe entry point checks it. Off by default so a newly registered
    # model is text-only until someone deliberately clears it for vision —
    # the Brain dropdown is derived from MODELS.keys(), so anything added here
    # is offerable to an operator the moment it lands.
    vision_ok: bool = False

    def reasoning_keys(self) -> dict[str, Any]:
        """A FRESH copy of the reasoning body fragment, safe to splice into a
        request body.

        The copy is the whole point. These structures live for the life of the
        process and are shared by every account, so handing out the originals
        would mean one mutation of one request body — by any future redactor,
        middleware or retry that decided to normalise something in place —
        silently rewriting the reasoning mode of every subsequent call on the
        deployment. Nothing does that today. This makes it so that nothing can.
        """
        return copy.deepcopy(dict(self.reasoning_body))

    def wire_model(self) -> str:
        """The model name to put on the request body (provider's real name)."""
        return self.api_model or self.id


# Prices are cents per 1k tokens (per 1M ÷ 10). Enough to stop a runaway loop
# via the cap; a DB-backed model_pricing table later overrides these seeds.
#
# DeepSeek RE-PRICED on 2026-08-16 and the seeds here did not follow for twelve
# days, so the cap was metering DeepSeek at a fifth of its real rate — the same
# under-count the Qwen block below was written from, on the models that carry
# almost all the traffic. It also replaced flat pricing with PEAK/off-peak
# (off-peak is half). Seeded at PEAK deliberately: the cap's job is to be
# impossible to overrun, so it must assume the dearer hour. That over-states
# off-peak spend by 2x, which is the harmless direction — the opposite mistake
# is the one that lets a sweep run past a limit that was never real.
#
#   v4-flash: $0.44 miss / $0.014 hit in · $1.32 out → 0.044 / 0.0014 / 0.132
#             (was $0.14 / $0.0028 / $0.28 before the re-price)
#   v4-pro:   $1.32 miss / $0.044 hit in · $3.96 out → 0.132 / 0.0044 / 0.396
#
# Cache hits are still ~30x cheaper than misses, and with long stable system
# prompts the bulk of input is a cache hit, so the cost path bills the hit
# portion at the hit rate. Grok-4.1-fast: ~$0.20/1M in, ~$0.50/1M out.
#
# DeepSeek model names: our ids ARE the wire names, so no api_model remap. They
# once weren't — until 2026-07-24 the mode was picked by NAME ("deepseek-chat" =
# thinking off, "deepseek-reasoner" = thinking on). DeepSeek retired both
# aliases with no notice and every call started 400-ing ("supported API model
# names are deepseek-v4-pro or deepseek-v4-flash"), which killed every LLM
# automation on every account for 35 minutes. Encoding a MODE as a model name
# gave us two sources of truth for one bit. `reasoning_body` is now the only
# one, and it is the literal body fragment rather than a token something else
# translates — so there is no second place for the two to drift apart. The
# request builder splices it verbatim and never infers a mode.
MODELS: dict[str, LLMModel] = {
    "grok-4-1-fast-non-reasoning": LLMModel(
        "grok-4-1-fast-non-reasoning", "grok",
        input_per_1k_cents=0.02, output_per_1k_cents=0.05,
    ),
    "deepseek-v4-flash": LLMModel(
        "deepseek-v4-flash", "deepseek",
        input_per_1k_cents=0.044, input_cache_hit_per_1k_cents=0.0014,
        output_per_1k_cents=0.132, reasoning_body=_body(thinking={"type": "disabled"}),
    ),
    "deepseek-v4-pro": LLMModel(
        "deepseek-v4-pro", "deepseek",
        input_per_1k_cents=0.132, input_cache_hit_per_1k_cents=0.0044,
        output_per_1k_cents=0.396,
        reasoning_body=_body(thinking={"type": "enabled"}),
        effort_choices=("low", "medium", "high"), reasoning_effort="high",
    ),
    # Qwen3-VL vision (DeepInfra). Cheap MoE primary + big escalation.
    #
    # Prices READ FROM `https://api.deepinfra.com/models/list` on 2026-08-05
    # (`pricing.cents_per_input_token` x1000 = cents per 1k), not estimated. The
    # seeds they replace were wrong in BOTH directions and had been since the
    # models were added: 30b was billed at 0.01/0.04 against a real 0.015/0.06,
    # so every describe and every flags call — the entire vision spend — was
    # metered ~35% LOW against the daily cap, and 235b at 0.03/0.12 against a
    # real 0.02/0.088 made the escalation look pricier than it is. A cap that
    # under-counts is the failure mode that matters: it is the only thing
    # standing between a sweep and an unbounded bill.
    "qwen3-vl-30b": LLMModel(
        "qwen3-vl-30b", "deepinfra", api_model="Qwen/Qwen3-VL-30B-A3B-Instruct",
        input_per_1k_cents=0.015, output_per_1k_cents=0.06, vision_ok=True,
    ),
    "qwen3-vl-235b": LLMModel(
        "qwen3-vl-235b", "deepinfra", api_model="Qwen/Qwen3-VL-235B-A22B-Instruct",
        input_per_1k_cents=0.02, output_per_1k_cents=0.088, vision_ok=True,
    ),
    # GLM-5.3-Flash (Z.ai). Registered for CHAT only — see `vision_ok` above.
    # The model does accept `image_url` parts and described test images
    # correctly when probed (2026-08-27); leaving the image paths on Qwen is a
    # scope decision, not a limitation of this model.
    #
    # Priced at LIST — $0.15 miss / $0.03 hit in · $0.50 out per 1M — NOT at the
    # $0.075/$0.015/$0.25 on z.ai's pricing page today. That is a 50% promotion
    # which the page itself says ends 2026-09-09, and a seed that expires is a
    # cap that starts under-counting by 2x on a date nobody will be watching.
    # Erring high only makes the cap stricter, which is the safe direction; the
    # comment above the Qwen block is the argument, and it was written from a
    # real 35% under-count. Note the shape against DeepSeek flash: the MISS rate
    # is comparable but the HIT rate is ~10x DEARER, and on the long stable
    # system prompts these lanes send almost all input is a cache hit.
    #
    # This model ALWAYS reasons — "This model always engages in thinking and
    # cannot be disabled" — so `reasoning_effort` is the only control it has and
    # sending it is mandatory. Pinned to "low" because that is the setting the
    # latency was measured at: p50 1822ms on a production-shaped prompt, level
    # with the incumbent flash model. Omitting the field cost 327 reasoning
    # tokens and 8.9s on the same prompt.
    "glm-5.3-flash": LLMModel(
        "glm-5.3-flash", "zai", api_model="glm-5.3-flash",
        input_per_1k_cents=0.015, input_cache_hit_per_1k_cents=0.003,
        output_per_1k_cents=0.05,
        # No `reasoning_body`: on this model the effort string IS the mode, and
        # the `thinking` object is a hard 400 for every value.
        effort_choices=("low", "high", "max"), reasoning_effort="low",
    ),
    # GLM-4.5-Air (Z.ai). The FAST, genuinely non-reasoning chat model, and the
    # reason `reasoning_control` is per-model: this one takes the `thinking`
    # object and honours it both ways (reasoning_content 565 chars enabled, 0
    # disabled), while its 5.3 sibling above 400s on that same field.
    #
    # It also accepts `reasoning_effort` with a 200 and ignores it completely —
    # asked for "none" it returned ~15k characters of reasoning. That is the
    # `enable_thinking:false` failure again: a request that looks like it worked.
    # Nothing here may state the mode by omission either; sending neither field
    # left it reasoning too.
    #
    # Measured 2026-08-27 on a production-shaped prompt, thinking disabled, n=10:
    # p50 759ms against 1822ms for glm-5.3-flash at effort=low and ~1825ms for
    # the incumbent deepseek-v4-flash. Cache reporting is excellent (1332 of
    # 1333 input tokens on a repeat), JSON mode works with reasoning off, and 5
    # explicit chat-lane prompts drew 0 refusals.
    #
    # Prices per 1M: $0.20 miss / $0.03 hit in · $1.10 out. On a measured call
    # (1333 in, 99.9% cached, 14 out) that is 0.0056c against 0.0047c for
    # glm-5.3-flash at list and 0.0008c for the incumbent deepseek-v4-flash.
    #
    # So the honest framing is: ~18% dearer than the other GLM, but only ~1.5x
    # the incumbent at PEAK since DeepSeek's 2026-08-16 re-price — not the ~7x
    # it would have been against the old rates. What decides it is the
    # cached-input rate, because our system prompts are long and stable and
    # ~99.9% of input is a cache hit: $0.03/M here against DeepSeek's $0.014/M
    # peak. Headline cache-MISS prices barely apply to this workload.
    "glm-4.5-air": LLMModel(
        "glm-4.5-air", "zai", api_model="glm-4.5-air",
        input_per_1k_cents=0.02, input_cache_hit_per_1k_cents=0.003,
        output_per_1k_cents=0.11,
        # No `effort_choices`: z.ai accepts the string on this model and ignores
        # it, so offering it would be offering a setting that does nothing.
        reasoning_body=_body(thinking={"type": "disabled"}),
    ),
}

# The pin must be offerable. A model that defaults to an effort it does not
# list is one whose dropdown and whose behaviour disagree, and this is the cheap
# place to find that out — import time, not the first fan of the day.
for _mid, _m in MODELS.items():
    assert not _m.reasoning_effort or _m.reasoning_effort in _m.effort_choices, (
        f"{_mid}: pinned reasoning_effort {_m.reasoning_effort!r} is not in "
        f"effort_choices {_m.effort_choices}")
