import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, readFile, rm, writeFile, realpath, symlink } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { DatabaseSync } from 'node:sqlite';
import { createProjectCipher } from '../src/project-crypto.js';
import { createProductService } from '../src/product.js';
import { createProductStore } from '../src/product-store.js';
import { createAppServer } from '../src/server.js';
import { apiAccessConfig } from '../src/access.js';
import { setTimeout as pause } from 'node:timers/promises';

const secret = 'test-only-canary-SUPER-SECRET-90815';
const input = (base = null, operations = [{ operation: 'set', name: 'API_TOKEN', kind: 'secret', value: secret, required: true }, { operation: 'set', name: 'LOG_LEVEL', value: 'debug' }]) => ({ base_revision_id: base, operations });
async function fixture(t, overrides = {}) {
  const home = await realpath(await mkdtemp(join(tmpdir(), 'railshot-project-'))), directory = join(home, 'state');
  await mkdir(directory, { mode: 0o700 });
  const keyFile = join(home, 'external-key.json');
  await writeFile(keyFile, JSON.stringify({ version: 1, active_key_id: 'test-v1', keys: { 'test-v1': 'b'.repeat(64) } }), { mode: 0o600 });
  let p;
  const start = async () => p = await createProductService({ directory, projectKeyFile: keyFile, ...overrides });
  await start();
  t.after(async () => { await p.close(); await rm(home, { recursive: true, force: true }); });
  return { get p() { return p; }, home, directory, keyFile, restart: async () => { await p.close(); await start(); } };
}
async function settle(fn) { for (let i = 0; i < 500; i++) { const r = await fn(); if (!['queued', 'running'].includes(r.status)) return r; await pause(5); } assert.fail('operation timeout'); }

test('envelope encryption authenticates project/revision/variable coordinates and external key identity', async t => {
  const f = await fixture(t), cipher = await createProjectCipher({ keyFile: f.keyFile, stateDirectory: f.directory });
  const encrypted = cipher.seal(secret, 'project:a/revision:1/name:TOKEN');
  assert.equal(cipher.open(encrypted, 'project:a/revision:1/name:TOKEN'), secret);
  for (const context of ['project:b/revision:1/name:TOKEN', 'project:a/revision:2/name:TOKEN', 'project:a/revision:1/name:OTHER']) assert.throws(() => cipher.open(encrypted, context));
  assert.throws(() => cipher.open({ ...encrypted, key_id: 'missing' }, 'project:a/revision:1/name:TOKEN'));
  const inside = join(f.directory, 'inside.json'); await writeFile(inside, await readFile(f.keyFile), { mode: 0o600 });
  const alias = join(f.home, 'alias'); await symlink(f.directory, alias);
  await assert.rejects(createProjectCipher({ keyFile: join(alias, 'inside.json'), stateDirectory: f.directory }), /External/);
});

test('immutable revisions persist encrypted values, retain omitted secrets, reject stale writes and never reveal secret values', async t => {
  const f = await fixture(t), p = await f.p.createProject({ name: 'Project' }, 'create', null);
  const revision = await f.p.createRevision(p.id, input(), 'save', null);
  assert.deepEqual(await f.p.createRevision(p.id, input(), 'save', null), revision);
  await assert.rejects(f.p.createRevision(p.id, input(null, [{ operation: 'set', name: 'TOKEN', value: 'different' }]), 'save', null), { code: 'IDEMPOTENCY_CONFLICT' });
  let variables = f.p.variables(p.id, null);
  assert.equal(variables.items[0].has_value, true); assert.equal('value' in variables.items[0], false); assert.equal(variables.items[1].value, 'debug');
  assert.ok(!JSON.stringify(variables).includes(secret));
  const second = await f.p.createRevision(p.id, input(revision.id, [{ operation: 'set', name: 'API_TOKEN', required: false }, { operation: 'delete', name: 'LOG_LEVEL' }]), 'save2', null);
  await assert.rejects(f.p.createRevision(p.id, input(revision.id), 'stale', null), { code: 'REVISION_CONFLICT' });
  for (const name of ['DATABASE_URL', 'PORT', 'PGSSLMODE', 'PGSSLROOTCERT']) await assert.rejects(f.p.createRevision(p.id, input(second.id, [{ operation: 'set', name, value: 'x' }]), name, null), { code: 'INVALID_INPUT' });
  await f.restart(); variables = f.p.variables(p.id, null); assert.equal(variables.revision_id, second.id); assert.equal(variables.items.length, 1); assert.equal(variables.items[0].has_value, true);
  const db = new DatabaseSync(join(f.directory, 'dashboard.sqlite3'), { readOnly: true });
  const rows = db.prepare('SELECT record FROM revisions').all(); db.close();
  assert.equal(rows.length, 2); assert.ok(!JSON.stringify(rows).includes(secret));
  const first = JSON.parse(rows[0].record), next = JSON.parse(rows[1].record);
  const cipher = await createProjectCipher({ keyFile: f.keyFile, stateDirectory: f.directory });
  const context = JSON.stringify([p.id, next.id, 'API_TOKEN', 'common', null]);
  assert.equal(cipher.open(next.variables[0].encrypted, context), secret);
  assert.throws(() => cipher.open(first.variables[0].encrypted, context));
});

test('HTTP contracts isolate anonymous owners, keep safe headers/errors and reject duplicate keys/unknown fields', async t => {
  const f = await fixture(t);
  const server = createAppServer({ product: f.p, access: apiAccessConfig({ RAILSHOT_PUBLIC_DEMO: '1', RAILSHOT_ALLOWED_HOSTS: '127.0.0.1', RAILSHOT_ALLOWED_ORIGINS: 'http://127.0.0.1' }) });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(() => new Promise(resolve => { server.close(resolve); server.closeAllConnections(); }));
  const base = `http://127.0.0.1:${server.address().port}`;
  let cookie;
  const request = async (path, body, key = 'key', owner = true) => {
    const r = await fetch(base + path, { method: body === undefined ? 'GET' : 'POST', headers: { ...(owner && cookie ? { Cookie: cookie } : {}), 'Content-Type': 'application/json', 'Idempotency-Key': key }, ...(body === undefined ? {} : { body: typeof body === 'string' ? body : JSON.stringify(body) }) });
    if (owner && r.headers.get('set-cookie')) cookie = r.headers.get('set-cookie').split(';')[0];
    return { response: r, body: await r.json() };
  };
  const created = await request('/api/v1/projects', { name: 'HTTP' }); assert.equal(created.response.status, 201);
  assert.equal(created.response.headers.get('location'), '/api/v1/projects/' + created.body.id); assert.equal(created.response.headers.get('cache-control'), 'no-store');
  const path = '/api/v1/projects/' + created.body.id;
  assert.equal((await request(path, undefined, 'unused', false)).response.status, 404);
  const saved = await request(path + '/revisions', input(), 'rev'); assert.equal(saved.response.status, 201);
  const publicVars = await request(path + '/variables'); assert.ok(!JSON.stringify(publicVars.body).includes(secret));
  assert.equal((await request(path + '/revisions', input(), 'rev', false)).response.status, 404);
  const unknown = await request(path + '/revisions', { ...input(saved.body.id), vault_path: '/admin' }, 'bad'); assert.equal(unknown.response.status, 422); assert.ok(!JSON.stringify(unknown.body).includes(secret));
  assert.equal(unknown.body.error.request_id, unknown.response.headers.get('x-request-id'));
  assert.equal((await request('/api/v1/projects', '{"name":"a","name":"b"}', 'duplicate')).response.status, 422);
  assert.equal((await request(path + '/variables?bad=1')).response.status, 422);
});

test('missing management key gates writes without fabricated readiness', async t => {
  const f = await fixture(t, { projectKeyFile: undefined }), project = await f.p.createProject({ name: 'No key' }, 'a', null);
  assert.equal(f.p.variables(project.id, null).capabilities.storage, false);
  await assert.rejects(f.p.createRevision(project.id, input(), 'b', null), { code: 'CAPABILITY_UNAVAILABLE' });
});

async function linkedFixture(t, { uncertain = false, hold = false } = {}) {
  let release, runs = 0;
  const gate = hold ? new Promise(resolve => { release = resolve; }) : Promise.resolve();
  const calls = [], publications = new Map(), files = [{ path: 'app.js', content: Buffer.from('test application') }];
  const sourceCommit = 'a'.repeat(40);
  const service = { targetId: 'env', allowTarget: () => {}, sourceFiles: async () => files,
    deploy: async value => { const id = ++runs; publications.set(id, { run_id: id, target_id: value.target_id, app: value.app, source_commit: sourceCommit, artifact_id: id + 100, producer_attempt: 1 }); return { run_id: id, source_commit: sourceCommit }; },
    status: async id => ({ run_id: Number(id), state: 'published', status: 'completed', conclusion: 'success', publication: publications.get(Number(id)) }) };
  const f = await fixture(t, { service, pollInterval: 1,
    applicationAdapter: { targets: { env: { provider: 'aws' }, other: { provider: 'gcp' } },
      describe: (environment, app) => ({ id: environment + '-app', app, environment_target_id: environment, target_id: environment + '-app' }),
      register: async app => ({ ...app, status: 'ready' }),
      deployPublished: async (app, args) => {
        if (args.configuration) assert.ok(args.configuration.revision_id);
        return { cd: { deployed: true, state: 'deployed', revision: 'b'.repeat(40) }, public_http: { state: 'succeeded', verified_at: new Date().toISOString(), url: 'https://test.example' } };
      } },
    secretsAdapter: { supports: () => true, deliver: async value => { calls.push(value); await gate; if (uncertain) throw new Error(secret); return { status: 'succeeded', observed_revision_id: value.revision_id,
      configuration: { project_id: value.project_id, binding_id: value.binding_id, revision_id: value.revision_id },
      checks: { synchronized: true, workload_ready: true, service_ready: true } }; } } });
  const first = await f.p.createDeployment({ app: 'demo', target_id: 'env', source_type: 'folder', files }, 'first', null, null);
  const deployed = await settle(() => f.p.getDeployment(first.id)); assert.equal(deployed.status, 'succeeded');
  const app = f.p.getApplication('env-app', null), project = f.p.getProject(app.project_id, null);
  const revision = await f.p.createRevision(project.id, input(), 'revision', null);
  return { ...f, get p() { return f.p; }, app, project, revision, calls, release };
}

test('legacy app linking is repeatable, deliveries enforce global admission and observe exact revision before marking applied', async t => {
  const f = await linkedFixture(t, { hold: true });
  const operation = await f.p.createDelivery(f.project.id, { binding_id: f.app.binding_id, revision_id: f.revision.id }, 'deliver', null);
  await assert.rejects(f.p.createRevision(f.project.id, input(f.revision.id), 'write', null), { code: 'PROJECT_BUSY' });
  await assert.rejects(f.p.createDelivery(f.project.id, { binding_id: f.app.binding_id, revision_id: f.revision.id }, 'second', null), { code: 'EXECUTOR_BUSY' });
  assert.equal(f.p.variables(f.project.id, null).bindings[0].applied_revision_id, null);
  f.release(); const final = await settle(() => f.p.getProjectOperation(f.project.id, 'deliveries', operation.id, null));
  assert.equal(final.status, 'succeeded'); assert.equal(final.observed_revision_id, f.revision.id); assert.equal(f.calls[0].variables.find(v => v.name === 'API_TOKEN').value, secret);
  assert.ok(!JSON.stringify(final).includes(secret)); assert.equal(f.p.variables(f.project.id, null).bindings[0].applied_revision_id, f.revision.id);
  await f.restart(); assert.equal(f.p.getApplication('env-app', null).project_id, f.project.id);
});

test('unknown delivery survives restart, retains previous application and occupies global lock', async t => {
  const f = await linkedFixture(t, { uncertain: true });
  const op = await f.p.createDelivery(f.project.id, { binding_id: f.app.binding_id, revision_id: f.revision.id }, 'deliver', null);
  const final = await settle(() => f.p.getProjectOperation(f.project.id, 'deliveries', op.id, null)); assert.equal(final.status, 'unknown'); assert.ok(!JSON.stringify(final).includes(secret));
  await f.restart(); assert.equal(f.p.getProjectOperation(f.project.id, 'deliveries', op.id, null).status, 'unknown');
  await assert.rejects(f.p.createDelivery(f.project.id, { binding_id: f.app.binding_id, revision_id: f.revision.id }, 'again', null), { code: 'EXECUTOR_BUSY' });
  assert.equal(f.p.getProject(f.project.id, null).active_binding_id, f.app.binding_id);
});

test('transfer re-publishes source for destination, pins revision, verifies before switching and retains source app', async t => {
  const f = await linkedFixture(t), plan = await f.p.createTransfer(f.project.id, { source_binding_id: f.app.binding_id, destination_environment_id: 'other', revision_id: f.revision.id }, 'plan', null);
  assert.deepEqual(plan.blockers, []); assert.ok(!JSON.stringify(plan).includes(secret));
  await assert.rejects(f.p.executeProjectTransfer(f.project.id, plan.id, { action: 'execute', plan_hash: 'wrong' }, 'wrong', null), { code: 'PLAN_STALE' });
  const op = await f.p.executeProjectTransfer(f.project.id, plan.id, { action: 'execute', plan_hash: plan.plan_hash }, 'execute', null);
  const final = await settle(() => f.p.getProjectOperation(f.project.id, 'transfers', op.id, null));
  assert.equal(final.status, 'succeeded'); assert.equal(final.traffic_switch, 'not_requested');
  assert.notEqual(final.destination_binding_id, f.app.binding_id);
  assert.equal(f.p.getProject(f.project.id, null).active_binding_id, final.destination_binding_id);
  assert.equal(f.p.getApplication('env-app', null).status, 'ready');
  assert.equal(f.p.getApplication('other-app', null).project_id, f.project.id);
  assert.equal(f.calls.length, 2); assert.deepEqual(f.calls.map(v => v.phase), ['prepare', 'verify']);
  assert.ok(f.calls.every(v => v.environment_id === 'other' && v.revision_id === f.revision.id));
  assert.equal((await f.p.executeProjectTransfer(f.project.id, plan.id, { action: 'execute', plan_hash: plan.plan_hash }, 'execute', null)).id, plan.id);
});

test('transfer refuses unreviewed environment variables and stale revisions while preserving source binding', async t => {
  const f = await linkedFixture(t);
  const revision = await f.p.createRevision(f.project.id, input(f.revision.id, [{ operation: 'set', name: 'INTERNAL_URL', value: 'https://old.example', scope: 'environment', environment_id: 'env' }]), 'env-specific', null);
  const plan = await f.p.createTransfer(f.project.id, { source_binding_id: f.app.binding_id, destination_environment_id: 'other', revision_id: revision.id }, 'plan', null);
  assert.deepEqual(plan.required_overrides, ['INTERNAL_URL']); assert.ok(plan.blockers.includes('ENVIRONMENT_VARIABLE_REVIEW_REQUIRED'));
  await assert.rejects(f.p.executeProjectTransfer(f.project.id, plan.id, { action: 'execute', plan_hash: plan.plan_hash }, 'run', null), { code: 'CAPABILITY_UNAVAILABLE' });
  assert.equal(f.p.getProject(f.project.id, null).active_binding_id, f.app.binding_id);
  const next = await f.p.createRevision(f.project.id, input(revision.id, [{ operation: 'set', name: 'INTERNAL_URL', value: 'https://new.example', scope: 'environment', environment_id: 'other' }]), 'review', null);
  await assert.rejects(f.p.executeProjectTransfer(f.project.id, plan.id, { action: 'execute', plan_hash: plan.plan_hash }, 'stale', null), { code: 'REVISION_CONFLICT' });
  const reviewed = await f.p.createTransfer(f.project.id, { source_binding_id: f.app.binding_id, destination_environment_id: 'other', revision_id: next.id }, 'reviewed-plan', null); assert.deepEqual(reviewed.blockers, []);
});

test('failure during destination setup never switches active binding', async t => {
  const f = await linkedFixture(t, { uncertain: true }), plan = await f.p.createTransfer(f.project.id, { source_binding_id: f.app.binding_id, destination_environment_id: 'other', revision_id: f.revision.id }, 'plan', null);
  await f.p.executeProjectTransfer(f.project.id, plan.id, { action: 'execute', plan_hash: plan.plan_hash }, 'run', null);
  const final = await settle(() => f.p.getProjectOperation(f.project.id, 'transfers', plan.id, null));
  assert.equal(final.status, 'unknown'); assert.equal(f.p.getProject(f.project.id, null).active_binding_id, f.app.binding_id);
  assert.ok(!JSON.stringify(final).includes(secret)); assert.ok(final.destination_binding_id);
});

test('interrupted operations recover as unknown without replay; deleted apps preserve central project values', async t => {
  const f = await linkedFixture(t); await f.p.close();
  const store = await createProductStore(f.directory);
  await store.transaction(state => {
    state.applications['env-app'].status = 'deleted';
    state.operations['interrupted-delivery'] = { id: 'interrupted-delivery', kind: 'deliveries', session_id: null, project_id: f.project.id, binding_id: f.app.binding_id, revision_id: f.revision.id, status: 'running', stage: 'delivering' };
  }); await store.close(); await f.restart();
  assert.equal(f.p.getProjectOperation(f.project.id, 'deliveries', 'interrupted-delivery', null).status, 'unknown');
  assert.equal(f.p.variables(f.project.id, null).items.find(v => v.name === 'API_TOKEN').has_value, true);
  assert.equal(f.p.variables(f.project.id, null).bindings[0].status, 'deleted');
  assert.equal(f.calls.length, 0);
});

test('explicit observation recovers a completed child without a second deployment or source publication', async t => {
  const f = await linkedFixture(t), operation = await f.p.createDelivery(f.project.id, { binding_id: f.app.binding_id, revision_id: f.revision.id }, 'delivery', null);
  const completed = await settle(() => f.p.getProjectOperation(f.project.id, 'deliveries', operation.id, null)); assert.equal(completed.status, 'succeeded');
  await f.p.close(); const store = await createProductStore(f.directory);
  await store.transaction(state => { Object.assign(state.operations[operation.id], { status: 'running', stage: 'verifying', observed_revision_id: null }); }); await store.close(); await f.restart();
  const count = f.p.list('deployments', null, { limit: 100, marker: null }).items.length;
  const observed = await f.p.observeProjectOperation(f.project.id, 'deliveries', operation.id, { action: 'observe' }, 'observe', null);
  assert.equal(observed.status, 'succeeded'); assert.equal(f.p.list('deployments', null, { limit: 100, marker: null }).items.length, count);
  assert.equal(f.calls.at(-1).phase, 'verify');
});

test('project source and revision automatically bind to a newly provisioned dedicated environment', async t => {
  let current, calls = [];
  const files = [{ path: 'app.js', content: Buffer.from('dedicated project') }];
  const service = { targetId: 'legacy', allowTarget: () => {}, sourceFiles: async () => files,
    deploy: async value => { current = value; return { run_id: 501, source_commit: 'a'.repeat(40) }; },
    status: async () => ({ run_id: 501, state: 'published', status: 'completed', conclusion: 'success', publication: { run_id: 501, app: current.app, target_id: current.target_id, source_commit: 'a'.repeat(40), artifact_id: 502, producer_attempt: 1 } }) };
  const adapter = { profiles: () => [],
    plan: async (value, { id }) => ({ public: { id, name: value.name, executable: true, runtime_target_id: 'new-environment' },
      private: { profile: { provider: 'aws', target: { target_id: 'new-environment' }, deployment: { cd: true }, secrets_delivery_file: '/test-only/delivery-template.json', create_per_request: true } } }),
    verifyPlan: async () => {}, execute: async () => { calls.push('environment'); return { status: 'succeeded', deployment_supported: true, runtime_target_id: 'new-environment', secrets_ready: true }; },
    deployPublished: async (environmentId, args) => { calls.push('cd'); assert.ok(environmentId.endsWith('.environment')); assert.ok(args.configuration.revision_id); return { cd: { state: 'deployed', deployed: true, revision: 'b'.repeat(40) }, public_http: { state: 'succeeded', verified_at: new Date().toISOString(), url: 'https://new.example' } }; } };
  const f = await fixture(t, { service, environmentAdapter: adapter, secretsAdapter: { supports: () => true, deliver: async value => { calls.push(value.phase); return { status: 'succeeded', observed_revision_id: value.revision_id, configuration: { project_id: value.project_id, binding_id: value.binding_id, revision_id: value.revision_id }, checks: { synchronized: true, workload_ready: true, service_ready: true } }; } } });
  const project = await f.p.createProject({ name: 'Independent project' }, 'project', null);
  const revision = await f.p.createRevision(project.id, input(), 'revision', null);
  const plan = await f.p.createPlan({ name: 'demo' }, null);
  const operation = await f.p.createDeployment({ app: 'demo', target_id: 'new-environment', plan_id: plan.id, project_id: project.id, revision_id: revision.id, source_type: 'folder', files }, 'deployment', null, null);
  const completed = await settle(() => f.p.getDeployment(operation.id));
  assert.equal(completed.status, 'succeeded'); assert.deepEqual(calls, ['environment', 'prepare', 'cd', 'verify']);
  assert.equal(f.p.variables(project.id, null).bindings[0].applied_revision_id, revision.id);
  assert.equal(f.p.getApplication(completed.application_id, null).registration_kind, 'environment');
});

test('private adapter erases value transport on failure and startup, rejects unexpected receipt fields', async t => {
  const { createSecretsAdapter } = await import('../src/project-secrets.js');
  const { randomUUID } = await import('node:crypto');
  const { readdir } = await import('node:fs/promises');
  const f = await fixture(t), home = join(f.home, 'delivery'); await mkdir(home, { mode: 0o700 });
  const configPath = join(f.home, 'delivery-config.json'); await writeFile(configPath, JSON.stringify({ version: 1, environments: { env: {} } }), { mode: 0o600 });
  const stale = join(home, randomUUID() + '.prepare.json'); await writeFile(stale, secret, { mode: 0o600 });
  let requestPath;
  const adapter = await createSecretsAdapter({ configPath, stateDirectory: home, runner: async (_, args) => {
    requestPath = args[args.indexOf('--request') + 1]; const value = JSON.parse(await readFile(requestPath)); assert.equal(value.variables[0].value, secret);
    throw new Error('fake failure');
  } });
  await adapter.cleanup();
  assert.deepEqual(await readdir(home), []);
  await assert.rejects(adapter.deliver({ operation_id: randomUUID(), project_id: 'p', binding_id: 'b', revision_id: 'r', environment_id: 'env', application_id: 'app', app: 'demo', phase: 'prepare', variables: [{ name: 'TOKEN', kind: 'secret', value: secret, required: true }] }));
  await assert.rejects(readFile(requestPath), { code: 'ENOENT' });
});
