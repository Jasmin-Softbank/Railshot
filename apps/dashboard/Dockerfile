# Build from the repository root. Registry index digests verified on 2026-10-02.
ARG NODE_IMAGE=node:22-bookworm-slim@sha256:43ac6c60b8f89723f746e8a92ce91abd5017e627ce1ddfe4238355d3a30b772c
ARG NGINX_IMAGE=nginx:stable-alpine@sha256:0985e772fb9f729e6fa0980da05fca5d9c468e870eed43071545afa9d2e27d94
FROM ${NODE_IMAGE} AS build
WORKDIR /build
COPY package.json package-lock.json ./
COPY apps/dashboard/package.json apps/dashboard/package.json
COPY apps/api/package.json apps/api/package.json
RUN npm ci --ignore-scripts --no-audit --no-fund --workspace=apps/dashboard --include-workspace-root=false
COPY apps/dashboard/ apps/dashboard/
RUN npm run build --workspace=apps/dashboard

FROM ${NGINX_IMAGE} AS dashboard
COPY apps/dashboard/nginx.conf /etc/nginx/nginx.conf
COPY --chmod=0555 apps/dashboard/start.sh /usr/local/bin/railshot-dashboard
COPY --from=build /build/apps/dashboard/dist/ /usr/share/nginx/html/
USER 101:101
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s CMD wget -q -O /dev/null http://127.0.0.1:8080/healthz || exit 1
ENTRYPOINT ["/usr/local/bin/railshot-dashboard"]
