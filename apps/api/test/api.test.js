import assert from 'node:assert/strict';
import { Readable } from 'node:stream';
import { test } from 'node:test';
import { createApiHandler, loadConfig } from '../src/server.js';
import { createGithubClient } from '../src/github.js';

const calls = [];
const fetchGithub = async (url, options = {}) => {
  calls.push({ url, options });
  if (url.includes('/contents/apps/demo/memo-sqlite')) return Response.json([{ name: 'app.py' }]);
  if (url.endsWith('/dispatches')) return Response.json({ workflow_run_id: 123, html_url: 'https://github.com/Jasmin-Softbank/railshot-apps/actions/runs/123' });
  if (url.endsWith('/actions/runs/123')) return Response.json({
    id: 123, event: 'workflow_dispatch', path: '.github/workflows/railshot-deploy.yml@main',
    status: 'completed', conclusion: 'success', head_sha: 'abc123', html_url: 'https://github.com/Jasmin-Softbank/railshot-apps/actions/runs/123',
  });
  if (url.includes('/actions/runs/123/jobs')) return Response.json({ total_count: 3, jobs: [
    { name: 'loop', status: 'completed', conclusion: 'success', steps: [{ name: 'Fix loop (intake → agent → gate, at most 3 attempts)', status: 'completed', conclusion: 'success' }] },
    { name: 'release', status: 'completed', conclusion: 'success', steps: [{ name: 'Build, push by digest, render', status: 'completed', conclusion: 'success' }] },
    { name: 'gitops', status: 'completed', conclusion: 'success', steps: [{ name: 'Verify the public URL, revert on failure (LKG)', status: 'completed', conclusion: 'skipped' }] },
  ] });
  return Response.json({ message: 'Not Found' }, { status: 404 });
};

const github = createGithubClient({ token: 'test-token', owner: 'Jasmin-Softbank', repo: 'railshot-apps', ref: 'main', tenant: 'demo', workflow: 'railshot-deploy.yml' }, fetchGithub);
const handler = createApiHandler({ github });

async function request(method, url, body, headers = {}) {
  const incoming = Readable.from(body === undefined ? [] : [JSON.stringify(body)]);
  incoming.method = method;
  incoming.url = url;
  incoming.headers = { host: '127.0.0.1:4182', 'content-type': 'application/json', 'x-railshot-request': '1', origin: 'http://127.0.0.1:4181', ...headers };
  const outgoing = {
    writeHead(status) { this.status = status; return this; },
    end(value) { this.body = JSON.parse(value); return this; },
  };
  await handler(incoming, outgoing);
  return outgoing;
}

function post(body, headers) {
  return request('POST', '/api/deployments', body, headers);
}

test('등록된 AWS 앱만 실행하고 워크플로의 두 입력만 보낸다', async () => {
  const response = await post({ app: 'memo-sqlite', target: { environment: 'cloud', provider: 'aws' } });
  assert.equal(response.status, 202);
  assert.equal(response.body.runId, 123);
  const dispatch = calls.find((call) => call.url.endsWith('/dispatches'));
  assert.deepEqual(JSON.parse(dispatch.options.body), { ref: 'main', inputs: { tenant: 'demo', app: 'memo-sqlite' } });
});

test('온프레미스 및 소스 업로드 요청은 Actions를 시작하지 않는다', async () => {
  const count = calls.length;
  assert.equal((await post({ app: 'memo-sqlite', target: { environment: 'onprem', provider: 'openstack' } })).status, 422);
  assert.equal((await post({ app: 'memo-sqlite', target: { environment: 'cloud', provider: 'aws' }, source: { type: 'zip' } })).status, 400);
  assert.equal(calls.length, count);
});

test('등록되지 않은 앱은 Actions 실행 전에 거부한다', async () => {
  const response = await post({ app: 'unknown-app', target: { environment: 'cloud', provider: 'aws' } });
  assert.equal(response.status, 404);
  assert.match(response.body.error, /등록된 앱/);
});

test('출처와 요청 헤더를 확인한다', async () => {
  assert.equal((await post({ app: 'memo-sqlite', target: { environment: 'cloud', provider: 'aws' } }, { origin: 'https://other.example' })).status, 403);
  assert.equal((await post({ app: 'memo-sqlite', target: { environment: 'cloud', provider: 'aws' } }, { 'x-railshot-request': 'wrong' })).status, 403);
});

test('Actions의 세부 단계와 공개 HTTP 검사 미실행을 그대로 전달한다', async () => {
  const response = await request('GET', '/api/deployments/123');
  assert.equal(response.status, 200);
  const run = response.body;
  assert.equal(run.jobs[1].steps[0].name, 'Build, push by digest, render');
  assert.equal(run.publicHttpCheck, 'not_run');
  assert.equal(run.requiredJobsSucceeded, true);
  assert.equal(run.target.provider, 'aws');
});

test('실행 ID가 없는 접수 결과를 성공한 배포로 만들지 않는다', async () => {
  const client = createGithubClient({ token: 'test-token', owner: 'Jasmin-Softbank', repo: 'railshot-apps', ref: 'main', tenant: 'demo', workflow: 'railshot-deploy.yml' }, async (url) => {
    if (url.includes('/contents/')) return Response.json([{ name: 'app.py' }]);
    return new Response(null, { status: 204 });
  });
  const result = await client.dispatch('memo-sqlite');
  assert.equal(result.runId, null);
  assert.equal(result.state, 'accepted_untracked');
});

test('토큰 및 tenant 설정 오류를 시작 전에 거부한다', () => {
  assert.throws(() => loadConfig({}), /GITHUB_TOKEN/);
  assert.throws(() => loadConfig({ GITHUB_TOKEN: 'token', RAILSHOT_TENANT: '../other' }), /RAILSHOT_TENANT/);
});
