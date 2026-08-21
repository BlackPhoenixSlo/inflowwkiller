"""service/api_errors.py — shared OF-error → HTTP mappings for the API routers.

Lives apart from of_promos.py on purpose: that module is deliberately free of
FastAPI imports (the automation plugin scan pulls it), while these helpers ARE
the FastAPI layer. Any owner-gated OF-create route maps its failures here so
every surface tells the operator the same thing.
"""
from __future__ import annotations

from fastapi import HTTPException


def raise_if_free_page_denied(exc: Exception, feature: str) -> None:
    """OF answers "Access denied." on /trials (401) and /promotions (403) when
    the profile's subscription is FREE — verified live 2026-07-23 on jakabasej
    (free page). Map that to a clear 409 the UI can show verbatim; any other
    error falls through to the caller's generic handler."""
    if "Access denied" in repr(exc):
        raise HTTPException(
            409,
            f"OnlyFans refused: {feature} need a PAID subscription price. "
            "Set a price on the OnlyFans profile (Settings → Subscription), then retry.")
