import test from 'node:test';
import assert from 'node:assert/strict';
import { chmod, mkdir, mkdtemp, readFile, realpath, rm, stat, writeFile } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { tmpdir } from 'node:os';
import { createApplicationAdapter } from '../src/applications.js';
import { privateJson } from '../src/environments.js';

async function fixture(t, replaceRunner) {
  const home = await realpath(await mkdtemp(join(tmpdir(), 'railshot-applications-')));
  t.after(() => rm(home, { recursive: true, force: true }));
  const configPath = join(home, 'config.json');
  const config = { version: 1, state_dir: join(home, 'applications'), environments: Object.fromEntries(
    ['aws', 'gcp', 'openstack'].map((provider) => [`runtime-${provider}`, { provider, tenant: 'team', source_repository: 'example/apps' }])) };
  await writeFile(configPath, JSON.stringify(config), { mode: 0o600 });
  const python = join(home, 'fake-python');
  await writeFile(python, `#!/usr/bin/env node
const {readFileSync,writeFileSync}=require('node:fs');
let raw='';process.stdin.on('data',chunk=>raw+=chunk);process.stdin.on('end',()=>{
  const request=JSON.parse(raw),configPath=process.argv[process.argv.indexOf('--config')+1];
  const config=JSON.parse(readFileSync(configPath));
  if(request.publication.app!==config.targets[request.target_id].app)process.exit(2);
  writeFileSync(configPath+'.observed.json',JSON.stringify(request),{mode:0o600});
  process.stdout.write(JSON.stringify({cd:{state:'deployed',deployed:true,revision:'b'.repeat(40)},
    public_http:{state:'succeeded',verified_at:'2026-10-03T00:00:00Z',url:'https://app.example/health',site_url:'https://app.example/'}}));
});
`, { mode: 0o700 });
  const calls = [], loads = [];
  const files = [{ path: 'railshot.yaml', content: Buffer.from('actual uploaded source contract') }];
  const options = { configPath, python, ciIdentity: { tenant: 'team', sourceRepository: 'example/apps' },
    loadPublished: async (publication) => { loads.push(publication); return files; },
    runner: async (executable, args, settings) => {
      const requestPath = args[args.indexOf('--request') + 1];
      const request = JSON.parse(await readFile(requestPath, 'utf8'));
      calls.push({ executable, args, settings, requestPath, request });
      if (replaceRunner) return replaceRunner(request, args);
      if (args[0].endsWith('/applications.py')) return { status: 'succeeded', application_id: request.application_id,
        target_id: request.application_id, environment_id: request.environment_id, app: request.app,
        namespace: `app-${request.app}`, node_port: 31001, hostname: `${request.app}.example` };
      assert.ok(args[0].endsWith('/application_routes.py'));
      await writeFile(join(dirname(requestPath), 'cd.json'), JSON.stringify({ version: 1, targets: {
        [request.publication.target_id]: { app: request.publication.app, tenant: 'team', target: { id: request.publication.target_id } },
      } }), { mode: 0o600 });
      return { status: 'succeeded', application_id: request.application_id, target_id: request.application_id,
        environment_id: request.environment_id, app: request.publication.app };
    } };
  return { home, configPath, config, options, adapter: await createApplicationAdapter(options), calls, loads, files };
}

test('existing environments describe stable separate application targets without prefilled app registrations', async (t) => {
  const f = await fixture(t);
  const first = f.adapter.describe('runtime-aws', 'calculator');
  const second = f.adapter.describe('runtime-aws', 'notes');
  assert.notEqual(first.target_id, second.target_id);
  assert.equal(first.environment_target_id, second.environment_target_id);
  assert.deepEqual(first, (await createApplicationAdapter(f.options)).describe('runtime-aws', 'calculator'));
  for (const provider of ['aws', 'gcp', 'openstack']) {
    assert.equal(f.adapter.targets[`runtime-${provider}`].deploymentScope, 'environment');
    assert.equal(f.adapter.targets[`runtime-${provider}`].applicationName, undefined);
    const described = f.adapter.describe(`runtime-${provider}`, 'calculator');
    const ready = await f.adapter.register(described);
    assert.equal(ready.target_id, described.target_id); assert.equal(ready.status, 'ready');
    assert.equal(ready.environment_target_id, `runtime-${provider}`);
    const call = f.calls.at(-1);
    assert.deepEqual(call.request, { environment_id: `runtime-${provider}`, app: 'calculator', application_id: described.id });
    assert.equal(call.settings.mutation, true);
    assert.equal((await stat(call.requestPath)).mode & 0o777, 0o600);
  }
  assert.equal(new Set(f.calls.map(({ request }) => request.application_id)).size, 3);
});

test('environment CI identity must match the live client before registration can run', async (t) => {
  const f = await fixture(t);
  for (const ciIdentity of [undefined, { tenant: 'other', sourceRepository: 'example/apps' },
    { tenant: 'team', sourceRepository: 'example/other' }]) {
    await assert.rejects(createApplicationAdapter({ ...f.options, ciIdentity }),
      { code: 'APPLICATION_CI_CONFIGURATION_MISMATCH', status: 503 });
  }
  assert.equal(f.calls.length, 0);
});

test('OpenStack automatic delivery requires the edge, Tunnel and DNS operator configurations', async (t) => {
  const f = await fixture(t);
  const complete = { edge_config_file: '/private/openstack-edge.json',
    dns_config_file: '/private/dns.json', tunnel_config_file: '/private/tunnel.json' };
  for (const missing of [null, ...Object.keys(complete)]) {
    const ingress = { ...complete };
    if (missing) delete ingress[missing];
    f.config.environments['runtime-openstack'].ingress = ingress;
    await writeFile(f.configPath, JSON.stringify(f.config));
    const adapter = await createApplicationAdapter(f.options);
    assert.equal(adapter.targets['runtime-openstack'].automaticDelivery, missing === null);
  }
  assert.equal(f.calls.length, 0);
});

test('registration rejects invalid or changed identities before execution and never accepts foreign readback', async (t) => {
  const f = await fixture(t);
  for (const [environment, app] of [['missing', 'calculator'], ['__proto__', 'calculator'], ['runtime-aws', '../escape']])
    assert.throws(() => f.adapter.describe(environment, app), { code: 'APPLICATION_INPUT_INVALID' });
  const application = f.adapter.describe('runtime-aws', 'calculator');
  for (const patch of [{ id: 'app-wrong' }, { target_id: 'app-wrong' }, { app: 'notes' }, { environment_target_id: 'runtime-gcp' }])
    await assert.rejects(f.adapter.register({ ...application, ...patch }), { code: 'APPLICATION_BINDING_MISMATCH' });
  assert.equal(f.calls.length, 0);
  await writeFile(f.configPath, JSON.stringify({ ...f.config, revision: 'changed' }));
  await assert.rejects(f.adapter.register(application), { code: 'APPLICATION_POLICY_CHANGED' });
  assert.equal(f.calls.length, 0);
  for (const field of ['application_id', 'target_id', 'environment_id', 'app']) {
    await t.test(field, async (t) => {
      const bad = await fixture(t, (request) => ({ status: 'succeeded', application_id: request.application_id,
        target_id: request.application_id, environment_id: request.environment_id, app: request.app, [field]: 'foreign' }));
      await assert.rejects(bad.adapter.register(bad.adapter.describe('runtime-aws', 'calculator')),
        { code: 'APPLICATION_BINDING_MISMATCH', outcomeUnknown: true });
    });
  }
});

test('unknown registration is preserved and published source binding reaches the existing CD adapter', async (t) => {
  const uncertain = await fixture(t, (request) => ({ status: 'unknown', application_id: request.application_id,
    target_id: request.application_id, environment_id: request.environment_id, app: request.app,
    error: { code: 'APPLICATION_RECONCILE_REQUIRED' } }));
  await assert.rejects(uncertain.adapter.register(uncertain.adapter.describe('runtime-aws', 'calculator')),
    { code: 'APPLICATION_RECONCILE_REQUIRED', outcomeUnknown: true });
  const f = await fixture(t), application = f.adapter.describe('runtime-aws', 'calculator');
  const publication = { app: application.app, target_id: application.target_id, tenant: 'team', source_commit: 'a'.repeat(40) };
  const args = { deploymentId: '10000000-0000-4000-8000-000000000001', app: application.app,
    targetId: application.target_id, sourceCommit: publication.source_commit, publication };
  for (const changes of [{ app: 'notes' }, { targetId: 'foreign' }, { sourceCommit: 'c'.repeat(40) }, { deploymentId: '../escape' },
    ...['app', 'target_id', 'tenant', 'source_commit'].map((field) => ({ publication: { ...publication, [field]: 'foreign' } }))]) {
    await assert.rejects(f.adapter.deployPublished(application, { ...args, ...changes }), { code: 'APPLICATION_PUBLICATION_MISMATCH' });
  }
  assert.equal(f.calls.length, 0); assert.equal(f.loads.length, 0);
  const result = await f.adapter.deployPublished(application, args);
  assert.equal(result.cd.deployed, true); assert.equal(result.public_http.site_url, 'https://app.example/');
  assert.equal(f.calls.length, 1);
  const saved = f.calls[0].request;
  assert.equal(saved.application_id, application.id); assert.equal(saved.environment_id, 'runtime-aws');
  assert.deepEqual(saved.publication, publication);
  assert.equal(Buffer.from(saved.files['railshot.yaml'], 'base64').toString(), f.files[0].content.toString());
  const cd = JSON.parse(await readFile(join(dirname(f.calls[0].requestPath), 'cd.json.observed.json'), 'utf8'));
  assert.equal(cd.target_id, application.target_id); assert.equal(cd.deployment_id, args.deploymentId);
  assert.deepEqual(cd.publication, publication); assert.deepEqual(cd.files, saved.files);
  const observed = await f.adapter.observePublished(application, args);
  assert.equal(observed.cd.deployed, true); assert.equal(f.calls.length, 1, 'recovery must not finalize routes again');
  const read = JSON.parse(await readFile(join(dirname(f.calls[0].requestPath), 'cd.json.observed.json'), 'utf8'));
  assert.equal(read.action, 'observe');
});

test('CD recovery reports an absent journal without hiding invalid existing configuration', async (t) => {
  const f = await fixture(t), application = f.adapter.describe('runtime-aws', 'calculator');
  const args = { deploymentId: '10000000-0000-4000-8000-000000000003', app: application.app,
    targetId: application.target_id };
  const missing = await f.adapter.observePublished(application, args);
  assert.equal(missing.error.code, 'DEPLOYMENT_NOT_FOUND');
  assert.equal(missing.cd.state, 'blocked');
  assert.equal(missing.cd.deployed, false);
  assert.equal(missing.cd.revision, null);
  assert.equal(missing.public_http.state, 'not_run');
  assert.equal(f.calls.length, 0, 'observation must not finalize or apply');
  assert.equal(f.loads.length, 0, 'missing configuration must not load artifacts');
  const configPath = join(f.config.state_dir, application.id, 'deployments', args.deploymentId, 'cd.json');
  await mkdir(dirname(configPath), { recursive: true, mode: 0o700 });
  await writeFile(configPath, '{invalid', { mode: 0o600 });
  await assert.rejects(f.adapter.observePublished(application, args), { code: 'ENVIRONMENT_CONFIGURATION_INVALID' });
  await writeFile(configPath, '{}');
  await chmod(configPath, 0o644);
  await assert.rejects(f.adapter.observePublished(application, args), { code: 'ENVIRONMENT_CONFIGURATION_INVALID' });
  await rm(configPath);
  await rm(f.configPath);
  await assert.rejects(f.adapter.observePublished(application, args), { code: 'ENVIRONMENT_CONFIGURATION_INVALID' });
});

test('publication above the operator-config limit replays unchanged and still rejects changed bytes', async (t) => {
  const f = await fixture(t), application = f.adapter.describe('runtime-aws', 'calculator');
  f.files[0].content = Buffer.alloc(900_000, 'a');
  const publication = { app: application.app, target_id: application.target_id, tenant: 'team', source_commit: 'a'.repeat(40) };
  const args = { deploymentId: '10000000-0000-4000-8000-000000000002', app: application.app,
    targetId: application.target_id, sourceCommit: publication.source_commit, publication };
  const first = await f.adapter.deployPublished(application, args);
  const requestPath = f.calls[0].requestPath, before = await stat(requestPath, { bigint: true });
  assert.ok(before.size > 1024n * 1024n && before.size < 14_000_000n);
  await assert.rejects(privateJson(requestPath), { code: 'ENVIRONMENT_CONFIGURATION_INVALID' },
    'operator configuration keeps its existing 1 MiB default limit');
  assert.deepEqual(await f.adapter.deployPublished(application, args), first);
  assert.equal((await stat(requestPath, { bigint: true })).mtimeNs, before.mtimeNs);
  assert.equal(f.calls.length, 2);
  assert.deepEqual(f.calls[1].request, f.calls[0].request);
  f.files[0].content[0] = 'b'.charCodeAt(0);
  await assert.rejects(f.adapter.deployPublished(application, args), { code: 'APPLICATION_PUBLICATION_MISMATCH' });
  assert.equal(f.calls.length, 2);
  assert.equal((await stat(requestPath, { bigint: true })).mtimeNs, before.mtimeNs);
});

test('registration recovery distinguishes no native request from an attempted registration', async t => {
  const f = await fixture(t), app = f.adapter.describe('runtime-gcp', 'memos');
  assert.equal(await f.adapter.registrationStarted(app), false);
  assert.equal(f.calls.length, 0);
  await f.adapter.register(app);
  assert.equal(await f.adapter.registrationStarted(app), true);
  assert.equal(f.calls.length, 1, 'observation must not invoke registration again');
  await assert.rejects(f.adapter.registrationStarted({ ...app, id: 'foreign' }), { code: 'APPLICATION_BINDING_MISMATCH' });
});

test('runtime observation does not launch CI or write a journal when CD has not started', async (t) => {
  const f = await fixture(t);
  const application = f.adapter.describe('runtime-gcp', 'clock');
  const record = { id: '21943fb1-681b-4423-a5ad-39d0870abe8c', application_id: application.id,
    app: application.app, target_id: application.target_id, cd: { state: 'blocked', revision: null },
    source_commit: 'a'.repeat(40), ci: { state: 'published', run_id: '123' } };
  const result = await f.adapter.observeRuntime(application, record);
  assert.equal(result.state, 'no_data'); assert.equal(result.reason, 'deployment_not_started');
  assert.equal(result.workload, null); assert.equal(f.calls.length, 0); assert.equal(f.loads.length, 0);
  await assert.rejects(f.adapter.observeRuntime(application, { ...record, application_id: 'foreign' }), { code: 'APPLICATION_BINDING_MISMATCH' });
});

test('environment credential observation coalesces callers and caches bounded safe results', async (t) => {
  const f = await fixture(t);
  f.config.environments['k3s-gcp'] = f.config.environments['runtime-gcp'];
  f.config.cd = { context: 'railshot-platform' };
  await writeFile(f.configPath, JSON.stringify(f.config), { mode: 0o600 });
  let now = Date.now();
  t.mock.method(Date, 'now', () => now);
  let calls = 0, release;
  const gate = new Promise(resolve => { release = resolve; });
  const expected = { state: 'ready', reason: null, checked_at: new Date().toISOString(),
    last_success_at: '2026-10-05T00:00:00Z', expires_at: '2026-10-06T00:00:00Z' };
  const adapter = await createApplicationAdapter({ ...f.options, runner: async (python, args, options) => {
    calls++; assert.ok(args[0].endsWith('/gitops/credentials.py'));
    assert.deepEqual(args.slice(1), ['observe', '--context', 'railshot-platform', '--environment', 'k3s-gcp']);
    assert.deepEqual(options, { mutation: false, timeout: 12000 });
    await gate; return { ...expected, bearerToken: 'never-expose' };
  } });
  const pending = Array.from({ length: 12 }, () => adapter.observeCredentials('k3s-gcp'));
  release();
  assert.deepEqual(await Promise.all(pending), Array(12).fill(expected));
  assert.equal(calls, 1);
  assert.deepEqual(await adapter.observeCredentials('k3s-gcp'), expected);
  assert.equal(calls, 1, 'refreshes within 60 seconds reuse the completed observation');
  now += 60001;
  assert.deepEqual(await adapter.observeCredentials('k3s-gcp'), expected);
  assert.equal(calls, 2, 'expired results trigger a fresh credential validation');
  assert.equal((await adapter.observeCredentials('unregistered')).state, 'no_data');
  assert.equal(calls, 2, 'unregistered environments cannot launch a collector');
  const failed = await createApplicationAdapter({ ...f.options, runner: async () => { throw Error('private-token'); } });
  assert.equal((await failed.observeCredentials('k3s-gcp')).reason, 'RENEWAL_FAILED');
  assert.equal((await failed.observeCredentials('k3s-gcp')).last_success_at, null);
});
