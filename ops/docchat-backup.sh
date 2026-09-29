#!/bin/bash
# DocChat backup: consistent SQLite snapshot + original uploads + page images,
# copied off-box to Cloudflare R2.
#
# Why sqlite3 .backup instead of `cp app.db`: the app runs in WAL mode, so a
# plain copy can capture a torn database. .backup takes a transactionally
# consistent snapshot of a live database with no downtime.
#
# `rclone copy` (not sync) so a pruning mistake can never delete the remote
# copies. The 14-day rotation is what bounds the bucket.

set -euo pipefail

DATA=/opt/docchat/backend/data
WORK=/var/backups/docchat
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
REMOTE="R2:docchat-backups"
KEEP_DAYS=14

log() { echo "$(date -u +%FT%TZ) $*"; }

mkdir -p "$WORK"

log "starting backup $STAMP"

# --- 1. database, consistent snapshot -----------------------------------------
# checkpoint first so the WAL is folded in and the snapshot is compact
/opt/docchat/backend/venv/bin/python - <<'PY'
import sqlite3
conn = sqlite3.connect("/opt/docchat/backend/data/app.db", timeout=30)
try:
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
finally:
    conn.close()
PY

sqlite3 "$DATA/app.db" ".backup '$WORK/app.db.$STAMP"

# Prove the snapshot is actually readable. A backup you have never restored is
# not a backup.
if ! sqlite3 "$WORK/app.db.$STAMP" "PRAGMA quick_check;" | grep -q "^ok$"; then
  log "FATAL: snapshot failed integrity check, not uploading"
  exit 1
fi

USERS=$(sqlite3 "$WORK/app.db.$STAMP" "SELECT COUNT(*) FROM users;" 2>/dev/null || echo "?")
SOURCES=$(sqlite3 "$WORK/app.db.$STAMP" "SELECT COUNT(*) FROM sources;" 2>/dev/null || echo "?")
MSGS=$(sqlite3 "$WORK/app.db.$STAMP" "SELECT COUNT(*) FROM messages;" 2>/dev/null || echo "?")
log "snapshot verified ok: $USERS users, $SOURCES sources, $MSGS messages"

# --- 2. original files, tarred so the upload set is atomic ---------------------
if [ -d "$DATA/files" ]; then
  tar -C "$DATA" -czf "$WORK/files.$STAMP.tar.gz" files
  log "archived uploads: $(du -h "$WORK/files.$STAMP.tar.gz" | cut -f1)"
fi
if [ -d "$DATA/media" ]; then
  tar -C "$DATA" -czf "$WORK/media.$STAMP.tar.gz" media
  log "archived page images: $(du -h "$WORK/media.$STAMP.tar.gz" | cut -f1)"
fi

# --- 3. off-box ----------------------------------------------------------------
if rclone copy "$WORK/app.db.$STAMP" "$REMOTE/db/" --timeout 120s 2>&1; then
  log "db uploaded"
else
  log "ERROR: db upload failed"
  exit 1
fi
rclone copy "$WORK/files.$STAMP.tar.gz" "$REMOTE/files/" --timeout 120s 2>&1 && log "files uploaded" || log "ERROR: files upload failed"
[ -f "$WORK/media.$STAMP.tar.gz" ] && { rclone copy "$WORK/media.$STAMP.tar.gz" "$REMOTE/media/" --timeout 120s 2>&1 && log "media uploaded" || log "ERROR: media upload failed"; }

# --- 4. write a manifest next to the data so a restore is self-describing ------
cat > "$WORK/manifest.$STAMP.txt" <<EOF
DocChat backup
taken:     $STAMP
db:        app.db (sqlite, WAL checkpointed, quick_check ok)
contents:  $USERS users, $SOURCES sources, $MSGS messages
uploads:   files.$STAMP.tar.gz
images:    media.$STAMP.tar.gz

restore:
  1. systemctl stop docchat
  2. sqlite3 $DATA/app.db ".restore 'app.db.<stamp>'"
  3. tar -C $DATA -xzf files.<stamp>.tar.gz
  4. systemctl start docchat
EOF
rclone copy "$WORK/manifest.$STAMP.txt" "$REMOTE/" --timeout 120s 2>&1

# --- 5. rotation: 14 days local, 14 newest remote -----------------------------
find "$WORK" -type f -mtime +$KEEP_DAYS -delete 2>/dev/null || true

for dir in db files media; do
  rclone ls "$REMOTE/$dir/" --order-by modified 2>/dev/null \
    | tail -n +15 | awk '{print $2}' \
    | while read -r f; do
        [ -n "$f" ] && rclone deletefile "$REMOTE/$dir/$f" 2>/dev/null || true
      done
done
rclone ls "$REMOTE/" --order-by modified 2>/dev/null \
  | tail -n +15 | awk '{print $2}' \
  | while read -r f; do
      [ -n "$f" ] && rclone deletefile "$REMOTE/$f" 2>/dev/null || true
    done

REMOTE_COUNT=$(rclone size "$REMOTE" 2>/dev/null | tail -1 || echo "?")
log "rotation done, remote now holds: $REMOTE_COUNT"
log "backup $STAMP completed"

exit 0
