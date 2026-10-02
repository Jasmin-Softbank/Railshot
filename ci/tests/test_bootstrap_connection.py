import base64
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'deployment/bootstrap'))
from client_setup import enrollment, preflight, wireguard
from install_payload import install_payload

KEY = base64.b64encode(bytes(range(32))).decode()
DATA = dict(node_id='node-1', address='10.253.0.2/32', server_public_key=KEY,
            endpoint='vpn.example.com:51820', allowed_ips=['10.253.0.1/32'],
            probe_url='https://10.253.0.1/health')


def result(args, **kwargs):
    if args[:3] == ['ip', 'link', 'show']:
        return subprocess.CompletedProcess(args, 1, '', '')
    return subprocess.CompletedProcess(args, 0, '[]' if args[:2] == ['ip', '-j'] else '', '')


class ConnectionTests(unittest.TestCase):
    def test_response_rejects_injection_and_broad_route(self):
        for field, value in [('endpoint', 'good:51820\nPostUp = evil'), ('address', '1.2.3.4\nDNS=evil'),
                             ('allowed_ips', ['0.0.0.0/0']), ('allowed_ips', ['10.0.0.0/8']),
                             ('probe_url', 'https://127.0.0.1/'), ('probe_url', 'http://10.253.0.1/')]:
            with self.subTest(field=field, value=value), self.assertRaises((ValueError, TypeError)):
                enrollment.validate_registration(dict(DATA, **{field: value}))
        self.assertEqual(enrollment.validate_registration(DATA), DATA)

    def test_registration_limits_and_redaction(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.status = 200
        response.read.return_value = json.dumps(DATA).encode()
        opener = Mock()
        opener.open.return_value = response
        enrollment.enroll_client('https://service.example', 'secretsecretsecret', KEY, 'request-id-123456', opener=opener)
        request = opener.open.call_args.args[0]
        self.assertNotIn('secretsecretsecret', request.data.decode())
        self.assertEqual(json.loads(request.data)['request_id'], 'request-id-123456')
        opener.open.side_effect = RuntimeError('secretsecretsecret')
        with self.assertRaises(RuntimeError) as exc:
            enrollment.enroll_client('https://service.example', 'secretsecretsecret', KEY, 'request-id-123456', opener=opener)
        self.assertNotIn('secretsecretsecret', str(exc.exception))

    def test_redirect_rejected(self):
        with self.assertRaises(RuntimeError):
            enrollment.NoRedirect().redirect_request(None, None, 307, '', {}, 'https://other.example')

    def test_foreign_config_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory).resolve() / 'jasmin0.conf'
            path.write_text('foreign config')
            runner = Mock(side_effect=result)
            with self.assertRaises(RuntimeError):
                wireguard.configure_wireguard(DATA, KEY, path, runner=runner)
            with self.assertRaises(RuntimeError):
                wireguard.uninstall_wireguard(path, runner=runner)
            runner.assert_not_called()
            self.assertEqual(path.read_text(), 'foreign config')

    def test_new_install_and_idempotent_rerun(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory).resolve() / 'jasmin0.conf'
            runner = Mock(side_effect=result)
            verifier = Mock(return_value={'connected': True})
            self.assertFalse(wireguard.configure_wireguard(DATA, KEY, path, runner=runner, verifier=verifier)['reused'])
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertTrue(wireguard.configure_wireguard(DATA, KEY, path, runner=runner, verifier=verifier)['reused'])
            self.assertEqual(wireguard.uninstall_wireguard(path, runner=runner), {'removed': True})

    def test_failure_removes_only_new_config(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory).resolve() / 'jasmin0.conf'
            runner = Mock(side_effect=result)
            with self.assertRaises(RuntimeError):
                wireguard.configure_wireguard(DATA, KEY, path, runner=runner, verifier=Mock(side_effect=RuntimeError()))
            self.assertFalse(path.exists())
            self.assertIn(['systemctl', 'disable', '--now', 'wg-quick@jasmin0'], [call.args[0] for call in runner.call_args_list])

    def test_route_conflict(self):
        runner = Mock(return_value=subprocess.CompletedProcess([], 0, '[{"dst":"10.253.0.0/24","dev":"eth0"}]', ''))
        with self.assertRaises(RuntimeError):
            wireguard.check_route_conflicts(DATA, runner=runner)

    def test_symlink_refusal(self):
        with tempfile.TemporaryDirectory() as directory:
            actual = Path(directory).resolve() / 'actual'
            actual.mkdir()
            link = Path(directory).resolve() / 'link'
            link.symlink_to(actual, target_is_directory=True)
            with self.assertRaises(RuntimeError):
                wireguard.configure_wireguard(DATA, KEY, link / 'jasmin0.conf')

    def test_payload_install_preserves_existing_and_checks_integrity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source = root / 'source'
            module = source / 'deployment/bootstrap/client_setup/main.py'
            module.parent.mkdir(parents=True)
            module.write_text('# test payload')
            target = root / 'installed'
            install_payload(source, target)
            install_payload(source, target)
            installed = target / 'deployment/bootstrap/client_setup/main.py'
            installed.write_text('# altered')
            with self.assertRaises(RuntimeError):
                install_payload(source, target)
            self.assertEqual(installed.read_text(), '# altered')
            foreign = root / 'foreign'
            foreign.mkdir()
            (foreign / 'existing').write_text('preserve')
            with self.assertRaises(RuntimeError):
                install_payload(source, foreign)
            self.assertEqual((foreign / 'existing').read_text(), 'preserve')

    def test_preflight(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory).resolve() / 'os-release'
            path.write_text('ID=ubuntu\nVERSION_ID="24.04"\n')
            runner = Mock(return_value=subprocess.CompletedProcess([], 0, 'running\n', ''))
            self.assertEqual(preflight.run_preflight(os_release=path, geteuid=lambda: 0, which=lambda n: n, runner=runner)['version'], '24.04')
            with self.assertRaises(RuntimeError):
                preflight.run_preflight(os_release=path, geteuid=lambda: 1)
            with self.assertRaises(RuntimeError):
                preflight.run_preflight(os_release=path, geteuid=lambda: 0, which=lambda n: None)


if __name__ == '__main__':
    unittest.main()
