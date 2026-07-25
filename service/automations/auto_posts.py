"""service/automations/auto_posts.py — P1/P2 `auto_posts` (self-cleaning feed).

The "post then auto-delete" / "ready-made posts" idea from
library/AUTOMATIONS_SIMPLE.txt §"NEXT IDEAS" (P1 + P2, unified). Given a LIST of
ready posts, this:

  1. posts the FIRST item   → `of_client.create_post` (POST /api2/v2/posts),
  2. records a `posts` row   (status='posted', of_post_id),
  3. if the item has `hours_to_live`, enqueues a one-shot `unsend_messages` job
     at now+hours_to_live carrying a `{post_id}` target → the post auto-deletes
     (DELETE /posts/{id}); items WITHOUT hours_to_live are kept (P2 "keep"),
  4. if more items remain, re-enqueues ITSELF with the rest at now+delay_minutes
     so the list drips out one post at a time.

The whole queue rides in the job payload — each run does ONE thing and hands the
rest forward (read → do one → write), so there's no new table and no persistent
queue state to reconcile. It mirrors the proven Mass Online auto-unsend pattern
(server._auto_unsend → enqueue_job("unsend_messages", run_at=now+Nh)); here the
follow-up target is a feed post instead of a mass queue id.

Posting costs no LLM budget and isn't fan-scoped, so — like unsend_messages — it
takes neither `acquire_fan_lease` nor `account_spend_lock`. Creating a post IS
irreversible/outward-facing, so the first runs should use `dry_run` (plan only,
no OF call). Self-registers via `@register("auto_posts")` on import.

Payload shape::

    {
      "posts": [
        {"text": "...", "media_files": [123],
         "price": 0,               # DOLLARS — >0 makes a PAID/PPV post (OF
                                   #   requires media on a priced post); 0/absent
                                   #   = free. Passed to create_post as-is (OF's
                                   #   /posts `price` is dollars) and stored as
                                   #   price_cents via _to_cents (19.99 → 1999).
                                   #   DROPPED on a paid-subscription page — that
                                   #   page has no paid-post lane (account_page).
         "hours_to_live": 6,       # null/absent = keep forever (P2 "keep")
         "delay_minutes": 0},       # stagger THIS post after the previous one
        {"text": "...", "media_files": [456], "hours_to_live": 12}
      ],
      "resend_after_hours": 24,     # re-post the WHOLE list this long after it
      "resend_count": 2,            #   finishes, this many extra cycles (0/absent
                                    #   = post the list once). Baked into the
                                    #   automation, NOT the rule cadence.
      "dry_run": false              # plan only — no OF call, no enqueue
    }
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
from datetime import datetime, timedelta

import account_page  # free-vs-paid page: a paid page has no paid-post lane
import automation_executor as ax  # shared _make_client seam + enqueue_job
from automation_registry import register
from automations._pools import has_media_source, pick_media, pick_text
from db.engine import get_session
from db.models import Post

log = logging.getLogger("of-relay.automation.auto_posts")


def _int_list(raw: object) -> list[int]:
    """Coerce a payload media-id list to ints; drop anything non-numeric."""
    out: list[int] = []
    if isinstance(raw, list):
        for v in raw:
            try:
                out.append(int(v))
            except (TypeError, ValueError):
                continue
    return out


def _pos_float(raw: object) -> float | None:
    """A positive float (hours/minutes), else None — so 0/missing/garbage means
    'not set' rather than firing immediately or selecting nothing."""
    if isinstance(raw, (int, float)) and raw > 0:
        return float(raw)
    return None


def _to_cents(price: object) -> int:
    try:
        return int(round(float(price) * 100))
    except (TypeError, ValueError):
        return 0


@register("auto_posts")
async def run(account_id: str, payload: dict, *, run_id: int) -> dict:
    """Post the first item of the queue, schedule its auto-delete, and drip the
    rest. Returns a stats dict (lands in `automation_runs.stats_json`)."""
    payload = payload or {}
    posts = payload.get("posts") or []
    if not isinstance(posts, list) or not posts:
        return {"status": "skipped", "reason": "no_posts"}

    # ── dry_run: preview the WHOLE plan in one run, touch nothing ────────
    if payload.get("dry_run"):
        plan = [
            {
                "text": (pick_text(p))[:80] if isinstance(p, dict) else "",
                "text_variants": (len(p.get("texts")) if isinstance(p, dict) and isinstance(p.get("texts"), list) else 1),
                "price": (p.get("price") or 0) if isinstance(p, dict) else 0,
                "media_pool": len(_int_list(p.get("media_files"))) if isinstance(p, dict) else 0,
                "media_folder_id": (p.get("media_folder_id") if isinstance(p, dict) else None),
                "media_count": (p.get("media_count") or 1) if isinstance(p, dict) else 1,
                "preview_pool": len(_int_list(p.get("preview_media_files"))) if isinstance(p, dict) else 0,
                "preview_media_folder_id": (p.get("preview_media_folder_id") if isinstance(p, dict) else None),
                "preview_media_count": (p.get("preview_media_count") or 1) if isinstance(p, dict) else 1,
                "hours_to_live": _pos_float(p.get("hours_to_live")) if isinstance(p, dict) else None,
                "delay_minutes": (_pos_float(p.get("delay_minutes")) or 0) if isinstance(p, dict) else 0,
            }
            for p in posts
        ]
        # Surface the paid-page gate in the preview, so an operator planning a
        # priced drop sees it will be dropped BEFORE the real run silently does it.
        paid_page = await account_page.is_paid_page(account_id)
        for p in plan:
            p["skipped_reason"] = (account_page.PAID_PAGE_SKIP
                                   if paid_page and (p.get("price") or 0) > 0 else None)
        return {
            "dry_run": True, "posts": len(posts), "plan": plan, "posted": 0,
            "paid_page": paid_page,
            "resend_after_hours": _pos_float(payload.get("resend_after_hours")),
            "resend_count": int(payload.get("resend_count") or 0),
        }

    spec = posts[0] if isinstance(posts[0], dict) else {}
    rest = posts[1:]

    # ── Repost cycle (baked into auto_posts, NOT the rule cadence) ────────
    # `resend_count` extra full cycles, each `resend_after_hours` after the WHOLE
    # list finishes. The pristine list is captured once as `_original_posts` and
    # threaded through every drip so it survives the list draining down to empty;
    # the restart fires from the terminal run (rest empty) below. Mirrors the
    # mass_premade resend pattern. resend_count is decremented per CYCLE (at
    # restart), not per post.
    resend_after = _pos_float(payload.get("resend_after_hours"))
    resend_count = int(payload.get("resend_count") or 0)
    original_posts = payload.get("_original_posts")
    if original_posts is None and resend_count > 0:
        original_posts = posts  # the full list at the start of this cycle
    cycle = {
        "resend_after_hours": resend_after,
        "resend_count": resend_count,
        "_original_posts": original_posts,
    }

    # Randomiser: pick ONE text from the variant pool + sample media_count images
    # from the image pool (pre-picked ids and/or a vault folder). Resolved per
    # fire so each drip post varies. See automations/_pools.py.
    text = pick_text(spec)
    price = spec.get("price") or 0
    hours_to_live = _pos_float(spec.get("hours_to_live"))

    if not text and not has_media_source(spec):
        log.warning("auto_posts: empty post (no text, no media) — skipping item")
        # Still drain the rest so one bad item doesn't strand the queue.
        remaining = await _drip_rest(account_id, rest, None, cycle)
        return {"status": "skipped", "reason": "empty_post", "remaining": remaining}

    # ── A PAID post needs a FREE page ────────────────────────────────────
    # A subscription page has no paid-post lane on OF (see service/account_page.py),
    # so a priced item is dropped instead of posted — never re-priced to 0, which
    # would hand the locked set to every subscriber for free. The queue still
    # drains, so the free items in the same list keep going out.
    if price and price > 0 and await account_page.is_paid_page(account_id):
        log.info("auto_posts: account=%s is a paid-subscription page — dropping the "
                 "priced item (no paid-post lane on OF)", account_id)
        remaining = await _drip_rest(account_id, rest, None, cycle)
        return {"status": "skipped", "reason": account_page.PAID_PAGE_SKIP,
                "remaining": remaining}

    # Attribution: a post from the worker is the system Automation actor.
    employee_id: int | None = None
    try:
        from employees import get_automation_employee_id
        employee_id = await get_automation_employee_id()
    except Exception:
        log.debug("automation employee lookup failed; post attribution NULL", exc_info=True)

    # ── Fire the OF post (off-thread so the loop never blocks) ───────────
    client = await asyncio.to_thread(ax._make_client, account_id)
    # Sample the image(s) for this fire (folder pull, if any, hits OF → thread).
    media_files = await asyncio.to_thread(pick_media, spec, client)
    if not text and not media_files:
        # Pool resolved to nothing (e.g. an empty folder) — skip but keep draining.
        log.warning("auto_posts: media pool resolved empty + no text — skipping item")
        remaining = await _drip_rest(account_id, rest, employee_id, cycle)
        return {"status": "skipped", "reason": "empty_after_resolve", "remaining": remaining}

    # ── Free-preview pool (PAID posts only) ──────────────────────────────
    # A paid post can show a FREE teaser: `preview` ⊆ mediaFiles is unlocked
    # (of_client.create_post → OF `preview`). The preview pool is its own picker
    # (preview_media_files / _folder_id / _count); its picks are ADDED to
    # media_files so `previews` is always a valid subset. Free posts skip it.
    previews: list[int] = []
    if price and price > 0:
        preview_spec = {
            "media_files": spec.get("preview_media_files"),
            "media_folder_id": spec.get("preview_media_folder_id"),
            "media_count": spec.get("preview_media_count"),
        }
        if has_media_source(preview_spec):
            preview_files = await asyncio.to_thread(pick_media, preview_spec, client)
            for pid in preview_files:
                if pid not in media_files:
                    media_files.append(pid)
            previews = [p for p in preview_files if p in media_files]

    # Shuffle the attachment order (item 35): the pool is sampled but preview ids
    # are appended last, so without this OF's gallery would post free-then-paid in
    # a fixed order. The SAME shuffled list rides to create_post AND the stored
    # posts row, so OF's display and our record never diverge.
    if len(media_files) > 1:
        random.shuffle(media_files)

    # A PAID post must carry media — OF rejects a priced text-only post. Server
    # mirror of the PostComposer guard (block only when price>0 && no media).
    if price and price > 0 and not media_files:
        log.warning("auto_posts: paid post resolved to no media — skipping item")
        remaining = await _drip_rest(account_id, rest, employee_id, cycle)
        return {"status": "skipped", "reason": "paid_no_media", "remaining": remaining}

    # Proactively SPACE this post ≥ the gap after the account's previous OF write
    # (so drips never crowd OF's 10s window), with async retry as a backstop. The
    # spacing wait is asyncio.sleep — the loop/threads stay free meanwhile.
    result = await ax.of_write_paced(
        account_id,
        lambda: client.create_post(
            text=text, media_files=media_files, price=price, previews=previews,
        ),
    )
    of_post_id = result.get("id") if isinstance(result, dict) else None
    if of_post_id is None:
        log.warning("auto_posts: create_post returned no id (result=%r)", result)
        remaining = await _drip_rest(account_id, rest, employee_id, cycle)
        return {"status": "error", "reason": "no_post_id", "remaining": remaining}
    of_post_id = int(of_post_id)

    now = datetime.utcnow()

    # ── Record our posts row ─────────────────────────────────────────────
    async with get_session() as s:
        row = Post(
            account_id=str(account_id),
            of_post_id=of_post_id,
            status="posted",
            text=text,
            price_cents=_to_cents(price),
            media_ids=json.dumps(media_files),
            posted_at=now,
            created_by_employee_id=employee_id,
            raw_json=json.dumps(result, default=str)[:20000],
        )
        s.add(row)
        await s.flush()
        local_post_id = int(row.id)

    # ── Schedule the auto-delete (the cleanup leg) ───────────────────────
    delete_job_id: int | None = None
    delete_at_iso: str | None = None
    if hours_to_live:
        delete_at = now + timedelta(hours=hours_to_live)
        delete_job_id = await ax.enqueue_job(
            account_id, "unsend_messages",
            payload={"targets": [{"post_id": of_post_id, "local_post_id": local_post_id}]},
            run_at=delete_at,
            created_by_employee_id=employee_id,
        )
        delete_at_iso = delete_at.isoformat() + "Z"

    # ── Drip the rest of the queue ───────────────────────────────────────
    remaining = await _drip_rest(account_id, rest, employee_id, cycle)

    # ── Repost cycle: the WHOLE list just finished (no rest) → restart ────
    resend_job_id: int | None = None
    resend_at_iso: str | None = None
    if remaining == 0 and resend_count > 0 and resend_after and original_posts:
        resend_at = now + timedelta(hours=resend_after)
        resend_job_id = await ax.enqueue_job(
            account_id, "auto_posts",
            payload={
                "posts": original_posts,
                "resend_after_hours": resend_after,
                "resend_count": resend_count - 1,
                "_original_posts": original_posts,
            },
            run_at=resend_at,
            created_by_employee_id=employee_id,
        )
        resend_at_iso = resend_at.isoformat() + "Z"

    return {
        "status": "ok",
        "posted": 1,
        "of_post_id": of_post_id,
        "local_post_id": local_post_id,
        "delete_job_id": delete_job_id,
        "delete_at": delete_at_iso,
        "remaining": remaining,
        "resend_job_id": resend_job_id,
        "resend_at": resend_at_iso,
        "resends_left": max(resend_count - 1, 0) if resend_count > 0 else 0,
    }


async def _drip_rest(
    account_id: str, rest: list, employee_id: int | None, cycle: dict | None = None
) -> int:
    """Re-enqueue `auto_posts` with the remaining queue at now+delay_minutes (the
    next item's own stagger). Returns the count handed forward (0 = queue done).
    The repost-cycle fields (if active) ride along so the pristine `_original_posts`
    survives the list draining; the restart itself fires from run() at the terminal
    (rest-empty) step."""
    if not rest:
        return 0
    head = rest[0] if isinstance(rest[0], dict) else {}
    delay_min = _pos_float(head.get("delay_minutes")) or 0
    run_at = datetime.utcnow() + timedelta(minutes=delay_min)
    drip_payload: dict = {"posts": rest}
    if cycle and cycle.get("resend_count", 0) > 0 and cycle.get("resend_after_hours"):
        drip_payload["resend_after_hours"] = cycle["resend_after_hours"]
        drip_payload["resend_count"] = cycle["resend_count"]
        drip_payload["_original_posts"] = cycle["_original_posts"]
    await ax.enqueue_job(
        account_id, "auto_posts",
        payload=drip_payload,
        run_at=run_at,
        created_by_employee_id=employee_id,
    )
    return len(rest)
