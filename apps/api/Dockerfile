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
# Preserve the local source-asset route for existing CLI/local HTTP clients.
COPY apps/dashboard/index.html apps/dashboard/app.js apps/dashboard/styles.css /app/apps/dashboard/
EXPOSE 4173
HEALTHCHECK --interval=30s --timeout=3s CMD node -e "fetch('http://127.0.0.1:'+(process.env.PORT||4173)+'/healthz').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"
CMD ["node", "src/server.js"]

FROM base AS mcp
# No HTTP listener: the MCP host must keep stdin open and use stdout for protocol messages.
CMD ["node", "src/mcp.js"]
