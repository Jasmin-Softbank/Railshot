import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'ci/scripts'))
sys.path.insert(0, str(ROOT / 'deployment/scripts'))
import openstack_routes as routes


class OpenStackRoutesTest(unittest.TestCase):
    def test_fixed_worker_private_transport_and_bound_readback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            def private(name, content):
                path = root / name
                path.write_text(content)
                path.chmod(0o600)
                return str(path)
            connection = {'host': '10.0.0.34', 'port': 22, 'user': 'railshot-operator',
                          'identity_file': private('key', 'test-only'),
                          'known_hosts_file': private('known', 'test-only'), 'host_key_alias': '10.0.0.34'}
            config = {'version': 1, 'provider': 'openstack', 'base_domain': 'railshot.io',
                      'runtime_private_address': '10.0.0.17', 'controller': connection,
                      'proxy': {**connection, 'host': '172.31.0.172', 'port': 10022, 'host_key_alias': '10.0.0.17'}}
            path = private('config.json', json.dumps(config))
            request = {'application_id': 'app-' + 'a' * 24, 'hostname': 'user-app.railshot.io',
                       'private_address': '10.0.0.17', 'node_port': 32123, 'health_path': '/health'}
            receipt = {**request, 'status': 'configured', 'https_verified': False,
                       'network_rule_id': 'abcdef01-1234-1234-1234-123456789abc',
                       'resources': {name: '12345678-1234-1234-1234-123456789abc'
                                     for name in ('pool', 'member', 'monitor', 'policy', 'rule')}}
            with patch.object(routes.subprocess, 'run') as run:
                run.return_value = subprocess.CompletedProcess([], 0, json.dumps(receipt), '')
                self.assertEqual(routes.ensure(path, request), receipt)
                command = run.call_args.args[0]
                self.assertEqual(command[-1], routes.COMMAND)
                self.assertNotIn(request['hostname'], ' '.join(command))
                self.assertIn('StrictHostKeyChecking=yes', command)
                self.assertTrue(any(value.startswith('ProxyCommand=ssh ') for value in command))
                self.assertEqual(json.loads(run.call_args.kwargs['input']), request)
                self.assertFalse(run.call_args.kwargs['shell'])
                for change in ({'private_address': '10.0.0.18'}, {'node_port': True},
                               {'hostname': 'other.example.com'}, {'health_path': '/health\nignored'}):
                    with self.subTest(change=change), self.assertRaises(ValueError):
                        routes.ensure(path, {**request, **change})
                self.assertEqual(run.call_count, 1)
                run.return_value.stdout = json.dumps({key: value for key, value in receipt.items()
                                                     if key != 'network_rule_id'})
                with self.assertRaisesRegex(ValueError, 'NETWORK_READBACK_DIFFERS'):
                    routes.ensure(path, request)
                run.return_value.stdout = json.dumps({**receipt, 'hostname': 'wrong.railshot.io'})
                with self.assertRaisesRegex(ValueError, 'READBACK_DIFFERS'):
                    routes.ensure(path, request)
                run.reset_mock()
                run.side_effect = subprocess.TimeoutExpired('ssh', 600)
                with self.assertRaisesRegex(ValueError, 'OUTCOME_UNKNOWN'):
                    routes.ensure(path, request)
                self.assertEqual(run.call_count, 1)


if __name__ == '__main__':
    unittest.main()
