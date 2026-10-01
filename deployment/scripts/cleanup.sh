#!/usr/bin/env bash
# shellcheck source-path=SCRIPTDIR
source "$(dirname -- "${BASH_SOURCE[0]}")/common.sh"
[[ $(uname -s) == Linux && $EUID == 0 ]] || die '전용 Linux 노드에서 sudo로 실행하세요.'
case ${1:---sample} in
  --sample)
    kubectl delete namespace jasmin-poc --ignore-not-found --wait=true --timeout="$WAIT_TIMEOUT"
    ;;
  --all)
    [[ ${2:-} == --disposable-node ]] || die '전체 삭제는 --all --disposable-node로 명시하세요. 클러스터 데이터가 삭제됩니다.'
    config=/etc/rancher/k3s/config.yaml
    if [[ ! -f $config ]] || ! grep -Fxq '# Managed by Jasmin deployment-poc. Dedicated single-node server only.' "$config"; then
      die 'Jasmin PoC가 생성한 K3s 설정이 아닙니다.'
    fi
    [[ -x /usr/local/bin/k3s-uninstall.sh ]] || die '공식 K3s uninstall 스크립트가 없습니다.'
    if [[ -x /usr/local/lib/jasmin-poc/cilium ]]; then
      /usr/local/lib/jasmin-poc/cilium uninstall --wait --timeout "$WAIT_TIMEOUT"
    fi
    for link in cilium_host cilium_net cilium_vxlan; do
      if ip link show "$link" >/dev/null 2>&1; then ip link delete "$link"; fi
    done
    /usr/local/bin/k3s-uninstall.sh
    # PoC 전용 CLI와 설정만 제거한다. 마운트된 BPF 파일을 직접 삭제하지 않는다.
    [[ ! -e /usr/local/lib/jasmin-poc/cilium ]] || unlink /usr/local/lib/jasmin-poc/cilium
    [[ ! -e /etc/sysctl.d/90-jasmin-poc.conf ]] || unlink /etc/sysctl.d/90-jasmin-poc.conf
    ;;
  *) die '사용법: cleanup.sh --sample | --all --disposable-node' ;;
esac
log 'PASS: cleanup 완료'
