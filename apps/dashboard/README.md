# RailShot dashboard UI

배포 대시보드이다. 저장소 루트에서 아래처럼 실행한다.

```sh
npm install
npm run dev
```

브라우저에서 `http://127.0.0.1:4181`을 연다.

현재 구현 범위는 소스 선택, 클라우드/온프레미스 환경 선택, 선택 내용 확인, 배포 내역 및 환경 모니터링의 빈 상태다. 네트워크 요청과 실제 배포는 수행하지 않는다. 온프레미스 제공자는 OpenStack과 Proxmox를 선택할 수 있지만 연결 정보 입력은 아직 없다.

Vite는 개발 중 대시보드의 정적 파일을 제공하고, `npm run build`는 배포용 `dist/`를 만든다.

저장소 루트에서 `docker build -f apps/dashboard/Dockerfile --target dashboard -t railshot-dashboard .`로 정적 이미지를 만든다. build stage에서 lockfile로 Vite를 설치하고 `dist/`만 Nginx 이미지에 복사한다. 최종 이미지는 UID 101, HTTP 8080으로 실행하며 `/healthz`가 준비 확인 경로다. root filesystem을 읽기 전용으로 사용할 때 `/tmp`에 쓰기 가능한 임시 볼륨이 필요하다. `NODE_IMAGE`와 `NGINX_IMAGE` build argument로 검토한 base image digest를 지정할 수 있다.

이 이미지에는 API 자격증명·Node 서버·MCP가 없다. `/api` 프록시도 아직 없으며 기존 비활성 배포 버튼과 화면 범위를 유지한다. API 연결은 내부 API 인증·사용자별 권한을 정한 뒤 별도 작업으로 진행한다.
