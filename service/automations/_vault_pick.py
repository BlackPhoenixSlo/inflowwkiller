"""service/automations/_vault_pick.py — pick unseen vault media for a fan.

THE folder→media read every folder-configured lane shares. Give it folder NAMES
(what every config stores) and a fan's already-seen set, get back media ids that
are fresh for him, budgeted by the lane's slot-cost policy.

It is not a teaser concept. It lived inside `teaser_select` because the teaser
ladder was the first caller, and three modules outside that feature ended up
reaching through the teaser module's private names to get at it — `tip_reward`
for the reward bundle, `make_right` for the apology gift, `welcome_chatter_for_info`
for the gather-close pool. Its own leaf, with a PUBLIC surface, so a caller that
has nothing to do with teasers no longer imports one.

Public names are the ones other modules import: `folder_list`, `resolve_folders`,
`folder_media_pool`, `gather_unseen`, `pull_stages`. The leading underscore
belongs to the MODULE (the house mark for a leaf, as in `_clock` / `_slot_cost`),
so `_folder_media_items` — the (id, type, duration) read nothing outside calls —
carries one of its own.

The fan's SENT-media set is deliberately NOT here: that is a fan↔media read and
lives with its siblings in `ownership.seen_media`.

Same logger name as tip_reward — this module was carved out of it by pure code
motion and every emitted log record must stay byte-identical, including the
"tip_reward ..." prefixes in the messages below.
"""
from __future__ import annotations

import logging

from automations._slot_cost import PER_ITEM, SlotCost

log = logging.getLogger("of-relay.automation.tip_reward")


# How many items to pull per folder when scanning for unseen ones — generous so a
# fan deep into a folder still finds fresh media.
_VAULT_SCAN_LIMIT = 100

# Vault item types we reward with. Photos, videos (incl. DRM-only — still sendable
# as a vault attachment) and gifs; audio is excluded (not a "reward image/clip").
_REWARD_MEDIA_TYPES = ("photo", "video", "gif")


def _folder_media_items(client, list_id: int) -> list[tuple[int, str, int]]:
    """Ordered `(media_id, type, duration_seconds)` in one vault folder
    (recent-first), PHOTOS AND VIDEOS (plus gifs). `type="all"` so a reward can
    hand out a clip as readily as a photo; audio is filtered out. DRM-only videos
    are kept — they can't be previewed but ARE sendable as a vault attachment.
    Type and duration ride along because the OF payload already carries them
    (zero extra calls): the images-only filter and the video slot-coster both
    read them downstream. Best-effort (empty on error)."""
    try:
        media = client.vault_media(list_id=int(list_id), type="all",
                                   limit=_VAULT_SCAN_LIMIT)
    except Exception:
        log.debug("tip_reward vault_media failed folder=%s", list_id, exc_info=True)
        return []
    items = media.get("list") if isinstance(media, dict) else media
    out: list[tuple[int, str, int]] = []
    for it in (items or []):
        if not isinstance(it, dict) or it.get("id") is None:
            continue
        # Tolerate a missing/blank type (older payloads / test fakes) — only an
        # explicit non-reward type (e.g. audio) is skipped.
        mtype = str(it.get("type") or "").strip().lower()
        if mtype and mtype not in _REWARD_MEDIA_TYPES:
            continue
        dur = it.get("duration")
        try:
            out.append((int(it["id"]), mtype,
                        int(dur) if isinstance(dur, (int, float)) and dur else 0))
        except (TypeError, ValueError):
            continue
    return out


def folder_list(v, *, max_len: int | None = None) -> list[str]:
    """PUBLIC seam: THE config→folder-names coercion, for every slot that holds
    folders — on BOTH sides of the wire. `tip_reward_config_api` validates saves
    through it too, so "what a folder slot means" has one definition and a save can
    never store a shape the read rejects.

    A slot may be stored as a list (the shape the tab writes now) or as the single
    string it was before — `""` → `[]`, `"tease10"` → `["tease10"]`. Reading through
    here is why multi-folder needed NO migration: an account whose rungs still hold
    a bare string keeps working, and the first save from the tab widens it.

    Names are stripped, blanks dropped, and duplicates removed case-insensitively
    (order preserved) — a folder listed twice must not make the scan read it twice,
    and `resolve_folders` is first-wins on name collisions anyway.

    `max_len` (the write side's per-name cap) truncates BEFORE the dedupe, which is
    why it belongs here and not in the caller: cut afterwards, two names that differ
    only past the cap would land as one name twice.

    🚨 NEVER `str()` a stored list to get here. The repr `"['a','b']"` matches no
    folder in the vault, which reads downstream as "no media" — a silently DEAD
    lane rather than an error."""
    raw = [v] if isinstance(v, str) else list(v or [])
    out: list[str] = []
    seen: set[str] = set()
    for nm in raw:
        nm = str(nm or "").strip()
        if max_len:
            nm = nm[:max_len]
        key = nm.lower()
        if nm and key not in seen:
            seen.add(key)
            out.append(nm)
    return out


def resolve_folders(client) -> dict[str, int]:
    """The account's WHOLE folder map: name (lowercased) → vault list id.

    It reads every folder and filters nothing, so one call serves any number of
    folder names — hoist it and pass the result down rather than calling it per
    name. It used to take a `folder_names` list it never looked at, which read
    like a scoped lookup and invited exactly the per-name calling it cannot
    benefit from. Best-effort: {} on error, which every caller reads as
    "folder not found"."""
    try:
        lists = client.vault_lists(view="main", limit=100)
    except Exception:
        log.debug("tip_reward vault_lists failed", exc_info=True)
        return {}
    folders = lists.get("list") if isinstance(lists, dict) else lists
    by_name: dict[str, int] = {}
    for f in (folders or []):
        if isinstance(f, dict) and f.get("id") is not None:
            by_name.setdefault(str(f.get("name", "")).strip().lower(), int(f["id"]))
    return by_name


def folder_media_pool(client, folder_name: str) -> list[int] | None:
    """PUBLIC seam: one vault folder's media ids, by NAME. None = no such folder.

    The name → list-id → media-ids read every folder-configured lane shares —
    the tip tiers and hot teaser here, welcome_chatter_for_info's gather-close from outside.
    Folder NAMES are what the configs store (the UI's VaultFolderPicker shape),
    so this is the one place the name resolves. Sync client calls — run it via
    `asyncio.to_thread` off the event loop."""
    list_id = resolve_folders(client).get(str(folder_name).strip().lower())
    if list_id is None:
        return None
    return [mid for mid, _t, _d in _folder_media_items(client, list_id)]


def gather_unseen(client, folders: list[str], by_name: dict[str, int],
                   seen: set[int], count: int, *,
                   cost: SlotCost = PER_ITEM) -> list[int]:
    """Up to `count` SLOTS of unseen media ids, scanning the tier's folders in
    order and de-duplicating across them (an id can live in two folders).

    `cost` is the lane's billing policy (see `SlotCost`): it decides both what an
    item is worth and whether it may ride at all. Under the default every item
    costs one slot, so `count` is the plain item count it always was. Under the
    rate-card cost `count` is a SLOT budget: an item that would overflow it is
    SKIPPED and the scan continues, because a cheaper item further down may still
    fit — the budget is a ceiling, never trimmed after the fact."""
    picked: list[int] = []
    taken: set[int] = set()
    total = 0
    for nm in folders:
        list_id = by_name.get(str(nm).strip().lower())
        if list_id is None:
            log.info("tip_reward folder not found name=%r", nm)
            continue
        for mid, mtype, duration in _folder_media_items(client, list_id):
            if mid in seen or mid in taken:
                continue
            c = cost(mid, mtype, duration)
            if c is None or total + c > count:
                continue
            picked.append(mid)
            taken.add(mid)
            total += c
            if total >= count:
                return picked
    return picked


def pull_stages(client, stages: list[tuple[list[str], int]],
                 seen: set[int], *,
                 repeat_ok: frozenset[int] | set[int] = frozenset(),
                 never_repeat: frozenset[int] | set[int] = frozenset(),
                 cost: SlotCost = PER_ITEM,
                 ) -> tuple[list[list[int]], list[int]]:
    """THE one folder-bundle composer: ordered stage pulls with cross-stage dedup
    + shortfall backfill. Each (folder_names, count) stage pulls up to `count`
    unseen items from its folders; a short stage does not shrink the bundle —
    the total shortfall is backfilled from ALL stages' folders at the end.

    `cost` rides through to `gather_unseen`. Under the rate-card cost every count
    here is a SLOT budget, and the shortfall is measured in slots too
    (`cost.slots_of`) — an item-counted shortfall would over-fill: a stage that
    spent its 5 slots on a 3-slot clip plus 2 photos is FULL, not 2 short.

    `repeat_ok` names stage INDICES whose folders hold tease/filler content
    (operator ruling 07-23: filler may repeat, and a filler shortfall must
    never shrink what tease-share we promised). When the unseen backfill still
    leaves the bundle short, those stages' folders are re-pulled ignoring the
    fan's send history — capped at the repeat_ok stages' OWN share (a payoff
    shortfall keeps shrinking the bundle: the price is for the payoff, and a
    priced send of 100% repeated tease is not a send), and deduped against
    THIS bundle plus `never_repeat` (ids the CALLER will append itself, e.g.
    tip_reward's context-matched photos) so a repeat never duplicates within
    one send. Payoff stages are never repeated from.

    Returns (per_stage_ids, backfill_ids). Both the teaser composer and the tip
    reward's tease/normal split ride this. Sync (OF reads) — call via to_thread."""
    stages = [([f for f in (fs or []) if str(f).strip()], int(n))
              for fs, n in stages]
    # ONE folder read for the whole bundle. `resolve_folders` returns the entire
    # name→id map whatever it is asked for, so the per-stage, backfill and repeat
    # calls this replaced were the SAME OF round trip fetched up to five times for
    # one send. Skipped entirely when no stage has work, so an unconfigured rung
    # (empty folder list) still costs zero calls.
    by_name = resolve_folders(client) if any(
        n > 0 and folders for folders, n in stages) else {}
    taken = set(seen)
    bundle: set[int] = set()
    picked: list[list[int]] = []
    for folders, n in stages:
        ids: list[int] = []
        if n > 0 and folders:
            ids = gather_unseen(client, folders, by_name, taken, n, cost=cost)
            taken.update(ids)
            bundle.update(ids)
        picked.append(ids)
    want = sum(max(0, n) for _, n in stages)
    short = want - sum(cost.slots_of(p) for p in picked)
    extras: list[int] = []
    if short > 0:
        all_folders = [f for folders, _ in stages for f in folders]
        if all_folders:
            extras = gather_unseen(client, all_folders, by_name, taken, short,
                                    cost=cost)
            bundle.update(extras)
    short -= cost.slots_of(extras)
    if short > 0 and repeat_ok:
        # Repeats may only restore the repeat_ok stages' OWN share — a payoff
        # shortfall keeps shrinking the bundle rather than becoming filler.
        repeat_want = sum(max(0, n) for i, (_, n) in enumerate(stages)
                          if i in repeat_ok)
        repeat_have = sum(cost.slots_of(picked[i]) for i in repeat_ok
                          if i < len(picked))
        short = min(short, max(0, repeat_want - repeat_have))
        r_folders = [f for i, (folders, _) in enumerate(stages)
                     if i in repeat_ok for f in folders]
        if short > 0 and r_folders:
            extras = extras + gather_unseen(
                client, r_folders, by_name,
                set(bundle) | set(never_repeat), short, cost=cost)
    return picked, extras
