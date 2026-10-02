# RailShot dashboard

저장소 루트에서 `npm ci --ignore-scripts` 후 `npm start --workspace apps/api`를 실행하고
`http://127.0.0.1:4173`을 연다. 로그인·회원·브라우저 토큰 입력은 없다.
공개 배치의 설정과 실행 범위는 [제품 API 운영 계약](../../docs/api/product.md)을 따른다.

- `/api/v1/targets`의 실제 대상과 capabilities를 사용한다. ZIP·폴더·공개 GitHub 또는 등록된 앱 이름을
  저장소를 `builds` 또는 `deployments`로 제출한다. 소스 검사·압축 해제·경로/크기 제한은
  서버가 수행한다. 배포할 앱 이름이 target에 고정된 경우 그 이름을 사용한다.
- 202의 `Location`과 자원 ID를 확인한 뒤 15초마다 상태를 조회한다. 이미지 게시와
  앱 배포 성공을 구분하고 GitHub Actions의 개별 step을 표시한다. 배포 성공 및 HTTP 검증 시각이 있을 때만 앱 링크를 표시한다.
- localStorage에는 마지막 실행의 종류·ID·앱 이름만 저장한다. 새로고침 시 서버의 영속
  기록을 다시 조회하며, 소스·토큰을 브라우저 저장소에 보관하지 않는다. 사용자별 계정이나
  실행 내역 격리는 없으며 이 배치는 공유 데모 workspace다.
- 제출은 자동 재시도하지 않는다. 배포 재요청에는 기존 Idempotency-Key를 유지한다.
  상태 조회 중지와 페이지 종료는 서버 실행을 취소하지 않는다. unknown 결과는 자동
  재실행하지 않으며 서버가 재조정될 때까지 신규 접수를 차단한다.
- 새 환경 화면은 서버가 등록한 `profiles`로 계획을 저장하고, 검토한 `plan_id`로 환경
  준비를 요청한다. 현재 AWS/GCP runtime 한 노드·DB 없음만 지원한다. runtime 준비와
  CI/CD·공개 경로 등록을 별도 상태로 표시한다. 미지원 기능을 실행 가능한 것으로 표시하지 않는다.

UI 개발은 `npm run dev`(Vite 4181), 정적 빌드는 `npm run build`다. Vite 개발 서버는
기본 API와 같은 origin이 아니므로 전체 연결은 API가 제공하는 4173 또는 운영 프록시를 사용한다.
브라우저 검사는 [ci/browser](../../ci/browser/README.md)를 따른다.
