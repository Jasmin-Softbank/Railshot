import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { randomUUID } from 'node:crypto';
import { homedir } from 'node:os';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { createDeploymentService, ServiceError } from './github.js';
import { archiveLimits, inspectArchive, validateFiles } from './archive.js';
import { fetchPublicGithubSource } from './public-github.js';
import { APP_NAME, APP_NAME_MESSAGE } from './contract.js';
import { createProductService, ProductError, idempotencyKey } from './product.js';
import { apiAccessConfig, allowsHost, allowsOrigin, allowsToken } from './access.js';
import { createEnvironmentAdapter, EnvironmentError } from './environments.js';
import { DashboardError, cookieToken, sessionCookie, SESSION_COOKIE } from './sessions.js';

const root = join(dirname(fileURLToPath(import.meta.url)), '..', '..', 'dashboard');
const assets = new Map([
  ['/', ['index.html', 'text/html; charset=utf-8']],
  ['/app.js', ['app.js', 'text/javascript; charset=utf-8']],
  ['/contracts/application.mjs', ['../../contracts/application.mjs', 'text/javascript; charset=utf-8']],
  ['/styles.css', ['styles.css', 'text/css; charset=utf-8']],
]);
function json(response, code, data, headers = {}) {
  response.writeHead(code, { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store', 'x-content-type-options': 'nosniff', ...headers });
  response.end(JSON.stringify(data));
}
async function readLimited(request, limit) {
  const chunks = []; let size = 0;
  for await (const chunk of request) {
    size += chunk.length;
    if (size > limit) throw new ServiceError('요청 크기가 허용 범위를 초과했습니다.', 413);
    chunks.push(chunk);
  }
  return Buffer.concat(chunks);
}
function normalizedRepository(value) {
  let url;
  try { url = new URL(value); } catch { throw new ServiceError('공개 GitHub 저장소 URL이 필요합니다.', 400); }
  const match = /^\/([A-Za-z0-9-]+)\/([A-Za-z0-9._-]+?)(?:\.git)?\/?$/.exec(url.pathname);
  if (url.protocol !== 'https:' || url.hostname !== 'github.com' || url.port || url.username || url.password || url.search || url.hash || !match || match[1].startsWith('-') || match[2].startsWith('.') || match[2].endsWith('.')) throw new ServiceError('공개 GitHub 저장소 기본 URL만 사용할 수 있습니다.', 400);
  return `https://github.com/${match[1].toLowerCase()}/${match[2].toLowerCase()}`;
}
// Parse without fetching GitHub: an idempotency replay must retain its first source snapshot.
async function uploadedSource(request, strict = false, allowSelection = false) {
  const contentType = request.headers['content-type'] || '';
  if (!/^multipart\/form-data\s*;/i.test(contentType)) throw new ServiceError('multipart/form-data 요청이 필요합니다.', 415);
  const body = await readLimited(request, archiveLimits.maxBytes + 1024 * 1024);
  let form;
  try { form = await new Request('http://localhost/', { method: 'POST', headers: { 'content-type': contentType }, body }).formData(); }
  catch { throw new ServiceError('multipart 요청 형식이 잘못되었습니다.', 400); }
  const fail = (message) => { throw new ServiceError(message, strict ? 422 : 400); };
  const allowed = new Set(['app', 'target_id', 'plan_id', 'source_type', 'repository_url', 'archive', 'files', 'paths']);
  if (allowSelection) for (const name of ['environment', 'provider', 'source_name']) allowed.add(name);
  for (const key of form.keys()) {
    if (!allowed.has(key)) fail('알 수 없는 입력 필드입니다.');
    if (key !== 'files' && form.getAll(key).length !== 1) fail('단일 입력 필드를 중복해서 보낼 수 없습니다.');
  }
  const selecting = allowSelection && (form.has('environment') || form.has('provider'));
  const app = form.has('app') ? form.get('app') : undefined;
  if (!selecting && (typeof app !== 'string' || !APP_NAME.test(app))) fail(APP_NAME_MESSAGE);
  const target_id = form.has('target_id') ? form.get('target_id') : undefined;
  if (target_id !== undefined && (typeof target_id !== 'string' || !target_id)) fail('대상 ID가 잘못되었습니다.');
  if (strict && !selecting && !target_id) fail('대상 ID가 필요합니다.');
  const plan_id = form.has('plan_id') ? form.get('plan_id') : undefined;
  if (plan_id !== undefined && (!allowSelection || typeof plan_id !== 'string' || !/^[A-Za-z0-9._-]{1,128}$/.test(plan_id))) fail('환경 계획 ID가 잘못되었습니다.');
  let selected = {};
  if (selecting) {
    const environment = form.get('environment'), provider = form.get('provider');
    if (form.has('app') || form.has('target_id') || form.has('plan_id')) fail('환경 선택과 직접 대상·계획 지정을 함께 사용할 수 없습니다.');
    if (!(environment === 'cloud' && ['aws', 'gcp'].includes(provider) || environment === 'onprem' && ['openstack', 'proxmox'].includes(provider))) fail('배포 환경과 인프라 종류를 확인하세요.');
    const source_name = form.has('source_name') ? form.get('source_name') : undefined;
    if (source_name !== undefined && (typeof source_name !== 'string' || !source_name.length || source_name.length > 255 || /[\x00-\x1f]/.test(source_name))) fail('소스 이름을 확인하세요.');
    selected = { deployment_selection: { environment, provider }, source_name };
  } else if (form.has('source_name')) fail('소스 이름은 환경 선택과 함께 입력하세요.');
  const uploads = form.getAll('files');
  const supplied = [form.has('repository_url') && 'github', uploads.length > 0 && 'folder', form.has('archive') && 'zip'].filter(Boolean);
  if (supplied.length !== 1) fail('배포 소스 하나만 입력하세요.');
  const source_type = supplied[0];
  if (form.has('source_type') && form.get('source_type') !== source_type) fail('소스 형식과 입력값이 일치하지 않습니다.');
  if (source_type !== 'folder' && form.has('paths')) fail('폴더 소스에만 paths를 사용할 수 있습니다.');
  if (source_type === 'github') {
    if (typeof form.get('repository_url') !== 'string') fail('공개 GitHub 저장소 URL이 필요합니다.');
    let repository_url;
    try { repository_url = normalizedRepository(form.get('repository_url')); } catch { fail('공개 GitHub 저장소 기본 URL이 필요합니다.'); }
    return { app, target_id, ...(plan_id ? { plan_id } : {}), ...selected, source_type, repository_url };
  }
  try {
    if (source_type === 'folder') {
      const paths = JSON.parse(form.get('paths'));
      if (!Array.isArray(paths) || paths.length !== uploads.length || uploads.length > archiveLimits.maxFiles) fail('폴더 파일 목록이 잘못되었습니다.');
      const files = await Promise.all(uploads.map(async (file, index) => {
        if (!file || typeof file.arrayBuffer !== 'function') fail('폴더 파일이 잘못되었습니다.');
        return { path: paths[index], content: Buffer.from(await file.arrayBuffer()) };
      }));
      return { app, target_id, ...(plan_id ? { plan_id } : {}), ...selected, source_type, files: validateFiles(files) };
    }
    const file = form.get('archive');
    if (!file || typeof file.arrayBuffer !== 'function' || !file.name?.toLowerCase().endsWith('.zip')) fail('ZIP 파일이 필요합니다.');
    return { app, target_id, ...(plan_id ? { plan_id } : {}), ...selected, ...(selecting && !selected.source_name ? { source_name: file.name } : {}), source_type, files: await inspectArchive(Buffer.from(await file.arrayBuffer())) };
  } catch { fail('소스 파일 목록·경로·크기를 확인하세요. 비밀 파일은 보낼 수 없습니다.'); }
}
async function jsonInput(request) {
  if (!/^application\/json(?:\s*;|$)/i.test(request.headers['content-type'] || '')) throw new ServiceError('application/json 요청이 필요합니다.', 415);
  let value, raw;
  try { raw = (await readLimited(request, 64 * 1024)).toString('utf8'); value = JSON.parse(raw); }
  catch (error) { if (error.status) throw error; throw new ServiceError('JSON 형식이 잘못되었습니다.', 400); }
  // JSON.parse validates grammar; this small token pass rejects duplicate decoded object keys at every depth.
  const tokens = raw.match(/"(?:\\[\s\S]|[^"\\])*"|[{}\[\]:]/g) || [], stack = [];
  for (let i = 0; i < tokens.length; i++) {
    const token = tokens[i];
    if (token === '{' || token === '[') stack.push(token === '{' ? new Set() : null);
    else if (token === '}' || token === ']') stack.pop();
    else if (token.startsWith('"') && tokens[i + 1] === ':') {
      const key = JSON.parse(token), keys = stack.at(-1);
      if (keys.has(key)) throw new ServiceError('JSON 필드를 중복해서 보낼 수 없습니다.', 422);
      keys.add(key);
    }
  }
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new ServiceError('JSON 객체가 필요합니다.', 422);
  return value;
}
function pagination(parameters) {
  for (const key of parameters.keys()) if (!['limit', 'marker'].includes(key) || parameters.getAll(key).length !== 1) throw new ServiceError('조회 조건이 잘못되었습니다.', 422);
  const rawLimit = parameters.get('limit') ?? '20';
  if (!/^[1-9]\d?$|^100$/.test(rawLimit)) throw new ServiceError('limit는 1–100이어야 합니다.', 422);
  const marker = parameters.get('marker');
  if (marker !== null && (!/^[A-Za-z0-9._-]{1,128}$/.test(marker))) throw new ServiceError('marker가 잘못되었습니다.', 422);
  return { limit: Number(rawLimit), marker };
}
function page(items, parameters) {
  const { limit, marker } = pagination(parameters);
  const start = marker === null ? 0 : items.findIndex((item) => item.id === marker) + 1;
  if (marker !== null && start === 0) throw new ServiceError('marker가 잘못되었습니다.', 422);
  const visible = items.slice(start, start + limit);
  return { items: visible, next_marker: start + visible.length < items.length ? visible.at(-1).id : null };
}
function apiError(response, error, requestId, versioned) {
  const status = error instanceof EnvironmentError && error.status === 400 ? 422 : Number.isInteger(error.status) && error.status >= 400 && error.status < 600 ? error.status : 500;
  const codes = { 400: 'INVALID_INPUT', 401: 'UNAUTHENTICATED', 403: 'FORBIDDEN', 404: 'NOT_FOUND', 405: 'METHOD_NOT_ALLOWED', 409: 'CONFLICT', 413: 'PAYLOAD_TOO_LARGE', 415: 'UNSUPPORTED_MEDIA_TYPE', 422: 'INVALID_INPUT', 502: 'UPSTREAM_FAILURE', 503: 'UPSTREAM_UNAVAILABLE' };
  const message = error instanceof ServiceError || error instanceof ProductError || error instanceof DashboardError ? error.message : '요청을 처리하지 못했습니다.';
  const headers = { 'X-Request-ID': requestId, ...(error.allow ? { Allow: error.allow } : {}), ...(error.retryable ? { 'Retry-After': '2' } : {}) };
  const code = error instanceof EnvironmentError && error.status === 400 ? 'INVALID_INPUT'
    : (error instanceof ProductError || error instanceof EnvironmentError || error instanceof DashboardError) && error.code || codes[status] || 'INTERNAL_ERROR';
  json(response, status, versioned ? { error: { code, message,
    request_id: requestId, retryable: Boolean(error.retryable), outcome_unknown: Boolean(error.outcomeUnknown) } } : { error: message }, headers);
}
function requestKey(request) {
  const count = request.rawHeaders.filter((value, index) => index % 2 === 0 && value.toLowerCase() === 'idempotency-key').length;
  if (count !== 1) throw new ServiceError('Idempotency-Key 하나만 입력하세요.', 422);
  return idempotencyKey(request.headers['idempotency-key']);
}
function accepted(response, kind, record, requestId, action = 'create') {
  const terminal = !['queued', 'running'].includes(record.status);
  json(response, terminal ? 200 : 202, terminal ? record : { resource_id: record.id, action, status: 'accepted', request_id: requestId },
    { Location: `/api/v1/${kind}/${record.id}`, 'X-Request-ID': requestId, ...(!terminal ? { 'Retry-After': '2' } : {}) });
}

export function createAppServer({ sourceLoader = fetchPublicGithubSource, access = apiAccessConfig(),
  service = process.env.GITHUB_TOKEN && process.env.RAILSHOT_TARGET_ID ? createDeploymentService({ token: process.env.GITHUB_TOKEN,
    owner: process.env.GITHUB_OWNER, repo: process.env.GITHUB_REPO, ref: process.env.GITHUB_REF, tenant: process.env.RAILSHOT_TENANT || process.env.JASMIN_TENANT,
    workflow: process.env.GITHUB_WORKFLOW, targetId: process.env.RAILSHOT_TARGET_ID, targetIds: process.env.RAILSHOT_TARGET_IDS?.split(',') }) : null,
  stateDirectory = process.env.RAILSHOT_STATE_DIR || join(homedir(), '.local', 'state', 'railshot'),
  deployPublished, environmentAdapter, applicationAdapter, observeMetrics, observeLogs, product, pollInterval,
  target = { provider: process.env.RAILSHOT_TARGET_PROVIDER }, providerTargets,
} = {}) {
  // Explicit adapter instances keep tests offline; production adapters consume only operator files.
  const productReady = Promise.resolve().then(async () => {
    if (product) return product;
    let cd = deployPublished;
    if (!cd && service && process.env.RAILSHOT_CD_CONFIG) {
      const { createCdAdapter } = await import('./cd.js');
      cd = await createCdAdapter({ configPath: process.env.RAILSHOT_CD_CONFIG, loadPublished: service.publishedFiles });
    }
    const environment = environmentAdapter || (process.env.RAILSHOT_PROFILES_FILE ? await createEnvironmentAdapter({ profilesFile: process.env.RAILSHOT_PROFILES_FILE, stateDir: join(stateDirectory, 'environments'), loadPublished: service?.publishedFiles }) : undefined);
    const { createApplicationAdapter } = await import('./applications.js');
    const applications = applicationAdapter || (process.env.RAILSHOT_APPLICATIONS_FILE ? await createApplicationAdapter({ configPath: process.env.RAILSHOT_APPLICATIONS_FILE, ciIdentity: service?.identity, loadPublished: service?.publishedFiles }) : undefined);
    const { createMetricsObserver } = await import('./metrics.js');
    const observer = observeMetrics || createMetricsObserver({
      configPath: process.env.RAILSHOT_OBSERVER_PRODUCT_FILE || process.env.RAILSHOT_OBSERVER_CONFIG,
    });
    const selections = providerTargets ?? (process.env.RAILSHOT_PROVIDER_TARGETS === undefined ? undefined : JSON.parse(process.env.RAILSHOT_PROVIDER_TARGETS));
    const { createAppLogsObserver } = await import('./logs.js');
    const logs = observeLogs || createAppLogsObserver({ configPath: process.env.RAILSHOT_CD_CONFIG });
    return createProductService({ observeMetrics: observer, observeLogs: logs, service, target, providerTargets: selections, directory: stateDirectory, deployPublished: cd, environmentAdapter: environment, applicationAdapter: applications, pollInterval });
  });
  // Hold initialization errors until a request can receive a safe 503; never leak private config paths.
  productReady.catch(() => {});
  const server = createServer(async (request, response) => {
    const requestId = randomUUID();
    let versioned = request.url.startsWith('/api/v1');
    response.setHeader('X-Request-ID', requestId);
    try {
      if (!allowsHost(request.headers.host, access) || !allowsOrigin(request.headers.origin, access)) throw new ServiceError('요청의 Host 또는 Origin이 허용되지 않습니다.', 403);
      let url;
      try { url = new URL(request.url, 'http://localhost'); } catch { throw new ServiceError('요청 경로가 잘못되었습니다.', 400); }
      versioned = url.pathname.startsWith('/api/v1');
      if (request.method === 'GET' && url.pathname === '/healthz') {
        // Liveness remains local; readiness also requires usable durable state and operator config.
        const configured = Boolean(await productReady.catch(() => null))
          && Boolean(product || service?.targetId || environmentAdapter || process.env.RAILSHOT_PROFILES_FILE);
        json(response, 200, { ok: true, configured, ...(!access.remote && { target_id: service?.targetId || null }) }); return;
      }
      if (url.pathname.startsWith('/api/')) {
        if (request.headers['sec-fetch-site'] === 'cross-site') throw new ServiceError('다른 사이트에서 보낸 요청은 허용되지 않습니다.', 403);
        if (!access.publicDemo && !allowsToken(request.headers.authorization, access.token)) {
          response.setHeader('www-authenticate', 'Bearer');
          throw new ServiceError('API authentication required', 401);
        }
        let products;
        try { products = await productReady; } catch { throw new ServiceError('제품 저장소 또는 서버 설정을 확인할 수 없습니다.', 503); }
        // Public visitors are anonymous cookie sessions. Existing localhost maintenance clients
        // without a cookie retain their private maintenance channel and legacy contracts.
        const dashboardRoute = /^\/api\/v1\/(sessions|preferences|connections)(?:\/|$)/.test(url.pathname);
        const scoped = access.remote || access.publicDemo || dashboardRoute || (request.headers.cookie || '').includes(`${SESSION_COOKIE}=`);
        const session = scoped ? products.dashboard.session(cookieToken(request.headers.cookie)) : null;
        const sessionId = session?.id ?? null;
        if (session?.token) response.setHeader('Set-Cookie', sessionCookie(session.token, access.remote));
        response.setHeader('Vary', 'Cookie');
        if (versioned) {
          if (dashboardRoute) {
            const route = /^\/api\/v1\/(sessions|preferences|connections)(?:\/([a-f0-9-]{36}))?$/.exec(url.pathname);
            if (!route) throw new ServiceError('API 경로를 찾을 수 없습니다.', 404);
            const [, kind, id] = route;
            if (id && kind !== 'connections') throw new ServiceError('API 경로를 찾을 수 없습니다.', 404);
            const methods = kind === 'preferences' ? ['GET', 'PUT'] : id ? ['GET', 'PUT', 'DELETE'] : ['GET', 'POST'];
            if (!methods.includes(request.method)) { const error = new ServiceError('지원하지 않는 메서드입니다.', 405); error.allow = methods.join(', '); throw error; }
            if ([...url.searchParams].length && !(kind === 'connections' && !id && request.method === 'GET')) throw new ServiceError('지원하지 않는 조회 조건입니다.', 422);
            if (kind === 'sessions' && !id && ['GET', 'POST'].includes(request.method)) {
              json(response, request.method === 'POST' && session.token ? 201 : 200, { expires_at: session.expires_at }, { 'X-Request-ID': requestId }); return;
            }
            if (kind === 'preferences' && !id && ['GET', 'PUT'].includes(request.method)) {
              json(response, 200, products.dashboard.preferences(sessionId, request.method === 'PUT' ? await jsonInput(request) : undefined)); return;
            }
            if (kind === 'connections') {
              if (!id && request.method === 'GET') { json(response, 200, page(products.dashboard.connections(sessionId), url.searchParams)); return; }
              if (id && request.method === 'GET') {
                const connection = products.dashboard.connections(sessionId).find((row) => row.id === id);
                if (!connection) throw new DashboardError('이 세션에서 자원을 찾을 수 없습니다.', 404, 'NOT_FOUND');
                json(response, 200, connection); return;
              }
              if (!id && request.method === 'POST' || id && request.method === 'PUT') {
                const connection = products.dashboard.saveConnection(sessionId, id, await jsonInput(request));
                json(response, id ? 200 : 201, connection, id ? {} : { Location: `/api/v1/connections/${connection.id}` }); return;
              }
              if (id && request.method === 'DELETE') {
                products.dashboard.deleteConnection(sessionId, id); response.writeHead(204, { 'cache-control': 'no-store' }); response.end(); return;
              }
            }
            throw new ServiceError('지원하지 않는 메서드입니다.', 405);
          }
          if (url.pathname === '/api/v1/options') {
            if (request.method !== 'GET') { const error = new ServiceError('지원하지 않는 메서드입니다.', 405); error.allow = 'GET'; throw error; }
            json(response, 200, page(products?.deploymentOptions?.() || [], url.searchParams)); return;
          }
          const eventRoute = /^\/api\/v1\/deployments\/([A-Za-z0-9._-]+)\/events$/.exec(url.pathname);
          if (eventRoute) {
            if (request.method !== 'GET') { const error = new ServiceError('지원하지 않는 메서드입니다.', 405); error.allow = 'GET'; throw error; }
            if ([...url.searchParams].length) throw new ServiceError('지원하지 않는 조회 조건입니다.', 422);
            if (!products) throw new ServiceError('제품 실행 기능이 설정되지 않았습니다.', 503);
            json(response, 200, await products.getDeploymentEvents(eventRoute[1], sessionId)); return;
          }
          const observationRoute = /^\/api\/v1\/targets\/([A-Za-z0-9._-]+)\/observations$/.exec(url.pathname);
          if (observationRoute) {
            if (request.method !== 'GET') { const error = new ServiceError('지원하지 않는 메서드입니다.', 405); error.allow = 'GET'; throw error; }
            if ([...url.searchParams].length) throw new ServiceError('지원하지 않는 조회 조건입니다.', 422);
            json(response, 200, await products.getTargetObservation(observationRoute[1], sessionId)); return;
          }
          const logRoute = /^\/api\/v1\/deployments\/([A-Za-z0-9._-]+)\/logs$/.exec(url.pathname);
          if (logRoute) {
            if (request.method !== 'GET') { const error = new ServiceError('지원하지 않는 메서드입니다.', 405); error.allow = 'GET'; throw error; }
            if ([...url.searchParams].length) throw new ServiceError('지원하지 않는 조회 조건입니다.', 422);
            if (!products) throw new ServiceError('제품 실행 기능이 설정되지 않았습니다.', 503);
            json(response, 200, await products.getDeploymentLogs(logRoute[1], sessionId)); return;
          }
          const actionRoute = /^\/api\/v1\/deployments\/([A-Za-z0-9._-]+)\/actions$/.exec(url.pathname);
          if (actionRoute) {
            if (request.method !== 'POST') { const error = new ServiceError('지원하지 않는 메서드입니다.', 405); error.allow = 'POST'; throw error; }
            if ([...url.searchParams].length) throw new ServiceError('지원하지 않는 조회 조건입니다.', 422);
            if (!products) throw new ServiceError('제품 실행 기능이 설정되지 않았습니다.', 503);
            const input = await jsonInput(request);
            if (!input || Array.isArray(input) || typeof input !== 'object'
                || Object.keys(input).length !== 1 || input.action !== 'resume') throw new ServiceError('action=resume만 입력하세요.', 422);
            accepted(response, 'deployments', await products.resumeDeployment(actionRoute[1], sessionId), requestId, 'resume'); return;
          }
          const routes = /^(?:\/api\/v1\/(targets|applications|builds|deployments|profiles|plans|environments))(?:\/([A-Za-z0-9._-]+))?$/.exec(url.pathname);
          if (!routes) throw new ServiceError('API 경로를 찾을 수 없습니다.', 404);
          const [, kind, id] = routes;
          const methods = id ? ['builds', 'deployments', 'plans', 'environments', 'applications'].includes(kind) ? ['GET'] : [] : ['targets', 'profiles', 'applications'].includes(kind) ? ['GET'] : ['GET', 'POST'];
          if (!methods.length) throw new ServiceError('API 경로를 찾을 수 없습니다.', 404);
          if (!methods.includes(request.method)) { const error = new ServiceError('지원하지 않는 메서드입니다.', 405); error.allow = methods.join(', '); throw error; }
          if (kind === 'applications') {
            if (id && [...url.searchParams].length) throw new ServiceError('지원하지 않는 조회 조건입니다.', 422);
            json(response, 200, id ? products.getApplication(id, sessionId) : page(products.applications(sessionId), url.searchParams)); return;
          }
          if (['targets', 'profiles'].includes(kind)) {
            json(response, 200, page(products ? await products[kind](sessionId) : [], url.searchParams)); return;
          }
          if (!id && request.method === 'GET') {
            json(response, 200, kind === 'plans' ? page(products.list(kind, sessionId), url.searchParams)
              : products.list(kind, sessionId, pagination(url.searchParams))); return;
          }
          if ([...url.searchParams].length) throw new ServiceError('지원하지 않는 조회 조건입니다.', 422);
          if (!products) throw new ServiceError('제품 실행 기능이 설정되지 않았습니다.', 503);
          if (request.method === 'GET') {
            const getter = { builds: 'getBuild', deployments: 'getDeployment', plans: 'getPlan', environments: 'getEnvironment' }[kind];
            json(response, 200, await products[getter](id, sessionId)); return;
          }
          if (kind === 'builds') {
            const result = await products.createBuild(await uploadedSource(request, true), sourceLoader, sessionId);
            accepted(response, kind, { id: String(result.run_id), status: 'queued' }, requestId); return;
          }
          if (kind === 'deployments') {
            const key = requestKey(request);
            accepted(response, kind, await products.createDeployment(await uploadedSource(request, true, true), key, sourceLoader, sessionId), requestId); return;
          }
          if (kind === 'plans') {
            const plan = await products.createPlan(await jsonInput(request), sessionId);
            json(response, 201, plan, { Location: `/api/v1/plans/${plan.id}` }); return;
          }
          const key = requestKey(request);
          accepted(response, kind, await products.createEnvironment(await jsonInput(request), key, sessionId), requestId); return;
        }
        if (!service) throw new ServiceError('CI 실행 기능이 설정되지 않았습니다.', 503);
        if (request.method === 'POST' && url.pathname === '/api/deploy') {
          if ((request.headers['x-railshot-request'] ?? request.headers['x-jasmin-request']) !== 'deploy') throw new ServiceError('요청 헤더가 필요합니다.', 403);
          const input = await uploadedSource(request);
          input.target_id ??= service.targetId;
          const result = products ? await products.createBuild(input, sourceLoader, sessionId) : await service.deploy(input.files ? input : { ...input, ...await sourceLoader(input.repository_url) });
          json(response, 202, result); return;
        }
        const match = request.method === 'GET' && /^\/api\/runs\/(\d+)$/.exec(url.pathname);
        if (match) { json(response, 200, products ? await products.legacyStatus(match[1], sessionId) : await service.status(match[1])); return; }
        throw new ServiceError('API 경로를 찾을 수 없습니다.', 404);
      }
      const asset = request.method === 'GET' && assets.get(url.pathname);
      if (!asset) { response.writeHead(404).end('Not found'); return; }
      const content = await readFile(join(root, asset[0]));
      response.writeHead(200, { 'content-type': asset[1], 'content-length': content.length, 'cache-control': 'no-store', 'x-content-type-options': 'nosniff' }).end(content);
    } catch (error) { apiError(response, error, requestId, versioned); }
  });
  server.on('close', () => { productReady.then((value) => value?.close?.()).catch(() => {}); });
  server.productReady = productReady;
  return server;
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const port = Number(process.env.PORT || 4173), access = apiAccessConfig();
  createAppServer({ access }).listen(port, access.bindHost, () => { console.log(`RAILSHOT API listening on ${access.bindHost}:${port}`); });
}
