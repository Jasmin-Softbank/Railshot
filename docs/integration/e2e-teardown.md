# E2E 종료와 리소스 해제

PR 검증이 만든 일회성 자원과 기존 AWS/GCP PoC 자원은 소유자가 다르다. PR CI는 자신의 실행에서 만든 자원만 정리한다. 이 문서는 종료 경로이며, 기존 클라우드 자원을 삭제했다는 기록은 아니다.

## 1. GitHub hosted runner의 E2E

일회성 GitHub hosted Linux amd64 runner의 저장소 root에서 실행한다. 로컬 개발 장비와 기존 self-hosted runner에서는 guard가 거부한다. run ID·attempt·job·commit SHA로 실행을 구분하고, 동일한 `--output-dir`로 결과와 정리 상태를 이어받는다.

```sh
sudo env GITHUB_ACTIONS="$GITHUB_ACTIONS" RUNNER_ENVIRONMENT="$RUNNER_ENVIRONMENT" \
  GITHUB_RUN_ID="$GITHUB_RUN_ID" GITHUB_RUN_ATTEMPT="$GITHUB_RUN_ATTEMPT" \
  GITHUB_JOB="$GITHUB_JOB" GITHUB_SHA="$GITHUB_SHA" \
  GITHUB_WORKSPACE="$GITHUB_WORKSPACE" RUNNER_TEMP="$RUNNER_TEMP" \
  python3 ci/scripts/integration_e2e.py run --output-dir "$RUNNER_TEMP/railshot-runtime-e2e"
sudo env GITHUB_ACTIONS="$GITHUB_ACTIONS" RUNNER_ENVIRONMENT="$RUNNER_ENVIRONMENT" \
  GITHUB_RUN_ID="$GITHUB_RUN_ID" GITHUB_RUN_ATTEMPT="$GITHUB_RUN_ATTEMPT" \
  GITHUB_JOB="$GITHUB_JOB" GITHUB_SHA="$GITHUB_SHA" \
  GITHUB_WORKSPACE="$GITHUB_WORKSPACE" RUNNER_TEMP="$RUNNER_TEMP" \
  python3 ci/scripts/integration_e2e.py cleanup --output-dir "$RUNNER_TEMP/railshot-runtime-e2e"
```

`run`은 팀 runtime의 `deploy.sh` → `verify.sh` → `finally`의 `cleanup.sh --all --disposable-node` 순서로 수행하며, workflow도 `if: always()` 단계에서 `cleanup`을 다시 호출한다. 기존 K3s가 있는 host는 시작 단계에서 거부한다. 설치 프로세스가 살아 있으면 동시에 삭제하지 않는다. 검증 실패를 정리 성공으로 덮어쓰지 않는다.

`ownership.json`은 실행 identity·request hash·smoke 결과·cleanup 상태를 보존한다. 정리 성공은 팀 스크립트의 `cleaned` 응답과 K3s binary/config/data/service 파일의 부재를 확인한 뒤 기록한다. 성공 후 같은 실행을 재호출하면 `already_succeeded`, 실행을 시작하지 않았으면 `not_started`다. 소유권 불일치나 정리 실패는 nonzero로 종료한다. `deploy.json`, `verify.json`, `cleanup.json`과 각 `.log`는 동일 output 디렉터리에 남으며 workflow artifact로 보존한다.

이 검사는 hosted Linux runner 안의 임시 런타임만 사용하고 AWS/GCP VM을 생성하지 않는다. GitHub가 runner를 강제 종료하거나 연결을 잃으면 `finally`와 후속 단계가 실행됐다고 보장할 수 없다. 이 경우 정리 증거가 없으면 **정리 미확인**으로 남기며 성공으로 간주하지 않는다. GitHub의 runner 회수와 스크립트의 정리 확인은 별개다.

## 2. 기존 PoC 자원의 소유 범위

기존 배포는 [클라우드 E2E 기록](cloud-e2e-progress.md)과 [Atlas 기록](../poc/inference-atlas-20261002.md)을 참고한다. 아래 자원은 PR CI의 무조건 삭제 대상이 아니다.

| 범위 | 자원과 의존성 | 해제 판단 |
| --- | --- | --- |
| AWS 고객 앱 노드 | `k3s-aws`, Atlas와 AWS fixture가 같은 K3s 노드를 사용 | 한 앱 테스트 종료만으로 VM 전체를 삭제하지 않는다. 다른 앱·데이터 사용 여부를 먼저 확인한다. |
| GCP 고객 앱 노드 | `k3s-gcp`, GCP fixture와 데이터 디스크 | 해당 노드의 모든 사용이 끝났을 때 provider state로 해제한다. |
| 운영 노드 | `railshot-control-poc`, Argo CD와 GCP WireGuard gateway | 정지하면 GCP 앱의 외부 경로도 끊긴다. 고객 노드보다 나중에 처리한다. |
| CI 노드 | AWS 전용 self-hosted runner | 실행·대기 중인 빌드와 등록된 runner를 확인한다. hosted PR CI와 다른 자원이다. |
| 공유 edge | ALB, target group, listener rule, Route53 레코드, ACM, WireGuard EIP·route | 앱 route만 제거할지 전체 edge를 철거할지 구분한다. VM state에서 삭제되지 않는다. |
| 도메인·DNS | Porkbun의 `railshot.io` 등록, Route53 public zone | 도메인 등록은 Terraform 관리 대상이 아니다. 앱 해제와 도메인 해지는 별도 결정이다. |

앱만 내릴 때는 해당 Argo Application의 재동기화를 멈추고 GitOps 선언과 실제 namespace 자원을 함께 정리한다. 선언을 남긴 채 Pod만 지우면 재배포될 수 있다. 공유 edge의 private `routes` 입력에서 해당 앱 항목을 제거한 **일반 plan**을 검토하면 그 앱의 DNS·listener rule·target group 및 불필요해진 전용 규칙이 삭제된다. 현재 edge 입력은 route 1개 이상을 요구하므로 마지막 앱을 제거할 때 `routes = {}`는 허용되지 않는다. 마지막 route와 공유 ALB/DNS를 유지할지 없앨지는 별도 구성 변경으로 검토해야 한다.

## 3. Stop은 삭제가 아니다

아래는 관리자에게 등록된 대상 ID·region·project·zone을 선택한 뒤 사용하는 정지 명령 형식이다. 이 문서 작성 과정에서는 실행하지 않았다.

```sh
aws ec2 stop-instances --region "$RAILSHOT_AWS_REGION" \
  --instance-ids "$RAILSHOT_AWS_INSTANCE_ID"
gcloud compute instances stop "$RAILSHOT_GCP_INSTANCE_NAME" \
  --project "$RAILSHOT_GCP_PROJECT" --zone "$RAILSHOT_GCP_ZONE"
```

Stop과 자동 정지 timer는 VM 실행만 멈춘다. 디스크·예약 IP·공유 ALB·Route53·이미지·백업은 남는다. 다시 시작할 수 있는 임시 중지와 Terraform destroy를 통한 실제 삭제를 같은 완료 상태로 기록하지 않는다.

## 4. 실제 AWS/GCP 삭제에 사용할 state

[`provision.py`](../../infrastructure/providers/terraform_tools/provision.py)는 `plan`과 `apply`만 지원한다. `destroy` API나 CLI action은 없다. 앱 target의 저장 구조는 다음과 같다.

```text
<private-state-root>/<target_id>/
  binding.json                 # provider/target/exact owner_ref
  terraform.tfstate            # authoritative resource state
  executor.lock                # provisioning executor lock
  .terraform/                  # TF_DATA_DIR; backend metadata, resource state 아님
  module/
    *.tf, *.tftpl, .terraform.lock.hcl
    executor_backend.tf        # backend "local" {}
    inputs.tfvars.json         # owner_ref가 포함된 실제 적용 변수
  reviewed.tfplan, plan-manifest.json, terraform.log
  node-descriptor.json
```

2026-10-02 로컬 작업의 private root는 `/Users/mango/.local/share/railshot/cloud-e2e-20261002`다. 자격값이나 state 본문을 Git/CI artifact에 올리지 않는다.

| 소유 대상 | 이 private root 아래 authoritative state | 적용 입력·모듈 |
| --- | --- | --- |
| AWS 고객 노드 | `terraform/k3s-aws/terraform.tfstate` | 같은 target의 `module/inputs.tfvars.json`, `module/` |
| GCP 고객 노드 | `terraform/k3s-gcp/terraform.tfstate` | 같은 target의 `module/inputs.tfvars.json`, `module/` |
| 공유 edge | `edge-terraform/terraform.tfstate` | `edge-terraform/inputs.tfvars.json`, `edge-terraform/module/` |
| 운영 노드 | `control-terraform/terraform.tfstate` | `control-terraform/inputs.tfvars.json`, 저장소 `infrastructure/terraform/control/` |
| CI 노드 | 기존 관리자의 등록 state를 별도 확인 | [`terraform/ci`](../../infrastructure/terraform/ci/README.md)의 native state 경로. 위 앱 target state를 사용하지 않는다. |

운영 노드의 옛 `Jasmin/infra/terraform/control/terraform.tfstate`는 보존 snapshot이다. [복원 문서](../../infrastructure/terraform/control/README.md)에 지정한 새 authoritative 경로만 사용한다. `.terraform/terraform.tfstate`는 backend 메타데이터이며 resource state 대신 넘기면 안 된다.

### 삭제 보호와 디스크 처리

| 코드의 현재 설정 | 삭제 시 의미 |
| --- | --- |
| AWS `aws_ebs_volume.data`: `prevent_destroy = true` | 전체 destroy plan이 차단된다. 데이터 보존 또는 삭제를 먼저 결정한다. |
| AWS 고객·운영·CI root: `delete_on_termination = false` | EC2를 종료해도 root EBS가 남는다. 종료 전 volume ID를 비공개 기록에 남기고, 종료 후 남은 디스크를 별도로 확인한다. |
| GCP `google_compute_disk.data`: `prevent_destroy = true`, `deletion_policy = "PREVENT"` | Terraform lifecycle과 provider 양쪽에서 데이터 디스크 삭제를 막는다. |
| GCP instance boot disk: `auto_delete = true` | VM 삭제 시 boot disk도 삭제된다. stop에서는 보존된다. 필요한 boot 데이터를 먼저 보관한다. |
| 운영 `aws_instance.control`: `prevent_destroy = true` | 공유 운영 노드 destroy가 차단된다. |
| edge가 생성한 Route53 zone: `prevent_destroy = true` | 전체 edge destroy가 차단된다. 기존 zone을 data source로 참조한 경우 소유·삭제 대상 자체가 다르다. |

보호 오류를 무시하거나 state에서 자원을 지워 성공으로 처리하지 않는다. 데이터까지 완전히 삭제하기로 정한 경우에만 담당자가 보호 설정 변경을 검토하고 그 변경을 적용한 뒤 새 destroy plan을 만든다. GCP의 `deletion_policy` 변경은 일반 plan/apply로 실제 관리 상태에 반영한 뒤 삭제를 계획한다. 데이터를 보존하려면 별도 보존 소유권과 state 관리 경로를 먼저 마련한다. 현재 공통 도구에는 이 이관 자동화가 없다.

### Native saved destroy plan 경로

다음은 **고객 target 하나**의 명령 예시다. `k3s-aws` 또는 `k3s-gcp` 중 실제 종료할 대상을 명시한다. 기존 worker·Ansible·CD 작업을 종료하고, 다른 운영자가 같은 target의 `provision.py`를 실행하지 않는 독점 작업 구간에 수행한다. Terraform backend lock도 유지한다.

1. 등록 target, `binding.json`, state, 적용 변수의 `target_id`, `provider_kind`, `owner_ref = terraform:local:<exact state path>`가 일치하는지 확인한다. 계정/project/region/zone 및 module/provider lock을 대조한다. 백업·drain·해당 앱 route 제거를 끝내고 공유 자원의 포함 여부를 확인한다.
2. 기존 적용 모듈과 private 입력을 사용한다. 최신 저장소 파일로 무작정 덮어쓰거나 provisioning의 `plan`을 다시 실행하지 않는다. 그 동작은 저장된 module/plan을 교체한다.
3. 아래 native 명령으로 **새** destroy plan과 비공개 검토 파일을 만든다. 보호 설정이 유지된 현재 코드에서는 앞서 설명한 보호 오류로 막힐 수 있으며, 이때 apply로 넘어가지 않는다.

```sh
umask 077
RAILSHOT_OPS=/Users/mango/.local/share/railshot/cloud-e2e-20261002
RAILSHOT_TARGET=k3s-aws
RAILSHOT_TF_HOME="$RAILSHOT_OPS/terraform/$RAILSHOT_TARGET"
RAILSHOT_TF_WORK="$RAILSHOT_TF_HOME/module"
export TF_DATA_DIR="$RAILSHOT_TF_HOME/.terraform"
test -f "$RAILSHOT_TF_HOME/binding.json"
test -f "$RAILSHOT_TF_HOME/terraform.tfstate"
test -f "$RAILSHOT_TF_WORK/inputs.tfvars.json"
RAILSHOT_TEARDOWN_DIR="$(mktemp -d "$RAILSHOT_TF_HOME/teardown.XXXXXX")"

terraform -chdir="$RAILSHOT_TF_WORK" init -input=false -lockfile=readonly -reconfigure \
  -backend-config="path=$RAILSHOT_TF_HOME/terraform.tfstate" \
  > "$RAILSHOT_TEARDOWN_DIR/init.log" 2>&1
terraform -chdir="$RAILSHOT_TF_WORK" state pull \
  > "$RAILSHOT_TEARDOWN_DIR/before.tfstate"
terraform -chdir="$RAILSHOT_TF_WORK" plan -destroy -input=false -lock-timeout=30s \
  -var-file=inputs.tfvars.json -out="$RAILSHOT_TEARDOWN_DIR/destroy.tfplan" \
  > "$RAILSHOT_TEARDOWN_DIR/plan.log" 2>&1
terraform -chdir="$RAILSHOT_TF_WORK" show -json "$RAILSHOT_TEARDOWN_DIR/destroy.tfplan" \
  > "$RAILSHOT_TEARDOWN_DIR/plan.json"
```

각 단계의 종료 코드가 0일 때만 다음 단계로 진행한다. 셸의 `TF_CLI_ARGS*`, `TF_VAR_*`, `TF_WORKSPACE`에 기존 override가 없는 깨끗한 운영 환경을 사용한다. 원래 `provision.py`는 이를 제거하지만 native 호출은 자동으로 제거하지 않는다. plan/log/state는 계속 0600으로 보관한다.

4. `plan.json`의 `resource_changes`를 검토한다. 삭제 주소가 선택한 target의 자원과 일치하는지, 보존 디스크·공유 IAM/OIDC·다른 앱까지 포함하는지 확인한다. 승인 대상은 해당 saved plan이며 변경되면 재계획한다. 검토를 마친 **동일 파일**만 다음 명령으로 적용한다.

```sh
terraform -chdir="$RAILSHOT_TF_WORK" apply -input=false -lock-timeout=30s \
  "$RAILSHOT_TEARDOWN_DIR/destroy.tfplan" > "$RAILSHOT_TEARDOWN_DIR/apply.log" 2>&1
terraform -chdir="$RAILSHOT_TF_WORK" state pull \
  > "$RAILSHOT_TEARDOWN_DIR/after.tfstate"
```

이 native 경로는 `provision.py`의 생성용 plan-manifest와 예산/maintenance receipt를 갱신하지 않는다. 기존 생성 receipt와 `node-descriptor.json`을 삭제 완료 증거로 재사용하지 않고, 종료 시각·선택 target·plan hash·native 종료 코드·삭제 후 관측을 별도 비공개 기록에 남긴다. 중단이나 실패는 부분 삭제 가능 상태로 취급해 state와 실제 자원을 대조한 후 새 계획을 만든다.

## 5. 완료 확인과 잔존 자원

관측 도구를 별도로 설치했다면 [observability 정리 절차](../../observability/README.md)를 따른다. 해당 출력 디렉터리의 `docker compose down`은 named volume을 보존하며 `down -v`는 관측 데이터를 삭제한다. 고객 클러스터에서는 검토한 `cluster.json`의 자원만 제거한다. 이 manifest에는 ClusterRole/ClusterRoleBinding도 있어 namespace만 지우면 자원이 남는다. 공유 이름을 쓰므로 다른 대상의 관측 구성이 같은 자원을 사용하는지 먼저 확인한다. 현재 PR CI는 관측 VM·exporter를 설치하지 않으며, 도구 추출 컨테이너와 loopback 테스트 프로세스만 자체 정리한다.

삭제 완료는 native apply 성공만으로 끝내지 않는다. 정확한 계정/project에서 대상 VM과 삭제 대상 디스크·예약 IP의 부재를 조회하고, 보존 자원 목록을 남긴다. AWS retained root EBS, 보존한 data disk·snapshot, 별도 state의 ALB/WireGuard EIP·DNS zone은 고객 VM 삭제 후에도 남을 수 있다. GHCR 이미지·GitOps 기록·SSM SecureString 등 별도 소유 자원도 함께 자동 삭제되지 않는다.

전체 PoC를 철거할 때의 순서는 앱 선언·공개 route → 고객 앱 노드 → 사용이 끝난 CI → 공유 edge와 운영 노드다. Edge는 운영 ENI를 참조하므로 운영 노드를 먼저 삭제하지 않는다. 도메인·zone을 보존할 경우, 보호된 DNS와 삭제할 edge 자원의 소유권을 분리하는 검토가 필요하다. 공통 destroy API와 공유 자원 전체 철거 자동화는 이번 PR CI의 구현 범위가 아니다.
