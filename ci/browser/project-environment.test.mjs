import assert from 'node:assert/strict';
import { once } from 'node:events';
import { mkdir, readFile } from 'node:fs/promises';
import { createServer } from 'node:http';
import { join } from 'node:path';
import test from 'node:test';
import { chromium } from 'playwright';

test('project settings keep secrets metadata-only and require reviewed delivery and transfer actions', { timeout: 45000 }, async (t) => {
  const writes = [], project = { id: 'project-1', name: '결제 서비스', revision_id: 'revision-1', capabilities: { storage: true, delivery: true, transfer: true } }; let deliveryAttempts = 0, revisionNumber = 1;
  let variables = { project_id: 'project-1', revision_id: 'revision-1', capabilities: project.capabilities, blockers: [], bindings: [{ id: 'binding-1', environment_id: 'aws-runtime', application_id: 'app-1', status: 'ready', applied_revision_id: null }], items: [{ name: 'LOG_LEVEL', kind: 'plain', scope: 'common', environment_id: null, required: true, has_value: true, value: 'info', updated_at: '2026-10-04T00:00:00Z' }, { name: 'API_URL', kind: 'plain', scope: 'common', environment_id: null, required: false, has_value: true, value: 'https://common', updated_at: '2026-10-04T00:00:00Z' }, { name: 'API_URL', kind: 'plain', scope: 'environment', environment_id: 'aws-runtime', required: false, has_value: true, value: 'https://aws', updated_at: '2026-10-04T00:00:00Z' }, { name: 'API_URL', kind: 'plain', scope: 'environment', environment_id: 'gcp-runtime', required: false, has_value: true, value: 'https://gcp', updated_at: '2026-10-04T00:00:00Z' }, { name: 'API_TOKEN', kind: 'secret', scope: 'common', environment_id: null, required: true, has_value: true, updated_at: '2026-10-04T00:00:00Z' }] };
  const json = (response, status, body, location) => { response.writeHead(status, { 'content-type': 'application/json', ...(location ? { location } : {}) }); response.end(JSON.stringify(body)); };
  const server = createServer(async (request, response) => {
    const path = new URL(request.url, 'http://localhost').pathname, chunks = []; for await (const chunk of request) chunks.push(chunk);
    const input = chunks.length ? JSON.parse(Buffer.concat(chunks)) : null;
    if (path === '/api/v1/sessions') return json(response, 200, { expires_at: '2099-01-01T00:00:00Z' });
    if (path === '/api/v1/preferences') return json(response, 200, { view: 'deploy', environment: 'cloud', provider: 'aws' });
    if (path === '/api/v1/options') return json(response, 200, { items: [] });
    if (path === '/api/v1/profiles') return json(response, 200, { items: [] });
    if (path === '/api/v1/connections' || path === '/api/v1/applications' || path === '/api/v1/deployments') return json(response, 200, { items: [], next_marker: null });
    if (path === '/api/v1/targets') return json(response, 200, { items: [{ id: 'aws-runtime', provider: 'aws', label: 'AWS 운영' }, { id: 'gcp-runtime', provider: 'gcp', label: 'GCP 이전 환경' }], next_marker: null });
    if (path === '/api/v1/projects' && request.method === 'GET') return json(response, 200, { items: [project], next_marker: null });
    if (path === '/api/v1/projects/project-1') return json(response, 200, project);
    if (path === '/api/v1/projects/project-1/variables') return json(response, 200, variables);
    if (path === '/api/v1/projects/project-1/revisions') {
      writes.push({ path, input, headers: request.headers });
      let items = variables.items;
      for (const operation of input.operations) {
        const matches = (item) => item.name === operation.name && item.scope === operation.scope && (item.environment_id || null) === (operation.environment_id || null);
        if (operation.operation === 'delete') items = items.filter((item) => !matches(item));
        else { const saved = { name: operation.name, kind: operation.kind, scope: operation.scope, environment_id: operation.environment_id, required: operation.required, has_value: true, ...(operation.kind === 'plain' ? { value: operation.value } : {}), updated_at: '2026-10-04T00:01:00Z' }; items = [...items.filter((item) => !matches(item)), saved]; }
      }
      revisionNumber += 1; variables = { ...variables, revision_id: `revision-${revisionNumber}`, items };
      return json(response, 201, { id: variables.revision_id }, `/api/v1/projects/project-1/revisions/${variables.revision_id}`);
    }
    if (path === '/api/v1/projects/project-1/deliveries') {
      writes.push({ path, input, headers: request.headers }); deliveryAttempts += 1;
      if (deliveryAttempts === 1) { response.writeHead(202, { 'content-type': 'application/json', location: '/api/v1/projects/project-1/deliveries/delivery-1' }); return response.end('{'); }
      return json(response, 202, { resource_id: 'delivery-1', action: 'create', status: 'accepted', request_id: 'request-1' }, '/api/v1/projects/project-1/deliveries/delivery-1');
    }
    if (path === '/api/v1/projects/project-1/deliveries/delivery-1') return json(response, 200, { id: 'delivery-1', status: 'succeeded', stage: 'verified', observed_revision_id: 'revision-2' });
    if (path === '/api/v1/projects/project-1/transfers') { writes.push({ path, input, headers: request.headers }); const blockers = input.destination_environment_id === 'aws-runtime' ? ['SAME_ENVIRONMENT'] : []; return json(response, 201, { id: 'transfer-1', plan_hash: 'a'.repeat(64), expires_at: '2099-01-01T00:00:00Z', blockers, required_overrides: ['INTERNAL_URL'] }, '/api/v1/projects/project-1/transfers/transfer-1'); }
    if (path === '/api/v1/projects/project-1/transfers/transfer-1/actions') { writes.push({ path, input, headers: request.headers }); if (input.action === 'observe') return json(response, 200, { id: 'transfer-1', status: 'succeeded', stage: 'verified' }, '/api/v1/projects/project-1/transfers/transfer-1'); return json(response, 202, { resource_id: 'transfer-1', action: 'execute', status: 'accepted', request_id: 'request-2' }, '/api/v1/projects/project-1/transfers/transfer-1'); }
    if (path === '/api/v1/projects/project-1/transfers/transfer-1') return json(response, 200, { id: 'transfer-1', status: 'unknown', stage: 'reconcile' });
    const files = { '/': ['index.html', 'text/html'], '/app.js': ['app.js', 'text/javascript'], '/styles.css': ['styles.css', 'text/css'], '/contracts/application.mjs': ['../../contracts/application.mjs', 'text/javascript'] };
    if (!files[path]) return json(response, 404, { error: { message: 'fixture path unavailable' } });
    response.writeHead(200, { 'content-type': files[path][1] }); response.end(await readFile(new URL('../../apps/dashboard/' + files[path][0], import.meta.url)));
  });
  server.listen(0, '127.0.0.1'); await once(server, 'listening');
  const browser = await chromium.launch({ executablePath: process.env.CHROME_EXECUTABLE || undefined }); const page = await browser.newPage(); page.setDefaultTimeout(8000);
  t.after(async () => { await browser.close(); await new Promise((resolve) => { server.close(resolve); server.closeAllConnections(); }); });
  const origin = `http://127.0.0.1:${server.address().port}`; const errors = []; page.on('pageerror', (error) => errors.push(error.message));
  await page.route('**/*', (route) => new URL(route.request().url()).origin === origin ? route.continue() : route.abort()); await page.goto(origin);
  await page.getByRole('button', { name: '프로젝트 환경변수' }).click(); await page.getByRole('button', { name: '환경변수 관리' }).click();
  await page.waitForFunction(() => document.querySelector('#project-detail-title').textContent.includes('결제 서비스'));
  if (process.env.CI_OUTPUT_DIR) { await mkdir(process.env.CI_OUTPUT_DIR, { recursive: true }); await page.screenshot({ path: join(process.env.CI_OUTPUT_DIR, 'project-environment.png'), fullPage: true }); }
  assert.doesNotMatch(await page.locator('#project-variable-list').innerText(), /secret-value|token-value/i);
  assert.equal(await page.locator('#project-variable-list tr').filter({ hasText: 'API_URL' }).count(), 3);
  const gcpVariable = page.locator('#project-variable-list tr').filter({ hasText: 'API_URL' }).filter({ hasText: 'gcp-runtime' });
  await gcpVariable.getByRole('button', { name: '수정' }).click(); await page.locator('#variable-environment').selectOption('aws-runtime'); await page.locator('#variable-form').evaluate((form) => form.requestSubmit());
  await page.waitForFunction(() => document.querySelector('#project-revision-state').textContent.includes('revision-2'));
  assert.deepEqual(writes[0].input, { base_revision_id: 'revision-1', operations: [{ operation: 'delete', name: 'API_URL', scope: 'environment', environment_id: 'gcp-runtime' }, { operation: 'set', name: 'API_URL', kind: 'plain', scope: 'environment', environment_id: 'aws-runtime', required: false, value: 'https://gcp' }] });
  await page.locator('#variable-name').fill('PAYMENT_KEY'); await page.locator('#variable-kind').selectOption('secret'); await page.locator('#variable-value').fill('secret-value'); await page.locator('#variable-form').evaluate((form) => form.requestSubmit());
  await page.waitForFunction(() => document.querySelector('#project-revision-state').textContent.includes('revision-3'));
  assert.equal(await page.locator('#variable-value').inputValue(), '');
  assert.deepEqual(writes[1].input, { base_revision_id: 'revision-2', operations: [{ operation: 'set', name: 'PAYMENT_KEY', kind: 'secret', scope: 'common', environment_id: null, required: false, value: 'secret-value' }] });
  assert.match(writes[1].headers['idempotency-key'], /^[a-f0-9-]{36}$/);
  await page.locator('#transfer-review').click();
  await page.waitForFunction(() => document.querySelector('#project-confirm-dialog').open && document.querySelector('#project-confirm-description').textContent.includes('차단'));
  assert.match(await page.locator('#project-confirm-description').innerText(), /차단 항목/); assert.equal(await page.getByRole('button', { name: '이전 실행' }).isDisabled(), true);
  await page.getByRole('button', { name: '취소' }).click();
  await page.locator('#delivery-review').click(); await page.getByRole('button', { name: '적용 접수' }).click();
  await page.waitForFunction(() => !document.querySelector('#project-confirm-error').hidden);
  await page.getByRole('button', { name: '적용 접수' }).click();
  await page.waitForFunction(() => document.querySelector('#delivery-status').textContent.includes('완료'));
  assert.deepEqual(writes[3].input, { binding_id: 'binding-1', revision_id: 'revision-3' });
  assert.deepEqual(writes[4].input, writes[3].input); assert.equal(writes[4].headers['idempotency-key'], writes[3].headers['idempotency-key']);
  await page.locator('#transfer-environment').selectOption('gcp-runtime'); await page.locator('#transfer-review').click(); await page.getByRole('button', { name: '이전 실행' }).click();
  await page.waitForFunction(() => !document.querySelector('#transfer-observe').hidden); await page.locator('#transfer-observe').click();
  await page.waitForFunction(() => document.querySelector('#transfer-status').textContent.includes('완료'));
  assert.deepEqual(writes[5].input, { destination_environment_id: 'gcp-runtime', revision_id: 'revision-3', source_binding_id: 'binding-1' });
  assert.deepEqual(writes[6].input, { action: 'execute', plan_hash: 'a'.repeat(64) });
  assert.deepEqual(writes[7].input, { action: 'observe' }); assert.notEqual(writes[7].headers['idempotency-key'], writes[6].headers['idempotency-key']);
  await page.setViewportSize({ width: 390, height: 844 }); assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
  assert.deepEqual(errors, []);
});
