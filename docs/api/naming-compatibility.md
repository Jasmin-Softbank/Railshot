# Railshot naming and existing installations

New workload specifications use `.railshot/railshot.yaml`, `apiVersion: railshot/v0`, and `ci/scripts/schemas/railshot.schema.json`. Existing `.jasmin/jasmin.yaml` and `jasmin/v0` remain readable. The agent repairs an existing legacy spec in place. A workspace with both specifications is rejected.

New tested bundles and published artifacts use `railshot.yaml`. Historical `jasmin.yaml` artifacts retain their original filenames, bytes, and manifest/handoff hashes. Naming migration never rewrites an existing publication or source digest. Deploy the updated API/GitOps consumers first, then the apps workflow that reads either filename and a verified `PLATFORM_REF` containing this change, before submitting new `.railshot` sources.

The API and CLI retain `RAILSHOT_TENANT` and `RAILSHOT_API_URL` with corresponding `JASMIN_*` fallback aliases. The current MCP server in `apps/agent` uses `RAILSHOT_API_URL` and the versioned deployment API; it accepts public GitHub URLs rather than local source paths. The legacy CLI sends `x-railshot-request: deploy` and `x-jasmin-request: deploy` for compatibility with existing servers. Historical endpoint and payload compatibility remains in the API and CLI.

WireGuard bootstrap enrollment is retired. `RAILSHOT_ENROLLMENT_TOKEN` and `JASMIN_ENROLLMENT_TOKEN` are discarded before installation operations; neither enables registration. New CLI help, OpenStack object descriptions and temporary cloud configuration use Railshot. New etcd units display `Railshot managed etcd`; initialized members retain their exact old description so a cosmetic change does not trigger the existing configuration-drift guard. The guard still compares every other service/config byte and TLS input.

These identities are preserved pending a separate, verified migration:

| Identity | Reason |
| --- | --- |
| GitHub `Jasmin-Softbank`, GHCR `ghcr.io/jasmin-softbank` | Actual external organization and registry namespace. |
| Historical `Jasmin` repository, validation records and pinned commit links | Original evidence and provenance. |
| `/opt/jasmin/bootstrap`, `/etc/jasmin`, `/var/lib/jasmin/bootstrap`, `.jasmin-install.json` | Installed code ownership, credentials, and progress state. |
| `jasmin0`, `wg-quick@jasmin0`, `/etc/wireguard/jasmin0.conf` | Installed tunnel, peers, routes, and systemd unit ownership. |
| `jasmin-job-v1` | Installed SSH forced command. |
| etcd `jasmin` data/config and `.jasmin-managed`; `jasmin-backup` units; Grafana `jasmin-patroni` UID | Persistent database, service and dashboard identities; renaming could orphan or duplicate them. |
| OpenStack resource prefixes and enrollment names; historical CodeBuild `Jasmin` source binding | Existing cloud objects and source binding require live reconciliation. |

This change does not move runtime data or create/delete cloud resources.
