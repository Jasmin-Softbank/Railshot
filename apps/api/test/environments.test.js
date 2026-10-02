import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdir, mkdtemp, readFile, realpath, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createEnvironmentAdapter, EnvironmentError, runEnvironmentCommand } from '../src/environments.js';

const DIGEST = 'a'.repeat(64);
const input = { name: 'demo-runtime', runtime: { profile_id: 'aws-small', node_count: 1 }, database: { mode: 'none' } };
const descriptor = { schema_version: 'v1', provider_kind: 'aws', execution_driver: 'terraform',
  target_id: 'demo-runtime', architecture: 'x86_64', resource_id: 'arn:aws:ec2:ap-northeast-2:123456789012:instance/i-12345678',
  location: { region: 'ap-northeast-2' }, addresses: { private: '10.0.1.10' }, transport_ref: 'ssm:ap-northeast-2:i-12345678' };

async function fixture(t, { replaceRunner, modify } = {}) {
  const home = await realpath(await mkdtemp(join(tmpdir(), 'railshot-environments-')));
  t.after(() => rm(home, { recursive: true, force: true }));
  const config = { version: 1, policy_revision: 'revision1', profiles: [{ id: 'aws-small', label: 'AWS demo',
    provider: 'aws', site: 'ap-northeast-2', purposes: ['runtime'], allow_apply: true,
    target_file: join(home, 'target.json'), state_root: join(home, 'terraform'),
    ssh: { user: 'ubuntu', identity_file: join(home, 'identity'), known_hosts_file: join(home, 'knownhosts') } }] };
  const profile = config.profiles[0];
  await writeFile(profile.target_file, JSON.stringify({ schema_version: 'v1', target_id: 'demo-runtime',
    provider_kind: 'aws', variables: { target_id: 'demo-runtime' }, budget: { ledger_path: 'private' } }), { mode: 0o600 });
  for (const path of [profile.ssh.identity_file, profile.ssh.known_hosts_file]) await writeFile(path, 'private', { mode: 0o600 });
  modify?.(config);
  const profilesFile = join(home, 'profiles.json');
  await writeFile(profilesFile, JSON.stringify(config), { mode: 0o600 });
  const calls = [];
  async function fakeRunner(python, args, options) {
    calls.push({ args, options });
    if (replaceRunner) return replaceRunner(python, args, options, defaultRunner);
    return defaultRunner(python, args, options);
  }
  async function defaultRunner(python, args, options) {
    if (args[1] === 'plan') {
      await mkdir(join(profile.state_root, 'demo-runtime'), { recursive: true, mode: 0o700 });
      await writeFile(join(profile.state_root, 'demo-runtime/plan-manifest.json'), JSON.stringify({ plan_sha256: DIGEST, apply_attempted: false }), { mode: 0o600 });
      return { plan_sha256: DIGEST, target_id: 'demo-runtime', destructive: false,
        maintenance_required: false, changes: [{ address: 'aws_instance.node', actions: ['create'] }] };
    }
    if (args[1] === 'apply') return { apply_status: 'completed', plan_sha256: DIGEST, node_descriptor: descriptor };
    if (args.includes('--validate-only')) return runEnvironmentCommand(python, args, options); // Real checked-in descriptor/schema adapter; no SSH.
    const operation = args[args.indexOf('--operation') + 1];
    return { status: 'succeeded', target_id: 'demo-runtime', request_id: args[args.indexOf('--request-id') + 1],
      operation, guest_ready: true, runtime_ready: operation === 'runtime.install' };
  }
  const adapter = await createEnvironmentAdapter({ profilesFile, stateDir: join(home, 'environments'), runner: fakeRunner });
  return { adapter, home, profilesFile, config, calls };
}

test('registered profile → saved plan → descriptor snapshot → native validation → guest/runtime receipts', async (t) => {
  const { adapter, home, calls } = await fixture(t);
  assert.deepEqual(Object.keys(adapter.profiles()[0]).sort(), ['blockers', 'id', 'label', 'provider', 'purposes', 'site', 'supported']);
  const plan = await adapter.plan(input, { id: 'plan1' });
  assert.equal(plan.public.executable, true);
  assert.equal(plan.public.plan_sha256, DIGEST);
  assert.equal(JSON.stringify(plan.public).includes(home), false);
  const progress = [];
  const result = await adapter.execute(plan, { id: 'environment1', onProgress: async (patch) => progress.push(patch) });
  assert.equal(result.status, 'succeeded');
  assert.equal(result.resources.status, 'succeeded');
  assert.equal(result.guest.status, 'succeeded');
  assert.equal(result.runtime.status, 'succeeded');
  assert.equal(result.runtime_target_id, descriptor.target_id);
  assert.equal(result.deployment_supported, false);
  assert.deepEqual(result.blockers, ['DEPLOYMENT_TARGET_NOT_REGISTERED']);
  assert.equal(result.database.status, 'skipped');
  assert.equal(progress[0].resources.status, 'running');
  assert.equal(JSON.stringify(result).includes(home), false);
  assert.deepEqual(calls.filter((call) => call.args[1] === 'apply')[0].args.slice(-2), ['--plan-sha256', DIGEST]);
  const registry = JSON.parse(await readFile(join(home, 'environments/environment1/targets.json'), 'utf8'));
  assert.equal(registry.targets['demo-runtime'].purpose, 'runtime');
  await assert.rejects(adapter.execute(plan, { id: 'environment1' }), { code: 'EEXIST' });
  assert.equal(calls.filter((call) => call.args[1] === 'apply').length, 1);
});

test('unsupported database/multinode requests are saved as blocked plans without cloud calls', async (t) => {
  const { adapter, calls } = await fixture(t);
  const plan = await adapter.plan({ ...input, runtime: { ...input.runtime, node_count: 2 }, database: { mode: 'patroni' } }, { id: 'plan2' });
  assert.equal(plan.public.executable, false);
  assert.deepEqual(plan.public.blockers, ['SINGLE_NODE_ONLY', 'DATABASE_EXECUTION_NOT_CONNECTED']);
  assert.equal(calls.length, 0);
  await assert.rejects(adapter.verifyPlan(plan), { code: 'PLAN_NOT_EXECUTABLE' });
});

test('client paths/credentials, unknown profiles and extra intent fields never reach the executor', async (t) => {
  const { adapter, calls } = await fixture(t);
  for (const bad of [
    { ...input, ssh: { identity_file: '/tmp/secret' } },
    { ...input, runtime: { ...input.runtime, target_file: '/tmp/target' } },
    { ...input, database: { mode: 'none', password: 'secret' } },
  ]) await assert.rejects(adapter.plan(bad, { id: 'bad' }), { code: 'INVALID_PLAN_REQUEST' });
  await assert.rejects(adapter.plan({ ...input, runtime: { profile_id: 'unknown', node_count: 1 } }, { id: 'bad' }), { code: 'PROFILE_NOT_REGISTERED' });
  assert.equal(calls.length, 0);
});

test('policy revocation, expired plans and changed snapshots reject before apply', async (t) => {
  const { adapter, profilesFile, config, calls } = await fixture(t);
  const plan = await adapter.plan({ database: input.database, runtime: input.runtime, name: input.name }, { id: 'plan3' });
  await adapter.verifyPlan(plan); // Input object key order is irrelevant.
  await assert.rejects(adapter.verifyPlan({ ...plan, public: { ...plan.public, expires_at: '2000-01-01' } }), { code: 'PLAN_EXPIRED' });
  await assert.rejects(adapter.verifyPlan({ ...plan, public: { ...plan.public, runtime: { ...input.runtime, node_count: 2 } } }), { code: 'PLAN_SNAPSHOT_CHANGED' });
  config.profiles[0].allow_apply = false;
  await writeFile(profilesFile, JSON.stringify(config));
  await assert.rejects(adapter.execute(plan, { id: 'environment3' }), { code: 'PROFILE_POLICY_CHANGED' });
  assert.equal(calls.some((call) => call.args[1] === 'apply'), false);
});

test('a superseding native plan or previously attempted apply is rejected before admission', async (t) => {
  const { adapter, config } = await fixture(t);
  const plan = await adapter.plan(input, { id: 'replaced' });
  const manifest = join(config.profiles[0].state_root, 'demo-runtime/plan-manifest.json');
  await writeFile(manifest, JSON.stringify({ plan_sha256: 'b'.repeat(64), apply_attempted: false }));
  await assert.rejects(adapter.verifyPlan(plan), { code: 'SAVED_PLAN_REPLACED' });
  await writeFile(manifest, JSON.stringify({ plan_sha256: DIGEST, apply_attempted: true }));
  await assert.rejects(adapter.verifyPlan(plan), { code: 'PLAN_ALREADY_EXECUTED' });
});

test('public creation cannot authorize destruction or modification of existing resources', async (t) => {
  const { adapter } = await fixture(t, { replaceRunner: async () => ({ plan_sha256: DIGEST, target_id: 'demo-runtime',
    changes: [{ address: 'aws_instance.node', actions: ['update'] }], destructive: false, maintenance_required: false }) });
  const plan = await adapter.plan(input, { id: 'plan4' });
  assert.equal(plan.public.executable, false);
  assert.ok(plan.public.blockers.includes('EXISTING_RESOURCE_CHANGE_REQUIRES_OPERATOR'));
});

test('uncertain apply is retained as unknown and runtime dispatch never starts', async (t) => {
  const { adapter, calls } = await fixture(t, { replaceRunner: async (python, args, options, run) => {
    if (args[1] === 'apply') throw new EnvironmentError('INFRA_EXECUTION_FAILED', 502, true);
    return run(python, args, options);
  } });
  const plan = await adapter.plan(input, { id: 'plan5' });
  const result = await adapter.execute(plan, { id: 'environment5' });
  assert.equal(result.status, 'unknown');
  assert.equal(result.error.outcome_unknown, true);
  assert.equal(result.error.retryable, false);
  assert.equal(calls.length, 2);
});

test('runtime readiness requires the exact environment request and target identity', async (t) => {
  const { adapter } = await fixture(t, { replaceRunner: async (python, args, options, run) => {
    const result = await run(python, args, options);
    if (args.includes('runtime.install')) result.request_id = 'another-environment';
    return result;
  } });
  const result = await adapter.execute(await adapter.plan(input, { id: 'plan6' }), { id: 'environment6' });
  assert.equal(result.resources.status, 'succeeded');
  assert.equal(result.guest.status, 'succeeded');
  assert.equal(result.runtime.status, 'unknown');
  assert.equal(result.status, 'unknown');
  assert.equal(result.error.code, 'READINESS_RESULT_INVALID');
});
