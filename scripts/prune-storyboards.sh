#!/usr/bin/env bash
# Prune storyboard cache directories that haven't been touched in $TTL_DAYS
# days (atime). Per-video sub-directory under $STORYBOARD_DIR; we remove
# the whole sub-directory when its frames are stale, since the 12 frames
# of one video are useless without the others.
#
# Designed to be cron-safe and idempotent: silent unless something is
# actually removed (or an error occurs). Re-running has no effect on
# fresh data.
#
# Usage:
#   ./scripts/prune-storyboards.sh               # 7-day default
#   TTL_DAYS=3 ./scripts/prune-storyboards.sh    # override
#   STORYBOARD_DIR=/var/cache/sb ./scripts/prune-storyboards.sh

set -euo pipefail

DIR="${STORYBOARD_DIR:-/tmp/of-relay-storyboard}"
TTL_DAYS="${TTL_DAYS:-7}"

[[ -d "$DIR" ]] || exit 0

# `find -mtime +N` matches dirs whose mtime is strictly more than N days
# ago. We use mtime (not atime) because macOS APFS doesn't update atime
# under default mount options. The relay actively bumps the dir's mtime
# with os.utime() on every served frame (see proxy_image_scrub), so this
# is effectively "not used in N days" rather than "built N days ago".
# The `-mindepth 1` keeps us from accidentally targeting $DIR itself.
removed=$(find "$DIR" -mindepth 1 -maxdepth 1 -type d -mtime +"$TTL_DAYS" -print -exec rm -rf {} + 2>/dev/null | wc -l | tr -d ' ')

if [[ "$removed" -gt 0 ]]; then
  echo "[storyboard-prune] removed $removed dirs older than ${TTL_DAYS}d from $DIR"
fi
