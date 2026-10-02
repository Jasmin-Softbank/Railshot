# CI 실가동과 담당 작업 인계

2026-10-02 · 정상/AI 수정 CI, private 이미지 게시, 인증된 manifest 조회, API published 확인 완료

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

새 저장소에 개인 feature와 테스트 통합본을 게시하고 원격 ref·파일 목록을 확인했습니다. 플랫폼 `main`은 원래 문서 2개를 유지합니다. 아래 두 실제 실행은 CI source `b77d53b514ef0f6896e5178fab804c679e7b1014`를 사용했습니다.

| 사례 | GitHub run / apps source | 결과 | 인계 artifact ID |
|---|---|---|---|
| 정상 앱 | [36948066574](https://github.com/Jasmin-Softbank/railshot-apps/actions/runs/36948066574) / `e04c620e130abcecd6da86184b5601fdec452a0f` | baseline 전체 gate PASS → release 재시도 2에서 private 게시·인증 조회 → API `published` | 원래 bundle `11202502763`, `published-2` = `11202763783` |
| AI 수정 앱 | [36949207283](https://github.com/Jasmin-Softbank/railshot-apps/actions/runs/36949207283) / `fb1cc2bd43a9947e8e4baf552ded13d2f2855c3c` | L1 실패 → Dockerfile만 수정 → 전체 gate PASS → private 게시·인증 조회 → API `published` | bundle `11202179693`, `published-1` = `11202564427` |

게시 위치는 `ghcr.io/jasmin-softbank/demo-fixture-npm-js-web`입니다. 정상 digest는 `sha256:48cf1c0ab9c4251cf60c41256f96a45bf6a9430a1e7e42bdbbf396a26fde5b86`, AI 수정 digest는 `sha256:8ded0180c728ae5a6d2e359a7313ec1fbf0d1fa1ca4569bdd75e68243333e54d`입니다. 두 artifact의 파일 해시·source·target·registry 계약을 확인했고 API의 `url`은 모두 `null`입니다. 이는 이미지 게시 완료이며 고객 앱 배포 완료는 아닙니다.

- **Worker:** `railshot-ci-k3s-aws` 전용 VM 한 대, 서울 리전 t3.xlarge(4 vCPU/16 GiB), 암호화 60 GiB. 외부 inbound 없이 SSM으로 접속하며 cloud-init·실제 격리 probe·runner online을 확인했습니다. 자동 STOP은 **2026-10-02 17:12:05 KST**, 디스크는 보존됩니다. 고객 K3s나 검토 중인 다중 노드 클러스터를 생성한 것은 아닙니다.
- **AI:** `gpt-5.6-sol` / `xhigh` / 구독 인증을 SDK metadata로 확인했습니다. 최종 AI 실행은 SDK 1회, Dockerfile만 작성, 거부된 변경 없음, 수정 후 전체 gate PASS입니다. 테스트·manifest·lockfile은 바꾸지 않았습니다.
- **Private 자격:** 관리자 본인 인증 후 PAT classic의 `read:packages`만 부여하고 `railshot-release`의 암호화 Secret에 등록했습니다. 만료는 **2026-10-09**이며 release 환경 허용 branch는 `main`, `ci/fixture-packaging-repair`입니다. 비밀값은 Git·문서·artifact에 넣지 않았습니다. 조직 공개 정책은 변경하지 않았습니다.
- **대상 참조:** `k3s-aws` / `tenant-demo` / `ghcr-pull`은 운영자 설정입니다. 실제 대상 Secret 설치·노드 pull은 아직 수행하지 않았습니다. CI가 확인한 것은 별도 읽기 자격의 digest manifest 접근입니다.
- **검사:** private 계약 관련 CI 9개, GitOps 1개, API 19개와 actionlint를 통과했습니다. API는 재실행 시 GitHub가 복제한 job의 조회 회차를 `steps.observed_attempt`로, 실제 게시 생산 회차를 `publication.producer_attempt`로 구분합니다. 원래 bundle ID도 유지합니다.

실패 기록도 보존합니다. `36943634074`는 Actions context 구문 오류, `36945479367`은 모델 호출 전 샘플 경로 차단입니다. 초기 baseline `36945621450`과 AI `36945951198`은 gate가 통과했지만 Docker image store 차이로 push 전에 release가 실패했습니다. store를 맞춘 `36946340727`은 private push 후 익명 조회에서 차단됐습니다. 최종 정상 run의 attempt 1도 pull 자격 누락 시 로그인 전에 차단되고 API `failed / url: null / publication: null`을 반환했습니다. 이들을 최종 성공과 합쳐 세지 않습니다.

자원 ID·Terraform state·plan·실제 artifact와 API 응답은 Git에서 제외한 `.local/ci-k3s-aws-20261002/`에 보관합니다. SDK 관리자 인증은 전용 CI worker의 보호된 경로를 사용하며 인증 파일과 토큰은 저장소·공유 산출물에 포함하지 않습니다.

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

- AWS 실행 환경과 고정 앱 도메인→승인된 target/origin 연결을 확정하고 DNS/TLS/ingress를 인수합니다. ALB·공통 edge·Tunnel은 네트워크 안에서 선택한 경로만 적용합니다.
- 화균 담당 관리 client와 EIP·WireGuard peer·왕복 경로를 연결합니다.
- 정빈·승민 담당에게 게시 artifact/digest와 준비된 target 정보를 전달해 Argo 적용·실제 앱 응답을 연결합니다.
- 진기 담당과 API의 단일 운영자 target 제약, 게시/적용/공개 URL 상태 표시를 맞춥니다.
- 팀 공유·Notion·Slack 제출은 별도 작업으로 진행합니다.

다중 노드 cloud cluster와 공개 앱 경로는 다른 채팅에서 검토 중입니다. WireGuard 관리 경로와 앱 HTTP 경로의 인수를 구분하며 검토안을 실제 배포 완료로 표시하지 않습니다.

## 남은 확인

CI 실가동과 private 게시·조회까지 완료했습니다. 네트워크 안 확정 후 고객 target 준비 → 같은 namespace에 pull Secret 설치 → 대상 노드의 실제 digest pull → CD 적용·Ready → 고정 앱 도메인의 HTTP를 확인합니다. 현재 fixture route는 `/health`이며 기존 검토용 CD CLI는 `/`만 지원하므로 CD 인수 시 이 입력도 맞춰야 합니다. 자격 만료 전 GitHub 환경과 실제 대상 Secret을 함께 갱신합니다.
