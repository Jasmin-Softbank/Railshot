"""The standard calculator path needs no generated application code."""
import json
import tempfile
import unittest
from pathlib import Path

from native_packaging import prepare_packaging


class NativePackagingTest(unittest.TestCase):
    def test_static_calculator_and_unsupported_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            ws = Path(directory)
            package = {'scripts': {'build': 'tsc && vite build'}, 'devDependencies': {'vite': '^4.4.5'}}
            (ws / 'package.json').write_text(json.dumps(package))
            (ws / 'index.html').write_text('<div id="root"></div>')
            (ws / 'yarn.lock').write_text('# yarn lockfile v1\n')
            original = {p.name: p.read_bytes() for p in ws.iterdir()}
            for invalid in ([], {'scripts': []}, {'packageManager': []}):
                (ws / 'package.json').write_text(json.dumps(invalid))
                self.assertEqual('unsupported', prepare_packaging(ws, 'calculator')['status'])
            (ws / 'package.json').write_bytes(original['package.json'])
            # Memos has a Go server; never misclassify its Vite frontend as the app.
            (ws / 'go.mod').write_text('module github.com/usememos/memos\n')
            self.assertEqual('unchanged', prepare_packaging(ws, 'memos')['status'])
            (ws / 'go.mod').unlink()
            (ws / 'vite.config.ts').write_text('export default {build: {outDir: "other"}}')
            self.assertEqual('unsupported', prepare_packaging(ws, 'calculator')['status'])
            (ws / 'vite.config.ts').unlink()
            result = prepare_packaging(ws, 'calculator')
            self.assertEqual('prepared', result['status'])
            self.assertIn('yarn install --frozen-lockfile', (ws / 'Dockerfile').read_text())
            self.assertIn('USER 65532', (ws / 'Dockerfile').read_text())
            for name, content in original.items():
                self.assertEqual(content, (ws / name).read_bytes())
            self.assertEqual('unchanged', prepare_packaging(ws, 'calculator')['status'])


if __name__ == '__main__':
    unittest.main()
