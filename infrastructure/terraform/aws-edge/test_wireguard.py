"""WireGuard config safety with synthetic keys only; no wg, SSH or network calls."""
import base64
import json
from pathlib import Path
import stat
import tempfile
import unittest

from render_wireguard import prepare, render

FAKE_PRIVATE = base64.b64encode(b'\x01' * 32).decode()
FAKE_PUBLIC = base64.b64encode(b'\x02' * 32).decode()


def fixture():
    return {'address': '10.200.0.1/30', 'peer': {'public_key': FAKE_PUBLIC,
            'endpoint': '8.8.8.8:51820', 'allowed_ips': ['10.200.0.2/32', '10.66.0.2/32']}}


class WireGuardTest(unittest.TestCase):
    def test_atomic_private_file_and_check_receipt_never_expose_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            private, public, output = root/'private.key', root/'peer.json', root/'config'/'wg-railshot.conf'
            private.write_text(FAKE_PRIVATE+'\n'); private.chmod(0o600)
            public.write_text(json.dumps(fixture()))
            result = prepare(public, private, output, check=True)
            self.assertFalse(output.exists())
            self.assertFalse(result['installed'])
            with self.assertRaisesRegex(ValueError, 'overwrite'):
                prepare(public, private, private)
            result = prepare(public, private, output)
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(output.parent.stat().st_mode), 0o700)
            self.assertEqual(output.read_text(), render(fixture(), FAKE_PRIVATE))
            self.assertNotIn(FAKE_PRIVATE, json.dumps(result))
            self.assertNotIn('PostUp', output.read_text())
            self.assertFalse(result['installed'])
            private.chmod(0o644)
            with self.assertRaisesRegex(ValueError, '0600'):
                prepare(public, private, output)

    def test_public_default_self_routes_and_injected_config_are_rejected(self):
        for allowed in (['0.0.0.0/0'], ['8.8.8.8/32'], ['169.254.169.254/32'], ['10.200.0.0/24'], ['10.66.0.2/24']):
            value = fixture(); value['peer']['allowed_ips'] = allowed
            with self.subTest(allowed=allowed), self.assertRaises(ValueError):
                render(value, FAKE_PRIVATE)
        for endpoint in ('host.example.com:51820', '8.8.8.8:22', '8.8.8.8:51820\nPostUp=bad', '10.1.1.1:51820', '224.0.0.1:51820'):
            value = fixture(); value['peer']['endpoint'] = endpoint
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                render(value, FAKE_PRIVATE)
        value = fixture(); value['PostUp'] = 'injected'
        with self.assertRaises(ValueError): render(value, FAKE_PRIVATE)
        with self.assertRaises(ValueError): render(fixture(), 'bad-key')

    def test_symlink_key_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root/'key').write_text(FAKE_PRIVATE); (root/'key').chmod(0o600)
            (root/'link').symlink_to(root/'key')
            with self.assertRaisesRegex(ValueError, 'non-symlink'):
                prepare(root/'unused.json', root/'link', root/'output.conf')


if __name__ == '__main__':
    unittest.main(verbosity=2)
