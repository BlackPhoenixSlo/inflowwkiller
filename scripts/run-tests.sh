#!/usr/bin/env bash
# Run the whole project test suite:
#   - backend: every service/tests/test_*.py plain-assert script
#   - frontend: vitest (app/) one-shot
# Exit 0 only if everything passes. See library/TEST_PLAN.md.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$ROOT/venv/bin/python"
fail=0

echo "──────── Backend (service/tests/*.py) ────────"
if [ ! -x "$PY" ]; then
  echo "✗ venv python not found at $PY"; fail=1
else
  for t in "$ROOT"/service/tests/test_*.py; do
    [ -e "$t" ] || continue
    echo "▶ $(basename "$t")"
    "$PY" "$t" || { echo "  ✗ FAILED"; fail=1; }
  done
fi

echo "──────── Frontend (vitest, app/) ────────"
if [ -f "$ROOT/app/vitest.config.ts" ] && [ -d "$ROOT/app/node_modules/vitest" ]; then
  ( cd "$ROOT/app" && pnpm test:run ) || fail=1
else
  echo "… vitest not installed yet (run T-TESTKIT); skipping frontend"
fi

echo "─────────────────────────────────────────────"
if [ "$fail" -eq 0 ]; then echo "✅ ALL TESTS PASSED"; else echo "❌ SOME TESTS FAILED"; fi
exit "$fail"
