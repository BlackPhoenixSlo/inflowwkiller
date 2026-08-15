"""service/automations/content_resolver.py — send him what he actually asked for.

ONE resolver, three callers (`tip_reward`, `make_right`, the on-ask PPV). It
answers a single question: *given this thread, which media may we attach?*

## Why this exists

Every path that sends a fan media used to choose it by MONEY. `tip_reward` picks
a tier from the tip size; `make_right` picks by the over-charge basis;
`ai_chatter` picks the cheapest offerable item. Meanwhile `gen_info` writes
`fetishes` for every fan and **nothing reads it** (verified 2026-08-10 — the only
consumers are a REST serializer and tests). So what he liked shaped what she
SAID and never what she SENT.

`tip_context.pick_context_media` was the first fix and it had the right idea, but
in production it produced **one match in 430 runs** (98 ok, 133 of them the
image-reply freebie, 6 bundles, 1 non-empty match). Nothing to learn from; the
mechanism was never exercised. This generalises it and hardens it.

## Three modules, one lane

This file got past 1000 lines and was split along the seam it already had:

    content_contract.py   the two shapes and the typed refusals (leaf, no I/O)
    content_prompts.py    every LLM call — READ, MATCH, VERIFY, NEAREST
    content_resolver.py   the vault pools, the house rules, and `resolve`

The names this module's callers already use (`Contract`, `Resolution`,
`read_contract`, every refusal constant) are re-exported below, so
`content_resolver.X` keeps meaning what it meant.

The call budget is up to THREE LLM calls per send and the third is the one that
matters — see `content_prompts` for why VERIFY exists and why it is prompted to
reject on doubt. A fourth, NEAREST, runs only where the answer was going to be
silence.

## Contract, not preference

🚨 The selector is **what this transaction promised**, never a stored profile.
The fan whose account deletion drives this asked *"only feet right?"* three
times: there was nothing to infer, and a perfect profile would not have saved
him. A profile fact is advisory; a thing he just said is binding. `fetishes` is
deliberately NOT read here.

## Failure contract — the caller decides, not this module

`resolve()` NEVER raises and never silently substitutes. It returns a
`Resolution` carrying either media ids or a typed `refusal`. That split is
load-bearing because the callers legitimately disagree:

  * `tip_reward` must never block a reward -> on refusal it falls back to its
    folder pull (a generic reward was never promised to be personal).
  * a PRICED send must fail CLOSED -> on refusal it sends nothing, because for a
    strict promise a generic send is worse than no send: it turns a recoverable
    delay into a second deception.

Hermetic in tests: no described vault rows ⇒ no candidates ⇒ zero LLM calls.
"""
from __future__ import annotations

import logging
import re

from sqlalchemy import case, func, literal, or_, select

import vault_pack_picker
import vault_scripts                    # is_solo — one definition of "she is alone"
from db.engine import get_session
from db.models import Fan, VaultFolder, VaultFolderItem, VaultItem
from jsonsafe import load_json

from ._common import resolve_model
# Re-exported, not merely used: `content_resolver.Contract` and
# `content_resolver.NO_MATCH` are what ~80 call sites across pack_sender,
# make_right, tip_reward and the suites already say. The split is an internal
# reorganisation and must not become a rename storm.
from .content_contract import (  # noqa: F401
    CUSTOM_REQUEST, EMPTY_SHELF, EXHAUSTED_FOR_FAN, INSUFFICIENT_QUANTITY,
    LLM_UNAVAILABLE, MEDIA_KINDS, NO_ASK, NO_MATCH, NO_MEDIA_OF_KIND,
    NO_SOLO_MEDIA, UNSUPPORTED_SUBJECT, VERIFY_REJECTED, Contract, Resolution,
    _empty, _STOPWORDS,
)
from .content_prompts import _match, _nearest, _verify, read_contract  # noqa: F401

log = logging.getLogger("of-relay.automation.content_resolver")

# Prompt-size guardrails, inherited from tip_context (which they served well).
_CANDIDATES_MAX = 150
_DESC_LEN = 180
_VERIFY_MAX = 24          # never verify more than this many in one call

# ── Candidate pools ─────────────────────────────────────────────────

async def _curated_pool(account_id: str, category: str, rung: str | None,
                        seen: set[int]) -> list[tuple[int, str]]:
    """Unseen items from the OPERATOR-CURATED shelf for a category.

    🚨 This is the pool a priced send should draw from. Membership here is a
    human verdict (`vault_pack_picker`), not a tag match, which is the only
    trustworthy answer to "is this item actually about the thing he asked for".
    """
    cat = vault_pack_picker.CATEGORIES.get(category)
    if cat is None:
        return []
    names = [cat.folder_name(rung)] if rung in cat.rungs else cat.folder_names
    async with get_session() as s:
        folder_ids = [
            f.id for f in (await s.execute(
                select(VaultFolder).where(
                    VaultFolder.account_id == str(account_id),
                    VaultFolder.name.in_(names),
                    VaultFolder.deleted_at.is_(None),
                )
            )).scalars().all()
        ]
        if not folder_ids:
            return []
        media_ids = [
            int(m) for m in (await s.execute(
                select(VaultFolderItem.media_id).where(
                    VaultFolderItem.account_id == str(account_id),
                    VaultFolderItem.folder_id.in_(folder_ids),
                ).order_by(
                    VaultFolderItem.manual_order.is_(None),
                    VaultFolderItem.manual_order,
                    VaultFolderItem.media_id,
                )
            )).scalars().all()
        ]
        fresh = [m for m in media_ids if m not in seen]
        if not fresh:
            return []
        rows = (await s.execute(
            select(VaultItem.media_id, VaultItem.kind, VaultItem.search_text,
                   VaultItem.description, VaultItem.video_description)
            .where(VaultItem.account_id == str(account_id),
                   VaultItem.media_id.in_(fresh))
        )).all()
    by_id = {int(r[0]): r for r in rows}
    out: list[tuple[int, str]] = []
    for mid in fresh:                       # preserve the operator's rank order
        row = by_id.get(mid)
        if row is None:
            continue
        text = " ".join(str(row[2] or row[3] or row[4] or "").split())
        out.append((mid, text[:_DESC_LEN] or "(no description)"))
    return out


async def _described_pool(account_id: str, seen: set[int],
                          media_kind: str | None,
                          terms: list[str] | None = None) -> list[tuple[int, str]]:
    """Unseen DESCRIBED vault items matching the contract's TERMS.

    🚨 This used to take the newest 150 items and let the matcher hunt. Measured
    on a real account 2026-08-11: of **280** items whose text mentions feet, only
    **26** are in the newest 150 — the window hid **91%** of the shelf. Recency
    is not relevance, and no amount of LLM cleverness recovers a candidate it was
    never shown.

    So retrieval is lexical and runs over the WHOLE described vault. The terms
    come from the model (it produced "soles", "toes", "barefoot", "arches" from
    the fan's word "feet"), which is strictly wider than a regex a human would
    maintain — and the scan itself costs no LLM call, only SQL.

    ⚠️ Still the 22%-precision pool: a description mentioning feet is usually a
    photo that merely HAS feet in it. Retrieval buys RECALL; VERIFY is what buys
    precision. Nothing may ship from here without passing it.
    """
    terms = [t for t in (terms or []) if t]
    async with get_session() as s:
        q = select(VaultItem.media_id, VaultItem.kind, VaultItem.search_text,
                   VaultItem.description, VaultItem.video_description).where(
            VaultItem.account_id == str(account_id),
            VaultItem.removed_at.is_(None),
        )
        if media_kind in MEDIA_KINDS:
            q = q.where(VaultItem.kind == media_kind)
        if terms:
            blob = func.lower(
                func.coalesce(VaultItem.search_text, "")
                + " " + func.coalesce(VaultItem.description, "")
                + " " + func.coalesce(VaultItem.video_description, "")
                + " " + func.coalesce(VaultItem.ai_fields_json, ""))
            q = q.where(or_(*[blob.like(f"%{t}%") for t in terms]))
        # 🚨 RANK IN SQL, so the LIMIT keeps the most RELEVANT rows.
        #
        # This used to order by `created_at` and limit to 600, then rank by hit
        # count in Python — which ranks only what the recency window happened to
        # let through. That is the same bug the docstring above warns about, one
        # level down: with a broad term set, 600-newest is a recency window
        # wearing a relevance costume.
        #
        # It also cost a whole mechanism. A `drop_useless_terms()` guard was
        # added to stop broad terms dragging the vault into that window, and it
        # deleted `breasts` (61% of this vault) — the exact word the lexicon
        # exists to produce. Ordering by score removes the need for the guard
        # entirely: a broad term contributes 1 to many rows and simply sorts
        # below `feet+soles+toes`, which is what a score is for.
        score = (
            sum((case((blob.like(f"%{t}%"), 1), else_=0) for t in terms),
                literal(0))
            if terms else literal(0))
        rows = (await s.execute(
            q.order_by(score.desc(), VaultItem.created_at.desc())
             .limit(_CANDIDATES_MAX * 4)
        )).all()
        if not rows and terms:
            # The scan found nothing. That is a vocabulary miss, NOT "he has seen
            # everything" — degrade to a broad pool and let the matcher judge,
            # rather than refusing a fan who genuinely asked for something.
            log.info("retrieval miss account=%s terms=%s — broad fallback",
                     account_id, terms[:6])
            broad = select(VaultItem.media_id, VaultItem.kind, VaultItem.search_text,
                           VaultItem.description, VaultItem.video_description).where(
                VaultItem.account_id == str(account_id),
                VaultItem.removed_at.is_(None))
            if media_kind in MEDIA_KINDS:
                broad = broad.where(VaultItem.kind == media_kind)
            rows = (await s.execute(
                broad.order_by(VaultItem.created_at.desc()).limit(_CANDIDATES_MAX * 2)
            )).all()

    # 🚨 SQL order is the ONLY order. There used to be a second scoring here — a
    # Python re-sort by term hits — and the two disagreed about what they were
    # reading: SQL scores the blob INCLUDING `ai_fields_json` (where the describe
    # pass writes `body_focus: feet`), Python scored the prose alone. The Python
    # sort ran last, so it silently overrode the SQL ranking with a weaker signal
    # and dropped every item whose metadata knew about feet but whose prose did
    # not. A feet query came back with one precise item in its top twenty.
    #
    # One scoring, over the widest text, in the place that also applies the
    # LIMIT. `seen` is still filtered here because it is a per-fan set, not a
    # property of the corpus.
    out: list[tuple[int, str]] = []
    for mid, _kind, search_text, desc, vdesc in rows:
        if int(mid) in seen:
            continue
        text = " ".join(str(search_text or desc or vdesc or "").split())
        # 🚨 OF fills `search_text` with the bare KIND WORD for media it has no
        # prose for: 699 audio rows on this roster read exactly "audio", and 26
        # gifs read "gif". That is a placeholder, not a description — it tells
        # the matcher nothing, and an unranked draw (`_alternatives`, or a
        # generic "show me") would ship it blind. Undescribed is undescribed
        # however the column is spelled.
        if not text or text.lower() == str(_kind or "").lower():
            continue
        out.append((int(mid), text[:_DESC_LEN]))
        if len(out) >= _CANDIDATES_MAX:
            break
    return out

async def solo_only(account_id: str, media_ids: list[int]) -> list[int]:
    """Drop anything with someone else in it.

    🚨 Operator ruling 2026-08-11, marked "very important": unless he asked for
    company, she is ALONE in what he gets. A fan who asked for her and was sent
    a threesome did not get a bonus, he got the wrong thing — and on this
    account that has cost an unsubscribe before.

    Reuses `vault_scripts.is_solo` rather than reading `people_count` here. That
    function already knows the way this goes wrong: the model returns
    `people_count: 1` for a clip it has just described as a blowjob, because it
    counts the person the clip is ABOUT. So the test is a disjunction over
    `partner_visible`, the count, the acts and the penetration field.

    🚨 THREE-WAY, not a veto. `is_solo` is deliberately false for a row nobody
    ever asked — correct for building an operator-facing folder, fatal here: on
    a vault without the V2 describe pass EVERY row is unknown, and a binary
    filter turns the whole lane silent. That is the same mistake `_rank_by_tier`
    documents, and it cost this file 8 red tests an hour apart.

      * confirmed company  → DROPPED. This is the rule, and it is absolute.
      * confirmed solo     → first.
      * never asked        → kept, behind the confirmed. Composition consumes
        this order, so an unknown is only reached once the certain ones are out.

    On the pilot vault that is 1,590 of 1,623 answered and 60 with company, so
    the uncertain tail is 2% — small, and behind everything that is sure.
    """
    if not media_ids:
        return []
    async with get_session() as s:
        rows = (await s.execute(
            select(VaultItem.media_id, VaultItem.ai_fields_json).where(
                VaultItem.account_id == str(account_id),
                VaultItem.media_id.in_(media_ids))
        )).all()
    fields = {int(m): (load_json(blob, {}) or {}) for m, blob in rows}
    solo, unknown = [], []
    for mid in media_ids:
        f = fields.get(mid, {})
        if vault_scripts.is_solo(f):
            solo.append(mid)
        elif not vault_scripts.solo_known(f):
            unknown.append(mid)        # nobody asked — not evidence of company
    return solo + unknown


async def profile_terms(account_id: str, fan_id: int) -> list[str]:
    """What HE likes, from his profile — not from this conversation.

    ⚠️ Deliberately never used to fill an ask. A thing he just said is binding
    and a profile fact is advisory, and letting `fetishes` answer "send me feet"
    is how a fan gets charged for a guess. This exists for the opposite moment:
    the answer to his ask is NO, and the alternative she offers instead should
    be something he has actually shown interest in rather than whatever happened
    to sort first.
    """
    async with get_session() as s:
        fan = await s.get(Fan, (str(account_id), int(fan_id)))
    if fan is None:
        return []
    raw = [str(fan.fetishes or ""), str(fan.self_description or "")]
    if fan.likes_ass:
        raw.append("ass booty butt")
    if fan.likes_boobs:
        raw.append("tits boobs cleavage")
    terms: list[str] = []
    for chunk in raw:
        for word in re.split(r"[^a-z0-9]+", chunk.lower()):
            if 3 <= len(word) <= 24 and word not in _STOPWORDS and word not in terms:
                terms.append(word)
    return terms[:12]


async def _alternatives(account_id: str, fan_id: int, pool: list[tuple[int, str]],
                        seen: set[int], count: int) -> list[int]:
    """What she CAN show when the answer to his ask is no.

    Operator ruling 2026-08-11: on "show me" with nothing that fits, say *"i
    don't have that, but check this — you might like it"* and take the
    alternative from what is known about HIM. A refusal that offers nothing is
    silence, and silence is the failure shape this map keeps hitting.

    Falls back to the head of the pool, which is still ranked by his own words —
    a fan with an empty profile gets a worse alternative, not no alternative.
    """
    terms = await profile_terms(account_id, fan_id)
    if terms:
        liked = await _described_pool(account_id, seen, None, terms)
        # A consolation offer is still a send, so the solo rule holds here too.
        solo = await solo_only(account_id, [mid for mid, _ in liked])
        if solo:
            return solo[:count]
    if pool:
        return [mid for mid, _ in pool[:count]]
    # 🚨 The pool itself is empty — the kind filter or the company rule took it,
    # or it was never built. Returning [] here is how a refusal becomes silence,
    # which is the failure shape this whole function exists to prevent, so draw
    # from the vault at large instead. Note the deliberate absence of a media
    # kind: "i don't have that video, but here are pics" is the answer, and
    # honouring the filter that just emptied the pool would return nothing.
    # SOLO still holds — a consolation offer is a send like any other.
    broad = await _described_pool(account_id, seen, None, [])
    return (await solo_only(account_id, [mid for mid, _ in broad]))[:count]


async def last_resort(account_id: str, fan_id: int, *, seen: set[int],
                      count: int) -> list[int]:
    """SOMETHING from the vault, whatever the ask was. Never raises, never empty
    unless the vault genuinely is.

    Operator ruling 2026-08-15: an ask must never end in silence. Every refusal
    branch above already carries `alternatives`, but three of them cannot —
    `LLM_UNAVAILABLE` twice and `VERIFY_REJECTED` — because at that point there is
    no trustworthy pool left to draw from. This is the caller-side floor under all
    of them: his profile terms first, the vault at large after, SOLO throughout.

    It makes NO claim about the subject. Whatever comes back is a substitute and
    the caption must say so — `pack_claim.substitute_clause` is the only honest
    frame for it, and `audit_ask(substitute=True)` is what checks the frame held.
    """
    try:
        return await _alternatives(account_id, fan_id, [], seen, count)
    except Exception:
        log.warning("last_resort failed account=%s fan=%s", account_id, fan_id,
                    exc_info=True)
        return []


async def kind_of(account_id: str, media_ids: list[int]) -> dict[int, str]:
    if not media_ids:
        return {}
    async with get_session() as s:
        rows = (await s.execute(
            select(VaultItem.media_id, VaultItem.kind).where(
                VaultItem.account_id == str(account_id),
                VaultItem.media_id.in_(media_ids))
        )).all()
    return {int(m): str(k or "photo") for m, k in rows}

async def _substitutes(account_id: str, fan_id: int, contract: Contract,
                       pool: list[tuple[int, str]], seen: set[int], count: int,
                       model: str) -> list[int]:
    """What she can offer instead: the judged answer, then the SQL one.

    The LLM half may be capped, erroring or simply unhelpful, and a refusal that
    falls back to silence is the failure this whole path exists to end — so the
    SQL draw stays underneath it as the floor, never as the first answer.
    """
    if pool:
        try:
            if alts := await _nearest(account_id, fan_id, contract, pool,
                                      count, model):
                return alts
        except Exception:
            log.warning("nearest failed account=%s fan=%s", account_id, fan_id,
                        exc_info=True)
    return await _alternatives(account_id, fan_id, pool, seen, count)


# ── The resolver ────────────────────────────────────────────────────

async def resolve(account_id: str, fan_id: int, *, count: int,
                  seen: set[int] | None = None,
                  contract: Contract | None = None,
                  require_curated: bool = False,
                  verify: bool = True,
                  n_msgs: int = 20) -> Resolution:
    """Media for this fan that satisfies what he asked for, or a typed refusal.

    `require_curated` — draw ONLY from operator-curated shelves. A priced send
    should set this: it is the difference between "a human filed this under
    feet" and "an LLM thought this description sounded like feet".

    `verify` — run CALL 3. Leave it on for anything a fan pays for. It may be
    turned off for a free, non-promised bundle where the cost of a loose match
    is a slightly-off freebie rather than a broken contract.

    `contract` — pass one to SKIP call 1 (the caller already knows what was
    promised, e.g. `make_right` repairing a specific broken promise).
    """
    seen = set(seen or ())
    if count <= 0:
        return _empty(contract or Contract(), INSUFFICIENT_QUANTITY)

    if contract is None:
        contract = await read_contract(account_id, fan_id, n_msgs=n_msgs)
    if not contract.asked:
        return _empty(contract, NO_ASK)
    # He asked but named nothing — "come show me", "Show me". Operator ruling
    # 2026-08-11: that IS a PPV ask. There is no subject to search on, so the
    # pool is the vault at large and the matcher picks something worth sending.
    generic = contract.is_ask and not (contract.subject or contract.category)

    # ── The pool is the CURATED SHELF ∪ THE WHOLE VAULT ────────────
    #
    # Operator ruling 2026-08-11: "we are searching the whole vault for videos a
    # fan might want + these folders, as they are absolutely correct as I picked
    # them." So the two are a union, not a fallback chain:
    #
    #   • curated items are TRUSTED — a human looked at the pixels and filed
    #     them. They rank first and they skip VERIFY, because re-judging a
    #     human's eyes against a description written by a different model is how
    #     the verifier came to reject 14 of the operator's own 14 keeps.
    #   • retrieved items are UNTRUSTED — found by lexical scan over everything,
    #     which is the only way a VIDEO ever gets picked (the feet shelves are
    #     stills). These are what VERIFY exists to gate.
    trusted: list[tuple[int, str]] = []
    if contract.category:
        trusted = await _curated_pool(account_id, contract.category, contract.rung, seen)
        if not trusted and require_curated:
            return _empty(contract, EMPTY_SHELF)
    if require_curated:
        pool = trusted
        if not pool:
            return _empty(contract, UNSUPPORTED_SUBJECT)
    else:
        have = {mid for mid, _ in trusted}
        found = [(m, t) for m, t in
                 await _described_pool(account_id, seen, contract.media_kind,
                                       contract.terms)
                 if m not in have]
        pool = trusted + found
    trusted_ids = {mid for mid, _ in trusted}
    if not pool:
        # `_described_pool` already degrades a vocabulary miss to a broad draw, so
        # an empty pool here never means "we don't stock that subject" — it means
        # the media-KIND filter emptied the scan (he asked for a video on an
        # account whose videos are undescribed — one live account carries 121
        # videos and 99 audio clips with none of the audio described) or he
        # really has seen it all.
        return _empty(contract,
                      NO_MEDIA_OF_KIND if contract.media_kind in MEDIA_KINDS
                      else EXHAUSTED_FOR_FAN,
                      alternatives=await _alternatives(account_id, fan_id, [],
                                                       seen, count))

    # Media kind is a promise too: "send me a video" is not satisfied by a photo,
    # and this is why the whole vault is in the pool — the curated feet shelves
    # are stills, so a video can only ever come from the retrieved side.
    if contract.media_kind in MEDIA_KINDS:
        kinds = await kind_of(account_id, [mid for mid, _ in pool])
        pool = [(mid, t) for mid, t in pool
                if kinds.get(mid, contract.media_kind) == contract.media_kind]
        trusted_ids &= {mid for mid, _ in pool}
        if not pool:
            return _empty(contract, NO_MEDIA_OF_KIND,
                          alternatives=await _alternatives(account_id, fan_id, [],
                                                           seen, count))

    # SOLO unless he asked otherwise. Applied to the whole pool — including the
    # curated shelves, which a human filed by SUBJECT and never by who else was
    # in the frame, so "a human picked it" is not evidence about company.
    if not contract.company:
        keep = set(await solo_only(account_id, [mid for mid, _ in pool]))
        pool = [(mid, t) for mid, t in pool if mid in keep]
        trusted_ids &= keep
        if not pool:
            # Every match has someone else in frame. A shelf problem, not a fan
            # who has run out — and `_alternatives` re-applies SOLO, so the
            # consolation draw can never leak the company media we just refused.
            return _empty(contract, NO_SOLO_MEDIA,
                          alternatives=await _alternatives(account_id, fan_id, [],
                                                           seen, count))

    considered = len(pool)
    model = await resolve_model(account_id, "content_resolver")

    if contract.custom_request:
        # He described a shoot. Refuse the ASK but hand the caller the nearest
        # real things, so she says "i don't have anything like that, but check
        # this" instead of guessing or going quiet.
        return _empty(contract, CUSTOM_REQUEST, considered=considered,
                      alternatives=await _substitutes(account_id, fan_id, contract,
                                                  pool, seen, count, model))
    try:
        picked = await _match(account_id, fan_id, contract, pool, count, model)
    except Exception:
        log.warning("match failed account=%s fan=%s", account_id, fan_id, exc_info=True)
        return _empty(contract, LLM_UNAVAILABLE, considered=considered)
    if not picked:
        alts = await _substitutes(account_id, fan_id, contract, pool, seen, count, model)
        # 🚨 A GENERIC ask can never fail to match. Operator ruling 2026-08-11:
        # "show me — always find something." He named nothing, so there is no
        # promise for a pick to break; refusing here is the bug, not the safety.
        # Three of the operator's 22 silences in round 3 were exactly this
        # ("come show me", "Show me", "Show me tell me once and I learn it") and
        # every one of them was a live buyer mid-conversation.
        if generic and alts:
            return Resolution(alts[:count], contract, considered=considered)
        return _empty(contract, NO_MATCH, considered=considered,
                      alternatives=alts)

    # Nothing was promised, so there is no promise to check. Running the
    # verifier against "what he asked for" when he asked for nothing in
    # particular is how a bare "show me" ends in VERIFY_REJECTED.
    if not verify or generic:
        return Resolution(picked, contract, considered=considered)

    by_id = dict(pool)
    # A curated pick is already verified — by a human, against the pixels. Only
    # what the machine found needs the gate.
    machine = [mid for mid in picked if mid not in trusted_ids][:_VERIFY_MAX]
    human = [mid for mid in picked if mid in trusted_ids]
    if not machine:
        return Resolution(picked, contract, considered=considered)
    try:
        kept = human + await _verify(
            account_id, fan_id, contract,
            [(mid, by_id.get(mid, "")) for mid in machine], model)
        kept = [mid for mid in picked if mid in set(kept)]   # keep rank order
    except Exception:
        # 🚨 A verifier that cannot run is not a pass. The whole point of this
        # call is that nothing ships unchecked.
        log.warning("verify failed account=%s fan=%s — refusing",
                    account_id, fan_id, exc_info=True)
        return _empty(contract, LLM_UNAVAILABLE, considered=considered)

    rejected = len(picked) - len(kept)
    if not kept:
        return _empty(contract, VERIFY_REJECTED, considered=considered,
                      rejected_by_verify=rejected)
    # A STRICT promise is all-or-nothing: if the verifier threw any of it out,
    # what is left is a partial delivery of an exclusive claim. Refuse instead.
    if contract.strict and rejected:
        return _empty(contract, VERIFY_REJECTED, considered=considered,
                      rejected_by_verify=rejected)
    return Resolution(kept, contract, considered=considered,
                      rejected_by_verify=rejected)


def explain(res: Resolution) -> str:
    """One line for a log or an operator readout. Never shown to a fan."""
    if res.ok:
        return (f"{len(res.media_ids)} item(s) for {res.contract.subject or '?'}"
                f"{' [strict]' if res.contract.strict else ''}"
                f" — {res.considered} considered, {res.rejected_by_verify} rejected")
    return (f"REFUSED {res.refusal} — asked={res.contract.subject or 'nothing'}"
            f" considered={res.considered} rejected={res.rejected_by_verify}")
