import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, readFile, rm, unlink } from 'node:fs/promises';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { createHash } from 'node:crypto';
import { DatabaseSync } from 'node:sqlite';
import { createAppServer } from '../src/server.js';
import { apiAccessConfig } from '../src/access.js';
import { createProductStore } from '../src/product-store.js';

const secret = 'test-only-mcp-internal-credential-0123456789';
const resource = 'https://railshot.example/mcp';
const bearer = 't'.repeat(43), tokenHash = createHash('sha256').update(bearer).digest('hex');

async function fixture(t) {
  const directory = await mkdtemp(join(tmpdir(), 'railshot-mcp-session-'));
  let server, base;
  async function start() {
    server = createAppServer({ stateDirectory: directory, service: null,
      access: apiAccessConfig({ RAILSHOT_API_TOKEN: secret, RAILSHOT_PUBLIC_DEMO: '1',
        RAILSHOT_ALLOWED_HOSTS: '127.0.0.1', RAILSHOT_ALLOWED_ORIGINS: 'http://127.0.0.1' }) });
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    await server.productReady;
    base = `http://127.0.0.1:${server.address().port}`;
  }
  await start();
  t.after(async () => { await server.shutdown(); await rm(directory, { recursive: true, force: true }); });
  return { directory, async restart() { await server.shutdown(); await start(); },
    async session() {
      const response = await fetch(base + '/api/v1/sessions', { method: 'POST' });
      return /railshot_session=([^;]+)/.exec(response.headers.get('set-cookie'))[1];
    },
    async request(input, { lookup = false, authorization = `Bearer ${secret}`, method = 'POST' } = {}) {
      return fetch(base + `/internal/mcp/tokens${lookup ? '/lookup' : ''}`, { method,
        headers: { authorization, 'content-type': 'application/json' }, ...(method === 'POST' ? { body: JSON.stringify(input) } : {}) });
    },
  };
}

test('internal MCP binding survives API restart, encrypts session cookies and rejects owner/resource changes', async t => {
  const f = await fixture(t), session = await f.session(), other = await f.session();
  const input = { token_hash: tokenHash, session, client_id: 'registered-client', resource };
  assert.equal((await f.request(input, { authorization: '' })).status, 401, 'public demo does not expose private token registration');
  assert.equal((await f.request(input, { method: 'GET' })).status, 405);
  assert.equal((await f.request(input)).status, 200);
  assert.equal((await f.request(input)).status, 200, 'same binding is idempotent');
  assert.equal((await f.request({ ...input, session: other })).status, 409);
  assert.equal((await f.request({ ...input, client_id: 'different-client' })).status, 409);
  assert.equal((await f.request({ ...input, resource: 'https://other.example/mcp' })).status, 409);
  assert.equal((await f.request({ ...input, token_hash: 'a'.repeat(64), session: 'f'.repeat(43) })).status, 409);
  assert.equal((await f.request({ ...input, resource: 'https://user:password@railshot.example/mcp' })).status, 422);
  const lookup = { token_hash: tokenHash, resource };
  assert.equal((await f.request(lookup, { lookup: true, authorization: '' })).status, 401);
  assert.equal((await f.request({ ...lookup, resource: 'https://other.example/mcp' }, { lookup: true })).status, 404);
  const db = new DatabaseSync(join(f.directory, 'dashboard.sqlite3'));
  try {
    const stored = db.prepare('SELECT * FROM mcp_tokens').get();
    assert.equal(stored.token_hash, tokenHash);
    assert.equal(stored.session_id, createHash('sha256').update(session).digest('hex'));
    assert.equal(Buffer.from(stored.session_encrypted).includes(Buffer.from(session)), false);
    assert.equal(JSON.stringify(stored).includes(bearer), false);
    assert.equal(db.prepare('SELECT count(*) AS count FROM mcp_tokens').get().count, 1);
    await f.restart();
    const restored = await (await f.request(lookup, { lookup: true })).json();
    assert.equal(restored.session, session);
    assert.equal(restored.client_id, input.client_id);
    assert.equal(restored.resource, resource);
    db.prepare('UPDATE sessions SET expires_at = 0 WHERE id = ?').run(stored.session_id);
    assert.equal((await f.request(lookup, { lookup: true })).status, 404, 'expired sessions cannot recover a bearer binding');
    assert.equal((await f.request(input)).status, 409);
  } finally { db.close(); }
});

test('MCP ciphertext is bound to client identity; missing encryption key fails closed on restart', async t => {
  const directory = await mkdtemp(join(tmpdir(), 'railshot-mcp-key-'));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const store = await createProductStore(directory);
  const session = store.dashboard.session().token;
  store.dashboard.saveMcpToken({ token_hash: tokenHash, session, client_id: 'client', resource });
  const db = new DatabaseSync(join(directory, 'dashboard.sqlite3'));
  try {
    db.prepare('UPDATE mcp_tokens SET client_id = ?').run('tampered');
    assert.throws(() => store.dashboard.mcpToken({ token_hash: tokenHash, resource }));
  } finally { db.close(); await store.close(); }
  const bytes = await readFile(join(directory, 'dashboard.sqlite3'));
  assert.equal(bytes.includes(Buffer.from(session)), false);
  assert.equal(bytes.includes(Buffer.from(bearer)), false);
  await unlink(join(directory, 'connections.key'));
  await assert.rejects(createProductStore(directory), /encryption key is missing/);
});
