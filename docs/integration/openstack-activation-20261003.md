# OpenStack runtime activation — 2026-10-03

This record distinguishes live runtime evidence from application delivery. The
operator is activating `k3s-openstack` alongside the existing AWS target.

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

## Registered connection being prepared

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

The planned target uses namespace `tenant-openstack`, project
`railshot-openstack`, app `fixture-npm-js`, NodePort `32000`, and health path
`/health` returning `{"status":"ready"}`. The reserved public expectation is
`https://openstack-demo.railshot.io/health`; it is not yet a verified live URL.

## Still to verify

Scoped registration and credential renewal, provider selection through actual
CI/CD, application health over HTTPS, and live metrics collection remain pending.
The existing calculator's missing tests are a separate issue; this infrastructure
acceptance uses the existing tested fixture app. Native Octavia/L7 readiness is
not established by this VM/bootstrap result. The active runtime is retained for
the hackathon, so this is not a full resource-teardown claim.
