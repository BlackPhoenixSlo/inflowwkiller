#!/usr/bin/env bash
# prod-disk-clean.sh — reclaim disk on the fastt.lol VPS. DESTRUCTIVE.
#
# Companion to prod-disk.sh (which only measures). Deletes a hardcoded
# ALLOWLIST of superseded DB backups and prunes docker's build cache.
#
# Design rules, because this runs as root on prod:
#   - Every target is a LITERAL absolute path. No globs, no `find -delete`,
#     no `rm -rf` of a directory. A typo can only ever miss, never widen.
#   - A KEEP list is checked twice: once as a pre-flight assertion that no
#     target collides with it, once after the deletes to prove they survived.
#   - Dry-run is the DEFAULT. `--yes` is required to actually delete.
#   - Touches no container, no image, no live DB, and restarts nothing.
#
# Usage:
#   scripts/prod-disk-clean.sh          # dry run — prints what WOULD go
#   scripts/prod-disk-clean.sh --yes    # do it
set -uo pipefail

VPS="${VPS:-root@YOUR_VPS_IP}"
MODE="dry"
[[ "${1:-}" == "--yes" ]] && MODE="go"

ssh -o ConnectTimeout=20 -o BatchMode=yes "$VPS" "MODE=$MODE bash -s" <<"REMOTE"
set -uo pipefail

# ── files that must exist when we're done ────────────────────────────────
# Live DB, today's (08-04) recovery pair, the newest pre-change snapshot,
# the 07-16 known-good, and the legacy stack's own live DB.
KEEP=(
  /root/fastt/service/chatterly.db
  /root/fastt/service/chatterly.db.prescrub.20260804T171004Z
  /root/fastt/service/chatterly.db.broken.20260804T152841Z
  /root/fastt-backups/prod-preInboundImage.20260725T162134Z.db
  /root/fastt-db-backup-20260716-healthy.db
  /root/chatterly/service/chatterly.db
)

# ── tier 2: June + 2026-07-14 snapshots ──────────────────────────────────
DELETE=(
  /root/fastt-prod-pre0033.20260612T124501Z.db
  /root/chatterly.db.bak_pre_autoconvo_20260620T100143Z
  /root/fastt/chatterly.db.bak.20260621T195339Z
  /root/fastt-prod-backup-pre-welcome-pin.20260703T115053Z.db
  /root/fastt-prod-backup-pre-account-health.20260710T095715Z.db
  /root/fastt-prod-backup-preprofile.20260714T144723Z.db
  /root/fastt-prod-backup-prereengage.20260714T150340Z.db
  /root/fastt-prod-backup-reengfix.20260714T151519Z.db
  /root/fastt-prod-backup-reengtype.20260714T152354Z.db
  /root/fastt-prod-backup-reengmove.20260714T154115Z.db
  /root/fastt-prod-backup-preupseller.20260714T131319Z.db
  /root/fastt-prod-backup-forceask.20260714T235410Z.db
  /root/corrupt-forensic.db
# ── tier 3: 2026-07-18 riff set + the 07-22 corruption incident ──────────
  /root/fastt-db-backups/chatterly.examples.20260718T190607Z.db
  /root/fastt-db-backups/chatterly.jobriff.20260718T191747Z.db
  /root/fastt-db-backups/chatterly.namefix.20260718T175917Z.db
  /root/fastt-db-backups/chatterly.spicyriff.20260718T194317Z.db
  /root/fastt-prod-backup-GOOD-defaults-live.20260722.db
  /root/fastt-prod-backup-pre-aichatter-backfill.20260722.db
  /root/fastt/service/chatterly.db.malformed.20260722T130003Z
  /root/fastt/service/chatterly.db.corrupt.20260722T130003Z
)

# ── pre-flight: no target may be a keeper ────────────────────────────────
for d in "${DELETE[@]}"; do
  for k in "${KEEP[@]}"; do
    if [ "$d" = "$k" ]; then
      echo "ABORT: $d is on the KEEP list" >&2; exit 9
    fi
  done
done

# ── pre-flight: every keeper must exist BEFORE we touch anything ─────────
missing=0
for k in "${KEEP[@]}"; do
  [ -f "$k" ] || { echo "ABORT: keeper missing before delete: $k" >&2; missing=1; }
done
[ "$missing" -eq 0 ] || exit 10

echo "════ before ════"; df -h / | tail -1; echo

total=0
echo "════ targets ════"
for d in "${DELETE[@]}"; do
  if [ -f "$d" ]; then
    sz=$(stat -c %s "$d")
    total=$((total + sz))
    printf "%8.2f GB  %s\n" "$(echo "$sz" | awk '{print $1/1073741824}')" "$d"
    if [ "$MODE" = "go" ]; then
      rm -f -- "$d" "$d-wal" "$d-shm"
    fi
  else
    printf "%11s  %s\n" "absent" "$d"
  fi
done
printf "\n%8.2f GB  in %d files\n\n" "$(echo "$total" | awk '{print $1/1073741824}')" "${#DELETE[@]}"

echo "════ docker build cache ════"
if [ "$MODE" = "go" ]; then
  docker builder prune -f 2>&1 | tail -3
else
  docker system df 2>/dev/null | grep -i "build cache"
  echo "(dry run — would run: docker builder prune -f)"
fi
echo

# ── post-flight: prove the keepers survived ──────────────────────────────
echo "════ keep-set verification ════"
for k in "${KEEP[@]}"; do
  if [ -f "$k" ]; then
    printf "  ok  %8s  %s\n" "$(du -h "$k" | cut -f1)" "$k"
  else
    printf "  GONE      %s\n" "$k"; missing=1
  fi
done
[ "$missing" -eq 0 ] || { echo "FAILURE: a keeper was deleted" >&2; exit 11; }
echo

echo "════ after ════"; df -h / | tail -1
[ "$MODE" = "dry" ] && echo && echo "DRY RUN — nothing was deleted. Re-run with --yes."
exit 0
REMOTE
