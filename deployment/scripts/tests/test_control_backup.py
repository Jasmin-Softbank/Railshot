import base64
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import control_backup as backup
import recovery_service as recovery


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.keys = {"old": os.urandom(32), "new": os.urandom(32)}
        self.keyring = self.root / "keyring.json"
        self.keyring.write_text(json.dumps({"version": 1, "active_key_id": "new", "keys": {k: base64.b64encode(v).decode() for k, v in self.keys.items()}}))
        self.keyring.chmod(0o600)
        self.data = self.root / "data"
        self.data.mkdir(mode=0o700)
        self.custody = recovery.Custody(self.data, "old", self.keys, owner=os.getuid())
        payload = {"version": 1, "environment_id": "env-a", "kind": "vault-delivery-approle", "operation_id": "first", "material": {"role_id": "synthetic-role", "secret_id": "synthetic-secret"}}
        self.custody.store(payload)
        self.custody.active_key_id = "new"
        self.custody.store({**payload, "operation_id": "second"})
        self.snapshot = self.root / "snapshot"
        state = b"synthetic-raft-encrypted-state"
        metadata = json.dumps({"ID": "snapshot-1", "Index": 10, "Term": 1, "Size": len(state)}).encode()
        entries = {"meta.json": metadata, "state.bin": state}
        entries["SHA256SUMS"] = "".join(hashlib.sha256(data).hexdigest() + "  " + name + "\n" for name, data in entries.items()).encode()
        with tarfile.open(self.snapshot, "w:gz") as archive:
            for name, data in entries.items():
                info = tarfile.TarInfo(name)
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
        self.snapshot.chmod(0o600)
        self.reference = self.root / "reference.json"
        self.reference.write_text(json.dumps({"version": 1, "vault_version": "2.1.1", "transit_key_ids": ["railshot-env-a"]}))
        self.reference.chmod(0o600)
        read, directory = recovery.private_read, recovery.private_directory
        self.patches = [patch.object(recovery, "private_read", side_effect=lambda p: read(p, owner=os.getuid())),
                        patch.object(recovery, "private_directory", side_effect=lambda p: directory(p, owner=os.getuid())),
                        patch.object(backup, "private_read", side_effect=lambda p: read(p, owner=os.getuid())),
                        patch.object(backup, "private_directory", side_effect=lambda p: directory(p, owner=os.getuid())),
                        patch.object(backup, "safe_input")]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.temp.cleanup()

    def bundle(self):
        return backup.backup(self.snapshot, self.data / "custody.sqlite3", self.keyring, self.reference, self.root / "bundle")

    def test_all_referenced_keys_are_inventoried_and_required_on_restore(self):
        self.assertEqual(self.bundle()["custody_key_ids"], ["new", "old"])
        self.assertEqual(backup.verify_bundle(self.root / "bundle", self.keyring)["custody_key_ids"], ["new", "old"])
        restored = backup.restore(self.root / "bundle", self.keyring, self.root / "restored")
        self.assertTrue(restored["vault_restore_and_unseal_required"])
        wrong = self.root / "missing-old.json"
        wrong.write_text(json.dumps({"version": 1, "active_key_id": "new", "keys": {"new": base64.b64encode(self.keys["new"]).decode()}}))
        wrong.chmod(0o600)
        with self.assertRaises(KeyError):
            backup.verify_bundle(self.root / "bundle", wrong)

    def test_tamper_extra_file_and_arbitrary_sqlite_are_rejected(self):
        self.bundle()
        (self.root / "bundle/extra.key").write_text("unexpected")
        with self.assertRaises(ValueError):
            backup.verify_bundle(self.root / "bundle", self.keyring)
        (self.root / "bundle/extra.key").unlink()
        (self.root / "bundle/vault.snapshot").write_bytes(b"corrupt")
        with self.assertRaises(ValueError):
            backup.verify_bundle(self.root / "bundle", self.keyring)
        import sqlite3
        fake = self.root / "fake.sqlite3"
        db = sqlite3.connect(fake)
        db.execute("CREATE TABLE plaintext(secret TEXT)")
        db.close()
        with self.assertRaises(ValueError):
            backup.custody_verify(fake, self.keyring)

    def test_snapshot_path_traversal_and_existing_restore_directory_rejected(self):
        malicious = self.root / "bad.snapshot"
        with tarfile.open(malicious, "w:gz") as archive:
            info = tarfile.TarInfo("../../escape")
            info.size = 1
            archive.addfile(info, io.BytesIO(b"x"))
        with self.assertRaises(ValueError):
            backup.snapshot_verify(malicious)
        self.bundle()
        with self.assertRaises(FileExistsError):
            backup.restore(self.root / "bundle", self.keyring, self.root / "data")


if __name__ == "__main__":
    unittest.main()
