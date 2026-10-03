# Existing platform release bootstrap

This module is applied by the bootstrap operator using their existing AWS and
GCP authority. It installs the fixed SSM release entrypoint, GitHub OIDC role,
AWS-to-GCP federation and observer ingress for the existing machines. It does
not create a VM, security group, load balancer, service-account key or customer
application. Keep its local state separate from the provider edge states.

## CI runtime promotion

A trusted automatic platform release now runs `ci-runtime` after the platform
image publication and live verification. This stage runs even when
`RAILSHOT_MULTICLOUD_RELEASE` is disabled. It uses the same fixed SSM document,
control instance and OIDC role with `Scope=ci-runtime`; no additional IAM action
or resource is granted. Apply the reviewed document update and set its new
`RAILSHOT_RELEASE_DOCUMENT_VERSION` / `RAILSHOT_RELEASE_DOCUMENT_SHA256` outputs
before activating this workflow revision.

The root-owned 0600 `/etc/railshot/release.json` needs only these keys for CI:

```json
{
  "version": 1,
  "state_dir": "/var/lib/railshot-release",
  "workers": {
    "runner_url": "https://github.com/Jasmin-Softbank/railshot-apps",
    "build_node": "<existing build node>",
    "object_uids": "<object returned by platform_workers.py discover>"
  },
  "apps": {"repository": "Jasmin-Softbank/railshot-apps", "branch": "main"}
}
```

Replace `object_uids` with the discovered object, not the placeholder string.
The full provider configuration may coexist in this file, but CI does not read
provider credentials, target registrations, runtime policies or edge state.
The existing GitHub token needs the already used apps-repository workflow/content
and Actions-variable update permissions; it is never returned in a receipt.

Promotion suspends replenishment and waits for the current controller tick to
finish. It updates the controller image and future runner template, reads them
back, pins the apps workflow and `PLATFORM_REF` to the same admitted source, then
resumes replenishment and verifies controller execution. CI reads and updates
only the build controller CronJob and its ConfigMap; credential renewal belongs
to the provider rollout and cannot block CI promotion. Existing runner Jobs keep
their original images and running workflows. Requests dispatched after promotion
use the newly pinned workflow; previously dispatched runs retain their original
GitHub workflow revision. A failed or uncertain promotion keeps a private receipt
for reconciliation and is never automatically replayed.

CI receipts are under `state_dir/ci-runtime/<source-sha>` and are independent of
provider rollout receipts. The optional `multicloud` stage starts after CI and
reads back that same CI/apps promotion rather than applying it twice, and updates
and verifies only the credential renewal worker before its providers. A node
or edge failure cannot roll back a completed CI promotion. Both scopes share the
existing control-host lock. Missing release configuration now returns the fixed
`RELEASE_CONFIG_UNAVAILABLE` code instead of exiting before a JSON receipt exists.

The release executor cannot bootstrap its own authority. Its GCP service account
receives the custom read/update role and one IAP tunnel binding; it receives no
IAM mutation, service enable, VM mutation, resource create or delete permission.
The custom role limits API verbs within `railshot-poc-20261001`. The separate
`edge_update.py` gate limits the exact edge resource IDs and state lineage and
rejects drift, create, delete and replacement plans. IAM is not an additional
per-resource-name restriction on those edge update verbs.

## Keyless control-host authentication

The AWS provider trusts account `721622471953` and requires this exact STS caller
ARN, including the EC2 instance-profile session name:

```
arn:aws:sts::721622471953:assumed-role/railshot-control-poc/i-033ae2db907fde68e
```

The same exact subject receives `roles/iam.workloadIdentityUser` on
`railshot-gcp-edge-release@railshot-poc-20261001.iam.gserviceaccount.com`.
The pool belongs to project number `359201781699`; a project-ID/number mismatch
blocks the bootstrap plan. Keep the existing AWS role trust limited to EC2.
Changing the control instance or role requires a reviewed bootstrap update.

After reviewing and applying the bootstrap plan, export the non-secret credential
configuration from **that module's applied state**:

```sh
terraform -chdir=infrastructure/terraform/platform-release output -json gcp_release_identity
terraform -chdir=infrastructure/terraform/platform-release output -raw gcp_external_account_json > external-account.json
```

Install this configuration as a root-owned, operator-readable file on the control
host and bind its absolute path as `gcp_credentials_file` in
`/etc/railshot/release.json`. The release executor sets both
`GOOGLE_APPLICATION_CREDENTIALS` (Terraform) and
`CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE` (gcloud) to that file. It must not inherit
`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` or `AWS_SESSION_TOKEN` from another
identity: the client otherwise prioritizes those variables over EC2 metadata.

The JSON contains an AWS IMDSv2 token endpoint, metadata credential source,
exact WIF provider audience and dedicated service-account impersonation URL.
It contains no private key, refresh token, personal ADC or AWS credential.
The client obtains short-lived AWS role credentials from EC2 metadata and
exchanges signed STS identity proof for short-lived GCP credentials. The file is
equivalent to this local configuration generation command (no login or cloud
mutation):

```sh
gcloud iam workload-identity-pools create-cred-config \
  projects/359201781699/locations/global/workloadIdentityPools/railshot-aws-release/providers/aws-control \
  --service-account=railshot-gcp-edge-release@railshot-poc-20261001.iam.gserviceaccount.com \
  --aws --enable-imdsv2 --output-file=external-account.json
```

Source: [Google AWS WIF setup and impersonation](https://docs.cloud.google.com/iam/docs/workload-identity-federation-with-other-clouds),
[AIP-4117 AWS credential format and environment precedence](https://google.aip.dev/auth/4117).

## Existing runtime and observer paths

The IAP binding belongs only to `railshot-gcp-poc` in `asia-northeast3-a`, with
`destination.port == 22`. Ansible uses its already registered operator SSH key
and `gcloud compute start-iap-tunnel`. `compute.instances.get/list` supports that
command; OS Login, metadata publication, service-account impersonation of the
VM's identity and `gcloud compute ssh` key management are not granted.
[Google's IAP permission table and port condition](https://docs.cloud.google.com/iap/docs/using-tcp-forwarding)
define this distinction.

Bootstrap also owns these observer-only ingress declarations:

| Existing target | Exact source | Allowed TCP ports |
| --- | --- | --- |
| AWS rules-only SG `sg-0ad2a18168c4eac2b` | Control SG `sg-01a71e8be9a585b5a` | 31490, 31491 |
| GCP runtime VM's existing network and service account | Control public IP `52.78.97.236/32` | 31490, 31491 |

The AWS node module's inline-owned SG and existing SSH rules are untouched. GCP
uses its existing public IP for metric scraping; these two firewall ports do not
open public SSH. Runtime Cilium policy and observer configuration must authorize
the same source separately. These bootstrap firewall resources are not part of
the executor's edge state ownership.

## Permission evidence

The custom role follows the 17 managed resources plus the instance data source
in `../gcp-edge`, with Google provider **8.5.0**. Only the current module's API
families and reference permissions are present. Future additions such as Cloud
Armor, TLS policy or VM changes require an explicit authority review.

| gcp-edge declaration | Refresh and existing-update authority |
| --- | --- |
| Project service | Project `get`, enabled-service `list`, API quota `use`; no service enable/disable |
| Backend instance data | Instance `get`; provider expands boot disk with disk `get` |
| NEG and endpoint | NEG `get` also authorizes endpoint listing; `use` for backend reference; no attach/detach |
| Firewall | `get`, `update` |
| Health check | `get`, `update`, `useReadOnly` |
| Backend service | `get`, `update`, `use` |
| Two URL maps | `get`, `update`, `use` |
| HTTP / HTTPS proxies | `get`, `setUrlMap`, `use`; HTTPS `setCertificateMap` |
| Global address | `get`, `setLabels` |
| Two global forwarding rules | `get`, `setLabels`, `setTarget`, `update` |
| DNS authorization, certificate, map, map entry | Corresponding `get` and `update`; certificate and map `use` |
| Async update completion | Compute global-operation `get`, Certificate Manager operation `get` |

The pinned provider's [Compute sources](https://github.com/hashicorp/terraform-provider-google/tree/v8.5.0/google/services/compute),
[Certificate Manager sources](https://github.com/hashicorp/terraform-provider-google/tree/v8.5.0/google/services/certificatemanager),
and [project-service refresh](https://github.com/hashicorp/terraform-provider-google/blob/v8.5.0/google/services/resourcemanager/resource_google_project_service.go)
establish the actual methods. Google documents the
[NEG list permission](https://docs.cloud.google.com/compute/docs/reference/rest/v1/networkEndpointGroups/listNetworkEndpoints),
[backend reference permissions](https://docs.cloud.google.com/compute/docs/reference/rest/v1/backendServices/update),
[certificate-map reference permission](https://docs.cloud.google.com/compute/docs/reference/rest/v1/targetHttpsProxies/setCertificateMap)
and [Certificate Manager permissions](https://docs.cloud.google.com/iam/docs/roles-permissions/certificatemanager).

## Local validation and deployment boundary

```sh
terraform -chdir=infrastructure/terraform/platform-release init -backend=false -input=false
terraform -chdir=infrastructure/terraform/platform-release validate
terraform -chdir=infrastructure/terraform/platform-release test
```

Tests use mocked AWS/Google providers. They check the exact caller, wrong project
number rejection, privilege boundaries, single-VM IAP port, observer ingress and
IMDSv2 configuration without cloud writes. Native Terraform 1.7.5 is supported.
Check in `.terraform.lock.hcl` with the module, including both AWS 6.66.0 and
Google 8.5.0 provider checksums; do not let a global ignore rule omit it.

Source validation and mock tests do not prove deployed IAM or successful WIF
authentication. After the operator applies the reviewed bootstrap plan, verify
the pool/provider condition, both service-account IAM bindings, observer rules,
control-host short-lived authentication, IAP SSH and the existing edge refresh
under this dedicated service account before treating the release path as ready.
