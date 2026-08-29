"""service/automations/_ppv_caption.py — the PPV caption prompt, in a 1:1 thread.

`describe_media.caption_style_prompt` writes a caption from what the vision model
actually saw in ONE piece of vault media. It knows the media and nothing about who
is reading it, which is exactly right for a mass send: one line goes to hundreds of
people at once, so there is no "him" to write for.

A 1:1 thread is the opposite case. The same media goes to ONE man whose name, kink
and notes we are already holding — and the two places the chatter attaches a priced
piece on its own initiative were sending a line that had read NEITHER half:

    _fire_post_buy_rung    `_pack_line("rung_escalate")` → or `voice.unlock_prompt`
    _maybe_discount_resend `_pack_line("discount_resend", price_cents=px)`

Those are script-pack lines. They are good lines, they are in voice, and they were
written before anyone knew what was in the media or who was buying it. "unlock it
for me 😘" is the same sentence whether the clip is a shower strip or a feet set,
and whether he is a $600 whale who told us three weeks ago exactly what he is into
or a man who has never spoken.

## What this module is, and what it deliberately is NOT

It is the fan-facing half: WHICH media to write about, WHAT he gets told about
himself, and the finished string the send path puts on the wire.

It owns no prompt text and makes no LLM call of its own. `describe_media` owns the
system prompt (`caption_style_prompt`), the model choice (`caption_model`), what
counts as described (`_copy_source`) and the call itself (`_copy_call`) — this
module supplies a style, a body suffix and a purpose. That split is not tidiness:
`_copy_source`'s own docstring warns that "two spellings of this is how one path
starts paying for items the other considers unwritable" — and `_copy_source` is
also what decides whether this lane writes anything at all, because THE GATE IS THE
DESCRIPTION and nothing else (ruling 2026-08-23; the vault-AI master used to sit in
front of it and no longer does — see `caption_model`). This module is the lane that
would have been that second spelling, so it does not keep one.

## Why the fan block is its OWN paragraph and not more item facts

`vault_ai_brief.item_facts` is ground truth — what a vision model saw. Its brief
(`SELLING_BRIEF`) opens by saying so and then states THE HARD RULE: never promise
something the facts don't list, because "selling a thing that isn't in the media is
what produces refunds and chargebacks".

His fetish list is not ground truth about the media. It is ground truth about HIM.
Folding the two into one block invites exactly the substitution that rule exists to
prevent — the model reading "FEET" under his name and writing a feet line about a
clip with no feet in it. So the fan block arrives after the media facts, under its
own heading, and its instruction is to STEER, never to ADD. `_STEER_RULE` is that
sentence and it is the most important line in this file.

🚨 This is why `pack_sender` is NOT a caller and must never become one. Its caption
is a CONTRACT — "11 bare feet pics" — audited against the attached media and
REFUSED rather than softened when they disagree, because on 2026-07-31 two fans made
their first purchase and deleted their OnlyFans accounts within hours over a caption
that promised what the media did not hold. A free-form line has no count to audit.
That lane already solved this problem; it does not need a second answer.

## The OFF path is the product

`line_for` always returns a sendable string, and returns the caller's own line on
every miss — flag off, nothing described in the media he is being shown, the daily
cap, a dead key, a blank line back, an unexpected exception. It is fail-open onto
the OLD behaviour, never onto silence: a caption that fails to generate must cost
us a nicer sentence, never the sale.
"""
from __future__ import annotations

import logging
import random
from typing import Any

from sqlalchemy import select

import ownership
from db.engine import get_session
from db.models import CatalogItem, Fan, FanProfile, VaultItem

from . import describe_media as dm
from ._common import apply_word_restriction
from .names import resolve_fan_name
# The 600-char chat-list clip. Imported from the module that already owns the
# value every 1:1 send path uses, rather than becoming a FOURTH copy of it
# (welcome_chatter_for_info, autoreply and pack_claim each declare their own).
from .welcome_chatter_for_info import _REPLY_MAX_CHARS

log = logging.getLogger("of-relay.automation.ppv_caption")

# The chatter-side flag, read from the ai_chatter config blob so an operator turns
# this on for one account from the raw-JSON editor with nothing to redeploy.
CONFIG_KEY = "ppv_caption_1to1"

def enabled(cfg: dict) -> bool:
    """THE question "is the caption lane on for this account", asked in one place.

    Three callers need it — both priced send paths (to decide whether to pay for
    his spend history) and `line_for` itself — and a flag with three literal
    readers is a flag that eventually gets read three different ways. It also
    keeps the OFF path honest: `_buyer_facts` is two DB queries, and running them
    on every post-buy rung and discount resend for a feature that is off
    everywhere would be exactly the kind of cost the OFF path is supposed not to
    have.
    """
    return bool(cfg.get(CONFIG_KEY))


# Its own purpose so caption spend is separable from reply spend in `grok_calls`.
# Same string as the flag by coincidence of naming, not by contract — they answer
# different questions and either may be renamed without the other.
PURPOSE = "ppv_caption_1to1"

# Re-exported so `ai_chatter._manifest_block` can splice the shared open-rule into
# the catalogue's close by name, with no function-level import at the use site.
# `describe_media` still owns the string; this is an alias, never a second copy.
#
# An earlier version imported `describe_media` lazily — inside three functions — to
# keep the vault-AI surface out of the reply loop's import graph. Measured, that was
# worth 99ms of one-time boot cost (ai_chatter's import goes 0.272s → 0.371s) in
# exchange for function-level imports in three places and a claim in this comment
# that stopped being true the moment any of them ran. Paid the 99ms.
CAPTION_OPEN_RULE = dm.CAPTION_OPEN_RULE

# Heading for the fan half of the brief. Deliberately not "FACTS ABOUT HIM": the
# media block above it is already called facts, and the whole point of the split is
# that these two kinds of true thing may not be used the same way.
_FAN_HEAD = "WHO IS READING IT"

# 🚨 THE LINE THAT KEEPS THE FAN BLOCK FROM BECOMING A CONTENT CLAIM.
# Without it the model treats his kink list as something to satisfy rather than
# something to aim at, and writes the piece he WANTS instead of the piece we have.
_STEER_RULE = (
    "Use this to choose WHICH TRUE DETAIL of the media to open on, and how to "
    "address him. It is NOT a list of things the media contains. Never name an act, "
    "a garment or a body part that the facts above do not list, however much he "
    "wants it — the HARD RULE above wins every time."
)

# One detail, not a dossier. A caption is <= 110 characters; handed six facts about
# him the model tries to spend them and the line stops sounding like a caption.
_ONE_DETAIL = ("Work in AT MOST ONE thing about him, and only if it lands "
               "naturally. A caption that just sells the media beats one that "
               "name-drops his job.")

# What a caption may know about a fan. A deliberate SUBSET of `_facts_block`'s
# fields, and the omissions are the design:
#   • age / city / country — a caption is not small talk; these produce "still up
#     at 2am in Leeds?" lines that read as surveillance on a paywall.
#   • hobbies / occupation — same, and they are what `_facts_block`'s own nudge
#     already steers the CONVERSATION at. The caption is not the conversation.
# What survives is what changes which media detail you open on (`fetishes`) and how
# you address him (`name`), plus what a human chatter would re-read before pricing
# him — his notes and what he has actually spent.
_FAN_FIELDS = (("into", "fetishes"),)

# Longer than the facts block's 80 for the same reason `_facts_block` clips its kink
# nudge at 160: a kink list is the one value here that routinely runs long enough
# for 80 to cut mid-item, and half a fetish is worse than none.
_KINK_MAX = 160
_BIO_MAX = 300
_NOTES_MAX = 400
# A caption is one line. Two spend facts is the most that can steer it.
_SPEND_FACTS_MAX = 2

# How much of one item's vision description reaches the CATALOGUE MANIFEST (not the
# caption — that gets the full brief). The manifest renders once per selling turn
# with every offerable piece on it, so this is multiplied by the shelf size: six
# pieces at 220 is ~1.3k characters of prompt on the engine that runs most often.
# 220 is where a `video_description` still carries the setting and the garment — the
# two details that make a pitch sound like a memory — and stops before the
# shot-by-shot beats, which are for a caption writer working one piece at a time,
# not for a model choosing between six.
MANIFEST_FACT_MAX = 220


# ── what the model is told about him ────────────────────────────────

def him_lines(f: Any, profile: Any = None,
              spend_facts: list[str] | None = None,
              name: str = "") -> list[str]:
    """The fan half of the brief, as bare `label: value` lines.

    Named `him_lines` and not `fan_lines` because `fans.get_fan_lines` /
    `generate_fan_lines` already own that name for something else entirely — copy
    written FOR a fan, not facts written ABOUT one.

    Pure and sync so the composition is testable without a model, a client or a
    fan row — the callers already hold all three and the tests should not have to.

    A fan with nothing on file yields `[]`, which `brief` turns into no block at
    all rather than an empty heading. That matters more than it looks: a heading
    with nothing under it reads to the model as "you were given his details and
    they were blank", and the observed reply to that is a line that apologises for
    not knowing him.
    """
    out: list[str] = []
    if name:
        # Same split/clip as `_facts_block` — `resolve_fan_name` can hand back a
        # "preferred/legal" pair and only the first half is what he is called.
        out.append(f"his name: {str(name).split('/')[0][:40]}")
    for label, attr in _FAN_FIELDS:
        val = getattr(f, attr, None) if f is not None else None
        if val and str(val).strip():
            out.append(f"{label}: {str(val).strip()[:_KINK_MAX]}")
    if profile is not None:
        bio = str(getattr(profile, "short_bio", "") or "").strip()
        if bio:
            out.append(f"about him: {bio[:_BIO_MAX]}")
        notes = str(getattr(profile, "bullet_points", "") or "").strip()
        if notes:
            out.append(f"notes on him: {notes.replace(chr(10), '; ')[:_NOTES_MAX]}")
    for sf in (spend_facts or [])[:_SPEND_FACTS_MAX]:
        if str(sf).strip():
            out.append(str(sf).strip()[:120])
    return out


def _money(cents: int) -> str:
    """`ppv_send._money`, and deliberately the same rule — a fan who reads "$41.23"
    in a mass caption and "$41.23" here is reading one voice. Copied rather than
    imported because importing `ppv_send` from the reply loop to format a number
    would pull the whole mass-send surface in for two lines of arithmetic."""
    return f"${cents // 100}" if cents % 100 == 0 else f"${cents / 100:.2f}"


def body_suffix(lines: list[str], *, price_cents: int | None = None,
                was_cents: int | None = None) -> str:
    """The BODY SUFFIX for a caption call — his facts, then the price.

    Returned as a suffix (not a whole body) because `describe_media._copy_call`
    builds the media facts and the selling brief, and this must land AFTER them.
    The brief ends on THE HARD RULE and the reverse-order guidance; splicing his
    details in above that puts the last word in the prompt on his kink instead of
    on what the media actually contains, and an example beats the rule beside it.

    `price_cents` rides along only where the caller is quoting a number the line
    may have to carry — the discount resend, which sends "$41.23" in the text and
    charges $41.23 on the paywall, and where those two disagreeing is precisely
    the tell the feature exists to remove.
    """
    parts: list[str] = []
    if lines:
        parts.append(_FAN_HEAD + "\n"
                     + "\n".join(f"  - {ln}" for ln in lines) + "\n\n"
                     + _STEER_RULE + "\n" + _ONE_DETAIL)
    if price_cents is not None:
        px = _money(int(price_cents))
        if was_cents and int(was_cents) > int(price_cents):
            parts.append(
                f"THE PRICE\nHe already saw this piece at {_money(int(was_cents))} "
                f"and did not buy. It is {px} now because you cut it for him. You "
                f"may name {px}; if you name a price it must be exactly {px}. Never "
                "beg and never apologise for the first price.")
        else:
            parts.append(f"THE PRICE\nIt unlocks at {px}. You may name it or not, "
                         f"but if you name a price it must be exactly {px}.")
    return ("\n\n" + "\n\n".join(parts)) if parts else ""


def style_for(seed: str, styles: tuple[str, ...]) -> str:
    """Which of the four caption styles this send uses.

    SEEDED, not rolled. Same fan, same media, same style — so a retried job, a
    re-run of the sweep or a replay in the simulator quotes him the same kind of
    line rather than a fresh personality each time. It is the same discipline
    `_pack_line` already applies to the script pack, minus the clock term: a pack
    line may vary within the minute, but a caption is bound to ONE piece of media
    and the pairing should be stable for as long as that pairing exists.
    """
    return random.Random(f"capstyle:{seed}").choice(styles)


# ── what the vault says about a set of media ────────────────────────

async def described_rows(account_id: str, media_ids) -> dict[int, VaultItem]:
    """media id → its vault row, for the ids that have something to write about.

    THE one read behind both callers below, and `describe_media._copy_source` is
    the only predicate for "described" — so the caption lane and the manifest lane
    can never disagree about which media is writable, and neither can drift from
    the describe sweep that filled the column.

    ONE query for however many ids: both callers run inside the reply loop, on a
    turn already holding a fan lease and two LLM calls.
    """
    ids = [int(m) for m in (media_ids or [])]
    if not ids:
        return {}
    async with get_session() as s:
        rows = (await s.execute(
            select(VaultItem).where(VaultItem.account_id == str(account_id),
                                    VaultItem.media_id.in_(ids))
        )).scalars().all()
        for r in rows:
            s.expunge(r)
    return {int(r.media_id): r for r in rows if dm._copy_source(r)}


def _first_described(rows: dict[int, VaultItem], media_ids) -> VaultItem | None:
    """The first described row IN THE CALLER'S OWN ORDER.

    Order matters and is not incidental. Media 1 is what the fan sees at the top of
    the locked box, so a caption written about media 4 describes something he cannot
    see until after he pays. Walking in order means the line and the thumbnail agree.
    """
    for mid in (media_ids or []):
        row = rows.get(int(mid))
        if row is not None:
            return row
    return None


async def hero_facts(account_id: str, offerable: dict[int, CatalogItem], *,
                     hero: dict[int, list[int]] | None = None) -> dict[int, str]:
    """item id → what a vision model actually saw in the media that item attaches.

    `hero` is the caller's ALREADY-BUILT hero map when it has one.
    `ai_chatter._drop_owned` builds one for the same shelf on the same turn and
    now returns it, and `ownership.hero_media_map` opens a session and queries
    `VaultItem` whenever previews exist — so re-deriving it here bought a
    duplicate round trip on the highest-volume engine. Passing None (a caller
    that never narrowed the shelf) builds it, which is why this is one function
    with an optional argument rather than two near-identical ones.

    Hero only, and first-described-wins within it. The hero set is the payoff
    (`ownership.hero_media_map` owns that ruling); describing a free preview frame
    would pitch him the part he can already see for nothing, which is the opposite
    of a tease. Items whose media was never described simply do not appear — the
    manifest falls back to `description_for_ai` for those rows alone, so a
    half-described vault degrades per-item instead of all-or-nothing.

    Best-effort: a failure here costs specificity, never the turn.
    """
    if not offerable:
        return {}
    try:
        if hero is None:
            hero = await ownership.hero_media_map(
                account_id,
                {int(it.id): (ownership.item_media(it), ownership.item_previews(it))
                 for it in offerable.values()})
        # Scoped to the shelf as it stands NOW — a hero map handed in from before a
        # later narrowing would otherwise widen the read for items nobody can be
        # offered.
        hero = {int(i): m for i, m in hero.items() if int(i) in offerable}
        if not hero:
            return {}
        rows = await described_rows(
            account_id, {m for ids in hero.values() for m in ids})
    except Exception:
        log.debug("ppv_caption manifest facts failed account=%s", account_id,
                  exc_info=True)
        return {}
    out: dict[int, str] = {}
    for iid, media in hero.items():
        row = _first_described(rows, media)
        if row is not None:
            # Collapsed to one line: the manifest is a bullet list and a
            # multi-line value under one bullet reads as a new item.
            out[int(iid)] = " ".join(
                dm._copy_source(row).split())[:MANIFEST_FACT_MAX]
    return out


# ── the line that reaches the wire ──────────────────────────────────

async def _him_context(account_id: str, fan_id: int,
                       spend_facts: list[str] | None) -> list[str]:
    """His facts, loaded here rather than threaded through two call sites.

    Both callers reach this from a job (`_run_post_buy`) or a watcher
    (`_maybe_discount_resend`) that holds a fan id and a config and nothing else —
    neither has the `Fan`/`FanProfile` pair the reply loop assembles. Loading them
    here keeps the diff at each site to one line and, more usefully, keeps "what a
    caption knows about a fan" answerable by reading ONE function.

    Best-effort: this is decoration on a line that already works, so a profile
    table that is empty, missing or slow degrades to a media-only caption rather
    than taking the send down with it.
    """
    try:
        async with get_session() as s:
            f = await s.get(Fan, (str(account_id), int(fan_id)))
            if f is not None:
                s.expunge(f)
            prof = (await s.execute(
                select(FanProfile).where(FanProfile.account_id == str(account_id),
                                         FanProfile.fan_id == int(fan_id)).limit(1)
            )).scalars().first()
            if prof is not None:
                s.expunge(prof)
        name = resolve_fan_name(f) if f is not None else ""
    except Exception:
        log.debug("ppv_caption fan context failed account=%s fan=%s",
                  account_id, fan_id, exc_info=True)
        return []
    return him_lines(f, prof, spend_facts, name=name or "")


async def caption_for(account_id: str, fan_id: int, media_ids: list[int], *,
                      voice: str = "her", price_cents: int | None = None,
                      was_cents: int | None = None,
                      spend_facts: list[str] | None = None,
                      style: str | None = None) -> str | None:
    """One caption for one priced 1:1 attach — or None, meaning "use your own line".

    None is not an error path, it is the DEFAULT path. Every miss returns None and
    `line_for` turns that back into the caller's canned line.
    """
    # THE GATE, and it is this read — not a config switch. `described_rows` filters
    # on `dm._copy_source`, the same predicate `_copy_call` refuses on, so a shelf
    # with nothing written about it costs ONE indexed query and stops here. Asked
    # FIRST, ahead of the config read below, because it is the miss that happens.
    item = _first_described(await described_rows(account_id, media_ids), media_ids)
    if item is None:
        log.debug("ppv_caption no described media account=%s fan=%s ids=%s",
                  account_id, fan_id, (media_ids or [])[:4])
        return None

    model = await dm.caption_model(account_id)
    if style is None:
        style = style_for(f"{account_id}:{fan_id}:{item.media_id}",
                          dm.CAPTION_STYLES)
    res = await dm._copy_call(
        # audience="1to1" drops `_CAPTION_MASS_AUDIENCE` — the mass lane's rule
        # that the writer knows nothing about who is reading. Here it does: the
        # body suffix below hands it his name, his kink and what he has spent,
        # and `_maybe_discount_resend` deliberately writes to what he already
        # saw. Leaving that clause in would forbid the whole point of this lane.
        account_id, item, dm.caption_style_prompt(style, voice, audience="1to1"),
        model,
        body_extra=body_suffix(
            await _him_context(account_id, fan_id, spend_facts),
            price_cents=price_cents, was_cents=was_cents),
        purpose=PURPOSE, fan_id=int(fan_id))
    if not res.get("ok"):
        log.info("ppv_caption %s account=%s fan=%s — keeping the canned line",
                 res.get("status"), account_id, fan_id)
        return None
    line = str((res.get("data") or {}).get("caption") or "").strip()
    if not line:
        log.debug("ppv_caption empty account=%s fan=%s style=%s",
                  account_id, fan_id, style)
        return None
    log.info("ppv_caption written account=%s fan=%s media=%s style=%s cost_mc=%s",
             account_id, fan_id, item.media_id, style, res.get("cost_millicents"))
    return line


async def line_for(base: str, cfg: dict, account_id: str, fan_id: int,
                   media: list[int], *, price_cents: int | None = None,
                   was_cents: int | None = None,
                   spend_facts: list[str] | None = None) -> str:
    """THE finished text for a priced 1:1 attach: caption-or-`base`, word-filtered,
    clipped. Both send paths call exactly this and send what comes back.

    It returns a FINISHED string rather than an optional caption on purpose. The
    word filter and the length clip are not the caller's business to remember — a
    model-written line is precisely the input `apply_word_restriction` exists for,
    and a caption that skipped it would be the only outbound text on the account
    that had. When both sites had to remember to apply it themselves, the only
    thing holding that invariant was a test that grepped `ai_chatter.py` for
    statement adjacency. Now the pipeline cannot be assembled wrongly.

    `base` is computed by the caller FIRST and passed in, so the canned line is
    provably still there on every path out of here.

    The bare `except` is deliberate and belongs here and nowhere near it in the
    send paths. A caption is the one thing in this call chain with no consequence
    when it is missing — the pack line ships, the media attaches, the price is
    unchanged and the offer is recorded exactly as before. Letting an unexpected
    error out of a decorative call abort a priced send would trade a nicer
    sentence for the sale.
    """
    written = ""
    if enabled(cfg) and media:
        try:
            written = await caption_for(
                account_id, fan_id, list(media),
                voice=cfg.get("_account_voice", "her"),
                price_cents=price_cents, was_cents=was_cents,
                spend_facts=spend_facts) or ""
        except Exception:
            log.warning("ppv_caption failed account=%s fan=%s — keeping the "
                        "canned line", account_id, fan_id, exc_info=True)
    # STRIPPED, not just truthy. `"   "` is a true string and would ship as the
    # caption on a priced attach — a paywall with a blank line over it, which is
    # worse than the canned line by every measure.
    return apply_word_restriction(written.strip() or base)[:_REPLY_MAX_CHARS]
