# RAILSHOT 테스트 통합본

이 저장소는 RAILSHOT의 AI 보조 배포 흐름을 함께 검토하는 로컬 통합본입니다. 담당 브랜치의 구현을 합의된 상위 디렉터리에 모으고, 입력·결과·설치 인터페이스를 연결했습니다. **코드 조립과 로컬 검증을 완료했으며, AWS/OpenStack 양쪽의 실제 자동 배포는 인수가 필요합니다.**

- [제품 기획서](docs/proposal.md) · [전체 설계와 6개 다이어그램](docs/architecture/README.md)
- [Ansible 인터페이스](docs/api/ansible.md) · [CI 게시 인터페이스](docs/api/ci-publication.md)
- [회의 결정·R&R](docs/meetings/2026-10-01.md) · [공식 제출 요건](docs/submission-requirements.md)
- [조립 출처](docs/integration/source-map.json) · [이번 검증과 미연결 경계](docs/integration/validation.md)

## A. Directory Architecture

```text
apps/
  api/                         홍진기 HTTP API·CLI·MCP
  dashboard/                   홍진기 웹 UI
ci/
  workflows/                   apps 저장소에 설치할 Actions/CodeBuild 소스
  scripts/                     류지환 기본 검사·제한된 AI 수정·동일 이미지 게시
infrastructure/
  providers/openstack/         김화균 REST Controller / OpenStack SDK
  providers/terraform_tools/   기존 CSP 운영 도구
  terraform/{aws,gcp,azure}/    기존 CSP host 준비. runtime 배포와 구분
  terraform/aws-edge/          선택형 Route53·ALB + gateway EIP·UDP51820
  ansible/                     김정빈 guest 검사 + 승민 runtime 호출
deployment/                   이승민 K3s/Cilium 및 기존 샘플
gitops/                       게시 digest → 검토용 Argo 선언 인계
contracts/                     공유 요청 형식
examples/ansible/               설명용 요청 JSON
docs/                         기획·회의·출처
  architecture/               전체 구조와 편집 가능한 다이어그램
  api/                        Ansible·CI 인터페이스
```

상위 폴더는 팀 scaffold를 따르며, 각 담당자의 내부 패키지 구조는 유지합니다. 아직 구현이 없는 기능의 빈 폴더는 포함하지 않습니다. AGENT/AGENTS, 계정·키·state, 로컬 cache와 원문 전사는 Git에서 제외합니다. 이전 연구와 원본 브랜치·cleanup archive는 보존하며, 원본 Jasmin과 다른 worktree의 미커밋 변경도 유지했습니다.

## B. Interface and Result Boundaries

| 생산자 → 소비자 | 전달 | 현재 결과 의미 |
|---|---|---|
| UI/CLI/MCP → Node API → Actions | source commit, tenant/app, operator target ID | CI 요청 접수. target 자동 생성 아님 |
| CI loop → release → API | tested bundle와 게시 ZIP, run/attempt/artifact ID | `published`. 고객 앱 배포 완료 아님 |
| OpenStack Controller → 상위 실행기 | `resource_id`, `status`, `addresses` | 202 접수와 ACTIVE 구분. guest 정보·Ansible 자동 호출은 별도 연결 필요 |
| 운영 실행기 → Ansible CLI | target/provider/placement, private inventory, SSH 파일 참조 | 실제 guest/runtime receipt를 확인. 앱·URL 결과는 false |
| 게시 artifact → CD 인계 CLI | 5개 게시 파일 + trusted target 설정 | `rendered_for_review`. Git push·Argo sync는 수행하지 않음 |

Controller의 Python `Protocol`은 같은 프로세스에서 Adapter가 구현하는 규약입니다. 자격증명은 요청 DTO와 공유 artifact에 포함하지 않습니다. 기본 runtime 경로는 **JB guest 검사 → 승민 runtime 설치**입니다. 이 경로에서는 JB standalone K3s 설치를 추가로 실행하지 않습니다.

## C. Local User Journeys

### Case 1. UI/API와 업로드 계약 검사

API의 업로드 계약을 검사하고 로컬 서버를 시작합니다. Node 22 이상이 필요합니다.

```sh
npm ci --prefix apps/api --ignore-scripts
npm test --prefix apps/api
npm start --prefix apps/api
```

API는 localhost에서 시작합니다. 실제 GitHub 제출에는 [API 설정](apps/api/README.md)의 token/repository/ref와 `RAILSHOT_TARGET_ID`가 필요합니다. 운영자는 apps 저장소에 `ci/workflows/railshot-deploy.yml`을 설치하고 통합 commit을 `PLATFORM_REF`로 고정합니다. 이 통합 저장소에서는 workflow를 자동 활성화하지 않습니다. ALB에 API를 공개하기 전에는 인증·Host/Origin·target 인가 계약을 정하고, [플랫폼 운영 인수 항목](docs/integration/validation.md#남은-인수-경계)을 확인합니다.

### Case 2. OpenStack Controller 계약 검사

lockfile에 고정된 환경을 설치하고 Controller의 단위·모의 API 검사를 실행합니다.

```sh
uv sync --frozen --project infrastructure/providers/openstack --group dev
uv run --frozen --project infrastructure/providers/openstack pytest infrastructure/providers/openstack/tests -q
```

이 검사는 실제 서버를 생성하지 않으며 provider 비밀번호를 요구하지 않습니다. 실제 호출에 필요한 설정은 [Controller 실행 문서](infrastructure/providers/openstack/README.md)와 [Notion 기준 페이지](https://app.notion.com/p/REST-API-Controller-3eb8bee9ada4804c8dd3c683efa3269e)를 확인합니다.

### Case 3. Ansible 입력을 실행 없이 확인

설명용 요청의 형식과 지원 범위를 검사합니다. 첫 실행 경로는 amd64 Ubuntu 단일 control-plane을 지원합니다.

```sh
python3 infrastructure/ansible/run.py --request examples/ansible/runtime-single-node.json --validate-only
python3 -m unittest discover -s infrastructure/ansible -p test_run.py -v
```

`--validate-only` 통과는 입력 검사가 끝났다는 뜻입니다. Patroni 요청은 DB/DCS 배치를 구분해 받으며, 담당 playbook이 없으면 실행을 차단합니다. VPN 내부 SSH를 사용할 수 있고, SSM/local 실행에는 별도 transport 구현이 필요합니다. 상세 입력과 결과 형식은 [Ansible 인터페이스](docs/api/ansible.md)를 따릅니다.

### Case 4. CD와 AWS 경계 검토

- [CD 인계 CLI](gitops/README.md)는 CI에서 검증한 digest로 제한된 stateless 앱 선언을 만듭니다. DB·secret·외부 egress·다중 서비스는 지원 범위에 포함하지 않습니다.
- [AWS edge](infrastructure/terraform/aws-edge/README.md)는 기존 VPC·subnet·instance·gateway ENI를 입력으로 받습니다. Terraform validate는 구성의 유효성을 검사하며, 적용과 도달성은 별도로 확인합니다.
- Argo/registry/공개 URL은 담당 팀원이 target·권한·네트워크를 연결한 환경에서 인수합니다.

## 검증과 제출

검사 명령과 결과, 남은 인수 항목은 [검증 기록](docs/integration/validation.md)에 정리했습니다. `published`, `runtime_ready`, Argo sync, 공개 HTTP는 각각 확인합니다. 테스트에서 만든 receipt는 로컬 검사 결과로만 사용합니다.

공식 안내는 설계 문서와 실제 데모를 요구하며 최종 슬라이드는 금지합니다. 이 자료는 Notion에 옮기기 전 검토본입니다. Notion 게시·Slack 제출·cloud apply는 수행하지 않았습니다. 새 저장소의 테스트 통합 브랜치와 지환 담당 feature 게시·CI 준비 상태는 [실가동 진행 기록](docs/integration/ci-activation.md)을 따릅니다.
