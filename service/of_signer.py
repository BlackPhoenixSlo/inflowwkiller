"""
OnlyFans request signing.

Pure-Python port of the cracked sign algorithm from
theOFchrackedextension/extension.js. No I/O.

NOTE: this module embodies our reverse-engineered signing approach. Keep it
private if/when the rest of the service goes open source — OF is more likely
to rotate the scheme if the exact derivation is public.

Three functions:

  derive_rules_from_chunk(chunk_code, samples)
      Parse the obfuscated 2313.js signing module to recover
      checksum_indexes (32 ints) and checksum_constant (signed int).
      Verifies the recovered values against every captured sample.

  verify_rules(rules, samples)
      Sanity check: recompute the captured checksumValue from sha1Hex.

  sign(url, user_id, rules, time_ms=None) -> {"sign", "time"}
      The actual request signer.
"""
from __future__ import annotations

import hashlib
import re
import time as _time
from urllib.parse import urlparse


# Layout constants of OF's checksum expression.
# The minified chunk contains 32 calls of the form `]W[<5-digit>...](0)` or
# `]W[n[...](<5-digit>, len)...](0)`, all summed. We extract those 32 numbers
# from a dense cluster of `](0)` closing-parens and mod 40 to get indexes.
_CHARCODE_CLOSE = re.compile(r"\]\(0\)")
_NUM5_BEFORE_WBRACKET = re.compile(r"(\d{5})(?=[,%]W\[)")
_NUM5_BROAD = re.compile(r"\b(\d{5})\b")
_REGION_PAD = 200          # chars to include around the dense cluster
_INDEX_COUNT = 32
_INDEX_MODULO = 40          # length of OF's W (sha1 hex char) array


def derive_rules_from_chunk(
    chunk_code: str,
    samples: list[dict],
) -> dict | None:
    """Derive checksum_indexes + checksum_constant from a captured 2313.js.

    Algorithm:
      1. Find every `](0)` (the closing of `charCodeAt(0)` calls).
      2. Locate the densest 32-call cluster — that's the checksum expression.
      3. Pull 32 five-digit numbers from that region (the index bases).
         Falls back to a broad `\\b\\d{5}\\b` match if the targeted regex
         (which expects `,W[` / `%W[` syntax) finds the wrong count.
      4. indexes = [n % 40 for n in bases]
      5. Solve constant: pick a candidate from sample0 (could be +cv or -cv
         due to abs() in the sign function), verify against all samples.

    `samples` must be the [{sha1Hex, checksumValue}, ...] list captured by
    interceptor.js. At least one sample is required to disambiguate the
    constant's sign.

    Returns dict with the derived fields, or None if extraction fails.
    """
    if not samples:
        return None

    positions = [m.start() for m in _CHARCODE_CLOSE.finditer(chunk_code)]
    if len(positions) < _INDEX_COUNT:
        return None

    # Densest cluster of 32 consecutive `](0)` positions
    best_start, best_span = 0, float("inf")
    for i in range(len(positions) - (_INDEX_COUNT - 1)):
        span = positions[i + _INDEX_COUNT - 1] - positions[i]
        if span < best_span:
            best_span = span
            best_start = i

    region_start = max(0, positions[best_start] - _REGION_PAD)
    region_end = positions[best_start + _INDEX_COUNT - 1] + _REGION_PAD
    region = chunk_code[region_start:region_end]

    bases = [int(m.group(1)) for m in _NUM5_BEFORE_WBRACKET.finditer(region)]
    if len(bases) != _INDEX_COUNT:
        bases = [int(m.group(1)) for m in _NUM5_BROAD.finditer(region)]
        if len(bases) != _INDEX_COUNT:
            return None

    indexes = [n % _INDEX_MODULO for n in bases]

    # Solve constant. abs() in sign means the captured cv could be +sum or -sum.
    s0 = samples[0]
    byte_sum_0 = _byte_sum(s0["sha1Hex"], indexes)
    for candidate in (s0["checksumValue"] - byte_sum_0,
                      -s0["checksumValue"] - byte_sum_0):
        if all(
            abs(_byte_sum(s["sha1Hex"], indexes) + candidate) == s["checksumValue"]
            for s in samples
        ):
            return {
                "checksum_indexes": indexes,
                "checksum_constant": candidate,
                "samples_verified": len(samples),
            }

    return None


def verify_rules(rules: dict, samples: list[dict]) -> tuple[bool, list[dict]]:
    """Recompute each sample's checksum from its sha1Hex and compare.
    Returns (all_match, [{sha1Hex, expected, got, ok}, ...])."""
    results = []
    indexes = rules["checksum_indexes"]
    constant = rules["checksum_constant"]
    for s in samples:
        got = abs(_byte_sum(s["sha1Hex"], indexes) + constant)
        results.append({
            "sha1Hex": s["sha1Hex"],
            "expected": s["checksumValue"],
            "got": got,
            "ok": got == s["checksumValue"],
        })
    return all(r["ok"] for r in results), results


def sign(url: str, user_id: str | int, rules: dict, time_ms: int | None = None) -> dict:
    """Generate OF `sign` + `time` headers for the given URL.

    `rules` must contain: static_param, start, end, checksum_indexes, checksum_constant.
    `user_id` is the OF numeric user id (string or int, both fine).
    `time_ms` defaults to current millis — pass an explicit value to reproduce
    a captured sample.

    Returns {"sign": "start:sha1:hexchecksum:end", "time": "<ms str>"}.
    """
    if time_ms is None:
        time_ms = int(_time.time() * 1000)

    parsed = urlparse(url)
    path = parsed.path + (f"?{parsed.query}" if parsed.query else "")

    payload = "\n".join([
        rules["static_param"],
        str(time_ms),
        path,
        str(user_id),
    ])
    sha1_hex = hashlib.sha1(payload.encode("utf-8")).hexdigest()
    checksum = _byte_sum(sha1_hex, rules["checksum_indexes"]) + rules["checksum_constant"]

    return {
        "sign": f"{rules['start']}:{sha1_hex}:{format(abs(checksum), 'x')}:{rules['end']}",
        "time": str(time_ms),
    }


def _byte_sum(sha1_hex: str, indexes: list[int]) -> int:
    """Sum the charCodes of `sha1_hex` at the given indexes — the core of the checksum."""
    return sum(ord(sha1_hex[i]) for i in indexes)
