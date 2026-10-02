# RailShot dashboard UI

배포 대시보드이다. API와 함께 실행하려면 저장소 루트에서 다음을 실행한다.

```sh
npm ci --prefix apps/api --ignore-scripts
npm run --prefix apps/api start
```

브라우저에서 `http://127.0.0.1:4173`을 연다. API에 GitHub 및 운영자 대상 설정이
없으면 소스·환경 선택과 검토는 가능하지만 CI 제출은 차단된다. 인증을 요구하는
API는 운영자가 제공한 API 접근 토큰을 입력한다. 토큰은 API 요청 헤더에만 전달하고
브라우저 저장소·URL·로그에 기록하지 않으며 페이지를 나갈 때 입력값을 지운다.

ZIP·폴더·공개 GitHub 저장소를 선택하고 앱 이름을 확인한 뒤 **운영자 등록 대상**을
선택하면 `/api/deploy`로 소스를 제출한다. 앱 이름이 같으면 해당 앱 소스를 갱신한다.
폴더는 선택한 최상위 폴더를 제외한 상대 경로로 전달하며 `.git`, `node_modules`,
`__MACOSX`, `.DS_Store`는 제외한다. 비밀 파일과 업로드 크기·경로는 API가 검사한다.

클라우드·온프레미스 선택은 기존 검토 화면에 유지되어 있지만 아직 등록 대상과의
연결 정보를 확인할 수 없어 제출하지 않는다. 등록 대상은 `/healthz`에서 확인하며,
원격 API가 대상 ID를 숨기면 서버에 설정된 대상을 사용한다. 대상 이름을 보고
클라우드 종류를 추정하거나 인프라를 자동 생성하지 않는다.

제출 후 `/api/runs/{run_id}`를 15초마다 조회한다. 이미지 게시 완료, 게시 증거 미확인,
CI 실패는 서로 구분하며 이미지 게시로 앱 배포 완료나 외부 URL을 표시하지 않는다.
완료·조회 오류·수동 중지·페이지 종료 시 자동 조회를 멈춘다. 조회 중지는 서버의 CI
실행을 취소하지 않는다. 제출은 자동 재시도하지 않으며, 전송 오류가 발생하면 실제
CI 실행 여부를 GitHub Actions에서 확인한 후 다시 검토해야 한다. 내역은 현재
페이지에서 제출한 마지막 실행만 표시하며 새로 고침 뒤에는 보존되지 않는다.

UI만 개발할 때는 루트에서 `npm ci && npm run dev`로 Vite를 실행한다
(`http://127.0.0.1:4181`). Vite 단독 실행에는 API가 연결되지 않아 제출이 차단된다.
`npm run build`는 정적 배포용 `dist/`를 만든다. 실제 API 통합 검사는
[`ci/browser/README.md`](../../ci/browser/README.md)의 브라우저 검사를 사용한다.

## 컨테이너 이미지

저장소 루트에서 `docker build -f apps/dashboard/Dockerfile --target dashboard -t railshot-dashboard .`로 정적 이미지를 만든다. build stage에서 lockfile로 Vite를 설치하고 `dist/`만 Nginx 이미지에 복사한다. 최종 이미지는 UID 101, HTTP 8080으로 실행하며 `/healthz`가 준비 확인 경로다. root filesystem을 읽기 전용으로 사용할 때 `/tmp`에 쓰기 가능한 임시 볼륨이 필요하다. `NODE_IMAGE`와 `NGINX_IMAGE` build argument로 검토한 base image digest를 지정할 수 있다.

이 이미지에는 API 자격증명·Node 서버·MCP가 없다. 위의 API 동시 실행과 달리 정적 Nginx 컨테이너의 `/api` 프록시는 아직 없으므로 이 컨테이너 단독 실행은 CI 제출 연결을 제공하지 않는다. 분리 배포의 API 경로와 사용자별 인가는 별도 연결 작업이며 내부 운영자 token을 이미지나 프런트 코드에 포함하지 않는다.
