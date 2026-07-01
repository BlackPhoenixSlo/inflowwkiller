"""service/automations/auto_stories.py — scheduled "stories from a vault folder".

Drives the Settings → Auto stories tab. On each trigger it:

  1. builds a photo POOL — explicit `media_files` ids ∪ a vault folder
     (`media_folder_id`, or the legacy `folder_id`) — via `_pools.pick_media`,
  2. samples `media_count` (default = `per_run`) random photos and resolves
     each id → its CDN url (stories need a url, not a bare vault id),
  3. posts each as a story  → of_client.post_story_from_url (the captured
     vault/CDN → convert → POST /stories flow),
  4. if `hours_to_live` is set, enqueues a one-shot `unsend_messages` job at
     now+hours carrying a `{story_id}` target → the story auto-deletes
     (DELETE /stories/{id}); 0/absent = keep until it expires on its own.

The cadence ("cron time") and the run-limit ("how many times to run") live on
the automation_rule's `trigger_json` and are enforced by the executor's
`_materialize_due_rules` — this handler just does ONE trigger's worth of work,
mirroring auto_posts (read → do → schedule cleanup), so there is no new table.

Posting a story is irreversible/outward-facing, so it takes neither
`acquire_fan_lease` nor `account_spend_lock` (no fan, no spend), exactly like
auto_posts / unsend_messages. Self-registers via `@register("auto_stories")`.

Payload (steps_json) shape::

    {
      "media_folder_id": 22454297, # vault folder pool (alias: legacy `folder_id`)
      "media_files": [11, 12],     # explicit vault-id pool — joins the folder pool
      "media_count": 1,            # photos per trigger (alias/fallback: `per_run`)
      "per_run": 1,                # legacy count knob (used when media_count unset)
      "hours_to_live": 6,          # auto-delete after N hours (null/0 = keep)
      "watermark_text": null,      # optional watermark stamped on re-upload
      "dry_run": false             # resolve picks, post nothing
    }

At least one of `media_folder_id`/`folder_id`/`media_files` must be present.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

import automation_executor as ax  # shared _make_client seam + enqueue_job
from automation_registry import register
from automations._pools import has_media_source, pick_media

log = logging.getLogger("of-relay.automation.auto_stories")


def _pos_float(raw: object) -> float | None:
    """A positive float, else None — 0/missing/garbage means 'not set'."""
    if isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw > 0:
        return float(raw)
    return None


def _pos_int(raw: object, default: int) -> int:
    if isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw >= 1:
        return int(raw)
    return default


def _photo_url(client, media_id: int) -> str | None:
    """Resolve one vault photo id → its full-res, non-DRM CDN url (or None).

    /stories can't take a bare vault id (create_story rejects it), so each pool
    pick is resolved to a url here via the by-id vault read; post_story_from_url
    then downloads → re-uploads it. DRM / urlless items resolve to None and are
    skipped by the caller."""
    try:
        it = client.vault_media_by_id(int(media_id))
    except Exception:
        log.warning("auto_stories: vault_media_by_id failed id=%r", media_id, exc_info=True)
        return None
    full = ((it.get("files") or {}).get("full") or {}) if isinstance(it, dict) else {}
    url = full.get("url")
    if url and not full.get("drm"):
        return url
    return None


@register("auto_stories")
async def run(account_id: str, payload: dict, *, run_id: int) -> dict:
    """Post `per_run` random stories from a media POOL, scheduling each delete.

    The pool mirrors auto_posts: explicit `media_files` ids ∪ a vault folder,
    with `media_count` (falls back to `per_run`) sampled per fire. The legacy
    single-folder shape (`folder_id`) still works — it maps onto the pool's
    `media_folder_id`."""
    payload = payload or {}

    # Folder: prefer the pool name, fall back to the legacy `folder_id`.
    folder_id = payload.get("media_folder_id")
    if folder_id is None:
        folder_id = payload.get("folder_id")
    try:
        folder_id = int(folder_id) if folder_id is not None else None
    except (TypeError, ValueError):
        folder_id = None

    per_run = _pos_int(payload.get("per_run"), 1)
    count = _pos_int(payload.get("media_count"), 0) or per_run
    hours_to_live = _pos_float(payload.get("hours_to_live"))
    watermark_text = payload.get("watermark_text") or None

    pool_spec = {
        "media_files": payload.get("media_files"),
        "media_folder_id": folder_id,
        "media_count": count,
    }
    if not has_media_source(pool_spec):
        return {"status": "skipped", "reason": "no_media_source"}

    client = await asyncio.to_thread(ax._make_client, account_id)

    # Sample the pool → ids (shared _pools helper: dedups + non-DRM folder filter
    # + clamps to pool size), then resolve each id → a CDN url stories can use.
    picked_ids = await asyncio.to_thread(pick_media, pool_spec, client)
    resolved: list[tuple[int, str]] = []
    for mid in picked_ids:
        url = await asyncio.to_thread(_photo_url, client, mid)
        if url:
            resolved.append((mid, url))
    if not resolved:
        return {"status": "skipped", "reason": "no_usable_photos",
                "folder_id": folder_id}

    # ── dry_run: report the picks, touch nothing ────────────────────────
    if payload.get("dry_run"):
        return {
            "dry_run": True, "folder_id": folder_id,
            "would_post": [mid for mid, _ in resolved],
            "posted": 0,
        }

    # Attribution: a story from the worker is the system Automation actor.
    employee_id = None
    try:
        from employees import get_automation_employee_id
        employee_id = await get_automation_employee_id()
    except Exception:
        log.debug("automation employee lookup failed; attribution NULL", exc_info=True)

    posted: list[dict] = []
    failed = 0
    now = datetime.utcnow()
    for mid, url in resolved:
        try:
            story = await asyncio.to_thread(
                lambda u=url: client.post_story_from_url(u, watermark_text=watermark_text)
            )
        except Exception as e:  # noqa: BLE001 — one bad pick shouldn't strand the rest
            failed += 1
            log.warning("auto_stories: post failed vault=%s: %s", mid, e)
            continue
        story_id = story.get("id") if isinstance(story, dict) else None
        entry = {"vault_id": mid, "story_id": story_id}

        # Schedule the auto-delete (the cleanup leg).
        if story_id and hours_to_live:
            delete_at = now + timedelta(hours=hours_to_live)
            entry["delete_job_id"] = await ax.enqueue_job(
                account_id, "unsend_messages",
                payload={"targets": [{"story_id": int(story_id)}]},
                run_at=delete_at,
                created_by_employee_id=employee_id,
            )
            entry["delete_at"] = delete_at.isoformat() + "Z"
        posted.append(entry)

    return {
        "status": "ok" if posted else "error",
        "folder_id": folder_id,
        "posted": len(posted),
        "failed": failed,
        "stories": posted,
        "hours_to_live": hours_to_live,
    }
