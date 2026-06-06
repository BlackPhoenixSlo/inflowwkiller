"""service/automations/auto_stories.py — scheduled "stories from a vault folder".

Drives the Settings → Auto stories tab. On each trigger it:

  1. lists photos in one vault folder (`folder_id`),
  2. picks `per_run` random non-DRM photos (default 1),
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
      "folder_id": 22454297,     # required — vault list/folder id
      "per_run": 1,              # stories to post this trigger (default 1)
      "hours_to_live": 6,        # auto-delete after N hours (null/0 = keep)
      "watermark_text": null,    # optional watermark stamped on re-upload
      "dry_run": false           # resolve picks, post nothing
    }
"""
from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timedelta

import automation_executor as ax  # shared _make_client seam + enqueue_job
from automation_registry import register

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


def _usable_photos(client, folder_id: int) -> list[dict]:
    """Non-DRM photos with a real CDN url from one vault folder."""
    page = client.vault_media(type="photo", list_id=folder_id,
                              limit=48, field="recent", sort="desc")
    items = page.get("list") or page.get("items") or []
    out = []
    for it in items:
        full = ((it.get("files") or {}).get("full") or {})
        if full.get("url") and not full.get("drm"):
            out.append(it)
    return out


@register("auto_stories")
async def run(account_id: str, payload: dict, *, run_id: int) -> dict:
    """Post `per_run` random stories from `folder_id`, scheduling each delete."""
    payload = payload or {}
    folder_id = payload.get("folder_id")
    try:
        folder_id = int(folder_id)
    except (TypeError, ValueError):
        return {"status": "skipped", "reason": "no_folder_id"}

    per_run = _pos_int(payload.get("per_run"), 1)
    hours_to_live = _pos_float(payload.get("hours_to_live"))
    watermark_text = payload.get("watermark_text") or None

    client = await asyncio.to_thread(ax._make_client, account_id)

    usable = await asyncio.to_thread(_usable_photos, client, folder_id)
    if not usable:
        return {"status": "skipped", "reason": "no_usable_photos",
                "folder_id": folder_id}

    n = min(per_run, len(usable))
    picks = random.sample(usable, n)

    # ── dry_run: report the picks, touch nothing ────────────────────────
    if payload.get("dry_run"):
        return {
            "dry_run": True, "folder_id": folder_id,
            "would_post": [p.get("id") for p in picks],
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
    for pick in picks:
        url = pick["files"]["full"]["url"]
        try:
            story = await asyncio.to_thread(
                lambda u=url: client.post_story_from_url(u, watermark_text=watermark_text)
            )
        except Exception as e:  # noqa: BLE001 — one bad pick shouldn't strand the rest
            failed += 1
            log.warning("auto_stories: post failed vault=%s: %s", pick.get("id"), e)
            continue
        story_id = story.get("id") if isinstance(story, dict) else None
        entry = {"vault_id": pick.get("id"), "story_id": story_id}

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
