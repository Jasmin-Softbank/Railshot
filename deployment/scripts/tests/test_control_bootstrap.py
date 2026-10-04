import base64
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'control-vault-bootstrap.sh'


class BootstrapTests(unittest.TestCase):
    def run_case(self, encoded=False, duplicate=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bin_dir = root / 'bin'
            bin_dir.mkdir()
            gpg = bin_dir / 'gpg'
            gpg.write_text('#!/usr/bin/env python3\nimport sys,hashlib\nraw=sys.stdin.buffer.read()\nassert raw.startswith(bytes([0x99,0xff]))\nprint("pub:::::::::")\nprint("fpr:::::::::"+hashlib.sha256(raw).hexdigest()+":")\n')
            gpg.chmod(0o700)
            vault = bin_dir / 'vault'
            vault.write_text('#!/usr/bin/env python3\nimport sys,json,base64,os\nopen(os.environ["TEST_ARG_LOG"],"a").write(json.dumps(sys.argv[1:])+"\\n")\nif sys.argv[1]=="status":\n print(json.dumps({"initialized":False,"sealed":True}));sys.exit(2)\nprint(json.dumps({"unseal_keys_b64":[base64.b64encode(b"encrypted"*30).decode()]*5,"root_token":base64.b64encode(b"encrypted-root"*30).decode()}))\n')
            vault.chmod(0o700)
            ca = root / 'ca'; ca.write_text('synthetic-public-ca'); ca.chmod(0o600)
            paths = []
            for index in range(6):
                public = root / f'public-{index}.pgp'
                raw = b'\x99\xffsynthetic-public' + bytes([0 if duplicate else index])
                public.write_bytes(base64.b64encode(raw) if encoded else raw)
                public.chmod(0o600)
                paths.append(public)
            recipients = root / 'recipients'
            recipients.write_text('\n'.join(str(p) for p in paths[:5])+'\n'); recipients.chmod(0o600)
            log = root / 'argv.jsonl'
            result = subprocess.run(['bash', str(SCRIPT), 'https://vault.invalid', str(ca), str(recipients), str(paths[5])], capture_output=True, text=True,
                                    env={**os.environ,'PATH':str(bin_dir)+':'+os.environ['PATH'],'TEST_ARG_LOG':str(log)})
            if duplicate:
                self.assertNotEqual(result.returncode,0)
                self.assertFalse(log.exists())
            else:
                self.assertEqual(result.returncode,0,result.stderr)
                self.assertEqual(len(json.loads(result.stdout)['unseal_keys_b64']),5)
                args=json.loads(log.read_text().splitlines()[-1])
                self.assertIn('-root-token-pgp-key='+str(paths[5]),args)

    def test_binary_public_keys_and_root_recipient_path(self):
        self.run_case()

    def test_base64_public_keys(self):
        self.run_case(encoded=True)

    def test_duplicate_fingerprint_rejected_before_vault(self):
        self.run_case(duplicate=True)


if __name__ == '__main__':
    unittest.main()
