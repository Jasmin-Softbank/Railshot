# Runtime deployment registration

`deployment/scripts/environment.py register` connects the existing environment worker to the existing CI publication and Argo bridge. It does not provision a VM, install K3s, build an image, or claim a public site is reachable.

The environment owner first applies Terraform, enrolls SSH host keys through authenticated cloud transport, verifies `guest_ready` and `runtime_ready`, and optionally produces a private PostgreSQL binding. The registrar accepts that worker's existing private target registry. All paths and credentials originate from operator profiles, never HTTP request fields.

```sh
python3 deployment/scripts/environment.py register \
  --registry /private/environment/targets.json --target-id stack-aws-1002 \
  --config /private/environment/deployment.json --state-dir /private/environment \
  --binding /private/environment/database/binding.json
```

Omit `--binding` for stateless apps. Configuration is a mode-0600 JSON wrapper:

```json
{
  "version": 1,
  "cd": {
    "version": 1,
    "state_dir": "/private/cd-state",
    "repository": "/private/config-repo",
    "branch": "deployment/apps",
    "context": "railshot-control",
    "targets": {
      "stack-aws-1002": {
        "app": "stack-db-demo",
        "tenant": "demo",
        "target": {
          "id": "stack-aws-1002",
          "namespace": "app-stack-db-demo",
          "argocd_namespace": "argocd",
          "project": "railshot-stack-aws-1002",
          "architecture": "amd64",
          "repo_url": "https://github.com/Jasmin-Softbank/Railshot.git",
          "path": "gitops/applications/stack-db-demo/stack-aws-1002",
          "node_port": 30100,
          "ingress_cidrs": ["172.31.0.0/16"],
          "resources": {"requests": {"cpu": "100m", "memory": "128Mi"}, "limits": {"cpu": "500m", "memory": "512Mi"}},
          "image_pull_secret": {"namespace": "app-stack-db-demo", "name": "ghcr-pull"},
          "database": {"runtime_secret": "runtime-db", "migration_secret": "migration-db", "ca_secret": "database-ca"}
        },
        "public_http": {"url": "https://operator-reviewed.example/health", "expected_json": {"ok": true}}
      }
    }
  },
  "registration": {
    "state_dir": "/private/registrations",
    "source_repository": "Jasmin-Softbank/railshot-apps",
    "pull_secret_file": "/private/ghcr-docker-config.json"
  }
}
```

These addresses and allocations are illustrative. The operator supplies the actual Git checkout, routes, resources and public health expectation. Omit `target.database` for stateless registration. The registrar derives `target.cluster_server` from the verified descriptor's private IP and derives `target.database.host/port` from the private DB binding. An explicitly configured value must agree. Every target needs its own AppProject and namespace; an existing foreign object is not adopted. Git path segments must contain both the app and target ID.

The pull file is the existing Docker shape `{"auths":{"ghcr.io":{"auth":"<base64 of username:token>"}}}`. It must contain only the dedicated pull credential, not a publisher/admin token. Runtime and migration DB credentials go to distinct Secrets, with a third Secret for the CA. URLs use `verify-full` and `/etc/railshot/db/ca.crt`. Credentials never enter Git, stdout, or registration receipts.

When the domain owner's `gitops/edge.py` is installed, optional `registration.edge_config_file` enables its `prepare` CLI. `registration.expires_at` is then mandatory; use the resource's actual expiry (the new app+DB rehearsal uses a two-hour lifetime; October 5 at 23:59 KST is the outer cleanup deadline). With edge preparation, the input `public_http` may contain `health_path` instead of `url`; the allocator alone supplies the final URL. The helper passes descriptor-derived private IP, environment ID, tenant/app/namespace and health expectations. AWS's attached security group is discovered with authenticated `DescribeInstances`; specify `target_security_group_id` only to choose among multiple attached groups. The returned `node_port`, `public_http`, and `reference` enter `cd.json`. Registration checks all runtime Services for NodePort collisions. Edge preparation is allocation, not DNS/ALB application or HTTP verification.

The controller needs existing SSH/cloud tools, an operator kubectl context on the control cluster, private routing to the customer's Kubernetes API, and a GitHub token authorized to edit Actions variables in the source repository. It reuses SSM/IAP forwarding and strict known-host checks for guest kubectl. A six-hour TokenRequest credential is checked against the runtime TLS CA and namespace permissions, then passed directly to the existing Argo cluster Secret installer. Argo receives no cluster-resource rights. Runtime SA permissions cover Deployment/Service/NetworkPolicy writes, ReplicaSet/Pod/Event reads, optional migration Jobs, and only its own TokenRequest. Secret reads, another namespace's writes and cluster-role writes are checked as denied.

The platform owner bootstraps a RoleBinding from the product ServiceAccount to the fixed `argocd/railshot-product-registrations` Role, initially with `rules: []` (an empty `resourceNames` list inside a rule means unrestricted names and is rejected). A separate static Role grants get/update/escalate only on `railshot-product-registrations` and `railshot-credentials`, get/update of the named renewal ConfigMap, get of its CronJob, and create of Secrets/AppProjects/Applications in `argocd`. The registrar appends only the validated snapshot's exact project, cluster Secret and rendered Application names to the registration Role, with get/patch verbs. It preserves previous names, verifies the readback, and later appends the new cluster Secret to the renewal Role. Both replacements carry Kubernetes resourceVersion and run inside the same claim/lock and durable unknown-state handling as registration. Existing unrelated Secrets gain no read grant. The Application name is produced by the same helper as the GitOps renderer.

This is a trusted registration-controller boundary, not dynamic label-based Kubernetes RBAC. Native `resourceNames` cannot match a future prefix/label, and create cannot be restricted by name. Named-Role `escalate` restricts which Role can be updated; it does not constrain that Role's contents. A compromised controller could expand permissions inside `argocd`; no cluster-wide administrator or cross-namespace grants are introduced. Operators requiring enforcement of rule contents need an admission policy in addition to RBAC.

The existing `argocd/railshot-credentials` CronJob must already be installed and working. Registration merges the new policy target and Secret resource name into its ConfigMap/Role using Kubernetes resourceVersion, preserving other ConfigMap fields such as `kubeconfig`. It records the token expiry and the renewal configuration separately. A configured renewal policy is not proof that a future scheduled run has succeeded. Removal/expiry cleanup belongs to the platform owner and must remove this target's renewal grant as well as its cluster/project/CI binding.

`registration.state_dir` is shared by all environment workers on this control plane. One file lock protects target claims, GitHub variable writes and renewal-policy updates. A claim binds the target, physical resource, environment, app/config and credential-input hashes. Another environment cannot reuse it. `registration.json` durably records stages; after a partial mutation, it returns `unknown` and keeps previous stages. An operator may re-run the exact command to reconcile the same object identities; changed inputs fail closed. Successful duplicate requests return the stored receipt without creating objects. This does not change the product API's rule against automatic retries of unknown environment operations.

On complete registration, `cd.json` is atomically written in the existing bridge v1 format. The environment owner reads it for the next deployment without restarting the API. The registry row binds one application to one runtime; it does not introduce a general cluster registry or multi-node topology.

Optional `registration.observability_config_file` invokes the observability owner's `observability/register.py` after credential registration and before CI admission. The private request binds target/environment/app/namespace, descriptor-derived node IP, the allocated probe URL, the existing SSH registry and control context. The helper receives its exporter ports and shared observer from operator configuration and uses the same strict cloud transport. Its successful receipt must match the target/app and `registered: true`; the registrar still records `collection_state: pending` until real metrics/probes are observed. The source helper, API packaging and provider firewall prerequisites must be installed before enabling this option. No observer VM is created per app.

`RAILSHOT_TARGET_BINDINGS` is an additive Actions variable map:

```json
{"stack-aws-1002":{"app":"stack-db-demo","tenant":"demo","image_pull_secret":{"namespace":"app-stack-db-demo","name":"ghcr-pull"}}}
```

Both CI admission and publication read this map. A mapped target requires the exact app/tenant; its pull Secret overrides legacy global namespace/name variables. Unmapped existing targets retain the existing allowlist contract. Registration preserves every existing map entry and refuses a conflicting target binding.

Rollout order: merge the source, publish the API image containing the helper, update the source repository's `railshot-deploy.yml` from `ci/workflows/railshot-deploy.yml`, and pin `PLATFORM_REF` to that reviewed source. Then let the environment worker call registration. Setting the variable alone cannot update an older workflow or pinned Python module. The platform owner serializes these shared changes and verifies a newly registered target is consumed by a real CI run and Argo sync. Existing fixture URL success is not that evidence.

AWS and GCP use the same registration/namespace/CI contract and different existing SSH transports. Local tests cover both descriptor shapes. GCP private API routing and edge reachability still require separate live evidence. No OpenStack/proxmox registration or multi-node runtime support is claimed.

Checks: `python -m unittest discover -s deployment/scripts/tests -p test_environment.py -v`, plus existing publication/workflow tests. The tests exercise duplicate/conflicting identity, partial-resume state, foreign namespace preservation, Secret separation, existing renewal-policy preservation, and consumption by the real CI validation/publication functions. They do not claim cloud mutation or deployment.

The Kubernetes [ServiceAccount token documentation](https://kubernetes.io/docs/tasks/configure-pod-container/configure-service-account/) describes TokenRequest audiences and expiry; the existing implementation is retained. Argo's [declarative cluster registration](https://argo-cd.readthedocs.io/en/stable/operator-manual/declarative-setup/#clusters) specifies the cluster Secret, namespace restrictions and project binding used here.
