import { readFileSync } from 'node:fs';
import { randomUUID } from 'node:crypto';
import { canonical, sha256 } from './diagnostics.js';

export const telemetryContract = Object.freeze(JSON.parse(readFileSync(new URL('../../../ci/scripts/contract/telemetry.json', import.meta.url))));
const stateOutcome = (state) => ({ queued: 'NOT_RUN', not_started: 'NOT_RUN', not_run: 'NOT_RUN', running: 'RUNNING',
  progressing: 'RUNNING', published: 'PASS', deployed: 'PASS', succeeded: 'PASS', failed: 'FAIL', blocked: 'BLOCKED',
  unknown: 'UNKNOWN', publication_unverified: 'UNKNOWN', unverified: 'UNKNOWN' }[state] || 'UNKNOWN');
const safeId = (v) => typeof v === 'string' && /^[A-Za-z0-9_.:-]{1,160}$/.test(v) ? v : null;
export function structuredError(error, phase) {
  if (!error) return null;
  const code = typeof error.code === 'string' && /^[A-Z][A-Z0-9_]{1,80}$/.test(error.code) ? error.code : 'INTERNAL_ERROR';
  const unknown = error.outcome_unknown === true || error.outcome === 'UNKNOWN';
  return { code, category: telemetryContract.error_categories[code] || 'unknown', component: 'api', phase,
    retryable: !unknown && error.retryable === true, retry_policy: unknown ? 'after_reconcile' : 'never',
    action: unknown ? 'operator_reconcile' : 'inspect_evidence', outcome_unknown: unknown, retry_decision: 'not_requested',
    causes: [] };
}
export function appendEvent(record, name, phase, outcome, { attributes = {}, error = null, evidence_refs = [], identity = null, occurred_at } = {}) {
  const now = new Date().toISOString();
  const log = record.telemetry ||= { version: 1, items: [], truncated: false, sequence: 0, checked_at: null, state: 'ready' };
  const key = identity || sha256(canonical([name, phase, outcome, attributes, error]));
  // Per-source identities make repeated polling and delivery idempotent. A delayed event
  // is retained with its source timestamp and never changes the authoritative operation.
  if (log.items.some((e) => e.producer_key === key)) return;
  const severity = ['FAIL', 'UNKNOWN'].includes(outcome) ? 'ERROR' : outcome === 'BLOCKED' ? 'WARN' : 'INFO';
  log.items.push({ schema_version: 1, event_id: randomUUID(), event_name: name, occurred_at: occurred_at || now,
    observed_at: now, ingested_at: now, sequence: ++log.sequence, producer_key: key,
    component: name.startsWith('gate.') || name.startsWith('agent.') || name.startsWith('loop.') ? 'ci' : 'api',
    phase, outcome, severity, severity_number: telemetryContract.severity_numbers[severity],
    correlation: { deployment_id: record.id, app: record.app, target_id: record.target_id,
      source_commit: record.source_commit || null, github_run_id: record.ci?.run_id ? String(record.ci.run_id) : null,
      github_run_attempt: attributes.github_run_attempt || record.ci?.producer_attempt || null },
    attributes, error, evidence_refs });
  const cutoff = Date.now() - telemetryContract.retention_days * 86400000;
  const kept = log.items.filter((e) => Date.parse(e.ingested_at) >= cutoff).slice(-telemetryContract.max_events);
  if (kept.length < log.items.length) log.truncated = true;
  log.items = kept; log.updated_at = now;
}
export function observeOperation(record, patch, before) {
  if (!['deployments', 'builds'].includes(record.kind)) return;
  const add = (name, phase, state, attributes = {}) => appendEvent(record, name, phase, stateOutcome(state),
    { attributes, error: ['failed', 'blocked', 'unknown', 'publication_unverified'].includes(state) ? structuredError(record.error, phase) : null });
  if (patch.stage && patch.stage !== before.stage) add('deployment.phase', patch.stage, record.status);
  if (patch.ci && patch.ci.state !== before.ci?.state) add('ci.observed', 'ci', patch.ci.state,
    { publication_artifact_id: patch.ci.publication_artifact_id || null, github_run_attempt: patch.ci.producer_attempt || null,
      images: patch.ci.images || {} });
  if (patch.cd) {
    if (record.cd.revision && record.cd.revision !== before.cd?.revision) add('gitops.revision.observed', 'gitops', 'succeeded', { config_revision: record.cd.revision });
    if (canonical(record.cd) !== canonical(before.cd || {})) add('controller.observed', 'controller', record.cd.state,
      { config_revision: record.cd.revision || null, deployed: record.cd.deployed === true,
        ...(record.cd.evidence ? { evidence: record.cd.evidence } : {}) });
  }
  if (patch.public_http && canonical(patch.public_http) !== canonical(before.public_http || {})) add('http.observed', 'http', patch.public_http.state,
    { verified_at: patch.public_http.verified_at || null, status_code: patch.public_http.status_code || null });
  if (patch.status && patch.status !== before.status) add('deployment.observed', record.stage || 'ci', patch.status);
}
export function ingestCiEvents(record, envelope) {
  const log = record.telemetry ||= { version: 1, items: [], truncated: false, sequence: 0 };
  log.checked_at = envelope.checked_at; log.state = envelope.state; log.stale = envelope.stale === true;
  log.reason = envelope.reason; log.truncated ||= envelope.truncated === true;
  for (const e of envelope.items || []) {
    if (!Number.isSafeInteger(e.sequence) || !safeId(e.native_run_id)) continue;
    const attributes = { github_run_attempt: envelope.run_attempt, native_run_id: e.native_run_id };
    for (const key of ['attempt_id', 'completed_steps', 'total_steps', 'duration_s', 'elapsed_ms', 'role', 'provider', 'sdk_invocations'])
      if (e[key] !== undefined) attributes[key] = e[key];
    appendEvent(record, e.event_name, e.phase || 'agent', e.outcome || 'RUNNING', { attributes,
      identity: `ci:${envelope.run_id}:${envelope.run_attempt}:${e.sequence}`, occurred_at: e.occurred_at });
  }
}
export function publicTelemetry(record) {
  const log = structuredClone(record.telemetry || { version: 1, items: [], state: 'not_started', checked_at: null, truncated: false });
  log.items = log.items.filter((e) => Date.now() - Date.parse(e.ingested_at) < telemetryContract.retention_days * 86400000)
    .map(({ producer_key, ...event }) => event);
  return log;
}
