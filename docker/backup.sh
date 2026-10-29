#!/bin/sh
# Database + media backups. Runs in the postgres image (pg_dump, tar, find are included).
#   backup.sh          loop forever: back up, then sleep BACKUP_INTERVAL_SECONDS
#   backup.sh once     one backup, then exit (manual runs and restore drills)
# Files: /backups/db-<UTC time>.dump (pg_dump custom format) and media-<UTC time>.tar.gz.
set -eu

BACKUP_DIR=${BACKUP_DIR:-/backups}
KEEP_DAYS=${BACKUP_KEEP_DAYS:-14}
INTERVAL=${BACKUP_INTERVAL_SECONDS:-86400}

backup_once() {
    stamp=$(date -u +%Y%m%dT%H%M%SZ)
    mkdir -p "$BACKUP_DIR"
    # Write to a temporary name so a crash never leaves a truncated file that looks complete.
    pg_dump --format=custom --no-owner --file="$BACKUP_DIR/db-$stamp.dump.partial"
    mv "$BACKUP_DIR/db-$stamp.dump.partial" "$BACKUP_DIR/db-$stamp.dump"
    if [ -d /media ]; then
        tar -czf "$BACKUP_DIR/media-$stamp.tar.gz.partial" -C /media .
        mv "$BACKUP_DIR/media-$stamp.tar.gz.partial" "$BACKUP_DIR/media-$stamp.tar.gz"
    fi
    find "$BACKUP_DIR" -name '*.partial' -mmin +60 -delete
    find "$BACKUP_DIR" \( -name 'db-*.dump' -o -name 'media-*.tar.gz' \) -mtime +"$KEEP_DAYS" -delete
    echo "backup $stamp complete: $(du -sh "$BACKUP_DIR/db-$stamp.dump" | cut -f1) database"
}

if [ "${1:-loop}" = "once" ]; then
    backup_once
    exit 0
fi
while true; do
    backup_once || echo "backup failed; retrying next interval" >&2
    sleep "$INTERVAL"
done
