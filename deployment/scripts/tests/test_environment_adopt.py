"""No-cloud checks for explicit, UID-bound metadata-only legacy adoption."""
import base64
from contextlib import contextmanager
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'deployment/scripts'))
spec = importlib.util.spec_from_file_location('environment_adopt', ROOT / 'deployment/scripts/environment-adopt.py')
adopt = importlib.util.module_from_spec(spec); spec.loader.exec_module(adopt)


class Kube:
    def __init__(self):
        self.objects = {}; self.patches = []; self.fail_after = None

    def add(self, document):
        value = copy.deepcopy(document); meta = value['metadata']
        meta.update(uid=f'00000000-0000-0000-0000-{len(self.objects) + 1:012d}', resourceVersion='1', generation=1)
        self.objects[meta.get('namespace', 'default'), value['kind'].lower(), meta['name']] = value
        return value

    def __call__(self, namespace, *args, document=None):
        if args[:2] == ('config', 'view'):
            return {'clusters': [{'cluster': {'certificate-authority-data': base64.b64encode(b'fixture-ca').decode()}}]}
        key = namespace, args[1].lower(), args[2]
        obj = self.objects.get(key)
        if obj is None:
            if '--ignore-not-found' in args:
                return None
            raise ValueError('fixture resource is absent')
        if args[0] == 'get':
            return copy.deepcopy(obj)
        if args[0] != 'patch':
            raise AssertionError('adoption may only get or patch: ' + str(args))
        if self.fail_after is not None and len(self.patches) >= self.fail_after:
            raise OSError('simulated uncertain patch')
        for operation in document:
            field = operation['path'].split('/')[-1]
            if operation['op'] == 'test':
                if obj['metadata'].get(field) != operation['value']:
                    raise ValueError('CAS conflict')
            else:
                assert operation['op'] == 'add' and operation['path'] == '/metadata/labels'
                obj['metadata']['labels'] = copy.deepcopy(operation['value'])
        obj['metadata']['resourceVersion'] = str(int(obj['metadata']['resourceVersion']) + 1)
        self.patches.append(copy.deepcopy(document))
        return copy.deepcopy(obj)


class AdoptionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve(); self.root.chmod(0o700)
        self.home = self.root / 'legacy'; self.shared = self.root / 'claims'; self.target_id = 'fixture-gcp'
        self.refs = {'registry': str(self.root / 'registry.json'), 'target_id': self.target_id,
            'config': str(self.root / 'config.json'), 'registration_dir': str(self.home),
            'policy_file': str(self.root / 'policy.json'), 'binding': None}
        self.policy = json.loads((ROOT / 'deployment/airgap/versions.json').read_text())
        self.request = {'target': {'provider': 'gcp'}}
        target = {'id': self.target_id, 'namespace': 'tenant', 'project': 'tenant-project', 'cluster_server': 'https://34.47.68.21:6443',
            'repo_url': 'https://github.com/owner/config', 'path': 'gitops/applications/fixture/' + self.target_id,
            'node_port': 30085, 'image_pull_secret': {'name': 'ghcr-pull', 'namespace': 'tenant'}}
        self.registered = {'app': 'fixture', 'target': target, 'public_http': {'url': 'https://app.example/health', 'expected_json': {'ready': True}}}
        self.cd = {'context': 'control', 'targets': {self.target_id: self.registered}}
        self.settings = {'state_dir': str(self.shared), 'observability_config_file': str(self.root / 'observer.json')}
        self.identity = {'descriptor': {'target_id': self.target_id, 'provider_kind': 'gcp', 'resource_id': 'instance-123',
            'addresses': {'private': '10.66.0.2', 'public': '34.47.68.21', 'metrics': '34.47.68.21'},
            'management_endpoint': target['cluster_server']}, 'environment_id': self.home.name}
        self.pull = {'auths': {'ghcr.io': {'auth': base64.b64encode(b'fixture:secret').decode()}}}
        self.observer = {'owner': 'fixture-observer', 'observer_ip': '10.66.0.3', 'prometheus_url': 'http://10.66.0.3:9090',
            'node_metrics_port': 31091, 'cluster_metrics_port': 31092, 'state_dir': str(self.root / 'observer-state')}
        self.loaded = ((self.request, self.cd, self.settings, self.pull, None, self.identity), self.observer, self.policy)
        self.node = {'runtime': self.policy['runtime'], 'cilium_images': self.policy['cilium_images'],
            'node_uid': 'node-uid', 'node_ip': '10.66.0.2', 'architecture': 'amd64', 'ready': True, 'helm_revision': 1}
        self.runtime = Kube(); self.control = Kube()
        for document in adopt.env.runtime_documents(target, 'unused', self.pull, None):
            document['metadata']['labels'] = {'legacy': 'preserved'}
            self.runtime.add(document)
        self.runtime.add({'kind': 'Deployment', 'metadata': {'namespace': 'tenant', 'name': 'fixture'},
            'spec': {'replicas': 1, 'template': {'metadata': {'labels': {'railshot.io/target': self.target_id}},
                'spec': {'containers': [{'image': 'fixture@sha256:' + 'a' * 64}]}}},
            'status': {'observedGeneration': 1, 'updatedReplicas': 1, 'availableReplicas': 1}})
        self.runtime.add({'kind': 'Service', 'metadata': {'namespace': 'tenant', 'name': 'fixture'},
            'spec': {'selector': {'railshot.io/target': self.target_id}, 'ports': [{'nodePort': 30085}]}})
        for document in adopt.observer.cluster({'node_ip': '10.66.0.2', 'observer_source_cidr': '10.66.0.3/32',
                'node_metrics_port': 31091, 'cluster_metrics_port': 31092})['items']:
            if document['kind'] == 'Deployment':
                document['status'] = {'observedGeneration': 1, 'availableReplicas': 1}
            if document['kind'] == 'DaemonSet':
                document['status'] = {'observedGeneration': 1, 'desiredNumberScheduled': 1, 'numberReady': 1}
            self.runtime.add(document)
        sa = self.runtime.objects['tenant', 'serviceaccount', adopt.env.SA]
        self.renewal = {'target_id': self.target_id, 'secret': 'railshot-' + self.target_id,
            'server': target['cluster_server'], 'project': target['project'], 'namespaces': ['tenant'],
            'service_account': {'name': adopt.env.SA, 'namespace': 'tenant', 'uid': sa['metadata']['uid']},
            'ca_sha256': hashlib.sha256(b'fixture-ca').hexdigest(), 'audiences': ['api'], 'tls_server_name': '10.66.0.2'}
        claims = {'sub': 'system:serviceaccount:tenant:' + adopt.env.SA, 'aud': ['api'], 'exp': int(time.time()) + 21600,
            'iat': int(time.time()), 'kubernetes.io': {'namespace': 'tenant', 'serviceaccount': {'name': adopt.env.SA, 'uid': sa['metadata']['uid']}}}
        token = 'e30.' + base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip('=') + '.signature'
        credential = {'bearerToken': token, 'tlsClientConfig': {'insecure': False,
            'caData': base64.b64encode(b'fixture-ca').decode(), 'serverName': '10.66.0.2'}}
        self.control.add({'kind': 'ConfigMap', 'metadata': {'namespace': 'argocd', 'name': 'railshot-credentials'},
            'data': {'policy.json': json.dumps({'version': 1, 'targets': [self.renewal]})}})
        self.control.add({'kind': 'Secret', 'metadata': {'namespace': 'argocd', 'name': self.renewal['secret']},
            'data': {k: base64.b64encode(v.encode()).decode() for k, v in {'name': self.target_id, 'server': target['cluster_server'],
            'project': target['project'], 'clusterResources': 'false', 'namespaces': 'tenant', 'config': json.dumps(credential)}.items()}})
        self.control.add({'kind': 'CronJob', 'metadata': {'namespace': 'argocd', 'name': 'railshot-credentials'},
            'spec': {'jobTemplate': {'spec': {'template': {'spec': {'serviceAccountName': 'railshot-credentials',
            'containers': [{'command': ['python3', '/app/gitops/credentials.py', 'renew']}]}}}}}})
        self.control.add({'kind': 'Role', 'metadata': {'namespace': 'argocd', 'name': 'railshot-credentials'},
            'rules': [{'apiGroups': [''], 'resources': ['secrets'], 'verbs': ['get', 'patch'], 'resourceNames': [self.renewal['secret']]}]})
        self.control.add({'kind': 'AppProject', 'metadata': {'namespace': 'argocd', 'name': target['project']},
            'spec': {'destinations': [{'server': target['cluster_server'], 'namespace': 'tenant'}], 'sourceRepos': [target['repo_url']], 'clusterResourceWhitelist': []}})
        self.control.add({'kind': 'Application', 'metadata': {'namespace': 'argocd', 'name': adopt.env.application_name(self.target_id, 'tenant', 'fixture')},
            'spec': {'project': target['project'], 'source': {'repoURL': target['repo_url'], 'path': target['path']},
                'destination': {'server': target['cluster_server'], 'namespace': 'tenant'}},
            'status': {'sync': {'status': 'Synced'}, 'health': {'status': 'Healthy'}}})
        @contextmanager
        def connection(_):
            yield []
        @contextmanager
        def kubectl(_):
            yield self.runtime
        self.enterContext(patch.object(adopt, 'load', return_value=self.loaded))
        self.enterContext(patch.object(adopt.update, 'connection', connection))
        self.node_call = self.enterContext(patch.object(adopt.update, 'node_call', side_effect=lambda prefix, payload:
            copy.deepcopy(self.node) if payload == {'action': 'inspect'} else self.fail('adoption called mutation')))
        self.enterContext(patch.object(adopt.env, 'runtime_kubectl', kubectl))
        self.enterContext(patch.object(adopt.env.argo, 'kubectl', lambda context, *a, **kw: self.control(*a, **kw)))
        self.customer = self.enterContext(patch.object(adopt.env.credentials, 'customer', return_value={'kind': 'PodList', 'items': []}))
        self.enterContext(patch.object(adopt.env.bridge, 'public_probe', return_value={'state': 'succeeded'}))
        self.plan_file = self.root / 'adoption.json'

    def make_plan(self):
        return adopt.plan(self.refs, self.plan_file)['plan_sha256']

    def test_plan_and_apply_only_add_exact_labels_preserve_specs_and_credentials(self):
        originals = [copy.deepcopy(k.objects) for k in (self.runtime, self.control)]
        digest = self.make_plan()
        self.assertFalse(self.home.exists()); self.assertFalse(self.shared.exists())
        self.assertFalse(self.runtime.patches + self.control.patches)
        private = adopt.env.read_private(self.plan_file)
        self.assertTrue(all('original_labels' in row for row in private['snapshot']['objects']))
        result = adopt.apply(self.plan_file, digest)
        self.assertEqual(result['status'], 'verified')
        record = adopt.env.read_private(self.home / 'registration.json')
        self.assertEqual(record['status'], 'succeeded')
        self.assertEqual(record['input_sha256'], adopt.env.argo.document_hash(self.identity))
        self.assertEqual(record['cd_sha256'], hashlib.sha256((self.home / 'cd.json').read_bytes()).hexdigest())
        for kube, original in zip((self.runtime, self.control), originals):
            for key, value in kube.objects.items():
                self.assertEqual(adopt.content_hash(value), adopt.content_hash(original[key]))
            for patch_document in kube.patches:
                self.assertEqual([item['path'] for item in patch_document[:2]], ['/metadata/uid', '/metadata/resourceVersion'])
                self.assertTrue(all(item['op'] == 'test' or item['path'] == '/metadata/labels' for item in patch_document))
        self.assertTrue(all(len(call.args) == 4 for call in self.customer.call_args_list))  # No TokenRequest or rotation POST.
        with self.assertRaisesRegex(ValueError, 'operator recovery'):
            adopt.apply(self.plan_file, digest)

    def test_recreated_uid_rv_spec_and_plan_tampering_fail_before_any_patch(self):
        digest = self.make_plan(); obj = self.runtime.objects['default', 'namespace', 'tenant']; original = copy.deepcopy(obj)
        for field, value in [('uid', 'recreated'), ('resourceVersion', '2'), ('annotations', {'changed': 'yes'})]:
            with self.subTest(field=field):
                obj['metadata'][field] = value
                with self.assertRaisesRegex(ValueError, 'changed'):
                    adopt.apply(self.plan_file, digest)
                obj.clear(); obj.update(copy.deepcopy(original))
                self.assertFalse(self.runtime.patches + self.control.patches)
                self.assertFalse((self.home / 'registration.json').exists())
        with self.assertRaisesRegex(ValueError, 'digest'):
            adopt.apply(self.plan_file, '0' * 64)
        with patch.object(adopt.time, 'time', return_value=time.time() + 901):
            with self.assertRaisesRegex(ValueError, 'expired'):
                adopt.apply(self.plan_file, digest)

    def test_foreign_manager_owner_and_management_uid_cannot_be_adopted(self):
        for key in [('default', 'namespace', 'tenant'), ('default', 'namespace', 'railshot-observability')]:
            obj = self.runtime.objects[key]; original = copy.deepcopy(obj)
            for patch_meta in ({'labels': {'app.kubernetes.io/managed-by': 'another-manager'}}, {'ownerReferences': [{'uid': 'another'}]}):
                with self.subTest(key=key, patch_meta=patch_meta):
                    obj['metadata'].update(patch_meta)
                    with self.assertRaises(ValueError):
                        self.make_plan()
                    self.assertFalse(self.plan_file.exists())
                    obj.clear(); obj.update(copy.deepcopy(original))
        self.runtime.objects['tenant', 'serviceaccount', adopt.env.SA]['metadata']['uid'] = '00000000-0000-0000-0000-999999999999'
        with self.assertRaisesRegex(ValueError, 'ServiceAccount UID'):
            self.make_plan()
        self.assertFalse(self.runtime.patches + self.control.patches)

    def test_partial_unknown_patch_retains_rollback_plan_and_never_replays(self):
        digest = self.make_plan(); self.runtime.fail_after = 1
        result = adopt.apply(self.plan_file, digest)
        self.assertEqual(result['status'], 'recovery_required')
        self.assertEqual(adopt.env.read_private(self.home / 'registration.json')['status'], 'unknown')
        receipt = adopt.env.read_private(self.home / 'adoption-receipt.json')
        self.assertIn('pending', receipt); self.assertTrue(receipt['patched'])
        self.assertTrue(self.plan_file.exists()); self.assertFalse((self.home / 'cd.json').exists())
        count = len(self.runtime.patches) + len(self.control.patches)
        with self.assertRaisesRegex(ValueError, 'operator recovery'):
            adopt.apply(self.plan_file, digest)
        self.assertEqual(count, len(self.runtime.patches) + len(self.control.patches))

    def test_absent_observer_is_pinned_and_left_absent_until_common_release(self):
        for key in list(self.runtime.objects):
            if key[0] == 'railshot-observability' or key[2] in ('railshot-observability', 'railshot-observer'):
                del self.runtime.objects[key]
        initial_keys = set(self.runtime.objects)
        digest = self.make_plan()
        plan = adopt.env.read_private(self.plan_file)
        self.assertTrue(plan['health']['observability']['missing'])
        self.assertEqual(plan['health']['observability']['collection_state'], 'pending')
        self.assertTrue(any(row.get('absent') for row in plan['snapshot']['objects']))
        self.assertEqual(adopt.apply(self.plan_file, digest)['status'], 'verified')
        self.assertEqual(set(self.runtime.objects), initial_keys)
        registration = adopt.env.read_private(self.home / 'registration.json')
        self.assertIs(registration['observability']['registered'], False)
        self.assertTrue(registration['observability']['reconciliation_required'])


if __name__ == '__main__':
    unittest.main()
