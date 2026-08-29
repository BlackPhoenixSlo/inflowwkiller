"""service/assistant_api.py — POST /admin/assistant/ask: the in-product help bot.

Agency staff ask "how do I make autopost?", "can I do autostories?" and get back
WHERE in the UI, WHAT to configure, and an honest "not supported" when that is
the truth. Design record: plans/help-chatbot/README.md (2026-08-28).

CLOSED-BOOK, by design. There is no retrieval, no embedding store and no
graph — `assistant_manual.md` is stuffed whole into the system prompt and the
model is told to answer ONLY from it. A wrong "yes you can" is worse than a
"not supported", so the LLM is allowed to EXPLAIN features and never to DECIDE
whether one exists. Three things enforce that: the closed-book instruction, the
refusal few-shots below (this repo's documented lesson is that an EXAMPLE beats
a rule), and the drift gate — which diffs the manual's front-matter `kinds:`
list against the LIVE automation registry and appends a stub sentence for every
kind the prose has not caught up with. That gate is why a newly-registered
automation makes the bot say "it exists, docs haven't caught up" instead of
confidently inventing a click path.

THE SYSTEM PROMPT IS BYTE-STABLE — built once, cached, no timestamps, no account
names, sorted drift lists. ~28k tokens of manual ride on every call (median
`tokens_in` 28,418 over 228 logged calls; 98.6% median cache hit), so the cost
model is entirely the provider's PREFIX CACHE; one varying byte at the front of
the prompt busts it and multiplies input cost. ALL per-request text (the
question, the derived role, the LIVE STATE block) goes in the USER message.

WHICH model answers is an operator setting, not a constant here: the route asks
`_common.resolve_model` under the purpose `help_assistant`, and an operator pins
it per account in the Brain panel (Automations → Brain → Per-purpose overrides).
It does NOT follow the account brain, which is the one way this route differs
from every lane: `_common.PURPOSE_DEFAULT_MODELS` holds it on DeepSeek between
that pin and `cfg.model`. Because of the cached-input economics above, the
cheapest model on a short chat turn is not the cheapest one here, and when all
seven live accounts were switched to glm-5.3-flash the brain would have taken
this route with them at roughly 2.8x — with nobody choosing it. That multiple
is NOT a price difference: replayed through `_cost_millicents`, glm-5.3-flash at
this route's own 98.6% cache rate is 0.77x DeepSeek, i.e. CHEAPER. It is a
CACHING difference. Live GLM calls hit cache 9% of the time (36 of 400), so a
route whose whole economics is a 98.6% hit would land near the no-cache figure,
2.75x. Re-measure that assumption before trusting the pin, not the price list.

v2 (plans/help-chatbot/05-v2-live-state.md): the asking account's LIVE STATE —
rule switches + last runs, the config-only Enabled flags, the paused bit — rides
the user message so "is my welcome actually on?" is answered from rows, not
prose. Local DB only, ~1s budget, and any fetch failure degrades to a docs-only
answer with an honest note. See the state section below.

v3 (plans/help-chatbot/06-v3-fan-why.md): the operator pastes a chat header, an
@u handle and a model name and asks "why didn't AI chatter reply to him?". A FAN
EVIDENCE PACK rides the same user message — stored rows marked PROVEN, live
numbers marked CURRENT-STATE, and the model is taught to state the first as fact
and rank the second as hypotheses. Still ONE llm call, no tabs, no tool loop.
Both blocks are LOCAL reads that degrade independently; each failure appends its
own honest note rather than an error.

Neither block lives here. `assistant_state` reads the account's rows,
`assistant_fan_pack` resolves who a question is about and reads the fan's, and
`assistant_evidence` turns a typed record into text — the seam between the last
two is the database, so every line of the pack is testable without one. THIS
file is the HTTP layer: the prompt, the drift gate, the route, and no
diagnosis at all.

  POST /admin/assistant/ask  {question, account_id} → {answer, error}

`error` is a machine tag for the log (null on success); `answer` is ALWAYS a
human sentence — a used-up cap or a missing agency key comes back 200 with a
friendly string, not a 500, because the bot looking broken exactly when someone
needs config help is the failure mode this endpoint exists to avoid.

AUTH — the role comes from the SERVER. `chatters._CHATTER_BLOCKED_ADMIN_PREFIXES`
is a default-ALLOW blocklist, so this new /admin route is reachable by chatter
sessions, and a role read out of the request body would be client-supplied
theater. The DECISION here is that chatters may ask (their questions are as real
as an owner's); `_asker_role` derives who they are from the session ContextVars
and the prompt then answers admin-tab questions with "ask an owner/admin", per
the manual's Roles section. Access itself is not this file's job —
`assert_account_owned` plus the account-isolation middleware own that.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body
from pydantic import BaseModel, Field

import assistant_fan_pack
import assistant_state
import llm_client
from auth import assert_account_owned, get_request_user
from automations._common import resolve_model  # the one model resolver
from chatters import get_request_chatter
from llm_client import LLMCapExceeded, LLMConfigError, LLMHTTPError

log = logging.getLogger("of-relay.assistant_api")

router = APIRouter()

MANUAL_PATH = Path(__file__).with_name("assistant_manual.md")
PURPOSE = "help_assistant"          # the tag every call is logged under in grok_calls
MAX_QUESTION_CHARS = 1000


# ── The prompt's fixed head ───────────────────────────────────────────

_INSTRUCTIONS = """\
You are the help assistant for this platform. Answer ONLY from the manual below.

Rules:
- If a feature or setting is not in the manual, say it is not supported and name
  the closest supported feature. Never invent a tab, toggle, or knob name.
- Answer with WHERE in the UI it lives and WHAT to set, in a few short
  sentences. Use the manual's own names for tabs, cards and switches.
- If the asker is a chatter and the answer lives on an admin-only tab
  (Automations, Growth, Vault, Setup, or an owner-only Settings tab), say what
  the feature does and tell them to ask an owner or admin to set it up — do not
  hand them a click path they cannot follow.
- Never guess. "I don't know, that isn't in the manual" is a correct answer.
- A "LIVE STATE" block may follow the question. It is ground truth for the
  asking account right now, and where it conflicts with the manual it WINS —
  the manual describes the product, the block describes this account. When an
  answer draws on it, say so: "On this account right now, …".
- SAY WHICH ACCOUNT, AND WHICH FAN. The LIVE STATE block opens by naming its
  account and the fan pack opens with a Subject line, because a paste can move
  either one off the model on screen. Echo them: name the account whenever the
  question named a model, and name the fan id whenever you diagnose one. An
  answer that is silently about someone else looks exactly like a right one.
- An account may hold MORE THAN ONE rule of the same kind, and the block then
  shows two lines for it, each stamped with its rule id and name. Never answer
  from one of them. Say there are two, give both switch positions and both ids,
  and say which one is actually doing the work. "It's on" about a kind that is
  also off somewhere is a confident wrong answer.
- A "FAN EVIDENCE PACK" may follow it, for a question about ONE fan. Its two
  kinds of line are not interchangeable:
  * PROVEN lines are stored rows. State them as fact, with their stored reason
    and their date.
  * CURRENT-STATE lines describe how things stand NOW. Turn them into RANKED
    hypotheses — "most likely …, because …" — and end each with how to confirm
    it: name the tab or page to open.
- THEN vs NOW. Unless a PROVEN row covers the moment being asked about, say
  plainly that the historical cause cannot be proven, then pivot to what holds
  today. "Why did he get that mass on the 25th", with no send row, is
  "unknown — here is what would include him now". (Dated on purpose: a question
  naming a WEEKDAY has its own rule below, and two examples answering one string
  two ways is the condition that made this exact case flake.)
  When a PROVEN row DOES cover it — a thread beat carrying "mass_run <id>" with
  an "audience at send time" — that run id and that stored audience ARE the
  answer, and both go in the reply. COVERS IT means the same day the question
  named. A send from a different day does not answer a question about this one:
  say the named day has no row, then offer the nearest send WITH ITS DATE and
  let the asker decide. Substituting a send from another day, dated correctly
  and presented as the answer, is the same wrong answer as inventing one. An "audience as configured today" line is
  never why a past send happened; it may appear only under an explicit "today"
  label, after the stored one. Presenting today's switches as last week's reason
  is the wrong answer this rule exists to stop, and it reads exactly like a
  right one.
- There is no per-tick decision log. Never send anyone to Automation runs, or to
  any log, to find out why ONE fan was or was not picked — that page records
  runs, not fans. Say the trace does not exist, then name what CAN be checked.
- WHAT YOU CAN AND CANNOT WORK OUT ABOUT TIME. In the FAN EVIDENCE PACK every
  timestamp arrives as a stamp AND an age — "2026-08-28 04:02:48 (19h ago)" — so
  from one of those, and only from those, today is recoverable and "yesterday"
  or "last week" are answerable directly. The LIVE STATE block is not like that:
  its run lines carry an age with no date ("last run 6h ago") and its paused line
  carries a date with no age ("OF session dead since 2026-08-20"). Never read the
  second as a clock — it is when a session died, which may be months back. With
  no pack in front of you, you do not know today at all. Two things you can never
  do:
  * NAME A WEEKDAY, or resolve one. Nothing here says which day of the week any
    date fell on, so "last Tuesday" is not a date you can work out. A weekday
    you worked out yourself is how a send from one day becomes the answer about
    another. When the question names a weekday, ASK WHICH DATE FIRST — the
    question is the answer's opening line, before any run you go on to offer.
    A caveat further down is read after the operator has already believed the
    first paragraph.
  * TREAT THE MANUAL'S "checked against the running product on …" LINE AS TODAY.
    That is when the manual was last verified, and it drifts further from today
    with every day that passes. The stamp-plus-age pairs are the live clock; that
    sentence is a changelog.
- NEVER name a row the pack marks as not blocking FOR THE ENGINE IT NAMES. An
  exempt skip reason is not why AI Seller is quiet, and an expired pause is not
  why anything is quiet — saying either would be a confident wrong answer. But
  "not blocking AI Seller" is not "not blocking": the pack says which other
  senders a row still stops, and that half is just as much the answer.
- If the pack says the fan is AMBIGUOUS or NOT FOUND, ask for the one piece you
  need and diagnose nothing.
- For next steps and the operator's own words, use the manual's "I turned it on
  and nothing happened" checklist and its "Stopping the bot on one fan"
  section. Neither lists the raw reason strings, so take a row's MEANING from
  the pack line that carries it, and the WORDING from the manual."""

# EXAMPLE beats the rule — a refusal the model can imitate is worth more than
# another sentence telling it to refuse. Both cases below are the ones staff
# actually ask, and both are documented "NOT SUPPORTED" entries in the manual.
_FEWSHOTS = """\
Examples of the expected behaviour:

Q: "Can fans get an automatic DM on their birthday?"
A: Not supported — there is no birthday trigger. Closest: 👋 Nudge Online
(Automations → Ready-made posts & broadcasts) messages a fan when they come
online, and Settings → Scheduled holds one-off 1:1 messages you time yourself.

Q: "Can I auto-deliver customs?"
A: Not supported — customs delivery is manual by design. The product is a voice
note a person records and sends. What Fastt does is notice a $50+ tip burst,
mark the fan owed, stop the bot selling to him, and put the debt on the
**Customs** page for a human to clear.

Q (asker is a chatter): "How do I turn on the AI seller?"
A: AI Seller is the freestyle chatter+seller that pitches catalog pieces. It is
configured on Automations → 🤖 AI Seller, which is an owner/admin surface your
login does not show. Ask an owner or admin to enable it for this account.

Q (a fan-why paste, with an evidence pack attached): "Rae/Lisbon,Portugal/41
@u100200300 · Model1 — why ai chatter didnt reply to him"
The pack said: fan wrote 2026-08-27 00:02:11 (36h ago); ai_chatter answered x4
bubbles 2026-08-27 00:05:21 through 00:05:47; nothing inbound since (36h); the only later outbound is a $99.99
ppv_send on a mass run, 17 media, no purchase row; ai_chatter enabled=True; not
blacklisted, muted-creator no; CONTENT payer YES; automation_paused_until
00:05:58, EXPIRED; skip_list 'old_fan_pre_ai' from three weeks earlier, marked
NOT blocking; ABSENCE — no send_followup rule on the account.
A: **On Model1, fan 100200300 — AI Seller did reply, and it has been his turn
for 36 hours.** It answered him at 2026-08-27 00:05, about three minutes after he wrote — four rows that
are ONE reply, because consecutive bubbles inside the two-minute bubble window
count as one. It only speaks when the FAN spoke last, and he has not written
since.

Nothing will re-open him on this account: there is no 👋 Follow up quiet fans
rule at all (Automations → Automation rules). Adding one would not reach him as
he stands, either — Follow up quiet fans reads the skip list with no reason
filter, so the `old_fan_pre_ai` row that AI Seller ignores still hides him from
it. The last thing in the thread is an unopened $99.99 mass PPV, and AI Seller
does not chase a broadcast.

Two things it is NOT. The `old_fan_pre_ai` skip row is not the cause — Engage
old fans is on for this account, which exempts exactly that reason, and the
engine demonstrably replied three weeks after the row was written. Nor is his
spend — he passes the payer floor on real content purchases and is nowhere near
the whale hand-off.

Honest gap: there is no per-tick decision log, so "it looked at him today and
chose silence" is an inference from turn ownership, not a stored fact. To move
him, lift the skip row and add a follow-up rule, or just write to him yourself
from the chat.

Q (a fan-why paste, pack attached): "Omar @u100200301 · Model1 — why did he get
this mass?"
The pack said: outbound ppv_send $11.99 at 2026-08-28 08:46:12 (19h ago),
mass_run 990401,
"ppv_send; 5 recipients; audience at send time: excluded_users=2 ids held back
(not this fan), included_users=5 recipients the run had already resolved — THIS
FAN IS ON IT, list_ids=[], user_lists=[]"; no purchase row; he has NEVER
written; and under "THE LINE(S) BELOW DESCRIBE NO PAST SEND …", the line
"- ppv_send audience as configured today, from the PPV Library config (rule ON):
reach_all=true".
A: **Mass run 990401** put him there — a `ppv_send` broadcast that went out at
2026-08-28 08:46:12, 19 hours ago, carrying an $11.99 PPV to 5 people, and that
run's own row records Model1's fan 100200301 among the five it reached. That is a stored fact, not a
reconstruction.

What the row does NOT say is why the engine picked those five: this one stored
the audience already worked out, not the rule that produced it. "He was on the
list" is as far as this evidence goes. (Other runs store the opposite — only a
filter like `user_lists`, with no recipient list at all, because OnlyFans
expanded it. Read whichever the row actually holds; never assume the other.)

The PPV Library is configured differently TODAY (`reach_all=true`), but that
describes who it would reach now, not why he was in a send that has already gone
out. The two drift apart daily, and only what that row itself stored explains
the run.

He has never written a word, so nothing is waiting on a reply from us.

Q: "Model1 · id 100200300 — why did he get a mass last Tuesday?"
The pack's newest beat is "2026-08-28 04:02:48 (19h ago)  OUT  ppv_send  $99.99
… mass_run 990318", and the thread holds three beats, all stamped the 27th and
the 28th.
A: **Which date do you mean?** (Model1, fan 100200300.) I can see when things
happened — the newest beat
is stamped the 28th, 19 hours ago — but nothing tells me which day of the week
any of those dates was, so I cannot turn "last Tuesday" into one. Give me the
date and I will tell you whether a send from it is in front of me.

What I can see is three beats, stamped the 27th and the 28th. The newest is
mass_run 990318, a $99.99 `ppv_send` at 04:02 on the 28th, with him on that
run's own recipient list. If that is not the send you mean, it is not an answer
to your question — a send from another day never is.

Whatever the date turns out to be: the audience the PPV Library carries TODAY
describes who it would reach from now on, never who it reached then."""

_DRIFT_HEADER = """\
# Drift notice — the live automation registry disagrees with the manual

The lines below were checked against the running registry, not the prose above.
Where they conflict with the manual, THESE WIN."""


# ── The manual + the drift gate ───────────────────────────────────────

def _read_manual() -> tuple[str, frozenset[str]]:
    """Return (prose, documented kinds) from `assistant_manual.md`.

    The YAML-ish front-matter is the drift gate's input, not the model's — it is
    a bracket list of kind ids plus a `verified:` date, and neither helps answer
    a question — so it is stripped from the prose and only its `kinds:` line
    survives, as the set the live registry is diffed against.
    """
    text = MANUAL_PATH.read_text(encoding="utf-8")
    kinds: frozenset[str] = frozenset()
    if text.startswith("---\n"):
        end = text.find("\n---\n", 3)
        if end != -1:
            front, text = text[4:end], text[end + 5:]
            for line in front.splitlines():
                if line.startswith("kinds:"):
                    raw = line.split(":", 1)[1].strip().strip("[]")
                    kinds = frozenset(
                        k.strip() for k in raw.split(",") if k.strip())
    return text.strip(), kinds


def _drift_addendum(documented: frozenset[str]) -> str:
    """Diff the manual's kinds against the LIVE registry; "" when they agree.

    ⚠️ CALLED LAZILY, never at import. `_kind_catalog()` runs the plugin loader,
    and if this ran before `service/automations/` was importable every kind would
    read as REMOVED — and, because the prompt is cached for the life of the
    process, the bot would spend the whole deploy telling people that features
    they are looking at were deleted.
    """
    import automation_rules_api  # deferred: pulls the automation plugin loader

    catalog = {row["kind"]: row for row in automation_rules_api._kind_catalog()}
    lines: list[str] = []

    # Registered but undocumented — the bot must admit it exists without
    # inventing steps for it.
    for kind in sorted(set(catalog) - documented):
        row = catalog[kind]
        label = (row.get("label") or kind).strip()
        summary = (row.get("summary") or "").strip().rstrip(".")
        what = f" — {summary}" if summary else ""
        lines.append(
            f"- `{kind}` ({label}): EXISTS{what} — but detailed docs haven't "
            f"caught up. Say exactly that; do not guess steps.")

    # Documented but gone — the prose above is describing something deleted.
    for kind in sorted(documented - set(catalog)):
        lines.append(f"- `{kind}` was removed; say it was removed.")

    if not lines:
        return ""
    return _DRIFT_HEADER + "\n\n" + "\n".join(lines)


def _build_system_prompt() -> str:
    """Assemble instructions + few-shots + manual + drift, deterministically.

    Pure and repeatable on purpose: the byte-stability the prefix cache needs is
    a property of THIS function, so a test can assert it by building twice.
    """
    prose, documented = _read_manual()
    parts = [_INSTRUCTIONS, _FEWSHOTS, prose]
    drift = _drift_addendum(documented)
    if drift:
        parts.append(drift)
    return "\n\n---\n\n".join(parts)


_SYSTEM_PROMPT: str | None = None


def system_prompt() -> str:
    """The cached prompt, built on the FIRST REQUEST (see `_drift_addendum`)."""
    global _SYSTEM_PROMPT
    if _SYSTEM_PROMPT is None:
        _SYSTEM_PROMPT = _build_system_prompt()
        log.info("assistant: system prompt built (%d chars)", len(_SYSTEM_PROMPT))
    return _SYSTEM_PROMPT


# ── The request ───────────────────────────────────────────────────────

def _asker_role() -> str:
    """Who is asking, derived from the session — NEVER from the request body.

    Mirrors the chatter middleware's own precedence: when a founder is testing
    with both cookies present the User cookie wins for /admin/*, so a valid user
    identity outranks a chatter one here too. This shapes WORDING only — what
    the caller may reach is `assert_account_owned`'s decision, not the prompt's.
    """
    if get_request_user() is not None:
        return "an owner/admin"
    return "a chatter" if get_request_chatter() is not None else "an owner/admin"


def _user_block(question: str, role: str, state: str | None = None,
                fan: str | None = None) -> str:
    """Everything that varies per request lives HERE, not in the system prompt.

    The fan pack goes LAST, closest to the answer: when someone pastes a chat
    header and asks why a thread is silent, that block IS the question."""
    parts = [f"The asker is {role}.", f"Question: {question}"]
    if state:
        parts.append(state)
    if fan:
        parts.append(fan)
    return "\n\n".join(parts)


class AskBody(BaseModel):
    question: str = Field(..., max_length=MAX_QUESTION_CHARS)
    account_id: str = Field(..., min_length=1)  # empty would no-op the gate
    # No `role` field, deliberately — see the module docstring.


def _answer(text: str, error: str | None = None) -> dict[str, Any]:
    return {"answer": text, "error": error}


@router.post("/admin/assistant/ask")
async def ask(body: AskBody = Body(...)) -> dict[str, Any]:
    assert_account_owned(body.account_id)
    question = body.question.strip()
    if not question:
        return _answer("Ask me something about the product.", "empty_question")

    role = _asker_role()
    # Resolution comes FIRST: the paste may name a different model, and the live
    # state block has to describe the account the question is actually about.
    target = await assistant_fan_pack.build(body.account_id, question)
    assert_account_owned(target.account_id)   # belt: _resolve_account already filters
    state = await assistant_state.build(target.account_id, target.label)
    # One rule for both lines below: the SCOPED account owns this answer's
    # settings AND its bill, even when the paste named another account. The
    # daily AI budget is per account, so naming one in a question must not
    # reach into its config or spend its money. `resolve_model` is total —
    # there is no failure branch to write here.
    model = await resolve_model(body.account_id, PURPOSE)
    try:
        result = await llm_client.chat(
            model=model,
            purpose=PURPOSE,
            account_id=body.account_id,      # pays — see above
            messages=[
                {"role": "system", "content": system_prompt()},
                {"role": "user",
                 "content": _user_block(question, role, state, target.block)},
            ],
            temperature=0.3,
        )
    except LLMCapExceeded:
        log.info("assistant: cap exceeded for account=%s", body.account_id)
        return _answer("This account's AI budget is used up for today.", "cap")
    except LLMConfigError:
        log.warning("assistant: llm config error", exc_info=True)
        return _answer(
            "Your agency's LLM key isn't set — Setup → Keys.", "config")
    except LLMHTTPError:
        log.warning("assistant: llm provider error", exc_info=True)
        return _answer(
            "The AI provider is having trouble — try again in a minute.",
            "provider")

    answer = (result.content or "").strip()
    if not answer:
        # A model that reasoned its whole output budget away returns content=""
        # (see llm_client's note on the `thinking` field). Say something.
        log.warning("assistant: empty answer from %s", result.model)
        return _answer(
            "The AI came back empty — try asking again.", "empty_answer")
    # The model answered without rows it should have had — be honest about why.
    if state is None:
        answer += assistant_state.FAIL_NOTE
    if not target.ok:
        answer += assistant_fan_pack.FAIL_NOTE
    return _answer(answer)
