# 익명 세션 대시보드 저장소

사용자 계정·로그인 없이 브라우저 세션으로 배포 내역, 화면 설정, OpenStack 연결 정보를 구분한다. 구현 순서는 기존 구조 확인 → SQLite 스키마 → API → 프론트엔드 → 테스트다. 이 문서는 구현 계약이며 운영 반영 여부는 PR·배포 실행·서버 읽기 결과로 별도 확인한다.

## 배치 결정과 확인한 리소스

2026-10-03 KST 읽기 전용 점검 기준이다.

| 기존 리소스 | 역할과 판단 |
| --- | --- |
| AWS `railshot-control-poc` / `i-033ae2db907fde68e` / `172.31.0.172` / t3.medium | 플랫폼 API가 실행되는 노드. 이 서버의 기존 디스크를 사용한다. |
| `railshot-system/railshot-api` | API 1 replica, Recreate 전략. SQLite 단일 writer와 맞는다. |
| PVC `railshot-api` | Bound, local-path, 요청 크기 4 GiB. `/var/lib/railshot`에 마운트. 노드 파일시스템 여유 약 14 GiB. local-path의 요청 크기는 물리 디스크 할당량 보장이 아니다. |
| `railshot-build-worker-aws-01` / `i-09955d23ad1d8dbe2` | CI 빌드 작업 서버. 대시보드 DB를 두지 않는다. |
| `railshot-user-aws-node` / `i-0050c052a1fcf41fe` | 사용자 앱 실행 서버. 플랫폼 기록과 앱 DB 수명을 분리한다. |
| RDS / control 클러스터의 PostgreSQL·CNPG | 이 점검 범위에서는 기존 DB 인스턴스·파드를 찾지 못했다. 새 DB 서버를 만들 필요가 없다. |

선택은 **SQLite + 기존 API PVC**다. 경로는 `${RAILSHOT_STATE_DIR}/dashboard.sqlite3`, 운영값은 `/var/lib/railshot/state/dashboard.sqlite3`다. Node 내장 `node:sqlite`를 사용하며 Node 22.13 이상이 필요하다. Node 22 계열에서는 experimental API다. 별도 패키지·ORM·DB 네트워크 포트를 추가하지 않는다. 현재처럼 한 서버에서 요청을 처리하고 쓰기 동시성이 낮은 경우에 맞는다. [SQLite 배치 기준](https://www.sqlite.org/whentouse.html), [Node SQLite API](https://nodejs.org/api/sqlite.html).

플랫폼을 여러 replica로 확장하거나 노드 장애를 견디는 DB가 필요해지면 PostgreSQL로 이전한다. 현재 local-path PVC는 해당 노드 디스크에 의존하며 HA 또는 자동 백업을 제공하지 않는다.

## 데이터 구조

실행되는 DDL은 [dashboard-schema.sql](../../apps/api/src/dashboard-schema.sql)이다. 모두 SQLite STRICT 테이블이며 FK 검사를 켠다.

| 테이블 | 주요 필드 | 관계·용도 |
| --- | --- | --- |
| `sessions` | `id` PK, `created_at`, `last_seen_at`, `expires_at` | id는 쿠키 토큰의 SHA-256. 시간은 epoch ms. 사용자 이름이나 계정은 없다. |
| `preferences` | `session_id` PK/FK, `data` JSON | 마지막 화면, cloud/onprem 선택, onprem provider. 배포 대상을 결정하는 숨은 상태로 사용하지 않는다. |
| `operations` | `id` PK, `session_id` FK nullable, `kind`, `status`, `created_at`, `record` JSON | build/deployment/environment의 기존 실행 상태. 세션·생성 시각 인덱스. |
| `plans` | `id` PK, `session_id` FK nullable, `record` JSON | 공개 계획, 비공개 profile snapshot, 실행 소비 상태. |
| `bindings` | `run_id` PK, `operation_id` FK, `record` JSON | 검증할 GitHub CI run과 로컬 실행을 연결. |
| `idempotency` | `key` PK, `operation_id` FK | 세션 hash + 자원 종류 + 요청 키. 기존 로컬 운영 기록의 키는 보존. |
| `connections` | `id` PK, `session_id` FK, `provider`, `label`, `console_url`, `username`, `password_encrypted`, `created_at`, `updated_at` | provider는 openstack만 허용. 세션별 인덱스, ISO 시간, 비밀번호는 암호문 BLOB. |

기존 실행 객체는 검증된 상태 전이 구조를 유지하기 위해 JSON으로 저장한다. 소스 파일은 기존 0600 snapshot 파일에 두며 DB BLOB으로 중복 저장하지 않는다. WAL, `synchronous=FULL`, SQLite 트랜잭션과 기존 단일 프로세스 잠금을 사용한다. 접수 의도와 소스 저장이 성공한 뒤에만 외부 실행을 시작한다. 작업 보관 상한 100개라 기존 상태 묶음을 한 트랜잭션에서 갱신한다. 보관량을 늘릴 때 행별 갱신으로 바꾼다.

## 세션과 소유 범위

서버가 256-bit 난수 토큰을 발급한다. 쿠키는 `railshot_session`, HttpOnly, SameSite=Strict, Path=/, Max-Age=604800이며 원격 바인딩에서는 Secure다. DB에는 토큰 원문을 저장하지 않는다. 유효기간은 생성 후 고정 7일이며 접속할 때 연장하지 않는다.

쿠키가 없거나 만료·변조됐으면 새 빈 세션을 만든다. 같은 브라우저의 탭은 세션을 공유하고 다른 브라우저·시크릿 창은 별도 세션이다. 쿠키 삭제 시 기존 세션 복구 기능은 없다. 계정 인증이나 사람의 신원 확인을 제공하지 않으며 쿠키를 소유한 클라이언트가 같은 세션이다.

모든 원격 API 요청은 세션 범위를 적용한다. reverse proxy가 기존 내부 Bearer를 삽입해도 소유 범위를 우회하지 못한다. 기존 비공개 localhost의 쿠키 없는 CLI 유지보수 경로만 기존 공유 상태 접근을 유지한다. 원격 CLI/MCP는 API origin별 쿠키를 `~/.local/state/railshot-client`의 0600 파일에 저장한다. `RAILSHOT_CLIENT_SESSION_DIR`로 위치를 바꿀 수 있다. 원격 curl 등 별도 클라이언트도 cookie jar를 유지해야 한다.

배포·빌드·계획·환경의 조회와 목록, 멱등 키, 신규 런타임 대상은 세션별이다. 다른 세션의 상세 ID는 404이고 외부 서비스 조회 전에 거부한다. 다른 세션의 계획으로 배포를 요청하면 실행 가능한 계획이 없다는 422, 환경 생성은 404다. 운영자가 지정한 공용 대상은 계속 공유하므로 같은 공용 앱에 배포하면 기존 앱을 갱신할 수 있다. 세션은 VM·네트워크 격리를 제공하지 않는다. 하나의 미완료 작업만 허용하는 기존 실행 제한도 전체 서버에 적용한다.

세션 만료는 VM 삭제나 작업 취소가 아니다. 만료된 행과 unknown 실행은 자동 삭제하지 않는다. 상한은 세션 10,000개, 연결 20개/세션, 기존 작업·계획 각 100개, 소스 512 MiB이며 도달 시 저장을 거부한다.

## HTTP 계약

상세 JSON 스키마는 [product.openapi.json](product.openapi.json)이다. 기존 에러 envelope, no-store, X-Request-ID와 목록의 limit/marker 규칙을 따른다.

| 메서드·경로 | 결과 |
| --- | --- |
| `POST /api/v1/sessions`, `GET /api/v1/sessions` | `{expires_at}`. POST에서 새 발급 201, 기존 세션 200. 토큰은 Set-Cookie에만 있다. |
| `GET /api/v1/preferences` | `{view, environment, provider}` |
| `PUT /api/v1/preferences` | 허용 필드만 부분 갱신 후 전체 설정 반환 |
| `GET /api/v1/deployments`, `/api/v1/builds`, `/api/v1/environments` | 본인 실행 요약 `{items, next_marker, total}`. 생성 시각·내부 실행 ID 내림차순. 외부 provider를 polling하지 않는다. |
| `GET /api/v1/plans` | 본인 공개 계획 목록 |
| `GET /api/v1/connections` | 본인 OpenStack 연결 목록 |
| `POST /api/v1/connections` | 연결 저장 201 + Location |
| `GET /api/v1/connections/{id}` | 본인 연결 메타데이터 조회 |
| `PUT /api/v1/connections/{id}` | 메타데이터와 선택적 비밀번호 변경 200 |
| `DELETE /api/v1/connections/{id}` | 본인 연결 삭제 204 |

실행 목록은 SQLite의 현재 세션·종류 조건으로 직접 조회한다. `limit` 기본 20, 범위 1–100이며 `marker`는
직전 응답의 `next_marker`다. deployment/environment는 실행 ID, build는 기존 GitHub run ID다.
다른 세션·종류·소유자 없는 legacy 기록의 marker는 422다. 같은 시각의 기록은 내부 실행 ID로
정렬하며 시각 없는 legacy 기록은 운영자 경로에서만 마지막에 놓는다. 재시작해도 정렬은 같다.
첫 페이지 이후 앞쪽에 새 기록이 들어와도 이미 본 기록을 다음 페이지에서 중복하지 않는다.
고정 snapshot은 아니므로 생성 시각이 같은 신규 기록은 ID 순서에 따라 다음 페이지에 포함될 수 있고,
`total`은 요청 시점 해당 종류의 전체 수다. 처음부터 새 기록을 보려면 marker 없이 다시 조회한다.
필터·page 번호·별도 cursor 형식은 추가하지 않으며 기존 전체 작업 100개 보관 상한을 유지한다.

연결 입력 예:

```json
{
  "label": "팀 OpenStack",
  "console_url": "https://openstack.example/dashboard/",
  "username": "demo-user",
  "password": "example-only-password"
}
```

label은 1–80자, URL은 최대 2048자, username은 최대 256자, password는 최대 4096자다. URL은 http/https만 받고 embedded credential·query·fragment를 거부한다. 서버는 이 URL로 접속하지 않는다. `PUT`은 label/console_url/username을 받으며 password 생략은 기존 값 유지, null은 제거다. 응답에는 id/provider/메타데이터/시각과 `has_password`만 있다.

비밀번호는 AES-256-GCM으로 암호화하고 세션 hash와 connection id를 AAD에 묶는다. 난수 IV 12 bytes + 인증 태그 16 bytes + ciphertext를 저장한다. 32-byte key는 별도 0600 `connections.key` 파일이다. 키 파일은 DB와 같은 보호 디렉터리에 있어 서버 침해를 막는 경계는 아니며 DB 파일만 유출된 경우의 평문 노출을 줄인다. 기존 암호문이 있는데 키가 없으면 새 키를 생성하지 않고 시작을 거부한다. 응답·화면·로그로 저장 비밀번호를 읽는 기능은 없고 저장 후 폼도 비운다. 이 기록은 현재 실행용 OpenStack 자격이나 자동 로그인 설정에 연결하지 않는다.

## 이관·백업·복구

DB의 `user_version=0`일 때 기존 `state.json`을 한 번 가져오고 트랜잭션에서 version 1을 기록한다. 기존 기록은 `session_id=NULL`로 남겨 첫 방문자에게 넘기지 않는다. `state.json`은 보존하지만 이후 기록의 정본이 아니다. 재기동 시 queued/running은 기존 정책대로 unknown으로 바꾸며 재실행하지 않는다.

배포 전 API에 활성 실행이 없는지 확인하고 기존 상태 디렉터리를 백업한다. 실행 중인 DB를 파일 복사할 때 WAL을 누락하면 안 된다. SQLite backup API 또는 API 정상 종료·checkpoint 후 디렉터리 snapshot을 사용한다. 복구 가능한 백업에는 DB, 소스 snapshot, 실행에 참조되는 비공개 환경 상태가 필요하고 `connections.key`도 별도 보호 백업으로 보관한다. DB만 공개 저장소나 CI artifact에 업로드하지 않는다.

이미지 교체만으로 예전 JSON 버전으로 되돌리면 신규 기록을 잃고 완료 실행을 다시 접수할 위험이 있다. 이전 코드로 복구할 때는 쓰기를 중지하고 이관 전 상태와 이후 외부 실행을 대조해야 한다. 정상 복구는 현재 SQLite 지원 코드와 일관된 DB·소스·키 백업을 사용한다. `PRAGMA integrity_check`와 소유권/0600, 세션별 읽기, 완료 작업 비재실행을 확인한 뒤 접수를 재개한다.

## 검증

`npm test --workspace apps/api`, `npm run build`, `npm --prefix ci/browser test`로 검증한다. 세션 테스트는 두 클라이언트의 자원·계획·멱등 키 격리, 쿠키 없는 원격 우회 거부, 세션 만료, JSON 이관, DB 재시작, 암호문 복호 검증, 다른 세션 수정·삭제 거부, CLI 프로세스 간 쿠키 유지를 포함한다. 기존 환경 등록 테스트는 신규 대상의 세션별 조회와 배포 제한을 재시작 전후로 확인한다. 브라우저 테스트는 기존 배포 흐름과 내역 복원, 설정 유지, 두 브라우저의 연결 정보 분리, 비밀번호 입력 초기화·삭제, desktop/mobile 렌더링을 확인한다.
