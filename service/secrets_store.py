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


def _load() -> dict:
    try:
        with open(_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:  # pragma: no cover — a corrupt file must not crash callers
        log.warning("secrets.json unreadable — ignoring", exc_info=True)
        return {}


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
