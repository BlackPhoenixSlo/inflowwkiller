"""
jsonsafe.py — parsing the JSON *text columns* this schema is full of, without a
try/except at every call site.

SQLite has no JSON type here, so per-fan state, AI field blobs, vault config, OF
frames and effective-value overlays all live in TEXT columns. Every reader faces
the same four cases — NULL, empty string, corrupt/truncated text (the payload
size cap trims outliers), and already-parsed (some callers hand these functions a
value that a layer above them decoded) — and every reader wants the same answer:
degrade to a default, never raise into a send path.

That rule had been written three separate times: `vault_ai_api._load_json`,
`vault_ai_effective._load_json` and `inbound_describe._load_json`, with three
slightly different tolerances, across ~28 call sites. This is the superset.

Deliberately dependency-free — the whole point is that a leaf module can parse a
column without importing a 3.4k-line one to do it.
"""
from __future__ import annotations

import json
from typing import Any


def load_json(raw: Any, fallback: Any) -> Any:
    """Parse a JSON text column, tolerating None / already-parsed / bad JSON.

    `raw` may already be a dict or list — callers that got their value from a
    layer which decoded it pass it straight through rather than re-serializing.
    A stored literal `null` also yields `fallback`: nothing wants None back from a
    column that was supposed to hold a structure."""
    if raw is None:
        return fallback
    if isinstance(raw, (dict, list)):
        return raw
    try:
        val = json.loads(raw)
    except Exception:
        # Deliberately bare. The narrow (TypeError, ValueError) net looks tighter
        # and is wrong: `json.loads` raises RecursionError — a RuntimeError, not a
        # ValueError — on deeply nested input, so a 20KB blob of '[' escaped this
        # function and propagated into whatever was parsing a column. That is the
        # one thing this module exists to prevent, and it matters most in
        # `parse_consistency_verdict`, which sits BETWEEN generation and send and
        # whose whole contract is to fail open. KeyboardInterrupt / SystemExit
        # derive from BaseException and still pass through.
        return fallback
    return val if val is not None else fallback


def load_dict(raw: Any) -> dict:
    """`load_json` for the common case where the column holds an OBJECT.

    Separate from `load_json(raw, {})` because that one still hands back whatever
    parsed — a column holding `"a string"` or `[1,2]` returns the str/list, and the
    caller's next line is a `.get()` that raises. Anything that isn't a dict is
    treated as unset, which is what every object-shaped column actually wants."""
    val = load_json(raw, {})
    return val if isinstance(val, dict) else {}
