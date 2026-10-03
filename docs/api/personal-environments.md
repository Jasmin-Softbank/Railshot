# 개인 OpenStack 환경 호출 규격

이 문서는 `apps/api/src/personal-environments.js`와 HTTP 계약 테스트의 구현 규격입니다. 실제 고객 OpenStack 설치·배포·삭제 인수 결과를 의미하지 않습니다.

## 소유권

외부 로그인은 추가하지 않습니다. `POST /api/v1/owners {}`는 장기 소유자 쿠키와 256비트 복구키를 발급합니다. `railshot_owner` 쿠키는 HttpOnly, SameSite=Strict, 1년 보관이며 원격 모드에서는 Secure입니다. 서버에는 쿠키·복구키 해시만 저장합니다. 기존 7일 익명 세션과 별도 소유자용 세션을 사용하므로 익명 세션 만료로 개인 환경 소유권이 없어지지 않습니다. `GET /owners`는 `{id,recovery_configured}`만 반환합니다. 이미 소유권 쿠키가 있는 `POST /owners`는 새 복구키를 만들지 않습니다.

`POST /api/v1/recoveries {recovery_key}`는 소유권 쿠키와 복구키를 모두 회전합니다. 이전 쿠키·복구키는 즉시 무효입니다. 새로운 복구키는 응답 한 번만 표시합니다. 복구키를 잃고 쿠키도 없으면 자동 복구할 수 없습니다. 로그인 계정 복구나 사용자 신원 확인 기능이 아닙니다. 원문을 브라우저 영구 저장소·서버 로그에 저장하지 않습니다.

개인 환경 변경 HTTP 요청은 `X-Railshot-Request: dashboard`가 필요하며 기존 Host/Origin 검사를 유지합니다. 인증되지 않은 환경/서비스 조회는 404입니다. 운영자 공용 대상을 방문·등록자격 요청·환경 생성 입력으로 개인 소유로 전환할 수 없습니다. 클라이언트용 claims/heartbeats/receipts는 각각의 Bearer 자격을 검사하며 브라우저 쿠키 및 내부 API 공용 Bearer로 대체하지 않습니다.

## 환경과 설치

- `POST /api/v1/targets {label,provider:"openstack"}` → 201 환경 객체.
- `GET /api/v1/targets?scope=owned&provider=openstack` → `{items,next_marker}`. `limit`, `marker` 지원.
- `GET /api/v1/targets/{id}` → 개인 환경 객체.
- `POST /api/v1/targets/{id}/enrollments {}` → 201 `{id,target_id,expires_at,install_command}`. 15분 유효, 일회용. 재발급 시 이전 자격 폐기. 이미 등록된 호스트의 재인수는 차단.
- `POST /api/v1/enrollments/{id}/claims`, Bearer 등록자격, `{public_key,client_version,project_id,runtime:{ssh_host_key}}` → 201 `{target_id,generation,client_token,tunnel,runtime_access,heartbeat_url}`.
- `POST /api/v1/targets/{id}/heartbeats`, Bearer 클라이언트자격, `{generation,client_version,checks:{tunnel,openstack,runtime?},project_id?,capabilities?,metrics?}` → `{target_id,status,command}`.

등록 자격은 게이트웨이 설정 이전에 사용 처리합니다. 외부 설정 결과가 불확실하면 `attention`을 보존하며 같은 등록자격을 재사용하지 않습니다. 등록 성공의 지속 자격은 별도로 생성합니다. 서버는 게이트웨이 연결과 호스트 키를 고정한 고객 측 OpenStack 명령 실행을 확인합니다. `server list` 명령이 성공해야 `ready`이며 클라이언트 자체 보고만으로 연결 완료를 표시하지 않습니다. K3s·VM 생성·사전 실행환경 프로필은 등록 조건이 아닙니다. `ready`는 OpenStack 제어 연결, `deployable`은 다른 파트에서 준비하는 기존 앱 배포 설정의 준비 상태입니다.

환경 객체 주요 필드는 `id,label,provider,scope,status,deployable,generation,last_seen_at,client_version,project_id,application_count,registration_stage,checks,blockers,deletion_operation_id`입니다. 상태는 `pending`, `installing`, `connecting`, `ready`, `offline`, `deleting`, `attention`, `deleted`입니다. 90초 이상 상태 보고가 없으면 `ready`를 `offline`으로 반환합니다. 서버 재시작 시 기존 `ready`는 다시 연결 확인해야 합니다.

`GET /targets/{id}/applications`는 해당 소유자가 RailShot으로 배포한 앱만 반환합니다. `GET /targets/{id}/observations`의 CPU·메모리·디스크·네트워크 값은 **관리 클라이언트 호스트 한 대**의 측정값이며 `scope=client_host`, `measured_scope=client_host`로 표시합니다. 프로젝트 전체 합계가 아닙니다. 미수집은 `not_configured` 및 null이며, 오래된 표본은 `stale`입니다.

배포 multipart 입력은 기존 `environment=onprem`, `provider=openstack`에 `target_id=<개인환경ID>`를 추가합니다. `deployment_selection`이라는 multipart JSON 필드를 추가하지 않습니다. 서버 내부에서 `deployment_selection:{environment,provider,target_id}`로 정규화하여 소유권·연결·삭제상태를 검증합니다. 직접 앱 대상 지정·업데이트·재시작에도 같은 개인 환경 검사를 적용합니다.

## 환경 삭제

1. `POST /targets/{id}/plans {action:"delete",delete_data:true}`는 앱별 기존 삭제 실행기로 계획을 생성합니다. `resources`, `retained`, `blockers`, `plan_hash`, 10분의 `expires_at`를 반환합니다. 다른 소유자의 앱, 진행/결과불명 작업, 연결 끊김, 미확인 상태가 있으면 차단합니다.
2. 사용자가 RailShot 서비스 및 앱 전용 데이터 삭제를 확인한 뒤 `POST /targets/{id}/operations {action:"delete",delete_data:true,plan_id,plan_hash,confirmation:<환경이름>}`와 `Idempotency-Key`를 보냅니다. 접수는 202 작업 객체와 `Location`, `Retry-After:2`를 반환합니다. 같은 입력·키는 기존 작업을 반환합니다.
3. 영속 `deleting` 상태를 먼저 저장합니다. 기존 앱 삭제 실행기로 앱과 해당 앱의 배포 연결을 순서대로 제거·확인합니다. 신규 클러스터 구성이나 다른 파트가 관리하는 공유 실행환경을 이 기능에서 생성·제거하지 않습니다. 이후 상태 보고 응답에 `{id,operation_id,kind:"environment.delete",applications:[],delete_data:true,generation}` 명령을 보냅니다.
4. 클라이언트는 `POST /targets/{id}/receipts {operation_id,generation,status,steps,residuals,client_removed}`를 보냅니다. `running`은 제거 시작 확인이며 성공이 아닙니다. `succeeded`·`client_removed:true`·빈 잔여자원과 서버 게이트웨이 제거가 모두 확인돼야 `deleted`가 됩니다. 마지막 응답 유실·부분 실패·재시작은 `attention/unknown`이며 자동 재실행하지 않습니다.
5. 앱 전용 데이터는 사용자 확인 범위에 포함됩니다. 고객이 별도로 운영하는 서비스, 공유 스토리지, 기반 VM·네트워크·기존 K3s는 삭제하지 않습니다. 삭제 기록은 남으며 정적 설정 재조회로 부활하지 않습니다.

## 운영 설정과 현재 지원 경로

`RAILSHOT_PERSONAL_CONFIG`는 API 프로세스 소유의 비공개 JSON 파일입니다. 이 파일만 설치 URL·고정 배포본 검증값·게이트웨이 실행기을 설정합니다. 요청 JSON에 임의 URL, 명령, 서버 로컬 경로, SSH 접속 주소를 받지 않습니다.

```json
{
  "version": 1,
  "public_url": "https://railshot.example.com",
  "installer_url": "https://releases.example.com/immutable/personal-install.sh",
  "artifact_url": "https://releases.example.com/immutable/railshot-client.tgz",
  "artifact_sha256": "실제 고정 배포본의 64자리 SHA256",
  "gateway": {
    "config_path": "/etc/railshot-personal-gateway/config.json",
    "command": "/usr/local/sbin/railshot-personal-gateway"
  }
}
```

지원 경로는 **OpenStack 관리망에 접근 가능한 Ubuntu 관리호스트에 클라이언트를 설치하여 서버가 그 호스트의 OpenStack CLI를 실행하는 연결**입니다. OpenStack 인증정보는 고객 측의 비공개 저장소에 남습니다. 클라이언트의 전용 SSH 호스트키와 WireGuard 공개키만 등록하며, API는 고객에게 배정된 WireGuard `/32` 주소와 고정 호스트키로 접속합니다. 전용 계정 `railshot-openstack`과 2222 포트의 제한된 SSH 명령을 사용합니다. 기존 root SSH 설정은 인수하지 않습니다.

호스트의 K3s 준비, VM 생성, 런타임 설정은 기존 다른 파트가 담당합니다. 이 등록 기능은 이를 자동 생성하지 않고, 미리 만들어진 프로필을 요구하지도 않습니다. API 프로세스는 실제 고객 클라이언트에 `server list`를 실행하고 성공 응답을 확인한 뒤 연결 완료로 표시합니다. `GET /targets/{id}/instances`도 같은 경로로 최신 OpenStack 서버 목록을 조회합니다.

다른 서버 파트에서 실행할 때는 `product.executeOpenStack(targetId, {job_id, argv, delete_data?, public_key?}, ownerSessionId)`를 호출합니다. `ownerSessionId`는 인증한 소유자 쿠키의 서버 측 소유자 세션이며, 사용자 입력의 소유자 ID를 그대로 신뢰하지 않습니다. `job_id`는 요청별로 생성하여 작업과 함께 영속 저장하고, 결과를 조회하거나 재확인할 때 동일 값을 유지해야 합니다. 별도의 공개 HTTP 임의 명령 실행 경로는 제공하지 않습니다.

SSH의 고정 명령은 `railshot-openstack-v1`이며 입력은 `{version:1,job_id,action:"openstack.execute",params:{argv,...}}`입니다. SSH 대상·개인키·OpenStack 인증·로컬 경로·셸 문자열은 입력으로 받지 않습니다. 클라이언트가 명령·옵션 허용 목록을 검사합니다. 생성·삭제를 사용하는 기존 파트는 고객 측 생성 이력과 명시 데이터 삭제 조건을 따라야 하며, 임의 고객 기존 자원을 이 채널에서 제거할 수 없습니다. 서버는 응답의 version/job_id/action/ok, 종료코드, 1 MiB 상한을 검증하고 원문 stderr를 공개하지 않습니다. 결과 불명인 변경 명령은 새 job_id로 자동 재실행하면 안 됩니다.

앱 배포 설정 연결은 기존 `RAILSHOT_APPLICATIONS_FILE`의 `environments`에서 **새로 생성된 개인 target ID를 키**로 등록하는 기존 application adapter 계약을 사용합니다. 다른 파트는 서버에서 반환된 target ID와 소유권을 유지하여 준비한 기존 실행환경 구성에 연결해야 합니다. 공용 환경의 ID를 개인 ID로 임의 인수하는 기능은 제공하지 않습니다. 구성 로드 후 `applicationAdapter.targets[targetId].automaticDelivery === true`이면 상태 보고 시 `deployable`이 켜집니다. 정적 설정 파일을 변경했다면 API를 재시작하여 기존 adapter가 새 구성을 읽도록 합니다. 구성 전에도 OpenStack 제어 연결은 `ready`일 수 있습니다.

API 컨테이너는 root로 실행하지 않습니다. [게이트웨이 배치 예제](../../deployment/manifests/personal/)의 고정 root 소유 wrapper와 제한된 sudo 규칙을 사용하며, 요청 파일은 고정 디렉터리·UID·권한을 검사합니다. WireGuard 서버 설정은 별도 권한 구성입니다. 동봉된 sudo wrapper는 같은 호스트 배치를 위한 예제이며, 일반 API 컨테이너만 실행한다고 호스트 WireGuard 권한이 자동 연결되지 않습니다. 컨테이너에서 사용할 별도 게이트웨이 연결 배치는 운영 환경에 맞게 구성해야 합니다. 설정 파일·호스트키·등록 토큰을 이미지에 포함하지 않습니다.

## 검증 구분

`apps/api/test/personal-environments.test.js`는 실제 로컬 HTTP 서버와 SQLite를 사용하며 외부 게이트웨이·배포·삭제 실행기를 모의 구현합니다. 소유권 복구, 등록토큰 회전/재사용 차단, readiness 판정, 동적 대상 배포, 삭제 경쟁 차단, 최종 제거 확인, 재시작 안전성을 확인합니다. 실제 OpenStack·WireGuard·Argo·DNS 변경은 수행하지 않았습니다.
