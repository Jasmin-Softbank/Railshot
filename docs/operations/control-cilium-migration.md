# 운영 K3s의 Cilium 설치·전환·복구

이 문서는 **실행 전 검토용 runbook**이다. 이 PR의 설치기·로컬 검사는 실행 중인 AWS 운영 노드의 CNI 전환을 증명하지 않는다. 기존 노드 전환, 재시작, 백업 자원 생성, 클러스터 복구, AWS/GCP 변경은 이 PR에서 실행하지 않았다. 아래 변경 명령은 별도로 승인한 유지보수 창에서만 사용한다. 특히 기존 Flannel 노드의 전환·복구 절차는 해당 환경의 격리 리허설을 통과하기 전까지 **제안·미검증** 상태다.

## 1. 적용 대상과 설치 계약

2026-10-02 18:08 KST 조사 기준 대상은 AWS 계정 `721622471953`, 서울 `ap-northeast-2`, `railshot-control-poc` / `i-033ae2db907fde68e`, private IP `172.31.0.172`다. t3.medium 2 vCPU / 4 GiB, 30 GiB gp3, K3s `v1.34.11+k3s1` amd64이며 Flannel VXLAN과 `wg-railshot`이 함께 있었다. Argo 7개와 CoreDNS/local-path Pod 2개가 Ready였고 Dashboard/API는 없었다. 이는 이전 관측이며 전환 직전에 다시 확인한다.

| 항목 | 운영 control | 고객 runtime |
|---|---|---|
| 실행 진입점 | 저장소 전체의 `infrastructure/ansible/control.sh` | 기존 runtime/Ansible 진입점 |
| 공통 Cilium 호출 | `deployment/cilium/install.sh control` | 인자 없이 기존 호출 |
| Pod / Service CIDR | `10.52.0.0/16` / `10.53.0.0/16` | `10.42.0.0/16` / `10.43.0.0/16` |
| Cilium / CLI | `1.20.2` / `v0.20.1`, 저장소 image digest 고정 | 동일 |
| datapath | cluster-pool, VXLAN, kube-proxy 유지, operator 1개, Hubble off | 동일 |
| workload | 운영 Argo, CoreDNS, local-path 유지 | 고객 앱; 기존 고객 storage 계약 유지 |

운영 설치기는 root/amd64, `control` 역할, 기존 설정, 기존 CNI를 검사하고 운영·runtime lock을 잡는다. 공통 `deployment/cilium/preflight.py`도 포함하므로 설치기 파일 하나만 복사하지 않고 저장소 전체를 사용한다. Flannel 설정·인터페이스·다른 활성 CNI가 남은 서버를 자동으로 전환하지 않는다. 재실행은 동일 운영 설정과 Cilium 프로파일·버전의 재조정만 허용한다. 기존 Cilium의 CIDR 또는 버전을 다른 값으로 덮어써서 전환하는 경로도 없다. 공통 설치기를 고객 기본값으로 운영 서버에 직접 실행하지 않는다.

K3s에서 custom CNI를 사용할 때 Flannel과 내장 NetworkPolicy를 비활성화한다. kube-proxy는 유지하므로 `disable-kube-proxy`는 넣지 않는다. K3s API를 먼저 기다리고 Cilium을 설치한 다음 Node Ready를 기다린다. CNI 설치 전에 Node Ready를 기다리면 새 서버에서 진행이 멈출 수 있다. [K3s custom CNI](https://docs.k3s.io/networking/basic-network-options#custom-cni), [Cilium K3s 설치](https://docs.cilium.io/en/stable/installation/k3s/)

## 2. 신규 운영 서버

기존 datastore/CNI/workload가 없는, 별도로 준비·승인한 전용 Linux amd64 서버에서만 실행한다. Python 3, curl, tar, sha256sum, flock, systemd와 필요한 커널 기능을 확인하고 고정된 K3s·Cilium·Argo 이미지/다운로드 주소에 접근할 수 있어야 한다. 이 운영 진입점은 온라인 설치다. 고객용 airgap bundle을 운영 프로파일로 가정하지 않는다. 커널/eBPF 요구사항은 실제 OS에서 확인한다. [Cilium 시스템 요구사항](https://docs.cilium.io/en/stable/operations/system_requirements/)

검토한 PR commit의 **저장소 전체**를 복사하고 commit을 기록한다. 아래는 그 저장소 root에서의 실행이다.

```sh
sudo bash infrastructure/ansible/control.sh
```

성공 흐름은 `역할/기존 CNI 검사 → 운영 K3s 설정 → API readyz → Cilium(control) → Cilium/Node/CoreDNS ready → Argo 설치/rollout`이다. 설치기 종료 후 6절의 기능 인수를 별도로 수행한다. 새 운영 노드 준비가 AWS ENI/EIP, ALB, DNS 또는 WireGuard 경로 변경을 승인하지는 않는다.

## 3. 기존 Flannel 운영 노드: 먼저 실행을 거부한다

기존 운영 서버에 위 명령을 실행하면 Flannel 설정을 조용히 덮어쓰지 않고 거부해야 한다. 설정 파일만 바꾸거나 `10-flannel.conflist`만 지운 뒤 재시도하지 않는다. 기존 PodSandbox는 이전 CNI의 주소·인터페이스를 계속 사용하므로 전환에는 모든 기존 PodSandbox의 교체가 필요하다.

공식 Cilium live migration은 별도 Pod CIDR, 별도 overlay protocol/port, cluster-pool IPAM과 노드별 전환을 사용한다. 기존 NetworkPolicy provider는 검증되지 않은 경우로 명시하며 전환 중 Cilium 정책을 끄는 단계도 있다. 현재 단일 노드와 기존 `10.52.0.0/16`을 유지하는 요청에는 이 절차를 그대로 적용할 수 없다. 정책을 삭제하거나 다른 CIDR을 임시 추가하는 범위를 이번 설치기에 넣지 않았다. [Cilium migration 요구사항과 한계](https://docs.cilium.io/en/stable/installation/k8s-install-migration/)

여기서는 **중단을 수용한 단일 노드 전환**을 제안한다. K3s datastore·token·Service CIDR·역할은 유지하고, Flannel PodSandbox와 host network 상태를 정리한 다음 Cilium을 설치한다. 공식 문서를 조합한 환경별 운영 절차이며 K3s가 제공하는 자동 migration 명령은 아니다. 단일 노드이므로 무중단 또는 drain 후 다른 노드로 이동을 보장할 수 없다.

운영 노드는 GCP 앱의 WireGuard gateway이기도 하다. 유지보수 영향에는 Argo/API뿐 아니라 `ALB → 운영 ENI → wg-railshot → GCP NodePort → Pod → GCP → wg-railshot → ALB`가 포함된다. 고객 AWS 노드의 CNI는 변경하지 않지만 공유 경로 전체를 회귀 확인한다.

### 3.1 시작 조건

다음 항목을 변경 작업 기록에 채우기 전에는 5절로 진행하지 않는다.

- 담당자, 승인된 대상/commit, 시작 시각, 허용 중단시간, **복구를 시작할 마감 시각**, 성공/실패 판단자.
- 현재 노드 수가 1인지, 실행 버전/역할/CIDR이 위와 같은지. 다른 CNI, 추가 노드, 사용자 workload가 발견되면 절차를 재검토한다.
- 외부의 독립된 관리 경로. WireGuard에 의존하지 않는 SSM 접속과 호스트 복구 수단을 확인한다. SSM session ID는 작업자가 관리하고 사용 후 종료한다.
- 고객 배포·CI→GitOps 쓰기를 중지할 방법, 진행 중인 Argo sync/Job의 완료 여부, 변경 동안 고객 클러스터를 건드리지 않는 유지보수 계획.
- 별도 격리 환경에서 동일 K3s/Cilium/OS 프로파일의 설치·전환·복구 리허설 결과와 소요 시간. 복구본의 Argo/WireGuard가 실제 고객·운영 endpoint에 접속하지 못하도록 네트워크를 먼저 격리한다. 복구본을 운영과 동시에 활성화하지 않는다.
- 아래 백업의 무결성·복원 확인, 노드 밖의 암호화된 백업 위치, 복호화 권한과 token 접근 권한. 백업 위치가 노드의 같은 root volume 하나뿐이면 시작하지 않는다.

## 4. 상태 확인과 백업

비밀을 터미널·SSM 응답·CI 로그·Git에 출력하지 않는다. `set -x`, `kubectl get secret -o yaml`의 stdout 출력, `wg show ... dump`, `wg showconf` 출력은 사용하지 않는다. root shell에서 `umask 077`을 적용하고, 출력은 checkout 밖의 0700 디렉터리에 0600 파일로 저장한다. base64 Secret도 비밀이다. 아래 경로는 기본 설치에 대한 예이며 실제 `data-dir`가 다르면 **모든 백업/복구 경로를 함께** 바꿔 검토한다.

```sh
umask 077
backup_dir=/root/railshot-control-before-cilium-$(date -u +%Y%m%dT%H%M%SZ)
install -d -m 0700 "$backup_dir"
systemctl cat k3s > "$backup_dir/k3s-unit.txt"
systemctl show k3s -p ExecStart -p EnvironmentFiles > "$backup_dir/k3s-startup.txt"
systemctl cat wg-quick@wg-railshot > "$backup_dir/wireguard-unit.txt"
k3s --version > "$backup_dir/k3s-version.txt"
k3s kubectl get nodes -o yaml > "$backup_dir/nodes.yaml"
k3s kubectl get pods,deployments,statefulsets,daemonsets,jobs,cronjobs -A -o yaml > "$backup_dir/workloads.yaml"
k3s kubectl get pv,pvc -A -o yaml > "$backup_dir/volumes.yaml"
k3s kubectl get services,endpointslices,networkpolicies -A -o yaml > "$backup_dir/network-resources.yaml"
ip -details addr > "$backup_dir/addresses.txt"
ip route show table all > "$backup_dir/routes.txt"
ip rule show > "$backup_dir/rules.txt"
iptables-save > "$backup_dir/iptables.save"
ip6tables-save > "$backup_dir/ip6tables.save"
sysctl net.ipv4.ip_forward net.ipv4.conf.all.rp_filter > "$backup_dir/forwarding.txt"
wg show wg-railshot latest-handshakes > "$backup_dir/wg-handshakes.txt"
wg show wg-railshot transfer > "$backup_dir/wg-transfer.txt"
```

실제 firewall가 nftables인 경우 `nft list ruleset`도 비공개 파일로 저장한다. `iptables` backend, 실제 external NIC/WireGuard interface의 `rp_filter`, MTU, FORWARD 기본 정책, persist/boot 시 적용되는 rule 파일·systemd unit을 기록한다. `wg-railshot.conf`의 PostUp/PostDown 또는 외부 스크립트가 있다면 그 파일도 백업한다. 인터페이스별 sysctl은 인터페이스 이름을 확인해 조회한다.

### 4.1 datastore 종류를 먼저 확정한다

`systemctl` unit/drop-in, EnvironmentFile, `/etc/rancher/k3s/config.yaml` 및 존재하는 config fragment를 **보안 경로에서** 읽어 `--data-dir`, `--datastore-endpoint`/`K3S_DATASTORE_ENDPOINT`, `cluster-init`, `server`를 확인한다. connection string에는 비밀번호가 들어갈 수 있다. 값은 작업 로그에 쓰지 않고 종류·파일 위치·백업 식별자만 기록한다. 실제 시작 로그와 디스크도 대조한다.

| 종류 | 판단 근거 | 일관된 백업 / 복구 |
|---|---|---|
| SQLite | external endpoint/embedded etcd 설정이 없고 실제 `server/db/state.db` 사용 | K3s 정지 후 `server/db/` 전체와 server token 복사. WAL/SHM을 빠뜨리지 않는다. 복구 시 정지 상태에서 원본 디렉터리 전체와 같은 token 복원 |
| embedded etcd | 실제 `server/db/etcd`와 실행 설정/로그의 embedded etcd | 실행 중 `k3s etcd-snapshot save --name pre-cilium` 후 `k3s etcd-snapshot ls`로 실제 생성 경로 확인·외부 보관. 복구는 아래 snapshot restore 절차 |
| external MySQL/PostgreSQL/etcd | 유효 datastore endpoint와 서버 로그 | DB 소유자가 해당 제품의 native consistent backup/PITR와 복구를 담당. 로컬 K3s 파일 복사만으로 DB가 백업되지 않는다. DB 쓰기 동결·복구 checkpoint를 합의하지 못하면 전환 중지 |

`state.db` 파일 하나가 있다는 사실만으로 SQLite라고 확정하지 않는다. K3s는 디스크의 embedded etcd 상태도 선택에 사용한다. 종류가 불명확하거나 기본 설치 계약에서 벗어난 설정이면 일반 설치기의 config 검사를 우회하지 말고 별도 변경 계획을 만든다. [K3s datastore 선택](https://docs.k3s.io/datastore)

모든 경우 `/var/lib/rancher/k3s/server/token`을 같은 백업 시점에 보관한다. 원본 token 없이 새 token으로 datastore를 복구하면 내부 기밀 데이터를 해독하지 못할 수 있다. server TLS/CA, secrets-encryption 설정·키를 포함한 `server/`도 보존한다. secrets encryption의 활성 여부를 확인하되 이번 전환에서 token·CA·암호화 키를 회전하지 않는다. [K3s datastore 백업·token 요구사항](https://docs.k3s.io/datastore/backup-restore)

### 4.2 Argo와 고객 클러스터 등록

정확한 Argo 버전, Application/AppProject/ApplicationSet, sync 정책, repo revision, 대상 server/namespace, credential Secret **이름·종류·개수**를 보관한다. `argocd-secret`, repository/repo-creds/cluster Secret, ConfigMap(RBAC, TLS trust, SSH known_hosts 포함)은 datastore에 들어 있지만 독립 복구용 export도 남긴다.

```sh
k3s kubectl -n argocd get secrets,configmaps,applications.argoproj.io,appprojects.argoproj.io,applicationsets.argoproj.io \
  -o yaml > "$backup_dir/argocd-resources.private.yaml"
```

추가 Application namespace 또는 외부 Secret provider가 있으면 그 소유자가 별도로 보관한다. 기존 관리자 환경에 동일 판본 Argo CLI가 있으면 `argocd admin export -n argocd > ...`도 사용할 수 있다. 잘못된 namespace의 export가 성공해도 내용이 비어 있을 수 있으므로 개수를 대조한다. 복구는 전체 datastore 복원을 우선하며, fresh Argo에만 검토한 export를 `argocd admin import -n argocd -`로 넣는다. 전체 datastore 복구 뒤에 무조건 import하지 않는다. [Argo disaster recovery](https://argo-cd.readthedocs.io/en/stable/operator-manual/disaster_recovery/)

고객 클러스터의 ServiceAccount/RBAC·토큰은 이 운영 CNI 변경의 교체 대상이 아니다. 같은 credential Secret과 server CA/server 주소를 유지한다. 복구 후 Secret 존재뿐 아니라 실제 Argo의 고객 AWS/GCP API 연결 성공까지 확인해야 한다.

### 4.3 local-path, 호스트 파일, WireGuard

Kubernetes PV/PVC 객체와 실제 volume 내용은 별개다. `volumes.yaml`의 `hostPath`/`local.path`, nodeAffinity 및 `/var/lib/rancher/k3s/storage` 외의 실제 경로를 목록으로 만든다. 사용자가 데이터를 쓰는 Pod는 정상 종료하고 DB가 있다면 앱의 consistent backup도 수행한다. mount 대상이 외부 volume이면 해당 volume 백업을 별도 지정한다.

백업 대상에는 `/etc/rancher/k3s`, `/etc/rancher/node`, K3s systemd unit·env·drop-in, 원본 K3s 바이너리/killall 스크립트, `/etc/railshot`, `/etc/wireguard`, peer/개인키의 실제 파일, 관련 sysctl/firewall unit·파일을 포함한다. 현재 CNI conf 디렉터리(`/var/lib/rancher/k3s/agent/etc/cni/net.d`, `/etc/cni/net.d`)도 보존한다. 존재 여부를 확인하고 외부에 연결된 symlink의 대상은 따로 포함한다. Secret·키 파일의 owner/mode를 보존한다.

AWS primary ENI/EIP, source/destination check, SG, ALB target group/health, **모든 ALB subnet의 유효 route table**, GCP 방화벽·peer·return route의 식별자를 read-only로 기록한다. Terraform state는 기존 private authoritative 경로에서 별도 보존한다. CNI 전환을 위해 state 복제본에 apply하거나 새 EIP/DNS/route를 만들지 않는다.

## 5. 승인 후 단일 노드 중단 전환 — 리허설 필수

이 절차는 기본 경로와 기존 Flannel 단일 노드가 확인된 경우만 사용한다. 명령의 원격 실행 승인은 이 문서에 포함되어 있지 않다.

1. **변경 동결과 사전 백업.** 고객 배포 입력을 중지하고 진행 중인 sync를 끝낸다. 기존 replica 수와 autosync 설정을 기록한 뒤 Argo application/ApplicationSet controller를 중지하는 유지보수 조작을 수행한다. PV 사용 workload는 정상 종료한다. controller 자동 재생성, active Job, 변경 중인 DB가 남으면 진행하지 않는다. datastore/Argo export와 4절 host/network 자료를 우선 확보한다.
2. **노드 전체 중단.** SSM과 독립 복구 경로가 살아 있는지 확인한다. 설치된 `/usr/local/bin/k3s-killall.sh`의 내용을 현재 버전 소스와 대조한다. Cilium interface/release가 아직 없는 Flannel 서버에서만 아래를 실행한다. 단순 `systemctl stop k3s`는 컨테이너를 남기므로 충분하지 않다.

   ```sh
   /usr/local/bin/k3s-killall.sh
   systemctl is-active k3s
   ```

   `is-active`는 inactive/비정상 종료값이 기대 결과다. killall은 datastore를 지우지 않지만 Pod/containerd 상태, Flannel 인터페이스와 KUBE-/CNI-/Flannel firewall 규칙을 정리한다. **WireGuard 인터페이스가 남아도 전달 경로가 끊길 수 있다.** 그 시점부터 GCP public 앱 경로를 정상이라고 가정하지 않는다. [K3s stop/killall](https://docs.k3s.io/upgrades/killall), [고정 K3s 버전의 killall 생성 코드](https://github.com/k3s-io/k3s/blob/v1.34.11%2Bk3s1/install.sh#L725)
3. **cold backup 확정.** K3s와 PV writer가 모두 정지한 상태에서 SQLite `server/db/` 또는 embedded etcd snapshot, 같은 server token 및 4절 전체 파일/PV를 ACL/xattr/owner/mode 보존 방식으로 복사한다. 파일이 변경되었다는 tar 경고를 성공으로 취급하지 않는다. archive 목록·SHA-256·외부 암호화 저장 readback을 확인한다. 이후 실패 시 돌아갈 **하나의 복구 checkpoint**를 기록한다. Argo 중지 후 저장한 checkpoint라면 복구 시 원래 replica 복원을 별도 수행해야 한다.

   아래 GNU tar 예시는 검토한 `backup-paths.txt`가 준비된 뒤 실행한다. 목록에는 `/` 기준 상대 경로를 한 줄에 하나씩 넣는다. 최소 `etc/rancher/k3s`, `etc/rancher/node`, `etc/railshot`, `etc/wireguard`, `var/lib/rancher/k3s/server`, 실제 PV 경로, 실제 K3s/CNI/WireGuard unit·설정·바이너리 경로를 포함한다. 존재하지 않는 경로나 mount·symlink의 미포함 대상을 확인하고 root 전체나 backup_dir 자체를 목록에 넣지 않는다.

   ```sh
   tar --acls --xattrs --numeric-owner -C / -cpf "$backup_dir/cold-files.tar" \
     --files-from "$backup_dir/backup-paths.txt"
   tar -tf "$backup_dir/cold-files.tar" > "$backup_dir/cold-files.list"
   sha256sum "$backup_dir/cold-files.tar" > "$backup_dir/cold-files.sha256"
   ```

   이 archive에는 비밀이 들어 있다. 접근 제어한 암호화 백업 저장소로 옮긴 뒤 다시 내려받은 bytes의 checksum까지 대조한다. 전송 성공 응답만으로 복원 가능을 주장하지 않는다.
4. **기존 CNI 정리.** K3s가 정지한 채 `flannel.1`, `cni0`, 이전 Pod network namespace/sandbox가 남지 않았는지 확인한다. 두 CNI 경로에서 실제 Flannel을 가리키는 `.conf`/`.conflist`만 백업 디렉터리로 이동한다. 디렉터리 전체를 삭제하거나 다른 플러그인 파일을 덮어쓰지 않는다. 다른 CNI가 있으면 중지한다. K3s 내장 NetworkPolicy를 끄는 것만으로 기존 kube-router 규칙은 사라지지 않는다. killall 이후 `KUBE-ROUTER` 잔여 여부를 확인하고, 있으면 공식 정리 방법을 해당 host firewall과 함께 검토한다. 전체 `iptables -F`는 사용하지 않는다. [K3s NetworkPolicy 잔여 규칙](https://docs.k3s.io/networking/networking-services#network-policy-controller)
5. **운영 K3s config를 명시적으로 변경.** 기존 파일과 node-role을 백업한 다음 아래 파일을 0600으로 작성한다. 기존 파일에 추가 설정/fragment, custom datastore 등이 있다면 이 파일로 덮어쓰지 말고 변경 계획을 재검토한다. CIDR/token/datastore는 바꾸지 않는다.

   ```yaml
   flannel-backend: none
   disable-network-policy: true
   cluster-cidr: 10.52.0.0/16
   service-cidr: 10.53.0.0/16
   write-kubeconfig-mode: "0600"
   disable:
     - traefik
     - servicelb
     - metrics-server
   ```

6. **검토한 설치기로 재기동.** 정확한 PR commit의 저장소 root에서 `sudo bash infrastructure/ansible/control.sh`를 실행한다. 이 단계는 API 재기동, 공통 Cilium(control) 설치, Node/CoreDNS Ready, Argo manifest 적용 및 rollout을 수행한다. controller가 다시 기동하므로 입력 동결과 고객 sync 설정이 유효해야 한다. 설치기 검사에 걸리면 `force`/가짜 role/lock 삭제로 우회하지 않는다. API 또는 Cilium timeout이면 실패 시각과 진단을 기록하고 정한 복구 마감 안에서 판단한다.
7. **기능 인수 후 동결 해제.** 6절을 모두 확인하고 중지했던 replica·sync 정책을 원래 값으로 복원한다. `control.sh`의 Argo 설치만으로 모든 기존 custom replica·정책이 복구되었다고 가정하지 않는다. 외부 트래픽과 새 배포 입력 재개 시각을 각각 기록한다.

기존 IPAM pool의 목록/크기를 임의로 수정하지 않는다. Cilium의 CIDR이 이미 다른 값으로 저장돼 있다면 이 전환 프로파일과 다른 클러스터다. [Cilium cluster-pool 주의사항](https://docs.cilium.io/en/stable/network/kubernetes/ipam-cluster-pool/)

## 6. 전환 후 기능 인수

모든 결과에 대상·시각·source commit·버전·명령의 exit code를 붙인다. 실패 로그에 credential이 포함되지 않도록 확인한다. Cilium status와 Node Ready는 아래 경로의 통과를 대신하지 않는다.

격리 서버 또는 설치·검사 namespace 생성까지 승인된 운영 유지보수에서 저장소 root 기준으로 최소 runnable regression을 실행한다.

```sh
sudo bash infrastructure/ansible/test-control-cilium.sh --run
```

이 검사는 자신이 생성한 임시 namespace와 UID를 확인해 정리한다. Pod의 ServiceAccount·CA로 로컬 Kubernetes API 인증, cluster DNS/HTTP, host→InternalIP의 `externalTrafficPolicy: Local` NodePort, 정상 연결이 확인된 두 client의 ingress deny 후 label 선택 allow, Cilium/CoreDNS와 모든 Argo workload rollout, 한 시점의 node memory를 검사한다. 실패 시 namespace 정리 실패도 확인한다. **등록된 Argo 원격 API 연결, 외부 ALB/WireGuard 경로, DNS TCP 전용 검사와 지속 부하/메모리 여유는 자동 smoke에 포함되지 않는다.** 아래 수동 인수 항목을 함께 완료한다.

```sh
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
/usr/local/lib/railshot-deployment/cilium status --wait --wait-duration 300s
k3s kubectl get nodes -o wide
k3s kubectl -n kube-system get pods -o wide
k3s kubectl -n kube-system get ciliumnodes -o yaml
k3s kubectl -n kube-system rollout status deployment/coredns --timeout=300s
k3s kubectl -n argocd get pods -o wide
```

| 검사 | 통과 근거 / 실패 의미 |
|---|---|
| 프로파일 | Cilium image digest, cluster-pool `10.52.0.0/16`, VXLAN, kubeProxyReplacement=false, operator 1개, Hubble off. 고객 CIDR 유입·새 flannel/cni0·다른 active CNI conf 없음 |
| 모든 Pod 이관 | hostNetwork 제외 Pod의 Cilium endpoint와 Pod IP 매핑 확인. 단순히 같은 `10.52` 주소를 갖는 것만으로 이관을 판정하지 않음. old sandbox 없음 |
| CoreDNS | 승인된 임시 검사 Pod에서 `kubernetes.default.svc.cluster.local`, Argo service 및 필요한 외부 Git/registry DNS의 실제 질의 성공. DNS UDP와 TCP 경로 확인 |
| NetworkPolicy | 승인된 검사 namespace에서 같은 목적지로 allowed client 성공·denied client timeout/차단을 각각 확인. 정책 없는 연결 성공과 정책 설치 후 deny/allow를 비교. 기존 Argo 정책의 API/DNS/repo egress도 확인 |
| Argo API | 운영 Kubernetes API `/readyz`, Argo server health와 application-controller/repo-server 정상. 같은 credential로 등록 고객 AWS/GCP cluster connection 상태 정상, 실제 조회/refresh 성공. 권한 없는 자동 sync는 하지 않음 |
| NodePort | 기존 Service의 `externalTrafficPolicy: Local`, target node와 EndpointSlice의 ready local endpoint 일치. 승인된 외부 경로에서 IP:NodePort health와 기대 앱 본문 확인. localhost curl만으로 통과 처리하지 않음 |
| WireGuard 왕복 | 아래 순서로 새 handshake/송수신 증가·양방향 route·ALB target health·외부 HTTPS 결과를 함께 확인 |
| 저장소 | 기존 PVC/PV binding, nodeAffinity, 실제 데이터 checksum/앱 read 검증. PV 객체가 Bound라는 사실만으로 데이터 복구를 판정하지 않음 |

`externalTrafficPolicy: Local`은 해당 노드의 ready endpoint가 없으면 전달하지 않는다. 로컬 endpoint와 외부 요청을 함께 검사하며 실패를 피하려고 `Cluster`로 바꾸지 않는다. 단일 운영 노드에 아직 NodePort workload가 없다면 승인된 disposable fixture로 Local 경로를 검사하거나 **운영 NodePort 미검증**으로 기록한다. 고객 노드의 Local NodePort/WireGuard 경로는 기존 앱에서 확인한다. [Kubernetes traffic policies](https://kubernetes.io/docs/reference/networking/virtual-ips/#traffic-policies)

WireGuard는 `wg show wg-railshot latest-handshakes`, `wg show wg-railshot transfer`, 운영의 `ip route get <GCP target IP>`, 고객의 `ip route get <실제 ALB subnet 내 source IP>`를 확인한다. 기존 ALB 양 AZ subnet의 `/32 → 운영 ENI` 경로, `source_dest_check=false`, 제한된 UDP51820/NodePort SG, GCP의 ALB subnet return route/AllowedIPs, host forwarding/MTU/rp_filter를 전후 대조한다. handshake만으로 전달을 통과시키지 않는다. 실제 **ALB를 거친** health/HTTPS와 양쪽 인터페이스 카운터 증가로 왕복을 확인한다. `FORWARD ACCEPT` 전역 설정이나 광역 MASQUERADE를 임시 처방으로 추가하지 않는다. 기존 host rule과 Cilium rule의 실제 packet 경로를 조사한다. [저장소 WireGuard 계약](../../infrastructure/terraform/aws-edge/README.md)

공식 `cilium connectivity test`는 임시 workload·정책을 생성하는 검사다. 격리 리허설에서는 실행하고 결과를 보관한다. 운영에서는 검사 namespace와 리소스/외부 egress 범위를 유지보수 계획에 포함한 경우에만 실행한다. 그 성공도 실제 AWS ALB/GCP WireGuard 경로를 대신하지 않는다.

### 6.1 메모리 여유를 다시 측정한다

기준 관측은 2026-10-02 18:08 KST **Cilium 이전** node working set `1,497,214,976 bytes`(약 1.39 GiB), available `2,521,214,976 bytes`(약 2.35 GiB)다. metrics-server가 없으므로 `kubectl top`의 실패를 0 사용량으로 쓰지 않는다. 같은 kubelet summary 경로를 전환 직전, 설치 후 안정화, 실제 Argo refresh/외부 요청 동안 다시 수집한다.

```sh
node_name=$(k3s kubectl get node -o jsonpath='{.items[0].metadata.name}')
k3s kubectl get --raw "/api/v1/nodes/$node_name/proxy/stats/summary" > "$backup_dir/kubelet-summary-after.json"
free -b > "$backup_dir/memory-after.txt"
k3s kubectl get events -A --sort-by=.lastTimestamp > "$backup_dir/events-after.txt"
```

node `workingSetBytes`/`availableBytes`, Cilium agent/operator/Envoy와 Argo Pod별 working set, RSS, 재시작/OOM/MemoryPressure를 구분해 기록한다. kubelet/system container와 node 합계는 겹칠 수 있으므로 더하지 않는다. 최소 15분 안정화 관측과 실제 요청 중 최저 available을 남기고 Dashboard/API를 위한 검토한 resource budget을 뺀 여유를 평가한다. **Cilium 전 2.35 GiB 여유를 전환 후 수치로 재사용하지 않는다.** 부족하면 제품 workload 추가를 보류하고 크기 변경/분리를 별도 검토한다. 이 PR은 Cilium의 메모리 증가량이나 t3.medium 충분성을 실측했다고 주장하지 않는다.

## 7. 실패 시 복구

전환 후 CNI만 삭제하면 기존 PodSandbox, Cilium CRD/IPAM·BPF/iptables, K3s 설정이 서로 다른 시점으로 남는다. **`cilium uninstall` 또는 `k3s-uninstall.sh`를 rollback 명령으로 사용하지 않는다.** K3s uninstall은 local datastore와 local PV 데이터를 지울 수 있다. [K3s uninstall 영향](https://docs.k3s.io/installation/uninstall)

| 실패 시점 | 복구 경로 |
|---|---|
| 사전 검사 거부, 변경 없음 | 원인만 기록. 원본 설정/CNI/WireGuard 동작 확인; 백업 복원 불필요 |
| killall 이후, Cilium 설치 전 | 원본 Flannel config/CNI conf·host rule 복원 후 기존 K3s 시작. 정지·변경한 replica/sync 설정 원복. datastore가 손상·변경됐거나 결과가 불명확하면 checkpoint 전체 복원 |
| Cilium 설치 시작 이후 | 입력 동결 유지. 사전 checkpoint의 datastore+token+config+volume+host/WG 상태로 복구하고 재부팅하여 혼합 CNI/BPF 상태 제거. 새 K3s 클러스터 생성으로 대체하지 않음 |

### 7.1 전체 복원 순서

1. 실패한 현 상태와 시각을 비공개로 보존하고 SSM/독립 복구 접속을 확인한다. 고객 클러스터 sync와 새 입력은 계속 막는다. Cilium 환경에서 killall을 실행해야 한다면 먼저 K3s가 안내하는 `cilium_host`, `cilium_net`, `cilium_vxlan` interface 및 Cilium firewall 정리 조건을 적용한다. 이를 생략하면 호스트 연결을 잃을 수 있다. 이 정리는 **중단 중의 복구 작업**이며 작동 중인 운영망에서 시험하지 않는다. [K3s Cilium 정리 경고](https://docs.k3s.io/networking/basic-network-options#custom-cni)
2. K3s·컨테이너·volume writer를 정지한다. K3s가 복원 도중 자동 시작하지 않도록 유지보수 상태를 설정한다. 원본 config, unit/env/drop-in, 같은 K3s 바이너리, node identity, server token/TLS/암호화 파일, CNI config, host/WireGuard 파일과 PV를 복원한다. 변경 후 디렉터리 위에 단순 덧씌우면 새 파일이 남으므로 실패한 디렉터리는 비공개 quarantine으로 옮기고 검토한 archive로 완전 교체한다. Cilium이 새로 쓴 CNI conf와 `/var/lib/cilium` 등 persistent 상태도 확인하여 원래 Flannel checkpoint에 섞이지 않게 격리한다. Kubernetes namespace/PVC를 삭제해서 정리하지 않는다.
3. **SQLite:** K3s가 정지한 상태에서 원본 `server/db/` 전체를 교체하고 같은 server token을 복원한다. **embedded etcd:** 원본 config/token을 복원한 뒤 아래 공식 snapshot restore를 사용한다. **external datastore:** 담당자가 합의한 checkpoint로 복구하고 원본 endpoint/credential을 확인한다. external DB를 로컬 SQLite 파일로 대신하지 않는다.

   ```sh
   # embedded etcd가 확정된 경우에만; 경로는 실제 보관 snapshot으로 지정
   systemctl stop k3s
   k3s server --cluster-reset --cluster-reset-restore-path=/secure/approved/pre-cilium-snapshot
   # 성공 종료 후에도 여기서 일반 서비스를 시작하지 않는다.
   # 다음 단계에서 Flannel/WireGuard 복원·재부팅 조건을 먼저 확인한다.
   ```

   token은 원본 파일에 0600으로 복원해 사용하고 명령 인자에 값으로 넣지 않는다. 기존 S3 설정이 있는 상태에서 local snapshot을 사용할 때는 공식 문서대로 `--etcd-s3=false`를 추가한다. snapshot 경로 없는 `--cluster-reset`은 백업 복원이 아니다. 복구 바이너리는 이번 작업의 기존 `v1.34.11+k3s1`로 유지한다. [K3s embedded etcd restore](https://docs.k3s.io/cli/etcd-snapshot#restoring-snapshots)
4. 복구 전용 시작/자동 시작 순서를 점검한 후 노드를 재부팅한다. persisted Cilium CNI 설정·manifest가 되살아나지 않고 원본 Flannel만 시작해야 한다. 원본 CNI conf와 host 부팅 규칙이 복원된 상태에서 reboot를 수행해 transient BPF/route/interface를 정리한다. K3s 자동 시작을 유지보수용으로 해제했다면 재접속 후 `systemctl daemon-reload`와 `systemctl enable --now k3s`로 원본 서비스를 시작한다. 원본 firewall 전체 snapshot을 Cilium 실행 중에 그대로 restore하지 않는다. 규칙은 복원된 Flannel/WireGuard 상태와 일치해야 하며 K3s는 동적 kube-proxy/NetworkPolicy 규칙을 다시 작성한다.
5. `/readyz`, Node Ready, CoreDNS, 원본 replica, Argo credential/등록 목록과 **실제 고객 API 연결**, local-path 데이터, WireGuard/ALB 왕복, public HTTPS를 다시 확인한다. 원본 source/image revision과 작업 동결 중 고객 변경 여부도 대조한다. checkpoint 이후 승인된 변경은 복구에 포함되지 않을 수 있다. 검증 후에만 sync·입력을 재개한다.

전체 root disk snapshot을 복구점으로 사용할 경우에는 별도 승인한 AWS 복구 절차가 필요하다. EC2/volume 교체는 ENI/EIP·node name·local PV·WireGuard identity·SSM에 미치는 영향을 따로 검토한다. 이 runbook은 신규 VM/volume 생성이나 DNS/route 전환을 자동 수행하지 않는다.

## 8. 완료 기록

PR의 source/회귀검사, 격리 설치/전환 리허설, 원격 CI, 승인된 운영 적용, 운영 기능 인수와 메모리 측정을 각각 구분한다. 실제 적용 기록에는 대상 instance/node, source commit, 시작·복구 마감·종료 시각, backup checksum/복원 연습 결과, datastore 종류, Cilium image digest, 각 기능 검사의 결과, 전후 메모리, 남은 제한을 남긴다. 비밀 파일·token·private key·Argo export 원문은 첨부하지 않는다.

현재 이 문서의 완료 범위는 운영 절차의 소스 검토와 문서화다. **운영 CNI 전환 및 운영 복구 검증은 미실행**이다.
