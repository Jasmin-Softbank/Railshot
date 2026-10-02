import { lstatSync, readFileSync } from 'node:fs';
import { APP_NAME, TARGET_ID } from './contract.js';

const STALE_SECONDS = 90;
const scopes = { pods: 'app_pods', node_up: 'target_node', cpu_percent: 'target_node', memory_percent: 'target_node',
  disk_percent: 'target_node', network_receive_bytes_per_second: 'target_node', network_transmit_bytes_per_second: 'target_node', http: 'app_probe' };
const dns = /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/;
const instance = /^[a-zA-Z0-9.-]+:[0-9]{1,5}$/;
function safeUrl(value) {
  const url = new URL(value);
  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.search || url.hash) throw new Error('Invalid observer URL');
  return url;
}
function configuration(path) {
  const info = lstatSync(path);
  if (!path.startsWith('/') || !info.isFile() || info.uid !== process.getuid() || (info.mode & 0o077) || info.size > 131072) throw new Error('Private observer configuration required');
  const config = JSON.parse(readFileSync(path, 'utf8'));
  if (config.version !== 1 || !Array.isArray(config.targets) || config.targets.length > 100) throw new Error('Invalid observer targets');
  const seen = new Set();
  for (const target of config.targets) {
    const key = `${target.target_id}:${target.app}`;
    if (!TARGET_ID.test(target.target_id || '') || !APP_NAME.test(target.app || '') || !dns.test(target.namespace || '') ||
        !instance.test(target.node_instance || '') || !instance.test(target.cluster_instance || '') || seen.has(key)) throw new Error('Invalid observer binding');
    safeUrl(target.prometheus_url); safeUrl(target.probe_url); seen.add(key);
  }
  let collector;
  if (config.collector) {
    const { id, lifecycle, expires_at } = config.collector;
    if (!TARGET_ID.test(id || '') || !['acceptance', 'shared'].includes(lifecycle) ||
        !Number.isFinite(Date.parse(expires_at))) throw new Error('Invalid observer lifecycle');
    collector = { id, role: 'shared_observer', lifecycle, expires_at };
  }
  return { targets: config.targets, collector };
}
const selector = (job, address) => `{job=${JSON.stringify(job)},instance=${JSON.stringify(address)}}`;
const named = (name, expression) => `label_replace((${expression}), "railshot_metric", "${name}", "", "")`;
function queries(target) {
  const node = selector('node', target.node_instance), cluster = selector('cluster', target.cluster_instance), http = selector('http', target.probe_url);
  const disk = `${node.slice(0, -1)},mountpoint="/"}`;
  const network = `${node.slice(0, -1)},device!="lo"}`;
  const pod = `kube_pod_status_phase{job="cluster",instance=${JSON.stringify(target.cluster_instance)},namespace=${JSON.stringify(target.namespace)},phase="Running"}`;
  const podLabels = `kube_pod_labels{job="cluster",instance=${JSON.stringify(target.cluster_instance)},namespace=${JSON.stringify(target.namespace)},label_app_kubernetes_io_name=${JSON.stringify(target.app)},label_railshot_io_target=${JSON.stringify(target.target_id)}}`;
  const group = (labels, values) => [named('up', `up${labels}`), named('observed', `timestamp(up${labels})`),
    ...Object.entries(values).flatMap(([name, [value, timestamp]]) => [named(name, value), named(`${name}_observed`, timestamp)])].join(' or ');
  return [
    { names: Object.keys(scopes).filter((name) => scopes[name] === 'target_node'), query: group(node, {
      node_up: [`up${node}`, `timestamp(up${node})`],
      cpu_percent: [`100 * (1 - avg(rate(node_cpu_seconds_total${node.slice(0, -1)},mode="idle"}[2m])))`, `min(timestamp(node_cpu_seconds_total${node}))`],
      memory_percent: [`100 * (1 - node_memory_MemAvailable_bytes${node} / node_memory_MemTotal_bytes${node})`, `min(timestamp(${node.slice(0, -1)},__name__=~"node_memory_MemAvailable_bytes|node_memory_MemTotal_bytes"}))`],
      disk_percent: [`max(100 * (1 - node_filesystem_avail_bytes${disk} / node_filesystem_size_bytes${disk}))`, `min(timestamp(${disk.slice(0, -1)},__name__=~"node_filesystem_avail_bytes|node_filesystem_size_bytes"}))`],
      network_receive_bytes_per_second: [`sum(rate(node_network_receive_bytes_total${network}[2m]))`, `min(timestamp(node_network_receive_bytes_total${network}))`],
      network_transmit_bytes_per_second: [`sum(rate(node_network_transmit_bytes_total${network}[2m]))`, `min(timestamp(node_network_transmit_bytes_total${network}))`],
    }) },
    { names: ['pods'], query: group(cluster, { pods: [`sum(${pod} and on(namespace,pod,uid) ${podLabels})`, `min(timestamp(${pod}) and on(namespace,pod,uid) ${podLabels})`] }) },
    { names: ['http'], query: group(http, { http: [`probe_success${http}`, `timestamp(probe_success${http})`] }) },
  ].filter(({ names }) => target.app || names[0] !== 'pods' && names[0] !== 'http');
}
async function query(url, expression, fetchImpl) {
  const endpoint = new URL(`${url.replace(/\/$/, '')}/api/v1/query`);
  endpoint.searchParams.set('query', expression);
  endpoint.searchParams.set('timeout', '4s');
  const response = await fetchImpl(endpoint, { signal: AbortSignal.timeout(5000), redirect: 'error' });
  if (!response.ok) throw new Error('Observer request failed');
  const chunks = []; let size = 0;
  for await (const chunk of response.body) {
    size += chunk.length;
    if (size > 65536) throw new Error('Observer response too large');
    chunks.push(chunk);
  }
  const result = JSON.parse(Buffer.concat(chunks).toString('utf8'));
  if (result.status !== 'success' || result.data?.resultType !== 'vector' || !Array.isArray(result.data.result)) throw new Error('Invalid observer response');
  const values = new Map();
  for (const row of result.data.result) {
    const name = row.metric?.railshot_metric, value = Number(row.value?.[1]);
    if (typeof name !== 'string' || values.has(name) || typeof row.value?.[1] !== 'string' || !row.value[1].trim()) throw new Error('Ambiguous observer sample');
    // Undefined arithmetic (for example a zero-sized filesystem) does not erase sibling metrics.
    values.set(name, Number.isFinite(value) ? value : undefined);
  }
  return values;
}

// Read-only operator binding: neither a public URL proxy nor a deployment success gate.
export function createMetricsObserver({ configPath, fetchImpl = fetch, now = Date.now } = {}) {
  return async (record) => {
    const checked = now();
    const metric = (name, state, value = null, observed_at = null) => ({ state, value, observed_at, scope: scopes[name] });
    const result = { deployment_id: record.id ?? null, target_id: record.target_id, app: record.app ?? null,
      checked_at: new Date(checked).toISOString(), stale_after_seconds: STALE_SECONDS,
      metrics: Object.fromEntries(Object.keys(scopes).map((name) => [name, metric(name, 'not_configured')])) };
    if (!configPath) return result;
    let config;
    try { config = configuration(configPath); }
    catch { for (const name of Object.keys(scopes)) result.metrics[name] = metric(name, 'unavailable'); return result; }
    if (config.collector) {
      result.collector = config.collector;
      if (Date.parse(config.collector.expires_at) <= checked) {
        for (const name of Object.keys(scopes)) result.metrics[name] = metric(name, 'unavailable');
        return result;
      }
    }
    const bindings = config.targets.filter((item) => item.target_id === record.target_id && (!record.app || item.app === record.app));
    // A node-only request cannot choose an arbitrary app or an ambiguous physical target.
    if (!record.app && new Set(bindings.map((item) => `${item.prometheus_url}|${item.node_instance}`)).size > 1) {
      for (const name of Object.keys(scopes)) result.metrics[name] = metric(name, 'unavailable');
      return result;
    }
    const target = bindings[0] && { ...bindings[0], app: record.app ?? null };
    if (!record.app) for (const name of ['pods', 'http']) result.metrics[name] = metric(name, 'unsupported');
    if (!target) { for (const name of Object.keys(scopes)) result.metrics[name] = metric(name, 'unsupported'); return result; }
    await Promise.all(queries(target).map(async ({ names, query: expression }) => {
      try {
        const values = await query(target.prometheus_url, expression, fetchImpl);
        for (const name of names) {
          const times = values.get('up') === 0 ? [values.get('observed')] : [values.get('observed'), values.get(`${name}_observed`)];
          const timestamp = times.every((time) => Number.isFinite(time) && time >= 0 && time <= 8640000000000) ? Math.min(...times) * 1000 : null;
          const fresh = timestamp !== null && checked - timestamp <= STALE_SECONDS * 1000 && timestamp <= checked + 5000;
          const value = values.get(name);
          const state = ![0, 1].includes(values.get('up')) || timestamp === null ? 'no_data' : !fresh ? 'stale' : values.get('up') === 0 ? 'collection_failed'
            : value === undefined || value < 0 || (['http', 'node_up'].includes(name) ? ![0, 1].includes(value) : name.endsWith('percent') && value > 100) ? 'no_data' : 'ready';
          result.metrics[name] = metric(name, state, state === 'ready' ? value : null, timestamp === null ? null : new Date(timestamp).toISOString());
        }
      } catch { for (const name of names) result.metrics[name] = metric(name, 'unavailable'); }
    }));
    return result;
  };
}
