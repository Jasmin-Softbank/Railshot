# 기존 운영 노드 Terraform 복원

원본 Jasmin commit `2b4b462`의 `infra/terraform/control/main.tf`와 provider lock을 재사용해 **기존** `railshot-control-poc`를 관리한다. 유저 AWS/GCP 앱 노드나 격리된 CI builder를 생성·대체하는 모듈이 아니다. 기존 resource 주소와 IAM role/profile/policy, 기본 보안 그룹을 유지한다.

현재 복원 대상은 계정 `721622471953`, region `ap-northeast-2`, instance `i-033ae2db907fde68e`, primary ENI `eni-09a70375ba8bfc234`다. 이미지와 subnet은 원본 state에 기록된 정확한 값을 private inputs로 전달한다. 새 default subnet을 선택하거나 AMI를 자동 갱신하지 않는다. `prevent_destroy`로 운영 인스턴스 교체·삭제를 차단한다.

기존 cloud-init은 옛 Ansible/accounts 경로를 참조한다. 이 복원에서는 그 파일을 복제하거나 user-data를 재계산하지 않는다. `ignore_changes = [user_data, user_data_base64]`로 기존 값을 보존하고, 현재 운영 K3s/Argo 설정은 별도 관리 작업으로 수행한다. 다른 기존 resource drift를 자동으로 승인하지 않는다.

기존 `AmazonSSMManagedInstanceCore` 및 Codex 인증 Parameter 조회를 유지한다. 추가한 registry 연결은 운영 역할에 `/railshot/registry/ghcr/pull_token` 한 경로의 조회만 허용하고, 다른 Parameter 조회와 경로·이력 조회는 거부한다. `registry.tf`의 GitHub OIDC 역할은 railshot-apps의 고유 조직·저장소 ID와 `railshot-release` 환경에 결합되어 이 경로의 `PutParameter`만 허용한다. 토큰 값은 Terraform 입력이나 state에 넣지 않고 전용 workflow가 SecureString으로 전달한다.

## State와 기대 plan

원본 snapshot:

```text
/Users/mango/workspace/softbank2026 hackerthon/Jasmin/infra/terraform/control/terraform.tfstate
```

새 authoritative state:

```text
/Users/mango/.local/share/railshot/cloud-e2e-20261002/control-terraform/terraform.tfstate
```

원본 serial 7, lineage `56131c6f-03c0-a9ab-5fe1-ba591b8bf7f8`를 0600으로 복사했다. 원본은 보존용 snapshot이며 이후 apply는 **새 경로 한 곳**에서만 수행한다. 원본과 복사본에 각각 apply하면 동일 자원을 두 state가 관리하게 되므로 원본 경로를 실행 대상으로 사용하지 않는다. Private 디렉터리 권한은 0700, state·tfvars·plan·로그 권한은 0600이다.

최초 복원 plan은 다음 두 변경으로 적용했다. 이후 `connectivity.tf`는 운영 노드에서 유저 AWS 노드의 6443 접근, `registry.tf`는 위의 제한된 OIDC 전달 권한을 추가했다. 각 단계는 새 saved plan으로 별도 검토했다.

1. `aws_security_group.edge` 한 개 생성. 인라인 ingress/egress 없이 만들고 규칙은 별도 `aws-edge` 모듈이 소유한다.
2. `aws_instance.control`을 제자리에서 수정. `source_dest_check: true → false`, 보안 그룹 목록에 위 edge SG 추가.

기존 base SG `sg-01a71e8be9a585b5a`의 ingress/egress·설명, AMI, user-data, instance profile은 유지한다. IAM 변경은 위의 registry 경로와 전용 OIDC 역할에 한정한다. 새 SG 출력만 `aws-edge.wireguard_security_group_id`로 넘긴다. Base SG를 edge 입력으로 사용하면 inline/외부 rule 소유권이 충돌한다.

## 관리자 명령

저장소 root에서 아래처럼 정확한 backend 경로와 private tfvars를 지정한다. 파일들은 복원 작업으로 작성되며 예산·권한·plan 검토는 호출자 책임이다. 이 모듈은 공통 app-host executor의 budget reservation 경로에 포함되지 않는다.

```sh
TF_DATA_DIR=/Users/mango/.local/share/railshot/cloud-e2e-20261002/control-terraform/.terraform \
terraform -chdir=infrastructure/terraform/control init -input=false -lockfile=readonly \
  -backend-config=path=/Users/mango/.local/share/railshot/cloud-e2e-20261002/control-terraform/terraform.tfstate

TF_DATA_DIR=/Users/mango/.local/share/railshot/cloud-e2e-20261002/control-terraform/.terraform \
terraform -chdir=infrastructure/terraform/control plan -input=false \
  -var-file=/Users/mango/.local/share/railshot/cloud-e2e-20261002/control-terraform/inputs.tfvars.json \
  -out=/Users/mango/.local/share/railshot/cloud-e2e-20261002/control-terraform/reviewed.tfplan
```

`review-summary.json`에는 변경 resource·action·필드 이름만, `reviewed-plan.json`과 native 로그에는 상세 검토 자료를 비공개로 보존한다. 기대 범위를 벗어난 변경이나 replacement가 있으면 apply하지 않고 먼저 원인을 확인한다. 검토한 saved plan의 apply는 root 운영 작업자가 별도로 수행한다. 2026-10-02 복원 및 제한된 후속 연결 apply는 완료했으며, 새 변경에도 plan 검토가 필요하다.

출력은 기존 `instance_id`, `auth_parameter_name`, `connect`와 추가된 `private_ip`, `primary_network_interface_id`, `edge_security_group_id`, `source_dest_check`다. 이 값은 WireGuard handshake, route, Kubernetes/Argo 또는 외부 앱 준비 완료 증거가 아니다.
