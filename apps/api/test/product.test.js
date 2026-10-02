import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, readFile, rm, stat, chmod, symlink, writeFile, readdir, mkdir } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { setTimeout as pause } from 'node:timers/promises';
import { createProductService } from '../src/product.js';
import { createProductStore } from '../src/product-store.js';
import { createAppServer } from '../src/server.js';

const files = [{ path: 'app.js', content: Buffer.from('hello') }];
const input = { app: 'demo-app', target_id: 'demo', source_type: 'folder', files };
const publication = { run_id: 123, target_id: 'demo', app: 'demo-app', source_commit: 'a'.repeat(40), artifact_id: 456, producer_attempt: 1 };
const deployed = { cd: { state: 'succeeded', deployed: true, revision: 'b'.repeat(40) }, public_http: { state: 'succeeded', verified_at: '2026-10-02T12:00:00Z', url: 'https://demo.example.test' } };
async function fixture(t, overrides = {}) {
  const directory = await mkdtemp(join(tmpdir(), 'railshot-product-'));
  let dispatches = 0, reads = 0, cdCalls = 0;
  const service = { targetId: 'demo',
    deploy: async () => { dispatches++; return { run_id: '123', app: 'demo-app', source_commit: publication.source_commit, target_id: 'demo', state: 'queued' }; },
    status: async () => { reads++; return { run_id: 123, state: 'published', status: 'completed', conclusion: 'success', publication }; },
    ...overrides.service };
  const deployPublished = overrides.deployPublished || (async () => { cdCalls++; return deployed; });
  const product = await createProductService({ service, directory, deployPublished, pollInterval: 5, ...overrides, service });
  t.after(async () => { await product.close(); await rm(directory, { recursive: true, force: true }); });
  return { product, service, directory, dispatches: () => dispatches, reads: () => reads, cdCalls: () => cdCalls };
}
async function settle(get, predicate = (record) => !['queued', 'running'].includes(record.status)) {
  const deadline = performance.now() + 10000;
  let record;
  // Durable writes and HTTP polling take longer on shared CI runners than local fixtures.
  do { record = await get(); if (predicate(record)) return record; await pause(5); } while (performance.now() < deadline);
  assert.fail(`Operation did not settle: status=${record?.status}, stage=${record?.stage}`);
}

test('snapshot and intent are durable before one dispatch; replay returns the same completed deployment', async (t) => {
  let f;
  f = await fixture(t, { service: { deploy: async () => {
    const state = JSON.parse(await readFile(join(f.directory, 'state.json'))), operation = Object.values(state.operations)[0];
    assert.equal(operation.status, 'running');
    const saved = JSON.parse(await readFile(join(f.directory, `${operation.id}.source.json`)));
    assert.equal(Buffer.from(saved[0].content, 'base64').toString(), 'hello');
    assert.equal((await stat(join(f.directory, `${operation.id}.source.json`))).mode & 0o777, 0o600);
    return { run_id: 123, source_commit: publication.source_commit };
  } } });
  const created = await f.product.createDeployment(input, 'same');
  const complete = await settle(() => f.product.getDeployment(created.id));
  assert.equal(complete.status, 'succeeded');
  assert.equal(complete.url, deployed.public_http.url);
  assert.equal(complete.ci.run_id, '123');
  assert.match(complete.source_digest, /^[a-f0-9]{64}$/);
  assert.equal(complete.observation.deployment_id, complete.id);
  assert.equal(complete.observation.metrics.pods.state, 'not_configured');
  assert.equal(complete.ci.publication_artifact_id, '456');
  assert.equal(f.cdCalls(), 1);
  assert.equal((await f.product.createDeployment(input, 'same')).id, created.id);
  assert.equal(f.cdCalls(), 1);
  await assert.rejects(f.product.createDeployment({ ...input, files: [{ ...files[0], content: Buffer.from('changed') }] }, 'same'), { code: 'IDEMPOTENCY_CONFLICT' });
  assert.equal((await f.product.getBuild('123')).id, '123');
  const before = f.reads();
  await assert.rejects(f.product.getBuild('999'), { status: 404 });
  await assert.rejects(f.product.getBuild('__proto__'), { status: 404 });
  assert.equal(f.reads(), before);
});

test('concurrent identical keys dispatch once; another intent is refused while active', async (t) => {
  let release;
  const wait = new Promise((resolve) => { release = resolve; });
  let calls = 0;
  const f = await fixture(t, { service: { deploy: async () => { calls++; await wait; return { run_id: 123, source_commit: publication.source_commit }; } } });
  const [first, second] = await Promise.all([f.product.createDeployment(input, 'same'), f.product.createDeployment(input, 'same')]);
  assert.equal(first.id, second.id);
  await assert.rejects(f.product.createDeployment(input, 'other'), { code: 'EXECUTOR_BUSY' });
  release();
  await settle(() => f.product.getDeployment(first.id));
  assert.equal(calls, 1);
});

test('CD progress persists revision and HTTP stage before completion while observer failure stays separate', async (t) => {
  let finish;
  const waiting = new Promise((resolve) => { finish = resolve; });
  const images = { web: `ghcr.io/example/demo@sha256:${'c'.repeat(64)}` };
  const f = await fixture(t, {
    service: { status: async () => ({ run_id: 123, state: 'published', publication: { ...publication, images } }) },
    deployPublished: async ({ onProgress }) => {
      await onProgress({ cd: deployed.cd, public_http: { state: 'unverified', verified_at: null, url: null } });
      await waiting; return deployed;
    },
    observeMetrics: async (record) => ({ deployment_id: record.id, metrics: { pods: { state: 'unavailable', value: null } } }),
  });
  const first = await f.product.createDeployment(input, 'progress');
  try {
    const progressing = await settle(() => f.product.getDeployment(first.id), (record) => record.stage === 'http');
    assert.equal(progressing.status, 'running'); assert.equal(progressing.cd.revision, deployed.cd.revision);
    assert.deepEqual(progressing.ci.images, images); assert.equal(progressing.url, null);
    assert.equal(progressing.observation.metrics.pods.state, 'unavailable');
    const disk = JSON.parse(await readFile(join(f.directory, 'state.json')));
    assert.equal(disk.operations[first.id].stage, 'http');
  } finally { finish(); }
  assert.equal((await settle(() => f.product.getDeployment(first.id))).status, 'succeeded');
});

test('GitHub URL replay keeps the first pinned source without fetching current HEAD', async (t) => {
  const f = await fixture(t);
  let loads = 0;
  const request = { app: input.app, target_id: input.target_id, source_type: 'github', repository_url: 'https://github.com/example/demo' };
  const loader = async () => { loads++; return { files, source: { type: 'github', repository: request.repository_url, sha: 'c'.repeat(40) } }; };
  const first = await f.product.createDeployment(request, 'github-key', loader);
  await settle(() => f.product.getDeployment(first.id));
  await f.product.createDeployment(request, 'github-key', async () => { throw new Error('Must not refetch'); });
  assert.equal(loads, 1);
});

test('lost dispatch response stays unknown, survives restart, occupies admission and never replays', async (t) => {
  let dispatches = 0;
  const f = await fixture(t, { service: { deploy: async () => { dispatches++; throw new Error('private upstream secret'); } } });
  const first = await f.product.createDeployment(input, 'uncertain');
  const failed = await settle(() => f.product.getDeployment(first.id));
  assert.equal(failed.status, 'unknown');
  assert.equal(failed.error.outcome_unknown, true);
  assert.ok(failed.error.request_id);
  assert.ok(!JSON.stringify(failed).includes('private upstream secret'));
  await f.product.close();
  const restarted = await createProductService({ service: f.service, directory: f.directory, deployPublished: async () => { assert.fail('No CD replay'); } });
  try {
    assert.equal((await restarted.createDeployment(input, 'uncertain')).id, first.id);
    await assert.rejects(restarted.createDeployment(input, 'new-key'), { code: 'EXECUTOR_BUSY' });
    assert.equal(dispatches, 1);
  } finally { await restarted.close(); }
});

test('unfinished durable intent is recovered as unknown; malformed or insecure storage fails closed', async (t) => {
  const directory = await mkdtemp(join(tmpdir(), 'railshot-recovery-'));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const store = await createProductStore(directory);
  await store.transaction((state) => { state.operations.pending = { id: 'pending', kind: 'deployments', status: 'queued' }; });
  await assert.rejects(createProductStore(directory), /already open/);
  await store.close();
  const restored = await createProductStore(directory);
  assert.equal(restored.read().operations.pending.status, 'unknown');
  await restored.close();
  await chmod(directory, 0o755);
  await assert.rejects(createProductStore(directory), /private owned/);
  await chmod(directory, 0o700);
  const linked = `${directory}-link`;
  await symlink(directory, linked);
  try { await assert.rejects(createProductStore(linked), /private owned/); } finally { await rm(linked); }
});

test('snapshot quota bounds admission before any dispatch', async (t) => {
  const f = await fixture(t, { maxSourceBytes: 1 });
  await assert.rejects(f.product.createDeployment(input, 'large'), { code: 'CAPACITY_EXCEEDED' });
  assert.equal(f.dispatches(), 0);
  assert.equal((await readdir(f.directory)).filter((name) => name.endsWith('.source.json')).length, 0);
  await assert.rejects(f.product.createDeployment({ ...input, target_id: 'foreign' }, 'bad-target'), { status: 422 });
  assert.equal(f.dispatches(), 0);
});

test('CI publication alone and mismatched publication never report application success', async (t) => {
  const f = await fixture(t, { deployPublished: async () => ({ cd: { state: 'running', revision: null, deployed: false }, public_http: { state: 'not_run', url: 'https://unverified.invalid' } }) });
  const first = await f.product.createDeployment(input, 'not-verified');
  const record = await settle(() => f.product.getDeployment(first.id));
  assert.equal(record.status, 'unknown'); assert.equal(record.url, null);
  const g = await fixture(t, { service: { status: async () => ({ state: 'published', publication: { ...publication, app: 'foreign' } }) } });
  const other = await g.product.createDeployment(input, 'mismatch');
  assert.equal((await settle(() => g.product.getDeployment(other.id))).status, 'unknown');
  assert.equal(g.cdCalls(), 0);
});

test('environment plans are private, consumed once and share deployment admission', async (t) => {
  const adapter = {
    profiles: () => [{ id: 'aws', supported: true }],
    plan: async (_, { id }) => ({ public: { id, executable: true }, private: { secret_path: '/private/key' } }),
    verifyPlan: async () => {}, execute: async () => ({ status: 'succeeded', stage: 'runtime', deployment_supported: false, runtime_target_id: 'runtime-demo' }),
  };
  const f = await fixture(t, { environmentAdapter: adapter });
  const plan = await f.product.createPlan({});
  assert.ok(!JSON.stringify(f.product.getPlan(plan.id)).includes('/private/key'));
  const environment = await f.product.createEnvironment({ plan_id: plan.id }, 'env-key');
  await settle(() => f.product.getEnvironment(environment.id));
  assert.equal((await f.product.createEnvironment({ plan_id: plan.id }, 'env-key')).id, environment.id);
  await assert.rejects(f.product.createEnvironment({ plan_id: plan.id }, 'other-key'), { code: 'CONFLICT' });
  assert.throws(() => f.product.getPlan('__proto__'), { status: 404 });
});

async function httpFixture(t, options = {}) {
  const directory = await mkdtemp(join(tmpdir(), 'railshot-http-'));
  const server = createAppServer({ stateDirectory: directory,
    service: { targetId: 'demo', deploy: async () => ({ run_id: '123', source_commit: publication.source_commit }),
      status: async () => ({ run_id: 123, state: 'published', status: 'completed', conclusion: 'success', publication }) },
    deployPublished: async () => deployed, pollInterval: 5, ...options });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  t.after(async () => { await new Promise((resolve) => server.close(resolve)); await (await server.productReady)?.close(); await rm(directory, { recursive: true, force: true }); });
  return { server, base: `http://127.0.0.1:${server.address().port}` };
}
function form() {
  const value = new FormData(); value.set('app', 'demo-app'); value.set('target_id', 'demo'); value.set('source_type', 'folder');
  value.append('files', new Blob(['hello']), 'app.js'); value.set('paths', '["app.js"]'); return value;
}
test('deployment polling preserves normalized CI steps while running and after publication', async (t) => {
  const runningSteps = [
    { key: 'loop', status: 'in_progress', conclusion: null, observed_attempt: 2 },
    { key: 'release', status: 'queued', conclusion: null, observed_attempt: null },
  ];
  let upstream = { run_id: 123, state: 'running', status: 'in_progress', conclusion: null, steps: runningSteps };
  const { base } = await httpFixture(t, { service: {
    targetId: 'demo', deploy: async () => ({ run_id: '123', source_commit: publication.source_commit }),
    status: async () => structuredClone(upstream),
  } });
  const accepted = await fetch(`${base}/api/v1/deployments`, { method: 'POST', body: form(), headers: { 'Idempotency-Key': 'steps-progress' } });
  assert.equal(accepted.status, 202);
  const get = async () => (await fetch(`${base}${accepted.headers.get('location')}`)).json();
  const running = await settle(get, (record) => record.ci?.steps?.[0]?.status === 'in_progress');
  assert.equal(running.status, 'running'); assert.equal(running.stage, 'ci');
  assert.equal(running.ci.run_id, '123'); assert.equal(running.cd.state, 'not_started');
  assert.deepEqual(running.ci.steps, runningSteps);

  const releasingSteps = [
    { key: 'loop', status: 'completed', conclusion: 'success', observed_attempt: 2 },
    { key: 'release', status: 'in_progress', conclusion: null, observed_attempt: 2 },
  ];
  upstream = { ...upstream, steps: releasingSteps };
  const releasing = await settle(get, (record) => record.ci?.steps?.[1]?.status === 'in_progress');
  assert.equal(releasing.status, 'running'); assert.equal(releasing.stage, 'ci');
  assert.deepEqual(releasing.ci.steps, releasingSteps);

  const completedSteps = releasingSteps.map((step) => ({ ...step, status: 'completed', conclusion: 'success' }));
  upstream = { ...upstream, state: 'published', status: 'completed', conclusion: 'success', steps: completedSteps, publication };
  const complete = await settle(get);
  assert.equal(complete.status, 'succeeded'); assert.equal(complete.stage, 'complete');
  assert.deepEqual(complete.ci.steps, completedSteps);
  assert.equal(complete.ci.producer_attempt, 1); // Observation attempt and verified artifact producer remain distinct.
  assert.deepEqual((await (await fetch(`${base}/api/v1/builds/123`)).json()).steps, completedSteps);
  assert.deepEqual((await (await fetch(`${base}/api/runs/123`)).json()).steps, completedSteps);
});

test('v1 HTTP contract has accepted/error headers, strict fields, preserved legacy and no foreign reads', async (t) => {
  const { base } = await httpFixture(t);
  const targets = await (await fetch(`${base}/api/v1/targets`)).json();
  assert.equal(targets.items[0].capabilities.application_deployment, true);
  const post = await fetch(`${base}/api/v1/deployments`, { method: 'POST', body: form(), headers: { 'Idempotency-Key': 'http1', 'X-Request-ID': 'untrusted' } });
  assert.equal(post.status, 202);
  const accepted = await post.json();
  assert.deepEqual(Object.keys(accepted).sort(), ['action', 'request_id', 'resource_id', 'status']);
  assert.equal(post.headers.get('x-request-id'), accepted.request_id);
  assert.notEqual(accepted.request_id, 'untrusted');
  assert.equal(post.headers.get('retry-after'), '2'); assert.equal(post.headers.get('cache-control'), 'no-store');
  const location = post.headers.get('location');
  assert.equal(location, `/api/v1/deployments/${accepted.resource_id}`);
  await settle(async () => (await fetch(`${base}${location}`)).json());
  const replay = await fetch(`${base}/api/v1/deployments`, { method: 'POST', body: form(), headers: { 'Idempotency-Key': 'http1' } });
  assert.equal(replay.status, 200); assert.equal((await replay.json()).id, accepted.resource_id);
  assert.notEqual(replay.headers.get('x-request-id'), accepted.request_id);
  const legacy = await fetch(`${base}/api/runs/123`); assert.equal(legacy.status, 200); assert.equal((await legacy.json()).run_id, 123);
  assert.equal((await fetch(`${base}/api/v1/builds/999`)).status, 404);
  assert.equal((await fetch(`${base}/api/runs/999`)).status, 404);
  for (const mutate of [(value) => value.append('app', 'other'), (value) => value.set('provider_token', 'secret'), (value) => value.set('target_id', 'foreign')]) {
    const value = form(); mutate(value);
    const response = await fetch(`${base}/api/v1/deployments`, { method: 'POST', body: value, headers: { 'Idempotency-Key': 'different' } });
    assert.equal(response.status, 422);
    const body = await response.json(); assert.equal(body.error.code, 'INVALID_INPUT'); assert.equal(body.error.request_id, response.headers.get('x-request-id'));
    assert.ok(!JSON.stringify(body).includes('secret'));
  }
  const method = await fetch(`${base}/api/v1/targets`, { method: 'POST' }); assert.equal(method.status, 405); assert.equal(method.headers.get('allow'), 'GET');
  assert.equal((await fetch(`${base}/api/v1/targets?limit=20&limit=2`)).status, 422);
  assert.equal((await fetch(`${base}/api/v1/targets?unexpected=1`)).status, 422);
  assert.equal((await fetch(`${base}/api/v1/deployments`, { method: 'POST', body: form() })).status, 422);
});

test('unconfigured server lists empty capabilities without creating state', async (t) => {
  const { base, server } = await httpFixture(t, { service: null, deployPublished: undefined });
  for (const path of ['targets', 'profiles']) assert.deepEqual(await (await fetch(`${base}/api/v1/${path}`)).json(), { items: [], next_marker: null });
  assert.equal(await server.productReady, null);
});

test('health stays live but not ready when private product state fails initialization', async (t) => {
  const directory = await mkdtemp(join(tmpdir(), 'railshot-unready-'));
  await chmod(directory, 0o750);
  let externalCalls = 0;
  const server = createAppServer({ stateDirectory: directory,
    service: { targetId: 'demo', deploy: async () => { externalCalls++; }, status: async () => { externalCalls++; } } });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  t.after(async () => { await new Promise((resolve) => server.close(resolve)); await rm(directory, { recursive: true, force: true }); });
  const base = `http://127.0.0.1:${server.address().port}`;
  const response = await fetch(`${base}/healthz`);
  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), { ok: true, configured: false, target_id: 'demo' });
  const api = await fetch(`${base}/api/v1/targets`);
  assert.equal(api.status, 503);
  assert.ok(!(await api.text()).includes(directory));
  assert.equal(externalCalls, 0);
});


test('intent write failure prevents external effects and fails subsequent writes closed', async (t) => {
  const f = await fixture(t);
  await rm(join(f.directory, 'state.json'));
  await mkdir(join(f.directory, 'state.json'));
  await assert.rejects(f.product.createDeployment(input, 'write-failure'));
  await assert.rejects(f.product.createDeployment(input, 'write-failure-2'));
  assert.equal(f.dispatches(), 0);
  assert.equal((await readdir(f.directory)).filter((name) => name.endsWith('.source.json')).length, 1);
});


test('JSON singleton keys including nested escaped duplicates are rejected before adapter calls', async (t) => {
  let calls = 0;
  const { base } = await httpFixture(t, { environmentAdapter: { profiles: () => [], plan: async () => { calls++; throw new Error('Must not execute'); } } });
  for (const raw of ['{"name":"a","name":"b"}', '{"runtime":{"node_count":1,"node_count":2}}', '{"plan_id":"a","plan_\\u0069d":"b"}']) {
    const response = await fetch(`${base}/api/v1/plans`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: raw });
    assert.equal(response.status, 422);
    assert.equal((await response.json()).error.code, 'INVALID_INPUT');
  }
  const malformed = await fetch(`${base}/api/v1/plans`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: '{"name":' });
  assert.equal(malformed.status, 400);
  assert.equal(calls, 0);
});

test('running intent becomes unknown after restart without dispatch or CD replay', async (t) => {
  const f = await fixture(t, { service: { status: async () => ({ state: 'running', status: 'in_progress', conclusion: null }) } });
  const first = await f.product.createDeployment(input, 'interrupted');
  await settle(() => f.product.getDeployment(first.id), (value) => value.ci.run_id === '123');
  await f.product.close();
  const restarted = await createProductService({ service: f.service, directory: f.directory, deployPublished: async () => assert.fail('No replay') });
  try {
    assert.equal((await restarted.getDeployment(first.id)).status, 'unknown');
    assert.equal((await restarted.createDeployment(input, 'interrupted')).id, first.id);
    assert.equal(f.dispatches(), 1);
    assert.equal(f.cdCalls(), 0);
  } finally { await restarted.close(); }
});

test('build POST preserves string run IDs and plan/environment HTTP use 201 then 202', async (t) => {
  const { base } = await httpFixture(t, { environmentAdapter: {
    profiles: () => [{ id: 'registered' }],
    plan: async (value, { id }) => ({ public: { id, ...value, executable: true }, private: { token: 'private-never-publish' } }),
    verifyPlan: async () => {}, execute: async () => ({ status: 'succeeded', stage: 'runtime', deployment_supported: false }),
  } });
  const build = await fetch(`${base}/api/v1/builds`, { method: 'POST', body: form() });
  assert.equal(build.status, 202);
  assert.equal((await build.json()).resource_id, '123');
  assert.equal(build.headers.get('location'), '/api/v1/builds/123');
  const observed = await (await fetch(`${base}/api/v1/builds/123`)).json();
  assert.equal(observed.id, '123'); assert.equal(observed.status, 'published'); assert.equal(observed.url, null);
  let planResponse;
  for (let attempt = 0; attempt < 30; attempt++) {
    planResponse = await fetch(`${base}/api/v1/plans`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{"name":"runtime-demo"}' });
    if (planResponse.status !== 409) break;
    await pause(5);
  }
  assert.equal(planResponse.status, 201);
  const plan = await planResponse.json();
  assert.equal(planResponse.headers.get('location'), `/api/v1/plans/${plan.id}`);
  assert.ok(!JSON.stringify(plan).includes('private-never-publish'));
  const create = await fetch(`${base}/api/v1/environments`, { method: 'POST', headers: { 'Content-Type': 'application/json', 'Idempotency-Key': 'environment' }, body: JSON.stringify({ plan_id: plan.id }) });
  assert.equal(create.status, 202);
  const complete = await settle(async () => (await fetch(`${base}${create.headers.get('location')}`)).json());
  assert.equal(complete.status, 'succeeded');
  assert.equal(complete.deployment_supported, false);
});

test('one deployment consumes its app-bound plan before CI, and replay never repeats the DB stack', async (t) => {
  const sequence = [];
  const environmentAdapter = {
    plan: async (value, { id }) => ({ public: { ...value, id, executable: true }, private: { profile: { target: { target_id: input.target_id }, deployment: {} } } }),
    verifyPlan: async () => sequence.push('verify'),
    execute: async (plan, { onProgress }) => { sequence.push('environment'); await onProgress({ status: 'running', stage: 'database' }); return { status: 'succeeded', database: { status: 'succeeded' }, deployment_supported: true, runtime_target_id: input.target_id }; },
    deployPublished: async () => { sequence.push('cd'); return deployed; },
  };
  const f = await fixture(t, { environmentAdapter, service: { deploy: async () => { sequence.push('ci'); return { run_id: 123, source_commit: publication.source_commit }; } } });
  const plan = await f.product.createPlan({ name: input.app, runtime: {}, database: { mode: 'patroni' } });
  await assert.rejects(f.product.createDeployment({ ...input, app: 'wrong-app', plan_id: plan.id }, 'wrong'), { code: 'INVALID_INPUT' });
  const request = { ...input, plan_id: plan.id };
  const created = await f.product.createDeployment(request, 'combined');
  const result = await settle(() => f.product.getDeployment(created.id));
  assert.equal(result.status, 'succeeded'); assert.equal(result.environment.database.status, 'succeeded');
  assert.deepEqual(sequence, ['verify', 'environment', 'ci', 'cd']);
  await f.product.createDeployment(request, 'combined');
  assert.deepEqual(sequence, ['verify', 'environment', 'ci', 'cd']);
  await assert.rejects(f.product.createDeployment(request, 'different'), { code: 'CONFLICT' });
});

test('failed database environment never dispatches CI or CD', async (t) => {
  const f = await fixture(t, { environmentAdapter: {
    plan: async (value, { id }) => ({ public: { ...value, id }, private: { profile: { target: { target_id: input.target_id }, deployment: {} } } }),
    verifyPlan: async () => {}, deployPublished: async () => assert.fail('must not deploy'),
    execute: async () => ({ status: 'unknown', database: { status: 'unknown' }, deployment_supported: false, error: { code: 'DATABASE_OUTCOME_UNKNOWN', outcome_unknown: true } }),
  } });
  const plan = await f.product.createPlan({ name: input.app });
  const created = await f.product.createDeployment({ ...input, plan_id: plan.id }, 'db-failed');
  const result = await settle(() => f.product.getDeployment(created.id));
  assert.equal(result.status, 'unknown'); assert.equal(f.dispatches(), 0); assert.equal(f.cdCalls(), 0);
});

for (const origin of ['environments', 'deployments']) {
  test(`successful ${origin} registration reuses the app-bound environment through restart`, async (t) => {
    const request = { ...input, app: 'another-app', target_id: 'request-aws-123' };
    let executions = 0, verifications = 0, dispatches = 0, registrations = 0, registrationComplete = false, unknownCd = false;
    const cdEnvironments = [];
    const environmentAdapter = {
      plan: async (value, { id }) => ({ public: { ...value, id, runtime_target_id: request.target_id },
        private: { profile: { create_per_request: true, target: { target_id: request.target_id }, deployment: {} } } }),
      verifyPlan: async () => { verifications++; },
      execute: async () => {
        executions++; registrationComplete = true;
        return { status: 'succeeded', deployment_supported: true, runtime_target_id: request.target_id, database: { status: 'succeeded' } };
      },
      deployPublished: async (id) => {
        cdEnvironments.push(id);
        return unknownCd ? { cd: { state: 'unknown', deployed: false }, public_http: { state: 'not_run' }, error: { outcome_unknown: true } } : deployed;
      },
    };
    function ciService() {
      const targetIds = ['demo'];
      return { targetId: 'demo', targetIds,
        allowTarget: (id) => { assert.equal(registrationComplete, true); registrations++; if (!targetIds.includes(id)) targetIds.push(id); },
        deploy: async ({ app, target_id }) => {
          assert.ok(targetIds.includes(target_id)); assert.equal(app, request.app); dispatches++;
          return { run_id: 122 + dispatches, source_commit: publication.source_commit };
        },
        status: async (id, target_id) => {
          assert.ok(targetIds.includes(target_id));
          return { state: 'published', publication: { ...publication, run_id: Number(id), app: request.app, target_id } };
        },
      };
    }
    const f = await fixture(t, { deployPublished: undefined, environmentAdapter, service: ciService() });
    assert.equal(f.product.targets().some(({ id }) => id === request.target_id), false);
    await assert.rejects(f.product.createDeployment(request, 'unregistered'), { code: 'INVALID_INPUT' });
    const plan = await f.product.createPlan({ name: request.app });
    const first = origin === 'environments'
      ? await f.product.createEnvironment({ plan_id: plan.id }, 'environment')
      : await f.product.createDeployment({ ...request, plan_id: plan.id }, 'initial');
    const ready = await settle(() => origin === 'environments' ? f.product.getEnvironment(first.id) : f.product.getDeployment(first.id));
    assert.equal(ready.status, 'succeeded');
    const environmentId = origin === 'environments' ? first.id : first.environment_id;
    const registered = f.product.targets().find(({ id }) => id === request.target_id);
    assert.equal(registered.application_name, request.app);
    assert.deepEqual(registered.capabilities, { ci_submission: true, application_deployment: true, database_configuration: true });
    await assert.rejects(f.product.createDeployment({ ...request, app: 'wrong-app' }, 'wrong-app'), { code: 'INVALID_INPUT' });
    await assert.rejects(f.product.createBuild({ ...request, app: 'wrong-app' }), { code: 'INVALID_INPUT' });
    await assert.rejects(f.product.createDeployment({ ...request, target_id: 'unknown-target' }, 'unknown'), { code: 'INVALID_INPUT' });
    const reused = await f.product.createDeployment(request, 'reuse');
    assert.equal(reused.environment_id, environmentId);
    assert.equal(Object.hasOwn(reused, 'environment'), false); assert.equal(Object.hasOwn(reused, 'plan_id'), false);
    const reusedComplete = await settle(() => f.product.getDeployment(reused.id));
    assert.equal(reusedComplete.status, 'succeeded');
    assert.equal((await f.product.createDeployment(request, 'reuse')).id, reused.id);
    assert.equal(executions, 1); assert.equal(verifications, 1); assert.equal(registrations, 1);
    await f.product.close();

    const restartedService = ciService();
    const restarted = await createProductService({ service: restartedService, directory: f.directory, environmentAdapter, pollInterval: 5 });
    try {
      assert.deepEqual(restartedService.targetIds, ['demo', request.target_id]);
      assert.equal(restarted.targets().find(({ id }) => id === request.target_id).capabilities.database_configuration, true);
      assert.equal((await restarted.getBuild(reusedComplete.ci.run_id)).status, 'published');
      assert.equal((await restarted.legacyStatus(reusedComplete.ci.run_id)).state, 'published');
      const afterRestart = await restarted.createDeployment(request, 'after-restart');
      assert.equal(afterRestart.environment_id, environmentId);
      assert.equal((await settle(() => restarted.getDeployment(afterRestart.id))).status, 'succeeded');
      assert.equal(executions, 1); assert.equal(verifications, 1);
      assert.ok(cdEnvironments.length >= 2 && cdEnvironments.every((id) => id === environmentId));
      unknownCd = true;
      const uncertain = await restarted.createDeployment(request, 'uncertain');
      assert.equal((await settle(() => restarted.getDeployment(uncertain.id))).status, 'unknown');
      const dispatchesBeforeRetry = dispatches;
      assert.equal((await restarted.createDeployment(request, 'uncertain')).id, uncertain.id);
      await assert.rejects(restarted.createDeployment(request, 'new-after-unknown'), { code: 'EXECUTOR_BUSY' });
      assert.equal(dispatches, dispatchesBeforeRetry); assert.equal(executions, 1);
    } finally { await restarted.close(); }
  });
}

test('dynamic target admission requires the exact private and public plan binding before provisioning', async (t) => {
  for (const mismatch of ['private-target', 'public-target', 'not-per-request', 'invalid-id']) {
    const targetId = mismatch === 'invalid-id' ? '../invalid' : 'request-aws-new';
    const f = await fixture(t, { deployPublished: undefined, service: { allowTarget: () => assert.fail('No registration') }, environmentAdapter: {
      plan: async (value, { id }) => ({ public: { ...value, id, runtime_target_id: mismatch === 'public-target' ? 'different-target' : targetId },
        private: { profile: { create_per_request: mismatch !== 'not-per-request',
          target: { target_id: mismatch === 'private-target' ? 'different-target' : targetId }, deployment: {} } } }),
      verifyPlan: async () => assert.fail('Invalid binding must fail before verification'),
      execute: async () => assert.fail('No provisioning'), deployPublished: async () => assert.fail('No deployment'),
    } });
    const plan = await f.product.createPlan({ name: input.app });
    await assert.rejects(f.product.createDeployment({ ...input, target_id: targetId, plan_id: plan.id }, 'invalid'), { code: 'INVALID_INPUT' });
    assert.equal(f.dispatches(), 0);
  }
});

test('incomplete or mismatched environment registration never admits the dynamic CI target', async (t) => {
  for (const patch of [{ status: 'unknown' }, { status: 'failed' }, { deployment_supported: false }, { runtime_target_id: 'wrong-target' }]) {
    const targetId = 'request-aws-new';
    const f = await fixture(t, { deployPublished: undefined, service: { allowTarget: () => assert.fail('No registration') }, environmentAdapter: {
      plan: async (value, { id }) => ({ public: { ...value, id, runtime_target_id: targetId },
        private: { profile: { create_per_request: true, target: { target_id: targetId }, deployment: {} } } }),
      verifyPlan: async () => {},
      execute: async () => ({ status: 'succeeded', deployment_supported: true, runtime_target_id: targetId, ...patch }),
      deployPublished: async () => assert.fail('No deployment'),
    } });
    const plan = await f.product.createPlan({ name: input.app });
    const created = await f.product.createDeployment({ ...input, target_id: targetId, plan_id: plan.id }, 'incomplete');
    const completed = await settle(() => f.product.getDeployment(created.id));
    assert.notEqual(completed.status, 'succeeded'); assert.equal(f.dispatches(), 0);
    assert.equal(f.product.targets().some(({ id }) => id === targetId || id === 'wrong-target'), false);
  }
});

test('original UI environment selection is resolved server-side and never falls back to a different provider', async (t) => {
  const submitted = [];
  const deployPublished = async () => deployed;
  deployPublished.targets = { demo: { applicationName: 'demo-app' } };
  const { base } = await httpFixture(t, { target: { provider: 'aws' }, deployPublished,
    service: { targetId: 'demo', deploy: async (value) => { submitted.push(value); return { run_id: '123', source_commit: publication.source_commit }; },
      status: async () => ({ run_id: 123, state: 'published', publication }) } });
  const options = await (await fetch(`${base}/api/v1/deployment-options`)).json();
  assert.deepEqual(options.items.map(({ provider, available }) => [provider, available]), [['aws', true], ['openstack', false], ['proxmox', false]]);
  const selection = () => { const value = form(); value.delete('app'); value.delete('target_id'); value.set('environment', 'cloud'); value.set('provider', 'aws'); value.set('source_name', 'different-source'); return value; };
  for (const [mutate, status] of [
    [(value) => { value.set('environment', 'onprem'); value.set('provider', 'openstack'); }, 409],
    [(value) => { value.set('environment', 'onprem'); value.set('provider', 'proxmox'); }, 409],
    [(value) => value.set('provider', 'gcp'), 422],
    [(value) => value.set('target_id', 'foreign'), 422],
    [(value) => value.set('app', 'foreign-app'), 422],
    [(value) => value.set('plan_id', 'foreign-plan'), 422],
    [(value) => value.append('provider', 'proxmox'), 422],
    [(value) => value.set('source_name', 'x'.repeat(256)), 422],
  ]) {
    const body = selection(); mutate(body);
    const response = await fetch(`${base}/api/v1/deployments`, { method: 'POST', body, headers: { 'Idempotency-Key': 'selection' } });
    assert.equal(response.status, status, await response.text());
  }
  assert.equal(submitted.length, 0);
  const post = () => fetch(`${base}/api/v1/deployments`, { method: 'POST', body: selection(), headers: { 'Idempotency-Key': 'selection' } });
  const accepted = await post(); assert.equal(accepted.status, 202);
  const completed = await settle(async () => (await fetch(`${base}${accepted.headers.get('location')}`)).json());
  assert.equal(completed.status, 'succeeded'); assert.equal(completed.app, 'demo-app'); assert.equal(completed.target_id, 'demo');
  assert.equal((await post()).status, 200); assert.equal(submitted.length, 1);
  assert.equal(submitted[0].app, 'demo-app'); assert.equal(submitted[0].target_id, 'demo');
  const unregistered = await fixture(t);
  assert.ok(unregistered.product.deploymentOptions().every((option) => !option.available), 'unknown provider never becomes AWS');
  const onprem = await fixture(t, { target: { provider: 'openstack' } });
  assert.deepEqual(onprem.product.deploymentOptions().filter((option) => option.available).map((option) => option.provider), ['openstack']);
});

test('HTTP deployment accepts a dynamic app-bound plan alongside existing environment options and replays once', async (t) => {
  const app = 'fresh-app', targetId = 'request-aws-new', targetIds = ['demo'];
  const sequence = [];
  const deployPublished = async () => assert.fail('Dynamic plan uses its environment CD binding');
  deployPublished.targets = { demo: { applicationName: 'demo-app' } };
  const environmentAdapter = {
    plan: async (value, { id }) => ({ public: { ...value, id, runtime_target_id: targetId, executable: true },
      private: { profile: { create_per_request: true, target: { target_id: targetId }, deployment: {} } } }),
    verifyPlan: async () => sequence.push('verify'),
    execute: async () => { sequence.push('environment'); return { status: 'succeeded', deployment_supported: true,
      runtime_target_id: targetId, database: { status: 'succeeded' } }; },
    deployPublished: async () => { sequence.push('cd'); return deployed; },
  };
  const { base } = await httpFixture(t, { target: { provider: 'aws' }, deployPublished, environmentAdapter,
    service: { targetId: 'demo', targetIds, allowTarget: (id) => targetIds.push(id),
      deploy: async (value) => { assert.equal(value.app, app); assert.equal(value.target_id, targetId);
        assert.ok(targetIds.includes(targetId)); sequence.push('ci'); return { run_id: 123, source_commit: publication.source_commit }; },
      status: async () => ({ state: 'published', publication: { ...publication, app, target_id: targetId } }) } });
  const options = await (await fetch(`${base}/api/v1/deployment-options`)).json();
  assert.equal(options.items.find(({ provider }) => provider === 'aws').available, true);
  const planned = await fetch(`${base}/api/v1/plans`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: app }) });
  assert.equal(planned.status, 201);
  const plan = await planned.json();
  const source = () => { const value = form(); value.set('app', plan.name); value.set('target_id', plan.runtime_target_id); value.set('plan_id', plan.id); return value; };
  assert.equal((await fetch(`${base}/api/v1/builds`, { method: 'POST', body: source() })).status, 422);
  assert.deepEqual(sequence, []);
  const post = () => fetch(`${base}/api/v1/deployments`, { method: 'POST', body: source(), headers: { 'Idempotency-Key': 'dynamic-plan' } });
  const accepted = await post(); assert.equal(accepted.status, 202);
  const complete = await settle(async () => (await fetch(`${base}${accepted.headers.get('location')}`)).json());
  assert.equal(complete.status, 'succeeded'); assert.equal(complete.app, app); assert.equal(complete.target_id, targetId);
  assert.equal(complete.plan_id, plan.id); assert.equal(complete.environment.database.status, 'succeeded');
  assert.deepEqual(sequence, ['verify', 'environment', 'ci', 'cd']);
  const replay = await post(); assert.equal(replay.status, 200); assert.equal((await replay.json()).id, complete.id);
  assert.deepEqual(sequence, ['verify', 'environment', 'ci', 'cd']);
});
