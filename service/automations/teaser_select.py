"""service/automations/teaser_select.py — teaser SELECTION library.

Split out of tip_reward.py by PURE CODE MOTION (2026-07-23): the hot-thread
teaser and the conversational teaser ladder are consumed by ai_chatter (which
owns the trigger and the SEND), not by the tip_reward automation's own run().
Every consumer imports THIS module directly — ai_chatter, fans.py and
test_tip_reward. tip_reward no longer imports it at all.

NOT an automation: no `@register` — nothing in here sends. The plugin scan may
import this module; it only defines selection helpers.

What is left here is teaser POLICY: which rung a fan is on, what a bundle of
that rung costs, when the next one is due, and what it is priced at. The
mechanics it is written on top of live elsewhere and are imported:

  _vault_pick          folder names → fresh media ids (the pull kernel)
  ownership            seen_media — what this fan has already been sent
  tip_reward_config    _DEFAULTS / _load_config / tier_folders /
                       rung_folder_slot (the config shape)
  _slot_cost           what one item is worth against a slot budget

Those four had all grown INSIDE this module, which is how `make_right` — an
apology engine with no teaser in it — ended up importing four private names from
"teaser_select". Every import above is top-level and cycle-free: the mechanics
know nothing about teasers, so nothing points back here.
"""
from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timedelta

from automations import tip_ladder
from automations._slot_cost import (PER_ITEM, PHOTOS_ONLY, SlotCost,
                                    cents_per_slot, video_slot_cost)
# The folder→media kernel is NOT a teaser concept — it moved to its own leaf once
# tip_reward, make_right and welcome_chatter all turned out to need it. Imported
# for USE only: a caller that wants the kernel imports the kernel, so there is no
# re-export chain making "teaser_select" look like the home of a vault read.
from automations._vault_pick import (
    folder_list, gather_unseen, pull_stages, resolve_folders,
)
from ownership import seen_media
from automations.upsell import OF_PRICE_FLOOR_CENTS
from automations.fan_state import fan_state, put_fan_state
from automations.tip_reward_config import _load_config, rung_folder_slot, tier_folders
from db.engine import get_session
from db.models import Fan, VaultSend

# Same logger name as tip_reward — this module was split out of it by pure code
# motion and every emitted log record must stay byte-identical.
log = logging.getLogger("of-relay.automation.tip_reward")


def _jitter_band(tcfg: dict) -> tuple[float, float, float, float, float]:
    """(cut_lo, cut_hi, chance, raise_lo, raise_hi) — the no-buy jitter model,
    normalized ONCE (bands sorted, chance clamped). Both the sender's roll and the
    forecast's band ends come from here, so a default can never drift between them."""
    lo = float(tcfg.get("cut_lo") or 0.40)
    hi = float(tcfg.get("cut_hi") or 0.60)
    if lo > hi:
        lo, hi = hi, lo
    chance = max(0.0, min(1.0, float(tcfg.get("raise_chance") or 0.0)))
    r_lo = float(tcfg.get("raise_lo") or 1.4)
    r_hi = float(tcfg.get("raise_hi") or 1.6)
    if r_lo > r_hi:
        r_lo, r_hi = r_hi, r_lo
    return lo, hi, chance, r_lo, r_hi


def _jitter_frac(band: tuple[float, float, float, float, float], rv: float) -> float:
    """ONE roll → the no-buy multiplier. `rv` below `chance` lands in the raise band
    (the bounce); the rest of the interval maps across the cut band. A single number
    deciding both the leg and the spot keeps the injectable-`rand` test seam intact."""
    lo, hi, chance, r_lo, r_hi = band
    rv = max(0.0, min(1.0, rv))
    if rv < chance:
        return r_lo + (r_hi - r_lo) * (rv / chance)
    span = (rv - chance) / (1.0 - chance) if chance < 1.0 else 0.0
    return lo + (hi - lo) * span


async def _pull_rung(client, *, tcfg: dict, rung: dict, seen: set[int],
                     cost: SlotCost) -> tuple[list[str], list[int]]:
    """A rung's OWN folders → up to its count of unseen media. Returns
    `(folders, media_ids)`; empty ids means the rung is dead (no folders, or the
    fan has seen everything they hold) and the caller sends nothing.

    Both ladder paths pull exactly this way — the legacy climb, and the adaptive
    no-tiers fallback — and they had grown two copies of it, which is how the
    videos flag reached one of them a change later than the other.

    Folders are scanned IN ORDER and deduped across each other (`gather_unseen`),
    so listing a second folder deepens a rung rather than replacing it: the first
    is exhausted before the next is touched.

    `rung` is a NORMALISED tcfg rung — `convo_teaser_config` already ran the stored
    slot through `rung_folder_slot`/`folder_list` — which is why this reads
    `folders` plainly instead of coercing again."""
    folders = list(rung.get("folders") or [])
    if not folders:
        return [], []
    by_name = await asyncio.to_thread(resolve_folders, client)
    ids = await asyncio.to_thread(gather_unseen, client, folders, by_name, seen,
                                  _rung_count(tcfg, rung), cost=cost)
    return folders, ids


# ── Hot-thread proactive teaser ──────────────────────────────────────────────
# ai_chatter owns the trigger (thread_heat) and the SEND (it attaches the media to
# the reply it is already composing). These three helpers are the only tip_reward
# surface it touches: a cheap enabled-gate, a pure SELECTION (no send), and a
# post-send record. State lives in fans.custom_fields['_hot_teaser'] = {'at': iso,
# 'free_sent': N} — same AI-owned JSON column, leading-"_" namespace, no migration,
# mirroring the '_image_reply' cooldown in tip_reward.
_HOT_TEASER_STATE_KEY = "_hot_teaser"


def _bundle_sizing(cfg: dict) -> tip_ladder.BundleSizing:
    """THE one place a config becomes a photo count.

    `dollars_per_image`, `min_images` and `max_images` are the only sizing numbers
    that exist: they are what the Tip Reward tab renders and the only ones
    `tip_reward_config_api._validate` persists. There deliberately are no separate
    bundle_* knobs — a set of those used to sit in `_DEFAULTS` with no branch in
    that allowlist, so no save could ever reach them and the teaser quietly sized
    on a $10/photo ladder nobody had set while the visible field said $5. One
    number, one meaning: change "$ per image" and every priced send re-sizes.

    `free_photos` tracks `min_images` so a free tease never looks stingier than the
    paid floor it is bait for; 0 means no free tease, on every caller."""
    lo = max(0, int(cfg.get("min_images") or 0))
    return tip_ladder.BundleSizing(
        cents_per_photo=cents_per_slot(cfg),
        min_photos=max(1, lo),
        max_photos=max(1, int(cfg.get("max_images") or 1), lo),
        free_photos=lo,
    )


def _rung_count(tcfg: dict, rung: dict) -> int:
    """Photos for a SINGLE-FOLDER rung pull (no tiers to compose from) — SLOTS once
    "Videos too" is on for the ladder, so a rung whose folder is all clips needs this
    number (or the tab's Minimum) raised to fit more than the shortest one.

    Per-rung `count` wins ($10→1, $30→3, $50→5), else the ladder-wide
    `teaser_convo_count` — but never below the tab's Minimum images. Both the
    legacy ladder and the adaptive no-tiers fallback size a rung this way, and
    they must agree: the fallback swaps the photo SOURCE, not the floor."""
    return max(max(1, int(tcfg["sizing"].min_photos)),
               int(rung.get("count") or 0) or int(tcfg.get("count") or 1))


async def hot_teaser_config(account_id: str) -> dict | None:
    """The hot-teaser knobs iff enabled, else None — one config read for ai_chatter's
    per-run setup (None ⇒ don't even look per fan). Independent of the tip `enabled`
    master switch: warming a hot thread with vault media has nothing to do with
    rewarding tips."""
    cfg = await _load_config(account_id)
    if not cfg.get("hot_teaser_enabled"):
        return None
    return {
        "count": max(1, int(cfg.get("hot_teaser_count") or 1)),
        "cooldown_hours": max(0, int(cfg.get("hot_teaser_cooldown_hours") or 0)),
        "free_folder": str(cfg.get("hot_teaser_free_folder") or "").strip(),
        "free_max": max(0, int(cfg.get("hot_teaser_free_max") or 0)),
        "paid_folder": str(cfg.get("hot_teaser_paid_folder") or "").strip(),
        "price_cents": max(0, int(cfg.get("hot_teaser_price_cents") or 0)),
        # Price-scaled bundle (default-off; pick_hot_teaser reads it).
        "bundle_scaling_enabled": bool(cfg.get("bundle_scaling_enabled")),
        "sizing": _bundle_sizing(cfg),
        # Tier→folder mapping for the composed bundle (reuse the tiers config):
        # premium = the 'premium' tier's folders, normal = basic+mid, free = the
        # hot-teaser free folder. Empty lists → that tier contributes nothing.
        "bundle_premium_folders": tier_folders(cfg, "premium"),
        "bundle_normal_folders": tier_folders(cfg, "basic", "mid"),
        "bundle_free_folders": [f for f in [str(cfg.get("hot_teaser_free_folder") or "").strip()] if f],
    }


def _compose_bundle_ids(client, plan, seen: set[int],
                        premium_folders: list[str], normal_folders: list[str],
                        free_folders: list[str], *,
                        cost: SlotCost = PER_ITEM) -> tuple[list[int], dict]:
    """Teaser adapter over pull_stages: pull the plan's premium/normal/free photo
    ids from their tier folders (dedup + backfill live in pull_stages) and keep
    the breakdown/weight shape the teaser callers report. Sync — call via
    to_thread.

    `cost` rides straight through (see `gather_unseen`): under the rate-card cost
    each stage's plan number is a SLOT budget, so one clip can consume a whole
    stage. The breakdown stays an ITEM count — it is what the caller logs, and
    "3 premium" reads truer than "3 slots of premium"."""
    (prem, norm, free), extras = pull_stages(
        client, [(premium_folders, plan.premium), (normal_folders, plan.normal),
                 (free_folders, plan.free)], seen, cost=cost,
        repeat_ok={2})  # the free tier is tease filler — may repeat, never blocks
    out = prem + norm + free + extras
    weight = len(prem) * tip_ladder.WEIGHT_PREMIUM + len(norm) * tip_ladder.WEIGHT_STANDARD
    return out, {"premium": len(prem), "normal": len(norm), "free": len(free),
                 "total": len(out), "weight": weight}


async def pick_hot_teaser(client, account_id: str, fan_id: int, *,
                          lifetime_spend_cents: int, tcfg: dict,
                          now: datetime | None = None) -> dict | None:
    """Pure SELECTION for ai_chatter's hot-thread teaser — resolves the spend branch,
    the per-fan cooldown/free-cap, and the unseen vault items. Returns
    {media_ids, price_cents, is_free, folders} or None (throttled / capped / no
    folder / nothing unseen left). `folders` names WHERE the media came from — empty
    when the bundle was composed from the tier folders rather than pulled from one.
    It is a record/diagnostic field; nothing branches on it.

    Sends NOTHING and writes NOTHING: ai_chatter attaches the media to its reply and
    calls `record_hot_teaser` only once the send confirms — so a dropped/failed reply
    never burns the cooldown or the fan's free allowance."""
    now = now or datetime.utcnow()
    async with get_session() as s:
        fan = await s.get(Fan, (str(account_id), int(fan_id)))
    state = fan_state(fan, _HOT_TEASER_STATE_KEY)
    state = state if isinstance(state, dict) else {}

    cd = int(tcfg.get("cooldown_hours") or 0)
    if cd > 0 and state.get("at"):
        try:
            if (now - datetime.fromisoformat(str(state["at"]))) < timedelta(hours=cd):
                return None
        except Exception:
            pass  # unparseable stamp → treat as no cooldown, re-stamp on this send

    # Spend branch. A fan who has PAID (lifetime > 0) is a proven buyer → priced tease
    # PPV. A $0 fan gets a FREE warm-up, but only `free_max` of them across his life so
    # a perpetual free-loader can't drain the folder one hot thread at a time.
    is_free = int(lifetime_spend_cents or 0) <= 0
    if is_free:
        if int(state.get("free_sent") or 0) >= int(tcfg.get("free_max") or 0):
            return None
        folder = str(tcfg.get("free_folder") or "").strip()
        price_cents = 0
    else:
        folder = str(tcfg.get("paid_folder") or "").strip()
        price_cents = int(tcfg.get("price_cents") or 0)

    seen = await seen_media(account_id, fan_id)
    scaling = bool(tcfg.get("bundle_scaling_enabled"))
    plan = tip_ladder.bundle_plan(price_cents, tcfg["sizing"]) if scaling else None

    # PAID + scaling: compose premium/normal/free from the tier folders (a
    # full-looking set whose value is concentrated in the premium shots). Does
    # NOT require the single paid_folder — the tiers config supplies the media.
    if scaling and not is_free:
        if plan.total <= 0:
            return None
        media_ids, breakdown = await asyncio.to_thread(
            _compose_bundle_ids, client, plan, seen,
            list(tcfg.get("bundle_premium_folders") or []),
            list(tcfg.get("bundle_normal_folders") or []),
            list(tcfg.get("bundle_free_folders") or []),
        )
        if not media_ids:
            return None
        return {"media_ids": media_ids, "price_cents": int(price_cents),
                "is_free": False, "folders": [], "bundle": breakdown}

    # Otherwise a single-folder pull. Count is the legacy fixed value, or the
    # bundle TOTAL when scaling is on (the free branch has no tiers to compose).
    if not folder:
        return None
    if scaling:
        count = plan.total
        if count <= 0:  # free_count=0 → nothing to send
            return None
    else:
        count = max(1, int(tcfg.get("count") or 1))
    by_name = await asyncio.to_thread(resolve_folders, client)
    media_ids = await asyncio.to_thread(gather_unseen, client, [folder], by_name, seen, count)
    if not media_ids:
        return None
    return {"media_ids": media_ids, "price_cents": int(price_cents),
            "is_free": is_free, "folders": [folder]}


async def record_hot_teaser(account_id: str, fan_id: int, *, media_ids: list[int],
                            message_id: int | None, price_cents: int, is_free: bool,
                            set_rung: int | None = None,
                            unbought: int | None = None,
                            now: datetime | None = None) -> None:
    """After the teaser media actually went out on ai_chatter's reply: one VaultSend
    per item (so the unseen filter never re-attaches it) and the per-fan cooldown +
    free-counter bump, in ONE transaction (a crash between them would let the unseen
    filter re-attach OR the cap re-trip). Mirrors `_run_image_reply`'s batching.
    `set_rung` (convo ladder) stamps the fan's NEXT rung so the price climbs."""
    now = now or datetime.utcnow()
    async with get_session() as s:
        for mid in media_ids:
            s.add(VaultSend(account_id=str(account_id), fan_id=int(fan_id),
                            media_id=int(mid),
                            message_id=int(message_id) if message_id else None,
                            price_cents=int(price_cents or 0), sent_at=now))
        fan = await s.get(Fan, (str(account_id), int(fan_id)))
        if fan is not None:
            st = fan_state(fan, _HOT_TEASER_STATE_KEY)
            st["at"] = now.isoformat()
            if is_free:
                st["free_sent"] = int(st.get("free_sent") or 0) + 1
            if set_rung is not None:
                st["rung"] = int(set_rung)
            # Adaptive convo-teaser buy-detection: remember the exact ask + its
            # message so next turn can ask "did THIS teaser sell?" (Message.is_paid
            # on this id) — scoped to her own teaser, never an ai_chatter catalog buy.
            st["last_price"] = int(price_cents or 0)
            st["last_msg"] = int(message_id) if message_id else None
            st["last_free"] = bool(is_free)
            # The last teaser that carried a PRICE, kept across free sends. A $0 bait
            # overwrites last_msg with a message that can never be "sold", which would
            # otherwise blind the ladder to a LATE unlock of the priced ask underneath
            # it — see `teaser_sale_check_msg`.
            if int(price_cents or 0) > 0 and message_id:
                st["last_paid_msg"] = int(message_id)
            # Consecutive PRICED teasers he has not unlocked — the circuit breaker's
            # numerator. Supplied by the caller (pick_convo_teaser resolved it against
            # the sale signal); None leaves it untouched for the hot-teaser lane.
            if unbought is not None:
                st["unbought"] = int(unbought)
            put_fan_state(fan, _HOT_TEASER_STATE_KEY, st)


def teaser_state(fan: Fan | None) -> dict:
    """The fan's teaser state ({at, free_sent, rung}) parsed off the Fan row ai_chatter
    already has in hand — no extra DB read. `at` (iso) is the last teaser, `rung` the
    convo-ladder position."""
    return fan_state(fan, _HOT_TEASER_STATE_KEY)


def teaser_sale_check_msg(state: dict) -> int | None:
    """WHICH of her teaser messages the ladder should ask `is_paid` about next turn —
    or None when there is no sale worth querying. Pure; the caller runs the query.

    Normally that is simply her last teaser. But the free BAIT leg sends a $0 message,
    and a $0 message can never be unlocked, so on the turn after a bait the obvious
    read (`last_msg`) asks a question whose answer is always False — and the PRICED ask
    underneath it silently stops being watched. That ask is still open and still
    unlockable: he can pay for it hours later, and it can carry any rung price on the
    ladder, not just a floor tease. Missing it would
    keep his unbought streak climbing toward the circuit breaker after he had paid.

    So: last_price > 0 → her last teaser; otherwise `last_paid_msg`, the last one that
    carried a price, which `record_hot_teaser` keeps across free sends.

    (A ladder whose rungs put a $0 rung ABOVE rung 0 could climb twice off one such
    late sale, because the climb it triggers would itself be a free send that leaves
    `last_paid_msg` in place. It terminates — the next climb is priced — and no
    authored ladder has that shape: free is the opening rung.)"""
    if int(state.get("last_price") or 0) > 0:
        return int(state.get("last_msg") or 0) or None
    return int(state.get("last_paid_msg") or 0) or None


def teaser_last_at(state: dict) -> datetime | None:
    """When the last teaser went out, or None. The `at` field is written here as an
    ISO string, so it is READ here too — ai_chatter had grown its own inline
    `datetime.fromisoformat` over this module's storage format."""
    raw = state.get("at")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw))
    except Exception:
        return None


async def convo_teaser_config(account_id: str) -> dict | None:
    """The conversational-ladder knobs iff enabled, else None — one config read for
    ai_chatter's per-run setup. Each rung is {folders, price_cents, count}; a rung
    with no folders is a dead step (skipped at selection)."""
    cfg = await _load_config(account_id)
    if not cfg.get("teaser_convo_enabled"):
        return None
    videos = bool(cfg.get("teaser_convo_videos"))
    rungs = []
    for r in (cfg.get("teaser_convo_rungs") or []):
        if not isinstance(r, dict):
            continue
        # Per-rung image `count` (e.g. $10→1, $30→3, $50→5). 0/absent → fall back to
        # the ladder-wide teaser_convo_count.
        # `folders` (a list) is the stored shape; `folder` is the single-string one
        # it grew out of. `folder_list` takes either, so no account needed migrating.
        rungs.append({"folders": folder_list(rung_folder_slot(r)),
                      "price_cents": max(0, int(r.get("price_cents") or 0)),
                      "count": max(0, int(r.get("count") or 0))})
    return {
        "after": max(1, int(cfg.get("teaser_convo_after_fan_msgs") or 20)),
        "count": max(1, int(cfg.get("teaser_convo_count") or 1)),
        "rungs": rungs,
        # Operator override: run the ladder past the companion/bot-accused/broke
        # brakes (manual stops still apply upstream). See _DEFAULTS for the risk.
        "ignore_brakes": bool(cfg.get("teaser_convo_ignore_brakes")),
        # Adaptive (buy-aware climb / jitter) + the weighted-bundle knobs it shares
        # with the hot teaser. All inert unless teaser_convo_adaptive is on.
        # DIRECT key access for the numeric knobs (like the breaker pair below):
        # `_load_config` merges _DEFAULTS, so the keys always exist, and an explicit
        # stored 0 (raise_chance off-switch, floor_cents → $3 wire floor) must
        # survive — a `.get(...) or DEFAULT` would silently eat it.
        "adaptive": bool(cfg.get("teaser_convo_adaptive")),
        "cut_lo": float(cfg["teaser_convo_cut_lo"]),
        "cut_hi": float(cfg["teaser_convo_cut_hi"]),
        "floor_cents": max(0, int(cfg["teaser_convo_floor_cents"])),
        "raise_chance": max(0.0, min(1.0, float(cfg["teaser_convo_raise_chance"]))),
        "raise_lo": float(cfg["teaser_convo_raise_lo"]),
        "raise_hi": float(cfg["teaser_convo_raise_hi"]),
        # Does a PROVEN buyer get the free bait leg too (floor ↔ free) instead of
        # holding on one repeated number? See _DEFAULTS for the volume caveat.
        "bait_for_buyers": bool(cfg.get("teaser_convo_bait_for_buyers")),
        # Escalation ×step off a SOFTENED/floor buy (never off the rung list). See _DEFAULTS.
        "climb_step": max(1.0, float(cfg.get("teaser_convo_climb_step") or 2.0)),
        # Circuit breaker on consecutive unbought priced teasers. See _DEFAULTS.
        # DIRECT key access: `_load_config` merges _DEFAULTS and drops stored Nones, so
        # both keys are always present — a `.get(... ) or DEFAULT` here would be dead
        # code that also silently ate a deliberate 0 ("never release on time alone").
        "max_unbought": max(0, int(cfg["teaser_convo_max_unbought"])),
        "unbought_reset_h": max(0, int(cfg["teaser_convo_unbought_reset_h"])),
        "sizing": _bundle_sizing(cfg),
        "bundle_premium_folders": tier_folders(cfg, "premium"),
        "bundle_normal_folders": tier_folders(cfg, "basic", "mid"),
        "bundle_free_folders": [f for f in [str(cfg.get("hot_teaser_free_folder") or "").strip()] if f],
        # Clips ride the rungs (and cost slots off the ask) — the flag only, NOT a
        # built coster: `/ai-status` calls this config on every strip poll and never
        # pulls media, and the coster is a whole vault read. `pick_convo_teaser` builds
        # it when it is about to spend it.
        "videos": videos,
    }


def convo_teaser_floors(tcfg: dict, *, max_paid_cents: int) -> tuple[int, int]:
    """`(floor, bait_floor)` — the bottom of his ladder, and the bottom of the free
    BAIT leg. ONE resolution, used by both the sender and the forecast so the drawer
    can never quote a floor the engine would not honour.

    The floor is ONE static number for everyone (`floor_cents`, default $10) —
    fluctuate-down pricing (operator ruling 2026-08-19, see _DEFAULTS): a stalled ask
    decays whether he has paid or not, and at the floor the ladder alternates
    floor ↔ free. This supersedes the 08-01 set-price floor for THIS lane only; a
    proven buyer's history still shapes the CATALOG lane (`upsell.next_price`).

    What still turns on HAS HE EVER PAID is the bait leg: a never-buyer always
    alternates floor ↔ free (the acquisition mechanic), a proven buyer only when
    `teaser_convo_bait_for_buyers` is on — off, his bait_floor equals the floor and
    the number repeats until the breaker trips.

    Returning both from one call is the point: they are two views of one policy, and
    when the sender and the forecast each derived them separately they could disagree.

    NEVER below `OF_PRICE_FLOOR_CENTS`. That is not defensive decoration — the floor
    used to be `0.38 × max_paid`, which for a $3 buyer is $1.14, a price OF will not
    accept. The send site clamped it up to $3 without telling the ladder, so the stored
    state drifted to numbers the fan never saw (1¢ on two prod fans) and
    `round(1 × 0.7) == 1` made the decay a FIXED POINT that could never reach the
    floor↔free oscillation. A floor the wire can't honour is not a floor."""
    floor = max(OF_PRICE_FLOOR_CENTS, int(tcfg.get("floor_cents") or 0))
    if int(max_paid_cents or 0) > 0:
        return floor, (0 if tcfg.get("bait_for_buyers") else floor)
    return floor, 0


def _next_convo_teaser_price(*, idx: int, prices: list[int], last_px: int,
                             last_sold: bool, last_was_free: bool,
                             floor: int, bait_floor: int,
                             step: float, frac: float) -> tuple[int, int, bool]:
    """PURE pricing policy for the adaptive convo ladder — no I/O. Given where he is
    (idx, the ladder prices) and what happened to the last teaser, return
    (new_rung, price_cents, softened). `floor`/`bait_floor`/`frac`/`step` are
    pre-resolved by the caller (the floors from `convo_teaser_floors`, frac = the
    jittered cut, step = climb ×mult).

      • first ever            → the opening rung-0 tease
      • bought a SOFTENED ask → escalate off what he PAID × step (capped, rung tracks
                                to the first rung ≥ the new ask); never the rung list
                                he already refused
      • full-price sale / the
        opening free tease     → climb the configured ladder one rung
      • no buy                → jitter ×frac. Usually frac < 1 (a cut toward the
                                floor, then to `bait_floor`: $0 ⇒ the ladder
                                alternates floor ↔ free bait; a `bait_floor` equal
                                to the floor ⇒ he holds on one number. Which one a
                                PROVEN buyer gets is `teaser_convo_bait_for_buyers`;
                                a never-buyer always alternates). One roll in
                                `raise_chance` the caller hands a frac > 1 — the
                                BOUNCE — and the ask goes UP instead, capped at the
                                ladder top so a lucky streak can't leave the ladder.
    """
    if last_px <= 0 and idx == 0 and not last_sold and not last_was_free:
        return 0, prices[0], False               # first ever
    if last_sold and 0 < last_px < prices[idx]:  # softened buy → climb off what he PAID
        price = min(prices[-1], int(round(last_px * step)))
        new_idx = next((i for i, p in enumerate(prices) if p >= price), len(prices) - 1)
        return new_idx, price, False
    if last_sold or (last_was_free and idx == 0):  # full-price sale / opening free → climb
        new_idx = min(idx + 1, len(prices) - 1)
        return new_idx, prices[new_idx], False
    # No buy → jitter (a cut, or the bounce), landing on WHOLE DOLLARS — a $20.91
    # lock reads like a bot (the retired tip ladder's human-rounding lesson, kept
    # minimal). The climb branches above stay exact: they escalate off what he PAID.
    cand = int((last_px * frac + 50) // 100) * 100
    if frac > 1.0 and prices and max(prices) > 0:
        cand = min(cand, max(prices))            # a raise never exceeds the ladder top
    if cand >= floor:        price = cand        # still above the floor → decay
    elif last_px > floor:    price = floor       # first dip below → land ON floor
    elif last_px == floor:   price = bait_floor  # at the floor → bait ($0, or hold)
    else:                    price = floor       # was $0 bait → bounce to floor
    return idx, price, True


def convo_teaser_forecast(*, tcfg: dict, state: dict, msgs_since: int,
                          last_sold: bool = False, max_paid_cents: int = 0) -> dict:
    """What the convo teaser will do NEXT, and how many of his messages away it is.

    Pure and I/O-free — it runs `_next_convo_teaser_price`, the same policy the
    sender runs, so the drawer can never quote a price the engine would not send.
    It deliberately stops short of `pick_convo_teaser`: choosing the PHOTOS costs
    OF folder resolution and a `VaultSend` dedup read, and a status endpoint must
    not pay that (nor warm caches) just to render a countdown.

    The no-buy move is jittered — a cut (`cut_lo`..`cut_hi`) or, one roll in
    `raise_chance`, a bounce UP (`raise_lo`..`raise_hi`) — so on a stall the next ask
    is a RANGE, not a number. Every band end is evaluated: `cents` is the lowest
    outcome and `cents_max` the highest, equal whenever the outcome doesn't depend on
    the roll. Reporting one jittered sample as "the" next ask would be a number the
    operator could watch the engine contradict.
    """
    rungs = tcfg.get("rungs") or []
    after = max(1, int(tcfg.get("after") or 20))
    since = max(0, int(msgs_since or 0))
    idx = max(0, min(int(state.get("rung") or 0), max(0, len(rungs) - 1)))
    out: dict = {
        "after": after,
        "msgs_since": since,
        "remaining": max(0, after - since),
        "rung": idx if rungs else None,
        "rungs": len(rungs),
        "adaptive": bool(tcfg.get("adaptive")),
        "cents": None, "cents_max": None, "softened": None,
    }
    if not rungs:
        return out

    prices = [max(0, int((r or {}).get("price_cents") or 0)) for r in rungs]
    if not tcfg.get("adaptive"):
        out["cents"] = out["cents_max"] = prices[idx]
        out["softened"] = False
        return out

    last_px = max(0, int(state.get("last_price") or 0))
    floor, bait_floor = convo_teaser_floors(tcfg, max_paid_cents=max_paid_cents)
    lo, hi, chance, r_lo, r_hi = _jitter_band(tcfg)
    fracs = (lo, hi) + ((r_lo, r_hi) if chance > 0 else ())
    step = float(tcfg.get("climb_step") or 2.0)
    ends = [
        _next_convo_teaser_price(
            idx=idx, prices=prices, last_px=last_px, last_sold=last_sold,
            last_was_free=bool(state.get("last_free")), floor=floor,
            bait_floor=bait_floor, step=step, frac=f)
        for f in fracs
    ]
    out["cents"] = min(e[1] for e in ends)
    out["cents_max"] = max(e[1] for e in ends)
    out["softened"] = any(e[2] for e in ends)
    return out


async def pick_convo_teaser(client, account_id: str, fan_id: int, *, tcfg: dict,
                            state: dict, msgs_since_last: int,
                            last_sold: bool = False, max_paid_cents: int = 0,
                            rand: float | None = None,
                            now: datetime | None = None) -> dict | None:
    """SELECTION for the conversational ladder. Fires only once he has sent `after`
    messages since his last teaser.

    LEGACY (tcfg['adaptive'] falsy): picks the CURRENT rung's single folder + price;
    climbs one rung every send. Byte-for-byte the old behavior.

    Both paths obey tcfg['videos'] ("Videos too" for this ladder): off = photos only,
    on = clips ride and cost slots off the ask (see `cost` below).

    ADAPTIVE (tcfg['adaptive'] on): the ladder ($0/$10/$40/$80/$120/$160/$200)
    moves on WHAT HAPPENED to the last teaser — climb one rung if it SOLD (or was a
    free tease he received), else JITTER: usually a cut to 40–60% of the last ask
    down to the floor `convo_teaser_floors` resolves, one roll in four a raise of
    40–60% capped at the ladder top, holding the rung. Photos come from a price-scaled
    WEIGHTED BUNDLE (bundle_plan → premium/normal/free tier folders), free rung → a
    free taste.

    `state` is the fan's `_hot_teaser` slot (`teaser_state(fan)`) — the caller already
    holds it, and rung / last_price / last_free / unbought / at are all fields OF it.
    Unpacking them into separate parameters only created five chances for the sender
    and `convo_teaser_forecast` (which has always taken `state`) to disagree. The two
    facts that genuinely are NOT in the slot stay explicit: `last_sold` costs a query
    against her own teaser message, and `max_paid_cents` is his payment history.
    `rand` is injectable for deterministic tests.

    Returns {media_ids, price_cents, is_free, folders, rung, next_rung, convo:True
    (+softened/bundle in adaptive)} or None. `folders` is the same record field the
    hot teaser returns: the rung's own folders, or empty when the bundle came from
    the tier folders instead."""
    rungs = tcfg.get("rungs") or []
    if not rungs or msgs_since_last < int(tcfg.get("after") or 0):
        return None

    # ── CIRCUIT BREAKER ──────────────────────────────────────────────────────────
    # He has ignored `max_unbought` priced teasers in a row. Stop selling to him; the
    # reply still goes out, just without a paywall bolted to it. THE WHOLE RULE LIVES
    # HERE: he is released by a purchase, or by `unbought_reset_h` of quiet — because
    # once we stopped asking, the streak stopped being evidence about him.
    #
    # This gate has to exist BEFORE the pricing, not inside it. The old design's only
    # answer to a non-buyer was a cheaper ask, so there was always one more thing to
    # send and the loop had no exit — 85 consecutive ignored locks to one prod fan.
    # Fluctuate-down (08-19) reintroduces the ever-cheaper ask, so this stop is the
    # one guarantee the wall ends; at the floor the engine would otherwise alternate
    # floor ↔ free forever.
    unbought = int(state.get("unbought") or 0)
    reset_h = int(tcfg["unbought_reset_h"])
    last_at = teaser_last_at(state)
    if unbought and reset_h and last_at is not None:
        if (now or datetime.utcnow()) - last_at >= timedelta(hours=reset_h):
            unbought = 0
    max_unbought = int(tcfg["max_unbought"])
    if max_unbought > 0 and not last_sold and unbought >= max_unbought:
        return None

    # Clips. OFF (the default, and what a tcfg built without the key means) drops
    # video/gif at every folder scan below — the ladder is photos-only. ON admits them
    # and turns each pull's `count` into a SLOT budget: the coster charges a clip what
    # the rate card says it is worth in photos, so several short clips ride a big rung
    # while one that costs more than the ask is SKIPPED — the budget is a ceiling,
    # never trimmed after the fact.
    #
    # Built HERE, below the threshold and breaker returns, because it is a whole-vault
    # read and this function is called for every fan in the run — most of whom are not
    # being teased this turn. Async (and pure afterwards) so it can cross into the
    # to_thread scans below.
    cost = (await video_slot_cost(account_id, tcfg["sizing"].cents_per_photo)
            if tcfg.get("videos") else PHOTOS_ONLY)

    rung = int(state.get("rung") or 0)
    if not tcfg.get("adaptive"):
        idx = max(0, min(rung, len(rungs) - 1))
        r = rungs[idx]
        price_cents = max(0, int(r.get("price_cents") or 0))
        seen = await seen_media(account_id, fan_id)
        folders, media_ids = await _pull_rung(client, tcfg=tcfg, rung=r, seen=seen,
                                              cost=cost)
        if not media_ids:
            return None
        return {"media_ids": media_ids, "price_cents": int(price_cents),
                "is_free": price_cents == 0, "folders": folders, "rung": idx,
                "next_rung": min(idx + 1, len(rungs) - 1), "convo": True}

    # ── ADAPTIVE: buy-aware climb / jitter, price-scaled weighted bundle ──
    # Resolve the scalars the PURE pricing policy needs, then delegate. The floor is
    # the static `floor_cents` (default $10) for everyone (`convo_teaser_floors`);
    # frac is the jittered no-buy move (`_jitter_frac` — a cut, or the bounce).
    idx = max(0, min(rung, len(rungs) - 1))
    prices = [max(0, int((r or {}).get("price_cents") or 0)) for r in rungs]
    last_px = max(0, int(state.get("last_price") or 0))
    floor, bait_floor = convo_teaser_floors(tcfg, max_paid_cents=max_paid_cents)
    frac = _jitter_frac(_jitter_band(tcfg),
                        random.random() if rand is None else float(rand))
    new_idx, price, softened = _next_convo_teaser_price(
        idx=idx, prices=prices, last_px=last_px, last_sold=last_sold,
        last_was_free=bool(state.get("last_free")), floor=floor,
        bait_floor=bait_floor, step=float(tcfg.get("climb_step") or 2.0), frac=frac)

    # ONE clamp, HERE, before this number is used for anything. Free ($0) stays free;
    # any priced ask is raised to what OF will actually accept.
    #
    # The clamp used to live only at ai_chatter's send site, so the price on the wire
    # and the price written back to `_hot_teaser.last_price` / `vault_sends` were
    # different numbers — and the ladder then computed its NEXT move from the one the
    # fan never saw. Prod on 2026-08-01: fan FAN_ID was charged $3.00 twice while
    # his state recorded 114, and vault_sends holds 114/3/2/1 for asks that all went
    # out at $3.00. Clamping downstream of the decision means the decision is made on
    # fiction; clamping here means every consumer sees the same, sendable number.
    if 0 < price < OF_PRICE_FLOOR_CENTS:
        price = OF_PRICE_FLOOR_CENTS

    # The streak the breaker reads next turn. A SALE clears it outright; free bait is
    # not a failed ask and does not advance it.
    next_unbought = 0 if last_sold else int(unbought or 0) + (1 if price > 0 else 0)

    plan = tip_ladder.bundle_plan(price, tcfg["sizing"])
    if plan.total <= 0:
        return None
    seen = await seen_media(account_id, fan_id)
    media_ids, breakdown = await asyncio.to_thread(
        _compose_bundle_ids, client, plan, seen,
        list(tcfg.get("bundle_premium_folders") or []),
        list(tcfg.get("bundle_normal_folders") or []),
        list(tcfg.get("bundle_free_folders") or []), cost=cost)
    if not media_ids:
        # No tier folders configured (or all exhausted). Adaptive is the house
        # default now, and an account whose media lives in the per-rung folders
        # must not go silent — pull from the rung's own folder instead; only
        # the PHOTO SOURCE falls back, the price stays adaptive.
        r = rungs[new_idx] if isinstance(rungs[new_idx], dict) else {}
        folders, media_ids = await _pull_rung(client, tcfg=tcfg, rung=r, seen=seen,
                                              cost=cost)
        if not media_ids:
            return None
        return {"media_ids": media_ids, "price_cents": int(price),
                "is_free": price <= 0, "folders": folders, "rung": new_idx,
                "next_rung": new_idx, "convo": True, "softened": softened,
                "unbought": next_unbought}
    return {"media_ids": media_ids, "price_cents": int(price),
            "is_free": price <= 0, "folders": [], "rung": new_idx,
            "next_rung": new_idx, "convo": True, "softened": softened,
            "bundle": breakdown, "unbought": next_unbought}


