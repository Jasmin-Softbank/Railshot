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
  await page.waitForFunction(() => document.querySelector('#connection-status').textContent.includes('API 연결됨'));
  assert.equal(await page.locator('#api-token').count(), 0);
  assert.equal(await page.locator('input[type="password"]').count(), 0);
  await page.locator('#deploy-form button[type="submit"]').click();
  assert.match(await page.locator('#form-error').innerText(), /소스를 선택/);
  await page.locator('#repository-url').fill('https://example.invalid/app');
  await page.locator('#deploy-form button[type="submit"]').click();
  assert.match(await page.locator('#form-error').innerText(), /GitHub 저장소 URL/);
  await page.locator('#repository-url').fill('https://github.com/example/demo');
  await page.locator('#deploy-form button[type="submit"]').click();
  assert.match(await page.locator('#form-error').innerText(), /실행 가능한 대상/);
  assert.equal(await page.locator('#deploy-button').isDisabled(), true);
  await page.locator('[data-view="history"]').click();
  assert.equal(await page.locator('#history-view').isVisible(), true);
  await page.locator('[data-view="monitor"]').click();
  assert.equal(await page.locator('#monitor-view').isVisible(), true);
  await page.locator('[data-console="app"]').click();
  assert.match(await page.locator('#console-output').innerText(), /실행을 시작/);
  assert.equal(requests.some((request) => request.method === 'POST'), false);
  assert.deepEqual(errors, []);
});

test('public dashboard submits three source types, distinguishes publication from deployment and restores the saved record', { timeout: 90000 }, async (t) => {
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
  const { page, origin, errors, requests, stateDirectory } = await start(t, { service,
    access: apiAccessConfig({ RAILSHOT_PUBLIC_DEMO: '1', RAILSHOT_ALLOWED_HOSTS: '127.0.0.1', RAILSHOT_ALLOWED_ORIGINS: 'http://127.0.0.1' }),
    sourceLoader: async () => ({ files: [{ path: 'index.js', content: Buffer.from('source from github') }] }),
    deployPublished: async (request) => { cd.push(request); return { cd: { state: 'deployed', revision: 'b'.repeat(40), deployed: true },
      public_http: { state: 'succeeded', verified_at: new Date().toISOString(), url: 'https://demo.railshot.io' } }; },
  });
  await page.goto(origin);
  await page.waitForFunction(() => document.querySelector('#target').value === 'demo-aws');
  const review = () => page.locator('#deploy-form button[type="submit"]').click();
  const run = () => page.locator('#deploy-button').click();
  await page.locator('#repository-url').fill('https://github.com/example/browser-demo');
  await page.locator('#operation').selectOption('builds');
  await review(); await run();
  await page.waitForFunction(() => document.querySelector('#run-state').textContent === '이미지 게시 완료');
  assert.equal(submitted.length, 1);
  assert.equal(await page.locator('#application-link').isVisible(), false, 'CI publication cannot expose a deployment URL');
  assert.equal(await page.locator('#actions-link').getAttribute('href'), 'https://github.com/example/apps/actions/runs/1');
  await page.reload();
  await page.waitForFunction(() => document.querySelector('#run-state').textContent === '이미지 게시 완료');
  assert.match(await page.locator('#history-summary').innerText(), /browser-demo/);
  assert.equal(submitted.length, 1, 'reload observes without submitting');
  const files = join(stateDirectory, 'fixture'); await mkdir(files);
  await writeFile(join(files, 'index.js'), 'source from zip');
  const zip = await archiveFromPath(files);
  await page.locator('#archive').setInputFiles({ name: 'archive-app.zip', mimeType: 'application/zip', buffer: zip.bytes });
  await page.locator('#operation').selectOption('builds');
  await review(); await run();
  await page.waitForFunction(() => document.querySelector('#run-meta').textContent.includes('archive-app') && document.querySelector('#run-state').textContent === '이미지 게시 완료');
  assert.equal(submitted[1].files[0].content.toString(), 'source from zip');
  await writeFile(join(files, 'index.js'), 'source from folder');
  await page.locator('#folder').setInputFiles(files);
  await page.locator('#operation').selectOption('deployments');
  await page.locator('#app-name').fill('folder-app');
  await review(); await run();
  await page.waitForFunction(() => document.querySelector('#run-meta').textContent.includes('folder-app') && document.querySelector('#run-state').textContent === '앱 배포 완료', undefined, { timeout: 30000 });
  assert.equal(await page.locator('#run-state').innerText(), '앱 배포 완료');
  assert.equal(submitted.length, 3);
  assert.equal(submitted[2].files[0].content.toString(), 'source from folder');
  assert.equal(cd.length, 1);
  assert.equal(await page.locator('#application-link').getAttribute('href'), 'https://demo.railshot.io/');
  assert.equal(requests.some((request) => request.authorization), false, 'no browser credentials');
  assert.equal(requests.some((request) => ['/api/deploy'].includes(request.path)), false, 'dashboard uses product resources');
  assert.deepEqual(errors, []);
  if (process.env.RAILSHOT_BROWSER_SCREENSHOT) {
    const output = process.env.RAILSHOT_BROWSER_SCREENSHOT;
    await page.screenshot({ path: output, fullPage: true });
  }
});

test('deployment monitor binds metrics, restores progress, and distinguishes stale, collection and HTTP failure', { timeout: 45000 }, async (t) => {
  let state = 'ready', age = 0, http = 1, broken = false;
  const record = { id: 'monitor-demo', app: 'demo-app', target_id: 'demo-aws', status: 'running', stage: 'cd',
    source_commit: 'a'.repeat(40), source_digest: 'b'.repeat(64), ci: { run_id: '123', state: 'published', images: { app: `ghcr.io/example/app@sha256:${'c'.repeat(64)}` }, steps: [{ key: 'release', status: 'completed', conclusion: 'success' }] },
    cd: { state: 'progressing', revision: 'd'.repeat(40), deployed: false }, public_http: { state: 'not_run', verified_at: null, url: null } };
  const { page, origin, errors, requests } = await start(t, { product: {
    targets: () => [], profiles: () => [],
    getDeployment: () => {
      if (broken) throw new Error('private backend details');
      return { ...record, observation: { deployment_id: record.id, app: record.app, target_id: record.target_id, checked_at: new Date().toISOString(), stale_after_seconds: 90,
        collector: { id: 'acceptance-observer', role: 'shared_observer', lifecycle: 'acceptance', expires_at: new Date(Date.now() + 3600000).toISOString() },
        metrics: Object.fromEntries(Object.entries({ pods: 2, cpu_percent: 12.5, memory_percent: 30, http }).map(([name, value]) => [name, { state, value: state === 'ready' ? value : null, observed_at: new Date(Date.now() - age).toISOString() }])) } };
    },
  } });
  await page.addInitScript(() => localStorage.setItem('railshot.lastExecution', JSON.stringify({ kind: 'deployments', id: 'monitor-demo' })));
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
  assert.match(await page.locator('#monitor-binding').innerText(), /CI run: 123/);
  assert.match(await page.locator('#monitor-binding').innerText(), /sha256:cccc/);
  assert.equal(await page.locator('#monitor-application-link').isVisible(), false);
  const output = process.env.CI_OUTPUT_DIR;
  if (output) { await mkdir(output, { recursive: true }); await page.screenshot({ path: join(output, 'monitor-running.png'), fullPage: true }); }
  record.status = 'succeeded'; record.stage = 'complete'; record.cd.deployed = true; record.cd.state = 'deployed';
  record.public_http = { state: 'succeeded', verified_at: new Date().toISOString(), url: 'https://app.example.test/health', site_url: 'https://app.example.test/' };
  await page.reload(); await page.waitForFunction(() => document.querySelector('#run-state').textContent === '앱 배포 완료'); await monitor();
  assert.equal(await page.locator('#monitor-application-link').getAttribute('href'), 'https://app.example.test/');
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
  broken = false; record.status = 'failed'; record.stage = 'cd'; record.error = { message: '앱 적용 실패' }; record.public_http.state = 'not_run';
  await page.reload(); await page.waitForFunction(() => document.querySelector('#run-state').textContent === '실행 실패'); await monitor();
  assert.equal(await page.locator('#monitor-application-link').isVisible(), false);
  if (output) await page.screenshot({ path: join(output, 'monitor-failed.png'), fullPage: true });
  assert.equal(requests.some((request) => request.method === 'POST'), false, 'resume and observation never redeploy');
  assert.deepEqual(errors, []);
});
