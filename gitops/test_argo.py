import base64
import copy
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import argo
import handoff
from execution import GATE_ORDER


class ArgoTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(); self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        published = self.root / 'published'; published.mkdir()
        spec = {'apiVersion': 'jasmin/v0', 'app': 'demo', 'services': [
            {'name': 'web', 'build': {'dockerfile': 'Dockerfile'}, 'port': 8080, 'route': '/health', 'health': '/health'}]}
        verdict = {'release_eligible': True, 'ok': True, 'status': 'PASS', 'source_sha256': 'a' * 64,
                   'layers': [{'layer': layer, 'ok': True} for layer in GATE_ORDER],
                   'images': {'web': 'local/web:test'}, 'image_ids': {'web': 'sha256:' + 'b' * 64}}
        values = {'jasmin.yaml': spec, 'verdict.json': verdict,
                  'images.json': {'web': 'ghcr.io/example/web@sha256:' + 'c' * 64}}
        data = {k: json.dumps(v).encode() for k, v in values.items()}
        manifest = {'version': 1, 'trust': handoff.TRUST, 'source_sha256': verdict['source_sha256'],
                    'images': {'web': {'id': verdict['image_ids']['web'], 'local_ref': 'local/web:test'}},
                    'files': {k: hashlib.sha256(v).hexdigest() for k, v in data.items() if k != 'images.json'}}
        data['manifest.json'] = json.dumps(manifest).encode()
        receipt = {'version': 2, 'status': 'published', 'run_id': 1, 'producer_attempt': 1, 'bundle_artifact_id': 2,
                   'source_commit': 'd' * 40, 'target_id': 'k3s-aws', 'tenant': 'team', 'app': 'demo',
                   'files': {k: hashlib.sha256(v).hexdigest() for k, v in data.items()}}
        receipt['registry'] = {'visibility': 'public', 'verification': 'anonymous_manifest_read',
                               'images_sha256': receipt['files']['images.json'], 'image_pull_secret': None}
        data['handoff.json'] = json.dumps(receipt).encode()
        for name, raw in data.items():
            (published / name).write_bytes(raw)
        target = {'id': 'k3s-aws', 'namespace': 'tenant-demo', 'argocd_namespace': 'argocd', 'project': 'railshot',
                  'architecture': 'amd64', 'repo_url': 'https://github.com/example/config.git',
                  'cluster_server': 'https://192.0.2.1:6443', 'path': 'targets/k3s-aws/demo',
                  'revision': 'e' * 40, 'node_port': 30080, 'ingress_cidrs': ['10.20.0.0/24'],
                  'resources': {'requests': {'cpu': '100m', 'memory': '128Mi'}, 'limits': {'cpu': '500m', 'memory': '256Mi'}}}
        rendered = handoff.render(published, target)
        self.review = {'application': rendered.pop('application'), 'workload': rendered.pop('workload'), 'receipt': rendered}
        self.directory = self.root / 'review'; self.save_review(self.review, self.directory)

    def save_review(self, review, directory):
        review['receipt']['documents'] = {name: handoff.document_hash(review[name]) for name in ('application', 'workload')}
        directory.mkdir(exist_ok=True)
        for name, value in review.items():
            (directory / (name + '.json')).write_text(json.dumps(value))

    def healthy(self, review=None):
        review = review or self.review
        app = copy.deepcopy(review['application']); spec = app['spec']
        app['status'] = {'sync': {'status': 'Synced', 'revision': spec['source']['targetRevision'],
                                 'comparedTo': {'source': copy.deepcopy(spec['source']), 'destination': copy.deepcopy(spec['destination'])}},
                         'health': {'status': 'Healthy'}, 'operationState': {'phase': 'Succeeded',
                         'syncResult': {'revision': spec['source']['targetRevision']}},
                         'resources': [{'group': kind['group'], 'kind': kind['kind'], 'name': review['receipt']['app'],
                                        'namespace': spec['destination']['namespace'], 'status': 'Synced',
                                        **({'health': {'status': 'Healthy'}} if kind['kind'] == 'Deployment' else {})}
                                       for kind in argo.KINDS],
                         'summary': {'images': ['ghcr.io/example/web@sha256:' + 'c' * 64]}}
        return app

    def test_actual_rendered_review_hash_and_project_contract(self):
        loaded = argo.load_review(self.directory)
        self.assertEqual(loaded['receipt']['http']['route'], '/health')
        project = argo.projects([loaded])['items'][0]
        argo.validate_project(project, loaded['application'])
        self.assertEqual(project['spec']['clusterResourceWhitelist'], [])
        for changes in ({'sourceRepos': ['*']}, {'destinations': [{'server': '*', 'namespace': '*'}]},
                        {'namespaceResourceWhitelist': [{'group': '*', 'kind': '*'}]},
                        {'clusterResourceWhitelist': [{'group': '', 'kind': 'Namespace'}]}):
            bad = copy.deepcopy(project); bad['spec'].update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                argo.validate_project(bad, loaded['application'])
        workload = copy.deepcopy(loaded['workload']); workload['items'][0]['spec']['replicas'] = 20
        (self.directory / 'workload.json').write_text(json.dumps(workload))
        with self.assertRaisesRegex(ValueError, 'hashes differ'):
            argo.load_review(self.directory)

    def test_deployed_requires_matching_live_binding_revision_resources_and_health(self):
        live = self.healthy()
        result = argo.observe(self.review, live)
        self.assertTrue(result['deployed']); self.assertFalse(result['public_verified']); self.assertIsNone(result['url'])
        without_resource_health = copy.deepcopy(live)
        for row in without_resource_health['status']['resources']: row.pop('health', None)
        self.assertTrue(argo.observe(self.review, without_resource_health)['deployed'])
        for field in ('sync_revision', 'operation_revision', 'operation_phase', 'health', 'resources', 'images', 'compared_source', 'pending'):
            bad = copy.deepcopy(live)
            if field == 'sync_revision': bad['status']['sync']['revision'] = 'f' * 40
            elif field == 'operation_revision': bad['status']['operationState']['syncResult']['revision'] = 'f' * 40
            elif field == 'operation_phase': bad['status']['operationState']['phase'] = 'Failed'
            elif field == 'health': bad['status']['health']['status'] = 'Progressing'
            elif field == 'resources': bad['status']['resources'][0]['namespace'] = 'another-app'
            elif field == 'images': bad['status']['summary']['images'] = []
            elif field == 'compared_source': bad['status']['sync']['comparedTo']['source']['path'] = 'other/app'
            else: bad['operation'] = {'sync': {'revision': 'e' * 40}}
            with self.subTest(field=field):
                self.assertFalse(argo.observe(self.review, bad)['deployed'])
        for field in ('server', 'namespace'):
            bad = copy.deepcopy(live); bad['spec']['destination'][field] = 'other'
            with self.subTest(field=field), self.assertRaises(ValueError): argo.observe(self.review, bad)
        bad = copy.deepcopy(live); bad['spec']['ignoreDifferences'] = [{'kind': '*'}]
        with self.assertRaises(ValueError): argo.observe(self.review, bad)

    def test_sync_requests_exact_revision_and_resume_does_not_start_a_second_operation(self):
        calls = []
        def client(context, namespace, *args, document=None):
            calls.append((args, document))
            if len(calls) == 1: return None
            return self.healthy()
        with patch('argo.kubectl', side_effect=client):
            result = argo.deploy(self.review, 'control', sync=True, timeout=0)
        self.assertTrue(result['deployed'])
        operation = next(document for args, document in calls if args[0] == 'patch')['operation']
        self.assertEqual(operation['sync'], {'revision': 'e' * 40, 'prune': False, 'syncStrategy': {'apply': {}}})
        pending = self.healthy(); pending['operation'] = copy.deepcopy(operation)
        with patch('argo.kubectl', side_effect=[pending, self.healthy()]) as client:
            self.assertTrue(argo.deploy(self.review, 'control', sync=True, timeout=0)['deployed'])
            self.assertTrue(all(call.args[2] == 'get' for call in client.call_args_list))

    def test_pinned_git_tree_must_match_reviewed_workload_without_extra_resources(self):
        repository = self.root / 'config'; repository.mkdir()
        def git(*args):
            return subprocess.run(['git', '-C', str(repository), '-c', 'core.hooksPath=/dev/null', *args],
                                  check=True, capture_output=True, text=True).stdout.strip()
        git('init', '-q'); git('remote', 'add', 'origin', 'https://github.com/example/config.git')
        app_path = repository / self.review['application']['spec']['source']['path']; app_path.mkdir(parents=True)
        (app_path / 'workload.json').write_text(json.dumps(self.review['workload']))
        git('add', '.'); git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'fixture')
        self.review['application']['spec']['source']['targetRevision'] = git('rev-parse', 'HEAD')
        argo.verify_git(self.review, repository)
        (app_path / 'extra.json').write_text('{}'); git('add', '.')
        git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'extra')
        self.review['application']['spec']['source']['targetRevision'] = git('rev-parse', 'HEAD')
        with self.assertRaisesRegex(ValueError, 'only workload.json'): argo.verify_git(self.review, repository)

    def test_remote_cluster_registration_scopes_secret_and_never_exposes_credentials(self):
        credential = {'bearerToken': 'synthetic-sensitive-token', 'tlsClientConfig': {
            'insecure': False, 'caData': base64.b64encode(b'-----BEGIN CERTIFICATE-----\nsynthetic\n').decode()}}
        calls = []; stored = {}
        def run(args, **kwargs):
            nonlocal stored
            calls.append(args)
            self.assertNotIn(credential['bearerToken'], json.dumps(args))
            self.assertFalse(kwargs.get('shell', False))
            if 'apply' in args:
                stored = json.loads(kwargs['input'])
            return subprocess.CompletedProcess(args, 0, json.dumps(stored) if stored else '', '')
        with patch('argo.subprocess.run', side_effect=run):
            result = argo.register_cluster([self.review], 'control', credential)
        data = {k: base64.b64decode(v).decode() for k, v in stored['data'].items()}
        self.assertEqual(data['namespaces'], 'tenant-demo'); self.assertEqual(data['clusterResources'], 'false')
        self.assertEqual(data['project'], 'railshot'); self.assertNotIn(credential['bearerToken'], json.dumps(result))
        self.assertTrue(any('--server-side' in args for args in calls))
        with patch('argo.subprocess.run', return_value=subprocess.CompletedProcess([], 1, credential['bearerToken'], credential['bearerToken'])):
            with self.assertRaises(ValueError) as error:
                argo.register_cluster([self.review], 'control', credential)
            self.assertNotIn(credential['bearerToken'], str(error.exception))
        credential['tlsClientConfig']['insecure'] = True
        with patch('argo.kubectl') as client, self.assertRaises(ValueError):
            argo.register_cluster([self.review], 'control', credential)
        client.assert_not_called()

    def test_multiple_applications_keep_partial_results(self):
        second = copy.deepcopy(self.review)
        second['application']['metadata']['name'] = 'k3s-gcp-tenant-demo-demo'
        second['application']['metadata']['labels']['railshot.io/target'] = 'k3s-gcp'
        second['application']['spec']['destination']['server'] = 'https://192.0.2.2:6443'
        second['receipt']['target_id'] = 'k3s-gcp'
        second_path = self.root / 'gcp'; self.save_review(second, second_path)
        project = argo.projects([self.review, second])['items'][0]
        out = io.StringIO()
        with patch('sys.argv', ['argo.py', 'sync', str(self.directory), str(second_path), '--context', 'control', '--repo', str(self.root)]), \
                patch('argo.kubectl', return_value=project), patch('argo.verify_git'), \
                patch('argo.deploy', side_effect=[{'status': 'deployed', 'deployed': True}, ValueError('synthetic target unavailable')]), \
                patch('sys.stdout', out):
            self.assertEqual(argo.main(), 1)
        result = json.loads(out.getvalue())
        self.assertFalse(result['deployed']); self.assertTrue(result['applications'][0]['deployed'])
        self.assertEqual(result['applications'][1]['status'], 'blocked')


if __name__ == '__main__':
    unittest.main()
