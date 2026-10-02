# CI 실가동과 담당 작업 인계

2026-10-02 · 실제 CI 검사·구독 AI 수정 통과 · private pull 관리자 인증 대기

## 작업 기준

사용자는 네트워크 안이 다른 채팅에서 정리되는 동안 CI 실가동부터 진행하도록 요청했습니다. 새 플랫폼 저장소는 `Jasmin-Softbank/Railshot`, 앱 업로드 저장소는 기존 `Jasmin-Softbank/railshot-apps`입니다. 네트워크·Ansible/runtime 확장안은 이 작업에서 확정하지 않습니다.

테스트 통합본은 기존 `3cc269035a8d29fa08faae80b69275ccb939c043`을 기준으로 `integration/team-assembly-20261002`에 보존합니다. 지환 담당 코드는 `feature/poc-cloud-jihwan`에서 관리합니다. 새 저장소의 `Agents.md`를 유지하고, 빈 기능 폴더를 추가하지 않습니다.

| 브랜치 | 관리 범위 |
|---|---|
| `feature/poc-cloud-jihwan` | CI/AI 검사·검증 이미지 게시, CSP Terraform, Terraform 지원 도구, CI worker용 Ansible, CI 게시 계약과 진행 기록 |
| `integration/team-assembly-20261002` | 위 담당 코드와 팀 UI/API·OpenStack Provider·guest/runtime·GitOps 연결을 포함한 테스트 통합본 |

개인 feature에는 팀원의 UI/API, OpenStack Controller, 고객 runtime, Argo 연결 코드를 포함하지 않습니다. 팀 소스의 원래 출처와 담당은 통합본의 source-map에 보존합니다. 전체 통합본에서 사용하던 `contracts/`·`examples/`도 이번 업로드에서는 의존 경로 그대로 보존하며, 구조 불일치만으로 재배치하지 않습니다.

## 활성화 전 확인한 원격 상태

- 새 플랫폼 저장소는 public이며 최초 조회 시 `main`에 `Agents.md`와 `README.md`만 있었습니다.
- apps 저장소는 private이며 예전 `railshot-deploy` workflow가 활성화되어 있습니다. 현재 통합본과 달리 개인 render/GitOps/URL 검사 경로를 포함합니다.
- apps의 `PLATFORM_REF`는 예전 `b397bc8b042809e306ef5b6c33603520e03f23f1`을 가리킵니다. repository 변수 이름은 `PLATFORM_REF`, `GITOPS_REPO` 두 개였습니다. repository secret과 environment 목록은 비어 있었습니다.
- repository runner 목록은 0개였습니다. organization runner·secret은 현재 GitHub 권한으로 조회할 수 없어 존재 여부를 확정하지 않습니다.
- 과거 run `36861808186`은 전체 표시가 success지만 release와 gitops job은 skipped였습니다. 이를 이미지 게시나 배포 성공 근거로 사용하지 않습니다.

## 현재 진행 결과

- 새 저장소에 담당 feature와 테스트 통합본을 게시하고 GitHub ref·파일 목록을 다시 읽었습니다. 플랫폼 `main`의 기존 `Agents.md`·`README.md`는 유지합니다.
- apps workflow는 새 저장소의 CI 전용 템플릿으로 교체했습니다. 구문 오류였던 job-level `runner.temp` 참조를 step의 임시 인증 경로 설정으로 고쳤습니다. actionlint와 workflow 검사 7개를 통과했습니다.
- 사용자는 `gpt-5.6-sol`, `xhigh`, Codex 구독 인증과 AWS CI worker 준비를 지시했습니다. 모델·effort를 SDK에 전달하고 실행 metadata로 기록하며 runner 검사 21개를 통과했습니다.
- `railshot-ci-k3s-aws` 전용 VM 한 대(t3.xlarge, 4 vCPU/16 GiB, 암호화 60 GiB)를 서울 리전에 생성했습니다. 외부 inbound는 없으며 SSM으로 접속합니다. 실제 격리 probe, cloud-init 완료, repository runner online을 확인했습니다. 자동 STOP은 2026-10-02 17:12:05 KST이며 디스크는 보존됩니다.
- `k3s-aws`는 CI가 CD에 넘길 운영자 target ID입니다. 고객 K3s 클러스터나 별도 검토 중인 9노드 구성을 생성했다는 뜻이 아닙니다.
- 기존 공개 `node-test-js` fixture를 CI VM에서 native npm lockfile과 함께 준비해 apps의 `demo/fixture-npm-js`에 게시했습니다. 정상 baseline run `36945621450`의 전체 gate가 통과했습니다.
- 별도 `ci/fixture-packaging-repair` 브랜치에서 Dockerfile의 `USER`만 제거했습니다. run `36945951198`은 L1 실패 → 요청한 모델·effort·구독 인증으로 Dockerfile만 수정 → 전체 gate PASS를 기록했습니다. 테스트·manifest·lockfile은 바꾸지 않았습니다.
- 두 초기 run의 release는 다른 Docker image store의 ID 조회에서 실패했습니다. producer와 hosted release에 같은 containerd image store를 명시해 수정했습니다. 이후 run `36946340727`은 정상 gate와 GHCR push가 성공했습니다. 이미지 `ghcr.io/jasmin-softbank/demo-fixture-npm-js-web:706b8207f97b`의 digest는 `sha256:48cf1c0ab9c4251cf60c41256f96a45bf6a9430a1e7e42bdbbf396a26fde5b86`입니다.
- 해당 package는 private이며 조직 정책은 public 전환을 금지합니다. 사용자는 private 유지를 선택했습니다. 익명 조회에서 차단된 run의 API readback은 `failed`, `url: null`, `publication: null`입니다. push 성공만으로 `published`를 주장하지 않습니다.
- private 연결 코드는 별도 읽기 자격의 digest manifest 조회와 v2 인계 계약을 추가합니다. 대상 참조는 `k3s-aws` / `tenant-demo` / `ghcr-pull`로 준비했으며 아직 실제 Secret은 없습니다. release 환경 허용 branch를 `main`, `ci/fixture-packaging-repair`로 제한했습니다.
- 사용자 지시로 관리자 설정을 진행했으나 PAT 발급은 GitHub sudo-mode 본인 인증에서 대기 중입니다. 토큰 등록·실제 private 접근 검증은 아직 완료되지 않았습니다.
- 최초 apps workflow 구문 검사 실패 `36943634074`와 샘플 등록 전 경로 검사 실패 `36945479367`도 보존합니다. 후자는 모델 호출 전 차단됐습니다.

실제 자원 ID·Terraform state·plan·원격 검사 기록은 Git에서 제외한 `.local/ci-k3s-aws-20261002/`에 보관합니다. 인증 파일과 토큰은 저장소·문서에 포함하지 않습니다.

## 코드 변경 규모

CI 엔진 재작성은 필요하지 않습니다. 기존 경로는 앱 source 등록 → baseline/필요한 AI 수정 → 같은 gate → 검증 image bundle → GHCR 게시 → API published 조회입니다.

필수 수정은 workflow의 platform repository 두 참조와 CI VM의 archive 주소를 새 저장소로 바꾸는 것이었습니다. 실제 실행에서 발견한 Actions context·Docker image store 차이도 연결부에서 수정했습니다. private 지원은 CI의 접근 검사·인계 metadata와 API/CD reader에 한정했습니다. gate·bundle·앱 spec을 재설계하지 않았습니다. 요청한 모델·effort 전달과 VM 이름 입력을 추가했고 fixture 실행 안내의 옛 `platform/gate` 경로와 network 이름을 고쳤습니다. `railshot-ci`는 builder 이름이며 품질 검사용 network는 `railshot-quality`입니다. 선택 구현인 CodeBuild는 이번 활성화 대상이 아닙니다.

## 실행 순서와 완료 기준

1. 담당 feature와 테스트 통합본을 새 저장소에 올리고 원격 commit·파일을 다시 읽어 확인합니다.
2. apps workflow를 현재 CI 전용 템플릿으로 갱신합니다. 플랫폼 소스는 새 저장소의 공개된 정확한 commit으로 고정합니다. API도 같은 source commit·target ID를 요청해야 합니다.
3. 전용 Linux CI worker, registry 경로, 모델·인증 방식, target ID를 확정합니다. 기존 `infrastructure/ansible/ci.yml`로 worker 조건을 준비하고 기존 probe를 통과해야 합니다.
4. 준비된 stateless 샘플로 실제 CI를 실행합니다. baseline을 확인하고, AI 시연은 허용된 packaging 변경과 재검사 기록으로 확인합니다.
5. 실제 run/attempt/artifact ID, verdict, 게시 digest를 연결하고 API가 해당 실행을 `published`로 읽는지 확인합니다. 이 단계의 완료는 앱 배포 완료가 아닙니다.

필수 repository 변수는 `PLATFORM_REF`, `RAILSHOT_TARGET_ID`, `RAILSHOT_CI_RUNNER_LABELS`, `RAILSHOT_RUN_ROOT`, `QUALITY_NETWORK`, `AGENT_PROVIDER`, `AGENT_AUTH_MODE`, `REGISTRY_PREFIX`, `REGISTRY_VISIBILITY`입니다. 구독 모드는 별도 관리자 Codex home, API 모드는 해당 provider secret이 필요합니다. 실제 비밀값은 문서와 Git에 기록하지 않습니다.

현재 `REGISTRY_VISIBILITY=private`입니다. release 환경의 pull 자격과 Secret 참조, digest manifest 조회가 모두 통과해야 v2 게시 인계 파일을 만듭니다. 세부 변수와 보안 경계는 [CI 게시 계약](../api/ci-publication.md)을 따릅니다. package 공개 정책은 변경하지 않았습니다.

## 네트워크 안 이후 이어갈 지환 작업

- AWS 실행 환경과 앱 공개 경로의 실제 입력을 확정하고 DNS/TLS/ALB/NodePort를 인수합니다.
- 화균 담당 관리 client와 EIP·WireGuard peer·왕복 경로를 연결합니다.
- 정빈·승민 담당에게 게시 artifact/digest와 준비된 target 정보를 전달해 Argo 적용·실제 앱 응답을 연결합니다.
- 진기 담당과 API의 단일 운영자 target 제약, 게시/적용/공개 URL 상태 표시를 맞춥니다.
- 팀 공유·Notion·Slack 제출은 별도 작업으로 진행합니다.

3노드 cloud cluster와 공통 ALB/원격 target은 다른 채팅에서 검토하는 확장안입니다. 현재 single-node 통합 코드나 실제 배포 완료로 표시하지 않습니다.

## 남은 확인

worker·모델·구독 인증, 정상 gate, AI 수정 후 gate, 동일 검증 이미지의 private GHCR push를 실제 실행으로 확인했습니다. 관리자 본인 인증 후 read-only pull 자격 등록 → 정상/AI 경로 재실행 → private manifest 접근 → API `published` readback을 이어갑니다. 고객 target Secret 설치·노드 pull·CD·공개 URL은 아직 수행하지 않았습니다.
