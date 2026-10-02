# Source repair in customer CI

The customer workflow defaults to `REPAIR_SCOPE=source`. `REPAIR_SCOPE=packaging`
retains the prior restricted mode, and direct CLI callers still default to it.
Set the apps repository's `PLATFORM_REF` to the reviewed platform commit and
update its workflow from `ci/workflows/railshot-deploy.yml`. A saved run is bound
to its original harness and source; start a new request after changing these
bindings instead of resuming an old failed run with different rules.

An early failure starts a bounded proposal, not a pass. Every proposal contains
an ordered `gate_plan` for **L0 → L1 → Q → L2 → L4 → L3**, the failure evidence,
and file-level reasons. The runner durably records `<role>-plan.json` before
applying any bytes. The loop stores it as `<role>-<attempt>-plan.json`, checkpoints
it, and includes its hash, gate list and subsequent written files/verdict in
`evidence.json`. Workflow artifacts expose that safe linkage; source text and
raw model planning text remain in the private run directory. Every changed
attempt reruns the deterministic gates from L0. There are at most three model
attempts; an unchanged failure signature stops the run sooner.

A completed model response rejected by schema or patch validation can consume a
remaining attempt for a fresh fixer plan only when the runner proves zero source
writes. Safe registered validation guidance is recorded in `rejection-N.json`
and the next failure context. Invalid/partial filesystem writes, authentication,
native policy failures and unknown execution outcomes never use this path.
Rejected attempts count toward the same limit, and checkpoint/resume does not
repeat the completed model call. A corrected proposal still reruns every gate.

| First failure | Authorized remediation | Required evidence afterward |
| --- | --- | --- |
| L0 prohibited patch | Revise proposal within unchanged platform limits | Full patch policy; no deleted files, escaped paths, secret files or bypasses |
| L1 missing/invalid Dockerfile or service spec | Generate packaging and inspect downstream source/test needs in the same proposal | Schema, port, image and service contract |
| Q missing/zero-collected unit tests or test script | Add behavior tests importing application code; add native Node/Jest/Vitest script, replace only empty/npm no-test placeholders | Positive executed tests, no skips; constant-only assertions rejected; a changed application must still fail those assertions |
| Q missing checker/reporter setup | Add missing supported test/typecheck script or exact public dependencies; use existing platform checker overlay; existing unsupported custom test scripts remain a configuration block | Original lint/type policy plus supported native reporter |
| Q lint, compile/type or unit error | Fix application source; original and previously generated tests stay immutable | Original quality commands and assertions pass |
| Missing JS dependency lock / additive dependencies | Native npm/pnpm/Yarn resolution in existing filtered-network Docker sandbox | External hash-bound native lock receipt, then frozen install and full gates |
| L2 install/context/architecture error | Correct Dockerfile, build context, source or additive dependencies | Build succeeds under existing isolation and allowlisted base constraints |
| L4 fixed vulnerability | Correct the permitted base/package packaging | Scanner passes unchanged policy |
| L3 start, port, real health or source error | Correct source, command or deployment spec | Real app runtime and contract health checks pass |
| Runner, Docker, network/auth/secret absence, uncertain execution | Stop with the actual operator action | Infrastructure recovery evidence before a new execution |

The model retains read-only, network-disabled tools. It proposes at most eight
files and 20 KB per patch. Existing tests, scripts (except the exact npm empty-test
placeholder), dependency versions, checker configuration, migrations and schemas
are immutable. Source scope accepts additive manifest test setup and exact
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

`test_source_repair.py` executes generated calculator tests with real Node,
demonstrates that a wrong arithmetic implementation fails, checks native npm lock
generation/receipt tampering, and tests every gate's repair decision and complete
plan requirement. Docker transport is mocked in those local checks. A real
customer workflow and deployment receipt are separate production evidence.
