import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, writeFile, rm, chmod } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createMetricsObserver } from '../src/metrics.js';

test('bound observations expire, fail closed, and never expose backend details', async (t) => {
  const directory = await mkdtemp(join(tmpdir(), 'railshot-metrics-'));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const configPath = join(directory, 'observer.json');
  const config = { version: 1, targets: [{ target_id: 'demo', app: 'demo-app', namespace: 'tenant-demo-app',
    prometheus_url: 'http://observer.internal:9090', node_instance: '10.0.0.1:30910', cluster_instance: '10.0.0.1:30081', probe_url: 'https://app.example.test/health' }] };
  await writeFile(configPath, JSON.stringify(config), { mode: 0o600 });
  const record = { id: 'deployment-1', target_id: 'demo', app: 'demo-app', public_http: { url: 'http://evil.invalid' } };
  const now = 1800000000000;
  let age = 20, up = 1, fail = false, empty = false, calls = 0, http = 1;
  const queries = [];
  const observe = createMetricsObserver({ configPath, now: () => now, fetchImpl: async (url, options) => {
    calls++; queries.push(url.searchParams.get('query'));
    assert.equal(url.origin, 'http://observer.internal:9090'); assert.equal(options.redirect, 'error');
    if (fail) throw new Error('private secret endpoint detail');
    const samples = { up, observed: now / 1000 - age, cpu_percent: 12.5, memory_percent: 30, pods: 2, http,
      cpu_percent_observed: now / 1000 - age, memory_percent_observed: now / 1000 - age, pods_observed: now / 1000 - age, http_observed: now / 1000 - age };
    return Response.json({ status: 'success', data: { resultType: 'vector', result: empty ? [] : Object.entries(samples).map(([name, value]) => ({ metric: { railshot_metric: name }, value: [now / 1000, String(value)] })) } });
  } });
  const fresh = await observe(record);
  assert.equal(fresh.deployment_id, record.id); assert.equal(fresh.metrics.pods.value, 2);
  assert.equal(fresh.metrics.cpu_percent.scope, 'target_node'); assert.equal(fresh.metrics.http.state, 'ready');
  assert.match(queries[1], /namespace="tenant-demo-app"/);
  assert.match(queries[0], /timestamp\(up/); assert.ok(!JSON.stringify(fresh).includes('internal'));
  http = 0; assert.equal((await observe(record)).metrics.http.value, 0, 'probe failure differs from collection failure');
  age = 100; assert.equal((await observe(record)).metrics.http.state, 'stale');
  assert.equal((await observe(record)).metrics.pods.value, null);
  age = 0; up = 0; assert.equal((await observe(record)).metrics.pods.state, 'collection_failed');
  up = 1; empty = true; assert.equal((await observe(record)).metrics.pods.state, 'no_data');
  empty = false; fail = true; assert.equal((await observe(record)).metrics.pods.state, 'unavailable');
  const before = calls;
  assert.equal((await observe({ ...record, app: 'other-app' })).metrics.pods.state, 'unsupported');
  assert.equal(calls, before, 'unregistered app cannot query Prometheus');
  assert.equal((await createMetricsObserver() (record)).metrics.pods.state, 'not_configured');
  await chmod(configPath, 0o644);
  assert.equal((await observe(record)).metrics.pods.state, 'unavailable');
  assert.equal((await createMetricsObserver({ configPath })(record)).metrics.pods.state, 'unavailable');
});
