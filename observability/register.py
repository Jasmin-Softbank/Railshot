#!/usr/bin/env python3
"""Register an operator-bound runtime with one existing shared observer."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(Path(__file__).resolve().parent), str(ROOT / 'deployment/scripts'), str(ROOT / 'infrastructure/ansible'),
               str(ROOT / 'gitops'), str(ROOT / 'ci/scripts')]
# Other deployment helpers also have a render.py; bind this renderer by path.
_renderer_spec = importlib.util.spec_from_file_location('railshot_observer_render', ROOT / 'observability/render.py')
_renderer = importlib.util.module_from_spec(_renderer_spec)
_renderer_spec.loader.exec_module(_renderer)
cluster, prometheus, NAMESPACE, validate = _renderer.cluster, _renderer.prometheus, _renderer.NAMESPACE, _renderer.validate
from storage import durable_write


def require(ok):
    if not ok:
        raise ValueError('Observer registration binding or configuration differs')


def settings(config):
    require(set(config) == {'version', 'owner', 'lifecycle', 'expires_at', 'state_dir', 'prometheus_url', 'observer_ip',
                           'observer_registry_file', 'observer_target_id', 'observer_directory',
                           'node_metrics_port', 'cluster_metrics_port'} and config['version'] == 1)
    require(config['lifecycle'] in ('acceptance', 'shared'))
    require(datetime.fromisoformat(config['expires_at'].replace('Z', '+00:00')) > datetime.now(timezone.utc))
    require(isinstance(config['owner'], str) and re.fullmatch(r'[a-z][a-z0-9-]{2,39}', config['owner']))
    require(str(ipaddress.IPv4Address(config['observer_ip'])) == config['observer_ip'])
    require(config['prometheus_url'] == 'http://' + config['observer_ip'] + ':9090')
    for key in ('state_dir', 'observer_registry_file', 'observer_directory'):
        path = Path(config[key])
        require(path.is_absolute() and '..' not in path.parts and str(path) != '/')
    require(re.fullmatch(r'/[a-zA-Z0-9_./-]+', config['observer_directory']))
    require(type(config['node_metrics_port']) is int and type(config['cluster_metrics_port']) is int)
    require(30000 <= config['node_metrics_port'] <= 32767 and 30000 <= config['cluster_metrics_port'] <= 32767
            and config['node_metrics_port'] != config['cluster_metrics_port'])
    return config


def registration_row(config, request, descriptor):
    require(set(request) == {'version', 'target_id', 'environment_id', 'app', 'namespace', 'node_ip',
                             'probe_url', 'registry_file', 'context'} and request['version'] == 1)
    require(request['target_id'] == descriptor['target_id'] and request['node_ip'] == descriptor['addresses']['private'])
    for key in ('target_id', 'app', 'namespace'):
        require(isinstance(request[key], str) and re.fullmatch(r'[a-z][a-z0-9-]{1,61}[a-z0-9]', request[key]))
    require(isinstance(request['environment_id'], str) and re.fullmatch(r'[A-Za-z0-9._-]{1,128}', request['environment_id']))
    rendered = validate({'name': config['owner'], 'node_ip': request['node_ip'],
        'observer_source_cidr': config['observer_ip'] + '/32', 'node_metrics_port': config['node_metrics_port'],
        'cluster_metrics_port': config['cluster_metrics_port'], 'probe_urls': [request['probe_url']], 'argocd_metrics': None})
    metrics_host = (descriptor['addresses'].get('metrics', request['node_ip'])
                    if descriptor.get('provider_kind') == 'openstack' else request['node_ip'])
    return {'target_id': request['target_id'], 'app': request['app'], 'namespace': request['namespace'],
            **({'node_ip': request['node_ip']} if metrics_host != request['node_ip'] else {}),
            'prometheus_url': config['prometheus_url'], 'node_instance': f"{metrics_host}:{config['node_metrics_port']}",
            'cluster_instance': f"{metrics_host}:{config['cluster_metrics_port']}", 'probe_url': request['probe_url'],
            'environment_id': request['environment_id'], 'resource_id': descriptor['resource_id']}, rendered


def merge_rows(rows, row):
    require(isinstance(rows, list) and len(rows) <= 100)
    existing = [item for item in rows if item['target_id'] == row['target_id']]
    require(not existing or existing == [row])
    if existing:
        return rows
    require(len(rows) < 100 and not any(item['node_instance'] == row['node_instance'] for item in rows))
    return [*rows, row]


def scrape_config(rows):
    require(bool(rows))
    first = rows[0]
    result = prometheus({'node_ip': first['node_instance'].split(':')[0],
        'node_metrics_port': int(first['node_instance'].split(':')[1]),
        'cluster_metrics_port': int(first['cluster_instance'].split(':')[1]),
        'probe_urls': [first['probe_url']], 'argocd_metrics': None})
    for job, key in zip(result['scrape_configs'], ('node_instance', 'cluster_instance', 'probe_url')):
        job['static_configs'] = [{'targets': sorted({row[key] for row in rows})}]
    return result


def node_request(registry_file, target_id, read_private=None, ansible=None):
    # Retain the existing observer bootstrap's four-argument call contract.
    from environment import read_private as read_registered, registered_node
    registry = (read_private or read_registered)(registry_file)
    require(registry.get('version') == 1 and target_id in registry.get('targets', {}))
    return registered_node(registry['targets'][target_id], target_id, 'observation.' + target_id,
                           timeout_seconds=300)


@contextmanager
def observer_ssh(config, request, ansible):
    node = request['inventory']['control_plane'][0]
    require(node['private_ipv4'] == config['observer_ip'])
    with ansible.forwarded_port(node['ssh'].get('transport_ref'), time.monotonic() + 90) as port:
        host = next(iter(ansible.build_inventory(request, port)['all']['children']['k3s_server']['hosts'].values()))
        prefix = ['ssh', *shlex.split(host['ansible_ssh_common_args']), '-i', host['ansible_ssh_private_key_file'],
                  '-p', str(host['ansible_port']), '-o', 'ConnectTimeout=15', host['ansible_user'] + '@' + host['ansible_host']]
        yield prefix


def sync_observer(config, request, document, ansible, native):
    with observer_ssh(config, request, ansible) as prefix:
        directory = shlex.quote(config['observer_directory'])
        # Bootstrap owns this exact directory and marker. Registration cannot adopt another stack.
        command = f'cd {directory} && test "$(cat owner)" = {shlex.quote(config["owner"] + ":" + config["lifecycle"])} && cat > prometheus.next.json && '
        command += 'sudo docker compose exec -T prometheus promtool check config /etc/prometheus/prometheus.json >/dev/null && '
        command += 'cp prometheus.json prometheus.previous.json && cat prometheus.next.json > prometheus.json && '
        command += '(sudo docker compose exec -T prometheus promtool check config /etc/prometheus/prometheus.json >/dev/null || '
        command += '{ cat prometheus.previous.json > prometheus.json; exit 1; }) && sudo docker compose kill -s SIGHUP prometheus >/dev/null'
        native([*prefix, command], document=document)
        require(json.loads(native([*prefix, 'cat ' + directory + '/prometheus.json'])) == document)


def register(config, request, output):
    # Supplied by the runtime registration owner (PR27); imports make no cloud calls.
    from environment import read_private, runtime_kubectl
    import run as ansible
    from argo import native
    config = settings(config)
    state = Path(config['state_dir']); state.mkdir(mode=0o700, parents=True, exist_ok=True)
    require(state.resolve() == state and state.stat().st_uid == os.geteuid() and not state.stat().st_mode & 0o077)
    runtime, descriptor = node_request(request['registry_file'], request['target_id'])
    observer, _ = node_request(config['observer_registry_file'], config['observer_target_id'])
    row, rendering = registration_row(config, request, descriptor)
    product = state / 'product.json'
    receipt = {'status': 'unknown', 'target_id': row['target_id'], 'app': row['app'], 'registered': False,
               'collection_state': 'pending', 'steps': []}
    def save():
        durable_write(output, json.dumps(receipt).encode())
    lock = os.open(state / 'registration.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        desired = state / 'desired.json'
        rows = read_private(desired)['targets'] if desired.exists() else read_private(product)['targets'] if product.exists() else []
        rows = merge_rows(rows, row)
        durable_write(desired, json.dumps({'version': 1, 'targets': rows}).encode())
        save()  # Record an uncertain outcome before the first mutation.
        labels = {'app.kubernetes.io/managed-by': 'railshot-observer', 'railshot.io/observer': config['owner']}
        with runtime_kubectl(runtime) as kube:
            services = kube('default', 'get', 'services', '-A', '-o', 'json')['items']
            for service in services:
                if any(port.get('nodePort') in (config['node_metrics_port'], config['cluster_metrics_port']) for port in service['spec']['ports']):
                    require(service['metadata'].get('namespace') == NAMESPACE and
                            all(service['metadata'].get('labels', {}).get(k) == v for k, v in labels.items()))
            for document in cluster(rendering)['items']:
                meta = document['metadata']; meta.setdefault('labels', {}).update(labels)
                namespace = meta.get('namespace', 'default')
                existing = kube(namespace, 'get', document['kind'], meta['name'], '--ignore-not-found', '-o', 'json')
                if existing:
                    require(not existing['metadata'].get('ownerReferences') and all(existing['metadata'].get('labels', {}).get(k) == v for k, v in labels.items()))
                kube(namespace, 'apply', '--server-side', '--field-manager=railshot-observer', '-f', '-', '-o', 'json', document=document)
        receipt['steps'].append('exporters_applied'); save()
        sync_observer(config, observer, scrape_config(rows), ansible, native)
        receipt['steps'].append('collector_registered'); save()
        durable_write(product, json.dumps({'version': 1, 'targets': rows, 'collector': {
            'id': config['owner'], 'lifecycle': config['lifecycle'], 'expires_at': config['expires_at']}}).encode())
        receipt.update(status='succeeded', registered=True)
        receipt['steps'].append('product_registered'); save()
        return receipt
    finally:
        os.close(lock)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--request', required=True)
    parser.add_argument('--out', required=True)
    args = parser.parse_args()
    from environment import read_private
    try:
        result = register(read_private(args.config), read_private(args.request), Path(args.out))
    except Exception as error:
        # Internal paths, SSH diagnostics, descriptors and credentials stay private.
        print(json.dumps({'status': 'unknown', 'registered': False, 'collection_state': 'pending', 'error_type': type(error).__name__}))
        raise SystemExit(1) from None
    print(json.dumps(result))


if __name__ == '__main__':
    main()
