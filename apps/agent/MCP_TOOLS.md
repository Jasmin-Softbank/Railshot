# RailShot MCP 도구 사용 가이드

RailShot MCP는 AI가 제품 API의 배포 기능을 호출하는 진입점입니다.

## 도구 목록

| 도구 | 필수 입력 | 제품 API | 동작 |
| --- | --- | --- | --- |
| `list_options` | 없음 (`{}`) | `GET /api/v1/options` | 사용 가능한 환경 선택지와 미지원 사유 조회 |
| `list_targets` | 없음 (`{}`) | `GET /api/v1/targets` | 등록 대상과 CI 제출·앱 배포 가능 여부 조회 |
| `get_deployment` | `deployment_id` | `GET /api/v1/deployments/{id}` | 배포 단계, 상태, 오류, 검증된 URL 조회 |
| `get_build` | `build_id` | `GET /api/v1/builds/{id}` | 등록된 CI 빌드와 이미지 게시 상태 조회 |
| `deploy_repository` | `repository_url`, `app`, `target_id`, `idempotency_key` | `POST /api/v1/deployments` | 공개 GitHub 저장소의 배포 요청 접수 |
| `prepare_file_deployment` (원격 전용) | `app`, `target_id`, `idempotency_key` | 업로드 페이지에서 `POST /api/v1/deployments` | 로컬 ZIP·개별 파일·폴더 업로드 링크 발급. 페이지에서 파일을 제출해야 배포 요청 접수 |
| `get_file_upload` (원격 전용) | `upload_id` | 없음 | 파일 제출 여부와 접수된 `resource_id` 조회 |

### `list_options`·`list_targets`

```json
{}
```

`list_options`는 AWS·GCP 같은 환경의 선택 가능 여부를 알려주고, `list_targets`는 서버에 실제 등록된 대상들을 반환합니다. 다음은 선택에 필요한 **핵심 필드만 발췌한 예시**입니다.

```json
{
  "items": [
    {
      "id": "demo-target",
      "label": "데모 대상",
      "capabilities": {
        "ci_submission": true,
        "application_deployment": true
      }
    }
  ],
  "next_marker": null
}
```

이 경우 `application_deployment`가 `true`인 항목의 `id`인 `demo-target`을 `deploy_repository.target_id`로 전달합니다.

`label`은 화면에 보여 줄 이름입니다.

### `deploy_repository`

```json
{
  "repository_url": "https://github.com/example-org/example-app",
  "app": "example-app",
  "target_id": "demo-target",
  "idempotency_key": "example-app-deploy-001"
}
```

| 입력 | 제한 |
| --- | --- |
| `repository_url` | 공개 GitHub 저장소의 `https://github.com/소유자/저장소` 기본 URL. |
| `app` | 영문 소문자로 시작하고 영문 소문자·숫자·`-`로 구성된 3~30자. 마지막은 영문 소문자 또는 숫자 |
| `target_id` | `list_targets`에서 확인한 ID. 영문자·숫자·`.`·`_`·`-` 1~128자 |
| `idempotency_key` | 같은 배포 요청을 재시도할 때 유지할 키. 영문자·숫자·`.`·`_`·`-` 1~128자 |

### `prepare_file_deployment` (원격 MCP)

```json
{"app":"example-app","target_id":"demo-target","idempotency_key":"example-file-deploy-001"}
```

새 요청이 접수되면 보통 다음과 같은 값을 반환합니다. 이때, `resource_id`는 접수된 배포 작업의 ID

```json
{
  "resource_id": "00000000-0000-4000-8000-000000000000",
  "action": "create",
  "status": "accepted",
  "request_id": "요청-ID"
}
```

`accepted`는 API가 요청을 받았다는 뜻입니다(CI/CD 완료나 앱 접속 성공을 뜻하지 않습니다). 

### `get_deployment`·`get_build`

`get_deployment`에는 `deploy_repository` 또는 파일 업로드 결과의 `resource_id`를 `deployment_id`로 전달합니다.

```json
{"deployment_id": "00000000-0000-4000-8000-000000000000"}
```

반환된 배포 객체의 `status`, `stage`, `ci`, `error`, `url`을 확인합니다. `queued`·`running`은 진행 중이고, `failed`·`blocked`·`unknown`은 실패나 결과 불확실 상태입니다. `status=succeeded`와 검증된 `url`이 있어야 배포 완료로 안내할 수 있습니다. 자동으로 AI 대화에 상태가 흘러오는 스트리밍 도구는 현재 없으므로 진행 상황은 `get_deployment`를 다시 호출해 조회합니다.

`get_build`에는 등록된 빌드의 ID를 `build_id`로 전달합니다.

```json
{"build_id": "123456789"}
```

빌드의 `published`는 이미지 게시 상태입니다. 앱 배포 완료 판정에는 `get_deployment`를 사용합니다.

## AI를 통한 사용 순서

1. `list_options`와 `list_targets`로 사용 가능한 환경과 실제 대상 ID를 확인합니다.
2. 사용자에게 저장소 URL, 앱 이름, 대상 ID를 확인받고 MCP 호스트에서 `deploy_repository` 실행을 승인합니다.
3. 반환된 `resource_id`를 보관하고 `get_deployment`로 같은 배포를 조회합니다.
4. 최종 상태와 검증된 URL 또는 실패 원인을 사용자에게 전달합니다.

로컬 파일을 배포할 때는 2번에서 `prepare_file_deployment`를 호출하고, 사용자가 반환된 업로드 링크에서 파일을 선택해 제출한 뒤 `get_file_upload`에서 받은 `resource_id`로 3번을 진행합니다.

AI에게는 예를 들어 다음처럼 요청할 수 있습니다.

> https://github.com/example-org/example-app을 example-app으로 배포해 줘. 먼저 대상과 배포 내용을 보여주고 내 확인을 받아. 이후 완료될 때까지 상태를 확인해 줘.
