# 통합 소스 조립 기록

2026-10-02, `integration/team-assembly-20261002`, base `e100373528352f1b9594311c643e9ad30ba87580`에서 담당자 소스 254개, 1,409,119 bytes를 조립했다. 이것은 원격 merge·배포·통합 E2E 완료 기록이 아니다. 원본 Jasmin의 미커밋 변경과 다른 worktree는 변경하지 않았다.

이 문서는 최초 소스 조립 당시의 기록입니다. 아래 설치 경로와 CI/CD 미연결 설명도 당시 상태를 보존합니다. 현재 구조와 후속 검증 범위는 [루트 README](../../README.md), [공통 아키텍처](../architecture/README.md), [Ansible 인터페이스](../api/ansible.md), [AWS/GCP 배포 기록](cloud-e2e-progress.md)을 확인하세요.

## 현재 runtime 경로와 승민 담당 범위 — 2026-10-02 대조

대조 기준은 승민 원본 `5f893ff2cf991561698524fe0d76cabcd2c07323`과 integration `b9cfd2969b261d1279aae927bbb5886209716c62`다. 원본 HEAD 전체가 integration의 조상은 아니지만 bootstrap·Cilium·JSON runtime·airgap·Lima 검사·선택 모듈 안내의 핵심 구현은 동일하다. 원본에만 있는 후속 [GCP blocked smoke](https://github.com/Jasmin-Softbank/Railshot/blob/5f893ff2cf991561698524fe0d76cabcd2c07323/deployment/scripts/tests/results/GCP-SMOKE-2026-10-02.md)와 [Vercel 진단 기록](https://github.com/Jasmin-Softbank/Railshot/blob/5f893ff2cf991561698524fe0d76cabcd2c07323/deployment/scripts/tests/results/VERCEL-DIAGNOSIS-2026-10-02.md)을 핵심 runtime 미반영으로 분류하지 않는다. 통합본의 플랫폼 이미지·배치 변경은 고객 runtime 구현과 별도로 비교한다.

GCP 기록의 `BLOCKED`와 도구·권한 부족은 작성자 환경과 그 실행 시점의 결과다. 이를 현재 운영 GCP의 상태나 통합본의 모든 GCP 경로 실패로 바꾸어 해석하지 않는다.

| 최초 담당 항목 | 현재 소스와 지원 범위 | 구분해야 할 한계 |
|---|---|---|
| 온프레/클라우드 배포 환경·`install.sh` | 준비된 단일 Linux 노드의 [bootstrap](../../deployment/bootstrap/)·[Cilium](../../deployment/cilium/) 설치, [JSON runtime](../../deployment/scripts/runtime.py) 반영 | `deployment/install.sh`와 `deployment/runtime.sh`는 양쪽에 없다. VM 생성·SSH 대상 선택은 상위 Provider/Ansible 책임 |
| Lima | [provider simulation](../../deployment/scripts/tests/provider_simulation.py)으로 같은 runtime을 일회성 VM에서 검사 | AWS/GCP 입력을 모의해도 실제 클라우드 IAM·라우팅·공개 ingress 검증은 아님 |
| Cilium 네트워크 정책 | [Ingress 템플릿](../../deployment/cilium/network-policy.json.template)과 [renderer](../../deployment/scripts/render.py), 허용/차단 검사 제공 | 기본 standalone 배포에는 자동 적용하지 않는다. 같은 namespace의 승인 label Pod를 허용하는 선택 예제이며 전체 tenant 격리 완료가 아님 |
| cloudflared | [모듈 안내](../../deployment/cloudflared/README.md), [기존 공개 URL의 HTTP 확인](../../deployment/scripts/exposure.py) | `cloudflare-tunnel` 입력이 connector 설치·Tunnel 생성·DNS 설정을 수행하지 않음 |
| CNPG | [선택 모듈 안내](../../deployment/cnpg/README.md)만 존재 | 자동 설치 미구현. 후속 DB 배치 합의를 대신하는 기본 DB 관리자로 추가하지 않음 |
| Sealed Secrets | [선택 모듈 안내](../../deployment/sealed-secrets/README.md)만 존재 | controller 설치·SealedSecret 암호화/해제 자동화 미구현. Kubernetes Secret 사용과 다름 |

현재 실행 경로는 다음 두 가지이며 서로의 완료 상태를 대신하지 않는다.

- **통합 노드 준비:** `infrastructure/ansible/run.py` → `guest.yml` → [`runtime.yml`](../../infrastructure/ansible/runtime.yml). 팀의 `common.sh`, bootstrap/K3s·Cilium·health 스크립트와 버전 정책을 대상 노드로 복사하고 `/run/railshot-deployment.lock` 아래에서 실행한다. JSON `runtime.py`/`engine.py`를 호출하거나 샘플 앱을 설치하는 경로는 아니다. 별도 standalone `site.yml`을 연이어 실행하지 않는다.
- **단독 runtime·CI 시험:** [`deployment/scripts/deploy.sh`](../../deployment/scripts/deploy.sh) → `runtime.py` → 입력 adapter → `engine.py`. K3s/Cilium과 요청 workload를 설치하고 `verify.sh`로 Pod·Service·HTTP를 검사한다. `cleanup.sh`의 전체 클러스터 제거 옵션은 소유가 확인된 일회성 노드에만 사용한다. [정리 절차](e2e-teardown.md)를 따른다.

여기서 단일 노드 지원은 고객 runtime 기준이다. 운영 K3s의 control/build profile은 [`cilium/preflight.py`](../../deployment/cilium/preflight.py)가 별도 CIDR·노드 역할로 검사하며, 이를 고객 클러스터의 일반 다중 노드 지원으로 표현하지 않는다.

제품의 Argo 경로는 [`gitops/handoff.py`](../../gitops/handoff.py)가 앱별 Deployment/Service와 **명시한 ingress CIDR·포트 허용, egress 차단** NetworkPolicy를 생성한다. standalone의 선택 Ingress 템플릿과 다른 경로다. Private pull은 namespace의 Kubernetes Secret과 `imagePullSecrets` 참조를 사용하며, Argo cluster Secret·자격 갱신 역시 Sealed Secrets 구현을 의미하지 않는다. 선언 생성, 실제 Secret 설치·image pull, Cilium의 트래픽 허용/차단은 각각 확인한다. [GitOps 계약](../../gitops/README.md)을 따른다.

DB 책임은 [10/1 회의 2:45:09–2:46:55 및 2:53:19–2:53:42](../meetings/2026-10-01.md)의 K8s 외부 배치·화균 담당 결정을 따른다. 최초 CNPG 항목만으로 CNPG와 외부 Patroni를 동시에 설치하지 않는다. 후속 DB 실행 지원과 단일 거점 인수 결과는 [현재 Ansible 계약](../api/ansible.md), [DB 검증 기록](database-acceptance-20261002.md)에서 확인한다.

이 대조는 소스 반영과 역할 범위 확인이다. 원본의 Lima 결과, hosted runner E2E, [AWS/GCP 앱 배포](cloud-e2e-progress.md), [runtime·관측 인수](observability-acceptance-20261002.md)는 각각 기록된 노드·revision·시점의 증거다. 코드 동일성만으로 현재 운영 Cilium·runner 상태나 모든 R&R 항목의 실환경 완료를 주장하지 않는다. 이번 문서 수정에서는 운영 자원을 변경하지 않았다.

## 최초 조립 내역 — 과거 기록

| 담당 | 선택 파일 수 | 배치 |
| --- | ---: | --- |
| 홍진기 | 16 | `apps/api`, `apps/dashboard` |
| 김화균 | 50 | `infrastructure/providers/openstack` |
| 김정빈 | 12 | `infrastructure/ansible` |
| 이승민 | 15 | `deployment` |
| 류지환 정리본 | 161 | `ci`, CSP Terraform, CI Ansible, Terraform 지원 도구 |

파일별 원격 branch/commit/path/blob/원문 SHA256와 조립 SHA256은 [source-map.json](source-map.json), 최초 11개 경로 연결 수정은 [relocation-map.json](relocation-map.json)과 [relocation.patch](relocation.patch)에 있다. 모든 원문은 명시적 고정 SHA에서 읽었다. AGENTS/AGENT, 실제 env·키·state·plan·tfvars, 외부 패키지 cache, 원문 실행 로그, 삭제된 개인 UI/control/CD 구현은 제외했다.

지환 소스는 원격 `9e13c7c2e50b003c146852ccf22c9e9399271cf7`에 보관된 정리 patch SHA256 `50cefac55f73c4c74e9f2bba9d3183706eb4d7940f352a15d16d02093d1616fe`를 임시 디렉터리에서 적용했다. 수정 파일을 cleanup manifest와 대조했으며 workflow는 SHA256 `71d78e47f54de7445442079ee543b78128320aaf2aaa2a9df955c9e3675dfa5b`의 보관본과 같았다. 원격의 개인 renderer·observer를 다시 가져오지 않았다.

초기 조립 기록에서는 운영 UI/API와 고객 runtime을 별도 책임으로 구분했다. API는 진기의 localhost 서비스, Provider API는 화균의 project-scoped OpenStack 서비스, CI는 원본 검사·제한된 AI 수정·재검사·동일 이미지 게시 범위였다. 당시 `deployment/install.sh`를 단일 노드 runtime·nginx sample 설치로, `deployment/runtime.sh`를 guest 검사 뒤 한 번 호출하는 경로로 기술했다. **이 두 파일명은 현재 실행 지침이 아니다.** 현재 경로는 위 대조 표와 실행 순서를 따른다.

초기 조립 당시 CI workflow는 `ci/workflows`의 소스 템플릿으로만 보관했다. 업로드용 private apps 저장소에 설치하고 신뢰할 수 있는 integration commit을 PLATFORM_REF로 고정하기 전에는 remote CI 실행 경로가 연결된 상태가 아니다. CodeBuild publisher와 Azure Terraform는 보존된 선택 실험이며 첫 통합 경로의 기본값이 아니다.

기존 연구·실행기록은 원격 고정 SHA에 보존되어 있다. [승민 검증 기록](https://github.com/Jasmin-Softbank/Jasmin/blob/fb503fd609161dc94cd167f75da0eef456d148e2/deployment-poc/reports/VALIDATION-2026-10-01.md)과 [화균 검증 기록](https://github.com/Jasmin-Softbank/Jasmin/blob/e308749cc78408c3d933ea76aa06ab982045450b/docs/validation.md)은 해당 부품의 과거 결과이며 이 통합본의 결과가 아니다. 원격 로그를 다시 실행하거나 cloud에 접속하지 않았다.

API→CI 연결은 [API–CI 검사 기록](api-ci-checks.md)에 완료 범위와 230개 로컬 검사 결과를 기록했다. 이후 Ansible→단일 runtime과 CD 검토용 선언 생성은 각 담당자의 별도 구현·검사 기록을 따른다. 실제 GitOps/Argo 적용→앱 상태·외부 URL 연결은 수행하지 않았다. 이 가운데 마지막 단계가 구현되기 전에는 published/인계 상태를 deployed로 표시하지 않는다. 조립 이후 연결 patch와 로컬 검사 결과는 별도 기록한다.

## 최종 디렉터리 정렬

[10/1 회의록의 디렉터리 합의](https://app.notion.com/p/3c18bee9ada482b8b38801af258d4c09#3ec8bee9ada4804398d4f59ea64553fb)에 맞춰 CD 인계 코드를 최상위 `gitops/`, 아키텍처를 `docs/architecture/`, 인터페이스를 `docs/api/`에 배치했습니다. 초기 조립 원장과 relocation patch는 당시 상태로 보존합니다. API/CI 연결 원장의 최종 해시는 이동한 문서 링크까지 포함합니다.

기존 runtime의 `scripts/`·`manifests/`와 Provider 내부 package는 담당자의 상대경로와 구현을 보존했습니다. 합의 그림에 있으나 구현이 없는 폴더는 만들지 않았고, 이전 CNPG 명칭도 최신 Patroni 논의를 대신하는 기본 구성으로 추가하지 않았습니다.
