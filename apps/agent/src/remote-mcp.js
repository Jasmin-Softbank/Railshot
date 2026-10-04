#!/usr/bin/env node
import { createHash, createHmac, randomBytes, timingSafeEqual } from 'node:crypto';
import { createServer } from 'node:http';
import { Readable } from 'node:stream';
import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';
import { createMcpHandler } from '@modelcontextprotocol/server';
import { z } from 'zod';
import { createApiClient } from './api.js';
import { createToolServer } from './mcp-tools.js';
import { uploadPage } from './upload-page.js';

const tokenPattern = /^[A-Za-z0-9_-]{43}$/;
const randomToken = () => randomBytes(32).toString('base64url');
const uploadLimit = 100 * 1024 * 1024 + 1024 * 1024;
const uploadRoute = /^\/mcp\/upload\/([A-Za-z0-9_-]{43})$/;
const uploadSchema = z.object({
  app: z.string().regex(/^[a-z](?:[a-z0-9-]{1,28}[a-z0-9])$/),
  target_id: z.string().regex(/^[A-Za-z0-9._-]{1,128}$/),
  idempotency_key: z.string().regex(/^[A-Za-z0-9._-]{1,128}$/),
}).strict();
const html = (value) => String(value).replace(/[&<>"']/g, (character) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[character]);
const json = (response, status, value) => {
  response.writeHead(status, { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store' });
  response.end(JSON.stringify(value));
};
const form = async (request) => {
  let body = '';
  for await (const chunk of request) {
    body += chunk;
    if (body.length > 8192) throw new Error('요청이 너무 큽니다.');
  }
  return new URLSearchParams(body);
};
const jsonBody = async (request) => {
  let body = '';
  for await (const chunk of request) {
    body += chunk;
    if (body.length > 8192) throw new Error('요청이 너무 큽니다.');
  }
  return JSON.parse(body);
};
const cookieToken = (header) => {
  const entries = (header || '').split(';').map((value) => value.trim()).filter((value) => value.startsWith('railshot_session='));
  const value = entries.length === 1 ? entries[0].slice('railshot_session='.length) : null;
  return value && tokenPattern.test(value) ? value : null;
};
const redirectAllowed = (value) => {
  try {
    const url = new URL(value);
    return !url.username && !url.password && !url.hash && (url.protocol === 'https:'
      || (url.protocol === 'http:' && ['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname)));
  } catch { return false; }
};
const redirectMatches = (registered, requested) => {
  if (registered === requested) return true;
  try {
    const a = new URL(registered), b = new URL(requested);
    return a.protocol === 'http:' && b.protocol === 'http:'
      && ['127.0.0.1', 'localhost', '[::1]'].includes(a.hostname)
      && a.hostname === b.hostname && a.pathname === b.pathname && a.search === b.search
      && !b.username && !b.password && !b.hash;
  } catch { return false; }
};

export function createRemoteMcpServer({ publicOrigin = process.env.RAILSHOT_PUBLIC_ORIGIN || 'https://railshot.io',
  apiUrl = process.env.RAILSHOT_API_URL || 'http://railshot-api:4173', env = process.env, fetchImpl = fetch,
  now = Date.now } = {}) {
  const origin = new URL(publicOrigin);
  if (!['http:', 'https:'].includes(origin.protocol) || origin.pathname !== '/' || origin.search || origin.hash) throw new Error('공개 원점 URL이 잘못되었습니다.');
  const resource = `${origin.origin}/mcp`;
  const approvals = new Map(), codes = new Map(), tokens = new Map(), uploads = new Map();
  async function apiSecret() {
    if (env.RAILSHOT_API_TOKEN && env.RAILSHOT_API_TOKEN_FILE) throw new Error('내부 API 토큰을 중복 설정했습니다.');
    const token = env.RAILSHOT_API_TOKEN || (env.RAILSHOT_API_TOKEN_FILE
      && await (await import('node:fs/promises')).readFile(env.RAILSHOT_API_TOKEN_FILE, 'utf8').then((value) => value.trim()));
    if (!token || token.length < 32) throw new Error('내부 API 토큰이 설정되지 않았습니다.');
    return token;
  }
  async function clientFor(id) {
    if (typeof id !== 'string' || id.length > 2048) return null;
    const match = /^r1\.([A-Za-z0-9_-]+)\.([A-Za-z0-9_-]{43})$/.exec(id);
    if (!match) return null;
    const expected = createHmac('sha256', await apiSecret()).update(match[1]).digest('base64url');
    if (!timingSafeEqual(Buffer.from(expected), Buffer.from(match[2]))) return null;
    try {
      const client = JSON.parse(Buffer.from(match[1], 'base64url').toString('utf8'));
      return Array.isArray(client.redirect_uris) && client.redirect_uris.every(redirectAllowed) ? client : null;
    } catch { return null; }
  }
  const clean = () => {
    for (const map of [approvals, codes, tokens, uploads]) for (const [key, value] of map) if (value.expires <= now()) map.delete(key);
  };
  const mcp = createMcpHandler(({ authInfo }) => {
    const bound = tokens.get(authInfo?.token);
    if (!bound || bound.expires <= now()) throw new Error('AI 연결이 만료되었습니다.');
    return createToolServer(createApiClient({ baseUrl: apiUrl, env, fetchImpl, session: bound.session }), {
      prepare_file_deployment: {
        title: '로컬 파일 배포 업로드 링크 발급',
        description: '지정한 앱과 등록 대상에 ZIP, 개별 파일 또는 로컬 폴더를 배포할 수 있는 짧게 유효한 브라우저 업로드 링크를 발급합니다. 링크에서 파일을 선택하고 배포 요청을 제출해야 실제 실행됩니다.',
        schema: uploadSchema, readOnly: false,
        run: (_api, { app, target_id, idempotency_key }) => {
          if (uploads.size >= 1000) throw new Error('파일 업로드 요청 한도에 도달했습니다. 잠시 뒤 다시 시도하세요.');
          const ticket = randomToken(), expires = now() + 600000;
          uploads.set(ticket, { session: bound.session, app, target_id, idempotency_key, expires, busy: false });
          return { upload_id: ticket, upload_url: `${origin.origin}/mcp/upload/${ticket}`,
            expires_at: new Date(expires).toISOString(), app, target_id };
        },
      },
      get_file_upload: {
        title: '파일 업로드 접수 상태 조회',
        description: '발급된 업로드 ID의 제출 상태와 접수된 배포 ID를 조회합니다. 접수 전에는 배포가 시작되지 않았습니다.',
        schema: z.object({ upload_id: z.string().regex(tokenPattern) }).strict(), readOnly: true,
        run: (_api, { upload_id }) => {
          const upload = uploads.get(upload_id);
          if (!upload || upload.expires <= now() || upload.session !== bound.session) throw new Error('업로드 요청을 찾을 수 없거나 만료되었습니다.');
          return upload.result || { status: upload.busy ? 'uploading' : 'awaiting_upload' };
        },
      },
    });
  }, { legacy: 'stateless' });

  async function ensureSession(request) {
    const existing = cookieToken(request.headers.cookie);
    const apiToken = await apiSecret();
    const result = await fetchImpl(new URL('/api/v1/sessions', apiUrl), { method: 'POST',
      headers: { authorization: `Bearer ${apiToken}`, ...(existing ? { cookie: `railshot_session=${existing}` } : {}) },
      redirect: 'error', signal: AbortSignal.timeout(10000) });
    if (!result.ok) throw new Error(`세션 발급에 실패했습니다 (${result.status}).`);
    const fresh = cookieToken(result.headers.get('set-cookie'));
    const session = fresh || existing;
    if (!session) throw new Error('API가 세션 쿠키를 반환하지 않았습니다.');
    const body = await result.json();
    const expires = Date.parse(body.expires_at);
    if (!Number.isFinite(expires) || expires <= now()) throw new Error('API 세션 만료 시각이 잘못되었습니다.');
    return { session, expires, setCookie: fresh && result.headers.get('set-cookie') };
  }

  const server = createServer(async (request, response) => {
    try {
      clean();
      const url = new URL(request.url, origin);
      response.setHeader('x-content-type-options', 'nosniff');
      if (url.pathname === '/healthz') { response.writeHead(200).end('ok\n'); return; }
      const upload = uploadRoute.exec(url.pathname);
      if (upload && ['GET', 'POST'].includes(request.method)) {
        const ticket = uploads.get(upload[1]);
        if (!ticket || ticket.expires <= now()) { json(response, 410, { error: { message: '업로드 링크가 만료되었습니다. AI에서 새 링크를 발급받으세요.' } }); return; }
        response.setHeader('cache-control', 'no-store');
        response.setHeader('referrer-policy', 'no-referrer');
        if (request.method === 'GET') {
          const nonce = randomToken();
          response.writeHead(200, { 'content-type': 'text/html; charset=utf-8',
            'content-security-policy': `default-src 'none'; script-src 'nonce-${nonce}'; style-src 'unsafe-inline'; connect-src 'self'; form-action 'self'; base-uri 'none'` });
          response.end(uploadPage(ticket, nonce)); return;
        }
        if (ticket.result) { json(response, 200, ticket.result); return; }
        if (ticket.busy) { json(response, 409, { error: { message: '같은 업로드가 처리 중입니다.' } }); return; }
        const contentType = request.headers['content-type'] || '';
        const boundary = /^multipart\/form-data;\s*boundary=([A-Za-z0-9'()+_,.\/:=?-]{1,70})$/i.exec(contentType)?.[1];
        if (!boundary) { json(response, 415, { error: { message: '파일 업로드 형식이 잘못되었습니다.' } }); return; }
        if (Number(request.headers['content-length'] || 0) > uploadLimit) {
          json(response, 413, { error: { message: '파일 업로드 크기가 허용 범위를 초과했습니다.' } }); return;
        }
        ticket.busy = true;
        try {
          const prefix = Buffer.from(`--${boundary}\r\nContent-Disposition: form-data; name="app"\r\n\r\n${ticket.app}\r\n--${boundary}\r\nContent-Disposition: form-data; name="target_id"\r\n\r\n${ticket.target_id}\r\n`);
          const body = Readable.toWeb(Readable.from((async function* () {
            yield prefix;
            let size = 0;
            for await (const chunk of request) {
              size += chunk.length;
              if (size > uploadLimit) throw new Error('파일 업로드 크기가 허용 범위를 초과했습니다.');
              yield chunk;
            }
          })()));
          const api = createApiClient({ baseUrl: apiUrl, env, fetchImpl, session: ticket.session });
          ticket.result = await api.deployMultipart(body, contentType, ticket.idempotency_key);
          json(response, 202, ticket.result);
        } catch (error) {
          json(response, error.status || (error.message.includes('크기') ? 413 : 502), { error: { message: error.message } });
        } finally { ticket.busy = false; }
        return;
      }
      if (request.method === 'GET' && ['/mcp/.well-known/oauth-protected-resource', '/.well-known/oauth-protected-resource', '/.well-known/oauth-protected-resource/mcp'].includes(url.pathname)) {
        json(response, 200, { resource, authorization_servers: [origin.origin], bearer_methods_supported: ['header'] }); return;
      }
      if (request.method === 'GET' && ['/mcp/.well-known/oauth-authorization-server', '/.well-known/oauth-authorization-server'].includes(url.pathname)) {
        json(response, 200, { issuer: origin.origin, authorization_endpoint: `${origin.origin}/mcp/authorize`,
          token_endpoint: `${origin.origin}/mcp/token`, registration_endpoint: `${origin.origin}/mcp/register`,
          authorization_response_iss_parameter_supported: true,
          response_types_supported: ['code'], grant_types_supported: ['authorization_code'],
          token_endpoint_auth_methods_supported: ['none'], code_challenge_methods_supported: ['S256'] }); return;
      }
      if (url.pathname === '/mcp/register' && request.method === 'POST') {
        const input = await jsonBody(request);
        if (!Array.isArray(input.redirect_uris) || input.redirect_uris.length < 1 || input.redirect_uris.length > 5
          || input.redirect_uris.some((item) => typeof item !== 'string' || item.length > 512 || !redirectAllowed(item))
          || (input.token_endpoint_auth_method && input.token_endpoint_auth_method !== 'none')
          || (input.grant_types && !input.grant_types.includes('authorization_code'))) {
          json(response, 400, { error: 'invalid_client_metadata' }); return;
        }
        const client = { client_name: String(input.client_name || 'AI 클라이언트').slice(0, 80),
          redirect_uris: input.redirect_uris, token_endpoint_auth_method: 'none',
          grant_types: ['authorization_code'], response_types: ['code'] };
        const encoded = Buffer.from(JSON.stringify(client)).toString('base64url');
        const signature = createHmac('sha256', await apiSecret()).update(encoded).digest('base64url');
        const clientId = `r1.${encoded}.${signature}`;
        if (clientId.length > 2048) { json(response, 400, { error: 'invalid_client_metadata' }); return; }
        client.client_id = clientId;
        json(response, 201, client); return;
      }
      if (url.pathname === '/mcp/authorize' && request.method === 'GET') {
        const clientId = url.searchParams.get('client_id'), redirectUri = url.searchParams.get('redirect_uri');
        const client = await clientFor(clientId);
        const challenge = url.searchParams.get('code_challenge');
        if (url.searchParams.get('response_type') !== 'code' || url.searchParams.get('code_challenge_method') !== 'S256'
          || !challenge || !tokenPattern.test(challenge) || !client?.redirect_uris.some((item) => redirectMatches(item, redirectUri))) {
          json(response, 400, { error: 'invalid_request' }); return;
        }
        if (url.searchParams.has('resource') && url.searchParams.get('resource') !== resource) {
          json(response, 400, { error: 'invalid_target' }); return;
        }
        if (approvals.size >= 1000) { json(response, 429, { error: 'temporarily_unavailable' }); return; }
        const approval = randomToken();
        approvals.set(approval, { clientId, redirectUri, challenge, state: url.searchParams.get('state'), resource, expires: now() + 600000 });
        response.writeHead(200, { 'content-type': 'text/html; charset=utf-8', 'cache-control': 'no-store',
          'content-security-policy': `default-src 'none'; style-src 'unsafe-inline'; form-action 'self' ${new URL(redirectUri).origin}; base-uri 'none'` });
        response.end(`<!doctype html><html lang="ko"><meta charset="utf-8"><title>Railshot AI 연결</title><style>body{font:16px system-ui;max-width:38rem;margin:4rem auto;padding:1rem;line-height:1.6}button{padding:.7rem 1rem}</style><h1>Railshot AI 연결</h1><p>${html(client.client_name || 'AI 클라이언트')}에 이 브라우저의 배포 관리 권한을 연결합니다. 아직 웹 세션이 없다면 새로 발급합니다.</p><p>연결 후 AI가 배포 도구를 호출할 수 있습니다. 배포 요청은 AI 앱에서 확인하고 실행하세요.</p><form method="post" action="/mcp/authorize"><input type="hidden" name="approval" value="${approval}"><button type="submit">연결 승인</button></form></html>`);
        return;
      }
      if (url.pathname === '/mcp/authorize' && request.method === 'POST') {
        const input = await form(request), approval = input.get('approval'), pending = approvals.get(approval);
        approvals.delete(approval);
        if (!pending || pending.expires <= now()) { json(response, 400, { error: 'invalid_request' }); return; }
        const scoped = await ensureSession(request);
        if (codes.size >= 1000) { json(response, 429, { error: 'temporarily_unavailable' }); return; }
        const code = randomToken();
        codes.set(code, { ...pending, session: scoped.session, expires: Math.min(now() + 600000, scoped.expires), sessionExpires: scoped.expires });
        const target = new URL(pending.redirectUri);
        target.searchParams.set('code', code);
        target.searchParams.set('iss', origin.origin);
        if (pending.state !== null) target.searchParams.set('state', pending.state);
        response.writeHead(302, { location: target.href, 'cache-control': 'no-store', ...(scoped.setCookie ? { 'set-cookie': scoped.setCookie } : {}) });
        response.end(); return;
      }
      if (url.pathname === '/mcp/token' && request.method === 'POST') {
        const input = await form(request), code = input.get('code'), entry = codes.get(code);
        codes.delete(code);
        if (input.get('grant_type') !== 'authorization_code' || !entry || entry.expires <= now()
          || input.get('client_id') !== entry.clientId || input.get('redirect_uri') !== entry.redirectUri
          || (input.has('resource') && input.get('resource') !== entry.resource)) {
          json(response, 400, { error: 'invalid_grant' }); return;
        }
        const verifier = input.get('code_verifier') || '';
        if (!/^[A-Za-z0-9._~-]{43,128}$/.test(verifier)) { json(response, 400, { error: 'invalid_grant' }); return; }
        const digest = createHash('sha256').update(verifier).digest('base64url');
        if (!timingSafeEqual(Buffer.from(digest), Buffer.from(entry.challenge))) { json(response, 400, { error: 'invalid_grant' }); return; }
        const accessToken = randomToken(), expires = Math.min(entry.sessionExpires, now() + 86400000);
        if (tokens.size >= 10000) { json(response, 429, { error: 'temporarily_unavailable' }); return; }
        tokens.set(accessToken, { session: entry.session, clientId: entry.clientId, resource: entry.resource, expires });
        json(response, 200, { access_token: accessToken, token_type: 'Bearer', expires_in: Math.max(1, Math.floor((expires - now()) / 1000)) }); return;
      }
      if (url.pathname === '/mcp') {
        const match = /^Bearer ([A-Za-z0-9_-]{43})$/.exec(request.headers.authorization || '');
        const bound = match && tokens.get(match[1]);
        if (!bound || bound.expires <= now() || bound.resource !== resource) {
          response.writeHead(401, { 'www-authenticate': `Bearer resource_metadata="${origin.origin}/.well-known/oauth-protected-resource/mcp"`,
            'cache-control': 'no-store' }).end(); return;
        }
        const webRequest = new Request(resource, { method: request.method, headers: request.headers,
          ...(request.method === 'POST' ? { body: Readable.toWeb(request), duplex: 'half' } : {}), signal: AbortSignal.timeout(30000) });
        const result = await mcp.fetch(webRequest, { authInfo: { token: match[1], clientId: bound.clientId, scopes: [] } });
        response.writeHead(result.status, Object.fromEntries(result.headers));
        if (result.body) Readable.fromWeb(result.body).on('error', () => response.destroy()).pipe(response);
        else response.end();
        return;
      }
      json(response, 404, { error: 'not_found' });
    } catch (error) {
      if (response.headersSent) response.destroy();
      else if (error instanceof SyntaxError) json(response, 400, { error: 'invalid_request' });
      else json(response, 500, { error: 'server_error', message: error.message });
    }
  });
  return { server, close: () => mcp.close() };
}

if (process.argv[1] && fileURLToPath(import.meta.url) === resolve(process.argv[1])) {
  const { server } = createRemoteMcpServer();
  // Kubernetes injects RAILSHOT_MCP_PORT as a Service URL, not a numeric listen port.
  server.listen(Number(process.env.RAILSHOT_MCP_LISTEN_PORT || 4185), process.env.RAILSHOT_MCP_HOST || '0.0.0.0');
}
