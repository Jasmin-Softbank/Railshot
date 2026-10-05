import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('backup_platform_state', Path(__file__).resolve().parents[1] / 'backup-platform-state.py')
backup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(backup)


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.root = self.home / 'platform'
        (self.root / 'state').mkdir(parents=True, mode=0o700)
        self.root.chmod(0o700)
        self.db = sqlite3.connect(self.root / 'state/dashboard.sqlite3')
        self.addCleanup(self.db.close)
        self.db.executescript('PRAGMA journal_mode=WAL; CREATE TABLE operations(id TEXT PRIMARY KEY, record TEXT);')
        self.db.execute('INSERT INTO operations VALUES (?, ?)', ('first', '{"status":"succeeded"}'))
        self.db.commit()
        for name, content in [('connections.key', b'k' * 32), ('first.source.json', b'[{"path":"index.html"}]'),
                              ('owner.json', b'{"pid":123}')]:
            (self.root / 'state' / name).write_bytes(content)
        (self.root / 'config').mkdir(mode=0o700)
        (self.root / 'config/executors.json').write_text('{"token":"fixture-private-value"}')
        for name in ['applications', 'provider-edges', 'reconciliations', 'tunnel-state']:
            (self.root / name).mkdir(mode=0o700)
            (self.root / name / 'receipt.json').write_text('{"status":"recorded"}')
        self.saved = self.home / 'backup'

    def test_online_wal_backup_and_isolated_restore_include_recovery_files(self):
        result = backup.backup(self.root, self.saved)
        self.assertEqual(result['databases'], 1)
        manifest = json.loads((self.saved / 'manifest.json').read_text())
        self.assertEqual(manifest['consistency'], 'online_per_database')
        self.assertNotIn('fixture-private-value', json.dumps(result))
        self.assertFalse((self.saved / 'state/owner.json').exists())
        self.assertFalse((self.saved / 'state/dashboard.sqlite3-wal').exists())
        self.db.execute('INSERT INTO operations VALUES (?, ?)', ('later', '{}'))
        self.db.commit()
        restored = self.home / 'restore'
        self.assertEqual(backup.restore_drill(self.saved, restored)['status'], 'restore_drill_verified')
        with sqlite3.connect(restored / 'state/dashboard.sqlite3') as db:
            self.assertEqual(db.execute('SELECT id FROM operations').fetchall(), [('first',)])
        self.assertEqual((restored / 'state/connections.key').read_bytes(), b'k' * 32)
        self.assertEqual((restored / 'state/first.source.json').read_bytes(), (self.root / 'state/first.source.json').read_bytes())
        self.assertEqual((restored / 'config/executors.json').stat().st_mode & 0o777, 0o600)
        for name in ['applications', 'provider-edges', 'reconciliations', 'tunnel-state']:
            self.assertEqual((restored / name / 'receipt.json').read_bytes(), (self.root / name / 'receipt.json').read_bytes())
        with self.assertRaisesRegex(backup.Blocked, 'NEW_DESTINATION_REQUIRED'):
            backup.restore_drill(self.saved, self.root)

    def test_tampered_backup_and_symlinks_are_rejected_before_restore(self):
        backup.backup(self.root, self.saved)
        (self.saved / 'state/connections.key').write_bytes(b'changed')
        with self.assertRaisesRegex(backup.Blocked, 'BACKUP_CHECKSUM_FAILED'):
            backup.restore_drill(self.saved, self.home / 'restore')
        self.assertFalse((self.home / 'restore').exists())
        (self.root / 'state/external').symlink_to(self.home / 'outside')
        with self.assertRaisesRegex(backup.Blocked, 'UNSAFE_STATE_FILE'):
            backup.backup(self.root, self.home / 'another-backup')

    def test_invalid_database_header_does_not_produce_a_successful_backup(self):
        self.db.close()
        (self.root / 'state/dashboard.sqlite3').write_bytes(b'not a database')
        with self.assertRaisesRegex(backup.Blocked, 'DATABASE_HEADER_INVALID'):
            backup.backup(self.root, self.saved)
        self.assertFalse(self.saved.exists())

    def test_pvc_root_2770_is_accepted_without_chmod_and_hourly_runner_rejects_drift(self):
        info = SimpleNamespace(st_mode=stat.S_IFDIR | 0o2770, st_uid=1000, st_gid=1000)
        with patch.object(backup.Path, 'stat', return_value=info), patch.object(backup.os, 'geteuid', return_value=0):
            self.assertEqual(backup.private_directory(self.root), self.root)
        # Execute the runner's actual preflight, stopping before host /run writes.
        runner = Path(__file__).resolve().parents[1] / 'backup-platform-state.sh'
        preflight = runner.read_text().split('umask 077', 1)[0]
        shell = 'id() { printf "0\\n"; }; stat() { printf "%s\\n" "$ROOT_STAT"; };\n' + preflight + '\nprintf "guard-passed\\n"\n'
        before = {path: (path.stat().st_mode, path.stat().st_uid, path.stat().st_gid)
                  for path in [self.root, *(self.root / 'state').iterdir()]}
        for value in ('1000:1000:2770', '1000:1000:700', '1000:1000:770', '1000:1000:2777', '0:1000:2770', '1000:0:2770'):
            with self.subTest(owner_mode=value):
                result = subprocess.run(['bash', '-c', shell, 'backup', str(self.root), 'railshot-platform-recovery-123456789012'],
                                        env={**os.environ, 'ROOT_STAT': value}, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0 if value == '1000:1000:2770' else 1)
                self.assertEqual(result.stdout.strip(), 'guard-passed' if value == '1000:1000:2770'
                                 else '{"status":"failed","code":"PVC_ROOT_PERMISSIONS_INVALID"}')
        self.assertEqual(before, {path: (path.stat().st_mode, path.stat().st_uid, path.stat().st_gid) for path in before})


if __name__ == '__main__':
    unittest.main()
