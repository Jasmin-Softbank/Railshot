#!/bin/bash
# Run on the dedicated CI VM, after infrastructure/ansible/ci.yml succeeds.
set -euo pipefail
test "$(id -u)" = 0 || { echo 'Run as root on the dedicated CI VM.' >&2; exit 2; }
. /etc/os-release
test "$ID:$VERSION_ID" = ubuntu:24.04 || { echo 'Ubuntu 24.04 is required.' >&2; exit 2; }
for path in /etc/rancher/k3s /var/lib/rancher/k3s /etc/kubernetes /var/lib/kubelet; do
  if [ -e "$path" ] || [ -L "$path" ]; then
    echo "Refusing a Kubernetes host: $path" >&2; exit 2
  fi
done
test -f /etc/railshot/ci-executor.yaml
/usr/local/sbin/railshot-ci-network --check
for path in /var/lib/railshot-runner /var/lib/railshot-runner/work /var/lib/railshot-runner/runs /var/lib/railshot-runner/codex; do
  test ! -L "$path" || { echo "Refusing symlink: $path" >&2; exit 2; }
  install -d -o root -g root -m 0700 "$path"
done
install -d -o root -g root -m 0700 /var/lib/railshot-runner/work/_temp
# These empty host directories let container preflight see any later K3s install.
install -d -o root -g root -m 0755 /etc/rancher /var/lib/rancher
printf '%s\n' dedicated-ci-ubuntu-24.04 > /etc/railshot/ci-runner-host
chown root:root /etc/railshot/ci-runner-host
chmod 0644 /etc/railshot/ci-runner-host
echo 'Dedicated CI host prepared. Supply a fresh registration token file before compose up.'
