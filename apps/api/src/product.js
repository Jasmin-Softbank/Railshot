import { createHash, randomUUID } from 'node:crypto';
import { setTimeout as pause } from 'node:timers/promises';
import { createProductStore } from './product-store.js';
import { APP_NAME, TARGET_ID, sourceAppName } from './contract.js';
import { validateFiles } from './archive.js';
import { createMetricsObserver } from './metrics.js';
import { emptyAgentEvents } from './agent-events.js';
import { EnvironmentError } from './environments.js';

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
  const { fingerprint, key, source, source_bytes, legacy, session_id, publication, ...visible } = record;
  return structuredClone(visible);
}
function checkFree(state, sessionId = null) {
  const blocker = Object.values(state.operations).find(active);
  if (!blocker) return;
  // ponytail: one shared Git branch and runtime writer; per-app admission needs isolated writers first.
  const detail = blocker.session_id === sessionId
    ? ` ${blocker.app || '환경'} · ${blocker.stage || '접수'} · 마지막 갱신 ${blocker.updated_at || blocker.created_at || '확인 불가'}.` : '';
  throw new ProductError(409, 'EXECUTOR_BUSY', `다른 실행 또는 결과 확인이 끝나지 않았습니다.${detail} 이번 요청은 실행 대기열에 추가되지 않았습니다.`, { retryable: blocker.status !== 'unknown' });
}

export async function createProductService({ service, directory, target, providerTargets, deployPublished, environmentAdapter, applicationAdapter, observeMetrics = createMetricsObserver(), observeLogs, pollInterval = 2000, maxOperations = 100, maxSourceBytes = 512 * 1024 * 1024 }) {
  const targetId = target?.id || service?.targetId;
  if (targetId && !TARGET_ID.test(targetId)) throw invalid('등록된 대상 ID가 잘못되었습니다.');
  const selections = new Map(target?.provider && targetId ? [[target.provider, targetId]] : []);
  if (providerTargets !== undefined) {
    if (!providerTargets || Array.isArray(providerTargets) || typeof providerTargets !== 'object') throw invalid('공급자별 대상 설정이 잘못되었습니다.');
    for (const [provider, id] of Object.entries(providerTargets)) {
      if (!['aws', 'gcp', 'openstack', 'proxmox'].includes(provider) || typeof id !== 'string' || !TARGET_ID.test(id)
          || selections.has(provider) && selections.get(provider) !== id
          || [...selections].some(([other, value]) => other !== provider && value === id)) throw invalid('공급자별 대상 설정이 잘못되었습니다.');
      selections.set(provider, id);
    }
  }
  const store = await createProductStore(directory);
  const abort = new AbortController();
  function checkCapacity(state, sourceBytes = 0) {
    if (Object.keys(state.operations).length >= maxOperations || store.snapshotBytes() + sourceBytes > maxSourceBytes) throw new ProductError(409, 'CAPACITY_EXCEEDED', 'workspace 보관 한도에 도달했습니다. 운영자가 저장소를 확인해야 합니다.');
  }
  const workers = new Set();
  const sharedTargets = new Set(service?.targetIds || (service?.targetId ? [service.targetId] : []));
  const scopeKey = (kind, key, sessionId) => `${sessionId ? sessionId + ':' : ''}${kind}:${key}`;
  const owns = (value, sessionId) => value && (!sessionId || value.session_id === sessionId);
  function providerSelection(provider) {
    const id = selections.get(provider);
    const environment = id && applicationAdapter?.targets?.[id];
    if (environment) return { id, cdTarget: null, available: Boolean(service && environment.provider === provider && environment.automaticDelivery !== false) };
    const cdTarget = id && deployPublished?.targets && Object.hasOwn(deployPublished.targets, id) ? deployPublished.targets[id] : null;
    // Older single-target adapters have no registry; they authorize only the existing default.
    const available = Boolean(service && id && sharedTargets.has(id) && deployPublished
      && (cdTarget || !deployPublished.targets && id === targetId));
    return { id, cdTarget, available };
  }
  function deploymentOptions() {
    return [['cloud', 'aws', '클라우드 · AWS'], ['cloud', 'gcp', '클라우드 · Google Cloud'], ['onprem', 'openstack', '온프레미스 · OpenStack'], ['onprem', 'proxmox', '온프레미스 · Proxmox']].map(([environment, provider, label]) => {
      const { cdTarget, available } = providerSelection(provider);
      return { id: `${environment}-${provider}`, environment, provider, label, available,
        message: available ? `소스 검사부터 앱 배포와 URL 확인까지 진행합니다.${cdTarget?.applicationName ? ` 등록된 앱 ${cdTarget.applicationName}의 소스를 갱신합니다.` : ''}`
          : `${label}의 앱 배포 설정이 아직 준비되지 않았습니다.` };
    });
  }
  function resolveSelection(input) {
    if (!input.deployment_selection) return input;
    if (input.app !== undefined || input.target_id !== undefined || input.plan_id !== undefined) throw invalid('환경 선택과 직접 대상·계획 지정을 함께 사용할 수 없습니다.');
    const { environment, provider } = input.deployment_selection;
    const option = deploymentOptions().find((item) => item.environment === environment && item.provider === provider);
    if (!option) throw invalid('배포 환경과 인프라 종류를 확인하세요.');
    if (!option.available) throw new ProductError(409, 'CAPABILITY_UNAVAILABLE', option.message);
    const { id } = providerSelection(provider);
    const name = input.source_name ?? input.repository_url?.split('/').filter(Boolean).at(-1)?.replace(/\.git$/i, '');
    let app;
    try { app = sourceAppName(name); } catch (error) { throw invalid(`소스 이름을 확인하세요. ${error.message}`); }
    return { ...input, app, target_id: id };
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
      return { environment_id: environmentId, applicationName: plan.public.name, provider: plan.private.profile.provider ?? null,
        database_configuration: environment.database?.status === 'succeeded' };
    }
    return null;
  }
  // Restore only successful registrations; interrupted intents still occupy checkFree admission.
  const restored = store.read();
  for (const app of Object.values(restored.applications)) if (app.status === 'ready') service?.allowTarget?.(app.target_id);
  for (const operation of Object.values(restored.operations)) {
    const id = operation.kind === 'environments' ? operation.runtime_target_id : operation.environment?.runtime_target_id;
    if (registeredEnvironment(restored, id)) service?.allowTarget?.(id);
  }
  function inputFingerprint(input) {
    return digest({ app: input.app, target_id: input.target_id, type: input.source_type, ...(input.environment_target_id ? { environment_target_id: input.environment_target_id } : {}), ...(input.plan_id ? { plan_id: input.plan_id } : {}),
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
      const application = kind === 'deployments' && input.environment_target_id
        ? applicationAdapter.describe(input.environment_target_id, input.app) : null;
      const previousApplication = application && state.applications[application.id];
      if (application && applicationAdapter.targets[application.environment_target_id]?.automaticDelivery === false) throw unavailable();
      if (previousApplication && previousApplication.session_id !== sessionId) throw new ProductError(409, 'APPLICATION_OWNERSHIP_CONFLICT', '같은 환경의 이 앱 이름은 다른 세션에 등록되어 있습니다. 다른 이름을 사용하세요.');
      if (previousApplication && !['queued', 'ready'].includes(previousApplication.status)) throw new ProductError(409, 'APPLICATION_RECONCILE_REQUIRED', '이 앱 등록 결과를 운영자가 확인해야 합니다. 자동 재등록하지 않습니다.');
      const selectedCdAvailable = Boolean(deployPublished && (!deployPublished.targets || selectedCdTarget));
      const savedEnvironment = registeredEnvironment(state, input.target_id, sessionId);
      const registered = !selectedCdAvailable && !input.plan_id && kind === 'deployments' ? savedEnvironment : null;
      const admittedTarget = sharedTargets.has(input.target_id) || Boolean(savedEnvironment) || Boolean(application);
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
      if (kind === 'deployments' && !input.plan_id && !selectedCdAvailable && !registered && !application) throw unavailable();
      checkFree(state, sessionId);
      checkCapacity(state);
      const applicationName = selectedCdTarget?.applicationName || savedEnvironment?.applicationName;
      if (!input.plan_id && applicationName && (kind === 'deployments' || savedEnvironment) && input.app !== applicationName) throw invalid(`등록된 배포 앱 이름과 일치하지 않습니다. 이 대상은 ${applicationName} 전용입니다. ${input.app} 배포에는 새 앱용 환경 또는 같은 이름의 앱 등록이 필요합니다.`);
      const source = input.files ? input : { ...input, ...await materialize(input.repository_url) };
      const files = validateFiles(source.files);
      const sourceBytes = files.reduce((sum, file) => sum + Buffer.byteLength(file.path) + Math.ceil(file.content.length / 3) * 4 + 128, 0);
      checkCapacity(state, sourceBytes);
      const id = randomUUID(), now = new Date().toISOString();
      await store.snapshot(id, files);
      if (application && !previousApplication) state.applications[application.id] = { ...application, session_id: sessionId, status: 'queued', created_at: now };
      const record = { id, kind, session_id: sessionId, app: input.app, target_id: input.target_id,
        ...(application ? { application_id: application.id, environment_target_id: application.environment_target_id } : {}),
        ...(plan ? { plan_id: input.plan_id, environment_id: `${id}.environment`, environment: { status: 'queued' } } : registered ? { environment_id: registered.environment_id } : {}), status: 'queued', stage: application ? 'registration' : plan ? 'environment' : 'ci',
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
        const application = record.application_id && store.read().applications[record.application_id];
        const registered = registeredEnvironment(store.read(), record.target_id);
        const admitted = application?.status === 'ready' && application.app === record.app && application.target_id === record.target_id
          || registered?.applicationName === record.app;
        if (!admitted || typeof service.allowTarget !== 'function') throw unavailable();
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
  async function observe(record, runId, { resume = false } = {}) {
    try {
      for (;;) {
        if (abort.signal.aborted) return;
        const build = await readBuild(runId);
        if (resume) {
          const published = build.publication;
          const imageEntries = (images) => Object.entries(images || {}).sort(([a], [b]) => a.localeCompare(b));
          if (build.status !== 'published' || build.source_commit !== record.source_commit
              || String(published?.run_id) !== runId || published?.source_commit !== record.source_commit
              || published?.app !== record.app || published?.target_id !== record.target_id
              || String(published?.artifact_id) !== record.ci.publication_artifact_id
              || published?.producer_attempt !== record.ci.producer_attempt
              || digest(imageEntries(published?.images)) !== digest(imageEntries(record.ci.images))) {
            throw new EnvironmentError('RESUME_PUBLICATION_CHANGED', 409, true);
          }
        }
        const ci = ciObservation(build);
        await update(record.id, { ci, ...(build.publication ? { publication: build.publication } : {}) });
        if (build.status === 'published') {
          if (record.kind === 'builds') { await update(record.id, { status: 'succeeded', stage: 'ci' }); return; }
          if (!resume || !record.cd?.deployed) await update(record.id, { stage: 'cd', cd: { state: 'running', revision: null, deployed: false } });
          const deploy = record.application_id ? (args) => applicationAdapter.deployPublished(store.read().applications[record.application_id], args)
            : record.environment_id ? (args) => environmentAdapter.deployPublished(record.environment_id, args) : deployPublished;
          const result = await deploy({ deploymentId: record.id, app: record.app, targetId: record.target_id,
            sourceCommit: build.source_commit, publication: build.publication, signal: abort.signal,
            onProgress: (progress) => update(record.id, { stage: progress.cd?.deployed ? 'http' : 'cd', cd: progress.cd, public_http: progress.public_http }) });
          const succeeded = result.cd?.deployed === true && typeof result.cd.revision === 'string' && result.cd.revision.length > 0 && result.public_http?.state === 'succeeded' && result.public_http.verified_at && /^https?:\/\//.test(result.public_http.url || '');
          const status = succeeded ? 'succeeded' : resume || result.error?.outcome_unknown ? 'unknown' : ['blocked', 'failed'].includes(result.cd?.state) ? result.cd.state : 'unknown';
          await update(record.id, { status, stage: succeeded ? 'complete' : result.cd?.deployed ? 'http' : 'cd', cd: result.cd, public_http: result.public_http,
            url: succeeded ? (result.public_http.site_url || result.public_http.url) : null, error: succeeded ? null : operationError(result.error?.code || 'CD_UNVERIFIED', status === 'unknown') });
          return;
        }
        if (['failed', 'publication_unverified'].includes(build.status)) {
          await update(record.id, ciFailure(build)); return;
        }
        await pause(pollInterval, undefined, { signal: abort.signal, ref: false });
      }
    } catch (error) {
      const known = error instanceof EnvironmentError;
      const unknown = resume || !known || error.outcomeUnknown;
      if (!abort.signal.aborted) await update(record.id, { status: unknown ? 'unknown' : 'blocked', error: operationError(known ? error.code : 'CD_OUTCOME_UNKNOWN', unknown) });
    }
  }
  // Updates retain the existing deployment identity, worker and durable source snapshot.
  const sourceDigest = (files) => digest(files.map(({ path, content }) => [path, createHash('sha256').update(content).digest('hex')]).sort());
  const sourceUnavailable = () => new ProductError(409, 'SOURCE_NOT_AVAILABLE', '이 배포의 검증된 소스를 제공할 수 없습니다.');
  function applicationRecord(state, id, sessionId) {
    const application = Object.hasOwn(state.applications, id) && state.applications[id];
    if (!owns(application, sessionId)) throw new ProductError(404, 'NOT_FOUND', '앱 등록을 찾을 수 없습니다.');
    return application;
  }
  function successfulDeployment(record) {
    return record.status === 'succeeded' && record.cd?.deployed === true && record.cd?.revision
      && record.public_http?.state === 'succeeded' && record.public_http.verified_at && /^https?:\/\//.test(record.public_http.url || '');
  }
  function applicationVersions(state, application) {
    const rows = Object.values(state.operations).filter((row) => row.kind === 'deployments'
      && row.application_id === application.id && row.app === application.app && row.target_id === application.target_id
      && row.session_id === application.session_id).reverse();
    const current = rows.find(successfulDeployment) || null;
    const newer = current ? rows.slice(0, rows.indexOf(current)) : rows;
    const uncertain = newer.some((row) => row.cd?.state === 'running' || row.cd?.state === 'unknown'
      || row.cd?.deployed === true || row.status === 'unknown' && ['cd', 'http'].includes(row.stage));
    return { current, latest: rows[0] || null, state: uncertain ? 'unverified' : current ? 'verified' : 'not_deployed' };
  }
  function publicApplication(state, application) {
    const versions = applicationVersions(state, application);
    return { ...publicRecord(application), current_deployment: versions.current ? publicRecord(versions.current) : null,
      latest_deployment: versions.latest ? publicRecord(versions.latest) : null, current_deployment_state: versions.state };
  }
  function readyApplication(state, id, sessionId) {
    const application = applicationRecord(state, id, sessionId);
    if (application.status !== 'ready' || applicationVersions(state, application).state === 'unverified')
      throw new ProductError(409, 'APPLICATION_RECONCILE_REQUIRED', '앱 등록 또는 현재 배포 버전을 먼저 확인해야 합니다.');
    if (!service || typeof applicationAdapter?.deployPublished !== 'function') throw unavailable();
    return application;
  }
  async function submittedFiles(record) {
    let files;
    try { files = await store.readSnapshot(record.id); }
    catch { throw sourceUnavailable(); }
    if (sourceDigest(files) !== record.source_digest) throw sourceUnavailable();
    return files;
  }
  async function deployedFiles(record) {
    if (!successfulDeployment(record) || typeof service?.sourceFiles !== 'function') throw sourceUnavailable();
    const publication = record.publication || (await readBuild(String(record.ci.run_id), record.session_id)).publication;
    if (!publication || String(publication.run_id) !== String(record.ci.run_id) || publication.app !== record.app
        || publication.target_id !== record.target_id || publication.source_commit !== record.source_commit)
      throw new ProductError(502, 'SOURCE_VERIFICATION_FAILED', '배포와 소스 게시 기록의 연결을 확인할 수 없습니다.');
    const files = await service.sourceFiles(publication);
    if (files === null) throw sourceUnavailable();
    try { return validateFiles(files); }
    catch { throw new ProductError(502, 'SOURCE_INVALID', '검증된 배포 소스의 파일 목록이 잘못되었습니다.'); }
  }
  async function createUpdate(applicationId, input, key, materialize, sessionId = null) {
    key = idempotencyKey(key);
    return store.transaction(async (state) => {
      const application = applicationRecord(state, applicationId, sessionId);
      if (!input || typeof input !== 'object' || Array.isArray(input)
          || Object.keys(input).some((name) => !['source_type', 'files', 'repository_url', 'source_name'].includes(name))
          || !['folder', 'zip', 'github'].includes(input.source_type)
          || (input.source_type === 'github' ? typeof input.repository_url !== 'string' || input.files !== undefined
            : !Array.isArray(input.files) || input.repository_url !== undefined)) throw invalid('업데이트에는 새 소스만 입력하세요. 앱과 배포 대상은 변경할 수 없습니다.');
      const fingerprint = inputFingerprint({ ...input, app: application.app, target_id: application.target_id });
      const scopedKey = scopeKey(`updates:${applicationId}`, key, sessionId), existingId = state.keys[scopedKey];
      if (existingId) {
        const existing = state.operations[existingId];
        if (existing.fingerprint !== fingerprint) throw new ProductError(409, 'IDEMPOTENCY_CONFLICT', '같은 키로 다른 입력을 보낼 수 없습니다.');
        return publicRecord(existing);
      }
      readyApplication(state, applicationId, sessionId);
      const baseline = applicationVersions(state, application).current;
      if (!baseline) throw new ProductError(409, 'BASE_DEPLOYMENT_REQUIRED', '업데이트 기준으로 사용할 성공한 배포가 없습니다.');
      checkCapacity(state);
      let before, baselineKind = 'deployed';
      try { before = await deployedFiles(baseline); }
      catch (error) {
        if (error.code !== 'SOURCE_NOT_AVAILABLE') throw error;
        before = await submittedFiles(baseline); baselineKind = 'submitted';
      }
      const source = input.source_type === 'github' ? await materialize(input.repository_url) : input;
      const files = validateFiles(source.files);
      const origin = input.source_type === 'github' ? source.source : null;
      if (origin && (origin.type !== 'github' || !/^https:\/\/github\.com\/[A-Za-z0-9-]+\/[A-Za-z0-9._-]+$/.test(origin.repository)
          || !/^[a-f0-9]{40}$/.test(origin.sha)) || input.source_type === 'github' && !origin) throw invalid('GitHub 소스의 저장소와 고정 SHA를 확인할 수 없습니다.');
      const previous = new Map(before.map((file) => [file.path, file.content]));
      const incoming = new Set(files.map((file) => file.path));
      const changes = { added: [], modified: [], deleted: [...previous.keys()].filter((path) => !incoming.has(path)).sort(), unchanged: 0 };
      for (const file of files) {
        const old = previous.get(file.path);
        if (old === undefined) changes.added.push(file.path);
        else if (!old.equals(file.content)) changes.modified.push(file.path);
        else changes.unchanged++;
      }
      changes.added.sort(); changes.modified.sort();
      const sourceBytes = files.reduce((sum, file) => sum + Buffer.byteLength(file.path) + Math.ceil(file.content.length / 3) * 4 + 128, 0);
      checkCapacity(state, sourceBytes);
      const id = randomUUID(), now = new Date().toISOString();
      await store.snapshot(id, files);
      const record = { id, kind: 'deployments', application_id: application.id, app: application.app, target_id: application.target_id,
        environment_target_id: application.environment_target_id, session_id: sessionId, status: 'preview', stage: 'review',
        base_deployment_id: baseline.id, base_revision: baseline.cd.revision, baseline_kind: baselineKind,
        expires_at: new Date(Date.now() + 30 * 60 * 1000).toISOString(), changes,
        no_changes: baselineKind === 'deployed' && !changes.added.length && !changes.modified.length && !changes.deleted.length,
        source_comparison_only: baselineKind === 'submitted',
        source_origin: origin ? { type: 'github', repository: origin.repository, sha: origin.sha } : null,
        source: origin, source_bytes: sourceBytes, source_digest: sourceDigest(files), fingerprint, key,
        ci: { run_id: null, state: 'not_started', steps: [], publication_artifact_id: null, producer_attempt: null },
        cd: { state: 'not_started', revision: null, deployed: false }, public_http: { state: 'not_run', verified_at: null, url: null },
        url: null, actions_url: null, error: null, created_at: now, updated_at: now };
      state.operations[id] = record; state.keys[scopedKey] = id;
      return publicRecord(record);
    });
  }
  async function startUpdate(id, options = {}, sessionId = null) {
    if (!options || typeof options !== 'object' || Array.isArray(options) || Object.keys(options).some((key) => key !== 'rebuild')
        || options.rebuild !== undefined && typeof options.rebuild !== 'boolean') throw invalid('rebuild는 참 또는 거짓이어야 합니다.');
    const accepted = await store.transaction(async (state) => {
      const record = state.operations[id];
      if (!owns(record, sessionId) || record.kind !== 'deployments' || !record.base_deployment_id) throw new ProductError(404, 'NOT_FOUND', '업데이트를 찾을 수 없습니다.');
      if (record.status !== 'preview') return { record, replay: true };
      if (Date.parse(record.expires_at) <= Date.now()) throw new ProductError(409, 'UPDATE_EXPIRED', '미리보기가 만료되었습니다. 새 소스를 다시 확인하세요.');
      const application = readyApplication(state, record.application_id, sessionId);
      const current = applicationVersions(state, application).current;
      if (current?.id !== record.base_deployment_id || current.cd.revision !== record.base_revision)
        throw new ProductError(409, 'UPDATE_BASE_CHANGED', '기준 배포가 변경되었습니다. 미리보기를 다시 만드세요.');
      checkFree(state, sessionId);
      const files = await submittedFiles(record);
      Object.assign(record, { status: record.no_changes && !options.rebuild ? 'unchanged' : 'queued',
        stage: record.no_changes && !options.rebuild ? 'complete' : 'ci', rebuild: options.rebuild === true, updated_at: new Date().toISOString() });
      return { record, replay: record.status === 'unchanged', input: { app: record.app, target_id: record.target_id, files, source: record.source } };
    });
    if (!accepted.replay) launch(async () => {
      let result;
      try { result = await submit(accepted.record, accepted.input); }
      catch (error) { if (error instanceof ProductError && error.outcomeUnknown) return; throw error; }
      await observe(accepted.record, String(result.run_id));
    });
    return publicRecord(accepted.record);
  }
  return {
    dashboard: store.dashboard,
    createUpdate, startUpdate,
    async sourceFiles(id, variant, sessionId = null) {
      const record = find('deployments', id, sessionId);
      if (!['submitted', 'deployed'].includes(variant)) throw invalid('지원하지 않는 소스 종류입니다.');
      return variant === 'submitted' ? submittedFiles(record) : deployedFiles(record);
    },
    applications(sessionId = null) { const state = store.read(); return Object.values(state.applications).filter((row) => owns(row, sessionId)).map((row) => publicApplication(state, row)); },
    getApplication(id, sessionId = null) {
      const state = store.read();
      return publicApplication(state, applicationRecord(state, id, sessionId));
    },
    list(kind, sessionId, pagination) {
      if (kind === 'plans') return Object.values(store.read().plans).filter((row) => owns(row, sessionId)).reverse().map((row) => structuredClone(row.public));
      const { records, hasMore, total } = store.operationPage(kind, sessionId, pagination);
      const items = records.map((row) => ({ id: kind === 'builds' ? String(row.ci.run_id) : row.id, kind, status: row.status,
        ...Object.fromEntries(['app', 'target_id', 'stage', 'created_at', 'updated_at'].filter((key) => row[key] !== undefined).map((key) => [key, row[key]])) }));
      return { items, next_marker: hasMore ? items.at(-1).id : null, total };
    },
    deploymentOptions,
    targets(sessionId = null) {
      const state = store.read();
      const ids = new Set(sharedTargets);
      for (const id of Object.keys(applicationAdapter?.targets || {})) ids.add(id);
      for (const operation of Object.values(state.operations)) {
        const id = operation.kind === 'environments' ? operation.runtime_target_id : operation.environment?.runtime_target_id;
        if (registeredEnvironment(state, id, sessionId)) ids.add(id);
      }
      return [...ids].map((id) => {
        const staticTarget = deployPublished?.targets?.[id];
        const staticAvailable = Boolean(deployPublished && (!deployPublished.targets || staticTarget));
        const registered = staticAvailable ? staticTarget : registeredEnvironment(state, id, sessionId);
        const applicationEnvironment = applicationAdapter?.targets?.[id];
        const available = Boolean(applicationEnvironment && applicationEnvironment.automaticDelivery !== false) || staticAvailable || Boolean(registered);
        return { id, label: id === targetId ? target?.label || id : id,
          provider: applicationEnvironment?.provider || [...selections].find(([, selected]) => selected === id)?.[0] || registered?.provider || null, environment: 'registered',
          environment_id: registered?.environment_id || null,
          ...(applicationEnvironment ? { deployment_scope: 'environment' } : registered?.applicationName ? { application_name: registered.applicationName, deployment_scope: 'registered_application' } : {}),
          capabilities: { ci_submission: Boolean(service), application_deployment: available,
            database_configuration: !staticAvailable && registered?.database_configuration === true },
          runtime: { status: 'unknown', observed_at: null }, blockers: available ? [] : ['CD_ADAPTER_NOT_CONFIGURED'] };
      });
    },
    async getTargetObservation(id, sessionId = null) {
      const target = TARGET_ID.test(id) && this.targets(sessionId).find((item) => item.id === id);
      if (!target) throw new ProductError(404, 'NOT_FOUND', '등록된 대상을 찾을 수 없습니다.');
      const observation = await observeMetrics({ target_id: id, app: target.application_name ?? null });
      const health = observation.metrics.runtime_healthz ?? { state: 'not_configured', observed_at: null };
      return { ...observation, environment_id: target.environment_id,
        runtime: { status: health.state === 'ready' ? (health.value === 1 ? 'healthy' : 'unhealthy') : 'unknown',
          observation_state: health.state, observed_at: health.observed_at } };
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
      if (!input.plan_id && applicationAdapter?.targets?.[input.target_id]) {
        const application = applicationAdapter.describe(input.target_id, input.app);
        input = { ...input, environment_target_id: input.target_id, target_id: application.target_id };
      }
      const reserved = await reserve('deployments', input, idempotencyKey(key), materialize, sessionId);
      if (!reserved.replay) launch(async () => {
        try {
          if (reserved.record.application_id && store.read().applications[reserved.record.application_id]?.status !== 'ready') {
            const appId = reserved.record.application_id;
            const application = store.read().applications[appId];
            await update(reserved.record.id, { status: 'running', stage: 'registration' });
            await store.transaction((state) => { state.applications[appId].status = 'registering'; });
            try {
              const registered = await applicationAdapter.register(application);
              await store.transaction((state) => { Object.assign(state.applications[appId], registered); });
            } catch (error) {
              const unknown = error.outcomeUnknown !== false;
              // This read-only preflight precedes the native registration intent; only a new explicit request may retry it.
              const unstarted = error.code === 'APPLICATION_AWS_ROUTE_PREFLIGHT_FAILED' && error.outcomeUnknown === false;
              await store.transaction((state) => { state.applications[appId].status = unstarted ? 'queued' : unknown ? 'unknown' : 'blocked'; });
              await update(reserved.record.id, { status: unknown ? 'unknown' : 'blocked', error: operationError(error.code || 'APPLICATION_REGISTRATION_FAILED', unknown) });
              return;
            }
          }
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
    async resumeDeployment(id, sessionId = null) {
      const record = await store.transaction((state) => {
        const operation = Object.hasOwn(state.operations, id) ? state.operations[id] : null;
        const application = operation?.application_id && state.applications[operation.application_id];
        // Unlike the legacy maintenance reads, resume always requires an exact cookie-session owner.
        if (!sessionId || operation?.kind !== 'deployments' || operation.session_id !== sessionId
            || !application || application.session_id !== sessionId) {
          throw new ProductError(404, 'NOT_FOUND', '이 세션에서 재개할 배포를 찾을 수 없습니다.');
        }
        const ci = operation.ci;
        if (operation.status !== 'unknown' || !['cd', 'http'].includes(operation.stage)
            || application.status !== 'ready' || application.deletion_requested || application.lifecycle_operation_id
            || ci?.state !== 'published'
            || typeof applicationAdapter?.deployPublished !== 'function'
            || !/^\d+$/.test(ci.run_id) || !/^[a-f0-9]{40}$/.test(operation.source_commit || '')
            || typeof ci.publication_artifact_id !== 'string' || !/^[1-9]\d*$/.test(ci.publication_artifact_id)
            || !Number.isSafeInteger(ci.producer_attempt) || ci.producer_attempt < 1
            || !ci.images || typeof ci.images !== 'object' || Array.isArray(ci.images) || !Object.keys(ci.images).length
            || Object.values(ci.images).some((image) => typeof image !== 'string' || !/^ghcr\.io\/[a-z0-9._/-]+@sha256:[a-f0-9]{64}$/.test(image))) {
          throw new ProductError(409, 'DEPLOYMENT_NOT_RESUMABLE', '게시 완료 후 결과가 불확실한 앱 배포만 재개할 수 있습니다.');
        }
        const binding = state.bindings[ci.run_id];
        const expected = applicationAdapter.describe(operation.environment_target_id, operation.app);
        if (!binding || binding.operation_id !== id || binding.app !== operation.app
            || binding.target_id !== operation.target_id || binding.source_commit !== operation.source_commit
            || application.id !== operation.application_id || application.target_id !== operation.target_id
            || application.app !== operation.app || application.environment_target_id !== operation.environment_target_id
            || expected.id !== application.id || expected.target_id !== application.target_id
            || expected.provider !== application.provider) {
          throw new ProductError(409, 'RESUME_BINDING_MISMATCH', '원래 앱·환경·CI 실행과 연결이 일치하지 않습니다.');
        }
        if (Object.values(state.operations).some((other) => other.id !== id && active(other))) {
          throw new ProductError(409, 'EXECUTOR_BUSY', '다른 실행 또는 결과 확인이 끝나지 않았습니다.', { retryable: true });
        }
        Object.assign(operation, { status: 'running', error: null, resumed_at: new Date().toISOString(),
          resume_count: (operation.resume_count || 0) + 1, updated_at: new Date().toISOString() });
        return operation;
      });
      launch(() => observe(record, String(record.ci.run_id), { resume: true }));
      return publicRecord(record);
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
    async getDeploymentEvents(id, sessionId = null) {
      const record = find('deployments', id, sessionId);
      const identity = { runId: record.ci?.run_id ? String(record.ci.run_id) : null,
        source_commit: record.source_commit, app: record.app, target_id: record.target_id };
      const empty = (state, reason) => ({ deployment_id: id, ...emptyAgentEvents(identity, state, reason) });
      if (!identity.runId) return empty('not_started', 'not_dispatched');
      const bound = () => {
        const current = find('deployments', id, sessionId), state = store.read();
        const binding = Object.hasOwn(state.bindings, identity.runId) ? state.bindings[identity.runId] : null;
        return binding?.operation_id === id && binding.app === identity.app && binding.target_id === identity.target_id
          && binding.source_commit === identity.source_commit && current.source_commit === identity.source_commit
          && String(current.ci?.run_id) === identity.runId && current.app === identity.app && current.target_id === identity.target_id;
      };
      if (!bound()) return empty('unavailable', 'binding_mismatch');
      if (typeof service?.events !== 'function') return empty('unavailable', 'not_configured');
      try {
        const observed = await service.events(identity.runId, { source_commit: identity.source_commit, app: identity.app, target_id: identity.target_id });
        return bound() ? { ...observed, deployment_id: id } : empty('unavailable', 'binding_mismatch');
      } catch { return empty('unavailable', 'upstream_unavailable'); }
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
      const observer = record.application_id ? (value) => applicationAdapter?.observeLogs(store.read().applications[record.application_id], value)
        : record.environment_id ? environmentAdapter?.observeLogs : observeLogs;
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
        checkFree(state, sessionId);
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
        checkFree(state, sessionId);
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
