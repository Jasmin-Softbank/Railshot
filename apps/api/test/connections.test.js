import test from 'node:test';
import assert from 'node:assert/strict';
import { randomBytes } from 'node:crypto';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createConnectionService, ConnectionError } from '../src/connections.js';
import { createAppServer } from '../src/server.js';
import { apiAccessConfig } from '../src/access.js';

function response(value, token) {
  return new Response(JSON.stringify(value), {
    status: 200,
    headers: token ? { 'X-Subject-Token': token } : {},
  });
}

test('unscoped OpenStack token registers the configured project without persisting either input or issued token', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'railshot-connections-'));
  const encryptionKey = randomBytes(32).toString('base64');
  const calls = [];
  const fetcher = async (url, options) => {
    calls.push({ url, options });
    const path = new URL(url).pathname;
    if (path === '/v3/auth/tokens' && options.method === 'GET') return response({ token: { user: { id: 'user1' } } });
    if (path === '/v3/auth/projects') {
      return response({ projects: [{ id: 'project1', name: 'test-cloud' }, { id: 'other-project' }] });
    }
    if (path === '/v3/auth/tokens' && options.method === 'POST') {
      return response({ token: {
        project: { id: 'project1' }, user: { id: 'user1' }, roles: [{ id: 'admin' }],
      } }, 'scoped-token');
    }
    if (path === '/v3/users/user1/application_credentials') {
      return response({ application_credential: { id: 'credential1', secret: 'credential-secret' } });
    }
    assert.fail(`Unexpected ${url}`);
  };
  let service;
  try {
    const settings = { directory, authUrl: 'https://keystone.example.test/v3', encryptionKey, projectId: 'project1', fetcher };
    service = await createConnectionService(settings);
    const result = await service.register({ unscoped_token: 'unscoped-secret' });
    assert.equal(result.project_name, 'test-cloud');
    assert.match(result.connection_token, /^[A-Za-z0-9_-]{43}$/);
    assert.equal(calls[0].options.headers['X-Subject-Token'], 'unscoped-secret');
    assert.deepEqual(JSON.parse(calls[2].options.body).auth.scope, { project: { id: 'project1' } });
    assert.equal(service.resolve(result.connection_token).credential_secret, 'credential-secret');
    assert.equal((await service.verify(result.connection_token)).project_id, 'project1');
    assert.deepEqual(JSON.parse(calls[4].options.body).auth.identity.application_credential,
      { id: 'credential1', secret: 'credential-secret' });
    const bytes = await readFile(join(directory, 'connections.sqlite3'));
    for (const secret of ['unscoped-secret', 'credential-secret', result.connection_token]) {
      assert.equal(bytes.includes(secret), false);
    }
    assert.throws(() => service.resolve('invalid'), { status: 422 });
    service.close();
    service = null;
    service = await createConnectionService(settings);
    assert.equal(service.resolve(result.connection_token).project_id, 'project1');
  } finally {
    service?.close();
    await rm(directory, { recursive: true, force: true });
  }
});

test('scoped token and ambiguous project access cannot register', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'railshot-connections-'));
  const encryptionKey = randomBytes(32).toString('base64');
  let responseValue = { token: { user: { id: 'user1' }, project: { id: 'project1' } } };
  const fetcher = async (url) => {
    const value = new URL(url).pathname.endsWith('/auth/projects')
      ? { projects: [{ id: 'one' }, { id: 'two' }] }
      : responseValue;
    return response(value);
  };
  let service;
  try {
    service = await createConnectionService({ directory, authUrl: 'https://keystone.example.test/v3', encryptionKey, fetcher });
    await assert.rejects(service.register({ unscoped_token: 'candidate' }), { status: 422 });
    responseValue = { token: { user: { id: 'user1' } } };
    await assert.rejects(service.register({ unscoped_token: 'candidate' }), { status: 422 });
  } finally {
    service?.close();
    await rm(directory, { recursive: true, force: true });
  }
});

test('OpenStack deployment checks a connection token against the configured project before dispatch', async () => {
  let dispatched = 0;
  const apiToken = 'private-api-token-0123456789abcdef';
  const server = createAppServer({
    access: apiAccessConfig({ RAILSHOT_API_TOKEN: apiToken }),
    openstackProjectId: 'project1',
    target: { provider: 'openstack' },
    service: { targetId: 'openstack-main' },
    connectionService: {
      register: async () => ({ id: 'new-id', provider: 'openstack', project_name: 'test', connection_token: 'a'.repeat(43) }),
      verify: async (token) => {
        if (!token) throw new ConnectionError(422, 'INVALID_INPUT', '기존 연결 토큰을 확인하세요.');
        return { project_id: token === 'correct' ? 'project1' : 'another-project' };
      },
    },
    product: {
      createDeployment: async () => {
        dispatched++;
        return { id: 'deployment1', status: 'queued' };
      },
    },
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const base = `http://127.0.0.1:${server.address().port}`;
  const form = (token) => {
    const body = new FormData();
    body.set('environment', 'onprem');
    body.set('provider', 'openstack');
    body.set('repository_url', 'https://github.com/example/demo');
    if (token) body.set('connection_token', token);
    return body;
  };
  try {
    const headers = { Authorization: `Bearer ${apiToken}`, 'Idempotency-Key': 'test-key' };
    const register = await fetch(`${base}/api/v1/connections`, {
      method: 'POST',
      headers: { Authorization: `Bearer ${apiToken}`, 'Content-Type': 'application/json' },
      body: JSON.stringify({ unscoped_token: 'unscoped' }),
    });
    assert.equal(register.status, 201);
    assert.equal((await register.json()).connection_token, 'a'.repeat(43));
    const deploy = (token) => fetch(`${base}/api/v1/deployments`, { method: 'POST', headers, body: form(token) });
    assert.equal((await deploy()).status, 422);
    assert.equal((await deploy('wrong')).status, 403);
    assert.equal(dispatched, 0);
    assert.equal((await deploy('correct')).status, 202);
    assert.equal(dispatched, 1);
  } finally {
    await new Promise((resolve) => server.close(resolve));
  }
});
