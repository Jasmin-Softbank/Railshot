# RailShot 서비스 내부 에이전트

Node 22 이상에서 실행합니다. 이 서비스는 모델이 MCP 도구를 통해 RailShot 제품 API를 조회하고, 공개 GitHub 저장소의 배포를 제안합니다. 읽기 호출은 즉시 실행하고 배포 요청은 정확한 인자를 보여준 뒤 승인 토큰을 받은 별도 요청에서 실행합니다. API의 공통 권한·입력 검증·멱등성·상태 기록을 그대로 사용하며, 에이전트가 인프라에 직접 접근하지 않습니다.

## 설정과 실행

저장소 루트에서 `npm ci --ignore-scripts` 후 다음 환경변수를 설정합니다.

| 변수 | 의미 |
| --- | --- |
| `OPENAI_API_KEY` | 서버에서만 사용하는 모델 API 키 |
| `RAILSHOT_AGENT_MODEL` | Responses API에서 사용할 모델명 |
| `RAILSHOT_AGENT_TOKEN` | 에이전트 HTTP 호출용 32자 이상 비밀 토큰. 승인 토큰 서명에도 사용 |
| `RAILSHOT_API_URL` | 제품 API 원점. 기본 `http://127.0.0.1:4173` |
| `RAILSHOT_API_TOKEN` 또는 `RAILSHOT_API_TOKEN_FILE` | 제품 API의 내부 Bearer 토큰. 둘 중 하나만 설정 |
| `RAILSHOT_AGENT_PORT` | 로컬 포트. 기본 `4184` |

`npm run start --workspace @railshot/agent`로 서비스, `npm run mcp --workspace @railshot/agent`로 독립 stdio MCP 도구 서버를 시작합니다. HTTP 서비스는 `127.0.0.1`에만 바인딩하고 브라우저 Origin 요청을 거부합니다. 대시보드에서 사용할 때는 사용자 인증을 제공하는 서버 프록시가 필요합니다. 토큰은 브라우저에 전달하지 마세요.

## HTTP 계약

모든 요청은 `Authorization: Bearer <RAILSHOT_AGENT_TOKEN>`을 사용합니다.

- `POST /v1/messages`, `Content-Type: application/json`, 본문 `{"message":"demo 대상의 배포 상태를 확인해 줘"}`: `answered`와 답변·읽기 도구 결과를 반환합니다. 배포가 제안되면 `approval_required`, 설명, `action`, `approval_token`을 반환하고 실행하지 않습니다.
- `POST /v1/approvals`, 본문 `{"approval_token":"..."}`: 제안된 인자를 변경하지 않고 MCP 도구로 배포를 한 번 요청합니다. 토큰은 10분 유효하며 같은 토큰 재호출은 제품 API의 같은 `Idempotency-Key`로 처리됩니다.
- `GET /healthz`: 프로세스 생존만 나타냅니다.

MCP 도구는 `list_options`, `list_targets`, `get_deployment`, `get_build`, `deploy_repository`입니다. 마지막 도구의 성공은 **요청 접수**이며 배포 성공이 아닙니다. 배포 완료는 `get_deployment`에서 대상 적용 및 공개 HTTP 검증 후 `status=succeeded`와 URL을 확인해야 합니다. 현재 에이전트 입력은 공개 GitHub 저장소 URL만 지원합니다. 파일·ZIP 업로드, 환경 생성 및 소스 자동 수정은 포함하지 않습니다.

`npm test --workspace @railshot/agent`는 모델·클라우드 호출 없이 승인 흐름과 실제 MCP stdio 프로토콜 및 제품 API 전달을 확인합니다.
