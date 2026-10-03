import { ServiceError } from '../github.js';
import { buildOpenStackInstaller } from '../openstack/installer.js';
import { json } from './response.js';
import { jsonInput, page } from './request.js';

const installerPaths = new Set([
  '/api/v1/installers/openstack',
  '/api/v1/installers/openstack/scripts',
  '/api/v1/installers/openstack/bundles',
]);

export const isRegistrationRoute = (path) => /^\/api\/v1\/registrations(?:\/|$)/.test(path);

export function createOpenStackRoutes() {
  let installerReady;

  return {
    async serveInstaller(request, response, url) {
      if (!installerPaths.has(url.pathname)) return false;
      if (request.method !== 'GET') {
        const error = new ServiceError('지원하지 않는 메서드입니다.', 405);
        error.allow = 'GET';
        throw error;
      }
      if ([...url.searchParams].length) throw new ServiceError('지원하지 않는 조회 조건입니다.', 422);

      const packageData = await (installerReady ??= buildOpenStackInstaller());
      if (url.pathname.endsWith('/bundles')) {
        response.writeHead(200, { 'content-type': 'application/zip', 'content-length': packageData.archive.length,
          'content-disposition': 'attachment; filename="railshot-openstack-installer.zip"',
          'cache-control': 'no-store', 'x-content-type-options': 'nosniff' });
        response.end(packageData.archive);
      } else if (url.pathname.endsWith('/scripts')) {
        response.writeHead(200, { 'content-type': 'text/x-shellscript; charset=utf-8',
          'content-disposition': 'attachment; filename="install.sh"', 'cache-control': 'no-store',
          'x-content-type-options': 'nosniff' });
        response.end(packageData.script);
      } else {
        json(response, 200, { install_sh: packageData.script, bundle_sha256: packageData.sha256,
          script_url: '/api/v1/installers/openstack/scripts', bundle_url: '/api/v1/installers/openstack/bundles' });
      }
      return true;
    },

    async serveRegistration(request, response, url, products, sessionId) {
      if (!products.registrations) throw new ServiceError('등록 저장소를 사용할 수 없습니다.', 503);
      if ([...url.searchParams].length && !(url.pathname === '/api/v1/registrations' && request.method === 'GET')) {
        throw new ServiceError('지원하지 않는 조회 조건입니다.', 422);
      }
      const route = /^\/api\/v1\/registrations(?:\/([a-f0-9-]{36})(?:\/(tokens))?)?$/.exec(url.pathname);
      if (!route) throw new ServiceError('API 경로를 찾을 수 없습니다.', 404);
      const [, id, child] = route;
      const methods = child ? ['POST'] : id ? ['GET'] : ['GET', 'POST'];
      if (!methods.includes(request.method)) {
        const error = new ServiceError('지원하지 않는 메서드입니다.', 405);
        error.allow = methods.join(', ');
        throw error;
      }
      if (child) {
        const input = await jsonInput(request);
        if (Object.keys(input).length !== 1 || typeof input.enrollment_key !== 'string') {
          throw new ServiceError('연계 키가 필요합니다.', 422);
        }
        json(response, 201, products.registrations.issue(sessionId, id, input.enrollment_key));
      } else if (id) {
        json(response, 200, products.registrations.get(sessionId, id));
      } else if (request.method === 'GET') {
        json(response, 200, page(products.registrations.list(sessionId), url.searchParams));
      } else {
        const record = products.registrations.create(sessionId, await jsonInput(request));
        json(response, 201, record, { Location: `/api/v1/registrations/${record.id}` });
      }
    },
  };
}
