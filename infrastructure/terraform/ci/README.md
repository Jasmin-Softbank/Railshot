# AWS private CI workspace

This module bootstraps the retained `infrastructure/ansible/ci.yml` CI container policy and native network probe. It adds provider resources and transport; it does not create a second CI implementation or a new app cluster. One `t3.xlarge` (configured 4 vCPU/16 GiB, Standard CPU credits), encrypted retained 60 GiB gp3 root disk, no inbound security-group rules, IMDSv2 with hop limit one. The public address is for outbound package/SSM access. HTTP/HTTPS and the two configured public DNS resolvers are allowed outbound; container isolation remains the common native probe's responsibility.

The instance role has SSM agent permissions with an explicit deny for all Parameter Store and Secrets Manager credential reads. No model/GitHub/registry key or cloud management permission is installed. The SSH public key belongs to the private administrator, who connects through authenticated SSM; SSH is not exposed on the public interface. The administrator has sudo and this VM is not a multi-tenant host boundary. Uploaded app checks execute under the common container policy. GitHub self-hosted runner registration is not provided by this module.

Optional `name` (default `railshot-ci-poc`) identifies this dedicated CI VM, separately from the CD handoff target.

`instance_name` optionally overrides only the EC2 `Name` tag. Keep `name` unchanged when renaming an existing worker because it also identifies its IAM role, instance profile, security group and target descriptor. The active build worker is `i-09955d23ad1d8dbe2`, displayed as `railshot-build-worker-aws-01` from 2026-10-02; its stable `name` remains `railshot-ci-k3s-aws`. Set `instance_name = "railshot-build-worker-aws-01"` in that instance's private Terraform inputs. This worker handles submitted app checks, bounded AI repair and image builds; it is separate from the operations K3s and does not run K3s itself.

The 2026-10-02 read-only plan confirmed matching `Name` tags after the rename, but proposed instance replacement for `associate_public_ip_address` (observed `false`, configured `true`) while the instance was stopped. That plan was not applied. Resolve this unrelated drift before any full apply; do not replace the worker to change its display name.

Required variables: registered `account_id`, exact Canonical Ubuntu 24.04 amd64 `ami_id`, published `platform_ref`, SHA256 of `https://codeload.github.com/Jasmin-Softbank/Railshot/tar.gz/<platform_ref>`, administrator Ed25519 **public** key without comment, and UTC `stop_at`. No private key is copied to the VM or Terraform state. Bootstrap arms an absolute systemd STOP timer before network installs and refuses already-expired reuse. This guest timer is a bounded PoC cutoff, not cloud-enforced orchestration or job draining. Stopping retains disk data and disk cost. Review and set a new deadline before intentional reuse.

```sh
terraform -chdir=infrastructure/terraform/ci init -backend=false
terraform -chdir=infrastructure/terraform/ci validate
terraform -chdir=infrastructure/terraform/ci plan -state=/private/path/ci.tfstate -var-file=/private/path/ci.tfvars.json -out=/private/path/ci.tfplan
terraform -chdir=infrastructure/terraform/ci show /private/path/ci.tfplan
# Apply only the reviewed plan, using independent private state/backend.
terraform -chdir=infrastructure/terraform/ci apply -state=/private/path/ci.tfstate /private/path/ci.tfplan
terraform -chdir=infrastructure/terraform/ci output -state=/private/path/ci.tfstate -json node_descriptor
```

The private console JSONL worker was removed. `node_descriptor.worker` keeps its prior Python/root fields for compatibility, with `source = null` and `status = NOT_CONFIGURED`. This module does not register a GitHub Actions runner or provide a job dispatcher. SSM access alone does not connect the VM to the team's Actions workflow. That integration needs a separately selected execution path.

The common `railshot-ci-verify.service` runs actual isolation probes after Docker/network restart. Terraform `UNVERIFIED`, a boot marker or SSM Online alone is not complete CI readiness. Local checks: `python3 -m unittest discover -s infrastructure/terraform/ci -p test_bootstrap.py`. This only renders the public source template and checks syntax/policy; AWS creation, SSM transport, runner registration and a complete build remain separately unverified. Current team boundaries are in the [repository README](../../../README.md).
