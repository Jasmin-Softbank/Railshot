# Railshot Agent Guidelines

## Core Principle

**Do not redesign or restructure the agreed Railshot repository layout without explicit team approval.**

## Repository Structure

Main source and documentation directories in the current checkout are shown below. Keep this tree as searchable text, not an image. Hidden configuration and generated dependency directories are omitted. The tree describes the checkout; it is not an instruction to create or move directories.

```text
railshot/
├── apps/
│   ├── agent/
│   ├── api/
│   └── dashboard/
├── ci/
│   ├── browser/
│   ├── policies/
│   ├── scripts/
│   ├── tests/
│   └── workflows/
├── deployment/
│   ├── airgap/
│   ├── bootstrap/
│   ├── cilium/
│   ├── cinder/
│   ├── cloudflared/
│   ├── cnpg/
│   ├── manifests/
│   ├── scripts/
│   └── sealed-secrets/
├── infrastructure/
│   ├── ansible/
│   ├── providers/
│   └── terraform/
├── gitops/
│   ├── applications/
│   ├── argo/
│   └── rollouts/
├── observability/
│   ├── dashboards/
│   ├── logs/
│   └── metrics/
├── docs/
│   ├── api/
│   ├── architecture/
│   ├── assets/
│   ├── decisions/
│   ├── demo/
│   ├── integration/
│   ├── logs/
│   ├── meetings/
│   ├── operations/
│   └── poc/
├── AGENT.md
├── README.md
└── README.ja.md
```

## Directory Responsibilities

| Directory | Responsibility |
| --- | --- |
| `apps/` | Frontend, API, MCP (Model Context Protocol) tools and servers, and agents. |
| `ci/` | Build, test, and container image pipelines, including workflows, policies, and scripts. |
| `deployment/` | Kubernetes runtime setup and application deployment after a Linux node has been provisioned. |
| `infrastructure/` | Provider provisioning, Terraform, and Ansible. |
| `gitops/` | Argo CD and GitOps configuration, applications, and rollouts. |
| `observability/` | Metrics, logs, and dashboards. |
| `docs/` | Architecture, API, decisions, and PoC documentation. |

## Repository Structure Rules

1. Do not arbitrarily change the agreed top-level repository structure.
2. Do not rename, move, merge, or delete existing directories without explicit team approval.
3. Do not add new top-level directories without explicit team approval.
4. Add feature code only under an existing directory that owns the relevant responsibility. If the agreed directory is missing or its ownership is unclear, propose the intended location before creating directories or placing code elsewhere.
5. If a structural change appears necessary, propose it first. Explain the reason, affected paths, and ownership impact, and wait for explicit team approval before making the change.
6. Existing code may be located outside the overview above. A mismatch does not authorize moving code or reorganizing the repository.
7. Do not unnecessarily modify code owned by another responsibility area. Keep changes limited to the requested scope.

## API Conventions

For new or changed HTTP API contracts, follow [docs/api/conventions.md](docs/api/conventions.md), based on the existing Hwagyun OpenStack controller. Use resource-oriented `/api/v1` routes and its response/error formats for new product APIs. Static resource names must be short lowercase plural nouns such as `builds`, `deployments`, `profiles`, and `plans`: no hyphens, underscores, camelCase compounds, or internal executor terminology. This is a local naming rule, not a REST or URI standard requirement. Keep the existing snake_case JSON format; do not rename HTTP headers or opaque external IDs to enforce route naming. Preserve documented legacy routes and internal Ansible contracts until their callers are explicitly migrated. Keep designs, implemented routes, generated OpenAPI, and HTTP verification results distinct; do not claim an endpoint exists from a design document alone.
