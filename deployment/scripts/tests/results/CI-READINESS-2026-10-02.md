# Deployment Runtime CI 점검 — 2026-10-02

실제 GitHub Actions(자동 검사 서비스)에서 Railshot CI와 Control Cilium 두 workflow(자동 검사 절차)가 성공했습니다. 로컬 검사는 **610 PASS / 0 FAIL / 0 SKIP**입니다. 발견된 Runtime(배포 실행 계층) 코드 결함이나 workflow 결함이 없어 기능 코드·검증 조건을 변경하지 않았습니다.

## A. 브랜치 및 검사 기준

- 저장소: `Jasmin-Softbank/Railshot`
- 브랜치: `feature/deployment-runtime-seungmin`
- 최초 HEAD: `5f893ff2cf991561698524fe0d76cabcd2c07323`
- 기존 미커밋 계약 검사·데모 증거 보존: `72c16dce1775aa682ad90f6f6615b0eb1c54063f`
- integration(팀 통합 브랜치) 반영 기준: `94808d26e36f84bc8e65f7b7e3d3bb085634e334`
- 일반 merge(이력 보존 병합) 및 실제 첫 CI 검사 commit: `003fc3ba0f84b4179ca697dfa31d4de5bc844b4d`

작업 시작 시 저장소 root, origin 주소, 현재 브랜치를 확인했습니다. 당시 개인 브랜치는 integration과 각각 5개/85개 고유 커밋 차이가 있었습니다. 먼저 기존 deployment 검사·증거만 커밋한 뒤 integration을 개인 feature 브랜치에 일반 병합했습니다. 충돌은 없었습니다. 팀 변경은 원본 이력 그대로 반영했으며 다른 담당자의 코드를 직접 편집하지 않았습니다.

## B. workflow 및 실행 명령

| workflow | 실제 역할 | 이번 실행 |
| --- | --- | --- |
| `.github/workflows/railshot-ci.yml` | 경로별 검사 선택, Python 계약, Linux Runtime 설치·verify·cleanup, API·브라우저, Ansible, Terraform validation, OpenStack, Observability, 컨테이너 검사, 최종 gate(통과 판정) | feature 브랜치를 지정해 수동 실행; **13개 job 모두 PASS** |
| `.github/workflows/control-cilium.yml` | 폐기 가능한 Linux 서버에서 운영용 Cilium/Argo 설치를 두 번 실행하고 실제 통신·정책·제거 검사 | feature 브랜치를 지정해 수동 실행; **1개 job PASS** |
| `.github/workflows/platform-containers.yml` | dashboard/api/mcp/ci-runner 이미지를 실제 빌드하고 실행 검사 | Railshot CI의 재사용 workflow로 4개 모두 PASS |
| `.github/workflows/platform-publish.yml` | 신뢰한 main/integration에서 이미지 게시와 실제 플랫폼 배포 | 실행하지 않았습니다. 개인 feature에서 게시·플랫폼 배포를 수행하지 않습니다. |

Railshot CI의 push(원격 업로드) trigger(시작 조건)는 main/integration이며 feature에서는 PR(변경 검토 요청) 또는 workflow_dispatch(수동 실행)를 사용합니다. 이를 결함으로 취급하거나 trigger를 임의 변경하지 않았습니다.

`ci/scripts/ci_scope.py`와 경로 선택 회귀 검사를 확인했습니다. `deployment/scripts/engine.py`, `deployment/airgap/scripts/bundle.py`, `deployment/cilium/install.sh`, `deployment/scripts/tests/test_engine.py` 변경이 contracts/runtime-smoke를 선택하는 것을 직접 확인했습니다. Cilium 변경은 필요한 API·컨테이너 검사도 선택합니다. 최종 gate는 선택된 검사 실패나 예기치 않은 SKIP을 거부합니다.

주요 실제 CI 명령은 다음과 같습니다.

```sh
kubectl kustomize gitops/argo > /tmp/railshot-ci-argocd-schema.yaml
python -m unittest discover -s deployment/scripts/tests -p 'test_*.py' -v
bash ci/scripts/check-ansible.sh
npm test --prefix apps/api --workspaces=false
npm run build
```

Python 계약 job의 10개 suite 전체도 CI와 같은 순서·명령 형식으로 로컬에서 실행했습니다. 로컬에서는 기존 `/tmp/railshot-runtime-validation` 환경에 저장소의 고정된 test/dev 의존성을 설치했고 Node 22.22.2를 사용했습니다. GitHub에서는 workflow에 정의된 Python 3.13/Node 22를 사용했습니다. Cloud API(클라우드 관리 호출)를 실행하는 테스트로 바꾸지 않았습니다.

## C. PASS / FAIL / SKIP

| 로컬 검사 | PASS | FAIL | SKIP |
| --- | ---: | ---: | ---: |
| `ci/scripts` | 56 | 0 | 0 |
| `ci/scripts/gate` | 134 | 0 | 0 |
| `ci/scripts/loop` | 31 | 0 | 0 |
| `ci/scripts/runner` | 50 | 0 | 0 |
| `ci/scripts/poc` | 1 | 0 | 0 |
| `ci/scripts/infra` | 18 | 0 | 0 |
| `infrastructure/ansible` | 65 | 0 | 0 |
| `deployment/scripts/tests` | 75 | 0 | 0 |
| `gitops` | 26 | 0 | 0 |
| `infrastructure/providers/terraform_tools` | 63 | 0 | 0 |
| 전체 API Node tests(단위 검사) | 73 | 0 | 0 |
| `ci/tests` pytest(템플릿·사전조건 검사) | 18 | 0 | 0 |
| **합계** | **610** | **0** | **0** |

NativeBoundaries(실제 도구를 사용하는 경계 검사) 4개는 Ansible 전체 65개에 포함되므로 별도 재실행 결과를 중복 합산하지 않았습니다. 최초 실행 시 Argo CRD(확장 자원 정의)와 native Ansible 도구 부재로 생략됐던 항목은 실제 도구·고정 Argo schema(자원 규약)를 준비한 뒤 전부 다시 실행해 통과했습니다.

추가로 shell(명령 스크립트) 문법 20개, YAML 구문 9개, OpenAPI(API 명세) 2개 검증, Ansible inventory/syntax/template rendering/lint(설정·문법·템플릿·정적 검사), Dashboard 패키지 빌드를 통과했습니다. Compose(컨테이너 실행 정의) 설정 검사도 통과했습니다. 이 항목은 위 610개 테스트 수에 합산하지 않았습니다.

첫 실제 GitHub 검사:

- [Railshot CI 37015888888](https://github.com/Jasmin-Softbank/Railshot/actions/runs/37015888888): **13 PASS / 0 FAIL / 0 SKIP**.
- [Control Cilium 37015893193](https://github.com/Jasmin-Softbank/Railshot/actions/runs/37015893193): **1 PASS / 0 FAIL / 0 SKIP**.

GitHub Runtime artifact(실행 증거 파일)의 실제 결과는 deploy/verify가 `ready`, K3s가 `ready`, Cilium이 `healthy`, workload(앱)가 `ready`이며 error는 null입니다. cleanup(제거)은 `cleaned`, 클러스터·Cilium·앱은 `absent`입니다. **provider=aws fixture(모의 입력)를 사용한 GitHub Linux amd64 검사이며 실제 AWS 자원 배포를 뜻하지 않습니다.**

## D–E. 변경 파일과 이유

이번 직접 변경은 `deployment/` 아래 검사·기록으로 제한했습니다.

1. 기존 `fixtures/dashboard-handoff.json`, `test_dashboard_handoff.py`, Dashboard 계약·데모 보고서와 증거 24개를 커밋했습니다. 기존 작업을 보존하고 제품 payload(요청 데이터)가 완성된 Runtime 명세 없이 로컬 배포를 실행하지 못하는 경계를 검사합니다.
2. 이 보고서와 `ci-readiness/`에 이번 로컬 로그, 구문 검사 결과, GitHub job 결과, 실제 Linux Runtime deploy/verify/cleanup 증거를 추가했습니다.
3. 허용된 일반 병합으로 integration의 팀 변경을 개인 feature 브랜치에 반영했습니다. 병합으로 들어온 Dashboard/DB/Provider/Observability 변경을 직접 재작성하거나 수정하지 않았습니다.

Runtime 기능 코드·workflow를 새로 수정할 필요는 없었습니다. assertion(검증 조건) 약화, 실패 무시, skip 추가, 테스트 삭제 또는 비활성화를 하지 않았습니다.

## F. 실패 원인 분류

| 분류 | 결과 |
| --- | --- |
| A. 개인 Runtime 코드 문제 | 관측되지 않았습니다. |
| B. integration 계약 불일치 | 관측되지 않았습니다. 개인 JSON 계약과 실제 통합 경로를 구분한 채 검사했습니다. |
| C. 다른 담당자 코드 문제 | 이번 실행에서 실패가 관측되지 않았습니다. 해당 코드는 직접 수정하지 않았습니다. |
| D. CI workflow 문제 | 관측되지 않았습니다. feature push의 자동 실행 제외는 현재 정책이며 수동 실행으로 검증했습니다. |
| E. 실제 cloud 인증·자원 필요 | 신규 실제 cloud smoke(짧은 배포 확인)는 이번에 실행하지 않았습니다. Runtime CI는 cloud 인증 없이 성공했습니다. |
| F. 로컬 환경 문제 | 로컬 Docker daemon(컨테이너 실행 서비스)이 꺼져 이미지 빌드는 로컬에서 수행하지 못했지만 같은 브랜치의 GitHub 컨테이너 4개 job이 모두 통과했습니다. native 의존성·Argo schema 부재는 준비 후 해소했습니다. |

현재 feature 브랜치에서 확인한 과거 Railshot CI `36980718934`도 성공 상태였습니다. 이번 두 workflow 역시 성공하여 분석할 실패 job/step 로그는 없습니다. 성공 여부는 실제 Actions job 결과 및 Runtime artifact를 근거로 기록했습니다.

## G. cloud 때문에 남은 범위

실제 AWS/GCP/OpenStack에서 신규 install/reuse/update/cleanup은 실행하지 않았습니다. 기존 앱·클러스터를 시험 대상으로 임의 사용하지 않았으며 cloud resource, IAM(권한), firewall(방화벽), VPC(사설 네트워크), LB(외부 요청 분배 장치)를 변경하지 않았습니다. 필요 시 전용 시험 대상·접속 권한·namespace(앱 격리 공간)·포트 소유권을 별도로 확정해야 합니다.

이 항목은 **실제 cloud 검증의 미실행 범위**입니다. 이번 GitHub Runtime job을 credential 문제로 BLOCKED(전제조건 미충족) 처리할 이유는 없으며 모두 성공했습니다.

## H. integration 병합 준비

`94808d2` 기준으로 일반 병합과 Runtime 회귀 검사가 완료됐습니다. provider-independent DeploymentSpec(환경에 종속되지 않는 실행 명세), aws/gcp/openstack 입력 정규화, K3s/Cilium, 앱·Service/NodePort(노드의 앱 접속 포트), health(정상 응답), cleanup 소유권, online/offline/airgap, 선택적 exposure(외부 공개)의 실패 처리 검사가 통과했습니다. GitHub 실제 Linux에서는 install/deploy/verify/full cleanup을, Control Cilium에서는 반복 설치·실제 통신·정책을 확인했습니다. 이번 CI에서 실제 Linux airgap 전체 시나리오를 새로 실행한 것은 아닙니다.

통합 담당자가 검토할 수 있는 상태입니다. main/develop/integration/다른 팀원 브랜치에 직접 commit/push/merge하지 않았습니다. PR merge, rebase(이력 재작성), force push를 수행하지 않았습니다.

## I. 통합 담당자에게 전달할 메시지

> 승민 Deployment 브랜치에 integration `94808d2`를 일반 병합했습니다. 로컬 610개 검사와 첫 GitHub Railshot CI 13개 job, Control Cilium 1개 job이 모두 통과했습니다. Runtime 설치·HTTP 확인·제거 및 Cilium 반복 설치·정책을 실제 GitHub Linux에서 확인했습니다. 기능 코드나 workflow 조건을 완화하지 않았고 다른 담당자 코드는 직접 수정하지 않았습니다. 실제 cloud 신규 배포는 별도 시험 대상이 필요한 범위입니다. 증거 커밋과 최종 feature 브랜치 CI 상태를 검토한 뒤 팀의 통합 절차로 반영해 주세요.

전달용 초안이며 다른 팀원에게 실제 전송하지 않았습니다.

## J. 최종 커밋 및 증거

이 보고서는 위에 명시한 실제 검사 commit을 기준으로 합니다. 이후 추가되는 파일은 검사 증거와 문서입니다. 최종 증거 commit SHA(정확한 버전 식별값)와 같은 commit에 대한 재실행 CI URL은 작업 완료 응답에 기록합니다.

[구조화 요약](ci-readiness/summary.json), [Railshot CI job 결과](ci-readiness/railshot-ci.json), [Control Cilium 결과](ci-readiness/control-cilium.json), [실제 Runtime deploy](ci-readiness/github-runtime/deploy.json), [verify](ci-readiness/github-runtime/verify.json), [cleanup](ci-readiness/github-runtime/cleanup.json)을 보존했습니다. 원본 로컬 검사 로그와 GitHub Runtime 로그는 같은 증거 폴더에 있습니다.
