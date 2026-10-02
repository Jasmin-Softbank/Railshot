#!/usr/bin/env bash
# shellcheck source-path=SCRIPTDIR
source "$(dirname -- "${BASH_SOURCE[0]}")/../scripts/common.sh"
TEMP_DIR=$(mktemp -d)
config=/etc/rancher/k3s/config.yaml
cat > "$TEMP_DIR/config.yaml" <<'YAML'
# Managed by Railshot deployment runtime. Dedicated single-node server only.
flannel-backend: none
disable-network-policy: true
cluster-cidr: 10.42.0.0/16
service-cidr: 10.43.0.0/16
write-kubeconfig-mode: "0600"
disable:
  - traefik
  - servicelb
  - metrics-server
  - local-storage
YAML
if [[ -n ${NODE_IP:-} ]]; then
  printf 'node-ip: "%s"\n' "$NODE_IP" >> "$TEMP_DIR/config.yaml"
fi
if [[ -e $config ]]; then
  cmp -s "$TEMP_DIR/config.yaml" "$config" || die '기존 K3s 설정과 다릅니다. 기존 클러스터/CNI를 자동으로 변경하지 않습니다. 전용 노드를 사용하세요.'
else
  if [[ -e /var/lib/rancher/k3s || -e /etc/systemd/system/k3s.service ]] || command -v k3s >/dev/null; then
    die '관리하지 않는 기존 K3s가 있습니다. 새 전용 노드를 사용하세요.'
  fi
fi
[[ ! -d /etc/rancher/k3s/config.yaml.d ]] || die 'K3s config.yaml.d가 있습니다. 설정 충돌을 피하도록 전용 노드를 사용하세요.'
install -d -m 755 /etc/rancher/k3s
install -m 600 "$TEMP_DIR/config.yaml" "$config"
sysctl -w net.ipv4.ip_forward=1
printf 'net.ipv4.ip_forward = 1\n' > /etc/sysctl.d/90-railshot-deployment.conf
if [[ -x /usr/local/bin/k3s ]]; then
  actual=$(/usr/local/bin/k3s --version | awk 'NR==1 {print $3}')
  [[ $actual == "$K3S_VERSION" ]] || die "기존 K3s=$actual, 요청=$K3S_VERSION. 자동 업그레이드는 하지 않습니다."
fi
if [[ ! -x /usr/local/bin/k3s || ! -e /etc/systemd/system/k3s.service ]]; then
  log "공식 installer로 K3s $K3S_VERSION 설치 (kube-proxy 유지)"
  download https://get.k3s.io "$TEMP_DIR/k3s-install.sh"
  INSTALL_K3S_VERSION="$K3S_VERSION" INSTALL_K3S_EXEC=server sh "$TEMP_DIR/k3s-install.sh"
else
  log '동일 설정/버전의 K3s 재사용'
fi
systemctl enable --now k3s
log 'Kubernetes API 준비 대기. Node Ready는 Cilium 설치 후 확인합니다.'
deadline=$((SECONDS + ${WAIT_TIMEOUT%s}))
until [[ -s $KUBECONFIG ]] && kubectl get --raw=/readyz >/dev/null 2>&1; do
  (( SECONDS < deadline )) || die 'Kubernetes API 준비 시간 초과'
  sleep 3
done
kubectl get nodes -o wide
