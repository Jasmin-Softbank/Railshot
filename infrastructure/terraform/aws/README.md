# AWS customer app host

This module prepares one Ubuntu 24.04 amd64 host and retained encrypted data disk. It does not install K3s, Cilium, Argo CD, WireGuard or applications. The platform CI/CD host uses the separate `terraform/ci` module. Use the [administrator executor](../../providers/terraform_tools/README.md) for a reviewed, account-bound saved plan.

For a new customer node behind the separately managed ALB, set:

| Input | Required selection |
| --- | --- |
| `account_id`, `region`, `ami_id` | Registered account, region and exact Canonical Ubuntu image |
| `vpc_id`, `subnet_id` | Both explicit existing IDs; subnet must belong to the VPC |
| `http_enabled`, `https_enabled` | Both `false`; no direct public web ingress |
| `additional_security_group_ids` | Reviewed groups for ALB NodePort and management/cluster traffic; same VPC |
| `create_ci_plan_role` | `false`; avoid recreating the account-level GitHub OIDC identity on each customer host |
| `allocate_eip` | `false` when a subnet-assigned public IP provides egress; routes and public-IP policy remain operator prerequisites |
| `operator_ssh_public_key` | One OpenSSH public key; creates `railshot-operator` with locked password and noninteractive sudo at first boot |
| `initialize_empty_data_disk` | `true` only for a reviewed new blank module-created disk |

`node_security_group_id`, `vpc_id`, `subnet_id` and `instance_id` outputs support the separately owned edge rules. `node_descriptor.transport_ref` remains `ssm:<region>:<instance-id>`. It does not open TCP22; the operator uses authenticated SSM forwarding and a separately verified SSH host key. The node SSM role retains its explicit Parameter Store deny. Private SSH credentials and WireGuard keys are never Terraform inputs.

Legacy defaults remain: default VPC/subnet selection, public HTTP, EIP and read-only GitHub CI role. Moved blocks preserve their Terraform resource identities when these defaults stay enabled. Disabling resources on an existing target can destroy them and must be reviewed; this configuration is intended for a new customer target. Public IP addresses are egress references, not proof that an application URL exists. The legacy `app_domain` output is null when no EIP is requested.

Bootstrap mounts only the expected data disk and records host preparation. Existing ext4 is reused; unknown or conflicting disk state fails closed. Existing guests do not re-run cloud-init merely because the key/configuration input changed. Rotate credentials through the separate management path. Terraform-created resources and descriptor output do not establish guest or Kubernetes readiness.

Offline template and validation checks:

```sh
uv run --python 3.13 --with pyyaml python infrastructure/terraform/aws/test_bootstrap.py
```
