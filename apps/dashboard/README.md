# RailShot dashboard

저장소 루트에서 `npm ci --ignore-scripts` 후 `npm start --workspace apps/api`를 실행하고
`http://127.0.0.1:4173`을 연다. 로그인·회원·브라우저 토큰 입력은 없다.
공개 배치의 설정과 실행 범위는 [제품 API 운영 계약](../../docs/api/product.md)을 따른다.

- `/api/v1/targets`의 실제 대상과 capabilities를 사용한다. ZIP·폴더·공개 GitHub
  저장소를 `builds` 또는 `deployments`로 제출한다. 소스 검사·압축 해제·경로/크기 제한은
  서버가 수행한다. 배포할 앱 이름이 target에 고정된 경우 그 이름을 사용한다.
- 202의 `Location`과 자원 ID를 확인한 뒤 15초마다 상태를 조회한다. 이미지 게시와
  앱 배포 성공을 구분한다. 배포 성공 및 HTTP 검증 시각이 있을 때만 앱 링크를 표시한다.
- localStorage에는 마지막 실행의 종류·ID·앱 이름만 저장한다. 새로고침 시 서버의 영속
  기록을 다시 조회하며, 소스·토큰을 브라우저 저장소에 보관하지 않는다. 사용자별 계정이나
  실행 내역 격리는 없으며 이 배치는 공유 데모 workspace다.
- 제출은 자동 재시도하지 않는다. 배포 재요청에는 기존 Idempotency-Key를 유지한다.
  상태 조회 중지와 페이지 종료는 서버 실행을 취소하지 않는다. unknown 결과는 자동
  재실행하지 않으며 서버가 재조정될 때까지 신규 접수를 차단한다.
- 실행 대상에서 새 환경 사양을 고르면 계획 확인 후 하나의 `plan_id`로 환경 준비부터 앱 배포까지
  요청한다. 사양의 `target_id`와 등록 앱 이름을 사용하며, DB를 선택하면 공개된 PostgreSQL·DCS·
  proxy 수량을 같은 `profile_id`의 placement로 전달한다. `deployment_supported`가 없는 사양은
  이 경로에서 선택할 수 없다. DB가 없는 사양과 기존 실행 대상은 기존 배포·이미지 게시 동작을 유지한다.
  `database.required` 사양은 두 환경 폼에서 PostgreSQL HA를 고정하고 DB 없음 선택을 막는다.
- 별도 새 환경 준비 화면도 같은 사양으로 runtime 한 노드와 선택적 PostgreSQL HA의 계획을
  저장하고 `/environments`로 실행한다. 앱 소스 제출 없이 환경만 준비하는 경로다. 두 경로 모두
  실제 자원을 만드는 버튼 앞에서 계획을 표시하고, 만료한 계획은 다시 확인하게 한다.
- 여러 노드의 Terraform 계획을 순차 생성할 수 있도록 `/plans` POST는 최대 10분 기다린다.
  컨테이너 Nginx의 upstream read timeout은 610초이며, 외부 ALB의 idle timeout도 배치 담당자가
  이에 맞춰 설정해야 한다. 다른 브라우저 POST는 기존 120초, 조회는 15초 제한을 유지한다.

UI 개발은 `npm run dev`(Vite 4181), 정적 빌드는 `npm run build`다. Vite 개발 서버는
기본 API와 같은 origin이 아니므로 전체 연결은 API가 제공하는 4173 또는 운영 프록시를 사용한다.
브라우저 검사는 [ci/browser](../../ci/browser/README.md)를 따른다.
환경 폼 회귀 검사도 `npm test --prefix ci/browser`에 포함된다.
DB 사양→계획→앱 배포의 식별자 일치, DB 없는 사양, 별도 환경 준비, 기존 대상 제출과
계획 불일치·만료 차단을 localhost fixture에서 검사하며 cloud를 호출하지 않는다.
