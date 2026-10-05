#!/usr/bin/env bash
# Join one dedicated platform agent. Supply the token through a private SSM session,
# never Terraform variables, user-data, arguments, Git, or command output.
set -euo pipefail
[[ $(id -u) == 0 && $(uname -s) == Linux && $(uname -m) == x86_64 && $# == 1 ]] || exit 2
python3 - "$1" <<'PY'
import ipaddress, sys
address = ipaddress.ip_address(sys.argv[1])
if address.version != 4 or not address.is_private:
    raise SystemExit('The existing control private IPv4 address is required')
PY
exec 9>/run/railshot-platform-worker.lock
flock -n 9
systemctl is-active --quiet railshot-platform-stop.timer || {
  echo 'Terraform cloud-init must enable the approved stop timer before joining.' >&2; exit 2;
}
config=/etc/rancher/k3s/config.yaml
token=/etc/rancher/k3s/agent-token
[[ -s $token && ! -L $token && $(stat -c '%u:%a' "$token") == 0:600 ]] || {
  echo 'Prepare a root-owned 0600 agent-token through the private management session.' >&2; exit 2;
}
[[ ! -e /etc/systemd/system/k3s.service && ! -e /etc/rancher/k3s/config.yaml.d && ! -L $config ]] || exit 2
[[ ! -e /etc/railshot/node-role || $(cat /etc/railshot/node-role) == platform-worker ]] || exit 2
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
cat > "$tmp/config.yaml" <<YAML
server: https://$1:6443
token-file: /etc/rancher/k3s/agent-token
node-name: railshot-platform-worker-aws-01
node-label:
  - railshot.io/node-role=platform-worker
lb-server-port: 6443
YAML
if [[ -e $config ]]; then
  cmp -s "$tmp/config.yaml" "$config" || { echo 'Existing agent configuration differs.' >&2; exit 2; }
elif [[ -e /var/lib/rancher/k3s || -e /etc/systemd/system/k3s-agent.service ]] || command -v k3s >/dev/null; then
  echo 'Refusing an existing unmanaged Kubernetes installation.' >&2; exit 2
fi
if command -v k3s >/dev/null; then
  [[ $(k3s --version | awk 'NR==1 {print $3}') == v1.34.11+k3s1 ]] || exit 2
fi
install -d -m 0755 /etc/rancher/k3s /etc/railshot
install -m 0600 "$tmp/config.yaml" "$config"
printf 'platform-worker\n' > /etc/railshot/node-role
sysctl -w net.ipv4.ip_forward=1
printf 'net.ipv4.ip_forward = 1\n' > /etc/sysctl.d/90-railshot-deployment.conf
if [[ ! -e /etc/systemd/system/k3s-agent.service ]]; then
  curl -fsSL --proto '=https' --tlsv1.2 --max-time 180 https://get.k3s.io -o "$tmp/install.sh"
  echo "e5cc3b3d9dfc1662c2d9be6da5abc9a4cd317d6abc3a5ffc02e3dd3248207fee  $tmp/install.sh" | sha256sum -c -
  INSTALL_K3S_VERSION=v1.34.11+k3s1 INSTALL_K3S_EXEC=agent sh "$tmp/install.sh"
fi
systemctl enable --now k3s-agent
systemctl is-active k3s-agent
