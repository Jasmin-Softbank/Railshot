# Railshot Agent Guidelines

## Core Principle

**Do not redesign or restructure the agreed Railshot repository layout without explicit team approval.**

## Agreed Repository Structure

The following layout is the agreed structure for Railshot. The `railshot/` label represents the repository root, regardless of the local checkout directory name. This document does not authorize creating, moving, or deleting directories to make an existing checkout match this layout.

```text
railshot/
├── apps/
│   ├── dashboard/
│   ├── api/
│   └── agent/
├── ci/
│   ├── workflows/
│   ├── policies/
│   └── scripts/
├── deployment/
│   ├── bootstrap/
│   ├── cilium/
│   ├── cloudflared/
│   ├── sealed-secrets/
│   ├── cnpg/
│   ├── manifests/
│   └── scripts/
├── infrastructure/
│   ├── providers/
│   │   ├── aws/
│   │   ├── openstack/
│   │   └── proxmox/
│   ├── terraform/
│   └── ansible/
├── gitops/
│   ├── argo/
│   ├── applications/
│   └── rollouts/
├── observability/
│   ├── metrics/
│   ├── logs/
│   └── dashboards/
├── docs/
│   ├── architecture/
│   ├── api/
│   ├── decisions/
│   └── poc/
├── AGENT.md
└── README.md
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
6. Existing code may be located outside the agreed layout. Do not move that code as part of this guidelines task, and do not treat a mismatch as permission to reorganize the repository during later work.
7. Do not unnecessarily modify code owned by another responsibility area. Keep changes limited to the requested scope.

## API Conventions

For new or changed HTTP API contracts, follow [docs/api/conventions.md](docs/api/conventions.md), based on the existing Hwagyun OpenStack controller. Use resource-oriented `/api/v1` routes and its response/error formats for new product APIs. Static resource names must be short lowercase plural nouns such as `builds`, `deployments`, `profiles`, and `plans`: no hyphens, underscores, camelCase compounds, or internal executor terminology. This is a local naming rule, not a REST or URI standard requirement. Keep the existing snake_case JSON format; do not rename HTTP headers or opaque external IDs to enforce route naming. Preserve documented legacy routes and internal Ansible contracts until their callers are explicitly migrated. Keep designs, implemented routes, generated OpenAPI, and HTTP verification results distinct; do not claim an endpoint exists from a design document alone.

## Scope of This Guidelines Task

Create or update only the root `AGENT.md`. Do not modify feature code, create the documented directories, or change the actual directory structure. The tree above records the agreement; it is not a migration instruction.
