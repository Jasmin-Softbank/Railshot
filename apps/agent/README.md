# RailShot 서비스 내부 에이전트

Node 22 이상에서 실행합니다. 이 패키지의 stdio MCP 서버는 RailShot 제품 API의 조회와 공개 GitHub 저장소 배포 요청을 도구로 제공합니다. 원격 MCP 서버는 로컬 ZIP·개별 파일·폴더 배포를 위한 브라우저 업로드 링크도 발급합니다. 별도 HTTP 대화 에이전트는 모델의 배포 제안을 승인 토큰으로 확인한 뒤 실행합니다. 외부 AI가 MCP 서버를 직접 등록해 `deploy_repository`를 호출하면 이 HTTP 승인 절차를 거치지 않고 즉시 제품 API에 배포 요청을 보냅니다. 이 경우 사용자 확인은 MCP 호스트에서 설정해야 합니다. API의 공통 입력 검증·멱등성·상태 기록을 사용하며, MCP가 인프라에 직접 접근하지 않습니다.

## 설정과 실행

저장소 루트에서 `npm ci --ignore-scripts` 후 다음 환경변수를 설정합니다.

| 변수 | 의미 |
| --- | --- |
| `OPENAI_API_KEY` | 서버에서만 사용하는 모델 API 키 |
| `RAILSHOT_AGENT_MODEL` | Responses API에서 사용할 모델명 |
| `RAILSHOT_AGENT_TOKEN` | 에이전트 HTTP 호출용 32자 이상 비밀 토큰. 승인 토큰 서명에도 사용 |
| `RAILSHOT_API_URL` | 제품 API 원점. 기본 `http://127.0.0.1:4173` |
| `RAILSHOT_API_TOKEN` 또는 `RAILSHOT_API_TOKEN_FILE` | 제품 API의 내부 Bearer 토큰. 둘 중 하나만 설정 |
| `RAILSHOT_AGENT_SESSION_DIR` | 선택 사항. 제품 API 세션 쿠키의 사용자 전용 저장 디렉터리. 기본 `~/.local/state/railshot-agent`; 절대 경로와 소유자 전용 권한 필요 |
| `RAILSHOT_AGENT_PORT` | 로컬 포트. 기본 `4184` |

`npm run start --workspace @railshot/agent`로 서비스, `npm run mcp --workspace @railshot/agent`로 독립 stdio MCP 도구 서버를 시작합니다. HTTP 서비스는 `127.0.0.1`에만 바인딩하고 브라우저 Origin 요청을 거부합니다. 대시보드에서 사용할 때는 사용자 인증을 제공하는 서버 프록시가 필요합니다. 토큰은 브라우저에 전달하지 마세요.

로컬 stdio MCP 진입점은 `apps/agent/src/mcp.js`입니다. 원격 Streamable HTTP MCP 진입점은 `apps/agent/src/remote-mcp.js`입니다. `apps/api/Dockerfile`의 `mcp` 이미지는 기본적으로 stdio를 실행하며 원격 배포에서는 명령을 `node src/remote-mcp.js`로 바꿉니다. 제품 API의 구형 `/api/deploy`는 CLI 호환 경로로 남아 있지만 MCP 배포에는 사용하지 않습니다.

## ChatGPT·Codex 원격 연결

운영 플랫폼에서 dashboard, api, mcp 이미지를 함께 배포하면 Nginx가 `https://railshot.io/mcp`를 MCP 서버로 연결합니다. 공개 OAuth 메타데이터는 `/.well-known/oauth-protected-resource/mcp`와 `/.well-known/oauth-authorization-server`에 있습니다. MCP 서버는 내부 API 토큰을 서버 측에서만 사용합니다.

- ChatGPT에서는 개발자 모드를 켜고 Plugins의 새 MCP 연결에 `https://railshot.io/mcp`를 입력합니다. 인증 방식은 OAuth이며 동적 클라이언트 등록(DCR)을 사용합니다.
- Codex CLI에서는 `codex mcp add railshot --url https://railshot.io/mcp`로 등록합니다. 등록 중 OAuth가 시작될 수 있으며, 연결을 다시 승인할 때 `codex mcp login railshot --oauth-client-registration dcr`을 실행합니다. Codex 앱 또는 IDE에서는 Streamable HTTP 서버 URL을 추가하고 인증을 시작합니다.
- 승인 화면은 **웹 앱을 쓰던 동일한 브라우저**에서 열어 `연결 승인`을 누릅니다. 이때 기존 `railshot_session` 쿠키를 AI 연결에 묶습니다. 웹 방문 기록이 없는 브라우저에서는 새 익명 세션을 발급하며, 이후 그 브라우저에서 Railshot을 열면 AI가 만든 앱을 같은 세션에서 볼 수 있습니다.

원격 AI 연결마다 별도 OAuth Bearer 토큰을 발급하고 서버에서 해당 토큰을 정확히 하나의 API 세션 쿠키에 연결합니다. 웹의 `sessions.js`는 수정하지 않았습니다. 인증 코드와 Bearer 토큰은 MCP 프로세스 메모리에만 보관하므로 MCP Pod가 재시작되면 AI에서 OAuth 연결을 다시 승인해야 합니다. 등록된 클라이언트 ID는 내부 API 토큰으로 서명되어 재시작 후에도 재사용할 수 있습니다. Bearer 토큰은 최대 24시간, 웹 세션은 최대 7일 유효합니다. 서로 다른 브라우저 세션의 앱은 자동 병합되지 않습니다.

## HTTP 계약

모든 요청은 `Authorization: Bearer <RAILSHOT_AGENT_TOKEN>`을 사용합니다.

- `POST /v1/messages`, `Content-Type: application/json`, 본문 `{"message":"demo 대상의 배포 상태를 확인해 줘"}`: `answered`와 답변·읽기 도구 결과를 반환합니다. 배포가 제안되면 `approval_required`, 설명, `action`, `approval_token`을 반환하고 실행하지 않습니다.
- `POST /v1/approvals`, 본문 `{"approval_token":"..."}`: 제안된 인자를 변경하지 않고 MCP 도구로 배포를 한 번 요청합니다. 토큰은 10분 유효하며 같은 토큰 재호출은 제품 API의 같은 `Idempotency-Key`로 처리됩니다.
- `GET /healthz`: 프로세스 생존만 나타냅니다.

공통 MCP 도구는 `list_options`, `list_targets`, `get_deployment`, `get_build`, `deploy_repository`입니다. 원격 MCP에는 `prepare_file_deployment`와 `get_file_upload`가 추가됩니다. 각 도구의 입력·결과·사용 순서는 [MCP 도구 사용 가이드](MCP_TOOLS.md)에 정리했습니다. 배포 요청 접수는 배포 성공이 아닙니다. 배포 완료는 `get_deployment`에서 대상 적용 및 공개 HTTP 검증 후 `status=succeeded`와 URL을 확인해야 합니다. 별도 HTTP 대화 에이전트의 입력은 공개 GitHub 저장소 URL만 지원하며, 환경 생성 및 소스 자동 수정은 포함하지 않습니다.

로컬 stdio MCP는 API 원점별 세션 쿠키를 `RAILSHOT_AGENT_SESSION_DIR`에 저장해 후속 조회와 MCP 프로세스 재시작 시 재전송합니다. 같은 API 원점과 쿠키 디렉터리를 공유하는 로컬 MCP 프로세스는 제품 API에서 같은 세션으로 취급됩니다. 이 쿠키는 해당 세션의 작업 조회 권한을 가지므로 디렉터리와 파일을 다른 OS 사용자가 읽을 수 없게 유지합니다. 원격 HTTP MCP는 이 공유 파일을 사용하지 않습니다. 원격 API에서 서로 다른 쿠키를 사용하는 세션의 배포 ID 조회는 차단되지만, 비공개 localhost의 쿠키 없는 유지보수 요청은 세션 소유권 제한을 적용하지 않습니다. 사용자 컴퓨터에 내부 API Bearer 토큰을 배포하는 것은 별도의 사용자 인증·권한 모델을 대신하지 않습니다.

`npm test --workspace @railshot/agent`는 모델·클라우드 호출 없이 승인 흐름과 실제 MCP stdio 프로토콜 및 제품 API 전달을 확인합니다.

실제 시험 대상에서는 운영자가 연결한 API와 등록된 대상에 대해 `list_targets` → `deploy_repository` → 반환된 `resource_id`로 `get_deployment`를 호출해 접수와 후속 조회를 확인합니다. `deploy_repository`는 실제 배포를 시작하므로 시험용 공개 저장소·앱 이름·대상을 사용하고 MCP 호스트에서 실행을 승인한 뒤 호출하세요. `accepted`는 CI/CD 완료를 의미하지 않습니다.
