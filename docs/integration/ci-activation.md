# CI 실가동과 담당 작업 인계

2026-10-02 · 진행 중 · 실제 CI/registry 실행 전

## 작업 기준

사용자는 네트워크 안이 다른 채팅에서 정리되는 동안 CI 실가동부터 진행하도록 요청했습니다. 새 플랫폼 저장소는 `Jasmin-Softbank/Railshot`, 앱 업로드 저장소는 기존 `Jasmin-Softbank/railshot-apps`입니다. 네트워크·Ansible/runtime 확장안은 이 작업에서 확정하지 않습니다.

테스트 통합본은 기존 `3cc269035a8d29fa08faae80b69275ccb939c043`을 기준으로 `integration/team-assembly-20261002`에 보존합니다. 지환 담당 코드는 `feature/poc-cloud-jihwan`에서 관리합니다. 새 저장소의 `Agents.md`를 유지하고, 빈 기능 폴더를 추가하지 않습니다.

| 브랜치 | 관리 범위 |
|---|---|
| `feature/poc-cloud-jihwan` | CI/AI 검사·검증 이미지 게시, CSP Terraform, Terraform 지원 도구, CI worker용 Ansible, CI 게시 계약과 진행 기록 |
| `integration/team-assembly-20261002` | 위 담당 코드와 팀 UI/API·OpenStack Provider·guest/runtime·GitOps 연결을 포함한 테스트 통합본 |

개인 feature에는 팀원의 UI/API, OpenStack Controller, 고객 runtime, Argo 연결 코드를 포함하지 않습니다. 팀 소스의 원래 출처와 담당은 통합본의 source-map에 보존합니다. 전체 통합본에서 사용하던 `contracts/`·`examples/`도 이번 업로드에서는 의존 경로 그대로 보존하며, 구조 불일치만으로 재배치하지 않습니다.

## 직접 확인한 원격 상태

- 새 플랫폼 저장소는 public이며 최초 조회 시 `main`에 `Agents.md`와 `README.md`만 있었습니다.
- apps 저장소는 private이며 예전 `railshot-deploy` workflow가 활성화되어 있습니다. 현재 통합본과 달리 개인 render/GitOps/URL 검사 경로를 포함합니다.
- apps의 `PLATFORM_REF`는 예전 `b397bc8b042809e306ef5b6c33603520e03f23f1`을 가리킵니다. repository 변수 이름은 `PLATFORM_REF`, `GITOPS_REPO` 두 개였습니다. repository secret과 environment 목록은 비어 있었습니다.
- repository runner 목록은 0개였습니다. organization runner·secret은 현재 GitHub 권한으로 조회할 수 없어 존재 여부를 확정하지 않습니다.
- 과거 run `36861808186`은 전체 표시가 success지만 release와 gitops job은 skipped였습니다. 이를 이미지 게시나 배포 성공 근거로 사용하지 않습니다.

## 코드 변경 규모

CI 엔진 재작성은 필요하지 않습니다. 기존 경로는 앱 source 등록 → baseline/필요한 AI 수정 → 같은 gate → 검증 image bundle → GHCR 게시 → API published 조회입니다.

필수 수정은 workflow의 platform repository 두 참조를 새 저장소로 바꾸는 것입니다. fixture 실행 안내의 옛 `platform/gate` 경로와 network 이름도 고칩니다. `railshot-ci`는 builder 이름이며 품질 검사용 network는 `railshot-quality`입니다. 선택 구현인 CodeBuild는 이번 활성화 대상이 아닙니다.

## 실행 순서와 완료 기준

1. 담당 feature와 테스트 통합본을 새 저장소에 올리고 원격 commit·파일을 다시 읽어 확인합니다.
2. apps workflow를 현재 CI 전용 템플릿으로 갱신합니다. 플랫폼 소스는 새 저장소의 공개된 정확한 commit으로 고정합니다. API도 같은 source commit·target ID를 요청해야 합니다.
3. 전용 Linux CI worker, registry 경로, 모델·인증 방식, target ID를 확정합니다. 기존 `infrastructure/ansible/ci.yml`로 worker 조건을 준비하고 기존 probe를 통과해야 합니다.
4. 준비된 stateless 샘플로 실제 CI를 실행합니다. baseline을 확인하고, AI 시연은 허용된 packaging 변경과 재검사 기록으로 확인합니다.
5. 실제 run/attempt/artifact ID, verdict, 게시 digest를 연결하고 API가 해당 실행을 `published`로 읽는지 확인합니다. 이 단계의 완료는 앱 배포 완료가 아닙니다.

필수 repository 변수는 `PLATFORM_REF`, `RAILSHOT_TARGET_ID`, `RAILSHOT_CI_RUNNER_LABELS`, `RAILSHOT_RUN_ROOT`, `QUALITY_NETWORK`, `AGENT_PROVIDER`, `AGENT_AUTH_MODE`, `REGISTRY_PREFIX`, `REGISTRY_VISIBILITY`입니다. 구독 모드는 별도 관리자 Codex home, API 모드는 해당 provider secret이 필요합니다. 실제 비밀값은 문서와 Git에 기록하지 않습니다.

현재 publisher는 private target pull 자격이 연결되지 않아 private 게시를 차단합니다. public 모드는 이미 public인 package를 전제로 익명 digest 조회를 검사하며 package 공개 전환은 하지 않습니다. 실제 게시 전에 시연 package와 권한을 확인합니다.

## 네트워크 안 이후 이어갈 지환 작업

- AWS 실행 환경과 앱 공개 경로의 실제 입력을 확정하고 DNS/TLS/ALB/NodePort를 인수합니다.
- 화균 담당 관리 client와 EIP·WireGuard peer·왕복 경로를 연결합니다.
- 정빈·승민 담당에게 게시 artifact/digest와 준비된 target 정보를 전달해 Argo 적용·실제 앱 응답을 연결합니다.
- 진기 담당과 API의 단일 운영자 target 제약, 게시/적용/공개 URL 상태 표시를 맞춥니다.
- 팀 공유·Notion·Slack 제출은 별도 작업으로 진행합니다.

3노드 cloud cluster와 공통 ALB/원격 target은 다른 채팅에서 검토하는 확장안입니다. 현재 single-node 통합 코드나 실제 배포 완료로 표시하지 않습니다.

## 현재 필요한 입력

전용 CI Linux worker의 대상과 접속 방식, 사용할 모델·인증 방식, 승인된 시연 registry package가 필요합니다. repository 수준에서는 실행 가능한 worker를 확인하지 못했으므로 이 정보를 확정하기 전 실제 workload/model/registry run은 시작하지 않습니다. 네트워크 안이 확정되기 전 임의 cloud resource를 만들거나 개인 노트북을 CI worker로 등록하지 않습니다.
