import base64
import copy
import json
from pathlib import Path
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import unittest
from unittest.mock import patch
from urllib.request import ProxyHandler, build_opener

import argo
import bridge
import test_argo as fixtures


class BridgeTest(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ArgoTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.repo, self.remote = self.root / 'repository', self.root / 'remote.git'
        subprocess.run(['git', 'init', '--bare', '-q', str(self.remote)], check=True)
        subprocess.run(['git', 'clone', '-q', str(self.remote), str(self.repo)], check=True, capture_output=True)
        self.git('checkout', '-qb', 'main')
        self.git('config', 'user.name', 'Fixture'); self.git('config', 'user.email', 'fixture@example.invalid')
        (self.repo / 'README.md').write_text('fixture\n')
        self.git('add', '.'); self.git('commit', '-qm', 'Initial'); self.git('push', '-q', 'origin', 'main')
        target = {'id': 'k3s-aws', 'namespace': 'tenant-demo', 'argocd_namespace': 'argocd', 'project': 'railshot',
                  'architecture': 'amd64', 'repo_url': 'https://github.com/example/config.git',
                  'cluster_server': 'https://192.0.2.1:6443', 'path': 'targets/k3s-aws/demo',
                  'node_port': 30080, 'ingress_cidrs': ['10.20.0.0/24'],
                  'resources': {'requests': {'cpu': '100m', 'memory': '128Mi'}, 'limits': {'cpu': '500m', 'memory': '256Mi'}}}
        self.config = {'version': 1, 'state_dir': str(self.root / 'state'), 'repository': str(self.repo),
                       '_sha256': 'f' * 64,
                       'branch': 'main', 'context': 'control', 'targets': {'k3s-aws': {
                           'target': target, 'app': 'demo', 'tenant': 'team',
                           'public_http': {'url': 'https://app.example/health', 'expected_json': {'status': 'ready'}}}}}
        directory = self.root / 'published'
        publication = json.loads((directory / 'handoff.json').read_bytes())
        publication.update(images=json.loads((directory / 'images.json').read_bytes()), artifact_id=3)
        self.request = {'action': 'apply', 'deployment_id': 'deployment-1', 'target_id': 'k3s-aws',
                        'config_sha256': 'f' * 64,
                        'publication': publication,
                        'files': {name: base64.b64encode((directory / name).read_bytes()).decode() for name in bridge.FILES}}
        self.native = argo.native
        self.calls = []

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.repo), '-c', 'core.hooksPath=/dev/null', *args],
                              check=True, capture_output=True, text=True).stdout.strip()

    def native_local_only(self, args, **kwargs):
        self.calls.append(args)
        if args[-3:] == ['remote', 'get-url', 'origin']:
            return 'https://github.com/example/config.git\n'
        self.assertEqual(args[0], 'git')
        # The fixture's origin is a temporary local bare repository; no network writes.
        return self.native(args, **kwargs)

    def kubectl(self, context, namespace, *args, document=None):
        self.calls.append(['kubectl', *args])
        if args[0:2] == ('get', 'appproject'):
            return argo.projects([self.fixture.review])['items'][0]
        review = argo.load_review(self.root / 'state/deployment-1/review')
        return self.fixture.healthy(review)

    def test_apply_pins_git_argo_and_public_receipt_then_replay_only_observes(self):
        verified = {'state': 'succeeded', 'verified_at': '2026-10-02T12:00:00+00:00', 'url': 'https://app.example/health'}
        with patch('argo.native', side_effect=self.native_local_only), patch('argo.kubectl', side_effect=self.kubectl), \
                patch('bridge.public_probe', return_value=verified):
            result = bridge.execute(self.config, self.request)
            self.assertTrue(result['cd']['deployed']); self.assertEqual(result['public_http'], verified)
            self.assertEqual(result['cd']['revision'], self.git('rev-parse', 'HEAD'))
            remote_revision = subprocess.check_output(['git', '--git-dir', str(self.remote), 'rev-parse', 'main'], text=True).strip()
            self.assertEqual(result['cd']['revision'], remote_revision)
            self.calls.clear()
            repeated = bridge.execute(self.config, self.request)
            self.assertTrue(repeated['cd']['deployed'])
            self.assertFalse(any('push' in args or 'commit' in args or 'patch' in args or 'apply' in args for args in self.calls))
            bad = copy.deepcopy(self.request); bad['publication']['artifact_id'] = 4
            with self.assertRaisesRegex(ValueError, 'binding conflict'):
                bridge.execute(self.config, bad)

    def test_uncertain_push_is_never_repeated_and_forged_input_never_dispatches(self):
        def failed(args, **kwargs):
            if 'push' in args:
                self.calls.append(args)
                raise RuntimeError('synthetic-sensitive-native-output')
            return self.native_local_only(args, **kwargs)
        with patch('argo.native', side_effect=failed), patch('argo.kubectl', side_effect=self.kubectl):
            result = bridge.execute(self.config, self.request)
            self.assertTrue(result['error']['outcome_unknown'])
            self.assertNotIn('synthetic', json.dumps(result))
        with patch('argo.native', side_effect=self.native_local_only), patch('argo.kubectl', side_effect=RuntimeError('unavailable')):
            self.calls.clear()
            repeated = bridge.execute(self.config, self.request)
            self.assertEqual(repeated['cd']['state'], 'unknown')
            self.assertFalse(any('push' in args for args in self.calls))
        bad = copy.deepcopy(self.request); bad['publication']['target_id'] = 'unregistered'
        with patch('argo.native') as native, self.assertRaises(ValueError):
            bridge.execute(self.config, bad)
        native.assert_not_called()
        changed = {**self.config, '_sha256': 'e' * 64}
        with patch('argo.native') as native, self.assertRaisesRegex(ValueError, 'config changed'):
            bridge.execute(changed, self.request)
        native.assert_not_called()

    def test_foreign_git_workload_is_not_overwritten_or_pushed(self):
        directory = self.repo / 'targets/k3s-aws/demo'; directory.mkdir(parents=True)
        workload = copy.deepcopy(self.fixture.review['workload'])
        for item in workload['items']:
            item['metadata']['name'] = 'another-app'
        path = directory / 'workload.json'; path.write_text(json.dumps(workload))
        self.git('add', '.'); self.git('commit', '-qm', 'Foreign app'); self.git('push', '-q', 'origin', 'main')
        original = path.read_bytes()
        with patch('argo.native', side_effect=self.native_local_only), patch('argo.kubectl', side_effect=self.kubectl):
            result = bridge.execute(self.config, self.request)
        self.assertEqual(result['cd']['state'], 'blocked')
        self.assertEqual(path.read_bytes(), original)
        self.assertFalse(any('push' in args for args in self.calls))

    def test_real_local_http_requires_exact_body_and_never_follows_redirect(self):
        class Handler(BaseHTTPRequestHandler):
            body, redirect = b'{"status":"ready"}', False
            def do_GET(self):
                self.send_response(302 if self.redirect else 200)
                if self.redirect:
                    self.send_header('Location', '/other')
                self.end_headers(); self.wfile.write(self.body)
            def log_message(self, *_args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        local = f'http://127.0.0.1:{server.server_port}/health'
        opener = build_opener(ProxyHandler({}), bridge.NoRedirect())
        class LocalTLSBoundary:
            def open(self, request, **kwargs):
                self_request = type(request)(local, headers=dict(request.headers))
                return opener.open(self_request, **kwargs)
        # Production still requires HTTPS; only the transport is replaced with this local HTTP server.
        with patch('bridge.build_opener', return_value=LocalTLSBoundary()):
            config = self.config['targets']['k3s-aws']['public_http']
            self.assertEqual(bridge.public_probe(config, '/health')['state'], 'succeeded')
            Handler.body = b'{"status":"wrong"}'
            self.assertEqual(bridge.public_probe(config, '/health')['state'], 'unverified')
            Handler.body = b'{"status":"ready"}'; Handler.redirect = True
            self.assertEqual(bridge.public_probe(config, '/health')['state'], 'unverified')
        with self.assertRaises(ValueError):
            bridge.public_probe({'url': local, 'expected_json': {}}, '/health')

    def test_edge_allocation_is_bound_and_only_initial_apply_can_mutate_routes(self):
        registered = self.config['targets']['k3s-aws']
        registered['edge'] = {'config_path': '/private/edge.json', 'allocation_path': '/private/allocation.json',
                              'config_sha256': 'a' * 64}
        verified = {'state': 'succeeded', 'verified_at': '2026-10-02T00:00:00Z',
                    'url': 'https://new-app.railshot.io/health', 'site_url': 'https://new-app.railshot.io/'}
        with patch('argo.native', side_effect=self.native_local_only), patch('argo.kubectl', side_effect=self.kubectl), \
                patch('edge.validate_binding') as bind, patch('edge.ensure') as ensure, \
                patch('edge.observe', return_value=verified) as observe, patch('bridge.public_probe') as legacy:
            result = bridge.execute(self.config, self.request)
            self.assertEqual(result['public_http'], verified)
            ensure.assert_called_once_with(registered['edge'])
            state = json.loads((self.root / 'state/deployment-1/state.json').read_bytes())
            self.assertEqual(state['edge_request']['deployment_id'], self.request['deployment_id'])
            ensure.reset_mock()
            bridge.execute(self.config, {**self.request, 'action': 'observe'})
            ensure.assert_not_called(); legacy.assert_not_called()
            self.assertEqual(observe.call_args.args[1]['publication']['source_commit'], self.request['publication']['source_commit'])
            self.assertEqual(bind.call_count, 2)
            observe.side_effect = ValueError('route binding differs')
            self.assertIsNone(bridge.execute(self.config, {**self.request, 'action': 'observe'})['public_http']['url'])
        with patch('edge.validate_binding', side_effect=ValueError('mismatch')), patch('argo.native') as native:
            with self.assertRaises(ValueError):
                bridge.execute(self.config, self.request)
            native.assert_not_called()

    def test_node_adapter_calls_apply_once_then_read_only_observe(self):
        executable = self.root / 'synthetic-python'
        executable.write_text('#!' + sys.executable + '\n' + '''import json,sys
from pathlib import Path
request=json.load(sys.stdin)
path=Path(sys.argv[-1])
config=json.loads(path.read_text())
config['calls'].append(request['action'])
path.write_text(json.dumps(config))
done=request['action']=='observe'
print(json.dumps({'cd':{'state':'deployed' if done else 'progressing','revision':'a'*40,'deployed':done},
 'public_http':{'state':'succeeded' if done else 'not_run','verified_at':'2026-10-02T00:00:00Z' if done else None,
 'url':'https://app.example/health' if done else None}}))
''')
        executable.chmod(0o700)
        calls = self.root / 'adapter-calls.json'
        calls.write_text(json.dumps({**self.config, 'calls': []})); calls.chmod(0o600)
        adapter = Path(__file__).resolve().parents[1] / 'apps/api/src/cd.js'
        program = '''
import assert from 'node:assert/strict';
const {createCdAdapter}=await import(process.argv[1]);
const adapter=createCdAdapter({configPath:process.argv[2],python:process.argv[3],timeoutMs:10000,
 loadPublished:async()=>[{path:'handoff.json',content:Buffer.from('{}')}]});
assert.equal(adapter.targets['k3s-aws'].applicationName,'demo');
assert.equal(adapter.targets['k3s-aws'].deploymentScope,'registered_application');
const request={deploymentId:'deployment-1',app:'demo',targetId:'k3s-aws',sourceCommit:'a'.repeat(40),
 publication:{app:'demo',tenant:'team',target_id:'k3s-aws',source_commit:'a'.repeat(40)}};
const result=await adapter(request);
assert.equal(result.cd.deployed,true);assert.equal(result.public_http.state,'succeeded');
await assert.rejects(adapter({...request,app:'wrong'}),/binding differs/);
'''
        subprocess.run(['node', '--input-type=module', '-e', program, adapter.as_uri(), str(calls), str(executable)],
                       check=True, capture_output=True, text=True, timeout=15)
        self.assertEqual(json.loads(calls.read_text())['calls'], ['apply', 'observe'])


if __name__ == '__main__':
    unittest.main()
