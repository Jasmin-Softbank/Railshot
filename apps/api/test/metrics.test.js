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
  assert.match(queries[1], /label_app_kubernetes_io_name="demo-app"/);
  assert.match(queries[1], /label_railshot_io_target="demo"/);
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
  config.collector = { id: 'shared-observer', lifecycle: 'acceptance', expires_at: new Date(now - 1000).toISOString() };
  await writeFile(configPath, JSON.stringify(config));
  const expired = await observe(record);
  assert.equal(expired.collector.lifecycle, 'acceptance');
  assert.equal(expired.metrics.http.state, 'unavailable');
  assert.equal(calls, before, 'expired observer does not claim current successful measurements');
  await chmod(configPath, 0o644);
  assert.equal((await observe(record)).metrics.pods.state, 'unavailable');
  assert.equal((await createMetricsObserver({ configPath })(record)).metrics.pods.state, 'unavailable');
});

test('target node metrics preserve partial data, real zero, timestamps and node-only scope', async (t) => {
  const directory = await mkdtemp(join(tmpdir(), 'railshot-node-metrics-'));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const configPath = join(directory, 'observer.json'), now = 1800000000000;
  const binding = { target_id: 'demo', app: 'demo-app', namespace: 'tenant-demo-app', prometheus_url: 'http://observer.internal:9090',
    node_instance: '10.0.0.1:30910', cluster_instance: '10.0.0.1:30081', probe_url: 'https://demo.example.test' };
  await writeFile(configPath, JSON.stringify({ version: 1, targets: [binding] }), { mode: 0o600 });
  let failCluster = false, age = 10, up = 1, disk = '40', network = false, calls = 0;
  const observe = createMetricsObserver({ configPath, now: () => now, fetchImpl: async (url) => {
    calls++;
    const query = url.searchParams.get('query'), node = query.includes('job="node"'), cluster = query.includes('job="cluster"');
    if (cluster && failCluster) throw new Error('private cluster failure');
    if (node) {
      assert.match(query, /mountpoint="\/"/); assert.match(query, /device!="lo"/);
      assert.match(query, /rate\(node_network_receive_bytes_total/);
    }
    const metrics = node ? { node_up: String(up), cpu_percent: '0', memory_percent: '21', disk_percent: disk,
      ...(network ? { network_receive_bytes_per_second: '100', network_transmit_bytes_per_second: '0' } : {}) }
      : cluster ? { pods: '1' } : { http: '0' };
    const samples = { up: String(up), observed: String(now / 1000 - age) };
    for (const [name, value] of Object.entries(metrics)) Object.assign(samples, { [name]: value, [`${name}_observed`]: String(now / 1000 - age) });
    return Response.json({ status: 'success', data: { resultType: 'vector', result: Object.entries(samples).map(([name, value]) => ({ metric: { railshot_metric: name }, value: [now / 1000, value] })) } });
  } });
  const record = { target_id: 'demo', app: 'demo-app' };
  let result = await observe(record);
  assert.equal(result.deployment_id, null);
  assert.deepEqual(result.metrics.cpu_percent, { state: 'ready', value: 0, scope: 'target_node', observed_at: new Date(now - 10000).toISOString() });
  assert.equal(result.metrics.disk_percent.value, 40); assert.equal(result.metrics.node_up.value, 1);
  assert.equal(result.metrics.network_receive_bytes_per_second.state, 'no_data');
  assert.equal(result.metrics.network_receive_bytes_per_second.value, null);
  assert.equal(result.metrics.http.value, 0);
  failCluster = true; network = true;
  result = await observe(record);
  assert.equal(result.metrics.pods.state, 'unavailable'); assert.equal(result.metrics.disk_percent.state, 'ready');
  assert.equal(result.metrics.network_receive_bytes_per_second.value, 100); assert.equal(result.metrics.network_transmit_bytes_per_second.value, 0);
  disk = 'NaN'; result = await observe(record);
  assert.equal(result.metrics.disk_percent.state, 'no_data'); assert.equal(result.metrics.node_up.state, 'ready');
  disk = null; assert.equal((await observe(record)).metrics.node_up.state, 'unavailable', 'invalid protocol value cannot become a healthy zero');
  disk = '40'; age = -10; assert.equal((await observe(record)).metrics.node_up.state, 'stale');
  age = 91; up = 0; assert.equal((await observe(record)).metrics.node_up.state, 'stale', 'old scrape failure is also stale');
  age = 10; result = await observe(record);
  assert.equal(result.metrics.node_up.state, 'collection_failed'); assert.equal(result.metrics.node_up.value, null);
  up = 1; const before = calls;
  result = await observe({ target_id: 'demo' });
  assert.equal(calls - before, 1); assert.equal(result.app, null);
  assert.equal(result.metrics.node_up.state, 'ready'); assert.equal(result.metrics.pods.state, 'unsupported'); assert.equal(result.metrics.http.state, 'unsupported');
  await writeFile(configPath, JSON.stringify({ version: 1, targets: [binding, { ...binding, app: 'another-app', node_instance: '10.0.0.2:30910' }] }));
  const beforeAmbiguous = calls;
  assert.equal((await observe({ target_id: 'demo' })).metrics.node_up.state, 'unavailable');
  assert.equal(calls, beforeAmbiguous, 'ambiguous node-only binding never picks an arbitrary node');
});
