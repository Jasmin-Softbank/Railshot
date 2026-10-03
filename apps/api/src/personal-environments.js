import { createHash, randomBytes, randomUUID } from 'node:crypto';
import { DashboardError } from './sessions.js';

const hash = (value) => createHash('sha256').update(typeof value === 'string' ? value : JSON.stringify(value)).digest('hex');
const now = () => new Date().toISOString();
const fail = (message, status = 422, code = 'INVALID_INPUT') => { throw new DashboardError(message, status, code); };
const absent = () => fail('개인 환경을 찾을 수 없습니다.', 404, 'NOT_FOUND');
const exact = (value, required, optional = []) => value && typeof value === 'object' && !Array.isArray(value)
  && required.every((key) => Object.hasOwn(value, key)) && Object.keys(value).every((key) => [...required, ...optional].includes(key));
const tokenHash = (authorization) => /^Bearer [A-Za-z0-9_-]{43}$/.test(authorization || '') ? hash(authorization.slice(7)) : null;
const shell = (value) => `'${String(value).replaceAll("'", "'\\''")}'`;
const metricNames = ['cpu_percent', 'memory_percent', 'disk_percent', 'network_receive_bytes_per_second', 'network_transmit_bytes_per_second'];
const held = (target) => ['deleting', 'attention', 'deleted'].includes(target.status);
const resources = (apps) => apps.map((app) => ({ kind: 'RailShotApplicationAndPrivateData', name: app.app, application_id: app.id }));
const retained = [{ kind: 'CustomerServices', name: 'RailShot 외부 서비스' }, { kind: 'Infrastructure', name: '기반 VM·공유 네트워크·클러스터·공유 스토리지' }, { kind: 'AuditRecord', name: '작업 및 삭제 기록' }];

export function createPersonalEnvironments({ store, adapter, applicationAdapter, launch, publicApplication, maxTargets = 20 }) {
  function owned(state, id, owner) {
    const target = state.personal.targets[id];
    if (!owner || !target || target.session_id !== owner) absent();
    return target;
  }
  function apps(state, target) { return Object.values(state.applications).filter((app) => app.environment_target_id === target.id && app.status !== 'deleted'); }
  function publicTarget(target, state = store.read()) {
    const status = target.status === 'ready' && Date.now() - Date.parse(target.last_seen_at) > 90000 ? 'offline' : target.status;
    return { id: target.id, label: target.label, provider: 'openstack', scope: 'owned', environment: 'onprem', status,
      deployable: status === 'ready' && target.runtime_prepared === true, generation: target.generation,
      last_seen_at: target.last_seen_at || null, client_version: target.client_version || null, project_id: target.project_id || null,
      application_count: apps(state, target).length, registration_stage: target.registration_stage,
      checks: target.checks || null, blockers: target.blockers || [], created_at: target.created_at,
      deletion_operation_id: target.deletion_operation_id || null,
      capabilities: { openstack_control: status === 'ready', ci_submission: status === 'ready' && target.runtime_prepared === true, application_deployment: status === 'ready' && target.runtime_prepared === true },
    };
  }
  function authenticated(state, id, authorization, generation) {
    const target = state.personal.targets[id];
    if (!target || !target.client_hash || target.client_hash !== tokenHash(authorization) || target.status === 'deleted'
        || generation !== undefined && generation !== target.generation) fail('클라이언트 인증에 실패했습니다.', 401, 'UNAUTHENTICATED');
    return target;
  }
  function writable(state, id, owner, requireReady = true) {
    const target = state.personal.targets[id];
    if (!target) return;
    owned(state, id, owner);
    if (held(target) || requireReady && !publicTarget(target, state).deployable) fail('현재 환경은 배포·앱 변경을 받을 수 없습니다.', 409, 'TARGET_UNAVAILABLE');
  }
  function snapshot(state, target) {
    return hash({ generation: target.generation, applications: apps(state, target), status: target.status,
      pending: Object.values(state.operations).filter((op) => ['queued', 'running', 'unknown'].includes(op.status) && (op.environment_target_id === target.id || op.target_id === target.id)) });
  }
  async function runDeletion(record, plan) {
    try {
      await store.transaction((state) => { Object.assign(state.operations[record.id], { status: 'running', stage: 'services', updated_at: now() }); });
      for (const entry of plan.children) {
        const app = store.read().applications[entry.application_id];
        await applicationAdapter.verifyLifecyclePlan(app, entry.plan);
        const result = await applicationAdapter.applyLifecycle(app, entry.plan, { id: entry.operation_id, deleteData: true });
        if (result.status !== 'succeeded' || (result.residuals || []).length) throw new Error('Application removal unverified');
        await store.transaction((state) => {
          state.applications[app.id].status = 'deleted';
          state.operations[record.id].steps.push({ name: `application:${app.id}`, status: 'succeeded' });
          state.operations[record.id].updated_at = now();
        });
      }
      const target = store.read().personal.targets[record.target_id];
      if (!target.client_hash) {
        // An unclaimed registration has no installed client and no gateway peer.
        if (target.public_key) throw new Error('Enrollment outcome requires reconciliation');
        await store.transaction((state) => {
          state.personal.targets[target.id].status = 'deleted';
          Object.assign(state.operations[record.id], { status: 'succeeded', stage: 'complete', residuals: [], updated_at: now() });
        });
      } else {
        await store.transaction((state) => {
          Object.assign(state.operations[record.id], { stage: 'client', residuals: [{ kind: 'RailShotClient', name: target.id }], updated_at: now() });
          state.personal.targets[target.id].command = { id: record.id, operation_id: record.id, kind: 'environment.delete', applications: [], delete_data: true, generation: target.generation };
        });
      }
    } catch {
      await store.transaction((state) => {
        state.personal.targets[record.target_id].status = 'attention';
        Object.assign(state.operations[record.id], { status: 'unknown', stage: 'reconciliation', updated_at: now(),
          error: { code: 'REMOVAL_UNVERIFIED', message: '남은 서비스·클라이언트를 확인해야 합니다. 자동 재실행하지 않습니다.', retryable: false, outcome_unknown: true } });
      });
    }
  }
  return {
    writable,
    async restore() {
      for (const target of Object.values(store.read().personal.targets)) {
        if (!target.client_hash || ['deleted', 'attention', 'deleting'].includes(target.status) || !adapter) continue;
        try {
          await adapter.register(target);
          const prepared = await adapter.prepare(target);
          await store.transaction((state) => { state.personal.targets[target.id].runtime_prepared = prepared; });
        } catch { await store.transaction((state) => { Object.assign(state.personal.targets[target.id], { status: 'attention', blockers: ['RUNTIME_RECONCILIATION_REQUIRED'] }); }); }
      }
    },
    list(owner) { const state = store.read(); return Object.values(state.personal.targets).filter((t) => owner && t.session_id === owner).map((t) => publicTarget(t, state)); },
    get(id, owner) { const state = store.read(); return publicTarget(owned(state, id, owner), state); },
    async execute(id, input, owner) {
      const target = owned(store.read(), id, owner);
      if (!input || !/^[A-Za-z0-9_-]{1,64}$/.test(input.job_id || '')) fail('OpenStack 실행에는 지속되는 job_id가 필요합니다.');
      if (publicTarget(target).status !== 'ready' || !adapter?.execute) fail('OpenStack 제어 연결이 준비되지 않았습니다.', 409, 'TARGET_UNAVAILABLE');
      return adapter.execute(target, input);
    },
    async instances(id, owner) {
      const target = owned(store.read(), id, owner);
      if (publicTarget(target).status !== 'ready' || !adapter?.execute) fail('OpenStack 제어 연결이 준비되지 않았습니다.', 409, 'TARGET_UNAVAILABLE');
      const result = await adapter.execute(target, { argv: ['server', 'list'] });
      if (!result.ok || !Array.isArray(result.result)) fail('OpenStack 조회를 확인하지 못했습니다.', 502, 'UPSTREAM_FAILURE');
      return result.result;
    },
    applications(id, owner) { const state = store.read(), target = owned(state, id, owner); return apps(state, target).filter((app) => app.session_id === owner).map((app) => publicApplication(state, app)); },
    observation(id, owner, observation) {
      const state = store.read(), target = owned(state, id, owner);
      const stale = !target.metrics_seen_at || Date.now() - Date.parse(target.metrics_seen_at) > 90000;
      for (const name of metricNames) observation.metrics[name] = {
        state: target.metrics?.[name] == null ? 'not_configured' : stale ? 'stale' : 'ready',
        value: target.metrics?.[name] ?? null, observed_at: target.metrics?.[name] == null ? null : target.metrics_seen_at,
        scope: 'client_host',
      };
      return { ...observation, measured_scope: 'client_host', measured_target: id };
    },
    async create(input, owner) {
      if (!owner) fail('복구키를 발급받아 개인 환경 소유권을 먼저 설정하세요.', 409, 'OWNER_REQUIRED');
      if (!exact(input, ['label'], ['provider']) || input.provider !== undefined && input.provider !== 'openstack'
          || typeof input.label !== 'string' || !input.label.trim() || input.label.length > 80 || /[\x00-\x1f]/.test(input.label)) fail('환경 이름을 확인하세요.');
      return store.transaction((state) => {
        if (Object.values(state.personal.targets).filter((t) => t.session_id === owner && t.status !== 'deleted').length >= maxTargets) fail('개인 환경 등록 한도를 초과했습니다.', 409, 'CAPACITY_EXCEEDED');
        const id = 'personal-' + randomUUID();
        const target = { id, session_id: owner, label: input.label.trim(), status: 'pending', generation: 0,
          registration_stage: 'enrollment', blockers: adapter ? [] : ['PERSONAL_GATEWAY_NOT_CONFIGURED'], created_at: now() };
        state.personal.targets[id] = target;
        return publicTarget(target, state);
      });
    },
    async enrollment(id, input, owner) {
      if (!exact(input, [])) fail('등록 자격 요청에는 추가 필드를 넣을 수 없습니다.');
      return store.transaction((state) => {
        const target = owned(state, id, owner);
        if (target.client_hash || held(target) || target.public_key) fail('이미 등록했거나 확인이 필요한 환경입니다.', 409, 'TARGET_UNAVAILABLE');
        if (!adapter) fail('개인 환경 설치 서버가 구성되지 않았습니다.', 409, 'CAPABILITY_UNAVAILABLE');
        for (const item of Object.values(state.personal.enrollments)) if (item.target_id === id) item.revoked = true;
        if (Object.keys(state.personal.enrollments).length >= 10000) fail('등록 자격 보관 한도를 초과했습니다.', 409, 'CAPACITY_EXCEEDED');
        const enrollmentId = randomUUID(), token = randomBytes(32).toString('base64url'), expires_at = new Date(Date.now() + 900000).toISOString();
        target.generation += 1;
        state.personal.enrollments[enrollmentId] = { id: enrollmentId, target_id: id, token_hash: hash(token), generation: target.generation, expires_at, used: false };
        const config = adapter.config;
        const command = `install_file=$(mktemp) && curl --disable --proto '=https' --max-redirs 0 --fail --silent --show-error ${shell(config.installer_url)} -o "$install_file" && RAILSHOT_ENROLLMENT_TOKEN=${shell(token)} sudo --preserve-env=RAILSHOT_ENROLLMENT_TOKEN bash "$install_file" --api-url ${shell(config.public_url)} --enrollment-id ${shell(enrollmentId)} --artifact-url ${shell(config.artifact_url)} --artifact-sha256 ${shell(config.artifact_sha256)}; install_status=$?; rm -f "$install_file"; (exit "$install_status")`;
        return { id: enrollmentId, target_id: id, expires_at, install_command: command };
      });
    },
    async claim(id, input, authorization) {
      if (!exact(input, ['public_key', 'client_version', 'project_id', 'runtime'], ['capabilities']) || !/^[A-Za-z0-9+/]{43}=$/.test(input.public_key || '')
          || typeof input.client_version !== 'string' || !/^[A-Za-z0-9._-]{1,64}$/.test(input.client_version)
          || typeof input.project_id !== 'string' || !/^[A-Za-z0-9._-]{1,128}$/.test(input.project_id)
          || input.runtime !== undefined && (!exact(input.runtime, ['ssh_host_key'], ['profile_id', 'resource_id'])
            || input.runtime.ssh_host_key !== undefined && !/^ssh-ed25519 [A-Za-z0-9+/]{68}={0,2}$/.test(input.runtime.ssh_host_key)
            || input.runtime.resource_id !== undefined && !/^[A-Za-z0-9._-]{1,128}$/.test(input.runtime.resource_id))) fail('클라이언트 등록 입력을 확인하세요.');
      const target = await store.transaction((state) => {
        const enrollment = state.personal.enrollments[id];
        if (!enrollment || enrollment.token_hash !== tokenHash(authorization) || enrollment.used || enrollment.revoked || Date.parse(enrollment.expires_at) <= Date.now()) fail('등록 자격이 만료되었거나 유효하지 않습니다.', 401, 'UNAUTHENTICATED');
        const target = state.personal.targets[enrollment.target_id];
        if (!target || held(target) || target.generation !== enrollment.generation || target.client_hash) fail('환경 등록 상태가 변경되었습니다.', 409, 'TARGET_UNAVAILABLE');
        enrollment.used = true;
        Object.assign(target, { status: 'installing', public_key: input.public_key, project_id: input.project_id, client_version: input.client_version, registration_stage: 'gateway', runtime: input.runtime || null });
        return structuredClone(target);
      });
      try {
        const registered = await adapter.register(target);
        if (registered.status !== 'succeeded' || !registered.tunnel) throw new Error('Gateway unverified');
        target.tunnel = registered.tunnel;
        const runtimePrepared = await adapter.prepare(target), token = randomBytes(32).toString('base64url');
        await store.transaction((state) => {
          Object.assign(state.personal.targets[target.id], { client_hash: hash(token), tunnel: registered.tunnel, status: 'connecting', runtime_prepared: runtimePrepared,
            registration_stage: 'openstack', blockers: ['OPENSTACK_CONTROL_PENDING'] });
        });
        return { target_id: target.id, generation: target.generation, client_token: token, tunnel: registered.tunnel,
          heartbeat_url: `/api/v1/targets/${target.id}/heartbeats`, ...(adapter.runtimeAccess?.(target) ? { runtime_access: adapter.runtimeAccess(target) } : {}) };
      } catch {
        await store.transaction((state) => { Object.assign(state.personal.targets[target.id], { status: 'attention', blockers: ['ENROLLMENT_RECONCILIATION_REQUIRED'] }); });
        fail('클라이언트 등록 결과를 확인해야 합니다. 등록 자격은 재사용할 수 없습니다.', 503, 'ENROLLMENT_UNVERIFIED');
      }
    },
    async heartbeat(id, input, authorization) {
      if (!exact(input, ['generation', 'client_version', 'checks'], ['capabilities', 'project_id', 'metrics'])
          || !Number.isSafeInteger(input.generation) || !/^[A-Za-z0-9._-]{1,64}$/.test(input.client_version || '')
          || !exact(input.checks, ['tunnel', 'openstack'], ['runtime']) || Object.values(input.checks).some((v) => typeof v !== 'boolean')
          || input.capabilities !== undefined && (!Array.isArray(input.capabilities) || input.capabilities.length > 20 || input.capabilities.some((v) => typeof v !== 'string' || !/^[a-z.]{1,64}$/.test(v)))) fail('상태 보고 입력을 확인하세요.');
      if (input.metrics !== undefined && (!exact(input.metrics, [], metricNames) || Object.entries(input.metrics).some(([name, value]) => value !== null && (!Number.isFinite(value) || value < 0 || name.endsWith('_percent') && value > 100)))) fail('메트릭 값 범위를 확인하세요.');
      const current = authenticated(store.read(), id, authorization, input.generation);
      let verified = false;
      if (!held(current) && input.checks.tunnel && adapter) {
        try { const result = await adapter.verify(current); verified = result.status === 'succeeded' && result.reachable === true && result.openstack_verified === true; } catch { /* never infer connectivity from the client alone */ }
      }
      return store.transaction((state) => {
        const target = authenticated(state, id, authorization, input.generation);
        if (input.project_id !== undefined && input.project_id !== target.project_id) fail('프로젝트가 등록과 다릅니다.', 409, 'PROJECT_MISMATCH');
        Object.assign(target, { last_seen_at: now(), client_version: input.client_version, checks: input.checks, ...(input.metrics !== undefined ? { metrics: input.metrics, metrics_seen_at: now() } : {}) });
        if (!held(target)) {
          if (adapter?.deploymentReady) target.runtime_prepared = adapter.deploymentReady(target);
          target.status = verified && input.checks.openstack ? 'ready' : 'connecting';
          target.registration_stage = target.status === 'ready' ? 'complete' : 'checks';
          target.blockers = target.status === 'ready' ? [] : ['OPENSTACK_CONTROL_PENDING'];
        }
        const operation = target.command && state.operations[target.command.id];
        return { target_id: id, status: publicTarget(target, state).status,
          command: operation?.status === 'running' ? target.command : null };
      });
    },
    async plan(id, input, owner) {
      if (!exact(input, ['action', 'delete_data']) || input.action !== 'delete' || input.delete_data !== true) fail('서비스와 앱 전용 데이터 삭제에 명시적으로 동의해야 합니다.');
      return store.transaction(async (state) => {
        const target = owned(state, id, owner), applications = apps(state, target);
        if (['deleting', 'deleted'].includes(target.status)) fail('삭제가 접수되었거나 끝난 환경입니다.', 409, 'TARGET_UNAVAILABLE');
        const blockers = [], children = [];
        if (target.status === 'attention') blockers.push('RECONCILIATION_REQUIRED');
        if (applications.some((app) => app.session_id !== owner)) blockers.push('FOREIGN_APPLICATIONS');
        if (Object.values(state.operations).some((op) => ['queued', 'running', 'unknown'].includes(op.status))) blockers.push('EXECUTION_IN_PROGRESS');
        if (target.client_hash && publicTarget(target, state).status !== 'ready') blockers.push('CLIENT_OFFLINE');
        if (!blockers.length) for (const app of applications) {
          if (!['ready', 'stopped'].includes(app.status) || !applicationAdapter?.planLifecycle) { blockers.push('APPLICATION_RECONCILIATION_REQUIRED'); break; }
          const child = await applicationAdapter.planLifecycle(app, { id: randomUUID(), action: 'delete' });
          children.push({ application_id: app.id, operation_id: randomUUID(), plan: child });
        }
        if (Object.keys(state.plans).length >= 1000) fail('삭제 계획 보관 한도를 초과했습니다.', 409, 'CAPACITY_EXCEEDED');
        const planId = randomUUID(), expires_at = new Date(Date.now() + 600000).toISOString(), targetSnapshot = snapshot(state, target);
        const plan = { id: planId, target_id: id, action: 'delete', delete_data: true, expires_at,
          resources: [...resources(applications), { kind: 'RailShotClient', name: target.label }, { kind: 'Registration', name: id }], retained, blockers,
          executable: blockers.length === 0, plan_hash: hash({ targetSnapshot, children, expires_at }) };
        state.plans[planId] = { kind: 'target-lifecycle', session_id: owner, public: plan, children, target_snapshot: targetSnapshot };
        return structuredClone(plan);
      });
    },
    async remove(id, input, key, owner) {
      if (!exact(input, ['action', 'plan_id', 'plan_hash', 'confirmation', 'delete_data']) || input.action !== 'delete' || input.delete_data !== true) fail('삭제 계획과 명시적인 데이터 삭제 확인이 필요합니다.');
      const accepted = await store.transaction(async (state) => {
        const target = owned(state, id, owner), fingerprint = hash({ target_id: id, ...input }), scopedKey = `${owner}:target-lifecycle:${key}`;
        const prior = state.keys[scopedKey] && state.operations[state.keys[scopedKey]];
        if (prior) { if (prior.fingerprint !== fingerprint) fail('같은 키로 다른 삭제를 요청할 수 없습니다.', 409, 'IDEMPOTENCY_CONFLICT'); return { record: prior, replay: true }; }
        const plan = state.plans[input.plan_id];
        if (!plan || plan.kind !== 'target-lifecycle' || plan.session_id !== owner || plan.public.target_id !== id) absent();
        if (plan.operation_id || input.confirmation !== target.label || input.plan_hash !== plan.public.plan_hash || Date.parse(plan.public.expires_at) <= Date.now()
            || plan.target_snapshot !== snapshot(state, target) || plan.public.blockers.length) fail('환경 삭제 계획을 다시 확인하세요.', 409, 'TARGET_PLAN_STALE');
        if (Object.values(state.operations).some((op) => ['queued', 'running', 'unknown'].includes(op.status))) fail('진행 중인 실행을 먼저 확인하세요.', 409, 'EXECUTOR_BUSY');
        for (const child of plan.children) await applicationAdapter.verifyLifecyclePlan(state.applications[child.application_id], child.plan);
        const operationId = randomUUID();
        const record = { id: operationId, kind: 'target-lifecycle', session_id: owner, target_id: id, action: 'delete', delete_data: true,
          plan_id: input.plan_id, status: 'queued', stage: 'services', steps: [], residuals: plan.public.resources, retained,
          fingerprint, created_at: now(), updated_at: now() };
        state.operations[operationId] = record; state.keys[scopedKey] = operationId; plan.operation_id = operationId;
        Object.assign(target, { status: 'deleting', deletion_operation_id: operationId });
        for (const enrollment of Object.values(state.personal.enrollments)) if (enrollment.target_id === id) enrollment.revoked = true;
        return { record: structuredClone(record), plan };
      });
      if (!accepted.replay) launch(() => runDeletion(accepted.record, accepted.plan));
      const { session_id, fingerprint, ...visible } = accepted.record; return visible;
    },
    async receipt(id, input, authorization) {
      if (!exact(input, ['operation_id', 'generation', 'status', 'steps', 'residuals', 'client_removed']) || !['running', 'succeeded', 'failed', 'unknown'].includes(input.status)
          || !Array.isArray(input.steps) || input.steps.length > 30 || !Array.isArray(input.residuals) || input.residuals.length > 100
          || typeof input.client_removed !== 'boolean') fail('제거 확인 형식을 확인하세요.');
      const target = authenticated(store.read(), id, authorization, input.generation), operation = store.read().operations[input.operation_id];
      if (!operation || operation.target_id !== id || operation.kind !== 'target-lifecycle' || target.deletion_operation_id !== input.operation_id || operation.stage !== 'client') absent();
      if (input.status === 'running') {
        await store.transaction((state) => { state.operations[input.operation_id].updated_at = now(); });
        return { target_id: id, operation_id: input.operation_id, status: 'running' };
      }
      const succeeded = input.status === 'succeeded' && input.client_removed && input.residuals.length === 0;
      let removed = false;
      if (succeeded) { try { const result = await adapter.remove(target); removed = result.status === 'succeeded'; } catch { /* persisted as unverified */ } }
      return store.transaction((state) => {
        const target = authenticated(state, id, authorization, input.generation), operation = state.operations[input.operation_id];
        if (apps(state, target).length) fail('서비스 제거가 확인되지 않았습니다.', 409, 'REMOVAL_UNVERIFIED');
        operation.steps.push({ name: 'client', status: succeeded ? 'succeeded' : 'unknown' }, { name: 'gateway', status: removed ? 'succeeded' : 'unknown' });
        Object.assign(operation, { status: succeeded && removed ? 'succeeded' : 'unknown', stage: succeeded && removed ? 'complete' : 'reconciliation', updated_at: now(),
          residuals: succeeded && removed ? [] : [{ kind: 'RemovalVerification', name: id }] });
        target.status = succeeded && removed ? 'deleted' : 'attention';
        if (target.status === 'deleted') { delete target.client_hash; delete target.command; }
        return { target_id: id, operation_id: operation.id, status: operation.status };
      });
    },
  };
}
