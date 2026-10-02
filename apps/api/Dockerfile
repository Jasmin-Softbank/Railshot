# API and stdio MCP share dependencies, but start separate processes.
# Build from the repository root. Registry index digest verified on 2026-10-02.
ARG NODE_IMAGE=node:22-bookworm-slim@sha256:43ac6c60b8f89723f746e8a92ce91abd5017e627ce1ddfe4238355d3a30b772c
FROM ${NODE_IMAGE} AS dependencies
WORKDIR /app
COPY package.json package-lock.json ./
COPY apps/api/package.json apps/api/package.json
COPY apps/dashboard/package.json apps/dashboard/package.json
RUN npm ci --omit=dev --ignore-scripts --no-audit --no-fund --workspace=apps/api --include-workspace-root=false

FROM ${NODE_IMAGE} AS base
ENV NODE_ENV=production
WORKDIR /app/apps/api
COPY --from=dependencies /app/node_modules /app/node_modules
COPY apps/api/package.json ./package.json
COPY apps/api/src/ ./src/
USER 1000:1000

FROM base AS api
USER root
ENV PATH=/opt/railshot-python/bin:${PATH}
COPY apps/api/runtime-install.sh apps/api/runtime-smoke.py apps/api/runtime-tools.json ./
COPY apps/api/prepare-state.js ./
COPY ci/requirements-dev.txt ci/requirements-test.txt /app/ci/
COPY gitops/bridge.py gitops/argo.py gitops/handoff.py /app/gitops/
COPY gitops/credentials.py /app/gitops/
COPY ci/scripts/execution.py ci/scripts/observability.py ci/scripts/process.py ci/scripts/publication.py ci/scripts/storage.py /app/ci/scripts/
COPY ci/scripts/gate/bundle.py /app/ci/scripts/gate/
COPY ci/scripts/runner/runtime_boundary.py /app/ci/scripts/runner/
COPY ci/scripts/runner/replenish.py /app/ci/scripts/runner/
COPY ci/scripts/schemas/jasmin.schema.json /app/ci/scripts/schemas/
COPY infrastructure/providers/terraform_tools/provision.py infrastructure/providers/terraform_tools/capabilities.py infrastructure/providers/terraform_tools/costs.py /app/infrastructure/providers/terraform_tools/
COPY infrastructure/terraform/aws/ /app/infrastructure/terraform/aws/
COPY infrastructure/terraform/gcp/ /app/infrastructure/terraform/gcp/
COPY infrastructure/ansible/run.py infrastructure/ansible/transport.py infrastructure/ansible/ansible.cfg infrastructure/ansible/guest.yml infrastructure/ansible/runtime.yml /app/infrastructure/ansible/
COPY infrastructure/ansible/tasks/guest-checks.yml /app/infrastructure/ansible/tasks/
COPY infrastructure/ansible/group_vars/all.yml /app/infrastructure/ansible/group_vars/
COPY contracts/ansible-request.schema.json /app/contracts/
COPY deployment/scripts/common.sh deployment/scripts/environment.py /app/deployment/scripts/
COPY deployment/bootstrap/preflight.sh deployment/bootstrap/install-k3s.sh deployment/bootstrap/health.sh /app/deployment/bootstrap/
COPY deployment/cilium/install.sh deployment/cilium/preflight.py deployment/cilium/health.sh /app/deployment/cilium/
COPY deployment/airgap/versions.json /app/deployment/airgap/
RUN RAILSHOT_CONTAINER_BUILD=1 bash runtime-install.sh
COPY --chmod=0555 apps/api/git-askpass.sh /usr/local/bin/railshot-git-askpass
ENV GIT_ASKPASS=/usr/local/bin/railshot-git-askpass GIT_TERMINAL_PROMPT=0 PYTHONDONTWRITEBYTECODE=1 CHECKPOINT_DISABLE=1
USER 1000:1000
RUN python3 runtime-smoke.py
# Preserve the local source-asset route for existing CLI/local HTTP clients.
COPY apps/dashboard/index.html apps/dashboard/app.js apps/dashboard/styles.css /app/apps/dashboard/
EXPOSE 4173
HEALTHCHECK --interval=30s --timeout=3s CMD node -e "fetch('http://127.0.0.1:'+(process.env.PORT||4173)+'/healthz').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"
CMD ["node", "src/server.js"]

FROM base AS mcp
# No HTTP listener: the MCP host must keep stdin open and use stdout for protocol messages.
CMD ["node", "src/mcp.js"]
