"""service/automations/content_prompts.py — the model calls the content lane makes.

Every LLM call the content lane makes lives here, and nothing else does.
Extracted from `content_resolver` when that file crossed 1000 lines: tuning a
prompt and tuning a SQL pool are different jobs, and neither reader should have
to scroll through the other. The resolver orchestrates; this module is what
it says out loud.

Two lanes render through here, not one: the ask, and the gather-close farewell
that shares its claim spine (`pack_sender._ask_claim`). A call added for the
farewell still belongs here — the alternative is a second module that knows how
to talk to a model, which is how one lane starts paying for phrasing the other
already solved.

## The calls, and why there are four in the resolve loop

    1. READ     thread tail          -> the CONTRACT (what he asked for)
    2. MATCH    contract + pool      -> candidate media ids
    3. VERIFY   each pick + contract -> keep only what provably satisfies it
    4. NEAREST  contract + pool      -> when nothing IS it, what is CLOSEST

Two more sit outside that loop and write CAPTION halves rather than choosing
media — 5. ACTION ("what is she doing in it") and 6. ANSWER ("her line back to
him"). Neither picks anything, both are refusable, and both degrade to wording
that shipped before they existed.

Calls 1-3 are the shipped budget and the third is the one that matters: two
calls would be a matcher, and the third makes it a gate. It exists because the
signal underneath is measured at **22% precision for subject matter** — a
lingerie photo with feet in frame matches "feet" — so VERIFY is prompted
adversarially and defaults to REJECT, and an item has to earn its place twice.

Call 4 only ever runs on a REFUSAL, where the alternative was silence. It is a
different question from MATCH ("what fits?" vs "nothing fits — what is closest?")
and asking it is the difference between "here is something" and "this is close".
"""
from __future__ import annotations

import logging
import re
import time
from collections import Counter
from datetime import datetime, timedelta

from sqlalchemy import func, select

import llm_client                       # module import so tests can patch .chat
import vault_pack_picker
from db.engine import get_session
from db.models import Fan, Message, VaultItem, created_at_text, parse_ts

from ._common import (
    build_facts_note, facts_from_fan, load_voice_blocks, resolve_model,
)
# The canonical strip. `_funnel_routing` is a pure leaf (re + datetime), and it
# is where `reply_mass_funnel` and `_funnel_signals` already get this.
from ._funnel_routing import strip_html
from .content_contract import MEDIA_KINDS, Contract, _STOPWORDS

log = logging.getLogger("of-relay.automation.content_prompts")

# ── Thread reading (shared with tip_context's proven shape) ─────────

async def _thread_lines(account_id: str, fan_id: int, n_msgs: int,
                        voice: str = "her") -> list[str]:
    """The last `n_msgs` live messages as FAN:/HER: lines, oldest first.

    PPV and tip markers ride along, because "what was promised" and "what was
    paid for" are both part of the contract.
    """
    async with get_session() as s:
        rows = (await s.execute(
            select(Message.direction, Message.body, Message.price_cents, Message.is_tip)
            .where(Message.account_id == str(account_id),
                   Message.fan_id == int(fan_id),
                   Message.is_unsent.is_(False))
            .order_by(Message.created_at.desc())
            .limit(max(1, int(n_msgs)))
        )).all()
    me = "HIM" if str(voice or "").strip().lower() == "him" else "HER"
    lines: list[str] = []
    for direction, body, price_cents, is_tip in reversed(rows):
        who = "FAN" if direction == "in" else me
        tag = " [tip]" if is_tip else (
            f" [PPV ${int(price_cents or 0) / 100:.0f}]" if int(price_cents or 0) > 0 else "")
        # 🚨 STRIP FIRST. Bodies arrive as `<p>text</p>` and this was the only
        # history renderer in the tree that skipped it — `autoreply._history`,
        # `reply_mass_funnel._history_block` and `welcome_chatter_for_info.
        # _history_text` all strip. Caught 2026-08-24 by rendering `_him_block`
        # against live threads: every line came out wrapped in tags, on this
        # call AND on `read_contract`, which reads his words to find the ask.
        # Stripping before the clip so a tag can never be cut in half.
        body = " ".join(strip_html(str(body or "")).split())[:300]
        if body or tag:
            lines.append(f"{who}{tag}: {body}")
    return lines

async def _thread_started_at(account_id: str, fan_id: int) -> datetime | None:
    """When this conversation began — the oldest live message on the thread.

    Sits beside `_thread_lines` because it reads the same table for the same
    lane; kept a separate call because that one wants the LAST n rows and this
    wants a MIN over all of them. Both are covered by
    `ix_messages_account_fan_time`. None when the thread is empty.

    🚨 `created_at_text()`, NOT the mapped column — see the note above it in
    db/models.py. One row on one account once held '' there, and the DateTime
    processor raises INSIDE `.execute()`, which no try/except around the caller
    can catch. MIN is the worst possible aggregate to meet that cell with: ''
    sorts below every real date, so a single dirty row would be the answer for
    the whole thread, every time. `parse_ts` turns it into None instead, and an
    unknown start is already a defined outcome here — the plain call.
    """
    async with get_session() as s:
        raw = (await s.execute(
            select(func.min(created_at_text())).where(
                Message.account_id == str(account_id),
                Message.fan_id == int(fan_id),
                Message.is_unsent.is_(False))
        )).scalar()
    return parse_ts(raw)


# ── The vault's own vocabulary ──────────────────────────────────────
#
# 🚨 Measured on the pilot 2026-08-11, and it is the whole reason this exists:
#
#     the descriptions say  breasts (979 items),  vulva (627)
#     fans ask for          tits    (10 fans),    pussy
#
# `read_contract` is asked to expand a fan's word into search terms and it does
# it BLIND — the model has never seen this vault. So it produces the words a
# reasonable person would use, the lexical scan finds nothing, and a man asking
# for the most-photographed part of the vault gets `no_match`. No amount of
# cleverness in the matcher recovers a candidate retrieval never returned.
#
# Feeding the account's real description vocabulary into the prompt closes it:
# the model sees `breasts` in the list and emits `breasts` for "tits".
#
# It goes in the SYSTEM prompt and is static per account, which is deliberate —
# static text inside the window is nearly free against the provider's prefix
# cache, while the same words appended per-message would not be.
_LEXICON_TTL_S = 6 * 3600
_LEXICON_MAX = 200
_lexicon_cache: dict[str, tuple[float, list[str]]] = {}


async def vault_lexicon(account_id: str) -> list[str]:
    """The words THIS account's descriptions actually use, most common first.

    DOCUMENT frequency, not raw count: a word in 900 items beats one repeated
    nine times in the same item. Deliberately NOT frequency-capped — `breasts`
    is in 61% of this vault and is exactly the word the expander needs, so any
    "too common to be useful" rule would delete the payload. One such rule
    existed for about an hour on 2026-08-11 and did exactly that.

    This was two functions returning `(Counter, total)` so a `drop_useless_terms`
    guard could score words the lexicon never showed the model. That guard is
    gone — ranking moved into SQL, where a broad term simply sorts below a
    precise one — so the head of the list is the whole product. Caching the
    finished 200 words instead of the full counter also stops this holding a
    vault-sized vocabulary per account for six hours.
    """
    now = time.monotonic()
    hit = _lexicon_cache.get(str(account_id))
    if hit and now - hit[0] < _LEXICON_TTL_S:
        return hit[1]
    async with get_session() as s:
        rows = (await s.execute(
            select(VaultItem.search_text, VaultItem.description,
                   VaultItem.video_description).where(
                VaultItem.account_id == str(account_id),
                VaultItem.removed_at.is_(None))
        )).all()
    freq: Counter = Counter()
    for row in rows:
        freq.update({
            w for w in re.split(r"[^a-z]+",
                                " ".join(str(x or "") for x in row).lower())
            if 3 <= len(w) <= 20 and w not in _STOPWORDS
        })
    words = [w for w, _n in freq.most_common(_LEXICON_MAX)]
    _lexicon_cache[str(account_id)] = (now, words)
    return words

# ── The calls ───────────────────────────────────────────────────────

def _contract_system() -> str:
    """CALL 1's system prompt, hoisted out of `read_contract`.

    House idiom — `_daylog`, `_objection`, `_persona`, `_pins`, `gen_info`,
    `describe_media` and `welcome_chatter_for_info` all keep their prompt at module level.
    `read_contract` was 139 lines with 62 of them this string, which buried the
    three things it actually does: read the thread, call, parse.

    Built once at import — `known` reads `vault_pack_picker.CATEGORIES`, a
    literal dict, so it cannot differ between calls.
    """
    known = ", ".join(sorted(vault_pack_picker.CATEGORIES)) or "(none)"
    return (
        "You read an OnlyFans chat and decide TWO separate things.\n\n"
        "FIRST — is_ask: is he asking to SEE or RECEIVE content from her?\n"
        "  YES: \"send me that set\", \"can i see\", \"show me\", \"come show me\", "
        "\"i wanna see your feet\", \"got any videos\", \"unlock it for me\", or him "
        "naming content he wants to watch.\n"
        "  NO: small talk, his job, logistics, plans, an address, or wanting to "
        "SEE something that is not her content — \"i wanna see them go home to "
        "their families\" is NOT an ask, even though it says \"wanna see\".\n"
        "  A bare \"show me\" IS an ask.\n"
        "  NOT an ask: he is REPORTING a purchase, not requesting one — "
        "\"unlocking now babe\", \"just bought it\", \"said unlock for $10 but it "
        "was already unlocked\". He is paying. Pitching him here bills a man mid-"
        "payment for what he is already paying for.\n"
        "  NOT an ask, and important: he is TESTING whether she is a bot or "
        "demanding proof she is real (\"until u show me proof of life\", \"are "
        "you a bot\", \"send a pic with today's date\"). Set is_ask false and "
        "bot_test true — another part of the system owns that moment.\n\n"
        "CUSTOM REQUEST — he is describing a scene, a prop or a camera setup that "
        "would have to be FILMED for him (\"put the camera on a stand and throw "
        "the ball to you\"). Set is_ask true AND custom_request true: he wants "
        "content, but not content that exists.\n\n"
        "SECOND — subject: WHAT he asked for. This may be null even when is_ask "
        "is true (a bare \"show me\" names nothing).\n"
        "  Take the subject from HIS OWN LATEST MESSAGE FIRST. If he names a "
        "thing — leather, feet, ass, a video — that is the subject, and it "
        "OUTRANKS anything said earlier in the chat.\n"
        "  Only if his message names nothing, resolve a referring expression "
        "(\"that set\", \"it\", \"them\") against what SHE offered earlier.\n"
        "  Never carry a subject over from an old message when his latest one is "
        "small talk. If is_ask is false, subject MUST be null.\n\n"
        "COMPANY — did he ask for SOMEONE ELSE to be in it? Only true when he "
        "names another person: \"you and your friend\", \"a threesome\", \"with "
        "a guy\", \"two girls\". Him being in his own fantasy (\"me fucking "
        "you\") is NOT company — he is not in her vault. Default false.\n\n"
        "Reply as JSON:\n"
        '{"is_ask": true|false,\n'
        ' "company": true|false,\n'
        ' "subject": "<short noun, or null>",\n'
        f' "category": "<one of: {known}, or null if none fit>",\n'
        ' "media_kind": "photo" | "video" | "audio" | null  (audio only when he '
        'asked to HEAR her — "voice note", "moaning", "say it out loud"),\n'
        ' "strict": true if he used exclusive words like "only"/"just"/"nothing '
        'but", else false,\n'
        ' "quote": "<his exact words, <=120 chars>",\n'
        ' "exclusions": ["<things he said he does NOT want>"],\n'
        ' "terms": ["REQUIRED when subject is set: 8-15 SINGLE lowercase words '
        'that would appear in a written description of the media he wants. '
        'Decompose his phrasing and add synonyms; NEVER return his sentence. '
        'Be careful with words that also name FURNITURE or a SETTING — for '
        '\'leather\' he means what she is WEARING, so use: leather, boots, '
        'latex, jacket, corset, harness, gloves, thigh-high — not couch or sofa. '
        'Examples — \'feet\': feet, foot, soles, toes, barefoot, arches, ankles. '
        '\'back shots\': ass, behind, doggy, bent, arched, rear."]}'
    )


_CONTRACT_SYSTEM = _contract_system()



async def read_contract(account_id: str, fan_id: int, *, n_msgs: int = 20,
                        model: str | None = None) -> Contract:
    """CALL 1 — what did he ask for, in his own words?

    Deliberately conservative: no ask is a perfectly good answer, and inventing
    one is how a fan gets sent something he never wanted. `strict` is set only
    for exclusive language ("only", "just", "nothing but") — the exact shape of
    the message that preceded a real account deletion.
    """
    lines = await _thread_lines(
        account_id, fan_id, n_msgs,
        (await load_voice_blocks(account_id)).voice)
    if not lines:
        return Contract()
    system = _CONTRACT_SYSTEM
    # 🚨 The vault's OWN words, appended to the system prompt.
    #
    # Without this the expansion is a guess about a vault the model has never
    # seen. Measured on the pilot: the descriptions say `breasts` in 979 items
    # and `vulva` in 627; fans ask for "tits". Retrieval is a LIKE over that
    # description text, so an expansion into words the descriptions do not use
    # returns NOTHING — and a man asking for the most-photographed thing in the
    # vault gets `no_match`.
    #
    # Static per account and appended to the SYSTEM block on purpose: it is the
    # same bytes on every call for this account, which is what a provider prefix
    # cache rewards. Carried per-message it would be paid for on every turn.
    lexicon = await vault_lexicon(account_id)
    if lexicon:
        system += (
            "\n\nTHE VAULT'S OWN WORDS — the vocabulary these descriptions "
            "actually use, most common first:\n" + ", ".join(lexicon) + "\n"
            "Add a word from this list ONLY when it is what THIS VAULT calls "
            "the thing he asked for — \"tits\" -> \"breasts\". Do NOT add a "
            "word just because it is common: \"nude\", \"posing\" and "
            "\"bedroom\" describe most of the vault and would match everything. "
            "And NEVER DROP one of your own terms because it is missing here — "
            "this is the 200 most common words, not the whole vocabulary."
        )
    try:
        res = await llm_client.chat(
            model=model or await resolve_model(account_id, "content_resolver"),
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": "CHAT (oldest first):\n" + "\n".join(lines)}],
            purpose="content_resolver_contract",
            account_id=str(account_id), fan_id=int(fan_id),
            response_format={"type": "json_object"}, temperature=0.1,
        )
        p = res.parsed if isinstance(res.parsed, dict) else {}
    except Exception:
        log.warning("contract read failed account=%s fan=%s", account_id, fan_id,
                    exc_info=True)
        return Contract()

    is_ask = bool(p.get("is_ask")) and not bool(p.get("bot_test"))
    subject = (str(p.get("subject") or "").strip() or None)
    if not is_ask:
        subject = None            # an ask and a subject stand or fall together
    category = (str(p.get("category") or "").strip().lower() or None)
    if category not in vault_pack_picker.CATEGORIES:
        category = None
    kind = (str(p.get("media_kind") or "").strip().lower() or None)
    if kind not in MEDIA_KINDS:
        kind = None
    excl = [str(x).strip() for x in (p.get("exclusions") or []) if str(x).strip()]
    # Split anything multi-word and drop stop-words: a term is a LIKE needle, and
    # "booty shake on my face" matches nothing while "booty" matches plenty. The
    # first live review set came back with terms == [the whole sentence] and an
    # empty pool on every thread, so this is a safety net under the prompt.
    raw = list(p.get("terms") or [])
    if subject:
        raw.append(subject)
    terms: list[str] = []
    for chunk in raw:
        for word in re.split(r"[^a-z0-9]+", str(chunk).strip().lower()):
            if 3 <= len(word) <= 24 and word not in _STOPWORDS and word not in terms:
                terms.append(word)
    terms = terms[:15]
    return Contract(
        is_ask=is_ask, custom_request=bool(p.get("custom_request")) and is_ask,
        subject=subject, category=category, rung=None, media_kind=kind,
        company=bool(p.get("company")) and is_ask,
        strict=bool(p.get("strict")), quote=str(p.get("quote") or "")[:120],
        exclusions=excl[:6], terms=terms,
    )


async def _match(account_id: str, fan_id: int, contract: Contract,
                 pool: list[tuple[int, str]], limit: int,
                 model: str) -> list[int]:
    """CALL 2 — pick ids from the pool that fit the contract."""
    want = contract.subject or contract.category or "what he asked for"
    system = (
        "You match vault media to what a fan asked for. Pick AT MOST "
        f"{int(limit)} ids from the catalog whose descriptions clearly show: "
        f"{want}. "
        + (f"He does NOT want: {', '.join(contract.exclusions)}. "
           if contract.exclusions else "")
        + "Only clear matches — an empty list is the right answer when nothing "
        'fits. Reply as JSON: {"media_ids": [..]}.'
    )
    user = "CATALOG (id: description):\n" + "\n".join(
        f"{mid}: {text}" for mid, text in pool)
    res = await llm_client.chat(
        model=model,
        messages=[{"role": "system", "content": system},
                  {"role": "user", "content": user}],
        purpose="content_resolver_match",
        account_id=str(account_id), fan_id=int(fan_id),
        response_format={"type": "json_object"}, temperature=0.2,
    )
    return _ids_from(res.parsed, pool, limit)


def _ids_from(parsed, pool: list[tuple[int, str]], limit: int) -> list[int]:
    """The ids a pick call returned, filtered to the pool it was shown."""
    p = parsed if isinstance(parsed, dict) else {}
    valid = {mid for mid, _ in pool}
    picked: list[int] = []
    for x in (p.get("media_ids") or []):
        try:
            mid = int(x)
        except (TypeError, ValueError):
            continue
        if mid in valid and mid not in picked:
            picked.append(mid)
        if len(picked) >= limit:
            break
    return picked


async def _nearest(account_id: str, fan_id: int, contract: Contract,
                   pool: list[tuple[int, str]], limit: int,
                   model: str) -> list[int]:
    """The SUBSTITUTE call — nothing IS it, so what is CLOSEST to it?

    `_alternatives` answers this in SQL, from his profile or the head of an
    unranked pool, and neither has any concept of what a JOI is adjacent to.
    The matcher has already read this pool and this ask and said nothing fits;
    asking it a second, different question is the whole difference between
    "here is something" and "this is close to it".

    Asks for a MIX on purpose — operator ruling 2026-08-13, "send some video
    and image". A substitute is a weaker claim than an ask, and a clip carrying
    it does work that four more stills do not.
    """
    want = contract.subject or contract.category or "what he asked for"
    system = (
        "A fan asked for something this creator does not have. Nothing in the "
        f"catalog below IS {want} — do not pretend otherwise. Pick AT MOST "
        f"{int(limit)} ids that come CLOSEST in mood or body focus: what he is "
        "most likely to enjoy instead. Prefer a MIX — at least one video "
        "alongside the photos whenever both exist. "
        + (f"He does NOT want: {', '.join(contract.exclusions)}. "
           if contract.exclusions else "")
        + 'Reply as JSON: {"media_ids": [..]}.'
    )
    user = "CATALOG (id: description):\n" + "\n".join(
        f"{mid}: {text}" for mid, text in pool)
    res = await llm_client.chat(
        model=model,
        messages=[{"role": "system", "content": system},
                  {"role": "user", "content": user}],
        purpose="content_resolver_nearest",
        account_id=str(account_id), fan_id=int(fan_id),
        response_format={"type": "json_object"}, temperature=0.2,
    )
    return _ids_from(res.parsed, pool, limit)

_ANSWER_MAX_CHARS = 120        # her reply half of a caption, not a paragraph


# ── What CALL 6 is allowed to know about the fan ────────────────────
#
# Operator ruling 2026-08-24: a fan younger than two months gets his facts and
# his thread; past that the call runs plain.
#
# 🚨 The window is NOT a token budget and NOT a privacy rule. It is the window
# in which `_HIM_TAIL` lines are most of what has ever been said. Past it the
# tail stops being "the conversation" and becomes an arbitrary slice of a long
# one — and a line written off an arbitrary slice is worse than one written off
# nothing, because it answers a beat he left weeks ago. Widening this needs a
# summary, not a bigger tail.
FRESH_FAN_DAYS = 60
_HIM_TAIL = 12

_HIM_FACTS_HEAD = "WHAT SHE KNOWS ABOUT HIM:"
_HIM_THREAD_HEAD = "HOW THE CHAT HAS GONE (oldest first):"

# 🚨 THE LINE THAT KEEPS HIS FACTS FROM BECOMING A CONTENT CLAIM — the same
# hazard `_ppv_caption._STEER_RULE` exists for, one call over. Handed a kink
# list with no steer, the model writes the piece he WANTS rather than answering
# the man, and on a PRICED send that is the 2026-07-31 shape again.
_HIM_STEER = (
    "Use this only to pick WHICH true thing to answer and how to say it. "
    "Nothing in it describes what you are sending, and none of it is a promise."
)


async def _him_block(f: Fan | None, account_id: str, fan_id: int,
                     speaker: str) -> str:
    """Who he is and how the chat has gone — or "" for a fan past the window.

    Without it CALL 6 sees ONE message and no idea who sent it, so what comes
    back generalises ("hey u, been hoping ud come back"): true of every fan and
    written for none. This is the half that makes the line his.

    Two things, deliberately together. `build_facts_note` is the same projection
    the fan drawer and the OF note render, so the model re-reads what a human
    chatter would; `_thread_lines` is the lane's own reader, which is why the
    PPV and tip markers ride along — what he has already been offered is part of
    answering him. Facts alone name a hobby from week one and cannot tell it
    still matters; the thread alone cannot tell it ever did.

    ⚠️ THE AGE IS THE THREAD'S, not the row's. `subscribed_at` is the better
    answer and is used whenever it is set — but measured on the live stack
    2026-08-24 it was set on ONE fan in 3,295, `joined_date` on none, and no
    `raw_json` survives to backfill from, so a gate on it alone is a gate that
    never opens. The thread's first message is populated (3,306 fans, 2,304 of
    them inside the window) and it answers this question more directly anyway:
    the window asks whether `_HIM_TAIL` lines are most of what has been said,
    and where the talking STARTED is exactly that. A man who subscribed in
    March and first wrote last week should get the full block.

    🚨 NOT `Fan.created_at`. That is our row-insert time: this database's oldest
    row is 2026-05-21, so 88% of the roster reads as "new" by it — men
    subscribed for years included, the long-thread case this window exists to
    keep out. A fan with no sub date AND no messages gets the plain call.

    A fan with nothing on file and no thread yields "", which the caller turns
    into no block rather than an empty heading — the same reason
    `_ppv_caption.him_lines` returns `[]`: a heading with nothing under it reads
    as "you were given his details and they were blank", and what comes back
    apologises for not knowing him.

    ⚠️ Everything here is a fact about the FAN, never about the media. It is
    spliced with `_HIM_STEER` and must not be spliced without it.
    """
    started = f.subscribed_at if f is not None else None
    if started is None:
        started = await _thread_started_at(account_id, fan_id)
    if started is None:
        return ""
    if started <= datetime.utcnow() - timedelta(days=FRESH_FAN_DAYS):
        return ""
    parts: list[str] = []
    # `f` is legitimately None — `answer_line` documents the row as optional and
    # a brand-new sub has no Fan row yet. `facts_from_fan` takes a Fan, not an
    # Optional, and raises AttributeError on None; the outer catch would swallow
    # that into "" and quietly turn the whole feature off for that fan. We know
    # the conversation without knowing the man, so render the half we have.
    facts = build_facts_note(facts_from_fan(f)) if f is not None else ""
    if facts:
        parts.append(f"{_HIM_FACTS_HEAD}\n{facts}")
    lines = await _thread_lines(str(account_id), int(fan_id), _HIM_TAIL, speaker)
    if lines:
        parts.append(_HIM_THREAD_HEAD + "\n" + "\n".join(lines))
    return "\n".join(parts)


async def _answer_line(account_id: str, fan_id: int, his_words: str,
                       voice: str, model: str, *, f: Fan | None = None,
                       speaker: str = "her") -> str:
    """CALL 6 — ONE short line answering what he just said, in her voice.

    The voice half of a caption on a send he did not ask for. The gather-close
    farewell ships a fixed parting line ("made u a lil something"), which reads
    correctly at a graduation and reads as a fan being talked past when
    `ai_chatter` re-offers the same pack days later, cold, on a message he just
    wrote. `pack_claim.compose_caption` already calls the voice line "her reply
    to the thread" — this is what makes it one.

    🚨 IT MUST NOT DESCRIBE THE MEDIA. The clause above it is the CONTRACT,
    audited against what is attached (`audit_ask`), and a second claim underneath
    can only contradict it — the 2026-07-31 shape, two fans who bought and
    deleted their accounts within hours. The banned words are stated as the
    prompt's first rule, but a prompt rule is a REQUEST: `pack_claim.
    voice_line_ok` at the caption is the guarantee, exactly as `clean_action`
    is for CALL 5 above.

    `f` is his row and `speaker` the account's pronoun for the creator; both go
    straight to `_him_block`, which is empty for anyone past `FRESH_FAN_DAYS`.
    They arrive as parameters rather than being read here for the same reason
    `voice` and `model` do: `content_resolver.answer_line` holds all three, and
    a second keyed read of a row the caller is already holding is not a seam.

    ⚠️ `_ppv_caption.him_lines` is the sibling brief — the fan half of a caption
    on a send he ASKED for — and it deliberately drops hobbies and job ("a
    caption is not small talk"). This one keeps them, and the disagreement is
    the point: that caption sells the media, and this line answers the man. The
    hard rule they share is the one below.
    """
    him = await _him_block(f, account_id, fan_id, speaker)
    system = (
        "You write ONE short line for a creator replying to a fan's message "
        "in a chat. Answer what he actually said — acknowledge it, and tease "
        "forward. Rules: at most 18 words, no number, no price, no words "
        "like pic/photo/video/set, do not describe or promise any media, no "
        "question mark at the end, no name. Emoji are fine, at most one.\n"
        + (f"HOW SHE TEXTS:\n{voice}\n" if voice else "")
        + (f"{him}\n{_HIM_STEER}\n" if him else "")
        + 'Reply as JSON: {"line": "..."}.'
    )
    res = await llm_client.chat(
        model=model,
        messages=[{"role": "system", "content": system},
                  {"role": "user", "content": f"HIS MESSAGE:\n{his_words}"}],
        purpose="gather_close_answer",
        account_id=str(account_id), fan_id=int(fan_id),
        response_format={"type": "json_object"}, temperature=0.7,
    )
    parsed = res.parsed if isinstance(res.parsed, dict) else {}
    return " ".join(str(parsed.get("line") or "").split())[:_ANSWER_MAX_CHARS]


async def _action_phrase(account_id: str, fan_id: int,
                         described: list[tuple[int, str]], model: str) -> str:
    """CALL 5 — what is she DOING in the media he is about to get, in her voice.

    Only ever runs on the SUBSTITUTE path, where the caption has no honest noun
    to name: "not exactly joi but 🙈 …" states what this is not, and without
    this call the rest of the sentence is "my own take on it" — a refusal
    wearing a price. Operator ruling 2026-08-16: the fan is told what he IS
    getting, and the stored describe cannot be that sentence itself. It is
    clinical, third-person and written for a matcher ("A woman in a pink bra is
    lying on a couch with a man. Her hand is inserted into her vagina."), so it
    is condensed here rather than truncated at the caption.

    ⚠️ It is shown the descriptions of the media ACTUALLY BEING SENT, never the
    pool — a phrase describing an item the composition dropped is the same lie
    as a count that over-states, one field across.

    Returns "" on anything unusable; `pack_claim.clean_action` is the second
    gate and the caption degrades to its old wording rather than to silence.
    """
    system = (
        "You write ONE short phrase for a creator's own paid message, in FIRST "
        "PERSON, describing what she or he is doing in the media below. "
        "Rules: at most 8 words, lowercase, no name, no number, no price, no "
        "words like pic/photo/video/set, no emoji, no full stop. Start with "
        '"me ". If the media show several things, describe the main one. '
        'Reply as JSON: {"phrase": "me riding him on the couch"}.'
    )
    user = "MEDIA DESCRIPTIONS:\n" + "\n".join(text for _, text in described)
    res = await llm_client.chat(
        model=model,
        messages=[{"role": "system", "content": system},
                  {"role": "user", "content": user}],
        purpose="content_resolver_action",
        account_id=str(account_id), fan_id=int(fan_id),
        response_format={"type": "json_object"}, temperature=0.4,
    )
    parsed = res.parsed if isinstance(res.parsed, dict) else {}
    return str(parsed.get("phrase") or "")


async def _verify(account_id: str, fan_id: int, contract: Contract,
                  picks: list[tuple[int, str]], model: str) -> list[int]:
    """CALL 3 — the gate. Keep only what PROVABLY satisfies the contract.

    🚨 Prompted to REJECT on doubt, on purpose. The pool underneath is measured
    at 22% precision for subject matter: a photo whose description mentions feet
    is usually a photo that merely HAS feet in it. The matcher is optimistic by
    construction (it is asked to find things); this call is the pessimist that
    has to agree before anything is attached.
    """
    want = contract.subject or contract.category or "what he asked for"
    system = (
        "You are checking whether media matches a promise, before a fan is "
        f"charged for it. The promise is: {want}. "
        + ("He explicitly said ONLY this — anything else is a broken promise. "
           if contract.strict else "")
        + (f"He does NOT want: {', '.join(contract.exclusions)}. "
           if contract.exclusions else "")
        + "For each id, answer true ONLY if the description shows the promised "
        "thing as the SUBJECT of the media, not merely present in the frame. "
        "If the description is vague, or the thing is only incidentally "
        "visible, answer false. DEFAULT TO FALSE when uncertain — sending the "
        "wrong thing costs far more than sending nothing. "
        'Reply as JSON: {"verdicts": {"<id>": true|false}}.'
    )
    user = "ITEMS (id: description):\n" + "\n".join(
        f"{mid}: {text}" for mid, text in picks)
    res = await llm_client.chat(
        model=model,
        messages=[{"role": "system", "content": system},
                  {"role": "user", "content": user}],
        purpose="content_resolver_verify",
        account_id=str(account_id), fan_id=int(fan_id),
        response_format={"type": "json_object"}, temperature=0.0,
    )
    p = res.parsed if isinstance(res.parsed, dict) else {}
    verdicts = p.get("verdicts") if isinstance(p.get("verdicts"), dict) else {}
    kept: list[int] = []
    for mid, _text in picks:
        v = verdicts.get(str(mid), verdicts.get(mid))
        if v is True:                    # anything but an explicit true is a reject
            kept.append(mid)
    return kept
