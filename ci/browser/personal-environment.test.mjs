import assert from 'node:assert/strict';
import { once } from 'node:events';
import { readFile } from 'node:fs/promises';
import { createServer } from 'node:http';
import test from 'node:test';
import { chromium } from 'playwright';

test('personal OpenStack management uses HTTP fixture states, preserves deploy source, and requires data deletion consent', { timeout: 60000 }, async (t) => {
  const calls = [], targets = [{ id: 'ready-target', label: '연구실 OpenStack', provider: 'openstack', status: 'ready', deployable: true,
    project_id: 'project-a', application_count: 1, last_seen_at: '2026-10-03T01:00:00Z', client_version: '1.0.0', registration_stage: 'ready' },
  { id: 'retry-target', label: '대기 중 OpenStack', provider: 'openstack', status: 'pending', deployable: false,
    application_count: 0, registration_stage: 'enrollment' }];
  let retryEnrollmentAttempts = 0;
  let failNextRecoveredList = false;
  let failNextCreatedList = false;
  let ownerConfigured = false;
  const respond = (response, status, body, location) => {
    response.writeHead(status, { 'content-type': 'application/json', ...(location ? { location } : {}) }); response.end(JSON.stringify(body));
  };
  const server = createServer(async (request, response) => {
    const url = new URL(request.url, 'http://localhost'), path = url.pathname;
    const chunks = []; for await (const chunk of request) chunks.push(chunk); const bytes = Buffer.concat(chunks);
    const json = () => bytes.length ? JSON.parse(bytes.toString('utf8')) : {};
    if (path === '/api/v1/sessions') return respond(response, 200, { expires_at: '2099-01-01T00:00:00Z' });
    if (path === '/api/v1/preferences') return respond(response, 200, { view: 'deploy', environment: 'cloud', provider: '' });
    if (path === '/api/v1/profiles') return respond(response, 200, { items: [] });
    if (path === '/api/v1/options') return respond(response, 200, { items: [{ environment: 'cloud', provider: 'aws', label: 'AWS', available: true }] });
    if (path === '/api/v1/connections') return respond(response, 200, { items: [] });
    if (path === '/api/v1/applications') return respond(response, 200, { items: [] });
    if (path === '/api/v1/deployments') {
      if (request.method === 'GET') return respond(response, 200, { items: [] });
      const form = await new Request('http://localhost', { method: 'POST', body: bytes, headers: request.headers }).formData();
      calls.push({ kind: 'deployment', environment: form.get('environment'), provider: form.get('provider'), target: form.get('target_id') });
      return respond(response, 202, { id: 'deployment-1', status: 'accepted' }, '/api/v1/deployments/deployment-1');
    }
    if (path === '/api/v1/deployments/deployment-1') return respond(response, 200, { id: 'deployment-1', status: 'succeeded', target_id: 'ready-target', steps: [] });
    if (path === '/api/v1/owners') {
      assert.equal(request.headers['x-railshot-request'], 'dashboard');
      if (request.method === 'POST') {
        if (ownerConfigured) return respond(response, 200, { id: 'owner-1', recovery_configured: true });
        ownerConfigured = true;
        return respond(response, 201, { id: 'owner-1', recovery_key: 'recover-this-once', recovery_key_once: true });
      }
      return respond(response, 200, { id: ownerConfigured ? 'owner-1' : null, recovery_configured: ownerConfigured });
    }
    if (path === '/api/v1/recoveries') {
      assert.equal(request.headers['x-railshot-request'], 'dashboard');
      const recoveryKey = json().recovery_key;
      assert.ok(['recover-this-once', 'recover-list-failure'].includes(recoveryKey));
      ownerConfigured = true;
      if (recoveryKey === 'recover-list-failure') failNextRecoveredList = true;
      return respond(response, 200, { id: 'owner-1', recovery_key: 'rotated-recovery-key' });
    }
    if (path === '/api/v1/targets' && request.method === 'GET') {
      assert.equal(request.headers['x-railshot-request'], 'dashboard');
      if (url.searchParams.get('scope') === 'owned' && (failNextRecoveredList || failNextCreatedList)) {
        failNextRecoveredList = false; failNextCreatedList = false;
        return respond(response, 503, { error: { message: '개인 환경 목록 서버가 응답하지 않습니다.' } });
      }
      return respond(response, 200, { items: url.searchParams.get('scope') === 'owned' ? targets : [] });
    }
    if (path === '/api/v1/targets' && request.method === 'POST') {
      assert.equal(request.headers['x-railshot-request'], 'dashboard');
      const target = { id: 'pending-target', label: json().label, provider: 'openstack', status: 'pending', deployable: false, application_count: 0 };
      targets.push(target); failNextCreatedList = true; calls.push({ kind: 'create-target', body: json() }); return respond(response, 201, target);
    }
    const targetMatch = path.match(/^\/api\/v1\/targets\/([^/]+)(?:\/(enrollments|observations|applications|plans|operations))?$/);
    if (targetMatch) {
      assert.equal(request.headers['x-railshot-request'], 'dashboard');
      const [, id, child] = targetMatch, target = targets.find((item) => item.id === id);
      if (!target) return respond(response, 404, { error: { message: 'missing target' } });
      if (!child) return respond(response, 200, target);
      if (child === 'enrollments') {
        if (id === 'retry-target') {
          retryEnrollmentAttempts += 1;
          if (retryEnrollmentAttempts === 1) return respond(response, 409, { error: { code: 'CAPABILITY_UNAVAILABLE', message: '개인 환경 설치 서버가 구성되지 않았습니다.' } });
        }
        return respond(response, 201, { id: `enrollment-${id}`, target_id: id, expires_at: '2099-01-01T00:00:00Z', install_command: `curl -fsSL https://example.test/${id}/install | sh` });
      }
      if (child === 'observations') return respond(response, 200, { target_id: id, stale_after_seconds: 90, metrics: { cpu_percent: { state: 'ready', value: 10, observed_at: '2026-10-03T01:00:00Z' }, memory_percent: { state: 'ready', value: 20, observed_at: '2026-10-03T01:00:00Z' }, disk_percent: { state: 'ready', value: 30, observed_at: '2026-10-03T01:00:00Z' } } });
      if (child === 'applications') return respond(response, 200, { items: [{ id: 'app-1', name: 'railshot-app', status: 'running', deployed_at: '2026-10-03T01:00:00Z' }] });
      if (child === 'plans') { calls.push({ kind: 'delete-plan', body: json() }); return respond(response, 201, { id: 'delete-plan', target_id: id, action: 'delete', plan_hash: 'signed-plan', expires_at: '2099-01-01T00:00:00Z', resources: ['railshot-app', '앱 전용 데이터'], retained: ['고객 별도 서비스', '기반 VM', '공유 데이터'], blockers: [] }); }
      if (child === 'operations') { calls.push({ kind: 'delete-operation', body: json() }); return respond(response, 202, { id: 'operation-1', status: 'running' }, '/api/v1/operations/operation-1'); }
    }
    if (path === '/api/v1/operations/operation-1') return respond(response, 200, { id: 'operation-1', status: 'succeeded', message: '삭제 완료' });
    const files = { '/': ['index.html', 'text/html'], '/app.js': ['app.js', 'text/javascript'], '/styles.css': ['styles.css', 'text/css'], '/contracts/application.mjs': ['../../contracts/application.mjs', 'text/javascript'] };
    if (!files[path]) return respond(response, 404, { error: { message: 'fixture path unavailable' } });
    response.writeHead(200, { 'content-type': files[path][1] }); response.end(await readFile(new URL('../../apps/dashboard/' + files[path][0], import.meta.url)));
  });
  server.listen(0, '127.0.0.1'); await once(server, 'listening');
  const browser = await chromium.launch({ executablePath: process.env.CHROME_EXECUTABLE || undefined });
  t.after(async () => { await browser.close(); await new Promise((resolve) => { server.close(resolve); server.closeAllConnections(); }); });
  const page = await browser.newPage(); page.setDefaultTimeout(10000); const origin = `http://127.0.0.1:${server.address().port}`;
  const errors = []; page.on('pageerror', (error) => errors.push(error.message)); await page.goto(origin);

  await page.getByRole('button', { name: '개인 배포환경 관리' }).click();
  await page.getByLabel('환경 이름').fill('새 OpenStack');
  await page.getByRole('button', { name: '등록 시작' }).click();
  await page.getByText('복구 키를 안전한 곳에 저장하세요').waitFor();
  assert.equal(await page.locator('#environment-recovery-key-value').innerText(), 'recover-this-once');
  await page.getByText('관리 호스트 설치').waitFor();
  assert.match(await page.locator('#environment-install-command').innerText(), /curl -fsSL/);
  assert.deepEqual(calls.find((call) => call.kind === 'create-target').body, { label: '새 OpenStack', provider: 'openstack' });
  assert.match(await page.locator('#environment-register-message').innerText(), /현재 브라우저에 관리 권한이 연결되었습니다/);
  assert.match(await page.locator('#environment-owner-note').innerText(), /현재 브라우저에 개인 환경 관리 권한이 연결되어 있습니다/);
  assert.match(await page.locator('#environment-owned-message').innerText(), /목록 조회 실패/);
  await page.getByRole('button', { name: '새 OpenStack' }).waitFor();
  await page.getByText('복구 키로 관리권 복구').click();
  await page.locator('#environment-recovery-input').fill('recover-this-once');
  await page.getByRole('button', { name: '관리권 복구' }).click();
  await page.getByText('이 브라우저에서 관리권을 복구했습니다.').waitFor();
  assert.equal(await page.locator('#environment-recovery-key-value').innerText(), 'rotated-recovery-key');
  assert.match(await page.locator('#environment-recover-message').innerText(), /등록한 환경 3개를 불러왔습니다/);
  assert.equal(await page.locator('#environment-owned-list-panel').evaluate((node) => node.classList.contains('recovery-highlight')), true);
  assert.equal(await page.locator('#environment-owned-items button.active').innerText(), '연구실 OpenStack');
  assert.equal(await page.locator('#environment-install-command').isHidden(), true, '복구 전 설치 명령은 새 소유권에 남지 않아야 합니다.');
  assert.equal(await page.locator('#environment-copy-command').isDisabled(), true);

  await page.getByRole('button', { name: '대기 중 OpenStack' }).click();
  await page.getByText('등록 대기 환경입니다. 설치 명령을 다시 발급할 수 있습니다.').waitFor();
  assert.equal(await page.locator('#environment-copy-command').isDisabled(), true);
  await page.getByRole('button', { name: '설치 명령 재발급' }).click();
  await page.getByText('개인 환경 설치 서버가 구성되지 않았습니다.').waitFor();
  assert.equal(await page.locator('#environment-enrollment').isHidden(), false);
  assert.equal(await page.locator('#environment-install-command').isHidden(), true);
  assert.equal(await page.locator('#environment-copy-command').isDisabled(), true);
  if (process.env.PERSONAL_UI_SCREENSHOT) await page.screenshot({ path: process.env.PERSONAL_UI_SCREENSHOT, fullPage: true });
  await page.getByRole('button', { name: '설치 명령 재발급' }).click();
  await page.locator('#environment-install-command').getByText(/retry-target/).waitFor();
  assert.equal(await page.locator('#environment-copy-command').isDisabled(), false);
  await page.getByRole('button', { name: '설치 명령 복사' }).click();

  await page.locator('#environment-recovery-input').fill('recover-list-failure');
  await page.getByRole('button', { name: '관리권 복구' }).click();
  await page.getByText('관리권은 복구했으나 환경 목록을 불러오지 못했습니다. 목록 새로고침으로 다시 확인하세요.').waitFor();
  assert.match(await page.locator('#environment-owned-message').innerText(), /목록 조회 실패/);
  assert.doesNotMatch(await page.locator('#environment-recover-message').innerText(), /환경 0개를 불러왔습니다/);
  await page.getByRole('button', { name: '목록 새로고침' }).click();
  await page.getByRole('button', { name: '연구실 OpenStack' }).waitFor();

  await page.getByRole('button', { name: '새 배포' }).click();
  await page.locator('#repository-url').fill('https://github.com/example/my-app');
  await page.locator('[name="environment"][value="onprem"]').check();
  await page.locator('#provider').selectOption('__new_openstack__');
  await page.getByText('개인 배포환경 관리로 이동').click();
  await page.getByRole('button', { name: '새 배포' }).click();
  assert.equal(await page.locator('#repository-url').inputValue(), 'https://github.com/example/my-app');
  await page.locator('#provider').selectOption('ready-target');
  await page.getByRole('button', { name: /선택 내용 확인/ }).click();
  await page.getByRole('button', { name: '배포 시작' }).click();
  assert.deepEqual(calls.find((call) => call.kind === 'deployment'), { kind: 'deployment', environment: 'onprem', provider: 'openstack', target: 'ready-target' });

  await page.getByRole('button', { name: '개인 배포환경 관리' }).click();
  await page.getByRole('button', { name: '연구실 OpenStack' }).click();
  await page.getByRole('button', { name: '환경 삭제' }).click();
  await page.getByRole('dialog').getByText('앱 전용 데이터', { exact: true }).waitFor();
  assert.deepEqual(calls.find((call) => call.kind === 'delete-plan').body, { action: 'delete', delete_data: true });
  await page.locator('#environment-delete-confirmation').fill('연구실 OpenStack');
  await page.locator('#environment-delete-data-consent').check();
  await page.getByRole('button', { name: '삭제 시작' }).click();
  assert.deepEqual(calls.find((call) => call.kind === 'delete-operation').body, { action: 'delete', plan_id: 'delete-plan', plan_hash: 'signed-plan', confirmation: '연구실 OpenStack', delete_data: true });
  assert.deepEqual(errors, []);
});
