# RAILSHOT 테스트 통합본

이 저장소는 RAILSHOT의 AI 보조 배포 흐름을 함께 개발하는 통합본입니다. **`integration/team-assembly-20261002`를 Gitflow의 `develop` 역할로 사용하며, 담당 feature의 변경은 PR을 통해 merge commit으로 합칩니다.** `main`은 검증된 릴리스를 반영하는 브랜치입니다. [브랜치 역할과 병합 절차](docs/integration/gitflow.md)를 따릅니다.

관리자 실행 경로의 AWS/GCP 배포와 private 이미지 pull·Argo·공개 HTTPS를 검증했습니다. 제품 UI에서 Provider 생성부터 공개 URL 반환까지 자동으로 연결하는 작업은 남아 있습니다. 현재 검증 근거는 [AWS/GCP 배포 기록](docs/integration/cloud-e2e-progress.md), [Inference Atlas 배포 기록](docs/poc/inference-atlas-20261002.md), [Ansible API 명세](docs/api/ansible.md)입니다. 이전 검증 문서는 각 문서에 적힌 시점의 기록입니다.

- [제품 기획서](docs/proposal.md) · [전체 설계와 6개 다이어그램](docs/architecture/README.md)
- [Ansible 인터페이스](docs/api/ansible.md) · [CI 게시 인터페이스](docs/api/ci-publication.md)
- [회의 결정·R&R](docs/meetings/2026-10-01.md) · [공식 제출 요건](docs/submission-requirements.md)
- [조립 출처](docs/integration/source-map.json) · [이번 검증과 미연결 경계](docs/integration/validation.md)

디렉터리 책임은 [Agents.md](Agents.md)의 합의를 따릅니다. [플랫폼 PR CI](ci/README.md)와 [E2E 자원 해제](docs/integration/e2e-teardown.md)를 확인할 수 있습니다.

화균 님의 선택형 하이브리드 PostgreSQL/Patroni 구현은 `infrastructure/ansible/playbooks/`와 `roles/`에 보존했습니다. [담당 구현의 사용법](https://github.com/Jasmin-Softbank/Railshot/blob/67d19efc81b01b2a55b6dd54c198088b018bdd1f/README.md)과 [입력 규격](docs/api/deployment-inputs.md)을 따르며, 기본 단일 DB VM 경로·제품 API에 자동 연결하거나 실제 DB를 배포한 상태는 아닙니다.

## A. Interface and Result Boundaries

| 생산자 → 소비자 | 전달 | 현재 결과 의미 |
|---|---|---|
| UI/CLI/MCP → Node API → Actions | source commit, tenant/app, operator target ID | CI 요청 접수. target 자동 생성 아님 |
| CI loop → release → API | tested bundle와 게시 ZIP, run/attempt/artifact ID | `published`. 유저 앱 배포 완료 아님 |
| OpenStack Controller → Ansible 연결부 | 생성: `resource_id`; 상세: `id`, `project_id`, `status`, `addresses` | 등록한 ACTIVE 자원을 inventory로 변환. 상위 API의 자동 등록·연속 호출은 별도 연결 필요 |
| 운영 실행기 → Ansible CLI | target/provider/placement, private inventory, SSH 파일 참조 | 실제 guest/runtime receipt를 확인. 앱·URL 결과는 false |
| 게시 artifact → CD 인계 CLI → 운영자 Git/Argo 실행 | 5개 게시 파일 + trusted target 설정 | 렌더 결과와 Git 반영·Argo sync·공개 HTTP 검증을 각각 기록. 제품 API 자동 연결은 별도 |

Controller의 Python `Protocol`은 같은 프로세스에서 Adapter가 구현하는 규약입니다. 자격증명은 요청 DTO와 공유 artifact에 포함하지 않습니다. 기본 runtime 경로는 **JB guest 검사 → 승민 runtime 설치**입니다. 이 경로에서는 JB standalone K3s 설치를 추가로 실행하지 않습니다.

## B. Local User Journeys

### Case 1. UI/API와 업로드 계약 검사

API의 업로드 계약을 검사하고 로컬 서버를 시작합니다. Node 22 이상이 필요합니다.

```sh
npm ci --prefix apps/api --ignore-scripts
npm test --prefix apps/api
npm start --prefix apps/api
```

API는 localhost에서 시작합니다. 실제 GitHub 제출에는 [API 설정](apps/api/README.md)의 token/repository/ref와 `RAILSHOT_TARGET_ID`가 필요합니다. 운영자는 apps 저장소에 `ci/workflows/railshot-deploy.yml`을 설치하고 통합 commit을 `PLATFORM_REF`로 고정합니다. 이 사용자 앱 게시용 템플릿은 통합 저장소에서 자동 실행하지 않습니다. 플랫폼 자체 PR 검사는 `.github/workflows/railshot-ci.yml`로 실행합니다. ALB에 API를 공개하기 전에는 인증·Host/Origin·target 인가 계약을 정하고, [플랫폼 운영 인수 항목](docs/integration/validation.md#남은-인수-경계)을 확인합니다.

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
python3 -m unittest discover -s infrastructure/ansible -p 'test_*.py' -v
```

`--validate-only` 통과는 입력 검사가 끝났다는 뜻입니다. Patroni 요청은 DB/DCS 배치를 구분해 받으며, 담당 playbook이 없으면 실행을 차단합니다. AWS SSM·GCP IAP 포트 전달과 strict SSH를 통한 runtime 설치를 검증했습니다. 상세 입력과 결과 형식은 [Ansible 인터페이스](docs/api/ansible.md)를 따릅니다.

### Case 4. CD와 AWS 경계 검토

- [CD 인계 CLI](gitops/README.md)는 CI에서 검증한 digest로 제한된 stateless 앱 선언을 만듭니다. DB·secret·외부 egress·다중 서비스는 지원 범위에 포함하지 않습니다.
- [AWS edge](infrastructure/terraform/aws-edge/README.md)는 기존 VPC·subnet·instance·gateway ENI를 입력으로 받습니다. Terraform validate는 구성의 유효성을 검사하며, 적용과 도달성은 별도로 확인합니다.
- AWS/GCP의 Argo·registry·공개 URL은 [관리자 배포 경로](docs/integration/cloud-e2e-progress.md)에서 검증했습니다. 운영 API 상시 배치와 제품 요청 자동 연결은 별도 인수 항목입니다.

## 검증과 제출

검사 명령과 결과, 남은 인수 항목은 [검증 기록](docs/integration/validation.md)에 정리했습니다. `published`, `runtime_ready`, Argo sync, 공개 HTTP는 각각 확인합니다. 테스트에서 만든 receipt는 로컬 검사 결과로만 사용합니다.

공식 안내는 설계 문서와 실제 데모를 요구하며 최종 슬라이드는 금지합니다. Notion 명세와 실제 AWS/GCP 배포 근거는 위의 최신 기록에서 확인합니다. 초기 CI 활성화 과정은 [실가동 진행 기록](docs/integration/ci-activation.md)에 시점별로 보존합니다. 코드 병합·로컬 검사·이미 배포된 서비스의 검증은 서로 구분합니다.
