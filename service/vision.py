"""
vision.py — the primitives every vision consumer needs, and nothing else.

Downloading a frame through an account's OF client, base64-ing it, shrinking it
to a sane edge, and recognising a model refusal are shared by consumers that must
NOT import each other:

  • vault_ai_api      — describing/flagging VAULT media (hers)
  • inbound_describe  — describing what a FAN sent (his)

Same reasoning that produced `of_shapes.py` one layer down: these lived in
`vault_ai_api`, so reading a fan's photo meant importing 3.7k lines of vault
listing, folder mirroring and PPV-week machinery to base64 a JPEG. A module that
imports only PIL and the OF client can be called from anywhere.

Rules for anything added here: no db, no FastAPI, no feature policy. Prompts,
budgets and "when do we describe" belong to the consumer that owns the feature.
"""
from __future__ import annotations

import asyncio
import base64
import io
import logging
from typing import Any

log = logging.getLogger("of-relay.vision")

# Longest edge, in px, we hand a vision model by default. Vault flag/describe runs
# use this; the inbound-chat path overrides it upward (a fan's photo is read once
# and the detail matters more than the bandwidth).
MAX_EDGE_DEFAULT = 448


def shrink_data_url(data_url: str, max_edge: int = MAX_EDGE_DEFAULT) -> str:
    """Downscale a base64 data-URL to `max_edge` on its LONG side, preserving
    aspect. Returns the input unchanged if Pillow can't read it — a cheap pass
    must never take down the caller."""
    try:
        from PIL import Image

        _, _, b64 = data_url.partition(",")
        with Image.open(io.BytesIO(base64.b64decode(b64))) as im:
            im = im.convert("RGB")
            im.thumbnail((max_edge, max_edge), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=82)
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception:  # noqa: BLE001
        log.warning("vision shrink failed; sending full-size", exc_info=True)
        return data_url


# A vision model that declines shapes its refusal like prose, so length is half
# the test: a genuine description of an explicit picture is long, a refusal is a
# short apology. Both halves are needed — "i'm sorry" appears inside plenty of
# real descriptions of screenshots.
_REFUSAL_MARKERS = ("i can't", "i cannot", "i'm unable", "cannot assist", "content policy",
                    "i'm sorry", "not able to", "against my")


def is_refusal(text: str) -> bool:
    """Did the model decline instead of describing? Consumers escalate to a bigger
    model on True rather than storing an apology as a description."""
    low = (text or "").strip().lower()
    return not low or (len(low) < 40 and any(m in low for m in _REFUSAL_MARKERS))


def img_data_url(b: bytes) -> str:
    """Raw image bytes → the data-URL shape the chat completions API wants."""
    return "data:image/jpeg;base64," + base64.b64encode(b).decode()


async def download_frames(client: Any, urls: list[str]) -> list[str]:
    """Download each frame URL through the account's OF client (source-IP
    signing) and return them as base64 data-URLs. Skips failures."""
    out: list[str] = []
    for u in urls:
        try:
            resp = await asyncio.to_thread(lambda u=u: client.http.get(u, timeout=client.timeout_s))
            if resp.status_code == 200 and resp.content:
                out.append(img_data_url(resp.content))
        except Exception:  # noqa: BLE001
            log.warning("poster frame fetch failed", exc_info=True)
    return out

