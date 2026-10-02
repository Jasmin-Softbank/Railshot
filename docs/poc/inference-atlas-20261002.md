# Inference Atlas AWS deployment — 2026-10-02

The verified static frontend is available at
[inference-atlas-c0f420a64777.railshot.io](https://inference-atlas-c0f420a64777.railshot.io/).
This is a separate application acceptance record from the earlier AWS/GCP fixture runs.

| Boundary | Verified identity / result |
| --- | --- |
| Original application | `mangowhoiscloud/inference-atlas@7493d10fcad47178e741c084951bba4aa0adc8e3` |
| Original frontend gate | [36962277972](https://github.com/mangowhoiscloud/inference-atlas/actions/runs/36962277972), all 11 checks passed, including production Chromium routes and unchanged source identity |
| Submitted package | `Jasmin-Softbank/railshot-apps@b4f6a8cb45b9db92b7c307a0ee31e0b810f504f9`, `apps/jihwan/inference-atlas` |
| RAILSHOT CI | [36962832731](https://github.com/Jasmin-Softbank/railshot-apps/actions/runs/36962832731), all six gates passed, static server tests 5/5, no SDK or model calls |
| Tested bundle | Artifact `11208807318`, run attempt 1 |
| Private image | `ghcr.io/jasmin-softbank/jihwan-inference-atlas-web@sha256:81c63de649c6862c4477af9ce0201f9492cecf435062f82095026aac5d54abba` |
| GitOps workload | `d490ba3acf2378d7fdef0bd8e0222dffc3f57cd3`, `gitops/applications/inference-atlas-c0f420a64777/k3s-aws` |
| Runtime | Existing AWS user K3s/Cilium node, namespace `tenant-jihwan-atlas`, NodePort `30081`; no new VM |
| Argo / Pod | `Synced`, `Healthy`, operation `Succeeded`; Pod running and ready; actual image ID matches the private published digest |
| Public delivery | Existing Route 53 zone, wildcard ACM certificate and ALB; dedicated hostname rule and healthy private IP target |
| HTTP content | Verified TLS, `/health` ready, all 9 delivered HTML/JS/CSS files match the original gate's SHA-256 manifest |
| Live browser | Chrome displayed the globe and CSS, navigated to the learning wiki, and reloaded the wiki route successfully |

## Scope and database boundary

The current React UI imports reviewed JSON snapshots and requires an empty API
base URL. The original frontend CI produced the bundle; RAILSHOT packaged those
exact bytes with a small Express static server. Original application source was
not modified or built in the local read-only clone. Packaging provenance and the
upstream quality receipt are retained in the private apps repository.

This package does not deploy the separate SQLite lab API, PostgreSQL, Patroni,
or a persistent volume. Atlas UI success does not verify the team's external DB
placement, connection, replication, or recovery contract. The Ansible
`patroni.install` path remains blocked until its team playbook and contract exist.

The static server runs without root, writable root filesystem, mounted service
account token, or runtime egress. Private image pull uses the pre-existing
operator-managed credential through a namespace-local pull Secret. Argo retains
the earlier fixture namespace and gains only the new workload namespace.

Private operator evidence under `cloud-e2e-20261002` contains
`atlas-argo-result.json`, `atlas-pod-observation.json`, `atlas-public-http.json`,
the unmodified CI receipts, and `inference-atlas-live.png`. Credentials and
Terraform state are not included in this repository.
