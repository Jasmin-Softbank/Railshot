import assert from 'node:assert/strict';
import { once } from 'node:events';
import { mkdir, mkdtemp, writeFile } from 'node:fs/promises';
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

async function checkLegacy(page) {
  assert.equal(await page.locator('#create-view').isVisible(), true);
  assert.equal(await page.locator('#run-view').isVisible(), false);
  await page.locator('#deploy-button').click();
  assert.match(await page.locator('#form-message').innerText(), /소스|ZIP 파일/);
  await page.locator('#repository-url').fill(repository);
  assert.match(await page.locator('#source-selection').innerText(), /GitHub URL/);
  await chooseArchive(page);
  await page.locator('#repository-url').fill(repository);
  assert.equal(await page.locator('#archive').evaluate((input) => input.files.length), 0);
  // The legacy history button stays on the form when there is no real run.
  await page.locator('#nav-runs').click();
  assert.equal(await page.locator('#create-view').isVisible(), true);
  assert.equal(await page.locator('#run-view').isVisible(), false);
  assert.match(await page.locator('#recent-row').innerText(), /아직 배포 내역이 없습니다/);
  await page.locator('#nav-create').click();
  await page.waitForFunction(() => document.querySelector('#connection-status').textContent.includes('설정 필요'));
  assert.equal(await page.locator('#target-id').inputValue(), '운영자 대상 설정 필요');
  assert.equal(await page.locator('#result-link').isVisible(), false);
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
  assert.match(await review.innerText(), /배포 요청은 아직 전송되지 않습니다/);
  assert.equal(await review.getByRole('button', { name: '배포 시작' }).isDisabled(), true);
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
  assert.equal(await review.getByRole('button', { name: '배포 시작' }).isDisabled(), true);
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
    const legacy = await page.locator('#create-view').count() === 1 && await page.locator('#nav-runs').count() === 1;
    const review = await page.locator('#deploy-view').count() === 1 && await page.locator('#review-panel').count() === 1;
    assert.notEqual(legacy, review, 'Expected exactly one supported UI: legacy CI form or dashboard review UI');
    if (legacy) await checkLegacy(page);
    else await checkReviewDashboard(page);
    assert.deepEqual(errors, [], 'No browser errors or unexpected requests');
    assert.equal(requests.some(({ url }) => new URL(url).pathname.startsWith('/api/')), false, 'No deployment/status API request is simulated');
    t.diagnostic(`Verified ${legacy ? 'legacy CI form' : 'dashboard review UI'}; HTML/JS/CSS, source selection, navigation; no external or deployment requests`);
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
