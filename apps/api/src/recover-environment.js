#!/usr/bin/env node
// Offline operator entry point. No HTTP route exposes this authority.
import { createProductStore } from './product-store.js';
import { createEnvironmentAdapter } from './environments.js';
import { fileURLToPath } from 'node:url';

export async function recoverEnvironment({ directory, environmentStateDir, profilesFile, environmentId, adapter }) {
  const store = await createProductStore(directory); // Refuses while the API owns this workspace.
  try {
    const snapshot = store.read();
    const operation = Object.values(snapshot.operations).find(row => row.kind === 'environments' && row.id === environmentId
      || row.kind === 'deployments' && row.environment_id === environmentId);
    if (!operation || !['unknown', 'failed', 'blocked'].includes(operation.status)) throw new Error('Environment is not recoverable');
    const plan = snapshot.plans[operation.plan_id];
    if (!plan || plan.session_id !== operation.session_id) throw new Error('Plan ownership differs');
    if (operation.kind === 'deployments' && operation.ci?.run_id) throw new Error('Existing CI execution requires deployment reconciliation');
    const runtime = adapter || await createEnvironmentAdapter({ profilesFile, stateDir: environmentStateDir });
    const update = async patch => store.transaction(state => {
      const current = state.operations[operation.id];
      if (current.kind === 'environments') Object.assign(current, patch);
      else current.environment = patch;
      current.updated_at = new Date().toISOString();
    });
    const result = await runtime.execute(plan, { id: environmentId, operatorResume: true, onProgress: update });
    await store.transaction(state => {
      const current = state.operations[operation.id];
      if (current.kind === 'environments') Object.assign(current, result);
      else {
        current.environment = result;
        // Provisioning is recovered, but an unsubmitted source build is never automatically replayed.
        current.status = result.status === 'succeeded' ? 'blocked' : result.status;
        current.stage = 'environment';
        current.error = result.status === 'succeeded' ? { code: 'ENVIRONMENT_RECOVERED', message: '환경 복구가 완료되었습니다. 보존된 소스로 새 배포를 요청하세요.', retryable: false, outcome_unknown: false } : result.error;
      }
      current.operator_recovered_at = new Date().toISOString();
    });
    return { environment_id: environmentId, operation_id: operation.id, status: result.status, deployment_supported: result.deployment_supported };
  } finally { await store.close(); }
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const values = process.argv.slice(2); const allowed = ['--workspace', '--environment-state-dir', '--profiles-file', '--environment-id'];
  try {
    if (values.length !== 8 || values.some((value, i) => i % 2 === 0 && !allowed.includes(value)) || new Set(values.filter((_, i) => i % 2 === 0)).size !== 4) throw new Error('Invalid arguments');
    const options = Object.fromEntries(values.reduce((rows, value, i) => i % 2 ? rows : [...rows, [value, values[i + 1]]], []));
    console.log(JSON.stringify(await recoverEnvironment({ directory: options['--workspace'], environmentStateDir: options['--environment-state-dir'], profilesFile: options['--profiles-file'], environmentId: options['--environment-id'] })));
  } catch { console.error(JSON.stringify({ status: 'blocked', error: 'OPERATOR_RECOVERY_UNAVAILABLE' })); process.exitCode = 3; }
}
