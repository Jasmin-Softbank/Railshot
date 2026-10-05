import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location("recovery_escrow", ROOT / "deployment/scripts/recovery_escrow.py")
escrow = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(escrow)


class RecoveryEscrowTests(unittest.TestCase):
    def test_redirects_are_rejected_before_forwarding_material(self):
        with self.assertRaisesRegex(ValueError, "redirect rejected"):
            escrow.NoRedirect().redirect_request(None, None, 307, "redirect", {}, "https://other.invalid")

    def test_malformed_private_config_returns_sanitized_error_only(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "escrow.json"
            config.write_text('{"endpoint":"https://secret-host.invalid/path"}')
            config.chmod(0o644)
            output = io.StringIO()
            with patch.object(escrow, "CONFIG", config), patch("sys.stdin", io.TextIOWrapper(io.BytesIO(b"{}"))), \
                    patch("sys.stdout", output):
                self.assertEqual(escrow.main(), 3)
            result = output.getvalue()
            self.assertEqual(json.loads(result), {"stored": False, "error": "ESCROW_FAILED"})
            self.assertNotIn("secret-host", result)


if __name__ == "__main__":
    unittest.main()

# Reuse only the local certificate/server fixture; all URLs bind loopback.
import test_recovery_service as server_fixture
payload = server_fixture.payload


class ClientContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        server_fixture.MutualTLSLocalTests.setUpClass()

    @classmethod
    def tearDownClass(cls):
        server_fixture.MutualTLSLocalTests.tearDownClass()

    def setUp(self):
        self.fixture = server_fixture.MutualTLSLocalTests('test_role_bound_envelope_and_tamper')
        self.fixture.setUp(); self.addCleanup(self.fixture.tearDown)
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)

    def client(self, identity='runtime'):
        f = self.fixture
        path = Path(self.temp.name) / (identity + '.json')
        cert, key = f.identities[identity][2:]
        path.write_text(json.dumps({'version': 1, 'endpoint': f'https://localhost:{f.server.server_port}/api/v1/escrows',
                                   'ca_file': str(f.ca_path), 'client_cert_file': str(cert), 'client_key_file': str(key)}))
        path.chmod(0o600)
        return escrow.Client(path)

    def test_real_client_response_loss_recovers_the_same_receipt_and_encrypted_material(self):
        client = self.client(); value = payload(); original = client.request
        def lost(method, *args, **kwargs):
            result = original(method, *args, **kwargs)
            if method == 'POST': raise OSError('response lost after commit')
            return result
        with patch.object(client, 'request', side_effect=lost):
            ack = client.store(value)
        recovered = client.recover(value['environment_id'], value['kind'], value['operation_id'])
        self.assertEqual(recovered['receipt_id'], ack['receipt_id'])
        self.assertEqual(recovered['material'], {'root_token': value['material']['root_token']})
        self.assertNotIn('material', ack)

    def test_forged_receipt_and_wrong_environment_envelope_are_rejected(self):
        client = self.client(); value = payload(); original = client.request
        def forged(method, *args, **kwargs):
            result = original(method, *args, **kwargs)
            if method == 'POST': return {'stored': True, 'receipt_id': 'forged-receipt'}
            return result
        with patch.object(client, 'request', side_effect=forged), self.assertRaises(ValueError):
            client.store(value)
        with self.assertRaises(ValueError):
            client.recover('env-other', value['kind'], value['operation_id'])

    def test_manager_delivery_handoff_only_decrypts_its_bound_environment(self):
        value = payload('vault-delivery-approle'); self.client().store(value)
        recovered = self.client('manager').recover('env-a', value['kind'], value['operation_id'])
        self.assertEqual(recovered['material'], value['material'])
        with self.assertRaises(Exception): self.client().recover('env-a', value['kind'], value['operation_id'])
