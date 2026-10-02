# CI 백엔드 API 분류와 프론트엔드 연동 설계

2026-10-02 · **설계 제안 / 실행 코드 변경 없음**

신규 제품 API는 화균 님 OpenStack REST Controller를 기준으로 정한 [REST API 컨벤션](conventions.md)을 따른다. `/api/v1` 자원 경로, 접수·목록·오류 형식을 통일했으며 기존 `/api/deploy`와 `/api/runs`는 호환 계약으로 구분한다. 사용자 기준에 따라 경로는 `builds`, `deployments`, `profiles`, `plans`처럼 짧은 제품 자원의 복수 명사로 정한다. 내부 실행 용어를 대시·밑줄·camelCase로 조합한 경로는 사용하지 않는다. 이는 HTTP·URI 표준 및 REST 아키텍처 원칙과 구분한 로컬 이름 규칙이다.

최초 감사 기준은 `f35ffda3825681f292a62bde3db4da0f4b35009c`이며, 문서 게시 전 `integration/team-assembly-20261002@6037d3a8e22cb6951436ea7f9d59582952c5b745`의 후속 소스를 확인했다. 아래 브랜치 표는 최초 감사 당시의 기록으로 보존하고, 현재 상태와 해결된 문제를 구분한다. 이 문서의 새 endpoint와 JSON 예시는 구현된 API가 아니다. 기존 계약은 [사용자 API](../../apps/api/docs/interface.md), [Ansible OpenAPI](ansible.openapi.json), [CI 게시 계약](ci-publication.md)이 정본이다.

## 1. 결론과 소유 경계

**프론트엔드와 MCP에는 제품 API를 제공하고, CI 실행·자원 준비·Ansible·CD는 제품 백엔드가 각 담당 인터페이스로 연결한다.** 제품 API를 현재 Node 서비스에 추가하는 것으로 시작하며, 분류별로 새 서버를 만들 필요는 없다.

현재 CI의 소스 등록→검사/AI 수정→검증 이미지 게시→게시 결과 확인은 계약이 정비되어 있다. 담당 runtime·DB·기본 관측 소스도 integration에 들어 있다. 후속 integration에서 Dashboard의 기존 CI API 연결과 standalone Cilium 경로·공통 lock 정렬이 반영됐다. 컨테이너 패키징은 PR #10 브랜치에 있다. 남은 핵심은 v1 자원 API, 인가된 target 목록, 제품 배포 기록, Provider→Ansible 대상 등록, DB 변수 변환, CD 결과 소비다.

| 분류 | 제공자 → 소비자 | 책임 | 현재 형태 |
|---|---|---|---|
| 제품 API | `apps/api` → Dashboard·CLI·MCP | 허용 대상, 제출, 전체 배포 상태, 실패 안내 | CI 제출·조회 HTTP만 구현 |
| CI 실행 계약 | 제품 API → GitHub Actions, CI → 제품 API | 소스 commit·target 고정, 검사·AI 수정, 게시 증거 | workflow dispatch + artifact 조회 구현 |
| 자원 준비 | 제품 API/운영자 → Provider | VM·관리망 생성/조회, 자원 식별자 반환 | AWS/GCP/Azure Terraform CLI, OpenStack REST |
| 서버 구성 | 제품 백엔드 → Ansible | 승인된 VM의 guest 검사·runtime 설치 | 내부 validate/jobs/status HTTP 구현 |
| DB 구성 | 실행기 → 담당 DB 플레이북 | PostgreSQL·etcd·HAProxy 구성 | 플레이북 구현, HTTP 연결 미구현 |
| 앱 적용 | 제품 백엔드 → GitOps/Argo | 검증한 digest의 선언 생성·적용·상태 확인 | 운영자 CLI 구현, 제품 API 연결 미구현 |
| 관측 | 제품 API/운영자 → 관측 구성 | 노드·워크로드·HTTP 관측 | 별도 VM용 Prometheus/Grafana 설정; 유저용 집계 API 없음 |

CI는 VM을 생성하거나 앱의 배포 성공을 판정하지 않는다. 제품 API는 플레이북 내용을 재작성하지 않는다. 각 담당 결과를 입력 식별자와 결합해 다음 단계를 진행한다.

## 2. 첨부 구조도 대조

검토 파일: `Architecture.svg`, 4452×3219, SHA-256 `dd245364019352eba16d21331a29e3c2754a94185804f3c4cdd10965c90cdb9f`. SVG는 글자가 path로 변환되어 있어 전체를 PNG로 렌더링해 확인했다. 도형의 계층·포함 관계는 있으나 요청 방향이나 메시지 이름을 나타내는 화살표는 없다. 배치 그림만으로 호출 순서를 확정하지 않는다.

| 그림 요소 | 코드 근거 | 판정 / 설명할 경계 |
|---|---|---|
| Web-base Dashboard, MCP | `apps/dashboard`, `apps/api/src/mcp.js` | 두 진입점은 타당. Dashboard도 등록 대상에 기존 CI 제출·상태 조회를 호출함. 전체 환경 생성·앱 배포 자동 연결은 미구현 |
| API Service | `apps/api/src/server.js` | 제품 API 위치로 적합. 현재 공개 인증·환경 생성·CD 조율까지 구현된 서비스는 아님 |
| CI/CD Pipeline | `ci/workflows/railshot-deploy.yml`, `gitops/argo.py` | 책임 안에서는 함께 묶을 수 있으나 실행은 CI worker와 Argo로 구분. API 프로세스 내부에서 유저 빌드를 실행한다는 뜻으로 읽히면 안 됨 |
| Compute / Load Balancer / Network / DNS | OpenStack `api/router.py`, Terraform `aws-edge` | 기능 분류로는 타당. 모든 Provider가 네 기능의 같은 CRUD API를 제공하지는 않음 |
| Provider Interface | `terraform_tools/provision.py`, OpenStack `src/control_plane/ports/` | OpenStack 내부 port/adapter와 Terraform 계약은 있음. 전체 Provider를 묶는 제품 수준 공통 HTTP interface는 없음 |
| Terraform Controller → AWS/GCP/Azure | `infrastructure/providers/terraform_tools`, `infrastructure/terraform` | 승인된 saved plan을 다루는 관리자 CLI. HTTP controller로 표시하려면 추가 연결 필요 |
| REST API Controller → OpenStack/Proxmox | `infrastructure/providers/openstack`, `infrastructure/providers/proxmox` | OpenStack 구현 있음. Proxmox는 placeholder라 기능 제공으로 표시하면 안 됨 |
| Kubernetes 외곽선 | `deployment`, 운영/고객 클러스터 설명 | 그림에서는 controller/adapter를 포함. 운영 서비스의 배치 영역으로 해석할 수 있으나 AWS/OpenStack 자원 자체가 하나의 Kubernetes 안에 있다는 의미는 아님. 운영 클러스터와 고객 실행 클러스터를 명시할 필요가 있음 |
| 우측 CI Platform | 전체 제품 범위 표기 | 별도 실행 컴포넌트나 API가 아님. 구체 실행 주체는 worker·publisher·Argo로 설명 |
| 그림에 없는 Ansible·DB·관측·registry | 각 담당 폴더와 게시 계약 | Ansible은 VM 생성 후 설치 경계, DB는 K3s 밖 VM, 관측은 별도 VM, registry는 CI→CD 산출물 경계로 추가 설명 필요 |

권장 호출 순서는 **환경 준비: 제품 API→Provider→대상 등록→Ansible**, **반복 앱 배포: 제품 API→CI→게시 검증→GitOps/Argo→앱·공개 HTTP 확인**이다. 환경이 준비돼 있으면 반복 배포마다 VM·K3s를 다시 설치하지 않는다.

운영 Dashboard/API/MCP 파드, CI 빌드 전용 VM, 고객 앱의 K3s 실행 환경은 서로 다른 배치 단위다. `ci.yml`의 빌드 VM은 Docker/BuildKit을 사용하고 K3s를 거부하며, 운영 `control.sh`와 고객 `runtime.yml`은 다른 설치 경로다. 사용자가 지정한 `railshot.io`의 Dashboard 파드·AWS 노드·DNS/HTTPS/Ingress 배치는 별도 채팅에서 검토한다. 로컬 Dashboard의 기존 CI 연결과 railshot.io의 공개 배치·유저 인증은 별도 인수 항목이다.

## 3. 원격 브랜치 감사와 후속 반영

다음 표는 2026-10-02 17:56 KST의 원격 목록과 18:03 KST의 컨테이너 후속 확인 기록이다. 당시 integration은 f35ffda다. 복원된 원격 feature 이름이 존재한다는 사실과 integration 반영 여부를 구분했다. Git 이력뿐 아니라 담당 경로의 blob 내용을 비교했다. `source-map.json`은 최초 조립 기록이므로 후속 병합 전체를 대표하지 않는다.

| 브랜치 | 확인 SHA | integration과의 관계 / 인터페이스 판정 |
|---|---|---|
| `main` | `cc1f084bf8453fa3ba3afeac9fd44ba8d01f92e6` | 저장소 안내·규칙만 있음. 실행 인터페이스 검토 기준은 integration |
| `feature/dashboard-ui` | `4fb39c88e17070d2ac048ec0667083f0252b644c` | 화면 5개 파일은 integration과 blob 동일. API 호출·활성 배포 버튼 없음 |
| `feature/deployment-runtime-seungmin` | `a50dd1a3239e2f8651ee61b4efe7a0dba55c08b4` | `deployment/` 원본 내용 반영. 이전 Dashboard의 CI 제출·조회 연결은 이 브랜치에 남아 있음 |
| `feature/multicloud-db-hwagyun` | `67d19efc81b01b2a55b6dd54c198088b018bdd1f` | DB roles/playbooks/계약 반영. Ansible 공통 설정 병합 외 원본 보존. API 실행으로의 연결은 없음 |
| `feature/observability_JB` | `80dae724722ec41fdd82b6265e98fd7d5f7921ca` | 이전 `e42cec9`의 기본 관측 소스는 반영됐고 integration에 DB dashboard/alert 추가. 최신 standalone Cilium 변경은 미반영이며 통합 시 경로·lock 조정 필요. API 변화 없음 |
| `codex/container-deploy-20261002` | `957afa277d5c17ba5df01aa4a1fb4344aecf7074` | [PR #10](https://github.com/Jasmin-Softbank/Railshot/pull/10), 확인 시 open. `3752ade`에 Dashboard/API/MCP 이미지·Compose/Kubernetes 설정·내부 운영자 Bearer·Host/Origin 설정 구현, `957afa2`에 Argo 선언 검증 추가. integration 미반영. UI fetch·API 프록시·유저 인가·새 제품 endpoint는 없음 |
| `integration/team-assembly-20261002` | `f35ffda3825681f292a62bde3db4da0f4b35009c` | 담당 부품과 내부 계약은 조립됨. 제품 UI→전체 배포의 연결 완료는 아님 |

### 게시 전 재확인: integration 6037d3a

`feature/dashboard-ci-connection-20261002@201d24414988d1cfb299989eac82e3f21fbfeb41`의 UI 연결과 `feature/observability-followup-20261002@a9b10753c228fcba936c9512b1c752b3c90daa25`의 통합 수정이 반영됐다. 대시보드는 기존 CI API를 호출하며, standalone site.yml도 통합 Cilium 파일 배치와 공통 lock을 사용한다. `apps/api`의 실행 소스는 f35ffda와 동일하므로 신규 v1·다중 대상·전체 배포 조율이 추가된 것은 아니다.

### 현재 공백과 해결 기록

1. **화면 연결 회귀 해결:** 후속 Dashboard는 앱 이름 제안·수정, 등록 대상 선택, `/healthz`, `POST /api/deploy`, `GET /api/runs/{run_id}` polling을 구현했다. 4173의 API가 제공하는 같은 origin 화면에서 사용한다. Vite 4181 단독 실행에는 proxy/CORS 연결이 없어 제출을 차단한다. v1 자원 API로의 이관은 별도 작업이다.
2. **단일 등록 대상만 지원:** UI는 운영자 등록 대상으로 제출하며, cloud/onprem 선택은 연결 정보가 없어 실행을 차단한다. 제품 API는 여전히 서버의 단일 `RAILSHOT_TARGET_ID`만 지원한다. CI workflow의 `RAILSHOT_TARGET_IDS` 지원이 제품 API의 다중 대상 지원을 뜻하지 않는다.
3. **Provider 공통화는 일부:** Terraform은 CLI, OpenStack은 REST, Proxmox는 미구현이다. OpenStack의 network 목록은 기존 네트워크 조회이며 network/LB/DNS 생성 API가 아니다.
4. **Ansible 대상 등록 공백:** 동적 target 등록 endpoint가 없다. Provider 생성 결과를 등록 파일과 결합하고 실행기 재시작에 반영하는 현재 경로는 운영자 작업이다.
5. **DB 계약 불일치:** API의 `database/dcs` 그룹과 담당 플레이북의 `db_nodes/etcd_nodes/proxy_nodes`를 연결하지 않았다. `database.configure` 접수는 501로 차단한다.
6. **게시→적용 공백:** `published` 검증은 정비되어 있으나 API가 GitOps/Argo 결과를 소비하지 않는다. 상태 응답의 `url`은 항상 null이다.
7. **운영 공개 전제:** integration 제품 API는 localhost Host/Origin 검사에 묶여 있다. 후속 UI에는 선택적 운영자 토큰 입력과 헤더 전달이 있지만 제품 유저 인증은 아니다. PR #10은 내부 운영자 Bearer와 명시적 Host/Origin 설정을 추가했지만 유저 인증·tenant 인가는 없다. 컨테이너 Nginx도 정적 파일만 제공하며 API 프록시가 아니다. 외부 Dashboard 연결에는 같은 origin의 API 경로와 제품 인증·대상별 권한이 필요하다. 공개 제품에서 공유 운영자 토큰을 브라우저에 배포하지 않는다.
8. **설명 갱신 필요:** 공통 아키텍처 문서의 UI→API 구현 표기와 일부 CD 설명은 현재 화면/CLI 범위보다 넓거나 오래됐다. 이 문서의 기준 commit과 실제 소스를 우선한다.
9. **standalone Cilium 통합 문제 해결:** 후속 site.yml이 `deployment/cilium/install.sh`, `scripts/common.sh`, `airgap/versions.json`을 전달하고 `/run/railshot-deployment.lock`을 사용하도록 고쳐졌다. README의 DB 플레이북 설명도 통합 상태로 수정됐다. 기존 Flannel 설치를 자동 전환하지 않으며, 기본 API는 runtime.yml만 호출한다. 동일 노드에서 두 설치 진입점을 혼용하지 않는다.

## 4. 제공할 API 목록과 구현 순서

신규 제품 경로는 `/api/v1`을 사용한다. 화균 님의 Provider API와 HTTP 형식을 맞추되 내부 Ansible `/v1/ansible`과 OpenStack `/api/v1/servers`를 그대로 공개하지 않는다. 아래 v1 경로는 모두 **설계이며 아직 라우터에 없다.** 기존 구현은 입력 검사·CI dispatch·게시 검증을 재사용한다.

| 단계 | 소비자 | 메서드·경로 | 기능 | 상태 |
|---|---|---|---|---|
| 현재 | 운영 진입점 | `GET /healthz` | 프로세스·CI 설정 여부 | 구현. integration 로컬 응답은 target 포함, PR #10 원격 모드는 숨김. 클라우드/배포 준비 검사 아님 |
| 1 | Dashboard·CLI·MCP | `GET /api/v1/targets` | 유저가 선택 가능한 등록 대상과 사용 가능 기능 | 신규 |
| 1 | Dashboard·CLI·MCP | `POST /api/v1/builds` | CI 실행 자원 생성, 202 + Location | 신규 HTTP 표현. 기존 제출 로직 재사용 |
| 1 | Dashboard·CLI·MCP | `GET /api/v1/builds/{id}` | CI 상태·게시 결과·Actions 링크, 200 | 신규 HTTP 표현. 기존 조회·게시 검증 재사용 |
| 2 | Dashboard·CLI·MCP | `POST /api/v1/deployments` | 준비된 대상에 CI부터 앱 적용까지 요청 | 신규. 기존 CI 입력 검사·dispatch 재사용 |
| 2 | Dashboard·CLI·MCP | `GET /api/v1/deployments/{id}` | CI/CD/HTTP를 연결한 제품 배포 상태 | 신규 |
| 3 | 환경 설정 화면·MCP | `GET /api/v1/profiles` | 허용된 Provider·용도·사양 profile과 지원 범위 | 신규 |
| 3 | 환경 설정 화면·MCP | `POST /api/v1/plans` | 자원·runtime·DB 배치 검사 후 계획 저장, 201 | 신규. 클라우드 변경 부작용 없음 |
| 3 | 환경 설정 화면·MCP | `GET /api/v1/plans/{id}` | 저장한 계획·만료·실행 가능 여부 조회, 200 | 신규 |
| 3 | 권한 있는 유저/운영자 | `POST /api/v1/environments` | 검토한 plan을 고정해 환경 준비 접수 | 신규 |
| 3 | 환경 설정 화면·MCP | `GET /api/v1/environments/{id}` | 자원 생성·설치 상태와 결과 target 조회 | 신규 |

호환 경로 `POST /api/deploy`와 `GET /api/runs/{run_id}`는 기존 요청/응답을 유지한다. 새 v1을 구현할 때 이 두 경로를 자동 redirect하거나 응답 필드를 몰래 바꾸지 않는다. 기존 CLI/MCP를 보존하고 새 Dashboard는 v1 계약에 맞춰 연결한다. v1 라우트 구현 전 기존 경로로 수행한 CI 시험은 별도 임시 연결로 기록한다.

단계 1은 **CI 게시 기능**만 제공한다. 단계 2가 연결되면 버튼을 전체 배포로 표시한다. 단계 3은 새 환경이 필요한 경우에 사용하고, 완료된 환경의 `runtime_target_id`를 단계 2에 전달한다. API별로 별도 서비스·범용 작업 프레임워크·새 큐를 만들지 않는다.

초기에는 상태 응답의 단계 정보와 `actions_url`을 사용한다. 원문 로그 API·SSE·WebSocket, 임의 PromQL, 취소·재실행·삭제 endpoint는 이번 계약에 추가하지 않는다. 실제 producer의 안전한 이벤트 배송 또는 원격 취소·정리 계약이 마련됐을 때 추가한다.

### 4.1 대상 선택 — 신규 `GET /api/v1/targets`

입력은 인증한 유저의 권한과 `limit/marker`다. 프론트가 tenant·provider 자격·파일 경로를 지정하지 않는다. 서버의 target 설정을 재사용하되, 인가된 항목의 공개 요약만 반환한다. 목록은 컨벤션의 `items/next_marker`, limit 기본 20·최대 100을 따른다.

```json
{
  "items": [{
    "id": "demo-aws",
    "label": "AWS 테스트 환경",
    "environment": "cloud",
    "provider": "aws",
    "capabilities": {"ci_submission": true, "application_deployment": false, "database_configuration": false},
    "runtime": {"status": "unknown", "observed_at": null},
    "blockers": ["CD_NOT_CONNECTED"]
  }],
  "next_marker": null
}
```

위 값은 설명용이다. `ci_submission`은 제출 설정과 권한을, `application_deployment`는 연결 코드·대상 준비·CD 계약을 함께 확인해 계산한다. 등록만으로 runtime을 ready로 만들지 않는다. 미관측·오래된 관측은 unknown/stale로 반환한다. 유효기간은 운영자 target 정책에서 정하고 `observed_at`과 함께 공개한다.

현재 API가 한 target만 지원하므로 첫 구현의 목록도 최대 한 개로 제한한다. 여러 개를 보여 주려면 서버의 target별 서비스 선택·권한 검사·CI 허용 목록을 함께 변경해야 한다. 목록만 늘려 화면에서 실행 가능한 것처럼 표시하지 않는다.

### 4.2 빌드 자원 — 신규 v1 표현과 기존 호환

`builds`는 소스를 검사·필요 시 수정하고 검증한 이미지를 게시하는 제품 자원이다. CI의 개별 셸 실행이나 build step을 뜻하지 않는다. 신규 `POST /api/v1/builds`는 `multipart/form-data`로 필수 `app`, 필수 `target_id`, 아래 소스 중 정확히 하나를 받는다. target 목록의 `id`를 요청의 `target_id`로 사용한다. 기존 `POST /api/deploy`의 선택 target·`x-jasmin-request: deploy`는 legacy 계약이며 v1의 인증 수단으로 사용하지 않는다. v1 공개 전에 사용자 인증과 해당 방식의 CSRF 방어를 적용한다.

| 입력 | 인코딩 |
|---|---|
| 공개 GitHub 저장소 | `repository_url` 문자열 |
| ZIP | `archive` 파일 |
| 폴더 | 반복 `files` 파일 + 동일 순서의 JSON 배열 `paths` |

앱 이름은 기존 3–30자 규칙을 사용한다. CLI의 `inferredAppName()` 정책을 참조해 이름을 제안하되 유저가 확인·수정할 수 있게 한다. 실제 bytes/files를 보내야 하며 화면의 `selectedSource.label`만 보내면 안 된다. `environment/provider` 문자열을 임의로 `target_id`로 쓰지 않는다.

v1 CI 실행의 `resource_id`와 상세 `id`는 현재 설정한 apps 저장소의 **GitHub workflow 실행 ID를 문자열로 표현**한다. 별도 범용 job 자원을 만들지 않는다. GitHub 숫자 ID를 브라우저에서 계산하거나 UUID로 재해석하지 않는다. 여러 apps 저장소를 지원하려면 이 ID 범위와 조회 매핑을 먼저 확장한다.

dispatch 결과의 run ID를 확인하고 run→principal/tenant/app/target 바인딩을 저장한 뒤 `202`, `Location: /api/v1/builds/{id}`, `Retry-After: 2`와 아래 접수 형식을 반환한다. 바인딩 저장 실패나 dispatch 응답 유실은 접수 성공으로 꾸미지 않고 안전한 오류와 `outcome_unknown=true`를 반환한다. 현재 legacy에는 이 바인딩이 없어 v1 구현 시 함께 추가해야 한다.

```json
{
  "resource_id": "123",
  "action": "create",
  "status": "accepted",
  "request_id": "9cd16c11-e380-4c48-8b97-d9dbdfbdf200"
}
```

`GET /api/v1/builds/{id}`는 소유권 검사 후 자원 객체를 직접 반환한다. 기존 결과의 `run_id`는 문자열 `id`, `state`는 제품 `status`로 표현하고 GitHub의 원본 `status/conclusion`은 `workflow.status/conclusion`에 보존한다. `app`, `target_id`, `source_commit`, `steps`, `publication`, `actions_url`, `url`의 의미는 유지한다. 제품 자원 ID와 HTTP request_id를 섞지 않는다. 상태 `queued/running/failed/publication_unverified/published`는 기존 검증 결과를 그대로 사용한다. `published`를 앱 배포 완료로 바꾸지 않으며 `url`도 실제 CD 연결 전에는 null이다.

기존 경로의 응답 `run_id/state`는 그대로 유지한다. CI 제출에는 영속 idempotency 계약이 없으며 v1 HTTP 표현만 추가해도 이 제한은 같다. 특히 source commit 뒤 dispatch 응답을 잃은 경우 자동 재전송하지 않는다. 202 이전/이후의 부분 결과를 제품 기록으로 보존하고 재시작 후 복구하는 처리는 다음 제품 deployment 자원에서 추가한다.

### 4.3 제품 배포 — 신규 `POST /api/v1/deployments`

기존 CI 제출과 같은 multipart 소스 형식, 필수 `app`, 필수 `target_id`를 사용한다. `Idempotency-Key`는 1–128자의 영문·숫자·`.`·`_`·`-`이며 principal/tenant·자원 종류 범위에서 유일하게 관리한다. 키는 유저 인증을 대체하지 않는다. 유저는 별도 Provider URL·Ansible operation·SSH 키를 보내지 않는다.

서버는 인증·대상 인가·기능·입력을 검사한 뒤 **비공개 source snapshot(또는 영속 저장소 참조), digest, 실행 의도를 함께 확정하고** 202를 반환한다. digest만 저장하고 업로드 bytes를 버리면 재시작 후 복구할 수 없다. 파일 저장·기록 확정에 실패하면 외부 실행을 시작하지 않는다. `Location: /api/v1/deployments/{id}`, `Retry-After: 2`, `X-Request-ID`와 화균 님의 접수 응답 형식을 사용한다. 이는 [HTTP 202의 접수 의미](https://www.rfc-editor.org/rfc/rfc9110.html#name-202-accepted)에 따른 설계다.

```json
{
  "resource_id": "dep001",
  "action": "create",
  "status": "accepted",
  "request_id": "9cd16c11-e380-4c48-8b97-d9dbdfbdf200"
}
```

접수의 `resource_id`와 상세 응답의 `id`는 제품 API가 만든 같은 deployment ID이며 CI run ID와 구분한다. GitHub dispatch 전이면 상세의 `ci.run_id`는 null이다. 준비되지 않은 target, 미구현 CD, DB가 필요한데 비밀 주입·egress 계약이 없는 앱은 실행 전에 차단한다. 현재 CD 지원 범위는 단일 stateless HTTP·amd64이며 이 범위를 임의 확장하지 않는다.

동일 키·동일 유저/target/app/소스 의미는 같은 기록을 반환한다. queued/running이면 같은 자원의 202 접수 응답, succeeded/failed/blocked/unknown이면 같은 자원 객체의 200 응답과 Location을 사용하며 HTTP request_id는 새로 생성한다. unknown은 재실행하지 않고 신규 접수 한도도 계속 점유한다. 소스 의미는 검증 후 정규화한 파일 경로·내용, 소스 종류와 repository URL(해당 시)로 정하며 multipart boundary나 ZIP 압축 시각으로 비교하지 않는다. 동일 키·다른 입력은 `409 IDEMPOTENCY_CONFLICT`다. GitHub URL 요청은 최초 해석한 upstream SHA·소스 snapshot을 저장하며 같은 키의 재요청에서 최신 HEAD로 다시 바꾸지 않는다. 각 외부 부작용 전후의 식별자를 저장하고 재시작 시 먼저 관측한다. 응답 유실은 unknown으로 남겨 중복 dispatch·VM 생성·설치를 막는다.

초기에는 백엔드의 한 worker가 영속 기록을 처리하고 기존 GitHub/Argo 조회를 재사용한다. 같은 키의 기존 기록을 먼저 반환한 뒤 신규 접수 가능 여부를 검사한다. 미완료 신규 작업은 처음에 한 건만 허용하고, 다른 신규 요청은 `409 EXECUTOR_BUSY`와 `Retry-After: 2`로 거부하며 202를 반환하지 않는다. 아직 재확인이 필요한 unknown 작업도 한도를 점유한다. 환경 준비 endpoint도 같은 접수 한도를 사용한다. 새 큐를 추가하지 않는다.

같은 유저·앱의 source ref 갱신은 직렬화한다. 다른 앱의 동시 갱신으로 CI checkout SHA가 달라지는 기존 차단도 유지한다. 재시작 시 저장한 원천 식별자로 관측부터 수행하고, 외부 부작용이 불확실한 작업을 자동 재실행하지 않는다.

### 4.4 제품 상태 — 신규 `GET /api/v1/deployments/{id}`

유저/tenant 소유권을 먼저 검사한다. 아래는 **CI 게시까지만 끝난 예시**다. 외부 시스템 원본 상태를 보존하고 화면용 상태는 근거로 계산한다.

```json
{
  "id": "dep001",
  "app": "demo-web",
  "target_id": "demo-aws",
  "status": "running",
  "stage": "cd",
  "ci": {"run_id": "123", "state": "published", "publication_artifact_id": "456", "producer_attempt": 1},
  "cd": {"state": "not_started", "revision": null, "deployed": false},
  "public_http": {"state": "not_run", "verified_at": null, "url": null},
  "error": null,
  "actions_url": "https://github.com/example/apps/actions/runs/123"
}
```

내부 기록에는 `tenant`, 업로드 소스 digest, 원본 저장소 SHA(해당 시), apps 저장소의 `source_commit`, 고정 `platform_ref`, CI run/producer attempt/bundle artifact ID/publication artifact ID, 게시 파일 hash·image digest, target 등록 판본, config Git revision과 Argo Application 식별자를 보존한다. 이 값은 단계들을 연결하는 식별자이며 각각을 성공 플래그로 사용하지 않는다.

| 전체 status | 사용 조건 |
|---|---|
| `queued` | 실행 의도 저장, 아직 시작하지 않음 |
| `running` | 필요한 단계가 진행 중 또는 다음 단계를 기다림 |
| `blocked` | 권한·설정·지원 범위·준비 조건 부족. 실행 가능한 것으로 표시하지 않음 |
| `failed` | 해당 단계의 실패가 관측됨 |
| `unknown` | 이미 시작한 외부 작업의 결과를 확정할 수 없음. 중복 실행 금지 |
| `succeeded` | 동일 source/target/digest의 게시, 고정 revision의 CD 적용, 기대 앱/버전의 공개 HTTP 확인까지 모두 충족 |

부분 완료는 `stage`와 원본 단계 결과로 표시한다. CI published, Ansible runtime_ready, Argo deployed를 전체 succeeded로 치환하지 않는다. 관측이 끊기면 마지막 성공 시각과 현재 unknown/stale을 함께 보여 준다. 등록된 앱 URL만 검사하고 요청 본문의 임의 URL을 probe하지 않는다. 현재 Blackbox의 2xx만으로 기대 앱/버전 확인까지 충족했다고 판단하지 않는다.

HTTP 오류는 신규 endpoint에서 화균 님과 같은 `error: {code, message, request_id, retryable, outcome_unknown}` 형식을 사용한다. 기존 `OperationError`의 분류·복구 의미를 이 형식으로 변환하고 raw exception/stdout/비밀을 노출하지 않는다. 작업 자체의 실패는 200 자원 응답의 `status=failed`, `stage`, 같은 필드의 `error`에 보존한다. 이 저장된 오류의 request_id는 해당 오류가 발생한 호출을 가리키며 현재 GET의 X-Request-ID와 구분한다. 아직 HTTP 호출 전 발생한 worker 오류에는 별도의 서버 생성 추적 ID를 기록한다. 기존 API의 문자열 `error`를 호환성 없이 변경하지 않는다. RFC 9457로의 전체 변경은 이번 범위에 넣지 않는다.

## 5. 환경 준비와 Slack 배치 변수 — 단계 3 제안

회의 요구는 웹/MCP 요청에서 CSP·온프렘별 배치 수를 Ansible로 전달하는 것이었다. AWS 3개·온프렘 2개는 예시이며 필드명·고정 기본값 합의가 아니다. [10/1 원문](https://softbankhackathon2026.slack.com/files/USLACKBOT/F0C63236BL1/___________________)의 2:49:49–2:51:07과 [회의 기록](../meetings/2026-10-01.md)을 따른다.

신규 `GET /api/v1/profiles`는 인가된 유저에게 `{items: [{id, label, provider, site, purposes, supported, blockers}], next_marker}`를 반환한다. `limit/marker`는 공통 목록 규칙을 따른다. 선택한 항목의 `id`를 계획 요청의 `profile_id`에 넣는다. `purposes`는 `runtime/database/dcs/proxy` 중 허용 역할이다. profile은 운영자가 등록한 계정/프로젝트·image/flavor/network 정책을 가리키며 실제 자격·파일 경로는 포함하지 않는다. provider가 있다는 이유만으로 모든 역할의 `supported`를 true로 만들지 않는다.

신규 `POST /api/v1/plans`는 위 목록에서 고른 profile과 유저의 배치 의도를 받는다. 각 profile의 유효성·역할·권한을 서버에서 다시 검사한다. 아래는 **후속 제안 형식**이며 현재 `ansible-job.schema.json`에 바로 보낼 수 없다.

```json
{
  "name": "demo-hybrid",
  "runtime": {"profile_id": "aws-runtime-small", "node_count": 1},
  "database": {
    "mode": "patroni",
    "placements": [
      {"profile_id": "aws-db", "database_nodes": 1, "dcs_voters": 2, "proxy_nodes": 1},
      {"profile_id": "onprem-db", "database_nodes": 1, "dcs_voters": 1, "proxy_nodes": 0}
    ]
  }
}
```

이 숫자는 입력 구조 설명용이며 두 site 장애 모두를 견디는 권장 HA 배치가 아니다. 담당 플레이북의 DB>=2·etcd>=3 홀수·proxy>=1 조건만 충족해도 사이트 장애·복제·라우팅 정책이 검증된 것은 아니다. `mode=none`은 DB 생략, `standalone`은 담당 설치 코드가 없으므로 실행 차단, `patroni`는 담당 HA 조건과 통신/비밀/인증서 정책까지 계획에 포함한다.

계획을 동기적으로 계산·저장한 뒤 `201 Created`, `Location: /api/v1/plans/{id}`, `X-Request-ID`와 계획 객체를 반환한다. 계획 자신의 식별자는 `id`이며 정규화한 입력, 담당별 실행 단계, 비용/권한/HA 제약, `executable`, `blockers`, 서버 정책 revision, 만료 시각을 포함한다. `GET /api/v1/plans/{id}`는 같은 소유권 검사를 거쳐 200과 같은 형식의 객체를 반환한다. 알려지지 않은 비용은 null이다. plan은 VM 생성이나 runtime 준비의 증거가 아니다. 현재 어댑터가 연결되기 전에는 `executable=false`로 표시한다. 계획 계산까지 비동기로 바꾸는 경우 별도로 접수·조회 계약을 정하고 201로 완료를 가장하지 않는다.

`POST /api/v1/environments`는 `{ "plan_id": "plan001" }`와 idempotency key를 받는다. 서버가 유저 권한·입력/정책 hash·만료·검토 상태를 재검사하고 영속 환경 ID로 접수한다. 202 본문은 공통 `resource_id/action/status/request_id`, Location은 `/api/v1/environments/{id}`다. Terraform은 해당 검토 saved plan, OpenStack은 해당 project의 등록된 사양으로 생성한다. plan과 다른 입력을 함께 보내는 우회 경로는 만들지 않는다. `GET /api/v1/environments/{id}`는 환경 자신의 `id`, 자원/guest/runtime/DB의 개별 상태, 결과 `runtime_target_id`, DB node 참조를 반환한다.

### 생산자 → 입력 → 소비자 매핑

| 생산자 | 필드/자료 | 소비자·처리 |
|---|---|---|
| 유저 | 환경 profile, runtime 수, DB mode·배치 수 | 제품 API가 권한·지원 기능·정책 검사 |
| 등록 profile | provider/site/account 또는 project, 허용 사양 | Provider 호출에 필요한 비공개 설정으로 해석 |
| Terraform / OpenStack | node_descriptor 또는 ACTIVE ServerResponse의 ID·project·주소 | 제품 backend가 현재 자원 확인 후 Ansible 등록 정보 생성 |
| 실행기 운영 설정 | SSH 참조·known_hosts·관리 경로 | Ansible에서만 결합. 브라우저로 전달하지 않음 |
| 제품 backend | runtime target, DB/DCS/proxy node 역할 | 기존 API `nodes/placements` 및 담당 inventory로 변환 |
| DB 담당 정책 | cluster_name, client_cidrs, 복제/백업 정책, 비밀·TLS 참조 | `db_nodes/etcd_nodes/proxy_nodes`와 실제 플레이북 변수에 결합 |
| Ansible/DB 실행기 | 단계 결과와 대상 identity·준비 검증 | 환경 record 갱신. 설치 exit 0만으로 서비스/HA 완료를 만들지 않음 |

현재 API는 `database/dcs` 두 그룹만 만들며 `proxy_nodes` 역할, `cluster_name`, `private_ip`, `client_cidrs`, 비밀/TLS 참조의 실제 DB 입력 매핑이 없다. `railshot_database`를 읽는 담당 플레이북도 없다. 이 변환을 검증한 뒤에만 501 실행 차단을 제거한다. 비밀·TLS 파일을 프론트 입력에 추가하는 방식으로 공백을 메우지 않는다.

현재 target registry는 시작 시 읽는 운영자 파일이다. 첫 통합은 등록 target 하나를 사용한다. 동적 환경 생성 단계에서는 서버가 Provider snapshot과 접속 설정을 원자적으로 등록/갱신하고 실행기가 안전하게 읽는 내부 계약을 먼저 추가해야 한다. 변경 시 진행 중 작업의 target snapshot을 바꾸지 않는다. 현재 파일 재시작 방식이 동적 등록 API처럼 동작한다고 가정하지 않는다.

## 6. 내부 인터페이스 재사용

| 내부 호출 | 기존 계약 | 제품 백엔드가 추가할 연결 |
|---|---|---|
| GitHub dispatch / run / artifact | `apps/api/src/github.js`, `published.js` | durable deployment에 CI 식별자를 결합. 게시 검증 로직 재사용 |
| Terraform plan/apply | `infrastructure/providers/terraform_tools/provision.py` | 등록 profile→검토 plan→환경 결과. VM 생성 HTTP 서버를 별도 복제하지 않음 |
| OpenStack | servers 생성/목록/상세/삭제/actions, images/flavors/networks 목록 | project 권한으로 호출, 202 뒤 상세 조회. 생성 timeout에 무조건 재전송 금지 |
| Ansible | `POST /v1/ansible/validate`, `POST /v1/ansible/jobs`, `GET /v1/ansible/jobs/{request_id}` | 등록 대상 해석·내부 인증·job ID 연결. 현재 localhost-only이므로 같은 실행 호스트 또는 인증된 내부 relay 배치 확정 필요 |
| DB | `infrastructure/ansible/playbooks` | 새 중복 DB 설치기 대신 입력 변환·호출·서비스 결과 확인 연결 |
| GitOps/Argo | `gitops/handoff.py`, `gitops/argo.py` | 게시 artifact→선언→검토 config commit→Argo sync/verify, 같은 revision의 결과 소비 |
| Edge / HTTP | `infrastructure/terraform/aws-edge`, 등록 route, 관측 설정 | 별도 관리자 준비 결과를 사용. DNS/LB/network 범용 CRUD API는 현재 제품에 추가하지 않음 |

GitOps 생성물 중 workload만 config repo 경로에 넣고 commit한 뒤 그 revision으로 Application/receipt를 확정한다. CI artifact는 외부 업로드 파일을 임의로 신뢰하지 않고 기존 GitHub artifact ID와 해시 검증을 통과한 것만 사용한다. CD 결과는 revision·target·이미지 일치까지 확인하며 공개 HTTP 결과는 별도 저장한다.

제품 API 공개 시 배포 유저 인증과 각 target·CI run·deployment·plan·environment 조회/실행 인가를 함께 구현한다. **기존 `POST /api/deploy`와 `GET /api/runs/{run_id}`에도 적용한다.** 현재 run 조회는 workflow 경로만 확인하므로, 공개 모드에서는 서버가 저장한 run→principal/tenant/app/target 바인딩을 검사해야 한다. 바인딩 없는 과거 run은 운영자 로컬 모드에서만 조회한다. 응답 형식을 보존하면서 접근 검사를 추가할 수 있다.

터널 연결이나 `x-jasmin-request` 헤더를 인증으로 취급하지 않는다. 공급자·Ansible 운영자 자격과 GitHub token은 서버 측에 둔다. 기존 CLI/MCP의 신뢰 로컬 모드는 공개 다중 유저 모드와 구분한다.

PR #10의 `apps/api/src/access.js`와 CLI/MCP token 전달은 내부 운영자 접근에 재사용할 수 있다. 이 코드를 별도 인증 서비스로 복제하지 않는다. 다만 공유 Bearer 비교로는 유저를 식별하거나 run 소유권을 검사할 수 없으므로 공개 제품 인증을 완료했다고 간주하지 않는다. `railshot.io`의 `/api` 전달과 인증 경계는 별도 배치 설계에서도 같은 조건을 유지한다.

## 7. 구현 파일과 인수 조건

| 순서 | 변경 위치 | 완료 조건 |
|---|---|---|
| 1. CI 자원 API·화면 연결 | `apps/dashboard/app.js`, `apps/api/src/server.js` | 기존 CI 로직을 재사용한 v1 라우트·응답 변환·소유권 바인딩, legacy 호환. 실제 화면으로 ZIP/폴더/GitHub 입력, 앱 이름·허용 target 선택, 202/Location과 run polling. UI에 CI 게시 범위 표시 |
| 2. target 조회 | `apps/api`의 현재 설정/서비스 생성 경계 | 허용 목록과 실제 제출 대상 일치. 타인 target 거부. 미구현 기능을 선택 불가로 표시 |
| 3. 제품 배포 record | `apps/api`, 기존 CI client | 소스 snapshot·의도 저장 실패 시 부작용 0, 동일 키 재접수 1건, 접수 한도 초과 거부, dispatch 응답 유실 뒤 중복 실행 0, 재시작 후 소스·상태 복구 |
| 4. CD 인계·상태 소비 | 제품 backend + 기존 `gitops` | 동일 이미지·target·revision 적용 확인, partial/unknown 처리, 공개 검사 전 전체 성공 금지 |
| 5. 환경/DB 연결 | 기존 Provider·Ansible 경계 | plan과 apply 입력 고정, ACTIVE→guest→runtime 구분, 등록 snapshot 유지, DB 필수 역할·변수 매핑·실패 복구 확인 |
| 6. 운영 공개 | 제품 인증·등록 정책·배치 설정 | 기존 CI endpoint를 포함한 인증·target/run 소유권·허용 origin/host·내부 접속 검증 후 배포. 임의 Provider URL/키/경로 입력 차단 |

단계 1–4는 mock GitHub·localhost HTTP·모의 Ansible/Argo로 부작용 없는 통합 시험을 먼저 만든다. 단계 5는 inventory parser와 실제 담당 플레이북의 preflight 입력 검사를 통과해야 한다. 실제 cloud/DB/HA/공개 HTTP 검증은 그 다음 별도 인수 결과로 기록한다. mock 통과나 이 설계 문서의 완성을 live 배포 성공으로 표시하지 않는다.

## 8. 검증 기록과 직접 근거

최초 구조도·브랜치 감사에 이어 화균 님의 router/schema/오류 처리/HTTP 테스트와 integration 6037d3a 후속 소스를 대조했다. 변경은 이 설계, [REST 컨벤션](conventions.md), [Ansible 명세](ansible.md)의 제품 경계, [AGENT.md](../../AGENT.md) 파일명·참조 정리이며 노션 인터페이스 문서에도 같은 계약을 반영한다. 실행 코드와 인프라는 변경하지 않는다.

최초 f35ffda 조사 중 Ansible 로컬 HTTP 테스트 2개와 제품 API 테스트 2개가 통과했다. 이 과거 테스트를 후속 Dashboard나 실제 Provider/Ansible/Argo 연결·공개 서비스 완료의 증거로 사용하지 않는다. 게시 전 후속 변경은 소스와 diff로 재확인했으며 이미지 빌드·서버 설치를 수행하지 않았다.

- 제품 라우트·입력: [server.js](../../apps/api/src/server.js), [github.js](../../apps/api/src/github.js), [client.js](../../apps/api/src/client.js).
- 현재 화면: [app.js](../../apps/dashboard/app.js), [index.html](../../apps/dashboard/index.html). 기존 동작 화면은 `a50dd1a:apps/dashboard/app.js`와 `e42cec9:apps/dashboard/app.js`에서 비교.
- Ansible: [api.py](../../infrastructure/ansible/api.py), [inputs.py](../../infrastructure/ansible/inputs.py), [HTTP schema](../../contracts/ansible-job.schema.json), [명세](ansible.md).
- DB 실제 요구: [입력 계약](deployment-inputs.md), [preflight](../../infrastructure/ansible/roles/preflight/tasks/validate_inputs.yml).
- 자원: [Terraform 도구](../../infrastructure/providers/terraform_tools/README.md), [OpenStack router](../../infrastructure/providers/openstack/src/control_plane/api/router.py).
- 게시/적용: [CI README](../../ci/README.md), [게시 계약](ci-publication.md), [Argo](../../gitops/argo.py), [GitOps 사용법](../../gitops/README.md).
- 관측: [범위와 제한](../../observability/README.md), [CI event 규약](../../ci/scripts/contract/observability.md).
- 브랜치 근거: [통합 규칙과 후속 병합 기록](../integration/gitflow.md). 문서에 남은 과거 시점은 현재 원격 SHA와 구분.
- PR #10 소스: [접근 설정](https://github.com/Jasmin-Softbank/Railshot/blob/3752adeb3fcb0557480d323c854b5e9d7e085f61/apps/api/src/access.js), [정적 Nginx](https://github.com/Jasmin-Softbank/Railshot/blob/3752adeb3fcb0557480d323c854b5e9d7e085f61/apps/dashboard/nginx.conf), [배치 문서](https://github.com/Jasmin-Softbank/Railshot/blob/3752adeb3fcb0557480d323c854b5e9d7e085f61/docs/architecture/container-deployment.md).
- 최신 관측 브랜치: [standalone Cilium 실행](https://github.com/Jasmin-Softbank/Railshot/blob/80dae724722ec41fdd82b6265e98fd7d5f7921ca/infrastructure/ansible/site.yml#L186), 통합 재사용 기준 [runtime.yml](../../infrastructure/ansible/runtime.yml), [Cilium 설치기](../../deployment/cilium/install.sh).

미결정 사항은 새 환경 생성의 최초 지원 provider/profile, DB 사이트 장애 정책, 공개 인증 방식, 실제 서비스 배치와 내부 접속 방법이다. 기존 등록 대상으로 CI 게시를 사용하는 것과 v1·전체 앱 배포·환경 자동 준비를 구현하는 것은 구분한다.
