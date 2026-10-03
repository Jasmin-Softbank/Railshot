import assert from 'node:assert/strict';
import { once } from 'node:events';
import { mkdtemp, realpath, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { chromium } from 'playwright';
import { createAppServer } from '../../apps/api/src/server.js';
import { createProductStore } from '../../apps/api/src/product-store.js';

test('monitor follows the bound Actions run and keeps long console output scrollable', { timeout: 45000 }, async (t) => {
  const directory = await realpath(await mkdtemp(join(tmpdir(), 'railshot-monitor-')));
  let server, browser, product;
  t.after(async () => {
    await browser?.close();
    if (server?.listening) await new Promise((resolve) => { server.close(resolve); server.closeAllConnections(); });
    await product?.close();
    await rm(directory, { recursive: true, force: true });
  });
  const store = await createProductStore(directory);
  const session = store.dashboard.session();
  const sourceCommit = 'a'.repeat(40);
  await store.transaction((state) => {
    state.operations['deploy-monitor'] = { id: 'deploy-monitor', kind: 'deployments', session_id: session.id,
      app: 'sample-app', target_id: 'demo-aws', source_commit: sourceCommit, status: 'running', stage: 'ci',
      created_at: new Date().toISOString(), ci: { run_id: '4242', state: 'queued', steps: [] } };
    state.bindings['4242'] = { operation_id: 'deploy-monitor', app: 'sample-app', target_id: 'demo-aws', source_commit: sourceCommit };
  });
  await store.close();
  let finished = false;
  const service = { targetId: 'demo-aws', targetIds: ['demo-aws'], status: async () => ({
    run_id: 4242, status: finished ? 'completed' : 'in_progress', conclusion: finished ? 'success' : null,
    state: finished ? 'published' : 'running', source_commit: sourceCommit, target_id: 'demo-aws',
    actions_url: 'https://github.com/Jasmin-Softbank/railshot-apps/actions/runs/4242',
    steps: [
      { key: 'loop', status: 'completed', conclusion: 'success', tasks: [] },
      { key: 'release', status: finished ? 'completed' : 'in_progress', conclusion: finished ? 'success' : null,
        tasks: [{ number: 3, name: 'Verify bundle and publish tested images', status: finished ? 'completed' : 'in_progress', conclusion: finished ? 'success' : null }] },
    ],
    ...(finished ? { publication: { run_id: 4242, app: 'sample-app', target_id: 'demo-aws', source_commit: sourceCommit } } : {}),
  }) };
  server = createAppServer({ stateDirectory: directory, service });
  product = await server.productReady;
  server.listen(0, '127.0.0.1'); await once(server, 'listening');
  const origin = `http://127.0.0.1:${server.address().port}`;
  browser = await chromium.launch({ executablePath: process.env.CHROME_EXECUTABLE || undefined });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  await context.addCookies([{ name: 'railshot_session', value: session.token, url: origin, httpOnly: true, sameSite: 'Strict' }]);
  const page = await context.newPage();
  page.setDefaultTimeout(5000);
  await page.goto(origin);
  await page.locator('[data-view="history"]').click();
  await page.getByRole('button', { name: /sample-app.*상세/ }).click();
  await page.waitForFunction(() => document.querySelector('#monitor-ci-status')?.textContent.includes('Actions #4242 · 확인'));
  assert.match(await page.locator('#monitor-steps').innerText(), /검증 이미지 게시\s+진행 중/);
  assert.match(await page.locator('#monitor-steps').innerText(), /Verify bundle and publish tested images/);
  assert.equal(await page.locator('#monitor-actions-link').getAttribute('href'), 'https://github.com/Jasmin-Softbank/railshot-apps/actions/runs/4242');

  finished = true;
  await page.locator('#refresh-run').evaluate((button) => button.click());
  await page.waitForFunction(() => document.querySelector('#monitor-steps')?.textContent.includes('게시 확인'));
  assert.match(await page.locator('#monitor-steps').innerText(), /GitOps 반영\s+대기/);
  assert.match(await page.locator('#monitor-steps').innerText(), /URL 및 앱 상태 확인\s+대기/);

  const dimensions = await page.locator('#console-output').evaluate((node) => {
    node.textContent = '긴 작업 로그\n'.repeat(400);
    node.scrollTop = node.scrollHeight;
    return { height: node.getBoundingClientRect().height, clientHeight: node.clientHeight,
      scrollHeight: node.scrollHeight, scrollTop: node.scrollTop };
  });
  assert.ok(dimensions.height <= 480, `console grew to ${dimensions.height}px`);
  assert.ok(dimensions.scrollHeight > dimensions.clientHeight && dimensions.scrollTop > 0);
});
