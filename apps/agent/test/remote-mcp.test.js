import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { createServer } from 'node:http';
import { once } from 'node:events';
import { createRemoteMcpServer } from '../src/remote-mcp.js';
import { createApiClient } from '../src/api.js';
import { uploadedSource } from '../../api/src/http/source.js';

const apiSecret = 'a'.repeat(32);
const webCookie = 'w'.repeat(43), aiCookie = 'i'.repeat(43);
const verifier = 'v'.repeat(43), challenge = createHash('sha256').update(verifier).digest('base64url');

test('an expired bound API session never switches to a newly issued session', async () => {
  let sent;
  const api = createApiClient({ baseUrl: 'https://api.example', env: { RAILSHOT_API_TOKEN: apiSecret },
    session: webCookie, fetchImpl: async (_url, options) => {
      sent = options.headers.cookie;
      return new Response('{}', { status: 200, headers: { 'set-cookie': `railshot_session=${aiCookie}; Path=/` } });
    } });
  await assert.rejects(api.deployment('old-app'), { code: 'SESSION_EXPIRED' });
  assert.equal(sent, `railshot_session=${webCookie}`);
});

test('OAuth joins an existing web session and issues a distinct AI-first session', async (t) => {
  const seen = [];
  const api = createServer(async (request, response) => {
    assert.equal(request.headers.authorization, `Bearer ${apiSecret}`);
    const cookie = /railshot_session=([A-Za-z0-9_-]{43})/.exec(request.headers.cookie || '')?.[1];
    const session = cookie || aiCookie;
    seen.push({ path: request.url, session });
    response.setHeader('content-type', 'application/json');
    if (!cookie) response.setHeader('set-cookie', `railshot_session=${aiCookie}; Path=/; HttpOnly; SameSite=Strict`);
    if (request.url === '/api/v1/sessions') response.end(JSON.stringify({ expires_at: new Date(Date.now() + 3600000).toISOString() }));
    else if (request.url === '/api/v1/deployments' && request.method === 'POST') {
      const source = await uploadedSource(request, true, true);
      assert.equal(source.app, 'file-app');
      assert.equal(source.target_id, 'demo-target');
      assert.equal(source.source_type, 'folder');
      assert.deepEqual(source.files.map(({ path, content }) => [path, content.toString()]), [['index.html', '<h1>Hi</h1>']]);
      assert.equal(request.headers['idempotency-key'], 'file-intent');
      response.writeHead(202).end(JSON.stringify({ resource_id: 'file-deployment', status: 'accepted' }));
    }
    else if (request.url === '/api/v1/deployments/web-app') {
      response.writeHead(session === webCookie ? 200 : 404).end(JSON.stringify(session === webCookie
        ? { id: 'web-app', status: 'succeeded' } : { error: { code: 'NOT_FOUND', message: '없음' } }));
    } else response.writeHead(404).end('{}');
  });
  api.listen(0, '127.0.0.1'); await once(api, 'listening');
  t.after(() => api.close());
  const { server, close } = createRemoteMcpServer({ publicOrigin: 'http://127.0.0.1:4181',
    apiUrl: `http://127.0.0.1:${api.address().port}`, env: { RAILSHOT_API_TOKEN: apiSecret } });
  server.listen(0, '127.0.0.1'); await once(server, 'listening');
  t.after(async () => { await close(); server.close(); });
  const base = `http://127.0.0.1:${server.address().port}`;
  const metadata = await (await fetch(`${base}/.well-known/oauth-protected-resource/mcp`)).json();
  assert.equal(metadata.resource, 'http://127.0.0.1:4181/mcp');
  assert.deepEqual(metadata.authorization_servers, ['http://127.0.0.1:4181']);
  const issuer = await (await fetch(`${base}/.well-known/oauth-authorization-server`)).json();
  assert.equal(issuer.authorization_response_iss_parameter_supported, true);
  assert.deepEqual(issuer.code_challenge_methods_supported, ['S256']);
  const registration = await fetch(`${base}/mcp/register`, { method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ client_name: 'ChatGPT test', redirect_uris: ['https://chatgpt.com/connector/oauth/test', 'http://127.0.0.1/callback'], token_endpoint_auth_method: 'none' }) });
  assert.equal(registration.status, 201);
  const { client_id } = await registration.json();
  const restarted = createRemoteMcpServer({ publicOrigin: 'http://127.0.0.1:4181',
    apiUrl: `http://127.0.0.1:${api.address().port}`, env: { RAILSHOT_API_TOKEN: apiSecret } });
  restarted.server.listen(0, '127.0.0.1'); await once(restarted.server, 'listening');
  t.after(async () => { await restarted.close(); restarted.server.close(); });
  const loopback = new URL(`${base}/mcp/authorize`);
  for (const [key, value] of Object.entries({ response_type: 'code', client_id,
    redirect_uri: 'http://127.0.0.1:49152/callback', code_challenge: challenge,
    code_challenge_method: 'S256', resource: 'http://127.0.0.1:4181/mcp' })) loopback.searchParams.set(key, value);
  const loopbackConsent = await fetch(loopback);
  assert.equal(loopbackConsent.status, 200);
  assert.match(loopbackConsent.headers.get('content-security-policy'), /form-action 'self' http:\/\/127\.0\.0\.1:49152(?:;|$)/);
  assert.equal((await fetch(`http://127.0.0.1:${restarted.server.address().port}${loopback.pathname}${loopback.search}`)).status, 200);

  async function connect(browserCookie) {
    const url = new URL(`${base}/mcp/authorize`);
    for (const [key, value] of Object.entries({ response_type: 'code', client_id,
      redirect_uri: 'https://chatgpt.com/connector/oauth/test', code_challenge: challenge,
      code_challenge_method: 'S256', resource: 'http://127.0.0.1:4181/mcp', state: 'demo-state' })) url.searchParams.set(key, value);
    const consent = await fetch(url);
    assert.equal(consent.status, 200);
    assert.match(consent.headers.get('content-security-policy'), /form-action 'self' https:\/\/chatgpt\.com(?:;|$)/);
    const approval = /name="approval" value="([A-Za-z0-9_-]{43})"/.exec(await consent.text())?.[1];
    assert.ok(approval);
    const authorized = await fetch(`${base}/mcp/authorize`, { method: 'POST', redirect: 'manual',
      headers: { 'content-type': 'application/x-www-form-urlencoded', ...(browserCookie ? { cookie: `railshot_session=${browserCookie}` } : {}) },
      body: new URLSearchParams({ approval }) });
    assert.equal(authorized.status, 302);
    const callback = new URL(authorized.headers.get('location'));
    assert.equal(callback.searchParams.get('state'), 'demo-state');
    assert.equal(callback.searchParams.get('iss'), 'http://127.0.0.1:4181');
    const exchanged = await fetch(`${base}/mcp/token`, { method: 'POST',
      body: new URLSearchParams({ grant_type: 'authorization_code', client_id,
        redirect_uri: 'https://chatgpt.com/connector/oauth/test', code: callback.searchParams.get('code'),
        code_verifier: verifier, resource: 'http://127.0.0.1:4181/mcp' }) });
    assert.equal(exchanged.status, 200);
    return { token: (await exchanged.json()).access_token, cookie: authorized.headers.get('set-cookie') };
  }

  const web = await connect(webCookie), ai = await connect(null);
  assert.equal(web.cookie, null);
  assert.match(ai.cookie, new RegExp(`railshot_session=${aiCookie}`));
  assert.notEqual(web.token, ai.token);
  async function tool(token, name = 'get_deployment', args = { deployment_id: 'web-app' }) {
    const response = await fetch(`${base}/mcp`, { method: 'POST', headers: { authorization: `Bearer ${token}`,
      'content-type': 'application/json', accept: 'application/json, text/event-stream', 'mcp-protocol-version': '2025-11-25' },
      body: JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'tools/call', params: { name, arguments: args } }) });
    assert.equal(response.status, 200);
    const body = await response.text();
    return body.startsWith('event:') ? JSON.parse(/^data: (.*)$/m.exec(body)[1]) : JSON.parse(body);
  }
  assert.equal((await tool(web.token)).result.structuredContent.id, 'web-app');
  assert.equal((await tool(ai.token)).result.isError, true);
  const prepared = (await tool(ai.token, 'prepare_file_deployment', {
    app: 'file-app', target_id: 'demo-target', idempotency_key: 'file-intent',
  })).result.structuredContent;
  assert.equal(prepared.app, 'file-app');
  assert.equal(prepared.target_id, 'demo-target');
  assert.equal((await tool(ai.token, 'get_file_upload', { upload_id: prepared.upload_id })).result.structuredContent.status, 'awaiting_upload');
  assert.equal((await tool(web.token, 'get_file_upload', { upload_id: prepared.upload_id })).result.isError, true);
  const uploadPath = new URL(prepared.upload_url).pathname;
  const uploadPage = await fetch(`${base}${uploadPath}`);
  assert.equal(uploadPage.status, 200);
  const pageHtml = await uploadPage.text();
  assert.match(pageHtml, /ZIP 파일/);
  assert.doesNotThrow(() => new Function(/<script[^>]*>([\s\S]*?)<\/script>/.exec(pageHtml)[1]));
  assert.match(uploadPage.headers.get('content-security-policy'), /script-src 'nonce-/);
  const source = new FormData();
  source.append('files', new Blob(['<h1>Hi</h1>']), 'index.html');
  source.set('paths', '["index.html"]');
  const submitted = await fetch(`${base}${uploadPath}`, { method: 'POST', body: source });
  assert.equal(submitted.status, 202);
  assert.deepEqual(await submitted.json(), { resource_id: 'file-deployment', status: 'accepted' });
  assert.equal((await tool(ai.token, 'get_file_upload', { upload_id: prepared.upload_id })).result.structuredContent.resource_id, 'file-deployment');
  const replay = await fetch(`${base}${uploadPath}`, { method: 'POST', body: source });
  assert.equal(replay.status, 200);
  assert.equal((await replay.json()).resource_id, 'file-deployment');
  assert.equal(seen.filter((entry) => entry.path === '/api/v1/deployments').length, 1);
  assert.ok(seen.some((entry) => entry.path === '/api/v1/deployments' && entry.session === aiCookie));
  assert.equal((await fetch(`${base}/mcp/upload/${'x'.repeat(43)}`)).status, 410);
  assert.equal((await fetch(`${base}/mcp`, { method: 'POST' })).status, 401);
  assert.ok(seen.some((entry) => entry.path === '/api/v1/deployments/web-app' && entry.session === webCookie));
  assert.ok(seen.some((entry) => entry.path === '/api/v1/deployments/web-app' && entry.session === aiCookie));
});
