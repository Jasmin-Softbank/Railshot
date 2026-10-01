import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from publication import prepare


class PublicationTest(unittest.TestCase):
    def test_handoff_preserves_publisher_bytes_and_producer_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = root / 'bundle'; bundle.mkdir()
            inputs = {'jasmin.yaml': b'app: my-app\n', 'verdict.json': b'{"ok": true}\n',
                      'manifest.json': b'{"version": 1}\n'}
            for name, content in inputs.items():
                (bundle / name).write_bytes(content)
            images = root / 'images.json'; images.write_bytes(b'{"web": "synthetic-digest"}\n')
            inputs['images.json'] = images.read_bytes()
            env = {'SOURCE_COMMIT': 'a' * 40, 'GITHUB_SHA': 'a' * 40, 'TARGET_ID': 'aws-demo',
                   'TENANT': 'demo', 'APP': 'my-app', 'GITHUB_RUN_ID': '123',
                   'GITHUB_RUN_ATTEMPT': '2', 'BUNDLE_ARTIFACT_ID': '777'}
            output = root / 'published'
            prepare(bundle, images, output, env)
            receipt = json.loads((output / 'handoff.json').read_text())
            self.assertEqual(receipt['status'], 'published')
            self.assertEqual(receipt['producer_attempt'], 2)
            self.assertEqual(receipt['bundle_artifact_id'], 777)
            self.assertEqual(receipt['source_commit'], env['SOURCE_COMMIT'])
            self.assertEqual(receipt['target_id'], 'aws-demo')
            self.assertEqual({p.name for p in output.iterdir()}, set(inputs) | {'handoff.json'})
            for name, content in inputs.items():
                self.assertEqual((output / name).read_bytes(), content)
                self.assertEqual(receipt['files'][name], hashlib.sha256(content).hexdigest())
            with self.assertRaises(FileExistsError):
                prepare(bundle, images, output, env)
            with self.assertRaisesRegex(ValueError, 'source commit mismatch'):
                prepare(bundle, images, root / 'bad', {**env, 'GITHUB_SHA': 'b' * 40})
            self.assertFalse((root / 'bad').exists())


if __name__ == '__main__':
    unittest.main()
