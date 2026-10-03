import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { randomUUID } from 'node:crypto';
import { homedir } from 'node:os';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { createDeploymentService, ServiceError } from './github.js';
import { fetchPublicGithubSource } from './public-github.js';
import { createProductService } from './product.js';
import { apiAccessConfig, allowsHost, allowsOrigin, allowsToken } from './access.js';
import { createEnvironmentAdapter } from './environments.js';
import { cookieToken, sessionCookie, SESSION_COOKIE } from './sessions.js';
import { json, apiError, accepted } from './http/response.js';
import { jsonInput, pagination, page, requestKey } from './http/request.js';
import { uploadedSource, sourceArchive } from './http/source.js';
import { createOpenStackRoutes, isRegistrationRoute, isTokenClaimRoute } from './http/openstack.js';
import { isDashboardRoute, serveDashboard } from './http/dashboard.js';

const root = join(dirname(fileURLToPath(import.meta.url)), '..', '..', 'dashboard');
const assets = new Map([
  ['/', ['index.html', 'text/html; charset=utf-8']],
  ['/app.js', ['app.js', 'text/javascript; charset=utf-8']],
  ['/src/api.js', ['src/api.js', 'text/javascript; charset=utf-8']],
  ['/src/openstack-installer.js', ['src/openstack-installer.js', 'text/javascript; charset=utf-8']],
  ['/src/lifecycle.js', ['src/lifecycle.js', 'text/javascript; charset=utf-8']],
  ['/contracts/application.mjs', ['../../contracts/application.mjs', 'text/javascript; charset=utf-8']],
  ['/styles.css', ['styles.css', 'text/css; charset=utf-8']],
]);
export function createAppServer({ sourceLoader = fetchPublicGithubSource, access = apiAccessConfig(),
  service = process.env.GITHUB_TOKEN && process.env.RAILSHOT_TARGET_ID ? createDeploymentService({ token: process.env.GITHUB_TOKEN,
    owner: process.env.GITHUB_OWNER, repo: process.env.GITHUB_REPO, ref: process.env.GITHUB_REF, tenant: process.env.RAILSHOT_TENANT || process.env.JASMIN_TENANT,
    workflow: process.env.GITHUB_WORKFLOW, targetId: process.env.RAILSHOT_TARGET_ID, targetIds: process.env.RAILSHOT_TARGET_IDS?.split(',') }) : null,
  stateDirectory = process.env.RAILSHOT_STATE_DIR || join(homedir(), '.local', 'state', 'railshot'),
  deployPublished, environmentAdapter, applicationAdapter, observeMetrics, observeLogs, product, pollInterval,
  target = { provider: process.env.RAILSHOT_TARGET_PROVIDER }, providerTargets,
} = {}) {
  const openstack = createOpenStackRoutes();
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
        if (isTokenClaimRoute(url.pathname)) {
          if (!access.token) throw new ServiceError('등록 요청에는 운영자 API 토큰 설정이 필요합니다.', 503);
          let products;
          try { products = await productReady; } catch { throw new ServiceError('제품 저장소 또는 서버 설정을 확인할 수 없습니다.', 503); }
          await openstack.claimToken(request, response, url, products);
          return;
        }
        if (!access.publicDemo && !allowsToken(request.headers.authorization, access.token)) {
          response.setHeader('www-authenticate', 'Bearer');
          throw new ServiceError('API authentication required', 401);
        }
        const registrationRoute = isRegistrationRoute(url.pathname);
        if (registrationRoute) {
          if (!access.token) throw new ServiceError('등록 요청에는 운영자 API 토큰 설정이 필요합니다.', 503);
          if (!allowsToken(request.headers.authorization, access.token)) {
            response.setHeader('www-authenticate', 'Bearer');
            throw new ServiceError('API authentication required', 401);
          }
        }
        if (versioned && await openstack.serveInstaller(request, response, url)) return;
        let products;
        try { products = await productReady; } catch { throw new ServiceError('제품 저장소 또는 서버 설정을 확인할 수 없습니다.', 503); }
        // Public visitors are anonymous cookie sessions. Existing localhost maintenance clients
        // without a cookie retain their private maintenance channel and legacy contracts.
        const dashboardRoute = isDashboardRoute(url.pathname);
        const scoped = access.remote || access.publicDemo || dashboardRoute || registrationRoute || (request.headers.cookie || '').includes(`${SESSION_COOKIE}=`);
        const session = scoped ? products.dashboard.session(cookieToken(request.headers.cookie)) : null;
        const sessionId = session?.id ?? null;
        if (session?.token) response.setHeader('Set-Cookie', sessionCookie(session.token, access.remote));
        response.setHeader('Vary', 'Cookie');
        if (versioned) {
          if (registrationRoute) {
            await openstack.serveRegistration(request, response, url, products, sessionId);
            return;
          }
          if (dashboardRoute) {
            await serveDashboard(request, response, url, products, session, requestId);
            return;
          }
          if (url.pathname === '/api/v1/options') {
            if (request.method !== 'GET') { const error = new ServiceError('지원하지 않는 메서드입니다.', 405); error.allow = 'GET'; throw error; }
            json(response, 200, page(products?.deploymentOptions?.() || [], url.searchParams)); return;
          }
          const updateRoute = /^\/api\/v1\/applications\/([A-Za-z0-9._-]+)\/updates$/.exec(url.pathname);
          if (updateRoute) {
            if (request.method !== 'POST') { const error = new ServiceError('지원하지 않는 메서드입니다.', 405); error.allow = 'POST'; throw error; }
            if ([...url.searchParams].length) throw new ServiceError('지원하지 않는 조회 조건입니다.', 422);
            // Authorize before reading an upload or resolving a remote repository.
            products.getApplication(updateRoute[1], sessionId);
            const key = requestKey(request);
            const record = await products.createUpdate(updateRoute[1], await uploadedSource(request, true, false, true), key, sourceLoader, sessionId);
            accepted(response, 'deployments', record, requestId); return;
          }
          const startRoute = /^\/api\/v1\/deployments\/([A-Za-z0-9._-]+)\/start$/.exec(url.pathname);
          if (startRoute) {
            if (request.method !== 'POST') { const error = new ServiceError('지원하지 않는 메서드입니다.', 405); error.allow = 'POST'; throw error; }
            if ([...url.searchParams].length) throw new ServiceError('지원하지 않는 조회 조건입니다.', 422);
            const input = await jsonInput(request);
            if (Object.keys(input).some((key) => key !== 'rebuild') || input.rebuild !== undefined && typeof input.rebuild !== 'boolean') throw new ServiceError('rebuild는 true 또는 false로 입력하세요.', 422);
            accepted(response, 'deployments', await products.startUpdate(startRoute[1], input, sessionId), requestId); return;
          }
          const sourceRoute = /^\/api\/v1\/deployments\/([A-Za-z0-9._-]+)\/source$/.exec(url.pathname);
          if (sourceRoute) {
            if (request.method !== 'GET') { const error = new ServiceError('지원하지 않는 메서드입니다.', 405); error.allow = 'GET'; throw error; }
            if ([...url.searchParams.keys()].some((key) => key !== 'variant') || url.searchParams.getAll('variant').length > 1) throw new ServiceError('지원하지 않는 조회 조건입니다.', 422);
            const variant = url.searchParams.get('variant') || 'submitted';
            if (!['submitted', 'deployed'].includes(variant)) throw new ServiceError('소스 종류를 확인하세요.', 422);
            const bytes = await sourceArchive(await products.sourceFiles(sourceRoute[1], variant, sessionId));
            response.writeHead(200, { 'content-type': 'application/zip', 'content-length': bytes.length,
              'content-disposition': `attachment; filename="railshot-${sourceRoute[1]}-${variant}.zip"`,
              'cache-control': 'no-store', 'x-content-type-options': 'nosniff' }); response.end(bytes); return;
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
          const lifecycle = /^\/api\/v1\/applications\/([A-Za-z0-9._-]+)\/(plans|operations)$/.exec(url.pathname);
          const operation = /^\/api\/v1\/operations\/([A-Za-z0-9._-]+)$/.exec(url.pathname);
          if (lifecycle || operation) {
            const allowed = lifecycle ? 'POST' : 'GET';
            if (request.method !== allowed) { const error = new ServiceError('지원하지 않는 메서드입니다.', 405); error.allow = allowed; throw error; }
            if ([...url.searchParams].length) throw new ServiceError('지원하지 않는 조회 조건입니다.', 422);
            if (!products) throw new ServiceError('제품 실행 기능이 설정되지 않았습니다.', 503);
            if (operation) { json(response, 200, products.getOperation(operation[1], sessionId)); return; }
            if (lifecycle[2] === 'plans') {
              json(response, 201, await products.createApplicationPlan(lifecycle[1], await jsonInput(request), sessionId)); return;
            }
            const key = requestKey(request);
            accepted(response, 'operations', await products.createApplicationOperation(lifecycle[1], await jsonInput(request), key, sessionId), requestId); return;
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
