# Shared workspace product API

이 문서는 `apps/api/src/server.js`, `product.js`, `environments.js`, `product-store.js`에 구현한 제품 API를 설명한다. 기계 판독 계약은 [product.openapi.json](product.openapi.json)이다. 이 계약의 문서·로컬 검증은 `not_deployed`이며 실제 클라우드 E2E 결과와 별도로 기록한다.

사용자 계정·로그인·팀원 allowlist는 없다. 모든 사용자가 같은 workspace의 등록 대상과 접수 기록을 사용한다. 공개 모드는 `RAILSHOT_PUBLIC_DEMO=1`, `RAILSHOT_ALLOWED_HOSTS`, `RAILSHOT_ALLOWED_ORIGINS`를 명시하며 브라우저 Bearer를 요구하지 않는다. 기본 로컬 모드와 별도 내부 운영자 모드는 `access.js`의 기존 경계를 사용한다. GitHub·Provider·SSH 자격은 서버 설정에만 둔다.

| 자원 | 구현 경로 | 의미 |
| --- | --- | --- |
| 대상 | `GET /api/v1/targets` | CI 서비스에 등록한 대상 목록. `ci_submission`·`application_deployment`를 구분한다. CD가 등록한 앱은 `application_name`과 `deployment_scope=registered_application`으로 표시한다. runtime 상태는 독립 관측이 없으면 unknown이다. |
| 빌드 | `POST /api/v1/builds`, `GET /api/v1/builds/{id}` | ZIP·폴더·공개 GitHub를 기존 CI로 제출한다. ID는 GitHub run ID 문자열이며 등록한 run만 조회한다. `published`는 검증한 이미지 게시다. |
| 배포 | `POST /api/v1/deployments`, `GET /api/v1/deployments/{id}` | 선택한 계획으로 환경 준비·대상 등록을 먼저 수행하거나, 이미 등록된 대상으로 바로 CI를 실행한다. CI 게시 결과를 검증한 후 CD 어댑터를 한 번 호출한다. 같은 source/target의 고정 revision 배포 및 기대 공개 HTTP 검증까지 확인해야 succeeded와 최상위 url을 반환한다. |
| profile | `GET /api/v1/profiles` | 운영자가 등록한 환경 사양, 고정 runtime 대상, 선택적 앱 이름·DB 역할 수와 지원 범위. 자격·로컬 경로는 포함하지 않는다. |
| 계획 | `POST /api/v1/plans`, `GET /api/v1/plans/{id}` | 검증·저장한 계획을 201로 반환한다. 계획은 VM 생성 결과가 아니다. |
| 환경 | `POST /api/v1/environments`, `GET /api/v1/environments/{id}` | 저장된 계획을 한 번 실행한다. 자원·guest·runtime, 선택적 Patroni DB와 배포 대상 등록을 각각 기록한다. 앱 소스의 CI·배포·공개 HTTP 검증은 이 요청에 포함하지 않는다. |

목록은 `{items, next_marker}`이고 미설정 서버의 대상·profile 목록은 빈 목록이다. 목록에는 `limit`(1–100, 기본 20)과 해당 목록의 ID를 사용한 `marker`만 받는다. 그 밖의 경로는 query를 받지 않는다. 알려지지 않은 필드, 중복 단일 multipart 필드·query·JSON key는 거부한다. 파일은 최대 2,000개·총 100 MiB이며 원시 multipart 상한에는 framing용 1 MiB를 더한다. `files`만 반복할 수 있다.

소스 접수는 multipart의 `app`, `target_id`와 공개 GitHub URL(`repository_url`), ZIP(`archive`), 폴더(`files`와 JSON 문자열 배열 `paths`) 중 하나를 받는다. `source_type`은 생략할 수 있으며 지정하면 실제 소스 형식과 일치해야 한다. `plan_id`는 `POST /api/v1/deployments`에서만 선택적으로 받는다. 빌드·legacy deploy에는 허용하지 않는다. 계획을 포함한 배포의 `app`·`target_id`는 계획의 이름·고정 runtime 대상과 일치해야 하고, 해당 profile에 배포 등록 설정이 있어야 한다. 계획이 없는 배포는 서버 CD 설정에 등록된 대상·앱을 사용한다.

profile의 `target_id`는 생성할 runtime 대상이다. `application_name`은 배포 설정의 앱 이름이며 미설정이면 null이다. `deployment_supported`는 배포 설정 유무, `supported`는 provider·runtime 용도·운영자 실행 허용 여부를 나타낸다. 두 값 모두 생성·등록·준비 완료를 뜻하지 않는다. `database`는 null 또는 `{mode:"patroni", required, database_nodes, dcs_voters, proxy_nodes}`다. `required`는 등록된 앱 대상의 DB binding 필요 여부다. DB와 DCS 역할은 같은 VM에 둘 수 있으므로 역할 수를 더한 값이 VM 수는 아니다.

계획 입력은 `{name, runtime:{profile_id,node_count}, database:{mode,placements?}}`다. `name`은 3–30자이며 소문자로 시작하고 소문자·숫자·하이픈을 사용하며 소문자나 숫자로 끝난다. 현재 실행 가능한 runtime은 운영자가 등록한 AWS/GCP amd64 VM 한 대다. `node_count` 입력 범위는 1–64이지만 1이 아니면 `SINGLE_NODE_ONLY`로 실행을 차단한다. `database.mode=none`은 placements를 생략하거나 빈 배열로 지정한다. `standalone`은 입력으로 받되 `STANDALONE_DATABASE_UNSUPPORTED`로 차단한다.

`database.mode=patroni`에는 `placements`가 필요하다. 각 항목은 `{profile_id,database_nodes,dcs_voters,proxy_nodes}`이며 해당 profile에 등록한 역할 수와 정확히 일치해야 한다. 배열은 최대 16개, 각 역할 수는 0–31이며 profile 중복은 차단한다. 합산 구성에는 DB 노드 2개 이상, 홀수인 DCS voter 3개 이상, proxy 1개 이상이 필요하다. DB와 proxy는 같은 VM에 배치할 수 없다. 이 입력은 운영자가 이미 허용한 VM·네트워크 구성을 선택하며 클라우드 자격·SSH 경로·임의 provider 변수를 받지 않는다. 서로 다른 provider의 profile을 선택해도 클라우드 간 네트워크 연결을 자동으로 만든다는 뜻은 아니다.

배포 설정이 있는 runtime profile은 등록 대상의 DB binding 필요 여부와 요청의 `database.mode=patroni` 여부가 일치해야 한다. 불일치하면 Terraform 계획을 실행하기 전에 `DATABASE_BINDING_PROFILE_MISMATCH`로 차단한다. 배포 설정이 없는 profile에서는 DB 준비를 독립적으로 선택할 수 있다.

계획은 15분 뒤 만료되며 입력·정책·profile·대상 snapshot과 각 VM의 저장된 Terraform plan digest에 묶인다. 기존 자원의 수정·삭제 없이 새 자원 생성만 허용한다. 실행 차단 이유는 `blockers`, 실행 가능 여부는 `executable`로 반환하고 `cost`는 현재 null이다. `steps`는 예정된 작업 목록이며 실행 증거나 실제 `stage` 전환 기록은 아니다. 한 계획은 환경 생성과 계획을 포함한 배포 중 한 번만 소비할 수 있다.

계획을 포함한 배포는 `plan_id`, `environment_id`, `environment`를 반환하고 최초 `stage`가 `environment`다. `environment_id`는 배포 UUID 뒤에 `.environment`를 붙인 내부 실행 식별자이며 별도 `GET /api/v1/environments/{id}` 자원으로 등록하지 않는다. 진행 상태는 배포 객체의 `environment`에서 읽는다. 초기값은 `{status:"queued"}`이며 실행 중 자원·guest·runtime·DB·binding 결과가 추가된다. 환경이 succeeded이고 대상 등록까지 확인된 뒤에만 CI를 제출한다.

환경의 `resources.nodes`는 모든 자원 생성이 성공한 뒤 반환하는 `{target_id,provider}` 목록이다. VM 생성만으로 guest·runtime·DB 준비를 증명하지 않는다. `database`는 Patroni 실행 결과와 비공개 binding 파일 digest를 확인한 뒤 succeeded가 된다. `binding`은 그 binding을 사용하는 배포 대상 등록 단계이며 등록 성공 후에만 환경의 `deployment_supported=true`가 된다. DB를 생략하면 `database.status=skipped`이고, 배포 설정이 없으면 runtime 또는 DB 준비가 성공해도 `deployment_supported=false`다. 환경의 `stage`는 성공 후에도 마지막 단계인 runtime·database·binding 등으로 남을 수 있다.

배포의 `cd.migration`은 migration Job을 관측한 경우에만 존재하며 `{name,state:"succeeded"|"unverified"}`다. migration 성공, DB 준비, 대상 등록, 앱 배포와 공개 HTTP 성공은 각각 별도 결과다. 최상위 배포 성공은 `cd.deployed=true`, 고정 revision과 성공한 공개 HTTP 관측을 모두 요구한다. 환경 생성의 성공이나 profile 설정만으로 앱 URL을 성공 결과로 제공하지 않는다.

비동기 접수는 화균 님의 `{resource_id, action:"create", status:"accepted", request_id}` 형식과 `202`, `Location`, `Retry-After: 2`, `X-Request-ID`, `Cache-Control: no-store`를 사용한다. 오류는 `{error:{code,message,request_id,retryable,outcome_unknown}}`다. 매 HTTP 요청마다 새로운 request ID를 생성한다. 작업에 저장한 오류 ID와 Ansible의 `ansible_job_id`는 별개다.

배포·환경 생성에는 `Idempotency-Key`가 필요하다. 같은 키와 입력은 같은 ID를 반환한다. queued/running이면 202, 완료·실패·차단·unknown이면 200 자원 객체와 Location이다. 같은 키로 입력을 바꾸면 409이며 배포의 `plan_id`도 입력에 포함한다. ZIP의 압축 시각·multipart boundary는 파일 의미에 포함하지 않으며 폴더 파일 순서도 정규화한다. GitHub URL의 첫 SHA·소스 snapshot은 고정하고 같은 키의 재요청에서 다시 다운로드하지 않는다. 빌드 생성에는 이 멱등 계약이 없으므로 응답 유실 시 자동 재전송하지 않는다.

`RAILSHOT_STATE_DIR`는 저장소 밖의 전용 영속 디렉터리로 설정한다. 기본값은 사용자 홈의 `.local/state/railshot`이다. 최종 디렉터리와 state 파일은 현재 OS 사용자 소유이며 다른 사용자 접근 권한과 심볼릭 링크를 거부한다. 소스는 0600 snapshot, 작은 작업 기록은 fsync 후 atomic rename으로 저장한다. snapshot과 의도를 저장한 뒤에만 CI·CD·환경 실행을 시작한다. 기본 보관 상한은 작업 100개, 계획 100개, 소스 snapshot 512 MiB다. 상한 도달 시 409를 반환하며 자동 삭제하지 않는다. 운영자는 작업을 확인하고 보관·정리 정책을 적용해야 한다.

한 API 프로세스가 한 로컬 저장소를 소유한다. 프로세스 ID와 시작 식별자로 같은 호스트의 중복 사용을 막는다. 이 lock은 여러 호스트의 분산 잠금이 아니므로 API replica는 1개로 운영하고 Recreate 전략으로 이전·새 컨테이너의 동시 쓰기를 피한다. 서로 다른 PID namespace와 여러 호스트의 동시 writer는 지원하지 않는다. 미완료 작업과 unknown은 하나의 admission 한도를 공유한다. 재시작 시 queued/running을 unknown으로 저장하고 작업을 자동 재실행하지 않는다. 외부 실행 결과 유실이나 저장 실패도 성공으로 표시하지 않는다. unknown을 해제하는 공개 API는 없으며 운영자가 GitHub·CD·환경 기록을 확인해야 한다.

CI는 `GITHUB_TOKEN`, 등록 대상 ID 및 기존 GitHub 저장소 설정을 사용한다. 기존 대상의 CD는 `RAILSHOT_CD_CONFIG`의 비공개 고정 설정과 검증한 publication 파일만 받는다. 환경은 `RAILSHOT_PROFILES_FILE`과 서버의 Python/Terraform/Ansible 도구를 사용하며, 새 대상의 CD 설정은 해당 환경의 등록 결과에서 읽는다. 현재 코드 경로는 AWS/GCP의 단일 runtime, 선택적 Patroni DB, 배포 대상 등록과 CI/CD를 연결한다. profile 등록·SSH·네트워크·실행 도구가 준비됐다는 사실과 실제 클라우드·DB·공개 앱 검증 성공은 구분한다.

서버 운영 인수 시 사용할 현재 로컬 실험 기준 경로는 `/Users/mango/.local/share/railshot/db-app-stack-20261002`이며 아래 표에서 `BASE`로 표기한다. 환경 변수에 `BASE`라는 문자열을 그대로 넣는 대신 실제 절대 경로로 확장한다. 이 경로와 설정 목록은 원격 API 활성화나 실험 E2E 완료 증거가 아니다.

| 설정·참조 | 값·운영 조건 |
| --- | --- |
| `RAILSHOT_PROFILES_FILE` | `BASE/profiles.json`. 운영자가 허용한 target 파일·Terraform state·SSH·선택적 배포 설정을 참조한다. |
| `RAILSHOT_STATE_DIR` | `BASE`. API 작업 저장소이며 환경 실행 상태는 `BASE/environments`에 저장한다. |
| `RAILSHOT_TARGET_ID`, `RAILSHOT_TARGET_IDS` | 기본 CI 대상과 쉼표로 구분한 허용 대상 ID 목록. 새 runtime `stack-aws-1002`를 허용 목록에 포함한다. 현재 서버는 CI 서비스 초기화에 기본 ID와 `GITHUB_TOKEN`도 요구한다. |
| profile의 `state_root` | `BASE/terraform`. VM별 저장된 plan·manifest·state를 유지한다. |
| profile의 SSH 참조 | 사용자 `railshot-operator`, identity `/Users/mango/.ssh/id_ed25519`. `enroll_ssh=true`이면 인증된 provider API로 검증한 공개 host key를 환경별 known_hosts 파일에 생성한다. |
| 실행 파일·권한 | Python/Terraform/Ansible과 provider CLI가 있는 단일 실행자. 디렉터리는 0700, 비공개 설정·상태·키 파일은 0600, 현재 실행 OS 사용자 소유로 준비한다. |

컨테이너로 옮길 때에는 profile에 저장한 절대 경로가 컨테이너 안에서도 같은 파일을 가리키도록 mount하고, 환경·Terraform·API 상태에는 필요한 쓰기 권한을 제공한다. 자격은 런타임의 비공개 파일·환경으로 공급하며 이미지나 HTTP 입력·응답에 포함하지 않는다. 원격 API를 활성화하려면 단일 실행자와 billing ledger의 권위를 먼저 인수해야 한다. 로컬 `billing.sqlite3`를 복제하고 양쪽 실행자를 동시에 가동하는 방식은 지원하지 않는다. 현재 문서 검증 상태는 `not_deployed`이며 VM 실행·DB readiness·대상 등록·migration·공개 앱 HTTP 검증의 live 완료 여부는 각각 실행 기록으로 확인해야 한다.

기존 `POST /api/deploy`와 `GET /api/runs/{run_id}`는 응답 필드와 `x-jasmin-request: deploy` 계약을 유지한다. 실제 등록 서비스에서는 새 영속 접수·admission·run binding을 공유하므로 이 workspace에서 접수하지 않은 과거 또는 외부 run ID는 조회하지 않는다. v1 배포와 달리 legacy deploy는 CI 제출이다.

현재 운영 메트릭, 수집 시각과 실패 상태는 [제품 관측 계약](observations.md)을 따른다.
