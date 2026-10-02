# Existing platform release bootstrap

This module is applied by the bootstrap operator using their existing AWS and
GCP authority. It installs the fixed SSM release entrypoint, GitHub OIDC role,
AWS-to-GCP federation and observer ingress for the existing machines. It does
not create a VM, security group, load balancer, service-account key or customer
application. Keep its local state separate from the provider edge states.

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
