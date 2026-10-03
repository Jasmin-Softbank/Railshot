# Bound deployment diagnostics

The model-free CI baseline explicitly uses `RAILSHOT_MAX_REPAIR_ATTEMPTS=0` and
packaging scope. The workflow fallback from PR #126 is two attempts/source;
repository variables override it, while direct CLI defaults remain zero/packaging. CI emits
redacted diagnostic artifacts and source snapshots independently of gate verdicts.
The API joins those facts with dispatch, publication, GitOps revision, controller,
workload and public HTTP observations in the existing SQLite operation record.
Jev classification is an asynchronous hypothesis; it cannot retry, modify source,
publish an image or mark a deployment successful.

## Architecture decision

Use one versioned JSON event/error contract with Python and Node emitters. A single
logging class across Python/Node/subprocess boundaries would add coupling without
sharing execution state. `ci/scripts/contract/telemetry.json` defines outcomes,
severity numbers, error categories, retention and the classification rubric. The
existing operation state remains authoritative; its changes are projected into a
bounded journal. No second deployment state machine or logging service is added.

This follows the separation of event time, observed time, severity and attributes
in the [OpenTelemetry Logs Data Model](https://opentelemetry.io/docs/specs/otel/logs/data-model/),
structured JSON fields in [Cloud Logging](https://docs.cloud.google.com/logging/docs/structured-logging),
and request correlation in [AWS structured logging](https://docs.aws.amazon.com/lambda/latest/dg/monitoring-cloudwatchlogs-logformat.html).
[OTel CI/CD conventions](https://opentelemetry.io/docs/specs/semconv/cicd/cicd-spans/)
informed run/attempt/source correlation. They are not claimed as a stable exported
OTel trace contract: there is no collector or fabricated trace/span identifier.

Events contain schema version, event ID/name, source/observed/ingested timestamps,
sequence, component, phase, outcome, severity, correlation and evidence references.
Errors retain safe code/category/origin, retry policy and action; retry advice is
separate from `retry_decision`. Python cause chains contain safe error types/codes,
not exception text. Application, infrastructure, unavailable evidence and model
hypotheses remain separate. Logging failures never create a passing gate.

## Retrieval and retention

- `GET /api/v1/deployments/{id}/events`: current CI progress plus `timeline` of
  collected phase observations. Deduplicated producer identity and observed time
  preserve delayed delivery without rewriting execution status. Collection freshness
  is explicit; missing transport remains unavailable, not success.
- `GET /api/v1/deployments/{id}/diagnostics`: collects verified failure facts and
  returns existing classification state. This GET never calls a model.
- `POST /api/v1/deployments/{id}/classifications` with `{}`: reserves one durable
  classification per exact input digest. HTTP 202 means running; 200 means existing
  result, unavailable evidence or unconfigured classifier. Terminal CI failures may
  schedule the same operation automatically. Restart marks unfinished calls unknown
  and never replays them. Lost responses are not retried. Explicit 429/529 rejection
  may be retried once within the same 20-second deadline.
- `GET /api/v1/deployments/{id}/source?variant=failed`: downloads the exact captured
  failed source only when source-before/source-after and artifact/snapshot/tree hashes
  agree. A diagnostic snapshot is not gate-passing source or release authority.

All routes authorize the owning session before looking up the persisted
operation/run/app/target/source binding. GitHub workflow, branch, repository,
producer attempt and head SHA must match before and after artifact downloads.
Cached classifications recheck the current attempt before spending a model call.
Archive redirects strip authorization and permit only known HTTPS artifact hosts.
The reader rejects links, path traversal, duplicates, excess entries/bytes, forged
policy hashes and invalid source references. An unavailable artifact has explicit
missing evidence; older attempts are never used as fallback.

GitHub diagnostics expire after seven days. The journal keeps at most 240 events
and seven days per operation; truncation is explicit. At most 100 operations bound
the existing store. Private diagnostic payloads expire on startup and before reuse;
public event retrieval filters expired entries. At most eight classification inputs
are retained per operation. Regex redaction covers supported key/token patterns,
credentials in URLs and secret assignments; it is not universal secret detection.
No command argv, environment, raw reasoning or provider error body is exported.

## Classifier and repair boundary

The fixed [TypeSafe endpoint and typed choice contract](https://docs.typesafe.ai/api)
use `jev-1.13.0`, a bounded redacted input, versioned rubric and exact candidate keys.
Every probability must be finite, in range, sum to one and select a maximal entry.
Selected evidence must be one of the supplied references. Model/input/rubric/case/
source hashes, request ID and usage (when returned) are persisted. A probability is
not proof of cause or a repair success criterion.

The model key is injected only into the API from dedicated `railshot-classifier`
secret key `api-key`; it is removed from process.env after constructing the client,
so deployment subprocesses do not inherit it. Dashboard, runner and init containers
do not mount this secret. The encrypted GitHub Production environment secret is the
operator provisioning source. No value belongs in source, SSM plaintext payloads,
command arguments, reports or provider logs.

Optional repair separately verifies exact case/source/policy identity at proposal
recording and immediately before application, including real file-line/log hash
references. Source/policy changes or fabricated citations reject all writes. These
are reference-integrity checks, not semantic verification. Dynamic profiles require
exact ordered gate plans; Q/L4 disabled by the profile are NOT_RUN. The default L3
check verifies runtime health, not application business behavior.

CD workload facts use the existing application-scoped credential and GET-only
requests. Deployment/template/selector, ReplicaSet and Pod owner UIDs, immutable
image digests, observed generation and readiness must agree. Reasons such as
OOMKilled are recorded without free-form Pod messages. Failed observation remains
unavailable and does not override the bridge's deployment decision.
