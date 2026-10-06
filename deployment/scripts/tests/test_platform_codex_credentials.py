"""Exercise encrypted delivery, private account binding, and busy-agent refusal."""
import base64
import fcntl
import hashlib
import importlib.util
import inspect
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / 'rotate-platform-codex.py'
spec = importlib.util.spec_from_file_location('credential_rotation', SCRIPT)
rotation = importlib.util.module_from_spec(spec); spec.loader.exec_module(rotation)
sys.path.insert(0, str(SCRIPT.parents[2] / 'ci/scripts/runner'))
from runtime_boundary import effective_auth_route


class CredentialTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.auth = {'auth_mode': 'chatgpt', 'tokens': {'account_id': 'platform-test',
                     'access_token': 'SECRET_CANARY_ACCESS', 'refresh_token': 'SECRET_CANARY_REFRESH'}}
        self.home = self.root / 'codex'; self.home.mkdir(mode=0o700)
        self.file = self.home / 'auth.json'; self.file.write_text(json.dumps(self.auth)); self.file.chmod(0o600)
        self.nonce = 'a' * 32
        # Exercise the real receiver with only its root location/OS owner adapted to this test machine.
        code = inspect.getsource(rotation.receiver).replace('/var/lib/railshot-runner', str(self.root))
        code = code.replace('os.geteuid() == 0', 'os.geteuid() == os.getuid()').replace('info.st_uid == 0', 'info.st_uid == os.getuid()')
        namespace = {}; exec(code, namespace); self.receive = namespace['receiver']

    def envelope(self):
        certificate = self.receive('prepare', self.nonce)['certificate']
        cert = self.root / 'public.pem'; cert.write_text(certificate)
        incoming = self.root / 'incoming.json'
        incoming.write_text(json.dumps({'auth': self.auth, 'model': 'gpt-5.5',
                            'account_sha256': hashlib.sha256(b'platform-test').hexdigest()}))
        encrypted = self.root / 'encrypted.cms'
        subprocess.run(['openssl', 'cms', '-encrypt', '-binary', '-aes-256-cbc', '-in', str(incoming),
                        '-outform', 'DER', '-out', str(encrypted), str(cert)], check=True, capture_output=True)
        return base64.b64encode(encrypted.read_bytes()).decode()

    def test_encrypted_install_and_runtime_binding(self):
        encrypted = self.envelope()
        self.assertNotIn('SECRET_CANARY', encrypted)
        result = self.receive('apply', self.nonce, encrypted)
        self.assertEqual(result['status'], 'installed')
        self.assertNotIn('SECRET_CANARY', json.dumps(result))
        self.assertFalse((self.root / 'credential-rotation' / self.nonce).exists())
        self.assertEqual(self.file.stat().st_mode & 0o777, 0o600)
        route = effective_auth_route(env={'RAILSHOT_CODEX_HOME': str(self.home)})
        self.assertEqual((route['model'], route['credential_rotation']), ('gpt-5.5', self.nonce))
        self.auth['tokens']['account_id'] = 'different-account'; self.file.write_text(json.dumps(self.auth))
        with self.assertRaisesRegex(ValueError, 'binding mismatch'):
            effective_auth_route(env={'RAILSHOT_CODEX_HOME': str(self.home)})

    def test_busy_agent_does_not_replace_auth(self):
        encrypted = self.envelope(); before = self.file.read_bytes()
        locks = self.root / 'work/.capacity'; locks.mkdir(parents=True, mode=0o700)
        with (locks / 'agent-0.lock').open('w') as lock:
            os.chmod(lock.name, 0o600); fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(ValueError, 'AGENT_BUSY'):
                self.receive('apply', self.nonce, encrypted)
        self.assertEqual(self.file.read_bytes(), before)
        self.assertFalse((self.home / 'railshot-account.json').exists())

    def test_private_input_and_account_only(self):
        self.assertEqual(rotation.private_auth(self.file), self.auth)
        self.file.chmod(0o644)
        with self.assertRaisesRegex(ValueError, 'PRIVATE_AUTH_FILE_REQUIRED'):
            rotation.private_auth(self.file)
        self.file.chmod(0o600); link = self.root / 'link'; link.symlink_to(self.file)
        with self.assertRaisesRegex(ValueError, 'PRIVATE_AUTH_FILE_REQUIRED'):
            rotation.private_auth(link)

    def test_failed_model_preflight_does_not_start_delivery(self):
        instance = {'Reservations': [{'Instances': [{'State': {'Name': 'running'},
                    'Tags': [{'Key': 'Name', 'Value': 'railshot-build-worker-aws-01'}]}]}]}
        with patch.dict(os.environ, {}, clear=True), patch.object(rotation, 'aws', side_effect=[{'Account': rotation.ACCOUNT}, instance]), \
                patch.object(rotation, 'deliver') as deliver, \
                patch.object(rotation.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, 'SECRET_CANARY', 'SECRET_CANARY')):
            with self.assertRaisesRegex(ValueError, '^ACCOUNT_MODEL_PREFLIGHT_FAILED$'):
                rotation.rotate(self.file, 'gpt-5.5')
            deliver.assert_not_called()

    def test_operator_to_receiver_with_real_encryption(self):
        original = subprocess.run
        def run(command, **kwargs):
            if command[0] == 'codex':
                return subprocess.CompletedProcess(command, 0, '{"type":"turn.completed"}\n', '')
            return original(command, **kwargs)
        instance = {'Reservations': [{'Instances': [{'State': {'Name': 'running'},
                    'Tags': [{'Key': 'Name', 'Value': 'railshot-build-worker-aws-01'}]}]}]}
        with patch.dict(os.environ, {}, clear=True), patch.object(rotation, 'aws', side_effect=[{'Account': rotation.ACCOUNT}, instance]), \
                patch.object(rotation.subprocess, 'run', side_effect=run), patch.object(rotation, 'deliver', side_effect=self.receive):
            result = rotation.rotate(self.file, 'gpt-5.5')
        self.assertEqual(result['status'], 'installed')
        self.assertNotIn('SECRET_CANARY', json.dumps(result))

    def test_delivery_command_contains_only_ciphertext(self):
        encrypted = self.envelope()
        replies = [{'Command': {'CommandId': 'test'}}, {'Status': 'Success', 'StandardOutputContent': '{"status":"installed"}'}]
        with patch.object(rotation, 'aws', side_effect=replies) as aws, patch.object(rotation.time, 'sleep'):
            rotation.deliver('apply', self.nonce, encrypted)
        parameters = aws.call_args_list[0].args[-1]
        self.assertIn(encrypted, parameters)
        self.assertNotIn('SECRET_CANARY', parameters)

    def test_unobserved_apply_retains_command_id_and_is_not_retried(self):
        for result in (ValueError('AWS_REQUEST_FAILED'), {'Status': 'Failed'},
                       {'Status': 'Success', 'StandardOutputContent': 'not-json'}):
            with self.subTest(result=result), patch.object(rotation, 'aws', side_effect=[{'Command': {'CommandId': 'command-id'}}, result]) as aws, \
                    patch.object(rotation.time, 'sleep'):
                with self.assertRaisesRegex(ValueError, '^DELIVERY_OUTCOME_UNKNOWN:command-id$'):
                    rotation.deliver('apply', self.nonce, 'ciphertext')
                self.assertEqual(aws.call_count, 2)
        with patch.object(rotation, 'aws', side_effect=TimeoutError) as aws:
            with self.assertRaisesRegex(ValueError, '^DELIVERY_SUBMISSION_UNKNOWN:' + self.nonce + '$'):
                rotation.deliver('apply', self.nonce, 'ciphertext')
            self.assertEqual(aws.call_count, 1)


if __name__ == '__main__':
    unittest.main()
