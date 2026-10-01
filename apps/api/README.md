# 사용자 API · CLI · MCP

홍진기의 `feature/fe-mcp-jingi@6840d1387798d375234bbf97919210eec96709a3`를 바탕으로 통합했다. 정적 화면은 `../dashboard`, Node 서버·CLI·MCP는 이 package에 있다. API는 기본적으로 `127.0.0.1:4173`만 수신하며 localhost Host/Origin 검사를 유지한다. ALB 뒤 운영 인증·허용 Origin 연결은 아직 구현하지 않았다.

## 로컬 시작

Node 22 이상에서 `npm ci --ignore-scripts`, `npm start`를 실행한다. 이 명령은 SDK나 cloud를 실행하지 않는다. 설정이 없으면 화면과 `/healthz`만 사용할 수 있고 제출은 503이다. `npm test`는 실제 GitHub/cloud 없이 mock GitHub와 로컬 HTTP를 검사한다.

실제 CI 제출에는 운영자가 process 환경으로 다음을 설정해야 한다. `.env`를 자동으로 읽지 않는다. 로컬 파일을 사용한다면 직접 `node --env-file=.env src/server.js`로 실행하며 파일은 Git에 추가하지 않는다.

| 변수 | 의미 |
| --- | --- |
| `GITHUB_TOKEN` | 업로드용 apps repository의 Git tree/commit/ref 및 Actions 호출 권한. 응답에 포함하지 않음 |
| `GITHUB_OWNER`, `GITHUB_REPO` | 기본 `Jasmin-Softbank`, `railshot-apps`; 통합 platform source repo와 별도 |
| `GITHUB_REF`, `GITHUB_WORKFLOW` | 기본 `main`, `railshot-deploy.yml` |
| `JASMIN_TENANT` | 소문자·숫자 1–20자. 기본 `demo` |
| `RAILSHOT_TARGET_ID` | 필수 운영자 target ID. CI 저장소의 같은 이름 변수와 일치해야 함 |
| `JASMIN_API_URL` | CLI/MCP의 API 주소. 기본 `http://127.0.0.1:4173` |
| `JASMIN_SOURCE_ROOT` | MCP local source의 허용 root |

`npm run cli -- deploy <폴더 또는 ZIP 또는 공개 GitHub URL> --app my-app --target aws-demo`, `npm run cli -- status <run_id>`가 같은 HTTP API를 사용한다. target 생략 시 API의 운영자 설정을 사용한다. 임의 target은 거부한다. 이 제출 명령은 실제 설정이 있을 때 GitHub에 소스를 등록하고 CI를 실행하므로 로컬 검증 과정에서는 실행하지 않는다.

MCP는 `npm run mcp`로 stdio transport를 사용한다. 기존 deploy/status 도구를 유지하며 CI의 published 상태를 앱 deployed로 바꾸지 않는다.

## 연결한 동작

- 앱 이름은 CI와 같은 3–30자 규칙이다. 영문 소문자로 시작하고 영문 소문자나 숫자로 끝나며 중간에는 하이픈을 쓸 수 있다.
- source 등록 commit SHA와 target ID를 workflow에 전달한다. CI checkout과 source SHA가 다르면 실행을 거부해 concurrent 등록 drift를 드러낸다.
- 상태는 loop/release 두 job을 읽는다. 완료된 재실행에 job이 없으면 이전 attempt의 실제 producer를 찾는다. 최신 실패를 옛 성공으로 대체하지 않는다.
- `published-<producer attempt>`의 유일하고 만료되지 않은 artifact를 고유 ID로 다운로드해 run/source/target과 evidence 파일 해시를 확인한다. 이를 통과해야 state가 `published`다.
- `published`는 이미지 게시 및 CD 자료 생성이다. 앱 적용, Argo 상태 관측, 외부 HTTP와 URL은 이 API가 수행하지 않는다. `url`은 null이다.

[API 계약](docs/interface.md), [소스 형식](docs/source-formats.md), [CI→CD 경계](../../docs/api/ci-publication.md)를 함께 참고한다.
