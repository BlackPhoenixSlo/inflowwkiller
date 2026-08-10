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

## The three calls, and why three

The operator's budget is up to THREE LLM calls per send, and the third is the
one that matters:

    1. READ    thread tail          -> the CONTRACT (what he asked for)
    2. MATCH   contract + shelf     -> candidate media ids
    3. VERIFY  each pick + contract -> keep only what provably satisfies it

Two calls would be a matcher. The third makes it a gate. It exists because the
signal underneath is measured at **22% precision for subject matter**: the vault
tag/description generator yielded 307 candidates for 68 keepers, and
`body_focus` lists "feet" whenever feet are VISIBLE rather than the subject — a
lingerie photo with her feet in frame matches. VERIFY is prompted adversarially
and defaults to REJECT, so an item has to earn its place twice.

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

import json
import logging
from dataclasses import dataclass, field

from sqlalchemy import select

import llm_client                       # module import so tests can patch .chat
import vault_pack_picker
from db.engine import get_session
from db.models import Message, VaultFolder, VaultFolderItem, VaultItem

from ._common import load_voice_blocks, resolve_model

log = logging.getLogger("of-relay.automation.content_resolver")

# Prompt-size guardrails, inherited from tip_context (which they served well).
_CANDIDATES_MAX = 150
_DESC_LEN = 180
_VERIFY_MAX = 24          # never verify more than this many in one call

# ── Typed refusals ──────────────────────────────────────────────────
# A refusal is a STATE, never silence. The caller turns it into a truthful
# message, a fallback, or an operator escalation — but it always knows why.
NO_ASK = "no_ask"                        # nothing was asked for in the thread
UNSUPPORTED_SUBJECT = "unsupported_subject"   # asked for something nobody curated
EMPTY_SHELF = "empty_shelf"              # the curated shelf exists but is empty
EXHAUSTED_FOR_FAN = "exhausted_for_fan"  # he already owns/has seen all of it
INSUFFICIENT_QUANTITY = "insufficient_quantity"  # fewer than asked for
NO_MATCH = "no_match"                    # the matcher found nothing that fits
VERIFY_REJECTED = "verify_rejected"      # every pick failed the adversarial check
LLM_UNAVAILABLE = "llm_unavailable"      # cap hit / provider error

MEDIA_KINDS = ("photo", "video")


@dataclass(frozen=True)
class Contract:
    """What THIS transaction promised, read from the thread.

    `strict` is the difference between "he likes feet" and "only feet, right?".
    A strict contract may never be satisfied by a substitute — the caller must
    refuse rather than send something adjacent.
    """
    subject: str | None = None          # the noun as he said it ("feet")
    category: str | None = None         # a curated category, when the subject maps
    rung: str | None = None             # a rung inside it, when he was specific
    media_kind: str | None = None       # "photo" | "video" | None (no preference)
    strict: bool = False
    quote: str = ""                     # his own words, for the audit trail
    exclusions: list[str] = field(default_factory=list)

    @property
    def asked(self) -> bool:
        return bool(self.subject or self.category)


@dataclass(frozen=True)
class Resolution:
    media_ids: list[int]
    contract: Contract
    refusal: str | None = None
    considered: int = 0                 # candidates the matcher saw
    rejected_by_verify: int = 0

    @property
    def ok(self) -> bool:
        return self.refusal is None and bool(self.media_ids)


def _empty(contract: Contract, refusal: str, **kw) -> Resolution:
    return Resolution(media_ids=[], contract=contract, refusal=refusal, **kw)


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
        body = " ".join(str(body or "").split())[:300]
        if body or tag:
            lines.append(f"{who}{tag}: {body}")
    return lines


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
                          media_kind: str | None) -> list[tuple[int, str]]:
    """Unseen DESCRIBED vault items, newest first — the fallback pool.

    ⚠️ This is the 22%-precision pool. Nothing may be sent from it without
    passing VERIFY, and a priced send should prefer the curated pool.
    """
    async with get_session() as s:
        q = select(VaultItem.media_id, VaultItem.kind, VaultItem.search_text,
                   VaultItem.description, VaultItem.video_description).where(
            VaultItem.account_id == str(account_id),
            VaultItem.removed_at.is_(None),
        )
        if media_kind in MEDIA_KINDS:
            q = q.where(VaultItem.kind == media_kind)
        rows = (await s.execute(
            q.order_by(VaultItem.created_at.desc()).limit(_CANDIDATES_MAX * 3)
        )).all()
    out: list[tuple[int, str]] = []
    for mid, _kind, search_text, desc, vdesc in rows:
        if int(mid) in seen:
            continue
        text = " ".join(str(search_text or desc or vdesc or "").split())
        if not text:
            continue
        out.append((int(mid), text[:_DESC_LEN]))
        if len(out) >= _CANDIDATES_MAX:
            break
    return out


async def _kind_of(account_id: str, media_ids: list[int]) -> dict[int, str]:
    if not media_ids:
        return {}
    async with get_session() as s:
        rows = (await s.execute(
            select(VaultItem.media_id, VaultItem.kind).where(
                VaultItem.account_id == str(account_id),
                VaultItem.media_id.in_(media_ids))
        )).all()
    return {int(m): str(k or "") for m, k in rows}


# ── The three calls ─────────────────────────────────────────────────

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
    known = ", ".join(sorted(vault_pack_picker.CATEGORIES)) or "(none)"
    system = (
        "You read an OnlyFans chat and state what the FAN has asked to see, or "
        "what he was explicitly promised. Reply as JSON:\n"
        '{"subject": "<short noun as he said it, or null>",\n'
        f' "category": "<one of: {known}, or null if none fit>",\n'
        ' "media_kind": "photo" | "video" | null,\n'
        ' "strict": true if he used exclusive words like "only"/"just"/"nothing '
        'but", else false,\n'
        ' "quote": "<his exact words, <=120 chars>",\n'
        ' "exclusions": ["<things he said he does NOT want>"]}\n'
        "If he has not asked for any specific content, return subject null. "
        "Do NOT infer a subject from what he seems to enjoy — only from what he "
        "asked for or was promised."
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

    subject = (str(p.get("subject") or "").strip() or None)
    category = (str(p.get("category") or "").strip().lower() or None)
    if category not in vault_pack_picker.CATEGORIES:
        category = None
    kind = (str(p.get("media_kind") or "").strip().lower() or None)
    if kind not in MEDIA_KINDS:
        kind = None
    excl = [str(x).strip() for x in (p.get("exclusions") or []) if str(x).strip()]
    return Contract(
        subject=subject, category=category, rung=None, media_kind=kind,
        strict=bool(p.get("strict")), quote=str(p.get("quote") or "")[:120],
        exclusions=excl[:6],
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
    p = res.parsed if isinstance(res.parsed, dict) else {}
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
        + (f"He explicitly said ONLY this — anything else is a broken promise. "
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

    # Pool: curated first — it is the only human-verified answer to "is this
    # item about the thing he asked for".
    pool: list[tuple[int, str]] = []
    if contract.category:
        pool = await _curated_pool(account_id, contract.category, contract.rung, seen)
        if not pool and require_curated:
            return _empty(contract, EMPTY_SHELF)
    if not pool:
        if require_curated:
            return _empty(contract, UNSUPPORTED_SUBJECT)
        pool = await _described_pool(account_id, seen, contract.media_kind)
    if not pool:
        return _empty(contract, EXHAUSTED_FOR_FAN)

    # Media kind is a promise too: "send me a video" is not satisfied by a photo.
    if contract.media_kind in MEDIA_KINDS:
        kinds = await _kind_of(account_id, [mid for mid, _ in pool])
        pool = [(mid, t) for mid, t in pool
                if kinds.get(mid, contract.media_kind) == contract.media_kind]
        if not pool:
            return _empty(contract, EXHAUSTED_FOR_FAN)

    considered = len(pool)
    model = await resolve_model(account_id, "content_resolver")
    try:
        picked = await _match(account_id, fan_id, contract, pool, count, model)
    except Exception:
        log.warning("match failed account=%s fan=%s", account_id, fan_id, exc_info=True)
        return _empty(contract, LLM_UNAVAILABLE, considered=considered)
    if not picked:
        return _empty(contract, NO_MATCH, considered=considered)

    if not verify:
        return Resolution(picked, contract, considered=considered)

    by_id = dict(pool)
    try:
        kept = await _verify(
            account_id, fan_id, contract,
            [(mid, by_id.get(mid, "")) for mid in picked[:_VERIFY_MAX]], model)
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
