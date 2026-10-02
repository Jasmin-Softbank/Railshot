# Shared workspace product API

이 문서는 `apps/api/src/server.js`, `product.js`, `product-store.js`에 구현한 제품 API를 설명한다. 기계 판독 계약은 [product.openapi.json](product.openapi.json)이다. 로컬 HTTP·모의 외부 서비스 검증과 실제 클라우드 E2E 결과는 별도로 기록한다.

사용자 계정·로그인·팀원 allowlist는 없다. 모든 사용자가 같은 workspace의 등록 대상과 접수 기록을 사용한다. 공개 모드는 `RAILSHOT_PUBLIC_DEMO=1`, `RAILSHOT_ALLOWED_HOSTS`, `RAILSHOT_ALLOWED_ORIGINS`를 명시하며 브라우저 Bearer를 요구하지 않는다. 기본 로컬 모드와 별도 내부 운영자 모드는 `access.js`의 기존 경계를 사용한다. GitHub·Provider·SSH 자격은 서버 설정에만 둔다.

| 자원 | 구현 경로 | 의미 |
| --- | --- | --- |
| 화면 선택 | `GET /api/v1/deployment-options` | 원래 UI의 클라우드(AWS)·온프레미스(OpenStack/Proxmox) 선택을 반환한다. 서버에 명시한 provider와 CI/CD 연결이 일치할 때만 available이다. |
| 대상 | `GET /api/v1/targets` | 서버의 한 등록 대상. `ci_submission`·`application_deployment`를 구분한다. CD가 등록한 앱은 `application_name`과 `deployment_scope=registered_application`으로 표시한다. runtime 상태는 독립 관측이 없으면 unknown이다. |
| 빌드 | `POST /api/v1/builds`, `GET /api/v1/builds/{id}` | ZIP·폴더·공개 GitHub를 기존 CI로 제출한다. ID는 GitHub run ID 문자열이며 등록한 run만 조회한다. `published`는 검증한 이미지 게시다. |
| 배포 | `POST /api/v1/deployments`, `GET /api/v1/deployments/{id}` | CI 게시 결과를 검증한 후 CD 어댑터를 한 번 호출한다. 같은 source/target의 고정 revision 배포 및 기대 공개 HTTP 검증까지 확인해야 succeeded와 최상위 url을 반환한다. |
| profile | `GET /api/v1/profiles` | 운영자가 등록한 환경 사양과 지원 범위. 자격·로컬 경로는 포함하지 않는다. |
| 계획 | `POST /api/v1/plans`, `GET /api/v1/plans/{id}` | 검증·저장한 계획을 201로 반환한다. 계획은 VM 생성 결과가 아니다. |
| 환경 | `POST /api/v1/environments`, `GET /api/v1/environments/{id}` | 저장된 계획을 한 번 실행한다. 자원·guest·runtime 준비를 각각 기록한다. 새 runtime은 CD 대상 등록과 구분하며 `deployment_supported=false`다. DB 실행은 이 연결에 포함하지 않는다. |

목록은 `{items, next_marker}`이고 미설정 서버의 대상·profile 목록은 빈 목록이다. 목록에는 `limit`(1–100, 기본 20)과 해당 목록의 ID를 사용한 `marker`만 받는다. 그 밖의 경로는 query를 받지 않는다. 알려지지 않은 필드, 중복 단일 multipart 필드·query·JSON key는 거부한다. 파일은 최대 2,000개·총 100 MiB이며 원시 multipart 상한에는 framing용 1 MiB를 더한다. `files`만 반복할 수 있다.

비동기 접수는 화균 님의 `{resource_id, action:"create", status:"accepted", request_id}` 형식과 `202`, `Location`, `Retry-After: 2`, `X-Request-ID`, `Cache-Control: no-store`를 사용한다. 오류는 `{error:{code,message,request_id,retryable,outcome_unknown}}`다. 매 HTTP 요청마다 새로운 request ID를 생성한다. 작업에 저장한 오류 ID와 Ansible의 `ansible_job_id`는 별개다.

배포·환경 생성에는 `Idempotency-Key`가 필요하다. 같은 키와 입력은 같은 ID를 반환한다. queued/running이면 202, 완료·실패·차단·unknown이면 200 자원 객체와 Location이다. 같은 키로 입력을 바꾸면 409다. ZIP의 압축 시각·multipart boundary는 파일 의미에 포함하지 않으며 폴더 파일 순서도 정규화한다. GitHub URL의 첫 SHA·소스 snapshot은 고정하고 같은 키의 재요청에서 다시 다운로드하지 않는다. 빌드 생성에는 이 멱등 계약이 없으므로 응답 유실 시 자동 재전송하지 않는다.

`RAILSHOT_STATE_DIR`는 저장소 밖의 전용 영속 디렉터리로 설정한다. 기본값은 사용자 홈의 `.local/state/railshot`이다. 최종 디렉터리와 state 파일은 현재 OS 사용자 소유이며 다른 사용자 접근 권한과 심볼릭 링크를 거부한다. 소스는 0600 snapshot, 작은 작업 기록은 fsync 후 atomic rename으로 저장한다. snapshot과 의도를 저장한 뒤에만 CI·CD·환경 실행을 시작한다. 기본 보관 상한은 작업 100개, 계획 100개, 소스 snapshot 512 MiB다. 상한 도달 시 409를 반환하며 자동 삭제하지 않는다. 운영자는 작업을 확인하고 보관·정리 정책을 적용해야 한다.

한 API 프로세스가 한 로컬 저장소를 소유한다. 프로세스 ID와 시작 식별자로 같은 호스트의 중복 사용을 막는다. 이 lock은 여러 호스트의 분산 잠금이 아니므로 API replica는 1개로 운영하고 Recreate 전략으로 이전·새 컨테이너의 동시 쓰기를 피한다. 서로 다른 PID namespace와 여러 호스트의 동시 writer는 지원하지 않는다. 미완료 작업과 unknown은 하나의 admission 한도를 공유한다. 재시작 시 queued/running을 unknown으로 저장하고 작업을 자동 재실행하지 않는다. 외부 실행 결과 유실이나 저장 실패도 성공으로 표시하지 않는다. unknown을 해제하는 공개 API는 없으며 운영자가 GitHub·CD·환경 기록을 확인해야 한다.

CI는 `GITHUB_TOKEN`, `RAILSHOT_TARGET_ID` 및 기존 GitHub 저장소 설정을 사용한다. CD는 `RAILSHOT_CD_CONFIG`의 비공개 고정 설정과 검증한 publication 파일만 받는다. 환경은 `RAILSHOT_PROFILES_FILE`과 서버의 Python/Terraform/Ansible 도구를 사용한다. 현재 환경 실행 범위는 등록된 AWS/GCP 단일 amd64 runtime과 database.mode=none이다. profile 등록·SSH·네트워크·실행 도구가 준비됐다는 사실과 실제 클라우드 준비 성공은 구분한다.

기존 `POST /api/deploy`와 `GET /api/runs/{run_id}`는 응답 필드와 `x-jasmin-request: deploy` 계약을 유지한다. 실제 등록 서비스에서는 새 영속 접수·admission·run binding을 공유하므로 이 workspace에서 접수하지 않은 과거 또는 외부 run ID는 조회하지 않는다. v1 배포와 달리 legacy deploy는 CI 제출이다.

현재 운영 메트릭, 수집 시각과 실패 상태는 [제품 관측 계약](observations.md)을 따른다.

신규 edge 등록을 사용한 배포는 공개 검증 성공 시 `public_http.site_url`과 `public_http.receipt`를 추가한다. `url`은 검증한 health 경로이고 `site_url`은 HTTPS 200을 확인한 앱 경로다. 제품 최상위 `url`은 `site_url`이 있으면 이를 사용하고 기존 고정 앱은 health URL을 유지한다. receipt는 deployment·target·tenant·app·environment·namespace, source/Git revision, image/route/plan digest, 만료 정책과 DNS/TLS/target health 결과를 연결한다. IP·자격·응답 body는 공개 receipt에 넣지 않는다. 검증되지 않은 결과에는 이 선택 필드가 없다.

대시보드는 소스와 `environment=cloud|onprem`, `provider=aws|openstack|proxmox`를 기존 배포 endpoint로 보낸다. 이 모드는 `app`·`target_id`와 함께 사용할 수 없다. API가 `RAILSHOT_TARGET_ID`·`RAILSHOT_TARGET_PROVIDER`와 CD 등록 앱을 결정하며, 등록 앱이 없으면 GitHub/ZIP/폴더 이름에서 유효한 앱 이름을 생성한다. 폴더명은 선택적 `source_name`(1–255자, 제어 문자 금지)으로 전달한다. 알 수 없는 provider와 잘못된 조합은 422, 연결되지 않은 선택은 409이며 다른 대상으로 대체하지 않는다. 기존 app/target_id 요청과 builds API는 유지한다.

현재 공개 플랫폼 manifest는 검증된 AWS 대상에 `RAILSHOT_TARGET_PROVIDER=aws`를 명시한다. 대상 인프라를 바꾸면 이 운영자 설정도 함께 바꿔야 한다. provider 미설정 시 화면 선택 실행은 차단되며 대상 ID 문자열로 provider를 추측하지 않는다. UI의 클라우드/온프레미스 카드와 provider 선택을 backend의 target ID나 실행 종류 드롭다운으로 대체하지 않는다.
