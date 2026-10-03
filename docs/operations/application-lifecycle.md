# Application stop, start and permanent deletion

The dashboard exposes management actions for a session-owned, registered application
in deployment history and its deployment detail. The trash icon opens a native warning
dialog. **Cancel** closes it; **Permanently delete** is the additional confirmation
click and sends `delete_data: true`. No typed name or extra checkbox is required.
The dialog displays a server-generated plan, including retained shared resources.

## API and state

| Request | Result |
| --- | --- |
| `POST /api/v1/applications/{id}/plans` with `action` | Read-only inventory, public resource names, retained resources, plan hash and ten-minute expiry |
| `POST /api/v1/applications/{id}/operations` | Durable operation intent; `202` with a status `Location`; requires `Idempotency-Key`, plan ID/hash and app-name confirmation |
| `GET /api/v1/operations/{id}` | Steps, outcome and potentially remaining resources, scoped to the same session |

The confirmation dialog supplies the known app name itself. The API still binds it to
the session-owned application and the approved plan. The same idempotency key/body
returns the existing operation; changing the body is a conflict. Reads do not retry
mutations. Restarted in-flight operations become `unknown` and need reconciliation.

| Action | App state after verified success | Resource effect |
| --- | --- | --- |
| Stop | `stopped` | Disable CI target and GitOps reconciliation; remove app-specific external backend/route; scale workloads to zero and suspend jobs; retain data, namespace, credentials and route identity |
| Start | `ready` | Restore saved replicas/suspend values; wait for workload readiness; recreate app route; restore reconciliation and CI target |
| Delete | `deleted` tombstone | Stop writers, remove app route/DNS, revoke credential renewal, delete namespace and supported storage, delete scoped Argo/credential registration, release NodePort allocation |

Completed Job Pods may remain while stopped. Start verifies Kubernetes readiness, provider resource readback and HTTPS 200 at
the registered hostname and health path before marking the app ready.
Deployment history, operation receipts, uploaded source/build artifacts and immutable
GitOps source history are retained as audit/build records. Delete removes the running
app's data and allocations; it is not a repository or account-erasure endpoint.

A queued/running deployment of the same registered app can be deleted. The API first
persists `deletion_requested`, waits for its local worker to stop, cancels the exact
bound GitHub run once and observes completion. Worker guards precede and follow
registration, CI dispatch and CD. Cleanup begins only after the remote writer is
known to be stopped. Explicitly resumed CD workers use the same deployment tracking,
so deletion waits for their completion too. A successful stop/start operation remains
an audit reference and does not permanently block later deployment resumption.
The inventory is refreshed after cancellation: an expanded scope
requires a new confirmation. Queued/registering apps use a deletion plan scoped to their exact application ID
and deployment. After the writer stops, a fresh private plan must prove ownership.
If registration never began, absence of its durable pre-write intent and binding under
the registrar lock permits a local tombstone with zero remote changes. A partial
registration or unknown remote outcome is blocked; resource ownership is never guessed
from a display name.

## Execution and ownership

`deployment/scripts/application_lifecycle.py` is the existing application adapter's
private CLI, using the same registrar configuration and registration lock. It persists
a private plan with registration/binding hash, Kubernetes UIDs, saved declarations and
provider authority snapshot. The public API only receives bounded resource names and
step states. Credentials and Terraform state never cross that boundary.

Apply checks expiry, action/app/environment, saved-plan hash, immutable registration,
namespace and registered ServiceAccount UID, workload declarations, provider lineage,
exact resource IDs and DNS ownership. Pod/Endpoint churn does not invalidate the
approved namespace's declaration. Kubernetes writes use UID/resourceVersion
preconditions. Shared policy changes use resourceVersion checks and preserve other
apps. An empty credential-renewal policy produces an empty Role, never an unrestricted
`resourceNames: []` rule.

Deletion order is deliberate:

1. Persist intent and block future registration/publication for this app.
2. Remove its CI binding and exact Argo Application after checking no sync is active.
3. Remove its external traffic resources and owned DNS through the existing provider writer.
4. Remove its renewal-policy entry and exact Secret permission.
5. Delete the UID-bound namespace and wait for supported backing-storage reclamation.
6. Delete its Argo cluster credential and AppProject; remove exact control permissions.
7. Save the deleted tombstone and release the reserved NodePort only after all checks pass.

Every provider writer revalidates its approved plan immediately before its own writes.
A late drift or timeout can therefore leave a partially completed operation. It remains
`unknown`, with conservative residual resource names; there is no automatic force,
rollback, finalizer removal, or mutation retry. Other writes remain blocked until an
operator reconciles actual state and the durable journals. Never clear a journal merely
to make a retry possible.

## Provider resources

| Provider | App-owned resources | Always shared/retained |
| --- | --- | --- |
| AWS | ALB listener rule, target group/attachment, app-specific NodePort SG rule; owned DNS on delete | ALB/listener, shared certificates, VM, VPC/subnets, cluster |
| GCP | URL-map app host/path entry, backend service, NEG/endpoint, health check, NodePort firewall contribution; app certificate/map entry and DNS on delete | Shared HTTPS frontend/IP/proxy/map, VM/network/cluster, legacy baseline |
| OpenStack | Exact route policy/rule, pool/member/health monitor, app NodePort SG rule, tunnel ingress entry and owned DNS | Octavia listener/LB, tunnel/Deployment/Secret, VM/network/cluster |

Stop removes active app backends instead of leaving an unhealthy target pointing at
an absent workload. Start restores the recorded app route. The last application may
be removed without destroying the shared edge. Terraform is applied only from a saved
plan whose permitted changes are the exact owned deletions/recreations plus bounded
shared route-list edits. AWS/GCP also read provider inventories after deletion to
check exact resource IDs and target-group/NEG parents are absent. Broad
`terraform destroy` is never used.

The older GCP baseline `.app` resources are not registered app-owned routes. This
feature does **not** adopt or delete those resources; the previously observed fixture
backend requires a separately reviewed ownership migration. Legacy deployment records
without an application ID and dedicated environment/VM deletion are likewise outside
this app deletion API.

## Rollout

Apply the reviewed AWS product-edge IAM policy first: it adds app-scoped deletion
and SG-rule revocation while retaining explicit protection for bootstrap targets.
GCP/OpenStack operator identities must also permit the corresponding app-owned deletes
and readback checks. This source change does not itself apply live cloud IAM.
AWS Access Analyzer `ValidatePolicy` returned zero findings on 2026-10-03 for
`product-edge-policy.json` SHA-256
`fb401be9f21ee946592cd56f1e2991359df0d60785bca03ddf36decb7a118dfb`.
That validates the policy document; it is not an effective-permission or deletion test.

The API image packages the CLI, runtime inventory helper and provider cleanup writer;
existing CI discovers their Python and API/browser tests. Dashboard and API must be
released together because the operation response/polling contract is shared. Kubernetes
control permissions are upgraded only for registered exact names during execution.

OpenStack additionally runs a fixed controller-host executable at
`/opt/railshot/octavia/openstack_route_worker.py`. Install the reviewed worker revision
through the existing operator deployment path before enabling its lifecycle RPCs; an
API image update alone does not update that host file. Unsupported/old worker responses
fail closed. The on-prem integration on 2026-10-03 installed the worker whose SHA-256 is
`5ccb8f6e1bf4e0d65c681c9a9f62045e66661f8bc3f3a01da1b5d1d9a8efb230`;
the source includes that version. Its isolated acceptance verified health-path
replacement, strict TLS/HTTP 200, native route/SG deletion and identical-request replay.
Do not replace it with the earlier worker during the API release. A health-path change
recreates only that app's verified route and briefly interrupts its traffic.

## Storage and verification limits

The runtime performs full namespaced API discovery. Supported built-in resources must
belong to the registered namespace and its ownership chain. A CiliumEndpoint is allowed
only when it belongs to a real Pod UID in that namespace. Other CRs, external controllers,
foreign owners and external database bindings block automatic deletion.

PVC deletion is supported for exclusively bound, dynamically provisioned CSI volumes
with a `Delete` reclaim policy and the external-provisioner deletion finalizer. It
checks PVC→PV UID binding, provisioning identity, StorageClass, duplicate/shared volume
handles and attachment state. Completion requires namespace, PV and VolumeAttachment
absence. This uses the CSI controller's backing-volume deletion guarantee, rather than
claiming a separate native cloud API verification. Retain/static/shared volumes, local
paths and unowned external storage require explicit ownership/reclamation support
before this operation can proceed. See Kubernetes documentation on
[PV deletion protection](https://kubernetes.io/docs/concepts/storage/persistent-volumes/#persistentvolume-deletion-protection-finalizer)
and [finalizers](https://kubernetes.io/docs/concepts/overview/working-with-objects/finalizers/).
The GitOps stop uses the documented
[skip reconciliation annotation](https://argo-cd.readthedocs.io/en/stable/user-guide/skip_reconcile/).

The automated checks use simulated Kubernetes/provider/GitHub interfaces and a local
browser/API. They exercise confirmation, session isolation, cancellation races,
idempotency, ownership drift, partial writes, storage retention and unknown outcomes.
They do not delete a production app or establish live three-provider teardown success.
A live acceptance run must use a disposable registered app on each provider, record its
namespace and edge IDs, test stop/start/delete, then verify the app resources are absent
and another app plus shared infrastructure remain healthy.

OpenStack storage component acceptance on 2026-10-03 separately verified Cinder PVC
creation, data preservation across Pod recreation, 1Gi-to-2Gi expansion, and deletion
through PVC/PV/VolumeAttachment absence to the original Cinder volume's HTTP 404.
The native storage inventory accepted that real volume with the source helper hash
`a0dc1e04bdc0377727d910e05094aeb40f7d65672b632f97574a327fb2221b73`.
This component evidence does not establish dashboard/API application deletion on all
three providers. The Cinder installation contract is in [deployment/cinder/README.md](../../deployment/cinder/README.md).
