#!/usr/bin/env bash
# Run the plain-assert test suites (house style — no pytest) and print a summary.
#
#   ./service/run_tests.sh            # run EVERY tests/test_*.py
#   ./service/run_tests.sh style      # run only the style/split/non-native suites
#   ./service/run_tests.sh of_ai_chat autoreply   # run named suites (with or without test_ prefix)
#
# Exit code is non-zero if any suite fails.
set -u
cd "$(dirname "$0")" || exit 2
# Find the project venv python (it lives at the REPO ROOT, ../venv, not service/venv).
# Fall back through a few spots; last resort python3 (may lack deps — warns below).
PY=""
for c in ./venv/bin/python ../venv/bin/python ./.venv/bin/python ../.venv/bin/python; do
  [ -x "$c" ] && PY="$c" && break
done
[ -n "$PY" ] || PY="$(command -v python3 || echo python3)"
if ! "$PY" -c "import sqlalchemy" 2>/dev/null; then
  echo "⚠️  '$PY' is missing deps (sqlalchemy). Set VENV or activate the project venv." >&2
  echo "    Tried ./venv and ../venv from $(pwd). Override: VENV=/path/to/venv ./service/run_tests.sh" >&2
fi
# Explicit override always wins.
[ -n "${VENV:-}" ] && [ -x "$VENV/bin/python" ] && PY="$VENV/bin/python"

# The suites touched by the bubble-split / non-native / pacing work.
STYLE_SUITES=(of_ai_chat autoreply deep_convo humanize_typos ask_pacing nonnative_style send_followup send_welcome)

if [ "${1:-}" = "style" ]; then
  SUITES=("${STYLE_SUITES[@]/#/tests/test_}"); SUITES=("${SUITES[@]/%/.py}")
elif [ "$#" -gt 0 ]; then
  SUITES=(); for a in "$@"; do f="tests/${a}"; [[ "$a" == test_* ]] || f="tests/test_${a}"; [[ "$a" == *.py ]] || f="${f}.py"; SUITES+=("$f"); done
else
  SUITES=(tests/test_*.py)
fi

pass=0; fail=0; failed=()
for t in "${SUITES[@]}"; do
  name="$(basename "$t")"
  if out="$($PY "$t" 2>&1)" && echo "$out" | grep -q "all cases passed"; then
    printf "  \033[32m✓\033[0m %s\n" "$name"; pass=$((pass+1))
  else
    printf "  \033[31m✗ %s\033[0m\n" "$name"; echo "$out" | grep -iE "FAIL|Error|assert" | head -3 | sed 's/^/      /'
    fail=$((fail+1)); failed+=("$name")
  fi
done

echo "--------------------------------------------"
printf "  %d passed, %d failed (of %d)\n" "$pass" "$fail" "$((pass+fail))"
[ "$fail" -eq 0 ] && { echo "  ✅ all green"; exit 0; }
printf "  ❌ failed: %s\n" "${failed[*]}"; exit 1
