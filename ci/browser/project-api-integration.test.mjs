import assert from 'node:assert/strict';
import { once } from 'node:events';
import { mkdtemp, mkdir, realpath, rm, writeFile } from 'node:fs/promises';
import { createServer } from 'node:http';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { chromium } from 'playwright';
import { apiAccessConfig } from '../../apps/api/src/access.js';
import { createProductService } from '../../apps/api/src/product.js';
import { createAppServer } from '../../apps/api/src/server.js';

test('dashboard project editor uses the actual createAppServer API without exposing a secret value', { timeout: 45000 }, async (t) => {
  const home = await realpath(await mkdtemp(join(tmpdir(), 'railshot-project-browser-'))), directory = join(home, 'state'), keyFile = join(home, 'key.json');
  await mkdir(directory, { mode: 0o700 }); await writeFile(keyFile, JSON.stringify({ version: 1, active_key_id: 'test', keys: { test: 'c'.repeat(64) } }), { mode: 0o600 });
  const service = { targetId: 'env', allowTarget: () => {}, sourceFiles: async () => [{ path: 'app.js', content: Buffer.from('fixture') }], deploy: async () => ({ run_id: 1, source_commit: 'a'.repeat(40) }), status: async () => ({ state: 'published', status: 'completed', conclusion: 'success' }) };
  const product = await createProductService({ directory, projectKeyFile: keyFile, service, pollInterval: 1,
    applicationAdapter: { targets: { env: { provider: 'aws' } }, describe: (environment, app) => ({ id: `${environment}-${app}`, app, environment_target_id: environment, target_id: environment }), register: async (app) => ({ ...app, status: 'ready' }), deployPublished: async () => ({ cd: { state: 'deployed' }, public_http: { state: 'succeeded', verified_at: new Date().toISOString(), url: 'https://fixture.example' } }) },
    secretsAdapter: { supports: () => true, deliver: async (value) => ({ status: 'succeeded', observed_revision_id: value.revision_id, configuration: { project_id: value.project_id, binding_id: value.binding_id, revision_id: value.revision_id }, checks: { synchronized: true, workload_ready: true, service_ready: true } }) } });
  const server = createAppServer({ product, access: apiAccessConfig({ RAILSHOT_ALLOWED_HOSTS: '127.0.0.1' }) });
  server.listen(0, '127.0.0.1'); await once(server, 'listening');
  const browser = await chromium.launch({ executablePath: process.env.CHROME_EXECUTABLE || undefined }); const context = await browser.newContext(); const page = await context.newPage(); page.setDefaultTimeout(9000);
  t.after(async () => { await browser.close(); await new Promise((resolve) => { server.close(resolve); server.closeAllConnections(); }); await product.close(); await rm(home, { recursive: true, force: true }); });
  const origin = `http://127.0.0.1:${server.address().port}`, errors = []; page.on('pageerror', (error) => errors.push(error.message));
  await context.route('**/*', (route) => new URL(route.request().url()).origin === origin ? route.continue() : route.abort()); await page.goto(origin);
  await page.getByRole('button', { name: '프로젝트 환경변수' }).click(); await page.locator('#project-create-name').fill('browser project'); await page.getByRole('button', { name: '프로젝트 만들기' }).click();
  await page.waitForFunction(() => document.querySelector('#project-detail').hidden === false && document.querySelector('#project-detail-title').textContent.includes('browser project'));
  await page.locator('#variable-name').fill('BROWSER_TOKEN'); await page.locator('#variable-kind').selectOption('secret'); await page.locator('#variable-value').fill('browser-only-test-secret'); await page.locator('#variable-form').evaluate((form) => form.requestSubmit());
  await page.waitForFunction(() => document.querySelector('#project-variable-list').textContent.includes('BROWSER_TOKEN'));
  assert.doesNotMatch(await page.locator('#project-variable-list').innerText(), /browser-only-test-secret/); assert.equal(await page.locator('#variable-value').inputValue(), '');
  const variables = await page.evaluate(async () => { const projects = await (await fetch('/api/v1/projects?limit=100')).json(); const project = projects.items.find((item) => item.name === 'browser project'); return (await (await fetch(`/api/v1/projects/${project.id}/variables`)).json()); });
  assert.equal(variables.items.find((item) => item.name === 'BROWSER_TOKEN').has_value, true); assert.doesNotMatch(JSON.stringify(variables), /browser-only-test-secret/);
  assert.deepEqual(errors, []);
});
