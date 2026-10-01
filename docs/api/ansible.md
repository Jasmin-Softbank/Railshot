# Ansible 실행 인터페이스 사용 안내

버전 1.0 · 작성일 2026-10-02

이 문서는 상위 API에서 Ansible 작업을 호출하는 방법을 설명합니다. 호출 도구는 `infrastructure/ansible/run.py`이며 JSON 요청을 받아 JSON 결과를 반환합니다. 별도 API 서버는 실행하지 않습니다. 입력 검사, guest 준비, runtime 설치 완료를 구분합니다. 실제 노드·VPN·클라우드 배포 검증은 아직 수행하지 않았습니다.

## 1. 실행 환경과 호출

신뢰된 운영 실행기에서 사용합니다. 실행기에 Python 3과 `ansible-playbook`이 필요하며, 대상 서버에는 Python 3과 비대화식 sudo 권한이 있어야 합니다. runtime 설치에는 curl·tar·systemd 등 기존 설치 스크립트의 선행조건도 필요합니다.

저장소 루트에서 다음 명령을 실행합니다. 아래 명령은 입력만 검사하며 SSH 파일이나 대상 노드에 접근하지 않습니다.

```bash
python3 infrastructure/ansible/run.py --request examples/ansible/guest-check.json --validate-only
python3 infrastructure/ansible/run.py --request examples/ansible/runtime-single-node.json --validate-only
```

표준 입력으로도 요청을 전달할 수 있습니다.

```bash
python3 infrastructure/ansible/run.py --request - --validate-only < examples/ansible/runtime-single-node.json
```

결과는 표준 출력(stdout)에 JSON 객체 하나로 반환합니다. `--validate-only`의 `validated`는 입력과 지원 범위를 확인했다는 뜻이며 준비·설치 성공을 보장하지 않습니다. 실제 실행은 대상과 자격 참조를 확인한 뒤 이 옵션을 제거하면 시작됩니다. 예제의 VM ID·주소·파일 경로는 실제 접속 정보로 바꾸어야 합니다.

## 2. 작업 목록과 공통 규칙

| operation | 실행 내용 | 현재 지원 범위 |
|---|---|---|
| `guest.check` | JB 공통 검사와 architecture·주소·디스크·cloud-init 상태 확인 | control-plane 1개, worker 없음 |
| `runtime.install` | guest 확인 후 승민의 K3s/Cilium runtime 설치 | amd64, Ubuntu 22.04/24.04, K3s 1.34.11+k3s1, Cilium 1.20.2 |
| `patroni.install` | 환경별 배치 입력 검사 | 담당자 플레이북이 없어 `blocked` 반환 |

control-plane이 여러 개이거나 worker가 있으면 `SINGLE_NODE_ONLY`로 차단합니다. 요청한 노드를 생략하거나 단일 노드로 줄여 실행하지 않습니다. `guest.check`는 amd64/arm64 요청을 실제 서버 정보와 비교할 수 있지만, 통합 runtime의 ARM64 지원은 아직 검증하지 않았습니다.

호출자는 playbook 경로·shell 명령·임의 extra-vars를 지정할 수 없습니다. `runtime.install`에는 앱·CD·DB 설치가 포함되지 않습니다. Patroni 작업은 `PATRONI_PLAYBOOK_UNAVAILABLE` 오류를 반환하며 설치를 시작하지 않습니다.

## 3. 요청 입력

요청 형식은 [ansible-request.schema.json](../../contracts/ansible-request.schema.json)을 따릅니다. [runtime 요청](../../examples/ansible/runtime-single-node.json), [guest 확인 요청](../../examples/ansible/guest-check.json), [Patroni 배치 요청](../../examples/ansible/patroni-placement-blocked.json) 예제를 사용할 수 있습니다.

| 필드 | 형식·규칙 |
|---|---|
| `schema_version` | `1.0` |
| `request_id` | 상위 실행·시도의 식별자. 중복 실행 방지 키는 아닙니다. |
| `operation` | 2절의 작업 이름 중 하나 |
| `target` | `id`, `provider`, `placement`, `os`, `architecture`, `initialization` |
| `target.provider` | `aws`, `gcp`, `openstack`, `onprem` 중 하나 |
| `target.placement` | 지역 또는 현장 식별자 |
| `target.os` · `target.architecture` | `linux`; architecture는 `amd64` 또는 `arm64`. 작업별 지원 범위는 2절을 따릅니다. |
| `target.initialization` | `cloud-init` 또는 `preconfigured` |
| `inventory.control_plane` · `inventory.workers` | 노드 객체 배열. 각 항목에 `id`, `resource_id`, `private_ipv4`, `ssh`를 지정합니다. |
| `ssh` | `user`, `port`, 절대 경로인 `identity_file`과 `known_hosts_file` |
| `timeout_seconds` | guest와 runtime을 합한 제한 시간, 30–1800초. 각 단계에는 남은 시간만 적용합니다. |
| `patroni.placements` | provider/site별 `database_nodes`와 `dcs_voters`를 각각 지정합니다. |

`private_ipv4`에는 RFC1918 사설 IPv4만 허용합니다. 공인·loopback·link-local 주소와 중복된 node ID·resource ID·주소는 거부합니다. `control_plane`은 Ansible의 `k3s_server`, `workers`는 `k3s_workers`로 변환하지만 현재 worker 요청은 실행 전에 차단합니다.

`patroni.placements`는 DB 노드 수와 DCS 투표 노드 수를 별도로 표현합니다. 같은 provider/site의 중복 항목과 두 수가 모두 0인 항목은 거부합니다. 이 검사는 quorum이나 HA 구성의 유효성을 확인하지 않습니다. 회의에서 언급한 3+2 배치도 기본값으로 사용하지 않습니다.

상위 API는 사용자 인증과 대상 접근 권한을 확인한 뒤 서버 측 target 설정으로 요청을 만들어야 합니다. 일반 사용자가 임의 주소나 SSH 파일 경로를 지정하도록 이 도구를 그대로 공개하지 마세요. provider·placement·resource ID는 대상의 출처를 기록하는 값이며, 그 값만으로 클라우드 자원 소유권이 검증되지는 않습니다.

현재 OpenStack Controller의 `resource_id/status/addresses` 결과에는 guest 접속 정보가 없습니다. 설치 전에 대상 준비 계약으로 접속 정보를 연결해야 합니다. 실제 키·토큰·비밀번호는 요청 본문에 넣지 않습니다.

## 4. SSH와 guest 준비 확인

실행기에서 VPN 또는 underlay 경로를 통해 대상의 사설 주소와 SSH 포트에 도달할 수 있어야 합니다. 이 도구는 VPN·라우팅·보안 그룹을 설정하거나 SSH를 공개하지 않습니다. SSH 성공은 해당 사설 경로의 접속 확인이며 VPN handshake를 별도로 검사한 결과는 아닙니다.

SSH는 지정한 개인키와 known_hosts를 사용합니다. `StrictHostKeyChecking=yes`, `IdentitiesOnly=yes`, `IdentityAgent=none`, `-F /dev/null`을 적용하고 ProxyCommand·ProxyJump·agent forwarding·암호 및 대화식 인증을 차단합니다. 서버 host key는 신뢰할 수 있는 경로로 미리 확인해 등록하세요. 최초 접속에서 받은 키를 자동으로 신뢰하는 방식은 사용하지 않습니다.

참조 파일은 비어 있지 않은 실행기 소유의 일반 파일이어야 하며 파일 자체의 symbolic link는 거부합니다. 개인키에는 group/other 권한을 허용하지 않고, known_hosts에는 group/other 쓰기 권한을 허용하지 않습니다. 경로는 공백·셸 메타문자가 없는 절대 경로로 제한합니다.

준비 확인에는 다음 검사를 수행합니다.

| 검사 | 통과 조건 |
|---|---|
| 접속·권한 | SSH를 통한 Python fact 수집과 비대화식 sudo 성공 |
| JB 공통 조건 | Ubuntu, systemd, CPU 2개 이상, 메모리 1800MiB 이상, swap 비활성 |
| 요청과 서버 일치 | 요청한 architecture와 NIC의 사설 IPv4 일치 |
| 디스크 | root 파일시스템 여유 공간 10GiB 이상 |
| 초기 설정 | `cloud-init` 요청이면 도구가 존재하고 상태가 `done`. `preconfigured`도 cloud-init이 존재하면 `done` 확인 |

cloud-init을 시작하거나 재실행하지는 않습니다. 진행 중·실패·필수 도구 부재는 준비 확인 실패로 처리합니다. **VM의 ACTIVE는 guest 준비 완료를 뜻하지 않습니다.**

inventory와 변수 파일은 비공개 임시 파일로 만들고 실행 후 제거합니다. 임의 Ansible 환경 설정·클라우드 자격·SSH agent를 자식 프로세스에 전달하지 않으며 Ansible의 원문 출력도 API 결과에 포함하지 않습니다. 패키지·release·registry에 대한 outbound 접근과 LAN/VPC/VPN 대비 Pod/Service CIDR 충돌은 운영자가 확인해야 합니다. 현재 runtime CIDR은 팀의 고정 값을 사용합니다.

## 5. 설치 흐름과 파일 구성

`runtime.install`은 **JB guest 검사 → 승민 K3s 설치 → Cilium 설치 → 준비 상태 확인** 순서로 실행합니다. JB의 기존 `site.yml`에 있는 1.37/Flannel 설치는 이 경로에서 호출하지 않습니다.

| 파일 | 역할 |
|---|---|
| `infrastructure/ansible/run.py` | JSON 입력 검사, 허용된 플레이북 호출, 제한 시간과 결과 처리 |
| `infrastructure/ansible/guest.yml` | guest 조건 확인과 준비 확인 기록 작성 |
| `infrastructure/ansible/tasks/guest-checks.yml` | JB 원본 `90b5196f…`에서 추출한 첫 guest 검사. 기존 `site.yml`과 공유합니다. |
| `infrastructure/ansible/runtime.yml` | 대상 identity 확인, runtime 스크립트 전달·실행, 실제 노드 상태 검사 |
| `deployment/runtime.sh` | 승민의 `install-k3s`와 `install-cilium` 두 단계만 실행 |
| `deployment/scripts/preflight.sh` | 승민 원본 `fb503fd6…`의 설치 전 검사. 기존 `install.sh`와 `runtime.sh`가 공유합니다. |

기존 `deployment/install.sh`는 샘플 앱까지 설치하는 별도 진입점으로 유지합니다. 어댑터는 이 명령을 호출하지 않으며 CD/GitOps 배포도 별도 경로입니다.

control-plane ID는 `K3S_NODE_NAME`, 사설 IPv4는 `NODE_IP`로 전달합니다. target·resource·node·주소·architecture·버전과 profile은 비공개 소유권 기록에 저장합니다. 기존 K3s가 있는데 이 기록이 없으면 사용을 거부하고, 기록이 있으면 요청과 같은 identity인지 비교합니다. 기존 K3s 설정도 설치기가 다시 비교합니다. 자동 cluster adoption·업그레이드·CIDR 변경·worker join은 지원하지 않습니다.

runtime은 kube-proxy를 유지하고 Flannel·내장 network-policy·Traefik·ServiceLB·metrics-server·local-storage를 비활성화하는 기존 승민 구성을 사용합니다. 같은 노드의 설치 중복 실행은 원격 스크립트의 `flock`으로 막습니다. 다만 API의 영구 작업 이력이나 중복 요청 방지를 제공하는 것은 아니므로, 호출자는 request/target별 실행을 조율해야 합니다.

## 6. 결과와 완료 확인

| status | 종료 코드 | 의미 |
|---|---|---|
| `validated` | 0 | 검사 전용 요청의 형식과 지원 범위 통과. 모든 readiness는 false입니다. |
| `succeeded` | 0 | 요청한 작업의 실제 준비 확인 기록 검증 완료 |
| `invalid` | 2 | JSON·알 수 없는 필드·주소·중복·범위 오류. 실행하지 않습니다. |
| `blocked` | 3 | 작업·노드 구성·architecture 미지원, SSH 참조 또는 Ansible 부재. 실행하지 않습니다. |
| `failed` | 4 | guest/runtime 실행, 제한 시간 또는 준비 확인 기록 검증 실패 |

결과는 `schema_version`, `request_id`, `target_id`, `operation`, `status`, `stage`, `steps`, 네 가지 readiness 필드와 `error`를 포함합니다. `steps`에는 단계별 `stage`와 `exit_code`를 기록합니다. 입력 검증에 실패하면 검증되지 않은 요청 식별자를 그대로 반환하지 않습니다.

플레이북은 검사가 모두 끝난 뒤 request·target·node·stage·실행 nonce를 결합한 비공개 확인 기록(receipt)을 작성합니다. 어댑터는 이를 현재 요청과 대조합니다. **종료 코드가 0이어도 일치하는 확인 기록이 없으면 성공으로 처리하지 않습니다.**

| 결과 필드 | 확인하는 상태 |
|---|---|
| `guest_ready` | 4절의 실제 guest 검사 통과 |
| `runtime_ready` | 실제 node 이름·단일 노드 수·kubelet 버전·Ready 및 Cilium/Operator/CoreDNS rollout 확인 |
| `application_ready` | 이 도구에서 검사하지 않으므로 false |
| `public_http_verified` | 이 도구에서 검사하지 않으므로 false |

runtime 성공은 앱 이미지 pull·CD revision·SQL·공개 HTTP 성공을 뜻하지 않습니다. kubeconfig나 cluster-admin 자격도 로컬로 내보내지 않습니다.

## 7. 오류와 재시도

`error`에는 `code`, 안전한 `message`, `retryable`, `outcome_unknown`이 포함됩니다. 모든 오류의 `retryable`은 false이며 자동 재시도·롤백·VM 삭제를 수행하지 않습니다.

runtime 실행 실패·시간 초과·확인 기록 부재에서는 일부 원격 변경이 남을 수 있어 `outcome_unknown=true`를 반환합니다. 먼저 대상의 현재 상태를 확인하세요. guest 검사까지 성공했다면 `guest_ready=true`는 유지하지만 `runtime_ready`는 false입니다. 오류 응답에는 원문 실행 로그나 관리 자격을 포함하지 않습니다.

## 8. 검증 범위와 작성 근거

다음은 이번 로컬 검증 결과입니다.

| 검증 | 결과 |
|---|---|
| stdlib unittest 16개 | 통과. 검사 전용 비실행, 임의 playbook·주입 거부, 주소·중복·키 권한, SSH identity·host key, 무증거 성공 거부, receipt 불일치, 단계별 실패·timeout, Patroni·worker 차단, 실제 로컬 프로세스 timeout 확인 |
| `guest.yml`, `runtime.yml`, 기존 `site.yml` | Ansible syntax-check 통과 |
| 기존·신규 deployment shell | `bash -n` 통과 |
| locale | 초기 syntax-check의 호스트 locale 오류를 UTF-8 locale 명시로 해결. 어댑터도 자식 프로세스에 플랫폼별 UTF-8 locale을 지정 |

테스트는 다음 명령으로 실행합니다.

```bash
python3 -m unittest discover -s infrastructure/ansible -p 'test_run.py' -v
```

테스트의 receipt는 로컬 fixture이며 실제 준비 완료의 증거로 사용할 수 없습니다. 이번 검증에서 SSH·VPN·guest·K3s·Cilium·Patroni·클라우드를 실행하지 않았습니다. 실제 대상에서 초기 설치·재실행·실패 복구와 사설 통신을 별도로 확인해야 합니다.

이 문서는 10/1 허들 2:49:20–2:51:07의 Ansible 변수 전달·CSP/온프레 배치 요청과 지환의 명세 작성 과제를 기준으로 작성했습니다. 상세 근거는 [회의·Notion·PDF 대조 기록](../research/openstack-ansible-evidence.md)에 있습니다. Patroni의 배치 예시와 아직 정하지 않은 quorum·HA 구성을 확정 계약으로 취급하지 않습니다.
