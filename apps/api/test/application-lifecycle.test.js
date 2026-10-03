import test from 'node:test';
import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';
import { mkdtemp, rm, readFile, stat, writeFile, realpath } from 'node:fs/promises';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { setTimeout as pause } from 'node:timers/promises';
import { DatabaseSync } from 'node:sqlite';
import { createProductService } from '../src/product.js';
import { createProductStore } from '../src/product-store.js';
import { createApplicationAdapter } from '../src/applications.js';
import { createAppServer } from '../src/server.js';
import { createDeploymentService } from '../src/github.js';
import { lifecycleResources } from '../src/application-lifecycle.js';

const app = { id: 'app-' + 'a'.repeat(24), app: 'calculator', target_id: 'app-' + 'a'.repeat(24), environment_target_id: 'runtime-aws', provider: 'aws', status: 'ready' };
const resources = [{ kind: 'Deployment', name: 'calculator', namespace: 'tenant-app' }];
const retained = [{ kind: 'PersistentVolumeClaim', name: 'calculator-data', namespace: 'tenant-app' }];
const source = { app: app.app, target_id: app.environment_target_id, source_type: 'folder', files: [{ path: 'app.js', content: Buffer.from('user app') }] };
function disk(directory, table, id) {
  const db = new DatabaseSync(join(directory, 'dashboard.sqlite3'), { readOnly: true });
  try { return JSON.parse(db.prepare(`SELECT record FROM ${table} WHERE id = ?`).get(id).record); } finally { db.close(); }
}
async function settled(product, id, owner) {
  for (let count = 0; count < 200; count++) {
    const value = product.getOperation(id, owner);
    if (!['queued', 'running'].includes(value.status)) return value;
    await pause(5);
  }
  assert.fail('Lifecycle operation did not settle');
}
async function settledDeployment(product, id, owner) {
  for (let count = 0; count < 200; count++) {
    const value = await product.getDeployment(id, owner);
    if (!['queued', 'running'].includes(value.status)) return value;
    await pause(5);
  }
  assert.fail('Deployment did not settle');
}
async function fixture(t, overrides = {}) {
  const directory = await realpath(await mkdtemp(join(tmpdir(), 'railshot-lifecycle-')));
  const store = await createProductStore(directory), owner = store.dashboard.session(), stranger = store.dashboard.session();
  await store.transaction((state) => { state.applications[app.id] = { ...app, session_id: owner.id, ...overrides.application }; });
  await store.close();
  const calls = { plan: 0, apply: 0, dispatch: 0 };
  const adapter = {
    targets: { 'runtime-aws': { provider: 'aws', automaticDelivery: true } }, describe: () => ({ ...app }),
    planPendingDeletion: async (application, { id, deploymentId }) => ({ public: { id, application_id: application.id, action: 'delete',
      plan_hash: 'a'.repeat(64), resources: [{ kind: 'ApplicationNamespace', name: application.id }, { kind: 'ApplicationRoutes', name: application.id }],
      retained: [], expires_at: new Date(Date.now() + 600000).toISOString() }, private: { deferred: true, deployment_id: deploymentId } }),
    planLifecycle: async (application, { id, action }) => {
      calls.plan++;
      return { public: { id, application_id: application.id, action, plan_hash: 'b'.repeat(64),
        resources, retained: action === 'delete' ? [] : retained, expires_at: new Date(Date.now() + 600000).toISOString() },
      private: { path: '/private/not-public', credential: 'never-public' } };
    }, verifyLifecyclePlan: async () => {},
    applyLifecycle: async (application, plan, { id }) => {
      calls.apply++;
      assert.equal(disk(directory, 'operations', id).status, 'running');
      assert.equal(disk(directory, 'plans', plan.public.id).operation_id, id);
      return { status: 'succeeded', application_id: application.id, action: plan.public.action,
        steps: [{ name: plan.public.action, status: 'succeeded' }], residuals: [] };
    }, ...overrides.adapter,
  };
  const options = { directory, service: { targetId: 'runtime-aws', deploy: async () => { calls.dispatch++; assert.fail('CI must not run'); } },
    applicationAdapter: adapter, ...overrides.options };
  const product = await createProductService(options);
  t.after(async () => { await product.close(); await rm(directory, { recursive: true, force: true }); });
  const plan = (action = 'stop') => product.createApplicationPlan(app.id, { action }, owner.id);
  const input = (value) => ({ action: value.action, plan_id: value.id, plan_hash: value.plan_hash, confirmation: app.app,
    ...(value.action === 'delete' ? { delete_data: true } : {}) });
  return { directory, product, options, owner, stranger, calls, adapter, plan, input };
}

test('stop/start/delete consume private plans once, preserve data on stop, and retain a deleted tombstone', async (t) => {
  const f = await fixture(t);
  for (const [action, status] of [['stop', 'stopped'], ['start', 'ready'], ['delete', 'deleted']]) {
    const plan = await f.plan(action);
    assert.equal(plan.private, undefined); assert.ok(!JSON.stringify(plan).includes('never-public'));
    assert.deepEqual(plan.retained, action === 'delete' ? [] : retained);
    assert.equal(disk(f.directory, 'plans', plan.id).private.credential, 'never-public');
    assert.throws(() => f.product.getPlan(plan.id, f.owner.id), { status: 404 });
    assert.deepEqual(f.product.list('plans', f.owner.id), []);
    const body = f.input(plan);
    const [first, repeat] = await Promise.all([f.product.createApplicationOperation(app.id, body, action, f.owner.id),
      f.product.createApplicationOperation(app.id, { ...body }, action, f.owner.id)]);
    assert.equal(first.id, repeat.id);
    const result = await settled(f.product, first.id, f.owner.id);
    assert.equal(result.status, 'succeeded'); assert.equal(result.stage, 'complete');
    assert.equal(f.product.getApplication(app.id, f.owner.id).status, status);
    assert.equal((await f.product.createApplicationOperation(app.id, body, action, f.owner.id)).id, first.id);
    await assert.rejects(f.product.createApplicationOperation(app.id, { ...body, confirmation: 'another' }, action, f.owner.id), { code: 'IDEMPOTENCY_CONFLICT' });
    if (status !== 'ready') await assert.rejects(f.product.createDeployment(source, 'deploy-' + action, undefined, f.owner.id), { code: 'APPLICATION_STATE_CONFLICT' });
  }
  assert.equal(f.calls.apply, 3); assert.equal(f.calls.dispatch, 0);
  await assert.rejects(f.plan('start'), { code: 'APPLICATION_STATE_CONFLICT' });
});

test('ownership, explicit data consent, strict inputs and stale snapshots fail before execution', async (t) => {
  const f = await fixture(t), plan = await f.plan('delete'), body = f.input(plan);
  await assert.rejects(f.product.createApplicationPlan(app.id, { action: 'delete' }, f.stranger.id), { status: 404 });
  await assert.rejects(f.product.createApplicationOperation(app.id, body, 'foreign', f.stranger.id), { status: 404 });
  for (const change of [{ delete_data: false }, { delete_data: undefined }, { config: '/private/file' }, { confirmation: 'wrong' }, { action: 'restart' }]) {
    await assert.rejects(f.product.createApplicationOperation(app.id, { ...body, ...change }, randomUUID(), f.owner.id), { status: 422 });
  }
  await assert.rejects(f.product.createApplicationOperation(app.id, { ...body, plan_hash: 'c'.repeat(64) }, 'changed-hash', f.owner.id), { code: 'APPLICATION_PLAN_STALE' });
  const stop = await f.plan('stop');
  const operation = await f.product.createApplicationOperation(app.id, f.input(stop), 'stop', f.owner.id);
  await settled(f.product, operation.id, f.owner.id);
  await assert.rejects(f.product.createApplicationOperation(app.id, body, 'old-delete', f.owner.id), { code: 'APPLICATION_PLAN_STALE' });
  assert.throws(() => f.product.getOperation(operation.id, f.stranger.id), { status: 404 });
  assert.equal(f.calls.apply, 1);
});

test('lifecycle shares the executor limit and an uncertain response never replays after restart', async (t) => {
  let finish, entered; const waiting = new Promise((resolve) => { finish = resolve; });
  const begun = new Promise((resolve) => { entered = resolve; });
  const f = await fixture(t, { adapter: { applyLifecycle: async () => { entered(); await waiting; throw new Error('/private/secret'); } } });
  const plan = await f.plan(), body = f.input(plan);
  const first = await f.product.createApplicationOperation(app.id, body, 'unknown', f.owner.id); await begun;
  try {
    await assert.rejects(f.plan('delete'), { code: 'EXECUTOR_BUSY' });
    await assert.rejects(f.product.createDeployment(source, 'deploy', undefined, f.owner.id), { status: 409 });
  } finally { finish(); }
  const result = await settled(f.product, first.id, f.owner.id);
  assert.equal(result.status, 'unknown'); assert.deepEqual(result.residuals, resources);
  assert.equal(f.product.getApplication(app.id, f.owner.id).status, 'unknown');
  assert.ok(!JSON.stringify(result).includes('/private/secret'));
  await f.product.close();
  const restarted = await createProductService({ ...f.options, applicationAdapter: { ...f.adapter, applyLifecycle: async () => assert.fail('No retry') } });
  try {
    assert.equal((await restarted.createApplicationOperation(app.id, body, 'unknown', f.owner.id)).id, first.id);
    await assert.rejects(restarted.createApplicationPlan(app.id, { action: 'delete' }, f.owner.id), { code: 'EXECUTOR_BUSY' });
  } finally { await restarted.close(); }
});

test('restart converts queued lifecycle and transitional app to unknown with remaining resource scope', async (t) => {
  const f = await fixture(t), plan = await f.plan('delete'); await f.product.close();
  const store = await createProductStore(f.directory), id = randomUUID();
  await store.transaction((state) => {
    state.applications[app.id].status = 'deleting';
    state.operations[id] = { id, session_id: f.owner.id, kind: 'application-lifecycle', application_id: app.id,
      action: 'delete', plan_id: plan.id, status: 'queued', steps: [], residuals: [] };
  });
  await store.close();
  const restarted = await createProductService(f.options);
  try {
    const result = restarted.getOperation(id, f.owner.id);
    assert.equal(result.status, 'unknown'); assert.equal(result.error.code, 'INTERRUPTED');
    assert.deepEqual(result.residuals, resources); assert.equal(restarted.getApplication(app.id, f.owner.id).status, 'unknown');
    assert.equal(f.calls.apply, 0);
  } finally { await restarted.close(); }
});

test('adapter pins operator configuration, writes private requests, sanitizes receipts and never repeats an invocation', async (t) => {
  const directory = await realpath(await mkdtemp(join(tmpdir(), 'railshot-lifecycle-adapter-'))); t.after(() => rm(directory, { recursive: true, force: true }));
  const configPath = join(directory, 'config.json');
  const config = { version: 1, state_dir: join(directory, 'registrations'), environments: { 'runtime-aws': { provider: 'aws', tenant: 'team', source_repository: 'owner/apps' } } };
  await writeFile(configPath, JSON.stringify(config), { mode: 0o600 });
  const calls = [];
  const adapter = await createApplicationAdapter({ configPath, ciIdentity: { tenant: 'team', sourceRepository: 'owner/apps' }, loadPublished: async () => [],
    runner: async (_python, args, options) => {
      assert.ok(args[0].endsWith('/application_lifecycle.py')); const path = args.at(-1);
      assert.equal((await stat(path)).mode & 0o777, 0o600);
      const request = JSON.parse(await readFile(path)); calls.push({ request, options });
      if (request.phase === 'plan') return { status: 'planned', plan_id: request.operation_id, plan_hash: 'c'.repeat(64),
        expires_at: new Date(Date.now() + 600000).toISOString(), resources: [{ ...resources[0], private_path: '/secret' }], retained };
      return { status: 'blocked', application_id: request.application_id, action: request.action,
        steps: [{ name: 'delete', status: 'blocked', stderr: '/secret' }], residuals: [{ ...resources[0], token: 'secret' }], private: '/secret' };
    } });
  const application = adapter.describe('runtime-aws', 'calculator'), id = randomUUID();
  const pending = await adapter.planPendingDeletion(application, { id: randomUUID(), deploymentId: randomUUID() });
  assert.equal(pending.private.deferred, true); assert.equal(calls.length, 0);
  assert.deepEqual(pending.public.resources, [{ kind: 'ApplicationNamespace', name: application.id }, { kind: 'ApplicationRoutes', name: application.id }]);
  await adapter.verifyLifecyclePlan(application, pending);
  await assert.rejects(adapter.applyLifecycle(application, pending, { id: randomUUID(), deleteData: true }), { code: 'APPLICATION_PLAN_STALE' });
  await assert.rejects(adapter.verifyLifecyclePlan(application, { ...pending, public: { ...pending.public, plan_hash: '0'.repeat(64) } }), { code: 'APPLICATION_PLAN_STALE' });
  const plan = await adapter.planLifecycle(application, { id, action: 'delete' });
  assert.deepEqual(plan.public.resources, resources);
  const operationId = randomUUID(), result = await adapter.applyLifecycle(application, plan, { id: operationId, deleteData: true });
  assert.deepEqual(result.residuals, resources); assert.deepEqual(result.steps, [{ name: 'delete', status: 'blocked' }]);
  assert.ok(!JSON.stringify(result).includes('secret')); assert.equal(calls[0].options.mutation, false); assert.equal(calls[1].options.mutation, true);
  assert.deepEqual(calls[1].request, { version: 1, application_id: application.id, environment_id: 'runtime-aws', app: 'calculator',
    phase: 'apply', action: 'delete', operation_id: operationId, plan_id: id, plan_hash: 'c'.repeat(64), delete_data: true });
  await assert.rejects(adapter.applyLifecycle(application, plan, { id: operationId, deleteData: true }), { code: 'APPLICATION_OPERATION_RECONCILE_REQUIRED' });
  await writeFile(configPath, JSON.stringify({ ...config, changed: true }));
  await assert.rejects(adapter.verifyLifecyclePlan(application, plan), { code: 'APPLICATION_POLICY_CHANGED' });
  assert.equal(calls.length, 2);
});

test('HTTP plan and operation routes enforce session, JSON, idempotency and polling headers', async (t) => {
  const f = await fixture(t);
  const server = createAppServer({ product: f.product });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  t.after(() => new Promise((resolve) => server.close(resolve)));
  const base = `http://127.0.0.1:${server.address().port}`;
  const headers = { cookie: 'railshot_session=' + f.owner.token, 'content-type': 'application/json' };
  const path = `/api/v1/applications/${app.id}`;
  const request = (suffix, body, more = {}) => fetch(base + path + suffix, { method: 'POST', headers: { ...headers, ...more }, body: JSON.stringify(body) });
  const preview = await request('/plans', { action: 'delete' }); assert.equal(preview.status, 201);
  const plan = await preview.json(); assert.equal(plan.application_id, app.id);
  assert.equal((await request('/operations', f.input(plan))).status, 422);
  assert.equal((await request('/plans', { action: 'delete', credentials: 'denied' })).status, 422);
  assert.equal((await request('/plans', { action: 'delete' }, { cookie: 'railshot_session=' + f.stranger.token })).status, 404);
  const response = await request('/operations', f.input(plan), { 'Idempotency-Key': 'http-delete' });
  assert.equal(response.status, 202); assert.equal(response.headers.get('retry-after'), '2');
  assert.equal(response.headers.get('cache-control'), 'no-store'); assert.ok(response.headers.get('x-request-id'));
  const body = await response.json(); assert.equal(body.action, 'delete');
  assert.match(body.id, /^[a-f0-9-]{36}$/); assert.equal(body.application_id, app.id);
  assert.ok(['queued', 'running'].includes(body.status)); assert.equal(body.resource_id, undefined);
  assert.equal(response.headers.get('location'), '/api/v1/operations/' + body.id);
  await settled(f.product, body.id, f.owner.id);
  const polled = await fetch(base + response.headers.get('location'), { headers });
  assert.equal(polled.status, 200); assert.equal((await polled.json()).status, 'succeeded');
  const replay = await request('/operations', f.input(plan), { 'Idempotency-Key': 'http-delete' });
  assert.equal(replay.status, 200); assert.equal((await replay.json()).id, body.id);
  assert.equal((await fetch(base + response.headers.get('location'), { headers: { cookie: 'railshot_session=' + f.stranger.token } })).status, 404);
  assert.equal((await fetch(base + response.headers.get('location') + '?extra=1', { headers })).status, 422);
});

test('same-app in-flight CI is cancelled once and quiescent before cleanup; unknown or changed application never deletes', async (t) => {
  for (const mode of ['cancelled', 'cancel_unknown', 'changed_application']) await t.test(mode, async (t) => {
    let unblock, observed; const waiting = new Promise((resolve) => { unblock = resolve; });
    const reading = new Promise((resolve) => { observed = resolve; });
    const events = [], allowed = new Set(['runtime-aws']); let plans = 0;
    const f = await fixture(t, { options: { pollInterval: 5, service: { targetId: 'runtime-aws', get targetIds() { return [...allowed]; },
      allowTarget: (id) => allowed.add(id),
      deploy: async () => ({ run_id: '123', source_commit: 'd'.repeat(40) }),
      status: async () => { observed(); await waiting; events.push('observer_returned'); return { state: 'running', status: 'in_progress', source_commit: 'd'.repeat(40) }; },
      cancel: async (id, binding) => {
        events.push('cancel'); assert.equal(id, '123'); assert.equal(binding.app, app.app); assert.equal(binding.source_commit, 'd'.repeat(40));
        if (mode === 'cancel_unknown') throw new Error('private upstream diagnostic');
        return { status: 'completed' };
      } } }, adapter: {
      register: async (application) => ({ ...application, status: 'ready' }),
      deployPublished: async () => assert.fail('Deletion must prevent any later CD'),
      planLifecycle: async (application, { id, action }) => ({ public: { id, application_id: mode === 'changed_application' ? 'another-app' : application.id, action,
        plan_hash: String(++plans).repeat(64), resources,
        retained: [], expires_at: new Date(Date.now() + 600000).toISOString() }, private: { secret: '/private/ref' } }),
      applyLifecycle: async (application, plan) => { events.push('cleanup'); assert.equal(plans, 1);
        assert.equal(events.filter((value) => value === 'cancel').length, 1);
        return { application_id: application.id, action: plan.public.action, status: 'succeeded', steps: [{ name: 'delete', status: 'succeeded' }], residuals: [] }; },
    } });
    const deployment = await f.product.createDeployment(source, 'deploy', undefined, f.owner.id); await reading;
    try {
      const plan = await f.plan('delete'); assert.ok(plan.resources.some((row) => row.kind === 'DeploymentOperation' && row.name === deployment.id));
      const operation = await f.product.createApplicationOperation(app.id, f.input(plan), 'delete-active', f.owner.id);
      assert.equal(disk(f.directory, 'operations', deployment.id).deletion_requested, operation.id);
      assert.ok(!events.includes('cleanup')); unblock();
      const result = await settled(f.product, operation.id, f.owner.id);
      assert.equal(result.status, mode === 'cancelled' ? 'succeeded' : 'unknown');
      assert.equal(events.filter((value) => value === 'cancel').length, 1);
      assert.equal(events.includes('cleanup'), mode === 'cancelled');
      assert.ok(!JSON.stringify(result).includes('/private/ref'));
      assert.equal((await f.product.createApplicationOperation(app.id, f.input(plan), 'delete-active', f.owner.id)).id, operation.id);
      assert.equal(events.filter((value) => value === 'cancel').length, 1);
    } finally { unblock(); }
  });
});

test('resource projections accept DNS validation names but never expose private fields', () => {
  const row = { kind: 'DNSRecord', name: '_acme-challenge.calculator.example.com', private_key: 'not-public' };
  assert.deepEqual(lifecycleResources([row]), [{ kind: row.kind, name: row.name }]);
  assert.throws(() => lifecycleResources([{ kind: 'DNSRecord', name: '/private/path' }]));
});

test('registration-stage deletion saves owned scope then waits for registration before a fresh exact plan', async (t) => {
  let finish, enter;
  const registering = new Promise((resolve) => { enter = resolve; });
  const waiting = new Promise((resolve) => { finish = resolve; });
  const events = [];
  const f = await fixture(t, { adapter: {
    register: async (application) => { enter(); await waiting; events.push('registered'); return { ...application, status: 'ready', namespace: app.id }; },
    planPendingDeletion: async (application, { id, deploymentId }) => ({ public: { id, application_id: application.id, action: 'delete',
      plan_hash: 'e'.repeat(64), resources: [{ kind: 'ApplicationNamespace', name: app.id }, { kind: 'ApplicationRoutes', name: app.id }],
      retained: [], expires_at: new Date(Date.now() + 600000).toISOString() }, private: { deferred: true, deployment_id: deploymentId } }),
    planLifecycle: async (application, { id, action }) => { assert.deepEqual(events, ['registered']); events.push('fresh-plan');
      return { public: { id, application_id: application.id, action, plan_hash: 'f'.repeat(64), resources,
        retained: [], expires_at: new Date(Date.now() + 600000).toISOString() }, private: {} }; },
    applyLifecycle: async (application, plan) => { assert.equal(plan.private.deferred, undefined); assert.deepEqual(events, ['registered', 'fresh-plan']);
      events.push('cleanup'); return { status: 'succeeded', application_id: application.id, action: 'delete', steps: [], residuals: [] }; },
  } });
  const deployment = await f.product.createDeployment(source, 'registering', undefined, f.owner.id); await registering;
  try {
    const plan = await f.plan('delete'); assert.equal(f.product.getApplication(app.id, f.owner.id).status, 'registering');
    const operation = await f.product.createApplicationOperation(app.id, f.input(plan), 'delete-registering', f.owner.id);
    assert.equal(disk(f.directory, 'operations', deployment.id).deletion_requested, operation.id);
    assert.deepEqual(events, []); finish();
    const result = await settled(f.product, operation.id, f.owner.id);
    assert.equal(result.status, 'succeeded'); assert.deepEqual(events, ['registered', 'fresh-plan', 'cleanup']);
    assert.equal(f.calls.dispatch, 0); assert.equal(f.product.getApplication(app.id, f.owner.id).status, 'deleted');
  } finally { finish(); }
});

test('expired plans and shared plan capacity reject before any lifecycle execution', async (t) => {
  const f = await fixture(t, { options: { maxOperations: 1 } });
  const original = f.adapter.planLifecycle;
  f.adapter.planLifecycle = async (...args) => {
    const plan = await original(...args); plan.public.expires_at = '2000-01-01T00:00:00Z'; return plan;
  };
  const expired = await f.plan('delete');
  await assert.rejects(f.product.createApplicationOperation(app.id, f.input(expired), 'expired', f.owner.id), { code: 'APPLICATION_PLAN_STALE' });
  await assert.rejects(f.plan('delete'), { code: 'CAPACITY_EXCEEDED' });
  assert.equal(f.calls.apply, 0);
});

test('a blocked executor with no inventory retains the approved resources as unverified residuals', async (t) => {
  const f = await fixture(t, { adapter: { applyLifecycle: async () => ({ status: 'blocked', application_id: app.id,
    action: 'delete', steps: [], residuals: [] }) } });
  const plan = await f.plan('delete');
  const operation = await f.product.createApplicationOperation(app.id, f.input(plan), 'blocked', f.owner.id);
  const result = await settled(f.product, operation.id, f.owner.id);
  assert.equal(result.status, 'blocked'); assert.deepEqual(result.residuals, resources);
  assert.equal(f.product.getApplication(app.id, f.owner.id).status, 'unknown');
});

test('active CD deletion previews without taking its registration lock and waits for CD completion before cleanup', async (t) => {
  let finish, enter; const entered = new Promise((resolve) => { enter = resolve; });
  const waiting = new Promise((resolve) => { finish = resolve; });
  const events = [], allowed = new Set(['runtime-aws']);
  const f = await fixture(t, { options: { service: { targetId: 'runtime-aws', get targetIds() { return [...allowed]; },
    allowTarget: (id) => allowed.add(id), deploy: async () => ({ run_id: '123', source_commit: 'd'.repeat(40) }),
    status: async () => ({ state: 'published', status: 'completed', source_commit: 'd'.repeat(40),
      publication: { run_id: 123, source_commit: 'd'.repeat(40), target_id: app.id, app: app.app } }),
    cancel: async () => { events.push('ci-quiescent'); return { status: 'completed' }; },
  } }, adapter: {
    register: async (application) => ({ ...application, status: 'ready' }),
    deployPublished: async () => { enter(); await waiting; events.push('cd-completed');
      return { cd: { deployed: true, state: 'succeeded', revision: 'e'.repeat(40) } }; },
    applyLifecycle: async (application) => { assert.deepEqual(events, ['cd-completed', 'ci-quiescent']); events.push('cleanup');
      return { status: 'succeeded', application_id: application.id, action: 'delete', steps: [], residuals: [] }; },
  } });
  await f.product.createDeployment(source, 'cd-running', undefined, f.owner.id); await entered;
  try {
    const plan = await f.plan('delete'); assert.equal(f.calls.plan, 0);
    const operation = await f.product.createApplicationOperation(app.id, f.input(plan), 'delete-cd', f.owner.id);
    await pause(10); assert.deepEqual(events, []); assert.equal(f.product.getOperation(operation.id, f.owner.id).stage, 'quiescing');
    finish(); const result = await settled(f.product, operation.id, f.owner.id);
    assert.equal(result.status, 'succeeded'); assert.equal(f.calls.plan, 1);
    assert.deepEqual(events, ['cd-completed', 'ci-quiescent', 'cleanup']);
  } finally { finish(); }
});

test('deletion during resumed CD waits for the registered worker before fresh planning or cleanup', async (t) => {
  let finish, enter; const entered = new Promise((resolve) => { enter = resolve; });
  const waiting = new Promise((resolve) => { finish = resolve; });
  const events = [], allowed = new Set(['runtime-aws']); let registrations = 0, submissions = 0, deliveries = 0;
  const publication = { run_id: 123, source_commit: 'd'.repeat(40), target_id: app.id, app: app.app,
    artifact_id: 456, producer_attempt: 1, images: { web: `ghcr.io/example/apps@sha256:${'e'.repeat(64)}` } };
  const f = await fixture(t, { options: { service: { targetId: 'runtime-aws', get targetIds() { return [...allowed]; },
    allowTarget: (id) => allowed.add(id), deploy: async () => { submissions++; return { run_id: '123', source_commit: publication.source_commit }; },
    status: async () => ({ state: 'published', status: 'completed', source_commit: publication.source_commit, publication }),
    cancel: async () => { events.push('ci-quiescent'); return { status: 'completed' }; },
  } }, adapter: {
    register: async (application) => { registrations++; return { ...application, status: 'ready' }; },
    deployPublished: async () => {
      if (++deliveries === 1) throw new Error('Interrupted CD');
      enter(); await waiting; events.push('cd-completed');
      return { cd: { deployed: true, state: 'succeeded', revision: 'f'.repeat(40) },
        public_http: { state: 'succeeded', verified_at: new Date().toISOString(), url: 'https://calculator.example.test' } };
    },
    applyLifecycle: async (application) => { assert.deepEqual(events, ['cd-completed', 'ci-quiescent']); events.push('cleanup');
      return { status: 'succeeded', application_id: application.id, action: 'delete', steps: [], residuals: [] }; },
  } });
  const deployment = await f.product.createDeployment(source, 'resume-delete', undefined, f.owner.id);
  assert.equal((await settledDeployment(f.product, deployment.id, f.owner.id)).status, 'unknown');
  await f.product.resumeDeployment(deployment.id, f.owner.id); await entered;
  try {
    const plan = await f.plan('delete');
    const operation = await f.product.createApplicationOperation(app.id, f.input(plan), 'delete-resumed', f.owner.id);
    assert.equal(disk(f.directory, 'operations', deployment.id).deletion_requested, operation.id);
    await pause(10);
    assert.equal(f.product.getOperation(operation.id, f.owner.id).stage, 'quiescing');
    assert.deepEqual(events, []); assert.equal(f.calls.plan, 0);
    finish(); assert.equal((await settled(f.product, operation.id, f.owner.id)).status, 'succeeded');
    assert.deepEqual(events, ['cd-completed', 'ci-quiescent', 'cleanup']); assert.equal(f.calls.plan, 1);
    const cancelled = await f.product.getDeployment(deployment.id, f.owner.id);
    assert.equal(cancelled.status, 'blocked'); assert.equal(cancelled.stage, 'cancelled');
    assert.equal(f.product.getApplication(app.id, f.owner.id).status, 'deleted');
    assert.deepEqual([registrations, submissions, deliveries], [1, 1, 2]);
  } finally { finish(); }
});

test('ready application with successful stop and start history can resume its original CD without new CI or registration', async (t) => {
  const allowed = new Set(['runtime-aws']); let registrations = 0, submissions = 0, deliveries = 0;
  const publication = { run_id: 123, source_commit: 'd'.repeat(40), target_id: app.id, app: app.app,
    artifact_id: 456, producer_attempt: 1, images: { web: `ghcr.io/example/apps@sha256:${'e'.repeat(64)}` } };
  const f = await fixture(t, { options: { service: { targetId: 'runtime-aws', get targetIds() { return [...allowed]; },
    allowTarget: (id) => allowed.add(id), deploy: async () => { submissions++; return { run_id: '123', source_commit: publication.source_commit }; },
    status: async () => ({ state: 'published', status: 'completed', source_commit: publication.source_commit, publication }),
  } }, adapter: {
    register: async (application) => { registrations++; return { ...application, status: 'ready' }; },
    deployPublished: async () => {
      if (++deliveries === 1) throw new Error('Interrupted CD');
      return { cd: { deployed: true, state: 'succeeded', revision: 'f'.repeat(40) },
        public_http: { state: 'succeeded', verified_at: new Date().toISOString(), url: 'https://calculator.example.test' } };
    },
  } });
  let last;
  for (const action of ['stop', 'start']) {
    const plan = await f.plan(action); last = await f.product.createApplicationOperation(app.id, f.input(plan), action, f.owner.id);
    assert.equal((await settled(f.product, last.id, f.owner.id)).status, 'succeeded');
  }
  assert.equal(f.product.getApplication(app.id, f.owner.id).status, 'ready');
  const deployment = await f.product.createDeployment(source, 'after-start', undefined, f.owner.id);
  const original = await settledDeployment(f.product, deployment.id, f.owner.id);
  assert.equal(original.status, 'unknown');
  assert.equal(f.product.getApplication(app.id, f.owner.id).lifecycle_operation_id, last.id);
  await f.product.resumeDeployment(deployment.id, f.owner.id);
  const resumed = await settledDeployment(f.product, deployment.id, f.owner.id);
  assert.equal(resumed.status, 'succeeded'); assert.equal(resumed.id, original.id); assert.equal(resumed.resume_count, 1);
  assert.equal(resumed.source_commit, original.source_commit);
  for (const key of ['run_id', 'publication_artifact_id', 'producer_attempt', 'images']) assert.deepEqual(resumed.ci[key], original.ci[key]);
  assert.equal(f.product.getApplication(app.id, f.owner.id).lifecycle_operation_id, last.id);
  assert.deepEqual([registrations, submissions, deliveries, f.calls.apply], [1, 1, 2, 2]);
});

test('GitHub cancellation binds workflow/source/repository and sends a single cancel request before verified completion', async () => {
  const binding = { target_id: 'runtime-aws', source_commit: 'a'.repeat(40) };
  const base = { id: 123, path: '.github/workflows/railshot-deploy.yml', head_sha: binding.source_commit,
    head_branch: 'main', event: 'workflow_dispatch', repository: { full_name: 'owner/apps' },
    html_url: 'https://github.com/owner/apps/actions/runs/123', run_attempt: 1, status: 'in_progress' };
  for (const mode of ['completed', 'source_mismatch', 'initial_attempt_changed', 'attempt_changed', 'lost_cancel']) {
    let reads = 0, cancels = 0;
    const service = createDeploymentService({ token: 'synthetic', owner: 'owner', repo: 'apps', targetId: binding.target_id }, async (url, options) => {
      if (url.endsWith('/cancel')) {
        cancels++; assert.equal(options.method, 'POST'); assert.equal(options.redirect, 'error');
        if (mode === 'lost_cancel') throw new Error('connection lost');
        return new Response(null, { status: 202 });
      }
      reads++;
      return Response.json({ ...base, ...(mode === 'source_mismatch' ? { head_sha: 'b'.repeat(40) } : {}),
        ...(mode === 'initial_attempt_changed' ? { run_attempt: 2 } : {}),
        ...(reads > 1 ? { status: 'completed', run_attempt: mode === 'attempt_changed' ? 2 : 1 } : {}) });
    });
    if (mode === 'completed') assert.equal((await service.cancel('123', binding)).status, 'completed');
    else await assert.rejects(service.cancel('123', binding));
    assert.equal(cancels, ['source_mismatch', 'initial_attempt_changed'].includes(mode) ? 0 : 1);
    assert.equal(reads, ['lost_cancel', 'source_mismatch', 'initial_attempt_changed'].includes(mode) ? 1 : 2);
  }
});
