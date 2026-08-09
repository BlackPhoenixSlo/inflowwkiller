"""Runtime secret/key store — a gitignored JSON file the UI can write.

Onboarding without SSH: a self-hoster pastes their DeepSeek key, Google Sheets
token, etc. into Setup → Keys instead of editing the VPS `.env`. Values land in
`service/secrets.json` (sibling of `proxies.json`, same bind-mount + .gitignore
story) and every consumer checks the store BEFORE the environment, so a pasted
key takes effect on the next call with no restart — while existing env-based
deploys keep working as the fallback.

Precedence everywhere: `secrets.json` value → process env → module `.env` file.

Never logs values. The HTTP layer only ever returns `status()` (masked), never
the raw secret.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from threading import Lock

log = logging.getLogger("of-relay.secrets")

_PATH = Path(__file__).resolve().parent / "secrets.json"
_LOCK = Lock()

# The keys the UI exposes. `secret` False = shown back in clear (it's config, not
# a credential). `multiline` = render a textarea (JSON blobs). `group` orders the
# Setup card. Order here is the order the UI renders.
KNOWN: dict[str, dict] = {
    "DEEPSEEK_API_KEY": {
        "label": "DeepSeek API key",
        "group": "AI models",
        "help": "Required for AI auto-messaging. Get one at platform.deepseek.com.",
    },
    "GROK_API_KEY": {
        "label": "Grok API key",
        "group": "AI models",
        "help": "Optional second LLM provider (x.ai). Leave blank if unused.",
    },
    "GOOGLE_SHEETS_TOKEN_JSON": {
        "label": "Google Sheets token JSON",
        "group": "Google Sheets export",
        "multiline": True,
        "help": "Paste the contents of token.json (authorized-user). Drives the "
                "push-to-sheets export at runtime.",
    },
    "GOOGLE_SHEETS_CREDENTIALS_JSON": {
        "label": "Google Sheets credentials JSON",
        "group": "Google Sheets export",
        "multiline": True,
        "help": "Paste credentials.json (OAuth client). Only needed to mint a new "
                "token; the token above is what runtime uses.",
    },
    "GOOGLE_SHEETS_SPREADSHEET_ID": {
        "label": "Spreadsheet ID",
        "group": "Google Sheets export",
        "secret": False,
        "help": "The id in the sheet URL (…/d/<THIS>/edit).",
    },
    "GOOGLE_SHEETS_TAB": {
        "label": "Sheet tab",
        "group": "Google Sheets export",
        "secret": False,
        "help": "Tab the export writes to (default: Main).",
    },
    "SHARE_TOKEN": {
        "label": "Access password (share link token)",
        "group": "Access",
        "help": "Set to require ?t=<token> on the public URL. Blank = the "
                "friend-auth login is the only gate. Takes effect immediately.",
    },
}


# (mtime_ns, size, inode) of the file the cached dict was parsed from, or None
# for "nothing cached yet". No lock: a torn read is impossible because the tuple
# and the dict are published in ONE assignment, and the worst race is two
# threads parsing the same bytes and storing equal values.
_CACHE: tuple[tuple[int, int, int, int], dict] | None = None


def _stamp() -> tuple[int, int, int, int] | None:
    """The identity of the file on disk, or None if it isn't there.

    `stat(2)` takes a PATH, not a descriptor, so this keeps answering while the
    process is out of descriptors — the property the whole cache rests on.

    Identity, not just a timestamp: `_atomic_write` publishes via `os.replace`,
    so a rewrite that lands inside a single mtime tick still arrives on a NEW
    inode and the stamp changes even when the clock doesn't. `st_dev` pairs
    with the inode because inode numbers are only unique within a filesystem,
    and this path is a bind-mount target.
    """
    try:
        st = _PATH.stat()
    except FileNotFoundError:
        return None
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)


def _load() -> dict:
    """The parsed store, re-read only when the file on disk has changed.

    This runs in the OUTERMOST middleware on every single request
    (`_effective_share_token` → `stored("SHARE_TOKEN")` in server.py's
    `_share_token_gate`), so the `open()` it used to do unconditionally was one
    descriptor per request in the hottest path in the process — and descriptors
    are what the relay runs out of (2026-07-28, 2026-08-08). Under that
    shortage the open failed, this returned `{}`, and the share token silently
    reverted to the env default for the duration: the gate CHANGED because the
    process was busy. Serving the last-known-good parse instead is both cheaper
    and more correct.

    Callers get a copy — `set_many` mutates what it gets back before writing,
    and it must not edit the cache in place ahead of the write landing.
    """
    global _CACHE
    stamp = _stamp()
    if stamp is None:
        _CACHE = None
        return {}
    cached = _CACHE
    if cached is not None and cached[0] == stamp:
        return dict(cached[1])
    try:
        with open(_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        _CACHE = None
        return {}
    except OSError:
        # Out of descriptors, or the file went unreadable mid-flight. The last
        # good parse is a far better answer than "no secrets are set" — that
        # answer silently swaps the access gate's token.
        log.warning("secrets.json could not be opened — serving last known good",
                    exc_info=True)
        return dict(cached[1]) if cached is not None else {}
    except Exception:  # pragma: no cover — a corrupt file must not crash callers
        log.warning("secrets.json unreadable — ignoring", exc_info=True)
        # Cache the verdict, not just the parse. A file that is there but
        # unparseable answers "no secrets" every time it is asked, and without
        # this it would answer that by re-opening the file on every request —
        # exactly the per-request descriptor this cache exists to remove.
        _CACHE = (stamp, {})
        return {}
    if not isinstance(data, dict):
        _CACHE = (stamp, {})
        return {}
    _CACHE = (stamp, data)
    return dict(data)


def stored(name: str) -> str:
    """The value from secrets.json only (""/absent → empty). This is the bit
    consumers prepend ahead of their own env/.env lookup."""
    v = _load().get(name)
    return v.strip() if isinstance(v, str) else ""


def get(name: str) -> str:
    """Convenience resolver for new code: store value, else process env."""
    return stored(name) or (os.environ.get(name) or "").strip()


def set_many(values: dict[str, str | None]) -> None:
    """Merge updates into secrets.json. A None/empty value CLEARS the key.
    Unknown keys are ignored. Atomic write, chmod 600."""
    with _LOCK:
        data = _load()
        for k, v in values.items():
            if k not in KNOWN:
                continue
            if v is None or not str(v).strip():
                data.pop(k, None)
            else:
                data[k] = str(v).strip()
        _atomic_write(data)


def _atomic_write(data: dict) -> None:
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(_PATH.parent), prefix=".secrets.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
        os.replace(tmp, _PATH)
        try:
            os.chmod(_PATH, 0o600)
        except OSError:  # pragma: no cover — best-effort on odd filesystems
            pass
        # Publish what we just wrote instead of waiting to re-read it. We know
        # the content, so the next `_load()` costs a stat and nothing else —
        # and a Setup → Keys edit takes effect on the very next request without
        # depending on the stamp having moved. A failed stat here just leaves
        # the cache cold, which re-reads: correct, only slower.
        global _CACHE
        stamp = _stamp()
        _CACHE = (stamp, dict(data)) if stamp is not None else None
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _hint(name: str, val: str, meta: dict) -> str:
    """A safe preview of a set value — never the full secret."""
    if not val:
        return ""
    if meta.get("secret", True) is False:
        return val if len(val) <= 80 else val[:77] + "…"
    if meta.get("multiline"):
        return f"•••• ({len(val)} chars)"
    return "••••" if len(val) <= 8 else "••••" + val[-4:]


def status() -> dict:
    """UI-facing view: for each known key whether it's set, a masked hint, and
    where the live value comes from (store vs env vs unset). Raw secrets never
    leave this process."""
    data = _load()
    out: dict[str, dict] = {}
    for name, meta in KNOWN.items():
        sval = data.get(name)
        sval = sval.strip() if isinstance(sval, str) else ""
        env = (os.environ.get(name) or "").strip()
        source = "store" if sval else ("env" if env else "unset")
        live = sval or env
        out[name] = {
            "label": meta["label"],
            "group": meta["group"],
            "secret": meta.get("secret", True),
            "multiline": bool(meta.get("multiline")),
            "help": meta.get("help", ""),
            "set": bool(live),
            "source": source,
            "hint": _hint(name, live, meta),
        }
    return out
