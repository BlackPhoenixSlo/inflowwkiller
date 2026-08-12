#!/usr/bin/env bash
# Enable the three prompt-shape flags (arm G) on EVERY ai_chatter account, through
# the real admin login. Idempotent — re-running just re-asserts the same true values,
# and prod-config.sh merges, so no other knob is touched.
#
#   regroup    — group blocks identity → hard → voice → situational → this turn → contract
#   drop_facts — remove THESE ARE THE FACTS ABOUT YOU where every core fact is duplicated
#                elsewhere (the guard keeps it on accounts that carry it only there)
#   task_line  — name the message the turn is answering
#
# Usage — the password lives in YOUR shell for one run, never in this file or a commit:
#   ADMIN_USER=<admin> ADMIN_PW=<pw> scripts/enable-prompt-shape.sh --dry-run   # preview
#   ADMIN_USER=<admin> ADMIN_PW=<pw> scripts/enable-prompt-shape.sh             # write
#
# --dry-run prints BEFORE/DRY-RUN for each account and writes nothing. Drop it to
# apply. prod-config.sh logs in per account and carries the session cookie; account
# ids are read from prod at runtime (never hard-coded here — the leak scan scrubs them).
set -euo pipefail
cd "$(dirname "$0")/.."

DRY=""
[ "${1:-}" = "--dry-run" ] && DRY="--dry-run"

: "${ADMIN_USER:?set ADMIN_USER (the admin username) for this run}"
: "${ADMIN_PW:?set ADMIN_PW (the admin password) for this run}"

PATCH='{"prompt_regroup_enabled":true,"prompt_drop_facts_enabled":true,"prompt_task_line_enabled":true}'

# Live ai_chatter accounts, read-only from prod. `tail -n +2` drops the header row.
ACCOUNTS=$(scripts/prod-read.sh "select account_id from account_ai_config order by account_id" | tail -n +2)
[ -n "${ACCOUNTS//[[:space:]]/}" ] || { echo "refusing: read no accounts from prod" >&2; exit 1; }

n=0
for acct in $ACCOUNTS; do
  printf '\n── %s ──\n' "$acct"
  ADMIN_USER="$ADMIN_USER" ADMIN_PW="$ADMIN_PW" scripts/prod-config.sh $DRY chatter "$acct" "$PATCH"
  n=$((n + 1))
done
printf '\n✓ %s account(s)%s\n' "$n" "${DRY:+ — dry-run, nothing written}"
