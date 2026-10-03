import test from 'node:test';
import assert from 'node:assert/strict';
import { once } from 'node:events';
import { randomUUID } from 'node:crypto';
import { setTimeout as pause } from 'node:timers/promises';
import { createAppServer } from '../src/server.js';
import { apiAccessConfig } from '../src/access.js';

async function fixture(t, product, releaseLeaseMs = 1000) {
  const token = 'release-test-token-'.repeat(3);
  const server = createAppServer({ product, access: apiAccessConfig({ RAILSHOT_API_TOKEN: token }), releaseLeaseMs });
  server.listen(0, '127.0.0.1'); await once(server, 'listening');
  t.after(() => server.shutdown());
  const origin = `http://127.0.0.1:${server.address().port}`;
  const headers = { authorization: `Bearer ${token}`, 'content-type': 'application/json' };
  return { server, get: (path) => fetch(origin + path, { headers }),
    prepare: (id = randomUUID(), auth = true) => fetch(origin + '/internal/releases/prepare', {
      method: 'POST', headers: auth ? headers : { 'content-type': 'application/json' }, body: JSON.stringify({ release_id: id }),
    }) };
}

test('release waits for in-flight requests, fences admissions atomically and recovers an abandoned lease', async (t) => {
  let finish, started;
  const entered = new Promise((resolve) => { started = resolve; });
  let paused = false, pauses = 0, resumed = 0, closed = 0;
  const f = await fixture(t, {
    async getDeploymentLogs() { started(); return new Promise((resolve) => { finish = () => resolve({ logs: [] }); }); },
    pauseForRelease() { paused = true; pauses++; return true; },
    resumeAfterRelease() { paused = false; resumed++; },
    close() { closed++; },
  }, 100);
  assert.equal((await f.prepare(undefined, false)).status, 401);
  assert.equal(pauses, 0);
  const pending = f.get('/api/v1/deployments/one/logs'); await entered;
  assert.equal((await f.prepare()).status, 202); assert.equal(pauses, 0);
  finish(); assert.equal((await pending).status, 200);
  const id = randomUUID();
  assert.equal((await f.prepare(id)).status, 200); assert.equal(paused, true);
  assert.equal((await f.prepare()).status, 409);
  const rejected = await f.get('/api/v1/options');
  assert.equal(rejected.status, 503);
  assert.deepEqual((await rejected.json()).error, { code: 'PLATFORM_UPDATING',
    message: '서버 업데이트를 마치고 요청을 이어서 처리합니다.', outcome_unknown: false, retryable: true });
  await pause(130);
  assert.equal(resumed, 1); assert.equal(paused, false);
  assert.equal((await f.get('/api/v1/options')).status, 200);
  await f.server.shutdown(); assert.equal(closed, 1);
});

test('release does not stop active native work; shutdown waits for durable store closure', async (t) => {
  let busy = true, closeDone = false;
  const f = await fixture(t, {
    pauseForRelease: () => !busy, resumeAfterRelease() {},
    async close() { await pause(15); closeDone = true; },
  });
  assert.equal((await f.prepare()).status, 202);
  assert.equal((await f.get('/api/v1/options')).status, 200);
  busy = false;
  assert.equal((await f.prepare()).status, 200);
  await f.server.shutdown(); assert.equal(closeDone, true);
});

test('the API entrypoint handles SIGTERM and exits after graceful closure', { timeout: 8000 }, async (t) => {
  const { spawn } = await import('node:child_process');
  const { fileURLToPath } = await import('node:url');
  const { mkdtemp, rm } = await import('node:fs/promises');
  const { tmpdir } = await import('node:os');
  const directory = await mkdtemp(`${tmpdir()}/railshot-signal-`);
  t.after(() => rm(directory, { recursive: true, force: true }));
  const child = spawn(process.execPath, [fileURLToPath(new URL('../src/server.js', import.meta.url))], {
    env: { PATH: process.env.PATH, HOME: process.env.HOME, PORT: '0', RAILSHOT_STATE_DIR: directory }, stdio: ['ignore', 'pipe', 'pipe'],
  });
  t.after(() => { if (child.exitCode === null && child.signalCode === null) child.kill('SIGKILL'); });
  const exited = once(child, 'exit');
  await new Promise((resolve, reject) => {
    child.stdout.on('data', (data) => { if (String(data).includes('RAILSHOT API listening')) resolve(); });
    child.once('error', reject);
    child.once('exit', () => reject(new Error('API exited before listening')));
  });
  child.kill('SIGTERM');
  assert.deepEqual(await exited, [0, null]);
});

test('an already deployed template never pauses service for a dashboard-only sync', async (t) => {
  const previous = process.env.RAILSHOT_POD_TEMPLATE_ID;
  process.env.RAILSHOT_POD_TEMPLATE_ID = 'a'.repeat(64);
  t.after(() => { if (previous === undefined) delete process.env.RAILSHOT_POD_TEMPLATE_ID; else process.env.RAILSHOT_POD_TEMPLATE_ID = previous; });
  const f = await fixture(t, { pauseForRelease() { assert.fail('current template must not pause'); }, close() {} });
  const response = await fetch(`http://127.0.0.1:${f.server.address().port}/internal/releases/prepare`, {
    method: 'POST', headers: { authorization: `Bearer ${'release-test-token-'.repeat(3)}`, 'content-type': 'application/json',
      'x-railshot-desired-template': 'a'.repeat(64) }, body: JSON.stringify({ release_id: randomUUID() }),
  });
  assert.equal(response.status, 200); assert.equal((await response.json()).status, 'current');
  assert.equal((await f.get('/api/v1/options')).status, 200);
  assert.equal((await f.get('/readyz')).status, 200);
});

test('pre-sync waits for active work and refuses malformed or rejected preparation', async () => {
  const { prepareRelease } = await import('../src/prepare-release.js');
  const options = { token: 'private-test-token', templateId: 'a'.repeat(64), pollMs: 1, timeoutMs: 100 };
  const calls = [];
  await prepareRelease({ ...options, request: async (_url, request) => {
    calls.push(request);
    const { release_id } = JSON.parse(request.body);
    return calls.length === 1 ? Response.json({ status: 'busy' }, { status: 202 })
      : Response.json({ status: 'prepared', release_id, lease_ms: 120000 });
  } });
  assert.equal(calls.length, 2); assert.equal(calls[0].body, calls[1].body);
  assert.equal(calls[0].headers['x-railshot-desired-template'], options.templateId);
  for (const response of [() => new Response('Not found', { status: 404 }),
    () => Response.json({ status: 'prepared', release_id: 'wrong', lease_ms: 120000 }),
    () => Response.json({ status: 'busy' }, { status: 401 })]) {
    await assert.rejects(prepareRelease({ ...options, request: async () => response() }));
  }
  await assert.rejects(prepareRelease({ ...options, timeoutMs: 5,
    request: async () => Response.json({ status: 'busy' }, { status: 202 }) }), /API_HANDOVER_BUSY/);
});
