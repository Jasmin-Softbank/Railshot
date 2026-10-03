import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, rm, readFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createHash } from 'node:crypto';
import { setTimeout as pause } from 'node:timers/promises';
import { createAppServer } from '../src/server.js';
import { apiAccessConfig } from '../src/access.js';
import { DatabaseSync } from 'node:sqlite';

async function fixture(t) {
  const directory = await mkdtemp(join(tmpdir(), 'personal-api-'));
  const calls = { deploy: [], delete: [], gateway: [], verify: [], execute: [] }, targets = {}, runs = new Map();
  const appAdapter = { targets,
    describe: (id, app) => ({ id: 'app-' + createHash('sha256').update(id + app).digest('hex').slice(0, 24), app,
      target_id: 'app-' + createHash('sha256').update(id + app).digest('hex').slice(0, 24), environment_target_id: id, provider: 'openstack' }),
    register: async (app) => ({ ...app, status: 'ready' }),
    deployPublished: async () => ({ cd: { deployed: true, revision: 'a'.repeat(40), state: 'deployed' }, public_http: { state: 'succeeded', verified_at: new Date().toISOString(), url: 'https://app.example.test' } }),
    planLifecycle: async (app, { id }) => ({ public: { id, application_id: app.id, action: 'delete', plan_hash: 'a'.repeat(64), expires_at: new Date(Date.now() + 600000).toISOString(), resources: [], retained: [] }, private: {} }),
    verifyLifecyclePlan: async () => {},
    applyLifecycle: async (app, plan, options) => { calls.delete.push({ app, options }); return { status: 'succeeded', residuals: [], steps: [] }; },
  };
  const personalAdapter = { config: { public_url: 'https://api.example.test', installer_url: 'https://api.example.test/install.sh', artifact_url: 'https://api.example.test/client.tgz', artifact_sha256: 'b'.repeat(64) },
    execute: async (target, input) => { calls.execute.push({ target_id: target.id, input }); return {ok:true,result:[{id:'server-one',name:'customer VM',status:'ACTIVE'}]}; },
    prepare: async (target) => { targets[target.id] = { provider: 'openstack', automaticDelivery: true }; return true; },
    register: async (target) => { calls.gateway.push(target.id); return { status: 'succeeded', tunnel: { server_public_key: 'B'.repeat(43) + '=', endpoint: 'wg.example.test:51820', address: '10.80.0.2/32', allowed_ips: '10.80.0.1/32' } }; },
    verify: async (target) => { calls.verify.push(target.id); return { status: 'succeeded', reachable: true, openstack_verified: true }; },
    cleanup: async () => ({ status: 'succeeded', residuals: [] }),
    remove: async () => ({ status: 'succeeded' }),
  };
  const service = { targetId: 'shared', allowTarget() {}, deploy: async (input) => {
    const id = runs.size + 1; runs.set(id, input); calls.deploy.push(input); return { run_id: id, source_commit: 'a'.repeat(40) };
  }, status: async (id) => { const input = runs.get(Number(id)); return { run_id: Number(id), state: 'published', status: 'completed', conclusion: 'success',
    publication: { run_id: Number(id), app: input.app, target_id: input.target_id, source_commit: 'a'.repeat(40), artifact_id: 1, producer_attempt: 1 } }; } };
  let server, base;
  async function start() {
    server = createAppServer({ stateDirectory: directory, service, applicationAdapter: appAdapter, personalAdapter, pollInterval: 1,
      access: apiAccessConfig({ RAILSHOT_PUBLIC_DEMO: '1', RAILSHOT_ALLOWED_HOSTS: '127.0.0.1', RAILSHOT_ALLOWED_ORIGINS: 'http://127.0.0.1' }) });
    await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve)); base = `http://127.0.0.1:${server.address().port}`;
  }
  async function stop() { await new Promise((resolve) => server.close(resolve)); await (await server.productReady).close(); }
  await start(); t.after(async () => { await stop(); await rm(directory, { recursive: true, force: true }); });
  const client = () => { const cookies = new Map(); return { cookies, async request(path, body, options = {}) {
    const response = await fetch(base + path, { method: body === undefined ? 'GET' : 'POST', ...options,
      headers: { Cookie: [...cookies].map(([k, v]) => `${k}=${v}`).join('; '), 'X-Railshot-Request': 'dashboard',
        ...(body !== undefined && { 'Content-Type': 'application/json' }), ...options.headers }, ...(body !== undefined && { body: JSON.stringify(body) }) });
    for (const set of response.headers.getSetCookie()) { const [name, value] = set.split(';')[0].split('='); cookies.set(name, value); }
    return { status: response.status, headers: response.headers, body: await response.json() };
  } }; };
  async function target(client, project = 'project-one') {
    const owner = await client.request('/api/v1/owners', {});
    const created = await client.request('/api/v1/targets', { label: '내 OpenStack', provider: 'openstack' }); assert.equal(created.status, 201);
    const enrollment = await client.request(`/api/v1/targets/${created.body.id}/enrollments`, {}); assert.equal(enrollment.status, 201);
    const token = /RAILSHOT_ENROLLMENT_TOKEN='([^']+)'/.exec(enrollment.body.install_command)[1];
    const claim = await client.request(`/api/v1/enrollments/${enrollment.body.id}/claims`, { public_key: 'A'.repeat(43) + '=', client_version: '1.0', project_id: project, runtime: {ssh_host_key:'ssh-ed25519 '+'A'.repeat(68)} }, { headers: { Authorization: `Bearer ${token}` } });
    return { owner, created, enrollment, token, claim, id: created.body.id };
  }
  const heartbeat = (client, row) => client.request(`/api/v1/targets/${row.id}/heartbeats`, { generation: row.claim.body.generation, client_version: '1.0', checks: { tunnel: true, openstack: true, runtime: true } }, { headers: { Authorization: `Bearer ${row.claim.body.client_token}` } });
  async function until(client, path, predicate) { let last; for (let i = 0; i < 100; i++) { const r = await client.request(path); last = r.body; if (predicate(r.body)) return r; await pause(10); } assert.fail('operation did not reach expected state: ' + JSON.stringify(last)); }
  return { directory, calls, client, target, heartbeat, until, personalAdapter, get base() { return base; }, restart: async () => { await stop(); await start(); } };
}

test('current browser owns registration immediately and can query instances as soon as the client is ready without recovery', async (t) => {
  const f = await fixture(t), owner = f.client(), other = f.client();
  assert.equal((await owner.request('/api/v1/owners', {})).status, 201);
  const ownerCookie = owner.cookies.get('railshot_owner');
  await other.request('/api/v1/owners', {});
  const created = await owner.request('/api/v1/targets', { label: '즉시 관리 환경', provider: 'openstack' });
  assert.equal(created.status, 201);
  const id = created.body.id, path = `/api/v1/targets/${id}`;
  assert.equal(created.body.status, 'pending');
  assert.equal((await owner.request(path)).status, 200);
  assert.deepEqual((await owner.request('/api/v1/targets?scope=owned&provider=openstack')).body.items.map((row) => row.id), [id]);
  assert.equal((await other.request(path)).status, 404);
  assert.deepEqual((await other.request('/api/v1/targets?scope=owned&provider=openstack')).body.items, []);
  assert.equal((await other.request(`${path}/enrollments`, {})).status, 404);
  assert.equal((await owner.request(`${path}/instances`)).status, 409, 'ownership precedes control connectivity');
  const enrollment = await owner.request(`${path}/enrollments`, {});
  assert.equal(enrollment.status, 201);
  const token = /RAILSHOT_ENROLLMENT_TOKEN='([^']+)'/.exec(enrollment.body.install_command)[1];
  const claim = await owner.request(`/api/v1/enrollments/${enrollment.body.id}/claims`, {
    public_key: 'A'.repeat(43) + '=', client_version: '1', project_id: 'project-one', runtime: { ssh_host_key: 'ssh-ed25519 ' + 'A'.repeat(68) },
  }, { headers: { Authorization: `Bearer ${token}` } });
  assert.equal(claim.status, 201);
  assert.equal((await f.heartbeat(owner, { id, claim })).body.status, 'ready');
  assert.equal((await other.request(`${path}/instances`)).status, 404);
  assert.equal(f.calls.execute.length, 0, 'unready or foreign requests never reach the customer executor');
  const instances = await owner.request(`${path}/instances`);
  assert.equal(instances.status, 200);
  assert.equal(instances.body.items[0].id, 'server-one');
  assert.deepEqual(f.calls.execute, [{ target_id: id, input: { argv: ['server', 'list'] } }]);
  assert.equal(owner.cookies.get('railshot_owner'), ownerCookie, 'no recovery or owner credential rotation is needed');
});

test('durable owner recovery rotates both capabilities and survives ordinary session expiry/restart', async (t) => {
  const f = await fixture(t), a = f.client(), b = f.client(), row = await f.target(a);
  assert.equal(row.claim.status, 201);
  assert.equal((await b.request(`/api/v1/targets/${row.id}`)).status, 404);
  assert.equal((await b.request('/api/v1/targets?scope=owned&provider=openstack')).body.items.length, 0);
  const previousCookie = new Map(a.cookies);
  const db = new DatabaseSync(join(f.directory, 'dashboard.sqlite3'));
  db.prepare('UPDATE sessions SET expires_at = 0 WHERE expires_at < ?').run(8640000000000000); db.close();
  await f.restart();
  assert.equal((await a.request(`/api/v1/targets/${row.id}`)).status, 200);
  const recovery = await b.request('/api/v1/recoveries', { recovery_key: row.owner.body.recovery_key }); assert.equal(recovery.status, 200);
  assert.notEqual(recovery.body.recovery_key, row.owner.body.recovery_key);
  assert.equal((await a.request(`/api/v1/targets/${row.id}`)).status, 404, 'old owner cookie is revoked');
  assert.equal((await b.request(`/api/v1/targets/${row.id}`)).status, 200);
  assert.equal((await a.request('/api/v1/recoveries', { recovery_key: row.owner.body.recovery_key })).status, 401);
  const bytes = await readFile(join(f.directory, 'dashboard.sqlite3')); assert.ok(!bytes.includes(Buffer.from(row.owner.body.recovery_key)));
  assert.ok(previousCookie.has('railshot_owner'));
});

test('enrollment is single-use and rotates stale credentials without requiring preassigned runtime profiles', async (t) => {
  const f = await fixture(t), a = f.client(); await a.request('/api/v1/owners', {});
  const created = await a.request('/api/v1/targets', { label: '환경' }), id = created.body.id;
  const first = await a.request(`/api/v1/targets/${id}/enrollments`, {}), second = await a.request(`/api/v1/targets/${id}/enrollments`, {});
  const claim = (enrollment, project = 'project-one') => a.request(`/api/v1/enrollments/${enrollment.body.id}/claims`, { public_key: 'A'.repeat(43) + '=', client_version: '1', project_id: project, runtime: {ssh_host_key:'ssh-ed25519 '+'A'.repeat(68)} }, { headers: { Authorization: `Bearer ${/RAILSHOT_ENROLLMENT_TOKEN='([^']+)'/.exec(enrollment.body.install_command)[1]}` } });
  assert.equal((await claim(first)).status, 401);
  assert.equal(f.calls.gateway.length, 0);
  assert.equal((await claim(second)).status, 201); assert.equal((await claim(second)).status, 401);
  assert.equal((await a.request(`/api/v1/targets/${id}/enrollments`, {})).status, 409);
  assert.match(second.body.install_command, /sudo --preserve-env=RAILSHOT_ENROLLMENT_TOKEN bash/);
});

test('client assertion alone cannot report deployment readiness and foreign heartbeat cannot mutate', async (t) => {
  const f = await fixture(t), a = f.client(), row = await f.target(a);
  assert.equal((await a.request(`/api/v1/targets/${row.id}`)).body.deployable, false);
  f.personalAdapter.verify = async () => ({ status: 'succeeded', reachable: false });
  assert.equal((await f.heartbeat(a, row)).body.status, 'connecting');
  f.personalAdapter.verify = async () => ({ status: 'succeeded', reachable: true });
  assert.equal((await f.heartbeat(a, row)).body.status, 'connecting', 'gateway alone is not OpenStack control verification');
  f.personalAdapter.verify = async () => ({ status: 'succeeded', reachable: true, openstack_verified: true });
  assert.equal((await f.heartbeat(a, row)).body.status, 'ready');
  const changed = { ...row, claim: { body: { ...row.claim.body } } }; changed.claim.body.client_token = 'Z'.repeat(43);
  assert.equal((await f.heartbeat(a, changed)).status, 401);
});

test('chosen personal target routes deployment and deletes only its RailShot app after explicit data consent', async (t) => {
  const f = await fixture(t), a = f.client(), b = f.client(), row = await f.target(a); await f.heartbeat(a, row);
  const form = () => { const value = new FormData(); value.set('environment', 'onprem'); value.set('provider', 'openstack'); value.set('target_id', row.id); value.set('source_name', 'hello-app'); value.append('files', new Blob(['console.log(1)']), 'app.js'); value.set('paths', '["app.js"]'); return value; };
  const deployment = await a.request('/api/v1/deployments', undefined, { method: 'POST', body: form(), headers: { 'Idempotency-Key': 'deploy-one' } });
  assert.equal(deployment.status, 202); await f.until(a, `/api/v1/deployments/${deployment.body.resource_id}`, (r) => r.status === 'succeeded');
  const listed = await a.request(`/api/v1/targets/${row.id}/applications`); assert.equal(listed.body.items.length, 1);
  assert.equal(f.calls.deploy[0].target_id, listed.body.items[0].target_id);
  assert.equal((await b.request(`/api/v1/targets/${row.id}/applications`)).status, 404);
  assert.equal((await a.request(`/api/v1/targets/${row.id}/plans`, { action: 'delete', delete_data: false })).status, 422);
  const plan = await a.request(`/api/v1/targets/${row.id}/plans`, { action: 'delete', delete_data: true }); assert.equal(plan.status, 201); assert.deepEqual(plan.body.blockers, []);
  const input = { action: 'delete', delete_data: true, plan_id: plan.body.id, plan_hash: plan.body.plan_hash, confirmation: row.created.body.label };
  const accepted = await a.request(`/api/v1/targets/${row.id}/operations`, input, { headers: { 'Idempotency-Key': 'remove-one' } }); assert.equal(accepted.status, 202);
  assert.equal((await a.request('/api/v1/deployments', undefined, { method: 'POST', body: form(), headers: { 'Idempotency-Key': 'deploy-after-delete' } })).status, 409);
  const operation = await f.until(a, `/api/v1/operations/${accepted.body.id}`, (r) => r.stage === 'client');
  assert.equal(f.calls.delete.length, 1); assert.equal(f.calls.delete[0].options.deleteData, true);
  const hb = await f.heartbeat(a, row); assert.deepEqual(hb.body.command.applications, []);
  const receipt = { operation_id: operation.body.id, generation: row.claim.body.generation, status: 'running', steps: [], residuals: [], client_removed: false };
  assert.equal((await a.request(`/api/v1/targets/${row.id}/receipts`, receipt, { headers: { Authorization: `Bearer ${row.claim.body.client_token}` } })).status, 200);
  assert.equal((await a.request(`/api/v1/targets/${row.id}/receipts`, { ...receipt, status: 'succeeded', client_removed: true }, { headers: { Authorization: `Bearer ${row.claim.body.client_token}` } })).body.status, 'succeeded');
  assert.equal((await a.request(`/api/v1/targets/${row.id}`)).body.status, 'deleted');
  assert.equal((await f.heartbeat(a, row)).status, 401);
  assert.equal((await a.request(`/api/v1/targets/${row.id}/operations`, input, { headers: { 'Idempotency-Key': 'remove-one' } })).body.id, accepted.body.id);
  assert.equal(f.calls.delete.length, 1);
  await f.restart(); assert.equal((await a.request(`/api/v1/targets/${row.id}`)).body.status, 'deleted');
});

test('interrupted client removal remains attention after restart and never repeats removal command', async (t) => {
  const f = await fixture(t), a = f.client(), row = await f.target(a); await f.heartbeat(a, row);
  const plan = await a.request(`/api/v1/targets/${row.id}/plans`, { action: 'delete', delete_data: true });
  const operation = await a.request(`/api/v1/targets/${row.id}/operations`, { action: 'delete', delete_data: true, plan_id: plan.body.id, plan_hash: plan.body.plan_hash, confirmation: row.created.body.label }, { headers: { 'Idempotency-Key': 'remove' } });
  await f.until(a, `/api/v1/operations/${operation.body.id}`, (r) => r.stage === 'client');
  await f.restart();
  assert.equal((await a.request(`/api/v1/targets/${row.id}`)).body.status, 'attention');
  assert.equal((await a.request(`/api/v1/operations/${operation.body.id}`)).body.status, 'unknown');
  assert.equal((await f.heartbeat(a, row)).body.command, null);
});

test('personal mutation requires request header, shared targets cannot be adopted and no runtime is fabricated', async (t) => {
  const f = await fixture(t), a = f.client();
  assert.equal((await a.request('/api/v1/owners', {}, { headers: { 'X-Railshot-Request': '' } })).status, 403);
  await a.request('/api/v1/owners', {});
  assert.equal((await a.request('/api/v1/targets/shared')).status, 404);
  assert.equal((await a.request('/api/v1/targets/shared/enrollments', {})).status, 404);
  assert.equal((await a.request('/api/v1/targets', { label: 'x', target_id: 'shared' })).status, 422);
  assert.equal((await a.request('/api/v1/targets?scope=owned&scope=owned')).status, 422);
  assert.equal((await a.request('/api/v1/targets?scope=owned&provider=aws')).status, 422);
});

test('unconfigured installation stays blocked while an unclaimed registration can be safely removed', async (t) => {
  const directory = await mkdtemp(join(tmpdir(), 'personal-unconfigured-'));
  const server = createAppServer({ stateDirectory: directory, service: null });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  t.after(async () => { await new Promise((resolve) => server.close(resolve)); await (await server.productReady).close(); await rm(directory, { recursive: true, force: true }); });
  const base = `http://127.0.0.1:${server.address().port}`, cookies = new Map();
  const request = async (path, body, key) => {
    const response = await fetch(base + path, { method: body === undefined ? 'GET' : 'POST',
      headers: { Cookie: [...cookies].map(([k,v]) => `${k}=${v}`).join('; '), 'X-Railshot-Request': 'dashboard', 'Content-Type': 'application/json', ...(key && { 'Idempotency-Key': key }) },
      ...(body !== undefined && { body: JSON.stringify(body) }) });
    for (const item of response.headers.getSetCookie()) { const [name, value] = item.split(';')[0].split('='); cookies.set(name, value); }
    return { status: response.status, body: await response.json() };
  };
  await request('/api/v1/owners', {});
  const created = await request('/api/v1/targets', { label: '미등록 환경' }), id = created.body.id;
  assert.equal((await request(`/api/v1/targets/${id}/enrollments`, {})).status, 409);
  const plan = await request(`/api/v1/targets/${id}/plans`, { action: 'delete', delete_data: true });
  const accepted = await request(`/api/v1/targets/${id}/operations`, { action: 'delete', delete_data: true, plan_id: plan.body.id, plan_hash: plan.body.plan_hash, confirmation: '미등록 환경' }, 'unclaimed-delete');
  assert.equal(accepted.status, 202);
  for (let i=0;i<30;i++) { const result = await request(`/api/v1/operations/${accepted.body.id}`); if (result.body.status === 'succeeded') break; await pause(10); }
  assert.equal((await request(`/api/v1/targets/${id}`)).body.status, 'deleted');
});

test('host measurements retain their own freshness and incomplete client removal cannot be declared complete', async (t) => {
  const f = await fixture(t), a = f.client(), row = await f.target(a);
  const body = { generation: row.claim.body.generation, client_version: '1', checks: { tunnel: true, openstack: true, runtime: true }, metrics: { cpu_percent: 21, memory_percent: 37, disk_percent: null } };
  const auth = { headers: { Authorization: `Bearer ${row.claim.body.client_token}` } };
  assert.equal((await a.request(`/api/v1/targets/${row.id}/heartbeats`, body, auth)).status, 200);
  const observation = await a.request(`/api/v1/targets/${row.id}/observations`);
  assert.equal(observation.body.metrics.cpu_percent.scope, 'client_host'); assert.equal(observation.body.metrics.cpu_percent.value, 21);
  assert.equal(observation.body.metrics.disk_percent.state, 'not_configured');
  assert.equal((await a.request(`/api/v1/targets/${row.id}/heartbeats`, { ...body, metrics: { cpu_percent: 101 } }, auth)).status, 422);
  const plan = await a.request(`/api/v1/targets/${row.id}/plans`, { action: 'delete', delete_data: true });
  const operation = await a.request(`/api/v1/targets/${row.id}/operations`, { action: 'delete', delete_data: true, plan_id: plan.body.id, plan_hash: plan.body.plan_hash, confirmation: row.created.body.label }, { headers: { 'Idempotency-Key': 'partial' } });
  await f.until(a, `/api/v1/operations/${operation.body.id}`, (r) => r.stage === 'client');
  await a.request(`/api/v1/targets/${row.id}/receipts`, {operation_id:operation.body.id,generation:row.claim.body.generation,status:'unknown',steps:[],residuals:[{kind:'Client',name:row.id}],client_removed:false},auth);
  assert.equal((await a.request(`/api/v1/targets/${row.id}`)).body.status, 'attention');
  assert.equal((await f.heartbeat(a, row)).body.command, null, 'uncertain client cleanup is never replayed');
});
