import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
import http.client
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import ssl
import tempfile
import threading
import unittest
from unittest.mock import patch

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID

ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location("recovery_service", ROOT / "deployment/scripts/recovery_service.py")
service = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(service)


def payload(kind="vault-initialization", environment="env-a", operation="bootstrap-1"):
    shares = [bytes([i]) * 33 for i in range(1, 6)]
    material = {"root_token": "hvs.root-very-sensitive", "recovery_keys_b64": [base64.b64encode(s).decode() for s in shares],
                "recovery_keys_hex": [s.hex() for s in shares], "recovery_keys_shares": 5,
                "recovery_keys_threshold": 3, "unseal_keys_b64": [], "unseal_keys_hex": [],
                "unseal_shares": 1, "unseal_threshold": 1}
    if kind == "vault-delivery-approle":
        material = {"role_id": "a-role-id-sensitive", "secret_id": "a-secret-id-sensitive"}
    return {"version": 1, "environment_id": environment, "kind": kind, "material": material, "operation_id": operation}


class CustodyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.keys = {"key-1": AESGCM.generate_key(bit_length=256)}
        self.store = service.Custody(self.root, "key-1", self.keys, owner=os.getuid())

    def tearDown(self):
        self.temp.cleanup()

    def test_concurrent_duplicate_restart_and_conflict(self):
        value = payload()
        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(lambda _: self.store.store(value), range(16)))
        self.assertEqual(sum(created for _, created in results), 1)
        self.assertEqual(len({r["receipt_id"] for r, _ in results}), 1)
        reopened = service.Custody(self.root, "key-1", self.keys, owner=os.getuid())
        self.assertEqual(reopened.store(value), (results[0][0], False))
        changed = payload()
        changed["material"]["root_token"] = "different-root-token"
        with self.assertRaises(service.Rejected) as caught:
            reopened.store(changed)
        self.assertEqual(caught.exception.status, 409)
        with self.assertRaises(service.Rejected):
            reopened.lookup("env-b", receipt=results[0][0]["receipt_id"])
        raw = (self.root / "custody.sqlite3").read_bytes()
        self.assertNotIn(b"root-very-sensitive", raw)
        self.assertNotIn(value["material"]["recovery_keys_b64"][0].encode(), raw)

    def test_commit_failure_does_not_ack_or_persist(self):
        original = self.store.connection
        class FailingCommit:
            def __enter__(inner):
                inner.cm = original()
                inner.db = inner.cm.__enter__()
                return inner.db
            def __exit__(inner, *args):
                inner.db.rollback()
                inner.cm.__exit__(*args)
                raise sqlite3.OperationalError("disk failure")
        with patch.object(self.store, "connection", side_effect=FailingCommit):
            with self.assertRaises(sqlite3.OperationalError):
                self.store.store(payload())
        self.assertEqual(self.store.verify(), 0)

    def test_wrong_key_tamper_and_rotation(self):
        receipt, _ = self.store.store(payload())
        wrong = service.Custody(self.root, "key-1", {"key-1": os.urandom(32)}, owner=os.getuid())
        with self.assertRaises(Exception):
            wrong.lookup("env-a", receipt=receipt["receipt_id"])
        new_keys = {**self.keys, "key-2": os.urandom(32)}
        rotated = service.Custody(self.root, "key-2", new_keys, owner=os.getuid())
        self.assertEqual(rotated.rotate(), 1)
        rotated.keys = {"key-2": new_keys["key-2"]}
        self.assertEqual(rotated.verify(), 1)
        with self.store.connection() as db:
            db.execute("UPDATE escrows SET operation_id='changed-context'")
        with self.assertRaises(Exception):
            rotated.verify()

    def test_backup_restore_and_exclusive_output(self):
        self.store.store(payload())
        backup = self.root / "backup.sqlite3"
        with patch.object(service, "private_directory", side_effect=lambda p: None):
            self.store.backup(backup)
            with self.assertRaises(FileExistsError):
                self.store.backup(backup)
        restore = self.root / "restore"
        restore.mkdir(mode=0o700)
        (restore / "custody.sqlite3").write_bytes(backup.read_bytes())
        (restore / "custody.sqlite3").chmod(0o600)
        restored = service.Custody(restore, "key-1", self.keys, owner=os.getuid())
        self.assertEqual(restored.verify(), 1)
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)

    def test_strict_vault_material_and_duplicate_fields(self):
        self.assertEqual(service.validate_payload(payload(), "env-a"), payload())
        for mutate in [lambda v: v["material"].update(extra="secret"),
                       lambda v: v["material"].update(recovery_keys_b64=["bad"] * 5),
                       lambda v: v["material"].update(recovery_keys_threshold=True),
                       lambda v: v.update(version=True),
                       lambda v: v.update(environment_id="env-b")]:
            value = payload()
            mutate(value)
            with self.assertRaises(service.Rejected):
                service.validate_payload(value, "env-a")
        with self.assertRaises(service.Rejected):
            service.strict_json('{"version":1,"version":1}')

    def test_cli_stored_key_counts_and_legacy_shape(self):
        # Official Vault 2.1.1 newMachineInit uses 1/1 with empty unseal arrays.
        self.assertEqual(service.validate_payload(payload(), "env-a"), payload())
        legacy = payload()
        legacy["material"].update(unseal_shares=0, unseal_threshold=0)
        self.assertEqual(service.validate_payload(legacy, "env-a"), legacy)
        invalid = [{"unseal_shares": 0}, {"unseal_threshold": 0},
                   {"unseal_shares": True}, {"unseal_threshold": True},
                   {"unseal_shares": 2, "unseal_threshold": 2},
                   {"unseal_keys_b64": ["unexpected"]}, {"unseal_keys_hex": ["unexpected"]},
                   {"recovery_keys_threshold": 1}]
        for update in invalid:
            with self.subTest(update=update):
                value = payload()
                value["material"].update(update)
                with self.assertRaises(service.Rejected):
                    service.validate_payload(value, "env-a")
        for missing in ("unseal_shares", "unseal_threshold", "unseal_keys_b64", "unseal_keys_hex",
                        "recovery_keys_shares", "recovery_keys_threshold"):
            with self.subTest(missing=missing):
                value = payload()
                del value["material"][missing]
                with self.assertRaises(service.Rejected):
                    service.validate_payload(value, "env-a")


class MutualTLSLocalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "local-test-ca")])
        now = datetime.now(timezone.utc)
        ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name).public_key(ca_key.public_key())
              .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=1))
              .not_valid_after(now + timedelta(days=1)).add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
              .sign(ca_key, hashes.SHA256()))
        cls.ca_path = cls.root / "ca.pem"
        cls.ca_path.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
        cls.identities = {}
        for name in ("server", "runtime", "manager", "other", "unregistered"):
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            builder = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)]))
                       .issuer_name(ca_name).public_key(key.public_key()).serial_number(x509.random_serial_number())
                       .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=1))
                       .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                       .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH if name == "server" else ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False))
            if name == "server":
                builder = builder.add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
            cert = builder.sign(ca_key, hashes.SHA256())
            cert_path, key_path = cls.root / (name + ".crt"), cls.root / (name + ".key")
            cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
            key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
            key_path.chmod(0o600)
            cls.identities[name] = (key, cert, cert_path, key_path)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = service.Custody(self.directory.name, "key-1", {"key-1": os.urandom(32)}, owner=os.getuid())
        self.registry = {hashlib.sha256(self.identities[name][1].public_bytes(serialization.Encoding.DER)).hexdigest():
                         {"environment_id": "env-b" if name == "other" else "env-a", "role": "manager" if name == "manager" else "runtime"}
                         for name in ("runtime", "manager", "other")}
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.verify_mode = ssl.CERT_REQUIRED
        ctx.load_verify_locations(cafile=self.ca_path)
        ctx.load_cert_chain(*self.identities["server"][2:])
        self.server = service.Server(("127.0.0.1", 0), self.store, self.registry, ctx)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.directory.cleanup()

    def request(self, method="POST", path="/api/v1/escrows", body=None, identity="runtime"):
        ctx = ssl.create_default_context(cafile=self.ca_path)
        if identity:
            ctx.load_cert_chain(*self.identities[identity][2:])
        conn = http.client.HTTPSConnection("localhost", self.server.server_port, context=ctx, timeout=5)
        try:
            conn.request(method, path, body=None if body is None else json.dumps(body), headers={"Content-Type": "application/json"})
            response = conn.getresponse()
            headers = dict(response.getheaders())
            return response.status, json.loads(response.read()), headers
        finally:
            conn.close()

    def test_real_mtls_and_response_loss_reconciliation(self):
        status, receipt, headers = self.request(body=payload())
        self.assertEqual(status, 201)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertIn("X-Request-ID", headers)
        status, repeated, _ = self.request(body=payload())
        self.assertEqual((status, repeated), (200, receipt))
        status, found, _ = self.request("GET", "/api/v1/escrows?kind=vault-initialization&operation_id=bootstrap-1")
        self.assertEqual((status, found), (200, receipt))
        status, found, _ = self.request("GET", headers["Location"], identity="other")
        self.assertEqual(status, 404)
        self.assertEqual(self.request(body=payload(environment="env-b"))[0], 403)
        self.assertEqual(self.request(body=payload(), identity="unregistered")[0], 403)
        self.assertEqual(self.request(body=payload(), identity="manager")[0], 403)
        with self.assertRaises((ssl.SSLError, ConnectionError, http.client.RemoteDisconnected)):
            self.request(body=payload(), identity=None)

    def test_role_bound_envelope_and_tamper(self):
        for kind, purpose, identity in (("vault-initialization", "initialization-resume", "runtime"),
                                       ("vault-delivery-approle", "delivery-handoff", "manager")):
            value = payload(kind)
            _, receipt, _ = self.request(body=value)
            path = "/api/v1/escrows/" + receipt["receipt_id"] + "/exports"
            status, envelope, _ = self.request(path=path, body={"purpose": purpose}, identity=identity)
            self.assertEqual(status, 200)
            self.assertNotIn("root-very-sensitive", json.dumps(envelope))
            self.assertNotIn("a-secret-id-sensitive", json.dumps(envelope))
            metadata = {k: v for k, v in envelope.items() if k not in ("wrapped_key", "nonce", "ciphertext")}
            key = self.identities[identity][0].decrypt(base64.b64decode(envelope["wrapped_key"]), padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None))
            plaintext = AESGCM(key).decrypt(base64.b64decode(envelope["nonce"]), base64.b64decode(envelope["ciphertext"]), service.canonical(metadata))
            self.assertEqual(json.loads(plaintext), {"root_token": value["material"]["root_token"]} if kind == "vault-initialization" else value["material"])
            metadata["environment_id"] = "env-b"
            with self.assertRaises(Exception):
                AESGCM(key).decrypt(base64.b64decode(envelope["nonce"]), base64.b64decode(envelope["ciphertext"]), service.canonical(metadata))
            self.assertEqual(self.request(path=path, body={"purpose": purpose}, identity="other")[0], 403 if identity == "manager" else 404)
            wrong_role = "manager" if identity == "runtime" else "runtime"
            self.assertEqual(self.request(path=path, body={"purpose": purpose}, identity=wrong_role)[0], 403)

    def test_disk_failure_is_generic_nonack(self):
        with patch.object(self.store, "store", side_effect=sqlite3.OperationalError("secret-disk-detail")):
            status, body, _ = self.request(body=payload())
        self.assertEqual(status, 503)
        self.assertNotIn("stored", body)
        self.assertNotIn("secret-disk-detail", json.dumps(body))
        self.assertTrue(body["error"]["outcome_unknown"])


if __name__ == "__main__":
    unittest.main()
