#!/usr/bin/env python3
"""Reconcile one registered runtime from an operator-pinned source release.

The caller authenticates the source archive/commit before invoking this program.
This executor binds approved runtime policy, existing ownership, and actual health
to a durable release receipt. It never provisions infrastructure, changes DNS, or retries
an incomplete mutation. Run each target in a separate process.
"""
import argparse
import base64
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time
from urllib.parse import urlencode, urlsplit
from urllib.request import ProxyHandler, Request, build_opener

import environment as env
import runtime_upgrade as upgrade

ROOT = Path(__file__).resolve().parents[2]


@contextmanager
def connection(request):
    node = request['inventory']['control_plane'][0]
    with env.ansible.forwarded_port(node['ssh'].get('transport_ref'), time.monotonic() + 1800) as port:
        host = next(iter(env.ansible.build_inventory(request, port)['all']['children']['k3s_server']['hosts'].values()))
        yield ['ssh', *shlex.split(host['ansible_ssh_common_args']), '-i', host['ansible_ssh_private_key_file'],
               '-p', str(host['ansible_port']), '-o', 'ConnectTimeout=15', host['ansible_user'] + '@' + host['ansible_host']]


def node_call(prefix, payload):
    # Code comes from the authenticated source release, not from operator JSON.
    script = base64.b64encode((ROOT / 'deployment/scripts/runtime_upgrade.py').read_bytes()).decode()
    command = 'import base64;exec(compile(base64.b64decode(' + repr(script) + '),"runtime_upgrade.py","exec"))'
    result = subprocess.run([*prefix, shlex.join(['sudo', '-n', 'python3', '-c', command])],
                            input=json.dumps(payload), text=True, capture_output=True, timeout=1800)
    env.argo.require(result.returncode == 0, 'node operation incomplete; inspect recovery receipt before retrying')
    return json.loads(result.stdout)


def stage_source(prefix, source_sha):
    home = '/var/lib/railshot/runtime-release-sources/' + source_sha
    names = ('airgap/versions.json', 'cilium/preflight.py')
    files = {name: base64.b64encode((ROOT / 'deployment' / name).read_bytes()).decode() for name in names}
    script = '''import base64,json,os,pathlib,sys
os.umask(0o077)
p=json.load(sys.stdin); root=pathlib.Path(p['home'])
root.mkdir(parents=True,exist_ok=True,mode=0o700)
for parent in [root,root.parent]:
    assert parent.resolve()==parent and parent.stat().st_uid==0 and not parent.stat().st_mode&0o077
for name,data in p['files'].items():
    path=root/name; data=base64.b64decode(data,validate=True)
    assert path.resolve()==path
    path.parent.mkdir(mode=0o700,exist_ok=True)
    if path.exists(): assert path.read_bytes()==data
    else:
        with path.open('xb') as stream: stream.write(data)
print(json.dumps({'source':str(root)}))
'''
    observed = json.loads(env.argo.native([*prefix, shlex.join(['sudo', '-n', 'python3', '-c', script])],
                                        document={'home': home, 'files': files}))
    env.argo.require(observed == {'source': home}, 'staged release source differs')
    return home


def load(args):
    release = env.read_private(args.release)
    upgrade.validate(release, json.loads((ROOT / 'deployment/airgap/versions.json').read_text()))
    record = env.read_private(Path(args.registration_dir) / 'registration.json')
    cd_bytes = env.read_private(Path(args.registration_dir) / 'cd.json', raw=True)
    env.argo.require(record['status'] == 'succeeded' and record['target_id'] == args.target_id
                     and record['cd_sha256'] == hashlib.sha256(cd_bytes).hexdigest(), 'existing registration receipt differs')
    cd = json.loads(cd_bytes); registered = cd['targets'][args.target_id]
    configuration = env.read_private(args.config)
    # Use the saved allocated URL/NodePort. Updating common policies never reallocates an edge.
    configuration['cd'] = cd
    registered.pop('edge', None)
    configuration['registration'].pop('edge_config_file', None)
    env.argo.require(configuration['registration'].get('observability_config_file'), 'registered observer required for common release')
    request, cd, settings, pull, binding, identity = env.load_configuration(
        env.read_private(args.registry), args.target_id, configuration, args.binding)
    env.argo.require(request['target']['provider'] == record['provider_kind']
                     and record['namespace'] == registered['target']['namespace']
                     and record['cluster_server'] == registered['target']['cluster_server'], 'registered target binding differs')
    # Bind the logical registration to its registry resource via the original claim.
    claim = env.read_private(Path(settings['state_dir']) / (args.target_id + '.json'))
    env.argo.require(claim['input_sha256'] == record['input_sha256']
                     and claim['resource_id'] == identity['descriptor']['resource_id'], 'registered resource owner differs')
    return release, record, request, cd, settings, pull, binding, identity


def app_health(kube, registered, target_id):
    target = registered['target']; namespace = target['namespace']; app = registered['app']
    obj = kube(namespace, 'get', 'deployment', app, '-o', 'json')
    env.argo.require(obj['spec']['template']['metadata']['labels'].get('railshot.io/target') == target_id,
                     'application target label differs')
    status = obj.get('status', {}); count = obj['spec'].get('replicas', 1)
    env.argo.require(count > 0 and status.get('observedGeneration', 0) >= obj['metadata']['generation']
                     and status.get('updatedReplicas', 0) == count and status.get('availableReplicas', 0) == count,
                     'application rollout is not ready')
    return {'uid': obj['metadata']['uid'], 'generation': obj['metadata']['generation'], 'ready_replicas': count,
            'images': [c['image'] for c in obj['spec']['template']['spec']['containers']]}


def management_health(cd, target_id):
    config = env.argo.kubectl(cd['context'], 'argocd', 'get', 'configmap', 'railshot-credentials', '-o', 'json')
    policy = env.credentials.validate_policy(json.loads(config['data']['policy.json']))
    rows = [t for t in policy['targets'] if t['target_id'] == target_id]
    env.argo.require(len(rows) == 1, 'one registered renewal target required')
    row = rows[0]; target = cd['targets'][target_id]['target']
    env.argo.require(row['server'] == target['cluster_server'] and target['namespace'] in row['namespaces'], 'management endpoint binding differs')
    secret = env.argo.kubectl(cd['context'], 'argocd', 'get', 'secret', row['secret'], '-o', 'json')
    credential, ca = env.credentials.registration(secret, row, time.time())
    pods = env.credentials.customer(row['server'], ca, credential['bearerToken'],
        '/api/v1/namespaces/' + target['namespace'] + '/pods?limit=1',
        **({'server_name': row['tls_server_name']} if 'tls_server_name' in row else {}))
    env.argo.require(pods.get('kind') == 'PodList' and isinstance(pods.get('items'), list), 'registered management credential read failed')
    return {'server': row['server'], 'namespace': target['namespace'], 'tls_verified': True, 'read_verified': True}


def observer_health(kube):
    deadline = time.monotonic() + 180
    while True:
        ready = True
        for kind, name, desired, actual in (('deployment', 'cluster-metrics', 'replicas', 'availableReplicas'),
                                            ('daemonset', 'node-metrics', 'desiredNumberScheduled', 'numberReady')):
            obj = kube('railshot-observability', 'get', kind, name, '-o', 'json')
            status = obj.get('status', {})
            count = obj['spec'].get(desired, 1) if kind == 'deployment' else status.get(desired, 0)
            ready &= (count > 0 and status.get('observedGeneration', 0) >= obj['metadata']['generation']
                      and status.get(actual, 0) == count)
        if ready:
            return {'exporters_ready': True}
        env.argo.require(time.monotonic() < deadline, 'observer rollout not ready')
        time.sleep(3)


def collection_query(row):
    # Match the product observer's target/app scopes and 90-second freshness rule.
    def selector(job, instance):
        return '{job=' + json.dumps(job) + ',instance=' + json.dumps(instance) + '}'
    node = selector('node', row['node_instance']); cluster = selector('cluster', row['cluster_instance'])
    http = selector('http', row['probe_url'])
    pod = ('kube_pod_status_phase' + cluster[:-1] + ',namespace=' + json.dumps(row['namespace']) + ',phase="Running"}')
    labels = ('kube_pod_labels' + cluster[:-1] + ',namespace=' + json.dumps(row['namespace'])
              + ',label_app_kubernetes_io_name=' + json.dumps(row['app'])
              + ',label_railshot_io_target=' + json.dumps(row['target_id']) + '}')
    expressions = {**{job + '_up': 'up' + sel for job, sel in (('node', node), ('cluster', cluster), ('http', http))},
        **{job + '_observed': 'timestamp(up' + sel + ')' for job, sel in (('node', node), ('cluster', cluster), ('http', http))},
        'cpu_percent': '100 * (1 - avg(rate(node_cpu_seconds_total' + node[:-1] + ',mode="idle"}[2m])))',
        'cpu_observed': 'min(timestamp(node_cpu_seconds_total' + node + '))',
        'memory_percent': '100 * (1 - node_memory_MemAvailable_bytes' + node + ' / node_memory_MemTotal_bytes' + node + ')',
        'memory_observed': 'min(timestamp(node_memory_MemAvailable_bytes' + node + ') or timestamp(node_memory_MemTotal_bytes' + node + '))',
        'pods': 'sum(' + pod + ' and on(namespace,pod,uid) ' + labels + ')',
        'pods_observed': 'min(timestamp(' + pod + ') and on(namespace,pod,uid) ' + labels + ')',
        'http': 'probe_success' + http, 'probe_observed': 'timestamp(probe_success' + http + ')'}
    return ' or '.join('label_replace((' + expression + '), "railshot_metric", "' + name + '", "", "")'
                       for name, expression in expressions.items())


def collection_values(document, now):
    env.argo.require(document.get('status') == 'success' and document['data']['resultType'] == 'vector', 'observer query failed')
    values = {}
    for item in document['data']['result']:
        name = item['metric']['railshot_metric']; value = float(item['value'][1])
        env.argo.require(name not in values and math.isfinite(value), 'ambiguous or nonfinite observer sample')
        values[name] = value
    clocks = ('node_observed', 'cluster_observed', 'http_observed', 'cpu_observed', 'memory_observed', 'pods_observed', 'probe_observed')
    env.argo.require(all(values.get(name) == 1 for name in ('node_up', 'cluster_up', 'http_up', 'http'))
                     and all(name in values and -5 <= now - values[name] <= 90 for name in clocks)
                     and all(name in values and 0 <= values[name] <= 100 for name in ('cpu_percent', 'memory_percent'))
                     and values.get('pods', 0) >= 1, 'observer samples missing, stale, or unhealthy')
    return {'collection_state': 'ready', 'cpu_percent': values['cpu_percent'], 'memory_percent': values['memory_percent'],
            'running_app_pods': values['pods'], 'http_probe_success': True,
            'oldest_observed_at': datetime.fromtimestamp(min(values[name] for name in clocks), timezone.utc).isoformat()}


def collection_health(settings, registration, registered, identity, *, wait_seconds=180):
    spec = importlib.util.spec_from_file_location('release_observer_validation', ROOT / 'observability/register.py')
    observer = importlib.util.module_from_spec(spec); spec.loader.exec_module(observer)
    config = observer.settings(env.read_private(settings['observability_config_file']))
    product = env.read_private(Path(config['state_dir']) / 'product.json')
    env.argo.require(product.get('version') == 1 and product.get('collector') ==
        {'id': config['owner'], 'lifecycle': config['lifecycle'], 'expires_at': config['expires_at']}, 'observer collector binding differs')
    rows = [row for row in product['targets'] if row['target_id'] == registered['target']['id'] and row['app'] == registered['app']]
    env.argo.require(len(rows) == 1, 'one registered app observation required')
    row = rows[0]; descriptor = identity['descriptor']
    address = observer.metrics_host(descriptor, descriptor['addresses']['private'])
    env.argo.require(row['resource_id'] == descriptor['resource_id'] and row['environment_id'] == registration['environment_id']
                     and row['namespace'] == registered['target']['namespace'] and row['probe_url'] == registered['public_http']['url']
                     and row['prometheus_url'] == config['prometheus_url']
                     and row['node_instance'] == address + ':' + str(config['node_metrics_port'])
                     and row['cluster_instance'] == address + ':' + str(config['cluster_metrics_port']), 'observer runtime binding differs')
    url = config['prometheus_url'] + '/api/v1/query?' + urlencode({'query': collection_query(row), 'timeout': '4s'})
    deadline = time.monotonic() + wait_seconds
    while True:
        try:
            with build_opener(ProxyHandler({}), env.credentials.NoRedirect()).open(Request(url), timeout=5) as response:
                raw = response.read(65537)
                env.argo.require(response.status == 200 and len(raw) <= 65536, 'observer response invalid')
            return collection_values(json.loads(raw), time.time())
        except (OSError, ValueError, KeyError, TypeError):
            env.argo.require(time.monotonic() < deadline, 'actual observer collection unverified')
            time.sleep(3)


def verify(args):
    release, registration, request, cd, settings, pull, binding, identity = load(args)
    path = Path(args.state_dir) / 'receipt.json'
    registration_home = Path(args.registration_dir)
    # Reuse the existing target lock read-only; never create state for verification.
    with os.fdopen(os.open(registration_home / 'runtime-release.lock', os.O_RDONLY | os.O_NOFOLLOW), 'r') as lock:
        fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
        previous = env.read_private(path)
        marker = env.read_private(registration_home / 'runtime-release.json')
        fingerprint = upgrade.digest({'release': release, 'identity': identity, 'registration': registration})
        env.argo.require(previous.get('status') == marker.get('status') == 'verified'
                         and previous.get('source_sha') == marker.get('source_sha') == release['source_sha']
                         and previous.get('target_id') == args.target_id
                         and previous.get('provider') == request['target']['provider']
                         and previous.get('input_sha256') == fingerprint
                         and marker.get('receipt') == str(path), 'verified release/target binding differs')
        with connection(request) as prefix:
            observed = node_call(prefix, {'action': 'inspect'})
        env.argo.require(observed['node_uid'] == previous['after']['node_uid']
                         and observed['node_ip'] == identity['descriptor']['addresses']['private']
                         and observed['architecture'] == 'amd64' and upgrade.matches(observed, release['to_policy']),
                         'live runtime drifted from the verified to-policy')
        registered = cd['targets'][args.target_id]
        with env.runtime_kubectl(request) as kube:
            namespace = kube('default', 'get', 'namespace', registered['target']['namespace'], '-o', 'json')
            env.argo.require(namespace['metadata'].get('labels', {}).get('railshot.io/registration') == registration['input_sha256'][:32],
                             'registered namespace ownership drifted')
            application = app_health(kube, registered, args.target_id)
            observer = observer_health(kube)
        observer.update(collection_health(settings, registration, registered, identity, wait_seconds=0))
        management = management_health(cd, args.target_id)
        public = registered['public_http']
        probe = env.bridge.public_probe(public, urlsplit(public['url']).path)
        env.argo.require(probe['state'] == 'succeeded', 'public HTTPS health drifted')
        return {'version': 1, 'status': 'verified', 'verify_only': True, 'source_sha': release['source_sha'],
                'provider': request['target']['provider'], 'target_id': args.target_id,
                'input_sha256': fingerprint, 'to_policy_sha256': release['to_policy_sha256'],
                'after': observed, 'application': application, 'observability': observer,
                'management': management, 'public_http': probe, 'checked_at': datetime.now(timezone.utc).isoformat()}


def execute(args):
    release, registration, request, cd, settings, pull, binding, identity = load(args)
    home = env.bridge.private_directory(args.state_dir)
    registration_home = env.bridge.private_directory(args.registration_dir)
    target_marker = registration_home / 'runtime-release.json'
    path = home / 'receipt.json'
    fingerprint = upgrade.digest({'release': release, 'identity': identity, 'registration': registration})
    with os.fdopen(os.open(registration_home / 'runtime-release.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600), 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if path.exists():
            previous = env.read_private(path)
            env.argo.require(previous.get('input_sha256') == fingerprint, 'release receipt input changed')
            env.argo.require(previous.get('status') == 'verified', 'incomplete release requires operator reconciliation; automatic retry forbidden')
            if target_marker.exists():
                marker = env.read_private(target_marker)
                env.argo.require(marker.get('source_sha') == release['source_sha'] and marker.get('receipt') == str(path),
                                 'a different release owns the target; stale success cannot be replayed')
            env.save(target_marker, {'status': 'verified', 'source_sha': release['source_sha'], 'receipt': str(path)})
            return {**previous, 'replayed': True}
        if target_marker.exists():
            env.argo.require(env.read_private(target_marker)['status'] == 'verified', 'previous target release requires operator reconciliation')
        receipt = {'version': 1, 'source_sha': release['source_sha'], 'target_id': args.target_id,
                   'provider': request['target']['provider'], 'input_sha256': fingerprint, 'status': 'checking',
                   'steps': [], 'mutation_started': False, 'from_policy_sha256': release['from_policy_sha256'],
                   'to_policy_sha256': release['to_policy_sha256'], 'automatic_rollback': False}
        if upgrade.validate(release):
            receipt['recovery_directory'] = '/var/lib/railshot/runtime-updates/' + release['source_sha']
        def stage(name, *, mutation=False):
            receipt['stage'] = name
            receipt['mutation_started'] |= mutation
            if mutation:
                env.save(target_marker, {'status': 'running', 'source_sha': release['source_sha'], 'receipt': str(path)})
            env.save(path, receipt)
        def done(name):
            receipt['steps'].append(name); env.save(path, receipt)
        try:
            stage('runtime_readback')
            with connection(request) as prefix:
                observed = node_call(prefix, {'action': 'inspect'})
                expected = {'node_uid': observed['node_uid'], 'node_ip': identity['descriptor']['addresses']['private'],
                            'target_id': args.target_id, 'resource_id': identity['descriptor']['resource_id']}
                env.argo.require(observed['node_ip'] == expected['node_ip'] and observed['architecture'] == 'amd64'
                                 and upgrade.matches(observed, release['from_policy']), 'live runtime differs from registered from-policy')
                receipt['before'] = observed; done('runtime_readback')
                stage('runtime_apply', mutation=True)
                source = stage_source(prefix, release['source_sha'])
                receipt['runtime'] = node_call(prefix, {'action': 'apply', 'release': release, 'expected': expected, 'source': source})
                env.argo.require(receipt['runtime']['status'] == 'verified', 'runtime apply was not verified')
                done('runtime_apply')
            registered = cd['targets'][args.target_id]
            stage('common_policy', mutation=True)
            with env.runtime_kubectl(request) as kube:
                namespace = kube('default', 'get', 'namespace', registered['target']['namespace'], '-o', 'json')
                env.argo.require(namespace['metadata'].get('labels', {}).get('railshot.io/registration') == registration['input_sha256'][:32],
                                 'registered namespace owner missing; refusing adoption')
                for document in env.runtime_documents(registered['target'], registration['input_sha256'][:32], pull, binding):
                    env.owned_apply(kube, document)
            done('common_policy')
            stage('observability', mutation=True)
            env.argo.require(settings.get('observability_config_file'), 'registered observer required for common release')
            spec = importlib.util.spec_from_file_location('runtime_release_observer', ROOT / 'observability/register.py')
            observer = importlib.util.module_from_spec(spec); spec.loader.exec_module(observer)
            observation = {'version': 1, 'target_id': args.target_id, 'environment_id': registration['environment_id'],
                           'app': registered['app'], 'namespace': registered['target']['namespace'],
                           'node_ip': expected['node_ip'], 'probe_url': registered['public_http']['url'],
                           'registry_file': str(args.registry), 'context': cd['context']}
            result = observer.register(env.read_private(settings['observability_config_file']), observation, home / 'observability.json')
            env.argo.require(result.get('registered') is True and result.get('status') == 'succeeded', 'observer reconciliation unverified')
            done('observability')
            stage('health')
            with env.runtime_kubectl(request) as kube:
                receipt['application'] = app_health(kube, registered, args.target_id)
                receipt['observability'] = observer_health(kube)
            receipt['observability'].update(collection_health(settings, registration, registered, identity))
            receipt['management'] = management_health(cd, args.target_id)
            public = registered['public_http']
            receipt['public_http'] = env.bridge.public_probe(public, urlsplit(public['url']).path)
            env.argo.require(receipt['public_http']['state'] == 'succeeded', 'public HTTPS health unverified')
            with connection(request) as prefix:
                after = node_call(prefix, {'action': 'inspect'})
            env.argo.require(after['node_uid'] == expected['node_uid'] and upgrade.matches(after, release['to_policy']), 'final runtime readback differs')
            receipt.update(status='verified', after=after, verified_at=datetime.now(timezone.utc).isoformat())
            done('health')
            env.save(target_marker, {'status': 'verified', 'source_sha': release['source_sha'], 'receipt': str(path)})
        except Exception as error:
            receipt.update(status='recovery_required' if receipt['mutation_started'] else 'blocked',
                           error_type=type(error).__name__)
            env.save(path, receipt)
        return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('registry', 'target-id', 'config', 'registration-dir', 'state-dir', 'release'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--binding')
    parser.add_argument('--verify-only', action='store_true', help='read current health without applying or writing release state')
    args = parser.parse_args(); os.umask(0o077)
    try:
        receipt = verify(args) if args.verify_only else execute(args)
    except Exception as error:
        receipt = {'status': 'blocked', 'error_type': type(error).__name__}
    print(json.dumps(receipt))
    return 0 if receipt['status'] == 'verified' else 1


if __name__ == '__main__':
    sys.exit(main())
