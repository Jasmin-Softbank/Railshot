# 개인 OpenStack 환경 호출 규격

이 문서는 `apps/api/src/personal-environments.js`와 HTTP 계약 테스트의 구현 규격입니다. 실제 고객 OpenStack 설치·배포·삭제 인수 결과를 의미하지 않습니다.

## 소유권

외부 로그인은 추가하지 않습니다. `POST /api/v1/owners {}`는 장기 소유자 쿠키와 256비트 복구키를 발급합니다. `railshot_owner` 쿠키는 HttpOnly, SameSite=Strict, 1년 보관이며 원격 모드에서는 Secure입니다. 서버에는 쿠키·복구키 해시만 저장합니다. 기존 7일 익명 세션과 별도 소유자용 세션을 사용하므로 익명 세션 만료로 개인 환경 소유권이 없어지지 않습니다. `GET /owners`는 `{id,recovery_configured}`만 반환합니다. 이미 소유권 쿠키가 있는 `POST /owners`는 새 복구키를 만들지 않습니다.

`POST /api/v1/recoveries {recovery_key}`는 소유권 쿠키와 복구키를 모두 회전합니다. 이전 쿠키·복구키는 즉시 무효입니다. 새로운 복구키는 응답 한 번만 표시합니다. 복구키를 잃고 쿠키도 없으면 자동 복구할 수 없습니다. 로그인 계정 복구나 사용자 신원 확인 기능이 아닙니다. 원문을 브라우저 영구 저장소·서버 로그에 저장하지 않습니다.

개인 환경 변경 HTTP 요청은 `X-Railshot-Request: dashboard`가 필요하며 기존 Host/Origin 검사를 유지합니다. 인증되지 않은 환경/서비스 조회는 404입니다. 운영자 공용 대상을 방문·등록자격 요청·환경 생성 입력으로 개인 소유로 전환할 수 없습니다. 클라이언트용 claims/heartbeats/receipts는 각각의 Bearer 자격을 검사하며 브라우저 쿠키 및 내부 API 공용 Bearer로 대체하지 않습니다.

## 환경과 설치

- `GET /api/v1/readiness?scope=personal` → `{scope:"personal",ready,verification_scope:"configuration_only",message,blockers}`. 인증·계정·VM 변경 전에 공개 설치기가 호출합니다. 운영 파일, 공통 빌드 연결, pull secret, DNS token, edge SSH 키, tunnel credential/CA와 실행환경별 worker 등록 기능의 로컬 구성을 검사합니다. 외부 자격의 실제 유효성 및 클라우드 연결은 등록 과정에서 별도로 검증합니다.
- `POST /api/v1/targets {label,provider:"openstack"}` → 201 환경 객체.
- `GET /api/v1/targets?scope=owned&provider=openstack` → `{items,next_marker}`. 삭제 완료 대상은 목록에서 제외합니다. `limit`, `marker` 지원.
- `GET /api/v1/targets/{id}` → 개인 환경 객체.
- `POST /api/v1/targets/{id}/enrollments {}` → 201 `{id,target_id,expires_at,install_command}`. 15분 유효, 일회용. 재발급 시 이전 자격 폐기. 이미 등록된 호스트의 재인수는 차단.
- `POST /api/v1/enrollments/{id}/claims`, Bearer 등록자격, `{public_key,client_version,project_id,runtime:{ssh_host_key}}` → 201 `{target_id,generation,client_token,tunnel,runtime_access,heartbeat_url}`.
- `POST /api/v1/targets/{id}/heartbeats`, Bearer 클라이언트자격, `{generation,client_version,checks:{tunnel,openstack,runtime?},project_id?,capabilities?,metrics?}` → `{target_id,generation,status,connection_status,deployable,runtime_preparation,command}`.

등록 자격은 게이트웨이 설정 이전에 사용 처리합니다. 외부 설정 결과가 불확실하면 `attention`을 보존하며 같은 등록자격을 재사용하지 않습니다. 등록 성공의 지속 자격은 별도로 생성합니다. 서버는 게이트웨이 연결과 호스트 키를 고정한 고객 측 OpenStack 명령 실행을 확인합니다. `server list` 명령이 성공해야 `connection_status=ready`이며 클라이언트 자체 보고만으로 연결 완료를 표시하지 않습니다. 설치기는 OpenStack 내부의 명시적으로 선택하거나 생성한 별도 VM에서 정상 K3s를 재사용하거나 기존 설치 기능을 실행합니다. 관리 호스트에 K3s를 설치하지 않습니다. 최종 `status=ready`와 `deployable=true`는 서버의 실제 클러스터 건강·제한된 배포 권한·동적 앱 배포 연결 확인 후에만 가능합니다. 시험용 앱을 배포하지 않습니다.

환경 객체 주요 필드는 `id,label,provider,scope,status,deployable,generation,last_seen_at,client_version,project_id,application_count,registration_stage,checks,blockers,deletion_operation_id`입니다. 상태는 `pending`, `installing`, `connecting`, `preparing`, `ready`, `offline`, `deleting`, `attention`, `deleted`입니다. 90초 이상 상태 보고가 없으면 연결을 `offline`으로 반환합니다. 실행환경 검증은 180초 동안만 유효합니다. 서버 재시작 시 연결과 기존 성공 바인딩을 읽기 전용으로 다시 검증해야 합니다. 진행 중 중단된 등록은 `unknown`으로 보존합니다.

`GET /targets/{id}/applications`는 해당 소유자가 RailShot으로 배포한 앱만 반환합니다. `GET /targets/{id}/observations`의 CPU·메모리·디스크·네트워크 값은 **관리 클라이언트 호스트 한 대**의 측정값이며 `scope=client_host`, `measured_scope=client_host`로 표시합니다. 프로젝트 전체 합계가 아닙니다. 미수집은 `not_configured` 및 null이며, 오래된 표본은 `stale`입니다.

배포 multipart 입력은 기존 `environment=onprem`, `provider=openstack`에 `target_id=<개인환경ID>`를 추가합니다. `deployment_selection`이라는 multipart JSON 필드를 추가하지 않습니다. 서버 내부에서 `deployment_selection:{environment,provider,target_id}`로 정규화하여 소유권·연결·삭제상태를 검증합니다. 직접 앱 대상 지정·업데이트·재시작에도 같은 개인 환경 검사를 적용합니다.

## 환경 삭제

1. `POST /targets/{id}/plans {action:"delete",delete_data:true}`는 앱별 기존 삭제 실행기로 계획을 생성합니다. `resources`, `retained`, `blockers`, `plan_hash`, 10분의 `expires_at`를 반환합니다. 다른 소유자의 앱, 진행/결과불명 작업, 연결 끊김, 미확인 상태가 있으면 차단합니다.
2. 사용자가 RailShot 서비스 및 앱 전용 데이터 삭제를 확인한 뒤 `POST /targets/{id}/operations {action:"delete",delete_data:true,plan_id,plan_hash,confirmation:<환경이름>}`와 `Idempotency-Key`를 보냅니다. 접수는 202 작업 객체와 `Location`, `Retry-After:2`를 반환합니다. 같은 입력·키는 기존 작업을 반환합니다.
3. 영속 `deleting` 상태를 먼저 저장합니다. 기존 앱 삭제 실행기로 앱과 해당 앱의 배포 연결을 순서대로 제거·확인합니다. 그다음 이 등록에서 만든 전용 ServiceAccount·Role·RoleBinding·이미지 인증 Secret, 중앙 Argo 프로젝트·클러스터 인증·갱신 정책의 해당 항목만 UID와 소유권을 확인하여 회수합니다. 고객 임의 자원이 들어갈 수 있는 네임스페이스 자체는 보존합니다. 권한 회수 실패 시 클라이언트 제거를 보내지 않고 `attention`을 유지합니다. 회수 성공 이후 상태 보고 응답에 `{id,operation_id,attempt_id,kind:"environment.delete",applications:[],delete_data:true,generation}` 명령을 보냅니다.
4. 클라이언트는 `POST /targets/{id}/receipts {operation_id,attempt_id,generation,status,steps,residuals,client_removed}`를 보냅니다. `running`은 제거 시작 확인이며 성공이 아닙니다. `succeeded`·`client_removed:true`·빈 잔여자원과 서버 게이트웨이 제거가 모두 확인돼야 `deleted`가 됩니다. 마지막 응답 유실·부분 실패·재시작은 `attention/unknown`이며 자동 재실행하지 않습니다.
5. 앱 전용 데이터는 사용자 확인 범위에 포함됩니다. 고객이 별도로 운영하는 서비스, 공유 스토리지, 기반 VM·네트워크·기존 K3s는 삭제하지 않습니다. 공유 railshot 프로젝트와 OpenStack 전용 계정·역할의 감사용 metadata도 보존 대상으로 계획에 표시합니다. 삭제 기록은 남으며 정적 설정 재조회로 부활하지 않습니다.

### 결과가 불확실한 클라이언트 삭제의 재확인·재개

앱 삭제와 환경 권한 회수가 모두 서버 기록에서 확인되어 클라이언트 단계까지 도달한 작업만 재개할 수 있습니다. 앱 또는 실행환경 권한 회수의 결과가 불확실하면 `REMOVAL_RECONCILIATION_REQUIRED`로 거절합니다. 데이터베이스 수정, 새 등록, 실패 상태 강제 초기화로 우회하지 않습니다.

1. 동일 소유자가 `POST /targets/{id}/reconciliations {operation_id}`를 요청합니다. 응답은 202 기존 작업 객체이며 `Location: /api/v1/operations/{id}`입니다. 작업의 `reconciliation`에는 `id,status,expires_at,resumable,blockers`가 들어갑니다. 확인 자격은 5분 동안 유효합니다.
2. 서버는 `environment.inspect` 읽기 명령을 상태 보고 응답으로 전달합니다. 명령에는 기존 `operation_id`, 새 `reconciliation_id`, `generation`, 이전 시도가 있으면 `previous_attempt_id`가 있습니다. 클라이언트는 설치 파일·소유권·계정·서비스·터널이 온전하고 이전 제거 프로세스가 실행 중이지 않은지 확인합니다. 기존 불확실한 작업 기록만 지우고 성공으로 보고할 수 없습니다.
3. 클라이언트는 `status:"inspected"`, `reconciliation_id`, `inspection:{state:"intact"|"partial"|"unknown",preflight_ok,helper_active}`, `client_removed:false`와 기존 receipt 필드를 전송합니다. 현재 요청·세대·전용 클라이언트 자격이 일치하고 설치 온전·사전 검사 성공·제거 프로세스 없음이 모두 확인된 경우에만 `reconciliation.status=ready,resumable=true`가 됩니다. 이 단계에서는 삭제를 실행하지 않습니다.
4. 동일 소유자가 환경 이름과 데이터 삭제를 다시 확인하여 `POST /targets/{id}/operations {action:"resume",operation_id,reconciliation_id,confirmation,delete_data:true}`와 `Idempotency-Key`를 보냅니다. 기존 작업 ID와 이전 실패 기록은 유지하며 새 `attempt_id`를 발급합니다. 새 삭제 명령에는 `reconciliation_id`도 들어가 고객 측이 확인 근거를 대조합니다. 동일 요청 키는 새 시도를 추가하지 않습니다.
5. 이전 세대·이전 시도·만료된 확인 응답은 새 시도를 변경하지 못합니다. 진행 중 같은 삭제 명령이 다시 전달되면 클라이언트는 `running`을 보고하며 제거 실행기를 중복 시작하지 않습니다. 제거 전 검사가 실패한 경우 `status:"blocked",mutation_started:false,error_code:"REMOVAL_PREFLIGHT_FAILED"`로 명시하고 자동 재시도하지 않습니다. 부분 변경은 `unknown`입니다.
6. 클라이언트 제거 성공 증거가 서버에 저장된 뒤 게이트웨이 제거만 실패하면 재확인은 고객 접속 없이 그 증거를 사용합니다. 사용자가 명시적으로 재개할 때 게이트웨이 회수만 수행합니다. 클라이언트 제거를 다시 요구하지 않습니다.

최종 성공 후 일반 클라이언트 인증과 명령은 폐기합니다. 마지막 응답 유실에 대응해 **정확히 같은 성공 receipt 내용·전용 토큰 해시·세대**에 한해서 15분 동안 중복 확인 응답만 반환합니다. 추가 삭제나 일반 상태 보고 권한은 아닙니다. 삭제 대상은 목록에서 사라지고, 감사용 삭제 상세·작업 이력은 기존 소유자만 직접 조회할 수 있습니다. 다른 소유자의 조회는 계속 404입니다.

## 운영 설정과 현재 지원 경로

`RAILSHOT_PERSONAL_CONFIG`는 API 프로세스 소유의 비공개 JSON 파일입니다. 이 파일만 설치 URL·고정 배포본 검증값·게이트웨이 실행기을 설정합니다. 요청 JSON에 임의 URL, 명령, 서버 로컬 경로, SSH 접속 주소를 받지 않습니다.

```json
{
  "version": 1,
  "public_url": "https://railshot.example.com",
  "installer_url": "https://releases.example.com/immutable/install.sh",
  "artifact_url": "https://releases.example.com/immutable/railshot-client.tgz",
  "artifact_sha256": "실제 고정 배포본의 64자리 SHA256",
  "gateway": {
    "config_path": "/etc/railshot-personal-gateway/config.json",
    "command": "/usr/local/sbin/railshot-personal-gateway"
  },
  "runtime": {"config_path": "/etc/railshot/personal-runtime.json"}
}
```

기본값은 세 URL 모두 HTTPS만 허용합니다. 승인된 격리 연결 시험에서는 **API 프로세스 환경변수 `RAILSHOT_PERSONAL_TEST_ALLOW_HTTP=1`**로 HTTP를 명시적으로 허용할 수 있습니다. 미지정 또는 `0`이면 HTTPS 요구를 유지하고, 다른 값은 시작 오류입니다. 비공개 설정 파일의 `test_allow_http` 필드나 브라우저 요청으로 이 예외를 켤 수 없습니다. HTTP 시험 모드에서도 URL의 인증정보·질의문자열·프래그먼트, HTTPS/HTTP 이외 프로토콜을 허용하지 않습니다.

시험 모드의 설치 명령은 다운로드에 `curl --proto '=http,https'`를 쓰고 설치기에 `--test-allow-http`를 전달합니다. 고정 SHA256 검증, 리디렉션 차단, 등록자격 만료와 소유권 검사는 유지합니다. 클라이언트는 이 명시적 옵션을 통해서만 HTTP API·배포본·OpenStack 인증 주소를 허용하고 상태 보고·제거 완료 전송에도 시험 설정을 유지해야 합니다. HTTPS 인증서 검증을 끄는 옵션은 추가하지 않습니다. HTTP 구간은 암호화되지 않으므로 이 설정은 운영 기본 설정으로 사용하지 않으며, 시험 종료 후 환경변수를 제거하고 API를 재시작합니다. 원격 브라우저 쿠키의 Secure 정책은 이 옵션으로 변경되지 않습니다.

지원 경로는 **OpenStack 관리망에 접근 가능한 Ubuntu 관리호스트에 클라이언트를 설치하여 서버가 그 호스트의 OpenStack CLI를 실행하는 연결**입니다. OpenStack 인증정보는 고객 측의 비공개 저장소에 남습니다. 클라이언트의 전용 SSH 호스트키와 WireGuard 공개키만 등록하며, API는 고객에게 배정된 WireGuard `/32` 주소와 고정 호스트키로 접속합니다. 전용 계정 `railshot-openstack`과 2222 포트의 제한된 SSH 명령을 사용합니다. 기존 root SSH 설정은 인수하지 않습니다.

설치기는 고객 측 비공개 실행환경 설정에 따라 OpenStack 별도 VM을 선택·생성하고 기존 Ansible K3s 설치 기능을 재사용합니다. 고객별 프로필 사전 할당은 요구하지 않습니다. 서버의 공통 중앙 배포 설정은 별도 서비스 운영 전제입니다. API 프로세스는 실제 고객 클라이언트에 `server list`를 실행하고 성공 응답을 확인한 뒤 연결 완료로 표시합니다. `GET /targets/{id}/instances`도 같은 경로로 최신 OpenStack 서버 목록을 조회합니다.

다른 서버 파트에서 실행할 때는 `product.executeOpenStack(targetId, {job_id, argv, delete_data?, public_key?}, ownerSessionId)`를 호출합니다. `ownerSessionId`는 인증한 소유자 쿠키의 서버 측 소유자 세션이며, 사용자 입력의 소유자 ID를 그대로 신뢰하지 않습니다. `job_id`는 요청별로 생성하여 작업과 함께 영속 저장하고, 결과를 조회하거나 재확인할 때 동일 값을 유지해야 합니다. 별도의 공개 HTTP 임의 명령 실행 경로는 제공하지 않습니다.

SSH의 고정 명령은 `railshot-openstack-v1`이며 입력은 `{version:1,job_id,action:"openstack.execute",params:{argv,...}}`입니다. SSH 대상·개인키·OpenStack 인증·로컬 경로·셸 문자열은 입력으로 받지 않습니다. 클라이언트가 명령·옵션 허용 목록을 검사합니다. 생성·삭제를 사용하는 기존 파트는 고객 측 생성 이력과 명시 데이터 삭제 조건을 따라야 하며, 임의 고객 기존 자원을 이 채널에서 제거할 수 없습니다. 서버는 응답의 version/job_id/action/ok, 종료코드, 1 MiB 상한을 검증하고 원문 stderr를 공개하지 않습니다. 결과 불명인 변경 명령은 새 job_id로 자동 재실행하면 안 됩니다.

실행환경 준비 호출은 클라이언트 Bearer 자격만 허용합니다.

- `POST /targets/{id}/runtimes {generation,progress:{stage,status,blockers?}}` → 202. 단계는 `client_selection`, `client_installation`, `client_verification`, 상태는 `running|blocked`입니다. 이 입력으로 준비 성공을 주장할 수 없습니다.
- `POST /targets/{id}/runtimes {generation,evidence:{resource_id,private_ipv4,management_network,placement,architecture:"amd64",initialization:"cloud-init"|"preconfigured",ssh_user:"railshot-runtime",ssh_port:2223,ssh_host_key}}` → 202. 서버는 실제 `server show`를 다시 실행해 VM·프로젝트·활성 상태·관리망 주소를 확인합니다. 클라이언트가 서버 조회 결과를 대신 입력할 수 없습니다.
- `GET /targets/{id}/runtimes` → `{target_id,generation,status,connection_status,deployable,runtime_preparation}`. 일반 소유자 브라우저는 기존 환경 상세 조회에서 동일 준비 상태를 읽습니다.

`runtime_preparation`에는 `status`(`not_started|queued|running|succeeded|blocked|unknown|revoked`), `stage`, `blockers`, `verified_at`, `client_reported_ready`, 선택적 `cluster_verified`가 있습니다. `client_reported_ready`는 고객 측 준비 증거 제출 여부이며 서버 검증 성공과 다릅니다. `cluster_verified=true`여도 `stage=application_configuration`에서 경로 설정이 빠져 `blocked`일 수 있습니다. `unknown`은 결과 불명 상태이며 자동 재시도하지 않습니다. 외부 변경 전 차단된 동일 입력만 운영 전제 해결 후 재시도할 수 있습니다. 다른 VM·호스트키로 바꾸는 재시도는 거절합니다.

공통 비공개 실행환경 설정은 다음과 같습니다. 중앙 CI 신원 및 발행 파일 조회, Argo 관리 자격, 제한된 등록 Role, 자격 갱신 ConfigMap·CronJob·Role, 이미지 읽기 자격과 Git 배포 저장소를 기존 배포 기능에 맞게 구성해야 합니다.

```json
{
  "version": 1,
  "application_template": "/etc/railshot/applications.json",
  "template_environment": "openstack-template",
  "state_dir": "/var/lib/railshot/personal-runtimes",
  "route_profiles": {
    "default": {
      "management_network": "private",
      "placement": "nova",
      "edge_template": "/etc/railshot/openstack-edge.json",
      "worker_base_file": "/etc/railshot/octavia-base.json",
      "tunnel_template": "/etc/railshot/tunnel-base.json",
      "dns_config_file": "/etc/railshot/dns.json"
    }
  }
}
```

서버는 `state_dir/<개인targetID>/applications.json`과 전용 레지스트리를 생성하고 기존 application adapter를 동적으로 연결합니다. 운영자 프로필은 고객 프로젝트 UUID를 미리 고정하지 않습니다. 선택한 관리망·placement와 operator base를 사용하고, 서버가 고객 OpenStack에서 다시 조회한 VM·port·단일 security group·project·사설 IP를 원격 Octavia worker의 환경별 immutable binding으로 등록합니다. worker는 project 이름·활성 상태, 서버/port/주소/보안그룹 귀속과 기존 claim 충돌을 다시 확인합니다. 정적 설정 존재나 `automaticDelivery` 값만으로 준비 완료를 표시하지 않습니다. 고정 VM 호스트키를 사용한 2223 관리 채널과 16443 TLS 전달 채널에서 Kubernetes Node·CoreDNS 건강, 클러스터·ServiceAccount UID, 자기 네임스페이스의 Deployment 생성 허용과 Secret 읽기·다른 네임스페이스·클러스터 권한 거절을 실제 조회합니다. TLS 인증서 확인은 유지합니다. 이전 성공 바인딩은 재시작과 상태 보고 시 읽기 전용으로 재검증합니다.

공통 템플릿의 다른 고객 실행경로나 공개 경로 설정을 그대로 인수하지 않습니다. 서버는 `ingress.runtime_binding={project_id,resource_id,private_ipv4}`와 개인 레지스트리, 환경별 edge/tunnel 설정을 새로 만들고, 운영자 tunnel credential·CA에서 해당 클러스터의 connector Secret/ConfigMap/Deployment를 생성합니다. tunnel ID는 한 개인 환경에만 귀속되어 여러 클러스터가 같은 tunnel ingress를 나눠 갖지 못합니다. 같은 관리망·placement에 여러 프로필을 두면 서버는 아직 귀속되지 않은 프로필을 이름순으로 선택하며, `profile_id`를 제공한 경우에는 그 프로필만 사용합니다. 따라서 profile 하나는 개인환경 하나의 동시 용량만 제공하고, 사용 가능한 고유 tunnel profile이 없으면 등록을 차단합니다. Octavia LB/listener/member subnet, Cloudflare tunnel/DNS zone과 자격은 운영자가 용량 pool로 미리 준비한 공통 기반 자원이어야 하며 서버가 임의로 만들지 않습니다. 이 경로 설정 검사는 설정상 귀속 확인이며 앱 공개 URL의 실서비스 검증을 뜻하지 않습니다. 공통 운영 설정이 아예 없으면 `RUNTIME_OPERATOR_NOT_CONFIGURED`, 중앙 CI가 없으면 `APPLICATION_CI_NOT_CONFIGURED`를 반환합니다.

API 컨테이너는 root로 실행하지 않습니다. [게이트웨이 배치 예제](../../deployment/manifests/personal/)의 고정 root 소유 wrapper와 제한된 sudo 규칙을 사용하며, 요청 파일은 고정 디렉터리·UID·권한을 검사합니다. WireGuard 서버 설정은 별도 권한 구성입니다. 동봉된 sudo wrapper는 같은 호스트 배치를 위한 예제이며, 일반 API 컨테이너만 실행한다고 호스트 WireGuard 권한이 자동 연결되지 않습니다. 컨테이너에서 사용할 별도 게이트웨이 연결 배치는 운영 환경에 맞게 구성해야 합니다. 설정 파일·호스트키·등록 토큰을 이미지에 포함하지 않습니다.

## 검증 구분

`apps/api/test/personal-environments.test.js`는 실제 로컬 HTTP 서버와 SQLite를 사용하며 외부 게이트웨이·배포·삭제 실행기를 모의 구현합니다. 소유권 복구, 등록토큰 회전/재사용 차단, readiness 판정, 동적 대상 배포, 삭제 경쟁 차단, 최종 제거 확인, 재시작 안전성을 확인합니다. 이 자동 시험은 실제 OpenStack·WireGuard·Argo·DNS 변경을 수행하지 않습니다. `deployment/scripts/tests/test_personal_runtime.py`는 기존 등록·갱신 정책 함수를 모의 Kubernetes API와 연결하여 신규 개인 정책 추가, 권한 확인, 다른 환경 보존, 결과 불명 재실행 차단과 권한 회수 순서를 검사합니다. 실제 환경 인수 결과는 별도 기록으로 구분합니다.
