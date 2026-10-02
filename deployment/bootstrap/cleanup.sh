#!/usr/bin/env bash
# shellcheck source-path=SCRIPTDIR
source "$(dirname -- "${BASH_SOURCE[0]}")/../scripts/common.sh"
[[ ${1:-} == --disposable-node ]] || die '전체 제거에는 --disposable-node가 필요합니다.'
config=/etc/rancher/k3s/config.yaml
if [[ ! -e $config && ! -e /usr/local/bin/k3s && ! -e /var/lib/rancher/k3s ]]; then
  log 'K3s is already absent'
  exit 0
fi
if [[ ! -f $config ]] || ! grep -Fxq '# Managed by Railshot deployment runtime. Dedicated single-node server only.' "$config"; then
  die 'Railshot runtime이 관리하는 K3s가 아닙니다.'
fi
[[ -x /usr/local/bin/k3s-uninstall.sh ]] || die '공식 K3s uninstall 스크립트가 없습니다.'
CILIUM=/usr/local/lib/railshot-deployment/cilium
if [[ -x $CILIUM ]]; then "$CILIUM" uninstall --wait --timeout "$WAIT_TIMEOUT"; fi
for link in cilium_host cilium_net cilium_vxlan; do
  if ip link show "$link" >/dev/null 2>&1; then ip link delete "$link"; fi
done
/usr/local/bin/k3s-uninstall.sh
[[ ! -e $CILIUM ]] || unlink "$CILIUM"
[[ ! -e /etc/sysctl.d/90-railshot-deployment.conf ]] || unlink /etc/sysctl.d/90-railshot-deployment.conf
log 'K3s / Cilium removed'
