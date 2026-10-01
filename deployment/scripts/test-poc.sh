#!/usr/bin/env bash
# shellcheck source-path=SCRIPTDIR
source "$(dirname -- "${BASH_SOURCE[0]}")/common.sh"
[[ $(uname -s) == Linux && $EUID == 0 ]] || die 'Linux 전용 테스트 노드에서 sudo로 실행하세요.'
[[ ${1:-} == --disposable-node ]] || die '장애 주입·K3s 재시작·클러스터 삭제를 수행합니다. 전용 노드에서 --disposable-node로 실행하세요.'
command -v python3 >/dev/null || die '부하 테스트에 python3가 필요합니다.'
exec 8>/run/jasmin-poc-test.lock
flock -n 8 || die '다른 test-poc.sh가 실행 중입니다.'
RESULT_DIR=${RESULT_DIR:-/var/tmp/jasmin-poc-test-$(date -u +%Y%m%dT%H%M%SZ)}
mkdir -p "$RESULT_DIR"
export WAIT_TIMEOUT=${TEST_WAIT_TIMEOUT:-180s}
[[ $WAIT_TIMEOUT =~ ^[1-9][0-9]*s$ ]] || die 'TEST_WAIT_TIMEOUT 형식 오류'
printf 'status\ttest\tduration_seconds\tdetail\n' > "$RESULT_DIR/results.tsv"
pass_count=0
case_started=$SECONDS
dirty=0
cilium_original=''
cilium_absent=0
probe_pid=''
restore() {
  if (( cilium_absent )); then bash "$ROOT_DIR/scripts/install-cilium.sh" > "$RESULT_DIR/emergency-cilium-restore.log" 2>&1 || return 1; fi
  if [[ -n $cilium_original ]]; then
    kubectl -n kube-system set image ds/cilium "cilium-agent=$cilium_original" >/dev/null || return 1
    kubectl -n kube-system rollout status ds/cilium --timeout="$WAIT_TIMEOUT" >/dev/null || return 1
  fi
  if (( dirty )); then
    kubectl -n jasmin-poc delete networkpolicy poc-test-sample-ingress poc-test-client-egress --ignore-not-found >/dev/null || return 1
    kubectl -n jasmin-poc delete pod poc-allowed poc-denied --ignore-not-found --wait=false >/dev/null || return 1
    bash "$ROOT_DIR/scripts/deploy-sample.sh" > "$RESULT_DIR/emergency-sample-restore.log" 2>&1 || return 1
  fi
}
finish() {
  local code=$?
  trap - EXIT ERR
  if [[ -n $probe_pid ]] && kill -0 "$probe_pid" 2>/dev/null; then
    kill "$probe_pid" 2>/dev/null || true
    wait "$probe_pid" 2>/dev/null || true
  fi
  if (( code != 0 )); then
    printf '[FAIL] %s (exit=%s)\n' "$STEP" "$code" >&2
    printf 'FAIL\t%s\t%s\texit=%s\n' "$STEP" "$((SECONDS-case_started))" "$code" >> "$RESULT_DIR/results.tsv"
    diagnostics > "$RESULT_DIR/failure-state.log" 2>&1 || true
  fi
  if ! restore; then
    printf '[FAIL] Restoration; inspect %s\n' "$RESULT_DIR" >&2
    printf 'FAIL\tRestoration\t0\tmanual recovery required\n' >> "$RESULT_DIR/results.tsv"
    code=1
  fi
  printf '[RESULT] passes=%s exit=%s evidence=%s\n' "$pass_count" "$code" "$RESULT_DIR"
  exit "$code"
}
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
begin() { STEP=$1; case_started=$SECONDS; printf '[RUN] %s\n' "$STEP"; }
pass() {
  printf '[PASS] %s %s\n' "$STEP" "${1:-}"
  printf 'PASS\t%s\t%s\t%s\n' "$STEP" "$((SECONDS-case_started))" "${1:-}" >> "$RESULT_DIR/results.tsv"
  pass_count=$((pass_count+1))
}
run_install() { bash "$ROOT_DIR/install.sh" > "$RESULT_DIR/$1.log" 2>&1; }
verify() { bash "$ROOT_DIR/scripts/verify.sh" > "$RESULT_DIR/$1.log" 2>&1; }
baseline() { bash "$ROOT_DIR/scripts/deploy-sample.sh" > "$RESULT_DIR/restore-$1.log" 2>&1; }
expect_verify_failure() {
  local name=$1 pattern=$2 code
  if WAIT_TIMEOUT=20s bash "$ROOT_DIR/scripts/verify.sh" > "$RESULT_DIR/$name.log" 2>&1; then
    code=0
  else
    code=$?
  fi
  (( code != 0 )) || die "$name: 실패를 감지하지 못했습니다."
  grep -q 'FAILED: 단계=verify' "$RESULT_DIR/$name.log" || die "$name: 단계 오류 로그 없음"
  grep -Eq "$pattern" "$RESULT_DIR/$name.log" || die "$name: 원인 로그 없음 ($pattern)"
}
begin 'Clean install'
if [[ ! -x /usr/local/bin/k3s && ! -e /var/lib/rancher/k3s ]]; then
  printf 'CLEAN_NODE_CONFIRMED\n' > "$RESULT_DIR/clean-state.txt"
  run_install clean-install
  pass 'fresh node confirmed'
else
  printf '[SKIP] Clean install: existing K3s; cleanup/reinstall is tested later\n'
  printf 'SKIP\tClean install\t0\texisting K3s\n' >> "$RESULT_DIR/results.tsv"
  run_install existing-bootstrap
fi
begin 'K3s ready'
kubectl wait --for=condition=Ready nodes --all --timeout="$WAIT_TIMEOUT" > "$RESULT_DIR/node-ready.log"
pass
begin 'Cilium healthy'
/usr/local/lib/jasmin-poc/cilium status --wait --wait-duration "$WAIT_TIMEOUT" > "$RESULT_DIR/cilium-status.log"
pass
begin 'Workload deployed / Service reachable / DNS'
verify networking
pass 'Pod DNS + ClusterIP + node HTTP 200/body'
node_ip=$(kubectl get nodes -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalIP")].address}')
endpoint="http://$node_ip:30080/"
dirty=1
begin 'Idempotency (two repeats)'
before_uid=$(kubectl -n jasmin-poc get deployment jasmin-sample -o jsonpath='{.metadata.uid}')
before_pod=$(kubectl -n jasmin-poc get pods -l app=jasmin-sample -o jsonpath='{.items[0].metadata.uid}')
run_install repeat-1
run_install repeat-2
after_uid=$(kubectl -n jasmin-poc get deployment jasmin-sample -o jsonpath='{.metadata.uid}')
after_pod=$(kubectl -n jasmin-poc get pods -l app=jasmin-sample -o jsonpath='{.items[0].metadata.uid}')
[[ $before_uid == "$after_uid" && $before_pod == "$after_pod" ]] || die '재실행이 Deployment/Pod를 불필요하게 교체했습니다.'
pass 'Deployment and Pod UIDs preserved'
begin 'Pod recovery'
kubectl -n jasmin-poc delete pod -l app=jasmin-sample --grace-period=0 --force > "$RESULT_DIR/pod-delete.log" 2>&1
verify pod-recovery
after_pod=$(kubectl -n jasmin-poc get pods -l app=jasmin-sample -o jsonpath='{.items[0].metadata.uid}')
[[ $before_pod != "$after_pod" ]] || die '새 Pod UID를 확인할 수 없습니다.'
pass 'new Pod UID and endpoint restored'
begin 'Rollout restart under HTTP traffic'
python3 "$ROOT_DIR/scripts/load-http.py" "$endpoint" --duration 20 --concurrency 4 > "$RESULT_DIR/restart-traffic.json" &
probe_pid=$!
sleep 2
kubectl -n jasmin-poc rollout restart deployment/jasmin-sample > "$RESULT_DIR/rollout-restart.log"
verify rollout-restart
wait "$probe_pid"
pass 'zero failed HTTP requests during restart'
begin 'Image update under HTTP traffic'
python3 "$ROOT_DIR/scripts/load-http.py" "$endpoint" --duration 25 --concurrency 4 > "$RESULT_DIR/update-traffic.json" &
probe_pid=$!
sleep 2
kubectl -n jasmin-poc set image deployment/jasmin-sample nginx=nginx:1.28.1-alpine > "$RESULT_DIR/image-update.log"
verify image-update
wait "$probe_pid"
kubectl -n jasmin-poc get pods -l app=jasmin-sample -o json > "$RESULT_DIR/updated-pods.json"
pass 'nginx:1.28.1-alpine healthy; zero failed HTTP requests'
baseline image-update
begin 'Malformed image rejection'
kubectl -n jasmin-poc set image deployment/jasmin-sample 'nginx=invalid@@@' > "$RESULT_DIR/invalid-image-apply.log"
expect_verify_failure invalid-image 'InvalidImageName|invalid reference format'
baseline invalid-image
pass 'nonzero exit and InvalidImageName diagnostic'
begin 'Missing image tag rejection'
kubectl -n jasmin-poc set image deployment/jasmin-sample nginx=nginx:jasmin-poc-tag-does-not-exist > "$RESULT_DIR/missing-tag-apply.log"
expect_verify_failure missing-tag 'ErrImagePull|ImagePullBackOff|not found'
baseline missing-tag
pass 'nonzero exit and image-pull diagnostic'
begin 'Wrong Service port rejection'
kubectl -n jasmin-poc patch service jasmin-sample --type=merge -p '{"spec":{"ports":[{"name":"http","port":80,"targetPort":81,"nodePort":30080}]}}' > "$RESULT_DIR/wrong-port-apply.log"
expect_verify_failure wrong-port 'targetPort: 81|80:30080|81/TCP'
baseline wrong-port
pass 'nonzero exit at internal Service check'
begin 'Readiness failure rejection'
kubectl -n jasmin-poc patch deployment jasmin-sample --type=strategic -p '{"spec":{"template":{"spec":{"containers":[{"name":"nginx","readinessProbe":{"httpGet":{"path":"/missing-healthz"}}}]}}}}' > "$RESULT_DIR/readiness-apply.log"
expect_verify_failure readiness-failure 'Readiness probe failed|statuscode: 404'
baseline readiness-failure
pass 'nonzero exit and readiness diagnostic'
begin 'Cilium unhealthy rejection and recovery'
cilium_original=$(kubectl -n kube-system get ds cilium -o jsonpath='{.spec.template.spec.containers[?(@.name=="cilium-agent")].image}')
kubectl -n kube-system set image ds/cilium 'cilium-agent=invalid@@@' > "$RESULT_DIR/cilium-break.log"
expect_verify_failure cilium-unhealthy 'Cilium|cilium'
kubectl -n kube-system set image ds/cilium "cilium-agent=$cilium_original" > "$RESULT_DIR/cilium-restore.log"
kubectl -n kube-system rollout status ds/cilium --timeout="$WAIT_TIMEOUT" >> "$RESULT_DIR/cilium-restore.log"
cilium_original=''
verify cilium-recovered
pass
begin 'Minimal NetworkPolicy allow / deny'
kubectl -n jasmin-poc run poc-allowed --image=nginx:1.28.0-alpine --labels=poc-client=allowed --restart=Never --command -- sleep 900 > "$RESULT_DIR/policy-clients.log"
kubectl -n jasmin-poc run poc-denied --image=nginx:1.28.0-alpine --labels=poc-client=denied --restart=Never --command -- sleep 900 >> "$RESULT_DIR/policy-clients.log"
kubectl -n jasmin-poc wait --for=condition=Ready pod/poc-allowed pod/poc-denied --timeout="$WAIT_TIMEOUT" >> "$RESULT_DIR/policy-clients.log"
# 적용 전 두 클라이언트가 모두 실제 접근 가능한지 확인한다.
for client in poc-allowed poc-denied; do
  kubectl -n jasmin-poc exec "$client" -- wget -T 5 -q -O - http://jasmin-sample.jasmin-poc.svc.cluster.local/ > "$RESULT_DIR/$client-before.log"
  grep -Fxq 'Jasmin Deployment PoC OK' "$RESULT_DIR/$client-before.log"
done
kubectl apply -f "$ROOT_DIR/tests/network-policy.yaml" > "$RESULT_DIR/policy-apply.log"
sleep 5
kubectl -n jasmin-poc exec poc-allowed -- wget -T 5 -q -O - http://jasmin-sample.jasmin-poc.svc.cluster.local/ > "$RESULT_DIR/policy-allowed.log"
grep -Fxq 'Jasmin Deployment PoC OK' "$RESULT_DIR/policy-allowed.log"
if kubectl -n jasmin-poc exec poc-denied -- wget -T 5 -q -O - http://jasmin-sample.jasmin-poc.svc.cluster.local/ > "$RESULT_DIR/policy-denied.log" 2>&1; then
  denied_code=0
else
  denied_code=$?
fi
(( denied_code != 0 )) || die '정책 적용 후 금지 클라이언트가 접근했습니다.'
kubectl -n jasmin-poc exec poc-denied -- nslookup jasmin-sample.jasmin-poc.svc.cluster.local > "$RESULT_DIR/policy-denied-dns.log"
kubectl -n jasmin-poc delete networkpolicy poc-test-sample-ingress poc-test-client-egress > "$RESULT_DIR/policy-delete.log"
kubectl -n jasmin-poc delete pod poc-allowed poc-denied --wait=true --timeout="$WAIT_TIMEOUT" >> "$RESULT_DIR/policy-delete.log"
verify policy-restored
pass 'both allowed before; only approved client allowed after; denied client DNS still works'
begin 'Basic load (100 requests / concurrency 8)'
python3 "$ROOT_DIR/scripts/load-http.py" "$endpoint" --requests 100 --concurrency 8 > "$RESULT_DIR/load.json"
pass 'HTTP 200 and response body on every request'
begin 'K3s service restart / recovery'
python3 "$ROOT_DIR/scripts/load-http.py" "$endpoint" --duration 20 --concurrency 4 --allow-failures > "$RESULT_DIR/k3s-restart-traffic.json" &
probe_pid=$!
systemctl restart k3s
verify k3s-restarted
wait "$probe_pid"
pass 'endpoint recovered; disruption failures recorded separately'
begin 'Cilium absent rejection / reinstall'
cilium_absent=1
/usr/local/lib/jasmin-poc/cilium uninstall --wait --timeout "$WAIT_TIMEOUT" > "$RESULT_DIR/cilium-uninstall.log" 2>&1
expect_verify_failure cilium-absent 'not found|not installed|Cilium|cilium'
bash "$ROOT_DIR/scripts/install-cilium.sh" > "$RESULT_DIR/cilium-reinstall.log" 2>&1
cilium_absent=0
verify cilium-reinstalled
pass
begin 'Sample cleanup / redeployment'
bash "$ROOT_DIR/scripts/cleanup.sh" --sample > "$RESULT_DIR/sample-cleanup.log" 2>&1
[[ -z $(kubectl get namespace jasmin-poc --ignore-not-found -o name) ]] || die 'sample namespace가 남았습니다.'
baseline sample-cleanup
verify sample-redeployed
pass
begin 'Full cleanup / reinstall'
dirty=0
bash "$ROOT_DIR/scripts/cleanup.sh" --all --disposable-node > "$RESULT_DIR/full-cleanup.log" 2>&1
[[ ! -e /usr/local/bin/k3s && ! -e /var/lib/rancher/k3s && ! -e /etc/rancher/k3s/config.yaml ]] || die 'K3s 제거 후 잔여 핵심 파일이 있습니다.'
run_install reinstall
verify reinstalled
pass 'official uninstall followed by successful installation'
kubectl get nodes -o wide > "$RESULT_DIR/final-state.log"
kubectl -n jasmin-poc get deployment,pods,service,endpointslices -o wide >> "$RESULT_DIR/final-state.log"
dirty=0
