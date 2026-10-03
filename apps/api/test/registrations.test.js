import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { DatabaseSync } from 'node:sqlite';
import { createAppServer } from '../src/server.js';
import { apiAccessConfig } from '../src/access.js';
import { createProductStore } from '../src/product-store.js';

const bearer = 'operator-registration-test-token-0123456789';
const input = { provider: 'openstack', project_id: 'project-1', user_id: 'user-1', auth_type: 'application_credential' };
const json = (body) => ({ method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) });

test('operator-authorized registration persists, isolates sessions and issues a hashed one-time token', async (t) => {
  const directory = await mkdtemp(join(tmpdir(), 'railshot-registration-'));
  let store = await createProductStore(directory);
  let server;
  const access = apiAccessConfig({ RAILSHOT_API_TOKEN: bearer });
  async function start() {
    server = createAppServer({ product: { dashboard: store.dashboard, registrations: store.registrations }, access });
    await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
    return `http://127.0.0.1:${server.address().port}`;
  }
  async function stop() { await new Promise((resolve) => server.close(resolve)); await store.close(); }
  let base = await start();
  t.after(async () => { if (server.listening) await stop(); await rm(directory, { recursive: true, force: true }); });
  const client = () => {
    let cookie;
    return async (path, options = {}) => {
      const response = await fetch(base + path, { ...options, headers: {
        authorization: `Bearer ${bearer}`, ...(cookie ? { cookie } : {}), ...options.headers,
      } });
      if (response.headers.get('set-cookie')) cookie = response.headers.get('set-cookie').split(';')[0];
      return { response, body: await response.json() };
    };
  };
  const a = client(), b = client();
  assert.equal((await fetch(base + '/api/v1/registrations')).status, 401);
  const created = await a('/api/v1/registrations', json(input));
  assert.equal(created.response.status, 201);
  const id = created.body.id;
  assert.equal(created.response.headers.get('location'), `/api/v1/registrations/${id}`);
  assert.equal(created.body.status, 'pending');
  assert.equal((await b(`/api/v1/registrations/${id}`)).response.status, 404);
  assert.deepEqual((await b('/api/v1/registrations')).body.items, []);
  assert.equal((await b(`/api/v1/registrations/${id}/tokens`, json({}))).response.status, 404);
  assert.equal((await a('/api/v1/registrations', json({ ...input, token: 'keystone-secret' }))).response.status, 422);
  const issued = await a(`/api/v1/registrations/${id}/tokens`, json({}));
  assert.equal(issued.response.status, 201);
  assert.match(issued.body.token, /^rsl_[A-Za-z0-9_-]{43}$/);
  assert.equal((await a(`/api/v1/registrations/${id}/tokens`, json({}))).response.status, 409);
  await stop();
  const db = new DatabaseSync(join(directory, 'dashboard.sqlite3'));
  try {
    const row = db.prepare('SELECT * FROM registration_tokens WHERE registration_id = ?').get(id);
    assert.match(row.token_hash, /^[a-f0-9]{64}$/);
    assert.equal(JSON.stringify(row).includes(issued.body.token), false);
    assert.equal(db.prepare('PRAGMA user_version').get().user_version, 2);
    db.prepare('UPDATE registration_tokens SET expires_at = ? WHERE registration_id = ?').run(Date.now() - 1, id);
  } finally { db.close(); }
  store = await createProductStore(directory); base = await start();
  assert.equal((await a(`/api/v1/registrations/${id}`)).body.project_id, input.project_id);
  assert.equal((await b(`/api/v1/registrations/${id}`)).response.status, 404);
  assert.equal(store.registrations.claim(issued.body.token), null);
  const renewed = await a(`/api/v1/registrations/${id}/tokens`, json({}));
  assert.equal(renewed.response.status, 201);
  assert.notEqual(renewed.body.token, issued.body.token);
  assert.equal(store.registrations.claim(renewed.body.token)?.status, 'claimed');
  assert.equal(store.registrations.claim(renewed.body.token), null);
  assert.equal(store.registrations.claim(issued.body.token), null);
  assert.equal((await a(`/api/v1/registrations/${id}/tokens`, json({}))).response.status, 409);
});

test('public demo cannot issue linkage tokens without an operator Bearer token', async (t) => {
  const directory = await mkdtemp(join(tmpdir(), 'railshot-registration-public-'));
  const store = await createProductStore(directory);
  const access = apiAccessConfig({ RAILSHOT_PUBLIC_DEMO: '1', RAILSHOT_BIND_HOST: '0.0.0.0',
    RAILSHOT_ALLOWED_HOSTS: '127.0.0.1', RAILSHOT_ALLOWED_ORIGINS: 'http://127.0.0.1' });
  const server = createAppServer({ product: { dashboard: store.dashboard, registrations: store.registrations }, access });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  t.after(async () => { await new Promise((resolve) => server.close(resolve)); await store.close(); await rm(directory, { recursive: true, force: true }); });
  const response = await fetch(`http://127.0.0.1:${server.address().port}/api/v1/registrations`, json(input));
  assert.equal(response.status, 503);
  assert.deepEqual(store.registrations.list('missing'), []);
});
