import assert from 'node:assert/strict';
import { once } from 'node:events';
import { mkdir, mkdtemp, readFile, writeFile, rm, realpath } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { chromium } from 'playwright';
import { createAppServer } from '../../apps/api/src/server.js';
import { apiAccessConfig } from '../../apps/api/src/access.js';
import { archiveFromPath } from '../../apps/api/src/client.js';
import { inspectArchive } from '../../apps/api/src/archive.js';

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
  assert.match(await page.locator('#form-error').innerText(), /앱 배포 설정이 아직 준비되지/);
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

test('application updates keep app and environment fixed across all source formats, review diffs, and start the frozen preview', { timeout: 90000 }, async (t) => {
  const { page, origin, errors, stateDirectory } = await start(t, { service: null });
  const baseline = { id: 'deployed-v1', app: 'stable-app', target_id: 'same-target', status: 'succeeded', source_commit: 'a'.repeat(40) };
  const application = { id: 'application-1', app: 'stable-app', target_id: 'same-target', status: 'ready', current_deployment_state: 'verified',
    current_deployment: baseline, latest_deployment: { ...baseline, id: 'failed-v2', status: 'failed' } };
  const previews = [], uploads = [], starts = []; let failPreview = true, rejectStart = true;
  const json = (route, data, status = 200, headers = {}) => route.fulfill({ status, contentType: 'application/json', headers, body: JSON.stringify(data) });
  await page.route('**/api/v1/applications?*', (route) => json(route, { items: [application], next_marker: null }));
  await page.route('**/api/v1/applications/application-1', (route) => json(route, application));
  await page.route('**/api/v1/applications/application-1/updates', async (route) => {
    const request = route.request();
    const form = await new Request('http://fixture', { method: 'POST', headers: { 'content-type': request.headers()['content-type'] }, body: request.postDataBuffer() }).formData();
    uploads.push({ key: request.headers()['idempotency-key'], keys: [...form.keys()], repository: form.get('repository_url'), paths: form.get('paths') });
    if (failPreview) { failPreview = false; return json(route, { error: { message: '미리보기 응답을 확인하지 못했습니다.' } }, 503); }
    const preview = { id: `preview-${previews.length + 1}`, application_id: application.id, app: application.app, target_id: application.target_id,
      status: 'preview', base_deployment_id: baseline.id, expires_at: '2099-01-01T00:00:00Z', baseline_kind: previews.length ? 'deployed' : 'submitted',
      changes: { added: previews.length ? [] : ['<img src=x onerror=alert(1)>.js'], modified: previews.length ? [] : ['index.js'], deleted: previews.length ? [] : ['old.js'], unchanged: 2 },
      no_changes: previews.length > 0, source_origin: form.has('repository_url') ? { repository: form.get('repository_url'), sha: 'b'.repeat(40) } : null };
    previews.push(preview); return json(route, preview, 200, { location: `/api/v1/deployments/${preview.id}` });
  });
  await page.route('**/api/v1/deployments/preview-*/start', async (route) => {
    const id = new URL(route.request().url()).pathname.split('/').at(-2), body = route.request().postDataJSON();
    starts.push({ id, body });
    if (rejectStart) { rejectStart = false; return json(route, { error: { message: '다른 실행을 확인한 뒤 다시 시작하세요.' } }, 409); }
    const preview = previews.find((row) => row.id === id);
    preview.status = preview.no_changes && !body.rebuild ? 'unchanged' : 'succeeded';
    return json(route, preview.status === 'unchanged' ? preview : { resource_id: id, status: 'accepted' }, preview.status === 'unchanged' ? 200 : 202,
      { location: `/api/v1/deployments/${id}` });
  });
  await page.route('**/api/v1/deployments/preview-*', (route) => json(route, previews.find((row) => row.id === new URL(route.request().url()).pathname.split('/').at(-1))));
  await page.route('**/api/v1/deployments/deployed-v1/source?variant=deployed', (route) => json(route, { error: { message: '이전 배포의 최종 소스가 보관되어 있지 않습니다.' } }, 404));
  await page.goto(origin); await page.waitForFunction(() => document.querySelector('#session-note').textContent.includes('까지'));
  const open = async () => {
    await page.locator('[data-view="history"]').click();
    await page.getByRole('button', { name: 'stable-app 앱 상세·업데이트' }).click();
    await page.locator('#application-update').click();
    assert.equal(await page.locator('#target-section').isVisible(), false);
  };
  await page.locator('[data-view="history"]').click();
  assert.match(await page.locator('#applications-list').innerText(), /현재 서비스: deployed-v1/);
  assert.match(await page.locator('#applications-list').innerText(), /최근 시도: 실행 실패 · failed-v2/);
  await page.getByRole('button', { name: 'stable-app 앱 상세·업데이트' }).click();
  await page.getByRole('button', { name: 'stable-app deployed-v1 최종 소스 다운로드' }).click();
  await page.waitForFunction(() => document.querySelector('#application-versions').textContent.includes('최종 소스가 보관되어 있지'));
  await page.locator('#application-update').click();
  await page.locator('#repository-url').fill('https://github.com/example/different-repository');
  const review = () => page.locator('#deploy-form button[type="submit"]').click();
  await review(); await page.waitForFunction(() => !document.querySelector('#form-error').hidden);
  await review(); await page.waitForFunction(() => !document.querySelector('#update-review').hidden);
  assert.equal(uploads[0].key, uploads[1].key, 'preview failure retries the same request key');
  assert.equal(await page.locator('#review-app').innerText(), 'stable-app');
  assert.match(await page.locator('#update-baseline').innerText(), /AI 수정 후 최종 소스는 보관되어 있지/);
  assert.match(await page.locator('#update-diff-summary').innerText(), /추가 1 · 수정 1 · 삭제 1/);
  assert.match(await page.locator('#update-origin').innerText(), /bbbbbbbb/);
  assert.equal(await page.locator('#update-changes img').count(), 0, 'file paths render as text');
  if (process.env.CI_OUTPUT_DIR) {
    await mkdir(process.env.CI_OUTPUT_DIR, { recursive: true });
    await page.screenshot({ path: join(process.env.CI_OUTPUT_DIR, 'application-update-desktop.png'), fullPage: true });
    await page.setViewportSize({ width: 390, height: 844 });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    await page.screenshot({ path: join(process.env.CI_OUTPUT_DIR, 'application-update-mobile.png'), fullPage: true });
    await page.setViewportSize({ width: 1440, height: 1000 });
  }
  await page.locator('#deploy-button').click();
  await page.waitForFunction(() => !document.querySelector('#request-error').hidden);
  assert.equal(await page.locator('#review-panel').isVisible(), true, 'rejected start stays in review');
  await page.locator('#deploy-button').click();
  await page.waitForFunction(() => document.querySelector('#run-state').textContent === '앱 배포 완료');
  assert.equal(starts[0].id, starts[1].id);
  assert.equal(uploads.length, 2, 'start retry never reuploads or creates another preview');
  const files = join(stateDirectory, 'renamed-folder'); await mkdir(files); await writeFile(join(files, 'index.js'), 'frozen source');
  const zip = await archiveFromPath(files);
  await open(); await page.locator('#archive').setInputFiles({ name: 'renamed-archive.zip', mimeType: 'application/zip', buffer: zip.bytes });
  await review(); await page.waitForFunction(() => !document.querySelector('#update-review').hidden);
  assert.equal(await page.locator('#review-app').innerText(), 'stable-app');
  assert.equal(await page.locator('#update-rebuild').isChecked(), false);
  assert.equal(await page.locator('#deploy-button').innerText(), '변경 없음으로 완료');
  await page.locator('#deploy-button').click(); await page.waitForFunction(() => document.querySelector('#run-state').textContent.includes('실행 생략'));
  assert.deepEqual(starts.at(-1).body, { rebuild: false });
  await open(); await page.locator('#folder').setInputFiles(files);
  await review(); await page.waitForFunction(() => !document.querySelector('#update-review').hidden);
  assert.equal(await page.locator('#review-app').innerText(), 'stable-app');
  await page.locator('#update-rebuild').check(); await page.locator('#deploy-button').click();
  await page.waitForFunction(() => document.querySelector('#run-state').textContent === '앱 배포 완료');
  assert.deepEqual(starts.at(-1).body, { rebuild: true });
  assert.ok(uploads.every((upload) => upload.keys.every((key) => ['repository_url', 'archive', 'files', 'paths'].includes(key))));
  assert.equal(uploads.at(-1).paths, '["index.js"]');
  assert.equal(previews.length, 3);
  await open(); await page.locator('#repository-url').fill('https://github.com/example/expiry-retry');
  await review(); await page.waitForFunction(() => !document.querySelector('#update-review').hidden);
  const expiredKey = uploads.at(-1).key, startCount = starts.length;
  await page.evaluate(() => { window.originalNow = Date.now; Date.now = () => 4102444800000; });
  await page.locator('#deploy-button').click();
  assert.match(await page.locator('#request-error').innerText(), /만료/);
  assert.equal(starts.length, startCount, 'locally expired preview never dispatches');
  await page.evaluate(() => { Date.now = window.originalNow; delete window.originalNow; });
  await review(); await page.waitForFunction(() => !document.querySelector('#deploy-button').disabled);
  assert.notEqual(uploads.at(-1).key, expiredKey, 'same-source re-review after expiry creates a fresh preview key');
  assert.deepEqual(errors, []);
});

test('update review ignores stale source responses and uncertain service state blocks a new update', { timeout: 45000 }, async (t) => {
  const { page, origin, errors } = await start(t, { service: null });
  const application = { id: 'app-guard', app: 'guard-app', target_id: 'guard-target', status: 'ready', current_deployment_state: 'verified',
    current_deployment: { id: 'old', app: 'guard-app', target_id: 'guard-target', status: 'succeeded' } };
  const json = (route, data) => route.fulfill({ contentType: 'application/json', body: JSON.stringify(data) });
  await page.route('**/api/v1/applications?*', (route) => json(route, new URL(route.request().url()).searchParams.has('marker')
    ? { items: [{ ...application, id: 'app-next', app: 'next-app' }], next_marker: null }
    : { items: [application], next_marker: 'app-guard' }));
  await page.route('**/api/v1/applications/app-guard', (route) => json(route, application));
  let delayed;
  await page.route('**/api/v1/applications/app-guard/updates', (route) => { delayed = route; });
  await page.goto(origin); await page.waitForFunction(() => document.querySelector('#session-note').textContent.includes('까지'));
  await page.locator('[data-view="history"]').click(); await page.locator('#applications-more').click();
  await page.getByRole('button', { name: 'next-app 앱 상세·업데이트' }).waitFor();
  assert.equal(await page.locator('#applications-list > li').count(), 2);
  await page.getByRole('button', { name: 'guard-app 앱 상세·업데이트' }).click();
  await page.locator('#application-update').click(); await page.locator('#repository-url').fill('https://github.com/example/first');
  const pending = page.waitForRequest('**/api/v1/applications/app-guard/updates');
  await page.locator('#deploy-form button[type="submit"]').click(); await pending;
  await page.locator('#repository-url').fill('https://github.com/example/second');
  await delayed.fulfill({ contentType: 'application/json', headers: { location: '/api/v1/deployments/stale-preview' }, body: JSON.stringify({
    id: 'stale-preview', app: application.app, target_id: application.target_id, status: 'preview', base_deployment_id: 'old',
    expires_at: '2099-01-01T00:00:00Z', baseline_kind: 'deployed', no_changes: true, changes: { added: [], modified: [], deleted: [], unchanged: 1 } }) });
  await page.waitForFunction(() => !document.querySelector('#deploy-form button[type="submit"]').disabled);
  assert.equal(await page.locator('#review-panel').isVisible(), false);
  await page.locator('#cancel-update').click();
  application.current_deployment_state = 'unverified';
  await page.locator('[data-view="history"]').click(); await page.getByRole('button', { name: 'guard-app 앱 상세·업데이트' }).click();
  await page.waitForFunction(() => document.querySelector('#application-detail-message').textContent.includes('운영자 확인'));
  assert.equal(await page.locator('#application-update').isDisabled(), true);
  assert.deepEqual(errors, []);
});

test('browser update crosses real preview/start/source HTTP routes and reuses the registered application', { timeout: 60000 }, async (t) => {
  const submissions = [], publications = new Map(); let registrations = 0, version = 1;
  const service = { targetId: 'runtime-aws', targetIds: [], allowTarget() {},
    async deploy(input) {
      submissions.push(input); const run = String(submissions.length), sha = String(submissions.length).repeat(40);
      publications.set(run, { run_id: run, app: input.app, tenant: 'demo', target_id: input.target_id,
        source_commit: sha, artifact_id: 900 + submissions.length, producer_attempt: 1 });
      return { run_id: run, source_commit: sha };
    },
    status: async (id) => ({ state: 'published', publication: publications.get(id) }),
    sourceFiles: async (publication) => submissions[Number(publication.run_id) - 1].files };
  const applicationAdapter = { targets: { 'runtime-aws': { provider: 'aws', automaticDelivery: true } },
    describe: (environment, app) => ({ id: `app-${app}`, app, target_id: `app-${app}`, environment_target_id: environment, provider: 'aws' }),
    register: async () => { registrations++; return { status: 'ready' }; },
    deployPublished: async (_application, args) => ({ cd: { state: 'deployed', deployed: true, revision: args.sourceCommit },
      public_http: { state: 'succeeded', verified_at: new Date().toISOString(), url: 'https://example.test' } }) };
  const { page, origin, errors } = await start(t, { service, applicationAdapter, target: { id: 'runtime-aws', provider: 'aws' },
    sourceLoader: async (repository) => ({ source: { type: 'github', repository, sha: 'b'.repeat(40) },
      files: [{ path: 'app.js', content: Buffer.from(`version ${version}`) }] }) });
  await page.goto(origin); await page.waitForFunction(() => document.querySelector('#connection-status').textContent.includes('URL 확인'));
  await page.locator('#repository-url').fill('https://github.com/example/stable-repo');
  await page.locator('#deploy-form button[type="submit"]').click(); await page.locator('#deploy-button').click();
  await page.waitForFunction(() => document.querySelector('#run-state').textContent === '앱 배포 완료');
  version = 2;
  await page.locator('[data-view="history"]').click(); await page.getByRole('button', { name: 'stable-repo 앱 상세·업데이트' }).click();
  await page.locator('#application-update').click(); await page.locator('#repository-url').fill('https://github.com/example/renamed-repo');
  await page.locator('#deploy-form button[type="submit"]').click();
  await page.waitForFunction(() => !document.querySelector('#update-review').hidden);
  assert.equal(await page.locator('#review-app').innerText(), 'stable-repo');
  assert.match(await page.locator('#update-diff-summary').innerText(), /수정 1/);
  assert.match(await page.locator('#update-baseline').innerText(), /검증된 최종 소스/);
  assert.equal(submissions.length, 1, 'preview does not dispatch');
  version = 3;
  await page.locator('#deploy-button').click();
  await page.waitForFunction(() => document.querySelector('#run-state').textContent === '앱 배포 완료' && document.querySelector('#run-binding').textContent.includes('CI run: 2'));
  assert.equal(submissions.length, 2); assert.equal(registrations, 1);
  assert.equal(submissions[1].app, submissions[0].app); assert.equal(submissions[1].target_id, submissions[0].target_id);
  assert.equal(submissions[1].files[0].content.toString(), 'version 2', 'start uses server-frozen source without refetching GitHub');
  await page.locator('[data-view="history"]').click(); await page.getByRole('button', { name: 'stable-repo 앱 상세·업데이트' }).click();
  const downloadEvent = page.waitForEvent('download');
  await page.locator('#application-versions .application-version').first().getByRole('button', { name: /최종 소스 다운로드/ }).click();
  const download = await downloadEvent;
  assert.equal(await download.failure(), null);
  const files = await inspectArchive(await readFile(await download.path()));
  assert.equal(files.find((file) => file.path === 'app.js').content.toString(), 'version 2');
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
  assert.match(await page.locator('#connection-status').innerText(), /OpenStack.*앱 배포 설정.*준비되지/);
  await page.getByRole('radio', { name: /클라우드/ }).check();
  assert.equal(await page.locator('#provider-field').isVisible(), false);
  const review = () => page.locator('#deploy-form button[type="submit"]').click();
  const run = () => page.locator('#deploy-button').click();
  await page.locator('#repository-url').fill('https://github.com/example/browser-demo.git/');
  await review();
  assert.equal(await page.locator('#review-app').innerText(), 'browser-demo');
  await page.getByRole('radio', { name: /온프레미스/ }).check();
  assert.equal(await page.locator('#review-panel').isVisible(), false, 'changing environment invalidates the reviewed request');
  await page.locator('#provider').selectOption('proxmox');
  await review();
  assert.match(await page.locator('#form-error').innerText(), /Proxmox.*앱 배포 설정.*준비되지/);
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
  assert.equal(submitted[0].app, 'browser-demo');
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
  await review();
  assert.equal(await page.locator('#review-app').innerText(), 'archive-app');
  await run();
  await page.waitForFunction(() => document.querySelector('#run-meta').textContent.includes('archive-app') && document.querySelector('#run-state').textContent === '앱 배포 완료', undefined, { timeout: 30000 });
  assert.equal(submitted[1].files[0].content.toString(), 'source from zip');
  await writeFile(join(files, 'index.js'), 'source from folder');
  await page.locator('#folder').setInputFiles(files);
  await review();
  assert.equal(await page.locator('#review-app').innerText(), 'fixture');
  await run();
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

test('dashboard resumes the same published deployment without another upload or CI dispatch', { timeout: 60000 }, async (t) => {
  let registrations = 0, submissions = 0, deliveries = 0;
  const commit = 'a'.repeat(40), image = `ghcr.io/example/calculator@sha256:${'b'.repeat(64)}`;
  const describe = (environment_target_id, app) => ({ id: 'app-calculator', target_id: 'app-calculator', environment_target_id, app, provider: 'aws' });
  const applicationAdapter = {
    targets: { 'runtime-aws': { provider: 'aws', automaticDelivery: true } }, describe,
    register: async (application) => { registrations++; return { ...application, status: 'ready' }; },
    deployPublished: async (_application, args) => {
      deliveries++;
      assert.equal(args.publication.images.web, image);
      if (deliveries === 1) return { cd: { state: 'unknown', deployed: false }, public_http: { state: 'not_run' },
        error: { code: 'APPLICATION_ROUTE_RECONCILE_REQUIRED', outcome_unknown: true } };
      return { cd: { state: 'deployed', revision: 'c'.repeat(40), deployed: true },
        public_http: { state: 'succeeded', verified_at: new Date().toISOString(), url: 'https://calculator.example.test/' } };
    },
  };
  const service = { targetId: 'runtime-aws', targetIds: [], allowTarget: () => {},
    deploy: async () => { submissions++; return { run_id: 123, source_commit: commit }; },
    status: async () => ({ state: 'published', publication: { run_id: '123', app: 'calculator', target_id: 'app-calculator',
      source_commit: commit, artifact_id: 456, producer_attempt: 1, images: { web: image } } }),
  };
  const { page, origin, errors, requests } = await start(t, { service, target: { id: 'runtime-aws', provider: 'aws' },
    applicationAdapter, sourceLoader: async () => ({ files: [{ path: 'app.js', content: Buffer.from('user calculator source') }] }) });
  await page.goto(origin);
  await page.waitForFunction(() => document.querySelector('#connection-status').textContent.includes('URL 확인'));
  await page.locator('#repository-url').fill('https://github.com/example/calculator');
  await page.locator('#deploy-form button[type="submit"]').click();
  await page.locator('#deploy-button').click();
  await page.waitForFunction(() => !document.querySelector('#resume-run').hidden, undefined, { timeout: 30000 });
  const identity = await page.locator('#run-meta').innerText();
  await page.getByRole('button', { name: '게시된 이미지로 배포 이어가기' }).click();
  await page.waitForFunction(() => document.querySelector('#run-state').textContent === '앱 배포 완료', undefined, { timeout: 30000 });
  assert.equal(await page.locator('#run-meta').innerText(), identity);
  assert.equal(await page.locator('#resume-run').isVisible(), false);
  assert.equal(await page.locator('#application-link').getAttribute('href'), 'https://calculator.example.test/');
  assert.deepEqual([registrations, submissions, deliveries], [1, 1, 2]);
  assert.equal(requests.filter((row) => row.method === 'POST' && row.path.endsWith('/actions')).length, 1);
  assert.deepEqual(errors, []);
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
    assert.equal(await page.locator('#review-app').innerText(), 'provider-fixture');
    await page.locator('#deploy-button').click();
    await page.waitForFunction(() => document.querySelector('#request-error').textContent.includes('전용입니다'));
    assert.equal(submissions.length, 0); assert.equal(deliveries.length, 0);
    await page.locator('#repository-url').fill(`https://github.com/example/${app}`);
    await page.locator('#deploy-form button[type="submit"]').click();
    assert.equal(await page.locator('#review-app').innerText(), app);
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
