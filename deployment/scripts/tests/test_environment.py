"""No-cloud regression for registration ownership, resume, native RBAC and CI consumption."""
import base64
from contextlib import contextmanager
import copy
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'deployment/scripts'))
import environment as env


class Kube:
    def __init__(self):
        self.objects = {}
        self.applications = 0

    def __call__(self, namespace, *args, document=None):
        if args[:3] == ('get', 'services', '-A'):
            return {'items': [v for (_, kind, _), v in self.objects.items() if kind == 'service']}
        if args[:2] == ('config', 'view'):
            return {'clusters': [{'cluster': {'certificate-authority-data': base64.b64encode(b'-----BEGIN CERTIFICATE-----\nfake').decode()}}]}
        if args[:2] == ('create', '--raw'):
            claims = {'sub': 'system:serviceaccount:' + namespace + ':' + env.SA, 'aud': ['https://kubernetes.default.svc'],
                'exp': int(time.time()) + 21600, 'iat': int(time.time()),
                'kubernetes.io': {'namespace': namespace, 'serviceaccount': {'name': env.SA, 'uid': '12345678-1234-1234-1234-123456789012'}}}
            token = 'e30.' + base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip('=') + '.signature'
            return {'status': {'token': token, 'expirationTimestamp': '2026-10-03T00:00:00Z'}}
        if args[0] in ('apply', 'replace'):
            data = copy.deepcopy(document); data['metadata'].setdefault('uid', '12345678-1234-1234-1234-123456789012')
            self.objects[namespace, data['kind'].lower(), data['metadata']['name']] = data
            self.applications += 1
            return data
        if args[0] == 'get':
            key = (namespace, args[1].lower(), args[2])
            value = self.objects.get(key)
            if value is None and '--ignore-not-found' not in args:
                raise ValueError('missing fixture resource ' + str(key))
            return copy.deepcopy(value)
        raise AssertionError(args)


class RegistrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.home = self.root / 'environment-new'
        self.shared = self.root / 'registrations'
        self.target = 'stack-aws-1002'
        self.descriptor = json.loads((ROOT / 'examples/ansible/aws-node-descriptor.json').read_text())
        self.descriptor['target_id'] = self.target
        self.write('descriptor.json', self.descriptor)
        self.write('key', 'synthetic-key', raw=True); self.write('hosts', '10.77.0.10 ssh-ed25519 synthetic', raw=True)
        self.registry = {'version': 1, 'targets': {self.target: {'descriptor_file': str(self.root / 'descriptor.json'),
            'purpose': 'runtime', 'ssh': {'user': 'ubuntu', 'identity_file': str(self.root / 'key'), 'known_hosts_file': str(self.root / 'hosts')}}}}
        self.config = {'version': 1, 'registration': {'state_dir': str(self.shared), 'source_repository': 'owner/source',
            'pull_secret_file': str(self.root / 'pull.json')}, 'cd': {'version': 1, 'state_dir': str(self.root / 'cd-state'),
            'repository': str(self.root / 'config-repo'), 'context': 'control', 'branch': 'deployment/apps',
            'targets': {self.target: {'app': 'new-app', 'tenant': 'demo', 'target': {'id': self.target,
                'namespace': 'app-new', 'argocd_namespace': 'argocd', 'project': 'railshot-new', 'architecture': 'amd64',
                'path': 'gitops/applications/new-app/' + self.target, 'repo_url': 'https://github.com/owner/config',
                'node_port': 30085, 'image_pull_secret': {'namespace': 'app-new', 'name': 'ghcr-pull'}},
                'public_http': {'url': 'https://new.example.test/health', 'expected_json': {'ok': True}}}}}}
        self.write('pull.json', {'auths': {'ghcr.io': {'auth': base64.b64encode(b'reader:synthetic-secret').decode()}}})
        self.write('registry.json', self.registry); self.write('config.json', self.config)
        self.runtime, self.control = Kube(), Kube()
        self.control.objects['argocd', 'cronjob', 'railshot-credentials'] = {'spec': {'jobTemplate': {'spec': {'template': {'spec': {
            'serviceAccountName': 'railshot-credentials', 'containers': [{'command': ['python3', '/app/gitops/credentials.py', 'renew']}]}}}}}}
        old = {'secret': 'railshot-old-target', 'target_id': 'old-target', 'server': 'https://10.0.0.2:6443', 'project': 'old-project',
            'namespaces': ['old-app'], 'service_account': {'name': env.SA, 'namespace': 'old-app', 'uid': '12345678-1234-1234-1234-123456789012'},
            'ca_sha256': 'a' * 64, 'audiences': ['https://kubernetes.default.svc']}
        self.control.objects['argocd', 'configmap', 'railshot-credentials'] = {'apiVersion': 'v1', 'kind': 'ConfigMap',
            'metadata': {'name': 'railshot-credentials'}, 'data': {'policy.json': json.dumps({'version': 1, 'targets': [old]}), 'kubeconfig': 'preserve-me'}}
        self.control.objects['argocd', 'role', 'railshot-credentials'] = {'apiVersion': 'rbac.authorization.k8s.io/v1', 'kind': 'Role',
            'metadata': {'name': 'railshot-credentials'}, 'rules': [{'apiGroups': [''], 'resources': ['secrets'],
            'resourceNames': ['railshot-old-target'], 'verbs': ['get', 'patch']}]}
        self.variable = None
        @contextmanager
        def runtime(_request):
            yield self.runtime
        self.enterContext(patch.object(env, 'runtime_kubectl', runtime))
        self.enterContext(patch.object(env.argo, 'kubectl', lambda context, *a, **kw: self.control(*a, **kw)))
        self.enterContext(patch.object(env.credentials, 'customer', side_effect=lambda server, ca, token, path, doc:
            {'status': {'allowed': doc['spec']['resourceAttributes']['namespace'] == 'app-new'
                and doc['spec']['resourceAttributes']['resource'] == 'deployments'}}))
        self.enterContext(patch.object(env, 'github_variable', self.github))

    def write(self, name, value, raw=False):
        p = self.root / name
        p.write_text(value if raw else json.dumps(value)); p.chmod(0o600)
        return p

    def github(self, repo, name, document=None, **_kw):
        if document:
            self.variable = copy.deepcopy(document)
        return copy.deepcopy(self.variable)

    def run_registration(self):
        return env.register(self.root / 'registry.json', self.target, self.root / 'config.json', self.home)

    def test_registration_is_consumed_replay_preserves_existing_and_conflict_blocks(self):
        result = self.run_registration()
        self.assertEqual(result['status'], 'succeeded', result)
        self.assertTrue(result['deployment_supported'])
        self.assertEqual(result['steps'], ['edge', 'namespace', 'argo', 'credentials', 'ci'])
        config = env.bridge.read_config(self.home / 'cd.json')
        self.assertEqual(config['targets'][self.target]['target']['cluster_server'], 'https://10.77.0.10:6443')
        self.assertEqual((self.home / 'cd.json').stat().st_mode & 0o777, 0o600)
        self.assertNotIn('synthetic-secret', json.dumps(result))
        self.assertEqual(self.control.objects['argocd', 'configmap', 'railshot-credentials']['data']['kubeconfig'], 'preserve-me')
        policy = json.loads(self.control.objects['argocd', 'configmap', 'railshot-credentials']['data']['policy.json'])
        self.assertEqual([p['target_id'] for p in policy['targets']], ['old-target', self.target])
        selected = env.validate_target({'CONFIGURED_BINDINGS': self.variable['value'], 'TARGET_ID': self.target, 'APP': 'new-app', 'TENANT': 'demo'})
        self.assertEqual(selected['image_pull_secret']['namespace'], 'app-new')
        count = (self.runtime.applications, self.control.applications)
        self.assertEqual(self.run_registration(), result)
        self.assertEqual(count, (self.runtime.applications, self.control.applications))
        self.config['cd']['targets'][self.target]['app'] = 'other-app'
        self.config['cd']['targets'][self.target]['target']['path'] = 'gitops/applications/other-app/' + self.target
        self.write('config.json', self.config)
        with self.assertRaisesRegex(Exception, 'TARGET_REGISTRATION_CONFLICT'):
            self.run_registration()

    def test_partial_failure_persists_and_same_identity_resumes_without_extra_objects(self):
        with patch.object(env, 'bind_ci', side_effect=RuntimeError('synthetic-secret')):
            failed = self.run_registration()
        self.assertEqual(failed['status'], 'unknown')
        self.assertEqual(failed['stage'], 'ci')
        self.assertFalse((self.home / 'cd.json').exists())
        self.assertNotIn('synthetic-secret', (self.home / 'registration.json').read_text())
        identities = set(self.runtime.objects) | set(self.control.objects)
        result = self.run_registration()
        self.assertEqual(result['status'], 'succeeded', result)
        self.assertNotIn('error', result)
        self.assertEqual(identities, set(self.runtime.objects) | set(self.control.objects))

    def test_foreign_namespace_is_not_adopted(self):
        self.runtime.objects['default', 'namespace', 'app-new'] = {'metadata': {'name': 'app-new', 'labels': {'owner': 'someone-else'}}}
        result = self.run_registration()
        self.assertEqual(result['status'], 'unknown')
        self.assertEqual(self.runtime.applications, 0)
        self.assertIsNone(self.variable)

    def test_existing_nodeport_is_not_overwritten(self):
        self.runtime.objects['old-ns', 'service', 'old-app'] = {'metadata': {'namespace': 'old-ns', 'name': 'old-app'},
            'spec': {'ports': [{'nodePort': 30085}]}}
        result = self.run_registration()
        self.assertEqual(result['status'], 'unknown')
        self.assertEqual(self.runtime.applications, 0)

    def test_db_address_derived_only_from_private_binding(self):
        target = self.config['cd']['targets'][self.target]['target']
        target['database'] = {'runtime_secret': 'runtime-db', 'migration_secret': 'migration-db', 'ca_secret': 'database-ca'}
        ca = self.write('db-ca', '-----BEGIN CERTIFICATE-----\nfake', raw=True)
        binding = {'version': 1, 'host': '10.1.1.2', 'port': 5432, 'database': 'app', 'sslmode': 'verify-full', 'sslrootcert': str(ca),
            'runtime': {'username': 'app', 'password': 'synthetic-password'}, 'migration': {'username': 'migrate', 'password': 'synthetic-password2'}}
        self.write('binding.json', binding); self.write('config.json', self.config)
        _, cd, *_ = env.load(self.root / 'registry.json', self.target, self.root / 'config.json', self.root / 'binding.json')
        self.assertEqual(cd['targets'][self.target]['target']['database']['host'], binding['host'])
        target['database']['host'] = '10.0.0.99'; self.write('config.json', self.config)
        with self.assertRaisesRegex(Exception, 'database TLS binding differs'):
            env.load(self.root / 'registry.json', self.target, self.root / 'config.json', self.root / 'binding.json')

    def test_aws_group_is_attached_to_descriptor_instance(self):
        response = {'Reservations': [{'Instances': [{'InstanceId': 'i-0123456789abcdef0',
            'PrivateIpAddress': self.descriptor['addresses']['private'], 'SecurityGroups': [{'GroupId': 'sg-12345678'}]}]}]}
        with patch.object(env.argo, 'native', return_value=json.dumps(response)):
            self.assertEqual(env.aws_security_group(self.descriptor), 'sg-12345678')
            with self.assertRaisesRegex(Exception, 'attached runtime security group'):
                env.aws_security_group(self.descriptor, 'sg-99999999')

    def test_gcp_contract_and_db_secrets_remain_separate_and_private(self):
        descriptor = json.loads((ROOT / 'examples/ansible/gcp-node-descriptor.json').read_text()); descriptor['target_id'] = self.target
        self.write('descriptor.json', descriptor)
        request, cd, settings, pull, _, _ = env.load(self.root / 'registry.json', self.target, self.root / 'config.json')
        self.assertEqual(request['target']['provider'], 'gcp')
        target = cd['targets'][self.target]['target']
        target['database'] = {'host': '10.1.1.2', 'port': 5432, 'runtime_secret': 'runtime-db', 'migration_secret': 'migration-db', 'ca_secret': 'database-ca'}
        binding = {'host': '10.1.1.2', 'port': 5432, 'database': 'app', 'runtime': {'username': 'app', 'password': 'a:@/?'},
            'migration': {'username': 'migrate', 'password': 'other'}, 'ca': 'CA'}
        docs = env.runtime_documents(target, 'owner', pull, binding)
        secrets = {d['metadata']['name']: d for d in docs if d['kind'] == 'Secret'}
        url = base64.b64decode(secrets['runtime-db']['data']['DATABASE_URL']).decode()
        self.assertIn('a%3A%40%2F%3F', url)
        self.assertIn('sslmode=verify-full', url)
        self.assertEqual(set(secrets['migration-db']['data']), {'MIGRATION_DATABASE_URL'})
        rules = next(d['rules'] for d in docs if d['kind'] == 'Role')
        self.assertFalse(any('secrets' in r['resources'] or '*' in r['resources'] for r in rules))
        self.assertTrue(any('jobs' in r['resources'] for r in rules))


if __name__ == '__main__':
    unittest.main()
