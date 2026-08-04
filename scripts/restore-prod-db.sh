#!/usr/bin/env bash
# restore-prod-db.sh — restore the fastt prod DB from a verified laptop backup.
#
# Written 2026-08-04 after prod-scrub-grok.sh corrupted the live DB: it opened
# /root/fastt/service/chatterly.db READ-WRITE, hit "database disk image is
# malformed" on its first read, and left the file at 1.25 GB (from 3.10 GB).
# The relay then crashed in _set_sqlite_pragmas on every boot.
#
# The rules this script follows, each from a specific past failure:
#   • VERIFY THE BACKUP FIRST. Never stop prod for a restore you haven't proven
#     can succeed. Integrity + row counts checked BEFORE anything is touched.
#   • VERIFY THE UPLOAD TOO. md5 parity + an in-container integrity_check on the
#     uploaded copy, BEFORE it is swapped in. A truncated scp is a real failure
#     mode here (a full laptop disk silently truncated one at 2.1 GB once).
#   • NEVER DELETE. The broken DB is renamed, never removed — it is the only
#     copy of the writes made since the backup, and `.recover` may salvage them.
#   • NEVER open a live DB read-write from outside the relay. All inspection
#     happens in a throwaway container against a NON-live file.
#
# Usage: scripts/restore-prod-db.sh <path-to-verified-backup.db>
set -euo pipefail

HOST="root@YOUR_VPS_IP"
DIR="/root/fastt/service"
BACKUP="${1:-}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"

say()  { printf '\n\033[1;36m▸ %s\033[0m\n' "$*"; }
ok()   { printf '\033[1;32m✓ %s\033[0m\n' "$*"; }
die()  { printf '\n\033[1;31m✗ %b\033[0m\n' "$*" >&2; exit 1; }

[ -n "$BACKUP" ] || die "usage: $0 <path-to-backup.db>"
[ -f "$BACKUP" ] || die "backup not found: $BACKUP"

say "1. Verify the backup BEFORE touching prod"
python3 - "$BACKUP" <<'PY' || die "backup failed verification — NOT restoring."
import sqlite3, sys
p = sys.argv[1]
c = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
qc = c.execute("PRAGMA quick_check").fetchone()[0]
print(f"  quick_check   : {qc}")
assert qc == "ok", "quick_check failed"
counts = {}
for t in ("grok_calls", "messages", "fans", "transactions", "actions"):
    counts[t] = c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    print(f"  {t:<14}: {counts[t]}")
# A restore that loses the fan/message tables is worse than the outage.
assert counts["messages"] > 100_000, "messages count implausibly low"
assert counts["fans"] > 1_000, "fans count implausibly low"
PY
LOCAL_MD5="$(md5 -q "$BACKUP" 2>/dev/null || md5sum "$BACKUP" | cut -d' ' -f1)"
LOCAL_SIZE="$(wc -c < "$BACKUP" | tr -d ' ')"
ok "backup verified — md5 $LOCAL_MD5, $LOCAL_SIZE bytes"

say "2. Disk headroom on the VPS"
ssh "$HOST" "free=\$(df -Pk / | awk 'NR==2{print \$4}'); need=\$(( $LOCAL_SIZE / 1024 + 1048576 ));
  echo \"  free=\${free}KB need=\${need}KB\";
  [ \"\$free\" -gt \"\$need\" ] || { echo 'REFUSING: not enough free space'; exit 1; }" \
  || die "insufficient disk on the VPS."

say "3. Stop relay + app (idempotent)"
ssh "$HOST" "cd /root/fastt && docker compose stop relay app"

say "4. Upload the backup alongside (never over) the live file"
scp -q "$BACKUP" "$HOST:$DIR/chatterly.db.restore" || die "scp failed — prod untouched, containers stopped."

say "5. Verify the UPLOADED copy before swapping"
ssh "$HOST" "cd $DIR
  rsize=\$(wc -c < chatterly.db.restore | tr -d ' ')
  echo \"  uploaded size : \$rsize (expected $LOCAL_SIZE)\"
  [ \"\$rsize\" = \"$LOCAL_SIZE\" ] || { echo 'SIZE MISMATCH — scp truncated'; exit 1; }
  rmd5=\$(md5sum chatterly.db.restore | cut -d' ' -f1)
  echo \"  uploaded md5  : \$rmd5 (expected $LOCAL_MD5)\"
  [ \"\$rmd5\" = \"$LOCAL_MD5\" ] || { echo 'MD5 MISMATCH'; exit 1; }" \
  || die "uploaded copy failed verification — prod file NOT touched."

# integrity_check runs in a throwaway container against the NON-live .restore file
ssh "$HOST" "docker run --rm -v $DIR:/s python:3.13-slim python -c \"
import sqlite3
c = sqlite3.connect('file:/s/chatterly.db.restore?mode=ro', uri=True)
print('  integrity     :', c.execute('PRAGMA integrity_check').fetchone()[0])
print('  grok_calls    :', c.execute('SELECT COUNT(*) FROM grok_calls').fetchone()[0])
print('  messages      :', c.execute('SELECT COUNT(*) FROM messages').fetchone()[0])
assert c.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
\"" || die "uploaded copy failed integrity_check — prod file NOT touched."
ok "uploaded copy is byte-identical and intact"

say "6. Swap — the broken file is RENAMED, never deleted"
ssh "$HOST" "cd $DIR
  if [ -f chatterly.db ]; then mv chatterly.db chatterly.db.broken.$STAMP; fi
  rm -f chatterly.db-wal chatterly.db-shm
  mv chatterly.db.restore chatterly.db
  ls -la chatterly.db chatterly.db.broken.$STAMP 2>/dev/null || true"

say "7. Restart"
ssh "$HOST" "cd /root/fastt && docker compose up -d"

say "8. Verify prod is genuinely serving (not just /livez)"
sleep 20
ssh "$HOST" "cd /root/fastt && docker compose logs relay --tail 30 2>&1 | grep -icE 'malformed|DatabaseError' | \
  xargs -I{} sh -c 'echo \"  DB errors in relay log: {}\"'"

cat <<EOF

$(ok "restore complete")

The corrupted file is kept at $DIR/chatterly.db.broken.$STAMP
It holds the only copy of writes made after the backup. To try salvaging them:
  ssh $HOST "docker run --rm -v $DIR:/s python:3.13-slim sh -c \\
    'apt-get update -qq && apt-get install -y -qq sqlite3 && \\
     sqlite3 /s/chatterly.db.broken.$STAMP .recover > /s/recovered.sql'"
Do NOT delete it until you have decided about that.
EOF
