# Session-scoped product API

이 문서는 `apps/api/src/server.js`, `product.js`, `environments.js`, `product-store.js`에 구현한 제품 API를 설명한다. 기계 판독 계약은 [product.openapi.json](product.openapi.json)이다. 이 계약의 문서·로컬 검증은 `not_deployed`이며 실제 클라우드 E2E 결과와 별도로 기록한다.

사용자 계정·로그인·팀원 allowlist는 없다. 개인 환경은 별도 장기 소유자 쿠키와 복구키로 유지하며 [개인 OpenStack 환경 호출 규격](personal-environments.md)에 추가 경로·삭제 정책·지원 조건을 정리한다. 익명 브라우저 세션별로 접수 기록·계획·화면 설정·연결 정보를 분리한다. 운영자 지정 공용 대상과 실행기의 동시 실행 한도는 공유한다. 세션·스키마·이관 계약은 [dashboard-sessions.md](dashboard-sessions.md)를 따른다. 공개 모드는 `RAILSHOT_PUBLIC_DEMO=1`, `RAILSHOT_ALLOWED_HOSTS`, `RAILSHOT_ALLOWED_ORIGINS`를 명시하며 브라우저 Bearer를 요구하지 않는다. 기본 로컬 모드와 별도 내부 운영자 모드는 `access.js`의 기존 경계를 사용한다. 실행용 GitHub·Provider·SSH 자격은 서버 설정에만 둔다. 별도 OpenStack 연결 정보 저장은 실행용 자격을 바꾸지 않는다.

### 실행 중 CI 관측

`GET /api/v1/deployments/{id}/events`는 소유 세션·소스·앱·대상·GitHub 실행과 현재 attempt를 검증한 뒤 실행 중인 GitHub Checks 기록을 반환한다. 대시보드의 작업 로그는 15초마다 조회한다. 실행기는 약 20초 간격으로 관측을 전달하고 API cache도 15초이므로 초 단위 즉시 전달을 보장하지 않는다. 진행 중 기록이 60초 이상 갱신되지 않으면 `stale=true`로 표시하며, 관측 지연을 실행 실패로 바꾸지 않는다.

`progress.latest`는 마지막으로 관측한 검사 단계 또는 SDK 진행 이벤트다. `progress.agent_budget`은 실행기가 선언한 활성화 여부와 합산 호출 한도다. 현재 정책은 초기 패키징 최대 1회와 오류 수정 최대 2회를 각각 제한하여 총 3회이며, 패키징이 필요 없는 앱의 수정 횟수는 최대 2회다. API는 이전 실행기가 선언하는 0·1·2회도 허용하고, 이 필드가 없으면 `null`이다. 선언 한도는 실제 사용 횟수가 아니다. 최근 60개 중 최초 이벤트가 빠져도 같은 run/attempt의 중앙 기록에 있으면 예산을 보존한다. 다른 attempt의 값을 재사용하지 않는다. `progress.sdk_invocations`는 확인된 SDK 실행 횟수이며 heartbeat만 있을 때는 `null`이다. SDK 실행 횟수를 모델 내부 호출 수나 과금 횟수로 해석하면 안 된다.

중앙 `timeline`에는 안전한 SDK 활동 종류·횟수·토큰 계수·갱신 시각을 보존한다. 프롬프트, 소스 본문, 명령, reasoning 원문은 허용하지 않는다. 조회 실패는 `unavailable`, 아직 CI 미접수는 `not_started`이며, 과거 기록은 현재 상태와 구분한다. 이 GET은 모델·CI·배포를 시작하지 않는다. `complete`는 관측 종료이며 CI 통과나 앱 배포 성공을 뜻하지 않는다.

| 자원 | 구현 경로 | 의미 |
| --- | --- | --- |
| 화면 선택 | `GET /api/v1/options` | 기존 환경의 클라우드(AWS/GCP)·온프레미스(OpenStack/Proxmox) 선택을 반환한다. provider에 배정된 대상이 CI 허용 목록과 CD 등록에 모두 있을 때만 available이다. |
| OpenStack 등록 | `POST /api/v1/registrations`, `GET /api/v1/registrations`, `GET /api/v1/registrations/{id}` | 운영자 Bearer로 인증하고 세션별 등록 ID를 SQLite에 저장한다. 생성 응답에서 무작위 일회성 토큰을 한 번만 반환한다. |
| 연계 토큰 재발급 | `POST /api/v1/registrations/{id}/tokens` | 같은 브라우저 세션에서 빈 JSON 객체로 요청하며 기존 토큰 만료 뒤에만 10분짜리 새 토큰을 발급한다. |
| 연계 토큰 접수 | `POST /api/v1/registrations/claim` | 고객 노드에서 입력한 토큰을 본문으로 받아 만료·재사용을 검사하고 원자적으로 한 번만 소비한다. 접수는 터널 연결을 뜻하지 않는다. |
| OpenStack 설치 파일 | `GET /api/v1/installers/openstack`, `GET /api/v1/installers/openstack/scripts`, `GET /api/v1/installers/openstack/bundles` | 저장소의 기존 `install.sh` 내용, 단독 파일, 필수 동반 파일 ZIP을 제공한다. |
| 토큰 포함 설치 파일 주소 | `GET /onpremise/install.sh?token=…` | 유효한 미사용 연계 토큰에 한해 같은 `install.sh`를 내려받는다. 다운로드만으로 토큰을 소비하거나 설치하지 않는다. |
| 토큰 입력 파일 | `GET /api/v1/installers/openstack/client` | 고객 노드에서 토큰을 숨겨 입력받아 접수 API에 보내는 독립 Python 파일을 제공한다. |
| 환경 관측 | `GET /api/v1/targets/{id}/observations` | 접근 가능한 등록 대상의 실제 노드·앱 지표와 수집 시각. 배포 이력 없이 조회하며 결측·실패는 null과 상태로 표시한다. |
| 대상 | `GET /api/v1/targets` | CI 서비스에 등록한 대상 목록. `ci_submission`·`application_deployment`를 구분한다. CD가 등록한 앱은 `application_name`과 `deployment_scope=registered_application`으로 표시한다. runtime 상태는 독립 관측이 없으면 unknown이다. |
| 빌드 | `POST /api/v1/builds`, `GET /api/v1/builds/{id}` | ZIP·폴더·공개 GitHub를 기존 CI로 제출한다. ID는 GitHub run ID 문자열이며 등록한 run만 조회한다. `published`는 검증한 이미지 게시다. |
| 배포 | `POST /api/v1/deployments`, `GET /api/v1/deployments/{id}` | 선택한 계획으로 환경 준비·대상 등록을 먼저 수행하거나, 이미 등록된 대상으로 바로 CI를 실행한다. CI 게시 결과를 검증한 후 CD 어댑터를 한 번 호출한다. 같은 source/target의 고정 revision 배포 및 기대 공개 HTTP 검증까지 확인해야 succeeded와 최상위 url을 반환한다. |
| 배포 재개 | `POST /api/v1/deployments/{id}/actions` | `{"action":"resume"}`만 받는다. 게시 완료 후 CD·HTTP 결과가 불확실한 기존 앱 배포를 같은 ID와 CI 산출물로 재개한다. |
| profile | `GET /api/v1/profiles` | 운영자가 등록한 환경 사양, 고정 대상 또는 생성 템플릿, 선택적 앱 이름·DB 역할 수와 지원 범위. 자격·로컬 경로는 포함하지 않는다. |
| 계획 | `POST /api/v1/plans`, `GET /api/v1/plans/{id}` | 검증·저장한 계획을 201로 반환한다. 계획은 VM 생성 결과가 아니다. |
| 환경 | `POST /api/v1/environments`, `GET /api/v1/environments/{id}` | 저장된 계획을 한 번 실행한다. 자원·guest·runtime, 선택적 Patroni DB와 배포 대상 등록을 각각 기록한다. 앱 소스의 CI·배포·공개 HTTP 검증은 이 요청에 포함하지 않는다. |
| 앱 작업 계획 | `POST /api/v1/applications/{id}/plans` | 현재 세션이 소유한 앱의 중지·시작·삭제 범위와 보존 자원을 읽어 10분간 유효한 계획을 반환한다. |
| 앱 작업 | `POST /api/v1/applications/{id}/operations`, `GET /api/v1/operations/{id}` | 확인한 계획을 한 번 실행하고 단계·결과·남거나 확인하지 못한 자원을 기록한다. |

### OpenStack 등록과 설치 파일 전달

브라우저는 `POST /api/v1/registrations`에 `provider: openstack`만 보낸다. OpenStack 프로젝트·사용자 ID 및 인증 방식은 이 단계에서 받지 않는다. 서비스는 32바이트 난수로 10분짜리 연계 토큰을 만들어 생성 응답의 `linkage_token`으로 한 번만 보여 준다. 토큰 원문은 DB에 저장하지 않고 해시만 저장한다. 상세·목록에도 토큰은 없다. 같은 브라우저 세션의 미완료 등록은 기존 토큰 만료 후 빈 JSON 객체를 보내 재발급할 수 있다. 기존 SQLite의 미사용 `auth_type`·`project_id`·`user_id`·`key_salt`·`key_hash` 열은 서버 시작 시 제거한다.

등록·재발급·조회 API는 공개 데모 모드에서도 운영자 Bearer를 요구한다. 운영 Nginx는 서버에서만 읽는 `RAILSHOT_API_TOKEN_FILE`을 `/api/` 프록시에 주입하고, 브라우저에는 이 토큰을 전달하지 않는다. HttpOnly 세션 쿠키가 등록 요청의 범위를 구분하지만 사용자 신원을 증명하지는 않는다. 접수 API는 고객 노드가 운영자 Bearer 없이 호출하며, 10분 유효한 일회성 연계 토큰 자체로 접수를 제한한다. 운영 배포에서는 이 API에 HTTPS로 접속해야 한다.

설치 파일 API는 저장소의 현재 `deployment/bootstrap/install.sh` 바이트를 단독 파일과 복사 가능한 코드로 제공한다. UI는 현재 접속 origin으로 `curl -fsSL 'https://서비스-주소/onpremise/install.sh?token=…' -o install.sh` 명령을 만들고, 이 주소는 미사용·미만료 토큰을 확인해 같은 스크립트를 반환한다. 토큰이 URL에 있으므로 중간 프록시의 요청 URL 기록에 남지 않도록 운영 설정을 확인해야 한다. ZIP에는 현재 저장소의 동반 파일과 `deployment/bootstrap/claim_token.py`가 포함된다. 단독 `install.sh`만으로는 로컬 실행 시 필요한 동반 파일이 준비되지 않는다. 고객은 노드에서 `python3 claim_token.py --service-url https://서비스-주소`를 실행해 토큰을 숨겨 입력한다. 접수 결과는 등록 ID와 접수 시각만 반환하며 OpenStack 인증정보를 보내지 않는다. 이 기존 연계 토큰 흐름은 `install.sh`의 개인 환경용 `--personal-registration` 모드를 자동 선택하지 않고 새 WireGuard 터널도 만들지 않는다. 따라서 발급·접수·파일 전달을 터널 연결 완료로 표시하지 않는다.

실행 목록은 `{items, next_marker, total}`, 나머지 목록은 `{items, next_marker}`이고 미설정 서버의 대상·profile 목록은 빈 목록이다. 목록에는 `limit`(1–100, 기본 20)과 해당 목록의 ID를 사용한 `marker`를 받는다. 개인 환경 목록에는 추가로 `scope=owned&provider=openstack`을 사용한다. 소스 다운로드는 `variant=submitted|deployed`를 받으며 그 밖의 경로는 query를 받지 않는다. 알려지지 않은 필드, 중복 단일 multipart 필드·query·JSON key는 거부한다. 파일은 최대 2,000개·총 100 MiB이며 원시 multipart 상한에는 framing용 1 MiB를 더한다. `files`만 반복할 수 있다.

### 기존 앱 업데이트

배포 내역의 등록된 앱에서 업데이트를 시작한다. `POST /api/v1/applications/{id}/updates`는 소스 multipart와 `Idempotency-Key`만 받고 앱·대상·환경 지정은 거부한다. ZIP·폴더·공개 GitHub 저장소가 바뀌어도 앱 ID, 환경, 기존 등록을 유지한다. 정상 등록된 `ready` 앱의 등록 절차를 반복하지 않는다.

미리보기는 CI를 시작하지 않고 `status=preview`, `stage=review`인 배포 기록과 30분 유효 스냅샷을 저장한다. `changes`에는 추가·수정·삭제 경로와 동일 파일 수를 반환한다. 새 입력은 전체 소스이며 생략한 기존 파일은 삭제 대상이다. 비교 기준은 마지막 검증 성공 배포의 최종 소스다. 과거 실행에 최종 소스가 없으면 `baseline_kind=submitted`, `source_comparison_only=true`로 제출 원본 비교임을 표시한다. 전송 실패나 무결성 검증 실패를 원본 비교로 대체하지 않는다.

`POST /api/v1/deployments/{id}/start`는 JSON `{ "rebuild": false }`로 저장된 스냅샷을 실행한다. 미리보기 이후 GitHub 기본 브랜치가 바뀌어도 다시 읽지 않는다. 기준 배포가 달라지거나 미리보기가 만료되면 새 검토를 요구한다. 검증된 최종 소스와 같으면 `unchanged`로 CI를 생략하며 `rebuild=true`로 명시적인 재빌드가 가능하다. 제출 원본만 비교한 경우에는 같아도 CI를 실행한다. 같은 미리보기의 시작 재요청은 최초 결과를 반환하고 중복 실행하지 않는다. 실행 결과가 불확실한 경우 자동 재전송하지 않는다.

앱 응답은 `current_deployment`, `latest_deployment`, `current_deployment_state`를 분리한다. 최신 CI 실패가 마지막 성공 배포를 덮지 않는다. 이후 CD 적용 결과가 불확실하면 현재 버전도 `unverified`로 표시하고 새 업데이트를 차단한다. 공용 실행기가 사용 중이면 409로 접수를 거부하며 대기열에 추가했다고 표시하지 않는다. 같은 세션에는 차단 중인 앱·단계·마지막 갱신 시각을 안내하지만 다른 세션의 실행 정보는 공개하지 않는다.

`GET /api/v1/deployments/{id}/source?variant=submitted`는 서버에 고정한 원본, `variant=deployed`는 검사에 사용된 최종 소스를 ZIP으로 제공한다. 최종 소스는 같은 CI 실행과 게시 attempt의 `source-{attempt}` artifact에서 읽으며, 게시물의 gate source digest와 경로·유형·권한·파일 내용을 다시 검증한다. release 단계만 재시도하면 이전 loop의 정확한 source artifact를 확인하여 새 게시 attempt에 연결한다. 원본 소스와 배포 소스를 서로 대체하지 않으며, 최종 소스가 보관되지 않은 실행이나 만료된 artifact는 다운로드할 수 없다. 이 소스 교체·다운로드는 DB·볼륨·운영 자격 변경을 포함하지 않는다.

소스 접수는 multipart의 `app`, `target_id`와 공개 GitHub URL(`repository_url`), ZIP(`archive`), 폴더(`files`와 JSON 문자열 배열 `paths`) 중 하나를 받는다. `source_type`은 생략할 수 있으며 지정하면 실제 소스 형식과 일치해야 한다. `plan_id`는 `POST /api/v1/deployments`에서만 선택적으로 받는다. 빌드·legacy deploy에는 허용하지 않는다. 계획을 포함한 배포의 `app`·`target_id`는 계획의 이름·`runtime_target_id`와 일치해야 하고, 해당 profile에 배포 등록 설정이 있어야 한다. 계획이 없는 배포는 서버 CD 설정 또는 성공한 환경 등록 기록의 대상·앱을 사용한다. 성공한 환경의 재배포는 저장한 CD 설정을 재사용하며 VM·DB 생성은 반복하지 않는다. 대상·앱·계획·환경 ID가 일치한 성공 기록만 재시작 후 CI 허용 대상으로 복원한다.

기존 환경을 선택하는 화면은 같은 배포 경로에 `app`·`target_id` 대신 `environment`·`provider`와 `source_name`을 보낼 수 있다. 폴더는 `source_name`이 필수이며 ZIP은 파일명, GitHub는 저장소명에서 이름을 얻을 수 있다. 화면·API·CLI는 `contracts/application.mjs`의 정규화 규칙을 공유하며 이름이 없거나 유효하지 않으면 거절한다. `apps/agent` MCP는 앱 이름과 등록된 대상 ID를 명시적으로 받는다. 서버는 등록된 provider와 CI/CD 연결에서 대상만 선택하고 앱 이름은 소스에서 정한다. `RAILSHOT_APPLICATIONS_FILE`에 등록된 환경은 업로드에서 앱 등록을 자동으로 생성한다. 기존 고정 앱 대상은 이름이 다르면 소스 취득·CI 전에 거절하며 샘플 앱 이름으로 바꾸지 않는다. 환경과 고정 앱 등록이 모두 없으면 미연결 provider로 거부한다. 이 선택 방식에는 `app`, `target_id`, `plan_id`를 함께 보낼 수 없다. 새 앱·DB 환경을 만드는 화면은 profiles로 계획을 만든 뒤 그 계획의 `name`, `runtime_target_id`, `id`를 각각 `app`, `target_id`, `plan_id`로 제출한다.

profile의 `target_id`는 고정 runtime 대상 또는 새 대상 이름의 기준이다. `create_per_request=true`이면 사용자 앱 이름과 계획 ID의 SHA-256 앞 8자리로 runtime·DB 대상, namespace와 GitOps 경로를 한 번 파생한다. 이때 `application_name`은 null이며 최종 요청은 계획의 `runtime_target_id`를 사용한다. false이면 기존 고정 대상·앱을 유지한다. 운영자 설정 `registration_max_age_seconds`(1800–604800)는 계획 준비 시 비공개 등록 만료 시각을 한 번 정하며 재검증으로 연장하지 않는다. 이 만료는 VM 종료나 비용 상한을 보장하지 않는다. `deployment_supported`는 배포 설정 유무, `supported`는 provider·runtime 용도·운영자 실행 허용 여부를 나타낸다. 두 값 모두 생성·등록·준비 완료를 뜻하지 않는다. `database`는 null 또는 `{mode:"patroni", required, database_nodes, dcs_voters, proxy_nodes}`다. `required`는 등록된 앱 대상의 DB binding 필요 여부다. DB와 DCS 역할은 같은 VM에 둘 수 있으므로 역할 수를 더한 값이 VM 수는 아니다.

계획 입력은 `{name, runtime:{profile_id,node_count}, database:{mode,placements?}}`다. `name`은 3–30자이며 소문자로 시작하고 소문자·숫자·하이픈을 사용하며 소문자나 숫자로 끝난다. 현재 실행 가능한 runtime은 운영자가 등록한 AWS/GCP amd64 VM 한 대다. `node_count` 입력 범위는 1–64이지만 1이 아니면 `SINGLE_NODE_ONLY`로 실행을 차단한다. `database.mode=none`은 placements를 생략하거나 빈 배열로 지정한다. `standalone`은 입력으로 받되 `STANDALONE_DATABASE_UNSUPPORTED`로 차단한다.

`database.mode=patroni`에는 `placements`가 필요하다. 각 항목은 `{profile_id,database_nodes,dcs_voters,proxy_nodes}`이며 해당 profile에 등록한 역할 수와 정확히 일치해야 한다. 배열은 최대 16개, 각 역할 수는 0–31이며 profile 중복은 차단한다. 합산 구성에는 DB 노드 2개 이상, 홀수인 DCS voter 3개 이상, proxy 1개 이상이 필요하다. DB와 proxy는 같은 VM에 배치할 수 없다. 이 입력은 운영자가 이미 허용한 VM·네트워크 구성을 선택하며 클라우드 자격·SSH 경로·임의 provider 변수를 받지 않는다. 서로 다른 provider의 profile을 선택해도 클라우드 간 네트워크 연결을 자동으로 만든다는 뜻은 아니다.

배포 설정이 있는 runtime profile은 등록 대상의 DB binding 필요 여부와 요청의 `database.mode=patroni` 여부가 일치해야 한다. 불일치하면 Terraform 계획을 실행하기 전에 `DATABASE_BINDING_PROFILE_MISMATCH`로 차단한다. 배포 설정이 없는 profile에서는 DB 준비를 독립적으로 선택할 수 있다.

계획은 준비 완료 시점부터 최대 15분간 유효하며 비용 견적 만료가 더 빠르면 그 시각에 만료된다. 계획은 입력·정책·profile·대상 snapshot과 각 VM의 저장된 Terraform plan digest에 묶인다. 기존 자원의 수정·삭제 없이 새 자원 생성만 허용한다. 실행 차단 이유는 `blockers`, 실행 가능 여부는 `executable`로 반환하고 `cost`는 자동 예산 갱신을 설정한 AWS profile에서 최신 청구·가격 관측을 바탕으로 반환하며 그 외에는 null이다. 비공개 `budget_refresh:{}`는 보존 디스크를 월말까지 계산한다. `budget_refresh:{retained_storage_hours:24}`는 정리 책임이 지정된 합성 검증에 한해서 쓰는 24시간 견적 정책이며 자동 삭제 기능이 아니다. 기존 원장의 held 금액, 미보고 비용, 월 한도는 유지하고 계정·통화 불일치나 수집 실패·예산 초과 시 Terraform 계획 전에 차단한다. `steps`는 예정된 작업 목록이며 실행 증거나 실제 `stage` 전환 기록은 아니다. 한 계획은 환경 생성과 계획을 포함한 배포 중 한 번만 소비할 수 있다.

계획을 포함한 배포는 `plan_id`, `environment_id`, `environment`를 반환하고 최초 `stage`가 `environment`다. `environment_id`는 배포 UUID 뒤에 `.environment`를 붙인 내부 실행 식별자이며 별도 `GET /api/v1/environments/{id}` 자원으로 등록하지 않는다. 진행 상태는 배포 객체의 `environment`에서 읽는다. 초기값은 `{status:"queued"}`이며 실행 중 자원·guest·runtime·DB·binding 결과가 추가된다. 환경이 succeeded이고 대상 등록까지 확인된 뒤에만 CI를 제출한다.

환경의 `resources.nodes`는 모든 자원 생성이 성공한 뒤 반환하는 `{target_id,provider}` 목록이다. VM 생성만으로 guest·runtime·DB 준비를 증명하지 않는다. `database`는 Patroni 실행 결과와 비공개 binding 파일 digest를 확인한 뒤 succeeded가 된다. `binding`은 그 binding을 사용하는 배포 대상 등록 단계이며 등록 성공 후에만 환경의 `deployment_supported=true`가 된다. DB를 생략하면 `database.status=skipped`이고, 배포 설정이 없으면 runtime 또는 DB 준비가 성공해도 `deployment_supported=false`다. 환경의 `stage`는 성공 후에도 마지막 단계인 runtime·database·binding 등으로 남을 수 있다.

배포의 `cd.migration`은 migration Job을 관측한 경우에만 존재하며 `{name,state:"succeeded"|"unverified"}`다. migration 성공, DB 준비, 대상 등록, 앱 배포와 공개 HTTP 성공은 각각 별도 결과다. 최상위 배포 성공은 `cd.deployed=true`, 고정 revision과 성공한 공개 HTTP 관측을 모두 요구한다. 환경 생성의 성공이나 profile 설정만으로 앱 URL을 성공 결과로 제공하지 않는다.

비동기 접수는 화균 님의 `{resource_id, action:"create", status:"accepted", request_id}` 형식과 `202`, `Location`, `Retry-After: 2`, `X-Request-ID`, `Cache-Control: no-store`를 사용한다. 오류는 `{error:{code,message,request_id,retryable,outcome_unknown}}`다. 매 HTTP 요청마다 새로운 request ID를 생성한다. 작업에 저장한 오류 ID와 Ansible의 `ansible_job_id`는 별개다.

`EXECUTOR_BUSY`는 요청을 접수하지 않은 HTTP 409이며 `error.admission`을 추가한다:

```json
{"scope":"workspace","accepted":false,"reason":"reconciliation_required"}
```

`reason`은 기존 작업 실행 중이면 `execution_in_progress`, 기존 결과가 불명확하면 `reconciliation_required`다. 후자는 `retryable=false`이며 시간 경과로 자동 해제하지 않는다. 차단 작업이 정확히 같은 세션 소유일 때만 `blocking_operation`에 `id`, `kind`, `app`, `status`, `stage`, `updated_at`을 제공한다. 다른 세션 및 소유자 없는 legacy 작업의 상세는 생략한다. 이 계약은 신규 실행, 업데이트 시작, 환경 생성, CD 재개, 앱 lifecycle 계획·실행에 공통 적용한다. CD 재개는 자신의 기존 작업만 검사 대상에서 제외하며, 명시적 삭제의 취소 대상 예외는 아래 lifecycle 계약을 따른다.

`outcome_unknown`은 **이번 HTTP 요청**의 결과 불명 여부다. 기존 작업이 unknown이어도 접수 거절은 `outcome_unknown=false`다. 프론트엔드는 확정된 409 거절에 “이미 처리됐을 수 있음”을 덧붙이지 않는다. 응답 유실·통신 오류 또는 `outcome_unknown=true`에는 같은 요청 키를 보존한다. 거절된 요청은 자동 실행 대기열에 들어가지 않는다. legacy API 오류 형식은 유지한다.

앱의 `environment_target_id`는 실행 환경, `id`/`target_id`는 앱 등록·배포 binding이다. 환경 필드가 없는 legacy 기록은 “배포 대상”으로 표시하며 앱 target을 환경 ID로 추정하지 않는다. 전체 경계와 Argo 구조는 [앱·환경·실행 관리 계층](../architecture/application-management.md)을 따른다.

배포·환경 생성에는 `Idempotency-Key`가 필요하다. 같은 세션·자원 종류 안에서 같은 키와 입력은 같은 ID를 반환한다. queued/running이면 202, 완료·실패·차단·unknown이면 200 자원 객체와 Location이다. 같은 키로 입력을 바꾸면 409이며 배포의 `plan_id`와 환경·provider 선택도 입력에 포함한다. ZIP의 압축 시각·multipart boundary는 파일 의미에 포함하지 않으며 폴더 파일 순서도 정규화한다. GitHub URL의 첫 SHA·소스 snapshot은 고정하고 같은 키의 재요청에서 다시 다운로드하지 않는다. 빌드 생성에는 이 멱등 계약이 없으므로 응답 유실 시 자동 재전송하지 않는다.

배포 재개는 `POST /api/v1/deployments/{id}/actions`에 `application/json` 본문 `{"action":"resume"}`를 보낸다. 기존 배포와 앱 등록 모두 현재 쿠키 세션 소유여야 하며, 앱은 `ready`, 배포는 `unknown`·`stage=cd|http`, CI는 원래 `published`여야 한다. 삭제·다른 lifecycle 작업이 시작된 앱과 다른 active 작업이 있으면 거절한다. 소스·앱·target·환경·CI 값을 요청으로 교체하거나 query로 전달할 수 없다. 쿠키 없는 유지보수 요청에도 소유권 예외를 두지 않는다.

접수는 기존 deployment의 `Location`, `Retry-After: 2`, `X-Request-ID`와 `202 {resource_id, action:"resume", status:"accepted", request_id}`를 반환한다. 별도 `Idempotency-Key`는 요구하지 않으며, 같은 배포를 동시에 재개하면 먼저 영속 admission을 얻은 요청만 실행하고 나머지는 409다. `resume_count`와 UTC `resumed_at`을 기록한다. CI run·source commit·게시 artifact ID·producer attempt·image digest가 원래 증거와 같은지 확인한 뒤에만 관측을 갱신한다. 불일치는 `RESUME_PUBLICATION_CHANGED` 또는 기존 binding 오류로 중단하고 원래 CI 증거를 보존한다.

재개는 앱 등록과 CI 접수를 반복하지 않는다. 기존 native journal을 유지하므로 Terraform의 불확실한 apply, DNS 생성 intent, GitOps push·sync를 무조건 다시 실행하지 않는다. 확인 가능한 결과를 관측하거나 아직 실행하지 않은 단계를 진행하며, 결과를 확정할 수 없으면 `unknown`이 유지된다. HTTP 단계 재개 중에도 이미 검증된 CD revision을 새 관측 전까지 보존한다. 다른 세션은 404, 재개 대상이 아닌 상태는 `409 DEPLOYMENT_NOT_RESUMABLE`, 내부 연결 불일치는 `409 RESUME_BINDING_MISMATCH`다.

`RAILSHOT_STATE_DIR`는 저장소 밖의 전용 영속 디렉터리로 설정한다. 기본값은 사용자 홈의 `.local/state/railshot`이다. 최종 디렉터리와 state 파일은 현재 OS 사용자 소유이며 다른 사용자 접근 권한과 심볼릭 링크를 거부한다. 소스는 0600 snapshot, 작업·계획·run binding·멱등 키는 dashboard.sqlite3의 WAL 트랜잭션(synchronous=FULL)으로 저장한다. 기존 state.json은 첫 기동에 한 번 이관하고 이후 갱신하지 않는다. snapshot과 의도를 저장한 뒤에만 CI·CD·환경 실행을 시작한다. 기본 보관 상한은 작업 100개, 계획 100개, 소스 snapshot 512 MiB다. 상한 도달 시 409를 반환하며 자동 삭제하지 않는다. 운영자는 작업을 확인하고 보관·정리 정책을 적용해야 한다.

한 API 프로세스가 한 로컬 저장소를 소유한다. 프로세스 ID와 시작 식별자로 같은 호스트의 중복 사용을 막는다. 이 lock은 여러 호스트의 분산 잠금이 아니므로 API replica는 1개로 운영하고 Recreate 전략으로 이전·새 컨테이너의 동시 쓰기를 피한다. 서로 다른 PID namespace와 여러 호스트의 동시 writer는 지원하지 않는다. 미완료 작업과 unknown은 하나의 admission 한도를 공유한다. 재시작 시 queued/running을 unknown으로 저장하고 작업을 자동 재실행하지 않는다. 외부 실행 결과 유실이나 저장 실패도 성공으로 표시하지 않는다. 등록된 앱의 게시 완료 이후 CD·HTTP 구간은 아래 명시적 재개 계약을 사용할 수 있다. 그 밖의 unknown은 운영자가 GitHub·CD·환경 기록을 확인해야 한다.

CI는 `GITHUB_TOKEN`, 등록 대상 ID 및 기존 GitHub 저장소 설정을 사용한다. 기존 대상의 CD는 `RAILSHOT_CD_CONFIG`의 비공개 고정 설정과 검증한 publication 파일만 받는다. 환경은 `RAILSHOT_PROFILES_FILE`과 서버의 Python/Terraform/Ansible 도구를 사용하며, 새 대상의 CD 설정은 해당 환경의 등록 결과에서 읽는다. 현재 코드 경로는 AWS/GCP의 단일 runtime, 선택적 Patroni DB, 배포 대상 등록과 CI/CD를 연결한다. profile 등록·SSH·네트워크·실행 도구가 준비됐다는 사실과 실제 클라우드·DB·공개 앱 검증 성공은 구분한다.

서버 설정·상태의 정본은 단일 API 실행자의 `/var/lib/railshot` 영속 볼륨에 둔다. 초기 인증·설정 이관 후 사용자 요청은 서버 도구로 실행하며 개발자 노트북의 개인 SSH/AWS 키에 의존하지 않는다.

| 설정·참조 | 값·운영 조건 |
| --- | --- |
| `RAILSHOT_PROFILES_FILE` | `/var/lib/railshot/config/app-db/profiles.json` |
| `RAILSHOT_STATE_DIR` | `/var/lib/railshot/state`; 환경 실행은 그 아래 `environments` |
| profile `state_root` | `/var/lib/railshot/terraform`; VM별 saved plan·manifest·state |
| budget `ledger_path` | `/var/lib/railshot/billing/billing.sqlite3`; 초기 이관 후 서버만 기록 |
| SSH identity | `/var/lib/railshot/config/app-db/id_ed25519`; 전용 키. `enroll_ssh=true`이면 인증된 provider API로 host key를 검증하여 환경별 known_hosts 생성 |
| AWS 자격 | 플랫폼 인스턴스 역할의 갱신 가능한 자격. 새 VM은 검증된 기존 instance profile을 재사용하고 exact-role PassRole 필요 |
| Terraform cache | 서버 Linux용 공유 provider cache. 다른 OS의 `.terraform` 바이너리를 이관하지 않음 |

디렉터리는 0700, 비공개 파일은 0600, API OS 사용자 소유여야 한다. 기존 API·Terraform·edge·budget writer 중지, SQLite backup·integrity·행 digest와 Terraform lineage 확인 후 단일 정본을 이전한다. 개인 자격·비밀을 이미지나 HTTP에 포함하지 않는다. 이 설정 계약은 이관·실배포 완료 증거가 아니며 VM·DB readiness·대상 등록·migration·공개 HTTP 결과는 각각 실행 기록으로 확인한다. GCP의 서버 자격과 자동 billing 수집, AWS↔GCP 사설 연결은 별도 준비·검증이 필요하다.

기존 `POST /api/deploy`와 `GET /api/runs/{run_id}`는 응답 필드와 `x-railshot-request: deploy` (legacy: `x-jasmin-request: deploy`) 계약을 유지한다. 실제 등록 서비스에서는 새 영속 접수·admission·run binding을 공유하므로 다른 세션에서 접수했거나 이 workspace에 없는 run ID는 원격에서 조회하지 않는다. CLI는 호스트별 쿠키를 `~/.local/state/railshot-client`의 0600 파일에 저장하며 `RAILSHOT_CLIENT_SESSION_DIR`로 위치를 지정한다. `apps/agent` MCP는 v1 배포를 사용하고 쿠키를 `RAILSHOT_AGENT_SESSION_DIR`에 보관한다. 쿠키 없는 비공개 localhost 유지보수만 기존 공유 범위를 유지한다. v1 배포와 달리 legacy deploy는 CI 제출이다.

현재 운영 메트릭, 수집 시각과 실패 상태는 [제품 관측 계약](observations.md)을 따른다.

신규 edge 등록을 사용한 배포는 공개 검증 성공 시 `public_http.site_url`과 `public_http.receipt`를 추가한다. `url`은 검증한 health 경로이고 `site_url`은 HTTPS 200을 확인한 앱 경로다. 제품 최상위 `url`은 `site_url`이 있으면 이를 사용하고 기존 고정 앱은 health URL을 유지한다. receipt는 deployment·target·tenant·app·environment·namespace, source/Git revision, image/route/plan digest, 만료 정책과 DNS/TLS/target health 결과를 연결한다. IP·자격·응답 body는 공개 receipt에 넣지 않는다. 검증되지 않은 결과에는 이 선택 필드가 없다.

대시보드는 소스와 `environment=cloud|onprem`, `provider=aws|gcp|openstack|proxmox`를 기존 배포 endpoint로 보낸다. 이 모드는 `app`·`target_id`와 함께 사용할 수 없다. API가 운영자의 `RAILSHOT_PROVIDER_TARGETS` 매핑과 CD 등록에서 대상·앱을 결정하며, 등록 앱이 없으면 GitHub/ZIP/폴더 이름에서 유효한 앱 이름을 생성한다. 폴더명은 선택적 `source_name`(1–255자, 제어 문자 금지)으로 전달한다. 알 수 없는 provider와 잘못된 조합은 422, 연결되지 않은 선택은 409이며 다른 대상으로 대체하지 않는다. 기존 app/target_id 요청과 builds API는 유지한다.

화면에서 사용자가 고른 환경은 늦게 도착한 저장 설정으로 덮어쓰지 않는다. 검토 후 선택값이 달라지면 다시 검토해야 한다. 대시보드는 앱 조회에서 확인한 환경 대상 ID를 `expected_target_id`로 함께 보내며, 서버의 현재 provider 대상과 다르면 소스 취득·접수·CI 전에 409 `DEPLOYMENT_TARGET_CHANGED`로 거부한다. 이 필드는 환경 선택 방식에서만 허용하며 이전 클라이언트에는 선택적이다. 신규 배포 기록의 `deployment_selection`은 접수한 `environment`·`provider`를 보존하고 `environment_target_id`는 실제 등록 환경을 나타낸다.

현재 공개 플랫폼 manifest는 AWS 기본 대상에 `RAILSHOT_TARGET_PROVIDER=aws`를 명시한다. 추가 provider는 선택 JSON 설정 `RAILSHOT_PROVIDER_TARGETS={"gcp":"k3s-gcp","openstack":"k3s-openstack"}`으로 연결하며 기본 AWS 대상은 유지한다. 추가 ID가 `RAILSHOT_TARGET_IDS`의 CI 허용 목록과 CD adapter의 등록 대상 양쪽에 있을 때만 `/api/v1/options`에서 사용 가능하다. 선택은 해당 대상의 등록 앱으로 바인딩되며 다른 provider로 대체하지 않는다. 잘못된 매핑, 기본 provider의 대상 교체, 같은 ID의 여러 provider 선언은 거부한다. provider가 명시되지 않은 대상은 ID 문자열로 종류를 추측하지 않는다. UI의 클라우드/온프레미스 카드와 provider 선택은 그대로 유지한다.

플랫폼 릴리스는 같은 이름의 선택 Actions 변수를 `--provider-targets`로 전달하고, 매핑과 기본 AWS를 포함한 CI 목록을 이미지 선언에 함께 보존한다. 기본값은 빈 매핑이다. 먼저 [런타임 등록](runtime-registration.md), CI 대상·앱 바인딩과 API의 CD 등록을 완료한 뒤 이 변수를 설정한다. OpenStack은 기존 서버·K3s의 등록과 재배포 경로이며, AWS/GCP의 Terraform 기반 신규 환경 생성 범위를 확장하지 않는다.

`GET /api/v1/builds`, `/api/v1/deployments`, `/api/v1/environments`는 현재 세션의 실행 요약 목록을, `GET /api/v1/plans`는 현재 세션의 계획 목록을 반환한다. 배포 내역은 서버 목록으로 복원하며 localStorage의 기존 마지막 실행 ID를 사용하지 않는다. 세션·설정·OpenStack 연결 API는 [별도 명세](dashboard-sessions.md)에 정리했다.

각 대상의 CD 설정은 별도 Kubernetes API, AppProject, namespace, GitOps 경로, pull Secret 참조 및 공개 health URL을 등록한다. CI 게시 결과의 target ID와 요청의 target ID가 다르면 Git push나 Argo sync 전에 거부한다. 같은 빌드·게시 코드를 사용해도 배포 선언과 클러스터 자격은 해당 대상에 묶인다. GCP의 네이티브 LB나 OpenStack의 공개 경로 준비 여부는 별도 운영 검증이며 옵션 표시만으로 완료를 뜻하지 않는다.

앱 자동 배포의 시작점은 `POST /api/v1/deployments`다. API가 소스 commit을 만들고 `railshot-deploy.yml`을 dispatch하면, CI gate·이미지 게시 성공 뒤 제품 worker가 CD를 호출해 고정 Git revision을 Argo에 적용하고 공개 HTTP를 확인한다. `POST /api/v1/builds`는 게시에서 끝난다. 현재 앱 workflow에는 push/PR 자동 배포 trigger가 없으므로 저장소 수정·병합만으로 이 제품 배포 경로가 시작되지는 않는다.

실패 CI의 세부 단계와 검증된 원인은 `steps[].tasks`, `ci.diagnostics`로 전달한다. 결과 불확실 상태는 `unknown`으로 유지한다. 앱 로그 조회는 `GET /api/v1/deployments/{id}/logs`이며 세션·현재 배포 버전·런타임 소유권 검증을 거친다. 상세 제한은 [제품 관측 계약](observations.md)을 따른다.

### 기존 환경과 앱 등록 분리

`RAILSHOT_APPLICATIONS_FILE`은 기존 Ansible registry의 환경 ID와 CD 기반 설정을 연결하는 비공개 운영자 JSON이다. API는 업로드에서 실제 앱 이름을 얻고 `(environment_id, tenant, app)`으로 앱 binding ID를 결정한다. 환경에는 앱 이름을 넣지 않는다. `GET /api/v1/targets`는 `deployment_scope=environment`, `GET /api/v1/applications[/{id}]`는 현재 세션의 앱 등록을 반환한다. 처음에는 앱 목록이 비어 있다.

같은 앱 재배포는 namespace·NodePort·Argo project·GitOps 경로를 재사용한다. 다른 앱은 별도 등록을 만든다. `deployment.target_id`는 앱 binding이고 `environment_target_id`는 기존 노드다. CI 게시물·CD·로그는 앱 binding을 사용하며 노드 관측만 환경 ID를 사용한다. 다른 세션의 같은 이름은 409이며 다른 세션의 등록 상세는 404다.

### 앱 중지·시작·삭제

`POST /api/v1/applications/{id}/plans`는 `{action:"stop"|"start"|"delete"}`를 받아 현재 세션의 앱 계획을 201로 반환한다. 응답은 `{id, application_id, action, plan_hash, resources, retained, expires_at}`이며 `resources`와 `retained`는 `{kind,name,namespace?}` 목록이다. 삭제할 앱의 고정 등록·소유권·경로를 읽고, 계획 ID·SHA-256·운영자 설정·앱 상태를 저장한다. 계획은 10분간 유효하며 환경 생성용 `/api/v1/plans` 목록에는 포함하지 않는다. 공개 입력에는 앱 ID와 허용된 작업만 받으며 자격·파일 경로·임의 명령을 받지 않는다.

확인한 계획은 `POST /api/v1/applications/{id}/operations`에 `{action,plan_id,plan_hash,confirmation,delete_data?}`와 `Idempotency-Key`를 보내 실행한다. `confirmation`은 현재 앱 이름과 정확히 같아야 한다. 삭제에는 `delete_data:true`가 필수이며 중지·시작은 이 필드를 생략하거나 false로 보낸다. 대시보드의 삭제 경고에서 한 번 더 삭제를 누르면 앱 이름과 데이터 삭제 동의를 전달한다. 별도의 이름 입력이나 체크박스는 요구하지 않는다.

앱 작업은 초기 202와 완료된 동일 요청의 200 모두 **작업 객체를 직접 반환**한다. 기존 배포·환경 생성의 accepted envelope는 유지한다. 앱 작업 응답의 `id`, `application_id`, `action`, `status`, `stage`, `steps`, `residuals`, `retained`, `error`로 화면을 갱신하고 `Location: /api/v1/operations/{id}`에서 조회한다. 202에는 `Retry-After: 2`, 모든 응답에는 새 `X-Request-ID`와 `Cache-Control: no-store`가 있다. `steps`는 `{name,status}` 목록이며 `residuals`는 남아 있거나 부재를 검증하지 못한 자원의 식별자다. 비공개 계획·경로·자격·하위 명령 출력은 반환하지 않는다.

중지는 앱 실행을 멈추고 데이터를 보존하며 `ready → stopping → stopped`, 시작은 명시적으로 재개해 `stopped → starting → ready`, 삭제는 확인한 앱 소유 자원과 데이터를 제거해 `ready|stopped → deleting → deleted`로 전환한다. 삭제 기록은 tombstone으로 남긴다. stopped·deleted 앱으로 새 소스 배포를 보내면 409이며 중지한 앱은 먼저 시작해야 한다. 앱 삭제는 공용 노드·K3s·Cilium·공용 ingress를 제거하는 환경 삭제가 아니다. 실행 실패나 결과 유실은 앱을 unknown으로 남기며 운영자가 실제 기록을 확인하기 전에는 새 작업을 허용하지 않는다.

배포 중에도 같은 앱에는 삭제를 접수할 수 있다. 서버는 `deletion_requested`를 먼저 영속 저장해 새 CI/CD 진행을 막고 기존 worker의 종료를 기다린다. CI가 시작됐다면 저장한 저장소·workflow·source SHA·run attempt에 묶인 실행에 취소를 한 번 요청하고 완료를 다시 확인한다. CD가 진행 중이면 그 실행 결과와 이후 실제 Argo 상태가 확인돼야 정리할 수 있다. 작업을 멈춘 후 새 비공개 계획을 만들며, 처음 구체적으로 확인한 삭제 범위가 늘었으면 `APPLICATION_PLAN_CHANGED`로 차단한다. 취소 응답 유실·타임아웃·원격 상태 불확실은 정리 실행 전에 unknown으로 남긴다.

같은 앱의 배포가 진행 중이면 등록·CI·CD 단계 모두 정확한 앱 ID의 `ApplicationNamespace`·`ApplicationRoutes`를 삭제 범위로 확인한다. 이는 생성 완료 목록이 아니라 해당 앱에 한정된 삭제 승인 범위다. 운영자 설정 digest와 그 배포 ID에 고정하며, 진행 중인 등록·CD writer의 lock을 선점하지 않는다. 최대 30분 동안 worker 종료를 기다리고 배포가 멈춘 뒤 실제 Python 계획이 등록 binding·소유권을 검증해야 실행할 수 있다. 등록 의도 자체가 없다는 증명은 공용 등록 lock 안에서 검사하고, 일부만 등록된 불확실 기록은 자동 정리하지 않는다. 다른 앱의 진행 중 작업과 unknown은 계속 전역 실행 한도를 점유한다.

멱등 키는 세션·앱 작업 종류별로 관리한다. 같은 키·같은 본문은 원래 작업을 반환하고 입력이 달라지면 409다. 계획 만료·이미 소비한 계획·변경된 앱 상태와 다른 세션 접근은 실행 전에 거부한다. SQLite에 계획 소비·작업·앱 전이를 저장한 뒤 고정 Python 실행기를 한 번 호출한다. 재시작 시 queued/running 작업과 전이 중 앱은 unknown으로 복구하며 자동 재실행하지 않는다. 중지·시작·삭제 계획과 작업도 기존 보관 상한을 공유한다.

최초 `stage=registration`에서 실제 노드의 Service와 영속 예약을 읽어 NodePort를 할당하고 namespace·pull Secret·Argo 권한·CI binding을 연결한다. VM 생성이나 runtime 재설치는 하지 않는다. 등록 `ready`는 배포 완료가 아니다. `running/unknown` 등록은 재실행하지 않고 운영자 조정이 필요하다. CI의 정확한 source commit·앱·target·image digest를 검증한 뒤 실제 spec의 포트·health·route로 CD와 공개 경로를 준비한다. 앱 URL은 해당 revision/digest가 실행되고 실제 health와 서비스 경로가 HTTPS 200을 반환할 때만 제공한다.

AWS 경로 사전 검사가 등록 시작 전에 `APPLICATION_AWS_ROUTE_PREFLIGHT_FAILED`와 `outcome_unknown=false`를 반환하면 배포 operation은 `blocked`로 보존하고 앱 등록만 `queued`로 남긴다. 이 `queued`는 자동 실행 대기가 아니라 native 등록을 시작하지 않은 상태다. 운영자가 원인을 수정한 뒤 사용자가 새 `Idempotency-Key`로 명시적으로 업로드하면 같은 앱 binding으로 등록을 다시 시도한다. 기존 키 재요청은 원래 실패 기록만 반환한다. 다른 blocked 오류나 불확실한 등록에는 이 예외를 적용하지 않는다.

환경의 `ingress`는 `base_domain`, `edge_config_file`, `dns_config_file`을 참조한다. AWS는 기존 ALB에 앱 전용 target group·host rule과 해당 NodePort 권한을 추가하고 Cloudflare CNAME을 만든다. GCP는 기존 전역 IP·proxy·certificate map에 앱별 backend·인증서와 host rule을 추가하고 인증용 CNAME과 앱 A 레코드를 만든다. DNS writer는 `railshot:<application_id>` 소유 표식과 레코드 재조회를 확인하며 외부 소유 레코드를 덮어쓰지 않는다. DNS API 성공은 `https_verified=false`인 경로 준비 결과이고 배포 성공이 아니다.

운영 참조 파일과 Terraform state는 단일 API PVC에 둔다. `railshot-cloudflare` Secret의 `cloudflare-token`·`cloudflare.json`은 init container가 0600으로 복사한다. GCP WIF 설정은 `railshot-environments.google_credentials_file`로 참조하며 정적 서비스 계정 키를 이미지에 넣지 않는다. 기존 Terraform state의 lineage·resource ID를 유지하고 이전 writer를 중지한 뒤 이관한다. 원본 state 복사본으로 별도 apply하지 않는다.

현재 한계: AWS/GCP 자동 공개 경로만 연결돼 있다. OpenStack은 기존 환경의 앱 등록 코드를 공유하지만 공개 경로 writer가 연결되기 전에는 자동 배포 옵션을 차단한다. 같은 앱의 소스·이미지 재배포는 기존 등록을 사용하고, NodePort·health path 등 라우팅 계약 변경은 자동 수정하지 않는다. 기존 경로 변경에는 별도 검토가 필요하다. 실제 실행 상태와 미검증 항목은 [앱 자동 등록 검증 기록](../poc/application-registration-20261003.md)을 따른다.

### 실행 결과와 관측 상태

`ci.observation`과 `cd.observation`은 원본 시스템의 마지막 조회 시각(`checked_at`), 마지막 성공 조회(`last_success_at`), 조회 오류(`error`), 다음 조회 시각(`next_retry_at`)을 별도로 제공한다. 브라우저가 API를 읽은 시각과 다르다. 일시적인 GitHub/클러스터 조회 장애는 마지막 실행 사실을 유지하며, CI 재실행이나 CD 재적용 없이 재조회한다. 조회 사이에는 저장된 다음 조회 시각을 사용하여 작업자를 반납하고 다른 앱을 처리한다. 같은 앱의 대기 요청과 실제 변경 작업은 직렬 처리한다. 명시적인 조회 권한 거절이나 식별자 충돌은 원인을 가진 `blocked`로 표시하며, 이미 접수된 실행의 결과가 불확실하면 같은 앱의 변경은 계속 보류한다.

제품 요청은 소스가 동일한 재빌드도 요청 ID를 포함한 고유 커밋으로 기록한다. `dispatch.state=preparing`은 소스 준비, `requesting`은 영속 저장 후 외부 접수 요청, `accepted`는 run ID 저장을 뜻한다. `prepared_at`은 GitHub 접수 성공 증거가 아니다. 응답 유실 시 저장한 커밋·요청 ID와 workflow 실행 이름을 대조하여 기존 실행을 찾는다. 접수가 명시적으로 거절된 4xx는 `CI_DISPATCH_REJECTED`, 정상 조회로 5분 동안 실행을 찾지 못하면 `CI_DISPATCH_NOT_IDENTIFIED`이며 자동 재접수하지 않는다. 재시작 시 CI는 저장된 run ID를 조회하고, 게시가 확인된 고객 CD는 기존 `cd.json` 및 native journal을 읽기 전용으로 관측한다. CD 기록이 없으면 `CD_RECORD_MISSING`을 표시하며 라우팅 설정이나 앱 적용을 자동 재실행하지 않는다.

### 앱 관리 접수 확인과 목록 순서

앱 계획 GET은 해당 계획으로 접수된 작업이 있으면 `operation_id`를 반환한다. 대시보드는 삭제·중지·재개 응답을 잃어도 저장한 `plan_id`로 기존 작업을 찾아 조회하며 변경 요청을 다시 보내지 않는다. 앱 목록은 생성 시각 내림차순, 같은 시각에는 ID 내림차순이다. 실행 이력은 기존 생성 시각/ID 내림차순을 유지한다.

## 프로젝트 환경변수·설정 전달·환경 이전 (2026-10-04)

구현 경로는 `/api/v1/projects`이며 익명 쿠키 세션을 항상 적용합니다. 기존 앱 식별자를 유지하고 각 앱에 독립 프로젝트와 `binding_id`를 반복 실행 가능한 방식으로 연결합니다. 같은 이름의 앱을 프로젝트 하나로 합치지 않습니다. 최초 앱 배포 전에는 `POST /projects {name}`으로 프로젝트를 만듭니다.

- `GET /projects`, `GET /projects/{id}`: 해당 세션의 프로젝트 목록·상세입니다.
- `GET /projects/{id}/variables`: `project_id`, `revision_id`, `items`, `bindings`, `capabilities`, `blockers`를 반환합니다. `kind=plain`만 `value`를 반환하며 `kind=secret`은 `has_value`와 메타데이터만 반환합니다.
- `POST /projects/{id}/revisions`: `{base_revision_id, operations:[{operation:"set"|"delete",name,kind?,scope?,environment_id?,required?,value?}]}`입니다. 추가·수정·삭제를 하나의 트랜잭션으로 저장합니다. 생략한 value는 유지하며 빈 문자열과 명시적 삭제를 구분합니다. 비밀값을 일반값으로 바꾸려면 값을 명시적으로 다시 입력해야 합니다. 현재 버전 불일치는 `409 REVISION_CONFLICT`입니다.
- `POST /projects/{id}/deliveries`: `{binding_id,revision_id}`입니다. 검증된 소스가 있는 준비 앱만 접수합니다. 값을 준비한 뒤 기존 소스를 다시 게시하고 GitOps 배포 선언에 버전별 참조를 고정합니다. 앱·공개 응답·정확한 설정 버전까지 확인해야 성공입니다. 기존 앱이 실행하는 버전은 `bindings[].applied_revision_id`입니다.
- `POST /projects/{id}/transfers`: `{source_binding_id,destination_environment_id,revision_id}`로 10분 유효 계획을 만듭니다. 출발 환경 전용 변수는 도착 환경의 명시적 override가 없으면 `ENVIRONMENT_VARIABLE_REVIEW_REQUIRED`로 차단합니다. 기존 도착 앱은 덮어쓰지 않습니다.
- `POST /projects/{id}/transfers/{transfer}/actions`: `{action:"execute",plan_hash}`로 검토한 계획을 실행합니다. 기존 검증 소스를 새 대상에 다시 게시하고 설정 전달·앱 확인 후 활성 연결을 조건부로 바꿉니다. 원래 앱은 삭제하지 않습니다. `traffic_switch:"not_requested"`는 공개 트래픽 전환을 수행하지 않았다는 뜻입니다.
- `GET /projects/{id}/deliveries/{delivery}`, `GET /projects/{id}/transfers/{transfer}`: 상태를 조회합니다.
- `POST /projects/{id}/deliveries/{delivery}/actions` 또는 `.../transfers/{transfer}/actions`, `{action:"observe"}`: 결과 불명 작업을 읽기 전용으로 재확인합니다. 저장된 배포 식별자·게시 증거·설정 참조가 있어야 기존 CD 관측과 설정 검증만 수행하며 새 게시·배포·값 쓰기를 하지 않습니다. 증거가 부족하거나 검증 실패면 unknown과 전역 실행 제한을 유지하고 운영자 확인을 요구합니다.

모든 POST에는 `Idempotency-Key`가 필요합니다. 세션·프로젝트·작업 종류에 한정하여 보존하며 값이 든 입력의 중복 비교는 키가 있는 HMAC을 사용합니다. 원문 비밀값이나 값의 공개 해시를 저장하지 않습니다. 프로젝트 생성·설정 버전·이전 계획은 `201`, 실행 접수는 `202 + Location + Retry-After: 2`, 종료 작업 재조회는 `200`입니다. 저장·수정도 실행 중인 동일 프로젝트 작업과 충돌하며 실제 전달·이전은 기존 workspace 전역 실행 제한을 공유합니다.

`scope=common` 위에 `scope=environment`가 적용됩니다. 변수 이름은 영문 대문자·숫자·밑줄이고 첫 문자는 숫자가 될 수 없습니다. `PORT`, `DATABASE_URL`, `MIGRATION_DATABASE_URL`, `PGHOST`, `PGPORT`, `PGUSER`, `PGPASSWORD`, `PGDATABASE`, `PGSSLMODE`, `PGSSLROOTCERT`는 플랫폼 예약값입니다. 200개 변수, 값당 UTF-8 16 KiB, 1,000개 설정 버전을 상한으로 둡니다. 신규 배포 multipart 요청은 `project_id`와 `revision_id`를 함께 받습니다. 빌드 입력에는 비밀값을 보내지 않습니다.

### 관리 저장·운영자 구성·검증 범위

`RAILSHOT_PROJECT_KEY_FILE`은 환경·관리 데이터 디렉터리 밖에 운영자가 미리 둔 소유자 전용 JSON 파일입니다. 형식은 `{"version":1,"active_key_id":"key-v1","keys":{"key-v1":"<32-byte lowercase hex>"}}`입니다. 자동 키 생성·기본값은 없습니다. active key로 새 데이터 키를 감싸고 AES-256-GCM으로 값을 암호화합니다. 프로젝트·버전·변수 좌표를 인증 데이터로 결합합니다. 키 회전 시 과거 `key_id`를 보존해야 이전 버전을 읽을 수 있습니다. 키 파일·데이터베이스의 별도 백업·보존 책임은 운영자에게 있습니다. 현재 구현은 외부 마운트 키링이며 클라우드 KMS 서비스 직접 호출은 구현하지 않았습니다.

원본 암호문과 메타데이터는 같은 SQLite 트랜잭션에 저장하므로 별도 객체 저장과의 부분 커밋이 없습니다. API 단일 호스트·프로세스의 기존 운영 경계를 유지합니다. 서버 재시작으로 중단된 작업은 unknown으로 복구하며 임의 재실행하지 않습니다. 세션 만료·앱 삭제가 중앙 원본 삭제를 발생시키지 않습니다. 계정 복구와 프로젝트 영구 삭제는 추가하지 않았습니다.

`RAILSHOT_SECRETS_FILE`은 환경별 Vault/전달 실행기의 신뢰된 설정 파일이며 전달 중 비밀값은 권한 0600의 임시 파일로만 전달하고 정상·오류 종료 시 지웁니다. SIGKILL 직후 남은 임시 파일은 서버 시작 시 제거합니다. 공용 API는 Vault 주소·namespace·명령·로컬 경로를 받지 않습니다. 신규 환경 profile에는 `secrets_config_file`을 등록해야 하며 runtime 직후 `secrets.configure`를 실행하고 `secrets_ready=true` 확인 전에는 환경을 준비 완료로 표시하지 않습니다.

격리 테스트는 암호화·세션 경계·저장/적용 구분·재시작·실행기 계약을 검증합니다. 실제 Provider의 Vault 초기화·재기동·스토리지 복원·배포·트래픽 전환 성공을 주장하지 않습니다. 운영 자격 사용과 실제 배포는 별도 명시적 승인이 필요합니다.

새 환경 자동 연결에는 profile의 `secrets_delivery_file` 템플릿과 전달기 설정의 `environment_registry_dir`(API 환경 상태 디렉터리)를 등록합니다. API는 파생 환경 ID, `targets.json`의 신뢰된 접속, 성공한 `registration.json`·`cd.json`, secrets 단계 결과의 해시를 묶어 전달 등록을 자동 생성합니다. 템플릿의 `vault.token_file`에 있는 `{environment_id}`만 서버가 발급한 환경 ID로 치환합니다. 외부 보관 도구는 해당 경로에 전달 자격을 먼저 안전하게 저장해야 합니다. 새 환경의 ID를 정적 `environments` 목록에 수동 추가할 필요가 없습니다. 프로젝트와 새 환경 계획을 함께 배포하려면 이 템플릿이 없을 때 Provider 실행 전에 차단합니다.

`vault.token_file`의 실제 내용은 `{"role_id":"…","secret_id":"…"}` 형식의 소유자 전용 AppRole 자격 파일입니다. 외부 복구자료 보관 서비스는 상호 TLS 인증 요청의 `vault-initialization` 자료를 보관하고, `vault-delivery-approle` 자료를 관리 호스트의 환경별 경로에 원자적으로 저장한 뒤 확인 응답을 보내야 합니다. 전달기는 이 자격으로 짧은 유효기간의 Vault 토큰을 발급받습니다. 외부 보관 서비스와 환경 인증서 발급 코드는 이제 저장소에 포함되며 [복구 API](recovery.md)를 따릅니다. 실제 비밀값과 토큰은 공개 응답이나 Git에 기록하지 않습니다. 환경별 중앙 발급·만료 자격 교체·운영자 전용 중단 복구 절차는 [Ansible 계약](ansible.md)의 중앙 발급 절을 따릅니다. 이 운영자 권한은 공개 제품 API에 노출하지 않습니다.
