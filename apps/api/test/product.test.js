import test from 'node:test';
import { DatabaseSync } from 'node:sqlite';
import assert from 'node:assert/strict';
import { mkdtemp, readFile, rm, stat, chmod, symlink, writeFile, readdir, mkdir } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { setTimeout as pause } from 'node:timers/promises';
import { createProductService } from '../src/product.js';
import { createProductStore } from '../src/product-store.js';
import { createAppServer } from '../src/server.js';

function diskState(directory) {
  const db = new DatabaseSync(join(directory, 'dashboard.sqlite3'), { readOnly: true });
  try { return { operations: Object.fromEntries(db.prepare('SELECT id, record FROM operations').all().map((row) => [row.id, JSON.parse(row.record)])) }; } finally { db.close(); }
}

const files = [{ path: 'app.js', content: Buffer.from('hello') }];
const input = { app: 'demo-app', target_id: 'demo', source_type: 'folder', files };
const publication = { run_id: 123, target_id: 'demo', app: 'demo-app', source_commit: 'a'.repeat(40), artifact_id: 456, producer_attempt: 1 };
const deployed = { cd: { state: 'deployed', deployed: true, revision: 'b'.repeat(40) }, public_http: { state: 'succeeded', verified_at: '2026-10-02T12:00:00Z', url: 'https://demo.example.test' } };
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
    const state = diskState(f.directory), operation = Object.values(state.operations)[0];
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
    const disk = diskState(f.directory);
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
test('API reads registrar updates and never falls back to a legacy observer after handoff', async (t) => {
  const directory = await mkdtemp(join(tmpdir(), 'railshot-observer-binding-'));
  const legacy = join(directory, 'observer.json'), live = join(directory, 'product.json');
  const previous = Object.fromEntries(['RAILSHOT_OBSERVER_CONFIG', 'RAILSHOT_OBSERVER_PRODUCT_FILE'].map((key) => [key, process.env[key]]));
  t.after(async () => {
    for (const [key, value] of Object.entries(previous)) {
      if (value === undefined) delete process.env[key]; else process.env[key] = value;
    }
    await rm(directory, { recursive: true, force: true });
  });
  const collector = { id: 'shared-observer', lifecycle: 'shared', expires_at: new Date(Date.now() + 3600000).toISOString() };
  await writeFile(legacy, JSON.stringify({ version: 1, targets: [], collector: { ...collector, id: 'legacy-observer' } }), { mode: 0o600 });
  process.env.RAILSHOT_OBSERVER_CONFIG = legacy;
  process.env.RAILSHOT_OBSERVER_PRODUCT_FILE = live;
  const { base, server } = await httpFixture(t);
  const product = await server.productReady;
  const operation = await product.createDeployment(input, 'observer-binding');
  await settle(() => product.getDeployment(operation.id));
  const read = async () => (await (await fetch(`${base}/api/v1/deployments/${operation.id}`)).json()).observation;
  assert.equal((await read()).metrics.pods.state, 'unavailable', 'missing live state must not restore legacy values');
  await writeFile(live, JSON.stringify({ version: 1, targets: [], collector }), { mode: 0o600 });
  assert.equal((await read()).collector.id, collector.id, 'the same API process reads registrar output');
  await rm(live);
  assert.equal((await read()).metrics.pods.state, 'unavailable');
  delete process.env.RAILSHOT_OBSERVER_PRODUCT_FILE;
  const oldServer = await httpFixture(t);
  const oldProduct = await oldServer.server.productReady;
  const oldOperation = await oldProduct.createDeployment(input, 'legacy-observer');
  await settle(() => oldProduct.getDeployment(oldOperation.id));
  assert.equal((await oldProduct.getDeployment(oldOperation.id)).observation.collector.id, 'legacy-observer', 'existing static configuration remains supported');
});

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

test('unconfigured server persists dashboard state while listing empty execution capabilities', async (t) => {
  const { base, server } = await httpFixture(t, { service: null, deployPublished: undefined });
  for (const path of ['targets', 'profiles']) assert.deepEqual(await (await fetch(`${base}/api/v1/${path}`)).json(), { items: [], next_marker: null });
  assert.ok((await server.productReady).dashboard);
  assert.equal((await (await fetch(`${base}/healthz`)).json()).configured, false, 'dashboard storage alone does not configure an executor');
  const response = await fetch(`${base}/api/v1/sessions`, { method: 'POST' });
  assert.equal(response.status, 201);
  assert.match(response.headers.get('set-cookie'), /railshot_session=/);
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
  const db = new DatabaseSync(join(f.directory, 'dashboard.sqlite3'));
  db.exec("CREATE TRIGGER simulate_disk_failure BEFORE INSERT ON operations BEGIN SELECT RAISE(ABORT, 'write failed'); END");
  db.close();
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
        private: { profile: { provider: 'aws', create_per_request: true, target: { target_id: request.target_id }, deployment: {} } } }),
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
    const owner = f.product.dashboard.session(null).id, stranger = f.product.dashboard.session(null).id;
    assert.equal(f.product.targets().some(({ id }) => id === request.target_id), false);
    await assert.rejects(f.product.createDeployment(request, 'unregistered'), { code: 'INVALID_INPUT' });
    const plan = await f.product.createPlan({ name: request.app }, owner);
    const first = origin === 'environments'
      ? await f.product.createEnvironment({ plan_id: plan.id }, 'environment', owner)
      : await f.product.createDeployment({ ...request, plan_id: plan.id }, 'initial', undefined, owner);
    const ready = await settle(() => origin === 'environments' ? f.product.getEnvironment(first.id) : f.product.getDeployment(first.id));
    assert.equal(ready.status, 'succeeded');
    assert.ok(f.product.targets(owner).some(({ id }) => id === request.target_id));
    assert.ok(!f.product.targets(stranger).some(({ id }) => id === request.target_id));
    await assert.rejects(f.product.createDeployment(request, 'foreign', undefined, stranger), { code: 'INVALID_INPUT' });
    await assert.rejects(f.product.createBuild(request, undefined, stranger), { code: 'INVALID_INPUT' });
    const environmentId = origin === 'environments' ? first.id : first.environment_id;
    const registered = f.product.targets().find(({ id }) => id === request.target_id);
    assert.equal(registered.application_name, request.app);
    assert.equal(registered.provider, 'aws'); assert.equal(registered.environment_id, environmentId);
    const observation = await f.product.getTargetObservation(request.target_id, owner);
    assert.equal(observation.environment_id, environmentId); assert.equal(observation.app, request.app);
    assert.equal(observation.metrics.node_up.state, 'not_configured');
    await assert.rejects(f.product.getTargetObservation(request.target_id, stranger), { status: 404 });
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
      assert.ok(restarted.targets(owner).some(({ id }) => id === request.target_id));
      assert.ok(!restarted.targets(stranger).some(({ id }) => id === request.target_id));
      await assert.rejects(restarted.getTargetObservation(request.target_id, stranger), { status: 404 });
      assert.equal((await restarted.getTargetObservation(request.target_id, owner)).environment_id, environmentId);
      await assert.rejects(restarted.createDeployment(request, 'foreign-restart', undefined, stranger), { code: 'INVALID_INPUT' });
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

test('UI environment selection preserves the source app identity and rejects missing or invalid names before execution', async (t) => {
  const contract = JSON.parse(await readFile(new URL('../../../docs/api/product.openapi.json', import.meta.url)));
  for (const route of Object.keys(contract.paths)) {
    assert.match(route, /^\/api\/v1(?:\/(?:[a-z]+|\{[a-z]+\}))+$/, `Nonconforming product route: ${route}`);
  }
  const submitted = [], deliveries = [];
  const deployPublished = async (value) => { deliveries.push(value); return deployed; };
  deployPublished.targets = { demo: { applicationName: 'calculator' } };
  const { base } = await httpFixture(t, { target: { provider: 'aws' }, deployPublished,
    service: { targetId: 'demo', deploy: async (value) => { submitted.push(value); return { run_id: '123', source_commit: publication.source_commit }; },
      status: async () => ({ run_id: 123, state: 'published', publication: { ...publication, app: 'calculator' } }) } });
  const options = await (await fetch(`${base}/api/v1/options`)).json();
  assert.deepEqual(options.items.map(({ provider, available }) => [provider, available]), [['aws', true], ['gcp', false], ['openstack', false], ['proxmox', false]]);
  const selection = () => { const value = form(); value.delete('app'); value.delete('target_id'); value.set('environment', 'cloud'); value.set('provider', 'aws'); value.set('source_name', 'Calculator'); return value; };
  for (const [mutate, status] of [
    [(value) => { value.set('environment', 'onprem'); value.set('provider', 'openstack'); }, 409],
    [(value) => { value.set('environment', 'onprem'); value.set('provider', 'proxmox'); }, 409],
    [(value) => value.set('provider', 'gcp'), 409],
    [(value) => { value.set('environment', 'onprem'); value.set('provider', 'gcp'); }, 422],
    [(value) => value.set('target_id', 'foreign'), 422],
    [(value) => value.set('app', 'foreign-app'), 422],
    [(value) => value.set('plan_id', 'foreign-plan'), 422],
    [(value) => value.append('provider', 'proxmox'), 422],
    [(value) => value.set('source_name', 'x'.repeat(256)), 422],
    [(value) => value.set('source_name', 'different-source'), 422],
    [(value) => value.delete('source_name'), 422],
    ...['', ' ', '한글', 'ab', '123calculator'].map((name) => [(value) => value.set('source_name', name), 422]),
  ]) {
    const body = selection(); mutate(body);
    const response = await fetch(`${base}/api/v1/deployments`, { method: 'POST', body, headers: { 'Idempotency-Key': 'selection' } });
    assert.equal(response.status, status, await response.text());
  }
  assert.equal(submitted.length, 0);
  assert.equal(deliveries.length, 0);
  const post = () => fetch(`${base}/api/v1/deployments`, { method: 'POST', body: selection(), headers: { 'Idempotency-Key': 'selection' } });
  const accepted = await post(); assert.equal(accepted.status, 202);
  const completed = await settle(async () => (await fetch(`${base}${accepted.headers.get('location')}`)).json());
  assert.equal(completed.status, 'succeeded'); assert.equal(completed.app, 'calculator'); assert.equal(completed.target_id, 'demo');
  assert.equal((await post()).status, 200); assert.equal(submitted.length, 1);
  assert.equal(submitted[0].app, 'calculator'); assert.equal(submitted[0].target_id, 'demo');
  assert.equal(deliveries.length, 1); assert.equal(deliveries[0].app, 'calculator');
  const unregistered = await fixture(t);
  assert.ok(unregistered.product.deploymentOptions().every((option) => !option.available), 'unknown provider never becomes AWS');
  const onprem = await fixture(t, { target: { provider: 'openstack' } });
  assert.deepEqual(onprem.product.deploymentOptions().filter((option) => option.available).map((option) => option.provider), ['openstack']);
});

test('HTTP provider selection rejects other source apps before fetching or dispatching and preserves registered app updates', async (t) => {
  const previous = process.env.RAILSHOT_PROVIDER_TARGETS;
  process.env.RAILSHOT_PROVIDER_TARGETS = JSON.stringify({ gcp: 'stack-gcp', openstack: 'stack-openstack' });
  t.after(() => { if (previous === undefined) delete process.env.RAILSHOT_PROVIDER_TARGETS; else process.env.RAILSHOT_PROVIDER_TARGETS = previous; });
  const submissions = [], deliveries = [], fetched = [], runs = new Map();
  const deployPublished = async ({ app, targetId }) => {
    deliveries.push({ app, targetId });
    return { ...deployed, public_http: { ...deployed.public_http, url: `https://${targetId}.example.test` } };
  };
  deployPublished.targets = { demo: { applicationName: 'demo-app' }, 'stack-gcp': { applicationName: 'gcp-app' }, 'stack-openstack': { applicationName: 'openstack-app' } };
  const { base } = await httpFixture(t, { target: { provider: 'aws' }, deployPublished,
    sourceLoader: async (url) => { fetched.push(url); return { files }; },
    service: { targetId: 'demo', targetIds: ['demo', 'stack-gcp', 'stack-openstack'],
      deploy: async (value) => {
        submissions.push({ app: value.app, targetId: value.target_id });
        const runId = String(123 + runs.size);
        runs.set(runId, { ...publication, run_id: runId, app: value.app, target_id: value.target_id });
        return { run_id: runId, source_commit: publication.source_commit };
      },
      status: async (runId, targetId) => {
        assert.equal(runs.get(runId).target_id, targetId);
        return { state: 'published', publication: runs.get(runId) };
      } } });
  const options = await (await fetch(`${base}/api/v1/options`)).json();
  assert.deepEqual(options.items.map(({ provider, available }) => [provider, available]), [['aws', true], ['gcp', true], ['openstack', true], ['proxmox', false]]);
  const targets = await (await fetch(`${base}/api/v1/targets`)).json();
  assert.deepEqual(targets.items.map(({ id, provider }) => [id, provider]), [['demo', 'aws'], ['stack-gcp', 'gcp'], ['stack-openstack', 'openstack']]);
  for (const [provider, environment, id, app] of [['aws', 'cloud', 'demo', 'demo-app'], ['gcp', 'cloud', 'stack-gcp', 'gcp-app'], ['openstack', 'onprem', 'stack-openstack', 'openstack-app']]) {
    const before = [submissions.length, deliveries.length];
    for (const sourceType of ['folder', 'github']) {
      const body = form(); body.delete('app'); body.delete('target_id');
      body.set('provider', provider); body.set('environment', environment);
      if (sourceType === 'github') {
        body.delete('files'); body.delete('paths'); body.set('source_type', 'github');
        body.set('repository_url', 'https://github.com/example/different-source');
      } else body.set('source_name', 'different-source');
      const rejected = await fetch(`${base}/api/v1/deployments`, { method: 'POST', body, headers: { 'Idempotency-Key': `wrong-${provider}-${sourceType}` } });
      assert.equal(rejected.status, 422, await rejected.text());
      assert.deepEqual([submissions.length, deliveries.length], before);
      assert.equal(fetched.length, 0);
    }
    const post = () => {
      const body = form(); body.delete('app'); body.delete('target_id');
      body.set('provider', provider); body.set('environment', environment); body.set('source_name', app.toUpperCase());
      return fetch(`${base}/api/v1/deployments`, { method: 'POST', body, headers: { 'Idempotency-Key': `multi-${provider}` } });
    };
    const accepted = await post(); assert.equal(accepted.status, 202, await accepted.text());
    const completed = await settle(async () => (await fetch(`${base}${accepted.headers.get('location')}`)).json());
    assert.equal(completed.status, 'succeeded'); assert.equal(completed.target_id, id); assert.equal(completed.app, app);
    assert.equal(completed.url, `https://${id}.example.test`);
    const replay = await post(); assert.equal(replay.status, 200); assert.equal((await replay.json()).id, completed.id);
  }
  assert.deepEqual(submissions, [{ app: 'demo-app', targetId: 'demo' }, { app: 'gcp-app', targetId: 'stack-gcp' }, { app: 'openstack-app', targetId: 'stack-openstack' }]);
  assert.deepEqual(deliveries, submissions);
});

test('provider metadata never admits an additional target without both CI permission and a CD registration', async (t) => {
  for (const [name, permitted, registered] of [['missing-ci', false, true], ['missing-cd', true, false], ['legacy-adapter', true, null]]) {
    await t.test(name, async (t) => {
      const deployPublished = async () => assert.fail('Unavailable selection must not invoke CD');
      if (registered !== null) deployPublished.targets = { demo: { applicationName: 'demo-app' },
        ...(registered ? { 'stack-openstack': { applicationName: 'openstack-app' } } : {}) };
      const f = await fixture(t, { target: { provider: 'aws' }, providerTargets: { openstack: 'stack-openstack' }, deployPublished,
        service: { targetIds: permitted ? ['demo', 'stack-openstack'] : ['demo'] } });
      assert.equal(f.product.deploymentOptions().find(({ provider }) => provider === 'aws').available, true);
      assert.equal(f.product.deploymentOptions().find(({ provider }) => provider === 'openstack').available, false);
      await assert.rejects(f.product.createDeployment({ source_type: 'folder', files,
        deployment_selection: { environment: 'onprem', provider: 'openstack' } }, 'unavailable'), { code: 'CAPABILITY_UNAVAILABLE' });
      assert.equal(f.dispatches(), 0);
    });
  }
  const deployPublished = async () => deployed;
  deployPublished.targets = { demo: { applicationName: 'demo-app' } };
  const f = await fixture(t, { target: { provider: 'aws' }, deployPublished, service: { targetIds: [] } });
  assert.ok(f.product.deploymentOptions().every(({ available }) => !available), 'the legacy default also requires CI admission');
});

test('provider target configuration rejects ambiguous identities and cannot replace the existing default', async () => {
  for (const providerTargets of [null, [], 'openstack', { openstack: '../foreign' }, { unknown: 'stack-openstack' },
    { aws: 'replacement' }, { openstack: 'demo' }, { openstack: 'stack-openstack', proxmox: 'stack-openstack' }]) {
    await assert.rejects(createProductService({ service: { targetId: 'demo' }, target: { provider: 'aws' }, providerTargets }), { code: 'INVALID_INPUT' });
  }
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
  const options = await (await fetch(`${base}/api/v1/options`)).json();
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

test('CI diagnostics preserve blocked and unknown outcomes without replaying execution', async (t) => {
  for (const outcome of ['BLOCKED', 'UNKNOWN']) {
    const diagnostics = { state: 'ready', reason: 'NO_TESTS', code: outcome === 'UNKNOWN' ? 'SDK_OUTCOME_UNKNOWN' : 'GATE_CONFIG_INVALID',
      outcome, phase: 'Q.discovery', message: '테스트를 찾지 못했습니다.', guidance: '수정 결과를 확인하세요.' };
    const steps = [{ key: 'loop', status: 'completed', conclusion: 'failure', tasks: [{ number: 2, name: 'Baseline gates, then SDK repair only when eligible', conclusion: 'failure' }] }];
    const f = await fixture(t, { service: { status: async () => ({ run_id: 123, state: 'failed', diagnostics, steps, message: 'CI 실패' }) } });
    const accepted = await f.product.createDeployment(input, 'diagnostic');
    const record = await settle(() => f.product.getDeployment(accepted.id));
    assert.equal(record.status, outcome === 'UNKNOWN' ? 'unknown' : 'blocked');
    assert.equal(record.error.outcome_unknown, outcome === 'UNKNOWN');
    assert.equal(record.error.message, '테스트를 찾지 못했습니다. 수정 결과를 확인하세요.');
    assert.deepEqual(record.ci.diagnostics, diagnostics); assert.deepEqual(record.ci.steps, steps);
    assert.equal(f.dispatches(), 1); assert.equal(f.cdCalls(), 0);
    await f.product.getDeployment(accepted.id);
    assert.equal(f.dispatches(), 1);
    if (outcome === 'UNKNOWN') await assert.rejects(f.product.createDeployment(input, 'another'), { code: 'EXECUTOR_BUSY' });
  }
});

test('app logs enforce session ownership, deployed state and latest shared-app ownership', async (t) => {
  let logReads = 0, run = 122;
  const f = await fixture(t, { observeLogs: async (record) => { logReads++; return { deployment_id: record.id, target_id: record.target_id,
    app: record.app, state: 'ready', checked_at: new Date().toISOString(), entries: [{ pod: 'demo-pod', container: 'app', text: 'GET /health 200' }] }; },
    service: { deploy: async () => ({ run_id: ++run, source_commit: publication.source_commit }),
      status: async (id) => ({ run_id: id, state: 'published', publication: { ...publication, run_id: id } }) } });
  const owner = f.product.dashboard.session().id, stranger = f.product.dashboard.session().id;
  const first = await f.product.createDeployment(input, 'first', undefined, owner);
  assert.equal((await f.product.getDeploymentLogs(first.id, owner)).state, 'not_deployed');
  await settle(() => f.product.getDeployment(first.id, owner));
  assert.equal((await f.product.getDeploymentLogs(first.id, owner)).state, 'ready');
  await assert.rejects(f.product.getDeploymentLogs(first.id, stranger), { status: 404 });
  assert.equal(logReads, 1);
  const second = await f.product.createDeployment(input, 'second', undefined, stranger);
  await settle(() => f.product.getDeployment(second.id, stranger));
  assert.equal((await f.product.getDeploymentLogs(first.id, owner)).state, 'superseded');
  assert.equal(logReads, 1, 'superseded session never reads the new deployment logs');
  assert.equal((await f.product.getDeploymentLogs(second.id, stranger)).state, 'ready');
});

test('deployment logs route rejects mutation and caller-provided query controls', async (t) => {
  const { base } = await httpFixture(t);
  const accepted = await fetch(`${base}/api/v1/deployments`, { method: 'POST', body: form(), headers: { 'Idempotency-Key': 'log-route' } });
  const path = accepted.headers.get('location');
  await settle(async () => (await fetch(base + path)).json());
  const response = await fetch(base + path + '/logs');
  assert.equal(response.status, 200);
  assert.equal((await response.json()).state, 'not_configured');
  assert.equal((await fetch(base + path + '/logs?pod=foreign')).status, 422);
  assert.equal((await fetch(base + path + '/logs', { method: 'POST' })).status, 405);
  assert.equal((await fetch(base + '/api/v1/deployments/foreign/logs')).status, 404);
});

test('app logs discard an in-flight read when another session admits CD for the same app', async (t) => {
  let releaseLogs, releaseCd, beganLogs, beganCd, calls = 0, run = 122;
  const waitingLogs = new Promise((resolve) => { releaseLogs = resolve; });
  const waitingCd = new Promise((resolve) => { releaseCd = resolve; });
  const enteredLogs = new Promise((resolve) => { beganLogs = resolve; });
  const enteredCd = new Promise((resolve) => { beganCd = resolve; });
  const f = await fixture(t, { observeLogs: async (record) => {
    beganLogs(); await waitingLogs;
    return { deployment_id: record.id, state: 'ready', entries: [{ pod: 'shared-pod', container: 'app', text: 'new session output' }] };
  }, deployPublished: async () => {
    if (++calls === 2) { beganCd(); await waitingCd; }
    return deployed; // Reusing the same image/revision must still revoke the old session's log read.
  }, service: { deploy: async () => ({ run_id: ++run, source_commit: publication.source_commit }),
    status: async (id) => ({ run_id: id, state: 'published', publication: { ...publication, run_id: id } }) } });
  const owner = f.product.dashboard.session().id, other = f.product.dashboard.session().id;
  try {
    const first = await f.product.createDeployment(input, 'first-race', undefined, owner);
    await settle(() => f.product.getDeployment(first.id, owner));
    const logs = f.product.getDeploymentLogs(first.id, owner);
    await enteredLogs;
    const second = await f.product.createDeployment(input, 'second-race', undefined, other);
    await enteredCd;
    releaseLogs();
    const result = await logs;
    assert.equal(result.state, 'superseded'); assert.deepEqual(result.entries, []);
    assert.equal((await f.product.getDeploymentLogs(first.id, owner)).state, 'superseded');
    releaseCd();
    await settle(() => f.product.getDeployment(second.id, other));
  } finally { releaseLogs(); releaseCd(); }
});

test('agent events are not fabricated before dispatch and require the persisted run binding after restart', async (t) => {
  let release, reads = 0;
  const waiting = new Promise((resolve) => { release = resolve; });
  const f = await fixture(t, { service: {
    deploy: async () => { await waiting; return { run_id: 123, source_commit: publication.source_commit }; },
    events: async () => { reads++; return { state: 'live', items: [] }; },
  } });
  const owner = f.product.dashboard.session().id;
  const created = await f.product.createDeployment(input, 'event-binding', undefined, owner);
  try {
    const pending = await f.product.getDeploymentEvents(created.id, owner);
    assert.equal(pending.state, 'not_started'); assert.equal(pending.reason, 'not_dispatched');
    assert.deepEqual(pending.items, []); assert.equal(reads, 0);
  } finally { release(); }
  await settle(() => f.product.getDeployment(created.id, owner));
  await f.product.close();
  const store = await createProductStore(f.directory);
  await store.transaction((state) => { state.bindings['123'].source_commit = 'd'.repeat(40); });
  await store.close();
  const restarted = await createProductService({ service: f.service, directory: f.directory });
  try {
    const rejected = await restarted.getDeploymentEvents(created.id, owner);
    assert.equal(rejected.state, 'unavailable'); assert.equal(rejected.reason, 'binding_mismatch');
    assert.equal(reads, 0);
    await assert.rejects(restarted.getDeploymentEvents(created.id, 'foreign'), { status: 404 });
  } finally { await restarted.close(); }
});

test('HTTP target observations authorize IDs before collecting and need no deployment history', async (t) => {
  const calls = [];
  const deployPublished = async () => assert.fail('read-only observation dispatched CD');
  deployPublished.targets = { demo: { applicationName: 'demo-app' }, 'stack-gcp': { applicationName: 'gcp-app' }, 'stack-openstack': { applicationName: 'openstack-app' } };
  const { base } = await httpFixture(t, { target: { provider: 'aws' }, providerTargets: { gcp: 'stack-gcp', openstack: 'stack-openstack' }, deployPublished,
    service: { targetId: 'demo', targetIds: ['demo', 'stack-gcp', 'stack-openstack'], deploy: async () => assert.fail('read-only observation dispatched CI') },
    observeMetrics: async (record) => {
      calls.push(record);
      return { deployment_id: null, target_id: record.target_id, app: record.app, checked_at: new Date().toISOString(), stale_after_seconds: 90,
        metrics: {
          node_up: { state: 'collection_failed', value: null, observed_at: '2026-10-03T00:00:00.000Z', scope: 'target_node' },
          runtime_healthz: { state: record.target_id === 'stack-openstack' ? 'stale' : 'ready', value: record.target_id === 'stack-gcp' ? 0 : record.target_id === 'stack-openstack' ? null : 1, observed_at: '2026-10-03T00:00:00.000Z', scope: 'target_runtime' },
        } };
    } });
  for (const [id, app] of [['demo', 'demo-app'], ['stack-gcp', 'gcp-app'], ['stack-openstack', 'openstack-app']]) {
    const response = await fetch(`${base}/api/v1/targets/${id}/observations`), body = await response.json();
    assert.equal(response.status, 200); assert.equal(response.headers.get('cache-control'), 'no-store'); assert.ok(response.headers.get('x-request-id'));
    assert.equal(body.target_id, id); assert.equal(body.app, app); assert.equal(body.environment_id, null); assert.equal(body.deployment_id, null);
    assert.deepEqual(body.runtime, { status: id === 'demo' ? 'healthy' : id === 'stack-gcp' ? 'unhealthy' : 'unknown',
      observation_state: body.metrics.runtime_healthz.state, observed_at: body.metrics.runtime_healthz.observed_at });
  }
  for (const id of ['unknown', '__proto__', 'BAD-ID']) assert.equal((await fetch(`${base}/api/v1/targets/${id}/observations`)).status, 404);
  assert.equal((await fetch(`${base}/api/v1/targets/demo/observations?app=another-app`)).status, 422);
  const method = await fetch(`${base}/api/v1/targets/demo/observations`, { method: 'POST' });
  assert.equal(method.status, 405); assert.equal(method.headers.get('allow'), 'GET'); assert.equal(calls.length, 3);
});
