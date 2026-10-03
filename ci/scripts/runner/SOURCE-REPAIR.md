# Source repair in customer CI

The customer workflow defaults to `REPAIR_SCOPE=source`. `REPAIR_SCOPE=packaging`
retains the prior restricted mode, and direct CLI callers still default to it.
Set the apps repository's `PLATFORM_REF` to the reviewed platform commit and
update its workflow from `ci/workflows/railshot-deploy.yml`. A saved run is bound
to its original harness and source; start a new request after changing these
bindings instead of resuming an old failed run with different rules.

Deployment requires the selected environment, exact app/image identity, a successful
build and real runtime/public access. Q quality results remain visible but missing
tests/checkers and completed lint/type/unit failures do not block release. Q
isolation, source snapshot, evidence-write, unknown execution and cleanup failures
still block. Publish the API validator update before emitting advisory artifacts.
Initial adaptation uses packaging scope; source scope is granted only after an
observed L2 build or L3 runtime failure. Do not generate tests, checker setup,
features or unrelated refactors for deployment.
The workflow also passes the registered `APP` as `--app-id`: the durable binding,
agent task and L1 check use that exact identity. A spec mismatch fails before
quality/build work and remains repairable as F5; publication checks it again.

An early failure starts a bounded proposal, not a pass. Every proposal contains
an ordered `gate_plan` for the supplied deployment gates (currently **L0 → L1 → L2 → L3**, optionally L4 before L3), the failure evidence,
and file-level reasons. The runner durably records `<role>-plan.json` before
applying any bytes. The loop stores it as `<role>-<attempt>-plan.json`, checkpoints
it, and includes its hash, gate list and subsequent written files/verdict in
`evidence.json`. Workflow artifacts expose that safe linkage; source text and
raw model planning text remain in the private run directory. Every changed
attempt reruns the deterministic gates from L0. The default is at most two SDK
attempts, with three available through `RAILSHOT_MAX_REPAIR_ATTEMPTS`; an unchanged failure signature stops the run sooner.

A completed model response rejected by schema or patch validation can consume a
remaining attempt for a fresh fixer plan only when the runner proves zero source
writes. Safe registered validation guidance is recorded in `rejection-N.json`
and the next failure context. Invalid/partial filesystem writes, authentication,
native policy failures and unknown execution outcomes never use this path.
Rejected attempts count toward the same limit, and checkpoint/resume does not
repeat the completed model call. A corrected proposal still reruns every gate.

| First failure | Authorized remediation | Required evidence afterward |
| --- | --- | --- |
| L0 prohibited patch | Revise proposal within unchanged platform limits | Full patch policy; scoped regular-text deletion only, no protected/escaped paths, secret files or bypasses |
| L1 missing/invalid Dockerfile or service spec | Generate the smallest packaging proposal | Schema, port, image and service contract |
| Q missing tests/checkers or completed lint/type/unit failure | Record the original result as advisory and continue; no model repair | Required build, scan and runtime checks still pass |
| Missing JS dependency lock / additive dependencies | Native npm/pnpm/Yarn resolution in existing filtered-network Docker sandbox | External hash-bound native lock receipt, then frozen install and full gates |
| L2 install/context/architecture error | Correct Dockerfile, build context, source or additive dependencies | Build succeeds under existing isolation and allowlisted base constraints |
| L4 fixed vulnerability | Correct the permitted base/package packaging | Scanner passes unchanged policy |
| L3 start, port, real health or source error | Correct source, command or deployment spec | Real app runtime and contract health checks pass |
| Runner, Docker, network/auth/secret absence, uncertain execution | Stop with the actual operator action | Infrastructure recovery evidence before a new execution |

The model retains read-only, network-disabled tools. It proposes at most eight
files and 20 KB per patch, including original bytes of deleted files. Creation/update uses the full content; deletion uses `action: delete` with empty content. File actions are recorded before applying them. Existing tests, scripts (except the exact npm empty-test
placeholder), dependency versions, checker configuration, migrations and schemas
are immutable. The underlying source validator accepts additive manifest test setup and exact
public package versions; it never accepts model-authored locks. Native locks have
a separate 4 MiB bound and must match both the recorded bytes and manifest.
The resolver uses the same non-root, capability-free, read-only source mount,
resource limits and approved network as quality execution. No credential,
Docker socket, host write, cluster or cloud permission is granted to the model.

The structural new-test check is a minimum, not independent proof of coverage:
the model must derive expected behavior from the application, and test execution
must verify it. Custom unsupported test runners, changes to existing oracle or
checker policy, destructive migrations, unsupported build roots/toolchains, and
missing Python/Java native lock contracts still require reviewed configuration.
Source repair is not an unconditional promise to deploy every repository.

The injected `agents/DONT.md` lists the exact supported test scripts and JavaScript
import/assertion forms before the first model call. Runtime flags such as
`node --experimental-strip-types --test` remain unsupported; existing Vite can
load TypeScript modules from a native Node `.test.mjs` instead. Literal local Vite
loads, dynamic `import(new URL(...))`, and named Node assertion imports are accepted
by the same writer and L0 checks. Missing application references, missing assertions
and unsupported scripts produce separate, concrete correction guidance. These
syntax checks do not replace execution or protect against every vacuous test.

While a Codex call runs, native SDK events update content-free progress counters
in the existing private lifecycle receipt at most once per five seconds (plus a
terminal flush). These include item types/status, elapsed time, token counts and
the last SDK event time, never commands, responses or reasoning text. The loop
projects a run/attempt-bound snapshot to GitHub Actions stderr every 20 seconds
as `agent.heartbeat`; `sdk_activity_since_previous` and `last_sdk_event_age_ms`
distinguish new SDK activity from a live process waiting without new events.
`agent.observation` records the process return. These observations never imply
gate success. The trusted parent also publishes at most 60 recent safe events to
a `Railshot agent events` GitHub Check, bound to the source commit, workflow run,
run attempt, tenant, app and target. Its completion is neutral, not gate success.
The job's short-lived `checks:write` token is removed from the environment before
child processes run; transport failure never changes the gate result or repeats
a model call. Private native logs retain the full history.

The session-owned `GET /api/v1/deployments/{id}/events` API verifies those bindings
and the GitHub Actions producer against the latest workflow attempt. The existing
dashboard work-log tab polls it with deployment status and displays content-free
SDK progress, delayed observations and missing records separately. An older
attempt is never used as fallback. Runs from before this transport was enabled
have no fabricated event history. The API's bounded 15-second cache and the
dashboard's 15-second polling can delay visibility beyond the 20-second producer
interval; this is progress observation, not a subsecond stream.

`test_source_repair.py` executes generated calculator tests with real Node,
demonstrates that a wrong arithmetic implementation fails, checks native npm lock
generation/receipt tampering, and tests every gate's repair decision and complete
plan requirement. Docker transport is mocked in those local checks. A real
customer workflow and deployment receipt are separate production evidence.
