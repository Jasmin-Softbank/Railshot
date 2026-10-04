import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location('provision_helper', ROOT / 'deployment/scripts/control-provision-helper.py')
helper = importlib.util.module_from_spec(spec); spec.loader.exec_module(helper)


class HelperTests(unittest.TestCase):
    def test_privilege_drop_precedes_all_consumer_tree_access_and_secrets_never_enter_argv(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory); source = home / 'root'; source.mkdir(mode=0o700)
            output = home / 'consumer'; output.mkdir(mode=0o700)
            config = home / 'config.json'; config.write_text(json.dumps({'version': 1, 'caller_uid': os.getuid(),
                'caller_gid': os.getgid(), 'root_state_dir': str(source), 'output_root': str(output), 'provision_config': str(home / 'operator.json')}))
            config.chmod(0o600); privilege = [0]; order = []; original = Path.lstat
            def lstat(path):
                info = original(path)
                if str(path).startswith(str(source)):
                    return SimpleNamespace(st_uid=0, st_mode=info.st_mode)
                if str(path).startswith(str(output / 'env-one')):
                    self.assertEqual(privilege[0], os.getuid(), 'consumer paths must never be read as root')
                return info
            def execute(argv, **kwargs):
                self.assertEqual(privilege[0], 0); self.assertNotIn('secret-token', str(argv))
                target = source / 'env-one'; target.mkdir(mode=0o700)
                (target / 'transit-profile.json').write_text(json.dumps({'environment_id': 'env-one', 'seal_env_file': str(target / 'seal.json')}))
                (target / 'seal.json').write_text('{"VAULT_TOKEN":"secret-token"}')
                return subprocess.CompletedProcess(argv, 0, json.dumps({'environment_id': 'env-one', 'status': 'issued', 'profile_path': str(target / 'transit-profile.json')}), '')
            with patch.object(helper, 'CONFIG', config), patch.object(helper, 'owned', return_value=SimpleNamespace(st_mode=0o100600)), \
                 patch.object(helper.os, 'geteuid', side_effect=lambda: privilege[0]), patch.dict(os.environ, {'SUDO_UID': str(os.getuid())}), \
                 patch.object(helper.os, 'setgroups', side_effect=lambda _: order.append('groups')), \
                 patch.object(helper.os, 'setgid', side_effect=lambda _: order.append('gid')), \
                 patch.object(helper.os, 'setuid', side_effect=lambda uid: (order.append('uid'), privilege.__setitem__(0, uid))), \
                 patch.object(Path, 'lstat', lstat):
                result = helper.provision('env-one', execute=execute)
            self.assertEqual(order, ['groups', 'gid', 'uid'])
            self.assertEqual(Path(result['profile_path']).parent, output / 'env-one')
            self.assertNotIn(str(source), Path(result['profile_path']).read_text())

    def test_unregistered_caller_or_path_input_never_executes(self):
        for value in ('../etc', 'env;id', '/tmp/env'):
            with patch.object(helper.os, 'geteuid', return_value=0), self.assertRaises(ValueError):
                helper.provision(value, execute=lambda *_: self.fail('must not execute'))

    def test_privileged_reference_rejects_symlink_and_writable_ancestor(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'source'; source.write_text('fixture')
            link = Path(directory) / 'link'; link.symlink_to(source)
            with self.assertRaises(ValueError): helper.owned(link)
