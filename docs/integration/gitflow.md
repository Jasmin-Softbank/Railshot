# 통합 브랜치와 PR 병합 규칙

2026-10-02 합의: 현재 `integration/team-assembly-20261002`를 Gitflow의 `develop` 역할로 사용한다. 별도 `develop` 브랜치를 만들거나 `main`에 바로 구현을 모으지 않는다.

| 브랜치 | 역할 |
|---|---|
| `main` | 검증된 릴리스 반영. 이번 feature 통합 대상이 아님 |
| `integration/team-assembly-20261002` | 팀 구현을 모으고 연결부를 검증하는 개발 기준 |
| `feature/*` | 담당 구현의 원본. 담당 범위의 변경을 유지 |
| `codex/integrate-*` | 충돌 해결·연결부 보완이 필요한 경우 사용하는 임시 PR 브랜치 |

## 병합 절차

1. 원격 feature의 최신 SHA와 담당 범위를 확인한다. 팀원의 구현 의도를 유지하고 충돌·입출력 계약·실행 의존성을 점검한다.
2. 충돌이 없는 feature PR은 integration으로 merge commit을 만든다. 충돌이나 연결부 수정이 필요하면 최신 integration에서 임시 브랜치를 만들고 `git merge --no-ff <feature SHA>`로 원본 이력을 포함한다.
3. 임시 브랜치에서 충돌과 필요한 공통 연결부만 해결한다. 기존의 담당 코드만 담은 개인 feature에 통합본 전체를 역병합하지 않는다.
4. 바뀐 연결부의 테스트와 빌드를 실행하고 PR 본문에 원본 SHA·해결 내용·검증 범위를 적는다. 기존 클라우드 배포 결과를 새 merge commit의 배포 결과로 취급하지 않는다.
5. PR을 integration으로 merge한다. squash·rebase·force push 없이 feature 이력을 보존하고, 원격 HEAD와 포함된 feature SHA를 다시 확인한다.

Vercel Preview는 별도 웹 배포 연동이다. 런타임·Ansible·CI 검증과 구분하며, 프로젝트 설정·로그가 확인되지 않은 Vercel 실패를 팀 런타임 테스트 실패로 기록하지 않는다. 2026-10-02 후속 통합부터 GitHub branch protection으로 `Railshot CI gate` 성공과 최신 integration 반영을 강제한다. 관리자도 적용 대상이며 force push와 브랜치 삭제를 허용하지 않는다. feature 원본은 병합 후에도 보존한다.

## 2026-10-02 전체 팀 브랜치 대조

새 Railshot와 이전 Jasmin 저장소의 원격 브랜치를 함께 확인했다. `source-map.json`은 최초 조립 시점의 기록으로 보존하고 후속 대조를 여기에 적는다.

| 원본 브랜치 | 확인한 최신 SHA | 통합 상태 |
|---|---|---|
| Jasmin `feature/ansible_JB` | `90b5196f643850aeff16dfc0ebb17606d035b686` | 매핑 12개 모두 포함. 10개 동일, README/site.yml은 통합 연결부 수정 |
| Jasmin `feature/poc-onprem-hwagyun` | `e308749cc78408c3d933ea76aa06ab982045450b` | OpenStack 50개 파일 모두 동일 |
| Jasmin `feature/fe-mcp-jingi` | `6840d1387798d375234bbf97919210eec96709a3` | 매핑 16개 모두 포함. 이후 API/MCP/CLI/UI 통합 변경 유지 |
| Jasmin `feature/deployment-runtime-seungmin` | `ec6a9df0258eee9843aa457bc10703aa7026db40` | Railshot `c732b3b`에 후속 airgap 변경 포함. 원본 465개 중 462개 동일, 문서 2개와 VM 검사 timeout 옵션만 후속 수정 |
| Railshot `feature/poc-cloud-jihwan` | `efa4d7c599bf9af512112a353b3d5324c036db68` | [PR #2](https://github.com/Jasmin-Softbank/Railshot/pull/2)로 이력 연결 |
| Railshot `feature/deployment-runtime-seungmin` | `c732b3bc83ad1b9cab416f1d763cfee9be26ea05` | [PR #3](https://github.com/Jasmin-Softbank/Railshot/pull/3)로 이력·Ansible asset 연결 |
| Railshot `feature/dashboard-ui` | `4fb39c88e17070d2ac048ec0667083f0252b644c` | [PR #4](https://github.com/Jasmin-Softbank/Railshot/pull/4)에서 원본 UI·npm workspace 통합 및 새 Railshot CI 검증 |

이전 cloud 승민 브랜치 `fb503fd`, 지환 `9e13c7c`, 구조 문서 `4b5e22c`, 초기 main `e100373`도 확인했다. 이미 후속 구현으로 대체됐거나 코드 추가가 없는 브랜치를 다시 덮어쓰지 않는다. Patroni와 제품 UI→배포 자동 연결은 원본에도 완성돼 있지 않아 누락 병합으로 분류하지 않는다. 별도 채팅에서 진행 중인 새 Ansible API 작업은 완료 PR과 CI 결과를 받은 뒤 통합한다.

향후 병합에는 [Railshot CI](../../.github/workflows/railshot-ci.yml)의 `Railshot CI gate` 성공을 확인한다. 이 검사는 실제 일회성 Linux 런타임 설치와 정리를 포함하지만 외부 AWS/GCP 제품 배포 성공을 대신하지 않는다. [배포 해제 경로](e2e-teardown.md)는 같은 PR에서 관리한다.

## 지환 feature 이력 연결

기준 integration은 `adda5c7532c22da95e938a3cf1a7ba4c05e4ca47`, 개인 feature는 `efa4d7c599bf9af512112a353b3d5324c036db68`이다. 개인 feature의 210개 파일 중 208개는 통합본과 내용·mode가 동일하다. 나머지는 개인 범위를 설명하는 README와 Ansible 문서 상단 안내뿐이다.

이미 반영된 CI·CSP·네트워크·Ansible·GitOps 구현을 다시 복사하지 않고 merge로 계보를 연결했다. README와 Ansible 문서는 통합 범위의 설명을 유지했다. 통합본에만 있는 팀 구현과 실제 앱 선언은 보존했다.

## 화균 하이브리드 DB 후속 통합

이후 새로 게시된 `feature/multicloud-db-hwagyun@67d19efc81b01b2a55b6dd54c198088b018bdd1f`는 공통 조상이 없는 독립 root commit이다. 임시 통합 브랜치에서 `--allow-unrelated-histories` merge로 원본 이력을 보존했다. 루트 README는 통합 안내와 원본 사용법 링크를 유지하고 `.gitignore`는 합집합으로 해결했다. `ansible.cfg`는 기존 guest/runtime 설정을 보존하면서 DB 역할 검색 경로만 추가했다. 각 팀 DB playbook은 이미 `become`을 명시하므로 원본의 전역 권한 상승 설정을 다른 작업에 적용하지 않는다.

DB 역할·playbook·template과 기존 15개 테스트는 원본 그대로다. 공통 Ansible 전체를 새 lint 규칙으로 바꾸지 않도록 담당 `playbooks roles inventories/example` 경로에 lint를 적용하고 별도 CI job에 syntax·lint·실제 localhost 입력/렌더 검사를 연결했다. `database.configure` 및 `patroni.install` 실행에는 연결하지 않는다. 원본은 최소 DB 2·etcd 3·proxy 1 구성으로 단일 DB VM 지원과 실제 DB 복제·복원·정리는 후속 담당 범위다.

## 정빈 observability 후속 통합

검사 진행 중 새로 게시된 Railshot `feature/observability_JB@180482ad2f069eb09edf5056f2ad3d0621e70532`를 merge했다. 담당자의 `observability/` 구현과 이력을 그대로 유지한다. 기존 14개 unittest를 CI에 연결하며 Compose가 지정한 Prometheus/Blackbox 이미지에서 도구를 꺼내 native 검사 두 개도 생략하지 않는다. 컨테이너는 도구 추출 후 삭제하고 HTTP fixture·Blackbox 프로세스는 테스트의 finally에서 종료한다. 실제 운영 observer·exporter를 배포한 결과는 아니다.

## 승민 runtime 통합

[원본 PR #1](https://github.com/Jasmin-Softbank/Railshot/pull/1)의 `c732b3bc83ad1b9cab416f1d763cfee9be26ea05`를 merge했다. `deployment/`는 원본과 byte 단위로 동일하게 유지한다. 새 Cilium 설치 코드가 online 경로에서도 읽는 `airgap/versions.json`을 Ansible 전달 목록에 추가했다. 버전 정책 누락을 잡는 기존 경계 검사를 보강했고, 수정 전 실패·수정 후 통과를 확인했다.

통합 checkout에서 runtime 단위·계약 검사 49개, Ansible 검사 26개를 통과했다. 전달할 7개 파일의 존재와 Ansible 실행 버전·팀 정책 버전의 일치도 확인했다. 이 결과는 오프라인 통합 검사이며 새 코드로 AWS/GCP를 재설치하거나 기존 서비스를 재배포한 결과가 아니다. 원본 PR의 Linux 실검증 기록은 그 PR에 보존한다.

## 대시보드 통합 후보

`feature/dashboard-ui@4fb39c88e17070d2ac048ec0667083f0252b644c`의 화면을 원본 그대로 채택했다. `.gitignore`는 양쪽 규칙을 보존하고, 루트 npm workspace lock에 기존 `apps/api`를 함께 반영했다. API의 정적 화면 검사는 이전 화면의 문구 대신 새 화면의 실제 요소를 확인하도록 조정했다. API 19개 검사와 Vite production build를 통과했다.

이 UI는 소스·환경 선택과 선택 결과 확인까지 구현돼 있다. `/api/deploy` 호출이나 Provider→Ansible→CD 자동 연결을 추가하지 않았다. Railshot PR CI를 먼저 마련한 뒤 이 후보 PR의 자동 검사 결과를 확인하고 병합한다.
