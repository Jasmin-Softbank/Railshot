import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from handoff import render, FILES, TRUST
from execution import GATE_ORDER


class HandoffTest(unittest.TestCase):
    def test_digest_target_and_unsupported_workload_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec = {'apiVersion': 'jasmin/v0', 'app': 'demo', 'services': [
                {'name': 'web', 'build': {'dockerfile': 'Dockerfile'}, 'port': 8080, 'route': '/', 'health': '/health'}]}
            verdict = {'release_eligible': True, 'ok': True, 'status': 'PASS', 'source_sha256': 'a' * 64,
                       'layers': [{'layer': x, 'ok': True} for x in GATE_ORDER],
                       'images': {'web': 'local/web:test'}, 'image_ids': {'web': 'sha256:' + 'b' * 64}}

            def prepare():
                data = {'jasmin.yaml': json.dumps(spec), 'verdict.json': json.dumps(verdict),
                        'images.json': json.dumps({'web': 'ghcr.io/example/web@sha256:' + 'c' * 64})}
                manifest = {'version': 1, 'trust': TRUST, 'source_sha256': verdict['source_sha256'],
                            'images': {'web': {'id': verdict['image_ids']['web'], 'local_ref': 'local/web:test'}},
                            'files': {k: hashlib.sha256(v.encode()).hexdigest() for k, v in data.items() if k != 'images.json'}}
                data['manifest.json'] = json.dumps(manifest)
                for name, content in data.items():
                    (root / name).write_text(content)
                receipt = {'version': 1, 'status': 'published', 'run_id': 1, 'producer_attempt': 1, 'bundle_artifact_id': 2,
                           'source_commit': 'd' * 40, 'target_id': 'aws-demo', 'tenant': 'team', 'app': 'demo',
                           'files': {k: hashlib.sha256((root / k).read_bytes()).hexdigest() for k in FILES}}
                (root / 'handoff.json').write_text(json.dumps(receipt))

            target = {'id': 'aws-demo', 'namespace': 'tenant-demo', 'argocd_namespace': 'argocd', 'project': 'railshot',
                      'architecture': 'amd64', 'repo_url': 'https://github.com/example/config.git',
                      'cluster_server': 'https://kubernetes.default.svc', 'path': 'targets/aws-demo/demo',
                      'revision': 'e' * 40, 'node_port': 30080, 'ingress_cidrs': ['10.20.0.0/24'],
                      'resources': {'requests': {'cpu': '100m', 'memory': '128Mi'}, 'limits': {'cpu': '500m', 'memory': '256Mi'}}}
            prepare()
            result = render(root, target)
            self.assertFalse(result['deployed'])
            self.assertEqual(result['bundle_artifact_id'], 2)
            self.assertEqual(result['producer_attempt'], 1)
            self.assertNotIn('syncPolicy', result['application']['spec'])
            self.assertEqual(result['workload']['items'][0]['spec']['template']['spec']['containers'][0]['image'],
                             'ghcr.io/example/web@sha256:' + 'c' * 64)
            for field, value in [('id', 'onprem-demo'), ('architecture', 'arm64'), ('revision', 'main'),
                                 ('project', 'default'), ('path', '../escape'), ('ingress_cidrs', ['0.0.0.0/0'])]:
                bad = copy.deepcopy(target)
                bad[field] = value
                with self.subTest(field=field), self.assertRaises(ValueError):
                    render(root, bad)
            (root / 'images.json').write_text('{}')
            with self.assertRaisesRegex(ValueError, 'hash mismatch'):
                render(root, target)
            spec['resources'] = {'postgres': {'size': 'small'}}
            prepare()
            with self.assertRaisesRegex(ValueError, 'stateless'):
                render(root, target)


if __name__ == '__main__':
    unittest.main()
