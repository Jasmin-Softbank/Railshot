import test from 'node:test';
import assert from 'node:assert/strict';
import { once } from 'node:events';
import { createAppServer } from '../src/server.js';
import { ProductError } from '../src/product.js';

test('application observation is a bounded read endpoint and preserves missing versus failed collection', async (t) => {
  let reads = 0;
  const product = { async getApplicationObservation(id, session) {
    assert.equal(session, null);
    if (id !== 'app-example') throw new ProductError(404, 'NOT_FOUND', '앱 등록을 찾을 수 없습니다.');
    reads++;
    return { application_id: id, deployment_id: 'deployment-1', state: 'ready', checked_at: new Date().toISOString(),
      reason: 'WORKLOAD_MISSING', workload: { state: 'missing', pods: [], code: 'WORKLOAD_MISSING' },
      public_http: { state: 'unverified', verified_at: null, url: null } };
  } };
  const server = createAppServer({ product, service: null });
  server.listen(0, '127.0.0.1'); await once(server, 'listening');
  t.after(() => new Promise(resolve => { server.close(resolve); server.closeAllConnections(); }));
  const base = `http://127.0.0.1:${server.address().port}/api/v1/applications/`;
  const response = await fetch(base + 'app-example/observations');
  assert.equal(response.status, 200); assert.equal((await response.json()).workload.state, 'missing');
  assert.equal((await fetch(base + 'foreign/observations')).status, 404);
  assert.equal((await fetch(base + 'app-example/observations?target=foreign')).status, 422);
  assert.equal((await fetch(base + 'app-example/observations', { method: 'POST' })).status, 405);
  assert.equal(reads, 1);
});
