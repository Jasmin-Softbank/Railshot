import assert from 'node:assert/strict';
import { once } from 'node:events';
import { mkdir, mkdtemp, writeFile, rm, realpath } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { chromium } from 'playwright';
import { createAppServer } from '../../apps/api/src/server.js';
import { apiAccessConfig } from '../../apps/api/src/access.js';
import { archiveFromPath } from '../../apps/api/src/client.js';

async function start(t, options) {
  const stateDirectory = await realpath(await mkdtemp(join(tmpdir(), 'railshot-browser-')));
  const server = createAppServer({ stateDirectory, pollInterval: 10, ...options });
  await server.productReady;
  server.listen(0, '127.0.0.1'); await once(server, 'listening');
  const origin = `http://127.0.0.1:${server.address().port}`;
  if (options.access?.publicDemo) options.access.allowedOrigins.add(origin);
  const browser = await chromium.launch({ executablePath: process.env.CHROME_EXECUTABLE || undefined });
  const context = await browser.newContext({ serviceWorkers: 'block', viewport: { width: 1440, height: 1000 } });
  const page = await context.newPage(); page.setDefaultTimeout(10000);
  const errors = [], requests = [];
  page.on('pageerror', (error) => errors.push(error.message));
  await context.route('**/*', async (route) => {
    const request = route.request(), url = new URL(request.url());
    requests.push({ method: request.method(), path: url.pathname, authorization: request.headers().authorization });
    if (url.origin !== origin) { errors.push(`Unexpected remote request ${url.origin}`); await route.abort(); }
    else await route.continue();
  });
  await context.routeWebSocket('**/*', (socket) => { errors.push('Unexpected WebSocket'); socket.close(); });
  t.after(async () => {
    await browser.close();
    await new Promise((resolve) => { server.close(resolve); server.closeAllConnections(); });
    await (await server.productReady)?.close?.();
    await rm(stateDirectory, { recursive: true, force: true });
  });
  return { page, origin, errors, requests, stateDirectory };
}

test('dashboard loads without credentials, offers no login and blocks unconfigured execution', { timeout: 45000 }, async (t) => {
  const { page, origin, errors, requests } = await start(t, { service: null });
  await page.goto(origin);
  await page.waitForFunction(() => document.querySelector('#session-note').textContent.includes('까지'));
  assert.equal(await page.locator('#api-token').count(), 0);
  assert.equal(await page.locator('#deploy-view input[type="password"]').count(), 0);
  await page.locator('#deploy-form button[type="submit"]').click();
  assert.match(await page.locator('#form-error').innerText(), /소스를 선택/);
  await page.locator('#repository-url').fill('https://example.invalid/app');
  await page.locator('#deploy-form button[type="submit"]').click();
  assert.match(await page.locator('#form-error').innerText(), /GitHub 저장소 URL/);
  await page.locator('#repository-url').fill('https://github.com/example/demo');
  await page.locator('#deploy-form button[type="submit"]').click();
  assert.match(await page.locator('#form-error').innerText(), /인프라가 아직 연결되지/);
  assert.equal(await page.locator('#deploy-button').isDisabled(), true);
  await page.locator('[data-view="history"]').click();
  assert.equal(await page.locator('#history-view').isVisible(), true);
  await page.locator('[data-view="monitor"]').click();
  assert.equal(await page.locator('#monitor-view').isVisible(), true);
  await page.locator('[data-console="app"]').click();
  assert.match(await page.locator('#console-output').innerText(), /실행을 시작/);
  assert.equal(requests.some((request) => request.method === 'POST' && request.path !== '/api/v1/sessions'), false);
  assert.deepEqual(errors, []);
});

test('original dashboard cards submit three source types through backend selection and restore the saved record', { timeout: 90000 }, async (t) => {
  const submitted = [], cd = [];
  const commit = 'a'.repeat(40);
  const service = {
    targetId: 'demo-aws',
    deploy: async (input) => {
      submitted.push(input);
      return { run_id: submitted.length, app: input.app, target_id: 'demo-aws', source_commit: commit,
        state: 'queued', actions_url: `https://github.com/example/apps/actions/runs/${submitted.length}` };
    },
    status: async (id) => ({ run_id: Number(id), target_id: 'demo-aws', state: 'published', status: 'completed', conclusion: 'success',
      source_commit: commit, actions_url: `https://github.com/example/apps/actions/runs/${id}`, url: 'https://example.invalid/unverified',
      steps: [{ key: 'loop', status: 'completed', conclusion: 'success' }, { key: 'release', status: 'completed', conclusion: 'success' }],
      publication: { run_id: String(id), target_id: 'demo-aws', app: submitted[Number(id) - 1].app, source_commit: commit, artifact_id: 22, producer_attempt: 1 } }),
  };
  const { page, origin, errors, requests, stateDirectory } = await start(t, { service, target: { provider: 'aws' },
    access: apiAccessConfig({ RAILSHOT_PUBLIC_DEMO: '1', RAILSHOT_ALLOWED_HOSTS: '127.0.0.1', RAILSHOT_ALLOWED_ORIGINS: 'http://127.0.0.1' }),
    sourceLoader: async () => ({ files: [{ path: 'index.js', content: Buffer.from('source from github') }] }),
    deployPublished: async (request) => { cd.push(request); return { cd: { state: 'deployed', revision: 'b'.repeat(40), deployed: true },
      public_http: { state: 'succeeded', verified_at: new Date().toISOString(), url: 'https://demo.railshot.io' } }; },
  });
  await page.goto(origin);
  await page.waitForFunction(() => document.querySelector('#connection-status').textContent.includes('URL 확인'));
  assert.equal(await page.locator('#target, #operation, #app-name, #environment-panel').count(), 0, 'backend internals do not replace the original UI');
  await page.getByRole('radio', { name: /온프레미스/ }).check();
  await page.locator('#provider').selectOption('openstack');
  assert.match(await page.locator('#connection-status').innerText(), /OpenStack.*아직 연결되지/);
  await page.getByRole('radio', { name: /클라우드/ }).check();
  assert.equal(await page.locator('#provider-field').isVisible(), false);
  const review = () => page.locator('#deploy-form button[type="submit"]').click();
  const run = () => page.locator('#deploy-button').click();
  await page.locator('#repository-url').fill('https://github.com/example/browser-demo');
  await review();
  await page.getByRole('radio', { name: /온프레미스/ }).check();
  assert.equal(await page.locator('#review-panel').isVisible(), false, 'changing environment invalidates the reviewed request');
  await page.locator('#provider').selectOption('proxmox');
  await review();
  assert.match(await page.locator('#form-error').innerText(), /Proxmox.*아직 연결되지/);
  assert.equal(submitted.length, 0);
  await page.getByRole('radio', { name: /클라우드/ }).check();
  const output = process.env.CI_OUTPUT_DIR;
  if (output) {
    await mkdir(output, { recursive: true });
    await page.screenshot({ path: join(output, 'original-cards-desktop.png'), fullPage: true });
    await page.setViewportSize({ width: 390, height: 844 });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    await page.screenshot({ path: join(output, 'original-cards-mobile.png'), fullPage: true });
    await page.setViewportSize({ width: 1440, height: 1000 });
  }
  await review(); await run();
  await page.waitForFunction(() => document.querySelector('#run-state').textContent === '앱 배포 완료', undefined, { timeout: 30000 });
  assert.equal(submitted.length, 1);
  assert.equal(await page.locator('#application-link').getAttribute('href'), 'https://demo.railshot.io/');
  assert.equal(await page.locator('#actions-link').getAttribute('href'), 'https://github.com/example/apps/actions/runs/1');
  await page.reload();
  await page.waitForFunction(() => document.querySelector('#run-state').textContent === '앱 배포 완료', undefined, { timeout: 30000 });
  assert.match(await page.locator('#history-list').innerText(), /browser-demo/);
  assert.equal(submitted.length, 1, 'reload observes without submitting');
  const files = join(stateDirectory, 'fixture'); await mkdir(files);
  await writeFile(join(files, 'index.js'), 'source from zip');
  const zip = await archiveFromPath(files);
  await page.locator('#archive').setInputFiles({ name: 'archive-app.zip', mimeType: 'application/zip', buffer: zip.bytes });
  await review(); await run();
  await page.waitForFunction(() => document.querySelector('#run-meta').textContent.includes('archive-app') && document.querySelector('#run-state').textContent === '앱 배포 완료', undefined, { timeout: 30000 });
  assert.equal(submitted[1].files[0].content.toString(), 'source from zip');
  await writeFile(join(files, 'index.js'), 'source from folder');
  await page.locator('#folder').setInputFiles(files);
  await review(); await run();
  await page.waitForFunction(() => document.querySelector('#run-meta').textContent.includes('fixture') && document.querySelector('#run-state').textContent === '앱 배포 완료', undefined, { timeout: 30000 });
  assert.equal(await page.locator('#run-state').innerText(), '앱 배포 완료');
  assert.equal(submitted.length, 3);
  assert.equal(submitted[2].files[0].content.toString(), 'source from folder');
  assert.equal(cd.length, 3);
  assert.equal(await page.locator('#application-link').getAttribute('href'), 'https://demo.railshot.io/');
  assert.equal(requests.some((request) => request.authorization), false, 'no browser credentials');
  assert.equal(requests.some((request) => ['/api/deploy'].includes(request.path)), false, 'dashboard uses product resources');
  const other = await page.context().browser().newContext();
  try {
    const stranger = await other.newPage(); await stranger.goto(origin);
    await stranger.waitForFunction(() => document.querySelector('#session-note').textContent.includes('까지'));
    assert.equal(await stranger.locator('#history-list li').count(), 0, 'another browser cannot restore these deployments');
  } finally { await other.close(); }
  assert.deepEqual(errors, []);
  if (process.env.RAILSHOT_BROWSER_SCREENSHOT) {
    const output = process.env.RAILSHOT_BROWSER_SCREENSHOT;
    await page.screenshot({ path: output, fullPage: true });
  }
});

test('anonymous browser sessions persist settings and write-only OpenStack connections separately', { timeout: 45000 }, async (t) => {
  const { page, origin, errors } = await start(t, { service: null });
  await page.goto(origin);
  await page.waitForFunction(() => document.querySelector('#session-note').textContent.includes('까지'));
  const cookie = (await page.context().cookies()).find((row) => row.name === 'railshot_session');
  assert.ok(cookie.httpOnly); assert.equal(cookie.sameSite, 'Strict');
  const savedView = page.waitForResponse((res) => res.url().endsWith('/api/v1/preferences') && res.request().method() === 'PUT');
  await page.locator('[data-view="connections"]').click(); await savedView;
  await page.locator('#connection-label').fill('우리 OpenStack');
  await page.locator('#connection-url').fill('https://openstack.example/dashboard/');
  await page.locator('#connection-username').fill('demo-user');
  await page.locator('#connection-password').fill('browser-secret-123');
  await page.locator('#connection-save').click();
  await page.waitForFunction(() => document.querySelector('#connection-list').textContent.includes('비밀번호 저장됨'));
  assert.equal(await page.locator('#connection-password').inputValue(), '');
  await page.reload();
  await page.waitForFunction(() => document.querySelector('#connection-list').textContent.includes('demo-user'));
  assert.equal(await page.locator('#connections-view').isVisible(), true);
  assert.ok(!(await page.locator('body').textContent()).includes('browser-secret-123'));
  const other = await page.context().browser().newContext();
  try {
    const stranger = await other.newPage(); await stranger.goto(origin);
    await stranger.waitForFunction(() => document.querySelector('#session-note').textContent.includes('까지'));
    assert.equal(await stranger.locator('#deploy-view').isVisible(), true);
    await stranger.locator('[data-view="connections"]').click();
    assert.equal(await stranger.locator('#connection-list li').count(), 0);
  } finally { await other.close(); }
  if (process.env.CI_OUTPUT_DIR) {
    await mkdir(process.env.CI_OUTPUT_DIR, { recursive: true });
    await page.screenshot({ path: join(process.env.CI_OUTPUT_DIR, 'sessions-connections-desktop.png'), fullPage: true });
    await page.setViewportSize({ width: 390, height: 844 });
    await page.evaluate(() => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done))));
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    await page.screenshot({ path: join(process.env.CI_OUTPUT_DIR, 'sessions-connections-mobile.png'), fullPage: true });
  }
  await page.locator('#connection-list').getByRole('button', { name: '수정', exact: true }).click();
  assert.equal(await page.locator('#connection-password').inputValue(), '');
  await page.locator('#connection-clear-password').check();
  await page.locator('#connection-save').click();
  await page.waitForFunction(() => document.querySelector('#connection-list').textContent.includes('비밀번호 없음'));
  await page.locator('#connection-list').getByRole('button', { name: '삭제', exact: true }).click();
  await page.waitForFunction(() => document.querySelector('#connection-list').children.length === 0);
  assert.deepEqual(errors, []);
});

test('each provider selection keeps the assigned CI and CD target through reload', { timeout: 90000 }, async (t) => {
  for (const provider of ['aws', 'gcp', 'openstack']) await t.test(provider, async (t) => {
    const targetId = `assigned-${provider}`, app = `${provider}-app`, commit = 'a'.repeat(40);
    const submissions = [], deliveries = [];
    const publication = { run_id: 1, target_id: targetId, app, tenant: 'demo', source_commit: commit, artifact_id: 2, producer_attempt: 1 };
    const service = { targetId,
      deploy: async (input) => { submissions.push(input); return { run_id: 1, source_commit: commit }; },
      status: async () => ({ state: 'published', status: 'completed', conclusion: 'success', source_commit: commit, publication }),
    };
    const deployPublished = Object.assign(async (input) => {
      deliveries.push(input);
      return { cd: { state: 'deployed', deployed: true, revision: 'b'.repeat(40) },
        public_http: { state: 'succeeded', verified_at: new Date().toISOString(), url: `https://${provider}.example.test/health` } };
    }, { targets: { [targetId]: { applicationName: app, tenant: 'demo' } } });
    const { page, origin, errors } = await start(t, { service, target: { provider }, deployPublished,
      sourceLoader: async () => ({ files: [{ path: 'index.js', content: Buffer.from('provider fixture') }] }),
    });
    await page.goto(origin);
    await page.waitForFunction(() => document.querySelector('#session-note').textContent.includes('까지'));
    const saved = page.waitForResponse((res) => res.url().endsWith('/api/v1/preferences') && res.request().method() === 'PUT');
    if (provider === 'openstack') {
      await page.getByRole('radio', { name: /온프레미스/ }).check();
      await page.locator('#provider').selectOption(provider);
    } else await page.locator('#cloud-provider').selectOption(provider);
    await saved; await page.reload();
    await page.waitForFunction(() => document.querySelector('#session-note').textContent.includes('까지'));
    assert.equal(await page.locator(provider === 'openstack' ? '#provider' : '#cloud-provider').inputValue(), provider);
    await page.locator('#repository-url').fill('https://github.com/example/provider-fixture');
    await page.locator('#deploy-form button[type="submit"]').click();
    await page.locator('#deploy-button').click();
    await page.waitForFunction(() => document.querySelector('#run-state').textContent === '앱 배포 완료');
    assert.equal(submissions.length, 1); assert.equal(deliveries.length, 1);
    assert.equal(submissions[0].target_id, targetId); assert.equal(submissions[0].app, app);
    assert.equal(deliveries[0].targetId, targetId); assert.equal(deliveries[0].publication.target_id, targetId);
    assert.equal(await page.locator('#application-link').getAttribute('href'), `https://${provider}.example.test/health`);
    assert.deepEqual(errors, []);
  });
});

test('deployment monitor binds metrics, restores progress, and distinguishes stale, collection and HTTP failure', { timeout: 45000 }, async (t) => {
  let state = 'ready', age = 0, http = 1, broken = false;
  const record = { id: 'monitor-demo', app: 'demo-app', target_id: 'demo-aws', status: 'running', stage: 'cd',
    actions_url: 'https://github.com/example/apps/actions/runs/123',
    source_commit: 'a'.repeat(40), source_digest: 'b'.repeat(64), ci: { run_id: '123', state: 'published', images: { app: `ghcr.io/example/app@sha256:${'c'.repeat(64)}` }, steps: [{ key: 'release', status: 'completed', conclusion: 'success' }] },
    cd: { state: 'progressing', revision: 'd'.repeat(40), deployed: false }, public_http: { state: 'not_run', verified_at: null, url: null } };
  const { page, origin, errors, requests } = await start(t, { product: {
    dashboard: { session: () => ({ id: 'monitor-test', expires_at: '2099-01-01T00:00:00Z' }), preferences: () => ({ view: 'deploy', environment: 'cloud', provider: '' }), connections: () => [] },
    list: () => ({ items: [record], next_marker: null, total: 1 }),
    targets: () => [], profiles: () => [],
    getDeploymentLogs: () => ({ deployment_id: record.id, app: record.app, target_id: record.target_id, state: 'ready',
      checked_at: new Date().toISOString(), entries: [{ pod: 'demo-app-123', container: 'app', text: 'GET /health 200\n<img src=x onerror=alert(1)>' }] }),
    getDeployment: () => {
      if (broken) throw new Error('private backend details');
      return { ...record, observation: { deployment_id: record.id, app: record.app, target_id: record.target_id, checked_at: new Date().toISOString(), stale_after_seconds: 90,
        collector: { id: 'acceptance-observer', role: 'shared_observer', lifecycle: 'acceptance', expires_at: new Date(Date.now() + 3600000).toISOString() },
        metrics: Object.fromEntries(Object.entries({ pods: 2, cpu_percent: 12.5, memory_percent: 30, http }).map(([name, value]) => [name, { state, value: state === 'ready' ? value : null, observed_at: new Date(Date.now() - age).toISOString() }])) } };
    },
  } });
  const monitor = async () => { await page.locator('[data-view="monitor"]').click(); };
  const refresh = async () => {
    await page.locator('[data-view="deploy"]').click();
    await page.locator('#stop-polling').click();
    assert.match(await page.locator('#observation-status').textContent(), /조회 중지 · 마지막 조회/);
    await page.locator('#refresh-run').click();
    await monitor();
  };
  await page.goto(origin); await page.waitForFunction(() => document.querySelector('#metric-pods').textContent === '2개');
  await monitor();
  assert.match(await page.locator('#collector-note').innerText(), /공유 관측 서버 · 임시 인수용 · 만료/);
  assert.match(await page.locator('#run-binding').textContent(), /CI run: 123/);
  assert.match(await page.locator('#run-binding').textContent(), /sha256:cccc/);
  assert.equal(await page.locator('#monitor-application-link').isVisible(), false);
  const output = process.env.CI_OUTPUT_DIR;
  if (output) { await mkdir(output, { recursive: true }); await page.screenshot({ path: join(output, 'monitor-running.png'), fullPage: true }); }
  record.status = 'succeeded'; record.stage = 'complete'; record.cd.deployed = true; record.cd.state = 'deployed';
  record.public_http = { state: 'succeeded', verified_at: new Date().toISOString(), url: 'https://app.example.test/health', site_url: 'https://app.example.test/' };
  await page.reload(); await page.waitForFunction(() => document.querySelector('#run-state').textContent === '앱 배포 완료', undefined, { timeout: 30000 }); await monitor();
  assert.equal(await page.locator('#monitor-application-link').getAttribute('href'), 'https://app.example.test/');
  assert.equal(await page.locator('#monitor-actions-link').getAttribute('href'), 'https://github.com/example/apps/actions/runs/123');
  await page.locator('[data-console="app"]').click();
  await page.waitForFunction(() => document.querySelector('#console-output').textContent.includes('GET /health 200'));
  assert.equal(await page.locator('#console-output img').count(), 0, 'app logs are text, never HTML');
  assert.ok(requests.some((request) => request.path === '/api/v1/deployments/monitor-demo/logs'));
  await page.locator('[data-console="work"]').click();
  if (output) await page.screenshot({ path: join(output, 'monitor-success.png'), fullPage: true });
  age = 100000; await refresh(); await page.waitForFunction(() => document.querySelector('#metric-pods').textContent === '오래된 값');
  age = 0; state = 'collection_failed'; await refresh(); await page.waitForFunction(() => document.querySelector('#metric-pods').textContent === '수집 실패');
  state = 'ready'; http = 0; await refresh(); await page.waitForFunction(() => document.querySelector('#metric-http').textContent === '검사 실패');
  assert.equal(await page.locator('#metric-pods').innerText(), '2개');
  broken = true; await refresh(); await page.waitForFunction(() => document.querySelector('#metric-pods').textContent === '수집 연결 실패');
  assert.match(await page.locator('#monitor-state').innerText(), /상태 조회 실패/);
  assert.match(await page.locator('#observation-status').innerText(), /재시도 중/);
  await page.locator('[data-view="deploy"]').click();
  await page.locator('#stop-polling').click();
  assert.match(await page.locator('#observation-status').textContent(), /조회 중지/);
  await monitor();
  if (output) await page.screenshot({ path: join(output, 'monitor-unavailable.png'), fullPage: true });
  broken = false; record.status = 'failed'; record.stage = 'ci'; record.error = { message: '테스트를 찾지 못했습니다. 실행 가능한 테스트를 추가하세요.' }; record.public_http.state = 'not_run';
  record.ci.diagnostics = { state: 'ready', reason: 'NO_TESTS', phase: 'Q.discovery', agent_attempts: 1, changed_file_count: 3 };
  await page.reload(); await page.waitForFunction(() => document.querySelector('#run-state').textContent === '실행 실패'); await monitor();
  assert.equal(await page.locator('#monitor-application-link').isVisible(), false);
  assert.match(await page.locator('#monitor-message').innerText(), /테스트를 찾지 못했습니다/);
  assert.match(await page.locator('#console-output').innerText(), /NO_TESTS/);
  assert.equal(await page.locator('#monitor-actions-link').isVisible(), true);
  if (output) await page.screenshot({ path: join(output, 'monitor-failed.png'), fullPage: true });
  assert.equal(requests.some((request) => request.method === 'POST' && request.path !== '/api/v1/sessions'), false, 'resume and observation never redeploy');
  assert.deepEqual(errors, []);
});

test('work log reads bound agent events over HTTP and marks stale or failed observations without redeploying', { timeout: 45000 }, async (t) => {
  const record = { id: 'events-demo', app: 'demo-app', target_id: 'demo-aws', status: 'running',
    source_commit: 'a'.repeat(40), ci: { run_id: '123', state: 'running', steps: [] } };
  let mode = 'live', attempt = 1;
  const { page, origin, errors, requests } = await start(t, { product: {
    dashboard: { session: () => ({ id: 'events-test', expires_at: '2099-01-01T00:00:00Z' }), preferences: () => ({ view: 'monitor', environment: 'cloud', provider: '' }), connections: () => [] },
    list: () => ({ items: [record], next_marker: null, total: 1 }), targets: () => [], profiles: () => [], getDeployment: () => record,
    getDeploymentEvents: () => {
      if (mode === 'error') throw new Error('private event transport details');
      return { deployment_id: record.id, app: record.app, target_id: record.target_id, source_commit: record.source_commit,
        run_id: record.ci.run_id, run_attempt: attempt, state: mode === 'empty' ? 'not_started' : 'live',
        checked_at: new Date().toISOString(), updated_at: new Date(Date.now() - (mode === 'stale' ? 120000 : 0)).toISOString(),
        truncated: true, items: mode === 'empty' ? [] : [{ sequence: 1, event_name: 'agent.heartbeat', role: 'fixer',
          progress: { sdk_event_count: 7, elapsed_ms: 1200, last_sdk_event_at_ms: Date.now(), item_counts: { commandExecution: 1 } } }], next_marker: null };
    },
  } });
  const output = page.locator('#console-output');
  const refresh = async () => { await page.locator('[data-console="work"]').click(); };
  await page.goto(origin);
  await page.waitForFunction(() => document.querySelector('#console-output').textContent.includes('agent.heartbeat'));
  assert.match(await output.innerText(), /관측 중/);
  assert.match(await output.innerText(), /최근 이벤트만/);
  assert.match(await output.innerText(), /sdk_event_count/);
  mode = 'stale'; await refresh();
  await page.waitForFunction(() => document.querySelector('#console-output').textContent.includes('갱신이 지연'));
  mode = 'error'; await refresh();
  await page.waitForFunction(() => document.querySelector('#console-output').textContent.includes('이벤트 조회 실패'));
  assert.match(await output.innerText(), /agent.heartbeat/);
  assert.doesNotMatch(await output.innerText(), /private event/);
  mode = 'empty'; attempt = 2; await refresh();
  await page.waitForFunction(() => document.querySelector('#console-output').textContent.includes('아직 에이전트 이벤트'));
  assert.doesNotMatch(await output.innerText(), /agent.heartbeat/, 'an earlier attempt is never reused for a new attempt');
  assert.ok(requests.some((request) => request.path === '/api/v1/deployments/events-demo/events'));
  assert.equal(requests.some((request) => request.authorization), false);
  assert.equal(requests.some((request) => request.method === 'POST' && request.path !== '/api/v1/sessions'), false);
  assert.deepEqual(errors, []);
});

test('saved connection failure stays local to its panel and does not disable deployment choices', { timeout: 45000 }, async (t) => {
  const { page, origin, errors } = await start(t, { service: null });
  await page.route('**/api/v1/connections*', (route) => route.fulfill({ status: 503, contentType: 'application/json',
    body: JSON.stringify({ error: { message: '연결 목록을 일시적으로 조회할 수 없습니다.' } }) }));
  await page.goto(origin);
  await page.waitForFunction(() => document.querySelector('#connection-message').textContent.includes('일시적으로'));
  assert.match(await page.locator('#session-note').textContent(), /까지 유지/);
  assert.doesNotMatch(await page.locator('#connection-status').textContent(), /일시적으로/);
  const saved = page.waitForResponse((response) => response.url().endsWith('/api/v1/preferences') && response.request().method() === 'PUT');
  await page.getByRole('radio', { name: /온프레미스/ }).check();
  assert.equal((await saved).status(), 200);
  assert.deepEqual(errors, []);
});
