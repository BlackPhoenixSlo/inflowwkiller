"""Runtime secret/key store — a gitignored JSON file the UI can write.

Onboarding without SSH: a self-hoster pastes their DeepSeek key, Google Sheets
token, etc. into Setup → Keys instead of editing the VPS `.env`. Values land in
`service/secrets/secrets.json` — a bind-mounted DIRECTORY, see the comment on
`_PATH`, and one an older VPS needs adding to its compose file by hand — and
every consumer checks the store BEFORE the environment, so a pasted key takes
effect on the next call with no restart while env-based deploys keep working.

Precedence: store value → process env → module `.env` file. ONE exception, and
it is load-bearing: `_BOOTSTRAP_ONLY` keys (ADMIN_PASSWORD) are resolved
ENV-FIRST by their reader (`auth._read_admin_password`) and reported env-first
by `status()`. Store-first there made a UI-set founder password permanent —
the store refused to change it and no `.env` value could outrank it.

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

# Inside a DIRECTORY that the container bind-mounts, not beside this module.
#
# `service/secrets.json` was never bind-mounted, and docker-compose said so in
# its own comment: every value pasted into Setup → Keys died on the next
# `up -d --build`. That is survivable for an API key an operator re-pastes; it
# is not survivable for ADMIN_PASSWORD, because a deploy would silently delete
# the founder's second factor AND re-open the bootstrap window that
# `_BOOTSTRAP_ONLY` exists to close. It happened: a password set on 2026-08-20
# was gone by the next deploy.
#
# A DIRECTORY mount, not a file mount: Docker materialises a missing bind-mount
# path as a directory, so mounting the file itself turns a first deploy on a
# fresh host into a directory where the JSON should be. Mounting the parent is
# the shape that cannot go wrong — the app creates the file inside it.
_DIR = Path(__file__).resolve().parent / "secrets"
_PATH = _DIR / "secrets.json"
# Where it used to live. Read once at import if the durable copy is absent, so
# a plain restart (as opposed to a rebuild) does not lose what is already set.
_LEGACY_PATH = Path(__file__).resolve().parent / "secrets.json"
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
    "ADMIN_PASSWORD": {
        "label": "Founder password",
        "group": "Access",
        "help": "Second factor on founder-only writes: granting or revoking a "
                "model, transferring one, deleting a user, and setting another "
                "agency's AI keys. BOOTSTRAP ONLY — set it here once; changing "
                "it is done in the relay's environment (ADMIN_PASSWORD in "
                "~/fastt/.env, then restart), which outranks this value.",
    },
    "SHARE_TOKEN": {
        "label": "Access password (share link token)",
        "group": "Access",
        "help": "Set to require ?t=<token> on the public URL. Blank = the "
                "friend-auth login is the only gate. Takes effect immediately.",
    },
}


# The one character a masked value is made of, and the one function that makes
# one. Both Setup cards render through `mask()`, so "what a masked key looks
# like" has a single definition — which is what lets a write path reject a value
# containing MASK_CHAR and know it is rejecting its OWN placeholder coming back
# (see tenant_keys.set_keys, and the settings-form bug that guard exists for).
MASK_CHAR = "•"


def mask(val: str) -> str:
    """A safe preview of a secret: bullets, plus the last four if there is
    enough to spare. Never enough to reconstruct the value."""
    if not val:
        return ""
    return MASK_CHAR * 4 if len(val) <= 8 else MASK_CHAR * 4 + val[-4:]


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


def _adopt_legacy_file() -> None:
    """One-way move of the pre-mount store into the mounted directory.

    Only when the durable copy does not exist yet — never an overwrite. A
    rebuild destroys the legacy file before this can run, which is the whole
    problem; this recovers the restart case and costs one stat at import.
    """
    try:
        if _PATH.exists() or not _LEGACY_PATH.is_file():
            return
        _DIR.mkdir(parents=True, exist_ok=True)
        _PATH.write_text(_LEGACY_PATH.read_text(encoding="utf-8"), encoding="utf-8")
        try:
            os.chmod(_PATH, 0o600)
        except OSError:
            pass
        log.info("secrets_store: adopted legacy %s into %s", _LEGACY_PATH, _PATH)
    except Exception:
        # A store that cannot be adopted must not stop the relay booting; the
        # env is still a complete source for every key here.
        log.exception("secrets_store: could not adopt the legacy secrets file")


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


# Keys this store may CREATE but never CHANGE. `ADMIN_PASSWORD` is the second
# factor on every founder-only write, and the page that edits this store is
# gated by the master SESSION — so if the session could rotate the password, a
# stolen cookie would be the only thing standing between an attacker and every
# founder operation, and the two factors would collapse into one.
#
# Bootstrapping is still allowed because the alternative is worse: this
# deployment shipped with the variable undocumented and unset, which does not
# make founder writes safe, it makes them all 503. Being able to set the first
# one from the UI is the difference between a working second factor and none.
# Rotation stays where it cannot be reached by a session: the VPS .env. That
# only works because `auth._read_admin_password` reads the ENV FIRST for this
# key — store-first plus bootstrap-only would make a UI-set password permanent,
# with `.env` silently outranked and this store refusing to change it.
_BOOTSTRAP_ONLY = ("ADMIN_PASSWORD",)


def _assert_bootstrap_only(values: dict[str, str | None]) -> None:
    for k in _BOOTSTRAP_ONLY:
        if k not in values:
            continue
        raw = values[k]
        # str() first: this runs BEFORE the loop below coerces, and the body is
        # arbitrary JSON — a number or a list here would AttributeError inside
        # the founder gate rather than surfacing as a 400.
        blank = not str(raw or "").strip()
        superseded = (os.environ.get(k) or "").strip()
        if superseded and blank:
            # RETIRING a superseded copy is not rotating. Once the environment
            # carries the live value the reader takes THAT (env-first, see
            # `auth._read_admin_password`) and this row is a dead credential —
            # one that comes back to life the moment the env var goes missing,
            # on a file that now survives deploys. Deleting it is the operator
            # tidying up after a rotation they performed somewhere a session
            # cannot reach, so it stays allowed. Only a CLEAR, and only while
            # the env holds a value: a session can still never set or change
            # the password that is actually in force.
            continue
        if get(k):
            raise ValueError(
                f"{k} is already configured and cannot be changed from here — "
                "set ADMIN_PASSWORD in the relay's environment (~/fastt/.env) "
                "and restart; it outranks this value, and you can then clear "
                "this one"
            )


def set_many(values: dict[str, str | None]) -> None:
    """Merge updates into secrets.json. A None/empty value CLEARS the key.
    Unknown keys are ignored. Atomic write, chmod 600.

    A value made of the mask this store itself renders is REFUSED, not stored:
    that is our own placeholder coming back from a form, and taking it literally
    replaces a working secret with bullets. `mask()` is the single definition of
    what one looks like, which is what makes the test sound. It raises rather
    than dropping the field, so the caller cannot report "Saved" for a write that
    did not happen — same contract as `tenant_keys.set_keys`.
    """
    for k, v in values.items():
        if k in KNOWN and v is not None and MASK_CHAR in str(v):
            raise ValueError(
                f"{k}: that looks like the masked placeholder, not a value — "
                "leave the field blank to keep what is stored"
            )
    _assert_bootstrap_only(values)
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
    if name in _BOOTSTRAP_ONLY:
        # A password, not an API key. `mask` shows the last four characters,
        # which is a fine tail for `sk-…7132` and a real leak for a passphrase
        # — it also reveals the short-vs-long length bucket. Nobody needs to
        # recognise this value in a list; they only need to know it is set.
        return "••••••••" if val else ""
    if not val:
        return ""
    if meta.get("secret", True) is False:
        return val if len(val) <= 80 else val[:77] + "…"
    if meta.get("multiline"):
        return f"{MASK_CHAR * 4} ({len(val)} chars)"
    return mask(val)


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
        # Resolve the way the key's READER does, or the card names a dead value
        # as the live one. Every key here is store-first — except the
        # bootstrap-only ones, whose reader is env-first precisely so a UI-set
        # value can be superseded. Reporting store-first for those would show
        # "set · via UI" with the stored hint while every founder write is
        # actually being checked against the environment.
        if name in _BOOTSTRAP_ONLY:
            source = "env" if env else ("store" if sval else "unset")
            live = env or sval
        else:
            source = "store" if sval else ("env" if env else "unset")
            live = sval or env
        out[name] = {
            "label": meta["label"],
            "group": meta["group"],
            "secret": meta.get("secret", True),
            "multiline": bool(meta.get("multiline")),
            "help": meta.get("help", ""),
            "set": bool(live),
            # Would `set_many({name: ""})` be accepted? The server answering
            # directly, rather than the UI re-deriving it from `source` — which
            # answers a different question and, for a bootstrap-only key, the
            # exactly complementary one. The clear affordance keys off this, so
            # it is offered when and only when it works.
            "clearable": bool(sval) and (
                name not in _BOOTSTRAP_ONLY
                or bool((os.environ.get(name) or "").strip())
            ),
            "source": source,
            "hint": _hint(name, live, meta),
        }
    return out


_adopt_legacy_file()
