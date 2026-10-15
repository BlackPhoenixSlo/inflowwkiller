#!/usr/bin/env python3
"""
Step 2: Complete the signing rules for the latest captured session.

Reads sessions/latest.json, derives missing checksum_indexes / checksum_constant
from the captured 2313.js, verifies against the runtime samples, and writes the
complete rules back into the session JSON.

After this runs, the session is signing-ready: of_signer.sign(url, user_id, rules)
should produce signatures OF accepts.

Run:
  python3 service/extract_rules.py
  python3 service/extract_rules.py path/to/session_<ts>.json   # explicit file
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from of_signer import derive_rules_from_chunk, verify_rules, sign  # noqa: E402

SESSIONS_DIR = HERE / "sessions"


def _resolve_session_path(arg: str | None) -> Path:
    if arg:
        p = Path(arg)
        if not p.is_absolute():
            p = (HERE.parent / p).resolve()
        if not p.exists():
            raise SystemExit(f"[!] Session file not found: {p}")
        return p

    # Multi-account: prefer the active account's latest session.
    sys.path.insert(0, str(HERE))
    import accounts as account_registry  # local import to keep CLI deps minimal
    aid = account_registry.get_active_account_id()
    if aid:
        sp = account_registry.latest_session_path(aid)
        if sp:
            return sp

    # Legacy flat fallback (pre-multi-account installs)
    latest = SESSIONS_DIR / "latest.json"
    if latest.exists():
        meta = json.loads(latest.read_text())
        session_path = SESSIONS_DIR / meta["session"]
        if session_path.exists():
            return session_path

    raise SystemExit("[!] No session yet. Run capture_session.py or "
                     "POST /admin/session/bootstrap first.")


def main(argv: list[str]) -> int:
    session_path = _resolve_session_path(argv[1] if len(argv) > 1 else None)
    print(f"[*] Session: {session_path.relative_to(HERE.parent)}")

    session = json.loads(session_path.read_text())
    signing = session["signing"]
    rules = signing.get("rules") or {}
    samples = signing.get("samples") or []

    # Sanity: do we have what we need?
    required_runtime = ["static_param", "start", "end"]
    missing = [k for k in required_runtime if not rules.get(k)]
    if missing:
        print(f"[!] Session missing runtime-captured fields: {missing}")
        print("    Re-run capture_session.py — webpack hooks didn't capture everything.")
        return 1

    if not samples:
        print("[!] No signing samples — can't disambiguate constant sign.")
        return 1

    # 1. If we already have indexes from a prior derive, skip — but verify
    if rules.get("checksum_indexes") and rules.get("checksum_constant") is not None:
        ok, results = verify_rules(rules, samples)
        if ok:
            print(f"[*] Rules already complete & verified against {len(samples)} samples.")
            _print_rules(rules)
            return 0
        print("[!] Stored rules failed verification — re-deriving from chunk.")

    # 2. Read the captured 2313.js
    chunk_filename = signing.get("chunk_2313_file")
    if not chunk_filename:
        print("[!] Session has no chunk_2313_file. Re-capture.")
        return 1

    chunk_path = session_path.parent / chunk_filename
    if not chunk_path.exists():
        print(f"[!] Chunk file missing: {chunk_path}")
        return 1

    chunk_code = chunk_path.read_text()
    print(f"[*] Loaded {chunk_path.name} ({len(chunk_code)} bytes)")

    # 3. Derive
    derived = derive_rules_from_chunk(chunk_code, samples)
    if not derived:
        print("[!] Could not derive checksum_indexes/constant from the chunk.")
        print("    OF may have changed the obfuscation pattern in this revision.")
        print(f"    Inspect {chunk_path} manually; update _NUM5_* regexes in of_signer.py.")
        return 2

    rules["checksum_indexes"] = derived["checksum_indexes"]
    rules["checksum_constant"] = derived["checksum_constant"]

    # 4. Verify
    ok, results = verify_rules(rules, samples)
    if not ok:
        mismatches = [r for r in results if not r["ok"]]
        print(f"[!] Verification failed: {len(mismatches)}/{len(results)} mismatches.")
        for m in mismatches[:3]:
            print(f"      sha1={m['sha1Hex'][:12]}.. expected={m['expected']} got={m['got']}")
        return 3

    print(f"[*] Verified against all {len(samples)} samples")

    # 5. Persist back into the session JSON
    session["signing"]["rules"] = rules
    session_path.write_text(json.dumps(session, indent=2))
    print(f"[*] Updated {session_path.name} with derived rules")

    # 6. Self-test: produce a signature for a sample URL and print it
    demo = sign(
        url=f"https://onlyfans.com/api2/v2/users/{session['headers']['user_id']}",
        user_id=session["headers"]["user_id"],
        rules=rules,
    )
    print()
    _print_rules(rules)
    print()
    print(f"[*] Demo signature (fresh time):")
    print(f"      sign: {demo['sign']}")
    print(f"      time: {demo['time']}")
    return 0


def _print_rules(rules: dict) -> None:
    print("─" * 60)
    print("SIGNING RULES")
    print("─" * 60)
    print(f"  static_param:      {rules['static_param']}")
    print(f"  start / end:       {rules['start']} / {rules['end']}")
    print(f"  checksum_indexes:  {rules['checksum_indexes']}")
    print(f"  checksum_constant: {rules['checksum_constant']}")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
