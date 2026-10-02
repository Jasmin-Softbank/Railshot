# Session-scoped product API

이 문서는 `apps/api/src/server.js`, `product.js`, `environments.js`, `product-store.js`에 구현한 제품 API를 설명한다. 기계 판독 계약은 [product.openapi.json](product.openapi.json)이다. 이 계약의 문서·로컬 검증은 `not_deployed`이며 실제 클라우드 E2E 결과와 별도로 기록한다.

사용자 계정·로그인·팀원 allowlist는 없다. 익명 브라우저 세션별로 접수 기록·계획·화면 설정·연결 정보를 분리한다. 운영자 지정 공용 대상과 실행기의 동시 실행 한도는 공유한다. 세션·스키마·이관 계약은 [dashboard-sessions.md](dashboard-sessions.md)를 따른다. 공개 모드는 `RAILSHOT_PUBLIC_DEMO=1`, `RAILSHOT_ALLOWED_HOSTS`, `RAILSHOT_ALLOWED_ORIGINS`를 명시하며 브라우저 Bearer를 요구하지 않는다. 기본 로컬 모드와 별도 내부 운영자 모드는 `access.js`의 기존 경계를 사용한다. 실행용 GitHub·Provider·SSH 자격은 서버 설정에만 둔다. 별도 OpenStack 연결 정보 저장은 실행용 자격을 바꾸지 않는다.

| 자원 | 구현 경로 | 의미 |
| --- | --- | --- |
| 화면 선택 | `GET /api/v1/options` | 기존 환경의 클라우드(AWS)·온프레미스(OpenStack/Proxmox) 선택을 반환한다. 서버에 명시한 provider와 CI/CD 연결이 일치할 때만 available이다. |
| 대상 | `GET /api/v1/targets` | CI 서비스에 등록한 대상 목록. `ci_submission`·`application_deployment`를 구분한다. CD가 등록한 앱은 `application_name`과 `deployment_scope=registered_application`으로 표시한다. runtime 상태는 독립 관측이 없으면 unknown이다. |
| 빌드 | `POST /api/v1/builds`, `GET /api/v1/builds/{id}` | ZIP·폴더·공개 GitHub를 기존 CI로 제출한다. ID는 GitHub run ID 문자열이며 등록한 run만 조회한다. `published`는 검증한 이미지 게시다. |
| 배포 | `POST /api/v1/deployments`, `GET /api/v1/deployments/{id}` | 선택한 계획으로 환경 준비·대상 등록을 먼저 수행하거나, 이미 등록된 대상으로 바로 CI를 실행한다. CI 게시 결과를 검증한 후 CD 어댑터를 한 번 호출한다. 같은 source/target의 고정 revision 배포 및 기대 공개 HTTP 검증까지 확인해야 succeeded와 최상위 url을 반환한다. |
| profile | `GET /api/v1/profiles` | 운영자가 등록한 환경 사양, 고정 대상 또는 생성 템플릿, 선택적 앱 이름·DB 역할 수와 지원 범위. 자격·로컬 경로는 포함하지 않는다. |
| 계획 | `POST /api/v1/plans`, `GET /api/v1/plans/{id}` | 검증·저장한 계획을 201로 반환한다. 계획은 VM 생성 결과가 아니다. |
| 환경 | `POST /api/v1/environments`, `GET /api/v1/environments/{id}` | 저장된 계획을 한 번 실행한다. 자원·guest·runtime, 선택적 Patroni DB와 배포 대상 등록을 각각 기록한다. 앱 소스의 CI·배포·공개 HTTP 검증은 이 요청에 포함하지 않는다. |

목록은 `{items, next_marker}`이고 미설정 서버의 대상·profile 목록은 빈 목록이다. 목록에는 `limit`(1–100, 기본 20)과 해당 목록의 ID를 사용한 `marker`만 받는다. 그 밖의 경로는 query를 받지 않는다. 알려지지 않은 필드, 중복 단일 multipart 필드·query·JSON key는 거부한다. 파일은 최대 2,000개·총 100 MiB이며 원시 multipart 상한에는 framing용 1 MiB를 더한다. `files`만 반복할 수 있다.

소스 접수는 multipart의 `app`, `target_id`와 공개 GitHub URL(`repository_url`), ZIP(`archive`), 폴더(`files`와 JSON 문자열 배열 `paths`) 중 하나를 받는다. `source_type`은 생략할 수 있으며 지정하면 실제 소스 형식과 일치해야 한다. `plan_id`는 `POST /api/v1/deployments`에서만 선택적으로 받는다. 빌드·legacy deploy에는 허용하지 않는다. 계획을 포함한 배포의 `app`·`target_id`는 계획의 이름·`runtime_target_id`와 일치해야 하고, 해당 profile에 배포 등록 설정이 있어야 한다. 계획이 없는 배포는 서버 CD 설정 또는 성공한 환경 등록 기록의 대상·앱을 사용한다. 성공한 환경의 재배포는 저장한 CD 설정을 재사용하며 VM·DB 생성은 반복하지 않는다. 대상·앱·계획·환경 ID가 일치한 성공 기록만 재시작 후 CI 허용 대상으로 복원한다.

기존 환경을 선택하는 화면은 같은 배포 경로에 `app`·`target_id` 대신 `environment`·`provider`와 선택적 `source_name`을 보낼 수 있다. 서버는 등록된 provider와 CI/CD 연결을 확인해 대상과 고정 앱 이름을 정하며, 고정 앱 이름이 없으면 소스 이름에서 유효한 앱 이름을 만든다. 미연결 provider는 소스를 가져오거나 실행하기 전에 거부한다. 이 선택 방식에는 `app`, `target_id`, `plan_id`를 함께 보낼 수 없다. 새 앱·DB 환경을 만드는 화면은 profiles로 계획을 만든 뒤 그 계획의 `name`, `runtime_target_id`, `id`를 각각 `app`, `target_id`, `plan_id`로 제출한다.

profile의 `target_id`는 고정 runtime 대상 또는 새 대상 이름의 기준이다. `create_per_request=true`이면 사용자 앱 이름과 계획 ID의 SHA-256 앞 8자리로 runtime·DB 대상, namespace와 GitOps 경로를 한 번 파생한다. 이때 `application_name`은 null이며 최종 요청은 계획의 `runtime_target_id`를 사용한다. false이면 기존 고정 대상·앱을 유지한다. 운영자 설정 `registration_max_age_seconds`(1800–604800)는 계획 준비 시 비공개 등록 만료 시각을 한 번 정하며 재검증으로 연장하지 않는다. 이 만료는 VM 종료나 비용 상한을 보장하지 않는다. `deployment_supported`는 배포 설정 유무, `supported`는 provider·runtime 용도·운영자 실행 허용 여부를 나타낸다. 두 값 모두 생성·등록·준비 완료를 뜻하지 않는다. `database`는 null 또는 `{mode:"patroni", required, database_nodes, dcs_voters, proxy_nodes}`다. `required`는 등록된 앱 대상의 DB binding 필요 여부다. DB와 DCS 역할은 같은 VM에 둘 수 있으므로 역할 수를 더한 값이 VM 수는 아니다.

계획 입력은 `{name, runtime:{profile_id,node_count}, database:{mode,placements?}}`다. `name`은 3–30자이며 소문자로 시작하고 소문자·숫자·하이픈을 사용하며 소문자나 숫자로 끝난다. 현재 실행 가능한 runtime은 운영자가 등록한 AWS/GCP amd64 VM 한 대다. `node_count` 입력 범위는 1–64이지만 1이 아니면 `SINGLE_NODE_ONLY`로 실행을 차단한다. `database.mode=none`은 placements를 생략하거나 빈 배열로 지정한다. `standalone`은 입력으로 받되 `STANDALONE_DATABASE_UNSUPPORTED`로 차단한다.

`database.mode=patroni`에는 `placements`가 필요하다. 각 항목은 `{profile_id,database_nodes,dcs_voters,proxy_nodes}`이며 해당 profile에 등록한 역할 수와 정확히 일치해야 한다. 배열은 최대 16개, 각 역할 수는 0–31이며 profile 중복은 차단한다. 합산 구성에는 DB 노드 2개 이상, 홀수인 DCS voter 3개 이상, proxy 1개 이상이 필요하다. DB와 proxy는 같은 VM에 배치할 수 없다. 이 입력은 운영자가 이미 허용한 VM·네트워크 구성을 선택하며 클라우드 자격·SSH 경로·임의 provider 변수를 받지 않는다. 서로 다른 provider의 profile을 선택해도 클라우드 간 네트워크 연결을 자동으로 만든다는 뜻은 아니다.

배포 설정이 있는 runtime profile은 등록 대상의 DB binding 필요 여부와 요청의 `database.mode=patroni` 여부가 일치해야 한다. 불일치하면 Terraform 계획을 실행하기 전에 `DATABASE_BINDING_PROFILE_MISMATCH`로 차단한다. 배포 설정이 없는 profile에서는 DB 준비를 독립적으로 선택할 수 있다.

계획은 준비 완료 시점부터 최대 15분간 유효하며 비용 견적 만료가 더 빠르면 그 시각에 만료된다. 계획은 입력·정책·profile·대상 snapshot과 각 VM의 저장된 Terraform plan digest에 묶인다. 기존 자원의 수정·삭제 없이 새 자원 생성만 허용한다. 실행 차단 이유는 `blockers`, 실행 가능 여부는 `executable`로 반환하고 `cost`는 자동 예산 갱신을 설정한 AWS profile에서 최신 청구·가격 관측을 바탕으로 반환하며 그 외에는 null이다. 비공개 `budget_refresh:{}`는 보존 디스크를 월말까지 계산한다. `budget_refresh:{retained_storage_hours:24}`는 정리 책임이 지정된 합성 검증에 한해서 쓰는 24시간 견적 정책이며 자동 삭제 기능이 아니다. 기존 원장의 held 금액, 미보고 비용, 월 한도는 유지하고 계정·통화 불일치나 수집 실패·예산 초과 시 Terraform 계획 전에 차단한다. `steps`는 예정된 작업 목록이며 실행 증거나 실제 `stage` 전환 기록은 아니다. 한 계획은 환경 생성과 계획을 포함한 배포 중 한 번만 소비할 수 있다.

계획을 포함한 배포는 `plan_id`, `environment_id`, `environment`를 반환하고 최초 `stage`가 `environment`다. `environment_id`는 배포 UUID 뒤에 `.environment`를 붙인 내부 실행 식별자이며 별도 `GET /api/v1/environments/{id}` 자원으로 등록하지 않는다. 진행 상태는 배포 객체의 `environment`에서 읽는다. 초기값은 `{status:"queued"}`이며 실행 중 자원·guest·runtime·DB·binding 결과가 추가된다. 환경이 succeeded이고 대상 등록까지 확인된 뒤에만 CI를 제출한다.

환경의 `resources.nodes`는 모든 자원 생성이 성공한 뒤 반환하는 `{target_id,provider}` 목록이다. VM 생성만으로 guest·runtime·DB 준비를 증명하지 않는다. `database`는 Patroni 실행 결과와 비공개 binding 파일 digest를 확인한 뒤 succeeded가 된다. `binding`은 그 binding을 사용하는 배포 대상 등록 단계이며 등록 성공 후에만 환경의 `deployment_supported=true`가 된다. DB를 생략하면 `database.status=skipped`이고, 배포 설정이 없으면 runtime 또는 DB 준비가 성공해도 `deployment_supported=false`다. 환경의 `stage`는 성공 후에도 마지막 단계인 runtime·database·binding 등으로 남을 수 있다.

배포의 `cd.migration`은 migration Job을 관측한 경우에만 존재하며 `{name,state:"succeeded"|"unverified"}`다. migration 성공, DB 준비, 대상 등록, 앱 배포와 공개 HTTP 성공은 각각 별도 결과다. 최상위 배포 성공은 `cd.deployed=true`, 고정 revision과 성공한 공개 HTTP 관측을 모두 요구한다. 환경 생성의 성공이나 profile 설정만으로 앱 URL을 성공 결과로 제공하지 않는다.

비동기 접수는 화균 님의 `{resource_id, action:"create", status:"accepted", request_id}` 형식과 `202`, `Location`, `Retry-After: 2`, `X-Request-ID`, `Cache-Control: no-store`를 사용한다. 오류는 `{error:{code,message,request_id,retryable,outcome_unknown}}`다. 매 HTTP 요청마다 새로운 request ID를 생성한다. 작업에 저장한 오류 ID와 Ansible의 `ansible_job_id`는 별개다.

배포·환경 생성에는 `Idempotency-Key`가 필요하다. 같은 세션·자원 종류 안에서 같은 키와 입력은 같은 ID를 반환한다. queued/running이면 202, 완료·실패·차단·unknown이면 200 자원 객체와 Location이다. 같은 키로 입력을 바꾸면 409이며 배포의 `plan_id`와 환경·provider 선택도 입력에 포함한다. ZIP의 압축 시각·multipart boundary는 파일 의미에 포함하지 않으며 폴더 파일 순서도 정규화한다. GitHub URL의 첫 SHA·소스 snapshot은 고정하고 같은 키의 재요청에서 다시 다운로드하지 않는다. 빌드 생성에는 이 멱등 계약이 없으므로 응답 유실 시 자동 재전송하지 않는다.

`RAILSHOT_STATE_DIR`는 저장소 밖의 전용 영속 디렉터리로 설정한다. 기본값은 사용자 홈의 `.local/state/railshot`이다. 최종 디렉터리와 state 파일은 현재 OS 사용자 소유이며 다른 사용자 접근 권한과 심볼릭 링크를 거부한다. 소스는 0600 snapshot, 작업·계획·run binding·멱등 키는 dashboard.sqlite3의 WAL 트랜잭션(synchronous=FULL)으로 저장한다. 기존 state.json은 첫 기동에 한 번 이관하고 이후 갱신하지 않는다. snapshot과 의도를 저장한 뒤에만 CI·CD·환경 실행을 시작한다. 기본 보관 상한은 작업 100개, 계획 100개, 소스 snapshot 512 MiB다. 상한 도달 시 409를 반환하며 자동 삭제하지 않는다. 운영자는 작업을 확인하고 보관·정리 정책을 적용해야 한다.

한 API 프로세스가 한 로컬 저장소를 소유한다. 프로세스 ID와 시작 식별자로 같은 호스트의 중복 사용을 막는다. 이 lock은 여러 호스트의 분산 잠금이 아니므로 API replica는 1개로 운영하고 Recreate 전략으로 이전·새 컨테이너의 동시 쓰기를 피한다. 서로 다른 PID namespace와 여러 호스트의 동시 writer는 지원하지 않는다. 미완료 작업과 unknown은 하나의 admission 한도를 공유한다. 재시작 시 queued/running을 unknown으로 저장하고 작업을 자동 재실행하지 않는다. 외부 실행 결과 유실이나 저장 실패도 성공으로 표시하지 않는다. unknown을 해제하는 공개 API는 없으며 운영자가 GitHub·CD·환경 기록을 확인해야 한다.

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

기존 `POST /api/deploy`와 `GET /api/runs/{run_id}`는 응답 필드와 `x-railshot-request: deploy` (legacy: `x-jasmin-request: deploy`) 계약을 유지한다. 실제 등록 서비스에서는 새 영속 접수·admission·run binding을 공유하므로 다른 세션에서 접수했거나 이 workspace에 없는 run ID는 원격에서 조회하지 않는다. CLI/MCP는 호스트별 쿠키를 ~/.local/state/railshot-client의 0600 파일에 저장하며 RAILSHOT_CLIENT_SESSION_DIR로 위치를 지정한다. 쿠키 없는 비공개 localhost 유지보수만 기존 공유 범위를 유지한다. v1 배포와 달리 legacy deploy는 CI 제출이다.

현재 운영 메트릭, 수집 시각과 실패 상태는 [제품 관측 계약](observations.md)을 따른다.

신규 edge 등록을 사용한 배포는 공개 검증 성공 시 `public_http.site_url`과 `public_http.receipt`를 추가한다. `url`은 검증한 health 경로이고 `site_url`은 HTTPS 200을 확인한 앱 경로다. 제품 최상위 `url`은 `site_url`이 있으면 이를 사용하고 기존 고정 앱은 health URL을 유지한다. receipt는 deployment·target·tenant·app·environment·namespace, source/Git revision, image/route/plan digest, 만료 정책과 DNS/TLS/target health 결과를 연결한다. IP·자격·응답 body는 공개 receipt에 넣지 않는다. 검증되지 않은 결과에는 이 선택 필드가 없다.

대시보드는 소스와 `environment=cloud|onprem`, `provider=aws|openstack|proxmox`를 기존 배포 endpoint로 보낸다. 이 모드는 `app`·`target_id`와 함께 사용할 수 없다. API가 `RAILSHOT_TARGET_ID`·`RAILSHOT_TARGET_PROVIDER`와 CD 등록 앱을 결정하며, 등록 앱이 없으면 GitHub/ZIP/폴더 이름에서 유효한 앱 이름을 생성한다. 폴더명은 선택적 `source_name`(1–255자, 제어 문자 금지)으로 전달한다. 알 수 없는 provider와 잘못된 조합은 422, 연결되지 않은 선택은 409이며 다른 대상으로 대체하지 않는다. 기존 app/target_id 요청과 builds API는 유지한다.

현재 공개 플랫폼 manifest는 AWS 기본 대상에 `RAILSHOT_TARGET_PROVIDER=aws`를 명시한다. 추가 provider는 선택 JSON 설정 `RAILSHOT_PROVIDER_TARGETS={"openstack":"k3s-openstack"}`으로 연결하며 기본 AWS 대상은 유지한다. 추가 ID가 `RAILSHOT_TARGET_IDS`의 CI 허용 목록과 CD adapter의 등록 대상 양쪽에 있을 때만 `/api/v1/options`에서 사용 가능하다. 선택은 해당 대상의 등록 앱으로 바인딩되며 다른 provider로 대체하지 않는다. 잘못된 매핑, 기본 provider의 대상 교체, 같은 ID의 여러 provider 선언은 거부한다. provider가 명시되지 않은 대상은 ID 문자열로 종류를 추측하지 않는다. UI의 클라우드/온프레미스 카드와 provider 선택은 그대로 유지한다.

플랫폼 릴리스는 같은 이름의 선택 Actions 변수를 `--provider-targets`로 전달하고, 매핑과 기본 AWS를 포함한 CI 목록을 이미지 선언에 함께 보존한다. 기본값은 빈 매핑이다. 먼저 [런타임 등록](runtime-registration.md), CI 대상·앱 바인딩과 API의 CD 등록을 완료한 뒤 이 변수를 설정한다. OpenStack은 기존 서버·K3s의 등록과 재배포 경로이며, AWS/GCP의 Terraform 기반 신규 환경 생성 범위를 확장하지 않는다.

`GET /api/v1/builds`, `/api/v1/deployments`, `/api/v1/environments`는 현재 세션의 실행 요약 목록을, `GET /api/v1/plans`는 현재 세션의 계획 목록을 반환한다. 배포 내역은 서버 목록으로 복원하며 localStorage의 기존 마지막 실행 ID를 사용하지 않는다. 세션·설정·OpenStack 연결 API는 [별도 명세](dashboard-sessions.md)에 정리했다.
