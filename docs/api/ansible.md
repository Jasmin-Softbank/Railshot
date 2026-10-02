# Ansible 실행 인터페이스 사용 안내

버전 1.0 · 작성일 2026-10-02

이 문서는 상위 API에서 Ansible 작업을 호출하는 방법을 설명합니다. CLI는 `infrastructure/ansible/run.py`이며 JSON 요청을 받아 JSON 결과를 반환합니다. 운영자 전용 비동기 HTTP API는 `infrastructure/ansible/api.py`이며 localhost에서만 수신합니다. HTTP 사용법은 9절에 있습니다. 입력 검사, guest 준비, runtime 설치 완료를 구분합니다. 이 문서의 테스트는 오프라인 검증이며 실제 노드·SSM/IAP·클라우드 준비 완료를 증명하지 않습니다. 앱 적용은 별도 Argo CD 경로가 소유합니다.

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

Terraform의 `output -json node_descriptor` 결과도 변환할 수 있습니다. 예제는 실제 자원·자격이 없는 [AWS descriptor](../../examples/ansible/aws-node-descriptor.json)와 [GCP descriptor](../../examples/ansible/gcp-node-descriptor.json)입니다.

```bash
python3 infrastructure/ansible/run.py \
  --node-descriptor examples/ansible/aws-node-descriptor.json \
  --request-id aws-runtime-001 --operation runtime.install \
  --ssh-user railshot-operator \
  --identity-file /secure/railshot/identity_ed25519 \
  --known-hosts-file /secure/railshot/known_hosts \
  --state-dir /secure/railshot/ansible-jobs --validate-only
```

GCP에는 GCP descriptor 파일을 지정하면 됩니다. 변환은 `x86_64`를 `amd64`로 매핑하고 target/resource/private IPv4를 보존합니다. AWS region 또는 GCP zone을 placement로 사용하며, `transport_ref`의 instance와 실제 resource ID가 일치해야 합니다. descriptor의 `public` 주소나 runtime readiness 주장은 설치 성공으로 사용하지 않습니다. AWS는 cloud-init 완료, GCP는 preconfigured guest 및 존재하는 cloud-init의 완료를 검사합니다. `--validate-only`는 상태 디렉터리·터널·SSH에 접근하지 않습니다.

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
| `request_id` | 상위 실행·시도의 식별자이자 영속 중복 방지 키. 같은 ID와 입력은 저장한 결과만 반환합니다. |
| `operation` | 2절의 작업 이름 중 하나 |
| `target` | `id`, `provider`, `placement`, `os`, `architecture`, `initialization` |
| `target.provider` | `aws`, `gcp`, `openstack`, `onprem` 중 하나 |
| `target.placement` | 지역 또는 현장 식별자 |
| `target.os` · `target.architecture` | `linux`; architecture는 `amd64` 또는 `arm64`. 작업별 지원 범위는 2절을 따릅니다. |
| `target.initialization` | `cloud-init` 또는 `preconfigured` |
| `inventory.control_plane` · `inventory.workers` | 노드 객체 배열. 각 항목에 `id`, `resource_id`, `private_ipv4`, `ssh`를 지정합니다. |
| `ssh` | `user`, `port`, 절대 경로인 `identity_file`과 `known_hosts_file`; 선택적 `transport_ref` |
| `ssh.transport_ref` | `ssm:region:i-instance` 또는 `iap:project/zone/instance`. provider·placement·resource와 일치해야 하며 guest SSH 22번만 허용합니다. |
| `timeout_seconds` | guest와 runtime을 합한 제한 시간, 30–1800초. 각 단계에는 남은 시간만 적용합니다. |
| `patroni.placements` | provider/site별 `database_nodes`와 `dcs_voters`를 각각 지정합니다. |

`private_ipv4`에는 RFC1918 사설 IPv4만 허용합니다. 공인·loopback·link-local 주소와 중복된 node ID·resource ID·주소는 거부합니다. `control_plane`은 Ansible의 `k3s_server`, `workers`는 `k3s_workers`로 변환하지만 현재 worker 요청은 실행 전에 차단합니다.

`patroni.placements`는 DB 노드 수와 DCS 투표 노드 수를 별도로 표현합니다. 같은 provider/site의 중복 항목과 두 수가 모두 0인 항목은 거부합니다. 이 검사는 quorum이나 HA 구성의 유효성을 확인하지 않습니다. 회의에서 언급한 3+2 배치도 기본값으로 사용하지 않습니다.

상위 API는 사용자 인증과 대상 접근 권한을 확인한 뒤 서버 측 target 설정으로 요청을 만들어야 합니다. 일반 사용자가 임의 주소나 SSH 파일 경로를 지정하도록 이 도구를 그대로 공개하지 마세요. provider·placement·resource ID는 대상의 출처를 기록하는 값이며, 그 값만으로 클라우드 자원 소유권이 검증되지는 않습니다.

현재 OpenStack Controller의 `resource_id/status/addresses` 결과에는 guest 접속 정보가 없습니다. 설치 전에 대상 준비 계약으로 접속 정보를 연결해야 합니다. 실제 키·토큰·비밀번호는 요청 본문에 넣지 않습니다.

## 4. SSH와 guest 준비 확인

`transport_ref`가 없으면 실행기에서 VPN 또는 underlay 경로로 대상의 사설 주소와 SSH 포트에 도달해야 합니다. 이 도구는 VPN·라우팅·보안 그룹을 설정하거나 SSH를 공개하지 않습니다. SSH 성공은 해당 사설 경로의 접속 확인이며 VPN handshake를 별도로 검사한 결과는 아닙니다.

SSH는 지정한 개인키와 known_hosts를 사용합니다. `StrictHostKeyChecking=yes`, `IdentitiesOnly=yes`, `IdentityAgent=none`, `-F /dev/null`을 적용하고 ProxyCommand·ProxyJump·agent forwarding·암호 및 대화식 인증을 차단합니다. 서버 host key는 신뢰할 수 있는 경로로 미리 확인해 등록하세요. 최초 접속에서 받은 키를 자동으로 신뢰하는 방식은 사용하지 않습니다.

SSM은 `aws` CLI와 `session-manager-plugin`, IAP는 `gcloud`가 실행기에 필요합니다. 실행기는 검증된 값으로만 AWS `AWS-StartPortForwardingSession` 또는 `gcloud compute start-iap-tunnel`의 argv를 구성합니다. 임시 localhost 포트가 열린 뒤 Ansible이 접속하며 `HostKeyAlias=사설IPv4`로 원래 호스트 키를 확인합니다. known_hosts에는 원래 사설 IPv4의 키를 신뢰된 별도 경로로 등록해야 합니다. 클라우드 CLI 로그인·IAP/SSM 권한·대상 SSH 공개키 설치는 선행조건입니다. GCP OS Login을 쓰면 등록된 OS Login 사용자와 키를 지정하고, 모듈의 operator 키 방식을 쓰면 `railshot-operator`를 지정합니다.

터널만 클라우드 CLI 환경을 상속합니다. Ansible과 guest에는 클라우드 토큰을 전달하지 않으며 ProxyCommand는 계속 차단합니다. 터널 준비 시간은 전체 deadline 안에서 최대 60초이고, 완료·실패 시 로컬 터널 프로세스 그룹을 종료합니다. SSH timeout이 원격 설치 종료까지 보장하지는 않습니다.

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

inventory와 변수 파일은 비공개 임시 파일로 만들고 실행 후 제거합니다. 임의 Ansible 환경 설정·클라우드 자격·SSH agent를 Ansible 자식 프로세스에 전달하지 않으며 Ansible의 원문 출력도 API 결과에 포함하지 않습니다. 패키지·release·registry에 대한 outbound 접근과 LAN/VPC/VPN 대비 Pod/Service CIDR 충돌은 운영자가 확인해야 합니다. 현재 runtime CIDR은 팀의 고정 값을 사용합니다.

## 5. 설치 흐름과 파일 구성

`runtime.install`은 **JB guest 검사 → 승민 K3s 설치 → Cilium 설치 → 준비 상태 확인** 순서로 실행합니다. JB의 기존 `site.yml`에 있는 1.37/Flannel 설치는 이 경로에서 호출하지 않습니다.

| 파일 | 역할 |
|---|---|
| `infrastructure/ansible/run.py` | JSON·descriptor 검사, 영속 요청 기록, target/resource 잠금, 플레이북 호출과 receipt 검증 |
| `infrastructure/ansible/transport.py` | native SSM/IAP SSH 터널 준비·종료 |
| `infrastructure/ansible/guest.yml` | guest 조건 확인과 준비 확인 기록 작성 |
| `infrastructure/ansible/tasks/guest-checks.yml` | JB 원본 `90b5196f…`에서 추출한 첫 guest 검사. 기존 `site.yml`과 공유합니다. |
| `infrastructure/ansible/runtime.yml` | 대상 identity 확인, runtime 스크립트 전달·실행, 실제 노드 상태 검사 |
| `deployment/bootstrap/{preflight,install-k3s,health}.sh` | 최신 팀 runtime의 guest 선행조건·K3s 설치·API health 검사 |
| `deployment/cilium/{install,health}.sh` | Cilium 설치 및 Cilium·Node·CoreDNS 준비 확인 |

최신 `deployment/scripts/runtime.py deploy`는 bootstrap과 앱을 함께 실행하는 별도 진입점입니다. 이 어댑터는 앱 CLI나 Argo Application을 적용하지 않습니다. runtime 준비 뒤 앱 적용은 Argo CD가 단독 소유합니다.

control-plane ID는 inventory·receipt 식별자이며 Kubernetes hostname을 강제로 변경하지 않습니다. 사설 IPv4는 `NODE_IP`로 전달하고 실제 단일 노드의 InternalIP·architecture·version·Ready를 대조합니다. target·resource·node·주소·architecture·버전과 profile은 비공개 소유권 기록에 저장합니다. 기존 K3s가 있는데 이 기록이 없으면 사용을 거부하고, 기록이 있으면 요청과 같은 identity인지 비교합니다. 기존 K3s 설정도 설치기가 다시 비교합니다. 이전 어댑터 profile 또는 예전 runtime 관리표식은 자동 승계하지 않습니다. 최신 팀 bootstrap 관리표식과 맞는 새로운 `team-bootstrap-json-0.1` profile을 사용합니다. 자동 cluster adoption·업그레이드·CIDR 변경·worker join은 지원하지 않습니다.

runtime은 kube-proxy를 유지하고 Flannel·내장 network-policy·Traefik·ServiceLB·metrics-server·local-storage를 비활성화하는 기존 승민 구성을 사용합니다. 원격 bootstrap 전체는 팀 CLI와 같은 `/run/railshot-deployment.lock`을 사용합니다. 로컬 실행기는 `--state-dir`(기본 `~/.local/state/railshot/ansible`)에 입력의 SHA-256과 검증한 단계별 receipt·최종 결과만 저장합니다. 디렉터리는 실행기 소유 0700, 기록은 0600이며 요청 본문·키 내용·원문 로그를 저장하지 않습니다. 같은 실행기의 target ID와 resource ID를 각각 잠가 동시 요청을 차단합니다.

동일 request ID와 동일 입력은 `replayed=true`와 저장된 결과를 반환하며 SSH를 다시 실행하지 않습니다. 이것은 과거 실행 결과 조회이고 현재 readiness 재검사가 아닙니다. 같은 ID의 입력 변경은 `REQUEST_ID_CONFLICT`, 동시 target 사용은 `TARGET_BUSY`입니다. 시작 기록만 남은 중단 작업은 `PREVIOUS_OUTCOME_UNKNOWN`으로 차단합니다. 운영자가 실제 상태를 확인한 후 새로운 request ID로 재개해야 합니다. 잠금·중복 방지는 같은 state 디렉터리를 공유하는 실행기에 적용되므로 API 인스턴스도 동일 디렉터리를 사용해야 합니다.

## 6. 결과와 완료 확인

| status | 종료 코드 | 의미 |
|---|---|---|
| `validated` | 0 | 검사 전용 요청의 형식과 지원 범위 통과. 모든 readiness는 false입니다. |
| `succeeded` | 0 | 요청한 작업의 실제 준비 확인 기록 검증 완료 |
| `invalid` | 2 | JSON·알 수 없는 필드·주소·중복·범위 오류. 실행하지 않습니다. |
| `blocked` | 3 | 작업·노드 구성·architecture 미지원, SSH 참조·터널·작업 기록 또는 Ansible 부재. 새 원격 작업을 실행하지 않습니다. |
| `failed` | 4 | guest/runtime 실행, 제한 시간, 준비 확인 기록 검증 또는 실행 후 영속 기록 실패 |

결과는 `schema_version`, `request_id`, `target_id`, `operation`, `status`, `stage`, `steps`, `replayed`, 네 가지 readiness 필드와 `error`를 포함합니다. `steps`에는 단계별 `stage`, `exit_code`와 성공 시 검증한 `receipt`의 허용 필드를 기록합니다. 입력 검증에 실패하면 검증되지 않은 요청 식별자를 그대로 반환하지 않습니다.

플레이북은 검사가 모두 끝난 뒤 request·target·node·stage·실행 nonce를 결합한 비공개 확인 기록(receipt)을 작성합니다. 어댑터는 이를 현재 요청과 대조합니다. **종료 코드가 0이어도 일치하는 확인 기록이 없으면 성공으로 처리하지 않습니다.**

| 결과 필드 | 확인하는 상태 |
|---|---|
| `guest_ready` | 4절의 실제 guest 검사 통과 |
| `runtime_ready` | 단일 노드 수·사설 InternalIP·architecture·kubelet 버전·Ready 및 Cilium/Operator/CoreDNS rollout 확인 |
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
| stdlib unittest 25개 | 통과. 기존 receipt·guest/runtime 실패 경계, descriptor 변환·transport/resource 불일치·native argv·터널 종료·영속 중복 방지·잠금·중단 작업 차단을 확인했습니다. HTTP 접수→상태 조회, 인증·등록 target 제한, 재시작 후 증거 대조, 실제 CLI의 SSH 실행 전 차단도 포함합니다. |
| `guest.yml`, `runtime.yml`, 기존 `site.yml` | Ansible syntax-check 통과 |
| 현재 bootstrap/Cilium shell | 6개 `bash -n` 통과 |
| locale | 초기 syntax-check의 호스트 locale 오류를 UTF-8 locale 명시로 해결. 어댑터도 자식 프로세스에 플랫폼별 UTF-8 locale을 지정 |

테스트는 다음 명령으로 실행합니다.

```bash
python3 -m unittest discover -s infrastructure/ansible -p 'test_*.py' -v
```

테스트의 receipt는 로컬 fixture이며 실제 준비 완료의 증거로 사용할 수 없습니다. 오프라인 테스트는 SSH·SSM/IAP·guest·K3s·Cilium·Patroni·클라우드를 실행하지 않습니다. 실제 대상에서 초기 설치·재실행·실패 복구와 사설 통신을 별도로 확인해야 합니다.

이 문서는 10/1 허들 2:49:20–2:51:07의 Ansible 변수 전달·CSP/온프레 배치 요청과 지환의 명세 작성 과제를 기준으로 작성했습니다. 상세 근거는 [회의·Notion·PDF 대조 기록](../research/openstack-ansible-evidence.md)에 있습니다. Patroni의 배치 예시와 아직 정하지 않은 quorum·HA 구성을 확정 계약으로 취급하지 않습니다.


## 9. 운영자 전용 비동기 HTTP API

이 HTTP 구현은 Ansible 준비 작업만 접수합니다. `apps/api`의 사용자 API와 아직 연결하지 않았으며, 외부 ALB에 공개하거나 사용자 로그인·tenant 권한을 대신 처리하지 않습니다. Terraform 생성과 Argo 적용도 여기서 시작하지 않습니다. 상위 제어 서비스는 Terraform이 완료한 descriptor를 운영자 target에 등록한 뒤 이 API를 호출할 수 있습니다.

운영자는 [등록 파일 예제](../../examples/ansible/api-targets.json)를 바탕으로 private 파일을 준비합니다. 등록 파일은 `version: 1`과 `targets` 맵을 가지며 각 target에는 `descriptor_file`, `ssh`, 선택적 `timeout_seconds`를 지정합니다. descriptor 파일과 SSH 접속 정보는 서버가 해석합니다. 등록 파일·descriptor·Bearer 파일은 실행기 소유이며 group/other 권한이 없어야 합니다. Bearer 파일은 32–512자의 무작위 ASCII 토큰을 담은 0600 파일로 준비합니다. 토큰·키 내용은 Git이나 요청 본문에 넣지 않습니다.

```bash
python3 infrastructure/ansible/api.py \
  --targets-file /secure/railshot/ansible-targets.json \
  --token-file /secure/railshot/ansible-api-token \
  --state-dir /secure/railshot/ansible-jobs \
  --listen 127.0.0.1 --port 4180
```

`--listen`은 `127.0.0.1`만 허용합니다. 모든 HTTP 요청은 `Authorization: Bearer …`로 인증하며, 값은 constant-time 방식으로 대조합니다. Host는 실제 포트의 `127.0.0.1` 또는 `localhost`, Origin은 없거나 동일 HTTP origin이어야 합니다. 임의 Proxy/Forwarded 헤더를 신뢰하지 않습니다. 같은 상태 디렉터리에서는 API 프로세스를 하나만 실행할 수 있습니다.

| 요청 | 결과 |
|---|---|
| `POST /v1/ansible/jobs` | 정확히 `request_id`, `target_id`, `operation` 세 필드만 받음. 신규 접수는 영속 기록 후 202와 `Location` 반환 |
| `GET /v1/ansible/jobs/{request_id}` | 저장한 작업 상태와 검증한 readiness 조회. 설치를 새로 실행하지 않음 |

접수 본문 예시입니다.

```json
{"request_id":"aws-runtime-001","target_id":"demo-aws","operation":"runtime.install"}
```

operation은 `guest.check` 또는 `runtime.install`만 허용합니다. 등록된 AWS/GCP target만 사용할 수 있으며 `ssh`, `command`, `credentials`, 경로, Terraform 변수, `validate_only` 같은 추가 필드는 거부합니다. 토큰을 명령행 인자에 노출하지 않는 Python 호출 예시는 다음과 같습니다.

```python
import json
from pathlib import Path
from urllib.request import Request, urlopen

base = 'http://127.0.0.1:4180'
headers = {'Authorization': 'Bearer ' + Path('/secure/railshot/ansible-api-token').read_text().strip(),
           'Content-Type': 'application/json'}
body = {'request_id': 'aws-runtime-001', 'target_id': 'demo-aws', 'operation': 'runtime.install'}
with urlopen(Request(base + '/v1/ansible/jobs', data=json.dumps(body).encode(), headers=headers)) as response:
    job = json.load(response)
with urlopen(Request(base + '/v1/ansible/jobs/' + job['request_id'], headers=headers)) as response:
    print(json.load(response))
```

한 번에 한 작업만 접수·실행합니다. 별도 무제한 대기열은 없으며 실행 중 다른 작업은 `409 EXECUTOR_BUSY`입니다. 같은 request ID와 같은 서버 측 입력은 현재 또는 저장된 결과를 반환합니다. 입력이 달라지면 `409 REQUEST_ID_CONFLICT`입니다. 같은 target의 미확정 작업이 남아 있으면 `409 TARGET_RECONCILE_REQUIRED`로 추가 변경을 막습니다.

상태는 `queued`, `running`, `succeeded`, `failed`, `blocked`, `unknown`입니다. `succeeded`는 guest/runtime receipt 검증 완료이고 앱 배포 성공이 아닙니다. `result.application_ready`와 `result.public_http_verified`는 항상 false입니다. 인증 실패는 401, Host/Origin 거부는 403, 미등록 target·없는 작업은 404, 입력 오류는 400, 서버 측 등록·상태 저장 문제는 503으로 반환합니다. 접수한 작업이 나중에 실패해도 상태 조회 자체는 200이며 본문의 상태와 error를 확인해야 합니다.

HTTP 작업 기록은 같은 상태 디렉터리의 `http-jobs/`에 저장하고 기존 `run.py`가 target/resource 잠금과 nonce receipt를 관리합니다. HTTP 오류는 코드만 반환하며 원문 예외·SSH 경로·credential·native stdout/stderr는 내보내지 않습니다. subprocess의 stderr는 운영자 전용 0600 로그에만 남깁니다. 기존 `storage.durable_write`를 사용하여 접수와 상태 변경을 영속화합니다.

API가 중단되면 이전 queued/running 작업을 자동 실행하지 않습니다. 시작 시 또는 GET 조회 시 입력 해시와 일치하는 native 완료 기록이 있으면 그 결과로 상태를 복구하고, 없으면 `unknown`으로 남깁니다. 살아남은 CLI가 나중에 완료 기록을 쓰면 이후 GET에서 대조할 수 있습니다. native 완료 증거가 없는 unknown은 운영자 확인이 필요하며 HTTP 강제 초기화·자동 재시도 기능은 제공하지 않습니다. 이 API의 정상 종료는 진행 중 worker가 끝날 때까지 기다립니다. 강제 종료나 timeout은 원격 설치 중단을 보장하지 않습니다.
