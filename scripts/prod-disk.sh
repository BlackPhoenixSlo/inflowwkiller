#!/usr/bin/env bash
# prod-disk.sh — READ-ONLY disk audit of the fastt.lol VPS.
#
# Answers "what is eating the 96G root filesystem" without deleting anything.
# Companion to prod-read.sh (SQL) and health-check.sh (uptime/integrity):
# same one-round-trip `ssh … bash -s` shape, same read-only contract.
#
# Prints, in order:
#   - df (space + inodes)
#   - docker system df (images / containers / volumes / build cache, reclaimable)
#   - per-container json.log sizes  ← the usual culprit, uncapped by default
#   - top-level /  and  ~  and  ~/fastt  breakdowns
#   - anything ≥ 200M anywhere under / (the long-tail finder)
#
# Usage: scripts/prod-disk.sh
#
# NOT a cleaner. Deleting is a separate, deliberate act — see the report it
# prints and decide per line. Nothing here writes, prunes, or restarts.
set -uo pipefail

VPS="${VPS:-root@YOUR_VPS_IP}"

ssh -o ConnectTimeout=20 -o BatchMode=yes "$VPS" 'bash -s' <<"REMOTE"
echo "════ df ════"
df -h / /var 2>/dev/null | sort -u
echo
echo "inodes:"; df -i / | tail -1
echo

echo "════ docker system df ════"
docker system df 2>/dev/null
echo

echo "════ container logs (json.log) ════"
# Uncapped docker json-file logs are the #1 silent disk eater on this box.
for d in /var/lib/docker/containers/*/; do
  id="$(basename "$d")"
  log="$d$id-json.log"
  [ -f "$log" ] || continue
  name="$(docker inspect -f '{{.Name}}' "$id" 2>/dev/null | tr -d '/')"
  printf "%8s  %s\n" "$(du -h "$log" 2>/dev/null | cut -f1)" "${name:-$id}"
done | sort -rh
echo

echo "════ top-level / ════"
du -xh --max-depth=1 / 2>/dev/null | sort -rh | head -15
echo

echo "════ ~ (root home) ════"
du -xh --max-depth=1 ~ 2>/dev/null | sort -rh | head -15
echo

echo "════ ~/fastt ════"
du -xh --max-depth=1 ~/fastt 2>/dev/null | sort -rh | head -20
echo

echo "════ files ≥ 200M anywhere on / ════"
find / -xdev -type f -size +200M -printf '%10s  %p\n' 2>/dev/null \
  | sort -rn | head -30 \
  | awk '{ printf "%8.2f GB  %s\n", $1/1073741824, $2 }'
echo

echo "════ docker images (largest 15) ════"
docker images --format "{{.Size}}\t{{.Repository}}:{{.Tag}}\t{{.CreatedSince}}" 2>/dev/null \
  | sort -rh | head -15
REMOTE
