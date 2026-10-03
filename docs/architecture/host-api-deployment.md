# Host API with Nginx on EC2

The dashboard is static content. The API is a long-running Node.js process on the same host, bound to `127.0.0.1:4173`; Nginx serves `apps/dashboard/dist` and proxies `/api/` to it. The OpenStack installer endpoint reads the repository's `deployment/bootstrap`, `infrastructure/providers/openstack`, and `apps/agent` files, so the deployed checkout must include them.

1. Install Node 22 or newer. In the deployed Railshot checkout, run `npm ci --ignore-scripts --no-audit --no-fund` and `npm run build`.
2. Create a dedicated OS user and private, persistent directories for `RAILSHOT_STATE_DIR` and the operator token file. Set `RAILSHOT_API_TOKEN_FILE` to that file. The API user must be able to read the token and write only its state directory.
3. Run `npm start --prefix apps/api --workspaces=false` under systemd with `RAILSHOT_BIND_HOST=127.0.0.1`, `PORT=4173`, `RAILSHOT_ALLOWED_HOSTS` set to the public hostname, and `RAILSHOT_ALLOWED_ORIGINS` set to its HTTPS origin. Add the existing GitHub and deployment settings only for functions used in that environment.
4. Configure host Nginx to serve `apps/dashboard/dist`, preserve the original Host and Origin headers, and proxy `/api/` to `http://127.0.0.1:4173`. The private Nginx configuration must add `Authorization: Bearer <operator token>` to upstream requests; never place that token in HTML, JavaScript, or a public location. Limit upload size to at least 101 MiB and allow the plan endpoint's longer timeout.
5. Check the local API `/healthz`, then the public same-origin `/api/v1/sessions` and installer download through Nginx. `configured: true` on `/healthz` does not prove external GitHub or OpenStack access.

`deployment/compose.yaml` is an optional local dashboard, API, and MCP container environment. The host Nginx path above does not require Docker Compose.

The existing platform K3s release uses the API image built from `apps/api/Dockerfile`. The host process described here is an alternative deployment path; that container release does not install or update the host process.
