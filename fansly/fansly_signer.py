"""
Fansly request signing.

Pure-Python port of the `fansly-client-check` header the Fansly web client
attaches to every API call. No I/O — feed it a chunk / samples, get values out.

NOTE: same spirit as service/of_signer.py — this embodies our reverse-engineered
signing approach. Keep it private if the rest of the service goes open source.

The scheme (recovered from main.<hash>.js, class `tQ`):

    interceptRequest(req):
        deviceId  = deviceService.getDeviceIdSync()
        sessionId = sessionService.getActiveSession().id
        path      = new URL(req.url).pathname          # path only, no query
        digest    = cache[path] ||= cyrb53(checkKey + "_" + path + "_" + deviceId).toString(16)
        headers   = { fansly-client-id: deviceId,
                      fansly-client-ts: currentCachedTimestamp_ + serverOffset,
                      fansly-session-id: sessionId,
                      fansly-client-check: digest }

Three things that matter and are easy to get wrong:

  1. The digest is over the **path only**. No method, no body, no query string,
     no timestamp. So it is stable per path and the client caches it (LRU 100).
     `fansly-client-ts` is sent but is NOT an input to the hash.
  2. cyrb53 is a 53-bit non-crypto hash in JS `Math.imul` semantics. A naive
     Python port overflows into bignums and silently produces wrong digests.
  3. `checkKey_` is assigned twice in the constructor. The first assignment is a
     decoy. Hence `candidate_check_keys()` returns all of them and
     `derive_check_key()` picks the one that verifies against real samples.

Public surface:

    cyrb53(text, seed=0) -> int
    client_check(path, device_id, check_key=CHECK_KEY) -> str
    client_timestamp(server_offset_ms=0) -> int
    candidate_check_keys(chunk_code) -> list[str]
    derive_check_key(chunk_code, samples) -> str | None
    verify_samples(samples, check_key) -> tuple[int, list]
    find_main_js(html_or_manifest) -> str | None
"""
from __future__ import annotations

import math
import random
import re
import time as _time
from urllib.parse import urlsplit


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Recovered from main.14d24b3e179efdb7.js (fetched 2026-08-31) and verified
# against every fansly-client-check header in fanslycalls.har. Re-derive with
# derive_check_key() when Fansly ships a new bundle; do not trust this literal
# after a 403/422 storm.
CHECK_KEY = "necvac-govry3-tybkYz"

# cyrb53 constants — identical to the reference implementation, but read them
# out of the bundle again if digests start mismatching.
_H1_INIT = 0xDEADBEEF          # 3735928559
_H2_INIT = 0x41C6CE57          # 1103547991
_MUL_H1 = 2654435761
_MUL_H2 = 1597334677
_MUL_FIN_A = 2246822507
_MUL_FIN_B = 3266489909

# this.currentCachedTimestamp_ = Date.now() + (5e3 - Math.floor(1e4*Math.random()))
# refreshed on a setInterval(..., 3e3) but only ever moved forward.
_TS_JITTER_CENTER_MS = 5000
_TS_JITTER_SPAN_MS = 10000
_TS_REFRESH_INTERVAL_S = 3.0

# The client only replaces backendServerDateMsOffset_ when the drift is large.
_SERVER_DRIFT_THRESHOLD_MS = 30000

_MASK32 = 0xFFFFFFFF


# ---------------------------------------------------------------------------
# JS numeric semantics
# ---------------------------------------------------------------------------

def _int32(value: int) -> int:
    """Coerce to a signed 32-bit int, the way JS bitwise ops do."""
    value &= _MASK32
    return value - 0x100000000 if value & 0x80000000 else value


def _urshift32(value: int, bits: int) -> int:
    """JS `>>>` — unsigned right shift on the 32-bit representation."""
    return (value & _MASK32) >> bits


def imul32(a: int, b: int) -> int:
    """JS `Math.imul` — 32-bit signed integer multiply."""
    return _int32((a & _MASK32) * (b & _MASK32))


def cyrb53(text: str, seed: int = 0) -> int:
    """cyrb53 — 53-bit non-cryptographic string hash.

    Straight port of the bundle's `cyrb53(o, i=0)`. Note `charCodeAt` yields
    UTF-16 code units; `ord()` diverges above the BMP. Every input we sign
    (check key, URL path, snowflake device id) is ASCII, so it does not bite —
    but do not reuse this for arbitrary user text without fixing that.
    """
    h1 = _int32(_H1_INIT ^ seed)
    h2 = _int32(_H2_INIT ^ seed)

    for ch in text:
        code = ord(ch)
        h1 = imul32(h1 ^ code, _MUL_H1)
        h2 = imul32(h2 ^ code, _MUL_H2)

    h1 = imul32(h1 ^ _urshift32(h1, 16), _MUL_FIN_A)
    h1 ^= imul32(h2 ^ _urshift32(h2, 13), _MUL_FIN_B)
    h2 = imul32(h2 ^ _urshift32(h2, 16), _MUL_FIN_A)
    h2 ^= imul32(h1 ^ _urshift32(h1, 13), _MUL_FIN_B)

    return 4294967296 * (2097151 & h2) + _urshift32(h1, 0)


# ---------------------------------------------------------------------------
# The signer
# ---------------------------------------------------------------------------

def signed_string(path: str, device_id: str, check_key: str = CHECK_KEY) -> str:
    """The exact string that gets hashed. Split out so tests can assert on it."""
    return f"{check_key}_{path}_{device_id}"


def client_check(path_or_url: str, device_id: str, check_key: str = CHECK_KEY) -> str:
    """Compute a `fansly-client-check` value.

    Accepts a full URL or a bare path — the query string is stripped either way,
    because the client hashes `new URL(url).pathname` and nothing else.
    """
    path = urlsplit(path_or_url).path if "//" in path_or_url else path_or_url
    return f"{cyrb53(signed_string(path, device_id, check_key)):x}"


def client_timestamp(server_offset_ms: int = 0, now_ms: int | None = None) -> int:
    """A `fansly-client-ts` value, jittered the way the web client jitters it.

    Mirroring the jitter matters: a clean `int(time()*1000)` is a fingerprint,
    because the real client is never that precise.
    """
    base = now_ms if now_ms is not None else int(_time.time() * 1000)
    jitter = _TS_JITTER_CENTER_MS - math.floor(_TS_JITTER_SPAN_MS * random.random())
    return base + jitter + server_offset_ms


def server_offset_ms(server_time_ms: int, now_ms: int | None = None) -> int:
    """Port of backendServerDateMsOffset_ — only applied past 30 s of drift."""
    now = now_ms if now_ms is not None else int(_time.time() * 1000)
    drift = server_time_ms - now
    return drift if abs(drift) > _SERVER_DRIFT_THRESHOLD_MS else 0


class ClientTimestamp:
    """The client's cached, monotonically-increasing timestamp.

    The bundle recomputes on a 3 s interval and keeps the value only if it is
    greater than the one it holds, so consecutive requests inside a tick carry
    an identical `fansly-client-ts`. Reproduce that instead of stamping every
    request, or the header pattern does not look like a browser.
    """

    def __init__(self, server_offset: int = 0) -> None:
        self.server_offset = server_offset
        self._value = client_timestamp(server_offset)
        self._last_refresh = _time.monotonic()

    def value(self) -> int:
        now = _time.monotonic()
        if now - self._last_refresh >= _TS_REFRESH_INTERVAL_S:
            self._last_refresh = now
            candidate = client_timestamp(self.server_offset)
            if candidate > self._value:
                self._value = candidate
        return self._value


class DigestCache:
    """Per-path digest cache — the bundle's `hashCache_` + 100-entry LRU."""

    MAX_ENTRIES = 100

    def __init__(self, device_id: str, check_key: str = CHECK_KEY) -> None:
        self.device_id = device_id
        self.check_key = check_key
        self._order: list[str] = []
        self._cache: dict[str, str] = {}

    def get(self, path_or_url: str) -> str:
        path = urlsplit(path_or_url).path if "//" in path_or_url else path_or_url
        cached = self._cache.get(path)
        if cached is not None:
            return cached
        digest = client_check(path, self.device_id, self.check_key)
        self._cache[path] = digest
        self._order.append(path)
        while len(self._order) > self.MAX_ENTRIES:
            self._cache.pop(self._order.pop(0), None)
        return digest


# ---------------------------------------------------------------------------
# Recovering the check key from the bundle
# ---------------------------------------------------------------------------

_MAIN_JS_RE = re.compile(r'"(/?main\.[0-9a-f]+\.js)"', re.IGNORECASE)
_SRC_MAIN_JS_RE = re.compile(r'src\s*=\s*"(main\.[^"]*?\.js)"', re.IGNORECASE)

_STRING_LIT = r'"[^"]*"|\'[^\']*\''
_ASSIGN_RE = re.compile(r"this\.checkKey_\s*=\s*")
_PUSH_RE = re.compile(r"(\w+)\.push\(\s*(" + _STRING_LIT + r")\s*\)")

# Anchored at the start of the window that follows `this.checkKey_=`. Anchoring
# beats hunting for the end of the statement: the array-literal form contains
# commas, so any "read to the next , or ;" rule truncates it.
_JOIN_VAR_RE = re.compile(r"\A(\w+)\.join\(\s*(" + _STRING_LIT + r")\s*\)")
_ARRAY_JOIN_RE = re.compile(
    r"\A\[([^\]]*)\]((?:\.reverse\(\))?)\.join\(\s*(" + _STRING_LIT + r")\s*\)"
)
_CONCAT_LIT_RE = re.compile(r"\A\s*\+\s*(" + _STRING_LIT + r")")
_BARE_LIT_RE = re.compile(r"\A(" + _STRING_LIT + r")")

# How much source after the assignment / before it we are willing to look at.
_EXPR_WINDOW = 400
_PUSH_WINDOW = 2000


def find_main_js(text: str) -> str | None:
    """Pull the `main.<hash>.js` filename out of the homepage HTML or ngsw.json.

    ngsw.json (the Angular service-worker manifest) is the more reliable of the
    two — the homepage HTML is server-rendered and the tag shape has moved
    before. Both are checked.
    """
    match = _SRC_MAIN_JS_RE.search(text) or _MAIN_JS_RE.search(text)
    return match.group(1).lstrip("/") if match else None


def _unquote(literal: str) -> str:
    return literal[1:-1]


def candidate_check_keys(chunk_code: str) -> list[str]:
    """Every value `this.checkKey_` is assigned in the bundle, in source order.

    As of main.14d24b3e179efdb7.js the constructor assigns it twice:

        this.checkKey_ = ["fySzis","oybZy8"].reverse().join("-") + "-bubayf"
        ...
        i.push("necvac"), i.push("govry3"), i.push("tybkYz")
        this.checkKey_ = i.join("-")

    The first is a decoy — it is overwritten a few statements later. We do not
    guess which is live; we return both and let verification decide.

    Handles three expression shapes: a bare literal, an array-literal
    `.reverse()?.join(sep)` optionally concatenated with a literal, and a
    `<var>.join(sep)` fed by preceding `<var>.push("...")` calls. Anything else
    is skipped rather than guessed at — a new shape shows up as a verification
    failure, not as a wrong key.
    """
    keys: list[str] = []

    for match in _ASSIGN_RE.finditer(chunk_code):
        window = chunk_code[match.end():match.end() + _EXPR_WINDOW]
        preamble = chunk_code[max(0, match.start() - _PUSH_WINDOW):match.start()]
        value = _eval_key_expr(window, preamble)
        if value and value not in keys:
            keys.append(value)

    return keys


def _eval_key_expr(window: str, preamble: str) -> str | None:
    """Evaluate the tiny expression grammar Fansly uses to hide the key.

    `window` is source starting immediately after `this.checkKey_=`; only the
    leading expression is consumed, so trailing statements are harmless.
    """
    # <var>.join("-")  — collect the pushes that fed <var>.
    join_var = _JOIN_VAR_RE.match(window)
    if join_var:
        var, sep = join_var.group(1), _unquote(join_var.group(2))
        parts = [_unquote(lit) for name, lit in _PUSH_RE.findall(preamble) if name == var]
        return sep.join(parts) if parts else None

    # ["a","b"].reverse().join("-") + "-c"
    arr = _ARRAY_JOIN_RE.match(window)
    if arr:
        items = [m.group(0) for m in re.finditer(_STRING_LIT, arr.group(1))]
        items = [_unquote(i) for i in items]
        if arr.group(2):
            items.reverse()
        value = _unquote(arr.group(3)).join(items)
        rest = window[arr.end():]
        while True:
            concat = _CONCAT_LIT_RE.match(rest)
            if not concat:
                break
            value += _unquote(concat.group(1))
            rest = rest[concat.end():]
        return value

    # Bare string literal.
    bare = _BARE_LIT_RE.match(window)
    if bare:
        return _unquote(bare.group(1))

    return None


def verify_samples(samples: list[dict], check_key: str) -> tuple[int, list[dict]]:
    """Recompute every captured digest. Returns (matched, mismatches).

    A sample is `{"path": ..., "device_id": ..., "check": ...}`. This is the
    Fansly analogue of of_signer.verify_rules — a key is not trusted until this
    returns zero mismatches.
    """
    matched = 0
    mismatches: list[dict] = []

    for sample in samples:
        expected = sample["check"]
        actual = client_check(sample["path"], sample["device_id"], check_key)
        if actual == expected:
            matched += 1
        else:
            mismatches.append({**sample, "computed": actual})

    return matched, mismatches


def derive_check_key(chunk_code: str, samples: list[dict]) -> str | None:
    """Recover the live check key from a bundle, proven against real samples.

    Returns None rather than a guess when no candidate reproduces every sample —
    that is the signal that Fansly changed the scheme and this module needs a
    look, not that the caller should retry.
    """
    if not samples:
        raise ValueError("derive_check_key needs captured samples to verify against")

    for candidate in candidate_check_keys(chunk_code):
        _, mismatches = verify_samples(samples, candidate)
        if not mismatches:
            return candidate

    return None
