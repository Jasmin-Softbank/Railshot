# RailShot dashboard

저장소 루트에서 `.env.example`을 `.env`로 복사하고 서버 전용 `GITHUB_TOKEN`을 설정한다. 토큰에는 `railshot-apps`의 Actions 쓰기와 Contents 읽기 권한이 필요하다. 터미널 두 개에서 실행한다.

```sh
npm install
npm run dev:api
```

```sh
npm run dev
```

브라우저에서 `http://127.0.0.1:4181`을 연다.

**등록된 앱 재배포**에 `railshot-apps/apps/<tenant>/<app>`의 앱 이름을 입력하고 RailShot AWS를 선택하면 Actions 실행을 요청한다. 예시 저장소에는 `demo/memo-sqlite`가 있다. 실행 후 환경 모니터링에서 Actions job과 각 step의 상태를 본다. 전체 로그는 GitHub Actions 링크에서 확인한다. 상태 조회가 완료되어도 대상 Ready·앱 버전까지 검증됐다는 뜻은 아니다.

ZIP·폴더·공개 GitHub URL과 OpenStack·Proxmox 선택은 현재 선택 내용만 확인할 수 있다. 소스 등록과 온프레미스 실행 API는 연결되지 않았다. 배포 내역 영속 저장, 앱 로그, 인프라 지표도 아직 없다. Vite는 개발 중 `/api`를 로컬 API로 전달하며, `npm run build`는 정적 파일만 만든다. 공개 서비스에는 별도 사용자 인증과 같은 출처의 API 라우팅이 필요하다.
