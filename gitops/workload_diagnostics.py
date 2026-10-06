"""Read bounded workload facts with the existing application-scoped credential.

No Pod output, exception messages, environment values or mutation is exported.
Observation failure does not change the bridge's deterministic result.
"""
from datetime import datetime, timezone
import argparse
import json
from pathlib import Path
import re
from urllib.parse import urlencode

import argo
import credentials
import logs

REASONS = {'CrashLoopBackOff', 'ImagePullBackOff', 'ErrImagePull', 'CreateContainerConfigError',
           'CreateContainerError', 'RunContainerError', 'ContainerCreating', 'PodInitializing',
           'OOMKilled', 'Error', 'Completed'}


def workload(config, review):
    checked = datetime.now(timezone.utc).isoformat()
    missing = {'state': 'unavailable', 'checked_at': checked, 'pods': [], 'code': 'WORKLOAD_OBSERVATION_UNAVAILABLE'}
    try:
        auth, tls = logs.customer_auth(config, review)
        namespace = review['application']['spec']['destination']['namespace']
        expected = next(item for item in review['workload']['items'] if item['kind'] == 'Deployment')
        template = expected['spec']['template']
        path = '/apis/apps/v1/namespaces/' + namespace + '/deployments/' + expected['metadata']['name']
        listing = credentials.customer(*auth, '/apis/apps/v1/namespaces/' + namespace + '/deployments?' +
                                       urlencode({'fieldSelector': 'metadata.name=' + expected['metadata']['name'], 'limit': 2}), **tls)
        argo.require(listing['kind'] == 'DeploymentList' and not listing.get('metadata', {}).get('continue')
                     and len(listing['items']) <= 1, 'bounded deployment required')
        if not listing['items']:
            return {'state': 'missing', 'checked_at': checked, 'pods': [], 'code': 'WORKLOAD_MISSING'}
        live = listing['items'][0]
        argo.require(live.get('kind', 'Deployment') == 'Deployment' and live['metadata']['namespace'] == namespace
                     and live['metadata']['name'] == expected['metadata']['name'] and not live['metadata'].get('deletionTimestamp')
                     and live['spec']['selector'] == expected['spec']['selector']
                     and logs.template_matches(live['spec']['template'], template), 'deployment binding differs')
        selector = urlencode({'labelSelector': ','.join(k + '=' + v for k, v in template['metadata']['labels'].items()), 'limit': 20})
        replicas = credentials.customer(*auth, '/apis/apps/v1/namespaces/' + namespace + '/replicasets?' + selector, **tls)
        argo.require(replicas['kind'] == 'ReplicaSetList' and not replicas.get('metadata', {}).get('continue')
                     and len(replicas['items']) <= 20, 'bounded replicas required')
        owners = {row['metadata']['uid'] for row in replicas['items'] if row['metadata']['namespace'] == namespace
                  and logs.owns(row, 'Deployment', live['metadata']['uid']) and logs.template_matches(row['spec']['template'], template)}
        listing = credentials.customer(*auth, '/api/v1/namespaces/' + namespace + '/pods?' + selector, **tls)
        argo.require(listing['kind'] == 'PodList' and not listing.get('metadata', {}).get('continue')
                     and len(listing['items']) <= 20, 'bounded pods required')
        selected = [p for p in listing['items'] if not p['metadata'].get('deletionTimestamp')
                    and any(logs.owns(p, 'ReplicaSet', uid) for uid in owners)]
        rows = []
        expected_images = {c['name']: c['image'] for c in template['spec']['containers']}
        for pod in selected:
            meta = pod['metadata']
            argo.require(meta['namespace'] == namespace and re.fullmatch(logs.NAME, meta['name'])
                         and logs.template_matches(pod, template), 'pod binding differs')
            statuses = pod.get('status', {}).get('containerStatuses', [])
            argo.require(len(statuses) <= len(expected_images), 'bounded containers required')
            containers = []
            for c in statuses:
                argo.require(c['name'] in expected_images, 'container binding differs')
                state = c.get('state', {})
                status = next((key for key in ('running', 'waiting', 'terminated') if key in state), 'unknown')
                detail = state.get(status, {})
                image_id = c.get('imageID', '')
                image_hash = re.search(r'sha256:[0-9a-f]{64}$', image_id)
                containers.append({'name': c['name'], 'state': status, 'ready': c.get('ready') is True,
                    'restart_count': c.get('restartCount') if type(c.get('restartCount')) is int else None,
                    'image_id': image_hash[0] if image_hash else None,
                    'image_matches': bool(image_hash and expected_images[c['name']].endswith('@' + image_hash[0])),
                    'reason': detail.get('reason') if detail.get('reason') in REASONS else None,
                    'exit_code': detail.get('exitCode') if type(detail.get('exitCode')) is int else None})
            ready = (len(containers) == len(expected_images) and all(c['ready'] and c['image_matches'] for c in containers)
                     and any(c.get('type') == 'Ready' and c.get('status') == 'True' for c in pod.get('status', {}).get('conditions', [])))
            rows.append({'name': meta['name'], 'ready': ready, 'containers': containers})
        after = credentials.customer(*auth, path, **tls)
        argo.require(after['metadata']['uid'] == live['metadata']['uid']
                     and after['metadata']['generation'] == live['metadata']['generation'], 'deployment changed during observation')
        desired = live['spec'].get('replicas', 1)
        status = live.get('status', {})
        strategy = live['spec'].get('strategy', {})
        update_strategy = None
        if strategy.get('type') in ('RollingUpdate', 'Recreate'):
            update_strategy = {'type': strategy['type']}
            for source, field in (('maxUnavailable', 'max_unavailable'), ('maxSurge', 'max_surge')):
                value = strategy.get('rollingUpdate', {}).get(source)
                update_strategy[field] = value if (type(value) is int and 0 <= value <= 10000
                    or isinstance(value, str) and re.fullmatch(r'(?:100|[0-9]{1,2})%', value)) else None
        ready = (type(desired) is int and desired > 0 and len(rows) >= desired and all(p['ready'] for p in rows)
                 and status.get('observedGeneration', 0) >= live['metadata']['generation']
                 and status.get('availableReplicas', 0) >= desired)
        return {'state': 'ready' if ready else 'progressing', 'checked_at': checked, 'pods': rows,
                'code': None, 'observed_generation': status.get('observedGeneration'), 'desired_replicas': desired,
                'update_strategy': update_strategy,
                'replicas': {key: status.get(field) if type(status.get(field)) is int and status[field] >= 0 else None
                             for key, field in (('ready', 'readyReplicas'), ('updated', 'updatedReplicas'),
                                                ('available', 'availableReplicas'))}}
    except Exception:
        return missing


def observe(config, identity):
    """Inspect the recorded version without replaying CI, Git writes, or Argo sync."""
    import bridge
    review = logs.load_bound_review(config, identity)
    registered = config['targets'][identity['target_id']]
    current = workload(config, review)
    public = bridge.public_probe(registered['public_http'], review['receipt']['http']['health_path'])
    reason = ('runtime_observation_unavailable' if current['state'] == 'unavailable' else
              'WORKLOAD_MISSING' if current['state'] == 'missing' else
              'WORKLOAD_NOT_READY' if current['state'] != 'ready' else
              'PUBLIC_HTTP_UNVERIFIED' if public['state'] != 'succeeded' else None)
    return {'state': 'collection_failed' if current['state'] == 'unavailable' else 'ready',
            'checked_at': datetime.now(timezone.utc).isoformat(), 'reason': reason,
            'workload': current, 'public_http': public}


def main():
    import bridge
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--identity', required=True)
    args = parser.parse_args()
    try:
        argo.require(len(args.identity) <= 4096, 'bounded observation identity required')
        value = observe(bridge.read_config(args.config), json.loads(args.identity))
    except (ValueError, KeyError, TypeError, OSError, RuntimeError):
        value = {'state': 'collection_failed', 'checked_at': datetime.now(timezone.utc).isoformat(),
                 'reason': 'runtime_observation_unavailable', 'workload': None, 'public_http': None}
    print(json.dumps(value, allow_nan=False))


if __name__ == '__main__':
    main()
