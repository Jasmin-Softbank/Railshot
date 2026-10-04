import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { setTimeout as pause } from 'node:timers/promises';
import { createProductService } from '../src/product.js';
import { SubmissionError, createDeploymentService } from '../src/github.js';
import { createHash } from 'node:crypto';

const files = [{ path: 'app.js', content: Buffer.from('hello') }];
const input = { app: 'demo-app', target_id: 'demo', source_type: 'folder', files };
const publication = { run_id: 123, target_id: 'demo', app: 'demo-app', source_commit: 'a'.repeat(40), artifact_id: 456, producer_attempt: 1 };
const published = { run_id: 123, state: 'published', status: 'completed', conclusion: 'success', publication };
const delivered = { cd: { state: 'deployed', deployed: true, revision: 'b'.repeat(40) }, public_http: { state: 'succeeded', verified_at: new Date().toISOString(), url: 'https://demo.example.test' } };
const deferred = () => { let resolve; const promise = new Promise(r => { resolve = r; }); return { promise, resolve }; };
async function until(read, condition) {
  const deadline = Date.now() + 5000;
  while (Date.now() < deadline) { const row = await read(); if (condition(row)) return row; await pause(5); }
  assert.fail('Observation did not settle');
}
async function fixture(t, overrides = {}) {
  const directory = await mkdtemp(join(tmpdir(), 'railshot-observation-'));
  let dispatches = 0, reads = 0, cdCalls = 0;
  const service = { targetId: 'demo', deploy: async () => { dispatches++; return { run_id: 123, source_commit: publication.source_commit }; },
    status: async () => { reads++; return published; }, ...overrides };
  const options = { directory, pollInterval: 5, service, deployPublished: async () => { cdCalls++; return delivered; } };
  const f = { options, directory, service, counts: () => ({ dispatches, reads, cdCalls }) };
  f.product = await createProductService(options);
  t.after(async () => { await f.product.close(); await rm(directory, { recursive: true, force: true }); });
  return f;
}

test('temporary CI read failure preserves the last observation and recovers without redispatch', async t => {
  let reads = 0, recovered = false;
  const f = await fixture(t, { status: async () => {
    if (++reads === 1) return { state: 'running', status: 'in_progress', conclusion: null };
    if (!recovered) throw new TypeError('private transport details');
    return published;
  } });
  const accepted = await f.product.createDeployment(input, 'read-recovery');
  const stale = await until(() => f.product.getDeployment(accepted.id), row => row.ci.observation?.error);
  assert.equal(stale.status, 'running'); assert.equal(stale.stage, 'ci'); assert.equal(stale.error, null);
  assert.equal(stale.ci.state, 'running'); assert.ok(stale.ci.observation.last_success_at);
  assert.equal(stale.ci.observation.error.code, 'CI_OBSERVATION_UNAVAILABLE');
  assert.ok(stale.ci.observation.next_retry_at); assert.ok(!JSON.stringify(stale).includes('private transport'));
  recovered = true;
  const done = await until(() => f.product.getDeployment(accepted.id), row => row.status === 'succeeded');
  assert.equal(done.ci.observation.error, null); assert.equal(done.ci.workflow.conclusion, 'success');
  assert.equal(f.counts().dispatches, 1); assert.equal(f.counts().cdCalls, 1);
});

test('restart during unavailable observation reattaches the saved run and completes once', async t => {
  const f = await fixture(t, { status: async () => { throw new TypeError('network unavailable'); } });
  const first = await f.product.createDeployment(input, 'restart-read');
  await until(() => f.product.getDeployment(first.id), row => row.ci.observation?.error);
  await f.product.close(); f.service.status = async () => published;
  f.product = await createProductService(f.options);
  const done = await until(() => f.product.getDeployment(first.id), row => row.status === 'succeeded');
  assert.equal(done.ci.run_id, '123'); assert.equal(f.counts().dispatches, 1); assert.equal(f.counts().cdCalls, 1);
});

test('permanent CI read rejection is actionable and never proceeds to CD', async t => {
  const f = await fixture(t, { status: async () => { throw Object.assign(new Error('private'), { retryable: false, upstreamStatus: 401 }); } });
  const accepted = await f.product.createDeployment(input, 'read-rejected');
  const result = await until(() => f.product.getDeployment(accepted.id), row => row.status === 'blocked');
  assert.equal(result.error.code, 'CI_OBSERVATION_REJECTED'); assert.equal(result.error.outcome_unknown, true);
  assert.equal(result.ci.observation.error.upstream_status, 401); assert.equal(result.ci.observation.next_retry_at, null);
  assert.equal(f.counts().cdCalls, 0);
});

test('source materialization does not hold unrelated CI progress in the persistence queue', async t => {
  const ci = deferred(), source = deferred(), entered = deferred();
  const f = await fixture(t, { status: async () => { await ci.promise; return published; } });
  const first = await f.product.createDeployment(input, 'first');
  await until(() => f.product.getDeployment(first.id), row => row.ci.run_id);
  const pending = f.product.createDeployment({ app: 'other-app', target_id: 'demo', source_type: 'github', repository_url: 'https://github.com/example/fixture' }, 'second', async () => {
    entered.resolve(); await source.promise; throw new Error('fixture stops before dispatch');
  }).catch(error => error.message);
  try {
    await entered.promise; ci.resolve();
    const done = await until(() => f.product.getDeployment(first.id), row => row.status === 'succeeded');
    assert.equal(done.ci.state, 'published'); assert.equal(f.counts().cdCalls, 1);
  } finally { ci.resolve(); source.resolve(); await pending; }
});

test('lost dispatch response is found by the durable request source, without a second dispatch', async t => {
  let sends = 0, lookups = 0, prepared;
  const f = await fixture(t, {
    deploy: async ({ operation_id, onPrepared }) => {
      sends++; prepared = { operation_id, source_commit: publication.source_commit, app: input.app, target_id: input.target_id };
      await onPrepared({ source_commit: publication.source_commit });
      throw new SubmissionError('ci_dispatch', new TypeError('lost response'));
    },
    findDeployment: async binding => { lookups++; assert.deepEqual(binding, prepared); return lookups === 1 ? null : { run_id: 123, source_commit: publication.source_commit }; },
  });
  const first = await f.product.createDeployment(input, 'lost-ack');
  const done = await until(() => f.product.getDeployment(first.id), row => row.status === 'succeeded');
  assert.ok(done.dispatch.prepared_at); assert.equal(done.ci.run_id, '123'); assert.equal(sends, 1);
  assert.equal(lookups, 2); assert.equal(f.counts().cdCalls, 1);
  assert.equal((await f.product.createDeployment(input, 'lost-ack')).id, first.id);
  assert.equal(sends, 1);
});

test('restart while locating a lost dispatch continues lookup without resubmission', async t => {
  let sends = 0;
  const f = await fixture(t, {
    deploy: async ({ onPrepared }) => { sends++; await onPrepared({ source_commit: publication.source_commit }); throw new SubmissionError('ci_dispatch', new TypeError('lost')); },
    findDeployment: async () => null,
  });
  const accepted = await f.product.createDeployment(input, 'restart-dispatch');
  await until(() => f.product.getDeployment(accepted.id), row => row.ci.observation?.error?.code === 'CI_DISPATCH_PENDING');
  await f.product.close(); f.service.findDeployment = async () => ({ run_id: 123, source_commit: publication.source_commit });
  f.product = await createProductService(f.options);
  await until(() => f.product.getDeployment(accepted.id), row => row.status === 'succeeded');
  assert.equal(sends, 1); assert.equal(f.counts().cdCalls, 1);
});

test('unchanged source receives a request commit before dispatch and lookup rejects ambiguous identities', async () => {
  const operation_id = '11111111-1111-4111-8111-111111111111', source_commit = 'b'.repeat(40);
  const blob = createHash('sha1').update(`blob ${files[0].content.length}\0`).update(files[0].content).digest('hex');
  let commit, prepared = false, dispatches = 0, mode = 'match';
  const service = createDeploymentService({ token: 'test', owner: 'org', repo: 'apps', targetId: 'demo' }, async (url, options = {}) => {
    const path = new URL(url).pathname;
    if (path.endsWith('/git/ref/heads/main')) return Response.json({ object: { sha: 'a'.repeat(40) } });
    if (path.endsWith(`/git/commits/${'a'.repeat(40)}`)) return Response.json({ tree: { sha: 'base' } });
    if (path.endsWith('/git/trees/base')) return Response.json({ tree: [{ path: 'apps', type: 'tree', sha: 'apps' }] });
    if (path.endsWith('/git/trees/apps')) return Response.json({ tree: [{ path: 'demo', type: 'tree', sha: 'tenant' }] });
    if (path.endsWith('/git/trees/tenant')) return Response.json({ tree: [{ path: 'demo-app', type: 'tree', sha: 'app' }] });
    if (path.endsWith('/git/trees/app')) return Response.json({ tree: [{ path: 'app.js', type: 'blob', mode: '100644', sha: blob }] });
    if (path.endsWith('/git/commits')) { commit = JSON.parse(options.body); assert.equal(commit.tree, 'base'); return Response.json({ sha: source_commit }); }
    if (path.endsWith('/git/refs/heads/main')) return Response.json({});
    if (path.endsWith('/dispatches')) { assert.ok(prepared); dispatches++; throw new TypeError('lost acknowledgement'); }
    if (path.endsWith(`/git/commits/${source_commit}`)) return Response.json({ message: commit.message });
    if (path.endsWith('/runs')) {
      const run = { id: 123, head_sha: source_commit, head_branch: 'main', event: 'workflow_dispatch', path: '.github/workflows/railshot-deploy.yml',
        run_attempt: mode === 'rerun' ? 2 : 1, display_title: `railshot:demo/demo-app:${mode === 'foreign' ? 'other' : 'demo'}:${source_commit}` };
      return Response.json({ total_count: mode === 'duplicate' ? 2 : 1, workflow_runs: mode === 'duplicate' ? [run, { ...run, id: 124 }] : [run] });
    }
    assert.fail(`Unexpected GitHub path ${path}`);
  });
  await assert.rejects(service.deploy({ ...input, operation_id, onPrepared: async value => { assert.equal(value.source_commit, source_commit); prepared = true; } }), { code: 'CI_DISPATCH_UNCONFIRMED' });
  await assert.rejects(service.deploy({ ...input, operation_id,
    onPrepared: async () => { throw new Error('private checkpoint failure'); } }),
  error => error.code === 'SOURCE_CHECKPOINT_FAILED' && error.phase === 'source_checkpoint'
    && !error.message.includes('GitHub') && error.outcomeUnknown === false);
  assert.equal(dispatches, 1, 'a failed local checkpoint must not dispatch CI');
  assert.match(commit.message, new RegExp(`Railshot-Request: ${operation_id}`));
  const binding = { operation_id, source_commit, app: input.app, target_id: input.target_id };
  assert.equal((await service.findDeployment(binding)).run_id, 123);
  mode = 'foreign'; assert.equal(await service.findDeployment(binding), null);
  for (mode of ['duplicate', 'rerun']) await assert.rejects(service.findDeployment(binding), { code: 'CI_DISPATCH_AMBIGUOUS' });
  assert.equal(dispatches, 1);
});

test('artifact transport failure remains retriable while explicit dispatch rejection is definite', async () => {
  const service = createDeploymentService({ token: 'test', targetId: 'demo' }, async url => {
    const path = new URL(url).pathname;
    if (path.endsWith('/runs/123')) return Response.json({ path: '.github/workflows/railshot-deploy.yml', run_attempt: 1, status: 'completed', conclusion: 'success', head_sha: publication.source_commit });
    if (path.endsWith('/jobs')) return Response.json({ total_count: 2, jobs: ['loop', 'release'].map(name => ({ name, status: 'completed', conclusion: 'success' })) });
    if (path.endsWith('/artifacts')) return Response.json({ message: 'unavailable' }, { status: 503 });
    assert.fail(path);
  });
  await assert.rejects(service.status('123'), error => error.retryable === true && error.upstreamStatus === 503);
  for (const status of [401, 403, 404, 422]) {
    const error = new SubmissionError('ci_dispatch', { upstreamStatus: status });
    assert.equal(error.code, 'CI_DISPATCH_REJECTED'); assert.equal(error.outcomeUnknown, false);
  }
});


test('GitHub rate limit backs off across runs without spending another request', async () => {
  let calls = 0;
  const service = createDeploymentService({ token: 'test', targetId: 'demo' }, async () => {
    calls++;
    return Response.json({}, { status: 429, headers: { 'retry-after': '120' } });
  });
  const started = Date.now();
  await assert.rejects(service.status('123'), error => error.retryable && error.retryAt >= started + 120_000);
  await assert.rejects(service.status('456'), error => error.upstreamStatus === 429 && error.retryable);
  assert.equal(calls, 1);
});

test('GitHub successful response near exhaustion preserves account reserve', async () => {
  let calls = 0;
  const reset = Math.floor(Date.now() / 1000) + 300;
  const service = createDeploymentService({ token: 'test', targetId: 'demo' }, async () => {
    calls++;
    return Response.json({ path: '.github/workflows/railshot-deploy.yml', run_attempt: 1 },
      { headers: { 'x-ratelimit-remaining': '100', 'x-ratelimit-reset': String(reset) } });
  });
  await assert.rejects(service.status('123'), error => error.retryAt === reset * 1000 && error.retryable);
  await assert.rejects(service.status('456'), error => error.retryAt === reset * 1000);
  assert.equal(calls, 1);
});

test('graceful shutdown finishes an admitted source submission and restart observes its run without redispatch', async t => {
  const entered = deferred(), uploaded = deferred();
  let sends = 0;
  const f = await fixture(t, {
    deploy: async ({ onPrepared }) => {
      sends++; entered.resolve(); await uploaded.promise;
      await onPrepared({ source_commit: publication.source_commit });
      return { run_id: 123, source_commit: publication.source_commit };
    },
  });
  const accepted = await f.product.createDeployment(input, 'shutdown-source');
  await entered.promise;
  const closing = f.product.close();
  uploaded.resolve(); await closing;
  f.product = await createProductService(f.options);
  const done = await until(() => f.product.getDeployment(accepted.id), row => row.status === 'succeeded');
  assert.equal(done.source_commit, publication.source_commit);
  assert.equal(done.ci.run_id, '123'); assert.equal(sends, 1);
});

test('local source checkpoint errors are never reported as GitHub communication errors', () => {
  const error = new SubmissionError('source_checkpoint', new Error('private storage path'));
  assert.equal(error.code, 'SOURCE_CHECKPOINT_FAILED');
  assert.equal(error.reason, 'local_checkpoint_failure');
  assert.equal(error.outcomeUnknown, false);
  assert.equal(error.upstream_status, null);
  assert.doesNotMatch(error.message, /GitHub|private/);
});
