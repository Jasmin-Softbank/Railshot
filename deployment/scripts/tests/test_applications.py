"""Model/cloud-free app registration checks; native helpers use the existing fake Kubernetes API."""
import base64
from contextlib import contextmanager, redirect_stdout
import copy
import fcntl
import hashlib
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'deployment/scripts'))
import applications as apps
import environment as env
import test_environment as existing_fixtures


class ApplicationsTest(unittest.TestCase):
    def test_policy_capacity_is_not_reported_as_invalid_user_input(self):
        output = io.StringIO()
        with patch.object(sys, 'argv', ['applications.py', '--config', '/unused', '--request', '/unused']), \
                patch.object(apps.os, 'umask'), patch.object(env, 'read_private', return_value={}), \
                patch.object(apps, 'register', side_effect=env.credentials.PolicyCapacityError('policy full')), \
                redirect_stdout(output):
            self.assertEqual(apps.main(), 3)
        result = json.loads(output.getvalue())
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['error']['code'], 'APPLICATION_CREDENTIAL_POLICY_CAPACITY_EXCEEDED')
        self.assertFalse(result['error']['outcome_unknown'])

    def setUp(self):
        # Reuse the existing synthetic runtime/control/GitHub fixture, not a real executor.
        self.fixture = existing_fixtures.RegistrationTest(methodName='runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root, self.env_id = self.fixture.root, self.fixture.target
        self.config = {'version': 1, 'state_dir': str(self.root / 'applications'),
            'registry_file': str(self.root / 'registry.json'),
            'cd': {key: value for key, value in self.fixture.config['cd'].items() if key != 'targets'},
            'environments': {self.env_id: {'provider': 'aws', 'tenant': 'demo', 'source_repository': 'owner/source',
                'pull_secret_file': str(self.root / 'pull.json'), 'target': {'repo_url': 'https://github.com/owner/config',
                    'architecture': 'amd64', 'ingress_cidrs': ['10.0.0.0/8'],
                    'resources': {'requests': {'cpu': '100m', 'memory': '128Mi'}, 'limits': {'cpu': '500m', 'memory': '512Mi'}}},
                'ingress': {'base_domain': 'railshot.io', 'mode': 'operator-finalized'}}}}
        self.config_path = self.fixture.write('applications.json', self.config)
        # The runtime already has one bounded environment credential. Apps must
        # extend that server cache instead of registering the server again.
        cm = self.fixture.control.objects['argocd', 'configmap', 'railshot-credentials']
        anchor = json.loads(cm['data']['policy.json'])['targets'][0]
        ca = b'-----BEGIN CERTIFICATE-----\nfake'
        anchor.update(secret='railshot-' + self.env_id, target_id=self.env_id,
                      server='https://' + self.fixture.descriptor['addresses']['private'] + ':6443',
                      ca_sha256=hashlib.sha256(ca).hexdigest())
        cm['data']['policy.json'] = json.dumps({'version': 1, 'targets': [anchor]})
        self.fixture.control.objects['argocd', 'role', 'railshot-credentials']['rules'][0]['resourceNames'] = [anchor['secret']]
        token = self.fixture.runtime('old-app', 'create', '--raw')['status']['token']
        data = {'name': self.env_id, 'server': anchor['server'], 'project': anchor['project'],
                'namespaces': 'old-app', 'clusterResources': 'false', 'config': json.dumps({
                    'bearerToken': token, 'tlsClientConfig': {'caData': base64.b64encode(ca).decode(), 'insecure': False}})}
        self.fixture.control.objects['argocd', 'secret', anchor['secret']] = {'apiVersion': 'v1', 'kind': 'Secret',
            'metadata': {'name': anchor['secret'], 'namespace': 'argocd', 'labels': dict(env.credentials.LABELS)},
            'data': {k: base64.b64encode(v.encode()).decode() for k, v in data.items()}}
        self.fixture.runtime.objects['old-app', 'serviceaccount', env.SA] = {'metadata': {'uid': anchor['service_account']['uid']}}
        for document in self.fixture.control.objects.values():
            document.setdefault('metadata', {}).setdefault('uid', '12345678-1234-1234-1234-123456789012')
            document['metadata'].setdefault('resourceVersion', '1')
        self.accesses = []
        def customer(server, ca, token, path, doc, **kw):
            item = doc['spec']['resourceAttributes']; self.accesses.append((server, item, kw))
            reader = self.fixture.runtime.objects.get(('default', 'clusterrole', 'railshot-argocd-cache'), {})
            cluster_read = any(item['resource'] in rule['resources'] and item['group'] in rule['apiGroups']
                               and item['verb'] in rule['verbs'] for rule in reader.get('rules', []))
            return {'status': {'allowed': cluster_read or item['namespace'].startswith('app-') and item['resource'] == 'deployments'}}
        self.enterContext(patch.object(env.credentials, 'customer', customer))
        self.native = self.enterContext(patch.object(env.argo, 'native', side_effect=AssertionError('no native cloud command permitted')))
        self.execute = self.enterContext(patch.object(env.ansible, 'execute', side_effect=AssertionError('no install/provision permitted')))

    def write_config(self):
        self.fixture.write('applications.json', self.config)

    def request(self, app='first-app', environment_id=None):
        env_id = environment_id or self.env_id
        tenant = self.config['environments'][env_id]['tenant']
        return {'environment_id': env_id, 'app': app, 'application_id': apps.application_id(env_id, tenant, app)}

    def register(self, app='first-app', environment_id=None):
        return apps.register(self.config_path, self.request(app, environment_id))

    def home(self, app='first-app'):
        return Path(self.config['state_dir']) / self.request(app)['application_id']

    def enable_fixed_cache(self):
        cm = self.fixture.control.objects['argocd', 'configmap', 'railshot-credentials']
        shared = next(row for row in json.loads(cm['data']['policy.json'])['targets'] if row['target_id'] == self.env_id)
        return env.enable_shared_cluster_cache(self.fixture.runtime, {'context': 'control'},
            {'target': {'cluster_server': shared['server']}}, self.env_id)

    def test_same_environment_two_apps_have_separate_rbac_and_replay_without_writes(self):
        self.enable_fixed_cache()
        first = self.register(); self.assertEqual(first['status'], 'succeeded', first)
        shared_before = copy.deepcopy(self.fixture.control.objects['argocd', 'secret', 'railshot-' + self.env_id])
        second = self.register('second-app'); self.assertEqual(second['status'], 'succeeded', second)
        self.assertNotEqual(first['namespace'], second['namespace'])
        self.assertNotEqual(first['node_port'], second['node_port'])
        self.assertNotEqual(first['hostname'], second['hostname'])
        counters = (self.fixture.runtime.applications, self.fixture.control.applications)
        self.assertEqual(self.register(), first)
        self.assertEqual(counters, (self.fixture.runtime.applications, self.fixture.control.applications))
        self.assertFalse(first['deployment_supported'])
        for record in (first, second):
            app_id = record['application_id']
            role = self.fixture.runtime.objects[app_id, 'role', env.SA]
            self.assertNotIn('secrets', [resource for rule in role['rules'] for resource in rule['resources']])
            secret = self.fixture.control.objects['argocd', 'secret', 'railshot-' + app_id]
            self.assertEqual(base64.b64decode(secret['data']['namespaces']).decode(), app_id)
            self.assertEqual(base64.b64decode(secret['data']['project']).decode(), app_id)
            self.assertEqual(base64.b64decode(secret['data']['clusterResources']).decode(), 'false')
            self.assertEqual(secret['metadata']['labels']['argocd.argoproj.io/secret-type'], 'railshot-application')
            shared_binding = self.fixture.runtime.objects[app_id, 'rolebinding', 'railshot-environment-argocd']
            self.assertEqual(shared_binding['subjects'], [{'kind': 'ServiceAccount', 'name': env.SA, 'namespace': 'old-app'}])
            self.assertIn(app_id, json.loads(self.fixture.variable['value']))
        clusters = [value for value in self.fixture.control.objects.values()
                    if value.get('metadata', {}).get('labels', {}).get('argocd.argoproj.io/secret-type') == 'cluster']
        self.assertEqual(len(clusters), 1)
        self.assertEqual(base64.b64decode(clusters[0]['data']['namespaces']), b'')
        self.assertEqual(clusters[0], shared_before, 'adding another app must not invalidate the shared Argo cache')
        self.assertEqual(base64.b64decode(clusters[0]['data']['project']), b'')
        policy = json.loads(self.fixture.control.objects['argocd', 'configmap', 'railshot-credentials']['data']['policy.json'])
        shared = next(row for row in policy['targets'] if row['target_id'] == self.env_id)
        self.assertEqual(shared['namespaces'], ['old-app'])
        self.assertTrue(shared['cluster_read'])
        self.native.assert_not_called(); self.execute.assert_not_called()

    def test_fixed_cache_reader_is_list_watch_only_and_foreign_binding_is_not_adopted(self):
        self.enable_fixed_cache()
        first = self.register(); self.assertEqual(first['status'], 'succeeded', first)
        role = self.fixture.runtime.objects['default', 'clusterrole', 'railshot-argocd-cache']
        resources = {resource for rule in role['rules'] for resource in rule['resources']}
        self.assertEqual(resources, {'pods', 'services', 'persistentvolumeclaims', 'deployments',
                                    'replicasets', 'jobs', 'networkpolicies'})
        self.assertTrue(all(rule['verbs'] == ['list', 'watch'] for rule in role['rules']))
        binding = self.fixture.runtime.objects['default', 'clusterrolebinding', 'railshot-argocd-cache']
        self.assertEqual(binding['subjects'], [{'kind': 'ServiceAccount', 'name': env.SA, 'namespace': 'old-app'}])
        shared_secret = copy.deepcopy(self.fixture.control.objects['argocd', 'secret', 'railshot-' + self.env_id])
        policy = json.loads(self.fixture.control.objects['argocd', 'configmap', 'railshot-credentials']['data']['policy.json'])
        shared = next(row for row in policy['targets'] if row['target_id'] == self.env_id)
        registered = {'target': {'cluster_server': shared['server']}}
        binding['metadata']['labels']['railshot.io/registration'] = 'foreign'
        with self.assertRaisesRegex(ValueError, 'cache reader owner differs'):
            env.enable_shared_cluster_cache(self.fixture.runtime, {'context': 'control'}, registered, self.env_id)
        self.assertEqual(shared_secret, self.fixture.control.objects['argocd', 'secret', shared_secret['metadata']['name']])

    def test_fixed_cache_migration_resumes_both_sides_of_secret_cas_without_repatching(self):
        for phase in ('transition', 'scope', 'final'):
            with self.subTest(phase=phase):
                case = ApplicationsTest(methodName='runTest'); case.setUp()
                try:
                    original = case.fixture.control.__call__
                    patches = []
                    def control(namespace, *args, document=None):
                        result = original(namespace, *args, document=document)
                        stage = None
                        if args[:3] == ('patch', 'secret', 'railshot-' + case.env_id):
                            patches.append(True); stage = 'scope'
                        elif args[0] == 'replace' and document.get('kind') == 'ConfigMap':
                            selected = next(row for row in json.loads(document['data']['policy.json'])['targets']
                                            if row['target_id'] == case.env_id)
                            stage = 'transition' if 'previous_scope' in selected else 'final'
                        if stage == phase:
                            raise OSError('write committed but reply lost')
                        return result
                    with patch.object(env.argo, 'kubectl', lambda context, *a, **kw: control(*a, **kw)), self.assertRaises(OSError):
                        case.enable_fixed_cache()
                    migrated = case.enable_fixed_cache()
                    self.assertTrue(migrated['cluster_read'])
                    self.assertNotIn('previous_scope', migrated)
                    cm = copy.deepcopy(case.fixture.control.objects['argocd', 'configmap', 'railshot-credentials'])
                    secret = copy.deepcopy(case.fixture.control.objects['argocd', 'secret', migrated['secret']])
                    self.assertEqual(secret['data']['namespaces'], '')
                    counters = case.fixture.runtime.applications, case.fixture.control.applications
                    self.assertEqual(case.enable_fixed_cache(), migrated)
                    self.assertEqual((case.fixture.runtime.applications, case.fixture.control.applications), counters)
                    self.assertEqual(cm, case.fixture.control.objects['argocd', 'configmap', 'railshot-credentials'])
                    self.assertEqual(secret, case.fixture.control.objects['argocd', 'secret', migrated['secret']])
                finally:
                    case.doCleanups()

    def test_customer_inclusions_preserve_full_control_and_exclude_secrets(self):
        customer = 'https://192.0.2.10:6443'
        control = 'https://kubernetes.default.svc'
        entries = env.customer_cache_inclusions([customer], [control, customer])
        self.assertEqual(entries[0], {'apiGroups': ['*'], 'kinds': ['*'], 'clusters': [control]})
        self.assertTrue(all(row['clusters'] == [customer] for row in entries[1:]))
        kinds = {kind for row in entries[1:] for kind in row['kinds']}
        self.assertEqual(kinds, {'Pod', 'Service', 'PersistentVolumeClaim', 'Deployment', 'ReplicaSet', 'Job', 'NetworkPolicy'})
        supported = {row['kind'] for row in env.argo.KINDS + [env.argo.PVC_KIND, env.argo.JOB_KIND]}
        self.assertTrue(supported <= kinds)
        for customers, others in [([], [control]), ([customer], []), ([customer], [customer]), (['*'], [control])]:
            with self.subTest(customers=customers, others=others), self.assertRaises(ValueError):
                env.customer_cache_inclusions(customers, others)

    def test_completed_legacy_app_migrates_only_cluster_scope_and_preserves_binding(self):
        first = self.register(); app_id = first['application_id']
        first.pop('cluster_registration'); first['steps'].remove('cluster')
        env.save(self.home() / 'registration.json', first)
        secret = self.fixture.control.objects['argocd', 'secret', 'railshot-' + app_id]
        secret['metadata']['labels']['argocd.argoproj.io/secret-type'] = 'cluster'
        before_data = copy.deepcopy(secret['data'])
        before_binding = (self.home() / 'binding.json').read_bytes()
        result = self.register()
        self.assertEqual(result['status'], 'succeeded', result)
        self.assertEqual(result['node_port'], first['node_port'])
        self.assertEqual((self.home() / 'binding.json').read_bytes(), before_binding)
        migrated = self.fixture.control.objects['argocd', 'secret', 'railshot-' + app_id]
        self.assertEqual(migrated['data'], before_data)
        self.assertEqual(migrated['metadata']['labels']['argocd.argoproj.io/secret-type'], 'railshot-application')

    def test_existing_registration_gains_storage_permissions_once(self):
        first = self.register(); app_id = first['application_id']
        first.pop('runtime_permissions_version')
        env.save(self.home() / 'registration.json', first)
        role = self.fixture.runtime.objects[app_id, 'role', env.SA]
        for rule in role['rules']:
            rule['resources'] = [r for r in rule['resources'] if r != 'persistentvolumeclaims']
        project = self.fixture.control.objects['argocd', 'appproject', app_id]
        project['spec']['namespaceResourceWhitelist'] = env.argo.KINDS
        before = (self.home() / 'binding.json').read_bytes()
        result = self.register()
        self.assertEqual(result['status'], 'succeeded', result)
        self.assertEqual(result['runtime_permissions_version'], 2)
        self.assertEqual((self.home() / 'binding.json').read_bytes(), before)
        role = self.fixture.runtime.objects[app_id, 'role', env.SA]
        self.assertIn('persistentvolumeclaims', [r for rule in role['rules'] for r in rule['resources']])
        project = self.fixture.control.objects['argocd', 'appproject', app_id]
        self.assertIn(env.argo.PVC_KIND, project['spec']['namespaceResourceWhitelist'])
        counters = (self.fixture.runtime.applications, self.fixture.control.applications)
        self.assertEqual(self.register(), result)
        self.assertEqual(counters, (self.fixture.runtime.applications, self.fixture.control.applications))

    def test_shared_scope_partial_write_requires_reconciliation_without_replay(self):
        original = self.fixture.control.__call__
        def fail_policy(namespace, *args, document=None):
            if args[0] == 'replace' and document.get('kind') == 'ConfigMap':
                policy = json.loads(document['data']['policy.json'])
                if any(row['target_id'] == self.env_id and row['project'] == '' for row in policy['targets']):
                    raise OSError('ambiguous write')
            return original(namespace, *args, document=document)
        with patch.object(env.argo, 'kubectl', lambda context, *a, **kw: fail_policy(*a, **kw)):
            first = self.register()
        self.assertEqual(first['status'], 'unknown')
        counts = self.fixture.runtime.applications, self.fixture.control.applications
        self.assertEqual(self.register()['status'], 'unknown')
        self.assertEqual((self.fixture.runtime.applications, self.fixture.control.applications), counts)

    def test_interruption_after_each_scope_write_keeps_environment_credential_valid(self):
        for interrupted_stage in ('transition', 'scope', 'final'):
            with self.subTest(stage=interrupted_stage):
                case = ApplicationsTest(methodName='runTest'); case.setUp()
                try:
                    original = case.fixture.control.__call__
                    def interrupt(namespace, *args, document=None):
                        result = original(namespace, *args, document=document)
                        stage = None
                        if args[0] == 'replace' and document.get('kind') == 'ConfigMap':
                            rows = json.loads(document['data']['policy.json'])['targets']
                            row = next(row for row in rows if row['target_id'] == case.env_id)
                            if row['project'] == '':
                                stage = 'transition' if 'previous_scope' in row else 'final'
                        elif args[:3] == ('patch', 'secret', 'railshot-' + case.env_id):
                            stage = 'scope'
                        if stage == interrupted_stage:
                            raise OSError('write committed but response lost')
                        return result
                    with patch.object(env.argo, 'kubectl', lambda context, *a, **kw: interrupt(*a, **kw)):
                        result = case.register()
                    self.assertEqual(result['status'], 'unknown', result)
                    objects = case.fixture.control.objects
                    policy = env.credentials.validate_policy(json.loads(objects['argocd', 'configmap', 'railshot-credentials']['data']['policy.json']))
                    row = next(row for row in policy['targets'] if row['target_id'] == case.env_id)
                    env.credentials.registration(objects['argocd', 'secret', row['secret']], row, env.time.time())
                    counts = case.fixture.runtime.applications, case.fixture.control.applications
                    self.assertEqual(case.register()['status'], 'unknown')
                    self.assertEqual((case.fixture.runtime.applications, case.fixture.control.applications), counts)
                    if interrupted_stage != 'final':
                        with self.assertRaisesRegex(ValueError, 'transition requires reconciliation'):
                            case.register('another-app')
                        self.assertEqual((case.fixture.runtime.applications, case.fixture.control.applications), counts)
                finally:
                    case.doCleanups()

    def test_oversized_renewal_policy_rejects_before_namespace_role_argo_or_ci_writes(self):
        self.fixture.fill_renewal_policy(20)
        before = copy.deepcopy(self.fixture.control.objects)
        with patch.object(env.credentials, 'MAX_POLICY_BYTES', 1), self.assertRaises(env.credentials.PolicyCapacityError):
            self.register()
        self.assertEqual((self.fixture.runtime.applications, self.fixture.control.applications), (0, 0))
        self.assertEqual(self.fixture.control.objects, before)
        self.assertIsNone(self.fixture.variable)
        self.assertFalse((self.home() / 'registration.json').exists())

    def test_ambiguous_runtime_group_blocks_before_registration_and_accepts_attached_selection(self):
        self.enterContext(patch.object(apps, 'reserved_node_ports', return_value=set()))
        self.config['environments'][self.env_id]['ingress']['edge_config_file'] = str(self.root / 'edge.json')
        self.write_config()
        self.native.side_effect = None
        self.native.return_value = json.dumps({'Reservations': [{'Instances': [{
            'InstanceId': 'i-0123456789abcdef0',
            'PrivateIpAddress': self.fixture.descriptor['addresses']['private'],
            'SecurityGroups': [{'GroupId': 'sg-12345678'}, {'GroupId': 'sg-87654321'}],
        }]}]})
        with self.assertRaisesRegex(apps.RegistrationError, 'APPLICATION_AWS_ROUTE_PREFLIGHT_FAILED') as raised:
            self.register()
        self.assertFalse(raised.exception.unknown)
        self.assertEqual((self.fixture.runtime.applications, self.fixture.control.applications), (0, 0))
        self.assertIsNone(self.fixture.variable)
        self.assertFalse((self.home() / 'registration.json').exists())
        self.fixture.descriptor['security_group_id'] = 'sg-12345678'
        self.fixture.write('descriptor.json', self.fixture.descriptor)
        with patch.object(env.argo, 'native', side_effect=RuntimeError('provider observation timed out')):
            with self.assertRaisesRegex(apps.RegistrationError, 'APPLICATION_AWS_ROUTE_PREFLIGHT_FAILED') as raised:
                self.register()
            self.assertFalse(raised.exception.unknown)
            self.assertEqual((self.fixture.runtime.applications, self.fixture.control.applications), (0, 0))
        self.assertEqual(self.register()['status'], 'succeeded')

    def test_registration_exceeds_twenty_and_replay_is_read_only(self):
        self.fixture.fill_renewal_policy(19)
        first = self.register(); self.assertEqual(first['status'], 'succeeded', first)
        self.assertEqual(self.register('second-app')['status'], 'succeeded')
        counts = (self.fixture.runtime.applications, self.fixture.control.applications)
        self.assertEqual(self.register(), first)
        self.assertEqual((self.fixture.runtime.applications, self.fixture.control.applications), counts)
        policy = json.loads(self.fixture.control.objects['argocd', 'configmap', 'railshot-credentials']['data']['policy.json'])
        self.assertEqual(len(policy['targets']), 21)

    def test_duplicate_or_unclaimed_existing_renewal_identity_rejected_before_writes(self):
        cm = self.fixture.control.objects['argocd', 'configmap', 'railshot-credentials']
        original = json.loads(cm['data']['policy.json'])['targets'][0]
        cm['data']['policy.json'] = json.dumps({'version': 1, 'targets': [original, original]})
        with self.assertRaisesRegex(ValueError, 'duplicate registration'):
            self.register()
        app_id = self.request()['application_id']
        previous = {**original, 'target_id': app_id, 'secret': 'railshot-' + app_id,
                    'server': 'https://' + self.fixture.descriptor['addresses']['private'] + ':6443',
                    'project': app_id, 'namespaces': [app_id],
                    'service_account': {**original['service_account'], 'namespace': app_id}}
        cm['data']['policy.json'] = json.dumps({'version': 1, 'targets': [previous]})
        with self.assertRaisesRegex(ValueError, 'renewal target binding conflict'):
            self.register()
        self.assertEqual((self.fixture.runtime.applications, self.fixture.control.applications), (0, 0))
        self.assertIsNone(self.fixture.variable)

    def test_binding_is_private_hash_bound_and_has_no_public_health_or_credentials(self):
        result = self.register()
        binding_file = self.home() / 'binding.json'
        binding = json.loads(binding_file.read_bytes())
        self.assertEqual(result['binding_sha256'], hashlib.sha256(binding_file.read_bytes()).hexdigest())
        self.assertEqual(binding['application_id'], result['target_id'])
        self.assertEqual(binding['environment_id'], self.env_id)
        self.assertNotIn('public_http', binding['registered'])
        self.assertNotIn('targets', binding['cd'])
        self.assertFalse((self.home() / 'cd.json').exists())
        for path in Path(self.config['state_dir']).rglob('*.json'):
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertNotIn('synthetic-secret', path.read_text())
            self.assertNotIn('reader:', path.read_text())
            self.assertNotIn('bearerToken', path.read_text())
        binding['hostname'] = 'forged.railshot.io'
        env.save(binding_file, binding)
        with self.assertRaisesRegex(apps.RegistrationError, 'APPLICATION_BINDING_CHANGED'):
            self.register()

    def test_second_environment_cannot_claim_same_physical_resource(self):
        self.register()
        other = 'another-environment'
        descriptor = {**self.fixture.descriptor, 'target_id': other}
        self.fixture.write('other-descriptor.json', descriptor)
        self.fixture.registry['targets'][other] = {**self.fixture.registry['targets'][self.env_id],
            'descriptor_file': str(self.root / 'other-descriptor.json')}
        self.fixture.write('registry.json', self.fixture.registry)
        self.config['environments'][other] = copy.deepcopy(self.config['environments'][self.env_id]); self.write_config()
        counts = (self.fixture.runtime.applications, self.fixture.control.applications)
        with self.assertRaisesRegex(apps.RegistrationError, 'ENVIRONMENT_OWNERSHIP_CONFLICT'):
            self.register('other-app', other)
        self.assertEqual(counts, (self.fixture.runtime.applications, self.fixture.control.applications))

    def test_same_environment_cannot_silently_retarget_physical_resource(self):
        self.register()
        self.fixture.descriptor['resource_id'] = 'arn:aws:ec2:ap-northeast-2:123456789012:instance/i-fffffffffffffffff'
        self.fixture.descriptor['transport_ref'] = 'ssm:ap-northeast-2:i-fffffffffffffffff'
        self.fixture.write('descriptor.json', self.fixture.descriptor)
        with self.assertRaisesRegex(apps.RegistrationError, 'ENVIRONMENT_OWNERSHIP_CONFLICT'):
            self.register('other-app')

    def test_unknown_mutation_and_started_intent_never_replay_external_writes(self):
        with patch.object(env, 'install_renewal', side_effect=OSError('private error must not escape')):
            result = self.register()
        self.assertEqual(result['status'], 'unknown')
        self.assertTrue(result['error']['outcome_unknown'])
        counts = (self.fixture.runtime.applications, self.fixture.control.applications)
        for status in ('unknown', 'running'):
            result['status'] = status; env.save(self.home() / 'registration.json', result)
            observed = self.register()
            self.assertEqual(observed['status'], 'unknown')
            self.assertEqual(counts, (self.fixture.runtime.applications, self.fixture.control.applications))
        self.assertFalse((self.home() / 'binding.json').exists())
        self.assertIsNone(self.fixture.variable)

    def delete_verified(self, operation='11111111-2222-4333-8444-555555555555', **result):
        home = self.home(); app_id = home.name
        journal = home / 'lifecycle' / 'operations' / (operation + '.json')
        apps.private_directory(journal.parent)
        env.save(journal, {'request_sha256': 'a' * 64, 'result': {'status': 'succeeded', 'application_id': app_id,
                 'action': 'delete', 'steps': [{'name': 'revoke-permissions', 'status': 'succeeded'}], 'residuals': [], **result}})
        env.save(home / 'lifecycle.json', {'status': 'deleted', 'operation_id': operation})
        return operation

    def remove_cluster_objects(self, app_id):
        # The fake equivalent of a verified delete: app-owned objects and its renewal row are gone.
        for objects in (self.fixture.runtime.objects, self.fixture.control.objects):
            for key in [key for key in objects if any(app_id in part for part in key)]:
                del objects[key]
        cm = self.fixture.control.objects['argocd', 'configmap', 'railshot-credentials']
        policy = json.loads(cm['data']['policy.json'])
        policy['targets'] = [row for row in policy['targets'] if row['target_id'] != app_id]
        cm['data']['policy.json'] = json.dumps(policy)

    def test_verified_deleted_registration_is_archived_and_recreated_with_same_identity(self):
        first = self.register(); self.assertEqual(first['status'], 'succeeded', first)
        old_receipt = (self.home() / 'registration.json').read_bytes()
        operation = self.delete_verified(); self.remove_cluster_objects(first['application_id'])
        second = self.register(); self.assertEqual(second['status'], 'succeeded', second)
        self.assertEqual({k: second[k] for k in ('application_id', 'namespace', 'hostname')},
                         {k: first[k] for k in ('application_id', 'namespace', 'hostname')})
        archived = Path(self.config['state_dir']) / 'archive' / first['application_id'] / operation
        # Old receipts, journals and lifecycle state stay intact for audit; the new home has none of them.
        self.assertEqual((archived / 'registration.json').read_bytes(), old_receipt)
        self.assertEqual(env.read_private(archived / 'lifecycle.json')['status'], 'deleted')
        self.assertTrue((archived / 'lifecycle' / 'operations' / (operation + '.json')).exists())
        self.assertFalse((self.home() / 'lifecycle.json').exists())
        self.assertFalse((self.home() / 'lifecycle').exists())
        counts = (self.fixture.runtime.applications, self.fixture.control.applications)
        self.assertEqual(self.register(), second)  # The new generation replays read-only.
        self.assertEqual(counts, (self.fixture.runtime.applications, self.fixture.control.applications))
        # The archived delete operation and its plan cannot act on the new generation.
        import application_lifecycle as lifecycle
        stale = {**self.request(), 'version': 1, 'phase': 'apply', 'action': 'delete', 'operation_id': operation,
                 'plan_id': operation, 'plan_hash': 'b' * 64, 'delete_data': True}
        with self.assertRaises(FileNotFoundError):
            lifecycle.lifecycle(self.config_path, stale)
        self.assertFalse((self.home() / 'lifecycle.json').exists())
        self.assertFalse((self.home() / 'lifecycle' / 'operations' / (operation + '.json')).exists())
        self.assertEqual(counts, (self.fixture.runtime.applications, self.fixture.control.applications))

    def test_uncertain_or_foreign_deletion_state_is_never_archived(self):
        first = self.register(); self.assertEqual(first['status'], 'succeeded', first)
        receipt = self.home() / 'registration.json'
        cases = [{'status': 'unknown'}, {'status': 'running'}, {'action': 'stop'}, {'application_id': 'app-' + 'f' * 24},
                 {'residuals': [{'kind': 'ApplicationNamespace', 'name': first['application_id']}]}]
        for change in cases:
            with self.subTest(change=change):
                self.delete_verified(**change)
                with self.assertRaisesRegex(apps.RegistrationError, 'APPLICATION_LIFECYCLE_BLOCKED'):
                    self.register()
                self.assertTrue(receipt.exists())
        for state in ({'status': 'deleted', 'operation_id': '../../escape'}, {'status': 'deleted'},
                      {'status': 'deleted', 'operation_id': '99999999-2222-4333-8444-555555555555'},
                      {'status': 'deleting', 'operation_id': '11111111-2222-4333-8444-555555555555'},
                      {'status': 'unknown', 'operation_id': '11111111-2222-4333-8444-555555555555'},
                      {'status': 'stopped', 'operation_id': '11111111-2222-4333-8444-555555555555'}):
            with self.subTest(state=state):
                env.save(self.home() / 'lifecycle.json', state)
                with self.assertRaisesRegex(apps.RegistrationError, 'APPLICATION_LIFECYCLE_BLOCKED'):
                    self.register()
                self.assertTrue(receipt.exists())
        self.assertFalse((Path(self.config['state_dir']) / 'archive').exists())

    def test_live_ports_and_persisted_reservations_are_both_excluded(self):
        app_id = self.request()['application_id']; seeded = 30000 + int(app_id[4:], 16) % 2768
        self.fixture.runtime.objects['default', 'service', 'foreign'] = {'spec': {'ports': [{'nodePort': seeded}]}}
        first = self.register(); self.assertNotEqual(first['node_port'], seeded)
        other_id = self.request('second-app')['application_id']; next_port = 30000 + int(other_id[4:], 16) % 2768
        stored = json.loads((self.home() / 'registration.json').read_text()); stored['node_port'] = next_port
        env.save(self.home() / 'registration.json', stored)
        second = self.register('second-app'); self.assertNotEqual(second['node_port'], next_port)

    def test_native_gcp_route_reservations_are_excluded(self):
        config = {'version': 1, 'provider': 'gcp', 'edge_kind': 'native',
            'state_file': str(self.root / 'state.json'), 'state_dir': str(self.root / 'edge-state'),
            'variables_file': str(self.root / 'vars.json'), 'state_lineage': 'fixture',
            'owned_resources': {'google_compute_url_map.app': 'owned'}, 'previous_source_sha': 'a' * 40}
        self.fixture.write('gcp-edge.json', config)
        self.fixture.write('vars.json', {'node_port': 30080, 'routes': {
            'active': {'node_port': 31001}, 'stopped': {'node_port': 31002, 'enabled': False}}})
        profile = {'provider': 'gcp', 'ingress': {'edge_config_file': str(self.root / 'gcp-edge.json')}}
        self.assertEqual(apps.reserved_node_ports(profile, {}), {30080, 31001, 31002})

    def test_legacy_edge_reservation_is_excluded_before_registration(self):
        import edge
        seed = 30000 + int(self.request()['application_id'][4:], 16) % 2768
        self.config['environments'][self.env_id]['ingress']['edge_config_file'] = '/private/aws-edge.json'
        self.write_config()
        with patch.object(env, 'aws_security_group', return_value='sg-0123456789abcdef0'), \
                patch.object(edge, 'reserved_ports', return_value={seed}) as ports:
            result = self.register()
        self.assertNotEqual(result['node_port'], seed)
        ports.assert_called_once_with('/private/aws-edge.json', self.fixture.descriptor['addresses']['private'])

    def test_foreign_namespace_cannot_be_adopted(self):
        app_id = self.request()['application_id']
        self.fixture.runtime.objects[app_id, 'namespace', app_id] = {'metadata': {'name': app_id, 'labels': {'owner': 'foreign'}}}
        with self.assertRaisesRegex(apps.RegistrationError, 'APPLICATION_NAMESPACE_CONFLICT'):
            self.register()
        self.assertEqual(self.fixture.runtime.applications, 0)
        self.assertEqual(self.fixture.control.applications, 0)
        self.assertIsNone(self.fixture.variable)

    def test_gcp_and_openstack_use_verified_endpoint_and_original_tls_name(self):
        for provider in ('gcp', 'openstack'):
            with self.subTest(provider=provider):
                # A fresh fixture for each provider also avoids mixing physical claims.
                case = ApplicationsTest(methodName='runTest'); case.setUp()
                try:
                    profile = case.config['environments'][case.env_id]; profile['provider'] = provider
                    if provider == 'gcp':
                        descriptor = json.loads((ROOT / 'examples/ansible/gcp-node-descriptor.json').read_text())
                        descriptor['target_id'] = case.env_id; descriptor['addresses']['public'] = '34.47.68.21'
                        case.fixture.write('descriptor.json', descriptor)
                        selected = case.fixture.registry['targets'][case.env_id]
                        selected['management_endpoint'] = 'https://34.47.68.21:6443'
                        expected_private = descriptor['addresses']['private']
                    else:
                        case.fixture.use_openstack()
                        selected = case.fixture.registry['targets'][case.env_id]
                        selected['ssh'].update(connect_host='172.31.0.172', port=10022)
                        selected['management_endpoint'] = 'https://172.31.0.172:16443'
                        expected_private = '10.26.1.5'
                    case.fixture.write('registry.json', case.fixture.registry); case.write_config()
                    cm = case.fixture.control.objects['argocd', 'configmap', 'railshot-credentials']
                    policy = json.loads(cm['data']['policy.json'])
                    policy['targets'][0].update(server=selected['management_endpoint'], tls_server_name=expected_private)
                    cm['data']['policy.json'] = json.dumps(policy)
                    secret = case.fixture.control.objects['argocd', 'secret', 'railshot-' + case.env_id]
                    secret['data']['server'] = base64.b64encode(selected['management_endpoint'].encode()).decode()
                    config = json.loads(base64.b64decode(secret['data']['config']))
                    config['tlsClientConfig']['serverName'] = expected_private
                    secret['data']['config'] = base64.b64encode(json.dumps(config).encode()).decode()
                    result = case.register(); self.assertEqual(result['status'], 'succeeded', result)
                    self.assertTrue(case.accesses)
                    self.assertTrue(all(kwargs == {'server_name': expected_private} for _, _, kwargs in case.accesses))
                    self.assertTrue(all(server == selected['management_endpoint'] for server, _, _ in case.accesses))
                    case.native.assert_not_called(); case.execute.assert_not_called()
                finally:
                    case.doCleanups()

    def test_input_template_and_configuration_drift_fail_before_mutation(self):
        request = self.request(); request['application_id'] = 'app-' + '0' * 24
        with self.assertRaisesRegex(apps.RegistrationError, 'APPLICATION_ID_MISMATCH'):
            apps.register(self.config_path, request)
        request = {**self.request(), 'namespace': 'kube-system'}
        with self.assertRaisesRegex(apps.RegistrationError, 'APPLICATION_REQUEST_INVALID'):
            apps.register(self.config_path, request)
        self.assertEqual(self.fixture.runtime.applications, 0)
        self.register()
        self.config['environments'][self.env_id]['ingress']['mode'] = 'changed'; self.write_config()
        with self.assertRaisesRegex(apps.RegistrationError, 'APPLICATION_BINDING_CHANGED'):
            self.register()
        self.config['environments'][self.env_id]['target']['namespace'] = 'kube-system'; self.write_config()
        with self.assertRaisesRegex(apps.RegistrationError, 'APPLICATION_CONFIGURATION_INVALID'):
            apps.load_config(self.config_path)

    def test_concurrent_registration_is_rejected(self):
        root = apps.private_directory(self.config['state_dir'])
        with (root / 'registration.lock').open('w') as lock:
            (root / 'registration.lock').chmod(0o600)
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(apps.RegistrationError, 'REGISTRATION_BUSY'):
                self.register()
        self.assertEqual(self.fixture.runtime.applications, 0)

    def test_tunnel_cleanup_failure_after_writes_stays_unknown(self):
        @contextmanager
        def broken_tunnel(_request):
            yield self.fixture.runtime
            raise ValueError('synthetic private SSM cleanup failure')
        with patch.object(env, 'runtime_kubectl', broken_tunnel):
            with self.assertRaises(apps.RegistrationError) as result:
                self.register()
        self.assertTrue(result.exception.unknown)
        receipt = json.loads((self.home() / 'registration.json').read_text())
        self.assertEqual(receipt['status'], 'unknown')
        counters = (self.fixture.runtime.applications, self.fixture.control.applications)
        self.assertEqual(self.register()['status'], 'unknown')
        self.assertEqual(counters, (self.fixture.runtime.applications, self.fixture.control.applications))

    def test_aws_raw_id_alias_cannot_reclaim_existing_physical_environment(self):
        self.register()
        other = 'alias-environment'
        descriptor = {**self.fixture.descriptor, 'target_id': other,
                      'resource_id': self.fixture.descriptor['resource_id'].split('/')[-1]}
        self.fixture.write('alias.json', descriptor)
        self.fixture.registry['targets'][other] = {**self.fixture.registry['targets'][self.env_id],
            'descriptor_file': str(self.root / 'alias.json')}
        self.fixture.write('registry.json', self.fixture.registry)
        self.config['environments'][other] = copy.deepcopy(self.config['environments'][self.env_id]); self.write_config()
        with self.assertRaisesRegex(apps.RegistrationError, 'ENVIRONMENT_OWNERSHIP_CONFLICT'):
            self.register('other-app', other)

    def test_storage_failure_after_external_write_is_unknown(self):
        real_save = env.save
        def fail_finished(path, value):
            if path.name == 'registration.json' and value.get('steps'):
                raise OSError('private storage failure')
            return real_save(path, value)
        with patch.object(env, 'save', side_effect=fail_finished):
            with self.assertRaises(apps.RegistrationError) as result:
                self.register()
        self.assertTrue(result.exception.unknown)
        self.assertGreater(self.fixture.runtime.applications, 0)
        self.assertEqual(json.loads((self.home() / 'registration.json').read_text())['status'], 'running')


if __name__ == '__main__':
    unittest.main()
