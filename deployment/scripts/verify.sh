#!/usr/bin/env bash
# shellcheck source-path=SCRIPTDIR
source "$(dirname -- "${BASH_SOURCE[0]}")/common.sh"
[[ -x /usr/local/lib/jasmin-poc/cilium ]] || die 'Cilium CLI가 없습니다. install-cilium.sh 단계부터 확인하세요.'
/usr/local/lib/jasmin-poc/cilium status --wait --wait-duration "$WAIT_TIMEOUT"
kubectl wait --for=condition=Ready nodes --all --timeout="$WAIT_TIMEOUT"
kubectl -n jasmin-poc rollout status deployment/jasmin-sample --timeout="$WAIT_TIMEOUT"
log 'Pod → DNS → ClusterIP Service → nginx 네트워크 확인'
deadline=$((SECONDS + ${WAIT_TIMEOUT%s}))
until body=$(kubectl -n jasmin-poc exec deployment/jasmin-sample -- \
  wget -T 10 -q -O - http://jasmin-sample.jasmin-poc.svc.cluster.local/ 2>/dev/null) && [[ $body == 'Jasmin Deployment PoC OK' ]]; do
  (( SECONDS < deadline )) || die '클러스터 내부 DNS/Service HTTP 확인 시간 초과'
  sleep 3
done
node_ip=$(kubectl get nodes -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalIP")].address}')
valid_ipv4 "$node_ip" || die '노드 InternalIP를 IPv4로 확인할 수 없습니다.'
port=$(kubectl -n jasmin-poc get service jasmin-sample -o jsonpath='{.spec.ports[0].nodePort}')
endpoint="http://$node_ip:$port/"
TEMP_DIR=$(mktemp -d)
check_http() {
  local url=$1 code
  log "HTTP 확인: $url"
  local deadline=$((SECONDS + ${WAIT_TIMEOUT%s}))
  while true; do
    if code=$(curl --noproxy '*' --silent --show-error --connect-timeout 3 --max-time 10 \
      -o "$TEMP_DIR/body" -w '%{http_code}' "$url") && \
      [[ $code == 200 && $(cat "$TEMP_DIR/body") == 'Jasmin Deployment PoC OK' ]]; then
      log "PASS: HTTP 200 + Jasmin 응답 본문 ($url)"
      break
    fi
    (( SECONDS < deadline )) || die "HTTP 확인 시간 초과: $url (마지막 HTTP=${code:-없음})"
    sleep 3
  done
}
check_http "$endpoint"
if [[ -n ${VERIFY_URL:-} ]]; then
  [[ $VERIFY_URL =~ ^http://[A-Za-z0-9.-]+:[0-9]+/$ ]] || die 'VERIFY_URL은 http://IPv4또는호스트:포트/ 형식이어야 합니다.'
  check_http "$VERIFY_URL"
fi
log "외부 클라이언트에서 실행: curl --noproxy '*' -i $endpoint"
log '이 서버에서의 curl 성공은 외부 방화벽/NAT 통과를 증명하지 않습니다.'
