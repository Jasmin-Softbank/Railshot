# Dashboard → 배포 준비 → Deployment Runtime 계약 점검

**설치 스크립트 연결은 존재하지만 Dashboard payload(요청 데이터)를 Runtime JSON으로 바로 전달할 수는 없습니다.** 최신 integration `dec1251`에는 대상 등록 helper(등록 연결 도구)도 들어왔습니다. 다만 환경 실행 API는 아직 이를 호출하지 않으며 `deployment_supported=false`를 반환합니다. 이를 바꾸는 앱·DB 통합 브랜치가 진행 중이므로 중복 adapter(입력 변환 계층)를 작성하지 않았습니다.

이 보고서는 코드와 로컬 검사 결과입니다. 운영 중인 Dashboard·클라우드·노드에서 E2E(처음부터 끝까지) 배포를 실행한 결과가 아닙니다. `S1`이라는 이름의 배포 API/함수는 조사한 Dashboard/API/워크플로 코드에 없습니다. 팀이 S1이라고 부르는 단계가 CI 시작인지 환경 준비인지 확인이 필요하며, 아래에서 두 경로를 구분합니다.

## A. 최신 구현 위치와 통합 상태

저장소는 Jasmin-Softbank/Railshot, origin은 https://github.com/Jasmin-Softbank/Railshot.git, 작업 브랜치는 feature/deployment-runtime-seungmin으로 확인했습니다. 시작 HEAD는 `5f893ff2cf991561698524fe0d76cabcd2c07323`이며 시작 working tree(미커밋 변경 상태)는 깨끗했습니다. git fetch origin --prune 후 모든 원격 heads도 조회했습니다. 다른 저장소 조회·원격 push·브랜치 checkout·merge·rebase·PR merge는 수행하지 않았습니다.

최초 integration 조회는 `b9cfd2969b261d1279aae927bbb5886209716c62`, 최종 분석/검사 대상은 **`dec1251af69e8690a0a620243dccf7a42f857aee`**입니다. 현재 feature가 최신 integration을 포함하지는 않습니다. 이번 요청대로 upstream(다른 브랜치의 코드)은 임시 snapshot(고정된 복사본)에서 읽기 전용으로 검사했으며 feature에 가져오지 않았습니다. JSON adapter/models/engine/runtime 및 schema는 현재 feature와 최종 integration이 동일합니다.

| 구현 | 확인한 HEAD | 최종 integration 포함 | 근거 |
| --- | --- | --- | --- |
| feature/dashboard-ui | a173c8d | 예 | ancestry(커밋 포함 관계), PR #20/#23 통합 이력 |
| feat/observability-ui-20261002 | 8185559 | 예 | PR #25, 현재 UI 상태/관측 변경 |
| feature/dispatch-actions | d730562 | 아니오 | 등록 앱 source_type=registered 재실행 변경; 해당 브랜치 github.js:131의 redeploy→dispatch |
| feat/runtime-target-registration-20261002 | ad5b49b | 예 | 점검 도중 PR #27로 통합; deployment/scripts/environment.py register 추가 |
| feature/db-app-stack-20261002 | 9a05ba6 | 아니오 | PR #28, 환경 준비→등록→앱 CI 연결과 DB 변경이 같은 작업에 존재 |
| feat/app-domain-routing-20261002 | dbe20af | 아니오 | PR #26, 공개 경로 준비 변경 |
| feat/runtime-observation-registration-20261002 | 09a7f79 | 아니오 | 관측 등록 추가 변경; 읽기만 수행 |

코드 근거는 아래 고정 SHA 링크를 사용합니다. 로컬 apps 파일은 최신 integration과 다르므로 그것을 최신 UI 근거로 제시하지 않습니다.

## B. 실제 호출 흐름

**기존 대상의 앱 배포**:

```text
GitHub URL / ZIP / 폴더 + app + target_id
→ multipart POST /api/v1/deployments (또는 /builds)
→ uploadedSource → createDeployment → service.deploy
→ GitHub에 앱 소스 등록 및 workflow dispatch
→ CI loop → release → 검증된 publication(게시 산출물)
→ deployPublished → gitops/bridge.py → Argo 적용/공개 HTTP 검증
→ 제품 실행 상태 polling(반복 조회)
```

[Dashboard app.js:264](https://github.com/Jasmin-Softbank/Railshot/blob/dec1251af69e8690a0a620243dccf7a42f857aee/apps/dashboard/app.js#L264)에서 FormData를 만들며 필드는 app·target_id·repository_url 또는 archive/files/paths입니다. [server.js:42](https://github.com/Jasmin-Softbank/Railshot/blob/dec1251af69e8690a0a620243dccf7a42f857aee/apps/api/src/server.js#L42)는 허용 필드와 소스 하나 조건을 검사합니다. [github.js:105](https://github.com/Jasmin-Softbank/Railshot/blob/dec1251af69e8690a0a620243dccf7a42f857aee/apps/api/src/github.js#L105)는 tenant/app/source_commit/target_id로 dispatch합니다. [ci/workflows/railshot-deploy.yml:1](https://github.com/Jasmin-Softbank/Railshot/blob/dec1251af69e8690a0a620243dccf7a42f857aee/ci/workflows/railshot-deploy.yml#L1)은 다른 앱 저장소에 설치할 소스 템플릿입니다. 대상 저장소를 Railshot 하나로 제한해 실제 다른 저장소의 설치 상태는 확인하지 않았습니다.

[product.js:127](https://github.com/Jasmin-Softbank/Railshot/blob/dec1251af69e8690a0a620243dccf7a42f857aee/apps/api/src/product.js#L127)은 publication 검증 뒤 CD(이미지를 앱으로 적용하는 단계)를 호출합니다. [cd.js:77](https://github.com/Jasmin-Softbank/Railshot/blob/dec1251af69e8690a0a620243dccf7a42f857aee/apps/api/src/cd.js#L77)은 action/deployment_id/target_id/config_sha256/publication/files 계약으로 gitops/bridge.py를 실행합니다. 이 경로는 deploy.sh를 호출하지 않습니다.

**새 실행 환경 준비**:

```text
GET /api/v1/profiles → 운영자가 등록한 사양 선택
→ POST /api/v1/plans
→ 저장된 계획 검토
→ POST /api/v1/environments {plan_id} + Idempotency-Key
→ environmentAdapter.execute
→ provision.py apply → node_descriptor
→ run.py --validate-only → guest.check → runtime.install
→ runtime.yml → 공통 bootstrap/K3s/Cilium/health shell scripts
→ runtime_ready receipt(준비 확인 결과)
```

[app.js:397](https://github.com/Jasmin-Softbank/Railshot/blob/dec1251af69e8690a0a620243dccf7a42f857aee/apps/dashboard/app.js#L397)은 `{name,runtime:{profile_id,node_count:1},database:{mode:"none"}}`을 보냅니다. provider/CPU/메모리/디스크/SSH를 사용자가 자유 입력하지 않습니다. provider는 profile(운영자가 등록한 사양)에서 결정됩니다. [app.js:416](https://github.com/Jasmin-Softbank/Railshot/blob/dec1251af69e8690a0a620243dccf7a42f857aee/apps/dashboard/app.js#L416)은 plan_id만 실행 요청으로 전달합니다.

[environments.js:211](https://github.com/Jasmin-Softbank/Railshot/blob/dec1251af69e8690a0a620243dccf7a42f857aee/apps/api/src/environments.js#L211)은 provider 실행 결과를 descriptor로 받아 private registry(비공개 대상 등록 파일)에 저장합니다. [run.py:404](https://github.com/Jasmin-Softbank/Railshot/blob/dec1251af69e8690a0a620243dccf7a42f857aee/infrastructure/ansible/run.py#L404)가 AWS/GCP x86_64 descriptor를 Ansible 계약으로 바꿉니다. [runtime.yml:70](https://github.com/Jasmin-Softbank/Railshot/blob/dec1251af69e8690a0a620243dccf7a42f857aee/infrastructure/ansible/runtime.yml#L70)은 팀 설치 스크립트를 복사하고 노드 전체 lock(동시 설치 방지) 아래서 실행합니다. **runtime.py/DeploymentSpec/Engine/샘플 앱을 호출하는 경로는 아닙니다.**

이 작업에서 위 provisioning/Ansible/Argo 명령을 실제 실행하지 않았습니다. API 검사에서는 cloud runner·GitHub·CD를 mock(가짜 실행부)으로 대체했습니다.

## C–D. Dashboard → Runtime 계약 대조

| 항목 | Dashboard/제품 API | 현재 Runtime JSON | 판단 |
| --- | --- | --- | --- |
| 요청 형식 | 앱은 multipart, 계획/환경은 JSON | 노드 로컬 JSON, schema_version 0.1/0.2 | 바로 전달 불가; 서로 다른 계층의 계약 |
| GitHub URL | repository_url; API가 소스를 가져와 CI에 전달 | URL 필드 없음, workload.image 필수 | 이미지 게시 완료 후 검증된 digest(이미지 고유 해시)를 인계해야 합니다. |
| 앱 이름 | app 또는 계획 name; 앱 이름 3~30자 | workload.namespace·environment_id; 각각 최대 63자 | 자동 동일시 금지; namespace/앱 소유권 정책 필요 |
| 실행 대상 | target_id, 또는 profile_id→node_descriptor | node.host, provider, 선택 ssh_user | 등록 대상 조회 adapter 필요; 현재 Ansible descriptor adapter는 존재합니다. |
| Provider(인프라 제공 환경) | 환경 계획은 profile 기반 AWS/GCP만 실행 가능 | aws/gcp/openstack | core는 OpenStack 입력을 허용하지만 제품 환경 준비에서는 차단됩니다. |
| 온프레 표기 | on-prem이라는 자유 입력 필드 없음 | 정확한 값 openstack 필요 | UI 표시 이름을 core 값으로 그대로 보내면 거부됩니다. |
| 환경 사양 | profile_id, node_count=1; profile이 실제 VM 사양 결정 | 인프라 사양·profile·node_count 없음 | Runtime에 넣지 않습니다. Provider 계층의 책임입니다. |
| CPU 구조 | descriptor x86_64→Ansible amd64 | Runtime은 대상 Linux에서 실제 구조 검사 | descriptor adapter는 AWS/GCP amd64 제한. core 능력과 별도입니다. |
| 앱 이미지·프로브 | 최초 소스 요청에는 image/port/health_path 없음 | workload.image/namespace 필수, 나머지 기본값 | 소스 요청 시점에는 누락이 정상입니다. 게시 산출물/앱 명세/등록 정책으로 확정해야 합니다. |
| NodePort(노드 앱 접속 포트) | CD 등록 target에 있음 | exposure.node_port | 등록 정책·포트 충돌 검사 뒤 사용. UI 요청에서 임의 추정 금지 |
| timeout(대기 시간) | profile.timeout_seconds는 Ansible 전체 기한; playbook wait_timeout_seconds 기본 300 | runtime.timeout_seconds 기본 180, 허용 5..900 | 서로 범위·단위 역할이 다릅니다. cold install(최초 설치) 대기 설정을 합의해야 합니다. |
| online/offline/auto | 환경 준비 API가 이 선택/Bundle을 전달하지 않음 | 0.2 JSON/CLI에서 지원 경로 존재 | 현재 환경 준비 경로는 shell online 설치; core의 network preflight/fallback을 실행했다고 주장할 수 없습니다. |
| 준비 성공 | succeeded, guest_ready/runtime_ready | ready, cluster_status/cilium_status/workload_status | bootstrap 성공과 앱/HTTP 성공을 구분해야 합니다. receipt를 DeploymentResult로 취급할 수 없습니다. |
| 단계·오류 | resources/guest/runtime, 제품 error/outcome_unknown | NODE_READY/K3S_READY/... 및 error.stage/code, network_states | 직접 표시 불가; 상태 변환 계약 필요. 0.2 추가 필드는 별도 보존 필요 |
| URL/health | 제품은 url/public_http, 공개 응답 검증 뒤 succeeded | endpoint/endpoint_scope, node-local HTTP | 노드 내부 HTTP가 공개 URL 성공을 뜻하지 않습니다. |
| 원격 실행 | 상위 계층의 승인된 SSH/IAP/SSM | core는 실행 중인 Linux를 변경; node.host는 context | node.host만 넣으면 원격 deploy되는 것은 아닙니다. 전송/접속은 상위 실행 계층 책임입니다. |
| 완료 후 앱 등록 | 환경 API는 deployment_supported=false | core에 CI/Argo/제품 대상 등록 기능 없음 | integration에 등록 helper가 있지만 환경 API 호출이 아직 연결되지 않았습니다. |

기존 경계는 [input_adapter.py](../../input_adapter.py), [models.py](../../models.py), [runtime.py](../../runtime.py)로 확인했습니다. provider·node_host·ssh_user는 RequestContext(요청 문맥)에 있으며 DeploymentSpec에는 provider/SSH가 없습니다. 기본 enum(허용 값 목록)을 제품의 queued/running/succeeded/blocked/unknown으로 바꾸는 수정은 하지 않았습니다.

### 확인된 연결 공백과 정확한 책임 위치

- **환경 완료 후 등록 호출 공백:** [environments.js:219](https://github.com/Jasmin-Softbank/Railshot/blob/dec1251af69e8690a0a620243dccf7a42f857aee/apps/api/src/environments.js#L219)은 deployment_supported=false로 시작하며 [258행](https://github.com/Jasmin-Softbank/Railshot/blob/dec1251af69e8690a0a620243dccf7a42f857aee/apps/api/src/environments.js#L258)에서 runtime 성공 후 반환합니다. environment.py 호출은 없습니다. 새 helper의 API 연동 공백이지 core JSON parser 결함이 아닙니다.
- **후속 구현 존재:** 아직 미통합인 `9a05ba6`의 [environments.js:357](https://github.com/Jasmin-Softbank/Railshot/blob/9a05ba64e98a3517d6e69e0b67e7356fdeca0b30/apps/api/src/environments.js#L357)은 `environment.py register`를 호출하고, [product.js:185](https://github.com/Jasmin-Softbank/Railshot/blob/9a05ba64e98a3517d6e69e0b67e7356fdeca0b30/apps/api/src/product.js#L185)는 준비된 환경 뒤 CI를 실행합니다. DB 변경과 함께 진행 중이므로 임의 cherry-pick/복사/편집하지 않았습니다.
- **등록 도구 자체:** 최종 integration의 [environment.py:394](https://github.com/Jasmin-Softbank/Railshot/blob/dec1251af69e8690a0a620243dccf7a42f857aee/deployment/scripts/environment.py#L394)은 register/registry/target-id/config/state-dir 계약입니다. deploy.sh JSON과 다르며, 자체적으로 VM 생성·K3s 설치·앱 배포 성공을 수행하거나 선언하지 않습니다.
- **온프레 제품 차단:** [environments.js:128](https://github.com/Jasmin-Softbank/Railshot/blob/dec1251af69e8690a0a620243dccf7a42f857aee/apps/api/src/environments.js#L128), [run.py:412](https://github.com/Jasmin-Softbank/Railshot/blob/dec1251af69e8690a0a620243dccf7a42f857aee/infrastructure/ansible/run.py#L412). core OpenStack 지원과 제품의 온프레 E2E 지원을 구분해야 합니다. 다른 담당자의 provider/API 코드는 수정하지 않았습니다.

## E. 변경 파일

직접 변경은 deployment/ 아래 새 fixture, 경계 검사 6개, 이 보고서, 구조화 결과, 실행 로그뿐입니다. core 구현·Dashboard/UI·API·DB·MCP/AI·Observability·CI·Provider를 수정하지 않았습니다. 현재 정보로 core 영역에 고칠 단순 이름/타입 오류는 확인되지 않았습니다. 해결이 필요한 부분은 상위 orchestration(여러 단계를 연결하는 실행 조정)과 진행 중인 팀 통합입니다.

## F. 실제 검사 결과

| 대상 | PASS | FAIL | SKIP | 범위 |
| --- | ---: | ---: | ---: | --- |
| 현재 feature Runtime 전체 + 새 계약 검사 | 58 | 0 | 1 | 59개; 새 검사 6개 포함, schema 검사 실행 |
| 최종 integration dec1251 Runtime 전체 | 65 | 0 | 1 | 66개; 새 등록 helper 검사 포함, 별도 임시 snapshot |
| integration 앱 API 관련 검사 | 45 | 0 | 0 | environments/product/CD/deployment, cloud/GitHub/CD mock |
| integration Ansible 전체 unit | 55 | 0 | 2 | 57개; 기본 입력/descriptor/Cilium/접속 경계, cloud 접속 없음 |
| integration CI publication 계약 | 4 | 0 | 0 | 등록 target/app/tenant/pull binding(게시 대상 연결) |
| feature shell 문법 | 20개 통과 | 0 | 0 | bash -n |
| integration shell 문법 | 19개 통과 | 0 | 0 | bootstrap/원래 설치기 제거 차이 포함; 실제 Linux 실행 아님 |

같은 core 검사를 두 버전에서 반복한 숫자이므로 고유 검사 수로 합산하지 않습니다. Runtime의 SKIP은 native Argo CRD(실제 확장 자원 형식) 입력 미준비이며, Ansible의 SKIP 2개는 native playbook/Vault 검사 환경 미준비입니다. Docker/실제 Actions/브라우저/클라우드 E2E는 실행하지 않았습니다.

최초 integration b9cfd29 검사에서 release 검사 2개가 하위 python3의 검증 환경 미사용으로 실패했습니다. 기본 python3에는 yaml이 없고 기존 임시 venv(격리된 Python 환경)에는 PyYAML 6.0.3이 있음을 확인했습니다. **소스 수정 없이 subprocess PATH를 같은 venv로 지정**해 재실행한 b9cfd29/dec1251은 통과했습니다. 처음 실패한 로그도 보존합니다.

새 [fixture](../fixtures/dashboard-handoff.json)는 실제 필드 형태에 가상 값을 사용합니다. resolved_runtime_input은 운영자 값으로 채운 **시험 예시**이며 production converter(실제 입력 변환기)가 아닙니다. 새 [검사](../test_dashboard_handoff.py)는 raw 제품 payload·descriptor를 실행 전에 거부하는지, 완성된 입력/결과 schema와 context 분리가 유지되는지 검사합니다. CLI 성공 출력은 Engine mock이며 mock의 workload_status=unknown/endpoint=null도 확인합니다. 실제 노드가 ready라고 기록하지 않습니다.

명령과 결과 로그는 [review/summary.json](dashboard-runtime-review/summary.json)에 기록했습니다. 테스트는 현재 feature와 읽기 전용 integration snapshot에서 실행했으며 운영 저장소나 클라우드에 write하지 않았습니다. 일부 integration release 테스트가 사용하는 Git 저장소/remote는 /tmp의 새 가짜 저장소입니다. Railshot의 main/타 브랜치를 쓰거나 push하는 것이 아닙니다.

## G. 팀 확인 사항

1. S1의 정확한 정의: 최초 CI 검사인지 환경 resources/guest/runtime 준비인지 확인해야 합니다.
2. 운영 경로는 기존 Ansible bootstrap + GitOps CD를 유지할지, standalone deploy.sh를 별도 시험 경로로 유지할지 결정해야 합니다. 두 경로를 같은 완료 계약으로 합치지 않습니다.
3. PR #28의 API 연결, 이미 통합된 등록 helper, PR #26 공개 경로 변경의 조립 순서와 담당자를 확인해야 합니다. Observability 등록은 별도 책임이며 본 작업에서는 변경하지 않습니다.
4. 앱 명세의 image digest/port/health/namespace/NodePort/registry Secret을 어느 계층에서 확정하는지, 상태·URL 의미를 어떻게 제품 API에 표시할지 합의해야 합니다.
5. 온프레 profile/descriptor/transport(접속 방식)의 제품 연결과 지원 범위, amd64 제한, timeout과 online/offline 전달 정책이 필요합니다.

## H. E2E를 위한 최소 잔여 작업

팀이 진행 중인 환경→등록→CI/CD 호출을 먼저 검토·통합해야 합니다. 이후 승인된 operator profile(운영 설정), SSH 신뢰 정보, registry pull/CI/Argo 설정, 공개 경로를 준비하고 고정 source/플랫폼 revision(코드 버전)으로 실행해야 합니다. 새 환경에서 resources→guest→runtime→registration→CI publication→CD→공개 HTTP를 각각 관측하고 같은 environment/target/app/source/image/revision으로 연결되는 증거를 수집해야 합니다.

환경 준비 성공만으로 앱·공개 URL 성공을 표시하면 안 됩니다. 기존 앱의 공개 HTTP나 과거 Lima 모의 성공을 새 환경 E2E의 증거로 대신하지 않습니다. 이번 작업에서는 실제 AWS/GCP 자원 생성·삭제, firewall/IAM/VPC/VM 변경, 기존 노드 workload/cleanup을 수행하지 않았습니다. 변경 파일은 현재 feature에서 검토할 수 있도록 두었으며 커밋·push·PR 생성은 수행하지 않았습니다.
