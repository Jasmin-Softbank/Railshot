# AWS/GCP deployment recovery

A successful build is not a delivered application. Record the operation ID, CI run and attempt, source commit, image digest, GitOps revision, ready Pod imageID and public HTTPS result separately.

## Stop/start is a recovery operation

Railshot uses six-hour scoped Kubernetes tokens. The renewal CronJob runs every two hours. A control-plane shutdown longer than the token lifetime necessarily misses renewal; restarting the VM cannot authenticate an already expired token. `CREDENTIAL_EXPIRED` identifies this state without treating an old Argo `Healthy` value as current connectivity.

Restore the registered customer nodes and the control/platform/build nodes before accepting new deployment work. Verify node readiness and the management API independently of customer apps. The platform worker must retain its Terraform-managed Elastic IP; GCP's `platform_worker_api_source_cidr` must equal that address as an exact `/32`. Do not widen the API firewall to work around address drift.

For expired credentials, an operator must use the existing registered SSH/SSM management transport to issue a six-hour TokenRequest for the original ServiceAccount. Validate the original Secret ownership, namespace/project scope, CA fingerprint, audience, ServiceAccount UID, new token authentication and namespace read access. Replace only the bearer token with a resourceVersion compare-and-swap, and read it back. Never log tokens, widen RBAC, disable TLS verification or extend token lifetime to conceal a failed renewal.

After recovery, run a Job from `argocd/railshot-credentials` and require all four AWS/GCP environment and observer credentials to renew. Check current Argo conditions, not only health labels. Exercise an AWS and GCP deployment concurrently through the public API.

## Concurrency boundaries

Source writes, shared route/Terraform mutations, and GitOps apply are serialized. Argo/Pod/HTTPS observations run outside the global mutation slot and read the exact saved revision. Same-app admission and lifecycle ownership remain enforced by the durable product queue. A slow customer readiness check must not stop a different customer's infrastructure mutation.

The runner controller checks demand at 0, 15 and 30 seconds within its existing minute CronJob. Every HTTP call shares the 45-second deadline; uncertain Job creation stops the current pass without replaying creation.

## Preserve before stopping

Keep the API SQLite backup, source snapshots, configuration and encryption/session keys, Terraform state, registered transport files and GitOps references together in private encrypted storage. Public evidence should contain identifiers, times, statuses, digests and checksums only. Confirm backup integrity and restoration before ending the operating window; saving CI logs alone is insufficient.

## Route allocation and completion

A deleted AWS edge allocation releases its NodePort, hostname and listener priority; stopped, reserved and uncertain allocations remain reserved. Application registration also checks native edge reservations, including routes that predate product registration. Keep route preparation and the initial GitOps apply in one mutation turn so a second preparation cannot overtake the first application's apply.

Delivery requires repeated exact-origin health/site success over at least 15 seconds, resetting after failure or a revision/URL change. Redirects do not count as success. This settling check reduces premature completion while a global load balancer propagates a new route; it does not certify every global frontend. Verify application behavior independently from the controller and preserve any first-request failure in the operation evidence.

## Update and delete verification

AWS/GCP stateless applications use Kubernetes RollingUpdate with maxUnavailable=0, maxSurge=1, three seconds of readiness, and five seconds of preStop drain time. Updates retain the existing Service, NodePort and public hostname while Argo replaces the image. Verify old/new image identity, continuous external requests during the transition, and the final Pod digest. Failed readiness must leave the old replica serving. Single-writer PVC applications retain Recreate; sharing a local SQLite volume between old and new writers is unsafe.

Deletion uses the existing identity-bound lifecycle plan and explicit data-delete acknowledgement. Verify workloads, Argo objects, provider route resources and owned DNS are removed, shared cloud nodes/LBs are retained, and the old allocation is released before recreating the same name. A stopped or uncertain allocation is not a deleted allocation.

A pinned, completed Argo sync that becomes Degraded is a known failed rollout. The workload progress deadline is 300 seconds, within the delivery observation budget. Native preflight failure during an uncertain resume does not clear prior uncertainty. Switching PVC storage mode during update is rejected before changing the GitOps workload; migrate data to a separate app instead.

The application observation API exposes only observed update strategy and replica counts; missing counters are null. Dashboard detail labels rolling/recreate behavior and unknown counters separately, and suppresses these values when observation fails or becomes stale.
