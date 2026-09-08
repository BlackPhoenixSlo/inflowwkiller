#!/usr/bin/env bash
# Refuse to let a real identity reach the public mirror.
#
# The export to inflowwkiller maps real creator names to placeholders. On
# 2026-09-08 an audit found it had missed eleven lines across eight already-
# pushed files: the misses were lowercase forms and a name embedded in a handle
# (@name_xo), which the mapping's word-boundary rule stepped over. This script
# is the dumb backstop that would have caught every one of them.
#
# Run it against the PUBLIC checkout before pushing:
#     scripts/check-public-export.sh ~/inflowwkiller
# Exit 0 = clean. Exit 1 = something real is about to go public.
#
# Keep this file in THIS repo only. It names the things it looks for, so it is
# itself unpublishable.
set -uo pipefail

TARGET="${1:-$HOME/inflowwkiller}"
[ -d "$TARGET/.git" ] || { echo "not a git checkout: $TARGET" >&2; exit 2; }

# Case-insensitive, bounded on LETTERS only, not on word characters. That is the
# whole trick: "@name_xo" and "name_cam.py" still match, because "_" is not a
# letter, while "lexicon" and "flexible" do not, because "c" and "b" are. A
# \\b-style word boundary gets this exactly backwards and is why the leak
# survived the original mapping.
NAMES='ava|isabelle|ariafree|ariapaid|sofiapaid|Dana'
# Real account/fan ids. Fansly/OF snowflakes are 18 digits. The public tree
# deliberately keeps snowflake-SHAPED ids so the float64-precision tests still
# reproduce the live bug, so this cannot simply flag long numbers. The synthetic
# ones all begin 900/700/500; a live id begins with something else. Widen this
# list if a new synthetic prefix is ever introduced.
IDS='(?<![\d.])(?!900|700|500)\d{18}(?![\d.])'
# Absolute paths off this machine. (?-i) is load-bearing: run() greps case-
# insensitively for the name rule, and without it this matches every
# /users/list route in the app.
PATHS='(?-i)/Users/[a-z]'

fail=0
run() {  # run <label> <perl-regex>
  local label="$1" re="$2" out
  out=$(git -C "$TARGET" grep -inP "$re" -- . 2>/dev/null) || return 0
  [ -z "$out" ] && return 0
  echo "── $label ──"
  echo "$out" | sed 's/^/   /'
  echo
  fail=1
}

run "real creator name"          "(?i)(?<![a-z])($NAMES)(?![a-z])"
run "real account / fan id"      "$IDS"
run "absolute local path"        "$PATHS"

if [ "$fail" -ne 0 ]; then
  echo "REFUSING: the lines above must be sanitized before $TARGET is pushed." >&2
  exit 1
fi
echo "clean: no real identity found in $TARGET"
