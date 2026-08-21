"""service/automations/_wordfilter.py — the operator's "Block sensitive words" filter.

Its own leaf module for the same reason fan_state, _clock and _funnel_signals are:
_common.py is 2.4k lines imported by 70 modules, so it is the file every merge
arbitrates as ONE unit. A `-X theirs` on a conflict anywhere in it takes out every
unrelated feature filed there too — which is exactly how this feature's wiring got
dropped once already. A self-contained feature with its own API surface
(banned_words_api), its own test file (tests/test_wordfilter) and five sender
consumers is a module, not a section of the grab-bag.

The whole feature, in the order a sender meets it:
    load_banned_words(account_id) -> (words, mode)   once per run
    filter_banned(parts, words, mode)                per turn, at the send chokepoint
    banned_hit_summary(hits)                         for the log line

`scan_outbound` is the pure single-string scanner underneath; `normalize_banned_config`
is the one parse shared with the write path so the API and the runtime cannot drift.
"""
from __future__ import annotations

import re

# The generic per-account JSON-column reader. Stays in _common (every config loader
# uses it); this module is one more consumer.
from ._common import _load_json_col


# SEPARATE from _common.apply_word_restriction (the FIXED OF-restricted-word doubler)
# and from the promo-spam audience guard (an audience skip, not a text scan): this is
# a per-account word list an operator edits in Settings, scanned against OUTBOUND text
# right before send. Two modes:
#   "block" — a hit ABORTS that send (the caller drops the message + logs the reason);
#   "mask"  — each hit is replaced by its first char + asterisks ("fuck" → "f***"),
#             and the send proceeds.
# WORD-BOUNDARY + case-insensitive ON PURPOSE: an "ass" rule must NOT trip on
# "class"/"pass" (the exact false-positive that makes a naive substring filter
# useless). Same \b alternation technique as _common._RESTRICTED_RE.
BANNED_WORD_MODES = ("block", "mask")


def _mask_hit(word: str) -> str:
    """Mask a matched word: keep the first char, the rest → asterisks ("fuck" → "f***").
    A single-char match keeps its char and gains one '*' so it is still visibly masked."""
    if not word:
        return word
    return word[0] + "*" * (len(word) - 1) if len(word) > 1 else word + "*"


def _compile_banned_re(words):
    """Boundary-aware, case-insensitive alternation over `words` (longest-first so a
    phrase matches before a word it contains). None when nothing is usable. Never raises."""
    try:
        clean, seen = [], set()
        for w in words or ():
            w = str(w).strip()
            k = w.lower()
            if w and k not in seen:
                seen.add(k)
                clean.append(w)
        if not clean:
            return None
        clean.sort(key=len, reverse=True)
        return re.compile(
            r"\b(" + "|".join(re.escape(w) for w in clean) + r")\b", re.IGNORECASE)
    except Exception:
        return None


def scan_outbound(text, words, mode="block") -> dict:
    """Scan OUTBOUND `text` against the operator's banned-word list, whole-word and
    case-insensitive. PURE + NEVER raises — None/empty text or an empty `words` list is
    a no-op → {"clean_text": <text as str>, "hits": []}.

    mode "mask"  → clean_text has every hit replaced by first-char + asterisks.
    mode "block" (or any other value) → clean_text is the ORIGINAL text; the caller
                    inspects `hits` and, if non-empty, ABORTS the send itself.
    `hits` is the list of the ORIGINAL matched substrings, in order of appearance, so a
    caller can log exactly what tripped."""
    try:
        s = "" if text is None else str(text)
        rx = _compile_banned_re(words)
        if rx is None or not s:
            return {"clean_text": s, "hits": []}
        hits = [m.group(0) for m in rx.finditer(s)]
        if not hits:
            return {"clean_text": s, "hits": []}
        if str(mode).strip().lower() == "mask":
            clean = rx.sub(lambda m: _mask_hit(m.group(0)), s)
        else:
            clean = s  # block: text unchanged; the caller drops the send on a hit
        return {"clean_text": clean, "hits": hits}
    except Exception:
        # Fail OPEN on an unexpected internal error: return the text unscanned rather
        # than crash a send. The list is a safety net, not a hard runtime dependency.
        return {"clean_text": "" if text is None else str(text), "hits": []}


def filter_banned(parts, words, mode="block") -> tuple[list[str] | None, list[str]]:
    """THE outbound banned-word decision, for every sender. One turn's bubbles in →
    (bubbles_to_send, hits) out, where a None first element means BLOCK — drop the
    whole turn.

        parts, hits = filter_banned(parts, banned_words, banned_mode)
        if parts is None: ...drop this send, log `hits`...

    Two rules this settles ONCE, so no sender has to re-derive them (they are exactly
    the "needs care" questions the per-site hookups used to leave open):
      • WHOLE-TURN, not per-bubble. A block trips if ANY bubble is dirty — shipping
        bubble 1 and swallowing bubble 2 would send a half conversation.
      • CALL IT BEFORE the nonnative/typo layers, so a typo can't smuggle a flagged
        word past the scan and a mask survives those passes.

    An empty `words` (the overwhelmingly common case) returns `parts` UNCHANGED and no
    hits — byte-identical to not calling this at all. Never raises: scan_outbound fails
    open, and an unknown mode falls back to "block" (the stricter default), matching
    normalize_banned_config."""
    parts = list(parts or ())
    if not words:
        return parts, []
    masking = str(mode).strip().lower() == "mask"
    out: list[str] = []
    hits: list[str] = []
    for p in parts:
        r = scan_outbound(p, words, "mask" if masking else "block")
        hits += r["hits"]
        out.append(r["clean_text"])
    if hits and not masking:
        return None, hits          # block: caller drops the turn
    return out, hits


def banned_hit_summary(hits) -> list[str]:
    """The stable log form of a `filter_banned` hit list: lowercased, deduped, sorted —
    so every sender's "banned word(s) %r" line reads the same in the logs."""
    return sorted({str(h).lower() for h in hits or ()})


def normalize_banned_config(cfg) -> tuple[list[str], str]:
    """Canonical parse of a banned-words config blob → (words, mode). The single
    source of truth shared by the API surface (banned_words_api) and this runtime
    reader, so their rules can't drift. Dedupe (case-insensitive, first spelling
    wins), drop blanks, cap length defensively, and pin `mode` to a member of
    BANNED_WORD_MODES (unknown → "block", the stricter fail-safe default). A
    non-dict blob normalizes to ([], "block")."""
    if not isinstance(cfg, dict):
        cfg = {}
    words: list[str] = []
    seen: set[str] = set()
    for w in cfg.get("words") or []:
        w = str(w).strip()
        k = w.lower()
        if w and k not in seen:
            seen.add(k)
            words.append(w)
    words = words[:5000]  # sane ceiling; a list this long is already a config smell
    mode = str(cfg.get("mode") or "block").strip().lower()
    if mode not in BANNED_WORD_MODES:
        mode = "block"
    return words, mode


async def load_banned_words(account_id: str) -> tuple[list[str], str]:
    """Read account_ai_config.banned_words_config_json → (words, mode). Absent / NULL /
    parse-error / empty list → ([], "block"): a no-op (nothing scanned, current behavior
    unchanged). Normalization is delegated to normalize_banned_config so the load path
    and the write path (banned_words_api) share one rule set."""
    return normalize_banned_config(
        await _load_json_col(account_id, "banned_words_config_json"))
