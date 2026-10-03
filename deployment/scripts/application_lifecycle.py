#!/usr/bin/env python3
"""Plan and execute one registered app's lifecycle; shared infrastructure is never destroyed."""
import argparse
import base64
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time
import uuid

import applications
import environment as runtime
import application_cleanup as edge
import lifecycle_runtime as workloads

require = applications.require
TTL = 600
SKIP = 'argocd.argoproj.io/skip-reconcile'


def reference(kind, name, namespace=None):
    return {'kind': kind, 'name': name, **({'namespace': namespace} if namespace else {})}


def identity(obj, *, spec=False):
    if not obj:
        return None
    meta = obj['metadata']
    require(isinstance(meta.get('uid'), str) and meta['uid'] and not meta.get('deletionTimestamp'), 'RESOURCE_IDENTITY_UNKNOWN')
    require(not meta.get('ownerReferences'), 'RESOURCE_OWNER_CONFLICT')
    return {'uid': meta['uid'], **({'spec_sha256': applications.digest(obj['spec'])} if spec else {})}


def control_for(binding):
    return lambda *args, **kwargs: runtime.argo.kubectl(binding['cd']['context'], 'argocd', *args, **kwargs)


def load_binding(config, request):
    selected = {key: request[key] for key in ('environment_id', 'app', 'application_id')}
    profile, native, _, endpoint, _, authority, _, fingerprint = applications._inputs(config, selected)
    home = Path(config['state_dir']) / request['application_id']
    record = runtime.read_private(home / 'registration.json')
    require(record.get('status') == 'succeeded' and all(record.get(k) == v for k, v in selected.items()), 'APPLICATION_NOT_REGISTERED')
    raw = runtime.read_private(home / 'binding.json', raw=True)
    require(record.get('binding_sha256') == hashlib.sha256(raw).hexdigest()
            and record.get('input_sha256') == fingerprint
            and record.get('environment_sha256') == applications.digest(authority), 'APPLICATION_BINDING_CHANGED')
    binding = json.loads(raw, object_pairs_hook=runtime.ansible.unique_pairs)
    app_id = request['application_id']
    require(binding.get('version') == 1 and binding.get('application_id') == app_id
            and binding.get('environment_id') == request['environment_id'] and binding.get('provider') == profile['provider']
            and binding.get('cd') == config['cd'] and binding.get('ingress') == profile['ingress'], 'APPLICATION_BINDING_CHANGED')
    registered = binding['registered']; target = registered['target']
    require(registered['app'] == request['app'] and registered['tenant'] == profile['tenant']
            and target == record['target'] and target['id'] == target['namespace'] == target['project'] == app_id
            and target['cluster_server'] == endpoint and target['argocd_namespace'] == 'argocd'
            and target['image_pull_secret'] == {'name': 'ghcr-pull', 'namespace': app_id}, 'APPLICATION_BINDING_CHANGED')
    return home, binding, native, profile, hashlib.sha256(raw).hexdigest()


def ci_binding(profile, binding):
    old = runtime.github_variable(profile['source_repository'], 'RAILSHOT_TARGET_BINDINGS')
    values = json.loads(old['value']) if old else {}
    selected = binding['registered']; app_id = binding['application_id']
    expected = {key: selected[key] for key in ('app', 'tenant')}
    expected['image_pull_secret'] = selected['target']['image_pull_secret']
    require(isinstance(values, dict) and (app_id not in values or values[app_id] == expected), 'CI_BINDING_CHANGED')
    return values, expected


def control_inventory(binding, profile, *, allow_active=False):
    control = control_for(binding); app_id = binding['application_id']; target = binding['registered']['target']
    cm = control('get', 'configmap', 'railshot-credentials', '-o', 'json')
    policy = runtime.credentials.validate_policy(json.loads(cm['data']['policy.json']))
    renewal = next((item for item in policy['targets'] if item['target_id'] == app_id), None)
    require(renewal and renewal['server'] == target['cluster_server'] and renewal['namespaces'] == [app_id]
            and renewal['project'] == app_id and renewal['service_account']['namespace'] == app_id
            and renewal['service_account']['name'] == runtime.SA, 'APPLICATION_CREDENTIAL_BINDING_CHANGED')
    name = runtime.application_name(app_id, app_id, binding['registered']['app'])
    app = control('get', 'application', name, '--ignore-not-found', '-o', 'json')
    if app:
        spec = app['spec']; labels = app['metadata'].get('labels', {})
        require(labels.get('railshot.io/target') == app_id and labels.get('app.kubernetes.io/managed-by') == 'railshot'
                and spec.get('project') == app_id and spec.get('destination') == {'server': target['cluster_server'], 'namespace': app_id}
                and spec['source'].get('repoURL') == target['repo_url'] and spec['source'].get('path') == target['path']
                and not spec.get('syncPolicy') and not app['metadata'].get('finalizers'), 'APPLICATION_CONTROL_CONFLICT')
        require(allow_active or (not app.get('operation') and app.get('status', {}).get('operationState', {}).get('phase') not in ('Running', 'Terminating')),
                'APPLICATION_SYNC_ACTIVE')
    project = control('get', 'appproject', app_id, '-o', 'json')
    require(project['metadata'].get('labels', {}).get('railshot.io/registration') == app_id
            and project['metadata'].get('labels', {}).get('app.kubernetes.io/managed-by') == 'railshot'
            and project['spec'].get('destinations') == [{'server': target['cluster_server'], 'namespace': app_id}]
            and project['spec'].get('sourceRepos') == [target['repo_url']]
            and not project['spec'].get('clusterResourceWhitelist') and not project['metadata'].get('finalizers'), 'APPLICATION_PROJECT_CONFLICT')
    secret = control('get', 'secret', renewal['secret'], '-o', 'json')
    data = {key: base64.b64decode(secret['data'][key], validate=True).decode() for key in ('name', 'server', 'project', 'namespaces', 'clusterResources')}
    require(data == {'name': app_id, 'server': target['cluster_server'], 'project': app_id, 'namespaces': app_id, 'clusterResources': 'false'}
            and all(secret['metadata'].get('labels', {}).get(k) == v for k, v in runtime.credentials.LABELS.items()),
            'APPLICATION_CREDENTIAL_BINDING_CHANGED')
    values, _ = ci_binding(profile, binding)
    return {'renewal': renewal, 'application_name': name, 'application': identity(app, spec=True),
            'skip': app['metadata'].get('annotations', {}).get(SKIP) if app else None,
            'project': identity(project, spec=True), 'secret': identity(secret), 'ci_bound': app_id in values}


def unbind_ci(profile, binding):
    values, _ = ci_binding(profile, binding)
    updated = {key: value for key, value in values.items() if key != binding['application_id']}
    if updated != values:
        runtime.github_variable(profile['source_repository'], 'RAILSHOT_TARGET_BINDINGS',
                                {'name': 'RAILSHOT_TARGET_BINDINGS', 'value': json.dumps(updated)})
    observed, _ = ci_binding(profile, binding)
    require(observed == updated, 'CI_BINDING_REMOVAL_UNVERIFIED')


def patch_skip(binding, expected, value):
    if expected['application'] is None:
        return
    control = control_for(binding)
    app = control('get', 'application', expected['application_name'], '-o', 'json')
    require(identity(app, spec=True) == expected['application'] and not app.get('operation')
            and app.get('status', {}).get('operationState', {}).get('phase') not in ('Running', 'Terminating'), 'APPLICATION_SYNC_ACTIVE')
    annotations = dict(app['metadata'].get('annotations', {}))
    if value is None:
        annotations.pop(SKIP, None)
    else:
        annotations[SKIP] = value
    patch = [{'op': 'test', 'path': '/metadata/uid', 'value': app['metadata']['uid']},
             {'op': 'test', 'path': '/metadata/resourceVersion', 'value': app['metadata']['resourceVersion']},
             {'op': 'add', 'path': '/metadata/annotations', 'value': annotations}]
    control('patch', 'application', expected['application_name'], '--type=json', '--patch-file=/dev/stdin', '-o', 'json', document=patch)
    observed = control('get', 'application', expected['application_name'], '-o', 'json')
    require(observed['metadata']['uid'] == app['metadata']['uid'] and observed['metadata'].get('annotations', {}).get(SKIP) == value,
            'APPLICATION_QUIESCE_UNVERIFIED')


def delete_control(binding, kind, name, expected):
    if expected is None:
        return
    control = control_for(binding)
    obj = control('get', kind, name, '--ignore-not-found', '-o', 'json')
    require(obj and identity(obj, spec='spec_sha256' in expected) == expected, 'CONTROL_IDENTITY_CHANGED')
    require(not obj['metadata'].get('finalizers'), 'CONTROL_FINALIZER_UNSUPPORTED')
    if kind == 'application':
        require(not obj.get('operation') and obj.get('status', {}).get('operationState', {}).get('phase') not in ('Running', 'Terminating'), 'APPLICATION_SYNC_ACTIVE')
    prefix = '/api/v1' if kind == 'secret' else '/apis/argoproj.io/v1alpha1'
    resource = {'secret': 'secrets', 'appproject': 'appprojects', 'application': 'applications'}[kind]
    control('delete', '--raw', prefix + '/namespaces/argocd/' + resource + '/' + name, '-f', '-',
            document={'apiVersion': 'v1', 'kind': 'DeleteOptions', 'propagationPolicy': 'Foreground',
                      'preconditions': {'uid': obj['metadata']['uid'], 'resourceVersion': obj['metadata']['resourceVersion']}})
    deadline = time.monotonic() + 60
    while control('get', kind, name, '--ignore-not-found', '-o', 'json'):
        require(time.monotonic() < deadline, 'CONTROL_DELETION_UNVERIFIED')
        time.sleep(1)


def remove_renewal(binding, expected):
    control = control_for(binding); selected = expected['renewal']
    cm = control('get', 'configmap', 'railshot-credentials', '-o', 'json')
    policy = runtime.credentials.validate_policy(json.loads(cm['data']['policy.json']))
    require([item for item in policy['targets'] if item['target_id'] == binding['application_id']] == [selected], 'RENEWAL_POLICY_CHANGED')
    policy['targets'] = [item for item in policy['targets'] if item != selected]
    role = control('get', 'role', 'railshot-credentials', '-o', 'json')
    rules = role.get('rules') or []
    require(len(rules) == 1 and rules[0].get('apiGroups') == [''] and rules[0].get('resources') == ['secrets']
            and rules[0].get('verbs') == ['get', 'patch'] and selected['secret'] in rules[0].get('resourceNames', []), 'RENEWAL_ROLE_CHANGED')
    names = [name for name in rules[0]['resourceNames'] if name != selected['secret']]
    # An empty resourceNames list grants every name. Remove the rule instead.
    role['rules'] = [{**rules[0], 'resourceNames': names}] if names else []
    control('replace', '-f', '-', '-o', 'json', document=role)
    cm['data']['policy.json'] = json.dumps(policy)
    control('replace', '-f', '-', '-o', 'json', document=cm)
    observed = control('get', 'configmap', 'railshot-credentials', '-o', 'json')
    require(json.loads(observed['data']['policy.json']) == policy
            and (control('get', 'role', 'railshot-credentials', '-o', 'json').get('rules') or []) == role['rules'], 'RENEWAL_REMOVAL_UNVERIFIED')


def revoke_control_grants(binding, expected):
    control = control_for(binding)
    role = control('get', 'role', 'railshot-product-registrations', '-o', 'json')
    names = {('argoproj.io', 'applications'): expected['application_name'],
             ('argoproj.io', 'appprojects'): binding['application_id'], ('', 'secrets'): expected['renewal']['secret']}
    updated = []
    for rule in role.get('rules') or []:
        key = (rule['apiGroups'][0], rule['resources'][0])
        require(key in names and rule['verbs'] in (['get', 'patch'], ['get', 'patch', 'delete'])
                and rule.get('resourceNames'), 'CONTROL_ROLE_CHANGED')
        remaining = [name for name in rule['resourceNames'] if name != names[key]]
        if remaining:
            updated.append({**rule, 'resourceNames': remaining})
    role['rules'] = updated
    control('replace', '-f', '-', '-o', 'json', document=role)
    require((control('get', 'role', 'railshot-product-registrations', '-o', 'json').get('rules') or []) == updated,
            'CONTROL_GRANT_REMOVAL_UNVERIFIED')


def collect(kube, binding, profile, action, approved_edge=None, *, allow_active=False):
    control = control_inventory(binding, profile, allow_active=allow_active)
    require(action == 'delete' or control['application'] is not None, 'APPLICATION_NOT_DEPLOYED')
    inventory = workloads.inventory(kube, binding, control['renewal'], action)
    if approved_edge is not None:
        edge.validate(binding, action, approved_edge)
    provider = approved_edge if approved_edge is not None else edge.plan(binding, action)
    app_id = binding['application_id']
    resources = list(inventory['resources']) + list(provider['resources'])
    retained = list(inventory.get('retained', [])) + list(provider.get('retained', []))
    refs = [reference('AppProject', app_id, 'argocd'), reference('Credential', control['renewal']['secret'], 'argocd'),
            reference('CIBinding', app_id)]
    if control['application']:
        refs.append(reference('Application', control['application_name'], 'argocd'))
    (resources if action == 'delete' else retained).extend(refs)
    retained.extend([reference('SharedRuntime', binding['environment_id']), reference('AuditRecord', app_id), reference('BuildArtifacts', app_id)])
    def unique(items):
        return sorted({applications.digest(item): item for item in items}.values(), key=lambda x: (x['kind'], x['name'], x.get('namespace', '')))
    return {'control': control, 'runtime': inventory, 'edge': provider, 'resources': unique(resources), 'retained': unique(retained)}


def validate_request(request):
    common = {'version', 'phase', 'application_id', 'environment_id', 'app', 'action', 'operation_id'}
    require(isinstance(request, dict) and common <= set(request) and request['version'] == 1
            and type(request['version']) is int and request['phase'] in ('plan', 'apply')
            and request['action'] in ('stop', 'start', 'delete'), 'LIFECYCLE_REQUEST_INVALID')
    extra = {'plan_id', 'plan_hash', 'delete_data'} if request['phase'] == 'apply' else set()
    require(set(request) <= common | extra, 'LIFECYCLE_REQUEST_INVALID')
    for key in ('operation_id', *(['plan_id'] if request['phase'] == 'apply' else [])):
        require(isinstance(request.get(key), str) and str(uuid.UUID(request[key])) == request[key], 'LIFECYCLE_REQUEST_INVALID')
    if request['phase'] == 'apply':
        require(isinstance(request.get('plan_hash'), str) and re.fullmatch(r'[a-f0-9]{64}', request['plan_hash']), 'LIFECYCLE_REQUEST_INVALID')
        require(request.get('delete_data') is True if request['action'] == 'delete' else request.get('delete_data') in (None, False), 'DELETE_DATA_CONFIRMATION_REQUIRED')


def never_registered(config, request, home):
    """The registrar commits registration.json BEFORE any external write.

    Under its lock, absence of that intent and binding proves this registration
    never began. Only a local tombstone is written; no cloud resource is adopted.
    """
    selected = {key: request[key] for key in ('environment_id', 'app', 'application_id')}
    *_, fingerprint = applications._inputs(config, selected)
    require(request['action'] == 'delete', 'APPLICATION_NOT_REGISTERED')
    require(not any((home / name).exists() or (home / name).is_symlink() for name in ('registration.json', 'binding.json')),
            'APPLICATION_REGISTRATION_INCOMPLETE')
    home = applications.private_directory(home)
    directory = applications.private_directory(home / 'lifecycle')
    plans = applications.private_directory(directory / 'plans')
    operations = applications.private_directory(directory / 'operations')
    state_path = home / 'lifecycle.json'
    journal = operations / (request['operation_id'] + '.json')
    request_sha = applications.digest(request)
    if request['phase'] == 'apply' and journal.exists():
        saved = runtime.read_private(journal)
        require(saved['request_sha256'] == request_sha, 'LIFECYCLE_OPERATION_CONFLICT')
        return saved['result']
    require(not state_path.exists(), 'APPLICATION_LIFECYCLE_BLOCKED')
    retained = [reference('SharedRuntime', request['environment_id']), reference('AuditRecord', request['application_id']),
                reference('BuildArtifacts', request['application_id'])]
    if request['phase'] == 'plan':
        expires = datetime.fromtimestamp(time.time() + TTL, timezone.utc).isoformat().replace('+00:00', 'Z')
        plan = {'version': 1, 'never_registered': True, **selected, 'action': 'delete', 'plan_id': request['operation_id'],
                'registration_sha256': fingerprint, 'expires_at': expires, 'resources': [], 'retained': retained}
        path = plans / (plan['plan_id'] + '.json')
        require(not path.exists(), 'LIFECYCLE_PLAN_CONFLICT')
        runtime.save(path, plan)
        return {'status': 'planned', 'plan_id': plan['plan_id'], 'plan_hash': applications.digest(plan),
                'resources': [], 'retained': retained, 'expires_at': expires}
    plan = runtime.read_private(plans / (request['plan_id'] + '.json'))
    require(applications.digest(plan) == request['plan_hash'] and plan.get('never_registered') is True
            and plan.get('action') == 'delete' and all(plan.get(key) == value for key, value in selected.items())
            and plan.get('registration_sha256') == fingerprint, 'LIFECYCLE_PLAN_CHANGED')
    require(datetime.fromisoformat(plan['expires_at'].replace('Z', '+00:00')).timestamp() > time.time(), 'LIFECYCLE_PLAN_EXPIRED')
    result = {'status': 'unknown', 'application_id': request['application_id'], 'action': 'delete', 'steps': [], 'residuals': []}
    runtime.save(journal, {'request_sha256': request_sha, 'result': result})
    runtime.save(state_path, {'status': 'deleted', 'operation_id': request['operation_id'], 'never_registered': True})
    result.update(status='succeeded', steps=[{'name': 'unstarted-registration-removed', 'status': 'succeeded'}])
    runtime.save(journal, {'request_sha256': request_sha, 'result': result})
    return result


def verify_public_health(binding, plan):
    path = plan['private'].get('health_path')
    require(isinstance(path, str), 'APPLICATION_HEALTH_CONTRACT_MISSING')
    config = {'url': 'https://' + binding['hostname'] + path, 'expected_status': 200}
    runtime.bridge.validate_public_http(config, path)
    deadline = time.monotonic() + 120
    while True:
        if runtime.bridge.public_probe(config, path)['state'] == 'succeeded':
            return
        require(time.monotonic() < deadline, 'APPLICATION_PUBLIC_HEALTH_UNVERIFIED')
        time.sleep(2)


def lifecycle(config_path, request):
    validate_request(request)
    config = applications.load_config(config_path)
    root = applications.private_directory(config['state_dir'])
    # ponytail: serialize all app ownership writers; shard by environment if contention matters.
    with os.fdopen(os.open(root / 'registration.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600), 'a') as lock:
        info = os.fstat(lock.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and not info.st_mode & 0o077, 'APPLICATION_STORAGE_INVALID')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise applications.RegistrationError('REGISTRATION_BUSY') from None
        require(isinstance(request.get('application_id'), str) and re.fullmatch(r'app-[a-f0-9]{24}', request['application_id']), 'LIFECYCLE_REQUEST_INVALID')
        candidate = root / request['application_id']
        if not (candidate / 'registration.json').exists():
            return never_registered(config, request, candidate)
        home, binding, native, profile, binding_sha = load_binding(config, request)
        state_path = home / 'lifecycle.json'
        state = runtime.read_private(state_path) if state_path.exists() else {'status': 'ready'}
        action = request['action']; phase = request['phase']
        directory = applications.private_directory(home / 'lifecycle')
        plans = applications.private_directory(directory / 'plans')
        operations = applications.private_directory(directory / 'operations')
        journal = operations / (request['operation_id'] + '.json')
        fingerprint = applications.digest(request)
        if phase == 'apply' and journal.exists():
            saved = runtime.read_private(journal)
            require(saved['request_sha256'] == fingerprint, 'LIFECYCLE_OPERATION_CONFLICT')
            result = saved['result']
            if result['status'] == 'running':
                result = {**result, 'status': 'unknown', 'error': {'code': 'LIFECYCLE_RECONCILE_REQUIRED', 'retryable': False, 'outcome_unknown': True}}
            return result
        require(state['status'] in (('stopped',) if action == 'start' else ('ready',) if action == 'stop' else ('ready', 'stopped')), 'APPLICATION_LIFECYCLE_BLOCKED')
        with runtime.runtime_kubectl(native) as kube:
            selected = {key: request[key] for key in ('application_id', 'environment_id', 'app', 'action')}
            plan = None
            if phase == 'apply':
                plan = runtime.read_private(plans / (request['plan_id'] + '.json'))
                require(applications.digest(plan) == request['plan_hash'] and all(plan.get(k) == v for k, v in selected.items())
                        and plan.get('binding_sha256') == binding_sha and plan.get('state_sha256') == applications.digest(state), 'LIFECYCLE_PLAN_CHANGED')
                require(datetime.fromisoformat(plan['expires_at'].replace('Z', '+00:00')).timestamp() > time.time(), 'LIFECYCLE_PLAN_EXPIRED')
            snapshot = collect(kube, binding, profile, action, plan['snapshot']['edge'] if plan else None,
                               allow_active=phase == 'plan' and action == 'delete')
            if phase == 'plan':
                plan_id = request['operation_id']
                expires = datetime.fromtimestamp(time.time() + TTL, timezone.utc).isoformat().replace('+00:00', 'Z')
                plan = {'version': 1, **selected, 'plan_id': plan_id, 'binding_sha256': binding_sha,
                        'state_sha256': applications.digest(state), 'expires_at': expires, 'snapshot': snapshot}
                path = plans / (plan_id + '.json')
                require(not path.exists(), 'LIFECYCLE_PLAN_CONFLICT')
                runtime.save(path, plan)
                return {'status': 'planned', 'plan_id': plan_id, 'plan_hash': applications.digest(plan),
                        'resources': snapshot['resources'], 'retained': snapshot['retained'], 'expires_at': expires}
            require(snapshot['control'] == plan['snapshot']['control'] and workloads.comparable(snapshot['runtime']) ==
                    workloads.comparable(plan['snapshot']['runtime']), 'LIFECYCLE_PLAN_CHANGED')
            result = {'status': 'running', 'application_id': binding['application_id'], 'action': action,
                      'steps': [], 'residuals': snapshot['resources']}
            def save():
                runtime.save(journal, {'request_sha256': fingerprint, 'result': result})
            def step(name, callback):
                result['steps'].append({'name': name, 'status': 'running'}); save()
                value = callback()
                if isinstance(value, dict):
                    require(value.get('status', 'succeeded') == 'succeeded' and not value.get('residuals'), 'LIFECYCLE_STEP_UNVERIFIED')
                result['steps'][-1]['status'] = 'succeeded'; save()
                return value
            save()  # Durable operation intent precedes every external mutation.
            runtime.save(state_path, {'status': {'delete': 'deleting', 'stop': 'stopping', 'start': 'starting'}[action], 'operation_id': request['operation_id']})
            try:
                expected = snapshot['control']
                if action in ('stop', 'delete'):
                    step('disable-ci', lambda: unbind_ci(profile, binding))
                    step('grant-cleanup', lambda: runtime.grant_control_objects(binding['cd'], binding['registered'], binding['application_id']))
                    if action == 'delete':
                        step('remove-gitops', lambda: delete_control(binding, 'application', expected['application_name'], expected['application']))
                    else:
                        step('pause-gitops', lambda: patch_skip(binding, expected, 'true'))
                    step('remove-route', lambda: edge.execute(binding, action, snapshot['edge']))
                    if action == 'delete':
                        step('remove-renewal', lambda: remove_renewal(binding, expected))
                    step('runtime-cleanup', lambda: workloads.execute(kube, binding, action, snapshot['runtime']))
                    if action == 'delete':
                        step('remove-credential', lambda: delete_control(binding, 'secret', expected['renewal']['secret'], expected['secret']))
                        step('remove-project', lambda: delete_control(binding, 'appproject', binding['application_id'], expected['project']))
                        step('revoke-permissions', lambda: revoke_control_grants(binding, expected))
                else:
                    step('resume-runtime', lambda: workloads.execute(kube, binding, action, snapshot['runtime'], stopped_inventory=state['runtime']))
                    step('restore-route', lambda: edge.execute(binding, action, snapshot['edge']))
                    step('verify-public-health', lambda: verify_public_health(binding, snapshot['edge']))
                    step('resume-gitops', lambda: patch_skip(binding, expected, state['skip']))
                    step('enable-ci', lambda: runtime.bind_ci(profile, binding['registered'], binding['application_id']))
                completed = {'status': {'stop': 'stopped', 'start': 'ready', 'delete': 'deleted'}[action], 'operation_id': request['operation_id']}
                if action == 'stop':
                    completed.update(runtime=snapshot['runtime'], skip=expected['skip'])
                runtime.save(state_path, completed)
                result.update(status='succeeded', residuals=[]); save()
            except Exception as exc:
                if result['steps'] and result['steps'][-1]['status'] == 'running':
                    result['steps'][-1]['status'] = 'unknown'
                code = getattr(exc, 'code', 'LIFECYCLE_RECONCILE_REQUIRED')
                if not isinstance(code, str) or not re.fullmatch(r'[A-Z][A-Z0-9_]{0,99}', code):
                    code = 'LIFECYCLE_RECONCILE_REQUIRED'
                result.update(status='unknown', error={'code': code, 'retryable': False, 'outcome_unknown': True})
                try:
                    runtime.save(state_path, {'status': 'unknown', 'operation_id': request['operation_id']})
                    save()
                except Exception:
                    raise applications.RegistrationError('LIFECYCLE_RECONCILE_REQUIRED', unknown=True) from None
            return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True); parser.add_argument('--request', required=True)
    args = parser.parse_args(); os.umask(0o077)
    request = {}
    try:
        request = runtime.read_private(args.request)
        result = lifecycle(args.config, request)
    except Exception as exc:
        code = exc.code if isinstance(exc, applications.RegistrationError) else 'LIFECYCLE_PLAN_UNAVAILABLE'
        unknown = getattr(exc, 'unknown', False) is True
        result = {'status': 'unknown' if unknown else 'blocked', 'steps': [], 'residuals': [],
                  **{key: request[key] for key in ('application_id', 'action') if isinstance(request, dict) and key in request},
                  'error': {'code': code, 'retryable': False, 'outcome_unknown': unknown}}
    print(json.dumps(result))  # Structured failures are still a successfully executed CLI request.
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
