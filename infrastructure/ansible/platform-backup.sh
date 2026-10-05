#!/usr/bin/env bash
# Install on the control host from a checked-out, reviewed repository.
set -euo pipefail
[[ $(id -u) == 0 && $# == 2 && $1 =~ ^/var/lib/rancher/k3s/storage/pvc-[a-z0-9_-]+$ && $2 =~ ^railshot-platform-recovery-[0-9]{12}$ ]] || exit 2
command -v aws >/dev/null
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
[[ -f $1/state/dashboard.sqlite3 && ! -L $1 ]] || exit 2
install -d -m 0755 /usr/local/lib/railshot
install -m 0700 "$root/deployment/scripts/backup-platform-state.py" "$root/deployment/scripts/backup-platform-state.sh" /usr/local/lib/railshot/
# Match the API Pod's existing uid/gid; remove local-path's world-writable root.
chown 1000:1000 "$1"
chmod 2770 "$1"
cat > /etc/systemd/system/railshot-state-backup.service <<UNIT
[Unit]
Description=Railshot online SQLite and recovery files to private S3
After=network-online.target
[Service]
Type=oneshot
UMask=0077
ExecStart=/usr/local/lib/railshot/backup-platform-state.sh $1 $2
TimeoutStartSec=15min
Nice=10
IOSchedulingClass=idle
UNIT
cat > /etc/systemd/system/railshot-state-backup.timer <<'UNIT'
[Unit]
Description=Hourly Railshot off-node recovery point
[Timer]
OnBootSec=5min
OnUnitActiveSec=1h
RandomizedDelaySec=60
Persistent=true
[Install]
WantedBy=timers.target
UNIT
systemctl daemon-reload
systemctl enable --now railshot-state-backup.timer
systemctl is-active railshot-state-backup.timer
