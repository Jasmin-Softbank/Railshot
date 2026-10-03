# RailShot dashboard

저장소 루트에서 `npm ci --ignore-scripts` 후 `npm start --workspace apps/api`를 실행하고
`http://127.0.0.1:4173`을 연다. 로그인·회원·브라우저 토큰 입력은 없다.
공개 배치의 설정과 실행 범위는 [제품 API 운영 계약](../../docs/api/product.md)을 따른다.

- UI 기준은 `006ad34`의 소스 → 클라우드/온프레미스 → 선택 내용 확인 → 배포 시작이다.
  기존 카드·OpenStack/Proxmox 선택·콘솔 탭을 유지한다. 화면은
  `/api/v1/options`에서 준비 상태를 읽고 소스와 environment/provider만
  deployments API에 보낸다. 등록 대상의 ID·앱 이름 결정과 실행 가능 여부 검사는 서버가 수행한다.
  미연결 provider는 명확히 차단하며 다른 대상에 배포하지 않는다.
- 202의 `Location`과 자원 ID를 확인한 뒤 15초마다 상태를 조회한다. 이미지 게시와
  앱 배포 성공을 구분한다. 배포 성공 및 HTTP 검증 시각이 있을 때만 앱 링크를 표시한다.
- HttpOnly 익명 세션 쿠키로 배포·빌드 이력을 분리한다. 실행 토큰·소스·자격증명은
  localStorage에 저장하지 않는다. 내역은 종류별 최신 접수 순으로 10건씩 서버에서 읽고
  next_marker로 이동한다. 페이지 위치는 현재 탭의 history.state에 유지하며, 최신 내역·
  실행 종류 변경·새 배포 접수는 첫 페이지로 돌아간다. 목록 실패를 빈 이력으로 표시하지 않는다.
- OpenStack 선택 시 프로젝트·사용자 ID와 임의의 연계 키를 등록 API로 보낸다. 키 원문은
  UI 저장소에 남기지 않고 백엔드가 salted scrypt 해시만 보관한다. 발급 토큰은 한 번만 표시한다.
  기존 `install.sh`의 파일 다운로드, 코드 복사, 동반 파일 ZIP 다운로드를 함께 제공한다.
  현재 스크립트는 연계 토큰 입력과 WireGuard 신규 연결을 지원하지 않는다.
- 환경 모니터링은 `/targets`의 접근 가능한 공용·세션 환경과
  `/targets/{id}/observations`를 조회한다. AWS/GCP/OpenStack 필터, 노드 수집 정상·수집/응답 실패·미수집·
  오래된 관측 요약, 지표별 수집 시각을 표시한다. 최대 4개 환경을 동시에 조회하고
  화면이 열려 있을 때 30초마다 갱신한다. CPU·메모리·루트 디스크·네트워크는 대상 노드 전체다.
  미수집/실패/stale은 0으로 바꾸지 않으며 과거 시계열을 합성하지 않는다.
  환경 필터는 아래 선택 실행의 상세·앱 로그를 바꾸지 않는다.
- 제출은 자동 재시도하지 않는다. 배포 재요청에는 기존 Idempotency-Key를 유지한다.
  상태 조회 중지와 페이지 종료는 서버 실행을 취소하지 않는다. unknown 결과는 자동
  재실행하지 않으며 서버가 재조정될 때까지 신규 접수를 차단한다.
- 선택한 provider에 앱 배포 사양이 하나 연결되어 있으면 같은 카드에서 새 환경을 준비한다.
  DB 사양은 PostgreSQL·DCS·proxy 수량을 보여 주며 required DB는 해제할 수 없다.
  소스 이름에서 앱 이름을 정하고 `/plans`로 비용·만료 시각을 확인한 뒤 하나의 `plan_id`로
  환경 준비부터 DB 연결·앱 배포까지 요청한다. 사양이 여러 개이거나 준비되지 않았으면 차단한다.
  계획 불일치·만료·예산 초과는 자원 생성 전에 차단하며 비용은 확인 화면에 표시한다.
- 환경 계획 `/plans`는 최대 10분 기다린다. 앱 중지·재개·삭제 계획은 `Prefer: respond-async`로 접수하고 반환된 계획 주소를 2초마다 조회한다. 경과 시간·실패 이유·다시 확인 버튼을 표시하며, 창을 닫거나 새로고침해도 저장한 계획 ID로 다시 조회한다. 계획 조회 실패는 같은 ID로 재조회하고, 계획 자체가 실패하거나 만료됐을 때만 새 계획을 생성한다. 삭제 실행은 유효한 계획을 확인한 별도 승인 이후에만 접수한다.
  컨테이너 Nginx와 외부 ALB의 timeout은 610초다. 다른 POST는 120초, 조회는 15초다.
- 환경 준비·이미지 게시 API는 기존 CLI/운영 경로에서 유지한다. 앱 배포 화면에
  내부 실행 종류나 target ID 선택을 추가하지 않는다. 콘솔은 확인된 작업·환경 상태를
  표시하며 앱 로그 수집이 미연결이면 그 상태를 표시한다.

UI 개발은 `npm run dev`(Vite 4181), 정적 빌드는 `npm run build`다. Vite 개발 서버는
기본 API와 같은 origin이 아니므로 전체 연결은 API가 제공하는 4173 또는 운영 프록시를 사용한다.
브라우저 검사는 [ci/browser](../../ci/browser/README.md)를 따른다.

앱+DB 브라우저 검사는 같은 카드에서 DB 계획 식별자, 필수 DB, DB 없는 사양, 비용 표시,
예산·만료·불일치 차단을 localhost fixture로 확인한다. 실제 클라우드는 호출하지 않는다.

## Repeated app submissions

The new-deployment form resolves the source-derived or explicit app name through `GET /api/v1/applications/resolve` using the selected provider's registered environment. The lookup is server-owned and independent of the visible application inventory page. A successful app owned by the same session opens the existing update preview with the selected GitHub/ZIP/folder source preserved; the app ID, target, namespace and public address stay fixed. The user reviews added/modified/deleted files before starting. Verified identical source can finish unchanged without CI, or be explicitly rebuilt.

Another session's matching name remains an ownership conflict. Different names/environments are separate apps; repository URL alone does not select an app because one repository may have multiple deployments. Stopped/deleted/pending apps are not silently recreated. A ready registration with no successful baseline can retry normal deployment. The explicit app-detail update flow still supports source files whose archive/repository name changes.

An uncertain published CD/HTTP result fences its registered environment, so a GCP route reconciliation does not block a same-named AWS application. Uncertain CI/registration or missing environment bindings keep the conservative name-level fence because CI source paths are shared. The affected environment still requires reconciliation; this does not retry it or alter its resources.
