#!/usr/bin/env bash
# Run on the control host: one online snapshot, encrypted off-node, every 30 minutes.
set -euo pipefail
[[ $(id -u) == 0 && $# == 2 && $1 == /* && $2 =~ ^[a-z0-9.-]+$ ]] || exit 2
# A 0700 PVC root makes OnRootMismatch recursively widen private state next mount.
if [[ $(stat -c '%u:%g:%a' -- "$1") != 1000:1000:2770 ]]; then
  printf '{"status":"failed","code":"PVC_ROOT_PERMISSIONS_INVALID"}\n'
  exit 1
fi
umask 077
exec 9>/run/railshot-state-backup.lock
flock -n 9 || exit 0
install -d -m 0700 /var/lib/railshot-backups
work=$(mktemp -d /var/lib/railshot-backups/capture.XXXXXXXX)
trap 'rm -rf -- "$work"' EXIT
python3 /usr/local/lib/railshot/backup-platform-state.py backup --source "$1" --destination "$work/state"
tar -C "$work/state" -czf "$work/state.tar.gz" .
key="platform/$(date -u +%Y%m%dT%H%M%SZ)-$(sha256sum "$work/state.tar.gz" | cut -c1-12).tar.gz"
aws s3 cp "$work/state.tar.gz" "s3://$2/$key" --region ap-northeast-2 --sse AES256 --only-show-errors
printf '{"status":"uploaded","bucket":"%s","key":"%s","bytes":%s}\n' "$2" "$key" "$(stat -c %s "$work/state.tar.gz")"
