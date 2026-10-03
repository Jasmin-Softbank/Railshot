import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { createAppServer } from '../src/server.js';
import { buildOpenStackInstaller } from '../src/installer.js';

test('OpenStack package contains the installer and companion deployment scripts', async () => {
  const installer = await buildOpenStackInstaller();
  assert.equal(installer.archive.subarray(0, 4).toString('hex'), '504b0304');
  assert.equal(createHash('sha256').update(installer.archive).digest('hex'), installer.sha256);
  assert.match(installer.script, /^#!\/usr\/bin\/env bash/);
  assert.ok(installer.archive.length > installer.script.length);
});

test('installer endpoints serve files without accepting Keystone credentials', async (t) => {
  const server = createAppServer({ product: {} });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  t.after(() => new Promise((resolve) => server.close(resolve)));
  const base = `http://127.0.0.1:${server.address().port}`;
  const metadata = await fetch(`${base}/api/v1/installers/openstack`);
  assert.equal(metadata.status, 200);
  const data = await metadata.json();
  assert.equal(data.bundle_url, '/api/v1/installers/openstack/bundle');
  assert.match(data.bundle_sha256, /^[a-f0-9]{64}$/);
  assert.match(data.install_sh, /install_payload\.py/);
  const archive = await fetch(`${base}${data.bundle_url}`);
  assert.equal(archive.status, 200);
  assert.equal(archive.headers.get('content-type'), 'application/zip');
  assert.equal(createHash('sha256').update(Buffer.from(await archive.arrayBuffer())).digest('hex'), data.bundle_sha256);
  const removed = await fetch(`${base}/api/v1/identities`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: '{}' });
  assert.equal(removed.status, 404);
});
