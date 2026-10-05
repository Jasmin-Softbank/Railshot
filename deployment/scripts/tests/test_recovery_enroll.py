from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import recovery_enroll as enroll
import recovery_service as service


class EnrollmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.registry = self.root / "registry.json"
        self.ca_path, self.key_path = self.root / "ca.crt", self.root / "ca.key"
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "synthetic-enroll-ca")])
        now = datetime.now(timezone.utc)
        self.ca = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
                   .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(days=1))
                   .not_valid_after(now + timedelta(days=365)).add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
                   .sign(key, hashes.SHA256()))
        self.ca_path.write_bytes(self.ca.public_bytes(serialization.Encoding.PEM))
        self.key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        self.ca_path.chmod(0o600)
        self.key_path.chmod(0o600)
        original_read, original_directory = service.private_read, service.private_directory
        self.patches = [patch.object(module, name, replacement)
                        for module in (service, enroll)
                        for name, replacement in (("private_read", lambda path: original_read(path, owner=os.getuid())),
                                                  ("private_directory", lambda path: original_directory(path, owner=os.getuid())))]
        for patcher in self.patches:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.patches):
            patcher.stop()
        self.temp.cleanup()

    def issue(self, environment, suffix=""):
        output = self.root / (environment + suffix)
        output.mkdir(mode=0o700)
        return enroll.issue(environment, "both", self.ca_path, self.key_path, self.registry, output)

    def test_concurrent_environment_issuance_and_explicit_revocation(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(self.issue, ("env-a", "env-b")))
        registry = service.load_registry(self.registry)
        self.assertEqual(len(registry), 4)
        keys = []
        for result in results:
            for role, client in result["clients"].items():
                self.assertEqual(registry[client["fingerprint"]], {"environment_id": result["environment_id"], "role": role})
                cert = x509.load_pem_x509_certificate(Path(client["cert_file"]).read_bytes())
                cert.verify_directly_issued_by(self.ca)
                keys.append(Path(client["key_file"]).read_bytes())
                self.assertEqual(Path(client["key_file"]).stat().st_mode & 0o777, 0o600)
        self.assertEqual(len(set(keys)), 4)
        old = results[0]["clients"]["runtime"]["fingerprint"]
        self.issue("env-a", "-renewed")
        self.assertIn(old, service.load_registry(self.registry))
        with self.assertRaises(ValueError):
            enroll.revoke("env-b", "runtime", old, self.registry)
        enroll.revoke("env-a", "runtime", old, self.registry)
        self.assertNotIn(old, service.load_registry(self.registry))
        self.assertTrue(Path(results[0]["clients"]["runtime"]["key_file"]).exists())

    def test_no_existing_credential_overwrite(self):
        result = self.issue("env-a")
        before = Path(result["clients"]["runtime"]["key_file"]).read_bytes()
        with self.assertRaises(ValueError):
            enroll.issue("env-a", "both", self.ca_path, self.key_path, self.registry, self.root / "env-a")
        self.assertEqual(Path(result["clients"]["runtime"]["key_file"]).read_bytes(), before)
        self.assertEqual(len(service.load_registry(self.registry)), 2)

    def test_registry_failure_retains_private_keys_for_reconciliation(self):
        output = self.root / "env-a"
        output.mkdir(mode=0o700)
        with patch.object(enroll, "write_registry", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                enroll.issue("env-a", "both", self.ca_path, self.key_path, self.registry, output)
        self.assertTrue((output / "runtime.key").is_file())
        self.assertEqual((output / "runtime.key").stat().st_mode & 0o777, 0o600)
        self.assertFalse(self.registry.exists())


if __name__ == "__main__":
    unittest.main()
