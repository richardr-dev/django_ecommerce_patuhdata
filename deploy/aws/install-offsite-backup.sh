#!/usr/bin/env bash
# One-time setup on the server, as root, after the CloudFormation stack created the bucket:
#   sudo deploy/aws/install-offsite-backup.sh <StorageBucketName> <AlertTopicArn>
# Both values are outputs of the stack. Installs an hourly systemd timer for offsite-backup.sh.
set -euo pipefail
bucket=${1:?StorageBucketName from the stack outputs}
topic=${2:?AlertTopicArn from the stack outputs}
region=$(echo "$topic" | cut -d: -f4)
here=$(cd "$(dirname "$0")" && pwd)

install -d -m 0755 /etc/lakuin
cat > /etc/lakuin/offsite.env <<ENV
BACKUP_BUCKET=$bucket
ALERT_TOPIC_ARN=$topic
AWS_REGION=$region
ENV
install -m 0755 "$here/offsite-backup.sh" /usr/local/bin/lakuin-offsite-backup

cat > /etc/systemd/system/lakuin-offsite.service <<'UNIT'
[Unit]
Description=Upload the newest Lakuin backup off-site and check disk space
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
ExecStart=/usr/local/bin/lakuin-offsite-backup
UNIT
cat > /etc/systemd/system/lakuin-offsite.timer <<'UNIT'
[Unit]
Description=Hourly Lakuin off-site backup and disk check

[Timer]
OnCalendar=hourly
RandomizedDelaySec=300
Persistent=true

[Install]
WantedBy=timers.target
UNIT
systemctl daemon-reload
systemctl enable --now lakuin-offsite.timer
systemctl start lakuin-offsite.service
journalctl -u lakuin-offsite.service -n 5 --no-pager
