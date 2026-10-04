# 대시보드 AI 자동 복구 표시 설계

상태: **조회 전용 카드 연동 구현**. PR #133의 배포 이력 상세에 AI 처리·수정 파일·재검증 결과를 표시한다.
CI의 `agent.repair` 기록을 기존 Checks 전달기로 보내고 기존 events API에서 표시 모델을 생성한다.
사용자 질문 제출·응답 이후 재개는 후속 작업이며 자동 실행 권한이나 복구 정책은 바꾸지 않는다.

## 사용자가 확인할 내용

사용자가 알고 싶은 것은 어떤 문제가 생겼고, 지금 무엇을 처리하며, 무엇을 바꿨고,
실제 검사에서 해결됐는지다. 기존 오류 상세의 ‘에이전트 활동 기록’을 다음 카드로 확장한다.

```text
AI 자동 복구                                      재검증 중
컨테이너 시작 검사에 실패했습니다.

수정한 이미지의 실행 상태를 확인하고 있습니다.
이미지 빌드 · 성공
컨테이너 실행 검사 · 진행 중

변경 내용
Dockerfile — 앱의 실제 실행 경로에 맞게 시작 명령 변경

1차 시도 · 경과 42초
[변경 내용 보기] [작업 로그]
```

| 표시 항목 | 내용 | 데이터 출처 |
| --- | --- | --- |
| 문제 요약 | 실패 단계와 관측된 오류 한 문장 | gate 진단 자료 |
| 현재 작업 | AI 분석, 수정 적용, 재검증 | 실행 이벤트와 gate 결과 |
| 수정 내용 | 파일별 변경 이유와 실제 적용 여부 | 모델의 `files_changed`와 실행기의 `written` |
| 검증 결과 | 이미지 빌드·실행 검사 등의 성공/실패 | 해당 시도의 공식 gate 결과 |
| 시도·시간 | 시도 번호, 시작·종료 시각, 마지막 관측까지의 경과 시간 | 호스트가 기록한 실행 정보 |
| 추가 확인 | 자동 처리가 불가능할 때 필요한 사용자 입력 | 서버가 발행한 해결 질문 |

모델의 내부 사고 과정이나 임의의 진행률은 표시하지 않는다. heartbeat만 있으면 ‘AI 처리 중’으로
표시하며, 실제 파일 조사 여부를 추정하지 않는다. 모델의 원인 설명은 ‘AI 분석’으로 표시하고,
관측된 오류와 구분한다. 수정안 제출과 실제 적용도 구분한다.

## 상태와 완료 기준

| `state` | 화면 문구 | 판정 근거 |
| --- | --- | --- |
| `analyzing` | AI 분석 중 | 해당 시도의 모델 실행 시작 확인 |
| `verifying` | 재검증 중 | 수정 적용 후 gate 실행 시작 확인 |
| `succeeded` | 복구 완료 | 해당 수정 소스에 대해 실패했던 검사와 필수 gate 통과 |
| `failed` | 이번 시도 실패 | 해당 시도의 수정 거부 또는 필수 검사 실패. 다음 시도는 별도로 표시 |
| `awaiting_input` (후속) | 사용자 확인 필요 | 유효한 서버 질문 존재 |
| `unknown` | 처리 상태 확인 필요 | 실제 실행 결과가 불확실함 |

`agent.completed`는 모델/수정 단계 종료이지 복구 성공이 아니다. 복구 완료 역시 이미지 게시나
Argo 배포 완료를 의미하지 않는다. 기존 배포 상태와 HTTP 검증이 서비스 접속 링크를 결정한다.
재시도는 새 `attempt`로 구분하고 이전 시도의 변경·실패 결과를 남긴다.

조회 장애나 heartbeat 지연은 실행 실패로 바꾸지 않는다. 마지막 확인 상태를 유지하고
관측 상태를 `stale` 또는 `unavailable`로 표시한다. 경과 시간도 성공 여부의 근거가 아니다.

## 조회 응답

기존 `GET /api/v1/deployments/{id}/events`의 필드는 유지하고 `agent_activity`를 추가한다.
아래는 가상 데이터다. CI 입력은 서버에서 검증하고 화면은 텍스트로 렌더링한다.

```json
{
  "deployment_id": "deployment-123",
  "agent_activity": {
    "schema_version": 1,
    "id": "repair-1",
    "revision": 4,
    "stage": "build",
    "state": "verifying",
    "attempt": 1,
    "started_at": "2026-10-04T03:00:00Z",
    "updated_at": "2026-10-04T03:00:42Z",
    "finished_at": null,
    "observation": {
      "state": "current",
      "checked_at": "2026-10-04T03:00:43Z"
    },
    "summary": "컨테이너 시작 검사에 실패했습니다.",
    "current_action": "수정한 이미지의 실행 상태를 확인하고 있습니다.",
    "changes": [
      {
        "path": "Dockerfile",
        "summary": "빌드 결과물 경로에 맞게 시작 명령 변경",
        "status": "applied"
      }
    ],
    "verification": [
      { "key": "image.build", "label": "이미지 빌드", "state": "succeeded" },
      { "key": "image.runtime", "label": "컨테이너 실행 검사", "state": "running" }
    ],
    "previous_attempts": [],
    "question": null
  }
}
```

- `agent_activity: null`은 표시할 확인된 활동이 없다는 뜻이다. 조회 실패를 활동 없음으로 바꾸지 않는다.
- `stage`는 기존 UI의 `build/environment/deploy`다. 현재 자동 코드 수정은 `build`에 연결한다.
  환경·배포 오류를 자동으로 AI 작업이라고 표시하지 않는다.
- `id`는 한 배포의 복구 루프를 식별한다. `attempt`는 그 안의 시도 번호,
  정수 `revision`은 서버가 증가시키는 표시 데이터 버전이다. 질문의 문자열 `revision`과 별개다.
- `observation.state`는 `current/stale/unavailable`이다. 기존 수집 신선도 규칙을 재사용한다.
- `changes`에는 `written`으로 확인된 파일만 포함하고 `status`는 `applied`다.
  생성·수정·삭제 구분은 현재 공개하지 않는다. 변경 이유는 AI 설명이며 원인 검증 결과가 아니다.
- `verification.state`는 `not_run/running/succeeded/failed/unknown`이다.
  실행하지 않은 검사는 성공으로 채우지 않는다.
- `previous_attempts`는 최근 3회의 `{attempt, state, summary, changes, verification, finished_at}`다.
  이전 검사 성공을 다음 시도의 성공으로 재사용해 표시하지 않는다.
- 변경 파일은 최대 24개, 경로는 240자, 변경 이유는 비밀값 제거 후 최대 300 bytes,
  이전 시도는 최대 3개, 검증 항목은 최대 6개다. `omitted`에 변경 파일·이전 시도의 생략 개수를 제공한다.
  오류 근거는 기존 diagnostics/단계 로그 영역을 유지하며 새 카드에 원문을 중복 전송하지 않는다.
- 전체 코드·원본 모델 응답·자격증명은 이 응답에 넣지 않는다. 근거와 요약도 비밀값 제거 후
  일반 텍스트로 렌더링한다. 파일 상세는 기존 세션 권한 검사를 거친 소스 조회를 사용한다.

## 전달과 저장

```text
CI의 에이전트 실행·수정 적용·gate 결과
    → 기존 GitHub Checks 진행 정보 전달
    → API가 실행 식별자를 검증하고 기존 작업 저장소에 수집
    → /deployments/{id}/events의 표시용 데이터
    → 대시보드 복구 카드
```

현재 Checks 전달기는 허용된 이벤트·필드만 보낸다. 위 JSON을 그대로 보내는 것이 아니라,
생산자와 수집기의 허용 목록에 `agent.repair`를 추가했다. 적용 직후와 gate 종료 후에 실제 적용 파일과 결과를 발행한다.
모델의 서술은 표시 자료이며 실행 상태를 결정하는 권한을 갖지 않는다.

수집 시 배포 ID, GitHub run/실행 회차, native run/attempt, 소스 식별자를 검증한다.
중복 이벤트는 제거하고 늦게 도착한 이전 시도 이벤트가 현재 상태를 되돌리지 않게 한다.
표시 모델은 기존 실행 기록에서 계산하며 별도의 배포 실행 엔진이나 분류 모델을 추가하지 않는다.
현재 bounded Checks 기록에서 이전 이벤트가 빠져도 수집한 완료 결과는 기존 보존 정책 안에서 유지한다.
아직 수집하지 못한 변경 내역은 누락으로 표시하며 복구 성공을 추정하지 않는다.

상세 화면이 보이고 작업이 진행 중인 경우 서버의 `poll_after_ms`에 따라 조회한다(현재 15초 캐시, 최소 5초). 같은 배포의 중복 요청은 합치고
이전 요청 종료 후 다음 조회를 예약한다. 화면 이동 시 취소하며 Checks 전달 완료를 확인하면 자동 조회를 멈춘다. 불확실한 결과는 수동 새로고침으로 확인한다.
늦은 응답은 배포 ID와 `revision`으로 배제한다. 서버의 기존 조회 캐시/간격 제한을 존중한다.
GET 요청은 모델 호출·수정·재시도를 트리거하지 않는다.

## 사용자 응답: PR #133 형식 유지

자동 처리를 계속할 수 있고 사용자 정보가 필요한 경우에만 `question`을 제공한다.
`recovery.load`는 조회 결과의 질문 또는 `null`을 기존 폼에 전달한다.
예시는 다음과 같으며 운영에서는 서버가 유효한 만료 시각과 버전을 발행한다.

```json
{
  "id": "question-1",
  "deployment_id": "deployment-123",
  "revision": "1",
  "expires_at": "2026-10-04T04:00:00Z",
  "stage": "build",
  "summary": "앱의 시작 명령을 확인할 수 없습니다.",
  "prompt": "앱을 실행하는 명령을 입력해 주세요.",
  "evidence": [
    { "label": "확인 결과", "text": "실행 설정과 문서에서 시작 명령을 확인하지 못했습니다." }
  ],
  "options": [
    {
      "id": "provide_start_command",
      "label": "시작 명령 제공",
      "fields": [
        { "id": "start_command", "label": "시작 명령", "type": "text", "required": true }
      ]
    }
  ]
}
```

기존 `recovery.submit(answer)` 입력도 유지한다.

```json
{
  "deployment_id": "deployment-123",
  "question_id": "question-1",
  "revision": "1",
  "option_id": "provide_start_command",
  "values": { "start_command": "java -jar build/libs/app.jar" }
}
```

서버는 세션 소유권·질문 버전·만료·현재 실행 상태·허용 선택지·필수값을 검사하고
질문/버전별로 중복 접수를 방지한다. 응답은 기존 형태인
`{status: "accepted", question_id: "question-1", revision: "1"}`이다.
`accepted`는 입력 접수이며 복구나 배포 성공이 아니다. 시작 명령은 수정 제안의 참고 정보로
전달하며 API에서 바로 셸 실행하지 않는다. 전송 결과가 불명확하면 조회로 확인한다.

제출 API와 질문 이후 재개 처리는 아직 PR #133에 없다. 조회 전용 카드부터 연결하고,
이 경로를 구현하기 전에는 질문 생성·제출 버튼을 활성화하지 않는다.
서버의 기존 작업 재개 계약을 확인한 후 제출 경로를 확정한다.

## 구현 위치

1. `src/deployment-history.js`: `hasDeploymentIssue`와 별도로 `hasProcessingDetail`에서 복구 활동/이력 여부를 판단한다.
   진행 중 또는 복구 성공 이력이 있으면 상세 진입을 허용한다. 목록 API에는 전체 로그 대신
   `{id, state, attempt, updated_at}`의 `agent_activity_summary`를 추가해 진입 가능 여부를 표시한다.
2. 기존 ‘에이전트 활동 기록’을 카드로 확장하고 성공한 배포에도 ‘자동 처리 1회’ 이력을 남긴다.
   오류 상세 제목은 ‘문제 및 처리 내역’으로 바꾼다. 기존 배포 상태 배지는 유지한다.
3. 열린 카드 영역만 갱신한다. 선택 단계·스크롤·펼친 근거·작성 중인 질문을 매 조회마다 초기화하지 않는다.
   질문 ID/버전이 바뀌면 기존 입력 폐기와 변경 사실을 안내한다.
4. `apps/api/src/agent-activity.js`가 검증된 telemetry를 표시 데이터로 투영한다. 실제 검증 결과를 완료 상태에 연결하고,
   사용자 질문은 제출·재개 경로 구현 후 기존 `src/recovery.js`에 주입한다.

## 확인할 동작

- AI 처리 중, 적용 완료, 재검증, 복구 완료를 실제 기록으로 표시한다.
- 모델 종료 후 빌드 실패인 경우 복구 성공으로 표시하지 않는다.
- 재시도와 이전 시도의 변경·실패 결과를 구분한다.
- 복구 완료 후에도 성공 배포에서 이력을 열 수 있다.
- 조회 장애·오래된 heartbeat·다른 배포 응답·역순 이벤트가 잘못된 성공을 만들지 않는다.
- 새로고침으로 완료 이력이 복원되며 GET 때문에 모델이 호출되지 않는다.
- 질문 만료·버전 변경·중복 접수·다른 세션 제출을 거부한다.
- 원문 로그/모델 출력의 비밀값과 HTML이 그대로 노출되지 않는다.

## 관련 구현

- [대시보드 연동 규격](../../apps/dashboard/README.md#배포-이력-화면과-선택형-응답-프런트엔드-우선-구현)
- [배포 이력 UI](../../apps/dashboard/src/deployment-history.js)
- [선택형 응답 UI](../../apps/dashboard/src/recovery.js)
- [기존 진단·이벤트 API](diagnostics.md)
- [GitHub Checks 전달기](../../ci/scripts/loop/checks_progress.py)
- [에이전트 제안·적용 기록](../../ci/scripts/runner/run_agent.py)

## 반영 순서와 이전 실행 호환성

API·대시보드를 먼저 배포한 뒤 CI가 참조하는 플랫폼 커밋을 승격한다. 기존 API 수집기는
새 이벤트 이름을 거부하므로 CI 생산자만 먼저 승격하지 않는다. 기존 CI에는 변경 파일 이벤트가
없으므로 실행 metadata까지만 표시하고 적용 파일·복구 성공을 추정하지 않는다. 배포 여부와
실제 클라우드 검증은 이 PR의 로컬 테스트와 별개다.
