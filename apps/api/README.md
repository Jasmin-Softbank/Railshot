# RailShot API의 현재 범위

로컬 전용 Node 서버다. `.env.example`을 루트 `.env`로 복사하고 `GITHUB_TOKEN`을 설정한 뒤 루트에서 `npm run dev:api`를 실행한다. 서버는 `127.0.0.1:4182`에만 바인딩된다. 이 로컬 보호 방식은 공개 웹 서비스의 사용자 인증·대상 권한 검사를 대신하지 않는다.

- `POST /api/deployments`: JSON `{ "app": "memo-sqlite", "target": { "environment": "cloud", "provider": "aws" } }`. `X-RailShot-Request: 1` 헤더가 필요하다. 이 헤더는 브라우저의 다른 출처 요청을 막는 용도이며 사용자 인증은 아니다. 서버가 정한 tenant의 기존 앱을 확인하고 `railshot-deploy.yml`을 실행한다. 성공 시 실행 ID와 Actions URL을 반환한다. 과거 GitHub API처럼 실행 ID 없이 접수되면 `runId: null`을 반환하며 재시도 전에 Actions를 확인해야 한다.
- `GET /api/deployments/:runId`: Actions 실행, job 및 각 step의 실제 상태를 반환한다. 공개 URL HTTP 검사는 워크플로의 해당 step 결과로만 표시한다. 배포 대상 Ready와 앱 버전은 아직 확인하지 않는다.
- `GET /api/health`: 로컬 API 생존 확인.

현재 워크플로는 `tenant`와 `app`만 받으며 GitOps 경로가 AWS로 고정돼 있다. 새 소스를 배포하려면 먼저 `railshot-apps/apps/<tenant>/<app>`에 등록해야 한다. 이 API는 ZIP·폴더·GitHub URL 업로드를 받지 않는다. 제공자 비밀 값과 GitHub 토큰은 브라우저로 보내지 않는다. GitHub Actions 실행 기록은 GitHub에 남지만 RailShot 자체의 영속 작업 로그 저장소는 아직 없다.
