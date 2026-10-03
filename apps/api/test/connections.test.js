import test from 'node:test';
import assert from 'node:assert/strict';
import { randomBytes } from 'node:crypto';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { DatabaseSync } from 'node:sqlite';
import { createConnectionService, ConnectionError } from '../src/connections.js';
import { createAppServer } from '../src/server.js';
import { apiAccessConfig } from '../src/access.js';

const identity = { token: { user: { id: 'user1' }, project: { id: 'project1', name: 'test-cloud' }, roles: [{ id: 'member' }] } };
const tokenInput = { auth_type: 'token', project_id: 'project1', user_id: 'user1', token: 'scoped-secret' };
const credentialInput = { auth_type: 'application_credential', project_id: 'project1', user_id: 'user1',
  application_credential_id: 'credential1', application_credential_secret: 'credential-secret' };
function response(value, subject) {
  return new Response(JSON.stringify(value), { status: 200, headers: subject ? { 'X-Subject-Token': subject } : {} });
}

async function fixture(t, fetcher, extra = {}) {
  const directory = await mkdtemp(join(tmpdir(), 'railshot-connections-'));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const settings = { directory, authUrl: 'https://keystone.example.test/v3',
    encryptionKey: randomBytes(32).toString('base64'), projectId: 'project1', fetcher, ...extra };
  const service = await createConnectionService(settings);
  t.after(() => service.close());
  return { service, directory, settings };
}

test('user-provided project token is verified and stored encrypted without creating a credential', async (t) => {
  const calls = [];
  const { service, directory } = await fixture(t, async (url, options) => {
    calls.push({ url, options });
    assert.equal(new URL(url).pathname, '/v3/auth/tokens');
    assert.equal(options.method, 'GET');
    assert.equal(options.headers['X-Auth-Token'], 'scoped-secret');
    assert.equal(options.headers['X-Subject-Token'], 'scoped-secret');
    return response(identity);
  });
  const result = await service.register(tokenInput);
  assert.equal(result.user_id, 'user1');
  assert.equal(result.project_id, 'project1');
  assert.equal(result.auth_type, 'token');
  assert.match(result.connection_token, /^[A-Za-z0-9_-]{43}$/);
  assert.equal((await service.verify(result.connection_token)).project_id, 'project1');
  assert.equal(calls.length, 2);
  const bytes = await readFile(join(directory, 'connections.sqlite3'));
  assert.equal(bytes.includes('scoped-secret'), false);
  assert.equal(bytes.includes(result.connection_token), false);
});

test('user-provided Application Credential is verified without server-side credential creation', async (t) => {
  const calls = [];
  const { service, directory } = await fixture(t, async (url, options) => {
    calls.push({ url, options });
    assert.equal(new URL(url).pathname, '/v3/auth/tokens');
    assert.equal(options.method, 'POST');
    assert.deepEqual(JSON.parse(options.body).auth.identity.application_credential,
      { id: 'credential1', secret: 'credential-secret' });
    return response(identity, 'issued-token');
  });
  const result = await service.register(credentialInput);
  assert.equal(result.auth_type, 'application_credential');
  assert.equal((await service.verify(result.connection_token)).user_id, 'user1');
  assert.equal(calls.length, 2);
  const bytes = await readFile(join(directory, 'connections.sqlite3'));
  assert.equal(bytes.includes('credential-secret'), false);
});

test('claimed identity, operator project, and malformed credentials are rejected', async (t) => {
  let calls = 0;
  let unscoped = false;
  const { service } = await fixture(t, async () => { calls++; return response(unscoped ? { token: { user: { id: 'user1' } } } : identity); });
  await assert.rejects(service.register({ ...tokenInput, user_id: 'another-user' }), { status: 403 });
  await assert.rejects(service.register({ ...tokenInput, project_id: 'another-project' }), { status: 403 });
  await assert.rejects(service.register({ ...tokenInput, token: 'bad\nheader' }), { status: 422 });
  await assert.rejects(service.register({ ...credentialInput, application_credential_secret: '' }), { status: 422 });
  await assert.rejects(service.register({ unscoped_token: 'legacy' }), { status: 422 });
  unscoped = true;
  await assert.rejects(service.register(tokenInput), { code: 'INVALID_OPENSTACK_CREDENTIALS' });
  assert.equal(calls, 2);
});

test('expired project token cannot authorize deployment and existing schema migrates', async (t) => {
  const directory = await mkdtemp(join(tmpdir(), 'railshot-connections-'));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const old = new DatabaseSync(join(directory, 'connections.sqlite3'));
  old.exec(`CREATE TABLE connections (id TEXT PRIMARY KEY, token_hash TEXT UNIQUE NOT NULL,
    project_id TEXT NOT NULL, project_name TEXT NOT NULL, credential_id TEXT NOT NULL,
    credential_ciphertext TEXT NOT NULL, created_at TEXT NOT NULL)`);
  old.close();
  let expired = false;
  const service = await createConnectionService({ directory, authUrl: 'https://keystone.example.test/v3',
    encryptionKey: randomBytes(32).toString('base64'), projectId: 'project1',
    fetcher: async () => expired ? new Response('{}', { status: 401 }) : response(identity) });
  t.after(() => service.close());
  const registered = await service.register(tokenInput);
  expired = true;
  await assert.rejects(service.verify(registered.connection_token), { code: 'INVALID_OPENSTACK_CREDENTIALS' });
});

test('OpenStack deployment checks a registered identity against the configured project before dispatch', async (t) => {
  let dispatched = 0;
  const apiToken = 'private-api-token-0123456789abcdef';
  const server = createAppServer({
    access: apiAccessConfig({ RAILSHOT_API_TOKEN: apiToken }),
    openstackProjectId: 'project1', target: { provider: 'openstack' }, service: { targetId: 'openstack-main' },
    connectionService: {
      register: async (input) => ({ id: 'new-id', provider: 'openstack', project_id: input.project_id,
        user_id: input.user_id, auth_type: input.auth_type, project_name: 'test', connection_token: 'a'.repeat(43) }),
      verify: async (token) => {
        if (!token) throw new ConnectionError(422, 'INVALID_INPUT', '기존 연결 토큰을 확인하세요.');
        return { project_id: token === 'correct' ? 'project1' : 'another-project', user_id: 'user1' };
      },
    },
    product: { targets: () => [{ id: 'openstack-extra', provider: 'openstack' }],
      createDeployment: async () => { dispatched++; return { id: 'deployment1', status: 'queued' }; } },
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  t.after(() => new Promise((resolve) => server.close(resolve)));
  const base = `http://127.0.0.1:${server.address().port}`;
  const form = (token) => {
    const body = new FormData();
    body.set('environment', 'onprem'); body.set('provider', 'openstack');
    body.set('repository_url', 'https://github.com/example/demo');
    if (token) body.set('connection_token', token);
    return body;
  };
  const headers = { Authorization: `Bearer ${apiToken}`, 'Idempotency-Key': 'test-key' };
  const register = await fetch(`${base}/api/v1/identities`, { method: 'POST',
    headers: { Authorization: `Bearer ${apiToken}`, 'Content-Type': 'application/json' },
    body: JSON.stringify(tokenInput) });
  assert.equal(register.status, 201);
  assert.equal((await register.json()).user_id, 'user1');
  const deploy = (token) => fetch(`${base}/api/v1/deployments`, { method: 'POST', headers, body: form(token) });
  assert.equal((await deploy()).status, 422);
  assert.equal((await deploy('wrong')).status, 403);
  assert.equal(dispatched, 0);
  assert.equal((await deploy('correct')).status, 202);
  assert.equal(dispatched, 1);
  const direct = (token) => {
    const body = new FormData();
    body.set('app', 'demo'); body.set('target_id', 'openstack-extra');
    body.set('repository_url', 'https://github.com/example/demo');
    if (token) body.set('connection_token', token);
    return fetch(`${base}/api/v1/deployments`, { method: 'POST', headers, body });
  };
  assert.equal((await direct()).status, 422);
  assert.equal((await direct('correct')).status, 202);
  assert.equal(dispatched, 2);
});
