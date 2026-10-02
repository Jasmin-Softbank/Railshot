import assert from 'node:assert/strict';
import { once } from 'node:events';
import { readFile } from 'node:fs/promises';
import { createServer } from 'node:http';
import test from 'node:test';
import { chromium } from 'playwright';

test('environment form keeps one plan through DB and app deployment and preserves environment-only and existing targets', { timeout: 60000 }, async (t) => {
  const plans = [], deployments = [], environments = [], errors = [];
  const profiles = [
    { id: 'ha-profile', label: 'AWS with PostgreSQL', provider: 'aws', supported: true, deployment_supported: true,
      target_id: 'ha-runtime', application_name: 'database-app', database: { mode: 'patroni', required: true, database_nodes: 2, dcs_voters: 3, proxy_nodes: 1 } },
    { id: 'plain-profile', label: 'AWS runtime', provider: 'aws', supported: true, deployment_supported: true,
      target_id: 'plain-runtime', application_name: 'plain-app', database: null },
    { id: 'optional-profile', label: 'Optional database environment', provider: 'aws', supported: true, deployment_supported: false,
      target_id: 'optional-runtime', application_name: null, database: { mode: 'patroni', required: false, database_nodes: 2, dcs_voters: 3, proxy_nodes: 1 } },
  ];
  let planMode = 'valid';
  const respond = (response, status, body, location) => {
    response.writeHead(status, { 'content-type': 'application/json', ...(location ? { location } : {}) });
    response.end(JSON.stringify(body));
  };
  const server = createServer(async (request, response) => {
    const path = new URL(request.url, 'http://localhost').pathname;
    const body = [];
    for await (const chunk of request) body.push(chunk);
    const bytes = Buffer.concat(body);
    if (path === '/api/v1/targets') return respond(response, 200, { items: [{ id: 'ready-runtime', label: 'Existing runtime',
      capabilities: { ci_submission: true, application_deployment: true } }] });
    if (path === '/api/v1/profiles') return respond(response, 200, { items: profiles });
    if (path === '/api/v1/plans') {
      const input = JSON.parse(bytes); plans.push(input);
      const result = { id: `plan-${plans.length}`, ...input, executable: true,
        expires_at: new Date(Date.now() + (planMode === 'expired' ? -1000 : 900000)).toISOString() };
      if (planMode === 'wrong-name') result.name = 'another-app';
      return respond(response, 200, result);
    }
    if (path === '/api/v1/deployments' || path === '/api/v1/builds') {
      const form = await new Request('http://localhost', { method: 'POST', body: bytes, headers: request.headers }).formData();
      const input = Object.fromEntries(form.entries()); deployments.push({ path, input });
      const id = `execution-${deployments.length}`;
      return respond(response, 202, { id, status: 'accepted' }, `${path}/${id}`);
    }
    if (/^\/api\/v1\/(deployments|builds)\/execution-\d+$/.test(path)) {
      const input = deployments[Number(path.split('-').at(-1)) - 1].input;
      return respond(response, 200, { id: path.split('/').at(-1), target_id: input.target_id, app: input.app,
        status: path.includes('/builds/') ? 'published' : 'succeeded', steps: [] });
    }
    if (path === '/api/v1/environments') {
      environments.push(JSON.parse(bytes));
      return respond(response, 202, { id: 'environment-1', status: 'accepted' }, '/api/v1/environments/environment-1');
    }
    if (path === '/api/v1/environments/environment-1') return respond(response, 200, { id: 'environment-1',
      status: 'succeeded', runtime_target_id: 'ha-runtime', deployment_supported: true });
    const files = { '/': ['index.html', 'text/html'], '/app.js': ['app.js', 'text/javascript'], '/styles.css': ['styles.css', 'text/css'] };
    if (!files[path]) return respond(response, 404, { error: 'fixture path unavailable' });
    response.writeHead(200, { 'content-type': files[path][1] });
    response.end(await readFile(new URL('../../apps/dashboard/' + files[path][0], import.meta.url)));
  });
  server.listen(0, '127.0.0.1'); await once(server, 'listening');
  const browser = await chromium.launch({ executablePath: process.env.CHROME_EXECUTABLE || undefined });
  t.after(async () => {
    await browser.close();
    await new Promise((resolve) => { server.close(resolve); server.closeAllConnections(); });
  });
  const page = await browser.newPage(); page.setDefaultTimeout(10000);
  await page.addInitScript(() => {
    const schedule = window.setTimeout.bind(window);
    window.railshotTestTimeouts = [];
    window.setTimeout = (callback, delay, ...args) => {
      window.railshotTestTimeouts.push(delay);
      return schedule(callback, delay, ...args);
    };
  });
  const origin = `http://127.0.0.1:${server.address().port}`;
  page.on('pageerror', (error) => errors.push(error.message));
  await page.route('**/*', (route) => new URL(route.request().url()).origin === origin ? route.continue() : route.abort());
  await page.goto(origin);
  await page.waitForFunction(() => document.querySelector('#target option[value="profile:ha-profile"]'));
  await page.locator('#repository-url').fill('https://github.com/example/other-source-name');
  await page.locator('#target').selectOption('profile:ha-profile');
  assert.equal(await page.locator('#app-name').inputValue(), 'database-app');
  assert.equal(await page.locator('#app-name').getAttribute('readonly'), '');
  assert.equal(await page.locator('#deployment-database').inputValue(), 'patroni');
  assert.equal(await page.locator('#deployment-database option[value="none"]').isDisabled(), true);
  assert.match(await page.locator('#deployment-database-note').innerText(), /PostgreSQL 2대.*DCS.*3대.*프록시 1대/);
  const review = () => page.locator('#deploy-form button[type="submit"]').click();
  await review();
  await page.waitForFunction(() => !document.querySelector('#review-panel').hidden);
  assert.deepEqual(plans[0], { name: 'database-app', runtime: { profile_id: 'ha-profile', node_count: 1 },
    database: { mode: 'patroni', placements: [{ profile_id: 'ha-profile', database_nodes: 2, dcs_voters: 3, proxy_nodes: 1 }] } });
  assert.equal(deployments.length, 0, 'review must not create resources or submit source');
  await page.locator('#deploy-button').click();
  await page.waitForFunction(() => document.querySelector('#run-state').textContent === '앱 배포 완료');
  assert.deepEqual(deployments[0], { path: '/api/v1/deployments', input: { app: 'database-app', target_id: 'ha-runtime',
    plan_id: 'plan-1', repository_url: 'https://github.com/example/other-source-name' } });
  assert.equal(environments.length, 0, 'combined deployment delegates environment execution to the product API');
  const timeouts = await page.evaluate(() => window.railshotTestTimeouts);
  assert.ok(timeouts.includes(600000), 'environment planning gets ten minutes');
  assert.ok(timeouts.includes(120000), 'deployment submission retains its two-minute deadline');
  assert.ok(timeouts.includes(15000), 'GET requests retain their fifteen-second deadline');

  await page.locator('#target').selectOption('profile:plain-profile');
  assert.equal(await page.locator('#deployment-database').inputValue(), 'none');
  assert.equal(await page.locator('#deployment-database option[value="patroni"]').isDisabled(), true);
  await review();
  await page.waitForFunction(() => !document.querySelector('#review-panel').hidden);
  assert.deepEqual(plans[1].database, { mode: 'none' });
  await page.locator('#deploy-button').click();
  await page.waitForFunction(() => document.querySelector('#run-meta').textContent.includes('execution-2') && document.querySelector('#run-state').textContent === '앱 배포 완료');
  assert.equal(deployments[1].input.plan_id, 'plan-2');
  assert.equal(deployments[1].input.app, 'plain-app');

  await page.locator('#environment-panel summary').click();
  await page.locator('#profile').selectOption('ha-profile');
  assert.equal(await page.locator('#environment-database option[value="none"]').isDisabled(), true);
  await page.locator('#plan-button').click();
  await page.waitForFunction(() => !document.querySelector('#environment-button').disabled);
  assert.equal(plans[2].database.mode, 'patroni');
  assert.match(await page.locator('#environment-message').innerText(), /PostgreSQL 2대/);
  await page.locator('#environment-button').click();
  await page.waitForFunction(() => document.querySelector('#environment-message').textContent.includes('runtime 준비 완료'));
  assert.deepEqual(environments, [{ plan_id: 'plan-3' }]);
  assert.equal(deployments.length, 2);
  await page.locator('#profile').selectOption('optional-profile');
  assert.equal(await page.locator('#environment-database option[value="none"]').isDisabled(), false);
  await page.locator('#environment-database').selectOption('none');
  assert.equal(await page.locator('#environment-database').inputValue(), 'none');

  await page.locator('#target').selectOption('ready-runtime');
  await page.locator('#operation').selectOption('builds');
  await page.locator('#app-name').fill('existing-app');
  await review(); await page.locator('#deploy-button').click();
  await page.waitForFunction(() => document.querySelector('#run-state').textContent === '이미지 게시 완료');
  assert.equal(plans.length, 3);
  assert.equal(deployments[2].input.plan_id, undefined);
  assert.equal(deployments[2].path, '/api/v1/builds');

  await page.locator('#target').selectOption('profile:ha-profile');
  planMode = 'wrong-name';
  await review();
  await page.waitForFunction(() => document.querySelector('#form-error').textContent.includes('일치하지 않습니다'));
  assert.equal(await page.locator('#deploy-button').isDisabled(), true);
  planMode = 'expired';
  await review();
  await page.waitForFunction(() => document.querySelector('#form-error').textContent.includes('만료'));
  assert.equal(await page.locator('#deploy-button').isDisabled(), true);
  assert.equal(deployments.length, 3);
  assert.deepEqual(errors, []);
});
