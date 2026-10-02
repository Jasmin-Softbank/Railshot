# Deployment Runtime 데모 안정화 결과

2026-10-02에 시작하여 10-03 KST에 정리했습니다. 전체 RailShot E2E(처음부터 끝까지의 통합 동작)는 검증하거나 수정하지 않았습니다. [발표 복붙 명령](../../../DEMO-RUNBOOK.md)과 [기계용 측정값](demo-stability/summary.json)을 함께 제공합니다.

## A. 현재 branch / commit

- 저장소: `Jasmin-Softbank/Railshot`
- 작업 브랜치: `feature/deployment-runtime-seungmin`
- 점검 시작 source: `d557045e2a72660be727ff38fc44506627c063e0`
- 읽기 전용 integration 기준: `61d4f98191994cb32c8cc157f4b276d702371825`
- 시작 작업 트리는 clean(미커밋 변경 없음)이었습니다. 이번 변경은 `deployment/`와 Runtime 전용 workflow만 대상으로 합니다. 최종 commit SHA(버전 식별값)는 최종 응답에 기록합니다.

## B. Golden Path(검증된 발표 경로)

준비된 Lima VM `railshot-runtime-test`의 기존 K3s `v1.34.11+k3s1` → 기존 Cilium `1.20.2` → `railshot-demo/railshot-workload` → NodePort `30080` → `/` HTTP 200을 확인했습니다. OS는 Ubuntu 24.04.4 arm64입니다. 맥의 전달 포트 `30082`에서도 HTTP 200과 `Railshot Runtime OK`를 확인했습니다.

실행 nginx imageID `docker.io/library/nginx@sha256:30f1c0d78e0ad60901648be663a710bdadf19e4c10ac6782c235200619158284`를 fixture에 기록했습니다. 기존 Deployment 선언은 `nginx:1.28.0-alpine`이며 이번에 재적용하지 않았습니다. 동일 namespace·이미지·Service·health 경로를 고정한 관찰 경로이며 다른 환경의 설치 성공을 대신하지 않습니다.

## C. Runtime Preflight(발표 직전 점검)

`deployment/scripts/demo_preflight.py`는 기존 JSON adapter를 사용하고 설치·재시작·검사 Pod 생성 없이 조회합니다. [실제 출력](demo-stability/preflight.json)의 11개 확인이 통과했습니다. 점검 시간은 **0.667초**, 기본 전체 제한은 **25초**입니다. 결과 `ready`는 노드 내부 정상이며 public exposure(외부 공개 경로)는 `not_checked`입니다.

## D. 테스트 결과

| 실제 실행 | PASS | FAIL | SKIP | 범위 |
| --- | ---: | ---: | ---: | --- |
| `unittest discover -s deployment/scripts/tests` | 95 | 0 | 0 | 기존 전체 suite + 이번 회귀 검사 |
| Bash 문법 | 20 | 0 | 0 | `deployment/**/*.sh` |
| Python 구문 | 32 | 0 | 0 | `deployment/**/*.py`, AST(코드 문법 구조) 분석 |
| deployment YAML 문법 | 5 | 0 | 0 | `deployment/**/*.yaml` |
| Runtime workflow YAML 문법 | 1 | 0 | 0 | 새 Runtime 전용 workflow |
| 입력 fixture / schema / adapter | 4 | 0 | 0 | AWS/GCP/OpenStack 및 golden fixture |

[95개 테스트 원본 로그](demo-stability/runtime-tests.log)를 보존합니다. provider 입력 정규화, DeploymentSpec, cleanup 소유권, 실패 단계, online/offline 모드 선택, 손상 bundle 거부, 선택 외부 exposure 실패, 기존 Dashboard handoff 계약을 검사했습니다. provider simulation(모의 환경)·airgap 검사는 fixture/mock 계약 검사이며 이번 실행에서 새 VM 설치나 offline 재배포를 하지 않았습니다.

추가 실측 검증은 준비된 Linux VM의 정상 preflight, 없는 health 경로의 HTTP 404 및 exit 1, 기존 Pod/Deployment/Service UID·generation 동일, fallback 명령 HTTP 200, 맥 전달 포트 HTTP 200, 기존 AWS 공개 health HTTP 200입니다. HTTP 404는 의도한 실패 주입을 정상 검출한 결과입니다. [실패 JSON](demo-stability/negative-health.json)과 [fallback 출력](demo-stability/fallback.log)을 제공합니다.

외부 URL HTTP 503과 5초 응답 지연은 로컬 HTTP 서버에 실제 curl을 실행하여 검사했습니다. 지연 요청은 약 3초 뒤 종료하고 외부 진단만 `degraded`(공개 경로 미확인)로 분리했습니다. 실제 node-local 실패는 Runtime `failed`로 남는 회귀 검사도 통과했습니다.

첫 실행에서 신규 테스트의 점검 개수 오기 1건과, 하위 `python3`에 PyYAML이 없는 개발 환경 오류 2건이 있었습니다. 점검 항목을 실제 11개로 정확히 검증하고 개발 가상환경 PATH를 적용한 뒤 95개 전체를 재실행했습니다. 테스트 삭제·skip 추가·실패 무시는 하지 않았습니다.

## E. GCP smoke evidence(기존 실환경 확인 자료)

다음은 **사용자가 제공한 기존 확인 결과**입니다. 이번 작업의 GCP 원격 실행 결과가 아닙니다.

| 항목 | 기존 확인 결과 |
| --- | --- |
| OS / CPU | Ubuntu 24.04 x86_64 |
| K3s / Cilium | Ready / Running |
| namespace / workload | `tenant-demo` / `fixture-npm-js` |
| Service 포트 | NodePort 30080 |
| node-local `/health` | HTTP 200, `{"status":"ready"}` |
| 외부 `34.47.68.21:30080` | timeout |

이 자료에서 Runtime의 노드 내부 구간은 정상입니다. 외부 timeout은 provider exposure 또는 외부 네트워크 경계 문제로 분리합니다. timeout만으로 firewall(방화벽)이 확정 원인이라고 주장하지 않습니다. 과거 `GCP-SMOKE-2026-10-02.md`는 당시 인증·노드 미준비로 BLOCKED였던 별도 실행 기록이며 그대로 보존했습니다.

## F. AWS smoke evidence

이번 작업에서 기존 AWS 앱 `https://fixture-npm-js-8461c33ac524.railshot.io/health`에 GET을 보내 **HTTP 200, `{"status":"ready"}`**를 확인했습니다. 기존 공개 서비스 관찰입니다. AWS 노드에 접속하여 K3s/Cilium을 새로 확인하거나 Runtime 배포를 실행한 것은 아닙니다. 승인된 전용 SSH·SSM 실행 경로를 사용하지 않았으며 IAM·network·VM·앱 변경도 하지 않았습니다.

`docs/integration/cloud-e2e-progress.md`의 팀원 AWS/GCP 성공 기록은 참고 자료입니다. 팀원 자료·사용자 제공 결과·이번 직접 실행을 서로 다른 증거로 표시합니다.

## G. integration compatibility(통합 호환성)

integration은 feature보다 고유 commit 27개, feature는 integration보다 8개였습니다. `git diff HEAD origin/integration/team-assembly-20261002`에서 adapter/models/runtime/engine/render/exposure/schema, 기존 K3s preflight/install/health, Cilium의 committed source는 동일했습니다. 기존 Dashboard handoff 계약 테스트도 통과했습니다. **이번 신규 수정 후 전체 Dashboard → S1 → Runtime E2E를 실행한 것은 아닙니다.**

읽기 전용 호출 위치는 `apps/api/src/environments.js:414`의 `guest.check` → `runtime.install`, `:12`의 `deployment/scripts/environment.py` 등록 경계입니다. JSON Engine CLI에 Dashboard가 직접 전달되는 흐름으로 간주하지 않습니다. workload 적용과 Argo/GitOps 경계는 통합 담당자의 영역입니다.

integration의 새 고객 bootstrap·OpenStack 관련 변경은 이번 데모 점검 범위를 넘습니다. 공통 Runtime 계약 차이가 없어 이번에 integration을 merge하지 않았습니다. 정상 merge가 필요할 경우 통합 담당자와 별도로 진행합니다.

## H. Runtime에서 발견한 문제와 최소 수정

1. `engine.py`에서 선택적 `verification_url`이 필수 node-local health 반복 검사에 포함되어 외부 timeout도 `ENDPOINT_HTTP_FAILED`로 반환했습니다. 필수 로컬 검사 후 제한 시간 3초의 별도 진단으로 바꾸었습니다. 실패 시 node-local 정상과 Runtime `ready`를 유지하고 0.2의 `exposure_status`에 이유를 기록합니다. offline에서는 추가 외부 요청을 하지 않습니다.
2. `input_adapter.py`는 샘플 nginx에 tag만 허용하여 검증한 digest 고정을 거부했습니다. 동일 nginx 저장소·포트 80·경로 `/` 제한을 유지하며 명시적 SHA256 digest도 허용합니다. provider별 core 분기는 추가하지 않았습니다.
3. 기존 `verify`는 임시 네트워크 검사 Pod를 만듭니다. 데모 전 점검에는 이를 호출하지 않고 별도 읽기 전용 점검을 제공합니다. API 시간 초과·조회 불가를 성공으로 바꾸지 않습니다.
4. 전체 deployment 테스트에는 PyYAML과 자식 `python3`의 동일 환경이 필요했습니다. 개발 의존성을 고정하고 실행 문서를 보완했습니다.

0.1 output shape와 states(상태 전이) 순서는 유지합니다. 외부 검증 URL 실패의 의미는 Runtime 실패에서 외부 의존성 경고로 바뀝니다. 이 의미 변경은 통합 담당자에게 반드시 전달합니다. 0.2는 기존 object형 `exposure_status`에 `additional_verification`을 추가하므로 schema version을 새로 올리지 않았습니다.

## I. 다른 담당 영역

공개 GCP NodePort timeout은 upstream/provider exposure 미확인입니다. 수정하지 않았습니다. `apps/api/src/environments.js:414`의 Ansible 경계는 Engine CLI 호출과 다른 계약이며 연결 전체 확인이 필요합니다. 이것을 Dashboard 결함으로 확정하거나 수정하지 않았습니다.

Runtime CI(자동 검사)는 기존 `.github/workflows/railshot-ci.yml`의 `contracts`와 `runtime-smoke`입니다. `ci/scripts/ci_scope.py`는 `deployment/` 변경에 두 job을 선택하므로 경로 누락은 발견하지 않았습니다. 기존 workflow push 대상은 main/integration이며 feature push 자동 실행은 없습니다. 수동 실행은 타 영역도 선택하므로 이번에는 실행하지 않았습니다.

`.github/workflows/control-cilium.yml`은 Argo 등 control cluster도 검사하여 이번 범위에서 재실행하지 않았습니다. 기존 `d557045`의 Railshot CI run `37016831101` 및 Control Cilium run `37016836087` success는 **이전 source의 결과**입니다. 새 Runtime 전용 workflow는 fixture·loopback 테스트와 문법 검사만 하며 실제 cloud smoke가 아닙니다. 타 workflow는 수정하지 않았습니다. 새 source에 대한 GitHub 결과는 최종 응답의 run URL을 확인합니다.

## J. 구체적인 demo risk(시연 실패 지점)

- Lima 또는 노드 접속이 끊기면 preflight 프로세스를 실행할 수 없습니다. 라이브 명령 대신 저장된 자료를 보여주어야 합니다.
- 기존 K3s/Cilium/Pod가 준비되지 않으면 node-local health도 실패할 수 있습니다. 25초에 중단하고 재설치를 시작하지 않습니다.
- 신규 이미지를 라이브 pull(다운로드)하면 registry·인증·CPU 차이로 rollout이 멈출 수 있습니다. 사전 검증한 캐시 이미지와 같은 노드를 사용합니다.
- 공개 주소의 timeout은 관객 접속을 막습니다. node-local 성공이 이를 해결하거나 외부 접근 성공을 보장하지 않습니다.
- 실패한 update 이후 기존 Ready Pod가 남아 있지 않으면 읽기 전용 fallback도 HTTP 200을 보여줄 수 없습니다. 자동 rollback은 구현하지 않았습니다.
- schema 0.1 consumer가 외부 URL 실패를 Runtime 실패와 동일하게 기대했다면 의미 변경 확인이 필요합니다.

## K. 20~30초 판단 기준

Node Ready + Cilium Ready + Pod Ready + Service/NodePort와 실제 backend 일치 + node-local HTTP 200이면 Runtime 정상입니다. 공개 endpoint 문제는 별도 판단합니다. 25초 초과·접속 불가이면 `unknown`, 관측한 준비/HTTP 실패는 `not_ready`로 종료합니다. 준비되지 않은 이유가 Runtime 코드 결함인지 상위 환경 문제인지는 추가 진단 전 단정하지 않습니다.

## L. Runtime fallback

[실행 안내의 로컬 fallback](../../../DEMO-RUNBOOK.md)은 25초 timeout 안에서 `get nodes`, `get pods -A`, Cilium DaemonSet, target Deployment/Service, node-local curl을 수행합니다. 해당 복붙 명령을 실제 VM에서 실행하여 HTTP 200을 확인했습니다. GCP용 명령은 namespace·workload·`/health`를 분리하여 적었으며 이번 작업에서는 GCP에서 실행하지 않았습니다.

## M. integration 병합 준비 여부

Runtime의 로컬 검사와 읽기 전용 기존 Linux 노드 검증은 통과했습니다. 공통 입력/출력 구조는 유지합니다. 통합 담당자가 의미 변경, 실제 target 이름·health 경로, 외부 공개 상태 표시를 확인한 뒤 검토할 수 있습니다. main/integration/develop/타인 feature 변경·push·merge, rebase·force push·PR merge는 하지 않았습니다.

## N. handoff(통합 담당자 전달 문구)

> Deployment Runtime 데모 점검을 feature 브랜치에서 준비했습니다. 기존 K3s/Cilium 재사용 경로의 읽기 전용 preflight가 0.67초에 통과했고, Runtime 테스트 95개가 통과했습니다. 선택적 외부 URL 실패는 Runtime 정상과 분리하여 0.2 `exposure_status=degraded`에 기록합니다. 발표 대상 namespace·앱/Service 이름·이미지·health 경로와 이 의미 변경을 확인해 주세요. 공개 GCP timeout 및 Dashboard/S1 전체 연결은 별도 통합 범위입니다.

## O. 최종 commit

이 보고서·검사 도구·최소 수정·Runtime workflow를 함께 커밋하며, 최종 SHA와 feature 원격 일치 여부는 최종 응답에서 확인합니다. 문서에 자기 자신의 commit hash를 미리 기입하지 않습니다.
