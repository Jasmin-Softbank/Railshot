import assert from 'node:assert/strict';
import { once } from 'node:events';
import { mkdir, mkdtemp, writeFile, rm } from 'node:fs/promises';
import { execFileSync } from 'node:child_process';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { chromium } from 'playwright';
import { createAppServer } from '../../apps/api/src/server.js';

const repository = 'https://github.com/example/browser-smoke';

async function chooseArchive(page) {
  const [chooser] = await Promise.all([
    page.waitForEvent('filechooser'),
    page.locator('#choose-file').click(),
  ]);
  // Selection only: an empty ZIP never leaves this browser.
  await chooser.setFiles({
    name: 'browser-smoke.zip', mimeType: 'application/zip',
    buffer: Buffer.from([0x50, 0x4b, 0x05, 0x06, ...Array(18).fill(0)]),
  });
  assert.match(await page.locator('#source-selection').innerText(), /browser-smoke\.zip/);
  assert.equal(await page.locator('#repository-url').inputValue(), '');
}

async function checkReviewDashboard(page) {
  const submit = page.locator('#deploy-form button[type="submit"]');
  const review = page.locator('#review-panel');
  assert.equal(await page.locator('#deploy-view').isVisible(), true);
  assert.equal(await review.isVisible(), false);
  await submit.click();
  assert.match(await page.locator('#form-error').innerText(), /소스를 선택/);
  await page.locator('#repository-url').fill('https://example.invalid/owner/app');
  await submit.click();
  assert.match(await page.locator('#form-error').innerText(), /GitHub 저장소 URL/);
  await page.locator('#repository-url').fill(repository);
  await submit.click();
  assert.equal(await review.isVisible(), true);
  assert.equal(await page.locator('#review-source').innerText(), repository);
  assert.match(await page.locator('#review-target').innerText(), /RailShot AWS/);
  assert.match(await review.innerText(), /운영자 등록 대상을 선택/);
  assert.equal(await review.getByRole('button', { name: '검사 및 이미지 게시' }).isDisabled(), true);
  await page.locator('#edit-selection').click();
  assert.equal(await review.isVisible(), false);
  await chooseArchive(page);
  await page.locator('input[name="environment"][value="onprem"]').check();
  assert.equal(await page.locator('#provider-field').isVisible(), true);
  await submit.click();
  assert.match(await page.locator('#form-error').innerText(), /인프라 종류를 선택/);
  await page.locator('#provider').selectOption('openstack');
  await submit.click();
  assert.equal(await page.locator('#review-source').innerText(), 'browser-smoke.zip');
  assert.match(await page.locator('#review-target').innerText(), /온프레미스.*OpenStack/);
  assert.equal(await review.getByRole('button', { name: '검사 및 이미지 게시' }).isDisabled(), true);
  await page.locator('[data-view="history"]').click();
  assert.equal(await page.locator('#deploy-view').isVisible(), false);
  assert.match(await page.locator('#history-view').innerText(), /아직 배포 내역이 없습니다/);
  assert.equal(await page.locator('[data-view="history"]').getAttribute('aria-current'), 'page');
  await page.locator('[data-view="monitor"]').click();
  assert.equal(await page.locator('#history-view').isVisible(), false);
  assert.match(await page.locator('#monitor-view').innerText(), /연결된 배포 없음/);
  for (const [tab, text] of [['environment', '인프라와 앱 상태'], ['app', '앱 로그'], ['work', '작업 로그']]) {
    await page.locator(`[data-console="${tab}"]`).click();
    assert.equal(await page.locator(`[data-console="${tab}"]`).getAttribute('aria-selected'), 'true');
    assert.match(await page.locator('#console-output').innerText(), new RegExp(text));
  }
  await page.locator('[data-view="deploy"]').click();
  assert.equal(await page.locator('#deploy-view').isVisible(), true);
  assert.equal(await page.locator('#monitor-view').isVisible(), false);
}

test('the real API serves a usable dashboard without deployment credentials', { timeout: 45_000 }, async (t) => {
  // Explicit null ignores any operator credentials in the inherited environment.
  const server = createAppServer({ service: null });
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  t.after(() => new Promise((resolve) => {
    server.close(resolve);
    server.closeAllConnections();
  }));
  const origin = `http://127.0.0.1:${server.address().port}`;
  const requests = [], responses = new Map(), errors = [];
  let browser, context, page;
  try {
    browser = await chromium.launch({ executablePath: process.env.CHROME_EXECUTABLE || undefined });
    context = await browser.newContext({ serviceWorkers: 'block', viewport: { width: 1440, height: 1000 } });
    await context.tracing.start({ screenshots: true, snapshots: true });
    // No remote assets, API dispatch, telemetry, or other local services are allowed.
    const allowed = new Set(['/', '/app.js', '/styles.css', '/healthz']);
    await context.route('**/*', async (route) => {
      const request = route.request();
      const url = new URL(request.url());
      requests.push({ method: request.method(), url: request.url() });
      if (url.origin === origin && request.method() === 'GET' && allowed.has(url.pathname)) {
        await route.continue();
      } else {
        errors.push(`Blocked unexpected request: ${request.method()} ${request.url()}`);
        await route.abort('blockedbyclient');
      }
    });
    await context.routeWebSocket('**/*', (socket) => {
      errors.push(`Blocked unexpected WebSocket: ${socket.url()}`);
      socket.close();
    });
    page = await context.newPage();
    page.setDefaultTimeout(5_000);
    page.on('pageerror', (error) => errors.push(`Page error: ${error.message}`));
    page.on('console', (message) => {
      if (message.type() === 'error') errors.push(`Console error: ${message.text()}`);
    });
    page.on('requestfailed', (request) => errors.push(`Request failed: ${request.url()} ${request.failure()?.errorText}`));
    page.on('response', (response) => {
      responses.set(new URL(response.url()).pathname, { status: response.status(), type: response.headers()['content-type'] });
    });
    await page.goto(origin, { waitUntil: 'load' });
    for (const [path, mime] of [['/', 'text/html'], ['/app.js', 'text/javascript'], ['/styles.css', 'text/css']]) {
      assert.equal(responses.get(path)?.status, 200, `${path} must load through the API server`);
      assert.match(responses.get(path)?.type || '', new RegExp(`^${mime}`));
    }
    assert.deepEqual(await page.evaluate(() => fetch('/healthz').then((response) => response.json())), {
      ok: true, configured: false, target_id: null,
    });
    await checkReviewDashboard(page);
    await page.locator('[name="environment"][value="registered"]').check();
    await page.locator('#deploy-form button[type="submit"]').click();
    assert.equal(await page.locator('#deploy-button').isDisabled(), true);
    assert.match(await page.locator('#review-note').innerText(), /설정/);
    assert.deepEqual(errors, [], 'No browser errors or unexpected requests');
    assert.equal(requests.some(({ url }) => new URL(url).pathname.startsWith('/api/')), false, 'No deployment/status API request is simulated');
    t.diagnostic('Verified dashboard assets, source selection, navigation and unconfigured API blocking; no deployment requests');
    await context.tracing.stop();
  } catch (error) {
    const base = process.env.CI_OUTPUT_DIR || process.env.RUNNER_TEMP || tmpdir();
    await mkdir(base, { recursive: true });
    const directory = await mkdtemp(join(base, 'railshot-browser-'));
    await writeFile(join(directory, 'failure.json'), JSON.stringify({ error: error.stack, errors, requests }, null, 2));
    if (page) await page.screenshot({ path: join(directory, 'failure.png'), fullPage: true }).catch(() => {});
    if (context) await context.tracing.stop({ path: join(directory, 'trace.zip') }).catch(() => {});
    t.diagnostic(`Browser failure artifacts: ${directory}`);
    throw error;
  } finally {
    await browser?.close();
  }
});

test('dashboard submits real multipart requests and bounds polling, credentials and target selection', { timeout: 60_000 }, async (t) => {
  const received = [], loaded = [], calls = [], errors = [];
  let state = 'queued', statusReads = 0, postError = false, statusError = false, hideTarget = false;
  let delayStatus = false, releaseStatus;
  const secret = 'dashboard-test-access-token-32-characters';
  const server = createAppServer({
    sourceLoader: async (url) => { loaded.push(url); return { files: [{ path: 'index.js', content: Buffer.from('hello') }] }; },
    service: {
      targetId: 'operator-demo',
      deploy: async (source) => {
        received.push(source);
        await new Promise((resolve) => setTimeout(resolve, 100));
        return { run_id: received.length, app: source.app, target_id: 'operator-demo', state: 'queued',
          actions_url: `https://github.com/example/apps/actions/runs/${received.length}` };
      },
      status: async (id) => {
        statusReads++;
        if (delayStatus) await new Promise((resolve) => { releaseStatus = resolve; });
        return { run_id: Number(id), target_id: 'operator-demo', state, status: state === 'queued' ? 'queued' : 'completed',
          // The UI must not mistake an arbitrary URL field for verified app deployment.
          url: 'https://example.invalid/not-a-verified-deployment',
          actions_url: state === 'failed' ? 'javascript:alert(1)' : `https://github.com/example/apps/actions/runs/${id}`,
          steps: [{ key: 'loop', status: 'completed', conclusion: 'success' },
            { key: 'release', status: state === 'queued' ? 'queued' : 'completed', conclusion: state === 'published' ? 'success' : null }] };
      },
    },
  });
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  t.after(() => new Promise((resolve) => { server.close(resolve); server.closeAllConnections(); }));
  const fixture = await mkdtemp(join(tmpdir(), 'railshot-dashboard-'));
  t.after(() => rm(fixture, { recursive: true, force: true }));
  const browser = await chromium.launch({ executablePath: process.env.CHROME_EXECUTABLE || undefined });
  t.after(() => browser.close());
  const context = await browser.newContext({ serviceWorkers: 'block' });
  const origin = `http://127.0.0.1:${server.address().port}`;
  await context.route('**/*', async (route) => {
    const request = route.request(), url = new URL(request.url());
    calls.push({ method: request.method(), path: url.pathname });
    if (url.origin !== origin || !/^\/(?:app\.js|styles\.css|healthz|api\/deploy|api\/runs\/\d+)?$/.test(url.pathname)) {
      errors.push(`Unexpected request ${request.method()} ${url.pathname}`);
      await route.abort();
    } else if (url.pathname === '/healthz' && hideTarget) {
      await route.fulfill({ json: { ok: true, configured: true } });
    } else if (url.pathname.startsWith('/api/')) {
      assert.equal(request.headers().authorization, `Bearer ${secret}`);
      if ((postError && request.method() === 'POST') || (statusError && request.method() === 'GET')) {
        await route.fulfill({ status: postError ? 401 : 502, json: { error: 'controlled failure' } });
      } else await route.continue();
    } else {
      assert.equal(request.headers().authorization, undefined, 'token must never reach assets or health checks');
      await route.continue();
    }
  });
  await context.routeWebSocket('**/*', (socket) => { errors.push('Unexpected WebSocket'); socket.close(); });
  const page = await context.newPage();
  page.setDefaultTimeout(5_000);
  page.on('pageerror', (error) => errors.push(error.message));
  await page.clock.install();
  await page.goto(origin);
  await page.waitForFunction(() => document.querySelector('#connection-status').textContent.includes('API 연결됨'));
  await page.locator('#api-token').fill(secret);
  const review = () => page.locator('#deploy-form button[type="submit"]').click();
  const start = () => page.locator('#deploy-button').click();
  const waitForState = (text) => page.waitForFunction((label) => document.querySelector('#run-state').textContent.includes(label), text);
  await page.locator('#repository-url').fill(repository);
  await review();
  assert.equal(await page.locator('#deploy-button').isDisabled(), true, 'cloud selection cannot infer provider from target ID');
  await page.locator('[name="environment"][value="onprem"]').check();
  await page.locator('#provider').selectOption('openstack');
  await review();
  assert.equal(await page.locator('#deploy-button').isDisabled(), true, 'on-prem selection cannot silently use operator target');
  assert.equal(received.length, 0);
  await page.locator('[name="environment"][value="registered"]').check();
  await review();
  assert.match(await page.locator('#review-target').innerText(), /operator-demo/);
  await page.evaluate(() => { document.querySelector('#deploy-button').click(); document.querySelector('#deploy-button').click(); });
  await waitForState('CI 대기 중');
  await page.waitForFunction(() => !document.querySelector('#stop-polling').hidden);
  assert.equal(received.length, 1, 'duplicate clicks send one request');
  assert.equal(received[0].app, 'browser-smoke');
  assert.equal(received[0].target_id, 'operator-demo');
  assert.deepEqual(loaded, [repository]);
  state = 'published';
  await page.clock.runFor(15_000);
  await waitForState('이미지 게시 완료 · 앱 배포 미확인');
  const terminalReads = statusReads;
  await page.clock.runFor(45_000);
  assert.equal(statusReads, terminalReads, 'terminal state stops polling');
  assert.equal(await page.locator('a[href="https://example.invalid/not-a-verified-deployment"]').count(), 0);
  await page.locator('[data-view="history"]').click();
  assert.match(await page.locator('#history-detail').innerText(), /앱 배포 미확인/);
  await page.locator('[data-view="deploy"]').click();
  assert.deepEqual(await page.evaluate(() => ({ local: { ...localStorage }, session: { ...sessionStorage } })), { local: {}, session: {} });
  assert.ok(!await page.locator('body').innerText().then((text) => text.includes(secret)));
  assert.ok(!page.url().includes(secret));

  postError = true;
  await review();
  await start();
  await page.waitForFunction(() => !document.querySelector('#request-error').hidden);
  assert.match(await page.locator('#request-error').innerText(), /API 접근 토큰/);
  assert.equal(received.length, 1);
  assert.equal(await page.locator('#deploy-button').isDisabled(), true, 'POST errors require a new review, never automatic retry');
  postError = false;

  await page.locator('#api-token').fill('invalid-token');
  await review();
  const beforeInvalidToken = calls.filter(({ method }) => method === 'POST').length;
  await start();
  await page.waitForFunction(() => !document.querySelector('#request-error').hidden);
  assert.match(await page.locator('#request-error').innerText(), /32~4096자/);
  assert.equal(calls.filter(({ method }) => method === 'POST').length, beforeInvalidToken);

  // A remote health check hides target_id. The explicit registered-target mode still works.
  hideTarget = true;
  await page.reload();
  await page.waitForFunction(() => document.querySelector('#connection-status').textContent.includes('API 연결됨'));
  await page.locator('#api-token').fill(secret);
  const directory = join(fixture, 'folder-app');
  await mkdir(join(directory, 'src'), { recursive: true });
  await mkdir(join(directory, 'node_modules'), { recursive: true });
  await writeFile(join(directory, 'src', 'index.js'), 'folder content');
  await writeFile(join(directory, 'node_modules', 'ignored.js'), 'ignored');
  await page.locator('#folder').setInputFiles(directory);
  await page.locator('[name="environment"][value="registered"]').check();
  await review();
  state = 'failed';
  await start();
  await waitForState('CI 실패');
  assert.equal(received[1].target_id, undefined, 'hidden target is resolved by the server');
  assert.equal(received[1].app, 'folder-app');
  assert.deepEqual(received[1].files.map(({ path, content }) => [path, content.toString()]), [['src/index.js', 'folder content']]);
  assert.equal(await page.locator('#actions-link').isVisible(), false, 'unsafe links never become clickable');

  const zip = execFileSync('python3', ['-c', 'import io, sys, zipfile\nb=io.BytesIO()\nwith zipfile.ZipFile(b,"w") as z: z.writestr("main.py","print(1)")\nsys.stdout.buffer.write(b.getvalue())']);
  await page.locator('#archive').setInputFiles({ name: 'archive-app.zip', mimeType: 'application/zip', buffer: zip });
  await review();
  state = 'publication_unverified';
  statusError = true;
  await start();
  await page.waitForFunction(() => document.querySelector('#run-message').textContent.includes('자동 조회를 중지'));
  const failedReads = calls.filter(({ path }) => path.startsWith('/api/runs/')).length;
  await page.clock.runFor(45_000);
  assert.equal(calls.filter(({ path }) => path.startsWith('/api/runs/')).length, failedReads);
  assert.equal(received[2].app, 'archive-app');
  assert.deepEqual(received[2].files.map(({ path }) => path), ['main.py']);
  statusError = false;
  await page.locator('#refresh-run').click();
  await waitForState('게시 증거 확인 필요');

  await review();
  state = 'queued';
  await start();
  await waitForState('CI 대기 중');
  await page.locator('#stop-polling').click();
  const stoppedReads = statusReads;
  await page.clock.runFor(45_000);
  assert.equal(statusReads, stoppedReads, 'explicit stop clears the polling timer');
  delayStatus = true;
  await page.locator('#refresh-run').click();
  for (let attempt = 0; !releaseStatus && attempt < 500; attempt++) await new Promise((resolve) => setTimeout(resolve, 10));
  assert.equal(typeof releaseStatus, 'function', 'status request must be in flight before page exit');
  await page.evaluate(() => window.dispatchEvent(new PageTransitionEvent('pagehide')));
  releaseStatus();
  const hiddenReads = statusReads;
  await page.clock.runFor(45_000);
  assert.equal(statusReads, hiddenReads, 'page exit clears polling');
  assert.equal(await page.locator('#api-token').inputValue(), '');
  assert.deepEqual(errors, []);
});
