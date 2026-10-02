import base64
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

from bootstrap import PREPARE, HERE
from register import registration_row, merge_rows, scrape_config, settings
import register as registration


class RegistrationTests(unittest.TestCase):
    def setUp(self):
        self.config = {'version': 1, 'owner': 'shared-observer', 'lifecycle': 'shared', 'expires_at': (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(), 'state_dir': '/private/observer',
            'prometheus_url': 'http://192.0.2.20:9090', 'observer_ip': '192.0.2.20',
            'observer_registry_file': '/private/targets.json', 'observer_target_id': 'observer-aws',
            'observer_directory': '/home/railshot-operator/observer', 'node_metrics_port': 30910, 'cluster_metrics_port': 30911}
        self.request = {'version': 1, 'target_id': 'new-aws', 'environment_id': 'env-1', 'app': 'demo-app',
            'namespace': 'tenant-demo', 'node_ip': '192.0.2.10', 'probe_url': 'https://app.example.com/health',
            'registry_file': '/private/targets.json', 'context': 'control'}
        self.descriptor = {'target_id': 'new-aws', 'resource_id': 'instance-1', 'addresses': {'private': '192.0.2.10'}}

    def test_identity_conflicts_and_duplicate_registration_preserve_shared_rows(self):
        settings(self.config)
        row, _ = registration_row(self.config, self.request, self.descriptor)
        rows = merge_rows([], row)
        self.assertEqual(merge_rows(rows, row), rows)
        for changed in ({'environment_id': 'other'}, {'resource_id': 'instance-2'}, {'node_instance': '192.0.2.99:30910'}):
            with self.assertRaises(ValueError):
                merge_rows(rows, {**row, **changed})
        with self.assertRaises(ValueError):
            registration_row(self.config, {**self.request, 'node_ip': '192.0.2.11'}, self.descriptor)
        with self.assertRaises(ValueError):
            registration_row(self.config, {**self.request, 'probe_url': 'http://a:b@internal/secret'}, self.descriptor)
        other = {**row, 'target_id': 'next-aws', 'node_instance': '192.0.2.11:30910', 'cluster_instance': '192.0.2.11:30911'}
        result = scrape_config(merge_rows(rows, other))
        self.assertEqual(result['scrape_configs'][0]['static_configs'][0]['targets'], ['192.0.2.10:30910', '192.0.2.11:30910'])
        self.assertEqual(len(rows), 1)

    def test_direct_observer_ssh_keeps_identity_and_default_transport(self):
        request = {'inventory': {'control_plane': [{'private_ipv4': self.config['observer_ip'],
            'ssh': {'transport_ref': 'ssm:ap-northeast-2:i-12345678'}}]}}
        references = []
        @contextmanager
        def forward(reference, deadline):
            references.append(reference)
            yield 2222 if reference else None
        def inventory(value, port):
            return {'all': {'children': {'k3s_server': {'hosts': {'observer': {
                'ansible_ssh_common_args': '-o StrictHostKeyChecking=yes -o UserKnownHostsFile=/private/known_hosts',
                'ansible_ssh_private_key_file': '/private/observer_key', 'ansible_port': port or 22,
                'ansible_user': 'railshot-operator',
                'ansible_host': '127.0.0.1' if port else value['inventory']['control_plane'][0]['private_ipv4']}}}}}}
        ansible = SimpleNamespace(forwarded_port=forward, build_inventory=inventory)
        with registration.observer_ssh(self.config, request, ansible) as prefix:
            self.assertEqual(prefix[-1], 'railshot-operator@127.0.0.1')
        direct = {**self.config, 'observer_transport': 'direct'}
        settings(direct)
        with registration.observer_ssh(direct, request, ansible) as prefix:
            self.assertEqual(prefix[-1], 'railshot-operator@192.0.2.20')
            self.assertIn('HostKeyAlias=192.0.2.20', prefix)
            self.assertIn('StrictHostKeyChecking=yes', prefix)
            self.assertIn('/private/observer_key', prefix)
        self.assertEqual(references, ['ssm:ap-northeast-2:i-12345678', None])
        self.assertEqual(request['inventory']['control_plane'][0]['ssh']['transport_ref'], references[0])
        request['inventory']['control_plane'][0]['ssh']['connect_host'] = '192.0.2.99'
        with self.assertRaises(ValueError):
            with registration.observer_ssh(direct, request, ansible):
                self.fail('unregistered observer address accepted')
        with self.assertRaises(ValueError):
            settings({**direct, 'observer_transport': 'arbitrary'})

    def test_bootstrap_is_empty_and_idempotent_without_rotating_password_or_promoting_acceptance(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root) / 'observer'
            config = {**self.config, 'observer_directory': str(directory), 'lifecycle': 'acceptance'}
            payload = {'config': config, 'files': {name: base64.b64encode((HERE / name).read_bytes()).decode()
                for name in ('render.py', 'compose.yaml')}}
            def prepare():
                return subprocess.run([sys.executable, '-c', PREPARE], input=json.dumps(payload), text=True, capture_output=True)
            self.assertEqual(prepare().returncode, 0)
            password = (directory / 'secrets/grafana_password').read_bytes()
            self.assertEqual(json.loads((directory / 'prometheus.json').read_text())['scrape_configs'], [])
            self.assertIn('192.0.2.20:9090:9090', (directory / 'compose.yaml').read_text())
            self.assertIn('127.0.0.1:3000:3000', (directory / 'compose.yaml').read_text())
            self.assertEqual(prepare().returncode, 0)
            self.assertEqual((directory / 'secrets/grafana_password').read_bytes(), password)
            config['lifecycle'] = 'shared'
            self.assertNotEqual(prepare().returncode, 0)
            self.assertEqual((directory / 'secrets/grafana_password').read_bytes(), password)

    def test_failed_sync_preserves_desired_binding_and_conflicting_retry_never_mutates(self):
        with tempfile.TemporaryDirectory() as root:
            root = str(Path(root).resolve())
            config = {**self.config, 'state_dir': root}
            output = Path(root) / 'receipt.json'
            writes = []
            def kube(namespace, *args, document=None):
                if args[:2] == ('get', 'services'):
                    return {'items': []}
                if args[0] == 'apply':
                    writes.append(document)
                return None
            @contextmanager
            def runtime(_):
                yield kube
            environment = SimpleNamespace(read_private=lambda path: json.loads(Path(path).read_text()), runtime_kubectl=runtime)
            with patch.dict(sys.modules, {'environment': environment, 'argo': SimpleNamespace(native=None)}), patch.object(registration, 'node_request', return_value=({}, self.descriptor)), patch.object(registration, 'sync_observer', side_effect=RuntimeError('offline')):
                with self.assertRaises(RuntimeError):
                    registration.register(config, self.request, output)
                self.assertFalse((Path(root) / 'product.json').exists())
                self.assertEqual(json.loads(output.read_text())['status'], 'unknown')
                self.assertEqual(json.loads((Path(root) / 'desired.json').read_text())['targets'][0]['environment_id'], 'env-1')
                before = len(writes)
                with self.assertRaises(ValueError):
                    registration.register(config, {**self.request, 'environment_id': 'env-2'}, output)
                self.assertEqual(len(writes), before)
            with patch.dict(sys.modules, {'environment': environment, 'argo': SimpleNamespace(native=None)}), patch.object(registration, 'node_request', return_value=({}, self.descriptor)), patch.object(registration, 'sync_observer'):
                self.assertTrue(registration.register(config, self.request, output)['registered'])
                self.assertEqual(json.loads((Path(root) / 'product.json').read_text())['targets'][0]['target_id'], 'new-aws')

    def test_node_only_registration_preserves_node_and_cluster_without_a_fake_probe(self):
        request = {key: value for key, value in self.request.items() if key not in {'app', 'namespace', 'probe_url', 'context'}}
        row, rendering = registration_row(self.config, request, self.descriptor)
        self.assertFalse({'app', 'namespace', 'probe_url'} & row.keys())
        self.assertEqual(rendering['probe_urls'], [])
        jobs = scrape_config([row])['scrape_configs']
        self.assertEqual([job['job_name'] for job in jobs], ['node', 'cluster'])
        app_row, _ = registration_row(self.config, self.request, self.descriptor)
        for first, second in [(row, app_row), (app_row, row)]:
            merged = merge_rows(merge_rows([], first), second)
            self.assertEqual(merged, [first, second])
            self.assertEqual(merge_rows(merged, second), merged)
            for key, value in [('resource_id', 'different'), ('prometheus_url', 'http://192.0.2.99:9090'),
                               ('node_instance', '192.0.2.99:30910'), ('cluster_instance', '192.0.2.99:30911'),
                               ('node_ip', '192.0.2.99')]:
                with self.subTest(key=key), self.assertRaises(ValueError):
                    merge_rows([first], {**second, key: value})
        self.assertEqual(scrape_config([row, app_row])['scrape_configs'][-1]['static_configs'],
                         [{'targets': [self.request['probe_url']]}])
        with self.assertRaises(ValueError):
            registration_row(self.config, {**request, 'probe_url': self.request['probe_url']}, self.descriptor)


    def test_gcp_uses_verified_public_route_and_actual_observer_source(self):
        config = {**self.config, 'observer_source_cidr': '198.51.100.20/32'}
        settings(config)
        descriptor = {**self.descriptor, 'provider_kind': 'gcp', 'management_endpoint': 'https://198.51.100.10:6443'}
        row, rendering = registration_row(config, self.request, descriptor)
        self.assertEqual(row['node_instance'], '198.51.100.10:30910')
        self.assertEqual(row['node_ip'], self.request['node_ip'])
        self.assertEqual(rendering['observer_source_cidr'], '198.51.100.20/32')
        for source in ['0.0.0.0/0', '198.51.100.0/24', '127.0.0.1/32']:
            with self.assertRaises(ValueError):
                settings({**config, 'observer_source_cidr': source})



if __name__ == '__main__':
    unittest.main()
