import { createHash, randomUUID } from 'node:crypto';
import { setTimeout as pause } from 'node:timers/promises';
import { createProductStore } from './product-store.js';
import { APP_NAME, TARGET_ID } from './contract.js';
import { validateFiles } from './archive.js';
import { createMetricsObserver } from './metrics.js';

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
function ciObservation(build, previous = {}) {
  return { ...previous, run_id: build.id, state: build.status, steps: build.steps ?? [],
    message: build.message || null, diagnostics: build.diagnostics || null, diagnostics_checked_at: new Date().toISOString(),
    images: build.publication?.images || {}, publication_artifact_id: build.publication?.artifact_id ? String(build.publication.artifact_id) : null,
    producer_attempt: build.publication?.producer_attempt || null };
}
function ciFailure(build) {
  const diagnostic = build.diagnostics?.state === 'ready' ? build.diagnostics : null;
  const unknown = diagnostic?.outcome === 'UNKNOWN';
  return { status: unknown ? 'unknown' : diagnostic?.outcome === 'BLOCKED' ? 'blocked' : 'failed',
    error: { ...operationError(diagnostic?.code || (build.status === 'publication_unverified' ? 'PUBLICATION_UNVERIFIED' : 'CI_FAILED'), unknown),
      message: [diagnostic?.message || build.message || 'CI 실행이 완료되지 않았습니다.', diagnostic?.guidance].filter(Boolean).join(' ') } };
}
export function idempotencyKey(value) {
  if (typeof value !== 'string' || !/^[A-Za-z0-9._-]{1,128}$/.test(value)) throw invalid('유효한 Idempotency-Key가 필요합니다.');
  return value;
}
function publicRecord(record) {
  const { fingerprint, key, source, source_bytes, legacy, session_id, ...visible } = record;
  return structuredClone(visible);
}
function checkFree(state) {
  if (Object.values(state.operations).some(active)) throw new ProductError(409, 'EXECUTOR_BUSY', '다른 실행 또는 결과 확인이 끝나지 않았습니다.', { retryable: true });
}

export async function createProductService({ service, directory, target, deployPublished, environmentAdapter, observeMetrics = createMetricsObserver(), observeLogs, pollInterval = 2000, maxOperations = 100, maxSourceBytes = 512 * 1024 * 1024 }) {
  const store = await createProductStore(directory);
  const abort = new AbortController();
  function checkCapacity(state, sourceBytes = 0) {
    if (Object.keys(state.operations).length >= maxOperations || store.snapshotBytes() + sourceBytes > maxSourceBytes) throw new ProductError(409, 'CAPACITY_EXCEEDED', 'workspace 보관 한도에 도달했습니다. 운영자가 저장소를 확인해야 합니다.');
  }
  const workers = new Set();
  const targetId = target?.id || service?.targetId;
  const sharedTargets = new Set(service?.targetIds || (targetId ? [targetId] : []));
  const scopeKey = (kind, key, sessionId) => `${sessionId ? sessionId + ':' : ''}${kind}:${key}`;
  const owns = (value, sessionId) => value && (!sessionId || value.session_id === sessionId);
  const cdTarget = deployPublished?.targets?.[targetId];
  const cdAvailable = Boolean(deployPublished && (!deployPublished.targets || cdTarget));
  if (targetId && !TARGET_ID.test(targetId)) { await store.close(); throw invalid('등록된 대상 ID가 잘못되었습니다.'); }
  function deploymentOptions() {
    return [['cloud', 'aws', '클라우드 · RailShot AWS'], ['onprem', 'openstack', '온프레미스 · OpenStack'], ['onprem', 'proxmox', '온프레미스 · Proxmox']].map(([environment, provider, label]) => {
      const available = Boolean(service && cdAvailable && targetId && target?.provider === provider);
      return { id: `${environment}-${provider}`, environment, provider, label, available,
        message: available ? `소스 검사부터 앱 배포와 URL 확인까지 진행합니다.${cdTarget?.applicationName ? ` 등록된 앱 ${cdTarget.applicationName}의 소스를 갱신합니다.` : ''}`
          : `${label}에 배포할 인프라가 아직 연결되지 않았습니다. 운영자의 대상 연결이 필요합니다.` };
    });
  }
  function resolveSelection(input) {
    if (!input.deployment_selection) return input;
    if (input.app !== undefined || input.target_id !== undefined || input.plan_id !== undefined) throw invalid('환경 선택과 직접 대상·계획 지정을 함께 사용할 수 없습니다.');
    const { environment, provider } = input.deployment_selection;
    const option = deploymentOptions().find((item) => item.environment === environment && item.provider === provider);
    if (!option) throw invalid('배포 환경과 인프라 종류를 확인하세요.');
    if (!option.available) throw new ProductError(409, 'CAPABILITY_UNAVAILABLE', option.message);
    const name = input.source_name || input.repository_url?.split('/').at(-1) || 'my-app';
    const normalized = name.normalize('NFKD').toLowerCase().replace(/\.zip$/i, '').replace(/[^a-z0-9-]+/g, '-').replace(/^-+|-+$/g, '');
    let app = normalized.replace(/^[^a-z]+/, '').slice(0, 30).replace(/-+$/g, '');
    if (!APP_NAME.test(app)) app = `app-${digest(name).slice(0, 10)}`;
    return { ...input, app: cdTarget?.applicationName || app, target_id: targetId };
  }
  async function update(id, patch) {
    await store.transaction((state) => { Object.assign(state.operations[id], patch, { updated_at: new Date().toISOString() }); });
  }
  function launch(fn) {
    const worker = Promise.resolve().then(fn).catch(() => { console.error('RAILSHOT worker could not persist its final state; inspect private workspace state.'); }).finally(() => workers.delete(worker));
    workers.add(worker);
  }
  function find(kind, id, sessionId = null) {
    const operation = store.read().operations[id];
    if (!owns(operation, sessionId) || operation.kind !== kind) throw new ProductError(404, 'NOT_FOUND', '자원을 찾을 수 없습니다.');
    return operation;
  }
  function validateInput(input) {
    if (!service || !targetId) throw unavailable();
    if (typeof input.app !== 'string' || !APP_NAME.test(input.app)) throw invalid('앱 이름이 잘못되었습니다.');
    if (typeof input.target_id !== 'string' || !TARGET_ID.test(input.target_id)) throw invalid('등록된 배포 대상 ID가 잘못되었습니다.');
  }
  function registeredEnvironment(state, id, sessionId = null) {
    if (typeof environmentAdapter?.deployPublished !== 'function' || typeof id !== 'string' || !TARGET_ID.test(id)) return null;
    for (const operation of Object.values(state.operations).reverse()) {
      if (!owns(operation, sessionId)) continue;
      const standalone = operation.kind === 'environments';
      const environment = standalone ? operation : operation.kind === 'deployments' ? operation.environment : null;
      const environmentId = standalone ? operation.id : operation.environment_id;
      const plan = Object.hasOwn(state.plans, operation.plan_id || '') ? state.plans[operation.plan_id] : null;
      if (environment?.status !== 'succeeded' || environment.deployment_supported !== true
          || environment.runtime_target_id !== id || !environmentId || plan?.environment_id !== environmentId
          || plan.private?.profile?.target?.target_id !== id || !plan.private.profile.deployment
          || !APP_NAME.test(plan.public?.name || '') || (!standalone && operation.app !== plan.public.name)) continue;
      return { environment_id: environmentId, applicationName: plan.public.name,
        database_configuration: environment.database?.status === 'succeeded' };
    }
    return null;
  }
  // Restore only successful registrations; interrupted intents still occupy checkFree admission.
  const restored = store.read();
  for (const operation of Object.values(restored.operations)) {
    const id = operation.kind === 'environments' ? operation.runtime_target_id : operation.environment?.runtime_target_id;
    if (registeredEnvironment(restored, id)) service?.allowTarget?.(id);
  }
  function inputFingerprint(input) {
    return digest({ app: input.app, target_id: input.target_id, type: input.source_type, ...(input.plan_id ? { plan_id: input.plan_id } : {}),
      ...(input.deployment_selection ? { selection: input.deployment_selection } : {}),
      ...(input.repository_url ? { repository_url: input.repository_url } : {
        files: input.files.map(({ path, content }) => [path, createHash('sha256').update(content).digest('hex')]).sort(([a], [b]) => a.localeCompare(b)),
      }) });
  }
  async function reserve(kind, input, key, materialize, sessionId = null) {
    return store.transaction(async (state) => {
      validateInput(input);
      const fingerprint = inputFingerprint(input);
      const existingId = key && state.keys[scopeKey(kind, key, sessionId)];
      if (existingId) {
        const existing = state.operations[existingId];
        if (existing.fingerprint !== fingerprint) throw new ProductError(409, 'IDEMPOTENCY_CONFLICT', '같은 키로 다른 입력을 보낼 수 없습니다.');
        return { record: existing, replay: true };
      }
      const selectedCdTarget = deployPublished?.targets?.[input.target_id];
      const selectedCdAvailable = Boolean(deployPublished && (!deployPublished.targets || selectedCdTarget));
      const savedEnvironment = registeredEnvironment(state, input.target_id, sessionId);
      const registered = !selectedCdAvailable && !input.plan_id && kind === 'deployments' ? savedEnvironment : null;
      const admittedTarget = sharedTargets.has(input.target_id) || Boolean(savedEnvironment);
      const plan = input.plan_id && Object.hasOwn(state.plans, input.plan_id) ? state.plans[input.plan_id] : null;
      if (input.plan_id) {
        if (kind !== 'deployments' || !owns(plan, sessionId) || !environmentAdapter?.deployPublished) throw invalid('실행 가능한 환경 계획이 필요합니다.');
        if (plan.environment_id) throw new ProductError(409, 'CONFLICT', '이미 실행에 사용된 계획입니다.');
        if (plan.public.name !== input.app || plan.private?.profile?.target?.target_id !== input.target_id
            || !plan.private.profile.deployment) throw invalid('계획의 앱·배포 대상과 일치해야 합니다.');
        if (!admittedTarget && (plan.private.profile.create_per_request !== true || plan.public.runtime_target_id !== input.target_id
            || typeof service.allowTarget !== 'function')) throw invalid('요청별 실행 대상이 승인된 계획이어야 합니다.');
        await environmentAdapter.verifyPlan(plan);
      }
      if (!admittedTarget && !plan && !savedEnvironment) throw invalid('등록된 배포 대상만 사용할 수 있습니다.');
      if (kind === 'deployments' && !input.plan_id && !selectedCdAvailable && !registered) throw unavailable();
      checkFree(state);
      checkCapacity(state);
      const applicationName = selectedCdTarget?.applicationName || savedEnvironment?.applicationName;
      if (!input.plan_id && applicationName && (kind === 'deployments' || savedEnvironment) && input.app !== applicationName) throw invalid('등록된 배포 앱 이름과 일치하지 않습니다.');
      const source = input.files ? input : { ...input, ...await materialize(input.repository_url) };
      const files = validateFiles(source.files);
      const sourceBytes = files.reduce((sum, file) => sum + Buffer.byteLength(file.path) + Math.ceil(file.content.length / 3) * 4 + 128, 0);
      checkCapacity(state, sourceBytes);
      const id = randomUUID(), now = new Date().toISOString();
      await store.snapshot(id, files);
      const record = { id, kind, session_id: sessionId, app: input.app, target_id: input.target_id, ...(plan ? { plan_id: input.plan_id, environment_id: `${id}.environment`, environment: { status: 'queued' } } : registered ? { environment_id: registered.environment_id } : {}), status: 'queued', stage: plan ? 'environment' : 'ci',
        ci: { run_id: null, state: 'queued', steps: [], publication_artifact_id: null, producer_attempt: null },
        cd: { state: 'not_started', revision: null, deployed: false },
        public_http: { state: 'not_run', verified_at: null, url: null }, url: null,
        actions_url: null, error: null, created_at: now, updated_at: now, fingerprint, key,
        source: source.source || null, source_bytes: sourceBytes, source_digest: digest(files.map(({ path, content }) => [path, createHash('sha256').update(content).digest('hex')]).sort()),
      };
      state.operations[id] = record;
      if (plan) plan.environment_id = record.environment_id;
      if (key) state.keys[scopeKey(kind, key, sessionId)] = id;
      return { record, plan, input: { app: input.app, target_id: input.target_id, files, source: source.source } };
    });
  }
  async function submit(record, input) {
    await update(record.id, { status: 'running', stage: 'ci' });
    try {
      if (!(service.targetIds || [targetId]).includes(record.target_id)) {
        const registered = registeredEnvironment(store.read(), record.target_id);
        if (!registered || registered.applicationName !== record.app || typeof service.allowTarget !== 'function') throw unavailable();
        service.allowTarget(record.target_id);
      }
      const result = await service.deploy(input);
      const runId = String(result.run_id);
      if (!/^\d+$/.test(runId) || typeof result.run_id === 'number' && !Number.isSafeInteger(result.run_id)) throw new Error('Invalid upstream run id');
      await store.transaction((state) => {
        if (state.bindings[runId]) throw new Error('Duplicate upstream run id');
        state.bindings[runId] = { operation_id: record.id, app: record.app, target_id: record.target_id, source_commit: result.source_commit || null };
        Object.assign(state.operations[record.id], { legacy: result, actions_url: result.actions_url || null,
          source_commit: result.source_commit || null, ci: { ...record.ci, run_id: runId } });
      });
      return result;
    } catch {
      await update(record.id, { status: 'unknown', error: operationError() });
      throw new ProductError(502, 'UPSTREAM_FAILURE', 'CI 접수 결과를 확인할 수 없습니다. 자동으로 재전송하지 마세요.', { outcomeUnknown: true });
    }
  }
  async function readBuild(runId, sessionId = null) {
    const state = store.read(), binding = /^\d+$/.test(runId) && Object.hasOwn(state.bindings, runId) ? state.bindings[runId] : null;
    if (!binding || !owns(state.operations[binding.operation_id], sessionId)) throw new ProductError(404, 'NOT_FOUND', '이 workspace에서 접수한 빌드를 찾을 수 없습니다.');
    let observed;
    try { observed = await service.status(runId, binding.target_id); }
    catch { throw new ProductError(502, 'UPSTREAM_FAILURE', 'CI 상태를 확인하지 못했습니다.', { retryable: true }); }
    if (observed.publication && (String(observed.publication.run_id) !== runId || observed.publication.target_id !== binding.target_id || observed.publication.app !== binding.app || binding.source_commit && observed.publication.source_commit !== binding.source_commit)) {
      throw new ProductError(502, 'UPSTREAM_FAILURE', '게시 결과와 접수 기록이 일치하지 않습니다.');
    }
    const { run_id, state: status, status: workflowStatus, conclusion, ...rest } = observed;
    return { ...rest, id: runId, app: binding.app, target_id: binding.target_id, source_commit: binding.source_commit,
      status, workflow: { status: workflowStatus, conclusion }, url: null };
  }
  async function observe(record, runId) {
    try {
      for (;;) {
        if (abort.signal.aborted) return;
        const build = await readBuild(runId);
        const ci = ciObservation(build);
        await update(record.id, { ci });
        if (build.status === 'published') {
          if (record.kind === 'builds') { await update(record.id, { status: 'succeeded', stage: 'ci' }); return; }
          await update(record.id, { stage: 'cd', cd: { state: 'running', revision: null, deployed: false } });
          const deploy = record.environment_id ? (args) => environmentAdapter.deployPublished(record.environment_id, args) : deployPublished;
          const result = await deploy({ deploymentId: record.id, app: record.app, targetId: record.target_id,
            sourceCommit: build.source_commit, publication: build.publication, signal: abort.signal,
            onProgress: (progress) => update(record.id, { stage: progress.cd?.deployed ? 'http' : 'cd', cd: progress.cd, public_http: progress.public_http }) });
          const succeeded = result.cd?.deployed === true && typeof result.cd.revision === 'string' && result.cd.revision.length > 0 && result.public_http?.state === 'succeeded' && result.public_http.verified_at && /^https?:\/\//.test(result.public_http.url || '');
          const status = succeeded ? 'succeeded' : result.error?.outcome_unknown ? 'unknown' : ['blocked', 'failed'].includes(result.cd?.state) ? result.cd.state : 'unknown';
          await update(record.id, { status, stage: succeeded ? 'complete' : result.cd?.deployed ? 'http' : 'cd', cd: result.cd, public_http: result.public_http,
            url: succeeded ? (result.public_http.site_url || result.public_http.url) : null, error: succeeded ? null : operationError(result.error?.code || 'CD_UNVERIFIED', status === 'unknown') });
          return;
        }
        if (['failed', 'publication_unverified'].includes(build.status)) {
          await update(record.id, ciFailure(build)); return;
        }
        await pause(pollInterval, undefined, { signal: abort.signal, ref: false });
      }
    } catch {
      if (!abort.signal.aborted) await update(record.id, { status: 'unknown', error: operationError() });
    }
  }
  return {
    dashboard: store.dashboard,
    list(kind, sessionId) {
      const state = store.read();
      if (kind === 'plans') return Object.values(state.plans).filter((row) => owns(row, sessionId)).reverse().map((row) => structuredClone(row.public));
      return Object.values(state.operations).filter((row) => row.kind === kind && owns(row, sessionId))
        .reverse().filter((row) => kind !== 'builds' || row.ci?.run_id)
        .map((row) => ({ id: kind === 'builds' ? String(row.ci.run_id) : row.id, kind, status: row.status,
          ...Object.fromEntries(['app', 'target_id', 'stage', 'created_at', 'updated_at'].filter((key) => row[key] !== undefined).map((key) => [key, row[key]])) }));
    },
    deploymentOptions,
    targets(sessionId = null) {
      const state = store.read();
      const ids = new Set(sharedTargets);
      for (const operation of Object.values(state.operations)) {
        const id = operation.kind === 'environments' ? operation.runtime_target_id : operation.environment?.runtime_target_id;
        if (registeredEnvironment(state, id, sessionId)) ids.add(id);
      }
      return [...ids].map((id) => {
        const staticTarget = deployPublished?.targets?.[id];
        const staticAvailable = Boolean(deployPublished && (!deployPublished.targets || staticTarget));
        const registered = staticAvailable ? staticTarget : registeredEnvironment(state, id, sessionId);
        const available = staticAvailable || Boolean(registered);
        return { id, label: id === targetId ? target?.label || id : id,
          provider: id === targetId ? target?.provider || null : null, environment: 'registered',
          ...(registered?.applicationName ? { application_name: registered.applicationName, deployment_scope: 'registered_application' } : {}),
          capabilities: { ci_submission: Boolean(service), application_deployment: available,
            database_configuration: !staticAvailable && registered?.database_configuration === true },
          runtime: { status: 'unknown', observed_at: null }, blockers: available ? [] : ['CD_ADAPTER_NOT_CONFIGURED'] };
      });
    },
    async createBuild(input, materialize, sessionId = null) {
      const reserved = await reserve('builds', input, null, materialize, sessionId);
      const result = await submit(reserved.record, reserved.input);
      launch(() => observe(reserved.record, String(result.run_id)));
      return result;
    },
    getBuild: readBuild,
    async legacyStatus(id, sessionId = null) { const state = store.read(); if (!Object.hasOwn(state.bindings, id) || !owns(state.operations[state.bindings[id].operation_id], sessionId)) throw new ProductError(404, 'NOT_FOUND', '접수한 실행을 찾을 수 없습니다.'); return service.status(id, store.read().bindings[id].target_id); },
    async createDeployment(input, key, materialize, sessionId = null) {
      input = resolveSelection(input);
      const reserved = await reserve('deployments', input, idempotencyKey(key), materialize, sessionId);
      if (!reserved.replay) launch(async () => {
        try {
          if (reserved.plan) {
            await update(reserved.record.id, { status: 'running', stage: 'environment' });
            const environment = await environmentAdapter.execute(reserved.plan, { id: reserved.record.environment_id,
              onProgress: (value) => update(reserved.record.id, { environment: value }) });
            await update(reserved.record.id, { environment });
            if (environment.status !== 'succeeded' || environment.deployment_supported !== true || environment.runtime_target_id !== reserved.record.target_id) {
              await update(reserved.record.id, { status: environment.status === 'succeeded' ? 'blocked' : environment.status,
                error: environment.error || operationError('DEPLOYMENT_TARGET_NOT_REGISTERED', false) }); return;
            }
          }
          const result = await submit(reserved.record, reserved.input); await observe(reserved.record, String(result.run_id)); } catch (error) { await update(reserved.record.id, { status: 'unknown', error: operationError('STACK_OUTCOME_UNKNOWN', true) }); }
      });
      return publicRecord(reserved.record);
    },
    async getDeployment(id, sessionId = null) {
      let record = publicRecord(find('deployments', id, sessionId));
      // Older completed records can acquire diagnostics without replaying CI or CD.
      if (record.stage === 'ci' && ['failed', 'blocked'].includes(record.status) && record.ci?.run_id && record.ci.diagnostics?.state !== 'ready'
          && (!record.ci.diagnostics_checked_at || Date.now() - Date.parse(record.ci.diagnostics_checked_at) > 30000)) {
        try {
          const build = await readBuild(record.ci.run_id, sessionId);
          if (['failed', 'publication_unverified'].includes(build.status)) {
            await update(id, { ci: ciObservation(build, record.ci), ...ciFailure(build) });
            record = publicRecord(find('deployments', id, sessionId));
          }
        } catch { /* Keep the recorded failure when read-only diagnostics are unavailable. */ }
      }
      return { ...record, observation: await observeMetrics(record) };
    },
    async getDeploymentLogs(id, sessionId = null) {
      const record = publicRecord(find('deployments', id, sessionId));
      const empty = (state) => ({ deployment_id: id, target_id: record.target_id, app: record.app,
        state, checked_at: new Date().toISOString(), entries: [] });
      if (!record.cd?.deployed) return empty('not_deployed');
      // A shared target can be reused even when its image/revision does not change.
      // Only the most recently admitted CD operation may expose its runtime logs.
      const current = () => Object.values(store.read().operations).reverse().find((row) => row.kind === 'deployments'
        && row.target_id === record.target_id && row.app === record.app && row.cd?.state !== 'not_started')?.id === id;
      if (!current()) return empty('superseded');
      const observer = record.environment_id ? environmentAdapter?.observeLogs : observeLogs;
      if (!observer) return empty('not_configured');
      try {
        const logs = await (record.environment_id ? observer(record.environment_id, record) : observer(record));
        return current() ? logs : empty('superseded');
      }
      catch { return empty('unavailable'); }
    },
    profiles() { return environmentAdapter?.profiles() || []; },
    async createPlan(input, sessionId = null) {
      if (!environmentAdapter) throw unavailable();
      return store.transaction(async (state) => {
        checkFree(state);
        if (Object.keys(state.plans).length >= maxOperations) throw new ProductError(409, 'CAPACITY_EXCEEDED', '계획 보관 한도에 도달했습니다.');
        const id = randomUUID(), plan = await environmentAdapter.plan(input, { id });
        plan.session_id = sessionId;
        state.plans[id] = plan;
        return plan.public;
      });
    },
    getPlan(id, sessionId = null) { const plans = store.read().plans, plan = Object.hasOwn(plans, id) ? plans[id] : null; if (!owns(plan, sessionId)) throw new ProductError(404, 'NOT_FOUND', '계획을 찾을 수 없습니다.'); return plan.public; },
    async createEnvironment(input, key, sessionId = null) {
      idempotencyKey(key);
      if (!input || Object.keys(input).length !== 1 || typeof input.plan_id !== 'string') throw invalid('plan_id만 입력하세요.');
      if (!environmentAdapter) throw unavailable();
      const accepted = await store.transaction(async (state) => {
        const fingerprint = digest(input), existingId = state.keys[scopeKey('environments', key, sessionId)];
        if (existingId) {
          const record = state.operations[existingId];
          if (record.fingerprint !== fingerprint) throw new ProductError(409, 'IDEMPOTENCY_CONFLICT', '같은 키로 다른 계획을 실행할 수 없습니다.');
          return { record, replay: true };
        }
        const plan = Object.hasOwn(state.plans, input.plan_id) ? state.plans[input.plan_id] : null;
        if (!owns(plan, sessionId)) throw new ProductError(404, 'NOT_FOUND', '계획을 찾을 수 없습니다.');
        if (plan.environment_id) throw new ProductError(409, 'CONFLICT', '이미 실행에 사용된 계획입니다.');
        checkFree(state);
        checkCapacity(state);
        await environmentAdapter.verifyPlan(plan);
        const id = randomUUID();
        const record = { id, kind: 'environments', session_id: sessionId, plan_id: input.plan_id, status: 'queued', stage: 'provision', error: null, fingerprint, created_at: new Date().toISOString() };
        state.operations[id] = record; state.keys[scopeKey('environments', key, sessionId)] = id; plan.environment_id = id;
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
    getEnvironment(id, sessionId = null) { return publicRecord(find('environments', id, sessionId)); },
    async close() { abort.abort(); await Promise.allSettled(workers); await store.close(); },
  };
}
