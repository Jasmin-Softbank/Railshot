# 통합 runtime 연결

Ansible 통합은 `runtime.sh`에서 기존 승민 preflight와 K3s/Cilium 두 단계만 실행한다. 기본 인터페이스는 [Ansible 명세](../docs/api/ansible.md)의 `runtime.install`이다. 샘플 앱·CD·Patroni·공개 URL 검증은 이 명령에 포함하지 않는다. `install.sh`는 아래 원본 PoC의 샘플 배포까지 포함하는 별도 진입점으로 보존한다.

---

# Jasmin Deployment 최소 PoC

이미 provisioning이 끝난 Linux 서버 하나에서 `install.sh → K3s → Cilium → nginx Deployment + Service → curl HTTP 200`을 확인합니다. Provider SDK, Terraform, cloudflared, Ingress, CNPG, Sealed Secrets, HA, 스토리지, autoscaling, observability stack은 포함하지 않습니다.

## 지원 대상과 전제조건

- 전용 Ubuntu 22.04/24.04 또는 Debian 12/13 서버/VM, systemd, amd64/arm64, 커널 5.10 이상 및 배포판 기본 eBPF 지원. 이 OS 목록은 구현의 지원 범위이며 실제 OS별 실행 검증 결과를 뜻하지 않습니다.
- root 권한, swap 비활성화. PoC 권장 사양: 2 vCPU, RAM 4 GiB 이상, 디스크 여유 10 GiB 이상.
- `bash`, `curl`, `tar`, `sha256sum`, `systemctl`, `ip`, `flock`, `sysctl`, `cmp`, `awk`, `grep` 등 기본 도구. Debian/Ubuntu 최소 이미지에서 부족하면 `sudo apt-get update && sudo apt-get install -y curl ca-certificates iproute2 util-linux procps`.
- 기존 Kubernetes/K3s/CNI가 없는 전용 노드. 기존 Flannel 클러스터를 Cilium으로 변환하는 도구가 아닙니다. NIC 주소가 Pod CIDR `10.42.0.0/16`, Service CIDR `10.43.0.0/16`과 겹치지 않아야 합니다.
- 인터넷 DNS 및 HTTPS 다운로드: get.k3s.io, GitHub/raw.githubusercontent.com, helm.cilium.io, quay.io, registry.k8s.io, Docker Hub와 해당 CDN/이미지 레지스트리 경로. 프록시 환경은 별도 설정이 필요합니다.
- 서버의 NodePort **TCP 30080**을 검증 클라이언트에서 접근할 수 있도록 호스트 방화벽/보안 그룹/NAT를 구성합니다. 외부에서 API 6443을 열 필요는 없습니다. 호스트 방화벽이 Pod/Service 트래픽을 차단하면 먼저 해결합니다. 스크립트는 방화벽을 자동 변경하지 않습니다.
- 일반 Linux VM/물리 서버용입니다. macOS, Docker 컨테이너, 권한이 제한된 LXC는 지원 대상이 아닙니다.

## 실행

서버로 `deployment-poc` 디렉터리를 복사한 뒤 실행합니다. 스크립트는 현재 셸의 위치와 관계없이 manifest(배포 설정 파일)를 찾습니다.

```bash
cd deployment-poc
sudo ./install.sh
```

버전을 기본값으로 고정해 재실행 때 K3s가 자동으로 최신 버전으로 바뀌지 않도록 했습니다.

| 입력 | 기본값 | 용도 |
| --- | --- | --- |
| `K3S_VERSION` | `v1.34.11+k3s1` | 최초 설치 버전, 기존 버전이 다르면 중단 |
| `CILIUM_VERSION` | `1.20.2` | Cilium Helm chart 버전 |
| `CILIUM_CLI_VERSION` | `v0.20.1` | 공식 CLI 버전, checksum 확인 |
| `NODE_IP` | K3s 자동 선택 | 다중 NIC인 경우 서버 NIC의 IPv4 지정 |
| `WAIT_TIMEOUT` | `300s` | 각 준비/검증 단계 대기 시간 |
| `VERIFY_URL` | 없음 | 서버에서 추가 확인할 `http://호스트:포트/` URL |

```bash
sudo env NODE_IP=192.168.10.20 WAIT_TIMEOUT=600s ./install.sh
# NAT/공인 IP가 있는 경우 선택적으로 서버에서 해당 주소도 확인
sudo env NODE_IP=192.168.10.20 VERIFY_URL=http://203.0.113.10:30080/ ./install.sh
```

예시 IP는 실제 서버 주소로 바꿉니다. 공인 NAT 주소는 `NODE_IP`에 넣지 않습니다. `VERIFY_URL`은 hairpin NAT 미지원 환경에서 서버로부터 접근이 실패할 수 있습니다. 그 경우 별도 클라이언트에서 확인합니다. kubeconfig는 `/etc/rancher/k3s/k3s.yaml`, 권한은 `0600`입니다.

## 내부 순서와 재실행

1. `install.sh`: 운영체제·관리자 권한·커널·서비스 관리자·swap·입력을 확인하고 `flock`으로 동시 실행을 방지합니다.
2. `scripts/install-k3s.sh`: 관리 설정을 `/etc/rancher/k3s/config.yaml`에 기록하고 공식 설치 도구로 K3s 서버를 설치합니다. `flannel-backend: none`, `disable-network-policy: true`. kube-proxy는 유지합니다. Traefik, ServiceLB, metrics-server, local-storage는 제외합니다. IPv4 forwarding을 활성화하고 `/etc/sysctl.d/90-jasmin-poc.conf`에 유지합니다. Cilium이 없으면 Node NotReady는 정상일 수 있으므로 API `/readyz`까지만 먼저 기다립니다.
3. `scripts/install-cilium.sh`: 공식 CLI를 `/usr/local/lib/jasmin-poc/cilium`에 설치합니다. CLI 내부의 Helm 방식으로 설치하거나 업데이트합니다. Pod IPAM CIDR을 K3s와 동일하게 맞추고 operator를 1개로 구성합니다. kube-proxy replacement는 비활성화합니다. Cilium status, Node Ready, CoreDNS rollout을 확인합니다.
4. `scripts/deploy-sample.sh`: Namespace, HTML ConfigMap, nginx Deployment, NodePort Service를 `kubectl apply`하고 readiness/rollout을 기다립니다.
5. `scripts/verify.sh`: Cilium/Node/Deployment 준비를 다시 확인한 뒤, nginx Pod에서 Service DNS 이름으로 HTTP 요청해 DNS + ClusterIP 경로를 확인합니다. 서버에서는 `curl`로 `InternalIP:30080`의 **HTTP 200과 `Jasmin Deployment PoC OK` 본문**을 함께 확인합니다.

같은 설정과 버전으로 재실행하면 K3s를 재설치하지 않고 중단된 서비스를 다시 시작합니다. Cilium은 동일한 선언 값을 재적용하고 workload는 적용합니다. 최초 설치 중 실패한 경우에도 로그에 표시된 원인을 해결한 뒤 재실행하실 수 있습니다. 다른 K3s 설정/버전 및 config drop-in은 자동 병합하지 않습니다. `NODE_IP`을 최초 지정했다면 재실행 때도 동일하게 전달합니다. 설정 변경이나 클러스터/CNI 버전 업그레이드는 별도 검토 후 수행합니다. 실패 시 자동 삭제하지 않아 상태와 로그가 남습니다.

## 정상 동작 확인

서버에서 마지막 `SUCCESS`와 각 HTTP `PASS`를 확인합니다.

```bash
sudo k3s kubectl get nodes -o wide
sudo k3s kubectl -n kube-system get pods -l k8s-app=cilium -o wide
sudo env KUBECONFIG=/etc/rancher/k3s/k3s.yaml /usr/local/lib/jasmin-poc/cilium status
sudo k3s kubectl -n jasmin-poc get deployment,pods,service,endpointslices
# 설치 없이 확인만 다시 실행
sudo bash scripts/verify.sh
```

**외부 접근 성공은 다른 PC/서버에서 아래 명령을 실행하여 별도로 확인합니다.** 공인 주소가 없으면 같은 LAN/VPN에서 접근 가능한 서버 IP를 사용하시면 됩니다. NodePort는 도메인/TLS 없이 외부 접근할 수 있는 첫 단계 노출 방식입니다.

```bash
curl --noproxy '*' --connect-timeout 5 --max-time 10 -i http://SERVER_REACHABLE_IP:30080/
```

기대 결과: `HTTP/1.1 200 OK`, 본문 `Jasmin Deployment PoC OK`. 서버 내부 curl만 성공했다면 외부 접근까지 검증된 것은 아닙니다. Kubernetes API의 port-forward는 NodePort 검증을 대체하지 않습니다.

추가 Cilium 네트워크 진단은 선택 사항입니다. 아래 테스트는 별도 namespace/pod를 만들며 외부 인터넷 테스트도 포함하므로 첫 성공 경로의 필수 단계로 넣지 않았습니다.

```bash
sudo env KUBECONFIG=/etc/rancher/k3s/k3s.yaml /usr/local/lib/jasmin-poc/cilium connectivity test --single-node
```

## 실패 포인트와 진단

| 단계 | 주된 원인 / 확인 |
| --- | --- |
| preflight | OS/kernel/swap/root/systemd/도구 부족, 잘못된 NODE_IP, 동시 실행 |
| install-k3s | 기존 설정 충돌, 디스크 부족, installer 다운로드 실패, API 준비 시간 초과; `journalctl -u k3s -n 100 --no-pager` |
| install-cilium | CLI checksum/chart/이미지 다운로드 실패, eBPF/kernel 기능 부족, CIDR 충돌, 방화벽, operator/CNI 준비 실패; `cilium status`, Cilium pod describe/logs |
| deploy-sample | nginx ImagePullBackOff, 자원 부족, NodePort 30080 충돌, readiness 실패; pod describe 및 events |
| verify | CoreDNS/Service 경로 실패, iptables/forwarding/방화벽 문제, HTTP 200이지만 다른 앱 응답, 외부 URL의 NAT 문제 |

```bash
sudo journalctl -u k3s -n 100 --no-pager
sudo k3s kubectl get pods -A -o wide
sudo k3s kubectl get events -A --sort-by=.lastTimestamp
sudo k3s kubectl -n kube-system logs -l k8s-app=cilium --tail=100
sudo k3s kubectl -n jasmin-poc describe pods
```

스크립트는 UTC 시각/단계명과 실패한 단계의 exit code를 출력하고 0이 아닌 코드로 종료합니다. 명령 오류는 line도 출력합니다. 실패 시점에 따라 일부 단계는 이미 적용되어 있을 수 있습니다. 실패 원인을 해결한 후 같은 입력으로 다시 실행합니다.

설치 프로세스가 강제 종료되어 Helm release가 `pending-install`/`pending-upgrade`에 남았다면 자동 재실행만으로 복구되지 않을 수 있습니다. 이 전용 PoC에서는 아래 Cilium uninstall 절차로 해당 release를 제거한 뒤 `install.sh`를 다시 실행합니다. 운영 클러스터의 Helm 복구/rollback은 이 PoC에 포함하지 않습니다.

## 제거 / cleanup

자동화된 제거는 다음과 같이 실행합니다. `--all`은 데이터가 삭제되는 전용 테스트 노드용입니다.

```bash
sudo bash scripts/cleanup.sh --sample
sudo bash scripts/cleanup.sh --all --disposable-node
```

샘플만 삭제할 때:

```bash
sudo k3s kubectl delete namespace jasmin-poc --ignore-not-found
# connectivity test를 실행했다면
sudo k3s kubectl delete namespace cilium-test-1 --ignore-not-found
```

전체 PoC 제거는 **전용 테스트 노드에서만** 수행합니다. K3s uninstall은 해당 노드의 클러스터 데이터와 workload를 모두 삭제합니다. K3s 제거 전에 Cilium부터 제거합니다.

```bash
sudo env KUBECONFIG=/etc/rancher/k3s/k3s.yaml /usr/local/lib/jasmin-poc/cilium uninstall --wait
# Cilium 링크가 남아 있으면 K3s uninstall 전에 제거
for link in cilium_host cilium_net cilium_vxlan; do
  if ip link show "$link" >/dev/null 2>&1; then sudo ip link delete "$link"; fi
done
sudo /usr/local/bin/k3s-uninstall.sh
sudo rm -rf /usr/local/lib/jasmin-poc
sudo rm -f /etc/sysctl.d/90-jasmin-poc.conf
```

forwarding의 현재 런타임 값은 자동으로 이전 값으로 복원하지 않습니다. 커널 BPF/network 잔여 상태까지 초기화하려면 제거 후 테스트 VM을 재부팅하거나 새 VM으로 재검증합니다. 가장 확실한 초기화는 전용 VM을 다시 만드는 것입니다. 실패한 첫 설치에서 uninstall 스크립트조차 생성되지 않았다면, 전용 VM을 재생성합니다.

## Provider Interface / 후속 구성 연결 지점

Provisioning 결과는 `node.address`(SSH 대상), `node.internalIPv4`, `endpoint.host`/포트/방화벽 준비 여부 같은 입력으로 전달하시면 됩니다. Interface 스키마 자체는 아직 확정하지 않습니다. Provider adapter가 노드에 파일을 복사하고 `sudo env NODE_IP=... ./install.sh`를 원격 실행하게 연결합니다. K3s/Cilium/workload 스크립트에는 AWS/OpenStack/Proxmox 호출을 넣지 않습니다. 외부 검증은 adapter 실행 머신에서 curl하고 성공 결과를 수집합니다. CIDR 변경이 필요하면 K3s 설정과 Cilium IPAM을 **함께** 변경해야 하며 기존 클러스터의 제자리 CIDR 변경은 이 PoC 범위가 아닙니다.

- **cloudflared**: 2차 PoC에서 sample 배포 뒤 별도 단계로 추가합니다. Tunnel origin은 `http://jasmin-sample.jasmin-poc.svc.cluster.local:80`, 외부 URL은 Tunnel(외부 접속 통로)이 준비된 뒤 검증합니다. 그때 외부 공개 NodePort 유지 여부를 결정합니다.
- **Sealed Secrets**: Cilium/노드 정상화 뒤, secret이 필요한 workload/cloudflared 배포 전에 controller(비밀정보 처리 기능)를 설치하고 SealedSecret을 적용하는 단계를 추가합니다.
- **CNPG**: Cilium/노드 정상화 뒤 별도 operator(DB 관리 기능)를 설치하는 단계를 추가합니다. DB Cluster/스토리지 준비 및 DB readiness를 확인한 다음 DB에 의존하는 앱을 배포합니다. 현재 storage를 비활성화했으므로 저장소 결정이 선행되어야 합니다.

## 로컬 코드 확인

```bash
for script in install.sh scripts/*.sh; do bash -n "$script" || exit; done
python3 -m unittest discover -s tests -v
```

로컬 테스트는 Kubernetes/Cilium 호출을 모킹(가짜 응답으로 대체)하고 실제 로컬 HTTP 서버를 통해 HTTP 200/본문 검증, 실패 코드와 단계 로그를 확인합니다. Linux 커널, CNI, K3s 설치, 실제 외부 클라이언트 E2E 검증을 대체하지 않습니다.

## 실제 Linux 장애·부하·재실행 테스트

**사용 중인 서버 대신 새 테스트 VM을 사용합니다.** 아래 테스트는 Pod 강제 삭제, 잘못된 이미지/포트/readiness, Cilium 제거·복구, K3s 재시작, 전체 클러스터 삭제·재설치를 실제 수행합니다. DB/사용자 데이터가 있는 노드에서 실행하지 않습니다. 원래 sample manifest로 복원하므로 사용자가 수정한 live sample 설정을 보존하는 도구가 아닙니다.

```bash
sudo env TEST_WAIT_TIMEOUT=180s RESULT_DIR=/var/tmp/jasmin-poc-results \
  bash scripts/test-poc.sh --disposable-node
```

추가 의존성은 `python3`뿐입니다. 테스트별 `[RUN]`, `[PASS]`, 예상 밖 오류에 `[FAIL]`을 출력합니다. 결과는 `results.tsv`, 단계별 `.log`, 부하 측정 `.json`으로 보존합니다. 실패 주입을 정상적으로 감지한 경우에는 테스트 결과가 PASS입니다. API 연결 실패를 미설치로 오인하지 않으며, 예상 오류라도 0이 아닌 종료 코드와 원인 로그가 있어야 통과합니다.

- 신규 설치: K3s가 없는 경우 먼저 설치합니다. 이미 설치돼 있으면 신규 설치 항목은 SKIP하고 나머지를 진행합니다. 마지막에는 전체 제거·재설치도 별도로 검증합니다.
- 재실행: `install.sh` 두 번 추가 실행, Deployment/Pod UID(개체 고유 번호) 유지 확인.
- 복구: sample Pod 강제 삭제, rollout restart(앱 재시작), 이미지 `1.28.0-alpine → 1.28.1-alpine` 변경, K3s 서비스 재시작.
- 오류: 잘못된 이미지 형식, 없는 태그, Service targetPort 81, readiness 주소 404, Cilium 비정상 이미지, Cilium 제거 상태.
- 네트워크: Pod의 DNS(이름 조회)·ClusterIP(클러스터 내부 Service 주소)·NodePort 접근. 테스트용 NetworkPolicy(통신 허용·차단 규칙)를 적용하기 전 두 클라이언트가 모두 접근 가능한지 확인한 뒤, 적용 후 승인한 클라이언트만 접근하고 금지 클라이언트는 DNS가 정상인데 HTTP만 실패하는지 확인합니다. 정책은 테스트 후 제거합니다. 외부 NodePort 제한이나 클러스터 전체 보안 정책을 검증하는 것은 아닙니다.
- 부하: 정상 상태 100요청/동시 8개. 앱 재시작과 이미지 변경 중에는 별도 연속 HTTP 요청을 보내 HTTP 200과 응답 본문을 검증합니다. K3s 재시작 중 실패는 측정하되 무중단을 요구하지 않고 복구 후 정상 응답을 필수 확인합니다.
- 제거: sample namespace 삭제·재배포, Cilium/K3s 공식 uninstall·재설치합니다. 커널 잔여 상태를 모든 환경에서 완전히 제거한다는 보장은 없으므로 VM 재부팅/재생성이 가장 확실합니다.

실패/일반 종료 신호 때 sample과 Cilium을 복원하도록 시도합니다. 강제 전원 종료나 `kill -9`에는 복원할 수 없습니다. 복원도 실패하면 별도 FAIL을 남깁니다. 다른 install/deploy 작업과 동시에 실행하지 않습니다. `results.tsv`와 단계 로그를 보존해 수동 복구합니다.

Mac 호스트(가상머신 밖의 컴퓨터)에서 Lima를 사용하는 예시:

```bash
limactl create --name=jasmin-poc-test --cpus=2 --memory=4 --disk=20 \
  --containerd=none --set='.portForwards = [{"guestPort":30080,"hostPort":30081,"static":true}]' \
  --tty=false template:ubuntu-24.04
limactl start jasmin-poc-test --tty=false
limactl shell jasmin-poc-test
# VM 안에서 Mac의 프로젝트를 VM 홈으로 복사하고 위 test-poc.sh 실행
```

`--set`으로 NodePort의 정적 전달을 명시합니다. 별도 클라이언트인 Mac에서 실제 접근을 추가 검증합니다.

```bash
curl --noproxy '*' -i http://127.0.0.1:30081/
python3 scripts/load-http.py http://127.0.0.1:30081/ --requests 100 --concurrency 8
```

`scripts/load-http.py`는 HTTP 실패 또는 잘못된 본문이 하나라도 있으면 비정상 종료합니다. `--allow-failures`는 장애 관찰용으로만 사용하며 실패 수를 숨기지 않습니다. 작은 규모의 정상 응답 검사이므로 최대 처리량이나 Production(실제 서비스 운영) 성능 보장이 아닙니다.

VM 강제 종료 뒤 Mac 접속만 시간 초과라면 `lsof -nP -iTCP:30081 -sTCP:LISTEN`과 Lima의 `ha.stderr.log`를 확인합니다. 이번 검증에서는 종료 전 SSH 연결이 포트를 계속 점유해 새 정적 전달 설정이 실패했습니다. VM 내부 NodePort가 정상임을 먼저 확인하고, 해당 테스트 VM의 오래된 SSH 프로세스임을 확인한 후 그 프로세스만 종료했습니다. 아래 명령으로 **동일 NodePort로 향하는 SSH 전달**을 다시 생성하여 Mac에서 정상 응답을 확인했습니다. Kubernetes API port-forward를 사용한 것은 아닙니다.

```bash
ssh -F ~/.lima/jasmin-poc-test/ssh.config -N -f \
  -L 127.0.0.1:30081:127.0.0.1:30080 lima-jasmin-poc-test
curl --noproxy '*' --max-time 10 -i http://127.0.0.1:30081/
```

2026-10-01 실제 검증 결과와 실패/복구 증거는 [검증 보고서](reports/VALIDATION-2026-10-01.md)에 정리했습니다. 이 결과의 외부 클라이언트는 Mac 호스트이며 인터넷 공개 URL 검증은 아닙니다.

## 후속 설계 TODO (현재 구현 범위 밖)

- Provider Interface(인프라 제공 환경과 배포를 연결하는 규약)는 노드 식별자, 실행 경로(local/cloud-init/SSM/SSH), root 실행 권한, 내부 IPv4, NIC/CIDR 충돌 여부, OS/kernel/CPU 구조, DNS·이미지 다운로드 경로, 외부 접근 주소·방화벽 준비 여부를 입력으로 전달합니다. 지금 스크립트가 처리하는 가변 입력은 버전·NODE_IP·WAIT_TIMEOUT·VERIFY_URL이며 나머지는 adapter의 전제조건입니다.
- 출력 규약은 종료 코드, 실패 단계, K3s/Cilium 버전, Node Ready, workload rollout 상태, 서버 내부 HTTP 결과와 외부 클라이언트 결과, 증거 경로를 구분하도록 정합니다. JSON 결과 규약은 아직 구현하지 않았습니다. kubeconfig나 토큰을 일반 로그/API 응답에 넣지 않습니다.
- 잘못된 새 버전은 rollout 실패로 보고하고 기존 정상 Pod를 유지합니다. 실패 버전을 자동 롤백(이전 버전 복원)하는 Production 로직은 없습니다. 테스트만 원래 manifest를 다시 적용해 복원합니다.
- 임의 앱에는 nginx Alpine의 wget, 고정 응답 본문을 전제로 하는 verify를 그대로 쓸 수 없습니다. 앱별 `/healthz`, 검증 방식, 외부 HTTPS/Tunnel 경로를 정의합니다.
- Production 우선순위: 설치 설정 drift(실제 설정과 선언의 차이) 검사 및 중단된 Helm 작업 복구 → 비밀정보/RBAC·Pod 보안·최소 통신 정책 → 이미지 digest(변경되지 않는 이미지 식별값)·업데이트 승인·LKG(마지막 정상 버전) 복구 → 운영 대상 OS/Provider/외부 접속 검증 → 저장소·백업/복구 → 다중 노드·HA(일부 서버 장애에도 서비스를 유지하는 구성).
- 단일 replica(실행 앱 개수)는 강제 Pod 삭제·노드 재부팅에서 일시 중단될 수 있습니다. 정상 rollout은 `maxUnavailable: 0`(준비된 앱 수를 줄이지 않음), 추가 Pod 1개와 종료 전 5초 대기를 사용합니다. 추가 Pod를 띄울 자원이 부족하면 교체는 기다리거나 실패하며 자동으로 보장되지 않습니다. [Kubernetes Deployment 문서](https://kubernetes.io/docs/concepts/workloads/controllers/deployment/)

공식 근거: [Cilium K3s 설치](https://docs.cilium.io/en/stable/installation/k3s/), [Kubernetes 호환성](https://docs.cilium.io/en/stable/network/kubernetes/compatibility/), [Cilium 시스템 요구사항](https://docs.cilium.io/en/stable/operations/system_requirements/), [K3s 구성](https://docs.k3s.io/installation/configuration), [K3s 제거](https://docs.k3s.io/installation/uninstall).
