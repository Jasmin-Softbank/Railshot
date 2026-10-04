# CI 백엔드 API 분류와 프론트엔드 연동 설계

2026-10-02 · **제품 API 구현 이후 갱신. 현행 요청·응답은 [제품 API](product.md)와 [OpenAPI](product.openapi.json)가 정본이다.** 이 문서는 책임 경계, 지원 범위와 최초 구조도·브랜치 감사 기록을 설명한다. 코드 연결과 로컬 시험을 실제 클라우드 배포 완료로 해석하지 않는다.

제품 API는 화균 님 OpenStack REST Controller의 자원·접수·목록·오류 형식을 재사용한다. [REST 컨벤션](conventions.md)에 따라 `/api/v1/targets`, `builds`, `deployments`, `profiles`, `plans`, `environments`를 사용한다. 경로에 실행기 이름을 대시·밑줄·camelCase로 조합하지 않는다. 짧은 복수 명사 사용은 팀 규칙이며 REST·HTTP 표준 자체의 의무는 아니다.

사용자 계정·로그인·팀원 allowlist·user/auth 도메인은 만들지 않는다. **모든 사용자가 같은 workspace에서 업로드·빌드·배포를 요청한다.** 실행 가능한 범위는 서버에 등록한 target·profile과 지원 기능으로 결정한다. CI tenant, Provider account/project, GitHub·SSH·클라우드 자격은 서버 설정이며 사용자별 소유권 모델이 아니다.

## 1. 제품 API와 담당 실행기의 경계

프론트엔드는 제품 자원과 상태를 다룬다. 제품 백엔드는 입력과 등록 설정을 대조하고 실행 의도를 영속 저장한 뒤 기존 CI·Provider·Ansible·CD 코드를 호출한다. 분류마다 새 HTTP 서버나 범용 작업 프레임워크를 만들지 않는다.

| 분류 | 제공자 → 소비자 | 구현된 책임과 범위 |
|---|---|---|
| 제품 API | `apps/api` → Dashboard·CLI·`apps/agent` MCP | 등록 대상·profile 조회, CI 빌드, 전체 앱 배포, 계획·환경 자원. 기존 CLI의 legacy CI 경로도 유지 |
| CI 실행 | 제품 API → GitHub Actions → 게시 검증 | 소스 snapshot·commit·target 고정, 검사·AI 수정, 검증 이미지 게시. CI 완료를 앱 배포 완료로 바꾸지 않음 |
| 자원 준비 | `environments.js` → `terraform_tools/provision.py` | 등록 AWS/GCP 단일 runtime의 saved plan 생성·digest 고정·apply. 기존 자원 변경·삭제·유지보수는 공개 생성에서 차단 |
| 서버 구성 | `environments.js` → `infrastructure/ansible/run.py` | Provider descriptor를 환경별 등록 snapshot으로 고정하고 입력 검사→guest→runtime 실행·결과 확인 |
| DB 구성 | 환경 API → cluster.py → 팀 DB 플레이북 | 별도 VM의 PostgreSQL·Patroni·etcd·HAProxy 준비, TLS·앱별 계정 binding 생성. 내부 Ansible HTTP 경로도 유지 |
| 앱 적용 | `cd.js` → `gitops/bridge.py` → 기존 GitOps/Argo | 검증한 게시 파일·image digest·고정 target 설정으로 선언 commit·sync·검증. 기대 공개 HTTP 확인 후 전체 성공 |
| 관측 | 담당 관측 소스 → 운영자 | 노드·워크로드·HTTP 관측. 범용 PromQL·집계 API를 제품에 추가하지 않음 |

호출 순서는 **환경 준비: 제품 API→Terraform→등록 snapshot→Ansible**, **반복 앱 배포: 제품 API→CI→게시 검증→GitOps/Argo→기대 공개 HTTP 검증**이다. 환경 준비와 앱 배포의 성공 조건을 구분하고 반복 배포마다 VM·K3s를 다시 설치하지 않는다.

## 2. 프론트엔드가 사용하는 자원

| 자원 | 요청 | 반환·판정 |
|---|---|---|
| 대상 | `GET /api/v1/targets` | 한 등록 대상의 공개 요약. `ci_submission`과 `application_deployment`를 분리하고 CD 앱은 `application_name`, `deployment_scope`로 표시 |
| 빌드 | `POST /api/v1/builds`, `GET /api/v1/builds/{id}` | 202 접수 후 GitHub run ID 문자열로 조회. `published`는 검증한 이미지 게시 |
| 배포 | `POST /api/v1/deployments`, `GET /api/v1/deployments/{id}` | 202 접수 후 CI·CD·공개 HTTP 단계를 조회. 동일 source/target/digest와 고정 revision의 검증이 완료되어야 `succeeded`와 최상위 `url` 제공 |
| profile | `GET /api/v1/profiles` | 등록 사양의 `id`, `label`, `provider`, `site`, `purposes`, `supported`, `blockers`. 자격·내부 경로 제외 |
| 계획 | `POST /api/v1/plans`, `GET /api/v1/plans/{id}` | 계산·저장한 계획은 201 + Location. 실행 불가 계획도 `executable=false`·`blockers`로 표시 |
| 환경 | `POST /api/v1/environments`, `GET /api/v1/environments/{id}` | `plan_id`만 받아 202 접수. 자원·guest·runtime·선택적 Patroni DB·등록 결과를 별도로 기록 |

앱 이름은 업로드 ZIP·폴더·GitHub 저장소에서 결정하며 UI 검토, API, CLI가 `contracts/application.mjs`의 규칙을 공유한다. `apps/agent` MCP는 앱 이름을 명시적으로 받는다. 환경 선택은 대상만 고르고 등록된 샘플 앱 이름으로 소스를 바꾸지 않는다. 다른 앱에 묶인 고정 대상은 소스 취득·CI 전에 거절한다. 신규 앱은 기존 `create_per_request` 계획 또는 그 앱의 등록된 대상을 사용한다. 폴더 업로드의 `source_name`은 필수이며 이름 누락·규칙 위반을 예시 이름이나 해시로 대체하지 않는다.

목록은 `{items, next_marker}`, 상세는 자원 객체를 직접 반환한다. 비동기 접수는 화균 님의 `{resource_id, action:"create", status:"accepted", request_id}`와 `Location`, `Retry-After: 2`, `X-Request-ID`를 사용한다. 오류는 `{error:{code,message,request_id,retryable,outcome_unknown}}`다. JSON 필드의 세부 타입·허용 값·입력 한도는 OpenAPI를 따른다.

빌드·배포는 `multipart/form-data`로 `app`, `target_id`와 소스 하나를 받는다. 소스는 공개 GitHub `repository_url`, ZIP `archive`, 또는 반복 `files`와 JSON `paths`다. 화면의 파일명만 전송하지 않고 실제 소스를 전송한다. 등록 target의 ID를 사용하며 provider 문자열을 임의 대상 ID로 취급하지 않는다.

배포·환경은 `Idempotency-Key`가 필수다. 공유 workspace의 자원 종류별로 같은 키·같은 입력이면 같은 자원을 반환하고, 입력이 달라지면 409다. queued/running은 202, 완료·실패·차단·unknown은 200과 기존 자원·Location을 반환한다. 빌드 생성에는 이 멱등 계약이 없으므로 응답 유실 시 자동 재접수하지 않는다.

`/healthz`는 프로세스와 CI 설정 여부를 보여 주며 클라우드·CD·SSH 준비를 보증하지 않는다. 기존 `POST /api/deploy`, `GET /api/runs/{run_id}`는 응답 필드와 `x-railshot-request: deploy` (legacy: `x-jasmin-request: deploy`) 계약을 유지한다. 실제 등록 서비스에서는 새 영속 접수·실행 한도·run binding을 공유하므로 이 workspace가 접수하지 않은 외부 run을 조회하지 않는다. legacy deploy의 의미는 CI 제출이다.

## 3. 영속 기록과 성공 조건

[product-store.js](../../apps/api/src/product-store.js)는 비공개 source snapshot과 실행 의도를 먼저 저장한다. 기록과 디렉터리 fsync·atomic rename이 성공한 뒤에만 외부 실행을 시작한다. `RAILSHOT_STATE_DIR`는 저장소 밖의 전용 영속 디렉터리이며 상태 파일과 소스는 실행 OS 사용자 소유로 보관한다.

한 API 프로세스와 단일 writer가 기존 SQLite operations의 FIFO를 처리한다. 새 배포와 업데이트는 소스 snapshot을 저장하고 queued로 접수한다. 빌드·계획·환경·수명주기·resume는 기존 응답 계약과 실행 슬롯 검사를 유지한다. worker가 끝난 unknown은 60초 뒤 전역 슬롯만 반납하고 같은 앱·미확정 환경의 변경은 계속 차단한다. 배포·환경의 동일 키 재조회는 기존 기록을 먼저 반환한다. 보관 한도 100개 안에서 대기하며 자동 재시도·공개 reset API는 없다. 재시작 시 신형 큐의 미실행 queued만 자동 시작하고 running과 구형 queued는 unknown으로 남긴다. [큐 및 중복 방지 계약](conventions.md)을 따른다.

| 상태 | 의미 |
|---|---|
| `queued`, `running` | 실행 의도 저장 후 대기 또는 실행 중 |
| `blocked` | 등록 설정·지원 범위·실행 전제 미충족 |
| `failed` | 해당 단계의 실패가 관측됨 |
| `unknown` | 외부 부작용 또는 결과 저장 여부를 확정할 수 없음. 자동 재실행 금지 |
| 배포 `succeeded` | 게시 증거, 동일 target·image·고정 revision의 CD 적용, 기대 공개 HTTP 검증을 모두 충족 |
| 환경 `succeeded` | 해당 환경의 자원·guest·runtime 준비 완료. 앱·DB 배포 완료를 뜻하지 않음 |

HTTP 조회 성공과 작업 성공도 구분한다. 실패 자원 조회는 200과 `status=failed`이고 조회 자체의 실패는 오류 HTTP 상태를 사용한다. 매 HTTP 요청의 request ID, 작업에 저장한 오류 추적 ID, 내부 Ansible의 `ansible_job_id`는 서로 다른 식별자다. 원문 예외·stdout·자격은 제품 응답에 내보내지 않는다.

CD 등록 설정은 작업에서 읽은 bytes의 SHA-256으로 고정한다. 같은 target 별칭의 설정을 접수 후 다른 cluster·namespace·Git 경로로 바꾸면 실행 전에 차단한다. child process 종료·timeout 시 실행기 process group 정리를 기다린다. 이 로컬 정리가 이미 수행한 원격 변경을 되돌리거나 불확실한 결과를 성공으로 바꾸지는 않는다.

## 4. 환경 계획과 Ansible 연결

현재 제품 환경 실행은 **운영자가 등록한 AWS/GCP 단일 amd64 runtime, `database.mode=none`**이다. 예를 들어 등록 profile이 `aws-runtime-small`이면 다음 입력을 사용한다.

```json
{
  "name": "demo-runtime",
  "runtime": {"profile_id": "aws-runtime-small", "node_count": 1},
  "database": {"mode": "none"}
}
```

제품 API는 profile·역할·SSH 참조·서버 정책을 검사한 뒤 기존 Terraform plan을 호출한다. 이 과정은 Provider 조회와 saved plan 파일 작성을 포함하지만 VM apply는 하지 않는다. 계획에는 정규화 입력, `policy_revision`, 15분 만료 시각, `steps`, `plan_sha256`, `executable`, `blockers`를 저장한다. 알려지지 않은 비용은 null이다. multi-node runtime과 DB mode `standalone`은 blockers를 반환한다. `patroni`는 등록된 DB profile의 역할·배치와 일치하는 계획만 실행 대상으로 인정한다.

환경 접수는 `{ "plan_id": "plan001" }`와 idempotency key만 받는다. 저장한 입력·profile snapshot·정책·만료·saved plan digest와 apply 이력을 재검사하고 계획을 한 환경에 한 번만 연결한다. 나중 계획이 같은 target의 saved plan을 바꾸었으면 기존 계획은 차단한다. 다른 입력을 함께 보내 저장 계획을 우회하는 경로는 없다.

Terraform 결과의 `node_descriptor`를 환경별 비공개 파일로 고정하고, 운영자 SSH 참조와 결합한 `targets.json` snapshot을 새로 만든다. 동일 snapshot에서 native `run.py --validate-only`, `guest.check`, `runtime.install`을 순서대로 호출한다. 기존 상시 Ansible HTTP 서버의 시작 시 registry를 변경하거나 재시작하지 않는다. 매 단계 전 snapshot을 확인하고 결과의 요청 ID·target·operation·준비 상태가 일치해야 다음 단계로 진행한다. SSH host key는 운영자가 검증한 known_hosts를 사용하거나, profile의 `enroll_ssh`가 켜진 경우 기존 Provider의 신뢰된 등록 경로로 환경별 known_hosts를 만든다. 호스트키 검증을 우회하지 않는다.

새 VM의 `runtime_target_id`는 그 환경의 식별자다. runtime profile에 운영자가 준비한 배포 설정이 있으면 기존 `environment.py register`로 CI 허용 target과 CD의 cluster·namespace·application·Git·공개 URL을 등록하고 검증한다. 필요한 DB binding과 등록 결과까지 확인해야 `deployment_supported=true`가 된다. 배포 설정이 없으면 runtime 준비 후에도 `deployment_supported=false`, `DEPLOYMENT_TARGET_NOT_REGISTERED`를 유지한다.

Slack 회의의 CSP·온프렘별 배치 수 전달 요구는 [10/1 기록](../meetings/2026-10-01.md)의 2:49:49–2:51:07과 [Ansible 계약](ansible.md)에 정리되어 있다. AWS 3개·온프렘 2개는 변수 전달 예시이며 고정 기본값이 아니다. 내부 Ansible HTTP는 승인 HA profile의 `nodes/placements`를 팀의 `db_nodes/etcd_nodes/proxy_nodes`와 TLS/Vault 참조에 연결한다. 제품 환경 API는 별도의 환경별 registry·cluster spec을 `cluster.py`에 전달하고 검증된 DB binding을 받는 경로를 구현했다. 두 경로 모두 팀 DB 플레이북을 사용하지만 입력 등록 방식은 구분한다. 현재 코드 연결과 실제 신규 VM·DB 설치·복제·앱 연결 검증의 완료 여부는 [Ansible 계약](ansible.md)의 운영 인수 상태로 구분한다.

## 5. 공개 workspace와 배치 조건

사용자 로그인 대신 서버 등록 범위를 검사한다. 브라우저는 target/profile ID와 소스·제품 입력만 전달한다. 임의 Provider URL·playbook·shell·SSH 키·로컬 파일 경로·클라우드 자격을 받지 않는다. Host·Origin, 입력 타입·중복 필드·파일 수·용량, 등록 run binding, 단일 실행 및 보관 한도는 공개 모드에서도 유지한다. 이 검사들은 사용자 식별 기능이 아니다.

API의 직접 공개 모드는 `RAILSHOT_PUBLIC_DEMO=1`과 정확한 allowed hosts/origins를 명시한다. 컨테이너 gateway 구성은 Dashboard Nginx가 같은 origin의 `/api/`를 내부 API로 전달하며 내부 Bearer를 서버에서 주입한다. 브라우저는 로그인하거나 token을 입력하지 않는다. 두 배치 방식 모두 제품은 공유 workspace이며, 내부 운영자 인증을 새 사용자 인증 서비스로 확장하지 않는다.

운영 Dashboard/API/Argo Pod, 전용 CI 빌드 워커, 고객 K3s와 DB VM은 배치 단위가 다르다. API 이미지는 Python·Terraform·Ansible·kubectl·Git·SSH·AWS SSM/GCP IAP 실행 도구와 기존 native 모듈을 포함한다. MCP 이미지는 Node stdio 클라이언트로 유지한다. API 상태는 PVC에 보존하고 replica 1·Recreate로 같은 store의 동시 writer를 피한다. 상세한 source·secret·권한·mount 조건은 [컨테이너 배치](../architecture/container-deployment.md)를 따른다.

railshot.io의 실제 이미지 게시·Pod 적용·등록 자격·공개 HTTP·CI→CD E2E는 별도의 실행 결과로 확인한다. 로컬 HTTP·mock adapter 검사, syntax/import 검사, artifact checksum은 각각의 연결 검증이며 클라우드 성공을 대신하지 않는다.

## 6. 첨부 구조도 대조

최초 검토 파일은 `Architecture.svg`, 4452×3219, SHA-256 `dd245364019352eba16d21331a29e3c2754a94185804f3c4cdd10965c90cdb9f`다. 글자가 path인 SVG를 PNG로 렌더링해 확인했다. 도형의 포함 관계는 있으나 요청 방향·메시지를 나타내는 화살표가 없어 그림만으로 호출 순서를 확정하지 않았다.

| 그림 요소 | 현재 코드와 해석 |
|---|---|
| Dashboard, MCP, API Service | `apps/dashboard`, `apps/api`가 제품 진입점. 제품 API가 CI·환경·CD를 연결하며 로그인 도메인은 없음 |
| CI/CD Pipeline | CI worker·publisher와 GitOps/Argo의 실행 주체를 구분. API Pod 안에서 고객 코드를 빌드하지 않음 |
| Provider Interface | Terraform CLI와 OpenStack REST는 별도 계약. 현행 제품 환경 어댑터는 AWS/GCP Terraform만 연결 |
| Compute / LB / Network / DNS | 기능 분류이며 모든 Provider가 동일 CRUD를 제공한다는 뜻이 아님. Edge와 등록 route는 운영자 설정 |
| REST Controller → OpenStack/Proxmox | OpenStack은 구현. Proxmox placeholder는 제품 지원 기능으로 표시하지 않음 |
| Kubernetes 외곽선 | 운영 제어 서비스의 배치 영역. AWS/OpenStack VM 자체가 모두 같은 클러스터 안에 있다는 뜻이 아님 |
| 우측 CI Platform | 전체 제품 범위 표기이며 별도 실행 컴포넌트가 아님 |
| 그림에 없는 Ansible·DB·관측·registry | Ansible은 guest/runtime 경계, DB는 K3s 밖 VM, registry는 CI→CD 검증 산출물 경계로 설명 |

## 7. 최초 원격 브랜치 감사 기록

아래는 **과거 시점의 감사 기록**이다. “없음”, “미구현”, “open”은 아래 확인 SHA 당시 판정이며 현재 제품 지원 범위가 아니다. 이후 PR #10과 #17의 통합 및 제품 API 구현이 이어졌다. 현행 소스·계약은 앞 절과 product.md/OpenAPI로 확인한다. 현재 브랜치 존재·PR 상태는 이 고정 표로 추정하지 않는다.


다음 표는 2026-10-02 17:56 KST의 원격 목록과 18:03 KST의 컨테이너 후속 확인 기록이다. 당시 integration은 f35ffda다. 복원된 원격 feature 이름이 존재한다는 사실과 integration 반영 여부를 구분했다. Git 이력뿐 아니라 담당 경로의 blob 내용을 비교했다. `source-map.json`은 최초 조립 기록이므로 후속 병합 전체를 대표하지 않는다.

| 브랜치 | 확인 SHA | integration과의 관계 / 인터페이스 판정 |
|---|---|---|
| `main` | `cc1f084bf8453fa3ba3afeac9fd44ba8d01f92e6` | 저장소 안내·규칙만 있음. 실행 인터페이스 검토 기준은 integration |
| `feature/dashboard-ui` | `4fb39c88e17070d2ac048ec0667083f0252b644c` | 화면 5개 파일은 integration과 blob 동일. API 호출·활성 배포 버튼 없음 |
| `feature/deployment-runtime-seungmin` | `a50dd1a3239e2f8651ee61b4efe7a0dba55c08b4` | `deployment/` 원본 내용 반영. 이전 Dashboard의 CI 제출·조회 연결은 이 브랜치에 남아 있음 |
| `feature/multicloud-db-hwagyun` | `67d19efc81b01b2a55b6dd54c198088b018bdd1f` | DB roles/playbooks/계약 반영. Ansible 공통 설정 병합 외 원본 보존. API 실행으로의 연결은 없음 |
| `feature/observability_JB` | `80dae724722ec41fdd82b6265e98fd7d5f7921ca` | 이전 `e42cec9`의 기본 관측 소스는 반영됐고 integration에 DB dashboard/alert 추가. 최신 standalone Cilium 변경은 미반영이며 통합 시 경로·lock 조정 필요. API 변화 없음 |
| `codex/container-deploy-20261002` | `957afa277d5c17ba5df01aa4a1fb4344aecf7074` | [PR #10](https://github.com/Jasmin-Softbank/Railshot/pull/10), 확인 시 open. `3752ade`에 Dashboard/API/MCP 이미지·Compose/Kubernetes 설정·내부 운영자 Bearer·Host/Origin 설정 구현, `957afa2`에 Argo 선언 검증 추가. integration 미반영. UI fetch·API 프록시·유저 인가·새 제품 endpoint는 없음 |
| `integration/team-assembly-20261002` | `f35ffda3825681f292a62bde3db4da0f4b35009c` | 담당 부품과 내부 계약은 조립됨. 제품 UI→전체 배포의 연결 완료는 아님 |

### 같은 날 최초 문서 게시 전 재확인: integration 6037d3a

`feature/dashboard-ci-connection-20261002@201d24414988d1cfb299989eac82e3f21fbfeb41`의 UI 연결과 `feature/observability-followup-20261002@a9b10753c228fcba936c9512b1c752b3c90daa25`의 통합 수정이 반영됐다. 대시보드는 기존 CI API를 호출하며, standalone site.yml도 통합 Cilium 파일 배치와 공통 lock을 사용한다. `apps/api`의 실행 소스는 f35ffda와 동일하므로 신규 v1·다중 대상·전체 배포 조율이 추가된 것은 아니다.


## 8. 검증 근거와 남은 인수

최초 f35ffda 감사에서 확인한 Ansible 로컬 HTTP 2개·제품 API 2개 테스트는 당시 기록이다. 이후 v1·CD·환경·영속 저장소의 검사는 아래 현재 소스와 테스트에서 확인한다. 이 문서는 특정 원격 CI run이나 클라우드 배포 성공을 보고하는 문서가 아니다.

- 제품 계약·라우터·상태: [product.md](product.md), [OpenAPI](product.openapi.json), [server.js](../../apps/api/src/server.js), [product.js](../../apps/api/src/product.js), [product-store.js](../../apps/api/src/product-store.js).
- 환경·CD 연결: [environments.js](../../apps/api/src/environments.js), [cd.js](../../apps/api/src/cd.js), [bridge.py](../../gitops/bridge.py), [API 테스트](../../apps/api/test/).
- 담당 실행 계약: [Terraform](../../infrastructure/providers/terraform_tools/README.md), [Ansible](ansible.md), [DB 입력](deployment-inputs.md), [CI 게시](ci-publication.md), [GitOps](../../gitops/README.md).
- 배치·병합 이력: [컨테이너 배치](../architecture/container-deployment.md), [통합 기록](../integration/gitflow.md).

남은 인수는 운영 profile·target·비공개 실행 설정 준비, API 이미지의 실제 build/smoke·게시·설치, railshot.io 같은 origin 요청, 실제 고객 소스의 CI 게시→고정 revision CD→공개 HTTP 검증이다. 새 runtime을 제품 배포 대상으로 등록하는 절차와 별도 DB 담당 검증도 각각 결과를 남겨야 한다. 인증 방식 결정이나 user/auth 구현을 남은 태스크로 추가하지 않는다.
