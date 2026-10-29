#!/bin/sh
# Restore a backup made by backup.sh. Replaces the contents of the target database.
#   restore.sh /backups/db-20261029T020000Z.dump [/backups/media-20261029T020000Z.tar.gz]
# Stop web and worker first so nothing writes while the restore runs.
set -eu

dump=${1:?usage: restore.sh DB_DUMP [MEDIA_ARCHIVE]}
media=${2:-}
pg_restore --clean --if-exists --no-owner --single-transaction --dbname="${PGDATABASE:-commerce}" "$dump"
if [ -n "$media" ]; then
    tar -xzf "$media" -C /media
fi
echo "restored $dump${media:+ and $media}"
