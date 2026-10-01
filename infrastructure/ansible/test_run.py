"""Offline boundary tests: fake receipts are local test fixtures, not deployment evidence."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('railshot_ansible', HERE / 'run.py')
adapter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter)
ROOT = HERE.parents[1]


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.request = json.loads((ROOT / 'examples/ansible/runtime-single-node.json').read_text())
        for name, field in [('identity', 'identity_file'), ('known_hosts', 'known_hosts_file')]:
            p = Path(self.temp.name) / name
            p.write_text('offline fixture, not a credential\n'); p.chmod(0o600)
            self.request['inventory']['control_plane'][0]['ssh'][field] = str(p)
        self.commands = []

    def runner(self, argv, timeout, env):
        self.commands.append(argv)
        self.assertGreater(timeout, 0)
        self.assertLessEqual(timeout, self.request['timeout_seconds'])
        self.assertEqual(env['ANSIBLE_HOST_KEY_CHECKING'], 'True')
        variables = json.loads(Path(argv[-1][1:]).read_text())
        stage = Path(argv[3]).stem
        Path(variables['railshot_receipt_path']).write_text(json.dumps({
            'request_id': variables['railshot_request_id'], 'target_id': variables['railshot_target_id'],
            'node_id': variables['k3s_node_name'], 'nonce': variables['railshot_nonce'],
            'stage': stage, stage + '_ready': True}))
        return 0

    def test_local_receipts_only_admit_matching_completed_stages(self):
        with patch.object(adapter.shutil, 'which', return_value='/trusted/ansible-playbook'):
            result = adapter.run(self.request, runner=self.runner)
        self.assertEqual(result['status'], 'succeeded')
        self.assertTrue(result['guest_ready']); self.assertTrue(result['runtime_ready'])
        self.assertFalse(result['application_ready']); self.assertFalse(result['public_http_verified'])
        self.assertEqual([Path(x[3]).name for x in self.commands], ['guest.yml', 'runtime.yml'])
        self.assertFalse(any('site.yml' in x for cmd in self.commands for x in cmd))

    def test_exit_zero_without_readiness_receipt_is_not_success(self):
        with patch.object(adapter.shutil, 'which', return_value='/trusted/ansible-playbook'):
            result = adapter.run(self.request, runner=lambda *_: 0)
        self.assertEqual(result['error']['code'], 'READINESS_UNPROVEN')
        self.assertFalse(result['guest_ready']); self.assertFalse(result['runtime_ready'])

    def test_wrong_request_receipt_is_rejected(self):
        def stale(argv, timeout, env):
            self.runner(argv, timeout, env)
            variables = json.loads(Path(argv[-1][1:]).read_text())
            p = Path(variables['railshot_receipt_path']); receipt = json.loads(p.read_text())
            receipt['request_id'] = 'another-attempt'; p.write_text(json.dumps(receipt))
            return 0
        with patch.object(adapter.shutil, 'which', return_value='/trusted/ansible-playbook'):
            result = adapter.run(self.request, runner=stale)
        self.assertEqual(result['error']['code'], 'READINESS_UNPROVEN')

    def test_guest_failure_prevents_runtime(self):
        calls = []
        def failed(*args): calls.append(args); return 4
        with patch.object(adapter.shutil, 'which', return_value='/trusted/ansible-playbook'):
            result = adapter.run(self.request, runner=failed)
        self.assertEqual(len(calls), 1)
        self.assertEqual(result['error']['code'], 'GUEST_CHECK_FAILED')
        self.assertFalse(result['runtime_ready'])

    def test_partial_runtime_failure_retains_only_guest_readiness(self):
        def partial(argv, timeout, env):
            return self.runner(argv, timeout, env) if Path(argv[3]).stem == 'guest' else 2
        with patch.object(adapter.shutil, 'which', return_value='/trusted/ansible-playbook'):
            result = adapter.run(self.request, runner=partial)
        self.assertTrue(result['guest_ready']); self.assertFalse(result['runtime_ready'])
        self.assertTrue(result['error']['outcome_unknown']); self.assertFalse(result['error']['retryable'])

    def test_runtime_timeout_is_bounded_and_not_retried(self):
        def timeout(argv, seconds, env):
            if Path(argv[3]).stem == 'guest': return self.runner(argv, seconds, env)
            raise subprocess.TimeoutExpired(argv, seconds)
        with patch.object(adapter.shutil, 'which', return_value='/trusted/ansible-playbook'):
            result = adapter.run(self.request, runner=timeout)
        self.assertEqual(result['error']['code'], 'EXECUTION_TIMEOUT')
        self.assertTrue(result['error']['outcome_unknown']); self.assertTrue(result['guest_ready'])

    def test_validation_never_executes_or_claims_readiness(self):
        result = adapter.run(self.request, validate_only=True, runner=lambda *_: self.fail('must not run'))
        self.assertEqual(result['status'], 'validated')
        self.assertFalse(result['guest_ready']); self.assertFalse(result['runtime_ready'])

    def test_arbitrary_playbook_extra_vars_and_shell_paths_are_rejected(self):
        for field in ('playbook', 'extra_vars', 'shell'):
            request = copy.deepcopy(self.request); request[field] = 'anything'
            self.assertEqual(adapter.run(request, validate_only=True)['status'], 'invalid')
        self.request['inventory']['control_plane'][0]['ssh']['identity_file'] = '/tmp/key;touch/tmp/marker'
        self.assertEqual(adapter.run(self.request, validate_only=True)['status'], 'invalid')

    def test_public_loopback_and_ipv6_addresses_are_rejected(self):
        for address in ('192.0.2.10', '127.0.0.1', '169.254.1.2', '::1', '8.8.8.8'):
            self.request['inventory']['control_plane'][0]['private_ipv4'] = address
            self.assertEqual(adapter.run(self.request, validate_only=True)['status'], 'invalid')

    def test_worker_request_is_explicitly_blocked(self):
        worker = copy.deepcopy(self.request['inventory']['control_plane'][0])
        worker.update(id='worker-1', resource_id='vm-worker', private_ipv4='10.77.0.11')
        self.request['inventory']['workers'].append(worker)
        result = adapter.run(self.request, validate_only=True)
        self.assertEqual(result['status'], 'blocked'); self.assertEqual(result['error']['code'], 'SINGLE_NODE_ONLY')

    def test_duplicate_identity_and_bool_timeout_rejected(self):
        self.request['inventory']['workers'] = [copy.deepcopy(self.request['inventory']['control_plane'][0])]
        self.assertEqual(adapter.run(self.request, validate_only=True)['status'], 'invalid')
        self.request['inventory']['workers'] = []; self.request['timeout_seconds'] = True
        self.assertEqual(adapter.run(self.request, validate_only=True)['status'], 'invalid')

    def test_placement_is_recorded_without_fabricating_patroni(self):
        request = json.loads((ROOT / 'examples/ansible/patroni-placement-blocked.json').read_text())
        result = adapter.run(request, runner=lambda *_: self.fail('must not run'))
        self.assertEqual(result['error']['code'], 'PATRONI_PLAYBOOK_UNAVAILABLE')
        self.assertFalse(result['guest_ready']); self.assertFalse(result['runtime_ready'])
        # A different count is not silently rewritten to the meeting's 3+2 example.
        request['patroni']['placements'][0]['dcs_voters'] = 2
        self.assertEqual(adapter.run(request, validate_only=True)['status'], 'blocked')

    def test_ssh_identity_and_host_verification_are_explicit(self):
        inv = adapter.build_inventory(self.request)
        host = inv['all']['children']['k3s_server']['hosts']['demo-cp']
        flags = host['ansible_ssh_common_args']
        for value in ('StrictHostKeyChecking=yes', 'IdentitiesOnly=yes', 'IdentityAgent=none', 'ProxyCommand=none', '-F /dev/null'):
            self.assertIn(value, flags)
        self.assertIn('known_hosts', flags)
        with patch.dict(os.environ, {'ANSIBLE_HOST_KEY_CHECKING': 'False', 'AWS_SECRET_ACCESS_KEY': 'fixture', 'SSH_AUTH_SOCK': 'fixture'}):
            env = adapter.child_env()
        self.assertEqual(env['ANSIBLE_HOST_KEY_CHECKING'], 'True')
        self.assertNotIn('AWS_SECRET_ACCESS_KEY', env); self.assertNotIn('SSH_AUTH_SOCK', env)

    def test_key_permissions_block_before_execution(self):
        Path(self.request['inventory']['control_plane'][0]['ssh']['identity_file']).chmod(0o644)
        result = adapter.run(self.request, runner=lambda *_: self.fail('must not run'))
        self.assertEqual(result['error']['code'], 'SSH_REFERENCE_UNAVAILABLE')

    def test_cli_stdin_and_duplicate_keys(self):
        command = [sys.executable, str(HERE / 'run.py'), '--validate-only']
        good = subprocess.run(command, input=json.dumps(self.request), capture_output=True, text=True, timeout=5)
        self.assertEqual(good.returncode, 0)
        self.assertEqual(json.loads(good.stdout)['status'], 'validated')
        bad = subprocess.run(command, input='{"schema_version":"1.0","schema_version":"2.0"}', capture_output=True, text=True, timeout=5)
        self.assertEqual(bad.returncode, 2)
        self.assertEqual(json.loads(bad.stdout)['error']['code'], 'INVALID_JSON')

    def test_real_local_process_timeout(self):
        with self.assertRaises(subprocess.TimeoutExpired):
            adapter.execute([sys.executable, '-c', 'import time; time.sleep(30)'], 0.05, adapter.child_env())


if __name__ == '__main__':
    unittest.main()
