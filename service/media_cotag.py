"""service/media_cotag.py — @-tag the released co-performer on every send whose
media is not PROVABLY solo.

OF's release-form rule: content that shows anyone besides the account owner has
to name that person. An untagged two-person send is the kind of thing that ends
an account, and no sender in this repo was passing `userTags` — the field was
wired for the operator's manual picker only. So the decision is made HERE, in
one place, and enforced inside `of_client`'s five send bodies rather than at the
~20 call sites that attach media (a call site added next month would forget).

Three things this module is deliberately paranoid about:

  1. **Unknown media is treated as multi-person.** `vault_scripts.is_solo`
     already errs this way — a row nobody described answers "who is in this?"
     with silence, and silence is not "just her". 17% of the prod vault (1,422
     of 8,556 items on 2026-08-12) carries no describe fields at all, so this is
     not a rounding error; it is the point. Over-tagging costs a mention,
     under-tagging costs the account.

  2. **A tag we cannot resolve is never invented.** OF 400s the whole send on a
     `userTags` id that is not in the caller's tagged-friend list, so guessing
     an id would not mis-tag — it would take every media send on that account
     offline. When the handle does not resolve for an account we log at ERROR
     and send untagged, because a delivered-but-untagged message is recoverable
     and a dead sender is not.

  3. **The tag can never cost a send.** `of_client` retries once without the
     auto-added tag if OF rejects the body (see `_post_dropping_auto_cotag`).

Config (env, read live so a restart is not needed to change the handle):
  OF_COTAG_ENABLED   "0" kills the whole feature. Default on.
  OF_COTAG_USERNAME  handle to tag. Default `jakabasej`.
  OF_COTAG_USER_ID   numeric OF user id, skips the per-account lookup entirely.
                     Set this only if the SAME id is taggable from every
                     account — it is not validated against the friend list.

Per-account overrides (the Brain panel → account_ai_config, read live through
the same sync engine, 60s cache):
  cotag_tag_videos   NULL ≡ OFF (most creators are solo — their videos really
                     are just them). Ticked ON, any send attaching a vault
                     VIDEO tags even when the describe verdict says solo:
                     video describes are cut from stills and miss the POV
                     partner — Lucas1/Lucas2 clips shipped untagged exactly
                     this way, so the collab accounts turn this on.
  cotag_username     handle to tag on THIS account, no '@'. NULL → the env →
                     the built-in default.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Iterable

log = logging.getLogger("of-relay.media_cotag")

DEFAULT_HANDLE = "jakabasej"


def fold_handle(raw: Any) -> str:
    """A username the way OF stores it: trimmed, no `@`, case-folded.

    The ONE definition — the env reader, the Brain-column reader and the config
    API's save path all fold through here, so "what counts as the same handle"
    cannot drift between the writer and the readers."""
    return str(raw or "").strip().lstrip("@").lower()

# How long a per-item solo verdict is trusted. Describe data changes only when
# a re-describe sweep runs, so this is about bounding staleness, not freshness.
_VERDICT_TTL_S = 600.0
# A resolved id is stable; a MISS is retried sooner because it is usually
# "@handle has not accepted the tag request on this account yet", which an
# operator fixes mid-day.
_ID_HIT_TTL_S = 3600.0
_ID_MISS_TTL_S = 300.0
# Bound the verdict cache so a long-lived relay can't grow it without limit.
_VERDICT_CACHE_MAX = 20_000
# The Brain overrides move when an operator hits Save; the save path also drops
# the caches, so this TTL only covers a second relay worker / a raw DB edit.
_CFG_TTL_S = 60.0

# (account_id, media_id) -> (stamped_at, (multi_person, is_video)).
# The two flags stay separate so flipping the Brain's video rule takes effect on
# the next config read, not after every item verdict expires.
_verdicts: dict[tuple[str, int], tuple[float, tuple[bool, bool]]] = {}
# account_id -> (stamped_at, user_id_or_None, handle_it_resolves).  The handle
# rides along so a Brain username change invalidates the cached id instead of
# serving the OLD person for the rest of the TTL.
_ids: dict[str, tuple[float, int | None, str]] = {}
# account_id -> (stamped_at, (always_tag_videos, username_override_or_None))
_acct_cfg: dict[str, tuple[float, tuple[bool, str | None]]] = {}

_engine = None


# ── Config ──────────────────────────────────────────────────────────

def enabled() -> bool:
    return (os.environ.get("OF_COTAG_ENABLED") or "1").strip().lower() not in (
        "0", "false", "no", "off", "")


def _account_cfg(account_id: str) -> tuple[bool, str | None]:
    """This account's Brain overrides: (always tag videos, username or None).

    No row / NULL columns / a lookup that fails → (False, None): the video rule
    is an OPT-IN for collab accounts, so every failure path lands on the
    shipped default — most creators are solo and their videos really are just
    them. (The describe-verdict spine keeps its own "unknown ⇒ tag" doctrine;
    this knob only adds tags on top of it.)
    """
    now = time.monotonic()
    cached = _acct_cfg.get(account_id)
    if cached and now - cached[0] < _CFG_TTL_S:
        return cached[1]
    always, username = False, None
    try:
        from sqlalchemy import text

        with _sync_engine().connect() as conn:
            row = conn.execute(text(
                "SELECT cotag_tag_videos, cotag_username FROM account_ai_config "
                "WHERE account_id = :a"), {"a": account_id}).first()
        if row is not None:
            always = bool(row[0])
            username = fold_handle(row[1]) or None
    except Exception:
        log.exception(
            "cotag: brain config lookup failed (account=%s) — using the "
            "defaults (video rule off, global handle)", account_id)
    _acct_cfg[account_id] = (now, (always, username))
    return always, username


def handle(account_id: Any = None) -> str:
    """The username to tag, normalised the way OF stores it (no `@`, folded).

    Per-account Brain override first, then the env, then the built-in default.
    A blanked env still means "nobody to tag" when no override is set. Takes
    the account id in whatever shape the caller holds (str, int, None, "") —
    falsy means "no account context, global answer"."""
    if account_id:
        _always, override = _account_cfg(str(account_id))
        if override:
            return override
    raw = os.environ.get("OF_COTAG_USERNAME")
    return fold_handle(raw if raw is not None else DEFAULT_HANDLE)


def _pinned_id() -> int | None:
    raw = (os.environ.get("OF_COTAG_USER_ID") or "").strip()
    try:
        return int(raw) if raw else None
    except ValueError:
        log.error("OF_COTAG_USER_ID=%r is not a number — ignoring it", raw)
        return None


# ── Is anyone else in this? ─────────────────────────────────────────

def _numeric_ids(media_files: Any) -> list[int] | None:
    """The vault media ids in a `mediaFiles` argument.

    None means "this send carries media we cannot look up" — a fresh-upload
    claim dict (stories) or anything else non-numeric. Callers read that as
    multi-person, since an item with no vault row has no describe data either.
    """
    if not media_files:
        return []
    out: list[int] = []
    for m in media_files:
        try:
            out.append(int(m))
        except (TypeError, ValueError):
            return None
    return out


def _multi_person(ai_fields_json: str | None) -> bool:
    """Can we PROVE only the account owner is in this item? If not → True.

    Delegates to `vault_scripts.is_solo` rather than re-reading the describe
    fields here: that function is the repo's one answer to "who is in this",
    it already folds partner ACTS (a blowjob is two people even when the model
    returns `people_count: 1`), and a second copy of the rule would drift.
    """
    import vault_scripts

    try:
        fields = json.loads(ai_fields_json) if ai_fields_json else {}
    except (TypeError, ValueError):
        fields = {}
    if not isinstance(fields, dict):
        fields = {}
    return not vault_scripts.is_solo(fields)


def _sync_engine():
    """A read-only sync engine on the app's own DB.

    The send path runs inside `asyncio.to_thread`, so the async session is not
    reachable from here. NullPool because a connection per lookup (rare — the
    verdict cache absorbs the repeats) is cheaper than holding file descriptors
    open across a relay that has hit EMFILE before, and `query_only` because
    nothing in this module has any business writing to the vault.
    """
    global _engine
    if _engine is None:
        from sqlalchemy import create_engine, event
        from sqlalchemy.pool import NullPool

        from db.engine import DATABASE_URL

        url = DATABASE_URL.replace("+aiosqlite", "").replace("+asyncpg", "")
        is_sqlite = url.startswith("sqlite")
        eng = create_engine(
            url,
            poolclass=NullPool,
            connect_args={"check_same_thread": False} if is_sqlite else {},
        )
        if is_sqlite:
            @event.listens_for(eng, "connect")
            def _read_only(dbapi_conn, _record):   # noqa: ANN001
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA query_only=1")
                cur.execute("PRAGMA busy_timeout=5000")
                cur.close()
        _engine = eng
    return _engine


def _describe_rows(account_id: str,
                   media_ids: Iterable[int]) -> dict[int, tuple[str | None, str | None]]:
    """media_id -> (ai_fields_json, kind) for the vault rows we hold."""
    from sqlalchemy import bindparam, text

    stmt = text(
        "SELECT media_id, ai_fields_json, kind FROM vault_items "
        "WHERE account_id = :account_id AND media_id IN :ids"
    ).bindparams(bindparam("ids", expanding=True))
    with _sync_engine().connect() as conn:
        rows = conn.execute(stmt, {"account_id": str(account_id),
                                   "ids": list(media_ids)})
        return {int(r[0]): (r[1], r[2]) for r in rows}


def _remember(key: tuple[str, int], flags: tuple[bool, bool]) -> None:
    if len(_verdicts) >= _VERDICT_CACHE_MAX:
        _verdicts.clear()
    _verdicts[key] = (time.monotonic(), flags)


def needs_cotag(account_id: str, media_files: Any) -> bool:
    """Does this attachment set need the co-performer tag?

    True when ANY attached item is not provably solo — one two-person clip in a
    five-item bundle still shows a second person — or, on an account whose
    Brain opted IN to the video rule, when any item is a VIDEO at all: video
    describes are cut from stills and miss the POV partner, which is how
    solo-described Lucas clips went out untagged.
    """
    ids = _numeric_ids(media_files)
    if ids is None:
        return True
    if not ids:
        return False
    always_videos, _ = _account_cfg(str(account_id))

    def _needs(flags: tuple[bool, bool]) -> bool:
        multi, video = flags
        return multi or (always_videos and video)

    now = time.monotonic()
    stale: list[int] = []
    for mid in ids:
        cached = _verdicts.get((str(account_id), mid))
        if cached and now - cached[0] < _VERDICT_TTL_S:
            if _needs(cached[1]):
                return True
        else:
            stale.append(mid)
    if not stale:
        return False

    try:
        rows = _describe_rows(account_id, stale)
    except Exception:
        log.exception(
            "cotag: vault lookup failed (account=%s media=%s) — tagging, because "
            "a lookup we could not run is not evidence that she is alone",
            account_id, stale,
        )
        return True

    verdict = False
    for mid in stale:
        # `.get` missing → None → no describe data → multi-person. An item we
        # have never collected is exactly the item nobody has ever looked at.
        row = rows.get(mid)
        flags = (_multi_person(row[0] if row else None),
                 row is not None and str(row[1] or "").strip().lower() == "video")
        _remember((str(account_id), mid), flags)
        verdict = verdict or _needs(flags)
    return verdict


# ── Who to tag ──────────────────────────────────────────────────────

def lookup_tag_id(client, want: str | None = None) -> int | None:
    """Find `want` in this account's tagged-friend list. One live OF call.

    OF rejects `userTags` ids that are not on that list, so the list IS the
    contract — resolving through it means a tag we emit is a tag OF accepts.

    Public because "can this account tag @handle?" is a question worth asking
    from outside a send (`_probe_cotag_taggable.py` is the ops answer to it),
    and a second implementation of this match would be free to pass while the
    real resolver fails. Unlike `tag_user_id` it neither caches nor honours the
    `OF_COTAG_USER_ID` pin: a probe wants the truth from OF, not our shortcut.
    """
    if want is None:
        want = handle(getattr(client, "account_id", None))
    resp = client.tagged_friend_users(search=want, limit=20)
    items = (resp or {}).get("items") or []
    for item in items:
        user = item.get("user") or {}
        for candidate in (user.get("username"), item.get("name"), user.get("name")):
            if str(candidate or "").strip().lstrip("@").lower() == want:
                # The top-level `id` is what the send body's `userTags` takes —
                # verified 2026-05-23 against the live chat composer's capture.
                raw = item.get("id") if item.get("id") is not None else user.get("id")
                try:
                    return int(raw)
                except (TypeError, ValueError):
                    return None
    return None


def tag_user_id(client) -> int | None:
    """The numeric OF id to put in `userTags` for this account, or None."""
    pinned = _pinned_id()
    if pinned is not None:
        return pinned

    account_id = str(getattr(client, "account_id", "") or "")
    want = handle(account_id)
    if not want:
        return None

    now = time.monotonic()
    cached = _ids.get(account_id)
    # A cached id is only good for the handle it resolved — a Brain username
    # change must re-resolve, not keep tagging the OLD person for an hour.
    if cached and cached[2] == want:
        ttl = _ID_HIT_TTL_S if cached[1] is not None else _ID_MISS_TTL_S
        if now - cached[0] < ttl:
            return cached[1]

    try:
        found = lookup_tag_id(client, want)
    except Exception:
        log.exception("cotag: tagged-friend lookup failed on account=%s", account_id)
        # Not cached: a network blip should not silence the tag for an hour.
        return None

    _ids[account_id] = (now, found, want)
    if found is None:
        log.error(
            "cotag: @%s is NOT in account %s's tagged-friend list — OF would 400 "
            "the send on an id it does not know, so multi-person media from this "
            "account goes out UNTAGGED until the tag request is accepted",
            want, account_id,
        )
    else:
        log.info("cotag: @%s resolves to %s on account %s", want, found, account_id)
    return found


def cotags_for(client, media_files: Any) -> list[int]:
    """The auto-added `userTags` ids for a send. Empty list = nothing to add."""
    if not enabled() or not media_files:
        return []
    account_id = str(getattr(client, "account_id", "") or "")
    if not account_id:
        return []
    if not needs_cotag(account_id, media_files):
        return []
    uid = tag_user_id(client)
    return [uid] if uid is not None else []


def story_mention(account_id: str, media_ids: Any) -> str | None:
    """`"@handle"` to overlay on a story, or None.

    Stories are the one send that cannot carry `userTags`: OF re-runs the full
    fresh-upload pipeline even for a vault pick, so the wire body holds a
    convert claim and never a media id (see of_client.create_story). The @mention
    OVERLAY is what OF's own story editor emits to tag someone, so that is what
    we use — and because it is text rather than an id, it needs no tagged-friend
    lookup and cannot 400 the post.

    Takes the SOURCE vault ids, which the caller still knows even though the
    body won't carry them.

    TOTAL — it does not raise. The fail-safe ("when in doubt, tag") is this
    module's to own; a caller that wrapped this in its own try/except would be
    a second place deciding the same policy, free to drift from this one.
    """
    try:
        if not enabled():
            return None
        if not str(account_id or "") or not needs_cotag(account_id, media_ids):
            return None
    except Exception:
        log.exception("cotag: story check failed — mentioning, because a check "
                      "we could not run is not evidence that she is alone")
    # A blanked OF_COTAG_USERNAME means "nobody to tag" on BOTH paths — the
    # userTags side already resolves it to no id, and falling back to the
    # built-in default here would resurrect a handle the operator cleared.
    want = handle(account_id)
    return f"@{want}" if want else None


def forget_tag_id(account_id: str) -> None:
    """Drop the cached id for one account, so the next send re-resolves it.

    Called when OF REJECTS a body we tagged. Without this the resolver would
    keep handing out the id OF just refused for the rest of its hour-long TTL,
    and every media send in that window would pay POST → 400 → POST. Re-asking
    costs one lookup and usually returns None (the tag request was revoked),
    after which sends go out untagged and loud instead of doubled and silent.
    """
    _ids.pop(str(account_id or ""), None)


def forget_account_cfg(account_id: str) -> None:
    """Drop ONE account's cached Brain overrides and resolved id — the Brain
    save path calls this so a new username/video rule applies on the very next
    send instead of after the TTLs drain.

    Deliberately narrow: a brain save must not cost every OTHER account a fresh
    tagged-friend lookup, and the per-item verdict flags don't depend on config
    at all (that separation is why flipping the video rule is just a config
    re-read). The id entry is dropped too — not because the resolver needs it
    (a changed handle already re-resolves via the `want` check), but as the
    operator's "I just accepted the tag request, re-ask now" lever."""
    _acct_cfg.pop(str(account_id or ""), None)
    _ids.pop(str(account_id or ""), None)


def reset_caches() -> None:
    """Drop every cached verdict, id and Brain override. For tests and for an
    operator who has just accepted a tag request and does not want to wait out
    the TTL."""
    _verdicts.clear()
    _ids.clear()
    _acct_cfg.clear()
