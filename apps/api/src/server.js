import { createServer } from 'node:http';
import { fileURLToPath } from 'node:url';
import { createGithubClient, ApiError } from './github.js';

const namePattern = /^[a-z0-9-]{1,30}$/;

export function loadConfig(env = process.env) {
  if (!env.GITHUB_TOKEN) throw new Error('GITHUB_TOKEN이 필요합니다.');
  const config = {
    token: env.GITHUB_TOKEN,
    owner: env.GITHUB_OWNER || 'Jasmin-Softbank',
    repo: env.GITHUB_REPO || 'railshot-apps',
    ref: env.GITHUB_REF || 'main',
    tenant: env.RAILSHOT_TENANT || 'demo',
    workflow: env.GITHUB_WORKFLOW || 'railshot-deploy.yml',
    port: Number(env.RAILSHOT_API_PORT || 4182),
    dashboardOrigin: env.RAILSHOT_DASHBOARD_ORIGIN || 'http://127.0.0.1:4181',
  };
  if (!namePattern.test(config.tenant)) throw new Error('RAILSHOT_TENANT가 잘못되었습니다.');
  if (!/^[A-Za-z0-9_.-]+$/.test(config.owner) || !/^[A-Za-z0-9_.-]+$/.test(config.repo) || !/^[A-Za-z0-9_.-]+$/.test(config.ref)) {
    throw new Error('GitHub 저장소 설정이 잘못되었습니다.');
  }
  if (!/^[A-Za-z0-9_.-]+\.ya?ml$/.test(config.workflow)) throw new Error('GITHUB_WORKFLOW이 잘못되었습니다.');
  if (!Number.isInteger(config.port) || config.port < 1 || config.port > 65535) throw new Error('RAILSHOT_API_PORT가 잘못되었습니다.');
  return config;
}

function json(response, status, value) {
  response.writeHead(status, { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store', 'x-content-type-options': 'nosniff' });
  response.end(JSON.stringify(value));
}

async function readJson(request) {
  if (!request.headers['content-type']?.startsWith('application/json')) throw new ApiError(415, 'JSON 요청이 필요합니다.');
  let body = '';
  let bytes = 0;
  for await (const chunk of request) {
    body += chunk;
    bytes += chunk.length;
    if (bytes > 8192) throw new ApiError(413, '요청이 너무 큽니다.');
  }
  try { return JSON.parse(body); } catch { throw new ApiError(400, 'JSON 형식이 잘못되었습니다.'); }
}

export function createApiHandler({ github, dashboardOrigin = 'http://127.0.0.1:4181' }) {
  return async (request, response) => {
    const host = request.headers.host?.split(':')[0];
    if (!['127.0.0.1', 'localhost'].includes(host)) { json(response, 403, { error: '로컬 요청만 허용합니다.' }); return; }
    if (request.headers.origin && request.headers.origin !== dashboardOrigin) { json(response, 403, { error: '허용되지 않은 출처입니다.' }); return; }
    const url = new URL(request.url, 'http://localhost');
    if (request.method === 'GET' && url.pathname === '/api/health') { json(response, 200, { ok: true }); return; }
    try {
      if (request.method === 'POST' && url.pathname === '/api/deployments') {
        if (request.headers['x-railshot-request'] !== '1') throw new ApiError(403, '요청 헤더가 필요합니다.');
        const body = await readJson(request);
        if (!body || typeof body !== 'object' || Array.isArray(body) || Object.keys(body).some((key) => !['app', 'target'].includes(key))) {
          throw new ApiError(400, '앱 이름과 배포 대상만 전달하세요.');
        }
        if (body.target?.environment !== 'cloud' || body.target?.provider !== 'aws') {
          throw new ApiError(422, '현재는 RailShot AWS의 등록된 앱만 실행할 수 있습니다.');
        }
        if (typeof body.app !== 'string' || !namePattern.test(body.app)) throw new ApiError(400, '앱 이름은 소문자·숫자·하이픈 1~30자여야 합니다.');
        json(response, 202, await github.dispatch(body.app));
        return;
      }
      const match = /^\/api\/deployments\/(\d+)$/.exec(url.pathname);
      if (request.method === 'GET' && match) { json(response, 200, await github.getRun(match[1])); return; }
      throw new ApiError(404, 'API 경로를 찾을 수 없습니다.');
    } catch (error) {
      json(response, error.status || 500, { error: error instanceof ApiError ? error.message : '요청을 처리하지 못했습니다.' });
    }
  };
}

export function createApiServer(options) {
  return createServer(createApiHandler(options));
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const config = loadConfig();
  const server = createApiServer({ github: createGithubClient(config), dashboardOrigin: config.dashboardOrigin });
  server.listen(config.port, '127.0.0.1', () => console.log(`RailShot API: http://127.0.0.1:${config.port}`));
}
