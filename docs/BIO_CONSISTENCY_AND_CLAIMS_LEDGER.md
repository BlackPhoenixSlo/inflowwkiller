# Creator-side memory: what she told the fan

**Status:** Phase 0, Phase 1 (Tier B) and Phase 2 shipped. Phase 3 designed, not built.
**Root incident:** the graded vault fans `514288063` / `142678649`, 2026-07-25.

---

## The problem in one paragraph

Every persistence mechanism in this system points at the **fan**. `fans` has 69
columns about him, `fan_profiles` holds a generated profile of him, `gen_info`
refreshes it, and `_build_messages` renders it into every prompt under *"What you
know about him"*. Nothing records what **she** said about **herself**. The reply
prompt carries only the last `_HISTORY_TAIL` bubbles, so in a long thread every
biographical answer she has ever given has scrolled out, and she re-answers from
scratch — differently each time.

## What the data actually showed

Measured against prod (8,050 fans / 604,522 messages), 2026-07-25:

| | |
|---|---|
| fans over 20 turns / 50 / 100 / 200 | 181 / 79 / 42 / 18 (max 1,096) |
| threads asking her biographical questions | 307 across 13 accounts (10.1%) |
| repeat questions with the earlier answer **out of the window** | **65 of 115 (56.5%)**, mean gap **127 messages** |
| re-asks that happened **inside** the window | **323 of 468 (69%)** |
| `persona_*_claimed` columns populated | **0 of 8,050** |

Those last two rows are the whole design brief. The 56.5% says a memory store is
genuinely needed. The 69% says a memory store **alone will not work** — the model
already ignores facts it can see. `fans.occupation = 'journeyman carpenter'` was
stored, injected, and used 20+ times on a $691 thread, and the model still asked
*"what do u do for wokr?"*, then doubled down with *"wtf u never told me what u
do 🙄"*. Storage was never that thread's problem.

So the fix is three layers, and the ledger is only one of them.

---

## The classification that drives the design

Not every self-fact belongs in the same place. Splitting them is what keeps the
ledger small enough to be reliable.

| kind | example | correct home | why |
|---|---|---|---|
| **account-constant** | age, nationality, home city, job, body, pets | `account_ai_config.persona` + `.location` | One true value for all 8,050 fans. Fixing it once fixes every thread retroactively. Storing it per-fan means 8,050 copies of one fact and no way to correct them. |
| **per-fan durable** | *"I grew up in Argentina, playing in the street with my cousins"* | **the claims ledger** | Improvised for this fan, must hold for the life of this thread. No config can pre-cover it. |
| **volatile** | *"I'm at Stave lake today"* | **nothing — deflect** | True for hours. Recording it makes things worse: it re-injects yesterday's location as authoritative tomorrow. |

**The consistency rule that matters is per-thread, not global.** Once she has
told JH she's in Chile, "correcting" her to Argentina mid-thread is *more*
damaging than staying wrong — the fan experiences the correction as the lie. The
persona keeps new threads right; the ledger keeps existing threads coherent.

---

## Phase 0 — shipped in this commit

No new schema, no new LLM calls, no per-fan state.

1. **`BIO_CONSISTENCY_GUARDRAIL`** (`_common.py`), wired unconditionally into all
   five chat builders (`of_ai_chat`, `ai_chatter`, `autoreply`, `deep_convo` ×2)
   beside the two existing guardrails. Note what it does **not** say: it does not
   forbid inventing. A persona can never cover every question, so improvising is
   allowed — the rule is that an improvised detail becomes *binding once said*.
   That is precisely what lets it compose with the ledger instead of fighting it.
2. **`persona_location_line()`** — `account_ai_config.location` is set on 10 of 12
   live accounts and was already rendered by `send_welcome` and `send_followup`,
   but **no chat engine read it**. The welcome message named a country the chat
   engine then had to guess at. Now pinned in all five prompts.
3. **`persona_register_age()`** — the `HOW YOU TEXT (a real 22yo girl…)` heading
   is a *register* instruction (text young and casual), not a claim about her age.
   It was hardcoded from Ava's default persona, so a persona saying 49 was still
   told to text like a 22-year-old. Now derived from the persona, with anything
   under 18 rejected outright.

Guarded by `service/tests/test_bio_consistency.py`. Every block is byte-absent
when its input is unset, so accounts with no location/age send an unchanged prompt.

## Phase 0.5 — config, not code (do before measuring anything)

**Six accounts are telling fans the wrong time of day**, and the clock block then
hard-instructs the model to defend it (*"never claim a different time of day"*).
This is not the model improvising; it is the model obeying a bad config row.

| account | location | bot thinks | truth | off by | set `timezone` to |
|---|---|---|---|---|---|
| `ACCOUNT_ID` | Argentina | −7h | −3h | **4h** | `America/Argentina/Buenos_Aires` |
| `267492960` | Tampa, Florida | −8h | −4h | **4h** | `America/New_York` |
| `571598796` | Miami, Florida | −8h | −4h | **4h** | `America/New_York` |
| `523982374` | Hawaii | −8h | −10h | **2h** | `Pacific/Honolulu` |
| `ACCOUNT_ID` | Colombia | −7h | −5h | **2h** | `America/Bogota` |
| `25166249` | Calli LA | −8h | −7h | 1h | `America/Los_Angeles` |
| `ACCOUNT_ID` | Ljubljana | +2h | +2h | 0h (DST-fragile) | `Europe/Ljubljana` |

IANA wins over the legacy `utc_offset` in `rhythm.tz_offset_for`, so setting
`timezone` is sufficient — leave `utc_offset` alone. `523982374` is the #3 account
by revenue and owns four of the seven longest threads in the DB.

**Also backfill the personas.** the graded vault's persona says *"Born and raised in
Argentina"* with **no city** — that is the exact hole the Argentina → Chile →
Córdoba cascade fell through. Every account needs: home city, current living
situation (alone / roommate / family), and a two-line upbringing. This is a data
edit in a UI field and it retroactively covers all 1,597 of that account's fans.

---

## Phase 1 — the claims ledger (MVP)

### Source of truth: `messages`, not the generation path

This is the single most important design choice, and it is what makes the MVP
cheap instead of dangerous. Capture from the **persisted outbound message rows**,
never from the LLM response object.

Every outbound message — automation *and* human chatter — is written by
`write_outbound_attribution` after OF returns 200. `_gather` already reads that
stream filtered on `is_unsent == False`. Sourcing claims from `c.messages` gets,
in one decision:

| landmine | why it evaporates |
|---|---|
| human chatters bypass every LLM path | their rows are in `messages` like any other |
| rhythm defer / autoreply race discards a generated draft | no OF 200 → no row → nothing captured |
| `unsend_messages` retracts a 1:1 AI message | sets the same `is_unsent` flag `_gather` already filters |
| one generation → up to 7 bubbles | capture once per fan per sweep, not per bubble |
| job retry re-runs the candidate loop | capture is idempotent (dedup by topic) |

Capturing at the generation hook instead would leave the ledger **blindest exactly
where the money is** — `_SPEND_GATE_CENTS = 100` hands fans past $1 to human
chatters, whose claims would never be recorded. A ledger that is confidently
incomplete on paying threads is worse than no ledger.

### Extraction: zero extra LLM calls

`_extract_messages` already renders **both directions** — `f"{'FAN' if d == 'in'
else 'YOU'}: {b}"`. The extract call has been looking at her own outbound lines
all along; it was simply never asked about them.

- Add claim keys to `_EXTRACT_SYSTEM`. ~+40 input tokens on a call that already fires.
- **Required gate fix:** `_extract_worthwhile` currently skips the call once the
  *fan's* profile is saturated — i.e. deep into a long thread, exactly where
  contradictions live. It must also return `True` while any claim slot is empty.
  Bounded: once they fill, the gate re-closes.

### Storage

- The three existing dead columns (`persona_age_claimed`, `persona_location_claimed`,
  `persona_job_claimed`) for the stable trio. Already migrated, already serialized
  in `fans.py`, already PATCH-editable — the correction path ships for free.
- `fans.persona_claims_json` (new TEXT column) for free-text claims:
  `[{"topic": "upbringing", "claim": "...", "at": "...", "message_id": N}]`.
  Accrete → dedup by `topic` → cap 20, cloning `gen_info`'s `recent_events` pattern.

**Latest-wins, not first-wins.** `_extract_and_fill`'s fill-empty-only semantics
are right for *fan* facts but wrong here: a bad first extraction would be permanent
and a persona edit would never propagate. Follow `_mark_question_asked`'s escalating
precedent instead — it already writes `key` then `key:2`.

### Injection

Into the **user** message, beside the facts block — that message is per-fan and
never prefix-cached, so this costs nothing beyond its own tokens. Putting it high
in the *system* prompt would fragment the shared cached prefix per-fan and cost
~3× more than the block itself.

Framed as an imperative, in the clock's voice — *"YOU HAVE ALREADY TOLD HIM: …"* —
not as another passive inventory. The passive *"What you know about him"* list is
the one the model demonstrably ignores; the imperative clock line is the one that
worked.

**Cost:** ~150 tokens ≈ +0.21 millicents/reply (**+5%** on a ~4mc reply). House-wide
that is roughly **$15/year**. For contrast, raising `_HISTORY_TAIL` from 20 to 100
costs **+47% to +137%** per reply and *still* misses the mean 127-message gap.

### Scope

Two engines only: `of_ai_chat` and `ai_chatter`. `of_ai_chat` caps at
`_MAX_TURNS = 30` / `_MAX_FAN_MESSAGES = 10` / $1 spend, so the long threads this
exists for live in `ai_chatter`, which has **no turn cap at all**. Ship flagged
off; enable on Ava + the second account (real payers) before house-wide.

---

## Phase 2 — the pre-send consistency check

The guardrail states the rule and the ledger supplies the answers, but neither
*verifies* the draft. Given the 69%-inside-the-window finding, verification is the
layer most likely to actually move the number.

**Do not run an LLM check on every reply.** Most replies say nothing about her.
Gate it on a deterministic prefilter — a new `mentions_self_claim(text)` in
`_common.py`, matching first-person + biographical keywords (age / born / grew up /
live / from / job / work / boyfriend / family / city / years old, plus the Spanish
equivalents, reusing the `norm_text` discipline). Expected fire rate: low single-digit
percent of replies, since bio-interrogation is 10% of threads and only some turns
inside those threads make a claim.

When it fires:

1. Send draft + persona + location + ledger to a cheap structured call
   (`temperature=0`, `json_object`) → `{"contradicts": bool, "which": "...", "fix": "..."}`.
2. Clean → send.
3. Contradiction → **one** regeneration with the conflict named explicitly, then send.
4. Second failure → fall back to a deflection rather than shipping a known
   contradiction. Never block the reply entirely; never surface "I don't remember".

Cost lands only on the small gated slice, so the blended per-reply cost stays
near Phase 1's +5%.

## Phase 3 — compaction (only if the ledger grows)

**Do not route this through `gen_info`.** `gen_info._trim_messages` deliberately
drops every outbound line (*"V1 fed Grok the FAN side only and produced sharper
profiles"*). Feeding outbound lines back in changes the input to every existing
profile field and forces a full re-baseline of `test_gen_info.py` and
`test_gen_info_richer.py`. If compaction is ever needed, run it as a separate
periodic pass over `persona_claims_json` that merges and dedups but never invents
and never drops.

---

## What would prove this worked

Re-run the contradiction scan after Phase 0 + 0.5 land, before building Phase 1:

- **repeat-question pairs with a >20-message gap** (baseline: 65 of 115)
- **re-asks inside the window** (baseline: 323 of 468) — if a correct age, a correct
  clock, a complete persona and an explicit consistency rule don't move this, the
  model cannot honor injected facts and Phase 1 should be reconsidered
- **bot-accusation threads** (baseline: 70 across 12 accounts)

Phase 0.5 is free and retroactive across all 7,983 fans. Phase 1 is ~6 files and
$15/year but only helps the ~79 long-thread fans going forward. Measure between
them — the ordering is the point.

## Known open question

The ~2.9%-of-revenue cohort (`turn_counter > 50`) converts at 53% vs 27%, but the
correlation is at least partly tautological: spend keeps a fan in the funnel, which
accrues turns. the graded vault — the account with the worst contradictions and the most
quotable trust collapse — converts at **0.69%** for **$127.75 lifetime total**. The
honest claim for the ledger is *"protects revenue already in flight"*, not *"grows
revenue"*. Phase 0 and 0.5 are cheap enough not to need that argument; Phase 1
should be judged against the re-measured numbers above, not against the exhibits.


---

## Tier B as built (2026-07-25)

`fans.persona_claims_json` — `[{topic, claim, at}]`, latest-wins per topic, cap 20,
plus the three `persona_*_claimed` scalars as the fast path for the topics fans ask
most. Injected into the **user** message of `of_ai_chat` and `ai_chatter` as
*"YOU HAVE ALREADY TOLD HIM…"*. `""` for a fan with no deviations.

**Deviations-only** is enforced by handing the extractor the canon: it is told to
return an empty list when everything she said already matches her profile. Verified
live — replaying a real thread where she wrote *"i am from argentina"* against an
Argentine persona recorded **nothing**, correctly.

**The cost gate.** The extract call is skipped once the fan's profile saturates
(~43% of the ai_chatter lane was that waste). Claims are open-ended and would never
saturate, so the gate is instead `mentions_self_claim()` — a deterministic EN/ES
prefilter over HER outbound lines only. Measured on 16,563 real 1:1 outbound
messages from the graded vault: **5.06% fire rate**. So the second call returns on about one
reply in twenty, not on all of them.

Live-replayed against the actual Chile cascade, it captured all four deviations:

    nationality        soy argentina de verdad
    current location   estoy acá en Chile
    hometown           en córdoba, cora
    living situation   vivo sola en un depto chiquito, con mi gata

### Known limitation, worth reading before Phase 3

That output is the feature working *and* its ceiling. Those four claims are mutually
contradictory, and the ledger presents them all as *"true for him now"* because the
contradictions were already made — the ledger stops the NEXT one, it cannot resolve
the ones already in the thread. That is the "a claim already made cannot be
retracted" property, showing up as prompt noise.

Topic keys are model-authored free text, so semantically overlapping topics
(`current location` vs `hometown`) do not collapse under latest-wins. Phase 3
compaction is what should merge them — and for an already-poisoned thread the
honest answer may be an operator-visible conflict flag rather than an automatic
merge, since picking a winner mid-thread is the move that reads as the lie.


---

## Phase 2 as built (2026-07-25)

`_verify_self_consistency` sits at the same send chokepoint as `guard_offplatform`
— that guard stops her leaking a phone number, this stops her contradicting
herself. The verifier returns the **remedy** as well as the verdict, so one call
yields both, and the fix is written in her voice and her language where a canned
deflection would be neither. Rewriting outbound text there is established
practice, not a new idea.

**OFF unless explicitly enabled**, per account, via
`style_config_json.consistency_<automation>`. It deliberately does **not** route
through `_resolve_style_flag`: that helper falls back to `_STYLE_DEFAULT_ON`,
which carries `ai_chatter: True`, so it would have switched a paid second call on
for every ai_chatter account the moment it shipped. Caught by the first test run.

### The gate — two triggers

| trigger | fires on | rate |
|---|---|---|
| A — her own first-person claim | `mentions_self_claim(draft)` | 1.51% |
| B — his biographical question | `asks_about_her(last_inbound)` | 0.65% |
| **combined** | | **2.09%** |

Measured over **127,260 real 1:1 outbound messages** across four accounts.

Trigger B exists because the first version missed a real case live: `"en córdoba,
cora 😅"` answering `"¿y en qué parte vivís?"` states her city while matching no
first-person pattern — and that was step three of the Chile cascade. An elliptical
answer carries its claim in the exchange, not the sentence.

### Fails open, everywhere

Cap, timeout, junk JSON, and a `contradicts: true` with no usable `fix` all return
the draft untouched. It sits between generation and send, so a verifier that can
silently swallow a reply is a worse failure than the contradiction it exists to
catch.

### Live results, real model, real canon

    HE: y en que parte vivis    SHE: en córdoba, cora 😅        → en buenos aires, cora 😅
    HE: vos sos chilena o no    SHE: y sí, estoy acá en Chile   → y sí, estoy acá en Buenos Aires
    HE: cuantos años tenes      SHE: 20 nomás 😏                 unchanged (agrees with canon)
    HE: que lindo               SHE: jaja siempre tan cuidado…   no call

`nope i'm 22 actually` → `nope i'm 20 actually` — Exhibit B's age flip, caught.
Note the fixes preserve the emoji, the nickname and the language: the prompt asks
for the smallest possible edit and forbids apologising or correcting herself out
loud, because a visible correction reads worse than the contradiction did.
