import { createHash } from 'node:crypto';
import { lstat, readFile, readdir } from 'node:fs/promises';
import { dirname, join, relative, sep } from 'node:path';
import { fileURLToPath } from 'node:url';
import yazl from 'yazl';

const repository = join(dirname(fileURLToPath(import.meta.url)), '..', '..', '..');
const directories = [
  'deployment/bootstrap', 'deployment/scripts', 'deployment/cilium',
  'deployment/airgap', 'deployment/manifests', 'deployment/cloudflared',
  'infrastructure/providers/openstack', 'apps/agent',
];
const excluded = new Set(['tests', 'docs', 'results', '__pycache__', '.pytest_cache', '.venv', '.gitkeep']);
const required = new Set([
  'deployment/bootstrap/install.sh',
  'deployment/bootstrap/client_setup/main.py',
  'deployment/bootstrap/preflight.sh',
  'deployment/bootstrap/install-k3s.sh',
  'deployment/scripts/deploy.sh',
  'deployment/scripts/runtime.py',
  'deployment/scripts/engine.py',
  'deployment/cilium/install.sh',
  'deployment/airgap/scripts/bundle.py',
  'deployment/manifests/workload.json.template',
  'infrastructure/providers/openstack/cli.py',
]);

async function filesIn(directory) {
  const files = [];
  for (const entry of await readdir(directory, { withFileTypes: true })) {
    if (excluded.has(entry.name) || entry.name.endsWith('.pyc') || entry.name.startsWith('test_') || entry.name.endsWith('.md')) continue;
    const path = join(directory, entry.name);
    if (entry.isSymbolicLink()) throw new Error('Installer source must not contain symbolic links');
    if (entry.isDirectory()) files.push(...await filesIn(path));
    else if (entry.isFile()) files.push(path);
    else throw new Error('Installer source must contain regular files only');
  }
  return files;
}

export async function buildOpenStackInstaller(root = repository) {
  const paths = (await Promise.all(directories.map((name) => filesIn(join(root, name)))))
    .flat().map((path) => ({ path, name: relative(root, path).split(sep).join('/') }))
    .sort((a, b) => a.name.localeCompare(b.name));
  const names = new Set(paths.map((item) => item.name));
  if ([...required].some((name) => !names.has(name))) throw new Error('OpenStack installer package is incomplete');
  const zip = new yazl.ZipFile();
  let total = 0;
  for (const file of paths) {
    const metadata = await lstat(file.path);
    if (!metadata.isFile() || metadata.nlink !== 1) throw new Error('Unsafe installer source');
    const bytes = await readFile(file.path);
    total += bytes.length;
    if (total > 10 * 1024 * 1024) throw new Error('OpenStack installer package is too large');
    zip.addBuffer(bytes, file.name, { mtime: new Date('1980-01-01T00:00:00Z'), mode: 0o644 });
  }
  zip.end();
  const chunks = [];
  for await (const chunk of zip.outputStream) chunks.push(chunk);
  const archive = Buffer.concat(chunks);
  return { archive, sha256: createHash('sha256').update(archive).digest('hex'),
    script: await readFile(join(root, 'deployment/bootstrap/install.sh'), 'utf8') };
}
