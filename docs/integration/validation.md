# 통합 검토본 검증 — 2026-10-02

이 문서는 통합본에서 연결한 구현과 로컬 검사 결과를 정리합니다. 검증 범위는 **담당 소스 조립, 로컬 계약 연결, 정적·단위 검사**입니다. 실제 클라우드 자동 배포, 사용자 계정 인증, registry 게시, VM/guest/runtime 준비, Argo 적용, 외부 HTTP 확인은 [남은 인수 경계](#남은-인수-경계)에 정리했습니다.

## 이번에 연결한 구현

| 경계 | 변경과 확인 |
|---|---|
| 진기 API ↔ 지환 CI | 업로드 commit/운영자 target 고정, workflow checkout 검증, loop/release 두 단계와 실제 producer attempt·artifact ID 결합 |
| CI 게시 ↔ 소비자 | 기존 bundle 증거 bytes 유지, 5개 파일의 flat publication과 해시 영수증, Node가 Python producer 결과를 읽는 연결 검사 |
| 정빈 Ansible ↔ 승민 runtime | guest 검사 공유, 허용된 CLI 요청, runtime-only entrypoint. JB 별도 Flannel 설치를 연속 실행하지 않음 |
| CI 게시 ↔ Argo 인계 | 같은 digest로 단일 stateless 앱의 검토용 Deployment/Service/NetworkPolicy와 Application 생성. Git/cluster에는 쓰지 않음 |
| AWS 공개 앱 ↔ 관리망 | Route53/ALB/ACM/명시적 NodePort와 별도 gateway ENI의 EIP/UDP51820을 선택형 모듈로 분리 |

OpenStack Controller는 기존 package 구조와 provider 책임을 유지합니다. 외부 provider 규약과 내부 Python Port를 위한 원격 서버는 추가하지 않았습니다. source-map에는 고정 branch/SHA/경로, relocation-map에는 최초 이동, api-ci-changes에는 API/CI 연결 해시를 기록했습니다. 최종 코드·문서의 파일 manifest는 공유 패키지를 생성할 때 별도로 기록합니다.

## 실행한 검사

| 검사 | 개수/결과 | 실제 범위 |
|---|---:|---|
| Node API/CLI/MCP·업로드·published reader | 16 PASS | mock GitHub API 및 실제 로컬 Python receipt writer 연결 |
| CI/common·gate·AI 작업 제어·SDK 경계·intake | 214 PASS | 모델 없는 단위 검사. [세부 명령](api-ci-checks.md) |
| OpenStack Controller | 243 PASS | lockfile 환경의 pytest, 모의 HTTP/SDK 계약 |
| Ansible adapter | 16 PASS | 입력/SSH 설정/timeout/receipt/부분 실패, 실제 원격 호출 없음 |
| 기존 runtime verify/load | 8 PASS | 모의 K3s/Cilium과 실제 loopback HTTP. 클러스터 부하 시험 아님 |
| 기존 CSP Terraform 지원 도구 | 32 PASS | 모든 Terraform 실행 mock, 파일 이동 후 import/계약 검사 |
| AWS/CI/GCP/Azure cloud-init | 5+1+8+1 PASS | Terraform console로 템플릿 렌더, 포함 스크립트 구문 검사 |
| CD handoff | 1 PASS | 여러 정상/음성 subcase: digest, target, source, 변조, 미지원 요청 차단 |
| AWS edge | fmt / init-backend=false / validate PASS | provider schema 확인. plan/apply·계정 호출 없음 |
| Ansible YAML / shell | syntax PASS | guest/runtime/site 3개와 수정 shell의 bash -n |

**합계 545개 단위·계약 검사를 통과했습니다.** 구문·정적 검사는 이 수에 포함하지 않습니다. OpenStack pytest에는 51개 dependency deprecation warning이 있었으며 실패는 없었습니다. 검사에 필요한 dependency는 로컬에 설치했습니다.

추가 검사에서는 CI cloud-init 테스트가 이전 `/infra/ansible/ci.yml` 경로를 기대하는 것을 확인했습니다. 새 경로 `/infrastructure/ansible/ci.yml`로 기대값을 수정한 뒤 해당 검사를 다시 통과했습니다. CodeBuild 선택 실험의 buildspec 경로도 `ci/workflows/`로 맞췄습니다. CD receipt는 run/producer/bundle ID를 유지하도록 보완했고, AWS ALB health matcher는 gate·Kubernetes의 2xx/3xx 범위와 맞췄습니다.

검사를 재현할 때는 각 package의 README와 위 세부 명령을 따릅니다. CI Python 검사는 PyYAML/jsonschema와 기존 SDK 테스트 환경을 사용했습니다. 기본 테스트 명령은 모델 호출·VM 생성·전체 장애 주입·자동 cleanup을 실행하지 않습니다. 팀원별 PoC와 검증 스크립트는 보존하며 일반 실행 경로와 별도로 둡니다.

## 남은 인수 경계

| 항목 | 남은 일 / 담당 |
|---|---|
| OpenStack 온보딩·인증 | 화균: unscoped→scoped 방식과 현재 application credential 구현 사이의 채택·교환·만료·폐기 계약. 외부 호스팅과 내부 DevStack demo 프로젝트 구분 |
| Provider → guest | 화균/정빈/지환: 생성 후 자원 ID·사설 주소·host key·SSH identity·OS 준비 정보를 trusted target inventory로 연결. `ACTIVE`만으로 호출하지 않음 |
| 실제 runtime | 정빈/승민: 승인된 amd64 단일 노드에서 설치·재실행·실패 후 상태 확인. 다중 노드는 현재 명시 blocked |
| DB/Patroni | 화균: 별도 VM의 실제 playbook·DB/DCS 배치·endpoint·복구 기준. 입력 명세만 있으며 실행은 blocked |
| CI 운영 | 지환/진기: apps 저장소에 workflow 설치, 신뢰된 PLATFORM_REF·runner·model·registry 설정, native run/게시 확인 |
| CD/공개 접속 | 정빈/승민/지환: 제한된 AppProject·repo·cluster 등록, Git commit/Argo sync/Pod digest, 외부 DNS·TLS·앱 응답을 API 상태로 연결 |
| 네트워크 | 지환/화균: 실제 subnet/AZ·target/SG·gatewayENI, WireGuard peer/AllowedIPs/왕복경로/방화벽, 온프레 공개 입구 선택 |
| 플랫폼 운영 | 진기/지환: localhost API의 사용자 인증·인가/Host/Origin/배치 계약. 고객 앱 ALB 모듈이 이를 대신하지 않음 |

각 담당자의 입력과 결과가 확정되면 기존 호출 지점에 연결합니다. 이번 통합에는 상위 범용 workflow engine·controller·cloud agent를 추가하지 않았습니다.

## 보존과 제외

- 기존 연구는 `docs/research/`에 관측 시점과 함께 보존하며, 원래 개인 workspace의 연구·cleanup archive도 유지합니다.
- 지환 개인 UI/control/CD renderer·observer, CNPG/KEDA 기본 구성과 광범위한 개인 인프라 E2E는 복원하지 않습니다. 고객 앱의 CI 검사와 팀원 PoC 검증은 구분해 보존합니다.
- AGENT/AGENTS는 각자 관리합니다. 실제 `.env`·키·kubeconfig·tfvars/state·cache·private raw transcript는 공유 패키지에서 제외합니다.
- Slack 세 날짜의 전사 전문과 스레드는 로컬 비공개 MD로 보존했습니다. 공유 문서는 정확한 기술 용어를 사용하고 합의·미결·구현을 구분합니다.

위 3cc2690 검증 당시에는 Notion 게시, Slack 메시지 제출, Figma 업로드, Git push, 실제 인프라 변경을 수행하지 않았습니다. 이후 새 저장소 브랜치 게시와 CI 실가동 준비는 [진행 기록](ci-activation.md)에서 구분합니다. 별도 검토 세션의 의견은 코드·문서와 대조한 뒤 확인된 오류만 반영합니다.
