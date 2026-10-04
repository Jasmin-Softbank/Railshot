import { randomUUID, createHash } from 'node:crypto';

export const RESERVED_VARIABLES = new Set(['PORT', 'DATABASE_URL', 'MIGRATION_DATABASE_URL', 'PGHOST', 'PGPORT', 'PGUSER', 'PGPASSWORD', 'PGDATABASE', 'PGSSLMODE', 'PGSSLROOTCERT']);
const now = () => new Date().toISOString();
const exact = (v, required, optional = []) => v && !Array.isArray(v) && typeof v === 'object' && required.every(k => Object.hasOwn(v, k)) && Object.keys(v).every(k => [...required, ...optional].includes(k));
const hash = value => createHash('sha256').update(JSON.stringify(value)).digest('hex');
const coordinate = row => JSON.stringify([row.name, row.scope, row.environment_id || null]);
const context = (project, revision, row) => JSON.stringify([project, revision, row.name, row.scope, row.environment_id || null]);
const publicProject = ({ session_id, ...rest }) => structuredClone(rest);
const publicOperation = ({ session_id, fingerprint, key, ...rest }) => structuredClone(rest);

export function createProjectService({ store, cipher, adapter, applicationAdapter, ErrorType, checkFree, checkCapacity, launch, update, executeTransfer, executeDelivery, reconcile, environmentTargets = () => applicationAdapter?.targets || {}, describeEnvironment = (id, app) => applicationAdapter.describe(id, app) }) {
  const fail = (code, message, status = 409) => new ErrorType(status, code, message);
  const invalid = () => fail('INVALID_INPUT', '프로젝트 설정 입력을 확인하세요.', 422);
  const notFound = () => fail('NOT_FOUND', '프로젝트 자원을 찾을 수 없습니다.', 404);
  function projectFor(state, id, sessionId) {
    const p = Object.hasOwn(state.projects, id) && state.projects[id];
    if (!p || p.session_id !== sessionId) throw notFound();
    return p;
  }
  function revisionFor(state, p, id) {
    const revision = id && state.revisions[id];
    if (!revision || revision.project_id !== p.id) throw notFound();
    return revision;
  }
  function bindingFor(state, p, id) {
    const b = state.project_bindings[id];
    if (!b || b.project_id !== p.id) throw notFound();
    const app = state.applications[b.application_id];
    if (!app || app.session_id !== p.session_id || app.project_id !== p.id) throw notFound();
    return { ...b, status: app.status };
  }
  function available() { if (!cipher) throw fail('CAPABILITY_UNAVAILABLE', '환경 밖의 관리 암호화 키를 운영자가 설정해야 합니다.'); }
  function decrypt(revision, row) {
    if (!row.encrypted) return undefined;
    available();
    try { return cipher.open(row.encrypted, context(revision.project_id, revision.id, row)); }
    catch { throw fail('CONFIGURATION_KEY_UNAVAILABLE', '저장 버전을 복호화할 관리 키를 확인해야 합니다.', 503); }
  }
  function busyProject(state, p) {
    if (Object.values(state.operations).some(o => o.project_id === p.id && ['queued', 'running', 'unknown'].includes(o.status)))
      throw fail('PROJECT_BUSY', '진행 중인 설정 적용 또는 이전 결과를 먼저 확인하세요.');
  }
  function dedup(state, sessionId, resource, key, input) {
    if (typeof key !== 'string' || !/^[A-Za-z0-9._-]{1,128}$/.test(key)) throw invalid();
    const id = JSON.stringify([sessionId, resource, key]), old = state.project_keys[id];
    const fingerprint = cipher ? cipher.fingerprint(input, old?.fingerprint?.key_id) : { digest: hash(input), key_id: null };
    if (old && old.fingerprint.digest !== fingerprint.digest) throw fail('IDEMPOTENCY_CONFLICT', '같은 요청 키에 다른 입력을 사용할 수 없습니다.');
    return { old: old?.result, save: result => { state.project_keys[id] = { fingerprint, result: structuredClone(result) }; } };
  }
  function readyRevision(state, p, id) {
    if (p.revision_id !== id) throw fail('REVISION_CONFLICT', '다른 화면에서 설정 버전이 변경되었습니다. 다시 조회하세요.');
    return revisionFor(state, p, id);
  }
  function material(state, p, revisionId, environmentId) {
    const revision = revisionFor(state, p, revisionId), selected = new Map();
    for (const row of revision.variables.filter(row => row.scope === 'common')) selected.set(row.name, row);
    for (const row of revision.variables.filter(row => row.scope === 'environment' && row.environment_id === environmentId)) selected.set(row.name, row);
    return [...selected.values()].map(row => {
      const value = decrypt(revision, row);
      if (row.required && (value === undefined || value === '')) throw fail('REQUIRED_VARIABLE_MISSING', `필수 변수 ${row.name}의 값을 등록하세요.`);
      return value === undefined ? null : { name: row.name, kind: row.kind, required: row.required, value };
    }).filter(Boolean);
  }
  function supported(environmentId) { return Boolean(cipher && adapter?.supports?.(environmentId)); }
  function linkApplication(state, app, projectId = null, revisionId = null) {
    let p;
    if (projectId) {
      p = projectFor(state, projectId, app.session_id);
      if (revisionId) readyRevision(state, p, revisionId);
      if (app.project_id && app.project_id !== p.id) throw fail('PROJECT_BINDING_CONFLICT', '앱은 이미 다른 프로젝트에 연결되어 있습니다.');
    } else if (app.project_id) return;
    else { const id = randomUUID(); p = state.projects[id] = { id, session_id: app.session_id, name: app.app, active_binding_id: null, revision_id: null, created_at: now() }; }
    if (!app.binding_id) {
      const id = randomUUID();
      state.project_bindings[id] = { id, project_id: p.id, environment_id: app.environment_target_id, application_id: app.id, status: app.status, applied_revision_id: null };
      Object.assign(app, { project_id: p.id, binding_id: id });
      p.active_binding_id ||= id;
    }
  }
  async function runDelivery(record, phase = 'apply') {
    const state = store.read(), p = projectFor(state, record.project_id, record.session_id);
    const b = bindingFor(state, p, record.binding_id), app = state.applications[b.application_id];
    const result = await adapter.deliver({ operation_id: record.id, project_id: p.id, binding_id: b.id,
      revision_id: record.revision_id, environment_id: b.environment_id, application_id: app.id, app: app.app, phase,
      variables: material(state, p, record.revision_id, b.environment_id) });
    if (result.status !== 'succeeded' || result.observed_revision_id !== record.revision_id
        || result.checks?.synchronized !== true || phase !== 'prepare' && (result.checks?.workload_ready !== true || result.checks?.service_ready !== true))
      throw new ErrorType(409, 'CONFIGURATION_UNVERIFIED', '설정 버전과 앱 준비 상태를 확인하지 못했습니다.', { outcomeUnknown: true });
    return result;
  }
  async function deliveryWorker(record) {
    try {
      await update(record.id, { status: 'running', stage: 'delivering' });
      if (!executeDelivery) throw fail('CAPABILITY_UNAVAILABLE', '앱 소스 재배포 기능이 준비되지 않았습니다.');
      await executeDelivery(record);
      await store.transaction(state => {
        const p = projectFor(state, record.project_id, record.session_id); readyRevision(state, p, record.revision_id);
        state.project_bindings[record.binding_id].applied_revision_id = record.revision_id;
        Object.assign(state.operations[record.id], { status: 'succeeded', stage: 'complete', observed_revision_id: record.revision_id, updated_at: now() });
      });
    } catch (e) {
      const unknown = e.outcomeUnknown !== false;
      await update(record.id, { status: unknown ? 'unknown' : 'blocked', error: { code: /^[A-Z][A-Z0-9_]+$/.test(e.code || '') ? e.code : 'CONFIGURATION_OUTCOME_UNKNOWN', message: '설정 전달 결과를 확인해야 합니다.', request_id: randomUUID(), retryable: false, outcome_unknown: unknown } });
    }
  }
  return {
    linkApplication, material, runDelivery,
    validateDeployment(state, input, sessionId) {
      if (!input.project_id && !input.revision_id) return;
      if (!input.project_id || !input.revision_id || !input.environment_target_id) throw invalid();
      const p = projectFor(state, input.project_id, sessionId); readyRevision(state, p, input.revision_id);
      if (!input.environment_pending && !supported(input.environment_target_id)) throw fail('CAPABILITY_UNAVAILABLE', '대상 환경의 비밀값 전달이 준비되지 않았습니다.');
      material(state, p, input.revision_id, input.environment_target_id);
    },
    projects(sessionId) { return Object.values(store.read().projects).filter(p => p.session_id === sessionId).map(publicProject); },
    getProject(id, sessionId) { return publicProject(projectFor(store.read(), id, sessionId)); },
    async createProject(input, key, sessionId) {
      if (!exact(input, ['name']) || typeof input.name !== 'string' || !input.name.trim() || input.name.length > 100 || /[\x00-\x1f]/.test(input.name)) throw invalid();
      return store.transaction(state => {
        const d = dedup(state, sessionId, 'projects', key, input); if (d.old) return publicProject(state.projects[d.old.id]);
        if (Object.keys(state.projects).length >= 100) throw fail('CAPACITY_EXCEEDED', '프로젝트 보관 한도에 도달했습니다.');
        const id = randomUUID(), p = { id, session_id: sessionId, name: input.name.trim(), active_binding_id: null, revision_id: null, created_at: now() };
        state.projects[id] = p; d.save({ id }); return publicProject(p);
      });
    },
    variables(id, sessionId) {
      const state = store.read(), p = projectFor(state, id, sessionId), revision = p.revision_id && revisionFor(state, p, p.revision_id);
      const bindings = Object.values(state.project_bindings).filter(b => b.project_id === id).map(b => bindingFor(state, p, b.id));
      return { project_id: id, revision_id: p.revision_id, items: (revision?.variables || []).map(row => {
        const { encrypted, ...metadata } = row;
        return { ...metadata, has_value: Boolean(encrypted), ...(row.kind === 'plain' && encrypted ? { value: decrypt(revision, row) } : {}) };
      }), bindings, capabilities: { storage: Boolean(cipher), delivery: Boolean(executeDelivery && bindings.some(b => supported(b.environment_id))), transfer: Boolean(cipher && adapter && executeTransfer) },
      blockers: cipher ? [] : ['PROJECT_KEY_NOT_CONFIGURED'] };
    },
    async createRevision(id, input, key, sessionId) {
      if (!exact(input, ['base_revision_id', 'operations']) || input.base_revision_id !== null && typeof input.base_revision_id !== 'string'
          || !Array.isArray(input.operations) || input.operations.length < 1 || input.operations.length > 200) throw invalid();
      available();
      return store.transaction(state => {
        const p = projectFor(state, id, sessionId), d = dedup(state, sessionId, id + ':revisions', key, input);
        if (d.old) return d.old;
        busyProject(state, p);
        if (p.revision_id !== input.base_revision_id) throw fail('REVISION_CONFLICT', '다른 화면에서 설정이 변경되었습니다. 다시 조회하세요.');
        const previous = p.revision_id ? revisionFor(state, p, p.revision_id) : null;
        const entries = new Map((previous?.variables || []).map(row => [coordinate(row), { ...row, value: decrypt(previous, row) }]));
        const touched = new Set(), revisionId = randomUUID();
        for (const op of input.operations) {
          if (!exact(op, ['operation', 'name'], ['kind', 'scope', 'environment_id', 'required', 'value']) || !['set', 'delete'].includes(op.operation)
              || typeof op.name !== 'string' || !/^[A-Z_][A-Z0-9_]{0,127}$/.test(op.name) || RESERVED_VARIABLES.has(op.name)) throw invalid();
          const scope = op.scope || 'common', environment_id = op.environment_id || null;
          if (!['common', 'environment'].includes(scope) || scope === 'common' && environment_id !== null
              || scope === 'environment' && (typeof environment_id !== 'string' || !Object.hasOwn(environmentTargets(sessionId), environment_id))) throw invalid();
          const c = coordinate({ name: op.name, scope, environment_id });
          if (touched.has(c)) throw invalid(); touched.add(c);
          if (op.operation === 'delete') {
            if (['kind', 'required', 'value'].some(k => Object.hasOwn(op, k))) throw invalid();
            entries.delete(c); continue;
          }
          const old = entries.get(c), kind = op.kind || old?.kind || 'plain', required = op.required ?? old?.required ?? false;
          if (!['plain', 'secret'].includes(kind) || typeof required !== 'boolean' || Object.hasOwn(op, 'value') && (typeof op.value !== 'string' || Buffer.byteLength(op.value) > 16384 || op.value.includes('\0'))
              || old?.kind === 'secret' && kind !== 'secret' && !Object.hasOwn(op, 'value')) throw invalid();
          entries.set(c, { name: op.name, kind, scope, environment_id, required, origin: 'user', updated_at: now(), value: Object.hasOwn(op, 'value') ? op.value : old?.value });
        }
        if (entries.size > 200 || Object.keys(state.revisions).length >= 1000) throw fail('CAPACITY_EXCEEDED', '설정 보관 한도를 초과했습니다.');
        const revision = { id: revisionId, project_id: id, parent_revision_id: p.revision_id, schema_version: 1, created_at: now(), variables: [...entries.values()].map(({ value, encrypted, ...row }) => ({ ...row,
          ...(value === undefined ? {} : { encrypted: cipher.seal(value, context(id, revisionId, row)) }) })) };
        state.revisions[revisionId] = revision; p.revision_id = revisionId;
        const result = { id: revisionId, project_id: id, parent_revision_id: revision.parent_revision_id, created_at: revision.created_at };
        d.save(result); return result;
      });
    },
    async createDelivery(id, input, key, sessionId) {
      if (!exact(input, ['binding_id', 'revision_id'])) throw invalid();
      const accepted = await store.transaction(state => {
        const p = projectFor(state, id, sessionId), d = dedup(state, sessionId, id + ':deliveries', key, input);
        if (d.old) return { record: state.operations[d.old.id], replay: true };
        readyRevision(state, p, input.revision_id); const b = bindingFor(state, p, input.binding_id);
        if (b.status !== 'ready') throw fail('APPLICATION_STATE_CONFLICT', '준비된 앱에만 설정을 적용할 수 있습니다.');
        if (!executeDelivery || !supported(b.environment_id)) throw fail('CAPABILITY_UNAVAILABLE', '대상 환경의 비밀값 전달이 준비되지 않았습니다.');
        const source = Object.values(state.operations).filter(o => o.kind === 'deployments' && o.application_id === b.application_id && o.status === 'succeeded').at(-1);
        if (!source) throw fail('SOURCE_DEPLOYMENT_REQUIRED', '설정을 적용할 검증된 앱 소스가 없습니다.');
        material(state, p, input.revision_id, b.environment_id); checkFree(state, sessionId); checkCapacity(state);
        const operationId = randomUUID(), record = { id: operationId, kind: 'deliveries', project_id: id, session_id: sessionId, source_deployment_id: source.id, ...input, observed_revision_id: null, status: 'queued', stage: 'delivering', error: null, created_at: now() };
        state.operations[operationId] = record; d.save({ id: operationId }); return { record };
      });
      if (!accepted.replay) launch(() => deliveryWorker(accepted.record));
      return publicOperation(accepted.record);
    },
    getProjectOperation(projectId, kind, id, sessionId) {
      const state = store.read(); projectFor(state, projectId, sessionId); const record = state.operations[id];
      if (!record || record.project_id !== projectId || record.kind !== kind || record.session_id !== sessionId) throw notFound();
      return publicOperation(record);
    },
    async createTransfer(id, input, key, sessionId) {
      if (!exact(input, ['source_binding_id', 'destination_environment_id', 'revision_id'])) throw invalid();
      return store.transaction(state => {
        const p = projectFor(state, id, sessionId), d = dedup(state, sessionId, id + ':transfers', key, input);
        if (d.old) return publicOperation(state.operations[d.old.id]);
        const revision = readyRevision(state, p, input.revision_id), source = bindingFor(state, p, input.source_binding_id);
        if (source.id !== p.active_binding_id || source.environment_id === input.destination_environment_id) throw invalid();
        if (!Object.hasOwn(environmentTargets(sessionId), input.destination_environment_id)) throw notFound();
        const blockers = [], required_overrides = revision.variables.filter(v => v.scope === 'environment' && v.environment_id === source.environment_id
          && !revision.variables.some(x => x.name === v.name && x.scope === 'environment' && x.environment_id === input.destination_environment_id)).map(v => v.name);
        if (required_overrides.length) blockers.push('ENVIRONMENT_VARIABLE_REVIEW_REQUIRED');
        if (!supported(input.destination_environment_id)) blockers.push('DESTINATION_SECRETS_UNAVAILABLE');
        if (!executeTransfer) blockers.push('TRANSFER_EXECUTOR_UNAVAILABLE');
        const app = state.applications[source.application_id];
        const deployment = Object.values(state.operations).filter(o => o.kind === 'deployments' && o.application_id === app.id && o.status === 'succeeded').at(-1);
        if (!deployment) blockers.push('SOURCE_DEPLOYMENT_REQUIRED');
        const destination = describeEnvironment(input.destination_environment_id, app.app, sessionId);
        if (!destination) throw fail('DESTINATION_APPLICATION_MISMATCH', '대상 환경에 등록된 앱 이름과 일치해야 합니다.');
        if (state.applications[destination.id]) blockers.push('DESTINATION_APPLICATION_EXISTS');
        checkCapacity(state); busyProject(state, p);
        const operationId = randomUUID(), record = { id: operationId, kind: 'transfers', project_id: id, session_id: sessionId, ...input,
          source_deployment_id: deployment?.id || null, destination_binding_id: null, destination_application_id: destination.id,
          status: 'planned', stage: 'planned', expires_at: new Date(Date.now() + 600000).toISOString(), blockers, required_overrides,
          traffic_switch: 'not_requested', error: null, created_at: now() };
        record.plan_hash = hash({ id: operationId, project_id: id, ...input, source_deployment_id: record.source_deployment_id, destination_application_id: destination.id, expires_at: record.expires_at });
        state.operations[operationId] = record; d.save({ id: operationId }); return publicOperation(record);
      });
    },
    async observeProjectOperation(projectId, kind, id, input, key, sessionId) {
      if (!exact(input, ['action']) || input.action !== 'observe') throw invalid();
      const accepted = await store.transaction(state => {
        projectFor(state, projectId, sessionId); const record = state.operations[id];
        if (!record || record.kind !== kind || record.project_id !== projectId || record.session_id !== sessionId) throw notFound();
        const d = dedup(state, sessionId, id + ':observe', key, input);
        if (d.old || record.status === 'succeeded') return { record, replay: true };
        if (record.status !== 'unknown') throw fail('CONFLICT', '결과 불명 상태만 관측으로 확인할 수 있습니다.');
        d.save({ id }); return { record };
      });
      if (!accepted.replay) {
        await reconcile(accepted.record);
        return publicOperation(store.read().operations[id]);
      }
      return publicOperation(accepted.record);
    },
    async executeProjectTransfer(projectId, id, input, key, sessionId) {
      if (!exact(input, ['action', 'plan_hash']) || input.action !== 'execute') throw invalid();
      const accepted = await store.transaction(state => {
        const p = projectFor(state, projectId, sessionId), record = state.operations[id];
        if (!record || record.kind !== 'transfers' || record.project_id !== p.id || record.session_id !== sessionId) throw notFound();
        const d = dedup(state, sessionId, id + ':execute', key, input); if (d.old) return { record, replay: true };
        if (record.plan_hash !== input.plan_hash || record.status !== 'planned' || Date.parse(record.expires_at) <= Date.now()
            || record.source_binding_id !== p.active_binding_id) throw fail('PLAN_STALE', '이전 계획이 변경되었거나 만료되었습니다. 다시 검토하세요.');
        readyRevision(state, p, record.revision_id);
        if (record.blockers.length) throw fail('CAPABILITY_UNAVAILABLE', '이전 계획의 차단 사유를 먼저 해결하세요.');
        checkFree(state, sessionId); Object.assign(record, { status: 'queued', stage: 'preparing', updated_at: now() }); d.save({ id }); return { record };
      });
      if (!accepted.replay) launch(async () => {
        try { await executeTransfer(accepted.record); }
        catch (e) { await update(id, { status: e.outcomeUnknown === false ? 'blocked' : 'unknown', error: { code: 'TRANSFER_UNVERIFIED', message: '새 환경 준비 결과를 확인해야 합니다. 기존 활성 연결은 유지됩니다.', request_id: randomUUID(), retryable: false, outcome_unknown: e.outcomeUnknown !== false } }); }
      });
      return publicOperation(accepted.record);
    },
  };
}
