# Railshot naming and existing installations

New workload specifications use `.railshot/railshot.yaml`, `apiVersion: railshot/v0`, and `ci/scripts/schemas/railshot.schema.json`. Existing `.jasmin/jasmin.yaml` and `jasmin/v0` remain readable. The agent repairs an existing legacy spec in place. A workspace with both specifications is rejected.

New tested bundles and published artifacts use `railshot.yaml`. Historical `jasmin.yaml` artifacts retain their original filenames, bytes, and manifest/handoff hashes. Naming migration never rewrites an existing publication or source digest. Deploy the updated API/GitOps consumers first, then the apps workflow that reads either filename and a verified `PLATFORM_REF` containing this change, before submitting new `.railshot` sources.

New API/CLI/MCP configuration uses `RAILSHOT_TENANT`, `RAILSHOT_API_URL`, and `RAILSHOT_SOURCE_ROOT`. Corresponding `JASMIN_*` variables remain fallback aliases; `RAILSHOT_*` takes precedence. The request header is `x-railshot-request: deploy`. The server also reads the old `x-jasmin-request`, and CLI/MCP send both for compatibility with existing servers. Endpoints and payload fields remain unchanged.

These identities are preserved pending a separate, verified migration:

| Identity | Reason |
| --- | --- |
| GitHub `Jasmin-Softbank`, GHCR `ghcr.io/jasmin-softbank` | Actual external organization and registry namespace. |
| Historical `Jasmin` repository, validation records and pinned commit links | Original evidence and provenance. |
| `/opt/jasmin/bootstrap`, `/etc/jasmin`, `/var/lib/jasmin/bootstrap`, `.jasmin-install.json` | Installed code ownership, credentials, and progress state. |
| `jasmin0`, `wg-quick@jasmin0`, `/etc/wireguard/jasmin0.conf` | Installed tunnel, peers, routes, and systemd unit ownership. |
| `jasmin-job-v1`, `JASMIN_ENROLLMENT_TOKEN` | Installed SSH forced command and bootstrap enrollment interface. |
| etcd `jasmin` data/config and `.jasmin-managed`; `jasmin-backup` units; Grafana `jasmin-patroni` UID | Persistent database, service and dashboard identities; renaming could orphan or duplicate them. |
| OpenStack resource prefixes and enrollment names; historical CodeBuild `Jasmin` source binding | Existing cloud objects and source binding require live reconciliation. |

This change does not move runtime data or create/delete cloud resources.
