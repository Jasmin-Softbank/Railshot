#!/usr/bin/env bash
# shellcheck source-path=SCRIPTDIR
source "$(dirname -- "${BASH_SOURCE[0]}")/../scripts/common.sh"
TEMP_DIR=$(mktemp -d)
case $(uname -m) in x86_64) arch=amd64 ;; aarch64) arch=arm64 ;; *) die '지원하지 않는 CPU' ;; esac
# 전역 cilium 바이너리를 덮어쓰지 않고 PoC 전용 경로에 checksum 검증 후 설치.
CILIUM=/usr/local/lib/railshot-deployment/cilium
if [[ ! -x $CILIUM ]] || [[ $("$CILIUM" version --client | awk '/cilium-cli:/ {print $2}') != "$CILIUM_CLI_VERSION" ]]; then
  archive="cilium-linux-$arch.tar.gz"
  url="https://github.com/cilium/cilium-cli/releases/download/$CILIUM_CLI_VERSION/$archive"
  download "$url" "$TEMP_DIR/$archive"
  download "$url.sha256sum" "$TEMP_DIR/$archive.sha256sum"
  (cd "$TEMP_DIR" && sha256sum --check "$archive.sha256sum")
  tar -xzf "$TEMP_DIR/$archive" -C "$TEMP_DIR" cilium
  install -d -m 755 /usr/local/lib/railshot-deployment
  install -m 755 "$TEMP_DIR/cilium" "$CILIUM"
fi
options=(--version "$CILIUM_VERSION" --namespace kube-system
  --set kubeProxyReplacement=false
  --set ipam.mode=cluster-pool
  --set 'ipam.operator.clusterPoolIPv4PodCIDRList=10.42.0.0/16'
  --set operator.replicas=1
  --set routingMode=tunnel --set tunnelProtocol=vxlan
  --set hubble.enabled=false)
# CLI 설치는 Helm release를 만든다. API 조회 실패를 '미설치'로 오인하지 않는다.
release=$(kubectl -n kube-system get secrets -l owner=helm,name=cilium -o name)
if [[ -n $release ]]; then
  log "Cilium Helm release 재조정: $CILIUM_VERSION"
  "$CILIUM" upgrade "${options[@]}"
else
  log "공식 Cilium CLI로 설치: $CILIUM_VERSION"
  "$CILIUM" install "${options[@]}"
fi
"$CILIUM" status --wait --wait-duration "$WAIT_TIMEOUT"
kubectl wait --for=condition=Ready nodes --all --timeout="$WAIT_TIMEOUT"
nodes=$(kubectl get nodes -o name)
[[ $(printf '%s\n' "$nodes" | wc -l) -eq 1 ]] || die '단일 노드만 지원합니다.'
kubectl -n kube-system rollout status deployment/coredns --timeout="$WAIT_TIMEOUT"
kubectl get nodes -o wide
kubectl -n kube-system get pods -l k8s-app=cilium -o wide
