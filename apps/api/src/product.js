import { createHash, randomUUID } from 'node:crypto';
import { setTimeout as pause } from 'node:timers/promises';
import { createProductStore } from './product-store.js';
import { APP_NAME, TARGET_ID } from './contract.js';
import { validateFiles } from './archive.js';

export class ProductError extends Error {
  constructor(status, code, message, { outcomeUnknown = false, retryable = false } = {}) {
    super(message); Object.assign(this, { status, code, outcomeUnknown, retryable });
  }
}
const invalid = (message) => new ProductError(422, 'INVALID_INPUT', message);
const unavailable = () => new ProductError(409, 'CAPABILITY_UNAVAILABLE', '이 서버에 해당 실행 기능이 설정되지 않았습니다.');
const active = (record) => ['queued', 'running', 'unknown'].includes(record.status);
const digest = (value) => createHash('sha256').update(JSON.stringify(value)).digest('hex');
const operationError = (code = 'UPSTREAM_FAILURE', unknown = true) => ({ code, request_id: randomUUID(), message: unknown ? '외부 실행 결과를 확인할 수 없습니다. 자동으로 재실행하지 않습니다.' : '실행이 완료되지 않았습니다.', retryable: false, outcome_unknown: unknown });
export function idempotencyKey(value) {
  if (typeof value !== 'string' || !/^[A-Za-z0-9._-]{1,128}$/.test(value)) throw invalid('유효한 Idempotency-Key가 필요합니다.');
  return value;
}
function publicRecord(record) {
  const { fingerprint, key, source, source_digest, source_bytes, legacy, ...visible } = record;
  return structuredClone(visible);
}
function checkFree(state) {
  if (Object.values(state.operations).some(active)) throw new ProductError(409, 'EXECUTOR_BUSY', '다른 실행 또는 결과 확인이 끝나지 않았습니다.', { retryable: true });
}

export async function createProductService({ service, directory, target, deployPublished, environmentAdapter, pollInterval = 2000, maxOperations = 100, maxSourceBytes = 512 * 1024 * 1024 }) {
  const store = await createProductStore(directory);
  const abort = new AbortController();
  function checkCapacity(state, sourceBytes = 0) {
    if (Object.keys(state.operations).length >= maxOperations || store.snapshotBytes() + sourceBytes > maxSourceBytes) throw new ProductError(409, 'CAPACITY_EXCEEDED', 'workspace 보관 한도에 도달했습니다. 운영자가 저장소를 확인해야 합니다.');
  }
  const workers = new Set();
  const targetId = target?.id || service?.targetId;
  const cdTarget = deployPublished?.targets?.[targetId];
  const cdAvailable = Boolean(deployPublished && (!deployPublished.targets || cdTarget));
  if (targetId && !TARGET_ID.test(targetId)) { await store.close(); throw invalid('등록된 대상 ID가 잘못되었습니다.'); }
  async function update(id, patch) {
    await store.transaction((state) => { Object.assign(state.operations[id], patch, { updated_at: new Date().toISOString() }); });
  }
  function launch(fn) {
    const worker = Promise.resolve().then(fn).catch(() => { console.error('RAILSHOT worker could not persist its final state; inspect private workspace state.'); }).finally(() => workers.delete(worker));
    workers.add(worker);
  }
  function find(kind, id) {
    const operation = store.read().operations[id];
    if (!operation || operation.kind !== kind) throw new ProductError(404, 'NOT_FOUND', '자원을 찾을 수 없습니다.');
    return operation;
  }
  function validateInput(input) {
    if (!service || !targetId) throw unavailable();
    if (typeof input.app !== 'string' || !APP_NAME.test(input.app)) throw invalid('앱 이름이 잘못되었습니다.');
    if (input.target_id !== targetId) throw invalid('등록된 배포 대상만 사용할 수 있습니다.');
  }
  function inputFingerprint(input) {
    return digest({ app: input.app, target_id: input.target_id, type: input.source_type,
      ...(input.repository_url ? { repository_url: input.repository_url } : {
        files: input.files.map(({ path, content }) => [path, createHash('sha256').update(content).digest('hex')]).sort(([a], [b]) => a.localeCompare(b)),
      }) });
  }
  async function reserve(kind, input, key, materialize) {
    return store.transaction(async (state) => {
      validateInput(input);
      const fingerprint = inputFingerprint(input);
      const existingId = key && state.keys[`${kind}:${key}`];
      if (existingId) {
        const existing = state.operations[existingId];
        if (existing.fingerprint !== fingerprint) throw new ProductError(409, 'IDEMPOTENCY_CONFLICT', '같은 키로 다른 입력을 보낼 수 없습니다.');
        return { record: existing, replay: true };
      }
      if (kind === 'deployments' && !cdAvailable) throw unavailable();
      checkFree(state);
      checkCapacity(state);
      if (kind === 'deployments' && cdTarget?.applicationName && input.app !== cdTarget.applicationName) throw invalid('등록된 배포 앱 이름과 일치하지 않습니다.');
      const source = input.files ? input : { ...input, ...await materialize(input.repository_url) };
      const files = validateFiles(source.files);
      const sourceBytes = files.reduce((sum, file) => sum + Buffer.byteLength(file.path) + Math.ceil(file.content.length / 3) * 4 + 128, 0);
      checkCapacity(state, sourceBytes);
      const id = randomUUID(), now = new Date().toISOString();
      await store.snapshot(id, files);
      const record = { id, kind, app: input.app, target_id: targetId, status: 'queued', stage: 'ci',
        ci: { run_id: null, state: 'queued', steps: [], publication_artifact_id: null, producer_attempt: null },
        cd: { state: 'not_started', revision: null, deployed: false },
        public_http: { state: 'not_run', verified_at: null, url: null }, url: null,
        actions_url: null, error: null, created_at: now, updated_at: now, fingerprint, key,
        source: source.source || null, source_bytes: sourceBytes, source_digest: digest(files.map(({ path, content }) => [path, createHash('sha256').update(content).digest('hex')]).sort()),
      };
      state.operations[id] = record;
      if (key) state.keys[`${kind}:${key}`] = id;
      return { record, input: { app: input.app, target_id: targetId, files, source: source.source } };
    });
  }
  async function submit(record, input) {
    await update(record.id, { status: 'running' });
    try {
      const result = await service.deploy(input);
      const runId = String(result.run_id);
      if (!/^\d+$/.test(runId) || typeof result.run_id === 'number' && !Number.isSafeInteger(result.run_id)) throw new Error('Invalid upstream run id');
      await store.transaction((state) => {
        if (state.bindings[runId]) throw new Error('Duplicate upstream run id');
        state.bindings[runId] = { operation_id: record.id, app: record.app, target_id: targetId, source_commit: result.source_commit || null };
        Object.assign(state.operations[record.id], { legacy: result, actions_url: result.actions_url || null,
          source_commit: result.source_commit || null, ci: { ...record.ci, run_id: runId } });
      });
      return result;
    } catch {
      await update(record.id, { status: 'unknown', error: operationError() });
      throw new ProductError(502, 'UPSTREAM_FAILURE', 'CI 접수 결과를 확인할 수 없습니다. 자동으로 재전송하지 마세요.', { outcomeUnknown: true });
    }
  }
  async function readBuild(runId) {
    const state = store.read(), binding = /^\d+$/.test(runId) && Object.hasOwn(state.bindings, runId) ? state.bindings[runId] : null;
    if (!binding) throw new ProductError(404, 'NOT_FOUND', '이 workspace에서 접수한 빌드를 찾을 수 없습니다.');
    let observed;
    try { observed = await service.status(runId); }
    catch { throw new ProductError(502, 'UPSTREAM_FAILURE', 'CI 상태를 확인하지 못했습니다.', { retryable: true }); }
    if (observed.publication && (String(observed.publication.run_id) !== runId || observed.publication.target_id !== binding.target_id || observed.publication.app !== binding.app || binding.source_commit && observed.publication.source_commit !== binding.source_commit)) {
      throw new ProductError(502, 'UPSTREAM_FAILURE', '게시 결과와 접수 기록이 일치하지 않습니다.');
    }
    const { run_id, state: status, status: workflowStatus, conclusion, message, ...rest } = observed;
    return { ...rest, id: runId, app: binding.app, target_id: binding.target_id, source_commit: binding.source_commit,
      status, workflow: { status: workflowStatus, conclusion }, url: null };
  }
  async function observe(record, runId) {
    try {
      for (;;) {
        if (abort.signal.aborted) return;
        const build = await readBuild(runId);
        const ci = { run_id: runId, state: build.status, steps: build.steps ?? [], publication_artifact_id: build.publication?.artifact_id ? String(build.publication.artifact_id) : null, producer_attempt: build.publication?.producer_attempt || null };
        await update(record.id, { ci });
        if (build.status === 'published') {
          if (record.kind === 'builds') { await update(record.id, { status: 'succeeded', stage: 'ci' }); return; }
          await update(record.id, { stage: 'cd', cd: { state: 'running', revision: null, deployed: false } });
          const result = await deployPublished({ deploymentId: record.id, app: record.app, targetId,
            sourceCommit: build.source_commit, publication: build.publication, signal: abort.signal });
          const succeeded = result.cd?.deployed === true && typeof result.cd.revision === 'string' && result.cd.revision.length > 0 && result.public_http?.state === 'succeeded' && result.public_http.verified_at && /^https?:\/\//.test(result.public_http.url || '');
          const status = succeeded ? 'succeeded' : result.error?.outcome_unknown ? 'unknown' : ['blocked', 'failed'].includes(result.cd?.state) ? result.cd.state : 'unknown';
          await update(record.id, { status, stage: succeeded ? 'complete' : 'cd', cd: result.cd, public_http: result.public_http,
            url: succeeded ? result.public_http.url : null, error: succeeded ? null : operationError(result.error?.code || 'CD_UNVERIFIED', status === 'unknown') });
          return;
        }
        if (['failed', 'publication_unverified'].includes(build.status)) {
          await update(record.id, { status: 'failed', error: operationError('CI_FAILED', false) }); return;
        }
        await pause(pollInterval, undefined, { signal: abort.signal, ref: false });
      }
    } catch {
      if (!abort.signal.aborted) await update(record.id, { status: 'unknown', error: operationError() });
    }
  }
  return {
    targets() {
      return targetId ? [{ id: targetId, label: target?.label || targetId, provider: target?.provider || null, environment: target?.environment || 'registered',
        ...(cdTarget?.applicationName ? { application_name: cdTarget.applicationName, deployment_scope: 'registered_application' } : {}),
        capabilities: { ci_submission: Boolean(service), application_deployment: Boolean(service && cdAvailable), database_configuration: false },
        runtime: { status: 'unknown', observed_at: null }, blockers: cdAvailable ? [] : ['CD_ADAPTER_NOT_CONFIGURED'] }] : [];
    },
    async createBuild(input, materialize) {
      const reserved = await reserve('builds', input, null, materialize);
      const result = await submit(reserved.record, reserved.input);
      launch(() => observe(reserved.record, String(result.run_id)));
      return result;
    },
    getBuild: readBuild,
    async legacyStatus(id) { if (!Object.hasOwn(store.read().bindings, id)) throw new ProductError(404, 'NOT_FOUND', '접수한 실행을 찾을 수 없습니다.'); return service.status(id); },
    async createDeployment(input, key, materialize) {
      const reserved = await reserve('deployments', input, idempotencyKey(key), materialize);
      if (!reserved.replay) launch(async () => {
        try { const result = await submit(reserved.record, reserved.input); await observe(reserved.record, String(result.run_id)); } catch { /* submit persists unknown */ }
      });
      return publicRecord(reserved.record);
    },
    getDeployment(id) { return publicRecord(find('deployments', id)); },
    profiles() { return environmentAdapter?.profiles() || []; },
    async createPlan(input) {
      if (!environmentAdapter) throw unavailable();
      return store.transaction(async (state) => {
        checkFree(state);
        if (Object.keys(state.plans).length >= maxOperations) throw new ProductError(409, 'CAPACITY_EXCEEDED', '계획 보관 한도에 도달했습니다.');
        const id = randomUUID(), plan = await environmentAdapter.plan(input, { id });
        state.plans[id] = plan;
        return plan.public;
      });
    },
    getPlan(id) { const plans = store.read().plans, plan = Object.hasOwn(plans, id) ? plans[id] : null; if (!plan) throw new ProductError(404, 'NOT_FOUND', '계획을 찾을 수 없습니다.'); return plan.public; },
    async createEnvironment(input, key) {
      idempotencyKey(key);
      if (!input || Object.keys(input).length !== 1 || typeof input.plan_id !== 'string') throw invalid('plan_id만 입력하세요.');
      if (!environmentAdapter) throw unavailable();
      const accepted = await store.transaction(async (state) => {
        const fingerprint = digest(input), existingId = state.keys[`environments:${key}`];
        if (existingId) {
          const record = state.operations[existingId];
          if (record.fingerprint !== fingerprint) throw new ProductError(409, 'IDEMPOTENCY_CONFLICT', '같은 키로 다른 계획을 실행할 수 없습니다.');
          return { record, replay: true };
        }
        const plan = Object.hasOwn(state.plans, input.plan_id) ? state.plans[input.plan_id] : null;
        if (!plan) throw new ProductError(404, 'NOT_FOUND', '계획을 찾을 수 없습니다.');
        if (plan.environment_id) throw new ProductError(409, 'CONFLICT', '이미 실행에 사용된 계획입니다.');
        checkFree(state);
        checkCapacity(state);
        await environmentAdapter.verifyPlan(plan);
        const id = randomUUID();
        const record = { id, kind: 'environments', plan_id: input.plan_id, status: 'queued', stage: 'provision', error: null, fingerprint, created_at: new Date().toISOString() };
        state.operations[id] = record; state.keys[`environments:${key}`] = id; plan.environment_id = id;
        return { record, plan };
      });
      if (!accepted.replay) launch(async () => {
        try {
          await update(accepted.record.id, { status: 'running' });
          const patch = await environmentAdapter.execute(accepted.plan, { id: accepted.record.id, onProgress: (value) => update(accepted.record.id, value) });
          await update(accepted.record.id, patch);
        } catch (error) { await update(accepted.record.id, { status: error.outcomeUnknown === false ? [409, 503].includes(error.status) ? 'blocked' : 'failed' : 'unknown', error: operationError('ENVIRONMENT_FAILED', error.outcomeUnknown !== false) }); }
      });
      return publicRecord(accepted.record);
    },
    getEnvironment(id) { return publicRecord(find('environments', id)); },
    async close() { abort.abort(); await Promise.allSettled(workers); await store.close(); },
  };
}
