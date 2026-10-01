# Failure classes

The classifier (deterministic) assigns one class to the first failing gate step and decides the next action before any LLM is called. The fixer only receives classes marked "fixer".

| Class | Meaning | Typical signals | Next action | LLM attempts |
|---|---|---|---|---|
| F1 | Dependency resolution or install | `No matching distribution`, `ERESOLVE`, `ModuleNotFoundError` at build, missing system library | Rule table first, then fixer | 1 (LLMs are weakest here) |
| F2 | Build context | `COPY failed`, file not found in context, `.dockerignore` excludes a needed path | Rule table first, then fixer | up to N |
| F3 | Base image or architecture | `exec format error`, unknown base, disallowed base | Deterministic swap to an allowlisted base | 0 |
| F4 | Start, port, bind, health | container exits, connection refused, health path 404/5xx, timeout | fixer | up to N |
| F5 | Spec schema or policy violation | JSON Schema error, gate L1 rule violation, render error | fixer | up to N |
| F6 | CRITICAL vulnerability with a fix | Trivy CRITICAL | fixer (base or package version only) | up to N |
| F7 | Application code defect | stack trace in app code, failing app tests, missing app feature | Stop, report to user with the exact change needed | 0 |
| F8 | Transient infrastructure | registry 5xx, DNS timeout, runner lost, rate limit | Retry once without LLM | 0 |
| F9 | Infra plan error or policy | `terraform validate/plan` error, destroy or replace in plan, cost policy deny | fixer for spec errors; destroy/replace always stops for confirmation | up to N |
| INJ | Suspected prompt injection | agent reports INJECTION_SUSPECTED, or deny-listed phrases in logs sent to the agent | Stop, flag for review | 0 |

Loop stops on: gate pass, `give_up`, class F7/F8/INJ, the same normalized signature twice, or attempt N (N=3, team decision D2).

The signature is `layer + class + first meaningful error line` after removing paths, hashes, timestamps and numbers. The excerpt sent to the fixer is the first failing step's first error block, secrets masked, at most 4 KB.
