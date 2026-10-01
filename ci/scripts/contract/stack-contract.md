# Jasmin Stack Contract v0

This file describes the current CI workload admission contract. Agents propose changes and deterministic gates decide CI success. It does not select or install a customer runtime, ingress, database operator or CD controller.

## 1. What an agent may produce

| File | Purpose |
|---|---|
| `Dockerfile`, `<service>.Dockerfile` | Container build for a service |
| `.dockerignore` | Keeps secrets, VCS data and build junk out of the context |
| `.jasmin/jasmin.yaml` | Workload spec (`schemas/jasmin.schema.json`) |

By default only these packaging files are writable. A trusted operator may enable bounded source repair for observed Q checker failures; `paths.yaml` remains the exact authority and tests, manifests, locks, migrations, policy and generated files stay protected. Agents do not produce Kubernetes manifests, Terraform values, DNS records or certificates; those belong to separate target owners.

## 2. Container rules

| ID | Rule | Gate layer |
|---|---|---|
| C1 | Builds with `docker buildx build --platform linux/amd64` from the declared context. | L2 build |
| C2 | Base images come from the allowlist in §5. The final stage is a slim, distroless or unprivileged variant. L2 records built image IDs for scan, runtime and artifact release; automatic rewriting of FROM tags to digests is not implemented. | L1 static / L2 evidence |
| C3 | The final stage sets `USER` to a numeric non-root UID of 10000 or higher (default 65532). | L1, L4 |
| C4 | The process listens on `0.0.0.0` and on the port declared in `jasmin.yaml` (1024–65535). | L3 readiness |
| C5 | The declared health path answers HTTP 2xx or 3xx within 60 s of start, without auth and without side effects. The platform probe decides; a Dockerfile `HEALTHCHECK` is rejected by L1. | L3 readiness |
| C6 | No secrets in the image, build args or context: no `.env*`, keys, tokens or credential files. `.dockerignore` excludes `.git`, `.env*`, `node_modules`, caches and build outputs. | L0, L4 |
| C7 | No CRITICAL vulnerability that has a fixed version. Fix it by moving to a newer base image or package version; ignore files are forbidden. | L4 conformance |
| C8 | Image size is at most 1 GiB compressed. | L4 |
| C9 | Build-time data generation (building a database from JSON, exporting frontend data, compiling assets) runs in a build stage; results are copied into the final stage read-only. | L2 |
| C10 | One main process per container. No `sshd`, no process supervisor unless the runtime requires it. | L1 |
| C11 | Each declared route answers from the running service, not only from a static placeholder. | L3 behavior |

## 3. Workload spec rules

- One entry in `services` per independently running process. A static frontend is its own service (served by an unprivileged web server image) unless another service already serves it.
- At least one service has a `route`. Service names must be unique. Target routing is configured separately.
- `env` holds non-secret values only. Secret names go in `secrets`; runtime injection is a separate target contract. The CI currently blocks required external secrets.
- Do not infer deployment resources from the CI limits in §4. The spec records facts about the app and explicit user choices, nothing else.
- `resources` may only request what `catalog.yaml` offers.

## 4. CI limits and target boundary

L1 validates schema, unique service names and reserved PORT/database bindings. Static replicas remain limited to 1–3 and size to S/M/L. Autoscaling requests are rejected as outside the current CI support; there is no controller selection flag or deployment resource generation.

L3 runs the tested image in an isolated ephemeral container environment. When requested, it starts temporary PostgreSQL 17, uses owner permissions for migration and a restricted runtime role for the app, and checks the declared health/routes. This does not choose a production DB operator, order a real rollout, verify backups or recover customer data.

A complete gate verdict binds source/spec and tested image identities. The trusted publisher exports or publishes those same images and returns remote digests. The current workflow hands the original spec, verdict and bundle manifest plus `images.json` to CD. It does not assign domains, issue certificates, configure Gateway/ALB, install a cluster or claim deployment success. See [registry contract](registry.md).

## 5. Base image allowlist

| Use | Images |
|---|---|
| Python | `python:3.12-slim`, `python:3.13-slim` |
| Node.js | `node:22-slim`, `node:24-slim` |
| Go, Rust (build stage) | `golang:1`, `rust:1` |
| Final stage, static binaries | `gcr.io/distroless/static-debian12`, `gcr.io/distroless/base-debian12` |
| Static web | `nginxinc/nginx-unprivileged:stable-alpine` |
| JVM | `eclipse-temurin:21-jre` runtime; reviewed Maven/Gradle builder profiles in `catalog.yaml` |

## 6. Forbidden changes

- Editing, renaming or deleting tests, CI configuration, lockfiles, dependency manifests, policies, this contract, or agent instruction files (`paths.yaml`).
- Weakening a check: `|| true`, `exit 0` in commands, skipped tests, `--no-verify`, `.trivyignore`, lower scan severity, a health path that stays green while the app is down.
- Changing application source code without trusted source-repair scope and an observed eligible Q failure. Scope cannot be granted by uploaded code or the model.
- Fetching and executing remote scripts during the build (`curl … | sh`).
