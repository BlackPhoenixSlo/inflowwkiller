"""
_outbound.py — the send chokepoint every chat engine funnels a draft through.

WHAT THIS OWNS: the whole-draft guards, in order, between "the model produced
text" and "we start splitting it into bubbles" —

    guard_offplatform  →  verify_self_consistency  →  guard again, if it
                                                      rewrote  →  strip_emojis

Four engines (ai_chatter, of_ai_chat, autoreply, deep_convo) each hand-copied that
sequence, and it drifted, because "did someone remember to paste this line" is not
a mechanism. Measured before this module existed:

  guard                    ai_chatter  of_ai_chat  autoreply  deep_convo
  guard_offplatform            ✓           ✓           ✓          ✓
  verify_self_consistency      ✓           ✓           ✗          ✗
  strip_emojis                 ✗        gated       gated    UNGATED

`strip_emojis` is an ACCOUNT-WIDE operator setting, and it had three different
meanings depending on which engine happened to answer. That is the bug this module
makes structurally impossible: membership is now an argument at the call site, so
it is declared and reviewable rather than inferred from whether a line got copied.

Extracting it is what made the ai_chatter cell READABLE as a bug rather than as an
absence — 7 of 12 live accounts had the toggle on, so the operator had asked for no
emojis and the engine answering most of the messages was still sending them. Today:

  guard                    ai_chatter  of_ai_chat  autoreply  deep_convo
  guard_offplatform            ✓           ✓           ✓          ✓
  verify_self_consistency      ✓           ✓           ✗          ✗
  strip_emojis               gated       gated       gated    ALWAYS

deep_convo's cell is the one remaining divergence and it is deliberate: that engine
is emoji-free by mandate, independent of the account setting. It is declared at its
call site and pinned by test_outbound.case_every_engine_declares_its_guard_set.

WHAT THIS DELIBERATELY DOES NOT OWN: everything after the split — bubble
splitting, `apply_word_restriction`, the non-native layers. Those look duplicated
but are NOT: deep_convo applies non-native then word-restriction to the whole text
BEFORE splitting, autoreply splits first and applies word-restriction per bubble,
and ai_chatter has nine exits with different clipping. Forcing one order on them
would change what goes out on the wire, which is a behaviour change wearing a
refactor's clothes. If those are to converge it needs its own change, with its own
before/after on real drafts — not a quiet fold into this one.

Adding a guard: add it here, in order, with a flag. Then every engine gets it, or
consciously does not, in one visible place.
"""
from __future__ import annotations

import logging
import random
from dataclasses import dataclass

from db.models import Fan

from ._common import guard_offplatform, strip_emojis
from ._persona import verify_self_consistency

log = logging.getLogger("of-relay.automation.outbound")


@dataclass(frozen=True)
class ConsistencyCtx:
    """Everything the PHASE 2 pre-send check needs, bundled so `finalize_draft`'s
    signature does not grow four positional arguments that only matter when the
    check is on. Pass None to skip the check entirely — that is what an engine
    which does not implement it does, and what an opted-out account does.

    No `purpose` here on purpose: it is `finalize_draft`'s own argument. The
    verifier's purpose MUST be the calling engine's (it keys
    account_ai_config.model_by_purpose — see verify_self_consistency), so a second
    copy could only ever be either redundant or a bug someone had to introduce."""
    fan: Fan
    persona: str
    model: str
    last_inbound: str = ""


async def finalize_draft(
    text: str,
    *,
    account_id: str,
    fan_id: int,
    purpose: str,
    consistency: ConsistencyCtx | None = None,
    strip_emoji: bool = False,
) -> tuple[str, list[str]]:
    """Run the whole-draft guards and return `(text, leak_reasons)`.

    `leak_reasons` is empty when nothing was guarded. It is returned rather than
    only logged because ai_chatter acts on it — a guarded reply must not also
    carry a paid attachment — and swallowing it here would push that engine back
    into calling `guard_offplatform` itself.

    ORDER IS LOAD-BEARING:
      1. `guard_offplatform` on the model's draft. It is the deterministic floor
         under the prompt-level guardrail and must see the model's own words
         before any later pass can disguise them (word-restriction turning "meet"
         into "meeet" would hide a leak from it).
      2. The consistency check, which may replace the draft with a model-written
         rewrite.
      3. `guard_offplatform` AGAIN, but only on a rewrite. The floor exists
         because model output is not trusted, and step 2's fix is model output;
         letting it reach the wire unguarded would have left a hole exactly the
         width of the guard. Skipped when the verifier returned the draft
         unchanged, which is the common case (no contradiction ⇒ no rewrite), so
         this costs nothing on the path that matters.
      4. `strip_emojis` last, before the split, so the emoji-aware bubble logic
         downstream simply never sees them.

    The rng seed is `f"{fan_id}:{text}"` — identical to what every engine passed,
    so the deterministic deflection picked for a given draft does not change.
    """
    text, leak = _guard(text, account_id=account_id, fan_id=fan_id, purpose=purpose)
    if consistency is not None:
        fixed = await verify_self_consistency(
            account_id, fan_id, consistency.fan, text, consistency.persona,
            consistency.model, purpose, last_inbound=consistency.last_inbound)
        if fixed != text:
            fixed, refix = _guard(fixed, account_id=account_id, fan_id=fan_id,
                                  purpose=f"{purpose}/consistency-fix")
            # Merged, not replaced: a caller acting on `leak` (ai_chatter drops the
            # paid attach) must hear about a leak from EITHER pass. Order-preserving
            # de-dupe keeps the reason list readable when both fire on the same tell.
            leak = list(dict.fromkeys([*leak, *refix]))
        text = fixed
    if strip_emoji:
        text = strip_emojis(text)
    return text, leak


def _guard(text: str, *, account_id: str, fan_id: int,
           purpose: str) -> tuple[str, list[str]]:
    """`guard_offplatform` plus the log line, so the two passes above cannot
    drift in how they report. On a leak the guard REPLACES the draft with a
    deflection — the returned text is already safe to send."""
    text, leak = guard_offplatform(text, random.Random(f"{fan_id}:{text}"))
    if leak:
        log.info("%s off-platform leak guarded account=%s fan=%s reasons=%s",
                 purpose, account_id, fan_id, leak)
    return text, leak
