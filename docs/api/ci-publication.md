# CI 이미지 게시 → API 조회 → CD 인계

이 문서는 CI에서 게시한 이미지를 API가 확인하고 CD에 인계하는 방법을 설명합니다. `ci/workflows/railshot-deploy.yml`은 private apps repository에 설치하는 workflow 소스입니다. 운영자는 `PLATFORM_REF`를 사용할 통합 source commit으로 고정하고 CI runner, auth mode, registry와 `RAILSHOT_TARGET_ID`를 설정합니다. 이 통합 저장소에서는 workflow를 자동 활성화하지 않습니다.

## 입력과 게시 파일

`workflow_dispatch`는 `tenant`, `app`, `source_commit`, `target_id`를 입력받습니다. `source_commit`은 apps repository에 등록한 40자 SHA이며 checkout HEAD와 `GITHUB_SHA`에 일치해야 합니다. `target_id`는 workflow 저장소의 운영자 설정과 같아야 합니다. registry, target credential, repair 권한은 업로드 파일에서 선택할 수 없습니다.

loop는 baseline 검사, 허용된 AI 수정, 재검사를 수행하고 bundle artifact ID를 출력합니다. release는 해당 artifact ID로 검증 이미지를 받아 재빌드 없이 게시합니다. private GHCR는 target의 pull 권한이 연결되기 전까지 차단합니다. public GHCR는 각 digest를 익명으로 조회할 수 있어야 합니다.

`published-N` ZIP은 **release producer attempt N**의 결과입니다. 아래 다섯 파일을 ZIP 최상위에 포함하며, 동명의 stable alias는 만들지 않습니다.

- `images.json`: service 이름 → registry `@sha256:` digest
- `jasmin.yaml`: gate가 검사한 앱 spec bytes
- `verdict.json`: full gate 판정·source digest·local image IDs
- `manifest.json`: 원래 tested bundle manifest와 image archive hash. 게시 ZIP에는 `images.tar`를 포함하지 않음
- `handoff.json`: version 1, status published, source_commit, target_id, tenant, app, run_id, producer_attempt, bundle_artifact_id, 위 네 파일의 SHA256

`ci/scripts/publication.py`는 첫 네 파일의 bytes를 그대로 복사합니다. 이미지 빌드와 registry 접근은 수행하지 않습니다. handoff 해시는 신뢰된 workflow artifact 채널에서 파일의 무결성을 확인하는 값이며, 별도 서명은 아닙니다. release의 job output `published_id`는 업로드가 끝난 뒤 정해지는 artifact ID입니다. API는 해당 producer attempt의 유일한 artifact ID를 GitHub에서 다시 읽어 사용합니다.

## 소비 경계

API는 GitHub run/attempt/jobs와 artifact metadata를 읽고 고유 artifact ID의 ZIP을 다운로드합니다. 최신 실제 release producer와 run SHA를 결합한 뒤 handoff, 파일 해시, manifest/verdict/service의 일치를 확인합니다. 검사가 통과하면 `published` 상태를 반환합니다. 실패한 job을 재실행하면서 이전 producer 결과를 사용하는 경우에는 해당 결과의 producer attempt를 유지합니다.

CD 담당자는 [인계 CLI](../../gitops/README.md)에 게시 디렉터리의 images/spec/verdict/manifest와 운영자가 선택한 target·destination·GitOps 설정을 전달합니다. `handoff.json`은 출처를 추가로 확인하는 metadata입니다. CLI는 검토용 Deployment/Service/Argo Application 파일을 생성합니다. API는 이 CLI를 자동 호출하지 않으며, `published`는 이미지 게시가 확인되었다는 뜻입니다.

OpenStack의 202 접수, Ansible의 `guest_ready`/`runtime_ready`, CI의 `published`, CD manifest 준비, Git push/Argo 적용, workload Ready, 외부 HTTP는 각각 확인합니다. 남은 연결과 담당은 [인수 항목](../integration/validation.md#남은-인수-경계)에 정리했습니다. Provider credential과 admin kubeconfig는 publication에 포함하지 않습니다.

GitHub 조회 형식은 공식 [workflow jobs API](https://docs.github.com/en/rest/actions/workflow-jobs#list-jobs-for-a-workflow-run-attempt)와 [artifact API](https://docs.github.com/en/rest/actions/artifacts#list-workflow-run-artifacts)를 기준으로 합니다. 이번 검증은 로컬 mock API와 실제 receipt writer의 연결 검사입니다. native GitHub 실행과 게시 확인은 [CI 운영 인수 항목](../integration/validation.md#남은-인수-경계)에 포함합니다.
