#!/usr/bin/env python3
"""Render a reviewable stateless Argo handoff from a trusted published artifact.

No Git push, cluster registration, sync, network access or deployed claim.
"""
import argparse
import hashlib
import ipaddress
import json
from pathlib import Path, PurePosixPath
import re
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'ci/scripts'))
from gate.bundle import contract, require, REPO, ID, TRUST
from execution import pod_security, container_security
from publication import validate_registry
import yaml
from jsonschema import ValidationError

FILES = ('images.json', 'jasmin.yaml', 'verdict.json', 'manifest.json')


def document_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def http_path(value):
    require(isinstance(value, str) and re.fullmatch(r'/[A-Za-z0-9/._~-]*', value) and
            '//' not in value and not {'.', '..'} & set(value.split('/')),
            'plain absolute HTTP path required; no rewrite, query, fragment or escaping')
    return value


def read_artifact(directory, target_id):
    directory = Path(directory)
    require(directory.is_dir() and not directory.is_symlink(), 'regular artifact directory required')
    raw = {}
    for name in (*FILES, 'handoff.json'):
        path = directory / name
        require(path.is_file() and not path.is_symlink() and path.stat().st_size < 2_000_000,
                'small regular artifact file required: ' + name)
        raw[name] = path.read_bytes()
    receipt = json.loads(raw['handoff.json'])
    require(receipt.get('version') == 2 and receipt.get('status') == 'published', 'published receipt v2 required')
    require(all(type(receipt.get(k)) is int and receipt[k] > 0 for k in
                ('run_id', 'producer_attempt', 'bundle_artifact_id')), 'positive producer identifiers required')
    require(receipt.get('target_id') == target_id, 'artifact target mismatch')
    require(re.fullmatch(r'[0-9a-f]{40}', receipt.get('source_commit', '')), 'source commit required')
    for name in FILES:
        require(receipt.get('files', {}).get(name) == hashlib.sha256(raw[name]).hexdigest(),
                'published receipt hash mismatch: ' + name)
    validate_registry(receipt.get('registry'), receipt['files']['images.json'])
    verdict = contract(raw['jasmin.yaml'], raw['verdict.json'])
    manifest = json.loads(raw['manifest.json'])
    require(manifest.get('version') == 1 and manifest.get('trust') == TRUST, 'bundle manifest contract mismatch')
    require(manifest.get('source_sha256') == verdict['source_sha256'], 'source digest mismatch')
    for name in ('jasmin.yaml', 'verdict.json'):
        require(manifest.get('files', {}).get(name) == hashlib.sha256(raw[name]).hexdigest(), 'bundle hash mismatch')
    require({k: v.get('id') for k, v in manifest.get('images', {}).items()} == verdict['image_ids'],
            'manifest gate image IDs differ')
    spec, images = yaml.safe_load(raw['jasmin.yaml']), json.loads(raw['images.json'])
    require(receipt.get('app') == spec['app'], 'artifact application mismatch')
    require(set(images) == {s['name'] for s in spec['services']}, 'published service mismatch')
    require(all(isinstance(v, str) and re.fullmatch(rf'{REPO}@{ID}', v) for v in images.values()),
            'immutable published digest references required')
    return spec, images, receipt


def render(directory, target):
    for field in ('id', 'namespace', 'argocd_namespace', 'project'):
        require(isinstance(target.get(field), str) and re.fullmatch(r'[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?', target[field]),
                'explicit DNS label required: ' + field)
    require(target['project'] != 'default', 'use a restricted team-owned Argo AppProject')
    require(target['namespace'] not in {'default', 'kube-system', 'kube-public', 'kube-node-lease',
                                       target['argocd_namespace']}, 'dedicated workload namespace required')
    require(target.get('architecture') == 'amd64', 'current published images require linux/amd64')
    for field in ('repo_url', 'cluster_server'):
        parsed = urlsplit(target.get(field, ''))
        require(parsed.scheme == 'https' and parsed.hostname and not parsed.username and not parsed.password
                and not parsed.query and not parsed.fragment, 'HTTPS URL without credentials required: ' + field)
    git_path = PurePosixPath(target.get('path', ''))
    require(str(git_path) not in ('', '.') and not git_path.is_absolute() and '..' not in git_path.parts,
            'explicit relative GitOps application path required')
    require(re.fullmatch(r'[0-9a-f]{40}', target.get('revision', '')), 'pin the reviewed GitOps commit SHA')
    require(type(target.get('node_port')) is int and 30000 <= target['node_port'] <= 32767, 'allocated NodePort required')
    cidrs = target.get('ingress_cidrs', [])
    require(isinstance(cidrs, list) and cidrs, 'explicit routed ingress source CIDRs required')
    for value in cidrs:
        require(ipaddress.ip_network(value).prefixlen > 0, 'world-open workload ingress is not a default')
    resources = target.get('resources', {})
    require(set(resources) == {'requests', 'limits'} and all(set(v) == {'cpu', 'memory'} for v in resources.values()),
            'target owner must provide CPU/memory requests and limits; CI size is not a runtime allocation')
    for values in resources.values():
        require(re.fullmatch(r'[1-9][0-9]*m', values['cpu']) and re.fullmatch(r'[1-9][0-9]*Mi', values['memory']),
                'use positive millicores and MiB quantities')
    for kind in ('cpu', 'memory'):
        unit = 'm' if kind == 'cpu' else 'Mi'
        require(int(resources['requests'][kind].removesuffix(unit)) <= int(resources['limits'][kind].removesuffix(unit)),
                'request exceeds limit')
    spec, images, receipt = read_artifact(directory, target['id'])
    pull_secret = receipt['registry']['image_pull_secret']
    if receipt['registry']['visibility'] == 'private':
        require(target.get('image_pull_secret') == pull_secret and pull_secret['namespace'] == target['namespace'],
                'private registry requires the published pull Secret in the target namespace')
    require(len(spec['services']) == 1 and not spec.get('resources') and not spec.get('egress'),
            'initial CD handoff supports one stateless service without external egress')
    svc = spec['services'][0]
    require(not svc.get('migrate') and not svc.get('secrets'),
            'DB and secret injection require the CD owner contract')
    require(target.get('path_mode', 'preserve') == 'preserve', 'HTTP paths must be forwarded without rewriting')
    route, health = http_path(svc.get('route')), http_path(svc.get('health'))
    name, namespace = spec['app'], target['namespace']
    labels = {'app.kubernetes.io/name': name, 'railshot.io/target': target['id']}
    container = {'name': svc['name'], 'image': images[svc['name']], 'ports': [{'containerPort': svc['port']}],
                 'env': [{'name': k, 'value': v} for k, v in {**svc.get('env', {}), 'PORT': str(svc['port'])}.items()],
                 'resources': resources, 'securityContext': container_security(),
                 'volumeMounts': [{'name': 'tmp', 'mountPath': '/tmp'}]}
    if svc.get('command'):
        container['command'] = svc['command']
    container['readinessProbe'] = {'httpGet': {'path': health, 'port': svc['port']}, 'periodSeconds': 5}
    workload = {'apiVersion': 'apps/v1', 'kind': 'Deployment', 'metadata': {'name': name, 'namespace': namespace},
                'spec': {'replicas': svc.get('replicas', 1), 'selector': {'matchLabels': labels},
                         'template': {'metadata': {'labels': labels}, 'spec': {
                             'automountServiceAccountToken': False, 'nodeSelector': {'kubernetes.io/arch': 'amd64'},
                             'securityContext': pod_security(), 'containers': [container],
                             'volumes': [{'name': 'tmp', 'emptyDir': {'sizeLimit': '64Mi'}}]}}}}
    if pull_secret is not None:
        workload['spec']['template']['spec']['imagePullSecrets'] = [{'name': pull_secret['name']}]
    service = {'apiVersion': 'v1', 'kind': 'Service', 'metadata': {'name': name, 'namespace': namespace},
               'spec': {'type': 'NodePort', 'selector': labels, 'externalTrafficPolicy': 'Local',
                        'ports': [{'port': svc['port'], 'targetPort': svc['port'], 'nodePort': target['node_port']}]}}
    # ponytail: one stateless service has no runtime egress; add a Cilium policy only with an agreed external dependency.
    policy = {'apiVersion': 'networking.k8s.io/v1', 'kind': 'NetworkPolicy',
              'metadata': {'name': name, 'namespace': namespace}, 'spec': {
                  'podSelector': {'matchLabels': labels}, 'policyTypes': ['Ingress', 'Egress'], 'egress': [],
                  'ingress': [{'from': [{'ipBlock': {'cidr': c}} for c in cidrs],
                               'ports': [{'protocol': 'TCP', 'port': svc['port']}]}]}}
    app_name = target['id'] + '-' + namespace + '-' + name
    if len(app_name) > 63:
        app_name = app_name[:50].rstrip('-') + '-' + hashlib.sha256(app_name.encode()).hexdigest()[:12]
    app = {'apiVersion': 'argoproj.io/v1alpha1', 'kind': 'Application',
           'metadata': {'name': app_name, 'namespace': target['argocd_namespace'],
                        'labels': {'app.kubernetes.io/managed-by': 'railshot', 'railshot.io/target': target['id']}},
           'spec': {'project': target['project'], 'source': {'repoURL': target['repo_url'],
                     'targetRevision': target['revision'], 'path': str(git_path), 'directory': {'recurse': False}},
                    'destination': {'server': target['cluster_server'], 'namespace': namespace}}}
    workload_list = {'apiVersion': 'v1', 'kind': 'List', 'items': [workload, service, policy]}
    return {'workload': workload_list,
            'application': app, 'status': 'rendered_for_review', 'deployed': False,
            'documents': {'workload': document_hash(workload_list), 'application': document_hash(app)},
            'http': {'route': route, 'health_path': health, 'path_mode': 'preserve',
                     'container_port': svc['port'], 'node_port': target['node_port']},
            'source_commit': receipt['source_commit'], 'target_id': target['id'],
            **{k: receipt[k] for k in ('tenant', 'app', 'run_id', 'producer_attempt', 'bundle_artifact_id', 'registry')}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('published', type=Path)
    parser.add_argument('target', type=Path)
    parser.add_argument('out', type=Path)
    args = parser.parse_args()
    try:
        result = render(args.published, json.loads(args.target.read_text()))
        args.out.mkdir(parents=True, exist_ok=False)
        for name, value in result.items():
            if name in ('workload', 'application'):
                (args.out / (name + '.json')).write_text(json.dumps(value, indent=2) + '\n')
        (args.out / 'receipt.json').write_text(json.dumps({k: v for k, v in result.items() if k not in ('workload', 'application')}, indent=2) + '\n')
        print(json.dumps({'status': result['status'], 'deployed': False, 'output': str(args.out)}))
        return 0
    except ValidationError:
        print(json.dumps({'status': 'blocked', 'deployed': False, 'reason': 'invalid published workload schema'}), file=sys.stderr)
        return 2
    except (ValueError, KeyError, TypeError, OSError) as exc:
        print(json.dumps({'status': 'blocked', 'deployed': False, 'reason': str(exc)}), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
