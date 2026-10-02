# RailShot dashboard

저장소 루트에서 `npm ci --ignore-scripts` 후 `npm start --workspace apps/api`를 실행하고
`http://127.0.0.1:4173`을 연다. 로그인·회원·브라우저 토큰 입력은 없다.
공개 배치의 설정과 실행 범위는 [제품 API 운영 계약](../../docs/api/product.md)을 따른다.

- UI 기준은 `006ad34`의 소스 → 클라우드/온프레미스 → 선택 내용 확인 → 배포 시작이다.
  기존 카드·OpenStack/Proxmox 선택·콘솔 탭을 유지한다. 화면은
  `/api/v1/deployment-options`에서 준비 상태를 읽고 소스와 environment/provider만
  deployments API에 보낸다. 등록 대상의 ID·앱 이름 결정과 실행 가능 여부 검사는 서버가 수행한다.
  미연결 provider는 명확히 차단하며 다른 대상에 배포하지 않는다.
- 202의 `Location`과 자원 ID를 확인한 뒤 15초마다 상태를 조회한다. 이미지 게시와
  앱 배포 성공을 구분한다. 배포 성공 및 HTTP 검증 시각이 있을 때만 앱 링크를 표시한다.
- localStorage에는 마지막 실행의 종류·ID·앱 이름만 저장한다. 새로고침 시 서버의 영속
  기록을 다시 조회하며, 소스·토큰을 브라우저 저장소에 보관하지 않는다. 사용자별 계정이나
  실행 내역 격리는 없으며 이 배치는 공유 데모 workspace다.
- 제출은 자동 재시도하지 않는다. 배포 재요청에는 기존 Idempotency-Key를 유지한다.
  상태 조회 중지와 페이지 종료는 서버 실행을 취소하지 않는다. unknown 결과는 자동
  재실행하지 않으며 서버가 재조정될 때까지 신규 접수를 차단한다.
- 선택한 provider에 앱 배포 사양이 하나 연결되어 있으면 같은 카드에서 새 환경을 준비한다.
  DB 사양은 PostgreSQL·DCS·proxy 수량을 보여 주며 required DB는 해제할 수 없다.
  소스 이름에서 앱 이름을 정하고 `/plans`로 비용·만료 시각을 확인한 뒤 하나의 `plan_id`로
  환경 준비부터 DB 연결·앱 배포까지 요청한다. 사양이 여러 개이거나 준비되지 않았으면 차단한다.
  계획 불일치·만료·예산 초과는 자원 생성 전에 차단하며 비용은 확인 화면에 표시한다.
- 여러 노드의 Terraform 계획을 순차 생성할 수 있도록 `/plans` POST는 최대 10분 기다린다.
  컨테이너 Nginx와 외부 ALB의 timeout은 610초다. 다른 POST는 120초, 조회는 15초다.
- 환경 준비·이미지 게시 API는 기존 CLI/운영 경로에서 유지한다. 앱 배포 화면에
  내부 실행 종류나 target ID 선택을 추가하지 않는다. 콘솔은 확인된 작업·환경 상태를
  표시하며 앱 로그 수집이 미연결이면 그 상태를 표시한다.

UI 개발은 `npm run dev`(Vite 4181), 정적 빌드는 `npm run build`다. Vite 개발 서버는
기본 API와 같은 origin이 아니므로 전체 연결은 API가 제공하는 4173 또는 운영 프록시를 사용한다.
브라우저 검사는 [ci/browser](../../ci/browser/README.md)를 따른다.

앱+DB 브라우저 검사는 같은 카드에서 DB 계획 식별자, 필수 DB, DB 없는 사양, 비용 표시,
예산·만료·불일치 차단을 localhost fixture로 확인한다. 실제 클라우드는 호출하지 않는다.
