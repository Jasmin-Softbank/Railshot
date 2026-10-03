import { telemetryContract } from './telemetry.js';
import { redactDiagnostic } from './diagnostics.js';

const policy = telemetryContract.diagnostic_context;

// Bound UTF-8 bytes without splitting a code point. The archive remains the source
// of truth; these excerpts only select what the classifier sees on its first call.
function prefix(text, limit) {
  let bytes = 0, result = '';
  for (const char of text) {
    bytes += Buffer.byteLength(char);
    if (bytes > limit) break;
    result += char;
  }
  return result;
}
function excerpt(text, code, limit) {
  const value = redactDiagnostic(text);
  const hit = code ? value.indexOf(code) : -1;
  // Keep a small lead-in around a captured code, otherwise terminal output.
  // Reversing code points makes the tail obey the same UTF-8 budget as the head.
  const start = hit >= 0 ? Math.max(0, hit - 200) : 0;
  const selected = hit >= 0 ? prefix(value.slice(start), limit)
    : [...prefix([...value].reverse().join(''), limit)].reverse().join('');
  return { text: selected, window: hit >= 0 ? 'error_code' : 'tail',
    truncated: selected !== value };
}

export function diagnosticContext(diagnostic) {
  const layer = diagnostic.failure.layer || diagnostic.error?.phase;
  const evidence = {}, references = [], selection = {};
  function add(id, text, bytes, reference) {
    const value = excerpt(text, diagnostic.failure.code, bytes);
    if (!value.text.trim() || Object.values(evidence).includes(value.text)) return;
    evidence[id] = value.text;
    selection[id] = { window: value.window, truncated: value.truncated };
    references.push({ id, artifact_id: diagnostic.artifact_id, ...reference });
  }
  add('failure', diagnostic.failure.excerpt, policy.failure_bytes,
    { path: 'case.json', sha256: diagnostic.case_sha256 });
  const logs = new Map(diagnostic.logs.map((log) => [log.process_id, log]));
  // A successful docker.logs command can contain a failed application's output.
  // Filter by failed layer, then prefer failed processes; never fill spare space
  // with unrelated successful stages.
  const candidates = diagnostic.processes.filter((p) => p.layer === layer && logs.has(p.id))
    .sort((a, b) => Number(b.outcome !== 'PASS') - Number(a.outcome !== 'PASS')
      || Number(b.id.split('-')[1]) - Number(a.id.split('-')[1]));
  for (const process of candidates) {
    if (references.filter((ref) => ref.id !== 'failure').length >= policy.max_logs) break;
    const log = logs.get(process.id);
    add(process.id, log.text, policy.log_bytes, { path: log.path, sha256: log.sha256 });
  }
  return {
    references,
    state: {
      context_version: policy.version, stage: policy.stages[layer] || 'unknown', gate_layer: layer,
      case_sha256: diagnostic.case_sha256, error: diagnostic.error, source_sha256: diagnostic.source.tested_sha256,
      failure_code: diagnostic.failure.code,
      // Durations add noise to diagnosis; case_sha256 still binds the exact artifact.
      checks: diagnostic.checks.map(({ check_id, outcome, required, reason }) =>
        ({ check_id, outcome, required, ...(reason ? { reason } : {}) })),
      missing_evidence: diagnostic.missing_evidence, evidence, selection,
    },
  };
}
