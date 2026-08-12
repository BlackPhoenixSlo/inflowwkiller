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
  5. if `remove_vault_dupe` (default ON), also removes the vault DUPLICATE the
     story's fresh re-upload creates: /stories can't take a bare vault id, so
     every post re-uploads the bytes → OF files a new vault item. We schedule a
     deferred `unsend_messages` cleanup carrying a `{hide_upload:{md5,size}}`
     target that, once OF finishes transcoding, resolves that new item by hash
     (vault_media_lookup_hash) and hides it (PUT /vault/media/hidden). The
     source vault id rides along as `exclude_ids` so a hash collision can never
     hide the ORIGINAL. Piggybacks on the delete job when a TTL is set; else a
     short-delay hide-only job so transcoding has finished first.

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
      "remove_vault_dupe": true,   # hide the re-uploaded vault copy (default ON)
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
import media_cotag                # release-form @mention for non-solo media
from automation_registry import register
from automations._pools import has_media_source, pick_media

log = logging.getLogger("of-relay.automation.auto_stories")

# When a story is KEPT (no TTL) but we still want its re-uploaded vault dupe
# gone, the hide has to wait for OF to finish transcoding the upload — the new
# vault item is only hash-findable once ready. This short delay covers that.
_HIDE_DELAY_MINUTES = 15


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
    # Default ON: missing/garbage → True, an explicit `false` opts a rule out.
    _rd = payload.get("remove_vault_dupe", True)
    remove_dupe = _rd if isinstance(_rd, bool) else True

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
            "remove_vault_dupe": remove_dupe,
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
        # Release-form tag. A story is the one send of_client can't auto-tag for
        # itself — OF re-uploads the media, so the body carries a convert claim
        # with no vault id in it. We still hold the SOURCE id here, so the
        # solo/not-solo call is made at this layer and rides along as the
        # @mention overlay OF's own story editor emits. `story_mention` is total
        # and owns the fail-safe; re-deciding it here would just let the two
        # copies drift.
        mention = media_cotag.story_mention(account_id, [mid])
        try:
            result = await asyncio.to_thread(
                lambda u=url, mn=mention: client.post_story_from_url(
                    u, watermark_text=watermark_text, mention=mn,
                    return_upload=remove_dupe)
            )
        except Exception as e:  # noqa: BLE001 — one bad pick shouldn't strand the rest
            failed += 1
            log.warning("auto_stories: post failed vault=%s: %s", mid, e)
            continue
        # return_upload=True → {story, upload:{md5,size}}; else a bare story dict.
        if remove_dupe and isinstance(result, dict) and "story" in result:
            story = result.get("story")
            upload = result.get("upload") or {}
        else:
            story, upload = result, {}
        story_id = story.get("id") if isinstance(story, dict) else None
        entry = {"vault_id": mid, "story_id": story_id}

        # The dupe-hide leg: hide the fresh vault item this upload created, keyed
        # by its source-byte hash, with the source id excluded so the original is
        # never touched. Only when we actually captured the upload identity.
        hide_tgt = None
        if remove_dupe and upload.get("md5") and upload.get("size"):
            hide_tgt = {"hide_upload": {
                "md5": upload["md5"],
                "size": int(upload["size"]),
                "exclude_ids": [int(mid)],
            }}

        # Cleanup scheduling: when a TTL is set, one job both deletes the story
        # and hides the dupe (fires at now+hours, so transcoding is long done).
        # When the story is KEPT, a separate short-delay job hides the dupe only.
        if story_id and hours_to_live:
            delete_at = now + timedelta(hours=hours_to_live)
            targets: list[dict] = [{"story_id": int(story_id)}]
            if hide_tgt:
                targets.append(hide_tgt)
            entry["delete_job_id"] = await ax.enqueue_job(
                account_id, "unsend_messages",
                payload={"targets": targets},
                run_at=delete_at,
                created_by_employee_id=employee_id,
            )
            entry["delete_at"] = delete_at.isoformat() + "Z"
        elif hide_tgt:
            hide_at = now + timedelta(minutes=_HIDE_DELAY_MINUTES)
            entry["hide_job_id"] = await ax.enqueue_job(
                account_id, "unsend_messages",
                payload={"targets": [hide_tgt]},
                run_at=hide_at,
                created_by_employee_id=employee_id,
            )
            entry["hide_at"] = hide_at.isoformat() + "Z"
        posted.append(entry)

    return {
        "status": "ok" if posted else "error",
        "folder_id": folder_id,
        "posted": len(posted),
        "failed": failed,
        "stories": posted,
        "hours_to_live": hours_to_live,
    }
