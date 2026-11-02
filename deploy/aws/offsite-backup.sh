#!/usr/bin/env bash
# Runs hourly on the server (lakuin-offsite.timer). Uploads the newest database and media
# backup to the off-site S3 bucket once, and emails an alert when the disk is nearly full or
# no new database backup has appeared for over a day.
# Settings come from /etc/lakuin/offsite.env: BACKUP_BUCKET, ALERT_TOPIC_ARN, AWS_REGION.
set -euo pipefail
source /etc/lakuin/offsite.env
DIR=${BACKUP_DIR:-/home/ubuntu/lakuin/backups}
STATE=/var/lib/lakuin
mkdir -p "$STATE"

# The AWS CLI runs in a container on the host network, so it reaches the instance role's
# credentials (IMDSv2) without installing anything on the host.
aws_cli() {
    docker run --rm --network host -e AWS_REGION="$AWS_REGION" -v "$DIR:/backups:ro" \
        amazon/aws-cli:2.17.0 "$@"
}

alert() {
    echo "ALERT: $1" >&2
    aws_cli sns publish --topic-arn "$ALERT_TOPIC_ARN" --subject "Lakuin: $1" \
        --message "$1 on $(hostname) at $(date -u +%FT%TZ)." >/dev/null || true
}

used=$(df --output=pcent / | tail -1 | tr -dc '0-9')
if [ "$used" -ge 85 ]; then
    alert "server disk ${used}% full"
fi

db=$(ls -t "$DIR"/db-*.dump 2>/dev/null | head -1 || true)
if [ -z "$db" ] || [ -n "$(find "$db" -mmin +1560)" ]; then  # 26 hours
    alert "no database backup in the last 26 hours"
    exit 0
fi
name=$(basename "$db")
if [ "$(cat "$STATE/last-upload" 2>/dev/null)" = "$name" ]; then
    exit 0
fi
aws_cli s3 cp --only-show-errors "/backups/$name" "s3://$BACKUP_BUCKET/backups/db/$name"
media="media-${name#db-}"
media="${media%.dump}.tar.gz"
if [ -f "$DIR/$media" ]; then
    aws_cli s3 cp --only-show-errors "/backups/$media" "s3://$BACKUP_BUCKET/backups/media/$media"
fi
echo "$name" > "$STATE/last-upload"
echo "uploaded $name to s3://$BACKUP_BUCKET"
