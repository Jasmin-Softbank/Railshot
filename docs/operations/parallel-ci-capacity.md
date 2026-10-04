# Existing-host parallel CI

Status: local implementation and verification; live 12-runner capacity is not yet verified.

## Limits and ownership

| Layer | Limit | Owner |
| --- | ---: | --- |
| API admitted deployments | 3 by default, configurable 1..64 | `RAILSHOT_MAX_CONCURRENT_DEPLOYMENTS` |
| Ephemeral runner workflows on the existing build node | 12 target, default remains 1 until configured | operator worker config `runner_count` |
| Heavy gate executions across those runners | 2 | root-owned `/etc/railshot/ci-executor.yaml`, `concurrency.gate` |
| Shared Codex subscription calls | 1 | same profile, `concurrency.agent` |
| Source Git ref writer | 1 | API process |
| Native registration/CD shared writer | 1 | API process |

No new VM is required. The existing AWS build node has 4 vCPU and 16 GiB RAM. Twelve concurrent workflows are not twelve full-speed builds: resource-heavy gates wait for host capacity. BuildKit retains its existing 2 CPU / 4 GiB bound. Runner container requests/limits remain enforced separately; Docker child workloads are not included in Kubernetes quota.

After a runner exits, its disposable checkout is removed; abrupt host failure/SIGKILL can still leave a directory that needs operator reconciliation. Durable evidence is not removed by this cleanup.

Each ephemeral runner uses its own `work/<runner-name>` checkout, temporary directory and lock. Durable run evidence remains under the separate run root. The shared Codex home is protected by a cross-process lock; private customer execution boundaries remain unchanged. Capacity wait emits a diagnostic, has a bounded timeout, and never reports a successful gate without running it.

## Queue and recovery

The existing SQLite queue remains the source of operation state. CI keeps its admission slot between asynchronous observations. Additional accepted requests queue durably. Without an explicit override, the first three deployments retain slots through CI/CD and the fourth waits in FIFO order. The production manifest must preserve the same three-slot limit. Same-app operations remain ordered across environments. Source branch updates and infrastructure writes have separate short serial writers. Shared native CD mutations are still serialized.

The existing CronJob controller creates unique ephemeral Jobs on the same approved node. `runner_count` adds slots, not nodes. A durable per-slot intent is written before creation. An uncertain create reserves that slot while unrelated slots can proceed. Three failed runner Jobs stop that slot; they do not trigger duplicate deployment. Legacy active runners drain before the new private workspace layout is used. Shrinking a pool with retained slot state requires operator reconciliation.

## Account consumption

Customer baseline gates use self-hosted runners. Model calls occur only for eligible repair and retain separate packaging/fixer budgets. Single-call concurrency protects shared credentials but does not guarantee a subscription cannot exhaust its daily or weekly quota. GitHub-hosted publication and platform workflows are separate consumers and must be reported separately.

Progress snapshots are coalesced at 60-second intervals (final completion bypasses this interval, but never a rate-limit backoff). Twelve continuously active jobs therefore produce at most 720 periodic updates/hour; initial and final publications add requests. Detailed local run logs are unaffected.

API status polling scales its interval with admitted CI operations, so additional jobs do not multiply the aggregate polling rate. The API preserves 100 remaining requests as an operator reserve when rate-limit headers are present, and defers requests until the upstream reset/Retry-After deadline. GitHub API exhaustion must preserve the last observed state and obey upstream backoff; a missing response is not a successful deployment.

## Rollout and evidence

1. Pass scheduler, controller, host semaphore, sandbox and platform rendering regressions.
2. Publish reviewed API/runner images and update the trusted workflow pin through the existing platform release procedure.
3. Update the existing host executor profile through its Ansible owner and reverify its network/sandbox receipt. Set `runner_count: 12` in the private worker promotion config, so future promotions retain the setting. Merely editing a rendered ConfigMap is insufficient.
4. Verify actual available disk/memory, runner startup, >=12 distinct overlapping workflow jobs on the same node, and heavy gate peak <=2. Check account rate/usage before and after.
5. Verify the fourth deployment queues at the default limit, same-app order, restart observation, uncertain create isolation and exact artifact/CD binding. Report live customer URL verification separately from runner capacity.

Read-only host measurement on 2026-10-04: 4 CPUs, 15,783 MiB RAM, 14,568 MiB available; 58 GiB root filesystem, 17 GiB available. This snapshot is not a load-test result. Avoid retaining disposable checkouts indefinitely; preserve durable evidence independently.

## Account audit, 2026-10-04

Production API credential `/rate_limit`: core limit 5,000, remaining 5,000 at observation. This is a point-in-time measurement, not a guarantee for a later burst. No credential was exported.

Organization billing usage response: Actions Linux 6,253 minutes, net amount USD 0; Actions storage 868.261 GigabyteHours, net amount USD 0. The response does not establish the remaining included allowance or a spend ceiling. Customer `loop` is self-hosted; publication/platform workflows still include GitHub-hosted jobs. See [GitHub Actions billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions).

The CI subscription's remaining model allowance has not been verified. Do not substitute this desktop's Codex quota for the CI account. Model call serialization and existing repair-attempt limits reduce consumption but do not constitute a daily/weekly budget guarantee.
