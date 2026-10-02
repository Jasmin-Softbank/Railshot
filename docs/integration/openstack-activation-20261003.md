# OpenStack runtime activation — 2026-10-03

`k3s-openstack` is active alongside the existing AWS and GCP targets. The product
deployment completed at **2026-10-03 05:39:40 KST** (2026-10-02 20:39:40 UTC).
This record distinguishes the tested fixture delivery from AI source repair and
the separate native Octavia migration.

## Verified live

- Nova project `8de0a5b10e974e71b575cd16ff55d6df`: the dedicated VM
  `ce9d854a-c5e5-40ae-b6f2-2898a37131a9` is ACTIVE, with Ubuntu 24.04,
  2 vCPU / 4 GiB RAM / 40 GiB disk and private IP `10.0.0.17`.
- The first owned VM lacked working network metadata. It was deleted and its
  absence verified, then recreated on the same owned port with `config_drive=true`.
  The existing team VM was not changed.
- Ansible `openstack-20261003-guest.check` succeeded. Ansible
  `openstack-20261003-runtime.install` subsequently returned `succeeded`,
  `guest_ready=true`, `runtime_ready=true`, exit 0.
- `railshot-openstack-runtime-01` is Ready with K3s `v1.34.11+k3s1`;
  Cilium, Cilium Envoy/operator and CoreDNS are Running.

The existing Controller server mapping and developer bootstrap/runtime scripts
are retained. Integration adds the explicit registered connection endpoint.

## Registered connection

The private external network is not an Internet ingress. A dedicated outbound
SSH connection reaches the existing AWS control node. Its account permits only
five remote listening ports and cannot open sessions, arbitrary forwards, agent
forwarding or Unix-socket forwarding. The AWS SSH ingress is limited to the
measured OpenStack NAT source `/32`.

| Purpose | AWS private relay | OpenStack destination |
| --- | --- | --- |
| Operator SSH | `172.31.0.172:10022` | `127.0.0.1:22` |
| Kubernetes API | `172.31.0.172:16443` | `127.0.0.1:6443` |
| App backend | `172.31.0.172:13200` | `10.0.0.17:32000` |
| Node metrics | `172.31.0.172:31490` | `10.0.0.17:31490` |
| Cluster metrics | `172.31.0.172:31491` | `10.0.0.17:31491` |

SSH pins the guest host key under its original private IP. The Kubernetes client
and Argo connect to the relay but verify the original VM IP against its CA; TLS
verification is not disabled. No WireGuard enrollment was introduced.

The registered target uses namespace `tenant-openstack`, project
`railshot-openstack`, app `openstack-smoke`, NodePort `32000`, and health path
`/health` returning `{"status":"ready"}`. The existing ALB has a dedicated target
group and DNS route for <https://openstack-demo.railshot.io/health>. Its target is
healthy and an independent HTTPS request returned 200 with certificate
verification enabled and the exact expected JSON. The ALB security group permits
only `172.31.0.172/32:13200` for this backend; the control ingress accepts that
port only from the ALB security group.

## Verified product delivery

- Registrar completed permissions, namespace/RBAC, Argo, credential renewal,
  observer registration and the exact CI application binding. Scoped access
  permits the application namespace and rejects secret reads, other namespaces
  and cluster-role access.
- A Job from the actual credential-renewal CronJob image renewed
  `railshot-k3s-openstack`, retaining the original CA, TLS name and principal.
  Its six-hour credential expires at `2026-10-03T02:12:44Z`; the scheduled worker
  remains enabled. This timestamp is an observed renewal, not a permanent token.
- [Platform publication 37056767944](https://github.com/Jasmin-Softbank/Railshot/actions/runs/37056767944)
  tested and published source `6755162ccdff44f9779f7dee8301163e1376a542`.
  Platform revision `b314cb951e0239554430831afa8dc1464540fe2b` was verified
  against the running API/dashboard image digests and public API/HTTPS. The
  provider map and CD registration preserve AWS and include GCP and OpenStack.
- Product operation `2985ff4e-5420-4198-a6e6-50bfeb1b829c` selected
  `onprem/openstack` and finished `succeeded`, `stage=complete`.
  [Application CI 37061305248](https://github.com/Jasmin-Softbank/railshot-apps/actions/runs/37061305248)
  passed both inspection/build and publication from source
  `0a9e22c34d0a2a2086e7360b0a6b73514940b720`.
- Argo observed exact application revision
  `e999fa3f3c07440db3395c8581a30610731c309f`, Synced and Healthy.
  The OpenStack Pod was Running/Ready with the published image
  `ghcr.io/jasmin-softbank/demo-openstack-smoke-web@sha256:48cf1c0ab9c4251cf60c41256f96a45bf6a9430a1e7e42bdbbf396a26fde5b86`.
- At `2026-10-02T20:40:41Z`, the product API returned fresh observations:
  one app Pod, CPU 29.69%, memory 31.58%, and HTTP probe success 1. The existing
  AWS control hosts the shared observer; no additional AWS VM was created.

The source is the existing tested fixture copied into a separate app; the user's
calculator source was preserved. The browser verified the OpenStack option and
registered application name. Its automated file chooser did not complete, so the
ZIP was submitted through the same public product API in a separate session.
This proves the provider deployment path, not an AI repair run or browser upload.

## Remaining scope and operator guardrails

Native Octavia/L7 migration is separate and still underway. This verified public
path uses the existing AWS ALB and restricted SSH relay. The active runtime is
retained for the hackathon; this is not a full resource-teardown claim.

- Do not cancel a publication-only workflow as though it were a live deployment;
  inspect `publish` and `deploy` inputs and coordinate with its owner.
- Do not replace the shared CD configuration with the registrar's single-target
  output. Merge the target into the live configuration and verify Secret/PVC
  equality while no product operation is active.
- Do not infer public reachability from a healthy Pod. Verify both endpoint
  ingress and load-balancer egress, target health, and strict external HTTPS.
