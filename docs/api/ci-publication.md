# CI 이미지 게시 → API 조회 → CD 인계

이 문서는 CI에서 게시한 이미지를 API가 확인하고 CD에 인계하는 방법을 설명합니다. `ci/workflows/railshot-deploy.yml`은 private apps repository에 설치하는 workflow 소스입니다. 운영자는 `PLATFORM_REF`를 사용할 통합 source commit으로 고정하고 CI runner, auth mode, registry와 `RAILSHOT_TARGET_ID`를 설정합니다. 이 통합 저장소에서는 workflow를 자동 활성화하지 않습니다.

## 입력과 게시 파일

`workflow_dispatch`는 `tenant`, `app`, `source_commit`, `target_id`를 입력받습니다. `source_commit`은 apps repository에 등록한 40자 SHA이며 checkout HEAD와 `GITHUB_SHA`에 일치해야 합니다. `target_id`는 workflow 저장소의 운영자 설정과 같아야 합니다. registry, target credential, repair 권한은 업로드 파일에서 선택할 수 없습니다.

loop는 baseline 검사, 허용된 AI 수정, 재검사를 수행하고 bundle artifact ID를 출력합니다. release는 해당 artifact ID로 검증 이미지를 받아 재빌드 없이 게시합니다. private GHCR는 별도 읽기 자격으로 모든 게시 digest의 manifest 조회가 성공해야 합니다. public GHCR는 같은 digest를 익명으로 조회합니다. target Secret 설치나 대상 노드 pull은 이 검사에 포함하지 않습니다.

`published-N` ZIP은 **release producer attempt N**의 결과입니다. 아래 다섯 파일을 ZIP 최상위에 포함하며, 동명의 stable alias는 만들지 않습니다.

- `images.json`: service 이름 → registry `@sha256:` digest
- `jasmin.yaml`: gate가 검사한 앱 spec bytes
- `verdict.json`: full gate 판정·source digest·local image IDs
- `manifest.json`: 원래 tested bundle manifest와 image archive hash. 게시 ZIP에는 `images.tar`를 포함하지 않음
- `handoff.json`: version 2, status published, source_commit, target_id, tenant, app, run_id, producer_attempt, bundle_artifact_id, 위 네 파일의 SHA256와 registry 접근 계약

`ci/scripts/publication.py`는 게시 digest의 manifest 접근을 확인한 뒤 첫 네 파일의 bytes를 그대로 복사합니다. 이미지는 재빌드하지 않습니다. handoff 해시는 신뢰된 workflow artifact 채널에서 파일의 무결성을 확인하는 값이며, 별도 서명은 아닙니다. release의 job output `published_id`는 업로드가 끝난 뒤 정해지는 artifact ID입니다. API는 해당 producer attempt의 유일한 artifact ID를 GitHub에서 다시 읽어 사용합니다.

## Private pull 연결

`railshot-release` 환경에는 secret `GHCR_PULL_TOKEN`과 변수 `GHCR_PULL_USERNAME`, `RAILSHOT_PULL_NAMESPACE`, `RAILSHOT_PULL_SECRET`을 설정합니다. 토큰은 패키지 읽기 권한이 있는 PAT classic(`read:packages`)을 사용하며, 업로드 권한을 가진 CI `GITHUB_TOKEN`과 분리합니다. 이 환경을 사용할 branch도 운영자가 제한합니다. 자격이 없거나 digest 조회가 실패하면 `published` artifact를 만들지 않습니다. READY 플래그는 없습니다.

검사는 push 자격을 상속하지 않는 임시 Docker config(디렉터리 0700, 파일 0600)를 사용하고 종료 시 제거합니다. 비밀값을 명령 인자·로그·Git·artifact에 넣지 않습니다. 토큰 발급 시 만료를 지정하고, 만료 전 GitHub 환경과 실제 대상 Secret을 함께 갱신해야 합니다. target Secret은 workload와 같은 namespace의 `kubernetes.io/dockerconfigjson`으로 운영자가 설치하며, 이 CI가 대신 설치하지 않습니다.

`handoff.json.registry`는 다음 네 필드만 허용합니다.

| 필드 | Private | Public |
|---|---|---|
| `visibility` | `private` | `public` |
| `verification` | `authenticated_manifest_read` | `anonymous_manifest_read` |
| `images_sha256` | `files["images.json"]`와 같은 해시 | 동일 |
| `image_pull_secret` | `{ "namespace": "<대상>", "name": "<Secret 이름>" }` | `null` |

Secret 참조는 설치할 위치와 이름입니다. API와 CD는 v2를 검사하고 알 수 없는 registry/Secret 필드와 잘못된 해시를 거부합니다. CD는 운영자 target의 `image_pull_secret`이 receipt와 일치하고 namespace도 target과 같을 때만 Deployment에 `imagePullSecrets`를 반영합니다. v1 소비자는 v2를 거부하므로 writer·API·CD를 같은 플랫폼 commit으로 갱신합니다.

공식 계약은 [GHCR 인증](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry)과 [Kubernetes private image pull](https://kubernetes.io/docs/tasks/configure-pod-container/pull-image-private-registry/)을 따릅니다. manifest 조회 성공은 전체 layer 다운로드·대상 Secret 설치·Pod 실행의 증거가 아닙니다.

## 소비 경계

API는 GitHub run/attempt/jobs와 artifact metadata를 읽고 고유 artifact ID의 ZIP을 다운로드합니다. 최신 실제 release producer와 run SHA를 결합한 뒤 handoff, 파일 해시, manifest/verdict/service의 일치를 확인합니다. 검사가 통과하면 `published` 상태를 반환합니다. 실패한 job을 재실행하면서 이전 producer 결과를 사용하는 경우에는 해당 결과의 producer attempt를 유지합니다.

CD 담당자는 [인계 CLI](https://github.com/Jasmin-Softbank/Railshot/blob/integration/team-assembly-20261002/gitops/README.md)에 게시 디렉터리의 images/spec/verdict/manifest와 운영자가 선택한 target·destination·GitOps 설정을 전달합니다. `handoff.json`은 출처를 추가로 확인하는 metadata입니다. CLI는 검토용 Deployment/Service/Argo Application 파일을 생성합니다. API는 이 CLI를 자동 호출하지 않으며, `published`는 이미지 게시가 확인되었다는 뜻입니다.

OpenStack의 202 접수, Ansible의 `guest_ready`/`runtime_ready`, CI의 `published`, CD manifest 준비, Git push/Argo 적용, workload Ready, 외부 HTTP는 각각 확인합니다. 남은 연결과 담당은 [인수 항목](https://github.com/Jasmin-Softbank/Railshot/blob/integration/team-assembly-20261002/docs/integration/validation.md#남은-인수-경계)에 정리했습니다. Provider credential과 admin kubeconfig는 publication에 포함하지 않습니다.

GitHub 조회 형식은 공식 [workflow jobs API](https://docs.github.com/en/rest/actions/workflow-jobs#list-jobs-for-a-workflow-run-attempt)와 [artifact API](https://docs.github.com/en/rest/actions/artifacts#list-workflow-run-artifacts)를 기준으로 합니다. 로컬 검사는 mock API와 실제 receipt writer를 연결하며 registry 호출만 대체합니다. 실제 GitHub 실행·registry 접근·API readback 기록은 [CI 운영 인수 항목](https://github.com/Jasmin-Softbank/Railshot/blob/integration/team-assembly-20261002/docs/integration/validation.md#남은-인수-경계)에 포함합니다.
