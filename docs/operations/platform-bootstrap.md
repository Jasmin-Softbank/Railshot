# 운영 플랫폼 bootstrap 진입점

`deployment/scripts/bootstrap-platform.py`는 현재 등록된 AWS control/build 구성을 인수하는 관리자 명령이다. 최초 계정 자격과 아래 private 입력을 준비하면 Terraform, 고정 SSM/SSH 명령, 기존 Ansible, Kubernetes 선언, 이미지 게시, GitOps, 실제 rollout 검증을 한 프로세스에서 연결한다. 운영 실행 검증 전인 소스이며 로컬 테스트 결과를 운영 설치 완료로 해석하지 않는다.

```sh
python3 deployment/scripts/bootstrap-platform.py --config /private/bootstrap.json --validate-only
python3 deployment/scripts/bootstrap-platform.py --config /private/bootstrap.json
```

`--validate-only`는 입력 파일만 검증한다. Terraform plan, 클라우드 접근, SSH, GitHub 호출을 하지 않는다. 실행에는 Python 3.12+, 기존 PyYAML pin, Terraform, AWS CLI/session-manager-plugin, OpenSSH, Git, `gh`가 필요하다. 정확한 게시 source SHA의 깨끗한 checkout에서 실행한다. AWS와 GitHub 자격은 처음 인증한 실행자의 native CLI 경로를 사용하며 고객 빌드 컨테이너에 복사하지 않는다.

범위는 계정 `721622471953`, 서울 리전, 기존 control `i-033ae2db907fde68e`와 명시한 build 인스턴스다. 두 인스턴스는 실행 중이고 SSM Online이어야 한다. 기존 root volume·Terraform lineage·node UID·절대 STOP 기한을 보존한다. 새로운 control ID, 자동 start/stop, CNI 전환, AMI 갱신, VM 교체, destroy는 지원하지 않는다. 다른 계정이나 새 control을 설치하려면 verifier의 고정 IAM/instance binding도 별도로 검토해야 한다. GCP 신규 provisioning과 Cloudflare 설정은 이 AWS bootstrap 범위 밖이며 해당 계정 자격이 없으면 blocked다. 과거 실행 기록에서 자격을 찾지 않는다.

## 한 번 준비하는 입력

모든 private JSON·자격 파일은 실행자 소유 0600, 상태 디렉터리는 0700으로 둔다. 다음은 **필드 구조 예시**이며 대문자 placeholder를 실제 인수 자료로 바꿔야 한다. 토큰 원문은 JSON에 쓰지 않고 파일을 참조한다.

```json
{
  "version": 1,
  "source": {"ref": "PUBLISHED_40_HEX_SHA", "archive_sha256": "CODELOAD_ARCHIVE_64_HEX"},
  "state_dir": "/private/platform-bootstrap",
  "aws": {"account_id": "721622471953", "region": "ap-northeast-2"},
  "terraform": {
    "control": {"state": "/private/control/terraform.tfstate", "variables": "/private/control/inputs.tfvars.json", "lineage": "EXISTING_LINEAGE"},
    "ci": {"state": "/private/ci/terraform.tfstate", "variables": "/private/ci/inputs.tfvars.json", "lineage": "EXISTING_LINEAGE"},
    "platform-verification": {"state": "/private/verifier/terraform.tfstate", "variables": "/private/verifier/inputs.tfvars.json", "lineage": null}
  },
  "control": {
    "instance_id": "i-033ae2db907fde68e", "private_ip": "172.31.0.172", "root_volume_id": "vol-EXISTING",
    "node_name": "ip-172-31-0-172", "node_uid": "EXISTING_NODE_UID", "stop_at": null,
    "ssh": {"identity_file": "/private/operator-key", "known_hosts_file": "/private/known_hosts"}
  },
  "build": {
    "instance_id": "i-09955d23ad1d8dbe2", "private_ip": "EXISTING_PRIVATE_IP", "root_volume_id": "vol-EXISTING",
    "node_name": "railshot-build-worker-aws-01", "node_uid": "EXISTING_NODE_UID_OR_NULL_BEFORE_FIRST_JOIN", "stop_at": "2026-10-05T14:59:00Z",
    "ssh": {"identity_file": "/private/operator-key", "known_hosts_file": "/private/known_hosts"}
  },
  "github": {"ref": "integration/team-assembly-20261002", "target_id": "k3s-aws", "node_port": 31080, "application": "USER_APP_ARGO_APPLICATION"},
  "secrets": {
    "api_token": "/private/api-token", "github_token": "/private/github-token", "pull_config": "/private/ghcr-pull.json",
    "executors": "/private/executors.json", "controller_github_token": "/private/runner-controller-token"
  },
  "registration": {"documents": "/private/registration.json", "credentials_policy": "/private/credentials-policy.json"},
  "adopted_uids": {"Namespace//railshot-system": "EXISTING_NAMESPACE_UID"},
  "import_package": null
}
```

- `terraform.*`는 기존 authoritative backend를 가리킨다. state를 복제해 같은 자원에 두 writer를 만들지 않는다. verifier만 첫 생성 전에 lineage가 null일 수 있으며 생성 후 관측한 lineage는 bootstrap 상태에 고정된다. 기존 verifier를 인수할 때는 그 lineage를 입력한다. CI tfvars의 source/user-data/STOP 값을 새 release SHA로 덮어쓰지 않는다. 코드는 별도 pinned archive로 배포한다. 현재 CI의 public-IP replacement drift가 남아 있으면 apply는 거부된다.
- `adopted_uids`는 인수할 기존 객체의 `Kind/namespace/name → UID` 목록이다. namespace처럼 cluster-scoped 객체는 `Namespace//name`이다. 자동 발견으로 소유권을 주장하지 않는다. 새 객체의 UID는 즉시 저장한다. 생성 응답 유실은 해당 bootstrap binding annotation을 가진 객체의 현재 readback으로만 인수한다.
- `executors.json`은 Secret key→파일 내용 문자열 mapping이다. `cd.json`, projected SA token 파일을 참조하는 `kubeconfig`가 필수이며 `observer.json`, `profiles.json`, `registration.json`, `edge.json`만 추가 허용한다. 기존 `prepare-state.js`가 이를 PVC `/var/lib/railshot/config`로 복사한다. 기존 Secret의 key나 값을 바꾸지 않는다. DB 프로필은 별도 이관 파일 `/var/lib/railshot/config/app-db/profiles.json`이며 이관 검증 후에만 `railshot-environments` ConfigMap의 `profiles_file`에 이 고정 경로를 설정한다. 이관된 deployment에 관측 등록 설정이 있으면 ConfigMap의 `observer_file`도 등록기의 `state_dir/product.json`으로 연결한다. 이때 초기화는 Secret의 `observer.json`을 더 이상 복사하지 않는다. 두 ConfigMap 경로는 최초 추가 후 변경을 거부하며 기존의 다른 key를 보존한다. API manifest의 optional ConfigMap 참조가 이후 release에도 유지되므로 반복 수동 env 수정이 필요하지 않다. 프로필·budget·edge 실행 권한을 자동으로 추정하지 않는다.
- `registration.documents`가 가리키는 `registration.json`은 담당자가 검토한 Kubernetes `List`다. 최초 registrar 권한은 `deployment/manifests/runtime-registration-access.yaml`의 SA 및 고정 bootstrap Role/Binding, 빈 `argocd/railshot-product-registrations` Role/Binding을 이 List에 포함한다. 기존 target의 AppProject/Application/Argo cluster Secret도 같은 List로 전달할 수 있다. 지정한 빈 registrations Role은 부재할 때만 native create하며, 재실행에는 `environment.grant_control_objects`가 추가한 `argoproj.io/{appprojects,applications}` 및 core `secrets`의 정확한 이름에 대한 `get,patch` 규칙만 허용한다. 중복 kind·wildcard·추가 권한을 거부하고 UID와 전체 rules를 다시 읽은 뒤 기존 이름을 보존한다. 다른 기존 등록 객체는 입력과 readback이 다르면 덮어쓰지 않는다. 고객 VM의 runtime/DB 설치 자체는 이 입력으로 대신하지 않는다. 신규 고객 runtime 등록은 기존 `deployment/scripts/environment.py`의 별도 계약을 사용한다.
- `credentials-policy.json`은 기존 `gitops/credentials.py` 정책이다. 실제 고객 SA UID, CA hash, audience, namespace를 담는다. 이미 등록된 더 많은 갱신 대상은 재실행 시 제거하지 않는다. 만료된 자격을 bootstrap이 추측해 복구하지 않는다.
- 기존 ALB/Route53/ACM/WireGuard 경로와 `railshot.io`는 edge owner가 준비한 권위 설정을 사용한다. 이 명령은 aws-edge를 별도 state에서 다시 생성하지 않는다. 공개 HTTPS 검증이 실패하면 전체 성공을 반환하지 않는다.
- control tfvars의 `enable_product_executor:true`는 검토된 기존 instance role의 제품 실행 정책과 IMDSv2 hop limit 2를 요청한다. 이 모드에서는 현재 API Deployment UID와 기존 CCNP가 있으면 그 UID도 `adopted_uids`에 넣는다. 이미 실행 중인 API·dashboard·Argo·DNS·local-path Pod와 Cilium 1.20.2가 필요하다. 다른 cloud 계정이나 새로운 플랫폼 identity를 자동 인수하는 모드가 아니다.

## 실행과 재실행

고정 순서는 identity 검사 → control/CI/verifier Terraform saved plan → 기존 control 프로필 확인 및 platform 배치 → build 가입/Ready/Cilium 확인 → build controller 중지·실행 중 runner 부재 확인 → `ci.yml`·실제 네트워크 검사·`prepare-host.sh` → namespace/Secret/RBAC → 이미지 게시 → 선택적 private state 이관 → controller/credentials 선언·제한 SA 실제 Job → 플랫폼 Application → 기존 publisher → 실제 revision/digest/HTTPS 검증이다.

제품 executor opt-in 시에는 control Terraform **전에** 고정 `product-metadata.yaml` CCNP를 적용하고 UID 및 최상위 specs 전체를 읽어 대조한다. 원하는 specs의 SHA256과 규칙 번호를 각 native `spec.labels`에 넣으며, agent가 반환한 같은 UID의 내부 규칙이 정확히 다섯 개이고 모두 같은 해시와 서로 다른 번호를 가질 때만 해당 revision을 기다린다. Cilium 1.20.2의 [Kubernetes 규칙 변환](https://github.com/cilium/cilium/blob/v1.20.2/pkg/k8s/apis/cilium.io/utils/utils.go)과 [내부 PolicyEntry 변환](https://github.com/cilium/cilium/blob/v1.20.2/pkg/policy/utils/parserules.go)이 이 labels 전달 및 egressDeny 항목별 한 규칙 변환의 근거다. 같은 CCNP UID에 이전 specs가 남은 상태는 성공으로 취급하지 않는다. 실제 endpoint의 realized revision을 확인한 뒤 Argo/platform/DNS/local-path의 대표 Pod마다 READY CRI sandbox UID·IP·PID를 검증한다. 그 network namespace에서 고정 metadata 주소로 요청하고, Cilium monitor의 실제 policy denied DROP을 endpoint ID·security identity·Pod IP·목적지 `169.254.169.254:80`과 대조한다. HTTP timeout·TTL exceeded는 차단 증거가 아니다. 선택된 hostNetwork Pod는 중단 조건이다.

control Terraform 직후에는 deny 검사만 반복하여 API replica 0 동결을 유지한다. 이관과 마지막 플랫폼 release가 끝난 뒤 `metadata-final` 단계에서 deny 검사를 다시 수행하고, 인수한 API Deployment의 소유 Ready Pod 안에서 IMDSv2 instance ID·role과 AWS CLI STS account/assumed-role session을 실제 대조한다. static key/profile/다른 자격 경로가 있으면 중단한다. token/credential 원문이나 전체 packet event는 출력·저장하지 않으며 receipt에는 검증된 identity와 정책/Pod UID·revision·deny 이유만 남긴다. IPv4 요청으로 실제 경로를 검증하며 IPv6 metadata deny 선언도 정확히 보존한다.

control 단계는 Argo의 strict RBAC 설정과 `argocd-cmd-params-cm`의 `controller.resource.health.persist=true`를 확인한다. 후자는 이전 migration Job의 개별 health를 Application 상태에서 검증하기 위한 설정이다. ConfigMap UID/resourceVersion을 application-controller Pod template annotation에 고정하여 rollout을 유도하고 항상 완료를 기다린다. ConfigMap patch 직후 중단되어도 재실행이 이전 Pod를 정상 적용으로 오인하지 않는다. 같은 binding이면 다시 변경하지 않는다. rollout과 ConfigMap 값·UID, controller UID·container image를 다시 확인한다. import Pod는 API와 같은 `fsGroupChangePolicy:OnRootMismatch`를 사용하여 기존 PVC의 0700/0600 파일 모드를 재귀 변경하지 않게 한다.

Terraform plan에 delete·replace가 있거나 기존 VM의 AMI/subnet/root disk/user-data/종료 기한 관련 값이 바뀌면 차단한다. 결과가 불명확한 apply는 다음 실행에서 새 plan으로 현재 상태만 확인한다. 변경이 남아 있으면 자동 재적용하지 않는다. GitHub 게시 workflow도 한 번만 dispatch하며 이후에는 저장한 run ID 또는 유일하게 관측한 새 run을 확인한다. 응답 유실·중복 후보는 새 run을 만들 근거가 아니다.

최초 verifier apply의 응답이 유실되어 입력 lineage가 null인 채 state가 생긴 경우, unknown 기록과 원래 saved plan의 SHA256을 먼저 대조한다. 원래 계획을 덮어쓰지 않고 별도 native 관측 plan에서 같은 resource address/type/provider·알려진 속성, 전체 no-op를 확인한 경우에만 생성된 lineage와 최종 attempt 상태를 한 번의 atomic write로 기록한다. 임의의 기존 state를 자동 인수하지 않는다.

이미지 게시는 기존 `platform-publish.yml`의 `publish=true, deploy=false`를 사용한다. 같은 run의 dashboard/API/runner artifact만 읽고 기존 `publish-platform.py`를 깨끗한 전용 checkout에서 실행한다. `verify-platform.py remote`가 정확한 Argo revision, 소유 ReplicaSet/Ready Pod의 imageID, 외부 HTTPS를 모두 확인해야 마지막 `verified`를 출력한다. 향후 workflow의 자동 deploy를 위한 고정 target/port/verifier 변수도 준비하고 readback한다. GitHub OIDC subject 설정과 계정 권한은 검토된 verifier 정책에 맞아야 한다.

최초 bootstrap 이후 자동 업데이트는 저장소 Actions 변수 `RAILSHOT_AUTO_RELEASE=true`와 `RAILSHOT_PLATFORM_VERIFY_REF`에 지정한 브랜치의 push로 시작한다. 문서 전용 변경을 제외한 플랫폼·런타임·공통 정책 변경이 대상이며, 기존 플랫폼 자동 릴리스는 이 설정으로 계속 운영한다. 중앙 워커와 AWS/GCP/OpenStack 공통 릴리스는 별도 변수 `RAILSHOT_MULTICLOUD_RELEASE=true`로 활성화한다. 이 두 번째 변수는 초기 운영 바인딩과 세 환경 검증을 준비할 때까지 미설정 또는 `false`로 유지한다. CI가 검사한 이미지 artifact를 같은 실행에서 GHCR 게시·GitOps 선언 갱신·운영 검증에 사용한다. 수동 publish/deploy 경로도 유지하며, 수동 `deploy=false` bootstrap 기록은 자동 업데이트의 성공 근거가 아니다. `platform-verification-<source SHA>` artifact의 exact revision·running digest·HTTPS 검증 결과를 확인한다.

재실행은 receipt만으로 단계를 건너뛰지 않는다. instance/volume/lineage/node UID와 현재 Kubernetes 객체를 다시 읽는다. 기존 Secret의 회전된 자격은 보존하고 executor binding이 바뀌면 중단한다. 검증 Job은 deterministic 이름으로 한 번 생성하고 같은 Job 결과를 다시 읽는다. native CI 검증 전에는 runner를 중단시키지 않고, active runner가 있으면 차단한다. 검증 실패 시 controller가 suspend 상태로 남을 수 있으며 이를 숨기거나 자동 성공으로 처리하지 않는다.

SDK 초기 자격은 별도 운영자가 active CODEX_HOME인 `/var/lib/railshot-runner/codex`에 준비하며 bootstrap은 해당 auth/config와 native SDK의 refresh 권위를 보존하고 legacy 자격을 다시 복사하지 않는다.

## 소유자가 준비하는 private 이관 패키지

`import_package`는 owner가 writer를 먼저 멈춘 뒤 내보낸 0600 JSON 경로다. 일반적인 파일 동기화 기능이 아니다. package와 archive는 각각 32 MiB 이하이며, 이를 넘는 이전은 별도 검토가 필요하다.

필드는 `version:1`, 32자리 hex `package_id`, `source_owner`, `destination_owner`, `freeze_receipt`, `freeze_sha256`, `destination_freeze_receipt`, `destination_freeze_sha256`, `archive`, `files`다. source freeze receipt는 같은 source_owner와 `writers:{api:"stopped",terraform:"frozen",edge:"frozen",budget:"frozen"}`를 기록한다. 별도 destination receipt는 `version:1`, 같은 `destination_owner`, 기존 `application_uid`·`deployment_uid`·`pvc_uid`, `writers:{api:"stopped",argocd:"frozen"}`만 갖는다. 미설치 Application/Deployment UID만 null일 수 있다. 두 receipt 모두 0600 파일이며 패키지의 SHA256과 일치해야 한다. archive에는 files에 열거한 regular file만 넣는다. `owner.json`, lock, SQLite `-wal/-shm`, symlink는 거부하므로 DB owner가 일관된 SQLite export를 만들어야 한다.

각 file은 `path`, `kind`, `sha256`을 갖는다. 목적지는 PVC `/var/lib/railshot` 아래의 `state/`, `cd/`, `infra/`, `edge/`, `budget/`, `registration/`, `terraform/`, `billing/`, `environments/`, `control-terraform/`에 한정한다. config는 `config/app-db/`의 직접 파일과 `config/edge.json`만 허용한다. app-db 파일은 immutable `source` kind로 현재 checksum을 재검사한다. 전용 SSH private/public key·검증된 고정 known_hosts도 app-db 하위에 두고 0600으로 설치한다. `enroll_ssh=true`로 런타임별 host key 등록을 사용하는 경우 known_hosts template 파일은 만들거나 이관하지 않는다. `config/app-db/profiles.json`은 필수다. 현재 API state는 `state/`, 고객 Terraform은 `terraform/`, ledger는 `billing/billing.sqlite3`를 사용한다. kind `terraform`에는 보존할 `lineage`, `budget`에는 `scopes:{scope:{provider,source_scope}}`, `cd/edge/registration`에는 변하지 않아야 하는 최상위 identity 필드 `binding`이 추가로 필요하다. API snapshot은 기존 v1 operations/keys/bindings/plans 형식을 확인한다.

`config/edge.json`은 `edge` kind이며 binding은 `version:1`, `state_dir:/var/lib/railshot/edge/allocations`, `terraform_dir:/var/lib/railshot/edge/module`, `variables_file:/var/lib/railshot/edge/inputs.tfvars.json`으로 고정한다. 최초 checksum을 확인한 뒤 owner의 baseline 검증에 따른 `auto_apply:false→true` 전환은 보존한다. 재실행이 이전 false 값을 덮어쓰지 않는다. 이 가변 파일을 executor Secret에도 넣으면 init container가 복사본을 되돌릴 수 있으므로 해당 중복은 차단한다.

플랫폼 control/CI의 authoritative Terraform backend는 이 명령의 `terraform` 입력에 계속 귀속한다. 패키지의 `control-terraform/`와 이전 k3s AWS/GCP Terraform 자료는 역사 보관이며 bootstrap이 PVC 복사본에 apply하지 않는다. 과거 macOS owner_ref나 saved plan 경로를 수정하지 않고 새 프로필의 실행 대상으로 등록하지 않는다. 이 두 플랫폼 backend까지 단일 API writer에 이관하려면 기존 bootstrap 입력과 실행 권위를 먼저 변경·검토해야 한다. 고객 Terraform·edge·budget 이관과 플랫폼 backend 이관을 동일한 것으로 취급하지 않는다. 런타임이 소비하지 않는 fixtures/transfer-evidence, 이전 provider `.terraform/` 캐시와 실행 lock은 패키지에서 제외한다. `.terraform.lock.hcl` 공급자 버전 lock은 보존한다.

최초 이관은 기존 API replica 0 또는 미설치 상태에서만 진행한다. receipt의 UID를 인수 목록과 live 객체에 대조하고, `railshot-platform` Application의 자동 sync 비활성·진행 중 operation 부재, API HPA 부재, 종료 중인 Pod를 포함한 API selector 및 PVC 사용자 Pod 부재를 import Pod 생성 전과 실제 쓰기 직전에 확인한다. 운영자가 자동 sync와 쓰기 경로를 먼저 동결해야 하며 이 명령은 단순 replica 수를 동결 증거로 취급하지 않는다. checksum, Terraform lineage, API 구조, SQLite integrity와 billing scope, CD/edge 등록 identity를 검증한 뒤에만 PVC에 새 owner marker를 남긴다. 기존 파일의 다른 내용을 덮어쓰지 않는다. 재실행에서는 marker와 현재 binding을 확인하고 새 writer가 진행시킨 state를 이전 snapshot으로 복사하지 않는다. 원본의 동결 해제나 원본 삭제는 수행하지 않는다.

이관 응답 유실 후 남은 Pod가 Succeeded/Failed이면 같은 manifest annotation·이미지·managed label·PVC mount·SA token 비활성 및 ownerReferences 부재를 확인하고, 관측한 UID를 삭제 precondition으로 지정한다. 부재 readback 후 Pod를 다시 만들어 기존 import marker를 검증한다. 실행 중인 Pod는 이 복구 경로에서 삭제하지 않는다.

로컬 검사:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s deployment/scripts/tests -p test_bootstrap_platform.py -v
```
