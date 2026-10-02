# 통합 소스 조립 기록

2026-10-02, `integration/team-assembly-20261002`, base `e100373528352f1b9594311c643e9ad30ba87580`에서 담당자 소스 254개, 1,409,119 bytes를 조립했다. 이것은 원격 merge·배포·통합 E2E 완료 기록이 아니다. 원본 Jasmin의 미커밋 변경과 다른 worktree는 변경하지 않았다.

이 문서는 최초 소스 조립 당시의 기록입니다. 아래 설치 경로와 CI/CD 미연결 설명도 당시 상태를 보존합니다. 현재 구조와 후속 검증 범위는 [루트 README](../../README.md), [공통 아키텍처](../architecture/README.md), [Ansible 인터페이스](../api/ansible.md), [AWS/GCP 배포 기록](cloud-e2e-progress.md)을 확인하세요.

| 담당 | 선택 파일 수 | 배치 |
| --- | ---: | --- |
| 홍진기 | 16 | `apps/api`, `apps/dashboard` |
| 김화균 | 50 | `infrastructure/providers/openstack` |
| 김정빈 | 12 | `infrastructure/ansible` |
| 이승민 | 15 | `deployment` |
| 류지환 정리본 | 161 | `ci`, CSP Terraform, CI Ansible, Terraform 지원 도구 |

파일별 원격 branch/commit/path/blob/원문 SHA256와 조립 SHA256은 [source-map.json](source-map.json), 최초 11개 경로 연결 수정은 [relocation-map.json](relocation-map.json)과 [relocation.patch](relocation.patch)에 있다. 모든 원문은 명시적 고정 SHA에서 읽었다. AGENTS/AGENT, 실제 env·키·state·plan·tfvars, 외부 패키지 cache, 원문 실행 로그, 삭제된 개인 UI/control/CD 구현은 제외했다.

지환 소스는 원격 `9e13c7c2e50b003c146852ccf22c9e9399271cf7`에 보관된 정리 patch SHA256 `50cefac55f73c4c74e9f2bba9d3183706eb4d7940f352a15d16d02093d1616fe`를 임시 디렉터리에서 적용했다. 수정 파일을 cleanup manifest와 대조했으며 workflow는 SHA256 `71d78e47f54de7445442079ee543b78128320aaf2aaa2a9df955c9e3675dfa5b`의 보관본과 같았다. 원격의 개인 renderer·observer를 다시 가져오지 않았다.

운영 UI/API와 고객 runtime은 별도 책임이다. API는 진기의 localhost 서비스이며 Provider API는 화균의 project-scoped OpenStack 서비스다. CI는 원본 검사·제한된 AI 수정·재검사·동일 이미지 게시까지만 수행한다. `deployment/install.sh`는 승민의 단일 노드 runtime 및 nginx sample 설치다. 정빈의 `site.yml`도 K3s를 설치하므로 두 설치기를 연속 실행하면 안 된다. 통합 실행 경로는 새 `infrastructure/ansible/run.py`가 guest 검사 후 승민 `deployment/runtime.sh`를 한 번 호출한다.

초기 조립 당시 CI workflow는 `ci/workflows`의 소스 템플릿으로만 보관했다. 업로드용 private apps 저장소에 설치하고 신뢰할 수 있는 integration commit을 PLATFORM_REF로 고정하기 전에는 remote CI 실행 경로가 연결된 상태가 아니다. CodeBuild publisher와 Azure Terraform는 보존된 선택 실험이며 첫 통합 경로의 기본값이 아니다.

기존 연구·실행기록은 원격 고정 SHA에 보존되어 있다. [승민 검증 기록](https://github.com/Jasmin-Softbank/Jasmin/blob/fb503fd609161dc94cd167f75da0eef456d148e2/deployment-poc/reports/VALIDATION-2026-10-01.md)과 [화균 검증 기록](https://github.com/Jasmin-Softbank/Jasmin/blob/e308749cc78408c3d933ea76aa06ab982045450b/docs/validation.md)은 해당 부품의 과거 결과이며 이 통합본의 결과가 아니다. 원격 로그를 다시 실행하거나 cloud에 접속하지 않았다.

API→CI 연결은 [API–CI 검사 기록](api-ci-checks.md)에 완료 범위와 230개 로컬 검사 결과를 기록했다. 이후 Ansible→단일 runtime과 CD 검토용 선언 생성은 각 담당자의 별도 구현·검사 기록을 따른다. 실제 GitOps/Argo 적용→앱 상태·외부 URL 연결은 수행하지 않았다. 이 가운데 마지막 단계가 구현되기 전에는 published/인계 상태를 deployed로 표시하지 않는다. 조립 이후 연결 patch와 로컬 검사 결과는 별도 기록한다.

## 최종 디렉터리 정렬

[10/1 회의록의 디렉터리 합의](https://app.notion.com/p/3c18bee9ada482b8b38801af258d4c09#3ec8bee9ada4804398d4f59ea64553fb)에 맞춰 CD 인계 코드를 최상위 `gitops/`, 아키텍처를 `docs/architecture/`, 인터페이스를 `docs/api/`에 배치했습니다. 초기 조립 원장과 relocation patch는 당시 상태로 보존합니다. API/CI 연결 원장의 최종 해시는 이동한 문서 링크까지 포함합니다.

기존 runtime의 `scripts/`·`manifests/`와 Provider 내부 package는 담당자의 상대경로와 구현을 보존했습니다. 합의 그림에 있으나 구현이 없는 폴더는 만들지 않았고, 이전 CNPG 명칭도 최신 Patroni 논의를 대신하는 기본 구성으로 추가하지 않았습니다.
